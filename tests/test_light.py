"""Light platform: switched loads, dimmers and tunable-white channels."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_TRANSITION,
    ColorMode,
    LightEntityFeature,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from voluptuous import Invalid

from custom_components.junghome_ble import light as light_platform
from custom_components.junghome_ble.const import (
    DOMAIN,
)
from custom_components.junghome_ble.entity import (
    UPDATE_READ_INTERVAL,
    UPDATE_READS,
    update_reads,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import ALL_LIGHTS, Light
from custom_components.junghome_ble.light import (
    DIM_MOVE_TRANSITION,
)

from . import property_helpers as ph
from .conftest import FakeProxyLink, load_sets, settle, setup_entry, wait_for_link
from .helpers import (
    LIGHT_CTL,
    LIGHT_CTL_TEMPERATURE,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    MESH_UUID,
    OUR_ADDRESS,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_LIGHT_OUT2,
    UID_LIGHT_SWITCH,
    ctl_range_status,
    ctl_status,
    ctl_temperature_status,
    entity_id,
    lightness_status,
    onoff_status,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .property_helpers import PropertyMesh

fast_timeouts, mesh = ph.fast_timeouts, ph.mesh


async def turn_on(hass: HomeAssistant, eid: str, **data: Any) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid, **data}, blocking=True
    )


async def turn_off(hass: HomeAssistant, eid: str) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: eid}, blocking=True
    )


async def test_entities(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    switch = hass.states.get(entity_id(hass, "light", UID_LIGHT_SWITCH))
    assert switch is not None
    assert (
        switch.entity_id == "light.wc_wc_mirror"
    )  # area-prefixed: the device sits in the room of the export
    assert switch.state == STATE_UNKNOWN  # nothing heard yet
    assert switch.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.ONOFF]
    assert switch.attributes["mesh_address"] == "0148"
    assert switch.attributes["rooms"] == ["WC"]

    dimmer = hass.states.get(entity_id(hass, "light", UID_LIGHT_DIMMER))
    assert dimmer is not None
    assert dimmer.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.BRIGHTNESS]

    ctl = hass.states.get(entity_id(hass, "light", UID_LIGHT_CTL))
    assert ctl is not None
    assert ctl.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.COLOR_TEMP]
    assert (
        ctl.attributes[ATTR_MIN_COLOR_TEMP_KELVIN] == 2000
    )  # the defaults: no range read yet
    assert ctl.attributes[ATTR_MAX_COLOR_TEMP_KELVIN] == 6000

    out2 = hass.states.get(entity_id(hass, "light", UID_LIGHT_OUT2))
    assert out2 is not None
    assert out2.entity_id == "light.kitchen_2_channel_actuator_0400_out_2"


async def test_switched_light(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    fake_link.sent.clear()
    assert hass.states.get(eid).state == STATE_OFF  # what it answered the refresh with

    # an acknowledged Set with an immediate transition, answered at the first attempt by the status the node
    # publishes: the action returns once it arrived, with the state it reports
    await turn_on(hass, eid)
    ((dst, pdu),) = load_sets(fake_link)
    assert dst == LIGHT_SWITCH
    assert pdu == M.generic_onoff_set(True, tid=pdu[3], transition=0)
    assert hass.states.get(eid).state == STATE_ON

    await turn_off(hass, eid)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_SWITCH
    assert pdu == M.generic_onoff_set(False, tid=pdu[3], transition=0)
    assert len(load_sets(fake_link)) == 2
    assert hass.states.get(eid).state == STATE_OFF

    # the state follows every status the node publishes, whoever changed it
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(True))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_ON
    assert ATTR_BRIGHTNESS not in hass.states.get(eid).attributes
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(False))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_OFF


async def test_dimmer(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    eid = entity_id(hass, "light", UID_LIGHT_DIMMER)
    fake_link.sent.clear()

    fake_link.inject(
        LIGHT_DIMMER, 0xC070, onoff_status(True)
    )  # on, but the level is not known yet
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] is None

    await turn_on(hass, eid, brightness=128)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_DIMMER
    assert pdu == M.light_lightness_set(32896, tid=pdu[4])  # 128/255 of 65535
    # the Light Lightness Status answering it
    assert hass.states.get(eid).attributes[ATTR_BRIGHTNESS] == 128

    await turn_on(hass, eid)  # no brightness given: a plain on
    _, pdu = load_sets(fake_link)[-1]
    assert pdu == M.generic_onoff_set(True, tid=pdu[3], transition=0)

    await turn_off(hass, eid)
    _, pdu = load_sets(fake_link)[-1]
    assert pdu == M.generic_onoff_set(False, tid=pdu[3], transition=0)
    assert len(load_sets(fake_link)) == 3

    fake_link.inject(LIGHT_DIMMER, 0xC070, lightness_status(32768))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    assert state.attributes[ATTR_COLOR_MODE] == ColorMode.BRIGHTNESS

    fake_link.inject(LIGHT_DIMMER, 0xC070, lightness_status(0))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_OFF

    # a Generic OnOff Status switching it on while the cached lightness is 0: on with an unknown level, never
    # "on at brightness 0"; the lowest levels round up to 1 rather than down to 0
    fake_link.inject(LIGHT_DIMMER, 0xC070, onoff_status(True))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] is None
    fake_link.inject(LIGHT_DIMMER, 0xC070, lightness_status(100))
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_BRIGHTNESS] == 1


async def test_tunable_white(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Write paths: a colour temperature → CTL Set (or CTL Temperature Set while on), a brightness alone → Lightness
    Set, neither → OnOff Set."""
    eid = entity_id(hass, "light", UID_LIGHT_CTL)
    fake_link.sent.clear()

    # nothing known about the light yet: a temperature alone turns it fully on at that temperature
    await turn_on(hass, eid, color_temp_kelvin=4000)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_CTL
    assert pdu == M.light_ctl_set(65535, 4000, tid=pdu[8])

    await turn_on(
        hass, eid, brightness=255
    )  # brightness alone never guesses a temperature
    dst, pdu = load_sets(fake_link)[-1]
    assert pdu == M.light_lightness_set(65535, tid=pdu[4])

    await turn_on(hass, eid, brightness=51, color_temp_kelvin=2700)
    dst, pdu = load_sets(fake_link)[-1]
    assert pdu == M.light_ctl_set(13107, 2700, tid=pdu[8])

    # clamped to the supported range; the light is on now (the CTL Status answering the last Set said so), so the
    # temperature alone goes to its temperature element
    await turn_on(hass, eid, color_temp_kelvin=1000)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_CTL_TEMPERATURE
    assert pdu == M.light_ctl_temperature_set(2000, tid=pdu[6], transition=0)

    # with a known state, a temperature alone keeps the current level: the light's own, not a resent one
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(30000, 5000))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 117
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == 5000
    assert state.attributes[ATTR_COLOR_MODE] == ColorMode.COLOR_TEMP

    await turn_on(hass, eid, brightness=128)
    dst, pdu = load_sets(fake_link)[-1]
    assert pdu == M.light_lightness_set(32896, tid=pdu[4])
    await turn_on(hass, eid, color_temp_kelvin=6000)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_CTL_TEMPERATURE
    assert pdu == M.light_ctl_temperature_set(6000, tid=pdu[6], transition=0)

    fake_link.inject(
        LIGHT_CTL, 0xC044, ctl_status(0, 5000)
    )  # off: a temperature alone turns it fully on again
    await hass.async_block_till_done()
    await turn_on(hass, eid, color_temp_kelvin=3500)
    dst, pdu = load_sets(fake_link)[-1]
    assert pdu == M.light_ctl_set(65535, 3500, tid=pdu[8])

    await turn_on(hass, eid)  # plain on
    dst, pdu = load_sets(fake_link)[-1]
    assert pdu == M.generic_onoff_set(True, tid=pdu[3], transition=0)
    await turn_off(hass, eid)
    dst, pdu = load_sets(fake_link)[-1]
    assert pdu == M.generic_onoff_set(False, tid=pdu[3], transition=0)

    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(0, 5000))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_OFF


async def test_tunable_white_temperature_alone(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """F11: a colour temperature alone for a light that is on is a Light CTL Temperature Set to its temperature
    element, so a cached lightness that fell behind (the light dimmed on without a status reaching us) is never
    sent back to it; the temperature element's status moves the colour temperature alone. With a brightness, or to
    switch the light on, it stays a full CTL Set to the light, and so for a CTL light without a temperature element.
    The Set is the gateway's on-air form, with transition 0 and delay 0 (A03).
    """
    hub = init_integration.runtime_data
    eid = entity_id(hass, "light", UID_LIGHT_CTL)
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(30000, 5000))
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_range_status(2700, 6500))
    await hass.async_block_till_done()
    fake_link.sent.clear()

    await turn_on(hass, eid, color_temp_kelvin=9000)  # clamped to the light's range
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_CTL_TEMPERATURE
    # the gateway's 7-byte form: [temperature][delta UV][tid][transition 0][delay 0], not the light's default
    # transition time
    assert pdu == M.light_ctl_temperature_set(6500, tid=pdu[6], transition=0)
    assert pdu[2:] == (6500).to_bytes(2, "little") + b"\x00\x00" + bytes([pdu[6], 0, 0])
    assert len(load_sets(fake_link)) == 1
    fake_link.inject(LIGHT_CTL_TEMPERATURE, 0xC044, ctl_temperature_status(6500))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert (state.state, state.attributes[ATTR_COLOR_TEMP_KELVIN]) == (STATE_ON, 6500)
    assert state.attributes[ATTR_BRIGHTNESS] == 117  # untouched

    await turn_on(hass, eid, brightness=255, color_temp_kelvin=3000)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_CTL
    assert pdu == M.light_ctl_set(65535, 3000, tid=pdu[8])

    # on with the level unknown (a Generic OnOff Status): the light keeps whatever level it is at
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(0, 3000))
    fake_link.inject(LIGHT_CTL, 0xC044, onoff_status(True))
    await hass.async_block_till_done()
    await turn_on(hass, eid, color_temp_kelvin=4000)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_CTL_TEMPERATURE
    assert pdu == M.light_ctl_temperature_set(4000, tid=pdu[6], transition=0)

    # a CTL light whose export shows no temperature element: the full CTL Set, at the cached level
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(30000, 4000))
    await hass.async_block_till_done()
    light = hub.devices.by_address[LIGHT_CTL]
    assert isinstance(light, Light)
    light.temperature_address = None
    await turn_on(hass, eid, color_temp_kelvin=5000)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_CTL
    assert pdu == M.light_ctl_set(30000, 5000, tid=pdu[8])


async def test_tunable_white_temperature_alone_send_failure(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A CTL Temperature Set that cannot be delivered surfaces as the translated error too."""
    eid = entity_id(hass, "light", UID_LIGHT_CTL)
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(30000, 5000))
    await hass.async_block_till_done()
    fake_link.write_error = ConnectionError("proxy disconnected")
    with pytest.raises(HomeAssistantError) as exc:
        await turn_on(hass, eid, color_temp_kelvin=3000)
    assert exc.value.translation_key == "send_failed"


async def test_tunable_white_range_from_the_light(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """A Light CTL Temperature Range Status replaces the default limits, for the attributes and for the clamp."""
    eid = entity_id(hass, "light", UID_LIGHT_CTL)
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_range_status(2700, 6500))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes[ATTR_MIN_COLOR_TEMP_KELVIN] == 2700
    assert state.attributes[ATTR_MAX_COLOR_TEMP_KELVIN] == 6500

    fake_link.sent.clear()
    await turn_on(
        hass, eid, color_temp_kelvin=2200
    )  # below the light's minimum (but above the old default)
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_CTL
    assert pdu == M.light_ctl_set(65535, 2700, tid=pdu[8])
    # on now (the CTL Status answering that Set): the temperature alone goes to its temperature element
    await turn_on(
        hass, eid, color_temp_kelvin=6300
    )  # above the old default, within the light's range
    dst, pdu = load_sets(fake_link)[-1]
    assert pdu == M.light_ctl_temperature_set(6300, tid=pdu[6], transition=0)
    await turn_on(hass, eid, color_temp_kelvin=9000)
    dst, pdu = load_sets(fake_link)[-1]
    assert pdu == M.light_ctl_temperature_set(6500, tid=pdu[6], transition=0)

    # a range from another light does not leak, and a failed range read keeps the defaults
    fake_link.inject(LIGHT_DIMMER, 0xC070, ctl_range_status(3000, 4000))
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_range_status(3000, 4000, status=2))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes[ATTR_MIN_COLOR_TEMP_KELVIN] == 2700
    assert state.attributes[ATTR_MAX_COLOR_TEMP_KELVIN] == 6500


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("proxy disconnected"),
        OSError("GATT write failed"),
        TimeoutError("no ack"),
    ],
)
async def test_send_failure_raises(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    error: Exception,
) -> None:
    """A command that cannot be delivered surfaces as a translated error, for every kind of light."""
    fake_link.write_error = error
    for uid in (UID_LIGHT_SWITCH, UID_LIGHT_DIMMER, UID_LIGHT_CTL):
        eid = entity_id(hass, "light", uid)
        with pytest.raises(HomeAssistantError) as exc:
            await turn_on(hass, eid, brightness=200)
        assert exc.value.translation_key == "send_failed"
        with pytest.raises(HomeAssistantError) as exc:
            await turn_off(hass, eid)
        assert exc.value.translation_key == "send_failed"


async def test_availability_follows_the_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(True))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_ON

    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get(eid).state == STATE_UNAVAILABLE

    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, init_integration)
    assert (
        hass.states.get(eid).state == STATE_ON
    )  # the cached state is kept across the reconnect


# ----------------------------------------------------------------------------- hold-to-dim (review-3 F12)


async def dim(hass: HomeAssistant, service: str, eid: str, **data: Any) -> None:
    await hass.services.async_call(
        DOMAIN, service, {ATTR_ENTITY_ID: eid, **data}, blocking=True
    )


async def test_dim_actions_move_and_step_the_level_server(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """start_dim / stop_dim: Generic Move Set at the speed asked for, then 0; step_dim: Generic Delta Set."""
    eid = entity_id(hass, "light", UID_LIGHT_DIMMER)
    fake_link.sent.clear()

    await dim(hass, "start_dim", eid, direction="up")  # the default 20 %/s
    dst, pdu = load_sets(fake_link)[-1]
    assert dst == LIGHT_DIMMER
    assert pdu == M.generic_move_set(1311, tid=pdu[4], transition=DIM_MOVE_TRANSITION)
    await dim(hass, "start_dim", eid, direction="down", speed=100)
    _, pdu = load_sets(fake_link)[-1]
    assert pdu == M.generic_move_set(-6554, tid=pdu[4], transition=DIM_MOVE_TRANSITION)

    fake_link.sent.clear()
    await dim(hass, "stop_dim", eid)
    await hass.async_block_till_done()
    _, stop = load_sets(fake_link)[0]
    assert stop == M.generic_move_set(0, tid=stop[4], transition=DIM_MOVE_TRANSITION)
    # then the light is asked where it stopped
    assert (LIGHT_DIMMER, M.light_lightness_get()) in {
        (d, p) for _, d, p in fake_link.sent
    }

    fake_link.sent.clear()
    await dim(hass, "step_dim", eid, step=-10)
    await hass.async_block_till_done()
    _, step = load_sets(fake_link)[0]
    assert step == M.generic_delta_set(-6554, tid=step[6])
    assert (LIGHT_DIMMER, M.light_lightness_get()) in {
        (d, p) for _, d, p in fake_link.sent
    }

    # a tunable-white channel dims the same way, read back with its CTL Get
    ctl = entity_id(hass, "light", UID_LIGHT_CTL)
    fake_link.sent.clear()
    await dim(hass, "step_dim", ctl, step=100)
    await hass.async_block_till_done()
    assert load_sets(fake_link)[0] == (
        LIGHT_CTL,
        M.generic_delta_set(65535, tid=load_sets(fake_link)[0][1][6]),
    )
    assert (LIGHT_CTL, M.light_ctl_get()) in {(d, p) for _, d, p in fake_link.sent}


async def test_dim_actions_do_not_wait_for_an_answer(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Hold-to-dim is unverified on air and the app never dims this way: a dimmer that does not answer a Move or
    Delta Set is sent each once, the actions succeed, the light is still read back, and it stays available."""
    hub = init_integration.runtime_data
    eid = entity_id(hass, "light", UID_LIGHT_DIMMER)
    await settle(hass)
    fake_link.sets_silent.add(LIGHT_DIMMER)
    fake_link.sent.clear()
    await dim(hass, "start_dim", eid, direction="up")
    await dim(hass, "stop_dim", eid)
    await dim(hass, "step_dim", eid, step=10)
    await settle(hass)
    assert [pdu[:2] for _, pdu in load_sets(fake_link)] == [
        bytes([0x82, 0x0B]),
        bytes([0x82, 0x0B]),
        bytes([0x82, 0x09]),
    ]  # one of each: no retries
    gets = [p for _, d, p in fake_link.sent if d == LIGHT_DIMMER]
    assert gets.count(M.light_lightness_get()) == 2  # after the stop and the step
    assert not hub.unreachable
    assert hass.states.get(eid).state != STATE_UNAVAILABLE


async def test_dim_actions_refuse_what_cannot_dim(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A switched light and *All lights* have no Level server to talk to; the fields are checked too."""
    for eid in (
        entity_id(hass, "light", UID_LIGHT_SWITCH),
        entity_id(hass, "light", f"{MESH_UUID}-central-fef5"),
    ):
        with pytest.raises(ServiceValidationError) as exc:
            await dim(hass, "start_dim", eid, direction="up")
        assert exc.value.translation_key == "dim_not_dimmable"
        assert exc.value.translation_placeholders == {"entity": eid}
    dimmer = entity_id(hass, "light", UID_LIGHT_DIMMER)
    for service, data in (
        ("start_dim", {"direction": "sideways"}),
        ("start_dim", {"direction": "up", "speed": 0}),
        ("step_dim", {"step": 0}),
        ("step_dim", {"step": 101}),
    ):
        with pytest.raises(Invalid):
            await dim(hass, service, dimmer, **data)


async def test_dim_send_failure_raises(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "light", UID_LIGHT_DIMMER)
    fake_link.write_error = ConnectionError("gone")
    for service, data in (
        ("start_dim", {"direction": "up"}),
        ("stop_dim", {}),
        ("step_dim", {"step": 5}),
    ):
        with pytest.raises(HomeAssistantError) as exc:
            await dim(hass, service, eid, **data)
        assert exc.value.translation_key == "send_failed"


# ----------------------------------------------------------------------------- transitions (review-4 F4-1)

FADING = frozenset({"switch", "dimmer", "ctl"})
ALL_LIGHTS_UID = f"{MESH_UUID}-central-fef5"
ROOM_WC_LIGHTS_UID = f"{MESH_UUID}-room-c00f-lights"
ROOM_WC = 0xC00F


@pytest.fixture
def fading_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every light kind fades, as the probe may find (`TRANSITION_KINDS`); list it before `init_integration`."""
    monkeypatch.setattr(light_platform, "TRANSITION_KINDS", FADING)


@pytest.fixture
def dimmers_fade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the dimmable kinds fade; list it before `init_integration`."""
    monkeypatch.setattr(
        light_platform, "TRANSITION_KINDS", frozenset({"dimmer", "ctl"})
    )


def features(hass: HomeAssistant, uid: str) -> int:
    return hass.states.get(entity_id(hass, "light", uid)).attributes[
        ATTR_SUPPORTED_FEATURES
    ]


def gets_to(link: FakeProxyLink, addr: int, get: bytes) -> int:
    return sum(1 for _, dst, a in link.sent if dst == addr and a == get)


async def light_call(hass: HomeAssistant, service: str, eid: str, **data: Any) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, service, {ATTR_ENTITY_ID: eid, **data}, blocking=True
    )


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await settle(hass)


async def test_no_light_takes_a_transition_until_the_probe(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """F4-1: `TRANSITION_KINDS` is empty until the on-air probe ran, so no light and no *All lights* declares the
    feature, and a `transition` changes no byte of any Set — HA drops it, and a light ignores one anyway."""
    for uid in (UID_LIGHT_SWITCH, UID_LIGHT_DIMMER, UID_LIGHT_CTL, ALL_LIGHTS_UID):
        assert features(hass, uid) == 0
    fake_link.sent.clear()
    await turn_on(
        hass, entity_id(hass, "light", UID_LIGHT_DIMMER), brightness=128, transition=3
    )
    await light_call(
        hass, SERVICE_TURN_OFF, entity_id(hass, "light", UID_LIGHT_SWITCH), transition=3
    )
    (_, dim), (_, off) = load_sets(fake_link)
    assert dim == M.light_lightness_set(32896, tid=dim[4])
    assert off == M.generic_onoff_set(False, tid=off[3], transition=0)
    assert light_platform._transition("dimmer", {ATTR_TRANSITION: 3}) is None


async def test_a_fading_light_sends_the_transition_and_reads_the_state_after_it(
    hass: HomeAssistant,
    fading_kinds: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    freezer: FrozenDateTimeFactory,
) -> None:
    """F4-1: a light of a fading kind declares the feature and puts HA's `transition` into its Set (the nearest
    transition-time byte, delay 0). The Status answering it mid-fade — old level, the requested one as target, the
    time still to run — confirms the Set (D32: its target shows the state), the entity shows the present level, and
    the light is read again once the fade is over plus a second; a newer Set replaces the pending read."""
    hub = init_integration.runtime_data
    for uid in (UID_LIGHT_SWITCH, UID_LIGHT_DIMMER, UID_LIGHT_CTL):
        assert features(hass, uid) == LightEntityFeature.TRANSITION
    eid = entity_id(hass, "light", UID_LIGHT_DIMMER)
    fake_link.fading[LIGHT_DIMMER] = ((0x1000).to_bytes(2, "little"), 0x1E)  # 3 s left
    fake_link.sent.clear()

    await turn_on(hass, eid, brightness=128, transition=3)
    ((dst, pdu),) = load_sets(fake_link)  # confirmed at the first attempt
    assert dst == LIGHT_DIMMER
    assert pdu == M.light_lightness_set(32896, tid=pdu[4], transition=0x1E)
    assert pdu[5:] == bytes([0x1E, 0])
    assert hass.states.get(eid).attributes[ATTR_BRIGHTNESS] == 16  # the present level
    assert hub.states[LIGHT_DIMMER].target_lightness == 32896

    await tick(hass, freezer, 2)
    # a second Set within the fade: its read replaces the first one's
    await turn_on(hass, eid, brightness=255, transition=3)
    await tick(hass, freezer, 2.5)  # the first Set's read would be due now
    assert gets_to(fake_link, LIGHT_DIMMER, M.light_lightness_get()) == 0
    await tick(hass, freezer, 2)
    assert gets_to(fake_link, LIGHT_DIMMER, M.light_lightness_get()) == 1
    await tick(hass, freezer, 10)
    assert gets_to(fake_link, LIGHT_DIMMER, M.light_lightness_get()) == 1
    assert not hub._transition_reread

    # a Status at rest (the load did not fade, or was done at once) leaves nothing to read
    del fake_link.fading[LIGHT_DIMMER]
    await light_call(hass, SERVICE_TURN_OFF, eid, transition=1.5)
    _, pdu = load_sets(fake_link)[-1]
    assert pdu == M.generic_onoff_set(False, tid=pdu[3], transition=0x0F)
    assert not hub._transition_reread

    # a switched light: OnOff with the transition
    switch = entity_id(hass, "light", UID_LIGHT_SWITCH)
    fake_link.fading[LIGHT_SWITCH] = (b"\x00", 0x14)
    await turn_on(hass, switch, transition=2)
    _, pdu = load_sets(fake_link)[-1]
    assert pdu == M.generic_onoff_set(True, tid=pdu[3], transition=0x14)
    await tick(hass, freezer, 3.5)
    assert gets_to(fake_link, LIGHT_SWITCH, M.generic_onoff_get()) == 1


async def test_a_tunable_white_light_fades_its_colour_temperature(
    hass: HomeAssistant,
    fading_kinds: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    freezer: FrozenDateTimeFactory,
) -> None:
    """F4-1: the CTL Set and the CTL Temperature Set carry the transition; the temperature element's Status
    mid-fade schedules the read of the light (its CTL Get), not of the temperature element."""
    eid = entity_id(hass, "light", UID_LIGHT_CTL)
    fake_link.inject(LIGHT_CTL, 0xC044, ctl_status(30000, 5000))
    await hass.async_block_till_done()
    fake_link.sent.clear()

    await turn_on(hass, eid, brightness=255, color_temp_kelvin=3000, transition=0.5)
    dst, pdu = load_sets(fake_link)[-1]
    assert (dst, pdu) == (
        LIGHT_CTL,
        M.light_ctl_set(65535, 3000, tid=pdu[8], transition=0x05),
    )

    fake_link.fading[LIGHT_CTL_TEMPERATURE] = (
        (3000).to_bytes(2, "little") + bytes(2),
        0x41,  # 1 s left
    )
    await turn_on(hass, eid, color_temp_kelvin=4000, transition=20)
    dst, pdu = load_sets(fake_link)[-1]
    assert (dst, pdu) == (
        LIGHT_CTL_TEMPERATURE,
        M.light_ctl_temperature_set(4000, tid=pdu[6], transition=0x54),  # 20 x 1 s
    )
    await tick(hass, freezer, 2.5)
    assert gets_to(fake_link, LIGHT_CTL, M.light_ctl_get()) == 1
    assert gets_to(fake_link, LIGHT_CTL_TEMPERATURE, M.light_ctl_temperature_get()) == 0


async def test_the_read_after_a_transition_waits_for_a_link(
    hass: HomeAssistant,
    fading_kinds: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A read due while the link is down is left to the next link's refresh; one pending at unload is cancelled."""
    hub = init_integration.runtime_data
    eid = entity_id(hass, "light", UID_LIGHT_DIMMER)
    fake_link.fading[LIGHT_DIMMER] = (bytes(2), 0x0A)  # 1 s left
    fake_link.sent.clear()
    await turn_on(hass, eid, brightness=128, transition=1)
    with patch.object(
        type(hub), "connected", new_callable=PropertyMock, return_value=False
    ):
        await tick(hass, freezer, 2.5)
    assert gets_to(fake_link, LIGHT_DIMMER, M.light_lightness_get()) == 0
    assert not hub._transition_reread

    await turn_on(hass, eid, brightness=64, transition=1)
    assert hub._transition_reread
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert not hub._transition_reread


async def test_all_lights_fade_only_when_every_member_does(
    hass: HomeAssistant,
    dimmers_fade: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
) -> None:
    """A dimmer fades, a switched light does not; *All lights*, with switched members, declares no transition:
    a member that does not take one is never sent one in the group's Unacknowledged Sets."""
    assert features(hass, UID_LIGHT_DIMMER) == LightEntityFeature.TRANSITION
    assert features(hass, UID_LIGHT_SWITCH) == 0
    assert features(hass, ALL_LIGHTS_UID) == 0
    assert features(hass, ROOM_WC_LIGHTS_UID) == 0


async def test_all_lights_pass_the_transition_on(
    hass: HomeAssistant,
    fading_kinds: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """With every member fading, *All lights* (and a room's) put the transition into each Unacknowledged Set."""
    assert features(hass, ALL_LIGHTS_UID) == LightEntityFeature.TRANSITION
    eid = entity_id(hass, "light", ALL_LIGHTS_UID)
    fake_link.sent.clear()
    await turn_on(hass, eid, brightness=51, transition=2)
    await light_call(hass, SERVICE_TURN_OFF, eid, transition=2)
    sent = [(dst, a) for _, dst, a in fake_link.sent]
    tids = [M.decode_opcode(a)[2] for _, a in sent]
    assert sent == [
        (
            ALL_LIGHTS,
            M.light_lightness_set(13107, ack=False, tid=tids[0][2], transition=0x14),
        ),
        (
            ALL_LIGHTS,
            M.generic_onoff_set(True, ack=False, tid=tids[1][1], transition=0x14),
        ),
        (
            ALL_LIGHTS,
            M.generic_onoff_set(False, ack=False, tid=tids[2][1], transition=0x14),
        ),
    ]

    fake_link.sent.clear()
    await turn_on(
        hass, entity_id(hass, "light", ROOM_WC_LIGHTS_UID), brightness=51, transition=2
    )
    sent = [(dst, a) for _, dst, a in fake_link.sent]
    tids = [M.decode_opcode(a)[2] for _, a in sent]
    assert sent == [
        (
            ROOM_WC,
            M.light_lightness_set(13107, ack=False, tid=tids[0][2], transition=0x14),
        ),
        (
            LIGHT_SWITCH,
            M.generic_onoff_set(True, ack=False, tid=tids[1][1], transition=0x14),
        ),
        (
            LIGHT_DIMMER,
            M.generic_onoff_set(True, ack=False, tid=tids[2][1], transition=0x14),
        ),
    ]


# ----------------------------------------------------------------------------- update entity, review-4 H4-10


async def update_entity(hass: HomeAssistant, eid: str) -> None:
    await hass.services.async_call(
        "homeassistant", "update_entity", {ATTR_ENTITY_ID: eid}, blocking=True
    )


async def test_update_entity_asks_the_light_now(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 H4-10: `homeassistant.update_entity` on a light sends the connect-time refresh's Get for its kind,
    and the entity shows the answer; the same Get to the same light again within UPDATE_READ_INTERVAL is not sent."""
    hub = init_integration.runtime_data
    switch = entity_id(hass, "light", UID_LIGHT_SWITCH)
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(True))  # published on: cached
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == STATE_ON
    for uid, address, get in (
        (UID_LIGHT_SWITCH, LIGHT_SWITCH, M.generic_onoff_get()),
        (UID_LIGHT_DIMMER, LIGHT_DIMMER, M.light_lightness_get()),
        (UID_LIGHT_CTL, LIGHT_CTL, M.light_ctl_get()),
    ):
        fake_link.sent.clear()
        await update_entity(hass, entity_id(hass, "light", uid))
        assert [(dst, pdu) for _, dst, pdu in fake_link.sent] == [(address, get)]
    assert hass.states.get(switch).state == STATE_OFF  # what the light answered

    # rate-limited per element and Get: asked again only once the interval has passed
    fake_link.sent.clear()
    await update_entity(hass, switch)
    assert fake_link.sent == []
    update_reads(hub)[LIGHT_SWITCH, "switch"] -= UPDATE_READ_INTERVAL
    await update_entity(hass, switch)
    assert [(dst, pdu) for _, dst, pdu in fake_link.sent] == [
        (LIGHT_SWITCH, M.generic_onoff_get())
    ]

    # *All lights* reads nothing of its own: its members publish
    fake_link.sent.clear()
    await update_entity(hass, entity_id(hass, "light", f"{MESH_UUID}-central-fef5"))
    assert fake_link.sent == []

    # the record goes with the entry
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.entry_id not in hass.data[UPDATE_READS]


async def test_update_entity_without_a_link_keeps_the_state(
    hass: HomeAssistant,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """No link: nothing is sent and nothing raised (Home Assistant logs a failed update as an error); a write that
    fails under the read is only logged too, and the cached state stays."""
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(True))
    await hass.async_block_till_done()
    fake_link.write_error = OSError("GATT write failed")
    try:
        await update_entity(hass, eid)
    finally:
        fake_link.write_error = None
    assert hass.config_entries.async_get_entry(init_integration.entry_id) is not None
    assert init_integration.runtime_data.states[LIGHT_SWITCH].on is True

    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert not init_integration.runtime_data.connected
    update_reads(init_integration.runtime_data).clear()
    fake_link.sent.clear()
    await update_entity(hass, eid)
    assert fake_link.sent == []
    assert init_integration.runtime_data.states[LIGHT_SWITCH].on is True


# --------------------------------------------------------------------------- locks (review-4 F4-2)

PID_LOCK = 0x0009
UNLOCKED = bytes.fromhex("00010000")
LOCKED = bytes.fromhex("02010000")  # the app's "lock the current state", no time limit
LOCKED_60 = bytes.fromhex("02013c00")  # ... for 60 s
MALFORMED = bytes.fromhex("02")  # too short for the codec


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up against the answering mesh and let the per-link reads through."""
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)


def report_lock(link: FakeProxyLink, element: int, value: bytes) -> None:
    """The load reports its lock function unasked (the Status of a Set from the app, say)."""
    link.inject(element, OUR_ADDRESS, ph.vendor_status(0x05, PID_LOCK, value))


async def test_a_locked_light_shows_it_and_refuses_commands(
    hass: HomeAssistant, answering_mesh: FakeProxyLink, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """F4-2: every light reads its lock once per link (the *Lock* switch is disabled: nothing else asks), shows it
    as `locked`, and refuses commands while locked — the lock read moments ago is not asked again; once the load
    reports the unlock, commands go out again. A malformed lock leaves the state as it was."""
    mesh.values[LIGHT_SWITCH, PID_LOCK] = LOCKED
    mesh.values[LIGHT_DIMMER, PID_LOCK] = LOCKED
    await start(hass, mock_config_entry)
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    state = hass.states.get(eid)
    assert state.attributes["locked"] is True
    assert state.attributes["lock_until"] is None  # no time limit
    other = hass.states.get(entity_id(hass, "light", UID_LIGHT_CTL))
    assert other.attributes["locked"] is False
    assert mesh.gets.count((LIGHT_SWITCH, PID_LOCK)) == 1

    answering_mesh.sent.clear()
    with pytest.raises(ServiceValidationError) as exc:
        await turn_on(hass, eid)
    assert exc.value.translation_key == "load_locked"
    assert exc.value.translation_placeholders == {"entity": eid}
    with pytest.raises(ServiceValidationError):
        await turn_off(hass, eid)
    dimmer = entity_id(hass, "light", UID_LIGHT_DIMMER)
    for service, data in (
        ("start_dim", {"direction": "up"}),
        ("step_dim", {"step": 10}),
    ):
        with pytest.raises(ServiceValidationError):
            await dim(hass, service, dimmer, **data)
    assert load_sets(answering_mesh) == []  # nothing went out
    assert (
        mesh.gets.count((LIGHT_SWITCH, PID_LOCK)) == 1
    )  # read within PROPERTY_READ_FRESH

    report_lock(answering_mesh, LIGHT_SWITCH, MALFORMED)
    await settle(hass)
    assert hass.states.get(eid).attributes["locked"] is True
    report_lock(answering_mesh, LIGHT_SWITCH, UNLOCKED)
    await settle(hass)
    assert hass.states.get(eid).attributes["locked"] is False
    await turn_on(hass, eid)
    assert [dst for dst, _ in load_sets(answering_mesh)] == [LIGHT_SWITCH]
    assert hass.states.get(eid).state == STATE_ON


async def test_a_lock_lifted_unseen_is_read_again_before_a_refusal(
    hass: HomeAssistant, answering_mesh: FakeProxyLink, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """A lock lifted in the app is answered to the app alone: a command to a load shown locked asks again first,
    once the last answer is older than PROPERTY_READ_FRESH, and goes out when the load reports no lock."""
    mesh.values[LIGHT_SWITCH, PID_LOCK] = LOCKED
    await start(hass, mock_config_entry)
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    assert hass.states.get(eid).attributes["locked"] is True
    mesh.values[LIGHT_SWITCH, PID_LOCK] = UNLOCKED  # unlocked in the app
    answering_mesh.sent.clear()
    with (
        patch(
            "custom_components.junghome_ble.properties.reader.PROPERTY_READ_FRESH", 0
        ),
        patch("custom_components.junghome_ble.config_entities.PROPERTY_READ_FRESH", 0),
    ):
        await turn_on(hass, eid)
    assert mesh.gets.count((LIGHT_SWITCH, PID_LOCK)) == 2
    assert [dst for dst, _ in load_sets(answering_mesh)] == [LIGHT_SWITCH]
    assert hass.states.get(eid).attributes["locked"] is False


async def test_a_timed_lock_is_read_again_once_it_should_have_ended(
    hass: HomeAssistant, answering_mesh: FakeProxyLink, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
    freezer: FrozenDateTimeFactory,
) -> None:  # fmt: skip
    """A lock with a time limit shows when it should end (`lock_until`, counted from its report). Past it, a command
    reads the lock again however recent the last answer: still locked (relocked, say) is refused, with the end moved
    on; an unlock lets it through."""
    mesh.values[LIGHT_SWITCH, PID_LOCK] = LOCKED_60
    await start(hass, mock_config_entry)
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    hub = mock_config_entry.runtime_data
    until = hub.states[LIGHT_SWITCH].lock_until
    assert hass.states.get(eid).attributes["lock_until"] == until.isoformat()
    with pytest.raises(ServiceValidationError):
        await turn_on(hass, eid)
    assert (
        mesh.gets.count((LIGHT_SWITCH, PID_LOCK)) == 1
    )  # within the limit: no new read

    await tick(hass, freezer, 61)
    with pytest.raises(ServiceValidationError):
        await turn_on(hass, eid)  # read again: still locked
    assert mesh.gets.count((LIGHT_SWITCH, PID_LOCK)) == 2
    assert hub.states[LIGHT_SWITCH].lock_until > until

    await tick(hass, freezer, 61)
    mesh.values[LIGHT_SWITCH, PID_LOCK] = (
        UNLOCKED  # the lock ran out, and the load told no one
    )
    answering_mesh.sent.clear()
    await turn_on(hass, eid)
    assert mesh.gets.count((LIGHT_SWITCH, PID_LOCK)) == 3
    assert [dst for dst, _ in load_sets(answering_mesh)] == [LIGHT_SWITCH]


async def test_a_lock_past_its_time_limit_does_not_block_a_silent_load(
    hass: HomeAssistant, answering_mesh: FakeProxyLink, mesh: PropertyMesh, fast_timeouts: None,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """A lock past its time limit is over by its own account: a load that does not answer the read is commanded."""
    mesh.values[LIGHT_SWITCH, PID_LOCK] = LOCKED_60
    await start(hass, mock_config_entry)
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    hub = mock_config_entry.runtime_data
    hub.states[LIGHT_SWITCH].lock_until = dt_util.utcnow() - timedelta(seconds=1)
    mesh.silent.add((LIGHT_SWITCH, PID_LOCK))
    answering_mesh.sent.clear()
    await turn_off(hass, eid)
    assert (
        mesh.gets.count((LIGHT_SWITCH, PID_LOCK)) == 1 + 3
    )  # asked: 3 attempts, unanswered
    assert [dst for dst, _ in load_sets(answering_mesh)] == [LIGHT_SWITCH]
