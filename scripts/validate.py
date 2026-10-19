"""Native offline source gate; preparation is a separate explicit action."""
import sys
if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
    print('{"exit_code":2,"finding":"ISOLATED_RUNTIME_REQUIRED"}')
    raise SystemExit(2)
import importlib.util
import json
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).absolute().parent.parent


def main():
    if sys.version_info[:3] != (3, 13, 16):
        print('{"exit_code":2,"finding":"PROJECT_PYTHON_PIN"}')
        return 2
    spec = importlib.util.spec_from_file_location("profile_interface", ROOT / "scripts/check-project-manifest.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    status = adapter.main(sys.argv[1:])
    if status:
        return status
    try:
        config = tomllib.loads(adapter.capture(ROOT / "mise.toml").decode())
        assert config["tools"] == {"python": "3.13.16"}
        assert all(config["settings"][key] is False for key in ("auto_install", "exec_auto_install"))
        assert config["settings"]["task"]["run_auto_install"] is False
        assert config["tasks"]["check"]["run"] == "python -I -S -B scripts/validate.py"
        assert config["tasks"]["test"]["depends"] == ["check"]
        adapter.capture(ROOT / "profile/README.md")
    except (AssertionError, OSError, ValueError, KeyError, adapter.Refusal):
        print('{"exit_code":1,"finding":"NATIVE_SOURCE_CONTRACT"}')
        return 1
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({"scope": "github-profile-source-check", "exit_code": 0 if result.wasSuccessful() else 1,
                      "tests_run": result.testsRun, "source_conformance": "worktree_only",
                      "producer_admission": "not_checked", "runtime_acceptance": "not_checked"}, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
