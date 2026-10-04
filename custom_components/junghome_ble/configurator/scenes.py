"""Scenes: creating, storing on the loads, removing from them, deleting — and the scene numbers a device still holds.

`Scenes` (review-4 brief 55) runs the app's scene sequences on the loads' Scene Setup and Scene Action Setup servers
through the `PlanExecutor`'s requests (a stop records the members done before it), clears the keys recalling a scene
with a Config plan first, and keeps the held numbers a forced deletion skipped (`held_scenes`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from custom_components.junghome_ble.const import (
    DEFAULT_UNUSED_SCENES_DRY_RUN,
    DOMAIN,
    ISSUE_SCENE_HELD,
    learn_more_url,
)
from custom_components.junghome_ble.conversions import level_to_temperature
from custom_components.junghome_ble.coordinator import issue_id
from custom_components.junghome_ble.data import jung_data
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.devices import load_kind
from custom_components.junghome_ble.jhmesh.export import (
    DEFAULT_SCENE_ICON,
    AllocationCrowded,
    ExportError,
    InvalidName,
    ProjectFile,
    has_model,
    hexaddr,
    scene_infos,
)

from .executor import Operations
from .plan import (
    applied_members,
    applied_scene_cleared,
    applied_scene_members,
    applied_scene_stored,
    applied_unused_deleted,
)
from .store import _failure, _name_error, _validation
from .wiring import (
    SCENE_ACTION_SETUP,
    SCENE_SETUP_SERVER,
    _scene_register_status_name,
    _sibling_channels,
    find_scene,
    scene_key_steps,
    scene_load,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.junghome_ble.jhmesh.cdb import Element

_LOGGER = logging.getLogger(__name__)

# What the app lets a device hold before it refuses to store one more scene (`AbstractC0916e.B1()`,
# network-features.md §3 *Capacity check*): 8 per channel on a node whose channels keep their own scene list
# (Scene Action Setup), 16 in a node's SIG scene register otherwise. Timer scenes take slots like any other.
SCENE_ACTION_CAPACITY, SCENE_REGISTER_CAPACITY = 8, 16


HELD_SCENES_VERSION = 1


def held_scenes(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the entry's held scene numbers (`.storage/junghome_ble.<entry id>.held_scenes`), one instance per entry.

    `{"held": [[number, element], ...]}`: the scene registers a forced `delete_scene` skipped, which still hold a
    number the export no longer names (review-4 W4-8). `create_scene` does not hand such a number out again — the
    skipped device would join every recall of the new scene — and `delete_unused_scenes` lets go of a pair once the
    register no longer holds it. Numbers and addresses, no key material.
    """
    stores = jung_data(hass).held_scenes
    if entry_id not in stores:
        stores[entry_id] = Store(
            hass, HELD_SCENES_VERSION, f"{DOMAIN}.{entry_id}.held_scenes"
        )
    return stores[entry_id]


class Scenes(Operations):
    """The scenes of one hub's mesh: the export's scenes and what the loads' registers and channels hold."""

    async def _held_scenes(self) -> set[tuple[int, int]]:
        """(scene number, register element) of every register a forced `delete_scene` skipped and that may hold it."""
        data = await held_scenes(self.hub.hass, self.hub.entry.entry_id).async_load()
        try:
            return {(int(n), int(e)) for n, e in (data or {}).get("held", [])}
        except (TypeError, ValueError, AttributeError) as err:
            _LOGGER.warning(
                "Ignoring an unreadable record of held scene numbers: %s", err
            )
            return set()

    async def _hold_scenes(self, pairs: set[tuple[int, int]]) -> None:
        """Keep `pairs` as the held scene numbers and let the `scene_held` repair name them (cleared when none).

        An empty record is written rather than the file removed: `Store` would hand data it loaded back to the
        next load in this run.
        """
        await held_scenes(self.hub.hass, self.hub.entry.entry_id).async_save(
            {"held": sorted([n, e] for n, e in pairs)}
        )
        issue = issue_id(self.hub.entry, ISSUE_SCENE_HELD)
        if not pairs:
            ir.async_delete_issue(self.hub.hass, DOMAIN, issue)
            return
        by_number: dict[int, list[int]] = {}
        for number, element in sorted(pairs):
            by_number.setdefault(number, []).append(element)
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue,
            is_fixable=False,
            # the record outlives a restart, so must the issue: nothing raises it again at setup
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SCENE_HELD,
            learn_more_url=learn_more_url(ISSUE_SCENE_HELD),
            translation_placeholders={
                "title": self.hub.entry.title,
                "members": "; ".join(
                    f"{number}: {', '.join(self.store.member_name(e) for e in elements)}"
                    for number, elements in by_number.items()
                ),
            },
        )

    async def _sibling_uses_scene(
        self, element: Element, number: int, applied: str
    ) -> bool:
        """Whether another channel of the node still holds a JUNG action for `number` (its register is shared).

        `applied` is what an error says was done before the check (review-3 W14: the caller knows whether this
        channel's description was cleared at all, and which loads of the call were removed before it).
        """
        for other in _sibling_channels(element):
            reply = await self.executor.read_reply(
                other.address,
                V.scene_action_get(number),
                V.SCENE_ACTION_SETUP_STATUS,
                applied,
                number,
            )
            if reply is None:
                raise _failure(
                    "service_no_reply",
                    node=hexaddr(other.address),
                    message=M.describe(V.scene_action_get(number)),
                    applied=applied,
                )
            try:
                status = V.decode_scene_action_status(reply.params)
            except ValueError:
                continue
            if status.scenes is None and status.action is not None:
                return True
        return False

    async def create_scene(self, name: str, icon: str | None = None) -> int:
        """Create an empty scene (CDB `scenes[]` + `meta.scenes[]`); nothing goes on air. Returns its number.

        Not a number a device still holds after a forced deletion skipped it (`held_scenes`): that device would
        join every recall of the new scene (review-4 W4-8).
        """
        async with self.store.lock:
            pf = await self.store.load()
            held_numbers = {number for number, _ in await self._held_scenes()}
            try:
                number = pf.add_scene(
                    name, icon=icon or DEFAULT_SCENE_ICON, avoid=held_numbers
                )
            except InvalidName as err:
                raise _name_error(err, name) from err
            except ValueError as err:
                raise _validation("service_scene_exists", scene=name) from err
            except AllocationCrowded as err:
                raise _validation(
                    "service_scene_range_crowded", free=str(err.below)
                ) from err
            except ExportError as err:
                raise _validation("service_scene_range_full") from err
            if self.store.dry:
                self.store.planned(scene=number, name=name)
            await self.store.save(pf)
            _LOGGER.info("Created scene %r as number %d", name, number)
            return number

    async def rename_scene(self, scene: str | int, name: str) -> bool:
        """Rename a scene in the CDB and `meta.scenes[]`; nothing goes on air."""
        async with self.store.lock:
            pf = await self.store.load()
            number = find_scene(pf, scene)
            before = pf.snapshot()
            try:
                pf.rename_scene(number, name)
            except InvalidName as err:
                raise _name_error(err, name) from err
            except ValueError as err:
                raise _validation("service_scene_exists", scene=name) from err
            if pf.snapshot() == before:
                return self.store.adopted  # already called that
            await self.store.save(pf)
            _LOGGER.info("Renamed scene %d to %r", number, name)
            return True

    async def store_scenes(
        self, scene: str | int, loads: Iterable[tuple[int, V.Action | None]]
    ) -> bool:
        """Store every load's current state under `scene`, one file rewrite and gateway upload for all of them.

        Per load: `Scene Store` to the node's Scene Setup Server (the SIG register — what a Scene Recall
        restores), then the JUNG `Scene Action Setup Set` with the load's action on the channel element (what the
        app shows for the member; None when the state is unknown, which leaves the vendor record alone), then the
        export's `scenes[].addresses` (the element the Store went to, as the app's library records it) and the
        member's `meta.sceneInfo` row with the action's values, the state the load settled on (`scene_infos`;
        unverified with the app, `ProjectFile.set_scene_info`). A load that fails stops the call; the members
        stored before it are recorded (apply-and-record).
        """
        async with self.store.lock:
            pf = await self.store.load()
            number = find_scene(pf, scene)
            targets = [(*scene_load(pf, a), action) for a, action in loads]
            before = list(pf.cdb.scenes.get(number, []))
            for done, (element, store, action) in enumerate(targets):
                applied = applied_scene_members(done, len(targets), number)
                try:
                    await self._store_one(pf, number, element, store, action, applied)
                except (HomeAssistantError, asyncio.CancelledError):
                    # the one save of a stopped (or cancelled) call: the loads stored before, and one whose Store
                    # took but whose description did not — recorded either way
                    if pf.cdb.scenes.get(number, []) != before:
                        await self.store.save(pf)
                    raise
            await self.store.save(pf)
            return True

    async def _store_one(
        self,
        pf: ProjectFile,
        number: int,
        element: Element,
        store: Element,
        action: V.Action | None,
        applied: str,
    ) -> None:
        """Store one load's scene; `applied` says what the loads before it left recorded (for the error).

        A channel whose state is unknown (`action` None) is not stored beside another light or socket channel of
        its node with a Scene Action Setup server (review-3 W6): the app — and `_forget_scene` after it — tells
        the members of such a node by their JUNG action, so removing the other channel from the scene would
        delete the shared register and drop this one without a word. (A blind's slat element is no channel a
        scene is stored on by itself.)
        """
        if (
            action is None
            and has_model(element, SCENE_ACTION_SETUP)
            and any(
                load_kind(other) in ("light", "socket")
                for other in _sibling_channels(element)
            )
        ):
            raise _failure(
                "service_scene_state_unknown",
                address=hexaddr(element.address),
                scene=str(number),
                applied=applied,
            )
        await self._check_capacity(element, store, number, applied)
        register, read_back = await self.executor.scene_register(
            store.address, M.scene_store(number), applied
        )
        if not register.ok or number not in register.scenes:
            raise _failure(
                "service_scene_not_stored",
                address=hexaddr(store.address),
                scene=str(number),
                status=(
                    "not in the register after read-back"
                    if read_back and register.ok
                    else _scene_register_status_name(register.status)
                ),
                applied=applied,
            )
        # stored: the member is recorded whatever the description write does next; the values the app shows for
        # it (`meta.sceneInfo`, review-4 F4-6) are the stored action's once that is in, and none before
        pf.set_scene_addresses(number, [*pf.cdb.scenes.get(number, []), store.address])
        pf.remove_scene_info(number, element.node, element.location)
        if action is not None and has_model(element, SCENE_ACTION_SETUP):
            await self.executor.scene_action(
                element.address,
                number,
                action,
                applied_scene_stored(store.address, number),
            )
        if action is not None:
            pf.set_scene_info(
                number, element.node, element.location, scene_infos(action)
            )
        _LOGGER.info(
            "Stored scene %d on %04X (%s)",
            number,
            element.address,
            action.describe() if action else "no action record",
        )

    async def _check_capacity(
        self, element: Element, store: Element, number: int, applied: str
    ) -> None:
        """Refuse a Scene Store the device has no room for, before it is sent (the app's capacity check).

        A channel of a node whose channels keep their own scene list (Scene Action Setup, beside a sibling) is
        asked for that list (`Scene Action Setup Get` scene 0) and may hold `SCENE_ACTION_CAPACITY`; any other
        load's node is asked for its register (`Scene Register Get`), which may hold `SCENE_REGISTER_CAPACITY`.
        Unlike the app, a scene the device already holds is no new slot: storing it again is always allowed. As in
        the app, a register that does not answer is no reason to refuse (the Store says whether it took); a
        channel list that does not answer stops the call — the app takes it for a full one.
        """
        if has_model(element, SCENE_ACTION_SETUP) and _sibling_channels(element):
            reply = await self.executor.read_reply(
                element.address,
                V.scene_action_get(),
                V.SCENE_ACTION_SETUP_STATUS,
                applied,
                V.SCENE_LIST,
            )
            if reply is None:
                raise _failure(
                    "service_no_reply",
                    node=hexaddr(element.address),
                    message=M.describe(V.scene_action_get()),
                    applied=applied,
                )
            try:
                held = V.decode_scene_action_status(reply.params).scenes or ()
            except ValueError:
                held = ()  # a status too short to name the list: no list, as for an unanswered register
            where, capacity = element.address, SCENE_ACTION_CAPACITY
        else:
            reply = await self.executor.read_reply(
                store.address,
                M.scene_register_get(),
                M.SCENE_REGISTER_STATUS,
                applied,
            )
            try:
                held = (
                    ()
                    if reply is None
                    else M.decode_scene_register_status(reply.params).scenes
                )
            except ValueError:
                held = ()
            where, capacity = store.address, SCENE_REGISTER_CAPACITY
        if number not in held and len(held) >= capacity:
            raise _failure(
                "service_scene_no_capacity",
                address=hexaddr(where),
                scene=str(number),
                capacity=str(capacity),
                applied=applied,
            )

    async def remove_from_scenes(
        self, scene: str | int, addresses: Iterable[int]
    ) -> bool:
        """Take every load in `addresses` out of a scene; one file rewrite and gateway upload for all of them."""
        async with self.store.lock:
            pf = await self.store.load()
            number = find_scene(pf, scene)
            targets = [scene_load(pf, a) for a in addresses]
            # the keys of those devices that recall the scene first, as the app does (a stop records itself)
            keys = scene_key_steps(pf, [e.node for e, _ in targets], number)
            await self.executor.send(keys, action="junghome_ble.remove_from_scene")
            for done, (element, store) in enumerate(targets):
                try:
                    await self._forget_scene(
                        pf, element, store, number, done, len(targets), bool(keys)
                    )
                except (HomeAssistantError, asyncio.CancelledError):
                    if done or keys:  # the loads before this one, the keys: recorded
                        await self.store.save(pf)
                    raise
                pf.remove_scene_info(number, element.node, element.location)
                _LOGGER.info("Removed %04X from scene %d", element.address, number)
            await self.store.save(pf)
            return True

    async def delete_scene(self, scene: str | int, *, force: bool = False) -> list[str]:
        """Delete a scene: every element that stored it forgets it (every channel's action too), then the CDB / `meta` entries go.

        The keys of the members that recall the scene are cleared first (`scene_key_steps`, as the app's
        *remove device from scene* does for every member). A member that cannot be reached, or refuses, stops the
        deletion with what was done recorded — unless `force`, the app's *Delete anyway*
        (`removeScene(scene, force)`): then that member is skipped, keeps the scene in its register, and the scene
        leaves the export all the same. A skipped member's number is held (`held_scenes`, the `scene_held` repair
        names it) until `delete_unused_scenes` deletes it there: a new scene with that number would also recall
        the skipped member (review-4 W4-8). Returns the skipped members (`["0232"]`); the device model always
        changes.
        """
        async with self.store.lock:
            pf = await self.store.load()
            number = find_scene(pf, scene)
            members = [
                e
                for a in pf.cdb.scenes.get(number, [])
                if (e := pf.cdb.element(a)) is not None
            ]
            keys = scene_key_steps(pf, [e.node for e in members], number)
            if self.store.dry:
                pf.remove_scene(number)
                self.store.planned(
                    self.executor.in_order(keys)[0],
                    then=[
                        f"{self.store.member_name(element.address)}: {M.describe(pdu)}"
                        for stored_on in members
                        for element, pdu in (
                            *(
                                (channel, V.scene_action_set(number, V.NO_ACTION))
                                for channel in stored_on.node.elements
                                if has_model(channel, SCENE_ACTION_SETUP)
                            ),
                            (stored_on, M.scene_delete(number)),
                        )
                    ],
                )
            try:
                await self.executor.send(keys, action="junghome_ble.delete_scene")
            except HomeAssistantError as err:
                if not force:
                    raise
                _LOGGER.warning(
                    "Scene %d: the keys recalling it were not all cleared (%s); deleted anyway",
                    number,
                    err,
                )
                pf = (
                    await self.store.load()
                )  # what the stopped plan recorded, not what it planned
            skipped: list[int] = []
            for done, stored_on in enumerate(members):
                applied = applied_members(
                    done, len(members), number, keys_cleared=bool(keys)
                )
                try:
                    for channel in stored_on.node.elements:
                        if has_model(channel, SCENE_ACTION_SETUP):
                            await self.executor.scene_action(
                                channel.address, number, None, applied
                            )
                    await self._delete_from_register(pf, stored_on, number, applied)
                except (HomeAssistantError, asyncio.CancelledError) as err:
                    if force and isinstance(err, HomeAssistantError):
                        _LOGGER.warning(
                            "Scene %d: %04X did not forget it (%s); deleted anyway",
                            number,
                            stored_on.address,
                            err,
                        )
                        skipped.append(stored_on.address)
                        continue
                    if done or keys:  # the members before this one, the keys: recorded
                        await self.store.save(pf)
                    raise
            if skipped:
                # held before the export lets the number go, so no later call can hand it out in between
                await self._hold_scenes(
                    await self._held_scenes() | {(number, a) for a in skipped}
                )
            pf.remove_scene(number)
            await self.store.save(pf)
            self.executor.outcome.summary = (
                "plan_scene_deleted",
                {"scene": str(number)},
            )
            _LOGGER.info(
                "Deleted scene %d%s",
                number,
                f" (still stored on {', '.join(hexaddr(a) for a in skipped)})"
                if skipped
                else "",
            )
            return [hexaddr(a) for a in skipped]

    async def delete_unused_scenes(
        self,
        *,
        dry_run: bool = DEFAULT_UNUSED_SCENES_DRY_RUN,
        numbers: Collection[int] | None = None,
        confirm_stale_export: bool = False,
    ) -> dict[str, list[int] | list[str]]:
        """Delete from every node's scene register the scenes the export does not know (the app's `DeleteUnusedScenes`).

        `Scene Register Get` to each node's first Scene Setup Server, then `Scene Delete` for every number that is
        no scene of the export — the app's scenes and the scenes of its timers are all in the CDB, so neither is
        touched. The app does this unasked, per device, whenever its timer list opens; here it is an action.
        Returns the deleted numbers by register element (`"0148": [5]`), plus `"unanswered"`: the elements that
        did not answer the Get, left alone. A Delete the node does not carry out stops the call, as elsewhere,
        and the error names the numbers deleted before it (`applied_unused_deleted`); nothing of this is in the
        export, so nothing is written.

        Judged by what the export *lacks*, so only on an export known to be current (review-4 W4-3): the app's
        scenes made since a file was exported are no scene of that file, and the call deleted them from every
        device while the app still listed them. A `dry_run` (the default, decision M3) sends the Gets only and
        answers what it would delete. A gateway entry plans on the gateway's export or not at all
        (`service_gateway_export_unavailable`, dry run included); an entry set up from a file deletes only with
        `confirm_stale_export` (the user vouches for the file) or the `numbers` to delete, which restrict the
        call either way and must be no scene of the export. A held number (`held_scenes`) is let go once its
        register no longer holds it. Unverified on air: an app scene taken over from the gateway before the dry run.
        """
        async with self.store.lock:
            pf = await self.store.load(fresh=True)
            known = set(pf.cdb.scenes)
            if numbers is not None and (named := sorted(set(numbers) & known)):
                raise _validation(
                    "service_unused_scenes_known",
                    numbers=", ".join(str(n) for n in named),
                )
            if (
                not dry_run
                and self.store.gateway is None
                and numbers is None
                and not confirm_stale_export
            ):
                raise _validation("service_unused_scenes_stale_export")
            wanted = None if numbers is None else set(numbers)
            registers = [
                store
                for node in pf.cdb.nodes
                if node.pid is not None
                and (
                    store := next(
                        (e for e in node.elements if has_model(e, SCENE_SETUP_SERVER)),
                        None,
                    )
                )
                is not None
            ]
            held_before = await self._held_scenes()
            still_held = set(held_before)
            deleted: dict[str, list[int]] = {}
            unanswered: list[int] = []
            try:
                for store in registers:
                    reply = await self.executor.read_reply(
                        store.address,
                        M.scene_register_get(),
                        M.SCENE_REGISTER_STATUS,
                        applied_unused_deleted(deleted),
                    )
                    try:
                        holds = (
                            None
                            if reply is None
                            else M.decode_scene_register_status(reply.params).scenes
                        )
                    except ValueError:
                        holds = None
                    if holds is None:
                        unanswered.append(store.address)
                        continue
                    on_node = {e.address for e in store.node.elements}
                    if not dry_run:  # a dry run changes nothing, not even this record
                        still_held -= {
                            (n, e)
                            for n, e in still_held
                            if e in on_node and n not in holds
                        }
                    for number in holds:
                        if number in known or (
                            wanted is not None and number not in wanted
                        ):
                            continue
                        if not dry_run:
                            await self._delete_unused(store, number, deleted)
                            still_held -= {
                                (n, e)
                                for n, e in still_held
                                if n == number and e in on_node
                            }
                        deleted.setdefault(hexaddr(store.address), []).append(number)
            finally:
                if still_held != held_before:
                    await self._hold_scenes(still_held)
            result: dict[str, list[int] | list[str]] = dict(deleted)
            result["unanswered"] = [hexaddr(a) for a in unanswered]
            return result

    async def _delete_unused(
        self, store: Element, number: int, deleted: dict[str, list[int]]
    ) -> None:
        """`Scene Delete` of a number the export does not know, checked; `deleted` is what went before it."""
        register, _read_back = await self.executor.scene_register(
            store.address, M.scene_delete(number), applied_unused_deleted(deleted)
        )
        if number in register.scenes:
            raise _failure(
                "service_scene_not_deleted",
                address=hexaddr(store.address),
                scene=str(number),
                status=_scene_register_status_name(register.status),
                applied=applied_unused_deleted(deleted),
            )
        _LOGGER.info(
            "Deleted scene %d, unknown to the export, from %04X", number, store.address
        )

    async def _forget_scene(
        self,
        pf: ProjectFile,
        element: Element,
        store: Element,
        number: int,
        done: int,
        total: int,
        keys_cleared: bool = False,
    ) -> None:
        """Run the app's *remove device from scene* for one channel, the `done`-th of the call's `total` loads.

        Clear the channel's JUNG action, then delete the scene from the node's (shared) register unless another
        channel of the node still holds an action for it. A stop after the action was cleared leaves the file
        as it is — it records the register, which is still held — and says so; a stop before says what the
        loads before this one left recorded.
        """
        applied = applied_members(done, total, number, keys_cleared=keys_cleared)
        if has_model(element, SCENE_ACTION_SETUP):
            await self.executor.scene_action(element.address, number, None, applied)
            applied = applied_scene_cleared(element.address, number, done, total)
        if await self._sibling_uses_scene(element, number, applied):
            _LOGGER.debug(
                "%04X: another channel still uses scene %d, register kept",
                store.address,
                number,
            )
            return
        await self._delete_from_register(pf, store, number, applied)

    async def _delete_from_register(
        self, pf: ProjectFile, store: Element, number: int, applied: str
    ) -> None:
        """`Scene Delete` on the element holding the register, checked, then the export's member list."""
        register, _read_back = await self.executor.scene_register(
            store.address, M.scene_delete(number), applied
        )
        if number in register.scenes:
            raise _failure(
                "service_scene_not_deleted",
                address=hexaddr(store.address),
                scene=str(number),
                status=_scene_register_status_name(register.status),
                applied=applied,
            )
        pf.set_scene_addresses(
            number, [a for a in pf.cdb.scenes.get(number, []) if a != store.address]
        )


def scene_action_for(kind: str, state: Any, slat: Any = None) -> V.Action | None:
    """Return the JUNG scene action describing a load's present state, per load kind (`Device.kind`).

    Switch inserts and sockets store *switch on/off*; dimmers a lightness (0 when off); tunable-white lights a
    lightness with the colour temperature — what the app writes and what the nodes were seen to hold. A blind
    stores its position and slat levels (`slat`: the slat element's state; None for a blind without slats, whose
    slat field repeats the position), a thermostat its set-point (`p044d6/i.java`; neither seen on a device).
    None when the state needed is not known yet (the Scene Store still happens; only the vendor record is left
    alone) and for kinds without a record.
    """
    action: V.Action | None = None
    if kind == "blind":
        slat_level = state.level if slat is None else slat.level
        if state.level is not None and slat_level is not None:
            action = V.Action(V.ACTION_BLINDS, blind=state.level, slat=slat_level)
        return action
    if kind == "thermostat":
        if state.level is not None:
            action = V.Action(
                V.ACTION_TEMPERATURE, temperature_c=level_to_temperature(state.level)
            )
        return action
    known = state.on is not None or state.lightness is not None
    if kind in ("switch", "socket") and state.on is not None:
        action = V.Action(V.ACTION_SWITCH, on=state.on)
    elif kind == "dimmer" and known:
        action = V.Action(V.ACTION_LIGHTNESS, lightness=_lit(state))
    elif kind == "ctl" and known and state.kelvin is not None:
        action = V.Action(
            V.ACTION_LIGHTNESS_CT, lightness=_lit(state), temperature_k=state.kelvin
        )
    return action


def _lit(state: Any) -> int:
    """Return a dimmer's lightness for its scene record: 0 when it is off, else what it shows."""
    if state.on is False:
        return 0
    return int(state.lightness or 0)
