"""What a stop applied, a planner's refusal, a key connection's plan and the plan journal's step rows.

The step model itself (`ConfigStep`, `ordered`, `replay`, the step builders) is the library's `jhmesh.plan`.
Pure: a `ProjectFile` and its CDB in, steps out — no Home Assistant import. A planner
that refuses raises `PlanError`, which `MeshConfigurator` turns into the translated service error it names.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from custom_components.junghome_ble.jhmesh.export import ModelChange, ProjectFile
from custom_components.junghome_ble.jhmesh.plan import ConfigStep


class PlanError(Exception):
    """A planner's refusal: the `exceptions` translation key and its placeholders (`MeshConfigurator` raises it)."""

    def __init__(self, key: str, **placeholders: str) -> None:
        """Keep the key and the placeholders of the service error to raise."""
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


# What the error of a stopped plan says about the messages before the one that failed (a placeholder: the
# sentence differs per outcome, and the file must always tell the truth about what the mesh holds). Said as data —
# `applied_<key>` messages of `exceptions` and their placeholders, worded in Home Assistant's language where the
# error is raised (`configurator.store.applied_message`): Home Assistant translates a message, never a
# placeholder's value. The placeholders are numbers, addresses and device names (`ExportStore.node_name`: the owner's
# own names, which no language translates), never words.


@dataclass(frozen=True)
class Applied:
    """What a stop applied: `applied_<key>` messages of `exceptions` with their placeholders, said in this order."""

    sentences: tuple[tuple[str, Mapping[str, str]], ...] = ()

    def __add__(self, other: Applied) -> Applied:
        """Say `other` after this."""
        return Applied(self.sentences + other.sentences)

    def __bool__(self) -> bool:
        """Whether it says anything."""
        return bool(self.sentences)


def said(key: str, **placeholders: object) -> Applied:
    """Return the one sentence `applied_<key>` with its placeholders (as text)."""
    return Applied(((key, {name: str(value) for name, value in placeholders.items()}),))


APPLIED_NOTHING = said("nothing")
# a key whose Config plan took and whose vendor writes did not: its key mode is missing, and its lock function or
# the scene it recalls with it
APPLIED_KEY_WIRED = said("key_wired")
APPLIED_LOCK_WIRED = said("lock_wired")
APPLIED_SCENE_WIRED = said("scene_wired")


def applied_text(accepted: int, total: int) -> Applied:
    """Describe the accepted steps of a plan that stopped after `accepted` of `total` messages."""
    if accepted == 0:
        return APPLIED_NOTHING
    return said("partly", accepted=accepted, total=total)


def applied_removed(node: str, accepted: int, total: int) -> Applied:
    """After a node's reset, when the plan taking the other nodes' wiring to it away stopped after `accepted`.

    `node` as the error names it (`ExportStore.node_name`).
    """
    return said("removed", device=node, accepted=accepted, total=total)


def applied_scene_stored(store: str, number: int) -> Applied:
    """After a Scene Store took on `store` (as the error names it) but the JUNG description did not."""
    return said("scene_stored", scene=number, device=store)


def applied_scene_cleared(
    element: str, number: int, done: int = 0, total: int = 1
) -> Applied:
    """After a channel's JUNG scene description was cleared but the register still holds the scene.

    `done` of the call's `total` loads forgot the scene before this one (recorded): the error says so too.
    """
    before = (
        said("scene_forgotten", done=done, total=total, scene=number)
        if done
        else Applied()
    )
    return before + said("scene_cleared", element=element, scene=number)


def applied_scene_members(done: int, total: int, number: int) -> Applied:
    """After `done` of `total` loads stored scene `number` and the next one did not."""
    if done == 0:
        return APPLIED_NOTHING
    return said("scene_members", done=done, total=total, scene=number)


def applied_members(
    done: int, total: int, number: int, *, keys_cleared: bool = False
) -> Applied:
    """After `done` of `total` members forgot scene `number` and the next one did not.

    `keys_cleared`: the call first cleared the members' keys that recalled the scene (recorded).
    """
    if done == 0:
        if keys_cleared:
            return said("keys_cleared", scene=number)
        return APPLIED_NOTHING
    return said("members", done=done, total=total, scene=number)


def applied_unused_deleted(deleted: dict[str, list[int]]) -> Applied:
    """After `delete_unused_scenes` stopped: the scenes it deleted before, by register (none of it is in the export)."""
    if not deleted:
        return APPLIED_NOTHING
    done = "; ".join(
        f"{address}: {', '.join(str(n) for n in numbers)}"
        for address, numbers in deleted.items()
    )
    return said("unused_deleted", deleted=done)


# A plan's bookkeeping that is no Config step, as data (`PlanExecutor._bookkeeping` applies it): it goes into the
# plan journal, so a record made after a crash can apply it too. `{"kind": "room", "name", "address"}` — a room the
# plan creates; `{"kind": "room_link", "key", "room", "publish", "function"}` — a room link's `meta` row;
# `{"kind": "excluded", "node", "iv_index"}` — a node that confirmed its reset.
Note = dict[str, Any]


@dataclass(frozen=True)
class KeyPlan:
    """A planned key connection: the Config messages and the KeyMode to write once they are accepted.

    `prepare` is the plan's bookkeeping that is no Config step (a room link's `meta` row), for the record of a
    plan that stops (`PlanExecutor.send`).
    """

    steps: list[ConfigStep]
    key_mode: int
    publish: int
    mode: str
    target: str  # for the log line
    prepare: Note | None = None
    # a scene link: the scene the key's KeyModeSceneConfig names, and the `meta` row recorded once the key took it
    scene: int | None = None
    record_scene: Callable[[ProjectFile], None] | None = None
    # the client models the key publishes from when not all of the key mode's (a target element: the Level client
    # alone); a lock link: the key's 0x5006 / 0x5007 / 0x5008 values and the load whose lock they set
    clients: tuple[str, ...] | None = None
    lock: tuple[bytes, bytes, bytes] | None = None
    lock_target: int | None = None


def _step_json(step: ConfigStep) -> dict[str, Any]:
    """Return a plan step as the plan journal keeps it."""
    change = step.change
    return {
        "node": step.node,
        "pdu": step.pdu.hex(),
        "expect": step.expect,
        "change": None
        if change is None
        else {
            "element": change.element,
            "model": change.model,
            "address": change.address,
            "kind": change.kind,
        },
        "bind": None if step.bind is None else list(step.bind),
        "unlinks": step.unlinks,
    }


def _step_from_json(row: dict[str, Any]) -> ConfigStep:
    """Return the plan step `_step_json` kept."""
    change, bind, unlinks = row["change"], row["bind"], row["unlinks"]
    return ConfigStep(
        int(row["node"]),
        bytes.fromhex(row["pdu"]),
        int(row["expect"]),
        change=None
        if change is None
        else ModelChange(
            int(change["element"]),
            str(change["model"]),
            int(change["address"]),
            change["kind"],
        ),
        bind=None if bind is None else (int(bind[0]), str(bind[1])),
        unlinks=None if unlinks is None else int(unlinks),
    )
