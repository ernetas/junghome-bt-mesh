"""The synthetic fixtures regenerate byte for byte from their generators (review-4 Q4-12).

`tests/fixtures/make_*.py` are the source of every export, metadata file and derived network under
`tests/fixtures/`; a fixture edited by hand, or a generator changed without rerunning it, would leave the two
disagreeing until the next regeneration silently changed what the suite tests. The generators run in a copy, in
the order their docstrings ask for (the derived exports read `MeshNetwork.json`), and the copy must hold exactly
the committed files.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
# the base networks first: the blinds network and the share exports are built from `MeshNetwork.json`
GENERATORS = ("make_fixture.py", "make_blinds_fixture.py", "make_export_fixture.py")


def data_files(root: Path) -> dict[str, bytes]:
    """Every fixture under `root` but the generators themselves, by path relative to it."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }


def test_every_fixture_regenerates_byte_for_byte(tmp_path: Path) -> None:
    for name in GENERATORS:
        shutil.copy(FIXTURES / name, tmp_path / name)
    for name in GENERATORS:
        subprocess.run(  # noqa: S603  # our own generator, run by the interpreter running the tests
            [sys.executable, "-B", name],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            timeout=60,
        )
    committed, generated = data_files(FIXTURES), data_files(tmp_path)
    assert sorted(generated) == sorted(committed)
    differing = [name for name in committed if generated[name] != committed[name]]
    assert not differing, f"regenerate with tests/fixtures/make_*.py: {differing}"
