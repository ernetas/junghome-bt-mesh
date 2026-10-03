"""Climate platform: the room thermostat (RTR) — spec-only, against the synthetic `MeshNetwork-rtr.json` fixture."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_TARGET_TEMP_STEP,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome_ble import climate as CL
from custom_components.junghome_ble.config_entities import (
    SIG_SOFTWARE_VERSION,
    property_reader,
)
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
    RTR_BOOST_DURATION,
    RTR_BOOST_READBACK_MARGIN,
)
from custom_components.junghome_ble.coordinator import STATUS_HANDLERS, JungHomeHub
from custom_components.junghome_ble.entity import node_device_info
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.cdb import CDB, Element, Node
from custom_components.junghome_ble.jhmesh.devices import (
    Light,
    Metadata,
    Thermostat,
    build_devices,
)
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode

from . import property_helpers as ph
from .conftest import (
    FIXTURES,
    META_DIR,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import (
    NODE_THERMOSTAT,
    OUR_ADDRESS,
    SENSOR_POWER,
    SOCKET,
    SOCKET_SENSOR,
    UID_SOCKET,
    entity_id,
    onoff_status,
    sensor_status,
)
from .property_helpers import PropertyMesh, vendor_status

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

fast_timeouts, mesh = ph.fast_timeouts, ph.mesh

RTR_CDB_PATH = str(FIXTURES / "MeshNetwork-rtr.json")
RTR = 0x0500  # the thermostat's only element: set-point, OnOff server and sensor in one
UUID_RTR = NODE_THERMOSTAT.upper()
UID_RTR = f"{UUID_RTR.lower()}-0001"
PID_COMFORT, PID_ECO, PID_FROST, PID_HVAC_MODE = 0x1203, 0x1204, 0x1205, 0x120B
PID_BOOST, PID_AUTOMATIC = 0x120D, 0x1246
PID_AMBIENT = 0x004F
COMFORT, ECO, FROST = 21.0, 18.0, 7.0


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def cdb() -> CDB:
    return CDB.load(Path(RTR_CDB_PATH))


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: RTR_CDB_PATH,
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
    )


def level_status(level: int, target: int | None = None) -> bytes:
    p = level.to_bytes(2, "little", signed=True)
    if target is not None:
        p += target.to_bytes(2, "little", signed=True) + b"\x00"
    return encode_opcode(M.GEN_LEVEL_STATUS) + p


def ambient(raw: int) -> bytes:
    """Sensor Status carrying Present Ambient Temperature 0x004F (one byte, x 0.5 °C)."""
    return sensor_status((PID_AMBIENT, bytes([raw])))


class RtrMesh:
    """Makes the fake mesh's thermostat answer its SIG Gets (Level / OnOff / Sensor 0x004F), on top of `PropertyMesh`."""

    def __init__(self, mesh: PropertyMesh) -> None:
        self.mesh = mesh
        self.link = mesh.link
        self.level = CL.temperature_to_level(COMFORT)
        self.on = True
        self.ambient = 41  # 20.5 °C
        self.silent = False  # True: the thermostat answers no SIG Get
        self.gets: list[int] = []  # opcodes of the Gets the thermostat received
        self._inner = self.link.write_gatt_char
        self.link.write_gatt_char = self._write  # type: ignore[method-assign]
        mesh.values.update(
            {
                (RTR, PID_COMFORT): P.TEMP_001C.encode(COMFORT),
                (RTR, PID_ECO): P.TEMP_001C.encode(ECO),
                (RTR, PID_FROST): P.TEMP_001C.encode(FROST),
            }
        )
        mesh.silent.add((RTR, PID_HVAC_MODE))  # firmware < 2.2.0.0 by default

    async def _write(
        self, char: str, data: bytes, response: bool | None = None
    ) -> None:
        before = len(self.link.sent)
        await self._inner(char, data, response)
        for src, dst, access in self.link.sent[before:]:
            op, cid, _ = decode_opcode(access)
            if dst != RTR or cid is not None:
                continue
            if op in (M.GEN_LEVEL_GET, M.GEN_ONOFF_GET, M.SENSOR_GET):
                self.gets.append(op)
            if self.silent:
                continue
            if op == M.GEN_LEVEL_GET:
                self.link.inject(RTR, src, level_status(self.level))
            elif op == M.GEN_ONOFF_GET:
                self.link.inject(RTR, src, onoff_status(self.on))
            elif op == M.SENSOR_GET:
                self.link.inject(RTR, src, ambient(self.ambient))


@pytest.fixture
def rtr_mesh(mesh: PropertyMesh) -> RtrMesh:
    return RtrMesh(mesh)


@pytest.fixture
async def init_rtr(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    rtr_mesh: RtrMesh,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> MockConfigEntry:
    """The integration up against an answering thermostat, its connect-time reads through."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    await reads_done(hass, mock_config_entry)
    return mock_config_entry


async def reads_done(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Wait for the property reader's queue (our reads and the parameter entities') to drain."""
    reader = property_reader(hass, entry.runtime_data)
    await ph.wait_until(hass, lambda: reader._worker is None or reader._worker.done())


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    hub: JungHomeHub = entry.runtime_data
    return hub


def climate_state(hass: HomeAssistant) -> Any:
    state = hass.states.get(entity_id(hass, "climate", UID_RTR))
    assert state is not None
    return state


async def call(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id(hass, "climate", UID_RTR), **data},
        blocking=True,
    )


def level_sets(link: FakeProxyLink) -> list[int]:
    """The levels of every Generic Level Set sent to the thermostat, in order."""
    out = []
    for src, dst, access in link.sent:
        op, _, p = decode_opcode(access)
        if (src, dst, op) == (OUR_ADDRESS, RTR, M.GEN_LEVEL_SET):
            assert access == M.generic_level_set(
                int.from_bytes(p[:2], "little", signed=True), tid=p[2]
            )
            out.append(int.from_bytes(p[:2], "little", signed=True))
    return out


# --------------------------------------------------------------------------- temperature <-> level


def test_temperature_level_mapping_at_the_ends_and_back() -> None:
    assert CL.temperature_to_level(5.0) == -32768
    assert CL.temperature_to_level(30.0) == 32767
    assert CL.level_to_temperature(-32768) == 5.0
    assert CL.level_to_temperature(32767) == 30.0
    # the app's formulae: pct = round((t - 5) / 25 * 100), level = -32768 + pct / 100 * 65535
    assert CL.temperature_to_level(21.0) == round(-32768 + 0.64 * 65535)
    assert CL.level_to_temperature(0) == 17.5  # pct 50
    # every 0.5 °C step of the slider survives the round trip
    for half_degrees in range(10, 61):
        t = half_degrees / 2
        assert CL.level_to_temperature(CL.temperature_to_level(t)) == t
    # out-of-range input is clamped, never overflows the s16
    assert CL.temperature_to_level(-40.0) == -32768
    assert CL.temperature_to_level(99.0) == 32767
    assert CL.level_to_temperature(-40000) == 5.0
    assert CL.level_to_temperature(40000) == 30.0


# --------------------------------------------------------------------------- device derivation


def _node(pid: int, elements: list[tuple[int, list[str]]]) -> Node:
    node = Node(
        "AAAAAAAA-0000-0000-0000-00000000000A", "synthetic", 0x0700, b"\0" * 16, pid
    )
    node.elements = [
        Element(0x0700 + i, loc, models, node)
        for i, (loc, models) in enumerate(elements)
    ]
    return node


def test_thermostat_is_derived_from_the_fixture(cdb: CDB) -> None:
    devices = build_devices(
        cdb,
        Metadata(
            Path(META_DIR) / "device_metadata.json",
            Path(META_DIR) / "scene_metadata.json",
        ),
    )
    (rtr,) = devices.thermostats
    assert rtr == Thermostat(
        RTR, UID_RTR, "Room thermostat 0500", rtr.node, RTR, RTR, rooms=["Living room"]
    )
    assert rtr.kind == "thermostat"
    assert devices.by_address[RTR] is rtr
    assert "thermostat" in devices.kinds()
    # its OnOff server sits on a load location, but the thermostat rule claims the element before the light rule
    assert not any(isinstance(devices.by_address[a], Light) for a in (RTR,))
    assert len(devices.lights) == 5


def test_thermostat_rule_resolves_the_elements_and_the_app_name(cdb: CDB) -> None:
    # set-point on a second element, OnOff server and sensor elsewhere, the app's name for it
    split = _node(
        0x0A,
        [(0x0001, ["1000", "1011"]), (0x0001, ["1002"]), (0x0040, ["1100", "1001"])],
    )
    # a thermostat listing neither an OnOff server nor a sensor: the primary element is the set-point
    bare = _node(0x0A, [(0x0001, ["1011"])])
    bare.uuid = "BBBBBBBB-0000-0000-0000-00000000000B"
    bare.unicast = 0x0710
    for i, e in enumerate(bare.elements):
        e.address = 0x0710 + i
    cdb.nodes += [split, bare]
    meta = Metadata.from_export(
        {
            "devices": [
                {
                    "name": "Bedroom",
                    "deviceId": {"nodeId": split.uuid, "locationIds": [1]},
                }
            ]
        }
    )
    devices = build_devices(cdb, meta)
    by_node = {t.node.uuid: t for t in devices.thermostats}
    assert by_node[split.uuid] == Thermostat(
        0x0701, f"{split.uuid.lower()}-0001", "Bedroom", split, 0x0700, 0x0702
    )
    assert 0x0700 not in devices.by_address  # the OnOff element is not a light either
    assert by_node[bare.uuid] == Thermostat(
        0x0710, f"{bare.uuid.lower()}-0001", "synthetic 0710", bare, None, None
    )


# --------------------------------------------------------------------------- status handlers


async def test_level_and_sensor_handlers_chain_on_the_hubs(
    hass: HomeAssistant, init_rtr: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_rtr)
    # the Level Status is the hub's own handler (present and target, for any element): climate adds nothing to it
    assert STATUS_HANDLERS[None, M.GEN_LEVEL_STATUS] is JungHomeHub._on_level_status
    fake_link.inject(0x0400, OUR_ADDRESS, level_status(-1234, 1234))
    fake_link.inject(0x0001, OUR_ADDRESS, encode_opcode(M.GEN_LEVEL_STATUS) + b"\x01")
    await hass.async_block_till_done()
    assert (hub.states[0x0400].level, hub.states[0x0400].target_level) == (-1234, 1234)
    assert 0x0001 not in hub.states
    # the socket's Sensor Status still reaches the hub's own handler (power), the thermostat's adds 0x004F
    fake_link.inject(
        SOCKET_SENSOR,
        0xC001,
        sensor_status((SENSOR_POWER, (1234).to_bytes(2, "little"))),
    )
    fake_link.inject(RTR, 0xC090, ambient(0x7F))  # "unknown"
    await hass.async_block_till_done()
    assert hub.states[SOCKET].power_w == 123.4
    assert (
        hass.states.get(entity_id(hass, "sensor", f"{UID_SOCKET}-power")).state
        == "123.4"
    )
    assert hub.states[RTR].properties[PID_AMBIENT] == b"\x7f"
    assert climate_state(hass).attributes[ATTR_CURRENT_TEMPERATURE] is None


# --------------------------------------------------------------------------- entity


async def test_entity_attributes_after_the_connect_time_reads(
    hass: HomeAssistant, init_rtr: MockConfigEntry, rtr_mesh: RtrMesh
) -> None:
    eid = entity_id(hass, "climate", UID_RTR)
    # the node device is the thermostat, in its room
    assert eid == "climate.living_room_room_thermostat_0500"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == HVACMode.HEAT
    attrs = state.attributes
    assert attrs[ATTR_HVAC_MODES] == [HVACMode.HEAT, HVACMode.AUTO]
    assert attrs[ATTR_MIN_TEMP] == 5.0
    assert attrs[ATTR_MAX_TEMP] == 30.0
    assert attrs[ATTR_TARGET_TEMP_STEP] == 0.5
    assert attrs[ATTR_PRESET_MODES] == [
        PRESET_NONE,
        PRESET_COMFORT,
        PRESET_ECO,
        "frost",
        PRESET_BOOST,
    ]
    assert (
        attrs[ATTR_SUPPORTED_FEATURES]
        == ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    assert attrs["mesh_address"] == "0500"
    assert attrs["rooms"] == ["Living room"]
    # the connect-time reads: the three state Gets, then the preset temperatures, the mode, boost and automatic
    assert rtr_mesh.gets == [M.GEN_LEVEL_GET, M.GEN_ONOFF_GET, M.SENSOR_GET]
    ours = {PID_COMFORT, PID_ECO, PID_FROST, PID_HVAC_MODE, PID_BOOST, PID_AUTOMATIC}
    assert [pid for addr, pid in rtr_mesh.mesh.gets if addr == RTR and pid in ours] == [
        PID_COMFORT,
        PID_ECO,
        PID_FROST,
        PID_HVAC_MODE,
        PID_HVAC_MODE,
        PID_HVAC_MODE,  # old firmware: silent, three attempts
        PID_BOOST,
        PID_AUTOMATIC,
    ]
    assert attrs[ATTR_TEMPERATURE] == COMFORT
    assert attrs[ATTR_CURRENT_TEMPERATURE] == 20.5
    assert attrs[ATTR_HVAC_ACTION] == HVACAction.HEATING
    assert attrs[ATTR_PRESET_MODE] == PRESET_COMFORT
    # it hangs off the node device, next to the RTR's parameter entities
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.device_id is not None
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device is not None
    assert (DOMAIN, f"node:{UUID_RTR.lower()}") in device.identifiers
    assert (
        device.name == "Room thermostat 0500"
    )  # no app name in this export: the node's
    assert device.area_id == "living_room"  # its room, like a load's own device


async def test_the_node_device_takes_the_thermostats_app_name(
    hass: HomeAssistant, init_rtr: MockConfigEntry
) -> None:
    hub = hub_of(init_rtr)
    [thermostat] = hub.devices.thermostats
    thermostat.name = "Bathroom heating"
    info = node_device_info(hub, thermostat.node)
    assert (info["name"], info["suggested_area"]) == ("Bathroom heating", "Living room")
    thermostat.rooms = []
    assert "suggested_area" not in node_device_info(hub, thermostat.node)


async def test_state_is_read_again_after_every_reconnect(
    hass: HomeAssistant,
    init_rtr: MockConfigEntry,
    rtr_mesh: RtrMesh,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    rtr_mesh.gets.clear()
    fake_link.inject(
        RTR, 0xC090, onoff_status(False)
    )  # an update on the same link: no re-read
    await hass.async_block_till_done()
    assert rtr_mesh.gets == []
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert climate_state(hass).state == STATE_UNAVAILABLE
    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, init_rtr)
    await settle(hass, 200)
    assert climate_state(hass).state == HVACMode.HEAT
    assert rtr_mesh.gets == [M.GEN_LEVEL_GET, M.GEN_ONOFF_GET, M.SENSOR_GET]


async def test_truncated_sensor_status_is_dropped_quietly(
    hass: HomeAssistant,
    init_rtr: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PLT-01: a Sensor Status cut short is dropped by the chained climate handler too, not raised into the
    client's "on_message handler failed" ERROR traceback; the next good one still counts."""
    fake_link.inject(RTR, 0xC090, encode_opcode(M.SENSOR_STATUS) + b"\x9e")
    await hass.async_block_till_done()
    assert "on_message handler failed" not in caplog.text
    fake_link.inject(RTR, 0xC090, ambient(0x2A))
    await hass.async_block_till_done()
    assert climate_state(hass).attributes[ATTR_CURRENT_TEMPERATURE] == 21.0


async def test_current_temperature_and_action_follow_the_publications(
    hass: HomeAssistant, init_rtr: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.inject(RTR, 0xC090, ambient(0x2A))  # 21.0
    fake_link.inject(RTR, 0xC090, onoff_status(False))
    await hass.async_block_till_done()
    attrs = climate_state(hass).attributes
    assert attrs[ATTR_CURRENT_TEMPERATURE] == 21.0
    assert attrs[ATTR_HVAC_ACTION] == HVACAction.IDLE
    fake_link.inject(RTR, 0xC090, ambient((-5) & 0xFF))  # signed: -2.5 °C
    fake_link.inject(RTR, 0xC090, onoff_status(True, target=True, remaining=0))
    await hass.async_block_till_done()
    attrs = climate_state(hass).attributes
    assert attrs[ATTR_CURRENT_TEMPERATURE] == -2.5
    assert attrs[ATTR_HVAC_ACTION] == HVACAction.HEATING


async def test_set_temperature_sends_a_level_set(
    hass: HomeAssistant, init_rtr: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 22.5})
    assert level_sets(fake_link) == [CL.temperature_to_level(22.5)]
    assert level_sets(fake_link) == [13106]  # pct 70
    assert len(fake_link.sent) == 1
    # the firmware confirms by publishing the new level to the element group
    fake_link.inject(RTR, 0xC090, level_status(13106))
    await hass.async_block_till_done()
    attrs = climate_state(hass).attributes
    assert attrs[ATTR_TEMPERATURE] == 22.5
    assert attrs[ATTR_PRESET_MODE] == PRESET_NONE  # matches no preset temperature
    # the ends of the slider
    for t, level in ((5.0, -32768), (30.0, 32767)):
        fake_link.sent.clear()
        await call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: t})
        assert level_sets(fake_link) == [level]
    # HA rejects a set-point outside 5..30 before anything is sent
    fake_link.sent.clear()
    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 31.0})
    assert fake_link.sent == []


async def test_set_temperature_without_a_temperature_is_ignored(
    hass: HomeAssistant, init_rtr: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_rtr)
    entity = CL.JungHomeClimate(hub, hub.devices.thermostats[0])
    fake_link.sent.clear()
    await entity.async_set_temperature(hvac_mode=HVACMode.HEAT)
    assert fake_link.sent == []


async def test_presets_are_derived_from_the_set_point_on_old_firmware(
    hass: HomeAssistant, init_rtr: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_rtr)
    for temperature, preset in (
        (ECO, PRESET_ECO),
        (FROST, "frost"),
        (COMFORT, PRESET_COMFORT),
        (19.5, PRESET_NONE),
    ):
        fake_link.inject(
            RTR, 0xC090, level_status(CL.temperature_to_level(temperature))
        )
        await hass.async_block_till_done()
        assert climate_state(hass).attributes[ATTR_PRESET_MODE] == preset
    # a preset temperature the RTR did not report cannot match; none reported at all: the preset is unknown
    props = hub.states[RTR].properties
    del props[PID_ECO]
    fake_link.inject(RTR, 0xC090, level_status(CL.temperature_to_level(ECO)))
    await hass.async_block_till_done()
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] == PRESET_NONE
    del props[PID_COMFORT], props[PID_FROST]
    hub.notify_update(RTR)
    await hass.async_block_till_done()
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] is None
    # a malformed preset temperature (too short) is as good as none
    fake_link.inject(RTR, OUR_ADDRESS, vendor_status(0x05, PID_COMFORT, b"\x01"))
    await hass.async_block_till_done()
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] is None
    # no set-point at all: nothing to derive from
    hub.states[RTR].level = None
    hub.notify_update(RTR)
    await hass.async_block_till_done()
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] is None
    assert climate_state(hass).attributes[ATTR_TEMPERATURE] is None


async def test_set_preset_writes_the_preset_temperature_on_old_firmware(
    hass: HomeAssistant,
    init_rtr: MockConfigEntry,
    fake_link: FakeProxyLink,
    rtr_mesh: RtrMesh,
) -> None:
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_ECO})
    assert level_sets(fake_link) == [CL.temperature_to_level(ECO)]
    assert len(fake_link.sent) == 1  # no mode property on this firmware
    assert rtr_mesh.mesh.sets == []
    # `none` is a derived fact, not a command
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_NONE})
    assert fake_link.sent == []
    with pytest.raises(
        ServiceValidationError
    ):  # HA validates the preset against the list
        await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "away"})


async def test_set_preset_reads_an_unknown_preset_temperature_on_demand(
    hass: HomeAssistant,
    init_rtr: MockConfigEntry,
    fake_link: FakeProxyLink,
    rtr_mesh: RtrMesh,
) -> None:
    hub = hub_of(init_rtr)
    mesh = rtr_mesh.mesh
    del hub.states[RTR].properties[PID_FROST]
    mesh.gets.clear()
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "frost"})
    assert mesh.gets == [(RTR, PID_FROST)]
    assert level_sets(fake_link) == [CL.temperature_to_level(FROST)]
    # a thermostat that does not answer: nothing to write, the call fails
    del hub.states[RTR].properties[PID_FROST]
    mesh.silent.add((RTR, PID_FROST))
    fake_link.sent.clear()
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "frost"})
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "preset_temperature_unknown"
    assert exc.value.translation_placeholders == {"preset": "frost"}
    assert level_sets(fake_link) == []


async def test_new_firmware_reports_and_takes_the_mode_property(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    rtr_mesh: RtrMesh,
    fast_sleep: list[float],
    fast_timeouts: None,
    fake_link: FakeProxyLink,
) -> None:
    mesh = rtr_mesh.mesh
    mesh.silent.discard((RTR, PID_HVAC_MODE))
    mesh.values[RTR, PID_HVAC_MODE] = b"\x02"  # eco, whatever the set-point says
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] == PRESET_ECO
    assert climate_state(hass).attributes[ATTR_TEMPERATURE] == COMFORT
    # selecting a preset writes its temperature as the set-point *and* the mode, as the app does on this firmware
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "frost"})
    assert level_sets(fake_link) == [CL.temperature_to_level(FROST)]
    assert mesh.sets == [(RTR, PID_HVAC_MODE, b"\x03")]
    assert (
        climate_state(hass).attributes[ATTR_PRESET_MODE] == "frost"
    )  # confirmed by the Status
    # a published 'none' is shown as such; a mode the table does not know falls back to the derivation, from the
    # frost set-point the thermostat reported answering the Level Set
    for raw, preset in ((b"\x00", PRESET_NONE), (b"\x09", "frost")):
        fake_link.inject(RTR, 0xC090, vendor_status(0x05, PID_HVAC_MODE, raw))
        await hass.async_block_till_done()
        assert climate_state(hass).attributes[ATTR_PRESET_MODE] == preset
    # with the mode property known, an unread preset temperature is no obstacle: the mode alone is written
    hub = hub_of(mock_config_entry)
    hub.states[RTR].properties[PID_HVAC_MODE] = b"\x01"
    del hub.states[RTR].properties[PID_ECO]
    mesh.silent.add((RTR, PID_ECO))
    mesh.sets.clear()
    mesh.gets.clear()
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_ECO})
    assert level_sets(fake_link) == []
    assert mesh.gets == []
    assert mesh.sets == [(RTR, PID_HVAC_MODE, b"\x02")]
    # a mode the thermostat neither confirms nor reports back is an error, not a success
    mesh.silent.add((RTR, PID_HVAC_MODE))
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: "frost"})
    assert exc.value.translation_key == "setting_no_answer"


def test_mode_property_is_only_read_on_firmware_that_has_it() -> None:
    """The version is unknown until something reads SIG 0x001A: then every property is assumed supported."""
    hub = ph.fake_hub()
    cdb = CDB.load(Path(RTR_CDB_PATH))
    hub.cdb, hub.devices = cdb, build_devices(cdb)
    entity = CL.JungHomeClimate(hub, hub.devices.thermostats[0])
    assert [s.id for s in entity._property_specs()] == [
        PID_COMFORT,
        PID_ECO,
        PID_FROST,
        PID_HVAC_MODE,
        PID_BOOST,
        PID_AUTOMATIC,
    ]
    hub.states[RTR] = SimpleNamespace(
        properties={SIG_SOFTWARE_VERSION: P.ASCII_VERSION.encode("2.1.0.9")}
    )
    assert [s.id for s in entity._property_specs()] == [
        PID_COMFORT,
        PID_ECO,
        PID_FROST,
        PID_BOOST,
        PID_AUTOMATIC,
    ]
    hub.states[RTR].properties[SIG_SOFTWARE_VERSION] = P.ASCII_VERSION.encode("2.2.0.0")
    assert PID_HVAC_MODE in [s.id for s in entity._property_specs()]


async def test_hvac_mode_is_automatic_operation(
    hass: HomeAssistant,
    init_rtr: MockConfigEntry,
    rtr_mesh: RtrMesh,
    fake_link: FakeProxyLink,
) -> None:
    """`auto` is the RTR's own comfort / eco profile (0x1246 = 1), `heat` manual (0); there is no off."""
    mesh = rtr_mesh.mesh
    assert climate_state(hass).state == HVACMode.HEAT  # read at link-up: manual
    # already manual: nothing to send
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    assert fake_link.sent == []
    await call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    assert mesh.sets == [(RTR, PID_AUTOMATIC, b"\x01")]
    assert climate_state(hass).state == HVACMode.AUTO  # confirmed by the Status
    await call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    assert mesh.sets[-1] == (RTR, PID_AUTOMATIC, b"\x00")
    assert climate_state(hass).state == HVACMode.HEAT
    # switched on the device (or by the app): taken from the Status it publishes
    fake_link.inject(RTR, 0xC090, vendor_status(0x05, PID_AUTOMATIC, b"\x01"))
    await hass.async_block_till_done()
    assert climate_state(hass).state == HVACMode.AUTO
    # unknown (never answered) counts as manual, and `heat` is then written
    del init_rtr.runtime_data.states[RTR].properties[PID_AUTOMATIC]
    mesh.sets.clear()
    await call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    assert mesh.sets == [(RTR, PID_AUTOMATIC, b"\x00")]
    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.OFF})
    # a mode the thermostat neither confirms nor reports back is an error
    mesh.silent.add((RTR, PID_AUTOMATIC))
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    assert exc.value.translation_key == "setting_no_answer"


BOOST_READBACK = timedelta(seconds=RTR_BOOST_DURATION + RTR_BOOST_READBACK_MARGIN + 1)


async def test_boost_is_a_preset_read_back_until_the_thermostat_ends_it(
    hass: HomeAssistant,
    init_rtr: MockConfigEntry,
    rtr_mesh: RtrMesh,
    fake_link: FakeProxyLink,
) -> None:
    """Boost writes 0x120D = 1 and nothing else; the RTR ends it after five minutes without a word, so it is read
    back then, and again as long as it still reads on."""
    mesh = rtr_mesh.mesh
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_BOOST})
    assert mesh.sets == [(RTR, PID_BOOST, b"\x01")]
    assert level_sets(fake_link) == []  # a boost has no set-point
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] == PRESET_BOOST
    # nothing is read before the boost should have ended
    mesh.gets.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=200))
    await settle(hass)
    assert mesh.gets == []
    # still boosting at the first read-back (restarted on the device, say): asked again later
    later = dt_util.utcnow() + BOOST_READBACK
    async_fire_time_changed(hass, later)
    await settle(hass)
    assert mesh.gets == [(RTR, PID_BOOST)]
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] == PRESET_BOOST
    mesh.values[RTR, PID_BOOST] = b"\x00"  # the RTR ends it and tells no one
    async_fire_time_changed(hass, later + BOOST_READBACK)
    await settle(hass)
    assert mesh.gets == [(RTR, PID_BOOST)] * 2
    # back to what the set-point says
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] == PRESET_COMFORT
    async_fire_time_changed(hass, later + 3 * BOOST_READBACK)
    await settle(hass)
    assert mesh.gets == [(RTR, PID_BOOST)] * 2  # nothing left to read back


async def test_a_boost_started_elsewhere_is_followed_and_any_other_preset_ends_it(
    hass: HomeAssistant,
    init_rtr: MockConfigEntry,
    rtr_mesh: RtrMesh,
    fake_link: FakeProxyLink,
) -> None:
    mesh = rtr_mesh.mesh
    # started on the device: the published Status arms the read-back, a repeat keeps the one pending
    for _ in range(2):
        fake_link.inject(RTR, 0xC090, vendor_status(0x05, PID_BOOST, b"\x01"))
        await hass.async_block_till_done()
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] == PRESET_BOOST
    # `none` ends it and sends nothing else
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_NONE})
    assert mesh.sets == [(RTR, PID_BOOST, b"\x00")]
    assert level_sets(fake_link) == []
    assert climate_state(hass).attributes[ATTR_PRESET_MODE] == PRESET_COMFORT
    # ended: no read-back is pending any more
    mesh.gets.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + BOOST_READBACK)
    await settle(hass)
    assert mesh.gets == []
    # boosting again, then eco: the boost ends first, then the preset temperature goes out
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_BOOST})
    mesh.sets.clear()
    fake_link.sent.clear()
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_ECO})
    assert mesh.sets == [(RTR, PID_BOOST, b"\x00")]
    assert level_sets(fake_link) == [CL.temperature_to_level(ECO)]
    # a boost the thermostat does not confirm is an error, and nothing is read back for it
    mesh.silent.add((RTR, PID_BOOST))
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_BOOST})
    assert exc.value.translation_key == "setting_no_answer"


async def test_unload_drops_a_pending_boost_read_back(
    hass: HomeAssistant, init_rtr: MockConfigEntry, rtr_mesh: RtrMesh
) -> None:
    mesh = rtr_mesh.mesh
    await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_BOOST})
    gets = len(mesh.gets)
    assert await hass.config_entries.async_unload(init_rtr.entry_id)
    async_fire_time_changed(hass, dt_util.utcnow() + BOOST_READBACK)
    await settle(hass)
    assert len(mesh.gets) == gets


# --------------------------------------------------------------------------- open window (binary_sensor.py)

UID_WINDOW = f"{UUID_RTR.lower()}-0001-rtr_drop_of_temp_state"
PID_WINDOW = 0x1225


async def test_open_window_is_off_by_default(
    hass: HomeAssistant, init_rtr: MockConfigEntry, rtr_mesh: RtrMesh
) -> None:
    """Unverified on air: registered disabled, so nothing asks the thermostat for it."""
    entry = er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, UID_WINDOW)
    assert entry is not None
    registered = er.async_get(hass).async_get(entry)
    assert registered is not None
    assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registered.entity_category is None
    assert (RTR, PID_WINDOW) not in rtr_mesh.mesh.gets


async def test_open_window_follows_the_drop_of_temperature_state(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    init_rtr: MockConfigEntry,
    rtr_mesh: RtrMesh,
    fake_link: FakeProxyLink,
) -> None:
    """Read once per link from the primary element, then taken from what the thermostat publishes."""
    eid = entity_id(hass, "binary_sensor", UID_WINDOW)
    assert eid == "binary_sensor.living_room_room_thermostat_0500_open_window"
    assert (RTR, PID_WINDOW) in rtr_mesh.mesh.gets
    state = hass.states.get(eid)
    assert state.state == "off"
    assert state.attributes[ATTR_DEVICE_CLASS] == "window"
    assert state.attributes["property_id"] == "0x1225"
    fake_link.inject(RTR, 0xC090, vendor_status(0x05, PID_WINDOW, b"\x01"))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "on"
    fake_link.inject(RTR, 0xC090, vendor_status(0x05, PID_WINDOW, b""))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "on"  # no value: dropped, as the app drops it


async def test_send_failure_raises(
    hass: HomeAssistant, init_rtr: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    hub = hub_of(init_rtr)
    fake_link.write_error = ConnectionError("proxy disconnected")
    for service, data in (
        (SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 20.0}),
        (SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: PRESET_COMFORT}),
    ):
        with pytest.raises(HomeAssistantError) as exc:
            await call(hass, service, **data)
        assert exc.value.translation_domain == DOMAIN
        assert exc.value.translation_key == "send_failed"
    # the mode write failing (no preset temperature to send first) surfaces the same way
    hub.states[RTR].properties[PID_HVAC_MODE] = b"\x01"
    del hub.states[RTR].properties[PID_COMFORT]
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_COMFORT})
    assert exc.value.translation_key == "send_failed"


async def test_refresh_survives_a_silent_or_vanishing_thermostat(
    hass: HomeAssistant, init_rtr: MockConfigEntry, rtr_mesh: RtrMesh
) -> None:
    hub = hub_of(init_rtr)
    entity = CL.JungHomeClimate(hub, hub.devices.thermostats[0])
    entity.hass = hass
    mesh = rtr_mesh.mesh
    mesh.gets.clear()
    rtr_mesh.gets.clear()
    entity.reader._read_at.clear()  # a property answered moments ago would not be asked again
    rtr_mesh.silent = True
    with patch.object(CL, "STATE_GET_TIMEOUT", 0.01):
        await (
            entity._refresh()
        )  # unanswered state Gets: the property reads still happen
    assert (
        rtr_mesh.gets
        == [M.GEN_LEVEL_GET] * 3 + [M.GEN_ONOFF_GET] * 3 + [M.SENSOR_GET] * 3
    )
    assert [pid for addr, pid in mesh.gets if addr == RTR] == [
        PID_COMFORT,
        PID_ECO,
        PID_FROST,
        PID_HVAC_MODE,
        PID_HVAC_MODE,
        PID_HVAC_MODE,
        PID_BOOST,
        PID_AUTOMATIC,
    ]
    rtr_mesh.silent = False
    mesh.gets.clear()
    with patch.object(hub.proxy, "request", side_effect=ConnectionError("gone")):
        await entity._refresh()  # the link went away: nothing more is tried
    assert mesh.gets == []
    with patch.object(type(entity.reader), "read", return_value=False) as read:
        await (
            entity._refresh()
        )  # a preset the RTR does not answer does not stop the others
    assert (
        read.call_count == 6
    )  # (on a lost link each of them fails at once inside `read`)


async def test_thermostat_without_onoff_or_sensor_elements(
    hass: HomeAssistant, init_rtr: MockConfigEntry, rtr_mesh: RtrMesh
) -> None:
    hub = hub_of(init_rtr)
    bare = Thermostat(RTR, UID_RTR, "bare", hub.devices.thermostats[0].node, None, None)
    entity = CL.JungHomeClimate(hub, bare)
    entity.hass = hass
    assert entity.addresses == {RTR}
    assert entity.hvac_action is None
    assert entity.current_temperature is None
    assert entity.target_temperature == COMFORT  # the set-point element is still read
    rtr_mesh.gets.clear()
    await entity._refresh()
    assert rtr_mesh.gets == [M.GEN_LEVEL_GET]  # nothing else to ask
    # heating demand stays unknown while the OnOff element has not been heard from
    hub.states.pop(RTR)
    full = CL.JungHomeClimate(hub, hub.devices.thermostats[0])
    assert entity.hvac_action is None
    assert entity.target_temperature is None
    assert full.hvac_action is None
    assert full.current_temperature is None
    hub.element_state(RTR)  # heard from, but not its OnOff status yet
    assert full.hvac_action is None


async def test_no_refresh_while_disconnected(
    hass: HomeAssistant, init_rtr: MockConfigEntry, rtr_mesh: RtrMesh, fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:  # fmt: skip
    hub = hub_of(init_rtr)
    entity = CL.JungHomeClimate(hub, hub.devices.thermostats[0])
    entity.hass = hass
    rtr_mesh.gets.clear()
    since, hub.connected_since = (
        hub.connected_since,
        None,
    )  # attached, stamp not set yet
    entity._maybe_refresh()
    hub.connected_since = since
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    entity._maybe_refresh()
    await settle(hass, 50)
    assert rtr_mesh.gets == []
    assert entity._refreshed_for is None


async def test_a_thermostat_spread_over_elements_is_watched_on_each(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    rtr_mesh: RtrMesh,
    fast_sleep: list[float],
    fast_timeouts: None,
    fake_link: FakeProxyLink,
) -> None:
    """Heating demand and temperature from other elements than the set-point's reach the entity as they arrive."""
    from custom_components.junghome_ble import coordinator  # noqa: PLC0415

    real = coordinator.build_devices
    other = 0x0501  # OnOff server and sensor on a second element (synthetic; never answers a Get)

    def with_split(cdb: CDB, meta: Metadata) -> Any:
        devices = real(cdb, meta)
        rtr = devices.thermostats[0]
        devices.add(
            Thermostat(RTR, f"{UID_RTR}-split", "split", rtr.node, other, other)
        )
        return devices

    with (
        patch.object(coordinator, "build_devices", side_effect=with_split),
        patch.object(CL, "STATE_GET_TIMEOUT", 0.01),
    ):
        await setup_entry(hass, mock_config_entry)
        await wait_for_link(hass, mock_config_entry)
        await settle(hass, 200)
        await reads_done(hass, mock_config_entry)
    eid = entity_id(hass, "climate", f"{UID_RTR}-split")
    attrs = hass.states.get(eid).attributes
    assert attrs[ATTR_TEMPERATURE] == COMFORT  # the shared set-point element
    assert ATTR_HVAC_ACTION not in attrs  # unknown: HA leaves the attribute out
    assert attrs[ATTR_CURRENT_TEMPERATURE] is None
    fake_link.inject(other, 0xC090, onoff_status(True))
    fake_link.inject(other, 0xC090, ambient(0x30))  # 24.0
    await hass.async_block_till_done()
    attrs = hass.states.get(eid).attributes
    assert attrs[ATTR_HVAC_ACTION] == HVACAction.HEATING
    assert attrs[ATTR_CURRENT_TEMPERATURE] == 24.0


# --------------------------------------------------------------------------- All thermostats (the app's central function)


async def test_all_thermostats_follows_each_element_once(
    hass: HomeAssistant, init_rtr: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """The thermostat's sensor is its set-point element: one subscription, which the unload removes cleanly."""
    hub = hub_of(init_rtr)
    eid = entity_id(hass, "climate", f"{hub.cdb.mesh_uuid.lower()}-central-fef9")
    assert hass.data["climate"].get_entity(eid).watched == [RTR]
    assert await hass.config_entries.async_unload(init_rtr.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown dispatcher" not in caplog.text


async def test_all_thermostats_sets_every_set_point_with_one_message(
    hass: HomeAssistant, init_rtr: MockConfigEntry, rtr_mesh: RtrMesh
) -> None:
    hub = hub_of(init_rtr)
    eid = entity_id(hass, "climate", f"{hub.cdb.mesh_uuid.lower()}-central-fef9")
    state = hass.states.get(eid)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_TEMPERATURE] == COMFORT
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 20.5
    assert (
        state.attributes[ATTR_SUPPORTED_FEATURES]
        == ClimateEntityFeature.TARGET_TEMPERATURE
    )
    assert state.attributes["mesh_address"] == "FEF9"

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: eid, ATTR_TEMPERATURE: 19.0},
        blocking=True,
    )
    _, dst, access = rtr_mesh.link.sent[-1]
    op, _, params = decode_opcode(access)
    # the app's "all RTRs": an Unacknowledged Generic Level Set to 0xFEF9
    assert (dst, op) == (0xFEF9, M.GEN_LEVEL_SET_UNACK)
    assert int.from_bytes(params[:2], "little", signed=True) == CL.temperature_to_level(
        19.0
    )
    sent = len(rtr_mesh.link.sent)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: eid, ATTR_HVAC_MODE: HVACMode.HEAT},
        blocking=True,
    )
    entity = hass.data["climate"].get_entity(eid)
    await entity.async_set_temperature(
        hvac_mode=HVACMode.HEAT
    )  # no temperature: nothing to send
    assert len(rtr_mesh.link.sent) == sent

    # nothing known: no temperatures
    hub.states[RTR].properties[PID_AMBIENT] = b""
    hub.states[RTR].level = None
    assert entity.target_temperature is None
    assert entity.current_temperature is None
    hub.states[RTR].properties[PID_AMBIENT] = b"\x7f"  # the sensor's "unknown"
    assert entity.current_temperature is None


async def test_room_thermostats_set_each_set_point(
    hass: HomeAssistant, init_rtr: MockConfigEntry, rtr_mesh: RtrMesh
) -> None:
    """The app's area sheet: one Unacknowledged Generic Level Set per thermostat, not to the room address."""
    hub = hub_of(init_rtr)
    eid = entity_id(
        hass, "climate", f"{hub.cdb.mesh_uuid.lower()}-room-c010-thermostats"
    )
    state = hass.states.get(eid)
    assert state.name.endswith("All thermostats in Living room")
    assert state.attributes["mesh_address"] == "C010"
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: eid, ATTR_TEMPERATURE: 19.0},
        blocking=True,
    )
    _, dst, access = rtr_mesh.link.sent[-1]
    op, _, params = decode_opcode(access)
    assert (dst, op) == (RTR, M.GEN_LEVEL_SET_UNACK)
    assert int.from_bytes(params[:2], "little", signed=True) == CL.temperature_to_level(
        19.0
    )
