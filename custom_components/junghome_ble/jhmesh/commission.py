"""The JUNG app's post-provisioning configuration of a new node, as data — nothing here sends anything (review-3 N3).

After *Complete* (`provisioning.py`) the app reconnects to the new node as its proxy and walks the `ConfigureDevice`
step machine (`docs/android/network-logic.md` §3.2, `docs/android/transport-provisioning.md` §3.3). `plan()` turns
that into the ordered list of Config messages (Mesh Profile §4.3.2, built with `config_messages`), each to the new
node's primary unicast under its device key, with the status opcode that answers it and where the evidence is:

| Phase (app state) | Messages | Evidence |
|---|---|---|
| `SetWhitelistFilter` | AppKey Add (AppKey 0 on NetKey 0) | network-logic.md §3.2 step 1 |
| `RequestCompositionData` | Composition Data Get page 0; Model App Bind for every model the template has AppKey 0 bound to — the bind list first, then the models the app's messenger binds on first use | §3.2 step 2, §3.3 |
| `SetConfiguration` | GATT Proxy Set 1; AppKey Add again; Default TTL Set; Relay Set; Network Transmit Set | §3.2 step 3 |
| `SetBlacklistFilter` | Beacon Set (on unless the node is a battery device) | §3.2 step 4 |
| `CreateElementGroups` | per element group: Model Publication Set + Model Subscription Add of the element's supported servers | §1.2, §1.4, §1.5 |
| `FinishConfiguration` | Model Subscription Add to the device-type groups `FEF5`..`FEF9` (and the time keeper `FEFF` of PP2 pucks) | §1.3, §3.4 steps 1 and 9 |
| `DisableProxy` | GATT Proxy Set 0 for battery devices | §3.2 step 9 |

**The template.** The new node's composition, its device class and the per-element choices come from a node of the
same product (and the same insert) already in the export: the app decides them from the Composition Data Status
and the InsertId it reads on air, which a plan made before any message cannot know. From the template:

- *which models to bind*: those the template has the AppKey bound to (the app's bind list, §3.3, plus the
  messenger's bind-on-first-use, which is why an export shows Health and `0527:1012` bound too);
- *node-wide settings*: `defaultTTL`, `features.relay`, `relayRetransmit`, `networkTransmit`,
  `secureNetworkBeacon`. The CDB counts transmissions and milliseconds, the wire retransmissions and 10 ms steps
  minus one — the conversion `audit.py` compares with, so a node commissioned from a template audits clean against
  it. It is also what the installation shows: the iOS export records relay 3 / 90 ms and network transmit
  3 / 100 ms, the nodes answer relay retransmit 2, steps 8 and network transmit 2, steps 9
  (`docs/hidden-features.md` §4, §9). Where the template records nothing, those observed values are used (and TTL
  5, beacon on unless battery, relay on — §3.2). The Android app's documented Sets (relay 3 / steps 9, network
  transmit 3 / steps 10, `transport-provisioning.md` §3.3) are one higher in both fields: which of the two the
  app really sends is one of the gaps below;
- *element groups*: an element gets one when the template's element has one (a group named
  `element group #<address>`); its supported servers (§1.4, Sensor Server excluded) publish to and/or subscribe to
  the new group exactly as the template's do to its own. This follows the real installation where the documented
  rule and the iOS export differ: the rule lists `0527:1011` among the supported servers, the export never wires it
  (`docs/network-topology.md`), and the template decides;
- *device-type groups*: the template's supported servers' subscriptions to `FEF5`..`FEF9` (the class → group map
  of §1.3: lamps `FEF5`, blind position `FEF6`, slats `FEF7`, sockets `FEF8`, RTR `FEF9`), and the Time Server's to
  `FEFF`. Room subscriptions and key connections are user configuration, never copied.

The new groups the element groups need are allocated like the app does — the lowest address of the provisioner's
first group range that the export does not use as a group anywhere, rooms and element groups sharing the counter
(§1.2) — or, with `policy="top"`, the highest one (`export.Allocation`: what Home Assistant does without a
provisioner of its own, so the app's next room does not get the same group), and returned with their names for the
caller to add to the CDB; nothing here writes the CDB, the `meta` block or the gateway.

**Not in the plan** (`NOT_COVERED`, each with the reason): what the app sends that is not a Config message, what it
decides from values it reads on air, and what the docs do not pin down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import config_messages as C
from .audit import export_settings
from .cdb import parse_address
from .devices import BATTERY_PIDS, ELEMENT_GROUP_PREFIX, element_group_address
from .export import Allocation, group_addresses_in_use, pick_free
from .pdu import decode_opcode

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .cdb import CDB, Element, Node

# "suppportedServers" (sic, the app's DI name, network-logic.md §1.4): the models an element group and the
# device-type groups are wired to. Generic OnOff, Generic Level, Light Lightness, Light CTL Temperature, Light CTL,
# LBC User Property, Sensor, Scene, LBC Admin Property.
SUPPORTED_SERVERS = (
    "1000",
    "1002",
    "1300",
    "1306",
    "1303",
    "05271013",
    "1100",
    "1203",
    "05271011",
)
# Sensor Server: wired towards the gateway on its own (network-logic.md §6.1)
ELEMENT_GROUP_EXCLUDED = frozenset({"1100"})
# the app's bind list (`P7/a.java:27`, network-logic.md §3.3); every other bound model is bound on first use
BIND_LIST = frozenset(
    {
        "1011",
        "1004",
        "1002",
        "1003",
        "1012",
        "1000",
        "1001",
        "1006",
        "1007",
        "100E",
        "100F",
        "100C",
        "1303",
        "1305",
        "1304",
        "1306",
        "1300",
        "1302",
        "1301",
        "1203",
        "1205",
        "1204",
        "1206",
        "1207",
        "1100",
        "1101",
        "1200",
        "1201",
        "05271013",
        "05271011",
        "05271015",
        "05271016",
        "05271017",
    }
)
# device-key models: never bound to an AppKey
CONFIG_MODELS = frozenset({"0000", "0001"})
# lamps, blind position, slats, sockets, RTR set-point (network-logic.md §1.3)
DEVICE_TYPE_GROUPS = range(0xFEF5, 0xFEFA)
TIME_KEEPER_GROUP = 0xFEFF  # PP2 pucks' Time Server subscription (§3.4 step 9)
TIME_SERVER = "1200"

# where the template records nothing: the app's TTL (§3.2 step 3) and, on the wire as (retransmissions, steps),
# what the installation's nodes hold (hidden-features.md §4: relay 2 x 90 ms, network transmit 2 x 100 ms)
DEFAULT_TTL = 5
DEFAULT_RELAY_RETRANSMIT = (2, 8)
DEFAULT_NETWORK_TRANSMIT = (2, 9)

NL = "docs/android/network-logic.md"
TP = "docs/android/transport-provisioning.md"


@dataclass(frozen=True)
class Gap:
    """Something the app does after provisioning that the plan leaves out, and why."""

    what: str
    why: str


NOT_COVERED = (
    Gap(
        "proxy filter white list {node, 0xFFFF, provisioner} before step 1, black list after step 3",
        f"proxy configuration messages, not Config messages ({NL} §3.2 steps 1 and 4); the caller's"
        " proxy link sets its own filter (`ProxyClient.set_filter`)",
    ),
    Gap(
        "RequestRequiredData: LBC User Property Get 0x0002 InsertId (Admin Get 0x5001 ButtonLayout)",
        f"vendor property reads ({NL} §3.2 step 5); the InsertId decides the device class, which the"
        " plan takes from the template",
    ),
    Gap(
        "SetTime: Time Set (0x5C) to the node's Time Server",
        f"an AppKey message, not a Config message ({NL} §3.2 step 6, §6.2)",
    ),
    Gap(
        "detectors: the factory OnOff client publication (SetDeviceConnection), Admin Sets 0x6021,"
        " 0x100B, 0x1007",
        f"key-connection wiring and vendor property writes ({NL} §3.4 step 2, §2.3); the target is the"
        " load element group of the detector's own node, known only on air",
    ),
    Gap(
        "push-buttons: factory rocker wiring after provisioning (ConfigureControlSwitchFunctionality)",
        "docs/gap-analysis/network-features.md §2.2 and §10 name it; where it runs in the sequence and"
        " which publications a factory-fresh rocker has are not documented",
    ),
    Gap(
        "FinishConfiguration reads: gateway credentials, socket energy, RTR STM32 version, manufacturer"
        " properties, battery level, CheckForMissingDevices",
        f"reads only ({NL} §3.4 steps 3-8, §6.1)",
    ),
    Gap(
        "the relay / network transmit values the app sends",
        f"the Android decompile says 3 / 9 and 3 / 10 steps ({TP} §3.3), the installation's nodes hold"
        " 2 / 8 and 2 / 9 (docs/hidden-features.md §4), which is what its iOS export records; the plan"
        " copies the template",
    ),
    Gap(
        "whether the Scene Server keeps its device-type group subscription",
        "network-logic.md §1.3 subscribes all supported servers (so the plan sends 1203 -> FEF5 / FEF8 when"
        " the template lists it); the installation's nodes hold only the element group on 1203"
        " (docs/hidden-features.md §9): sent as documented, the node may not keep it",
    ),
    Gap(
        "the node's real composition",
        "the app binds from the Composition Data Status; the plan assumes the template's (same product"
        " and insert) and refuses a different element count",
    ),
    Gap(
        "CDB node entry, element-group rows in `meta`, the (UUID, MAC) record, the gateway upload",
        f"project-file work ({NL} §6.4, {TP} §3.3 step 4), not mesh messages",
    ),
    Gap(
        "the group range HA allocates element groups in",
        "review-3 N1 (HA's own provisioner entry); the plan takes the export's first range unless told",
    ),
)


@dataclass(frozen=True)
class Step:
    """One Config message of the plan, for the new node's primary unicast under its device key.

    `pdu` is the access payload (opcode + parameters); an AppKey Add carries the AppKey, so it stays out of
    `repr()` — `text` describes the message without key bytes.
    """

    phase: str
    destination: int
    pdu: bytes = field(repr=False)
    expect: int  # opcode of the status that answers it
    evidence: str

    @property
    def text(self) -> str:
        """Describe the message (`config_messages.describe_config`: key bytes are never shown)."""
        opcode, _cid, params = decode_opcode(self.pdu)
        return C.describe_config(opcode, params, devkey=True)


@dataclass(frozen=True)
class NewGroup:
    """An element group the plan allocated: its address, the name the app gives it, and the element it serves."""

    address: int
    name: str
    element: int


@dataclass(frozen=True)
class Plan:
    """The commissioning sequence for one node: its addresses, the ordered steps and the groups they need."""

    unicast: int
    elements: int
    template: int  # the template node's primary unicast
    steps: list[Step]
    groups: list[NewGroup]
    not_covered: tuple[Gap, ...] = NOT_COVERED

    def phases(self) -> list[str]:
        """Return the phases in the order the steps pass through them."""
        seen: list[str] = []
        for step in self.steps:
            if step.phase not in seen:
                seen.append(step.phase)
        return seen


def plan(
    cdb: CDB,
    unicast: int,
    elements: int,
    template: Node,
    *,
    app_key_index: int = 0,
    group_range: tuple[int, int] | None = None,
    reserved_groups: Iterable[int] = (),
    policy: Allocation = "app",
) -> Plan:
    """Build the commissioning plan of a node provisioned at `unicast` with `elements` elements, after `template`.

    Raises `ValueError` when the element count differs from the template's, when the new node's addresses are
    not free in the CDB, when the AppKey is unknown, or when no group address is left for its element groups.
    `group_range` overrides the range element groups are allocated in (default: the first provisioner's first).
    `reserved_groups` are taken although the export does not show them: the element groups of nodes Home Assistant
    provisioned but did not record (`vault.Vault.reserved_groups`), which those nodes may hold already.
    `policy` picks the lowest free address of the range ("app") or the highest below the device-type groups
    ("top"); `export.AllocationCrowded` when a "top" pick comes too close to the app's own groups.
    """
    if elements != len(template.elements):
        raise ValueError(
            f"the new node has {elements} element(s), template {template.unicast:04X} has"
            f" {len(template.elements)}: not the same product / insert"
        )
    # the app skips excluded addresses too (`nextAvailableUnicastAddress`, transport-provisioning.md §3.3): a node reusing
    # one would have its messages dropped by every node's replay list until the IV index moved on
    taken = sorted(
        set(range(unicast, unicast + elements))
        & (cdb.used_unicasts() | cdb.excluded_addresses)
    )
    if taken or unicast < 1 or unicast + elements - 1 > 0x7FFF:
        raise ValueError(
            f"addresses {unicast:04X}..{unicast + elements - 1:04X} are not free"
            + (
                f" ({', '.join(f'{a:04X}' for a in taken)} in use or excluded)"
                if taken
                else ""
            )
        )
    app_key = cdb.app_keys.get(app_key_index)
    if app_key is None:
        raise ValueError(f"the export has no AppKey {app_key_index}")
    builder = _Builder(
        cdb, unicast, template, app_key_index, group_range, set(reserved_groups), policy
    )
    builder.add_app_key(app_key.key)
    builder.bind()
    builder.set_configuration(app_key.key)
    builder.element_groups()
    builder.fixed_groups()
    builder.disable_proxy()
    return Plan(unicast, elements, template.unicast, builder.steps, builder.groups)


class _Builder:
    """Collects the steps of one plan in order; one method per phase of the app's step machine."""

    def __init__(
        self,
        cdb: CDB,
        unicast: int,
        template: Node,
        app_key_index: int,
        group_range: tuple[int, int] | None,
        reserved_groups: set[int],
        policy: Allocation,
    ) -> None:
        self.cdb = cdb
        self.unicast = unicast
        self.template = template
        self.app_key_index = app_key_index
        self.group_range = group_range
        self.reserved_groups = reserved_groups
        self.policy = policy
        self.steps: list[Step] = []
        self.groups: list[NewGroup] = []
        self.battery = template.pid in BATTERY_PIDS

    def add(self, phase: str, pdu: bytes, expect: int, evidence: str) -> None:
        self.steps.append(Step(phase, self.unicast, pdu, expect, evidence))

    def new_address(self, element: Element) -> int:
        """Return the new node's element at the template element's index."""
        return self.unicast + (element.address - self.template.unicast)

    def add_app_key(self, app_key: bytes) -> None:
        self.add(
            "SetWhitelistFilter",
            C.appkey_add(app_key, self.app_key_index),
            C.CONFIG_APPKEY_STATUS,
            f"{NL} §3.2 step 1; {TP} §3.3",
        )

    def bind(self) -> None:
        phase = "RequestCompositionData"
        self.add(
            phase,
            C.composition_data_get(0),
            C.CONFIG_COMPOSITION_DATA_STATUS,
            f"{NL} §3.2 step 2",
        )
        bound = [
            (element, model["modelId"])
            for element in self.template.elements
            for model in element.raw_models
            if model["modelId"] not in CONFIG_MODELS
            and self.app_key_index in model.get("bind", [])
        ]
        listed = [(e, m) for e, m in bound if m.upper() in BIND_LIST]
        for element, model_id in listed:
            self.add(
                phase,
                C.model_app_bind(
                    self.new_address(element), model_id, self.app_key_index
                ),
                C.CONFIG_MODEL_APP_STATUS,
                f"{NL} §3.3 (bind list)",
            )
        for element, model_id in bound:
            if (element, model_id) not in listed:
                self.add(
                    phase,
                    C.model_app_bind(
                        self.new_address(element), model_id, self.app_key_index
                    ),
                    C.CONFIG_MODEL_APP_STATUS,
                    f"{NL} §3.3 (bound by the app's messenger before the first message to the element; the"
                    " template has it bound)",
                )

    def set_configuration(self, app_key: bytes) -> None:
        phase = "SetConfiguration"
        settings = export_settings(self.template)
        evidence = f"{NL} §3.2 step 3; {TP} §3.3"
        self.add(phase, C.gatt_proxy_set(True), C.CONFIG_GATT_PROXY_STATUS, evidence)
        self.add(
            phase,
            C.appkey_add(app_key, self.app_key_index),
            C.CONFIG_APPKEY_STATUS,
            f"{evidence} (the app adds the AppKey a second time)",
        )
        ttl = settings["default_ttl"]
        self.add(
            phase,
            C.default_ttl_set(DEFAULT_TTL if ttl is None else ttl),
            C.CONFIG_DEFAULT_TTL_STATUS,
            f"{evidence}; value from {_source(ttl, 'the app (5)')}",
        )
        observed = "what the installation's nodes hold (docs/hidden-features.md §4)"
        relay, retransmit = settings["relay"], settings["relay_retransmit"]
        self.add(
            phase,
            C.relay_set(relay != 0, *_wire(retransmit, DEFAULT_RELAY_RETRANSMIT)),
            C.CONFIG_RELAY_STATUS,
            f"{evidence}; state from {_source(relay, 'the app (on)')}, retransmit from"
            f" {_source(retransmit, observed)}",
        )
        transmit = settings["network_transmit"]
        self.add(
            phase,
            C.network_transmit_set(*_wire(transmit, DEFAULT_NETWORK_TRANSMIT)),
            C.CONFIG_NETWORK_TRANSMIT_STATUS,
            f"{evidence}; value from {_source(transmit, observed)}",
        )
        beacon = settings["beacon"]
        self.add(
            "SetBlacklistFilter",
            C.beacon_set(not self.battery if beacon is None else beacon),
            C.CONFIG_BEACON_STATUS,
            f"{NL} §3.2 step 4 (the app does not wait for the status); value from"
            f" {_source(beacon, 'the app (on unless a battery device)')}",
        )

    def _allocate(self, element: int) -> NewGroup:
        """Take the lowest group address of the range (`policy` "top": the highest) that nothing else uses (§1.2).

        Nothing in the export, no earlier allocation. Nothing in the export: not only the CDB `groups[]` but the `meta` rooms, room links and every live
        publication and subscription (`export.group_addresses_in_use`, what a new room is allocated around too) —
        the new node would otherwise share a group some node still listens to. Nor a reserved one (`plan`), nor
        the device-type groups a range may reach into.
        """
        if self.group_range is not None:
            low, high = self.group_range
        elif self.cdb.provisioner_group_ranges:
            low, high = self.cdb.provisioner_group_ranges[0]
        else:
            raise ValueError(
                "the export has no provisioner group range to allocate element groups in"
            )
        used = (
            group_addresses_in_use(self.cdb, self.cdb.export_meta)
            | {g.address for g in self.groups}
            | self.reserved_groups
        )
        top = min(high, DEVICE_TYPE_GROUPS[0] - 1)
        address = pick_free(low, top, used, self.policy, "group address")
        if address is None:
            raise ValueError(f"no free group address left in {low:04X}..{high:04X}")
        group = NewGroup(address, _element_group_name(self.cdb, element), element)
        self.groups.append(group)
        return group

    def element_groups(self) -> None:
        own_groups = {
            addr: group
            for group, name in self.cdb.groups.items()
            if (addr := element_group_address(name)) is not None
        }
        evidence = f"{NL} §1.4, §1.5 (publish TTL 0xFF, no period, no retransmission); wiring as on the template"
        for element in self.template.elements:
            own = own_groups.get(element.address)
            if own is None:
                continue
            wired = [
                (
                    model["modelId"],
                    _publishes_to(model, own),
                    own in _subscriptions(model),
                )
                for model in element.raw_models
                if model["modelId"].upper() in SUPPORTED_SERVERS
                and model["modelId"].upper() not in ELEMENT_GROUP_EXCLUDED
            ]
            wired = [w for w in wired if w[1] or w[2]]
            if not wired:
                continue
            target = self.new_address(element)
            group = self._allocate(target)
            for model_id, publish, subscribe in wired:
                if publish:
                    self.add(
                        "CreateElementGroups",
                        C.model_publication_set(
                            target,
                            group.address,
                            model_id,
                            app_key_index=self.app_key_index,
                        ),
                        C.CONFIG_MODEL_PUBLICATION_STATUS,
                        evidence,
                    )
                if subscribe:
                    self.add(
                        "CreateElementGroups",
                        C.model_subscription_add(target, group.address, model_id),
                        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
                        evidence,
                    )

    def fixed_groups(self) -> None:
        for element in self.template.elements:
            target = self.new_address(element)
            for model in element.raw_models:
                model_id = model["modelId"].upper()
                for address in _subscriptions(model):
                    if model_id in SUPPORTED_SERVERS and address in DEVICE_TYPE_GROUPS:
                        evidence = f"{NL} §1.3, §3.4 step 1 (device-type group, as on the template)"
                    elif model_id == TIME_SERVER and address == TIME_KEEPER_GROUP:
                        evidence = f"{NL} §3.4 step 9, §6.2 (time keeper group of a PP2 puck, as on the template)"
                    else:
                        continue
                    self.add(
                        "FinishConfiguration",
                        C.model_subscription_add(target, address, model_id),
                        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
                        evidence,
                    )

    def disable_proxy(self) -> None:
        if self.battery:
            self.add(
                "DisableProxy",
                C.gatt_proxy_set(False),
                C.CONFIG_GATT_PROXY_STATUS,
                f"{NL} §3.2 step 9 (low-power device)",
            )


def _subscriptions(model: dict[str, Any]) -> list[int]:
    return [parse_address(a) for a in model.get("subscribe", [])]


def _publishes_to(model: dict[str, Any], group: int) -> bool:
    pub = model.get("publish")
    return (
        isinstance(pub, dict)
        and "address" in pub
        and parse_address(pub["address"]) == group
    )


def _source(value: object, fallback: str) -> str:
    """Say where a node-wide value came from: the template, or (when it records none) `fallback`."""
    return "the template" if value is not None else fallback


def _wire(value: dict[str, int] | None, default: tuple[int, int]) -> tuple[int, int]:
    """Turn a CDB `{count, interval}` (transmissions, ms) into the wire's (retransmissions, 10 ms steps minus one)."""
    if value is None:
        return default
    return max(value["count"] - 1, 0), max(value["interval"] // 10 - 1, 0)


def _element_group_name(cdb: CDB, element: int) -> str:
    """Name an element group the way the export's own ones are named: iOS `#0x148`, Android (and default) `#328`."""
    ios = any(
        name.startswith(ELEMENT_GROUP_PREFIX + "0x") for name in cdb.groups.values()
    )
    return (
        f"{ELEMENT_GROUP_PREFIX}0x{element:X}"
        if ios
        else f"{ELEMENT_GROUP_PREFIX}{element}"
    )
