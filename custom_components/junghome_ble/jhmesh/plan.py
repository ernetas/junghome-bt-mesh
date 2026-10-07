"""The plan model: Config steps, their order on air, their replay into the export, their pre-flight reads.

One model for every plan of Config messages: the configurator's plans (a room, a key connection, a scene, a node's
removal: `ConfigStep` with the CDB edit it mirrors) and the commissioning of a new node (`commission.plan`: the same
`ConfigStep` with its phase of the app's step machine and its evidence; `commission.Step` is this class). Pure: a
`ProjectFile` and its CDB in, steps out — nothing here sends or writes anything.

A plan is computed from the export; the nodes are only told. Before a plan that removes or overwrites what the
export says a node holds (`destructive`), the configurator asks the nodes for it first (`preflight_checks`, one Get
per element and model) and compares their answers with the export (`Check.compare`): a node that no longer holds
what the export says stops the plan before its first write.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from . import config_messages as C
from . import messages as M
from .audit import SCENE_MODELS
from .devices import GROUP_RANGE
from .export import ModelChange, ProjectFile, raw_model
from .pdu import ALL_PROXIES, decode_opcode

if TYPE_CHECKING:
    from .cdb import Element
    from .client import AccessMessage

__all__ = [
    "APP_KEY_INDEX",
    "DESTRUCTIVE_KINDS",
    "SCENE_SERVER",
    "STEP_KINDS",
    "Check",
    "ConfigStep",
    "Difference",
    "bind_step",
    "config_step",
    "config_steps",
    "deletable",
    "destructive",
    "element_of",
    "ordered",
    "preflight_checks",
    "register_check",
    "replay",
    "step_kind",
]

APP_KEY_INDEX = 0


@dataclass(frozen=True)
class ConfigStep:
    """One Config message of a plan: the node it goes to, the PDU, the status opcode that acknowledges it.

    `change` is the CDB edit the message mirrors (a Model App Bind carries `bind` = (element, model) instead), so
    an accepted step can be replayed into a fresh copy of the export and its Status checked against it;
    `unlinks` names the key element whose room link the step tears down (the link's `meta` row goes with it).
    A commissioning step (`commission.plan`) goes to the new node's primary unicast under its device key and names
    its `phase` of the app's step machine and the `evidence` for it instead.

    `pdu` is the access payload (opcode + parameters); an AppKey Add carries the AppKey, so it stays out of
    `repr()` — `text` and `what` describe the message without key bytes.
    """

    node: int
    pdu: bytes = field(repr=False)
    expect: int  # opcode of the status that answers it
    change: ModelChange | None = None
    bind: tuple[int, str] | None = None
    unlinks: int | None = None
    phase: str = ""
    evidence: str = ""

    @property
    def destination(self) -> int:
        """The node the message goes to (a commissioning step's name for `node`)."""
        return self.node

    @property
    def what(self) -> str:
        """Describe the message for logs and errors."""
        return M.describe(self.pdu)

    @property
    def text(self) -> str:
        """Describe the message (`config_messages.describe_config`: key bytes are never shown)."""
        opcode, _cid, params = decode_opcode(self.pdu)
        return C.describe_config(opcode, params, devkey=True)

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
    order: such a node is awake for a moment after a key press, so its steps must not wait
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
        raw = raw_model(element_of(pf, element), model)
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
            target = element_of(pf, change.element)
            pf.set_publication(
                target.node, target, change.model, change.address or None
            )


def deletable(address: int) -> bool:
    """Whether a Config Model Subscription Delete can name `address`: a group, not virtual nor a fixed group.

    A virtual subscription needs the Virtual Address variant (with its Label UUID), which nothing here sends,
    and the fixed groups (all-proxies … all-nodes) are no subscription the app ever adds.
    """
    return GROUP_RANGE[0] <= address < ALL_PROXIES


def element_of(pf: ProjectFile, address: int) -> Element:
    """Return the CDB element at `address`, which a step is only ever built for."""
    element = pf.cdb.element(address)
    assert element is not None  # a step is only ever built for an element the CDB has
    return element


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


# ----------------------------------------------------------------------------- destructive steps, pre-flight reads

# What a step does to a model, by its edit: `bind` (Model App Bind), `subscribe`, `unsubscribe`, `publish` (a
# Publication Set to an address), `unpublish` (a Publication Set 0x0000), `other` (a step without an edit: the
# commissioning plan's AppKey Add and node-wide Sets, to a node that holds nothing yet)
STEP_KINDS = ("bind", "subscribe", "unsubscribe", "publish", "unpublish", "other")
# the kinds that always take away what the export says a node holds; a `publish` does when it replaces one
DESTRUCTIVE_KINDS = frozenset({"unsubscribe", "unpublish"})
SCENE_SERVER = "1203"  # the model that answers Scene Register Get

CheckKind = Literal["subscriptions", "publication", "scene_register"]


def step_kind(step: ConfigStep) -> str:
    """Return what `step` does to its model, one of `STEP_KINDS`."""
    if step.bind is not None:
        return "bind"
    change = step.change
    if change is None:
        return "other"
    if change.kind == "publish":
        return "publish" if change.address else "unpublish"
    return change.kind


def _held_publication(export: ProjectFile, element: int, model: str) -> int:
    """Return the publish address the export records for the model (0x0000: none)."""
    target = export.cdb.element(element)
    return 0 if target is None else target.publication(model)


def _held_subscriptions(export: ProjectFile, element: int, model: str) -> list[int]:
    """Return the addresses the export says the model subscribes to, sorted."""
    target = export.cdb.element(element)
    return [] if target is None else sorted(target.subscriptions(model))


def destructive(step: ConfigStep, export: ProjectFile) -> bool:
    """Whether `step` removes or overwrites what `export` — the export the plan was made from — says the node holds.

    A Subscription Delete and a Publication Set 0x0000 always do; a Publication Set to an address does when the
    export holds another publication for the model (a key rewired), not when it holds none or the same one. A
    Model App Bind, a Subscription Add and a commissioning step take nothing away. Not the same as
    `ConfigStep.additive`, which orders a plan: a publication that replaces another goes out with the additions.
    """
    kind = step_kind(step)
    if kind in DESTRUCTIVE_KINDS:
        return True
    if kind != "publish":
        return False
    change = step.change
    assert change is not None  # a `publish` step carries its edit
    return _held_publication(export, change.element, change.model) not in (
        0,
        change.address,
    )


@dataclass(frozen=True)
class Difference:
    """A pre-flight Get answered with something other than what the export says (`Check.compare`).

    `expected` (the export) and `found` (the node) as text: hex addresses for subscriptions and a publication,
    scene numbers for a scene register; `found` is the status name when the node refused the Get.
    """

    node: int
    element: int
    model: str
    kind: CheckKind
    what: str  # the Get, described (`messages.describe`)
    expected: list[str]
    found: list[str]

    def as_dict(self) -> dict[str, Any]:
        """Return the difference for an action response."""
        return {
            "node": f"{self.node:04X}",
            "element": f"{self.element:04X}",
            "model": self.model,
            "kind": self.kind,
            "expected": self.expected,
            "found": self.found,
        }


@dataclass(frozen=True)
class Check:
    """One pre-flight Get: what the node holds for one model of one element, before a plan changes it.

    `subscriptions` and `publication` are Config Gets under the node's device key; `scene_register` is a Scene
    Register Get (an AppKey message) to the element holding the node's scene register, for the `scene` a plan
    deletes there.
    """

    node: int
    element: int
    model: str
    kind: CheckKind
    scene: int | None = None

    @property
    def devkey(self) -> bool:
        """Whether the Get goes under the node's device key (a Config Get) rather than the AppKey."""
        return self.kind != "scene_register"

    @property
    def pdu(self) -> bytes:
        """The Get's access payload."""
        if self.kind == "scene_register":
            return M.scene_register_get()
        return C.model_get(self.kind, self.element, self.model)[0]

    @property
    def expect(self) -> int:
        """The opcode of the status that answers the Get."""
        if self.kind == "scene_register":
            return M.SCENE_REGISTER_STATUS
        return C.model_get(self.kind, self.element, self.model)[1]

    @property
    def what(self) -> str:
        """Describe the Get for logs and errors."""
        return M.describe(self.pdu)

    def matches(self, message: AccessMessage) -> bool:
        """Whether a Config status answers this Get: it echoes the element and model (a late one of another does not).

        One that does not decode does as well: `compare` reports it as malformed rather than waiting it out.
        """
        try:
            decoded = C.decode_config(message.opcode, message.params)
        except ValueError:
            return True
        if not isinstance(decoded, (C.ModelPublicationStatus, C.ModelSubscriptionList)):
            return True
        return C.echoes(decoded, self.element, self.model)

    def compare(self, export: ProjectFile, reply: AccessMessage) -> Difference | None:
        """Compare the node's answer with the export; the difference, or None when the node holds what it says.

        Subscriptions: every address the export lists must be on the node; one the node holds beyond them is left
        alone by the plan and not compared (the firmware subscribes some client models to their element's group by
        itself, docs/hidden-features.md). A publication must be the export's (0x0000 when it records none). A
        scene register must still hold the scene. A refused or malformed status differs only where the export
        expects something.
        """
        expected, found = self._values(export, reply)
        if isinstance(found, str):
            return self._difference(expected, [found]) if any(expected) else None
        if self.kind == "publication":
            differs = found != expected
        else:
            differs = not set(expected) <= set(found)
        return self._difference(expected, found) if differs else None

    def _values(
        self, export: ProjectFile, reply: AccessMessage
    ) -> tuple[list[int], list[int] | str]:
        """Return what the export says and what the node answered (or the status it refused the Get with)."""
        if self.kind == "scene_register":
            assert self.scene is not None  # a register check names its scene
            return [self.scene], _register_scenes(reply)
        expected = (
            [_held_publication(export, self.element, self.model)]
            if self.kind == "publication"
            else _held_subscriptions(export, self.element, self.model)
        )
        return expected, _model_values(reply)

    def _difference(
        self, expected: list[int], found: list[int] | list[str]
    ) -> Difference:
        def shown(values: list[int] | list[str]) -> list[str]:
            if self.kind == "scene_register":
                return [str(v) for v in values]
            return [v if isinstance(v, str) else f"{v:04X}" for v in values]

        return Difference(
            self.node,
            self.element,
            self.model,
            self.kind,
            self.what,
            shown(expected),
            shown(found),
        )


def _register_scenes(reply: AccessMessage) -> list[int] | str:
    """Return the scenes a Scene Register Status lists, or why it lists none."""
    try:
        return sorted(M.decode_scene_register_status(reply.params).scenes)
    except ValueError:
        return "malformed status"


def _model_values(reply: AccessMessage) -> list[int] | str:
    """Return the addresses a Publication Status or a Subscription List carries, or the status refusing the Get."""
    try:
        decoded = C.decode_config(reply.opcode, reply.params)
    except ValueError:
        return "malformed status"
    if not isinstance(decoded, (C.ModelPublicationStatus, C.ModelSubscriptionList)):
        return "malformed status"
    if not decoded.ok:
        return decoded.status_name
    if isinstance(decoded, C.ModelPublicationStatus):
        return [decoded.publish_address]
    return sorted(decoded.addresses)


def preflight_checks(steps: Iterable[ConfigStep], export: ProjectFile) -> list[Check]:
    """List the Gets that read what the destructive steps of `steps` change: one per element, model and kind.

    In plan order; `export` is the export the plan was made from. A step that changes a publication is checked
    against the publication, one that deletes a subscription against the subscription list. The Scene Server / Scene
    Setup Server subscriptions are not read: the export lists room and device-type groups there that the nodes never
    got (docs/hidden-features.md §9), so comparing them would stop every plan that leaves a room. There is no
    Model App Get: no plan unbinds an AppKey.
    """
    checks: dict[tuple[int, str, str], Check] = {}
    for step in steps:
        if not destructive(step, export):
            continue
        change = step.change
        assert change is not None  # a destructive step carries its edit
        kind: CheckKind = "publication" if change.kind == "publish" else "subscriptions"
        if kind == "subscriptions" and change.model.upper() in SCENE_MODELS:
            continue
        checks.setdefault(
            (change.element, change.model.upper(), kind),
            Check(step.node, change.element, change.model, kind),
        )
    return list(checks.values())


def register_check(element: Element, scene: int) -> Check:
    """Return the Scene Register Get that reads whether `element` — a node's scene register — still holds `scene`."""
    return Check(
        element.node.unicast, element.address, SCENE_SERVER, "scene_register", scene
    )
