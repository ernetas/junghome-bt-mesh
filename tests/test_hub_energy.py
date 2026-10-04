"""The hub's energy component (`hub/energy.py`): the metered loads' polls, on-demand reads and the poll timer."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import PropertyMock, patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble.const import ENERGY_POLL_INTERVAL
from custom_components.junghome_ble.coordinator import JungHomeHub
from custom_components.junghome_ble.jhmesh import messages as M

from .conftest import (
    FakeProxyLink,
    settle,
    wait_for_link,
)
from .helpers import (
    OUR_ADDRESS,
    PROPERTY_POWER_ON_TIME,
    SENSOR_CURRENT,
    SENSOR_POWER,
    SENSOR_VOLTAGE,
    SOCKET,
    SOCKET_SENSOR,
    UID_SOCKET,
    admin_property_status,
    entity_id,
)
from .test_coordinator import (
    AFTER_ENERGY,
    CONNECT_TAIL,
    COUNTER_GETS,
    ENERGY_GETS,
    HOURS_GET,
    STATE_REPLIES,
    hub_of,
    never_done,
    tick,
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
    import pytest
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant


async def test_energy_poll_repeats_while_connected(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The counter is read after the connect-time refresh and every ENERGY_POLL_INTERVAL while a link is up."""
    hub = hub_of(init_answered)
    assert fake_link.sent[-AFTER_ENERGY:-CONNECT_TAIL] == COUNTER_GETS
    assert hub.states[SOCKET].power_on_hours == 42
    fake_link.sent.clear()

    # the poll interval is as long as the link watchdog's patience (a silent proxy is another test); keep it out of the way
    with patch(
        "custom_components.junghome_ble.coordinator.LINK_IDLE_TIMEOUT",
        10 * ENERGY_POLL_INTERVAL,
    ):
        freezer.tick(ENERGY_POLL_INTERVAL - 60)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not fake_link.sent  # not yet

        STATE_REPLIES[HOURS_GET] = admin_property_status(
            PROPERTY_POWER_ON_TIME, (43).to_bytes(3, "little")
        )
        try:
            freezer.tick(120)
            async_fire_time_changed(hass)
            await settle(hass)
        finally:
            STATE_REPLIES[HOURS_GET] = admin_property_status(
                PROPERTY_POWER_ON_TIME, (42).to_bytes(3, "little")
            )
        assert fake_link.sent == COUNTER_GETS
        assert hub.states[SOCKET].power_on_hours == 43
        assert hub.energy.task is not None
        assert hub.energy.task.done()

        # nothing is asked while the link is down
        fake_link.sent.clear()
        with patch.object(
            JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
        ):
            freezer.tick(ENERGY_POLL_INTERVAL)
            async_fire_time_changed(hass)
            await settle(hass)
        assert not fake_link.sent

        # ... nor while a poll (or the connect-time refresh that ends with one) is still running
        hub.energy.task = running = never_done(hass)
        freezer.tick(ENERGY_POLL_INTERVAL)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not fake_link.sent
        assert hub.energy.task is running
        running.cancel()
        await settle(hass)

        # stopping the hub unsubscribes the timer and cancels a running poll
        hub.energy.task = task = never_done(hass)
        await hub.async_stop()
        assert task.cancelled()
        assert hub.energy.task is None
        assert hub.energy.unsub_energy is None
        fake_link.sent.clear()
        freezer.tick(ENERGY_POLL_INTERVAL)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not fake_link.sent


async def test_update_entity_reads_the_meter_now(
    hass: HomeAssistant, init_answered: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """`homeassistant.update_entity` on a meter sensor reads the socket's readings and counters at once, not at the
    next ENERGY_POLL_INTERVAL: the app's consumption page reads them every 5 s (`requestData`)."""
    hub = hub_of(init_answered)
    readings = [
        (OUR_ADDRESS, SOCKET_SENSOR, M.sensor_get(pid))
        for pid in (SENSOR_POWER, SENSOR_VOLTAGE, SENSOR_CURRENT)
    ]
    STATE_REPLIES[HOURS_GET] = admin_property_status(
        PROPERTY_POWER_ON_TIME, (44).to_bytes(3, "little")
    )
    try:
        fake_link.sent.clear()
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": entity_id(hass, "sensor", f"{UID_SOCKET}-power")},
            blocking=True,
        )
        await settle(hass)
    finally:
        STATE_REPLIES[HOURS_GET] = admin_property_status(
            PROPERTY_POWER_ON_TIME, (42).to_bytes(3, "little")
        )
    assert fake_link.sent == readings + COUNTER_GETS
    assert hub.states[SOCKET].power_on_hours == 44

    # asked twice at once (every sensor of the load updated together): one read, the second call waits for it
    socket = hub.devices.by_address[SOCKET]
    gate = asyncio.Event()
    real_readings = hub.energy.get_readings

    async def held(load: Any) -> None:
        await gate.wait()
        await real_readings(load)

    fake_link.sent.clear()
    with patch.object(hub.energy, "get_readings", held):
        first = hass.async_create_task(hub.async_refresh_meter(socket))
        await asyncio.sleep(0)
        second = hass.async_create_task(hub.async_refresh_meter(socket))
        await asyncio.sleep(0)
        assert not second.done()
        gate.set()
        await first
        await second
    assert fake_link.sent == readings + COUNTER_GETS
    assert not hub.energy._meter_refreshes

    # no link: nothing raised, the cached values stay
    hub.states[SOCKET].power_on_hours = 44
    fake_link.write_error = OSError("GATT write failed")
    try:
        await hub.async_refresh_meter(socket)
    finally:
        fake_link.write_error = None
    assert hub.states[SOCKET].power_on_hours == 44
    assert not hub.energy._meter_refreshes


async def test_energy_poll_is_cancelled_with_the_link(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """A poll that the link outlives is cancelled with the link's refresh, and a failing send is only logged."""
    hub = hub_of(init_answered)
    hub.energy.task = task = never_done(hass)
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert task.cancelled()
    assert hub.energy.task is None

    fake_link.sent.clear()
    fake_link.write_error = ConnectionError("proxy disconnected")
    await hub.energy.poll()  # the link went away between the tick and the send
    assert not fake_link.sent


async def test_energy_poll_survives_an_unanswered_socket(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A socket that does not answer costs one attempt per counter (no retries), then the poll moves on."""
    hub = hub_of(init_answered)
    replies = {get: STATE_REPLIES.pop(get) for _src, _dst, get in COUNTER_GETS}
    fake_link.sent.clear()
    try:
        poll = hass.async_create_background_task(hub.energy.poll(), "poll")
        await settle(hass)
        assert fake_link.sent == COUNTER_GETS[:1]
        for n in range(
            2, len(COUNTER_GETS) + 1
        ):  # each timeout moves on to the next counter, no retry
            freezer.tick(3.1)
            async_fire_time_changed(hass)
            await settle(hass)
            assert fake_link.sent == COUNTER_GETS[:n]
        freezer.tick(3.1)
        async_fire_time_changed(hass)
        await settle(hass)
        assert fake_link.sent == COUNTER_GETS
        assert poll.done()
    finally:
        STATE_REPLIES.update(replies)
    assert "0172 did not answer its property Get 006D" in caplog.text
    assert "0173 did not answer its property Get 0072" in caplog.text
    assert hub.states[SOCKET].power_on_hours == 42  # the connect-time values stay
    assert hub.states[SOCKET].energy_wh == 210198


async def test_energy_poll_is_anchored_on_the_connection(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_answered: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The poll grid restarts with every link: the connect-time poll is tick 0, the next one ENERGY_POLL_INTERVAL later,
    wherever the integration's own start was (so a poll never lands right at the link watchdog's deadline)."""
    hub = hub_of(init_answered)
    timer = hub.energy.unsub_energy
    assert timer is not None
    await tick(hass, freezer, 200)
    fake_link.drop_link()
    await settle(hass)
    await wait_for_link(hass, init_answered)
    assert fake_link.connect_count == 2
    assert hub.energy.unsub_energy is not None
    assert (
        hub.energy.unsub_energy is not timer
    )  # re-armed by the new link, the old timer is gone
    # the connect-time poll of the new link; the scene and fault reads of the first, a link that held, are fresh
    assert fake_link.sent[-ENERGY_GETS:] == COUNTER_GETS
    fake_link.sent.clear()

    await tick(
        hass, freezer, ENERGY_POLL_INTERVAL - 60
    )  # 500 s after the start: the old grid would poll now
    assert not fake_link.sent
    await tick(
        hass, freezer, 120
    )  # 360 s after the new link came up: the new grid does
    assert fake_link.sent == COUNTER_GETS


async def test_energy_poll_skips_meshes_without_a_metering_socket(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Without a metering socket there is nothing to poll, so no connection arms a timer."""
    hub = hub_of(init_integration)
    assert hub.energy.unsub_energy is not None
    await hub.async_stop()
    hub.devices.sockets.clear()
    hub.stopping = False
    await hub.async_start()
    await wait_for_link(hass, init_integration)
    assert hub.connected
    assert hub.energy.unsub_energy is None
    await hub.async_stop()
