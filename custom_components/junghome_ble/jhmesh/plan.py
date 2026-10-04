"""The plan model: Config steps, their order on air, their replay into the export.

One model for every plan of Config messages: the configurator's plans (a room, a key connection, a scene, a node's
removal: `ConfigStep` with the CDB edit it mirrors) and the commissioning of a new node (`commission.plan`: the same
`ConfigStep` with its phase of the app's step machine and its evidence; `commission.Step` is this class). Pure: a
`ProjectFile` and its CDB in, steps out — nothing here sends or writes anything.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import config_messages as C
from . import messages as M
from .devices import GROUP_RANGE
from .export import ModelChange, ProjectFile, raw_model
from .pdu import ALL_PROXIES, decode_opcode

if TYPE_CHECKING:
    from .cdb import Element
    from .client import AccessMessage

__all__ = [
    "APP_KEY_INDEX",
    "ConfigStep",
    "bind_step",
    "config_step",
    "config_steps",
    "deletable",
    "element_of",
    "ordered",
    "replay",
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
