"""jhmesh.standalone: scanning, connecting and the reconnect loop with bleak replaced by fakes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Self

import pytest

from jhmesh import standalone
from jhmesh.cdb import CDB, Node
from jhmesh.client import MESH_PROXY_SERVICE, ProxyCandidate, ProxyClient
from jhmesh.provisioning import (
    MESH_PROVISIONING_SERVICE,
    Capabilities,
    ProvisioningData,
    ProvisioningError,
    UnprovisionedDevice,
)
from jhmesh.standalone import StandaloneLink, connect, scan_for_proxies

from .conftest import LIGHT_2G, PROXY_NODE, FakeBleak, FastAsyncio, Recorder
from .test_provisioning import FakeDevice

ADDR_A, ADDR_B, ADDR_C = "AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB", "CC:CC:CC:CC:CC:CC"


def adv(
    service_data: bytes | None, rssi: int = -60, local_name: str | None = None
) -> Any:
    sd = {MESH_PROXY_SERVICE: service_data} if service_data is not None else {}
    return SimpleNamespace(service_data=sd, rssi=rssi, local_name=local_name)


def device(address: str) -> Any:
    return SimpleNamespace(address=address)


async def until(pred: Callable[[], bool], turns: int = 2000) -> None:
    """Step the loop until ``pred`` holds (every library sleep is a single loop turn under the ``fast`` fixture)."""
    for _ in range(turns):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


class FakeScanner:
    """bleak.BleakScanner stand-in: reports the class-level ``adverts`` (device, advertisement) pairs on start()."""

    adverts: list[tuple[Any, Any]] = []
    instances: list[FakeScanner] = []

    def __init__(
        self,
        cb: Callable[[Any, Any], None],
        service_uuids: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.cb, self.service_uuids = cb, service_uuids
        self.started = self.stopped = False
        FakeScanner.instances.append(self)

    async def start(self) -> None:
        self.started = True
        for dev, a in list(self.adverts):
            self.cb(dev, a)

    async def stop(self) -> None:
        self.stopped = True

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


@pytest.fixture
def scanner(monkeypatch: pytest.MonkeyPatch) -> type[FakeScanner]:
    FakeScanner.adverts = []
    FakeScanner.instances = []
    monkeypatch.setattr(standalone, "BleakScanner", FakeScanner)
    return FakeScanner


@pytest.fixture
def bleak_client(monkeypatch: pytest.MonkeyPatch, cdb: CDB) -> Any:
    """Replace bleak.BleakClient with a FakeBleak-backed class; ``made`` lists instances, ``fail`` maps addresses to
    the error their connect() raises."""
    made: list[Any] = []
    fail: dict[str, Exception] = {}

    class FakeBleakClient(FakeBleak):
        def __init__(
            self,
            address_or_device: Any,
            disconnected_callback: Any = None,
            timeout: float | None = None,
            **kw: Any,
        ) -> None:
            super().__init__(
                cdb, address=getattr(address_or_device, "address", address_or_device)
            )
            self.is_connected = False
            # as a real proxy: `connect` waits for this beacon before the filter request (`BEACON_WAIT`)
            self.beacon_on_subscribe = True
            self.given = address_or_device
            self.disconnected_callback = disconnected_callback
            self.timeout = timeout
            made.append(self)

        async def connect(self) -> None:
            if self.address in fail:
                raise fail[self.address]
            self.is_connected = True

        def drop(self) -> None:
            self.is_connected = False
            if self.disconnected_callback:
                self.disconnected_callback(self)

    monkeypatch.setattr(standalone, "BleakClient", FakeBleakClient)
    return SimpleNamespace(cls=FakeBleakClient, made=made, fail=fail)


@pytest.fixture
def network_id(cdb: CDB) -> bytes:
    return cdb.net_keys[0].network_id


# ----------------------------------------------------------------------------- scan_for_proxies


async def test_scan_for_proxies(
    proxy: ProxyClient,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    cdb: CDB,
    network_id: bytes,
):
    nk = cdb.net_keys[0]
    rnd = bytes(range(8))
    cdb.nodes.append(
        Node("X", "Elementless", 0x0777, bytes(16), 1)
    )  # a node the CDB knows but has no elements for
    scanner.adverts = [
        (device(ADDR_A), adv(b"\x00" + network_id, rssi=-70, local_name="JUNG A")),
        (
            device(ADDR_B),
            adv(
                b"\x01" + nk.node_identity_hash(rnd, LIGHT_2G) + rnd,
                rssi=-50,
                local_name="ignored",
            ),
        ),
        (
            device(ADDR_C),
            adv(
                b"\x01" + nk.node_identity_hash(rnd, 0x0777) + rnd,
                rssi=-90,
                local_name="C",
            ),
        ),
        (
            device("DD:DD:DD:DD:DD:DD"),
            adv(b"\x00" + bytes(8), rssi=-10),
        ),  # another network
        (
            device("EE:EE:EE:EE:EE:EE"),
            adv(None, rssi=-10),
        ),  # no Mesh Proxy service data
        (
            device(ADDR_A),
            adv(b"\x00" + network_id, rssi=-65, local_name="JUNG A"),
        ),  # repeated advert: latest wins
    ]
    cands = await scan_for_proxies(proxy, seconds=0.01)
    assert fast.sleeps == [0.01]
    sc = scanner.instances[0]
    assert sc.service_uuids == [MESH_PROXY_SERVICE]
    assert sc.started
    assert sc.stopped
    assert [(c.address, c.rssi, c.kind, c.node_addr, c.name) for c in cands] == [
        (ADDR_B, -50, "node-identity", LIGHT_2G, "Push-button 2-gang"),
        (ADDR_A, -65, "network-id", None, "JUNG A"),
        (ADDR_C, -90, "node-identity", 0x0777, "C"),
    ]
    assert all(isinstance(c, ProxyCandidate) for c in cands)
    assert cands[0].device is scanner.adverts[1][0]
    assert cands[0].adv is scanner.adverts[1][1]  # kept for `mesh_poc.py scan --adv`
    assert cands[1].adv is scanner.adverts[-1][1]


async def test_scan_for_proxies_empty(
    proxy: ProxyClient, scanner: type[FakeScanner], fast: FastAsyncio
):
    assert await scan_for_proxies(proxy) == []
    assert fast.sleeps == [4.0]


# ----------------------------------------------------------------------------- connect


async def test_connect_with_explicit_candidate(
    proxy: ProxyClient, bleak_client: Any, fast: FastAsyncio
):
    cand = ProxyCandidate(
        ADDR_A, -50, "network-id", None, "JUNG A", device=device(ADDR_A)
    )
    assert await connect(proxy, cand, timeout=7.5) is cand
    client = bleak_client.made[0]
    assert client.given is cand.device
    assert client.timeout == 7.5
    assert client.is_connected
    assert proxy.client is client
    assert proxy.connected
    assert proxy.proxy_addr == PROXY_NODE
    assert fast.sleeps == [0.3]
    # the disconnected callback is wired to the ProxyClient
    client.drop()
    assert proxy.client is None
    assert not proxy.connected


async def test_connect_waits_for_the_proxy_beacon_before_the_filter(
    proxy: ProxyClient,
    bleak_client: Any,
    fast: FastAsyncio,
    monkeypatch: pytest.MonkeyPatch,
):
    """Review-4 R4-10: the CLI's link sent its filter request without waiting for the proxy's beacon (the hub
    waits), so after an IV Update the request went out under a stale IV index and the proxy dropped it. `connect`
    waits up to BEACON_WAIT for it; a proxy that does not beacon gets the filter after that wait."""
    waits: list[float] = []
    wait = proxy._wait_for_beacon

    async def spy(timeout: float) -> None:
        waits.append(timeout)
        await wait(timeout)

    monkeypatch.setattr(proxy, "_wait_for_beacon", spy)
    await connect(proxy, ProxyCandidate(ADDR_A, -50, "network-id"))
    assert waits == [standalone.BEACON_WAIT]
    assert proxy.proxy_addr == PROXY_NODE  # the filter went out and was answered
    await proxy.detach()

    # a proxy that does not beacon when subscribed to: the filter follows the (shortened) wait
    monkeypatch.setattr(standalone, "BEACON_WAIT", 0.01)
    connecting = bleak_client.cls.__init__

    def quiet(self: Any, *args: Any, **kw: Any) -> None:
        connecting(self, *args, **kw)
        self.beacon_on_subscribe = False

    monkeypatch.setattr(bleak_client.cls, "__init__", quiet)
    await connect(proxy, ProxyCandidate(ADDR_B, -50, "network-id"))
    assert waits[1:] == [0.01]
    assert proxy.proxy_addr == PROXY_NODE


async def test_connect_uses_the_address_when_there_is_no_device_object(
    proxy: ProxyClient, bleak_client: Any
):
    await connect(proxy, ProxyCandidate(ADDR_A, -50, "network-id"))
    assert bleak_client.made[0].given == ADDR_A
    assert bleak_client.made[0].timeout == 15.0


async def test_connect_scans_when_no_candidate_is_given(
    proxy: ProxyClient, bleak_client: Any, scanner: type[FakeScanner], network_id: bytes
):
    with pytest.raises(RuntimeError, match="no proxy node of this network in range"):
        await connect(proxy, scan_seconds=0.01)
    assert bleak_client.made == []
    scanner.adverts = [
        (device(ADDR_A), adv(b"\x00" + network_id, rssi=-70)),
        (device(ADDR_B), adv(b"\x00" + network_id, rssi=-40)),
    ]
    cand = await connect(proxy, scan_seconds=0.01)
    assert cand.address == ADDR_B
    assert bleak_client.made[0].address == ADDR_B  # strongest signal first
    assert proxy.connected


async def test_connect_propagates_connect_errors(proxy: ProxyClient, bleak_client: Any):
    bleak_client.fail[ADDR_A] = OSError("adapter busy")
    with pytest.raises(OSError, match="adapter busy"):
        await connect(proxy, ProxyCandidate(ADDR_A, -50, "network-id"))
    assert not proxy.connected
    assert proxy.client is None


# ----------------------------------------------------------------------------- StandaloneLink


async def test_link_reconnects_to_another_proxy_after_link_loss(
    proxy: ProxyClient,
    bleak_client: Any,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    network_id: bytes,
):
    scanner.adverts = [
        (device(ADDR_A), adv(b"\x00" + network_id, rssi=-40)),
        (device(ADDR_B), adv(b"\x00" + network_id, rssi=-80)),
    ]
    link = StandaloneLink(proxy, scan_seconds=0.01)
    assert link._task is None
    await link.start()
    try:
        await link.wait_connected(timeout=5)
        await until(
            lambda: link._current is not None
        )  # connected, and the loop has recorded where
        assert link._current == ADDR_A
        assert proxy.client is bleak_client.made[0]
        assert link._failed == {}
        # link loss: the loop wakes up, remembers A as failed and connects to the other proxy
        bleak_client.made[0].drop()
        assert not proxy.connected
        await until(lambda: proxy.connected and link._current == ADDR_B)
        assert set(link._failed) == {ADDR_A}
        assert proxy.client is bleak_client.made[1]
        assert fast.sleeps.count(0.5) == 1
        # second loss: both proxies failed recently → fall back to the full list, strongest first
        bleak_client.made[1].drop()
        await until(lambda: len(bleak_client.made) == 3 and link._current == ADDR_A)
        assert proxy.connected
        assert proxy.client is bleak_client.made[2]
        assert bleak_client.made[2].address == ADDR_A
        assert set(link._failed) == {ADDR_A, ADDR_B}
        assert all(s < 1 for s in fast.sleeps)  # never had to back off
    finally:
        await link.stop()
    assert proxy.client is None
    assert bleak_client.made[2].disconnect_calls == 1
    assert link._task is not None
    assert link._task.cancelled()


async def test_link_backs_off_while_nothing_is_reachable(
    proxy: ProxyClient,
    bleak_client: Any,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    network_id: bytes,
):
    link = StandaloneLink(proxy, scan_seconds=0.01)
    await link.start()
    try:
        backoffs = lambda: [s for s in fast.sleeps if s >= 1]  # noqa: E731
        await until(lambda: len(backoffs()) >= 3)  # "no proxies in range"
        assert backoffs()[:3] == [1.0, 2.0, 4.0]
        # a proxy shows up but refuses the connection: keep backing off, capped at 30 s
        scanner.adverts = [(device(ADDR_A), adv(b"\x00" + network_id, rssi=-40))]
        bleak_client.fail[ADDR_A] = OSError("refused")
        await until(lambda: len(backoffs()) >= 7)
        assert backoffs()[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        assert bleak_client.made
        assert not proxy.connected
        # the connection succeeds eventually…
        bleak_client.fail.clear()
        await link.wait_connected(timeout=5)
        await until(lambda: link._current is not None)
        assert link._current == ADDR_A
        n = len(backoffs())
        # …and the backoff starts from 1 s again after the next failure
        bleak_client.fail[ADDR_A] = OSError("refused again")
        bleak_client.made[-1].drop()
        await until(lambda: len(backoffs()) > n)
        assert backoffs()[n] == 1.0
    finally:
        await link.stop()


async def test_wait_connected_times_out(proxy: ProxyClient, fast: FastAsyncio):
    link = StandaloneLink(proxy)
    with pytest.raises(TimeoutError, match="no proxy connection"):
        await link.wait_connected(timeout=-1)
    assert fast.sleeps == []
    with pytest.raises(TimeoutError):
        await link.wait_connected(timeout=0.001)
    assert fast.sleeps
    assert set(fast.sleeps) == {0.2}
    await link.stop()  # never started: nothing to cancel, nothing to detach


async def test_scan_for_proxies_stops_the_scanner_when_cancelled(
    proxy: ProxyClient, scanner: type[FakeScanner]
):
    """`StandaloneLink.stop()` cancels `_run` inside this very sleep; a scanner left running keeps BlueZ
    discovering and refuses the next StartDiscovery from this process (`org.bluez.Error.InProgress`)."""
    task = asyncio.create_task(scan_for_proxies(proxy, seconds=60))
    await asyncio.sleep(0)
    sc = scanner.instances[0]
    assert sc.started
    assert not sc.stopped
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sc.stopped


async def test_link_reconnects_when_the_link_drops_during_attach(
    proxy: ProxyClient,
    bleak_client: Any,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    network_id: bytes,
    monkeypatch: pytest.MonkeyPatch,
):
    """The disconnect wake-up used to be cleared *after* `connect()` returned: a proxy that drops the link while
    `attach()` is still writing the filter (or in its 0.3 s settle) woke nobody, and the loop parked in
    `_wake.wait()` for ever with no link — `wait_connected` timed out although another proxy was in range."""
    scanner.adverts = [
        (device(ADDR_A), adv(b"\x00" + network_id, rssi=-40)),
        (device(ADDR_B), adv(b"\x00" + network_id, rssi=-80)),
    ]
    real_write = bleak_client.cls.write_gatt_char

    async def write_then_drop(self: Any, *args: Any, **kw: Any) -> None:
        await real_write(self, *args, **kw)
        if (
            self.address == ADDR_A
        ):  # the first proxy drops the link right after the filter write
            self.drop()

    monkeypatch.setattr(bleak_client.cls, "write_gatt_char", write_then_drop)
    link = StandaloneLink(proxy, scan_seconds=0.01)
    await link.start()
    try:
        await link.wait_connected(timeout=5)
        await until(lambda: link._current == ADDR_B)
        assert proxy.client is bleak_client.made[-1]
        assert (
            bleak_client.made[0].address == ADDR_A
        )  # tried first, dropped during attach…
        assert ADDR_A in link._failed  # …and remembered as failed
        assert not link._wake.is_set()
    finally:
        await link.stop()


async def test_link_sets_aside_a_proxy_that_refuses_the_connection(
    proxy: ProxyClient,
    bleak_client: Any,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    network_id: bytes,
):
    """A proxy whose `connect()` fails was never recorded in `_failed`, so the strongest one — occupied by the
    phone, say — was retried for ever while a weaker, working proxy was ignored."""
    scanner.adverts = [
        (device(ADDR_A), adv(b"\x00" + network_id, rssi=-40)),
        (device(ADDR_B), adv(b"\x00" + network_id, rssi=-80)),
    ]
    bleak_client.fail[ADDR_A] = OSError("le-connection-abort-by-local")
    link = StandaloneLink(proxy, scan_seconds=0.01)
    await link.start()
    try:
        await link.wait_connected(timeout=5)
        await until(lambda: link._current == ADDR_B)
        assert [c.address for c in bleak_client.made] == [ADDR_A, ADDR_B]
        assert set(link._failed) == {ADDR_A}
        assert [s for s in fast.sleeps if s >= 1] == [
            1.0
        ]  # one backoff, then the other proxy
    finally:
        await link.stop()


async def test_link_keeps_retrying_a_lone_proxy_that_refuses(
    proxy: ProxyClient,
    bleak_client: Any,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    network_id: bytes,
):
    scanner.adverts = [(device(ADDR_A), adv(b"\x00" + network_id, rssi=-40))]
    bleak_client.fail[ADDR_A] = OSError("busy")
    link = StandaloneLink(proxy, scan_seconds=0.01)
    await link.start()
    try:
        await until(
            lambda: len(bleak_client.made) >= 3
        )  # the `or cands` fallback keeps it retryable
        assert set(link._failed) == {ADDR_A}
        assert not proxy.connected
        bleak_client.fail.clear()
        await link.wait_connected(timeout=5)
    finally:
        await link.stop()


async def test_link_started_over_an_existing_connection_takes_over_on_loss(
    proxy: ProxyClient,
    bleak_client: Any,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    network_id: bytes,
):
    """`mesh_poc.py` may connect first and start the link afterwards: the loop then only waits for the loss, and
    a proxy it did not pick itself is not marked failed (it has no address for it) — it simply rescans."""
    scanner.adverts = [(device(ADDR_A), adv(b"\x00" + network_id, rssi=-40))]
    await connect(proxy, ProxyCandidate(ADDR_B, -50, "network-id"))
    assert proxy.connected
    link = StandaloneLink(proxy, scan_seconds=0.01)
    await link.start()
    try:
        for _ in range(3):  # let the loop reach its wait
            await asyncio.sleep(0)
        assert scanner.instances == []  # already connected: no scan
        assert len(bleak_client.made) == 1
        bleak_client.made[0].drop()
        await until(lambda: proxy.connected and link._current == ADDR_A)
        assert link._failed == {}
        assert fast.sleeps.count(0.5) == 1
    finally:
        await link.stop()


async def test_wait_connected_waits_for_the_filter(
    proxy: ProxyClient,
    bleak_client: Any,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    network_id: bytes,
    monkeypatch: pytest.MonkeyPatch,
):
    """CLI-08: `connected` turns True as soon as `attach()` has the client, before the notifications and the proxy
    filter are set up; `wait_connected` waits for the attach to finish (`ready`), or the first request after it
    could miss its reply or the group statuses the default filter drops."""
    scanner.adverts = [(device(ADDR_A), adv(b"\x00" + network_id, rssi=-40))]
    release = asyncio.Event()
    real_set_filter = ProxyClient.set_filter

    async def blocked_set_filter(self: ProxyClient, *a: Any, **kw: Any) -> None:
        await release.wait()
        await real_set_filter(self, *a, **kw)

    monkeypatch.setattr(ProxyClient, "set_filter", blocked_set_filter)
    link = StandaloneLink(proxy, scan_seconds=0.01)
    await link.start()
    try:
        await until(lambda: proxy.connected)
        assert not proxy.ready
        with pytest.raises(TimeoutError):
            await link.wait_connected(timeout=0.05)
        release.set()
        await link.wait_connected(timeout=5)
        assert proxy.ready
    finally:
        await link.stop()
    assert not proxy.ready


async def test_standalone_link_keeps_the_applications_disconnect_callback(
    proxy: ProxyClient, link: FakeBleak, recorder: Recorder
):
    """CLI-08: the link chains the disconnect callback the application gave the client instead of replacing it,
    and starting it twice runs one loop, not two."""
    standalone_link = StandaloneLink(proxy)
    await proxy.attach(link)
    proxy.handle_disconnected()
    assert recorder.disconnects == 1
    assert standalone_link._wake.is_set()


async def test_starting_a_link_twice_runs_one_loop(
    proxy: ProxyClient, monkeypatch: pytest.MonkeyPatch
):
    """CLI-08: a second `start()` while the loop runs is a no-op."""
    started: list[int] = []
    never = asyncio.Event()

    async def run(self: StandaloneLink) -> None:
        started.append(1)
        await never.wait()

    monkeypatch.setattr(StandaloneLink, "_run", run)
    link = StandaloneLink(proxy)
    await link.start()
    first = link._task
    await link.start()
    await asyncio.sleep(0)
    assert link._task is first
    assert started == [1]
    await link.stop()


async def test_a_link_over_a_client_without_a_disconnect_callback(
    cdb: CDB, state: Any, link: FakeBleak
):
    """CLI-08: with no application callback to chain, the link's own wake-up is all a disconnect does."""
    proxy = ProxyClient(cdb, state)
    standalone_link = StandaloneLink(proxy)
    await proxy.attach(link, filter_blacklist=False)
    proxy.handle_disconnected()
    assert standalone_link._wake.is_set()


async def test_a_silent_link_is_dropped_for_another(
    proxy: ProxyClient,
    bleak_client: Any,
    scanner: type[FakeScanner],
    fast: FastAsyncio,
    network_id: bytes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Review-3 T10: a proxy that stops forwarding never disconnects; a long CLI command sat on it for good."""
    monkeypatch.setattr(standalone, "SILENCE_TIMEOUT", 0.05)
    scanner.adverts = [
        (device(ADDR_A), adv(b"\x00" + network_id, rssi=-40)),
        (device(ADDR_B), adv(b"\x00" + network_id, rssi=-80)),
    ]
    link = StandaloneLink(proxy, scan_seconds=0.01)
    await link.start()
    try:
        await link.wait_connected(timeout=5)
        await until(lambda: link._current == ADDR_A)
        for _ in range(100):  # the watchdog waits on the real clock
            if len(bleak_client.made) >= 2 and link._current == ADDR_B:
                break
            await asyncio.sleep(0.01)
        assert link._current == ADDR_B
        assert "nothing from the proxy for" in caplog.text
    finally:
        await link.stop()


# ----------------------------------------------------------------------------- unprovisioned devices


def provisioning_adv(
    service_data: bytes | None,
    rssi: int = -60,
    manufacturer_data: dict[int, bytes] | None = None,
) -> Any:
    sd = {MESH_PROVISIONING_SERVICE: service_data} if service_data is not None else {}
    return SimpleNamespace(
        service_data=sd,
        rssi=rssi,
        local_name="JUNG",
        manufacturer_data=manufacturer_data,
    )


async def test_scan_unprovisioned(scanner: type[FakeScanner], fast: FastAsyncio):
    uuid = bytes.fromhex("30fb10fffe1234560000000000000000")
    scanner.adverts = [
        (device(ADDR_A), provisioning_adv(uuid + b"\x00\x00", rssi=-70)),
        (
            device(ADDR_B),
            provisioning_adv(
                uuid[:15] + b"\x01" + b"\x40\x00",
                rssi=-40,
                manufacturer_data={0x0527: bytes.fromhex("0101000000")},
            ),
        ),
        (
            device(ADDR_C),
            provisioning_adv(uuid),
        ),  # no OOB field: not PB-GATT service data
        (device("DD:DD:DD:DD:DD:DD"), provisioning_adv(None)),
    ]
    found = await standalone.scan_unprovisioned(0.01)
    assert fast.sleeps == [0.01]
    assert scanner.instances[0].service_uuids == [MESH_PROVISIONING_SERVICE]
    assert [(d.address, d.rssi, d.uuid, d.oob_info, d.product_id) for d in found] == [
        (ADDR_B, -40, "30FB10FF-FE12-3456-0000-000000000001", 0x4000, 0x0001),
        (ADDR_A, -70, "30FB10FF-FE12-3456-0000-000000000000", 0, None),
    ]
    assert found[0].device is scanner.adverts[1][0]
    assert found[0].name == "JUNG"


@pytest.fixture
def provisionee(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace bleak.BleakClient with a connectable `FakeDevice` (an unprovisioned node); ``made`` lists them."""
    made: list[Any] = []
    settings = SimpleNamespace(disconnect_error=None)

    class FakeProvisioneeClient(FakeDevice):
        def __init__(
            self, address_or_device: Any, timeout: float | None = None
        ) -> None:
            super().__init__(elements=2)
            self.given, self.timeout = address_or_device, timeout
            self.connected = False
            self.disconnects = 0
            made.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.disconnects += 1
            if settings.disconnect_error is not None:
                raise settings.disconnect_error

    monkeypatch.setattr(standalone, "BleakClient", FakeProvisioneeClient)
    return SimpleNamespace(made=made, settings=settings)


async def test_provision_device_connects_provisions_and_disconnects(provisionee: Any):
    data = ProvisioningData(net_key=bytes(range(16)), unicast=0x0D20)
    target = UnprovisionedDevice("30FB10FF-FE12-3456-0000-000000000000", 0, ADDR_A)
    result = await standalone.provision_device(target, data)
    client = provisionee.made[0]
    assert client.given == ADDR_A  # no BLEDevice from a scan: the address
    assert client.timeout == 15.0
    assert client.connected
    assert client.disconnects == 1
    assert client.data == data
    assert result.device_key == client.device_key
    assert result.elements == 2


async def test_provision_device_disconnects_after_a_failure(provisionee: Any):
    data = ProvisioningData(net_key=bytes(16), unicast=0x0D20)
    backend = device(ADDR_B)
    target = UnprovisionedDevice(
        "30FB10FF-FE12-3456-0000-000000000000", 0, ADDR_B, device=backend
    )

    def refuse(caps: Capabilities) -> None:
        raise ProvisioningError("no room")

    provisionee.settings.disconnect_error = OSError("already gone")  # only logged
    with pytest.raises(ProvisioningError, match="no room"):
        await standalone.provision_device(target, data, check=refuse)
    client = provisionee.made[0]
    assert client.given is backend
    assert client.disconnects == 1
