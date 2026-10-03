"""Lights: switched loads, dimmers and tunable-white (DALI) channels, and the app's *all lights* central function.

**Hold-to-dim** (review-3 F12, the `junghome_ble.start_dim` / `stop_dim` / `step_dim` actions): what a rocker does to
a dimmer, from Home Assistant. A dimmer's (and a DALI channel's) first element hosts a Generic Level server bound to
its lightness (Mesh Model: level = lightness - 32768), so a Generic Move Set starts it moving up or down
at a speed until a Move Set 0 stops it, and a Generic Delta Set moves it by a step — the messages the coordinator
already sends a blind (`JungHomeHub.move_level` / `delta_level`). The app itself never dims this way (it sends
Lightness Sets); unverified on air. The light's state is asked for after a stop or a step, as its Level Status
does not carry the lightness the entity shows.

**Transitions** (review-4 F4-1): HA's `transition` goes into the Set (`JungHomeHub.set_lightness` and the other
setters) only for a kind in `TRANSITION_KINDS`, the only lights that declare the feature. Neither the app nor the
gateway ever sends a transition, so which JUNG loads fade is up to the on-air probe (`docs/hidden-features.md` §11);
until it ran the table is empty, HA drops a `transition` before it reaches a light, and every Set keeps the bytes it
always had. Unverified on air.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_TRANSITION,
    LightEntity,
)
from homeassistant.components.light.const import ColorMode, LightEntityFeature
from homeassistant.exceptions import ServiceValidationError

from .const import (
    DIM_MOVE_TRANSITION,
    DIM_STEPS_PER_SECOND,
    DOMAIN,
    MAX_KELVIN,
    MIN_KELVIN,
)
from .entity import (
    JungHomeCentralEntity,
    JungHomeEntity,
    async_setup_platform,
    light_device_info,
    room_loads,
)
from .jhmesh.devices import ALL_LIGHTS, Light

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .jhmesh.devices import Device

PARALLEL_UPDATES = 0  # push-based; commands are serialised by the mesh client itself

ATTR_DIRECTION, ATTR_SPEED, ATTR_STEP = "direction", "speed", "step"
DIM_DIRECTIONS = ("up", "down")
LEVEL_SERVER = "1002"  # Generic Level server: what the dimming messages go to
LIGHTNESS_RANGE = 65535  # the full Generic Level range, bottom to top
# The light kinds (`Light.kind`) whose firmware fades a Set with a transition time, from the probe of
# `docs/hidden-features.md` §11; empty until it ran (unverified on air): no light declares the feature.
TRANSITION_KINDS: frozenset[str] = frozenset()


def _transition(kind: str, kwargs: dict[str, Any]) -> float | None:
    """HA's `transition` for a light of `kind`; None (the Set as without one) for a kind not known to fade."""
    return kwargs.get(ATTR_TRANSITION) if kind in TRANSITION_KINDS else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of `build_entities`, kept with the hub to follow a new export in place (`model_update`)."""
    async_setup_platform(entry.runtime_data, "light", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[LightEntity]:
    """Return one light entity per load output, and the central *All lights* (home and per room).

    Home-wide when loads listen to the lamps' device-type group; per room for each room that has some (the app's
    area sheet).
    """
    entities: list[LightEntity] = [
        JungHomeLight(hub, light) for light in hub.devices.lights
    ]
    if members := hub.devices.central.get(ALL_LIGHTS):
        entities.append(JungHomeAllLights(hub, members))
    entities += [
        JungHomeAllLights(hub, lights, room)
        for room, lights in room_loads(hub, Light).items()
    ]
    return entities


class JungHomeLight(JungHomeEntity, LightEntity):
    """A load output: on/off, dimmable or tunable white depending on the device kind."""

    _attr_name = None  # the device *is* the light

    def __init__(self, hub: JungHomeHub, light: Light) -> None:
        """Bind to `light` and declare the colour mode its kind supports."""
        super().__init__(
            hub, light.address, light.unique_id, light_device_info(hub, light)
        )
        self.light = light
        if light.kind == "ctl":
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif light.kind == "dimmer":
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF
        if light.kind in TRANSITION_KINDS:
            self._attr_supported_features = LightEntityFeature.TRANSITION
        self._attr_extra_state_attributes = {
            "mesh_address": f"{light.address:04X}",
            "rooms": light.rooms,
        }

    @property
    def is_on(self) -> bool | None:
        """On/off from the state cache; None until the element has been heard from."""
        st = self.hub.states.get(self.address)
        return st.on if st else None

    @property
    def brightness(self) -> int | None:
        """Brightness 1-255 scaled from the mesh lightness; None while the level is unknown or the light is off.

        HA's light model has no "on at brightness 0": a lightness of 1..128 (a dimmer at its lowest level, or
        mid-fade) is 1, and a cached lightness of 0 — the light was off and a Generic OnOff Status just switched it
        on, the Lightness Status still to come — is an unknown level, not 0. Core's own integrations floor at 1 the
        same way.
        """
        st = self.hub.states.get(self.address)
        if st is None or not st.lightness:
            return None
        return max(1, round(st.lightness * 255 / 65535))

    @property
    def color_temp_kelvin(self) -> int | None:
        """Colour temperature from the state cache; None until known."""
        st = self.hub.states.get(self.address)
        return st.kelvin if st else None

    @property
    def min_color_temp_kelvin(self) -> int:
        """The light's own lower limit (Light CTL Temperature Range Status), the integration default until read."""
        st = self.hub.states.get(self.address)
        return st.kelvin_min if st and st.kelvin_min else MIN_KELVIN

    @property
    def max_color_temp_kelvin(self) -> int:
        """The light's own upper limit (Light CTL Temperature Range Status), the integration default until read."""
        st = self.hub.states.get(self.address)
        return st.kelvin_max if st and st.kelvin_max else MAX_KELVIN

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the narrowest message for what was asked.

        A colour temperature alone for a light that is on: CTL Temperature Set to its temperature element, which
        leaves the lightness where it is (a CTL Set would resend the cached one, stale while the light dims). A
        colour temperature with a brightness, or to switch the light on: CTL Set (brightness-only writes never guess
        a temperature). Lightness Set for a brightness, OnOff Set otherwise. The temperature is clamped to the
        light's range (HA does not do that for us). A `transition` goes with it for a kind in `TRANSITION_KINDS`.
        """
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        lightness = round(brightness * 65535 / 255) if brightness is not None else None
        await self._send(
            self._turn_on(lightness, kelvin, _transition(self.light.kind, kwargs))
        )

    async def _turn_on(
        self, lightness: int | None, kelvin: Any, transition: float | None
    ) -> None:
        """Send what `async_turn_on` asks for: a colour temperature alone, CTL, a lightness, or OnOff."""
        if self.light.kind == "ctl" and kelvin is not None:
            kelvin = max(
                self.min_color_temp_kelvin,
                min(self.max_color_temp_kelvin, int(kelvin)),
            )
            st = self.hub.states.get(self.address)
            if (
                lightness is None
                and st is not None
                and st.on
                and self.light.temperature_address is not None
            ):
                await self.hub.set_ctl_temperature(self.light, kelvin, transition)
                return
            if (
                lightness is None
            ):  # keep the current level; full on when it is not known (or off)
                lightness = st.lightness if st and st.lightness else 65535
            await self.hub.set_ctl(self.address, lightness, kelvin, transition)
        elif self.light.kind != "switch" and lightness is not None:
            await self.hub.set_lightness(self.address, lightness, transition)
        else:
            await self.hub.set_onoff(self.address, True, transition)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send Generic OnOff Set off (with a `transition` for a kind in `TRANSITION_KINDS`)."""
        await self._send(
            self.hub.set_onoff(
                self.address, False, _transition(self.light.kind, kwargs)
            )
        )

    @property
    def dimmable(self) -> bool:
        """Whether the light's element hosts the Generic Level server the dimming actions talk to."""
        element = self.hub.cdb.element(self.address)
        return element is not None and LEVEL_SERVER in element.models

    async def async_start_dim(self, direction: str, speed: int) -> None:
        """Start dimming `up` or `down` at `speed` % of the range per second: Generic Move Set. Unverified on air."""
        delta = round(speed / 100 * LIGHTNESS_RANGE / DIM_STEPS_PER_SECOND)
        await self._send(
            self.hub.move_level(
                self.address,
                delta if direction == "up" else -delta,
                DIM_MOVE_TRANSITION,
            )
        )

    async def async_stop_dim(self) -> None:
        """Stop dimming: Generic Move Set 0, then ask where the light stopped. Unverified on air."""
        await self._send(self.hub.move_level(self.address, 0, DIM_MOVE_TRANSITION))
        self._refresh()

    async def async_step_dim(self, step: int) -> None:
        """Dim by `step` % of the range (negative: darker): Generic Delta Set, then read back. Unverified on air."""
        await self._send(
            self.hub.delta_level(self.address, round(step / 100 * LIGHTNESS_RANGE))
        )
        self._refresh()

    def _refresh(self) -> None:
        self.hub.entry.async_create_background_task(
            self.hass,
            self.hub.async_refresh_element(self.address, self.light.kind),
            f"{DOMAIN} light refresh",
        )


def _dimmable(entity: Entity) -> JungHomeLight:
    """Return the light a dimming action targets; *All lights* and a switched light cannot be dimmed this way."""
    if isinstance(entity, JungHomeLight) and entity.dimmable:
        return entity
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="dim_not_dimmable",
        translation_placeholders={"entity": entity.entity_id},
    )


async def async_start_dim(entity: Entity, call: ServiceCall) -> None:
    """`junghome_ble.start_dim` on one light entity."""
    await _dimmable(entity).async_start_dim(
        call.data[ATTR_DIRECTION], call.data[ATTR_SPEED]
    )


async def async_stop_dim(entity: Entity, call: ServiceCall) -> None:
    """`junghome_ble.stop_dim` on one light entity."""
    await _dimmable(entity).async_stop_dim()


async def async_step_dim(entity: Entity, call: ServiceCall) -> None:
    """`junghome_ble.step_dim` on one light entity."""
    await _dimmable(entity).async_step_dim(call.data[ATTR_STEP])


class JungHomeAllLights(JungHomeCentralEntity, LightEntity):
    """The app's "all luminaires": one Unacknowledged OnOff (and Lightness) Set to 0xFEF5 for every lamp at once.

    Dimmable when any member is; the brightness is the mean of the dimmable members that are on. A brightness goes
    to the dimmers only (the switched loads just switch on), as the app's "dim all" does. The lights of a room: the
    brightness as one Lightness Set to the room address (`Dim.Group`), on / off per light
    (`JungHomeHub.room_command`). A transition goes into those Unacknowledged Sets only when every member is of a
    kind in `TRANSITION_KINDS`: a member that does not take one is never sent one.
    """

    def __init__(
        self, hub: JungHomeHub, members: list[Device], room: int | None = None
    ) -> None:
        """Bind to the lamps' device-type group, or to the lights of `room`."""
        super().__init__(hub, ALL_LIGHTS, members, "lights", room)
        self._dimmable = [
            m for m in members if isinstance(m, Light) and m.kind in ("dimmer", "ctl")
        ]
        mode = ColorMode.BRIGHTNESS if self._dimmable else ColorMode.ONOFF
        self._attr_supported_color_modes = {mode}
        self._attr_color_mode = mode
        self._fades = all(
            isinstance(m, Light) and m.kind in TRANSITION_KINDS for m in members
        )
        if self._fades:
            self._attr_supported_features = LightEntityFeature.TRANSITION

    @property
    def brightness(self) -> int | None:
        """Mean brightness (1-255) of the dimmable members that are on; None when none is."""
        levels = [
            st.lightness
            for m in self._dimmable
            if (st := self.hub.states.get(m.address)) and st.on and st.lightness
        ]
        if not levels:
            return None
        return max(1, round(sum(levels) / len(levels) * 255 / 65535))

    @property
    def is_on(self) -> bool | None:
        """On while any lamp is."""
        return self.members_on()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch every lamp on, the dimmers at the brightness given."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        lightness = None if brightness is None else round(brightness * 65535 / 255)
        await self._switch(True, lightness, self._transition(kwargs))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch every lamp off."""
        await self._switch(False, transition=self._transition(kwargs))

    def _transition(self, kwargs: dict[str, Any]) -> float | None:
        """HA's `transition` when every member takes one; None otherwise."""
        return kwargs.get(ATTR_TRANSITION) if self._fades else None
