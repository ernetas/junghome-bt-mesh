"""The integration's layers, from its imports (review-4 A4-11), read with `tools/import_graph.py`.

- No module imports a platform module: Home Assistant loads the platforms; what two of them share lives in a module
  of its own (`conversions.py`, `bus_events.py`, `actions/dim.py`, `device_info.py`).
- `jhmesh` imports nothing from the integration: it is published on its own.
- The static import graph — module-level imports and those made for the type checker only — has no cycle: the hub's
  parts name the hub by a Protocol (`protocols.py`), not by `coordinator.JungHomeHub`.
"""

from __future__ import annotations

from custom_components.junghome_ble.const import PLATFORMS
from tools import import_graph as G

EDGES = G.edges()
JHMESH = f"{G.PACKAGE}.jhmesh"


def _in_jhmesh(name: str) -> bool:
    return name == JHMESH or name.startswith(f"{JHMESH}.")


def _describe(edges: list[G.Edge]) -> list[str]:
    return sorted(
        f"{G.short(e.source)} -> {G.short(e.target)} ({e.kind})" for e in edges
    )


def test_no_module_imports_a_platform_module() -> None:
    platforms = {f"{G.PACKAGE}.{platform}" for platform in PLATFORMS}
    assert len(platforms) == len(PLATFORMS)
    assert all(p in G.modules() for p in platforms)
    assert _describe([e for e in EDGES if e.target in platforms]) == []


def test_jhmesh_imports_nothing_from_the_integration() -> None:
    assert any(_in_jhmesh(e.source) for e in EDGES)
    assert (
        _describe(
            [e for e in EDGES if _in_jhmesh(e.source) and not _in_jhmesh(e.target)]
        )
        == []
    )


def test_the_static_import_graph_has_no_cycle() -> None:
    assert G.cycles(G.graph_of(EDGES, G.STATIC)) == []
    assert G.main(["--check"]) == 0
