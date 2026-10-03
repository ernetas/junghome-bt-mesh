"""jhmesh.vault: Home Assistant's provisioner identity, its ranges and the vault of its nodes (review-3 N1)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jhmesh.cdb import CDB, InvalidExport, Provisioner
from jhmesh.export import ProjectFile
from jhmesh.onboarding import free_unicast_block
from jhmesh.provisioning import Capabilities, Method, capability_record
from jhmesh.vault import (
    GROUP_CEILING,
    RangeError,
    Ranges,
    RefreshProgress,
    Vault,
    VaultError,
    choose_ranges,
    recognise,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ANDROID = FIXTURES / "JungHome-android.json"
OUR = 0x0D00
PHONE = "00000001-0000-4000-8000-000000000001"
DIMMER = 0x0300  # 2 elements, element groups C070 / C071, two app device rows
NEW_UUID = "11111111-2222-4333-8444-555555555555"
NEW_KEY = bytes(range(0x40, 0x50))
EXPECTED = Ranges((0x0D00, 0x0DFF), (0xFDF5, 0xFEF4), (0xFF00, 0xFFFF))


def seeded(n: int) -> bytes:
    return bytes(range(n))


def project(path: Path = ANDROID) -> ProjectFile:
    return ProjectFile.load(path)


def add_provisioner(
    pf: ProjectFile,
    uuid: str,
    unicast: tuple[int, int] | None = None,
    group: tuple[int, int] | None = None,
    scene: tuple[int, int] | None = None,
) -> None:
    """Another provisioner (a second app user) with the given ranges; the CDB rebuilt."""
    entry: dict[str, Any] = {"provisionerName": "Second phone", "UUID": uuid}
    if unicast:
        entry["allocatedUnicastRange"] = [
            {"lowAddress": f"{unicast[0]:04X}", "highAddress": f"{unicast[1]:04X}"}
        ]
    if group:
        entry["allocatedGroupRange"] = [
            {"lowAddress": f"{group[0]:04X}", "highAddress": f"{group[1]:04X}"}
        ]
    if scene:
        entry["allocatedSceneRange"] = [
            {"firstScene": f"{scene[0]:04X}", "lastScene": f"{scene[1]:04X}"}
        ]
    pf.net["provisioners"].append(entry)
    pf.cdb = CDB.from_network(pf.net, pf.meta)


def choose(pf: ProjectFile, own: int = OUR, **kwargs: Any) -> Ranges:
    return choose_ranges(pf.cdb, own, pf.used_group_addresses(), **kwargs)


# ----------------------------------------------------------------------------- the CDB's view of provisioners


def test_the_cdb_keeps_each_provisioner_with_its_ranges() -> None:
    cdb = project().cdb
    assert cdb.provisioners == [
        Provisioner(
            PHONE, "iPhone", [(0x0001, 0x0CCC)], [(0xC000, 0xC64B)], [(0x0001, 0x1999)]
        )
    ]
    assert cdb.own_provisioner(None) is None
    assert cdb.own_provisioner(PHONE.lower()) is cdb.provisioners[0]
    assert cdb.foreign_unicast_ranges() == [(0x0001, 0x0CCC)]
    assert cdb.foreign_unicast_ranges(PHONE) == []
    # the phone's own address and range are "ours" when the phone's UUID is named
    assert 0x0001 in cdb.used_unicasts()
    assert 0x0001 not in cdb.used_unicasts(PHONE)
    assert not cdb.unicast_is_free(0x0001)
    assert cdb.unicast_is_free(0x0001, own=PHONE)
    assert not cdb.unicast_is_free(0x0148, own=PHONE)  # another node's all the same


# ----------------------------------------------------------------------------- choosing ranges


def test_ranges_around_our_address_and_at_the_top_of_the_group_and_scene_spaces() -> (
    None
):
    pf = project()
    assert choose(pf) == EXPECTED
    assert not EXPECTED.problems(pf.cdb, OUR, None)
    # the unicast range grows upwards from our address, downwards only where there is no room above
    assert choose(pf, 0x7FF0).unicast == (0x7F00, 0x7FFF)


def test_crowded_spaces_give_shorter_ranges() -> None:
    pf = project()
    add_provisioner(
        pf,
        "22222222-0000-4000-8000-000000000002",
        unicast=(0x0D10, 0x7FFF),
        group=(0xC64C, 0xFE80),
        scene=(0x199A, 0xFFF0),
    )
    ranges = choose(pf)
    assert ranges.unicast == (
        0x0CCD,
        0x0D0F,
    )  # all there is between the two phones' ranges
    assert ranges.group == (0xFE81, GROUP_CEILING)
    assert ranges.scene == (0xFFF1, 0xFFFF)
    assert not ranges.problems(pf.cdb, OUR, None)


def test_the_longest_run_wins_and_the_highest_of_equals() -> None:
    pf = project()
    add_provisioner(
        pf,
        "22222222-0000-4000-8000-000000000002",
        group=(0xC64C, 0xFE00),
        scene=(0x199A, 0xFFF0),
    )
    # two free group runs FE01..FE10 (16) and FE12..FEF4 (227) around a group in use: the longer one
    pf.net["groups"].append(
        {"name": "Stray", "address": "FE11", "parentAddress": "0000"}
    )
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    assert choose(pf).group == (0xFE12, GROUP_CEILING)
    # scenes FFF1..FFF7 and FFF9..FFFF: equal length, the higher one
    pf.net["scenes"].append({"name": "Stray", "number": "FFF8", "addresses": []})
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    assert choose(pf).scene == (0xFFF9, 0xFFFF)


@pytest.mark.parametrize(
    ("kind", "space"),
    [("group", (0xC000, 0xFEFF)), ("scene", (0x0001, 0xFFFF))],
)
def test_a_full_space_is_a_range_error(kind: str, space: tuple[int, int]) -> None:
    pf = project()
    add_provisioner(pf, "22222222-0000-4000-8000-000000000002", **{kind: space})
    with pytest.raises(RangeError, match=kind):
        choose(pf)


@pytest.mark.parametrize(
    "address", [0x0100, 0x8000, 0x0000]
)  # the phone's range, not unicast
def test_our_address_must_be_free(address: int) -> None:
    with pytest.raises(RangeError, match=f"{address:04X}"):
        choose(project(), address)


def test_our_address_taken_by_a_node_or_excluded() -> None:
    pf = project()
    pf.cdb.nodes[
        1
    ].unicast = OUR  # a node sits at our address now (its elements move with it)
    pf.cdb.nodes[1].elements[0].address = OUR
    with pytest.raises(RangeError):
        choose(pf)
    pf = project()
    pf.cdb.excluded_addresses.add(OUR)
    with pytest.raises(RangeError):
        choose(pf)


def test_a_stored_range_is_kept_while_it_is_valid() -> None:
    pf = project()
    stored = Ranges((0x0D00, 0x0D1F), (0xFE00, 0xFE0F), (0xF000, 0xF00F))
    # our own nodes and groups inside it do not invalidate it
    pf.net["groups"].append(
        {"name": "Ours", "address": "FE00", "parentAddress": "0000"}
    )
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    assert choose(pf, preferred=stored) == stored
    # another provisioner reaching into it does: a fresh choice clear of it
    add_provisioner(pf, "22222222-0000-4000-8000-000000000002", group=(0xFE08, 0xFE20))
    assert stored.problems(pf.cdb, OUR, None) == [
        "the group range overlaps provisioner 'Second phone'"
    ]
    # FE21..FEF4 is shorter than a whole range: the highest whole one below (FE00 is ours, but in use)
    assert choose(pf, preferred=stored).group == (0xFD00, 0xFDFF)
    # our address moved out of it
    assert choose(pf, 0x0E00, preferred=stored).unicast == (0x0E00, 0x0EFF)


def test_problems_of_ranges_outside_their_spaces() -> None:
    cdb = project().cdb
    bad = Ranges((0x0C00, 0x8000), (0xFE00, 0xFEFF), (0x0000, 0x0010))
    assert bad.problems(cdb, 0x0B00, None) == [
        "the unicast range is outside its space",
        "the group range is outside its space",
        "the scene range is outside its space",
        "0B00 is outside the unicast range",
        "the unicast range overlaps provisioner 'iPhone'",
        "the scene range overlaps provisioner 'iPhone'",
    ]
    # our own entry (the phone's here) is not in our way
    assert "overlaps" not in " ".join(bad.problems(cdb, 0x0D00, PHONE))


# ----------------------------------------------------------------------------- persistence


def test_create_and_round_trip() -> None:
    vault = Vault.create(seeded)
    assert vault.uuid == "00010203-0405-4607-8809-0A0B0C0D0E0F"
    assert vault.node_key == seeded(16)
    vault.ranges = EXPECTED
    vault.remember_provisioned(NEW_UUID.lower(), 0x0D10, 2, NEW_KEY)
    data = json.loads(json.dumps(vault.to_dict()))
    assert data["ranges"] == {
        "unicast": ["0D00", "0DFF"],
        "group": ["FDF5", "FEF4"],
        "scene": ["FF00", "FFFF"],
    }
    back = Vault.from_dict(data)
    assert back == vault
    assert [n.uuid for n in back.pending] == [NEW_UUID]
    fresh = Vault.from_dict(
        {k: v for k, v in Vault.create().to_dict().items() if k != "nodes"}
    )
    assert fresh.ranges is None
    assert fresh.nodes == {}


def test_no_key_in_a_repr() -> None:
    vault = Vault.create(seeded)
    vault.remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    text = repr(vault)
    for key in (vault.node_key, NEW_KEY):
        assert key.hex() not in text.lower()


def valid() -> dict[str, Any]:
    vault = Vault.create(seeded)
    vault.ranges = EXPECTED
    vault.remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    vault.nodes[NEW_UUID].groups = [(0xFDF5, "element group #0xD10")]
    return vault.to_dict()


def edited(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    data = valid()
    target: Any = data
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "not an object"),
        (edited(("version",), 2), "unknown version"),
        (edited(("name",), 5), "name is not a string"),
        (edited(("uuid",), "nope"), "uuid is not a UUID"),
        (edited(("nodeKey",), None), "nodeKey is not a 16-byte"),
        (edited(("nodeKey",), "zz" * 16), "nodeKey is not a 16-byte"),
        (edited(("ranges",), []), "ranges is not an object"),
        (
            edited(("ranges", "unicast"), ["0D00"]),
            r"ranges unicast is not a \(low, high\)",
        ),
        (
            edited(("ranges", "group"), ["C000", None]),
            "ranges group is not a hexadecimal",
        ),
        (
            edited(("ranges", "group"), ["C000", "xyz"]),
            "ranges group is not a hexadecimal",
        ),
        (edited(("ranges", "scene"), ["0000", "0010"]), "ranges scene is out of range"),
        (edited(("ranges", "unicast"), ["0D10", "0D00"]), "ranges unicast is empty"),
        (edited(("nodes",), {}), "nodes is not a list"),
        (edited(("nodes", 0), 1), r"nodes\[0\] is not an object"),
        (edited(("nodes", 0, "elements"), True), "elements is not a positive"),
        (edited(("nodes", 0, "elements"), 0), "elements is not a positive"),
        (edited(("nodes", 0, "entry"), []), "entry is not an object"),
        (edited(("nodes", 0, "groups"), [{"address": "C000"}]), "groups is not a list"),
        (edited(("nodes", 0, "groups"), {}), "groups is not a list"),
        (edited(("nodes", 0, "devices"), [1]), "devices is not a list"),
        (edited(("nodes", 0, "devices"), {}), "devices is not a list"),
        (edited(("nodes", 0, "deviceKey"), "00"), r"nodes\[0\] deviceKey"),
        (edited(("nodes", 0, "keyRefresh"), []), r"nodes\[0\] keyRefresh is not an"),
        (
            edited(("nodes", 0, "capabilities"), []),
            r"nodes\[0\] capabilities is not an object",
        ),
        (
            edited(("nodes", 0, "keyRefresh"), {"networkId": "00", "phase": 1}),
            "keyRefresh networkId is not an 8-byte",
        ),
        (
            edited(("nodes", 0, "keyRefresh"), {"networkId": "zz" * 8, "phase": 1}),
            "keyRefresh networkId is not an 8-byte",
        ),
        (
            edited(("nodes", 0, "keyRefresh"), {"networkId": 5, "phase": 1}),
            "keyRefresh networkId is not an 8-byte",
        ),
        (
            edited(("nodes", 0, "keyRefresh"), {"networkId": "00" * 8, "phase": 4}),
            "keyRefresh phase is not 0, 1, 2 or 3",
        ),
        (
            edited(("nodes", 0, "keyRefresh"), {"networkId": "00" * 8, "phase": True}),
            "keyRefresh phase is not 0, 1, 2 or 3",
        ),
    ],
)
def test_a_vault_that_does_not_read_back(data: Any, message: str) -> None:
    with pytest.raises(VaultError, match=message) as err:
        Vault.from_dict(data)
    assert NEW_KEY.hex() not in str(err.value).lower()


# ----------------------------------------------------------------------------- merging into a file


def test_merge_appends_our_provisioner_and_node_and_is_idempotent() -> None:
    pf = project()
    vault = Vault.create(seeded)
    result = vault.merge_into(pf, OUR)
    assert result.changed
    assert not result.skipped
    assert not result.stale
    assert vault.ranges == EXPECTED
    provisioners = pf.net["provisioners"]
    assert [p["provisionerName"] for p in provisioners] == ["iPhone", "Home Assistant"]
    assert provisioners[-1] == {
        "provisionerName": "Home Assistant",
        "UUID": vault.uuid,
        "allocatedUnicastRange": [{"lowAddress": "0D00", "highAddress": "0DFF"}],
        "allocatedGroupRange": [{"lowAddress": "FDF5", "highAddress": "FEF4"}],
        "allocatedSceneRange": [{"firstScene": "FF00", "lastScene": "FFFF"}],
    }
    node = pf.net["nodes"][-1]
    assert node["UUID"] == vault.uuid
    assert node["unicastAddress"] == "0D00"
    assert node["deviceKey"] == vault.node_key.hex().upper()
    assert "cid" not in node  # not the phone's company
    assert list(node)[:4] == [
        "UUID",
        "name",
        "unicastAddress",
        "deviceKey",
    ]  # the phone's key order
    # the file loads back, and our address is ours in it: in use by our node only, inside our range only
    back = ProjectFile.loads(pf.render().encode())
    assert back.cdb.own_provisioner(vault.uuid) is not None
    assert OUR in back.cdb.used_unicasts()
    assert OUR not in back.cdb.used_unicasts(vault.uuid)
    assert back.cdb.unicast_is_free(OUR, own=vault.uuid)
    assert not back.cdb.unicast_is_free(OUR)
    # rooms and scenes are allocated in our ranges now
    assert pf.own_provisioner == vault.uuid
    assert pf.free_group_address() == 0xFDF5
    assert pf.free_scene_number() == 0xFF00
    before = copy.deepcopy(pf.net)
    again = vault.merge_into(pf, OUR)
    assert not again.changed
    assert pf.net == before
    # the same file, merged by a vault read back from storage
    assert not Vault.from_dict(vault.to_dict()).merge_into(back, OUR).changed


def test_merge_follows_a_new_address_and_new_ranges() -> None:
    pf = project()
    vault = Vault.create(seeded)
    vault.merge_into(pf, OUR)
    result = vault.merge_into(
        pf, 0x0E00
    )  # the entry was reconfigured to another address
    assert result.changed
    assert pf.net["nodes"][-1]["unicastAddress"] == "0E00"
    assert pf.net["provisioners"][-1]["allocatedUnicastRange"] == [
        {"lowAddress": "0E00", "highAddress": "0EFF"}
    ]


def test_no_provisioner_of_the_apps_means_no_merge() -> None:
    """Ours would be the first entry, which the iOS library takes for its own: refused, the file untouched."""
    pf = project()
    pf.net["provisioners"] = []
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    before = copy.deepcopy(pf.net)
    with pytest.raises(RangeError, match="would come first"):
        Vault.create(seeded).merge_into(pf, OUR)
    assert pf.net == before


def test_merge_into_a_file_without_nodes() -> None:
    pf = project()
    pf.net["nodes"] = []
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    vault = Vault.create(seeded)
    assert vault.merge_into(pf, OUR).changed
    assert pf.net["provisioners"][-1]["UUID"] == vault.uuid
    assert pf.net["nodes"] == [vault.node_entry(OUR)]


def test_our_ranges_are_compared_by_value() -> None:
    """Another writer's hex case or width is no change: nothing is rewritten, the file does not churn."""
    pf = project()
    vault = Vault.create(seeded)
    vault.merge_into(pf, OUR)
    ours = pf.net["provisioners"][-1]
    ours["allocatedUnicastRange"] = [{"lowAddress": "0d00", "highAddress": "00dff"}]
    ours["allocatedSceneRange"] = [{"firstScene": "ff00", "lastScene": "ffff"}]
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    before = copy.deepcopy(pf.net)
    assert not vault.merge_into(pf, OUR).changed
    assert pf.net == before
    # a row that is not a range at all is rewritten
    ours["allocatedGroupRange"] = [{"lowAddress": None}]
    assert vault.merge_into(pf, OUR).changed
    assert ours["allocatedGroupRange"] == [
        {"lowAddress": "FDF5", "highAddress": "FEF4"}
    ]
    ours["allocatedGroupRange"] = "FDF5"
    assert vault.merge_into(pf, OUR).changed


def test_uuids_follow_an_undashed_file() -> None:
    vault = Vault.create(seeded)
    vault.ranges = EXPECTED
    undashed = {"UUID": PHONE.replace("-", "")}
    assert vault.node_entry(OUR, undashed)["UUID"] == vault.uuid.replace("-", "")
    assert vault.provisioner_entry(undashed)["UUID"] == vault.uuid.replace("-", "")
    assert vault.node_entry(OUR, {"UUID": 5})["UUID"] == vault.uuid


def test_a_range_error_leaves_the_file_alone() -> None:
    pf = project()
    before = copy.deepcopy(pf.net)
    with pytest.raises(RangeError):
        Vault.create(seeded).merge_into(pf, 0x0148)
    assert pf.net == before
    assert pf.own_provisioner is None


def new_node(pf: ProjectFile, unicast: int = 0x0D10) -> tuple[Vault, dict[str, Any]]:
    """A vault that recorded a copy of the dimmer node as one Home Assistant provisioned at `unicast`.

    The copy is added to `pf`, recorded, and taken out again: `pf` is then a file (the app's next upload) that
    lacks it. Returns the vault and the node's entry as recorded.
    """
    original = copy.deepcopy((pf.net, pf.meta))
    template = next(
        n for n in pf.net["nodes"] if n["unicastAddress"] == f"{DIMMER:04X}"
    )
    entry = copy.deepcopy(template)
    entry["UUID"] = NEW_UUID
    entry["unicastAddress"] = f"{unicast:04X}"
    entry["deviceKey"] = NEW_KEY.hex().upper()
    entry["name"] = "Hall light"
    groups = [(0xFDF5 + i, f"element group #0x{unicast + i:X}") for i in range(2)]
    pf.add_node_entry(entry, groups)
    pf.meta["devices"].append(
        {"name": "Hall light", "deviceId": {"nodeId": NEW_UUID, "locationIds": [1]}}
    )
    vault = Vault.create(seeded)
    vault.remember_provisioned(NEW_UUID, unicast, 2, NEW_KEY)
    assert vault.pending
    kept = vault.remember_recorded(pf, NEW_UUID.lower())
    assert kept.recorded
    assert not vault.pending
    assert kept.groups == groups
    assert [d["name"] for d in kept.devices] == ["Hall light"]
    pf.net, pf.meta = original
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    return vault, entry


def test_merge_puts_back_a_node_the_file_lacks() -> None:
    pf = project()
    vault, entry = new_node(pf)
    result = vault.merge_into(pf, OUR)
    assert result.changed
    node = pf.cdb.node_by_addr(0x0D10)
    assert node is not None
    assert node.dev_key == NEW_KEY
    assert next(n for n in pf.net["nodes"] if n["UUID"] == NEW_UUID) == entry
    assert pf.cdb.groups[0xFDF5] == "element group #0xD10"
    assert {0xFDF5, 0xFDF6} <= {
        r["groupAddress"] for r in pf.meta["elementConnectionGroups"]
    }
    assert any(d["name"] == "Hall light" for d in pf.meta["devices"])
    assert not vault.merge_into(pf, OUR).changed  # present and the same: nothing to do


def test_merge_creates_a_missing_device_list() -> None:
    pf = project()
    vault, _entry = new_node(pf)
    pf.meta["devices"] = None
    assert vault.merge_into(pf, OUR).changed
    assert [d["name"] for d in pf.meta["devices"]] == ["Hall light"]


def test_a_node_whose_addresses_or_groups_are_taken_is_skipped() -> None:
    pf = project()
    vault, _entry = new_node(pf, 0x0D10)
    pf.net["networkExclusions"].append({"ivIndex": 0, "addresses": ["0D11"]})
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    result = vault.merge_into(pf, OUR)
    assert result.skipped == {NEW_UUID: "its addresses are in use"}
    assert pf.cdb.node_by_addr(0x0D10) is None
    pf = project()
    vault, _entry = new_node(pf, 0x0D10)
    pf.add_group("Someone's room", address=0xFDF6)
    result = vault.merge_into(pf, OUR)
    assert result.skipped == {NEW_UUID: "one of its element groups is in use"}


@pytest.mark.parametrize(
    "change",
    [
        {"excluded": True},
        {"unicastAddress": "0D40"},
        {"deviceKey": "00" * 16},
    ],
)
def test_a_node_the_file_has_in_another_shape_is_stale(change: dict[str, Any]) -> None:
    pf = project()
    vault, entry = new_node(pf)
    pf.net["nodes"].append({**copy.deepcopy(entry), **change})
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    result = vault.merge_into(pf, OUR)
    assert result.stale == [NEW_UUID]
    assert not result.skipped


def test_an_entry_that_does_not_load_restores_the_file() -> None:
    pf = project()
    vault, _entry = new_node(pf)
    vault.nodes[NEW_UUID].entry["deviceKey"] = "nope"  # type: ignore[index]
    before = copy.deepcopy((pf.net, pf.meta))
    with pytest.raises(InvalidExport):
        vault.merge_into(pf, OUR)
    assert (pf.net, pf.meta) == before
    assert pf.cdb.own_provisioner(vault.uuid) is None


def test_remember_and_forget() -> None:
    pf = project()
    vault = Vault.create(seeded)
    with pytest.raises(KeyError):
        vault.remember_recorded(pf, NEW_UUID)
    vault.remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    assert vault.nodes[NEW_UUID].addresses() == range(0x0D10, 0x0D12)
    assert vault.forget(NEW_UUID.lower())
    assert not vault.forget(NEW_UUID)


# ----------------------------------------------------------------------------- allocating in our ranges


def test_project_file_allocates_in_its_own_provisioners_ranges_only_when_told() -> None:
    pf = project()
    assert pf.free_group_address() == 0xC002  # the app's range
    pf.own_provisioner = NEW_UUID  # not in the file: the app's range still
    assert pf.free_group_address() == 0xC002
    add_provisioner(
        pf, NEW_UUID, unicast=(0x0D00, 0x0DFF)
    )  # in the file, but no group or scene range
    assert pf.free_group_address() == 0xC002
    assert pf.free_scene_number() == 3
    pf.net["provisioners"][-1]["allocatedGroupRange"] = [
        {"lowAddress": "FE00", "highAddress": "FE0F"}
    ]
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    assert pf.free_group_address() == 0xFE00
    # Home Assistant's "top" allocation is for the app's range; in its own nobody else allocates (review-4 W4-2)
    pf.allocation = "top"
    assert pf.free_group_address() == 0xFE00
    assert (
        pf.free_scene_number() == 0x1999
    )  # no scene range of its own: the top of the app's


def test_new_nodes_go_into_our_unicast_range() -> None:
    pf = project()
    vault = Vault.create(seeded)
    vault.merge_into(pf, OUR)
    assert free_unicast_block(pf.cdb, 2) == 0x7FFE  # the default: above every range
    ours = {"avoid": [OUR], "within": EXPECTED.unicast, "own": vault.uuid}
    assert free_unicast_block(pf.cdb, 2, **ours) == 0x0DFE
    assert free_unicast_block(pf.cdb, 0x100, **ours) is None
    # without naming our entry, our own range blocks like any other provisioner's
    assert free_unicast_block(pf.cdb, 2, avoid=[OUR], within=EXPECTED.unicast) is None
    # another provisioner whose range equals ours (a file that says so) still blocks: dropped by entry, not value
    add_provisioner(
        pf, "22222222-0000-4000-8000-000000000002", unicast=EXPECTED.unicast
    )
    assert free_unicast_block(pf.cdb, 2, **ours) is None
    assert pf.cdb.foreign_unicast_ranges(vault.uuid) == [
        (0x0001, 0x0CCC),
        EXPECTED.unicast,
    ]
    # a range another provisioner covers is refused even inside ours (a file that says so)
    assert free_unicast_block(pf.cdb, 1, within=(0x0CC0, 0x0CCC)) is None  # the phone's


class Boom(Exception):
    """Anything a merge may raise besides `InvalidExport`."""


def test_any_failure_restores_the_file() -> None:
    """A malformed stored entry (here one marked excluded, which the file then does not list as a node) fails
    past the validation: the file is put back as it was all the same, never half merged."""
    pf = project()
    vault, _entry = new_node(pf)
    vault.nodes[NEW_UUID].entry["excluded"] = True  # type: ignore[index]
    before = copy.deepcopy((pf.net, pf.meta))
    with pytest.raises(AssertionError):
        vault.merge_into(pf, OUR)
    assert (pf.net, pf.meta) == before
    assert pf.cdb.own_provisioner(vault.uuid) is None
    assert pf.own_provisioner is None


# ----------------------------------------------------------------------------- a lost vault


def merged(own: int = OUR) -> tuple[ProjectFile, Vault]:
    pf = project()
    vault = Vault.create(seeded)
    vault.merge_into(pf, own)
    return ProjectFile.loads(pf.render().encode()), vault


def test_recognise_takes_back_what_a_merge_wrote() -> None:
    pf, vault = merged()
    found = recognise(pf.cdb, OUR)
    assert found is not None
    assert (found.uuid, found.node_key, found.name, found.ranges) == (
        vault.uuid,
        vault.node_key,
        vault.name,
        EXPECTED,
    )
    assert found.nodes == {}
    assert recognise(pf.cdb, 0x0D01) is None  # not at our address
    assert recognise(project().cdb, OUR) is None  # never merged


def test_recognise_wants_exactly_one_entry_of_the_right_shape() -> None:
    pf, vault = merged()
    node = next(n for n in pf.net["nodes"] if n["UUID"] == vault.uuid)
    node["elements"][0]["models"].append(
        {"modelId": "1000", "bind": [], "subscribe": []}
    )
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    assert (
        recognise(pf.cdb, OUR) is None
    )  # a node with a server model is a device, not us
    pf, vault = merged()
    next(n for n in pf.net["nodes"] if n["UUID"] == vault.uuid)["pid"] = "0003"
    pf.net["nodes"][-1]["cid"] = "0527"
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    assert recognise(pf.cdb, OUR) is None  # a JUNG product
    pf, vault = merged()
    pf.net["provisioners"][-1]["provisionerName"] = "Someone"
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    assert recognise(pf.cdb, OUR) is None
    pf, vault = merged()
    pf.net["nodes"] = [n for n in pf.net["nodes"] if n["UUID"] != vault.uuid]
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    assert recognise(pf.cdb, OUR) is None  # an entry without its node


def test_recognised_ranges_that_no_longer_fit_are_chosen_anew() -> None:
    pf, vault = merged()
    add_provisioner(pf, "22222222-0000-4000-8000-000000000002", group=(0xFE00, 0xFE10))
    found = recognise(pf.cdb, OUR)
    assert found is not None
    assert found.uuid == vault.uuid
    assert found.ranges is None
    del pf.net["provisioners"][-2]["allocatedSceneRange"]  # ours, without a scene range
    pf.cdb = CDB.from_network(pf.net, pf.meta)
    found = recognise(pf.cdb, OUR)
    assert found is not None
    assert found.ranges is None


def test_adopt_identity_keeps_the_nodes() -> None:
    pf, old = merged()
    fresh = Vault.create()
    fresh.remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    fresh.adopt_identity(old)
    assert (fresh.uuid, fresh.node_key, fresh.ranges) == (
        old.uuid,
        old.node_key,
        old.ranges,
    )
    assert list(fresh.nodes) == [NEW_UUID]
    assert not fresh.merge_into(pf, OUR).stale


# ----------------------------------------------------------------------------- pending nodes (review-4 D2)


def test_planned_groups_are_kept_and_reserved() -> None:
    vault = Vault.create(seeded)
    vault.remember_provisioned(
        NEW_UUID, 0x0D10, 2, NEW_KEY, [(0xFDF5, "element group #0xD10")]
    )
    back = Vault.from_dict(json.loads(json.dumps(vault.to_dict())))
    assert back == vault
    kept = back.nodes[NEW_UUID]
    assert kept.groups == [(0xFDF5, "element group #0xD10")]
    assert kept.groups_known
    assert back.reserved_unicasts() == {0x0D10, 0x0D11}
    assert back.reserved_groups() == {0xFDF5}
    assert back.groups_unknown == []


def test_a_pending_node_of_an_older_vault_has_unknown_groups() -> None:
    """A vault from before planned groups were kept wrote `groups: []` for a pending node: that is "unknown", not
    "none"; a recorded node's groups came from the file and are known either way."""
    pf = project()
    vault = Vault.create(seeded)
    vault.remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    dimmer = pf.cdb.node_by_addr(DIMMER)
    assert dimmer is not None
    vault.remember_recorded(pf, dimmer.uuid)
    data = vault.to_dict()
    for row in data["nodes"]:
        del row["groupsKnown"]
    back = Vault.from_dict(data)
    assert [n.uuid for n in back.groups_unknown] == [NEW_UUID]
    assert back.nodes[dimmer.uuid].groups_known
    # the recorded node's groups are reserved; the pending one's addresses still are
    assert back.reserved_groups() == {0xC070, 0xC071}
    assert {0x0D10, 0x0D11, DIMMER, DIMMER + 1} <= back.reserved_unicasts()


def test_a_groups_known_flag_that_is_not_a_boolean() -> None:
    data = valid()
    data["nodes"][0]["groupsKnown"] = "yes"
    with pytest.raises(VaultError, match=r"nodes\[0\] groupsKnown is not a boolean"):
        Vault.from_dict(data)


def test_free_unicast_block_never_hands_out_a_vault_nodes_block() -> None:
    """The reviewers' repro, library half: a node provisioned at the top block and never recorded is in no file,
    so without the vault the next node would get the very same block."""
    pf = project()
    first = free_unicast_block(pf.cdb, 2, avoid=[OUR])
    assert first is not None
    vault = Vault.create(seeded)
    vault.remember_provisioned(NEW_UUID, first, 2, NEW_KEY)
    second = free_unicast_block(pf.cdb, 2, avoid=[OUR, *vault.reserved_unicasts()])
    assert second is not None
    assert not set(range(second, second + 2)) & set(range(first, first + 2))


@given(
    pending=st.lists(
        st.tuples(st.integers(0x7FC0, 0x7FFC), st.integers(1, 3)),
        max_size=12,
    ),
    count=st.integers(1, 4),
    within=st.booleans(),
)
def test_no_block_overlaps_any_pending_node(
    pending: list[tuple[int, int]], count: int, within: bool
) -> None:
    """Whatever pending nodes the vault holds near the top of the space (where blocks are taken from), the block
    handed out next shares no address with any of them."""
    pf = project()
    vault = Vault.create(seeded)
    for i, (unicast, elements) in enumerate(pending):
        vault.remember_provisioned(
            f"11111111-2222-4333-8444-{i:012X}", unicast, elements, NEW_KEY
        )
    block = free_unicast_block(
        pf.cdb,
        count,
        avoid=[OUR, *vault.reserved_unicasts()],
        within=(0x7FB0, 0x7FFF) if within else None,
    )
    assert block is not None
    taken = {a for n in vault.nodes.values() for a in n.addresses()}
    assert not set(range(block, block + count)) & taken


def test_key_refresh_progress_is_kept_and_survives_the_recording() -> None:
    """Review-4 D11: how far a node came through a key refresh — named by the new key's Network ID, never the key —
    round-trips, is left out while there is none (the vault keeps its shape), and stays with the node when it is
    recorded."""
    pf = project()
    dimmer = pf.cdb.node_by_addr(DIMMER)
    assert dimmer is not None
    vault = Vault.create(seeded)
    assert "keyRefresh" not in json.dumps(vault.to_dict())
    progress = RefreshProgress(bytes(range(8)), 2)
    vault.remember_provisioned(dimmer.uuid, DIMMER, 2, dimmer.dev_key, (), progress)
    data = json.loads(json.dumps(vault.to_dict()))
    assert data["nodes"][0]["keyRefresh"] == {
        "networkId": "0001020304050607",
        "phase": 2,
    }
    back = Vault.from_dict(data)
    assert back == vault
    assert back.nodes[dimmer.uuid].key_refresh == progress
    assert back.remember_recorded(pf, dimmer.uuid).key_refresh == progress
    assert vault.remember_recorded(pf, dimmer.uuid).key_refresh == progress
    fresh = Vault.create(seeded)
    assert fresh.remember_recorded(pf, dimmer.uuid).key_refresh is None


def test_the_provisioning_capabilities_are_kept_and_survive_the_recording() -> None:
    """Review-4 P4-8: what the device offered and which method provisioned it round-trips with the node (names and
    flags only), is left out while there is none (an older vault keeps its shape), and stays once recorded."""
    pf = project()
    dimmer = pf.cdb.node_by_addr(DIMMER)
    assert dimmer is not None
    offered = Capabilities(2, 3, 0, 1, 0, 0, 0, 0)
    record = capability_record(offered, Method(1, 1))
    vault = Vault.create(seeded)
    vault.remember_provisioned(dimmer.uuid, DIMMER, 2, dimmer.dev_key)
    assert "capabilities" not in json.dumps(vault.to_dict())
    vault.remember_provisioned(
        dimmer.uuid, DIMMER, 2, dimmer.dev_key, capabilities=record
    )
    data = json.loads(json.dumps(vault.to_dict()))
    assert data["nodes"][0]["capabilities"]["used"] == {
        "algorithm": "BTM_ECDH_P256_HMAC_SHA256_AES_CCM",
        "authentication": "Static OOB",
    }
    assert dimmer.dev_key.hex() not in json.dumps(data["nodes"][0]["capabilities"])
    back = Vault.from_dict(data)
    assert back == vault
    assert back.remember_recorded(pf, dimmer.uuid).capabilities == record
    assert Vault.create(seeded).remember_recorded(pf, dimmer.uuid).capabilities is None


def test_a_vault_node_as_the_client_addresses_it() -> None:
    vault = Vault.create(seeded)
    kept = vault.remember_provisioned(NEW_UUID, 0x0D10, 2, NEW_KEY)
    node = kept.as_node()
    assert (node.uuid, node.unicast, node.dev_key, node.pid) == (
        NEW_UUID,
        0x0D10,
        NEW_KEY,
        None,
    )  # no product: never taken for a JUNG device's evidence of a key refresh
    assert [e.address for e in node.elements] == [0x0D10, 0x0D11]
    assert all(e.node is node for e in node.elements)
