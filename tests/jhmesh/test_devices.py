"""jhmesh.devices: the CDB (+ app metadata) turned into lights, sockets, buttons and scenes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jhmesh.cdb import CDB, Element, Node, canonical_uuid
from jhmesh.devices import (
    BLIND_ONLY_PIDS,
    ELEMENT_RULES,
    THERMOSTAT_PIDS,
    Blind,
    BuildContext,
    Button,
    Detector,
    Device,
    Devices,
    ElementRule,
    InvalidMetadata,
    KeyConnection,
    Light,
    Metadata,
    SceneDef,
    Socket,
    Thermostat,
    build_devices,
    ctl_temperature_element,
    expected_device_count,
    insert_function,
    insert_mismatch,
    key_position,
    load_kind,
    meta_list,
    meter_element,
    pick_insert,
    time_keeper_candidates,
    with_key_lock,
)
from jhmesh.properties import EnforcedOutput

from .conftest import (
    CDB_PATH,
    FIXTURES,
    GROUP_LIVING,
    GROUP_WC,
    LIGHT_2G,
    META_DIR,
    PROXY_NODE,
    SENSOR,
    SOCKET,
    on_small_stack,
)

GROUP_KITCHEN = 0xC011
DIMMER = 0x0300
ACTUATOR = 0x0400
UUID_1G = "00005EFF-FE00-5314-0000-000000000000"
UUID_2G = "00005EFF-FE00-5323-0000-000000000000"
UUID_SOCKET = "00005EFF-FE00-5317-0000-000000000000"
UUID_DIMMER = "00005EFF-FE00-5330-0000-000000000000"
UUID_ACTUATOR = "00005EFF-FE00-5340-0000-000000000000"


@pytest.fixture
def meta() -> Metadata:
    return Metadata(META_DIR / "device_metadata.json", META_DIR / "scene_metadata.json")


# ----------------------------------------------------------------------------- Metadata


def test_metadata_empty_when_no_files(tmp_path: Path):
    m = Metadata()
    assert m.devices == {}
    assert m.scenes == {}
    m = Metadata(tmp_path / "missing.json", tmp_path / "missing2.json")
    assert m.devices == {}
    assert m.scenes == {}
    assert m.name_for(UUID_1G, 1) is None
    assert m.entry_for(UUID_1G, 1) is None


def test_metadata_parses_swift_pair_encoding(meta: Metadata):
    assert meta.devices[UUID_1G] == [([1], "WC mirror"), ([64, 68], "WC mirror button")]
    assert meta.devices[UUID_2G] == [
        ([1], "Living room DALI"),
        ([64, 65, 68], "Living room rocker"),
    ]
    assert meta.devices[UUID_SOCKET] == [([1, 64], "Boiler")]
    assert meta.devices[UUID_DIMMER] == [
        ([1], "WC ceiling"),
        ([64], "WC ceiling button"),
    ]
    assert meta.devices[UUID_ACTUATOR] == [([1], "Kitchen ceiling")]
    assert meta.scenes == {1: "WC off", 2: "All off"}


def test_metadata_name_lookup(meta: Metadata):
    assert meta.name_for(UUID_1G, 1) == "WC mirror"
    assert meta.name_for(UUID_1G, 64) == "WC mirror button"
    assert meta.name_for(UUID_1G, 68) == "WC mirror button"
    assert meta.name_for(UUID_1G, 2) is None
    assert meta.name_for("unknown", 1) is None
    assert meta.entry_for(UUID_SOCKET, 64) == ([1, 64], "Boiler")
    assert meta.entry_for(UUID_ACTUATOR, 2) is None


def test_metadata_prefers_most_specific_entry(tmp_path: Path):
    p = tmp_path / "device_metadata.json"
    p.write_text(
        json.dumps(
            [
                {"nodeId": "N", "locationIds": [68, 64, 1]},
                {"name": "Whole device"},
                {"nodeId": "N", "locationIds": [64]},
                {"name": "Key A"},
            ]
        )
    )
    m = Metadata(p)
    assert m.devices["N"] == [([1, 64, 68], "Whole device"), ([64], "Key A")]
    assert m.name_for("N", 64) == "Key A"
    assert m.name_for("N", 68) == "Whole device"
    assert m.name_for("N", 1) == "Whole device"
    assert m.scenes == {}


# ----------------------------------------------------------------------------- build_devices


def test_build_devices_without_metadata(cdb: CDB):
    d = build_devices(cdb)
    assert isinstance(d, Devices)
    assert d.rooms == {
        GROUP_WC: "WC",
        GROUP_LIVING: "Living room",
        GROUP_KITCHEN: "Kitchen",
    }

    assert [
        (light.address, light.name, light.kind, light.rooms) for light in d.lights
    ] == [
        (PROXY_NODE, "Push-button 1-gang 0148", "switch", ["WC"]),
        (LIGHT_2G, "Push-button 2-gang 0232", "ctl", ["Living room"]),
        (DIMMER, "Push-button 1-gang 0300", "dimmer", ["WC"]),
        (ACTUATOR, "2-channel actuator 0400", "switch", ["Kitchen"]),
        (ACTUATOR + 1, "2-channel actuator 0400 out 2", "switch", ["Kitchen"]),
    ]
    assert [light.unique_id for light in d.lights] == [
        f"{UUID_1G.lower()}-0001",
        f"{UUID_2G.lower()}-0001",
        f"{UUID_DIMMER.lower()}-0001",
        f"{UUID_ACTUATOR.lower()}-0001",
        f"{UUID_ACTUATOR.lower()}-0002",
    ]
    assert d.lights[0].node.name == "Push-button 1-gang"
    assert d.lights[1].node.unicast == LIGHT_2G

    assert [(s.address, s.name, s.meter_address, s.rooms) for s in d.sockets] == [
        (SOCKET, "Socket 0172", SENSOR, ["Kitchen"])
    ]
    assert d.sockets[0].unique_id == f"{UUID_SOCKET.lower()}-0001"
    assert d.sockets[0].node.unicast == SOCKET

    assert [
        (b.address, b.name, b.key, b.location, b.group_name) for b in d.buttons
    ] == [
        (
            PROXY_NODE + 1,
            "Push-button 1-gang 0148 buttons A",
            "A",
            0x40,
            "Push-button 1-gang 0148 buttons",
        ),
        (
            LIGHT_2G + 2,
            "Push-button 2-gang 0232 buttons A",
            "A",
            0x40,
            "Push-button 2-gang 0232 buttons",
        ),
        (
            LIGHT_2G + 3,
            "Push-button 2-gang 0232 buttons B",
            "B",
            0x41,
            "Push-button 2-gang 0232 buttons",
        ),
        (
            DIMMER + 1,
            "Push-button 1-gang 0300 buttons A",
            "A",
            0x40,
            "Push-button 1-gang 0300 buttons",
        ),
    ]
    assert all(b.key_mode is None for b in d.buttons)
    assert d.buttons[0].unique_id == f"{UUID_1G.lower()}-0040"
    assert d.buttons[0].node.unicast == PROXY_NODE

    assert d.scenes == [
        SceneDef(1, "Scene #1"),
        SceneDef(2, "Scene #2"),
    ]  # the CDB's own names


def test_build_devices_skips_provisioner_gateway_and_secondary_elements(cdb: CDB):
    d = build_devices(cdb)
    addresses = {x.address for coll in (d.lights, d.sockets, d.buttons) for x in coll}
    assert 0x0001 not in addresses  # iPhone (no pid)
    assert 0x00DC not in addresses  # gateway: no OnOff/Lightness server on element 0
    assert LIGHT_2G + 1 not in addresses  # CTL temperature element
    assert SENSOR not in addresses  # socket power sensor is not a button
    assert PROXY_NODE + 2 not in addresses  # vendor-only element (location 0x44)
    assert LIGHT_2G + 4 not in addresses


def test_build_devices_with_metadata(cdb: CDB, meta: Metadata):
    d = build_devices(cdb, meta)
    assert [light.name for light in d.lights] == [
        "WC mirror",
        "Living room DALI",
        "WC ceiling",
        "Kitchen ceiling",
        "2-channel actuator 0400 out 2",
    ]
    assert [s.name for s in d.sockets] == ["Boiler"]
    assert [(b.name, b.group_name) for b in d.buttons] == [
        ("WC mirror button A", "WC mirror button"),
        ("Living room rocker A", "Living room rocker"),
        ("Living room rocker B", "Living room rocker"),
        ("WC ceiling button A", "WC ceiling button"),
    ]
    assert d.scenes == [SceneDef(1, "WC off"), SceneDef(2, "All off")]
    # unique ids and rooms never depend on the (renamable) metadata
    plain = build_devices(cdb)
    assert [light.unique_id for light in d.lights] == [
        light.unique_id for light in plain.lights
    ]
    assert [light.rooms for light in d.lights] == [
        light.rooms for light in plain.lights
    ]


def test_build_devices_kind_follows_models(cdb: CDB):
    ctl = cdb.element(LIGHT_2G)
    assert ctl is not None
    ctl.models.remove("1303")
    light = build_devices(cdb).by_address.get(LIGHT_2G)
    assert isinstance(light, Light)
    assert light.kind == "dimmer"
    ctl.models.remove("1300")
    light = build_devices(cdb).by_address.get(LIGHT_2G)
    assert isinstance(light, Light)
    assert light.kind == "switch"
    ctl.models.remove("1000")
    # a push-button load element with a Generic Level server alone is a blinds insert
    blind = build_devices(cdb).by_address.get(LIGHT_2G)
    assert isinstance(blind, Blind)
    assert (blind.kind, blind.slat_address, blind.rooms) == (
        "blind",
        None,  # the CTL temperature element next to it is a lamp's, not slats
        ["Living room"],
    )
    ctl.models.remove("1002")
    assert build_devices(cdb).by_address.get(LIGHT_2G) is None


def test_ctl_light_knows_its_temperature_element(cdb: CDB):
    """F11: a CTL light's Light CTL Temperature Server is on the next element hosting `1306`, where a colour
    temperature is set on its own; no other kind of light has one, nor a CTL light when another CTL Server comes
    first (that temperature element is the other light's) or none follows."""
    d = build_devices(cdb)
    assert [(light.address, light.temperature_address) for light in d.lights] == [
        (PROXY_NODE, None),
        (LIGHT_2G, LIGHT_2G + 1),
        (DIMMER, None),
        (ACTUATOR, None),
        (ACTUATOR + 1, None),
    ]
    node, ctl, temperature = (
        cdb.node_by_addr(LIGHT_2G),
        cdb.element(LIGHT_2G),
        cdb.element(LIGHT_2G + 1),
    )
    assert node is not None
    assert ctl is not None
    assert temperature is not None
    assert ctl_temperature_element(node, ctl) is temperature
    temperature.models.remove("1306")
    temperature.models.append(
        "1303"
    )  # another CTL light next: a temperature element after it is that light's
    assert ctl_temperature_element(node, ctl) is None
    temperature.models.remove("1303")  # no CTL Temperature Server on any later element
    assert ctl_temperature_element(node, ctl) is None


def test_load_kind_falls_back_for_a_non_primary_rtr_element_and_a_blind_slat() -> None:
    """`load_kind`'s two fallbacks, past `ELEMENT_RULES` itself: an RTR element other than the primary one
    `_is_thermostat` claims, and a blinds-only node's second (slat) element, which `_is_blind` never claims —
    only the first level element of such a node is `levels[0]`."""
    rtr = Node("0" * 32, "RTR", 0x0500, b"\x00" * 16, next(iter(THERMOSTAT_PIDS)))
    primary = Element(0x0500, 0x0001, ["1000", "1002"], rtr)
    sensor = Element(0x0501, 0x0001, ["1100"], rtr)
    rtr.elements = [primary, sensor]
    assert load_kind(sensor) == "thermostat"

    blind = Node("1" * 32, "Blind", 0x0600, b"\x00" * 16, next(iter(BLIND_ONLY_PIDS)))
    position = Element(0x0600, 0x0001, ["1000", "1002"], blind)
    slats = Element(0x0601, 0x0001, ["1002"], blind)
    blind.elements = [position, slats]
    assert load_kind(slats) == "blind"


def test_a_blinds_only_slat_element_with_an_onoff_server_is_no_light(cdb: CDB) -> None:
    """The slat element of a blinds actuator mini / puck is its blind's even when it hosts an OnOff server too:
    no spurious Light next to the Blind, and `load_kind` says blind for both elements (not the light rule's
    answer for the slats)."""
    blind = Node("1" * 32, "Blind", 0x0600, b"\x00" * 16, next(iter(BLIND_ONLY_PIDS)))
    position = Element(0x0600, 0x0001, ["1000", "1002"], blind)
    slats = Element(0x0601, 0x0001, ["1000", "1002"], blind)
    blind.elements = [position, slats]
    cdb.nodes.append(blind)
    d = build_devices(cdb)
    assert d.by_address.get(0x0601) is None
    built = d.by_address.get(0x0600)
    assert isinstance(built, Blind)
    assert built.slat_address == 0x0601
    assert (load_kind(position), load_kind(slats)) == ("blind", "blind")


def test_build_devices_rooms_ignore_foreign_element_groups_and_unnamed_addresses(
    cdb: CDB,
):
    light = cdb.element(PROXY_NODE)
    assert light is not None
    for m in light.raw_models:
        if m["modelId"] == "1000":
            m["subscribe"] = [
                "C062",
                "C123",
                "C00F",
                "C010",
            ]  # another element's group, unnamed, two rooms
    d = build_devices(cdb)
    l1 = d.by_address.get(PROXY_NODE)
    assert isinstance(l1, Light)
    assert l1.rooms == ["WC", "Living room"]


def test_build_devices_button_letters_beyond_d(cdb: CDB):
    button = cdb.element(PROXY_NODE + 1)
    assert button is not None
    button.location = 0x45
    b = build_devices(cdb).by_address.get(PROXY_NODE + 1)
    assert isinstance(b, Button)
    assert (b.key, b.name, b.input) == (
        "45",
        "Push-button 1-gang 0148 buttons 45",
        False,
    )


def test_mini_actuator_inputs_are_named_e1_e2_as_in_the_app():
    """A mini actuator's key-location elements are its binary inputs E1 / E2 (the app's names), not keys A / B."""
    blinds = build_devices(CDB.load(FIXTURES / "Blinds.json"))
    inputs = [(b.address, b.key, b.input) for b in blinds.buttons if b.input]
    assert inputs == [
        (0x0502, "E1", True),
        (0x0503, "E2", True),
        (0x0701, "E1", True),
        (0x0702, "E2", True),
    ]
    assert blinds.by_address[0x0502].name.endswith(" E1")
    # the push-button next to them keeps its key letters
    assert {b.key for b in blinds.buttons if not b.input} <= {"A", "B", "C", "D"}


def test_socket_without_sensor_element(cdb: CDB):
    sensor = cdb.element(SENSOR)
    assert sensor is not None
    sensor.models.remove("1100")
    d = build_devices(cdb)
    s = d.by_address.get(SOCKET)
    assert isinstance(s, Socket)
    assert s.meter_address is None
    assert d.by_meter(SENSOR) is None
    # without the sensor server the 0x40 element now looks like a plain button
    assert isinstance(d.by_address.get(SENSOR), Button)


def test_lookups(cdb: CDB, meta: Metadata):
    d = build_devices(cdb, meta)
    assert isinstance(d.by_address.get(PROXY_NODE), Light)
    assert isinstance(d.by_address.get(SOCKET), Socket)
    assert isinstance(d.by_address.get(LIGHT_2G + 3), Button)
    assert d.by_address.get(0x0001) is None
    assert d.by_address.get(GROUP_WC) is None
    s = d.by_meter(SENSOR)
    assert s is not None
    assert s.address == SOCKET
    assert s.name == "Boiler"
    assert d.by_meter(SOCKET) is None
    assert Devices().by_address.get(1) is None
    assert Devices().by_meter(1) is None


def test_meter_and_temperature_lookups_find_what_a_search_found():
    """Review-4 R4-9: kept up by `add` — a socket's meter before a light's, then the first added; the first CTL
    light of a temperature element."""
    node = Node("00000000-0000-4000-8000-0000000000aa", "n", 0x0700, bytes(16), 3)
    d = Devices()
    first = Light(
        0x0700, "a", "A", node, "ctl", temperature_address=0x0701, meter_address=0x0709
    )
    second = Light(
        0x0702, "b", "B", node, "ctl", temperature_address=0x0701, meter_address=0x0709
    )
    plain = Light(0x0703, "c", "C", node, "switch")
    socket = Socket(0x0704, "d", "D", node, 0x0709)
    later = Socket(0x0705, "e", "E", node, 0x0709)
    unmetered = Socket(0x0706, "f", "F", node, None)
    for device in (first, second, plain, socket, later, unmetered):
        d.add(device)
    searched = next(load for load in d.metered if load.meter_address == 0x0709)
    assert d.by_meter(0x0709) is searched is socket
    assert d.by_temperature(0x0701) is first
    assert d.by_temperature(0x0700) is None
    lights_only = Devices()
    lights_only.add(second)
    lights_only.add(first)
    assert lights_only.by_meter(0x0709) is second


# ----------------------------------------------------------------------------- rule table


def test_devices_index_and_kinds(cdb: CDB, meta: Metadata):
    d = build_devices(cdb, meta)
    typed = [x for coll in (d.lights, d.sockets, d.buttons) for x in coll]
    assert sorted(d.by_address) == sorted(x.address for x in typed)
    assert all(d.by_address[x.address] is x for x in typed)
    assert d.kinds() == {"switch", "dimmer", "ctl", "socket", "button"}
    assert [(s.kind, b.kind) for s in d.sockets for b in d.buttons[:1]] == [
        ("socket", "button")
    ]
    assert d.metadata is meta
    assert Devices().kinds() == set()
    assert Devices().by_address == {}


def test_button_gangs_follow_the_app_device_entries(cdb: CDB, meta: Metadata):
    """A gang is the location set of the app device entry a key sits in; without app names it is empty."""
    d = build_devices(cdb, meta)
    assert [(b.address, b.gang) for b in d.buttons] == [
        (PROXY_NODE + 1, (64, 68)),
        (LIGHT_2G + 2, (64, 65, 68)),
        (LIGHT_2G + 3, (64, 65, 68)),
        (DIMMER + 1, (64,)),
    ]
    assert [b.gang for b in build_devices(cdb).buttons] == [()] * 4


def test_an_appended_rule_claims_an_element_no_built_in_rule_wants(cdb: CDB):
    """A future device kind is one appended `ElementRule`: here the CTL dimmer's temperature element (Generic
    Level + CTL Temperature Server at location 0001), which the built-in rules leave alone."""

    def build(ctx, node, element):
        return Device(
            element.address,
            f"{ctx.unique_id(node, element)}-temperature",
            f"{ctx.node_label(node)} temperature",
            node,
            kind="temperature",
            rooms=ctx.rooms_of(element),
        )

    temperature = ElementRule(
        "temperature", lambda node, element, pid: "1306" in element.models, build
    )
    plain = build_devices(cdb)
    d = build_devices(cdb, rules=[*ELEMENT_RULES, temperature])

    probe = d.by_address[LIGHT_2G + 1]
    assert type(probe) is Device
    assert (probe.kind, probe.name, probe.unique_id, probe.rooms) == (
        "temperature",
        "Push-button 2-gang 0232 temperature",
        f"{UUID_2G.lower()}-0001-temperature",
        [],
    )
    assert probe.node is d.by_address[LIGHT_2G].node
    assert "temperature" in d.kinds()
    # everything else is derived as before, and a plain Device joins no typed list
    assert (d.lights, d.sockets, d.buttons) == (
        plain.lights,
        plain.sockets,
        plain.buttons,
    )
    assert set(d.by_address) == set(plain.by_address) | {LIGHT_2G + 1}

    # the table is ordered: the first rule to claim an element wins, and no rules means no devices
    everything = ElementRule(
        "any",
        lambda node, element, pid: True,
        lambda ctx, node, element: Device(
            element.address, ctx.unique_id(node, element), "x", node, kind="any"
        ),
    )
    first = build_devices(cdb, rules=[everything, *ELEMENT_RULES])
    assert first.kinds() == {"any"}
    assert (first.lights, first.sockets, first.buttons) == ([], [], [])
    assert len(first.by_address) == sum(
        len(n.elements) for n in cdb.nodes if n.pid is not None
    )
    assert build_devices(cdb, rules=[]).by_address == {}


# ----------------------------------------------------------------------------- share-export metadata


def test_metadata_from_export_uses_the_share_file_meta_block():
    cdb = CDB.load(FIXTURES / "JungHome.json")

    m = Metadata.from_export(cdb.export_meta)
    assert m.devices == {UUID_1G: [([1], "WC mirror (share)")]}
    assert m.scenes == {1: "WC off (share)"}
    d = build_devices(cdb, m)
    assert d.by_address.get(PROXY_NODE).name == "WC mirror (share)"  # type: ignore[union-attr]
    assert d.by_address.get(LIGHT_2G).name == "Push-button 2-gang 0232"  # type: ignore[union-attr]
    assert d.scenes == [SceneDef(1, "WC off (share)"), SceneDef(2, "Scene #2")]


def test_metadata_from_export_tolerates_missing_and_malformed_entries():
    assert Metadata.from_export(None).devices == {}
    assert Metadata.from_export({}).scenes == {}
    m = Metadata.from_export(
        {
            "devices": [
                {"name": "no device id"},
                {
                    "deviceId": {"nodeId": "abc-def", "locationIds": ["2", 1]},
                    "name": "Y",
                },
                {"deviceId": {"nodeId": "n"}},  # no name
                {"deviceId": {"locationIds": [1]}, "name": "no node"},
                {"deviceId": {"nodeId": "abc-def"}, "name": "Z"},  # no locations
            ],
            "scenes": [
                {"number": "7", "name": "Seven"},
                {"name": "no number"},
                {"number": "x", "name": "bad"},
                {"number": None, "name": "none"},
                {"number": 8, "name": "Eight"},
            ],
        }
    )
    assert m.devices == {"ABC-DEF": [([1, 2], "Y"), ([], "Z")]}
    assert m.scenes == {7: "Seven", 8: "Eight"}
    assert m.name_for("abc-def", 2) == "Y"  # node ids are matched case-insensitively
    assert m.name_for("ABC-DEF", 3) is None


def test_metadata_node_ids_are_case_insensitive(tmp_path: Path):
    p = tmp_path / "device_metadata.json"
    p.write_text(
        json.dumps([{"nodeId": UUID_1G.lower(), "locationIds": [1]}, {"name": "Lower"}])
    )
    m = Metadata(p)
    assert m.devices == {UUID_1G: [([1], "Lower")]}
    assert m.name_for(UUID_1G, 1) == "Lower"
    assert m.name_for(UUID_1G.lower(), 1) == "Lower"


# ----------------------------------------------------------------------------- scene names (P2-20)


def test_scene_names_fall_back_to_the_cdb_then_to_scene_n(cdb: CDB):
    """App metadata first, the CDB's own `scenes[].name` second (a raw `MeshNetwork.json` or the gateway's CDB
    carries the user's names there), "Scene N" only when both are missing."""
    meta = Metadata()
    meta.scenes[1] = "From the app"
    cdb.scene_names[2] = ""  # an unnamed CDB scene
    assert build_devices(cdb, meta).scenes == [
        SceneDef(1, "From the app"),
        SceneDef(2, "Scene 2"),
    ]
    assert build_devices(cdb).scenes == [
        SceneDef(1, "Scene #1"),
        SceneDef(2, "Scene 2"),
    ]


def test_timer_scenes_are_flagged(cdb: CDB):
    """The scene a SIG timer recalls is named `TimerScene <index> <deviceId>` by the app, which leaves it out of
    its scene lists: flagged, still listed (it holds a register slot and a recall of it is still a recall)."""
    meta = Metadata()
    meta.scenes[1] = "TimerScene 0 DeviceIdentifier(nodeId=1)"
    meta.scenes[2] = "timerscene 1"  # the app's test is case-sensitive
    assert build_devices(cdb, meta).scenes == [
        SceneDef(1, "TimerScene 0 DeviceIdentifier(nodeId=1)", timer=True),
        SceneDef(2, "timerscene 1"),
    ]


# ----------------------------------------------------------------------------- null meta lists (P2-21)


def test_meta_list_reads_null_and_non_lists_as_empty():
    assert meta_list(None) == []
    assert meta_list({"a": 1}) == []
    assert meta_list("x") == []
    assert meta_list([1]) == [1]


def test_metadata_from_export_tolerates_null_lists_and_odd_entries():
    """A share export with `null` where the schema has a list, or entries of the wrong shape, loads (the names are
    cosmetic; the app accepts the file)."""
    assert Metadata.from_export({"devices": None, "scenes": None}).devices == {}
    m = Metadata.from_export(
        {
            "devices": [
                None,
                "x",
                {"deviceId": "not an object", "name": "Y"},
                {
                    "deviceId": {"nodeId": "n", "locationIds": None},
                    "name": "No locations",
                },
                {
                    "deviceId": {"nodeId": "n", "locationIds": ["a"]},
                    "name": "Bad locations",
                },
                {
                    "deviceId": {"nodeId": "n", "locationIds": [None]},
                    "name": "None location",
                },
                {
                    "deviceId": {"nodeId": "n", "locationIds": {"1": 1}},
                    "name": "Dict locations",
                },
            ],
            "scenes": [None, "x", [], {"number": 3, "name": "Three"}],
        }
    )
    assert m.devices == {"N": [([], "No locations"), ([], "Dict locations")]}
    assert m.scenes == {3: "Three"}


# ----------------------------------------------------------------------------- app-container metadata validation


def _metadata_file(tmp_path: Path, name: str, content: object) -> Path:
    p = tmp_path / name
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    return p


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("not json", "cannot be read as JSON"),
        ({"nodeId": "N"}, "is not a list of key/value pairs"),
        ([{"nodeId": "N", "locationIds": [1]}], "is not a list of key/value pairs"),
        (["N", {"name": "x"}], "not a key/value pair"),
        ([{"nodeId": "N", "locationIds": [1]}, "x"], "not a key/value pair"),
        (
            [{"locationIds": [1]}, {"name": "x"}],
            "without a nodeId, locationIds or name",
        ),
        ([{"nodeId": None, "locationIds": [1]}, {"name": "x"}], "without a nodeId"),
        ([{"nodeId": "N", "locationIds": ["1"]}, {"name": "x"}], "without a nodeId"),
        ([{"nodeId": "N", "locationIds": [True]}, {"name": "x"}], "without a nodeId"),
        ([{"nodeId": "N", "locationIds": 1}, {"name": "x"}], "without a nodeId"),
        ([{"nodeId": "N", "locationIds": [1]}, {"name": None}], "without a nodeId"),
        ([{"nodeId": "N", "locationIds": [1]}, {}], "without a nodeId"),
    ],
    ids=[
        "not_json",
        "object",
        "odd_length",
        "key_not_object",
        "value_not_object",
        "no_node_id",
        "node_id_null",
        "location_str",
        "location_bool",
        "locations_int",
        "name_null",
        "no_name",
    ],
)
def test_malformed_device_metadata_is_reported_by_its_own_path(
    tmp_path: Path, content: object, problem: str
):
    """A drifted / half-synced `device_metadata.json` raises `InvalidMetadata` (a ValueError, so the callers'
    load-error handling catches it) whose message starts with the metadata file's path, never the export's."""
    p = _metadata_file(tmp_path, "device_metadata.json", content)
    with pytest.raises(InvalidMetadata, match=problem) as info:
        Metadata(p)
    assert str(info.value).startswith(str(p))
    assert info.value.path == p
    assert isinstance(info.value, ValueError)


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (["1", {"name": "x"}], "without a number or name"),
        ([True, {"name": "x"}], "without a number or name"),
        ([1, "x"], "without a number or name"),
        ([1, {"name": 5}], "without a number or name"),
        ([1, {"icon": "SceneDay"}], "without a number or name"),
        ([1], "is not a list of key/value pairs"),
    ],
    ids=["number_str", "number_bool", "value_str", "name_int", "no_name", "odd_length"],
)
def test_malformed_scene_metadata_is_reported_by_its_own_path(
    tmp_path: Path, content: object, problem: str
):
    p = _metadata_file(tmp_path, "scene_metadata.json", content)
    with pytest.raises(InvalidMetadata, match=problem) as info:
        Metadata(None, p)
    assert info.value.path == p


def test_unreadable_metadata_file_is_reported_by_its_own_path(tmp_path: Path):
    p = _metadata_file(tmp_path, "device_metadata.json", [])
    p.chmod(0)
    if os.access(p, os.R_OK):
        pytest.skip("running as root: the file stays readable")
    with pytest.raises(InvalidMetadata, match="cannot be read as JSON"):
        Metadata(p)


def test_deeply_nested_metadata_is_reported_not_a_recursion_error(tmp_path: Path):
    p = tmp_path / "device_metadata.json"
    p.write_text("[" * 1_000_000 + "]" * 1_000_000)
    with pytest.raises(InvalidMetadata, match="nested too deeply"):
        on_small_stack(lambda: Metadata(p))


def test_metadata_node_ids_match_across_the_dashed_and_undashed_forms(tmp_path: Path):
    """The iOS container writes dashed ids; a CDB from an older library may be undashed — both are one node."""
    p = _metadata_file(
        tmp_path,
        "device_metadata.json",
        [
            {"nodeId": UUID_1G.replace("-", "").lower(), "locationIds": [1]},
            {"name": "Undashed"},
        ],
    )
    m = Metadata(p)
    assert m.devices == {UUID_1G: [([1], "Undashed")]}
    assert m.name_for(UUID_1G, 1) == "Undashed"
    assert m.name_for(UUID_1G.replace("-", ""), 1) == "Undashed"
    m = Metadata.from_export(
        {
            "devices": [
                {
                    "deviceId": {
                        "nodeId": UUID_1G.replace("-", ""),
                        "locationIds": [1],
                    },
                    "name": "Share",
                }
            ]
        }
    )
    assert m.name_for(UUID_1G, 1) == "Share"


def test_an_undashed_node_uuid_keeps_the_unique_ids_and_names(meta: Metadata):
    """`unique_id` and the app names are derived from the canonical UUID: an undashed CDB changes nothing."""
    net = json.loads((FIXTURES / "MeshNetwork.json").read_text())["meshNetwork"]
    for node in net["nodes"]:
        node["UUID"] = node["UUID"].replace("-", "").lower()
    d = build_devices(CDB.from_network(net), meta)
    light = d.by_address[PROXY_NODE]
    assert light.unique_id == UUID_1G.lower() + "-0001"
    assert light.name == "WC mirror"


# ----------------------------------------------------------------------------- virtual groups are not rooms


LABEL = "0073E7E4D8B9440FAF8415DF4C56C0E1"  # virtual address 0xB529 (§3.4.2.3 sample)


def test_virtual_groups_and_targets_are_parsed_but_never_rooms_or_targets(
    meta: Metadata,
):
    """A virtual group (a Label UUID) subscribes and publishes like any group in the file, but nothing here can
    address it (no label in the upper-transport crypto), so it is neither a room nor a detector's target."""
    net = json.loads((FIXTURES / "MeshNetwork.json").read_text())["meshNetwork"]
    net["groups"].append(
        {"address": LABEL, "name": "Virtual room", "parentAddress": "0000"}
    )
    light = next(n for n in net["nodes"] if n["unicastAddress"] == f"{PROXY_NODE:04X}")
    onoff = next(m for m in light["elements"][0]["models"] if m["modelId"] == "1000")
    onoff["subscribe"].append(LABEL)
    onoff["publish"] = {"address": LABEL, "index": 0, "ttl": 255}
    cdb = CDB.from_network(net)
    d = build_devices(cdb, meta)
    assert 0xB529 in cdb.groups
    assert 0xB529 not in d.rooms
    assert d.by_address[PROXY_NODE].rooms == ["WC"]
    element = cdb.element(PROXY_NODE)
    assert element is not None
    assert 0xB529 in element.subscriptions("1000")
    assert BuildContext.publish_address(element, "1000") is None


def test_time_keeper_group_is_not_a_room():
    """MOD-10: one definition of a room (`is_room`): the time-keeper group is never one, in `Devices.rooms` or
    in a load's rooms."""
    raw = json.loads(CDB_PATH.read_text())
    net = raw["meshNetwork"]
    net["groups"].append(
        {"name": "#time_keeper_group#", "address": "FEFF", "parentAddress": "0000"}
    )
    node = next(n for n in net["nodes"] if n["unicastAddress"] == "0148")
    model = next(m for m in node["elements"][0]["models"] if m["modelId"] == "1000")
    model["subscribe"].append("FEFF")
    devices = build_devices(CDB.from_network(net))
    assert 0xFEFF not in devices.rooms
    light = devices.by_address[0x0148]
    assert "#time_keeper_group#" not in light.rooms


# ----------------------------------------------------------------------------- key connections


def _publish(element: Element, model: str, address: str) -> None:
    """Point the `model` publication of `element` at `address` (a hex string, as the CDB writes it)."""
    for raw in element.raw_models:
        if raw["modelId"] == model:
            raw["publish"] = {"address": address}
            return
    raise AssertionError(f"{element.address:04X} has no {model}")


def _unpublish(element: Element) -> None:
    for raw in element.raw_models:
        raw.pop("publish", None)


def _connection(
    cdb: CDB, key: int, meta: Metadata | None = None
) -> KeyConnection | None:
    devices = build_devices(cdb, meta)
    button = devices.by_address[key]
    assert isinstance(button, Button)
    return button.connection


def test_key_connections_of_the_fixture_network(meta: Metadata):
    devices = build_devices(CDB.load(CDB_PATH), meta)
    by_key = {b.address: b.connection for b in devices.buttons}
    assert by_key == {
        0x0149: KeyConnection("gateway", 0xC005, 0x00DC),  # the gateway's element group
        0x0234: KeyConnection("device", 0xC044, LIGHT_2G, "Living room DALI"),
        0x0235: KeyConnection("device", 0xC044, LIGHT_2G, "Living room DALI"),
        0x0301: KeyConnection("device", 0xC070, DIMMER, "WC ceiling"),
    }


def test_room_link_names_the_room_the_app_recorded():
    cdb = CDB.load(CDB_PATH)
    key = cdb.element(0x0149)
    assert key is not None
    for model in ("1001", "05271015"):
        _publish(
            key, model, "C062"
        )  # its own element group: the room's loads subscribe to it
    meta = Metadata()
    assert _connection(cdb, 0x0149, meta) == KeyConnection(
        "room", 0xC062
    )  # no record: which room is unknown
    meta.room_links[0x0149] = GROUP_WC
    assert _connection(cdb, 0x0149, meta) == KeyConnection(
        "room", 0xC062, GROUP_WC, "WC"
    )


def test_scene_group_device_and_no_connection():
    cdb = CDB.load(CDB_PATH)
    key = cdb.element(0x0149)
    assert key is not None
    _unpublish(key)
    assert _connection(cdb, 0x0149) is None  # a cleared key
    _publish(key, "1205", "FFFF")
    meta = Metadata()
    assert _connection(cdb, 0x0149, meta) == KeyConnection("scene", 0xFFFF)
    meta.key_scenes[0x0149] = 1
    scene = _connection(cdb, 0x0149, meta)
    assert scene is not None
    assert (scene.kind, scene.scene) == ("scene", 1)
    assert scene.name == build_devices(cdb, meta).scenes[0].name
    _unpublish(key)
    _publish(
        key, "1001", "C011"
    )  # straight to a room group: not how the app wires a key
    assert _connection(cdb, 0x0149) == KeyConnection(
        "group", GROUP_KITCHEN, name="Kitchen"
    )
    _publish(
        key, "1001", "C04E"
    )  # the element group of the DALI insert's temperature element: no device
    assert _connection(cdb, 0x0149) == KeyConnection("device", 0xC04E, 0x0233)


def test_a_property_mode_key_is_a_lock_once_its_values_are_known():
    """`GetConnection` (network-logic.md §2.7): a key publishing from its LBC User Property client alone is in
    property mode, which the export cannot tell apart further; its 0x5006 / 0x5007 make it a lock link."""
    cdb = CDB.load(CDB_PATH)
    key = cdb.element(0x0234)
    assert key is not None
    _unpublish(key)
    _publish(
        key, "05271015", "C061"
    )  # the vendor client alone, to the 1-gang light's element group
    device = _connection(cdb, 0x0234)
    assert device == KeyConnection(
        "device", 0xC061, 0x0148, "Push-button 1-gang 0148", property_mode=True
    )
    assert with_key_lock(device, {}) is device  # nothing read yet
    values = {0x5006: bytes.fromhex("090001"), 0x5007: bytes.fromhex("02013c00")}
    lock = with_key_lock(device, values)
    assert lock is not None
    assert (lock.kind, lock.address, lock.target, lock.name, lock.lock) == (
        "lock",
        0xC061,
        0x0148,
        "Push-button 1-gang 0148",
        EnforcedOutput(2, 1, 60),
    )
    assert with_key_lock(device, {**values, 0x5003: b"\x05"}) is device
    _publish(
        key, "05271015", "C04F"
    )  # its own element group: a room link in property mode
    room = _connection(cdb, 0x0234)
    assert room is not None
    assert room.property_mode
    assert getattr(with_key_lock(room, values), "kind", None) == "lock"
    _publish(
        key, "1001", "C04F"
    )  # the OnOff client publishes too: light or switch mode
    light = _connection(cdb, 0x0234)
    assert light == KeyConnection("room", 0xC04F)
    assert with_key_lock(light, values) is light
    assert with_key_lock(None, values) is None
    _unpublish(key)
    _publish(
        key, "05271015", "C005"
    )  # the gateway's group: a gateway link, never a lock
    gateway = _connection(cdb, 0x0234)
    assert gateway == KeyConnection("gateway", 0xC005, 0x00DC)
    assert with_key_lock(gateway, values) is gateway


def test_metadata_reads_room_links_and_scene_keys_best_effort(tmp_path: Path):
    meta = Metadata.from_export(
        {
            "devices": [
                {"cachedGroupConnectionMetadata": [
                    {"elementAddress": 0x0149, "groupAddress": "C00F", "publishAddress": 0xC062, "function": "LIGHT"},
                    {"elementAddress": "zz", "groupAddress": 0xC010},
                    "stray",
                ]},
                {"cachedGroupConnectionMetadata": None},
                "stray",
            ],
            "keyModeSceneConfigExports": [
                {"elementAddress": 0x0234, "sceneConfig": {"sceneId": 5}},
                {"elementAddress": 0x0235, "sceneConfig": None},
                {"elementAddress": 0x0301},
                "stray",
            ],
        }
    )  # fmt: skip
    assert meta.room_links == {0x0149: 0xC00F}
    assert meta.key_scenes == {0x0234: 5}
    # the iOS app container keeps the room links next to each device's name
    path = tmp_path / "device_metadata.json"
    path.write_text(
        json.dumps(
            [
                {"nodeId": UUID_1G, "locationIds": [64]},
                {"name": "WC mirror button", "cachedGroupConnectionMetadata": [
                    {"elementAddress": 0x0149, "groupAddress": 0xC00F},
                ]},
            ]
        )
    )  # fmt: skip
    assert Metadata(path).room_links == {0x0149: 0xC00F}


# ----------------------------------------------------------------------------- the other fixture networks


def test_blinds_detectors_and_thermostats_of_the_other_fixture_networks():
    """The kinds the main fixture lacks, each from its own generated network (`tests/fixtures/make_*fixture.py`):
    blinds (position + slat element, a puck without slats), detectors (the Sensor Server element of a detector
    product, its relay a light) and the room thermostat (set point, heating demand and temperature on one element).
    """
    blinds = build_devices(CDB.load(FIXTURES / "Blinds.json"))
    assert [(b.address, b.slat_address, b.level_elements) for b in blinds.blinds] == [
        (0x0500, 0x0501, (0x0500, 0x0501)),
        (0x0600, 0x0601, (0x0600, 0x0601)),
        (0x0700, None, (0x0700,)),
    ]
    assert all(isinstance(blinds.by_address[b.address], Blind) for b in blinds.blinds)

    detectors = build_devices(CDB.load(FIXTURES / "MeshNetwork-detectors.json"))
    assert [(d.address, d.relay_address, d.presence) for d in detectors.detectors] == [
        (0x0501, 0x0500, False),
        (0x0511, 0x0510, True),
    ]
    assert all(
        isinstance(detectors.by_address[d.address], Detector)
        for d in detectors.detectors
    )
    assert [light.address for light in detectors.lights] == [0x0500, 0x0510]
    assert detectors.kinds() == {"switch", "button", "detector"}

    rtr = build_devices(CDB.load(FIXTURES / "MeshNetwork-rtr.json"))
    (thermostat,) = rtr.thermostats
    assert (
        thermostat.address,
        thermostat.onoff_address,
        thermostat.sensor_address,
        thermostat.kind,
    ) == (0x0500, 0x0500, 0x0500, "thermostat")
    assert isinstance(rtr.by_address[0x0500], Thermostat)
    assert 0x0500 not in {light.address for light in rtr.lights}


def test_loads_a_room_thermostat_switches(tmp_path: Path):
    """The app's RTR -> actuator link (`SetMultiConnection`): a load whose OnOff server subscribes to the element
    group the thermostat's OnOff client publishes to is controlled by it; an RTR element without the client, an
    element without a group and a load listening elsewhere are not."""
    raw = json.loads((FIXTURES / "MeshNetwork-rtr.json").read_text())
    for node in raw["meshNetwork"]["nodes"]:
        for element in node["elements"]:
            for model in element["models"]:
                # actuator output 1 (0x0400) and the socket (0x0172) are driven by the thermostat's group 0xC090
                if model["modelId"] == "1000" and node["unicastAddress"] in (
                    "0400",
                    "0172",
                ):
                    if element["index"] == 0:
                        model["subscribe"].append("C090")
        if node["unicastAddress"] == "0500":
            # a second RTR element, with the client but no element group of its own
            node["elements"].append(
                {
                    "name": "Element 2",
                    "index": 1,
                    "location": "0002",
                    "models": [
                        {"modelId": "1001", "bind": [0], "subscribe": []},
                    ],
                }
            )
    path = tmp_path / "rtr-links.json"
    path.write_text(json.dumps(raw))
    devices = build_devices(CDB.load(path))
    (rtr,) = devices.thermostats
    assert devices.thermostats_of == {0x0400: [rtr], 0x0172: [rtr]}

    plain = build_devices(CDB.load(FIXTURES / "MeshNetwork-rtr.json"))
    assert plain.thermostats_of == {}


def test_the_energy_puck_output_is_a_metered_light():
    """The puck (0x0010, `MeshNetwork-puck.json`): its output a switched light measured by the node's meter element.

    The meter (the Sensor Server element 0613) is no device of its own; the inputs stay E1 / E2 (review-3 F3).
    """
    d = build_devices(CDB.load(FIXTURES / "MeshNetwork-puck.json"))
    puck = d.by_address[0x0610]
    assert isinstance(puck, Light)
    assert (puck.kind, puck.meter_address) == ("switch", 0x0613)
    assert d.metered == [d.by_address[SOCKET], puck]  # sockets first
    assert d.by_meter(0x0613) is puck
    assert d.by_meter(SENSOR) is d.by_address[SOCKET]
    assert 0x0613 not in d.by_address
    assert [(b.address, b.key) for b in d.buttons if b.node is puck.node] == [
        (0x0611, "E1"),
        (0x0612, "E2"),
    ]
    meter = meter_element(puck.node)
    assert meter is not None
    assert (meter.address, meter.location) == (0x0613, 0x0042)
    assert [light.address for light in d.lights if light.meter_address] == [0x0610]


def test_a_sensor_server_is_no_meter_on_detectors_and_thermostats():
    """A detector's Sensor Server is the detector, an RTR's its room temperature: their loads are not metered."""
    detectors = build_devices(CDB.load(FIXTURES / "MeshNetwork-detectors.json"))
    assert detectors.lights
    assert all(light.meter_address is None for light in detectors.lights)
    assert detectors.metered == []
    rtr = CDB.load(FIXTURES / "MeshNetwork-rtr.json")
    thermostat = rtr.node_by_addr(0x0500)
    assert thermostat is not None
    assert "1100" in thermostat.elements[0].models
    assert meter_element(thermostat) is None
    assert [load.address for load in build_devices(rtr).metered] == [SOCKET]


def test_the_meter_measures_the_load_on_the_primary_element_only(cdb: CDB):
    """A node's one meter is not shared: only the load on its primary element gets it (a 2-gang node's second is not)."""
    actuator = cdb.node_by_addr(ACTUATOR)
    assert actuator is not None
    actuator.elements.append(Element(ACTUATOR + 2, 0x0040, ["1001", "1100"], actuator))
    d = build_devices(cdb)
    first, other = d.by_address[ACTUATOR], d.by_address[ACTUATOR + 1]
    assert isinstance(first, Light)
    assert isinstance(other, Light)
    assert (first.meter_address, other.meter_address) == (ACTUATOR + 2, None)
    assert d.by_meter(ACTUATOR + 2) is first


def test_a_sensor_server_on_a_load_element_is_no_meter(cdb: CDB):
    """A Sensor Server below the key locations (on the primary or another load's element) meters nothing."""
    actuator = cdb.node_by_addr(ACTUATOR)
    assert actuator is not None
    for element in actuator.elements:
        element.models.append("1100")
        assert meter_element(actuator) is None
    d = build_devices(cdb)
    loads = [d.by_address[a] for a in (ACTUATOR, ACTUATOR + 1)]
    assert all(isinstance(li, Light) and li.meter_address is None for li in loads)
    assert [load.address for load in d.metered] == [SOCKET]


def test_publish_address_of_a_malformed_four_digit_address_is_none(cdb: CDB):
    """Four characters that are not hex (a hand-edited or damaged export) are no target, like a virtual label."""
    element = cdb.element(PROXY_NODE)
    assert element is not None
    _publish(element, "1000", "ZZZZ")
    assert BuildContext.publish_address(element, "1000") is None


# ----------------------------------------------------------------------------- insert id, RTR, excluded nodes


def _push_button(
    insert: int | None, *load_models: list[str], pid: int = 0x0002
) -> Node:
    """A push-button at 0x0900 with one element per `load_models` (location 0x0001) and a key at 0x0040."""
    node = Node(
        canonical_uuid("9" * 32),
        "Push-button",
        0x0900,
        bytes(16),
        pid,
        insert_function=insert,
    )
    node.elements = [
        Element(0x0900 + i, 0x0001, models, node)
        for i, models in enumerate(load_models)
    ]
    node.elements.append(Element(0x0900 + len(load_models), 0x0040, ["1001"], node))
    return node


def test_a_blinds_insert_on_a_push_button_is_a_blind_next_to_lamp_servers(cdb: CDB):
    """The app's cached InsertId says what a push-button drives: a blinds insert is a blind even where its level
    elements host lamp servers, which the composition alone takes for a light."""
    node = _push_button(5, ["1000", "1002"], ["1000", "1002"])
    cdb.nodes.append(node)
    d = build_devices(cdb)
    blind = d.by_address.get(0x0900)
    assert isinstance(blind, Blind)
    assert blind.slat_address == 0x0901
    assert d.by_address.get(0x0901) is None
    assert not [light for light in d.lights if light.node is node]
    assert (load_kind(node.elements[0]), load_kind(node.elements[1])) == (
        "blind",
        "blind",
    )


def test_without_a_known_insert_the_composition_decides(cdb: CDB):
    for insert in (None, 0xFFFF, 8):  # not read, unset, nonsense on a push-button
        node = _push_button(insert, ["1000", "1002"], ["1002"])
        cdb.nodes = [n for n in cdb.nodes if n.uuid != node.uuid] + [node]
        d = build_devices(cdb)
        assert isinstance(d.by_address.get(0x0900), Light)
        blind = d.by_address.get(0x0901)
        assert isinstance(blind, Blind)
        assert blind.slat_address is None


def test_a_lamp_insert_is_never_a_blind_and_no_insert_is_no_load(cdb: CDB):
    lamp = _push_button(
        2, ["1000", "1300"], ["1002"]
    )  # a level element alone would pass for a blind
    cdb.nodes.append(lamp)
    d = build_devices(cdb)
    assert isinstance(d.by_address.get(0x0900), Light)
    assert d.by_address.get(0x0901) is None
    assert load_kind(lamp.elements[1]) is None
    for insert in (
        6,
        7,
    ):  # extension (satellite), not available: the firmware's servers drive nothing
        cdb.nodes[-1] = _push_button(insert, ["1000"], ["1002"])
        d = build_devices(cdb)
        assert d.by_address.get(0x0900) is None
        assert d.by_address.get(0x0901) is None
        assert isinstance(d.by_address.get(0x0902), Button)


def test_the_insert_comes_from_the_export_then_from_what_the_node_reported(cdb: CDB):
    """F4-12: the export's InsertId decides; where it has none (or one the app writes unread), the node's own report
    (its advertisement or an InsertId Get, `Node.reported_function`) does; neither leaves it to the composition."""
    node = _push_button(None, ["1000", "1002"], ["1000", "1002"])
    cdb.nodes.append(node)
    node.reported_function = 5  # it advertises a blinds insert
    assert insert_function(node) == 5
    assert isinstance(build_devices(cdb).by_address.get(0x0900), Blind)
    for exported in (None, 0xFFFF, 8):  # not cached, unset, nonsense on a push-button
        node.insert_function = exported
        assert insert_function(node) == 5
    node.insert_function = 2  # the export's dimming insert wins over the report
    assert insert_function(node) == 2
    assert isinstance(build_devices(cdb).by_address.get(0x0900), Light)
    node.insert_function, node.reported_function = None, 0xFFFF
    assert insert_function(node) is None
    # another product: no insert, whatever it reports
    assert pick_insert(0x0003, 0, 0) is None
    assert pick_insert(0x0001, None, None) is None
    assert pick_insert(0x0001, 6, 7) == 6


def test_an_insert_mismatch_is_a_push_button_whose_report_differs_from_its_export():
    assert insert_mismatch(0x0002, 4, 5)
    assert not insert_mismatch(0x0002, 4, 4)
    assert not insert_mismatch(0x0002, None, 5)  # nothing cached: nothing swapped
    assert not insert_mismatch(0x0002, 4, 0xFFFF)  # an unset report says nothing
    assert not insert_mismatch(0x0002, 0xFFFF, 5)
    assert not insert_mismatch(0x0003, 0, 5)  # a socket has no insert to swap


@pytest.mark.parametrize(
    ("layout", "positions"),
    [
        (0, {0x40: "top", 0x41: "bottom"}),
        (1, {0x40: "rocker", 0x41: None}),
        (
            2,
            {
                0x40: "left_top",
                0x41: "left_bottom",
                0x42: "right_top",
                0x43: "right_bottom",
            },
        ),
        (3, {0x40: "left_rocker", 0x41: None, 0x42: "right_top", 0x43: "right_bottom"}),
        (4, {0x40: "left_top", 0x41: "left_bottom", 0x42: "right_rocker", 0x43: None}),
        (5, {0x40: "left_rocker", 0x41: None, 0x42: "right_rocker"}),
        (0xFF, {0x40: None}),  # unknown
        (None, {0x40: None}),
    ],
)
def test_key_positions_follow_the_button_layout(
    layout: int | None, positions: dict[int, str | None]
):
    for location, position in positions.items():
        assert key_position(layout, location) == position


@pytest.mark.parametrize(
    ("pid", "function", "expected"),
    [
        (0x0003, 0, 1),  # metering socket: one device
        (0x000A, 8, 1),  # RTR
        (0x0005, None, 1),  # wall transmitter
        (0x0002, 6, 1),  # push-button with an extension insert: the keys alone
        (0x0002, 1, 3),  # 2-gang switch: two loads and the keys
        (0x0011, 3, 3),
        (0x0001, 0, 2),  # switch, dimming, DALI, blinds: the load and the keys
        (0x0004, 2, 2),
        (0x0002, 4, 2),
        (0x000D, 5, 2),
        (
            0x0002,
            7,
            None,
        ),  # not available, unset, unknown: the app's own error, no count here
        (0x0002, 0xFFFF, None),
        (0x0001, None, None),
    ],
)
def test_expected_device_count_is_the_apps_missing_devices_table(
    pid: int, function: int | None, expected: int | None
):
    assert expected_device_count(pid, function) == expected


def test_an_insert_id_on_another_product_changes_nothing(cdb: CDB):
    """Only a push-button takes different inserts; a socket or mini actuator is what its product id says."""
    socket = cdb.element(SOCKET)
    assert socket is not None
    socket.node.insert_function = 5
    assert isinstance(build_devices(cdb).by_address.get(SOCKET), Socket)


def test_an_rtr_onoff_server_on_another_element_is_no_light(cdb: CDB):
    """The RTR's heating demand (Generic OnOff server) is its thermostat's, not a phantom light, wherever it sits."""
    rtr = Node("8" * 32, "RTR", 0x0A00, bytes(16), next(iter(THERMOSTAT_PIDS)))
    rtr.elements = [
        Element(0x0A00, 0x0001, ["1002"], rtr),
        Element(0x0A01, 0x0001, ["1000"], rtr),
        Element(0x0A02, 0x0002, ["1000", "1300"], rtr),
    ]
    cdb.nodes.append(rtr)
    d = build_devices(cdb)
    assert [t.onoff_address for t in d.thermostats if t.node is rtr] == [0x0A01]
    assert not [light for light in d.lights if light.node is rtr]
    assert {load_kind(e) for e in rtr.elements} == {"thermostat"}


def test_excluded_and_foreign_nodes_are_no_devices():
    net = json.loads(CDB_PATH.read_text())["meshNetwork"]
    excluded = next(n for n in net["nodes"] if n.get("pid") == "0003")  # the socket
    excluded["excluded"] = True
    foreign = next(
        n for n in net["nodes"] if n.get("pid") == "0010"
    )  # the 2-channel actuator
    foreign["cid"] = "0059"
    d = build_devices(CDB.from_network(net))
    units = {int(n["unicastAddress"], 16) for n in (excluded, foreign)}
    assert not [dev for dev in d.by_address.values() if dev.node.unicast in units]
    assert d.lights  # everything else is still there
    assert not d.sockets


def test_metadata_reads_the_ios_insert_ids(tmp_path: Path, cdb: CDB):
    """The iOS app container keys every device by its InsertId (`actuatorFunction`); a load-side entry wins, and
    `build_devices` applies it to a node the export's own `meta` said nothing about."""
    p = tmp_path / "device_metadata.json"
    node = _push_button(None, ["1000", "1002"], ["1000", "1002"])
    uuid = node.uuid

    def key(locations: list[int], function: object) -> dict:
        return {
            "nodeId": uuid,
            "locationIds": locations,
            "actuatorFunction": {"actuatorFunctionId": function, "insertType": 2},
        }

    p.write_text(
        json.dumps(
            [
                key([64], 7),
                {"name": "keys"},
                key([1], 5),
                {"name": "shutter"},
                key([65], 6),
                {"name": "more keys"},
                key([2], True),
                {"name": "junk"},
                {"nodeId": "N", "locationIds": [1]},
                {"name": "no insert id"},
            ]
        )
    )
    meta = Metadata(p)
    assert meta.insert_functions == {uuid: 5}
    cdb.nodes.append(node)
    assert isinstance(build_devices(cdb, meta).by_address.get(0x0900), Blind)
    assert node.insert_function == 5
    node.insert_function = (
        0  # the export's own `meta` said so: the app container does not override it
    )
    assert isinstance(build_devices(cdb, meta).by_address.get(0x0900), Light)


def test_room_members_are_the_loads_listening_to_the_room() -> None:
    """The app's area filter (`DeviceFilter.Group`): per room, the lights, sockets, blinds and thermostats in it."""
    devices = build_devices(CDB.load(FIXTURES / "MeshNetwork-rtr.json"))
    members = {
        devices.rooms[room]: [(d.kind, d.address) for d in loads]
        for room, loads in devices.room_members.items()
    }
    assert members == {
        "WC": [("switch", PROXY_NODE), ("dimmer", DIMMER)],
        "Living room": [("ctl", LIGHT_2G), ("thermostat", 0x0500)],
        "Kitchen": [("switch", ACTUATOR), ("switch", ACTUATOR + 1), ("socket", SOCKET)],
    }
    blinds = build_devices(CDB.load(FIXTURES / "Blinds.json"))
    assert [(d.kind, d.address) for d in blinds.room_members[GROUP_KITCHEN]][-1] == (
        "blind",
        0x0500,
    )
    # a room nothing listens to has no members
    cdb = CDB.load(CDB_PATH)
    cdb.groups[0xC020] = "Attic"
    assert 0xC020 not in build_devices(cdb).room_members


def test_time_keeper_candidates(cdb: CDB):
    """Review-4 F4-14: every JUNG node with a Time Server and its Setup Server may keep the PP2 pucks' time, but the
    gateway and the battery devices (the app never chooses those), and a phone has no product."""
    assert [n.unicast for n in time_keeper_candidates(cdb)] == [
        0x0148,
        0x0232,
        0x0172,
        0x0300,
        0x0400,
    ]
    battery = cdb.node_by_addr(0x0300)
    assert battery is not None
    battery.pid = 0x0005  # a wall transmitter
    gateway = cdb.node_by_addr(0x0148)
    assert gateway is not None
    gateway.pid = 0x000B
    assert [n.unicast for n in time_keeper_candidates(cdb)] == [0x0232, 0x0172, 0x0400]
