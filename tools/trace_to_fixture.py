#!/usr/bin/env python3
"""Turn a decoded capture of an installation into a trace of the fixture network that the tests replay.

Run it where the real export lives (the capture host, `docs/sniffer.md`): only its output may leave that machine,
and only after a review by hand (`docs/dev/testing.md`, *Replayed traces*).

    .venv/bin/python tools/mesh_sniff.py decode --export JungHome.json cap.ndjson --json decoded.ndjson
    .venv/bin/python tools/trace_to_fixture.py decoded.ndjson --export JungHome.json \\
        --map 0148=0148 --map C061=C061 --keep 0148 --keep C061 -o tests/traces/light.ndjson

Input: the NDJSON `mesh_sniff.py decode --json` writes. Kept: the complete access messages under an AppKey to or
from a unicast or group address. Dropped, and counted on stderr: relay copies, segments, control messages, beacons,
everything under a device key (Config messages carry device keys and addresses), withheld parameters, virtual
destinations, and every message whose parameters hold the little-endian bytes of an address the input uses (a
parameter is never rewritten, so such a message would carry an address of the installation).

Output: one JSON object per message, in capture order,

    {"delay": 0.25, "src": "0148", "dst": "C061", "ttl": 5, "access": "820401", "pdus": ["…"]}

`delay` is the gap to the previous message in seconds, rounded to 10 ms (no absolute time is kept), `access` the
access PDU (opcode and parameters), `pdus` the same message re-encrypted as network PDUs under the keys and IV index
of the fixture network (`--fixture`, by default the suite's `tests/fixtures/MeshNetwork.json`), one per segment,
with sequence numbers counted per source from 1. RSSI, channel, sequence numbers, the description text, the
advertising MACs and the capture's clock are all dropped.

Addresses: `--map IN=OUT` (hex) maps an address to the fixture address it should become (the light of the capture
to the fixture's light, its group to the light's group). Every other unicast or group address gets the next free
address of a pool (unicast from 7000, group from C800; neither an input nor a fixture address), assigned in the
order of the input addresses, so the same set of addresses always maps the same way whatever order the capture
heard them in. The fixed group addresses (FF00 and above) and the unassigned address stay as they are.

Before anything is written the output is checked, and nothing is written (exit status 2) when it holds any key of
the export (its NetKeys and every key derived from them, the Network ID, AppKeys, device keys) in hex, Base64 or
a Python list or bytes form, a mesh or node UUID of the export, a node MAC of the export or an advertising MAC of
the input, or an address that is not a mapping target, or that is an input address not explicitly mapped to
itself. The error names what was found, never its value.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jhmesh.advert import mac_from_uuid
from jhmesh.cdb import CDB, InvalidExport
from jhmesh.pdu import (
    UNSEGMENTED_UPPER_MAX,
    encode_opcode,
    is_group,
    is_unicast,
    lower_segments_access,
    lower_unsegmented_access,
    network_encrypt,
    upper_encrypt_app,
)

FIXTURE_EXPORT = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "MeshNetwork.json"
)
UNICAST_POOL = 0x7000  # where unmapped unicast addresses go: above every fixture node
GROUP_POOL = (
    0xC800  # ... and unmapped groups: above the fixture's element and room groups
)
FIXED_GROUPS = 0xFF00  # all-proxies, all-friends, all-relays, all-nodes and the RFU block: the same everywhere
GAP_RESOLUTION = 0.01  # seconds a delay is rounded to
FIELDS = ("delay", "src", "dst", "ttl", "access", "pdus")
_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")


class LeakError(Exception):
    """The output would hold something of the installation; the message names what (never its value)."""


@dataclass
class Message:
    """One kept access message of the capture, in input addresses."""

    time: float
    src: int
    dst: int
    ttl: int
    opcode: bytes
    params: bytes

    @property
    def access(self) -> bytes:
        """The access PDU: opcode and parameters."""
        return self.opcode + self.params


@dataclass
class Conversion:
    """What `convert` produced: the output lines, the address mapping it used and what it dropped, by reason."""

    lines: list[str]
    mapping: dict[int, int]
    dropped: Counter[str] = field(default_factory=Counter)


def parse_map(text: str) -> tuple[int, int]:
    """`--map IN=OUT`: two hex addresses."""
    try:
        left, right = text.split("=")
        return int(left, 16), int(right, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not IN=OUT (two hex addresses)"
        ) from None


def parse_hex(text: str) -> int:
    """A hex address, for `--keep`."""
    try:
        return int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a hex address") from None


def encodings(secret: bytes) -> dict[str, str]:
    """`secret` written out every way the leak check looks for, by the name of the encoding."""
    b64 = base64.b64encode(secret).decode()
    return {
        "hex": secret.hex(),
        "HEX": secret.hex().upper(),
        "base64": b64.rstrip("="),
        "base64url": base64.urlsafe_b64encode(secret).decode().rstrip("="),
        "list(bytes)": str(list(secret)),
        "repr(bytes)": repr(secret),
    }


def secrets_of(cdb: CDB) -> dict[str, bytes]:
    """Every key of `cdb` by name (NetKeys with what derives from them, the Network ID, AppKeys, device keys)."""
    found: dict[str, bytes] = {}
    for index, nk in cdb.net_keys.items():
        found |= {
            f"NetKey {index}": nk.key,
            f"NetKey {index} encryption key": nk.enc_key,
            f"NetKey {index} privacy key": nk.priv_key,
            f"NetKey {index} identity key": nk.identity_key,
            f"NetKey {index} beacon key": nk.beacon_key,
            f"NetKey {index} private beacon key": nk.private_beacon_key,
            f"Network ID {index}": nk.network_id,
        }
    for index, (old, _phase) in cdb.net_key_refresh.items():
        found[f"old NetKey {index}"] = old.key
    for index, ak in cdb.app_keys.items():
        found[f"AppKey {index}"] = ak.key
    for node in (*cdb.nodes, *cdb.excluded_nodes):
        found[f"device key of {node.unicast:04X}"] = node.dev_key
    # a key of one repeated byte (a placeholder) is runs of `0` or `[0, 0, …]`, which padding produces as well
    return {name: key for name, key in found.items() if len(set(key)) > 1}


def identities_of(cdb: CDB) -> dict[str, str]:
    """The mesh UUID, every node UUID and every node MAC of `cdb`, by name; hex digits only, upper case."""
    found = {"mesh UUID": cdb.mesh_uuid}
    for node in (*cdb.nodes, *cdb.excluded_nodes):
        found[f"UUID of node {node.unicast:04X}"] = node.uuid
        if (mac := mac_from_uuid(node.uuid)) is not None:
            found[f"MAC of node {node.unicast:04X}"] = mac
    return {name: re.sub(r"[^0-9A-Fa-f]", "", v).upper() for name, v in found.items()}


def mac_needles(mac: str) -> list[str]:
    """A MAC (12 hex digits) as text could hold it: plain, with colons or dashes, either byte order (BLE is LE)."""
    octets = [mac[i : i + 2] for i in range(0, 12, 2)]
    forms = []
    for order in (octets, octets[::-1]):
        forms += ["".join(order), ":".join(order), "-".join(order)]
    return forms


@dataclass
class Mapper:
    """Input address → fixture address: the explicit pairs, then a pool address per other address (`assign`)."""

    explicit: dict[int, int]
    reserved: set[int]
    mapping: dict[int, int] = field(default_factory=dict)

    def assign(self, addresses: set[int]) -> dict[int, int]:
        """Map every one of `addresses` (sorted, so the result does not depend on the order they were heard in)."""
        self.mapping = {}
        taken = self.reserved | set(self.explicit.values()) | addresses
        next_free = {"unicast": UNICAST_POOL, "group": GROUP_POOL}
        for address in sorted(addresses):
            if address in self.explicit:
                self.mapping[address] = self.explicit[address]
            elif address == 0 or address >= FIXED_GROUPS:
                self.mapping[address] = address
            else:
                pool = "unicast" if is_unicast(address) else "group"
                while next_free[pool] in taken:
                    next_free[pool] += 1
                self.mapping[address] = next_free[pool]
                taken.add(next_free[pool])
        return self.mapping


def read_messages(lines: list[str], dropped: Counter[str]) -> list[Message]:
    """The access messages under an AppKey of a `decode --json` capture; everything else counted in `dropped`."""
    kept = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
            kind = doc["kind"]
        except (ValueError, TypeError, KeyError) as err:
            raise ValueError(
                f"line {number} is not a decoded record (`mesh_sniff.py decode --json`)"
            ) from err
        if kind != "access":
            dropped[kind] += 1
            continue
        if not str(doc.get("key", "")).startswith("app"):
            dropped["device key"] += 1
            continue
        if doc.get("params") is None:
            dropped["withheld parameters"] += 1
            continue
        opcode, _, company = str(doc["opcode"]).partition(":")
        kept.append(
            Message(
                time=float(doc["t"]),
                src=int(doc["src"], 16),
                dst=int(doc["dst"], 16),
                ttl=int(doc["ttl"]),
                opcode=encode_opcode(
                    int(opcode, 16), int(company, 16) if company else None
                ),
                params=bytes.fromhex(doc["params"]),
            )
        )
    return kept


def holds_address(params: bytes, addresses: set[int]) -> bool:
    """Whether `params` holds the little-endian bytes of one of `addresses` anywhere."""
    return any(a.to_bytes(2, "little") in params for a in addresses)


def encrypt(
    fixture: CDB, src: int, dst: int, ttl: int, access: bytes, seq: int
) -> tuple[list[bytes], int]:
    """`access` as the network PDUs node `src` of the fixture network sends it with; the next free sequence number."""
    nk, ak, iv = fixture.net_keys[0], fixture.app_keys[0], fixture.iv_index
    upper = upper_encrypt_app(ak, iv, seq, src, dst, access)
    if len(upper) <= UNSEGMENTED_UPPER_MAX:
        lowers = [lower_unsegmented_access(ak.aid, upper)]
    else:
        lowers = lower_segments_access(ak.aid, seq, upper)
    pdus = [
        network_encrypt(nk, iv, False, ttl, seq + i, src, dst, lower)
        for i, lower in enumerate(lowers)
    ]
    return pdus, seq + len(lowers)


def installation_addresses(real: CDB, messages: list[Message]) -> set[int]:
    """Every address of the installation: the export's elements and groups and every address the capture used.

    Unassigned and the fixed groups (FF00 and above) are the same in every network, so they are none of them.
    """
    addresses = {e.address for n in real.nodes for e in n.elements} | set(real.groups)
    addresses |= {a for m in messages for a in (m.src, m.dst)}
    return {a for a in addresses if 0 < a < FIXED_GROUPS}


def convert(
    lines: list[str],
    real: CDB,
    fixture: CDB,
    explicit: dict[int, int],
    keep: set[int] | None = None,
) -> Conversion:
    """Convert a decoded capture (`lines`) taken on the network of `real` into a trace of `fixture`.

    LeakError when the result would hold a key, identity, MAC or address of the input (`check`).
    """
    dropped: Counter[str] = Counter()
    messages = read_messages(lines, dropped)
    if keep:
        before = len(messages)
        messages = [m for m in messages if m.src in keep or m.dst in keep]
        dropped["not kept"] += before - len(messages)
    installation = installation_addresses(real, messages)
    # an address mapped to itself on purpose is the maintainer's decision, not a leak
    sensitive = {a for a in installation if explicit.get(a) != a}
    clean = []
    for m in messages:
        if not (is_unicast(m.dst) or is_group(m.dst) or m.dst == 0):
            dropped["virtual destination"] += 1
        elif holds_address(m.params, sensitive):
            dropped["address in the parameters"] += 1
        else:
            clean.append(m)
    reserved = {e.address for n in fixture.nodes for e in n.elements}
    mapping = Mapper(explicit, reserved | set(fixture.groups) | installation).assign(
        {a for m in clean for a in (m.src, m.dst)}
    )
    out: list[str] = []
    seq: dict[int, int] = {}
    previous: float | None = None
    for m in clean:
        src, dst = mapping[m.src], mapping[m.dst]
        pdus, seq[src] = encrypt(fixture, src, dst, m.ttl, m.access, seq.get(src, 1))
        gap = 0.0 if previous is None else max(0.0, m.time - previous)
        previous = m.time
        doc = {
            "delay": round(round(gap / GAP_RESOLUTION) * GAP_RESOLUTION, 2),
            "src": f"{src:04X}",
            "dst": f"{dst:04X}",
            "ttl": m.ttl,
            "access": m.access.hex(),
            "pdus": [p.hex() for p in pdus],
        }
        out.append(json.dumps(doc, separators=(", ", ": ")))
    conversion = Conversion(out, mapping, dropped)
    check(conversion, real, sensitive, mac_inputs(lines))
    return conversion


def mac_inputs(lines: list[str]) -> set[str]:
    """Every MAC-looking string of the input (the `adv` field of a capture record, or anywhere else in a line)."""
    return {
        re.sub(r"[^0-9A-Fa-f]", "", m).upper() for m in _MAC.findall("\n".join(lines))
    }


def check(
    conversion: Conversion, real: CDB, sensitive: set[int], macs: set[str]
) -> None:
    """LeakError naming the things of the installation `conversion` holds (never their values).

    `sensitive`: the installation's addresses but those mapped to themselves on purpose.
    """
    text = "\n".join(conversion.lines)
    upper = text.upper()
    found = [
        f"{name} as {encoding}"
        for name, key in secrets_of(real).items()
        for encoding, needle in encodings(key).items()
        if needle in text or json.dumps(needle)[1:-1] in text
    ]
    identities = identities_of(real) | {
        f"input MAC {i}": mac for i, mac in enumerate(sorted(macs))
    }
    for name, value in identities.items():
        forms = mac_needles(value) if len(value) == 12 else [value]
        if any(form in upper.replace("-", "") or form in upper for form in forms):
            found.append(name)
    targets = set(conversion.mapping.values())
    for line in conversion.lines:
        doc = json.loads(line)
        for key in ("src", "dst"):
            address = int(doc[key], 16)
            if address not in targets:
                found.append(f"{key} {doc[key]}, which is no mapping target")
            elif address in sensitive:
                found.append(f"{key} {doc[key]}, an address of the installation")
    if found:
        raise LeakError(
            f"the output would hold {len(found)} thing(s) of the installation: "
            + ", ".join(sorted(set(found))[:8])
        )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input", help="NDJSON from `mesh_sniff.py decode --json`")
    ap.add_argument(
        "--export",
        required=True,
        help="the export the capture was decoded with: its keys, UUIDs and MACs must not reach the output",
    )
    ap.add_argument(
        "--fixture",
        default=str(FIXTURE_EXPORT),
        help="the network the trace is re-encrypted for (default: the suite's MeshNetwork.json)",
    )
    ap.add_argument(
        "--map",
        action="append",
        type=parse_map,
        default=[],
        metavar="IN=OUT",
        help="map input address IN to fixture address OUT (hex; repeatable)",
    )
    ap.add_argument(
        "--keep",
        action="append",
        type=parse_hex,
        default=[],
        metavar="ADDR",
        help="keep only messages from or to these input addresses (hex; repeatable)",
    )
    ap.add_argument("-o", "--output", help="write the trace here (default: stdout)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        real = CDB.load(Path(args.export))
        fixture = CDB.load(Path(args.fixture))
        lines = Path(args.input).read_text(encoding="utf-8").splitlines()
    except (OSError, InvalidExport) as err:
        print(f"cannot read the input: {err}", file=sys.stderr)
        return 1
    try:
        conversion = convert(
            lines, real, fixture, dict(args.map), set(args.keep) or None
        )
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 1
    except LeakError as err:
        print(f"nothing written: {err}", file=sys.stderr)
        return 2
    text = "".join(line + "\n" for line in conversion.lines)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    dropped = ", ".join(f"{k}={v}" for k, v in sorted(conversion.dropped.items()))
    print(
        f"{len(conversion.lines)} messages; dropped: {dropped or 'nothing'}",
        file=sys.stderr,
    )
    for real_address, fixture_address in sorted(conversion.mapping.items()):
        print(f"  {real_address:04X} -> {fixture_address:04X}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
