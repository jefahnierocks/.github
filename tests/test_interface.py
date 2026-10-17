"""Synthetic consumer coverage; no actual project or host runtime is executed."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).absolute().parent.parent
GIT = str(Path(os.environ.get("PROFILE_MANIFEST_GIT") or shutil.which("git")).resolve())


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Interface(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="github-profile-interface-test-")
        self.repo = Path(self.temporary.name).resolve()
        for name in ("scripts", "profile"):
            (self.repo / name).mkdir()
        self.adapter = self.repo / "scripts/check-project-manifest.py"
        shutil.copyfile(ROOT / "scripts/check-project-manifest.py", self.adapter)
        shutil.copytree(ROOT / "vendor", self.repo / "vendor")
        for name in ("project.yaml", "README.md", "mise.toml", ".gitattributes"):
            shutil.copyfile(ROOT / name, self.repo / name)
        (self.repo / "profile/README.md").write_text("# Synthetic profile\n")
        self.env = os.environ.copy()
        for name in tuple(self.env):
            if name.startswith("GIT_") or name.startswith("GITHUB_") or name == "PROFILE_MANIFEST_COMMON_DIR":
                self.env.pop(name)
        self.env["PROFILE_MANIFEST_GIT"] = GIT
        self.git("init", "--quiet", "--initial-branch=main")
        self.commit()

    def tearDown(self):
        # Only successful owned fixtures are removed; failed ones remain inspectable.
        result = self._outcome.result
        if not any(test is self for test, _ in (*result.failures, *result.errors)):
            self.temporary.cleanup()
        else:
            self.temporary._finalizer.detach()

    def git(self, *args):
        env = {**self.env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.check_output([GIT, "-C", str(self.repo), *args], env=env,
                                       stderr=subprocess.PIPE, text=True).strip()

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "synthetic fixture")
        return self.git("rev-parse", "HEAD")

    def gate(self, status, *args, env=None, isolated=True):
        flags = ["-I", "-S", "-B"] if isolated else ["-B"]
        result = subprocess.run([sys.executable, *flags, str(self.adapter), *args],
                                env={**self.env, **(env or {})}, capture_output=True,
                                text=True, timeout=65)
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        self.assertEqual(report["exit_code"], status)
        return report

    def test_actual_released_worktree_scope(self):
        report = self.gate(0)
        self.assertEqual(report["declaration_validity"], "valid")
        self.assertEqual(report["declared_native_contract"], "matched")
        self.assertEqual(report["observation"]["mode"], "worktree")
        self.assertEqual(report["observation"]["complete_v1_validity"], "not_checked")
        self.assertEqual(report["runtime_acceptance"], "not_checked")
        self.assertEqual(report["producer_admission"], "not_checked")

    def test_revision_not_dirty_worktree(self):
        oid = self.git("rev-parse", "HEAD")
        original = (self.repo / "project.yaml").read_bytes()
        (self.repo / "project.yaml").write_text("invalid local bytes\n")
        report = self.gate(0, "--rev", oid)
        self.assertEqual(report["observation"]["commit_oid"], oid)
        self.assertEqual(report["observation"]["manifest_sha256"], hashlib.sha256(original).hexdigest())
        self.gate(1)

    def test_event_revision_always_literal(self):
        oid = self.git("rev-parse", "HEAD")
        report = self.gate(0, env={"GITHUB_ACTIONS": "true", "GITHUB_SHA": oid})
        self.assertEqual(report["observation"]["commit_oid"], oid)
        self.gate(2, env={"GITHUB_ACTIONS": "true", "GITHUB_SHA": "HEAD"})
        self.gate(2, "--rev", "0" * 40, env={"GITHUB_ACTIONS": "true", "GITHUB_SHA": oid})
        self.gate(2, "--rev", "HEAD")

    def test_no_isolation_is_refused(self):
        self.assertEqual(self.gate(2, isolated=False)["finding"], "ISOLATED_RUNTIME_REQUIRED")

    def test_wrong_python_pin_refused(self):
        module = load(self.adapter, "wrong_python_fixture")
        from contextlib import redirect_stdout
        from io import StringIO
        with patch.object(module.sys, "version_info", (3, 12, 0)), redirect_stdout(StringIO()) as output:
            status = module.main([])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["finding"], "PROJECT_PYTHON_PIN")

    def test_bad_manifest_is_value_blind(self):
        (self.repo / "project.yaml").write_text('spec_version: 0.1\nunknown: "hidden-value-canary"\n')
        report = self.gate(1)
        self.assertEqual(report["declaration_validity"], "invalid")
        self.assertNotIn("hidden-value-canary", json.dumps(report))

    def test_missing_tracked_reference(self):
        (self.repo / "README.md").unlink()
        self.gate(1)

    def test_untracked_reference_not_accepted(self):
        self.git("rm", "--cached", "README.md")
        self.gate(1)

    def test_project_shape_not_inferred(self):
        target = self.repo / "project.yaml"
        target.write_text(target.read_text().replace("id: github-profile", "id: another-profile"))
        report = self.gate(1)
        self.assertEqual(report["declaration_validity"], "valid")
        self.assertEqual(report["finding"], "PROJECT_DECLARATION")

    def test_inherited_git_override_refused(self):
        self.gate(2, env={"GIT_DIR": str(self.repo / ".git")})

    def test_script_cannot_impersonate_native_git(self):
        prefix = self.repo / "shim"
        prefix.mkdir()
        git = prefix / "git"
        marker = self.repo / "script-executed"
        git.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\necho "git version 2.56.0"\n')
        git.chmod(0o755)
        self.gate(2, env={"PROFILE_MANIFEST_GIT": str(git)})
        self.assertFalse(marker.exists())

    def test_bootstrap_tamper_not_executed(self):
        target = self.repo / "vendor/project-manifest/bootstrap/release_verifier.py"
        target.write_text(target.read_text() + '\nraise RuntimeError("unverified execution")\n')
        self.gate(2)

    def test_release_content_tamper(self):
        target = self.repo / "vendor/project-manifest/release-1/LICENSE"
        target.write_text(target.read_text() + "altered\n")
        self.gate(2)

    def test_extra_release_leaf(self):
        (self.repo / "vendor/project-manifest/release-1/extra").write_text("extra\n")
        self.gate(2)

    def test_linked_release_leaf(self):
        target = self.repo / "vendor/project-manifest/release-1/LICENSE"
        target.unlink()
        target.symlink_to(self.repo / "README.md")
        self.gate(2)

    def test_only_four_library_modules_importable(self):
        module = load(self.adapter, "closed_namespace_fixture")
        sources = module.verified_runtime(self.repo)
        self.assertEqual(set(sources), {"project_manifest", "project_manifest.candidate",
                                       "project_manifest.validation", "project_manifest.acquire"})
        with module.CapturedModules(sources, "project_manifest").installed():
            __import__("project_manifest.acquire")
            for name in ("__main__", "cli", "unlisted"):
                with self.assertRaises(ImportError):
                    __import__("project_manifest." + name)
        self.assertFalse(any(name.startswith("project_manifest") for name in sys.modules))

    def test_runtime_uses_captured_bytes(self):
        module = load(self.adapter, "captured_bytes_fixture")
        sources = module.verified_runtime(self.repo)
        target = self.repo / "vendor/project-manifest/release-1/src/project_manifest/acquire.py"
        target.write_text('raise RuntimeError("path reread executed")\n')
        with module.CapturedModules(sources, "project_manifest").installed():
            api = __import__("project_manifest.acquire", fromlist=["observe_released_repository"])
            self.assertTrue(callable(api.observe_released_repository))

    def test_checkout_attributes_preserve_release(self):
        attributes = self.repo / "ambient-attributes"
        attributes.write_text('* text eol=crlf filter=poison ident working-tree-encoding=UTF-16\n')
        target = self.repo / "checkout"
        target.mkdir()
        # Only protected leaves; unrelated README deliberately has no transformation policy.
        paths = ["project.yaml", *self.git("ls-files", "vendor").splitlines()]
        self.git("-c", "core.attributesFile=" + str(attributes), "-c", "filter.poison.required=true",
                 "-c", "filter.poison.smudge=false", "checkout-index", "--prefix=" + str(target) + "/", "--", *paths)
        for name in paths:
            self.assertEqual((target / name).read_bytes(), (self.repo / name).read_bytes())


class Preparation(unittest.TestCase):
    def setUp(self):
        self.module = load(ROOT / "scripts/prepare-linux-tools.py", "profile_prep_fixture")

    def test_current_lock_has_exact_two_tools(self):
        selected = self.module.load_artifacts(ROOT)
        self.assertEqual(set(selected), {"python", "git"})
        self.assertEqual(selected["python"]["version"], "3.13.16")
        self.assertEqual(selected["git"]["version"], "2.56.0")

    def test_local_host_refused_before_download(self):
        with patch.object(self.module.platform, "system", return_value="Darwin"), \
                patch.object(self.module, "download", side_effect=AssertionError("download ran")):
            with self.assertRaises(self.module.PreparationError):
                self.module.prepare()

    def test_wrong_checksum_refused(self):
        import io
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(self.module, "urlopen", return_value=io.BytesIO(b"altered")):
                with self.assertRaises(self.module.PreparationError):
                    self.module.download({"url": "https://example.invalid/archive", "checksum": "sha256:" + "0" * 64}, Path(folder) / "archive")

    def test_build_timeout_reaps_owned_group_and_restores_handlers(self):
        from unittest.mock import Mock, call
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1), 0, 0]
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(self.module.subprocess, "Popen", return_value=process), \
                patch.object(self.module.os, "killpg") as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.module.run(["fixture"], cwd=ROOT, seconds=1)
            self.assertEqual(kill.call_args_list, [call(12345, signal.SIGTERM), call(12345, signal.SIGKILL)])
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)
        self.assertEqual(process.wait.call_count, 3)

    def test_spawn_cancellation_is_deferred_until_handle_registered(self):
        from unittest.mock import Mock, call
        process = Mock(pid=12345, returncode=0)
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        events = []
        def spawn(*args, **kwargs):
            self.assertTrue(kwargs["start_new_session"])
            self.assertTrue(all(signal.getsignal(sig) != handler for sig, handler in before.items()))
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            events.append("spawn-returned-handle")
            return process
        def killed(pid, signum):
            self.assertEqual(events, ["spawn-returned-handle"])
        with patch.object(self.module.subprocess, "Popen", side_effect=spawn), \
                patch.object(self.module.os, "killpg", side_effect=killed) as kill:
            with self.assertRaisesRegex(self.module.PreparationError, "PREPARATION_INTERRUPTED"):
                self.module.run(["fixture"], cwd=ROOT)
        self.assertEqual(kill.call_args_list, [call(12345, signal.SIGTERM), call(12345, signal.SIGKILL)])
        self.assertEqual(process.wait.call_args_list, [call(timeout=5), call(timeout=5)])
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_spawn_failure_restores_handlers_without_group_signal(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(self.module.subprocess, "Popen", side_effect=OSError("fixture spawn")), \
                patch.object(self.module.os, "killpg") as kill:
            with self.assertRaisesRegex(OSError, "fixture spawn"):
                self.module.run(["fixture"], cwd=ROOT)
        kill.assert_not_called()
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_partial_handler_install_restores_before_any_spawn(self):
        original_signal = signal.signal
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        failed = False
        def change(signum, handler):
            nonlocal failed
            if signum == signal.SIGTERM and handler != before[signum] and not failed:
                failed = True
                raise OSError("fixture handler installation")
            return original_signal(signum, handler)
        with patch.object(self.module.signal, "signal", side_effect=change), \
                patch.object(self.module.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(OSError, "fixture handler installation"):
                self.module.run(["fixture"], cwd=ROOT)
        spawn.assert_not_called()
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_cancellation_during_handler_install_prevents_spawn(self):
        original_signal = signal.signal
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        def change(signum, handler):
            old = original_signal(signum, handler)
            if signum == signal.SIGINT and handler != before[signum]:
                handler(signum, None)
            return old
        with patch.object(self.module.signal, "signal", side_effect=change), \
                patch.object(self.module.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(self.module.PreparationError, "PREPARATION_INTERRUPTED"):
                self.module.run(["fixture"], cwd=ROOT)
        spawn.assert_not_called()
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_kill_and_wait_errors_do_not_skip_final_cleanup(self):
        from unittest.mock import Mock, call
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1), OSError("fixture wait"), 0]
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(self.module.subprocess, "Popen", return_value=process), \
                patch.object(self.module.os, "killpg", side_effect=[PermissionError("fixture kill"), None]) as kill:
            with self.assertRaisesRegex(self.module.PreparationError, "PREPARATION_CLEANUP_UNCONFIRMED"):
                self.module.run(["fixture"], cwd=ROOT, seconds=1)
        self.assertEqual(kill.call_args_list, [call(12345, signal.SIGTERM), call(12345, signal.SIGKILL)])
        self.assertEqual(process.wait.call_args_list, [call(timeout=1), call(timeout=5), call(timeout=5)])
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_final_reap_is_bounded_and_failure_reported(self):
        from unittest.mock import Mock, call
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", value) for value in (1, 5, 5)]
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(self.module.subprocess, "Popen", return_value=process), \
                patch.object(self.module.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaisesRegex(self.module.PreparationError, "PREPARATION_CLEANUP_UNCONFIRMED"):
                self.module.run(["fixture"], cwd=ROOT, seconds=1)
        self.assertEqual(process.wait.call_args_list, [call(timeout=1), call(timeout=5), call(timeout=5)])
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_wait_cancellation_reaps_and_repeated_signal_cannot_interrupt_cleanup(self):
        from unittest.mock import Mock, call
        process = Mock(pid=12345, returncode=0)
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        attempts = 0
        def waited(**kwargs):
            nonlocal attempts
            attempts += 1
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            return 0
        process.wait.side_effect = waited
        with patch.object(self.module.subprocess, "Popen", return_value=process), \
                patch.object(self.module.os, "killpg"):
            with self.assertRaisesRegex(self.module.PreparationError, "PREPARATION_INTERRUPTED"):
                self.module.run(["fixture"], cwd=ROOT, seconds=1)
        self.assertEqual(attempts, 3)
        self.assertEqual(process.wait.call_args_list, [call(timeout=1), call(timeout=5), call(timeout=5)])
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_success_time_cancellation_stops_the_next_command(self):
        from unittest.mock import Mock
        process = Mock(pid=12345, returncode=0)
        original_signal = signal.signal
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        def change(signum, handler):
            if signum == signal.SIGINT and handler == before[signum]:
                signal.getsignal(signum)(signum, None)
            return original_signal(signum, handler)
        next_command = Mock()
        with patch.object(self.module.subprocess, "Popen", return_value=process), \
                patch.object(self.module.os, "killpg") as kill, \
                patch.object(self.module.signal, "signal", side_effect=change):
            with self.assertRaisesRegex(self.module.PreparationError, "PREPARATION_INTERRUPTED"):
                self.module.run(["fixture"], cwd=ROOT)
                next_command()
        next_command.assert_not_called()
        kill.assert_not_called()  # The successful child had already been reaped.
        self.assertEqual(process.wait.call_count, 1)
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_one_restore_failure_does_not_skip_other_handler(self):
        from unittest.mock import Mock
        process = Mock(pid=12345, returncode=0)
        original_signal = signal.signal
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        restored = []
        def change(signum, handler):
            if handler == before[signum]:
                restored.append(signum)
                if signum == signal.SIGINT:
                    raise OSError("fixture restore")
            return original_signal(signum, handler)
        try:
            with patch.object(self.module.subprocess, "Popen", return_value=process), \
                    patch.object(self.module.signal, "signal", side_effect=change):
                with self.assertRaisesRegex(self.module.PreparationError, "PREPARATION_HANDLER_RESTORE_FAILED"):
                    self.module.run(["fixture"], cwd=ROOT)
            self.assertEqual(restored, [signal.SIGINT, signal.SIGTERM])
        finally:
            for signum, handler in before.items():
                original_signal(signum, handler)
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
