"""Switch platform: metering sockets, on/off device parameters, the key status LED and the LED night mode."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.number import SERVICE_SET_VALUE
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.select import SERVICE_SELECT_OPTION
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_OPTION,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.junghome_ble.config_entities import (
    LOCK_EXPIRY_MARGIN,
)
from custom_components.junghome_ble.const import (
    DOMAIN,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.number import (
    LOCK_TIME_LIMIT_MAX,
)
from custom_components.junghome_ble.switch import lock_mode

from . import property_helpers as ph
from .conftest import FakeProxyLink, settle, setup_entry, wait_for_link, wait_until
from .helpers import (
    BUTTON_WC,
    GATEWAY,
    LIGHT_CTL,
    LIGHT_SWITCH,
    OUR_ADDRESS,
    ROCKER_A,
    ROCKER_B,
    SOCKET,
    UID_BUTTON_WC,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    UID_ROCKER_A,
    UID_ROCKER_B,
    UID_SOCKET,
    entity_id,
    onoff_status,
)
from .property_helpers import (
    PID_AUTO_DST,
    PID_LED1_OFF,
    PID_LED1_ON,
    PID_LED2_OFF,
    PID_LED2_ON,
    PID_STATUS_LED,
    PropertyMesh,
    vendor_status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

fast_timeouts, mesh, init_with_mesh = ph.fast_timeouts, ph.mesh, ph.init_with_mesh

UID_AUTO_DST = f"{UID_LIGHT_SWITCH}-automatic_dst"
UID_STATUS_LED = f"{UID_BUTTON_WC}-key_status_led"
UID_NIGHT_MODE = f"{UID_LIGHT_SWITCH}-led_night_mode"
UID_ROCKER_NIGHT_MODE = f"{UID_LIGHT_CTL}-led_night_mode"


async def test_entity(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    eid = entity_id(hass, "switch", UID_SOCKET)
    assert eid == "switch.kitchen_boiler"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_DEVICE_CLASS] == SwitchDeviceClass.OUTLET
    assert state.attributes["mesh_address"] == "0172"
    assert state.attributes["rooms"] == ["Kitchen"]
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.device_id is not None
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device is not None
    assert device.identifiers == {(DOMAIN, UID_SOCKET)}


async def test_turn_on_off(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "switch", UID_SOCKET)
    fake_link.sent.clear()

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    src, dst, pdu = fake_link.sent[-1]
    assert (src, dst) == (OUR_ADDRESS, SOCKET)
    assert pdu == M.generic_onoff_set(True, tid=pdu[3], transition=0)

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    src, dst, pdu = fake_link.sent[-1]
    assert (src, dst) == (OUR_ADDRESS, SOCKET)
    assert pdu == M.generic_onoff_set(False, tid=pdu[3], transition=0)
    assert len(fake_link.sent) == 2

    fake_link.inject(SOCKET, 0xC000, onoff_status(True))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_ON
    fake_link.inject(SOCKET, 0xC000, onoff_status(False))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_OFF


async def test_send_failure_raises(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "switch", UID_SOCKET)
    fake_link.write_error = ConnectionError("proxy disconnected")
    for service in (SERVICE_TURN_ON, SERVICE_TURN_OFF):
        with pytest.raises(HomeAssistantError) as exc:
            await hass.services.async_call(
                SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: eid}, blocking=True
            )
        assert exc.value.translation_domain == DOMAIN
        assert exc.value.translation_key == "send_failed"


async def test_availability_follows_the_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    eid = entity_id(hass, "switch", UID_SOCKET)
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get(eid).state == STATE_UNAVAILABLE

    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, init_integration)
    assert hass.states.get(eid).state == STATE_UNKNOWN


async def test_update_entity_asks_the_socket_now(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 H4-10: `homeassistant.update_entity` sends the socket a Generic OnOff Get; it shows the answer."""
    eid = entity_id(hass, "switch", UID_SOCKET)
    fake_link.inject(SOCKET, 0xC000, onoff_status(True))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_ON
    fake_link.sent.clear()
    await hass.services.async_call(
        "homeassistant", "update_entity", {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert [(dst, pdu) for _, dst, pdu in fake_link.sent] == [
        (SOCKET, M.generic_onoff_get())
    ]
    assert hass.states.get(eid).state == STATE_OFF  # what the socket answered


# --------------------------------------------------------------------------- config switches


async def test_property_switch_read_and_written(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    mesh.values[LIGHT_SWITCH, PID_AUTO_DST] = b"\x01"
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "switch", UID_AUTO_DST)
    assert eid == "switch.wc_wc_mirror_time_change_active"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_ON
    assert ATTR_ASSUMED_STATE not in state.attributes
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG
    assert (
        OUR_ADDRESS,
        LIGHT_SWITCH,
        M.vendor_property_get("admin", PID_AUTO_DST),
    ) in mesh.link.sent

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    # `C3 27 05 [0F 00][03][00]`
    assert mesh.link.sent[-1] == (
        OUR_ADDRESS,
        LIGHT_SWITCH,
        M.vendor_property_set("admin", PID_AUTO_DST, b"\x00"),
    )
    assert hass.states.get(eid).state == STATE_OFF
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert mesh.sets[-1] == (LIGHT_SWITCH, PID_AUTO_DST, b"\x01")
    assert hass.states.get(eid).state == STATE_ON

    # a Status without a value — id + access, or the id alone — is dropped, as the app's resolver drops it: the
    # value it had stays
    mesh.link.inject(LIGHT_SWITCH, 0xC061, vendor_status(0x05, PID_AUTO_DST, b""))
    mesh.link.inject(LIGHT_SWITCH, 0xC061, vendor_status(0x05, PID_AUTO_DST, b"")[:-1])
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_ON
    mesh.link.inject(
        LIGHT_SWITCH, 0xC061, vendor_status(0x05, 0x1234, b"\x01")
    )  # a property nobody catalogued: not cached either
    await hass.async_block_till_done()
    assert 0x1234 not in mock_config_entry.runtime_data.states[LIGHT_SWITCH].properties


async def test_set_answered_without_a_value_is_not_supported(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """The element answers a Set with the property id alone (the socket's meter to 0x5003 on air): no re-read.

    As in the app, the answer completes the Set — no resend, no read-back — and the cached value stays; HA reports
    that the device does not have the setting instead of the app's silence.
    """
    mesh.values[LIGHT_SWITCH, PID_AUTO_DST] = b"\x01"
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "switch", UID_AUTO_DST)
    assert hass.states.get(eid).state == STATE_ON
    mesh.unsupported.add((LIGHT_SWITCH, PID_AUTO_DST))
    gets, sets = len(mesh.gets), len(mesh.sets)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: eid}, blocking=True
        )
    assert err.value.translation_key == "setting_not_supported"
    assert len(mesh.sets) == sets + 1  # one Set, not resent
    assert len(mesh.gets) == gets  # and not read back
    assert hass.states.get(eid).state == STATE_ON


async def test_status_led_is_written_with_a_user_property_status(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, mesh: PropertyMesh
) -> None:
    """The gateway drives the key's status LED with a User Property Status to the key element, never a Set."""
    eid = entity_id(hass, "switch", UID_STATUS_LED)
    assert eid == "switch.wc_wc_mirror_button_status_led"
    state = hass.states.get(eid)
    assert state is not None
    assert (
        state.state == STATE_UNKNOWN
    )  # nothing to read: the gateway never reads it either
    assert state.attributes[ATTR_ASSUMED_STATE] is True
    assert state.attributes["mesh_address"] == "0149"
    assert not any(pid == PID_STATUS_LED for _, pid in mesh.gets)
    sent = len(mesh.link.sent)

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    # `D1 27 05 [13 50][03][01]` to the key element, no reply expected
    assert mesh.link.sent[sent:] == [
        (
            OUR_ADDRESS,
            BUTTON_WC,
            bytes.fromhex("d12705") + bytes.fromhex("135003") + b"\x01",
        )
    ]
    assert hass.states.get(eid).state == STATE_ON
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert mesh.link.sent[-1] == (
        OUR_ADDRESS, BUTTON_WC, M.vendor_property_status("user", PID_STATUS_LED, b"\x00"),
    )  # fmt: skip
    assert hass.states.get(eid).state == STATE_OFF
    assert mesh.sets == []  # never a Set

    # every key of a multi-key device has its own, named by its letter
    a = entity_id(hass, "switch", f"{UID_ROCKER_A}-key_status_led")
    b = entity_id(hass, "switch", f"{UID_ROCKER_B}-key_status_led")
    assert (a, b) == (
        "switch.living_room_living_room_rocker_status_led_a",
        "switch.living_room_living_room_rocker_status_led_b",
    )
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: b}, blocking=True
    )
    assert mesh.link.sent[-1][1] == ROCKER_B
    assert hass.states.get(a).state == STATE_UNKNOWN
    assert ROCKER_A not in {dst for _, dst, _ in mesh.link.sent}


async def test_status_led_follows_the_gateways_writes(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, mesh: PropertyMesh
) -> None:
    """A User Property Status 0x5013 the gateway sends *to* a key is a write: the key's switch shows it.

    On air the gateway drives the LED of pid2/el1 and pid2/el2 this way (`air:access:11-0527:0x5013`). A Status
    of the LED to anything but a push-button's key (here: to Home Assistant, to the gateway) still describes its
    sender, so it moves no key's switch.
    """
    eid = entity_id(hass, "switch", UID_STATUS_LED)
    a = entity_id(hass, "switch", f"{UID_ROCKER_A}-key_status_led")
    hub = init_with_mesh.runtime_data
    led_on = vendor_status(0x11, PID_STATUS_LED, b"\x01")
    led_off = vendor_status(0x11, PID_STATUS_LED, b"\x00")

    mesh.link.inject(GATEWAY, BUTTON_WC, led_on)
    await settle(hass)
    assert hass.states.get(eid).state == STATE_ON
    assert hass.states.get(a).state == STATE_UNKNOWN  # only the key it was sent to
    assert PID_STATUS_LED not in hub.element_state(GATEWAY).properties
    mesh.link.inject(GATEWAY, BUTTON_WC, led_off)
    await settle(hass)
    assert hass.states.get(eid).state == STATE_OFF

    # a Status to us, or from the key to the gateway, is the sender's own value
    mesh.link.inject(GATEWAY, OUR_ADDRESS, led_on)
    mesh.link.inject(ROCKER_A, GATEWAY, led_on)
    # an Admin Status is not the gateway's write, a Status to a node without a status LED is no key's
    mesh.link.inject(GATEWAY, BUTTON_WC, vendor_status(0x05, PID_STATUS_LED, b"\x01"))
    mesh.link.inject(GATEWAY, SOCKET, led_on)
    await settle(hass)
    assert hass.states.get(eid).state == STATE_OFF
    assert hub.element_state(GATEWAY).properties[PID_STATUS_LED] == b"\x01"
    assert hass.states.get(a).state == STATE_ON  # ROCKER_A's own report
    assert PID_STATUS_LED not in hub.element_state(SOCKET).properties


async def test_night_mode(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """Night mode = the mode byte of every LED of the node; switching keeps the colours."""
    mesh.values[LIGHT_SWITCH, PID_LED1_ON] = bytes([100, 0, 0, 5])
    mesh.values[LIGHT_SWITCH, PID_LED1_OFF] = bytes([0, 4, 100, 0])
    mesh.values[LIGHT_CTL, PID_LED1_ON] = bytes([75, 0, 0, 5])
    mesh.values[LIGHT_CTL, PID_LED1_OFF] = bytes([0, 0, 0, 5])
    mesh.values[LIGHT_CTL, PID_LED2_ON] = bytes([75, 0, 0, 5])
    mesh.values[LIGHT_CTL, PID_LED2_OFF] = bytes([0, 0, 0, 5])
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "switch", UID_NIGHT_MODE)
    assert eid == "switch.wc_wc_mirror_button_led_night_mode"
    assert hass.states.get(eid).state == STATE_OFF  # not every LED has it
    rocker = entity_id(hass, "switch", UID_ROCKER_NIGHT_MODE)
    assert rocker == "switch.living_room_living_room_rocker_led_night_mode"
    assert hass.states.get(rocker).state == STATE_ON
    # the LED colours were read once for the colour selects and the night-mode switch together
    assert mesh.gets.count((LIGHT_SWITCH, PID_LED1_ON)) == 1

    del mesh.sets[:]
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert mesh.sets == [
        (LIGHT_SWITCH, PID_LED1_ON, bytes([100, 0, 0, 5])),
        (LIGHT_SWITCH, PID_LED1_OFF, bytes([0, 4, 100, 5])),
    ]
    assert hass.states.get(eid).state == STATE_ON

    del mesh.sets[:]
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: rocker}, blocking=True
    )
    assert mesh.sets == [
        (LIGHT_CTL, PID_LED1_ON, bytes([75, 0, 0, 0])),
        (LIGHT_CTL, PID_LED1_OFF, bytes([0, 0, 0, 0])),
        (LIGHT_CTL, PID_LED2_ON, bytes([75, 0, 0, 0])),
        (LIGHT_CTL, PID_LED2_OFF, bytes([0, 0, 0, 0])),
    ]
    assert hass.states.get(rocker).state == STATE_OFF


async def test_night_mode_reads_an_unknown_colour_first(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
) -> None:  # fmt: skip
    mesh.silent.add((SOCKET, PID_LED1_OFF))
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    eid = entity_id(hass, "switch", f"{UID_SOCKET}-led_night_mode")
    # the connect-time read and its retry went unanswered; every entity showing the property reads it (the LED
    # colour select too) and none is cached, so by the time this looks there may be more than two Gets
    await wait_until(hass, lambda: mesh.gets.count((SOCKET, PID_LED1_OFF)) >= 2)
    assert hass.states.get(eid).state == STATE_UNKNOWN  # one LED unknown: no verdict

    # still silent: nothing is written, the user is told (a translated error naming the switch)
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
        )
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "led_colour_unknown"
    assert exc.value.translation_placeholders == {"entity": eid}
    assert mesh.sets == []
    # the device answers now: read on demand, then written
    mesh.silent.clear()
    mesh.values[SOCKET, PID_LED1_OFF] = bytes([0, 60, 0, 0])
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert mesh.sets == [
        (SOCKET, PID_LED1_ON, bytes([0, 0, 0, 5])),
        (SOCKET, PID_LED1_OFF, bytes([0, 60, 0, 5])),
    ]
    assert hass.states.get(eid).state == STATE_ON


async def test_config_switch_send_failure_raises(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.write_error = ConnectionError("proxy disconnected")
    for uid in (UID_AUTO_DST, UID_STATUS_LED, UID_NIGHT_MODE):
        eid = entity_id(hass, "switch", uid)
        with pytest.raises(HomeAssistantError) as exc:
            await hass.services.async_call(
                SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
            )
        assert exc.value.translation_key == "send_failed"


# --------------------------------------------------------------------------- lock function (0x0009)

UID_LOCK = f"{UID_SOCKET}-enforced_output"
UID_LOCK_LIMIT = f"{UID_SOCKET}-lock_time_limit"
PID_LOCK = 0x0009
UNLOCKED = bytes.fromhex("00010000")  # the default the test mesh reports
LOCKED = bytes.fromhex("02010000")  # the app's "lock", no time limit
WIND_ALARM = bytes.fromhex("01ff00000000")


@pytest.fixture
def lock_enabled(hass: HomeAssistant) -> None:
    """The user enabled the socket's Lock switch and its time limit (both hidden by default)."""
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "switch",
        DOMAIN,
        UID_LOCK,
        suggested_object_id="kitchen_boiler_lock",
        disabled_by=None,
    )
    registry.async_get_or_create(
        "number", DOMAIN, UID_LOCK_LIMIT, suggested_object_id="kitchen_boiler_lock_time_limit",
        disabled_by=None,
    )  # fmt: skip


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entity_id(hass, "switch", UID_LOCK)


async def _switch(hass: HomeAssistant, service: str, eid: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: eid}, blocking=True
    )


async def _set_limit(hass: HomeAssistant, seconds: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id(hass, "number", UID_LOCK_LIMIT), "value": seconds},
        blocking=True,
    )


def test_lock_modes() -> None:
    assert lock_mode(P.decode(PID_LOCK, LOCKED)) == "keep_state"
    assert lock_mode(P.lock_output(60, lockout=True)) == "lockout_protection"
    assert lock_mode(P.WIND_ALARM) == "wind_alarm"
    assert (
        lock_mode(P.EnforcedOutput(P.ENFORCE_VALUE, 1, 0, b"\x01")) == "enforced_value"
    )


async def test_lock_is_a_hidden_config_switch_of_the_load(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, mesh: PropertyMesh
) -> None:
    registry = er.async_get(hass)
    eid = entity_id(hass, "switch", UID_LOCK)
    assert eid == "switch.kitchen_boiler_lock"
    entry = registry.async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    limit = registry.async_get(entity_id(hass, "number", UID_LOCK_LIMIT))
    assert limit is not None
    assert limit.entity_category is EntityCategory.CONFIG
    assert limit.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device is not None
    assert device.name == "Boiler"
    # a disabled entity asks nothing: the one Get of the lock is the socket's own (`LoadLock`)
    assert mesh.gets.count((SOCKET, PID_LOCK)) == 1


async def test_lock_locks_the_current_state_and_unlocks_like_the_app(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    eid = await _setup(hass, mock_config_entry)
    assert (SOCKET, PID_LOCK) in mesh.gets
    state = hass.states.get(eid)
    assert state.state == STATE_OFF
    assert "lock_mode" not in state.attributes

    await _switch(hass, SERVICE_TURN_ON, eid)
    # `C3 27 05 [09 00][03][02 01 00 00]`: lock the current state, no time limit
    assert mesh.link.sent[-1] == (
        OUR_ADDRESS,
        SOCKET,
        M.vendor_property_set("admin", PID_LOCK, LOCKED),
    )
    state = hass.states.get(eid)
    assert state.state == STATE_ON
    assert state.attributes["lock_mode"] == "keep_state"
    assert state.attributes["lock_time_limit"] == 0

    await _switch(hass, SERVICE_TURN_OFF, eid)
    # command 0 with the priority and time read (`$unlockDevice$1.java:74-76`)
    assert mesh.sets[-1] == (SOCKET, PID_LOCK, UNLOCKED)
    assert hass.states.get(eid).state == STATE_OFF


async def test_unlock_keeps_the_priority_of_the_lock_it_releases(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    mesh.values[SOCKET, PID_LOCK] = WIND_ALARM
    eid = await _setup(hass, mock_config_entry)
    state = hass.states.get(eid)
    assert state.state == STATE_ON
    assert state.attributes["lock_mode"] == "wind_alarm"

    await _switch(hass, SERVICE_TURN_OFF, eid)
    assert mesh.sets[-1] == (SOCKET, PID_LOCK, bytes.fromhex("00ff00000000"))
    assert hass.states.get(eid).state == STATE_OFF


@pytest.mark.parametrize(
    ("reported", "sent"),
    [
        # unlocked as a light reported it on air: command 0, priority 0, no time limit, a 4-byte value
        ("0000000000000000", "00010000"),
        # the app's lock as the light reported it on air: the unlock keeps its priority (and the fields read)
        ("0201000000000000", "0001000000000000"),
    ],
)
async def test_unlock_never_sends_priority_0(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
    reported: str, sent: str,
) -> None:  # fmt: skip
    """On air an unlocked light reports priority 0, and refuses an unlock with priority 0 (`00 00 00 00` answered
    with the property id alone, the lock kept; the app's `00 01 00 00` unlocked it): with no lock to release, the
    unlock is the plain one, not the fields read."""
    mesh.values[SOCKET, PID_LOCK] = bytes.fromhex(reported)
    eid = await _setup(hass, mock_config_entry)
    await _switch(hass, SERVICE_TURN_OFF, eid)
    assert mesh.sets[-1] == (SOCKET, PID_LOCK, bytes.fromhex(sent))
    assert hass.states.get(eid).state == STATE_OFF


async def test_unlock_of_an_unknown_lock_state_sends_the_plain_unlock(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
    fast_timeouts: None,
) -> None:  # fmt: skip
    mesh.silent.add((SOCKET, PID_LOCK))
    eid = await _setup(hass, mock_config_entry)
    assert hass.states.get(eid).state == STATE_UNKNOWN
    with pytest.raises(
        HomeAssistantError
    ) as exc:  # the socket stays silent: nothing confirms the unlock
        await _switch(hass, SERVICE_TURN_OFF, eid)
    assert exc.value.translation_key == "setting_no_answer"
    assert mesh.sets[-1] == (SOCKET, PID_LOCK, UNLOCKED)


async def test_timed_lock_is_read_back_when_it_should_have_ended(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    eid = await _setup(hass, mock_config_entry)
    await _set_limit(hass, 120)
    await _switch(hass, SERVICE_TURN_ON, eid)
    assert mesh.sets[-1] == (SOCKET, PID_LOCK, bytes.fromhex("02017800"))  # 120 s
    state = hass.states.get(eid)
    assert state.state == STATE_ON
    assert state.attributes["lock_time_limit"] == 120

    mesh.values[SOCKET, PID_LOCK] = (
        UNLOCKED  # the socket unlocks itself and tells no one
    )
    gets = len(mesh.gets)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=100))
    await settle(hass)
    assert len(mesh.gets) == gets
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=120 + LOCK_EXPIRY_MARGIN + 1)
    )
    await settle(hass)
    assert mesh.gets[gets:] == [(SOCKET, PID_LOCK)]
    assert hass.states.get(eid).state == STATE_OFF


async def test_unlock_or_unload_cancels_the_read_back(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    eid = await _setup(hass, mock_config_entry)
    await _set_limit(hass, 60)
    await _switch(hass, SERVICE_TURN_ON, eid)
    await _switch(hass, SERVICE_TURN_OFF, eid)
    await _switch(hass, SERVICE_TURN_ON, eid)
    gets = len(mesh.gets)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1))
    await settle(hass)
    assert len(mesh.gets) == gets


async def test_lock_time_limit_takes_seconds_like_the_app_picker(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """The app's picker is H:MM:SS up to 4:59:59 and the time on air is seconds: 1:02:03 locks for 3723 s."""
    eid = await _setup(hass, mock_config_entry)
    state = hass.states.get("number.kitchen_boiler_lock_time_limit")
    assert state.attributes["unit_of_measurement"] == "s"
    assert state.attributes["max"] == LOCK_TIME_LIMIT_MAX == 17999
    await _set_limit(hass, 3723)
    await _switch(hass, SERVICE_TURN_ON, eid)
    assert mesh.sets[-1] == (SOCKET, PID_LOCK, bytes.fromhex("02018b0e"))


async def test_lock_time_limit_in_minutes_is_restored_as_seconds(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """A limit an earlier release kept in minutes comes back as the same time in seconds."""
    limit = "number.kitchen_boiler_lock_time_limit"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(limit, "5"),
                {
                    "native_max_value": 299,
                    "native_min_value": 0,
                    "native_step": 1,
                    "native_unit_of_measurement": "min",
                    "native_value": 5,
                },
            )
        ],
    )
    eid = await _setup(hass, mock_config_entry)
    assert hass.states.get(limit).state == "300"
    await _switch(hass, SERVICE_TURN_ON, eid)
    assert mesh.sets[-1] == (SOCKET, PID_LOCK, bytes.fromhex("02012c01"))  # 300 s


async def test_lock_time_limit_is_restored(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    limit = "number.kitchen_boiler_lock_time_limit"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(limit, "90"),
                {
                    "native_max_value": LOCK_TIME_LIMIT_MAX,
                    "native_min_value": 0,
                    "native_step": 1,
                    "native_unit_of_measurement": "s",
                    "native_value": 90,
                },
            )
        ],
    )
    eid = await _setup(hass, mock_config_entry)
    state = hass.states.get(limit)
    assert state.state == "90"
    assert state.attributes["max"] == LOCK_TIME_LIMIT_MAX
    await _switch(hass, SERVICE_TURN_ON, eid)
    assert mesh.sets[-1] == (SOCKET, PID_LOCK, bytes.fromhex("02015a00"))  # 90 s


async def test_lock_send_failure_raises(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    eid = await _setup(hass, mock_config_entry)
    mesh.link.write_error = ConnectionError("proxy disconnected")
    with pytest.raises(HomeAssistantError) as exc:
        await _switch(hass, SERVICE_TURN_ON, eid)
    assert exc.value.translation_key == "send_failed"


async def test_night_mode_and_a_colour_change_at_once_keep_both(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """The colour select and the night-mode switch rewrite the same LED value: run together, the second waits for
    the first and starts from what it wrote, instead of both starting from the old value and undoing each other."""
    mesh.values[LIGHT_SWITCH, PID_LED1_ON] = bytes(
        [100, 0, 0, 0]
    )  # red, night mode off
    mesh.values[LIGHT_SWITCH, PID_LED1_OFF] = bytes([0, 4, 100, 0])
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    night = entity_id(hass, "switch", UID_NIGHT_MODE)
    colour = entity_id(hass, "select", f"{UID_LIGHT_SWITCH}-led1_mode_on")
    proxy = mock_config_entry.runtime_data.proxy
    request = proxy.request

    async def on_air(*args: Any, **kwargs: Any) -> Any:
        for _ in range(
            3
        ):  # an exchange takes time on air: the other change runs up to its own meanwhile
            await asyncio.sleep(0)
        return await request(*args, **kwargs)

    proxy.request = on_air
    await asyncio.gather(
        hass.services.async_call(
            SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: colour, ATTR_OPTION: "blue"}, blocking=True,
        ),
        hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: night}, blocking=True
        ),
    )  # fmt: skip
    assert mesh.values[LIGHT_SWITCH, PID_LED1_ON] == bytes(
        [0, 4, 100, 5]
    )  # blue, night mode on
    assert hass.states.get(colour).state == "blue"
    assert hass.states.get(night).state == STATE_ON


async def test_a_timed_lock_set_elsewhere_is_read_back_too(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """A lock the app set for two minutes: the switch reads it back when it should have ended, and again while a
    read-back still finds it locked; an unlock reported meanwhile drops the pending read-back."""
    mesh.values[SOCKET, PID_LOCK] = bytes.fromhex("02017800")  # locked for 120 s
    eid = await _setup(hass, mock_config_entry)
    assert hass.states.get(eid).state == STATE_ON
    # the same lock reported again (a publication) keeps the one read-back already scheduled
    mesh.link.inject(
        SOCKET, OUR_ADDRESS, ph.vendor_status(0x05, PID_LOCK, bytes.fromhex("02017800"))
    )
    await settle(hass)
    gets = len(mesh.gets)
    later = dt_util.utcnow() + timedelta(seconds=120 + LOCK_EXPIRY_MARGIN + 1)
    async_fire_time_changed(hass, later)
    await settle(hass)
    assert mesh.gets[gets:] == [
        (SOCKET, PID_LOCK)
    ]  # still locked (relocked, say): asked again later
    assert hass.states.get(eid).state == STATE_ON

    mesh.values[SOCKET, PID_LOCK] = (
        UNLOCKED  # the socket unlocks itself and tells no one
    )
    async_fire_time_changed(
        hass, later + timedelta(seconds=120 + LOCK_EXPIRY_MARGIN + 1)
    )
    await settle(hass)
    assert mesh.gets[gets:] == [(SOCKET, PID_LOCK)] * 2
    assert hass.states.get(eid).state == STATE_OFF

    # locked elsewhere again, then unlocked with a Status before the time is up: nothing left to read back
    for value in (bytes.fromhex("02017800"), UNLOCKED):
        mesh.link.inject(SOCKET, OUR_ADDRESS, ph.vendor_status(0x05, PID_LOCK, value))
        await settle(hass)
    async_fire_time_changed(hass, later + timedelta(hours=1))
    await settle(hass)
    assert mesh.gets[gets:] == [(SOCKET, PID_LOCK)] * 2
    assert hass.states.get(eid).state == STATE_OFF


async def test_a_locked_socket_shows_it_and_refuses_commands(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """F4-2: the socket reads its own lock once per link (its *Lock* switch is disabled), shows `locked` and refuses
    to switch while locked, as the app disables the control; reported unlocked, it switches again."""
    mesh.values[SOCKET, PID_LOCK] = LOCKED
    await _setup(hass, mock_config_entry)
    eid = entity_id(hass, "switch", UID_SOCKET)
    state = hass.states.get(eid)
    assert state.attributes["locked"] is True
    assert state.attributes["lock_until"] is None
    mesh.link.sent.clear()
    for service in (SERVICE_TURN_ON, SERVICE_TURN_OFF):
        with pytest.raises(ServiceValidationError) as exc:
            await _switch(hass, service, eid)
        assert exc.value.translation_key == "load_locked"
    assert mesh.link.sent == []  # read within PROPERTY_READ_FRESH: not even a Get

    mesh.link.inject(SOCKET, OUR_ADDRESS, ph.vendor_status(0x05, PID_LOCK, UNLOCKED))
    await settle(hass)
    assert hass.states.get(eid).attributes["locked"] is False
    await _switch(hass, SERVICE_TURN_ON, eid)
    assert mesh.link.sent[-1][1:] == (
        SOCKET,
        M.generic_onoff_set(True, tid=mesh.link.sent[-1][2][3], transition=0),
    )


async def test_the_lock_is_read_once_per_link_for_the_socket_and_its_lock_switch(
    hass: HomeAssistant, lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """The socket's read and its enabled *Lock* switch's share one Get per link (`PropertyReader.read`'s `since`),
    whichever runs first; the next link asks again."""
    mesh.values[SOCKET, PID_LOCK] = LOCKED
    eid = await _setup(hass, mock_config_entry)
    assert hass.states.get(eid).state == STATE_ON
    assert (
        hass.states.get(entity_id(hass, "switch", UID_SOCKET)).attributes["locked"]
        is True
    )
    assert mesh.gets.count((SOCKET, PID_LOCK)) == 1

    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    mesh.link.drop_link()
    await settle(hass)
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, mock_config_entry)
    await wait_until(hass, lambda: mesh.gets.count((SOCKET, PID_LOCK)) == 2)
    await settle(hass, 50)
    assert mesh.gets.count((SOCKET, PID_LOCK)) == 2


# --------------------------------------------------------------------------- device lock (0x0001)

PID_DEVICE_LOCK = 0x0001
UID_LOCK_OPERATION = f"{UID_LIGHT_SWITCH}-local_devices_lock"
UID_FACTORY_RESET_LOCK = f"{UID_LIGHT_SWITCH}-factory_reset_time_limit"


@pytest.fixture
def device_lock_enabled(hass: HomeAssistant) -> None:
    """The user enabled the push-button's *Lock operation* and *Lock factory reset* (both hidden by default)."""
    registry = er.async_get(hass)
    for uid in (UID_LOCK_OPERATION, UID_FACTORY_RESET_LOCK):
        registry.async_get_or_create("switch", DOMAIN, uid, disabled_by=None)


async def test_device_lock_flags_are_config_switches_lock_operation_enabled(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, mesh: PropertyMesh
) -> None:
    """*Lock operation* is in the app's normal list (on by default); *Lock factory reset* is expert (hidden)."""
    registry = er.async_get(hass)
    for uid, disabled_by in (
        (UID_LOCK_OPERATION, None),
        (UID_FACTORY_RESET_LOCK, er.RegistryEntryDisabler.INTEGRATION),
    ):
        entry = registry.async_get(entity_id(hass, "switch", uid))
        assert entry is not None
        assert entry.entity_category is EntityCategory.CONFIG
        assert entry.disabled_by is disabled_by
    # the enabled switch reads the word once (the app's Get on opening the page)
    assert mesh.gets.count((LIGHT_SWITCH, PID_DEVICE_LOCK)) == 1
    assert (
        hass.states.get(entity_id(hass, "switch", UID_LOCK_OPERATION)).state
        == STATE_OFF
    )


async def test_upgrade_enables_lock_operation_and_drops_retired_entities(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """An install from before: every device-lock flag registered disabled by the integration, the DALI tunable-white
    load's dim mode registered. *Lock operation* is enabled unless the user disabled it, the expert flag stays
    hidden, and the dim mode the app has no cell for is removed (the dimmer's stays)."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    integration, user = (
        er.RegistryEntryDisabler.INTEGRATION,
        er.RegistryEntryDisabler.USER,
    )
    for platform, uid, disabled_by in (
        ("switch", UID_LOCK_OPERATION, integration),
        ("switch", UID_FACTORY_RESET_LOCK, integration),
        ("switch", f"{UID_SOCKET}-local_devices_lock", user),
        ("select", f"{UID_LIGHT_CTL}-dim_mode", integration),
        ("select", f"{UID_LIGHT_DIMMER}-dim_mode", integration),
    ):
        registry.async_get_or_create(
            platform,
            DOMAIN,
            uid,
            config_entry=mock_config_entry,
            disabled_by=disabled_by,
        )
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    assert [
        registry.async_get(entity_id(hass, "switch", uid)).disabled_by
        for uid in (
            UID_LOCK_OPERATION,
            UID_FACTORY_RESET_LOCK,
            f"{UID_SOCKET}-local_devices_lock",
        )
    ] == [None, integration, user]
    assert (
        registry.async_get_entity_id("select", DOMAIN, f"{UID_LIGHT_CTL}-dim_mode")
        is None
    )
    assert registry.async_get_entity_id(
        "select", DOMAIN, f"{UID_LIGHT_DIMMER}-dim_mode"
    )


async def test_device_lock_flag_rewrites_the_whole_word(
    hass: HomeAssistant, device_lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """A flag is set or cleared in the word as read: the other flags and the bits without a name go back as they
    were (the app's read-modify-write)."""
    mesh.values[LIGHT_SWITCH, PID_DEVICE_LOCK] = bytes.fromhex(
        "2100"
    )  # bit 0 and an unnamed bit 5
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    operation = entity_id(hass, "switch", UID_LOCK_OPERATION)
    reset = entity_id(hass, "switch", UID_FACTORY_RESET_LOCK)
    assert (
        mesh.gets.count((LIGHT_SWITCH, PID_DEVICE_LOCK)) == 1
    )  # two switches, one read
    state = hass.states.get(operation)
    assert state.state == STATE_OFF
    assert state.attributes["property_id"] == "0x0001"
    assert hass.states.get(reset).state == STATE_OFF

    for service, eid, word in (
        (SERVICE_TURN_ON, operation, "2500"),  # bit 2
        (SERVICE_TURN_ON, reset, "2700"),  # bit 1
        (SERVICE_TURN_OFF, operation, "2300"),
    ):
        await hass.services.async_call(
            SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: eid}, blocking=True
        )
        assert mesh.sets[-1] == (LIGHT_SWITCH, PID_DEVICE_LOCK, bytes.fromhex(word))
    assert hass.states.get(operation).state == STATE_OFF
    assert hass.states.get(reset).state == STATE_ON


async def test_device_lock_flags_changed_at_once_keep_both(
    hass: HomeAssistant, device_lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """Both switches rewrite the same word: run together, the second waits for the first and starts from its word."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    proxy = mock_config_entry.runtime_data.proxy
    request = proxy.request

    async def on_air(*args: Any, **kwargs: Any) -> Any:
        for _ in range(
            3
        ):  # an exchange takes time on air: the other change runs up to its own meanwhile
            await asyncio.sleep(0)
        return await request(*args, **kwargs)

    proxy.request = on_air
    await asyncio.gather(
        *(
            hass.services.async_call(
                SWITCH_DOMAIN,
                SERVICE_TURN_ON,
                {ATTR_ENTITY_ID: entity_id(hass, "switch", uid)},
                blocking=True,
            )
            for uid in (UID_LOCK_OPERATION, UID_FACTORY_RESET_LOCK)
        )
    )
    assert mesh.values[LIGHT_SWITCH, PID_DEVICE_LOCK] == bytes.fromhex("0600")


async def test_device_lock_word_is_read_first_never_guessed(
    hass: HomeAssistant, device_lock_enabled: None, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
    fast_timeouts: None,
) -> None:  # fmt: skip
    mesh.silent.add((LIGHT_SWITCH, PID_DEVICE_LOCK))
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    operation = entity_id(hass, "switch", UID_LOCK_OPERATION)
    assert hass.states.get(operation).state == STATE_UNKNOWN
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: operation}, blocking=True
        )
    assert exc.value.translation_key == "device_lock_unknown"
    assert not any(pid == PID_DEVICE_LOCK for _, pid, _ in mesh.sets)

    # the node answers now: the word is read, then written with the flag
    mesh.silent.clear()
    mesh.values[LIGHT_SWITCH, PID_DEVICE_LOCK] = bytes.fromhex("0000")
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: operation}, blocking=True
    )
    assert mesh.sets[-1] == (LIGHT_SWITCH, PID_DEVICE_LOCK, bytes.fromhex("0400"))
    assert hass.states.get(operation).state == STATE_ON


# --------------------------------------------------------------------------- LED colour synchronisation (A23)

UID_LED_SYNC = f"{UID_LIGHT_CTL}-led_colour_sync"
CYAN = bytes([0, 100, 48, 0])  # the 2-gang's LED 1, on and off, on air (0293)


async def _sync_setup(
    hass: HomeAssistant, mesh: PropertyMesh, entry: MockConfigEntry
) -> tuple[str, str, str]:
    mesh.values[LIGHT_CTL, PID_LED1_ON] = CYAN
    mesh.values[LIGHT_CTL, PID_LED1_OFF] = CYAN
    mesh.values[LIGHT_CTL, PID_LED2_ON] = bytes([4, 100, 0, 0])
    mesh.values[LIGHT_CTL, PID_LED2_OFF] = bytes([100, 27, 0, 0])
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass, 200)
    return (
        entity_id(hass, "switch", UID_LED_SYNC),
        entity_id(hass, "select", f"{UID_LIGHT_CTL}-led1_mode_on"),
        entity_id(hass, "select", f"{UID_LIGHT_CTL}-led2_mode_on"),
    )


async def test_led_colour_sync(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """On writes LED 1's values to LED 1 and LED 2 in the app's order (0xA001, 0xA004, 0xA002, 0xA005, as on
    air); while on, an LED 1 colour goes to LED 2 too and LED 2's selects are unavailable; off sends nothing."""
    sync, led1_on, led2_on = await _sync_setup(hass, mesh, mock_config_entry)
    assert sync == "switch.living_room_living_room_rocker_synchronise_led_colours"
    assert hass.states.get(sync).state == STATE_OFF
    entry = er.async_get(hass).async_get(sync)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG
    assert entry.disabled_by is None  # the app shows it with the colours
    assert hass.states.get(led2_on).state == "green"
    # only a 2-gang node has it
    registry = er.async_get(hass)
    assert not registry.async_get_entity_id(
        "switch", DOMAIN, f"{UID_LIGHT_SWITCH}-led_colour_sync"
    )

    del mesh.sets[:]
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: sync}, blocking=True
    )
    assert mesh.sets == [
        (LIGHT_CTL, PID_LED1_ON, CYAN),
        (LIGHT_CTL, PID_LED2_ON, CYAN),
        (LIGHT_CTL, PID_LED1_OFF, CYAN),
        (LIGHT_CTL, PID_LED2_OFF, CYAN),
    ]
    assert hass.states.get(sync).state == STATE_ON
    assert hass.states.get(led2_on).state == STATE_UNAVAILABLE
    assert hass.states.get(led1_on).state == "cyan"

    del mesh.sets[:]
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: led1_on, ATTR_OPTION: "red"},
        blocking=True,
    )
    red = bytes([75, 0, 0, 0])
    assert mesh.sets == [(LIGHT_CTL, PID_LED1_ON, red), (LIGHT_CTL, PID_LED2_ON, red)]

    del mesh.sets[:]
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: sync}, blocking=True
    )
    assert mesh.sets == []
    assert hass.states.get(sync).state == STATE_OFF
    assert hass.states.get(led2_on).state == "red"
    # not synchronised: LED 1 alone
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: led1_on, ATTR_OPTION: "blue"},
        blocking=True,
    )
    assert mesh.sets == [(LIGHT_CTL, PID_LED1_ON, bytes([0, 4, 100, 0]))]


async def test_led_colour_sync_is_restored(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """The node has no such setting (the app keeps it itself): the flag survives a restart as the entity's state."""
    mock_restore_cache(
        hass,
        [
            State(
                "switch.living_room_living_room_rocker_synchronise_led_colours",
                STATE_ON,
            )
        ],
    )
    sync, _, led2_on = await _sync_setup(hass, mesh, mock_config_entry)
    assert hass.states.get(sync).state == STATE_ON
    assert hass.states.get(led2_on).state == STATE_UNAVAILABLE
    assert mesh.sets == []  # restoring writes nothing


async def test_led_colour_sync_not_applied(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
) -> None:  # fmt: skip
    """A copy the node does not take (no Status, the read-back shows LED 2's old colour) raises as the colour
    selects do; the flag stays off and LED 2's selects stay available."""
    sync, _, led2_on = await _sync_setup(hass, mesh, mock_config_entry)
    mesh.confirm_sets = (
        False  # LED 1's own value reads back unchanged; LED 2's does not
    )
    del mesh.sets[:]
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: sync}, blocking=True
        )
    assert exc.value.translation_key == "setting_not_applied"
    assert exc.value.translation_placeholders == {"entity": sync}
    assert mesh.sets == [(LIGHT_CTL, PID_LED1_ON, CYAN), (LIGHT_CTL, PID_LED2_ON, CYAN)]
    assert hass.states.get(sync).state == STATE_OFF
    assert hass.states.get(led2_on).state == "green"


async def test_led_colour_sync_needs_the_colours(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
) -> None:  # fmt: skip
    """LED 1's colours are read first when not known; still unknown: nothing is written, the flag stays off."""
    mesh.silent.add((LIGHT_CTL, PID_LED1_OFF))
    sync, _, _ = await _sync_setup(hass, mesh, mock_config_entry)
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: sync}, blocking=True
        )
    assert exc.value.translation_key == "led_colour_unknown"
    assert mesh.sets == []
    assert hass.states.get(sync).state == STATE_OFF
