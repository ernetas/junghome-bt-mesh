"""tools/mesh_report.py: the topology report of an app dump — argparse, redaction, where `--keys` may go."""

from __future__ import annotations

import base64
import json
import re
import shutil
import stat
from pathlib import Path

import pytest

from tests.helpers import MAC_LIGHT_SWITCH
from tools import mesh_report

FIXTURES = Path(__file__).parent / "fixtures"


def _b64(doc: dict) -> str:
    return base64.b64encode(json.dumps(doc).encode()).decode()


@pytest.fixture
def dump(tmp_path: Path) -> Path:
    """An app container with the fixture CDB and just enough of the app's caches for every section to render."""
    root = tmp_path / "AppDomain-de.jung.junghome"
    (root / "Documents").mkdir(parents=True)
    shutil.copy(FIXTURES / "MeshNetwork.json", root / "Documents/MeshNetwork.json")
    aps = root / "Library/Application Support"
    aps.mkdir(parents=True)
    for name in ("device_metadata.json", "scene_metadata.json"):
        shutil.copy(FIXTURES / "Application Support" / name, aps / name)
    node_uuid = json.loads((root / "Documents/MeshNetwork.json").read_text())[
        "meshNetwork"
    ]["nodes"][0]["UUID"]
    (aps / "groups.json").write_text(
        json.dumps([{"address": 0xC00F, "name": "WC", "isFavorite": True}])
    )
    (aps / "node_gattdata.json").write_text(
        json.dumps([node_uuid, {"macAddress": {"description": MAC_LIGHT_SWITCH}}])
    )
    cache = [
        "a",
        {
            "notificationType": 0,
            "notificationAsJson": _b64(
                {
                    "elementAddress": 0x0148,
                    "actuatorFunction": {"actuatorFunctionId": 0, "insertType": 2},
                }
            ),
        },
        "b",
        {
            "notificationType": 1,
            "notificationAsJson": _b64({"elementAddress": 0x0148, "deviceLayout": 1}),
        },
        "c",
        {
            "notificationType": 4,
            "notificationAsJson": _b64(
                {"elementAddress": 0x0148, "propertyId": 5, "value": "2.2.0.2"}
            ),
        },
        "d",
        {
            "notificationType": 3,
            "notificationAsJson": _b64({"elementAddress": 0x0149, "mode": 2}),
        },
        "e",
        {
            "notificationType": 2,
            "notificationAsJson": _b64({"elementAddress": 0x0149, "sceneNumber": 1}),
        },
    ]
    (aps / "meshnotificationcache.json").write_text(json.dumps(cache))
    (aps / "element_connection_groups.json").write_text("[]")
    (aps / "device_type_groups.json").write_text(
        json.dumps([{"groupAddress": 0xFEF5, "elements": [0x0148, ["1000"]]}])
    )
    return root


def fixture_keys() -> list[str]:
    net = json.loads((FIXTURES / "MeshNetwork.json").read_text())["meshNetwork"]
    return [
        net["netKeys"][0]["key"],
        net["appKeys"][0]["key"],
        *(n["deviceKey"] for n in net["nodes"]),
    ]


def test_report_redacts_every_key_completely(
    dump: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """Not even a prefix: the tracked docs/network-topology.md used to show 24 bits of every live key."""
    out = tmp_path / "report.md"
    assert mesh_report.main([str(dump), "--out", str(out)]) == 0
    text = out.read_text()
    assert text.startswith("# JUNG HOME mesh network: ")
    assert text.count("[redacted]") == len(fixture_keys())
    for key in fixture_keys():
        assert key not in text
        assert (
            key[:4] not in text.split("devKey `")[1][:8] if "devKey `" in text else True
        )
    assert "NetKey[0] `[redacted]`" in text
    assert "devKey `[redacted]`" in text
    assert "| `C00F` | `id49167` | WC | ★ |" in text
    assert "actuatorFunction 0 (Switch)" in text
    assert "keyMode=2 (Scene)" in text
    assert f"MAC `{MAC_LIGHT_SWITCH}`" in text
    assert f"written to {out}" in capsys.readouterr().err
    # stdout when no --out is given: the same text
    assert mesh_report.main([str(dump)]) == 0
    assert capsys.readouterr().out == text


def test_keys_go_only_to_an_explicit_private_file_outside_docs(
    dump: Path, tmp_path: Path
):
    with pytest.raises(SystemExit, match="--keys needs --out"):
        mesh_report.main([str(dump), "--keys"])
    tracked = mesh_report.DOCS / "network-topology.md"
    before = tracked.read_bytes() if tracked.exists() else None
    with pytest.raises(SystemExit, match=r"refused for .*docs/ is tracked"):
        mesh_report.main([str(dump), "--keys", "--out", str(tracked)])
    with pytest.raises(SystemExit, match="refused"):
        mesh_report.main(
            [str(dump), "--keys", "--out", str(mesh_report.DOCS / "sub" / "x.md")]
        )
    assert (tracked.read_bytes() if tracked.exists() else None) == before
    assert (
        mesh_report.key_output_refusal(
            mesh_report.ROOT / "docs" / ".." / "docs" / "x.md"
        )
        is not None
    )
    assert mesh_report.key_output_refusal(mesh_report.ROOT / "docs-private.md") is None
    out = tmp_path / "keys.md"
    out.write_text("old")
    out.chmod(0o644)
    assert mesh_report.main([str(dump), "--keys", "--out", str(out)]) == 0
    text = out.read_text()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "[redacted]" not in text
    for key in fixture_keys():
        assert key in text


def test_help_and_a_missing_dump_are_one_line_each(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    with pytest.raises(SystemExit) as exc:
        mesh_report.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: " in out
    assert "--keys" in out
    assert "ios/AppDomain-de.jung.junghome" in out
    with pytest.raises(
        SystemExit,
        match=r"holds no Documents/MeshNetwork\.json: pass the app container",
    ):
        mesh_report.main([str(tmp_path / "nowhere")])
    assert mesh_report.model_name("05271016").startswith("JH Scheduler")
    assert mesh_report.model_name("1234ffff") == "vendor 1234:FFFF"
    assert mesh_report.model_name("1000") == "Generic OnOff Server"
    assert mesh_report.model_name("abcd") == "SIG 0xabcd"


def test_report_renders_the_rarer_cache_and_topology_shapes(
    dump: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Partial node caches, a room link in the app's device cache, an element group whose element is gone and a
    publication with non-default parameters each render instead of being skipped."""
    cdb_path = dump / "Documents/MeshNetwork.json"
    doc = json.loads(cdb_path.read_text())
    net = doc["meshNetwork"]
    net["groups"].append(
        {"name": "element group #0x0999", "address": "C0FE", "parentAddress": "0000"}
    )
    first = net["nodes"][0]
    model = first["elements"][0]["models"][0]
    model["subscribe"] = [*model["subscribe"], "C0FE"]
    model["publish"] = {
        "address": "C00F",
        "index": 0,
        "ttl": 5,
        "credentials": 0,
        "period": {"numberOfSteps": 0, "resolution": 100},
        "retransmit": {"count": 0, "interval": 50},
    }
    cdb_path.write_text(json.dumps(doc))
    aps = dump / "Library/Application Support"
    cache = json.loads((aps / "meshnotificationcache.json").read_text())
    second = int(net["nodes"][1]["unicastAddress"], 16)
    third = int(net["nodes"][2]["unicastAddress"], 16)
    cache += [
        "f",
        {
            "notificationType": 1,
            "notificationAsJson": _b64({"elementAddress": second, "deviceLayout": 0}),
        },
        "g",
        {
            "notificationType": 0,
            "notificationAsJson": _b64(
                {
                    "elementAddress": third,
                    "actuatorFunction": {"actuatorFunctionId": 2, "insertType": 1},
                }
            ),
        },
    ]
    (aps / "meshnotificationcache.json").write_text(json.dumps(cache))
    devices = json.loads((aps / "device_metadata.json").read_text())
    devices[1]["cachedGroupConnectionMetadata"] = [
        {
            "elementAddress": 0x0149,
            "groupAddress": 0xC00F,
            "publishAddress": 0xC062,
            "groupActuatorFunction": "LIGHT",
        }
    ]
    (aps / "device_metadata.json").write_text(json.dumps(devices))
    assert mesh_report.main([str(dump)]) == 0
    out = capsys.readouterr().out
    assert "`C0FE`→el 0999 (gone)" in out
    assert "ttl=5 period=" in out
    assert "- Cached state: deviceLayout 0 (" in out
    assert "- Cached state: actuatorFunction 2 (" in out
    assert (
        "group connection: element 0149 in group `C00F` (WC) publishes to `C062`, fn LIGHT"
        in out
    )


def _names(root: Path) -> set[str]:
    """Every name in the dump that identifies the installation: rooms, scenes, nodes, app devices, provisioners."""
    net = json.loads((root / "Documents/MeshNetwork.json").read_text())["meshNetwork"]
    aps = root / "Library/Application Support"
    names = {net["meshName"], *(p["provisionerName"] for p in net["provisioners"])}
    names |= {n["name"] for n in net["nodes"]}
    names |= {g["name"] for g in net["groups"]}
    names |= {g["name"] for g in json.loads((aps / "groups.json").read_text())}
    for doc in ("scene_metadata.json", "device_metadata.json"):
        raw = json.loads((aps / doc).read_text())
        names |= {v["name"] for v in raw[1::2]}
    return {n for n in names if not n.startswith(mesh_report._GENERATED_GROUP)}


def test_anonymise_replaces_every_address_and_name_and_never_prints_a_key(
    dump: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    net = json.loads((dump / "Documents/MeshNetwork.json").read_text())["meshNetwork"]
    assert mesh_report.main([str(dump)]) == 0
    plain = capsys.readouterr().out
    assert mesh_report.main([str(dump), "--anonymise"]) == 0
    text = capsys.readouterr().out
    # the same report, only the identifiers differ: every line is still there
    assert len(text.splitlines()) == len(plain.splitlines())
    real_uuids = [net["meshUUID"], *(p["UUID"] for p in net["provisioners"])]
    real_uuids += [n["UUID"] for n in net["nodes"]]
    for uuid in real_uuids:
        assert uuid.upper() not in text.upper()
    assert MAC_LIGHT_SWITCH not in text
    names = _names(dump)
    assert {"WC", "WC off", "WC mirror", "Test network", "iPhone"} <= names
    for name in names:
        for shape in (
            f"` {name}  (pid",
            f"**{name}**",
            f"| {name} |",
            f"| {name} `",
            f"({name}",
        ):
            assert shape not in text, name
    for key in fixture_keys():
        assert key not in text
    assert text.count("[redacted]") == len(fixture_keys())
    # the vendor's public OUI stays, the rest of the MAC is a pseudonym (the UUID ↔ MAC pairing: the next test)
    mac = text.split("MAC `")[1].split("`")[0]
    assert mac.startswith(MAC_LIGHT_SWITCH[:9])
    assert len(mac) == len(MAC_LIGHT_SWITCH)
    # names become numbered generic ones: rooms by address, scenes by number, nodes by unicast address
    assert "| `C00F` | `id49167` | Room A | ★ |" in text
    assert "| 1 | Scene 1 | SceneAbsent |" in text
    assert "# JUNG HOME mesh network: Mesh 1" in text
    assert "- Provisioner **Provisioner 1** `" in text
    first = min(net["nodes"], key=lambda n: int(n["unicastAddress"], 16))
    assert f"### `{first['unicastAddress']}` Node 1  (pid" in text
    assert "- App device **Device 1**" in text
    # mesh addresses, product ids and model ids are not personal: untouched
    for kept in (
        "`C00F`",
        "actuatorFunction 0 (Switch)",
        "keyMode=2 (Scene)",
        "Generic OnOff Server",
    ):
        assert kept in text


def test_anonymise_is_deterministic_within_a_run_and_across_runs_only_with_a_salt(
    dump: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert mesh_report.main([str(dump), "--anonymise", "--salt", "s3cret"]) == 0
    one = capsys.readouterr().out
    assert mesh_report.main([str(dump), "--anonymise", "--salt", "s3cret"]) == 0
    assert capsys.readouterr().out == one
    assert mesh_report.main([str(dump), "--anonymise"]) == 0
    random_salt = capsys.readouterr().out
    assert random_salt != one  # the addresses differ ...

    def names_only(text: str) -> str:
        text = re.sub(
            r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
            "<uuid>",
            text,
            flags=re.IGNORECASE,
        )
        return re.sub(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", "<mac>", text)

    assert names_only(random_salt) == names_only(
        one
    )  # ... the names do not (numbered, not hashed)

    anon = mesh_report.Anonymiser(b"salt")
    assert anon.mac("30:FB:10:12:34:56") == anon.mac("30:fb:10:12:34:56")
    assert anon.mac("30:FB:10:12:34:56") != anon.mac("30:FB:10:12:34:57")
    assert anon.mac("30:FB:10:12:34:56").startswith("30:FB:10:")
    assert mesh_report.Anonymiser(b"other").mac("30:FB:10:12:34:56") != anon.mac(
        "30:FB:10:12:34:56"
    )
    # a locally administered (random, no vendor) address keeps nothing, and stays locally administered unicast
    local = anon.mac("2F:AC:F8:13:BF:42")
    assert not local.startswith("2F:AC:F8")
    assert not local.endswith(":13:BF:42")
    assert int(local[:2], 16) & 0x03 == 0x02
    eui = anon.uuid("30FB10FF-FE12-3456-0000-000000000000")
    assert eui == anon.uuid("30fb10ff-fe12-3456-0000-000000000000").upper()
    raw = eui.replace("-", "")
    assert raw[:6] + raw[10:16] == anon.mac("30:FB:10:12:34:56").replace(":", "")
    other = anon.uuid("0FAAECF7-BB00-4A1F-8974-FD4A96D9F560")
    assert other != "0FAAECF7-BB00-4A1F-8974-FD4A96D9F560"
    assert other[14] == "4"
    assert other[19] in "89AB"  # a well-formed version-4 UUID
    assert anon.uuid("not a uuid") == "not a uuid"
    assert anon.mac("?") == "?"
    rooms = [anon.name("Room", "Kitchen", key=f"C{i:03X}") for i in (1, 2, 1)]
    assert rooms == ["Room A", "Room B", "Room A"]
    assert mesh_report._letters(25) == "Z"
    assert mesh_report._letters(26) == "AA"


def test_anonymise_refuses_keys_and_a_salt_needs_it(dump: Path, tmp_path: Path) -> None:
    out = tmp_path / "private.md"
    with pytest.raises(SystemExit, match="--anonymise and --keys"):
        mesh_report.main([str(dump), "--anonymise", "--keys", "--out", str(out)])
    assert not out.exists()
    with pytest.raises(SystemExit, match="--salt only"):
        mesh_report.main([str(dump), "--salt", "x"])
