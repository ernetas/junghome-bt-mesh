"""Adding a node end to end (review-3 N3): address it, commission it, read it back, record it in the export.

`provisioning` gives a device its keys and address over PB-GATT; `commission.plan` lists the Config messages the
JUNG app sends a new node, after a node of the same product. What is left to make the node part of the
installation is here:

- `free_unicast_block`: where the node goes — the highest block of free addresses outside every provisioner's
  range, so neither the app nor another provisioner hands them out later (the app allocates only inside its own
  range, `docs/android/transport-provisioning.md` §3.3);
- `node_for`: the new node as the proxy client needs it to talk to it (`ProxyClient.add_node`) before the export
  knows it;
- `commission`: the plan's messages, each answered and each status checked, over the proxy link — planned again
  from the node's own Composition Data once it answered (`commission.Plan.resume`), and the two phases that are no
  Config message (the InsertId read, Time Set) handed to the caller at their place in the sequence;
- `node_entry` + `record`: the CDB node entry (the template's composition, the new identity and device key,
  what the node *answered* to the read-back — the audit's Gets — for publications, subscriptions and bindings),
  the element groups the plan allocated and the app's device rows, so the app, the gateway and the integration all
  see the node as the app would have left it.

Nothing here has run against a real device yet: the flow is exercised against the spec's provisioning sample data
(`provisioning`) and simulated Configuration Servers.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import config_messages as C
from .cdb import Element, Node, canonical_uuid
from .commission import CALLER_PHASES, PHASES
from .devices import expected_device_count

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from .audit import NodeAudit
    from .cdb import CDB
    from .client import ProxyClient
    from .commission import Plan
    from .export import ProjectFile

UNICAST_MAX = 0x7FFF


class CommissioningError(Exception):
    """The commissioning stopped: a Config message refused or unanswered, a node that is not the template's.

    The status names why a message was refused; `phase` names the step of the app's sequence it stopped in.
    """

    def __init__(self, message: str, phase: str = "") -> None:
        """Record the message and the phase."""
        super().__init__(message)
        self.phase = phase


def free_unicast_block(
    cdb: CDB,
    count: int,
    avoid: Iterable[int] = (),
    *,
    within: tuple[int, int] | None = None,
    own: str | None = None,
) -> int | None:
    """Return the first address of the highest block of `count` free addresses, None when there is none.

    Free = not an element of a node (excluded nodes included), not in `networkExclusions`, not in any
    provisioner's unicast range, not in `avoid` (Home Assistant's own address, and every address of a node it
    provisioned — `vault.Vault.reserved_unicasts`: one that was never recorded is in no file but still sends from
    them). Searching from the top keeps clear of where the apps allocate (from the bottom of their ranges).

    `within`: Home Assistant's own provisioner range (review-3 N1, `vault.Ranges.unicast`) — the block is taken
    inside it instead, where no other provisioner's range may reach (the range was chosen clear of them), and is
    still refused an address another provisioner's range covers, should the file say so. `own`: Home Assistant's
    provisioner UUID — its own entry's ranges (once merged into the file) do not block it; every other entry's do.
    """
    taken = cdb.used_unicasts() | cdb.excluded_addresses | set(avoid)
    ranges = cdb.foreign_unicast_ranges(own)
    bottom, top = within if within is not None else (1, UNICAST_MAX)

    def free(address: int) -> bool:
        return address not in taken and not any(
            lo <= address <= hi for lo, hi in ranges
        )

    run = 0
    for address in range(top, bottom - 1, -1):
        run = run + 1 if free(address) else 0
        if run == count:
            return address
    return None


def node_for(
    template: Node, *, uuid: str, unicast: int, dev_key: bytes, name: str
) -> Node:
    """Return the new node, shaped after `template`, for the proxy client to address before the export has it."""
    node = Node(
        canonical_uuid(uuid),
        name,
        unicast,
        dev_key,
        template.pid,
        cid=template.cid,
        insert_function=template.insert_function,
    )
    node.elements = [
        Element(unicast + i, e.location, list(e.models), node, _blank(e.raw_models))
        for i, e in enumerate(template.elements)
    ]
    return node


def _blank(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return copies of a template's model entries without its publications, subscriptions and bindings."""
    out = copy.deepcopy(models)
    for model in out:
        model.pop("publish", None)
        model["subscribe"] = []
        model["bind"] = []
    return out


async def commission(
    proxy: ProxyClient,
    plan: Plan,
    *,
    timeout: float = 3.0,
    retries: int = 3,
    on_phase: Callable[[str], Awaitable[None]] | None = None,
) -> Plan:
    """Send the plan's messages in order; `CommissioningError` at the first one refused or unanswered.

    Each step waits for its status (the plan names it) before the next one goes out, as the app does; a step the
    node refuses stops the plan — what went before stays on the node, and `Config Node Reset` is the way back.
    The Composition Data Status plans the rest again from the node's own composition (`Plan.resume`), which must
    be the template's: one that is not stops here, before any binding. `on_phase` is awaited as each phase of the
    app's sequence begins, the two without a Config message (`commission.CALLER_PHASES`) included: the caller's
    InsertId read and Time Set go there, and what it raises stops the commissioning. Returns the plan carried out
    (the resumed one).
    """
    announced: list[str] = []

    async def enter(phase: str | None) -> None:
        # the caller's phases that come before this one first (None: those left at the end), each once
        upto = len(PHASES) if phase is None else PHASES.index(phase) + 1
        for name in PHASES[:upto]:
            if name not in announced and (name == phase or name in CALLER_PHASES):
                announced.append(name)
                if on_phase is not None:
                    await on_phase(name)

    current, index = plan, 0
    while index < len(current.steps):
        step = current.steps[index]
        await enter(step.phase)
        try:
            reply = await proxy.request_config(
                step.destination,
                step.pdu,
                step.expect,
                timeout=timeout,
                retries=retries,
            )
        except TimeoutError as err:
            raise CommissioningError(f"no answer to {step.text}", step.phase) from err
        index += 1
        if step.expect == C.CONFIG_COMPOSITION_DATA_STATUS:
            try:
                current = plan.resume(C.decode_composition_data(reply.params))
            except ValueError as err:  # undecodable, or not the template's
                raise CommissioningError(str(err), step.phase) from err
            continue
        decoded = C.decode_config(reply.opcode, reply.params)
        if isinstance(decoded, C.ConfigStatus) and not decoded.ok:
            raise CommissioningError(
                f"{step.text} refused: {decoded.status_name}", step.phase
            )
    await enter(None)
    return current


def node_entry(
    template: dict[str, Any], *, uuid: str, unicast: int, dev_key: bytes, name: str
) -> dict[str, Any]:
    """Return the CDB entry of the new node: the template's composition and settings, the new identity and keys.

    Publications, subscriptions and bindings start empty: `record` fills them from what the node answered.
    """
    entry = copy.deepcopy(template)
    dashed = "-" in str(template.get("UUID", ""))
    canonical = canonical_uuid(uuid)
    entry["UUID"] = canonical if dashed else canonical.replace("-", "")
    entry["unicastAddress"] = f"{unicast:04X}"
    entry["deviceKey"] = dev_key.hex().upper()
    entry["name"] = name
    entry["excluded"] = False
    for key in ("heartbeatPub", "heartbeatSub"):
        entry.pop(key, None)
    for element in entry.get("elements", []):
        for model in element.get("models", []):
            model.pop("publish", None)
            model["subscribe"] = []
            model["bind"] = []
    return entry


def record(
    pf: ProjectFile,
    template: Node,
    entry: dict[str, Any],
    audit: NodeAudit,
    plan: Plan,
    name: str,
    function: int | None = None,
    layout: int | None = None,
) -> Node:
    """Put the new node into the export: its entry, its element groups, its app device rows, what it holds.

    Every publication, subscription list and AppKey binding comes from the node's answers to the read-back
    (`audit.NodeAudit.models`), not from the plan: the file then says what the node holds even where the plan and
    the node disagree. A model whose Get went unanswered keeps the empty value `node_entry` gave it. `function`:
    the actuator function the device advertised, for its device rows (`ProjectFile.clone_device_rows`); with
    `layout`, the button layout it advertised, also for the app's per-element InsertId and ButtonLayout rows
    (`ProjectFile.clone_property_rows`, review-4 F4-6).
    """
    node = pf.add_node_entry(entry, [(g.address, g.name) for g in plan.groups])
    for row in audit.models:
        element = pf.cdb.element(row.element)
        assert element is not None  # the audit asked the new node's own elements
        if row.node_publish is not None:
            pf.set_publication(node, element, row.model, row.node_publish or None)
        if row.node_subscribe is not None:
            pf.set_subscriptions(node, element, row.model, row.node_subscribe)
        if row.node_app_keys is not None:
            model = next(m for m in element.raw_models if m["modelId"] == row.model)
            model["bind"] = list(row.node_app_keys)
    pf.clone_property_rows(template, node, function, layout)
    if not pf.clone_device_rows(template, node, name, function):
        # a template the app has no device row for (an export without `meta`): one device on the primary element,
        # with the advertised function rather than the one guessed from the composition
        device = pf.set_device_name(node, [node.elements[0].location], name)
        if function is not None:
            device["deviceId"]["actuatorFunctionId"] = function
    return node


@dataclass(frozen=True)
class DeviceCount:
    """The app devices recorded for a new node against the number its product and insert call for."""

    recorded: int
    expected: int


def missing_devices(
    pf: ProjectFile, node: Node, function: int | None
) -> DeviceCount | None:
    """Run the app's check after adding a device (`CheckForMissingDevices`): None when the node has its devices.

    The node's app device rows (`ProjectFile.device_rows`) against `devices.expected_device_count` for its product
    and `function` (what it advertised, else the template's InsertId); a function the check has no count for passes.
    """
    expected = expected_device_count(node.pid, function)
    recorded = len(pf.device_rows(node))
    if expected is None or recorded == expected:
        return None
    return DeviceCount(recorded, expected)
