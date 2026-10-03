"""Generate the SYNTHETIC replay trace `synthetic.ndjson` the way a real one is made, from a stand-in installation.

Run from anywhere: `.venv/bin/python tests/traces/make_traces.py`. Never put a capture of a real installation here.

A real trace comes from a capture of the installation, decoded with its export (`tools/mesh_sniff.py decode
--json`) and converted with `tools/trace_to_fixture.py` on the capture host (`docs/dev/testing.md`, *Replayed
traces*). This script runs the same chain on a stand-in: `standin_network()` is the fixture network
(`tests/fixtures/MeshNetwork.json`) with other synthetic keys, other node UUIDs and MACs (still in the IANA
documentation block) and every unicast and room or element group address moved (`UNICAST_SHIFT`, `GROUP_SHIFT`), so
the converter has an "installation" whose every key, identity and address must be gone from its output.
`capture()` plays a few minutes of documented on-air behaviour (`docs/sniffer.md`, *What the first captures
established*) on it as sniffer records: an acknowledged Set that changes a light's state (no unicast reply, the
status published twice), a gateway Set to a light that is already on (a unicast reply as well), the gateway polling
a socket, the socket's meter publishing power, current and voltage once each, a tunable-white light's status
published twice, and a rocker's button events with their counters (a double press published twice, a hold) — with
relay copies, a device-key message, a heartbeat and a beacon around them for the converter to drop. Synthetic
throughout: it shows the shape of the traffic, not anything a real device sent.

`tests/test_trace_converter.py` reruns `build()` and fails unless it gives the committed file byte for byte.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from jhmesh import config_messages as C  # noqa: E402
from jhmesh import messages as M  # noqa: E402
from jhmesh.cdb import CDB  # noqa: E402
from jhmesh.crypto import aes_cmac  # noqa: E402
from jhmesh.pdu import (  # noqa: E402
    encode_opcode,
    lower_unsegmented_access,
    network_encrypt,
    upper_encrypt_app,
    upper_encrypt_dev,
)
from jhmesh.sniffer import MeshDecoder, SniffRecord  # noqa: E402
from tools import mesh_sniff  # noqa: E402
from tools.trace_to_fixture import convert  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "MeshNetwork.json"
TRACE = HERE / "synthetic.ndjson"

# the stand-in installation: keys, identities and addresses that are not the fixture's
NETKEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
APPKEY = "a0b1c2d3e4f5061728394a5b6c7d8e9f"
MESH_UUID = "1BAF3ADE-0000-4000-8000-0000000000FE"
UNICAST_SHIFT = 0x0800  # 0148 → 0948
GROUP_SHIFT = (
    0x0500  # C061 → C561 (the device type groups at FEF5 / FEF8 stay where they are)
)
CLIENT = 0x0D02  # another client of the installation (a phone, Home Assistant): no node of either export

# the fixture's elements and groups the trace is about (`tests/helpers.py` names them)
GATEWAY, LIGHT, SOCKET, SOCKET_METER, LIGHT_CTL, ROCKER = (
    0x00DC,
    0x0148,
    0x0172,
    0x0173,
    0x0232,
    0x0234,
)
LIGHT_GROUP, METER_GROUP, CTL_GROUP = 0xC061, 0xC001, 0xC044
FIXTURE_ADDRESSES = (
    GATEWAY,
    LIGHT,
    SOCKET,
    SOCKET_METER,
    LIGHT_CTL,
    ROCKER,
    LIGHT_GROUP,
    METER_GROUP,
    CTL_GROUP,
)

SENSOR_POWER, SENSOR_CURRENT, SENSOR_VOLTAGE = 0x0081, 0x005C, 0x005D
ROCKER_UP_CLICK, ROCKER_DOWN_HOLD, BUTTON_HOLD_END = 0x01, 0x02, 0x04
BUTTON_EVENT = 0x5012  # LBC user property ButtonEvent: [counter][code]


def shifted(address: int) -> int:
    """Where a fixture address is in the stand-in installation."""
    if address < 0x8000:
        return address + UNICAST_SHIFT
    return address + GROUP_SHIFT if address < 0xF000 else address


def _shift_text(text: str) -> str:
    return f"{shifted(int(text, 16)):04X}"


def standin_network() -> dict[str, Any]:
    """The `meshNetwork` object of the stand-in installation (`shifted` addresses, other keys and identities)."""
    net = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8"))["meshNetwork"])
    net["meshUUID"] = MESH_UUID
    net["netKeys"][0]["key"] = NETKEY
    net["appKeys"][0]["key"] = APPKEY
    for node in net["nodes"]:
        node["UUID"] = (
            node["UUID"][:16]
            + f"{int(node['UUID'][16:18], 16) | 0x80:02X}"
            + node["UUID"][18:]
        )
        node["unicastAddress"] = _shift_text(node["unicastAddress"])
        node["deviceKey"] = "d0e1f2" + node["unicastAddress"].lower() + "a5" * 11
        for element in node["elements"]:
            for model in element["models"]:
                model["subscribe"] = [
                    _shift_text(a) for a in model.get("subscribe", [])
                ]
                if "publish" in model:
                    model["publish"]["address"] = _shift_text(
                        model["publish"]["address"]
                    )
    for group in net["groups"]:
        group["address"] = _shift_text(group["address"])
    for scene in net["scenes"]:
        scene["addresses"] = [_shift_text(a) for a in scene["addresses"]]
    return net


class Air:
    """The stand-in installation's advertising bearer: network PDUs as its nodes send them, as sniffer records."""

    def __init__(self, cdb: CDB) -> None:
        self.cdb = cdb
        self.nk, self.ak = cdb.net_keys[0], cdb.app_keys[0]
        self.seq: dict[int, int] = {}
        self.records: list[SniffRecord] = []

    def _next(self, src: int) -> int:
        self.seq[src] = self.seq.get(src, 0x0B0800 + (src & 0xFF)) + 1
        return self.seq[src]

    def _record(self, t: float, pdu: bytes, kind: str = "msg") -> None:
        # the advertising address is random per transmission on air; any MAC here must not reach the trace
        adv = f"4A:00:5E:00:53:{len(self.records) & 0xFF:02X}"
        self.records.append(
            SniffRecord(1000 + t, 37 + len(self.records) % 3, -60, adv, kind, pdu)
        )

    def send(
        self, t: float, src: int, dst: int, access: bytes, ttl: int = 5, relays: int = 1
    ) -> None:
        """Node `src` (a fixture address, sent from its stand-in) sends `access`; `relays` copies follow it."""
        src, dst = shifted(src), shifted(dst)
        seq = self._next(src)
        upper = upper_encrypt_app(self.ak, 0, seq, src, dst, access)
        lower = lower_unsegmented_access(self.ak.aid, upper)
        for hop in range(relays + 1):
            pdu = network_encrypt(self.nk, 0, False, ttl - hop, seq, src, dst, lower)
            self._record(t + 0.03 * hop, pdu)

    def send_config(self, t: float, src: int, node: int, access: bytes) -> None:
        """A device-key message to `node`, as the app sends one."""
        src, node = shifted(src), shifted(node)
        found = self.cdb.node_by_addr(node)
        assert found is not None
        seq = self._next(src)
        upper = upper_encrypt_dev(found.dev_key, 0, seq, src, node, access)
        lower = lower_unsegmented_access(0, upper, akf=False)
        self._record(t, network_encrypt(self.nk, 0, False, 5, seq, src, node, lower))

    def heartbeat(self, t: float, src: int, dst: int) -> None:
        src, dst = shifted(src), shifted(dst)
        transport = bytes([0x0A, 5]) + (3).to_bytes(2, "big")
        self._record(
            t,
            network_encrypt(self.nk, 0, True, 4, self._next(src), src, dst, transport),
        )

    def beacon(self, t: float) -> None:
        body = b"\x00" + self.nk.network_id + (0).to_bytes(4, "big")
        self._record(
            t, b"\x01" + body + aes_cmac(self.nk.beacon_key, body)[:8], kind="beacon"
        )


def onoff_status(on: bool) -> bytes:
    return encode_opcode(M.GEN_ONOFF_STATUS) + bytes([1 if on else 0])


def sensor_status(prop: int, raw: bytes) -> bytes:
    """Sensor Status with one Format B marshalled value."""
    return (
        encode_opcode(M.SENSOR_STATUS)
        + bytes([((len(raw) - 1) << 1) | 1])
        + prop.to_bytes(2, "little")
        + raw
    )


def ctl_status(lightness: int, kelvin: int) -> bytes:
    return (
        encode_opcode(M.LIGHT_CTL_STATUS)
        + lightness.to_bytes(2, "little")
        + kelvin.to_bytes(2, "little")
    )


def button_event(counter: int, code: int) -> bytes:
    """LBC User Property Set Unacknowledged of ButtonEvent, as a key in gateway mode publishes it."""
    return (
        encode_opcode(0x10, M.JUNG_CID)
        + BUTTON_EVENT.to_bytes(2, "little")
        + bytes([counter, code])
    )


def capture(cdb: CDB) -> list[SniffRecord]:
    """The stand-in installation's synthetic capture (module docstring), in time order."""
    air = Air(cdb)
    air.beacon(0.0)
    # light: a client's acknowledged Set ON changes the state: no unicast reply, the status published twice
    air.send(0.5, CLIENT, LIGHT, M.generic_onoff_set(True, ack=True, tid=1), ttl=4)
    air.send(1.4, LIGHT, LIGHT_GROUP, onoff_status(True))
    air.send(3.5, LIGHT, LIGHT_GROUP, onoff_status(True))
    # ... the gateway's Set ON while it is on: a unicast reply 200 ms later, between the two publications
    air.send(6.0, GATEWAY, LIGHT, M.generic_onoff_set(True, ack=True, tid=7))
    air.send(6.2, LIGHT, GATEWAY, onoff_status(True))
    air.send(6.9, LIGHT, LIGHT_GROUP, onoff_status(True))
    air.send(8.1, LIGHT, LIGHT_GROUP, onoff_status(True))
    air.heartbeat(9.0, LIGHT, GATEWAY)
    # socket: the gateway's poll answered unicast, then the meter's three values, published once each
    air.send(10.0, GATEWAY, SOCKET, M.generic_onoff_get())
    air.send(10.1, SOCKET, GATEWAY, onoff_status(False))
    air.send(
        12.0,
        SOCKET_METER,
        METER_GROUP,
        sensor_status(SENSOR_POWER, (1234).to_bytes(2, "little")),
    )
    air.send(
        12.15,
        SOCKET_METER,
        METER_GROUP,
        sensor_status(SENSOR_CURRENT, (520).to_bytes(2, "little")),
    )
    air.send(
        12.3,
        SOCKET_METER,
        METER_GROUP,
        sensor_status(SENSOR_VOLTAGE, (3680).to_bytes(2, "little")),
    )
    air.send_config(
        14.0, 0x0001, LIGHT, encode_opcode(C.CONFIG_COMPOSITION_DATA_GET) + b"\x00"
    )
    # tunable white: the status published twice
    air.send(15.0, LIGHT_CTL, CTL_GROUP, ctl_status(30000, 4000))
    air.send(16.2, LIGHT_CTL, CTL_GROUP, ctl_status(30000, 4000))
    # rocker: a double press (counters 16, 17) published twice, then a hold
    air.send(20.0, ROCKER, CTL_GROUP, button_event(0x16, ROCKER_UP_CLICK))
    air.send(20.3, ROCKER, CTL_GROUP, button_event(0x17, ROCKER_UP_CLICK))
    air.send(20.9, ROCKER, CTL_GROUP, button_event(0x16, ROCKER_UP_CLICK))
    air.send(21.2, ROCKER, CTL_GROUP, button_event(0x17, ROCKER_UP_CLICK))
    air.send(24.0, ROCKER, CTL_GROUP, button_event(0x18, ROCKER_DOWN_HOLD))
    air.send(25.5, ROCKER, CTL_GROUP, button_event(0x19, BUTTON_HOLD_END))
    air.beacon(30.0)
    return sorted(air.records, key=lambda r: r.time)


def decoded(cdb: CDB, records: list[SniffRecord]) -> list[str]:
    """`records` as `mesh_sniff.py decode --json` writes them."""
    decoder = MeshDecoder(cdb)
    return [mesh_sniff.decoded_json(decoder.feed(r)) for r in records]


def explicit_map() -> dict[int, int]:
    """Every stand-in address of the scenario → its fixture address (the client is left to the pool)."""
    return {shifted(a): a for a in FIXTURE_ADDRESSES}


def build() -> str:
    """The text of `synthetic.ndjson`."""
    standin = CDB.from_network(standin_network())
    fixture = CDB.load(FIXTURE)
    lines = decoded(standin, capture(standin))
    conversion = convert(lines, standin, fixture, explicit_map())
    return "".join(line + "\n" for line in conversion.lines)


if __name__ == "__main__":
    TRACE.write_text(build(), encoding="utf-8")
