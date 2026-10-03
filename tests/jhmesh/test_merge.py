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
