#!/usr/bin/env python3
"""Fail on anything in the tree that could identify the maintainer, the installation or a home network.

    tools/privacy_scan.py              # every file git tracks or would track (untracked but not ignored)
    tools/privacy_scan.py FILE ...     # these files only (the pre-commit hook)
    tools/privacy_scan.py --history    # the commit history: session trailers and personal e-mail addresses

What it looks for, by *kind*:

    mac        a six-octet MAC address (`:` or `-` between the octets)
    uuid       a UUID in its dashed form
    ip         a dotted IPv4 address
    hex128     32 hex digits in a row (a key, or a UUID without its dashes)
    home-path  an absolute home directory (`/home/<name>`, `/Users/<name>`, `C:\\Users\\<name>`) or a `~/<name>` path

Let through without asking: the documentation ranges (MACs `00:00:5E:00:53:xx` and `01:00:5E:90:10:xx`, RFC 7042;
IPv4 `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, RFC 5737), loopback, `0.0.0.0` and `255.255.255.255`,
UUIDs on the Bluetooth Base UUID (the SIG's 16- and 32-bit UUIDs), a node UUID (EUI-64) whose MAC is let through,
and *made-up* values whose shape says so: octets or hex digits that repeat or step evenly (`11:22:33:44:55:66`,
`00112233…`, `0123456789abcdef…`), half the digits one digit (`…-0000-000000000001`), at most six distinct digits
in a 128-bit value, a vendor block with a counter (`<OUI>:00:00:xx`) or a numbered placeholder
(`AA:BB:CC:DD:EE:xx`). A dotted quad whose first number is below 10 is not reported: in this tree those are firmware
versions and specification section numbers, never an address (`10.x` is private and is reported). Everything else
must be in `tools/privacy_allowlist.txt` (documented pseudonyms, Mesh Profile sample data, vendor GATT UUIDs, the
fixtures' fixed values), which is not scanned itself.

The output names the file, the line and the kind of every finding, never the value: a CI log is public too.
`--history` prints counts per kind only. It looks at `git log` (author, committer, message), which no change to the
tree can fix; it is a check to run by hand before publishing, not part of CI.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = "tools/privacy_allowlist.txt"

_HEX = "0-9A-Fa-f"
PATTERNS = {
    # six octets, one separator throughout; not part of a longer colon/dash run (a byte dump, an IPv6 address)
    "mac": re.compile(
        rf"(?<![{_HEX}])(?<![{_HEX}][:-])[{_HEX}]{{2}}([:-])[{_HEX}]{{2}}(?:\1[{_HEX}]{{2}}){{4}}"
        rf"(?![{_HEX}])(?![:-][{_HEX}])"
    ),
    "uuid": re.compile(
        rf"(?<![{_HEX}-])[{_HEX}]{{8}}-[{_HEX}]{{4}}-[{_HEX}]{{4}}-[{_HEX}]{{4}}-[{_HEX}]{{12}}(?![{_HEX}-])"
    ),
    "ip": re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)"),
    # not inside a longer run of letters and digits (a 40-digit commit hash, a 64-digit digest, base64)
    "hex128": re.compile(r"(?<![0-9A-Za-z])(?:0x)?([0-9A-Fa-f]{32})(?![0-9A-Za-z])"),
    "home-path": re.compile(
        r"(?<![\w.~/\\-])(?:/home/|/Users/|[A-Za-z]:\\+Users\\+)[\w.-]+|(?<![\w/~])~/[\w.-]+"
    ),
}
KINDS = tuple(PATTERNS)
# the domain starts with a letter: `icon@2x.png` is a file name
EMAIL = re.compile(r"[\w.+-]+@[A-Za-z][\w-]*(?:\.[\w-]+)+")
NOREPLY = re.compile(r"(?i)^(?:no-?reply@|.*@users\.noreply\.github\.com$)")
TRAILER = re.compile(r"(?m)^Claude-Session:")

_DOCUMENTATION_MACS = ("00:00:5E:00:53:", "01:00:5E:90:10:")
_DOCUMENTATION_NETS = tuple(
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8")
)
# 0.0.0.0 and 255.255.255.255: the unspecified and the broadcast address, nobody's
_ANY_ADDRESS = (ipaddress.IPv4Address(0), ipaddress.IPv4Address(2**32 - 1))
_BASE_UUID = re.compile(r"[0-9a-f]{8}-0000-1000-8000-00805f9b34fb")
# a node UUID: the EUI-64 of its MAC (FFFE in the middle), zeros behind; dashed or not
_EUI64 = re.compile(r"([0-9a-f]{6})ff-?fe([0-9a-f]{2})-?([0-9a-f]{4})-?0000-?0{12}")


@dataclass(frozen=True)
class Finding:
    """One match the allowlist does not cover: where, and of which kind (never the value)."""

    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def _steps_evenly(values: list[int], modulus: int) -> bool:
    """Every value is the one before it plus the same step (a repeat is a step of 0)."""
    return len({(b - a) % modulus for a, b in pairwise(values)}) <= 1


def _even(digits: str) -> bool:
    """The octets or the hex digits of `digits` repeat or step evenly: `112233…`, `000102…`, `0123…`, `aaaa…`."""
    octets = list(bytes.fromhex(digits))
    return _steps_evenly(octets, 256) or _steps_evenly([int(d, 16) for d in digits], 16)


def made_up(digits: str) -> bool:
    """`digits` (hex, no separators: a MAC's 12 or a UUID's / key's 32) is a placeholder by its shape."""
    digits = digits.lower()
    if len(digits) == 12:
        # a MAC: all of it even, a numbered placeholder, or a counter under a vendor block
        return _even(digits) or _even(digits[:10]) or digits[6:10] == "0000"
    counts = Counter(digits)
    return (
        _even(digits)
        or len(counts) <= 6
        or counts.most_common(1)[0][1] * 2 >= len(digits)
    )


@dataclass(frozen=True)
class Allowlist:
    """The reviewed values by kind, normalised (MACs upper case with colons, hex lower case)."""

    values: dict[str, frozenset[str]]

    @classmethod
    def load(cls, path: Path) -> Allowlist:
        """Read `<kind> <value>` lines; `#` starts a comment; an unknown kind is an error, not a silent no-op."""
        found: dict[str, set[str]] = {kind: set() for kind in KINDS}
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.partition("#")[0].strip()
            if not line:
                continue
            kind, _, value = line.partition(" ")
            if kind not in found or not value.strip():
                raise ValueError(
                    f"{path.name}:{number}: expected `<kind> <value>` with a kind of {KINDS}"
                )
            found[kind].add(normalise(kind, value.strip()))
        return cls({kind: frozenset(values) for kind, values in found.items()})

    def __contains__(self, item: tuple[str, str]) -> bool:
        kind, value = item
        return normalise(kind, value) in self.values.get(kind, frozenset())


def normalise(kind: str, value: str) -> str:
    """The form values of `kind` are compared in."""
    if kind == "mac":
        return value.upper().replace("-", ":")
    if kind in {"uuid", "hex128"}:
        return value.lower()
    return value


def _mac_allowed(mac: str, allow: Allowlist) -> bool:
    mac = normalise("mac", mac)
    return (
        mac.startswith(_DOCUMENTATION_MACS)
        or made_up(mac.replace(":", ""))
        or ("mac", mac) in allow
    )


def _uuid_allowed(digits: str, allow: Allowlist) -> bool:
    """A 128-bit value, dashed or not: a node UUID is judged by its MAC, anything else by shape or the allowlist."""
    digits = digits.lower()
    plain = digits.replace("-", "")
    dashed = f"{plain[:8]}-{plain[8:12]}-{plain[12:16]}-{plain[16:20]}-{plain[20:]}"
    if (node := _EUI64.fullmatch(digits)) is not None:
        mac = node[1] + node[2] + node[3]
        return _mac_allowed(":".join(mac[i : i + 2] for i in range(0, 12, 2)), allow)
    return (
        _BASE_UUID.fullmatch(dashed) is not None
        or made_up(plain)
        or ("uuid", dashed) in allow
        or ("hex128", plain) in allow
    )


def _ip_allowed(text: str, allow: Allowlist) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:  # 300.1.2.3: a version number, not an address
        return True
    return (
        int(text.partition(".")[0]) < 10
        or address in _ANY_ADDRESS
        or any(address in net for net in _DOCUMENTATION_NETS)
        or ("ip", text) in allow
    )


def allowed(kind: str, match: re.Match[str], allow: Allowlist) -> bool:
    """Whether the match of `kind` is let through (see the module docstring)."""
    text = match[0]
    if kind == "mac":
        return _mac_allowed(text, allow)
    if kind == "uuid":
        return _uuid_allowed(text, allow)
    if kind == "hex128":
        return _uuid_allowed(match[1], allow)
    if kind == "ip":
        return _ip_allowed(text, allow)
    return (kind, text) in allow  # home-path


def scan_text(text: str, path: str, allow: Allowlist) -> list[Finding]:
    """Every finding in `text`, reported as lines of `path`."""
    findings = {
        Finding(path, text.count("\n", 0, match.start()) + 1, kind)
        for kind, pattern in PATTERNS.items()
        for match in pattern.finditer(text)
        if not allowed(kind, match, allow)
    }
    return sorted(findings, key=lambda f: (f.path, f.line, KINDS.index(f.kind)))


def tree_files(root: Path) -> list[str]:
    """What git tracks plus what it would track (untracked and not ignored), relative to `root`."""
    listed = subprocess.run(  # fixed arguments, no shell
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode()
    return sorted({name for name in listed.split("\0") if name})


def scan_files(root: Path, names: list[str], allow: Allowlist) -> list[Finding]:
    """Scan `names` (relative to `root`): text files only, and never the allowlist itself."""
    findings = []
    for name in names:
        path = root / name
        if (
            Path(name).as_posix() == ALLOWLIST
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        data = path.read_bytes()
        if b"\0" in data:  # binary: an image, an archive
            continue
        findings += scan_text(
            data.decode("utf-8", errors="replace"), Path(name).as_posix(), allow
        )
    return findings


def scan_history(root: Path) -> Counter[str]:
    """Count, per kind, the commits of `git log` that carry a session trailer or a personal e-mail address."""
    log = subprocess.run(  # fixed arguments, no shell
        ["git", "log", "--format=%ae%x1f%ce%x1f%B%x1e"],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8", errors="replace")
    counts: Counter[str] = Counter()
    for commit in filter(str.strip, log.split("\x1e")):
        author, committer, message = commit.strip("\n").split("\x1f", 2)
        counts["session-trailer"] += TRAILER.search(message) is not None
        counts["author-email"] += _personal(author)
        counts["committer-email"] += _personal(committer)
        counts["message-email"] += any(_personal(m[0]) for m in EMAIL.finditer(message))
    return counts


def _personal(address: str) -> bool:
    return bool(address) and NOREPLY.match(address) is None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail on identifying values in the tree (or, with --history, in the commit history)."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="files to scan, relative to the repository (default: the tree)",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="count session trailers and personal e-mail addresses in `git log` instead",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.root
    if args.history:
        counts = scan_history(root)
        for kind in (
            "session-trailer",
            "author-email",
            "committer-email",
            "message-email",
        ):
            print(f"history: {counts[kind]} commit(s) with {kind}")
        return 1 if sum(counts.values()) else 0
    allow = Allowlist.load(root / ALLOWLIST)
    findings = scan_files(root, args.paths or tree_files(root), allow)
    for finding in findings:
        print(finding)
    if findings:
        files = len({f.path for f in findings})
        print(
            f"{len(findings)} finding(s) in {files} file(s): replace the value with a documentation-range, "
            f"placeholder or pseudonymous one, or add a reviewed value to {ALLOWLIST}"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
