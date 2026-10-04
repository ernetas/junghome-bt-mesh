"""Schedules on the loads themselves: the JH Scheduler (`0x0527:1016`) every load element hosts.

The app's *Automation* page (`docs/gap-analysis/network-features.md` §4.1): up to 16 slots per element, each a
timed, sunrise or sunset trigger on a set of weekdays with an action — switch on / off, a lightness, a lightness and
colour temperature, a blind and slat position, a target temperature. The node runs them itself, on the time Home
Assistant sends it (`coordinator._send_time`); nothing about them is in the export, the node is the only record.

`Scheduler` (one per hub, `scheduler`) reads and changes an element's slots and keeps what it last read; the
*Schedules* sensor shows that (read once per link) and the `get_schedules` … `delete_schedule` actions
(`actions/schedules.py`; `update_schedule` rewrites a slot in place, review-4 F4-6) go through it. An astro schedule is preceded by Home Assistant's home location, the way the app
sends the phone's: a `Generic Location Global Set Unacknowledged` to the node's Location Setup Server (the hub
also broadcasts it after every connection, `coordinator._send_location`).

Formats are the app's; only empty slots have been seen on air (`docs/hidden-features.md` §10). Not tried on a device
yet: whether a Set is answered by its Status (a silent one is read back) and the open bound of an astro window
(`vendor_models.UNSET_TIME`).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from . import const
from .config_entities import EntityTarget
from .const import (
    DOMAIN,
    PROPERTY_READ_RETRIES,
)
from .conversions import closedness_to_level, level_to_closedness
from .data import jung_data
from .entity import (
    blind_device_info,
    light_device_info,
    node_device_info,
    socket_device_info,
)
from .errors import mesh_errors
from .jhmesh import messages as M
from .jhmesh import vendor_models as V
from .jhmesh.devices import Blind, Light, Socket, Thermostat
from .jhmesh.vendor_models import (
    SCHEDULER_MODEL,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo

    from .config_entities import Page
    from .coordinator import JungHomeHub
    from .jhmesh.client import AccessMessage
    from .jhmesh.devices import Device
    from .jhmesh.properties import PropertySpec

_LOGGER = logging.getLogger(__name__)

LOCATION_SETUP_MODEL = "100F"
# trigger -> (type while disabled, type while enabled), `vendor_models.SCHEDULE_TYPES`
TRIGGERS: dict[str, tuple[int, int]] = {
    "time": (2, 3),
    "sunrise": (4, 5),
    "sunset": (6, 7),
}
TRIGGER_OF = {t: name for name, types in TRIGGERS.items() for t in types}
ENABLED_TYPES = frozenset(enabled for _, enabled in TRIGGERS.values())
AVAILABLE = 0  # the slot type, and the list status, of a free slot
# the list states the app shows (inactive, active); 1 is a slot of a central scheduler, hidden like the app does
LISTED = frozenset({2, 3})

# the action fields each kind of load takes (`schedule_action`); `get_schedules` answers in the same terms
ACTION_FIELDS: dict[str, frozenset[str]] = {
    "switch": frozenset({"action"}),
    "socket": frozenset({"action"}),
    "dimmer": frozenset({"action", "brightness_pct"}),
    "ctl": frozenset({"action", "brightness_pct", "color_temp_kelvin"}),
    "blind": frozenset({"position", "tilt_position"}),
    "thermostat": frozenset({"temperature"}),
}
ALL_ACTION_FIELDS = frozenset().union(*ACTION_FIELDS.values())


class ActionError(ValueError):
    """The action fields of a schedule do not fit the load: `key` names what is wrong, as an exception translation.

    The message says the same in English (for the log); the caller adds the load's `name` to `placeholders`.
    """

    def __init__(self, key: str, message: str, **placeholders: str) -> None:
        """Carry the translation `key` and its `placeholders` besides the English `message`."""
        super().__init__(message)
        self.key = key
        self.placeholders = placeholders


def _percent(lightness: int) -> int:
    return round(lightness * 100 / V.LIGHTNESS_MAX)


def _open_percent(level: int | None) -> int:
    """Home Assistant's position (percent open) of a Generic Level, as the cover shows it."""
    return 100 - level_to_closedness(level or 0)


def schedule_action(kind: str, data: Mapping[str, Any]) -> V.Action:
    """Build the slot's action for a load of `kind` from the call's fields, the way the app picks one per device class.

    Switch inserts and sockets switch; dimmers set a lightness (0 = off, 100 % when on without one); tunable-white
    lights add the colour temperature when one is given; a blind moves to a position (its slats to `tilt_position`,
    else the same percentage); a thermostat takes a target temperature.
    """
    given = {name for name in ALL_ACTION_FIELDS if name in data}
    allowed = ACTION_FIELDS[kind]
    if extra := sorted(given - allowed):
        fields = ", ".join(extra)
        raise ActionError(
            "schedule_action_not_applicable",
            f"{fields} does not apply to a {kind} load",
            fields=fields,
        )
    if kind == "thermostat":
        if "temperature" not in data:
            raise ActionError(
                "schedule_action_needs_temperature", "a thermostat needs a temperature"
            )
        # centi-degrees on air: the value the element answers with, for the comparison after the write
        centi = round(float(data["temperature"]) * 100)
        return V.Action(V.ACTION_TEMPERATURE, temperature_c=centi / 100)
    if kind == "blind":
        if "position" not in data:
            raise ActionError(
                "schedule_action_needs_position", "a blind needs a position"
            )
        position = int(data["position"])
        tilt = int(data.get("tilt_position", position))
        return V.Action(
            V.ACTION_BLINDS,
            blind=closedness_to_level(100 - position),
            slat=closedness_to_level(100 - tilt),
        )
    on = data.get("action", "on" if "brightness_pct" in data else None)
    if on is None:
        raise ActionError(
            "schedule_action_needs_action", f"a {kind} load needs an action (on or off)"
        )
    if on == "off" and "brightness_pct" in data:
        raise ActionError(
            "schedule_action_brightness_off", "brightness_pct goes with the action on"
        )
    if kind in ("switch", "socket"):
        return V.Action(V.ACTION_SWITCH, on=on == "on")
    lightness = (
        round(int(data.get("brightness_pct", 100)) * V.LIGHTNESS_MAX / 100)
        if on == "on"
        else 0
    )
    if "color_temp_kelvin" in data:
        # the app's 100 K steps
        kelvin = round(int(data["color_temp_kelvin"]) / 100) * 100
        return V.Action(
            V.ACTION_LIGHTNESS_CT, lightness=lightness, temperature_k=kelvin
        )
    return V.Action(V.ACTION_LIGHTNESS, lightness=lightness)


def action_fields(action: V.Action | None) -> dict[str, Any]:
    """Return a slot's action in the fields `create_schedule` takes (the inverse of `schedule_action`)."""
    if action is None:
        return {}
    code = action.code
    if code == V.ACTION_SWITCH:
        return {"action": "on" if action.on else "off"}
    if code in (V.ACTION_LIGHTNESS, V.ACTION_LIGHTNESS_CT):
        lightness = action.lightness or 0
        out: dict[str, Any] = {"action": "on" if lightness else "off"}
        if lightness:
            out["brightness_pct"] = _percent(lightness)
        if code == V.ACTION_LIGHTNESS_CT:
            out["color_temp_kelvin"] = action.temperature_k
        return out
    if code == V.ACTION_BLINDS:
        return {
            "position": _open_percent(action.blind),
            "tilt_position": _open_percent(action.slat),
        }
    if code == V.ACTION_TEMPERATURE:
        return {"temperature": action.temperature_c}
    return {"action": "none"}


def _hhmm(value: tuple[int, int]) -> str | None:
    return None if value == V.UNSET_TIME else f"{value[0]:02d}:{value[1]:02d}"


def _bound(value: dt_time | None) -> tuple[int, int]:
    return V.UNSET_TIME if value is None else (value.hour, value.minute)


@dataclass(frozen=True)
class Slot:
    """One used slot of an element: when it fires, what it does, and when the node computed it fires next."""

    schedule: V.Schedule
    action: V.Action | None = None
    effective: V.EffectiveTime | None = None

    @property
    def index(self) -> int:
        """The slot number, 0..15."""
        return self.schedule.index

    @property
    def enabled(self) -> bool:
        """Whether the slot fires (its type is an *active* one)."""
        return self.schedule.type in ENABLED_TYPES

    def as_dict(self) -> dict[str, Any]:
        """Return the slot for the sensor's attribute and the `get_schedules` response."""
        s = self.schedule
        trigger = TRIGGER_OF.get(s.type, s.type_name)
        out: dict[str, Any] = {
            "slot": s.index,
            "trigger": trigger,
            "enabled": self.enabled,
            "weekdays": [d for d in V.DAYS if d in s.days],
        }
        if trigger == "time":
            out["time"] = _hhmm(s.not_before)
        else:
            out |= {
                "not_before": _hhmm(s.not_before),
                "not_after": _hhmm(s.not_after),
                "offset": s.offset_min,
            }
            if self.effective is not None:
                out["effective_time"] = _hhmm(self.effective.time)
        return out | action_fields(self.action)


def build_schedule(index: int, data: Mapping[str, Any]) -> V.Schedule:
    """Return the slot contents a `create_schedule` call asks for (the schema checked which fields go together)."""
    trigger = data["trigger"]
    kind = TRIGGERS[trigger][bool(data.get("enabled", True))]
    days = frozenset(data.get("weekdays", V.DAYS))
    if trigger == "time":
        at = _bound(data["time"])
        return V.Schedule(index, kind, days, at, at, 0)
    return V.Schedule(
        index,
        kind,
        days,
        _bound(data.get("not_before")),
        _bound(data.get("not_after")),
        int(data.get("offset", 0)),
    )


def _error(
    key: str, cls: type[HomeAssistantError] = HomeAssistantError, **placeholders: str
) -> HomeAssistantError:
    return cls(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


class Scheduler:
    """The JH Scheduler side of one hub: reads and writes of the load elements' slots, one exchange per element."""

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to `hub`; nothing is read until asked."""
        self.hub = hub
        self.slots: dict[
            int, list[Slot]
        ] = {}  # element -> its used slots, as last read or written
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def hosts(self, address: int) -> bool:
        """Whether the element at `address` has a JH Scheduler."""
        element = self.hub.cdb.element(address)
        return element is not None and SCHEDULER_MODEL in element.models

    async def _ask(
        self, address: int, pdu: bytes, header: int, *, write: bool = False
    ) -> AccessMessage | None:
        """Send a JH Scheduler Get / Set and return the element's Status for the same slot and sub-command.

        None when it stays silent (a Set is given one short attempt: firmware may publish instead of replying);
        a lost link is a send failure.
        """
        timeout = const.PROPERTY_WRITE_TIMEOUT if write else const.PROPERTY_READ_TIMEOUT
        with mesh_errors():
            try:  # silence is caught here, as None: `mesh_errors` would call it send_failed
                return await self.hub.proxy.request(
                    address,
                    pdu,
                    V.JH_SCHEDULER_STATUS,
                    timeout=timeout,
                    retries=1 if write else PROPERTY_READ_RETRIES,
                    expect_cid=M.JUNG_CID,
                    match=lambda m: m.params[:1] == bytes([header]),
                )
            except TimeoutError:
                return None

    async def _get(self, address: int, index: int, sub: int) -> V.SchedulerStatus:
        header = (sub << 4) | (0 if sub == V.SUB_LIST else index)
        pdu = (
            V.scheduler_list_get() if sub == V.SUB_LIST else V.scheduler_get(index, sub)
        )
        reply = await self._ask(address, pdu, header)
        if reply is None:
            raise _error("schedule_no_reply", address=f"{address:04X}")
        return V.decode_scheduler_status(reply.params)

    async def _read_slot(self, address: int, index: int) -> Slot | None:
        """Read one slot's schedule, action and effective time; None when it is free."""
        schedule = (await self._get(address, index, V.SUB_SCHEDULE)).schedule
        if schedule is None or schedule.type == AVAILABLE:
            return None
        action = (await self._get(address, index, V.SUB_ACTION)).action
        effective = (await self._get(address, index, V.SUB_EFFECTIVE_TIME)).effective
        return Slot(schedule, action, effective)

    def _store(self, address: int, index: int, slot: Slot | None) -> None:
        """Put what slot `index` now holds (None: nothing) into the cache and tell the element's entities."""
        others = [s for s in self.slots.get(address, []) if s.index != index]
        self._replace(address, others if slot is None else [*others, slot])

    def _replace(self, address: int, slots: list[Slot]) -> None:
        self.slots[address] = sorted(slots, key=lambda s: s.index)
        self.hub.notify_update(address)

    async def _list(self, address: int) -> tuple[int, ...]:
        """Read the state of the element's 16 slots.

        A Status without them (too short, or the answer to a sub-command the firmware does not know) is no answer:
        taken as no slots it would cache "no schedules", or refuse a new one as if every slot were used.
        """
        slots = (await self._get(address, 0, V.SUB_LIST)).slots
        if slots is None:
            raise _error("schedule_no_reply", address=f"{address:04X}")
        return slots

    async def _free_slot(self, address: int) -> int:
        slots = await self._list(address)
        index = next((i for i, s in enumerate(slots) if s == AVAILABLE), None)
        if index is None:
            raise _error("schedule_slots_full", address=f"{address:04X}")
        return index

    async def free_slot(self, address: int) -> int:
        """Return the element's first free slot, the one `create` would write now; raise when there is none."""
        async with self._locks[address]:
            return await self._free_slot(address)

    async def read(self, address: int) -> list[Slot]:
        """Read every listed slot of the element: the list, then each slot's schedule, action and effective time."""
        async with self._locks[address]:
            slots = [
                slot
                for index, status in enumerate(await self._list(address))
                if status in LISTED
                and (slot := await self._read_slot(address, index)) is not None
            ]
            self._replace(address, slots)
            return self.slots[address]

    async def _write_schedule(
        self, address: int, pdu: bytes, want: V.Schedule | int
    ) -> None:
        """Send a schedule Set and confirm it from its Status or a Get: the full slot, or just its type (an int)."""
        index = pdu[3] & 0xF

        def confirms(status: V.SchedulerStatus | None) -> bool:
            got = None if status is None else status.schedule
            if isinstance(want, int):
                return got is not None and got.type == want
            return got == want

        reply = await self._ask(address, pdu, pdu[3], write=True)
        status = None if reply is None else V.decode_scheduler_status(reply.params)
        if not confirms(status):
            _LOGGER.debug(
                "%04X: no Status for slot %d, reading it back", address, index
            )
            status = await self._get(address, index, V.SUB_SCHEDULE)
        if not confirms(status):
            raise _error(
                "schedule_not_applied", address=f"{address:04X}", slot=str(index)
            )

    async def _write_action(self, address: int, index: int, action: V.Action) -> None:
        pdu = V.scheduler_action_set(index, action)
        reply = await self._ask(address, pdu, pdu[3], write=True)
        got = None if reply is None else V.decode_scheduler_status(reply.params).action
        if got != action:
            _LOGGER.debug(
                "%04X: no Status for the action of slot %d, reading it back",
                address,
                index,
            )
            got = (await self._get(address, index, V.SUB_ACTION)).action
        if got != action:
            raise _error(
                "schedule_not_applied", address=f"{address:04X}", slot=str(index)
            )

    async def _send_location(self, address: int) -> None:
        """Tell the node where it is (Home Assistant's home), for its sunrise / sunset times; skipped without a server."""
        element = self.hub.cdb.element(address)
        server = next(
            (
                e
                for e in (element.node.elements if element else ())
                if LOCATION_SETUP_MODEL in e.models
            ),
            None,
        )
        if server is None:
            _LOGGER.debug(
                "%04X: no Location Setup Server to send the home location to", address
            )
            return
        config = self.hub.hass.config
        with mesh_errors():
            await self.hub.proxy.send_access(
                server.address,
                M.generic_location_global_set(
                    config.latitude, config.longitude, int(config.elevation)
                ),
            )

    async def create(
        self, address: int, data: Mapping[str, Any], action: V.Action
    ) -> Slot:
        """Write a new schedule into the element's first free slot, as the app does: location, schedule, action.

        The schedule goes in inactive and is made active (a type-only Set) once its action is in: the slot must
        not fire with whatever action it held before (a deleted schedule's). When any write fails, the slot is
        freed again, if the element lets us: it was free before.
        """
        async with self._locks[address]:
            index = await self._free_slot(address)
            schedule = build_schedule(index, data)
            inactive = replace(schedule, type=TRIGGERS[data["trigger"]][False])
            if data["trigger"] != "time":
                await self._send_location(address)
            try:
                await self._write_schedule(address, inactive.encode(), inactive)
                await self._write_action(address, index, action)
                if schedule.type != inactive.type:
                    await self._write_schedule(
                        address,
                        V.scheduler_type_set(index, schedule.type),
                        schedule.type,
                    )
            except HomeAssistantError:
                try:
                    await self._write_schedule(
                        address, V.scheduler_type_set(index, AVAILABLE), AVAILABLE
                    )
                except HomeAssistantError:
                    _LOGGER.warning(
                        "%04X: slot %d was not freed again; it may hold the new schedule, active only if its action is in",
                        address,
                        index,
                    )
                raise
            slot = Slot(schedule, action)
            self._store(address, index, slot)
            return slot

    async def _slot(self, address: int, index: int) -> V.Schedule:
        schedule = (await self._get(address, index, V.SUB_SCHEDULE)).schedule
        if schedule is None or schedule.type == AVAILABLE:
            raise _error(
                "schedule_empty_slot",
                ServiceValidationError,
                address=f"{address:04X}",
                slot=str(index),
            )
        return schedule

    async def update(
        self, address: int, index: int, data: Mapping[str, Any], action: V.Action
    ) -> Slot:
        """Rewrite a used slot in place, as the app's edit does (`CreateJHSchedule` with `Params.Update(index, …)`).

        The same writes as `create`, to slot `index`: location (astro), schedule, action. The schedule goes in
        inactive first and is made active once the new action is in, so the slot never fires the new times with its
        old action. When a write fails the slot's old contents are written back, if the element lets us; else it is
        left inactive, possibly with part of the update (a warning says so). A free slot, or one the app did not write (a
        central scheduler's or a reserved type), is the caller's mistake. Unverified on air, like every write here.
        """
        async with self._locks[address]:
            old = await self._slot(address, index)
            if old.type not in TRIGGER_OF:
                raise _error(
                    "schedule_empty_slot",
                    ServiceValidationError,
                    address=f"{address:04X}",
                    slot=str(index),
                )
            old_action = (await self._get(address, index, V.SUB_ACTION)).action
            schedule = build_schedule(index, data)
            inactive = replace(schedule, type=TRIGGERS[data["trigger"]][False])
            if data["trigger"] != "time":
                await self._send_location(address)
            try:
                await self._write_schedule(address, inactive.encode(), inactive)
                await self._write_action(address, index, action)
                if schedule.type != inactive.type:
                    await self._write_schedule(
                        address,
                        V.scheduler_type_set(index, schedule.type),
                        schedule.type,
                    )
            except HomeAssistantError:
                await self._restore(address, old, old_action)
                raise
            slot = Slot(schedule, action)
            self._store(address, index, slot)
            return slot

    async def _restore(
        self, address: int, schedule: V.Schedule, action: V.Action | None
    ) -> None:
        """Write a slot's old schedule and action back after a failed update; warn when the element does not take it.

        The schedule goes back inactive until its action is in again, as in `update`.
        """
        index = schedule.index
        trigger = TRIGGER_OF[schedule.type]
        inactive = replace(schedule, type=TRIGGERS[trigger][False])
        try:
            await self._write_schedule(address, inactive.encode(), inactive)
            if action is not None:
                await self._write_action(address, index, action)
            if schedule.type != inactive.type:
                await self._write_schedule(
                    address, V.scheduler_type_set(index, schedule.type), schedule.type
                )
        except HomeAssistantError:
            _LOGGER.warning(
                "%04X: slot %d was not written back; it may hold part of the update, left inactive",
                address,
                index,
            )

    async def set_enabled(self, address: int, index: int, enabled: bool) -> None:
        """Enable or disable a used slot: a type-only Set with its trigger's active or inactive type."""
        async with self._locks[address]:
            schedule = await self._slot(address, index)
            trigger = TRIGGER_OF.get(schedule.type)
            if trigger is None:  # reserved: not one of the app's schedules
                raise _error(
                    "schedule_empty_slot",
                    ServiceValidationError,
                    address=f"{address:04X}",
                    slot=str(index),
                )
            kind = TRIGGERS[trigger][enabled]
            if kind != schedule.type:
                await self._write_schedule(
                    address, V.scheduler_type_set(index, kind), kind
                )
            try:
                slot = await self._read_slot(address, index)
            except HomeAssistantError:
                # the change was confirmed: the cache follows what was written, the rest of the slot as last read
                cached = next(
                    (s for s in self.slots.get(address, ()) if s.index == index), None
                )
                slot = Slot(
                    replace(schedule, type=kind),
                    cached.action if cached else None,
                    cached.effective if cached else None,
                )
            self._store(address, index, slot)

    async def delete(self, address: int, index: int) -> None:
        """Free a used slot (type *available*), as the app's delete does."""
        async with self._locks[address]:
            await self._slot(address, index)
            await self._write_schedule(
                address, V.scheduler_type_set(index, AVAILABLE), AVAILABLE
            )
            self._store(address, index, None)


@dataclass(frozen=True, kw_only=True)
class ScheduleTarget(EntityTarget):
    """The *Schedules* sensor of one load element: its used slots, read once per link (no property behind it)."""

    kind: str  # the load's kind, `ACTION_FIELDS`

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """None: the slots are the JH Scheduler's, not properties."""
        return ()

    @property
    def base_translation_key(self) -> str:
        """Always `schedules`."""
        return "schedules"


def _load_info(hub: JungHomeHub, device: Device) -> tuple[DeviceInfo, Page] | None:
    """Return the HA device and parameter page a load shows under; None for what is not a load."""
    if isinstance(device, Light):
        return light_device_info(hub, device), "lamp"
    if isinstance(device, Socket):
        return socket_device_info(hub, device), "socket"
    if isinstance(device, Blind):
        return blind_device_info(hub, device), "blind"
    if isinstance(device, Thermostat):
        return node_device_info(hub, device.node), "rtr"
    return None


def schedule_targets(hub: JungHomeHub) -> list[ScheduleTarget]:
    """Return the *Schedules* sensor of every load whose element hosts a JH Scheduler, off by default."""
    out: list[ScheduleTarget] = []
    for address, device in hub.devices.by_address.items():
        element = hub.cdb.element(address)
        if (
            element is None
            or SCHEDULER_MODEL not in element.models
            or (info := _load_info(hub, device)) is None
        ):
            continue
        device_info, page = info
        out.append(
            ScheduleTarget(
                node=device.node,
                address=address,
                unique_id=f"{device.node.uuid.lower()}-{element.location:04x}-schedules",
                device_info=device_info,
                page=page,
                enabled_default=False,
                kind=device.kind,
            )
        )
    return out


def scheduler(hass: HomeAssistant, hub: JungHomeHub) -> Scheduler:
    """Return the hub's scheduler, created on first use and dropped when the entry unloads."""
    schedulers = jung_data(hass).schedulers
    entry_id = hub.entry.entry_id
    if isinstance(known := schedulers.get(entry_id), Scheduler):
        return known
    made = schedulers[entry_id] = Scheduler(hub)

    def forget() -> None:
        schedulers.pop(entry_id, None)

    hub.entry.async_on_unload(forget)
    return made
