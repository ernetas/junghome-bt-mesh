"""The CI jobs, runnable locally: `nox -l` lists the sessions, `nox -s tests` runs one.

One session per job of `.github/workflows/ci.yml`, installing from the same pins and running the same commands:

    lint                  ruff check, ruff format --check, the privacy scan; actionlint (with shellcheck) and zizmor
                          from the images and digests ci.yml names, when Docker is there
    types                 mypy on everything (the `tests` job's typing step)
    tests                 the whole suite with the coverage gates (the `tests` job)
    tests-library-<X.Y>   the built wheel on the oldest Python `requires-python` admits: mypy and the library tests
                          (the `library` job)
    build                 sdist and wheel, `twine check --strict` (the `library` job's build steps)
    floor                 imports the integration on the Home Assistant release hacs.json names (the `floor` job)
    package               the manual-install zip and its check (the `package` job)

and two that write files rather than check them:

    regen-fixtures        reruns the synthetic fixtures' generators (tests/test_fixtures_regen.py checks the result)
    snapshots             updates the syrupy snapshots and the tools' --help files; review the diff before committing

The `validate` job (hassfest, HACS) has no session: both run as GitHub actions only.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
import urllib.request
import zipfile
from pathlib import Path

import nox

ROOT = Path(__file__).resolve().parent
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PYTHON = "3.14"  # what every ci.yml job but `library` sets up


def _group(pattern: str, text: str) -> str:
    """Return the first group of `pattern`'s first match in `text`; no match is an error."""
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise ValueError(f"no match for {pattern!r}")
    return match[1]


# read from pyproject.toml like ci.yml `library` does, which also allows a plain `>=X.Y` bound only
OLDEST = _group(
    r"^>=\s*(\d+\.\d+)$",
    tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "requires-python"
    ].strip(),
)
# the order tests/test_fixtures_regen.py runs them in: the derived exports read MeshNetwork.json
FIXTURE_GENERATORS = (
    "make_fixture.py",
    "make_blinds_fixture.py",
    "make_export_fixture.py",
)

nox.options.default_venv_backend = "venv"
nox.options.reuse_venv = "yes"
nox.options.sessions = [
    "lint",
    "types",
    "tests",
    f"tests-library-{OLDEST}",
    "build",
    "floor",
    "package",
]


def _image(name: str) -> str:
    """Return ci.yml's `docker://` image `name`, with its tag and digest."""
    return _group(rf"docker://({re.escape(name)}:\S+)", CI)


def _install_test_stack(session: nox.Session) -> None:
    """requirements-test.txt, then the `usb` integration's requirements read from the installed Home Assistant."""
    session.install("-r", "requirements-test.txt")
    usb = session.run(
        "python",
        "-c",
        "import json, pathlib, homeassistant; "
        "p = pathlib.Path(homeassistant.__file__).parent / 'components' / 'usb' / 'manifest.json'; "
        "print(' '.join(json.loads(p.read_text())['requirements']))",
        silent=True,
    )
    session.install(*str(usb).split())


@nox.session(python=PYTHON)
def lint(session: nox.Session) -> None:
    """Run the `lint` job: ruff, the privacy scan, the workflow and shell linters."""
    session.install("-r", "requirements-lint.txt")
    session.run("python", "-m", "ruff", "check", ".")
    session.run("python", "-m", "ruff", "format", "--check", ".")
    session.run("python", "tools/privacy_scan.py")
    if shutil.which("docker") is None:
        session.warn(
            "no docker: actionlint, shellcheck and zizmor skipped (CI runs them)"
        )
        return
    docker = (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{ROOT}:/repo:ro",
        "-w",
        "/repo",
    )
    actionlint = _image("rhysd/actionlint")
    session.run(*docker, actionlint, "-color", external=True)
    session.run(
        *docker,
        "--entrypoint",
        "shellcheck",
        actionlint,
        "scripts/release_checks.sh",
        "scripts/package_ha.sh",
        external=True,
    )
    session.run(
        *docker, _image("ghcr.io/zizmorcore/zizmor"), "--offline", ".", external=True
    )


@nox.session(python=PYTHON)
def types(session: nox.Session) -> None:
    """Run the `tests` job's strict typing."""
    _install_test_stack(session)
    session.run("python", "-m", "mypy")


@nox.session(python=PYTHON)
def tests(session: nox.Session) -> None:
    """Run the `tests` job: the suite, its coverage total and every line of the integration."""
    _install_test_stack(session)
    session.run(
        "python",
        "-m",
        "pytest",
        "tests",
        "-q",
        "-n",
        "auto",
        "--durations=20",
        "--cov",
        "--cov-report=term-missing",
    )
    # ci.yml "Every line of the integration and the package is covered"
    report = Path(session.create_tmp()) / "coverage-integration.json"
    session.run(
        "python",
        "-m",
        "coverage",
        "json",
        "--include=custom_components/junghome_ble/*",
        "-o",
        str(report),
        "-q",
    )
    missing = {
        name: f["missing_lines"]
        for name, f in json.loads(report.read_text())["files"].items()
        if f["missing_lines"]
    }
    if missing:
        session.error(f"lines not covered: {missing}")


def _build(session: nox.Session) -> Path:
    """Build the sdist and the wheel into a clean dist/ and check them like the `library` job; return the wheel."""
    shutil.rmtree(ROOT / "dist", ignore_errors=True)
    session.install("-r", "requirements-build.txt")
    session.run("python", "-m", "build")
    session.run(
        "python",
        "-m",
        "twine",
        "check",
        "--strict",
        *map(str, (ROOT / "dist").glob("jhmesh-*")),
    )
    return next((ROOT / "dist").glob("jhmesh-*.whl"))


@nox.session(python=OLDEST)
def build(session: nox.Session) -> None:
    """Run the `library` job's build and `twine check --strict`."""
    _build(session)


@nox.session(python=OLDEST, name=f"tests-library-{OLDEST}")
def tests_library(session: nox.Session) -> None:
    """Run the `library` job: the wheel with the test runner the `tests` job pins, typed and tested alone."""
    wheel = _build(session)
    text = (ROOT / "requirements-test.txt").read_text(encoding="utf-8")
    version = _group(r"^pytest-homeassistant-custom-component==(\S+)$", text)
    url = f"https://pypi.org/pypi/pytest-homeassistant-custom-component/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as reply:
        requires = json.load(reply)["info"]["requires_dist"]
    wanted = {"pytest", "pytest-asyncio", "pytest-timeout", "pytest-cov", "coverage"}
    pins = [r for r in requires if r.partition("==")[0] in wanted]
    own = [
        line
        for line in text.splitlines()
        if line.startswith(("mypy==", "hypothesis=="))
    ]
    session.install(str(wheel), *own, *pins)
    env = {
        "PYTHONSAFEPATH": "1"
    }  # the checkout's `jhmesh` symlink must not shadow the installed wheel
    session.run(
        "python", "-m", "mypy", "--python-version", OLDEST, "-p", "jhmesh", env=env
    )
    package = session.run(
        "python",
        "-c",
        "import jhmesh, pathlib; print(pathlib.Path(jhmesh.__file__).parent)",
        env=env,
        silent=True,
    )
    session.run(
        "python",
        "-m",
        "pytest",
        "tests/jhmesh",
        "--confcutdir=tests/jhmesh",
        "--import-mode=importlib",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--cov={str(package).strip()}",
        "--cov-report=term-missing:skip-covered",
        "--cov-fail-under=100",
        env=env,
    )


@nox.session(python=PYTHON)
def floor(session: nox.Session) -> None:
    """Run the `floor` job: import the integration on the Home Assistant release hacs.json names."""
    version = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))[
        "homeassistant"
    ]
    session.install(f"homeassistant=={version}")
    requirements = session.run(
        "python",
        "-c",
        "import json, pathlib, homeassistant\n"
        "components = pathlib.Path(homeassistant.__file__).parent / 'components'\n"
        "todo, seen, reqs = ['bluetooth'], set(), set()\n"
        "while todo:\n"
        "    if (domain := todo.pop()) in seen:\n"
        "        continue\n"
        "    seen.add(domain)\n"
        "    manifest = json.loads((components / domain / 'manifest.json').read_text())\n"
        "    reqs |= set(manifest.get('requirements', []))\n"
        "    todo += manifest.get('dependencies', []) + manifest.get('after_dependencies', [])\n"
        "print(' '.join(sorted(reqs)))",
        silent=True,
    )
    session.install(*str(requirements).split())
    session.run(
        "python",
        "-c",
        "import importlib\n"
        "from custom_components.junghome_ble.const import PLATFORMS\n"
        "for mod in (*PLATFORMS, 'config_flow', 'diagnostics', 'device_trigger', 'logbook'):\n"
        "    importlib.import_module(f'custom_components.junghome_ble.{mod}')\n"
        "    print('imported', mod)",
        env={"PYTHONPATH": str(ROOT)},
    )


@nox.session(python=False)
def package(session: nox.Session) -> None:
    """Run the `package` job: build the zip and check it (a manifest at `junghome_ble/`, no bytecode)."""
    session.run("./scripts/package_ha.sh", external=True)
    names = zipfile.ZipFile(ROOT / "dist" / "junghome_ble.zip").namelist()
    if "junghome_ble/manifest.json" not in names:
        session.error("dist/junghome_ble.zip has no junghome_ble/manifest.json")
    if any("__pycache__" in name for name in names):
        session.error("dist/junghome_ble.zip carries __pycache__")
    session.log(f"{len(names)} entries")


@nox.session(python=PYTHON, name="regen-fixtures")
def regen_fixtures(session: nox.Session) -> None:
    """Rerun the synthetic fixtures' generators in place."""
    with session.chdir(ROOT / "tests" / "fixtures"):
        for generator in FIXTURE_GENERATORS:
            session.run("python", "-B", generator)


@nox.session(python=PYTHON)
def snapshots(session: nox.Session) -> None:
    """Update the syrupy snapshots, the tools' --help files and the entity reference."""
    _install_test_stack(session)
    session.run(
        "python",
        "-m",
        "pytest",
        "tests/test_snapshots.py",
        "tests/test_traces.py",
        "-q",
        "--snapshot-update",
    )
    session.run("python", "-m", "tests.test_cli_help")
    session.run("python", "tools/gen_entity_reference.py")
    session.log(
        "review `git diff tests/snapshots tests/cli_help docs/user/entities.md` before committing"
    )
