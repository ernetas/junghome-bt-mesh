"""Each node's clock, zone offset and stored location, as the nodes answer them (review-4 F4-8).

The nodes run their schedules (JH Scheduler slots, astro times) on their own clock, with the zone offset and the
location Home Assistant last broadcast (`Clock.send_time`, `_send_location`); nothing confirmed that they
took them. Here:

- every Time Status a node sends is kept (`NodeClocks.note_time`): the answer each node gives the Time Set broadcast
  after every connection and once a day, the answer to a Time Get, one a node publishes. It gives the node's clock
  offset from Home Assistant's (seconds; the answer's travel time, well under a second, included) and its zone
  offset. A Time Zone Status and a Generic Location Global Status are kept the same way;
- after the daily Time Set every mains node with a Time Server is asked for its time, zone and location
  (`NodeClocks.read_all`: Time Get and Time Zone Get to its Time Server `1200`, Generic Location Global Get to its
  Location Server `100E`), REFRESH_CHUNK nodes at a time; battery nodes sleep and are not asked;
- a node that may run schedules (a load element hosting a JH Scheduler whose last read did not find it empty) with
  no time, a clock more than CLOCK_OFFSET_MAX seconds off, or another zone offset than the Time Set it got carried
  raises the fixable `node_clock_wrong` repair; the fix sends Time Set now and asks those nodes again.

Each mains node's *Clock offset* sensor (diagnostic, off by default) shows the offset, the diagnostics the rest.
The stored location is only compared with Home Assistant's home, never shown: diagnostics get shared. Unverified on
air: the Gets, the nodes' answers to the Time Set broadcast and the repair (a probe saw 27 nodes answer a Time Get
within a second of the probing host's clock, `docs/hidden-features.md` §9).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from . import const
from .const import (
    CLOCK_OFFSET_MAX,
    CLOCK_READ_PAUSE,
    DOMAIN,
    ISSUE_NODE_CLOCK_WRONG,
    LOCATION_TOLERANCE,
    REFRESH_CHUNK,
    SCHEDULER_MODEL,
    SCHEDULERS,
    SIGNAL_NODE,
    learn_more_url,
)
from .entity import health_nodes
from .jhmesh import messages as M
from .jhmesh.devices import Blind, Light, Socket, Thermostat

if TYPE_CHECKING:
    from datetime import datetime

    from .coordinator import JungHomeHub
    from .jhmesh.cdb import Element, Node

_LOGGER = logging.getLogger(__name__)

TIME_SERVER = "1200"
LOCATION_SERVER = "100E"
# the loads the app creates schedules on (`schedules.schedule_targets`)
SCHEDULED_LOADS = (Light, Socket, Blind, Thermostat)


def time_server(node: Node) -> Element | None:
    """Return the node's element hosting the Time Server, the one asked for its time and zone; None without one."""
    return next((e for e in node.elements if TIME_SERVER in e.models), None)


def zone_sent(now: datetime) -> int:
    """Return the zone offset, in minutes, of a Time Set sent at `now`: UTC when the message cannot carry the local one.

    The same fallback as `Clock.send_time`.
    """
    try:
        return (M.time_set(now)[-1] - 64) * 15
    except ValueError:
        return 0


def _hhmm(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


@dataclass
class NodeClock:
    """What one node last told of its clock, zone and location; None where it has not told yet."""

    read: datetime | None = None  # when its last Time Status arrived
    offset: float | None = (
        None  # its UTC minus Home Assistant's, seconds; None with `read` set: it has no time
    )
    zone: int | None = None  # its zone offset, minutes
    zone_expected: int | None = None  # the one Time Set carried when `zone` arrived
    location: tuple[float | None, float | None, int | None] | None = None


class NodeClocks:
    """The nodes' clocks as the hub heard them (module docstring), the daily read and the `node_clock_wrong` repair."""

    def __init__(self, hub: JungHomeHub, issue: str) -> None:
        """Bind to `hub`; `issue` is the entry's `node_clock_wrong` repair id."""
        self.hub = hub
        self.issue = issue
        self.clocks: dict[int, NodeClock] = {}  # node unicast → its clock
        self._reading = False

    # ------------------------------------------------------------------ what the nodes answer
    def _clock(self, src: int) -> tuple[int, NodeClock] | None:
        """Return the unicast and the record of the node the element at `src` belongs to; None for no node."""
        node = self.hub.cdb.node_by_addr(src)
        if node is None:
            return None
        return node.unicast, self.clocks.setdefault(node.unicast, NodeClock())

    def _changed(self, unicast: int) -> None:
        async_dispatcher_send(
            self.hub.hass, SIGNAL_NODE.format(self.hub.entry.entry_id, unicast)
        )
        self.report()

    def note_time(self, src: int, params: bytes) -> None:
        """Keep a Time Status from the element at `src`: the clock offset, and the zone offset when it has a time."""
        try:
            status = M.decode_time_status(params)
        except ValueError as err:
            _LOGGER.debug("%04X: %s", src, err)
            return
        if (found := self._clock(src)) is None:
            return
        unicast, clock = found
        now = dt_util.utcnow()
        utc = status.utc
        clock.read = now
        clock.offset = None if utc is None else (utc - now).total_seconds()
        if utc is not None:
            clock.zone = status.zone_offset
            clock.zone_expected = zone_sent(dt_util.now())
        self._changed(unicast)

    def note_zone(self, src: int, params: bytes) -> None:
        """Keep a Time Zone Status from the element at `src`: the zone offset in force."""
        try:
            status = M.decode_time_zone_status(params)
        except ValueError as err:
            _LOGGER.debug("%04X: %s", src, err)
            return
        if (found := self._clock(src)) is None:
            return
        unicast, clock = found
        clock.zone = status.current
        clock.zone_expected = zone_sent(dt_util.now())
        self._changed(unicast)

    def note_location(self, src: int, params: bytes) -> None:
        """Keep a Generic Location Global Status from the element at `src`: the location the node computes astro times for."""
        try:
            location = M.location_global(params)
        except ValueError as err:
            _LOGGER.debug("%04X: Generic Location Global Status: %s", src, err)
            return
        if (found := self._clock(src)) is None:
            return
        unicast, clock = found
        clock.location = location
        self._changed(unicast)

    def offset(self, unicast: int) -> float | None:
        """Return the node's clock offset in seconds (its *Clock offset* sensor); None while unknown or it has no time."""
        clock = self.clocks.get(unicast)
        return None if clock is None or clock.offset is None else round(clock.offset, 1)

    # ------------------------------------------------------------------ the repair
    def _may_run_schedules(self, node: Node) -> bool:
        """Whether a load of the node hosts a JH Scheduler whose slots are not known to be empty.

        The slots are known only where the *Schedules* sensor or a schedule action read them (`schedules.Scheduler`);
        a load nothing read may have schedules the app made.
        """
        scheduler = self.hub.hass.data.get(SCHEDULERS, {}).get(self.hub.entry.entry_id)
        for address, device in self.hub.devices.by_address.items():
            element = self.hub.cdb.element(address)
            if (
                device.node is node
                and isinstance(device, SCHEDULED_LOADS)
                and element is not None
                and SCHEDULER_MODEL in element.models
            ):
                slots = None if scheduler is None else scheduler.slots.get(address)
                if slots is None or slots:
                    return True
        return False

    def wrong(self, unicast: int) -> str | None:
        """Return what is wrong with the node's clock (`no time`, its offset, its zone); None when nothing known is."""
        clock = self.clocks.get(unicast)
        if clock is None or clock.read is None:
            return None
        if clock.offset is None:
            return "no time"
        if abs(clock.offset) > CLOCK_OFFSET_MAX:
            return f"{clock.offset:+.0f} s"
        if clock.zone is not None and clock.zone != clock.zone_expected:
            return f"UTC{_hhmm(clock.zone)}"
        return None

    def _wrong_nodes(self) -> list[tuple[Node, str]]:
        return [
            (node, reason)
            for node in health_nodes(self.hub)
            if (reason := self.wrong(node.unicast)) is not None
            and self._may_run_schedules(node)
        ]

    def report(self) -> None:
        """Raise (or update, or clear) the `node_clock_wrong` repair: nodes that may run schedules on a wrong clock."""
        wrong = self._wrong_nodes()
        if not wrong:
            ir.async_delete_issue(self.hub.hass, DOMAIN, self.issue)
            return
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            self.issue,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NODE_CLOCK_WRONG,
            learn_more_url=learn_more_url(ISSUE_NODE_CLOCK_WRONG),
            translation_placeholders={
                "title": self.hub.entry.title,
                "devices": ", ".join(
                    f"{node.name} {node.unicast:04X} ({reason})"
                    for node, reason in wrong
                ),
            },
            data={"entry_id": self.hub.entry.entry_id},
        )

    async def async_fix(self) -> bool:
        """Send Time Set now, then ask the nodes the repair names again; False without a link.

        Their answers clear the repair (`report`); a node that stays silent keeps it.
        """
        if not self.hub.connected:
            return False
        await self.hub.async_send_time()
        await self._read_nodes([node for node, _ in self._wrong_nodes()])
        return True

    # ------------------------------------------------------------------ the daily read
    async def read_all(self) -> bool:
        """Ask every mains node with a Time Server for its time, zone and location (after the daily Time Set).

        True when every node answered; False when one stayed silent, the link went away or a read already runs.
        """
        if self._reading:
            return False
        self._reading = True
        try:
            return await self._read_nodes(
                [n for n in health_nodes(self.hub) if time_server(n) is not None]
            )
        finally:
            self._reading = False

    async def _read_nodes(self, nodes: list[Node]) -> bool:
        complete = True
        for i in range(0, len(nodes), REFRESH_CHUNK):
            if i:
                await asyncio.sleep(CLOCK_READ_PAUSE)
            results = await asyncio.gather(
                *(self._read(node) for node in nodes[i : i + REFRESH_CHUNK])
            )
            if None in results:
                _LOGGER.debug("clock read aborted: the link went away")
                return False
            complete = complete and all(results)
        return complete

    async def _read(self, node: Node) -> bool | None:
        """Ask one node; its Statuses land in `note_*` through the hub's handlers.

        True when it answered every Get, False when it stayed silent (the rest is not asked then), None when the link
        went away.
        """
        server = time_server(node)
        if server is None:
            return True
        asks = [
            (server.address, M.time_get(), M.TIME_STATUS),
            (server.address, M.time_zone_get(), M.TIME_ZONE_STATUS),
        ]
        location = next((e for e in node.elements if LOCATION_SERVER in e.models), None)
        if location is not None:
            asks.append(
                (
                    location.address,
                    M.generic_location_global_get(),
                    M.GEN_LOCATION_GLOBAL_STATUS,
                )
            )
        for address, pdu, opcode in asks:
            try:
                await self.hub.proxy.request(
                    address,
                    pdu,
                    opcode,
                    timeout=const.PROPERTY_READ_TIMEOUT,
                    retries=const.PROPERTY_READ_RETRIES,
                )
            except TimeoutError:
                _LOGGER.debug("%04X did not answer %s", address, M.describe(pdu))
                return False
            except ConnectionError:
                return None
        return True

    # ------------------------------------------------------------------ diagnostics
    def _location(self, clock: NodeClock) -> str | None:
        """Describe the stored location without it: `home`, `elsewhere`, `not configured`; None while not read."""
        if clock.location is None:
            return None
        latitude, longitude, _altitude = clock.location
        if latitude is None or longitude is None:
            return "not configured"
        config = self.hub.hass.config
        if (
            abs(latitude - config.latitude) <= LOCATION_TOLERANCE
            and abs(longitude - config.longitude) <= LOCATION_TOLERANCE
        ):
            return "home"
        return "elsewhere"

    def diagnostics(self, unicast: int) -> dict[str, Any] | None:
        """Render what the node told of its clock, zone and location (the location only as compared with home)."""
        clock = self.clocks.get(unicast)
        if clock is None:
            return None
        return {
            "read": None if clock.read is None else clock.read.isoformat(),
            "offset": self.offset(unicast),
            "has_time": None if clock.read is None else clock.offset is not None,
            "zone_offset": clock.zone,
            "zone_expected": clock.zone_expected,
            "location": self._location(clock),
            "wrong": self.wrong(unicast),
        }
