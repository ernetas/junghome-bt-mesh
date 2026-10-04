#!/usr/bin/env python3
"""Print the integration's import cycles, with and without the imports made for type checking only.

    tools/import_graph.py              # the cycles of the static graph (module-level + `TYPE_CHECKING` imports)
    tools/import_graph.py --lazy       # also count the imports made inside a function
    tools/import_graph.py --check      # exit 1 when the static graph has a cycle

An edge is one module of `custom_components/junghome_ble` importing another (`jhmesh` included, also when imported
by its top-level name). Each import is one of three kinds: *module* (executed on import), *typing* (under
`if TYPE_CHECKING:`, read by the type checker only) and *lazy* (inside a function body: executed when called, which
is how a run-time cycle is broken). A cycle is a strongly connected component of more than one module.
`tests/test_layers.py` keeps the static graph acyclic. Standard library only (AST): it runs without Home Assistant.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "custom_components.junghome_ble"
ALIASES = {
    "jhmesh": f"{PACKAGE}.jhmesh"
}  # the repository's top-level `jhmesh` is the bundled package
KINDS = ("module", "typing", "lazy")
STATIC = ("module", "typing")


@dataclass(frozen=True)
class Edge:
    """`source` imports `target`; `kind` is one of `KINDS`."""

    source: str
    target: str
    kind: str


def modules(root: Path = ROOT) -> dict[str, Path]:
    """Return the integration's modules by dotted name (a package's `__init__.py` is the package)."""
    found: dict[str, Path] = {}
    for path in sorted((root / PACKAGE.replace(".", "/")).rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        found[".".join(parts)] = path
    return found


def _type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def imports(
    tree: ast.AST, kind: str = "module"
) -> list[tuple[ast.Import | ast.ImportFrom, str]]:
    """Return every import statement under `tree` with its kind (`KINDS`)."""
    found: list[tuple[ast.Import | ast.ImportFrom, str]] = []
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.Import | ast.ImportFrom):
            found.append((child, kind))
        elif isinstance(child, ast.If) and _type_checking(child.test):
            typing = "typing" if kind == "module" else kind
            found += imports(ast.Module(body=child.body, type_ignores=[]), typing)
            found += imports(ast.Module(body=child.orelse, type_ignores=[]), kind)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            found += imports(child, "lazy")
        else:
            found += imports(child, kind)
    return found


def _canonical(name: str) -> str:
    for short, full in ALIASES.items():
        if name == short or name.startswith(f"{short}."):
            return full + name[len(short) :]
    return name


def _targets(
    node: ast.Import | ast.ImportFrom, package: str, known: dict[str, Path]
) -> list[str]:
    """Return the dotted names an import statement loads: a `from` import's names that are modules, else its module."""
    if isinstance(node, ast.Import):
        return [_canonical(alias.name) for alias in node.names]
    if node.level:
        parts = package.split(".")
        base = ".".join(
            [
                *parts[: len(parts) - node.level + 1],
                *([node.module] if node.module else []),
            ]
        )
    else:
        base = _canonical(node.module or "")
    return [
        sub if (sub := f"{base}.{alias.name}") in known else base
        for alias in node.names
    ]


def edges(root: Path = ROOT) -> list[Edge]:
    """Return every import edge between two modules of the integration."""
    known = modules(root)
    out: list[Edge] = []
    for name, path in known.items():
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        for node, kind in imports(
            ast.parse(path.read_text(encoding="utf-8"), str(path))
        ):
            for imported in _targets(node, package, known):
                target = imported
                while target and target not in known:
                    target = target.rpartition(".")[0]
                if target and target != name:
                    out.append(Edge(name, target, kind))
    return out


def graph_of(all_edges: list[Edge], kinds: tuple[str, ...]) -> dict[str, set[str]]:
    """Return the adjacency of the edges of the given kinds."""
    graph: dict[str, set[str]] = defaultdict(set)
    for edge in all_edges:
        if edge.kind in kinds:
            graph[edge.source].add(edge.target)
    return graph


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return the strongly connected components of more than one module (Tarjan), largest first."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    found: list[list[str]] = []

    def connect(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        for w in sorted(graph.get(v, ())):
            if w not in index:
                connect(w)
                low[v] = min(low[v], low[w])
            elif w in stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component = stack[stack.index(v) :]
            del stack[stack.index(v) :]
            if len(component) > 1:
                found.append(sorted(component))

    for v in sorted(graph):
        if v not in index:
            connect(v)
    return sorted(found, key=lambda c: (-len(c), c))


def short(name: str) -> str:
    """Return a module's name within the integration (`__init__` for the package itself)."""
    return name.removeprefix(f"{PACKAGE}.") if name != PACKAGE else "__init__"


def report(all_edges: list[Edge], kinds: tuple[str, ...]) -> list[str]:
    """Return the lines describing the cycles of the graph of `kinds`."""
    found = cycles(graph_of(all_edges, kinds))
    return [f"{'+'.join(kinds)}: {len(found)} cycle(s)"] + [
        f"  {len(component)} modules: {', '.join(short(m) for m in component)}"
        for component in found
    ]


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    """Print the cycles; with `--check`, 1 when the static graph has one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--lazy", action="store_true", help="also count imports inside functions"
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when the static graph has a cycle"
    )
    args = parser.parse_args(argv)
    all_edges = edges(root)
    counts = ", ".join(f"{k} {sum(e.kind == k for e in all_edges)}" for k in KINDS)
    print(f"{len(modules(root))} modules; edges: {counts}")
    for kinds in [("module",), STATIC] + ([KINDS] if args.lazy else []):
        print("\n".join(report(all_edges, kinds)))
    return 1 if args.check and cycles(graph_of(all_edges, STATIC)) else 0


if __name__ == "__main__":
    sys.exit(main())
