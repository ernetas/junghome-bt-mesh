"""tools/parity.py: the committed parity ledger is closed, and the checker, air and apk commands on tiny fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools import parity

ROOT = Path(__file__).resolve().parent.parent
JADX = ROOT / "android" / "jadx-out"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def dump(path: Path, doc: Any) -> Path:
    return write(path, json.dumps(doc))


def problems(root: Path, area: str | None = None) -> list[str]:
    return [str(p) for p in parity.check(root)[1] if area is None or p.area == area]


# ----------------------------------------------------------------------------- the committed files


@pytest.fixture(scope="module")
def committed() -> list[parity.Problem]:
    return parity.check(ROOT)[1]


def test_committed_ledger_is_closed(committed: list[parity.Problem]) -> None:
    assert [str(p) for p in committed if p.area == "ledger"] == []


def test_committed_anchors_json_is_consistent(committed: list[parity.Problem]) -> None:
    assert [str(p) for p in committed if p.area == "anchors"] == []


def test_committed_anchor_coverage(committed: list[parity.Problem]) -> None:
    assert [str(p) for p in committed if p.area == "coverage"] == []
    assert parity.read_json(ROOT / parity.PARITY / parity.UNMAPPED) == {}


@pytest.mark.skipif(
    not (JADX / "sources").is_dir(), reason="the decompiled APK is not checked in"
)
def test_committed_anchors_match_the_decompiled_apk() -> None:
    found = parity.extract_anchors(JADX)
    anchors = parity.read_json(ROOT / parity.PARITY / parity.ANCHORS)["anchors"]
    assert sorted(set(found) ^ set(anchors)) == []


# ----------------------------------------------------------------------------- check: a tiny closed ledger

TESTS_PY = """\
def test_a():
    pass


async def test_b():
    pass


class TestC:
    def test_d(self):
        pass
"""


MOD_PY = """\
\"\"\"A module.\"\"\"

a = 1
b = 2


def f():
    x = 1
    return x
"""

DATA_JSON = """\
{
  "config": {
    "title": "x"
  }
}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One domain, one implemented row with real citations, one build row, every anchor covered."""
    write(tmp_path / "src" / "mod.py", MOD_PY)
    write(tmp_path / "src" / "data.json", DATA_JSON)
    write(tmp_path / "src" / "notes.txt", "a\n")
    write(tmp_path / "tests" / "test_mod.py", TESTS_PY)
    base = tmp_path / parity.PARITY
    dump(
        base / "inventory-msg.json",
        {
            "domain": "msg",
            "apk": "APP 1.0",
            "items": [{"id": "msg:op:8201"}, {"id": "msg:op:8202"}],
        },
    )
    set_rows(
        tmp_path,
        [
            {
                "id": "msg:op:8201",
                "status": "implemented",
                "code": ["src/mod.py::b"],
                "tests": [
                    "tests/test_mod.py::test_a",
                    "tests/test_mod.py::test_b",
                    "tests/test_mod.py::TestC::test_d[param]",
                ],
            },
            {
                "id": "msg:op:8202",
                "status": "gap",
                "class": "build",
                "verify": "offline",
                "missing": "everything",
            },
        ],
    )
    dump(
        base / parity.ANCHORS,
        {
            "apk": "APP 1.0",
            "rules": [
                {"match": "string:abc_*", "id": None, "why": "not a feature: library"},
                {"match": "opcode:82*", "id": "msg:op:8202", "why": "family"},
            ],
            "anchors": {
                "opcode:8201": "msg:op:8201",
                "opcode:8202": ["msg:op:8202"],
                "opcode:8203": None,
                "string:abc_up": None,
            },
        },
    )
    return tmp_path


def set_rows(root: Path, rows: list[dict[str, Any]], domain: str = "msg") -> None:
    dump(root / parity.PARITY / f"ledger-{domain}.json", {"rows": rows})


def test_a_closed_ledger_has_no_problems(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert problems(repo) == []
    assert parity.main(["check", "--root", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "msg: 2 items, 2 rows; gap 1, implemented 1; build 1" in out
    assert "0 problems" in out
    assert "build msg" not in out


def test_check_lists_build_rows_by_domain_and_verify(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert parity.main(["check", "--root", str(repo), "--build"]) == 0
    assert (
        "build msg / offline (1):\n  msg:op:8202: everything" in capsys.readouterr().out
    )


NOT_A_CITATION = "is not a repository-relative path::symbol or path:line::symbol"

GOOD = {
    "id": "msg:op:8202",
    "status": "gap",
    "class": "build",
    "verify": "on-air",
    "missing": "x",
}


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({}, ["no status", "not implemented but no class"]),
        (
            {"status": "done", "class": "internal", "reason": "r"},
            [
                "status 'done' is not one of implemented, partial, gap, na",
                "class internal needs status na",
            ],
        ),
        (
            {"status": "gap", "class": "weird", "missing": "x"},
            ["class 'weird' is not one of build, ha-native, internal, declined, dup"],
        ),
        (
            {"status": "gap", "class": "dup", "dup_of": "msg:op:8202", "missing": "x"},
            ["class dup needs status na"],
        ),
        (
            {"status": "na", "class": "build", "reason": "r"},
            [
                "class build needs status gap or partial",
                "build row needs verify (offline, on-air, absent-hardware)",
            ],
        ),
        (
            {"status": "partial", "class": "build", "verify": "offline"},
            ["partial row without missing"],
        ),
        ({"status": "na", "class": "ha-native"}, ["na row without reason"]),
        (
            {"status": "na", "class": "dup", "reason": "r", "dup_of": "msg:op:nope"},
            ["dup_of 'msg:op:nope' is not another existing id"],
        ),
        (
            {"status": "na", "class": "dup", "reason": "r", "dup_of": "msg:op:8201"},
            ["dup_of 'msg:op:8201' is not another existing id"],
        ),
        (
            {"status": "implemented"},
            ["implemented without code", "implemented without tests"],
        ),
        (
            {
                "status": "implemented",
                "code": [
                    "src/mod.py",
                    "src/mod.py:3",
                    "/etc/passwd::a",
                    "../outside.py::a",
                    "src/../src/mod.py::a",
                    "src/none.py::a",
                    "src/notes.txt::a",
                    "src/mod.py::nope",
                    "src/mod.py::f.x",
                    "src/mod.py::x",
                    "src/mod.py:30::f",
                    "src/mod.py:1::f",
                    "src/data.json::title",
                ],
                "tests": [
                    "tests/test_mod.py",
                    "src/mod.py::test_a",
                    "tests/none.py::test_a",
                    "tests/test_mod.py::test_zzz",
                    "tests/test_mod.py::TestC::test_zzz",
                    "tests/test_mod.py::TestC",
                ],
            },
            [
                f"code 'src/mod.py' {NOT_A_CITATION}",
                f"code 'src/mod.py:3' {NOT_A_CITATION}",
                f"code '/etc/passwd::a' {NOT_A_CITATION}",
                f"code '../outside.py::a' {NOT_A_CITATION}",
                f"code 'src/../src/mod.py::a' {NOT_A_CITATION}",
                "code src/none.py::a: no such file",
                "code src/notes.txt::a: only .py and .json files can be cited by symbol",
                "code src/mod.py::nope: src/mod.py defines no nope",
                "code src/mod.py::f.x: src/mod.py defines no f.x",
                "code src/mod.py::x: src/mod.py defines no x",
                "code src/mod.py:30::f: line 30 is not within f (lines 7-9)",
                "code src/mod.py:1::f: line 1 is not within f (lines 7-9)",
                "code src/data.json::title: src/data.json defines no title",
                "test 'tests/test_mod.py' is not tests/file.py::name or tests/file.py::Class::name",
                "test 'src/mod.py::test_a' is not tests/file.py::name or tests/file.py::Class::name",
                "test tests/none.py::test_a: no such file",
                "test tests/test_mod.py::test_zzz: no such test",
                "test tests/test_mod.py::TestC::test_zzz: no such test",
                "test tests/test_mod.py::TestC: no such test",
            ],
        ),
    ],
)
def test_each_row_problem(repo: Path, row: dict[str, Any], expected: list[str]) -> None:
    set_rows(repo, [{"id": "msg:op:8201", **row}, GOOD])
    assert problems(repo) == [
        f"[ledger] ledger-msg.json msg:op:8201: {e}" for e in expected
    ]


def test_symbol_citations_that_resolve(repo: Path) -> None:
    """A symbol, a line inside it or a few lines above it (drifted, but still at the symbol), a JSON key path."""
    row = {
        "id": "msg:op:8201",
        "status": "implemented",
        "code": [
            "src/mod.py::a",
            "src/mod.py::f",
            "src/mod.py:9::f",
            f"src/mod.py:{7 - parity.LINE_SLACK}::f",
            "src/mod.py:1::a",
            "src/data.json::config.title",
            "src/data.json:2::config",
        ],
        "tests": ["tests/test_mod.py::test_a"],
    }
    set_rows(repo, [row, GOOD])
    assert problems(repo) == []


SYMBOLS_PY = """\
from typing import overload

CONST: int = 1
x, (y, *z) = 1, (2, 3)
total = 0
total += 1
type Alias = int
if CONST:
    FLAG = True
else:
    FLAG = False
try:
    import json
except ImportError:
    FALLBACK = None
finally:
    DONE = True
match CONST:
    case 1:
        MATCHED = 1
for _ in ():
    LOOPED = 1
with open(__file__) as fh:
    OPENED = 1


@overload
def g(v: int) -> int: ...
@overload
def g(v: str) -> str: ...
def g(v):
    local = v

    def inner():
        pass

    if v:
        class Local:
            attr = 1
    return local


class C:
    attr = 1
    other: int

    @property
    def p(self):
        return self.attr

    async def m(self):
        pass

    class Inner:
        deep = 2


D = {}
D["k"] = 1
"""


def test_python_symbols() -> None:
    spans = parity.python_symbols(SYMBOLS_PY)
    expected = (
        "CONST x y z total Alias FLAG FALLBACK DONE MATCHED LOOPED OPENED "
        "g g.inner g.Local g.Local.attr C C.attr C.other C.p C.m C.Inner C.Inner.deep D"
    )
    assert sorted(spans) == sorted(expected.split())
    assert spans["total"] == [(5, 5), (6, 6)]
    assert spans["FLAG"] == [(9, 9), (11, 11)]
    # each overload from its decorator
    assert spans["g"] == [(27, 28), (29, 30), (31, 40)]
    assert spans["C.p"] == [(47, 49)]
    assert parity.innermost_symbol(spans, 49) == "C.p"
    assert parity.innermost_symbol(spans, 46) == "C"  # between methods: the class
    assert parity.innermost_symbol(spans, 39) == "g.Local.attr"
    assert parity.innermost_symbol(spans, 1) is None


def test_json_symbols() -> None:
    text = '{\n  "a": {\n    "b": 1,\n    "c": [\n      {"d": 2}\n    ]\n  },\n  "e": "f"\n}\n'
    spans = parity.json_symbols(text)
    assert spans == {"a": [(2, 7)], "a.b": [(3, 3)], "a.c": [(4, 7)], "e": [(8, 9)]}
    assert parity.innermost_symbol(spans, 5) == "a.c"


def test_ids_must_agree_between_inventory_and_ledger(repo: Path) -> None:
    dump(
        repo / parity.PARITY / "inventory-msg.json",
        {
            "items": [
                {"id": "msg:op:8201"},
                {"id": "msg:op:8201"},
                {"id": "msg:op:8203"},
            ]
        },
    )
    row = {"id": "msg:op:8201", "status": "implemented", "code": ["src/mod.py::a"]}
    row["tests"] = ["tests/test_mod.py::test_a"]
    set_rows(repo, [row, row, GOOD])
    assert problems(repo, "ledger") == [
        "[ledger] inventory-msg.json: duplicate id msg:op:8201",
        "[ledger] ledger-msg.json: duplicate row msg:op:8201",
        "[ledger] ledger-msg.json: no row for msg:op:8203",
        "[ledger] ledger-msg.json: row msg:op:8202 is not an inventory id",
    ]


def test_unpaired_and_unreadable_files(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo / parity.PARITY
    dump(base / "inventory-ui.json", {"items": [{"id": "ui:screen:x"}]})
    dump(base / "ledger-net.json", {"rows": []})
    write(base / "inventory-prop.json", "{")
    write(base / "ledger-prop.json", "{")
    assert problems(repo, "ledger") == [
        "[ledger] inventory-net.json: no inventory file for this domain",
        "[ledger] inventory-prop.json: not JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
        "[ledger] ledger-prop.json: not JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
        "[ledger] ledger-ui.json: no ledger file for this domain",
    ]
    # an inventory whose ledger is missing still contributes ids (to anchors.json and dup_of)
    assert "ui:screen:x" in parity.load_parity(repo).ids
    assert parity.main(["check", "--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "ui: 1 items, 0 rows; \n" in out
    assert "\n4 problems, ledger 4\n" in out
    assert "\n  [ledger] ledger-ui.json: no ledger file for this domain\n" in out


# ----------------------------------------------------------------------------- check: anchors.json


def set_anchors(
    root: Path, anchors: dict[str, Any], rules: list[dict[str, Any]] | None = None
) -> None:
    dump(
        root / parity.PARITY / parity.ANCHORS,
        {"apk": "APP 1.0", "rules": rules or [], "anchors": anchors},
    )


def set_unmapped(root: Path, doc: dict[str, Any]) -> None:
    dump(root / parity.PARITY / parity.UNMAPPED, doc)


def test_anchors_json_must_exist(repo: Path) -> None:
    (repo / parity.PARITY / parity.ANCHORS).unlink()
    assert problems(repo) == ["[anchors] anchors.json: missing"]


def test_anchors_json_structure(repo: Path) -> None:
    set_anchors(
        repo,
        {
            "opcode:8201": 5,
            "opcode:8202": [1],
            "opcode:8203": "msg:op:nope",
            "opcode:8204": ["msg:op:8201", "msg:op:gone"],
        },
        [
            {"match": "", "why": ""},
            {"match": "string:*", "id": "msg:op:nope", "why": "w"},
        ],
    )
    assert problems(repo) == [
        "[anchors] anchors.json rules[0] '': no match glob",
        "[anchors] anchors.json rules[0] '': no why",
        "[anchors] anchors.json rules[1] 'string:*': id 'msg:op:nope' is not an inventory id",
        "[anchors] opcode:8201: maps to something other than an id, a list of ids or null",
        "[anchors] opcode:8202: maps to something other than an id, a list of ids or null",
        "[anchors] opcode:8203: maps to msg:op:nope, not an inventory id",
        "[anchors] opcode:8204: maps to msg:op:gone, not an inventory id",
        "[coverage] opcode:8201: no inventory item covers it, and unmapped-anchors.json does not list it",
        "[coverage] opcode:8202: no inventory item covers it, and unmapped-anchors.json does not list it",
    ]


def test_unmapped_anchors_are_coverage_problems(repo: Path) -> None:
    set_anchors(repo, {"string:a": None, "string:b": None, "string:c": "msg:op:8201"})
    assert problems(repo) == [
        "[coverage] string:a: no inventory item covers it, and unmapped-anchors.json does not list it",
        "[coverage] string:b: no inventory item covers it, and unmapped-anchors.json does not list it",
    ]
    set_unmapped(
        repo,
        {
            "ui": [
                {"anchor": "string:a", "evidence": "x:1", "suggested_id": "ui:msg:a"},
                {"anchor": "string:b", "evidence": "", "suggested_id": "ui:msg:b"},
            ],
            "bogus": [
                {"anchor": "string:c", "evidence": "x:1", "suggested_id": "ui:msg:c"}
            ],
        },
    )
    assert problems(repo) == [
        "[anchors] unmapped-anchors.json string:b: needs evidence and suggested_id",
        "[anchors] unmapped-anchors.json: domain 'bogus' is not one of msg, prop, prod, net, mgmt, ui",
        "[anchors] unmapped-anchors.json string:c: listed, but anchors.json has no such unmapped anchor",
        "[coverage] string:a: no inventory item covers it (listed under ui)",
        "[coverage] string:b: no inventory item covers it (listed under ui)",
    ]


def test_resolve_anchor() -> None:
    rules: list[dict[str, Any]] = [
        {"match": "string:abc_*", "id": None, "why": "library"},
        {"match": "string:*", "id": "ui:msg:x", "why": "family"},
    ]
    anchors = {"string:mapped": ["ui:a", "ui:b"], "string:abc_x": None}
    assert parity.resolve_anchor("string:mapped", anchors, rules) == ["ui:a", "ui:b"]
    assert parity.resolve_anchor("string:abc_x", anchors, rules) == []
    assert parity.resolve_anchor("string:other", anchors, rules) == ["ui:msg:x"]
    assert parity.resolve_anchor("opcode:01", anchors, rules) is None
    assert parity.targets({"no": "dict"}) is None


# ----------------------------------------------------------------------------- air


@pytest.mark.parametrize(
    ("kind", "text", "item"),
    [
        ("beacon", "beacon iv=5 flags=- auth=ok", "air:beacon:beacon"),
        ("copy", "copy 2 of 0001→C000 seq=000001", "air:copy:copy"),
        (
            "control",
            "0001→0002 ttl=5 seq=00000A Heartbeat 0a1b",
            "air:control:heartbeat",
        ),
        (
            "control",
            "0001→0002 ttl=5 seq=00000A Segment Ack seq_zero=3 block=00000001",
            "air:control:segment-ack",
        ),
        ("control", "0001→0002 ttl=5 seq=00000A control op 05 abcd", "air:control:op"),
        (
            "segment",
            "0001→0002 ttl=5 seq=00000A segment 1/2 (SeqAuth 00000A)",
            "air:segment:segment",
        ),
        ("foreign", "foreign beacon a0ffee", "air:foreign:beacon"),
        (
            "foreign",
            "[gatt →node] foreign network PDU (NID 12)",
            "air:foreign:network-pdu",
        ),
        ("pbadv", "PB-ADV c5abcd", "air:pbadv:pb-adv"),
        ("gatt", "ATT Write Request 0011", "air:gatt:att-write-request"),
        ("undecryptable", "", "air:undecryptable:undecryptable"),
    ],
)
def test_air_ids_of_non_access_records(kind: str, text: str, item: str) -> None:
    assert parity.air_key({"kind": kind, "text": text}, frozenset()) == (
        kind,
        None,
        None,
        item,
    )


@pytest.mark.parametrize(
    ("record", "key"),
    [
        (
            {
                "opcode": "02:0527",
                "text": "8001→0010 ttl=5 seq=000001 LBC Admin Property Get prop 0x000F name",
                "params": None,
            },
            ("02:0527", "0x000f", "air:access:02-0527:0x000f"),
        ),
        (
            {"opcode": "52", "text": "Sensor Status prop=0x0052 raw=0a le=10"},
            ("52", "0x0052", "air:access:52:0x0052"),
        ),
        (
            {"opcode": "05:0527", "text": "?? 0e1000", "params": "0e1000"},
            ("05:0527", "0x100e", "air:access:05-0527:0x100e"),
        ),
        (
            {"opcode": "05:0527", "text": "?? <3 bytes>", "params": None},
            ("05:0527", None, "air:access:05-0527"),
        ),
        (
            {"opcode": "8201", "text": "Generic OnOff Get", "params": "0e10"},
            ("8201", None, "air:access:8201"),
        ),
    ],
)
def test_air_ids_of_access_messages(
    record: dict[str, Any], key: tuple[str, str | None, str]
) -> None:
    assert parity.air_key({"kind": "access", **record}, frozenset({"05-0527"})) == (
        "access",
        *key,
    )


@pytest.fixture
def air_repo(tmp_path: Path) -> Path:
    items = ["air:access:8201", "air:access:05-0527:0x000e", "air:beacon:beacon"]
    dump(
        tmp_path / parity.PARITY / "inventory-air.json",
        {"items": [{"id": i} for i in items]},
    )
    return tmp_path


def ndjson(path: Path, *records: Any) -> str:
    write(path, "\n".join(r if isinstance(r, str) else json.dumps(r) for r in records))
    return str(path)


def test_air_reports_what_the_inventory_lacks(
    air_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    covered = ndjson(
        air_repo / "ok.ndjson",
        {"kind": "access", "opcode": "8201", "text": "Generic OnOff Get"},
        "",
        {"kind": "access", "opcode": "05:0527", "text": "?", "params": "0e0001"},
        {"kind": "beacon", "text": "beacon iv=1 flags=- auth=ok"},
    )
    assert parity.main(["air", "--root", str(air_repo), covered]) == 0
    assert capsys.readouterr().out == ""
    new = ndjson(
        air_repo / "new.ndjson",
        {"kind": "access", "opcode": "8202", "text": "Generic OnOff Set ON tid=1"},
        {"kind": "access", "opcode": "8202", "text": "Generic OnOff Set OFF tid=2"},
        {"kind": "access", "opcode": "05:0527", "text": "LBC prop 0x1007 access=3"},
        {"kind": "control", "text": "0001→0002 ttl=5 seq=00000A Heartbeat 0a"},
    )
    assert parity.main(["air", "--root", str(air_repo), covered, new]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "access\t05:0527\t0x1007\tair:access:05-0527:0x1007\t1",
        "access\t8202\t-\tair:access:8202\t2",
        "control\t-\t-\tair:control:heartbeat\t1",
    ]
    assert "7 records, 6 distinct, 3 not in inventory-air.json" in captured.err


def test_air_fails_on_lines_that_are_not_decoded_records(
    air_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = ndjson(
        air_repo / "bad.ndjson",
        "{",
        "[1]",
        {"kind": "access", "opcode": "05:0527", "params": "zzzz"},
    )
    assert parity.main(["air", "--root", str(air_repo), bad]) == 1
    err = capsys.readouterr().err
    assert f"{bad}:1: not a decoded record" in err
    assert f"{bad}:2: not a decoded record" in err
    assert f"{bad}:3: not a decoded record" in err


# ----------------------------------------------------------------------------- apk: extraction

PROPERTY_KT = """\
package p056e8;

import no.nordicsemi.android.mesh.utils.MeshAddress;
import org.spongycastle.crypto.tls.CipherSuite;

/* JADX INFO: compiled from: Property.kt */
public abstract class r {

    public static abstract class b extends r {

        public static final class a extends b {
            public a() {
                super(16);
            }
        }

        public static final class e extends b {
            public e() {
                super(CipherSuite.TLS_X);
            }
        }
    }

    public static abstract class c extends r {

        public static final class A extends c {
            public A() {
                super(4097);
            }
        }

        public static final class Z extends c {
            public static final Z f1 = new Z(MeshAddress.START_GROUP_ADDRESS);

            public Z(int i10) {
                super(i10);
            }
        }

        public static abstract class T extends r {

            public static final class a extends T {
                public final int d() {
                    return 40962;
                }
            }
        }
    }

    public static final class d extends r {
        public d() {
            super(7);
        }
    }

    public static abstract class q extends r {

        public static final class x extends q {
            public x() {
                super(8);
            }
        }
"""

JAVA = {
    # the Nordic library: opcode tables, message classes and the sensor property enum
    "no/nordicsemi/android/mesh/opcodes/ApplicationMessageOpCodes.java": """\
public class ApplicationMessageOpCodes {
    public static final int GENERIC_ON_OFF_GET = 33281;
    public static final int TIME_GET = 0x8237;
}
""",
    "no/nordicsemi/android/mesh/opcodes/ProxyConfigMessageOpCodes.java": "    public static final int SET_FILTER_TYPE = 0;\n",
    "no/nordicsemi/android/mesh/opcodes/TransportLayerOpCodes.java": "    public static final int SAR_ACK_OPCODE = 0;\n",
    "no/nordicsemi/android/mesh/opcodes/Readme.java": "    public static final int NOT_AN_OPCODE = 5;\n",
    "no/nordicsemi/android/mesh/transport/TimeGet.java": """\
import no.nordicsemi.android.mesh.opcodes.ApplicationMessageOpCodes;

public class TimeGet extends ApplicationMessage {
    public int getOpCode() {
        return ApplicationMessageOpCodes.TIME_GET;
    }
}
""",
    "no/nordicsemi/android/mesh/transport/ConfigAppKeyAdd.java": "public int getOpCode() {\n    return 0;\n}\n",
    "no/nordicsemi/android/mesh/transport/ProxyConfigAddAddressToFilter.java": """\
public class ProxyConfigAddAddressToFilter extends ProxyConfigMessage {
    public int getOpCode() {
        return 1;
    }
}
""",
    "no/nordicsemi/android/mesh/transport/VendorModelMessageAcked.java": "public int getOpCode() {\n    return this.mOpCode;\n}\n",
    "no/nordicsemi/android/mesh/transport/Gone.java": """\
import no.nordicsemi.android.mesh.opcodes.Missing;
import no.nordicsemi.android.mesh.opcodes.ApplicationMessageOpCodes;

public int getOpCode() {
    return Missing.X;
}
public int getOpCode() {
    return ApplicationMessageOpCodes.NOPE;
}
""",
    "no/nordicsemi/android/mesh/utils/MeshAddress.java": "    public static final int START_GROUP_ADDRESS = 49152;\n",
    "no/nordicsemi/android/mesh/sensorutils/DeviceProperty.java": """\
import org.spongycastle.crypto.tls.CipherSuite;

public enum DeviceProperty {
    PRESENT_AMBIENT_LIGHT_LEVEL(78),
    TOTAL_DEVICE_POWER_ON_TIME(CipherSuite.TLS_X),
    OTHER(i);
    static DeviceProperty self = DeviceProperty.PRESENT_AMBIENT_LIGHT_LEVEL;
}
""",
    "org/spongycastle/crypto/tls/CipherSuite.java": "    public static final int TLS_X = 109;\n",
    # the app: vendor opcode ints, property builders, the Property sealed class
    "G7/a.java": "public a() {\n    super(337666, 12920581);\n    int x = 0x0527D6, y = 123456;\n}\n",
    "p234v7/S.java": "return new GenericPropertyGet(33323, applicationKey, (short) r.b.a.f1.f2);\n",
    "p056e8/r.java": PROPERTY_KT,
    "zz/Other.java": "/* JADX INFO: compiled from: Property.kt */\npublic abstract class o {\n    super(99);\n}\n",
    "F7/b.java": """\
int a = DeviceProperty.PRESENT_AMBIENT_LIGHT_LEVEL.getPropertyId();
int b = DeviceProperty.TOTAL_DEVICE_POWER_ON_TIME.getPropertyId();
int c = DeviceProperty.NOT_THERE.getPropertyId();
int d = DeviceProperty.OTHER.getPropertyId();
""",
    # class names
    "de/jung/junghome/domain/interactors/scene/ActivateScene.java": "package x;\n\npublic final class ActivateScene {\n}\n",
    "de/jung/junghome/domain/interactors/scene/SceneKt.java": "package x;\n",
    "de/jung/junghome/domain/interactors/scene/a.java": "class a {}\n",
    "de/jung/junghome/domain/interactors/scene/ActivateScene$work$1.java": "class x {}\n",
    "de/jung/junghome/app/ui/scenes/ScenesFragment.java": """\
package x;

public final class ScenesFragment {
    int t = R.string.scenes_title;
    int d = R.string.scenes_title_description;
}
""",
    "de/jung/junghome/app/ui/scenes/ScenesViewModel.java": "public class ScenesViewModel {}\n",
    "de/jung/junghome/app/ui/scenes/SceneActivity.java": "public class SceneActivity {}\n",
    "de/jung/junghome/app/ui/scenes/SceneView.java": "public class SceneView {}\n",
    "de/jung/junghome/app/ui/scenes/C1234a.java": "public class C1234a {}\n",
    "de/jung/junghome/ui/components/PickerViewModel.java": "public class PickerViewModel {}\n",
    "de/jung/junghome/data/Foo.java": "public class Foo {}\n",
    "de/jung/junghome/R.java": "int x = R.string.unused;\n",
    "androidx/core/Lib.java": "int x = R.string.lib_only;\nint y = 337667;\n",
}

RESOURCES = {
    "res/values/strings.xml": """\
<resources>
    <string name="scenes_title">Scenes</string>
    <string name="scenes_title_description">Recall one</string>
    <string name="layout_only">X</string>
    <string name="unused">U</string>
    <string name="lib_only">L</string>
</resources>
""",
    "res/values-de/strings.xml": '<string name="x">@string/unused</string>\n',
    "res/layout/fragment_scenes.xml": '<TextView\n    android:text="@string/layout_only" />\n<X a="@string/undefined_ref" />\n',
    "assets/updates/a_update.json": '{"update_infos": [{"products": [\n  {"product_id": 10},\n  {"product_id": 1}\n]}]}\n',
    "assets/updates/a.gbl": "binary",
}

EXPECTED = {
    "activity:SceneActivity": "sources/de/jung/junghome/app/ui/scenes/SceneActivity.java:1",
    "ctlop:00": "sources/no/nordicsemi/android/mesh/opcodes/TransportLayerOpCodes.java:1",
    "fragment:ScenesFragment": "sources/de/jung/junghome/app/ui/scenes/ScenesFragment.java:3",
    "interactor:ActivateScene": "sources/de/jung/junghome/domain/interactors/scene/ActivateScene.java:3",
    "interactor:SceneKt": "sources/de/jung/junghome/domain/interactors/scene/SceneKt.java:1",
    "opcode:00": "sources/no/nordicsemi/android/mesh/transport/ConfigAppKeyAdd.java:1",
    "opcode:8201": "sources/no/nordicsemi/android/mesh/opcodes/ApplicationMessageOpCodes.java:2",
    "opcode:822b": "sources/p234v7/S.java:1",
    "opcode:8237": "sources/no/nordicsemi/android/mesh/opcodes/ApplicationMessageOpCodes.java:3",
    "opcode:c22705": "sources/G7/a.java:2",
    "opcode:c52705": "sources/G7/a.java:2",
    "opcode:d62705": "sources/G7/a.java:3",
    "product:0x0001": "resources/assets/updates/a_update.json:3",
    "product:0x000a": "resources/assets/updates/a_update.json:2",
    "prop:0x0008": "sources/p056e8/r.java:60",
    "prop:0x1001": "sources/p056e8/r.java:28",
    "prop:0xa002": "sources/p056e8/r.java:44",
    "prop:0xc000": "sources/p056e8/r.java:33",
    "proxyop:00": "sources/no/nordicsemi/android/mesh/opcodes/ProxyConfigMessageOpCodes.java:1",
    "proxyop:01": "sources/no/nordicsemi/android/mesh/transport/ProxyConfigAddAddressToFilter.java:2",
    "sensorprop:0x004e": "sources/F7/b.java:1",
    "sensorprop:0x006d": "sources/F7/b.java:2",
    "sigprop:0x0010": "sources/p056e8/r.java:13",
    "sigprop:0x006d": "sources/p056e8/r.java:19",
    "string:layout_only": "resources/res/layout/fragment_scenes.xml:2",
    "string:scenes_title": "sources/de/jung/junghome/app/ui/scenes/ScenesFragment.java:4",
    "string:scenes_title_description": "sources/de/jung/junghome/app/ui/scenes/ScenesFragment.java:5",
    "viewmodel:PickerViewModel": "sources/de/jung/junghome/ui/components/PickerViewModel.java:1",
    "viewmodel:ScenesViewModel": "sources/de/jung/junghome/app/ui/scenes/ScenesViewModel.java:1",
}


@pytest.fixture
def jadx(tmp_path: Path) -> Path:
    root = tmp_path / "jadx-out"
    for rel, text in JAVA.items():
        write(root / "sources" / rel, text)
    for rel, text in RESOURCES.items():
        write(root / "resources" / rel, text)
    return root


def test_extract_anchors(jadx: Path) -> None:
    assert parity.extract_anchors(jadx) == EXPECTED


def test_extract_anchors_from_a_tree_without_the_usual_files(tmp_path: Path) -> None:
    write(
        tmp_path / "sources" / "p" / "r.java",
        "/* compiled from: Property.kt */\nsuper(1);\n",
    )
    assert parity.extract_anchors(tmp_path) == {}
    assert parity.extract_anchors(tmp_path / "nothing") == {}


def test_helpers() -> None:
    assert [parity.opcode_hex(v) for v in (0x05, 0x8201, 0x052702, 0xC52705)] == [
        "05",
        "8201",
        "c22705",
        "c52705",
    ]
    assert parity.readable("ActivateScene")
    assert not any(
        parity.readable(n)
        for n in ("a", "T0", "C1846b", "AbstractC0916e", "InterfaceC1862s")
    )
    assert parity.kebab("TimeKeeperInfoActivity") == "time-keeper-info-activity"
    assert (
        parity.normal_path("android/jadx-out/sources/de/X$y$1.java:12") == "de/X.java"
    )
    assert parity.normal_path("res/values/strings.xml:3") == "res/values/strings.xml"
    assert parity.siblings("x_dialog_text") == ["x_title", "x", "x_header", "x_label"]
    assert parity.siblings("x_title") == ["x", "x_header", "x_label"]
    assert parity.string_lines(Path("/nonexistent")) == {}


# ----------------------------------------------------------------------------- apk: mapping and the command


def inventories(root: Path) -> None:
    base = root / parity.PARITY
    items = {
        "msg": ["msg:op:8201", "msg:op:c22705", "msg:proxy:01"],
        "mgmt": ["mgmt:cfgop:00"],
        "prop": ["prop:0x1001", "prop:sig:0x006d", "prop:sensor:0x004e"],
        "prod": ["prod:pid:0x000a", "prod:ui:scenes_title"],
        "net": ["net:uc:activatescene"],
        "ui": ["ui:uc:activatescene", "ui:vm:scenesviewmodel.load"],
    }
    docs: dict[str, list[dict[str, Any]]] = {
        d: [{"id": i} for i in ids] for d, ids in items.items()
    }
    docs["ui"] += [
        {
            "id": "ui:screen:scenes",
            "apk_refs": ["de/jung/junghome/app/ui/scenes/ScenesFragment.java:3"],
        },
        {
            "id": "ui:msg:layout",
            "title": "Layout",
            "details": "Shows layout_only.",
            "apk_refs": ["resources/res/values/strings.xml:4"],
        },
        {
            "id": "ui:msg:passing",
            "apk_refs": [
                "x/Y.java:1",
                "de/jung/junghome/app/ui/scenes/SceneActivity.java:1",
            ],
        },
    ]
    docs["zzz"] = [
        {
            "id": "zzz:about-recall",
            "apk_refs": [
                "android/jadx-out/resources/res/values/strings.xml:3",
                "res/values/strings.xml:x",
            ],
        }
    ]
    for domain, its in docs.items():
        dump(
            base / f"inventory-{domain}.json",
            {"apk": "APP 2.0" if domain == "ui" else "", "items": its},
        )
        dump(base / f"ledger-{domain}.json", {"rows": []})


def test_auto_map(tmp_path: Path, jadx: Path) -> None:
    inventories(tmp_path)
    index = parity.build_index(parity.load_parity(tmp_path), parity.string_lines(jadx))
    found = dict(EXPECTED)
    found["string:layout_only_hint"] = "resources/res/layout/fragment_nothing.xml:1"
    found["string:shown"] = (
        "sources/de/jung/junghome/app/ui/scenes/ScenesViewModel.java:9"
    )
    found["activity:OtherActivity"] = (
        "sources/de/jung/junghome/app/ui/scenes/OtherActivity.java:1"
    )
    expected = {
        "opcode:8201": "msg:op:8201",
        "opcode:c22705": "msg:op:c22705",
        "opcode:00": "mgmt:cfgop:00",
        "opcode:8237": None,
        "ctlop:00": None,
        "proxyop:01": "msg:proxy:01",
        "prop:0x1001": "prop:0x1001",
        "sigprop:0x006d": "prop:sig:0x006d",
        "sensorprop:0x004e": "prop:sensor:0x004e",
        "product:0x000a": "prod:pid:0x000a",
        "interactor:ActivateScene": "ui:uc:activatescene",  # the id is named after it; ui ranks first
        "viewmodel:ScenesViewModel": "ui:vm:scenesviewmodel.load",  # an id about one of its methods
        "string:scenes_title": "prod:ui:scenes_title",
        "string:scenes_title_description": "zzz:about-recall",  # cites the string's strings.xml line
        "string:layout_only": "ui:msg:layout",
        "string:layout_only_hint": "ui:msg:layout",  # the control it describes
        "fragment:ScenesFragment": "ui:screen:scenes",  # cited by an item about it
        "string:shown": None,  # a string is not assumed to belong to its package
        "activity:OtherActivity": "ui:screen:scenes",  # an item about another class of its package
        "activity:SceneActivity": "ui:screen:scenes",  # ui:msg:passing merely passes through it
        "interactor:SceneKt": None,
    }
    assert {a: parity.auto_map(a, found, index) for a in expected} == expected


def test_a_layout_string_counts_as_its_screen(tmp_path: Path, jadx: Path) -> None:
    inventories(tmp_path)
    index = parity.build_index(parity.load_parity(tmp_path), {})
    found = {
        "string:sc": "resources/res/layout/fragment_scenes.xml:2",
        "fragment:ScenesFragment": "sources/de/jung/junghome/app/ui/scenes/ScenesFragment.java:3",
    }
    assert (
        parity.evidence_path("string:sc", found)
        == "de/jung/junghome/app/ui/scenes/ScenesFragment.java"
    )
    assert parity.auto_map("string:sc", found, index) == "ui:screen:scenes"


def test_apk_command(
    tmp_path: Path, jadx: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventories(tmp_path)
    args = ["apk", str(jadx), "--root", str(tmp_path)]
    assert parity.main(args) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"{len(EXPECTED)} anchors (activity 1, ctlop 1, fragment 1, ")
    assert f"0 in anchors.json\n\nnot in anchors.json ({len(EXPECTED)}):\n" in out

    assert parity.main([*args, "--write"]) == 0
    path = tmp_path / parity.PARITY / parity.ANCHORS
    doc = parity.read_json(path)
    assert doc["apk"] == "APP 2.0"
    assert doc["rules"] == []
    assert list(doc["anchors"]) == sorted(EXPECTED)
    assert doc["anchors"]["opcode:8201"] == "msg:op:8201"
    assert doc["anchors"]["ctlop:00"] is None
    capsys.readouterr()

    # a hand-made mapping and a rule survive a rewrite; anchors the APK no longer has go
    doc["anchors"]["opcode:8201"] = "msg:proxy:01"
    doc["anchors"]["opcode:gone"] = "msg:op:8201"
    doc["rules"] = [{"match": "prop:*", "id": None, "why": "not a feature: test"}]
    doc["anchors"]["prop:0x1001"] = None
    parity.write_json(path, doc)
    assert parity.main(args) == 1
    out = capsys.readouterr().out
    assert "in anchors.json, not in the APK (1):\n  opcode:gone\t\n" in out
    assert "\nunmapped (" in out
    assert parity.main([*args, "--write"]) == 0
    doc = parity.read_json(path)
    assert doc["anchors"]["opcode:8201"] == "msg:proxy:01"
    assert doc["anchors"]["prop:0x1001"] is None
    assert "opcode:gone" not in doc["anchors"]

    doc["rules"].append({"match": "*", "id": None, "why": "not a feature: the rest"})
    parity.write_json(path, doc)
    capsys.readouterr()
    assert parity.main(args) == 0
    assert "not in anchors.json" not in capsys.readouterr().out
