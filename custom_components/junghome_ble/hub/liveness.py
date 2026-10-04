"""Liveness of the nodes for one hub: reachability (the app's rule) and heartbeats.

A node is unreachable once a request asked with the app's full budget went unanswered (`missed_answer`), re-asked
after UNREACHABLE_RECHECK (a short probe, a node heard from meanwhile, a locked load) and every UNREACHABLE_REPROBE
while it stays so, and reachable again with the next message from it (`heard_from`). With the heartbeat option on,
the mains nodes publish Heartbeats to us (`configure_heartbeats`); a node silent past its deadline is dead
(`check_heartbeats`) until heard from again (`mark_alive`), and asked for its publication again (`_reprobe_dead`).
The hub feeds it every message and Heartbeat and asks it `node_alive`; entities, the diagnostics and the
configurator read it through the hub's delegations (`JungHomeHub.node_alive`, `unreachable`, `last_heard`, …).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Final

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from custom_components.junghome_ble.const import (
    CONF_HEARTBEATS_PUBLISHING,
    DEFAULT_HEARTBEATS,
    DOMAIN,
    OPTION_HEARTBEATS,
    REQUEST_ATTEMPTS,
    REQUEST_TIMEOUT,
    SIGNAL_REACHABILITY,
    SIGNAL_UPDATE,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh.devices import BATTERY_PIDS

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.cdb import Node
    from custom_components.junghome_ble.jhmesh.client import Heartbeat
    from custom_components.junghome_ble.protocols import HubPort

_LOGGER = logging.getLogger(__name__)


# Heartbeats (`OPTION_HEARTBEATS`, `docs/hidden-features.md` §4): Config Heartbeat Publication with this PeriodLog
# (2^(n-1) s) to our own address; a node is dead after HEARTBEAT_MISSED_BEATS periods (plus half a period of slack)
# without a beat or any other message; the check runs every HEARTBEAT_CHECK_INTERVAL and the publications are
# (re)configured at most every HEARTBEAT_RECONFIGURE_INTERVAL — they persist in the nodes (CountLog 0xFF).
HEARTBEAT_PERIOD_LOG: Final = 7  # 64 s
HEARTBEAT_MISSED_BEATS: Final = 3
HEARTBEAT_RECONFIGURE_INTERVAL: Final = 6 * 3600.0
HEARTBEAT_REPROBE_INTERVAL: Final = 120.0  # a node marked dead is asked again for heartbeats this often: a rebooted node lost its publication
# Per-node reachability, the app's rule (`MeshMessengerImpl$handleError$1`, `docs/gap-analysis/control-and-state.md`
# §1.2): a node is unreachable as soon as a request it was asked with the full budget (REQUEST_ATTEMPTS x
# REQUEST_TIMEOUT) goes unanswered — a state Get or a command — unless it was heard from meanwhile, and reachable
# again with any message from it. A shorter probe it missed (the link watchdog's one-attempt keep-alive) is no verdict:
# the element is asked again with a full-budget Get after UNREACHABLE_RECHECK seconds.
UNREACHABLE_RECHECK: Final = 60.0
# ... and an unreachable node is asked again this often while the link lasts: a breaker that was off for a few minutes
# must not leave its entities unavailable until the next link
UNREACHABLE_REPROBE: Final = 300.0


class Liveness:
    """The reachability and heartbeat state of one hub's nodes (module docstring)."""

    def __init__(self, hub: HubPort) -> None:
        """Bind to `hub` (its export, link and entry); every node alive, the heartbeat option read from the entry."""
        self.hub = hub
        # Option: node heartbeats — per-node liveness (`heartbeats`, `configure_heartbeats`, `node_alive`)
        self.heartbeats_enabled = bool(
            hub.entry.options.get(OPTION_HEARTBEATS, DEFAULT_HEARTBEATS)
        )
        self.heartbeats: dict[int, Heartbeat] = {}  # node unicast → its last Heartbeat
        self._reprobed_at: dict[
            int, float
        ] = {}  # dead node → when it was last asked for heartbeats again
        self.reprobe_task: asyncio.Task[None] | None = None
        self._alive_deadline: dict[
            int, float
        ] = {}  # node unicast → monotonic time after which it counts as dead
        self._dead_nodes: set[int] = set()
        # nodes that left a full-budget request unanswered (`missed_answer`): their entities are unavailable until
        # the node is heard from (`heard_from`)
        self.unreachable: set[int] = set()
        self.last_heard: dict[
            int, float
        ] = {}  # node unicast → monotonic time of its last message
        # node unicast → its pending re-ask; the hub's (`JungHomeHub.lifecycle`), as are the `heartbeats` timer
        # (`check_heartbeats`, armed by `JungHomeHub.async_start`) and task (a renewal of the publications)
        self._lifecycle = hub.lifecycle
        self.recheck = hub.lifecycle.keyed("recheck")
        # when the last heartbeat configuration round ended (monotonic, `configure_heartbeats`); the diagnostics show it
        self.configured_at: float | None = None

    def link_up(self) -> None:
        """Give every node a full timeout and drop the pending re-asks: a new link is up (`LinkManager._connect_to`)."""
        # silence while the link was down was the link's fault, not the nodes': every node gets a full timeout
        # from here (a dead node stays dead until heard from — `mark_alive` revives it)
        now = time.monotonic()
        for unicast in self._alive_deadline:
            self._alive_deadline[unicast] = now + self.heartbeat_timeout
        # ... and no re-ask pending (the refresh asks everyone); an unreachable node stays so until heard from
        for unicast in list(self.recheck):
            self._cancel_recheck(unicast)

    @property
    def heartbeat_nodes(self) -> list[Node]:
        """The nodes asked for heartbeats: every provisioned mains device (battery nodes sleep and would not beat)."""
        return [
            n
            for n in self.hub.cdb.nodes
            if n.pid is not None and n.pid not in BATTERY_PIDS
        ]

    @property
    def heartbeat_timeout(self) -> float:
        """Seconds without a beat (or any message) after which a node counts as dead."""
        period = C.heartbeat_period_seconds(HEARTBEAT_PERIOD_LOG)
        return period * (HEARTBEAT_MISSED_BEATS + 0.5)

    def node_alive(self, address: int) -> bool:
        """Whether the node owning element `address` counts as there: it answers its requests, and beats.

        Unreachable after one request asked with the app's full budget went unanswered (`missed_answer`, always
        on), dead after a heartbeat timeout (only with the heartbeat option); either ends with the next message from it.
        """
        if not self.unreachable and not (self.heartbeats_enabled and self._dead_nodes):
            return True
        node = self.hub.cdb.node_by_addr(address)
        if node is None:
            return True
        return node.unicast not in self.unreachable and not (
            self.heartbeats_enabled and node.unicast in self._dead_nodes
        )

    def missed_answer(
        self,
        address: int,
        kind: str,
        asked: float,
        *,
        full: bool = True,
        command: bool = False,
    ) -> None:
        """Take note that the element at `address` left a request sent at `asked` (`time.monotonic()`) unanswered.

        The app's rule (`MeshMessengerImpl$handleError$1`: a request timeout sets the device's failed-message counter
        to the unreachable mark at once): a `full` budget exhausted — REQUEST_ATTEMPTS attempts of a state Get or a
        load's command — marks the node unreachable, and its entities unavailable until it is heard from
        (`heard_from`). Two exceptions keep it: a node heard from since `asked` is there, only busy (the app
        completes a pending request on any User Property Status from the element, and resets the counter on any
        status, so the same node would not be unreachable there either); and a shorter probe (the link watchdog's
        one-attempt keep-alive) is no verdict — the element is asked again with a full-budget Get (its `kind`'s)
        after UNREACHABLE_RECHECK. An unreachable node is asked again every UNREACHABLE_REPROBE for as long as the
        link lasts (nothing else would ask it: its entities are unavailable, so nobody can operate them).

        Battery nodes sleep between key presses: they are never asked for their state, and a command they miss says
        nothing about whether their keys still work, so they are never marked (the app does not show them as "No
        connection" either).

        Nor is a load known to be locked (`load_locked`) for a `command` it left unanswered: a locked load may well
        ignore the Set, and is not gone for it — it is asked again with a state Get after
        UNREACHABLE_RECHECK, whose silence counts as any other. Whether a locked load answers a Set at all is
        unverified on air (`docs/hidden-features.md` §12).
        """
        node = self.hub.cdb.node_by_addr(address)
        if node is None or node.pid in BATTERY_PIDS:
            return
        if node.unicast in self.unreachable:
            self._schedule_recheck(node.unicast, address, kind, UNREACHABLE_REPROBE)
            return
        if (
            not full
            or self.last_heard.get(node.unicast, -1e9) >= asked
            or (command and self.hub.load_locked(address))
        ):
            self._schedule_recheck(node.unicast, address, kind, UNREACHABLE_RECHECK)
            return
        self._cancel_recheck(node.unicast)
        self.unreachable.add(node.unicast)
        _LOGGER.warning(
            "%s did not answer a request (%d attempts in %.0f s): marking it unavailable",
            node.name,
            REQUEST_ATTEMPTS,
            REQUEST_ATTEMPTS * REQUEST_TIMEOUT,
        )
        self._notify_node(node)
        self._schedule_recheck(node.unicast, address, kind, UNREACHABLE_REPROBE)

    def _schedule_recheck(
        self, unicast: int, address: int, kind: str, delay: float
    ) -> None:
        """Ask the node again (its element `address`, `kind`'s Get) after `delay`, unless a re-ask is pending."""
        if unicast not in self.recheck and not self.hub.stopping:
            self.recheck[unicast] = async_call_later(
                self.hub.hass,
                delay,
                partial(self._recheck_node, unicast, address, kind),
            )

    def _cancel_recheck(self, unicast: int) -> None:
        if (cancel := self.recheck.pop(unicast, None)) is not None:
            cancel()

    @callback
    def _recheck_node(
        self, unicast: int, address: int, kind: str, _now: datetime
    ) -> None:
        """Ask a node that missed a state Get again, unless it was heard from meanwhile or the link is down."""
        del self.recheck[unicast]
        if not self.hub.connected:
            return  # the new link's refresh asks every node again
        self.hub.entry.async_create_background_task(
            self.hub.hass,
            self.hub.async_refresh_element(
                address, kind, quiet=unicast in self.unreachable
            ),
            f"{DOMAIN} reachability {address:04X}",
        )

    def heard_from(self, address: int) -> None:
        """Take a message from the element at `address` (`JungHomeHub._on_message`): its node is there."""
        node = self.hub.cdb.node_by_addr(address)
        if node is None:
            return
        self.last_heard[node.unicast] = time.monotonic()
        self.hub.last_seen[node.unicast] = dt_util.utcnow()
        self.hub.signal_node(node.unicast)
        self._cancel_recheck(node.unicast)
        if node.unicast in self.unreachable:
            self.unreachable.discard(node.unicast)
            _LOGGER.info("%s is reachable again", node.name)
            self._notify_node(node)

    def heartbeat_age(self, node: Node) -> float | None:
        """Seconds since the node's last Heartbeat; None when none was seen since the start."""
        beat = self.heartbeats.get(node.unicast)
        return None if beat is None else time.monotonic() - beat.received

    async def configure_heartbeats(self) -> None:
        """Ask every mains node to publish Heartbeats to our address (`OPTION_HEARTBEATS`), at most every few hours.

        A Config Heartbeat Publication Set with the device key, one node at a time in the usual chunks; the
        publication persists in the node, so this is not repeated on every link — only when the last round is
        older than HEARTBEAT_RECONFIGURE_INTERVAL (a node that lost it, e.g. after a mains failure, gets it back
        then; until then any message it sends keeps it alive anyway). Every node gets a fresh deadline: silence
        counts only from here.
        """
        if not self.heartbeats_enabled:
            return
        now = time.monotonic()
        if (
            self.configured_at is not None
            and now - self.configured_at < HEARTBEAT_RECONFIGURE_INTERVAL
        ):
            return
        pdu = C.heartbeat_publication_set(
            self.hub.proxy.state.src, HEARTBEAT_PERIOD_LOG, ttl=self.hub.proxy.ttl
        )
        # before the first Set: any node may take it
        self._set_heartbeats_publishing({node.unicast for node in self.heartbeat_nodes})
        try:
            await self.hub.chunked(
                [
                    partial(self._set_heartbeat, node, pdu, "configure")
                    for node in self.heartbeat_nodes
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("heartbeat configuration aborted: %s", err)
            return
        self.configured_at = time.monotonic()

    async def async_disable_heartbeats(self) -> None:
        """Tell the mains nodes to stop publishing Heartbeats (the option was switched off).

        The nodes still to be told are `heartbeats_publishing` (every heartbeat node when nothing is recorded);
        each that confirms leaves the list, and the next link with the option off asks the rest again
        (`Refresh.after_connect`) — the publication has CountLog 0xFF, a node told nothing publishes forever.
        """
        pending = self.heartbeats_publishing
        nodes = [n for n in self.heartbeat_nodes if not pending or n.unicast in pending]
        pdu = C.heartbeat_publication_set(0x0000, C.HEARTBEAT_PERIOD_OFF, count_log=0)
        confirmed: set[int] = set()
        try:
            await self.hub.chunked(
                [
                    partial(self._disable_heartbeat, node, pdu, confirmed)
                    for node in nodes
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("disabling heartbeats aborted: %s", err)
        # what is left to tell: of the nodes asked, those that did not confirm (a node no longer in the export
        # cannot be asked, nor needs to be)
        self._set_heartbeats_publishing({n.unicast for n in nodes} - confirmed)

    async def _disable_heartbeat(
        self, node: Node, pdu: bytes, confirmed: set[int]
    ) -> None:
        if await self._set_heartbeat(node, pdu, "disable"):
            confirmed.add(node.unicast)

    @property
    def heartbeats_publishing(self) -> set[int]:
        """Nodes that may still publish heartbeats to us (`CONF_HEARTBEATS_PUBLISHING`, unicasts as hex)."""
        recorded = self.hub.entry.data.get(CONF_HEARTBEATS_PUBLISHING)
        if (
            recorded is True
        ):  # one flag, as an earlier 0.3.0 build wrote it: any node may
            return {node.unicast for node in self.heartbeat_nodes}
        return {int(address, 16) for address in recorded or []}

    def _set_heartbeats_publishing(self, nodes: set[int]) -> None:
        """Record which nodes may still publish heartbeats to us; not hub data, so this reloads nothing."""
        if (
            CONF_HEARTBEATS_PUBLISHING in self.hub.entry.data
            and self.heartbeats_publishing == nodes
        ):
            return
        self.hub.hass.config_entries.async_update_entry(
            self.hub.entry,
            data={
                **self.hub.entry.data,
                CONF_HEARTBEATS_PUBLISHING: [f"{n:04X}" for n in sorted(nodes)],
            },
        )

    async def _set_heartbeat(self, node: Node, pdu: bytes, what: str) -> bool:
        """Send one Heartbeat Publication Set; whether the node confirmed it."""
        try:
            reply = await self.hub.proxy.request_config(
                node.unicast, pdu, C.CONFIG_HEARTBEAT_PUBLICATION_STATUS, retries=1
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer the heartbeat %s", node.unicast, what)
            if what == "configure":
                # a node that did not even answer the Set (off, out of range) is the case liveness is for: it gets
                # a deadline like the others, counts as dead when it passes and is asked again every
                # HEARTBEAT_REPROBE_INTERVAL (`check_heartbeats`) — an answer then revives it. A node that did
                # answer earlier keeps its deadline (`setdefault`).
                self._alive_deadline.setdefault(
                    node.unicast, time.monotonic() + self.heartbeat_timeout
                )
            return False
        try:
            status = C.decode_heartbeat_publication_status(reply.params)
        except ValueError as err:
            _LOGGER.debug(
                "%04X: malformed Heartbeat Publication Status: %s", node.unicast, err
            )
            return False
        if not status.ok:
            _LOGGER.warning(
                "%s refused the heartbeat %s: %s", node.name, what, status.status_name
            )
            return False
        if what == "disable" and status.enabled:
            # a success status only says the Set was taken; what the node publishes now is in the rest of it
            _LOGGER.warning(
                "%s answered the heartbeat disable but still publishes to %04X",
                node.name,
                status.destination,
            )
            return False
        if what == "configure":
            self._alive_deadline[node.unicast] = (
                time.monotonic() + self.heartbeat_timeout
            )
        elif what == "reconfigure":
            self.mark_alive(node.unicast)  # it answered: back, and publishing again
        return True

    def _beats_stopped(self, node: Node, now: float) -> bool:
        """Whether the node beat since the current configuration but not for a whole timeout: its publication is gone although it talks."""
        beat = self.heartbeats.get(node.unicast)
        configured = self.configured_at
        return (
            beat is not None
            and configured is not None
            and beat.received >= configured
            and now - beat.received >= self.heartbeat_timeout
        )

    async def _reprobe_dead(self, nodes: list[Node]) -> None:
        """Send the heartbeat configuration to dead (or no longer beating) nodes again; an answer revives a dead one."""
        pdu = C.heartbeat_publication_set(
            self.hub.proxy.state.src, HEARTBEAT_PERIOD_LOG, ttl=self.hub.proxy.ttl
        )
        try:
            await self.hub.chunked(
                [
                    partial(self._set_heartbeat, node, pdu, "reconfigure")
                    for node in nodes
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("heartbeat reprobe aborted: %s", err)

    @callback
    def on_heartbeat(self, beat: Heartbeat) -> None:
        """Remember a node's Heartbeat and mark the node alive."""
        node = self.hub.cdb.node_by_addr(beat.src)
        if node is None:
            return
        self.heartbeats[node.unicast] = beat
        self.mark_alive(beat.src)
        self.hub.signal_node(
            node.unicast, force=True
        )  # hops change rarely: show them at once

    def mark_alive(self, address: int) -> None:
        """Renew the heartbeat deadline of the node owning `address`: a Heartbeat or any message came from it."""
        node = self.hub.cdb.node_by_addr(address)
        if node is None or node.unicast not in self._alive_deadline:
            return  # not a node we asked for heartbeats (yet)
        self._alive_deadline[node.unicast] = time.monotonic() + self.heartbeat_timeout
        if node.unicast in self._dead_nodes:
            self._dead_nodes.discard(node.unicast)
            _LOGGER.info("%s is back (heard from it again)", node.name)
            self._notify_node(node)

    @callback
    def check_heartbeats(self, _now: datetime) -> None:
        """Mark the nodes whose deadline passed as dead and tell their entities; renew the publications when due.

        A link that stays up for days would otherwise never repeat the configuration (`Refresh.after_connect` runs it
        once per link). A dead node is asked again every HEARTBEAT_REPROBE_INTERVAL (`_reprobe_dead`): a node
        that rebooted — a mains blip, seen on a mini actuator — starts with an empty heartbeat
        publication and would otherwise stay unavailable until the next renewal, hours later. So is a node whose
        beats stopped while its other traffic keeps it alive (a metering socket after a power cut:
        it publishes readings every minute, so it never counts as dead, and it would not beat again before the
        renewal either).
        """
        now = time.monotonic()
        if (
            self.hub.connected
            and self.configured_at is not None
            and now - self.configured_at >= HEARTBEAT_RECONFIGURE_INTERVAL
            and ((task := self._lifecycle.task("heartbeats")) is None or task.done())
        ):
            self._lifecycle.set_task(
                "heartbeats",
                self.hub.entry.async_create_background_task(
                    self.hub.hass, self.configure_heartbeats(), f"{DOMAIN} heartbeats"
                ),
            )
        for node in self.heartbeat_nodes if self.hub.connected else ():
            # while the link is down nobody can be heard: the silence is the link's (`LinkManager._connect_to` renews the
            # deadlines when it is back), and the entities are unavailable anyway
            deadline = self._alive_deadline.get(node.unicast)
            if deadline is None or now < deadline or node.unicast in self._dead_nodes:
                continue
            self._dead_nodes.add(node.unicast)
            _LOGGER.warning(
                "%s has not been heard from for %.0f s: marking it unavailable",
                node.name,
                self.heartbeat_timeout,
            )
            self._notify_node(node)
        due = [
            node
            for node in self.heartbeat_nodes
            if (node.unicast in self._dead_nodes or self._beats_stopped(node, now))
            and now - self._reprobed_at.get(node.unicast, -HEARTBEAT_REPROBE_INTERVAL)
            >= HEARTBEAT_REPROBE_INTERVAL
        ]
        if (
            due
            and self.hub.connected
            and (self.reprobe_task is None or self.reprobe_task.done())
        ):
            for node in due:
                self._reprobed_at[node.unicast] = now
            self.reprobe_task = self.hub.entry.async_create_background_task(
                self.hub.hass, self._reprobe_dead(due), f"{DOMAIN} heartbeat reprobe"
            )

    def _notify_node(self, node: Node) -> None:
        """Tell the node's entities, and the mesh health sensors, that its reachability changed."""
        for element in node.elements:
            async_dispatcher_send(
                self.hub.hass,
                SIGNAL_UPDATE.format(self.hub.entry.entry_id, element.address),
            )
        async_dispatcher_send(
            self.hub.hass, SIGNAL_REACHABILITY.format(self.hub.entry.entry_id)
        )
