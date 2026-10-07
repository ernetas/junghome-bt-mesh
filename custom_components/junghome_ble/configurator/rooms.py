"""Rooms, device names and key connections: the operations behind the room and key actions.

`Rooms` and `Keys` plan with the pure planners of `wiring`, send through the `PlanExecutor` and
write through the `ExportStore`; a key link's LBC Admin writes (KeyMode, the property mode, a scene, a lock) follow
its Config plan here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, NoReturn

from homeassistant.exceptions import HomeAssistantError

from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.devices import DETECTOR_PIDS
from custom_components.junghome_ble.jhmesh.export import (
    InvalidName,
    ModelChange,
    ProjectFile,
    has_model,
    hexaddr,
)
from custom_components.junghome_ble.jhmesh.plan import (
    ConfigStep,
    bind_step,
    config_steps,
)

from .executor import Operations
from .plan import (
    APPLIED_KEY_WIRED,
    APPLIED_LOCK_WIRED,
    APPLIED_SCENE_WIRED,
    KeyPlan,
    Note,
    PlanError,
)
from .store import _failure, _name_error, _validation, applied_message
from .wiring import (
    DETECTOR_MODES,
    KEY_MODE_CLIENTS,
    LOCK_SECONDS_MAX,
    MODE_LOCK,
    PROPERTY_KEY_MODE,
    PROPERTY_KEY_PROPERTY_MODE,
    PROPERTY_KEY_SCENE_CONFIG,
    PROPERTY_KEY_VALUE_DOWN,
    PROPERTY_KEY_VALUE_UP,
    UNTESTED_MODES,
    add_room,
    check_room_name,
    clear_steps,
    device_entry,
    find_element,
    find_room,
    plan_device_link,
    plan_room_link,
    plan_scene_link,
    room_keys,
    rooms_of,
)

if TYPE_CHECKING:
    from custom_components.junghome_ble.areas import AreaRoom
    from custom_components.junghome_ble.jhmesh.cdb import Element

_LOGGER = logging.getLogger(__name__)


class Rooms(Operations):
    """Device names, rooms and room membership of one hub; every operation holds the store's lock."""

    async def rename_device(self, address: int, name: str) -> str:
        """Rename the app device the element at `address` belongs to, as the app's rename does; returns the name.

        `ProjectFile.rename_device`: the app's name check (`service_name_blank` / `service_name_not_allowed` /
        `service_name_too_long`), its
        number suffix when another device has the name, `meta.devices[].name` only. The device is the export's
        `meta.devices[]` entry covering the element (the most specific, as the app resolves it); an element no entry
        covers gets one of its own. Nothing goes on air; the export is written and handed to the gateway only when
        the name changed.
        """
        async with self.store.lock:
            pf = await self.store.load()
            element = find_element(pf, address)
            entry = device_entry(pf, element.node, element.location)
            locations = (pf.device_locations(entry) if entry is not None else None) or [
                element.location
            ]
            before = pf.snapshot()
            try:
                written = pf.rename_device(element.node, locations, name)
            except InvalidName as err:
                raise _name_error(err, name) from err
            if pf.snapshot() != before:
                await self.store.save(pf)
                _LOGGER.info("Renamed device %04X to %r", address, written)
            return written

    async def create_room(self, name: str) -> int:
        """Create a room (CDB `groups[]` + `meta.userGroups[]`); nothing goes on air. Returns its address."""
        async with self.store.lock:
            pf = await self.store.load()
            address = add_room(pf, name, (await self.store.reservations()).groups)
            if self.store.dry:
                self.store.planned(room=name, address=hexaddr(address))
            await self.store.save(pf)
            _LOGGER.info("Created room %r at %04X", name, address)
            return address

    async def rename_room(self, room: str | AreaRoom, name: str) -> bool:
        """Rename a room (by name, or by its area: `ExportStore.room_name`) in the CDB and `meta.userGroups[]`; nothing goes on air."""
        async with self.store.lock:
            pf = await self.store.load()
            address = find_room(pf, self.store.room_name(pf, room))
            wanted = check_room_name(name)
            before = pf.snapshot()
            try:
                pf.rename_group(address, wanted)
            except InvalidName as err:
                raise _name_error(err, wanted) from err
            except ValueError as err:
                raise _validation("service_room_exists", room=name) from err
            if pf.snapshot() == before:
                return self.store.adopted  # already called that
            await self.store.save(pf)
            _LOGGER.info("Renamed room %04X to %r", address, name)
            return True

    async def delete_room(self, room: str | AreaRoom) -> bool:
        """Delete a room: members unsubscribed, room-linked keys cleared, CDB and `meta` entries dropped."""
        async with self.store.lock:
            pf = await self.store.load()
            room = self.store.room_name(pf, room)
            address = find_room(pf, room)
            steps: list[ConfigStep] = []
            for link in pf.room_links(address):
                key = pf.cdb.element(link.key) if link.key is not None else None
                if key is not None:
                    steps += clear_steps(pf, key)
            steps += config_steps(pf, pf.remove_group(address))
            await self.executor.send(steps, action="junghome_ble.delete_room")
            await self.store.save(pf)
            self.executor.outcome.summary = ("plan_room_deleted", {"room": room})
            _LOGGER.info(
                "Deleted room %r (%04X), %d Config messages", room, address, len(steps)
            )
            return True

    async def set_rooms(
        self, addresses: Iterable[int], room: str | AreaRoom, *, create: bool = False
    ) -> bool:
        """Put every load element in `addresses` into `room`, leaving every other room.

        Membership is what `AddGroupToDevices` sends: the element's OnOff / Level servers subscribe to the room, and
        to the publish group of every key already linked to the room (`reconnectSwitchesWithGroup`); leaving a room
        is the mirror image (`DeleteGroupFromDevices`). One plan, one file rewrite and one gateway upload for
        all the loads of a service call — the gateway reconfigures itself on every upload. A room the export does
        not have is created only with `create`: a typo used to make a new room and move the loads into it.
        """
        return await self._change_rooms(
            addresses, room, action="junghome_ble.set_room", create=create, only=True
        )

    async def add_to_rooms(
        self, addresses: Iterable[int], room: str | AreaRoom, *, create: bool = False
    ) -> bool:
        """Put every load element in `addresses` into `room` as well, keeping the rooms it is in.

        The app's `AddDeviceToGroups`: a device can be in several rooms at once. The same `AddGroupToDevices`
        messages as `set_rooms` (the room's Subscription Adds, then each key linked to the room), without leaving
        any other room. A load already in the room sends nothing. Unverified on air.
        """
        return await self._change_rooms(
            addresses, room, action="junghome_ble.add_to_room", create=create
        )

    async def remove_from_rooms(
        self, addresses: Iterable[int], room: str | AreaRoom, *, force: bool = False
    ) -> bool:
        """Take every load element in `addresses` out of `room`, keeping the other rooms it is in.

        The app's `DeleteDeviceFromGroups` (`DeleteGroupFromDevices`): the load stops listening to every key linked
        to the room, then every model carrying the room drops it (`ProjectFile.set_room(member=False)`). A load a
        key's room link drives — it listens to the key's group — is refused unless `force`, naming the key: taking
        it out of the room unwires it from the key too, which the user may not expect from a room change. A load
        that is in no room afterwards is fine; the app allows that too. A load not in the room sends nothing.
        Unverified on air.
        """
        return await self._change_rooms(
            addresses,
            room,
            action="junghome_ble.remove_from_room",
            join=False,
            force=force,
        )

    async def _change_rooms(
        self,
        addresses: Iterable[int],
        room: str | AreaRoom,
        *,
        action: str,
        join: bool = True,
        only: bool = False,
        create: bool = False,
        force: bool = False,
    ) -> bool:
        """Join `room` (leaving every other room as well with `only`), or leave it: one plan, one rewrite, one upload.

        `create` makes a missing room to join (its creation is the plan's `prepare` note, so a stopped plan
        records it), at a group address no node holds (`ExportStore.reservations`); leaving a room needs one the
        export has. A room named by its area is the one the entry's mapping gives it (`ExportStore.room_name`):
        `create` never makes another. `force`: leave even where a key's room link drives the load (`room_keys`).
        """
        async with self.store.lock:
            pf = await self.store.load()
            before = pf.snapshot()
            elements = [find_element(pf, a) for a in addresses]
            room = self.store.room_name(pf, room)
            created: str | None = None
            try:
                group = find_room(pf, room)
            except PlanError:
                if not (join and create):
                    raise
                created = check_room_name(room)
                group = add_room(pf, room, (await self.store.reservations()).groups)
            self.executor.outcome.room = pf.cdb.groups[group]
            changes: list[ModelChange] = []
            if join:
                for element in elements:
                    for other in rooms_of(pf, element) if only else ():
                        if other != group:
                            changes += pf.set_room(element, other, member=False)
                    changes += pf.set_room(element, group)
            else:
                members = [e for e in elements if group in rooms_of(pf, e)]
                if not force:
                    self._refuse_room_keys(pf, members, group)
                for element in members:
                    changes += pf.set_room(element, group, member=False)
            prepare: Note | None = (
                {"kind": "room", "name": created, "address": group}
                if created is not None
                else None
            )
            steps = config_steps(pf, changes)
            if not steps and pf.snapshot() == before:
                return (
                    self.store.adopted
                )  # already so: nothing to send, write or upload
            await self.executor.send(steps, action=action, prepare=prepare)
            await self.store.save(pf)
            self.executor.outcome.summary = (
                "plan_room_joined" if join else "plan_room_left",
                {
                    "devices": ", ".join(
                        self.store.member_name(e.address) for e in elements
                    ),
                    "room": pf.cdb.groups[group],
                },
            )
            _LOGGER.info(
                "Element(s) %s %s room %r (%04X), %d Config messages",
                ", ".join(f"{e.address:04X}" for e in elements),
                "now in" if join else "taken out of",
                pf.cdb.groups[group],
                group,
                len(changes),
            )
            return True

    def _refuse_room_keys(
        self, pf: ProjectFile, elements: Iterable[Element], group: int
    ) -> None:
        """Refuse taking a load out of `group` while a key's room link to it drives the load (the first one found)."""
        for element in elements:
            if keys := room_keys(pf, element, group):
                raise _validation(
                    "service_room_key_drives_load",
                    device=self.store.member_name(element.address),
                    button=", ".join(self.store.member_name(k) for k in keys),
                    room=pf.cdb.groups[group],
                )


class Keys(Operations):
    """Key connections of one hub: a key wired to a device, a room or a scene, or to nothing."""

    async def assign_key(
        self,
        key_address: int,
        *,
        element: int | None = None,
        room: str | AreaRoom | None = None,
        scene: str | int | None = None,
        mode: str | None = None,
        target_element: str | None = None,
        lock_seconds: int | None = None,
    ) -> bool:
        """Wire the key element at `key_address` to a load element (`element`), a `room` or a `scene`.

        `mode` picks the key mode (`light` / `switch` / `move` / `gateway` / `lock` / `temperature`, rooms also
        `light_and_switch`); left out, a device target gets the mode the app derives from it and a room gets
        `light`. A scene (number or name) is recalled on every node, in key mode *scene*; it takes no `mode`.

        A device target only: `target_element` drives a tunable-white light's colour temperature or a blind's slats
        (`TARGET_ELEMENTS`) with the key's Level client; `mode: lock` makes the key lock (up / on) and unlock (down /
        off) a light or socket, for `lock_seconds` (0 or left out: no limit) — KeyMode *property* with the lock in the
        key's 0x5006 to 0x5008 (`_write_lock_function`), then the load is asked for its lock; `mode: temperature`
        (KeyMode RTR) steps a room thermostat's set-point. All unverified on air.

        A detector's sensor element is a key here too, the app's `ConnectionSource.Detector`: it drives one device,
        wired as a key's (`SetDeviceConnection`, network-logic.md §2.3), but has no KeyMode nor property mode to
        write (`KeyModeCapability`: `P.KEY_HOSTS` has no detector) — so only in a load's own mode
        (`DETECTOR_MODES`), with no `target_element` nor lock. Unverified on air (no detector here).
        """
        if sum(target is not None for target in (element, room, scene)) != 1:
            raise _validation("service_one_target")
        if target_element is not None and element is None:
            raise _validation("service_target_element_needs_device")
        if lock_seconds is not None and (
            mode != MODE_LOCK or not 0 <= lock_seconds <= LOCK_SECONDS_MAX
        ):
            raise _validation("service_lock_seconds_needs_lock")
        async with self.store.lock:
            pf = await self.store.load()
            key = find_element(pf, key_address)
            if room is not None:
                room = self.store.room_name(pf, room)
            detector = key.node.pid in DETECTOR_PIDS
            if detector and element is None:
                raise _validation(
                    "service_detector_device_only", address=hexaddr(key.address)
                )
            if room is not None:
                plan = plan_room_link(pf, key, room, mode, self.hub.metadata)
            elif scene is not None:
                plan = plan_scene_link(pf, key, scene, mode)
            else:
                assert element is not None  # exactly one target, checked above
                plan = plan_device_link(
                    pf, key, element, mode, target_element, lock_seconds
                )
                if detector and (
                    plan.mode not in DETECTOR_MODES or target_element is not None
                ):
                    raise _validation(
                        "service_detector_mode_unsupported",
                        address=hexaddr(key.address),
                        mode=plan.mode
                        if target_element is None
                        else f"{plan.mode} ({target_element})",
                    )
            clients = [
                m
                for m in plan.clients or KEY_MODE_CLIENTS[plan.key_mode]
                if has_model(key, m)
            ]
            if not clients:
                raise _validation(
                    "service_key_mode_unsupported",
                    address=hexaddr(key.address),
                    mode=plan.mode,
                )
            if plan.mode in UNTESTED_MODES or target_element is not None:
                _LOGGER.warning(
                    "Key mode %r%s has never been tried on a real device from Home Assistant; check the result in the app",
                    plan.mode,
                    ""
                    if target_element is None
                    else f" on the {target_element} element",
                )
            steps = list(plan.steps)
            for model in clients:
                bind = bind_step(key, model)
                if bind is not None:
                    steps.append(bind)
                steps += config_steps(
                    pf, pf.set_publication(key.node, key, model, plan.publish)
                )
                if (
                    plan.scene is None
                ):  # a scene key only publishes (`ConnectToAddress … PUBLISH_ONLY`)
                    steps += config_steps(pf, pf.subscribe(key, model, plan.publish))
            if self.store.dry:
                await self._plan_dry(pf, key.address, plan, steps, detector=detector)
            # a battery key stays held from its first Config step to its KeyMode write
            async with self.hub.keep_awake.hold([key.address]):
                await self.executor.send(
                    steps, action="junghome_ble.assign_key", prepare=plan.prepare
                )
                # every Config step was accepted: the key is wired as planned, whatever the vendor writes do next
                try:
                    await self._write_key_link(pf, key.address, plan, detector=detector)
                except (HomeAssistantError, asyncio.CancelledError):
                    await self.store.save(pf)
                    raise
            await self.store.save(pf)
            self.executor.outcome.summary = _key_summary(
                self.store.member_name(key.address),
                room=room,
                scene=plan.scene,
                target=None if element is None else self.store.member_name(element),
            )
            if plan.lock_target is not None:
                await self._request_lock(plan.lock_target)
            _LOGGER.info(
                "Key %04X now drives %s in mode %r (publishes to %04X), %d Config messages",
                key.address,
                plan.target,
                plan.mode,
                plan.publish,
                len(steps),
            )
            return True

    async def _plan_dry(
        self,
        pf: ProjectFile,
        key: int,
        plan: KeyPlan,
        steps: list[ConfigStep],
        *,
        detector: bool,
    ) -> NoReturn:
        """End a dry run of `assign_key`: its pre-flight reads, then its Config plan and vendor writes noted."""
        if plan.record_scene is not None:
            plan.record_scene(pf)
        in_order = self.executor.in_order(steps)[0]
        await self.executor.preflight(in_order)
        self.store.planned(
            in_order,
            then=[
                f"{self.store.member_name(key)}: {M.describe(pdu)}"
                for pdu in _key_link_writes(plan, detector=detector)
            ],
        )

    async def _write_key_link(
        self, pf: ProjectFile, key: int, plan: KeyPlan, *, detector: bool
    ) -> None:
        """Write what the key does on its own after a key link's Config steps (network-logic.md §2.2).

        A scene link names its scene (and records the app's row), a lock link its lock function, any other link resets
        the property mode; then the KeyMode. A detector source has neither property mode nor KeyMode: its wiring is
        all of it (it never takes a scene nor a lock, `assign_key`).
        """
        if plan.scene is not None:
            await self._write_scene_config(key, plan.scene)
            assert plan.record_scene is not None  # a scene plan always has it
            plan.record_scene(pf)
        elif plan.lock is not None:
            await self._write_lock_function(key, plan.lock)
        elif not detector:  # a detector has no property mode to reset ...
            await self._reset_property_mode(key)
        if not detector:  # ... nor a KeyMode
            await self._write_key_mode(key, plan.key_mode)

    async def clear_key(self, key_address: int) -> bool:
        """Give the key no function: drop its room link and every publication / subscription; KeyMode stays (as in the app)."""
        async with self.store.lock:
            pf = await self.store.load()
            before = pf.snapshot()
            key = find_element(pf, key_address)
            steps = clear_steps(pf, key)
            if not steps and pf.snapshot() == before:
                return self.store.adopted  # nothing left to clear
            await self.executor.send(steps, action="junghome_ble.clear_key")
            await self.store.save(pf)
            self.executor.outcome.summary = (
                "plan_key_cleared",
                {"key": self.store.member_name(key.address)},
            )
            _LOGGER.info(
                "Key %04X cleared, %d Config messages", key.address, len(steps)
            )
            return True

    async def _reset_property_mode(self, key: int) -> None:
        """`ResetKeySetPropertyMode`: KeySetPropertyMode (0, stateless), up / down values empty.

        Unacknowledged Sets (`C4 27 05`): the app fires them and never waits, and an acknowledged Set's three
        late Admin Statuses would be taken for the KeyMode Status `_write_key_mode` waits for next. Sent only
        once every Config step was accepted, so a refused plan leaves a key in property mode as it was.
        """
        writes = (
            (
                PROPERTY_KEY_PROPERTY_MODE,
                P.encode(PROPERTY_KEY_PROPERTY_MODE, P.PropertyMode(0, stateful=False)),
            ),
            (PROPERTY_KEY_VALUE_UP, b""),
            (PROPERTY_KEY_VALUE_DOWN, b""),
        )
        for prop, value in writes:
            pdu = M.vendor_property_set("admin", prop, value, ack=False)
            try:
                await self.hub.proxy.send_access(key, pdu)
            except (ConnectionError, OSError) as err:
                raise _failure(
                    "service_send_failed",
                    node=hexaddr(key),
                    message=M.describe(pdu),
                    applied=applied_message(self.hub.hass, APPLIED_KEY_WIRED),
                ) from err

    async def _write_key_mode(self, key: int, key_mode: int) -> None:
        """Write KeyMode 0x5003 and confirm it. Runs after the Config plan: its errors say the key is wired already."""
        value = P.encode(PROPERTY_KEY_MODE, key_mode)
        if not await self.executor.write_key_property(
            key, PROPERTY_KEY_MODE, value, APPLIED_KEY_WIRED
        ):
            raise _failure(
                "service_key_mode_not_applied",
                address=hexaddr(key),
                mode=str(P.KEY_MODE.get(key_mode, key_mode)),
            )

    async def _write_lock_function(
        self, key: int, values: tuple[bytes, bytes, bytes]
    ) -> None:
        """Write the key's lock function — KeySetPropertyMode 0x5006, up 0x5007, down 0x5008 — and confirm each.

        `SetLockingFunctionConnection` (network-logic.md §2.6) writes them before its `SetDeviceConnection`; here
        they follow the Config plan as every other vendor write does, so a refused plan leaves the key as it was.
        No `ResetKeySetPropertyMode` before them: it would only be overwritten. Unverified on air.
        """
        props = (
            PROPERTY_KEY_PROPERTY_MODE,
            PROPERTY_KEY_VALUE_UP,
            PROPERTY_KEY_VALUE_DOWN,
        )
        for prop, value in zip(props, values, strict=True):
            if not await self.executor.write_key_property(
                key, prop, value, APPLIED_LOCK_WIRED
            ):
                raise _failure(
                    "service_key_lock_not_applied",
                    address=hexaddr(key),
                    property=f"{prop:04X}",
                )

    async def _request_lock(self, target: int) -> None:
        """Ask the locked load for its lock function (Admin Get 0x0009), as the app does once a lock link is made.

        The answer reaches the load's entities like any other (`ElementState.note_lock`). Best effort: the key is
        wired whatever comes back, so a lost link is only logged. Unverified on air.
        """
        pdu = M.vendor_property_get("admin", P.ENFORCED_OUTPUT)
        try:
            await self.hub.proxy.send_access(target, pdu)
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("%04X: its lock function was not asked for: %s", target, err)

    async def _write_scene_config(self, key: int, scene: int) -> None:
        """Write KeyModeSceneConfig 0x5002 = (scene, no transition), as the app does, and confirm it.

        `[scene u16 LE][transition u32 LE ms]` (network-logic.md §2.2); the app waits for its status. Unverified on
        air.
        """
        value = P.encode(PROPERTY_KEY_SCENE_CONFIG, P.SceneConfig(scene))
        if not await self.executor.write_key_property(
            key, PROPERTY_KEY_SCENE_CONFIG, value, APPLIED_SCENE_WIRED
        ):
            raise _failure(
                "service_key_scene_not_applied",
                address=hexaddr(key),
                scene=str(scene),
            )


def _key_link_writes(plan: KeyPlan, *, detector: bool) -> list[bytes]:
    """List the LBC Admin writes `_write_key_link` sends after a key link's Config steps (the PDUs, unconfirmed)."""
    if plan.scene is not None:
        writes = [
            (
                PROPERTY_KEY_SCENE_CONFIG,
                P.encode(PROPERTY_KEY_SCENE_CONFIG, P.SceneConfig(plan.scene)),
                True,
            )
        ]
    elif plan.lock is not None:
        props = (
            PROPERTY_KEY_PROPERTY_MODE,
            PROPERTY_KEY_VALUE_UP,
            PROPERTY_KEY_VALUE_DOWN,
        )
        writes = [(p, v, True) for p, v in zip(props, plan.lock, strict=True)]
    elif not detector:  # `_reset_property_mode`: unacknowledged
        writes = [
            (
                PROPERTY_KEY_PROPERTY_MODE,
                P.encode(PROPERTY_KEY_PROPERTY_MODE, P.PropertyMode(0, stateful=False)),
                False,
            ),
            (PROPERTY_KEY_VALUE_UP, b"", False),
            (PROPERTY_KEY_VALUE_DOWN, b"", False),
        ]
    else:
        writes = []
    if not detector:
        writes.append(
            (PROPERTY_KEY_MODE, P.encode(PROPERTY_KEY_MODE, plan.key_mode), True)
        )
    return [M.vendor_property_set("admin", p, v, ack=ack) for p, v, ack in writes]


def _key_summary(
    key: str, *, room: str | None, scene: int | None, target: str | None
) -> tuple[str, dict[str, str]]:
    """Word the logbook line of a finished `assign_key`: the key and what it drives (a device, a room, a scene)."""
    if room is not None:
        return "plan_key_room", {"key": key, "room": room}
    if scene is not None:
        return "plan_key_scene", {"key": key, "scene": str(scene)}
    return "plan_key_device", {"key": key, "target": str(target)}
