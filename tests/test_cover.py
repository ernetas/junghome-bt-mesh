"""Cover platform: blinds, roller shutters and awnings on Generic Level elements (spec-derived, no hardware yet).

Runs against `fixtures/Blinds.json` (`make_blinds_fixture.py`): the standard synthetic network plus a blinds
actuator mini with a slat element (Kitchen blind), a push-button 2-gang with a blinds insert (Living room shutter)
and a blinds PP2 puck without slats. The device class of each comes from the `0x1104` operation mode the fake
mesh answers: 0 blinds, 1 shutter, 3 awning.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntityFeature,
    CoverState,
)
from homeassistant.components.cover import (
    DOMAIN as COVER_DOMAIN,
)
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.number import SERVICE_SET_VALUE
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.select import SERVICE_SELECT_OPTION
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_CLOSE_COVER,
    SERVICE_CLOSE_COVER_TILT,
    SERVICE_OPEN_COVER,
    SERVICE_OPEN_COVER_TILT,
    SERVICE_SET_COVER_POSITION,
    SERVICE_SET_COVER_TILT_POSITION,
    SERVICE_STOP_COVER,
    SERVICE_STOP_COVER_TILT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotSupported,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_UNICAST,
    COVER_MODE_PROPERTY,
    COVER_MOVE_TRANSITION,
    DOMAIN,
    LOCK_EXPIRY_MARGIN,
    REFERENCE_RUN_LONGEST,
    REFERENCE_RUN_MARGIN,
    SIGNAL_UPDATE,
)
from custom_components.junghome_ble.cover import (
    JungHomeAllBlinds,
    JungHomeCover,
    _to_ha,
    _to_level,
    closedness_to_level,
    level_to_closedness,
)
from custom_components.junghome_ble.entity import blind_device_info
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import (
    Blind,
    Light,
    Metadata,
    build_devices,
)
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode

from .conftest import (
    FIXTURES,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    LIGHT_CTL,
    LIGHT_SWITCH,
    NODE_BLIND,
    NODE_PUCK,
    NODE_SHUTTER,
    OUR_ADDRESS,
    PROPERTY_POWER_ON_TIME,
    admin_property_status,
    ctl_range_status,
    ctl_status,
    ctl_temperature_status,
    entity_id,
    lightness_status,
    onoff_status,
    sensor_replies,
)
from .property_helpers import PropertyMesh, vendor_status

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

BLINDS_PATH = str(FIXTURES / "Blinds.json")
BLIND, BLIND_SLAT = 0x0500, 0x0501  # blinds actuator mini "Kitchen blind"
SHUTTER, SHUTTER_SLAT = 0x0600, 0x0601  # push-button 2-gang with a blinds insert
AWNING = 0x0700  # blinds PP2 puck, position element only
UID_BLIND = f"{NODE_BLIND}-0001"
UID_SHUTTER = f"{NODE_SHUTTER}-0001"
UID_AWNING = f"{NODE_PUCK}-0001"
MODES = {BLIND: b"\x00", SHUTTER: b"\x01", AWNING: b"\x03"}
LEVEL_OPEN, LEVEL_CLOSED = -32768, 32767
POSITION_FEATURES = (
    CoverEntityFeature.OPEN
    | CoverEntityFeature.CLOSE
    | CoverEntityFeature.STOP
    | CoverEntityFeature.SET_POSITION
)
TILT_FEATURES = (
    CoverEntityFeature.OPEN_TILT
    | CoverEntityFeature.CLOSE_TILT
    | CoverEntityFeature.SET_TILT_POSITION
)
SLAT_FEATURES = (
    TILT_FEATURES | CoverEntityFeature.STOP_TILT
)  # one blind's slats; *All blinds* cannot stop them


def level_status(present: int, target: int | None = None, remaining: int = 0) -> bytes:
    """Generic Level Status `[present s16]` or `[present s16][target s16][remaining u8]`."""
    p = present.to_bytes(2, "little", signed=True)
    if target is not None:
        p += target.to_bytes(2, "little", signed=True) + bytes([remaining])
    return encode_opcode(M.GEN_LEVEL_STATUS) + p


def move_set(delta: int, tid: int) -> bytes:
    """The Generic Move Set the cover is expected to send: `[delta s16][tid][transition][delay 0]`."""
    return M.generic_move_set(delta, tid=tid, transition=COVER_MOVE_TRANSITION)


# what the lamps and the socket answer to the connect-time refresh and the energy poll (so it completes at once)
STATE_REPLIES = {
    M.generic_onoff_get(): onoff_status(False),
    M.light_ctl_get(): ctl_status(0, 4000),
    M.light_lightness_get(): lightness_status(0),
    **sensor_replies(),
    M.light_ctl_temperature_range_get(): ctl_range_status(2700, 6500),
    M.light_ctl_temperature_get(): ctl_temperature_status(4000),
    M.generic_property_get("admin", PROPERTY_POWER_ON_TIME): admin_property_status(
        PROPERTY_POWER_ON_TIME, (1).to_bytes(3, "little")
    ),
}


class LevelMesh:
    """Makes the fake mesh answer Generic Level Gets with the level each element holds (`levels[addr]`), and
    every other state Get of the refresh with a fixed reply; `silent` elements leave their Level Get unanswered.

    The same levels are the ones the fake's elements answer a Level / Delta / Move Set from (`FakeProxyLink.levels`):
    a Set moves them, a Move Set reports where the element is.
    """

    def __init__(self, link: FakeProxyLink, levels: dict[int, int]) -> None:
        self.link = link
        self.levels = levels
        link.levels = levels
        self.silent: set[int] = set()
        self.gets: list[int] = []  # destinations of every Level Get seen
        self._original = link.write_gatt_char
        link.write_gatt_char = self._write  # type: ignore[method-assign]

    async def _write(
        self, char: str, data: bytes, response: bool | None = None
    ) -> None:
        before = len(self.link.sent)
        await self._original(char, data, response)
        for src, dst, access in self.link.sent[before:]:
            op, cid, _ = decode_opcode(access)
            if cid is None and op == M.GEN_LEVEL_GET:
                self.gets.append(dst)
                if dst in self.levels and dst not in self.silent:
                    self.link.inject(dst, src, level_status(self.levels[dst]))
            elif (reply := STATE_REPLIES.get(access)) is not None:
                self.link.inject(dst, src, reply)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """The blinds network (a share export: names travel inside the file)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME blinds test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: BLINDS_PATH, CONF_UNICAST: "0D00"},
    )


@pytest.fixture
def mesh(fake_link: FakeProxyLink) -> PropertyMesh:
    """Every device parameter answers with a default, the blinds with their operation modes."""
    return PropertyMesh(
        fake_link, {(addr, COVER_MODE_PROPERTY): mode for addr, mode in MODES.items()}
    )


@pytest.fixture
def levels(fake_link: FakeProxyLink, mesh: PropertyMesh) -> LevelMesh:
    """Level Gets are answered: the blind half open, its slats closed, the shutter open, the awning closed."""
    return LevelMesh(
        fake_link,
        {
            BLIND: 0,
            BLIND_SLAT: LEVEL_CLOSED,
            SHUTTER: LEVEL_OPEN,
            SHUTTER_SLAT: LEVEL_OPEN,
            AWNING: LEVEL_CLOSED,
        },
    )


@pytest.fixture
async def init_blinds(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh,
    fast_sleep: list[float],
) -> MockConfigEntry:
    """The integration on the blinds network, the refresh and the operation-mode reads answered."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    return mock_config_entry


async def call(hass: HomeAssistant, service: str, eid: str, **data: Any) -> None:
    await hass.services.async_call(
        COVER_DOMAIN, service, {ATTR_ENTITY_ID: eid, **data}, blocking=True
    )


def last_sent(link: FakeProxyLink) -> tuple[int, int, bytes]:
    return link.sent[-1]


def cover_entity(hass: HomeAssistant, eid: str) -> JungHomeCover:
    platform = next(
        p
        for p in entity_platform.async_get_platforms(hass, DOMAIN)
        if p.domain == "cover"
    )
    entity = platform.entities[eid]
    assert isinstance(entity, JungHomeCover)
    return entity


# --------------------------------------------------------------------------- device model


def test_blinds_are_derived_from_the_export() -> None:
    """Position = first lamp-free level element, slats = the last one; a blinds-only puck is never a light."""
    cdb = CDB.load(Path(BLINDS_PATH))
    devices = build_devices(cdb, Metadata.from_export(cdb.export_meta))
    assert [
        (b.address, b.slat_address, b.name, b.rooms, b.kind) for b in devices.blinds
    ] == [
        (BLIND, BLIND_SLAT, "Kitchen blind", ["Kitchen"], "blind"),
        (SHUTTER, SHUTTER_SLAT, "Living room shutter", ["Living room"], "blind"),
        (AWNING, None, "Blinds PP2 actuator 0700", [], "blind"),
    ]
    blind, shutter, awning = devices.blinds
    assert (blind.unique_id, shutter.unique_id, awning.unique_id) == (
        UID_BLIND,
        UID_SHUTTER,
        UID_AWNING,
    )
    assert blind.level_elements == (BLIND, BLIND_SLAT)
    assert awning.level_elements == (AWNING,)
    assert all(
        isinstance(devices.by_address[a], Blind) for a in (BLIND, SHUTTER, AWNING)
    )
    # the slat elements and the puck's OnOff server produced nothing else
    assert BLIND_SLAT not in devices.by_address
    assert SHUTTER_SLAT not in devices.by_address
    assert "blind" in devices.kinds()
    # the lamps of the base network are untouched: the CTL dimmer's temperature element is still no device
    assert isinstance(devices.by_address[LIGHT_CTL], Light)
    assert LIGHT_CTL + 1 not in devices.by_address
    assert [light.address for light in devices.lights] == [
        LIGHT_SWITCH,
        LIGHT_CTL,
        0x0300,
        0x0400,
        0x0401,
    ]
    # the blinds' inputs are keys, grouped by the app's device entries
    assert [
        (b.address, b.group_name) for b in devices.buttons if b.address > 0x0500
    ] == [
        (0x0502, "Kitchen blind inputs"),
        (0x0503, "Kitchen blind inputs"),
        (0x0602, "Living room shutter rocker"),
        (0x0603, "Living room shutter rocker"),
        (0x0701, "Blinds PP2 actuator 0700 buttons"),
        (0x0702, "Blinds PP2 actuator 0700 buttons"),
    ]


def test_base_network_has_no_blinds(cdb: CDB) -> None:
    devices = build_devices(cdb)
    assert devices.blinds == []
    assert "blind" not in devices.kinds()


def test_blind_device_info() -> None:
    """A blind is a device of its own: named as in the app, in its room, under its node when that is registered."""
    cdb = CDB.load(Path(BLINDS_PATH))
    blind, _, awning = build_devices(cdb, Metadata.from_export(cdb.export_meta)).blinds
    hub = SimpleNamespace(device_ids={})
    assert blind_device_info(hub, blind) == {  # type: ignore[arg-type]
        "identifiers": {(DOMAIN, UID_BLIND)},
        "name": "Kitchen blind",
        "manufacturer": "JUNG",
        "model": "Blind / shutter drive",
        "suggested_area": "Kitchen",
    }
    hub.device_ids[f"node:{NODE_PUCK}"] = "node-registry-id"
    info = blind_device_info(hub, awning)  # type: ignore[arg-type]
    assert info["via_device_id"] == "node-registry-id"
    assert "suggested_area" not in info


# --------------------------------------------------------------------------- mapping


@pytest.mark.parametrize(
    ("level", "closedness", "position"),
    [
        (LEVEL_OPEN, 0, 100),
        (LEVEL_CLOSED, 100, 0),
        (0, 50, 50),
        (-16384, 25, 75),
        (16384, 75, 25),
        (-32440, 1, 99),  # 0.5005 %: the first level that rounds to 1 %
        (32440, 100, 0),
    ],
)
def test_level_to_position(level: int, closedness: int, position: int) -> None:
    assert level_to_closedness(level) == closedness
    assert _to_ha(level) == position


@pytest.mark.parametrize(
    ("position", "level"),
    [
        (100, LEVEL_OPEN),
        (0, LEVEL_CLOSED),
        (50, -32768 + 32768),  # 50 % closed = round(-32768 + 32767.5) = 0
        (75, -16384),
        (25, 16383),
        (1, 32112),
        (99, -32113),
        (150, LEVEL_OPEN),  # clamped
        (-5, LEVEL_CLOSED),
    ],
)
def test_position_to_level(position: int, level: int) -> None:
    assert _to_level(position) == level
    assert closedness_to_level(100 - max(0, min(100, position))) == level


def test_mapping_round_trips_at_the_ends() -> None:
    for position in (0, 100):
        assert _to_ha(_to_level(position)) == position
    for level in (LEVEL_OPEN, LEVEL_CLOSED):
        assert _to_level(_to_ha(level)) == level
    assert closedness_to_level(200) == LEVEL_CLOSED
    assert closedness_to_level(-1) == LEVEL_OPEN


# --------------------------------------------------------------------------- entities and refresh


async def test_entities_and_device_classes(
    hass: HomeAssistant, init_blinds: MockConfigEntry, levels: LevelMesh
) -> None:
    """One cover per blind; the class follows the operation mode, tilt only for blinds mode with a slat element."""
    blind = hass.states.get(entity_id(hass, "cover", UID_BLIND))
    assert blind is not None
    assert (
        blind.entity_id == "cover.kitchen_kitchen_blind"
    )  # area-prefixed like the lights
    assert blind.attributes[ATTR_DEVICE_CLASS] == CoverDeviceClass.BLIND
    assert (
        blind.attributes[ATTR_SUPPORTED_FEATURES] == POSITION_FEATURES | SLAT_FEATURES
    )
    assert blind.attributes["mesh_address"] == "0500"
    assert blind.attributes["slat_address"] == "0501"
    assert blind.attributes["rooms"] == ["Kitchen"]
    assert blind.attributes["operation_mode"] == "blinds"
    assert "assumed_state" not in blind.attributes
    # the refresh answered: level 0 = 50 % closed, slats fully closed
    assert blind.state == CoverState.OPEN
    assert blind.attributes[ATTR_CURRENT_POSITION] == 50
    assert blind.attributes[ATTR_CURRENT_TILT_POSITION] == 0

    shutter = hass.states.get(entity_id(hass, "cover", UID_SHUTTER))
    assert shutter is not None
    assert shutter.attributes[ATTR_DEVICE_CLASS] == CoverDeviceClass.SHUTTER
    assert shutter.attributes[ATTR_SUPPORTED_FEATURES] == POSITION_FEATURES
    assert (
        shutter.attributes["slat_address"] == "0601"
    )  # present, but not slats in shutter mode
    assert ATTR_CURRENT_TILT_POSITION not in shutter.attributes
    assert shutter.state == CoverState.OPEN
    assert shutter.attributes[ATTR_CURRENT_POSITION] == 100

    awning = hass.states.get(entity_id(hass, "cover", UID_AWNING))
    assert awning is not None
    assert awning.attributes[ATTR_DEVICE_CLASS] == CoverDeviceClass.AWNING
    assert awning.attributes[ATTR_SUPPORTED_FEATURES] == POSITION_FEATURES
    assert "slat_address" not in awning.attributes
    assert awning.state == CoverState.CLOSED
    assert awning.attributes[ATTR_CURRENT_POSITION] == 0

    # the refresh asked every level element, position before slats, after the lamps
    assert levels.gets[:5] == [BLIND, BLIND_SLAT, SHUTTER, SHUTTER_SLAT, AWNING]

    # each blind is a device of its own under its node, in the room's area
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, UID_BLIND), init_blinds.entry_id
    )
    assert device is not None
    assert device.name == "Kitchen blind"
    assert device.model == "Blind / shutter drive"
    node = registry.async_get_device_by_identifier(
        (DOMAIN, f"node:{NODE_BLIND}"), init_blinds.entry_id
    )
    assert node is not None
    assert device.via_device_id == node.id


async def test_blind_devices_survive_a_reload(
    hass: HomeAssistant, init_blinds: MockConfigEntry
) -> None:
    """The blind's device is part of the export, so a reload keeps it (its id, area and name) instead of pruning it."""
    registry = dr.async_get(hass)
    before = registry.async_get_device_by_identifier(
        (DOMAIN, UID_BLIND), init_blinds.entry_id
    )
    assert before is not None
    await hass.config_entries.async_reload(init_blinds.entry_id)
    await wait_for_link(hass, init_blinds)
    await settle(hass)
    after = registry.async_get_device_by_identifier(
        (DOMAIN, UID_BLIND), init_blinds.entry_id
    )
    assert after is not None
    assert after.id == before.id
    assert init_blinds.entry_id in after.config_entries


async def test_unknown_mode_is_a_shutter_until_the_device_answers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh,
    mesh: PropertyMesh,
    fast_sleep: list[float],
    fake_link: FakeProxyLink,
) -> None:
    """Without a `0x1104` answer the cover is a plain shutter; an unsolicited Status (or a later answer) upgrades it."""
    mesh.silent.add((BLIND, COVER_MODE_PROPERTY))
    with (
        pytest.MonkeyPatch.context() as mp
    ):  # an unanswered Get gives up after milliseconds
        mp.setattr("custom_components.junghome_ble.const.PROPERTY_READ_TIMEOUT", 0.01)
        await setup_entry(hass, mock_config_entry)
        await wait_for_link(hass, mock_config_entry)
        await settle(hass, 200)
        eid = entity_id(hass, "cover", UID_BLIND)
        entity = cover_entity(hass, eid)
        await wait_until(
            hass, lambda: mesh.gets.count((BLIND, COVER_MODE_PROPERTY)) == 3
        )  # all three attempts
        await wait_until(hass, lambda: not entity._mode_read_pending)  # ... timed out
    assert (
        not entity._mode_read_done
    )  # the mode is still unknown: the read stays open for the next link
    async_dispatcher_send(hass, SIGNAL_UPDATE.format(mock_config_entry.entry_id, BLIND))
    await settle(hass, 50)
    assert (
        mesh.gets.count((BLIND, COVER_MODE_PROPERTY)) == 3
    )  # ... but not repeated on this one
    state = hass.states.get(eid)
    assert state.attributes[ATTR_DEVICE_CLASS] == CoverDeviceClass.SHUTTER
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == POSITION_FEATURES
    assert state.attributes["operation_mode"] == "unknown"
    assert ATTR_CURRENT_TILT_POSITION not in state.attributes
    # the tilt services are refused meanwhile
    with pytest.raises(ServiceNotSupported):
        await call(hass, SERVICE_OPEN_COVER_TILT, eid)

    fake_link.inject(BLIND, 0xC090, vendor_status(0x05, COVER_MODE_PROPERTY, b"\x00"))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes[ATTR_DEVICE_CLASS] == CoverDeviceClass.BLIND
    assert (
        state.attributes[ATTR_SUPPORTED_FEATURES] == POSITION_FEATURES | SLAT_FEATURES
    )
    assert state.attributes["operation_mode"] == "blinds"
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 0

    # a Status without a value is dropped, as the app drops it: the mode stays
    fake_link.inject(BLIND, 0xC090, vendor_status(0x05, COVER_MODE_PROPERTY, b""))
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes["operation_mode"] == "blinds"
    # a mode the table does not know is a shutter again, and an unknown mode
    fake_link.inject(BLIND, 0xC090, vendor_status(0x05, COVER_MODE_PROPERTY, b"\x02"))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes[ATTR_DEVICE_CLASS] == CoverDeviceClass.SHUTTER
    assert state.attributes["operation_mode"] == "unknown"


async def test_mode_read_is_retried_after_a_lost_link(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh,
    mesh: PropertyMesh,
    fast_sleep: list[float],
    fake_link: FakeProxyLink,
) -> None:
    """A read cut short by a dropped link (no answer possible) is done again on the next link."""
    mesh.silent.add((SHUTTER, COVER_MODE_PROPERTY))
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "cover", UID_SHUTTER)
    assert mesh.gets.count((SHUTTER, COVER_MODE_PROPERTY)) == 1  # in flight, unanswered
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get(eid).state == STATE_UNAVAILABLE
    entity = cover_entity(hass, eid)
    assert not entity._mode_read_done

    mesh.silent.discard((SHUTTER, COVER_MODE_PROPERTY))
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    assert mesh.gets.count((SHUTTER, COVER_MODE_PROPERTY)) == 2
    assert entity._mode_read_done
    assert hass.states.get(eid).attributes["operation_mode"] == "shutter"


async def test_silent_mode_read_is_repeated_on_the_next_link(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh,
    mesh: PropertyMesh,
    fast_sleep: list[float],
    fake_link: FakeProxyLink,
) -> None:
    """A drive that stayed silent through all three attempts is asked again on the next link and answers this time."""
    mesh.silent.add((SHUTTER, COVER_MODE_PROPERTY))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.junghome_ble.const.PROPERTY_READ_TIMEOUT", 0.01)
        await setup_entry(hass, mock_config_entry)
        await wait_for_link(hass, mock_config_entry)
        await settle(hass, 200)
        eid = entity_id(hass, "cover", UID_SHUTTER)
        entity = cover_entity(hass, eid)
        await wait_until(
            hass, lambda: mesh.gets.count((SHUTTER, COVER_MODE_PROPERTY)) == 3
        )
        await wait_until(hass, lambda: not entity._mode_read_pending)
    assert not entity._mode_read_done
    assert hass.states.get(eid).attributes["operation_mode"] == "unknown"

    mesh.silent.discard((SHUTTER, COVER_MODE_PROPERTY))
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    await wait_until(hass, lambda: entity._mode_read_done)
    assert mesh.gets.count((SHUTTER, COVER_MODE_PROPERTY)) == 4
    assert hass.states.get(eid).attributes["operation_mode"] == "shutter"


# --------------------------------------------------------------------------- state from statuses


async def test_position_and_direction_from_level_status(
    hass: HomeAssistant, init_blinds: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Present level = position; target below / above present = opening / closing; the short form ends a move."""
    eid = entity_id(hass, "cover", UID_SHUTTER)
    hub = init_blinds.runtime_data

    fake_link.inject(SHUTTER, 0xC0A0, level_status(LEVEL_CLOSED))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_CURRENT_POSITION] == 0
    assert (hub.states[SHUTTER].level, hub.states[SHUTTER].target_level) == (
        LEVEL_CLOSED,
        LEVEL_CLOSED,
    )

    # opening: present 100 % closed, heading to 0 %
    fake_link.inject(SHUTTER, 0xC0A0, level_status(LEVEL_CLOSED, LEVEL_OPEN, 0x54))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == CoverState.OPENING
    assert state.attributes[ATTR_CURRENT_POSITION] == 0  # still the present level
    assert hub.states[SHUTTER].target_level == LEVEL_OPEN

    fake_link.inject(SHUTTER, 0xC0A0, level_status(-16384, LEVEL_OPEN, 0x22))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == CoverState.OPENING
    assert state.attributes[ATTR_CURRENT_POSITION] == 75

    fake_link.inject(SHUTTER, 0xC0A0, level_status(LEVEL_OPEN))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 100

    # closing: heading to 60 % closed from open
    fake_link.inject(SHUTTER, 0xC0A0, level_status(LEVEL_OPEN, 6553, 0x30))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == CoverState.CLOSING
    fake_link.inject(SHUTTER, 0xC0A0, level_status(6553, 6553, 0))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 40

    # a status from the slat element never moves the position; a short one is ignored
    fake_link.inject(SHUTTER_SLAT, 0xC0A1, level_status(LEVEL_CLOSED))
    fake_link.inject(SHUTTER, 0xC0A0, encode_opcode(M.GEN_LEVEL_STATUS) + b"\x01")
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_CURRENT_POSITION] == 40
    assert hub.states[SHUTTER_SLAT].level == LEVEL_CLOSED


async def test_tilt_follows_the_slat_element(
    hass: HomeAssistant, init_blinds: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "cover", UID_BLIND)
    fake_link.inject(BLIND_SLAT, 0xC091, level_status(LEVEL_OPEN))
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes[ATTR_CURRENT_TILT_POSITION] == 100
    fake_link.inject(BLIND_SLAT, 0xC091, level_status(-16384, 16384, 5))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 75
    assert state.state == CoverState.OPEN  # slats moving is not the blind moving


async def test_update_entity_asks_the_position_and_the_slats(
    hass: HomeAssistant,
    init_blinds: MockConfigEntry,
    levels: LevelMesh,
) -> None:
    """Review-4 H4-10 (unverified on air): `homeassistant.update_entity` sends a Generic Level Get to the position
    element and, on a blind with slats, to the slat element; the cover shows the answers."""
    eid = entity_id(hass, "cover", UID_BLIND)
    levels.levels[BLIND], levels.levels[BLIND_SLAT] = (
        LEVEL_OPEN,
        LEVEL_OPEN,
    )  # moved on the device
    levels.gets.clear()
    await hass.services.async_call(
        "homeassistant", "update_entity", {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert levels.gets == [BLIND, BLIND_SLAT]
    state = hass.states.get(eid)
    assert state.attributes[ATTR_CURRENT_POSITION] == 100
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 100


async def test_nothing_known_before_the_first_status(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh,
    fast_sleep: list[float],
) -> None:
    levels.silent.update(levels.levels)
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    state = hass.states.get(entity_id(hass, "cover", UID_BLIND))
    assert state.state == STATE_UNKNOWN
    assert ATTR_CURRENT_POSITION not in state.attributes
    assert ATTR_CURRENT_TILT_POSITION not in state.attributes
    assert state.attributes["is_closed"] is None
    assert (
        state.attributes[ATTR_SUPPORTED_FEATURES] == POSITION_FEATURES | SLAT_FEATURES
    )


# --------------------------------------------------------------------------- commands


async def test_open_close_stop(
    hass: HomeAssistant,
    init_blinds: MockConfigEntry,
    fake_link: FakeProxyLink,
    levels: LevelMesh,
) -> None:
    """Open / close / stop are the gateway's Generic Move Sets; a stop is followed by a Generic Level Get."""
    eid = entity_id(hass, "cover", UID_SHUTTER)
    levels.levels[SHUTTER] = 0  # half open: neither end skips its arrow
    fake_link.inject(SHUTTER, 0xC090, level_status(0))
    await hass.async_block_till_done()
    fake_link.sent.clear()
    levels.gets.clear()

    await call(hass, SERVICE_OPEN_COVER, eid)
    src, dst, pdu = last_sent(fake_link)
    assert (src, dst) == (OUR_ADDRESS, SHUTTER)
    assert pdu == move_set(-32768, tid=pdu[4])
    assert pdu == bytes(
        [0x82, 0x0B, 0x00, 0x80, pdu[4], 0x87, 0x00]
    )  # 0x8000 up, 70 s, no delay
    assert len(fake_link.sent) == 1

    await call(hass, SERVICE_CLOSE_COVER, eid)
    src, dst, pdu = last_sent(fake_link)
    assert (src, dst) == (OUR_ADDRESS, SHUTTER)
    assert pdu == move_set(32767, tid=pdu[4])
    assert pdu[:4] == bytes([0x82, 0x0B, 0xFF, 0x7F])  # 0x7FFF down
    assert len(fake_link.sent) == 2

    # nothing is written optimistically: the state is what the element last reported (answering the Move Sets)
    assert hass.states.get(eid).attributes[ATTR_CURRENT_POSITION] == 50
    assert hass.states.get(eid).state == CoverState.OPEN

    levels.levels[SHUTTER] = 16384  # the blind stopped three quarters down
    await call(hass, SERVICE_STOP_COVER, eid)
    await settle(hass)
    assert [pdu for _, _, pdu in fake_link.sent[2:]] == [
        move_set(0, tid=fake_link.sent[2][2][4]),
        M.generic_level_get(),
    ]
    assert fake_link.sent[3][1] == SHUTTER
    assert levels.gets == [SHUTTER]
    assert hass.states.get(eid).attributes[ATTR_CURRENT_POSITION] == 25


async def test_a_blind_that_does_not_answer_its_movements_still_runs(
    hass: HomeAssistant,
    init_blinds: MockConfigEntry,
    fake_link: FakeProxyLink,
    levels: LevelMesh,
) -> None:
    """Open / close / stop are not waited for: whether a blind publishes its level when it starts to run or only when
    it stops is unknown (`control-and-state.md` §5), and the app never sends a Move Set. A blind silent on them is
    sent each once, the actions succeed, the stop's read-back still goes out, and the blind stays available."""
    hub = init_blinds.runtime_data
    eid = entity_id(hass, "cover", UID_SHUTTER)
    await settle(hass)
    fake_link.inject(SHUTTER, 0xC090, level_status(0))  # half open: no end to skip
    await hass.async_block_till_done()
    fake_link.sets_silent.add(SHUTTER)
    fake_link.sent.clear()
    levels.gets.clear()
    for service in (SERVICE_OPEN_COVER, SERVICE_CLOSE_COVER, SERVICE_STOP_COVER):
        await call(hass, service, eid)
    await settle(hass)
    assert [pdu[:4] for _, _, pdu in fake_link.sent[:3]] == [
        bytes([0x82, 0x0B, 0x00, 0x80]),
        bytes([0x82, 0x0B, 0xFF, 0x7F]),
        bytes([0x82, 0x0B, 0x00, 0x00]),
    ]  # one of each: no retries
    assert levels.gets == [SHUTTER]  # where it stopped
    assert not hub.unreachable
    assert not hub._unanswered
    assert hass.states.get(eid).state != STATE_UNAVAILABLE


async def test_delta_set_is_the_apps_alternative(
    hass: HomeAssistant, init_blinds: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """`JungHomeHub.delta_level` sends what the JUNG HOME app sends for up / down / stop (not used by the cover yet)."""
    hub = init_blinds.runtime_data
    fake_link.sent.clear()
    for delta, raw in (
        (-1, b"\xff\xff\xff\xff"),
        (1, b"\x01\x00\x00\x00"),
        (0, bytes(4)),
    ):
        await hub.delta_level(BLIND, delta)
        src, dst, pdu = last_sent(fake_link)
        assert (src, dst) == (OUR_ADDRESS, BLIND)
        assert pdu == M.generic_delta_set(delta, tid=pdu[6])
        assert (
            pdu[:6] == bytes([0x82, 0x09]) + raw
        )  # acknowledged, s32 LE, no transition
    assert len(fake_link.sent) == 3


async def test_set_position(
    hass: HomeAssistant, init_blinds: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A position is one acknowledged Generic Level Set with the closedness level, no transition."""
    eid = entity_id(hass, "cover", UID_AWNING)
    fake_link.sent.clear()
    for position, level in (
        (100, LEVEL_OPEN),
        (0, LEVEL_CLOSED),
        (25, 16383),
        (60, -6554),
    ):
        await call(hass, SERVICE_SET_COVER_POSITION, eid, **{ATTR_POSITION: position})
        src, dst, pdu = last_sent(fake_link)
        assert (src, dst) == (OUR_ADDRESS, AWNING)
        assert pdu == M.generic_level_set(level, tid=pdu[4])
        assert len(pdu) == 5  # opcode, level, tid: no transition / delay
    assert bytes(fake_link.sent[1][2][2:4]) == b"\xff\x7f"
    assert bytes(fake_link.sent[0][2][2:4]) == b"\x00\x80"
    assert len(fake_link.sent) == 4  # each answered at its first attempt
    # the state is what the awning reported answering the last Set; it maps like everything else (the app labels
    # it Open / Closed the same way)
    assert hass.states.get(eid).state == CoverState.OPEN
    assert hass.states.get(eid).attributes[ATTR_CURRENT_POSITION] == 60


async def test_tilt_commands_go_to_the_slat_element(
    hass: HomeAssistant, init_blinds: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "cover", UID_BLIND)
    fake_link.sent.clear()

    await call(hass, SERVICE_OPEN_COVER_TILT, eid)
    src, dst, pdu = last_sent(fake_link)
    assert (src, dst) == (OUR_ADDRESS, BLIND_SLAT)
    assert pdu == M.generic_level_set(LEVEL_OPEN, tid=pdu[4])

    await call(hass, SERVICE_CLOSE_COVER_TILT, eid)
    src, dst, pdu = last_sent(fake_link)
    assert (src, dst) == (OUR_ADDRESS, BLIND_SLAT)
    assert pdu == M.generic_level_set(LEVEL_CLOSED, tid=pdu[4])

    await call(hass, SERVICE_SET_COVER_TILT_POSITION, eid, **{ATTR_TILT_POSITION: 75})
    src, dst, pdu = last_sent(fake_link)
    assert (src, dst) == (OUR_ADDRESS, BLIND_SLAT)
    assert pdu == M.generic_level_set(-16384, tid=pdu[4])
    assert len(fake_link.sent) == 3
    assert (
        hass.states.get(eid).attributes[ATTR_CURRENT_TILT_POSITION] == 75
    )  # what the slat element reported answering the last Set

    # a shutter (no slats in its mode) and the awning (no slat element) refuse the tilt services
    for uid in (UID_SHUTTER, UID_AWNING):
        with pytest.raises(ServiceNotSupported):
            await call(
                hass,
                SERVICE_SET_COVER_TILT_POSITION,
                entity_id(hass, "cover", uid),
                **{ATTR_TILT_POSITION: 50},
            )
    assert len(fake_link.sent) == 3
    # ... and the entity itself sends nothing for slats it does not have
    awning = cover_entity(hass, entity_id(hass, "cover", UID_AWNING))
    await awning.async_open_cover_tilt()
    await awning.async_stop_cover_tilt()
    assert len(fake_link.sent) == 3


async def test_stop_tilt_stops_the_slat_element(
    hass: HomeAssistant,
    init_blinds: MockConfigEntry,
    fake_link: FakeProxyLink,
    levels: LevelMesh,
) -> None:
    """The position's stop, sent to the slat element (no JUNG client does this: SIG Generic Level semantics), then
    the slats are asked where they stopped."""
    eid = entity_id(hass, "cover", UID_BLIND)
    fake_link.sent.clear()
    levels.gets.clear()
    levels.levels[BLIND_SLAT] = 0  # the slats stopped half-way
    await call(hass, SERVICE_STOP_COVER_TILT, eid)
    await settle(hass)
    assert [(dst, pdu) for _, dst, pdu in fake_link.sent] == [
        (BLIND_SLAT, move_set(0, tid=fake_link.sent[0][2][4])),
        (BLIND_SLAT, M.generic_level_get()),
    ]
    assert levels.gets == [BLIND_SLAT]
    assert hass.states.get(eid).attributes[ATTR_CURRENT_TILT_POSITION] == 50


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
    init_blinds: MockConfigEntry,
    fake_link: FakeProxyLink,
    error: Exception,
) -> None:
    fake_link.write_error = error
    eid = entity_id(hass, "cover", UID_BLIND)
    for service, data in (
        (SERVICE_OPEN_COVER, {}),
        (SERVICE_CLOSE_COVER, {}),
        (SERVICE_STOP_COVER, {}),
        (SERVICE_SET_COVER_POSITION, {ATTR_POSITION: 10}),
        (SERVICE_OPEN_COVER_TILT, {}),
        (SERVICE_CLOSE_COVER_TILT, {}),
        (SERVICE_SET_COVER_TILT_POSITION, {ATTR_TILT_POSITION: 10}),
        (SERVICE_STOP_COVER_TILT, {}),
    ):
        with pytest.raises(HomeAssistantError) as exc:
            await call(hass, service, eid, **data)
        assert exc.value.translation_key == "send_failed"


async def test_stop_refresh_is_best_effort(
    hass: HomeAssistant,
    init_blinds: MockConfigEntry,
    fake_link: FakeProxyLink,
    levels: LevelMesh,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Get after a stop neither blocks the service call nor fails it: silence and a lost link are logged."""
    eid = entity_id(hass, "cover", UID_AWNING)
    hub = init_blinds.runtime_data
    caplog.set_level(logging.DEBUG, logger="custom_components.junghome_ble")

    original = hub.proxy.request

    def failing(error: Exception) -> Any:
        """`request` for the Level Get after the stop fails with `error`; the stop itself is answered as usual."""

        async def request(
            dst: int, access_pdu: bytes, *args: Any, **kwargs: Any
        ) -> Any:
            if access_pdu == M.generic_level_get():
                raise error
            return await original(dst, access_pdu, *args, **kwargs)

        return request

    for request, text in (
        (
            failing(TimeoutError("no response from 0700")),
            "0700 did not answer its Generic Level Get after a stop",
        ),
        (
            failing(ConnectionError("proxy disconnected")),
            "Generic Level Get to 0700 not sent: proxy disconnected",
        ),
    ):
        fake_link.sent.clear()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(hub.proxy, "request", request)
            await call(
                hass, SERVICE_STOP_COVER, eid
            )  # the stop itself went out, the call returned
            await settle(hass)
        assert [pdu[:4] for _, _, pdu in fake_link.sent] == [bytes([0x82, 0x0B, 0, 0])]
        assert text in caplog.text


# --------------------------------------------------------------------------- availability


async def test_availability_follows_the_link(
    hass: HomeAssistant,
    init_blinds: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    eid = entity_id(hass, "cover", UID_BLIND)
    assert hass.states.get(eid).state == CoverState.OPEN

    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get(eid).state == STATE_UNAVAILABLE
    fake_link.sent.clear()
    await call(
        hass, SERVICE_OPEN_COVER, eid
    )  # HA skips unavailable entities: nothing goes out
    assert fake_link.sent == []

    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, init_blinds)
    await settle(hass)
    state = hass.states.get(eid)
    assert (
        state.state == CoverState.OPEN
    )  # the cached state is kept across the reconnect
    assert state.attributes[ATTR_CURRENT_POSITION] == 50


# --------------------------------------------------------------------------- All blinds (the app's central function)


def group_sends(link: FakeProxyLink, group: int) -> list[tuple[int, int]]:
    """(opcode, first s16 of the parameters) of every message the hub sent to `group`."""
    out = []
    for _, dst, access in link.sent:
        if dst == group:
            op, _, params = decode_opcode(access)
            out.append((op, int.from_bytes(params[:2], "little", signed=True)))
    return out


async def test_all_blinds_moves_every_blind_with_one_message(
    hass: HomeAssistant, init_blinds: MockConfigEntry, fake_link: FakeProxyLink, levels: LevelMesh
) -> None:  # fmt: skip
    hub = init_blinds.runtime_data
    eid = entity_id(hass, "cover", f"{hub.cdb.mesh_uuid.lower()}-central-fef6")
    state = hass.states.get(eid)
    # half open, open and closed: 50 % on average; the two slat elements closed and open
    assert state.attributes[ATTR_CURRENT_POSITION] == 50
    assert state.attributes[ATTR_CURRENT_TILT_POSITION] == 50
    assert state.state == CoverState.OPEN
    assert (
        state.attributes[ATTR_SUPPORTED_FEATURES] == POSITION_FEATURES | TILT_FEATURES
    )
    assert state.attributes["members"] == [
        "Kitchen blind",
        "Living room shutter",
        "Blinds PP2 actuator 0700",
    ]

    # the app's messages, unacknowledged: 0 % / 100 % closed, a position, Delta 0 for stop, the slats' group
    await call(hass, SERVICE_OPEN_COVER, eid)
    await call(hass, SERVICE_CLOSE_COVER, eid)
    await call(hass, SERVICE_SET_COVER_POSITION, eid, **{ATTR_POSITION: 75})
    gets = len(levels.gets)
    await call(hass, SERVICE_STOP_COVER, eid)
    await settle(hass)
    assert group_sends(fake_link, 0xFEF6) == [
        (M.GEN_LEVEL_SET_UNACK, LEVEL_OPEN),
        (M.GEN_LEVEL_SET_UNACK, LEVEL_CLOSED),
        (M.GEN_LEVEL_SET_UNACK, -16384),  # 75 % open = 25 % closed
        (M.GEN_DELTA_SET_UNACK, 0),
    ]
    # after the stop every blind (and slat) is asked where it ended up
    assert sorted(levels.gets[gets:]) == [
        BLIND,
        BLIND_SLAT,
        SHUTTER,
        SHUTTER_SLAT,
        AWNING,
    ]
    await call(hass, SERVICE_OPEN_COVER_TILT, eid)
    await call(hass, SERVICE_CLOSE_COVER_TILT, eid)
    await call(hass, SERVICE_SET_COVER_TILT_POSITION, eid, **{ATTR_TILT_POSITION: 0})
    assert group_sends(fake_link, 0xFEF7) == [
        (M.GEN_LEVEL_SET_UNACK, LEVEL_OPEN),
        (M.GEN_LEVEL_SET_UNACK, LEVEL_CLOSED),
        (M.GEN_LEVEL_SET_UNACK, LEVEL_CLOSED),
    ]

    # closed once every blind reports closed
    for addr in (BLIND, SHUTTER):
        fake_link.inject(addr, 0xC090, level_status(LEVEL_CLOSED))
    await settle(hass)
    assert hass.states.get(eid).state == CoverState.CLOSED


async def test_room_blinds_move_each_blind_and_stop_with_one_message(
    hass: HomeAssistant, init_blinds: MockConfigEntry, fake_link: FakeProxyLink, levels: LevelMesh
) -> None:  # fmt: skip
    """The app's area sheet: positions and slats per blind, the stop as one Delta Set 0 to the room (`OpenClose.Group`)."""
    hub = init_blinds.runtime_data
    eid = entity_id(hass, "cover", f"{hub.cdb.mesh_uuid.lower()}-room-c011-blinds")
    state = hass.states.get(eid)
    assert state.name.endswith("All blinds in Kitchen")
    assert state.attributes["members"] == ["Kitchen blind"]
    assert state.attributes[ATTR_CURRENT_POSITION] == 50  # the one blind, half open
    assert (
        state.attributes[ATTR_SUPPORTED_FEATURES] == POSITION_FEATURES | TILT_FEATURES
    )
    fake_link.sent.clear()
    await call(hass, SERVICE_OPEN_COVER, eid)
    await call(hass, SERVICE_CLOSE_COVER, eid)
    await call(hass, SERVICE_SET_COVER_POSITION, eid, **{ATTR_POSITION: 75})
    await call(hass, SERVICE_SET_COVER_TILT_POSITION, eid, **{ATTR_TILT_POSITION: 0})
    assert group_sends(fake_link, BLIND) == [
        (M.GEN_LEVEL_SET_UNACK, LEVEL_OPEN),
        (M.GEN_LEVEL_SET_UNACK, LEVEL_CLOSED),
        (M.GEN_LEVEL_SET_UNACK, -16384),
    ]
    assert group_sends(fake_link, BLIND_SLAT) == [(M.GEN_LEVEL_SET_UNACK, LEVEL_CLOSED)]
    assert group_sends(fake_link, 0xFEF6) == group_sends(fake_link, 0xFEF7) == []
    gets = len(levels.gets)
    await call(hass, SERVICE_STOP_COVER, eid)
    await settle(hass)
    assert group_sends(fake_link, 0xC011) == [(M.GEN_DELTA_SET_UNACK, 0)]
    assert sorted(levels.gets[gets:]) == [BLIND, BLIND_SLAT]


async def test_all_blinds_without_news_or_slats(
    hass: HomeAssistant, init_blinds: MockConfigEntry
) -> None:
    hub = init_blinds.runtime_data
    entity = JungHomeAllBlinds(hub, [hub.devices.by_address[AWNING]], [])
    assert entity.supported_features == POSITION_FEATURES  # no slats: no tilt
    hub.states.clear()
    assert entity.current_cover_position is None
    assert entity.current_cover_tilt_position is None
    assert entity.is_closed is None


# --------------------------------------------------------------------------- lock function, wind alarm, reference run

PID_LOCK, PID_REFERENCE_RUN, PID_RUNNING_TIME = 0x0009, 0x110D, 0x1102
UNLOCKED = bytes.fromhex("00010000")  # what the test mesh reports by default
LOCKED = bytes.fromhex("02010000")  # the app's lock, no time limit
WIND_ALARM = bytes.fromhex("01ff00000000")  # the app's wind alarm
UID_WIND_ALARM = f"{UID_BLIND}-wind_alarm"
UID_REFERENCE_RUN = f"{UID_BLIND}-reference_run_active"
UID_LOCK_FUNCTION = f"{UID_BLIND}-lock_function"
UID_LOCK_SWITCH = f"{UID_BLIND}-enforced_output"
UID_LOCK_LIMIT = f"{UID_BLIND}-lock_time_limit"


def enable(hass: HomeAssistant, domain: str, unique_id: str) -> None:
    """Register an entity the integration keeps disabled by default as enabled by the user."""
    er.async_get(hass).async_get_or_create(domain, DOMAIN, unique_id, disabled_by=None)


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)


def lock_sends(link: FakeProxyLink, addr: int = BLIND) -> list[bytes]:
    """The values of every Admin Set of the lock function the hub sent to `addr`."""
    out = []
    for _, dst, access in link.sent:
        op, cid, params = decode_opcode(access)
        if (
            dst == addr
            and cid == M.JUNG_CID
            and op == 0x03
            and params[:2] == b"\x09\x00"
        ):
            out.append(bytes(params[3:]))
    return out


async def test_blind_lock_entities(
    hass: HomeAssistant, init_blinds: MockConfigEntry, mesh: PropertyMesh
) -> None:
    """Per blind: the *Wind alarm* safety sensor and the *Reference run* diagnostic, read once and enabled; the
    *Lock function* select that writes, disabled until tried on a real blind."""
    registry = er.async_get(hass)
    for addr, uid in ((BLIND, UID_BLIND), (SHUTTER, UID_SHUTTER), (AWNING, UID_AWNING)):
        wind = registry.async_get(entity_id(hass, "binary_sensor", f"{uid}-wind_alarm"))
        assert wind is not None
        assert wind.disabled_by is None
        assert wind.entity_category is None
        run = registry.async_get(
            entity_id(hass, "binary_sensor", f"{uid}-reference_run_active")
        )
        assert run is not None
        assert run.disabled_by is None
        assert run.entity_category is EntityCategory.DIAGNOSTIC
        select = registry.async_get(entity_id(hass, "select", f"{uid}-lock_function"))
        assert select is not None
        assert select.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert select.entity_category is EntityCategory.CONFIG
        assert wind.device_id == select.device_id == run.device_id
        assert mesh.gets.count((addr, PID_LOCK)) == 1  # the wind alarm's read
        assert mesh.gets.count((addr, PID_REFERENCE_RUN)) == 1
    state = hass.states.get(entity_id(hass, "binary_sensor", UID_WIND_ALARM))
    assert state.state == STATE_OFF
    assert state.attributes["device_class"] == BinarySensorDeviceClass.SAFETY
    assert state.attributes["property_id"] == "0x0009"
    state = hass.states.get(entity_id(hass, "binary_sensor", UID_REFERENCE_RUN))
    assert state.state == STATE_OFF
    assert state.attributes["device_class"] == BinarySensorDeviceClass.RUNNING
    device = dr.async_get(hass).async_get(wind.device_id)
    assert device is not None
    assert device.name == "Blinds PP2 actuator 0700"  # the blind's own device


async def test_a_locked_blind_refuses_commands(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh, mesh: PropertyMesh, fast_sleep: list[float], fake_link: FakeProxyLink,
) -> None:  # fmt: skip
    """A blind held by a wind alarm or a lock ignores commands, as the app knows (it disables its controls): the
    cover says so instead of sending, after reading the lock again (a timed one ends silently)."""
    mesh.values[BLIND, PID_LOCK] = WIND_ALARM
    mesh.values[SHUTTER, PID_LOCK] = LOCKED
    await start(hass, mock_config_entry)
    blind = entity_id(hass, "cover", UID_BLIND)
    shutter = entity_id(hass, "cover", UID_SHUTTER)
    assert (
        hass.states.get(entity_id(hass, "binary_sensor", UID_WIND_ALARM)).state
        == STATE_ON
    )
    assert (
        hass.states.get(
            entity_id(hass, "binary_sensor", f"{UID_SHUTTER}-wind_alarm")
        ).state
        == STATE_OFF
    )  # a plain lock is no wind alarm

    fake_link.sent.clear()
    mesh.gets.clear()
    services = (
        (SERVICE_OPEN_COVER, {}),
        (SERVICE_CLOSE_COVER, {}),
        (SERVICE_STOP_COVER, {}),
        (SERVICE_SET_COVER_POSITION, {ATTR_POSITION: 10}),
        (SERVICE_OPEN_COVER_TILT, {}),
        (SERVICE_CLOSE_COVER_TILT, {}),
        (SERVICE_SET_COVER_TILT_POSITION, {ATTR_TILT_POSITION: 10}),
        (SERVICE_STOP_COVER_TILT, {}),
    )
    for service, data in services:
        with pytest.raises(HomeAssistantError) as exc:
            await call(hass, service, blind, **data)
        assert exc.value.translation_key == "cover_wind_alarm"
        assert exc.value.translation_placeholders == {"entity": blind}
    assert mesh.gets == [(BLIND, PID_LOCK)] * len(services)  # asked each time ...
    assert all(
        dst == BLIND for _, dst, _ in fake_link.sent
    )  # ... and nothing but asked
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, SERVICE_OPEN_COVER, shutter)
    assert exc.value.translation_key == "cover_locked"

    # the shutter's lock ended on its own: the fresh read says so and the command goes out (it is open: down)
    mesh.values[SHUTTER, PID_LOCK] = UNLOCKED
    fake_link.sent.clear()
    await call(hass, SERVICE_CLOSE_COVER, shutter)
    assert last_sent(fake_link)[1:] == (
        SHUTTER,
        move_set(32767, tid=last_sent(fake_link)[2][4]),
    )
    # unlocked in the cache: no read before the next command
    mesh.gets.clear()
    await call(hass, SERVICE_CLOSE_COVER, shutter)
    assert mesh.gets == []

    # a lock state the blind reports malformed does not block it, nor does one never read
    mesh.values[BLIND, PID_LOCK] = b"\x02"
    await call(hass, SERVICE_OPEN_COVER, blind)
    assert last_sent(fake_link)[1] == BLIND
    hub = mock_config_entry.runtime_data
    del hub.states[AWNING].properties[PID_LOCK]
    await call(hass, SERVICE_OPEN_COVER, entity_id(hass, "cover", UID_AWNING))
    assert last_sent(fake_link)[1] == AWNING


async def test_lock_function_select(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh, mesh: PropertyMesh, fast_sleep: list[float], fake_link: FakeProxyLink,
) -> None:  # fmt: skip
    """The blind page's lock functions: lock, lock-out protection (both with the load's time limit), wind alarm and
    unlock, as the app sends them; a timed one is read back when it should have ended."""
    for domain, uid in (
        ("select", UID_LOCK_FUNCTION),
        ("switch", UID_LOCK_SWITCH),
        ("number", UID_LOCK_LIMIT),
    ):
        enable(hass, domain, uid)
    await start(hass, mock_config_entry)
    select = entity_id(hass, "select", UID_LOCK_FUNCTION)
    wind = entity_id(hass, "binary_sensor", UID_WIND_ALARM)
    state = hass.states.get(select)
    assert state.state == "unlocked"
    assert state.attributes["options"] == [
        "unlocked",
        "keep_state",
        "lockout_protection",
        "wind_alarm",
    ]
    assert mesh.gets.count((BLIND, PID_LOCK)) == 1  # three entities, one read

    async def pick(option: str) -> None:
        await hass.services.async_call(
            SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: select, "option": option}, blocking=True
        )  # fmt: skip

    await pick("wind_alarm")
    assert lock_sends(fake_link)[-1] == WIND_ALARM  # `01 FF 00 00 00 00`
    assert hass.states.get(select).state == "wind_alarm"
    assert hass.states.get(wind).state == STATE_ON  # the same cached value
    await pick("unlocked")  # command 0 with the fields read, as the lock switch's off
    assert lock_sends(fake_link)[-1] == bytes.fromhex("00ff00000000")
    assert hass.states.get(select).state == "unlocked"
    assert hass.states.get(wind).state == STATE_OFF

    await hass.services.async_call(
        NUMBER_DOMAIN, SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id(hass, "number", UID_LOCK_LIMIT), "value": 120}, blocking=True,
    )  # fmt: skip
    await pick("keep_state")
    assert lock_sends(fake_link)[-1] == bytes.fromhex("02017800")  # 120 s
    assert hass.states.get(select).state == "keep_state"
    await pick("lockout_protection")
    assert lock_sends(fake_link)[-1] == bytes.fromhex("02fe7800")  # `02 FE <t>`
    assert hass.states.get(select).state == "lockout_protection"
    assert hass.states.get(entity_id(hass, "switch", UID_LOCK_SWITCH)).state == STATE_ON

    # the blind ends the lock-out protection itself: select, switch and wind alarm want it read back at the same
    # moment and share one Get
    mesh.values[BLIND, PID_LOCK] = UNLOCKED
    gets = len(mesh.gets)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=120 + LOCK_EXPIRY_MARGIN + 1)
    )
    await settle(hass)
    assert mesh.gets[gets:] == [(BLIND, PID_LOCK)]
    assert hass.states.get(select).state == "unlocked"

    # a lock a rocker set with a value of its own has no option; an unread one is unknown
    fake_link.inject(
        BLIND, 0xC090, vendor_status(0x05, PID_LOCK, bytes.fromhex("0101000001"))
    )
    await settle(hass)
    assert hass.states.get(select).state == STATE_UNKNOWN
    fake_link.inject(BLIND, 0xC090, vendor_status(0x05, PID_LOCK, b"\x02"))  # malformed
    await settle(hass)
    assert hass.states.get(select).state == STATE_UNKNOWN
    assert hass.states.get(wind).state == STATE_UNKNOWN


async def test_reference_run_is_followed_until_it_ends(
    hass: HomeAssistant, init_blinds: MockConfigEntry, mesh: PropertyMesh, fake_link: FakeProxyLink,
) -> None:  # fmt: skip
    """A run is read back when it should be over (the running time + 10 s, the longest the app allows while the
    running time is unknown), and again while it goes on; a new running time makes the app check for a run."""
    eid = entity_id(hass, "binary_sensor", UID_REFERENCE_RUN)
    assert hass.states.get(eid).state == STATE_OFF

    # the reference-run button's Set is answered with the running state
    mesh.values[BLIND, PID_REFERENCE_RUN] = b"\x01"
    fake_link.inject(
        BLIND, OUR_ADDRESS, vendor_status(0x05, PID_REFERENCE_RUN, b"\x01")
    )
    await settle(hass)
    assert hass.states.get(eid).state == STATE_ON
    gets = len(mesh.gets)
    now = dt_util.utcnow()
    async_fire_time_changed(hass, now + timedelta(seconds=REFERENCE_RUN_LONGEST))
    await settle(hass)
    assert len(mesh.gets) == gets  # not yet: the running time is not known
    now += timedelta(seconds=REFERENCE_RUN_LONGEST + REFERENCE_RUN_MARGIN + 1)
    async_fire_time_changed(hass, now)
    await settle(hass)
    assert mesh.gets[gets:] == [
        (BLIND, PID_REFERENCE_RUN)
    ]  # still running: asked again later
    assert hass.states.get(eid).state == STATE_ON

    # the running time becomes known (30 s): the next read-back follows it
    fake_link.inject(BLIND, 0xC090, vendor_status(0x05, PID_RUNNING_TIME, b"\x1e\x00"))
    await settle(hass)
    assert mesh.gets[gets:] == [(BLIND, PID_REFERENCE_RUN)]  # learning it is no change
    mesh.values[BLIND, PID_REFERENCE_RUN] = b"\x00"
    now += timedelta(seconds=REFERENCE_RUN_LONGEST + REFERENCE_RUN_MARGIN + 1)
    async_fire_time_changed(hass, now)
    await settle(hass)
    assert mesh.gets[gets:] == [(BLIND, PID_REFERENCE_RUN)] * 2
    assert hass.states.get(eid).state == STATE_OFF

    # a changed running time (or inverse operation) may start a run: read at once, then after 45 + 10 s
    mesh.values[BLIND, PID_REFERENCE_RUN] = b"\x01"
    fake_link.inject(BLIND, 0xC090, vendor_status(0x05, PID_RUNNING_TIME, b"\x2d\x00"))
    await settle(hass)
    assert mesh.gets[gets:] == [(BLIND, PID_REFERENCE_RUN)] * 3
    assert hass.states.get(eid).state == STATE_ON
    mesh.values[BLIND, PID_REFERENCE_RUN] = b"\x00"
    async_fire_time_changed(
        hass, now + timedelta(seconds=45 + REFERENCE_RUN_MARGIN + 1)
    )
    await settle(hass)
    assert mesh.gets[gets:] == [(BLIND, PID_REFERENCE_RUN)] * 4
    assert hass.states.get(eid).state == STATE_OFF

    # a run pending at unload is not read back afterwards
    fake_link.inject(
        BLIND, OUR_ADDRESS, vendor_status(0x05, PID_REFERENCE_RUN, b"\x01")
    )
    await settle(hass)
    assert await hass.config_entries.async_unload(init_blinds.entry_id)
    async_fire_time_changed(hass, now + timedelta(hours=1))
    await settle(hass)
    assert mesh.gets[gets:] == [(BLIND, PID_REFERENCE_RUN)] * 4


async def test_blind_sensors_unknown_until_read(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh, mesh: PropertyMesh, fast_sleep: list[float],
) -> None:  # fmt: skip
    mesh.silent |= {(BLIND, PID_LOCK), (BLIND, PID_REFERENCE_RUN)}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.junghome_ble.const.PROPERTY_READ_TIMEOUT", 0.01)
        await start(hass, mock_config_entry)
        await wait_until(hass, lambda: mesh.gets.count((BLIND, PID_REFERENCE_RUN)) == 2)
    assert (
        hass.states.get(entity_id(hass, "binary_sensor", UID_WIND_ALARM)).state
        == STATE_UNKNOWN
    )
    assert (
        hass.states.get(entity_id(hass, "binary_sensor", UID_REFERENCE_RUN)).state
        == STATE_UNKNOWN
    )


# --------------------------------------------------------------------------- the app's rules (review-4 F4-16)


async def test_open_and_close_skip_the_end_positions(
    hass: HomeAssistant,
    init_blinds: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The app skips its up arrow at 0 % and its down arrow at 100 % (`BlindsViewModel.blindsOpening` / `Closing`):
    nothing at all goes out then; the other direction does."""
    shutter = entity_id(hass, "cover", UID_SHUTTER)  # fully open
    awning = entity_id(hass, "cover", UID_AWNING)  # fully closed
    fake_link.sent.clear()
    await call(hass, SERVICE_OPEN_COVER, shutter)
    await call(hass, SERVICE_CLOSE_COVER, awning)
    assert fake_link.sent == []
    await call(hass, SERVICE_CLOSE_COVER, shutter)
    await call(hass, SERVICE_OPEN_COVER, awning)
    assert [(dst, pdu[:4]) for _, dst, pdu in fake_link.sent] == [
        (SHUTTER, bytes([0x82, 0x0B, 0xFF, 0x7F])),
        (AWNING, bytes([0x82, 0x0B, 0x00, 0x80])),
    ]
    # a position not known (yet) skips nothing
    hub = init_blinds.runtime_data
    hub.states[SHUTTER].level = None
    fake_link.sent.clear()
    await call(hass, SERVICE_OPEN_COVER, shutter)
    assert [dst for _, dst, _ in fake_link.sent] == [SHUTTER]


async def test_slats_are_refused_while_the_blind_is_fully_open(
    hass: HomeAssistant,
    init_blinds: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The app's slat slider is disabled while the blind is at 0 % (`GenericLevelCapable.q0()`); stopping the slats
    is not a slider move and stays allowed."""
    eid = entity_id(hass, "cover", UID_BLIND)
    fake_link.inject(BLIND, 0xC090, level_status(LEVEL_OPEN))
    await hass.async_block_till_done()
    fake_link.sent.clear()
    for service, data in (
        (SERVICE_OPEN_COVER_TILT, {}),
        (SERVICE_CLOSE_COVER_TILT, {}),
        (SERVICE_SET_COVER_TILT_POSITION, {ATTR_TILT_POSITION: 30}),
    ):
        with pytest.raises(ServiceValidationError) as exc:
            await call(hass, service, eid, **data)
        assert exc.value.translation_key == "cover_slats_open"
        assert exc.value.translation_placeholders == {"entity": eid}
    assert fake_link.sent == []
    await call(hass, SERVICE_STOP_COVER_TILT, eid)
    assert fake_link.sent[0][1] == BLIND_SLAT
    # lowered a little: the slats move again
    fake_link.inject(BLIND, 0xC090, level_status(-30000))
    await hass.async_block_till_done()
    fake_link.sent.clear()
    await call(hass, SERVICE_CLOSE_COVER_TILT, eid)
    assert [dst for _, dst, _ in fake_link.sent] == [BLIND_SLAT]


def param(name: str, uid: str = UID_BLIND) -> str:
    """The unique id of a blind's device parameter."""
    return f"{uid}-{name}"


async def test_blind_parameters_follow_the_operation_mode(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any],
    levels: LevelMesh, mesh: PropertyMesh, fast_sleep: list[float], fake_link: FakeProxyLink,
) -> None:  # fmt: skip
    """The app's Parameters tab per operation mode (`device-settings.md` §7.2): slat cells only for *blinds*, the
    slat time hidden for a shutter and at least 300 ms for blinds, the positions on power only for *stored
    position*; the behaviour after mains return without *stop* and *position for network failure*."""
    for uid in (UID_BLIND, UID_SHUTTER, UID_AWNING):
        enable(hass, "number", param("slats_move_time", uid))
        enable(hass, "number", param("blind_position_on_power", uid))
        enable(hass, "number", param("slat_position_on_power", uid))
        enable(hass, "select", param("move_on_power_mode", uid))
    mesh.values[BLIND, 0x1105] = b"\x04"  # stored position
    mesh.values[AWNING, 0x1105] = b"\x04"
    mesh.values[SHUTTER, 0x1105] = b"\x00"  # no reaction
    await start(hass, mock_config_entry)
    await settle(hass, 100)

    def state(domain: str, name: str, uid: str) -> Any:
        return hass.states.get(entity_id(hass, domain, param(name, uid)))

    # slat ventilation position (on by default) and slat position on power: blinds only
    assert (
        state("number", "slat_ventilation_position", UID_BLIND).state
        != STATE_UNAVAILABLE
    )
    for uid in (UID_SHUTTER, UID_AWNING):
        assert (
            state("number", "slat_ventilation_position", uid).state == STATE_UNAVAILABLE
        )
        assert state("number", "slat_position_on_power", uid).state == STATE_UNAVAILABLE
    assert (
        state("number", "slat_position_on_power", UID_BLIND).state != STATE_UNAVAILABLE
    )
    # the slat change-over time: from 300 ms for blinds, 0 ms as an awning's reversal time, none for a shutter
    assert state("number", "slats_move_time", UID_BLIND).attributes["min"] == 300
    assert state("number", "slats_move_time", UID_AWNING).attributes["min"] == 0
    assert state("number", "slats_move_time", UID_SHUTTER).state == STATE_UNAVAILABLE
    # the positions on power while the drive goes to its stored position only
    assert (
        state("number", "blind_position_on_power", UID_AWNING).state
        != STATE_UNAVAILABLE
    )
    assert (
        state("number", "blind_position_on_power", UID_SHUTTER).state
        == STATE_UNAVAILABLE
    )
    assert state("select", "move_on_power_mode", UID_SHUTTER).attributes["options"] == [
        "no_reaction",
        "move_up",
        "move_down",
        "move_to_stored_position",
    ]
    # choosing the stored position offers its position at once
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {
            ATTR_ENTITY_ID: entity_id(
                hass, "select", param("move_on_power_mode", UID_SHUTTER)
            ),
            "option": "move_to_stored_position",
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert (
        state("number", "blind_position_on_power", UID_SHUTTER).state
        != STATE_UNAVAILABLE
    )
    # a value the app cannot set shows as unknown
    fake_link.inject(SHUTTER, 0xC090, vendor_status(0x05, 0x1105, b"\x03"))
    await hass.async_block_till_done()
    assert state("select", "move_on_power_mode", UID_SHUTTER).state == STATE_UNKNOWN
    # a blind turned into a shutter loses its slat cells; one whose mode is not known (or not named) keeps them
    fake_link.inject(BLIND, 0xC090, vendor_status(0x05, COVER_MODE_PROPERTY, b"\x01"))
    await hass.async_block_till_done()
    assert (
        state("number", "slat_ventilation_position", UID_BLIND).state
        == STATE_UNAVAILABLE
    )
    fake_link.inject(BLIND, 0xC090, vendor_status(0x05, COVER_MODE_PROPERTY, b"\x02"))
    await hass.async_block_till_done()
    assert (
        state("number", "slat_ventilation_position", UID_BLIND).state
        != STATE_UNAVAILABLE
    )
    del mock_config_entry.runtime_data.states[BLIND].properties[COVER_MODE_PROPERTY]
    async_dispatcher_send(hass, SIGNAL_UPDATE.format(mock_config_entry.entry_id, BLIND))
    await hass.async_block_till_done()
    assert (
        state("number", "slat_ventilation_position", UID_BLIND).state
        != STATE_UNAVAILABLE
    )
    assert state("number", "slats_move_time", UID_BLIND).attributes["min"] == 0
