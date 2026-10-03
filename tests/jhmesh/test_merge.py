"""jhmesh.merge: carrying Home Assistant's changes over onto the app's next upload (review-3 W1)."""

from __future__ import annotations

import copy
from typing import Any

from jhmesh.merge import MISSING, Change, Key, Row, apply_changes, diff_documents


def doc() -> dict[str, Any]:
    return {
        "network": {
            "timestamp": "2020-01-01T00:00:00Z",
            "nodes": [
                {
                    "UUID": "00000001-0000-4000-8000-000000000001",
                    "name": "switch",
                    "elements": [
                        {
                            "index": 0,
                            "models": [
                                {"modelId": "1000", "subscribe": ["C001"], "bind": [0]},
                                {
                                    "modelId": "1001",
                                    "subscribe": [],
                                    "publish": {"address": "0000"},
                                },
                            ],
                        }
                    ],
                },
                {
                    "UUID": "00000002-0000-4000-8000-000000000002",
                    "name": "dimmer",
                    "elements": [{"index": 0, "models": [{"modelId": "1000"}]}],
                },
            ],
            "groups": [{"address": "C001", "name": "Living"}],
        },
        "meta": {
            "userGroups": [{"address": 49153, "name": "Living"}],
            "devices": [
                {
                    "name": "Switch",
                    "deviceId": {
                        "nodeId": "00000001-0000-4000-8000-000000000001",
                        "locationIds": [1],
                    },
                    "cachedGroupConnectionMetadata": [],
                }
            ],
            "keyModeSceneConfigExports": [],
        },
    }


def node(d: dict[str, Any], i: int) -> dict[str, Any]:
    return d["network"]["nodes"][i]


def test_our_change_survives_an_unrelated_app_change() -> None:
    """The scenario: HA links a key and puts a load in a room; the app, which never saw that, renames a node and
    adds a room, then uploads. The merge keeps both sides."""
    base = doc()
    ours = copy.deepcopy(base)
    node(ours, 0)["elements"][0]["models"][1]["publish"]["address"] = "C002"
    node(ours, 1)["elements"][0]["models"][0]["subscribe"] = ["C001"]
    ours["network"]["groups"].append({"address": "C002", "name": "Hall"})
    ours["meta"]["keyModeSceneConfigExports"].append({"elementAddress": 1, "scene": 3})
    ours["network"]["timestamp"] = "2021-01-01T00:00:00Z"
    changes = diff_documents(base, ours)
    assert all(c.path != ("network", "timestamp") for c in changes)

    theirs = copy.deepcopy(base)
    node(theirs, 1)["name"] = "dimmer (renamed in the app)"
    theirs["network"]["groups"].append({"address": "C003", "name": "Kitchen"})
    theirs["network"]["nodes"].append(
        {"UUID": "00000003-0000-4000-8000-000000000003", "name": "new", "elements": []}
    )
    applied, conflicts = apply_changes(theirs, changes)
    assert conflicts == []
    assert len(applied) == len(changes)
    assert node(theirs, 0)["elements"][0]["models"][1]["publish"]["address"] == "C002"
    assert node(theirs, 1)["elements"][0]["models"][0]["subscribe"] == ["C001"]
    assert node(theirs, 1)["name"] == "dimmer (renamed in the app)"
    assert [g["address"] for g in theirs["network"]["groups"]] == [
        "C001",
        "C003",
        "C002",
    ]
    assert theirs["meta"]["keyModeSceneConfigExports"] == [
        {"elementAddress": 1, "scene": 3}
    ]
    assert len(theirs["network"]["nodes"]) == 3
    assert theirs["network"]["timestamp"] == "2020-01-01T00:00:00Z"  # theirs


def test_subscriptions_merge_as_sets() -> None:
    base = doc()
    ours = copy.deepcopy(base)
    node(ours, 0)["elements"][0]["models"][0]["subscribe"] = [
        "C002"
    ]  # left C001, joined C002
    theirs = copy.deepcopy(base)
    node(theirs, 0)["elements"][0]["models"][0]["subscribe"] = ["C001", "C00A"]
    applied, conflicts = apply_changes(theirs, diff_documents(base, ours))
    assert conflicts == []
    assert len(applied) == 1
    assert node(theirs, 0)["elements"][0]["models"][0]["subscribe"] == ["C00A", "C002"]


def test_the_app_wins_a_conflict() -> None:
    """The app re-linked the same key after HA did: its Config messages went out later, its value stays."""
    base = doc()
    ours = copy.deepcopy(base)
    node(ours, 0)["elements"][0]["models"][1]["publish"]["address"] = "C002"
    theirs = copy.deepcopy(base)
    node(theirs, 0)["elements"][0]["models"][1]["publish"]["address"] = "C00F"
    applied, conflicts = apply_changes(theirs, diff_documents(base, ours))
    assert applied == []
    assert [c.where() for c in conflicts] == [
        "network.nodes[00000001000040008000000000000001].elements[0].models[1001].publish.address"
    ]
    assert node(theirs, 0)["elements"][0]["models"][1]["publish"]["address"] == "C00F"


def test_a_change_inside_something_the_app_removed_is_a_conflict() -> None:
    base = doc()
    ours = copy.deepcopy(base)
    node(ours, 1)["elements"][0]["models"][0]["subscribe"] = ["C001"]
    node(ours, 1)["name"] = "renamed by HA"
    theirs = copy.deepcopy(base)
    del theirs["network"]["nodes"][1]  # the app removed the node
    applied, conflicts = apply_changes(theirs, diff_documents(base, ours))
    assert applied == []
    assert len(conflicts) == 2
    assert len(theirs["network"]["nodes"]) == 1


def test_removals_additions_and_already_present_changes() -> None:
    base = doc()
    ours = copy.deepcopy(base)
    del ours["network"]["groups"][0]  # HA deleted the room
    ours["meta"]["userGroups"] = []
    del node(ours, 0)["elements"][0]["models"][0]["bind"]  # a key HA dropped
    ours["meta"]["devices"][0]["name"] = "Switch (HA)"
    changes = diff_documents(base, ours)
    theirs = copy.deepcopy(base)
    theirs["meta"]["devices"][0]["name"] = "Switch (HA)"  # the same edit on both sides
    applied, conflicts = apply_changes(theirs, changes)
    assert conflicts == []
    assert len(applied) == len(changes)
    assert theirs["network"]["groups"] == []
    assert theirs["meta"]["userGroups"] == []
    assert "bind" not in node(theirs, 0)["elements"][0]["models"][0]
    # applying again changes nothing: every change is already there
    again = copy.deepcopy(theirs)
    applied, conflicts = apply_changes(again, changes)
    assert conflicts == []
    assert again == theirs


def test_arrays_without_an_identity_are_sets_of_rows() -> None:
    base = doc()
    base["meta"]["keyModeSceneConfigExports"] = [{"a": 1}, {"a": 2}]
    ours = copy.deepcopy(base)
    ours["meta"]["keyModeSceneConfigExports"] = [{"a": 2}, {"a": 3}]
    changes = diff_documents(base, ours)
    assert {(type(c.path[-1]), c.old is MISSING) for c in changes} == {
        (Row, True),
        (Row, False),
    }
    theirs = copy.deepcopy(base)
    theirs["meta"]["keyModeSceneConfigExports"].append({"a": 9})
    apply_changes(theirs, changes)
    assert theirs["meta"]["keyModeSceneConfigExports"] == [{"a": 2}, {"a": 9}, {"a": 3}]


def test_duplicate_identities_fall_back_to_rows_and_type_changes_are_changes() -> None:
    base = doc()
    base["network"]["groups"].append({"address": "C001", "name": "twin"})
    ours = copy.deepcopy(base)
    ours["network"]["groups"][1]["name"] = "twin renamed"
    ours["meta"]["userGroups"] = {"not": "a list"}  # a value of another type
    ours["meta"]["extra"] = [[1, 2]]  # an array of arrays: rows
    changes = diff_documents(base, ours)
    assert any(isinstance(c.path[-1], Row) for c in changes)
    theirs = copy.deepcopy(base)
    _applied, conflicts = apply_changes(theirs, changes)
    assert conflicts == []
    assert theirs["meta"]["userGroups"] == {"not": "a list"}
    assert theirs["meta"]["extra"] == [[1, 2]]
    assert {g["name"] for g in theirs["network"]["groups"]} == {
        "Living",
        "twin renamed",
    }


def test_member_changes_on_a_value_that_is_no_longer_a_list_conflict() -> None:
    change = Change(("meta", "userGroups"), [1], [1, 2], members=True)
    d = {"meta": {"userGroups": "gone"}}
    assert apply_changes(d, [change]) == ([], [change])
    assert apply_changes({"meta": 3}, [change]) == ([], [change])
    # a step into an array that is not one, or into a keyed entry of an array without an identity rule
    assert apply_changes({"a": {}}, [Change(("a", Key(1), "x"), 1, 2)])[1]
    assert apply_changes({"a": [{"x": 1}]}, [Change(("a", Key(1), "x"), 1, 2)])[1]
    assert apply_changes({"nodes": [3]}, [Change(("nodes", Key(1), "x"), 1, 2)])[1]


def test_devices_are_matched_by_node_and_locations() -> None:
    base = doc()
    ours = copy.deepcopy(base)
    ours["meta"]["devices"][0]["cachedGroupConnectionMetadata"].append(
        {"groupAddress": 49153, "function": "LIGHT"}
    )
    theirs = copy.deepcopy(base)
    theirs["meta"]["devices"].insert(
        0,
        {
            "name": "Other",
            "deviceId": {
                "nodeId": "00000002-0000-4000-8000-000000000002",
                "locationIds": [1],
            },
        },
    )
    theirs["meta"]["devices"].append(
        {"name": "no id"}
    )  # a row without one: the array is rows then
    _applied, conflicts = apply_changes(theirs, diff_documents(base, ours))
    assert conflicts == []
    assert theirs["meta"]["devices"][1]["cachedGroupConnectionMetadata"] == [
        {"groupAddress": 49153, "function": "LIGHT"}
    ]
    assert Change(("x",), 1, 2).where() == "x"
    assert Change(("a", Row("{}"), "b"), 1, 2).where() == "a[row].b"


def test_scene_info_rows_are_matched_by_scene_and_device() -> None:
    """Review-4 F4-6: Home Assistant writes a row per scene and device; one it rewrote is one change of its
    values, carried onto the app's upload next to the app's own new row of the same scene."""
    first = {"nodeId": "00000001-0000-4000-8000-000000000001", "locationIds": [1]}
    second = {"nodeId": "00000002-0000-4000-8000-000000000002", "locationIds": [1]}
    base: dict[str, Any] = {
        "meta": {
            "sceneInfo": [
                {"scene": 1, "deviceId": first, "infos": {"lightness": 0}},
                {"scene": 1, "deviceId": second, "infos": {"lightness": 0}},
            ]
        }
    }
    ours = copy.deepcopy(base)
    ours["meta"]["sceneInfo"][1]["infos"]["lightness"] = 40
    changes = diff_documents(base, ours)
    assert [c.path for c in changes] == [
        (
            "meta",
            "sceneInfo",
            Key((1, ("00000002000040008000000000000002", ("1",)))),
            "infos",
            "lightness",
        )
    ]
    theirs = copy.deepcopy(base)
    added = {"scene": 2, "deviceId": first, "infos": {"lightness": 100}}
    theirs["meta"]["sceneInfo"].insert(0, added)
    _applied, conflicts = apply_changes(theirs, changes)
    assert conflicts == []
    assert theirs["meta"]["sceneInfo"][2]["infos"] == {"lightness": 40}
    # a row naming no scene or no device: the rows are matched whole
    base["meta"]["sceneInfo"].append({"scene": None, "deviceId": first})
    assert all(type(c.path[2]) is Row for c in diff_documents(base, ours))


def test_odd_shapes() -> None:
    assert repr(MISSING) == "MISSING"
    # an array whose entries lack the identity is a set of rows; one turned into a dict takes no row
    base = {"meta": {"devices": [{"name": "no id"}]}}
    ours = {"meta": {"devices": [{"name": "no id"}, {"name": "added"}]}}
    changes = diff_documents(base, ours)
    assert [type(c.path[-1]) for c in changes] == [Row]
    theirs: dict[str, Any] = {"meta": {"devices": {"a": 1}}}
    assert apply_changes(theirs, changes) == ([], changes)
    # members already there: applied, nothing to do
    change = Change(("s",), ["A"], ["A", "B"], members=True)
    d = {"s": ["B", "A"]}
    assert apply_changes(d, [change]) == ([change], [])
    assert d == {"s": ["B", "A"]}


def test_a_key_both_sides_changed_keeps_one_row_per_element_and_reports_the_conflict() -> (
    None
):
    """Review-4 S4-4: HA puts key 328 into mode 5 / scene 5 while the app puts it into mode 3 / scene 7. The
    `*Exports` rows are matched by `elementAddress` now: one row per element survives (the app's, its Config
    messages went out later) and both edits are reported — content-matched rows used to keep both versions."""
    base = doc()
    base["meta"]["buttonLayoutExports"] = [
        {"mode": 1, "elementAddress": 328},
        {"mode": 5, "elementAddress": 562},
    ]
    base["meta"]["keyModeSceneConfigExports"] = [
        {
            "sceneConfig": {"sceneId": 1, "transitionStepSeconds": 0},
            "elementAddress": 328,
        }
    ]
    base["meta"]["actuatorExports"] = [
        {
            "actuatorId": {"actuatorFunctionId": 0, "insertType": 2},
            "elementAddress": 328,
        }
    ]
    base["network"]["networkExclusions"] = [{"ivIndex": 0, "addresses": ["0002"]}]
    ours = copy.deepcopy(base)
    ours["meta"]["buttonLayoutExports"][0]["mode"] = 5
    ours["meta"]["keyModeSceneConfigExports"][0]["sceneConfig"]["sceneId"] = 5
    ours["meta"]["actuatorExports"][0]["actuatorId"]["actuatorFunctionId"] = 4
    ours["network"]["networkExclusions"][0]["addresses"].append("0003")
    theirs = copy.deepcopy(base)
    theirs["meta"]["buttonLayoutExports"][0]["mode"] = 3
    theirs["meta"]["keyModeSceneConfigExports"][0]["sceneConfig"]["sceneId"] = 7
    theirs["network"]["networkExclusions"].append({"ivIndex": 1, "addresses": []})

    applied, conflicts = apply_changes(theirs, diff_documents(base, ours))

    meta = theirs["meta"]
    assert [r["elementAddress"] for r in meta["buttonLayoutExports"]] == [328, 562]
    assert [r["elementAddress"] for r in meta["keyModeSceneConfigExports"]] == [328]
    assert [r["elementAddress"] for r in meta["actuatorExports"]] == [328]
    assert meta["buttonLayoutExports"][0]["mode"] == 3  # the app's
    assert meta["keyModeSceneConfigExports"][0]["sceneConfig"]["sceneId"] == 7
    assert sorted(c.where() for c in conflicts) == [
        "meta.buttonLayoutExports[328].mode",
        "meta.keyModeSceneConfigExports[328].sceneConfig.sceneId",
    ]
    # what only HA changed is carried over, inside the matched rows
    assert meta["actuatorExports"][0]["actuatorId"]["actuatorFunctionId"] == 4
    assert theirs["network"]["networkExclusions"] == [
        {"ivIndex": 0, "addresses": ["0002", "0003"]},
        {"ivIndex": 1, "addresses": []},
    ]
    assert len(applied) == 2


def test_element_addresses_may_be_hex_text() -> None:
    """`meta` rows carry an address as an int or as hex text (`devices.as_int`): both name the same element; text
    that is no address, or a flag, leaves the array a set of rows."""
    base = {"meta": {"buttonLayoutExports": [{"mode": 1, "elementAddress": "0148"}]}}
    ours = copy.deepcopy(base)
    ours["meta"]["buttonLayoutExports"][0]["mode"] = 5
    theirs = {"meta": {"buttonLayoutExports": [{"mode": 1, "elementAddress": 328}]}}
    _applied, conflicts = apply_changes(theirs, diff_documents(base, ours))
    assert conflicts == []
    assert theirs["meta"]["buttonLayoutExports"] == [{"mode": 5, "elementAddress": 328}]
    for odd in ("x", True):
        rows = {"meta": {"buttonLayoutExports": [{"mode": 1, "elementAddress": odd}]}}
        changed = copy.deepcopy(rows)
        changed["meta"]["buttonLayoutExports"][0]["mode"] = 2
        assert [type(c.path[-1]) for c in diff_documents(rows, changed)] == [Row, Row]
