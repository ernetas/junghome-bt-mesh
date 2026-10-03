"""Numeric device parameters (delays, run-on time, positions, temperatures, ...) as config entities.

Plus the time limit of each load's lock: not a device parameter but the value its lock switch sends with the
next lock (the app's time-limit picker, `docs/gap-analysis/control-and-state.md` §2.1), kept by HA. And a dimmer's
SIG setup states (`docs/gap-analysis/device-settings.md` §4.2 / §4.3): its brightness range, switch-on brightness
and, on a DALI insert, switch-on colour temperature and colour-temperature range.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)

from .config_entities import (
    LIGHTNESS_DEFAULT,
    PropertyEntity,
    PropertyTarget,
    SetupStateEntity,
    SetupTarget,
    config_targets,
    lock_targets,
    property_reader,
    setup_targets,
    u16,
)
from .const import (
    LOCK_TIME_LIMIT_MAX,
    WHITE_RANGE_MAX_KELVIN,
    WHITE_RANGE_MIN_KELVIN,
)
from .entity import JungHomeEntity
from .jhmesh import messages as M
from .jhmesh import properties as P

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .jhmesh.properties import Codec

# The app's switch-on colour temperature slider (*Switch-on white value*, `PROV/z.java`, `device-settings.md` §4.3):
# until the light reported its own range.
DEFAULT_KELVIN_MIN, DEFAULT_KELVIN_MAX = 2000, 10000

PARALLEL_UPDATES = (
    0  # push-based; the property reader serialises the mesh exchanges per element
)

# The spec's unit -> HA unit and device class. A temperature *difference* (the RTR sensor offset, "K") is a
# `temperature_delta`, not a `temperature`: HA converts a delta between °C, K and °F as a difference, not a point.
UNITS: dict[str, tuple[str, NumberDeviceClass | None]] = {
    "s": (UnitOfTime.SECONDS, NumberDeviceClass.DURATION),
    "ms": (UnitOfTime.MILLISECONDS, NumberDeviceClass.DURATION),
    "°C": (UnitOfTemperature.CELSIUS, NumberDeviceClass.TEMPERATURE),
    "K": (UnitOfTemperature.KELVIN, NumberDeviceClass.TEMPERATURE_DELTA),
    "%": (PERCENTAGE, None),
    "lx": (LIGHT_LUX, NumberDeviceClass.ILLUMINANCE),
}


def _int_bounds(size: int, signed: bool) -> tuple[int, int]:
    bits = 8 * size
    if signed:
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return 0, (1 << bits) - 1


def codec_range(codec: Codec) -> tuple[float, float, float]:
    """Return the (min, max, step) the wire type allows, for specs that state no range of their own."""
    if isinstance(codec, P.Percent):  # incl. Position
        return 0, 100, 1
    if isinstance(codec, P.Duration):
        _, hi = _int_bounds(codec.size, False)
        return (0, hi / 1000, 0.001) if codec.unit == "ms" else (0, hi, 1)
    if isinstance(codec, P.Scaled):
        lo, hi = _int_bounds(codec.size, codec.signed)
        return lo * codec.scale, hi * codec.scale, codec.scale
    if isinstance(codec, P.Int):
        lo, hi = _int_bounds(codec.size, codec.signed)
        return lo, hi, 1
    raise TypeError(f"{codec!r} is not a numeric codec")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one number entity per numeric property of every device, the lock time limit of every load, the dimmers' setup."""
    hub = entry.runtime_data
    entities: list[NumberEntity] = [
        JungHomePropertyNumber(hub, target) for target in config_targets(hub, "number")
    ]
    entities += [JungHomeLockTimeLimit(hub, target) for target in lock_targets(hub)]
    entities += [
        SETUP_NUMBERS[target.entity](hub, target)
        for target in setup_targets(hub, "number")
    ]
    add_entities(entities)


class JungHomePropertyNumber(PropertyEntity, NumberEntity):
    """A numeric property: range, step and unit from the catalogue, the wire type as fallback."""

    def __init__(self, hub: JungHomeHub, target: PropertyTarget) -> None:
        """Bind to `target`."""
        super().__init__(hub, target)
        spec = target.spec
        lo, hi, step = codec_range(spec.codec)
        self._attr_native_min_value = lo if spec.min is None else spec.min
        self._attr_native_max_value = hi if spec.max is None else spec.max
        self._attr_native_step = step if spec.step is None else spec.step
        self._attr_mode = (
            NumberMode.SLIDER if isinstance(spec.codec, P.Percent) else NumberMode.BOX
        )
        if spec.unit in UNITS:
            unit, device_class = UNITS[spec.unit]
            self._attr_native_unit_of_measurement = unit
            self._attr_device_class = device_class

    @property
    def native_value(self) -> float | None:
        """The decoded value; None until the element reported it (or when it reports "unknown")."""
        value = self.property_value
        return value if isinstance(value, int | float) else None

    async def async_set_native_value(self, value: float) -> None:
        """Write the value (integer codecs take whole numbers)."""
        await self.async_write_value(
            int(value) if isinstance(self.spec.codec, P.Int) else value
        )


class JungHomeLockTimeLimit(JungHomeEntity, RestoreNumber):
    """How long the load's lock switch locks it, in seconds (0 = until unlocked); restored across restarts.

    Seconds like the app's H:MM:SS picker (`JungDurationPickerDialog`, up to 4:59:59) and the u16 time on air; a
    limit restored from a release that kept it in minutes is converted.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "lock_time_limit"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 0
    _attr_native_max_value = LOCK_TIME_LIMIT_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_value: float | None = 0

    def __init__(self, hub: JungHomeHub, target: PropertyTarget) -> None:
        """Bind to the lock `target`; shown and enabled alongside its switch."""
        base = target.unique_id.removesuffix(target.spec.name)
        super().__init__(
            hub, target.address, f"{base}lock_time_limit", target.device_info
        )
        self._attr_entity_registry_enabled_default = target.enabled_default

    async def async_added_to_hass(self) -> None:
        """Restore the last limit and hand it to the lock switch."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            minutes = last.native_unit_of_measurement == UnitOfTime.MINUTES
            self._attr_native_value = last.native_value * (60 if minutes else 1)
        self._publish()

    async def async_set_native_value(self, value: float) -> None:
        """Keep the new limit for the next lock."""
        self._attr_native_value = value
        self._publish()
        self.async_write_ha_state()

    def _publish(self) -> None:
        property_reader(self.hass, self.hub).lock_time_limits[self.address] = int(
            self._attr_native_value or 0
        )


def lightness_percent(lightness: int) -> int:
    """Return a Light Lightness (0..65535) as the app's percentage."""
    return round(lightness * 100 / 0xFFFF)


def lightness_of(percent: float) -> int:
    """Return the app's percentage as a Light Lightness (`N9/b.java`: pct / 100 · 65535)."""
    return round(percent * 0xFFFF / 100)


class _SwitchOnValue(SetupStateEntity, NumberEntity):
    """A switch-on value of a dimmer: unavailable while it uses the previous value, as the app disables the cell.

    *Use previous value* is Light Lightness Default 0 (`switch.JungHomeUseLastLightness`); the app greys the
    switch-on brightness and colour temperature out then (`docs/gap-analysis/device-settings.md` §4.2, §4.3). A
    write would turn *Use previous value* off (the brightness) or keep it (the colour temperature, sent with
    lightness 0) unseen, so neither is offered until the switch is turned off.
    """

    @property
    def uses_last_lightness(self) -> bool:
        """Whether the dimmer reported Light Lightness Default 0 (the CTL Default's lightness is a copy of it)."""
        raw = self.reader.cached_setup(self.address, LIGHTNESS_DEFAULT)
        return raw is not None and u16(raw) == 0

    @property
    def available(self) -> bool:
        """Available with the link, unless the dimmer uses the previous value."""
        return super().available and not self.uses_last_lightness


class _LightnessPercent(SetupStateEntity, NumberEntity):
    """A lightness of a setup state as the app's 1 to 100 % slider."""

    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER


class JungHomeLightnessBound(_LightnessPercent):
    """One end of a dimmer's brightness range (Light Lightness Range); a Set keeps the other end as read."""

    def __init__(self, hub: JungHomeHub, target: SetupTarget) -> None:
        """Bind to `target`: the range's minimum or maximum."""
        super().__init__(hub, target)
        self._offset = (
            1 if target.entity == "lightness_min" else 3
        )  # after the status code

    @property
    def native_value(self) -> float | None:
        """The bound in %; None until the dimmer reported its range."""
        raw = self.setup_value
        return None if raw is None else lightness_percent(u16(raw, self._offset))

    async def async_set_native_value(self, value: float) -> None:
        """Send the range with this end moved; a minimum above the maximum is refused."""
        async with self.reader.modifying(
            self.address
        ):  # the other end's entity sends the same range
            raw = await self.async_current()
            bounds = {1: u16(raw, 1), 3: u16(raw, 3)}
            bounds[self._offset] = lightness_of(value)
            await self.async_write_setup(
                M.light_lightness_range_set, bounds[1], bounds[3]
            )


class JungHomeDefaultLightness(_LightnessPercent, _SwitchOnValue):
    """A dimmer's switch-on brightness (Light Lightness Default); unavailable while it uses the previous value (0)."""

    @property
    def native_value(self) -> float | None:
        """The switch-on brightness in %; None while the last brightness is used, or until read."""
        raw = self.setup_value
        if raw is None or (lightness := u16(raw)) == 0:
            return None
        return lightness_percent(lightness)

    async def async_set_native_value(self, value: float) -> None:
        """Set the switch-on brightness, which also stops using the previous value."""
        await self.async_write_setup(M.light_lightness_default_set, lightness_of(value))


class JungHomeDefaultColorTemp(_SwitchOnValue):
    """A DALI insert's switch-on colour temperature (Light CTL Default), within the range the light reported.

    The app's slider is a fixed 2000 to 10000 K; this one is the light's own Light CTL Temperature Range once read,
    which bounds every colour temperature the light takes, so a switch-on value outside it is not offered. Until
    then it is the app's range.
    """

    _attr_native_step = 100
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_mode = NumberMode.SLIDER

    @property
    def native_min_value(self) -> float:
        """The light's own minimum once read, else the app's."""
        st = self.hub.states.get(self.address)
        return st.kelvin_min if st and st.kelvin_min else DEFAULT_KELVIN_MIN

    @property
    def native_max_value(self) -> float:
        """The light's own maximum once read, else the app's."""
        st = self.hub.states.get(self.address)
        return st.kelvin_max if st and st.kelvin_max else DEFAULT_KELVIN_MAX

    @property
    def native_value(self) -> float | None:
        """The switch-on colour temperature; None until read."""
        raw = self.setup_value
        return None if raw is None else u16(raw, 2)

    async def async_set_native_value(self, value: float) -> None:
        """Send CTL Default with the switch-on brightness and the delta UV as read (the app's: `p234v7/S0.java`).

        The lightness is the Light Lightness Default the app sends (its capability's, `S0.java` `r()`), as last
        read or set; the CTL Default's own copy of it only when the dimmer has not reported that.
        """
        async with self.reader.modifying(
            self.address
        ):  # a read-modify-write, like the range's
            raw = await self.async_current()
            default = self.reader.cached_setup(self.address, LIGHTNESS_DEFAULT)
            await self.async_write_setup(
                M.light_ctl_default_set,
                u16(raw if default is None else default),
                int(value),
                u16(raw, 4, signed=True),
            )


class JungHomeColorTempBound(SetupStateEntity, NumberEntity):
    """One end of a DALI insert's colour-temperature range, the app's *White area* (Light CTL Temperature Range).

    As the app's range slider: 2000 to 10000 K in 100 K steps (`cells/Y.java` `k`), and a Set carries both ends, the
    other one as read, clamped into those limits as the app clamps both (`p234v7/V0.java`, `p097i9/b.java`). A
    minimum above the maximum is refused. The range is also the light's own slider limits
    (`JungHomeHub._on_ctl_range_status`): an applied change moves them. The installation's DALI insert neither
    answered nor applied a range Set on air (`hidden-features.md` §9): the read-back then fails the change as not
    applied rather than show a range the light does not have.
    """

    _attr_native_min_value = WHITE_RANGE_MIN_KELVIN
    _attr_native_max_value = WHITE_RANGE_MAX_KELVIN
    _attr_native_step = 100
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_mode = NumberMode.SLIDER

    def __init__(self, hub: JungHomeHub, target: SetupTarget) -> None:
        """Bind to `target`: the range's minimum or maximum."""
        super().__init__(hub, target)
        self._offset = (
            1 if target.entity == "color_temp_min" else 3
        )  # after the status code

    async def _read(self) -> bool:
        """Send no Get of its own: the connect-time refresh reads every CTL light's range (`STATE_GETS["ctl_range"]`)."""
        return self.setup_value is not None

    @property
    def native_value(self) -> float | None:
        """The bound in K; None until the light reported its range."""
        raw = self.setup_value
        return None if raw is None else u16(raw, self._offset)

    async def async_set_native_value(self, value: float) -> None:
        """Send the range with this end moved and the other as read."""
        async with self.reader.modifying(
            self.address
        ):  # the other end's entity sends the same range
            raw = await self.async_current()
            bounds = {
                offset: max(
                    WHITE_RANGE_MIN_KELVIN,
                    min(WHITE_RANGE_MAX_KELVIN, u16(raw, offset)),
                )
                for offset in (1, 3)
            }
            bounds[self._offset] = round(value)
            await self.async_write_setup(
                M.light_ctl_temperature_range_set, bounds[1], bounds[3]
            )


SETUP_NUMBERS: dict[
    str, type[_LightnessPercent | _SwitchOnValue | JungHomeColorTempBound]
] = {
    "lightness_min": JungHomeLightnessBound,
    "lightness_max": JungHomeLightnessBound,
    "default_lightness": JungHomeDefaultLightness,
    "default_color_temp": JungHomeDefaultColorTemp,
    "color_temp_min": JungHomeColorTempBound,
    "color_temp_max": JungHomeColorTempBound,
}
