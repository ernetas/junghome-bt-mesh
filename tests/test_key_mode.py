"""What a key does: its connection on the event entity (from the export) and its key-mode sensor (0x5003)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.junghome_ble import config_entities as C
from custom_components.junghome_ble.const import CONF_CDB_PATH, DOMAIN
from custom_components.junghome_ble.event import connection_attributes
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.devices import KeyConnection

from . import property_helpers as ph
from .conftest import (
    CDB_PATH,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import (
    BUTTON_DIMMER,
    BUTTON_WC,
    ROCKER_A,
    UID_BUTTON_WC,
    UID_ROCKER_A,
    entity_id,
)
from .property_helpers import PropertyMesh, fake_hub, vendor_status

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

mesh = ph.mesh

PID_KEY_MODE = 0x5003
UID_KEY_MODE_A = f"{UID_ROCKER_A}-key_mode"
UID_KEY_MODE_WC = f"{UID_BUTTON_WC}-key_mode"


async def test_event_entities_say_what_their_key_drives(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    rocker = hass.states.get(entity_id(hass, "event", UID_ROCKER_A)).attributes
    assert (
        rocker["connection"],
        rocker["connection_address"],
        rocker["connection_name"],
    ) == ("device", "0232", "Living room DALI")
    wc = hass.states.get(entity_id(hass, "event", UID_BUTTON_WC)).attributes
    assert (wc["connection"], wc["connection_address"]) == ("gateway", "00DC")
    assert "connection_name" not in wc


async def test_connection_attributes_of_rooms_scenes_and_cleared_keys(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    button = init_integration.runtime_data.devices.by_address[BUTTON_DIMMER]
    button.connection = None
    assert connection_attributes(button) == {"connection": "none"}
    button.connection = KeyConnection("room", 0xC071, 0xC00F, "WC")
    assert connection_attributes(button) == {
        "connection": "room",
        "connection_address": "C00F",
        "connection_name": "WC",
    }
    button.connection = KeyConnection(
        "room", 0xC071
    )  # a room link the export has no record of
    assert connection_attributes(button) == {
        "connection": "room",
        "connection_address": "C071",
    }
    button.connection = KeyConnection("scene", 0xFFFF, name="Evening", scene=3)
    assert connection_attributes(button) == {
        "connection": "scene",
        "connection_address": "FFFF",
        "connection_name": "Evening",
        "connection_scene": 3,
    }


def test_one_key_mode_sensor_per_key_off_by_default() -> None:
    targets = C.key_mode_targets(fake_hub())
    assert [(t.address, t.key, t.translation_key) for t in targets] == [
        (BUTTON_WC, None, "key_mode"),
        (ROCKER_A, "A", "key_mode_key"),
        (0x0235, "B", "key_mode_key"),
        (BUTTON_DIMMER, None, "key_mode"),
    ]
    assert not any(t.enabled_default for t in targets)
    assert {t.specs for t in targets} == {(P.PROPERTIES[PID_KEY_MODE],)}
    assert targets[1].unique_id == UID_KEY_MODE_A
    # a key on a product without the property (none in the fixtures; a future product) gets no sensor
    hub = fake_hub()
    hub.devices.by_address[BUTTON_WC].node.pid = 0x0003
    assert BUTTON_WC not in {t.address for t in C.key_mode_targets(hub)}


async def test_key_mode_sensor_reads_the_mode(
    hass: HomeAssistant, mesh: PropertyMesh, fake_link: FakeProxyLink,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
) -> None:  # fmt: skip
    registry = er.async_get(hass)
    for uid in (UID_KEY_MODE_A, UID_KEY_MODE_WC):
        registry.async_get_or_create("sensor", DOMAIN, uid, disabled_by=None)
    mesh.values[ROCKER_A, PID_KEY_MODE] = bytes([5])  # switch
    mesh.values[BUTTON_WC, PID_KEY_MODE] = bytes([6])  # gateway
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    rocker = entity_id(hass, "sensor", UID_KEY_MODE_A)
    state = hass.states.get(rocker)
    assert state.state == "switch"
    assert state.attributes["options"] == [
        "light",
        "move",
        "scene",
        "property",
        "rtr",
        "switch",
        "gateway",
    ]
    assert (
        hass.states.get(entity_id(hass, "sensor", UID_KEY_MODE_WC)).state == "gateway"
    )
    assert registry.async_get(rocker).entity_category is EntityCategory.DIAGNOSTIC
    # `assign_key` writes the mode: its Status updates the sensor; a value the app does not name is unknown
    fake_link.inject(ROCKER_A, 0xC04F, vendor_status(0x05, PID_KEY_MODE, bytes([0])))
    await hass.async_block_till_done()
    assert hass.states.get(rocker).state == "light"
    fake_link.inject(ROCKER_A, 0xC04F, vendor_status(0x05, PID_KEY_MODE, bytes([0xFF])))
    await hass.async_block_till_done()
    assert hass.states.get(rocker).state == STATE_UNKNOWN


def test_a_lock_connection_names_its_time_limit() -> None:
    """A key in property mode whose cached 0x5006 / 0x5007 lock its target (`devices.with_key_lock`)."""
    button = fake_hub().devices.by_address[ROCKER_A]
    button.connection = KeyConnection(
        "device", 0xC061, 0x0148, "WC mirror", property_mode=True
    )
    values = {0x5006: bytes.fromhex("090001"), 0x5007: bytes.fromhex("02010e10")}
    assert connection_attributes(button, values) == {
        "connection": "lock",
        "connection_address": "0148",
        "connection_name": "WC mirror",
        "connection_lock_seconds": 0x100E,
    }
    assert connection_attributes(button)["connection"] == "device"


def property_mode_export(path: Path) -> str:
    """The fixture network with rocker A publishing from its LBC User Property client alone (KeyMode property)."""
    doc = json.loads(Path(CDB_PATH).read_text(encoding="utf-8"))
    node = next(n for n in doc["meshNetwork"]["nodes"] if n["unicastAddress"] == "0232")
    element = next(e for e in node["elements"] if e["index"] == ROCKER_A - 0x0232)
    for model in element["models"]:
        if model["modelId"] in ("1001", "1003"):
            model.pop("publish", None)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


async def test_a_key_in_property_mode_is_asked_what_it_locks(
    hass: HomeAssistant, mesh: PropertyMesh, fake_link: FakeProxyLink,
    mock_config_entry: MockConfigEntry, mock_bluetooth_env: dict[str, Any], fast_sleep: list[float],
    tmp_path: Path,
) -> None:  # fmt: skip
    """Its event entity reads KeySetPropertyMode and the up value once per link and says `lock` (unverified on air);
    a key in light mode is not asked; a reset of the key's property mode makes it a device link again."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_CDB_PATH: property_mode_export(tmp_path / "MeshNetwork.json"),
        },
    )
    mesh.values[ROCKER_A, 0x5006] = bytes.fromhex("090001")
    mesh.values[ROCKER_A, 0x5007] = bytes.fromhex("02013c00")
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    rocker = entity_id(hass, "event", UID_ROCKER_A)
    attrs = hass.states.get(rocker).attributes
    assert (
        attrs["connection"],
        attrs["connection_address"],
        attrs["connection_lock_seconds"],
    ) == ("lock", "0232", 60)
    assert {(ROCKER_A, 0x5006), (ROCKER_A, 0x5007)} <= set(mesh.gets)
    assert (0x0235, 0x5006) not in mesh.gets  # rocker B is in light mode
    # the app (or `assign_key`) resets the key's property mode: its Status reaches the entity
    fake_link.inject(ROCKER_A, 0xC04F, vendor_status(0x05, 0x5006, bytes(3)))
    await hass.async_block_till_done()
    attrs = hass.states.get(rocker).attributes
    assert attrs["connection"] == "device"
    assert "connection_lock_seconds" not in attrs
