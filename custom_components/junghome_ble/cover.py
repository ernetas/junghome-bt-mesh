"""Covers: blinds, roller shutters and awnings driven through Generic Level elements.

UNVERIFIED ON HARDWARE. The maintainer owns no blinds: everything here is derived from the gateway firmware
(`docs/cross-repo-analysis.md` §1.4) and the decompiled app (`docs/gap-analysis/control-and-state.md` §2.6).
Please report what a real blind does.

Model. A blind (`jhmesh.devices.Blind`) is a *position* element and, when the node has one, a *slat* element,
both Generic Level servers. JUNG's level is **closedness**: level -32768 = 0 % = "Open", +32767 = 100 % =
"Closed", `pct = round((level + 32768) * 100 / 65535)`, the same for slats (0 % = slats open). Home Assistant's
position is percent *open*, so `position = 100 - pct`; `_to_ha` / `_to_level` are the single inversion point.
Nothing is inverted per operation mode: the app labels an awning "Open" / "Closed" exactly like a shutter, and
neither JUNG client looks at `0x1108` (blinds invert output) for the mapping — that property swaps the motor
relays inside the firmware ("relay outputs up/down controlled the opposite way, e.g. roof hatch"), after which
the app's "Open" is physically open again, so mirroring it here would double the inversion.

Device class. `0x1104 MOVE_OPERATION_MODE` (0 blinds, 1 shutter, 3 awning) decides it. The cover reads the
property once per link through the config entities' `PropertyReader` (rate-limited, after the state refresh),
and the vendor Status lands in the property cache; until it is known the cover is a `shutter` without tilt.
Mode 0 adds tilt from the slat element (the app shows the slat slider in that mode only).

Commands. Open / close / stop are what the gateway sends: `Generic Move Set` delta 0x8000 (up) / 0x7FFF (down)
/ 0 (stop) with a transition time (`const.COVER_MOVE_TRANSITION`). The app does the same with `Generic Delta
Set` -1 / +1 / 0 and no transition (`JungHomeHub.delta_level`; kept as the fallback should Move Set turn out
to be ignored). A position or tilt is a plain `Generic Level Set`, as the app's sliders send it. Stop ends a
move at an unknown position, so it is followed by a `Generic Level Get`. Nothing is written optimistically:
the state changes when the element publishes (or answers) its Generic Level Status, and `is_opening` /
`is_closing` come from target vs present level while they differ. Stopping the slats (`stop_cover_tilt`) is the
position's stop sent to the slat element: no JUNG client does it (the app has a slat slider only), it is the
plain Generic Level server semantics of the SIG spec (class c).

Locks. A blind whose lock function (`0x0009`) holds it — a lock, lock-out protection, a wind alarm — ignores
commands; the app disables its controls meanwhile (`control-and-state.md` §2.6). The cover refuses them with an
error that says so, when the lock state is known: the blind's *Wind alarm* sensor (or its lock entities) read it
once per link, and a lock found there is read again before refusing, since a timed one ends on its own.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .config_entities import PROPERTY_LOCK, property_reader
from .const import (
    COVER_LEVEL_CLOSED,
    COVER_LEVEL_OPEN,
    COVER_MODE_PROPERTY,
    COVER_MOVE_DOWN,
    COVER_MOVE_STOP,
    COVER_MOVE_TRANSITION,
    COVER_MOVE_UP,
    DOMAIN,
    REFRESH_RETRIES,
    SIGNAL_UPDATE,
)
from .entity import (
    JungHomeCentralEntity,
    JungHomeEntity,
    blind_device_info,
    room_loads,
)
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh.devices import ALL_BLINDS, ALL_SLATS, Blind

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import ElementState, JungHomeHub
    from .jhmesh.devices import Device

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0  # push-based; commands are serialised by the mesh client itself

MODE_SPEC = P.PROPERTIES[COVER_MODE_PROPERTY]
LOCK_SPEC = P.PROPERTIES[PROPERTY_LOCK]
DEVICE_CLASSES: dict[str, CoverDeviceClass] = {
    "blinds": CoverDeviceClass.BLIND,
    "shutter": CoverDeviceClass.SHUTTER,
    "awning": CoverDeviceClass.AWNING,
}
POSITION_FEATURES = (
    CoverEntityFeature.OPEN
    | CoverEntityFeature.CLOSE
    | CoverEntityFeature.STOP
    | CoverEntityFeature.SET_POSITION
)
TILT_FEATURES = (
    CoverEntityFeature.OPEN_TILT
    | CoverEntityFeature.CLOSE_TILT
    | CoverEntityFeature.SET_TILT_POSITION
)
SLAT_FEATURES = TILT_FEATURES | CoverEntityFeature.STOP_TILT  # one blind's slats


def level_to_closedness(level: int) -> int:
    """Return the JUNG percent (0 open .. 100 closed) of a Generic Level, the app's rounding (`control-and-state.md` §0)."""
    return max(0, min(100, round((level + 32768) * 100 / 65535)))


def closedness_to_level(pct: int) -> int:
    """Return the Generic Level of a JUNG percent: -32768 for 0 %, 32767 for 100 %."""
    return max(
        COVER_LEVEL_OPEN, min(COVER_LEVEL_CLOSED, round(-32768 + pct / 100 * 65535))
    )


def _to_ha(level: int) -> int:
    """Return the Home Assistant position / tilt (percent open) of a level."""
    return 100 - level_to_closedness(level)


def _to_level(position: int) -> int:
    """Return the level of a Home Assistant position / tilt (percent open)."""
    return closedness_to_level(100 - max(0, min(100, position)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one cover entity per blind, and the central *All blinds* (home and per room).

    Home-wide when blinds listen to their device-type group; per room for each room that has some (the app's area
    sheet; the slats are those of the room's blinds).
    """
    hub = entry.runtime_data
    entities: list[CoverEntity] = [
        JungHomeCover(hub, blind) for blind in hub.devices.blinds
    ]
    if members := hub.devices.central.get(ALL_BLINDS):
        entities.append(
            JungHomeAllBlinds(hub, members, hub.devices.central.get(ALL_SLATS, []))
        )
    entities += [
        JungHomeAllBlinds(hub, blinds, blinds, room)
        for room, blinds in room_loads(hub, Blind).items()
    ]
    add_entities(entities)


class JungHomeCover(JungHomeEntity, CoverEntity):
    """A blind, shutter or awning: position on one Generic Level element, slats (blinds mode) on a second."""

    _attr_name = None  # the device *is* the cover
    _attr_assumed_state = False
    _attr_translation_key = "blind"

    def __init__(self, hub: JungHomeHub, blind: Blind) -> None:
        """Bind to `blind`."""
        super().__init__(
            hub, blind.address, blind.unique_id, blind_device_info(hub, blind)
        )
        self.blind = blind
        self._mode_read_done = False
        self._mode_read_link: int | None = (
            None  # `hub.link_count` of the link the read was last queued on
        )
        self._mode_read_pending = False

    # ------------------------------------------------------------------ state
    def _state(self, addr: int) -> ElementState | None:
        return self.hub.states.get(addr)

    @property
    def operation_mode(self) -> str | int | None:
        """The decoded `0x1104` value (`blinds` / `shutter` / `awning`, a raw int when unmapped), None until read."""
        st = self._state(self.address)
        raw = st.properties.get(COVER_MODE_PROPERTY) if st else None
        if raw is None:
            return None
        # one byte, never cached empty (`config_entities._on_vendor_property_status`): always decodes
        mode: str | int = MODE_SPEC.codec.decode(raw)
        return mode

    @property
    def device_class(self) -> CoverDeviceClass:
        """`blind` / `shutter` / `awning` from the operation mode; a shutter until the mode is known (or unmapped)."""
        mode = self.operation_mode
        if isinstance(mode, str):
            return DEVICE_CLASSES.get(mode, CoverDeviceClass.SHUTTER)
        return CoverDeviceClass.SHUTTER

    @property
    def has_tilt(self) -> bool:
        """Whether slats are controllable: a slat element, and the device says it drives blinds."""
        return self.blind.slat_address is not None and self.operation_mode == "blinds"

    @property
    def supported_features(self) -> CoverEntityFeature:
        """Position control always; tilt once the device reported blinds mode."""
        return POSITION_FEATURES | (SLAT_FEATURES if self.has_tilt else 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The element addresses, the app rooms and the operation mode as the device reported it.

        The mode is one of the translated states (`blinds`, `shutter`, `awning`, `unknown`): a value the table
        does not know is `unknown` too, as the device class treats it.
        """
        mode = self.operation_mode
        attrs: dict[str, Any] = {
            "mesh_address": f"{self.address:04X}",
            "rooms": self.blind.rooms,
            "operation_mode": mode if isinstance(mode, str) else "unknown",
        }
        if self.blind.slat_address is not None:
            attrs["slat_address"] = f"{self.blind.slat_address:04X}"
        return attrs

    @property
    def current_cover_position(self) -> int | None:
        """Percent open from the position element's present level; None until heard from."""
        st = self._state(self.address)
        return _to_ha(st.level) if st and st.level is not None else None

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Percent open of the slats (blinds mode only); None until heard from."""
        if not self.has_tilt or self.blind.slat_address is None:
            return None
        st = self._state(self.blind.slat_address)
        return _to_ha(st.level) if st and st.level is not None else None

    @property
    def is_closed(self) -> bool | None:
        """Fully closed: position 0 (level 32767, the app's "Closed"); None until the position is known."""
        position = self.current_cover_position
        return None if position is None else position == 0

    @property
    def is_opening(self) -> bool:
        """The last status carried a target below the present level (toward -32768 = open)."""
        st = self._state(self.address)
        return (
            st is not None
            and st.level is not None
            and st.target_level is not None
            and st.target_level < st.level
        )

    @property
    def is_closing(self) -> bool:
        """The last status carried a target above the present level (toward 32767 = closed)."""
        st = self._state(self.address)
        return (
            st is not None
            and st.level is not None
            and st.target_level is not None
            and st.target_level > st.level
        )

    # ------------------------------------------------------------------ operation mode read
    async def async_added_to_hass(self) -> None:
        """Subscribe to updates (the slat element's too), then read the operation mode once the link is up."""
        await super().async_added_to_hass()
        if self.blind.slat_address is not None:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    SIGNAL_UPDATE.format(
                        self.hub.entry.entry_id, self.blind.slat_address
                    ),
                    self._handle_update,
                )
            )
        self._maybe_read_mode()

    @callback
    def _handle_update(self) -> None:
        self._maybe_read_mode()
        super()._handle_update()

    @callback
    def _maybe_read_mode(self) -> None:
        """Queue the `0x1104` read when the link is up and the mode is not known yet — once per link.

        A drive that did not answer (asleep, out of range, drowned out by the connect-time traffic) is asked again
        on the next link, not on the next update of this one; until it answers the cover is a shutter without tilt.
        """
        if (
            self._mode_read_done
            or self._mode_read_pending
            or not self.hub.connected
            or self._mode_read_link == self.hub.link_count
        ):
            return
        self._mode_read_pending = True
        self._mode_read_link = self.hub.link_count
        property_reader(self.hass, self.hub).schedule(self.address, self._read_mode)

    async def _read_mode(self) -> None:
        try:
            self._mode_read_done = await property_reader(self.hass, self.hub).read(
                self.address, MODE_SPEC
            )
        finally:
            self._mode_read_pending = False

    # ------------------------------------------------------------------ locks
    def _lock(self) -> P.EnforcedOutput | None:
        """Return the cached lock state of the blind; None while unknown (or malformed)."""
        raw = property_reader(self.hass, self.hub).cached(self.address, LOCK_SPEC)
        if raw is None:
            return None
        try:
            value: P.EnforcedOutput = LOCK_SPEC.codec.decode(raw)
        except ValueError:
            return None
        return value

    async def _check_unlocked(self) -> None:
        """Refuse a command while the blind reports a lock (it would ignore it): read again first, it may have ended."""
        value = self._lock()
        if value is None or not value.locked:
            return
        await property_reader(self.hass, self.hub).read(
            self.address, LOCK_SPEC, since=time.monotonic()
        )
        value = self._lock()
        if value is None or not value.locked:
            return
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cover_wind_alarm" if value.wind_alarm else "cover_locked",
            translation_placeholders={"entity": self.entity_id},
        )

    # ------------------------------------------------------------------ commands
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Run up: Generic Move Set 0x8000 (the app: Generic Delta Set -1)."""
        await self._check_unlocked()
        await self._send(
            self.hub.move_level(self.address, COVER_MOVE_UP, COVER_MOVE_TRANSITION)
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Run down: Generic Move Set 0x7FFF (the app: Generic Delta Set +1)."""
        await self._check_unlocked()
        await self._send(
            self.hub.move_level(self.address, COVER_MOVE_DOWN, COVER_MOVE_TRANSITION)
        )

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop: Generic Move Set 0 (the app: Generic Delta Set 0), then ask where the blind ended up.

        Whether the firmware publishes a status after a stop is unknown (`control-and-state.md` §5), so the position
        is read back; off the service call, as a silent element takes the Get's retries to give up.
        """
        await self._check_unlocked()
        await self._stop(self.address)

    async def _stop(self, addr: int) -> None:
        await self._send(
            self.hub.move_level(addr, COVER_MOVE_STOP, COVER_MOVE_TRANSITION)
        )
        self.hub.entry.async_create_background_task(
            self.hass, self._request_level(addr), f"{DOMAIN} cover refresh"
        )

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Send Generic Level Set with the closedness level of the position asked for."""
        await self._check_unlocked()
        await self._send(
            self.hub.set_level(self.address, _to_level(int(kwargs[ATTR_POSITION])))
        )

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Slats fully open: Generic Level Set -32768 to the slat element."""
        await self._set_slats(COVER_LEVEL_OPEN)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Slats fully closed: Generic Level Set 32767 to the slat element."""
        await self._set_slats(COVER_LEVEL_CLOSED)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Send Generic Level Set with the closedness level of the tilt asked for."""
        await self._set_slats(_to_level(int(kwargs[ATTR_TILT_POSITION])))

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        """Stop the slats: Generic Move Set 0 to the slat element, then ask where they ended up (class c, see above)."""
        if self.blind.slat_address is None:
            return  # the tilt services are not offered without a slat element (`supported_features`)
        await self._check_unlocked()
        await self._stop(self.blind.slat_address)

    async def _set_slats(self, level: int) -> None:
        if self.blind.slat_address is None:
            return  # the tilt services are not offered without a slat element (`supported_features`)
        await self._check_unlocked()
        await self._send(self.hub.set_level(self.blind.slat_address, level))

    async def _request_level(self, addr: int) -> None:
        """Send a Generic Level Get, best effort: a silent element or a lost link is logged, the next status will tell."""
        try:
            await self.hub.proxy.request(
                addr, M.generic_level_get(), M.GEN_LEVEL_STATUS, retries=REFRESH_RETRIES
            )
        except TimeoutError:
            _LOGGER.debug(
                "%04X did not answer its Generic Level Get after a stop", addr
            )
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("Generic Level Get to %04X not sent: %s", addr, err)


def _mean(values: list[int]) -> int | None:
    return round(sum(values) / len(values)) if values else None


class JungHomeAllBlinds(JungHomeCentralEntity, CoverEntity):
    """The app's "all blinds": one Unacknowledged Generic Level Set to 0xFEF6 (positions) or 0xFEF7 (slats).

    Open and close are the levels of 0 % and 100 % closed, as the app sends them; stop is an Unacknowledged
    Generic Delta Set 0 to 0xFEF6, after which every member is asked where it stopped. The position and tilt are
    the means of the members' (HA's cover groups do the same); closed when every known member is. Tilt is offered
    when slat elements listen to 0xFEF7. The blinds of a room: the positions and slats one Level Set per element,
    the stop one Delta Set 0 to the room address (`OpenClose.Group`). **Unverified on hardware**, like the blinds
    themselves.
    """

    def __init__(
        self,
        hub: JungHomeHub,
        members: list[Device],
        slats: list[Device],
        room: int | None = None,
    ) -> None:
        """Bind to the blinds' device-type groups, or to the blinds of `room`."""
        super().__init__(hub, ALL_BLINDS, members, "blinds", room)
        self._slats = [
            b.slat_address for b in slats if isinstance(b, Blind) and b.slat_address
        ]
        self._attr_supported_features = POSITION_FEATURES | (
            TILT_FEATURES if self._slats else 0
        )

    @property
    def watched(self) -> list[int]:
        """The position elements, and the slat elements when the group has slats."""
        return [*super().watched, *self._slats]

    @property
    def _positions_at(self) -> list[int]:
        """The members' position elements."""
        return super().watched

    def _positions(self, addresses: list[int]) -> list[int]:
        return [
            _to_ha(st.level)
            for a in addresses
            if (st := self.hub.states.get(a)) and st.level is not None
        ]

    @property
    def current_cover_position(self) -> int | None:
        """The members' mean position (percent open); None until one reported."""
        return _mean(self._positions(self._positions_at))

    @property
    def current_cover_tilt_position(self) -> int | None:
        """The slats' mean tilt (percent open); None until one reported."""
        return _mean(self._positions(self._slats))

    @property
    def is_closed(self) -> bool | None:
        """Every member that reported is closed; None until one reported."""
        positions = self._positions(self._positions_at)
        return all(p == 0 for p in positions) if positions else None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Every blind fully open (0 % closed)."""
        await self._level(ALL_BLINDS, self._positions_at, COVER_LEVEL_OPEN)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Every blind fully closed (100 %)."""
        await self._level(ALL_BLINDS, self._positions_at, COVER_LEVEL_CLOSED)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Every blind to the position asked for."""
        level = _to_level(int(kwargs[ATTR_POSITION]))
        await self._level(ALL_BLINDS, self._positions_at, level)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop every blind, then ask each where it stopped (off the service call)."""
        await self._send(self.hub.central_stop(self.address))
        self.hub.entry.async_create_background_task(
            self.hass, self._read_positions(), f"{DOMAIN} all blinds refresh"
        )

    async def _read_positions(self) -> None:
        for address in self.watched:
            await self.hub.async_refresh_element(address, "level")

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Every slat fully open."""
        await self._level(ALL_SLATS, self._slats, COVER_LEVEL_OPEN)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Every slat fully closed."""
        await self._level(ALL_SLATS, self._slats, COVER_LEVEL_CLOSED)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Every slat to the tilt asked for."""
        level = _to_level(int(kwargs[ATTR_TILT_POSITION]))
        await self._level(ALL_SLATS, self._slats, level)
