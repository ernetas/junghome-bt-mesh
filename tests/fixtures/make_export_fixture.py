"""Generate the SYNTHETIC share exports (`JungHome-android.json`, `JungHome.json`) from `MeshNetwork.json`.

Same fake keys and node identities as `make_fixture.py` (never real ones; run that first). `JungHome-android.json`
has the layout the Android app writes (`docs/gap-analysis/network-features.md` §8.1): Gson pretty-printed outer
document, a complete `meta` block with Gson field order and integer addresses, `network` = Base64 of the *compact*
Nordic CDB JSON without the iOS `meshNetwork` wrapper, and a Nordic-Android timestamp (`+0100` zone). The CDB
differs from `MeshNetwork.json` in one respect: the dimmer's button `0301` is linked to the *WC room* (publishes to
its own element group `C071`, the WC loads subscribe to it) so the `cachedGroupConnectionMetadata` rewiring paths
have something to work on. `JungHome.json` is the iOS app's "share via file" flavour of the unchanged network
(`build_ios`).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from make_fixture import (
    DIMMER,
    PUSH_BUTTON_1G,
    PUSH_BUTTON_2G,
    SOCKET,
    node_mac,
    node_uuid,
)

HERE = Path(__file__).parent


def build() -> None:
    net = json.loads((HERE / "MeshNetwork.json").read_text())["meshNetwork"]
    net["timestamp"] = "2026-02-01T10:00:00+0100"
    nodes = {n["unicastAddress"]: n for n in net["nodes"]}
    # button 0301 -> WC room link: clients publish/subscribe to its own element group C071
    for m in nodes["0300"]["elements"][1]["models"]:
        if m["modelId"] in ("1001", "05271015"):
            m["publish"]["address"] = "C071"
            m["subscribe"] = ["C071"]
    # WC loads (0148 switch, 0300 dimmer) subscribe their OnOff / Level servers to C071
    for unicast in ("0148", "0300"):
        for m in nodes[unicast]["elements"][0]["models"]:
            if m["modelId"] in ("1000", "1002") and "C071" not in m["subscribe"]:
                m["subscribe"].append("C071")
    groups = {int(g["address"], 16): g["name"] for g in net["groups"]}
    meta = {
        "userGroups": [
            {"name": "WC", "address": 0xC00F, "icon": "ic_group_bathroom"},
            {"name": "Living room", "address": 0xC010, "icon": "ic_group_living_room"},
            {"name": "Kitchen", "address": 0xC011, "icon": "ic_group_kitchen"},
        ],
        "elementConnectionGroups": [
            {"elementAddress": int(name.split("#0x")[1], 16), "groupAddress": addr}
            for addr, name in sorted(groups.items())
            if name.startswith("element group #")
        ],
        "devices": [
            {
                "name": "WC mirror",
                "macAddress": node_mac(PUSH_BUTTON_1G),
                "deviceId": {
                    "actuatorFunctionId": 0,
                    "locationIds": [1],
                    "insertType": 2,
                    "productId": 1,
                    "nodeId": node_uuid(PUSH_BUTTON_1G),
                },
                "cachedGroupConnectionMetadata": [],
            },
            {
                "name": "WC mirror button",
                "macAddress": node_mac(PUSH_BUTTON_1G),
                "deviceId": {
                    "actuatorFunctionId": 0,
                    "locationIds": [64, 68],
                    "insertType": 2,
                    "productId": 1,
                    "nodeId": node_uuid(PUSH_BUTTON_1G),
                },
                "cachedGroupConnectionMetadata": None,
            },
            {
                "name": "Living room DALI",
                "macAddress": node_mac(PUSH_BUTTON_2G),
                "deviceId": {
                    "actuatorFunctionId": 4,
                    "locationIds": [1],
                    "insertType": 2,
                    "productId": 2,
                    "nodeId": node_uuid(PUSH_BUTTON_2G),
                },
                "cachedGroupConnectionMetadata": [],
            },
            {
                "name": "Boiler",
                "macAddress": node_mac(SOCKET),
                "deviceId": {
                    "actuatorFunctionId": 0,
                    "locationIds": [1, 64],
                    "insertType": 1,
                    "productId": 3,
                    "nodeId": node_uuid(SOCKET),
                },
                "cachedGroupConnectionMetadata": [],
            },
            {
                "name": "WC ceiling",
                "macAddress": node_mac(DIMMER),
                "deviceId": {
                    "actuatorFunctionId": 2,
                    "locationIds": [1],
                    "insertType": 2,
                    "productId": 1,
                    "nodeId": node_uuid(DIMMER),
                },
                "cachedGroupConnectionMetadata": [],
            },
            {
                "name": "WC ceiling button",
                "macAddress": node_mac(DIMMER),
                "deviceId": {
                    "actuatorFunctionId": 2,
                    "locationIds": [64],
                    "insertType": 2,
                    "productId": 1,
                    "nodeId": node_uuid(DIMMER),
                },
                "cachedGroupConnectionMetadata": [
                    {
                        "elementAddress": 0x0301,
                        "groupAddress": 0xC00F,
                        "publishAddress": 0xC071,
                        "function": "LIGHT",
                    }
                ],
            },
        ],
        "scenes": [
            {"name": "WC off", "number": 1, "icon": "SceneAbsent"},
            {"name": "All off", "number": 2, "icon": "SceneNight"},
        ],
        "sceneInfo": [
            {
                "scene": 1,
                "deviceId": {
                    "actuatorFunctionId": 0,
                    "locationIds": [1],
                    "insertType": 2,
                    "productId": 1,
                    "nodeId": node_uuid(PUSH_BUTTON_1G),
                },
                "infos": {"lightness": 0},
            }
        ],
        "schedulerMetaInfo": [],
        "timer": [],
        "actuatorExports": [
            {
                "actuatorId": {"actuatorFunctionId": 0, "insertType": 2},
                "elementAddress": 0x0148,
            },
            {
                "actuatorId": {"actuatorFunctionId": 4, "insertType": 2},
                "elementAddress": 0x0232,
            },
            {
                "actuatorId": {"actuatorFunctionId": 2, "insertType": 2},
                "elementAddress": 0x0300,
            },
        ],
        "buttonLayoutExports": [
            {"mode": 1, "elementAddress": 0x0148},
            {"mode": 5, "elementAddress": 0x0232},
            {"mode": 1, "elementAddress": 0x0300},
        ],
        "keyModeSceneConfigExports": [
            {
                "sceneConfig": {
                    "transitionStepSeconds": 0,
                    "sceneId": 1,
                    "transitionResolution": 0,
                    "publicationAddress": 0xC061,
                },
                "elementAddress": 0x0149,
            }
        ],
    }
    inner = json.dumps(net, ensure_ascii=False, separators=(",", ":"))  # Gson compact
    doc = {
        "version": "1.1",
        "appVersion": "2.2.0 (822956)",
        "platform": "Android (Pixel 8, 14)",
        "meta": meta,
        "network": base64.b64encode(inner.encode()).decode(),
    }
    (HERE / "JungHome-android.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    )


def build_ios() -> None:
    """`JungHome.json`: the iOS "share via file" flavour — one-line JSON with Python's default separators, the
    network as it is (no `meshNetwork` wrapper, not compacted), `deviceId` keys in alphabetical order (the Codable
    encoder's; `test_ios_share_export_key_order_is_kept` pins that the writer keeps it) and a `meta` block that
    renames the WC mirror and scene 1 so a test can tell this file's names from the metadata directory's."""
    net = json.loads((HERE / "MeshNetwork.json").read_text())["meshNetwork"]
    meta = {
        "devices": [
            {
                "name": "WC mirror (share)",
                "macAddress": node_mac(PUSH_BUTTON_1G),
                "deviceId": {
                    "actuatorFunctionId": 0,
                    "insertType": 2,
                    "locationIds": [1],
                    "nodeId": node_uuid(PUSH_BUTTON_1G),
                    "productId": 1,
                },
                "cachedGroupConnectionMetadata": [],
            }
        ],
        "scenes": [{"name": "WC off (share)", "number": 1, "icon": "x"}],
        "userGroups": [],
    }
    doc = {
        "version": "1.1",
        "appVersion": "2.2.0 (822956)",
        "platform": "Android",
        "meta": meta,
        "network": base64.b64encode(json.dumps(net).encode()).decode(),
    }
    (HERE / "JungHome.json").write_text(json.dumps(doc))


if __name__ == "__main__":
    build()
    build_ios()
