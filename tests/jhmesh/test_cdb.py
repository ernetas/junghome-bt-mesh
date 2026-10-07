"""jhmesh.cdb: loading the synthetic JUNG HOME exports and the address/name lookups."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from jhmesh.cdb import (
    CDB,
    MAX_DEPTH,
    Element,
    InvalidExport,
    Node,
    canonical_uuid,
    is_virtual,
    parse_address,
    virtual_address,
)
from jhmesh.crypto import AppKeyMaterial, NetKeyMaterial

from .conftest import (
    CDB_PATH,
    FIXTURES,
    GATEWAY,
    GROUP_LIVING,
    GROUP_WC,
    LIGHT_2G,
    OUR_SRC,
    PHONE,
    PROXY_NODE,
    SENSOR,
    SOCKET,
    on_small_stack,
)

GROUP_KITCHEN = 0xC011
DIMMER = 0x0300
ACTUATOR = 0x0400


def test_load_keys_and_identity(cdb: CDB):
    assert cdb.mesh_uuid == "1BAF3ADE-0000-4000-8000-000000000001"
    assert cdb.iv_index == 0
    assert cdb.export_meta is None
    assert set(cdb.net_keys) == {0}
    assert set(cdb.app_keys) == {0}
    nk, ak = cdb.net_keys[0], cdb.app_keys[0]
    assert isinstance(nk, NetKeyMaterial)
    assert isinstance(ak, AppKeyMaterial)
    assert nk == NetKeyMaterial.derive(
        bytes.fromhex("00112233445566778899aabbccddeeff")
    )
    assert ak == AppKeyMaterial.derive(
        bytes.fromhex("ffeeddccbbaa99887766554433221100")
    )


def test_load_nodes_and_elements(cdb: CDB):
    by_addr = {n.unicast: n for n in cdb.nodes}
    assert set(by_addr) == {
        PHONE,
        GATEWAY,
        PROXY_NODE,
        LIGHT_2G,
        SOCKET,
        DIMMER,
        ACTUATOR,
    }
    assert [n.name for n in cdb.nodes[:5]] == [
        "iPhone",
        "Gateway",
        "Push-button 1-gang",
        "Push-button 2-gang",
        "Socket",
    ]
    assert {a: n.pid for a, n in by_addr.items()} == {
        PHONE: None,
        GATEWAY: 0x000B,
        PROXY_NODE: 0x0001,
        LIGHT_2G: 0x0002,
        SOCKET: 0x0003,
        DIMMER: 0x0001,
        ACTUATOR: 0x0010,
    }
    phone, socket = by_addr[PHONE], by_addr[SOCKET]
    assert phone.uuid == "00000001-0000-4000-8000-000000000001"
    assert phone.dev_key == bytes(15) + b"\x01"
    assert socket.dev_key == bytes(15) + b"\x72"
    assert [e.address for e in socket.elements] == [SOCKET, SENSOR]
    assert [e.location for e in socket.elements] == [0x0001, 0x0040]
    assert "1100" in socket.elements[1].models
    assert "1000" in socket.elements[0].models
    assert [e.location for e in by_addr[ACTUATOR].elements] == [0x0001, 0x0002]
    for n in cdb.nodes:
        for e in n.elements:
            assert isinstance(e, Element)
            assert e.node is n
            assert [m["modelId"] for m in e.raw_models] == e.models


def test_groups_and_scenes(cdb: CDB):
    assert cdb.groups[GROUP_WC] == "WC"
    assert cdb.groups[GROUP_LIVING] == "Living room"
    assert cdb.groups[GROUP_KITCHEN] == "Kitchen"
    assert cdb.groups[0xC061] == "element group #0x148"
    assert cdb.groups[0xFEF5] == "device type group #0xFEF5"
    assert all(0xC000 <= a <= 0xFEFF for a in cdb.groups)
    assert cdb.scenes == {1: [PROXY_NODE], 2: []}


def test_element_and_node_lookups(cdb: CDB):
    e = cdb.element(PROXY_NODE)
    assert e is not None
    assert e.location == 0x0001
    assert e.node.name == "Push-button 1-gang"
    e = cdb.element(PROXY_NODE + 1)
    assert e is not None
    assert e.location == 0x0040
    assert e.node.unicast == PROXY_NODE
    assert cdb.element(0x0150) is None
    assert cdb.element(GROUP_WC) is None
    node = cdb.node_by_addr(LIGHT_2G + 3)
    assert isinstance(node, Node)
    assert node.name == "Push-button 2-gang"
    assert cdb.node_by_addr(0x7FFF) is None


def test_the_address_index_follows_reindex(cdb: CDB):
    """Review-4 R4-9: lookups are a dictionary built on first use; a change of an element's address shows only after
    `reindex` (`index_is_current` tells), a node added or taken out without one is caught by the count."""
    assert cdb.index_is_current()  # nothing built yet
    first = cdb.element(PROXY_NODE)
    assert first is not None
    assert cdb.index_is_current()
    first.address = 0x0700  # moved in place: the index still has it where it was
    assert not cdb.index_is_current()
    assert cdb.element(PROXY_NODE) is first
    cdb.reindex()
    assert cdb.element(0x0700) is first
    assert cdb.element(PROXY_NODE) is None
    assert cdb.index_is_current()
    node = first.node
    node.elements[0] = Element(0x0700, first.location, first.models, node)
    assert not cdb.index_is_current()  # the same addresses, another element
    cdb.reindex()
    assert cdb.element(0x0700) is node.elements[0]
    extra = Node("00000000-0000-4000-8000-0000000000aa", "extra", 0x0710, bytes(16), 1)
    extra.elements = [Element(0x0710, 0x0001, [], extra)]
    cdb.nodes.append(extra)  # no reindex: the node count tells
    assert cdb.node_by_addr(0x0710) is extra
    twin = Node("00000000-0000-4000-8000-0000000000bb", "twin", 0x0710, bytes(16), 1)
    twin.elements = [Element(0x0710, 0x0001, [], twin)]
    cdb.nodes.append(twin)
    assert (
        cdb.node_by_addr(0x0710) is extra
    )  # two claim the address: the first in `nodes`, as a scan found
    cdb.nodes.remove(extra)
    assert cdb.node_by_addr(0x0710) is twin
    assert cdb.index_is_current()


def test_label(cdb: CDB):
    assert cdb.label(GROUP_WC) == "C00F 'WC'"
    assert cdb.label(PROXY_NODE + 1) == "0149 (Push-button 1-gang @0148 el loc 0040)"
    assert cdb.label(0x0150) == "0150"
    assert cdb.label(0xFFFF) == "FFFF"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("WC", GROUP_WC),
        ("wc", GROUP_WC),
        ("  Living ROOM ", GROUP_LIVING),
        ("C00F", GROUP_WC),
        ("c010", GROUP_LIVING),
        ("0x148", PROXY_NODE),
        ("0148", PROXY_NODE),
        (" 172 ", SOCKET),
    ],
)
def test_resolve(cdb: CDB, target: str, expected: int):
    assert cdb.resolve(target) == expected


def test_resolve_rejects_unknown_names(cdb: CDB):
    with pytest.raises(ValueError, match="invalid literal for int"):
        cdb.resolve("Bathroom")


def test_used_unicasts(cdb: CDB):
    assert cdb.used_unicasts() == {
        PHONE,
        GATEWAY,
        PROXY_NODE,
        PROXY_NODE + 1,
        PROXY_NODE + 2,
        LIGHT_2G,
        LIGHT_2G + 1,
        LIGHT_2G + 2,
        LIGHT_2G + 3,
        LIGHT_2G + 4,
        SOCKET,
        SENSOR,
        DIMMER,
        DIMMER + 1,
        ACTUATOR,
        ACTUATOR + 1,
    }


def test_provisioner_ranges_and_exclusions(cdb: CDB):
    assert cdb.provisioner_unicast_ranges == [
        (0x0001, 0x0CCC)
    ]  # the phone allocates node addresses here
    assert cdb.excluded_addresses == {0x0002}


@pytest.mark.parametrize(
    ("addr", "free"),
    [
        (OUR_SRC, True),
        (0x0CCD, True),
        (0x7FFF, True),
        (PROXY_NODE, False),
        (SENSOR, False),  # element addresses
        (0x0001, False),
        (0x0500, False),
        (0x0CCC, False),  # inside the provisioner's allocated range
        (0x0002, False),  # excluded (also inside the range)
    ],
)
def test_unicast_is_free(cdb: CDB, addr: int, free: bool):
    assert cdb.unicast_is_free(addr) is free


def test_unicast_is_free_honours_every_range_and_exclusion(tmp_path: Path):
    """Several provisioners, several ranges each, exclusions of several IV indexes: all of them count."""
    raw = json.loads(CDB_PATH.read_text())
    net = raw["meshNetwork"]
    net["provisioners"].append(
        {
            "provisionerName": "Second phone",
            "UUID": "00000000-0000-4000-8000-000000000002",
            "allocatedUnicastRange": [
                {"lowAddress": "1000", "highAddress": "10FF"},
                {"lowAddress": "2000", "highAddress": "2000"},
            ],
            "allocatedGroupRange": [],
            "allocatedSceneRange": [],
        }
    )
    net["networkExclusions"].append({"ivIndex": 1, "addresses": ["0D05", "0D06"]})
    p = tmp_path / "MeshNetwork.json"
    p.write_text(json.dumps(raw))
    cdb = CDB.load(p)
    assert cdb.provisioner_unicast_ranges == [
        (0x0001, 0x0CCC),
        (0x1000, 0x10FF),
        (0x2000, 0x2000),
    ]
    assert cdb.excluded_addresses == {0x0002, 0x0D05, 0x0D06}
    assert [
        a
        for a in (0x0FFF, 0x1000, 0x1080, 0x10FF, 0x1100, 0x2000, 0x2001)
        if cdb.unicast_is_free(a)
    ] == [0x0FFF, 0x1100, 0x2001]
    assert [a for a in (0x0D04, 0x0D05, 0x0D06, 0x0D07) if cdb.unicast_is_free(a)] == [
        0x0D04,
        0x0D07,
    ]


def test_suggest_unicast_skips_what_is_taken_and_wraps_around(
    cdb: CDB, monkeypatch: pytest.MonkeyPatch
):
    """The first free address from `start` upwards, then from 0x0001; None when the whole unicast range is taken."""
    assert cdb.suggest_unicast() == OUR_SRC
    # past the nodes' elements and the provisioner's range (0x0001..0x0CCC)
    assert cdb.suggest_unicast(PROXY_NODE) == 0x0CCD
    # nothing free from 0x7FFF upwards: wraps around to 0x0001 and on
    cdb.excluded_addresses.add(0x7FFF)
    assert cdb.suggest_unicast(0x7FFF) == 0x0CCD
    monkeypatch.setattr(CDB, "unicast_is_free", lambda *_: False)
    assert cdb.suggest_unicast() is None


def test_element_subscriptions(cdb: CDB):
    light = cdb.element(PROXY_NODE)
    assert light is not None
    assert light.subscriptions("1000") == [0xC061, 0xFEF5, GROUP_WC]
    assert light.subscriptions("05271013") == [0xC061]
    assert light.subscriptions("0000") == []  # model present, no subscriptions
    assert light.subscriptions("1300") == []  # model absent
    button = cdb.element(PROXY_NODE + 1)
    assert button is not None
    assert button.subscriptions("1001") == [0xC005]


def test_element_publication_and_subscriptions_ignore_the_case_of_a_model_id(cdb: CDB):
    """One rule for the audit, the pre-flight and the export's edits: a model id matches whatever its case."""
    light = cdb.element(PROXY_NODE)
    assert light is not None
    entry = light.model_entry("05271013")
    assert entry is not None
    entry["modelId"] = "0527101a"
    entry["publish"] = {"address": "C061"}
    assert light.model_entry("0527101A") is entry
    assert light.subscriptions("0527101A") == [0xC061]
    assert light.publication("0527101A") == 0xC061
    assert light.publication("1300") == 0  # model absent
    entry["publish"] = {}
    assert (
        light.publication("0527101a") == 0
    )  # an entry without an address publishes nowhere


# ----------------------------------------------------------------------------- export flavours


def test_parse_raw_cdb_flavour():
    net, meta = CDB.parse(CDB_PATH.read_text())
    assert meta is None
    assert net["meshUUID"] == "1BAF3ADE-0000-4000-8000-000000000001"


def test_load_share_export_flavour():
    cdb = CDB.load(FIXTURES / "JungHome.json")
    assert cdb.export_meta is not None
    assert "devices" in cdb.export_meta
    reference = CDB.load(CDB_PATH)
    assert cdb.mesh_uuid == reference.mesh_uuid
    assert cdb.net_keys == reference.net_keys
    assert cdb.app_keys == reference.app_keys
    assert cdb.used_unicasts() == reference.used_unicasts()
    assert cdb.groups == reference.groups
    assert cdb.scenes == reference.scenes
    assert cdb.provisioner_unicast_ranges == reference.provisioner_unicast_ranges
    assert cdb.excluded_addresses == reference.excluded_addresses


def test_parse_share_export_with_bare_network_and_no_meta(tmp_path: Path):
    net = json.loads(CDB_PATH.read_text())["meshNetwork"]
    doc = {
        "version": "1.1",
        "network": base64.b64encode(json.dumps(net).encode()).decode(),
    }
    p = tmp_path / "JungHome.json"
    p.write_text(json.dumps(doc))
    parsed, meta = CDB.parse(p.read_text())
    assert meta is None
    assert parsed["meshUUID"] == net["meshUUID"]
    assert CDB.load(p).used_unicasts() == CDB.load(CDB_PATH).used_unicasts()


def test_parse_rejects_foreign_documents():
    with pytest.raises(ValueError, match="not a JUNG HOME mesh export"):
        CDB.parse(json.dumps({"hello": "world"}))
    with pytest.raises(ValueError, match="not a JUNG HOME mesh export"):
        CDB.parse(
            json.dumps({"network": {"meshNetwork": {}}})
        )  # network must be a Base64 string


def test_load_tolerates_missing_optional_fields(tmp_path: Path):
    raw = json.loads(CDB_PATH.read_text())
    net = raw["meshNetwork"]
    del net["scenes"]
    for n in net["nodes"]:
        n.pop("pid", None)
    p = tmp_path / "MeshNetwork.json"
    p.write_text(json.dumps(raw))
    cdb = CDB.load(p)
    assert cdb.scenes == {}
    assert all(n.pid is None for n in cdb.nodes)
    assert cdb.used_unicasts() == CDB.load(CDB_PATH).used_unicasts()


def test_load_tolerates_missing_provisioners_and_exclusions(tmp_path: Path):
    """Without the (optional) provisioner ranges and exclusions only the element addresses are taken."""
    raw = json.loads(CDB_PATH.read_text())
    net = raw["meshNetwork"]
    del net["provisioners"], net["networkExclusions"]
    p = tmp_path / "MeshNetwork.json"
    p.write_text(json.dumps(raw))
    cdb = CDB.load(p)
    assert cdb.provisioner_unicast_ranges == []
    assert cdb.excluded_addresses == set()
    assert cdb.unicast_is_free(0x0500)
    assert not cdb.unicast_is_free(PROXY_NODE)

    net["provisioners"] = [
        {"provisionerName": "iPhone", "UUID": "00000001-0000-4000-8000-000000000001"}
    ]  # no ranges at all
    net["networkExclusions"] = [{"ivIndex": 0}]
    p.write_text(json.dumps(raw))
    cdb = CDB.load(p)
    assert cdb.provisioner_unicast_ranges == []
    assert cdb.excluded_addresses == set()


# ----------------------------------------------------------------------------- the parsed tree (for the writer)


def test_load_keeps_the_parsed_tree_and_scene_names(cdb: CDB):
    assert cdb.raw is not None
    assert cdb.raw["meshUUID"] == cdb.mesh_uuid
    assert cdb.scene_names == {1: "Scene #1", 2: "Scene #2"}
    light = cdb.element(PROXY_NODE)
    assert light is not None
    raw_node = next(n for n in cdb.raw["nodes"] if n["unicastAddress"] == "0148")
    assert light.raw_models is raw_node["elements"][0]["models"]  # views, not copies
    raw_node["elements"][0]["models"][0]["subscribe"] = ["C0FF"]
    assert light.subscriptions(light.models[0]) == [0xC0FF]
    assert "$schema" not in repr(cdb)  # the tree never shows in repr (repr=False)


def test_from_network_builds_the_same_cdb_as_load():
    net, meta = CDB.parse(CDB_PATH.read_text())
    cdb = CDB.from_network(net, meta)
    reference = CDB.load(CDB_PATH)
    assert cdb.raw is net
    assert cdb.export_meta is None
    assert cdb.groups == reference.groups
    assert cdb.scenes == reference.scenes
    assert cdb.scene_names == reference.scene_names
    assert cdb.used_unicasts() == reference.used_unicasts()
    assert cdb.net_keys == reference.net_keys
    net["scenes"] = [{"number": "0005", "addresses": []}]  # a scene without a name
    assert CDB.from_network(net, {"devices": []}).scene_names == {5: ""}
    assert CDB.from_network(net, {"devices": []}).export_meta == {"devices": []}


# ----------------------------------------------------------------------------- IV index hint and key hygiene


def test_iv_index_is_the_highest_exclusion_bucket():
    """The CDB schema carries no IV index; the `networkExclusions` buckets are its only trace (a lower bound)."""
    net, _ = CDB.parse(CDB_PATH.read_text())
    assert CDB.from_network(net).iv_index == 0  # one bucket, filed under IV index 0
    net["networkExclusions"] = [
        {"ivIndex": 3, "addresses": ["0002"]},
        {"ivIndex": 5, "addresses": ["0003", "0004"]},
        {"addresses": ["0005"]},  # a bucket without an index counts as 0
    ]
    cdb = CDB.from_network(net)
    assert cdb.iv_index == 5
    assert cdb.excluded_addresses == {2, 3, 4, 5}
    del net["networkExclusions"]
    assert CDB.from_network(net).iv_index == 0


@pytest.mark.parametrize("phase", [1, 2])
def test_a_netkey_in_key_refresh_keeps_its_old_key(phase: int):
    """Mesh CDB schema: mid key refresh `key` is the new NetKey and `oldKey` the one it replaces, which the network
    still transmits with in phase 1 and still accepts in phase 2. Loading `key` alone lost the old one."""
    net = _network()
    old = net["netKeys"][0]["key"]
    new = "5a" * 16
    net["netKeys"][0].update(key=new, oldKey=old, phase=phase)
    net["netKeys"].append({"index": 1, "key": "11" * 16, "phase": 0})
    cdb = CDB.from_network(net)
    assert cdb.net_keys[0].key.hex() == new
    old_nk, refresh_phase = cdb.net_key_refresh[0]
    assert (old_nk.key.hex(), refresh_phase) == (old.lower(), phase)
    assert [nk.key.hex() for nk in cdb.rx_net_keys(0)] == [old.lower(), new]
    assert cdb.rx_net_keys(1) == (cdb.net_keys[1],)
    assert old.lower() not in repr(cdb).lower()


def test_an_old_key_left_after_the_refresh_is_ignored():
    """Phase 0: the nRF-Mesh library keeps the revoked `oldKey` in the file (`NetworkKey.revokeOldKey`)."""
    net = _network()
    net["netKeys"][0].update(oldKey="5a" * 16, phase=0)
    cdb = CDB.from_network(net)
    assert cdb.net_key_refresh == {}
    assert cdb.rx_net_keys() == (cdb.net_keys[0],)


def test_reprs_never_carry_key_material(cdb: CDB):
    """`repr()` of the CDB, its nodes, elements and key material must be safe to log."""
    net, _ = CDB.parse(CDB_PATH.read_text())
    netkey = bytes.fromhex(net["netKeys"][0]["key"])
    appkey = bytes.fromhex(net["appKeys"][0]["key"])
    assert cdb.net_keys[0].key == netkey  # the fixture keys are what we look for
    assert cdb.app_keys[0].key == appkey
    texts = [
        repr(cdb),
        str(cdb),
        repr(cdb.net_keys[0]),
        repr(cdb.app_keys[0]),
        *(repr(n) for n in cdb.nodes),
        *(repr(e) for n in cdb.nodes for e in n.elements),
    ]
    nk = cdb.net_keys[0]
    secrets = [
        netkey,
        appkey,
        nk.enc_key,
        nk.priv_key,
        nk.identity_key,
        nk.beacon_key,
        *(n.dev_key for n in cdb.nodes),
    ]
    for text in texts:
        assert "net_keys" not in text
        assert "app_keys" not in text
        for secret in secrets:
            assert secret.hex() not in text.lower()
            assert repr(secret)[2:-1] not in text  # the bytes literal form
    assert "nid=30" in repr(cdb.net_keys[0])  # the public parts stay
    assert repr(cdb.app_keys[0]) == f"AppKeyMaterial(aid={cdb.app_keys[0].aid})"


# ----------------------------------------------------------------------------- shape validation (malformed exports)


def _network() -> dict:
    return json.loads(CDB_PATH.read_text())["meshNetwork"]


def _node(n: dict, i: int = 0) -> dict:
    return n["nodes"][i]


def _first_model(n: dict) -> dict:
    return n["nodes"][0]["elements"][0]["models"][0]


# "0148" in digits `int(x, 16)` happily accepts but `bytes.fromhex` and the mesh do not
FULLWIDTH_0148 = "".join(chr(0xFF10 + d) for d in (0, 1, 4, 8))
ARABIC_0148 = "".join(chr(0x0660 + d) for d in (0, 1, 4, 8))


def _duplicate_unicast(n: dict) -> None:
    n["nodes"][1]["unicastAddress"] = n["nodes"][0]["unicastAddress"]


def _element_overflow(n: dict) -> None:
    _node(n)["unicastAddress"] = "7FFF"
    _node(n)["elements"][-1]["index"] = 1


MALFORMED: list[tuple[str, object, str]] = [
    # (id, mutation of `meshNetwork` (or a replacement document), fragment of the expected message)
    (
        "unicast-int",
        lambda n: _node(n).update(unicastAddress=5),
        "unicastAddress is not a string",
    ),
    (
        "unicast-zero",
        lambda n: _node(n).update(unicastAddress="0000"),
        "unicastAddress is out of range",
    ),
    ("unicast-group", lambda n: _node(n).update(unicastAddress="C000"), "out of range"),
    (
        "unicast-over-16-bit",
        lambda n: _node(n).update(unicastAddress="10000"),
        "out of range",
    ),
    (
        "unicast-fullwidth-digits",
        lambda n: _node(n).update(unicastAddress=FULLWIDTH_0148),
        "hexadecimal",
    ),
    (
        "unicast-arabic-digits",
        lambda n: _node(n).update(unicastAddress=ARABIC_0148),
        "hexadecimal",
    ),
    ("unicast-none", lambda n: _node(n).update(unicastAddress=None), "not a string"),
    ("netkeys-empty", lambda n: n.update(netKeys=[]), "netKeys has no key at index 0"),
    (
        "netkeys-index-1-only",
        lambda n: n["netKeys"][0].update(index=1),
        "no key at index 0",
    ),
    ("netkeys-str", lambda n: n.update(netKeys="x"), "netKeys is not a list"),
    (
        "netkeys-entry-str",
        lambda n: n.update(netKeys=["x"]),
        "netKeys entry is not an object",
    ),
    (
        "netkey-15-bytes",
        lambda n: n["netKeys"][0].update(key="00" * 15),
        "16-byte hexadecimal key",
    ),
    (
        "netkey-17-bytes",
        lambda n: n["netKeys"][0].update(key="00" * 17),
        "16-byte hexadecimal key",
    ),
    (
        "netkey-int",
        lambda n: n["netKeys"][0].update(key=5),
        "netKeys key is not a string",
    ),
    (
        "netkey-non-hex",
        lambda n: n["netKeys"][0].update(key="zz" * 16),
        "16-byte hexadecimal key",
    ),
    (
        "netkey-index-bool",
        lambda n: n["netKeys"][0].update(index=False),
        "netKeys index is not an integer",
    ),
    (
        "netkey-index-huge",
        lambda n: n["netKeys"][0].update(index=4096),
        "netKeys index is out of range",
    ),
    (
        "netkey-phase-3",  # the schema's phases are 0, 1 and 2: a finished refresh is 0 again
        lambda n: n["netKeys"][0].update(phase=3),
        "netKeys phase is out of range",
    ),
    (
        "netkey-phase-str",
        lambda n: n["netKeys"][0].update(phase="1"),
        "netKeys phase is not an integer",
    ),
    (
        "netkey-refresh-without-old-key",
        lambda n: n["netKeys"][0].update(phase=1),
        "netKeys oldKey is not a string",
    ),
    (
        "netkey-refresh-old-key-15-bytes",
        lambda n: n["netKeys"][0].update(phase=2, oldKey="00" * 15),
        "netKeys oldKey is not a 16-byte",
    ),
    (
        "netkey-index-twice",
        lambda n: n["netKeys"].append(dict(n["netKeys"][0])),
        "index 0 appears twice",
    ),
    ("appkeys-empty", lambda n: n.update(appKeys=[]), "appKeys has no key at index 0"),
    (
        "appkeys-index-5",
        lambda n: n["appKeys"][0].update(index=5),
        "appKeys has no key at index 0",
    ),
    (
        "appkey-15-bytes",
        lambda n: n["appKeys"][0].update(key="ab" * 15),
        "16-byte hexadecimal key",
    ),
    (
        "provisioners-str",
        lambda n: n.update(provisioners="x"),
        "provisioners is not a list",
    ),
    (
        "provisioner-range-str",
        lambda n: n["provisioners"][0].update(allocatedUnicastRange="x"),
        "not a list",
    ),
    (
        "provisioner-range-int",
        lambda n: n["provisioners"][0]["allocatedUnicastRange"][0].update(lowAddress=1),
        "lowAddress is not a string",
    ),
    ("uuid-int", lambda n: n.update(meshUUID=7), "meshUUID is not a string"),
    ("uuid-short", lambda n: n.update(meshUUID="x"), "meshUUID is not a UUID"),
    (
        "uuid-path",
        lambda n: n.update(meshUUID="../../../etc/passwd"),
        "meshUUID is not a UUID",
    ),
    (
        "uuid-no-dashes",
        lambda n: n.update(meshUUID="1BAF3ADE00004000800000000000000"),
        "meshUUID is not a UUID",
    ),
    ("uuid-missing", lambda n: n.pop("meshUUID"), "meshUUID is not a string"),
    ("nodes-str", lambda n: n.update(nodes="x"), "nodes is not a list"),
    ("nodes-missing", lambda n: n.pop("nodes"), "nodes is not a list"),
    ("node-int", lambda n: n.update(nodes=[1]), "nodes[0] is not an object"),
    ("node-uuid-int", lambda n: _node(n).update(UUID=1), "UUID is not a string"),
    ("node-name-none", lambda n: _node(n).update(name=None), "name is not a string"),
    (
        "devkey-15-bytes",
        lambda n: _node(n).update(deviceKey="00" * 15),
        "deviceKey is not a 16-byte",
    ),
    ("pid-int", lambda n: _node(n).update(pid=11), "pid is not a string"),
    ("cid-int", lambda n: _node(n).update(cid=0x0527), "cid is not a string"),
    ("cid-5-hex", lambda n: _node(n).update(cid="10527"), "cid is out of range"),
    (
        "excluded-str",
        lambda n: _node(n).update(excluded="no"),
        "excluded is not a boolean",
    ),
    (
        "excluded-int",
        lambda n: _node(n).update(excluded=0),
        "excluded is not a boolean",
    ),
    ("elements-str", lambda n: _node(n).update(elements="x"), "elements is not a list"),
    (
        "element-index-str",
        lambda n: _node(n)["elements"][0].update(index="0"),
        "index is not an integer",
    ),
    (
        "element-index-bool",
        lambda n: _node(n)["elements"][0].update(index=True),
        "index is not an integer",
    ),
    (
        "element-location-int",
        lambda n: _node(n)["elements"][0].update(location=1),
        "location is not a string",
    ),
    ("element-overflow", _element_overflow, "address is out of range"),
    ("duplicate-unicasts", _duplicate_unicast, "address is used twice"),
    (
        "models-none",
        lambda n: _node(n)["elements"][0].update(models=None),
        "models is not a list",
    ),
    (
        "model-id-int",
        lambda n: _first_model(n).update(modelId=4096),
        "modelId is not a string",
    ),
    (
        "model-id-too-long",
        lambda n: _first_model(n).update(modelId="0" * 9),
        "modelId is not a hexadecimal",
    ),
    (
        "subscribe-str",
        lambda n: _first_model(n).update(subscribe="C000"),
        "subscribe is not a list",
    ),
    (
        "subscribe-non-hex",
        lambda n: _first_model(n).update(subscribe=["ZZZZ"]),
        "subscribe entry is not a hexadecimal",
    ),
    (
        "bind-str",
        lambda n: _first_model(n).update(bind=["0"]),
        "bind entry is not an integer",
    ),
    ("groups-missing", lambda n: n.pop("groups"), "groups is not a list"),
    (
        "group-address-int",
        lambda n: n["groups"][0].update(address=0xC000),
        "address is not a string",
    ),
    (
        "group-name-none",
        lambda n: n["groups"][0].update(name=None),
        "name is not a string",
    ),
    (
        "scene-number-non-hex",
        lambda n: n["scenes"][0].update(number="GGGG"),
        "number is not a hexadecimal",
    ),
    (
        "scene-addresses-str",
        lambda n: n["scenes"][0].update(addresses="C000"),
        "addresses is not a list",
    ),
    ("scene-name-int", lambda n: n["scenes"][0].update(name=1), "name is not a string"),
    (
        "exclusion-ivindex-str",
        lambda n: n["networkExclusions"][0].update(ivIndex="3"),
        "ivIndex is not an integer",
    ),
    (
        "exclusion-addresses-int",
        lambda n: n["networkExclusions"][0].update(addresses=[5]),
        "addresses entry is not a string",
    ),
    (
        "exclusions-dict",
        lambda n: n.update(networkExclusions={}),
        "networkExclusions is not a list",
    ),
    ("network-list", [], "meshNetwork is not an object"),
    ("network-str", "x", "meshNetwork is not an object"),
    ("node-uuid-not-a-uuid", lambda n: _node(n).update(UUID="y"), "UUID is not a UUID"),
    (
        "node-uuid-31-hex",
        lambda n: _node(n).update(UUID="0" * 31),
        "UUID is not a UUID",
    ),
    (
        "node-uuid-twice",
        lambda n: _node(n, 1).update(UUID=_node(n, 0)["UUID"].lower().replace("-", "")),
        "nodes[1] UUID is used twice",
    ),
    (
        "group-address-twice",
        lambda n: n["groups"].append(dict(n["groups"][0])),
        "address is used twice",
    ),
    (
        "scene-number-twice",
        lambda n: n["scenes"].append(dict(n["scenes"][0])),
        "number is used twice",
    ),
    (
        "publish-str",
        lambda n: _first_model(n).update(publish="C000"),
        "publish is not an object",
    ),
    (
        "publish-address-int",
        lambda n: _first_model(n).update(publish={"address": 0xC000}),
        "publish address is not a string",
    ),
    (
        "publish-address-label-non-hex",
        lambda n: _first_model(n).update(publish={"address": "Z" * 32}),
        "publish address is not a hexadecimal",
    ),
    (
        "subscribe-label-non-hex",
        lambda n: _first_model(n).update(subscribe=["Z" * 32]),
        "subscribe entry is not a hexadecimal",
    ),
    (
        "subscribe-33-hex",
        lambda n: _first_model(n).update(subscribe=["0" * 33]),
        "subscribe entry is not a hexadecimal",
    ),
    (
        "scene-address-label",  # scene addresses are element addresses: a label makes no sense there
        lambda n: n["scenes"][0].update(addresses=["0" * 32]),
        "addresses entry is not a hexadecimal",
    ),
]


@pytest.mark.parametrize(
    ("case", "mutate", "message"), MALFORMED, ids=[c[0] for c in MALFORMED]
)
def test_malformed_exports_are_refused_with_a_key_free_message(
    case: str, mutate: object, message: str
):
    """Every wrong type or range is `InvalidExport` (a ValueError) from `from_network`, never a bare Python error;
    the message names the field, never the value (the document holds every key)."""
    net = _network()
    if callable(mutate):
        mutate(net)
    else:
        net = mutate
    with pytest.raises(InvalidExport) as info:
        CDB.from_network(net)
    text = str(info.value)
    assert message in text, text
    reference = _network()
    for secret in (
        reference["netKeys"][0]["key"],
        reference["appKeys"][0]["key"],
        *(n["deviceKey"] for n in reference["nodes"]),
        "00" * 15,
        "ab" * 15,
    ):
        assert secret.lower() not in text.lower()


def test_meta_must_be_an_object():
    with pytest.raises(InvalidExport, match="meta is not an object"):
        CDB.from_network(_network(), [])  # type: ignore[arg-type]


def test_parse_refuses_documents_of_the_wrong_shape():
    for doc, message in (
        ("[]", "not a JUNG HOME mesh export"),
        ('{"meshNetwork": []}', "meshNetwork is not an object"),
        ('{"network": "not base64!!"}', "network is not a Base64 CDB"),
        (
            '{"network": "' + base64.b64encode(b"[]").decode() + '"}',
            "network is not an object",
        ),
        (
            '{"network": "' + base64.b64encode(b"\xff\xfe").decode() + '"}',
            "network is not a Base64 CDB",
        ),
        (
            '{"network": "' + base64.b64encode(b"{}").decode() + '", "meta": []}',
            "meta is not an object",
        ),
        (
            '{"network": "' + base64.b64encode(b'{"meshNetwork": 1}').decode() + '"}',
            "meshNetwork is not an object",
        ),
    ):
        with pytest.raises(InvalidExport, match=message):
            CDB.parse(doc)
    assert issubclass(InvalidExport, ValueError)  # what every caller already catches
    with pytest.raises(ValueError, match="Expecting value"):
        CDB.parse("not json")  # a plain decode error stays what it is


def test_lenient_shapes_still_load(tmp_path: Path):
    """What the schema leaves optional or the apps write differently is accepted: short / lower-case hex, missing
    optional lists, an empty `pid`, keys at other indices next to index 0."""
    net = _network()
    node = _node(net, 2)  # the proxy node at 0148
    node["unicastAddress"] = "148"
    node["elements"][0]["location"] = "1"
    node["elements"][0]["models"][0].pop("subscribe", None)
    node["elements"][0]["models"][0].pop("bind", None)
    node["pid"] = ""
    node["deviceKey"] = node["deviceKey"].lower()
    net["netKeys"].append({"index": 1, "key": "11" * 16})
    net["appKeys"].append({"index": 2, "key": "22" * 16})
    net["meshUUID"] = net["meshUUID"].lower()
    for key in ("scenes", "provisioners", "networkExclusions"):
        net.pop(key, None)
    cdb = CDB.from_network(net)
    assert cdb.nodes[2].unicast == 0x0148
    assert cdb.nodes[2].elements[0].location == 1
    assert cdb.nodes[2].pid is None
    assert set(cdb.net_keys) == {0, 1}
    assert set(cdb.app_keys) == {0, 2}
    assert cdb.scenes == {}
    assert cdb.provisioner_unicast_ranges == []
    assert cdb.mesh_uuid == net["meshUUID"]


# ----------------------------------------------------------------------------- virtual addresses (parsed, not routed)

LABEL = "0073E7E4D8B9440FAF8415DF4C56C0E1"  # Mesh Profile §8.1.x sample: virtual address 0xB529
VIRTUAL = 0xB529


def test_virtual_address_matches_the_spec_sample():
    assert virtual_address(bytes.fromhex(LABEL)) == VIRTUAL
    assert is_virtual(VIRTUAL)
    assert not is_virtual(0x7FFF)
    assert not is_virtual(0xC000)
    assert parse_address(LABEL) == VIRTUAL
    assert parse_address("C00F") == 0xC00F
    assert parse_address("148") == 0x148


def test_labels_in_subscribe_publish_and_groups_load_as_virtual_addresses():
    """A third-party provisioner's virtual group does not make the export unimportable: the label is hashed to
    its virtual address (§3.4.2.3), remembered in `virtual_labels`, and the element reports the subscription."""
    net = _network()
    model = _first_model(net)
    model["subscribe"] = [*model.get("subscribe", []), LABEL.lower()]
    model["publish"] = {"address": LABEL, "index": 0, "ttl": 255}
    net["groups"].append({"address": LABEL, "name": "Virtual", "parentAddress": "0000"})
    cdb = CDB.from_network(net)
    assert cdb.virtual_labels == {VIRTUAL: bytes.fromhex(LABEL)}
    assert cdb.groups[VIRTUAL] == "Virtual"
    element = cdb.nodes[0].elements[0]
    assert element.subscriptions(model["modelId"])[-1] == VIRTUAL
    assert cdb.label(VIRTUAL) == "B529 'Virtual'"


def test_publish_without_an_address_or_null_is_tolerated():
    net = _network()
    _first_model(net)["publish"] = {"index": 0}
    _node(net, 1)["elements"][0]["models"][0]["publish"] = None
    assert CDB.from_network(net).virtual_labels == {}


# ----------------------------------------------------------------------------- node UUID canonical form


def test_canonical_uuid():
    dashed = "00005EFF-FE00-5314-0000-000000000000"
    assert canonical_uuid(dashed) == dashed
    assert canonical_uuid(dashed.lower()) == dashed
    assert canonical_uuid(dashed.replace("-", "").lower()) == dashed
    assert canonical_uuid("N") == "N"  # not a UUID at all: compared as given
    assert (
        canonical_uuid("z" * 32) == "Z" * 32
    )  # 32 characters but not hex: no dashes invented


def test_undashed_node_uuids_load_in_the_canonical_form():
    """An export from an older nRF-Mesh library (undashed node UUIDs) keeps its HA ids, MACs and names: `Node.uuid`
    is dashed and upper-case whatever the file wrote; the raw tree is left as loaded."""
    net = _network()
    original = _node(net)["UUID"]
    _node(net)["UUID"] = original.replace("-", "").lower()
    cdb = CDB.from_network(net)
    assert cdb.nodes[0].uuid == original
    assert cdb.raw is not None
    assert cdb.raw["nodes"][0]["UUID"] == original.replace("-", "").lower()


def test_elements_are_ordered_by_index():
    """MOD-12: an element is identified by its `index`, not its position in the file: the list is in index order
    whatever order an exporter wrote it in (callers read `elements[0]` as the primary)."""
    raw = json.loads(CDB_PATH.read_text())["meshNetwork"]
    node = next(n for n in raw["nodes"] if n["unicastAddress"] == "0148")
    node["elements"].reverse()
    cdb = CDB.from_network(raw)
    parsed = cdb.node_by_addr(0x0148)
    assert parsed is not None
    assert [e.address for e in parsed.elements] == [0x148, 0x149, 0x14A]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_a_share_export_payload_in_another_json_encoding_is_accepted(encoding: str):
    """The `network` payload is JSON: a BOM or UTF-16/32 decodes as `json.loads(bytes)` accepted before the parser
    kept the payload's text (MOD-11)."""
    doc = json.loads((FIXTURES / "JungHome.json").read_text())
    inner = base64.b64decode(doc["network"]).decode("utf-8")
    doc["network"] = base64.b64encode(inner.encode(encoding)).decode()
    net, _meta = CDB.parse(json.dumps(doc))
    reference, _ = CDB.parse((FIXTURES / "JungHome.json").read_text())
    assert net == reference


def _nested(depth: int) -> object:
    value: object = 1
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    ("key", "row"),
    [
        ("devices", {"name": 5, "deviceId": {"nodeId": "x", "locationIds": [1]}}),
        ("scenes", {"name": ["WC"], "number": 1}),
        ("userGroups", {"name": {"x": 1}, "address": 0xC00F}),
    ],
)
def test_meta_names_must_be_strings(key: str, row: dict):
    """A number where the app writes a name is not an app export (the apps decode a string): refused at load,
    rather than an int reaching Home Assistant as a device, scene or room name."""
    with pytest.raises(InvalidExport, match=rf"meta {key}\[1\] name is not a string"):
        CDB.from_network(_network(), {key: [{"name": "fine"}, row]})
    # null / absent names and rows that are not objects stay the best-effort metadata loader's business
    CDB.from_network(_network(), {key: [{"name": None}, {}, 3], "other": [{"name": 1}]})
    CDB.from_network(_network(), {key: None})


def test_deeply_nested_exports_are_invalid_not_a_recursion_error():
    """RecursionError is no ValueError: a hostile export nested deeper than the JSON decoder (or any later walk of
    the tree) recurses must still come back as `InvalidExport`."""
    past_the_decoder = "[" * 1_000_000 + "]" * 1_000_000
    share = json.loads((FIXTURES / "JungHome.json").read_text())
    share["meta"]["deep"] = _nested(MAX_DEPTH)
    for text, what in (
        (past_the_decoder, "the export"),
        (json.dumps({"meshNetwork": _nested(MAX_DEPTH)}), "the export"),
        (json.dumps(share), "the export"),
        (
            json.dumps(
                {"network": base64.b64encode(past_the_decoder.encode()).decode()}
            ),
            "network",
        ),
    ):
        with pytest.raises(InvalidExport, match=f"{what} is nested too deeply"):
            on_small_stack(lambda text=text: CDB.parse(text))
    share["meta"]["deep"] = _nested(
        MAX_DEPTH - 3
    )  # the bound counts every level from the document down
    CDB.parse(json.dumps(share))


def test_excluded_nodes_are_kept_apart_and_their_addresses_stay_taken():
    """`excluded: true` marks a node being removed (Mesh CDB schema): no device of it, its addresses not ours."""
    net = _network()
    _node(net, 2)["excluded"] = True
    cdb = CDB.from_network(net)
    [gone] = cdb.excluded_nodes
    assert gone.excluded
    assert gone not in cdb.nodes
    assert not any(n.excluded for n in cdb.nodes)
    address = gone.elements[0].address
    assert address in cdb.used_unicasts()
    assert not cdb.unicast_is_free(address)
    assert cdb.element(address) is None


def test_cid_is_kept_and_a_foreign_company_has_no_product_id():
    net = _network()
    jung, foreign = _node(net, 2), _node(net, 3)
    foreign["cid"] = "004C"  # another maker's node, whatever its pid says
    cdb = CDB.from_network(net)
    assert (cdb.nodes[2].cid, cdb.nodes[2].pid) == (0x0527, int(jung["pid"], 16))
    assert (cdb.nodes[3].cid, cdb.nodes[3].pid) == (0x004C, None)
    del foreign[
        "cid"
    ]  # the schema requires it, but an older file may lack it: read as a JUNG node then
    node = CDB.from_network(net).nodes[3]
    assert (node.cid, node.pid) == (None, int(foreign["pid"], 16))


def test_insert_function_comes_from_the_share_meta():
    """The app caches every node's InsertId in `meta.devices[].deviceId`; a load-side row wins, junk is skipped."""
    net = _network()
    uuid = _node(net, 3)["UUID"]

    def row(locations: list[int], function: object, node: str | None = uuid) -> dict:
        return {
            "name": "x",
            "deviceId": {
                "nodeId": node,
                "locationIds": locations,
                "actuatorFunctionId": function,
            },
        }

    meta = {
        "devices": [
            row([64], 7),
            row([1], 5),
            row([65], 6),
            row([1], True),
            row([1], 0, None),
            row([1], 0, "00000000-0000-0000-0000-000000000000"),
            {"name": "no id"},
            "not a row",
        ]
    }
    cdb = CDB.from_network(net, meta)
    assert cdb.nodes[3].insert_function == 5
    assert cdb.nodes[2].insert_function is None
    only_keys = CDB.from_network(net, {"devices": [row([64], 7)]})
    assert (
        only_keys.nodes[3].insert_function == 7
    )  # the node's insert, whichever of its app devices says it
    assert CDB.from_network(net, {"devices": None}).nodes[3].insert_function is None


def test_button_layout_comes_from_the_android_share_meta():
    """F4-12: the Android app caches each node's ButtonLayout in `meta.buttonLayoutExports` by element address."""
    cdb = CDB.load(FIXTURES / "JungHome-android.json")
    layouts = {n.unicast: n.button_layout for n in cdb.nodes}
    assert (layouts[0x0148], layouts[0x0232], layouts[0x0300]) == (1, 5, 1)
    assert layouts[0x0172] is None  # the socket has none
    net = _network()
    second = _node(net, 3)["elements"][1]["index"] + int(
        _node(net, 3)["unicastAddress"], 16
    )
    meta = {
        "buttonLayoutExports": [
            {"mode": 2, "elementAddress": second},  # any element of the node names it
            {"mode": True, "elementAddress": 0x0148},  # not a number
            {"mode": 0, "elementAddress": "0148"},
            {"mode": 0, "elementAddress": 0x7000},  # no node there
            "not a row",
        ]
    }
    cdb = CDB.from_network(net, meta)
    assert [n.button_layout for n in cdb.nodes] == [
        None,
        None,
        None,
        2,
        None,
        None,
        None,
    ]
    assert (
        CDB.from_network(net, {"buttonLayoutExports": None}).nodes[3].button_layout
        is None
    )
