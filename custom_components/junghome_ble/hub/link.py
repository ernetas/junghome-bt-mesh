"""The proxy link of one hub: proxy choice, connection, watchdog, keep-alive, Filter Status watch, grace (A4-3).

`connection_loop` keeps a link: it picks the strongest proxy node of this mesh in range (`visible_proxies`,
`ours`), connects (`_connect_to`, through bleak-retry-connector), watches the link (`_watch_link`: a silent proxy
is asked with a keep-alive Get, `_keep_alive`, and dropped when that goes unanswered too) and connects again with a
back-off when it goes (`_judge_link`). Every end of a link goes through `_link_ended`, which records it
(`JungHomeHub.link_history`), starts the grace that keeps the entities available for LINK_LOSS_GRACE
(`link_available`, `wait_for_link`) and tells the listeners (`async_on_link_loss`). The proxy client reports a
lost transport (`on_disconnect`) and the proxy's Filter Status (`on_filter_status`) here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Protocol

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from custom_components.junghome_ble.const import (
    CONNECT_BACKOFF_MAX,
    CONNECT_BACKOFF_MIN,
    CONNECT_BEACON_WAIT,
    DOMAIN,
    FAILED_PROXY_COOLDOWN,
    FILTER_STATUS_TIMEOUT,
    KEEP_ALIVE_ATTEMPTS,
    KEEP_ALIVE_TIMEOUT,
    LINK_BLUETOOTH_OFF,
    LINK_CONNECTING,
    LINK_DISCONNECTED,
    LINK_FAILED,
    LINK_IDLE_TIMEOUT,
    LINK_LOSS_GRACE,
    LINK_SEARCHING,
    LINK_UPDATING,
    PROXY_ADVERT_MAX_AGE,
    SHORT_LINK,
    SHORT_LINK_STREAK,
    SIGNAL_CONNECTION,
    SIGNAL_LINK_STATE,
    STOP_TIMEOUT,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.advert import parse_manufacturer_data
from custom_components.junghome_ble.jhmesh.client import MESH_PROXY_SERVICE
from custom_components.junghome_ble.jhmesh.devices import BATTERY_PIDS
from custom_components.junghome_ble.protocols import HubPort

from .lifecycle import Backoff

if TYPE_CHECKING:
    from collections import deque

    from custom_components.junghome_ble.inserts import NodeInserts
    from custom_components.junghome_ble.vault_refresh import VaultKeyRefresh

    from .energy import Energy
    from .export_watch import ExportWatch
    from .issues import Issues
    from .liveness import Liveness
    from .refresh import Refresh


class LinkHub(HubPort, Protocol):
    """What the link asks of the hub besides `HubPort`: the parts it hands each link to, and the link's record."""

    link_count: int
    link_state: str
    link_history: deque[LinkRecord]

    @property
    def energy(self) -> Energy:
        """The metered loads' readings and polls."""

    @property
    def export_watch(self) -> ExportWatch:
        """The unknown nodes, the export refresh and the gateway's trust."""

    @property
    def inserts(self) -> NodeInserts:
        """Each node's insert and key layout."""

    @property
    def issues(self) -> Issues:
        """The repair issues."""

    @property
    def liveness(self) -> Liveness:
        """The nodes' reachability and heartbeats."""

    @property
    def refresh(self) -> Refresh:
        """The connect-time reads of every link."""

    @property
    def vault_refresh(self) -> VaultKeyRefresh:
        """The vault's devices taken through the app's key refresh."""


_LOGGER = logging.getLogger(__name__)

# SIG model id of a Generic OnOff Server, as the export lists it: every element that hosts one answers an OnOff Get
# (loads, sockets, actuator channels, detectors, thermostats), which makes it a keep-alive target for the watchdog.
GENERIC_ONOFF_SERVER = "1000"


@dataclass(frozen=True)
class LinkEnd:
    """How a proxy link ended (`_link_ended`): why, what it says about the proxy, how long it lasted.

    `penalise` True counts the end against the proxy (it went silent), False not at all (we ended a working link
    ourselves: the repair's skip-ahead, an error of our own), None by how long the link lasted (`SHORT_LINK`).
    """

    reason: str
    penalise: bool | None
    lasted: float  # seconds the link was up


# what `_link_end` holds while no link is up: before the first, and while one is being set up
NO_LINK = LinkEnd("no link yet", False, 0.0)
LINK_HISTORY = 20  # links the diagnostics describe (`JungHomeHub.link_history`)


@dataclass(frozen=True)
class LinkRecord:
    """One past link, for the diagnostics (review-4 R I-9: nothing told why the links of the last hours ended).

    The proxy is named by its mesh address only, never by its Bluetooth MAC.
    """

    proxy_node: (
        int | None
    )  # the proxy's unicast address, None when it never named itself
    ended: float  # monotonic time the link ended
    lasted: float  # seconds the link was up
    reason: str
    penalise: bool | None  # as in `LinkEnd`
    refresh: (
        float | None
    )  # seconds from link-up to the end of its state refresh, None when it never got through
    held_back: float  # seconds sends were held back for the sequence-number store during the link


class LinkManager:
    """The proxy link of one hub (module docstring)."""

    def __init__(self, hub: LinkHub) -> None:
        """Bind to `hub` (its proxy client, entry and components); no link yet."""
        self.hub = hub
        # its timers and the connection loop's task are the hub's (`JungHomeHub.lifecycle`): `grace`,
        # `filter_watch`, `adv` (`adv_seen`), and `link` (`connection_loop`)
        self._lifecycle = hub.lifecycle
        # set with `connected_since`, cleared when the link goes down
        self._link_up = asyncio.Event()
        self._connect_failure_logged = False  # the first failure of a link-down period is a WARNING, the rest DEBUG
        self._link_lost = asyncio.Event()
        self._was_available = False
        self.last_rx = time.monotonic()  # when the proxy last forwarded anything we could decode, or named itself (link watchdog)
        self._lost_at: float | None = (
            None  # monotonic time the last link was lost, while no new one is up
        )
        # how the last link ended, None while one is up (`_link_ended`); when the current one came up (monotonic)
        self._link_end: LinkEnd | None = NO_LINK
        self._link_since = 0.0
        # how the link before the current one ended, for the connect-time steps (`Refresh.connect_step`)
        self.previous_link = NO_LINK
        # proxy MAC → its links in a row that ended within SHORT_LINK (`_judge_link`)
        self._short_links: dict[str, int] = {}
        self._link_loss_listeners: list[Callable[[LinkEnd], None]] = []
        # for the current link: how long its state refresh took (None until it is through, `Refresh.after_connect`)
        # and the store's `held_back_total` when it came up (`JungHomeHub.link_history`)
        self.link_refresh: float | None = None
        self._held_back_at_link = 0.0
        self.probe_link = (
            asyncio.Event()
        )  # a command went unanswered: the watchdog asks the proxy now
        # load commands that went unanswered, waiting for the probe's verdict on the link (`JungHomeHub._load_command`):
        # (element the miss is accounted to, its state Get's kind, when it was asked)
        self.unanswered: list[tuple[int, str, float]] = []

    @property
    def link_since(self) -> float:
        """When the current (or last) link came up, a `time.monotonic()`: a read made since is this link's."""
        return self._link_since

    @property
    def link_available(self) -> bool:
        """Whether the entities count as reachable: a link is up, or one was lost less than LINK_LOSS_GRACE ago.

        A lost link is usually replaced within seconds by the next proxy node; flapping every entity to unavailable
        and back for that (and failing a command sent meanwhile) is worse than a short wait (`JungHomeHub._command`). A link
        counts once `_connect_to` took it: `connected` turns True inside `attach()` already, and a link lost before
        that is a failed connection, not one to show as up for a moment (review-4 R4-3).
        """
        if self.hub.connected and self._link_end is None:
            return True
        return (
            self._lost_at is not None
            and not self.hub.stopping
            and time.monotonic() - self._lost_at < LINK_LOSS_GRACE
        )

    async def async_wait_connected(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a proxy link; whether one is up.

        The event is re-cleared before each wait rather than trusted: the proxy client drops `connected` in its
        disconnect callback a moment before `_set_available(False)` clears the event, so a still-set event
        must not end the wait while there is no link.
        """
        try:
            async with asyncio.timeout(timeout):
                while not self.hub.connected:
                    self._link_up.clear()
                    await self._link_up.wait()
        except TimeoutError:
            return False
        return True

    def visible_proxies(self) -> list[bluetooth.BluetoothServiceInfoBleak]:
        """Proxy nodes of *this* network currently advertising, strongest first.

        Those heard within PROXY_ADVERT_MAX_AGE come first (review-3 C5): a node switched off keeps its last,
        possibly strongest, advertisement in the history for a long time, and connecting to it costs a timeout.
        """
        out = [
            info
            for info in bluetooth.async_discovered_service_info(
                self.hub.hass, connectable=True
            )
            if self.ours(info)
        ]
        now = bluetooth.MONOTONIC_TIME()
        return sorted(
            out,
            key=lambda i: (now - i.time > PROXY_ADVERT_MAX_AGE, -(i.rssi or -127)),
        )

    @callback
    def adv_seen(
        self,
        info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Take a proxy advert: wake the loop for a candidate of this mesh; note the node's signal and inserts."""
        if not self.hub.proxy.connected and self.ours(info):
            self._link_lost.set()  # wake the loop: a candidate appeared
        if (node := self.hub.node_for_address(info.address)) is not None:
            self.hub.node_rssi[node.unicast] = info.rssi
            self.hub.signal_node(node.unicast)
            # its JUNG record, merged into the proxy advert's data: insert and key layout (`inserts.py`)
            self.hub.inserts.note_advert(
                node, parse_manufacturer_data(info.manufacturer_data)
            )
        self.hub.export_watch.check_unknown_node(info)

    def ours(self, info: bluetooth.BluetoothServiceInfoBleak) -> bool:
        """Whether a proxy advert is of *this* network (Network ID or one of our nodes' Node Identity).

        Only such an advert wakes the connection loop while unlinked (review-4 R4-8): every other network's proxies
        in range woke it before, each wake-up a `visible_proxies` pass over every advert HA holds, to find nothing.
        """
        if not (sd := info.service_data.get(MESH_PROXY_SERVICE)):
            return False
        return self.hub.proxy.classify_service_data(bytes(sd)) is not None

    async def connection_loop(self) -> None:
        """Keep a link: connect to the best proxy node in range, watch it, and connect again when it goes.

        An unexpected error in one pass is logged and the loop goes on after a pause (review-3 C6: it used to end
        the task, and with it every link until a reload).
        """
        failed: dict[str, float] = {}
        # shared with `_connection_pass`, which doubles it on a failure or a short link and resets it after a long one
        backoff = Backoff(CONNECT_BACKOFF_MIN, CONNECT_BACKOFF_MAX)
        while not self.hub.stopping:
            try:
                await self._connection_pass(failed, backoff)
            except Exception:
                _LOGGER.exception(
                    "Unexpected error in the JUNG mesh connection loop; trying again in %.0f s",
                    CONNECT_BACKOFF_MAX,
                )
                await self.drop_link(
                    "an unexpected error in the connection loop", penalise=False
                )
                self._set_available(False)
                self.set_link_state(LINK_FAILED)
                await asyncio.sleep(CONNECT_BACKOFF_MAX)

    async def _connection_pass(
        self, failed: dict[str, float], backoff: Backoff
    ) -> None:
        """One pass of `connection_loop`: wait for a candidate, connect, watch the link until it goes."""
        cands = self.visible_proxies()
        now = time.monotonic()
        cands = [
            c
            for c in cands
            if now - failed.get(c.address, -FAILED_PROXY_COOLDOWN)
            > FAILED_PROXY_COOLDOWN
        ] or cands
        if not cands:
            self._set_available(False)
            self._check_bluetooth()
            self._link_lost.clear()
            try:
                await asyncio.wait_for(self._link_lost.wait(), 30)
            except TimeoutError:
                pass
            return
        info = cands[0]
        self.hub.issues.report_bluetooth_unavailable(
            False
        )  # a proxy node was seen: something hears the mesh
        self.set_link_state(LINK_CONNECTING)
        try:
            await self._connect_to(info)
        except Exception as err:  # any BLE failure: try the next node
            failed[info.address] = time.monotonic()
            # the first failure of a down period is what the user gets to see (a link that never comes up —
            # no free connection slot on the ESPHome proxy, an adapter gone — would otherwise leave every
            # entity unavailable with nothing in the log); the retries are DEBUG
            _LOGGER.log(
                logging.DEBUG if self._connect_failure_logged else logging.WARNING,
                "connecting to %s failed: %s; retry in %.0fs",
                info.address,
                err,
                backoff.delay,
            )
            self._connect_failure_logged = True
            self._set_available(False)
            self.set_link_state(LINK_FAILED)
            await asyncio.sleep(backoff.delay)
            backoff.grow()
            return
        self._link_lost.clear()
        await self._watch_link()
        if self._link_end is None:
            # gone without the disconnected callback (a transport that only turned `is_connected` False): the
            # client is still attached and nothing ended the link yet
            await self.drop_link("the transport reported it closed", penalise=None)
        self._set_available(False)
        await asyncio.sleep(self._judge_link(info.address, failed, backoff))

    def _judge_link(
        self, address: str, failed: dict[str, float], backoff: Backoff
    ) -> float:
        """Weigh the link to `address` that just ended (`_link_end`) against its proxy; the pause before the next pass.

        A link lost within SHORT_LINK is a failed connection that only took longer to show (review-4 R4-1): the
        back-off doubles, and after SHORT_LINK_STREAK of them in a row the node is set aside like one that cannot be
        connected to (`failed`), so the next pass prefers another node — the strongest one was otherwise picked
        again and again, each new link restarting the connect-time refresh. Only a long link resets the back-off.
        A silent proxy is set aside at once, as before; a link we ended for reasons of our own counts for nothing.
        A node that reached the streak is set aside again by its next short link, until it holds one for SHORT_LINK.
        Unverified on air.
        """
        end = self._link_end
        assert end is not None  # `_connection_pass` ended it
        if end.penalise is False:
            return 1.0
        now = time.monotonic()
        if end.penalise:  # a proxy that went silent: prefer another node for a while
            failed[address] = now
        if end.lasted >= SHORT_LINK:
            self._short_links.pop(address, None)
            backoff.reset()
            return 1.0
        streak = self._short_links[address] = self._short_links.get(address, 0) + 1
        if streak >= SHORT_LINK_STREAK:
            failed[address] = now
            if streak == SHORT_LINK_STREAK:
                _LOGGER.warning(
                    "Proxy node %s lost %d links in a row within %.0f s of connecting; preferring another node "
                    "for a while",
                    address,
                    streak,
                    SHORT_LINK,
                )
        return backoff.grow()

    async def _watch_link(self) -> None:
        """Block while the link is up; drop it (`drop_link`) when the proxy went silent.

        A GATT proxy that stops forwarding (stuck filter, half-dead relay) never disconnects by itself. A mesh with a
        gateway is never quiet (it polls every load every 15 s), but one without can be silent for hours at night, so
        LINK_IDLE_TIMEOUT of silence only triggers a keep-alive Get (`_keep_alive`); the link is dropped when that
        goes unanswered as well. A link that went away while the keep-alive was out was lost, not silent.

        A probe a load command asked for (`JungHomeHub._load_command`) also settles whether the nodes that left their commands
        unanswered are unreachable: only when the proxy answered it is the silence theirs.
        """
        self.unanswered.clear()  # misses of an earlier link: the new link's refresh asks those nodes again
        while self.hub.proxy.connected and not self._link_lost.is_set():
            idle = time.monotonic() - self.last_rx
            if idle >= LINK_IDLE_TIMEOUT:
                if await self._keep_alive():
                    continue
                if self._link_lost.is_set():
                    break
                _LOGGER.warning(
                    "Nothing received from the JUNG mesh through proxy node %s for %.0f s and no answer to a "
                    "keep-alive Get; dropping the link",
                    self.hub.proxy_address,
                    time.monotonic() - self.last_rx,
                )
                await self.drop_link("the proxy went silent", penalise=True)
                return
            if self.probe_link.is_set():
                self.probe_link.clear()
                unanswered, self.unanswered = self.unanswered, []
                # a command went unanswered: ask the proxy now rather than after LINK_IDLE_TIMEOUT of silence
                if await self._keep_alive():
                    for address, kind, asked in unanswered:
                        self.hub.liveness.missed_answer(
                            address, kind, asked, command=True
                        )
                    continue
                if not self._link_lost.is_set():
                    _LOGGER.warning(
                        "No answer from the JUNG mesh through proxy node %s to a command nor to a keep-alive "
                        "Get; dropping the link",
                        self.hub.proxy_address,
                    )
                    await self.drop_link(
                        "the proxy answered neither a command nor a keep-alive Get",
                        penalise=True,
                    )
                    return
                continue
            lost = asyncio.ensure_future(self._link_lost.wait())
            probe = asyncio.ensure_future(self.probe_link.wait())
            try:
                await asyncio.wait(
                    (lost, probe),
                    timeout=LINK_IDLE_TIMEOUT - idle,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                lost.cancel()
                probe.cancel()

    def _keep_alive_targets(self) -> list[int]:
        """One Generic OnOff Server element per node, nodes heard from before those never heard, the proxy's last.

        An answer from another node travels the mesh through the proxy, which proves it still forwards in both
        directions; the proxy node's own element only proves the GATT link (better than nothing on a mesh where
        it is the only load). Nodes that cannot answer are left out (review-3 C4: a healthy quiet link was dropped
        for asking an unplugged node, a dead one or a sleeping battery transmitter three times) — unless nothing
        else is left, when any element is better than none.
        """
        others: list[tuple[bool, int]] = []
        own: list[int] = []
        fallback: list[int] = []
        for node in self.hub.cdb.nodes:
            if node.pid is None:
                continue  # the phone / another client: not a device
            element = next(
                (e for e in node.elements if GENERIC_ONOFF_SERVER in e.models), None
            )
            if element is None:
                continue
            fallback.append(element.address)
            if node.pid in BATTERY_PIDS or not self.hub.node_alive(node.unicast):
                continue
            if node.unicast == self.hub.proxy_node:
                own.append(element.address)
            else:
                others.append(
                    (node.unicast not in self.hub.last_heard, element.address)
                )
        others.sort(
            key=lambda target: target[0]
        )  # stable: export order within each group
        return [address for _, address in others] + own or fallback

    async def _keep_alive(self) -> bool:
        """Ask a node for its state to tell a quiet mesh from a dead link; True when the proxy delivered anything.

        A Get is the one message JUNG firmware always answers (`Refresh._refresh_all`), so an unanswered keep-alive means
        the proxy no longer forwards (or the element is gone: up to KEEP_ALIVE_ATTEMPTS distinct elements are
        tried). Traffic of any kind arriving meanwhile counts as well. A send the sequence-number store holds
        back is waited for (`while_seq_stalls`), not taken for a dead link; one that cannot go out at all (the
        store refused past SEQ_STALL_DEADLINE, the sequence space is used up) is no verdict either way, so what
        arrived since decides alone — with nothing, the watchdog drops a silent proxy as after any unanswered
        keep-alive.
        """
        before = self.last_rx
        for addr in self._keep_alive_targets()[:KEEP_ALIVE_ATTEMPTS]:
            asked = time.monotonic()
            try:
                await self.hub.while_seq_stalls(
                    partial(
                        self.hub.proxy.request,
                        addr,
                        M.generic_onoff_get(),
                        M.GEN_ONOFF_STATUS,
                        timeout=KEEP_ALIVE_TIMEOUT,
                        retries=1,
                    )
                )
            except TimeoutError:
                _LOGGER.debug("%04X did not answer the keep-alive Get", addr)
                # an OnOff Get, like the refresh of a switch; one attempt is no verdict on the node
                self.hub.liveness.missed_answer(addr, "switch", asked, full=False)
                if self.last_rx != before:
                    return True  # not that element, but the proxy forwarded something else meanwhile
                continue
            except ConnectionError as err:
                _LOGGER.debug("keep-alive not sent: %s", err)
                break
            return True
        return self.last_rx != before

    def set_link_state(self, state: str) -> None:
        """Record the link's state (one of `LINK_STATES`) and tell the link state sensor when it changed.

        Every change is one DEBUG line of `key=value` fields (review-4 A4-14), so a log can be searched by them.
        """
        if state == self.hub.link_state:
            return
        _LOGGER.debug(
            "link_state from=%s to=%s link=%d proxy_node=%s",
            self.hub.link_state,
            state,
            self.hub.link_count,
            "-" if self.hub.proxy_node is None else f"{self.hub.proxy_node:04X}",
        )
        self.hub.link_state = state
        async_dispatcher_send(
            self.hub.hass, SIGNAL_LINK_STATE.format(self.hub.entry.entry_id)
        )

    def _check_bluetooth(self) -> None:
        """No proxy node in range: tell a mesh out of range from a Home Assistant that has no Bluetooth left.

        The app locks its screen while the phone's Bluetooth is off (`ObserveBluetoothState`); here the equivalent
        is no connectable scanner at all — the adapter is off, unplugged or failed, every ESPHome proxy is gone —
        which raises `bluetooth_unavailable` (`Issues.report_bluetooth_unavailable`).
        """
        off = bluetooth.async_scanner_count(self.hub.hass, connectable=True) == 0
        self.hub.issues.report_bluetooth_unavailable(off)
        self.set_link_state(LINK_BLUETOOTH_OFF if off else LINK_SEARCHING)

    async def _connect_to(self, info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Connect to the proxy node `info` advertised from and start the link's work.

        The wait for the connection is bleak-retry-connector's (up to `max_attempts` attempts of its own timeout),
        not the app's 5 s (`ConnectToDevice`): the app's phone radio connects at once or not at all, while an ESPHome
        proxy first waits for a free connection slot and gives up only after its own establishment timeout (at least
        10 s); cutting that short would turn a slow proxy into a failed one and rotate away from the only node in
        range. A failed attempt is retried by the connection loop with a back-off either way.
        """
        ble_device = (
            bluetooth.async_ble_device_from_address(
                self.hub.hass, info.address, connectable=True
            )
            or info.device
        )
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            f"JUNG proxy {info.address}",
            disconnected_callback=self.hub.proxy.handle_disconnected,
            max_attempts=2,
            use_services_cache=True,
        )
        # the link's counts start over in the attach (`proxy.link_stats`)
        self.hub.beacon_authenticated = False
        # counted before the attach: `proxy.connected` turns True inside it, and an entity added right then must
        # already see the new link's number (`config_entities.PropertyEntity._maybe_read`)
        self.hub.link_count += 1
        await self.hub.proxy.attach(client, beacon_wait=CONNECT_BEACON_WAIT)
        if not self.hub.proxy.connected:
            # lost while attach() settled after the filter request (review-4 R4-3): a failed connection, not a link
            # to report as up for a moment and then as lost — the entities would flap
            raise ConnectionError("the link was lost while it was set up")
        self.previous_link = self._link_end or NO_LINK
        self._link_end = None
        self._link_since = time.monotonic()
        self.link_refresh = None
        self._held_back_at_link = self.hub.state.held_back_total
        self._connect_failure_logged = False
        self.hub.proxy_address = info.address
        # every node gets a full timeout from here, and no re-ask stays pending
        self.hub.liveness.link_up()
        # the proxy's Filter Status names the node a little after the filter request (`on_filter_status`); its
        # Bluetooth address usually names it already (JUNG nodes advertise from their MAC, `node_for_address`)
        node = self.hub.node_for_address(info.address)
        self.hub.proxy_node = self.hub.proxy.proxy_addr or (
            node.unicast if node is not None else None
        )
        self.hub.connected_since = time.time()
        self._lost_at = None
        self._cancel_grace()
        self._link_up.set()
        self.last_rx = time.monotonic()
        self._set_available(True)
        self.set_link_state(
            LINK_UPDATING
        )  # until the connect-time state refresh is through (`Refresh.after_connect`)
        self.cancel_refresh()  # a refresh still running from the previous link would keep polling through this one
        if (
            self.hub.proxy.proxy_addr is None
        ):  # the Filter Status itself is still due, whatever the address told us
            self._lifecycle.set_timer(
                "filter_watch",
                async_call_later(
                    self.hub.hass, FILTER_STATUS_TIMEOUT, self._filter_status_overdue
                ),
            )
        self.hub.energy.arm_poll()
        self._lifecycle.set_task(
            "refresh",
            self.hub.entry.async_create_background_task(
                self.hub.hass, self.hub.refresh.after_connect(), f"{DOMAIN} refresh"
            ),
        )
        self.hub.export_watch.check_pin()
        self.hub.export_watch.request_refresh()  # unknown nodes seen before this link (or during setup) are asked about now
        self.hub.vault_refresh.schedule()  # a device Home Assistant added that missed a key refresh step: again now

    def cancel_refresh(self) -> None:
        """Cancel the per-link background work: the connect-time refresh, a running energy poll, the Filter Status watchdog."""
        for name in ("refresh", "energy"):
            if (task := self._lifecycle.task(name)) is not None:
                task.cancel()
            self._lifecycle.set_task(name, None)
        self.cancel_filter_watch()

    def cancel_filter_watch(self) -> None:
        """Stop waiting for the proxy's Filter Status: it came, a new link is up, or the hub stops."""
        self._lifecycle.cancel_timer("filter_watch")

    @callback
    def _filter_status_overdue(self, _now: datetime) -> None:
        """FILTER_STATUS_TIMEOUT after attaching, no Filter Status yet: the proxy discards our PDUs.

        The filter request is the first PDU of every link and the one the proxy answers by itself. When its
        beacon authenticated (so the keys fit) but the status never came, the proxy dropped the request as a
        replay — a stale sequence number, or another client using our address — and the link stays on the default
        whitelist: nothing at all is forwarded, so the refresh-based detection (which needs other traffic) never
        fires. Seen on air with an address whose sequence numbers the nodes already knew higher.

        Nothing to report when the request never went out — no filter request written on this link (the store held
        every attempt back) — or while the store holds sends back: the proxy was not asked, and the repair's
        skip-ahead could not be written either (`seq_store_unwritable` reports that).
        """
        self._lifecycle.set_timer("filter_watch", None)
        if (
            not self.hub.connected
            or self.hub.proxy.proxy_addr is not None  # the status did arrive
            or not self.hub.beacon_authenticated
            or self.hub.proxy.filter_writes == 0
            or self.hub.state.stalled_for is not None
        ):
            return
        _LOGGER.warning(
            "Proxy node %s authenticated the mesh beacon but did not answer the proxy filter request within "
            "%.0f s: it discards our messages (stale sequence number, or address %04X is used by another client)",
            self.hub.proxy_address,
            FILTER_STATUS_TIMEOUT,
            self.hub.proxy.state.src,
        )
        self.hub.issues.report_pdus_dropped(True)

    def _set_available(self, available: bool) -> None:
        """Record the link state and tell the entities, unless it is "still unavailable" with nothing to clear.

        The no-proxy branch of the connection loop passes every 30 s for as long as the mesh is out of range: a
        signal each time would make every entity write its state twice a minute, for hours. A (re)connect always
        signals: the proxy the entities show may have changed.
        """
        changed = (
            available
            or available != self._was_available
            or self.hub.proxy_address is not None
            or self.hub.proxy_node is not None
        )
        if available != self._was_available:
            if available:
                _LOGGER.info(
                    "Connected to the JUNG mesh through proxy node %s",
                    self.hub.proxy_address,
                )
            else:
                _LOGGER.warning(
                    "Lost the connection to the JUNG mesh; reconnecting to another proxy node"
                )
            self._was_available = available
        if not available:
            self.hub.proxy_address = None
            self.hub.proxy_node = None
            self.hub.connected_since = None
            self._link_up.clear()
        if changed:
            async_dispatcher_send(
                self.hub.hass, SIGNAL_CONNECTION.format(self.hub.entry.entry_id)
            )

    def on_disconnect(self) -> None:
        """Handle the link the proxy client lost (its transport's disconnected callback)."""
        self._link_ended("the proxy disconnected", None)
        self._link_lost.set()

    async def drop_link(self, reason: str, *, penalise: bool | None) -> None:
        """End the current link ourselves, for `reason`; `penalise` as in `LinkEnd`.

        Every path that detaches a link it still had goes through here (review-4 R4-3: only a transport's
        disconnect used to start the link-loss grace, so a link the watchdog or the `pdus_dropped` repair dropped
        made every entity unavailable at once, and Home Assistant skipped them in a command meanwhile). The end is
        recorded — and the grace started — before the detach: `detach` clears `connected` at once and then waits
        for the transport, and a command arriving in that wait must find the grace and wait for the next link
        (`wait_for_link`) rather than fail. The detach is bounded like the one of `async_stop`. Unverified on air.
        """
        self._link_ended(reason, penalise)
        try:
            await asyncio.wait_for(self.hub.proxy.detach(), STOP_TIMEOUT)
        except TimeoutError:
            _LOGGER.warning(
                "The Bluetooth link did not close within %.0f s; leaving it",
                STOP_TIMEOUT,
            )
        except Exception:  # pragma: no cover - detach logs and swallows its own errors
            _LOGGER.debug("detach failed", exc_info=True)
        self._link_lost.set()  # the watchdog, when another task dropped the link

    def _link_ended(self, reason: str, penalise: bool | None) -> None:
        """Handle the end of a link, whoever ended it: record why (`link_history`), start the grace, tell the listeners.

        Nothing to do when no link is up: one lost while `attach()` was still settling is a failed connection
        (`_connect_to`), and a link ends once however many paths notice.
        """
        if self._link_end is not None:
            return
        now = time.monotonic()
        end = self._link_end = LinkEnd(reason, penalise, now - self._link_since)
        self.hub.link_history.append(
            LinkRecord(
                self.hub.proxy_node,
                now,
                end.lasted,
                reason,
                penalise,
                self.link_refresh,
                self.hub.state.held_back_total - self._held_back_at_link,
            )
        )
        self.cancel_refresh()
        self._start_grace()
        self.set_link_state(LINK_DISCONNECTED)
        _LOGGER.info(
            "The link through proxy node %s ended after %.0f s: %s",
            self.hub.proxy_address,
            end.lasted,
            reason,
        )
        for listener in list(self._link_loss_listeners):
            listener(end)

    @callback
    def async_on_link_loss(self, listener: Callable[[LinkEnd], None]) -> CALLBACK_TYPE:
        """Call `listener` with every link's `LinkEnd` the moment it ends, before the grace runs; returns the unsubscribe."""
        self._link_loss_listeners.append(listener)
        return partial(self._link_loss_listeners.remove, listener)

    def _cancel_grace(self) -> None:
        self._lifecycle.cancel_timer("grace")

    def _start_grace(self) -> None:
        """Keep the entities available for LINK_LOSS_GRACE after a link loss; tell them when it ends."""
        self._lost_at = time.monotonic()
        self._cancel_grace()

        @callback
        def ended(_now: datetime) -> None:
            self._lifecycle.set_timer("grace", None)
            async_dispatcher_send(
                self.hub.hass, SIGNAL_CONNECTION.format(self.hub.entry.entry_id)
            )

        self._lifecycle.set_timer(
            "grace", async_call_later(self.hub.hass, LINK_LOSS_GRACE, ended)
        )

    def on_filter_status(self, proxy_unicast: int) -> None:
        """Record which node of the mesh we talk through, now that the proxy's Filter Status named it.

        The status is the proxy talking to us, so it feeds the link watchdog like any decoded PDU; and the proxy
        accepted the request it answers, so our PDUs are not being discarded (any more).
        """
        self.last_rx = time.monotonic()
        self.cancel_filter_watch()
        if self.hub.issues.pdus_dropped:
            self.hub.issues.report_pdus_dropped(False)
        if self.hub.proxy_node == proxy_unicast:
            return
        self.hub.proxy_node = proxy_unicast
        _LOGGER.debug(
            "Proxy %s is mesh node %04X", self.hub.proxy_address, proxy_unicast
        )
        async_dispatcher_send(
            self.hub.hass, SIGNAL_CONNECTION.format(self.hub.entry.entry_id)
        )

    async def wait_for_link(self) -> None:
        """During the link-loss grace, wait for the new link: a command then goes out on it instead of failing."""
        if (
            not self.hub.connected
            and self._lost_at is not None
            and not self.hub.stopping
        ):
            remaining = LINK_LOSS_GRACE - (time.monotonic() - self._lost_at)
            if remaining > 0:
                await self.async_wait_connected(remaining)
