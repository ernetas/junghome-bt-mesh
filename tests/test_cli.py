"""tools/cli_ops.py (and the link-free paths of tools/mesh_poc.py): parsing, property codecs, Config requests, exports."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jhmesh import audit
from jhmesh import config_messages as C
from jhmesh import messages as M
from jhmesh import properties as P
from jhmesh import vendor_models as V
from jhmesh.cdb import CDB
from jhmesh.client import AccessMessage, LocalState
from jhmesh.pdu import decode_opcode, encode_opcode
from jhmesh.provisioning import (
    Capabilities,
    ProvisioningData,
    ProvisioningError,
    ProvisioningResult,
    UnprovisionedDevice,
)
from tools import cli_ops as ops
from tools import mesh_poc

from .jhmesh.conftest import FakeConfigServers, FastAsyncio

FIXTURES = Path(__file__).parent / "fixtures"
CDB_PATH = FIXTURES / "MeshNetwork.json"
SHARE_PATH = FIXTURES / "JungHome.json"
h = bytes.fromhex
KEY = 0x0149
NODE = 0x0148
COMPOSITION_PAGE0 = (
    h("00")
    + h("2705 0100 0200 2800 0300")
    + h("0100 02 01 0000 0010 2705 1310")
    + h("4000 00 01 2705 1510")
)


def message(
    src: int, dst: int, access_pdu: bytes, *, ttl: int = 3, seq: int = 0x1234
) -> AccessMessage:
    op, cid, params = M.decode_opcode(access_pdu)
    return AccessMessage(src, dst, ttl, seq, op, cid, params, access_pdu, "app0")


def vendor_status(kind: str, pid: int, value: bytes, access: int = 3) -> AccessMessage:
    return message(
        KEY, 0x0D01, M.vendor_property_status(kind, pid, value, user_access=access)
    )


# ----------------------------------------------------------------------------- argument parsing


def test_parse_address():
    assert ops.parse_address("0D01") == 0x0D01
    assert ops.parse_address("0x0149") == 0x0149
    for bad in ("zz", "0000", "C000", "8000"):
        with pytest.raises(argparse.ArgumentTypeError):
            ops.parse_address(bad)


def test_parse_hex_model_and_product():
    assert ops.parse_hex("C022") == 0xC022
    assert ops.parse_hex("0xc022") == 0xC022
    with pytest.raises(argparse.ArgumentTypeError):
        ops.parse_hex("room")
    assert ops.parse_model("1000") == "1000"
    assert ops.parse_model("0x05271013") == "05271013"
    assert ops.parse_model("05271013") == "05271013"
    for bad in ("zz", "123456789"):
        with pytest.raises(argparse.ArgumentTypeError):
            ops.parse_model(bad)
    assert ops.parse_product("01") == 1
    assert ops.parse_product("0x10") == 0x10
    with pytest.raises(argparse.ArgumentTypeError, match="unknown product"):
        ops.parse_product("99")


def test_resolve_property_by_name_and_id():
    assert ops.resolve_property("key_mode") == (0x5003, P.spec_for(0x5003))
    assert ops.resolve_property("5003") == (0x5003, P.spec_for(0x5003))
    assert ops.resolve_property("0x5003") == (0x5003, P.spec_for(0x5003))
    assert ops.resolve_property("006A") == (
        0x006A,
        P.spec_for(0x006A, sig=True),
    )  # SIG-only id
    assert ops.resolve_property("0002") == (
        0x0002,
        P.spec_for(0x0002),
    )  # vendor wins over SIG
    assert ops.resolve_property("6000") == (0x6000, None)  # unknown id
    with pytest.raises(argparse.ArgumentTypeError, match="neither"):
        ops.resolve_property("no_such_property")
    with pytest.raises(argparse.ArgumentTypeError, match="16-bit"):
        ops.resolve_property("12345")


# ----------------------------------------------------------------------------- property values


@pytest.mark.parametrize(
    ("prop", "text", "wire"),
    [
        ("key_mode", "gateway", h("06")),  # enum by name
        ("key_mode", "6", h("06")),  # enum by number
        ("automatic_dst", "on", h("01")),
        ("automatic_dst", "FALSE", h("00")),
        ("on_delay", "1.5", h("dc050000")),  # duration in seconds -> ms u32
        (
            "device_lock",
            "factory_reset_time_limit,local_devices_lock",
            h("0600"),
        ),  # bits 1 and 2
        ("device_lock", "none", h("0000")),
        ("device_lock", "0x3", h("0300")),
        ("secure_element_version", "0.1.2.13", h("0d020100")),
        ("key_property_value_up", "0201", h("0201")),  # raw: hex
        ("led1_mode_on", "hex:64000005", h("64000005")),  # struct-like: hex escape
        ("gateway_ip", "192.168.1.5", b"192.168.1.5"),
    ],
)
def test_parse_value(prop: str, text: str, wire: bytes):
    _, spec = ops.resolve_property(prop)
    assert ops.parse_value(spec, text) == wire


def test_parse_value_errors_and_unknown_ids():
    assert ops.parse_value(None, "0102") == h("0102")  # unknown id: raw bytes
    _, spec = ops.resolve_property("automatic_dst")
    with pytest.raises(ValueError, match="not a boolean"):
        ops.parse_value(spec, "maybe")
    _, spec = ops.resolve_property("led1_mode_on")
    with pytest.raises(ValueError, match="no text form"):
        ops.parse_value(spec, "red")
    with pytest.raises(ValueError, match="not hex"):
        ops.parse_value(None, "xyz")
    _, spec = ops.resolve_property("key_mode")
    with pytest.raises(ValueError, match="unknown option"):
        ops.parse_value(spec, "sideways")


# ----------------------------------------------------------------------------- property requests


def test_property_get_picks_the_hosting_server():
    req = ops.property_get(*ops.resolve_property("key_mode"))
    assert req == ops.PropertyRequest(
        0x5003, "admin", M.vendor_property_get("admin", 0x5003), 0x05, M.JUNG_CID
    )
    assert req.describe == "LBC Admin Property Get prop 0x5003 key_mode"
    req = ops.property_get(*ops.resolve_property("key_mode"), server="user")
    assert (req.server, req.pdu, req.expect_opcode) == (
        "user",
        M.vendor_property_get("user", 0x5003),
        0x11,
    )
    req = ops.property_get(*ops.resolve_property("total_energy"))
    assert (req.server, req.pdu, req.expect_opcode, req.expect_cid) == (
        "sig_admin",
        M.generic_property_get("admin", 0x006A),
        M.GEN_ADMIN_PROP_STATUS,
        None,
    )
    req = ops.property_get(*ops.resolve_property("software_version"))
    assert (req.pdu, req.expect_opcode) == (
        M.generic_property_get("manufacturer", 0x001A),
        M.GEN_MANU_PROP_STATUS,
    )
    req = ops.property_get(*ops.resolve_property("active_power"))
    assert (req.server, req.pdu, req.expect_opcode) == (
        "sensor",
        M.sensor_get(0x0081),
        M.SENSOR_STATUS,
    )
    req = ops.property_get(0x6000, None, server="sig_user")
    assert req.pdu == M.generic_property_get("user", 0x6000)
    with pytest.raises(ValueError, match="--server"):
        ops.property_get(0x6000, None)
    with pytest.raises(ValueError, match="server must be"):
        ops.property_get(0x6000, None, server="owner")


def test_property_set_builds_the_app_message():
    pid, spec = ops.resolve_property("key_mode")
    req = ops.property_set(pid, spec, "gateway")
    assert req.pdu == M.vendor_property_set(
        "admin", 0x5003, h("06"), ack=True, user_access=3
    )
    assert (
        req.describe
        == "LBC Admin Property Set prop 0x5003 access=3 value=06 key_mode=gateway"
    )
    pid, spec = ops.resolve_property("dim_mode")  # the app writes it with access 1
    req = ops.property_set(pid, spec, "trailing_edge", ack=False)
    assert req.pdu == M.vendor_property_set(
        "admin", 0x0013, h("02"), ack=False, user_access=1
    )
    req = ops.property_set(pid, spec, "trailing_edge", user_access=3)
    assert req.pdu[3:] == h("1300") + h("03") + h("02")
    pid, spec = ops.resolve_property(
        "key_status_led"
    )  # manufacturer server: no access byte
    req = ops.property_set(pid, spec, "on", server="user")
    assert req.pdu == M.vendor_property_set("user", 0x5013, h("01"))
    pid, spec = ops.resolve_property(
        "total_energy"
    )  # SIG admin server, value 0 = reset
    req = ops.property_set(pid, spec, "0")
    assert req.pdu == M.generic_property_set("admin", 0x006A, bytes(4), user_access=3)
    assert req.expect_opcode == M.GEN_ADMIN_PROP_STATUS
    req = ops.property_set(0x6004, None, "2c01", server="manufacturer")
    assert req.pdu == M.vendor_property_set("manufacturer", 0x6004, h("2c01"))
    with pytest.raises(ValueError, match="read-only"):
        ops.property_set(*ops.resolve_property("active_power"), "1")


def test_property_status_text():
    assert (
        ops.property_status_text(vendor_status("admin", 0x5003, h("06")), 0x5003)
        == "key_mode=gateway (access=3 raw=06)"
    )
    assert (
        ops.property_status_text(vendor_status("user", 0x5003, b"", access=1), 0x5003)
        == "key_mode=? (access=1 raw=-)"
    )  # a load element answering a key property
    assert (
        ops.property_status_text(vendor_status("admin", 0x5003, h("06")), 0x5001)
        == "status of another property: LBC Admin Property Status prop 0x5003 access=3 value=06 key_mode=gateway"
    )
    assert ops.property_status_text(
        message(KEY, 0x0D01, encode_opcode(0x05, M.JUNG_CID) + b"\x03"), 0x5003
    ).startswith("malformed status: ")
    # SIG statuses
    energy = message(
        0x0172,
        0x0D01,
        encode_opcode(M.GEN_ADMIN_PROP_STATUS) + h("6a00") + h("03") + h("393000"),
    )
    assert (
        ops.property_status_text(energy, 0x006A)
        == "total_energy=12345Wh (access=3 raw=393000)"
    )
    sensor = message(
        0x0173,
        0xC001,
        encode_opcode(M.SENSOR_STATUS) + h("05") + h("8100") + h("a00100"),
    )
    assert ops.property_status_text(sensor, 0x0081) == "active_power=41.6W (raw=a00100)"
    assert ops.property_status_text(sensor, 0x005D).startswith(
        "sensor status without property 0x005D"
    )


def test_property_rows():
    rows = ops.property_rows(0x01)
    assert rows[0].startswith("0x0001 device_lock")
    assert all("(firmware only)" not in row for row in rows)
    key_mode = next(row for row in rows if "key_mode" in row)
    assert "admin" in key_mode
    assert "enum" in key_mode
    on_delay = next(row for row in rows if "on_delay" in row)
    assert on_delay.endswith("duration s 0..14400")
    dst = next(row for row in rows if "automatic_dst" in row)
    assert "fw>=1.1.0.0" in dst
    everything = ops.property_rows(0x01, include_firmware_only=True)
    assert len(everything) > len(rows)
    assert any("(firmware only)" in row for row in everything)
    assert any("Wh" in row for row in ops.property_rows(0x03))  # socket: energy counter


# ----------------------------------------------------------------------------- config requests


def test_config_requests():
    req = ops.config_composition(NODE)
    assert (req.node, req.pdu, req.expect_opcode) == (
        NODE,
        C.composition_data_get(0),
        C.CONFIG_COMPOSITION_DATA_STATUS,
    )
    assert req.describe == "Config Composition Data Get page=0"
    req = ops.config_publication(NODE, KEY, "1001", 0xC022)
    assert req.pdu == C.model_publication_set(KEY, 0xC022, "1001")
    assert req.expect_opcode == C.CONFIG_MODEL_PUBLICATION_STATUS
    req = ops.config_publication(NODE, KEY, "1001", None)
    assert req.pdu == C.model_publication_get(KEY, "1001")
    req = ops.config_subscription(NODE, NODE, "1000", 0xC022, add=True)
    assert req.pdu == C.model_subscription_add(NODE, 0xC022, "1000")
    assert req.expect_opcode == C.CONFIG_MODEL_SUBSCRIPTION_STATUS
    req = ops.config_subscription(NODE, NODE, "1000", 0xC022, add=False)
    assert req.pdu == C.model_subscription_delete(NODE, 0xC022, "1000")
    req = ops.config_subscriptions(NODE, NODE, "1000")
    assert req.expect_opcode == C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST
    req = ops.config_subscriptions(NODE, NODE, "05271013")
    assert req.expect_opcode == C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST
    assert req.pdu == C.model_subscription_get(NODE, "05271013")
    req = ops.config_bind(NODE, KEY, "1001")
    assert (req.pdu, req.expect_opcode) == (
        C.model_app_bind(KEY, "1001"),
        C.CONFIG_MODEL_APP_STATUS,
    )
    assert ops.config_bind(NODE, KEY, "1001", bind=False).pdu == C.model_app_unbind(
        KEY, "1001"
    )


def test_config_status_text():
    reply = message(
        NODE,
        0x0D01,
        encode_opcode(C.CONFIG_COMPOSITION_DATA_STATUS) + COMPOSITION_PAGE0,
    )
    assert ops.config_status_text(reply).splitlines() == [
        "cid=0527 pid=0001 vid=0002 crpl=40 features: relay=1 proxy=1 friend=0 lpn=0",
        "  element 0 loc=0001 models=0000,1000,05271013",
        "  element 1 loc=0040 models=05271015",
    ]
    reply = message(
        NODE,
        0x0D01,
        encode_opcode(C.CONFIG_MODEL_APP_STATUS)
        + h("00")
        + h("4901")
        + h("0000")
        + h("0110"),
    )
    assert (
        ops.config_status_text(reply)
        == "Config Model App Status Success: elem=0149 appkey=0 model=1001"
    )


# ----------------------------------------------------------------------------- listen


def test_format_message_and_filter():
    m = message(
        KEY,
        0xC005,
        M.vendor_property_set("user", 0x5012, bytes([3, 1]), ack=False),
        ttl=3,
        seq=0x0A0B0C,
    )
    when = datetime(2026, 1, 15, 10, 20, 30, 123456)  # noqa: DTZ001  # the wall clock `listen` stamps with
    assert ops.format_message(m, when) == (
        "10:20:30.123 0149→C005 ttl=3 seq=0A0B0C [app0] LBC User Property Set Unack prop 0x5012 value=0301"
        " key_event=KeyEvent(counter=3, event='pushed_up')"
    )
    assert ops.format_message(m, when, "rocker top").endswith("  # rocker top")
    assert ops.message_matches(m, None, None)
    assert ops.message_matches(m, KEY, None)
    assert ops.message_matches(m, None, 0xC005)
    assert not ops.message_matches(m, NODE, None)
    assert not ops.message_matches(m, KEY, 0xC006)


# ----------------------------------------------------------------------------- export round trip


def test_roundtrip_export_is_byte_identical_for_both_flavours(tmp_path: Path):
    for path in (CDB_PATH, SHARE_PATH):
        rendered, diff = ops.roundtrip_export(path)
        assert diff == [], path.name
        # the writer always ends the file with a newline; the fixtures (like the app's files) have none
        assert rendered == path.read_text() + "\n"
        assert ops.summarize_diff(diff) == "identical"
    copy = tmp_path / "MeshNetwork.json"
    text = CDB_PATH.read_text().replace('"meshName": "', '"meshName": "changed ')
    assert text != CDB_PATH.read_text()
    copy.write_text(text)
    rendered, diff = ops.roundtrip_export(copy)
    assert rendered == text + "\n"  # the writer keeps the loaded layout and content
    assert diff == []
    # a file the writer would change shows up as a diff (here: a re-indented copy)
    reindented = tmp_path / "indented.json"
    reindented.write_text(text.replace("\n ", "\n    "))
    rendered, diff = ops.roundtrip_export(reindented)
    assert diff
    assert diff[0].startswith("--- ")
    assert "identical" not in ops.summarize_diff(diff)


def test_summarize_diff_truncates():
    long = ["-" + "x" * 300, "+" + "y" * 300]
    shown = ops.summarize_diff(long)
    assert shown.splitlines()[0].endswith("...")
    assert len(shown.splitlines()[0]) == 160
    many = [f"+line {i}" for i in range(50)]
    assert ops.summarize_diff(many, limit=40).splitlines()[-1] == "... 10 more lines"


@pytest.mark.parametrize(
    "argv",
    [
        ["get", "not-a-target"],
        ["scene", "living-rom", "7"],
        ["config", "subscribe", "0148", "0148", "1000", "not-a-group"],
    ],
)
def test_cmd_get_rejects_an_unknown_target_before_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> None:
    """TLC-02: a target / group that is neither a group name nor hex is refused with a clean message before any
    BLE connection is made, not with a ValueError traceback after one."""

    async def must_not_connect(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("must not connect")

    # never the real tools/ state files, whatever the code under test gets to
    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", tmp_path / ".jhmesh_state.json")

    monkeypatch.setattr(mesh_poc, "connect", must_not_connect)
    monkeypatch.setattr(mesh_poc, "scan_for_proxies", must_not_connect)
    with pytest.raises(SystemExit) as exc:
        mesh_poc.main(["--cdb", str(CDB_PATH), *argv])
    assert "is neither a group name nor a hex address" in str(exc.value.code)
    assert "not-a-" in str(exc.value.code) or "living-rom" in str(exc.value.code)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["get", "0148", "not_a_real_property_xyz"],
            "neither a property name nor a hex id",
        ),
        (["get", "0148", "abcd"], "is not in the catalogue"),
        (["set", "0148", "key_mode", "notabool"], "unknown option"),
    ],
)
def test_cmd_prop_get_and_set_reject_bad_property_input_cleanly(
    argv: list[str], message: str
) -> None:
    """TLC-01: a bad property, id or value is a one-line error, not a traceback (nothing connects first)."""
    with pytest.raises(SystemExit) as exc:
        mesh_poc.main(["--cdb", str(CDB_PATH), "prop", *argv])
    assert isinstance(exc.value.code, str)
    assert message in exc.value.code


@pytest.mark.parametrize(
    "argv",
    [
        ["--cdb", "/nonexistent/x.json", "devices"],
        ["--cdb", "/nonexistent/x.json", "get", "0148"],
        ["export", "write", "/nonexistent/x.json"],
    ],
)
def test_missing_cdb_file_is_a_clean_error(argv: list[str]) -> None:
    """TLC-04: a missing or unreadable input file is a one-line error naming it, not a traceback."""
    with pytest.raises(SystemExit, match="No such file") as exc:
        mesh_poc.main(argv)
    assert "/nonexistent/x.json" in str(exc.value.code)


def test_an_unparsable_cdb_file_is_a_clean_error(tmp_path: Path) -> None:
    """TLC-04: so is a file that is not an export at all."""
    bad = tmp_path / "x.json"
    bad.write_text("{not json")
    with pytest.raises(SystemExit) as exc:
        mesh_poc.main(["--cdb", str(bad), "devices"])
    assert str(bad) in str(exc.value.code)


def test_scene_number_is_validated_by_argparse() -> None:
    """TLC-03: a scene number is parsed by argparse, before anything connects."""
    parser = mesh_poc.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scene", "0148", "seven"])
    assert parser.parse_args(["scene", "0148", "7"]).number == 7
    assert parser.parse_args(["scene", "0148", "07"]).number == 7  # decimal, as before
    assert parser.parse_args(["scene-actions", "0148", "0x10"]).number == 16
    assert parser.parse_args(["scene-actions", "0148"]).number is None
    with pytest.raises(SystemExit):
        parser.parse_args(["scene-actions", "0148", "x"])


def test_export_write_never_prints_keys_of_a_plain_cdb(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TLC-06: in a plain CDB every key sits on a short line of its own, and the diff shows lines around a change
    as context: `export write` must redact them however the round trip differs."""
    net = json.loads(CDB_PATH.read_text())["meshNetwork"]
    keys = [k["key"] for k in net["netKeys"] + net["appKeys"]] + [
        n["deviceKey"] for n in net["nodes"] if n.get("deviceKey")
    ]
    assert len(keys) == 9
    text = CDB_PATH.read_text().replace('"cid": "0527"', '"cid":"0527"')
    p = tmp_path / "MeshNetwork.json"
    p.write_text(text.replace("\n ", "\n    "))
    assert mesh_poc.main(["export", "write", str(p)]) == 0
    out = capsys.readouterr().out
    assert "@@" in out
    for key in keys:
        assert key.lower() not in out.lower()


def test_export_write_hides_the_network_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TLC-06: an app export's `network` line is the Base64 of the whole CDB, every key included: when it differs
    only its length is shown."""
    doc = json.loads((FIXTURES / "JungHome-android.json").read_text())
    inner = base64.b64decode(doc["network"]).decode()
    alt = base64.b64encode(
        inner.replace('"Test network"', '"Test \\u003d network"').encode()
    ).decode()  # a Gson-style escape the writer normalises away: the round trip differs on this line
    p = tmp_path / "JungHome.json"
    p.write_text(json.dumps({**doc, "network": alt}, indent=2))
    assert mesh_poc.main(["export", "write", str(p)]) == 0
    out = capsys.readouterr().out
    assert "@@" in out
    assert alt[:40] not in out
    assert doc["network"][:40] not in out


# ----------------------------------------------------------------------------- mesh_poc.py: parser and link-free commands


def test_parser_accepts_the_capture_commands():
    ap = mesh_poc.build_parser()
    args = ap.parse_args(
        ["listen", "--seconds", "120", "--src", "0293"]
    )  # options after the command
    assert (args.cmd, args.seconds, args.src, args.dst) == (
        "listen",
        120.0,
        "0293",
        None,
    )
    args = ap.parse_args(["prop", "get", "0149", "key_mode", "--server", "user"])
    assert (args.cmd, args.prop_cmd, args.target, args.property, args.server) == (
        "prop",
        "get",
        "0149",
        "key_mode",
        "user",
    )
    args = ap.parse_args(
        ["prop", "set", "0149", "5003", "gateway", "--unack", "--access", "1"]
    )
    assert (args.prop_cmd, args.value, args.unack, args.access) == (
        "set",
        "gateway",
        True,
        1,
    )
    args = ap.parse_args(["prop", "list", "0x0A", "--all"])
    assert (args.product, args.all) == (0x0A, True)
    args = ap.parse_args(["config", "get-composition", "0148"])
    assert (args.config_cmd, args.node, args.page) == ("get-composition", 0x0148, 0)
    args = ap.parse_args(["config", "publication", "0148", "0149", "1001", "C022"])
    assert (args.node, args.element, args.model, args.group) == (
        0x0148,
        0x0149,
        "1001",
        "C022",
    )
    args = ap.parse_args(["config", "publication", "0148", "0149", "1001"])
    assert args.group is None
    args = ap.parse_args(["config", "subscribe", "0148", "0148", "1000", "Living room"])
    assert args.group == "Living room"
    args = ap.parse_args(["config", "bind", "0148", "0149", "05271015"])
    assert args.model == "05271015"
    args = ap.parse_args(["export", "write", "x.json", "--out", "y.json", "--strict"])
    assert (args.export_cmd, args.file, args.out, args.strict) == (
        "write",
        "x.json",
        "y.json",
        True,
    )
    args = ap.parse_args(["scan", "--adv"])
    assert args.adv is True
    args = ap.parse_args(["ctlrange", "016A"])
    assert args.target == "016A"
    with pytest.raises(SystemExit):
        ap.parse_args(["config", "subscribe", "0148", "0148", "1000"])  # group required
    with pytest.raises(SystemExit):
        ap.parse_args(["prop", "list", "zz"])


def test_main_prop_list_and_export_write(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    assert mesh_poc.main(["prop", "list", "03"]) == 0
    out = capsys.readouterr().out
    assert "total_energy" in out
    assert "0x5004 turn_on_threshold" in out
    out_path = tmp_path / "copy.json"
    assert (
        mesh_poc.main(
            ["export", "write", str(CDB_PATH), "--out", str(out_path), "--strict"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "identical"
    assert out_path.read_text() == CDB_PATH.read_text() + "\n"
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600  # every mesh key is in there


def test_write_private_ignores_the_umask_and_tightens_an_existing_file(
    tmp_path: Path,
) -> None:
    old_umask = os.umask(0o022)
    try:
        fresh = tmp_path / "fresh.json"
        ops.write_private(fresh, "{}")
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
        loose = tmp_path / "loose.json"
        loose.write_text("old")
        loose.chmod(0o644)
        ops.write_private(loose, "new")
        assert loose.read_text() == "new"
        assert stat.S_IMODE(loose.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)


def test_write_private_replaces_the_file_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails partway leaves the old export whole (it is written aside, then renamed over)."""
    target = tmp_path / "MeshNetwork.json"
    target.write_text("old")

    def refuse(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        ops.write_private(target, "new")
    assert target.read_text() == "old"
    assert not list(tmp_path.glob(".MeshNetwork.json.*"))  # no temporary file left


def test_print_scan_with_advertisement_details(capsys: pytest.CaptureFixture[str]):
    adv = SimpleNamespace(
        local_name="JUNG HOME",
        manufacturer_data={0x0527: h("01010000")},
        service_uuids=["00001828-0000-1000-8000-00805f9b34fb"],
    )
    cands = [
        SimpleNamespace(
            rssi=-50,
            kind="network-id",
            address="AA:BB",
            name=None,
            node_addr=None,
            adv=adv,
        ),
        SimpleNamespace(
            rssi=-60,
            kind="node-identity",
            address="CC:DD",
            name="Socket",
            node_addr=0x0172,
            adv=None,
        ),
    ]
    mesh_poc.print_scan(cands, show_adv=False)
    out = capsys.readouterr().out
    assert "local_name" not in out
    assert "node 0172" in out
    mesh_poc.print_scan(cands, show_adv=True)
    out = capsys.readouterr().out
    assert "local_name='JUNG HOME' manufacturer_data={'0x0527': '01010000'}" in out
    assert (
        out.count("local_name") == 1
    )  # nothing to show for the candidate without advertisement data


def test_node_of(capsys: pytest.CaptureFixture[str]):
    cdb = CDB.load(CDB_PATH)
    assert mesh_poc._node_of(cdb, KEY) == NODE
    with pytest.raises(SystemExit):
        mesh_poc._node_of(cdb, 0x7FFF)


# ----------------------------------------------------------------------------- property lists (`prop lists`)


def test_property_status_text_uses_the_requests_server():
    # 0x000D is `energy_since_turn_on` on a SIG server but a schema version on an LBC one; 0x0010 / 0x0011 likewise
    status = message(
        0x0173,
        0x0D01,
        encode_opcode(M.GEN_MANU_PROP_STATUS) + h("0d00") + h("01") + h("f1030000"),
    )
    assert (
        ops.property_status_text(status, 0x000D, "sig_manufacturer")
        == "energy_since_turn_on=1009Wh (access=1 raw=f1030000)"
    )
    assert ops.property_status_text(status, 0x000D).startswith(
        "co_processor_schema_version="
    )  # no server: the JUNG catalogue wins for an id both know
    name = message(
        0x0172,
        0x0D01,
        encode_opcode(M.GEN_MANU_PROP_STATUS)
        + h("1100")
        + h("01")
        + b"Albrecht Jung GmbH & Co.KG\0\0",
    )
    assert (
        ops.property_status_text(name, 0x0011, "sig_manufacturer")
        == "manufacturer_name=Albrecht Jung GmbH & Co.KG (access=1 raw="
        + (b"Albrecht Jung GmbH & Co.KG\0\0").hex()
        + ")"
    )
    assert (
        ops.property_status_text(
            vendor_status("admin", 0x5003, h("06")), 0x5003, "admin"
        )
        == "key_mode=gateway (access=3 raw=06)"
    )


def test_property_list_requests_and_text():
    reqs = ops.property_list_requests()
    assert [r.server for r in reqs] == [
        "admin",
        "manufacturer",
        "user",
        "sig_admin",
        "sig_manufacturer",
        "sig_user",
    ]
    assert reqs[0].pdu == h("c02705")
    assert reqs[0].expect_opcode == 0x01
    assert reqs[0].expect_cid == M.JUNG_CID
    assert reqs[4].pdu == h("822a")
    assert reqs[4].expect_opcode == 0x43
    assert reqs[4].expect_cid is None
    assert all(r.pid == 0 for r in reqs)
    vendor = message(
        0x0173, 0x0D01, encode_opcode(0x01, M.JUNG_CID) + h("0450 0550 000f 1450")
    )
    assert (
        ops.property_list_text("admin", vendor)
        == "4 ids: 5004 turn_on_threshold, 5005 turn_off_threshold, 0F00 transmission_settings, 5014 meter_timestamp"
    )
    sig = message(0x0173, 0x0D01, encode_opcode(0x43) + h("7200 0d00"))
    assert (
        ops.property_list_text("sig_manufacturer", sig)
        == "2 ids: 0072 precise_total_energy, 000D energy_since_turn_on"
    )
    assert (
        ops.property_list_text(
            "sig_admin", message(0x0173, 0x0D01, encode_opcode(0x47))
        )
        == "empty"
    )


class _ListClient:
    """Answers every list request of `cmd_prop_lists` except the SIG admin one; the sensor descriptor too."""

    def __init__(self, cdb) -> None:
        self.cdb = cdb
        self.requests: list[bytes] = []

    async def request(self, dst, pdu, expect_opcode, *, expect_cid=None, **_kw):
        self.requests.append(pdu)
        if expect_opcode == M.GEN_ADMIN_PROPS_STATUS:
            raise TimeoutError
        if expect_opcode == M.SENSOR_DESCRIPTOR_STATUS:
            return message(dst, 0x0D01, encode_opcode(0x51) + h("8100000000030050"))
        body = h("0450") if expect_cid is not None else h("7200")
        return message(dst, 0x0D01, encode_opcode(expect_opcode, expect_cid) + body)


def test_cmd_prop_lists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    cdb = CDB.load(CDB_PATH)
    client = _ListClient(cdb)

    async def with_client(args, fn):
        await fn(client, cdb)

    monkeypatch.setattr(mesh_poc, "with_client", with_client)
    assert mesh_poc.main(["prop", "lists", "0173"]) == 0
    out = capsys.readouterr().out
    assert "0173 (" in out
    assert "admin            1 ids: 5004 turn_on_threshold" in out
    assert "sig_admin        no answer" in out
    assert "sig_manufacturer 1 ids: 0072 precise_total_energy" in out
    assert "Sensor Descriptor Status 0x0081 tol=+0/-0 func=3 interval=4.59s" in out
    assert len(client.requests) == 7
    # an element without a Sensor Server gets no descriptor request
    client.requests.clear()
    assert mesh_poc.main(["prop", "lists", "0148"]) == 0
    assert len(client.requests) == 6
    assert "sensor" not in capsys.readouterr().out.splitlines()[-1]


# ----------------------------------------------------------------------------- config audit (export ↔ device)


def test_audit_texts():
    """The rows, findings and verdict `config audit` prints around `jhmesh.audit`'s result."""
    cdb = CDB.load(CDB_PATH)
    ok = audit.ModelAudit(
        NODE, "1000", 0xC061, (0xC00F, 0xC061), (0,), 0xC061, (0xC00F, 0xC061), (0,)
    )
    assert ops.audit_row_text(ok) == (
        "0148 1000     publish C061 → C061  subscribe C00F,C061 → C00F,C061  appkeys 0 → 0"
    )
    odd = audit.ModelAudit(
        NODE, "1204", 0, (0xC00F,), (), None, (), None, {"app_keys": "Invalid Model"}
    )
    assert ops.audit_row_text(odd) == (
        "0148 1204     publish 0000 → ?  subscribe C00F → -  appkeys - → (Invalid Model)"
    )
    assert ops.finding_text(audit.Finding("node_unanswered")) == "node_unanswered"
    assert ops.finding_text(
        audit.Finding("scene_subscriptions_missing", NODE, "1204", expected=["C00F"])
    ) == ("0148 1204 scene_subscriptions_missing: expected C00F")
    assert ops.finding_text(
        audit.Finding(
            "setting_differs",
            setting="network_transmit",
            expected={"count": 3, "interval": 100},
            actual={"count": 2, "interval": 100},
        )
    ) == ("network_transmit setting_differs: expected 3x100ms, node 2x100ms")
    assert ops.finding_text(
        audit.Finding("setting_differs", setting="beacon", expected=True, actual=False)
    ) == ("beacon setting_differs: expected on, node off")
    silent = audit.NodeAudit(
        NODE, "WC", answered=False, findings=[audit.Finding("node_unanswered")]
    )
    assert ops.audit_text(cdb, silent).splitlines()[1:] == [
        "  no answer (off, asleep or out of reach)"
    ]
    quiet = audit.NodeAudit(
        NODE,
        "WC",
        answered=True,
        settings={
            "default_ttl": {"export": 5, "node": 3},
            "beacon": {"export": None, "node": None},
        },
        models=[
            ok,
            audit.ModelAudit(
                NODE, "1002", 0, (), (0,), 0, (), (0,)
            ),  # only AppKey 0: no row
            audit.ModelAudit(
                0x0149, "1003", 0, (), (0,), 0, (), ()
            ),  # a finding: a row
        ],
        findings=[audit.Finding("app_keys_unbound", 0x0149, "1003", expected=[0])],
    )
    assert ops.audit_text(cdb, quiet).splitlines()[1:] == [
        "  default_ttl 3 (export 5)  beacon ?",
        "  " + ops.audit_row_text(ok),
        "  0149 1003     publish 0000 → 0000  subscribe - → -  appkeys 0 → -",
        "  ! 0149 1003 app_keys_unbound: expected 0",
        "  3 models checked; 1 findings",
    ]
    quiet.findings.clear()
    assert ops.audit_text(cdb, quiet).endswith(
        "3 models checked; everything matches the export"
    )


class _AuditClient:
    """Answers the audit's Gets from the fixture's Configuration Servers; a silent one raises TimeoutError."""

    def __init__(self, servers: FakeConfigServers) -> None:
        self.servers = servers
        self.kwargs: list[dict[str, Any]] = []

    async def request_config(self, node, pdu, expect, **kw):
        self.kwargs.append(kw)
        access = self.servers(node, pdu)
        if access is None:
            raise TimeoutError
        sop, _scid, sparams = decode_opcode(access)
        return AccessMessage(
            node, 0x0D01, 3, 0, sop, None, sparams, access, f"dev:{node:04X}"
        )


def test_cmd_config_audit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    cdb = CDB.load(CDB_PATH)
    servers = FakeConfigServers(cdb)
    servers.subscribe[NODE, "1203"] = [
        0xC061
    ]  # the scene server holds only its element group, as on air
    servers.silent_gets.add((NODE, "1004", "subscriptions"))
    client = _AuditClient(servers)

    async def with_client(args, fn):
        await fn(client, cdb)

    monkeypatch.setattr(mesh_poc, "with_client", with_client)
    monkeypatch.setattr(
        audit, "asyncio", FastAsyncio()
    )  # no real pauses between chunks
    assert mesh_poc.main(["config", "audit", "0149"]) == 0  # any element of the node
    out = capsys.readouterr().out
    assert "relay 1  relay_retransmit 3x90ms  network_transmit 3x100ms" in out
    assert "0148 1000     publish C061 → C061" in out
    assert "! 0148 1203 scene_subscriptions_missing: expected C00F,FEF5" in out
    assert "! 0148 1004 subscriptions_unanswered" in out
    assert "30 models checked; 2 findings" in out
    assert {kw["retries"] for kw in client.kwargs} == {
        1
    }  # --timeout per attempt, one attempt each


# ----------------------------------------------------------------------------- scene actions / scheduler / health / hops


class _VendorModelClient:
    """Answers Scene Action Setup / JH Scheduler / Health Gets from tables keyed by the request bytes after the opcode."""

    def __init__(
        self, cdb, replies: dict[bytes, bytes], silent: set[bytes] = frozenset()
    ) -> None:
        self.cdb = cdb
        self.replies, self.silent = replies, silent
        self.sent: list[bytes] = []
        self.requests: list[bytes] = []

    async def request(self, dst, pdu, expect_opcode, *, expect_cid=None, **_kw):
        self.requests.append(pdu)
        _op, cid, params = decode_opcode(pdu)
        if params in self.silent:
            raise TimeoutError
        return message(
            dst, 0x0D01, encode_opcode(expect_opcode, cid) + self.replies[params]
        )

    async def collect(self, dst, pdu, expect_opcode, *, window):
        self.requests.append(pdu)
        return [
            message(src, 0x0D01, encode_opcode(expect_opcode) + body)
            for src, body in ((0x0172, h("0027058180")), (0x0148, h("002705")))
        ]

    async def send_access(self, dst, pdu):
        self.sent.append(pdu)


def _patch_client(monkeypatch: pytest.MonkeyPatch, client, cdb) -> None:
    async def with_client(args, fn):
        await fn(client, cdb)

    monkeypatch.setattr(mesh_poc, "with_client", with_client)


def test_cmd_scene_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    cdb = CDB.load(CDB_PATH)
    # the node lists scene 1 and a scene 7 the export does not know; scene 2 (empty in the export) is not asked
    replies = {
        h("00000000"): h("0000 0100 0700 0000"),
        h("01000000"): h("0100 01 01 00000000"),
        h("07000000"): h("0700 03 ffff d007 00"),
        h("09000000"): h("0900"),
    }
    client = _VendorModelClient(cdb, replies)
    _patch_client(monkeypatch, client, cdb)
    assert mesh_poc.main(["scene-actions", "0148"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith(": scenes with an action [1, 7], in the export [1]")
    assert out[1] == '  scene 1 "Scene #1": switch on'
    assert out[2] == "  scene 7: lightness 100% 2000K"
    assert len(client.requests) == 3
    # one scene, no answer
    client = _VendorModelClient(cdb, replies, silent={h("09000000")})
    _patch_client(monkeypatch, client, cdb)
    assert mesh_poc.main(["scene-actions", "0148", "9"]) == 0
    assert capsys.readouterr().out == "  scene 9: no answer\n"
    client = _VendorModelClient(cdb, replies)
    _patch_client(monkeypatch, client, cdb)
    assert mesh_poc.main(["scene-actions", "0148", "0x9"]) == 0
    assert capsys.readouterr().out == "  scene 9: no action\n"


def test_cmd_sched(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    cdb = CDB.load(CDB_PATH)
    schedule = (
        h("03")
        + h("07")
        + h("15")
        + (30 | (6 << 6) | (15 << 11) | (22 << 17)).to_bytes(3, "little")
        + h("f1")
        + h("00")
    )
    replies = {
        h("f000"): h("f000")
        + bytes([0b11000000, 0, 0, 0]),  # slot 3 active, the rest available
        h("f002"): h("f002") + bytes(4),
        h("03"): schedule,
        h("13"): h("13") + h("01 01 00000000"),
        h("23"): h("23")
        + h("07")
        + h("15")
        + (42 | (21 << 6)).to_bytes(2, "little")
        + h("00")
        + h("f1"),
        h("00"): h("00000000000000"),
        h("10"): h("10"),  # header only: the node stores nothing for that slot
    }
    client = _VendorModelClient(cdb, replies, silent={h("20")})
    _patch_client(monkeypatch, client, cdb)
    assert mesh_poc.main(["sched", "0148"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith(": slots (central id 0): 15 available, 1 active")
    assert (
        out[1]
        == "  slot 3 schedule: sunset_active mon,wed,fri 06:30-22:15 offset -15min"
    )
    assert out[2] == "  slot 3 action: switch on"
    assert (
        out[3]
        == "  slot 3 effective time: sunset_active mon,wed,fri at 21:42 offset -15min"
    )
    assert mesh_poc.main(["sched", "0148", "--slot", "0", "--central-id", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "  slot 0 schedule: available no days 00:00-00:00 offset +0min",
        "  slot 0 action: nothing",
        "  slot 0 effective time: no answer",
    ]


def test_cmd_health(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    cdb = CDB.load(CDB_PATH)
    replies = {h("2705"): h("0027058180"), h("022705"): h("022705")}
    client = _VendorModelClient(cdb, replies)
    _patch_client(monkeypatch, client, cdb)
    assert mesh_poc.main(["health", "0148"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "  0148 (Push-button 1-gang @0148 el loc 0001): Health Fault Status test=0 company=0527 faults=[0x81 (vendor), 0x80 (vendor)]"
    ]
    assert mesh_poc.main(["health", "0148", "--clear", "--test", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "  faults cleared (reading back)"
    assert out[1].endswith("Health Fault Status test=2 company=0527 faults=[none]")
    assert client.sent == [h("802F2705")]  # the acknowledged Clear, as ever on the air
    assert client.requests[-2] == M.health_fault_test(2)
    # a group: one line per answering node, sorted by address
    assert mesh_poc.main(["health", "FFFF"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("  0148 (")
    assert out[0].endswith("faults=[none]")
    assert out[1].startswith("  0172 (")
    assert "0x81 (vendor), 0x80 (vendor)" in out[1]


def test_hop_probe_plan_and_text():
    cdb = CDB.load(CDB_PATH)
    probe = ops.HopProbe(0x0148, 0x0172, 4, 2)
    assert (probe.count_log, probe.period_log, probe.wait_seconds) == (3, 2, 10.0)
    assert probe.subscribe.pdu == C.heartbeat_subscription_set(0x0172, 0xFFFF, 5)
    assert probe.publish.pdu == C.heartbeat_publication_set(
        0xFFFF, 2, count_log=3, ttl=0x7F
    )
    assert probe.read_publication.pdu == C.heartbeat_publication_get()
    assert probe.read_subscription.pdu == C.heartbeat_subscription_get()
    assert probe.unsubscribe.pdu == C.heartbeat_subscription_off()
    before = C.HeartbeatPublicationStatus(0, 0x0D02, 0xFF, 7, 5, 0, 0)
    assert probe.restore_publication(before).pdu == C.heartbeat_publication_set(
        0x0D02, 7, ttl=5
    )
    assert ops.HopProbe(0x0148, 0x0172, 1, 1).count_log == 1
    assert ops.HopProbe(0x0148, 0x0172, 8, 1).count_log == 4
    status = C.HeartbeatSubscriptionStatus(0, 0x0172, 0xFFFF, 3, 3, 1, 2)
    assert ops.hops_text(cdb, probe, status).endswith(
        ": 4 beats sent, 4..7 counted, 1..2 hops"
    )
    assert ops.hops_text(
        cdb, probe, C.HeartbeatSubscriptionStatus(0, 0x0172, 0xFFFF, 3, 1, 1, 1)
    ).endswith("1 counted, 1..1 hops")
    assert ops.hops_text(
        cdb, probe, C.HeartbeatSubscriptionStatus(0, 0x0172, 0xFFFF, 3, 0, 0x7F, 0)
    ).endswith("0 counted, no beat arrived")


class _HopClient:
    """Plays the node side of `config hops`: records every Config message, answers with fixed statuses."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, bytes]] = []

    async def request_config(self, node, pdu, expect, **_kw):
        self.sent.append((node, pdu))
        if expect == C.CONFIG_HEARTBEAT_PUBLICATION_STATUS:
            body = h("00 020d ff 07 05 0000 0000")
        else:
            body = (
                h("00 7201 ffff 03 03 01 01")
                if len(self.sent) >= 4
                else h("00 7201 ffff 05 00 7f 00")
            )
        access = encode_opcode(expect) + body
        op, _cid, params = decode_opcode(access)
        return AccessMessage(
            node, 0x0D01, 3, 0, op, None, params, access, f"dev:{node:04X}"
        )


def test_cmd_config_hops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    cdb = CDB.load(CDB_PATH)
    client = _HopClient()
    _patch_client(monkeypatch, client, cdb)
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(mesh_poc.asyncio, "sleep", sleep)
    assert (
        mesh_poc.main(["config", "hops", "0149", "0172"]) == 0
    )  # any element of the counting node
    out = capsys.readouterr().out
    assert waited == [10.0]
    assert (
        "0172 (Socket @0172 el loc 0001) → 0148 (Push-button 1-gang @0148 el loc 0001): 4 beats sent, 4..7 counted, 1..1 hops"
        in out
    )
    nodes = [n for n, _ in client.sent]
    pdus = [p for _, p in client.sent]
    assert nodes == [0x0172, 0x0148, 0x0172, 0x0148, 0x0172, 0x0148]
    assert pdus[0] == C.heartbeat_publication_get()
    assert pdus[1] == C.heartbeat_subscription_set(0x0172, 0xFFFF, 5)
    assert pdus[2] == C.heartbeat_publication_set(0xFFFF, 2, count_log=3, ttl=0x7F)
    assert pdus[3] == C.heartbeat_subscription_get()
    assert pdus[4] == C.heartbeat_publication_set(
        0x0D02, 7, ttl=5
    )  # what the source had (HA's heartbeats)
    assert pdus[5] == C.heartbeat_subscription_off()


def test_scene_action_text_prefers_the_share_exports_scene_names():
    cdb = CDB.load(CDB_PATH)
    status = V.SceneActionStatus(1, V.Action(V.ACTION_SWITCH, on=False))
    assert ops.scene_action_text(cdb, status) == 'scene 1 "Scene #1": switch off'
    assert (
        ops.scene_action_text(CDB.load(SHARE_PATH), status)
        == 'scene 1 "WC off (share)": switch off'
    )
    assert ops.scene_action_text(cdb, V.SceneActionStatus(9)) == "scene 9: no action"
    assert (
        ops.scene_action_text(cdb, V.SceneActionStatus(0, scenes=(1, 2)))
        == "scenes=[1, 2]"
    )


# ----------------------------------------------------------------------------- P2-25: set / lightness / ctl values


class _StatusClient:
    """Answers every request with `replies[expect_opcode]` from the addressed element; records what was sent."""

    def __init__(self, replies: dict[int, bytes]) -> None:
        self.replies = replies
        self.requests: list[tuple[int, bytes]] = []
        self.sent: list[tuple[int, bytes]] = []

    async def request(self, dst, pdu, expect_opcode, **_kw):
        self.requests.append((dst, pdu))
        return message(
            dst, 0x0D01, encode_opcode(expect_opcode) + self.replies[expect_opcode]
        )

    async def send_access(self, dst, pdu):
        self.sent.append((dst, pdu))


def test_set_value_is_validated_and_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Anything but on/off/true/false/1/0 is a parser error — before the fix a typo meant OFF for the whole target."""
    ap = mesh_poc.build_parser()
    assert ap.parse_args(["set", "WC", "ON"]).value == "on"
    assert ap.parse_args(["set", "WC", "False"]).value == "false"
    assert ap.parse_args(["set", "WC", "1"]).value == "1"
    for bad in ("yes", "On1", "enable", "oN ", ""):
        with pytest.raises(SystemExit):
            ap.parse_args(["set", "WC", bad])
    client = _StatusClient({M.GEN_ONOFF_STATUS: h("00")})
    _patch_client(monkeypatch, client, CDB.load(CDB_PATH))
    assert mesh_poc.main(["set", "0148", "FALSE"]) == 0
    assert mesh_poc.main(["set", "0148", "true", "--t0"]) == 0
    assert mesh_poc.main(["--window", "0", "set", "0148", "1", "--unack"]) == 0
    on_bits = [M.decode_opcode(pdu)[2][0] for _, pdu in client.requests + client.sent]
    assert on_bits == [0, 1, 1]
    assert "0148 (Push-button 1-gang @0148 el loc 0001): OFF" in capsys.readouterr().out


def test_lightness_and_ctl_values_are_range_checked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A 16-bit value is an argparse error outside 0..65535 (`struct.pack` used to raise a traceback), a colour
    temperature outside the codec's 800..20000 K is one error line."""
    assert ops.parse_uint16("65535") == 65535
    assert ops.parse_uint16("0x1000") == 0x1000
    with pytest.raises(argparse.ArgumentTypeError, match=r"outside 0\.\.65535"):
        ops.parse_uint16("70000")
    with pytest.raises(argparse.ArgumentTypeError, match="not a number"):
        ops.parse_uint16("bright")
    ap = mesh_poc.build_parser()
    assert ap.parse_args(["lightness", "0148", "4096"]).value == 4096
    assert ap.parse_args(["lightness", "0148"]).value is None
    with pytest.raises(SystemExit):
        ap.parse_args(["lightness", "0148", "-1"])
    assert ap.parse_args(["ctl", "0148", "100", "2700"]).kelvin == 2700
    with pytest.raises(SystemExit):
        ap.parse_args(["ctl", "0148", "100", "warm"])
    with pytest.raises(
        SystemExit, match=r"colour temperature 50 K is outside 800\.\.20000"
    ):
        mesh_poc.main(["ctl", "0148", "100", "50"])
    client = _StatusClient(
        {
            M.LIGHT_LIGHTNESS_STATUS: h("0010"),
            M.LIGHT_CTL_STATUS: h("00108c0a"),
        }
    )
    _patch_client(monkeypatch, client, CDB.load(CDB_PATH))
    assert mesh_poc.main(["lightness", "0148", "0x1000"]) == 0
    assert mesh_poc.main(["lightness", "0148"]) == 0
    assert mesh_poc.main(["ctl", "0148", "4096", "2700"]) == 0
    assert [pdu for _, pdu in client.requests] == [
        M.light_lightness_set(0x1000, tid=M.decode_opcode(client.requests[0][1])[2][2]),
        M.light_lightness_get(),
        M.light_ctl_set(4096, 2700, tid=M.decode_opcode(client.requests[2][1])[2][6]),
    ]
    out = capsys.readouterr().out
    assert out.count("Light Lightness Status") == 2
    assert "Light CTL Status" in out


# ----------------------------------------------------------------------------- --transition (review-4 F4-1)


class _FadingClient(_StatusClient):
    """`_StatusClient` whose group requests collect the same reply from one element."""

    async def collect(self, dst, pdu, expect_opcode, **_kw):
        return [await self.request(dst, pdu, expect_opcode)]


def test_parse_transition():
    """Seconds to the transition-time byte (the nearest, finest step); anything no byte carries is an argparse error."""
    assert ops.parse_transition("3") == 0x1E
    assert ops.parse_transition("0") == 0
    assert ops.parse_transition("20") == 0x54
    assert ops.parse_transition("3600") == 0xC6
    for bad in ("-1", "37201", "nan", "inf", "slow", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            ops.parse_transition(bad)


def test_the_transition_option_of_the_sets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """`--transition SECONDS` on `set`, `lightness`, `ctl`, `delta` and `scene` (the probe's): the Set carries the
    byte and delay 0, an OnOff Status mid-transition shows its target and remaining time; `set` takes `--t0` or
    `--transition`, not both; a Get takes none."""
    ap = mesh_poc.build_parser()
    for argv in (
        ["set", "0148", "on", "--transition", "3"],
        ["lightness", "0148", "6553", "--transition", "3"],
        ["ctl", "0148", "100", "2700", "--transition", "3"],
        ["delta", "0148", "-6553", "--transition", "3"],
        ["scene", "FFFF", "2", "--transition", "3"],
    ):
        assert ap.parse_args(argv).transition == 0x1E
    assert ap.parse_args(["lightness", "0148", "1"]).transition is None
    for bad in (
        ["set", "0148", "on", "--t0", "--transition", "3"],
        ["lightness", "0148", "1", "--transition", "-3"],
        ["scene", "FFFF", "2", "--transition", "99999"],
    ):
        with pytest.raises(SystemExit):
            ap.parse_args(bad)
    with pytest.raises(SystemExit, match="--transition needs a lightness to set"):
        mesh_poc.main(["lightness", "0148", "--transition", "3"])
    with pytest.raises(SystemExit, match="delta 2147483648 is not"):
        mesh_poc.main(["delta", "0148", str(1 << 31)])

    client = _FadingClient(
        {
            M.GEN_ONOFF_STATUS: h("00011e"),  # off, fading on, 3 s left
            M.LIGHT_LIGHTNESS_STATUS: h("ffff" + "9919" + "1e"),
            M.LIGHT_CTL_STATUS: h("0010a00f" + "6400b80a" + "1e"),
            M.GEN_LEVEL_STATUS: h("0000" + "6766" + "1e"),
            M.SCENE_STATUS: h("00" + "0100" + "0200" + "1e"),
        }
    )
    _patch_client(monkeypatch, client, CDB.load(CDB_PATH))
    for argv in (
        ["set", "0148", "on", "--transition", "3"],
        ["set", "0148", "off", "--t0"],
        ["lightness", "0148", "6553", "--transition", "3"],
        ["ctl", "0148", "100", "2700", "--transition", "3"],
        ["delta", "0148", "-6553", "--transition", "3"],
        ["scene", "FFFF", "2", "--transition", "3"],
        ["scene", "0148", "2"],
    ):
        assert mesh_poc.main(argv) == 0
    sent = [pdu for _, pdu in client.requests]
    tids = [decode_opcode(pdu)[2] for pdu in sent]
    assert sent == [
        M.generic_onoff_set(True, tid=tids[0][1], transition=0x1E),
        M.generic_onoff_set(False, tid=tids[1][1], transition=0),
        M.light_lightness_set(6553, tid=tids[2][2], transition=0x1E),
        M.light_ctl_set(100, 2700, tid=tids[3][6], transition=0x1E),
        M.generic_delta_set(-6553, tid=tids[4][4], transition=0x1E),
        M.scene_recall(2, tid=tids[5][2], transition=0x1E),
        M.scene_recall(2, tid=tids[6][2]),
    ]
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith(": OFF target=ON remaining=3 s")
    assert "target=6553 remaining=30x100ms" in out[2]
    assert "Generic Level Status" in out[4]
    assert "Scene Status" in out[5]

    client.replies[M.GEN_ONOFF_STATUS] = h("01003f")  # fading off, no estimate
    assert mesh_poc.main(["set", "0148", "off", "--transition", "1"]) == 0
    assert capsys.readouterr().out.endswith(": ON target=OFF remaining=unknown\n")


# ----------------------------------------------------------------------------- config --dry-run (H P3)


def test_config_dry_run_prints_the_message_and_opens_no_link(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    async def no_link(*_args, **_kw):
        raise AssertionError("dry run must not connect")

    monkeypatch.setattr(mesh_poc, "with_client", no_link)
    cdb = ["--cdb", str(CDB_PATH)]
    assert (
        mesh_poc.main(
            [*cdb, "config", "subscribe", "0149", "0148", "1000", "C00F", "--dry-run"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.startswith("  dry run — would send to 0148 (Push-button 1-gang @0148")
    assert "Subscription Add" in out
    assert "C00F" in out
    for argv, expected in (
        (["publication", "0148", "0149", "1001", "C00F"], "Publication Set"),
        (["publication", "0148", "0149", "1001"], "Publication Get"),
        (["unsubscribe", "0148", "0148", "1000", "C00F"], "Subscription Delete"),
        (["subscriptions", "0148", "0148", "1000"], "Subscription Get"),
        (["bind", "0148", "0149", "05271015"], "App Bind"),
        (["unbind", "0148", "0149", "05271015"], "App Unbind"),
    ):
        assert mesh_poc.main([*cdb, "config", *argv, "--dry-run"]) == 0
        line = capsys.readouterr().out
        assert line.startswith("  dry run — would send to 0148 (")
        assert expected in line, line
    with pytest.raises(SystemExit):  # get-composition has no dry run: it is a read
        mesh_poc.build_parser().parse_args(
            ["config", "get-composition", "0148", "--dry-run"]
        )


# ----------------------------------------------------------------------------- live writes need --yes (review-3 Q9)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["config", "subscribe", "0149", "0148", "1000", "C00F"],
            "config subscribe changes the node's configuration now",
        ),
        (
            ["config", "unsubscribe", "0149", "0148", "1000", "WC"],
            "config unsubscribe changes",
        ),
        (["config", "bind", "0148", "0149", "05271015"], "config bind changes"),
        (["config", "unbind", "0148", "0149", "05271015"], "config unbind changes"),
        (
            ["config", "publication", "0148", "0149", "1001", "C00F"],
            "config publication changes",
        ),
        (
            ["prop", "set", "C00F", "key_mode", "light"],
            "prop set to group C00F 'WC' writes the property on every element subscribed to it",
        ),
        (["prop", "set", "WC", "key_mode", "light", "--unack"], "group C00F 'WC'"),
        (["prop", "set", "FFFF", "key_mode", "light"], "prop set to group FFFF writes"),
    ],
)
def test_a_live_write_without_yes_is_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], message: str
) -> None:
    """A Config write (the export is not updated) and a property Set to a group (every member takes it) are refused
    unless confirmed, with one line saying why, before any BLE connection."""

    async def must_not_connect(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", tmp_path / ".jhmesh_state.json")
    monkeypatch.setattr(mesh_poc, "connect", must_not_connect)
    with pytest.raises(SystemExit) as exc:
        mesh_poc.main(["--cdb", str(CDB_PATH), *argv])
    assert message in str(exc.value.code)
    assert "add --yes to send it" in str(exc.value.code)


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "subscribe", "0149", "0148", "1000", "C00F", "--yes"],
        ["config", "publication", "0148", "0149", "1001"],  # a Get
        ["config", "subscriptions", "0148", "0148", "1000"],
        ["config", "get-composition", "0148"],
        ["prop", "set", "0149", "key_mode", "light"],  # one element
        ["prop", "set", "C00F", "key_mode", "light", "--yes"],
        ["prop", "get", "C00F", "key_mode"],
        ["get", "C00F"],
    ],
)
def test_reads_unicast_sets_and_confirmed_writes_pass(argv: list[str]) -> None:
    """Only the unconfirmed writes stop: reads, a property Set to one element and anything with --yes go ahead."""
    args = mesh_poc.build_parser().parse_args(argv)
    mesh_poc.refuse_unconfirmed_write(args, CDB.load(CDB_PATH))


class _EchoConfigClient:
    """A node that takes every Config message: its Status is the request's parameters behind a Success byte (what
    Model App Status and an empty Model Subscription List look like)."""

    def __init__(self) -> None:
        self.config: list[tuple[int, bytes]] = []

    async def request_config(self, node, pdu, expect, **_kw):
        self.config.append((node, pdu))
        access = encode_opcode(expect) + b"\x00" + pdu[2:]
        return AccessMessage(
            node, 0x0D01, 3, 0, expect, None, access[2:], access, "dev"
        )


def test_a_confirmed_config_write_points_at_config_audit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After a Config write the export no longer describes the node: the output says so and how to compare them;
    a read says nothing of the kind."""
    cdb = CDB.load(CDB_PATH)
    client = _EchoConfigClient()
    _patch_client(monkeypatch, client, cdb)
    assert mesh_poc.main(["config", "bind", "0149", "0149", "05271015", "--yes"]) == 0
    assert client.config == [(0x0148, C.model_app_bind(0x0149, "05271015"))]
    out = capsys.readouterr().out
    assert out.endswith(
        "  export not updated — run `config audit 0148` to compare it with the node\n"
    )
    assert mesh_poc.main(["config", "subscriptions", "0148", "0148", "1000"]) == 0
    assert "export not updated" not in capsys.readouterr().out


# ----------------------------------------------------------------------------- with_client: the source address (P1-3)


def test_with_client_refuses_the_ha_address_and_a_state_file_in_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    async def no_proxies(_client, _seconds):
        return []

    monkeypatch.setattr(mesh_poc, "scan_for_proxies", no_proxies)
    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", tmp_path / "legacy.json")
    cdb = ["--cdb", str(CDB_PATH)]
    with pytest.raises(
        SystemExit, match="0D00 is the Home Assistant integration's own address"
    ):
        mesh_poc.main([*cdb, "--source", "0D00", "scan"])
    assert not list(
        tmp_path.glob(".jhmesh_state*")
    )  # refused before any state was touched
    with pytest.raises(SystemExit, match="0148 collides with a node in the CDB"):
        mesh_poc.main([*cdb, "--source", "0148", "scan"])
    assert mesh_poc.main([*cdb, "--source", "0D77", "scan"]) == 0
    state = tmp_path / ".jhmesh_state_0D77.json"
    assert json.loads(state.read_text())["src"] == "0D77"
    holder = LocalState(state, 0x0D77)  # what another running mesh_poc.py looks like
    with pytest.raises(
        SystemExit, match=r"in use by another process \(pid \d+\).*\n.*running as 0D77"
    ):
        mesh_poc.main([*cdb, "--source", "0D77", "scan"])
    holder.close()
    assert (
        mesh_poc.main([*cdb, "--source", "0D77", "scan"]) == 0
    )  # released: continues (with the margin)
    assert json.loads(state.read_text())["seq"] >= 512


def test_with_client_refuses_a_source_that_is_not_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An excluded address, or one inside any provisioner's allocated range, is refused like a node's (review-3 W3).

    A second app user's provisioner gets the block above the first phone's, which holds 0D01 (the old default);
    the default is the top of the unicast space now, which the app hands out last.
    """

    async def no_proxies(_client, _seconds):
        return []

    monkeypatch.setattr(mesh_poc, "scan_for_proxies", no_proxies)
    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", tmp_path / "legacy.json")
    raw = json.loads(CDB_PATH.read_text())
    net = raw["meshNetwork"]
    net["provisioners"].append(
        {
            "provisionerName": "Second phone",
            "UUID": "00000000-0000-4000-8000-000000000002",
            "allocatedUnicastRange": [{"lowAddress": "0CCD", "highAddress": "1999"}],
            "allocatedGroupRange": [],
            "allocatedSceneRange": [],
        }
    )
    net["networkExclusions"].append({"ivIndex": 1, "addresses": ["7FFE"]})
    export = tmp_path / "MeshNetwork.json"
    export.write_text(json.dumps(raw))
    cdb = ["--cdb", str(export)]
    with pytest.raises(
        SystemExit,
        match=r"our address 0D01 is inside a provisioner's allocated range 0CCD-1999 .*: pass --source 7FFF",
    ):
        mesh_poc.main([*cdb, "--source", "0D01", "scan"])
    with pytest.raises(
        SystemExit, match=r"our address 0002 is in the export's networkExclusions"
    ):
        mesh_poc.main(
            [*cdb, "--source", "0002", "scan"]
        )  # inside the phone's range too
    assert not list(
        tmp_path.glob(".jhmesh_state*")
    )  # refused before any state was touched
    assert mesh_poc.main([*cdb, "scan"]) == 0  # the default
    assert (tmp_path / ".jhmesh_state_7FFF.json").exists()
    # the default taken as well: the suggestion comes from the top down, past the exclusion
    net["provisioners"][1]["allocatedUnicastRange"].append(
        {"lowAddress": "7FFF", "highAddress": "7FFF"}
    )
    export.write_text(json.dumps(raw))
    with pytest.raises(SystemExit, match=r"range 7FFF-7FFF .*: pass --source 7FFD"):
        mesh_poc.main([*cdb, "scan"])
    # nothing free but Home Assistant's own address: never suggested
    monkeypatch.setattr(CDB, "unicast_is_free", lambda _self, a, *_: a == 0x0D00)
    with pytest.raises(SystemExit, match=r"range 7FFF-7FFF .*: no address is free"):
        mesh_poc.main([*cdb, "scan"])


def ha_store(storage: Path, suffix: str, addresses: dict[str, Any]) -> None:
    """One of Home Assistant's sequence-number files of the fixture's mesh, as its `Store` writes it."""
    mesh = CDB.load(CDB_PATH).mesh_uuid.lower()
    (storage / f"junghome_ble.seq.{mesh}{suffix}").write_text(
        json.dumps({"version": 1, "minor_version": 4, "data": {"addresses": addresses}})
    )


def test_with_client_refuses_every_address_home_assistants_store_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Review-4 S4-11: only 0D00 was refused, while Home Assistant may be configured with any address. With
    `--ha-storage` every address its store of this mesh (the store, its `.backup`, the floor) holds a counter for is
    refused before any state file is touched, and never suggested instead of a taken one."""

    async def no_proxies(_client, _seconds):
        return []

    monkeypatch.setattr(mesh_poc, "scan_for_proxies", no_proxies)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(mesh_poc, "STATE_DIR", state_dir)
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", state_dir / "legacy.json")
    storage = tmp_path / ".storage"
    storage.mkdir()
    cdb = ["--cdb", str(CDB_PATH), "--ha-storage", str(storage)]
    assert mesh_poc.main([*cdb, "--source", "0D44", "scan"]) == 0  # no store yet
    ha_store(storage, "", {"0D44": {"seq": 7}})
    ha_store(storage, ".backup", {"0D45": {"seq": 7}})
    ha_store(storage, ".floor", {"0D46": {"iv_index": 0, "seq": 7}})
    for source in ("0D44", "0D45", "0D46"):
        with pytest.raises(
            SystemExit,
            match=f"{source} is an address Home Assistant keeps a sequence counter for",
        ):
            mesh_poc.main([*cdb, "--source", source, "scan"])
    # only 0D44's files, from before the store knew it
    assert {p.name for p in state_dir.glob(".jhmesh_state*")} == {
        f".jhmesh_state_0D44.{suffix}" for suffix in ("json", "bak", "lock")
    }
    assert mesh_poc.main([*cdb, "--source", "0D47", "scan"]) == 0
    # the address suggested instead of a taken one is none of Home Assistant's either
    monkeypatch.setattr(
        CDB, "unicast_is_free", lambda _self, a, *_: a in (0x0D45, 0x0D47)
    )
    with pytest.raises(SystemExit, match=r"range 0001-0CCC .*: pass --source 0D47"):
        mesh_poc.main([*cdb, "--source", "0C00", "scan"])  # inside the phone's range
    # a store that does not read: refused, an address it holds would go unnoticed
    (storage / f"junghome_ble.seq.{CDB.load(CDB_PATH).mesh_uuid.lower()}").write_text(
        "{"
    )
    with pytest.raises(SystemExit, match="cannot read Home Assistant's sequence store"):
        mesh_poc.main([*cdb, "--source", "0D47", "scan"])
    with pytest.raises(SystemExit, match="is not a directory"):
        mesh_poc.main(
            ["--cdb", str(CDB_PATH), "--ha-storage", str(tmp_path / "none"), "scan"]
        )


def test_cmd_provision_refuses_an_address_home_assistants_store_holds(
    radio: SimpleNamespace, tmp_path: Path
):
    """`provision --unicast` at an address Home Assistant sends from (its store names it) is refused too."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    ha_store(storage, "", {"0D10": {"seq": 7}})
    with pytest.raises(SystemExit, match="0D10 cannot go to the new node"):
        mesh_poc.main(["--ha-storage", str(storage), *provision_argv("--yes")])
    assert not radio.runs


# ----------------------------------------------------------------------------- hops: everything restored (P2-24)


class _HopClientThatLosesTheRestore(_HopClient):
    """The origin stops answering once the beats are over: restoring its publication times out."""

    def __init__(self, unsubscribe_too: bool = False) -> None:
        super().__init__()
        self.unsubscribe_too = unsubscribe_too

    async def request_config(self, node, pdu, expect, **kw):
        if pdu == C.heartbeat_publication_set(0x0D02, 7, ttl=5):
            self.sent.append((node, pdu))
            raise TimeoutError("no response from 0172")
        if self.unsubscribe_too and pdu == C.heartbeat_subscription_off():
            self.sent.append((node, pdu))
            raise ConnectionError("proxy link lost")
        return await super().request_config(node, pdu, expect, **kw)


def test_cmd_config_hops_restores_the_publication_when_the_wait_is_interrupted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A lost link (or Ctrl-C) during the wait used to leave the origin beating to all-nodes with HA's
    publication gone; the restore and the unsubscribe now run in a `finally`."""
    client = _HopClient()
    _patch_client(monkeypatch, client, CDB.load(CDB_PATH))

    async def sleep(seconds: float) -> None:
        if (
            seconds == 10.0
        ):  # the beat wait (asyncio itself sleeps too: leave those alone)
            raise ConnectionError("proxy link lost")

    monkeypatch.setattr(mesh_poc.asyncio, "sleep", sleep)
    with pytest.raises(ConnectionError, match="proxy link lost"):
        mesh_poc.main(["config", "hops", "0149", "0172"])
    pdus = [p for _, p in client.sent]
    assert pdus == [
        C.heartbeat_publication_get(),
        C.heartbeat_subscription_set(0x0172, 0xFFFF, 5),
        C.heartbeat_publication_set(0xFFFF, 2, count_log=3, ttl=0x7F),
        C.heartbeat_publication_set(
            0x0D02, 7, ttl=5
        ),  # restored although the wait blew up
        C.heartbeat_subscription_off(),
    ]
    assert "!!" not in capsys.readouterr().err


def test_cmd_config_hops_reports_what_it_could_not_restore(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    async def sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(mesh_poc.asyncio, "sleep", sleep)
    client = _HopClientThatLosesTheRestore()
    _patch_client(monkeypatch, client, CDB.load(CDB_PATH))
    assert mesh_poc.main(["config", "hops", "0149", "0172"]) == 0
    err = capsys.readouterr().err
    assert (
        "0172 (Socket @0172 el loc 0001): Heartbeat Publication NOT restored (no response from 0172)"
        in err
    )
    assert "re-run `config hops 0148 0172`" in err
    assert [p for _, p in client.sent][
        -1
    ] == C.heartbeat_subscription_off()  # still switched off
    client = _HopClientThatLosesTheRestore(unsubscribe_too=True)
    _patch_client(monkeypatch, client, CDB.load(CDB_PATH))
    assert mesh_poc.main(["config", "hops", "0149", "0172"]) == 0
    err = capsys.readouterr().err
    assert (
        "Heartbeat Subscription not switched off (proxy link lost); it expires by itself after 10 s"
        in err
    )


def _hop_exchange(sent: list[bytes], *, restore: str = "ok"):
    """A `_hop_round` exchange: answers the publication Get and every subscription Get; `restore` = ok | silent | lost."""
    publication = C.HeartbeatPublicationStatus(0, 0x0D02, 0xFF, 7, 5, 0, 0)
    restore_pdu = (
        ops.HopProbe(0x0148, 0x0172, 4, 2).restore_publication(publication).pdu
    )

    async def exchange(req: ops.ConfigRequest):
        sent.append(req.pdu)
        if req.pdu == restore_pdu:
            if restore == "silent":
                return None
            if restore == "lost":
                raise ConnectionError("proxy link lost")
        if req.expect_opcode == C.CONFIG_HEARTBEAT_PUBLICATION_STATUS:
            body = h("00 020d ff 07 05 0000 0000")
        else:
            body = h("00 7201 ffff 03 03 01 02")
        access = encode_opcode(req.expect_opcode) + body
        op, _cid, params = decode_opcode(access)
        return AccessMessage(
            req.node, 0x0D01, 3, 0, op, None, params, access, f"dev:{req.node:04X}"
        )

    return exchange


async def test_hop_round_restores_the_origin_when_interrupted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    probes = [ops.HopProbe(counter, 0x0172, 4, 2) for counter in (0x0148, 0x0232)]
    sent: list[bytes] = []

    async def no_wait(seconds: float) -> None:
        if seconds != 10.0:  # only the beat wait is skipped
            await _real_sleep(seconds)

    _real_sleep = asyncio.sleep
    monkeypatch.setattr(mesh_poc.asyncio, "sleep", no_wait)
    assert await mesh_poc._hop_round(_hop_exchange(sent), []) == {}
    cells = await mesh_poc._hop_round(_hop_exchange(sent), probes)
    assert cells == {
        (0x0172, 0x0148): (4, 1, 2),
        (0x0172, 0x0232): (4, 1, 2),
    }  # (beats counted, min, max hops)
    assert sent[-3:] == [
        C.heartbeat_publication_set(0x0D02, 7, ttl=5),
        C.heartbeat_subscription_off(),
        C.heartbeat_subscription_off(),
    ]

    async def cancelled(seconds: float) -> None:
        if seconds == 10.0:  # the beat wait
            raise asyncio.CancelledError  # what Ctrl-C delivers into the main task

    monkeypatch.setattr(mesh_poc.asyncio, "sleep", cancelled)
    sent.clear()
    with pytest.raises(asyncio.CancelledError):
        await mesh_poc._hop_round(_hop_exchange(sent), probes)
    assert sent[-3:] == [
        C.heartbeat_publication_set(0x0D02, 7, ttl=5),
        C.heartbeat_subscription_off(),
        C.heartbeat_subscription_off(),
    ]
    assert C.heartbeat_subscription_get() not in sent  # interrupted before the read
    assert "!!" not in capsys.readouterr().err


async def test_hop_round_reports_an_origin_it_could_not_restore(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    async def sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(mesh_poc.asyncio, "sleep", sleep)
    probes = [ops.HopProbe(0x0148, 0x0172, 4, 2)]
    sent: list[bytes] = []
    cells = await mesh_poc._hop_round(_hop_exchange(sent, restore="silent"), probes)
    assert cells == {(0x0172, 0x0148): (4, 1, 2)}
    assert sent[-1] == C.heartbeat_subscription_off()
    assert (
        "!! 0172: Heartbeat Publication NOT restored — re-run `config hops <node> 0172`"
        in capsys.readouterr().err
    )
    sent.clear()
    cells = await mesh_poc._hop_round(
        _hop_exchange(sent, restore="lost"), probes
    )  # the unsubscribe still answers here
    assert cells == {(0x0172, 0x0148): (4, 1, 2)}
    assert "NOT restored" in capsys.readouterr().err

    # a silent origin at the very start: nothing to restore, every cell unknown
    async def unreachable(req: ops.ConfigRequest) -> None:
        sent.append(req.pdu)

    sent.clear()
    assert await mesh_poc._hop_round(unreachable, probes) == {(0x0172, 0x0148): None}
    assert sent == [C.heartbeat_publication_get()]
    assert "unreachable, skipped" in capsys.readouterr().out


# ----------------------------------------------------------------------------- export write --out (H P3)


def test_export_write_refuses_to_overwrite_its_input(tmp_path: Path):
    copy = tmp_path / "JungHome.json"
    copy.write_text(CDB_PATH.read_text())
    same = tmp_path / "sub" / ".." / "JungHome.json"
    with pytest.raises(SystemExit, match="is the input file"):
        mesh_poc.main(["export", "write", str(copy), "--out", str(same)])
    assert copy.read_text() == CDB_PATH.read_text()


# ----------------------------------------------------------------------------- the other link-bound commands


class _EveryStatusClient(_StatusClient):
    """`_StatusClient` that also answers group requests (`collect`) and records `on_message` for `listen`."""

    def __init__(self, replies: dict[int, bytes]) -> None:
        super().__init__(replies)
        self.on_message = None
        self.state = SimpleNamespace(src=0x0D01)

    async def request(self, dst, pdu, expect_opcode, *, expect_cid=None, **_kw):
        self.requests.append((dst, pdu))
        return message(
            dst,
            0x0D01,
            encode_opcode(expect_opcode, expect_cid) + self.replies[expect_opcode],
        )

    async def collect(self, dst, pdu, expect_opcode, *, window):
        self.requests.append((dst, pdu))
        return [
            message(
                src, 0x0D01, encode_opcode(expect_opcode) + self.replies[expect_opcode]
            )
            for src in (0x0148, 0x0172)
        ]


def test_link_bound_commands_print_their_statuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    client = _EveryStatusClient(
        {
            M.GEN_ONOFF_STATUS: h("01"),
            M.SCENE_STATUS: h("000100"),
            M.LIGHT_CTL_TEMP_RANGE_STATUS: h("00b80b1027"),
            M.LIGHT_CTL_STATUS: h("00108c0a"),
            0x05: h("03500306"),  # LBC Admin Property Status prop 0x5003 = gateway
        }
    )
    _patch_client(monkeypatch, client, CDB.load(CDB_PATH))

    async def sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(mesh_poc.asyncio, "sleep", sleep)
    assert mesh_poc.main(["get", "0148"]) == 0
    assert mesh_poc.main(["get", "C00F"]) == 0
    assert mesh_poc.main(["blink", "0148", "--hold", "0"]) == 0
    assert mesh_poc.main(["scene", "0148", "1"]) == 0
    assert mesh_poc.main(["scene", "C00F", "1"]) == 0
    assert mesh_poc.main(["ctlrange", "0148"]) == 0
    assert mesh_poc.main(["ctlget", "0148", "--echo"]) == 0
    assert mesh_poc.main(["prop", "get", "0149", "key_mode", "--pad", "2"]) == 0
    assert mesh_poc.main(["prop", "set", "0149", "key_mode", "gateway"]) == 0
    assert (
        mesh_poc.main(
            ["--window", "0", "prop", "set", "0149", "key_mode", "light", "--unack"]
        )
        == 0
    )
    assert (
        mesh_poc.main(["listen", "--seconds", "0", "--src", "0148", "--dst", "C00F"])
        == 0
    )
    assert mesh_poc.main(["listen", "--seconds", "0"]) == 0
    out = capsys.readouterr().out
    assert (
        out.count(": ON") == 5
    )  # get, the two group answers, blink's "after set" and "restored"
    assert "toggling for 0.0s" in out
    assert "Scene Status" in out
    assert "Light CTL Temperature Range Status" in out
    assert "echoing CTL Set l=4096 t=2700 as a segmented message" in out
    assert "key_mode=gateway" in out
    assert "(unacknowledged)" in out
    assert [pdu for _, pdu in client.sent] == [
        M.vendor_property_set("admin", 0x5003, h("00"), ack=False, user_access=3)
    ]
    assert client.requests[-3][1].endswith(
        bytes(2)
    )  # --pad: two zero bytes after the Get
    assert client.on_message is not None  # listen installed its printer
    client.on_message(message(0x0148, 0xC00F, h("820401")))
    client.on_message(
        message(0x0172, 0xC00F, h("820401"))
    )  # filtered out by --src (the last listen had no filter, so both print)


# ----------------------------------------------------------------------------- tools coverage (review 1)


def test_hop_cell_and_matrix_renderings() -> None:
    """The hop-matrix pieces `config hopmatrix` prints and writes: a cell per status, the table, the JSON."""
    assert ops.hop_cell(
        C.HeartbeatSubscriptionStatus(0, 0x0172, 0xFFFF, 3, 0, 0x7F, 0)
    ) == (
        (0, None, None),
        "-",
    )
    assert ops.hop_cell(
        C.HeartbeatSubscriptionStatus(0, 0x0172, 0xFFFF, 3, 3, 1, 2)
    ) == (
        (4, 1, 2),
        "1..2",
    )
    assert (
        ops.hop_cell(C.HeartbeatSubscriptionStatus(0, 0x0172, 0xFFFF, 3, 1, 2, 2))[1]
        == "2"
    )
    cdb = CDB.load(CDB_PATH)
    nodes = [0x0148, 0x0172, 0x0232]
    results: dict[tuple[int, int], Any] = {
        (0x0148, 0x0172): (4, 1, 2),
        (0x0148, 0x0232): (0, None, None),
        (0x0172, 0x0148): None,
    }
    lines = ops.hop_matrix_text(cdb, nodes, results).splitlines()
    assert lines[:4] == [
        "      0148 0172 0232",
        "0148     ·    1    -",
        "0172     ?    ·    ?",
        "0232     ?    ?    ·",
    ]
    assert lines[4] == ""
    assert lines[5] == f"0148  {cdb.label(0x0148)}"
    doc = json.loads(ops.hop_matrix_json(cdb, nodes, results, 4, 2))
    assert doc["nodes"] == ["0148", "0172", "0232"]
    assert (doc["beats"], doc["period"]) == (4, 2)
    assert doc["pairs"][0] == {
        "origin": "0148",
        "counter": "0172",
        "counted": 4,
        "min_hops": 1,
        "max_hops": 2,
    }
    assert doc["pairs"][2]["counted"] is None  # the counter did not answer


def test_state_path_adopts_the_legacy_file_of_the_same_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old single-file layout is taken over by its own address only; garbage is left alone."""
    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    legacy = tmp_path / ".jhmesh_state.json"
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", legacy)
    legacy.write_text(json.dumps({"src": "0D00", "seq": 5}))
    assert mesh_poc.state_path(0x0D01) == tmp_path / ".jhmesh_state_0D01.json"
    assert legacy.exists()  # another address's counter: not taken
    path = mesh_poc.state_path(0x0D00)
    assert path == tmp_path / ".jhmesh_state_0D00.json"
    assert json.loads(path.read_text())["seq"] == 5
    assert not legacy.exists()
    legacy.write_text("not json")
    mesh_poc.state_path(0x0D02)
    assert legacy.exists()


def test_cmd_devices_lists_the_device_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`devices` needs no link: the lights, sockets, keys and scenes of the export."""
    monkeypatch.setattr(mesh_poc, "APP_DIR", tmp_path)  # no app-container names
    assert mesh_poc.main(["--cdb", str(CDB_PATH), "devices"]) == 0
    out = capsys.readouterr().out
    assert "light  0148 switch" in out
    assert "socket 0172 sensor=" in out
    assert "button 0149 loc=40" in out
    assert "scene  1 " in out


class _MatrixClient(_HopClient):
    """`_HopClient`, with one node that never answers and one counter that never reports its subscription."""

    async def request_config(self, node, pdu, expect, **kw):
        if node == 0x0400 or (
            node == 0x0300 and expect == C.CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS
        ):
            raise TimeoutError(f"no response from {node:04X}")
        return await super().request_config(node, pdu, expect, **kw)


def test_cmd_config_hopmatrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every node beats in turn and the others count; a silent node is skipped, a silent counter is `?`."""
    cdb = CDB.load(CDB_PATH)
    _patch_client(monkeypatch, _MatrixClient(), cdb)

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mesh_poc.asyncio, "sleep", sleep)
    out_json = tmp_path / "matrix.json"
    argv = ["config", "hopmatrix", "0148", "0300", "0400", "--json", str(out_json)]
    assert mesh_poc.main(argv) == 0
    out = capsys.readouterr().out
    assert "[1/3] 0148" in out
    assert "no answer to" in out
    assert "unreachable, skipped" in out  # 0400 never answers its publication Get
    assert "0300=?" in out  # 0300 never reports what it counted
    assert f"written to {out_json}" in out
    doc = json.loads(out_json.read_text())
    assert doc["nodes"] == ["0148", "0300", "0400"]


async def test_with_client_connects_runs_and_detaches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A one-shot command: connect (scan), run, detach — whatever the command does."""
    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", tmp_path / ".jhmesh_state.json")
    connected: list[float] = []

    async def connect(client: Any, scan_seconds: float) -> None:
        connected.append(scan_seconds)

    monkeypatch.setattr(mesh_poc, "connect", connect)
    ran: list[int] = []

    async def fn(client: Any, cdb: CDB) -> None:
        ran.append(len(cdb.nodes))

    args = mesh_poc.build_parser().parse_args(["--cdb", str(CDB_PATH), "get", "0148"])
    await mesh_poc.with_client(args, fn)
    assert connected == [args.scan]
    assert ran == [len(CDB.load(CDB_PATH).nodes)]


async def test_with_client_listens_through_a_reconnecting_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`listen` keeps a StandaloneLink (it reconnects when the proxy drops) and stops it afterwards."""
    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", tmp_path / ".jhmesh_state.json")
    events: list[str] = []

    class Link:
        def __init__(self, client: Any, scan: float) -> None:
            events.append("made")

        async def start(self) -> None:
            events.append("start")

        async def wait_connected(self) -> None:
            events.append("connected")

        async def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr(mesh_poc, "StandaloneLink", Link)

    async def fn(client: Any, cdb: CDB) -> None:
        events.append("run")

    args = mesh_poc.build_parser().parse_args(["--cdb", str(CDB_PATH), "listen"])
    await mesh_poc.with_client(args, fn)
    assert events == ["made", "start", "connected", "run", "stop"]


class _SilentClient:
    """Nothing answers; a group Set is collected (no replies); Config requests answer with a fixed Status."""

    def __init__(self) -> None:
        self.config: list[tuple[int, bytes]] = []
        self.collected: list[int] = []

    async def request(self, *_a: Any, **_kw: Any) -> Any:
        raise TimeoutError("no response")

    async def collect(self, dst: int, *_a: Any, **_kw: Any) -> list[Any]:
        self.collected.append(dst)
        return []

    async def request_config(self, node, pdu, expect, **_kw):
        self.config.append((node, pdu))
        # a page-0 Composition Data Status with no elements (also a Success status for the others)
        access = encode_opcode(expect) + bytes(11)
        op, _cid, params = decode_opcode(access)
        return AccessMessage(node, 0x0D01, 3, 0, op, None, params, access, "dev")


def test_group_set_and_silent_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Set to a group collects the answers; a property survey of a silent sensor element says so per server."""
    cdb = CDB.load(CDB_PATH)
    client = _SilentClient()
    _patch_client(monkeypatch, client, cdb)
    assert mesh_poc.main(["--window", "0", "set", "C00F", "on"]) == 0
    assert client.collected == [0xC00F]
    sensor = next(
        e.address for n in cdb.nodes for e in n.elements if "1100" in e.models
    )
    assert mesh_poc.main(["prop", "lists", f"{sensor:04X}"]) == 0
    out = capsys.readouterr().out
    assert "no answer" in out
    assert "sensor           no descriptor answer" in out


def test_config_command_goes_through_the_exchange(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --dry-run a Config command is sent (here to a fake node); get-composition is one too."""
    cdb = CDB.load(CDB_PATH)
    client = _SilentClient()
    _patch_client(monkeypatch, client, cdb)
    assert mesh_poc.main(["config", "get-composition", "0148"]) == 0
    assert client.config[0][0] == 0x0148
    capsys.readouterr()


# ----------------------------------------------------------------------------- re-review of Phases 3-5


@pytest.mark.parametrize(
    ("argv", "ok"),
    [
        (["scene", "0148", "0"], False),  # scene 0 is prohibited
        (["scene", "0148", "70000"], False),
        (["scene", "0148", "-3"], False),
        (["scene", "0148", "65535"], True),
        (["scene-actions", "0148", "0"], True),  # 0 asks for the list
        (["scene-actions", "0148", "70000"], False),
    ],
)
def test_scene_numbers_are_range_checked_by_argparse(argv: list[str], ok: bool) -> None:
    """A scene number the builders would refuse is an argparse error, before anything connects."""
    parser = mesh_poc.build_parser()
    if ok:
        parser.parse_args(argv)
    else:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


@pytest.mark.parametrize("target", ["1C00F", "-1", "0", ""])
def test_an_address_out_of_range_is_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    """A target that parses as hex but is no 16-bit address (or is the unassigned 0000, or empty) is refused too."""

    async def must_not_connect(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr(mesh_poc, "connect", must_not_connect)
    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mesh_poc, "LEGACY_STATE", tmp_path / ".jhmesh_state.json")
    with pytest.raises(SystemExit) as exc:
        mesh_poc.main(["--cdb", str(CDB_PATH), "get", target])
    assert "is neither a group name nor a hex address" in str(exc.value.code)


def test_dry_run_with_a_bad_group_is_a_clean_error() -> None:
    """The dry run resolves the group the same way, without a traceback."""
    argv = ["--cdb", str(CDB_PATH), "config", "subscribe", "0149", "0148", "1000"]
    with pytest.raises(SystemExit) as exc:
        mesh_poc.main([*argv, "not-a-group", "--dry-run"])
    assert "is neither a group name nor a hex address" in str(exc.value.code)


# ----------------------------------------------------------------------------- provision

NEW_UUID = "30FB10FF-FE12-3456-0000-000000000000"
DEVICE_KEY = bytes(range(0x30, 0x40))  # what the fake provisioning run hands back


def test_provision_arguments():
    ap = mesh_poc.build_parser()
    args = ap.parse_args(["provision", "--scan"])
    assert (args.cmd, args.list_devices, args.uuid, args.seconds) == (
        "provision",
        True,
        None,
        10.0,
    )
    assert args.scan == 4.0  # the global proxy-scan option is a different one
    args = ap.parse_args(
        [
            "provision",
            "30fb10fffe1234560000000000000000",
            "--unicast",
            "0x0d10",
            "--iv-index",
            "0x10",
            "--iv-update",
            "--yes",
        ]
    )
    assert (args.uuid, args.unicast, args.iv_index, args.iv_update, args.yes) == (
        NEW_UUID,
        0x0D10,
        16,
        True,
        True,
    )
    assert ops.parse_device_uuid(NEW_UUID.lower()) == NEW_UUID
    for bad in ("30FB10FF", "zz" * 16, NEW_UUID + "00"):
        with pytest.raises(argparse.ArgumentTypeError):
            ops.parse_device_uuid(bad)
    assert ops.parse_iv_index("42") == 42
    for bad in ("-1", "0x100000000", "many"):
        with pytest.raises(argparse.ArgumentTypeError):
            ops.parse_iv_index(bad)


def test_provision_address_problem():
    cdb = CDB.load(CDB_PATH)
    assert ops.provision_address_problem(cdb, 0x0D10, 3, {0x0D00}) is None
    problem = ops.provision_address_problem(cdb, 0x0CFF, 3, {0x0D00, 0x0D01})
    assert problem is not None
    assert problem.startswith("0D00, 0D01 cannot go to the new node")
    # inside the phone's allocated range, and a node's own address
    assert "0148" in (ops.provision_address_problem(cdb, 0x0148, 1, set()) or "")
    assert "0100" in (ops.provision_address_problem(cdb, 0x0100, 1, set()) or "")
    assert "past the unicast range" in (
        ops.provision_address_problem(cdb, 0x7FFF, 2, set()) or ""
    )


def test_unprovisioned_rows_and_provisioned_text():
    assert ops.unprovisioned_rows([]) == [
        "no device advertises the Mesh Provisioning Service (0x1827)"
    ]
    rows = ops.unprovisioned_rows(
        [
            UnprovisionedDevice(NEW_UUID, 0x4802, "AA:BB", -40, "JUNG", 0x0001),
            UnprovisionedDevice(NEW_UUID, 0, "CC:DD", -80),
        ]
    )
    assert rows == [
        f"  -40 dBm  AA:BB  {NEW_UUID}  oob 4802 (URI, on box, inside manual)  product 0001  JUNG",
        f"  -80 dBm  CC:DD  {NEW_UUID}  oob 0000",
    ]
    result = ProvisioningResult(
        0x0D10, 3, bytes(range(16)), Capabilities(3, 1, 0, 0, 0, 0, 0, 0)
    )
    text = ops.provisioned_text(result, Path("/keys/devkey.json"))
    assert text.splitlines()[:2] == [
        "provisioned: unicast 0D10..0D12 (3 element(s))",
        "device key: written to /keys/devkey.json (owner-only)",
    ]
    assert "000102030405" not in text
    assert json.loads(ops.device_key_record(NEW_UUID, result)) == {
        "uuid": NEW_UUID,
        "unicast": "0D10",
        "elements": 3,
        "deviceKey": "000102030405060708090A0B0C0D0E0F",
    }


@pytest.fixture
def radio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Replace the scan and the provisioning run of `mesh_poc` with recorders (no Bluetooth).

    The device-key files go to `tmp_path`, never into the repository's `tools/`.
    """
    monkeypatch.setattr(mesh_poc, "STATE_DIR", tmp_path)
    state = SimpleNamespace(
        devices=[UnprovisionedDevice(NEW_UUID, 0, "AA:BB", -50)],
        scans=[],
        runs=[],
        elements=2,
        error=None,
    )

    async def scan(seconds: float) -> list[UnprovisionedDevice]:
        state.scans.append(seconds)
        return state.devices

    async def run(
        device: UnprovisionedDevice, data: ProvisioningData, *, check: Any
    ) -> ProvisioningResult:
        state.runs.append((device, data))
        caps = Capabilities(state.elements, 1, 0, 0, 0, 0, 0, 0)
        check(caps)
        if state.error is not None:
            raise state.error
        return ProvisioningResult(data.unicast, caps.elements, DEVICE_KEY, caps)

    monkeypatch.setattr(mesh_poc, "scan_unprovisioned", scan)
    monkeypatch.setattr(mesh_poc, "provision_device", run)
    return state


def provision_argv(*extra: str) -> list[str]:
    return ["--cdb", str(CDB_PATH), "provision", NEW_UUID, "--unicast", "0D10", *extra]


def test_cmd_provision_scan(radio: SimpleNamespace, capsys: pytest.CaptureFixture[str]):
    assert mesh_poc.main(["provision", "--scan", "--seconds", "3"]) == 0
    assert radio.scans == [3.0]
    assert capsys.readouterr().out == f"  -50 dBm  AA:BB  {NEW_UUID}  oob 0000\n"


def test_cmd_provision_writes_the_device_key_to_a_private_file(
    radio: SimpleNamespace, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    """P I-10: the device key went to stdout (scrollback, `tee` logs, pasted transcripts). It goes to an
    owner-only file whatever the umask, and only the file's path is printed."""
    old_umask = os.umask(0o022)
    try:
        assert mesh_poc.main(provision_argv("--yes")) == 0
    finally:
        os.umask(old_umask)
    out = capsys.readouterr().out
    key_file = tmp_path / ".jhmesh_devkey_0D10.json"
    assert out.splitlines()[0].startswith("IV index 0 (the export's lower bound")
    assert "provisioned: unicast 0D10..0D11 (2 element(s))" in out
    assert f"device key: written to {key_file} (owner-only)" in out
    assert DEVICE_KEY.hex() not in out.lower()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert json.loads(key_file.read_text())["deviceKey"] == DEVICE_KEY.hex().upper()
    ((device, data),) = radio.runs
    cdb = CDB.load(CDB_PATH)
    assert device is radio.devices[0]
    assert data == ProvisioningData(cdb.net_keys[0].key, 0x0D10, iv_index=0)
    # the key of the first device is never overwritten: refused before any radio traffic
    with pytest.raises(SystemExit, match="already exists"):
        mesh_poc.main(provision_argv("--yes"))
    assert len(radio.runs) == 1
    with pytest.raises(SystemExit, match="directory is missing or not writable"):
        mesh_poc.main(
            provision_argv("--yes", "--key-file", str(tmp_path / "no" / "k.json"))
        )
    assert len(radio.runs) == 1
    other = tmp_path / "second.json"
    argv = provision_argv(
        "--yes", "--iv-index", "5", "--iv-update", "--key-file", str(other)
    )
    assert mesh_poc.main(argv) == 0
    assert radio.runs[-1][1].iv_index == 5
    assert radio.runs[-1][1].iv_update
    out = capsys.readouterr().out
    assert "lower bound" not in out
    assert str(other) in out
    assert stat.S_IMODE(other.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--cdb", str(CDB_PATH), "provision"], "needs a device UUID and --unicast"),
        (["--cdb", str(CDB_PATH), "provision", NEW_UUID], "needs a device UUID"),
        (provision_argv(), "pass --yes"),
        (
            [
                "--cdb",
                str(CDB_PATH),
                "provision",
                "00005EFF-FE00-5314-0000-000000000000",
                "--unicast",
                "0D10",
                "--yes",
            ],
            "is node 0148 (Push-button 1-gang) in the export",
        ),
        (
            [
                "--cdb",
                str(CDB_PATH),
                "provision",
                NEW_UUID,
                "--unicast",
                "7FFF",
                "--yes",
            ],
            "7FFF cannot go to the new node",  # the CLI's own address (DEFAULT_SOURCE)
        ),
    ],
)
def test_cmd_provision_refuses_before_any_radio_traffic(
    radio: SimpleNamespace, argv: list[str], message: str
):
    with pytest.raises(SystemExit) as exc:
        mesh_poc.main(argv)
    assert message in str(exc.value.code)
    assert radio.scans == []


def test_cmd_provision_failures(radio: SimpleNamespace):
    radio.devices = []
    with pytest.raises(SystemExit, match="was not seen advertising"):
        mesh_poc.main(provision_argv("--yes"))
    radio.devices = [UnprovisionedDevice(NEW_UUID, 0, "AA:BB", -50)]
    radio.elements = 0x7FFF  # the capabilities do not fit at the address
    with pytest.raises(
        SystemExit, match=r"provisioning failed: .*past the unicast range"
    ):
        mesh_poc.main(provision_argv("--yes"))
    radio.elements = 2
    radio.error = ProvisioningError(
        "device reported Provisioning Failed: Out of Resources"
    )
    with pytest.raises(SystemExit, match="provisioning failed: device reported"):
        mesh_poc.main(provision_argv("--yes"))
