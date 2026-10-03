"""The app's central functions: *All lights* (0xFEF5) and *All sockets* (0xFEF8), one group message each; a room's area sheet."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError

from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import (
    ALL_LIGHTS,
    ALL_SOCKETS,
    build_devices,
)
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode
from custom_components.junghome_ble.light import JungHomeAllLights

from .conftest import CDB_PATH, FakeProxyLink, settle
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_OUT2,
    LIGHT_SWITCH,
    MESH_UUID,
    OUR_ADDRESS,
    SOCKET,
    entity_id,
    lightness_status,
    onoff_status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

UID_ALL_LIGHTS = f"{MESH_UUID}-central-fef5"
UID_ALL_SOCKETS = f"{MESH_UUID}-central-fef8"


def group_sends(link: FakeProxyLink, group: int) -> list[tuple[int, bytes]]:
    """(opcode, params) of every message the hub sent to `group`."""
    out = []
    for src, dst, access in link.sent:
        if src == OUR_ADDRESS and dst == group:
            op, _, params = decode_opcode(access)
            out.append((op, params))
    return out


def test_members_are_the_loads_listening_to_the_group() -> None:
    cdb = CDB.load(CDB_PATH)
    devices = build_devices(cdb)
    assert [d.address for d in devices.central[ALL_LIGHTS]] == [
        LIGHT_SWITCH,
        LIGHT_CTL,
        LIGHT_DIMMER,
        LIGHT_OUT1,
        LIGHT_OUT2,
    ]
    assert [d.address for d in devices.central[ALL_SOCKETS]] == [SOCKET]
    # a load that left the group is no member; a group nobody listens to has no entity
    for element in (cdb.element(SOCKET),):
        assert element is not None
        for raw in element.raw_models:
            raw["subscribe"] = [a for a in raw.get("subscribe", []) if a != "FEF8"]
    assert ALL_SOCKETS not in build_devices(cdb).central


async def test_all_lights_follows_its_members_and_switches_them_with_one_message(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "light", UID_ALL_LIGHTS)
    state = hass.states.get(eid)
    # nobody reported yet (the fake mesh does not answer the refresh)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["supported_color_modes"] == [ColorMode.BRIGHTNESS]
    assert state.attributes["mesh_address"] == "FEF5"
    assert "Living room DALI" in state.attributes["members"]

    fake_link.inject(LIGHT_SWITCH, 0xC061, onoff_status(False))
    await settle(hass)
    assert hass.states.get(eid).state == STATE_OFF
    fake_link.inject(LIGHT_DIMMER, 0xC070, onoff_status(True))
    fake_link.inject(LIGHT_DIMMER, 0xC070, lightness_status(0x8000))
    await settle(hass)
    state = hass.states.get(eid)
    assert state.state == STATE_ON  # one member is
    # the mean of the dimmable members that are on
    assert state.attributes[ATTR_BRIGHTNESS] == 128

    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    # unacknowledged, so the members do not all answer at once
    assert group_sends(fake_link, ALL_LIGHTS)[-1][0] == M.GEN_ONOFF_SET_UNACK
    assert group_sends(fake_link, ALL_LIGHTS)[-1][1][0] == 1
    fake_link.sent.clear()
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: eid, ATTR_BRIGHTNESS: 51},
        blocking=True,
    )
    sends = group_sends(fake_link, ALL_LIGHTS)
    # the lightness first (the dimmers take it), then on (the switched loads)
    assert [op for op, _ in sends] == [
        M.LIGHT_LIGHTNESS_SET_UNACK,
        M.GEN_ONOFF_SET_UNACK,
    ]
    assert int.from_bytes(sends[0][1][:2], "little") == 13107  # 20 %
    fake_link.sent.clear()
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert [(op, p[0]) for op, p in group_sends(fake_link, ALL_LIGHTS)] == [
        (M.GEN_ONOFF_SET_UNACK, 0)
    ]


async def test_all_sockets(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "switch", UID_ALL_SOCKETS)
    fake_link.inject(SOCKET, 0xC000, onoff_status(True))
    await settle(hass)
    assert hass.states.get(eid).state == STATE_ON
    for service, value in ((SERVICE_TURN_OFF, 0), (SERVICE_TURN_ON, 1)):
        await hass.services.async_call(
            SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: eid}, blocking=True
        )
        assert group_sends(fake_link, ALL_SOCKETS)[-1][0] == M.GEN_ONOFF_SET_UNACK
        assert group_sends(fake_link, ALL_SOCKETS)[-1][1][0] == value


async def test_send_failure_and_lost_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    eid = entity_id(hass, "switch", UID_ALL_SOCKETS)
    fake_link.write_error = ConnectionError("proxy disconnected")
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
        )
    assert exc.value.translation_key == "send_failed"
    fake_link.write_error = None
    mock_bluetooth_env["infos"] = []  # no other proxy in sight: the link stays down
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get(eid).state == STATE_UNAVAILABLE


async def test_switched_loads_only_make_an_on_off_group(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hub = init_integration.runtime_data
    switched = [
        m
        for m in hub.devices.central[ALL_LIGHTS]
        if m.address in (LIGHT_OUT1, LIGHT_SWITCH)
    ]
    entity = JungHomeAllLights(hub, switched)
    assert entity.supported_color_modes == {ColorMode.ONOFF}
    assert entity.brightness is None


# --------------------------------------------------------------------------- a room's central control (the area sheet)

ROOM_WC, ROOM_KITCHEN = 0xC00F, 0xC011


def sends(link: FakeProxyLink) -> list[tuple[int, int, bytes]]:
    """(destination, opcode, params) of every message the hub sent."""
    out = []
    for src, dst, access in link.sent:
        if src == OUR_ADDRESS:
            op, _, params = decode_opcode(access)
            out.append((dst, op, params))
    return out


async def test_room_lights_dim_with_one_message_and_switch_each_light(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The app's area sheet: a dim level to the room address (`Dim.Group`), on / off per light, all unacknowledged."""
    eid = entity_id(hass, "light", f"{MESH_UUID}-room-c00f-lights")
    state = hass.states.get(eid)
    assert state.name.endswith("All lights in WC")
    assert state.attributes["mesh_address"] == "C00F"
    assert state.attributes["members"] == ["WC mirror", "WC ceiling"]
    assert state.attributes["supported_color_modes"] == [ColorMode.BRIGHTNESS]
    fake_link.inject(LIGHT_DIMMER, 0xC070, onoff_status(True))
    await settle(hass)
    assert hass.states.get(eid).state == STATE_ON
    fake_link.sent.clear()

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: eid, ATTR_BRIGHTNESS: 51},
        blocking=True,
    )
    sent = sends(fake_link)
    assert [(dst, op) for dst, op, _ in sent] == [
        (ROOM_WC, M.LIGHT_LIGHTNESS_SET_UNACK),
        (LIGHT_SWITCH, M.GEN_ONOFF_SET_UNACK),
        (LIGHT_DIMMER, M.GEN_ONOFF_SET_UNACK),
    ]
    assert int.from_bytes(sent[0][2][:2], "little") == 13107  # 20 %
    assert [p[0] for _, _, p in sent[1:]] == [1, 1]
    fake_link.sent.clear()
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    # never on / off to the room address: the room's sockets listen there too
    assert [(dst, op, p[0]) for dst, op, p in sends(fake_link)] == [
        (LIGHT_SWITCH, M.GEN_ONOFF_SET_UNACK, 0),
        (LIGHT_DIMMER, M.GEN_ONOFF_SET_UNACK, 0),
    ]


async def test_room_sockets_switch_each_socket(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "switch", f"{MESH_UUID}-room-c011-sockets")
    assert hass.states.get(eid).name.endswith("All sockets in Kitchen")
    # the Kitchen's lights are switched loads only: no brightness
    kitchen = hass.states.get(entity_id(hass, "light", f"{MESH_UUID}-room-c011-lights"))
    assert kitchen.attributes["supported_color_modes"] == [ColorMode.ONOFF]
    fake_link.sent.clear()
    for service, value in ((SERVICE_TURN_ON, 1), (SERVICE_TURN_OFF, 0)):
        await hass.services.async_call(
            SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: eid}, blocking=True
        )
        assert sends(fake_link)[-1][:2] == (SOCKET, M.GEN_ONOFF_SET_UNACK)
        assert sends(fake_link)[-1][2][0] == value
    assert not [s for s in sends(fake_link) if s[0] == ROOM_KITCHEN]
