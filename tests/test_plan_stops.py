"""Stop anywhere (D12, W I10): whatever stops a configuration plan, the export records exactly what the nodes hold.

For every plan builder of `MeshConfigurator`, Hypothesis picks where the plan stops — after k = 0..n accepted
Config messages — and how: a refusal, silence, a cancellation from outside (an automation in `mode: restart`,
`script.turn_off`, Home Assistant stopping), or a crash that leaves only the plan journal, recorded at the next
start (`async_replay_journal`). The nodes are the Configuration Servers of the simulated mesh
(`tests/sim/servers.py`), fed every request the plan sends; after the stop the export on disk must say exactly
what they hold (subscriptions, publications, AppKey bindings), and running the action again must complete the
plan: the nodes and the export then match an uninterrupted run.

Runs on the `test_mesh_config` bench: the real `ProxyClient` over the fake proxy link, the android fixture export.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from custom_components.junghome_ble import mesh_config as mc
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode
from custom_components.junghome_ble.mesh_config import MeshConfigurator

from .sim.node import Received
from .sim.servers import Servers
from .test_mesh_config import (
    DALI_LOAD,
    DIMMER_KEY,
    DIMMER_LOAD,
    DIMMER_NODE,
    OUR_SRC,
    ROCKER_A,
    SOCKET_NODE,
    SWITCH_KEY,
    SWITCH_LOAD,
    Bench,
    make_bench,
)
from .test_mesh_config import (
    adverts as adverts,  # noqa: PLC0414  # the autouse fixture: no Bluetooth on the bench
)
from .test_mesh_config import (
    fast as fast,  # noqa: PLC0414  # the fixture, re-exported for this module
)

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.export import ProjectFile

    from .jhmesh.conftest import FastAsyncio

Wiring = dict[tuple[int, str], tuple[frozenset[int], int, frozenset[int]]]
Operation = Callable[[MeshConfigurator], Coroutine[Any, Any, Any]]

# one operation per plan builder, each with a plan of several messages on the android fixture
OPERATIONS: dict[str, Operation] = {
    "assign_key to a device": lambda c: c.assign_key(ROCKER_A, element=DIMMER_LOAD),
    "assign_key to a room": lambda c: c.assign_key(ROCKER_A, room="WC"),
    "assign_key to a socket": lambda c: c.assign_key(
        SWITCH_KEY, element=SOCKET_NODE, mode="switch"
    ),
    "clear_key": lambda c: c.clear_key(DIMMER_KEY),
    "set_room": lambda c: c.set_rooms([DALI_LOAD, SOCKET_NODE], "WC"),
    "set_room into a new room": lambda c: c.set_rooms(
        [SWITCH_LOAD, DIMMER_LOAD], "Garage", create=True
    ),
    "add_to_room": lambda c: c.add_to_rooms([DALI_LOAD, SOCKET_NODE], "WC"),
    "add_to_room into a new room": lambda c: c.add_to_rooms(
        [SWITCH_LOAD, DIMMER_LOAD], "Garage", create=True
    ),
    "remove_from_room": lambda c: c.remove_from_rooms(
        [SWITCH_LOAD, DIMMER_LOAD], "WC", force=True
    ),
    "delete_room": lambda c: c.delete_room("WC"),
    "set_threshold_devices": lambda c: c.set_threshold_devices(
        SOCKET_NODE, [DALI_LOAD, SWITCH_LOAD]
    ),
    "set_sensor_publication": lambda c: c.set_sensor_publication(SOCKET_NODE, False),
    "remove_device": lambda c: c.remove_node(DIMMER_NODE),
}


class Nodes:
    """The nodes' Configuration Servers of `tests/sim`, behind the bench's Config replies.

    Every request the plan sends is handed to the node's simulated server first; the bench then echoes the
    Success the simulated server gave (the stop being injected is the one exception). A Node Reset takes the
    node out of the comparison: it forgot everything.
    """

    def __init__(self, bench: Bench) -> None:
        self.servers: dict[int, _QuietServers] = {}
        for node in bench.hub.cdb.nodes:
            stub = SimpleNamespace(addr=node.unicast, app_keys={0: None})
            self.servers[node.unicast] = _QuietServers(stub, node, fresh=False)  # type: ignore[arg-type]
        self.reset: set[int] = set()
        self.stop: tuple[str, int] | None = (
            None  # (how, after how many accepted requests)
        )
        self.task: asyncio.Task[Any] | None = None
        self.accepted = 0
        self.stopped_at: bytes | None = None
        bench.config.on_request = self.request
        self.bench = bench

    def request(self, node: int, access: bytes) -> bool:
        """The bench's hook: True swallows the request (silence), else the bench answers it."""
        if self.stopped_at is not None and access == self.stopped_at:
            return True  # a retry of the silent request: still nobody answers
        if self.stop is not None and self.accepted == self.stop[1]:
            how = self.stop[0]
            self.stop = None
            if how == "refuse":
                self.bench.config.refuse[access] = 0x08  # answered, not taken
                return False
            self.stopped_at = access
            if how in ("cancel", "crash"):
                assert self.task is not None
                self.task.cancel()
            return True
        self.bench.config.refuse.pop(access, None)
        self.accepted += 1
        if access == C.node_reset():
            self.reset.add(node)
            return False
        op, _cid, params = decode_opcode(access)
        server = self.servers[node]
        server.answers.clear()
        server._config(Received(OUR_SRC, node, 5, op, None, params, "dev", 0))
        [answer] = server.answers
        assert answer[0] == C.STATUS_SUCCESS, f"{node:04X} refused {access.hex()}"
        return False

    def wiring(self) -> Wiring:
        return {
            (element, model): (
                frozenset(cfg.subscriptions),
                cfg.publish_address,
                frozenset(cfg.bind),
            )
            for unicast, server in self.servers.items()
            if unicast not in self.reset
            for (element, model), cfg in server.config.models.items()
        }


class _QuietServers(Servers):
    """`Servers` whose replies are kept for the test instead of going on air (the bench answers)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.answers: list[bytes] = []

    def reply(self, element: int, msg: Received, access: bytes) -> None:
        self.answers.append(decode_opcode(access)[2])


def export_wiring(pf: ProjectFile) -> Wiring:
    """What the export says every model of every node holds."""
    out: Wiring = {}
    for node in pf.cdb.nodes:
        for element in node.elements:
            for raw in element.raw_models:
                model = raw["modelId"]
                out[(element.address, model)] = (
                    frozenset(element.subscriptions(model)),
                    pf.publication(element, model) or 0,
                    frozenset(int(b) for b in raw.get("bind", [])),
                )
    return out


def agree(pf: ProjectFile, nodes: Nodes) -> None:
    """The export and the nodes agree on every model the export lists (and lists no node the nodes forgot)."""
    held = nodes.wiring()
    recorded = export_wiring(pf)
    differences = {
        key: (value, held.get(key))
        for key, value in recorded.items()
        if held.get(key) != value
    }
    assert not differences, differences
    for unicast in nodes.reset:
        assert pf.cdb.node_by_addr(unicast) is None, f"{unicast:04X} was reset"


async def run(bench: Bench, nodes: Nodes, operation: Operation) -> BaseException | None:
    """Run the operation as a task (the stop may cancel it); the exception it ended with, if any."""
    task = asyncio.ensure_future(operation(bench.configurator))
    nodes.task = task
    try:
        await task
    except (HomeAssistantError, asyncio.CancelledError) as err:
        return err
    return None


_REFERENCE: dict[str, tuple[Wiring, int]] = {}


async def _nothing(*_args: Any) -> None:
    """A record that never happens (the process is gone)."""


async def reference(name: str, root: Path) -> tuple[Wiring, int]:
    """The export's wiring after an uninterrupted run of the operation, and its count of Config messages."""
    if name not in _REFERENCE:
        (root / "reference").mkdir()
        bench = await make_bench(root / "reference")
        nodes = Nodes(bench)
        assert await run(bench, nodes, OPERATIONS[name]) is None
        pf = bench.reload()
        agree(pf, nodes)
        _REFERENCE[name] = export_wiring(pf), nodes.accepted
    return _REFERENCE[name]


# Over the 5 s budget on a slow runner, by design: each of the profile's examples builds a bench, loads, runs and
# reloads a whole export (seconds of work in all, no wait on the real clock beyond a silent node's 50 ms timeouts)
@pytest.mark.slow_ok
@settings(
    suppress_health_check=(HealthCheck.function_scoped_fixture, HealthCheck.too_slow)
)
@given(
    name=st.sampled_from(sorted(OPERATIONS)),
    how=st.sampled_from(("refuse", "silent", "cancel", "crash")),
    data=st.data(),
)
async def test_a_plan_stopped_anywhere_leaves_the_export_equal_to_the_nodes(
    fast: FastAsyncio, name: str, how: str, data: st.DataObject
) -> None:
    """Stop after k = 0..n accepted messages: the export records exactly what the nodes took; the cancellation
    is re-raised, a refusal or silence is a translated error; running the action again completes the plan."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        want, total = await reference(name, root)
        # a removal stops after its Node Reset at the earliest: a stop there is the action refused, nothing to record
        first = 1 if name == "remove_device" else 0
        accepted = data.draw(st.integers(min_value=first, max_value=total), "accepted")
        (root / "stopped").mkdir()
        bench = await make_bench(root / "stopped")
        nodes = Nodes(bench)
        agree(
            bench.reload(), nodes
        )  # the simulated nodes start from what the export says
        nodes.stop = (how, accepted)
        if how == "crash":
            # Home Assistant killed when the stop comes: nothing records the plan but its journal
            with patch.object(MeshConfigurator, "_record_stopped", _nothing):
                outcome = await run(bench, nodes, OPERATIONS[name])
            if accepted < total:
                assert bench.journal.data
                with patch.object(mc.ir, "async_create_issue"):
                    await MeshConfigurator(bench.hub).async_replay_journal()  # type: ignore[arg-type]
        else:
            outcome = await run(bench, nodes, OPERATIONS[name])
        agree(bench.reload(), nodes)
        assert bench.journal.data is None
        if accepted == total:  # nothing left to stop: the plan completed
            assert nodes.stop is not None
            assert outcome is None
            assert export_wiring(bench.reload()) == want
            return
        if how in ("cancel", "crash"):
            assert isinstance(outcome, asyncio.CancelledError)
        else:
            assert isinstance(outcome, HomeAssistantError)
        nodes.stopped_at = None
        if name == "remove_device":
            # the node is out of the network once it confirmed its reset (`exclude_node`): there is nothing to
            # run again, and the others' wiring left to it stays recorded as the nodes hold it
            again = await run(bench, nodes, OPERATIONS[name])
            assert isinstance(again, ServiceValidationError)
            assert again.translation_key == "service_unknown_element"
            agree(bench.reload(), nodes)
            return
        # running it again completes the plan
        assert await run(bench, nodes, OPERATIONS[name]) is None
        pf = bench.reload()
        agree(pf, nodes)
        assert export_wiring(pf) == want
