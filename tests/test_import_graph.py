"""tools/import_graph.py on a tiny package: the kinds of import, the edges they make, the cycles and the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import import_graph as G

PKG = G.PACKAGE


def write(root: Path, name: str, text: str) -> None:
    path = root / "custom_components" / "junghome_ble" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write(tmp_path, "__init__.py", "import os\nfrom .hub import a\n")
    write(
        tmp_path,
        "hub/__init__.py",
        "",
    )
    write(
        tmp_path,
        "hub/a.py",
        "import typing\nfrom typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from ..b import B\nelse:\n    from .. import c\n"
        "if typing.TYPE_CHECKING:\n    import custom_components.junghome_ble.c\n",
    )
    write(
        tmp_path,
        "b.py",
        "from .hub.a import thing\nimport jhmesh.d\n"
        "def f():\n    from . import c\n    if TYPE_CHECKING:\n        from .hub import a\n"
        "class K:\n    async def g(self):\n        import custom_components.junghome_ble.hub.a\n",
    )
    write(
        tmp_path,
        "c.py",
        "from custom_components.junghome_ble.jhmesh import d\nfrom . import missing\n",
    )
    write(tmp_path, "jhmesh/__init__.py", "")
    write(tmp_path, "jhmesh/d.py", "from . import d\n")
    return tmp_path


def test_edges_by_kind(root: Path) -> None:
    edges = {(G.short(e.source), G.short(e.target), e.kind) for e in G.edges(root)}
    assert edges == {
        ("__init__", "hub.a", "module"),
        ("hub.a", "b", "typing"),
        ("hub.a", "c", "module"),  # the `else` of TYPE_CHECKING runs
        ("hub.a", "c", "typing"),
        ("b", "hub.a", "module"),
        ("b", "jhmesh.d", "module"),  # the top-level `jhmesh` is the bundled package
        ("b", "c", "lazy"),
        (
            "b",
            "hub.a",
            "lazy",
        ),  # TYPE_CHECKING inside a function is still in it; so is a method's import
        ("c", "jhmesh.d", "module"),
        ("c", "__init__", "module"),  # `from . import missing`: the package itself
    }
    assert sorted(G.modules(root)) == [
        PKG,
        f"{PKG}.b",
        f"{PKG}.c",
        f"{PKG}.hub",
        f"{PKG}.hub.a",
        f"{PKG}.jhmesh",
        f"{PKG}.jhmesh.d",
    ]


def test_cycles(root: Path) -> None:
    edges = G.edges(root)
    assert G.cycles(G.graph_of(edges, ("module",))) == [
        [PKG, f"{PKG}.c", f"{PKG}.hub.a"]
    ]
    assert G.cycles(G.graph_of(edges, G.STATIC)) == [
        [PKG, f"{PKG}.b", f"{PKG}.c", f"{PKG}.hub.a"]
    ]
    assert G.cycles({"x": {"y"}, "y": {"x"}, "z": {"z"}}) == [["x", "y"]]
    assert G.report(edges, ("lazy",)) == ["lazy: 0 cycle(s)"]


def test_main_prints_and_checks(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert G.main(["--check", "--lazy"], root) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "7 modules; edges: module 6, typing 2, lazy 3"
    assert out[1] == "module: 1 cycle(s)"
    assert out[2] == "  3 modules: __init__, c, hub.a"
    assert out[4] == "  4 modules: __init__, b, c, hub.a"
    assert out[-2].startswith("module+typing+lazy: 1 cycle(s)")
    assert G.main([], root) == 0
    write(root, "c.py", "")
    write(root, "b.py", "")
    assert G.main(["--check"], root) == 0
