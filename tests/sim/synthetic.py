"""A scaled synthetic network: the fixture network grown to a few hundred nodes, with a radio layout to match.

`scaled_network(n, seed)` copies the fixture export (`tests/fixtures/MeshNetwork.json`) and adds clones of its load
nodes (push-button 1-gang, socket, dimmer, 2-channel actuator) until it holds `n` nodes, each clone with addresses,
a UUID, a device key and a name of its own (synthetic patterns, like the fixture's) and without the template's
group publications and subscriptions (a clone answers a Set by unicast). The layout keeps the fixture's chain and
hangs a relay tree off the proxy 0148: a ring of relays next to it, three more relays behind each of those, and the
leaves (relay off) on the second ring, each heard by two of its relays — every node within four hops of the proxy,
the reach of the client's TTL 5.
"""

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mesh import Mesh
from .topology import FIXTURE_HOP_MATRIX, Topology, parse_hop_matrix

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "MeshNetwork.json"
PROXY = 0x0148
TEMPLATES = (0x0148, 0x0172, 0x0300, 0x0400)  # the load nodes the clones copy, in turn
FIRST_CLONE = 0x0500  # clones' unicasts from here (the fixture's end at 0x0401; the client sends from 0x0D00)
RING = 6  # relays next to the proxy
FAN = 3  # relays behind each of the ring's


@dataclass
class ScaledNetwork:
    """The export, the radio adjacency between primary unicasts and the nodes that relay."""

    export: dict[str, Any]
    adjacency: dict[int, set[int]]
    relays: set[int]

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.export, indent=1), encoding="utf-8")
        return path

    @property
    def clones(self) -> list[int]:
        return sorted(a for a in self.adjacency if a >= FIRST_CLONE)


def scaled_network(nodes: int, *, seed: int) -> ScaledNetwork:
    """The fixture network grown to `nodes` nodes (at least the fixture's own seven plus the relay rings)."""
    export = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mesh = export["meshNetwork"]
    by_unicast = {int(n["unicastAddress"], 16): n for n in mesh["nodes"]}
    adjacency = parse_hop_matrix(FIXTURE_HOP_MATRIX)
    relays = {
        a for a in adjacency if a != 0x0001
    }  # the fixture's nodes all relay; the phone does not
    rng = random.Random(seed)  # noqa: S311  # a repeatable layout, not a secret
    wanted = nodes - len(mesh["nodes"])
    assert wanted >= RING + RING * FAN, (
        f"{nodes} nodes leave no room for the relay rings"
    )
    address = FIRST_CLONE
    ring: list[int] = []
    second: list[int] = []
    for i in range(wanted):
        template = by_unicast[TEMPLATES[i % len(TEMPLATES)]]
        clone = _clone(template, i, address)
        mesh["nodes"].append(clone)
        unicast = address
        address += len(clone["elements"])
        adjacency.setdefault(unicast, set())
        if len(ring) < RING:
            ring.append(unicast)
            relays.add(unicast)
            _link(adjacency, unicast, PROXY)
            if len(ring) > 1:
                _link(adjacency, unicast, ring[-2])
        elif len(second) < RING * FAN:
            parent = ring[len(second) // FAN]
            second.append(unicast)
            relays.add(unicast)
            _link(adjacency, unicast, parent)
            if len(second) > 1:
                _link(adjacency, unicast, second[-2])
        else:
            for relay in rng.sample(second, 2):
                _link(adjacency, unicast, relay)
    if len(ring) > 2:
        _link(adjacency, ring[0], ring[-1])
    return ScaledNetwork(export, adjacency, relays)


def _link(adjacency: dict[int, set[int]], a: int, b: int) -> None:
    adjacency.setdefault(a, set()).add(b)
    adjacency.setdefault(b, set()).add(a)


def _clone(template: dict[str, Any], i: int, address: int) -> dict[str, Any]:
    """Template `template` as clone number `i` at `address`: own identity, no group addressing."""
    clone = copy.deepcopy(template)
    clone["UUID"] = f"5C000000-0000-4000-8000-{i + 1:012X}"
    clone["name"] = f"{template['name']} {i + 1}"
    clone["unicastAddress"] = f"{address:04X}"
    clone["deviceKey"] = f"dd{i + 1:030x}"
    for element in clone["elements"]:
        for model in element["models"]:
            model.pop("publish", None)
            model["subscribe"] = []
    return clone


def build_mesh(network: ScaledNetwork, path: Path, **options: Any) -> Mesh:
    """The simulated mesh of `network`, its export written to `path` (the client's copy reads it too); relay off on
    the leaves."""
    mesh = Mesh(
        cdb_path=network.write(path),
        topology=Topology(network.adjacency),
        proxies=(PROXY,),
        **options,
    )
    for unicast, node in mesh.nodes.items():
        if unicast not in network.relays and node.servers is not None:
            node.servers.config.relay = (0, 0, 0)
    return mesh
