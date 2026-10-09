"""Energy history: a metering socket's charts imported into its Energy statistics after a gap (`energy_history.py`).

Every test runs with the recorder (the autouse override below starts it before `hass`, as it must be). The fixture
clock stands at 10:20 UTC in the suite's time zone (US/Pacific, eight hours behind in winter), so the running hour of
the hourly chart is 10:00 UTC and the local day begins at 08:00 UTC.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.junghome_ble import energy_history as EH
from custom_components.junghome_ble.const import DOMAIN, ENERGY_HISTORY_INTERVAL
from custom_components.junghome_ble.jhmesh import messages as M

from .conftest import FakeProxyLink, setup_entry, wait_for_link, wait_until
from .helpers import OUR_ADDRESS, SOCKET, SOCKET_SENSOR, UID_SOCKET
from .property_helpers import PropertyMesh
from .test_coordinator import answer_gets

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder import Recorder
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.junghome_ble.coordinator import JungHomeHub

NOW = datetime(2026, 1, 15, 10, 20, tzinfo=UTC)  # synthetic
RUNNING = datetime(
    2026, 1, 15, 10, tzinfo=UTC
)  # the running hour, and the hour the link comes up in
H = timedelta(hours=1)
ENERGY_ID = "sensor.socket_energy"
COUNTER_WH = 210198  # what the fake socket's lifetime counter reads (`test_coordinator.STATE_REPLIES`)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Recorder, enable_custom_integrations: None
) -> None:
    """Start the recorder before `hass` is set up, then load custom_components (overrides conftest's)."""


@pytest.fixture
def recorder_config() -> dict[str, Any]:
    """Record the Energy sensor's states alone (overrides Home Assistant's fixture).

    The tests read its statistics and nothing else, while writing the states of every other entity the entry adds
    kept the recorder thread busy for most of a test's second (`CALL_BUDGET`).
    """
    return {"include": {"entities": [ENERGY_ID]}}


@pytest.fixture(autouse=True)
def no_property_reads() -> Generator[None]:
    """Keep the config entities' reads and the loads' lock reads out: the mesh here answers the charts only."""
    with (
        patch(
            "custom_components.junghome_ble.config_entities.ConfigEntity._maybe_read"
        ),
        patch(
            "custom_components.junghome_ble.config_entities.LoadLock._maybe_read_lock"
        ),
    ):
        yield


def hourly_chart(values: Sequence[float | None]) -> bytes:
    """0x5010 on the wire: big-endian u16 x 0.1 Wh per hour, newest first, all ones = no value."""
    return b"".join(
        (0xFFFF if v is None else round(v * 10)).to_bytes(2, "big") for v in values
    )


def daily_chart(values: Sequence[float | None]) -> bytes:
    """0x5011 on the wire: big-endian u24 x 0.1 Wh per day, newest first."""
    return b"".join(
        (0xFFFFFF if v is None else round(v * 10)).to_bytes(3, "big") for v in values
    )


# the hours 10:00 (running), 09:00, 08:00, 07:00, 06:00, 05:00 UTC, then older ones (one without a value)
HOURLY = [
    50.0,
    100.0,
    200.0,
    300.0,
    400.0,
    150.0,
    0.0,
    0.0,
    0.0,
    0.0,
    None,
    *[0.0] * 13,
]


async def seed(
    hass: HomeAssistant,
    start: datetime,
    state: float | None,
    total: float | None,
    **meta: Any,
) -> None:
    """Record one hour of the Energy sensor's statistics, in kWh like the sensor itself."""
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=ENERGY_ID,
        unit_class="energy",
        unit_of_measurement="kWh",
    )
    row = StatisticData(start=start)
    if state is not None:
        row["state"] = state
    if total is not None:
        row["sum"] = total
    async_import_statistics(hass, metadata | meta, [row])
    await async_wait_recording_done(hass)


async def recorded(hass: HomeAssistant) -> list[tuple[datetime, float, float]]:
    """The sensor's hourly rows in kWh: (start, state, sum)."""
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        NOW - 30 * 24 * H,
        None,
        {ENERGY_ID},
        "hour",
        {"energy": "kWh"},
        {"state", "sum"},
    )
    return [
        (datetime.fromtimestamp(r["start"], UTC), r["state"], r["sum"])
        for r in stats.get(ENERGY_ID, [])
    ]


def register_energy_sensor(hass: HomeAssistant) -> None:
    """Give the socket's Energy sensor a known entity id before the integration adds it."""
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{UID_SOCKET}-energy", suggested_object_id="socket_energy"
    )


async def connect(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    fake_link: FakeProxyLink,
    charts: dict[int, bytes],
) -> tuple[JungHomeHub, PropertyMesh]:
    """Set the integration up against a mesh that answers the refresh, the counters and `charts` (pid -> value).

    Returns once the connect-time sequence (which ends with the import) is through and the recorder has written.
    """
    answer_gets(fake_link)
    mesh = PropertyMesh(
        fake_link, {(SOCKET_SENSOR, pid): value for pid, value in charts.items()}
    )
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    hub: JungHomeHub = entry.runtime_data
    await wait_until(
        hass,
        lambda: (
            hub.lifecycle.task("refresh") is not None
            and hub.lifecycle.task("refresh").done()
        ),
        what="the connect-time sequence",
    )
    await async_wait_recording_done(hass)
    return hub, mesh


# --------------------------------------------------------------------------- the import on connect


async def test_missed_hours_are_imported_on_connect(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Statistics end at 05:00, the link comes up at 10:20: the hours 06:00-09:00 come from the hourly chart.

    The counter moved 1100 Wh since the 05:00 reading: between what the chart has after that hour (1050 Wh, the
    running hour included) and the same plus the 05:00 hour's 150 Wh, so the chart fits. The 10:00 hour is the
    recorder's own (the sensor has a value in it); the daily chart is not needed.
    """
    freezer.move_to(NOW)
    register_energy_sensor(hass)
    await seed(hass, RUNNING - 5 * H, (COUNTER_WH - 1100) / 1000, 1000.0)

    hub, mesh = await connect(
        hass, mock_config_entry, fake_link, {EH.HOURLY_CHART: hourly_chart(HOURLY)}
    )

    assert hub.states[SOCKET].energy_wh == COUNTER_WH
    assert (SOCKET_SENSOR, EH.HOURLY_CHART) in mesh.gets
    assert (SOCKET_SENSOR, EH.DAILY_CHART) not in mesh.gets
    chart_get = M.vendor_property_get("user", EH.HOURLY_CHART)
    assert chart_get[:3] == bytes.fromhex("ce2705")  # the app's LBC User Property Get
    assert (OUR_ADDRESS, SOCKET_SENSOR, chart_get) in fake_link.sent
    rows = await recorded(hass)
    assert [start for start, _, _ in rows] == [RUNNING - n * H for n in (5, 4, 3, 2, 1)]
    assert [s for _, s, _ in rows] == pytest.approx(
        [209.098, 209.498, 209.798, 209.998, 210.098]
    )
    assert [s for _, _, s in rows] == pytest.approx(
        [1000.0, 1000.4, 1000.7, 1000.9, 1001.0]
    )
    assert f"{ENERGY_ID}: imported 4 missed hours of energy" in caplog.text

    # a second link within the hour asks nothing: no whole hour can be missing
    mesh.gets.clear()
    await hub.energy.backfill_history()
    assert mesh.gets == []


async def test_days_beyond_the_hourly_chart_come_from_the_daily_one(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Statistics end two and a half days ago: whole days, then hours.

    Local days begin at 08:00 UTC. The hourly chart (10 Wh every hour) reaches back to 11:00 yesterday; the day it
    begins in counted 240 Wh, 210 of them in its hours from 11:00 on, so the 30 Wh before land at 10:00. The day
    before (500 Wh) lands on its last hour, 07:00 yesterday; the day holding the last recorded hour (700 Wh) only
    bounds the check. Moved 1000 Wh: between 770 and 1470.
    """
    freezer.move_to(NOW)
    register_energy_sensor(hass)
    last = datetime(2026, 1, 12, 20, tzinfo=UTC)
    await seed(hass, last, (COUNTER_WH - 1000) / 1000, 50.0)
    daily = [20.0, 240.0, 500.0, 700.0, *[None] * 27]

    _, mesh = await connect(
        hass,
        mock_config_entry,
        fake_link,
        {
            EH.HOURLY_CHART: hourly_chart([10.0] * 24),
            EH.DAILY_CHART: daily_chart(daily),
        },
    )

    assert (SOCKET_SENSOR, EH.DAILY_CHART) in mesh.gets
    yesterday_start = datetime(2026, 1, 14, 8, tzinfo=UTC)
    expected = [
        (last, 0.0),
        (yesterday_start - H, 500.0),
        (yesterday_start + 2 * H, 530.0),
        *[(yesterday_start + (3 + n) * H, 540.0 + 10 * n) for n in range(23)],
    ]
    rows = await recorded(hass)
    assert [start for start, _, _ in rows] == [start for start, _ in expected]
    assert [s for _, _, s in rows] == pytest.approx(
        [50.0 + wh / 1000 for _, wh in expected]
    )
    assert expected[-1][0] == RUNNING - H


async def test_charts_that_do_not_fit_the_counter_are_left_alone(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The counter moved 5 kWh, the chart holds 1.2 kWh: another unit or order than assumed, nothing is imported."""
    freezer.move_to(NOW)
    register_energy_sensor(hass)
    await seed(hass, RUNNING - 5 * H, (COUNTER_WH - 5000) / 1000, 1000.0)

    await connect(
        hass, mock_config_entry, fake_link, {EH.HOURLY_CHART: hourly_chart(HOURLY)}
    )

    assert len(await recorded(hass)) == 1
    assert "energy charts not imported" in caplog.text


async def test_nothing_is_asked_without_a_gap(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Statistics up to the hour before the link: no whole hour is missing, the charts are not read."""
    freezer.move_to(NOW)
    register_energy_sensor(hass)
    await seed(hass, RUNNING - H, COUNTER_WH / 1000, 1000.0)

    _, mesh = await connect(hass, mock_config_entry, fake_link, {})

    assert mesh.gets == []


async def test_nothing_is_asked_before_anything_is_recorded(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    freezer.move_to(NOW)
    _, mesh = await connect(hass, mock_config_entry, fake_link, {})
    assert mesh.gets == []


@pytest.mark.parametrize(
    ("state", "total", "meta"),
    [
        (None, 1000.0, {}),  # a row without a reading
        (209.0, 1000.0, {"unit_of_measurement": "blah", "unit_class": None}),
        (209.0, None, {"has_sum": False, "mean_type": StatisticMeanType.ARITHMETIC}),
    ],
)
async def test_statistics_that_are_no_energy_sum_are_left_alone(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    state: float | None,
    total: float | None,
    meta: dict[str, Any],
) -> None:
    freezer.move_to(NOW)
    register_energy_sensor(hass)
    await seed(hass, RUNNING - 5 * H, state, total, **meta)
    _, mesh = await connect(hass, mock_config_entry, fake_link, {})
    assert mesh.gets == []


async def test_what_the_hub_skips(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The per-link limit, a link gone, a lost connection, an unknown counter, a half-hour zone, silent charts."""
    freezer.move_to(NOW)
    register_energy_sensor(hass)
    hub, mesh = await connect(hass, mock_config_entry, fake_link, {})
    await seed(hass, RUNNING - 5 * H, (COUNTER_WH - 1100) / 1000, 1000.0)
    sock = next(s for s in hub.devices.sockets if s.address == SOCKET)

    # within the hour of the last try: nothing
    await hub.energy.backfill_history()
    assert mesh.gets == []

    # an hour on, but the link just went away
    freezer.tick(ENERGY_HISTORY_INTERVAL)
    since, hub.connected_since = hub.connected_since, None
    await hub.energy.backfill_history()
    assert mesh.gets == []
    hub.connected_since = since

    # the link breaks while reading
    with patch(
        "custom_components.junghome_ble.hub.energy.async_backfill",
        AsyncMock(side_effect=ConnectionError("not connected to a proxy")),
    ):
        await hub.energy.backfill_history()
    assert "energy history import aborted: not connected to a proxy" in caplog.text

    # the counter is unknown; a zone half an hour off the statistics' hours
    hub.states[SOCKET].energy_wh = None
    await EH.async_backfill(hub, sock, RUNNING)
    hub.states[SOCKET].energy_wh = COUNTER_WH
    await hass.config.async_update(time_zone="Asia/Kolkata")
    await EH.async_backfill(hub, sock, RUNNING)
    await hass.config.async_update(time_zone="US/Pacific")
    assert mesh.gets == []

    # the socket does not answer (the frozen clock would never time the Get out: the proxy gives up at once)
    with patch.object(hub.proxy, "request", AsyncMock(side_effect=TimeoutError())):
        await EH.async_backfill(hub, sock, RUNNING)
    assert "did not answer the Get of its energy chart 5010" in caplog.text
    assert len(await recorded(hass)) == 1

    # a socket whose Energy sensor has no entity
    await EH.async_backfill(
        hub, SimpleNamespace(**{**vars(sock), "unique_id": "gone"}), RUNNING
    )
    assert mesh.gets == []


# --------------------------------------------------------------------------- reading the charts


def chart_hub(replies: list[bytes | Exception], on_request: Any = None) -> Any:
    """A hub whose meter answers each chart Get with the next reply (a value, or an exception to raise)."""

    async def request(addr: int, pdu: bytes, *args: Any, **kwargs: Any) -> Any:
        if on_request is not None:
            on_request()
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(params=pdu[3:5] + b"\x01" + reply)

    return SimpleNamespace(proxy=SimpleNamespace(request=request))


async def test_read_charts(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)
    hourly = hourly_chart([1.0] * 24)
    # the hours reach back to the last recorded one: the daily chart is not read
    spans = await EH.read_charts(chart_hub([hourly]), SOCKET_SENSOR, RUNNING - 23 * H)
    assert spans is not None
    assert len(spans) == 24
    # further back: it is
    spans = await EH.read_charts(
        chart_hub([hourly, daily_chart([1.0, 2.0])]), SOCKET_SENSOR, RUNNING - 24 * H
    )
    assert spans is not None
    assert len(spans) == 25
    # an unanswered daily chart, an hour that began while reading
    assert (
        await EH.read_charts(
            chart_hub([hourly, TimeoutError()]), SOCKET_SENSOR, RUNNING - 24 * H
        )
        is None
    )
    assert (
        await EH.read_charts(
            chart_hub([hourly], on_request=lambda: freezer.tick(H)),
            SOCKET_SENSOR,
            RUNNING - 23 * H,
        )
        is None
    )


# --------------------------------------------------------------------------- spans and rows


async def test_chart_spans(hass: HomeAssistant) -> None:
    """Hours newest first; days before the hours; the day the hours begin in cut at their first one."""
    now = NOW
    first = RUNNING - 2 * H  # three hourly entries: 08:00, 09:00, 10:00 UTC
    local_day = dt_util.start_of_local_day(dt_util.as_local(now).date())
    assert local_day.astimezone(UTC) == datetime(2026, 1, 15, 8, tzinfo=UTC)
    spans = EH.chart_spans(now, [3.0, 2.0, 1.0], [5.0, 20.0, 30.0])
    assert spans == [
        EH.Span(first - 48 * H, first - 24 * H, 30.0),
        EH.Span(first - 24 * H, first, 20.0),  # yesterday: ends where the hours begin
        EH.Span(first, first + H, 1.0),
        EH.Span(first + H, first + 2 * H, 2.0),
        EH.Span(first + 2 * H, first + 3 * H, 3.0),
    ]  # today begins with the first hour: the hours have it all

    # a day the hours begin inside of: what it counted before them
    first = RUNNING - 3 * H  # 07:00 UTC, the last hour of yesterday
    day = datetime(2026, 1, 14, 8, tzinfo=UTC)
    assert EH.chart_spans(now, [1.0, 1.0, 1.0, 1.0], [2.0, 10.0])[0] == EH.Span(
        day, first, 9.0
    )
    # ... unknown when one of its hours is, when it is, or when its hours hold far more than the day
    for hourly, daily in (
        ([1.0, 1.0, 1.0, None], [2.0, 10.0]),
        ([1.0, 1.0, 1.0, 1.0], [2.0, None]),
        ([1.0, 1.0, 1.0, 100.0], [2.0, 10.0]),
    ):
        assert EH.chart_spans(now, hourly, daily)[0] == EH.Span(day, first, None)
    # ... and nothing when they hold only a little more (rounding)
    assert EH.chart_spans(now, [1.0, 1.0, 1.0, 15.0], [2.0, 10.0])[0] == EH.Span(
        day, first, 0.0
    )

    # no hours at all: the days run up to the end of the running hour
    assert EH.chart_spans(now, [], [4.0]) == [
        EH.Span(datetime(2026, 1, 15, 8, tzinfo=UTC), RUNNING + H, 4.0)
    ]


def spans_of(*energies: float | None) -> list[EH.Span]:
    """Hourly spans ending with the running hour, oldest first."""
    n = len(energies)
    return [
        EH.Span(RUNNING - (n - 1 - i) * H, RUNNING - (n - 2 - i) * H, e)
        for i, e in enumerate(energies)
    ]


async def test_backfill_rows() -> None:
    last = (
        RUNNING - 3 * H
    )  # 07:00 recorded; 08:00 and 09:00 missing; 10:00 the recorder's
    spans = spans_of(5.0, 100.0, 200.0, 50.0)
    assert EH.backfill_rows(spans, last, 1000.0, 1360.0, RUNNING) == [
        (RUNNING - 2 * H, 100.0),
        (RUNNING - H, 300.0),
    ]
    # the chart comes out above the counter (within the tolerance): never past what it moved
    assert EH.backfill_rows(spans, last, 1000.0, 1340.0, RUNNING + H) == [
        (RUNNING - 2 * H, 100.0),
        (RUNNING - H, 300.0),
        (RUNNING, 340.0),
    ]
    assert EH.backfill_rows(spans, last, 1000.0, 1250.0, RUNNING) is None  # too little
    assert EH.backfill_rows(spans, last, 1000.0, 1400.0, RUNNING) is None  # too much
    # the counter went back a little, the chart is empty: rows at the last reading
    assert EH.backfill_rows(
        spans_of(0.0, 0.0, 0.0, 0.0), last, 1000.0, 999.0, RUNNING
    ) == [(RUNNING - 2 * H, 0.0), (RUNNING - H, 0.0)]
    # the charts do not reach back to the last recorded hour; its hour, or one after it, has no value
    assert EH.backfill_rows(spans[1:], last, 1000.0, 1350.0, RUNNING) is None
    assert (
        EH.backfill_rows(spans_of(None, 100.0, 200.0, 50.0), last, 0.0, 350.0, RUNNING)
        is None
    )
    assert (
        EH.backfill_rows(spans_of(5.0, None, 200.0, 50.0), last, 0.0, 250.0, RUNNING)
        is None
    )
