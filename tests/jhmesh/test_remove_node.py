"""ProjectFile.remove_node: a node taken out of the network's file as the app does (review-3 N4)."""

from __future__ import annotations

from pathlib import Path

from jhmesh.export import ProjectFile

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_a_removed_node_leaves_no_wiring_behind() -> None:
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    node = pf.cdb.node_by_addr(0x0148)
    assert node is not None
    own = {e.address for e in node.elements}
    # something of another node points at it: a publication to its element, a scene stored on it
    other = pf.cdb.element(0x0301)
    assert other is not None
    pf.set_publication(other.node, other, "1001", 0x0148)
    assert pf.cdb.scenes[1] == [0x0148]
    pf.meta["keyModeSceneConfigExports"] = [
        {"elementAddress": 0x0149, "scene": 1},
        {"elementAddress": 0x0301},
    ]
    changes = pf.remove_node(node, iv_index=7)
    assert all(
        c.element not in own for c in changes
    )  # nothing for the reset node itself
    assert pf.publication(0x0301, "1001") is None
    assert not any(
        n in pf.cdb.groups for n in (0xC061, 0xC062)
    )  # its element groups are gone
    assert pf.cdb.scenes[1] == []
    assert pf.meta["keyModeSceneConfigExports"] == [{"elementAddress": 0x0301}]
    assert not [
        d
        for d in pf.meta["devices"]
        if isinstance(d.get("deviceId"), dict)
        and d["deviceId"]["nodeId"].upper().replace("-", "")
        == node.uuid.replace("-", "")
    ]
    entry = next(n for n in pf.net["nodes"] if n["unicastAddress"] == "0148")
    assert entry["excluded"] is True
    assert own <= pf.cdb.excluded_addresses
    assert pf.cdb.node_by_addr(0x0148) is None  # no device of it any more
    assert {"ivIndex": 7, "addresses": ["0148", "0149", "014A"]} in pf.net[
        "networkExclusions"
    ]
    # a second removal under the same IV index joins the same exclusions row
    second = pf.cdb.node_by_addr(0x0300)
    assert second is not None
    pf.remove_node(second, iv_index=7)
    rows = [x for x in pf.net["networkExclusions"] if x["ivIndex"] == 7]
    assert len(rows) == 1
    assert "0300" in rows[0]["addresses"]


def test_room_links_to_it_go_and_a_file_without_key_scene_rows_is_fine() -> None:
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    pf.meta.pop("keyModeSceneConfigExports", None)
    holder = next(d for d in pf.meta["devices"] if isinstance(d.get("deviceId"), dict))
    holder["cachedGroupConnectionMetadata"] = [
        {"elementAddress": 0x0301, "groupAddress": 0xC010},
        {"elementAddress": 0x0235, "groupAddress": 0xC010},
    ]
    pf.meta["devices"].append({"name": "no links", "deviceId": {"nodeId": "x"}})
    node = pf.cdb.node_by_addr(0x0300)
    assert node is not None
    pf.remove_node(node, iv_index=0)
    assert holder["cachedGroupConnectionMetadata"] == [
        {"elementAddress": 0x0235, "groupAddress": 0xC010}
    ]
    assert "keyModeSceneConfigExports" not in pf.meta


def test_exclude_node_records_the_reset_node_and_leaves_the_others_wiring() -> None:
    """The record of a removal whose unwiring stopped: the node is out (scenes, rows, `excluded`, exclusions), what
    the others still hold stays in the file — a group some node still listens to is not free for a new one."""
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    node = pf.cdb.node_by_addr(0x0148)
    assert node is not None
    other = pf.cdb.element(0x0301)
    assert other is not None
    pf.set_publication(other.node, other, "1001", 0x0148)
    groups = {a for a in (0xC061, 0xC062) if a in pf.cdb.groups}
    assert groups
    pf.exclude_node(node, iv_index=7)
    assert pf.cdb.node_by_addr(0x0148) is None
    assert {0x0148, 0x0149, 0x014A} <= pf.cdb.excluded_addresses
    assert {"ivIndex": 7, "addresses": ["0148", "0149", "014A"]} in pf.net[
        "networkExclusions"
    ]
    assert pf.cdb.scenes[1] == []
    # not unwired: that takes a message to node 0300
    assert pf.publication(0x0301, "1001") == 0x0148
    assert groups <= set(pf.cdb.groups)
    assert groups <= pf.used_group_addresses()


def test_an_element_group_known_only_from_meta_goes_too() -> None:
    """Review-4 W4-14: `remove_node` matched element groups by name only, while the rest of the integration
    (`cdb_element_groups`) also takes the app's `elementConnectionGroups` rows: a group named otherwise stayed,
    still listened to by whoever the node's element group was wired to."""
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    node = pf.cdb.node_by_addr(0x0148)
    assert node is not None
    entry = next(g for g in pf.net["groups"] if g["address"] == "C061")
    entry["name"] = "Something else"
    pf.cdb.groups[0xC061] = "Something else"
    stale = {
        "elementAddress": 0x0149,
        "groupAddress": 0xC0EE,
    }  # a row naming no group of the file
    pf.meta["elementConnectionGroups"] = [
        {"elementAddress": 0x0148, "groupAddress": 0xC061},
        stale,
    ]
    pf.remove_node(node, iv_index=7)
    assert 0xC061 not in pf.cdb.groups
    assert pf.meta["elementConnectionGroups"] == [stale]


def test_the_rows_the_app_keeps_of_the_node_go_too() -> None:
    """Review-4 F4-6: its scene values, SIG timer rows and the InsertId / ButtonLayout caches of its elements go
    with it; another node's rows, a row naming no node and an odd entry stay, written back in the app's bytes."""
    path = FIXTURES / "JungHome-android.json"
    pf = ProjectFile.load(path)
    node = pf.cdb.node_by_addr(0x0148)
    assert node is not None
    mine = pf.meta["sceneInfo"][0]["deviceId"]  # the fixture's row is 0148's
    other = {**mine, "nodeId": "00005EFF-FE00-5330-0000-000000000000"}
    pf.meta["schedulerMetaInfo"] = [
        {"schedulerIndex": 0, "deviceId": mine, "action": 1, "scene": 0},
        {"schedulerIndex": 0, "deviceId": other, "action": 1, "scene": 0},
        {"schedulerIndex": 1},
        None,
    ]
    pf.meta["timer"] = [{"id": "a", "index": 0, "deviceId": mine}]
    pf.remove_node(node, iv_index=0)
    assert pf.meta["sceneInfo"] == []
    assert pf.meta["schedulerMetaInfo"] == [
        {"schedulerIndex": 0, "deviceId": other, "action": 1, "scene": 0},
        {"schedulerIndex": 1},
        None,
    ]
    assert pf.meta["timer"] == []
    assert [r["elementAddress"] for r in pf.meta["actuatorExports"]] == [562, 768]
    assert [r["elementAddress"] for r in pf.meta["buttonLayoutExports"]] == [562, 768]
    # what stays is as the app wrote it: its rows of 0232, byte for byte
    text = pf.render()
    for row in (
        """      {
        "actuatorId": {
          "actuatorFunctionId": 4,
          "insertType": 2
        },
        "elementAddress": 562
      },""",
        """      {
        "mode": 5,
        "elementAddress": 562
      },""",
    ):
        assert row in path.read_text()
        assert row in text


def test_a_file_without_those_lists_gets_none() -> None:
    pf = ProjectFile.load(FIXTURES / "JungHome.json")
    node = pf.cdb.node_by_addr(0x0148)
    assert node is not None
    pf.remove_node(node, iv_index=0)
    for key in (
        "sceneInfo",
        "schedulerMetaInfo",
        "timer",
        "actuatorExports",
        "buttonLayoutExports",
    ):
        assert key not in pf.meta
