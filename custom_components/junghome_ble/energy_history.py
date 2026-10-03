"""Energy history: spread what a metered load counted while Home Assistant could not see it over the right hours.

While Home Assistant is down, or the mesh link is, the *Energy* sensor has no value and the recorder compiles no
hourly statistics for it; the first reading afterwards puts the whole gap's consumption into a single hour of the
Energy dashboard. The socket's meter element keeps the two charts the app draws its consumption page from
(`docs/android/properties.md` §1.6, `docs/gap-analysis/control-and-state.md` §2.4): `0x5010`, the energy of each of
the last 24 hours, and `0x5011`, of each of the last 31 days (LBC User Property Get, as the app reads them). The
app draws the energy puck's page from the same two (`properties.md` §4, `MeasureLampDevice`); its meter is asked
the same way, unverified on air (`jhmesh.devices.meter_element`), and checked against the counter its *Energy*
sensor shows (`ElementState.energy_total`: 0x006A where the meter has no 0x0072; a reset in the gap moves that
counter backwards, and no row goes past what the counter moved, `backfill_rows`). After
the connect-time energy poll the hub reads them (`JungHomeHub._backfill_energy_history`: once per link, at most once
every `ENERGY_HISTORY_INTERVAL`) and imports the hours the sensor's statistics lack into those statistics, so the
jump is spread back over the hours it was counted in — beyond the hourly chart, one row per day, at the day's last
hour. The daily chart is only read when the gap reaches back past the hourly one.

What is known of the charts comes from the app's decoder alone (`J7/b.java:85-133`): big-endian samples x 0.1,
entry *i* = *i* hours (days) ago, all ones = no value (`properties.EnergyChart`). The rest are assumptions:

- the unit is 0.1 Wh: the counters are whole Wh, and a u16 of 0.1 Wh holds 6.5 kWh an hour, just above what a 16 A
  socket can pass (3.7 kWh); a coarser unit could not tell one hour of a lamp from nothing;
- entry 0 is the hour (day) still running (`CHART_LAG`), on clock hours and the local days of the device's clock,
  which Home Assistant's Time Set keeps on its own time zone. A zone whose offset is not a whole number of hours
  would put the device's hours across the statistics' hours: no import there.

None of this was read on air yet, so every import is checked against the lifetime counter first: the energy the
charts give after the last recorded hour must match what the counter moved since that hour's reading
(`TOLERANCE_WH` + `TOLERANCE_SHARE`); charts that do not fit — another unit, the other order, other hours — are left
alone. An import only ever moves energy earlier: every imported sum lies between the last recorded one and what the
counter reads now, so the totals do not change, and whatever the charts cannot place still lands in the first hour
after the gap as before.

Only hours after the last recorded one are written, and only hours that ended before the link came up: from that
hour on the sensor has a value and the recorder compiles the hour itself. Hours the recorder already filled with an
unchanged value (it can, for a restart, when it compiles the missed periods after the entity is back) count as
recorded: nothing is written then.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
    get_metadata,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from . import const
from .const import DOMAIN, PROPERTY_READ_RETRIES
from .jhmesh import messages as M
from .jhmesh import properties as P

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import JungHomeHub
    from .jhmesh.devices import MeteredLoad

_LOGGER = logging.getLogger(__name__)

HOURLY_CHART = 0x5010  # `daily_energy_chart`: the app's 24-hour view
DAILY_CHART = 0x5011  # `monthly_energy_chart`: the app's 31-day view
HOUR = timedelta(hours=1)
# Assumption: entry i of a chart is the hour (day) that began i + CHART_LAG hours (days) before the one running now;
# 0 = entry 0 is the running one, still counting. The counter check rejects charts one period off only when the
# load changed between the hours, so this stays an assumption until a read on air settles it.
CHART_LAG = 0
# How far the charts may miss the counter's move: the counter is read a few seconds before the charts (a 3.7 kW load
# counts 10 Wh in 10 s), the charts round every sample to 0.1 Wh, and the counter to whole Wh.
TOLERANCE_WH = 20.0
TOLERANCE_SHARE = 0.02


@dataclass(frozen=True)
class Span:
    """A stretch of time the charts give the energy of (UTC); `energy_wh` None when the chart has no value for it."""

    start: datetime
    end: datetime
    energy_wh: float | None


def floor_hour(when: datetime) -> datetime:
    """Return the start of the UTC hour `when` falls in."""
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def chart_spans(
    now: datetime, hourly: Sequence[float | None], daily: Sequence[float | None]
) -> list[Span]:
    """Return the stretches the charts read at `now` cover, oldest first: one per hourly entry, one per day before them.

    The day the hourly entries begin in counts only up to their first hour: its energy less theirs, unknown when any
    of them is, or when theirs exceed it by more than the tolerance (then the two charts disagree).
    """
    running = floor_hour(now)
    hours = [
        Span(
            running - (i + CHART_LAG) * HOUR,
            running - (i + CHART_LAG - 1) * HOUR,
            energy,
        )
        for i, energy in enumerate(hourly)
    ]
    first = min((span.start for span in hours), default=running + HOUR)
    today = dt_util.as_local(now).date()
    days: list[Span] = []
    for j, energy in enumerate(daily):
        start = dt_util.start_of_local_day(today - timedelta(days=j + CHART_LAG))
        end = dt_util.start_of_local_day(today - timedelta(days=j + CHART_LAG - 1))
        if start >= first:
            continue  # the hourly chart has this day's hours
        rest = energy
        if end > first:
            covered = [span.energy_wh for span in hours if span.start < end]
            known = [e for e in covered if e is not None]
            rest = (
                None
                if energy is None
                or len(known) < len(covered)
                or energy - sum(known) < -TOLERANCE_WH
                else max(energy - sum(known), 0.0)
            )
            end = first
        days.append(Span(start.astimezone(UTC), end.astimezone(UTC), rest))
    return sorted([*days, *hours], key=lambda span: span.start)


def backfill_rows(
    spans: Sequence[Span],
    last_start: datetime,
    last_wh: float,
    now_wh: float,
    before: datetime,
) -> list[tuple[datetime, float]] | None:
    """Return the statistics rows to import: (hour, energy counted since the last recorded reading, Wh).

    `last_start` is the last recorded hour, `last_wh` the counter reading it holds, `now_wh` the counter now;
    `before` the hour the link came up in, from which on the recorder has the hours itself. One row per span
    after the last recorded hour that ends before `before`, at the span's last hour. None when the charts do not
    fit the counter: they must reach back to the last recorded hour and hold a value for every span from there, and
    the counter's move must lie between their energy after that hour and the same plus that hour's own (the
    reading may have been taken anywhere in it).
    """
    boundary = [span for span in spans if span.start <= last_start < span.end]
    after = [span for span in spans if span.start > last_start]
    known = [(span, span.energy_wh) for span in after if span.energy_wh is not None]
    if not boundary or boundary[0].energy_wh is None or len(known) < len(after):
        return None
    moved = now_wh - last_wh
    low = sum(wh for _, wh in known)
    high = low + boundary[0].energy_wh
    slack = TOLERANCE_WH + TOLERANCE_SHARE * high
    if not low - slack <= moved <= high + slack:
        return None
    rows: list[tuple[datetime, float]] = []
    total = 0.0
    for span, wh in known:
        if span.end > before:
            break
        total += wh
        # never past the counter: the recorder's next hour must not come out lower than an imported one
        rows.append((span.end - HOUR, min(total, max(moved, 0.0))))
    return rows


async def read_chart(
    hub: JungHomeHub, addr: int, pid: int
) -> list[float | None] | None:
    """Read one chart from the load's meter element (LBC User Property Get); None when it did not answer."""
    key = pid.to_bytes(2, "little")
    try:
        reply = await hub.proxy.request(
            addr,
            M.vendor_property_get("user", pid),
            M.VENDOR_PROPERTY_STATUS_OPCODES["user"],
            timeout=const.PROPERTY_READ_TIMEOUT,
            retries=PROPERTY_READ_RETRIES,
            expect_cid=M.JUNG_CID,
            match=lambda m: m.params[:2] == key,
        )
    except TimeoutError:
        _LOGGER.debug("%04X did not answer the Get of its energy chart %04X", addr, pid)
        return None
    chart: list[float | None] = P.PROPERTIES[pid].codec.decode(reply.params[3:])
    return chart


@dataclass(frozen=True)
class LastHour:
    """The last hour the Energy sensor's statistics hold: its start, reading and sum, in the metadata's `unit`."""

    start: datetime
    state: float
    sum: float
    unit: str
    metadata: StatisticMetaData


async def last_recorded(hass: HomeAssistant, entity_id: str) -> LastHour | None:
    """Return the sensor's last hourly statistics row; None without one, or when it is no energy sum."""
    instance = get_instance(hass)
    last = await instance.async_add_executor_job(
        get_last_statistics, hass, 1, entity_id, False, {"state", "sum"}
    )
    metadata = await instance.async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={entity_id})
    )
    if entity_id not in last or entity_id not in metadata:
        return None  # nothing recorded yet: no gap to fill
    row = last[entity_id][0]
    meta = metadata[entity_id][1]
    unit = meta["unit_of_measurement"]
    state, total = row.get("state"), row.get("sum")
    if (
        state is None
        or total is None
        or unit is None
        or unit not in EnergyConverter.VALID_UNITS
        or not meta["has_sum"]
    ):
        return None
    return LastHour(datetime.fromtimestamp(row["start"], UTC), state, total, unit, meta)


async def read_charts(
    hub: JungHomeHub, addr: int, last_start: datetime
) -> list[Span] | None:
    """Read the hourly chart, and the daily one when the hours do not reach back to `last_start`, into spans.

    None when a chart went unanswered, or when an hour began while reading (the entries moved on in between).
    """
    now = dt_util.utcnow()
    hourly = await read_chart(hub, addr, HOURLY_CHART)
    if hourly is None:
        return None
    daily: list[float | None] | None = []
    if min((s.start for s in chart_spans(now, hourly, [])), default=now) > last_start:
        daily = await read_chart(hub, addr, DAILY_CHART)
    if daily is None or floor_hour(dt_util.utcnow()) != floor_hour(now):
        return None
    return chart_spans(now, hourly, daily)


async def async_backfill(hub: JungHomeHub, load: MeteredLoad, before: datetime) -> None:
    """Import the hours the load's Energy statistics lack from its charts, when there are any and the charts fit.

    `before` is the hour the link came up in (`backfill_rows`). Needs the recorder loaded; the statistics are the
    sensor's own (source `recorder`), in the unit its metadata holds.
    """
    hass = hub.hass
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{load.unique_id}-energy"
    )
    st = hub.states.get(load.address)
    now_wh = st.energy_total if st is not None else None
    offset = dt_util.now().utcoffset() or timedelta()
    if (
        entity_id is None
        or now_wh is None
        or load.meter_address is None
        or offset % HOUR
    ):
        return
    last = await last_recorded(hass, entity_id)
    if last is None or last.start + HOUR >= before:
        return  # nothing recorded, or no whole hour missing
    spans = await read_charts(hub, load.meter_address, last.start)
    last_wh = EnergyConverter.convert(last.state, last.unit, UnitOfEnergy.WATT_HOUR)
    rows = (
        None
        if spans is None
        else backfill_rows(spans, last.start, last_wh, now_wh, before)
    )
    if not rows:
        _LOGGER.debug(
            "%s: energy charts not imported (unanswered, no whole hour to place, or not fitting the counter)",
            entity_id,
        )
        return
    stats = [
        StatisticData(
            start=start,
            state=last.state
            + (moved := EnergyConverter.convert(wh, UnitOfEnergy.WATT_HOUR, last.unit)),
            sum=last.sum + moved,
        )
        for start, wh in rows
    ]
    meta = last.metadata
    async_import_statistics(
        hass,
        StatisticMetaData(
            mean_type=meta["mean_type"],
            has_sum=True,
            name=meta["name"],
            source=meta["source"],
            statistic_id=entity_id,
            unit_class=meta["unit_class"],
            unit_of_measurement=last.unit,
        ),
        stats,
    )
    _LOGGER.info(
        "%s: imported %d missed hours of energy from the meter's charts",
        entity_id,
        len(stats),
    )
