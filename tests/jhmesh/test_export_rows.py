"""jhmesh.export: the `meta` rows the configurator reads and writes through `ProjectFile` (review-4 A4-10).

Room links (`cachedGroupConnectionMetadata`), the keys' scene rows (`keyModeSceneConfigExports`), the app device
covering an element and a node's CDB entry: each rewrite mirrors the file's own style and leaves a file without the
row byte-identical.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

from jhmesh.export import ProjectFile, RoomLink

from .conftest import FIXTURES

if TYPE_CHECKING:
    from jhmesh.cdb import Node

ANDROID = FIXTURES / "JungHome-android.json"
LINKED_KEY = 0x0301  # the fixture's one room link: room C00F, publishing to C071, LIGHT
ROOM = 0xC00F
SCENE_KEY = 0x0149  # the fixture's one scene row: scene 1, publication C061
LIGHT = 0x0148


@pytest.fixture
def pf() -> ProjectFile:
    return ProjectFile.load(ANDROID)


def node(pf: ProjectFile, unicast: int) -> Node:
    found = pf.cdb.node_by_addr(unicast)
    assert found is not None
    return found


def link_rows(pf: ProjectFile) -> list[Any]:
    return [
        r
        for d in pf.meta["devices"]
        if isinstance(d, dict)
        for r in d.get("cachedGroupConnectionMetadata") or []
    ]


def test_room_links_read_the_rows() -> None:
    pf = ProjectFile.load(ANDROID)
    assert pf.room_links(ROOM) == [RoomLink(LINKED_KEY, ROOM, 0xC071, 0)]
    assert pf.room_links(0xC010) == []
    assert RoomLink.of({"elementAddress": "0301", "function": None}) == RoomLink(
        0x0301, None, None, None
    )


def test_take_room_links_removes_only_the_keys_rows(pf: ProjectFile) -> None:
    devices = pf.meta["devices"]
    devices[0]["cachedGroupConnectionMetadata"] = [None, {"elementAddress": 0x0149}]
    devices.append("stray")
    del devices[1]["cachedGroupConnectionMetadata"]
    before = copy.deepcopy(devices)
    assert pf.take_room_links(LINKED_KEY) == [RoomLink(LINKED_KEY, ROOM, 0xC071, 0)]
    assert link_rows(pf) == [None, {"elementAddress": 0x0149}]
    # only the device that had the row is rewritten; one without the list keeps lacking it
    assert "cachedGroupConnectionMetadata" not in devices[1]
    assert devices[:5] == before[:5]
    assert devices[5] == {**before[5], "cachedGroupConnectionMetadata": []}
    assert pf.take_room_links(LINKED_KEY) == []


def test_drop_room_links_drops_the_rows_and_the_scene_row(pf: ProjectFile) -> None:
    pf.drop_room_links(LINKED_KEY)
    assert link_rows(pf) == []
    assert len(pf.meta["keyModeSceneConfigExports"]) == 1
    pf.drop_room_links(SCENE_KEY)
    assert pf.meta["keyModeSceneConfigExports"] == []


@pytest.mark.parametrize("keep", [False, True])
def test_add_room_link_replaces_the_keys_rows_unless_kept(
    pf: ProjectFile, *, keep: bool
) -> None:
    entry = pf.device_entry(node(pf, 0x0300), 64)
    assert entry is not None
    pf.add_room_link(entry, LINKED_KEY, 0xC010, 0xC071, "SWITCH", keep=keep)
    new = {
        "elementAddress": LINKED_KEY,
        "groupAddress": 0xC010,
        "publishAddress": 0xC071,
        "function": "SWITCH",
    }
    old = {**new, "groupAddress": ROOM, "function": "LIGHT"}
    assert entry["cachedGroupConnectionMetadata"] == ([old, new] if keep else [new])
    # the same row again: replaced, never doubled
    pf.add_room_link(entry, LINKED_KEY, 0xC010, 0xC071, "SWITCH", keep=keep)
    assert entry["cachedGroupConnectionMetadata"] == ([old, new] if keep else [new])


def test_add_room_link_mirrors_the_files_style(pf: ProjectFile) -> None:
    template = pf.meta["devices"][5]["cachedGroupConnectionMetadata"][0]
    template.update(
        function=0, elementAddress="0301", groupAddress="C00F", publishAddress="C071"
    )
    entry: dict[str, Any] = {}
    pf.add_room_link(entry, 0x0234, ROOM, 0xC044, "SWITCH")
    assert entry["cachedGroupConnectionMetadata"] == [
        {
            "elementAddress": "0234",
            "groupAddress": "C00F",
            "publishAddress": "C044",
            "function": 4,
        }
    ]
    # no row anywhere to copy: the app's own (Gson) style
    for dev in pf.meta["devices"]:
        dev["cachedGroupConnectionMetadata"] = []
    pf.add_room_link(entry, 0x0234, ROOM, 0xC044, "SWITCH")
    assert entry["cachedGroupConnectionMetadata"] == [
        {
            "elementAddress": 0x0234,
            "groupAddress": ROOM,
            "publishAddress": 0xC044,
            "function": "SWITCH",
        }
    ]


def test_scene_link_keys_read_the_scene_rows(pf: ProjectFile) -> None:
    rows = pf.meta["keyModeSceneConfigExports"]
    rows += [
        None,
        {"elementAddress": 0x0234},
        {"elementAddress": None, "sceneConfig": {"sceneId": 1}},
        {"elementAddress": "0235", "sceneConfig": {"sceneId": "1"}},
        {"elementAddress": 0x0236, "sceneConfig": {"sceneId": 2}},
    ]
    assert pf.scene_link_keys(1) == {SCENE_KEY, 0x0235}
    assert pf.scene_link_keys(2) == {0x0236}
    assert pf.scene_link_keys(3) == set()


def test_record_scene_link_mirrors_the_existing_row(pf: ProjectFile) -> None:
    pf.record_scene_link(0x0234, 2, None)
    pf.record_scene_link(SCENE_KEY, 3, 0xC061)
    assert pf.meta["keyModeSceneConfigExports"] == [
        {
            "sceneConfig": {
                "transitionStepSeconds": 0,
                "sceneId": 2,
                "transitionResolution": 0,
            },
            "elementAddress": 0x0234,
        },
        {
            "sceneConfig": {
                "transitionStepSeconds": 0,
                "sceneId": 3,
                "transitionResolution": 0,
                "publicationAddress": 0xC061,
            },
            "elementAddress": SCENE_KEY,
        },
    ]


def test_record_scene_link_in_hex_string_style_and_without_a_row(
    pf: ProjectFile,
) -> None:
    pf.meta["keyModeSceneConfigExports"] = [
        {"elementAddress": "0149", "sceneConfig": {"sceneId": 1}}
    ]
    pf.record_scene_link(0x0234, 2, 0xC044)
    pf.record_scene_link(0x0235, 3, None)
    assert pf.meta["keyModeSceneConfigExports"][1:] == [
        {
            "elementAddress": "0234",
            "sceneConfig": {
                "sceneId": 2,
                "transitionStepSeconds": 0,
                "transitionResolution": 0,
                "publicationAddress": "C044",
            },
        },
        {
            "elementAddress": "0235",
            "sceneConfig": {
                "sceneId": 3,
                "transitionStepSeconds": 0,
                "transitionResolution": 0,
            },
        },
    ]
    del pf.meta["keyModeSceneConfigExports"]
    pf.record_scene_link(0x0234, 2, None)
    assert pf.meta["keyModeSceneConfigExports"] == [
        {
            "sceneConfig": {
                "transitionStepSeconds": 0,
                "sceneId": 2,
                "transitionResolution": 0,
            },
            "elementAddress": 0x0234,
        }
    ]


def test_drop_scene_link_leaves_a_file_without_the_row_untouched(
    pf: ProjectFile,
) -> None:
    rows = pf.meta["keyModeSceneConfigExports"]
    pf.drop_scene_link(0x0234)
    assert pf.meta["keyModeSceneConfigExports"] is rows
    pf.drop_scene_link(SCENE_KEY)
    assert pf.meta["keyModeSceneConfigExports"] == []
    del pf.meta["keyModeSceneConfigExports"]
    pf.drop_scene_link(SCENE_KEY)
    assert "keyModeSceneConfigExports" not in pf.meta


def test_device_entry_is_the_most_specific_covering_entry(pf: ProjectFile) -> None:
    light = node(pf, LIGHT)
    devices = pf.meta["devices"]
    button = pf.device_entry(light, 64)
    assert button is not None
    assert button["name"] == "WC mirror button"
    assert pf.device_locations(button) == [64, 68]
    # a narrower entry for the same location wins
    narrow = copy.deepcopy(button)
    narrow["deviceId"]["locationIds"] = [64]
    devices.append(narrow)
    assert pf.device_entry(light, 64) is narrow
    assert pf.device_entry(light, 68) is button
    # rows that name nothing usable are passed over
    devices.insert(0, {"deviceId": None})
    devices.insert(
        0, {**narrow, "deviceId": {**narrow["deviceId"], "locationIds": ["x"]}}
    )
    assert pf.device_entry(light, 64) is narrow
    assert pf.device_entry(light, 2) is None
    assert pf.device_locations({"deviceId": {"locationIds": None}}) == []
    assert pf.device_locations({}) == []


def test_node_entry_is_the_cdb_row(pf: ProjectFile) -> None:
    assert pf.node_entry(LIGHT)["unicastAddress"] == "0148"
    with pytest.raises(StopIteration):
        pf.node_entry(0x0999)
