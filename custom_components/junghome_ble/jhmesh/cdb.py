"""Load the nRF-Mesh / Bluetooth Mesh CDB JSON exported by the JUNG HOME app.

The document is validated as it is read (`CDB.from_network`): every field the integration relies on must have the
type and range the CDB schema gives it, both network keys must include index 0 and be 16 bytes (so must the
`oldKey` of a NetKey in key refresh phase 1 or 2, `CDB.net_key_refresh`), node addresses must be unicast and
unique, and the `meshUUID` must be a UUID (it names the file the integration stores the export under). Anything
else raises `InvalidExport`, a `ValueError` whose message describes the shape problem and never quotes the
document — the export carries every mesh key.

Virtual addresses: the schema writes a model's subscription to, or publication at, a virtual address as its 16-byte
Label UUID (32 hex digits), and a `groups[]` entry may carry one too (both nRF-Mesh libraries do; the JUNG apps
themselves allocate group addresses only). They are *parsed* — the label is hashed to its virtual address per
Mesh Profile §3.4.2.3 and kept in `CDB.virtual_labels` — but *not routed*: the upper-transport crypto takes no
label (the Label UUID is the CCM additional data, §3.8.6.2), so nothing here or in the client sends to one, and a
virtual group is never a room (`devices.is_room`).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeGuard

from .advert import JUNG_COMPANY_ID
from .crypto import AppKeyMaterial, NetKeyMaterial, aes_cmac, s1

__all__ = [
    "CDB",
    "MAX_DEPTH",
    "UUID_PATTERN",
    "Element",
    "InvalidExport",
    "Node",
    "Provisioner",
    "canonical_uuid",
    "is_virtual",
    "model_publication",
    "model_subscriptions",
    "parse_address",
    "virtual_address",
]

UUID_PATTERN = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_HEX = re.compile(
    r"[0-9A-Fa-f]+"
)  # ASCII digits only: `int(x, 16)` would also accept Unicode digits
KEY_HEX_LENGTH = 32  # a 128-bit NetKey / AppKey / device key
LABEL_HEX_LENGTH = 32  # a 16-byte Label UUID, the schema's form of a virtual address
UUID_HEX_LENGTH = (
    32  # a UUID without its dashes (older nRF-Mesh libraries wrote node UUIDs so)
)
MAX_ADDRESS = 0xFFFF
MAX_UNICAST = 0x7FFF
MAX_KEY_INDEX = 0xFFF
VIRTUAL_RANGE = (0x8000, 0xBFFF)  # §3.4.2.3: 0b10 in the top two bits, 14 bits of hash
_VTAD_SALT = s1(b"vtad")  # the virtual address salt (§3.4.2.3)
# Nesting bound of a whole document: an export is about a dozen levels deep, and a hostile one nested thousands deep
# would otherwise raise RecursionError (not a ValueError) from the JSON decoder or from any later recursive walk
MAX_DEPTH = 64
# `meta` lists whose rows carry a `name` the app shows (`MetaData.java`): a non-string one is not an app export
_META_NAMED = ("userGroups", "devices", "scenes")
# `devices.LOAD_LOCATIONS`, not imported from there: `devices` imports this module
_LOAD_LOCATIONS = (0x0001, 0x0002)


class InvalidExport(ValueError):
    """The document is not a mesh export the integration can use; the message never carries key material."""


def is_virtual(addr: int) -> bool:
    """Tell whether `addr` is a virtual address (0x8000..0xBFFF, §3.4.2.3)."""
    return VIRTUAL_RANGE[0] <= addr <= VIRTUAL_RANGE[1]


def virtual_address(label: bytes) -> int:
    """Virtual address of a Label UUID: `0b10 << 14 | AES-CMAC_salt(label) mod 2^14`, salt = s1("vtad") (§3.4.2.3)."""
    return VIRTUAL_RANGE[0] | (
        int.from_bytes(aes_cmac(_VTAD_SALT, label)[14:], "big") & 0x3FFF
    )


def parse_address(text: str) -> int:
    """Address as the CDB writes it: up to 8 hex digits, or a 32-hex Label UUID mapped to its virtual address.

    For validated trees only (`Element.raw_models`, a `ProjectFile`'s `groups[]` / `publish` entries): the shape
    check with a key-free error is `_address`.
    """
    if len(text) == LABEL_HEX_LENGTH:
        return virtual_address(bytes.fromhex(text))
    return int(text, 16)


def model_publication(raw: Mapping[str, Any]) -> int:
    """Return the publish address a CDB model entry records (0x0000 when it records none)."""
    publish = raw.get("publish")
    if not isinstance(publish, dict) or "address" not in publish:
        return 0
    return parse_address(str(publish["address"]))


def model_subscriptions(raw: Mapping[str, Any]) -> list[int]:
    """Return the addresses a CDB model entry subscribes to, in the export's order (virtual ones as 0x8xxx)."""
    return [parse_address(str(a)) for a in raw.get("subscribe", [])]


def canonical_uuid(text: str) -> str:
    """Return the apps' form of a UUID: upper-case, dashed — from that form or from the 32 undashed hex digits.

    Anything else is upper-cased as it is (callers compare, they do not validate here). The stable HA ids, the
    MAC derivation (`advert.mac_from_uuid`) and the `meta` node ids all assume the dashed form, so an export
    written by an older nRF-Mesh library (undashed `UUID`) must not change every id or lose its names.
    """
    if len(text) == UUID_HEX_LENGTH and _HEX.fullmatch(text):
        text = f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"
    return text.upper()


def _dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidExport(f"{what} is not an object")
    return value


def _list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise InvalidExport(f"{what} is not a list")
    return value


def _str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise InvalidExport(f"{what} is not a string")
    return value


def _int(value: Any, what: str, low: int = 0, high: int = MAX_ADDRESS) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidExport(f"{what} is not an integer")
    if not low <= value <= high:
        raise InvalidExport(f"{what} is out of range")
    return value


def _hex(value: Any, what: str, high: int = MAX_ADDRESS, low: int = 0) -> int:
    """Parse a hexadecimal string field (addresses, ids, scene numbers) with a range check."""
    text = _str(value, what)
    if not _HEX.fullmatch(text) or len(text) > 8:
        raise InvalidExport(f"{what} is not a hexadecimal string")
    number = int(text, 16)
    if not low <= number <= high:
        raise InvalidExport(f"{what} is out of range")
    return number


def _address(value: Any, what: str, labels: dict[int, bytes] | None) -> int:
    """Parse a group / publish / subscribe address: 4 hex digits, or a Label UUID when `labels` collects them."""
    text = _str(value, what)
    if labels is not None and len(text) == LABEL_HEX_LENGTH and _HEX.fullmatch(text):
        label = bytes.fromhex(text)
        addr = virtual_address(label)
        labels[addr] = label
        return addr
    return _hex(value, what)


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidExport(f"{what} is not a boolean")
    return value


def _check_depth(doc: Any, what: str) -> None:
    """Refuse a tree nested deeper than `MAX_DEPTH`; iterative, so the check itself cannot overflow the stack."""
    stack: list[tuple[Any, int]] = [(doc, 1)]
    while stack:
        value, depth = stack.pop()
        children = (
            value.values()
            if isinstance(value, dict)
            else value
            if isinstance(value, list)
            else ()
        )
        if children and depth >= MAX_DEPTH:
            raise InvalidExport(f"{what} is nested too deeply")
        stack.extend((child, depth + 1) for child in children)


def _json(text: str, what: str) -> Any:
    """`json.loads` with a nesting bound: `InvalidExport` rather than a RecursionError past the decoder's limit."""
    try:
        doc = json.loads(text)
    except RecursionError as err:
        raise InvalidExport(f"{what} is nested too deeply") from err
    _check_depth(doc, what)
    return doc


def _uuid(value: Any, what: str) -> str:
    """Parse a node UUID into the canonical (dashed, upper-case) form; dashed or undashed hex, nothing else."""
    text = canonical_uuid(_str(value, what))
    if not UUID_PATTERN.fullmatch(text):
        raise InvalidExport(f"{what} is not a UUID")
    return text


def _key(value: Any, what: str) -> bytes:
    """Parse a 128-bit key given as 32 hex digits; the error mentions the length only."""
    text = _str(value, what)
    if len(text) != KEY_HEX_LENGTH or not _HEX.fullmatch(text):
        raise InvalidExport(f"{what} is not a 16-byte hexadecimal key")
    return bytes.fromhex(text)


def _address_list(
    value: Any, what: str, labels: dict[int, bytes] | None = None
) -> list[int]:
    return [_address(a, f"{what} entry", labels) for a in _list(value, what)]


def _key_list(value: Any, what: str) -> dict[int, bytes]:
    """`netKeys` / `appKeys`: a non-empty list of `{index, key}`, unique indices, index 0 present."""
    keys: dict[int, bytes] = {}
    for entry in _list(value, what):
        k = _dict(entry, f"{what} entry")
        index = _int(k.get("index"), f"{what} index", high=MAX_KEY_INDEX)
        if index in keys:
            raise InvalidExport(f"{what} index {index} appears twice")
        keys[index] = _key(k.get("key"), f"{what} key")
    if 0 not in keys:
        raise InvalidExport(f"{what} has no key at index 0")
    return keys


def _key_refresh(value: list[Any], what: str) -> dict[int, tuple[bytes, int]]:
    """`netKeys` caught in a key refresh: index → (`oldKey`, `phase`) of every entry in phase 1 or 2 (Mesh CDB schema).

    `key` is then the new NetKey and `oldKey` the one it replaces. Phase 0 (normal operation) ignores `oldKey`: the
    nRF-Mesh library leaves the revoked key there (`NetworkKey.revokeOldKey`). For entries `_key_list` validated.
    """
    refresh: dict[int, tuple[bytes, int]] = {}
    for k in value:
        if phase := _int(k.get("phase", 0), f"{what} phase", high=2):
            refresh[k["index"]] = (_key(k.get("oldKey"), f"{what} oldKey"), phase)
    return refresh


def _range_list(
    value: Any, what: str, low_key: str, high_key: str
) -> list[tuple[int, int]]:
    """Parse a provisioner's allocated ranges `[{low_key, high_key}]`: both 4-hex fields present; absent = none."""
    return [
        (
            _hex(r.get(low_key), f"{what} {low_key}"),
            _hex(r.get(high_key), f"{what} {high_key}"),
        )
        for r in (_dict(r_raw, f"{what} range") for r_raw in _list(value, what))
    ]


@dataclass
class Provisioner:
    """One `provisioners[]` entry: identity and allocated ranges, each list inclusive `(low, high)` in file order.

    `uuid` is canonical (`canonical_uuid`) whatever the file wrote; it is not validated beyond that — an app that
    wrote an odd one still loads, and the ranges are what matter here.
    """

    uuid: str
    name: str
    unicast: list[tuple[int, int]] = field(default_factory=list)
    group: list[tuple[int, int]] = field(default_factory=list)
    scene: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Element:
    """One element of a node: unicast address, location descriptor and the models it hosts."""

    address: int
    location: int
    models: list[str]
    node: Node
    raw_models: list[dict[str, Any]] = field(
        default_factory=list
    )  # CDB model entries (bind/subscribe/publish)

    def model_entry(self, model: str) -> dict[str, Any] | None:
        """Return this element's CDB entry of `model` (model ids compared regardless of case); None without one."""
        for m in self.raw_models:
            if m["modelId"].upper() == model.upper():
                return m
        return None

    def subscriptions(self, model: str) -> list[int]:
        """Return the addresses the `model` model on this element subscribes to (virtual ones as 0x8xxx)."""
        entry = self.model_entry(model)
        return [] if entry is None else model_subscriptions(entry)

    def publication(self, model: str) -> int:
        """Return the address the `model` model on this element publishes to (0x0000: none, or no such model)."""
        entry = self.model_entry(model)
        return 0 if entry is None else model_publication(entry)


@dataclass
class Node:
    """A provisioned node as the CDB records it: identity, device key, product id and elements.

    `uuid` is in the canonical form (`canonical_uuid`) whatever the file wrote.
    """

    uuid: str
    name: str
    unicast: int
    dev_key: bytes = field(repr=False)  # key material never in logs or error text
    # the JUNG product id; None for the phones and any node of another company (`cid`)
    pid: int | None
    elements: list[Element] = field(default_factory=list)
    raw: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )  # the CDB node entry (holds the device key): node-wide states such as `defaultTTL` for the audit
    # company id of the composition data; None when the file does not say
    cid: int | None = None
    # being removed from the network (Mesh CDB schema `excluded`): the node is in `CDB.excluded_nodes`
    excluded: bool = False
    # the actuator function (InsertId 0x0002, `properties.ACTUATOR_FUNCTION`) the app cached for the node in the
    # export's `meta.devices[].deviceId`: what insert a push-button carries, which its composition does not tell
    insert_function: int | None = field(default=None, compare=False)
    # the ButtonLayout (0x5001, `properties.BUTTON_LAYOUT`) the app cached for the node (`meta.buttonLayoutExports`,
    # share exports of the Android app only): which keys and rockers its key elements are
    button_layout: int | None = field(default=None, compare=False)
    # the actuator function the node itself reported — its JUNG advertisement (`jhmesh.advert`) or an InsertId Get
    # — set by whoever heard it before the device model is built: `devices.insert_function` falls back to it where
    # the export cached none
    reported_function: int | None = field(default=None, compare=False)


@dataclass
class CDB:
    """The mesh configuration database of one network: keys, nodes, groups, scenes, IV index.

    `repr()` never carries key material: the key maps are left out, `Node.dev_key` is hidden by `Node`, and the
    parsed tree (`raw`, which holds every key as hex) is hidden too.
    """

    mesh_uuid: str
    net_keys: dict[int, NetKeyMaterial] = field(repr=False)
    app_keys: dict[int, AppKeyMaterial] = field(repr=False)
    nodes: list[Node]
    groups: dict[int, str]
    scenes: dict[int, list[int]]
    iv_index: int = 0
    """Lower bound of the network's IV index when the export was written.

    The Mesh CDB schema (and so both app export flavours) carries no IV index; the only trace of it is the bucket
    the nRF-Mesh library files `networkExclusions` under (the IV index at the time the addresses were excluded).
    Buckets older than *index - 2* are purged, so the highest bucket is at most two behind the network when at
    least one exclusion exists; 0 (the schema's starting point) when there is none. It is informational — the
    client learns the real index from the proxy's Secure Network Beacon (`LocalState.apply_beacon`).
    """
    export_meta: dict[str, Any] | None = (
        None  # the app's "meta" block when loaded from a share export
    )
    excluded_nodes: list[Node] = field(
        default_factory=list
    )  # nodes the file marks `excluded` (being deleted): not devices, but their addresses stay taken
    provisioner_unicast_ranges: list[tuple[int, int]] = field(
        default_factory=list
    )  # (low, high) inclusive; the phones allocate here
    provisioner_group_ranges: list[tuple[int, int]] = field(
        default_factory=list
    )  # allocatedGroupRange of every provisioner, in file order (the app allocates rooms in the first)
    provisioner_scene_ranges: list[tuple[int, int]] = field(
        default_factory=list
    )  # allocatedSceneRange, likewise
    provisioners: list[Provisioner] = field(
        default_factory=list
    )  # the same ranges per provisioner, with its UUID and name (Home Assistant's own entry told apart)
    excluded_addresses: set[int] = field(
        default_factory=set
    )  # networkExclusions: dead until the IV index moved on twice
    scene_names: dict[int, str] = field(
        default_factory=dict
    )  # CDB scene `name` (the app keeps it equal to `meta.scenes[].name`)
    virtual_labels: dict[int, bytes] = field(
        default_factory=dict
    )  # Label UUID by virtual address, for every label the file carries (parsed, not routed: module docstring)
    net_key_refresh: dict[int, tuple[NetKeyMaterial, int]] = field(
        default_factory=dict, repr=False
    )
    """NetKeys the export caught in a key refresh: index → (the old key, phase 1 or 2); `net_keys` has the new one.

    Mesh Profile §3.10.4: in phase 1 the network still transmits with the old key, from phase 2 with the new one;
    it accepts both until phase 3 revokes the old one (`rx_net_keys`, `ProxyClient.nk`).
    """
    raw: dict[str, Any] | None = field(
        default=None, repr=False
    )  # the parsed `meshNetwork` object; `Element.raw_models` are views into it, `export.ProjectFile` edits it
    # element address → element (`element`), built on first use; `reindex` drops it after `nodes` changed
    _by_addr: dict[int, Element] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _indexed_nodes: int = field(
        default=0, init=False, repr=False, compare=False
    )  # len(nodes) when `_by_addr` was built

    @staticmethod
    def parse(text: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Accept both export flavours: the raw nRF Mesh CDB and the app's "share via file" `JungHome.json`."""
        net, meta, _doc, _inner = CDB.parse_document(text)
        return net, meta

    @staticmethod
    def parse_document(
        text: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], str | None]:
        """`parse`, plus what a writer needs to keep the file's layout: (net, meta, the document, the inner text).

        The inner text is the decoded `network` payload of a share export (None for a raw CDB). The one parser of
        both flavours: `CDB.load` and `export.ProjectFile.load` validate alike.
        """
        doc = _json(text, "the export")
        if not isinstance(doc, dict):
            raise InvalidExport("not a JUNG HOME mesh export")
        if "meshNetwork" in doc:
            return _dict(doc["meshNetwork"], "meshNetwork"), None, doc, None
        if isinstance(doc.get("network"), str):
            try:
                payload = base64.b64decode(doc["network"])
                # JSON's own encodings (a BOM, UTF-16 / 32), as `json.loads(bytes)` detects them
                inner_text = payload.decode(json.detect_encoding(payload))
            except ValueError as err:  # binascii.Error, UnicodeDecodeError
                raise InvalidExport("network is not a Base64 CDB") from err
            try:
                inner = _json(inner_text, "network")
            except json.JSONDecodeError as err:
                raise InvalidExport("network is not a Base64 CDB") from err
            inner = _dict(inner, "network")
            net = inner.get("meshNetwork", inner)
            meta = doc.get("meta")
            if meta is not None and not isinstance(meta, dict):
                raise InvalidExport("meta is not an object")
            return _dict(net, "meshNetwork"), meta, doc, inner_text
        raise InvalidExport("not a JUNG HOME mesh export")

    @classmethod
    def load(cls, path: Path) -> CDB:
        """Read an export file (either flavour) and build the CDB from it."""
        net, meta = cls.parse(Path(path).read_bytes().decode("utf-8"))
        return cls.from_network(net, meta)

    @classmethod
    def from_network(
        cls, net: dict[str, Any], meta: dict[str, Any] | None = None
    ) -> CDB:
        """Build the CDB from a parsed `meshNetwork` object (kept as `raw`) and the optional `meta` block.

        Validates the shape as it goes (see the module docstring); `InvalidExport` names the offending field, never
        its value.
        """
        net = _dict(net, "meshNetwork")
        if meta is not None and not isinstance(meta, dict):
            raise InvalidExport("meta is not an object")
        if meta is not None:
            _check_meta_names(meta)
        mesh_uuid = _str(net.get("meshUUID"), "meshUUID")
        if not UUID_PATTERN.fullmatch(mesh_uuid):
            raise InvalidExport("meshUUID is not a UUID")
        net_keys = _key_list(net.get("netKeys"), "netKeys")
        key_refresh = _key_refresh(net["netKeys"], "netKeys")
        app_keys = _key_list(net.get("appKeys"), "appKeys")
        labels: dict[int, bytes] = {}
        all_nodes = _parse_nodes(_list(net.get("nodes"), "nodes"), labels)
        _note_insert_functions(all_nodes, meta)
        _note_button_layouts(all_nodes, meta)
        nodes = [n for n in all_nodes if not n.excluded]
        groups: dict[int, str] = {}
        for i, g_raw in enumerate(_list(net.get("groups"), "groups")):
            g = _dict(g_raw, f"groups[{i}]")
            address = _address(g.get("address"), f"groups[{i}] address", labels)
            if address in groups:
                raise InvalidExport(f"groups[{i}] address is used twice")
            groups[address] = _str(g.get("name"), f"groups[{i}] name")
        scenes: dict[int, list[int]] = {}
        scene_names: dict[int, str] = {}
        for i, s_raw in enumerate(_list(net.get("scenes", []), "scenes")):
            sc = _dict(s_raw, f"scenes[{i}]")
            number = _hex(sc.get("number"), f"scenes[{i}] number")
            if number in scenes:
                raise InvalidExport(f"scenes[{i}] number is used twice")
            scenes[number] = _address_list(
                sc.get("addresses", []), f"scenes[{i}] addresses"
            )
            scene_names[number] = _str(sc.get("name", ""), f"scenes[{i}] name")
        provisioners: list[Provisioner] = []
        for i, p_raw in enumerate(_list(net.get("provisioners", []), "provisioners")):
            prov = _dict(p_raw, f"provisioners[{i}]")
            what = f"provisioners[{i}]"
            provisioners.append(
                Provisioner(
                    canonical_uuid(str(prov.get("UUID", ""))),
                    str(prov.get("provisionerName", "")),
                    _range_list(
                        prov.get("allocatedUnicastRange", []),
                        f"{what} allocatedUnicastRange",
                        "lowAddress",
                        "highAddress",
                    ),
                    _range_list(
                        prov.get("allocatedGroupRange", []),
                        f"{what} allocatedGroupRange",
                        "lowAddress",
                        "highAddress",
                    ),
                    _range_list(
                        prov.get("allocatedSceneRange", []),
                        f"{what} allocatedSceneRange",
                        "firstScene",
                        "lastScene",
                    ),
                )
            )
        iv_index = 0
        excluded: set[int] = set()
        for i, x_raw in enumerate(
            _list(net.get("networkExclusions", []), "networkExclusions")
        ):
            x = _dict(x_raw, f"networkExclusions[{i}]")
            iv_index = max(
                iv_index,
                _int(
                    x.get("ivIndex", 0),
                    f"networkExclusions[{i}] ivIndex",
                    high=0xFFFFFFFF,
                ),
            )
            excluded.update(
                _address_list(
                    x.get("addresses", []), f"networkExclusions[{i}] addresses"
                )
            )
        return cls(
            mesh_uuid=mesh_uuid,
            net_keys={i: NetKeyMaterial.derive(k) for i, k in net_keys.items()},
            app_keys={i: AppKeyMaterial.derive(k) for i, k in app_keys.items()},
            nodes=nodes,
            groups=groups,
            scenes=scenes,
            export_meta=meta,
            excluded_nodes=[n for n in all_nodes if n.excluded],
            provisioner_unicast_ranges=[r for p in provisioners for r in p.unicast],
            provisioner_group_ranges=[r for p in provisioners for r in p.group],
            provisioner_scene_ranges=[r for p in provisioners for r in p.scene],
            provisioners=provisioners,
            iv_index=iv_index,
            excluded_addresses=excluded,
            scene_names=scene_names,
            virtual_labels=labels,
            net_key_refresh={
                i: (NetKeyMaterial.derive(k), phase)
                for i, (k, phase) in key_refresh.items()
            },
            raw=net,
        )

    # ---- lookups
    def rx_net_keys(self, index: int = 0) -> tuple[NetKeyMaterial, ...]:
        """Return the NetKeys at `index` the network may send with: the key, and mid key refresh the old one first."""
        if (refresh := self.net_key_refresh.get(index)) is None:
            return (self.net_keys[index],)
        return (refresh[0], self.net_keys[index])

    def element(self, addr: int) -> Element | None:
        """Return the element at unicast `addr`, if a node has one.

        A dictionary lookup: it runs for every message heard, every entity's availability and
        every keep-alive target, and a scan of every node cost tens of microseconds on a mesh of hundreds. Whoever
        adds a node to `nodes`, takes one out or moves an element calls `reindex` (`ProxyClient.add_node` /
        `remove_node`; the export's own edits build a new `CDB`). A list that grew or shrank without it is
        re-indexed all the same — a net, not the contract: a stale index would hand out the wrong device key.
        """
        index = self._by_addr
        if index is None or self._indexed_nodes != len(self.nodes):
            index = self._by_addr = self._index()
            self._indexed_nodes = len(self.nodes)
        return index.get(addr)

    def _index(self) -> dict[int, Element]:
        """Every element by address; of two nodes claiming one address the first in `nodes` wins, as a scan found."""
        index: dict[int, Element] = {}
        for n in self.nodes:
            for e in n.elements:
                index.setdefault(e.address, e)
        return index

    def reindex(self) -> None:
        """Forget the address index after a change of `nodes` or of an element's address: the next lookup rebuilds it."""
        self._by_addr = None

    def index_is_current(self) -> bool:
        """Whether the address index (when built) still matches `nodes`: what a missing `reindex` call breaks (tests)."""
        if (index := self._by_addr) is None:
            return True
        fresh = self._index()
        return index.keys() == fresh.keys() and all(
            index[a] is e for a, e in fresh.items()
        )

    def node_by_addr(self, addr: int) -> Node | None:
        """Return the node owning the element at unicast `addr`."""
        e = self.element(addr)
        return e.node if e else None

    def label(self, addr: int) -> str:
        """Describe `addr` for logs: group name, or the owning node and element location."""
        if addr in self.groups:
            return f"{addr:04X} '{self.groups[addr]}'"
        e = self.element(addr)
        if e:
            return f"{addr:04X} ({e.node.name} @{e.node.unicast:04X} el loc {e.location:04X})"
        return f"{addr:04X}"

    def resolve(self, target: str) -> int:
        """Accept a hex address ('C00F', '0x148') or a group name."""
        t = target.strip()
        for a, name in self.groups.items():
            if name.lower() == t.lower():
                return a
        return int(t, 16)

    def used_unicasts(self, own: str | None = None) -> set[int]:
        """Return every element address a node occupies, an excluded node's included (it may still be on air).

        `own`: the UUID of Home Assistant's own provisioner; the node recording its address is left
        out, as that address is ours.
        """
        mine = None if own is None else canonical_uuid(own)
        return {
            e.address
            for n in (*self.nodes, *self.excluded_nodes)
            if n.uuid != mine
            for e in n.elements
        }

    def unicast_is_free(
        self, addr: int, used: set[int] | None = None, *, own: str | None = None
    ) -> bool:
        """Tell whether we may use `addr` as our own source address.

        True when it is not an element of any node, not inside a provisioner's allocated range (the phone hands
        those out) and not excluded (replay-protected until the IV index moved on twice). `used` is
        `used_unicasts()` when the caller already has it. `own`: the UUID of Home Assistant's own provisioner — its
        node and its range are ours, not someone else's (`used` must then leave its node out too).
        """
        return (
            addr not in (self.used_unicasts(own) if used is None else used)
            and addr not in self.excluded_addresses
            and not any(
                low <= addr <= high for low, high in self.foreign_unicast_ranges(own)
            )
        )

    def own_provisioner(self, own: str | None) -> Provisioner | None:
        """Return the `provisioners[]` entry whose UUID is `own` (Home Assistant's), if the file has it."""
        mine = None if own is None else canonical_uuid(own)
        return next((p for p in self.provisioners if p.uuid == mine), None)

    def foreign_unicast_ranges(self, own: str | None = None) -> list[tuple[int, int]]:
        """Every provisioner's allocated unicast ranges but those of the provisioner `own` (None: all of them).

        Dropped by the entry they belong to, not by value: another provisioner's range that happens to equal one of
        ours stays. Without `own`, the flat `provisioner_unicast_ranges` as they are.
        """
        mine = self.own_provisioner(own)
        if mine is None:
            return list(self.provisioner_unicast_ranges)
        return [r for p in self.provisioners if p is not mine for r in p.unicast]

    def suggest_unicast(self, start: int = 0x0D00) -> int | None:
        """Return the first address from `start` upwards (then from 0x0001) that `unicast_is_free` admits."""
        used = self.used_unicasts()
        for addr in (*range(start, 0x8000), *range(1, start)):
            if self.unicast_is_free(addr, used):
                return int(addr)
        return None


def _parse_nodes(entries: list[Any], labels: dict[int, bytes]) -> list[Node]:
    """`nodes[]`: unique canonical UUIDs, unique element addresses, validated models; labels collected."""
    nodes: list[Node] = []
    used: set[int] = set()
    uuids: set[str] = set()
    for i, entry in enumerate(entries):
        n = _dict(entry, f"nodes[{i}]")
        what = f"nodes[{i}]"
        cid = _hex(n["cid"], f"{what} cid") if n.get("cid") is not None else None
        pid = _hex(n["pid"], f"{what} pid") if n.get("pid") else None
        if cid is not None and cid != JUNG_COMPANY_ID:
            pid = None  # a product id names a JUNG product only under JUNG's company id: not a device
        node = Node(
            _uuid(n.get("UUID"), f"{what} UUID"),
            _str(n.get("name"), f"{what} name"),
            _hex(n.get("unicastAddress"), f"{what} unicastAddress", MAX_UNICAST, 1),
            _key(n.get("deviceKey"), f"{what} deviceKey"),
            pid,
            raw=n,
            cid=cid,
            excluded=_bool(n.get("excluded", False), f"{what} excluded"),
        )
        if node.uuid in uuids:
            raise InvalidExport(f"{what} UUID is used twice")
        uuids.add(node.uuid)
        for j, e_raw in enumerate(_list(n.get("elements"), f"{what} elements")):
            e = _dict(e_raw, f"{what} elements[{j}]")
            ewhat = f"{what} elements[{j}]"
            address = node.unicast + _int(e.get("index"), f"{ewhat} index")
            if address > MAX_UNICAST:
                raise InvalidExport(f"{ewhat} address is out of range")
            if address in used:
                raise InvalidExport(f"{ewhat} address is used twice")
            used.add(address)
            models = _list(e.get("models"), f"{ewhat} models")
            ids: list[str] = []
            for k, m_raw in enumerate(models):
                m = _dict(m_raw, f"{ewhat} models[{k}]")
                mwhat = f"{ewhat} models[{k}]"
                _hex(m.get("modelId"), f"{mwhat} modelId", high=0xFFFFFFFF)
                ids.append(m["modelId"])
                _address_list(m.get("subscribe", []), f"{mwhat} subscribe", labels)
                for b in _list(m.get("bind", []), f"{mwhat} bind"):
                    _int(b, f"{mwhat} bind entry", high=MAX_KEY_INDEX)
                pub = m.get("publish")
                if pub is not None and "address" in _dict(pub, f"{mwhat} publish"):
                    _address(pub["address"], f"{mwhat} publish address", labels)
            node.elements.append(
                Element(
                    address,
                    _hex(e.get("location"), f"{ewhat} location"),
                    ids,
                    node,
                    models,
                )
            )
        # an element is identified by its `index` (address = unicast + index), not its place in the file:
        # callers read `elements[0]` as the primary and `ProjectFile._element(node, i)` by position
        node.elements.sort(key=lambda e: e.address)
        nodes.append(node)
    return nodes


def _check_meta_names(meta: dict[str, Any]) -> None:
    """Refuse a room, device or scene `name` in `meta` that is not a string (absent / null is fine): the apps decode one."""
    for key in _META_NAMED:
        rows = meta.get(key)
        for i, row in enumerate(rows if isinstance(rows, list) else []):
            if isinstance(row, dict) and row.get("name") is not None:
                _str(row["name"], f"meta {key}[{i}] name")


def _note_insert_functions(nodes: list[Node], meta: dict[str, Any] | None) -> None:
    """Set `Node.insert_function` from the app's `meta.devices[].deviceId` rows, best effort (unknown stays None).

    Every app device of a node carries the node's InsertId; a row covering a load location wins over a key-side
    one should they ever disagree. A row without an integer function or a known node id is skipped.
    """
    by_uuid = {n.uuid: n for n in nodes}
    rows = (meta or {}).get("devices")
    for row in rows if isinstance(rows, list) else []:
        did = row.get("deviceId") if isinstance(row, dict) else None
        if not isinstance(did, dict) or not isinstance(did.get("nodeId"), str):
            continue
        node = by_uuid.get(canonical_uuid(did["nodeId"]))
        function = did.get("actuatorFunctionId")
        if node is None or isinstance(function, bool) or not isinstance(function, int):
            continue
        locations = did.get("locationIds")
        load = isinstance(locations, list) and any(
            x in _LOAD_LOCATIONS for x in locations
        )
        if node.insert_function is None or load:
            node.insert_function = function


def _note_button_layouts(nodes: list[Node], meta: dict[str, Any] | None) -> None:
    """Set `Node.button_layout` from the app's `meta.buttonLayoutExports` rows (`{mode, elementAddress}`), best effort.

    A row names an element of the node (the app writes the primary one); a row without an integer mode or for an
    address no node has is skipped.
    """
    by_address = {e.address: n for n in nodes for e in n.elements}
    rows = (meta or {}).get("buttonLayoutExports")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        mode, address = row.get("mode"), row.get("elementAddress")
        if not (_plain_int(mode) and _plain_int(address)):
            continue
        if (node := by_address.get(address)) is not None:
            node.button_layout = mode


def _plain_int(value: Any) -> TypeGuard[int]:
    """Whether a JSON value is an integer (a boolean is not one, though Python counts it as one)."""
    return isinstance(value, int) and not isinstance(value, bool)
