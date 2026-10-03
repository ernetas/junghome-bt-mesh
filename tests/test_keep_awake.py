"""keep_awake: the app's keep-alive for battery nodes (review-3 W4 / F24) — which nodes, the cadence, the stop.

`KeepAwake` runs against a hub stand-in (the detectors export's CDB, a scripted proxy, `last_heard`) on a fake
clock: `asyncio.sleep` in the module advances it, and a request that times out advances it by its timeout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.junghome_ble import keep_awake as ka
from custom_components.junghome_ble.const import (
    KEEP_AWAKE_INTERVAL,
    KEEP_AWAKE_RETRY,
    KEEP_AWAKE_TIMEOUT,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import CDB

DETECTORS = Path(__file__).parent / "fixtures" / "MeshNetwork-detectors.json"
TRANSMITTER_1G, KEY_1G = 0x0520, 0x0521  # battery wall transmitters
TRANSMITTER_2G, KEY_2G_B = 0x0530, 0x0532
MOTION, MOTION_KEY = 0x0500, 0x0501  # mains
START = 1000.0
_real_sleep = asyncio.sleep


class Clock:
    """`time.monotonic` and `asyncio.sleep` of the module: a sleep moves the clock, firing the events it passes."""

    def __init__(self) -> None:
        self.now = START
        self.events: list[tuple[float, Callable[[], None]]] = []

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, result: Any = None) -> Any:
        self.now += delay
        for event in [e for e in self.events if e[0] <= self.now]:
            self.events.remove(event)
            event[1]()
        await _real_sleep(0)
        return result


class Proxy:
    """Records every keep-alive Get (relative time, destination, PDU, match) and plays `outcomes` in order: None
    answers, an exception class is raised (a timeout after its wait), and once they run out the request hangs."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[float, int, bytes, Any]] = []
        self.outcomes: list[type[Exception] | None] = []

    async def request(
        self, dst: int, pdu: bytes, opcode: int, **kwargs: Any
    ) -> SimpleNamespace:
        assert opcode == M.VENDOR_PROPERTY_STATUS_OPCODES["admin"]
        assert kwargs["retries"] == 1
        assert kwargs["expect_cid"] == M.JUNG_CID
        self.calls.append((self.clock.now - START, dst, pdu, kwargs["match"]))
        if not self.outcomes:
            await (
                asyncio.Event().wait()
            )  # never answered: the test ends the hold meanwhile
        outcome = self.outcomes.pop(0)
        if outcome is TimeoutError:
            self.clock.now += kwargs["timeout"]
        if outcome is not None:
            raise outcome
        return SimpleNamespace(params=b"\x01\x50\x03\x05\x00")


def _task(_hass: Any, target: Coroutine[Any, Any, None], name: str) -> Any:
    return asyncio.get_running_loop().create_task(target, name=name)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(ka, "asyncio", fake)
    monkeypatch.setattr(ka, "time", fake)
    return fake


@pytest.fixture
def hub(clock: Clock) -> Any:
    return SimpleNamespace(
        cdb=CDB.load(DETECTORS),
        proxy=Proxy(clock),
        last_heard={},
        hass=None,
        entry=SimpleNamespace(async_create_background_task=_task),
    )


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(1000):
        if predicate():
            return
        await _real_sleep(0)
    raise AssertionError("condition not met")


def test_only_battery_nodes_are_sleepy() -> None:
    cdb = CDB.load(DETECTORS)
    assert ka.sleepy_node(cdb, TRANSMITTER_1G) == TRANSMITTER_1G
    assert (
        ka.sleepy_node(cdb, KEY_2G_B) == TRANSMITTER_2G
    )  # an element resolves to its node
    assert ka.sleepy_node(cdb, MOTION_KEY) is None
    assert ka.sleepy_node(cdb, 0x7000) is None  # no such node


async def test_the_keep_alive_follows_the_apps_cadence(hub: Any, clock: Clock) -> None:
    """The app's `KeepLowPowerDeviceAwake`: Admin Get 0x5001 (`C2 27 05 01 50`) to the node's primary element
    every 6 s, 1 s after one went unanswered; a message from the node postpones the next one. Nothing for a mains
    node; one task however many holds; none after the last hold ends."""
    proxy: Proxy = hub.proxy
    proxy.outcomes = [None, None, None, TimeoutError, ConnectionError, None]
    clock.events.append(
        (START + 15, lambda: hub.last_heard.__setitem__(TRANSMITTER_1G, START + 15))
    )
    keep = ka.KeepAwake(hub)
    async with keep.hold([MOTION, MOTION_KEY]):
        assert keep._tasks == {}
    async with keep.hold([KEY_1G, MOTION]):
        async with keep.hold([TRANSMITTER_1G]):
            assert list(keep._tasks) == [TRANSMITTER_1G]
            await _until(lambda: len(proxy.calls) == 2)
        assert list(keep._tasks) == [TRANSMITTER_1G]  # the outer hold keeps it
        await _until(lambda: len(proxy.calls) == 7)
        task = keep._tasks[TRANSMITTER_1G]
    assert keep._tasks == {}
    await _until(task.done)
    assert task.cancelled()
    interval, retry, timeout = KEEP_AWAKE_INTERVAL, KEEP_AWAKE_RETRY, KEEP_AWAKE_TIMEOUT
    assert (interval, retry, timeout) == (6.0, 1.0, 3.0)
    assert [t for t, *_ in proxy.calls] == [
        6,  # quiet since the hold began
        12,
        21,  # the node spoke at 15
        27,  # unanswered after 3 s ...
        31,  # ... asked again 1 s later; the link could not send it ...
        32,  # ... again 1 s later: answered
        38,
    ]
    assert {dst for _, dst, _, _ in proxy.calls} == {TRANSMITTER_1G}
    assert {pdu for _, _, pdu, _ in proxy.calls} == {bytes.fromhex("c2 2705 0150")}
    match = proxy.calls[0][3]
    assert match(SimpleNamespace(params=b"\x01\x50\x03\x05\x00"))
    assert not match(
        SimpleNamespace(params=b"\x03\x50\x03\x00")
    )  # the KeyMode's Status is not its answer
    for _ in range(20):
        await _real_sleep(0)
    assert len(proxy.calls) == 7


async def test_two_battery_nodes_get_a_task_each(hub: Any) -> None:
    keep = ka.KeepAwake(hub)
    async with keep.hold([KEY_1G, KEY_2G_B, TRANSMITTER_2G]):
        assert sorted(keep._tasks) == [TRANSMITTER_1G, TRANSMITTER_2G]
        await _until(lambda: len(hub.proxy.calls) == 2)
    assert keep._tasks == {}
    assert sorted(dst for _, dst, _, _ in hub.proxy.calls) == [
        TRANSMITTER_1G,
        TRANSMITTER_2G,
    ]


async def test_a_dead_keep_alive_is_logged_and_replaced(
    hub: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A keep-alive that failed on something unexpected says so and ends; the next hold of the node starts a new
    one, even while an earlier hold is still open."""
    proxy: Proxy = hub.proxy
    proxy.outcomes = [RuntimeError, None]
    keep = ka.KeepAwake(hub)
    async with keep.hold([KEY_1G]):
        dead = keep._tasks[TRANSMITTER_1G]
        await _until(dead.done)
        assert not dead.cancelled()
        assert "Keep-alive of 0520 stopped" in caplog.text
        async with keep.hold([TRANSMITTER_1G]):
            task = keep._tasks[TRANSMITTER_1G]
            assert task is not dead
            # answered, then the next one hangs
            await _until(lambda: len(proxy.calls) == 3)
        assert keep._tasks[TRANSMITTER_1G] is task  # the outer hold keeps it
    assert keep._tasks == {}
    assert [t for t, *_ in proxy.calls] == [6, 12, 18]


async def test_a_hold_that_could_not_start_releases_what_it_took(hub: Any) -> None:
    """Starting the second node's task fails (the entry is unloading): the first node is released again."""
    started: list[int] = []

    def start(_hass: Any, target: Coroutine[Any, Any, None], name: str) -> Any:
        if started:
            target.close()
            raise RuntimeError("entry unloading")
        started.append(1)
        return _task(_hass, target, name)

    hub.entry = SimpleNamespace(async_create_background_task=start)
    keep = ka.KeepAwake(hub)
    with pytest.raises(RuntimeError):
        async with keep.hold([KEY_1G, KEY_2G_B]):
            pytest.fail("the block must not run")
    assert keep._holders == {}
    assert keep._tasks == {}
