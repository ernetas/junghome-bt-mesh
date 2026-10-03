"""The JUNG app's post-provisioning configuration of a new node, as data — nothing here sends anything (review-3 N3).

After *Complete* (`provisioning.py`) the app reconnects to the new node as its proxy and walks the `ConfigureDevice`
step machine (`docs/android/network-logic.md` §3.2, `docs/android/transport-provisioning.md` §3.3). `plan()` turns
that into the ordered list of Config messages (Mesh Profile §4.3.2, built with `config_messages`), each to the new
node's primary unicast under its device key, with the status opcode that answers it and where the evidence is:

| Phase (app state) | Messages | Evidence |
|---|---|---|
| `SetWhitelistFilter` | AppKey Add (AppKey 0 on NetKey 0) | network-logic.md §3.2 step 1 |
| `RequestCompositionData` | Composition Data Get page 0; Model App Bind for every model of the bind list the node has, then the models the app's messenger binds on first use | §3.2 step 2, §3.3 |
| `SetConfiguration` | GATT Proxy Set 1; AppKey Add again; Default TTL Set 5; Relay Set; Network Transmit Set | §3.2 step 3 |
| `SetBlacklistFilter` | Beacon Set (on unless the node is a battery device) | §3.2 step 4 |
| `RequestRequiredData` | none here: the caller reads the InsertId (an AppKey message) | §3.2 step 5 |
| `SetTime` | none here: the caller sends Time Set to `Plan.time_server` (an AppKey message) | §3.2 step 6, §6.2 |
| `CreateElementGroups` | per element with a supported server: Model Publication Set + Model Subscription Add of each to a new group | §1.2, §1.4, §1.5 |
| `FinishConfiguration` | Model Subscription Add to the device-type group(s) of the node's class `FEF5`..`FEF9`, and a PP2 puck's Time Server to the time keeper's `FEFF` | §1.3, §3.4 steps 1 and 9 |
| `DisableProxy` | GATT Proxy Set 0 for battery devices | §3.2 step 9 |

**The composition** (review-4 F4-13). The app plans from the node's Composition Data Status and its InsertId; a
plan made before the node answered cannot know them, but the addresses and element groups must be decided before
the device gets its Provisioning Data (the vault reserves them, `vault.Vault.remember_provisioned`). So `plan()`
plans first from a *template* — a node of the same product (and the same insert) already in the export, whose
composition the new one should have — and `Plan.resume` plans again from what the node answered: the same rules
on the node's own elements and models, keeping the element groups the first plan allocated. A composition that is
not the template's (another company or product, another element count, an element with another location or other
models, or one without the `1012` / `0527:1012` servers the app's `t1()` asserts — its `NodeNotConfigured`) is
refused (`CompositionMismatch`): the first plan's element groups and the device class would be wrong for it.

**The app's rules**, applied to the composition (the template's until the node answered):

- *binding*: every model of the bind list (§3.3) the node has, then what the app's messenger binds on first use —
  every other model the template has AppKey 0 bound to (which is why an export shows Health and `0527:1012` bound
  too); the Configuration Server and Client never;
- *node-wide settings*, the app's values whatever the template holds (`setconfiguration`): GATT Proxy on, TTL 5,
  relay on, beacon on unless a battery device; relay retransmit 2 x 90 ms and network transmit 2 x 100 ms on the
  wire (retransmissions, 10 ms steps minus one: (2, 8) and (2, 9)) — what the installation's nodes answer, set by
  the iOS app, whose export records relay 3 / 90 ms and network transmit 3 / 100 ms (`docs/hidden-features.md` §4,
  §9). The Android app's documented Sets (relay 3 / steps 9, network transmit 3 / steps 10,
  `transport-provisioning.md` §3.3) are one higher in both fields: which of the two the app really sends is one of
  the gaps below;
- *element groups* (`CreateElementConnectionGroups`, default wiring): an element with at least one supported server
  (§1.4) gets a group, and each of those servers publishes to and subscribes to it — except the Sensor Server
  (wired towards the gateway on its own, §6.1) and `0527:1011`: the decompile lists it among the supported servers,
  but no node of the installation has it wired (`docs/network-topology.md`), and the installation is what the app
  that made it did;
- *device-type groups* (`ConnectToDeviceTypeGroup`, §1.3): the node's class decides — lamps `FEF5` and sockets
  `FEF8` on every element, RTRs `FEF9`, blinds `FEF6` on the position element (its first Generic Level server) and
  `FEF7` on the slat element (the last one, when it is another) — for its SIG supported servers: the
  installation's nodes subscribe neither vendor server there. Push-buttons by their insert (`function`), mini
  actuators and pucks by product; detectors, the gateway, wall transmitters and an extension insert have none.
  A PP2 puck's Time Server subscribes to the time keeper's group `FEFF` (§3.4 step 9). Room subscriptions and key
  connections are user configuration, never copied.

The new groups the element groups need are allocated like the app does — the lowest address of the provisioner's
first group range that the export does not use as a group anywhere, rooms and element groups sharing the counter
(§1.2) — or, with `policy="top"`, the highest one (`export.Allocation`: what Home Assistant does without a
provisioner of its own, so the app's next room does not get the same group), and returned with their names for the
caller to add to the CDB; nothing here writes the CDB, the `meta` block or the gateway.

**Not in the plan** (`NOT_COVERED`, each with the reason): what the app sends that is not a Config message, what it
decides from values it reads on air, and what the docs do not pin down. Unverified on air: no node has been
commissioned from these rules yet (a spare device is needed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import config_messages as C
from .devices import (
    BATTERY_PIDS,
    BLIND_FUNCTION,
    BLIND_ONLY_PIDS,
    ELEMENT_GROUP_PREFIX,
    KEY_LOCATION,
    LAMP_FUNCTIONS,
    LAMP_LEVEL_MODELS,
    LOAD_LOCATIONS,
    PP2_PIDS,
    PUSH_BUTTON_PIDS,
    SOCKET_PIDS,
    THERMOSTAT_PIDS,
    TIME_KEEPER_ADDRESS,
    TIME_SERVER,
    insert_function,
)
from .export import Allocation, group_addresses_in_use, pick_free
from .pdu import decode_opcode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .cdb import CDB, Node

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
# Sensor Server: wired towards the gateway on its own (network-logic.md §6.1); LBC Admin Property: never wired on the
# installation's nodes (module docstring)
ELEMENT_GROUP_EXCLUDED = frozenset({"1100", "05271011"})
# the supported servers a device-type group reaches: the SIG ones but the Sensor Server, as on the installation
DEVICE_TYPE_SERVERS = frozenset({"1000", "1002", "1300", "1306", "1303", "1203"})
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
# the models the app's `t1()` asserts after the Composition Data (`NodeNotConfigured` otherwise, §3.2 step 5)
REQUIRED_MODELS = ("1012", "05271012")
# lamps, blind position, slats, sockets, RTR set-point (network-logic.md §1.3)
DEVICE_TYPE_GROUPS = range(0xFEF5, 0xFEFA)
LAMPS, BLINDS, SLATS, SOCKETS, RTRS = DEVICE_TYPE_GROUPS
TIME_KEEPER_GROUP = (
    TIME_KEEPER_ADDRESS  # PP2 pucks' Time Server subscription (§3.4 step 9)
)
# mini actuators and pucks with a light output (a `LampDevice` of theirs): the switch / dimmer / DALI ones
LAMP_ACTUATOR_PIDS = frozenset({0x0004, 0x0010, 0x0011, 0x0012, 0x0014})

# the app's values (module docstring): its TTL (§3.2 step 3) and, on the wire as (retransmissions, steps), what the
# installation's nodes hold (hidden-features.md §4: relay 2 x 90 ms, network transmit 2 x 100 ms)
DEFAULT_TTL = 5
DEFAULT_RELAY_RETRANSMIT = (2, 8)
DEFAULT_NETWORK_TRANSMIT = (2, 9)

# the app's `ConfigureDevice` states in order; the two without a Config message are the caller's
PHASES = (
    "SetWhitelistFilter",
    "RequestCompositionData",
    "SetConfiguration",
    "SetBlacklistFilter",
    "RequestRequiredData",
    "SetTime",
    "CreateElementGroups",
    "FinishConfiguration",
    "DisableProxy",
)
CALLER_PHASES = ("RequestRequiredData", "SetTime")

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
        f"vendor property reads ({NL} §3.2 step 5): the caller reads the InsertId in this phase and refuses"
        " a node whose insert is not the one the plan was made for",
    ),
    Gap(
        "SetTime: Time Set (0x5C) to the node's Time Server",
        f"an AppKey message, not a Config message ({NL} §3.2 step 6, §6.2): the caller sends it in this phase"
        " to `Plan.time_server`",
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
        " sends the installation's",
    ),
    Gap(
        "whether the Scene Server keeps its device-type group subscription",
        "network-logic.md §1.3 subscribes all supported servers (so the plan sends 1203 -> FEF5 / FEF8);"
        " docs/hidden-features.md §9 saw nodes holding only the element group on 1203: sent as documented,"
        " the node may not keep it",
    ),
    Gap(
        "the time keeper election (EnsureTimeKeeper)",
        f"{NL} §6.2: the app elects one mains node for the PP2 pucks; Home Assistant offers a switch per node"
        " instead and raises a repair while the project has pucks and no keeper",
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


class CompositionMismatch(ValueError):
    """The node's Composition Data is not the template's (`composition_mismatch` names each difference)."""

    def __init__(self, template: int, differences: list[str]) -> None:
        """Record what differs from template node `template`."""
        self.differences = differences
        super().__init__(
            f"the device's composition is not that of template {template:04X}: "
            + "; ".join(differences)
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
class Shape:
    """One element as the plan sees it: its location and its model ids (CDB form, upper case)."""

    location: int
    models: tuple[str, ...]


def template_shape(template: Node) -> list[Shape]:
    """Return the template node's elements as the new node should have them."""
    return [
        Shape(e.location, tuple(m.upper() for m in e.models)) for e in template.elements
    ]


def composition_shape(composition: C.CompositionData) -> list[Shape]:
    """Return the elements a Composition Data Status page 0 lists."""
    return [Shape(e.location, tuple(e.model_ids)) for e in composition.elements]


def composition_mismatch(template: Node, composition: C.CompositionData) -> list[str]:
    """List how `composition` differs from the template's ([] when it is the same; model order aside)."""
    out = []
    if template.cid is not None and composition.cid != template.cid:
        out.append(f"company {composition.cid:04X}, not {template.cid:04X}")
    if template.pid is not None and composition.pid != template.pid:
        out.append(f"product {composition.pid:04X}, not {template.pid:04X}")
    have, want = composition_shape(composition), template_shape(template)
    if len(have) != len(want):
        out.append(f"{len(have)} element(s), not {len(want)}")
    else:
        for index, (got, expected) in enumerate(zip(have, want, strict=True)):
            if got.location != expected.location:
                out.append(
                    f"element {index} at location {got.location:04X}, not {expected.location:04X}"
                )
            extra = sorted(set(got.models) - set(expected.models))
            missing = sorted(set(expected.models) - set(got.models))
            if extra or missing:
                out.append(
                    f"element {index} models +{','.join(extra) or '-'} -{','.join(missing) or '-'}"
                )
    present = {m for element in have for m in element.models}
    out += [f"no model {m}" for m in REQUIRED_MODELS if m not in present]
    return out


# the class of every product whose class does not depend on its insert
CLASS_BY_PRODUCT = {
    **dict.fromkeys(THERMOSTAT_PIDS, "rtr"),
    **dict.fromkeys(SOCKET_PIDS, "socket"),
    **dict.fromkeys(BLIND_ONLY_PIDS, "blind"),
    **dict.fromkeys(LAMP_ACTUATOR_PIDS, "lamp"),
}


def device_class(
    pid: int | None, function: int | None, shape: Sequence[Shape]
) -> str | None:
    """Return the node's class for its device-type groups (§1.3): lamp, socket, rtr, blind, or None for none.

    A push-button by its insert (`function`; unknown: a load element's lamp servers make it a lamp, a Generic
    Level server without them a blind, as `devices` tells a push-button's load without an InsertId).
    """
    if pid is None or pid not in PUSH_BUTTON_PIDS:
        return CLASS_BY_PRODUCT.get(pid or -1)
    if function is None:
        loads = [set(e.models) for e in shape if e.location in LOAD_LOCATIONS]
        if any("1002" in m and not LAMP_LEVEL_MODELS & m for m in loads):
            function = BLIND_FUNCTION
        elif any({"1000", "1300"} & m for m in loads):
            function = min(LAMP_FUNCTIONS)
    if function == BLIND_FUNCTION:
        return "blind"
    return "lamp" if function in LAMP_FUNCTIONS else None


@dataclass(frozen=True)
class Plan:
    """The commissioning sequence for one node: its addresses, the ordered steps and the groups they need."""

    unicast: int
    elements: int
    template: int  # the template node's primary unicast
    steps: list[Step]
    groups: list[NewGroup]
    not_covered: tuple[Gap, ...] = NOT_COVERED
    # the element whose Time Server takes the SetTime phase's Time Set; None: the node has none
    time_server: int | None = None
    device_class: str | None = None  # `device_class`
    composition: C.CompositionData | None = (
        None  # what the node answered, once `resume` planned from it
    )
    _resume: Callable[[C.CompositionData], Plan] | None = field(
        default=None, repr=False, compare=False
    )

    def phases(self) -> list[str]:
        """Return the phases in the order the commissioning passes through them (the caller's two always)."""
        have = {step.phase for step in self.steps}
        return [p for p in PHASES if p in have or p in CALLER_PHASES]

    def resume(self, composition: C.CompositionData) -> Plan:
        """Plan again from the node's own Composition Data, the same element groups; `CompositionMismatch` if not the template's."""
        assert self._resume is not None  # every plan comes from `plan()`
        return self._resume(composition)


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
    function: int | None = None,
) -> Plan:
    """Build the commissioning plan of a node provisioned at `unicast` with `elements` elements, after `template`.

    Raises `ValueError` when the element count differs from the template's, when the new node's addresses are
    not free in the CDB, when the AppKey is unknown, or when no group address is left for its element groups.
    `group_range` overrides the range element groups are allocated in (default: the first provisioner's first).
    `reserved_groups` are taken although the export does not show them: the element groups of nodes Home Assistant
    provisioned but did not record (`vault.Vault.reserved_groups`), which those nodes may hold already.
    `policy` picks the lowest free address of the range ("app") or the highest below the device-type groups
    ("top"); `export.AllocationCrowded` when a "top" pick comes too close to the app's own groups. `function`: the
    insert the device advertised (a push-button's class), else the template's. `Plan.resume` plans again once the
    node answered its Composition Data.
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
        cdb,
        unicast,
        template,
        app_key_index,
        group_range,
        set(reserved_groups),
        policy,
        insert_function(template) if function is None else function,
    )
    return builder.build(app_key.key, template_shape(template), None)


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
        function: int | None,
    ) -> None:
        self.cdb = cdb
        self.unicast = unicast
        self.template = template
        self.app_key_index = app_key_index
        self.group_range = group_range
        self.reserved_groups = reserved_groups
        self.policy = policy
        self.function = function
        self.battery = template.pid in BATTERY_PIDS
        self.steps: list[Step] = []
        self.groups: list[NewGroup] = []
        self.kept: dict[
            int, NewGroup
        ] = {}  # an earlier plan's element groups, by element
        self.shape: list[Shape] = []

    def build(
        self,
        app_key: bytes,
        shape: list[Shape],
        composition: C.CompositionData | None,
    ) -> Plan:
        """Plan every phase for the elements of `shape`; the plan resumes with this builder's settings."""
        self.steps, self.groups, self.shape = [], [], shape
        self.add_app_key(app_key)
        self.bind()
        self.set_configuration(app_key)
        self.element_groups()
        kind = device_class(self.template.pid, self.function, shape)
        self.fixed_groups(kind)
        self.disable_proxy()
        groups = list(self.groups)

        def resume(received: C.CompositionData) -> Plan:
            differences = composition_mismatch(self.template, received)
            if differences:
                raise CompositionMismatch(self.template.unicast, differences)
            self.kept = {g.element: g for g in groups}
            return self.build(app_key, composition_shape(received), received)

        time_server = next(
            (
                self.unicast + i
                for i, element in enumerate(shape)
                if TIME_SERVER in element.models
            ),
            None,
        )
        return Plan(
            self.unicast,
            len(shape),
            self.template.unicast,
            list(self.steps),
            groups,
            time_server=time_server,
            device_class=kind,
            composition=composition,
            _resume=resume,
        )

    def add(self, phase: str, pdu: bytes, expect: int, evidence: str) -> None:
        self.steps.append(Step(phase, self.unicast, pdu, expect, evidence))

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
        for index, shaped in enumerate(self.shape):
            for model_id in shaped.models:
                if model_id in BIND_LIST:
                    self.add(
                        phase,
                        C.model_app_bind(
                            self.unicast + index, model_id, self.app_key_index
                        ),
                        C.CONFIG_MODEL_APP_STATUS,
                        f"{NL} §3.3 (bind list)",
                    )
        for index, element in enumerate(self.template.elements):
            for model in element.raw_models:
                model_id = model["modelId"].upper()
                if (
                    model_id in CONFIG_MODELS
                    or model_id in BIND_LIST
                    or model_id not in self.shape[index].models
                    or self.app_key_index not in model.get("bind", [])
                ):
                    continue
                self.add(
                    phase,
                    C.model_app_bind(
                        self.unicast + index, model_id, self.app_key_index
                    ),
                    C.CONFIG_MODEL_APP_STATUS,
                    f"{NL} §3.3 (bound by the app's messenger before the first message to the element; the"
                    " template has it bound)",
                )

    def set_configuration(self, app_key: bytes) -> None:
        phase = "SetConfiguration"
        evidence = f"{NL} §3.2 step 3; {TP} §3.3"
        observed = "the installation's nodes (docs/hidden-features.md §4)"
        self.add(phase, C.gatt_proxy_set(True), C.CONFIG_GATT_PROXY_STATUS, evidence)
        self.add(
            phase,
            C.appkey_add(app_key, self.app_key_index),
            C.CONFIG_APPKEY_STATUS,
            f"{evidence} (the app adds the AppKey a second time)",
        )
        self.add(
            phase,
            C.default_ttl_set(DEFAULT_TTL),
            C.CONFIG_DEFAULT_TTL_STATUS,
            f"{evidence}; the app's value",
        )
        self.add(
            phase,
            C.relay_set(True, *DEFAULT_RELAY_RETRANSMIT),
            C.CONFIG_RELAY_STATUS,
            f"{evidence}; relay on as the app, retransmit as {observed}",
        )
        self.add(
            phase,
            C.network_transmit_set(*DEFAULT_NETWORK_TRANSMIT),
            C.CONFIG_NETWORK_TRANSMIT_STATUS,
            f"{evidence}; as {observed}",
        )
        self.add(
            "SetBlacklistFilter",
            C.beacon_set(not self.battery),
            C.CONFIG_BEACON_STATUS,
            f"{NL} §3.2 step 4 (the app does not wait for the status): on unless a battery device",
        )

    def _allocate(self, element: int) -> NewGroup:
        """Take the lowest group address of the range (`policy` "top": the highest) that nothing else uses (§1.2).

        Nothing in the export: not only the CDB `groups[]` but the `meta` rooms, room links and every live
        publication and subscription (`export.group_addresses_in_use`, what a new room is allocated around too) —
        the new node would otherwise share a group some node still listens to. Nor an earlier allocation, nor a
        reserved one (`plan`), nor the device-type groups a range may reach into. An element the first plan gave
        a group (`Plan.resume`) keeps it.
        """
        kept = self.kept.get(element)
        if kept is not None:
            self.groups.append(kept)
            return kept
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
            | {g.address for g in self.kept.values()}
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
        evidence = f"{NL} §1.4, §1.5 (publish and subscribe, TTL 0xFF, no period, no retransmission)"
        for index, element in enumerate(self.shape):
            wired = [
                m
                for m in element.models
                if m in SUPPORTED_SERVERS and m not in ELEMENT_GROUP_EXCLUDED
            ]
            if not wired:
                continue
            target = self.unicast + index
            group = self._allocate(target)
            for model_id in wired:
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
                self.add(
                    "CreateElementGroups",
                    C.model_subscription_add(target, group.address, model_id),
                    C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
                    evidence,
                )

    def _device_type_targets(self, kind: str | None) -> list[tuple[int, int]]:
        """Return (element index, device-type group) for the node's class (`device_class`, §1.3)."""
        everywhere = {"lamp": LAMPS, "socket": SOCKETS, "rtr": RTRS}
        if kind in everywhere:
            return [(i, everywhere[kind]) for i in range(len(self.shape))]
        if kind != "blind":
            return []
        levels = [
            i
            for i, e in enumerate(self.shape)
            if e.location < KEY_LOCATION and "1002" in e.models
        ]
        if not levels:
            return []
        out = [(levels[0], BLINDS)]
        if levels[-1] != levels[0]:
            out.append((levels[-1], SLATS))
        return out

    def fixed_groups(self, kind: str | None) -> None:
        for index, group in self._device_type_targets(kind):
            for model_id in self.shape[index].models:
                if model_id in DEVICE_TYPE_SERVERS:
                    self.add(
                        "FinishConfiguration",
                        C.model_subscription_add(self.unicast + index, group, model_id),
                        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
                        f"{NL} §1.3, §3.4 step 1 (device-type group of a {kind})",
                    )
        if self.template.pid in PP2_PIDS:
            clock = next(
                (i for i, e in enumerate(self.shape) if TIME_SERVER in e.models), None
            )
            if clock is not None:
                self.add(
                    "FinishConfiguration",
                    C.model_subscription_add(
                        self.unicast + clock, TIME_KEEPER_GROUP, TIME_SERVER
                    ),
                    C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
                    f"{NL} §3.4 step 9, §6.2 (time keeper group of a PP2 puck)",
                )

    def disable_proxy(self) -> None:
        if self.battery:
            self.add(
                "DisableProxy",
                C.gatt_proxy_set(False),
                C.CONFIG_GATT_PROXY_STATUS,
                f"{NL} §3.2 step 9 (low-power device)",
            )


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
