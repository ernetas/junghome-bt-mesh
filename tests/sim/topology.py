"""Who hears whom: the radio adjacency of the simulated mesh, read from a `docs/hop-matrix.md`-style table.

`tools/mesh_poc.py config hopmatrix` measures, for every pair of nodes, the fewest hops a Heartbeat took (row = the
node that beat, column = the node that counted; `1..2` = min..max, `-` = no beat, `?` = no answer, `·` = itself).
Two nodes hear each other directly when either direction saw a single hop — that is the adjacency here.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable

_ADDRESS = re.compile(r"`?([0-9A-Fa-f]{4})`?")


def parse_hop_matrix(text: str) -> dict[int, set[int]]:
    """Return the 1-hop adjacency of the first hop matrix table in `text` (symmetric)."""
    lines = [line.strip() for line in text.splitlines()]
    start = next(
        i for i, line in enumerate(lines) if line.startswith("|") and "beats" in line
    )
    rows: list[list[str]] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break  # the end of the table (the file goes on with other tables)
        rows.append(line.strip("|").split("|"))
    header = rows[0]
    columns = [int(_ADDRESS.search(c)[1], 16) for c in header[1:]]  # type: ignore[index]
    adjacency: dict[int, set[int]] = {c: set() for c in columns}
    for row in rows:
        first = _ADDRESS.fullmatch(row[0].strip())
        if first is None:
            continue
        origin = int(first[1], 16)
        adjacency.setdefault(origin, set())
        for column, cell in zip(columns, row[1:], strict=True):
            hops = cell.strip().split("..")[0]
            if hops == "1":
                adjacency[origin].add(column)
                adjacency.setdefault(column, set()).add(origin)
    return adjacency


class Topology:
    """A symmetric radio adjacency between node addresses (primary unicasts); links can be cut and restored."""

    def __init__(self, adjacency: dict[int, set[int]]) -> None:
        self._links: dict[int, set[int]] = {}
        for a, neighbours in adjacency.items():
            self._links.setdefault(a, set())
            for b in neighbours:
                self.link(a, b)

    @classmethod
    def from_hop_matrix(cls, text: str) -> Topology:
        return cls(parse_hop_matrix(text))

    @property
    def nodes(self) -> set[int]:
        return set(self._links)

    def neighbours(self, address: int) -> set[int]:
        return self._links.get(address, set())

    def link(self, a: int, b: int) -> None:
        if a != b:
            self._links.setdefault(a, set()).add(b)
            self._links.setdefault(b, set()).add(a)

    def cut(self, a: int, b: int) -> None:
        self._links.get(a, set()).discard(b)
        self._links.get(b, set()).discard(a)

    def add(self, address: int, neighbours: Iterable[int]) -> None:
        self._links.setdefault(address, set())
        for b in neighbours:
            self.link(address, b)

    def hops(self, a: int, b: int) -> int | None:
        """Fewest radio hops from `a` to `b` (None when unreachable)."""
        seen, todo = {a: 0}, deque([a])
        while todo:
            here = todo.popleft()
            if here == b:
                return seen[here]
            for n in sorted(self.neighbours(here)):
                if n not in seen:
                    seen[n] = seen[here] + 1
                    todo.append(n)
        return None


# The fixture network (`tests/fixtures/MeshNetwork.json`) laid out as a chain, so that reaching the far end takes
# relays: the proxy 0148 hears 0232 and the gateway 00DC; 0400 is four hops away. The phone (0001, the provisioner)
# sits next to the gateway. Same format as docs/hop-matrix.md.
FIXTURE_HOP_MATRIX = """
| ↓ beats / counts → | `0001` | `00DC` | `0148` | `0232` | `0172` | `0300` | `0400` |
|---|---|---|---|---|---|---|---|
| `0001` | · | 1 | 2 | 3 | 2 | 3 | 4 |
| `00DC` | 1 | · | 1 | 2 | 1 | 2 | 3 |
| `0148` | 2 | 1 | · | 1 | 2 | 3 | 4 |
| `0232` | 3 | 2 | 1 | · | 1 | 2 | 3 |
| `0172` | 2 | 1 | 2 | 1 | · | 1 | 2 |
| `0300` | 3 | 2 | 3 | 2 | 1 | · | 1 |
| `0400` | 4 | 3 | 4 | 3 | 2 | 1 | · |
"""
