"""Offline profile declaration consumer of the immutable public Release 1.

Captured-byte loader adapted from the public MIT project-manifest origin's
check-self-adoption.py. The vendored release retains its MIT license.
"""

import sys

if __name__ == "__main__" and not (
    sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
):
    sys.stdout.write('{"exit_code":2,"finding":"ISOLATED_RUNTIME_REQUIRED"}\n')
    raise SystemExit(2)

import argparse
import datetime
from contextlib import contextmanager
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).absolute().parent.parent
SOURCE_REVISION = "56e3920089726e585eaa0fd7f4dc94e6938a1e0c"
BOOTSTRAP_SHA256 = "2ccf58e0da97bbbd9dae173d1cca047d465c4a2d950ee6e56052309a9689cf4f"
TABLE_SHA256 = "f7ff9dd0972af5d3a6a3348c3e092de1a6acd97fa3975e13e522cf80ce5e7df2"
VENDOR = "vendor/project-manifest/"
BOOTSTRAP = VENDOR + "bootstrap/release_verifier.py"
BUNDLE = VENDOR + "release-1/"
MODULES = {
    "project_manifest": "src/project_manifest/__init__.py",
    **{"project_manifest." + name: "src/project_manifest/" + name + ".py"
       for name in ("candidate", "validation", "acquire")},
}
PAYLOAD = tuple(sorted((
    "LICENSE", "docs/specification-candidate.md", "docs/field-table.md",
    "docs/enum-provenance.md", "docs/validation-candidate.md",
    "docs/acquisition-candidate.md", "docs/offline-binding-candidate.md",
    *MODULES.values(), "src/project_manifest/__main__.py", "src/project_manifest/cli.py", "tests/acquisition_fixtures.py", "tests/test_candidate.py",
    "tests/test_validation.py", "tests/test_acquisition.py", "tests/test_cli.py",
    "tests/run_corpus.py", "tests/compare_loaders.py",
)))
MAX_BYTES = 1_048_576


class Refusal(Exception):
    def __init__(self, code, exit_code=1):
        self.code, self.exit_code = code, exit_code


def require(condition, code, exit_code=2):
    if not condition:
        raise Refusal(code, exit_code)


def capture(path):
    """No-follow bounded read; retained bytes, never the pathname, are executed."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.parts[1:]
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(leaf)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "VENDOR_FILE_TYPE")
            require(before.st_size <= MAX_BYTES, "VENDOR_BYTE_BOUND")
            chunks, size = [], 0
            while True:
                chunk = os.read(leaf, min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                require(size <= MAX_BYTES, "VENDOR_BYTE_BOUND")
            key = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            require(key(before) == key(os.fstat(leaf))
                    == key(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False))
                    and size == before.st_size, "VENDOR_CHANGED")
            return b"".join(chunks)
        finally:
            os.close(leaf)
    finally:
        os.close(fd)


class CapturedModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Closed import namespace backed by already authenticated in-memory bytes."""

    def __init__(self, sources, package=None):
        self.sources, self.package = sources, package

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.sources:
            return importlib.util.spec_from_loader(fullname, self,
                                                   is_package=fullname == self.package)
        if self.package and fullname.startswith(self.package + "."):
            raise ImportError("Undeclared released module")
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.sources[module.__name__], "<released-module>", "exec"), module.__dict__)

    @contextmanager
    def installed(self):
        names = lambda: [name for name in sys.modules if name in self.sources
                         or (self.package and name.startswith(self.package + "."))]
        require(not names(), "IMPORT_NAMESPACE_OCCUPIED", 2)
        sys.meta_path.insert(0, self)
        try:
            yield
        finally:
            sys.meta_path.remove(self)
            for name in names():
                del sys.modules[name]


def verified_runtime(root):
    bootstrap = capture(root / BOOTSTRAP)
    require(hashlib.sha256(bootstrap).hexdigest() == BOOTSTRAP_SHA256, "BOOTSTRAP_DIGEST")
    with CapturedModules({"_profile_release_bootstrap": bootstrap}).installed():
        verifier = __import__("_profile_release_bootstrap")
        original = verifier.verify_release(str(root / BUNDLE), expected_digest=TABLE_SHA256,
                                           expected_format="project-manifest-release-1")
        require(original.exit_code == 0, "RELEASE_BINDING")
        captured = {name: capture(root / BUNDLE / name) for name in (*PAYLOAD, "release.json")}
        with tempfile.TemporaryDirectory(prefix="profile-manifest-binding-") as temporary:
            bundle = Path(temporary).resolve() / "bundle"
            bundle.mkdir()
            for name, data in captured.items():
                target = bundle / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            result = verifier.verify_release(str(bundle), expected_digest=TABLE_SHA256,
                                             expected_format="project-manifest-release-1")
            require(result.exit_code == 0, "RELEASE_BINDING")
            descriptor = result.descriptor
            require(descriptor["source_revision"] == SOURCE_REVISION
                    and descriptor["origin"] == "jefahnierocks/project-manifest"
                    and descriptor["release_id"] == "1"
                    and tuple(row["path"] for row in descriptor["files"]) == PAYLOAD,
                    "RELEASE_IDENTITY")
            rows = {row["path"]: row for row in descriptor["files"]}
            for name, data in captured.items():
                if name == "release.json":
                    continue
                require(len(data) == rows[name]["bytes"]
                        and hashlib.sha256(data).hexdigest() == rows[name]["sha256"],
                        "CAPTURE_DIGEST")
    return {module: captured[path] for module, path in MODULES.items()}


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal("ARGUMENTS", 2)


def select_git():
    selected = os.environ.get("PROFILE_MANIFEST_GIT") or shutil.which("git")
    require(selected and Path(selected).is_absolute(), "NATIVE_GIT", 2)
    git = Path(selected).resolve(strict=True)
    require(git.name == "git" and git.is_file() and os.access(git, os.X_OK), "NATIVE_GIT", 2)
    with git.open("rb") as stream:
        magic = stream.read(4)
    require(magic in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                      b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"), "NATIVE_GIT", 2)
    measured = subprocess.run([str(git), "--version"], capture_output=True,
                              timeout=10, check=False,
                              env={"PATH": str(git.parent), "LC_ALL": "C"})
    require(measured.returncode == 0 and measured.stdout == b"git version 2.56.0\n"
            and not measured.stderr, "PROJECT_GIT_PIN", 2)
    return git


def native_declaration(record):
    commands = {name: None for name in
                ("setup", "check", "test", "build", "dev", "format_check", "format_write")}
    commands.update(check={"runner": "mise", "target": "check"},
                    test={"runner": "mise", "target": "test"})
    require(record["project"] == {"id": "github-profile",
            "display_name": "Jefahnierocks organization profile", "owner_entity": "jefahnierocks"}
            and record["role"] == "canonical" and record["canonical"] is None
            and record["peers"] == [] and record["authority"]["status_of_record"] == "README.md",
            "PROJECT_DECLARATION", 1)
    require(record["development"] == {
        "platforms": ["macos-arm64", "linux-x86_64"], "commands": commands,
        "environment": {"owner": "none", "template": None, "secret_launcher": None},
        "stacks": {}, "host_requirements": [],
    }, "NATIVE_DECLARATION", 1)


def main(argv=None):
    report = {"scope": "github-profile-interface-1", "exit_code": 2,
              "declaration_validity": "not_checked", "declared_native_contract": "not_checked",
              "native_conformance": "not_checked", "producer_admission": "not_checked",
              "runtime_acceptance": "not_checked", "observation": None,
              "source_revision": SOURCE_REVISION, "table_sha256": TABLE_SHA256,
              "bootstrap_sha256": BOOTSTRAP_SHA256}
    prior_path = os.environ.get("PATH")
    try:
        parser = Arguments(description=__doc__)
        parser.add_argument("--rev")
        parser.add_argument("--git-common-dir", default=os.environ.get("PROFILE_MANIFEST_COMMON_DIR"))
        parser.add_argument("--reference-date", default=datetime.datetime.now(datetime.timezone.utc).date().isoformat())
        args = parser.parse_args(argv)
        require(sys.version_info[:3] == (3, 13, 16), "PROJECT_PYTHON_PIN", 2)
        rev = args.rev
        if os.environ.get("GITHUB_ACTIONS") == "true":
            expected = os.environ.get("GITHUB_SHA", "")
            require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected)
                    and (rev is None or rev == expected), "CI_REVISION", 2)
            rev = expected
        require(rev is None or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", rev), "REVISION", 2)
        sources = verified_runtime(ROOT)
        selected_git = select_git()
        os.environ["PATH"] = str(selected_git.parent) + os.pathsep + (prior_path or "")
        with CapturedModules(sources, "project_manifest").installed():
            api = __import__("project_manifest.acquire", fromlist=["observe_released_repository"])
            result = api.observe_released_repository(
                str(ROOT), mode="rev" if rev else "worktree", rev=rev,
                reference_date=args.reference_date, common_dir=args.git_common_dir)
            report["observation"] = result.diagnostics
            require(result.exit_code == 0, "RELEASED_OBSERVATION", result.exit_code)
            report["declaration_validity"] = "valid"
            native_declaration(result.record)
        report.update(exit_code=0, declared_native_contract="matched")
    except Refusal as error:
        report.update(exit_code=error.exit_code, finding=error.code)
        if error.code == "RELEASED_OBSERVATION" and error.exit_code == 1:
            report["declaration_validity"] = "invalid"
    except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError):
        report.update(exit_code=2, finding="INTERFACE_UNAVAILABLE")
    finally:
        if prior_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = prior_path
    print(json.dumps(report, sort_keys=True))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
