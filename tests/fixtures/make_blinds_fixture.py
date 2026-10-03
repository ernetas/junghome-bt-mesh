"""Generate `Blinds.json`: the SYNTHETIC `MeshNetwork.json` network plus three blinds nodes, as a share export.

Same fake keys as `make_fixture.py` (never real ones; run that first). No blinds node has been captured yet, so the
three compositions below are what `docs/gap-analysis/control-and-state.md` §2.6 and the real export's element layout
suggest — the cover tests pin the integration's behaviour against them, not against hardware:

- `0500` blinds actuator mini (PID 0x000D, "Kitchen blind"): position element 0500 (Generic Level server, location
  0001) subscribed to the Kitchen room, slat element 0501 (a second Generic Level server), two binary inputs;
- `0600` push-button 2-gang with a blinds insert (PID 0x0002, "Living room shutter"): the same two level elements
  without a lamp server next to them, a rocker and the aux element;
- `0700` blinds PP2 puck (PID 0x0013, unnamed): a position element only — hosting an OnOff server *too* (a
  hypothetical composition): a blinds-only product must not come out as a switched light.

The document is the app's "share via file" flavour (`{"version", "meta", "network": base64}`), so the names travel
inside the file and the tests need no metadata directory.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from make_fixture import (
    BLIND,
    PUCK,
    SHUTTER,
    button_models,
    model,
    node,
    node_mac,
    node_uuid,
)

HERE = Path(__file__).parent
BASE_MODELS = ["0000", "0002", "1200", "1201", "1011", "1012", "1013"]
VENDOR_MODELS = ["05271011", "05271012", "05271016", "05271017"]


def level_element(
    own: str, subs: list[str], *, primary: bool, onoff: bool = False
) -> list[dict]:
    """A Generic Level server element: the node's primary one carries the common servers and the scene server."""
    all_subs = [own, *subs]
    models = [model(m) for m in BASE_MODELS] if primary else []
    if onoff:
        models.append(model("1000", all_subs, own))
    models.append(model("1002", all_subs, own))
    if primary:
        models += [
            model("1004", all_subs),
            model("1006", all_subs),
            model("1007", all_subs),
            model("1203", all_subs, own),
            model("1204", all_subs),
        ]
    models.append(model("05271013", [own], own))
    models += [model(m) for m in VENDOR_MODELS]
    return models


def build() -> None:
    net = json.loads((HERE / "MeshNetwork.json").read_text())["meshNetwork"]
    net["timestamp"] = "2026-03-01T10:00:00+0100"
    net["groups"] += [
        {
            "address": "FEF6",
            "name": "device type group #0xFEF6",
            "parentAddress": "0000",
        },
        {
            "address": "FEF7",
            "name": "device type group #0xFEF7",
            "parentAddress": "0000",
        },
        {"address": "C090", "name": "element group #0x500", "parentAddress": "0000"},
        {"address": "C091", "name": "element group #0x501", "parentAddress": "0000"},
        {"address": "C092", "name": "element group #0x502", "parentAddress": "0000"},
        {"address": "C093", "name": "element group #0x503", "parentAddress": "0000"},
        {"address": "C0A0", "name": "element group #0x600", "parentAddress": "0000"},
        {"address": "C0A1", "name": "element group #0x601", "parentAddress": "0000"},
        {"address": "C0A2", "name": "element group #0x602", "parentAddress": "0000"},
        {"address": "C0A3", "name": "element group #0x603", "parentAddress": "0000"},
        {"address": "C0B0", "name": "element group #0x700", "parentAddress": "0000"},
        {"address": "C0B1", "name": "element group #0x701", "parentAddress": "0000"},
        {"address": "C0B2", "name": "element group #0x702", "parentAddress": "0000"},
    ]
    aux = {
        "models": [
            model("05271015"),
            model("05271013"),
            model("05271012"),
            model("05271011"),
        ]
    }
    net["nodes"] += [
        node(
            node_uuid(BLIND),
            "Blinds actuator 1-gang mini",
            "0500",
            "000D",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": level_element("C090", ["FEF6", "C011"], primary=True),
                },
                {
                    "index": 1,
                    "location": "0001",
                    "models": level_element("C091", ["FEF7"], primary=False),
                },
                {
                    "index": 2,
                    "location": "0040",
                    "models": button_models("C092", "C090"),
                },
                {
                    "index": 3,
                    "location": "0041",
                    "models": button_models("C093", "C090"),
                },
            ],
        ),
        node(
            node_uuid(SHUTTER),
            "Push-button 2-gang",
            "0600",
            "0002",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": level_element("C0A0", ["FEF6", "C010"], primary=True),
                },
                {
                    "index": 1,
                    "location": "0001",
                    "models": level_element("C0A1", ["FEF7"], primary=False),
                },
                {
                    "index": 2,
                    "location": "0040",
                    "models": button_models("C0A2", "C0A0"),
                },
                {
                    "index": 3,
                    "location": "0041",
                    "models": button_models("C0A3", "C0A0"),
                },
                {"index": 4, "location": "0044", **aux},
            ],
        ),
        node(
            node_uuid(PUCK),
            "Blinds PP2 actuator",
            "0700",
            "0013",
            [
                {
                    "index": 0,
                    "location": "0001",
                    "models": level_element("C0B0", ["FEF6"], primary=True, onoff=True),
                },
                {
                    "index": 1,
                    "location": "0040",
                    "models": button_models("C0B1", "C0B0"),
                },
                {
                    "index": 2,
                    "location": "0041",
                    "models": button_models("C0B2", "C0B0"),
                },
            ],
        ),
    ]
    meta = {
        "userGroups": [
            {"name": "WC", "address": 0xC00F, "icon": "ic_group_bathroom"},
            {"name": "Living room", "address": 0xC010, "icon": "ic_group_living_room"},
            {"name": "Kitchen", "address": 0xC011, "icon": "ic_group_kitchen"},
        ],
        "elementConnectionGroups": [],
        "devices": [
            {
                "name": "Kitchen blind",
                "macAddress": node_mac(BLIND),
                "deviceId": {
                    "actuatorFunctionId": 5,
                    "locationIds": [1],
                    "insertType": 1,
                    "productId": 13,
                    "nodeId": node_uuid(BLIND),
                },
                "cachedGroupConnectionMetadata": [],
            },
            {
                "name": "Kitchen blind inputs",
                "macAddress": node_mac(BLIND),
                "deviceId": {
                    "actuatorFunctionId": 5,
                    "locationIds": [64, 65],
                    "insertType": 1,
                    "productId": 13,
                    "nodeId": node_uuid(BLIND),
                },
                "cachedGroupConnectionMetadata": [],
            },
            {
                "name": "Living room shutter",
                "macAddress": node_mac(SHUTTER),
                "deviceId": {
                    "actuatorFunctionId": 5,
                    "locationIds": [1],
                    "insertType": 2,
                    "productId": 2,
                    "nodeId": node_uuid(SHUTTER),
                },
                "cachedGroupConnectionMetadata": [],
            },
            {
                "name": "Living room shutter rocker",
                "macAddress": node_mac(SHUTTER),
                "deviceId": {
                    "actuatorFunctionId": 5,
                    "locationIds": [64, 65, 68],
                    "insertType": 2,
                    "productId": 2,
                    "nodeId": node_uuid(SHUTTER),
                },
                "cachedGroupConnectionMetadata": [],
            },
        ],
        "scenes": [
            {"name": "WC off", "number": 1, "icon": "SceneAbsent"},
            {"name": "All off", "number": 2, "icon": "SceneNight"},
        ],
        "sceneInfo": [],
        "schedulerMetaInfo": [],
        "timer": [],
        "actuatorExports": [],
        "buttonLayoutExports": [],
        "keyModeSceneConfigExports": [],
    }
    inner = json.dumps(net, ensure_ascii=False, separators=(",", ":"))
    doc = {
        "version": "1.1",
        "appVersion": "2.2.0 (822956)",
        "platform": "Android (Pixel 8, 14)",
        "meta": meta,
        "network": base64.b64encode(inner.encode()).decode(),
    }
    (HERE / "Blinds.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    build()
