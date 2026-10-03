"""Link-loss UX (review-3): a grace before entities go unavailable, commands waiting for the next link, a probe
when a command goes unanswered, a clean close when Home Assistant stops."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.junghome_ble import coordinator
from custom_components.junghome_ble.coordinator import JungHomeHub
from custom_components.junghome_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import ALL_LIGHTS

from .conftest import FakeProxyLink, settle, setup_entry, wait_for_link, wait_until
from .helpers import LIGHT_DIMMER, UID_LIGHT_DIMMER, entity_id

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


def sent_onoff(link: FakeProxyLink, dst: int, on: bool) -> bool:
    """Whether a Generic OnOff Set `on` went to `dst` (whatever its TID)."""
    head = M.generic_onoff_set(on, transition=0)[:3]
    return any(d == dst and pdu[:3] == head for _src, d, pdu in link.sent)


@pytest.mark.link_loss_grace
async def test_entities_stay_available_through_a_short_link_loss(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The next proxy node usually takes over within seconds: flapping every entity for that helps nobody."""
    hub = hub_of(init_integration)
    light = entity_id(hass, "light", UID_LIGHT_DIMMER)
    with patch.object(
        JungHomeHub, "visible_proxies", return_value=[]
    ):  # no proxy in range for now
        fake_link.drop_link()
        await settle(hass)
        assert not hub.connected
        assert hub.link_available
        assert hass.states.get(light).state != STATE_UNAVAILABLE
        freezer.tick(coordinator.LINK_LOSS_GRACE + 1)
        async_fire_time_changed(hass)
        await settle(hass)
        assert not hub.link_available
        assert hass.states.get(light).state == STATE_UNAVAILABLE


@pytest.mark.link_loss_grace
async def test_a_command_in_the_grace_waits_for_the_next_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub = hub_of(init_integration)
    with patch.object(JungHomeHub, "visible_proxies", return_value=[]):
        fake_link.drop_link()
        await settle(hass)
        assert not hub.connected
        fake_link.sent.clear()
        command = hass.async_create_task(hub.set_onoff(LIGHT_DIMMER, True))
        for _ in range(5):  # not `settle`: it would wait out the command's grace
            await asyncio.sleep(0)
        assert not command.done()  # waiting for the link, not failing
        hub._on_disconnect()  # another loss in the grace starts it over
    hub._link_lost.set()  # a proxy advertises again: the loop wakes
    await command
    assert hub.connected
    assert sent_onoff(fake_link, LIGHT_DIMMER, True)


async def test_an_unanswered_command_makes_the_watchdog_probe_the_link(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proxy that stopped forwarding was only noticed after LINK_IDLE_TIMEOUT of silence; every command until then
    was lost. No echo to a group command (unacknowledged: nothing to wait for) within COMMAND_ECHO_TIMEOUT probes it
    at once; no answer to the probe drops it. (A load's own command waits for its status: `test_reachability.py`.)"""
    hub = hub_of(init_integration)
    keep_alive = AsyncMock(return_value=False)
    with patch.object(JungHomeHub, "_keep_alive", keep_alive):
        await hub.central_command(ALL_LIGHTS, True)
        await hub.central_command(ALL_LIGHTS, False)  # a burst: one watch
        freezer.tick(coordinator.COMMAND_ECHO_TIMEOUT + 1)
        async_fire_time_changed(hass)
        await wait_until(hass, lambda: keep_alive.await_count >= 1, what="the probe")
        await wait_until(hass, lambda: fake_link.connect_count == 2, what="a new link")
    assert "to a command nor to a keep-alive Get; dropping the link" in caplog.text
    await wait_for_link(hass, init_integration)
    # an answered probe keeps the link
    keep_alive = AsyncMock(return_value=True)
    with patch.object(JungHomeHub, "_keep_alive", keep_alive):
        hub._probe_link.set()
        await wait_until(hass, lambda: keep_alive.await_count == 1, what="the probe")
        await settle(hass)
    assert hub.connected
    # an echo within the time: no probe
    await hub.central_command(ALL_LIGHTS, True)
    hub._last_rx += 1
    freezer.tick(coordinator.COMMAND_ECHO_TIMEOUT + 1)
    async_fire_time_changed(hass)
    await settle(hass)
    assert not hub._probe_link.is_set()


async def test_home_assistant_stopping_closes_the_link_and_the_counter(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub = hub_of(init_integration)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not hub.connected
    assert hub._stop
    assert hub.state._closed  # stored as cleanly closed: no restart margin next time


async def test_a_link_that_never_closes_does_not_hold_up_the_stop(
    init_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = hub_of(init_integration)
    monkeypatch.setattr(coordinator, "STOP_TIMEOUT", 0.01)

    async def hang(*_a: Any, **_k: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(hub.proxy, "detach", hang)
    await hub.async_stop()
    assert "did not close within" in caplog.text


def test_the_next_utc_offset_change_is_found_to_the_second() -> None:
    """Review-3 F17: the Time Set after a daylight-saving change needs its moment; zoneinfo does not tell it."""
    berlin = ZoneInfo("Europe/Berlin")
    before = datetime(2021, 3, 1, 12, 0, tzinfo=berlin)
    assert coordinator.next_utc_offset_change(before) == datetime(
        2021, 3, 28, 1, 0, tzinfo=UTC
    )
    assert coordinator.next_utc_offset_change(
        datetime(2021, 4, 1, tzinfo=berlin)
    ) == datetime(2021, 10, 31, 1, 0, tzinfo=UTC)
    assert coordinator.next_utc_offset_change(datetime(2021, 4, 1, tzinfo=UTC)) is None


async def test_a_time_set_follows_a_daylight_saving_change(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    fake_link: FakeProxyLink,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fast_sleep: list[float],
) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(datetime(2021, 3, 28, 0, 59, 0, tzinfo=UTC))
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    fake_link.sent.clear()
    freezer.move_to(datetime(2021, 3, 28, 1, 0, 10, tzinfo=UTC))
    async_fire_time_changed(hass)
    await settle(hass)
    time_sets = [
        pdu for _src, dst, pdu in fake_link.sent if dst == 0xFFFF and pdu[:1] == b"\x5c"
    ]
    assert len(time_sets) == 1
    hub = mock_config_entry.runtime_data
    assert hub._unsub_offset_change is not None  # the autumn change is armed next


async def test_no_daylight_saving_no_extra_time_set(
    hass: HomeAssistant,
    fake_link: FakeProxyLink,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fast_sleep: list[float],
) -> None:
    await hass.config.async_set_time_zone("UTC")
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    assert mock_config_entry.runtime_data._unsub_offset_change is None


def light_available(hass: HomeAssistant) -> bool:
    return (
        hass.states.get(entity_id(hass, "light", UID_LIGHT_DIMMER)).state
        != STATE_UNAVAILABLE
    )


async def spin(cycles: int = 5) -> None:
    """A few loop turns; not `settle`, which would wait out a command's grace."""
    for _ in range(cycles):
        await asyncio.sleep(0)


async def reconnect(hass: HomeAssistant, hub: JungHomeHub) -> None:
    """A proxy advertises again (the patch hiding them is gone): the loop wakes and connects."""
    hub._link_lost.set()
    await wait_until(hass, lambda: hub.connected, what="the next link")


@pytest.mark.link_loss_grace
async def test_a_watchdog_drop_keeps_the_grace_and_a_command_waits(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 R4-3: only a transport's disconnect started the grace; a link the hub dropped itself — the watchdog
    finding the proxy silent — made every entity unavailable at once, and an action skipped them."""
    hub = hub_of(init_integration)
    with (
        patch.object(JungHomeHub, "visible_proxies", return_value=[]),
        patch.object(JungHomeHub, "_keep_alive", AsyncMock(return_value=False)),
    ):
        hub._probe_link.set()  # a command went unanswered, and so does the probe
        await wait_until(hass, lambda: not hub.connected, what="the drop")
        await spin()
        assert hub.link_available
        assert light_available(hass)
        fake_link.sent.clear()
        command = hass.async_create_task(hub.set_onoff(LIGHT_DIMMER, True))
        await spin()
        assert not command.done()  # waiting for the next link, not failing
    await reconnect(hass, hub)
    await command
    assert sent_onoff(fake_link, LIGHT_DIMMER, True)


@pytest.mark.link_loss_grace
async def test_the_skip_ahead_repair_keeps_the_grace_and_a_command_waits(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The reviewer's repro: right after `async_skip_ahead` every entity was unavailable."""
    hub = hub_of(init_integration)
    with patch.object(JungHomeHub, "visible_proxies", return_value=[]):
        await hub.async_skip_ahead()
        await spin()
        assert not hub.connected
        assert hub.link_available
        assert light_available(hass)
        fake_link.sent.clear()
        command = hass.async_create_task(hub.set_onoff(LIGHT_DIMMER, True))
        await spin()
        assert not command.done()
    await reconnect(hass, hub)
    await command
    assert sent_onoff(fake_link, LIGHT_DIMMER, True)
    # our own reconnect says nothing about the proxy
    assert hub._short_links == {}


@pytest.mark.link_loss_grace
async def test_a_link_the_transport_closed_without_telling_gets_the_grace(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """A transport that only turns `is_connected` False (no disconnected callback) still ends the link properly."""
    hub = hub_of(init_integration)
    with patch.object(JungHomeHub, "visible_proxies", return_value=[]):
        fake_link.is_connected = False
        info = mock_bluetooth_env["infos"][0]
        mock_bluetooth_env["callbacks"][0](info, BluetoothChange.ADVERTISEMENT)
        await wait_until(hass, lambda: hub.proxy_address is None, what="the end")
        await spin()
        assert hub.link_available
        assert light_available(hass)
        assert hub.proxy.client is None  # released, not left attached
    assert hub._link_end is not None
    assert hub._link_end.reason == "the transport reported it closed"
    assert hub._link_end.penalise is None
    await reconnect(hass, hub)


async def test_link_loss_listeners_hear_every_end_once(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    hub = hub_of(init_integration)
    ends: list[coordinator.LinkEnd] = []
    unsub = hub.async_on_link_loss(ends.append)
    fake_link.drop_link()
    hub._on_disconnect()  # a second notice of the same end changes nothing
    await wait_for_link(hass, init_integration)
    assert [(end.reason, end.penalise) for end in ends] == [
        ("the proxy disconnected", None)
    ]
    assert ends[0].lasted >= 0
    unsub()
    fake_link.drop_link()
    await wait_for_link(hass, init_integration, connected=False)
    await wait_for_link(hass, init_integration)
    assert len(ends) == 1  # unsubscribed


@pytest.mark.link_loss_grace
async def test_a_command_interrupted_by_a_link_change_goes_out_on_the_next_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 R I-11: a link lost while a load's Set waited for its status failed the command; it says nothing
    about the load, and the Set goes out once more on the next link (same TID: a load that applied it only
    answers)."""
    hub = hub_of(init_integration)
    fake_link.sets_silent.add(LIGHT_DIMMER)  # the first Set stays unanswered ...
    fake_link.sent.clear()
    # not a task Home Assistant tracks: `wait_until` would wait the unanswered attempts out
    command = asyncio.get_running_loop().create_task(hub.set_onoff(LIGHT_DIMMER, True))
    for _ in range(100):
        if sent_onoff(fake_link, LIGHT_DIMMER, True):
            break
        await asyncio.sleep(0)
    assert sent_onoff(fake_link, LIGHT_DIMMER, True)
    fake_link.sets_silent.clear()  # ... until the link it went out on is lost
    fake_link.drop_link()
    await command
    assert hub.connected
    sets = [
        pdu
        for _src, dst, pdu in fake_link.sent
        if dst == LIGHT_DIMMER and pdu[:2] == M.generic_onoff_set(True)[:2]
    ]
    assert len(sets) >= 2
    assert len({bytes(pdu) for pdu in sets}) == 1  # the same message, TID and all
    assert hub.states[LIGHT_DIMMER].on is True


async def test_a_detach_that_never_returns_does_not_hold_up_the_drop(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = hub_of(init_integration)
    detach = hub.proxy.detach

    async def detach_then_hang(*args: Any, **kwargs: Any) -> None:
        await detach(*args, **kwargs)
        await asyncio.Event().wait()

    monkeypatch.setattr(coordinator, "STOP_TIMEOUT", 0.01)
    monkeypatch.setattr(hub.proxy, "detach", detach_then_hang)
    await hub.async_skip_ahead()
    assert "did not close within" in caplog.text
    monkeypatch.setattr(hub.proxy, "detach", detach)
    await wait_for_link(hass, init_integration)


async def test_the_link_history_tells_why_the_last_links_ended(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 R I-9: nothing recorded why a link ended. The last LINK_HISTORY links are kept, oldest first, each
    with its proxy (by mesh address), length, reason, how long its refresh took and how long sends were held back;
    the diagnostics show them newest first."""
    hub = hub_of(init_integration)
    assert list(hub.link_history) == []
    assert hub.link_history.maxlen == coordinator.LINK_HISTORY == 20
    with patch.object(JungHomeHub, "_refresh_all", AsyncMock(return_value=True)):
        fake_link.drop_link()  # the proxy went away
        await wait_for_link(hass, init_integration, connected=False)
        await wait_for_link(hass, init_integration)
        await wait_until(
            hass, lambda: hub._link_refresh is not None, what="the second refresh"
        )
        await hub.async_skip_ahead()  # we end the second link ourselves
        await wait_for_link(hass, init_integration)
    first, second = hub.link_history
    assert (first.reason, first.penalise) == ("the proxy disconnected", None)
    assert (second.reason, second.penalise) == ("sequence numbers skipped ahead", False)
    assert first.proxy_node == second.proxy_node == 0x0148
    assert second.refresh is not None
    assert 0 <= second.refresh <= second.lasted
    assert first.held_back == second.held_back == 0
    assert first.ended <= second.ended

    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)
    history = diagnostics["link"]["history"]
    assert [(h["reason"], h["penalised"], h["proxy_node"]) for h in history] == [
        ("sequence numbers skipped ahead", False, "0148"),
        ("the proxy disconnected", None, "0148"),
    ]
    assert set(history[0]) == {
        "proxy_node",
        "ended_ago",
        "lasted",
        "reason",
        "penalised",
        "refresh",
        "held_back",
    }
    assert history[0]["refresh"] is not None
    assert history[0]["ended_ago"] <= history[1]["ended_ago"]


async def test_a_link_lost_before_its_refresh_records_none(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """A link that went before its state refresh got through has no refresh time."""
    hub = hub_of(init_integration)
    with patch.object(JungHomeHub, "_refresh_all", AsyncMock(return_value=False)):
        fake_link.drop_link()
        await wait_for_link(hass, init_integration, connected=False)
        await wait_for_link(hass, init_integration)
        fake_link.drop_link()
        await wait_for_link(hass, init_integration, connected=False)
    assert len(hub.link_history) == 2
    assert hub.link_history[1].refresh is None
