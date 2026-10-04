"""The hub's clock component (`hub/clock.py`): Time Set and the home location broadcast to the nodes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, PropertyMock, patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble.const import TIME_SET_INTERVAL
from custom_components.junghome_ble.coordinator import JungHomeHub
from custom_components.junghome_ble.jhmesh.pdu import ALL_NODES

from .conftest import (
    FakeProxyLink,
    settle,
)
from .test_coordinator import (
    BROADCASTS,
    assert_time_set,
    hub_of,
)
from .test_coordinator import (
    answering_link as answering_link,  # noqa: PLC0414  # the fixture
)
from .test_coordinator import (
    init_answered as init_answered,  # noqa: PLC0414  # the fixture
)
from .test_coordinator import (
    no_property_reads as no_property_reads,  # noqa: PLC0414  # the autouse fixture
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant


async def test_time_set_repeats_daily_while_connected(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Device timers have no other clock: the gateway never publishes time, so we do, once a day and after every connection."""
    hub = hub_of(init_answered)
    assert [dst for _, dst, _ in fake_link.sent].count(
        ALL_NODES
    ) == BROADCASTS  # the Time Set and location after the refresh
    assert hub.lifecycle.timer("energy") is not None
    hub.lifecycle.timer(
        "energy"
    )()  # a day of ticks would also fire the energy poll; it has its own tests
    hub.lifecycle.set_timer("energy", None)
    fake_link.sent.clear()

    # day-long jumps would trip the link watchdog (a silent proxy is another test); keep it out of the way; the
    # clock reads that follow the daily Time Set have their own tests (test_node_clocks.py)
    with (
        patch(
            "custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT",
            10 * TIME_SET_INTERVAL,
        ),
        patch.object(hub.clocks, "read_all", AsyncMock(return_value=True)) as reads,
    ):
        freezer.tick(TIME_SET_INTERVAL - 60)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not fake_link.sent  # not yet

        freezer.tick(120)
        async_fire_time_changed(hass)
        await settle(hass)
        assert len(fake_link.sent) == 1
        assert_time_set(fake_link.sent[0], dt_util.now())
        reads.assert_awaited_once()  # the nodes' clocks are read after it

        # nothing goes out while the link is down, and a failing send is only logged
        fake_link.sent.clear()
        with patch.object(
            JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
        ):
            freezer.tick(TIME_SET_INTERVAL)
            async_fire_time_changed(hass)
            await settle(hass)
        assert not fake_link.sent

    fake_link.write_error = ConnectionError("gone")
    await hub.clock.send_time()
    await hub.clock.send_location()
    assert not fake_link.sent


async def test_time_set_falls_back_to_utc_for_an_odd_zone(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A zone offset Time Set cannot carry (not a quarter hour) still gets the devices a UTC clock."""
    odd = datetime(2026, 1, 15, 12, 0, tzinfo=timezone(timedelta(minutes=7)))
    fake_link.sent.clear()
    with patch(
        "custom_components.junghome_ble.coordinator.dt_util.now", return_value=odd
    ):
        await hub_of(init_answered).clock.send_time()
    assert len(fake_link.sent) == 1
    assert_time_set(fake_link.sent[0], odd, zone=timedelta(0))


async def test_time_set_goes_out_before_the_refresh(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Review-4 R I-5: Time Set and the location follow the proxy filter, before the refresh — sent after a complete
    refresh, they never went out on a link lost before it was through. A refresh cut short sends nothing more."""
    hub = hub_of(init_answered)
    fake_link.sent.clear()
    with patch.object(hub.refresh, "_refresh_all", AsyncMock(return_value=False)):
        await hub.refresh.after_connect()
    assert [dst for _, dst, _ in fake_link.sent] == [ALL_NODES] * BROADCASTS
    assert_time_set(fake_link.sent[0], dt_util.now())
    # a link already gone: nothing goes out, and nothing is raised
    fake_link.sent.clear()
    fake_link.write_error = ConnectionError("proxy disconnected")
    await hub.refresh.after_connect()
    assert not fake_link.sent
