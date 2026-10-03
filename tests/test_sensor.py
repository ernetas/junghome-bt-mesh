"""Sensor platform: socket power / voltage / current / power-on time / installed, detector illuminance, battery level
and the proxy-node diagnostic."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    LIGHT_LUX,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.junghome_ble import coordinator as hub_module
from custom_components.junghome_ble import sensor
from custom_components.junghome_ble.binary_sensor import JungHomeDetectorOccupancy
from custom_components.junghome_ble.config_entities import (
    SIG_SOFTWARE_VERSION,
    property_reader,
    retired_unique_ids,
)
from custom_components.junghome_ble.const import (
    BATTERY_READ_INTERVAL,
    DETECTOR_BRIGHTNESS_POLL,
    DETECTOR_PROPERTY_ILLUMINANCE,
    DOMAIN,
    KEEP_AWAKE_INTERVAL,
)
from custom_components.junghome_ble.coordinator import (
    NODE_VERSION_STORES,
    NODE_VERSIONS,
    ElementState,
)
from custom_components.junghome_ble.entity import (
    node_identifier,
    update_node_device,
    update_reads,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.pdu import encode_opcode
from custom_components.junghome_ble.sensor import (
    PROPERTY_INSTALLED,
    battery_nodes,
    illuminance_lux,
)

from . import property_helpers as ph
from .conftest import (
    PROXY_ADDRESS,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    LIGHT_SWITCH,
    OUR_ADDRESS,
    PROPERTY_ENERGY_SINCE_TURN_ON,
    PROPERTY_POWER_ON_TIME,
    PROPERTY_PRECISE_TOTAL_ENERGY,
    PROPERTY_TOTAL_ENERGY,
    SENSOR_CURRENT,
    SENSOR_POWER,
    SENSOR_VOLTAGE,
    SOCKET,
    SOCKET_SENSOR,
    UID_LIGHT_SWITCH,
    UID_PROXY,
    UID_SOCKET,
    admin_property_status,
    entity_id,
    onoff_status,
    sensor_status,
    vendor_button_event,
)
from .test_binary_sensor import (
    BUTTON_CLICK,
    DETECTOR_MOTION,
    DETECTOR_PRESENCE,
    GROUP_GATEWAY,
    GROUP_RELAY_MOTION,
    GROUP_SENSOR_MOTION,
    KEY_1G,
    KEY_2G_A,
    KEY_2G_B,
    RELAY_MOTION,
    TRANSMITTER_1G,
    TRANSMITTER_2G,
    UUID_1G,
    UUID_2G,
    UUID_MOTION,
    UUID_PRESENCE,
    make_detectors_entry,
    presence_status,
    start_detectors,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .property_helpers import PropertyMesh

fast_timeouts, mesh, init_with_mesh = ph.fast_timeouts, ph.mesh, ph.init_with_mesh

UID_INSTALLED = f"{UID_SOCKET}-installed"
# `7c 00 0a 03 0e 2e 2e`: [year - 1900 u16 LE][month][day][hour][minute][second] = 2024-10-03 14:46:46 local
INSTALLED_RECORD = bytes.fromhex("7c000a030e2e2e")


@pytest.fixture
async def init_detectors(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> MockConfigEntry:
    """The detectors network of tests/test_binary_sensor.py, set up and connected."""
    return await start_detectors(hass, make_detectors_entry(), fake_link)


UID_POWER, UID_VOLTAGE, UID_CURRENT = (
    f"{UID_SOCKET}-power",
    f"{UID_SOCKET}-voltage",
    f"{UID_SOCKET}-current",
)
UID_POWER_ON_TIME = f"{UID_SOCKET}-power_on_time"
UID_ENERGY, UID_ENERGY_RESETTABLE, UID_ENERGY_SINCE_ON = (
    f"{UID_SOCKET}-energy",
    f"{UID_SOCKET}-energy_resettable",
    f"{UID_SOCKET}-energy_since_on",
)


async def test_power_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    eid = entity_id(hass, "sensor", UID_POWER)
    assert eid == "sensor.kitchen_boiler_power"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.POWER
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfPower.WATT

    fake_link.inject(
        SOCKET_SENSOR,
        0xC001,
        sensor_status((SENSOR_POWER, (1234).to_bytes(2, "little"))),
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "123.4"


async def test_socket_sensor_set(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A metering socket gets its meter's readings, the three energy counters, the power-on hours, *Installed*,
    *Schedules*, *Scenes*, its two thresholds and its two wear counters; nothing else."""
    registry = er.async_get(hass)
    socket_sensors = {
        e.unique_id.rsplit("-", 1)[1]
        for e in er.async_entries_for_config_entry(registry, init_integration.entry_id)
        if e.domain == "sensor" and e.unique_id.startswith(f"{UID_SOCKET}-")
    }
    assert socket_sensors == {
        "power",
        "voltage",
        "current",
        "energy",
        "energy_resettable",
        "energy_since_on",
        "power_on_time",
        "installed",
        "schedules",
        "scenes",
        "switching_cycles",
        "power_on_cycles",
        "switch_on_threshold",
        "switch_off_threshold",
        "off_at",
    }


async def test_energy_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The lifetime counter 0x0072 of the *meter* element (Manufacturer server) is the socket's Energy sensor.

    It is a `total_increasing` energy in Wh (HA shows kWh); a status from the meter element lands on the socket,
    an all-ones value clears it.
    """
    eid = entity_id(hass, "sensor", UID_ENERGY)
    assert eid == "sensor.kitchen_boiler_energy"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENERGY
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL_INCREASING
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfEnergy.KILO_WATT_HOUR

    fake_link.inject(
        SOCKET_SENSOR,
        OUR_ADDRESS,
        admin_property_status(
            PROPERTY_PRECISE_TOTAL_ENERGY,
            (210198).to_bytes(4, "little"),
            access=1,
            opcode=M.GEN_MANU_PROP_STATUS,
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "210.198"
    fake_link.inject(
        SOCKET_SENSOR,
        OUR_ADDRESS,
        admin_property_status(
            PROPERTY_PRECISE_TOTAL_ENERGY, b"\xff" * 4, opcode=M.GEN_USER_PROP_STATUS
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_UNKNOWN


async def test_installed_sensor_is_read_once_from_the_meter_element(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    mesh: PropertyMesh,
    fast_sleep: list[float],
) -> None:
    """*Installed* asks the socket's meter element for JUNG property 0x5014 once the link is up and shows it as a timestamp.

    The record is local wall time; it is stamped with the instance's zone. It is a diagnostic entity on the socket
    device, enabled by default, never polled: one Get per link.
    """
    mesh.values[SOCKET_SENSOR, PROPERTY_INSTALLED] = INSTALLED_RECORD
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    eid = entity_id(hass, "sensor", UID_INSTALLED)
    assert eid == "sensor.kitchen_boiler_installed"
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert entry.disabled_by is None
    await wait_until(hass, lambda: (SOCKET_SENSOR, PROPERTY_INSTALLED) in mesh.gets)
    await settle(hass, 50)
    state = hass.states.get(eid)
    assert state is not None
    assert dt_util.parse_datetime(state.state) == datetime(
        2024, 10, 3, 14, 46, 46, tzinfo=dt_util.get_default_time_zone()
    )
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TIMESTAMP
    assert state.attributes["property_id"] == "0x5014"
    assert state.attributes["mesh_address"] == f"{SOCKET_SENSOR:04X}"
    assert mesh.gets.count((SOCKET_SENSOR, PROPERTY_INSTALLED)) == 1

    # a malformed record (wrong length) is not a timestamp
    mesh.link.inject(
        SOCKET_SENSOR,
        OUR_ADDRESS,
        ph.vendor_status(0x0B, PROPERTY_INSTALLED, b"\x00\x01\x02"),
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_UNKNOWN


async def test_switch_off_at_is_off_by_default_on_every_light_and_socket(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """*Switches off at* exists for every light and socket, disabled until the user enables it (unverified on air)."""
    registry = er.async_get(hass)
    entries = [
        e
        for e in er.async_entries_for_config_entry(registry, init_integration.entry_id)
        if e.domain == "sensor" and e.unique_id.endswith("-off_at")
    ]
    hub = init_integration.runtime_data
    assert len(entries) == len(hub.devices.lights) + len(hub.devices.sockets)
    for e in entries:
        assert e.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert e.entity_category is None
        assert e.translation_key == "switch_off_at"


async def test_switch_off_at_follows_the_remaining_time_of_an_onoff_status(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    freezer: FrozenDateTimeFactory,
) -> None:
    """On, heading off, with a remaining time: the moment it will be off. Anything else clears it (review-4 F4-11)."""
    uid = f"{UID_LIGHT_SWITCH}-off_at"
    er.async_get(hass).async_get_or_create("sensor", DOMAIN, uid, disabled_by=None)
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    eid = entity_id(hass, "sensor", uid)
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TIMESTAMP
    now = dt_util.utcnow().replace(microsecond=0)  # a timestamp state has whole seconds
    freezer.move_to(now)

    def status(on: bool, target: bool | None = None, remaining: int = 0) -> None:
        fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, onoff_status(on, target, remaining))

    status(True, False, M.encode_transition(90))  # 9 x 10 s: a run-on time ending
    await hass.async_block_till_done()
    assert dt_util.parse_datetime(hass.states.get(eid).state) == now + timedelta(
        seconds=90
    )

    for args in (
        (True,),  # the short form: no transition, nothing scheduled
        (True, False, 0x3F),  # remaining time unknown
        (False, True, M.encode_transition(5)),  # off, fading on
        (True, True, M.encode_transition(5)),  # on and staying on
        (True, False, 0),  # no time left
    ):
        status(True, False, M.encode_transition(90))
        await hass.async_block_till_done()
        assert hass.states.get(eid).state != STATE_UNKNOWN
        status(*args)
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == STATE_UNKNOWN, args


async def test_wear_counters_are_read_from_the_load(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    mesh: PropertyMesh,
    fast_sleep: list[float],
) -> None:
    """Review-3 F2: the socket's switching and power-on cycles (u32, read on air as 118 / 79)."""
    mesh.values[SOCKET, 0x100F] = (118).to_bytes(4, "little")
    mesh.values[SOCKET, 0x1010] = (79).to_bytes(4, "little")
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await wait_until(hass, lambda: (SOCKET, 0x1010) in mesh.gets)
    await settle(hass, 50)
    switching = hass.states.get(
        entity_id(hass, "sensor", f"{UID_SOCKET}-switching_cycles")
    )
    assert switching is not None
    assert switching.state == "118"
    assert switching.attributes["state_class"] == "total_increasing"
    power_on = hass.states.get(
        entity_id(hass, "sensor", f"{UID_SOCKET}-power_on_cycles")
    )
    assert power_on is not None
    assert power_on.state == "79"
    # a Status without a value is dropped, as the app drops it: the count stays
    mesh.link.inject(SOCKET, OUR_ADDRESS, ph.vendor_status(0x0B, 0x1010, b""))
    await hass.async_block_till_done()
    assert hass.states.get(power_on.entity_id).state == "79"
    # a value too short for the u32 is no count
    mesh.link.inject(SOCKET, OUR_ADDRESS, ph.vendor_status(0x0B, 0x1010, b"\xff"))
    await hass.async_block_till_done()
    assert hass.states.get(power_on.entity_id).state == STATE_UNKNOWN
    registry = er.async_get(hass)
    entry = registry.async_get(power_on.entity_id)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC


async def test_diagnostic_sensors_disabled_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    for uid in (
        UID_VOLTAGE,
        UID_CURRENT,
        UID_POWER_ON_TIME,
        UID_ENERGY_RESETTABLE,
        UID_ENERGY_SINCE_ON,
    ):
        eid = entity_id(hass, "sensor", uid)
        entry = registry.async_get(eid)
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert hass.states.get(eid) is None


async def test_diagnostic_sensors_when_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    registry = er.async_get(hass)
    for uid in (
        UID_VOLTAGE,
        UID_CURRENT,
        UID_POWER_ON_TIME,
        UID_ENERGY_RESETTABLE,
        UID_ENERGY_SINCE_ON,
    ):
        registry.async_get_or_create(
            "sensor", DOMAIN, uid, disabled_by=None
        )  # the user enabled them
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)

    voltage, current, hours = (
        entity_id(hass, "sensor", UID_VOLTAGE),
        entity_id(hass, "sensor", UID_CURRENT),
        entity_id(hass, "sensor", UID_POWER_ON_TIME),
    )
    assert (
        hass.states.get(hours).attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTime.HOURS
    )
    assert (
        hass.states.get(hours).attributes[ATTR_DEVICE_CLASS]
        == SensorDeviceClass.DURATION
    )
    assert (
        hass.states.get(hours).attributes[ATTR_STATE_CLASS]
        == SensorStateClass.TOTAL_INCREASING
    )
    assert hass.states.get(hours).state == STATE_UNKNOWN
    assert (
        hass.states.get(voltage).attributes[ATTR_UNIT_OF_MEASUREMENT]
        == UnitOfElectricPotential.VOLT
    )
    assert (
        hass.states.get(voltage).attributes[ATTR_DEVICE_CLASS]
        == SensorDeviceClass.VOLTAGE
    )
    assert (
        hass.states.get(current).attributes[ATTR_UNIT_OF_MEASUREMENT]
        == UnitOfElectricCurrent.AMPERE
    )
    assert (
        hass.states.get(current).attributes[ATTR_DEVICE_CLASS]
        == SensorDeviceClass.CURRENT
    )

    fake_link.inject(
        SOCKET_SENSOR,
        0xC001,
        sensor_status(
            (SENSOR_VOLTAGE, (230).to_bytes(2, "little")),
            (SENSOR_CURRENT, (537).to_bytes(2, "little")),
        ),
    )
    fake_link.inject(
        SOCKET,
        OUR_ADDRESS,
        admin_property_status(PROPERTY_POWER_ON_TIME, (4321).to_bytes(3, "little")),
    )
    # the meter element's counters: the app's resettable total (Admin server) and the energy since turn-on
    fake_link.inject(
        SOCKET_SENSOR,
        OUR_ADDRESS,
        admin_property_status(PROPERTY_TOTAL_ENERGY, (210040).to_bytes(4, "little")),
    )
    fake_link.inject(
        SOCKET_SENSOR,
        OUR_ADDRESS,
        admin_property_status(
            PROPERTY_ENERGY_SINCE_TURN_ON,
            (1009).to_bytes(4, "little"),
            access=1,
            opcode=M.GEN_MANU_PROP_STATUS,
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get(voltage).state == "230.0"
    assert hass.states.get(current).state == "5.37"
    assert hass.states.get(hours).state == "4321"
    resettable = hass.states.get(entity_id(hass, "sensor", UID_ENERGY_RESETTABLE))
    assert resettable is not None
    # the registry entries above were created bare, so the platform's suggested kWh never got stored on them and
    # the sensor shows its native Wh (a normally created entity shows kWh: `test_energy_sensor`)
    assert resettable.state == "210040"
    assert resettable.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfEnergy.WATT_HOUR
    assert resettable.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL_INCREASING
    since_on = hass.states.get(entity_id(hass, "sensor", UID_ENERGY_SINCE_ON))
    assert since_on is not None
    assert since_on.state == "1009"
    assert since_on.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfEnergy.WATT_HOUR


async def test_proxy_node_sensor(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    eid = entity_id(hass, "sensor", UID_PROXY)
    assert eid == "sensor.jung_home_mesh_test_proxy_node"
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(eid).state == "Push-button 1-gang 0148"

    power = entity_id(hass, "sensor", UID_POWER)
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert (
        hass.states.get(eid).state == STATE_UNKNOWN
    )  # still available: it tells that there is no link
    assert hass.states.get(power).state == STATE_UNAVAILABLE

    mock_bluetooth_env["infos"] = infos
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, init_integration)
    assert hass.states.get(eid).state == "Push-button 1-gang 0148"
    assert hass.states.get(power).state == STATE_UNKNOWN

    # a proxy that never answered the filter status (node unknown) is shown by its Bluetooth address
    hub = init_integration.runtime_data
    hub.proxy_node = None
    hub._set_available(True)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == PROXY_ADDRESS


# --------------------------------------------------------------------------- detectors and battery products
# On the synthetic detectors network of tests/test_binary_sensor.py; spec-only, nothing verified on hardware.


def battery_status(
    level: int | None,
    discharge: int | None = None,
    charge: int | None = None,
    flags: int = 0,
) -> bytes:
    """Generic Battery Status `[level][discharge u24][charge u24][flags]`; None = the unknown markers."""
    return (
        encode_opcode(M.GEN_BATTERY_STATUS)
        + bytes([0xFF if level is None else level])
        + (0xFFFFFF if discharge is None else discharge).to_bytes(3, "little")
        + (0xFFFFFF if charge is None else charge).to_bytes(3, "little")
        + bytes([flags])
    )


def battery_gets(fake_link: FakeProxyLink) -> list[int]:
    return [dst for _, dst, pdu in fake_link.sent if pdu == M.generic_battery_get()]


async def test_illuminance_sensor(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Lux from the detector's Present Illuminance (0.01 lx), unknown while it reports all ones."""
    eid = entity_id(hass, "sensor", f"{UUID_MOTION.lower()}-0040-illuminance")
    assert eid == "sensor.wc_motion_detector_1_m_0500_illuminance"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ILLUMINANCE
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == LIGHT_LUX
    assert state.attributes["mesh_address"] == "0501"
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is None
    assert entry.options["sensor"]["suggested_display_precision"] == 0

    fake_link.inject(
        DETECTOR_MOTION,
        GROUP_SENSOR_MOTION,
        presence_status(True, (12345).to_bytes(3, "little")),
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "123.45"

    fake_link.inject(
        DETECTOR_MOTION,
        GROUP_SENSOR_MOTION,
        sensor_status((DETECTOR_PROPERTY_ILLUMINANCE, b"\xff\xff\xff")),
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_UNKNOWN

    # the other detector's reading does not leak over
    fake_link.inject(
        DETECTOR_PRESENCE,
        OUR_ADDRESS,
        sensor_status((DETECTOR_PROPERTY_ILLUMINANCE, (250).to_bytes(3, "little"))),
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == STATE_UNKNOWN
    assert (
        hass.states.get(
            entity_id(hass, "sensor", f"{UUID_PRESENCE.lower()}-0040-illuminance")
        ).state
        == "2.5"
    )


async def test_illuminance_scaling_follows_the_device_software_version(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Up to device software 1.4.0.0 the value is whole lux; above (or unknown) it is 0.01 lx."""
    hub = init_detectors.runtime_data
    node = hub.devices.detectors[0].node
    raw = (1234).to_bytes(3, "little")
    assert illuminance_lux(hub, node, raw) == 12.34
    hub.element_state(RELAY_MOTION).properties[SIG_SOFTWARE_VERSION] = b"01040000"
    assert illuminance_lux(hub, node, raw) == 1234.0
    hub.element_state(RELAY_MOTION).properties[SIG_SOFTWARE_VERSION] = b"01040001"
    assert illuminance_lux(hub, node, raw) == 12.34
    hub.element_state(RELAY_MOTION).properties[SIG_SOFTWARE_VERSION] = b"garbage"
    assert illuminance_lux(hub, node, raw) == 12.34  # an unreadable version is none
    # MOD-01: five digit pairs decode, but are no version `parse_version` reads: current firmware's 0.01 lx
    hub.element_state(RELAY_MOTION).properties[SIG_SOFTWARE_VERSION] = b"0202000201"
    assert illuminance_lux(hub, node, raw) == 12.34
    assert illuminance_lux(hub, node, b"\xff\xff\xff") is None

    eid = entity_id(hass, "sensor", f"{UUID_MOTION.lower()}-0040-illuminance")
    hub.element_state(RELAY_MOTION).properties[SIG_SOFTWARE_VERSION] = b"01030000"
    fake_link.inject(DETECTOR_MOTION, GROUP_SENSOR_MOTION, presence_status(False, raw))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "1234.0"


PID_BRIGHTNESS, PID_FORCED_OFF = 0x6004, 0x6016
BRIGHTNESS_POLL = timedelta(seconds=DETECTOR_BRIGHTNESS_POLL + 1)


@pytest.fixture
async def detectors_with_mesh(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> AsyncGenerator[tuple[MockConfigEntry, PropertyMesh]]:
    """The detectors network with devices that answer property Gets; the motion detector reports 321 lx.

    The detectors' connect-time Sensor Gets are left out: nothing answers them here, and their full timeouts would
    hold up the property reads queued behind them.
    """
    mesh = ph.PropertyMesh(fake_link)
    mesh.values[DETECTOR_MOTION, PID_BRIGHTNESS] = (321).to_bytes(2, "little")
    with patch.object(JungHomeDetectorOccupancy, "_maybe_refresh"):
        entry = await start_detectors(hass, make_detectors_entry(), fake_link)
        await settle(hass)
        yield entry, mesh


async def test_illuminance_falls_back_to_the_brightness_property(
    hass: HomeAssistant,
    detectors_with_mesh: tuple[MockConfigEntry, PropertyMesh],
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """A detector that delivers no Present Illuminance is asked for its own brightness (0x6004) every minute."""
    entry, mesh = detectors_with_mesh
    eid = entity_id(hass, "sensor", f"{UUID_MOTION.lower()}-0040-illuminance")
    state = hass.states.get(eid)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["source"] is None
    assert (
        DETECTOR_MOTION,
        PID_BRIGHTNESS,
    ) not in mesh.gets  # not before the first poll
    later = dt_util.utcnow() + BRIGHTNESS_POLL
    async_fire_time_changed(hass, later)
    await settle(hass)
    assert mesh.gets.count((DETECTOR_MOTION, PID_BRIGHTNESS)) == 1
    assert (DETECTOR_PRESENCE, PID_BRIGHTNESS) in mesh.gets  # each detector its own
    state = hass.states.get(eid)
    assert state.state == "321.0"
    assert state.attributes["source"] == "brightness"
    # the Present Illuminance wins as soon as it arrives, and ends the poll
    fake_link.inject(
        DETECTOR_MOTION,
        GROUP_SENSOR_MOTION,
        presence_status(True, (12345).to_bytes(3, "little")),
    )
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == "123.45"
    assert state.attributes["source"] == "present_illuminance"
    async_fire_time_changed(hass, later + BRIGHTNESS_POLL)
    await settle(hass)
    assert mesh.gets.count((DETECTOR_MOTION, PID_BRIGHTNESS)) == 1
    # one read at a time: a poll while the last one is still out (unanswered: three attempts) asks nothing
    entity = hass.data["entity_components"]["sensor"].get_entity(
        entity_id(hass, "sensor", f"{UUID_PRESENCE.lower()}-0040-illuminance")
    )
    mesh.silent.add((DETECTOR_PRESENCE, PID_BRIGHTNESS))
    gets = len(mesh.gets)
    entity._poll_brightness(None)
    entity._poll_brightness(None)
    await wait_until(hass, entity._brightness_read.done)
    assert mesh.gets[gets:] == [(DETECTOR_PRESENCE, PID_BRIGHTNESS)] * 3
    # and nothing without a link
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    entity._poll_brightness(None)
    await settle(hass)
    assert mesh.gets[gets:] == [(DETECTOR_PRESENCE, PID_BRIGHTNESS)] * 3
    assert entry.runtime_data.connected is False


async def test_update_entity_reads_the_detector_illuminance_now(
    hass: HomeAssistant,
    detectors_with_mesh: tuple[MockConfigEntry, PropertyMesh],
    fake_link: FakeProxyLink,
) -> None:
    """Review-4 H4-10 (unverified on air: no detector here): `homeassistant.update_entity` sends the qualified Sensor
    Get of the Present Illuminance; a detector that answers without a reading is asked for its brightness."""
    entry, mesh = detectors_with_mesh
    hub = entry.runtime_data
    eid = entity_id(hass, "sensor", f"{UUID_MOTION.lower()}-0040-illuminance")
    replies = {
        DETECTOR_MOTION: sensor_status(
            (DETECTOR_PROPERTY_ILLUMINANCE, (12345).to_bytes(3, "little"))
        )
    }

    def sensor_server(dst: int, pdu: bytes) -> bytes | None:
        if pdu != M.sensor_get(DETECTOR_PROPERTY_ILLUMINANCE):
            return None
        return replies.get(dst)

    fake_link.app_reply = sensor_server

    async def update() -> None:
        update_reads(hub).clear()
        await hass.services.async_call(
            "homeassistant", "update_entity", {ATTR_ENTITY_ID: eid}, blocking=True
        )

    await update()
    state = hass.states.get(eid)
    assert state.state == "123.45"
    assert state.attributes["source"] == "present_illuminance"
    assert (DETECTOR_MOTION, PID_BRIGHTNESS) not in mesh.gets
    # answered without a reading (all ones): the brightness property is read instead
    replies[DETECTOR_MOTION] = sensor_status(
        (DETECTOR_PROPERTY_ILLUMINANCE, b"\xff\xff\xff")
    )
    await update()
    state = hass.states.get(eid)
    assert state.state == "321.0"
    assert state.attributes["source"] == "brightness"
    assert mesh.gets.count((DETECTOR_MOTION, PID_BRIGHTNESS)) == 1
    # silent: the cached reading stays
    with patch.object(hub.proxy, "request", side_effect=TimeoutError):
        await update()
    assert hass.states.get(eid).state == "321.0"


def test_a_malformed_sensor_status_carries_no_value() -> None:
    carries = sensor.carries_sensor_value(DETECTOR_PROPERTY_ILLUMINANCE)
    assert not carries(SimpleNamespace(params=b"\x01"))  # type: ignore[arg-type]
    assert not carries(SimpleNamespace(params=b""))  # type: ignore[arg-type]


UID_FORCED_OFF = f"{UUID_MOTION.lower()}-0040-forced_off"


async def test_forced_off_is_a_read_only_sensor_off_by_default(
    hass: HomeAssistant, detectors_with_mesh: tuple[MockConfigEntry, PropertyMesh]
) -> None:
    """Continuous on / off is set on the detector itself: shown, never written, and not read by the disabled sensor;
    the detector's relay light reads it once per link (`LoadLock`, review-4 F4-16)."""
    _, mesh = detectors_with_mesh
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id(hass, "sensor", UID_FORCED_OFF))
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert (
        registry.async_get_entity_id("select", "junghome_ble", UID_FORCED_OFF) is None
    )
    await wait_until(hass, lambda: (DETECTOR_MOTION, PID_FORCED_OFF) in mesh.gets)
    await settle(hass)
    assert mesh.gets.count((DETECTOR_MOTION, PID_FORCED_OFF)) == 1


async def test_forced_off_sensor_reads_the_detector(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    detectors_with_mesh: tuple[MockConfigEntry, PropertyMesh],
    fake_link: FakeProxyLink,
) -> None:
    _, mesh = detectors_with_mesh
    eid = entity_id(hass, "sensor", UID_FORCED_OFF)
    assert eid == "sensor.wc_motion_detector_1_m_0500_continuous_on_off"
    # read from the detector element, once the queue gets there
    await wait_until(hass, lambda: hass.states.get(eid).state != STATE_UNKNOWN)
    assert (DETECTOR_MOTION, PID_FORCED_OFF) in mesh.gets
    state = hass.states.get(eid)
    assert state.state == "inactive"
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENUM
    assert state.attributes["options"] == ["inactive", "off", "on"]
    for raw, shown in ((b"\x03", "on"), (b"\x02", "off"), (b"\x01", STATE_UNKNOWN)):
        fake_link.inject(
            DETECTOR_MOTION, OUR_ADDRESS, ph.vendor_status(0x05, PID_FORCED_OFF, raw)
        )
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == shown
    assert not [set_ for set_ in mesh.sets if set_[1] == PID_FORCED_OFF]


async def test_the_old_forced_off_select_is_removed(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """An earlier version registered a writable select under the same unique id: it goes, the sensor stays."""
    registry = er.async_get(hass)
    old = registry.async_get_or_create("select", "junghome_ble", UID_FORCED_OFF)
    await start_detectors(hass, make_detectors_entry(), fake_link)
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get_entity_id("sensor", "junghome_ble", UID_FORCED_OFF)


def version_status(version: bytes) -> bytes:
    """A Generic Manufacturer Property Status for the software version (SIG 0x001A), ASCII digit pairs."""
    return encode_opcode(M.GEN_MANU_PROP_STATUS) + b"\x1a\x00\x01" + version


async def test_detector_version_is_read_and_gates_illuminance(
    hass: HomeAssistant,
    fast_timeouts: None,  # the relay's lock read, unanswered here, may be queued before its node's version
    init_detectors: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """PLT-03: nothing used to cache SIG 0x001A, so every firmware gate was inert and an old-firmware detector's
    whole-lux reading was divided by 100. The version is asked of each detector's node once per link and cached;
    the gate then applies, and it survives the entry's next reload (config-entity setup reads it at once)."""

    def version_gets() -> list[int]:
        return [
            dst
            for _src, dst, pdu in fake_link.sent
            if pdu == M.generic_property_get("manufacturer", SIG_SOFTWARE_VERSION)
        ]

    # the motion detector's node, asked after the link came up (the queue may hold its relay's lock read first)
    await wait_until(hass, lambda: RELAY_MOTION in version_gets())
    assert (
        version_gets().count(RELAY_MOTION) == 1
    )  # once per link, however many entities the node has

    fake_link.inject(RELAY_MOTION, OUR_ADDRESS, version_status(b"01040000"))
    fake_link.inject(
        DETECTOR_MOTION,
        GROUP_SENSOR_MOTION,
        presence_status(False, (400).to_bytes(3, "little")),
    )
    await hass.async_block_till_done()
    eid = entity_id(hass, "sensor", f"{UUID_MOTION.lower()}-0040-illuminance")
    assert hass.states.get(eid).state == "400.0"

    await hass.config_entries.async_reload(init_detectors.entry_id)
    await wait_for_link(hass, init_detectors)
    hub = init_detectors.runtime_data
    assert hub.states[RELAY_MOTION].properties[SIG_SOFTWARE_VERSION] == b"01040000"


async def test_node_device_shows_the_software_version(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Review-3 F1: the version read for the firmware gates goes onto the node's device page, and a registered
    device without one yet is updated when it arrives (a malformed or unchanged one changes nothing)."""
    hub = init_detectors.runtime_data
    node = hub.cdb.node_by_addr(RELAY_MOTION)
    registry = dr.async_get(hass)
    device = registry.async_get(hub.device_ids[node_identifier(node)])
    assert device is not None
    assert device.sw_version is None
    assert device.model_id == f"0x{node.pid:04X}"
    fake_link.inject(RELAY_MOTION, OUR_ADDRESS, version_status(b"01040000"))
    await hass.async_block_till_done()
    assert registry.async_get(device.id).sw_version == "1.4.0.0"
    fake_link.inject(RELAY_MOTION, OUR_ADDRESS, version_status(b"01040001"))
    await hass.async_block_till_done()
    assert registry.async_get(device.id).sw_version == "1.4.0.1"
    update_node_device(hass, hub, node)  # nothing new: no registry write
    # not digit pairs
    fake_link.inject(RELAY_MOTION, OUR_ADDRESS, version_status(b"xx"))
    await hass.async_block_till_done()
    assert registry.async_get(device.id).sw_version == "1.4.0.1"
    hub.device_ids.pop(node_identifier(node))
    update_node_device(hass, hub, node)  # a node without a device: nothing to update


@pytest.fixture
def immediate_node_version_saves() -> Generator[None]:
    """Every save of the nodes' information is written at once (list it before the setup fixture).

    Patching the delay only after the setup would not do: a save waits for the latest one already pending, on the
    loop's own clock (`Store._async_schedule_callback_delayed_write`), so it would still take the real 10 s.
    """
    with patch.object(hub_module, "NODE_VERSIONS_SAVE_DELAY", 0):
        yield


async def test_node_versions_survive_a_restart_and_go_with_the_entry(
    hass: HomeAssistant,
    fast_timeouts: None,
    immediate_node_version_saves: None,
    init_detectors: MockConfigEntry,
    fake_link: FakeProxyLink,
    hass_storage: dict[str, Any],
) -> None:
    """A4: the versions are on disk too, so the first setup after a Home Assistant restart applies the firmware
    gates (`_candidates` runs before any Get can answer), not only the setup after a reload; removing the entry
    removes them."""
    key = f"{DOMAIN}.{init_detectors.entry_id}.node_versions"
    hub = init_detectors.runtime_data
    # the link-up read of the nodes' information is through (its last Get: the time role), and saved
    await wait_until(hass, lambda: "time_role" in hub.node_info(RELAY_MOTION))
    fake_link.inject(RELAY_MOTION, OUR_ADDRESS, version_status(b"01040000"))
    await wait_until(
        hass,
        lambda: (
            "software_version"
            in hass_storage.get(key, {}).get("data", {}).get(f"{RELAY_MOTION:04X}", {})
        ),
    )
    assert hass_storage[key]["minor_version"] == 2
    row = hass_storage[key]["data"][f"{RELAY_MOTION:04X}"]
    # the fake answers the rest without a value: not supported (under no version), kept so as not to ask again
    assert row == {
        "software_version": b"01040000".hex(),
        "time_role": "03",
        "hardware_revision.unsupported": "",
        "manufacturer_name.unsupported": "",
        "secure_element_version.unsupported": "",
        "bootloader_version.unsupported": "",
    }
    # the next link's read answers the same: nothing to write
    saved = hass_storage.pop(key)
    fake_link.inject(RELAY_MOTION, OUR_ADDRESS, version_status(b"01040000"))
    await settle(hass)  # a save (immediate here) would have been written by now
    assert key not in hass_storage
    hass_storage[key] = saved

    # a restart: nothing of this run is left in memory
    assert await hass.config_entries.async_unload(init_detectors.entry_id)
    hass.data.pop(NODE_VERSIONS)
    hass.data.pop(NODE_VERSION_STORES)
    fake_link.sent.clear()
    assert await hass.config_entries.async_setup(init_detectors.entry_id)
    hub = init_detectors.runtime_data
    assert hub.states[RELAY_MOTION].properties[SIG_SOFTWARE_VERSION] == b"01040000"
    await wait_for_link(hass, init_detectors)

    await hass.config_entries.async_remove(init_detectors.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
    assert init_detectors.entry_id not in hass.data[NODE_VERSIONS]


async def test_unreadable_node_version_rows_are_skipped(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    hass_storage: dict[str, Any],
) -> None:
    """A store of the first layout (1.1, the version alone) is migrated; rows that do not read are dropped."""
    entry = make_detectors_entry()
    hass_storage[f"{DOMAIN}.{entry.entry_id}.node_versions"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}.node_versions",
        "data": {
            f"{RELAY_MOTION:04X}": b"01040000".hex(),
            "not an address": "3031",
            "0001": "not hex",
            "0002": 7,
        },
    }
    await start_detectors(hass, entry, fake_link)
    hub = entry.runtime_data
    assert hub.states[RELAY_MOTION].properties[SIG_SOFTWARE_VERSION] == b"01040000"
    nodes = hass.data[NODE_VERSIONS][entry.entry_id]
    assert set(nodes) <= {n.unicast for n in hub.cdb.nodes}  # none of the bad rows
    assert nodes[RELAY_MOTION]["software_version"] == b"01040000"


async def test_node_information_rows_of_the_current_layout(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    hass_storage: dict[str, Any],
) -> None:
    """1.2: the items of each node by name; a node without a version has none on its element, a bad row is dropped."""
    entry = make_detectors_entry()
    hass_storage[f"{DOMAIN}.{entry.entry_id}.node_versions"] = {
        "version": 1,
        "minor_version": 2,
        "key": f"{DOMAIN}.{entry.entry_id}.node_versions",
        "data": {
            f"{RELAY_MOTION:04X}": {"hardware_revision": b"10000000".hex()},
            "0001": {"software_version": "not hex"},
            "0002": ["not", "a", "record"],
        },
    }
    await start_detectors(hass, entry, fake_link)
    hub = entry.runtime_data
    assert hub.node_info(RELAY_MOTION)["hardware_revision"] == b"10000000"
    assert set(hass.data[NODE_VERSIONS][entry.entry_id]) <= {
        n.unicast for n in hub.cdb.nodes
    }
    assert hub.node_info(0x7FFF) == {}


async def test_a_version_status_without_a_value_is_ignored(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """PLT-03: a Status with no value is how an element reports a property it does not have: nothing is cached."""
    fake_link.inject(RELAY_MOTION, OUR_ADDRESS, version_status(b""))
    await hass.async_block_till_done()
    st = init_detectors.runtime_data.states.get(RELAY_MOTION)
    assert st is None or SIG_SOFTWARE_VERSION not in st.properties


async def test_battery_sensor_is_read_after_a_key_event_only(
    hass: HomeAssistant,
    init_detectors: MockConfigEntry,
    fake_link: FakeProxyLink,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No Battery Get until a key of the node reports; then one, and none again for BATTERY_READ_INTERVAL once answered."""
    eid = entity_id(hass, "sensor", f"{UUID_1G.lower()}-battery")
    assert eid == "sensor.wall_transmitter_1_gang_0520_battery"
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.BATTERY
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == PERCENTAGE
    assert state.attributes["mesh_address"] == "0520"
    assert state.attributes["indicator"] is None
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert entry.disabled_by is None
    assert (
        battery_gets(fake_link) == []
    )  # the connect-time refresh never polls a battery

    # a gateway-mode click: the node is awake, one Get goes out; more events while it is in flight add nothing
    fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(1, BUTTON_CLICK))
    fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(2, 0x06))
    await hass.async_block_till_done()
    assert battery_gets(fake_link) == [TRANSMITTER_1G]

    fake_link.inject(TRANSMITTER_1G, OUR_ADDRESS, battery_status(87, flags=0x09))
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == "87"
    assert state.attributes["indicator"] == "good"
    assert state.attributes["presence"] == "removable"
    assert state.attributes["charging"] == "not-chargeable"
    assert state.attributes["serviceability"] == "reserved"
    assert state.attributes["discharge_minutes"] is None
    assert state.attributes["charge_minutes"] is None

    # answered: the next events within the interval do not read again, the first after it does
    fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(3, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert battery_gets(fake_link) == [TRANSMITTER_1G]
    freezer.tick(BATTERY_READ_INTERVAL + 1)
    fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(4, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert battery_gets(fake_link) == [TRANSMITTER_1G, TRANSMITTER_1G]

    # an unknown level with a low indicator: the indicator's level (the app shows only the flags), a short
    # status is ignored
    fake_link.inject(
        TRANSMITTER_1G,
        OUR_ADDRESS,
        battery_status(None, discharge=90, flags=0x04 | 0x10 | 0x40),
    )
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state == "15"
    assert state.attributes["level_source"] == "indicator"
    assert state.attributes["indicator"] == "low"
    assert state.attributes["charging"] == "not-charging"
    assert state.attributes["serviceability"] == "no-service-required"
    assert state.attributes["discharge_minutes"] == 90
    fake_link.inject(
        TRANSMITTER_1G, OUR_ADDRESS, encode_opcode(M.GEN_BATTERY_STATUS) + b"\x55"
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "15"
    assert hass.states.get(eid).attributes["indicator"] == "low"


async def test_battery_status_of_another_mesh_does_not_reach_this_entity(
    hass: HomeAssistant, init_detectors: MockConfigEntry
) -> None:
    """PLT-02: another mesh's node at the same unicast must not set this battery sensor's attributes."""
    eid = entity_id(hass, "sensor", f"{UUID_1G.lower()}-battery")
    other = SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(entry_id="other"),
        element_state=lambda _addr: ElementState(),
        notify_update=lambda _addr: None,
    )
    sensor._on_battery_status(
        other,
        SimpleNamespace(src=TRANSMITTER_1G),
        bytes([10, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0b01000000]),
    )
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.attributes["indicator"] is None
    assert state.attributes["serviceability"] is None


async def test_battery_level_above_100_is_not_a_percentage(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A level of 101..254 is prohibited on the wire (Mesh Model §3.1.6.1): no level, not 200 % (the indicator's)."""
    eid = entity_id(hass, "sensor", f"{UUID_1G.lower()}-battery")
    fake_link.inject(TRANSMITTER_1G, OUR_ADDRESS, battery_status(100, flags=0x09))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "100"
    assert hass.states.get(eid).attributes["level_source"] == "reported"
    fake_link.inject(TRANSMITTER_1G, OUR_ADDRESS, battery_status(200, flags=0x09))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "50"
    assert hass.states.get(eid).attributes["level_source"] == "indicator"
    assert hass.states.get(eid).attributes["indicator"] == "good"


async def test_battery_sensor_per_node_and_sig_key_events(
    hass: HomeAssistant, init_detectors: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A 2-gang transmitter has one battery sensor; any of its keys wakes it, SIG rocker messages included."""
    registry = er.async_get(hass)
    batteries = sorted(
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, init_detectors.entry_id)
        if e.domain == "sensor" and e.unique_id.endswith("-battery")
    )
    assert batteries == sorted(
        [f"{UUID_1G.lower()}-battery", f"{UUID_2G.lower()}-battery"]
    )
    hub = init_detectors.runtime_data
    assert [
        (node.unicast, [k.address for k in keys]) for node, keys in battery_nodes(hub)
    ] == [
        (TRANSMITTER_1G, [KEY_1G]),
        (TRANSMITTER_2G, [KEY_2G_A, KEY_2G_B]),
    ]

    eid = entity_id(hass, "sensor", f"{UUID_2G.lower()}-battery")
    fake_link.inject(
        KEY_2G_B, GROUP_RELAY_MOTION, M.generic_onoff_set(True, ack=False, tid=1)
    )
    await hass.async_block_till_done()
    assert battery_gets(fake_link) == [TRANSMITTER_2G]
    fake_link.inject(TRANSMITTER_2G, OUR_ADDRESS, battery_status(42))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "42"
    assert (
        hass.states.get(entity_id(hass, "sensor", f"{UUID_1G.lower()}-battery")).state
        == STATE_UNKNOWN
    )


async def test_battery_read_that_fails_is_retried_at_the_next_event(
    hass: HomeAssistant,
    init_detectors: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    """A node already asleep again (no answer) or a lost link leaves the level unknown; the next key event tries again."""
    hub = init_detectors.runtime_data
    eid = entity_id(hass, "sensor", f"{UUID_1G.lower()}-battery")

    def battery_requests(
        request: Any,
    ) -> int:  # the LED entities read at a key event too
        return sum(c.args[1] == M.generic_battery_get() for c in request.call_args_list)

    with patch.object(
        hub.proxy, "request", side_effect=TimeoutError("no response")
    ) as request:
        fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(1, BUTTON_CLICK))
        await hass.async_block_till_done()
        assert battery_requests(request) == 1
        fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(2, BUTTON_CLICK))
        await hass.async_block_till_done()
        assert battery_requests(request) == 2
    with patch.object(
        hub.proxy, "request", side_effect=ConnectionError("gone")
    ) as request:
        fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(3, BUTTON_CLICK))
        await hass.async_block_till_done()
        assert battery_requests(request) == 1
    assert hass.states.get(eid).state == STATE_UNKNOWN

    # no link, no read
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get(eid).state == STATE_UNAVAILABLE
    fake_link.sent.clear()
    hub.fire_button(KEY_1G, "click", {"counter": 4})
    await hass.async_block_till_done()
    assert fake_link.sent == []


async def test_battery_level_is_restored_until_the_node_reports(
    hass: HomeAssistant, mock_bluetooth_env: dict[str, Any], fake_link: FakeProxyLink, fast_sleep: list[float],
) -> None:  # fmt: skip
    """A sleeping transmitter may not report for days: the level from before the restart is shown until it does."""
    eid = "sensor.wall_transmitter_1_gang_0520_battery"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State(eid, "64"), {"native_value": 64, "native_unit_of_measurement": PERCENTAGE}),
            (
                State("sensor.wall_transmitter_2_gang_0530_battery", "unknown"),
                {"native_value": None, "native_unit_of_measurement": PERCENTAGE},
            ),
        ],
    )  # fmt: skip
    await start_detectors(hass, make_detectors_entry(), fake_link)
    assert entity_id(hass, "sensor", f"{UUID_1G.lower()}-battery") == eid
    state = hass.states.get(eid)
    assert state.state == "64"
    assert state.attributes["level_source"] == "restored"
    two_gang = entity_id(hass, "sensor", f"{UUID_2G.lower()}-battery")
    assert hass.states.get(two_gang).state == STATE_UNKNOWN
    fake_link.inject(TRANSMITTER_1G, OUR_ADDRESS, battery_status(None, flags=0x0C))
    await hass.async_block_till_done()
    # neither a level nor an indicator: unknown, the node's own word, whatever was restored
    state = hass.states.get(eid)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["level_source"] is None


# --------------------------------------------------------------------------- the app's rules (review-4 F4-16)


async def test_sleep_mode_follows_what_the_node_was_last_heard_saying(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """The app's *Power saving mode* (`SleepMode`): awake within the keep-alive period of the node's last message,
    asleep after it; unknown until it was heard. Off by default."""
    entry = await start_detectors(hass, make_detectors_entry(), fake_link)
    hub = entry.runtime_data
    uid = f"{UUID_1G.lower()}-sleep_mode"
    eid = entity_id(hass, "sensor", uid)
    registry_entry = er.async_get(hass).async_get(eid)
    assert registry_entry is not None
    assert registry_entry.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(eid).state == STATE_UNKNOWN
    assert hass.states.get(eid).attributes["options"] == ["awake", "asleep"]
    fake_link.inject(KEY_1G, GROUP_GATEWAY, vendor_button_event(1, BUTTON_CLICK))
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "awake"
    # quiet for the keep-alive period: asleep
    hub.last_heard[TRANSMITTER_1G] -= KEEP_AWAKE_INTERVAL
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=KEEP_AWAKE_INTERVAL + 1)
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "asleep"
    # an answer to the keep-alive (a property Status of its primary element) wakes it too
    fake_link.inject(
        TRANSMITTER_1G, OUR_ADDRESS, ph.vendor_status(0x05, 0x5001, b"\x01\x00")
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "awake"
    # the pending switch is dropped with the entity
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


RELAY_PRESENCE_UID = f"{UUID_PRESENCE.lower()}-0001"


@pytest.fixture
async def answering_detectors(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_timeouts: None,
) -> AsyncGenerator[tuple[MockConfigEntry, PropertyMesh]]:
    """`detectors_with_mesh`, the relays also answering their connect-time OnOff Gets (a command waits for them)."""
    mesh = ph.PropertyMesh(fake_link)
    inner = fake_link.write_gatt_char

    async def write(char: str, data: bytes, response: bool | None = None) -> None:
        before = len(fake_link.sent)
        await inner(char, data, response)
        for src, dst, access in fake_link.sent[before:]:
            if access == M.generic_onoff_get():
                fake_link.inject(dst, src, onoff_status(False))

    fake_link.write_gatt_char = write  # type: ignore[method-assign]
    with patch.object(JungHomeDetectorOccupancy, "_maybe_refresh"):
        entry = await start_detectors(hass, make_detectors_entry(), fake_link)
        await settle(hass)
        yield entry, mesh


async def test_a_detector_relay_follows_the_detectors_continuous_on_off(
    hass: HomeAssistant,
    answering_detectors: tuple[MockConfigEntry, PropertyMesh],
    fake_link: FakeProxyLink,
) -> None:
    """The app disables a detector relay's controls while the detector holds it (`ForcedOffMode`), with the
    product's instruction; the relay reads the state once per link and again before refusing."""
    entry, mesh = answering_detectors
    hub = entry.runtime_data
    reader = property_reader(hass, hub)
    light = entity_id(hass, "light", f"{UUID_MOTION.lower()}-0001")
    presence = entity_id(hass, "light", RELAY_PRESENCE_UID)
    await wait_until(
        hass,
        lambda: hass.states.get(light).attributes["continuous_on_off"] == "inactive",
    )
    assert "controlled_by" not in hass.states.get(light).attributes

    async def turn_on(eid: str) -> None:
        await hass.services.async_call(
            "light", "turn_on", {ATTR_ENTITY_ID: eid}, blocking=True
        )

    mesh.values[DETECTOR_MOTION, PID_FORCED_OFF] = b"\x03"
    fake_link.inject(
        DETECTOR_MOTION, OUR_ADDRESS, ph.vendor_status(0x05, PID_FORCED_OFF, b"\x03")
    )
    await hass.async_block_till_done()
    assert hass.states.get(light).attributes["continuous_on_off"] == "on"
    reader._read_at.pop((DETECTOR_MOTION, PID_FORCED_OFF), None)
    gets = mesh.gets.count((DETECTOR_MOTION, PID_FORCED_OFF))
    with pytest.raises(ServiceValidationError) as exc:
        await turn_on(light)
    assert (
        exc.value.translation_key == "load_forced_short"
    )  # the motion detector 0x0007: its slide switch
    assert exc.value.translation_placeholders == {"entity": light}
    assert (
        mesh.gets.count((DETECTOR_MOTION, PID_FORCED_OFF)) == gets + 1
    )  # asked again first
    # 0x0008 ends it with its ON / OFF button
    hub.cdb.node_by_addr(RELAY_MOTION).pid = 0x08
    with pytest.raises(ServiceValidationError) as exc:
        await turn_on(light)
    assert exc.value.translation_key == "load_forced_on_button"
    hub.cdb.node_by_addr(RELAY_MOTION).pid = 0x07
    # ended on the detector: the fresh read says so and the command goes out
    mesh.values[DETECTOR_MOTION, PID_FORCED_OFF] = b"\x00"
    reader._read_at.pop((DETECTOR_MOTION, PID_FORCED_OFF), None)
    fake_link.sent.clear()
    await turn_on(light)
    assert [dst for _, dst, _ in fake_link.sent][-1] == RELAY_MOTION
    assert hass.states.get(light).attributes["continuous_on_off"] == "inactive"

    # the presence detector 0x0009: its programming button
    fake_link.inject(
        DETECTOR_PRESENCE,
        OUR_ADDRESS,
        ph.vendor_status(0x05, PID_FORCED_OFF, b"\x02"),
    )
    await hass.async_block_till_done()
    mesh.values[DETECTOR_PRESENCE, PID_FORCED_OFF] = b"\x02"
    with pytest.raises(ServiceValidationError) as exc:
        await turn_on(presence)
    assert exc.value.translation_key == "load_forced_off_presence"
    # a value the app does not name is no hold
    fake_link.inject(
        DETECTOR_PRESENCE,
        OUR_ADDRESS,
        ph.vendor_status(0x05, PID_FORCED_OFF, b"\x01"),
    )
    await hass.async_block_till_done()
    assert hass.states.get(presence).attributes["continuous_on_off"] is None
    await turn_on(presence)


async def test_detector_numbers_take_the_apps_steps(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    detectors_with_mesh: tuple[MockConfigEntry, PropertyMesh],
    fake_link: FakeProxyLink,
) -> None:
    """The activation areas snap to the app's 25 % detents and the brightness threshold to its 5 lx steps; the
    threshold is unavailable in day mode (`DayModeCompatible`); area C is the presence detector's only."""
    entry, mesh = detectors_with_mesh
    motion = UUID_MOTION.lower()
    area_a = entity_id(hass, "number", f"{motion}-0040-pir_sensor_a")
    threshold = entity_id(hass, "number", f"{motion}-0040-switch_on_brightness")
    assert hass.states.get(area_a).attributes["step"] == 25
    assert hass.states.get(threshold).attributes["step"] == 5

    async def set_value(eid: str, value: float) -> None:
        await hass.services.async_call(
            "number", "set_value", {ATTR_ENTITY_ID: eid, "value": value}, blocking=True
        )

    await set_value(area_a, 60)
    await set_value(threshold, 13)
    await set_value(threshold, 1000)
    assert mesh.sets[-3:] == [
        (DETECTOR_MOTION, 0x6008, bytes([128])),  # 50 %
        (DETECTOR_MOTION, 0x600F, (15).to_bytes(2, "little")),
        (DETECTOR_MOTION, 0x600F, (1000).to_bytes(2, "little")),
    ]
    fake_link.inject(
        DETECTOR_MOTION, OUR_ADDRESS, ph.vendor_status(0x05, 0x6015, b"\x01")
    )
    await hass.async_block_till_done()
    assert hass.states.get(threshold).state == STATE_UNAVAILABLE
    fake_link.inject(
        DETECTOR_MOTION, OUR_ADDRESS, ph.vendor_status(0x05, 0x6015, b"\x00")
    )
    await hass.async_block_till_done()
    assert hass.states.get(threshold).state != STATE_UNAVAILABLE

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "number", "junghome_ble", f"{motion}-0040-pir_sensor_c"
        )
        is None
    )
    presence = UUID_PRESENCE.lower()
    assert registry.async_get_entity_id(
        "number", "junghome_ble", f"{presence}-0040-pir_sensor_c"
    )
    # an earlier version's area C of a motion detector is cleared from the registry
    assert ("number", f"{motion}-0040-pir_sensor_c") in retired_unique_ids(
        entry.runtime_data
    )
    assert ("number", f"{presence}-0040-pir_sensor_c") not in retired_unique_ids(
        entry.runtime_data
    )
