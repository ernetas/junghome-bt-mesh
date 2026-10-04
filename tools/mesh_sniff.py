#!/usr/bin/env python3
"""Sniff the mesh from the outside with a Nordic nRF Sniffer for Bluetooth LE and decode it with the export's keys.

Two halves that may run on different machines (`docs/sniffer.md`):

    capture   next to the dongle — needs only Python 3, pyserial/psutil and Nordic's SnifferAPI (the extcap
              directory of the nRF Sniffer download); writes one JSON line per mesh AD structure, no keys involved
    decode    where the export lives — turns those lines (a file, or stdin for a live pipe) or a Nordic pcap into
              decrypted, described messages; relay copies are collapsed, segmented messages reassembled

    # live view from the machine that holds the export (the capture host never sees a key):
    ssh sniffhost '/path/to/venv/bin/python - capture --api /path/to/extcap --ndjson -' < tools/mesh_sniff.py \\
        | .venv/bin/python tools/mesh_sniff.py decode --export ~/Downloads/JungHome.json -

    # record on the sniffer host, decode later
    python3 mesh_sniff.py capture --api ~/nrfsniff/extcap --seconds 600 --ndjson cap.ndjson --pcap cap.pcap
    .venv/bin/python tools/mesh_sniff.py decode --export JungHome.json cap.ndjson --copies --json decoded.ndjson

`decode --src/--dst/--grep` filter what is printed (hex addresses; `--grep` matches the described text),
`--copies` also prints every relay copy (channel, RSSI, TTL), `--beacons` every beacon (our network's, and the
Unprovisioned Device beacons of devices waiting to be added) instead of only changes, `--foreign` the PDUs of other
networks. The summary at the end (stderr) counts records per kind and messages per source. `--json` is written
readable by the owner only and withholds the parameter bytes of anything that carries a key or a credential (Config
AppKey / NetKey Add and Update, every other device-key message the decoder does not know, the gateway API token
property): the described `text` is all it keeps of those, exactly like the terminal.

The capture half deliberately imports nothing from `jhmesh` so it can be streamed to the sniffer host over ssh as
shown above; `jhmesh.sniffer.SniffRecord` defines the line format both halves share.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_API = "~/nrfsniff/extcap"  # where the nRF Sniffer download is unpacked on the capture host
SNIFFER_BAUDRATE = 1_000_000
ADV_CHANNELS = (37, 38, 39)
MESH_AD_TYPES = {
    0x29: "pbadv",
    0x2A: "msg",
    0x2B: "beacon",
}  # = jhmesh.sniffer.AD_KINDS
PACKET_TYPE_ADVERTISING = 1  # SnifferAPI Types.PACKET_TYPE_ADVERTISING
PACKET_TYPE_DATA = 2  # a data PDU of the followed connection
LLID_CONTINUATION, LLID_START = (
    1,
    2,
)  # LL data PDU header: L2CAP continuation / start (or complete)
L2CAP_ATT_CID = 0x0004
FOLLOW_WAIT = 20.0  # seconds to wait for the device to advertise before following it
ADV_PDU_TYPES_WITH_DATA = (0x0, 0x2, 0x6)
BLE_ADDRESS_LEN = 6
POLL_INTERVAL = 0.05


# ----------------------------------------------------------------------------- capture (dongle side, key-free)


def ad_structures(data: bytes) -> Iterator[tuple[int, bytes]]:
    """(AD type, value) of the AD structures in `data` — the capture-side twin of `jhmesh.sniffer.ad_structures`."""
    i = 0
    while i + 1 < len(data):
        length = data[i]
        if length == 0 or i + 1 + length > len(data):
            return
        yield data[i + 1], data[i + 2 : i + 1 + length]
        i += 1 + length


def capture_records(packet: Any) -> list[dict[str, Any]]:
    """The mesh AD structures of one SnifferAPI packet as NDJSON documents (`SniffRecord.from_json` reads them)."""
    ble = packet.blePacket
    if not packet.OK or ble is None or ble.type != PACKET_TYPE_ADVERTISING:
        return []
    if ble.advType not in ADV_PDU_TYPES_WITH_DATA:
        return []
    payload = bytes(ble.payload)
    if len(payload) < BLE_ADDRESS_LEN:
        return []
    adv = ":".join(f"{b:02X}" for b in ble.advAddress[:BLE_ADDRESS_LEN])
    records = []
    for ad_type, value in ad_structures(payload[BLE_ADDRESS_LEN:]):
        if ad_type in MESH_AD_TYPES:
            records.append(
                {
                    "t": round(packet.time, 6),
                    "ch": packet.channel,
                    "rssi": packet.RSSI,
                    "adv": adv,
                    "kind": MESH_AD_TYPES[ad_type],
                    "pdu": value.hex(),
                    "ts_us": packet.timestamp,
                }
            )
    return records


class L2capReassembler:
    """Put the L2CAP fragments of one direction of a followed connection back together; yields ATT PDUs.

    A LL data PDU with LLID 2 starts (or completes) an L2CAP frame `[length u16][channel u16][payload]`, LLID 1
    continues it; LL control PDUs (LLID 3) are ignored. Only the ATT channel (0x0004) is returned.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._buf = bytearray()

    def feed(self, llid: int, data: bytes) -> bytes | None:
        """Add one LL data payload; return the ATT PDU when a frame on the ATT channel completed."""
        if llid == LLID_START:
            self._buf = bytearray(data)
        elif llid == LLID_CONTINUATION and self._buf:
            self._buf += data
        else:
            return None
        if len(self._buf) < 4:
            return None
        length = int.from_bytes(self._buf[:2], "little")
        cid = int.from_bytes(self._buf[2:4], "little")
        if len(self._buf) < 4 + length:
            return None
        frame = bytes(self._buf[4 : 4 + length])
        self._buf = bytearray()
        return frame if cid == L2CAP_ATT_CID else None


def capture_data_records(
    packet: Any, reassemblers: dict[str, L2capReassembler]
) -> list[dict[str, Any]]:
    """The ATT PDUs a data packet of a followed connection completes, as `gatt` records."""
    ble = packet.blePacket
    if not packet.OK or ble is None or ble.type != PACKET_TYPE_DATA:
        return []
    direction = "m2s" if packet.direction else "s2m"
    att = reassemblers.setdefault(direction, L2capReassembler()).feed(
        ble.llid, bytes(ble.payload)
    )
    if att is None:
        return []
    return [
        {
            "t": round(packet.time, 6),
            "ch": packet.channel,
            "rssi": packet.RSSI,
            "adv": ":".join(f"{b:02X}" for b in ble.accessAddress),
            "kind": "gatt",
            "pdu": att.hex(),
            "ts_us": packet.timestamp,
            "dir": direction,
        }
    ]


def parse_mac(text: str) -> list[int]:
    """A public MAC in print order (`AA:BB:CC:DD:EE:FF`) as its six octets; an argparse error for anything else."""
    parts = text.split(":")
    try:
        mac = [int(part, 16) for part in parts]
    except ValueError:
        mac = []
    if len(parts) != 6 or len(mac) != 6 or not all(0 <= b <= 0xFF for b in mac):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a MAC address (six hex octets, AA:BB:CC:DD:EE:FF)"
        )
    return mac


def format_mac(mac: list[int]) -> str:
    return ":".join(f"{b:02X}" for b in mac)


def follow_device(
    api: Any, sniffer: Any, mac: list[int], wait: float | None = None
) -> bool:
    """Tell the dongle to follow `mac` (a public MAC, `parse_mac`) into its connection once it has seen it advertise."""
    deadline = time.time() + (FOLLOW_WAIT if wait is None else wait)
    while time.time() < deadline:
        devices = sniffer.getDevices()
        for candidate in list(getattr(devices, "devices", [])):
            if list(candidate.address[:6]) == mac:
                sniffer.follow(candidate)
                return True
        time.sleep(0.5)
    device = api.Devices.Device(
        [*mac, 0], "", 0
    )  # not seen advertising yet: register it and follow anyway
    sniffer.addDevice(device)
    sniffer.follow(device)
    return False


class Dedupe:
    """Fold byte-identical PDUs seen within `window` seconds into one record with a count (`n`).

    Network-transmit repeats of one node are byte-identical; relay copies are not (the TTL changes the
    obfuscation and the ciphertext), so those stay separate and the decoder still sees every hop.
    """

    def __init__(self, window: float) -> None:
        """Remember PDUs for `window` seconds."""
        self.window = window
        self._pending: dict[str, dict[str, Any]] = {}

    def push(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Offer one record; returns the records that became final (their window closed)."""
        out = self.flush(record["t"] - self.window)
        known = self._pending.get(record["pdu"])
        if known is not None:
            known["n"] = known.get("n", 1) + 1
        else:
            self._pending[record["pdu"]] = record
        return out

    def flush(self, before: float | None = None) -> list[dict[str, Any]]:
        """Return (and forget) the records first seen before `before`; everything when None."""
        done = [r for r in self._pending.values() if before is None or r["t"] <= before]
        for r in done:
            del self._pending[r["pdu"]]
        return done


def _interrupt(_signum: int, _frame: Any) -> None:
    """SIGTERM handler: unwind through the capture loop's `finally` like a Ctrl-C."""
    raise KeyboardInterrupt


class _NoCaptureFile:
    """Stands in for SnifferAPI's `CaptureFileHandler`, which appends every packet the dongle reports to
    `/tmp/logs/capture.pcap` whatever the caller asked for (~3 GB a day on a busy mesh, in RAM where /tmp is a tmpfs).
    `--pcap` writes the capture's own pcap."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def writePacket(self, _packet: Any) -> None:  # noqa: N802  # SnifferAPI's name
        pass


def load_sniffer_api(api_dir: str) -> Any:
    """Import Nordic's SnifferAPI package from the extcap directory.

    Its serial-port lock file lives in /var/lock, which only root may write on some distributions (Arch); the
    dongle has one user here, so the lock is disabled. Its own capture file is disabled too (`_NoCaptureFile`).
    """
    sys.path.insert(0, str(Path(api_dir).expanduser()))
    try:
        import SnifferAPI  # type: ignore[import-not-found]  # noqa: PLC0415  # optional, only on the capture host
        from SnifferAPI import (  # noqa: PLC0415
            CaptureFiles,
            Devices,
            Filelock,
            Pcap,
            Sniffer,
        )
    except ImportError as err:
        raise SystemExit(
            f"SnifferAPI not found in {api_dir} (unpack the nRF Sniffer for Bluetooth LE there): {err}"
        ) from err
    Filelock.lock = lambda _port: None
    Filelock.unlock = lambda _port: None
    CaptureFiles.CaptureFileHandler = _NoCaptureFile
    SnifferAPI.Sniffer, SnifferAPI.Pcap, SnifferAPI.Devices = Sniffer, Pcap, Devices
    return SnifferAPI


def start_sniffer(api: Any, port: str, channels: list[int]) -> Any:
    """Open the dongle and put it into scan mode on `channels`."""
    sniffer = api.Sniffer.Sniffer(port, SNIFFER_BAUDRATE)
    sniffer.getFirmwareVersion()
    sniffer.getTimestamp()
    sniffer.start()
    sniffer.setAdvHopSequence(channels)
    sniffer.scan(False, False, False)
    return sniffer


def open_outputs(
    api: Any, args: argparse.Namespace
) -> tuple[IO[str] | None, IO[bytes] | None]:
    """Open the NDJSON (`-` = stdout) and pcap outputs the capture was asked for."""
    ndjson: IO[str] | None = None
    pcap: IO[bytes] | None = None
    if args.ndjson:
        ndjson = sys.stdout if args.ndjson == "-" else Path(args.ndjson).open("w")  # noqa: SIM115  # ciphertext only: the capture host holds no key
    if args.pcap:
        pcap = Path(args.pcap).open("wb")  # noqa: SIM115
        pcap.write(api.Pcap.get_global_header())
    return ndjson, pcap


def parse_channels(text: str) -> list[int]:
    """`--channels`: a comma-separated subset of the advertising channels 37, 38, 39."""
    channels = [int(c) for c in text.split(",")]
    if any(c not in ADV_CHANNELS for c in channels):
        raise SystemExit(
            f"--channels must be a subset of {','.join(map(str, ADV_CHANNELS))}"
        )
    return channels


def cmd_capture(args: argparse.Namespace) -> int:
    """Scan every advertising channel with the dongle and write the mesh AD structures it hears."""
    api = load_sniffer_api(args.api)
    channels = parse_channels(args.channels)
    dedupe = Dedupe(args.dedupe) if args.dedupe else None
    sniffer = start_sniffer(api, args.port, channels)
    # outputs after the dongle answered: a serial error must not leave an empty capture file behind on every
    # restart of the service
    ndjson, pcap = open_outputs(api, args)
    reassemblers: dict[str, L2capReassembler] = {}
    if args.follow:
        advertised = follow_device(api, sniffer, args.follow)
        print(
            f"following {format_mac(args.follow)} ({'seen advertising' if advertised else 'not seen yet'}) into its connection …",
            file=sys.stderr,
        )
    started = time.time()
    seen = written = 0
    by_kind: dict[str, int] = {}
    # `systemctl stop` sends SIGTERM, which would end the process without the `finally` below (the pending dedupe
    # window lost, the dongle left scanning): treat it like Ctrl-C
    previous_sigterm = signal.signal(signal.SIGTERM, _interrupt)

    def emit(records: Iterable[dict[str, Any]]) -> None:
        nonlocal written
        for record in records:
            written += 1
            by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
            if ndjson is not None:
                ndjson.write(json.dumps(record, separators=(",", ":")) + "\n")
        if ndjson is not None:
            ndjson.flush()

    print(f"capturing on channels {channels} from {args.port} …", file=sys.stderr)
    try:
        while not args.seconds or time.time() - started < args.seconds:
            for packet in sniffer.getPackets():
                if pcap is not None and packet.OK:
                    pcap.write(
                        api.Pcap.create_packet(
                            bytes([packet.boardId, *packet.getList()]), packet.time
                        )
                    )
                records = capture_records(packet)
                if args.follow:
                    records += capture_data_records(packet, reassemblers)
                for record in records:
                    seen += 1
                    if dedupe is None or record["kind"] == "gatt":
                        emit([record])
                    else:
                        emit(dedupe.push(record))
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if dedupe is not None:
            emit(dedupe.flush())
        sniffer.doExit()
        for stream in (pcap, ndjson):
            if stream is not None and stream not in (sys.stdout, sys.stdout.buffer):
                stream.close()
    elapsed = time.time() - started
    print(
        f"{elapsed:.0f}s: {seen} mesh AD structures, {written} records written {by_kind}",
        file=sys.stderr,
    )
    return 0


# ----------------------------------------------------------------------------- decode (export side)


def read_records(path: str) -> Iterator[Any]:
    """Records from an NDJSON file, `-` (stdin, line by line, for a live pipe) or a Nordic pcap."""
    from jhmesh.sniffer import (  # noqa: PLC0415  # the capture half must not need jhmesh
        SniffRecord,
        read_pcap,
    )

    if path == "-":
        for line in sys.stdin:
            if line.strip():
                yield SniffRecord.from_json(line)
        return
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        sys.exit(f"cannot read {path}: {err}")
    if data[:1] != b"{":
        yield from read_pcap(data)
        return
    for line in data.decode().splitlines():
        if line.strip():
            yield SniffRecord.from_json(line)


def decoded_json(decoded: Any) -> str:
    """One NDJSON line per decoded record (`--json`): the record, what it was and, for a message, its fields."""
    doc: dict[str, Any] = {
        "t": round(decoded.record.time, 6),
        "ch": decoded.record.channel,
        "rssi": decoded.record.rssi,
        "kind": decoded.kind,
        "text": decoded.text,
    }
    if decoded.net is not None:
        doc.update(
            src=f"{decoded.net.src:04X}",
            dst=f"{decoded.net.dst:04X}",
            ttl=decoded.net.ttl,
            seq=f"{decoded.net.seq:06X}",
        )
    if decoded.kind == "copy":
        doc["copies"] = decoded.copies
    if decoded.message is not None:
        m = decoded.message
        doc.update(
            key=m.key,
            opcode=f"{m.opcode:02X}"
            if m.company_id is None
            else f"{m.opcode:02X}:{m.company_id:04X}",
        )
        if withholds_params(m):
            doc.update(params=None, redacted=True)
        else:
            doc["params"] = m.params.hex()
    if decoded.beacon is not None:
        doc.update(
            iv_index=decoded.beacon.iv_index,
            key_refresh=decoded.beacon.key_refresh,
            iv_update=decoded.beacon.iv_update,
        )
    if decoded.device is not None:
        doc.update(uuid=decoded.device.uuid, oob=f"{decoded.device.oob_info:04X}")
    return json.dumps(doc, ensure_ascii=False)


# LBC vendor property messages: Gets `[pid]`, Statuses / Admin Sets `[pid][access][value]`, other Sets `[pid][value]`
# (docs/android/vendor-models.md §3.3) — the property id leads in every one of them.
VENDOR_PROPERTY_OPCODES = frozenset(
    {0x02, 0x03, 0x04, 0x05, 0x08, 0x09, 0x0A, 0x0B, 0x0E, 0x0F, 0x10, 0x11}
)


def withholds_params(m: Any) -> bool:
    """Whether `--json` must not carry a message's parameter bytes: they hold a key or a credential.

    `text` (from `describe`) already hides these; the raw `params` field next to it undid that. Withheld: every
    device-key message that carries a key (Config AppKey / NetKey Add and Update) or that the decoder does not
    know (an unknown Config message under a device key may be a key refresh in another form), and every property
    message — SIG Generic Property or LBC vendor — whose property is a secret (`PropertySpec.secret`: the
    gateway API token 0xC001).
    """
    # decode-side imports: the capture half must not need jhmesh
    from jhmesh import config_messages as C  # noqa: PLC0415
    from jhmesh import messages as M  # noqa: PLC0415
    from jhmesh import properties as P  # noqa: PLC0415

    if m.key.startswith("dev:"):
        return (
            m.company_id is not None
            or m.opcode in C.KEY_CARRYING_OPCODES
            or m.opcode not in C.CONFIG_NAMES
        )
    is_property_message = (
        m.company_id == M.JUNG_CID and m.opcode in VENDOR_PROPERTY_OPCODES
    ) or (m.company_id is None and m.opcode in sig_property_opcodes())
    if not is_property_message or len(m.params) < 2:
        return False
    pid = int.from_bytes(m.params[:2], "little")
    spec = P.PROPERTIES.get(pid) or P.SIG_PROPERTIES.get(pid)
    return spec is not None and spec.secret


@functools.cache
def sig_property_opcodes() -> frozenset[int]:
    """The SIG Generic Property opcodes (Admin / Manufacturer / User Get, Set, Set Unacknowledged, Status)."""
    # decode-side import: the capture half must not need jhmesh
    from jhmesh import messages as M  # noqa: PLC0415

    return frozenset(
        op
        for name in dir(M)
        if name.startswith(("GEN_ADMIN_PROP_", "GEN_MANU_PROP_", "GEN_USER_PROP_"))
        for op in [getattr(M, name)]
        if isinstance(op, int)
    )


def open_private(path: Path) -> IO[str]:
    """Open `path` for writing readable by the owner only (0600), whatever the umask; an existing file is tightened."""
    f = os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "w",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return f


# repeated every few seconds: printed when they change (`--beacons`: all of them)
BEACON_KINDS = ("beacon", "unprovisioned")


def wants(decoded: Any, args: argparse.Namespace, last_beacon: str | None) -> bool:  # noqa: PLR0911  # one verdict per record kind
    """The `decode` print filter; `last_beacon` is the text of the last record of the same beacon kind."""
    kind = decoded.kind
    if kind == "copy":
        return bool(args.copies) and _route_matches(decoded, args)
    if kind in BEACON_KINDS:
        return bool(args.beacons) or decoded.text != last_beacon
    if kind in ("foreign", "pbadv"):
        return bool(args.foreign)
    if kind == "gatt":
        return bool(args.gatt)
    if kind in ("access", "control", "segment", "undecryptable", "malformed", "proxy"):
        if not _route_matches(decoded, args):
            return False
        return not args.grep or args.grep.lower() in decoded.text.lower()
    return True


def _route_matches(decoded: Any, args: argparse.Namespace) -> bool:
    net = decoded.net
    if net is None:
        return not (args.src or args.dst)
    return (args.src is None or net.src == args.src) and (
        args.dst is None or net.dst == args.dst
    )


def format_line(decoded: Any) -> str:
    """`HH:MM:SS.mmm ch37 -39  <text>`; copies are indented under their message."""
    when = datetime.fromtimestamp(decoded.record.time)  # noqa: DTZ006  # the capture host's local clock, like `listen`
    stamp = when.strftime("%H:%M:%S.") + f"{when.microsecond // 1000:03d}"
    head = f"{stamp} ch{decoded.record.channel} {decoded.record.rssi:4d}"
    if decoded.kind == "copy" and decoded.net is not None:
        return f"{head}   ↳ copy {decoded.copies} ttl={decoded.net.ttl}"
    return f"{head}  {decoded.text}"


def cmd_decode(args: argparse.Namespace) -> int:
    """Decrypt and print a capture."""
    from jhmesh.cdb import CDB  # noqa: PLC0415  # the capture half must not need jhmesh
    from jhmesh.sniffer import MeshDecoder  # noqa: PLC0415

    try:
        cdb = CDB.load(Path(args.export))
    except (OSError, ValueError) as err:
        sys.exit(f"cannot read {args.export}: {err}")
    decoder = MeshDecoder(cdb, args.iv)
    out = (
        open_private(Path(args.json)) if args.json else None
    )  # decrypted traffic: never world-readable
    # per beacon kind: a device waiting to be provisioned must not hide a change of our network's beacon
    last_beacon: dict[str, str] = {}
    try:
        for record in read_records(args.input):
            decoded = decoder.feed(record)
            if out is not None:
                out.write(decoded_json(decoded) + "\n")
            if wants(decoded, args, last_beacon.get(decoded.kind)):
                print(format_line(decoded), flush=True)
            if decoded.kind in BEACON_KINDS:
                last_beacon[decoded.kind] = decoded.text
    except KeyboardInterrupt:
        pass
    finally:
        if out is not None:
            out.close()
    print_summary(decoder, cdb)
    return 0


def print_summary(decoder: Any, cdb: Any) -> None:
    """Counts per kind and the busiest sources (stderr)."""
    kinds = ", ".join(f"{k}={v}" for k, v in sorted(decoder.stats.items()))
    print(f"records: {kinds}; iv_index={decoder.iv_index}", file=sys.stderr)
    for src, count in decoder.sources.most_common(12):
        print(f"  {count:5d}  {cdb.label(src)}", file=sys.stderr)


# ----------------------------------------------------------------------------- CLI


def parse_hex(text: str) -> int:
    """A hex address with or without `0x`."""
    try:
        return int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a hex address") from None


CAPTURE_OPTIONS: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (
        ("--port",),
        {
            "default": DEFAULT_PORT,
            "help": f"sniffer serial port (default {DEFAULT_PORT})",
        },
    ),
    (
        ("--api",),
        {
            "default": DEFAULT_API,
            "help": f"directory holding Nordic's SnifferAPI (default {DEFAULT_API})",
        },
    ),
    (
        ("--channels",),
        {
            "default": "37,38,39",
            "help": "advertising channel hop sequence (default 37,38,39)",
        },
    ),
    (
        ("--seconds",),
        {
            "type": float,
            "default": 0,
            "help": "stop after this long (default: run until Ctrl-C)",
        },
    ),
    (("--ndjson",), {"help": "write records here (`-` = stdout, for a pipe)"}),
    (
        ("--pcap",),
        {"help": "also write every good BLE packet as a Nordic BLE pcap (Wireshark)"},
    ),
    (
        ("--dedupe",),
        {
            "type": float,
            "default": 0,
            "help": "fold byte-identical PDUs within this many seconds into one record",
        },
    ),
    (
        ("--follow",),
        {
            "metavar": "MAC",
            "type": parse_mac,
            "help": "follow this node (its public MAC) into its GATT connection and record the ATT traffic"
            " (proxy PDUs)",
        },
    ),
)
DECODE_OPTIONS: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("--export",), {"required": True, "help": "MeshNetwork.json or JungHome.json"}),
    (("input",), {"help": "NDJSON or pcap file, or `-` for stdin"}),
    (("--src",), {"type": parse_hex, "help": "only messages from this address"}),
    (("--dst",), {"type": parse_hex, "help": "only messages to this address"}),
    (("--grep",), {"help": "only messages whose description contains this text"}),
    (
        ("--copies",),
        {"action": "store_true", "help": "also print every relay / retransmit copy"},
    ),
    (
        ("--beacons",),
        {"action": "store_true", "help": "print every beacon, not only changes"},
    ),
    (
        ("--foreign",),
        {
            "action": "store_true",
            "help": "print PDUs of other networks and PB-ADV traffic",
        },
    ),
    (
        ("--gatt",),
        {
            "action": "store_true",
            "help": "print every ATT PDU of a followed connection, not only the mesh ones",
        },
    ),
    (
        ("--iv",),
        {
            "type": int,
            "help": "IV index to start from (default: the export's, then the beacons)",
        },
    ),
    (("--json",), {"help": "also write every decoded record as NDJSON here"}),
)
# the sub-commands: name (`cmd_<name>` runs it), help, its arguments in `--help` order
COMMANDS = (
    (
        "capture",
        "scan with the dongle, write mesh AD structures as NDJSON / pcap",
        CAPTURE_OPTIONS,
    ),
    (
        "decode",
        "decrypt a capture (NDJSON, pcap or `-`) with the keys of an export",
        DECODE_OPTIONS,
    ),
)


def build_parser() -> argparse.ArgumentParser:
    """The sub-commands of `COMMANDS`."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_text, options in COMMANDS:
        p = sub.add_parser(name, help=help_text)
        for flags, kwargs in options:
            p.add_argument(*flags, **kwargs)
        p.set_defaults(fn=globals()[f"cmd_{name}"])
    return ap


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    if args.cmd == "decode":
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        return int(args.fn(args))
    except BrokenPipeError:  # `| head`: leave quietly
        sys.stderr.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
