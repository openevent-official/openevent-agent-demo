from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    "OPENEVENT": "openevent",
    "OPENEVENT_MODULES_CHAT": "openevent-modules-chat",
    "OPENEVENT_MODEL_PROXY": "openevent-modules-model-proxy",
    "OPENEVENT_MODULES_CMD": "openevent-modules-cmd",
    "OPENEVENT_VIEW": "openevent-view",
    "OPENEVENT_SDK": "openevent-sdk",
}

# Exercise the real bootstrap shell with isolated command doubles. The git double
# runs the actual `submodule foreach` shell command, so dirty-file checks are not
# replaced with a canned success/failure. No Git commits or network are needed.
FAKE_TOOL = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ["BOOTSTRAP_TEST_STATE"])
state = json.loads(state_path.read_text())
with Path(os.environ["BOOTSTRAP_TEST_LOG"]).open("a") as log:
    log.write(json.dumps({"tool": name, "args": args}) + "\n")

def save():
    state_path.write_text(json.dumps(state))

def checkout(directory):
    if directory.exists() and any(directory.iterdir()):
        print("destination is not an empty directory", file=sys.stderr)
        sys.exit(1)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".git").mkdir(exist_ok=True)
    state["checkouts"][str(directory)] = {"head": "0" * 40}

if name == "git":
    directory = Path.cwd()
    if args[:1] == ["-C"]:
        directory = Path(args[1])
        args = args[2:]
    if args[0] == "clone":
        checkout(Path(args[-1]))
        state["checkouts"][args[-1]]["url"] = args[-2]
    elif args[0] == "remote":
        assert args[:3] == ["remote", "set-url", "origin"], args
        state["checkouts"][str(directory)]["url"] = args[-1]
    elif args[0] == "config":
        assert args[-2] == "submodule.openevent-sdk.url", args
        state["checkouts"][str(directory)]["sdk_url"] = args[-1]
    elif args[0] == "status":
        assert "--porcelain" in args, args
        if (directory / "local-change").exists():
            print(" M tracked-file")
        child = directory / "openevent-sdk"
        if ("--ignore-submodules=all" not in args
                and str(child) in state["checkouts"]
                and state["checkouts"][str(child)]["head"] != "0" * 40):
            print(" M openevent-sdk")
    elif args[0] == "fetch":
        assert "origin" in args, args
        if directory.name == os.environ.get("BOOTSTRAP_TEST_FAIL_FETCH"):
            print("simulated fetch failure", file=sys.stderr)
            sys.exit(1)
        ref = args[-1]
        sha = os.environ["BOOTSTRAP_TEST_HEAD"] if ref == "HEAD" else "3" * 40
        state["checkouts"][str(directory)]["fetched"] = sha
        state["checkouts"][str(directory)]["ref"] = ref
    elif args[0] == "checkout":
        assert args == ["checkout", "--detach", "FETCH_HEAD"], args
        state["checkouts"][str(directory)]["head"] = state["checkouts"][str(directory)]["fetched"]
    elif args[0] == "rev-parse":
        if args[-1] == "HEAD":
            print(state["checkouts"][str(directory)]["head"])
        elif args[-1] == "--is-inside-work-tree":
            print("true")
        else:
            raise AssertionError(args)
    elif args[:2] == ["submodule", "init"]:
        child = directory / "openevent-sdk"
        child.mkdir(parents=True, exist_ok=True)
    elif args[:2] == ["submodule", "foreach"]:
        child = directory / "openevent-sdk"
        if (child / ".git").exists():
            command = " ".join(arg for arg in args[2:] if arg not in {"--recursive", "--quiet", "--"})
            result = subprocess.run(["bash", "-c", command], cwd=child,
                                    env={**os.environ, "name": child.name, "sm_path": child.name})
            if result.returncode:
                sys.exit(result.returncode)
    else:
        raise AssertionError(args)
    save()
elif name == "python":
    if args[:2] == ["-m", "venv"]:
        target = Path(args[2]) / "bin/python"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.symlink_to(Path(__file__).resolve())
elif name == "cmake":
    print("cmake version 3.25.0")
elif name == "make":
    directory = Path(args[args.index("-C") + 1])
    if args[-1] == "build":
        binary = directory / "build/openevent_server"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    elif args[-1] == "install":
        state["installs"].append({"project": directory.name,
                                  "head": state["checkouts"].get(str(directory), {}).get("head")})
        save()
    else:
        raise AssertionError(args)
elif name not in {"protoc", "grpc_cpp_plugin"}:
    raise AssertionError(name)
'''


class BootstrapUbuntuTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / "build/bootstrap-tests"
        build.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=build)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.project = self.directory / "demo"
        scripts = self.project / "scripts"
        scripts.mkdir(parents=True)
        self.script = scripts / "bootstrap_ubuntu_venv.sh"
        shutil.copyfile(ROOT / "scripts/bootstrap_ubuntu_venv.sh", self.script)
        self.workdir = self.directory / "runtime"
        self.state_path = self.directory / "state.json"
        self.state_path.write_text(json.dumps({"checkouts": {}, "installs": []}))
        self.log_path = self.directory / "commands.jsonl"
        self.log_path.touch()
        commands = self.directory / "bin"
        commands.mkdir()
        executable = commands / "tool"
        executable.write_text(f"#!{sys.executable} -B\n" + FAKE_TOOL)
        executable.chmod(0o755)
        for name in ("git", "python", "make", "cmake", "protoc", "grpc_cpp_plugin"):
            (commands / name).symlink_to(executable)
        self.environment = {
            key: value for key, value in os.environ.items()
            if key != "PYTHON_BIN" and not (key.startswith("OPENEVENT_") and key.endswith(("_REF", "_URL")))
        }
        self.environment.update({
            "PATH": f"{commands}:{os.environ['PATH']}",
            "PYTHON_BIN": str(commands / "python"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "BOOTSTRAP_TEST_STATE": str(self.state_path),
            "BOOTSTRAP_TEST_LOG": str(self.log_path),
            "BOOTSTRAP_TEST_HEAD": "1" * 40,
        })

    def run_bootstrap(self, **overrides):
        return subprocess.run(
            ["bash", str(self.script), "--skip-apt", "--workdir", str(self.workdir)],
            cwd=self.project, env={**self.environment, **overrides},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    def state(self):
        return json.loads(self.state_path.read_text())

    def checkout(self, component):
        if component == "openevent-sdk":
            return self.workdir / "sources/openevent/openevent-sdk"
        return self.workdir / "sources" / component

    def reset_observations(self):
        state = self.state()
        state["installs"] = []
        self.state_path.write_text(json.dumps(state))
        self.log_path.write_text("")

    def test_default_install_fetches_latest_for_every_component_including_sdk(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout)
        state = self.state()
        for component in COMPONENTS.values():
            self.assertEqual(state["checkouts"][str(self.checkout(component))]["ref"], "HEAD")
            self.assertEqual(state["checkouts"][str(self.checkout(component))]["head"], "1" * 40)
        self.assertTrue((self.checkout("openevent-sdk") / ".git").is_dir())
        self.assertTrue((self.workdir / "sources").is_dir())
        self.assertFalse((self.workdir / "src").exists())
        self.assertEqual([item["project"] for item in state["installs"]],
                         ["openevent-sdk", "openevent-modules-chat", "openevent-modules-model-proxy",
                          "openevent-modules-cmd", "openevent-view", "demo"])
        self.assertTrue(all(item["head"] == "1" * 40 for item in state["installs"][:-1]))
        stack = self.project / "openevent-stack"
        self.assertEqual({path.name for path in stack.iterdir()}, {"runtime"})
        generated_env = stack / "runtime/config/env.sh"
        loaded = subprocess.run(
            ["bash", "-c", 'source "$1"; printf "%s\\n" "$PYTHON_BIN" "$OPENEVENT_SERVER_BIN"',
             "test", str(generated_env)],
            env={key: value for key, value in os.environ.items()
                 if key not in {"PYTHON_BIN", "OPENEVENT_SERVER_BIN"}},
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(loaded.stdout.splitlines(), [
            str(self.workdir / "venv/bin/python"),
            str(self.checkout("openevent") / "build/openevent_server"),
        ])

    def test_rerun_fetches_moved_remote_head_and_reuses_sdk_gitfile(self):
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stdout)
        # An SDK created by a previous submodule update has a gitfile, whereas a
        # direct clone has a .git directory. Both must be reusable.
        sdk_git = self.checkout("openevent-sdk") / ".git"
        sdk_git.rmdir()
        sdk_git.write_text("gitdir: ../.git/modules/openevent-sdk\n")
        self.reset_observations()
        second = self.run_bootstrap(BOOTSTRAP_TEST_HEAD="2" * 40)
        self.assertEqual(second.returncode, 0, second.stdout)
        state = self.state()
        self.assertTrue(all(item["head"] == "2" * 40 for item in state["checkouts"].values()))
        self.assertTrue(all(item["head"] == "2" * 40 for item in state["installs"][:-1]))

    def test_explicit_branch_tag_and_commit_are_fetch_targets(self):
        for ref in ("feature/current", "v1.2.3", "a" * 40):
            with self.subTest(ref=ref):
                result = self.run_bootstrap(**{f"{prefix}_REF": ref for prefix in COMPONENTS})
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertTrue(all(item["ref"] == ref for item in self.state()["checkouts"].values()))
                self.assertTrue(all(item["head"] == "3" * 40 for item in self.state()["checkouts"].values()))

    def test_sdk_url_override_is_used_for_initial_clone(self):
        sdk_url = "https://example.invalid/custom-sdk.git"
        result = self.run_bootstrap(OPENEVENT_SDK_URL=sdk_url)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.state()["checkouts"][str(self.checkout("openevent-sdk"))]["url"], sdk_url)
        commands = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        config = next(index for index, item in enumerate(commands)
                      if item["tool"] == "git" and "submodule.openevent-sdk.url" in item["args"])
        sdk_clone = next(index for index, item in enumerate(commands)
                         if item["tool"] == "git" and "clone" in item["args"]
                         and item["args"][-1] == str(self.checkout("openevent-sdk")))
        self.assertLess(config, sdk_clone)
        self.assertIn(sdk_url, commands[sdk_clone]["args"])

    def test_local_file_changes_stop_before_overwriting_or_installing(self):
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stdout)
        for component in ("openevent", "openevent-modules-chat", "openevent-sdk"):
            with self.subTest(component=component):
                self.reset_observations()
                marker = self.checkout(component) / "local-change"
                marker.write_text("keep this local edit")
                try:
                    result = self.run_bootstrap(BOOTSTRAP_TEST_HEAD="2" * 40)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("local changes", result.stdout)
                    self.assertEqual(marker.read_text(), "keep this local edit")
                    self.assertEqual(self.state()["installs"], [])
                    commands = [json.loads(line) for line in self.log_path.read_text().splitlines()]
                    touched = [item for item in commands if item["tool"] == "git"
                               and item["args"][:2] == ["-C", str(self.checkout(component))]
                               and item["args"][2:3] in (["fetch"], ["checkout"])]
                    self.assertEqual(touched, [])
                finally:
                    marker.unlink()

    def test_fetch_failure_does_not_install_existing_checkout(self):
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stdout)
        for component in ("openevent-modules-chat", "openevent-sdk"):
            with self.subTest(component=component):
                self.reset_observations()
                result = self.run_bootstrap(BOOTSTRAP_TEST_FAIL_FETCH=component,
                                            BOOTSTRAP_TEST_HEAD="2" * 40)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("simulated fetch failure", result.stdout)
                self.assertEqual(self.state()["installs"], [])

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "requires an existing Git checkout")
    def test_initial_fetch_failure_leaves_clean_checkout_for_rerun(self):
        # Use existing history and real clone/status/checkout behavior. Only the
        # first explicit fetch fails; the test creates no commits or network I/O.
        prefix = self.script.read_text().split("SCRIPT_DIR=", 1)[0]
        runner = self.directory / "clone-retry.sh"
        runner.write_text(prefix + '\nclone_or_update TEST "$1" HEAD "$2"\n')
        commands = self.directory / "retry-bin"
        commands.mkdir()
        git = commands / "git"
        git.write_text('''#!/usr/bin/env bash
if [ "${3:-}" = fetch ] && [ -e "$BOOTSTRAP_RETRY_FAIL_ONCE" ]; then
  rm "$BOOTSTRAP_RETRY_FAIL_ONCE"
  printf 'transient fetch unavailable\\n' >&2
  exit 128
fi
exec "$BOOTSTRAP_RETRY_REAL_GIT" "$@"
''')
        git.chmod(0o755)
        marker = self.directory / "fail-once"
        marker.touch()
        real_git = shutil.which("git")
        environment = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
                       "BOOTSTRAP_RETRY_FAIL_ONCE": str(marker), "BOOTSTRAP_RETRY_REAL_GIT": real_git}
        checkout = self.directory / "retry-checkout"
        command = ["bash", str(runner), str(ROOT), str(checkout)]
        first = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("transient fetch unavailable", first.stderr)
        status = subprocess.run([real_git, "-C", str(checkout), "status", "--porcelain", "--ignore-submodules=all"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(status.stdout, "")
        second = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("updating TEST checkout", second.stdout)

    def test_nonempty_non_git_directory_is_preserved(self):
        target = self.checkout("openevent-modules-chat")
        target.mkdir(parents=True)
        marker = target / "existing-file"
        marker.write_text("keep me")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(marker.read_text(), "keep me")
        self.assertEqual(self.state()["installs"], [])


if __name__ == "__main__":
    unittest.main()
