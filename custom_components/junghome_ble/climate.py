"""Climate: the room thermostat (RTR) as a heating climate entity, manual or automatic.

**Unverified on a real room thermostat.** The maintainer has none; everything here follows the app's and the gateway
firmware's behaviour as documented in `docs/gap-analysis/control-and-state.md` §2.7 and `docs/cross-repo-analysis.md`
§1.4 / D9, with the gateway integration's HA semantics (permanent `heat`, derived presets, `none` a no-op).

Mesh side, all on the RTR node (`jhmesh.devices.Thermostat`):
- set-point = the Generic Level state of the node's first Generic Level server (`Thermostat.address`): 5..30 °C mapped
  onto the level range with `pct = (t - 5) / 25 * 100` (`temperature_to_level` / `level_to_temperature`, 0.25 °C
  resolution), read with Generic Level Get, written with an acknowledged Generic Level Set like the app's slider;
- room temperature = Sensor Status property 0x004F (Present Ambient Temperature, one byte * 0.5 °C) of the Sensor
  server element, read with Sensor Get 0x004F and cached as a raw property on that element's state;
- heating demand = the RTR's own Generic OnOff server (`hvac_action`), read with Generic OnOff Get;
- presets = the preset temperatures 0x1203 (comfort) / 0x1204 (eco) / 0x1205 (frost) and, on firmware ≥ 2.2.0.0, the
  mode property 0x120B — vendor Admin properties of the primary element, read through the config entities'
  `PropertyReader` and cached in `ElementState.properties`. Selecting a preset writes its temperature as the set-point
  (every firmware) and the mode property when the RTR has reported one (new firmware, as the app does);
- boost = the vendor Admin property 0x120D, the `boost` preset: full heating for five minutes, which the RTR ends by
  itself without telling anyone, so the entity reads it back `RTR_BOOST_DURATION` after it started (and every as long
  again while it still reads on) — the app polls it while its page is open;
- automatic operation = 0x1246 (1 auto: the RTR's own comfort / eco profile, 0 manual), the `auto` / `heat` HVAC
  modes. An RTR has no off.

Boost and automatic operation are the app's Display-tab controls (`docs/gap-analysis/control-and-state.md` §2.7);
writing them from here is **unverified on air**, like the rest of the entity. The firmware's other RTR ids (cooling
0x1202 / 0x1206, holiday 0x120E, set-point limits 0x1242 / 0x1243, the sensor value 0x1223) have no documented
layout or no documented way in, so they are not used (`docs/android/properties.md` §1.10).

The hub's connect-time refresh does not know thermostats, so the entity queues its own Gets through the property
reader after every connection; they run after the hub's refresh, like the config entities' initial reads, and
`homeassistant.update_entity` runs them at once (boost included). Status
handlers: Generic Level Status and OnOff Status are the hub's own (`ElementState.level` / `.on`); Sensor Status
wraps the hub's socket handler and adds 0x004F.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .config_entities import (
    cached_value,
    check_outcome,
    node_version,
    property_reader,
)
from .const import (
    CLIMATE_MAX_TEMP,
    CLIMATE_MIN_TEMP,
    CLIMATE_TEMP_STEP,
    DOMAIN,
    REFRESH_RETRIES,
    RTR_BOOST_DURATION,
    RTR_BOOST_POLL_INTERVAL,
    RTR_BOOST_READBACK_MARGIN,
    SIGNAL_UPDATE,
)
from .coordinator import STATUS_HANDLERS, StatusHandler, register_status_handler
from .entity import (
    JungHomeCentralEntity,
    JungHomeEntity,
    async_setup_platform,
    node_device_info,
    room_loads,
)
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh.devices import ALL_THERMOSTATS, Thermostat

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .config_entities import PropertyReader
    from .coordinator import JungHomeHub
    from .entity import UpdateRead
    from .jhmesh.client import AccessMessage
    from .jhmesh.devices import Device

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0  # push-based; commands are serialised by the mesh client itself

PRESET_FROST = "frost"  # frost protection: no HA constant, the gateway's name
PROPERTY_AMBIENT_TEMPERATURE = 0x004F  # SIG Present Ambient Temperature (Sensor Status)
PROPERTY_HVAC_MODE = 0x120B  # RtrHvacMode, firmware >= 2.2.0.0
# HA preset -> the vendor Admin property holding its temperature (docs/android/properties.md §1.5).
PRESET_TEMPERATURES: dict[str, int] = {
    PRESET_COMFORT: 0x1203,
    PRESET_ECO: 0x1204,
    PRESET_FROST: 0x1205,
}
# 0x120B option names (`properties.HVAC_MODE`) <-> HA presets.
HVAC_MODE_PRESETS: dict[str, str] = {
    "none": PRESET_NONE,
    "comfort": PRESET_COMFORT,
    "eco": PRESET_ECO,
    "frost": PRESET_FROST,
}
PRESET_HVAC_MODES = {preset: name for name, preset in HVAC_MODE_PRESETS.items()}
HVAC_MODE_SPEC = P.PROPERTIES[PROPERTY_HVAC_MODE]
BOOST_SPEC = P.PROPERTIES[0x120D]  # RtrBoostMode, bool
AUTOMATIC_SPEC = P.PROPERTIES[0x1246]  # SchedulerEnabled, bool: 1 auto, 0 manual
AMBIENT_TEMPERATURE_SPEC = P.SIG_PROPERTIES[PROPERTY_AMBIENT_TEMPERATURE]
LEVEL_MIN, LEVEL_MAX = -32768, 32767
STATE_GET_TIMEOUT = 3.0  # seconds per attempt of a state Get, as the hub's own refresh


def temperature_to_level(temperature: float) -> int:
    """Map a set-point in °C to the Generic Level the app sends: `pct = round((t - 5) / 25 * 100)`, `level = -32768 + pct / 100 * 65535`."""
    span = CLIMATE_MAX_TEMP - CLIMATE_MIN_TEMP
    pct = max(0, min(100, round((temperature - CLIMATE_MIN_TEMP) / span * 100)))
    return max(LEVEL_MIN, min(LEVEL_MAX, round(LEVEL_MIN + pct / 100 * 65535)))


def level_to_temperature(level: int) -> float:
    """Map a Generic Level back to °C: `pct = round((level + 32768) * 100 / 65535)`, `t = 5 + 25 * pct / 100` (0.25 °C steps)."""
    pct = max(0, min(100, round((level - LEVEL_MIN) * 100 / 65535)))
    return CLIMATE_MIN_TEMP + (CLIMATE_MAX_TEMP - CLIMATE_MIN_TEMP) * pct / 100


# ----------------------------------------------------------------------------- status handlers


def _after(*opcodes: int) -> Callable[[StatusHandler], StatusHandler]:
    """Register a SIG status handler that runs *after* the one the table already has for the opcode, if any.

    The registry keeps one handler per message type, so a module adding to a type the hub (or another platform)
    already handles must chain: the earlier handler keeps doing its work, then this one does its own.
    """
    earlier = {op: STATUS_HANDLERS.get((None, op)) for op in opcodes}

    def register(handler: StatusHandler) -> StatusHandler:
        def chained(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
            if (before := earlier[m.opcode]) is not None:
                before(hub, m, p)
            handler(hub, m, p)

        register_status_handler(*opcodes)(chained)
        return handler

    return register


@_after(M.SENSOR_STATUS)
def _on_sensor_status(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
    """Cache the Present Ambient Temperature (0x004F) a Sensor Status carries, raw, on the sending element."""
    try:
        values = M.sensor_values(p)
    except ValueError:
        return  # malformed: the hub's own Sensor Status handler already logged it
    for prop, raw in values:
        if prop == PROPERTY_AMBIENT_TEMPERATURE:
            hub.element_state(m.src).properties[prop] = raw
            hub.notify_update(m.src)
            return


# ----------------------------------------------------------------------------- platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of `build_entities`, kept with the hub to follow a new export in place (`model_update`)."""
    async_setup_platform(entry.runtime_data, "climate", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[ClimateEntity]:
    """Return one climate entity per room thermostat, and the central *All thermostats* (home and per room).

    Home-wide when thermostats listen to their device-type group; per room for each room that has some (the app's
    area sheet).
    """
    entities: list[ClimateEntity] = [
        JungHomeClimate(hub, rtr) for rtr in hub.devices.thermostats
    ]
    if members := hub.devices.central.get(ALL_THERMOSTATS):
        entities.append(JungHomeAllThermostats(hub, members))
    entities += [
        JungHomeAllThermostats(hub, rtrs, room)
        for room, rtrs in room_loads(hub, Thermostat).items()
    ]
    return entities


class JungHomeClimate(JungHomeEntity, ClimateEntity):
    """A room thermostat: set-point 5..30 °C, manual or automatic, comfort / eco / frost / boost, heating demand.

    Temperatures in 0.5 °C: the room temperature's resolution and the app's slider step.
    """

    _attr_name = None  # the node device *is* the thermostat
    _attr_translation_key = "thermostat"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = CLIMATE_TEMP_STEP
    _attr_min_temp = CLIMATE_MIN_TEMP
    _attr_max_temp = CLIMATE_MAX_TEMP
    # An RTR cannot be switched off (the closest thing is the frost preset): manual (heat) or its own profile (auto).
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.AUTO]
    _attr_preset_modes = [
        PRESET_NONE,
        PRESET_COMFORT,
        PRESET_ECO,
        PRESET_FROST,
        PRESET_BOOST,
    ]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(self, hub: JungHomeHub, thermostat: Thermostat) -> None:
        """Bind to the thermostat's set-point element, under its node device."""
        super().__init__(
            hub,
            thermostat.address,
            thermostat.unique_id,
            node_device_info(hub, thermostat.node),
        )
        self.thermostat = thermostat
        self.primary = thermostat.node.unicast  # the vendor Admin properties live here
        # `hub.connected_since` of the link whose state reads were queued (one refresh per connection)
        self._refreshed_for: float | None = None
        # the pending read-back of a running boost
        self._boost_check: CALLBACK_TYPE | None = None
        self._attr_extra_state_attributes = {
            "mesh_address": f"{thermostat.address:04X}",
            "rooms": thermostat.rooms,
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The addresses and rooms, and `controlled_loads`: the lights and sockets the RTR switches (its links).

        The app's `GetConnectedDevices`, read from the loads' subscriptions (`jhmesh.devices.thermostat_links`);
        unverified on air.
        """
        devices = self.hub.devices
        return {
            **self._attr_extra_state_attributes,
            "controlled_loads": [
                devices.by_address[address].name
                for address, rtrs in devices.thermostats_of.items()
                if any(rtr.address == self.address for rtr in rtrs)
            ],
        }

    # ------------------------------------------------------------------ wiring
    @property
    def reader(self) -> PropertyReader:
        """The hub's property reader: the RTR's preset temperatures and mode go through it."""
        return property_reader(self.hass, self.hub)

    @property
    def addresses(self) -> set[int]:
        """Every element whose state the entity renders."""
        return {
            addr
            for addr in (
                self.address,
                self.thermostat.onoff_address,
                self.thermostat.sensor_address,
                self.primary,
            )
            if addr is not None
        }

    @property
    def listened(self) -> tuple[int, ...]:
        """The thermostat's other elements, whose updates the entity renders too."""
        return tuple(sorted(self.addresses - {self.address}))

    async def async_added_to_hass(self) -> None:
        """Subscribe to every element of the thermostat, then read its state once the link is up."""
        await super().async_added_to_hass()
        for addr in self.listened:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    SIGNAL_UPDATE.format(self.hub.entry.entry_id, addr),
                    self._handle_update,
                )
            )
        self.async_on_remove(self._cancel_boost_check)
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._poll_boost,
                timedelta(seconds=RTR_BOOST_POLL_INTERVAL),
            )
        )
        self._maybe_refresh()

    @callback
    def _poll_boost(self, _now: datetime) -> None:
        """Queue a read of the boost while the link is up (`RTR_BOOST_POLL_INTERVAL`): one started on the RTR shows.

        The app's 5 s poll runs only while its thermostat page is open (`requestBoostFunction`); this one runs at a
        minute, behind the property reader's queue, and not while a boost is known to run: its read-back follows
        that one (`_follow_boost`). A value another read got within PROPERTY_READ_FRESH is taken as it is.
        Unverified on air.
        """
        if not self.hub.connected or self._property(self.primary, BOOST_SPEC) is True:
            return
        self.reader.schedule(self.primary, self._read_boost, key=BOOST_SPEC.id)

    async def _read_boost(self) -> None:
        await self.reader.read(self.primary, BOOST_SPEC)

    @callback
    def _handle_update(self) -> None:
        self._maybe_refresh()
        self._follow_boost()
        super()._handle_update()

    @callback
    def _follow_boost(self, *, restart: bool = False) -> None:
        """Keep one read-back pending while the RTR reports a boost, none otherwise; `restart`: count from now.

        A boost reported by anyone (the app, the RTR's own key, this entity) ends on its own after five minutes and
        nothing says so. A read-back that still finds it on leaves the next one to the update its answer causes; one
        the RTR does not answer leaves it to the next update of the thermostat, or the next link's refresh.
        """
        if self._property(self.primary, BOOST_SPEC) is not True:
            self._cancel_boost_check()
            return
        if restart:
            self._cancel_boost_check()
        if self._boost_check is None:
            self._boost_check = async_call_later(
                self.hass,
                RTR_BOOST_DURATION + RTR_BOOST_READBACK_MARGIN,
                self._boost_over,
            )

    @callback
    def _cancel_boost_check(self) -> None:
        if self._boost_check is not None:
            self._boost_check()
            self._boost_check = None

    @callback
    def _boost_over(self, _now: datetime) -> None:
        """Read the boost back once it should have ended."""
        self._boost_check = None
        self.hub.entry.async_create_background_task(
            self.hass,
            self.reader.read(self.primary, BOOST_SPEC, since=time.monotonic()),
            f"{DOMAIN} boost read-back {self.primary:04X}",
        )

    @callback
    def _maybe_refresh(self) -> None:
        """Queue the state reads once per connection (the hub's own refresh does not know thermostats)."""
        since = self.hub.connected_since
        if not self.hub.connected or since is None or since == self._refreshed_for:
            return
        self._refreshed_for = since
        self.reader.schedule_version(self.thermostat.node)
        self.reader.schedule(self.address, self._refresh)

    def _update_read(self) -> UpdateRead:
        """`homeassistant.update_entity` runs the per-link refresh now, its properties asked whatever was read last.

        Boost included: it ends on its own and says so to no one (`_follow_boost`). Unverified on air.
        """
        return "thermostat", partial(self._refresh, fresh=True)

    async def _refresh(self, *, fresh: bool = False) -> None:
        """Ask the RTR for its set-point, heating demand and room temperature, then its preset temperatures and mode.

        `fresh`: ask every property even when another entity read it within PROPERTY_READ_FRESH.
        """
        since = time.monotonic() if fresh else None
        gets: list[tuple[int | None, bytes, int, str]] = [
            (self.address, M.generic_level_get(), M.GEN_LEVEL_STATUS, "Level"),
            (
                self.thermostat.onoff_address,
                M.generic_onoff_get(),
                M.GEN_ONOFF_STATUS,
                "OnOff",
            ),
            (
                self.thermostat.sensor_address,
                M.sensor_get(PROPERTY_AMBIENT_TEMPERATURE),
                M.SENSOR_STATUS,
                "Sensor",
            ),
        ]
        try:
            for addr, pdu, status, name in gets:
                if addr is None:
                    continue
                try:
                    await self.hub.proxy.request(
                        addr,
                        pdu,
                        status,
                        timeout=STATE_GET_TIMEOUT,
                        retries=REFRESH_RETRIES,
                    )
                except TimeoutError:
                    _LOGGER.debug("%04X did not answer its %s Get", addr, name)
        except ConnectionError as err:
            _LOGGER.debug("thermostat refresh aborted: %s", err)
            return
        # a preset the RTR does not answer is not the others' problem (and all of them are asked again on the next
        # link, `_maybe_refresh`); on a lost link every read fails at once
        for spec in self._property_specs():
            await self.reader.read(self.primary, spec, since=since)

    def _property_specs(self) -> list[P.PropertySpec]:
        """Return the preset temperatures, the mode property when the firmware (if known) has it, boost, automatic."""
        specs = [P.PROPERTIES[pid] for pid in PRESET_TEMPERATURES.values()]
        if P.supported(HVAC_MODE_SPEC, node_version(self.hub, self.thermostat.node)):
            specs.append(HVAC_MODE_SPEC)
        return [*specs, BOOST_SPEC, AUTOMATIC_SPEC]

    # ------------------------------------------------------------------ state
    def _property(self, addr: int, spec: P.PropertySpec) -> Any:
        """Return the decoded cached value of `spec` on element `addr`, None when unknown or malformed."""
        return cached_value(self.hub, addr, spec)

    def _preset_temperature(self, preset: str) -> float | None:
        value = self._property(self.primary, P.PROPERTIES[PRESET_TEMPERATURES[preset]])
        return float(value) if value is not None else None

    @property
    def _mode_property(self) -> str | None:
        """The RTR's own mode (0x120B) as an option name, None until it reported one (or on old firmware, never)."""
        value = self._property(self.primary, HVAC_MODE_SPEC)
        return value if isinstance(value, str) else None

    @property
    def hvac_mode(self) -> HVACMode:
        """`auto` while the RTR runs its own comfort / eco profile (0x1246 on); `heat` when manual, and until known."""
        if self._property(self.primary, AUTOMATIC_SPEC) is True:
            return HVACMode.AUTO
        return HVACMode.HEAT

    @property
    def target_temperature(self) -> float | None:
        """The set-point from the level element's cached Generic Level; None until heard from."""
        st = self.hub.states.get(self.address)
        if st is None or st.level is None:
            return None
        return level_to_temperature(st.level)

    @property
    def current_temperature(self) -> float | None:
        """The room temperature from the sensor element's cached 0x004F; None until heard from (or while 'unknown')."""
        if self.thermostat.sensor_address is None:
            return None
        value = self._property(self.thermostat.sensor_address, AMBIENT_TEMPERATURE_SPEC)
        return float(value) if value is not None else None

    @property
    def hvac_action(self) -> HVACAction | None:
        """Heating while the RTR's own OnOff server is on (its heating output), idle while off, None until known."""
        addr = self.thermostat.onoff_address
        st = self.hub.states.get(addr) if addr is not None else None
        if st is None or st.on is None:
            return None
        return HVACAction.HEATING if st.on else HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        """`boost` while boosting; else the RTR's mode property when it reported one, else the preset the set-point is.

        Derived like the app on old firmware and the gateway: `none` when the set-point matches no preset, None
        while neither the set-point nor any preset temperature is known.
        """
        if self._property(self.primary, BOOST_SPEC) is True:
            return PRESET_BOOST
        if (mode := self._mode_property) is not None:
            return HVAC_MODE_PRESETS.get(mode)
        target = self.target_temperature
        if target is None:
            return None
        known = False
        for preset in PRESET_TEMPERATURES:
            temperature = self._preset_temperature(preset)
            if temperature is None:
                continue
            known = True
            if level_to_temperature(temperature_to_level(temperature)) == target:
                return preset
        return PRESET_NONE if known else None

    # ------------------------------------------------------------------ commands
    async def _set_level(self, level: int) -> None:
        await self._send(self.hub.set_level(self.address, level))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Send the set-point as a Generic Level Set (HA has already checked the 5..30 °C range).

        Refused while the RTR boosts: the app disables its slider and +/- then (`updateBoostMode`); another preset
        ends the boost first. Unverified on air.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        if self._property(self.primary, BOOST_SPEC) is True:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="thermostat_boosting",
                translation_placeholders={"entity": self.entity_id},
            )
        await self._set_level(temperature_to_level(float(temperature)))

    async def _write(self, spec: P.PropertySpec, value: Any) -> None:
        """Write a vendor property of the primary element; a Set the RTR neither confirms nor reads back fails."""
        try:
            outcome = await self.reader.write(self.primary, spec, value)
        except (ConnectionError, OSError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err
        check_outcome(outcome, self.entity_id)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Start a boost, or end a running one and write the preset's temperature (and the mode property if any).

        While the RTR boosts the app offers nothing else, so any other preset stops the boost first. `none` then
        is a local no-op: a preset is the derived fact that the set-point equals a preset temperature, so there is
        nothing to command. A preset temperature the RTR has not reported yet is read on demand first.
        """
        if preset_mode == PRESET_BOOST:
            await self._write(BOOST_SPEC, True)
            self._follow_boost(restart=True)
            return
        if self._property(self.primary, BOOST_SPEC) is True:
            await self._write(BOOST_SPEC, False)
        if preset_mode == PRESET_NONE:
            _LOGGER.debug("%s: 'none' is display-only, nothing to send", self.entity_id)
            return
        spec = P.PROPERTIES[PRESET_TEMPERATURES[preset_mode]]
        has_mode = self._mode_property is not None
        temperature = self._preset_temperature(preset_mode)
        if temperature is None and not has_mode:
            await self.reader.read(self.primary, spec)
            temperature = self._preset_temperature(preset_mode)
        if temperature is None and not has_mode:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="preset_temperature_unknown",
                translation_placeholders={"preset": preset_mode},
            )
        if temperature is not None:
            await self._set_level(temperature_to_level(temperature))
        if has_mode:
            await self._write(HVAC_MODE_SPEC, PRESET_HVAC_MODES[preset_mode])

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch automatic operation (0x1246) on for `auto`, off for `heat`; nothing to send when it already is."""
        automatic = hvac_mode == HVACMode.AUTO
        if self._property(self.primary, AUTOMATIC_SPEC) is automatic:
            return
        await self._write(AUTOMATIC_SPEC, automatic)


class JungHomeAllThermostats(JungHomeCentralEntity, ClimateEntity):
    """The app's "all RTRs": one Unacknowledged Generic Level Set to 0xFEF9 sets every thermostat's set-point.

    Heat-only like each thermostat; the target and room temperatures are the means of the members'. No presets:
    the app's central function sets the temperature only. The thermostats of a room: one Level Set per set-point
    element, as the app's area sheet sends them. **Unverified on hardware**, like the thermostats.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = CLIMATE_TEMP_STEP
    _attr_min_temp = CLIMATE_MIN_TEMP
    _attr_max_temp = CLIMATE_MAX_TEMP
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(
        self, hub: JungHomeHub, members: list[Device], room: int | None = None
    ) -> None:
        """Bind to the thermostats' device-type group, or to the thermostats of `room`."""
        super().__init__(hub, ALL_THERMOSTATS, members, "thermostats", room)
        self._sensors = [
            t.sensor_address
            for t in members
            if isinstance(t, Thermostat) and t.sensor_address is not None
        ]

    @property
    def watched(self) -> list[int]:
        """The set-point elements and the room-temperature sensors, each once (a sensor may be a set-point element)."""
        return list(dict.fromkeys([*super().watched, *self._sensors]))

    @property
    def target_temperature(self) -> float | None:
        """The members' mean set-point; None until one reported."""
        values = [
            level_to_temperature(st.level)
            for m in self.members
            if (st := self.hub.states.get(m.address)) and st.level is not None
        ]
        return _mean(values)

    @property
    def current_temperature(self) -> float | None:
        """The members' mean room temperature; None until one reported."""
        values = [
            float(value)
            for address in self._sensors
            if (value := cached_value(self.hub, address, AMBIENT_TEMPERATURE_SPEC))
            is not None
        ]
        return _mean(values)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Send the set-point to every thermostat (HA has already checked the 5..30 °C range)."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        level = temperature_to_level(float(temperature))
        await self._level(ALL_THERMOSTATS, [m.address for m in self.members], level)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Accept `heat`, the only mode."""


def _mean(values: list[float]) -> float | None:
    """Mean of the values to the set-point step (0.5 °C), None for none."""
    if not values:
        return None
    return round(sum(values) / len(values) / CLIMATE_TEMP_STEP) * CLIMATE_TEMP_STEP
