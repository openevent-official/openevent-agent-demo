"""Build selected source checkouts into the dedicated dependency test venv."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = (
    ("sdk", "openevent-sdk", "openevent.sdk"),
    ("chat", "openevent-modules-chat", "openevent.chat_sdk"),
    ("model-proxy", "openevent-model-proxy", "openevent.model_proxy_sdk"),
    ("cmd", "openevent-modules-cmd", "openevent.cmd_sdk"),
    ("view", "openevent-view", "openevent.view"),
)


def source_directory(value):
    if not value:
        raise argparse.ArgumentTypeError("an explicit source directory is required")
    path = Path(value).expanduser().resolve()
    if not (path / "Makefile").is_file() or not (path / "pyproject.toml").is_file():
        raise argparse.ArgumentTypeError(f"not a component source checkout: {path}")
    return path


def prepare(sources):
    temporary = ROOT / "build" / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONPYCACHEPREFIX", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
        environment.pop(name, None)
    environment.update(PYTHONDONTWRITEBYTECODE="1", PIP_NO_COMPILE="1",
                       PIP_CACHE_DIR=str(ROOT / "build" / "pip-cache"), TMPDIR=str(temporary))

    def run(*command):
        subprocess.run(command, cwd=ROOT, env=environment, check=True)

    destination = ROOT / "build" / "dependency-test-env"
    run(sys.executable, "-I", "-B", "-m", "venv", str(destination))
    python = str(destination / "bin" / "python")
    # These are this project's declared build tools; component make targets own
    # their own build tools and all runtime dependency resolution.
    run(python, "-I", "-B", "-m", "pip", "install", "--no-compile", "setuptools>=68", "wheel")
    for name, _, _ in COMPONENTS:
        source = sources[name]
        print(f"Building {name} from {source}", flush=True)
        run("make", "--no-print-directory", "-C", str(source), "install", f"PYTHON={python}")
    run("make", "--no-print-directory", "-C", str(ROOT), "install", f"PYTHON={python}")
    # -I excludes PYTHONPATH and the current directory. Check module origins as
    # well as versions, so a checkout cannot stand in for the built package.
    modules = [(package, module) for _, package, module in COMPONENTS]
    modules.append(("openevent-agent-demo", "chat_model_agent"))
    check = """
from importlib import import_module, metadata
from pathlib import Path
import sys
for package, module_name in %r:
    distribution = metadata.distribution(package)
    module = import_module(module_name)
    expected = Path(distribution.locate_file(module_name.replace('.', '/') + '/__init__.py')).resolve()
    actual = Path(module.__file__).resolve()
    if actual != expected or not actual.is_relative_to(Path(sys.prefix).resolve()):
        raise SystemExit(f'{module_name} did not load from the test environment: {actual}')
    print(f'{package} {distribution.version}: {actual}')
""" % modules
    run(python, "-I", "-B", "-c", check)
    print(f"Dependency test Python: {python}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, _, _ in COMPONENTS:
        parser.add_argument(f"--{name}", required=True, type=source_directory)
    args = parser.parse_args()
    prepare({name: getattr(args, name.replace("-", "_")) for name, _, _ in COMPONENTS})


if __name__ == "__main__":
    main()
