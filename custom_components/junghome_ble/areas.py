"""Rooms to areas: which Home Assistant area a JUNG room's devices go to, and moving them.

Home Assistant puts a new device in the area its `suggested_area` names, created when no area has that *name*:
aliases are ignored, so a German room next to an English area with that alias became a second area. The flow's
`areas` step therefore maps each room to an area (`CONF_ROOM_AREAS`), prefilled with the area named or aliased like
the room (`matching_area`); a device's `suggested_area` is the name of its room's area (`area_name_for`). A room the
mapping does not know (made in the app later) gets the same matching at runtime; one the user left empty gets an
area named after it, as before.

`suggested_area` acts only when Home Assistant first registers a device. Moving devices that exist already is
`async_move_devices`: on a changed mapping (the reconfigure step) and, with `OPTION_SYNC_AREAS`, after an export
adoption or a room action changed a device's room (`model_update.async_sync_areas`). It never moves a device the
user placed: only one without an area, or in the very area the integration gave it.

The other way round, an action's `room_area` names a room by its area: `room_in_area` resolves it per entry, with the
same mapping (`room_area`), so an action and the devices agree on which room an area stands for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_ROOM_AREAS,
    DEFAULT_ASSIGN_AREAS,
    DOMAIN,
    OPTION_ASSIGN_AREAS,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def matching_area(hass: HomeAssistant, room: str) -> ar.AreaEntry | None:
    """Return the area named like `room`, else the first one with it as an alias (Home Assistant ignores case)."""
    registry = ar.async_get(hass)
    if (area := registry.async_get_area_by_name(room)) is not None:
        return area
    return next(iter(registry.async_get_areas_by_alias(room)), None)


def mapped_area(
    hass: HomeAssistant, options: Mapping[str, Any], room: str
) -> str | None:
    """Return the area id the `areas` step showed for `room`: the mapping's, else the matching area's; None: empty."""
    mapping: Mapping[str, str | None] = options.get(CONF_ROOM_AREAS) or {}
    if room in mapping:
        area_id = mapping[room]
        return (
            area_id if area_id and ar.async_get(hass).async_get_area(area_id) else None
        )
    area = matching_area(hass, room)
    return area.id if area is not None else None


def room_area(
    hass: HomeAssistant, options: Mapping[str, Any], room: str
) -> ar.AreaEntry | None:
    """Return the area `room` stands for, the one resolver of rooms and areas; None: none (yet).

    A room the mapping gives an area to stands for it; one it leaves empty (or whose area was deleted since) for
    the area named after the room; one the mapping does not know for the area named or aliased like it. `room` is
    the export's name of the room: the mapping is keyed by it.
    """
    mapping: Mapping[str, str | None] = options.get(CONF_ROOM_AREAS) or {}
    registry = ar.async_get(hass)
    if room in mapping:
        area_id = mapping[room]
        area = registry.async_get_area(area_id) if area_id else None
        return area if area is not None else registry.async_get_area_by_name(room)
    return matching_area(hass, room)


def area_name_for(
    hass: HomeAssistant, options: Mapping[str, Any], room: str | None
) -> str | None:
    """Return the area a device of `room` belongs in, by name (its `suggested_area`); None: no area.

    None without a room, or with `OPTION_ASSIGN_AREAS` off. The area the room stands for (`room_area`), else one
    named after the room.
    """
    if room is None or not options.get(OPTION_ASSIGN_AREAS, DEFAULT_ASSIGN_AREAS):
        return None
    area = room_area(hass, options, room)
    return area.name if area is not None else room


@dataclass(frozen=True)
class AreaRoom:
    """A room an action names by its area (`room_area`): which room that is, is up to each entry (`room_in_area`)."""

    area_id: str


class AmbiguousArea(Exception):
    """Several rooms of an entry stand for the area an action names (`room_in_area`)."""

    def __init__(self, area: str, rooms: list[str]) -> None:
        """Name the area and the rooms."""
        super().__init__(area, rooms)
        self.area = area
        self.rooms = rooms


def room_in_area(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    area: ar.AreaEntry,
    rooms: Iterable[str],
) -> str:
    """Return the room of an entry that `area` stands for, among its export's `rooms`: the one an action means.

    First a room the entry's mapping gives that area to; then a room whose area it is otherwise (`room_area`: named
    or aliased like it, or named like a room the mapping left empty); then a room the mapping gives the area to that
    the export no longer has; else the area's own name — the room called like it, or the one `create` makes. So a
    call never makes a new room for an area the mapping already gives a room. `AmbiguousArea` when several rooms
    come first.
    """
    mapping: Mapping[str, str | None] = options.get(CONF_ROOM_AREAS) or {}
    rooms = list(rooms)
    tiers = (
        [r for r in rooms if r in mapping and mapping[r] == area.id],
        [
            r
            for r in rooms
            if (found := room_area(hass, options, r)) is not None
            and found.id == area.id
        ],
        [r for r in mapping if r not in rooms and mapping[r] == area.id],
    )
    for tier in tiers:
        if len(tier) > 1:
            raise AmbiguousArea(area.name, tier)
        if tier:
            return tier[0]
    return area.name


def area_id_for(
    hass: HomeAssistant, options: Mapping[str, Any], room: str | None
) -> str | None:
    """Return the id of the area `area_name_for` names, None when it names none or it does not exist (yet)."""
    name = area_name_for(hass, options, room)
    if name is None:
        return None
    area = ar.async_get(hass).async_get_area_by_name(name)
    return area.id if area is not None else None


def async_move_devices(
    hass: HomeAssistant,
    entry_id: str,
    rooms: Mapping[str, tuple[str | None, str | None]],
    old_options: Mapping[str, Any],
    new_options: Mapping[str, Any],
) -> int:
    """Move devices to the area their room now gives them; return how many moved.

    `rooms` maps a device identifier (`entity.device_rooms`) to its room before and after the change, `old_options`
    and `new_options` are the entry's options before and after it. A device moves when it has no area, or sits in
    the area its old room gave it under the old options — never when the user put it elsewhere — and its new room
    names an area (created when missing, as Home Assistant does for a new device).
    """
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    moved = 0
    for ident, (old_room, room) in rooms.items():
        device = devices.async_get_device_by_identifier((DOMAIN, ident), entry_id)
        if device is None:
            continue
        given = area_id_for(hass, old_options, old_room)
        if device.area_id is not None and device.area_id != given:
            continue  # placed by the user
        name = area_name_for(hass, new_options, room)
        if name is None:
            continue
        area = areas.async_get_or_create(name)
        if area.id == device.area_id:
            continue
        _LOGGER.info("Moving %s to the area %s", device.name, area.name)
        devices.async_update_device(device.id, area_id=area.id)
        moved += 1
    return moved
