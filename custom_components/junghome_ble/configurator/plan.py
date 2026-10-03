"""The plan model: Config steps, their order on air, their replay into the export, what a stop applied.

Pure (review-4 brief 55): a `ProjectFile` and its CDB in, steps out — no Home Assistant import. A planner that refuses
raises `PlanError`, which `MeshConfigurator` turns into the translated service error it names.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import GROUP_RANGE, meta_list
from custom_components.junghome_ble.jhmesh.export import (
    ModelChange,
    ProjectFile,
    hexaddr,
    keeps_row,
    meta_rows,
    raw_model,
)
from custom_components.junghome_ble.jhmesh.pdu import ALL_PROXIES

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.cdb import Element
    from custom_components.junghome_ble.jhmesh.client import AccessMessage

APP_KEY_INDEX = 0


class PlanError(Exception):
    """A planner's refusal: the `exceptions` translation key and its placeholders (`MeshConfigurator` raises it)."""

    def __init__(self, key: str, **placeholders: str) -> None:
        """Keep the key and the placeholders of the service error to raise."""
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


@dataclass(frozen=True)
class ConfigStep:
    """One Config message of a plan: the node it goes to, the PDU, the status opcode that acknowledges it.

    `change` is the CDB edit the message mirrors (a Model App Bind carries `bind` = (element, model) instead), so
    an accepted step can be replayed into a fresh copy of the export and its Status checked against it;
    `unlinks` names the key element whose room link the step tears down (the link's `meta` row goes with it).
    """

    node: int
    pdu: bytes
    expect: int
    change: ModelChange | None = None
    bind: tuple[int, str] | None = None
    unlinks: int | None = None

    @property
    def what(self) -> str:
        """Describe the message for logs and errors."""
        return M.describe(self.pdu)

    @property
    def additive(self) -> bool:
        """Whether the step adds wiring (Bind, Subscription Add, Publication Set to a group) rather than removes it."""
        if self.change is None:
            return True
        return self.change.kind == "subscribe" or (
            self.change.kind == "publish" and self.change.address != 0
        )

    def matches(self, message: AccessMessage) -> bool:
        """Whether a Config Status is the answer to *this* step: it echoes the element, model and address sent.

        A node answers every request it receives, so a reply that came late — after the attempt timed out and
        the PDU was re-sent — arrives twice; matched on node + opcode alone the duplicate would acknowledge the
        next same-opcode step and mask its refusal. A Status that does not decode is left to `PlanExecutor._request`,
        which counts it as a refusal (it cannot be told apart from anyone's).
        """
        try:
            status = C.decode_config(message.opcode, message.params)
        except ValueError:
            return True
        if self.bind is not None:
            element, model = self.bind
            return not isinstance(status, C.ModelAppStatus) or (
                status.element == element
                and status.model == C.model_id(model)
                and status.app_key_index == APP_KEY_INDEX
            )
        change = self.change
        if change is None:
            return True
        if isinstance(status, C.ModelSubscriptionStatus):
            return (
                status.element == change.element
                and status.model == C.model_id(change.model)
                and status.address == change.address
            )
        if isinstance(status, C.ModelPublicationStatus):
            return (
                status.element == change.element
                and status.model == C.model_id(change.model)
                and status.publish_address == change.address
            )
        return True


def ordered(
    steps: Iterable[ConfigStep], sleepy: Collection[int] = frozenset()
) -> list[ConfigStep]:
    """Put the additive steps of a plan before the destructive ones, dropping clears the additions supersede.

    The plans are computed in the app's order (clear the old wiring, then add the new) because the `ProjectFile`
    mutators derive each edit from the file's state; on air the new wiring goes out first so a plan that stops
    half-way leaves the old link working. A `Subscription Delete` the plan re-adds later, or a
    `Publication Set 0x0000` followed by a `Publication Set` of the same model, is not sent at all — sent after
    the addition it would undo it. Within each half the steps to the battery nodes in `sleepy` go first, in plan
    order (review-3 W4 / F24): such a node is awake for a moment after a key press, so its steps must not wait
    behind the mains nodes', and a node found asleep stops the plan at its first message — in every plan here
    with one such node the plan's first message, so nothing is applied — rather than half-way through.
    """
    additive = [s for s in steps if s.additive]
    destructive = [s for s in steps if not s.additive]
    added = {
        (c.element, c.model.upper(), c.address)
        for s in additive
        if (c := s.change) is not None and c.kind == "subscribe"
    }
    published = {
        (c.element, c.model.upper())
        for s in additive
        if (c := s.change) is not None and c.kind == "publish"
    }
    kept: list[ConfigStep] = []
    for step in destructive:
        change = step.change
        assert change is not None  # destructive steps always carry their edit
        if change.kind == "unsubscribe" and (
            (change.element, change.model.upper(), change.address) in added
        ):
            continue
        if change.kind == "publish" and (
            (change.element, change.model.upper()) in published
        ):
            continue
        kept.append(step)
    return sorted(additive, key=lambda s: s.node not in sleepy) + sorted(
        kept, key=lambda s: s.node not in sleepy
    )


def replay(pf: ProjectFile, step: ConfigStep) -> None:
    """Apply an accepted step's edit to `pf` — what the node holds now — through the same mutators the plan used."""
    if step.bind is not None:
        element, model = step.bind
        raw = raw_model(_element_of(pf, element), model)
        bound = [int(b) for b in raw.get("bind", [])]
        if APP_KEY_INDEX not in bound:
            raw["bind"] = [*bound, APP_KEY_INDEX]
    change = step.change
    if change is not None:
        if change.kind == "subscribe":
            pf.subscribe(change.element, change.model, change.address)
        elif change.kind == "unsubscribe":
            pf.unsubscribe(change.element, change.model, change.address)
        else:
            target = _element_of(pf, change.element)
            pf.set_publication(
                target.node, target, change.model, change.address or None
            )


def _drop_link_rows(pf: ProjectFile, key: int) -> None:
    """Remove `key`'s room-link and scene rows: every step tagged `unlinks=key` was accepted, the old wiring is gone."""
    for dev in meta_rows(meta_list(pf.meta.get("devices"))):
        dev["cachedGroupConnectionMetadata"] = [
            r
            for r in meta_list(dev.get("cachedGroupConnectionMetadata"))
            if keeps_row(r, "elementAddress", key)
        ]
    _drop_scene_key_row(pf, key)


def _drop_scene_key_row(pf: ProjectFile, key: int) -> None:
    """Remove `key`'s `keyModeSceneConfigExports` row (review-3 W2).

    The row is what makes the app show a key as recalling "Scene N"; a key cleared or given another function
    no longer does, whatever its KeyMode still says. A file without such a row is left byte-identical.
    """
    rows = meta_list(pf.meta.get("keyModeSceneConfigExports"))
    kept = [r for r in rows if keeps_row(r, "elementAddress", key)]
    if len(kept) != len(rows):
        pf.meta["keyModeSceneConfigExports"] = kept


def _deletable(address: int) -> bool:
    """Whether a Config Model Subscription Delete can name `address`: a group, not virtual nor a fixed group.

    A virtual subscription needs the Virtual Address variant (with its Label UUID), which nothing here sends,
    and the fixed groups (all-proxies … all-nodes) are no subscription the app ever adds.
    """
    return GROUP_RANGE[0] <= address < ALL_PROXIES


def _element_of(pf: ProjectFile, address: int) -> Element:
    element = pf.cdb.element(address)
    assert element is not None  # a step is only ever built for an element the CDB has
    return element


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


def config_step(pf: ProjectFile, change: ModelChange) -> ConfigStep:
    """Build the Config message that mirrors one CDB edit, addressed to the element's node."""
    node = pf.cdb.node_by_addr(change.element)
    assert node is not None  # the mutators only edit elements the CDB has
    if change.kind == "subscribe":
        pdu = C.model_subscription_add(change.element, change.address, change.model)
        expect = C.CONFIG_MODEL_SUBSCRIPTION_STATUS
    elif change.kind == "unsubscribe":
        pdu = C.model_subscription_delete(change.element, change.address, change.model)
        expect = C.CONFIG_MODEL_SUBSCRIPTION_STATUS
    else:
        pdu = C.model_publication_set(change.element, change.address, change.model)
        expect = C.CONFIG_MODEL_PUBLICATION_STATUS
    return ConfigStep(node.unicast, pdu, expect, change=change)


def config_steps(pf: ProjectFile, changes: Iterable[ModelChange]) -> list[ConfigStep]:
    """Build the Config message of each CDB edit of `changes`, in order (`config_step`)."""
    return [config_step(pf, c) for c in changes]


def bind_step(element: Element, model: str) -> ConfigStep | None:
    """Model App Bind for a model the CDB shows unbound (the app auto-binds before its first message)."""
    raw = raw_model(element, model)
    bound = [int(b) for b in raw.get("bind", [])]
    if APP_KEY_INDEX in bound:
        return None
    raw["bind"] = [*bound, APP_KEY_INDEX]
    return ConfigStep(
        element.node.unicast,
        C.model_app_bind(element.address, model, APP_KEY_INDEX),
        C.CONFIG_MODEL_APP_STATUS,
        bind=(element.address, model),
    )
