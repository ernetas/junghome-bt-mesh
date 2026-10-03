"""tools/gen_entity_reference.py: the user guide's entity reference cannot drift from `strings.json` and the registry.

The committed `docs/user/entities.md` must be what the tool generates now; every translated entity name must be on
it; and every translated entity must be registered on a fixture network (or the page could only say "not
registered"), which `tests/test_snapshots.py` pins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import gen_entity_reference as gen

from .test_snapshots import NETWORKS

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / gen.OUT


def test_committed_page_is_what_the_tool_generates(tmp_path: Path) -> None:
    """Regenerate into a temporary file: byte for byte the committed page (run the tool and commit when this fails)."""
    out = tmp_path / "entities.md"
    assert gen.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == PAGE.read_text(encoding="utf-8")
    assert gen.generate(ROOT) == gen.generate(
        ROOT
    )  # nothing in it depends on order, time or chance


def test_every_translated_entity_name_is_on_the_page() -> None:
    page = PAGE.read_text(encoding="utf-8")
    strings = json.loads((ROOT / gen.STRINGS).read_text(encoding="utf-8"))
    missing = [
        f"{platform}.{key}: {value['name']}"
        for platform, keys in strings["entity"].items()
        for key, value in keys.items()
        if value.get("name") and gen.display(value["name"]) not in page
    ]
    assert missing == []


def test_every_translated_entity_is_registered_on_a_fixture_network() -> None:
    """A name no fixture network registers would be listed without category, default or device: add a fixture."""
    assert gen.unregistered(ROOT) == []


def test_the_tool_reads_every_network_the_registry_snapshot_pins() -> None:
    assert {name: Path(path).resolve() for name, (path, _meta) in NETWORKS.items()} == {
        name: (ROOT / path).resolve() for name, path in gen.NETWORKS.items()
    }


def test_check_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gen.main(["--check"]) == 0  # the committed page
    stale = tmp_path / "entities.md"
    stale.write_text("# Entity reference\n", encoding="utf-8")
    assert gen.main(["--check", "--out", str(stale)]) == 1
    assert "out of date" in capsys.readouterr().err
    assert gen.main(["--check", "--out", str(tmp_path / "missing.md")]) == 1
    assert gen.main(["--out", str(tmp_path / "new" / "entities.md")]) == 0
    assert (tmp_path / "new" / "entities.md").exists()


def entity(
    uid: str,
    platform: str,
    key: str | None,
    device: str,
    *,
    enabled: bool = True,
    category: str = "None",
) -> gen.Registered:
    return gen.Registered("net", uid, platform, key, category, enabled, device)


NODE = "00005eff-fe00-5300-0000-000000000000"


def test_rows_fold_twins_and_name_main_entities() -> None:
    strings = {
        "entity": {
            "sensor": {
                "key_mode": {"name": "Key mode", "state": {"a": "Light", "b": "Blind"}},
                "key_mode_key": {"name": "Key mode {key}"},
                "lonely": {"name": "Lonely"},
            },
            "cover": {"blind": {"name": None}},
        }
    }
    entities = [
        entity(
            f"{NODE}-0040-key_mode",
            "sensor",
            "key_mode_key",
            f"junghome_ble:{NODE}-0040-buttons",
            enabled=False,
            category="diagnostic",
        ),
        entity(f"{NODE}-0001", "cover", "blind", f"junghome_ble:{NODE}-0001"),
        entity(
            f"{NODE}-0001-x", "sensor", "not_translated", f"junghome_ble:{NODE}-0001"
        ),
        entity("scene-1", "scene", None, "-"),
        entity(
            f"{NODE}-other",
            "sensor",
            "lonely",
            "junghome_ble:node:ffffffff-ffff-ffff-ffff-ffffffffffff",
        ),
    ]
    rows = gen.build_rows(strings, entities, {NODE: 0x01})
    sensor = {row.key: row for row in rows["sensor"]}
    assert sensor["key_mode"].names == ["Key mode", "Key mode {key}"]
    assert sensor["key_mode"].states == ["Blind", "Light"]
    assert sensor["key_mode"].sits_on == {"Push-buttons device"}
    assert sensor["key_mode"].products == {0x01}
    assert sensor["key_mode"].enabled == {False}
    assert sensor["lonely"].products == set()  # a node no fixture names
    assert (
        "not_translated" not in sensor
    )  # tests/test_translations.py reports such a key
    assert rows["cover"][0].names == [gen.UNNAMED_DEFAULT]
    assert rows["cover"][0].sits_on == {"Blind device"}
    assert rows["scene"][0].names == [gen.UNNAMED["scene"]]
    assert rows["scene"][0].sits_on == {"no device"}

    page = gen.render(rows, {0x01}, {0x01: "Push-button 1-gang"})
    assert "## Sensors" in page
    assert "## Lights" not in page  # a platform without rows has no section
    key_mode = gen.display("Key mode {key}")
    assert (
        f"| Key mode<br>{key_mode} | Diagnostic | No | Push-buttons device | every device | Blind, Light |"
        in page
    )
    unregistered = gen.Row("sensor", "ghost", ["Ghost"])
    page = gen.render({"sensor": [unregistered]}, set(), {})
    dash = gen.DASH
    assert (
        f"| Ghost | {dash} | {dash} | not registered on any test network | {dash} |\n"
        in page
    )


def test_cells() -> None:
    names = {1: "Push-button 1-gang", 5: "Wall transmitter 1-gang", 0x0B: "Gateway"}
    universe = {1, 5, 0x0B}
    assert gen.products_cell(set(), universe, names) == gen.DASH
    assert gen.products_cell({1, 5, 0x0B}, universe, names) == "every device"
    assert gen.products_cell({1, 5}, universe, names) == "every device but the gateway"
    assert gen.products_cell({1, 0x0B}, universe, names) == "every mains-powered device"
    assert gen.products_cell({1}, {1, 2, 5, 0x0B}, names) == "Push-button 1-gang"
    assert (
        gen.products_cell({1}, {1, 5, 0x0B}, names)
        == "every mains-powered device but the gateway"
    )
    assert (
        gen.products_cell({99, 1}, {1, 2, 99, 5}, names)
        == "Product 99, Push-button 1-gang"
    )
    assert gen.enabled_cell({True}) == "Yes"
    assert gen.enabled_cell({False}) == "No"
    assert gen.enabled_cell({True, False}) == "Depends on the device"
    assert gen.enabled_cell(set()) == gen.DASH
    assert (
        gen.enabled_cell({True}, {True}) == "Yes, hidden"
    )  # a room's central entities
    assert gen.enabled_cell({True}, {False}) == "Yes"


def test_product_names_come_from_the_source(tmp_path: Path) -> None:
    assert gen.product_names(ROOT)[0x03] == "Socket (metering)"
    fake = tmp_path / gen.ENTITY_PY
    fake.parent.mkdir(parents=True)
    fake.write_text("OTHER = {1: 'x'}\nA, B = 1, 2\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no PRODUCT_NAMES"):
        gen.product_names(tmp_path)


def test_registered_reads_only_the_identity_blocks(tmp_path: Path) -> None:
    snapshot = tmp_path / gen.SNAPSHOT
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        "# serializer version: 1\n"
        "# name: test_devices[x]\n"
        "  'a sensor key=k category=None disabled_by=None device=-',\n"
        "# ---\n"
        "# name: test_registry_identity[base]\n"
        "  dict({\n"
        "    'devices': list([\n"
        "      'junghome_ble:mesh:m connections=- via=-',\n"
        "    ]),\n"
        "    'entities': list([\n"
        "      'u light key=None category=None disabled_by=integration device=junghome_ble:a junghome_ble:b',\n"
        "      'h light key=room_lights category=None disabled_by=None hidden_by=integration device=junghome_ble:m',\n"
        "    ]),\n"
        "  })\n"
        "# ---\n",
        encoding="utf-8",
    )
    assert gen.registered(tmp_path) == [
        gen.Registered("base", "u", "light", None, "None", False, "junghome_ble:a"),
        gen.Registered(
            "base", "h", "light", "room_lights", "None", True, "junghome_ble:m", True
        ),
    ]
