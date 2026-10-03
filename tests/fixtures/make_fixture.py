"""Generate a small SYNTHETIC JUNG HOME export (fake keys) for the test-suite. Never use real keys here."""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
NETKEY = "00112233445566778899aabbccddeeff"
APPKEY = "ffeeddccbbaa99887766554433221100"
# The provisioning phone: a fixed fake UUID (the app draws a random one per installation).
PROVISIONER_UUID = "00000001-0000-4000-8000-000000000001"


def node_uuid(octet: int) -> str:
    """UUID of a synthetic JUNG node: the EUI-64 of a MAC from the IANA documentation block `00:00:5E:00:53:xx`.

    The shape is the real one (`jhmesh.advert.mac_from_uuid`: the MAC with `FFFE` in the middle, zeros behind),
    the block is reserved for documentation (RFC 7042 §2.1.2), so no fixture carries a real device's address.
    The last octet is unique across ALL the synthetic networks (a device registry keyed on the UUID must never
    merge two of them) and echoes the unicast where it can (`0x0148` → `:14`).
    """
    return f"00005EFF-FE00-53{octet:02X}-0000-000000000000"


def node_mac(octet: int) -> str:
    """The MAC `node_uuid(octet)` encodes, in the `meta.devices[].macAddress` form."""
    return f"00:00:5E:00:53:{octet:02X}"


# the main network (`MeshNetwork.json`, shared by the derived fixtures)
GATEWAY, PUSH_BUTTON_1G, SOCKET, PUSH_BUTTON_2G, DIMMER, ACTUATOR = (
    0x0D,
    0x14,
    0x17,
    0x23,
    0x30,
    0x40,
)
THERMOSTAT = 0x50  # `MeshNetwork-rtr.json`
MOTION, PRESENCE, TRANSMITTER_1G, TRANSMITTER_2G = (
    0x51,
    0x52,
    0x53,
    0x54,
)  # `MeshNetwork-detectors.json`
BLIND, SHUTTER, PUCK = 0x55, 0x60, 0x70  # `Blinds.json`
ENERGY_PUCK = 0x61  # `MeshNetwork-puck.json`


def model(mid: str, subs: list[str] | None = None, pub: str | None = None) -> dict:
    m: dict = {"modelId": mid, "bind": [0], "subscribe": subs or []}
    if pub:
        m["publish"] = {
            "address": pub,
            "index": 0,
            "ttl": 255,
            "credentials": 0,
            "retransmit": {"count": 0, "interval": 50},
            "period": {"numberOfSteps": 0, "resolution": 100},
        }
    return m


def primary_models(load_kind: str, own: str, extra_subs: list[str]) -> list[dict]:
    base = [
        model("0000"),
        model("0002"),
        model("1200"),
        model("1201"),
        model("1011"),
        model("1012"),
        model("1013"),
    ]
    subs = [own, *extra_subs]
    if load_kind == "switch":
        base += [
            model("1000", subs, own),
            model("1004", subs),
            model("1006", subs),
            model("1007", subs),
            model("1203", subs, own),
            model("1204", subs),
        ]
    elif load_kind == "dimmer":
        base += [
            model("1000", subs, own),
            model("1002", subs),
            model("1300", subs, own),
            model("1301", subs),
            model("1203", subs, own),
            model("1204", subs),
        ]
    elif load_kind == "ctl":
        base += [
            model("1000", subs, own),
            model("1002", subs),
            model("1300", subs, own),
            model("1301", subs),
            model("1303", subs, own),
            model("1304", subs),
            model("1203", subs, own),
            model("1204", subs),
        ]
    base += [
        model("05271013", [own], own),
        model("05271011"),
        model("05271012"),
        model("05271016"),
        model("05271017"),
    ]
    return base


def button_models(own: str, target: str | None = None) -> list[dict]:
    pub = target or own
    return [
        model("1001", [pub], pub),
        model("1003"),
        model("1302"),
        model("1205"),
        model("1305"),
        model("05271015", [pub], pub),
        model("05271013", [own], own),
        model("05271012"),
        model("05271011"),
    ]


def node(
    uuid: str,
    name: str,
    unicast: str,
    pid: str,
    elements: list[dict],
    cid: str = "0527",
) -> dict:
    return {
        "UUID": uuid,
        "name": name,
        "unicastAddress": unicast,
        "deviceKey": "0" * 30 + unicast[-2:],
        "cid": cid,
        "pid": pid,
        "vid": "0001",
        "crpl": "0040",
        "security": "insecure",
        "configComplete": True,
        "excluded": False,
        "defaultTTL": 5,
        "features": {"relay": 1, "proxy": 1, "friend": 2, "lowPower": 2},
        "netKeys": [{"index": 0, "updated": False}],
        "appKeys": [{"index": 0, "updated": False}],
        "elements": elements,
    }


def build() -> None:
    groups = [
        {
            "address": "FEF5",
            "name": "device type group #0xFEF5",
            "parentAddress": "0000",
        },
        {
            "address": "FEF8",
            "name": "device type group #0xFEF8",
            "parentAddress": "0000",
        },
        {"address": "C00F", "name": "WC", "parentAddress": "0000"},
        {"address": "C010", "name": "Living room", "parentAddress": "0000"},
        {"address": "C011", "name": "Kitchen", "parentAddress": "0000"},
        {"address": "C005", "name": "element group #0xDC", "parentAddress": "0000"},
        {"address": "C061", "name": "element group #0x148", "parentAddress": "0000"},
        {"address": "C062", "name": "element group #0x149", "parentAddress": "0000"},
        {"address": "C044", "name": "element group #0x232", "parentAddress": "0000"},
        {"address": "C04E", "name": "element group #0x233", "parentAddress": "0000"},
        {"address": "C04F", "name": "element group #0x234", "parentAddress": "0000"},
        {"address": "C050", "name": "element group #0x235", "parentAddress": "0000"},
        {"address": "C000", "name": "element group #0x172", "parentAddress": "0000"},
        {"address": "C001", "name": "element group #0x173", "parentAddress": "0000"},
        {"address": "C070", "name": "element group #0x300", "parentAddress": "0000"},
        {"address": "C071", "name": "element group #0x301", "parentAddress": "0000"},
        {"address": "C080", "name": "element group #0x400", "parentAddress": "0000"},
        {"address": "C081", "name": "element group #0x401", "parentAddress": "0000"},
    ]
    nodes = [
        node(
            PROVISIONER_UUID,
            "iPhone",
            "0001",
            None,
            [
                {
                    "index": 0,
                    "location": "0001",
                    "name": "Primary Element",
                    "models": [model("0001"), model("1001"), model("05271015")],
                }
            ],
            cid="004C",
        ),
        node(
            node_uuid(GATEWAY),
            "Gateway",
            "00DC",
            "000B",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": [
                        model("0000"),
                        model("0002"),
                        model("1001"),
                        model("1013"),
                        model("05271013", ["C005"], "C005"),
                    ],
                }
            ],
        ),
        # switched push-button: load 0148 (WC), button 0149 wired to gateway, aux 014A
        node(
            node_uuid(PUSH_BUTTON_1G),
            "Push-button 1-gang",
            "0148",
            "0001",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": primary_models("switch", "C061", ["FEF5", "C00F"]),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": button_models("C062", "C005"),
                },
                {
                    "index": 2,
                    "location": "0044",
                    "models": [
                        model("05271015"),
                        model("05271013"),
                        model("05271012"),
                        model("05271011"),
                    ],
                },
            ],
        ),
        # DALI dimmer 2-gang: CTL load 0232 (+ temperature element 0233), rocker 0234/0235, aux 0236
        node(
            node_uuid(PUSH_BUTTON_2G),
            "Push-button 2-gang",
            "0232",
            "0002",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": primary_models("ctl", "C044", ["FEF5", "C010"]),
                },
                {
                    "index": 1,
                    "location": "0001",
                    "models": [
                        model("1002", ["C04E", "FEF5"]),
                        model("1306", ["C04E", "FEF5"], "C04E"),
                    ],
                },
                {
                    "index": 2,
                    "location": "0040",
                    "models": button_models("C04F", "C044"),
                },
                {
                    "index": 3,
                    "location": "0041",
                    "models": button_models("C050", "C044"),
                },
                {
                    "index": 4,
                    "location": "0044",
                    "models": [
                        model("05271015"),
                        model("05271013"),
                        model("05271012"),
                        model("05271011"),
                    ],
                },
            ],
        ),
        # metering socket (Kitchen): load 0172, sensor element 0173
        node(
            node_uuid(SOCKET),
            "Socket",
            "0172",
            "0003",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": primary_models("switch", "C000", ["FEF8", "C011"]),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": [
                        model("1001"),
                        model("1205"),
                        model("1100", [], "C001"),
                        model("1101"),
                        model("1013"),
                        model("05271013", ["C001"], "C001"),
                    ],
                },
            ],
        ),
        # dimmer push-button (Lightness server, no CTL): load 0300 (WC), button 0301 wired to its own load
        node(
            node_uuid(DIMMER),
            "Push-button 1-gang",
            "0300",
            "0001",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": primary_models("dimmer", "C070", ["FEF5", "C00F"]),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": button_models("C071", "C070"),
                },
            ],
        ),
        # 2-channel actuator (Kitchen): two switched outputs 0400 (location 0001) and 0401 (location 0002)
        node(
            node_uuid(ACTUATOR),
            "2-channel actuator",
            "0400",
            "0010",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": primary_models("switch", "C080", ["FEF5", "C011"]),
                },
                {
                    "index": 1,
                    "location": "0002",
                    "models": primary_models("switch", "C081", ["FEF5", "C011"]),
                },
            ],
        ),
    ]
    net = {
        "meshNetwork": {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "id": "https://www.bluetooth.com/specifications/specs/mesh-cdb-1-0-1-schema.json#",
            "version": "1.0.1",
            "meshUUID": "1BAF3ADE-0000-4000-8000-000000000001",
            "meshName": "Test network",
            "timestamp": "2026-01-01T00:00:00Z",
            "partial": False,
            "netKeys": [
                {
                    "name": "Primary Network Key",
                    "index": 0,
                    "phase": 0,
                    "key": NETKEY,
                    "minSecurity": "insecure",
                    "timestamp": "0001-01-01T00:00:00Z",
                }
            ],
            "appKeys": [
                {
                    "name": "Default Application Key",
                    "index": 0,
                    "boundNetKey": 0,
                    "key": APPKEY,
                }
            ],
            "provisioners": [
                {
                    "provisionerName": "iPhone",
                    "UUID": PROVISIONER_UUID,
                    "allocatedUnicastRange": [
                        {"lowAddress": "0001", "highAddress": "0CCC"}
                    ],
                    "allocatedGroupRange": [
                        {"lowAddress": "C000", "highAddress": "C64B"}
                    ],
                    "allocatedSceneRange": [
                        {"firstScene": "0001", "lastScene": "1999"}
                    ],
                }
            ],
            "nodes": nodes,
            "groups": groups,
            "scenes": [
                {"name": "Scene #1", "number": "0001", "addresses": ["0148"]},
                {"name": "Scene #2", "number": "0002", "addresses": []},
            ],
            "networkExclusions": [{"ivIndex": 0, "addresses": ["0002"]}],
        }
    }
    (HERE / "MeshNetwork.json").write_text(json.dumps(net, indent=1))
    meta_dir = HERE / "Application Support"
    meta_dir.mkdir(exist_ok=True)
    device_metadata = [
        {
            "nodeId": node_uuid(PUSH_BUTTON_1G),
            "productIdentifier": {"albrechtJung": {"_0": 1}},
            "actuatorFunction": {"actuatorFunctionId": 0, "insertType": 2},
            "locationIds": [1],
        },
        {
            "id": None,
            "name": "WC mirror",
            "isFavorite": False,
            "cachedGroupConnectionMetadata": [],
            "gatewayNoteFieldInfo": {"positionNotes": {}},
        },
        {
            "nodeId": node_uuid(PUSH_BUTTON_1G),
            "productIdentifier": {"albrechtJung": {"_0": 1}},
            "actuatorFunction": {"actuatorFunctionId": 0, "insertType": 2},
            "locationIds": [64, 68],
        },
        {
            "id": None,
            "name": "WC mirror button",
            "isFavorite": False,
            "cachedGroupConnectionMetadata": [],
            "gatewayNoteFieldInfo": {"positionNotes": {}},
        },
        {
            "nodeId": node_uuid(PUSH_BUTTON_2G),
            "productIdentifier": {"albrechtJung": {"_0": 2}},
            "actuatorFunction": {"actuatorFunctionId": 4, "insertType": 2},
            "locationIds": [1],
        },
        {
            "id": None,
            "name": "Living room DALI",
            "isFavorite": False,
            "cachedGroupConnectionMetadata": [],
            "gatewayNoteFieldInfo": {"positionNotes": {}},
        },
        {
            "nodeId": node_uuid(PUSH_BUTTON_2G),
            "productIdentifier": {"albrechtJung": {"_0": 2}},
            "actuatorFunction": {"actuatorFunctionId": 4, "insertType": 2},
            "locationIds": [64, 65, 68],
        },
        {
            "id": None,
            "name": "Living room rocker",
            "isFavorite": False,
            "cachedGroupConnectionMetadata": [],
            "gatewayNoteFieldInfo": {"positionNotes": {}},
        },
        {
            "nodeId": node_uuid(SOCKET),
            "productIdentifier": {"albrechtJung": {"_0": 3}},
            "actuatorFunction": {"actuatorFunctionId": 0, "insertType": 1},
            "locationIds": [1, 64],
        },
        {
            "id": None,
            "name": "Boiler",
            "isFavorite": False,
            "cachedGroupConnectionMetadata": [],
            "gatewayNoteFieldInfo": {"positionNotes": {}},
        },
        {
            "nodeId": node_uuid(DIMMER),
            "productIdentifier": {"albrechtJung": {"_0": 1}},
            "actuatorFunction": {"actuatorFunctionId": 2, "insertType": 2},
            "locationIds": [1],
        },
        {
            "id": None,
            "name": "WC ceiling",
            "isFavorite": False,
            "cachedGroupConnectionMetadata": [],
            "gatewayNoteFieldInfo": {"positionNotes": {}},
        },
        {
            "nodeId": node_uuid(DIMMER),
            "productIdentifier": {"albrechtJung": {"_0": 1}},
            "actuatorFunction": {"actuatorFunctionId": 2, "insertType": 2},
            "locationIds": [64],
        },
        {
            "id": None,
            "name": "WC ceiling button",
            "isFavorite": False,
            "cachedGroupConnectionMetadata": [],
            "gatewayNoteFieldInfo": {"positionNotes": {}},
        },
        {
            "nodeId": node_uuid(ACTUATOR),
            "productIdentifier": {"albrechtJung": {"_0": 16}},
            "actuatorFunction": {"actuatorFunctionId": 0, "insertType": 3},
            "locationIds": [1],
        },
        {
            "id": None,
            "name": "Kitchen ceiling",
            "isFavorite": False,
            "cachedGroupConnectionMetadata": [],
            "gatewayNoteFieldInfo": {"positionNotes": {}},
        },
    ]
    (meta_dir / "device_metadata.json").write_text(
        json.dumps(device_metadata, indent=1)
    )
    (meta_dir / "scene_metadata.json").write_text(
        json.dumps(
            [
                1,
                {
                    "icon": "SceneAbsent",
                    "isFavorite": False,
                    "name": "WC off",
                    "sceneNumber": 1,
                },
                2,
                {
                    "icon": "SceneNight",
                    "isFavorite": False,
                    "name": "All off",
                    "sceneNumber": 2,
                },
            ],
            indent=1,
        )
    )


def network(mesh_uuid: str, name: str, nodes: list[dict], groups: list[dict]) -> dict:
    """Wrap nodes and groups in a raw (`meshNetwork`) CDB document with the fake keys and the phone provisioner."""
    return {
        "meshNetwork": {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "id": "https://www.bluetooth.com/specifications/specs/mesh-cdb-1-0-1-schema.json#",
            "version": "1.0.1",
            "meshUUID": mesh_uuid,
            "meshName": name,
            "timestamp": "2026-01-01T00:00:00Z",
            "partial": False,
            "netKeys": [
                {
                    "name": "Primary Network Key",
                    "index": 0,
                    "phase": 0,
                    "key": NETKEY,
                    "minSecurity": "insecure",
                    "timestamp": "0001-01-01T00:00:00Z",
                }
            ],
            "appKeys": [
                {
                    "name": "Default Application Key",
                    "index": 0,
                    "boundNetKey": 0,
                    "key": APPKEY,
                }
            ],
            "provisioners": [
                {
                    "provisionerName": "iPhone",
                    "UUID": PROVISIONER_UUID,
                    "allocatedUnicastRange": [
                        {"lowAddress": "0001", "highAddress": "0CCC"}
                    ],
                    "allocatedGroupRange": [
                        {"lowAddress": "C000", "highAddress": "C64B"}
                    ],
                    "allocatedSceneRange": [
                        {"firstScene": "0001", "lastScene": "1999"}
                    ],
                }
            ],
            "nodes": nodes,
            "groups": groups,
            "scenes": [],
            "networkExclusions": [],
        }
    }


def detector_models(own: str, target: str, sensor_group: str) -> list[dict]:
    """The sensor element of a detector: OnOff client → `target` (its load), Sensor Server → `sensor_group`."""
    return [
        model("1001", [target], target),
        model("1003"),
        model("1100", [], sensor_group),  # "sensor values for gateway" on
        model("1101"),
        model("1013"),
        model("05271015", [target], target),
        model("05271013", [own], own),
        model("05271012"),
        model("05271011"),
    ]


def battery_primary_models(own: str) -> list[dict]:
    """The primary element of a battery wall transmitter: no load, a Generic Battery Server and the LBC servers."""
    return [
        model("0000"),
        model("0002"),
        model("100C"),
        model("1200"),
        model("1201"),
        model("1011"),
        model("1012"),
        model("1013"),
        model("05271013", [own], own),
        model("05271011"),
        model("05271012"),
        model("05271016"),
        model("05271017"),
    ]


def build_detectors() -> None:
    """A second SYNTHETIC network (`MeshNetwork-detectors.json`): detectors and battery wall transmitters.

    Their real compositions are unknown (no such device in the maintainer's network); these follow the rules of
    `docs/android/network-logic.md` (loads at location 0001, keys / sensor elements at 0040+, publications to the
    element's own group) and the detector wiring of `docs/gap-analysis/control-and-state.md` §2.8.
    """
    groups = [
        {
            "address": "FEF5",
            "name": "device type group #0xFEF5",
            "parentAddress": "0000",
        },
        {"address": "C00F", "name": "WC", "parentAddress": "0000"},
        {"address": "C010", "name": "Living room", "parentAddress": "0000"},
        {"address": "C005", "name": "element group #0xDC", "parentAddress": "0000"},
        {"address": "C0A0", "name": "element group #0x500", "parentAddress": "0000"},
        {"address": "C0A1", "name": "element group #0x501", "parentAddress": "0000"},
        {"address": "C0A2", "name": "element group #0x510", "parentAddress": "0000"},
        {"address": "C0A3", "name": "element group #0x511", "parentAddress": "0000"},
        {"address": "C0A4", "name": "element group #0x520", "parentAddress": "0000"},
        {"address": "C0A5", "name": "element group #0x521", "parentAddress": "0000"},
        {"address": "C0A6", "name": "element group #0x530", "parentAddress": "0000"},
        {"address": "C0A7", "name": "element group #0x531", "parentAddress": "0000"},
        {"address": "C0A8", "name": "element group #0x532", "parentAddress": "0000"},
    ]
    aux = {
        "index": 2,
        "location": "0044",
        "models": [
            model("05271015"),
            model("05271013"),
            model("05271012"),
            model("05271011"),
        ],
    }
    nodes = [
        node(
            PROVISIONER_UUID,
            "iPhone",
            "0001",
            None,
            [
                {
                    "index": 0,
                    "location": "0001",
                    "name": "Primary Element",
                    "models": [model("0001"), model("1001"), model("05271015")],
                }
            ],
            cid="004C",
        ),
        node(
            node_uuid(GATEWAY),
            "Gateway",
            "00DC",
            "000B",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": [
                        model("0000"),
                        model("0002"),
                        model("1001"),
                        model("1013"),
                        model("05271013", ["C005"], "C005"),
                    ],
                }
            ],
        ),
        # motion detector 1 m (WC): relay output 0500, sensor element 0501 driving the relay through its group
        node(
            node_uuid(MOTION),
            "Motion detector 1 m",
            "0500",
            "0007",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": primary_models("switch", "C0A0", ["FEF5", "C00F"]),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": detector_models("C0A1", "C0A0", "C0A1"),
                },
            ],
        ),
        # ceiling presence detector (Living room): relay 0510, sensor element 0511 driving the room group
        node(
            node_uuid(PRESENCE),
            "Presence detector",
            "0510",
            "0009",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": primary_models("switch", "C0A2", ["FEF5", "C010"]),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": detector_models("C0A3", "C010", "C0A3"),
                },
            ],
        ),
        # battery wall transmitter 1-gang: key 0521 linked to the gateway (vendor events)
        node(
            node_uuid(TRANSMITTER_1G),
            "Wall transmitter 1-gang",
            "0520",
            "0005",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": battery_primary_models("C0A4"),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": button_models("C0A5", "C005"),
                },
                aux,
            ],
        ),
        # battery wall transmitter 2-gang: key A 0531 linked to the gateway, key B 0532 wired to the WC relay
        node(
            node_uuid(TRANSMITTER_2G),
            "Wall transmitter 2-gang",
            "0530",
            "0006",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": battery_primary_models("C0A6"),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": button_models("C0A7", "C005"),
                },
                {
                    "index": 2,
                    "location": "0041",
                    "models": button_models("C0A8", "C0A0"),
                },
                {**aux, "index": 3},
            ],
        ),
    ]
    (HERE / "MeshNetwork-detectors.json").write_text(
        json.dumps(
            network(
                "1BAF3ADE-0000-4000-8000-000000000002",
                "Test network (detectors)",
                nodes,
                groups,
            ),
            indent=1,
        )
    )


def rtr_models(own: str, room: str) -> list[dict]:
    """A room thermostat's primary element: OnOff server (heating demand), Level server (set-point), Sensor server
    (room temperature, publishing to the element group as "sensor values for gateway" configures it) and the OnOff
    client that drives bound heating actuators. Synthetic: the composition of a real RTR is not captured yet."""
    subs = [own, "FEF9", room]
    return [
        model("0000"),
        model("0002"),
        model("1200"),
        model("1201"),
        model("1011"),
        model("1012"),
        model("1013"),
        model("1000", subs, own),
        model("1002", subs, own),
        model("1001", [own], own),
        model("1100", [], own),
        model("1101"),
        model("1203", subs, own),
        model("1204", subs),
        model("05271013", [own], own),
        model("05271011"),
        model("05271012"),
        model("05271016"),
        model("05271017"),
    ]


def build_rtr() -> None:
    """Write `MeshNetwork-rtr.json`: the base network plus a room thermostat (PID 0x000A) at 0500 in the living room."""
    net = json.loads((HERE / "MeshNetwork.json").read_text())
    net["meshNetwork"]["groups"] += [
        {
            "address": "FEF9",
            "name": "device type group #0xFEF9",
            "parentAddress": "0000",
        },
        {"address": "C090", "name": "element group #0x500", "parentAddress": "0000"},
    ]
    net["meshNetwork"]["nodes"].append(
        node(
            node_uuid(THERMOSTAT),
            "Room thermostat",
            "0500",
            "000A",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": rtr_models("C090", "C010"),
                }
            ],
        )
    )
    (HERE / "MeshNetwork-rtr.json").write_text(json.dumps(net, indent=1))


def build_puck() -> None:
    """Write `MeshNetwork-puck.json`: the base network plus an energy puck (PID 0x0010) at 0610 in the kitchen.

    Its composition is a guess from the app (no puck in the maintainer's network): the switched output on the
    primary element, the binary inputs E1 / E2 at 0040 / 0041 (`docs/android/firmware-products.md`), and a meter
    element like the metering socket's (Sensor Server, SIG property servers, OnOff client), at 0042.
    """
    net = json.loads((HERE / "MeshNetwork.json").read_text())
    net["meshNetwork"]["groups"] += [
        {"address": "C0A0", "name": "element group #0x610", "parentAddress": "0000"},
        {"address": "C0A1", "name": "element group #0x611", "parentAddress": "0000"},
        {"address": "C0A2", "name": "element group #0x612", "parentAddress": "0000"},
        {"address": "C0A3", "name": "element group #0x613", "parentAddress": "0000"},
    ]
    net["meshNetwork"]["nodes"].append(
        node(
            node_uuid(ENERGY_PUCK),
            "Energy puck",
            "0610",
            "0010",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": primary_models("switch", "C0A0", ["FEF5", "C011"]),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": button_models("C0A1", "C0A0"),
                },
                {
                    "index": 2,
                    "location": "0041",
                    "models": button_models("C0A2", "C0A0"),
                },
                {
                    "index": 3,
                    "location": "0042",
                    "models": [
                        model("1001"),
                        model("1205"),
                        model("1100", [], "C0A3"),
                        model("1101"),
                        model("1011"),
                        model("1012"),
                        model("1013"),
                        model("05271013", ["C0A3"], "C0A3"),
                    ],
                },
            ],
        )
    )
    (HERE / "MeshNetwork-puck.json").write_text(json.dumps(net, indent=1))


if __name__ == "__main__":
    build()
    build_detectors()
    build_rtr()
    build_puck()
