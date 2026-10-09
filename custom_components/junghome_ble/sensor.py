"""Sensors: metered-load power / voltage / current / energy / power-on time, detector illuminance, battery level, link diagnostics.

The metering socket's counters live on two elements (`docs/hidden-features.md` §2): power-on hours on the main
element, the energy counters on the meter element — `0x0072` the lifetime total nothing resets (the *Energy* sensor,
`total_increasing`, what the Energy dashboard wants), `0x006A` the total the app shows and its "reset consumption"
zeroes, `0x000D` the energy since the socket was last switched on (both diagnostic, off by default). The coordinator
polls all of them every five minutes (`hub.energy.COUNTER_READS`). The meter element also keeps the moment the
socket was commissioned (JUNG firmware property `0x5014`, `docs/hidden-features.md` §10) — the *Installed*
timestamp, read once per link through the config entities' property reader and never polled. Any other load whose
node has a meter (`jhmesh.devices.meter_element`: the energy puck 0x0010's output, a light) gets the same meter
sensors on its light device, less what the evidence gives the metering socket alone (`MeterSensorDescription.
socket_only`: voltage, current, power-on hours). Of the rest the app reads only power `0x0081`, `0x006A` and the
charts on the puck (`MeasureLampDevice`, `docs/android/properties.md` §4); `0x0072`, `0x000D` and `0x5014` are
there because the firmware lists them for it (`properties.ENERGY_HOSTS`), read on the metering socket alone. Where
its meter says it has no `0x0072`, the *Energy* sensor shows `0x006A` (`ElementState.energy_total`). All of it is
unverified on air, no puck being in the maintainer's network. So is every key's
*Key mode* (`0x5003`, diagnostic, off by default), and every load's *Schedules*: the used JH Scheduler slots
(`schedules.py`, diagnostic, off by default), and every scene member's *Scenes*: the scenes it is in, as the app's
device page lists them (`JungHomeScenes`, diagnostic, off by default); a metering socket's *Switch-on* /
*Switch-off threshold* (`thresholds.py`, diagnostic, off by default); every light's and socket's *Switches off at*,
when its last OnOff Status said it will be off (`JungHomeSwitchOffAt`, off by default). And the gateway's *IP
address* as its node reports it (`0xC002`, the address the app finds the gateway at; diagnostic), read once per
link. An entry set up from the gateway also has
what the app's gateway pages show, from the gateway's REST API (`gateway_status.py`, diagnostic, off by default):
*Firmware version*, *Firmware build*, *Serial number*, *Access requests* (waiting for approval in the app; the app's
permissions indicator), *API clients* and the *Error log* (the count of its non-debug entries, the latest ten as an
attribute), plus *Last export upload*, when Home Assistant last handed its export to the gateway (the app's
`gateway_last_sync`).

The mesh device has the mesh's health at a glance: *Unreachable devices*, how many mains nodes do
not answer and their names (`JungHomeUnreachableDevices`, on by default, the entity to alert on), and *Mesh
overview*, the reachable mains nodes with a row per node — name, area, product, reachable, last seen, signal, the
Bluetooth adapter or proxy that hears it best, hops, proxy — for a dashboard table (`JungHomeMeshOverview`,
diagnostic, at most one write a minute). Their lists are kept out of the recorder.

Detector illuminance and battery level are spec-only so far (no such device in the maintainer's network):

- illuminance is the Present Illuminance (`0x0055`) reading `binary_sensor.py` caches from the detector's Sensor
  Status, in 0.01 lx (whole lux on device software up to 1.4.0.0, `docs/cross-repo-analysis.md` §1.4); while the
  detector has delivered none, it is the detector's own *Current brightness* (vendor property 0x6004, whole lux, what
  the app's parameter page shows), read every `DETECTOR_BRIGHTNESS_POLL` seconds;
- a detector's *Continuous on/off* (0x6016, diagnostic, off by default) is what its own slider or keys set; the app
  only shows it (`control-and-state.md` §2.8), so it is read once per link and never written;
- battery products (`jhmesh.devices.BATTERY_PIDS`) sleep and answer nothing while they do, so their level is never
  polled: a `Generic Battery Get` goes out right after one of the node's keys reported an event (the node is awake
  for a moment), at most once per `BATTERY_READ_INTERVAL` once it answered (`control-and-state.md` §2.9).
  So the level is restored across restarts, and a node that reports no level byte (0xFF, which the app ignores
  anyway) shows the level its Battery Indicator flags stand for (`INDICATOR_LEVELS`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components import bluetooth
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .config_entities import (
    PROPERTY_FORCED_OFF,
    ConfigEntity,
    EntityTarget,
    PropertyEntity,
    cached_value,
    gateway_ip_targets,
    key_mode_targets,
    node_version,
    property_id_targets,
    property_reader,
)
from .const import (
    DETECTOR_PROPERTY_ILLUMINANCE,
    DOMAIN,
    KEEP_AWAKE_INTERVAL,
    LINK_STATES,
    NODE_DIAGNOSTICS_INTERVAL,
    REFRESH_RETRIES,
    SIGNAL_BATTERY,
    SIGNAL_CONNECTION,
    SIGNAL_GATEWAY_SYNCED,
    SIGNAL_LINK_STATE,
    SIGNAL_NODE,
    SIGNAL_REACHABILITY,
    SIGNAL_SCENES,
)
from .coordinator import JungHomeHub, register_status_handler
from .entity import (
    JungHomeEntity,
    async_setup_platform,
    blind_device_info,
    health_nodes,
    hub_device_info,
    light_device_info,
    load_entity_id,
    metered_device_info,
    node_device_info,
    node_label,
    product_name,
    socket_device_info,
)
from .gateway_api import GatewayConfig, GatewayHealthEntry
from .gateway_status import (
    GatewayPoll,
    GatewayPollEntity,
    as_attributes,
    error_entries,
    gateway_polls,
)
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh.advert import mac_from_uuid
from .jhmesh.devices import Blind, Light, Socket, Thermostat
from .jhmesh.properties import parse_version
from .mesh_config import gateway_sync
from .mesh_topology import node_rows
from .node_clocks import time_server
from .schedules import ScheduleTarget, schedule_targets, scheduler
from .thresholds import ThresholdTarget, switched_devices, threshold_targets

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .config_entities import ValueTarget
    from .element_state import ElementState
    from .entity import UpdateRead
    from .jhmesh.access import AccessMessage
    from .jhmesh.cdb import Node
    from .jhmesh.devices import Button, Detector, MeteredLoad
    from .jhmesh.properties import PropertySpec

_LOGGER = logging.getLogger(__name__)


DETECTOR_ILLUMINANCE_RAW_LUX_MAX_VERSION: Final = (
    1,
    4,
    0,
    0,
)  # up to this device software version the value is whole lux
# seconds between reads of a detector's own brightness (0x6004) while it has delivered no Present Illuminance (the app
# reads it every 5 s while its parameter page is open; nothing reads it otherwise)
DETECTOR_BRIGHTNESS_POLL: Final = 60.0
# Battery products (sensor.py; `control-and-state.md` §2.9). They sleep: a Generic Battery Get is only sent right after
# a key event (the node is awake for a moment), never polled.
BATTERY_READ_INTERVAL: Final = (
    21600.0  # seconds after an answered read before a key event triggers the next one
)
BATTERY_READ_TIMEOUT: Final = 3.0  # seconds to wait for the Battery Status (the app's property-read timeout); one attempt


PARALLEL_UPDATES = 0  # push-based

PROPERTY_INSTALLED = 0x5014  # `meter_timestamp`: the commissioning moment, local time, on the meter element
# wear counters of a load: how often its output switched, how often it was powered up
CYCLE_COUNTERS = {0x100F: "switching_cycles", 0x1010: "power_on_cycles"}
BRIGHTNESS_SPEC = P.PROPERTIES[
    0x6004
]  # a detector's Current brightness, lx, on the Manufacturer server


@dataclass(frozen=True, kw_only=True)
class MeterSensorDescription(SensorEntityDescription):
    """A metered load's sensor: how to read its value from the cached element state, and whether sockets alone have it.

    `socket_only`: a reading or counter the evidence ties to the metering socket (`properties.SOCKET_METERING`),
    not to its meter: voltage and current (`0x005D` / `0x005C`), the power-on hours of its main element (`0x006D`).
    """

    value_fn: Callable[[ElementState], float | None]
    socket_only: bool = False


METER_SENSORS: tuple[MeterSensorDescription, ...] = (
    MeterSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda st: st.power_w,
    ),
    MeterSensorDescription(
        key="voltage",
        translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda st: st.voltage_v,
        socket_only=True,
    ),
    MeterSensorDescription(
        key="current",
        translation_key="current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda st: st.current_a,
        socket_only=True,
    ),
    MeterSensorDescription(
        key="energy",
        translation_key="energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda st: st.energy_total,
    ),
    MeterSensorDescription(
        key="energy_resettable",
        translation_key="energy_resettable",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda st: st.energy_resettable_wh,
    ),
    MeterSensorDescription(
        key="energy_since_on",
        translation_key="energy_since_on",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_display_precision=0,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda st: st.energy_since_on_wh,
    ),
    MeterSensorDescription(
        key="power_on_time",
        translation_key="power_on_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda st: st.power_on_hours,
        socket_only=True,
    ),
)


ILLUMINANCE_SENSOR = SensorEntityDescription(
    key="illuminance",
    translation_key="illuminance",
    device_class=SensorDeviceClass.ILLUMINANCE,
    native_unit_of_measurement=LIGHT_LUX,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=0,
)
# Generic Battery Status' Battery Indicator (flags bits 2-3) as a level, for a node that sends no level byte: the
# app shows only these flags, as a medium, an empty and a critical battery icon (`control-and-state.md` §2.9)
INDICATOR_LEVELS: dict[str | int | None, int] = {
    "good": 50,
    "low": 15,
    "critically-low": 5,
}

BATTERY_SENSOR = SensorEntityDescription(
    key="battery",
    translation_key="battery",
    device_class=SensorDeviceClass.BATTERY,
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
)


@dataclass(frozen=True, kw_only=True)
class InstalledTarget(EntityTarget):
    """The commissioning timestamp of a metered load: property 0x5014 on its meter element, under the load."""

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The one property."""
        return (P.PROPERTIES[PROPERTY_INSTALLED],)

    @property
    def base_translation_key(self) -> str:
        """Always `installed`."""
        return "installed"


@dataclass(frozen=True, kw_only=True)
class CounterTarget(EntityTarget):
    """One wear counter (`CYCLE_COUNTERS`) of a load element, under the load's device."""

    property_id: int

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The one property."""
        return (P.PROPERTIES[self.property_id],)

    @property
    def base_translation_key(self) -> str:
        """The counter's name."""
        return CYCLE_COUNTERS[self.property_id]


def counter_targets(hub: JungHomeHub) -> list[CounterTarget]:
    """Return both wear counters of every light and socket load; diagnostic, off by default.

    Only the socket's counters have been read on air; the firmware lists them for every load host
    (`properties.LOAD_HOSTS`), so a light gets them too — a node without them simply never answers the read,
    which only an enabled entity makes.
    """
    loads: list[tuple[Node, int, str, DeviceInfo]] = [
        (light.node, light.address, light.unique_id, light_device_info(hub, light))
        for light in hub.devices.lights
    ]
    loads += [
        (sock.node, sock.address, sock.unique_id, socket_device_info(hub, sock))
        for sock in hub.devices.sockets
    ]
    return [
        CounterTarget(
            node=node,
            address=address,
            unique_id=f"{unique_id}-{name}",
            device_info=info,
            page="socket",
            enabled_default=False,
            property_id=pid,
        )
        for node, address, unique_id, info in loads
        if (node.pid or 0) in P.PROPERTIES[0x100F].products
        for pid, name in CYCLE_COUNTERS.items()
    ]


def installed_target(hub: JungHomeHub, load: MeteredLoad) -> InstalledTarget:
    """Return the *Installed* sensor's binding for a metered load (the puck's record: `ENERGY_HOSTS`, unverified)."""
    assert load.meter_address is not None  # only metered loads have the record
    return InstalledTarget(
        node=load.node,
        address=load.meter_address,
        unique_id=f"{load.unique_id}-installed",
        device_info=metered_device_info(hub, load),
        page="socket" if isinstance(load, Socket) else "lamp",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of `build_entities`, kept with the hub to follow a new export in place (`model_update`)."""
    hub = entry.runtime_data
    drop_forced_off_selects(
        hass, property_id_targets(hub, PROPERTY_FORCED_OFF, "forced_off")
    )
    async_setup_platform(hub, "sensor", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[SensorEntity]:
    """Return the meter sensors, detector illuminance, battery per battery node, key mode per key, the diagnostics."""
    mains = health_nodes(hub)
    entities: list[SensorEntity] = [
        JungHomeMeterSensor(hub, load, desc)
        for load in hub.devices.metered
        for desc in METER_SENSORS
        if isinstance(load, Socket) or not desc.socket_only
    ]
    entities += [
        JungHomeInstalledSensor(hub, installed_target(hub, load))
        for load in hub.devices.metered
    ]
    entities += [
        JungHomeDetectorIlluminance(hub, detector) for detector in hub.devices.detectors
    ]
    entities += [
        JungHomeForcedOff(hub, target)
        for target in property_id_targets(hub, PROPERTY_FORCED_OFF, "forced_off")
    ]
    entities += [
        JungHomeBatterySensor(hub, node, keys) for node, keys in battery_nodes(hub)
    ]
    entities += [
        JungHomeSleepMode(hub, node, keys) for node, keys in battery_nodes(hub)
    ]
    entities += [JungHomeKeyMode(hub, target) for target in key_mode_targets(hub)]
    entities += [JungHomeSchedules(hub, target) for target in schedule_targets(hub)]
    entities += scene_list_sensors(hub)
    entities += switch_off_sensors(hub)
    entities += [JungHomeThreshold(hub, target) for target in threshold_targets(hub)]
    entities += [JungHomeCounterSensor(hub, target) for target in counter_targets(hub)]
    entities += [JungHomeGatewayIp(hub, target) for target in gateway_ip_targets(hub)]
    if (polls := gateway_polls(hub.hass, hub)) is not None:
        entities += [
            JungHomeGatewaySensor(polls.config, polls.node, desc)
            for desc in GATEWAY_SENSORS
        ]
        entities.append(JungHomeGatewayErrorLog(polls.health, polls.node))
        entities.append(JungHomeGatewayLastSync(hub, polls.node))
    entities.append(JungHomeProxySensor(hub))
    entities.append(JungHomeLinkStateSensor(hub))
    entities.append(JungHomeUnreachableDevices(hub))
    entities.append(JungHomeMeshOverview(hub))
    entities += [
        JungHomeNodeDiagnostic(hub, node, desc)
        for node in hub.cdb.nodes
        if node.pid is not None
        for desc in NODE_DIAGNOSTICS
        if (desc.mains_only is False or node in mains) and desc.applies(node)
    ]
    entities += [JungHomeMeshDiagnostic(hub, desc) for desc in MESH_DIAGNOSTICS]
    return entities


def drop_forced_off_selects(hass: HomeAssistant, targets: list[ValueTarget]) -> None:
    """Remove the *Continuous on/off* selects an earlier version registered: they wrote what the detector sets itself.

    The sensor that replaces each has the select's unique id, so nothing else would ever clear the stale entry.
    """
    registry = er.async_get(hass)
    for target in targets:
        entity_id = registry.async_get_entity_id("select", DOMAIN, target.unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)


def battery_nodes(hub: JungHomeHub) -> list[tuple[Node, list[Button]]]:
    """Every node running on a battery, with the keys whose events tell that it is awake (in export order)."""
    keys: dict[int, list[Button]] = {}
    nodes: dict[int, Node] = {}
    for button in hub.devices.buttons:
        if button.battery:
            nodes.setdefault(button.node.unicast, button.node)
            keys.setdefault(button.node.unicast, []).append(button)
    return [(node, keys[unicast]) for unicast, node in nodes.items()]


def illuminance_lux(hub: JungHomeHub, node: Node, raw: bytes) -> float | None:
    """Decode a Present Illuminance value: LE 0.01 lx, whole lux on old device software, all ones = unknown."""
    if raw == b"\xff" * len(raw):
        return None
    value = int.from_bytes(raw, "little")
    version = node_version(hub, node)
    try:
        whole_lux = (
            version is not None
            and parse_version(version) <= DETECTOR_ILLUMINANCE_RAW_LUX_MAX_VERSION
        )
    except ValueError:
        whole_lux = False  # a revision we cannot read counts as unknown: current firmware's 0.01 lx
    return float(value) if whole_lux else value / 100


@register_status_handler(M.GEN_BATTERY_STATUS)
def _on_battery_status(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
    """Store a Generic Battery Status `[level][discharge u24][charge u24][flags]`: the level (0xFF = unknown) and the flags.

    A level of 101..254 is prohibited on the wire (Mesh Model §3.1.6.1); `battery_status` reads it as unknown, not
    as 200 %.
    """
    if len(p) < 8:
        return
    status = M.battery_status(p)
    st = hub.element_state(m.src)
    level = status["level"]
    st.battery = level if isinstance(level, int) else None  # percent
    hub.notify_update(m.src)
    async_dispatcher_send(
        hub.hass, SIGNAL_BATTERY.format(hub.entry.entry_id, m.src), status
    )


class JungHomeMeterSensor(JungHomeEntity, SensorEntity):
    """One measurement or counter of a metered load's meter, cached on the load's own element."""

    entity_description: MeterSensorDescription

    def __init__(
        self, hub: JungHomeHub, load: MeteredLoad, description: MeterSensorDescription
    ) -> None:
        """Bind to the load's element with `description`, under the load's device."""
        super().__init__(
            hub,
            load.address,
            f"{load.unique_id}-{description.key}",
            metered_device_info(hub, load),
        )
        self.entity_description = description
        self._load = load

    def _update_read(self) -> UpdateRead:
        """`homeassistant.update_entity` reads the load's meter now; its answers update the cache as any status does."""
        return "meter", partial(self.hub.async_refresh_meter, self._load)

    @property
    def native_value(self) -> float | None:
        """The described value from the state cache; None until the element has been heard from."""
        st = self.hub.states.get(self.address)
        return self.entity_description.value_fn(st) if st else None


class JungHomeInstalledSensor(PropertyEntity, SensorEntity):
    """When the metered load was commissioned: the meter element's 0x5014 record, read once when the link is up.

    The record is a plain local-time date and time (`properties.Timestamp7`); the two sockets that revealed it
    were installed on the days it names (October 2024). It is shown in Home Assistant's time zone.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """The cached record (the codec's ISO local time) with the instance's zone attached; None until read or when malformed."""
        value = self.property_value
        if not isinstance(value, str):
            return None
        return datetime.fromisoformat(value).replace(
            tzinfo=dt_util.get_default_time_zone()
        )


class JungHomeCounterSensor(PropertyEntity, SensorEntity):
    """A load's wear counter, read once per link: relay switching cycles, or power-ups of the device."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> int | None:
        """The cached count; None until read."""
        value = self.property_value
        return value if isinstance(value, int) else None


class JungHomeGatewayIp(PropertyEntity, SensorEntity):
    """The gateway's IP address as its node serves it (0xC002, UTF-8; the app reads it to find the gateway)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | None:
        """The address; None until read, or when the gateway reports none."""
        value = self.property_value
        return value or None


@dataclass(frozen=True, kw_only=True)
class GatewaySensorDescription(SensorEntityDescription):
    """A value of the app's gateway pages, from the gateway's `GET config`."""

    value: Callable[[GatewayConfig], str | int | None]
    clients: Callable[[GatewayConfig], tuple[str, ...]] | None = (
        None  # names behind a count
    )


GATEWAY_SENSORS: tuple[GatewaySensorDescription, ...] = (
    GatewaySensorDescription(key="firmware", value=lambda c: c.release or None),
    GatewaySensorDescription(key="firmware_build", value=lambda c: c.build or None),
    GatewaySensorDescription(key="serial", value=lambda c: c.serial or None),
    GatewaySensorDescription(
        key="access_requests",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda c: len(c.clients_asking),
        clients=lambda c: c.clients_asking,
    ),
    GatewaySensorDescription(
        key="api_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda c: len(c.api_clients),
        clients=lambda c: c.api_clients,
    ),
)
GATEWAY_ERROR_LOG_SHOWN = 10  # the latest entries of the error log kept as an attribute


class JungHomeGatewaySensor(GatewayPollEntity[GatewayConfig], SensorEntity):
    """A value of the gateway's status page (`gateway_status.py`): versions, serial, API clients and requests."""

    entity_description: GatewaySensorDescription

    def __init__(
        self,
        poll: GatewayPoll[GatewayConfig],
        node: Node,
        description: GatewaySensorDescription,
    ) -> None:
        """Bind to the gateway status poll."""
        super().__init__(poll, node, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | None:
        """The value; None until the gateway answered, or when it reports none."""
        data = self.data
        return None if data is None else self.entity_description.value(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The names behind a count of clients."""
        clients = self.entity_description.clients
        data = self.data
        if clients is None or data is None:
            return None
        return {"clients": list(clients(data))}


class JungHomeGatewayErrorLog(
    GatewayPollEntity[list[GatewayHealthEntry]], SensorEntity
):
    """The gateway's error log (`GET healthstatus`): how many entries the app shows (DEBUG hidden), the latest ones."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"entries"})

    def __init__(self, poll: GatewayPoll[list[GatewayHealthEntry]], node: Node) -> None:
        """Bind to the error-log poll."""
        super().__init__(poll, node, "error_log")

    @property
    def native_value(self) -> int | None:
        """The number of non-debug entries; None until the gateway answered."""
        data = self.data
        return None if data is None else len(error_entries(data))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The last `GATEWAY_ERROR_LOG_SHOWN` non-debug entries, in the gateway's order (none before an answer)."""
        shown = error_entries(self.data or [])[-GATEWAY_ERROR_LOG_SHOWN:]
        return {"entries": [as_attributes(e) for e in shown]}


class JungHomeGatewayLastSync(SensorEntity):
    """When Home Assistant last handed its export to the gateway (the app's `gateway_last_sync`), from its record.

    Recorded by every successful upload (`configurator.store.ExportStore.upload`) in the entry's `GatewaySync` record, which
    says so through `SIGNAL_GATEWAY_SYNCED` (no longer an `entry.data` write and an update
    listener); known without a link or an answer from the gateway, so always available.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "gateway_last_sync"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, hub: JungHomeHub, node: Node) -> None:
        """Bind to the gateway node's device."""
        self.hub = hub
        self._attr_unique_id = f"node:{node.uuid.lower()}-gateway_last_sync"
        self._attr_device_info = node_device_info(hub, node)

    async def async_added_to_hass(self) -> None:
        """Follow the record: an upload records its time there and says so."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_GATEWAY_SYNCED.format(self.hub.entry.entry_id),
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> datetime | None:
        """The time of the last upload; None before the first one."""
        stamp = gateway_sync(self.hass, self.hub.entry.entry_id).last_sync
        return dt_util.parse_datetime(stamp) if isinstance(stamp, str) else None


class JungHomeKeyMode(PropertyEntity, SensorEntity):
    """What a key or input is set to do (0x5003, the app's key modes); what it drives is on its event entity."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [m for m in P.KEY_MODE.values() if m != "unknown"]

    @property
    def native_value(self) -> str | None:
        """The mode; None until read, or for a value the app does not name."""
        value = self.property_value
        return value if value in self._attr_options else None


class JungHomeSchedules(ConfigEntity, SensorEntity):
    """How many of a load's 16 JH Scheduler slots are used, with the slots themselves as the `schedules` attribute.

    Read once per link like a config entity; `create_schedule` and the other actions update it as they write.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"schedules"})
    target: ScheduleTarget

    async def _read(self) -> bool:
        try:
            await scheduler(self.hass, self.hub).read(self.address)
        except HomeAssistantError as err:
            _LOGGER.debug("%04X: schedules not read: %s", self.address, err)
            return False
        return True

    @property
    def _slots(self) -> list[dict[str, Any]] | None:
        slots = scheduler(self.hass, self.hub).slots.get(self.address)
        return None if slots is None else [slot.as_dict() for slot in slots]

    @property
    def native_value(self) -> int | None:
        """The number of used slots; None until read."""
        slots = self._slots
        return None if slots is None else len(slots)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The mesh address and, once read, every used slot as `get_schedules` answers it."""
        out = dict(self._attr_extra_state_attributes or {})
        if (slots := self._slots) is not None:
            out["schedules"] = slots
        return out


# the models that make an element a scene member (`configurator.wiring.scene_load`): the node's Scene
# Setup Server holds the register, a channel's JUNG Scene Action Setup server its own list
SCENE_SETUP_SERVER, SCENE_ACTION_SETUP = "1204", "05271017"


def scene_list_sensors(hub: JungHomeHub) -> list[JungHomeScenes]:
    """Return the *Scenes* sensor of every load a scene can be stored on (its node has a Scene Setup Server)."""
    out: list[JungHomeScenes] = []
    for address, device in hub.devices.by_address.items():
        element = hub.cdb.element(address)
        if element is None or not (
            SCENE_SETUP_SERVER in element.models or SCENE_ACTION_SETUP in element.models
        ):
            continue
        if not any(SCENE_SETUP_SERVER in e.models for e in element.node.elements):
            continue
        if isinstance(device, Light):
            info = light_device_info(hub, device)
        elif isinstance(device, Socket):
            info = socket_device_info(hub, device)
        elif isinstance(device, Blind):
            info = blind_device_info(hub, device)
        elif isinstance(device, Thermostat):
            info = node_device_info(hub, device.node)
        else:
            continue
        unique_id = f"{element.node.uuid.lower()}-{element.location:04x}-scenes"
        out.append(JungHomeScenes(hub, address, unique_id, info))
    return out


class JungHomeScenes(JungHomeEntity, SensorEntity):
    """How many scenes a load is in, with their names as the `scenes` attribute (the app's `GetScenesForDevice`).

    From the export's scene members, narrowed on a channel with its own scene list to what that list names once
    it was read after the connection (`JungHomeHub.scenes_of`); the app's timer scenes are left out, as its lists
    leave them out. Follows the hub's re-reads of the lists and the scene actions.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "scenes"
    _unrecorded_attributes = frozenset({"scenes"})

    async def async_added_to_hass(self) -> None:
        """Also follow the hub's re-reads of the scene lists."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SCENES.format(self.hub.entry.entry_id),
                self._handle_update,
            )
        )

    @property
    def _scenes(self) -> list[str]:
        names = {s.number: s.name for s in self.hub.devices.scenes if not s.timer}
        return [names[n] for n in self.hub.scenes_of(self.address) if n in names]

    @property
    def native_value(self) -> int:
        """The number of scenes the load is in."""
        return len(self._scenes)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The mesh address and the scenes' names."""
        return {"mesh_address": f"{self.address:04X}", "scenes": self._scenes}


def switch_off_sensors(hub: JungHomeHub) -> list[JungHomeSwitchOffAt]:
    """Return the *Switches off at* sensor of every light and socket: the loads a run-on time (`0x1007`) applies to."""
    out = [
        JungHomeSwitchOffAt(
            hub,
            light.address,
            f"{light.unique_id}-off_at",
            light_device_info(hub, light),
        )
        for light in hub.devices.lights
    ]
    out += [
        JungHomeSwitchOffAt(
            hub,
            socket.address,
            f"{socket.unique_id}-off_at",
            socket_device_info(hub, socket),
        )
        for socket in hub.devices.sockets
    ]
    return out


class JungHomeSwitchOffAt(JungHomeEntity, SensorEntity):
    """When the load will be off (`ElementState.off_at`, kept by `JungHomeHub._on_onoff_status`).

    The remaining time of its last Generic OnOff Status, while it is switching off with a transition; else its
    run-on time (`0x1007`, as its *Run-on time* entity read it) from when it was seen switching on, as the firmware
    does not report the time left of a run-on time (sweep C6.4). Off by default, and unknown whenever the load is
    off, has no run-on time (unknown, or 0) or was on before Home Assistant saw it switch on. Read-only: nothing is
    written. Unverified on air: that the time shown is when the load switches off.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "switch_off_at"
    _refresh_kind = "switch"  # `homeassistant.update_entity`: Generic OnOff Get

    @property
    def native_value(self) -> datetime | None:
        """The moment the load's last OnOff Status said it will be off; None when it said nothing of the kind."""
        st = self.hub.states.get(self.address)
        return st.off_at if st else None


class JungHomeThreshold(PropertyEntity, SensorEntity):
    """A metering socket's switch-on or switch-off threshold: its power level; unknown while the socket has none.

    The duration, whether it is enabled and the loads it switches are attributes.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    target: ThresholdTarget

    @property
    def threshold(self) -> P.Threshold | None:
        """The decoded threshold; None until read."""
        value = self.property_value
        return value if isinstance(value, P.Threshold) else None

    @property
    def native_value(self) -> float | None:
        """The power level; None until read, or while no threshold is set."""
        threshold = self.threshold
        return None if threshold is None else threshold.power_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The mesh address and property; once read the duration, `enabled` and the loads switched."""
        out = dict(self._attr_extra_state_attributes or {})
        if (threshold := self.threshold) is not None:
            socket = self.hub.devices.by_address[self.address]
            assert isinstance(socket, Socket)  # thresholds are the metering socket's
            devices = self.hub.devices.by_address
            out |= {
                "duration": threshold.time_s,
                "enabled": threshold.active,
                "devices": [
                    load_entity_id(self.hass, devices[a])
                    if a in devices
                    else f"{a:04X}"
                    for a in switched_devices(self.hub, socket)
                ],
            }
        return out


def carries_sensor_value(pid: int) -> Callable[[AccessMessage], bool]:
    """Return a reply matcher for a qualified Sensor Get: a Sensor Status carrying `pid` (a malformed one carries none)."""

    def carries(m: AccessMessage) -> bool:
        try:
            return any(prop == pid for prop, _raw in M.sensor_values(m.params))
        except ValueError:
            return False

    return carries


class JungHomeDetectorIlluminance(JungHomeEntity, SensorEntity):
    """The light level a detector measures (unverified on hardware): its Present Illuminance, else its brightness property.

    The SIG value comes with the detector's Sensor Status (`binary_sensor.py`). A detector that has not delivered one
    (it does not publish, or does not answer the Sensor Get) is asked for its *Current brightness* (0x6004) every
    `DETECTOR_BRIGHTNESS_POLL` seconds instead, while the link is up; `source` says which one is shown.
    `homeassistant.update_entity` asks for both at once (`_read_now`).
    """

    entity_description = ILLUMINANCE_SENSOR

    def __init__(self, hub: JungHomeHub, detector: Detector) -> None:
        """Bind to the detector's sensor element, on its node device."""
        super().__init__(
            hub,
            detector.address,
            f"{detector.unique_id}-{ILLUMINANCE_SENSOR.key}",
            node_device_info(hub, detector.node),
        )
        self.detector = detector
        self._brightness_read: asyncio.Task[bool] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates and start the brightness poll (which asks nothing while the SIG value arrives)."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._poll_brightness,
                timedelta(seconds=DETECTOR_BRIGHTNESS_POLL),
            )
        )

    def _present_illuminance(self) -> float | None:
        st = self.hub.states.get(self.address)
        raw = st.properties.get(DETECTOR_PROPERTY_ILLUMINANCE) if st else None
        return illuminance_lux(self.hub, self.detector.node, raw) if raw else None

    def _reading(self) -> tuple[float | None, str | None]:
        """Return the value shown and where it comes from; (None, None) until the detector reported either."""
        if (lux := self._present_illuminance()) is not None:
            return lux, "present_illuminance"
        brightness = cached_value(self.hub, self.address, BRIGHTNESS_SPEC)
        if brightness is not None:
            return float(brightness), "brightness"
        return None, None

    @property
    def native_value(self) -> float | None:
        """Lux from the cached reading; None until the detector reported one (or while it says unknown)."""
        return self._reading()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The element, and which reading the value is (`present_illuminance`, `brightness`)."""
        return {
            "mesh_address": f"{self.address:04X}",
            "source": self._reading()[1],
        }

    def _update_read(self) -> UpdateRead:
        """`homeassistant.update_entity` asks for the Present Illuminance, then the brightness while there is none."""
        return "illuminance", self._read_now

    async def _read_now(self) -> None:
        """Send the qualified Sensor Get of 0x0055 (as the occupancy refresh); without a reading, read 0x6004.

        Unverified on air (no detector here). A lost link raises; `async_update` logs it.
        """
        try:
            await self.hub.proxy.request(
                self.address,
                M.sensor_get(DETECTOR_PROPERTY_ILLUMINANCE),
                M.SENSOR_STATUS,
                retries=REFRESH_RETRIES,
                match=carries_sensor_value(DETECTOR_PROPERTY_ILLUMINANCE),
            )
        except TimeoutError:
            _LOGGER.debug(
                "%04X did not answer its illuminance Sensor Get", self.address
            )
        if self._present_illuminance() is None:
            await property_reader(self.hass, self.hub).fetch(
                self.address, BRIGHTNESS_SPEC, since=time.monotonic()
            )

    @callback
    def _poll_brightness(self, _now: datetime) -> None:
        """Read 0x6004 while the link is up and no Present Illuminance is known, one read at a time."""
        if not self.hub.connected or self._present_illuminance() is not None:
            return
        if self._brightness_read is not None and not self._brightness_read.done():
            return
        self._brightness_read = self.hub.entry.async_create_background_task(
            self.hass,
            property_reader(self.hass, self.hub).read(
                self.address, BRIGHTNESS_SPEC, since=time.monotonic()
            ),
            f"{DOMAIN} detector brightness {self.address:04X}",
        )


class JungHomeForcedOff(PropertyEntity, SensorEntity):
    """A detector's *Continuous on/off* (0x6016): inactive, or its load held off / on by the detector's own controls.

    Read-only: the app reads it when a load's page opens and never writes it, though it has a Set builder
    (`docs/gap-analysis/control-and-state.md` §2.8). Unverified on air, off by default.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(P.FORCED_OFF.values())
    target: ValueTarget

    @property
    def native_value(self) -> str | None:
        """The state; None until read, or for a value the app does not name."""
        value = self.property_value
        return value if value in self._attr_options else None


class JungHomeSleepMode(JungHomeEntity, SensorEntity):
    """Whether a battery node is awake now: the app's *Power saving mode* banner (`SleepMode`) as a diagnostic sensor.

    The app keeps the state only while a battery device's page is open: its keep-alive answered (`DeviceAwake`) or
    not (`DeviceNotAwake`, "press the button to wake it", `docs/gap-analysis/control-and-state.md` §2.9). Here the
    node is `awake` while it was heard from — a key event, an answer, the keep-alive of a change
    (`keep_awake.py`) — within KEEP_AWAKE_INTERVAL, the app's keep-alive period, and `asleep` after; unknown until
    it was heard since the start. Off by default: it changes with every key press. How long a node really stays
    awake is unverified on air (no battery node here).
    """

    _attr_translation_key = "sleep_mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["awake", "asleep"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, hub: JungHomeHub, node: Node, keys: list[Button]) -> None:
        """Bind to the node's primary element, whose keep-alive answers land there, on the node device."""
        super().__init__(
            hub,
            node.unicast,
            f"{node.uuid.lower()}-sleep_mode",
            node_device_info(hub, node),
        )
        self.node = node
        self.keys = keys
        self._asleep_at: CALLBACK_TYPE | None = None  # the pending switch to `asleep`

    @property
    def native_value(self) -> str | None:
        """`awake` within KEEP_AWAKE_INTERVAL of the last message from the node, `asleep` after; None until heard."""
        heard = self.hub.last_heard.get(self.node.unicast)
        if heard is None:
            return None
        return "awake" if time.monotonic() - heard < KEEP_AWAKE_INTERVAL else "asleep"

    async def async_added_to_hass(self) -> None:
        """Listen to the node's keys too: an event of theirs means it is awake."""
        await super().async_added_to_hass()
        for address in self.listened:
            self.async_on_remove(
                self.hub.add_event_listener(address, self._on_key_event)
            )
        self.async_on_remove(self._cancel)

    @property
    def listened(self) -> tuple[int, ...]:
        """The node's keys."""
        return tuple(key.address for key in self.keys)

    @callback
    def _on_key_event(self, event: str, attrs: dict[str, Any]) -> None:
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        """Write the state, and have it turn `asleep` once the node stayed quiet for KEEP_AWAKE_INTERVAL."""
        self._cancel()
        heard = self.hub.last_heard.get(self.node.unicast)
        if (
            heard is not None
            and (left := heard + KEEP_AWAKE_INTERVAL - time.monotonic()) > 0
        ):
            self._asleep_at = async_call_later(self.hass, left, self._fell_asleep)
        super()._handle_update()

    @callback
    def _fell_asleep(self, _now: datetime) -> None:
        self._asleep_at = None
        self._handle_update()

    @callback
    def _cancel(self) -> None:
        if self._asleep_at is not None:
            self._asleep_at()
            self._asleep_at = None


class JungHomeBatterySensor(JungHomeEntity, RestoreSensor):
    """The battery level of a wall transmitter or battery puck, read right after one of its keys reported an event.

    The node sleeps otherwise and would not answer, so nothing is polled: until a key was pressed while a proxy link
    was up, the sensor shows the level it had before the restart. A read that got no answer is retried on the next
    key event; one that was answered holds for `BATTERY_READ_INTERVAL`. A Status without a level byte stands for
    the level of its Battery Indicator (`INDICATOR_LEVELS`); `level_source` says which one is shown.
    """

    entity_description = BATTERY_SENSOR

    def __init__(self, hub: JungHomeHub, node: Node, keys: list[Button]) -> None:
        """Bind to the node's primary element (its Generic Battery Server), on the node device."""
        super().__init__(
            hub,
            node.unicast,
            f"{node.uuid.lower()}-{BATTERY_SENSOR.key}",
            node_device_info(hub, node),
        )
        self.node = node
        self.keys = keys
        self._status: dict[str, int | str | None] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._answered_at: float | None = None
        self._restored: int | None = (
            None  # the level before the restart, until the node reports one
        )

    @property
    def native_value(self) -> int | None:
        """The level; None until known."""
        return self._level()[0]

    def _level(self) -> tuple[int | None, str | None]:
        """(level, where it comes from): the reported level, else the indicator's, else the restored one.

        A Status heard since the start replaces what was restored even when it tells nothing (level and indicator
        unknown): the node said so itself.
        """
        st = self.hub.states.get(self.address)
        if st is not None and st.battery is not None:
            return st.battery, "reported"
        if self._status:
            level = INDICATOR_LEVELS.get(self._status.get("indicator"))
            return level, None if level is None else "indicator"
        if self._restored is not None:
            return self._restored, "restored"
        return None, None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The element and the flags of the last Battery Status (indicator, presence, charging, serviceability)."""
        attrs: dict[str, Any] = {
            "mesh_address": f"{self.address:04X}",
            "level_source": self._level()[1],
        }
        for key in (
            "indicator",
            "presence",
            "charging",
            "serviceability",
            "discharge_minutes",
            "charge_minutes",
        ):
            attrs[key] = self._status.get(key)
        return attrs

    async def async_added_to_hass(self) -> None:
        """Restore the last level; listen to the node's keys (any event means it is awake) and its Battery Status."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float):
            self._restored = int(last.native_value)
        for address in self.listened:
            self.async_on_remove(
                self.hub.add_event_listener(address, self._on_key_event)
            )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_BATTERY.format(self.hub.entry.entry_id, self.address),
                self._on_battery_status,
            )
        )

    @property
    def listened(self) -> tuple[int, ...]:
        """The node's keys: any event of theirs means it is awake."""
        return tuple(key.address for key in self.keys)

    @callback
    def _on_battery_status(self, status: dict[str, int | str | None]) -> None:
        self._status = status
        self.async_write_ha_state()

    @callback
    def _on_key_event(self, event: str, attrs: dict[str, Any]) -> None:
        """Read the battery now that a key of the node reported something, if a read is due and none is in flight."""
        if not self.hub.connected or (
            self._read_task is not None and not self._read_task.done()
        ):
            return
        if (
            self._answered_at is not None
            and time.monotonic() - self._answered_at < BATTERY_READ_INTERVAL
        ):
            return
        self._read_task = self.hub.entry.async_create_background_task(
            self.hass, self._read(), f"{DOMAIN} battery read"
        )

    async def _read(self) -> None:
        try:
            await self.hub.proxy.request(
                self.address,
                M.generic_battery_get(),
                M.GEN_BATTERY_STATUS,
                timeout=BATTERY_READ_TIMEOUT,
                retries=1,
            )
        except TimeoutError:
            _LOGGER.debug(
                "%04X did not answer the Battery Get (asleep again?); retrying at its next key event",
                self.address,
            )
        except ConnectionError as err:
            _LOGGER.debug("Battery Get to %04X aborted: %s", self.address, err)
        else:
            self._answered_at = time.monotonic()


class JungHomeProxySensor(JungHomeEntity, SensorEntity):
    """Which node we are currently talking through (diagnostic)."""

    _attr_translation_key = "proxy_node"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to the mesh (service) device."""
        super().__init__(
            hub, 0, f"{hub.cdb.mesh_uuid.lower()}-proxy", hub_device_info(hub)
        )

    @property
    def available(self) -> bool:
        """Always available: 'no proxy' is a state worth showing."""
        return True

    @property
    def native_value(self) -> str | None:
        """The proxy node's name and address, its Bluetooth address when it is not in the CDB, None when offline."""
        if not self.hub.connected:
            return None
        node = (
            self.hub.cdb.node_by_addr(self.hub.proxy_node)
            if self.hub.proxy_node
            else None
        )
        return f"{node.name} {node.unicast:04X}" if node else self.hub.proxy_address


class JungHomeLinkStateSensor(JungHomeEntity, SensorEntity):
    """Where the link to the mesh stands (diagnostic): the app's connection states, and the screens before them.

    `bluetooth_off` (no connectable Bluetooth adapter or proxy at all, `bluetooth_unavailable`), `searching` (no
    proxy node of the mesh in range), `connecting`, `updating` (connected, the connect-time state refresh running:
    the app's "the status of your devices is being updated"), `connected`, `failed` (the last attempt failed, the
    next follows after a back-off) and `disconnected` (the link went, the next attempt is due). Follows its own
    signal (`SIGNAL_LINK_STATE`), not every entity's link signal.

    On by default: it is the mesh's visible health indicator, which *Proxy node* (a name, or
    nothing) is not. Only new registrations: one an earlier version registered disabled stays as it is.
    """

    _attr_translation_key = "link_state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(LINK_STATES)

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to the mesh (service) device."""
        super().__init__(
            hub, 0, f"{hub.cdb.mesh_uuid.lower()}-link-state", hub_device_info(hub)
        )

    @property
    def available(self) -> bool:
        """Always available: a link that is down is the state worth showing."""
        return True

    @property
    def native_value(self) -> str:
        """The hub's link state."""
        return self.hub.link_state

    async def async_added_to_hass(self) -> None:
        """Follow the link state signal only."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LINK_STATE.format(self.hub.entry.entry_id),
                self._handle_update,
            )
        )


def unreachable_nodes(hub: JungHomeHub) -> list[Node]:
    """Return the mains nodes that left a request unanswered, or stopped beating (`JungHomeHub.node_alive`)."""
    return [node for node in health_nodes(hub) if not hub.node_alive(node.unicast)]


def best_scanner(hass: HomeAssistant, mac: str | None) -> str | None:
    """Return the Bluetooth adapter or proxy hearing the node's connectable advertisements best; None if none does."""
    if mac is None:
        return None
    heard = bluetooth.async_scanner_devices_by_address(hass, mac, connectable=True)
    if not heard:
        return None
    return max(heard, key=lambda device: device.advertisement.rssi).scanner.name


def mesh_overview(hub: JungHomeHub) -> list[dict[str, Any]]:
    """One row per node of the export: how Home Assistant hears it (the *Mesh overview* sensor's `nodes`).

    The area is the node device's, else the first one among the devices that hang off it (a light's, a socket's:
    the room the app put the load in). A battery node sleeps: its `reachable` is None rather than a verdict, and it
    has no hops. Without a link no node is reachable and none is the proxy. The rows are `mesh_topology.node_rows`,
    the *Mesh topology* picture's too.
    """
    return [
        {
            "name": row.name,
            "area": row.area,
            "product": product_name(row.node.pid),
            "reachable": None if row.battery else bool(row.reachable),
            "last_seen": row.last_seen.isoformat()
            if row.last_seen is not None
            else None,
            "rssi": hub.node_rssi.get(row.node.unicast),
            "scanner": best_scanner(hub.hass, mac_from_uuid(row.node.uuid)),
            "hops": row.hops,
            "proxy": row.proxy,
        }
        for row in node_rows(hub)
    ]


class JungHomeUnreachableDevices(JungHomeEntity, SensorEntity):
    """How many mains devices do not answer, and which: the entity to alert on for single devices.

    A mains node counts while it is unreachable (a request asked with the app's full budget went unanswered) or,
    with the *Node heartbeats* option, dead (no beat for the timeout) — the nodes whose entities are unavailable for
    it (`JungHomeHub.node_alive`). Battery nodes sleep and never count. Unavailable without a link: then *Mesh
    connection* is off and nothing can be told about single devices. Pushed at each change; the names
    (`devices`) are kept out of the recorder. Unverified on air: no device has been taken off the mains under it.
    """

    _attr_translation_key = "unreachable_devices"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"devices"})

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to the mesh (service) device."""
        super().__init__(
            hub,
            0,
            f"{hub.cdb.mesh_uuid.lower()}-unreachable-devices",
            hub_device_info(hub),
        )

    @property
    def available(self) -> bool:
        """Available while the entities count as reachable (a link, or its loss grace)."""
        return self.hub.link_available

    @property
    def native_value(self) -> int:
        """The number of mains nodes that do not answer."""
        return len(unreachable_nodes(self.hub))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Their names, as their node devices show them."""
        registry = dr.async_get(self.hass)
        return {
            "devices": [
                node_label(self.hub, registry, node)
                for node in unreachable_nodes(self.hub)
            ]
        }

    async def async_added_to_hass(self) -> None:
        """Follow the link and the nodes' reachability, not every element's state."""
        for signal in (SIGNAL_CONNECTION, SIGNAL_REACHABILITY):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    signal.format(self.hub.entry.entry_id),
                    self._handle_update,
                )
            )


class JungHomeMeshOverview(JungHomeEntity, SensorEntity):
    """Every node at a glance: the reachable mains nodes, and a row per node for a dashboard table.

    The rows (`nodes`, `mesh_overview`) grow with the mesh and change with every message heard, so they are kept out
    of the recorder and written at most once per NODE_DIAGNOSTICS_INTERVAL: a change of a node's reachability is
    shown at once unless the last write was less than that ago, then when it is over; the time and signal of the
    nodes heard meanwhile on the next tick. A change of the link is shown at once, always: held back, the overview
    would show no node reachable for up to a minute after any write while the link was still connecting. Always available: without a link it shows no node
    reachable, and when each was last heard. Unverified on air, the `scanner` of each row in particular (which
    adapter or proxy Home Assistant's Bluetooth stack names for a JUNG node).
    """

    _attr_translation_key = "mesh_overview"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"nodes"})
    _written_at = float("-inf")  # the last write (`time.monotonic()`)
    _pending: CALLBACK_TYPE | None = None  # the write held back by the rate limit

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to the mesh (service) device."""
        super().__init__(
            hub, 0, f"{hub.cdb.mesh_uuid.lower()}-mesh-overview", hub_device_info(hub)
        )

    @property
    def available(self) -> bool:
        """Always available: when each node was last heard is worth showing without a link."""
        return True

    @property
    def native_value(self) -> int:
        """The number of mains nodes that answer (none without a link)."""
        if not self.hub.link_available:
            return 0
        return len(health_nodes(self.hub)) - len(unreachable_nodes(self.hub))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """A row per node (`mesh_overview`)."""
        return {"nodes": mesh_overview(self.hub)}

    async def async_added_to_hass(self) -> None:
        """Follow the link and the nodes' reachability, and look at the rest once per interval."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(self.hub.entry.entry_id),
                self._link_changed,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_REACHABILITY.format(self.hub.entry.entry_id),
                self._schedule_write,
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._tick,
                timedelta(seconds=NODE_DIAGNOSTICS_INTERVAL),
            )
        )
        self.async_on_remove(self._cancel_pending)

    @callback
    def _tick(self, _now: datetime) -> None:
        self._schedule_write()

    @callback
    def _link_changed(self) -> None:
        """Write now, with whatever was held back: the link decides whether any node is reachable at all."""
        self._cancel_pending()
        self._write()

    @callback
    def _schedule_write(self) -> None:
        """Write now, or when the interval since the last write is over (once, however often asked meanwhile)."""
        if self._pending is not None:
            return
        wait = self._written_at + NODE_DIAGNOSTICS_INTERVAL - time.monotonic()
        if wait > 0:
            self._pending = async_call_later(self.hass, wait, self._held_back)
            return
        self._write()

    @callback
    def _held_back(self, _now: datetime) -> None:
        self._pending = None
        self._write()

    @callback
    def _write(self) -> None:
        self._written_at = time.monotonic()
        self._handle_update()

    @callback
    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending()
            self._pending = None


# The mesh-level diagnostics change with every message sent or received: polled, not pushed.
SCAN_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True, kw_only=True)
class NodeDiagnosticDescription(SensorEntityDescription):
    """A link diagnostic of one node: what it shows, read off the hub."""

    value: Callable[[JungHomeHub, int], datetime | float | None]
    mains_only: bool = False  # battery nodes sleep: no heartbeats, no hops
    applies: Callable[[Node], bool] = lambda _node: True  # the nodes that get it


NODE_DIAGNOSTICS: tuple[NodeDiagnosticDescription, ...] = (
    NodeDiagnosticDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda hub, unicast: hub.last_seen.get(unicast),
    ),
    NodeDiagnosticDescription(
        key="rssi",
        translation_key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda hub, unicast: hub.node_rssi.get(unicast),
    ),
    NodeDiagnosticDescription(
        key="hops",
        translation_key="hops",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        mains_only=True,
        value=lambda hub, unicast: hub.node_hops(
            unicast
        ),  # the fewest of its recent beats
    ),
    NodeDiagnosticDescription(
        key="last_restart",
        translation_key="last_restart",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        mains_only=True,
        value=lambda hub, unicast: hub.restarted.get(unicast),
    ),
    NodeDiagnosticDescription(
        key="clock_offset",
        translation_key="clock_offset",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        mains_only=True,
        applies=lambda node: time_server(node) is not None,
        value=lambda hub, unicast: hub.clocks.offset(unicast),
    ),
)


class JungHomeNodeDiagnostic(JungHomeEntity, SensorEntity):
    """How Home Assistant hears one node: when last, how strongly, over how many relays; when it last restarted.

    *Last seen* is the time of the node's last message, *Signal strength* the RSSI of its last advertisement as
    the Bluetooth scanner heard it, *Hops* the relays its last Heartbeat took to our proxy (heartbeat option), and
    *Last restart* the last time its sequence number jumped into a fresh block (`JungHomeHub._note_seq`) — a
    mains blip, a breaker, a firmware reset — seen while Home Assistant ran. Pushed at most once a minute. *Clock
    offset* is how far the node's clock was off Home Assistant's at its last Time Status (`node_clocks.py`, off by
    default; a mains node with a Time Server), pushed as each one arrives — that one is unverified on air.
    """

    entity_description: NodeDiagnosticDescription

    def __init__(
        self, hub: JungHomeHub, node: Node, description: NodeDiagnosticDescription
    ) -> None:
        """Bind to the node device."""
        super().__init__(
            hub,
            node.unicast,
            f"{node.uuid.lower()}-{description.key}",
            node_device_info(hub, node),
        )
        self.entity_description = description

    @property
    def available(self) -> bool:
        """A time stays valid without a link; a signal or hop count only shows what the link hears now."""
        if self.entity_description.device_class is SensorDeviceClass.TIMESTAMP:
            return True
        return super().available

    @property
    def native_value(self) -> datetime | float | None:
        """The diagnostic's current value; unknown until the node was heard."""
        return self.entity_description.value(self.hub, self.address)

    async def async_added_to_hass(self) -> None:
        """Follow the node's (rate-limited) diagnostics signal and the link — not every update of its element."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE.format(self.hub.entry.entry_id, self.address),
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(self.hub.entry.entry_id),
                self._handle_update,
            )
        )


@dataclass(frozen=True, kw_only=True)
class MeshDiagnosticDescription(SensorEntityDescription):
    """A diagnostic of the whole mesh, read off the hub."""

    value: Callable[[JungHomeHub], int | float | None]
    attributes: Callable[[JungHomeHub], dict[str, Any]] = lambda _hub: {}


def _space_used(seq: int) -> float:
    return round(100 * seq / 0xFFFFFF, 2)


def _iv_state(hub: JungHomeHub) -> dict[str, Any]:
    """Return the *IV index* sensor's attributes: the IV state, and since when an IV Update Home Assistant started waits.

    `waiting_for_mesh_since` (ISO 8601, UTC): set while the mesh has not taken an IV Update Home Assistant started
    (`LocalState.mesh_iv_index` behind its index), the time it started; None otherwise. Given up 144 hours on.
    """
    state = hub.proxy.state
    started = state.iv_update_started_at
    return {
        "iv_update_active": state.iv_update_active,
        "transmit_iv_index": state.tx_iv_index,
        "waiting_for_mesh_since": dt_util.utc_from_timestamp(started).isoformat()
        if started is not None and state.mesh_iv_index != state.iv_index
        else None,
    }


def _highest_source(hub: JungHomeHub) -> dict[str, Any]:
    highest = hub.highest_seq()
    if highest is None:
        return {}
    src, _seq = highest
    node = hub.cdb.node_by_addr(src)
    return {
        "source": f"{src:04X}",
        "source_name": node.name if node is not None else None,
    }


MESH_DIAGNOSTICS: tuple[MeshDiagnosticDescription, ...] = (
    MeshDiagnosticDescription(
        key="iv_index",
        translation_key="iv_index",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda hub: hub.proxy.state.iv_index,
        attributes=_iv_state,
    ),
    MeshDiagnosticDescription(
        key="sequence_used",
        translation_key="sequence_used",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda hub: _space_used(hub.proxy.state.seq),
        attributes=lambda hub: {"source": f"{hub.proxy.state.src:04X}"},
    ),
    MeshDiagnosticDescription(
        key="mesh_sequence_used",
        translation_key="mesh_sequence_used",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda hub: (
            _space_used(highest[1])
            if (highest := hub.highest_seq()) is not None
            else None
        ),
        attributes=_highest_source,
    ),
)


class JungHomeMeshDiagnostic(JungHomeEntity, SensorEntity):
    """The mesh's IV index and how much of its sequence space is used.

    Every source stops sending at the end of the 24-bit space of an IV index; only an IV Update resets it (which
    node starts one: `Issues.check_sequence_space`). *Sequence used* is Home Assistant's own counter, *Mesh
    sequence used* the source furthest along — whose numbers the replay protection accepted — the one that decides
    when the update is due (`sequence_space_low` warns at three quarters).
    """

    _attr_should_poll = True
    entity_description: MeshDiagnosticDescription

    def __init__(
        self, hub: JungHomeHub, description: MeshDiagnosticDescription
    ) -> None:
        """Bind to the mesh (service) device."""
        super().__init__(
            hub,
            0,
            f"{hub.cdb.mesh_uuid.lower()}-{description.key}",
            hub_device_info(hub),
        )
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Known with or without a link: the counters are Home Assistant's own bookkeeping."""
        return True

    @property
    def native_value(self) -> int | float | None:
        """The diagnostic's current value."""
        return self.entity_description.value(self.hub)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Where the value comes from."""
        return self.entity_description.attributes(self.hub)
