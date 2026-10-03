"""tools/trace_to_fixture.py on a synthetic capture: nothing of the installation reaches a trace (Q4 T5).

The "installation" is the stand-in of `tests/traces/make_traces.py`: the fixture network with other keys, other
UUIDs and MACs and every address moved, played as sniffer records and decoded as `mesh_sniff.py decode --json`
writes them. A trace made from it must hold none of its keys (in any encoding), MACs, UUIDs or addresses, map
addresses the same way whatever the order of the capture, and decode under the fixture's keys to the very access
PDUs the capture carried.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from jhmesh import messages as M
from jhmesh.advert import mac_from_uuid
from jhmesh.cdb import CDB
from jhmesh.pdu import decode_opcode, encode_opcode
from jhmesh.sniffer import MeshDecoder, SniffRecord
from tests import key_scan
from tests.traces import make_traces
from tests.traces.make_traces import shifted
from tools import trace_to_fixture as T

FIXTURE = make_traces.FIXTURE


@pytest.fixture(scope="module")
def standin() -> CDB:
    return CDB.from_network(make_traces.standin_network())


@pytest.fixture(scope="module")
def fixture_cdb() -> CDB:
    return CDB.load(FIXTURE)


@pytest.fixture(scope="module")
def decoded(standin: CDB) -> list[str]:
    return make_traces.decoded(standin, make_traces.capture(standin))


def access_line(
    src: int,
    dst: int,
    access: bytes,
    t: float = 1.0,
    key: str = "app0",
    ttl: int = 5,
) -> str:
    """One `decode --json` line of an access message, as `mesh_sniff.decoded_json` writes it."""
    opcode, company, params = decode_opcode(access)
    return json.dumps(
        {
            "t": t,
            "ch": 37,
            "rssi": -60,
            "kind": "access",
            "text": "…",
            "src": f"{src:04X}",
            "dst": f"{dst:04X}",
            "ttl": ttl,
            "seq": "000001",
            "key": key,
            "opcode": f"{opcode:02X}"
            if company is None
            else f"{opcode:02X}:{company:04X}",
            "params": params.hex(),
        }
    )


def onoff(on: bool = True) -> bytes:
    return encode_opcode(M.GEN_ONOFF_STATUS) + bytes([on])


# ----------------------------------------------------------------------------- the committed synthetic trace


def test_the_synthetic_trace_regenerates_byte_for_byte() -> None:
    assert make_traces.build() == make_traces.TRACE.read_text(encoding="utf-8"), (
        "regenerate with tests/traces/make_traces.py"
    )


# ----------------------------------------------------------------------------- privacy


def test_no_key_identity_mac_or_address_of_the_input_in_the_output(
    standin: CDB, fixture_cdb: CDB, decoded: list[str]
) -> None:
    conversion = T.convert(decoded, standin, fixture_cdb, make_traces.explicit_map())
    text = "\n".join(conversion.lines)
    secrets = key_scan.secrets(standin) | {"Network ID": standin.net_keys[0].network_id}
    assert key_scan.leaks(text, secrets) == []
    upper = text.upper()
    for node in standin.nodes:
        assert node.uuid.replace("-", "") not in upper.replace("-", "")
        mac = mac_from_uuid(node.uuid)
        assert mac is None or mac not in upper
        assert mac is None or mac.replace(":", "") not in upper
    assert standin.mesh_uuid.replace("-", "") not in upper.replace("-", "")
    # the advertising MACs of the capture records
    for record in make_traces.capture(standin):
        assert record.adv not in upper
        assert record.adv.replace(":", "") not in upper
    installation = T.installation_addresses(standin, [])
    for line in conversion.lines:
        doc = json.loads(line)
        assert set(doc) == set(T.FIELDS)
        assert int(doc["src"], 16) not in installation
        assert int(doc["dst"], 16) not in installation


def test_what_is_dropped(standin: CDB, fixture_cdb: CDB, decoded: list[str]) -> None:
    conversion = T.convert(decoded, standin, fixture_cdb, make_traces.explicit_map())
    assert conversion.dropped == {
        "beacon": 2,
        "copy": 20,
        "control": 1,
        "device key": 1,
    }
    assert len(conversion.lines) == 20


def test_mapping_is_stable_whatever_the_order(
    standin: CDB, fixture_cdb: CDB, decoded: list[str]
) -> None:
    """The same input addresses map the same way in a reordered capture; the explicit pairs are kept."""
    explicit = make_traces.explicit_map()
    forward = T.convert(decoded, standin, fixture_cdb, explicit)
    backward = T.convert(decoded[::-1], standin, fixture_cdb, explicit)
    assert forward.mapping == backward.mapping
    assert forward.lines == T.convert(decoded, standin, fixture_cdb, explicit).lines
    for real, fixture in explicit.items():
        assert forward.mapping[real] == fixture
    # the client, which no --map names, gets the first pool address
    assert forward.mapping[shifted(make_traces.CLIENT)] == T.UNICAST_POOL


def test_round_trip_decodes_to_the_same_access_pdus(
    standin: CDB, fixture_cdb: CDB, decoded: list[str]
) -> None:
    conversion = T.convert(decoded, standin, fixture_cdb, make_traces.explicit_map())
    expected = [
        (
            conversion.mapping[int(doc["src"], 16)],
            conversion.mapping[int(doc["dst"], 16)],
            doc["ttl"],
            encode_opcode(*_opcode(doc["opcode"])) + bytes.fromhex(doc["params"]),
        )
        for doc in map(json.loads, decoded)
        if doc["kind"] == "access" and doc["key"].startswith("app")
    ]
    decoder = MeshDecoder(fixture_cdb)
    got = []
    for doc in map(json.loads, conversion.lines):
        for pdu in doc["pdus"]:
            result = decoder.feed(
                SniffRecord(1.0, 37, 0, "", "msg", bytes.fromhex(pdu))
            )
        assert result.message is not None
        got.append(
            (
                result.message.src,
                result.message.dst,
                result.message.ttl,
                result.message.access_pdu,
            )
        )
        assert result.message.access_pdu.hex() == doc["access"]
    assert got == expected


def _opcode(text: str) -> tuple[int, int | None]:
    op, _, company = text.partition(":")
    return int(op, 16), int(company, 16) if company else None


def test_a_long_message_is_segmented_and_still_round_trips(
    standin: CDB, fixture_cdb: CDB
) -> None:
    light = shifted(make_traces.LIGHT)
    long = encode_opcode(0x11, M.JUNG_CID) + bytes(range(0x30, 0x48))
    conversion = T.convert(
        [access_line(light, 0x0001 + 0x0800, long)], standin, fixture_cdb, {}
    )
    (doc,) = map(json.loads, conversion.lines)
    assert len(doc["pdus"]) == 3
    decoder = MeshDecoder(fixture_cdb)
    kinds = [
        decoder.feed(SniffRecord(1.0, 37, 0, "", "msg", bytes.fromhex(p)))
        for p in doc["pdus"]
    ]
    assert [d.kind for d in kinds] == ["segment", "segment", "access"]
    assert kinds[-1].message is not None
    assert kinds[-1].message.access_pdu == long


def test_delays_are_relative_and_rounded(standin: CDB, fixture_cdb: CDB) -> None:
    light = shifted(make_traces.LIGHT)
    group = shifted(make_traces.LIGHT_GROUP)
    lines = [
        access_line(light, group, onoff(), t=987_654.123456),
        access_line(light, group, onoff(), t=987_654.3389),
        access_line(light, group, onoff(), t=987_656.0),
    ]
    docs = [json.loads(x) for x in T.convert(lines, standin, fixture_cdb, {}).lines]
    assert [d["delay"] for d in docs] == [0.0, 0.22, 1.66]
    assert all("98765" not in json.dumps(d) for d in docs)


# ----------------------------------------------------------------------------- what is kept and how


def test_keep_and_fixed_addresses(standin: CDB, fixture_cdb: CDB) -> None:
    light, socket = shifted(make_traces.LIGHT), shifted(make_traces.SOCKET)
    lines = [
        access_line(light, 0xFFFF, onoff()),
        access_line(light, 0x0000, onoff()),
        access_line(socket, 0xFFFF, onoff()),
        "",
    ]
    conversion = T.convert(lines, standin, fixture_cdb, {}, keep={light})
    assert conversion.dropped == {"not kept": 1}
    assert [(d["src"], d["dst"]) for d in map(json.loads, conversion.lines)] == [
        ("7000", "FFFF"),
        ("7000", "0000"),
    ]


def test_withheld_virtual_and_address_carrying_messages_are_dropped(
    standin: CDB, fixture_cdb: CDB
) -> None:
    light, gateway = shifted(make_traces.LIGHT), shifted(make_traces.GATEWAY)
    withheld = json.loads(access_line(light, gateway, onoff()))
    withheld.update(params=None, redacted=True)
    # a vendor message whose parameters name another node of the installation (the socket, little endian)
    naming = encode_opcode(0x05, M.JUNG_CID) + shifted(make_traces.SOCKET).to_bytes(
        2, "little"
    )
    lines = [
        json.dumps(withheld),
        access_line(light, 0x8123, onoff()),
        access_line(light, gateway, naming),
        access_line(light, gateway, onoff()),
    ]
    conversion = T.convert(lines, standin, fixture_cdb, {})
    assert conversion.dropped == {
        "withheld parameters": 1,
        "virtual destination": 1,
        "address in the parameters": 1,
    }
    assert len(conversion.lines) == 1


def test_an_address_mapped_to_itself_on_purpose_may_stay(
    standin: CDB, fixture_cdb: CDB
) -> None:
    light, gateway = shifted(make_traces.LIGHT), shifted(make_traces.GATEWAY)
    naming = encode_opcode(0x05, M.JUNG_CID) + gateway.to_bytes(2, "little")
    conversion = T.convert(
        [access_line(light, gateway, naming)],
        standin,
        fixture_cdb,
        {light: make_traces.LIGHT, gateway: gateway},
    )
    (doc,) = map(json.loads, conversion.lines)
    assert (doc["src"], doc["dst"]) == ("0148", f"{gateway:04X}")


def test_the_pool_skips_fixture_and_installation_addresses() -> None:
    mapper = T.Mapper(explicit={}, reserved={T.UNICAST_POOL, T.GROUP_POOL})
    assert mapper.assign({0x0001, 0xC001}) == {
        0x0001: T.UNICAST_POOL + 1,
        0xC001: T.GROUP_POOL + 1,
    }


# ----------------------------------------------------------------------------- refusals


def test_refuses_a_key_in_the_parameters(standin: CDB, fixture_cdb: CDB) -> None:
    light, gateway = shifted(make_traces.LIGHT), shifted(make_traces.GATEWAY)
    carrying = encode_opcode(0x05, M.JUNG_CID) + standin.app_keys[0].key
    with pytest.raises(T.LeakError, match=r"AppKey 0 as hex") as err:
        T.convert([access_line(light, gateway, carrying)], standin, fixture_cdb, {})
    assert standin.app_keys[0].key.hex() not in str(err.value)


def test_refuses_a_mac_in_the_parameters(standin: CDB, fixture_cdb: CDB) -> None:
    light, gateway = shifted(make_traces.LIGHT), shifted(make_traces.GATEWAY)
    node = standin.node_by_addr(light)
    assert node is not None
    mac = mac_from_uuid(node.uuid)
    assert mac is not None
    carrying = (
        encode_opcode(0x05, M.JUNG_CID) + bytes.fromhex(mac.replace(":", ""))[::-1]
    )
    with pytest.raises(T.LeakError, match=r"MAC of node 0948"):
        T.convert([access_line(light, gateway, carrying)], standin, fixture_cdb, {})


def test_refuses_an_advertising_mac_of_the_input(
    standin: CDB, fixture_cdb: CDB
) -> None:
    light, gateway = shifted(make_traces.LIGHT), shifted(make_traces.GATEWAY)
    adv = "4A:00:5E:00:53:77"
    record = json.loads(
        access_line(
            light,
            gateway,
            encode_opcode(0x05, M.JUNG_CID) + bytes.fromhex("4a005e005377"),
        )
    )
    record["adv"] = adv
    with pytest.raises(T.LeakError, match=r"input MAC 0"):
        T.convert([json.dumps(record)], standin, fixture_cdb, {})


def test_refuses_a_uuid_in_the_parameters(standin: CDB, fixture_cdb: CDB) -> None:
    light, gateway = shifted(make_traces.LIGHT), shifted(make_traces.GATEWAY)
    carrying = encode_opcode(0x05, M.JUNG_CID) + bytes.fromhex(
        standin.mesh_uuid.replace("-", "")
    )
    with pytest.raises(T.LeakError, match=r"mesh UUID"):
        T.convert([access_line(light, gateway, carrying)], standin, fixture_cdb, {})


def test_refuses_to_map_onto_another_address_of_the_installation(
    standin: CDB, fixture_cdb: CDB
) -> None:
    light, gateway = shifted(make_traces.LIGHT), shifted(make_traces.GATEWAY)
    with pytest.raises(T.LeakError, match=r"an address of the installation"):
        T.convert(
            [access_line(light, gateway, onoff())],
            standin,
            fixture_cdb,
            {light: gateway},
        )


def test_check_refuses_an_address_that_is_no_mapping_target(standin: CDB) -> None:
    conversion = T.Conversion(
        lines=[json.dumps({"src": "0148", "dst": "C061"})], mapping={0x0948: 0x0148}
    )
    with pytest.raises(T.LeakError, match=r"dst C061, which is no mapping target"):
        T.check(conversion, standin, set(), set())


def test_secrets_include_derived_keys_old_keys_and_skip_placeholders(
    fixture_cdb: CDB,
) -> None:
    secrets = T.secrets_of(fixture_cdb)
    assert "NetKey 0 privacy key" in secrets
    assert "Network ID 0" in secrets
    # the fixture's device keys are not placeholders of one repeated byte, the all-zero one would be
    network = make_traces.standin_network()
    network["nodes"][0]["deviceKey"] = "00" * 16
    network["netKeys"][0].update(phase=1, oldKey="0123456789abcdef0123456789abcdef")
    cdb = CDB.from_network(network)
    secrets = T.secrets_of(cdb)
    assert "old NetKey 0" in secrets
    assert f"device key of {cdb.nodes[0].unicast:04X}" not in secrets


def test_a_line_that_is_no_decoded_record_is_an_error(
    standin: CDB, fixture_cdb: CDB
) -> None:
    with pytest.raises(ValueError, match=r"line 2 is not a decoded record"):
        T.convert(
            [access_line(0x0948, 0xC561, onoff()), "{not json"],
            standin,
            fixture_cdb,
            {},
        )


# ----------------------------------------------------------------------------- CLI


def write_export(tmp_path: Path) -> Path:
    path = tmp_path / "standin.json"
    path.write_text(
        json.dumps({"meshNetwork": make_traces.standin_network()}), encoding="utf-8"
    )
    return path


def test_cli_writes_the_trace(
    tmp_path: Path, decoded: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    export = write_export(tmp_path)
    capture = tmp_path / "decoded.ndjson"
    capture.write_text("\n".join(decoded) + "\n", encoding="utf-8")
    out = tmp_path / "trace.ndjson"
    maps = [
        f"--map={real:04X}={fixture:04X}"
        for real, fixture in make_traces.explicit_map().items()
    ]
    assert T.main([str(capture), "--export", str(export), *maps, "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == make_traces.TRACE.read_text(
        encoding="utf-8"
    )
    err = capsys.readouterr().err
    assert "20 messages; dropped: beacon=2, control=1, copy=20, device key=1" in err
    assert "1502 -> 7000" in err
    # to stdout, with --keep
    light = shifted(make_traces.LIGHT)
    assert (
        T.main([str(capture), "--export", str(export), "--keep", f"{light:04X}"]) == 0
    )
    printed = capsys.readouterr()
    assert len(printed.out.splitlines()) == 7
    assert "not kept=13" in printed.err


def test_cli_writes_nothing_on_a_leak(
    tmp_path: Path, standin: CDB, capsys: pytest.CaptureFixture[str]
) -> None:
    export = write_export(tmp_path)
    light, gateway = shifted(make_traces.LIGHT), shifted(make_traces.GATEWAY)
    capture = tmp_path / "decoded.ndjson"
    capture.write_text(
        access_line(
            light, gateway, encode_opcode(0x05, M.JUNG_CID) + standin.app_keys[0].key
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "trace.ndjson"
    assert T.main([str(capture), "--export", str(export), "-o", str(out)]) == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "nothing written" in err
    assert standin.app_keys[0].key.hex() not in err


def test_cli_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    export = write_export(tmp_path)
    assert T.main([str(tmp_path / "missing.ndjson"), "--export", str(export)]) == 1
    assert "cannot read the input" in capsys.readouterr().err
    bad = tmp_path / "bad.ndjson"
    bad.write_text("[]\n", encoding="utf-8")
    assert T.main([str(bad), "--export", str(export)]) == 1
    assert "line 1 is not a decoded record" in capsys.readouterr().err
    with pytest.raises(argparse.ArgumentTypeError):
        T.parse_map("0148")
    with pytest.raises(argparse.ArgumentTypeError):
        T.parse_hex("zz")
    assert T.parse_map("0948=0148") == (0x0948, 0x0148)
