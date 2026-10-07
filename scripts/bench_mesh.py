"""Time the per-message hot paths against a synthetic large mesh.

Run from the repository root with the development venv:

    PYTHONPATH=. .venv/bin/python scripts/bench_mesh.py [--nodes 300] [--rpl 600]

The mesh is the test fixture's network with its metering socket cloned until it has `--nodes` nodes (fresh UUIDs
and addresses; every key is the fixture's, none is real). Each line is the mean time of one call, in microseconds:
an element lookup (`CDB.element`) at the last node and for an address no node has, a meter lookup
(`Devices.by_meter`), the classification of one Node-Identity advert of the last node and of one Network-ID and one
Node-Identity advert of another network (`ProxyClient.classify_service_data`, the same bytes over and over as a proxy repeats them), and the bound
the sequence-number store puts on a send (`HAState._limit`, both copies holding a replay list of `--rpl` sources)
with one `HAState.reserve_seq` per sent PDU. A benchmark, not a test: nothing here asserts a time.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import timeit
import uuid
from pathlib import Path
from typing import Any

from custom_components.junghome_ble.coordinator import HAState
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.client import LocalState, ProxyClient
from custom_components.junghome_ble.jhmesh.devices import build_devices

FIXTURE = Path(__file__).resolve().parent.parent / "tests/fixtures/MeshNetwork.json"
TEMPLATE = "0172"  # the fixture's metering socket: two elements, one of them a meter


class _Hass:
    """Just what `HAState` asks of Home Assistant: `data`, and tasks (run to completion at once: writes land)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def async_create_task(self, coro: Any, _name: str | None = None) -> None:
        try:
            coro.send(None)
        except StopIteration:
            pass


class _Store:
    """A `SeqStore` whose writes land as soon as they are asked for."""

    def __init__(self, hass: _Hass) -> None:
        self.hass = hass
        self.written: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.written = data

    def async_delay_save(self, _data: Any, _delay: float) -> None:
        """Debounced writes never land here: only the immediate ones move `written`, as on a busy link."""


def large_cdb(count: int) -> CDB:
    """Return the fixture's network with its metering socket cloned until it has `count` nodes."""
    net = json.loads(FIXTURE.read_text(encoding="utf-8"))["meshNetwork"]
    template = next(n for n in net["nodes"] if n["unicastAddress"] == TEMPLATE)
    address = 0x1000
    while len(net["nodes"]) < count:
        node = copy.deepcopy(template)
        node["UUID"] = str(uuid.uuid4()).upper()
        node["unicastAddress"] = f"{address:04X}"
        node["name"] = f"Socket {address:04X}"
        net["nodes"].append(node)
        address += len(node["elements"])
    return CDB.from_network(net, None)


def per_call(func: Any, number: int) -> float:
    """Mean time of one call of `func` in microseconds, the best of three runs of `number`."""
    return min(timeit.repeat(func, number=number, repeat=3)) / number * 1e6


def main(argv: list[str] | None = None) -> int:
    """Print the mean time of each hot path."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nodes", type=int, default=300)
    parser.add_argument("--rpl", type=int, default=600)
    args = parser.parse_args(argv)

    cdb = large_cdb(args.nodes)
    devices = build_devices(cdb)
    last = cdb.nodes[-1]
    meter = devices.metered[-1].meter_address
    if meter is None:  # `metered` holds loads with a meter only
        raise SystemExit("no metered load")
    proxy = ProxyClient(cdb, LocalState(None, 0x0D00))
    nk = cdb.net_keys[0]
    rnd = bytes(range(8))
    identity = b"\x01" + nk.node_identity_hash(rnd, last.unicast) + rnd
    foreign = b"\x00" + bytes(8)
    # another network's Node Identity: no node of ours matches, so every node is tried
    stranger = b"\x01" + bytes(16)

    hass = _Hass()
    store, backup = _Store(hass), _Store(hass)
    state = HAState(store, None, 0x0D00, "bench", backup)  # type: ignore[arg-type]
    state.rpl = {0x2000 + i: (0, i) for i in range(args.rpl)}
    state.persist_now()

    rows = [
        ("CDB.element, last node", lambda: cdb.element(last.unicast), 20000),
        ("CDB.element, no such address", lambda: cdb.element(0x7FFF), 20000),
        ("Devices.by_meter, last meter", lambda: devices.by_meter(meter), 20000),
        ("classify, Node Identity", lambda: proxy.classify_service_data(identity), 200),
        (
            "classify, foreign Network ID",
            lambda: proxy.classify_service_data(foreign),
            20000,
        ),
        (
            "classify, foreign Node Identity",
            lambda: proxy.classify_service_data(stranger),
            200,
        ),
        ("HAState._limit", state._limit, 2000),  # noqa: SLF001
        ("HAState.reserve_seq(1)", lambda: state.reserve_seq(1), 2000),
    ]
    sys.stdout.write(
        f"{len(cdb.nodes)} nodes, replay list of {len(state.rpl)} sources\n"
    )
    for name, func, number in rows:
        sys.stdout.write(f"{name:32} {per_call(func, number):10.2f} µs\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
