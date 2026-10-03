"""jhmesh.export: CDB / project-file writer — round trips, minimal diffs, the §8.3 mutators and the write guard."""

from __future__ import annotations

import base64
import difflib
import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from jhmesh.cdb import CDB, InvalidExport
from jhmesh.devices import Metadata
from jhmesh.export import (
    ALLOCATION_MARGIN,
    DEFAULT_GROUP_ICON,
    DEFAULT_SCENE_ICON,
    META_KEYS,
    RENAME_MAX_LENGTH,
    AllocationCrowded,
    ExportError,
    InvalidName,
    Layout,
    ModelChange,
    NewerExportError,
    ProjectFile,
    Style,
    check_name,
    element_group_address,
    guess_actuator_function,
    guess_insert_type,
    is_room,
    mac_from_uuid,
    name_length,
    now_timestamp,
    suffixed_name,
    timestamp_advanced,
    write_private,
    write_private_with_backup,
)
from jhmesh.fileio import (
    BACKUP_GENERATIONS,
    backup_paths,
    fsync_dir,  # the directory-fsync helper only the writers otherwise reach
)

from .conftest import (
    CDB_PATH,
    FIXTURES,
    GATEWAY,
    GROUP_LIVING,
    GROUP_WC,
    LIGHT_2G,
    META_DIR,
    PHONE,
    PROXY_NODE,
    SOCKET,
)

SHARE_PATH = FIXTURES / "JungHome.json"
ANDROID_PATH = FIXTURES / "JungHome-android.json"
DIMMER = 0x0300
DIMMER_BUTTON = 0x0301
DIMMER_BUTTON_GROUP = 0xC071
ACTUATOR = 0x0400
GROUP_KITCHEN = 0xC011
NETKEY_HEX = "00112233445566778899aabbccddeeff"
UUID_1G = "00005EFF-FE00-5314-0000-000000000000"  # the proxy node (0148)
FIXED_NOW = datetime(2026, 3, 1, 12, 30, 45, tzinfo=UTC)


def views(cdb: CDB) -> dict[str, Any]:
    """Everything the loader derives, in a comparable shape (dataclass equality would recurse node <-> element)."""
    return {
        "uuid": cdb.mesh_uuid,
        "net_keys": cdb.net_keys,
        "app_keys": cdb.app_keys,
        "nodes": [(n.uuid, n.name, n.unicast, n.dev_key, n.pid) for n in cdb.nodes],
        "elements": {
            e.address: (e.location, e.models) for n in cdb.nodes for e in n.elements
        },
        "pubsub": {
            (e.address, m["modelId"]): (
                m.get("subscribe", []),
                (m.get("publish") or {}).get("address"),
            )
            for n in cdb.nodes
            for e in n.elements
            for m in e.raw_models
        },
        "groups": cdb.groups,
        "scenes": cdb.scenes,
        "scene_names": cdb.scene_names,
        "ranges": cdb.provisioner_unicast_ranges,
        "excluded": cdb.excluded_addresses,
        "meta": cdb.export_meta,
    }


def without_timestamp(doc: dict[str, Any]) -> dict[str, Any]:
    doc = json.loads(json.dumps(doc))
    net = doc.get("meshNetwork", doc)
    net.pop("timestamp", None)
    return doc


def inner_of(text: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(json.loads(text)["network"]))  # type: ignore[no-any-return]


# ----------------------------------------------------------------------------- loading and round trips


@pytest.mark.parametrize("source", [CDB_PATH, SHARE_PATH, ANDROID_PATH])
def test_round_trip_load_write_load(tmp_path: Path, source: Path):
    pf = ProjectFile.load(source)
    reference = views(CDB.load(source))
    assert views(pf.cdb) == reference
    target = tmp_path / source.name
    pf.save(target, now=FIXED_NOW)
    again = ProjectFile.load(target)
    assert views(again.cdb) == reference
    assert again.loaded_timestamp == "2026-03-01T12:30:45Z"
    assert again.flavour == pf.flavour
    assert again.style == pf.style
    assert again.meta == pf.meta
    assert without_timestamp(again.net) == without_timestamp(
        json.loads(source.read_text()).get("meshNetwork")
        or inner_of(source.read_text())
    )


def test_noop_write_changes_only_the_timestamp_cdb_flavour(tmp_path: Path):
    pf = ProjectFile.load(CDB_PATH)
    target = tmp_path / "MeshNetwork.json"
    pf.save(target, now=FIXED_NOW)
    diff = [
        line
        for line in difflib.unified_diff(
            CDB_PATH.read_text().splitlines(),
            target.read_text().splitlines(),
            lineterm="",
            n=0,
        )
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert diff == [
        '-  "timestamp": "2026-01-01T00:00:00Z",',
        '+  "timestamp": "2026-03-01T12:30:45Z",',
    ]


@pytest.mark.parametrize("source", [SHARE_PATH, ANDROID_PATH])
def test_noop_write_changes_only_the_timestamp_share_flavour(
    tmp_path: Path, source: Path
):
    pf = ProjectFile.load(source)
    original = source.read_text()
    target = tmp_path / source.name
    pf.save(target, now=FIXED_NOW)
    written = target.read_text()
    before, after = json.loads(original), json.loads(written)
    assert {k: v for k, v in before.items() if k != "network"} == {
        k: v for k, v in after.items() if k != "network"
    }
    assert list(before) == list(after)  # key order kept
    inner_before = base64.b64decode(before["network"]).decode()
    inner_after = base64.b64decode(after["network"]).decode()
    old_ts = json.loads(inner_before)["timestamp"]
    assert inner_before.replace(old_ts, "2026-03-01T12:30:45Z") == inner_after
    # the outer layout is reproduced too (pretty 2-space for Android, single-line for the hand-made one)
    assert written.count("\n") == original.count("\n") + (
        0 if original.endswith("\n") else 1
    )


def test_load_from_cdb_object_shares_the_tree(cdb: CDB):
    pf = ProjectFile(cdb)
    assert pf.net is cdb.raw
    assert pf.cdb is cdb
    assert pf.path is None
    assert pf.flavour == "cdb"
    assert list(pf.meta) == list(META_KEYS)
    with pytest.raises(ExportError, match="no path"):
        pf.save()


def test_cdb_without_tree_is_rejected(cdb: CDB):
    bare = CDB(cdb.mesh_uuid, cdb.net_keys, cdb.app_keys, [], {}, {})
    with pytest.raises(ExportError, match="no parsed tree"):
        ProjectFile(bare)


def test_load_rejects_foreign_documents(tmp_path: Path):
    """The loader validates like `CDB.parse` (MOD-11: one parser, `InvalidExport` naming what is wrong)."""
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(InvalidExport, match="not a JUNG HOME mesh export"):
        ProjectFile.load(p)
    p.write_text(json.dumps([1, 2]))
    with pytest.raises(InvalidExport, match="not a JUNG HOME mesh export"):
        ProjectFile.load(p)
    doc = json.loads(SHARE_PATH.read_text())
    doc["meta"] = "oops"
    p.write_text(json.dumps(doc))
    with pytest.raises(InvalidExport, match="meta is not an object"):
        ProjectFile.load(p)


def test_share_export_without_meta_gets_a_synthesised_block(tmp_path: Path):
    doc = json.loads(SHARE_PATH.read_text())
    del doc["meta"]
    p = tmp_path / "JungHome.json"
    p.write_text(json.dumps(doc))
    pf = ProjectFile.load(p)
    assert pf.cdb.export_meta is None
    assert list(pf.meta) == list(META_KEYS)
    assert [g["name"] for g in pf.meta["userGroups"]] == [
        "WC",
        "Living room",
        "Kitchen",
    ]
    assert pf.header == {
        "version": "1.1",
        "appVersion": "2.2.0 (822956)",
        "platform": "Android",
    }


def test_repr_and_errors_never_show_key_material(tmp_path: Path):
    pf = ProjectFile.load(CDB_PATH)
    assert NETKEY_HEX not in repr(pf)
    assert "deviceKey" not in repr(pf)
    assert repr(pf) == (
        f"ProjectFile({CDB_PATH}, flavour='cdb', mesh=1BAF3ADE-0000-4000-8000-000000000001,"
        " timestamp='2026-01-01T00:00:00Z', nodes=7, groups=18, scenes=2)"
    )
    err = NewerExportError(
        tmp_path / "f.json", "2027-01-01T00:00:00Z", "2026-01-01T00:00:00Z"
    )
    assert NETKEY_HEX not in str(err)
    assert err.file_timestamp == "2027-01-01T00:00:00Z"
    assert err.loaded_timestamp == "2026-01-01T00:00:00Z"
    assert err.path == tmp_path / "f.json"
    assert isinstance(err, ExportError)


# ----------------------------------------------------------------------------- layout sniffing


def test_layout_sniff_variants():
    assert Layout.sniff('{\n  "a" : 1,\n  "b" : [\n\n  ]\n}') == Layout(2, " : ", ",")
    assert Layout.sniff('{\n "a": 1\n}') == Layout(1, ": ", ",")
    assert Layout.sniff('{"a":1,"b":2}') == Layout(None, ":", ",")
    assert Layout.sniff('{"a": 1, "b": 2}') == Layout(None, ": ", ", ")
    assert Layout.sniff('{\n"a"\n:\n1}') == Layout(
        None, ": ", ","
    )  # newline in the separator: default
    assert Layout.sniff("") == Layout(None, ": ", ",")
    assert Layout.sniff('{"a\\"b":1}') == Layout(
        None, ":", ","
    )  # escaped quote in the first key


def test_apple_style_layout_is_reproduced(tmp_path: Path):
    """Apple's JSONSerialization writes `"key" : value`; a rewrite keeps that so the diff stays minimal."""
    doc = json.loads(CDB_PATH.read_text())
    text = json.dumps(doc, indent=2, separators=(",", " : "), ensure_ascii=False)
    p = tmp_path / "MeshNetwork.json"
    p.write_text(text)
    pf = ProjectFile.load(p)
    assert pf.style == Style(Layout(2, " : ", ","))
    pf.save(now=FIXED_NOW)
    written = p.read_text()
    assert '"meshUUID" : "1BAF3ADE-0000-4000-8000-000000000001"' in written
    assert (
        written.replace("2026-03-01T12:30:45Z", "2026-01-01T00:00:00Z") == text + "\n"
    )


def test_wrapped_inner_payload_is_kept(tmp_path: Path):
    """A share export whose `network` carries the iOS `meshNetwork` wrapper is written back wrapped."""
    net = json.loads(CDB_PATH.read_text())
    doc = {
        "version": "1.1",
        "meta": {"devices": []},
        "network": base64.b64encode(json.dumps(net, indent=2).encode()).decode(),
    }
    p = tmp_path / "JungHome.json"
    p.write_text(json.dumps(doc, indent=2))
    pf = ProjectFile.load(p)
    assert pf.style == Style(
        Layout(2, ": ", ","), Layout(2, ": ", ","), True, ("version", "meta", "network")
    )
    pf.save(now=FIXED_NOW)
    inner = base64.b64decode(json.loads(p.read_text())["network"]).decode()
    assert inner.startswith('{\n  "meshNetwork": {')
    assert inner_of(p.read_text())["meshNetwork"]["timestamp"] == "2026-03-01T12:30:45Z"


def test_ios_share_export_key_order_is_kept(tmp_path: Path):
    """The iOS app writes `network` first (then appVersion, version, platform, meta); a rewrite keeps that order.

    Verified against a real iOS 2.2.0 share export: loaded and rendered unchanged, the file came
    back byte-identical (only our trailing newline added) — compact layout, UTF-8 names, `meta` field order.
    """
    android = json.loads(SHARE_PATH.read_text())
    ios = {
        k: android[k] for k in ("network", "appVersion", "version", "platform", "meta")
    }
    ios["platform"] = "iOS (27.0)"
    p = tmp_path / "JungHome.json"
    p.write_text(json.dumps(ios, separators=(",", ":"), ensure_ascii=False))
    pf = ProjectFile.load(p)
    assert pf.style.outer_keys == (
        "network",
        "appVersion",
        "version",
        "platform",
        "meta",
    )
    rendered = pf.render()
    assert list(json.loads(rendered)) == list(ios)
    assert rendered == p.read_text() + "\n"
    # a header field the loaded file lacks is appended after the known ones
    pf.header["extra"] = 1
    assert list(json.loads(pf.render()))[-1] == "extra"


def test_non_ascii_names_are_written_verbatim(tmp_path: Path):
    pf = ProjectFile.load(CDB_PATH)
    pf.rename_group(GROUP_WC, "Küche")
    assert '"Küche"' in pf.cdb_json()
    assert "\\u00fc" not in pf.cdb_json()
    p = tmp_path / "MeshNetwork.json"
    pf.save(p)
    assert CDB.load(p).groups[GROUP_WC] == "Küche"


# ----------------------------------------------------------------------------- the newer-export guard


def bump_timestamp(path: Path, ts: str) -> None:
    """Simulate the app saving over the file: same content, later CDB timestamp."""
    doc = json.loads(path.read_text())
    if "meshNetwork" in doc:
        doc["meshNetwork"]["timestamp"] = ts
    else:
        inner = json.loads(base64.b64decode(doc["network"]))
        inner["timestamp"] = ts
        doc["network"] = base64.b64encode(json.dumps(inner).encode()).decode()
    path.write_text(json.dumps(doc))


@pytest.mark.parametrize("source", [CDB_PATH, ANDROID_PATH])
def test_guard_refuses_a_file_the_app_updated_since_load(tmp_path: Path, source: Path):
    target = tmp_path / source.name
    target.write_bytes(source.read_bytes())
    pf = ProjectFile.load(target)
    bump_timestamp(target, "2030-06-01T00:00:00Z")
    with pytest.raises(NewerExportError) as info:
        pf.save(now=FIXED_NOW)
    assert info.value.path == target
    assert info.value.file_timestamp == "2030-06-01T00:00:00Z"
    assert info.value.loaded_timestamp == pf.loaded_timestamp
    assert "re-import" in str(info.value)
    assert (
        inner_of(target.read_text())
        if source is ANDROID_PATH
        else json.loads(target.read_text())
    )  # untouched
    assert not (target.with_name(target.name + ".bak")).exists()
    # forcing writes anyway, and the guard is re-based on what we wrote
    pf.save(now=FIXED_NOW, force=True)
    assert ProjectFile.load(target).loaded_timestamp == "2026-03-01T12:30:45Z"
    pf.save(now=FIXED_NOW + timedelta(seconds=1))
    assert pf.loaded_timestamp == "2026-03-01T12:30:46Z"


def test_guard_allows_unchanged_older_and_new_targets(tmp_path: Path):
    target = tmp_path / "MeshNetwork.json"
    target.write_bytes(CDB_PATH.read_bytes())
    pf = ProjectFile.load(target)
    pf.save(now=FIXED_NOW)  # byte-identical to what we loaded
    pf.save(now=FIXED_NOW)  # byte-identical to what we wrote
    other = tmp_path / "elsewhere.json"
    pf.save(other, now=FIXED_NOW)  # a new path needs no check
    assert ProjectFile.load(other).loaded_timestamp == "2026-03-01T12:30:45Z"
    assert pf.path == other
    bump_timestamp(other, "2031-01-01T00:00:00Z")
    with pytest.raises(NewerExportError):
        pf.save(now=FIXED_NOW)
    # a newer file at a path we did not load from is refused as well; an older one is overwritten
    third = tmp_path / "third.json"
    third.write_bytes(CDB_PATH.read_bytes())
    bump_timestamp(third, "2031-01-01T00:00:00Z")
    with pytest.raises(NewerExportError):
        pf.save(third, now=FIXED_NOW)
    bump_timestamp(third, "2020-01-01T00:00:00Z")
    pf.save(third, now=FIXED_NOW)
    assert pf.path == third


@pytest.mark.parametrize(
    "stamp",
    [
        "2020-01-01T00:00:00Z",  # older than ours (a writer with a skewed clock, an old copy put back)
        "2026-03-01T12:30:45Z",  # equal to what we wrote: a meta-only or hand edit
    ],
)
def test_guard_refuses_any_change_to_the_file_we_loaded(tmp_path: Path, stamp: str):
    target = tmp_path / "MeshNetwork.json"
    target.write_bytes(CDB_PATH.read_bytes())
    pf = ProjectFile.load(target)
    pf.save(now=FIXED_NOW)
    bump_timestamp(target, stamp)
    changed = target.read_bytes()
    with pytest.raises(NewerExportError) as info:
        pf.save(now=FIXED_NOW)
    assert info.value.file_timestamp == stamp
    assert target.read_bytes() == changed  # untouched
    # through a symlink it is the same file
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(NewerExportError):
        pf.save(link, now=FIXED_NOW)
    pf.save(now=FIXED_NOW, force=True)
    assert ProjectFile.load(target).loaded_timestamp == "2026-03-01T12:30:45Z"


def test_guard_refuses_to_overwrite_a_foreign_file(tmp_path: Path):
    pf = ProjectFile.load(CDB_PATH)
    target = tmp_path / "notes.json"
    target.write_text('{"hello": "world"}')
    with pytest.raises(ExportError, match="not a JUNG HOME mesh export"):
        pf.save(target)
    target.write_bytes(b"\xff\xfe garbage")
    with pytest.raises(ExportError, match="not a JUNG HOME mesh export"):
        pf.save(target)
    assert target.read_bytes() == b"\xff\xfe garbage"
    pf.save(target, force=True)
    assert CDB.load(target).mesh_uuid == pf.cdb.mesh_uuid


@pytest.mark.parametrize(
    ("candidate", "reference", "advanced"),
    [
        ("2026-02-01T10:00:00+0100", "2026-02-01T09:00:00Z", False),  # same instant
        ("2026-02-01T10:00:01+0100", "2026-02-01T09:00:00Z", True),
        ("2026-02-01T09:00:00Z", "2026-02-01T10:00:00+0100", False),
        (
            "2026-02-01T10:00:00",
            "2026-02-01T09:00:00Z",
            True,
        ),  # naive vs aware: wall clock
        ("2026-02-01T08:00:00", "2026-02-01T09:00:00Z", False),
        ("garbage", "2026-02-01T09:00:00Z", True),  # unparseable counts when different
        ("garbage", "garbage", False),
        ("", "", False),
    ],
)
def test_timestamp_advanced(candidate: str, reference: str, advanced: bool):
    assert timestamp_advanced(candidate, reference) is advanced


def test_now_timestamp_format():
    assert now_timestamp(FIXED_NOW) == "2026-03-01T12:30:45Z"
    assert (
        now_timestamp(
            datetime(2026, 3, 1, 14, 30, 45, 999, tzinfo=timezone(timedelta(hours=2)))
        )
        == "2026-03-01T12:30:45Z"
    )
    assert timestamp_advanced(now_timestamp(), "2026-01-01T00:00:00Z")


def test_save_is_atomic_and_keeps_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "MeshNetwork.json"
    target.write_bytes(CDB_PATH.read_bytes())
    pf = ProjectFile.load(target)
    pf.rename_group(GROUP_WC, "Toilet")
    pf.save(now=FIXED_NOW)
    bak = tmp_path / "MeshNetwork.json.bak"
    assert bak.read_bytes() == CDB_PATH.read_bytes()
    assert CDB.load(target).groups[GROUP_WC] == "Toilet"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "MeshNetwork.json",
        "MeshNetwork.json.bak",
    ]
    # a failing write leaves the target and the backup untouched and no temp file behind
    first = target.read_bytes()
    pf.rename_group(GROUP_WC, "WC again")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("jhmesh.fileio.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        pf.save(now=FIXED_NOW)
    assert target.read_bytes() == first
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "MeshNetwork.json",
        "MeshNetwork.json.bak",
    ]


def test_save_rotates_a_few_backup_generations(tmp_path: Path) -> None:
    """Review-3 W7: every save replaced the one `.bak`, so a bad change noticed a change later had nothing to go
    back to. The previous contents rotate through `BACKUP_GENERATIONS` backups, newest first; the oldest goes."""
    target = tmp_path / "MeshNetwork.json"
    target.write_bytes(CDB_PATH.read_bytes())
    pf = ProjectFile.load(target)
    contents = [target.read_bytes()]
    for name in ("One", "Two", "Three", "Four"):
        pf.rename_group(GROUP_WC, name)
        pf.save(now=FIXED_NOW)
        contents.append(target.read_bytes())
    assert BACKUP_GENERATIONS == 3
    assert [p.read_bytes() for p in backup_paths(target)] == contents[-2:-5:-1]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "MeshNetwork.json",
        "MeshNetwork.json.bak",
        "MeshNetwork.json.bak.1",
        "MeshNetwork.json.bak.2",
    ]


def test_write_private_is_atomic_and_tightens_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "keys.json"
    target.write_bytes(b"old")
    target.chmod(0o644)
    real_fsync = os.fsync

    def boom(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        write_private(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["keys.json"]

    monkeypatch.setattr(os, "fsync", real_fsync)
    write_private(target, b"new")
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_private_with_backup_keeps_the_previous_content(tmp_path: Path) -> None:
    target = tmp_path / "export.json"
    target.write_bytes(b"old")
    target.chmod(0o644)
    write_private_with_backup(target, b"new")
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    bak = tmp_path / "export.json.bak"
    assert bak.read_bytes() == b"old"
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    # the next write moves that backup one generation down
    write_private_with_backup(target, b"newer")
    assert bak.read_bytes() == b"new"
    assert (tmp_path / "export.json.bak.1").read_bytes() == b"old"
    # a target that does not exist yet: no backup to keep, still written private
    fresh = tmp_path / "fresh.json"
    write_private_with_backup(fresh, b"first")
    assert fresh.read_bytes() == b"first"
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_fsync_dir_tolerates_unopenable_or_unsupported_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory `fsync` is a best-effort durability nicety, not on every platform: neither failure crashes."""
    real_open = os.open
    real_fsync = os.fsync

    def boom_open(*args: Any, **kwargs: Any) -> int:
        raise OSError("cannot open directories here")

    monkeypatch.setattr(os, "open", boom_open)
    fsync_dir(tmp_path)  # os.open itself fails: nothing to fsync, no exception

    monkeypatch.setattr(os, "open", real_open)

    def boom_fsync(fd: int) -> None:
        raise OSError("directory fsync not supported")

    monkeypatch.setattr(os, "fsync", boom_fsync)
    fsync_dir(tmp_path)  # opened fine, fsync unsupported: still no exception
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert not (tmp_path / "fresh.json.bak").exists()


def test_save_can_switch_flavour(tmp_path: Path):
    pf = ProjectFile.load(
        CDB_PATH,
        Metadata(META_DIR / "device_metadata.json", META_DIR / "scene_metadata.json"),
    )
    target = tmp_path / "JungHome.json"
    pf.save(target, "share", now=FIXED_NOW)
    assert pf.flavour == "share"
    doc = json.loads(target.read_text())
    assert list(doc) == ["version", "appVersion", "platform", "meta", "network"]
    assert doc["platform"] == "Home Assistant"
    assert list(doc["meta"]) == list(META_KEYS)
    back = CDB.load(target)
    assert views(back)["pubsub"] == views(pf.cdb)["pubsub"]
    meta = Metadata.from_export(back.export_meta)
    assert meta.name_for(UUID_1G, 0x40) == "WC mirror button"
    assert meta.scenes == {1: "WC off", 2: "All off"}
    assert pf.render("cdb").startswith('{\n "meshNetwork": {')


# ----------------------------------------------------------------------------- the synthesised meta block


def test_meta_synthesised_from_cdb_and_app_container_metadata():
    meta = Metadata(META_DIR / "device_metadata.json", META_DIR / "scene_metadata.json")
    pf = ProjectFile.load(CDB_PATH, meta)
    assert pf.meta["userGroups"] == [
        {"name": "WC", "address": GROUP_WC, "icon": DEFAULT_GROUP_ICON},
        {"name": "Living room", "address": GROUP_LIVING, "icon": DEFAULT_GROUP_ICON},
        {"name": "Kitchen", "address": GROUP_KITCHEN, "icon": DEFAULT_GROUP_ICON},
    ]
    ecg = {
        e["groupAddress"]: e["elementAddress"]
        for e in pf.meta["elementConnectionGroups"]
    }
    assert ecg[0xC061] == PROXY_NODE
    assert ecg[0xC005] == GATEWAY
    assert ecg[0xC071] == DIMMER_BUTTON
    assert len(ecg) == 13
    assert pf.meta["scenes"] == [
        {"name": "WC off", "number": 1, "icon": DEFAULT_SCENE_ICON},
        {"name": "All off", "number": 2, "icon": DEFAULT_SCENE_ICON},
    ]
    devices = {
        (d["deviceId"]["nodeId"], tuple(d["deviceId"]["locationIds"])): d
        for d in pf.meta["devices"]
    }
    assert len(devices) == 8
    wc_button = devices[(UUID_1G, (64, 68))]
    assert wc_button == {
        "name": "WC mirror button",
        "macAddress": "00:00:5E:00:53:14",
        "deviceId": {
            "actuatorFunctionId": 0,
            "locationIds": [64, 68],
            "insertType": 2,
            "productId": 1,
            "nodeId": UUID_1G,
        },
        "cachedGroupConnectionMetadata": [],
    }
    dali = devices[("00005EFF-FE00-5323-0000-000000000000", (1,))]["deviceId"]
    assert (dali["actuatorFunctionId"], dali["insertType"], dali["productId"]) == (
        4,
        2,
        2,
    )
    boiler = devices[("00005EFF-FE00-5317-0000-000000000000", (1, 64))]["deviceId"]
    assert (
        boiler["actuatorFunctionId"],
        boiler["insertType"],
        boiler["productId"],
    ) == (0, 1, 3)
    dimmer = devices[("00005EFF-FE00-5330-0000-000000000000", (1,))]["deviceId"]
    assert dimmer["actuatorFunctionId"] == 2
    two_channel = devices[("00005EFF-FE00-5340-0000-000000000000", (1,))]["deviceId"]
    assert (
        two_channel["actuatorFunctionId"],
        two_channel["insertType"],
        two_channel["productId"],
    ) == (1, 1, 16)
    for key in (
        "sceneInfo",
        "schedulerMetaInfo",
        "timer",
        "actuatorExports",
        "buttonLayoutExports",
        "keyModeSceneConfigExports",
    ):
        assert pf.meta[key] == []


def test_meta_without_app_metadata_falls_back_to_cdb_names():
    pf = ProjectFile.load(CDB_PATH)
    assert pf.meta["devices"] == []
    assert [s["name"] for s in pf.meta["scenes"]] == ["Scene #1", "Scene #2"]
    assert pf.scene_names() == {1: "Scene #1", 2: "Scene #2"}


def test_stale_app_metadata_for_unknown_nodes_is_skipped():
    meta = Metadata()
    meta.devices["00000000-0000-4000-8000-00000000DEAD"] = [([1], "Ghost")]
    phone_uuid = "00000001-0000-4000-8000-000000000001"
    meta.devices[phone_uuid] = [([1], "Phone")]
    meta.scenes[7] = "no such scene"
    pf = ProjectFile.load(CDB_PATH, meta)
    names = [d["name"] for d in pf.meta["devices"]]
    assert names == ["Phone"]
    phone = pf.meta["devices"][0]["deviceId"]
    assert (
        phone["actuatorFunctionId"],
        phone["insertType"],
        phone["productId"],
        phone["nodeId"],
    ) == (7, 1, 0, phone_uuid)
    assert pf.meta["devices"][0]["macAddress"] == ""
    assert [s["number"] for s in pf.meta["scenes"]] == [1, 2]


def test_complete_meta_overlays_names_on_a_loaded_export():
    pf = ProjectFile.load(ANDROID_PATH)
    before = json.loads(json.dumps(pf.meta))
    pf.complete_meta()  # nothing missing: no change at all
    assert pf.meta == before
    meta = Metadata()
    meta.scenes[1] = "WC dark"
    meta.devices[UUID_1G.lower()] = [([1], "Mirror light")]
    pf.complete_meta(meta)
    assert pf.meta["scenes"][0] == {
        "name": "WC dark",
        "number": 1,
        "icon": "SceneAbsent",
    }
    assert pf.meta["devices"][0]["name"] == "Mirror light"
    assert len(pf.meta["devices"]) == len(before["devices"])


def test_load_with_metadata_overlays_a_loaded_meta_block():
    meta = Metadata()
    meta.scenes[2] = "Everything off"
    meta.devices["00005EFF-FE00-5330-0000-000000000000"] = [([64], "WC key")]
    pf = ProjectFile.load(ANDROID_PATH, meta)
    assert pf.meta is pf.cdb.export_meta
    assert pf.meta["scenes"][1]["name"] == "Everything off"
    assert pf.meta["devices"][5]["name"] == "WC key"
    assert (
        pf.meta["devices"][5]["cachedGroupConnectionMetadata"][0]["publishAddress"]
        == DIMMER_BUTTON_GROUP
    )


def test_meta_entries_with_unusable_fields_are_left_alone():
    """Odd values (a boolean address, a non-hex string, a nameless scene) never break the sync; new rows are added."""
    pf = ProjectFile.load(ANDROID_PATH)
    pf.meta["userGroups"] = [
        {"name": "Odd", "address": True},
        {"name": "Odder", "address": "zz"},
    ]
    pf.meta["scenes"] = [{"number": 1}, {"name": "Nameless", "number": None}]
    pf.complete_meta()
    assert [g["name"] for g in pf.meta["userGroups"]] == [
        "Odd",
        "Odder",
        "WC",
        "Living room",
        "Kitchen",
    ]
    assert (
        pf.meta["userGroups"][2]["address"] == GROUP_WC
    )  # the template's address type (bool) is not a str
    assert pf.meta["scenes"] == [
        {"number": 1, "name": "Scene #1"},
        {"name": "Nameless", "number": None},
        {"name": "Scene #2", "number": 2, "icon": DEFAULT_SCENE_ICON},
    ]
    assert pf.scene_names() == {1: "Scene #1", 2: "Scene #2"}
    pf.remove_scene(2)
    pf2 = ProjectFile.load(
        SHARE_PATH
    )  # a loaded meta without `sceneInfo` gains no key on scene removal
    pf2.remove_scene(1)
    assert "sceneInfo" not in pf2.meta
    assert pf2.meta["scenes"] == []


def test_meta_uses_hex_strings_when_the_loaded_file_does(tmp_path: Path):
    """Address types in `meta` mirror the loaded entries (ints for Gson; strings if a file ever has them)."""
    doc = json.loads(ANDROID_PATH.read_text())
    doc["meta"]["userGroups"] = [{"name": "WC", "address": "C00F", "icon": "x"}]
    doc["meta"]["elementConnectionGroups"] = [
        {"groupAddress": "C061", "elementAddress": "0148"}
    ]
    p = tmp_path / "JungHome.json"
    p.write_text(json.dumps(doc))
    pf = ProjectFile.load(p)
    pf.complete_meta()
    assert pf.meta["userGroups"][1:] == [
        {"name": "Living room", "address": "C010", "icon": DEFAULT_GROUP_ICON},
        {"name": "Kitchen", "address": "C011", "icon": DEFAULT_GROUP_ICON},
    ]
    assert pf.meta["elementConnectionGroups"][1] == {
        "groupAddress": "C005",
        "elementAddress": "00DC",
    }
    pf.add_group("Attic")
    assert pf.meta["userGroups"][-1] == {
        "name": "Attic",
        "address": "C002",
        "icon": DEFAULT_GROUP_ICON,
    }


# ----------------------------------------------------------------------------- pub/sub mutators


def test_set_subscriptions_replaces_the_list_and_reports_changes():
    pf = ProjectFile.load(CDB_PATH)
    light = pf.cdb.element(PROXY_NODE)
    assert light is not None
    assert light.subscriptions("1000") == [0xC061, 0xFEF5, GROUP_WC]
    changes = pf.set_subscriptions(
        PROXY_NODE, 0, "1000", [0xC061, GROUP_LIVING, GROUP_LIVING, 0xFEF5]
    )
    assert changes == [
        ModelChange(PROXY_NODE, "1000", GROUP_WC, "unsubscribe"),
        ModelChange(PROXY_NODE, "1000", GROUP_LIVING, "subscribe"),
    ]
    assert light.subscriptions("1000") == [0xC061, GROUP_LIVING, 0xFEF5]
    raw = json.loads(pf.cdb_json())["meshNetwork"]["nodes"][2]["elements"][0]["models"]
    assert next(m for m in raw if m["modelId"] == "1000")["subscribe"] == [
        "C061",
        "C010",
        "FEF5",
    ]
    # node by Node / uuid / unicast, element by Element / index, model id case-insensitively
    node = light.node
    assert pf.set_subscriptions(node, light, "05271013", []) == [
        ModelChange(PROXY_NODE, "05271013", 0xC061, "unsubscribe")
    ]
    assert pf.set_subscriptions(node.uuid.lower(), 0, "05271013", [0xC061]) == [
        ModelChange(PROXY_NODE, "05271013", 0xC061, "subscribe")
    ]
    assert (
        pf.set_subscriptions(PROXY_NODE, 0, "1000", [0xC061, GROUP_LIVING, 0xFEF5])
        == []
    )
    with pytest.raises(KeyError, match="no model 1300"):
        pf.set_subscriptions(PROXY_NODE, 0, "1300", [])
    with pytest.raises(KeyError, match="no element index 9"):
        pf.set_subscriptions(PROXY_NODE, 9, "1000", [])
    with pytest.raises(KeyError, match="no node"):
        pf.set_subscriptions(0x0150, 0, "1000", [])
    with pytest.raises(KeyError, match="no node"):
        pf.set_subscriptions("00000000-0000-4000-8000-00000000DEAD", 0, "1000", [])


def test_subscribe_and_unsubscribe_are_idempotent():
    pf = ProjectFile.load(CDB_PATH)
    assert pf.subscribe(PROXY_NODE, "1000", GROUP_WC) == []
    assert pf.subscribe(PROXY_NODE, "1000", GROUP_KITCHEN) == [
        ModelChange(PROXY_NODE, "1000", GROUP_KITCHEN, "subscribe")
    ]
    assert pf.unsubscribe(PROXY_NODE, "1000", GROUP_KITCHEN) == [
        ModelChange(PROXY_NODE, "1000", GROUP_KITCHEN, "unsubscribe")
    ]
    assert pf.unsubscribe(PROXY_NODE, "1000", GROUP_KITCHEN) == []
    assert pf.cdb.element(PROXY_NODE).subscriptions("1000") == [
        0xC061,
        0xFEF5,
        GROUP_WC,
    ]  # type: ignore[union-attr]
    with pytest.raises(KeyError, match="no element 0150"):
        pf.subscribe(0x0150, "1000", GROUP_WC)


def test_set_publication_uses_the_apps_defaults_and_clears_with_none():
    pf = ProjectFile.load(CDB_PATH)
    assert pf.publication(PROXY_NODE + 1, "1001") == 0xC005  # button A -> gateway group
    assert pf.publication(PROXY_NODE + 1, "1003") is None
    changes = pf.set_publication(PROXY_NODE, 1, "1001", 0xC061)
    assert changes == [ModelChange(PROXY_NODE + 1, "1001", 0xC061, "publish")]
    button = pf.cdb.element(PROXY_NODE + 1)
    assert button is not None
    assert next(m for m in button.raw_models if m["modelId"] == "1001")["publish"] == {
        "address": "C061",
        "index": 0,
        "ttl": 255,
        "credentials": 0,
        "retransmit": {"count": 0, "interval": 50},
        "period": {"numberOfSteps": 0, "resolution": 100},
    }
    assert pf.set_publication(
        PROXY_NODE,
        1,
        "1003",
        0xFFFF,
        ttl=7,
        period_steps=3,
        period_resolution=1000,
        retransmit_count=2,
        retransmit_interval=100,
        credentials=1,
        app_key_index=1,
    ) == [ModelChange(PROXY_NODE + 1, "1003", 0xFFFF, "publish")]
    assert next(m for m in button.raw_models if m["modelId"] == "1003")["publish"] == {
        "address": "FFFF",
        "index": 1,
        "ttl": 7,
        "credentials": 1,
        "retransmit": {"count": 2, "interval": 100},
        "period": {"numberOfSteps": 3, "resolution": 1000},
    }
    assert pf.set_publication(PROXY_NODE, 1, "1001", None) == [
        ModelChange(PROXY_NODE + 1, "1001", 0, "publish")
    ]
    assert pf.publication(PROXY_NODE + 1, "1001") is None
    assert "publish" not in next(m for m in button.raw_models if m["modelId"] == "1001")
    assert pf.set_publication(PROXY_NODE, 1, "1001", None) == [
        ModelChange(PROXY_NODE + 1, "1001", 0, "publish")
    ]


# ----------------------------------------------------------------------------- rooms


def test_add_group_allocates_the_lowest_free_address_like_the_app():
    pf = ProjectFile.load(ANDROID_PATH)
    before = pf.snapshot()
    addr = pf.add_group("Bedroom")
    assert (
        addr == 0xC002
    )  # C000, C001 are element groups; rooms and element groups share the counter
    assert pf.cdb.groups[addr] == "Bedroom"
    assert pf.user_groups()[addr] == "Bedroom"
    assert pf.net["groups"][-1] == {
        "address": "C002",
        "name": "Bedroom",
        "parentAddress": "0000",
    }
    assert list(pf.net["groups"][-1]) == list(
        pf.net["groups"][0]
    )  # key order copied from a sibling
    assert pf.meta["userGroups"][-1] == {
        "name": "Bedroom",
        "address": 0xC002,
        "icon": DEFAULT_GROUP_ICON,
    }
    assert pf.add_group("Attic", icon="ic_group_attic") == 0xC003
    assert pf.meta["userGroups"][-1] == {
        "name": "Attic",
        "address": 0xC003,
        "icon": "ic_group_attic",
    }
    assert pf.add_group("Garage", address=0xC100, parent=0xC002) == 0xC100
    assert pf.net["groups"][-1] == {
        "address": "C100",
        "name": "Garage",
        "parentAddress": "C002",
    }
    after = pf.snapshot()
    assert before["network"]["groups"] == after["network"]["groups"][:-3]
    assert (
        before["network"]["nodes"] == after["network"]["nodes"]
    )  # nothing on the mesh side
    assert before["meta"]["devices"] == after["meta"]["devices"]
    assert (
        CDB.from_network(json.loads(pf.cdb_json())["meshNetwork"]).groups[0xC100]
        == "Garage"
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"name": "wc"}, "already exists"),  # case-insensitive, like CheckNameInput
        ({"name": "  "}, "cannot be blank"),
        ({"name": "New", "address": 0xC00F}, "already exists"),
        ({"name": "New", "address": 0xBFFF}, "not a user group address"),
        ({"name": "New", "address": 0xFEF5}, "not a user group address"),
    ],
)
def test_add_group_rejects_bad_input(kwargs: dict[str, Any], match: str):
    pf = ProjectFile.load(ANDROID_PATH)
    with pytest.raises(ValueError, match=match):
        pf.add_group(**kwargs)


def _with_provisioners(provisioners: list[dict[str, Any]] | None) -> ProjectFile:
    net = json.loads(CDB_PATH.read_text())["meshNetwork"]
    if provisioners is None:
        del net["provisioners"]
    else:
        net["provisioners"] = provisioners
    return ProjectFile(CDB.from_network(net))


def test_group_allocation_without_provisioner_ranges_and_when_exhausted():
    """The ranges come from the CDB as loaded (validated there), so a range is set up before the load."""
    pf = _with_provisioners(None)
    assert pf.free_group_address() == 0xC002
    assert pf.free_scene_number() == 3
    pf = _with_provisioners(
        [
            {
                "provisionerName": "x",
                "UUID": "y",
                "allocatedGroupRange": [],
                "allocatedSceneRange": [{"firstScene": "0001", "lastScene": "0002"}],
            }
        ]
    )
    assert pf.free_group_address() == 0xC002
    with pytest.raises(ExportError, match="no free scene number"):
        pf.free_scene_number()
    pf = _with_provisioners(
        [{"allocatedGroupRange": [{"lowAddress": "C000", "highAddress": "C001"}]}]
    )
    with pytest.raises(ExportError, match="no free group address"):
        pf.free_group_address()


@pytest.mark.parametrize(
    ("provisioners", "message"),
    [
        (
            [{"allocatedGroupRange": [{}]}],
            "allocatedGroupRange lowAddress is not a string",
        ),
        (
            [{"allocatedSceneRange": [{"firstScene": "0001"}]}],
            "allocatedSceneRange lastScene is not a string",
        ),
        (
            [{"allocatedSceneRange": [{"firstScene": 1, "lastScene": "0002"}]}],
            "allocatedSceneRange firstScene is not a string",
        ),
        ([{"allocatedGroupRange": [5]}], "allocatedGroupRange range is not an object"),
        ([{"allocatedSceneRange": {}}], "allocatedSceneRange is not a list"),
    ],
    ids=[
        "group_empty",
        "scene_missing_last",
        "scene_int",
        "group_entry_int",
        "scene_dict",
    ],
)
def test_malformed_provisioner_ranges_are_refused_at_load(
    provisioners: list[dict[str, Any]], message: str
):
    """A range without its keys or with ints is `InvalidExport` at load, never a KeyError / TypeError from
    `free_group_address` / `free_scene_number` inside a service call."""
    with pytest.raises(InvalidExport, match=message):
        _with_provisioners(provisioners)


def test_provisioner_ranges_are_read_in_file_order(cdb: CDB):
    assert cdb.provisioner_group_ranges == [(0xC000, 0xC64B)]
    assert cdb.provisioner_scene_ranges == [(0x0001, 0x1999)]
    pf = _with_provisioners(
        [
            {"allocatedGroupRange": []},
            {"allocatedGroupRange": [{"lowAddress": "C100", "highAddress": "C1FF"}]},
            {"allocatedSceneRange": [{"firstScene": "0010", "lastScene": "0020"}]},
        ]
    )
    assert pf.free_group_address() == 0xC100  # the first provisioner that has one
    assert pf.free_scene_number() == 0x10


def test_rename_group_changes_cdb_and_meta():
    pf = ProjectFile.load(ANDROID_PATH)
    pf.rename_group(GROUP_WC, "Toilet")
    assert pf.cdb.groups[GROUP_WC] == "Toilet"
    assert (
        next(g for g in pf.net["groups"] if g["address"] == "C00F")["name"] == "Toilet"
    )
    assert pf.meta["userGroups"][0] == {
        "name": "Toilet",
        "address": GROUP_WC,
        "icon": "ic_group_bathroom",
    }
    pf.rename_group(GROUP_WC, "toilet")  # own name in another case is fine
    with pytest.raises(ValueError, match="already exists"):
        pf.rename_group(GROUP_WC, "kitchen")
    with pytest.raises(ValueError, match="cannot be blank"):
        pf.rename_group(GROUP_WC, "")
    with pytest.raises(KeyError, match="no group C0FF"):
        pf.rename_group(0xC0FF, "Nowhere")


def test_remove_group_unwires_members_and_linked_buttons():
    pf = ProjectFile.load(ANDROID_PATH)
    assert {e.address for e in pf.group_members(GROUP_WC)} == {PROXY_NODE, DIMMER}
    assert pf.room_connections(GROUP_WC) == [
        {
            "elementAddress": DIMMER_BUTTON,
            "groupAddress": GROUP_WC,
            "publishAddress": DIMMER_BUTTON_GROUP,
            "function": "LIGHT",
        }
    ]
    changes = pf.remove_group(GROUP_WC)
    assert GROUP_WC not in pf.cdb.groups
    assert all(g["address"] != "C00F" for g in pf.net["groups"])
    assert [g["name"] for g in pf.meta["userGroups"]] == ["Living room", "Kitchen"]
    assert pf.meta["devices"][5]["cachedGroupConnectionMetadata"] == []
    for addr in (PROXY_NODE, DIMMER):
        el = pf.cdb.element(addr)
        assert el is not None
        for m in el.raw_models:
            assert GROUP_WC not in el.subscriptions(m["modelId"])
            assert DIMMER_BUTTON_GROUP not in el.subscriptions(m["modelId"])
    assert ModelChange(PROXY_NODE, "1000", GROUP_WC, "unsubscribe") in changes
    assert (
        ModelChange(PROXY_NODE, "1000", DIMMER_BUTTON_GROUP, "unsubscribe") in changes
    )
    assert ModelChange(DIMMER, "1002", DIMMER_BUTTON_GROUP, "unsubscribe") in changes
    assert (
        ModelChange(PROXY_NODE, "1004", GROUP_WC, "unsubscribe") in changes
    )  # the fixture subscribes every server
    assert all(c.kind == "unsubscribe" for c in changes)
    assert len(changes) == len(set(changes))
    # element groups of the rewired buttons are untouched, the room is simply gone
    assert pf.publication(DIMMER_BUTTON, "1001") == DIMMER_BUTTON_GROUP
    with pytest.raises(KeyError, match="no group C00F"):
        pf.remove_group(GROUP_WC)


def test_remove_group_clears_publications_and_element_group_metadata():
    pf = ProjectFile.load(ANDROID_PATH)
    changes = pf.remove_group(
        DIMMER_BUTTON_GROUP
    )  # an element group: publications point at it
    assert ModelChange(DIMMER_BUTTON, "1001", 0, "publish") in changes
    assert ModelChange(DIMMER_BUTTON, "05271013", 0, "publish") in changes
    assert (
        ModelChange(DIMMER_BUTTON, "1001", DIMMER_BUTTON_GROUP, "unsubscribe")
        in changes
    )
    assert (
        ModelChange(PROXY_NODE, "1000", DIMMER_BUTTON_GROUP, "unsubscribe") in changes
    )
    assert pf.publication(DIMMER_BUTTON, "1001") is None
    assert all(
        e["groupAddress"] != DIMMER_BUTTON_GROUP
        for e in pf.meta["elementConnectionGroups"]
    )
    assert len(pf.meta["userGroups"]) == 3


def test_set_room_adds_and_removes_a_member_like_the_app():
    pf = ProjectFile.load(ANDROID_PATH)
    socket = pf.cdb.element(SOCKET)
    assert socket is not None
    # a socket joins WC: OnOff server <- room; the LIGHT-function button link does not apply to sockets
    assert pf.set_room(SOCKET, GROUP_WC) == [
        ModelChange(SOCKET, "1000", GROUP_WC, "subscribe")
    ]
    assert socket.subscriptions("1000") == [0xC000, 0xFEF8, GROUP_KITCHEN, GROUP_WC]
    assert socket.subscriptions("1004") == [0xC000, 0xFEF8, GROUP_KITCHEN]
    assert pf.set_room(SOCKET, GROUP_WC) == []
    assert pf.set_room(SOCKET, GROUP_WC, member=False) == [
        ModelChange(SOCKET, "1000", GROUP_WC, "unsubscribe")
    ]
    # a lamp joins WC: OnOff (+ Level) servers <- room, and <- the room-linked button's publish group
    assert pf.set_room(ACTUATOR, GROUP_WC) == [
        ModelChange(ACTUATOR, "1000", GROUP_WC, "subscribe"),
        ModelChange(ACTUATOR, "1000", DIMMER_BUTTON_GROUP, "subscribe"),
    ]
    ctl = pf.cdb.element(LIGHT_2G)
    assert ctl is not None
    assert pf.set_room(ctl, GROUP_WC) == [
        ModelChange(LIGHT_2G, "1000", GROUP_WC, "subscribe"),
        ModelChange(LIGHT_2G, "1002", GROUP_WC, "subscribe"),
        ModelChange(LIGHT_2G, "1000", DIMMER_BUTTON_GROUP, "subscribe"),
        ModelChange(LIGHT_2G, "1002", DIMMER_BUTTON_GROUP, "subscribe"),
    ]
    assert ctl.subscriptions("1000")[-2:] == [GROUP_WC, DIMMER_BUTTON_GROUP]
    # leaving: the button's group first, the room last — a stop in between leaves the light in the room, so the
    # next run leaves it again, the rest of the button's group included (D12)
    assert pf.set_room(LIGHT_2G, GROUP_WC, member=False) == [
        ModelChange(LIGHT_2G, "1000", DIMMER_BUTTON_GROUP, "unsubscribe"),
        ModelChange(LIGHT_2G, "1002", DIMMER_BUTTON_GROUP, "unsubscribe"),
        ModelChange(LIGHT_2G, "1000", GROUP_WC, "unsubscribe"),
        ModelChange(LIGHT_2G, "1002", GROUP_WC, "unsubscribe"),
    ]
    assert pf.set_room(LIGHT_2G, GROUP_WC, member=False) == []
    # meta is untouched by membership changes (§8.3: cached button metadata changes only when rewired)
    assert pf.meta == ProjectFile.load(ANDROID_PATH).meta
    # a room without linked buttons: just the room subscription
    assert pf.set_room(LIGHT_2G, GROUP_KITCHEN) == [
        ModelChange(LIGHT_2G, "1000", GROUP_KITCHEN, "subscribe"),
        ModelChange(LIGHT_2G, "1002", GROUP_KITCHEN, "subscribe"),
    ]
    with pytest.raises(KeyError, match="C061 is not a room"):
        pf.set_room(LIGHT_2G, 0xC061)
    with pytest.raises(KeyError, match="C0FF is not a room"):
        pf.set_room(LIGHT_2G, 0xC0FF)


@pytest.mark.parametrize(
    ("function", "socket_joins", "lamp_joins", "ctl_temp_joins"),
    [
        ("LIGHT", False, True, False),
        (1, False, True, False),  # iOS ordinal
        ("SWITCH", True, False, False),
        ("SWITCH_PROPERTY_MODE", True, False, False),
        ("LIGHT_AND_SWITCH", True, True, False),
        (
            "BLIND",
            False,
            False,
            False,
        ),  # MOD-04: classified by device kind now, not by "has a Level server only" — the CTL
        # temperature element is a light's element, not a blind, whatever models it happens to host
        (
            "BLIND_PROPERTY_MODE",
            False,
            False,
            False,
        ),  # Property key mode: 05271013, absent on that element
        ("RTR_PROPERTY_MODE", False, False, False),
        ("NONSENSE", True, True, True),  # unknown function: no filtering
        (None, True, True, True),
    ],
)
def test_set_room_filters_linked_buttons_by_function(
    function: Any, socket_joins: bool, lamp_joins: bool, ctl_temp_joins: bool
):
    pf = ProjectFile.load(ANDROID_PATH)
    link = pf.meta["devices"][5]["cachedGroupConnectionMetadata"][0]
    link["function"] = function
    publish = DIMMER_BUTTON_GROUP
    changes = pf.set_room(SOCKET, GROUP_WC)
    assert any(c.address == publish for c in changes) is socket_joins
    changes = pf.set_room(ACTUATOR, GROUP_WC)
    assert any(c.address == publish for c in changes) is lamp_joins
    changes = pf.set_room(
        LIGHT_2G + 1, GROUP_WC
    )  # the CTL temperature element: Level server only
    assert any(c.address == publish for c in changes) is ctl_temp_joins
    # removal always drops the link subscriptions, whatever the function says
    assert all(
        c.kind == "unsubscribe" for c in pf.set_room(SOCKET, GROUP_WC, member=False)
    )


def test_set_room_uses_the_linked_buttons_key_mode_models():
    pf = ProjectFile.load(ANDROID_PATH)
    link = pf.meta["devices"][5]["cachedGroupConnectionMetadata"][0]
    link["function"] = (
        "LIGHT_PROPERTY_MODE"  # -> key mode Property: the LBC User Property Server
    )
    changes = pf.set_room(ACTUATOR, GROUP_WC)
    assert changes == [
        ModelChange(ACTUATOR, "1000", GROUP_WC, "subscribe"),
        ModelChange(ACTUATOR, "05271013", GROUP_WC, "subscribe"),
        ModelChange(ACTUATOR, "05271013", DIMMER_BUTTON_GROUP, "subscribe"),
    ]
    del link["publishAddress"]
    assert pf.set_room(LIGHT_2G, GROUP_WC) == [
        ModelChange(LIGHT_2G, "1000", GROUP_WC, "subscribe"),
        ModelChange(LIGHT_2G, "1002", GROUP_WC, "subscribe"),
        ModelChange(LIGHT_2G, "05271013", GROUP_WC, "subscribe"),
    ]


def test_matches_function_uses_the_device_kind() -> None:
    """A blinds-only node's element, or an RTR's, is never a lamp just because it hosts an OnOff/Level server
    the app's own device classes never gave that meaning (MOD-04)."""
    pf = ProjectFile.load(FIXTURES / "Blinds.json")
    pp2 = pf.cdb.element(0x0700)
    assert pp2 is not None
    assert not pf._matches_function(pp2, 0)  # LIGHT
    assert pf._matches_function(pp2, 2)  # BLIND

    pf_rtr = ProjectFile.load(FIXTURES / "MeshNetwork-rtr.json")
    rtr = pf_rtr.cdb.element(0x0500)
    assert rtr is not None
    assert not pf_rtr._matches_function(rtr, 0)  # LIGHT
    assert pf_rtr._matches_function(rtr, 8)  # RTR_PROPERTY_MODE

    # unchanged: a light (LIGHT) and a socket (SWITCH) still match as before
    light = pf.cdb.element(PROXY_NODE)
    assert light is not None
    assert pf._matches_function(light, 0)
    socket = pf.cdb.element(SOCKET)
    assert socket is not None
    assert pf._matches_function(socket, 4)


# ----------------------------------------------------------------------------- scenes


def test_add_scene_allocates_the_lowest_free_number():
    pf = ProjectFile.load(ANDROID_PATH)
    num = pf.add_scene("Dinner", addresses=[PROXY_NODE, DIMMER, PROXY_NODE])
    assert num == 3
    assert pf.cdb.scenes[3] == [PROXY_NODE, DIMMER]
    assert pf.cdb.scene_names[3] == "Dinner"
    assert pf.net["scenes"][-1] == {
        "name": "Dinner",
        "number": "0003",
        "addresses": ["0148", "0300"],
    }
    assert pf.meta["scenes"][-1] == {
        "name": "Dinner",
        "number": 3,
        "icon": DEFAULT_SCENE_ICON,
    }
    assert pf.add_scene("Movie", icon="SceneTv") == 4
    assert pf.meta["scenes"][-1] == {"name": "Movie", "number": 4, "icon": "SceneTv"}
    assert pf.add_scene("Night", number=100) == 100
    assert pf.scene_names() == {
        1: "WC off",
        2: "All off",
        3: "Dinner",
        4: "Movie",
        100: "Night",
    }
    assert CDB.load(ANDROID_PATH).scenes.keys() == {1, 2}
    loaded = CDB.from_network(json.loads(pf.cdb_json())["meshNetwork"])
    assert loaded.scenes == {
        1: [PROXY_NODE],
        2: [],
        3: [PROXY_NODE, DIMMER],
        4: [],
        100: [],
    }
    with pytest.raises(ValueError, match="already exists"):
        pf.add_scene("wc OFF")  # meta name, case-insensitively
    with pytest.raises(ValueError, match="already exists"):
        pf.add_scene("X", number=100)
    with pytest.raises(ValueError, match=r"not 1\.\.65535"):
        pf.add_scene("X", number=0)
    with pytest.raises(ValueError, match="cannot be blank"):
        pf.add_scene(" ")


def test_add_scene_on_a_cdb_without_scenes_creates_the_list():
    pf = ProjectFile.load(CDB_PATH)
    del pf.net["scenes"]
    pf.cdb.scenes.clear()
    pf.cdb.scene_names.clear()
    assert pf.add_scene("First") == 1
    assert pf.net["scenes"] == [{"name": "First", "number": "0001", "addresses": []}]


def test_rename_scene_and_set_scene_addresses():
    pf = ProjectFile.load(ANDROID_PATH)
    pf.rename_scene(2, "Everything off")
    assert pf.net["scenes"][1]["name"] == "Everything off"
    assert pf.cdb.scene_names[2] == "Everything off"
    assert pf.meta["scenes"][1] == {
        "name": "Everything off",
        "number": 2,
        "icon": "SceneNight",
    }
    pf.rename_scene(2, "EVERYTHING OFF")
    with pytest.raises(ValueError, match="already exists"):
        pf.rename_scene(2, "wc off")
    with pytest.raises(ValueError, match="cannot be blank"):
        pf.rename_scene(2, "")
    with pytest.raises(KeyError, match="no scene 9"):
        pf.rename_scene(9, "Nine")
    pf.set_scene_addresses(2, [DIMMER, SOCKET, DIMMER])
    assert pf.net["scenes"][1]["addresses"] == ["0300", "0172"]
    assert pf.cdb.scenes[2] == [DIMMER, SOCKET]
    with pytest.raises(KeyError, match="no scene 9"):
        pf.set_scene_addresses(9, [])


def test_remove_scene_drops_cdb_meta_and_scene_info_rows():
    pf = ProjectFile.load(ANDROID_PATH)
    assert pf.meta["sceneInfo"][0]["scene"] == 1
    pf.remove_scene(1)
    assert pf.cdb.scenes == {2: []}
    assert pf.cdb.scene_names == {2: "Scene #2"}
    assert [s["number"] for s in pf.net["scenes"]] == ["0002"]
    assert pf.meta["scenes"] == [{"name": "All off", "number": 2, "icon": "SceneNight"}]
    assert pf.meta["sceneInfo"] == []
    assert (
        pf.meta["keyModeSceneConfigExports"][0]["sceneConfig"]["sceneId"] == 1
    )  # key link left for the DevKey path
    with pytest.raises(KeyError, match="no scene 1"):
        pf.remove_scene(1)
    pf2 = ProjectFile.load(CDB_PATH)  # synthesised meta: no sceneInfo rows to filter
    pf2.remove_scene(2)
    assert pf2.meta["scenes"] == [
        {"name": "Scene #1", "number": 1, "icon": DEFAULT_SCENE_ICON}
    ]


def test_remove_scene_info_drops_only_that_devices_row_of_that_scene():
    """*Remove device from scene* drops the device's `sceneInfo` row; other scenes, devices and odd rows stay."""
    pf = ProjectFile.load(ANDROID_PATH)
    row = pf.meta["sceneInfo"][0]  # scene 1, the WC mirror device (0148, location 1)
    node = pf.cdb.node_by_addr(0x0148)
    assert node is not None
    other_scene = {**row, "scene": 2}
    other_device = {**row, "deviceId": {**row["deviceId"], "locationIds": [64, 68]}}
    no_device = {"scene": 1, "deviceId": None}
    pf.meta["sceneInfo"] = [row, other_scene, other_device, no_device, None]
    pf.remove_scene_info(1, node, 2)  # a location no device of the node has
    assert pf.meta["sceneInfo"] == [row, other_scene, other_device, no_device, None]
    pf.remove_scene_info(1, node, 1)
    assert pf.meta["sceneInfo"] == [other_scene, other_device, no_device, None]
    pf2 = ProjectFile.load(SHARE_PATH)  # no `sceneInfo` list: none is added
    pf2.remove_scene_info(1, node, 1)
    assert "sceneInfo" not in pf2.meta


# ----------------------------------------------------------------------------- device names


def test_set_device_name_renames_or_creates_the_meta_entry():
    pf = ProjectFile.load(ANDROID_PATH)
    before = json.loads(json.dumps(pf.net))
    entry = pf.set_device_name(UUID_1G.lower(), (1,), "Mirror")
    assert entry is pf.meta["devices"][0]
    assert entry["name"] == "Mirror"
    assert entry["macAddress"] == "00:00:5E:00:53:14"
    assert pf.net == before  # the CDB node name is not the app's device name
    node = pf.cdb.node_by_addr(ACTUATOR)
    assert node is not None
    new = pf.set_device_name(node, [2, 1], "Kitchen ceiling")
    assert new is pf.meta["devices"][-1]
    assert new == {
        "name": "Kitchen ceiling",
        "macAddress": "00:00:5E:00:53:40",
        "deviceId": {
            "actuatorFunctionId": 1,
            "locationIds": [1, 2],
            "insertType": 1,
            "productId": 16,
            "nodeId": "00005EFF-FE00-5340-0000-000000000000",
        },
        "cachedGroupConnectionMetadata": [],
    }
    assert list(new) == list(pf.meta["devices"][0])
    assert list(new["deviceId"]) == list(pf.meta["devices"][0]["deviceId"])
    assert pf.set_device_name(ACTUATOR, [1, 2], "Renamed") is new
    assert new["name"] == "Renamed"
    assert len(pf.meta["devices"]) == 7
    with pytest.raises(KeyError, match="no node"):
        pf.set_device_name(0x0150, [1], "x")


@pytest.mark.parametrize(
    "name",
    ["Mirror", "50%% off", "a%nb", "100 %5%", "%-5%", "%1$%", "Küche", " Hall "],
)
def test_check_name_accepts_what_java_formats_without_arguments(name: str) -> None:
    assert check_name(name) == name


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        (None, "blank"),
        ("", "blank"),
        (" \t", "blank"),
        ("100%", "not_allowed"),  # a % at the end: unknown conversion
        ("50% off", "not_allowed"),  # "% o": octal with the space flag, and no argument
        ("%s lamp", "not_allowed"),
        ("%q", "not_allowed"),  # no such conversion
        ("%-%", "not_allowed"),  # left-justified without a width
        ("%#%", "not_allowed"),
        ("%.2%", "not_allowed"),
        ("%5n", "not_allowed"),
        ("%-n", "not_allowed"),
        ("%tY", "not_allowed"),
        ("%%%", "not_allowed"),
    ],
)
def test_check_name_refuses_like_check_name_input(
    name: str | None, reason: str
) -> None:
    with pytest.raises(InvalidName) as exc:
        check_name(name)
    assert exc.value.reason == reason
    assert isinstance(exc.value, ValueError)  # the room / scene mutators' old contract


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (
            ["Lamp"],
            "Lamp 3",
        ),  # counted as a name and as a stem, like the app's countInstance
        (["lamp", "Lamp 2", "Lamp shade"], "Lamp 5"),
        (["Lamp", "Lamp 4"], "Lamp 5"),  # the app's "Lamp 4" is taken: counted on
    ],
)
def test_suffixed_name_is_the_apps_count(names: list[str], expected: str) -> None:
    assert suffixed_name("Lamp", names) == expected


def test_rename_device_is_the_apps_update_device_name() -> None:
    pf = ProjectFile.load(ANDROID_PATH)
    before = json.loads(json.dumps(pf.net))
    mirror = pf.meta["devices"][0]
    assert pf.rename_device(UUID_1G, [1], "Mirror") == "Mirror"
    assert mirror["name"] == "Mirror"
    assert pf.net == before  # the CDB node name stays the product's
    snapshot = pf.snapshot()
    assert pf.rename_device(UUID_1G, [1], "Mirror") == "Mirror"
    assert pf.snapshot() == snapshot  # already called that: nothing changes
    # only the case changes: the device's own name does not count as taken
    assert pf.rename_device(UUID_1G, [1], "mirror") == "mirror"
    # another device's name, ignoring case: numbered ("WC ceiling" and the stem of "WC ceiling button")
    assert pf.rename_device(UUID_1G, [1], "wc CEILING") == "wc CEILING 3"
    assert mirror["name"] == "wc CEILING 3"
    for bad in ("  ", "50% off"):
        with pytest.raises(InvalidName):
            pf.rename_device(UUID_1G, [1], bad)
    assert mirror["name"] == "wc CEILING 3"
    # a device the project has no entry for yet gets one
    count = len(pf.meta["devices"])
    assert pf.rename_device(ACTUATOR, [2, 1], "Kitchen ceiling") == "Kitchen ceiling"
    assert len(pf.meta["devices"]) == count + 1
    assert pf.device_names()[-1] == "Kitchen ceiling"


def test_room_and_scene_names_are_checked_like_the_app() -> None:
    pf = ProjectFile.load(ANDROID_PATH)
    scene = pf.add_scene("Evening")
    for mutate in (
        lambda: pf.add_group("50% off"),
        lambda: pf.rename_group(GROUP_WC, "WC 100%"),
        lambda: pf.add_scene("%d lamps"),
        lambda: pf.rename_scene(scene, "Evening %"),
    ):
        with pytest.raises(InvalidName) as exc:
            mutate()
        assert exc.value.reason == "not_allowed"
    assert pf.add_group("50%% off") > 0  # a doubled % is a literal one


def test_a_rename_takes_at_most_the_rename_sheets_thirty_characters() -> None:
    """`fragment_name_config.xml` `app:maxLength="30"`: the rename sheet of devices, rooms and scenes."""
    pf = ProjectFile.load(ANDROID_PATH)
    scene = pf.add_scene("Evening")
    at_limit, past = "x" * RENAME_MAX_LENGTH, "x" * (RENAME_MAX_LENGTH + 1)
    assert RENAME_MAX_LENGTH == 30
    assert (
        name_length("K\u00fcche \U0001f4a1") == 8
    )  # Java chars: the bulb is a surrogate pair
    assert check_name(past) == past  # no limit unless asked for
    with pytest.raises(InvalidName) as exc:
        check_name("\U0001f4a1" * 15 + "x", RENAME_MAX_LENGTH)
    assert exc.value.reason == "too_long"
    assert "30 characters" in str(exc.value)
    for mutate in (
        lambda: pf.rename_device(UUID_1G, [1], past),
        lambda: pf.rename_group(GROUP_WC, past),
        lambda: pf.rename_scene(scene, past),
    ):
        with pytest.raises(InvalidName) as exc:
            mutate()
        assert exc.value.reason == "too_long"
    # creating has no limit (the app's create screens set none), and a name kept as it is stays
    room = pf.add_group(past)
    pf.rename_group(room, past)
    long_scene = pf.add_scene(past + " scene")
    pf.rename_scene(long_scene, past + " scene")
    pf.rename_group(GROUP_WC, at_limit)
    pf.rename_scene(scene, at_limit)
    # the limit is on the name typed: the app's number may take the result past it
    assert pf.rename_device(UUID_1G, [1], at_limit) == at_limit
    numbered = pf.rename_device(ACTUATOR, [2, 1], at_limit.upper())
    assert (numbered, len(numbered)) == (at_limit.upper() + " 3", 32)


# ----------------------------------------------------------------------------- helpers


def test_mac_from_uuid():
    assert mac_from_uuid("00005EFF-FE00-5323-0000-000000000000") == "00:00:5E:00:53:23"
    assert mac_from_uuid("00005eff-fe00-530d-0000-000000000000") == "00:00:5E:00:53:0D"
    assert mac_from_uuid("00000001-0000-4000-8000-000000000001") == ""
    assert mac_from_uuid("") == ""


def test_element_group_address_parsing():
    assert element_group_address("element group #0x148") == 0x148
    assert element_group_address("element group #0X148") == 0x148
    assert element_group_address("element group #328") == 328
    assert element_group_address("element group #") is None
    assert element_group_address("element group #zz") is None
    assert element_group_address("WC") is None
    assert element_group_address("device type group #65269") is None


def test_is_room():
    assert is_room(0xC00F, "WC")
    assert not is_room(0xC061, "element group #0x148")
    assert not is_room(0xFEF5, "device type group #0xFEF5")
    assert not is_room(0xFEF5, "Looks like a room")  # device-type range is never a room
    assert not is_room(0xFEFF, "#time_keeper_group#")
    assert not is_room(0xC0FF, "#time_keeper_group#")


def test_actuator_function_and_insert_type_guesses(cdb: CDB):
    by_addr = {n.unicast: n for n in cdb.nodes}
    assert guess_actuator_function(by_addr[GATEWAY]) == 9
    assert guess_actuator_function(by_addr[SOCKET]) == 0
    assert guess_actuator_function(by_addr[LIGHT_2G]) == 4  # CTL -> TwDimming
    assert guess_actuator_function(by_addr[DIMMER]) == 2
    assert guess_actuator_function(by_addr[PROXY_NODE]) == 0
    assert guess_actuator_function(by_addr[ACTUATOR]) == 1  # two switched outputs
    assert guess_actuator_function(by_addr[PHONE]) == 7  # no load at all
    assert [
        guess_insert_type(by_addr[a])
        for a in (PROXY_NODE, LIGHT_2G, SOCKET, GATEWAY, ACTUATOR, PHONE)
    ] == [2, 2, 1, 1, 1, 1]
    two_dimmers = by_addr[ACTUATOR]
    for e in two_dimmers.elements:
        e.models.append("1300")
    assert guess_actuator_function(two_dimmers) == 3


def test_guess_actuator_function_blinds_and_rtr():
    """MOD-02: a blinds node's synthesised `meta` row must say `actuatorFunctionId` 5 ("blind"), an RTR's 8
    ("rtr") — device identity the app reads back (network-features.md §8), not 0/7 as an unmatched load."""
    blinds = CDB.load(FIXTURES / "Blinds.json")
    by_addr = {n.unicast: n for n in blinds.nodes}
    assert guess_actuator_function(by_addr[0x0500]) == 5  # Blinds mini
    assert guess_actuator_function(by_addr[0x0600]) == 5  # PB 2-gang blinds insert
    assert (
        guess_actuator_function(by_addr[0x0700]) == 5
    )  # PP2, primary element 1000+1002

    rtr = CDB.load(FIXTURES / "MeshNetwork-rtr.json")
    by_addr_rtr = {n.unicast: n for n in rtr.nodes}
    assert guess_actuator_function(by_addr_rtr[0x0500]) == 8


# ----------------------------------------------------------------------------- file modes (the export holds every key)


@pytest.mark.parametrize(
    ("target_mode", "expected"),
    [(0o600, 0o600), (0o644, 0o600), (0o400, 0o400)],
    ids=["stored_private", "user_file_world_readable", "user_file_read_only"],
)
def test_save_keeps_the_export_and_its_backup_private_under_a_loose_umask(
    tmp_path: Path, target_mode: int, expected: int
) -> None:
    """The temp file is created 0600 whatever the umask, and neither the rewritten export nor the `.bak` copy ever
    becomes more readable than 0600 (a stricter mode of the target is kept)."""
    old_umask = os.umask(0o022)
    try:
        target = tmp_path / "MeshNetwork.json"
        target.write_bytes(CDB_PATH.read_bytes())
        target.chmod(target_mode)
        bak = tmp_path / "MeshNetwork.json.bak"
        bak.write_text("stale")
        bak.chmod(0o644)  # a backup left by an earlier version: replaced, not reused
        pf = ProjectFile.load(target)
        pf.rename_group(GROUP_WC, "Toilet")
        pf.save(now=FIXED_NOW)
        assert stat.S_IMODE(target.stat().st_mode) == expected
        assert stat.S_IMODE(bak.stat().st_mode) == expected
        assert bak.read_bytes() == CDB_PATH.read_bytes()
        assert CDB.load(target).groups[GROUP_WC] == "Toilet"
        # a save to a path that does not exist yet: private as well
        fresh = tmp_path / "Copy.json"
        pf.save(fresh, now=FIXED_NOW)
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
        assert not (tmp_path / "Copy.json.bak").exists()
    finally:
        os.umask(old_umask)


# ----------------------------------------------------------------------------- null meta lists (P2-21)


def _share_with_meta(tmp_path: Path, meta: dict[str, Any]) -> Path:
    doc = json.loads(ANDROID_PATH.read_text())
    doc["meta"].update(meta)
    p = tmp_path / "JungHome.json"
    p.write_text(json.dumps(doc))
    return p


def test_null_meta_lists_load_and_read_as_empty(tmp_path: Path):
    """`null` where the schema has a list (the Android app writes it for `cachedGroupConnectionMetadata`) is
    treated as empty by every reader and mutator; the `null` stays in the file until something writes the list."""
    p = _share_with_meta(
        tmp_path,
        {
            "userGroups": None,
            "elementConnectionGroups": None,
            "devices": None,
            "scenes": None,
            "sceneInfo": None,
        },
    )
    pf = ProjectFile.load(p)
    assert pf.meta["userGroups"] is None  # kept as loaded
    assert pf.room_connections(GROUP_WC) == []
    assert pf.scene_names() == {1: "Scene #1", 2: "Scene #2"}
    pf.complete_meta()
    assert [g["name"] for g in pf.meta["userGroups"]] == [
        "WC",
        "Living room",
        "Kitchen",
    ]
    assert [s["name"] for s in pf.meta["scenes"]] == ["Scene #1", "Scene #2"]
    assert len(pf.meta["elementConnectionGroups"]) == 13
    pf.remove_scene(1)
    assert pf.meta["sceneInfo"] == []
    pf.remove_group(GROUP_WC)
    assert [g["name"] for g in pf.meta["userGroups"]] == ["Living room", "Kitchen"]
    pf.set_device_name(PROXY_NODE, [1], "Named")
    assert pf.meta["devices"][0]["name"] == "Named"


def test_odd_meta_entries_are_ignored_and_kept(tmp_path: Path):
    """Non-object rows and devices with odd `deviceId` / `locationIds` / `cachedGroupConnectionMetadata` values
    never crash a mutator; the rewrites leave the stray rows where they are."""
    p = _share_with_meta(
        tmp_path,
        {
            "userGroups": [None, {"name": "WC", "address": GROUP_WC, "icon": "x"}],
            "elementConnectionGroups": ["x"],
            "scenes": [None, {"name": "One", "number": 1, "icon": "SceneDay"}],
            "sceneInfo": [None, {"scene": 1}],
            "devices": [
                None,
                {"deviceId": "not an object", "name": "A"},
                {
                    "deviceId": {"nodeId": "n", "locationIds": ["a"]},
                    "name": "B",
                    "cachedGroupConnectionMetadata": None,
                },
                {
                    "deviceId": {"nodeId": "n", "locationIds": None},
                    "name": "C",
                    "cachedGroupConnectionMetadata": [None, {"groupAddress": GROUP_WC}],
                },
                {
                    "deviceId": {"nodeId": UUID_1G, "locationIds": ["a", 1]},
                    "name": "D",  # the right node, unusable locations: never the match
                },
            ],
        },
    )
    pf = ProjectFile.load(p)
    assert pf.room_connections(GROUP_WC) == [{"groupAddress": GROUP_WC}]
    pf.complete_meta()
    assert pf.meta["userGroups"][0] is None
    assert [g["name"] for g in pf.meta["userGroups"][1:]] == [
        "WC",
        "Living room",
        "Kitchen",
    ]
    assert pf.meta["elementConnectionGroups"][0] == "x"
    assert pf.scene_names() == {1: "One", 2: "Scene #2"}
    pf.rename_scene(1, "Uno")
    pf.add_scene("Tres", 3)
    assert [s and s["name"] for s in pf.meta["scenes"]] == [
        None,
        "Uno",
        "Scene #2",
        "Tres",
    ]
    pf.remove_scene(1)
    assert pf.meta["sceneInfo"] == [None]
    assert pf.meta["scenes"][0] is None
    pf.add_group("Attic")
    pf.remove_group(GROUP_WC)
    assert pf.meta["userGroups"][0] is None
    assert pf.meta["devices"][3]["cachedGroupConnectionMetadata"] == [None]
    assert pf.meta["devices"][2]["cachedGroupConnectionMetadata"] is None
    entry = pf.set_device_name(
        PROXY_NODE, [1], "New"
    )  # none of the odd rows matched: a fresh entry
    assert entry is pf.meta["devices"][-1]


# ----------------------------------------------------------------------------- virtual addresses (P2-22)

LABEL = "0073E7E4D8B9440FAF8415DF4C56C0E1"  # virtual address 0xB529 (§3.4.2.3 sample)
VIRTUAL = 0xB529


def _with_virtual_group() -> ProjectFile:
    net = json.loads(CDB_PATH.read_text())["meshNetwork"]
    net["groups"].append({"address": LABEL, "name": "Virtual", "parentAddress": "0000"})
    light = next(n for n in net["nodes"] if n["unicastAddress"] == f"{PROXY_NODE:04X}")
    for m in light["elements"][0]["models"]:
        if m["modelId"] == "1000":
            m["subscribe"].append(LABEL)
            m["publish"] = {"address": LABEL, "index": 0, "ttl": 255}
    return ProjectFile(CDB.from_network(net))


def test_a_label_publication_is_its_virtual_address_not_a_bogus_unicast():
    pf = _with_virtual_group()
    assert pf.publication(PROXY_NODE, "1000") == VIRTUAL
    assert not is_room(VIRTUAL, "Virtual")
    assert VIRTUAL not in pf.user_groups()
    assert pf.group_members(VIRTUAL) == [pf.cdb.element(PROXY_NODE)]


def test_subscriptions_to_a_known_label_are_written_back_as_the_label():
    """Rewriting a subscription list keeps a virtual entry as its Label UUID (the label is the address on the
    wire); a virtual address the file has no label for cannot be written."""
    pf = _with_virtual_group()
    light = pf.cdb.element(PROXY_NODE)
    assert light is not None
    before = light.subscriptions("1000")
    assert before[-1] == VIRTUAL
    changes = pf.set_subscriptions(PROXY_NODE, 0, "1000", [*before, GROUP_LIVING])
    assert changes == [ModelChange(PROXY_NODE, "1000", GROUP_LIVING, "subscribe")]
    assert pf._model(light, "1000")["subscribe"][-2] == LABEL
    with pytest.raises(ValueError, match="virtual address without a label"):
        pf.set_subscriptions(PROXY_NODE, 0, "1000", [0x8001])
    changes = pf.remove_group(VIRTUAL)  # the whole group, publication included
    assert ModelChange(PROXY_NODE, "1000", VIRTUAL, "unsubscribe") in changes
    assert ModelChange(PROXY_NODE, "1000", 0, "publish") in changes
    assert VIRTUAL not in pf.cdb.groups
    assert all(g["address"] != LABEL for g in pf.net["groups"])
    assert pf.publication(PROXY_NODE, "1000") is None


# ----------------------------------------------------------------------------- symlinked target


def test_save_through_a_symlink_writes_the_linked_file_and_keeps_the_link(
    tmp_path: Path,
):
    """An entry pointing at a link (a synced app-container copy) must update the original: the temp file, the
    `.bak` and the replace all happen on the resolved path, and the link stays a link."""
    real_dir = tmp_path / "sync"
    real_dir.mkdir()
    real = real_dir / "MeshNetwork.json"
    real.write_bytes(CDB_PATH.read_bytes())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    pf = ProjectFile.load(link)
    pf.rename_group(GROUP_WC, "Toilet")
    assert pf.save(now=FIXED_NOW) == link
    assert link.is_symlink()
    assert pf.path == link
    assert CDB.load(real).groups[GROUP_WC] == "Toilet"
    assert (real_dir / "MeshNetwork.json.bak").read_bytes() == CDB_PATH.read_bytes()
    assert not (tmp_path / "link.json.bak").exists()
    assert list(tmp_path.glob(".*.tmp")) == []
    assert list(real_dir.glob(".*.tmp")) == []


# ----------------------------------------------------------------------------- canonical node UUIDs


def test_nodes_are_found_by_any_form_of_their_uuid():
    pf = ProjectFile.load(CDB_PATH)
    proxy = pf.cdb.node_by_addr(PROXY_NODE)
    assert proxy is not None
    undashed = proxy.uuid.replace("-", "").lower()
    assert pf._node(undashed) is proxy
    entry = pf.set_device_name(undashed, [1], "Mirror")
    assert entry["deviceId"]["nodeId"] == proxy.uuid  # written in the canonical form
    pf.meta["devices"][-1]["deviceId"]["nodeId"] = (
        undashed  # a meta block from an older library
    )
    assert pf.set_device_name(proxy, [1], "Mirror 2") is entry  # still the same device
    assert entry["name"] == "Mirror 2"


def test_set_publication_writes_a_label_for_a_virtual_address():
    """MOD-06: like `set_subscriptions`, a virtual publish address is written as its Label UUID; one without a
    known label is refused before the entry is touched."""
    pf = ProjectFile.load(CDB_PATH)
    pf.cdb.virtual_labels[0x8123] = bytes(range(16))
    node = pf.cdb.node_by_addr(0x0148)
    assert node is not None
    pf.set_publication(node, 0, "1000", 0x8123)
    element = pf._element(node, 0)
    model = pf._model(element, "1000")
    assert model["publish"]["address"] == bytes(range(16)).hex().upper()
    before = dict(model["publish"])
    with pytest.raises(ValueError, match="without a label"):
        pf.set_publication(node, 0, "1000", 0x8124)
    assert model["publish"] == before


def test_free_addresses_skip_reserved_values():
    """MOD-09: a provisioner range reaching into the device-type / time-keeper groups, or starting at scene 0,
    never hands those out."""
    pf = ProjectFile.load(CDB_PATH)
    pf.cdb.provisioner_group_ranges = [(0xFEF0, 0xFEFF)]
    for address in range(0xFEF0, 0xFEF5):
        pf.cdb.groups[address] = f"g{address:04X}"
    with pytest.raises(ExportError, match="no free group address"):
        pf.free_group_address()
    pf.cdb.provisioner_scene_ranges = [(0, 5)]
    pf.cdb.scenes.pop(1, None)
    assert pf.free_scene_number() == 1


def test_free_group_address_avoids_every_group_the_export_uses():
    """Review-3 hardening: "free" meant absent from the CDB's `groups[]` only. A group the app keeps only in
    `meta`, or one a node still publishes or listens to, was handed out again — and a new room there inherits
    whatever listens to it. Each kind of use takes the address out, one after the other."""
    pf = ProjectFile.load(ANDROID_PATH)
    node = pf.cdb.node_by_addr(0x0148)
    assert node is not None
    device = next(
        d for d in pf.meta["devices"] if d.get("cachedGroupConnectionMetadata")
    )
    uses: list[Callable[[int], object]] = [
        lambda a: pf.meta["userGroups"].append(
            {"name": "Ghost", "address": a, "icon": "x"}
        ),
        lambda a: pf.meta["elementConnectionGroups"].append(
            {"elementAddress": 0x0999, "groupAddress": a}
        ),
        lambda a: device["cachedGroupConnectionMetadata"].append(
            {
                "elementAddress": "0999",
                "groupAddress": "C00F",
                "publishAddress": f"{a:04X}",
            }
        ),
        lambda a: pf.subscribe(0x0148, "1000", a),
        lambda a: pf.set_publication(node, 0, "1000", a),
    ]
    for use in uses:
        address = pf.free_group_address()
        assert address not in pf.cdb.groups
        assert address not in pf.used_group_addresses()
        use(address)
        assert address in pf.used_group_addresses()
        assert pf.free_group_address() != address


def test_load_rejects_a_non_object_network_payload(tmp_path: Path):
    """MOD-11: `ProjectFile.load` validates like `CDB.parse` (one parser now): a `network` that decodes to a
    list is an InvalidExport, not an AttributeError."""
    path = tmp_path / "x.json"
    path.write_text(
        json.dumps(
            {"version": "1.1", "meta": {}, "network": base64.b64encode(b"[]").decode()}
        )
    )
    with pytest.raises(InvalidExport):
        ProjectFile.load(path)


# ----------------------------------------------------------------------------- allocating away from the app (W4-2)


def test_top_allocation_takes_the_highest_free_numbers_and_the_app_its_lowest():
    """Review-4 D6 (W4-2): the app never downloads the project, so it gives its next room and scene the lowest
    numbers it believes free. Home Assistant's "top" allocation takes the other end of the same ranges (C000..C64B,
    scenes 1..1999 here): the app's next room and scene get other numbers than Home Assistant's."""
    pf = ProjectFile.load(ANDROID_PATH)
    app = ProjectFile.load(
        ANDROID_PATH
    )  # the app's copy: it never sees what Home Assistant adds
    assert pf.allocation == "app"  # the library (and the CLI) mirror the app
    pf.allocation = "top"
    assert pf.add_group("Attic") == 0xC64B
    assert pf.add_group("Loft") == 0xC64A
    assert pf.add_scene("Evening") == 0x1999
    assert pf.add_scene("Night") == 0x1998
    assert app.add_group("From the app") == 0xC002
    assert app.add_scene("Morning") == 3
    # an explicit policy wins over the file's; `avoid` takes numbers the export does not show
    assert pf.free_group_address(policy="app") == 0xC002
    assert pf.free_group_address(avoid=[0xC649]) == 0xC648
    assert pf.free_scene_number(policy="app") == 3
    assert pf.free_scene_number(avoid=range(0x1990, 0x1998)) == 0x198F
    # a number a device still holds (a forced deletion skipped it) is passed on by `add_scene`
    assert pf.add_scene("Late", avoid=[0x1997]) == 0x1996


def test_top_allocation_without_provisioner_ranges_stays_below_the_device_type_groups():
    pf = _with_provisioners(None)
    pf.allocation = "top"
    assert pf.free_group_address() == 0xFEF4  # never the reserved FEF5..FEFF
    assert pf.free_scene_number() == 0xFFFF


def test_a_top_pick_that_comes_close_to_the_apps_numbers_is_refused():
    """The range is no longer sparse: fewer than `ALLOCATION_MARGIN` free numbers below the pick, and the app's
    next allocations would reach it."""
    pf = _with_provisioners(
        [
            {
                "allocatedGroupRange": [{"lowAddress": "C000", "highAddress": "C03F"}],
                "allocatedSceneRange": [{"firstScene": "0001", "lastScene": "0050"}],
            }
        ]
    )
    pf.allocation = "top"
    with pytest.raises(AllocationCrowded) as err:
        pf.free_group_address()
    assert err.value.what == "group address"
    assert err.value.below < ALLOCATION_MARGIN
    assert f"free group address(s) left below {err.value.pick:04X}" in str(err.value)
    assert isinstance(err.value, ExportError)
    assert pf.free_scene_number() == 0x50  # 77 free below: enough
    with pytest.raises(AllocationCrowded, match="scene number"):
        pf.free_scene_number(avoid=range(3, 30))
    # the app's own rule is never refused for being close
    assert pf.free_group_address(policy="app") == 0xC002


def test_a_scene_number_a_key_still_recalls_is_not_handed_out_again():
    """Review-4 W4-8: `remove_scene` leaves the `keyModeSceneConfigExports` rows (the keys still send the number);
    the next `create_scene` took the number again, and the old key now recalled the new scene."""
    pf = ProjectFile.load(ANDROID_PATH)
    assert pf.key_scene_numbers() == {1}  # the fixture's key 0149 recalls scene 1
    pf.remove_scene(1)
    assert 1 not in pf.cdb.scenes
    assert pf.meta[
        "keyModeSceneConfigExports"
    ]  # the key row stays: the key still sends 1
    assert pf.free_scene_number() == 3  # not 1
    assert pf.add_scene("Again") == 3
    # rows that name no scene, or not as a number, are no obstacle
    pf.meta["keyModeSceneConfigExports"] += [
        {"elementAddress": 0x0301},
        {"elementAddress": 0x0302, "sceneConfig": None},
        {"elementAddress": 0x0303, "sceneConfig": {"sceneId": "x"}},
        None,
    ]
    assert pf.key_scene_numbers() == {1}
    del pf.meta["keyModeSceneConfigExports"]
    assert pf.key_scene_numbers() == set()
    assert pf.free_scene_number() == 1
