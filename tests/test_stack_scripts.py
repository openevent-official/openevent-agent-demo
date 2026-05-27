from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


class StackScriptTests(unittest.TestCase):
    def test_view_is_required_stack_component(self):
        repo_root = Path(__file__).resolve().parents[1]
        stack = yaml.safe_load((repo_root / "openevent-stack/stack.yaml").read_text(encoding="utf-8"))

        self.assertIn("view", stack)
        self.assertNotIn("enabled", stack["view"])
        self.assertNotIn("host", stack["view"])
        self.assertIsInstance(stack["view"]["port"], int)

    def test_render_view_config_fixes_host(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = (repo_root / "openevent-stack/render-view-config.sh").read_text(encoding="utf-8")

        self.assertIn('"host": "0.0.0.0"', script)
        self.assertNotIn('view.get("host"', script)

    def test_stack_scripts_use_config_directory(self):
        repo_root = Path(__file__).resolve().parents[1]
        common = (repo_root / "openevent-stack/common.sh").read_text(encoding="utf-8")
        process = (repo_root / "openevent-stack/process.sh").read_text(encoding="utf-8")

        self.assertIn('CONFIG_DIR="$STACK_DIR/config"', common)
        self.assertIn('"$CONFIG_DIR/env.sh"', common)
        self.assertIn('"$CONFIG_DIR/openevent-server.yaml"', process)
        self.assertIn('"$CONFIG_DIR/openevent-view.yaml"', process)

    def test_start_scripts_print_process_result(self):
        repo_root = Path(__file__).resolve().parents[1]
        common = (repo_root / "openevent-stack/common.sh").read_text(encoding="utf-8")
        bootstrap = (repo_root / "openevent-stack/bootstrap.sh").read_text(encoding="utf-8")
        start = (repo_root / "openevent-stack/start.sh").read_text(encoding="utf-8")

        self.assertIn("process start result", common)
        self.assertIn("print_start_result", bootstrap)
        self.assertIn("print_start_result", start)

    def test_process_manager_detaches_and_cleans_stale_processes(self):
        repo_root = Path(__file__).resolve().parents[1]
        process = (repo_root / "openevent-stack/process.sh").read_text(encoding="utf-8")

        self.assertIn("matching_pids()", process)
        self.assertIn("DUPLICATE pids", process)
        self.assertIn("ORPHANED pids", process)
        self.assertIn("cleaned stale pid", process)
        self.assertIn("exec setsid nohup", process)
        self.assertIn("</dev/null", process)

    def test_view_does_not_start_without_openevent(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            stack_dir = Path(temp) / "openevent-stack"
            shutil.copytree(repo_root / "openevent-stack", stack_dir)
            for name in ("config", "data", "logs", "run"):
                shutil.rmtree(stack_dir / name, ignore_errors=True)

            result = subprocess.run(
                [str(stack_dir / "process.sh"), "start", "openevent-view"],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("openevent-view depends on openevent", result.stderr)


if __name__ == "__main__":
    unittest.main()
