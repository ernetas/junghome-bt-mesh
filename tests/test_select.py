"""Select platform: enumerated device parameters and LED colours."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh import messages as M

from . import property_helpers as ph
from .conftest import FakeProxyLink, settle, setup_entry, wait_for_link
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    OUR_ADDRESS,
    SOCKET,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    UID_SOCKET,
    entity_id,
)
from .property_helpers import (
    PID_DIM_MODE,
    PID_LED1_OFF,
    PID_LED1_ON,
    PID_LED2_ON,
    PropertyMesh,
    vendor_status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

fast_timeouts, mesh, init_with_mesh = ph.fast_timeouts, ph.mesh, ph.init_with_mesh

UID_DIM_MODE = f"{UID_LIGHT_DIMMER}-dim_mode"
UID_LED_ON = f"{UID_LIGHT_SWITCH}-led1_mode_on"
UID_LED_OFF = f"{UID_LIGHT_SWITCH}-led1_mode_off"
UID_SOCKET_LED_ON = f"{UID_SOCKET}-led1_mode_on"
UID_ROCKER_LED2_ON = f"{UID_LIGHT_CTL}-led2_mode_on"
PALETTE = [
    "red",
    "green",
    "white",
    "blue",
    "violet",
    "orange",
    "yellow",
    "cyan",
    "no_color",
]


async def test_enum_select(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    """Dim mode is an expert parameter of dimmer / DALI inserts: enabled by the user, then read and written."""
    er.async_get(hass).async_get_or_create(
        "select", DOMAIN, UID_DIM_MODE, disabled_by=None
    )
    mesh.values[LIGHT_DIMMER, PID_DIM_MODE] = b"\x02"  # trailing edge
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "select", UID_DIM_MODE)
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == "trailing_edge"
    assert state.attributes[ATTR_OPTIONS] == [
        "leading_edge",
        "trailing_edge",
    ]  # "unknown" is not selectable
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG

    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: eid, ATTR_OPTION: "leading_edge"},
        blocking=True,
    )
    # the app sets DimMode with userAccess = 1: `C3 27 05 [13 00][01][01]`
    assert mesh.link.sent[-1] == (
        OUR_ADDRESS, LIGHT_DIMMER, M.vendor_property_set("admin", PID_DIM_MODE, b"\x01", user_access=1),
    )  # fmt: skip
    assert hass.states.get(eid).state == "leading_edge"

    # a value outside the table (the app's "unknown", 5) is no option
    mesh.link.inject(LIGHT_DIMMER, 0xC070, vendor_status(0x05, PID_DIM_MODE, b"\x05"))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_UNKNOWN
    mesh.link.inject(LIGHT_DIMMER, 0xC070, vendor_status(0x05, PID_DIM_MODE, b"\x09"))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_UNKNOWN


async def test_dim_mode_only_on_dimmable_inserts(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("select", DOMAIN, UID_DIM_MODE)
    # a tunable-white (DALI) load is not `DimModeCompatible` in the app
    assert (
        registry.async_get_entity_id("select", DOMAIN, f"{UID_LIGHT_CTL}-dim_mode")
        is None
    )
    assert (
        registry.async_get_entity_id("select", DOMAIN, f"{UID_LIGHT_SWITCH}-dim_mode")
        is None
    )
    assert (
        registry.async_get_entity_id("select", DOMAIN, f"{UID_SOCKET}-dim_mode") is None
    )


async def test_led_colour_select(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    mesh.values[LIGHT_SWITCH, PID_LED1_ON] = bytes(
        [100, 0, 0, 5]
    )  # red, night mode on (1-gang palette)
    mesh.values[LIGHT_SWITCH, PID_LED1_OFF] = bytes(
        [50, 50, 50, 0]
    )  # a colour the app cannot name
    mesh.values[SOCKET, PID_LED1_ON] = bytes(
        [0, 60, 0, 0]
    )  # green on the socket palette
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    on, off = (
        entity_id(hass, "select", UID_LED_ON),
        entity_id(hass, "select", UID_LED_OFF),
    )
    assert on == "select.wc_wc_mirror_button_led_colour_switched_on"
    assert hass.states.get(on).state == "red"
    assert hass.states.get(on).attributes[ATTR_OPTIONS] == PALETTE
    assert hass.states.get(off).state == STATE_UNKNOWN
    assert (
        hass.states.get(entity_id(hass, "select", UID_SOCKET_LED_ON)).state == "green"
    )
    # LED properties are read from and written to the primary element, not the key
    assert (LIGHT_SWITCH, PID_LED1_ON) in mesh.gets
    assert not any(addr == 0x0149 for addr, _ in mesh.gets)

    # the colour changes, the night-mode byte is kept: `C3 27 05 [01 A0][03][00 04 64 05]`
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: on, ATTR_OPTION: "blue"},
        blocking=True,
    )
    assert mesh.link.sent[-1] == (
        OUR_ADDRESS, LIGHT_SWITCH, M.vendor_property_set("admin", PID_LED1_ON, bytes([0, 4, 100, 5])),
    )  # fmt: skip
    assert hass.states.get(on).state == "blue"
    # an LED whose colour the app cannot name keeps its night-mode byte (off) too
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: off, ATTR_OPTION: "no_color"},
        blocking=True,
    )
    assert mesh.sets[-1] == (LIGHT_SWITCH, PID_LED1_OFF, bytes([0, 0, 0, 0]))
    assert hass.states.get(off).state == "no_color"


async def test_led_colour_of_an_unread_led_reads_the_night_mode_first(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], fast_timeouts: None,
) -> None:  # fmt: skip
    """The night-mode byte is never guessed: an LED not known yet is read first; one that stays silent is not written."""
    mesh.silent.add((LIGHT_SWITCH, PID_LED1_ON))
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    on = entity_id(hass, "select", UID_LED_ON)
    assert hass.states.get(on).state == STATE_UNKNOWN
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            SELECT_DOMAIN,
            SERVICE_SELECT_OPTION,
            {ATTR_ENTITY_ID: on, ATTR_OPTION: "red"},
            blocking=True,
        )
    assert exc.value.translation_key == "led_colour_unknown"
    assert exc.value.translation_placeholders == {"entity": on}
    assert not any(pid == PID_LED1_ON for _, pid, _ in mesh.sets)

    # the LED answers now, in night mode: read, then written with the byte kept
    mesh.silent.clear()
    mesh.values[LIGHT_SWITCH, PID_LED1_ON] = bytes([0, 0, 0, 5])
    gets = mesh.gets.count((LIGHT_SWITCH, PID_LED1_ON))
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: on, ATTR_OPTION: "red"},
        blocking=True,
    )
    assert mesh.gets.count((LIGHT_SWITCH, PID_LED1_ON)) == gets + 1
    assert mesh.sets[-1] == (LIGHT_SWITCH, PID_LED1_ON, bytes([100, 0, 0, 5]))
    assert hass.states.get(on).state == "red"


async def test_second_led_of_a_two_gang(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, mesh: PropertyMesh
) -> None:
    eid = entity_id(hass, "select", UID_ROCKER_LED2_ON)
    assert eid == "select.living_room_living_room_rocker_led_colour_switched_on_b"
    assert hass.states.get(eid).state == "no_color"
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: eid, ATTR_OPTION: "red"},
        blocking=True,
    )
    # the 2-gang palette's red is (75, 0, 0), written to LED 2's slot on the primary element
    assert mesh.sets[-1] == (LIGHT_CTL, PID_LED2_ON, bytes([75, 0, 0, 0]))


async def test_send_failure_raises(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "select", UID_LED_ON)
    fake_link.write_error = ConnectionError("proxy disconnected")
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            SELECT_DOMAIN,
            SERVICE_SELECT_OPTION,
            {ATTR_ENTITY_ID: eid, ATTR_OPTION: "red"},
            blocking=True,
        )
    assert exc.value.translation_key == "send_failed"
