"""What a stop applied, a planner's refusal, a key connection's plan and the plan journal's step rows.

The step model itself (`ConfigStep`, `ordered`, `replay`, the step builders) is the library's `jhmesh.plan` (review-4
A4-10). Pure (review-4 brief 55): a `ProjectFile` and its CDB in, steps out — no Home Assistant import. A planner
that refuses raises `PlanError`, which `MeshConfigurator` turns into the translated service error it names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from custom_components.junghome_ble.jhmesh.export import (
    ModelChange,
    ProjectFile,
    hexaddr,
)
from custom_components.junghome_ble.jhmesh.plan import ConfigStep


class PlanError(Exception):
    """A planner's refusal: the `exceptions` translation key and its placeholders (`MeshConfigurator` raises it)."""

    def __init__(self, key: str, **placeholders: str) -> None:
        """Keep the key and the placeholders of the service error to raise."""
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


# What the error of a stopped plan says about the messages before the one that failed (a placeholder: the
# sentence differs per outcome, and the file must always tell the truth about what the mesh holds).
APPLIED_NOTHING = "Nothing before it was applied; the mesh export is unchanged."
APPLIED_KEY_WIRED = (
    "Every connection of the key was configured and is recorded in the mesh export; only the key mode is "
    "missing — run the action again with the same target to set it."
)
APPLIED_LOCK_WIRED = (
    "Every connection of the key was configured and is recorded in the mesh export; its lock function and key mode "
    "are missing — run the action again with the same target to set them."
)
APPLIED_SCENE_WIRED = (
    "The key publishes its scene recalls to all devices and that is recorded in the mesh export; the scene it "
    "recalls and its key mode are missing — run the action again with the same scene to set them."
)


def applied_text(accepted: int, total: int) -> str:
    """Describe the accepted steps of a plan that stopped after `accepted` of `total` messages."""
    if accepted == 0:
        return APPLIED_NOTHING
    return (
        f"The {accepted} of {total} messages accepted before it were applied on the mesh and are recorded in the "
        "mesh export; run the action again with the same target to complete it."
    )


def applied_removed(node: int, accepted: int, total: int) -> str:
    """After a node's reset, when the plan taking the other nodes' wiring to it away stopped after `accepted`."""
    return (
        f"Device {hexaddr(node)} was reset and the mesh export records it as removed from the network; {accepted} "
        f"of the {total} messages taking the other devices' links to it away were applied and are recorded too. "
        "The links left on the other devices point at a device that no longer answers; the mesh export keeps them, "
        "so nothing new reuses their groups."
    )


def applied_scene_stored(store: int, number: int) -> str:
    """After a Scene Store took but the JUNG description did not."""
    return (
        f"Scene {number} is stored on device {hexaddr(store)} and the member is recorded in the mesh export; only "
        "the scene description is missing — run the action again to write it."
    )


def applied_scene_cleared(
    element: int, number: int, done: int = 0, total: int = 1
) -> str:
    """After a channel's JUNG scene description was cleared but the register still holds the scene.

    `done` of the call's `total` loads forgot the scene before this one (recorded): the error says so too.
    """
    before = (
        f"{done} of {total} devices already forgot scene {number} and the mesh export records that. "
        if done
        else ""
    )
    return (
        f"{before}The scene description of {hexaddr(element)} for scene {number} was cleared; the scene itself is "
        "still stored on the device and recorded in the mesh export — run the action again to finish."
    )


def applied_scene_members(done: int, total: int, number: int) -> str:
    """After `done` of `total` loads stored scene `number` and the next one did not."""
    if done == 0:
        return APPLIED_NOTHING
    return (
        f"{done} of {total} devices stored scene {number} and are recorded in the mesh export; run the action "
        "again to finish."
    )


def applied_members(
    done: int, total: int, number: int, *, keys_cleared: bool = False
) -> str:
    """After `done` of `total` members forgot scene `number` and the next one did not.

    `keys_cleared`: the call first cleared the members' keys that recalled the scene (recorded).
    """
    if done == 0:
        if keys_cleared:
            return (
                f"The keys of these devices that recalled scene {number} were cleared and the mesh export records "
                "that; run the action again to finish."
            )
        return APPLIED_NOTHING
    return (
        f"{done} of {total} devices already forgot scene {number} and the mesh export records that; run the "
        "action again to finish."
    )


def applied_unused_deleted(deleted: dict[str, list[int]]) -> str:
    """After `delete_unused_scenes` stopped: the scenes it deleted before, by register (none of it is in the export)."""
    if not deleted:
        return APPLIED_NOTHING
    done = "; ".join(
        f"{address}: {', '.join(str(n) for n in numbers)}"
        for address, numbers in deleted.items()
    )
    return (
        f"Scenes unknown to the mesh export were already deleted before it ({done}); the mesh export is unchanged, "
        "as it never held them — run the action again to finish."
    )


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
