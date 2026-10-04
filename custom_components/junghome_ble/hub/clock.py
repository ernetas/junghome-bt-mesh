"""The time and location one hub broadcasts to the nodes (review-4 A4-3).

Time Set and the home location go to all nodes at the start of every link (`JungHomeHub._after_connect`), Time Set
again once a day with a read of the nodes' clocks (`send_time_daily`) and right after each change of the local UTC
offset (`arm_offset_change`, `next_utc_offset_change`): the nodes' timers and astro schedules have no other clock.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from custom_components.junghome_ble.const import (
    DOMAIN,
    OFFSET_CHANGE_DELAY,
    OFFSET_SEARCH_DAYS,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.pdu import ALL_NODES

if TYPE_CHECKING:
    from custom_components.junghome_ble.coordinator import JungHomeHub

_LOGGER = logging.getLogger(__name__)


def next_utc_offset_change(now: datetime) -> datetime | None:
    """Return the first moment (UTC) after `now` at which `now`'s time zone changes its UTC offset, within a year.

    Found by stepping a day at a time, then bisecting the day to the second: zoneinfo keeps its transitions to
    itself. None when the offset does not change within the year (no daylight-saving time).
    """
    tz = now.tzinfo
    assert tz is not None
    offset = now.utcoffset()
    step = timedelta(days=1)
    probe = now.astimezone(UTC)
    for _ in range(OFFSET_SEARCH_DAYS):
        nxt = probe + step
        if nxt.astimezone(tz).utcoffset() != offset:
            low, high = probe, nxt  # the change lies after `low`, at or before `high`
            while high - low > timedelta(seconds=1):
                mid = low + (high - low) / 2
                if mid.astimezone(tz).utcoffset() == offset:
                    low = mid
                else:
                    high = mid
            return high.replace(microsecond=0)
        probe = nxt
    return None


class Clock:
    """The time and location broadcasts of one hub (module docstring)."""

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to `hub` (its link, entry and node clocks); no timer armed yet."""
        self.hub = hub
        self.unsub_time: CALLBACK_TYPE | None = (
            None  # the daily Time Set (`send_time_daily`)
        )
        self.unsub_offset_change: CALLBACK_TYPE | None = (
            None  # the Time Set after a DST change
        )

    async def send_time(self, destination: int = ALL_NODES) -> None:
        """Broadcast Time Set (unacknowledged, to all nodes) as the app does after every connection.

        Devices with timers or astro schedules have no clock source but this message: the gateway never publishes
        time (its publish interval is configured to 0), so without a phone nearby their schedules drift.
        `destination`: one element instead (a new node's Time Server, `onboard`'s SetTime phase).
        """
        now = dt_util.now()
        try:
            pdu = M.time_set(now)
        except (
            ValueError
        ):  # a zone offset the message cannot carry: better a UTC clock than none
            pdu = M.time_set(now, zone_offset=timedelta(0))
        try:
            await self.hub.while_seq_stalls(
                partial(self.hub.proxy.send_access, destination, pdu)
            )
        except ConnectionError as err:
            _LOGGER.debug("Time Set not sent: %s", err)
        else:
            _LOGGER.debug(
                "Sent Time Set %s to %04X",
                now.isoformat(timespec="seconds"),
                destination,
            )

    async def send_location(self) -> None:
        """Broadcast Home Assistant's home location (Generic Location Global Set Unacknowledged, to all nodes).

        The nodes compute their sunrise / sunset times from it (astro schedules, `schedules.py`); the app only sends
        the phone's position when it creates such a schedule. Every node hosts the Location Setup Server on its
        primary element, which the all-nodes address reaches, as for Time Set. Unverified on air.
        """
        config = self.hub.hass.config
        pdu = M.generic_location_global_set(
            config.latitude, config.longitude, int(config.elevation)
        )
        try:
            await self.hub.while_seq_stalls(
                partial(self.hub.proxy.send_access, ALL_NODES, pdu)
            )
        except ConnectionError as err:
            _LOGGER.debug("Location not sent: %s", err)
        else:
            _LOGGER.debug("Sent the home location to all nodes")

    async def async_send_time(self, destination: int = ALL_NODES) -> None:
        """Broadcast Time Set now (the `node_clock_wrong` repair's fix); without a link nothing goes out.

        `destination`: one element instead of all nodes (`onboard`: the new node's Time Server).
        """
        await self.send_time(destination)

    @callback
    def send_time_daily(self, _now: datetime) -> None:
        """Broadcast Time Set and read the nodes' clocks in the background, while a link is up (the daily timer)."""
        if self.hub.connected:
            self.hub.entry.async_create_background_task(
                self.hub.hass, self._send_time_and_read_clocks(), f"{DOMAIN} time"
            )

    async def _send_time_and_read_clocks(self) -> None:
        """Broadcast Time Set, then ask the mains nodes for their time, zone and location (`NodeClocks.read_all`).

        Once a day and after a change of the local UTC offset, not on every link: the nodes answer the Time Set of
        each link with their Time Status all the same. Unverified on air.
        """
        await self.send_time()
        await self.hub.clocks.read_all()

    def arm_offset_change(self) -> None:
        """Send Time Set again right after the next change of the local UTC offset (review-3 F17).

        A Time Set carries the zone offset in force when it is sent; the nodes' timers and astro schedules run on it
        until the next one — up to TIME_SET_INTERVAL after a daylight-saving change, an hour off meanwhile.
        """
        change = next_utc_offset_change(dt_util.now())
        if change is None:
            return

        @callback
        def changed(_now: datetime) -> None:
            self.unsub_offset_change = None
            self.send_time_daily(_now)
            self.arm_offset_change()

        self.unsub_offset_change = async_track_point_in_utc_time(
            self.hub.hass, changed, change + timedelta(seconds=OFFSET_CHANGE_DELAY)
        )
