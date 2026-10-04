"""The tools' `--help`, every sub-command's included, pinned byte for byte in `tests/cli_help/<tool>.txt`.

The help is rendered for a fixed program name and width, without colour. After a deliberate change of an option
or its help text, regenerate the files with `python -m tests.test_cli_help` and review the diff.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tools import mesh_poc, mesh_report, mesh_sniff

GOLDEN = Path(__file__).with_name("cli_help")
TOOLS: dict[str, Callable[[], argparse.ArgumentParser]] = {
    "mesh_poc": mesh_poc.build_parser,
    "mesh_sniff": mesh_sniff.build_parser,
    "mesh_report": mesh_report.build_parser,
}
ENVIRONMENT = {"COLUMNS": "100", "NO_COLOR": "1", "PYTHON_COLORS": "0"}


def _parsers(
    parser: argparse.ArgumentParser,
) -> Iterator[argparse.ArgumentParser]:
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _parsers(sub)


def help_text(tool: str) -> str:
    """Every parser's help of `tool`, top-level first, under the name `<tool>.py`; set ENVIRONMENT first."""
    saved = sys.argv, getattr(argparse, "_prog_name", None)
    sys.argv = [f"{tool}.py"]
    if (
        saved[1] is not None
    ):  # 3.14 names a `python -m` run after the module, not after argv[0]
        argparse._prog_name = lambda prog=None: prog or f"{tool}.py"  # type: ignore[attr-defined]
    try:
        return "".join(
            f"===== {p.prog}\n{p.format_help()}" for p in _parsers(TOOLS[tool]())
        )
    finally:
        sys.argv = saved[0]
        if saved[1] is not None:
            argparse._prog_name = saved[1]  # type: ignore[attr-defined]


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_help_matches_the_golden_text(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert help_text(tool) == (GOLDEN / f"{tool}.txt").read_text(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    os.environ.update(ENVIRONMENT)
    os.environ.pop("FORCE_COLOR", None)
    for name in TOOLS:
        (GOLDEN / f"{name}.txt").write_text(help_text(name), encoding="utf-8")
