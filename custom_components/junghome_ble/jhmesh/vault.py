"""Home Assistant as a provisioner of its own, and the vault of what only it knows (review-3 N1).

Two things the network's file (the Mesh CDB the app exports) does not keep for Home Assistant by itself:

- **Its identity.** The file lists every provisioner with its allocated unicast / group / scene ranges, and the
  apps allocate only inside their own (`docs/android/network-logic.md` §1.1, §6.4) — the first free range when a
  new provisioner is created, a random free address inside it for the provisioner itself. Home Assistant sends
  from an address of its own that no range reserves, so the next provisioner an app creates may cover it (review-3
  W3). `Vault.merge_into` puts a provisioner entry for Home Assistant into the file, with ranges clear of every
  other provisioner's (`choose_ranges`), and a node entry recording Home Assistant's address inside its unicast
  range — how the Mesh CDB records a provisioner's address (the phones appear in `nodes[]` the same way). The entry
  is *appended*: the iOS library takes the first provisioner of an imported file as the local one until the app
  picks its own by UUID, so Home Assistant's never comes first.
- **The device keys of the nodes it provisioned.** A node Home Assistant adds (`onboarding`) exists in the file
  only once it is recorded, and the app never downloads the file: its next upload lacks the node (review-3 W1,
  the device-key half). The vault keeps each such node's device key from the moment provisioning completes
  (*pending* until it is recorded — a node whose commissioning failed is still reachable with it), then the node's
  CDB entry, element groups and app device rows as recorded, and `merge_into` puts back whatever a file lacks.
  Every vault node's addresses, and the element groups planned for a pending one, stay reserved
  (`reserved_unicasts`, `reserved_groups`): a node the file lacks still sends from its addresses and still holds
  its groups, so the next node Home Assistant adds must not get them (review-4 D2). The app never hands such a node
  a new NetKey either: how far each came through the app's key refresh, which Home Assistant carries it through
  (`vaultrefresh`, review-4 D11), is kept with it (`RefreshProgress`: the new key's Network ID, never the key).

The vault is the caller's to persist (`to_dict` / `from_dict`); the dict holds key material, so it belongs where
the export itself is kept. Nothing here logs, and no `repr()` or error message carries a key.

Import by the apps is **unverified**: the rules above come from the Android decompile, the installation runs the
iOS app. Try a file with the entry on a spare app install before handing it to a real one.
"""

from __future__ import annotations

import copy
import os
import uuid as uuid_mod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .cdb import CDB, Element, Node, canonical_uuid, parse_address
from .devices import DEVICE_TYPE_GROUPS, element_group_address, meta_list
from .export import _ordered_like, hexaddr

if TYPE_CHECKING:
    from .export import ProjectFile

VAULT_VERSION = 1
UNICAST_BOUNDS = (0x0001, 0x7FFF)  # Mesh CDB schema: allocatedUnicastRange
GROUP_BOUNDS = (0xC000, 0xFEFF)  # allocatedGroupRange
SCENE_BOUNDS = (0x0001, 0xFFFF)  # allocatedSceneRange (scene number 0 is prohibited)
# JUNG's fixed device-type / time-keeper groups (0xFEF5..0xFEFF) are every app provisioner's: never ours
GROUP_CEILING = DEVICE_TYPE_GROUPS[0] - 1
# how much of each space Home Assistant asks for; less where less is free (the apps take a tenth of each space)
RANGE_SIZE = 0x0100
DEFAULT_NAME = "Home Assistant"
KEY_LENGTH = 16
NETWORK_ID_LENGTH = 8

Range = tuple[int, int]


class VaultError(ValueError):
    """A stored vault that cannot be read back; the message names the field, never a value."""


class RangeError(ValueError):
    """No range for Home Assistant fits the file (its address is taken, or a space is full)."""


def _overlaps(a: Range, b: Range) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _inside(r: Range, bounds: Range) -> bool:
    return bounds[0] <= r[0] <= r[1] <= bounds[1]


@dataclass(frozen=True)
class Ranges:
    """Home Assistant's allocated ranges, each inclusive `(low, high)`."""

    unicast: Range
    group: Range
    scene: Range

    def problems(self, cdb: CDB, own_address: int, own: str | None) -> list[str]:
        """List what makes these ranges unusable in `cdb` ([] when nothing does).

        The spec's rules: each range inside its space (unicast 0x0001..0x7FFF, group 0xC000..0xFEFF — here below
        JUNG's fixed groups — scenes 0x0001..0xFFFF), low <= high, no overlap with any other provisioner's range
        of the kind; and Home Assistant's own address inside the unicast range.
        """
        out = []
        for name, r, bounds in (
            ("unicast", self.unicast, UNICAST_BOUNDS),
            ("group", self.group, (GROUP_BOUNDS[0], GROUP_CEILING)),
            ("scene", self.scene, SCENE_BOUNDS),
        ):
            if not _inside(r, bounds):
                out.append(f"the {name} range is outside its space")
        if not self.unicast[0] <= own_address <= self.unicast[1]:
            out.append(f"{own_address:04X} is outside the unicast range")
        mine = None if own is None else canonical_uuid(own)
        for p in cdb.provisioners:
            if p.uuid == mine:
                continue
            for name, r, theirs in (
                ("unicast", self.unicast, p.unicast),
                ("group", self.group, p.group),
                ("scene", self.scene, p.scene),
            ):
                if any(_overlaps(r, t) for t in theirs):
                    out.append(f"the {name} range overlaps provisioner {p.name!r}")
        return out


def _block(bounds: Range, blocked: Callable[[int], bool], size: int) -> Range | None:
    """Return the highest run of `size` free values inside `bounds`; else the longest shorter run (the highest of equals)."""
    best: Range | None = None
    run_top: int | None = None
    for value in range(bounds[1], bounds[0] - 2, -1):
        if value >= bounds[0] and not blocked(value):
            if run_top is None:
                run_top = value
            if run_top - value + 1 == size:
                return value, run_top
            continue
        if run_top is not None:
            run = (value + 1, run_top)
            if best is None or run[1] - run[0] > best[1] - best[0]:
                best = run
            run_top = None
    return best


def choose_ranges(
    cdb: CDB,
    own_address: int,
    used_groups: Iterable[int],
    *,
    own: str | None = None,
    preferred: Ranges | None = None,
    size: int = RANGE_SIZE,
) -> Ranges:
    """Return Home Assistant's ranges in `cdb`: `preferred` while it is still valid, else a deterministic new choice.

    `own` is Home Assistant's provisioner UUID: its entry and node in the file (from an earlier merge) are ours, not
    something to keep clear of. A new choice is clear of every other provisioner's range and of every address in
    use: unicast — up to `size` addresses around `own_address`, from it upwards where there is room (the apps take
    the lowest free range for their next provisioner, and their own nodes from the bottom of their ranges); group
    and scene — the highest `size` free values of their spaces, far from where the apps allocate (from the
    bottom). `used_groups` are the group addresses in use (`ProjectFile.used_group_addresses`). A stored range stays
    valid with Home Assistant's own nodes and groups inside it: it only has to keep clear of other provisioners.

    `RangeError` when Home Assistant's address is another node's, excluded or in another provisioner's range, or
    when a space has no free value left.
    """
    mine = None if own is None else canonical_uuid(own)
    others = [p for p in cdb.provisioners if p.uuid != mine]
    used = cdb.used_unicasts(own)
    if (
        not UNICAST_BOUNDS[0] <= own_address <= UNICAST_BOUNDS[1]
        or own_address in used
        or own_address in cdb.excluded_addresses
        or any(lo <= own_address <= hi for p in others for lo, hi in p.unicast)
    ):
        msg = f"Home Assistant's address {own_address:04X} is not free for a range of its own"
        raise RangeError(msg)
    if preferred is not None and not preferred.problems(cdb, own_address, own):
        return preferred

    def unicast_blocked(a: int) -> bool:
        return (
            a in used
            or a in cdb.excluded_addresses
            or any(lo <= a <= hi for p in others for lo, hi in p.unicast)
        )

    low = high = own_address
    while low > UNICAST_BOUNDS[0] and not unicast_blocked(low - 1):
        low -= 1
    while high < UNICAST_BOUNDS[1] and not unicast_blocked(high + 1):
        high += 1
    start = max(low, min(own_address, high - size + 1))
    unicast = (start, min(high, start + size - 1))

    groups_used = set(used_groups)
    group = _block(
        (GROUP_BOUNDS[0], GROUP_CEILING),
        lambda a: (
            a in groups_used or any(lo <= a <= hi for p in others for lo, hi in p.group)
        ),
        size,
    )
    if group is None:
        msg = "no group address is left for a range of Home Assistant's own"
        raise RangeError(msg)
    scene = _block(
        SCENE_BOUNDS,
        lambda s: (
            s in cdb.scenes or any(lo <= s <= hi for p in others for lo, hi in p.scene)
        ),
        size,
    )
    if scene is None:
        msg = "no scene number is left for a range of Home Assistant's own"
        raise RangeError(msg)
    return Ranges(unicast, group, scene)


@dataclass(frozen=True)
class RefreshProgress:
    """How far a vault node came through one key refresh (review-4 D11, `vaultrefresh`).

    The refresh is named by its new NetKey's Network ID (public: every beacon of the new key carries it), never by
    the key. `phase` is what the node confirmed: 0 nothing yet, 1 it holds the new key (NetKey Status), 2 it
    transmits with it (Phase Status 2), 3 it dropped the old one (Phase Status 0 after Phase Set 3).
    """

    network_id: bytes
    phase: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {"networkId": self.network_id.hex().upper(), "phase": self.phase}

    @classmethod
    def from_dict(cls, data: Any, what: str) -> RefreshProgress:
        """Read back `to_dict`'s form; `VaultError` naming `what` when it is not one."""
        if not isinstance(data, dict):
            msg = f"{what} is not an object"
            raise VaultError(msg)
        raw, phase = data.get("networkId"), data.get("phase")
        try:
            network_id = bytes.fromhex(raw) if isinstance(raw, str) else b""
        except ValueError:
            network_id = b""
        if len(network_id) != NETWORK_ID_LENGTH:
            msg = f"{what} networkId is not an 8-byte hexadecimal Network ID"
            raise VaultError(msg)
        if isinstance(phase, bool) or phase not in (0, 1, 2, 3):
            msg = f"{what} phase is not 0, 1, 2 or 3"
            raise VaultError(msg)
        return cls(network_id, phase)


@dataclass
class VaultNode:
    """A node Home Assistant provisioned: its device key, and once recorded, what the file got for it."""

    uuid: str
    unicast: int
    elements: int
    dev_key: bytes = field(repr=False)
    # the CDB node entry as recorded (it holds the device key); None while the node is pending
    entry: dict[str, Any] | None = field(default=None, repr=False)
    # its element groups: as recorded, or while pending as the commissioning plan allocated them
    groups: list[tuple[int, str]] = field(default_factory=list)
    devices: list[dict[str, Any]] = field(default_factory=list, repr=False)  # app rows
    # False for a pending node of a vault written before planned groups were kept: which groups it holds is unknown
    groups_known: bool = True
    # how far it came through the last key refresh Home Assistant carried it through; None: none so far
    key_refresh: RefreshProgress | None = None
    # what its Provisioning Capabilities offered and which method provisioned it (`provisioning.capability_record`:
    # algorithm and OOB names, sizes and flags, never a value or a key); None in a vault from before it was kept
    capabilities: dict[str, Any] | None = None

    @property
    def recorded(self) -> bool:
        """Whether the node made it into the file (else commissioning or recording did not finish)."""
        return self.entry is not None

    def addresses(self) -> range:
        """Return the node's element addresses."""
        return range(self.unicast, self.unicast + self.elements)

    def as_node(self) -> Node:
        """Return a CDB node the proxy client can address this one as: its device key and elements, no product.

        For a node the client does not know (a pending one, or one the file lost): made known for a Config
        exchange and forgotten again (`ProxyClient.add_node` / `remove_node`). Without a product id it never counts
        as a JUNG device, so its statuses are no evidence of a key refresh (`ProxyClient._follow_key_refresh`).
        """
        node = Node(self.uuid, "pending", self.unicast, self.dev_key, None)
        node.elements = [Element(a, 0, [], node) for a in self.addresses()]
        return node


@dataclass
class MergeResult:
    """What `Vault.merge_into` did: whether the file changed, which vault nodes it could not put back and why."""

    changed: bool = False
    # UUID → why a recorded node was not put back (its addresses or groups are someone else's now)
    skipped: dict[str, str] = field(default_factory=dict)
    # UUIDs the file has in another shape (removed, or provisioned anew): the vault's copy is out of date
    stale: list[str] = field(default_factory=list)


def _key(value: Any, what: str) -> bytes:
    if not isinstance(value, str) or len(value) != 2 * KEY_LENGTH:
        msg = f"{what} is not a 16-byte hexadecimal key"
        raise VaultError(msg)
    try:
        return bytes.fromhex(value)
    except ValueError as err:
        msg = f"{what} is not a 16-byte hexadecimal key"
        raise VaultError(msg) from err


def _hex(value: Any, what: str, bounds: Range) -> int:
    try:
        number = int(value, 16)
    except (TypeError, ValueError) as err:
        msg = f"{what} is not a hexadecimal number"
        raise VaultError(msg) from err
    if not bounds[0] <= number <= bounds[1]:
        msg = f"{what} is out of range"
        raise VaultError(msg)
    return number


def _range(value: Any, what: str, bounds: Range) -> Range:
    if not isinstance(value, list) or len(value) != 2:
        msg = f"{what} is not a (low, high) pair"
        raise VaultError(msg)
    low, high = (_hex(v, what, bounds) for v in value)
    if low > high:
        msg = f"{what} is empty"
        raise VaultError(msg)
    return low, high


def _optional_object(value: Any, what: str) -> dict[str, Any] | None:
    if value is not None and not isinstance(value, dict):
        msg = f"{what} is not an object"
        raise VaultError(msg)
    return value


def _uuid(value: Any, what: str) -> str:
    try:
        return str(uuid_mod.UUID(str(value))).upper()
    except ValueError as err:
        msg = f"{what} is not a UUID"
        raise VaultError(msg) from err


@dataclass
class Vault:
    """Home Assistant's provisioner identity and the nodes it provisioned (see the module docstring)."""

    uuid: str  # Home Assistant's provisioner UUID (canonical)
    node_key: bytes = field(
        repr=False
    )  # the device key of Home Assistant's own node entry (never used on air)
    name: str = DEFAULT_NAME
    ranges: Ranges | None = None
    nodes: dict[str, VaultNode] = field(default_factory=dict)

    @classmethod
    def create(cls, random: Callable[[int], bytes] = os.urandom) -> Vault:
        """Return a new identity: a random (version 4) UUID and a random device key for its node entry."""
        ident = str(uuid_mod.UUID(bytes=random(16), version=4)).upper()
        return cls(ident, random(KEY_LENGTH))

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        """Return the vault as JSON-ready data (it holds key material: keep it as private as the export)."""
        return {
            "version": VAULT_VERSION,
            "uuid": self.uuid,
            "name": self.name,
            "nodeKey": self.node_key.hex().upper(),
            "ranges": None
            if self.ranges is None
            else {
                kind: [hexaddr(r[0]), hexaddr(r[1])]
                for kind, r in (
                    ("unicast", self.ranges.unicast),
                    ("group", self.ranges.group),
                    ("scene", self.ranges.scene),
                )
            },
            "nodes": [
                {
                    "uuid": n.uuid,
                    "unicast": hexaddr(n.unicast),
                    "elements": n.elements,
                    "deviceKey": n.dev_key.hex().upper(),
                    "entry": copy.deepcopy(n.entry),
                    "groups": [{"address": hexaddr(a), "name": g} for a, g in n.groups],
                    "devices": copy.deepcopy(n.devices),
                    "groupsKnown": n.groups_known,
                    # only once there is some: a vault that never carried a node through a refresh keeps its shape
                    **(
                        {}
                        if n.key_refresh is None
                        else {"keyRefresh": n.key_refresh.to_dict()}
                    ),
                    **(
                        {}
                        if n.capabilities is None
                        else {"capabilities": copy.deepcopy(n.capabilities)}
                    ),
                }
                for n in self.nodes.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: Any) -> Vault:
        """Read back what `to_dict` wrote; `VaultError` names what is wrong (never a key)."""
        if not isinstance(data, dict):
            msg = "the vault is not an object"
            raise VaultError(msg)
        if data.get("version") != VAULT_VERSION:
            msg = "the vault has an unknown version"
            raise VaultError(msg)
        name = data.get("name", DEFAULT_NAME)
        if not isinstance(name, str):
            msg = "the vault name is not a string"
            raise VaultError(msg)
        vault = cls(
            _uuid(data.get("uuid"), "uuid"), _key(data.get("nodeKey"), "nodeKey"), name
        )
        raw = data.get("ranges")
        if raw is not None:
            if not isinstance(raw, dict):
                msg = "ranges is not an object"
                raise VaultError(msg)
            vault.ranges = Ranges(
                _range(raw.get("unicast"), "ranges unicast", UNICAST_BOUNDS),
                _range(raw.get("group"), "ranges group", GROUP_BOUNDS),
                _range(raw.get("scene"), "ranges scene", SCENE_BOUNDS),
            )
        nodes = data.get("nodes", [])
        if not isinstance(nodes, list):
            msg = "nodes is not a list"
            raise VaultError(msg)
        for i, row in enumerate(nodes):
            what = f"nodes[{i}]"
            if not isinstance(row, dict):
                msg = f"{what} is not an object"
                raise VaultError(msg)
            elements = row.get("elements")
            if (
                isinstance(elements, bool)
                or not isinstance(elements, int)
                or elements < 1
            ):
                msg = f"{what} elements is not a positive integer"
                raise VaultError(msg)
            entry = _optional_object(row.get("entry"), f"{what} entry")
            groups = row.get("groups", [])
            devices = row.get("devices", [])
            if not isinstance(groups, list) or not all(
                isinstance(g, dict) and isinstance(g.get("name"), str) for g in groups
            ):
                msg = f"{what} groups is not a list of named groups"
                raise VaultError(msg)
            if not isinstance(devices, list) or not all(
                isinstance(d, dict) for d in devices
            ):
                msg = f"{what} devices is not a list of objects"
                raise VaultError(msg)
            # a vault from before planned groups were kept wrote none for a pending node: unknown, not "none"
            known = row.get("groupsKnown", entry is not None)
            if not isinstance(known, bool):
                msg = f"{what} groupsKnown is not a boolean"
                raise VaultError(msg)
            progress = row.get("keyRefresh")
            node = VaultNode(
                _uuid(row.get("uuid"), f"{what} uuid"),
                _hex(row.get("unicast"), f"{what} unicast", UNICAST_BOUNDS),
                elements,
                _key(row.get("deviceKey"), f"{what} deviceKey"),
                entry,
                [
                    (_hex(g.get("address"), f"{what} group", GROUP_BOUNDS), g["name"])
                    for g in groups
                ],
                devices,
                known,
                None
                if progress is None
                else RefreshProgress.from_dict(progress, f"{what} keyRefresh"),
                _optional_object(row.get("capabilities"), f"{what} capabilities"),
            )
            vault.nodes[node.uuid] = node
        return vault

    # ------------------------------------------------------------------ nodes
    @property
    def pending(self) -> list[VaultNode]:
        """Nodes provisioned but not recorded in the file (their commissioning or recording did not finish)."""
        return [n for n in self.nodes.values() if not n.recorded]

    def remember_provisioned(
        self,
        uuid: str,
        unicast: int,
        elements: int,
        dev_key: bytes,
        groups: Iterable[tuple[int, str]] = (),
        key_refresh: RefreshProgress | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> VaultNode:
        """Keep a node's device key the moment provisioning completed: pending until `remember_recorded`.

        `groups`: the element groups its commissioning plan allocated, `(address, name)` — reserved with the node
        (`reserved_groups`) whether or not the commissioning got as far as wiring them. `key_refresh`: where a node
        provisioned during a key refresh starts (Phase 2 hands out the new key with the Key Refresh flag set).
        `capabilities`: what the device offered and the method used (`provisioning.capability_record`).
        """
        node = VaultNode(
            canonical_uuid(uuid),
            unicast,
            elements,
            dev_key,
            groups=list(groups),
            key_refresh=key_refresh,
            capabilities=capabilities,
        )
        self.nodes[node.uuid] = node
        return node

    def reserved_unicasts(self) -> set[int]:
        """Every element address of every vault node, pending or recorded: never another node's."""
        return {a for n in self.nodes.values() for a in n.addresses()}

    def reserved_groups(self) -> set[int]:
        """Every element group address of every vault node (a pending one's as planned): never another node's."""
        return {a for n in self.nodes.values() for a, _name in n.groups}

    @property
    def groups_unknown(self) -> list[VaultNode]:
        """Pending nodes whose element groups an older vault did not keep: their groups cannot be reserved."""
        return [n for n in self.pending if not n.groups_known]

    def remember_recorded(self, pf: ProjectFile, uuid: str) -> VaultNode:
        """Keep what `pf` records for node `uuid` (entry, element groups, app device rows); KeyError if it has none."""
        wanted = canonical_uuid(uuid)
        node = next((n for n in pf.cdb.nodes if n.uuid == wanted), None)
        if node is None:
            msg = f"no node {wanted} in the export"
            raise KeyError(msg)
        entry = next(
            n
            for n in pf.net["nodes"]
            if canonical_uuid(str(n.get("UUID", ""))) == wanted
        )
        own = {e.address for e in node.elements}
        before = self.nodes.get(wanted)
        kept = VaultNode(
            wanted,
            node.unicast,
            len(node.elements),
            node.dev_key,
            copy.deepcopy(entry),
            [
                (address, name)
                for address, name in pf.cdb.groups.items()
                if element_group_address(name) in own
            ],
            [copy.deepcopy(row) for row in _device_rows(pf.meta, wanted)],
            key_refresh=None if before is None else before.key_refresh,
            capabilities=None if before is None else before.capabilities,
        )
        self.nodes[wanted] = kept
        return kept

    def adopt_identity(self, other: Vault) -> None:
        """Take `other`'s identity (UUID, node key, name, ranges) and keep this vault's nodes (`recognise`)."""
        self.uuid, self.node_key, self.name, self.ranges = (
            other.uuid,
            other.node_key,
            other.name,
            other.ranges,
        )

    def forget(self, uuid: str) -> bool:
        """Drop node `uuid` (removed from the network); True when the vault had it."""
        return self.nodes.pop(canonical_uuid(uuid), None) is not None

    # ------------------------------------------------------------------ the file
    def ensure_ranges(self, pf: ProjectFile, own_address: int) -> Ranges:
        """Choose (or keep) Home Assistant's ranges for `pf` (`choose_ranges`) and remember them."""
        self.ranges = choose_ranges(
            pf.cdb,
            own_address,
            pf.used_group_addresses(),
            own=self.uuid,
            preferred=self.ranges,
        )
        return self.ranges

    def provisioner_entry(self, template: Any = None) -> dict[str, Any]:
        """Home Assistant's `provisioners[]` entry, keys in the order of `template` (a sibling entry)."""
        assert self.ranges is not None  # `ensure_ranges` first
        entry = {
            "provisionerName": self.name,
            "UUID": _uuid_like(template, self.uuid),
            "allocatedUnicastRange": [
                {
                    "lowAddress": hexaddr(self.ranges.unicast[0]),
                    "highAddress": hexaddr(self.ranges.unicast[1]),
                }
            ],
            "allocatedGroupRange": [
                {
                    "lowAddress": hexaddr(self.ranges.group[0]),
                    "highAddress": hexaddr(self.ranges.group[1]),
                }
            ],
            "allocatedSceneRange": [
                {
                    "firstScene": hexaddr(self.ranges.scene[0]),
                    "lastScene": hexaddr(self.ranges.scene[1]),
                }
            ],
        }
        return _ordered_like(template, entry)

    def node_entry(self, own_address: int, template: Any = None) -> dict[str, Any]:
        """Return the `nodes[]` entry recording Home Assistant's address, shaped like the phones' own (a Config Client only)."""
        entry = {
            "UUID": _uuid_like(template, self.uuid),
            "name": self.name,
            "unicastAddress": hexaddr(own_address),
            "deviceKey": self.node_key.hex().upper(),
            "security": "insecure",
            "configComplete": True,
            "excluded": False,
            "defaultTTL": 5,
            "features": {"relay": 2, "proxy": 2, "friend": 2, "lowPower": 2},
            "netKeys": [{"index": 0, "updated": False}],
            "appKeys": [{"index": 0, "updated": False}],
            "elements": [
                {
                    "index": 0,
                    "location": "0000",
                    "name": "Primary Element",
                    "models": [{"modelId": "0001", "bind": [], "subscribe": []}],
                }
            ],
        }
        return _ordered_like(template, entry)

    def merge_into(self, pf: ProjectFile, own_address: int) -> MergeResult:
        """Put Home Assistant's provisioner entry, its node and every recorded vault node `pf` lacks into `pf`.

        Idempotent: a file that has them all is left as it is (`changed` False). The provisioner entry is appended
        after every other one (never first, see the module docstring) or brought up to date; so is Home
        Assistant's node (its address follows a changed `own_address`). A recorded node the file lacks is added
        with its element groups and app device rows unless one of its addresses or groups is taken meanwhile
        (`skipped`); one the file has in another shape — excluded, another address or device key — is reported
        `stale` and left alone. Groups and scenes of `pf` are allocated in Home Assistant's ranges from here on
        (`ProjectFile.own_provisioner`). `RangeError` when no ranges fit (`choose_ranges`), or when the file names
        no other provisioner — Home Assistant's entry would then be the first, which the iOS library takes for its
        own; the file is then untouched. Any other failure (`InvalidExport` when the result would not load, a
        malformed stored entry) restores the file before it is raised: never half a merge.
        """
        if not any(p.uuid != self.uuid for p in pf.cdb.provisioners):
            msg = "the file names no provisioner of the app's: Home Assistant's entry would come first"
            raise RangeError(msg)
        self.ensure_ranges(pf, own_address)
        result = MergeResult()
        saved = copy.deepcopy((pf.net, pf.meta))
        try:
            result.changed = self._merge_identity(pf, own_address)
            if result.changed:
                pf.cdb = CDB.from_network(pf.net, pf.meta)
            for node in self.nodes.values():
                if node.recorded and self._merge_node(pf, node, result):
                    result.changed = True
        except Exception:
            pf.net.clear()
            pf.net.update(saved[0])
            pf.meta.clear()
            pf.meta.update(saved[1])
            pf.cdb = CDB.from_network(pf.net, pf.meta)
            raise
        pf.own_provisioner = self.uuid
        return result

    def _merge_identity(self, pf: ProjectFile, own_address: int) -> bool:
        changed = False
        provisioners = pf.net.setdefault("provisioners", [])
        mine = next(
            (
                p
                for p in provisioners
                if isinstance(p, dict)
                and canonical_uuid(str(p.get("UUID", ""))) == self.uuid
            ),
            None,
        )
        entry = self.provisioner_entry(
            next((p for p in provisioners if p is not mine), None)
        )
        if mine is None:
            provisioners.append(entry)
            changed = True
        else:
            for key in (
                "provisionerName",
                "allocatedUnicastRange",
                "allocatedGroupRange",
                "allocatedSceneRange",
            ):
                if not _same_value(mine.get(key), entry[key]):
                    mine[key] = entry[key]
                    changed = True
        nodes = pf.net["nodes"]
        own_node = next(
            (n for n in nodes if canonical_uuid(str(n.get("UUID", ""))) == self.uuid),
            None,
        )
        if own_node is None:
            uuids = {p.uuid for p in pf.cdb.provisioners if p.uuid != self.uuid}
            template = next(
                (n for n in nodes if canonical_uuid(str(n.get("UUID", ""))) in uuids),
                nodes[0] if nodes else None,
            )
            nodes.append(self.node_entry(own_address, template))
            changed = True
        elif parse_address(str(own_node.get("unicastAddress", ""))) != own_address:
            own_node["unicastAddress"] = hexaddr(own_address)
            changed = True
        return changed

    def _merge_node(
        self, pf: ProjectFile, node: VaultNode, result: MergeResult
    ) -> bool:
        assert node.entry is not None  # recorded
        present = next(
            (
                n
                for n in pf.net["nodes"]
                if canonical_uuid(str(n.get("UUID", ""))) == node.uuid
            ),
            None,
        )
        if present is not None:
            same = (
                present.get("excluded") is not True
                and parse_address(str(present.get("unicastAddress", "")))
                == node.unicast
                and str(present.get("deviceKey", "")).upper()
                == node.dev_key.hex().upper()
            )
            if not same:
                result.stale.append(node.uuid)
            return False
        taken = pf.cdb.used_unicasts() | pf.cdb.excluded_addresses
        if any(a in taken for a in node.addresses()):
            result.skipped[node.uuid] = "its addresses are in use"
            return False
        groups_used = pf.used_group_addresses()
        if any(a in groups_used for a, _name in node.groups):
            result.skipped[node.uuid] = "one of its element groups is in use"
            return False
        pf.add_node_entry(copy.deepcopy(node.entry), node.groups)
        rows = pf.meta.get("devices")
        if not isinstance(rows, list):
            rows = pf.meta["devices"] = []
        rows.extend(copy.deepcopy(r) for r in node.devices)
        return True


def _device_rows(meta: dict[str, Any], uuid: str) -> list[dict[str, Any]]:
    return [
        row
        for row in meta_list(meta.get("devices"))
        if isinstance(row, dict)
        and isinstance(row.get("deviceId"), dict)
        and canonical_uuid(str(row["deviceId"].get("nodeId", ""))) == uuid
    ]


def _uuid_like(template: Any, uuid: str) -> str:
    """`uuid` dashed like the apps write it, undashed when the sibling `template` is (older nRF-Mesh libraries)."""
    text = template.get("UUID") if isinstance(template, dict) else None
    return uuid.replace("-", "") if isinstance(text, str) and "-" not in text else uuid


def _same_value(have: Any, want: Any) -> bool:
    """Whether a provisioner field already says `want`: ranges compared by value (hex case and width aside)."""
    if isinstance(want, list):
        return _range_values(have) == _range_values(want)
    return bool(have == want)


def _range_values(rows: Any) -> list[tuple[int, ...]] | None:
    """`[{low, high}]` rows as number pairs, in order; None when they are not hexadecimal range rows."""
    if not isinstance(rows, list):
        return None
    try:
        return [tuple(int(v, 16) for v in row.values()) for row in rows]
    except (AttributeError, TypeError, ValueError):
        return None


def recognise(cdb: CDB, own_address: int) -> Vault | None:
    """Home Assistant's identity as an earlier merge left it in `cdb`, for a vault that was lost; None if absent.

    Recognised by what `Vault.merge_into` writes: a provisioner named `DEFAULT_NAME` whose node sits at
    `own_address` with one element holding a Config Client only (no product, unlike every JUNG device). The identity comes back with its UUID, its node's device key and the entry's
    first ranges (None when they are unusable: they are then chosen anew); the nodes it provisioned do not.
    """
    found = []
    for p in cdb.provisioners:
        if p.name != DEFAULT_NAME:
            continue
        node = next((n for n in cdb.nodes if n.uuid == p.uuid), None)
        if (
            node is None
            or node.unicast != own_address
            or node.pid is not None
            or [e.models for e in node.elements] != [["0001"]]
        ):
            continue
        found.append((p, node))
    if not found:
        return None
    p, node = found[
        0
    ]  # node addresses are unique: one node at our address, whatever the entries say
    vault = Vault(p.uuid, node.dev_key, p.name)
    if p.unicast and p.group and p.scene:
        ranges = Ranges(p.unicast[0], p.group[0], p.scene[0])
        if not ranges.problems(cdb, own_address, p.uuid):
            vault.ranges = ranges
    return vault
