"""tools/privacy_scan.py: what it reports, what it lets through, and that it never prints what it found.

Every value the scanner must report is built at run time (from a digest, or joined from parts), so this file passes
the scan of the tree like any other; the literals left in it are the documentation-range and made-up values the
scanner lets through by design.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from tools import privacy_scan as S


def _join(sep: str, *parts: str) -> str:
    """`parts` joined at run time: the scan of this file never sees the value whole."""
    return sep.join(parts)


def _digest(label: str) -> str:
    """32 irregular hex digits, the same on every run (no made-up shape: no repeat, no even step)."""
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def _mac(label: str) -> str:
    """A MAC with a public-looking OUI and an irregular rest, outside every documentation range."""
    digits = "30fb10" + _digest(label)[:6]
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2)).upper()


def _uuid(label: str) -> str:
    d = _digest(label)
    return f"{d[:8]}-{d[8:12]}-4{d[13:16]}-a{d[17:20]}-{d[20:]}"


MAC = _mac("mac")
UUID = _uuid("uuid")
KEY = _digest("key")
# what is reported (and allowlisted) of a path under it: the home directory
HOME = _join("/", "", "home", "someone")
TILDE = _join("/", "~", "private", "x.json")
IP = _join(".", "192", "168", "7", "9")
SYNTHETIC = {"mac": MAC, "uuid": UUID, "hex128": KEY, "home-path": HOME, "ip": IP}


def write_allowlist(root: Path, text: str = "") -> S.Allowlist:
    path = root / S.ALLOWLIST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return S.Allowlist.load(path)


@pytest.fixture
def empty(tmp_path: Path) -> S.Allowlist:
    return write_allowlist(tmp_path)


def kinds(text: str, allow: S.Allowlist) -> list[str]:
    return [f.kind for f in S.scan_text(text, "f.md", allow)]


@pytest.mark.parametrize(("kind", "value"), sorted(SYNTHETIC.items()))
def test_synthetic_values_are_reported_by_kind(
    kind: str, value: str, empty: S.Allowlist
) -> None:
    assert kinds(f"before\nthe value `{value}` here\n", empty) == [kind]
    [finding] = S.scan_text(f"x\n\n{value}\n", "docs/a.md", empty)
    assert str(finding) == f"docs/a.md:3: {kind}"


@pytest.mark.parametrize(
    "text",
    [
        MAC.lower().replace(":", "-"),  # dash-separated, lower case
        KEY.upper(),
        "0x" + KEY,
        UUID.replace("-", ""),  # a UUID without its dashes is a 128-bit value
        TILDE,
        HOME.replace("/home/", "/Users/"),
        _join("\\", "C:", "Users", "someone", "x"),
        _join(".", "10", "0", "0", "1"),
        _join(".", "172", "16", "0", "1"),
        _join(".", "100", "64", "0", "1"),  # shared address space (carrier-grade NAT)
        _join(".", "93", "184", "216", "34"),  # a public address
    ],
)
def test_other_forms_are_reported(text: str, empty: S.Allowlist) -> None:
    assert kinds(text, empty)


@pytest.mark.parametrize(
    "text",
    [
        # documentation ranges, loopback, the unspecified and the broadcast address
        "00:00:5E:00:53:01 01-00-5e-90-10-ff 192.0.2.1 198.51.100.200 203.0.113.9 127.0.0.1 0.0.0.0 255.255.255.255",
        # made-up shapes
        "11:22:33:44:55:66 AA:BB:CC:DD:EE:07 dd:dd:dd:dd:dd:dd 30:FB:10:00:00:9E",
        "00112233445566778899aabbccddeeff ffeeddccbbaa99887766554433221100 000102030405060708090a0b0c0d0e0f",
        "0123456789abcdef0123456789abcdef 00000000000000000000000000000001",
        "00000001-0000-4000-8000-000000000001 11111111-2222-4333-8444-555555555555",
        # the Bluetooth Base UUID, a node UUID over a documentation MAC (dashed or not)
        "0000180a-0000-1000-8000-00805f9b34fb 00005EFF-FE00-5314-0000-000000000000 00005efffe0053140000000000000000",
        # version and section numbers, a quad that is no address (and so, the price of it, any address below 10.)
        "firmware 2.2.0.1, Mesh Profile 3.4.2.3, 300.1.2.3, 8.8.8.8",
        # paths that are not a home directory
        "de/jung/junghome/app/ui/home/devices /home/ alone ~/ /config/x.json %h/nrfsniff",
        # longer runs are not 128-bit values or MACs: a commit hash, a digest, a byte dump
        "3d3c42e5aac5ba805825da76410c181273ba90b1 " + hashlib.sha256(b"x").hexdigest(),
        "01:02:03:04:05:06:07:08",
    ],
)
def test_documentation_ranges_and_made_up_values_pass(
    text: str, empty: S.Allowlist
) -> None:
    assert kinds(text, empty) == []


def test_allowlisted_values_pass(tmp_path: Path) -> None:
    allow = write_allowlist(
        tmp_path,
        "# reviewed\n\n"
        + "\n".join(f"{kind} {value}  # why" for kind, value in SYNTHETIC.items())
        + f"\nhome-path {TILDE.rsplit('/', 1)[0]}\n",
    )
    text = "\n".join([*SYNTHETIC.values(), TILDE, MAC.lower().replace(":", "-")])
    assert kinds(text, allow) == []
    # the allowlisted MAC lets its node UUID (EUI-64) through too, dashed or not; another MAC's stays reported
    digits = MAC.replace(":", "").lower()
    node = f"{digits[:6]}fffe{digits[6:]}" + "0" * 16
    dashed = f"{node[:8]}-{node[8:12]}-{node[12:16]}-{node[16:20]}-{node[20:]}"
    assert kinds(f"{node} {dashed.upper()}", allow) == []
    other = _mac("other").replace(":", "").lower()
    assert kinds(f"{other[:6]}fffe{other[6:]}" + "0" * 16, allow) == ["hex128"]
    # an allowlisted UUID covers its plain form, an allowlisted 128-bit value its dashed form
    assert kinds(UUID.replace("-", ""), allow) == []
    assert (
        kinds(f"{KEY[:8]}-{KEY[8:12]}-{KEY[12:16]}-{KEY[16:20]}-{KEY[20:]}", allow)
        == []
    )


@pytest.mark.parametrize("line", ["mac", "colour 00:11:22:33:44:55", "mac   "])
def test_a_malformed_allowlist_line_is_an_error(tmp_path: Path, line: str) -> None:
    with pytest.raises(ValueError, match=r"privacy_allowlist\.txt:2: expected"):
        write_allowlist(tmp_path, f"# header\n{line}\n")


# scans every file of the tree: over 2 s of CPU alone, past the 5 s budget (`CALL_BUDGET`) under `-n auto` load —
# no real-clock wait, the work is the time
@pytest.mark.slow_ok
def test_the_tree_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    assert S.main([]) == 0
    assert capsys.readouterr().out == ""


def git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603  # fixed arguments, no shell
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (
        tmp_path / "repo"
    )  # tmp_path itself also holds Home Assistant's test config directory
    root.mkdir()
    git(root, "init", "-q")
    write_allowlist(root)
    (root / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    return root


def test_output_names_file_line_and_kind_never_the_value(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text(
        "\n".join(f"{kind}: {value}" for kind, value in SYNTHETIC.items()),
        encoding="utf-8",
    )
    (repo / "ignored.md").write_text(MAC, encoding="utf-8")  # ignored: never scanned
    (repo / "image.png").write_bytes(b"\x89PNG\0" + MAC.encode())  # binary: skipped
    (repo / "link.md").symlink_to(
        repo / "docs" / "a.md"
    )  # a symlink: its target is scanned once
    (repo / S.ALLOWLIST).write_text(f"mac {_mac('stale')}\n", encoding="utf-8")
    assert S.main(["--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[:-1] == [
        f"docs/a.md:{n}: {kind}" for n, kind in enumerate(SYNTHETIC, 1)
    ]
    assert out.splitlines()[-1].startswith("5 finding(s) in 1 file(s)")
    forms = {
        v
        for value in SYNTHETIC.values()
        for v in (value, value.lower(), value.upper(), value.replace(":", ""))
    }
    for form in forms:
        assert form not in out
    assert _mac("stale") not in out


def test_named_files_only(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "a.md").write_text(KEY, encoding="utf-8")
    (repo / "b.md").write_text(UUID, encoding="utf-8")
    assert S.main(["--root", str(repo), "b.md", "missing.md", S.ALLOWLIST]) == 1
    assert capsys.readouterr().out.splitlines()[0] == "b.md:1: uuid"


def test_tree_files_lists_tracked_and_untracked_but_not_ignored(repo: Path) -> None:
    (repo / "tracked.md").write_text("x", encoding="utf-8")
    git(repo, "add", "tracked.md")
    (repo / "new.md").write_text("x", encoding="utf-8")
    (repo / "ignored.md").write_text("x", encoding="utf-8")
    assert S.tree_files(repo) == sorted(
        [".gitignore", "new.md", "tracked.md", S.ALLOWLIST]
    )


def commit(repo: Path, message: str, email: str) -> None:
    git(
        repo,
        "-c",
        "user.name=Someone",
        "-c",
        f"user.email={email}",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )


def test_history_counts_trailers_and_personal_addresses_only(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    personal = "someone@example.org"
    commit(
        repo,
        "One\n\nCo-Authored-By: A Bot <noreply@example.com>",
        "1+someone@users.noreply.github.com",
    )
    assert S.main(["--root", str(repo), "--history"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"history: 0 commit(s) with {kind}"
        for kind in (
            "session-trailer",
            "author-email",
            "committer-email",
            "message-email",
        )
    ]
    trailer = "Claude-Session: https://example.invalid/s"
    commit(repo, f"Two\n\nwrites to {personal}, see icon@2x.png\n\n{trailer}", personal)
    assert S.main(["--root", str(repo), "--history"]) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "history: 1 commit(s) with session-trailer",
        "history: 1 commit(s) with author-email",
        "history: 1 commit(s) with committer-email",
        "history: 1 commit(s) with message-email",
    ]
    assert personal not in out
    assert "example" not in out
