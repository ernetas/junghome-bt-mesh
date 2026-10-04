"""Link diagnostics (review-3 F8, F9, N2): last seen, signal, hops, restarts per node; IV index and sequence space."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.junghome_ble.const import (
    ISSUE_SEQUENCE_SPACE_LOW,
    NODE_DIAGNOSTICS_INTERVAL,
    SIGNAL_CONNECTION,
)
from custom_components.junghome_ble.coordinator import (
    SEQUENCE_CHECK_INTERVAL,
    JungHomeHub,
)
from custom_components.junghome_ble.hub.issues import (
    SEQUENCE_SPACE_WARN,
)
from custom_components.junghome_ble.sensor import MESH_DIAGNOSTICS

from .conftest import PROXY_ADDRESS, FakeProxyLink, make_service_info, settle
from .helpers import (
    LIGHT_SWITCH,
    NODE_LIGHT_SWITCH,
    OUR_ADDRESS,
    entity_id,
    find_issue,
    onoff_status,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry


UUID = NODE_LIGHT_SWITCH.lower()
GROUP_SWITCH = 0xC061  # the group LIGHT_SWITCH publishes its status to


def value(hass: HomeAssistant, key: str) -> str:
    state = hass.states.get(entity_id(hass, "sensor", f"{UUID}-{key}"))
    assert state is not None
    return state.state


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


@pytest.fixture
async def diagnostics(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entity_registry_enabled_by_default: None,
    init_integration: MockConfigEntry,
) -> MockConfigEntry:
    """The integration with every diagnostic enabled, a minute after its start (the rate limit is past)."""
    freezer.tick(NODE_DIAGNOSTICS_INTERVAL + 1)
    return init_integration


async def send_from(link: FakeProxyLink, src: int, seq: int) -> None:
    """Deliver an OnOff Status from `src` with sequence number `seq`."""
    link.src_seq[src] = seq - 1
    link.inject(src, GROUP_SWITCH, onoff_status(True))


async def test_last_seen_follows_messages_at_most_once_a_minute(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    diagnostics: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await settle(hass)
    first = value(hass, "last_seen")
    assert first not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    freezer.tick(10)
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(False))
    await settle(hass)
    assert value(hass, "last_seen") == first  # not pushed again within the minute ...
    freezer.tick(NODE_DIAGNOSTICS_INTERVAL)
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await settle(hass)
    assert value(hass, "last_seen") != first  # ... after it
    # a time stays valid without a link; a signal strength does not
    with patch.object(
        JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
    ):
        async_dispatcher_send(hass, SIGNAL_CONNECTION.format(diagnostics.entry_id))
        await hass.async_block_till_done()
        assert value(hass, "last_seen") not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        assert value(hass, "rssi") == STATE_UNAVAILABLE


async def test_signal_strength_comes_from_the_nodes_advertisements(
    hass: HomeAssistant,
    diagnostics: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    mock_bluetooth_env["callbacks"][0](
        make_service_info(network_id, address=PROXY_ADDRESS, rssi=-71),
        BluetoothChange.ADVERTISEMENT,
    )
    await settle(hass)
    assert value(hass, "rssi") == "-71"
    assert hub_of(diagnostics).node_rssi == {LIGHT_SWITCH: -71}


async def test_hops_come_from_heartbeats(
    hass: HomeAssistant, diagnostics: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    assert value(hass, "hops") == STATE_UNKNOWN
    fake_link.inject_heartbeat(LIGHT_SWITCH, OUR_ADDRESS, init_ttl=5, ttl=3)
    await settle(hass)
    assert value(hass, "hops") == "2"
    fake_link.inject_heartbeat(0x0999, OUR_ADDRESS)  # no node of the export: ignored
    await settle(hass)


async def test_a_jump_into_a_fresh_block_of_the_counter_is_a_restart(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    diagnostics: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JUNG firmware continues from the next 0x10000 block after a restart (seen on two nodes)."""
    hub = hub_of(diagnostics)
    assert value(hass, "last_restart") == STATE_UNKNOWN
    await send_from(fake_link, LIGHT_SWITCH, 0x212340)
    await send_from(fake_link, LIGHT_SWITCH, 0x220003)
    await settle(hass)
    assert "restarted (its sequence number jumped from 212340 to 220003)" in caplog.text
    restarted = value(hass, "last_restart")
    assert restarted not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    # another element of the same node jumping with it is the same restart
    first = hub.restarted[LIGHT_SWITCH]
    freezer.tick(5)
    hub._note_seq(0x0149, 0x212000)  # the node's second element (its own counter)
    hub._note_seq(0x0149, 0x220001)
    assert hub.restarted[LIGHT_SWITCH] == first
    # the counter running on into the next block is no restart; nor is a source outside the export
    freezer.tick(NODE_DIAGNOSTICS_INTERVAL + 1)
    await send_from(fake_link, LIGHT_SWITCH, 0x23FFF0)
    await send_from(fake_link, LIGHT_SWITCH, 0x240002)
    assert hub.restarted[LIGHT_SWITCH] == first
    hub._note_seq(0x0999, 0x010)
    hub._note_seq(0x0999, 0x020001)
    assert 0x0999 not in hub.restarted
    # a lower number (a new IV index) only restarts the tracking
    hub._note_seq(LIGHT_SWITCH, 0x000010)
    assert hub._last_seq[LIGHT_SWITCH] == 0x000010


async def test_the_mesh_diagnostics(
    hass: HomeAssistant, diagnostics: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(diagnostics)
    iv = hass.states.get("sensor.jung_home_mesh_test_iv_index")
    assert iv is not None
    assert iv.state == "0"
    assert iv.attributes["iv_update_active"] is False
    await send_from(fake_link, LIGHT_SWITCH, 0x7FFFFF)
    await settle(hass)
    async_fire_time_changed(hass, fire_all=True)
    await settle(hass)
    used = hass.states.get("sensor.jung_home_mesh_test_mesh_sequence_numbers_used")
    assert used is not None
    assert used.state == "50.0"
    assert used.attributes["source"] == f"{LIGHT_SWITCH:04X}"
    ours = hass.states.get("sensor.jung_home_mesh_test_sequence_numbers_used")
    assert ours is not None
    assert ours.attributes["source"] == f"{OUR_ADDRESS:04X}"
    # during an IV Update we still transmit under the old index and nothing is known under the new one yet
    with patch.object(hub.proxy.state, "rpl", {}):
        hub.proxy.state.iv_update_active = True
        hub.proxy.state.iv_index += 1
        try:
            assert hub.highest_seq() is None
            description = next(
                d for d in MESH_DIAGNOSTICS if d.key == "mesh_sequence_used"
            )
            assert description.value(hub) is None
            assert description.attributes(hub) == {}
        finally:
            hub.proxy.state.iv_index -= 1
            hub.proxy.state.iv_update_active = False


async def test_sequence_space_running_low_raises_an_issue(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    diagnostics: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Every source stops at the end of the 24-bit space until an IV Update, which Home Assistant does not start."""
    hub = hub_of(diagnostics)
    await send_from(fake_link, LIGHT_SWITCH, SEQUENCE_SPACE_WARN + 5)
    freezer.tick(SEQUENCE_CHECK_INTERVAL + 1)
    async_fire_time_changed(hass)
    await settle(hass)
    issue = find_issue(hass, ISSUE_SEQUENCE_SPACE_LOW)
    assert issue is not None
    assert issue.translation_placeholders == {
        "title": "JUNG HOME mesh test",
        "source": f"Push-button 1-gang {LIGHT_SWITCH:04X}",
        "percent": "75",
        "iv_index": "0",
    }
    # a source outside the export is named by its address
    # (well above the others: the answers to the connect-time reads keep coming from the fake mesh meanwhile)
    hub.proxy.state.rpl[0x0999] = (0, SEQUENCE_SPACE_WARN + 0x1000)
    hub.issues.check_sequence_space()
    issue = find_issue(hass, ISSUE_SEQUENCE_SPACE_LOW)
    assert issue is not None
    assert issue.translation_placeholders["source"] == "0999"
    # an IV Update later everyone starts over: the issue goes
    hub.proxy.state.rpl.clear()
    hub.issues.check_sequence_space()
    assert find_issue(hass, ISSUE_SEQUENCE_SPACE_LOW) is None
    # and it goes with the unload too
    hub.proxy.state.rpl[0x0999] = (0, SEQUENCE_SPACE_WARN)
    hub.issues.check_sequence_space()
    assert find_issue(hass, ISSUE_SEQUENCE_SPACE_LOW) is not None
    assert await hass.config_entries.async_unload(diagnostics.entry_id)
    await hass.async_block_till_done()
    assert find_issue(hass, ISSUE_SEQUENCE_SPACE_LOW) is None


async def test_the_last_restart_is_signalled_at_once(
    hass: HomeAssistant, diagnostics: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A restart pushes the node's diagnostics even inside the minute a message just pushed them."""
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await settle(hass)
    await send_from(fake_link, LIGHT_SWITCH, 0x5A0000 + 5)
    await send_from(fake_link, LIGHT_SWITCH, 0x5B0000 + 1)
    await settle(hass)
    assert value(hass, "last_restart") not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
