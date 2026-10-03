#!/usr/bin/env python3
"""Render a human-readable Markdown report of a JUNG HOME iOS app data dump.

Usage:
    tools/mesh_report.py [ios/AppDomain-de.jung.junghome] --anonymise > docs/network-topology.md
    tools/mesh_report.py [ios/AppDomain-de.jung.junghome] --keys --out /somewhere/private.md

Reads the nRF-Mesh style MeshNetwork.json plus the app's own JSON caches and
joins them into one topology view: nodes, elements, models, pub/sub wiring,
rooms, scenes, connections.  Every key (NetKey, AppKey, DevKeys) prints as
`[redacted]` — not even a prefix — unless --keys is given, and --keys only
writes to an explicit --out file (created 0600) that does not lie under the
repository's docs/, so a tracked page can never pick up key material.

--anonymise replaces everything that identifies the installation (MACs, EUI-64 node UUIDs, the mesh and
provisioner UUIDs, the mesh / provisioner / node / app-device / room / group / scene names) with pseudonyms
that are consistent within the run, so the report's cross-references still line up; keys stay `[redacted]`
and --keys is refused alongside it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = ROOT / "ios/AppDomain-de.jung.junghome"
DOCS = ROOT / "docs"
REDACTED = "[redacted]"

# ---------------------------------------------------------------- lookup tables

SIG_MODELS = {
    0x0000: "Config Server",
    0x0001: "Config Client",
    0x0002: "Health Server",
    0x0003: "Health Client",
    0x0004: "Remote Provisioning Server",
    0x0005: "Remote Provisioning Client",
    0x000A: "Private Beacon Server",
    0x000B: "Private Beacon Client",
    0x000E: "SAR Config Server",
    0x000F: "SAR Config Client",
    0x1000: "Generic OnOff Server",
    0x1001: "Generic OnOff Client",
    0x1002: "Generic Level Server",
    0x1003: "Generic Level Client",
    0x1004: "Generic Default Transition Time Server",
    0x1005: "Generic Default Transition Time Client",
    0x1006: "Generic Power OnOff Server",
    0x1007: "Generic Power OnOff Setup Server",
    0x1008: "Generic Power OnOff Client",
    0x100C: "Generic Battery Server",
    0x100D: "Generic Battery Client",
    0x100E: "Generic Location Server",
    0x100F: "Generic Location Setup Server",
    0x1010: "Generic Location Client",
    0x1011: "Generic Admin Property Server",
    0x1012: "Generic Manufacturer Property Server",
    0x1013: "Generic User Property Server",
    0x1014: "Generic Client Property Server",
    0x1015: "Generic Property Client",
    0x1100: "Sensor Server",
    0x1101: "Sensor Setup Server",
    0x1102: "Sensor Client",
    0x1200: "Time Server",
    0x1201: "Time Setup Server",
    0x1202: "Time Client",
    0x1203: "Scene Server",
    0x1204: "Scene Setup Server",
    0x1205: "Scene Client",
    0x1206: "Scheduler Server",
    0x1207: "Scheduler Setup Server",
    0x1208: "Scheduler Client",
    0x1300: "Light Lightness Server",
    0x1301: "Light Lightness Setup Server",
    0x1302: "Light Lightness Client",
    0x1303: "Light CTL Server",
    0x1304: "Light CTL Setup Server",
    0x1305: "Light CTL Client",
    0x1306: "Light CTL Temperature Server",
    0x1307: "Light HSL Server",
    0x1308: "Light HSL Setup Server",
    0x1309: "Light HSL Client",
    0x1400: "BLOB Transfer Server",
    0x1401: "BLOB Transfer Client",
    0x1402: "Firmware Update Server",
    0x1403: "Firmware Update Client",
    0x1404: "Firmware Distribution Server",
    0x1405: "Firmware Distribution Client",
}

# Vendor models of Albrecht JUNG (CID 0x0527), named from the Android decompile
# (docs/android/vendor-models.md §1) and confirmed on air / in the gateway firmware
# (docs/cross-repo-analysis.md §1.3).  The three property servers mirror the SIG
# Generic *Property Server models of the same ids; 0x1016/0x1017 have no SIG counterpart.
JUNG_VENDOR_MODELS = {
    0x1011: "LBC Admin Property Server (device configuration properties)",
    0x1012: "LBC Manufacturer Property Server (versions, gateway strings)",
    0x1013: "LBC User Property Server (InsertId, energy charts; pub/sub own element group)",
    0x1015: "LBC Property Client (button/sensor elements; publishes 0x5012 key events in gateway mode)",
    0x1016: "JH Scheduler (16-slot timed/sunrise/sunset schedules)",
    0x1017: "Scene Action Setup (per-scene actions of the element)",
}

PRODUCT_NAMES = {  # pid -> name; docs/android/properties.md §4 (app) cross-checked with the gateway firmware's ProductID enum
    0x0001: "Push-button 1-gang",
    0x0002: "Push-button 2-gang",
    0x0003: "Socket (metering)",
    0x000C: "Socket (without metering)",
    0x0004: "Switch actuator 1-gang mini",
    0x000D: "Blinds actuator mini",
    0x0005: "Wall transmitter 1-gang (battery)",
    0x0006: "Wall transmitter 2-gang (battery)",
    0x0007: "Motion detector 1.1 m",
    0x0008: "Motion detector 2.2 m",
    0x0009: "Presence detector",
    0x000A: "Room thermostat",
    0x000B: "Gateway",
    0x0010: "Puck switch actuator (energy)",
    0x0011: "Puck switch actuator 2-gang",
    0x0012: "Puck dimmer",
    0x0013: "Puck blinds actuator",
    0x0014: "Puck DALI controller",
    0x0015: "Puck binary input (230 V)",
    0x0016: "Puck binary input (battery)",
}

# GATT namespace descriptor location values used by JUNG as element roles.
LOCATION_ROLE = {
    0x0001: "load/output 1",
    0x0002: "load/output 2",
    0x0040: "button A",
    0x0041: "button B",
    0x0042: "button C",
    0x0043: "button D",
    0x0044: "aux (vendor-only element: LBC property servers + property client)",
}

# Enums confirmed from the Android decompile (docs/android/properties.md §2)
ACTUATOR_FUNCTION = {
    0: "Switch",
    1: "TwoGangSwitch",
    2: "Dimming",
    3: "TwoGangDimming",
    4: "TwDimming (DALI/tunable white)",
    5: "Blind",
    6: "Extension (no load)",
    7: "NotAvailable",
    8: "Rtr",
    9: "Gateway",
}
DEVICE_LAYOUT = {
    0: "ONE_TOP_ONE_BOTTOM (keys A+B)",
    1: "ONE_ROCKER (A)",
    2: "TWO_LEFT_TWO_RIGHT (keys A..D)",
    3: "ONE_LEFT_TWO_RIGHT",
    4: "TWO_LEFT_ONE_RIGHT",
    5: "ONE_LEFT_ONE_RIGHT (rockers A, C)",
}
KEY_MODE = {
    0: "Light",
    1: "Move",
    2: "Scene",
    3: "Property",
    4: "RTR",
    5: "Switch",
    6: "Gateway",
}


def model_name(mid: str) -> str:
    if len(mid) == 8:
        cid, m = int(mid[:4], 16), int(mid[4:], 16)
        if cid == 0x0527:
            return JUNG_VENDOR_MODELS.get(m, f"JUNG vendor 0x{m:04X}")
        return f"vendor {cid:04X}:{m:04X}"
    return SIG_MODELS.get(int(mid, 16), f"SIG 0x{mid}")


# ---------------------------------------------------------------- pseudonyms

_MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
# a JUNG node UUID is its MAC as EUI-64 (`FFFE` after the OUI, zeros behind): `30FB10FF-FE12-3456-0000-000000000000`
_EUI64_UUID = re.compile(
    r"^([0-9A-Fa-f]{6})[Ff]{2}-[Ff][Ee]([0-9A-Fa-f]{2})-([0-9A-Fa-f]{4})-0000-000000000000$"
)
_UUID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_GENERATED_GROUP = ("element group #0x", "device type group #0x")


def _letters(i: int) -> str:
    """0 → A, 25 → Z, 26 → AA: spreadsheet-style column letters."""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(ord("A") + r) + out
    return out


class Anonymiser:
    """Deterministic pseudonyms for everything in a dump that identifies the installation.

    Same input → same pseudonym for the lifetime of the object (one report), so cross-references in the report
    still line up.  Addresses (MACs, UUIDs) are a keyed hash (HMAC-SHA256) under `salt`, which is random per run
    by default: a MAC keeps its public OUI and has only 24 other bits, so a hash under a salt written in this
    public tool could be inverted by trying all 2^24 suffixes.  Pass the same private salt to get the same
    pseudonyms in two runs (stable diffs of a regenerated page).  Names are numbered in the order they are first
    asked for; `render` asks in a fixed order (rooms by address, scenes by number, nodes by unicast), so those
    are stable across runs anyway and carry nothing of the original.
    """

    def __init__(self, salt: bytes | None = None) -> None:
        self._salt = os.urandom(16) if salt is None else salt
        self._seen: dict[tuple[str, str], str] = {}
        self._count: defaultdict[str, int] = defaultdict(int)

    def _digest(self, kind: str, value: str) -> bytes:
        return hmac.new(
            self._salt, f"{kind}\0{value.upper()}".encode(), hashlib.sha256
        ).digest()

    def mac(self, mac: str) -> str:
        """Keep a public (IEEE-assigned, universally administered) OUI, hash the rest; a locally administered
        address has no vendor meaning, so all of it is replaced (by another locally administered unicast one)."""
        if not _MAC.match(mac):
            return mac
        raw = bytes.fromhex(mac.replace(":", ""))
        h = self._digest("mac", mac)
        local = raw[0] & 0x02  # locally administered: nothing public in it
        new = bytes([(h[0] & 0xFC) | 0x02]) + h[1:6] if local else raw[:3] + h[:3]
        return ":".join(f"{b:02X}" for b in new)

    def uuid(self, uuid: str) -> str:
        """A node's EUI-64 UUID becomes the EUI-64 of its pseudonymous MAC (so UUID ↔ MAC still pair up in the
        report); any other UUID becomes a random-looking version-4 UUID.  The input's letter case is kept."""
        if (m := _EUI64_UUID.match(uuid)) is not None:
            raw = m.group(1) + m.group(2) + m.group(3)
            mac = self.mac(":".join(raw[i : i + 2] for i in range(0, 12, 2)))
            hx = mac.replace(":", "")
            new = f"{hx[:6]}FF-FE{hx[6:8]}-{hx[8:]}-0000-000000000000"
        elif _UUID.match(uuid):
            h = bytearray(self._digest("uuid", uuid)[:16])
            h[6] = (h[6] & 0x0F) | 0x40
            h[8] = (h[8] & 0x3F) | 0x80
            x = h.hex().upper()
            new = f"{x[:8]}-{x[8:12]}-{x[12:16]}-{x[16:20]}-{x[20:]}"
        else:
            return uuid
        return new.lower() if uuid == uuid.lower() else new

    def name(self, kind: str, value: str, key: str | None = None) -> str:
        """`Room A`, `Room B` … / `Scene 1`, `Node 2` …: numbered per kind in first-asked order of `key` (default:
        the value itself — two nodes of the same product name are two pseudonyms when keyed on their UUIDs)."""
        seen = (kind, value if key is None else key)
        if seen not in self._seen:
            i = self._count[kind]
            self._count[kind] += 1
            label = _letters(i) if kind == "Room" else str(i + 1)
            self._seen[seen] = f"{kind} {label}"
        return self._seen[seen]


class _Identity:
    """The no-op stand-in when --anonymise is not given."""

    def mac(self, mac: str) -> str:
        return mac

    def uuid(self, uuid: str) -> str:
        return uuid

    def name(
        self, kind: str, value: str, key: str | None = None
    ) -> str:  # Anonymiser.name's signature
        return value


# ---------------------------------------------------------------- loading


class Dump:
    def __init__(self, root: Path, anon: Anonymiser | None = None) -> None:
        self.root = root
        self.anon: Anonymiser | _Identity = anon if anon is not None else _Identity()
        self.net: dict[str, Any] = json.loads(
            (root / "Documents/MeshNetwork.json").read_text()
        )["meshNetwork"]
        aps = root / "Library/Application Support"
        self.groups: dict[str, str] = {
            g["address"]: g["name"] for g in self.net["groups"]
        }
        self.rooms: dict[str, dict[str, Any]] = {}
        for g in json.loads((aps / "groups.json").read_text()):
            self.rooms[f"{g['address']:04X}"] = g
        self.scenes: dict[int, dict[str, Any]] = {}
        raw = json.loads((aps / "scene_metadata.json").read_text())
        for i in range(0, len(raw), 2):
            self.scenes[raw[i]] = raw[i + 1]
        self.gatt: dict[str, dict[str, Any]] = {}
        raw = json.loads((aps / "node_gattdata.json").read_text())
        for i in range(0, len(raw), 2):
            self.gatt[raw[i]] = raw[i + 1]
        self.devices: defaultdict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = (
            defaultdict(list)
        )  # nodeId -> [(key, value)]
        raw = json.loads((aps / "device_metadata.json").read_text())
        for i in range(0, len(raw), 2):
            self.devices[raw[i]["nodeId"]].append((raw[i], raw[i + 1]))
        self.notif: defaultdict[int, dict[int, dict[str, Any]]] = defaultdict(
            dict
        )  # elementAddress -> {type: payload}
        raw = json.loads((aps / "meshnotificationcache.json").read_text())
        for i in range(0, len(raw), 2):
            v = raw[i + 1]
            p = json.loads(base64.b64decode(v["notificationAsJson"]))
            self.notif[p["elementAddress"]][v["notificationType"]] = p
        self.conn_groups = json.loads(
            (aps / "element_connection_groups.json").read_text()
        )
        self.type_groups = json.loads((aps / "device_type_groups.json").read_text())

        # element address -> (node, element)
        self.elements: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        for n in self.net["nodes"]:
            base = int(n["unicastAddress"], 16)
            for e in n["elements"]:
                self.elements[base + e["index"]] = (n, e)

        # pseudonyms are numbered in first-asked order: ask in a fixed one, whatever order render() needs them in
        for addr, g in sorted(self.rooms.items()):
            self.room_name(addr, g["name"])
        for num, s in sorted(self.scenes.items()):
            self.anon.name("Scene", s["name"], key=str(num))
        for n in sorted(self.net["nodes"], key=lambda n: int(n["unicastAddress"], 16)):
            self.node_name(n)

    def node_name(self, n: dict[str, Any]) -> str:
        return self.anon.name("Node", n["name"], key=n["UUID"])

    def room_name(self, addr: str, name: str) -> str:
        """A group's name (the app's or the CDB's), pseudonymised per address; generated names are kept as is."""
        if name == "?" or name.startswith(_GENERATED_GROUP):
            return name
        return self.anon.name("Room", name, key=addr)

    def group_label(self, addr: str) -> str:
        name = self.groups.get(addr, "?")
        if name.startswith("element group #0x"):
            target = int(name.split("#0x")[1], 16)
            if target in self.elements:
                n, e = self.elements[target]
                return f"`{addr}`→el {target:04X} ({self.node_name(n)} {n['unicastAddress']} loc {e['location']})"
            return f"`{addr}`→el {target:04X} (gone)"
        return f"`{addr}` **{self.room_name(addr, name)}**"


# ---------------------------------------------------------------- rendering


def render(d: Dump, show_keys: bool) -> str:  # noqa: PLR0915  # one linear Markdown template
    out: list[str] = []
    w = out.append
    net = d.net
    a = d.anon

    # no prefix, no suffix: 24 bits of a live key in a tracked page were still 24 bits of a live key
    def red(key: str) -> str:
        return key if show_keys else REDACTED

    w(f"# JUNG HOME mesh network: {a.name('Mesh', net['meshName'])}\n")
    w(
        f"*Generated by `tools/mesh_report.py` from the iOS app dump; timestamp in file: {net['timestamp']}*\n"
    )
    mesh_uuid = a.uuid(net["meshUUID"])
    w(f"- Mesh UUID: `{mesh_uuid}`")
    w(f"- Schema: `{net['id']}` (Bluetooth Mesh CDB {net['version']})")
    w(f"- IV index: 0 (from `Library/Preferences/{mesh_uuid}.plist`)")
    for k in net["netKeys"]:
        w(
            f"- NetKey[{k['index']}] `{red(k['key'])}` phase={k['phase']} minSecurity={k['minSecurity']} ({k['name']})"
        )
    for k in net["appKeys"]:
        w(
            f"- AppKey[{k['index']}] `{red(k['key'])}` bound to NetKey {k['boundNetKey']} ({k['name']})"
        )
    for p in net["provisioners"]:
        w(
            f"- Provisioner **{a.name('Provisioner', p['provisionerName'], key=p['UUID'])}** `{a.uuid(p['UUID'])}` unicast {p['allocatedUnicastRange']} "
            f"groups {p['allocatedGroupRange']} scenes {p['allocatedSceneRange']}"
        )
    excl = (
        net["networkExclusions"][0]["addresses"] if net.get("networkExclusions") else []
    )
    w(f"- Excluded (previously used) unicast addresses: {len(excl)}")
    w(f"- Nodes: {len(net['nodes'])}\n")

    # ---- rooms
    w("## Rooms (app groups)\n")
    w(
        "The JUNG HOME Gateway API names the same group `id<decimal address>` (e.g. `id49186` = `C022`); "
        "a gateway *function* is one mesh element and a gateway scene's `value` is the mesh scene number "
        '(docs/ios-app-data.md, "Cross-mapping to the JUNG HOME Gateway API").\n'
    )
    w("| Address | Gateway id | Name | Favorite |")
    w("|---|---|---|---|")
    for addr, g in sorted(d.rooms.items()):
        w(
            f"| `{addr}` | `id{int(addr, 16)}` | {d.room_name(addr, g['name'])} | {'★' if g['isFavorite'] else ''} |"
        )
    w("")
    w(
        "Device-type groups (all servers of one kind, used by 'all on/off' style actions):\n"
    )
    for tg in d.type_groups:
        w(
            f"- `{tg['groupAddress']:04X}`: sample element {tg['elements'][0]:04X}, models {json.dumps(tg['elements'][1])}"
        )
    w("")

    # ---- scenes
    w("## Scenes\n")
    w("| # | Name | Icon | Stored on elements |")
    w("|---|---|---|---|")
    sc_addr = {int(s["number"], 16): s["addresses"] for s in net["scenes"]}
    for num, s in sorted(d.scenes.items()):
        w(
            f"| {num} | {a.name('Scene', s['name'], key=str(num))} | {s['icon']} | {', '.join(f'`{x}`' for x in sc_addr.get(num, [])) or '—'} |"
        )
    w("")

    # ---- nodes
    w("## Nodes\n")
    for n in sorted(net["nodes"], key=lambda n: int(n["unicastAddress"], 16)):
        base = int(n["unicastAddress"], 16)
        pid = int(n["pid"], 16) if n.get("pid") else None
        w(
            f"### `{n['unicastAddress']}` {d.node_name(n)}  (pid {n.get('pid')} = {PRODUCT_NAMES.get(pid, '?') if pid is not None else '?'}, vid {n.get('vid')})\n"
        )
        w(
            f"- UUID `{a.uuid(n['UUID'])}`; MAC `{a.mac(d.gatt.get(n['UUID'], {}).get('macAddress', {}).get('description', '?'))}`; "
            f"CID {n.get('cid')}; CRPL {n.get('crpl')}; devKey `{red(n['deviceKey'])}`"
        )
        w(
            f"- Features {n.get('features')}; TTL {n.get('defaultTTL')}; "
            f"netTx {n.get('networkTransmit')}; relayRetx {n.get('relayRetransmit')}; security {n.get('security')}"
        )
        nf = d.notif.get(base, {})
        if nf:
            af = nf.get(0, {}).get("actuatorFunction")
            dl = nf.get(1, {}).get("deviceLayout")
            prop = nf.get(4, {})
            bits = []
            if af:
                bits.append(
                    f"actuatorFunction {af['actuatorFunctionId']} ({ACTUATOR_FUNCTION.get(af['actuatorFunctionId'], '?')}), insertType {af['insertType']}"
                )
            if dl is not None:
                bits.append(f"deviceLayout {dl} ({DEVICE_LAYOUT.get(dl, '?')})")
            if prop:
                bits.append(
                    f"property {prop['propertyId']} = `{prop['value']}` (Device Software Revision?)"
                )
            w("- Cached state: " + "; ".join(bits))
        for key, meta in d.devices.get(n["UUID"], []):
            w(
                f"- App device **{a.name('Device', meta['name'], key=n['UUID'] + chr(0) + meta['name'])}**{' ★' if meta['isFavorite'] else ''}: locations {sorted(key['locationIds'])}, "
                f"actuatorFunction {key['actuatorFunction']}"
            )
            for c in meta.get("cachedGroupConnectionMetadata", []):
                ga = f"{c['groupAddress']:04X}"
                room = d.room_name(ga, d.rooms.get(ga, {}).get("name", "?"))
                w(
                    f"  - group connection: element {c['elementAddress']:04X} in group `{c['groupAddress']:04X}` ({room}) "
                    f"publishes to `{c['publishAddress']:04X}`, fn {c['groupActuatorFunction']}"
                )
        w("")
        for e in n["elements"]:
            addr = base + e["index"]
            loc = int(e["location"], 16)
            enf = d.notif.get(addr, {})
            state = []
            if 3 in enf:
                state.append(
                    f"keyMode={enf[3]['mode']} ({KEY_MODE.get(enf[3]['mode'], '?')})"
                )
            if 2 in enf:
                state.append(f"scene={enf[2]['sceneNumber']}")
            w(
                f"#### Element {e['index']} @ `{addr:04X}` — location `{e['location']}` ({LOCATION_ROLE.get(loc, '?')})"
                + (f" — cached {' '.join(state)}" if state else "")
            )
            w("")
            w("| Model | Bind | Publish | Subscribe |")
            w("|---|---|---|---|")
            for m in e["models"]:
                pub = m.get("publish")
                pub_s = (
                    d.group_label(pub["address"])
                    if pub and pub["address"] != "FFFF"
                    else (f"`{pub['address']}` (all nodes)" if pub else "")
                )
                if pub and (
                    pub.get("ttl", 255) != 255
                    or pub["period"]["numberOfSteps"]
                    or pub["retransmit"]["count"]
                ):
                    pub_s += f" ttl={pub['ttl']} period={pub['period']} retx={pub['retransmit']}"
                subs = "<br>".join(d.group_label(s) for s in m["subscribe"])
                w(
                    f"| `{m['modelId']}` {model_name(m['modelId'])} | {m['bind']} | {pub_s} | {subs} |"
                )
            w("")

    # ---- wiring summary
    w("## Wiring summary (who controls what)\n")
    w(
        "Every element gets its own group address (`element group #0x<elementAddr>`). "
        "A load element's server models subscribe to its own group, its room group(s), the device-type group, "
        "and the element groups of buttons that were 'connected' to it. Button elements publish to their own "
        "element group or straight to a load's element group.\n"
    )
    w("| Load element | Node | Subscribed to |")
    w("|---|---|---|")
    for element, (n, e) in sorted(d.elements.items()):
        onoff = next((m for m in e["models"] if m["modelId"] in ("1000", "1300")), None)
        if not onoff:
            continue
        labels = [d.group_label(s) for s in onoff["subscribe"]]
        w(
            f"| `{element:04X}` loc {e['location']} | {d.node_name(n)} `{n['unicastAddress']}` | {'<br>'.join(labels)} |"
        )
    w("")
    w("| Button element | Node | Publishes (OnOff client) to |")
    w("|---|---|---|")
    for element, (n, e) in sorted(d.elements.items()):
        cli = next((m for m in e["models"] if m["modelId"] == "1001"), None)
        if not cli or int(e["location"], 16) < 0x40:
            continue
        pub = cli.get("publish")
        w(
            f"| `{element:04X}` loc {e['location']} | {d.node_name(n)} `{n['unicastAddress']}` | "
            f"{d.group_label(pub['address']) if pub else '—'} (subs: {', '.join(cli['subscribe']) or '—'}) |"
        )
    w("")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"the app container (default {DEFAULT_ROOT.relative_to(ROOT)})",
    )
    ap.add_argument(
        "--keys",
        action="store_true",
        help="print the keys in clear (needs --out, outside docs/; the file is created 0600)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        help="write the report here instead of stdout",
    )
    ap.add_argument(
        "--anonymise",
        action="store_true",
        help="pseudonymise MACs, UUIDs and every name (for a page that is published); refuses --keys",
    )
    ap.add_argument(
        "--salt",
        help="with --anonymise: a private string that makes the address pseudonyms repeatable across runs"
        " (default: random per run; never commit it, it is what keeps the pseudonyms from being inverted)",
    )
    return ap


def key_output_refusal(out: Path | None) -> str | None:
    """Why `--keys` may not go to `out` (None: it may): stdout could be any redirect, docs/ is tracked."""
    if out is None:
        return (
            "--keys needs --out: a shell redirect could land the keys in a tracked file"
        )
    try:
        out.resolve().relative_to(DOCS)
    except ValueError:
        return None
    return (
        f"--keys refused for {out}: docs/ is tracked and published, keys never go there"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.root
    if not (root / "Documents/MeshNetwork.json").is_file():
        sys.exit(
            f"{root} holds no Documents/MeshNetwork.json: pass the app container directory"
            " (…/AppDomain-de.jung.junghome, see docs/ios-app-data.md)"
        )
    if args.anonymise and args.keys:
        sys.exit(
            "--anonymise and --keys together: an anonymised report never carries keys"
        )
    if args.salt is not None and not args.anonymise:
        sys.exit("--salt only means something with --anonymise")
    if args.keys and (why := key_output_refusal(args.out)) is not None:
        sys.exit(why)
    anon = (
        Anonymiser(None if args.salt is None else args.salt.encode())
        if args.anonymise
        else None
    )
    text = render(Dump(root, anon), args.keys)
    if args.out is None:
        sys.stdout.write(text)
        return 0
    out: Path = args.out
    if args.keys:  # owner-readable only, whatever the umask
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        out.chmod(0o600)
    else:
        out.write_text(text, encoding="utf-8")
    print(f"written to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
