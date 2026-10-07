"""Sockets (switchable outlets with power metering), *All sockets*, and the on/off device parameters as config switches.

A dimmer's *Use previous value* (Light Lightness Default 0: switch on at the last brightness) is one too.

The lock function (0x0009, the app's "Lock device") is a config switch per lockable load: on locks the output in
its current state against local and remote operation — for the time limit its `number` entity sets, or until
switched off. A light or socket reads its own lock once per link (`config_entities.LoadLock`, one Get shared with
this switch), and the switch reads it back when a timed lock should have ended; a lock the load publishes to its
element group (on air, when locked and on every Set it refuses, `docs/hidden-features.md` §12) is taken as it
comes. A locked socket shows `locked` and refuses commands (`LoadLock`).

The device lock (0x0001) is a config switch per flag the app offers: *Lock operation* and *Lock factory reset* on
every node, *Key lock* and *Lock configuration on the unit* on a room thermostat — off by default, since which bit
is which is not verified on a device (`JungHomeDeviceLockFlag`).

A detector's walking test is a config switch of its own (`JungHomeWalkingTest`), off by default.

A 2-gang node's *Synchronise LED colours* (`JungHomeLedColourSync`) copies LED 1's colours to LED 2; the flag is Home
Assistant's own, as it is the app's.

In a project with PP2 pucks, every mains node with a Time Server has a *Time keeper* switch (`JungHomeTimeKeeper`,
off by default): the node relays the time to the pucks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from . import const
from .config_entities import (
    LED_SYNC_PAIRS,
    PROPERTY_LOCK,
    PROPERTY_PRESENCE_CONTROL,
    PROPERTY_WALKING_TEST,
    EdgeDetectionEntity,
    FlagEntity,
    LedSyncTarget,
    LoadLock,
    LockFunctionEntity,
    NightModeTarget,
    PropertyEntity,
    PropertyTarget,
    SetupStateEntity,
    config_targets,
    device_lock_targets,
    edge_detection_targets,
    led_sync_targets,
    lock_mode,
    lock_targets,
    night_mode_targets,
    node_version,
    property_id_targets,
    property_reader,
    setup_targets,
    u16,
)
from .const import (
    DOMAIN,
    NODE_INFO_TIME_ROLE,
    PROPERTY_READ_RETRIES,
    SIGNAL_UPDATE,
)
from .entity import (
    JungHomeCentralEntity,
    JungHomeEntity,
    async_setup_platform,
    metered_device_info,
    node_device_info,
    room_loads,
    socket_device_info,
)
from .jhmesh import config_messages as C
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh.audit import Query
from .jhmesh.devices import (
    ALL_SOCKETS,
    GATEWAY_PID,
    PP2_PIDS,
    PRESENCE_DETECTOR_PID,
    TIME_SETUP_SERVER,
    Socket,
    time_keeper_candidates,
)
from .mesh_config import SENSOR_SERVER, sensor_elements, sensor_publication
from .services import async_configure

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .config_entities import ValueTarget
    from .coordinator import JungHomeHub
    from .jhmesh.cdb import Node
    from .jhmesh.properties import PropertySpec

_LOGGER = logging.getLogger(__name__)


# The app's walking test (`control-and-state.md` §2.8): it stops the test itself after five minutes and asks for the
# PIR zones (0x6005) every second while it runs.
DETECTOR_WALKING_TEST_DURATION: Final = 300.0
DETECTOR_WALKING_TEST_POLL: Final = 1.0


PARALLEL_UPDATES = 0  # push-based; commands are serialised by the mesh client itself
PRESENCE_CONTROL_SPEC = P.PROPERTIES[PROPERTY_PRESENCE_CONTROL]
PIR_SPEC = P.PROPERTIES[
    0x6005
]  # PresenceControlPir: a bit per PIR zone, on the Manufacturer server

# the app offers *Sensor values for IoT systems* from this device software on, and only with a gateway in the project
SENSOR_PUBLICATION_MIN_VERSION = (1, 3, 0, 0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of `build_entities`, kept with the hub to follow a new export in place (`model_update`)."""
    async_setup_platform(entry.runtime_data, "switch", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[SwitchEntity]:
    """Return one switch per socket, *All sockets* (home and per room), then every device's boolean parameters.

    Night mode, lock, edge mode, last value, sensor publication and walking test included.
    """
    entities: list[SwitchEntity] = [
        JungHomeSocket(hub, sock) for sock in hub.devices.sockets
    ]
    if members := hub.devices.central.get(ALL_SOCKETS):
        entities.append(JungHomeAllSockets(hub, ALL_SOCKETS, members, "sockets"))
    entities += [
        JungHomeAllSockets(hub, ALL_SOCKETS, sockets, "sockets", room)
        for room, sockets in room_loads(hub, Socket).items()
    ]
    entities += [
        JungHomePropertySwitch(hub, target)
        for target in config_targets(hub, "switch")
        if target.spec.id != PROPERTY_LOCK
    ]
    entities += [JungHomeLockSwitch(hub, target) for target in lock_targets(hub)]
    entities += [
        JungHomeDeviceLockFlag(hub, target) for target in device_lock_targets(hub)
    ]
    entities += [
        JungHomeEdgeMode(hub, target)
        for target in edge_detection_targets(hub, "switch")
    ]
    entities += [
        JungHomeLedNightMode(hub, target) for target in night_mode_targets(hub)
    ]
    entities += [JungHomeLedColourSync(hub, target) for target in led_sync_targets(hub)]
    entities += [
        JungHomeUseLastLightness(hub, target) for target in setup_targets(hub, "switch")
    ]
    entities += [
        JungHomeSensorPublication(hub, node, info)
        for node, info in sensor_publication_nodes(hub)
    ]
    entities += [
        JungHomeWalkingTest(hub, target)
        for target in property_id_targets(hub, PROPERTY_WALKING_TEST, "walking_test")
    ]
    if any(n.pid in PP2_PIDS for n in hub.cdb.nodes):
        entities += [
            JungHomeTimeKeeper(hub, node) for node in time_keeper_candidates(hub.cdb)
        ]
    return entities


def sensor_publication_nodes(hub: JungHomeHub) -> list[tuple[Node, DeviceInfo]]:
    """Return every node the app offers *Sensor values for IoT systems* for, with the device it shows under.

    A node with a Sensor Server, running device software 1.3.0.0 or later (or not known yet), in a project with a
    gateway (`PROV/SensorValuesParameterProvider.java`); a metered load's switch sits on the load (a socket, an energy
    puck's output: the app lists it among the load's parameters, `device-settings.md` §4.1 / §5.1), others on the node.
    """
    if not any(n.pid == GATEWAY_PID for n in hub.cdb.nodes):
        return []
    out: list[tuple[Node, DeviceInfo]] = []
    for node in hub.cdb.nodes:
        if not sensor_elements(node):
            continue
        version = node_version(hub, node)
        try:
            if version and P.parse_version(version) < SENSOR_PUBLICATION_MIN_VERSION:
                continue
        except ValueError:
            pass  # a version that does not parse: unknown, as the property catalogue treats it
        load = next((m for m in hub.devices.metered if m.node is node), None)
        out.append(
            (
                node,
                metered_device_info(hub, load)
                if load is not None
                else node_device_info(hub, node),
            )
        )
    return out


class JungHomeSocket(LoadLock, SwitchEntity):
    """A switched socket; refused while locked (`LoadLock`)."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_name = None  # the device *is* the socket
    _refresh_kind = "switch"  # `homeassistant.update_entity`: Generic OnOff Get

    def __init__(self, hub: JungHomeHub, socket: Socket) -> None:
        """Bind to `socket`."""
        super().__init__(
            hub, socket.address, socket.unique_id, socket_device_info(hub, socket)
        )
        self._attr_extra_state_attributes = {
            "mesh_address": f"{socket.address:04X}",
            "rooms": socket.rooms,
        }

    @property
    def is_on(self) -> bool | None:
        """On/off from the state cache; None until the element has been heard from."""
        st = self.hub.states.get(self.address)
        return st.on if st else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send Generic OnOff Set on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send Generic OnOff Set off."""
        await self._set(False)

    async def _set(self, on: bool) -> None:
        await self._check_unlocked()
        await self._send_switch(self.hub.set_onoff(self.address, on), on)


class JungHomePropertySwitch(PropertyEntity, SwitchEntity):
    """A boolean property; the key status LED is written the gateway's way and has no state to read (assumed)."""

    def __init__(self, hub: JungHomeHub, target: PropertyTarget) -> None:
        """Bind to `target`."""
        super().__init__(hub, target)
        self._attr_assumed_state = target.description.write == "status"

    @property
    def is_on(self) -> bool | None:
        """The decoded value; None until the element reported it."""
        value = self.property_value
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write 1."""
        await self.async_write_value(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Write 0."""
        await self.async_write_value(False)


class JungHomeLedNightMode(PropertyEntity, SwitchEntity):
    """The app's "Night mode": the LEDs light for 5 s after a key press only.

    On = every LED mode property of the node has the night-mode byte set (the app shows the AND too); switching
    rewrites each one with its current colour, so the colours must be known — they are read first when not. An LED
    already in the asked mode is not written: the node lights its LEDs at every write, so an automation asserting the
    mode again would make them flash each time. The cache follows the node (read at every link, every Status heard,
    the read-back of each write); `homeassistant.update_entity` reads it again.
    """

    target: NightModeTarget

    @property
    def is_on(self) -> bool | None:
        """AND of the night-mode flags; None until every LED mode is known."""
        modes = [self.value_of(spec) for spec in self.specs]
        if not all(isinstance(m, P.LedMode) for m in modes):
            return None
        return all(m.night_mode for m in modes)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set the night-mode byte on every LED, colours unchanged."""
        await self._set(night_mode=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear the night-mode byte on every LED, colours unchanged."""
        await self._set(night_mode=False)

    async def _set(self, *, night_mode: bool) -> None:
        """Rewrite every LED not in the mode with its colour; nothing is written unless every colour is known."""
        # the colour selects rewrite the same values: neither may start from what the other is about to change
        async with self.changing():
            colours: list[tuple[PropertySpec, P.LedMode]] = []
            for spec in self.specs:
                current = self.value_of(spec)
                if not isinstance(current, P.LedMode):
                    await self.read_current(spec)
                    current = self.value_of(spec)
                if not isinstance(current, P.LedMode):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="led_colour_unknown",
                        translation_placeholders={"entity": self.entity_id},
                    )
                colours.append((spec, current))
            for spec, current in colours:
                if current.night_mode == night_mode:
                    continue
                await self.async_write_value(
                    P.LedMode(*current.rgb, night_mode=night_mode), spec
                )


class JungHomeLedColourSync(PropertyEntity, SwitchEntity, RestoreEntity):
    """The app's *Synchronise buttons* on a 2-gang node: LED 2 shows LED 1's on / off colours.

    Switching it on writes LED 1's current values to LED 1 and LED 2 in the app's order — 0xA001, 0xA004, 0xA002,
    0xA005, each value as read (on air); switching it off writes nothing. While on, a colour
    chosen for LED 1 is copied to LED 2 and LED 2's selects are unavailable (`select.JungHomeLedColour`). The node
    has no such setting: the app keeps the flag itself (`UpdateSyncLed`), Home Assistant as this entity's restored
    state.
    """

    target: LedSyncTarget

    async def async_added_to_hass(self) -> None:
        """Take the flag over from the last state, then tell the colour selects."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            self._set_flag(on=True)

    @property
    def is_on(self) -> bool:
        """Whether LED 1's colours are copied to LED 2."""
        return self.reader.led_sync.get(self.address, False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write LED 1's colours to both LEDs, then keep them together."""
        # the colour selects and the night mode rewrite the same values
        async with self.changing():
            values: dict[int, P.LedMode] = {}
            for led1 in LED_SYNC_PAIRS:
                spec = P.PROPERTIES[led1]
                current = self.value_of(spec)
                if not isinstance(current, P.LedMode):
                    await self.read_current(spec)
                    current = self.value_of(spec)
                if not isinstance(current, P.LedMode):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="led_colour_unknown",
                        translation_placeholders={"entity": self.entity_id},
                    )
                values[led1] = current
            # nothing is read for the flag, but the colours are: a read-back showing another one is an error, as
            # for the colour selects, and the flag stays off
            for led1, led2 in LED_SYNC_PAIRS.items():
                for pid in (led1, led2):
                    await self.async_write_value(
                        values[led1], P.PROPERTIES[pid], compare=True
                    )
        self._set_flag(on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Let the LEDs have colours of their own again; nothing is sent."""
        self._set_flag(on=False)

    @callback
    def _set_flag(self, *, on: bool) -> None:
        """Keep the flag where the colour selects of the element read it, and have them (and this entity) redrawn."""
        self.reader.led_sync[self.address] = on
        async_dispatcher_send(
            self.hass, SIGNAL_UPDATE.format(self.hub.entry.entry_id, self.address)
        )


class JungHomeLockSwitch(LockFunctionEntity, SwitchEntity):
    """The lock function of a load: on = locked (`02 01 <time>`), off = unlocked with the fields read (the app's)."""

    @property
    def is_on(self) -> bool | None:
        """Locked; None until the element reported its lock state."""
        value = self.lock
        return None if value is None else value.locked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The address and property, plus the kind of lock and its time limit (s, 0 = none) while locked."""
        attrs = dict(self._attr_extra_state_attributes)
        value = self.lock
        if value is not None and value.locked:
            attrs["lock_mode"] = lock_mode(value)
            attrs["lock_time_limit"] = value.time_s
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock the current state, for the time limit set on the load (none by default)."""
        await self.async_lock(
            P.lock_output(self.reader.lock_time_limits.get(self.address, 0))
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unlock, as the app does."""
        await self.async_unlock()


class JungHomeDeviceLockFlag(FlagEntity, SwitchEntity):
    """One flag of a node's device lock (0x0001): *Lock operation*, *Lock factory reset*, *Key lock*, *Lock configuration*.

    The app's encoder and decoder of the 16-bit word disagree; the flags follow the encoder (bit 1 factory reset with
    time limit, bit 2 lock operation, bit 3 key lock, bit 4 configuration lock), which the gateway's value map
    corroborates (`docs/android/properties.md` §1.2 and §5 q. 1, `docs/gap-analysis/device-settings.md` §1.3) and the
    app's own Sets confirmed on air (the settings session: *Lock operation* wrote 0x0004, *Lock factory reset*
    0x0002). *Lock operation* is therefore enabled by default, as in the app's normal list; the thermostat's bits 3
    and 4 have not been seen on air (`properties.targets.DEVICE_LOCK_ENABLED`).

    A change is a read-modify-write of the whole word, as the app does it (`PROV/S.java`): the current word is read
    first when not known, never guessed, and the bits without a name are sent back as read; the other flags'
    switches rewrite the same word, so the change holds `PropertyReader.modifying` from the read to the write.
    """

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set the flag, keeping the others."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear the flag, keeping the others."""
        await self._set(False)

    @property
    def is_on(self) -> bool | None:
        """The flag; None until the node reported its device lock."""
        return self.flag

    async def _set(self, on: bool) -> None:
        async with self.changing():
            word = self.word
            if word is None:
                await self.read_current(self.spec)
                word = self.word
            if word is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="device_lock_unknown",
                    translation_placeholders={"entity": self.entity_id},
                )
            bit = 1 << self.target.bit
            await self.async_write_value(word | bit if on else word & ~bit)


class JungHomeWalkingTest(PropertyEntity, SwitchEntity):
    """A detector's walking test, the app's way; **unverified on air** (no detector in the installation), so off by default.

    The app starts it with an acknowledged Set of the test flag (0x6001) and then of the presence control (0x6003),
    asks for the PIR zones (0x6005) every second while it runs, and sets both back to 0 when stopped, after five
    minutes or when its page closes (`docs/gap-analysis/control-and-state.md` §2.8). The detector is not known to end
    the test by itself, so this switch ends any test it sees running — started here or in the app — five minutes
    after it saw it start. While the test runs the triggered zones are the `pir_zones` attribute: bit 0 = A, 1 = B,
    2 = C (the ceiling detector has three zones, the wall ones two; bit 0's polarity is unconfirmed).
    """

    target: ValueTarget

    def __init__(self, hub: JungHomeHub, target: ValueTarget) -> None:
        """Bind to the detector element; the zone letters follow the product."""
        super().__init__(hub, target)
        self._zones = "abc" if target.node.pid == PRESENCE_DETECTOR_PID else "ab"
        self._stop: CALLBACK_TYPE | None = None  # the pending end of the test
        self._poll: CALLBACK_TYPE | None = None  # the zone poll while it runs
        self._poll_read: asyncio.Task[bool] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe and read like every config entity; the timers go with the entity."""
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_timers)

    @property
    def is_on(self) -> bool | None:
        """Whether the detector's test flag is set; None until read."""
        value = self.property_value
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The address and property, plus the triggered zones (`a`, `b`, `c`) while the test runs."""
        attrs = dict(self._attr_extra_state_attributes)
        pir = self.value_of(PIR_SPEC)
        if self.is_on and isinstance(pir, int):
            attrs["pir_zones"] = [
                z for bit, z in enumerate(self._zones) if pir >> bit & 1
            ]
        return attrs

    @callback
    def _handle_update(self) -> None:
        self._follow()
        super()._handle_update()

    @callback
    def _follow(self, *, restart: bool = False) -> None:
        """While the test runs keep its end and the zone poll pending, none otherwise; `restart`: end it 5 min from now."""
        if not self.is_on:
            self._cancel_timers()
            return
        if restart and self._stop is not None:
            self._stop()
            self._stop = None
        if self._stop is None:
            self._stop = async_call_later(
                self.hass, DETECTOR_WALKING_TEST_DURATION, self._time_up
            )
        if self._poll is None:
            self._poll = async_track_time_interval(
                self.hass,
                self._poll_zones,
                timedelta(seconds=DETECTOR_WALKING_TEST_POLL),
            )

    @callback
    def _cancel_timers(self) -> None:
        for cancel in (self._stop, self._poll):
            if cancel is not None:
                cancel()
        self._stop = self._poll = None

    @callback
    def _poll_zones(self, _now: Any) -> None:
        """Ask for the zones, unless the last question is still out (a Get may wait longer than a poll period)."""
        if self._poll_read is not None and not self._poll_read.done():
            return
        self._poll_read = self.hub.entry.async_create_background_task(
            self.hass,
            self.reader.read(self.address, PIR_SPEC, since=time.monotonic()),
            f"{DOMAIN} walking test zones {self.address:04X}",
        )

    @callback
    def _time_up(self, _now: Any) -> None:
        self._stop = None
        self.hub.entry.async_create_background_task(
            self.hass, self._end(), f"{DOMAIN} walking test end {self.address:04X}"
        )

    async def _end(self) -> None:
        """Stop the test after its five minutes; a detector that does not confirm is logged, not raised."""
        try:
            await self.async_turn_off()
        except HomeAssistantError as err:
            _LOGGER.warning(
                "%s: the walking test was not stopped: %s", self.entity_id, err
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the test: the test flag, then the presence control, as the app does."""
        await self.async_write_value(True)
        await self.async_write_value(True, PRESENCE_CONTROL_SPEC)
        self._follow(restart=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the test: both back to 0."""
        self._cancel_timers()
        await self.async_write_value(False)
        await self.async_write_value(False, PRESENCE_CONTROL_SPEC)


class JungHomeEdgeMode(EdgeDetectionEntity, SwitchEntity):
    """A mini-actuator input's *Edge evaluation*: on = its edges act (rising / falling selects), off = state mode."""

    @property
    def is_on(self) -> bool | None:
        """Edge mode; None until the input reported it."""
        value = self.edge_detection
        return value.edge_mode if value else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Evaluate edges."""
        await self.async_write_part(edge_mode=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Evaluate the state."""
        await self.async_write_part(edge_mode=False)


class JungHomeUseLastLightness(SetupStateEntity, SwitchEntity):
    """A dimmer's *Use previous value*: on = it switches on at its last brightness (Light Lightness Default 0).

    Off writes 100 %, as the app does (`C1850f.java`); the *Switch-on brightness* number sets any other value.
    """

    @property
    def is_on(self) -> bool | None:
        """Whether the switch-on brightness is the last one; None until read."""
        raw = self.setup_value
        return None if raw is None else u16(raw) == 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch on at the last brightness."""
        await self.async_write_setup(M.light_lightness_default_set, 0)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch on at full brightness."""
        await self.async_write_setup(M.light_lightness_default_set, 0xFFFF)


class JungHomeSensorPublication(JungHomeEntity, SwitchEntity):
    """*Sensor values for IoT systems*: whether the node's Sensor Servers publish their values.

    Read from the node, as the app does (`CheckPublicationForSensorServer`): a Config Model Publication Get of each
    Sensor Server, once per link through the property reader's queue; on while one of them publishes anywhere (the
    app's test: a publication address). Until the node has answered, what the export says (a Sensor Server
    publishing to its element's group). A change is the Config messages of `MeshConfigurator.set_sensor_publication`,
    planned against the node's answer when there is one, the export rewritten and followed in place, like the key
    and room actions; the node is asked again afterwards.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "sensor_publication"

    def __init__(self, hub: JungHomeHub, node: Node, device_info: DeviceInfo) -> None:
        """Bind to the node's first sensor element."""
        element = sensor_elements(node)[0]
        super().__init__(
            hub,
            element.address,
            f"{node.uuid.lower()}-sensor_publication",
            device_info,
        )
        self.node = node
        self._attr_extra_state_attributes = {"mesh_address": f"{node.unicast:04X}"}
        self._published: bool | None = None  # what the node answered, None until then
        self._read_link: int | None = None  # `hub.link_count` the read was queued on

    @property
    def is_on(self) -> bool:
        """Whether a Sensor Server of the node publishes: the node's answer, else the export's."""
        if self._published is not None:
            return self._published
        return sensor_publication(self.hub.cdb, self.hub.cdb.export_meta, self.node)

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates, then read the publications once the link is up."""
        await super().async_added_to_hass()
        self._maybe_read()

    @callback
    def _handle_update(self) -> None:
        self._maybe_read()
        super()._handle_update()

    @callback
    def _maybe_read(self) -> None:
        """Queue the read of the publications, once per link."""
        if not self.hub.connected or self._read_link == self.hub.link_count:
            return
        self._read_link = self.hub.link_count
        property_reader(self.hass, self.hub).schedule(self.node.unicast, self._read)

    async def _read(self) -> None:
        """Ask each Sensor Server of the node for its publication; a node that stays silent keeps the last verdict."""
        answers: list[bool] = []
        for element in sensor_elements(self.node):
            query = (
                Query(  # the audit's Get: its `matches` takes only this model's echo
                    self.node.unicast,
                    "publication",
                    C.model_publication_get(element.address, SENSOR_SERVER),
                    C.CONFIG_MODEL_PUBLICATION_STATUS,
                    element.address,
                    SENSOR_SERVER,
                )
            )
            try:
                reply = await self.hub.proxy.request_config(
                    query.node,
                    query.pdu,
                    query.expect,
                    timeout=const.PROPERTY_READ_TIMEOUT,
                    retries=PROPERTY_READ_RETRIES,
                    match=query.matches,
                )
                status = C.decode_model_publication_status(reply.params)
            except (TimeoutError, ConnectionError, ValueError) as err:
                _LOGGER.debug(
                    "no publication of %04X's Sensor Server: %r", element.address, err
                )
                continue
            if status.ok:
                answers.append(status.publish_address != 0)
        if answers:
            self._published = any(answers)
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Have every Sensor Server of the node publish to its element group."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the node's sensor publications."""
        await self._set(False)

    async def _set(self, on: bool) -> None:
        # planned against what the node answered, which the switch shows, not only against the export
        unicast, live = self.node.unicast, self._published
        try:
            await async_configure(
                self.hass,
                self.hub.entry.entry_id,
                lambda configurator: configurator.set_sensor_publication(
                    unicast, on, live=live
                ),
            )
        finally:
            # the node's answer was about its publications before the change, and the entity stays (no reload):
            # the export, which recorded what the node took, shows until the node is asked again
            self._published = self._read_link = None
            if self.platform is not None:
                self._handle_update()


class JungHomeTimeKeeper(JungHomeEntity, SwitchEntity):
    """*Time keeper*: the node relays the time to the PP2 pucks (off by default, unverified on air).

    The app's `TimeKeeperConfiguration` (network-logic.md §6.2), by hand: the app elects one mains node itself
    whenever the project has a PP2 puck (`EnsureTimeKeeper`), Home Assistant lets the user pick one, and raises
    the `time_keeper_missing` repair while none is (`Issues.report_time_keeper`). On: the node's Time Server
    publishes to `FEFF` (`MeshConfigurator.set_time_keeper`), then Time Role Set 2 (relay) to its Time Setup
    Server; off: the publication removed, then Time Role Set 3 (client). Shows the time role the node last
    answered (the connect-time Time Role Get, `properties.reader.PropertyReader`, or the answer to the Set): on for
    relay, unknown until it answered. Several keepers are not refused: each relays the same time.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "time_keeper"

    def __init__(self, hub: JungHomeHub, node: Node) -> None:
        """Bind to the node's Time Setup Server element."""
        element = next(e for e in node.elements if TIME_SETUP_SERVER in e.models)
        super().__init__(
            hub,
            element.address,
            f"{node.uuid.lower()}-time_keeper",
            node_device_info(hub, node),
        )
        self.node = node
        self._attr_extra_state_attributes = {"mesh_address": f"{node.unicast:04X}"}

    @property
    def is_on(self) -> bool | None:
        """Whether the node answered the relay role; None until it answered one."""
        raw = self.hub.node_info(self.node.unicast).get(NODE_INFO_TIME_ROLE)
        return None if not raw else raw[0] == M.TIME_ROLE_RELAY

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Make the node the time keeper."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stand the node down (a time client again)."""
        await self._set(False)

    async def _set(self, on: bool) -> None:
        unicast = self.node.unicast
        await async_configure(
            self.hass,
            self.hub.entry.entry_id,
            lambda configurator: configurator.set_time_keeper(unicast, on),
        )
        role = M.TIME_ROLE_RELAY if on else M.TIME_ROLE_CLIENT
        try:
            reply = await self.hub.proxy.request(
                self.address,
                M.time_role_set(role),
                M.TIME_ROLE_STATUS,
                timeout=const.PROPERTY_READ_TIMEOUT,
                retries=PROPERTY_READ_RETRIES,
            )
            M.decode_time_role_status(reply.params)
        except (TimeoutError, ConnectionError, ValueError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="time_keeper_role_failed",
                translation_placeholders={
                    "address": f"{unicast:04X}",
                    "error": str(err) or type(err).__name__,
                },
            ) from err
        self.hub.remember_node_info(unicast, NODE_INFO_TIME_ROLE, reply.params[:1])
        if self.platform is not None:
            self.async_write_ha_state()


class JungHomeAllSockets(JungHomeCentralEntity, SwitchEntity):
    """The app's "all sockets": one Unacknowledged OnOff Set to 0xFEF8 for every socket at once; in a room, one per socket."""

    _attr_device_class = SwitchDeviceClass.OUTLET

    @property
    def is_on(self) -> bool | None:
        """On while any socket is."""
        return self.members_on()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch every socket on."""
        await self._switch(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch every socket off."""
        await self._switch(False)
