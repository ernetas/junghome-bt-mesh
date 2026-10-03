"""Enumerated device parameters, LED colours and mini-actuator input edge behaviours as config entities.

The parameters: dim mode, blind / thermostat / detector modes, and every load's behaviour after mains return
(SIG Generic OnPowerUp). And a blind's *Lock function* (0x0009): unlocked, locked, lock-out protection or wind
alarm, the lock functions of the app's blind page (`JungHomeLockFunction`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from .config_entities import (
    LED_SYNC_PAIRS,
    PROPERTY_LOCK,
    EdgeDetectionEntity,
    EdgeDetectionTarget,
    LockFunctionEntity,
    PropertyEntity,
    PropertyTarget,
    SetupStateEntity,
    blind_targets,
    config_targets,
    edge_detection_targets,
    lock_mode,
    setup_targets,
)
from .const import DOMAIN
from .entity import async_setup_platform
from .jhmesh import messages as M
from .jhmesh import properties as P

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub

PARALLEL_UPDATES = (
    0  # push-based; the property reader serialises the mesh exchanges per element
)

UNKNOWN_OPTION = "unknown"  # the catalogue's name for a wire value the app cannot set
# Generic OnPowerUp 0 / 1 / 2, the app's Switched OFF / Switched ON / Previous state (`device-settings.md` S5)
POWER_ON_OPTIONS = ["off", "on", "restore"]
# Where the app offers fewer options than the catalogue names: a blind's behaviour after mains return has no *stop*
# and no *position for network failure* (`C1846b.java:112-118`, `device-settings.md` §7.2). A value outside them,
# set elsewhere, shows as unknown. Unverified on air: no blind here.
OFFERED_OPTIONS: dict[int, list[str]] = {
    0x1105: ["no_reaction", "move_up", "move_down", "move_to_stored_position"],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of `build_entities`, kept with the hub to follow a new export in place (`model_update`)."""
    async_setup_platform(entry.runtime_data, "select", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[SelectEntity]:
    """Return one select per enumerated property and LED colour, per edge of every input, per load's power-up."""
    entities: list[SelectEntity] = []
    for target in config_targets(hub, "select"):
        if isinstance(target.spec.codec, P.RgbMode):
            entities.append(JungHomeLedColour(hub, target))
        else:
            entities.append(JungHomePropertySelect(hub, target))
    entities += [
        JungHomeEdgeBehaviour(hub, target)
        for target in edge_detection_targets(hub, "select")
    ]
    entities += [
        JungHomePowerOnBehaviour(hub, target) for target in setup_targets(hub, "select")
    ]
    entities += [
        JungHomeLockFunction(hub, target)
        for target in blind_targets(
            hub, PROPERTY_LOCK, "lock_function", enabled_default=False
        )
    ]
    return entities


class JungHomePropertySelect(PropertyEntity, SelectEntity):
    """An enumerated property: the options are the catalogue's value names (minus "unknown"), or the app's few."""

    def __init__(self, hub: JungHomeHub, target: PropertyTarget) -> None:
        """Bind to `target`."""
        super().__init__(hub, target)
        codec = target.spec.codec
        assert isinstance(codec, P.Enum)
        self._attr_options = list(
            OFFERED_OPTIONS.get(
                target.spec.id, [o for o in codec.options if o != UNKNOWN_OPTION]
            )
        )

    @property
    def current_option(self) -> str | None:
        """The decoded name; None until known, or when the device reports a value outside the table."""
        value = self.property_value
        return value if isinstance(value, str) else None

    async def async_select_option(self, option: str) -> None:
        """Write the option."""
        await self.async_write_value(option)


class JungHomeLedColour(PropertyEntity, SelectEntity):
    """The colour of a key / socket LED when the load is on or off: the app's palette for the product.

    The mode byte (night mode) of the current value is kept, as the app does when only the colour changes: it is
    read first when not known, never guessed (a guess would switch night mode off).

    While a 2-gang node's LED colours are synchronised (`switch.JungHomeLedColourSync`), a colour of LED 1 is
    written to LED 2 as well, right after LED 1's (the app's order, 0xA001 then 0xA004), and LED 2's selects are
    unavailable: the app hides them.
    """

    def __init__(self, hub: JungHomeHub, target: PropertyTarget) -> None:
        """Bind to `target`."""
        super().__init__(hub, target)
        self._palette = P.led_palette(target.node.pid or 0)
        self._attr_options = list(self._palette)

    @property
    def synchronised(self) -> bool:
        """Whether LED 1's colours are copied to LED 2 on this node."""
        return self.reader.led_sync.get(self.address, False)

    @property
    def available(self) -> bool:
        """Unavailable while it is LED 2's colour and the node's colours are synchronised."""
        if self.synchronised and self.spec.id in LED_SYNC_PAIRS.values():
            return False
        return super().available

    @property
    def current_option(self) -> str | None:
        """The palette name of the current colour; None until known, or for a colour the app cannot name."""
        value = self.property_value
        if not isinstance(value, P.LedMode):
            return None
        return P.led_colour_name(value.rgb, self.target.node.pid or 0)

    async def async_select_option(self, option: str) -> None:
        """Write the colour, keeping the current night-mode flag."""
        async with self.changing():  # the night-mode switch rewrites it too
            current = self.property_value
            if not isinstance(current, P.LedMode):
                await self.read_current(self.spec)
                current = self.property_value
            if not isinstance(current, P.LedMode):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="led_colour_unknown",
                    translation_placeholders={"entity": self.entity_id},
                )
            value = P.LedMode(*self._palette[option], night_mode=current.night_mode)
            await self.async_write_value(value)
            if self.synchronised and (copy := LED_SYNC_PAIRS.get(self.spec.id)):
                await self.async_write_value(value, P.PROPERTIES[copy])


class JungHomeEdgeBehaviour(EdgeDetectionEntity, SelectEntity):
    """What a rising or falling edge of a mini-actuator input does (acts only with *Edge evaluation* on)."""

    _attr_options = list(P.EDGE_BEHAVIOUR.values())

    def __init__(self, hub: JungHomeHub, target: EdgeDetectionTarget) -> None:
        """Bind to `target` (its part is "rising" or "falling")."""
        super().__init__(hub, target)
        self._part = target.part

    @property
    def current_option(self) -> str | None:
        """The edge's behaviour; None until the input reported it."""
        value = self.edge_detection
        return getattr(value, self._part) if value else None

    async def async_select_option(self, option: str) -> None:
        """Write the behaviour, keeping the mode and the other edge."""
        await self.async_write_part(**{self._part: option})


class JungHomePowerOnBehaviour(SetupStateEntity, SelectEntity):
    """What a light or socket does when mains voltage returns: off, on, or its state before (Generic OnPowerUp)."""

    _attr_options = POWER_ON_OPTIONS

    @property
    def current_option(self) -> str | None:
        """The behaviour; None until read, or for a value the spec does not define."""
        raw = self.setup_value
        return (
            POWER_ON_OPTIONS[raw[0]] if raw and raw[0] < len(POWER_ON_OPTIONS) else None
        )

    async def async_select_option(self, option: str) -> None:
        """Send Generic OnPowerUp Set."""
        await self.async_write_setup(
            M.generic_onpowerup_set, POWER_ON_OPTIONS.index(option)
        )


UNLOCKED = "unlocked"
LOCK_FUNCTIONS = [UNLOCKED, "keep_state", "lockout_protection", "wind_alarm"]


class JungHomeLockFunction(LockFunctionEntity, SelectEntity):
    """A blind's lock functions, as its page in the app offers them (`control-and-state.md` §2.6, §2.11).

    *Unlocked* sends command 0 with the fields last read, like the *Lock* switch's off; *Locked* (keep the current
    position) `02 01 <t>` and *Lock-out protection* `02 FE <t>`, both for the time the load's *Lock time limit*
    sets (none by default); *Wind alarm* `01 FF 00 00 00 00`, enforce level 0 (open) with the wind-alarm priority
    and no time limit (`network-logic.md` §4.2; open question: whether the value bytes are a Generic Level,
    `control-and-state.md` §5 q. 7). A lock another controller set with a value of its own shows as no option.

    UNVERIFIED ON AIR (class b, the decompiled app's `LockFunctionViewModelDelegate`): the installation has no
    blind, so the entity is disabled by default.
    """

    _attr_options = LOCK_FUNCTIONS

    @property
    def current_option(self) -> str | None:
        """The lock the blind reports; None until read, or for a lock the app has no name for (an enforced value)."""
        value = self.lock
        if value is None:
            return None
        if not value.locked:
            return UNLOCKED
        mode = lock_mode(value)
        return mode if mode in LOCK_FUNCTIONS else None

    async def async_select_option(self, option: str) -> None:
        """Send the lock function."""
        time_s = self.reader.lock_time_limits.get(self.address, 0)
        if option == UNLOCKED:
            await self.async_unlock()
        elif option == "wind_alarm":
            await self.async_lock(P.WIND_ALARM)
        else:
            await self.async_lock(
                P.lock_output(time_s, lockout=option == "lockout_protection")
            )
