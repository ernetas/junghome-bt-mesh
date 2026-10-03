"""Binary sensors: detectors, thermostat windows, mini-actuator inputs, fault registers, blind and gateway states.

**Fault register** (`JungHomeFaultSensor`, one per mains node, diagnostic, device class *problem*): the Health Server's
registered faults. JUNG nodes register the vendor codes 0x81 (every device) and 0x80 (some) — what they mean is
unknown, the app never looks and nothing publishes them (`docs/hidden-features.md` §10) — so the hub reads the
register once per connection (`JungHomeHub._get_faults`) and the entity mirrors `ElementState.faults` of the node's
primary element. The codes are attributes; the register is cleared with `mesh_poc.py health <node> --clear`.

**Detectors.** A detector (`jhmesh.devices.Detector`: the `1001` OnOff client + `1100` Sensor Server element of a product 0x07 /
0x08 / 0x09) tells about motion in two ways (`docs/gap-analysis/control-and-state.md` §2.8, `docs/cross-repo-analysis.md`
§1.4 — neither verified on hardware yet, the maintainer's network has no detector):

- a Sensor Status carrying Presence Detected (`0x004D`, one byte) and Present Illuminance (`0x0055`), published to
  the sensor element's group when the app's "sensor values for gateway" is on, and answered to the Sensor Get this
  platform sends after every connection;
- the detector's own OnOff Set publications: it drives its relay (or a room) the way a rocker does, so every
  publication is a free motion signal. It sets the entity on, and a hold timer clears it again unless a Presence
  Detected status arrives first (that status always wins). The hold is the run-on time (0x1007) of the detector's
  relay output, the time the detector keeps its load on after the last motion; `DETECTOR_MOTION_HOLD` (the app's
  default run-on for detector loads) until that has been read.

**Mini-actuator inputs** (`JungHomeInputState`, one per input E1 / E2, disabled by default): a binary input wired to
a contact — a door or window contact, a switch — is a state, not only presses. The input sends what its key mode
sends (`docs/gap-analysis/control-and-state.md` §2.5): a Generic OnOff Set to wherever it publishes, which the hub
already turns into `press_on` / `press_off` events of the input's event entity. The entity keeps the last of those
as its state (restored across restarts: the input publishes only when it changes). With edge evaluation on
(`0x5009`, `docs/android/properties.md` §2.6) and the edges set to *Switch on* (rising) / *Switch off* (falling),
the published value is the level on the input; the review-3 plan (F10) expects the same of state mode (edge
evaluation off). Unverified on air — no mini actuator has been heard yet — hence off by default; which of on / off
means "open" depends on the contact, so the entity has no device class (Home Assistant's *Show as* sets one).

Sensor Status and OnOff Set already have handlers in the coordinator (socket meters, rocker events) and a message type
has exactly one handler, so the detector handlers are chained behind them: the previous handler runs, then the message
is looked at for detector sources. Readings are cached in `ElementState.properties` under their SIG property ids
(diagnostics; the illuminance sensor reads them there) and the entity is driven through `SIGNAL_DETECTOR`.

**Blinds** (unverified on hardware, like the cover): *Wind alarm* (safety) is on while the blind's lock function
(`0x0009`) holds a wind alarm, the lock with priority 255 the app shows with its wind badge; *Reference run*
(diagnostic, running) is `0x110D`, which reads 1 while the drive runs to its reference position. Both are
read-only vendor properties of the position element, read once per link through the config entities' reader.

**Gateway** (diagnostic): the two flags of its API status `0xC000` (bit 0 API available, bit 1 a client waiting
for approval in the app, `docs/android/properties.md` §1.9 from the gateway firmware), read once per link from the
gateway node's Manufacturer server. An entry set up from the gateway also has the indicators of the app's gateway
status page — *Network problem*, *Bluetooth mesh problem*, *Cloud problem* and *Cloud connection* — from the
gateway's REST API (`GET config`, `gateway_status.py`; off by default).

**Open window** (`JungHomeRtrWindow`, room thermostats, off by default): the firmware's drop-of-temperature state
0x1225 (`RTR_DROP_OF_TEMP_STATE`, `docs/android/properties.md` §1.5 note) — the thermostat's window-open detection.
The app declares the same state under 0x1212 and reads one byte, but never shows it; what the byte holds beyond zero /
non-zero is not documented, and the property is **unverified on air**. Read once per link like a config entity, and
taken from any Status the thermostat publishes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from .config_entities import (
    PROPERTY_LOCK,
    PROPERTY_REFERENCE_RUN,
    FlagEntity,
    LockFunctionEntity,
    PropertyEntity,
    blind_targets,
    cached_value,
    gateway_status_targets,
    property_id_targets,
    property_reader,
)
from .const import (
    DETECTOR_MOTION_HOLD,
    DETECTOR_PROPERTY_ILLUMINANCE,
    DETECTOR_PROPERTY_PRESENCE,
    DOMAIN,
    REFERENCE_RUN_LONGEST,
    REFERENCE_RUN_MARGIN,
    REFRESH_RETRIES,
    SIGNAL_DETECTOR,
)
from .coordinator import STATUS_HANDLERS, JungHomeHub, register_status_handler
from .entity import (
    JungHomeEntity,
    button_gang,
    buttons_device_info,
    health_nodes,
    node_device_info,
    node_unit_device_info,
)
from .gateway_api import GatewayConfig
from .gateway_status import GatewayPoll, GatewayPollEntity, gateway_polls
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh.devices import Button, Detector

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .config_entities import ValueTarget
    from .coordinator import StatusHandler
    from .jhmesh.cdb import Node
    from .jhmesh.client import AccessMessage

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0  # push-based

DETECTOR_PROPERTIES = frozenset(
    {DETECTOR_PROPERTY_PRESENCE, DETECTOR_PROPERTY_ILLUMINANCE}
)
EVENT_PRESENCE, EVENT_MOTION = (
    "presence",
    "motion",
)  # SIGNAL_DETECTOR events: a 0x004D status, an OnOff Set publication
SOURCE_OF = {EVENT_PRESENCE: "sensor_status", EVENT_MOTION: "onoff_set"}
# the blind parameters after whose change the app checks for a reference run: running time, inverse operation
REFERENCE_RUN_TRIGGERS = (0x1102, 0x1108)
RUN_ON_SPEC = P.PROPERTIES[0x1007]  # TimedOnDuration of the detector's relay output
# the input events (`coordinator._on_onoff_set`) that carry the value an input published
INPUT_STATES = {"press_on": True, "press_off": False}


PROPERTY_WINDOW_OPEN = 0x1225  # RTR_DROP_OF_TEMP_STATE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the detectors', thermostats', inputs', nodes', blinds' and gateways' binary sensors."""
    hub = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        JungHomeDetectorOccupancy(hub, detector) for detector in hub.devices.detectors
    ]
    entities += [
        JungHomeRtrWindow(hub, target)
        for target in property_id_targets(hub, PROPERTY_WINDOW_OPEN, "window_open")
    ]
    entities += [
        JungHomeInputState(hub, button)
        for button in hub.devices.buttons
        if button.input
    ]
    entities += [JungHomeFaultSensor(hub, node) for node in health_nodes(hub)]
    entities += [
        JungHomeWindAlarm(hub, target)
        for target in blind_targets(
            hub, PROPERTY_LOCK, "wind_alarm", enabled_default=True
        )
    ]
    entities += [
        JungHomeReferenceRun(hub, target)
        for target in blind_targets(
            hub, PROPERTY_REFERENCE_RUN, "reference_run_active", enabled_default=True
        )
    ]
    entities += [
        JungHomeGatewayStatus(hub, target) for target in gateway_status_targets(hub)
    ]
    if (polls := gateway_polls(hass, hub)) is not None:
        entities += [
            JungHomeGatewayIndicator(polls.config, polls.node, desc)
            for desc in GATEWAY_INDICATORS
        ]
    add_entities(entities)


# ----------------------------------------------------------------------------- status handlers


def chain_status_handler(
    *opcodes: int,
) -> Callable[[StatusHandler], StatusHandler]:
    """Register the decorated handler for the SIG `opcodes` *behind* the handler each opcode already has.

    `register_status_handler` keeps one handler per message type; this keeps the coordinator's (the socket meter
    reading of a Sensor Status, the rocker event of an OnOff Set) and runs the new one after it. Opcodes that
    shared a handler share the chained one too.
    """

    def register(handler: StatusHandler) -> StatusHandler:
        chained_for: dict[StatusHandler | None, StatusHandler] = {}
        for opcode in opcodes:
            previous = STATUS_HANDLERS.get((None, opcode))
            if previous not in chained_for:
                chained_for[previous] = _chained(previous, handler)
            register_status_handler(opcode)(chained_for[previous])
        return handler

    return register


def _chained(previous: StatusHandler | None, handler: StatusHandler) -> StatusHandler:
    def chained(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
        if previous is not None:
            previous(hub, m, p)
        handler(hub, m, p)

    return chained


def detector_at(hub: JungHomeHub, addr: int) -> Detector | None:
    """Return the detector whose sensor element is `addr`, if any."""
    device = hub.devices.by_address.get(addr)
    return device if isinstance(device, Detector) else None


@chain_status_handler(M.SENSOR_STATUS)
def _on_detector_sensor_status(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
    """Cache a detector's Presence Detected / Present Illuminance readings and report a presence value."""
    if detector_at(hub, m.src) is None:
        return
    try:
        values = M.sensor_values(p)
    except ValueError:
        return  # malformed: the hub's own Sensor Status handler already logged it
    st = hub.element_state(m.src)
    presence: bool | None = None
    for prop, raw in values:
        if prop in DETECTOR_PROPERTIES and raw:
            st.properties[prop] = raw
            if prop == DETECTOR_PROPERTY_PRESENCE:
                presence = bool(raw[0])
    hub.notify_update(m.src)
    if presence is not None:
        async_dispatcher_send(
            hub.hass,
            SIGNAL_DETECTOR.format(hub.entry.entry_id, m.src),
            EVENT_PRESENCE,
            presence,
        )


@chain_status_handler(M.GEN_ONOFF_SET, M.GEN_ONOFF_SET_UNACK)
def _on_detector_onoff_set(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
    """Report a detector switching its load as a motion signal (both copies the firmware sends count)."""
    if p and detector_at(hub, m.src) is not None:
        async_dispatcher_send(
            hub.hass,
            SIGNAL_DETECTOR.format(hub.entry.entry_id, m.src),
            EVENT_MOTION,
            bool(p[0]),
        )


# ----------------------------------------------------------------------------- entities


class JungHomeFaultSensor(JungHomeEntity, BinarySensorEntity):
    """The node's Health fault register: a *problem* while it holds a fault, the codes as attributes.

    Unknown until the node answered its Health Fault Get. An explicit *no fault* entry (0x00, left by a Health
    Fault Test) is not a problem. Shown under the device people look at for the node (`node_unit_device_info`),
    next to Identify.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "fault"

    def __init__(self, hub: JungHomeHub, node: Node) -> None:
        """Bind to the node's primary element, whose cached state carries the register."""
        super().__init__(
            hub,
            node.unicast,
            f"node:{node.uuid.lower()}-fault",
            node_unit_device_info(hub, node),
        )
        self.node = node

    @property
    def _faults(self) -> tuple[int, ...] | None:
        st = self.hub.states.get(self.address)
        return None if st is None else st.faults

    @property
    def is_on(self) -> bool | None:
        """Whether the register holds a fault; None until read."""
        faults = self._faults
        if faults is None:
            return None
        return any(code != 0 for code in faults)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The registered fault codes (`0x81 (vendor)`, `0x01 battery low warning`, …), or None until read."""
        faults = self._faults
        return {
            "faults": None
            if faults is None
            else [M.health_fault_name(code) for code in faults if code != 0]
        }


class JungHomeDetectorOccupancy(JungHomeEntity, BinarySensorEntity):
    """Motion (wall detectors) or occupancy (ceiling presence detector) of one detector, on its node device.

    Presence Detected statuses set the state directly; an OnOff Set publication sets it on for the relay's run-on
    time (`hold_seconds`; an OnOff Set *off* clears it at once). After every connection the detector is asked for
    its sensor values once (`Sensor Get`), so the state is filled in when the detector supports the read.
    """

    def __init__(self, hub: JungHomeHub, detector: Detector) -> None:
        """Bind to the detector's sensor element; the class and name follow the product (presence vs motion)."""
        key = "occupancy" if detector.presence else "motion"
        super().__init__(
            hub,
            detector.address,
            f"{detector.unique_id}-{key}",
            node_device_info(hub, detector.node),
        )
        self.detector = detector
        self._attr_device_class = (
            BinarySensorDeviceClass.OCCUPANCY
            if detector.presence
            else BinarySensorDeviceClass.MOTION
        )
        self._attr_translation_key = key
        self._on: bool | None = None
        self._source: str | None = None
        self._hold: Callable[[], None] | None = None
        self._refreshed = False

    @property
    def is_on(self) -> bool | None:
        """The last motion / presence signal; None until the detector has been heard from."""
        return self._on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The element, what the detector switches, and which message set the current state."""
        detector = self.detector
        return {
            "mesh_address": f"{detector.address:04X}",
            "relay": f"{detector.relay_address:04X}"
            if detector.relay_address is not None
            else None,
            "target": f"{detector.target:04X}" if detector.target is not None else None,
            "source": self._source,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to the detector events, seed the state from the cache and ask for the sensor values."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DETECTOR.format(self.hub.entry.entry_id, self.address),
                self._on_detector_event,
            )
        )
        self.async_on_remove(self._cancel_hold)
        st = self.hub.states.get(self.address)
        raw = st.properties.get(DETECTOR_PROPERTY_PRESENCE) if st else None
        if raw:
            self._on, self._source = bool(raw[0]), SOURCE_OF[EVENT_PRESENCE]
        self._maybe_refresh()

    @callback
    def _handle_update(self) -> None:
        self._maybe_refresh()
        super()._handle_update()

    @callback
    def _maybe_refresh(self) -> None:
        """Ask the detector for its sensor values once per connection (the hub's refresh only covers loads)."""
        if not self.hub.connected:
            self._refreshed = False  # the next link refreshes again
            return
        if self._refreshed:
            return
        self._refreshed = True
        # the illuminance scaling depends on the node's software version
        property_reader(self.hass, self.hub).schedule_version(self.detector.node)
        self.hub.entry.async_create_background_task(
            self.hass, self._refresh(), f"{DOMAIN} detector refresh"
        )

    async def _refresh(self) -> None:
        """One property-qualified Sensor Get per value, both in flight at once.

        The unqualified form (no property id) is the one JUNG's sensor server is known to ignore — the metering
        socket never answered it on air (`coordinator.SENSOR_READINGS`) and the thermostat is asked the qualified
        way too. Each reply is matched on its property id, so the two outstanding requests cannot take each
        other's answer.
        """
        try:
            await asyncio.gather(
                *(
                    self._get(pid)
                    for pid in (
                        DETECTOR_PROPERTY_PRESENCE,
                        DETECTOR_PROPERTY_ILLUMINANCE,
                    )
                )
            )
        except ConnectionError as err:
            _LOGGER.debug("Sensor Get to %04X aborted: %s", self.address, err)
            self._refreshed = False

    async def _get(self, pid: int) -> None:
        def carries(m: AccessMessage) -> bool:
            try:
                return any(prop == pid for prop, _raw in M.sensor_values(m.params))
            except ValueError:
                return False

        try:
            await self.hub.proxy.request(
                self.address,
                M.sensor_get(pid),
                M.SENSOR_STATUS,
                retries=REFRESH_RETRIES,
                quiet=True,
                match=carries,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer its Sensor Get %04X", self.address, pid)

    @property
    def hold_seconds(self) -> float:
        """How long a motion from an OnOff Set lasts: the relay's run-on time when read and not zero, else the default.

        The relay's *Run-on time* entity (`timed_on_duration`) reads it once per link; zero means the load stays on
        until switched off, which says nothing about how long the motion lasts.
        """
        relay = self.detector.relay_address
        run_on = None if relay is None else cached_value(self.hub, relay, RUN_ON_SPEC)
        return float(run_on) if run_on else DETECTOR_MOTION_HOLD

    @callback
    def _on_detector_event(self, event: str, value: bool) -> None:
        """Apply a presence status (authoritative) or an OnOff Set publication (on with a hold, off at once)."""
        self._cancel_hold()
        self._on = value
        self._source = SOURCE_OF[event]
        if event == EVENT_MOTION and value:
            self._hold = async_call_later(
                self.hass, self.hold_seconds, self._hold_expired
            )
        self.async_write_ha_state()

    @callback
    def _hold_expired(self, _now: datetime) -> None:
        self._hold = None
        self._on = False
        self.async_write_ha_state()

    @callback
    def _cancel_hold(self) -> None:
        if self._hold is not None:
            self._hold()
            self._hold = None


class JungHomeWindAlarm(LockFunctionEntity, BinarySensorEntity):
    """A blind held by a wind alarm: its lock function (0x0009) is a lock with the wind-alarm priority 255.

    The app's rule (`C8/a.java`, `control-and-state.md` §2.11): locked (command not 0 / 0xFF) and priority 0xFF.
    Whoever started it — the app, a rocker's locking function, the blind's *Lock function* select — it lasts until
    unlocked, and the cover refuses commands meanwhile. Read-only, one read per link (shared with the lock switch
    and select); unverified on air.
    """

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_entity_category = None

    @property
    def is_on(self) -> bool | None:
        """Whether the blind reports a wind alarm; None until read."""
        value = self.lock
        return None if value is None else value.wind_alarm


class JungHomeReferenceRun(PropertyEntity, BinarySensorEntity):
    """Whether a blind is on its reference run (0x110D reads 1 while the drive runs to its reference position).

    Read once per link, and — as the app does — again when a run should be over, the running time (`0x1102`, the
    longest the app allows while it is not known) plus `REFERENCE_RUN_MARGIN`, and after a change of the running
    time or the inverse operation (`0x1102` / `0x1108`), after which the app checks for a reference run
    (`device-settings.md` §7.2). The *Reference run* button's Set is answered with the new state. Read-only;
    unverified on air.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _follow: CALLBACK_TYPE | None = None
    _triggers: dict[int, bytes | None] | None = None

    @property
    def is_on(self) -> bool | None:
        """Running; None until read."""
        value = self.property_value
        return value if isinstance(value, bool) else None

    async def async_will_remove_from_hass(self) -> None:
        """Drop a pending read-back."""
        self._cancel_follow()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_update(self) -> None:
        """Read the state back when a run should be over, or when a parameter that may start one changed."""
        triggers = {
            pid: self.reader.cached(self.address, P.PROPERTIES[pid])
            for pid in REFERENCE_RUN_TRIGGERS
        }
        if self._triggers is not None and any(
            before is not None and triggers[pid] != before
            for pid, before in self._triggers.items()
        ):
            self._read_back()
        self._triggers = triggers
        if self.is_on:
            if self._follow is None:
                self._follow = async_call_later(
                    self.hass,
                    self._running_time() + REFERENCE_RUN_MARGIN,
                    self._run_over,
                )
        else:
            self._cancel_follow()
        super()._handle_update()

    def _running_time(self) -> float:
        value = self.value_of(P.PROPERTIES[0x1102])
        return value if isinstance(value, float) else REFERENCE_RUN_LONGEST

    @callback
    def _run_over(self, _now: datetime) -> None:
        self._follow = None
        self._read_back()

    @callback
    def _read_back(self) -> None:
        self.hub.entry.async_create_background_task(
            self.hass,
            self.reader.read(self.address, self.spec, since=time.monotonic()),
            f"{DOMAIN} reference run read-back {self.address:04X}",
        )

    @callback
    def _cancel_follow(self) -> None:
        if self._follow is not None:
            self._follow()
            self._follow = None


class JungHomeGatewayStatus(FlagEntity, BinarySensorEntity):
    """One flag of the gateway's API status (0xC000): its REST API is available, or a client awaits approval.

    A client (Home Assistant registering for a token, `config_flow.py`) waits until someone approves it in the app;
    the flag tells that the request is pending. The meaning of the bits is the gateway firmware's
    (`btmesh_property_service.js`, class b); read-only, one read per link.

    The waiting flag is off by default: the firmware sets it whenever its `api_client_name_asking` setting is not
    the empty string, and a gateway whose configuration has no such setting at all (on this installation, `GET /config`
    lists `api_client_accept` but no `api_client_name_asking`) reports it set for good (`undefined !== ""`) — on
    with no request pending, checked against the gateway's own API.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool | None:
        """The flag; None until the gateway reported its API status."""
        return self.flag


@dataclass(frozen=True, kw_only=True)
class GatewayIndicatorDescription(BinarySensorEntityDescription):
    """One of the indicators of the app's gateway status page, from the gateway's `GET config`."""

    value: Callable[[GatewayConfig], bool]


# The app's Network / Bluetooth Mesh / Cloud indicators (`GatewayConfigDTO`: `ip_error`, `btmesh_error` and
# `btmesh_device_not_available`, `cloud_error`) and whether the gateway is connected to the cloud (`cloud_connect`).
GATEWAY_INDICATORS: tuple[GatewayIndicatorDescription, ...] = (
    GatewayIndicatorDescription(
        key="network_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value=lambda c: c.ip_error,
    ),
    GatewayIndicatorDescription(
        key="mesh_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value=lambda c: c.mesh_error or c.mesh_device_missing,
    ),
    GatewayIndicatorDescription(
        key="cloud_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value=lambda c: c.cloud_error,
    ),
    GatewayIndicatorDescription(
        key="cloud_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value=lambda c: c.cloud_connected,
    ),
)


class JungHomeGatewayIndicator(GatewayPollEntity[GatewayConfig], BinarySensorEntity):
    """A Network / Bluetooth Mesh / Cloud indicator of the gateway, as its REST API reports it (`gateway_status.py`)."""

    entity_description: GatewayIndicatorDescription

    def __init__(
        self,
        poll: GatewayPoll[GatewayConfig],
        node: Node,
        description: GatewayIndicatorDescription,
    ) -> None:
        """Bind to the gateway status poll."""
        super().__init__(poll, node, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """The indicator; None until the gateway answered."""
        data = self.data
        return None if data is None else self.entity_description.value(data)


class JungHomeRtrWindow(PropertyEntity, BinarySensorEntity):
    """A room thermostat's window-open detection (0x1225, non-zero = open), on its node device; unverified on air."""

    # a state, not a setting (and a binary sensor may not be a config entity)
    _attr_entity_category = None
    _attr_device_class = BinarySensorDeviceClass.WINDOW
    target: ValueTarget

    @property
    def is_on(self) -> bool | None:
        """Whether the thermostat reports an open window; None until read."""
        value = self.property_value
        return value if isinstance(value, bool) else None


class JungHomeInputState(JungHomeEntity, BinarySensorEntity, RestoreEntity):
    """The state of a mini actuator's binary input (E1 / E2): the last On / Off it published. Unverified on air.

    Next to the input's event entity, on the same buttons device. Unknown until the input has published once (or
    a state from before the restart is restored); disabled by default (module docstring).
    """

    _attr_entity_registry_enabled_default = False

    def __init__(self, hub: JungHomeHub, button: Button) -> None:
        """Bind to the input element; a gang of one input names the entity after the device, else after the input."""
        gang = button_gang(hub, button)
        super().__init__(
            hub,
            button.address,
            f"{button.unique_id}-input_state",
            buttons_device_info(hub, gang),
        )
        self.button = button
        if len(gang) == 1:
            self._attr_translation_key = "input_state"
        else:
            self._attr_translation_key = "input_state_key"
            self._attr_translation_placeholders = {"key": button.key}
        self._attr_extra_state_attributes = {"mesh_address": f"{button.address:04X}"}
        self._on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        """The last value the input published; None until known."""
        return self._on

    async def async_added_to_hass(self) -> None:
        """Restore the last state, then follow the input's On / Off publications."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in (STATE_ON, STATE_OFF):
            self._on = last.state == STATE_ON
        self.async_on_remove(self.hub.add_event_listener(self.address, self._on_event))

    @callback
    def _on_event(self, event_type: str, attrs: dict[str, Any]) -> None:
        if (value := INPUT_STATES.get(event_type)) is None:
            return  # a scene recall, a dimming message, a gateway-mode gesture: no level
        self._on = value
        self.async_write_ha_state()
