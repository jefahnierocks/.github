"""Explicit hosted-only preparation of Python and Git in an owned prefix.

Adapted from the public MIT project-manifest scripts/prepare-linux-tools.py;
the exact released MIT license is retained under vendor/project-manifest/.
"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).absolute().parent.parent
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_MEMBERS = 25_000
QUALIFICATION_PINS = {
    "git": {
        "version": "2.56.0",
        "url": "https://www.kernel.org/pub/software/scm/git/git-2.56.0.tar.xz",
        "checksum": "sha256:26c56c296b38c0695b26fa95f475f1d01704d2d38e73465ca30b0b2f5dc789d3",
    },
}
# Git's archive checksum is taken from the official HTTPS-published checksum list.
# GitHub's verified tag describes another object; no tar-signature claim is made.
# https://www.kernel.org/pub/software/scm/git/sha256sums.asc


class PreparationError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise PreparationError(code)


def artifact(version, entry, tool):
    checksum = entry.get("checksum", "")
    url = entry.get("url", "")
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", checksum), "LOCK_CHECKSUM")
    require(parsed.scheme == "https" and parsed.hostname == "github.com"
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment, "LOCK_ORIGIN")
    require(tool == "python" and path.startswith("/astral-sh/python-build-standalone/releases/download/")
            and re.fullmatch(r"cpython-" + re.escape(version)
                             + r"\+[0-9]{8}-x86_64-unknown-linux-gnu-install_only_stripped\.tar\.gz",
                             path.rsplit("/", 1)[-1])
            and entry.get("provenance") == "github-attestations", "LOCK_PYTHON_ARTIFACT")
    return {"version": version, "url": url, "checksum": checksum}


def load_artifacts(root):
    config = tomllib.loads((root / "mise.toml").read_text())
    lock = tomllib.loads((root / "mise.lock").read_text())
    require(config["tools"] == {"python": "3.13.16"}
            and set(lock["tools"]) == set(config["tools"]), "LOCK_TOOL_SET")
    result = dict(QUALIFICATION_PINS)
    for tool, version in config["tools"].items():
        require(isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), "LOCK_VERSION")
        entries = lock["tools"][tool]
        require(len(entries) == 1 and entries[0].get("version") == version
                and entries[0].get("specifiers") == [version], "LOCK_PIN_MATCH")
        expected = "core:python"
        require(entries[0].get("backend") == expected, "LOCK_BACKEND")
        linux = entries[0].get("platforms.linux-x64")
        require(isinstance(linux, dict), "LOCK_LINUX_MISSING")
        result["python"] = artifact(version, linux, tool)
    return result


def require_host():
    require(platform.system() == "Linux" and platform.machine() == "x86_64"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_OS") == "Linux"
            and os.environ.get("RUNNER_ARCH") == "X64", "UNQUALIFIED_PREPARATION_HOST")
    require(sys.version_info >= (3, 11) and hasattr(tarfile, "data_filter"), "BOOTSTRAP_RUNTIME")


def download(info, path):
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + 60
    with urlopen(Request(info["url"], headers={"User-Agent": "github-profile-source-qualification"}), timeout=30) as response:
        with path.open("xb") as output:
            while chunk := response.read(65_536):
                size += len(chunk)
                require(size <= MAX_DOWNLOAD and time.monotonic() <= deadline, "DOWNLOAD_BOUND")
                digest.update(chunk)
                output.write(chunk)
    require("sha256:" + digest.hexdigest() == info["checksum"], "ARCHIVE_CHECKSUM")


def extract(archive, destination):
    require(not destination.exists(), "EXTRACTION_TARGET_EXISTS")
    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        require(len(members) <= MAX_MEMBERS, "ARCHIVE_MEMBER_BOUND")
        require(sum(member.size for member in members) <= MAX_EXPANDED, "ARCHIVE_BYTE_BOUND")
        for member in members:
            parts = PurePosixPath(member.name).parts
            require(parts and not member.name.startswith("/") and ".." not in parts
                    and "\\" not in member.name and "\x00" not in member.name, "ARCHIVE_PATH")
            require(member.isfile() or member.isdir() or member.issym(), "ARCHIVE_ENTRY")
            if member.issym():
                require(not member.linkname.startswith("/") and "\\" not in member.linkname, "ARCHIVE_LINK")
            tarfile.data_filter(member, str(destination))
        destination.mkdir()
        source.extractall(destination, members=members, filter="data")


def run(argv, *, cwd, seconds=600):
    """Bind cancellation before spawn; independently bound cleanup and restoration."""
    process = None
    previous = {}
    cancelled = False
    cleaning = False
    failure = None
    cleanup_errors = []
    restore_errors = []

    def interrupted(signum, frame):
        nonlocal cancelled
        cancelled = True
        # Popen must return its child handle before cancellation can unwind it.
        # During cleanup, another signal records cancellation without interrupting reap.
        if process is not None and not cleaning:
            raise PreparationError("PREPARATION_INTERRUPTED")

    def stop():
        # Each operation is attempted even when the preceding signal/wait fails.
        # Never wait indefinitely, including the final reap after SIGKILL.
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                if signum == signal.SIGKILL:
                    cleanup_errors.append(error)
            except BaseException as error:
                cleanup_errors.append(error)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            # Record first so a partial handler installation can be restored too.
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupted)
        require(not cancelled, "PREPARATION_INTERRUPTED")
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        require(not cancelled, "PREPARATION_INTERRUPTED")
        process.wait(timeout=seconds)
        require(not cancelled, "PREPARATION_INTERRUPTED")
        require(process.returncode == 0, "PREPARATION_COMMAND")
    except BaseException as error:
        failure = error
    finally:
        cleaning = True
        try:
            if process is not None and (failure is not None or cancelled):
                stop()
        finally:
            for signum, handler in previous.items():
                try:
                    signal.signal(signum, handler)
                except BaseException as error:
                    restore_errors.append(error)
    if cleanup_errors:
        raise PreparationError("PREPARATION_CLEANUP_UNCONFIRMED") from failure
    if restore_errors:
        raise PreparationError("PREPARATION_HANDLER_RESTORE_FAILED") from failure
    # A signal during success-time handler restoration must not authorize the
    # next preparation command. The successful child was already reaped.
    if cancelled:
        raise PreparationError("PREPARATION_INTERRUPTED") from failure
    if failure is not None:
        raise failure


def observed(argv):
    result = subprocess.run(argv, capture_output=True, check=False, timeout=10)
    require(result.returncode == 0 and len(result.stdout) + len(result.stderr) <= 16_384, "TOOL_OBSERVATION")
    return result.stdout.decode("utf-8").strip()


def prepare():
    require_host()
    selected = load_artifacts(ROOT)
    parent = Path(os.environ.get("RUNNER_TEMP", ""))
    require(parent.is_absolute() and parent.is_dir() and not parent.is_symlink(), "RUNNER_TEMP_REQUIRED")
    resolved = parent.resolve()
    checkout = ROOT.resolve()
    require(resolved != checkout and checkout not in resolved.parents, "TOOL_PREFIX_IN_CHECKOUT")
    container = Path(tempfile.mkdtemp(prefix="github-profile-linux-tools-", dir=parent))
    require(not container.is_symlink() and container.resolve().parent == resolved
            and container.stat().st_uid == os.getuid(), "TOOL_PREFIX_OWNERSHIP")
    (container / "qualification-owner").write_text("Owned hosted source qualification tools\n")
    paths = {}
    for name, info in selected.items():
        archive = container / (name + ".archive")
        destination = container / name
        download(info, archive)
        extract(archive, destination)
        paths[name] = destination
    git_source = paths["git"] / ("git-" + selected["git"]["version"])
    git_prefix = container / "git-prefix"
    run(["make", "-j2", "prefix=" + str(git_prefix), "NO_OPENSSL=YesPlease",
         "NO_CURL=YesPlease", "NO_EXPAT=YesPlease", "NO_GETTEXT=YesPlease",
         "NO_TCLTK=YesPlease", "NO_PERL=YesPlease", "NO_PYTHON=YesPlease", "NO_RUST=YesPlease", "all", "install"], cwd=git_source)
    binaries = {"python": paths["python"] / "python/bin/python3", "git": git_prefix / "bin/git"}
    for binary in binaries.values():
        require(binary.is_file() and os.access(binary, os.X_OK)
                and container.resolve() in binary.resolve().parents, "PREPARED_BINARY")
    versions = {}
    for name in ("python",):
        versions[name] = observed([str(binaries[name]), "-I", "-B", "-c", "import platform; print(platform.python_version())"])
        require(versions[name] == selected[name]["version"], "PREPARED_PYTHON_VERSION")
    versions["git"] = observed([str(binaries["git"]), "--version"])
    require(versions["git"] == "git version " + selected["git"]["version"], "PREPARED_GIT_VERSION")
    report = {"scope": "github-profile-linux-tool-preparation", "versions": versions,
              "binary_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()},
              "lock_sha256": hashlib.sha256((ROOT / "mise.lock").read_bytes()).hexdigest(),
              "git_source_checksum_trust": "official-HTTPS-published-SHA256; tar-signature-not-verified",
              "compiled_git": True, "release_or_admission": "not_checked"}
    (container / "tool-observation.json").write_text(json.dumps(report, sort_keys=True) + "\n")
    output = Path(os.environ["GITHUB_OUTPUT"])
    with output.open("a") as stream:
        for name, path in binaries.items():
            stream.write(name + "=" + str(path) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    try:
        prepare()
    except (PreparationError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(json.dumps({"scope": "owned-linux-tool-preparation", "exit_code": 2,
                          "reason": str(error) if isinstance(error, PreparationError) else type(error).__name__}, sort_keys=True))
        raise SystemExit(2)
