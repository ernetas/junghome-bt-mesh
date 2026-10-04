"""The metered loads of one hub: their readings and counters, the energy poll, the reset, the history import (A4-3).

A load's meter publishes its readings, but nothing publishes its counters: the hub reads them at link-up and every
ENERGY_POLL_INTERVAL (`poll`, a timer `arm_poll` anchors on each link), on demand (`async_refresh_meter`), and
zeroes the resettable ones as the app's "reset consumption" does (`reset_consumption`). After the connect-time poll
the hours a load's *Energy* missed are imported from its meter's charts (`backfill_history`, `energy_history.py`).
The coordinator's status handlers store what the loads answer (`SENSOR_FIELDS`, `COUNTER_FIELDS`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from custom_components.junghome_ble.const import (
    DOMAIN,
    ENERGY_HISTORY_INTERVAL,
    ENERGY_POLL_INTERVAL,
)
from custom_components.junghome_ble.energy_history import async_backfill, floor_hour
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import MeteredLoad, Socket
from custom_components.junghome_ble.jhmesh.properties import SIG_PROPERTIES

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.cdb import CDB
    from custom_components.junghome_ble.jhmesh.client import AccessMessage
    from custom_components.junghome_ble.protocols import HubPort

_LOGGER = logging.getLogger(__name__)

SENSOR_POWER, SENSOR_VOLTAGE, SENSOR_CURRENT = 0x0081, 0x005D, 0x005C
# What the connect-time refresh asks a socket's meter element for, one property-qualified `Sensor Get` each: on air
# the meter ignores an unqualified Sensor Get (no property id; two attempts, no reply, every connection) and answers
# a qualified one within ~100 ms — the gateway polls the same way. Another metered load (the energy puck's output)
# is asked for its power only: all the app reads there (`docs/android/properties.md` §4, `MeasureLampDevice`), and
# voltage and current are the metering socket's properties (`properties.SOCKET_METERING`); unverified on air.
SENSOR_READINGS = (SENSOR_POWER, SENSOR_VOLTAGE, SENSOR_CURRENT)
METER_READINGS = (SENSOR_POWER,)
SENSOR_FIELDS = {  # `ElementState` field each reading lands in
    SENSOR_POWER: "power_w",
    SENSOR_VOLTAGE: "voltage_v",
    SENSOR_CURRENT: "current_a",
}
# SIG device properties a metering socket keeps on its Generic Property servers, none of them ever published, so
# `poll` reads them every ENERGY_POLL_INTERVAL: the power-on hours on the *main* element's Admin server
# (`docs/gap-analysis/control-and-state.md` §2.4) and the energy counters on the *meter* element (the Sensor
# Server's, `docs/hidden-features.md` §2): the lifetime total 0x0072 and the energy since turn-on 0x000D on its
# Manufacturer server, the resettable total 0x006A (the app's "reset consumption") on its Admin server. Another
# metered load (the energy puck's output) has the meter's counters only: the app reads no power-on hours there
# (`docs/gap-analysis/device-settings.md` §6, "no 0x006D"; `counter_element`), unverified on air.
PROPERTY_POWER_ON_TIME = 0x006D
PROPERTY_TOTAL_ENERGY = 0x006A
PROPERTY_PRECISE_TOTAL_ENERGY = 0x0072
PROPERTY_ENERGY_SINCE_TURN_ON = 0x000D


@dataclass(frozen=True)
class CounterRead:
    """One counter of a metering socket: which property, on which server, of which element, into which field."""

    pid: int
    server: (
        str  # "admin" | "manufacturer" — the Generic Property server kind that holds it
    )
    meter: bool  # False = the socket's main element, True = its meter element (`counter_element`)
    field: (
        str  # `ElementState` attribute the decoded value lands in (on the load's state)
    )


class CounterNotReset(Exception):
    """A socket answered a counter reset with a value other than 0 (it kept counting), or with none."""

    def __init__(self, address: int, pid: int) -> None:
        """Name the element and the counter."""
        super().__init__(f"{address:04X} did not reset property {pid:04X}")
        self.address = address
        self.pid = pid


COUNTER_READS: tuple[CounterRead, ...] = (
    CounterRead(PROPERTY_POWER_ON_TIME, "admin", False, "power_on_hours"),
    CounterRead(PROPERTY_PRECISE_TOTAL_ENERGY, "manufacturer", True, "energy_wh"),
    CounterRead(PROPERTY_TOTAL_ENERGY, "admin", True, "energy_resettable_wh"),
    CounterRead(
        PROPERTY_ENERGY_SINCE_TURN_ON, "manufacturer", True, "energy_since_on_wh"
    ),
)
COUNTER_FIELDS = {read.pid: read.field for read in COUNTER_READS}
# the app's "reset consumption" (`docs/gap-analysis/device-settings.md` §5.2): an acknowledged Admin Property Set of
# a 0 to each of these, in the app's order (`ConfigurationConsumptionViewModel.resetTotalConsumption`: the power-on
# hours first, then the resettable total); `meter` as in CounterRead. The lifetime total 0x0072 has no reset.
COUNTER_RESETS: tuple[tuple[int, bool], ...] = (
    (PROPERTY_POWER_ON_TIME, False),
    (PROPERTY_TOTAL_ENERGY, True),
)


def meter_readings(load: MeteredLoad) -> tuple[int, ...]:
    """Return the readings the connect-time refresh asks the load's meter for: SENSOR_READINGS or METER_READINGS."""
    return SENSOR_READINGS if isinstance(load, Socket) else METER_READINGS


def lacks_precise_energy(cdb: CDB, load: MeteredLoad) -> bool:
    """Whether a load other than a socket has no Generic Manufacturer Property Server (`1012`) on its meter.

    That server holds 0x0072 and 0x000D (`docs/hidden-features.md` §2, read on the metering socket only); the app
    reads neither on the energy puck (`docs/android/properties.md` §4), so the composition decides. Without it the
    load's *Energy* is 0x006A (`ElementState.energy_total`). A socket keeps its field-tested reads.
    """
    if isinstance(load, Socket) or load.meter_address is None:
        return False
    meter = cdb.element(load.meter_address)
    return meter is not None and "1012" not in meter.models


def counter_element(load: MeteredLoad, meter: bool) -> int | None:
    """Return the element that holds one of the load's counters (`CounterRead.meter`); None where it has no such counter.

    The meter element for the energy counters; the main element for the power-on hours, which only the metering
    socket keeps (`SIG_PROPERTIES[0x006D].products`).
    """
    if meter:
        return load.meter_address
    return load.address if isinstance(load, Socket) else None


SIG_PROPERTY_STATUS_BY_SERVER = {
    "admin": M.GEN_ADMIN_PROP_STATUS,
    "manufacturer": M.GEN_MANU_PROP_STATUS,
    "user": M.GEN_USER_PROP_STATUS,
}


class Energy:
    """The metered loads' polls and reads of one hub (module docstring)."""

    def __init__(self, hub: HubPort) -> None:
        """Bind to `hub` (its devices, link and entry); no poll armed yet, nothing read."""
        self.hub = hub
        # its timer and task are the hub's (`JungHomeHub.lifecycle`): `energy`, the poll timer (`arm_poll`), and
        # `energy`, the poll running now (`_poll_energy_periodic`)
        self._lifecycle = hub.lifecycle
        self._energy_history_at: float | None = (
            None  # monotonic time of the last energy-chart import (`energy_history`)
        )
        # per socket (main address): its counter Gets and a reset share reply opcodes, so they take turns
        self._counter_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # per metered load (main address): the on-demand read running now, which later callers wait for
        self._meter_refreshes: dict[int, asyncio.Event] = {}

    def arm_poll(self) -> None:
        """(Re)start the energy poll timer so its grid is anchored on the connection just made.

        Anchored on the start of the integration instead, a poll could fall right before the link watchdog's
        deadline on a mesh where the poll's reply is the only traffic. No timer without a metered load.
        """
        self._lifecycle.cancel_timer("energy")
        if self.hub.devices.metered:
            self._lifecycle.set_timer(
                "energy",
                async_track_time_interval(
                    self.hub.hass,
                    self._poll_energy_periodic,
                    timedelta(seconds=ENERGY_POLL_INTERVAL),
                ),
            )

    async def poll(self) -> None:
        """Read the counters of every metered load (energy; a socket's power-on hours); nothing ever publishes them.

        A load's answers share opcodes (Generic Property Statuses), so a load gets its Gets one after the
        other; different loads are polled REFRESH_CHUNK at a time like the state refresh.
        """
        try:
            await self.hub.chunked(
                [partial(self._get_counters, load) for load in self.hub.devices.metered]
            )
        except ConnectionError as err:
            _LOGGER.debug("energy poll aborted: %s", err)

    async def async_refresh_meter(self, load: MeteredLoad) -> None:
        """Read one metered load's readings and counters now: `homeassistant.update_entity` on one of its sensors.

        The app reads them every 5 s while its consumption page is open (`PowerConsumptionViewModel.requestData`);
        here nothing is open, so they are read every ENERGY_POLL_INTERVAL and on demand. The meter's Sensor Gets
        (`get_readings`) go first, then the counters (`_get_counters`), as the connect-time refresh orders them.
        A call while one is running for the same load waits for it instead of asking again: an update of all of a
        load's sensors at once is one read. Best effort: a lost link or a silent meter leaves the cached values.
        """
        running = self._meter_refreshes.get(load.address)
        if running is not None:
            await running.wait()
            return
        done = self._meter_refreshes[load.address] = asyncio.Event()
        try:
            await self.get_readings(load)
            await self._get_counters(load)
        except ConnectionError as err:
            _LOGGER.debug("%04X: meter not read: %s", load.address, err)
        finally:
            del self._meter_refreshes[load.address]
            done.set()

    async def backfill_history(self) -> None:
        """Import what each metered load counted while nobody saw it into its Energy statistics (`energy_history`).

        Right after the connect-time poll, so the counter the charts are checked against is fresh. Needs the
        recorder (an optional dependency: without it there are no statistics to fill); once per link, and not again
        within ENERGY_HISTORY_INTERVAL of the last try — the link before this one lasted until less than that ago,
        so no whole hour can be missing. Loads go REFRESH_CHUNK at a time like the poll.
        """
        now = time.monotonic()
        if (
            "recorder" not in self.hub.hass.config.components
            or self.hub.connected_since is None
            or (
                self._energy_history_at is not None
                and now - self._energy_history_at < ENERGY_HISTORY_INTERVAL
            )
        ):
            return
        self._energy_history_at = now
        link_up = floor_hour(datetime.fromtimestamp(self.hub.connected_since, UTC))
        try:
            await self.hub.chunked(
                [
                    partial(async_backfill, self.hub, load, link_up)
                    for load in self.hub.devices.metered
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("energy history import aborted: %s", err)

    @callback
    def _poll_energy_periodic(self, _now: datetime) -> None:
        """Start a poll every ENERGY_POLL_INTERVAL while a link is up and the previous one is through.

        The connect-time refresh ends with a poll of its own (`Refresh.after_connect`), so a tick during it is skipped too.
        """
        if not self.hub.connected or any(
            task is not None and not task.done()
            for task in (
                self._lifecycle.task("refresh"),
                self._lifecycle.task("energy"),
            )
        ):
            return
        self._lifecycle.set_task(
            "energy",
            self.hub.entry.async_create_background_task(
                self.hub.hass, self.poll(), f"{DOMAIN} energy"
            ),
        )

    async def get_readings(self, load: MeteredLoad) -> None:
        """Ask a load's meter element for its readings, one property-qualified Sensor Get per `meter_readings` entry.

        The meter ignores an unqualified Sensor Get, so the readings are asked for one at a time; each reply is a
        Sensor Status `JungHomeHub._on_sensor_status` stores. One attempt each: the meter publishes every change afterwards,
        so a missed reading is filled in by its next publication.
        """
        addr = load.meter_address
        assert addr is not None  # only metered loads are asked
        for pid in meter_readings(load):
            try:
                await self.hub.proxy.request(
                    addr, M.sensor_get(pid), M.SENSOR_STATUS, retries=1
                )
            except TimeoutError:
                _LOGGER.debug(
                    "%04X did not answer its Sensor Get for property %04X", addr, pid
                )

    async def _get_counters(self, load: MeteredLoad) -> None:
        """Read the counters of one metered load: a Generic Property Get per COUNTER_READS entry it has (`counter_element`)."""
        no_manufacturer = lacks_precise_energy(self.hub.cdb, load)
        if no_manufacturer:
            self.hub.element_state(load.address).energy_fallback = True
        async with self._counter_locks[load.address]:
            for read in COUNTER_READS:
                if (addr := counter_element(load, read.meter)) is None:
                    continue
                if no_manufacturer and read.server == "manufacturer":
                    continue  # no server to answer it
                try:
                    await self.hub.proxy.request(
                        addr,
                        M.generic_property_get(read.server, read.pid),
                        SIG_PROPERTY_STATUS_BY_SERVER[read.server],
                        retries=1,
                    )
                except TimeoutError:
                    _LOGGER.debug(
                        "%04X did not answer its property Get %04X", addr, read.pid
                    )

    async def reset_consumption(self, load: MeteredLoad) -> None:
        """Zero a metered load's resettable energy total (a socket's power-on hours too), as the app's "reset consumption" does.

        One acknowledged Admin Property Set per COUNTER_RESETS entry the load has (`counter_element`), each answered
        by an Admin Property Status that `JungHomeHub._on_sig_property_status` stores. JUNG firmware may only publish the status
        of a Set that changed something, so an unanswered Set is read back with a Get. The first counter that does
        not read 0 stops the reset with CounterNotReset; an unanswered read-back raises TimeoutError. The energy
        puck's reset (0x006A on its meter) is the app's (`device-settings.md` §5.2), unverified on air.
        """
        async with self._counter_locks[load.address]:
            for pid, meter in COUNTER_RESETS:
                if (addr := counter_element(load, meter)) is not None:
                    await self._reset_counter(addr, pid)

    async def _reset_counter(self, addr: int, pid: int) -> None:
        codec = SIG_PROPERTIES[pid].codec
        key = pid.to_bytes(2, "little")

        def for_pid(m: AccessMessage) -> bool:
            return m.params[:2] == key

        try:
            status = await self.hub.proxy.request(
                addr,
                M.generic_property_set("admin", pid, codec.encode(0)),
                M.GEN_ADMIN_PROP_STATUS,
                retries=1,
                match=for_pid,
            )
        except TimeoutError:
            status = await self.hub.proxy.request(
                addr,
                M.generic_property_get("admin", pid),
                M.GEN_ADMIN_PROP_STATUS,
                retries=1,
                match=for_pid,
            )
        value = status.params[3:]
        if not value or codec.decode(value) != 0:
            raise CounterNotReset(addr, pid)
