"""Standalone transport: scan + connect with plain bleak (local adapter), with a reconnect loop.

Home Assistant uses its own transport (see custom_components/junghome_ble/coordinator.py); this one is
for the CLI, tests and any non-HA host.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner

from .advert import parse_manufacturer_data
from .client import MESH_PROXY_SERVICE, ProxyCandidate, ProxyClient
from .provisioning import (
    DEFAULT_ATTENTION,
    MESH_PROVISIONING_SERVICE,
    PROTOCOL_TIMEOUT,
    Capabilities,
    ProvisioningData,
    ProvisioningResult,
    UnprovisionedDevice,
    parse_provisioning_service_data,
    provision,
)

__all__ = [
    "BEACON_WAIT",
    "SILENCE_TIMEOUT",
    "StandaloneLink",
    "connect",
    "provision_device",
    "scan_for_proxies",
    "scan_unprovisioned",
]

log = logging.getLogger("jhmesh.standalone")

FAILED_COOLDOWN = 60.0  # seconds a proxy that dropped or refused the link is passed over when another is in range
# a link that delivered nothing for this long is dropped and another proxy looked for (the hub waits as long,
# `LINK_IDLE_TIMEOUT`, before its keep-alive)
SILENCE_TIMEOUT = 660.0
# a JUNG proxy sends its Secure Network Beacon right after the subscription: the filter request waits up to this long
# for it, so it goes out under the network's current IV index rather than a stale stored one the proxy would drop (the
# hub waits as long, `CONNECT_BEACON_WAIT`)
BEACON_WAIT = 1.0


async def scan_for_proxies(
    proxy: ProxyClient, seconds: float = 4.0
) -> list[ProxyCandidate]:
    """Scan for `seconds` and return the proxy nodes of this network, strongest signal first."""
    found: dict[str, ProxyCandidate] = {}

    def cb(device: Any, adv: Any) -> None:
        r = proxy.classify_service_data(adv.service_data.get(MESH_PROXY_SERVICE, b""))
        if r:
            kind, node_addr = r
            name = adv.local_name
            if node_addr is not None:
                node = proxy.cdb.node_by_addr(node_addr)
                name = node.name if node else name
            found[device.address] = ProxyCandidate(
                device.address, adv.rssi, kind, node_addr, name, device, adv
            )

    # the context manager stops the scanner on the way out, also when the sleep is cancelled (`StandaloneLink.stop`
    # spends most of its life cancelling exactly this sleep): a scanner left running keeps BlueZ discovering with our
    # filter and answers the next StartDiscovery from this process with `org.bluez.Error.InProgress`
    async with BleakScanner(cb, service_uuids=[MESH_PROXY_SERVICE]):
        await asyncio.sleep(seconds)
    return sorted(found.values(), key=lambda c: -c.rssi)


async def connect(
    proxy: ProxyClient,
    candidate: ProxyCandidate | None = None,
    scan_seconds: float = 4.0,
    timeout: float = 15.0,
) -> ProxyCandidate:
    """Connect `proxy` to `candidate` (or the best one a scan finds) with plain bleak."""
    if candidate is None:
        cands = await scan_for_proxies(proxy, scan_seconds)
        if not cands:
            raise RuntimeError("no proxy node of this network in range")
        candidate = cands[0]
    log.info(
        "connecting to proxy %s (rssi %s, %s %s)",
        candidate.address,
        candidate.rssi,
        candidate.kind,
        candidate.name or "",
    )
    client = BleakClient(
        candidate.device or candidate.address,
        disconnected_callback=proxy.handle_disconnected,
        timeout=timeout,
    )
    await client.connect()
    await proxy.attach(client, beacon_wait=BEACON_WAIT)
    return candidate


async def scan_unprovisioned(seconds: float = 10.0) -> list[UnprovisionedDevice]:
    """Scan for `seconds` and return the devices advertising the Mesh Provisioning Service, strongest first.

    The Device UUID and OOB Information come from the 0x1827 service data; the product id from the JUNG
    manufacturer record, when the advertisement carries one (`advert.parse_manufacturer_data`).
    """
    found: dict[str, UnprovisionedDevice] = {}

    def cb(device: Any, adv: Any) -> None:
        parsed = parse_provisioning_service_data(
            adv.service_data.get(MESH_PROVISIONING_SERVICE, b"")
        )
        if parsed is None:
            return
        jung = parse_manufacturer_data(getattr(adv, "manufacturer_data", None) or {})
        found[device.address] = UnprovisionedDevice(
            uuid=parsed[0],
            oob_info=parsed[1],
            address=device.address,
            rssi=adv.rssi,
            name=adv.local_name,
            product_id=jung.product_id if jung is not None else None,
            device=device,
        )

    async with BleakScanner(cb, service_uuids=[MESH_PROVISIONING_SERVICE]):
        await asyncio.sleep(seconds)
    return sorted(found.values(), key=lambda d: -d.rssi)


async def provision_device(
    device: UnprovisionedDevice,
    data: ProvisioningData,
    *,
    attention: int = DEFAULT_ATTENTION,
    check: Callable[[Capabilities], None] | None = None,
    timeout: float = 15.0,
    protocol_timeout: float = PROTOCOL_TIMEOUT,
) -> ProvisioningResult:
    """Connect to an unprovisioned `device` with plain bleak, provision it (`provisioning.provision`), disconnect.

    The link is closed on every path: after Complete the node restarts as a proxy node anyway, and closing is
    how a provisioner aborts a failed session.
    """
    client = BleakClient(device.device or device.address, timeout=timeout)
    await client.connect()
    try:
        return await provision(
            client, data, attention=attention, timeout=protocol_timeout, check=check
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("disconnect after provisioning failed", exc_info=True)


class StandaloneLink:
    """Keeps a ProxyClient connected: reconnects to the best other proxy node after a link loss."""

    def __init__(self, proxy: ProxyClient, scan_seconds: float = 4.0) -> None:
        """Wrap `proxy`; the link wakes up to reconnect whenever it reports a disconnect."""
        self.proxy = proxy
        self.scan_seconds = scan_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._failed: dict[str, float] = {}
        self._current: str | None = None
        previous = (
            proxy.on_disconnect
        )  # the application's own callback: chained, not replaced

        def on_disconnect() -> None:
            self._wake.set()
            if previous is not None:
                previous()

        proxy.on_disconnect = on_disconnect

    async def start(self) -> None:
        """Start the background (re)connect loop; a no-op while it already runs."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the loop and detach the proxy client."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.proxy.detach()

    async def wait_connected(self, timeout: float = 60.0) -> None:
        """Wait until the proxy is connected and set up (`ProxyClient.ready`); TimeoutError after `timeout` seconds."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not self.proxy.ready:
            if loop.time() > end:
                raise TimeoutError("no proxy connection")
            await asyncio.sleep(0.2)

    async def _watch_silence(self) -> None:
        """Wait for the link to go; drop it when the proxy has delivered nothing for SILENCE_TIMEOUT.

        A GATT proxy that stops forwarding never disconnects by itself: a long-running CLI command (a sniff, a
        monitor) would sit on a dead link for good. Silence is judged by `ProxyClient.last_rx` — every proxy PDU,
        beacons included.
        """
        while True:  # a lost link wakes the wait below (`on_disconnect`)
            idle = time.monotonic() - self.proxy.last_rx
            if idle >= SILENCE_TIMEOUT:
                log.warning(
                    "nothing from the proxy for %.0fs: dropping the link to find another",
                    idle,
                )
                await self.proxy.detach()
                return
            try:
                await asyncio.wait_for(self._wake.wait(), SILENCE_TIMEOUT - idle)
                return
            except TimeoutError:
                continue

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            # cleared *before* connecting: a link that drops while `attach()` is still running (the filter write, the
            # 0.3 s settle) sets the event during `connect()`; clearing afterwards swallowed that wake-up and parked
            # the loop in `wait()` for good, with no link and no further scan
            self._wake.clear()
            if not self.proxy.connected:
                candidate: ProxyCandidate | None = None
                try:
                    cands = await scan_for_proxies(self.proxy, self.scan_seconds)
                    now = asyncio.get_running_loop().time()
                    cands = [
                        c
                        for c in cands
                        if now - self._failed.get(c.address, -999) > FAILED_COOLDOWN
                    ] or cands
                    if not cands:
                        raise RuntimeError("no proxies in range")
                    candidate = cands[0]
                    await connect(self.proxy, candidate)
                    self._current = candidate.address
                    backoff = 1.0
                except Exception as e:
                    if candidate is not None:
                        # a proxy that refuses the connection (occupied by the phone, a persistent abort) is set
                        # aside like one that dropped the link, so the next scan prefers the others; the `or cands`
                        # fallback above keeps a lone proxy retryable
                        self._failed[candidate.address] = (
                            asyncio.get_running_loop().time()
                        )
                    log.warning("connect failed: %s; retry in %.0fs", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
            if self.proxy.connected:
                await self._watch_silence()
            if self._current:
                self._failed[self._current] = asyncio.get_running_loop().time()
            await asyncio.sleep(0.5)
