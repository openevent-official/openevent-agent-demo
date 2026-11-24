"""Build the Demo wheel and optionally install it into the selected Python."""

from configparser import RawConfigParser
from contextlib import contextmanager, nullcontext
from importlib import metadata
from importlib.util import cache_from_source
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from pip._internal.commands import create_command
from pip._internal.commands.install import decide_user_install
from pip._internal.configuration import Configuration
from pip._internal.locations import get_scheme
from pip._internal.utils.wheel import wheel_dist_info_dir


PROJECT = "openevent-agent-demo"
PIP = [sys.executable, "-B", "-m", "pip"]


def build_environment(work):
    env = os.environ.copy()
    redirects = {"python", "root", "target"}
    sources = {"index-url", "extra-index-url", "no-index", "find-links"}
    for name in redirects:
        env.pop("PIP_" + name.upper(), None)

    # pip installs build requirements with its own --no-user and --prefix.
    # Keep the install subprocess's transport settings, including trusted-host,
    # while using the wheel command's package sources for this build.
    settings = Configuration(isolated=False)
    settings.load()
    config = RawConfigParser()
    changed = False
    for key, value in settings.items():
        section, name = key.split(".", 1)
        if section == ":env:":
            continue
        if not config.has_section(section):
            config.add_section(section)
        if value and (name in redirects or (section == "install" and name in sources)):
            value = ""
            changed = True
        elif value and section == "wheel" and name in sources:
            variable = "PIP_" + name.upper().replace("-", "_")
            env[variable] = env.get(variable) or value
        config.set(section, name, value)
    if changed:
        path = work / "pip.conf"
        with path.open("w", encoding="utf-8") as stream:
            config.write(stream)
        env["PIP_CONFIG_FILE"] = str(path.resolve())
    return env


def build_wheel():
    tmp = Path(os.environ["TMPDIR"])
    tmp.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=tmp) as directory:
        work = Path(directory)
        source = work / "source"
        source.mkdir()
        for filename in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy(filename, source)
        for package in ("chat_model_agent", "runtime_reconciler"):
            shutil.copytree(package, source / package)

        output = work / "wheels"
        subprocess.run([*PIP, "wheel", "--no-deps", "--wheel-dir", output, source],
                       env=build_environment(work), check=True)
        wheel, = output.glob("*.whl")
        Path("dist").mkdir(exist_ok=True)
        release = Path("dist") / wheel.name
        shutil.move(wheel, release)
    print(f"Built {release}", flush=True)
    return release


def install_options():
    parser = create_command("install").parser
    options, _ = parser.parse_args([])
    for flag in ("--python", "--root"):
        if getattr(options, parser.get_option(flag).dest):
            sys.exit(f"Remove pip's {flag} setting to install into the Python selected by Make.")
    return options


@contextmanager
def replace_user_files(wheel):
    # In a venv with a visible user site, pip writes there but skips uninstalling
    # its previous files because the user site lies outside the venv.
    scheme = get_scheme(PROJECT, user=True)
    previous = next(metadata.distributions(name=PROJECT, path=[scheme.purelib]), None)
    if previous is None:
        yield
        return
    if previous.files is None:
        sys.exit(f"Cannot replace {PROJECT} in {scheme.purelib}: installed file list is missing")
    old = {Path(entry.locate()) for entry in previous.files}
    yield
    with ZipFile(wheel) as archive:
        info = Path(scheme.purelib) / wheel_dist_info_dir(archive, PROJECT)
    current = metadata.Distribution.at(info)
    obsolete = old.difference(Path(entry.locate()) for entry in current.files)
    # Discard bytecode for replaced sources even when their size and timestamp match.
    obsolete.update(Path(cache_from_source(str(path))) for path in old if path.suffix == ".py")
    user_base = Path(scheme.data).resolve()
    for path in obsolete:
        parent = path.parent.resolve()
        if parent.is_relative_to(user_base):
            (parent / path.name).unlink(missing_ok=True)
            while parent != user_base and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent


def main(mode):
    options = install_options() if mode == "install" else None
    wheel = build_wheel()
    if options is not None:
        cleanup = nullcontext()
        if (sys.prefix != sys.base_prefix and site.ENABLE_USER_SITE
                and decide_user_install(options.use_user_site)):
            cleanup = replace_user_files(wheel)
        with cleanup:
            pip_install = [*PIP, "install", "--no-compile"]
            subprocess.run([*pip_install, wheel], check=True)
            subprocess.run([*pip_install, "--force-reinstall", "--no-deps", wheel], check=True)


if __name__ == "__main__":
    main(sys.argv[1])
