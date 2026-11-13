from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "openevent_agent_demo"


def wheel(directory, name, version, value, requirements=()):
    """Write a tiny valid wheel so installation tests need no build backend."""
    directory.mkdir(parents=True, exist_ok=True)
    dist_info = f"{name}-{version}.dist-info"
    contents = {
        f"{name}/__init__.py": f"VALUE = {value!r}\n",
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements)
            + "\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: installation-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = f"{dist_info}/RECORD"
    contents[record] = "".join(f"{path},,\n" for path in (*contents, record))
    path = directory / f"{name}-{version}-py3-none-any.whl"
    with ZipFile(path, "w") as archive:
        for member, data in contents.items():
            archive.writestr(member, data)
    return path


class InstallTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / "build/tests/install"
        build.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=build)
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        (self.work / "build/tmp").mkdir(parents=True)
        self.wheelhouse = self.work / "wheels"
        self.wheelhouse.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("PIP_") and key not in {"PYTHONPATH", "PYTHONHOME"}
        }
        self.env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": str(self.wheelhouse),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_COMPILE": "1",
            "TMPDIR": str(self.work / "build/tmp"),
        })
        self.run_command(sys.executable, "-B", "-m", "venv", str(self.work / "venv"))
        self.python = str(self.work / "venv/bin/python")
        old = wheel(self.work / "old", PROJECT, "0.2.0", "old")
        self.run_command(self.python, "-B", "-m", "pip", "install", "--no-deps", str(old))

    def run_command(self, *command, success=True):
        result = subprocess.run(command, cwd=self.work, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def install(self, current, *, success=True):
        # Exercise the real install recipe with our offline fixture wheel. The
        # build prerequisite is skipped because it needs the actual app packages.
        return self.run_command(
            "make", "--no-print-directory", "-f", str(ROOT / "Makefile"),
            "-o", "build", "install", f"PYTHON={self.python}", f"WHEEL={current}",
            success=success,
        )

    def value(self, module):
        return self.run_command(self.python, "-B", "-c",
                                f"import {module}; print({module}.VALUE)").stdout.strip()

    def test_dependency_resolution_failure_preserves_installed_project(self):
        current = wheel(self.work / "current", PROJECT, "0.2.0", "new",
                        ("missing_install_test_dependency>=1",))
        result = self.install(current, success=False)
        self.assertIn("No matching distribution found", result.stdout)
        self.assertEqual(self.value(PROJECT), "old")

    def test_same_version_replacement_reuses_satisfied_dependencies(self):
        dependency = wheel(self.wheelhouse, "installed_dependency_probe", "1.0", "original")
        self.run_command(self.python, "-B", "-m", "pip", "install", str(dependency))
        # A reinstall would overwrite this recorded file; an upgrade would load
        # the newer wheel. Neither should happen for an already satisfied dep.
        self.run_command(self.python, "-B", "-c", "from pathlib import Path; "
                         "import installed_dependency_probe as dep; "
                         """Path(dep.__file__).write_text("VALUE = 'kept'\\n")""")
        wheel(self.wheelhouse, "installed_dependency_probe", "2.0", "upgraded")
        wheel(self.wheelhouse, "missing_dependency_probe", "1.0", "installed")
        current = wheel(self.work / "current", PROJECT, "0.2.0", "new", (
            "installed_dependency_probe>=1", "missing_dependency_probe>=1",
        ))
        self.install(current)
        self.assertEqual(self.value(PROJECT), "new")
        self.assertEqual(self.value("installed_dependency_probe"), "kept")
        self.assertEqual(self.value("missing_dependency_probe"), "installed")


if __name__ == "__main__":
    unittest.main()
