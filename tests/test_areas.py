"""Rooms to areas (review-4 U4-2, `areas.py`): where the devices land, and what moves them afterwards.

The flow's `areas` step itself is covered in `test_config_flow.py`; here the registry side: an entry set up with
what that step stores, the buttons and node devices' areas, the reconfigure step moving devices, and
`sync_areas` after a room action.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble.areas import (
    area_id_for,
    area_name_for,
    async_move_devices,
    mapped_area,
)
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_ROOM_AREAS,
    CONF_UNICAST,
    DOMAIN,
    OPTION_ASSIGN_AREAS,
    OPTION_SYNC_AREAS,
)
from custom_components.junghome_ble.coordinator import JungHomeHub
from custom_components.junghome_ble.entity import (
    buttons_device_info,
    connection_room,
    gang_room,
    node_device_name,
    node_gangs,
    node_room,
    room_area_name,
)
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import (
    KeyConnection,
    Metadata,
    build_devices,
    room_names,
)

from . import test_services as services_env
from .conftest import CDB_PATH, FIXTURES, META_DIR, settle, setup_entry, wait_for_link
from .helpers import (
    MESH_UUID,
    NODE_ACTUATOR,
    NODE_GATEWAY,
    NODE_LIGHT_CTL,
    NODE_LIGHT_DIMMER,
    NODE_LIGHT_SWITCH,
    NODE_SOCKET,
    NODE_TRANSMITTER_1G,
    NODE_TRANSMITTER_2G,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    UID_SOCKET,
    areas_prefill,
    entity_id,
)
from .property_helpers import with_inserts
from .test_binary_sensor import make_detectors_entry, start_detectors
from .test_services import Env, call

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeProxyLink

env, export_source = services_env.env, services_env.export_source

# the devices of the base network that the WC holds: its two lights, their keys and their nodes
WC_DEVICES = {
    UID_LIGHT_SWITCH,
    f"{NODE_LIGHT_SWITCH}-0040-buttons",
    f"node:{NODE_LIGHT_SWITCH}",
    UID_LIGHT_DIMMER,
    f"{NODE_LIGHT_DIMMER}-0040-buttons",
    f"node:{NODE_LIGHT_DIMMER}",
}


def device(hass: HomeAssistant, entry: MockConfigEntry, ident: str) -> dr.DeviceEntry:
    found = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ident), entry.entry_id
    )
    assert found is not None, ident
    return found


def area_of(hass: HomeAssistant, entry: MockConfigEntry, ident: str) -> str | None:
    """The name of the area a device of the entry is in, None without one."""
    area_id = device(hass, entry, ident).area_id
    if area_id is None:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    assert area is not None
    return area.name


def area_names(hass: HomeAssistant) -> list[str]:
    return sorted(a.name for a in ar.async_get(hass).async_list_areas())


def base_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: CDB_PATH,
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
        options=options or {},
    )


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    await settle(hass)
    return entry


@pytest.fixture
def ready(
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """The fake proxy and the instant back-offs a real setup of the base network needs."""


@pytest.fixture
def toilet(hass: HomeAssistant) -> ar.AreaEntry:
    """An area that existed before the integration: another name, the room's as an alias (in other letters)."""
    return ar.async_get(hass).async_create("Toilet", aliases={"wc"})


# --------------------------------------------------------------------------- where the devices land


@pytest.mark.usefixtures("ready")
async def test_a_room_aliased_by_an_area_creates_no_duplicate(
    hass: HomeAssistant, toilet: ar.AreaEntry
) -> None:
    """The acceptance case: the `areas` step's prefill as stored, the WC's devices — keys and nodes included — go to
    the area that carries the room's name as an alias; no area named after it appears; the gateway and the mesh
    device stay out of every area."""
    kitchen = ar.async_get(hass).async_create("Kitchen")
    rooms = room_names(CDB.load(Path(CDB_PATH)))
    assert rooms == ["Kitchen", "Living room", "WC"]
    options = {
        OPTION_ASSIGN_AREAS: True,
        CONF_ROOM_AREAS: {"Kitchen": kitchen.id, "Living room": None, "WC": toilet.id},
    }
    entry = await start(hass, base_entry(options))
    assert area_names(hass) == ["Kitchen", "Living room", "Toilet"]
    for ident in WC_DEVICES:
        assert area_of(hass, entry, ident) == "Toilet", ident
    assert area_of(hass, entry, UID_SOCKET) == "Kitchen"
    assert area_of(hass, entry, f"node:{NODE_SOCKET}") == "Kitchen"
    assert area_of(hass, entry, f"node:{NODE_ACTUATOR}") == "Kitchen"
    assert area_of(hass, entry, f"{NODE_LIGHT_CTL}-0040-buttons") == "Living room"
    assert area_of(hass, entry, f"node:{NODE_GATEWAY}") is None
    assert area_of(hass, entry, f"mesh:{MESH_UUID}") is None
    # every buttons device of a node with a load has an area
    for dev in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        if any(i.endswith("-buttons") for _d, i in dev.identifiers):
            assert dev.area_id is not None, dev.name


@pytest.mark.usefixtures("ready")
async def test_without_a_mapping_the_room_finds_its_area_by_alias(
    hass: HomeAssistant, toilet: ar.AreaEntry
) -> None:
    """An entry the step never ran for (set up before it existed, or a room made later): the same matching."""
    entry = await start(hass, base_entry())
    assert area_of(hass, entry, UID_LIGHT_SWITCH) == "Toilet"
    assert "WC" not in area_names(hass)
    assert area_of(hass, entry, UID_SOCKET) == "Kitchen"  # created, as before


@pytest.mark.usefixtures("ready")
async def test_a_room_left_empty_gets_an_area_named_after_it(
    hass: HomeAssistant, toilet: ar.AreaEntry
) -> None:
    """Today's behaviour, chosen: the room's name, created though an area has it as an alias."""
    options = {OPTION_ASSIGN_AREAS: True, CONF_ROOM_AREAS: {"WC": None}}
    entry = await start(hass, base_entry(options))
    assert area_of(hass, entry, UID_LIGHT_SWITCH) == "WC"
    assert "WC" in area_names(hass)


@pytest.mark.usefixtures("ready")
async def test_assign_areas_off_leaves_every_device_without_one(
    hass: HomeAssistant,
) -> None:
    entry = await start(hass, base_entry({OPTION_ASSIGN_AREAS: False}))
    for dev in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        assert dev.area_id is None, dev.name
    assert area_names(hass) == []


async def test_a_wall_transmitter_goes_where_its_keys_switch(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """A battery wall transmitter has no load of its own: its keys and node go to the room of the load a key
    switches (the 2-gang's B key: the motion detector's relay, in the WC); one wired to the gateway alone gets no
    area. A detector's node device is the detector, in its relay's room."""
    entry = await start_detectors(hass, make_detectors_entry(), fake_link)
    assert area_of(hass, entry, f"{NODE_TRANSMITTER_2G}-0040-buttons") == "WC"
    assert area_of(hass, entry, f"node:{NODE_TRANSMITTER_2G}") == "WC"
    assert area_of(hass, entry, f"{NODE_TRANSMITTER_1G}-0040-buttons") is None
    assert area_of(hass, entry, f"node:{NODE_TRANSMITTER_1G}") is None


# --------------------------------------------------------------------------- the reconfigure step


async def reconfigure_areas(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    result = await entry.start_reconfigure_flow(hass)
    assert "areas" in result["menu_options"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "areas"}
    )


@pytest.mark.usefixtures("ready")
async def test_reconfigure_moves_only_the_devices_the_integration_placed(
    hass: HomeAssistant,
) -> None:
    """A new area for the WC: its devices move there — except the one the user put elsewhere, and one already
    without an area moves too; the count says how many; the choice is stored (and the entry reloads)."""
    entry = await start(hass, base_entry())
    registry = dr.async_get(hass)
    hall = ar.async_get(hass).async_create("Hall")
    bathroom = ar.async_get(hass).async_create("Bathroom")
    registry.async_update_device(
        device(hass, entry, UID_LIGHT_DIMMER).id, area_id=hall.id
    )
    registry.async_update_device(
        device(hass, entry, f"node:{NODE_LIGHT_DIMMER}").id, area_id=None
    )

    result = await reconfigure_areas(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "areas"
    prefill = areas_prefill(result)
    assert prefill[OPTION_ASSIGN_AREAS] is True
    assert set(prefill) == {OPTION_ASSIGN_AREAS, "Kitchen", "Living room", "WC"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**prefill, "WC": bathroom.id}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "areas_updated"
    assert result["description_placeholders"] == {"count": str(len(WC_DEVICES) - 1)}
    assert area_of(hass, entry, UID_LIGHT_DIMMER) == "Hall"  # placed by the user
    for ident in WC_DEVICES - {UID_LIGHT_DIMMER}:
        assert area_of(hass, entry, ident) == "Bathroom", ident
    assert area_of(hass, entry, UID_SOCKET) == "Kitchen"
    assert entry.options[CONF_ROOM_AREAS]["WC"] == bathroom.id
    await hass.async_block_till_done()
    await wait_for_link(hass, entry)
    await settle(hass)


@pytest.mark.usefixtures("ready")
async def test_reconfigure_prefills_the_stored_mapping_and_turning_areas_off_moves_nothing(
    hass: HomeAssistant,
) -> None:
    """The form shows what is stored — a mapped area that was deleted since shows empty — and switching areas off
    leaves every device where it is."""
    gone = ar.async_get(hass).async_create("Gone")
    entry = await start(
        hass,
        base_entry(
            {
                OPTION_ASSIGN_AREAS: True,
                CONF_ROOM_AREAS: {"WC": gone.id, "Kitchen": None},
            }
        ),
    )
    ar.async_get(hass).async_delete(gone.id)
    kitchen = ar.async_get(hass).async_get_area_by_name("Kitchen")
    assert kitchen is not None
    result = await reconfigure_areas(hass, entry)
    prefill = areas_prefill(result)
    assert "WC" not in prefill  # the mapped area is gone
    assert "Kitchen" not in prefill  # left empty on purpose
    assert (
        prefill["Living room"]
        == ar.async_get(hass).async_get_area_by_name("Living room").id
    )  # type: ignore[union-attr]
    before = {
        d.id: d.area_id
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {OPTION_ASSIGN_AREAS: False}
    )
    assert result["description_placeholders"] == {"count": "0"}
    assert before == {
        d.id: d.area_id
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    }
    assert entry.options[OPTION_ASSIGN_AREAS] is False
    await hass.async_block_till_done()
    await wait_for_link(hass, entry)
    await settle(hass)


@pytest.mark.usefixtures("ready")
async def test_the_step_refuses_an_entry_that_is_not_running(
    hass: HomeAssistant,
) -> None:
    """Offered while the entry runs; one unloaded meanwhile has no devices to compare."""
    entry = await start(hass, base_entry())
    result = await entry.start_reconfigure_flow(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "areas"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
    result = await entry.start_reconfigure_flow(hass)
    assert "areas" not in result["menu_options"]


# --------------------------------------------------------------------------- sync_areas after a room action


async def sync_on(hass: HomeAssistant, env: Env) -> None:
    """Switch `sync_areas` on: an options change, which reloads the entry."""
    hass.config_entries.async_update_entry(env.entry, options={OPTION_SYNC_AREAS: True})
    await hass.async_block_till_done()
    await services_env.settled(hass, env)


async def test_sync_areas_moves_a_light_and_what_hangs_off_its_node(
    hass: HomeAssistant, env: Env
) -> None:
    """`set_room` puts the WC mirror into the living room: its device, its node and the node's keys follow — unless
    the user placed one of them, which stays."""
    await sync_on(hass, env)
    entry = env.entry
    hall = ar.async_get(hass).async_create("Hall")
    dr.async_get(hass).async_update_device(
        device(hass, entry, f"{NODE_LIGHT_SWITCH}-0040-buttons").id, area_id=hall.id
    )
    assert area_of(hass, entry, UID_LIGHT_SWITCH) == "WC"
    await call(
        hass,
        "set_room",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
            "room": "Living room",
        },
    )
    assert area_of(hass, entry, UID_LIGHT_SWITCH) == "Living room"
    assert area_of(hass, entry, f"node:{NODE_LIGHT_SWITCH}") == "Living room"
    assert area_of(hass, entry, f"{NODE_LIGHT_SWITCH}-0040-buttons") == "Hall"
    assert area_of(hass, entry, UID_LIGHT_DIMMER) == "WC"  # its room did not change


async def test_without_sync_areas_a_room_change_moves_nothing(
    hass: HomeAssistant, env: Env
) -> None:
    await call(
        hass,
        "set_room",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
            "room": "Living room",
        },
    )
    assert area_of(hass, env.entry, UID_LIGHT_SWITCH) == "WC"
    assert area_of(hass, env.entry, f"node:{NODE_LIGHT_SWITCH}") == "WC"


async def test_sync_areas_after_a_change_followed_by_a_reload(
    hass: HomeAssistant, env: Env
) -> None:
    """A change the hub cannot take over in place reloads the entry: the new setup moves the devices instead."""
    await sync_on(hass, env)
    hub = env.hub
    with patch.object(JungHomeHub, "model_refusal", return_value="a test refusal"):
        await call(
            hass,
            "set_room",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                "room": "Kitchen",
            },
        )
    await services_env.settled(hass, env)
    assert env.hub is not hub
    assert area_of(hass, env.entry, UID_LIGHT_SWITCH) == "Kitchen"


async def test_set_room_places_an_area_less_device_where_the_room_maps(
    hass: HomeAssistant, env: Env
) -> None:
    """A room action puts a device without an area into its room's area: by alias too, not a new one by name."""
    areas = ar.async_get(hass)
    living = areas.async_get_area_by_name("Living room")
    assert living is not None  # made by the setup, for the room's own devices
    areas.async_delete(living.id)
    lounge = areas.async_create("Lounge", aliases={"Living room"})
    dr.async_get(hass).async_update_device(
        device(hass, env.entry, UID_LIGHT_SWITCH).id, area_id=None
    )
    await call(
        hass,
        "set_room",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
            "room": "Living room",
        },
    )
    assert device(hass, env.entry, UID_LIGHT_SWITCH).area_id == lounge.id
    assert areas.async_get_area_by_name("Living room") is None


async def test_set_room_places_nothing_when_areas_are_off(
    hass: HomeAssistant, env: Env
) -> None:
    hass.config_entries.async_update_entry(
        env.entry, options={OPTION_ASSIGN_AREAS: False}
    )
    await hass.async_block_till_done()
    await services_env.settled(hass, env)
    dr.async_get(hass).async_update_device(
        device(hass, env.entry, UID_LIGHT_SWITCH).id, area_id=None
    )
    await call(
        hass,
        "add_to_room",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
            "room": "Living room",
        },
    )
    assert area_of(hass, env.entry, UID_LIGHT_SWITCH) is None


# --------------------------------------------------------------------------- the pieces


def fake_hub(path: Path, meta: Metadata | None = None) -> Any:
    """A hub stand-in over a fixture network: no Home Assistant, so suggested areas are the rooms' names."""
    cdb = CDB.load(path)
    return with_inserts(
        SimpleNamespace(
            cdb=cdb, devices=build_devices(cdb, meta or Metadata()), device_ids={}
        )
    )


def test_what_a_key_drives_names_its_room() -> None:
    """A room link names the room; a load link the load's first room; a scene, the gateway or a group that is no
    room none."""
    hub = fake_hub(FIXTURES / "MeshNetwork-detectors.json")
    to_load = next(b for b in hub.devices.buttons if b.address == 0x0532).connection
    assert to_load is not None
    assert connection_room(hub, to_load) == "WC"
    assert connection_room(hub, None) is None
    assert connection_room(
        hub, KeyConnection("room", 0xC010, 0xC010, "Living room")
    ) == ("Living room")
    assert connection_room(hub, KeyConnection("group", 0xC020)) is None
    assert connection_room(hub, KeyConnection("scene", 0xFFFF, scene=1)) is None
    assert connection_room(hub, KeyConnection("device", 0xC0A0)) is None  # no target
    assert connection_room(hub, replace(to_load, target=0x0999)) is None  # unknown


def test_a_node_without_a_room_anywhere_has_none() -> None:
    """A wall transmitter whose keys switch nothing that has a room: no area for its keys, nor for its node; a
    node device named after its one gang once the app named it."""
    hub = fake_hub(FIXTURES / "MeshNetwork-detectors.json")
    node = next(n for n in hub.cdb.nodes if n.unicast == 0x0520)
    (gang,) = node_gangs(hub, node)
    assert gang_room(hub, gang) is None
    assert node_room(hub, node) is None
    assert "suggested_area" not in buttons_device_info(hub, gang)
    assert node_device_name(hub, node) == "Wall transmitter 1-gang 0520"
    for key in gang:
        key.gang = (0x40,)
        key.group_name = "Hall switch"
    assert node_device_name(hub, node) == "Hall switch - Wall transmitter 1-gang"
    two = next(n for n in hub.cdb.nodes if n.unicast == 0x0530)
    assert node_device_name(hub, two) == "Wall transmitter 2-gang 0530"  # unnamed
    assert room_area_name(hub, "WC") == "WC"


def test_a_node_with_two_outputs_keeps_its_label() -> None:
    hub = fake_hub(Path(CDB_PATH), Metadata(Path(META_DIR) / "device_metadata.json"))
    actuator = next(n for n in hub.cdb.nodes if n.unicast == 0x0400)
    socket = next(n for n in hub.cdb.nodes if n.unicast == 0x0172)
    assert node_device_name(hub, actuator) == "2-channel actuator 0400"
    assert node_device_name(hub, socket) == "Boiler - Socket (metering)"
    unnamed = fake_hub(Path(CDB_PATH))
    assert node_device_name(unnamed, socket) == "Socket 0172"


async def test_an_area_mapped_and_deleted_since_falls_back_to_the_room(
    hass: HomeAssistant,
) -> None:
    gone = ar.async_get(hass).async_create("Gone")
    options = {CONF_ROOM_AREAS: {"WC": gone.id}}
    assert mapped_area(hass, options, "WC") == gone.id
    assert area_name_for(hass, options, "WC") == "Gone"
    ar.async_get(hass).async_delete(gone.id)
    assert mapped_area(hass, options, "WC") is None
    assert area_name_for(hass, options, "WC") == "WC"
    assert area_id_for(hass, options, "WC") is None  # not created by asking
    assert area_name_for(hass, options, None) is None


async def test_a_device_the_registry_does_not_have_is_skipped(
    hass: HomeAssistant,
) -> None:
    assert (
        async_move_devices(hass, "entry", {"nothing": ("WC", "Kitchen")}, {}, {}) == 0
    )
