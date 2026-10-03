"""SIG setup states as config entities: behaviour after mains return, brightness range, switch-on values.

The fake proxy's loads answer the setup Gets and Sets (`conftest.SETUP_SERVED`) with the values the installation's
0148 / 0232 reported: restore, range 3084..65535, switch-on brightness 100 %, 2700 K.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.number import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_VALUE,
    SERVICE_SET_VALUE,
)
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.select import SERVICE_SELECT_OPTION
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.junghome_ble import config_entities as C
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode
from custom_components.junghome_ble.number import lightness_of, lightness_percent

from . import property_helpers as ph
from .conftest import (
    ELEMENT_GROUP,
    SETUP_SERVED,
    SETUP_SETS,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    OUR_ADDRESS,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    entity_id,
)
from .property_helpers import PropertyMesh, fake_hub
from .test_input_edges import blinds_hub

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

fast_timeouts, mesh = ph.fast_timeouts, ph.mesh

UID_POWER_ON = f"{UID_LIGHT_DIMMER}-power_on_behaviour"
UID_MIN = f"{UID_LIGHT_DIMMER}-lightness_min"
UID_MAX = f"{UID_LIGHT_DIMMER}-lightness_max"
UID_DEFAULT = f"{UID_LIGHT_DIMMER}-default_lightness"
UID_USE_LAST = f"{UID_LIGHT_DIMMER}-use_last_lightness"
UID_CTL_DEFAULT = f"{UID_LIGHT_CTL}-default_color_temp"
UID_CTL_DEFAULT_LIGHTNESS = f"{UID_LIGHT_CTL}-default_lightness"
UID_CTL_USE_LAST = f"{UID_LIGHT_CTL}-use_last_lightness"


def test_targets_follow_the_models_of_each_load() -> None:
    hub = fake_hub()
    power_on = C.setup_targets(hub, "select")
    # every light and socket (1007, or the Lightness Setup Server extending it), expert: off by default
    assert [t.address for t in power_on] == [
        0x0148,
        0x0232,
        0x0172,
        0x0300,
        0x0400,
        0x0401,
    ]
    assert not any(t.enabled_default for t in power_on)
    numbers = C.setup_targets(hub, "number")
    assert (
        [(t.address, t.entity) for t in numbers]
        == [
            (0x0232, "lightness_min"),
            (0x0232, "lightness_max"),
            (0x0232, "default_lightness"),
            (0x0232, "default_color_temp"),
            (0x0232, "color_temp_min"),
            (0x0232, "color_temp_max"),
            (0x0300, "lightness_min"),
            (0x0300, "lightness_max"),
            (0x0300, "default_lightness"),
        ]
    )  # dimmers only; the colour temperature and its range on the DALI insert only (1304)
    # the lamp's first Parameters page; the white area is expert
    assert [t.entity for t in numbers if not t.enabled_default] == [
        "color_temp_min",
        "color_temp_max",
    ]
    assert [t.address for t in C.setup_targets(hub, "switch")] == [0x0232, 0x0300]
    assert C.setup_targets(hub, "button") == []
    targets = power_on + numbers
    assert all(t.specs == () and t.key is None for t in targets)
    assert numbers[0].unique_id == f"{UID_LIGHT_CTL}-lightness_min"
    assert numbers[0].translation_key == "lightness_min"
    assert len({t.unique_id for t in targets}) == len(targets)


def test_blinds_have_their_own_power_on_property() -> None:
    """The blind actuators host the Power OnOff Setup Server, but their behaviour is 0x1105 (move_on_power_mode)."""
    addresses = {t.address for t in C.setup_targets(blinds_hub(), "select")}
    assert addresses == {0x0148, 0x0232, 0x0172, 0x0300, 0x0400, 0x0401}


async def test_status_handler_caches_setup_states(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = init_integration.runtime_data
    fake_link.inject(
        LIGHT_CTL,
        OUR_ADDRESS,
        encode_opcode(M.LIGHT_CTL_DEFAULT_STATUS) + bytes.fromhex("0080b80bf6ff"),
    )
    fake_link.inject(
        LIGHT_DIMMER,
        OUR_ADDRESS,
        encode_opcode(M.LIGHT_LIGHTNESS_RANGE_STATUS) + b"\x00\x01",
    )  # truncated: not a range
    await hass.async_block_till_done()
    setup = hub.states[LIGHT_CTL].setup
    # the CTL Default's lightness is the Lightness Default
    assert setup[M.LIGHT_CTL_DEFAULT_STATUS] == bytes.fromhex("0080b80bf6ff")
    assert setup[M.LIGHT_LIGHTNESS_DEFAULT_STATUS] == bytes.fromhex("0080")
    assert LIGHT_DIMMER not in hub.states or not hub.states[LIGHT_DIMMER].setup


@pytest.fixture
def expert_enabled(hass: HomeAssistant) -> None:
    """The user enabled the dimmer's behaviour after mains return (expert, off by default)."""
    er.async_get(hass).async_get_or_create(
        "select", DOMAIN, UID_POWER_ON, disabled_by=None
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass, 200)


def _sets(link: FakeProxyLink, opcode: int) -> list[tuple[int, bytes]]:
    """(dst, params) of every access message with `opcode` the hub sent."""
    out = []
    for _, dst, access in link.sent:
        op, _, p = decode_opcode(access)
        if op == opcode:
            out.append((dst, p))
    return out


async def _call(
    hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]
) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


async def test_power_on_behaviour_is_read_and_set(
    hass: HomeAssistant, expert_enabled: None, fake_link: FakeProxyLink, mesh: PropertyMesh,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    eid = entity_id(hass, "select", UID_POWER_ON)
    state = hass.states.get(eid)
    assert state.state == "restore"
    assert state.attributes["options"] == ["off", "on", "restore"]
    assert state.attributes["mesh_address"] == "0300"
    entry = er.async_get(hass).async_get(eid)
    assert entry.entity_category is EntityCategory.CONFIG
    # the disabled ones are never asked
    assert {dst for dst, _ in _sets(fake_link, M.GEN_ONPOWERUP_GET)} == {LIGHT_DIMMER}

    await _call(
        hass,
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: eid, ATTR_OPTION: "off"},
    )
    assert _sets(fake_link, M.GEN_ONPOWERUP_SET) == [(LIGHT_DIMMER, b"\x00")]
    assert hass.states.get(eid).state == "off"

    # a value the spec does not define is no option
    fake_link.inject(
        LIGHT_DIMMER, OUR_ADDRESS, encode_opcode(M.GEN_ONPOWERUP_STATUS) + b"\x07"
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_UNKNOWN


async def test_brightness_range_moves_one_end_and_keeps_the_other(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    low, high = entity_id(hass, "number", UID_MIN), entity_id(hass, "number", UID_MAX)
    assert hass.states.get(low).state == "5"  # 3084 / 65535
    assert hass.states.get(high).state == "100"
    assert hass.states.get(low).attributes["unit_of_measurement"] == "%"
    assert (
        hass.states.get(low).attributes[ATTR_MIN],
        hass.states.get(low).attributes[ATTR_MAX],
    ) == (1, 100)
    assert (
        len(_sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_GET)) == 2
    )  # 0232 and 0300, once for both ends each

    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: low, ATTR_VALUE: 10}
    )
    # 10 % = 6554, the maximum as read; the dimmer answers with a publication to its group only
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_SET) == [
        (LIGHT_DIMMER, (6554).to_bytes(2, "little") + b"\xff\xff")
    ]
    assert fake_link.sent[-1][2] == M.light_lightness_range_set(
        6554, 0xFFFF
    )  # no read-back
    assert hass.states.get(low).state == "10"
    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: high, ATTR_VALUE: 80}
    )
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_SET)[-1] == (
        LIGHT_DIMMER, (6554).to_bytes(2, "little") + (52428).to_bytes(2, "little"),
    )  # fmt: skip
    assert hass.states.get(high).state == "80"

    # a minimum above the maximum is refused before anything is sent
    with pytest.raises(HomeAssistantError) as exc:
        await _call(
            hass,
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: low, ATTR_VALUE: 90},
        )
    assert exc.value.translation_key == "value_rejected"
    assert len(_sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_SET)) == 2


async def test_switch_on_brightness_and_previous_value(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    number, switch = (
        entity_id(hass, "number", UID_DEFAULT),
        entity_id(hass, "switch", UID_USE_LAST),
    )
    assert hass.states.get(number).state == "100"
    assert hass.states.get(switch).state == STATE_OFF

    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: switch})
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_DEFAULT_SET)[-1] == (
        LIGHT_DIMMER,
        b"\x00\x00",
    )
    assert hass.states.get(switch).state == STATE_ON
    # the last brightness is used: the app greys the switch-on brightness out, HA makes it unavailable, and a
    # value for it changes nothing
    assert hass.states.get(number).state == STATE_UNAVAILABLE
    sets = len(_sets(fake_link, M.LIGHT_LIGHTNESS_DEFAULT_SET))
    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: number, ATTR_VALUE: 50}
    )
    assert len(_sets(fake_link, M.LIGHT_LIGHTNESS_DEFAULT_SET)) == sets
    assert hass.states.get(switch).state == STATE_ON

    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: switch})
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_DEFAULT_SET)[-1] == (
        LIGHT_DIMMER,
        b"\xff\xff",
    )  # the app's 100 %
    assert hass.states.get(number).state == "100"
    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: number, ATTR_VALUE: 50}
    )
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_DEFAULT_SET)[-1] == (
        LIGHT_DIMMER, (32768).to_bytes(2, "little"),
    )  # fmt: skip
    assert hass.states.get(number).state == "50"
    assert hass.states.get(switch).state == STATE_OFF


async def test_switch_on_colour_temperature_keeps_lightness_and_delta_uv(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    fake_link.setup[LIGHT_CTL, M.LIGHT_CTL_DEFAULT_STATUS] = (
        (0x8000).to_bytes(2, "little")
        + (2700).to_bytes(2, "little")
        + (-10).to_bytes(2, "little", signed=True)
    )
    await _setup(hass, mock_config_entry)
    eid = entity_id(hass, "number", UID_CTL_DEFAULT)
    state = hass.states.get(eid)
    assert state.state == "2700"
    assert state.attributes["unit_of_measurement"] == "K"
    # the app's fixed range until the light reports its own
    assert (state.attributes[ATTR_MIN], state.attributes[ATTR_MAX]) == (2000, 10000)
    hub = mock_config_entry.runtime_data
    fake_link.inject(
        LIGHT_CTL,
        OUR_ADDRESS,
        encode_opcode(M.LIGHT_CTL_TEMP_RANGE_STATUS)
        + bytes([0])
        + (2500).to_bytes(2, "little")
        + (5000).to_bytes(2, "little"),
    )
    await hass.async_block_till_done()
    assert hub.states[LIGHT_CTL].kelvin_min == 2500
    state = hass.states.get(eid)
    assert (state.attributes[ATTR_MIN], state.attributes[ATTR_MAX]) == (2500, 5000)

    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: eid, ATTR_VALUE: 3000}
    )
    assert fake_link.sent[-1] == (
        OUR_ADDRESS,
        LIGHT_CTL,
        M.light_ctl_default_set(0x8000, 3000, -10),
    )
    assert hass.states.get(eid).state == "3000"
    # the Status is the Lightness Default too: the DALI insert's previous-brightness switch follows
    assert (
        hass.states.get(entity_id(hass, "switch", UID_CTL_USE_LAST)).state == STATE_OFF
    )


async def test_switch_on_colour_temperature_keeps_the_switch_on_brightness_set_last(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """A Lightness Default Status updates the cached CTL Default: its Set sends the new brightness, not the one read."""
    await _setup(hass, mock_config_entry)
    brightness = entity_id(hass, "number", UID_CTL_DEFAULT_LIGHTNESS)
    temperature = entity_id(hass, "number", UID_CTL_DEFAULT)
    await _call(
        hass,
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: brightness, ATTR_VALUE: 50},
    )
    hub = mock_config_entry.runtime_data
    assert hub.states[LIGHT_CTL].setup[M.LIGHT_CTL_DEFAULT_STATUS] == (
        lightness_of(50).to_bytes(2, "little") + (2700).to_bytes(2, "little") + b"\x00\x00"
    )  # fmt: skip
    await _call(
        hass,
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: temperature, ATTR_VALUE: 3000},
    )
    assert fake_link.sent[-1] == (
        OUR_ADDRESS,
        LIGHT_CTL,
        M.light_ctl_default_set(lightness_of(50), 3000, 0),
    )
    assert hass.states.get(brightness).state == "50"


async def test_switch_on_colour_temperature_sends_the_lightness_default(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """The Set's lightness is the Light Lightness Default the app sends, not the CTL Default's copy of it."""
    await _setup(hass, mock_config_entry)
    temperature = entity_id(hass, "number", UID_CTL_DEFAULT)
    setup = mock_config_entry.runtime_data.states[LIGHT_CTL].setup
    setup[M.LIGHT_CTL_DEFAULT_STATUS] = (
        (0x8000).to_bytes(2, "little") + (2700).to_bytes(2, "little") + b"\x05\x00"
    )
    setup[M.LIGHT_LIGHTNESS_DEFAULT_STATUS] = lightness_of(30).to_bytes(2, "little")
    await _call(
        hass,
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: temperature, ATTR_VALUE: 3000},
    )
    assert fake_link.sent[-1] == (
        OUR_ADDRESS,
        LIGHT_CTL,
        M.light_ctl_default_set(lightness_of(30), 3000, 5),
    )


async def test_switch_on_colour_temperature_is_unavailable_with_the_previous_value(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """The app disables *Switch-on white value* while *Use previous value* is on (Lightness Default 0)."""
    await _setup(hass, mock_config_entry)
    temperature = entity_id(hass, "number", UID_CTL_DEFAULT)
    use_last = entity_id(hass, "switch", UID_CTL_USE_LAST)
    assert hass.states.get(temperature).state == "2700"
    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: use_last})
    assert hass.states.get(temperature).state == STATE_UNAVAILABLE
    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: use_last})
    assert hass.states.get(temperature).state == "2700"


async def test_unknown_state_is_read_first_and_never_guessed(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
) -> None:  # fmt: skip
    fake_link.setup_silent.add(LIGHT_DIMMER)
    await _setup(hass, mock_config_entry)
    low = entity_id(hass, "number", UID_MIN)
    assert hass.states.get(low).state == STATE_UNKNOWN
    with pytest.raises(HomeAssistantError) as exc:
        await _call(
            hass,
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: low, ATTR_VALUE: 10},
        )
    assert exc.value.translation_key == "setup_state_unknown"
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_SET) == []

    # the dimmer answers now: read first, then the Set with the maximum as read
    fake_link.setup_silent.clear()
    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: low, ATTR_VALUE: 10}
    )
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_SET) == [
        (LIGHT_DIMMER, (6554).to_bytes(2, "little") + b"\xff\xff")
    ]


async def test_unconfirmed_set_is_read_back(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    switch = entity_id(hass, "switch", UID_USE_LAST)
    fake_link.setup_silent.add(LIGHT_DIMMER)
    gets = len(_sets(fake_link, M.LIGHT_LIGHTNESS_DEFAULT_GET))
    # neither the Set nor the read-back answered: an error, not a success
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: switch})
    assert exc.value.translation_key == "setting_no_answer"
    assert exc.value.translation_placeholders == {"entity": switch}
    assert len(_sets(fake_link, M.LIGHT_LIGHTNESS_DEFAULT_GET)) > gets
    assert (
        hass.states.get(switch).state == STATE_OFF
    )  # nothing confirmed, nothing assumed


async def test_set_the_read_back_does_not_show_is_not_applied(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
) -> None:  # fmt: skip
    """An unanswered Set whose read-back reports the old value failed; one that reports the new value took."""
    await _setup(hass, mock_config_entry)
    switch = entity_id(hass, "switch", UID_USE_LAST)
    low = entity_id(hass, "number", UID_MIN)
    answer = fake_link._answer_setup

    def ignore_sets(src: int, dst: int, access: bytes) -> None:
        if decode_opcode(access)[0] not in SETUP_SETS:
            answer(src, dst, access)

    fake_link._answer_setup = ignore_sets  # type: ignore[method-assign]
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: switch})
    assert exc.value.translation_key == "setting_not_applied"
    assert exc.value.translation_placeholders == {"entity": switch}
    assert hass.states.get(switch).state == STATE_OFF  # still 100 %, as read back

    # the range's read-back repeats the Set after its status code: taken
    fake_link.setup[LIGHT_DIMMER, M.LIGHT_LIGHTNESS_RANGE_STATUS] = (
        bytes([0]) + (6554).to_bytes(2, "little") + b"\xff\xff"
    )
    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: low, ATTR_VALUE: 10}
    )
    assert hass.states.get(low).state == "10"


@pytest.mark.parametrize("code", [1, 2])  # Cannot Set Range Min, Cannot Set Range Max
async def test_range_status_that_cannot_set_is_not_applied(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], code: int,
) -> None:  # fmt: skip
    """A Range Status answering the Set with a status code other than Success: the dimmer kept its range."""
    await _setup(hass, mock_config_entry)
    low = entity_id(hass, "number", UID_MIN)
    answer = fake_link._answer_setup
    status = M.LIGHT_LIGHTNESS_RANGE_STATUS

    def cannot_set(src: int, dst: int, access: bytes) -> None:
        if decode_opcode(access)[0] != M.LIGHT_LIGHTNESS_RANGE_SET:
            answer(src, dst, access)
            return
        held = fake_link.setup.get((dst, status), SETUP_SERVED[status][1])
        fake_link.inject(
            dst, ELEMENT_GROUP, encode_opcode(status) + bytes([code]) + held[1:]
        )

    fake_link._answer_setup = cannot_set  # type: ignore[method-assign]
    gets = len(_sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_GET))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(
            hass,
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: low, ATTR_VALUE: 10},
        )
    assert exc.value.translation_key == "setting_not_applied"
    assert exc.value.translation_placeholders == {"entity": low}
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_SET) == [
        (LIGHT_DIMMER, (6554).to_bytes(2, "little") + b"\xff\xff")
    ]
    assert (
        len(_sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_GET)) == gets
    )  # answered: no read-back
    assert hass.states.get(low).state == "5"  # the range the Status reports


async def test_send_failure_raises(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    fake_link.write_error = ConnectionError("proxy disconnected")
    with pytest.raises(HomeAssistantError) as exc:
        await _call(
            hass,
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id(hass, "switch", UID_USE_LAST)},
        )
    assert exc.value.translation_key == "send_failed"


async def test_lost_link_aborts_the_read(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    await _setup(hass, mock_config_entry)
    reader = C.property_reader(hass, mock_config_entry.runtime_data)
    fake_link.write_error = ConnectionError("proxy disconnected")
    assert not await reader.read_setup(LIGHT_SWITCH, C.ON_POWER_UP)
    assert (
        reader.cached_setup(0x0999, C.LIGHTNESS_RANGE) is None
    )  # an element never heard from


def test_lightness_percent_round_trip() -> None:
    assert lightness_percent(0xFFFF) == 100
    assert lightness_percent(3084) == 5
    assert lightness_of(1) == 655
    assert lightness_of(100) == 0xFFFF
    assert all(lightness_percent(lightness_of(p)) == p for p in range(1, 101))


async def test_both_ends_of_the_range_set_at_once_keep_both(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """Both ends go out in one Range Set: moved together, the second Set starts from the first's result."""
    await _setup(hass, mock_config_entry)
    low, high = entity_id(hass, "number", UID_MIN), entity_id(hass, "number", UID_MAX)
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
        _call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: low, ATTR_VALUE: 10}),
        _call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: high, ATTR_VALUE: 80}),
    )  # fmt: skip
    assert _sets(fake_link, M.LIGHT_LIGHTNESS_RANGE_SET)[-1] == (
        LIGHT_DIMMER, (6554).to_bytes(2, "little") + (52428).to_bytes(2, "little"),
    )  # fmt: skip
    assert (hass.states.get(low).state, hass.states.get(high).state) == ("10", "80")


UID_WHITE_MIN = f"{UID_LIGHT_CTL}-color_temp_min"
UID_WHITE_MAX = f"{UID_LIGHT_CTL}-color_temp_max"


def ctl_range(kelvin_min: int, kelvin_max: int, code: int = 0) -> bytes:
    """A Light CTL Temperature Range Status `[status][min K][max K]`."""
    return (
        encode_opcode(M.LIGHT_CTL_TEMP_RANGE_STATUS)
        + bytes([code])
        + kelvin_min.to_bytes(2, "little")
        + kelvin_max.to_bytes(2, "little")
    )


@pytest.fixture
def white_area_enabled(hass: HomeAssistant) -> None:
    """The user enabled the DALI insert's white area (expert, off by default)."""
    for uid in (UID_WHITE_MIN, UID_WHITE_MAX):
        er.async_get(hass).async_get_or_create("number", DOMAIN, uid, disabled_by=None)


def serve_white_area(link: FakeProxyLink, *, apply: bool = True, code: int = 0) -> None:
    """Make the DALI insert's CTL Setup Server answer the range Get and Set, as a spec'd server does.

    `apply` off: the Set is neither answered nor applied (what the installation's DALI insert did on air,
    `hidden-features.md` §9). `code`: the status code a Set is answered with (1/2: Cannot Set Range Min / Max).
    """
    held = [2000, 6000]
    answer = link._answer_setup

    def setup_server(src: int, dst: int, access: bytes) -> None:
        op, _, p = decode_opcode(access)
        if dst != LIGHT_CTL or op not in (
            M.LIGHT_CTL_TEMP_RANGE_GET,
            M.LIGHT_CTL_TEMP_RANGE_SET,
        ):
            answer(src, dst, access)
            return
        if op == M.LIGHT_CTL_TEMP_RANGE_SET:
            if not apply:
                return
            if code == 0:
                held[:] = [
                    int.from_bytes(p[:2], "little"),
                    int.from_bytes(p[2:4], "little"),
                ]
        link.inject(dst, src, ctl_range(*held, code=code))

    link._answer_setup = setup_server  # type: ignore[method-assign]


async def test_white_area_moves_one_end_and_keeps_the_other(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], white_area_enabled: None,
) -> None:  # fmt: skip
    """A04: the app's white area, one Light CTL Temperature Range Set `[min K][max K]` to the CTL Setup Server's
    element with the other end as read (`p234v7/V0.java`); 2000..10000 K as the app's slider, a minimum above the
    maximum refused. The range the light reports is its colour-temperature limits too."""
    serve_white_area(fake_link)
    await _setup(hass, mock_config_entry)
    low = entity_id(hass, "number", UID_WHITE_MIN)
    high = entity_id(hass, "number", UID_WHITE_MAX)
    light = entity_id(hass, "light", UID_LIGHT_CTL)
    # the entities send no Get of their own: the range is the connect-time refresh's (its Status, as the light's)
    assert _sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_GET) == []
    assert hass.states.get(low).state == STATE_UNKNOWN
    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, ctl_range(2000, 6000))
    await hass.async_block_till_done()
    assert (hass.states.get(low).state, hass.states.get(high).state) == ("2000", "6000")
    state = hass.states.get(low)
    assert state.attributes["unit_of_measurement"] == "K"
    assert (state.attributes[ATTR_MIN], state.attributes[ATTR_MAX]) == (2000, 10000)
    entry = er.async_get(hass).async_get(low)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG

    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: low, ATTR_VALUE: 2700}
    )
    assert _sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_SET) == [
        (LIGHT_CTL, (2700).to_bytes(2, "little") + (6000).to_bytes(2, "little"))
    ]
    assert fake_link.sent[-1] == (
        OUR_ADDRESS,
        LIGHT_CTL,
        M.light_ctl_temperature_range_set(2700, 6000),
    )  # answered: no read-back
    assert hass.states.get(low).state == "2700"
    assert hass.states.get(light).attributes["min_color_temp_kelvin"] == 2700

    await _call(
        hass,
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: high, ATTR_VALUE: 10000},
    )
    assert _sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_SET)[-1] == (
        LIGHT_CTL, (2700).to_bytes(2, "little") + (10000).to_bytes(2, "little"),
    )  # fmt: skip
    assert hass.states.get(high).state == "10000"
    assert hass.states.get(light).attributes["max_color_temp_kelvin"] == 10000

    # a minimum above the maximum is refused before anything is sent, and so is a value outside the app's slider
    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: high, ATTR_VALUE: 3000}
    )
    for entity, value in ((low, 5000), (high, 12000), (low, 1500)):
        with pytest.raises(HomeAssistantError):
            await _call(
                hass,
                NUMBER_DOMAIN,
                SERVICE_SET_VALUE,
                {ATTR_ENTITY_ID: entity, ATTR_VALUE: value},
            )
    assert len(_sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_SET)) == 3

    # the other end as read, clamped into 2000..10000 as the app clamps both ends of the range it writes
    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, ctl_range(800, 20000))
    await hass.async_block_till_done()
    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: low, ATTR_VALUE: 2500}
    )
    assert _sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_SET)[-1] == (
        LIGHT_CTL, (2500).to_bytes(2, "little") + (10000).to_bytes(2, "little"),
    )  # fmt: skip


async def test_white_area_unknown_is_read_first(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], white_area_enabled: None,
) -> None:  # fmt: skip
    """A range the refresh did not get is read before a change, never guessed."""
    await _setup(hass, mock_config_entry)  # nothing answers the refresh's range Get
    low = entity_id(hass, "number", UID_WHITE_MIN)
    assert hass.states.get(low).state == STATE_UNKNOWN
    serve_white_area(fake_link)
    await _call(
        hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: low, ATTR_VALUE: 3000}
    )
    assert _sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_GET)[-1] == (LIGHT_CTL, b"")
    assert _sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_SET) == [
        (LIGHT_CTL, (3000).to_bytes(2, "little") + (6000).to_bytes(2, "little"))
    ]


@pytest.mark.parametrize(
    ("apply", "code"),
    [
        (
            False,
            0,
        ),  # unanswered and not applied, as on air: the read-back shows the old range
        (True, 1),  # Cannot Set Range Min
        (True, 2),  # Cannot Set Range Max
    ],
)
async def test_white_area_the_light_does_not_take_is_not_applied(
    hass: HomeAssistant, fake_link: FakeProxyLink, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
    white_area_enabled: None, apply: bool, code: int,
) -> None:  # fmt: skip
    """A range the light keeps is an error, not a success, and the entity keeps showing the light's range."""
    serve_white_area(fake_link, apply=apply, code=code)
    await _setup(hass, mock_config_entry)
    low = entity_id(hass, "number", UID_WHITE_MIN)
    fake_link.inject(LIGHT_CTL, OUR_ADDRESS, ctl_range(2000, 6000))
    await hass.async_block_till_done()
    gets = len(_sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_GET))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(
            hass,
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: low, ATTR_VALUE: 2700},
        )
    assert exc.value.translation_key == "setting_not_applied"
    assert exc.value.translation_placeholders == {"entity": low}
    # read back only when nothing answered the Set
    assert len(_sets(fake_link, M.LIGHT_CTL_TEMP_RANGE_GET)) == gets + (not apply)
    assert hass.states.get(low).state == "2000"
