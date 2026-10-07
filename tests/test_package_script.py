"""scripts/package_ha.sh: the manual-install zip holds the files git tracks under the integration, nothing else."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "package_ha.sh"
# Absolute paths: the commands below are fixed, only their arguments vary (all of them written by these tests).
SH = shutil.which("sh") or "/bin/sh"
GIT = shutil.which("git") or "/usr/bin/git"

pytestmark = pytest.mark.skipif(
    not (shutil.which("rsync") and shutil.which("zip")),
    reason="needs rsync and zip, as the script does",
)


def git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603  # fixed command, arguments written here
        [GIT, "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_the_zip_holds_the_tracked_files_only(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    package = tmp_path / "custom_components" / "junghome_ble"
    (package / "jhmesh").mkdir(parents=True)
    (package / "manifest.json").write_text("{}\n", encoding="utf-8")
    (package / "jhmesh" / "client.py").write_text("x = 1\n", encoding="utf-8")
    (package / "removed.py").write_text("gone\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    # tracked and deleted in the working tree: left out, not an error; tracked and changed: as in the working tree
    (package / "removed.py").unlink()
    (package / "jhmesh" / "client.py").write_text("x = 2\n", encoding="utf-8")
    # what never goes into the zip: an export saved next to the code, bytecode, a file nobody added
    (package / "MeshNetwork.json").write_text("{}\n", encoding="utf-8")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (package / "notes.txt").write_text("x\n", encoding="utf-8")

    subprocess.run(  # noqa: S603  # fixed command, arguments written here
        [SH, str(tmp_path / "scripts" / SCRIPT.name)], check=True, capture_output=True
    )

    with zipfile.ZipFile(tmp_path / "dist" / "junghome_ble.zip") as zipped:
        files = sorted(n for n in zipped.namelist() if not n.endswith("/"))
        assert files == ["junghome_ble/jhmesh/client.py", "junghome_ble/manifest.json"]
        assert zipped.read("junghome_ble/jhmesh/client.py") == b"x = 2\n"
