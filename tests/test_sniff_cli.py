"""tools/mesh_sniff.py: the key-free capture half (against a fake SnifferAPI) and the decode half."""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jhmesh.sniffer import MeshDecoder, SniffRecord
from tests.helpers import (
    LIGHT_SWITCH,
    MAC_LIGHT_SWITCH,
    SOCKET,
    onoff_status,
    vendor_button_event,
)
from tests.jhmesh.test_sniffer import GROUP, Air, nordic_packet, pcap
from tools import mesh_sniff

# the six octets the sniffer API takes (`follow_device` parses the MAC in print order) plus the address type
MAC_LIGHT_SWITCH_BYTES = [*bytes.fromhex(MAC_LIGHT_SWITCH.replace(":", "")), 0]
CDB_FIXTURE = Path(__file__).parent / "fixtures" / "MeshNetwork.json"

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jhmesh.cdb import CDB

h = bytes.fromhex


# ----------------------------------------------------------------------------- capture side


def fake_packet(
    adv_data: bytes = h("032a1122"),
    ok: bool = True,
    ble_type: int = 1,
    adv_type: int = 2,
    payload: bytes | None = None,
    channel: int = 37,
    rssi: int = -50,
    time: float = 1000.5,
    timestamp: int = 42,
) -> Any:
    adv_addr = [
        0x2F,
        0xAC,
        0xF8,
        0x13,
        0xBF,
        0x42,
        1,
    ]  # SnifferAPI keeps MSB first plus the address type
    if payload is None:
        payload = h("42bf13f8ac2f") + adv_data
    ble = SimpleNamespace(
        type=ble_type, advType=adv_type, advAddress=adv_addr, payload=list(payload)
    )
    return SimpleNamespace(
        OK=ok,
        blePacket=ble,
        channel=channel,
        RSSI=rssi,
        time=time,
        timestamp=timestamp,
        boardId=0,
        getList=lambda: [
            0x0A,
            0x00,
            0x03,
            0x01,
            0x00,
            0x02,
            *([10, 1, channel, -rssi] + [0] * 6),
            *payload,
        ],
    )


def test_capture_records_extracts_mesh_ad_structures():
    recs = mesh_sniff.capture_records(
        fake_packet(h("020106") + h("032a1122") + h("042b010203"))
    )
    assert [(r["kind"], r["pdu"]) for r in recs] == [
        ("msg", "1122"),
        ("beacon", "010203"),
    ]
    assert recs[0] == {
        "t": 1000.5,
        "ch": 37,
        "rssi": -50,
        "adv": "2F:AC:F8:13:BF:42",
        "kind": "msg",
        "pdu": "1122",
        "ts_us": 42,
    }
    # the record is what the decoder reads
    rec = SniffRecord.from_json(json.dumps(recs[0]))
    assert rec.pdu == h("1122")
    assert rec.adv == "2F:AC:F8:13:BF:42"


@pytest.mark.parametrize(
    "packet",
    [
        fake_packet(ok=False),
        fake_packet(ble_type=2),  # data PDU of a connection
        fake_packet(adv_type=1),  # ADV_DIRECT_IND
        fake_packet(payload=h("aabb")),  # shorter than an address
        fake_packet(h("020106")),  # no mesh AD structure
        SimpleNamespace(OK=True, blePacket=None),
    ],
)
def test_capture_records_ignores_other_packets(packet: Any):
    assert mesh_sniff.capture_records(packet) == []


def test_dedupe_folds_identical_pdus_within_the_window():
    d = mesh_sniff.Dedupe(1.0)
    a = {"t": 10.0, "pdu": "aa"}
    b = {"t": 10.2, "pdu": "bb"}
    assert d.push(dict(a)) == []
    assert d.push(dict(a)) == []
    assert d.push(b) == []
    later = {
        "t": 11.5,
        "pdu": "aa",
    }  # a's window closed: it is released with n=2, then remembered again
    out = d.push(later)
    assert [(r["pdu"], r.get("n")) for r in out] == [("aa", 2), ("bb", None)]
    assert [(r["pdu"], r.get("n")) for r in d.flush()] == [("aa", None)]
    assert d.flush() == []


def install_fake_sniffer_api(tmp_path: Path, packets: list[Any]) -> Path:
    """A stand-in for Nordic's package: enough surface for `cmd_capture`, records every call."""
    pkg = tmp_path / "extcap" / "SnifferAPI"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "Filelock.py").write_text(
        "def lock(port):\n    raise AssertionError('lock must be disabled')\n\ndef unlock(port):\n    raise AssertionError\n"
    )
    (pkg / "CaptureFiles.py").write_text(
        "class CaptureFileHandler:\n"
        "    def __init__(self, capture_file_path=None, clear=False):\n"
        "        raise AssertionError('the /tmp/logs capture file must be disabled')\n"
    )
    (pkg / "Pcap.py").write_text(
        "def get_global_header():\n    return b'PCAPHDR'\n\n"
        "def create_packet(packet, ts):\n    return b'PKT' + bytes([len(packet)])\n"
    )
    (pkg / "Devices.py").write_text(
        "class Device:\n"
        "    def __init__(self, address, name, RSSI):\n        self.address, self.name, self.RSSI = address, name, RSSI\n"
        "class DeviceList:\n"
        "    def __init__(self):\n        self.devices = []\n"
    )
    (pkg / "Sniffer.py").write_text(
        "from . import CaptureFiles, Devices\n"
        "CALLS = []\n"
        "class Sniffer:\n"
        "    def __init__(self, port, baudrate):\n        CALLS.append(('init', port, baudrate)); self._captureHandler = CaptureFiles.CaptureFileHandler(capture_file_path=None); self._batches = list(BATCHES); self._devices = Devices.DeviceList(); self._devices.devices = list(DEVICES)\n"
        "    def getFirmwareVersion(self):\n        CALLS.append('fw')\n"
        "    def getTimestamp(self):\n        CALLS.append('ts')\n"
        "    def start(self):\n        CALLS.append('start')\n"
        "    def setAdvHopSequence(self, seq):\n        CALLS.append(('hop', list(seq)))\n"
        "    def scan(self, *flags):\n        CALLS.append(('scan', flags))\n"
        "    def getDevices(self):\n        return self._devices\n"
        "    def addDevice(self, device):\n        CALLS.append(('add', list(device.address))); self._devices.devices.append(device)\n"
        "    def follow(self, device):\n        CALLS.append(('follow', list(device.address)))\n"
        "    def getPackets(self):\n        batch = self._batches.pop(0) if self._batches else []\n        for p in batch:\n            self._captureHandler.writePacket(p)\n        return batch\n"
        "    def doExit(self):\n        CALLS.append('exit')\n"
        "BATCHES = []\n"
        "DEVICES = []\n"
    )
    return tmp_path / "extcap"


@pytest.fixture
def fake_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    api_dir = install_fake_sniffer_api(tmp_path, [])
    for name in [
        m for m in sys.modules if m == "SnifferAPI" or m.startswith("SnifferAPI.")
    ]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return api_dir


def test_load_sniffer_api_disables_the_lock(fake_api: Path):
    api = mesh_sniff.load_sniffer_api(str(fake_api))
    assert api.Filelock.lock("/dev/ttyACM0") is None
    assert api.Filelock.unlock("/dev/ttyACM0") is None
    assert api.Pcap.get_global_header() == b"PCAPHDR"


def test_load_sniffer_api_disables_the_tmp_capture_file(fake_api: Path):
    """SnifferAPI's collector writes every packet to /tmp/logs/capture.pcap on its own; the fake's handler raises."""
    api = mesh_sniff.load_sniffer_api(str(fake_api))
    handler = api.CaptureFiles.CaptureFileHandler(capture_file_path=None)
    assert handler.writePacket(object()) is None


def test_load_sniffer_api_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in [
        m for m in sys.modules if m == "SnifferAPI" or m.startswith("SnifferAPI.")
    ]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit, match="SnifferAPI not found"):
        mesh_sniff.load_sniffer_api(str(tmp_path / "nowhere"))


def test_cmd_capture_writes_ndjson_and_pcap(
    fake_api: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    api = mesh_sniff.load_sniffer_api(str(fake_api))
    mesh = fake_packet(h("032a1122"), time=1000.0)
    api.Sniffer.BATCHES[:] = [
        [mesh, fake_packet(h("020106"), time=1000.1), fake_packet(ok=False)],
        [fake_packet(h("032a1122"), time=1000.2, channel=38)],
    ]
    monkeypatch.setattr(mesh_sniff.time, "sleep", lambda _s: None)
    ndjson, pcap_path = tmp_path / "cap.ndjson", tmp_path / "cap.pcap"
    rc = mesh_sniff.main(
        [
            "capture",
            "--api",
            str(fake_api),
            "--port",
            "/dev/ttyX",
            "--channels",
            "37,38",
            "--seconds",
            "0.01",
            "--ndjson",
            str(ndjson),
            "--pcap",
            str(pcap_path),
        ]
    )
    assert rc == 0
    lines = ndjson.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["ch"] == 38
    assert pcap_path.read_bytes().startswith(
        b"PCAPHDR" + b"PKT"
    )  # header + one good packet per OK packet
    assert pcap_path.read_bytes().count(b"PKT") == 3
    assert api.Sniffer.CALLS[:1] == [("init", "/dev/ttyX", 1_000_000)]
    assert ("hop", [37, 38]) in api.Sniffer.CALLS
    assert "exit" in api.Sniffer.CALLS
    assert (
        "2 mesh AD structures, 2 records written {'msg': 2}" in capsys.readouterr().err
    )


def test_cmd_capture_stdout_dedupe_and_keyboard_interrupt(
    fake_api: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    api = mesh_sniff.load_sniffer_api(str(fake_api))
    same = fake_packet(h("032a1122"), time=1000.0)
    api.Sniffer.BATCHES[:] = [
        [same, fake_packet(h("032a1122"), time=1000.1)],
        [fake_packet(h("032a3344"), time=1000.2)],
    ]
    calls = {"n": 0}

    def sleep(_s: float) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(mesh_sniff.time, "sleep", sleep)
    rc = mesh_sniff.main(
        ["capture", "--api", str(fake_api), "--ndjson", "-", "--dedupe", "2"]
    )
    assert rc == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(r["pdu"], r.get("n")) for r in out] == [("1122", 2), ("3344", None)]


def test_cmd_capture_rejects_bad_channels(fake_api: Path):
    with pytest.raises(SystemExit, match="--channels"):
        mesh_sniff.main(["capture", "--api", str(fake_api), "--channels", "36"])


# ----------------------------------------------------------------------------- decode side


def write_capture(air: Air, path: Path) -> dict[str, Any]:
    """A small synthetic capture: a beacon, a message with a relay copy, a segmented reply, a foreign PDU."""
    recs = []
    recs.append(air.record(air.beacon(), kind="beacon"))
    recs.append(air.record(air.beacon(), kind="beacon"))
    on = air.access(LIGHT_SWITCH, GROUP, onoff_status(True), ttl=4)
    recs.append(air.record(on))
    seq = air.seq
    recs.append(
        air.record(air.access(LIGHT_SWITCH, GROUP, onoff_status(True), ttl=3, seq=seq))
    )
    recs.append(air.record(air.access(SOCKET, GROUP, onoff_status(False), ttl=4)))
    foreign = bytes([(on[0] & 0x80) | ((air.nk.nid + 1) & 0x7F)]) + on[1:]
    recs.append(air.record(foreign))
    recs.append(air.record(h("aabb"), kind="pbadv"))
    path.write_text("".join(r.to_json() + "\n" for r in recs))
    return {"seq": seq}


@pytest.fixture
def capture(cdb: CDB, tmp_path: Path) -> Path:
    path = tmp_path / "cap.ndjson"
    write_capture(Air(cdb), path)
    return path


def test_read_records_ndjson_stdin_and_pcap(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from_file = list(mesh_sniff.read_records(str(capture)))
    assert len(from_file) == 7
    assert from_file[0].kind == "beacon"
    monkeypatch.setattr(sys, "stdin", capture.open())
    assert list(mesh_sniff.read_records("-")) == from_file
    pcap_path = tmp_path / "cap.pcap"
    pcap_path.write_bytes(pcap(nordic_packet(h("032a1122"))))
    (rec,) = mesh_sniff.read_records(str(pcap_path))
    assert rec.pdu == h("1122")
    assert rec.channel == 38


def test_cmd_decode_prints_messages_and_summary(
    cdb: CDB, capture: Path, capsys: pytest.CaptureFixture[str]
):
    rc = mesh_sniff.main(
        [
            "decode",
            "--export",
            str(Path("tests/fixtures/MeshNetwork.json")),
            str(capture),
        ]
    )
    assert rc == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert (
        len(lines) == 3
    )  # one beacon (the second is unchanged), two messages; no copies, no foreign
    assert "beacon iv=0 flags=- auth=ok" in lines[0]
    assert "0148→C061 ttl=4" in lines[1]
    assert "Generic OnOff Status present=ON" in lines[1]
    assert lines[1].split()[1] == "ch37"
    assert lines[1].split()[2] == "-50"
    assert "0172→C061" in lines[2]
    assert "records: access=2, beacon=2, copy=1, foreign=1, pbadv=1; iv_index=0" in err
    assert "1  0148 (" in err
    assert "1  0172 (" in err


def test_cmd_decode_filters_copies_beacons_foreign_and_json(
    cdb: CDB, capture: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    export = str(Path("tests/fixtures/MeshNetwork.json"))
    out_json = tmp_path / "decoded.ndjson"
    mesh_sniff.main(
        [
            "decode",
            "--export",
            export,
            str(capture),
            "--copies",
            "--beacons",
            "--foreign",
            "--json",
            str(out_json),
        ]
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 7
    assert lines[1].split()[3:] == ["beacon", "iv=0", "flags=-", "auth=ok"]
    assert "↳ copy 2 ttl=3" in lines[3]
    assert "foreign network PDU" in lines[5]
    assert "PB-ADV aabb" in lines[6]
    docs = [json.loads(line) for line in out_json.read_text().splitlines()]
    assert [d["kind"] for d in docs] == [
        "beacon",
        "beacon",
        "access",
        "copy",
        "access",
        "foreign",
        "pbadv",
    ]
    assert docs[0]["iv_index"] == 0
    assert docs[0]["key_refresh"] is False
    assert docs[2]["src"] == "0148"
    assert docs[2]["dst"] == "C061"
    assert docs[2]["ttl"] == 4
    assert docs[2]["opcode"] == "8204"
    assert docs[2]["params"] == "01"
    assert docs[2]["key"] == "app0"
    assert docs[3]["copies"] == 2
    assert docs[3]["seq"] == docs[2]["seq"]

    capsys.readouterr()
    mesh_sniff.main(
        ["decode", "--export", export, str(capture), "--src", "0172", "--copies"]
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert "0172→C061" in lines[1]

    mesh_sniff.main(
        [
            "decode",
            "--export",
            export,
            str(capture),
            "--dst",
            "0xC061",
            "--grep",
            "present=off",
        ]
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert "present=OFF" in lines[1]


def test_decoded_json_vendor_opcode(cdb: CDB):
    air = Air(cdb)
    decoder = MeshDecoder(cdb)
    decoded = decoder.feed(
        air.record(air.access(0x0149, 0xC005, vendor_button_event(3, 5)))
    )
    doc = json.loads(mesh_sniff.decoded_json(decoded))
    assert doc["opcode"] == "10:0527"
    assert doc["params"].startswith("1250")
    assert doc["kind"] == "access"


def test_wants_route_filter_without_network_context(cdb: CDB):
    air = Air(cdb)
    decoded = MeshDecoder(cdb).feed(air.record(air.beacon(), kind="beacon"))
    assert mesh_sniff._route_matches(decoded, SimpleNamespace(src=None, dst=None))
    assert not mesh_sniff._route_matches(decoded, SimpleNamespace(src=0x0148, dst=None))
    # a malformed (authenticated, undecodable) PDU obeys --src/--dst like the other routed kinds
    malformed = SimpleNamespace(
        kind="malformed",
        text="0148→C061 malformed: x",
        net=SimpleNamespace(src=0x0148, dst=0xC061),
    )
    args = SimpleNamespace(
        src=0x0172,
        dst=None,
        grep=None,
        copies=False,
        beacons=False,
        foreign=False,
        gatt=False,
    )
    assert mesh_sniff.wants(malformed, args, None) is False
    args.src = 0x0148
    assert mesh_sniff.wants(malformed, args, None) is True


def test_parse_hex_rejects_garbage():
    assert mesh_sniff.parse_hex("0xC061") == 0xC061
    with pytest.raises(Exception, match="not a hex address"):
        mesh_sniff.parse_hex("zz")


def test_main_swallows_broken_pipe(monkeypatch: pytest.MonkeyPatch):
    def boom(_args: Any) -> int:
        raise BrokenPipeError

    monkeypatch.setattr(mesh_sniff, "cmd_decode", boom)
    monkeypatch.setattr(sys, "stderr", open("/dev/null", "w"))  # noqa: SIM115, PTH123  # it gets closed on the way out
    assert mesh_sniff.main(["decode", "--export", "x", "-"]) == 0


# ----------------------------------------------------------------------------- following a connection


def data_packet(
    llid: int,
    payload: bytes,
    direction: bool = True,
    ok: bool = True,
    time: float = 2000.0,
) -> Any:
    ble = SimpleNamespace(
        type=2,
        llid=llid,
        payload=list(payload),
        accessAddress=[0xD6, 0xBE, 0x89, 0x8E],
    )
    return SimpleNamespace(
        OK=ok,
        blePacket=ble,
        channel=17,
        RSSI=-55,
        time=time,
        timestamp=7,
        direction=direction,
        boardId=0,
        getList=lambda: [0] * 16 + list(payload),
    )


def test_l2cap_reassembler():
    r = mesh_sniff.L2capReassembler()
    att = h("52 1a00 00aabb".replace(" ", ""))
    frame = len(att).to_bytes(2, "little") + h("0400") + att
    assert r.feed(2, frame) == att  # one complete fragment
    assert r.feed(2, frame[:5]) is None  # start, incomplete
    assert r.feed(1, frame[5:]) == att  # continuation completes it
    assert r.feed(1, b"\x00") is None  # a continuation without a start is ignored
    assert r.feed(3, frame) is None  # LL control PDU
    other = len(att).to_bytes(2, "little") + h("0500") + att  # another L2CAP channel
    assert r.feed(2, other) is None
    assert r.feed(2, b"\x01") is None  # shorter than an L2CAP header


def test_capture_data_records():
    att = h("1b1c00") + h("00aabbcc")  # notification, handle 1c, proxy PDU
    frame = len(att).to_bytes(2, "little") + h("0400") + att
    reassemblers: dict[str, mesh_sniff.L2capReassembler] = {}
    assert mesh_sniff.capture_data_records(
        data_packet(2, frame, direction=False), reassemblers
    ) == [
        {
            "t": 2000.0,
            "ch": 17,
            "rssi": -55,
            "adv": "D6:BE:89:8E",
            "kind": "gatt",
            "pdu": att.hex(),
            "ts_us": 7,
            "dir": "s2m",
        }
    ]
    assert (
        mesh_sniff.capture_data_records(data_packet(2, frame[:6]), reassemblers) == []
    )  # incomplete
    assert (
        mesh_sniff.capture_data_records(data_packet(2, frame, ok=False), reassemblers)
        == []
    )
    assert (
        mesh_sniff.capture_data_records(fake_packet(), reassemblers) == []
    )  # an advertisement
    rec = SniffRecord.from_json(
        json.dumps(
            mesh_sniff.capture_data_records(data_packet(1, frame[6:]), reassemblers)[0]
        )
    )
    assert rec.kind == "gatt"
    assert rec.direction == "m2s"


def test_follow_device_waits_for_the_advertiser(
    fake_api: Path, monkeypatch: pytest.MonkeyPatch
):
    api = mesh_sniff.load_sniffer_api(str(fake_api))
    sniffer = api.Sniffer.Sniffer("/dev/ttyX", 1_000_000)
    monkeypatch.setattr(mesh_sniff.time, "sleep", lambda _s: None)
    # never seen: registered and followed anyway
    mac = mesh_sniff.parse_mac(MAC_LIGHT_SWITCH)
    assert mesh_sniff.follow_device(api, sniffer, mac, wait=0.01) is False
    assert ("add", MAC_LIGHT_SWITCH_BYTES) in api.Sniffer.CALLS
    assert ("follow", MAC_LIGHT_SWITCH_BYTES) in api.Sniffer.CALLS
    # already advertising: followed at once
    api.Sniffer.CALLS.clear()
    assert mesh_sniff.follow_device(api, sniffer, mac, wait=5) is True
    assert api.Sniffer.CALLS == [("follow", MAC_LIGHT_SWITCH_BYTES)]


def test_cmd_capture_follow(
    fake_api: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    api = mesh_sniff.load_sniffer_api(str(fake_api))
    att = h("521c00") + h("00aabbcc")
    frame = len(att).to_bytes(2, "little") + h("0400") + att
    api.Sniffer.BATCHES[:] = [
        [fake_packet(h("032a1122"), time=1000.0), data_packet(2, frame)]
    ]
    monkeypatch.setattr(mesh_sniff.time, "sleep", lambda _s: None)
    monkeypatch.setattr(mesh_sniff, "FOLLOW_WAIT", 0.01)
    out = tmp_path / "follow.ndjson"
    assert (
        mesh_sniff.main(
            [
                "capture",
                "--api",
                str(fake_api),
                "--follow",
                MAC_LIGHT_SWITCH.lower(),
                "--seconds",
                "0.01",
                "--ndjson",
                str(out),
                "--dedupe",
                "2",
            ]
        )
        == 0
    )
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["kind"] for r in lines] == [
        "gatt",
        "msg",
    ]  # gatt records bypass the dedupe window, the advert waits for it
    assert lines[0]["dir"] == "m2s"
    assert f"following {MAC_LIGHT_SWITCH} (not seen yet)" in capsys.readouterr().err


# ----------------------------------------------------------------------------- P1-4: `--json` never carries a key


def test_decode_json_withholds_key_material_and_credentials(
    cdb: CDB, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """The NDJSON line used to carry the raw `params` next to the redacted `text`: the AppKey of a Config AppKey Add,
    the NetKey of a NetKey Update and the gateway API token (0xC001) in clear, in a 0644 file. Now those lines say
    `params: null, redacted: true`, everything else keeps its bytes, and the file is 0600."""
    from jhmesh import config_messages as C  # noqa: PLC0415
    from jhmesh import messages as M  # noqa: PLC0415
    from tests.helpers import admin_property_status  # noqa: PLC0415

    air = Air(cdb)
    app_key, net_key, token = (
        bytes(range(16)),
        bytes(range(16, 32)),
        b"secret-token-1234",
    )
    node = LIGHT_SWITCH
    recs = [
        air.record(seg)
        for access in (
            C.appkey_add(app_key),
            C.netkey_update(net_key),
        )  # 19 bytes each: segmented
        for seg in air.segments(node, 0x0001, access, akf=False)
    ]
    recs.append(
        air.record(air.devkey(0x0001, node, C.composition_data_get(0), node))
    )  # known, harmless: kept
    recs.append(
        air.record(air.devkey(0x0001, node, h("70aabb"), node))
    )  # unknown Config opcode under a device key
    recs += [  # the token statuses: 17-byte value → segmented
        air.record(seg)
        for access in (
            M.vendor_property_status("manufacturer", 0xC001, token),
            admin_property_status(0xC001, token),  # the SIG form of the same property
        )
        for seg in air.segments(SOCKET, GROUP, access)
    ]
    recs.append(
        air.record(air.access(SOCKET, GROUP, admin_property_status(0x5003, h("06"))))
    )  # key_mode: kept
    recs.append(air.record(air.access(LIGHT_SWITCH, GROUP, onoff_status(True))))
    capture = tmp_path / "cap.ndjson"
    capture.write_text("".join(r.to_json() + "\n" for r in recs))
    out_json = tmp_path / "decoded.ndjson"
    out_json.write_text("stale")
    out_json.chmod(0o644)
    old_umask = os.umask(0o022)
    try:
        assert (
            mesh_sniff.main(
                [
                    "decode",
                    "--export",
                    str(Path("tests/fixtures/MeshNetwork.json")),
                    str(capture),
                    "--json",
                    str(out_json),
                ]
            )
            == 0
        )
    finally:
        os.umask(old_umask)
    text = out_json.read_text()
    assert stat.S_IMODE(out_json.stat().st_mode) == 0o600
    for secret in (app_key, net_key, token):
        assert secret.hex() not in text
        assert secret.hex().upper() not in text
    docs = [json.loads(line) for line in text.splitlines()]
    by_opcode = {d["opcode"]: d for d in docs if d.get("opcode")}
    assert by_opcode["00"]["key"] == "dev:0148"  # AppKey Add
    assert by_opcode["00"]["params"] is None
    assert by_opcode["00"]["redacted"] is True
    assert by_opcode["00"]["text"].endswith("key=<16 bytes>")
    assert (
        by_opcode[f"{C.CONFIG_NETKEY_UPDATE:02X}"]["params"],
        by_opcode[f"{C.CONFIG_NETKEY_UPDATE:02X}"]["redacted"],
    ) == (None, True)
    assert (
        by_opcode[f"{C.CONFIG_COMPOSITION_DATA_GET:02X}"]["params"] == "00"
    )  # a read: its bytes stay
    assert "redacted" not in by_opcode[f"{C.CONFIG_COMPOSITION_DATA_GET:02X}"]
    assert (
        by_opcode["70"]["params"] is None
    )  # not in the Config table: could be anything, so nothing
    vendor = next(d for d in docs if d.get("opcode", "").endswith(":0527"))
    assert (vendor["params"], vendor["redacted"]) == (None, True)
    assert "gateway_api_token=<redacted>" in vendor["text"]
    sig = [d for d in docs if d.get("opcode") == f"{M.GEN_ADMIN_PROP_STATUS:02X}"]
    assert [(d["params"] is None, d.get("redacted")) for d in sig] == [
        (True, True),
        (False, None),
    ]
    assert sig[1]["params"].startswith("0350")  # key_mode: not a secret
    assert by_opcode[f"{M.GEN_ONOFF_STATUS:02X}"]["params"] == "01"
    assert "records: access=8" in capsys.readouterr().err


def test_withholds_params_edge_cases(cdb: CDB):
    from jhmesh import messages as M  # noqa: PLC0415
    from jhmesh.client import AccessMessage  # noqa: PLC0415

    def msg(access: bytes, key: str = "app0") -> AccessMessage:
        op, cid, params = M.decode_opcode(access)
        return AccessMessage(0x0172, 0xC061, 3, 1, op, cid, params, access, key)

    assert (
        mesh_sniff.withholds_params(msg(h("8204") + h("01"))) is False
    )  # not a property message
    assert (
        mesh_sniff.withholds_params(msg(h("8204") + h("01c0"))) is False
    )  # the bytes happen to spell 0xC001
    short = msg(
        bytes([M.GEN_ADMIN_PROP_STATUS >> 8, M.GEN_ADMIN_PROP_STATUS & 0xFF, 0x01])
    )
    assert mesh_sniff.withholds_params(short) is False  # no property id to look at
    unknown_pid = msg(
        bytes([M.GEN_ADMIN_PROP_STATUS >> 8, M.GEN_ADMIN_PROP_STATUS & 0xFF])
        + h("ffff00")
    )
    assert mesh_sniff.withholds_params(unknown_pid) is False
    scheduler = msg(
        h("d42705") + h("01c0")
    )  # a JUNG opcode that is not a property message, params spell 0xC001
    assert mesh_sniff.withholds_params(scheduler) is False
    vendor_config = msg(
        h("c12705") + h("00"), key="dev:0148"
    )  # a vendor opcode under a device key: unknown → withheld
    assert mesh_sniff.withholds_params(vendor_config) is True
    assert mesh_sniff.sig_property_opcodes() >= {
        M.GEN_ADMIN_PROP_STATUS,
        M.GEN_MANU_PROP_STATUS,
        M.GEN_USER_PROP_STATUS,
    }


# ----------------------------------------------------------------------------- service: SIGTERM, outputs after the dongle


def test_cmd_capture_treats_sigterm_like_ctrl_c(
    fake_api: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """`systemctl stop` sends SIGTERM: the loop unwinds through its `finally` (dedupe window flushed, dongle
    released) instead of dying with the last records unwritten; the previous handler is put back."""
    api = mesh_sniff.load_sniffer_api(str(fake_api))
    api.Sniffer.BATCHES[:] = [[fake_packet(h("032a1122"), time=1000.0)]]
    before = signal.getsignal(signal.SIGTERM)

    def sleep(_s: float) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(mesh_sniff.time, "sleep", sleep)
    assert (
        mesh_sniff.main(
            ["capture", "--api", str(fake_api), "--ndjson", "-", "--dedupe", "2"]
        )
        == 0
    )
    out = capsys.readouterr()
    assert [json.loads(line)["pdu"] for line in out.out.splitlines()] == [
        "1122"
    ]  # flushed on the way out
    assert "1 records written" in out.err
    assert "exit" in api.Sniffer.CALLS
    assert signal.getsignal(signal.SIGTERM) is before


def test_cmd_capture_opens_no_output_before_the_dongle_answers(
    fake_api: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A dongle that fails to start must not leave an empty capture file per service restart."""
    api = mesh_sniff.load_sniffer_api(str(fake_api))

    def broken(_self: Any) -> None:
        raise RuntimeError("serial port busy")

    monkeypatch.setattr(api.Sniffer.Sniffer, "start", broken)
    ndjson = tmp_path / "cap.ndjson"
    with pytest.raises(RuntimeError, match="serial port busy"):
        mesh_sniff.main(
            [
                "capture",
                "--api",
                str(fake_api),
                "--ndjson",
                str(ndjson),
                "--pcap",
                str(tmp_path / "cap.pcap"),
            ]
        )
    assert not ndjson.exists()
    assert not (tmp_path / "cap.pcap").exists()
    assert mesh_sniff.parse_channels("39,37") == [39, 37]


@pytest.mark.parametrize(
    "text",
    [
        "AA:BB:CC",
        "AA:BB:CC:DD:EE:GG",
        "AA:BB:CC:DD:EE:FF:00",
        "AABBCCDDEEFF",
        "AA:BB:CC:DD:EE:100",
    ],
)
def test_follow_rejects_a_malformed_mac(text: str) -> None:
    """TLC-05: `--follow` takes exactly six hex octets; anything else is an argparse error, not a traceback (or a
    silently wrong-length address handed to the sniffer)."""
    with pytest.raises(SystemExit):
        mesh_sniff.build_parser().parse_args(["capture", "--follow", text])


def test_parse_mac() -> None:
    assert mesh_sniff.parse_mac("AA:BB:CC:DD:EE:FF") == [
        0xAA,
        0xBB,
        0xCC,
        0xDD,
        0xEE,
        0xFF,
    ]
    assert mesh_sniff.parse_mac("aa:bb:cc:dd:ee:ff") == [
        0xAA,
        0xBB,
        0xCC,
        0xDD,
        0xEE,
        0xFF,
    ]


def test_missing_export_file_is_a_clean_error() -> None:
    """TLC-04: a missing export (or capture) file is a one-line error naming it, not a traceback."""
    with pytest.raises(SystemExit, match="No such file") as exc:
        mesh_sniff.main(["decode", "--export", "/nonexistent/x.json", "-"])
    assert "/nonexistent/x.json" in str(exc.value.code)
    with pytest.raises(SystemExit, match="No such file") as exc:
        mesh_sniff.main(
            ["decode", "--export", str(CDB_FIXTURE), "/nonexistent/in.ndjson"]
        )
    assert "/nonexistent/in.ndjson" in str(exc.value.code)


def test_cmd_decode_names_every_beacon_type(
    cdb: CDB, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """Unprovisioned Device and Mesh Private beacons print as what they are, not as another network's.

    Each beacon kind prints when it changes, so a device waiting to be provisioned, beaconing between our
    network's beacons, does not make every beacon of either kind print again.
    """
    air = Air(cdb)
    unprovisioned = b"\x00" + bytes(range(16)) + b"\x00\x02"
    recs = [
        air.record(air.beacon(), kind="beacon"),
        air.record(unprovisioned, kind="beacon"),
        air.record(air.beacon(), kind="beacon"),
        air.record(unprovisioned, kind="beacon"),
        air.record(air.private_beacon(iv_index=1), kind="beacon"),
    ]
    path = tmp_path / "cap.ndjson"
    path.write_text("".join(r.to_json() + "\n" for r in recs))
    out_json = tmp_path / "decoded.ndjson"
    rc = mesh_sniff.main(
        [
            "decode",
            "--export",
            str(Path("tests/fixtures/MeshNetwork.json")),
            str(path),
            "--json",
            str(out_json),
        ]
    )
    assert rc == 0
    out, err = capsys.readouterr()
    assert [line.split(maxsplit=3)[3] for line in out.splitlines()] == [
        "beacon iv=0 flags=- auth=ok",
        "unprovisioned device beacon 00010203-0405-0607-0809-0A0B0C0D0E0F oob=0002 (URI)",
        "private beacon iv=1 flags=- auth=ok",
    ]
    assert "records: beacon=3, unprovisioned=2; iv_index=1" in err
    docs = [json.loads(line) for line in out_json.read_text().splitlines()]
    assert docs[1]["uuid"] == "00010203-0405-0607-0809-0A0B0C0D0E0F"
    assert docs[1]["oob"] == "0002"
    assert docs[4]["iv_index"] == 1
    assert "uuid" not in docs[0]


def test_ad_structures_stop_at_a_truncated_structure() -> None:
    """A length that runs past the data ends the walk instead of yielding a short value."""
    assert list(mesh_sniff.ad_structures(bytes([2, 0x2A, 0x01, 5, 0x2A]))) == [
        (0x2A, b"\x01")
    ]


def test_wants_gatt_records_only_when_asked_and_unknown_kinds_always() -> None:
    args = argparse.Namespace(gatt=False)
    assert mesh_sniff.wants(SimpleNamespace(kind="gatt"), args, None) is False
    args.gatt = True
    assert mesh_sniff.wants(SimpleNamespace(kind="gatt"), args, None) is True
    assert mesh_sniff.wants(SimpleNamespace(kind="something new"), args, None) is True


def test_decode_interrupted_still_prints_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl-C while decoding a live pipe ends the listing, not the summary."""

    def interrupted(_path: str) -> Iterator[Any]:
        raise KeyboardInterrupt
        yield

    monkeypatch.setattr(mesh_sniff, "read_records", interrupted)
    assert mesh_sniff.main(["decode", "--export", str(CDB_FIXTURE), "-"]) == 0
    assert "records: " in capsys.readouterr().err
