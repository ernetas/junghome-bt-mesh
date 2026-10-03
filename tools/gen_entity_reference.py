#!/usr/bin/env python3
"""Generate the user guide's entity reference (docs/user/entities.md) from what the integration really registers.

    tools/gen_entity_reference.py                 # rewrite docs/user/entities.md
    tools/gen_entity_reference.py --out FILE      # write it elsewhere
    tools/gen_entity_reference.py --check         # exit 1 when the committed page is not what this generates

Nothing here is written by hand, so the page cannot drift from the code. The names are the translated entity names
of `strings.json`; everything else comes from the registry snapshot (`tests/snapshots/test_snapshots.ambr`,
`test_registry_identity`), the reviewed record of what every synthetic fixture network registers — per entity its
platform, translation key, category, `disabled_by` and the device it sits on — and from the fixture exports
themselves, which say which JUNG product each device belongs to (`PRODUCT_NAMES` of `entity.py`, read without
importing Home Assistant). A translation key with a `_key` twin (`input_state` / `input_state_key`: one key of a
device with several gets its letter in the name) is one row. A key that no fixture network registers is still
listed, marked so (`tests/test_docs_reference.py` fails on one).

The output is the same for the same inputs: rows sorted by name, sets joined in sorted order, no dates.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jhmesh.cdb import CDB
from jhmesh.devices import BATTERY_PIDS, GATEWAY_PID

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = Path("custom_components/junghome_ble")
STRINGS = INTEGRATION / "strings.json"
ENTITY_PY = INTEGRATION / "entity.py"
SNAPSHOT = Path("tests/snapshots/test_snapshots.ambr")
FIXTURES = Path("tests/fixtures")
OUT = Path("docs/user/entities.md")
# the fixture network of every `test_registry_identity[...]` block: tests/test_snapshots.py NETWORKS, which
# tests/test_docs_reference.py holds this to
NETWORKS: dict[str, Path] = {
    "base": FIXTURES / "MeshNetwork.json",
    "blinds": FIXTURES / "Blinds.json",
    "rtr": FIXTURES / "MeshNetwork-rtr.json",
    "detectors": FIXTURES / "MeshNetwork-detectors.json",
    "puck": FIXTURES / "MeshNetwork-puck.json",
    "android": FIXTURES / "JungHome-android.json",
    "gateway": FIXTURES / "JungHome.json",
}
# the platforms in the order a reader meets them, each with its heading and the reference section it is explained in
PLATFORMS: dict[str, tuple[str, str]] = {
    "light": ("Lights", "light"),
    "switch": ("Switches", "switch"),
    "cover": ("Covers (blinds)", "cover"),
    "climate": ("Climate (room thermostats)", "climate"),
    "sensor": ("Sensors", "sensor"),
    "binary_sensor": ("Binary sensors", "binary-sensor"),
    "event": ("Events (keys and inputs)", "event"),
    "scene": ("Scenes", "scene"),
    "button": ("Buttons", "device-parameters-number-select-switch-button"),
    "number": ("Numbers", "device-parameters-number-select-switch-button"),
    "select": ("Selects", "device-parameters-number-select-switch-button"),
    "update": ("Updates", "firmware"),
}
REFERENCE = "../ha-integration.md"
KEY_TWIN = "_key"
# the typography of the page: an empty cell, and the marks around a placeholder of a name
DASH = "\N{EN DASH}"
OPEN, CLOSE = (
    "\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}",
    "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}",
)
CATEGORIES = {"None": DASH, "config": "Configuration", "diagnostic": "Diagnostic"}
# what an entity without a translation key is named after
UNNAMED = {"scene": "*(the scene's name)*"}
UNNAMED_DEFAULT = "*(the device's name)*"
BLOCK = re.compile(
    r"^# name: test_registry_identity\[(?P<network>[\w-]+)\]\n(?P<body>.*?)^# ---$",
    re.MULTILINE | re.DOTALL,
)
ITEM = re.compile(r"^\s+'(?P<line>.*)',$")
ENTITY = re.compile(
    r"(?P<uid>\S+) (?P<platform>\w+) key=(?P<key>\S+) category=(?P<category>\S+) "
    r"disabled_by=(?P<disabled>\S+) device=(?P<device>.+)"
)
PLACEHOLDER = re.compile(r"\{(\w+)\}")
LOAD_KINDS = {
    "light": "Light device",
    "switch": "Socket device",
    "cover": "Blind device",
}


@dataclass
class Row:
    """One entity of the reference: its names and where the fixture networks register it."""

    platform: str
    key: str | None
    names: list[str]
    states: list[str] = field(default_factory=list)
    categories: set[str] = field(default_factory=set)
    enabled: set[bool] = field(default_factory=set)
    sits_on: set[str] = field(default_factory=set)
    products: set[int] = field(default_factory=set)

    @property
    def registered(self) -> bool:
        """Whether a fixture network registers it at all."""
        return bool(self.enabled)


@dataclass(frozen=True)
class Registered:
    """One line of a `test_registry_identity` block: an entity and the device it sits on."""

    network: str
    uid: str
    platform: str
    key: str | None
    category: str
    enabled: bool
    device: str


def display(name: str) -> str:
    """A translated name as the page shows it: a placeholder (`{key}`, `{room}`) between angle quotation marks."""
    return PLACEHOLDER.sub(lambda m: f"{OPEN}{m[1]}{CLOSE}", name)


def product_names(root: Path) -> dict[int, str]:
    """`entity.PRODUCT_NAMES`, read from the source (importing it would import Home Assistant)."""
    tree = ast.parse((root / ENTITY_PY).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [
            t.id for t in node.targets if isinstance(t, ast.Name)
        ] == ["PRODUCT_NAMES"]:
            value: dict[int, str] = ast.literal_eval(node.value)
            return value
    raise SystemExit(f"{ENTITY_PY} defines no PRODUCT_NAMES")


def registered(root: Path) -> list[Registered]:
    """Every entity line of every `test_registry_identity` block of the snapshot."""
    text = (root / SNAPSHOT).read_text(encoding="utf-8")
    out: list[Registered] = []
    for block in BLOCK.finditer(text):
        for raw in block["body"].splitlines():
            item = ITEM.match(raw)
            entity = ENTITY.fullmatch(item["line"]) if item else None
            if entity is None:
                continue  # a device line, or the block's structure
            out.append(
                Registered(
                    network=block["network"],
                    uid=entity["uid"],
                    platform=entity["platform"],
                    key=None if entity["key"] == "None" else entity["key"],
                    category=entity["category"],
                    enabled=entity["disabled"] == "None",
                    device=entity["device"].split(" ")[0],
                )
            )
    return out


def node_products(root: Path, networks: dict[str, Path]) -> dict[str, int]:
    """Node UUID (lower case, as the registry holds it) → JUNG product id, over every fixture network."""
    out: dict[str, int] = {}
    for path in networks.values():
        for node in CDB.load(root / path).nodes:
            if node.pid is not None:
                out[node.uuid.lower()] = node.pid
    return out


def device_kind(entity: Registered, mains: dict[tuple[str, str], str]) -> str:
    """What kind of device an entity sits on, from the device identifier's shape."""
    ident = entity.device.removeprefix("junghome_ble:")
    if ident == "-":
        return "no device"
    if ident.startswith("mesh:"):
        return "Mesh network device"
    if ident.startswith("node:"):
        return "Node device"
    if ident.endswith("-buttons"):
        return "Push-buttons device"
    return mains.get((entity.network, ident), "Load device")


def device_node(entity: Registered) -> str | None:
    """The node UUID an entity's device belongs to; None for the mesh device and none at all."""
    ident = entity.device.removeprefix("junghome_ble:").removeprefix("node:")
    match = re.match(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ident
    )
    if match is None or entity.device.startswith("junghome_ble:mesh:"):
        return None
    return match[0]


def build_rows(
    strings: dict[str, Any], entities: list[Registered], pids: dict[str, int]
) -> dict[str, list[Row]]:
    """The rows per platform: every translated name, joined with where the fixture networks register it."""
    rows: dict[tuple[str, str | None], Row] = {}
    for platform, keys in strings.get("entity", {}).items():
        for key, value in keys.items():
            base = key.removesuffix(KEY_TWIN)
            if key != base and base in keys:
                continue  # folded into its twin's row below
            names = [str(value.get("name"))] if value.get("name") else [UNNAMED_DEFAULT]
            twin = keys.get(key + KEY_TWIN)
            if twin is not None:
                names.append(str(twin["name"]))
            states = sorted({str(s) for s in value.get("state", {}).values()})
            rows[(platform, key)] = Row(platform, key, names, states)
    # the main entity of a device (no translation key): named after its device
    for entity in entities:
        if entity.key is None and (entity.platform, None) not in rows:
            name = UNNAMED.get(entity.platform, UNNAMED_DEFAULT)
            rows[(entity.platform, None)] = Row(entity.platform, None, [name])
    # a load's device is named by its main entity, the one whose unique id is the device's identifier
    mains = {
        (e.network, e.uid): LOAD_KINDS[e.platform]
        for e in entities
        if e.platform in LOAD_KINDS and e.device == f"junghome_ble:{e.uid}"
    }
    for entity in entities:
        key = entity.key
        if key is not None and key.endswith(KEY_TWIN):
            key = key.removesuffix(KEY_TWIN)
        row = rows.get((entity.platform, key))
        if row is None:
            continue  # a key strings.json lacks: tests/test_translations.py reports it
        row.categories.add(CATEGORIES.get(entity.category, entity.category))
        row.enabled.add(entity.enabled)
        row.sits_on.add(device_kind(entity, mains))
        node = device_node(entity)
        if node is not None and node in pids:
            row.products.add(pids[node])
    per_platform: dict[str, list[Row]] = defaultdict(list)
    for row in rows.values():
        per_platform[row.platform].append(row)
    return {
        p: sorted(rs, key=lambda r: (r.names[0].lower(), r.key or ""))
        for p, rs in per_platform.items()
    }


def products_cell(products: set[int], universe: set[int], names: dict[int, str]) -> str:
    """The products an entity appears on, summarised when it is (almost) every product of the fixture networks."""
    if not products:
        return DASH
    mains = universe - BATTERY_PIDS
    summaries = (
        (universe, "every device"),
        (universe - {GATEWAY_PID}, "every device but the gateway"),
        (mains, "every mains-powered device"),
        (mains - {GATEWAY_PID}, "every mains-powered device but the gateway"),
    )
    for group, text in summaries:
        if products == group:
            return text
    return ", ".join(sorted(names.get(pid, f"Product {pid}") for pid in products))


def enabled_cell(enabled: set[bool]) -> str:
    if enabled == {True}:
        return "Yes"
    if enabled == {False}:
        return "No"
    return "Depends on the device" if enabled else DASH


def render(
    rows: dict[str, list[Row]], universe: set[int], names: dict[int, str]
) -> str:
    """The Markdown page."""
    out = [
        "# Entity reference",
        "",
        "<!-- Generated by tools/gen_entity_reference.py from strings.json and the registry snapshot of the test",
        "     networks; do not edit by hand: run the tool and commit the result. -->",
        "",
        "Every entity the integration can create, by type. What each one does is explained in the",
        f"[reference]({REFERENCE}) section linked under each heading; this page is the complete list.",
        "",
        "- **Category**: *Configuration* entities sit in the device page's configuration block, *Diagnostic* ones in",
        "  its diagnostic block; neither shows up on automatically generated dashboards.",
        "- **On by default**: *No* means the entity exists but is disabled; enable it on the entity's settings page",
        "  (*Settings → Devices & services → Entities*, pick it, *Enabled*).",
        "- **Sits on**: the Home Assistant device the entity belongs to. Every JUNG device has a *node device*; its",
        "  lights, sockets and blinds have a device each, and its keys a *push-buttons device* per gang. The *mesh",
        "  network device* stands for the whole installation.",
        "- **Devices**: the JUNG products the entity appears on in the integration's test networks.",
        (
            f"- {display('{key}')} stands for a key's letter (A to D) or a mini actuator's input (E1, E2); "
            f"{display('{room}')} for a room's name."
        ),
        "",
    ]
    for platform, (heading, anchor) in PLATFORMS.items():
        if platform not in rows:
            continue
        platform_rows = rows[platform]
        with_states = any(r.states for r in platform_rows)
        out += [
            f"## {heading}",
            "",
            f"Explained in [{heading}]({REFERENCE}#{anchor}).",
            "",
        ]
        header = ["Name", "Category", "On by default", "Sits on", "Devices"]
        if with_states:
            header.append("Values")
        out += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for row in platform_rows:
            if row.registered:
                cells = [
                    "<br>".join(display(n) for n in row.names),
                    ", ".join(sorted(row.categories)),
                    enabled_cell(row.enabled),
                    ", ".join(sorted(row.sits_on)),
                    products_cell(row.products, universe, names),
                ]
            else:
                cells = [
                    "<br>".join(display(n) for n in row.names),
                    DASH,
                    DASH,
                    "not registered on any test network",
                    DASH,
                ]
            if with_states:
                cells.append(", ".join(row.states) or DASH)
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        out.append("")
    return "\n".join(out)


def generate(root: Path) -> str:
    """The page for the tree at `root`."""
    strings = json.loads((root / STRINGS).read_text(encoding="utf-8"))
    entities = registered(root)
    pids = node_products(root, NETWORKS)
    names = product_names(root)
    universe = {pid for pid in pids.values() if pid in names}
    return render(build_rows(strings, entities, pids), universe, names)


def unregistered(root: Path) -> list[str]:
    """`platform.key` of every translated entity no fixture network registers (the page marks them)."""
    strings = json.loads((root / STRINGS).read_text(encoding="utf-8"))
    rows = build_rows(strings, registered(root), node_products(root, NETWORKS))
    return sorted(
        f"{row.platform}.{row.key}"
        for rs in rows.values()
        for row in rs
        if not row.registered
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--root", default=str(ROOT), help="repository root (default: this checkout)"
    )
    ap.add_argument(
        "--out", help=f"where to write the page (default: {OUT} under the root)"
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="only compare with the committed page; exit 1 when it differs",
    )
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    page = generate(root)
    out = Path(args.out) if args.out else root / OUT
    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else None
        if current != page:
            print(
                f"{out} is out of date: run tools/gen_entity_reference.py",
                file=sys.stderr,
            )
            return 1
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
