"""Light platform: switched loads, dimmers and tunable-white channels."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_SUPPORTED_COLOR_MODES,
    ColorMode,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from voluptuous import Invalid

from custom_components.junghome_ble.const import DIM_MOVE_TRANSITION, DOMAIN
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import Light

from .conftest import FakeProxyLink, load_sets, settle, wait_for_link
from .helpers import (
    LIGHT_CTL,
    LIGHT_CTL_TEMPERATURE,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    MESH_UUID,
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
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry


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
