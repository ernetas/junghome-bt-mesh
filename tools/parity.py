#!/usr/bin/env python3
"""Keep the parity ledger (docs/parity/README.md) closed: check it, diff captures against it, re-extract APK anchors.

    tools/parity.py check [--build]                 # the ledger and anchors.json, as tests/test_parity.py does
    tools/parity.py air decoded.ndjson...           # (kind, opcode, property) on air that inventory-air.json lacks
    tools/parity.py apk android/jadx-out [--write]  # APK anchors that anchors.json does not map to an item

Standard library only: `check` runs in CI without Home Assistant, `air` on the sniffer host.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import functools
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PARITY = Path("docs/parity")
ANCHORS = "anchors.json"
UNMAPPED = "unmapped-anchors.json"

STATUSES = ("implemented", "partial", "gap", "na")
CLASSES = ("build", "ha-native", "internal", "declined", "dup")
NA_CLASSES = frozenset(CLASSES) - {"build"}
VERIFY = ("offline", "on-air", "absent-hardware")
ANCHOR_DOMAINS = ("msg", "prop", "prod", "net", "mgmt", "ui")
# which domain an item is looked for in first when several cite the same class or string
DOMAIN_ORDER = ("ui", "net", "prod", "mgmt", "msg", "prop", "air")

# `path::symbol` or `path:line::symbol`: a symbol survives edits above it, a bare line number drifts with them
CODE_CITE = re.compile(
    r"(?P<path>(?!/)(?!(?:.*/)?\.\.(?:/|:))[^:]+?)(?::(?P<line>\d+))?::(?P<symbol>[\w.-]+)"
)
# how far a cited line may sit before its symbol (decorators, a comment) or past its end
LINE_SLACK = 5
TEST_CITE = re.compile(r"(?P<path>tests/[^:]+\.py)::(?P<name>\w+(?:::\w+)?)(?:\[.*\])?")


# ----------------------------------------------------------------------------- loading


@dataclass(frozen=True)
class Problem:
    """One reason the ledger is not closed; `area` is ledger, anchors (structure) or coverage (unmapped anchors)."""

    area: str
    where: str
    what: str

    def __str__(self) -> str:
        return f"[{self.area}] {self.where}: {self.what}"


@dataclass
class Domain:
    """One inventory and its ledger (None: the ledger file does not exist)."""

    name: str
    items: list[dict[str, Any]]
    rows: list[dict[str, Any]] | None
    apk: str = ""


@dataclass
class Parity:
    """Everything under docs/parity that `check` reads."""

    domains: dict[str, Domain] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {str(i.get("id")) for d in self.domains.values() for i in d.items}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, doc: Any) -> None:
    path.write_text(
        json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_parity(root: Path) -> Parity:
    """Pair every inventory-<d>.json with ledger-<d>.json; unreadable or unpaired files become problems."""
    parity = Parity()
    base = root / PARITY
    names = sorted(
        {p.stem.split("-", 1)[1] for p in base.glob("inventory-*.json")}
        | {p.stem.split("-", 1)[1] for p in base.glob("ledger-*.json")}
    )
    for name in names:
        docs: dict[str, Any] = {}
        for kind in ("inventory", "ledger"):
            path = base / f"{kind}-{name}.json"
            if not path.exists():
                parity.problems.append(
                    Problem("ledger", path.name, f"no {kind} file for this domain")
                )
                continue
            try:
                docs[kind] = read_json(path)
            except json.JSONDecodeError as e:
                parity.problems.append(Problem("ledger", path.name, f"not JSON: {e}"))
        if "inventory" in docs:
            inventory = docs["inventory"]
            ledger = docs.get("ledger")
            parity.domains[name] = Domain(
                name,
                list(inventory.get("items", [])),
                None if ledger is None else list(ledger.get("rows", [])),
                str(inventory.get("apk", "")),
            )
    return parity


# ----------------------------------------------------------------------------- check: the ledger


Spans = dict[str, list[tuple[int, int]]]
SYMBOL_SUFFIXES = (".py", ".json")
JSON_KEY = re.compile(r'(?P<indent>\s*)"(?P<key>(?:[^"\\]|\\.)*)"\s*:')


def python_symbols(text: str, filename: str = "<string>") -> Spans:
    """Every name a module defines, with its line spans (first decorator to last line).

    Functions and classes at any depth are qualified by what encloses them (`Class.method`, `outer.inner`);
    assignments count at module and class level only (`CONST`, `Class.attr`), not a function's locals. Statements
    that only nest (`if`, `try`, `with`, `for`, `match`) are looked into. A name defined twice (an overload, both
    branches of an `if`) keeps every span.
    """
    out: Spans = defaultdict(list)
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)

    def names(target: ast.expr) -> list[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            return [n for t in target.elts for n in names(t)]
        if isinstance(target, ast.Starred):
            return names(target.value)
        return []

    def visit(body: list[ast.stmt], prefix: str, in_function: bool) -> None:
        for node in body:
            end = node.end_lineno or node.lineno
            if isinstance(node, (*funcs, ast.ClassDef)):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                out[prefix + node.name].append((start, end))
                visit(node.body, f"{prefix}{node.name}.", isinstance(node, funcs))
                continue
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            elif isinstance(node, ast.TypeAlias):
                targets = [node.name]
            if not in_function:
                for name in (n for t in targets for n in names(t)):
                    out[prefix + name].append((node.lineno, end))
            for field_name in ("body", "orelse", "finalbody", "handlers", "cases"):
                for part in getattr(node, field_name, None) or []:
                    # an except handler or a match case is not a statement but holds a body of them
                    inner = [part] if isinstance(part, ast.stmt) else part.body
                    visit(inner, prefix, in_function)

    visit(ast.parse(text, filename=filename).body, "", in_function=False)
    return dict(out)


def json_symbols(text: str) -> Spans:
    """Every key of a JSON file written one key per line, as a dotted path (`config.step.user.title`), with its span.

    Read from the indentation, not by parsing, so that a key keeps the lines it is written on.
    """
    out: Spans = defaultdict(list)
    lines = text.splitlines()
    open_keys: list[tuple[int, str, int]] = []  # (indent, dotted path, first line)
    for n, line in enumerate(lines, 1):
        m = JSON_KEY.match(line)
        if m is None:
            continue
        indent = len(m["indent"])
        while open_keys and open_keys[-1][0] >= indent:
            _, path, start = open_keys.pop()
            out[path].append((start, n - 1))
        parent = open_keys[-1][1] + "." if open_keys else ""
        open_keys.append((indent, parent + m["key"], n))
    for _, path, start in open_keys:
        out[path].append((start, len(lines)))
    return dict(out)


def innermost_symbol(spans: Spans, line: int) -> str | None:
    """The most specific symbol whose span holds `line` (a method over its class), or None outside every symbol."""
    holding = [
        (b - a, -len(name), name)
        for name, ranges in spans.items()
        for a, b in ranges
        if a <= line <= b
    ]
    return min(holding)[2] if holding else None


class Citations:
    """Resolves `path[:line]::symbol` and `tests/file.py::[Class::]name` citations against the repository, cached."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @functools.cache  # noqa: B019  # one instance per check run
    def symbols(self, rel: str) -> Spans | None:
        """The symbols `rel` (a .py or .json file) defines, or None when there is no such file."""
        path = self.root / rel
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        return python_symbols(text, rel) if rel.endswith(".py") else json_symbols(text)

    @functools.cache  # noqa: B019
    def test_names(self, rel: str) -> frozenset[str] | None:
        path = self.root / rel
        if not path.is_file():
            return None
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
        names = {n.name for n in tree.body if isinstance(n, funcs)}
        names |= {
            f"{c.name}::{f.name}"
            for c in tree.body
            if isinstance(c, ast.ClassDef)
            for f in c.body
            if isinstance(f, funcs)
        }
        return frozenset(names)

    def code(self, cite: str) -> str | None:
        m = CODE_CITE.fullmatch(cite)
        if m is None:
            return f"code {cite!r} is not a repository-relative path::symbol or path:line::symbol"
        path, symbol = m["path"], m["symbol"]
        if not path.endswith(SYMBOL_SUFFIXES):
            return f"code {cite}: only {' and '.join(SYMBOL_SUFFIXES)} files can be cited by symbol"
        symbols = self.symbols(path)
        if symbols is None:
            return f"code {cite}: no such file"
        spans = symbols.get(symbol)
        if not spans:
            return f"code {cite}: {path} defines no {symbol}"
        line = m["line"]
        if line is not None and not any(
            a - LINE_SLACK <= int(line) <= b for a, b in spans
        ):
            where = ", ".join(f"{a}-{b}" for a, b in spans)
            return f"code {cite}: line {line} is not within {symbol} (lines {where})"
        return None

    def test(self, cite: str) -> str | None:
        m = TEST_CITE.fullmatch(cite)
        if m is None:
            return f"test {cite!r} is not tests/file.py::name or tests/file.py::Class::name"
        names = self.test_names(m["path"])
        if names is None:
            return f"test {cite}: no such file"
        if m["name"] not in names:
            return f"test {cite}: no such test"
        return None


def row_problems(row: dict[str, Any], ids: set[str], cites: Citations) -> list[str]:
    """What is wrong with one ledger row, per the README's row schema and classification rules."""
    out: list[str] = []
    status, cls = row.get("status"), row.get("class")
    if status not in STATUSES:
        out.append(
            "no status"
            if status is None
            else f"status {status!r} is not one of {', '.join(STATUSES)}"
        )
    if status != "implemented" and cls is None:
        out.append("not implemented but no class")
    if cls is not None and cls not in CLASSES:
        out.append(f"class {cls!r} is not one of {', '.join(CLASSES)}")
    if cls in NA_CLASSES and status != "na":
        out.append(f"class {cls} needs status na")
    if cls == "build":
        if status not in ("gap", "partial"):
            out.append("class build needs status gap or partial")
        if row.get("verify") not in VERIFY:
            out.append(f"build row needs verify ({', '.join(VERIFY)})")
    if status in ("gap", "partial") and not row.get("missing"):
        out.append(f"{status} row without missing")
    if status == "na" and not row.get("reason"):
        out.append("na row without reason")
    if cls == "dup" and (
        row.get("dup_of") not in ids or row.get("dup_of") == row.get("id")
    ):
        out.append(f"dup_of {row.get('dup_of')!r} is not another existing id")
    if status == "implemented":
        if not row.get("code"):
            out.append("implemented without code")
        if not row.get("tests"):
            out.append("implemented without tests")
    out.extend(
        p for c in row.get("code") or [] if (p := cites.code(str(c))) is not None
    )
    out.extend(
        p for t in row.get("tests") or [] if (p := cites.test(str(t))) is not None
    )
    return out


def duplicates(values: list[str]) -> list[str]:
    return sorted(v for v, n in Counter(values).items() if n > 1)


def ledger_problems(parity: Parity, root: Path) -> list[Problem]:
    """Inventory/ledger id agreement and every row's decision and citations."""
    out: list[Problem] = []
    ids = parity.ids
    cites = Citations(root)
    for d in parity.domains.values():
        if d.rows is None:
            continue
        inventory = f"inventory-{d.name}.json"
        ledger = f"ledger-{d.name}.json"
        item_ids = [str(i.get("id")) for i in d.items]
        row_ids = [str(r.get("id")) for r in d.rows]
        out += [
            Problem("ledger", inventory, f"duplicate id {i}")
            for i in duplicates(item_ids)
        ]
        out += [
            Problem("ledger", ledger, f"duplicate row {i}") for i in duplicates(row_ids)
        ]
        out += [
            Problem("ledger", ledger, f"no row for {i}")
            for i in sorted(set(item_ids) - set(row_ids))
        ]
        out += [
            Problem("ledger", ledger, f"row {i} is not an inventory id")
            for i in sorted(set(row_ids) - set(item_ids))
        ]
        for row in d.rows:
            out += [
                Problem("ledger", f"{ledger} {row.get('id')}", p)
                for p in row_problems(row, ids, cites)
            ]
    return out


# ----------------------------------------------------------------------------- check: anchors.json


def targets(value: Any) -> list[str] | None:
    """The ids an anchors.json mapping names ([] for null); None when the value is neither an id, a list nor null."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def resolve_anchor(
    anchor: str, anchors: dict[str, Any], rules: list[dict[str, Any]]
) -> list[str] | None:
    """The ids covering an anchor ([] = declared not a feature by a rule), or None when nothing covers it."""
    explicit = targets(anchors.get(anchor))
    if explicit:
        return explicit
    for rule in rules:
        if fnmatch.fnmatchcase(anchor, str(rule.get("match", ""))):
            return targets(rule.get("id")) or []
    return None


def anchor_problems(root: Path, ids: set[str]) -> list[Problem]:
    """anchors.json structure (rules, known ids), unmapped-anchors.json consistency, and coverage."""
    base = root / PARITY
    if not (base / ANCHORS).exists():
        return [Problem("anchors", ANCHORS, "missing")]
    doc = read_json(base / ANCHORS)
    rules: list[dict[str, Any]] = list(doc.get("rules", []))
    anchors: dict[str, Any] = dict(doc.get("anchors", {}))
    out: list[Problem] = []
    for n, rule in enumerate(rules):
        where = f"{ANCHORS} rules[{n}] {rule.get('match')!r}"
        if not rule.get("match"):
            out.append(Problem("anchors", where, "no match glob"))
        if not rule.get("why"):
            out.append(Problem("anchors", where, "no why"))
        rid = rule.get("id")
        if rid is not None and rid not in ids:
            out.append(Problem("anchors", where, f"id {rid!r} is not an inventory id"))
    for anchor, value in anchors.items():
        names = targets(value)
        if names is None:
            out.append(
                Problem(
                    "anchors",
                    anchor,
                    "maps to something other than an id, a list of ids or null",
                )
            )
            continue
        out += [
            Problem("anchors", anchor, f"maps to {i}, not an inventory id")
            for i in names
            if i not in ids
        ]
    unmapped = [a for a in anchors if resolve_anchor(a, anchors, rules) is None]
    listed: dict[str, str] = {}
    if (base / UNMAPPED).exists():
        for domain, entries in read_json(base / UNMAPPED).items():
            if domain not in ANCHOR_DOMAINS:
                out.append(
                    Problem(
                        "anchors",
                        UNMAPPED,
                        f"domain {domain!r} is not one of {', '.join(ANCHOR_DOMAINS)}",
                    )
                )
            for entry in entries:
                anchor = str(entry.get("anchor"))
                listed[anchor] = domain
                if not entry.get("evidence") or not entry.get("suggested_id"):
                    out.append(
                        Problem(
                            "anchors",
                            f"{UNMAPPED} {anchor}",
                            "needs evidence and suggested_id",
                        )
                    )
    unmapped_set = set(unmapped)
    out += [
        Problem(
            "anchors",
            f"{UNMAPPED} {a}",
            "listed, but anchors.json has no such unmapped anchor",
        )
        for a in sorted(listed)
        if a not in unmapped_set
    ]
    out += [
        Problem(
            "coverage",
            a,
            f"no inventory item covers it (listed under {listed[a]})"
            if a in listed
            else f"no inventory item covers it, and {UNMAPPED} does not list it",
        )
        for a in sorted(unmapped)
    ]
    return out


# ----------------------------------------------------------------------------- check: the command


def check(root: Path) -> tuple[Parity, list[Problem]]:
    """Every problem of the committed parity files under `root`."""
    parity = load_parity(root)
    problems = (
        parity.problems
        + ledger_problems(parity, root)
        + anchor_problems(root, parity.ids)
    )
    return parity, problems


def build_rows(parity: Parity) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """The `build` rows grouped by (domain, verify), in ledger order."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for d in parity.domains.values():
        for row in d.rows or []:
            if row.get("class") == "build":
                groups[d.name, str(row.get("verify"))].append(row)
    return dict(sorted(groups.items()))


def cmd_check(args: argparse.Namespace) -> int:
    parity, problems = check(Path(args.root))
    for d in parity.domains.values():
        rows = d.rows or []
        statuses = Counter(str(r.get("status")) for r in rows)
        classes = Counter(str(r.get("class")) for r in rows if r.get("class"))
        print(
            f"{d.name}: {len(d.items)} items, {len(rows)} rows; "
            + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items()))
            + (
                "; " + ", ".join(f"{k} {v}" for k, v in sorted(classes.items()))
                if classes
                else ""
            )
        )
    if args.build:
        for (domain, verify), rows in build_rows(parity).items():
            print(f"\nbuild {domain} / {verify} ({len(rows)}):")
            for row in rows:
                print(f"  {row.get('id')}: {row.get('missing', '')}")
    by_area = Counter(p.area for p in problems)
    print(
        f"\n{len(problems)} problems"
        + "".join(f", {a} {n}" for a, n in sorted(by_area.items()))
    )
    for p in problems:
        print(f"  {p}")
    return 1 if problems else 0


# ----------------------------------------------------------------------------- air

# `prop 0x5003` (property Get / Status / Set) or `prop=0x0052` (Sensor Status): what captures-inventory.json keys on
PROP_TEXT = re.compile(r"\bprop[ =]0x([0-9A-Fa-f]{4})\b")
# what precedes a network-layer message's own text: `[gatt …] ` and `SRC→DST ttl=N seq=XXXXXX `
TEXT_HEAD = re.compile(
    r"^(?:\[[^\]]*\]\s*)?(?:[0-9A-Fa-f]{4}→[0-9A-Fa-f]{4} ttl=\d+ seq=[0-9A-Fa-f]+\s*)?"
)


def record_name(text: str) -> str:
    """The decoder's name for a non-access record: its leading words, before any number, hex or `key=value`."""
    words: list[str] = []
    for token in TEXT_HEAD.sub("", text, count=1).split():
        if token.startswith("("):
            break
        if re.fullmatch(r"[0-9a-f]+", token) or not re.fullmatch(
            r"[A-Za-z][A-Za-z_-]*", token
        ):
            break
        words.append(token)
    return " ".join(words)


def air_slug(kind: str, name: str) -> str:
    """inventory-air.json's slug rule: lowercase, non-alphanumerics to '-', `<kind>-` / `foreign-` prefix dropped."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    for prefix in (f"{kind}-", "foreign-"):
        slug = slug.removeprefix(prefix)
    return slug or kind


def air_key(
    record: dict[str, Any], property_opcodes: frozenset[str]
) -> tuple[str, str | None, str | None, str]:
    """(kind, opcode, property, inventory-air id) of one decoded record, the id formed as inventory-air.json forms it."""
    kind = str(record.get("kind"))
    text = str(record.get("text") or "")
    opcode = record.get("opcode")
    if kind != "access" or not opcode:
        return kind, None, None, f"air:{kind}:{air_slug(kind, record_name(text))}"
    op = str(opcode).lower().replace(":", "-")
    prop: str | None = None
    m = PROP_TEXT.search(text)
    params = str(record.get("params") or "")
    if m is not None:
        prop = f"0x{m[1].lower()}"
    elif op in property_opcodes and len(params) >= 4:
        prop = f"0x{int.from_bytes(bytes.fromhex(params[:4]), 'little'):04x}"
    return kind, str(opcode), prop, f"air:access:{op}" + (f":{prop}" if prop else "")


def cmd_air(args: argparse.Namespace) -> int:
    inventory = read_json(Path(args.root) / PARITY / "inventory-air.json")
    ids = {str(i["id"]) for i in inventory["items"]}
    # opcodes whose items are keyed per property: a record of theirs names one even when its text does not
    property_opcodes = frozenset(
        i.split(":")[2]
        for i in ids
        if i.startswith("air:access:") and i.count(":") == 3
    )
    seen: Counter[tuple[str, str | None, str | None, str]] = Counter()
    bad = 0
    for name in args.files:
        for n, line in enumerate(
            Path(name).read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                seen[air_key(json.loads(line), property_opcodes)] += 1
            except (json.JSONDecodeError, AttributeError, ValueError) as e:
                print(f"{name}:{n}: not a decoded record: {e}", file=sys.stderr)
                bad += 1
    missing = sorted(
        ((k, n) for k, n in seen.items() if k[3] not in ids),
        key=lambda kn: tuple(x or "" for x in kn[0]),
    )
    for (kind, opcode, prop, item), n in missing:
        print(f"{kind}\t{opcode or '-'}\t{prop or '-'}\t{item}\t{n}")
    print(
        f"{sum(seen.values())} records, {len(seen)} distinct, {len(missing)} not in inventory-air.json",
        file=sys.stderr,
    )
    return 1 if missing or bad else 0


# ----------------------------------------------------------------------------- apk: anchor extraction

# third-party packages jadx keeps under their own names (obfuscated ones cannot be told apart and stay in)
LIB_DIRS = frozenset(
    {
        "android",
        "androidx",
        "com",
        "dagger",
        "io",
        "j$",
        "javax",
        "junit",
        "kotlin",
        "kotlinx",
        "okhttp3",
        "okio",
        "org",
        "retrofit2",
    }
)
INT_LITERAL = r"(?:0x[0-9A-Fa-f]+|\d+)"
# the app's vendor opcode ints (0x0527 << 8 | first opcode byte) and its status opcodes re-encoded b0<<16|b1<<8|b2
JUNG_SEND = range(0x052700, 0x052800)
JUNG_RECEIVE = frozenset(((0xC0 + k) << 16) | 0x2705 for k in range(64))
UI_SUFFIX = re.compile(r"(Fragment|ViewModel|Activity)$")
INTERACTORS = "de/jung/junghome/domain/interactors/"
UI_DIRS = ("de/jung/junghome/app/ui/", "de/jung/junghome/ui/")


def parse_int(text: str) -> int:
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def opcode_hex(value: int) -> str:
    """msg inventory form: 2 hex digits below 0x80, 4 for two-byte SIG opcodes, the 3 wire bytes for vendor ones."""
    if value in JUNG_SEND:
        return f"{(value & 0xFF) | 0xC0:02x}2705"
    return (
        f"{value:02x}"
        if value < 0x80
        else f"{value:04x}"
        if value <= 0xFFFF
        else f"{value:06x}"
    )


def readable(name: str) -> bool:
    """A class name that survived obfuscation (not `a`, `C1846b`, `AbstractC0916e`, `InterfaceC1862s`)."""
    return (
        re.fullmatch(r"[A-Z][A-Za-z0-9]*", name) is not None
        and re.search(r"[a-z]{2}", name) is not None
        and re.fullmatch(r"(?:Abstract|Interface)?C\d+[a-z]*", name) is None
    )


@functools.lru_cache(
    maxsize=256
)  # constant holders (CipherSuite, opcode tables) are read once per reference
def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def resolve_int(expr: str, text: str, src: Path) -> int | None:
    """An int literal, or `Cls.NAME` resolved through the file's imports to `NAME = <literal>` in Cls's source."""
    expr = re.sub(r"^\(\w+\)\s*", "", expr.strip())
    if re.fullmatch(INT_LITERAL, expr):
        return parse_int(expr)
    m = re.fullmatch(r"(\w+)\.(\w+)", expr)
    if m is None:
        return None
    imp = re.search(rf"^import ([\w.$]+\.{m[1]});", text, re.MULTILINE)
    path = src / (f"{imp[1].replace('.', '/')}.java" if imp else "")
    if imp is None or not path.is_file():
        return None
    const = re.search(rf"\b{m[2]}\s*=\s*({INT_LITERAL})\s*;", read_text(path))
    return None if const is None else parse_int(const[1])


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


class Extraction:
    """Anchors found so far, each with the first place it was seen (`sources/…:line`, `resources/…:line`)."""

    def __init__(self, jadx: Path) -> None:
        self.jadx = jadx
        self.src = jadx / "sources"
        self.found: dict[str, str] = {}
        self.string_refs: dict[str, str] = {}
        self.device_property_refs: dict[str, str] = {}
        self.generic_property_lines: list[str] = []
        self.property_file: Path | None = None
        self.device_property_file: Path | None = None

    def where(self, path: Path, line: int) -> str:
        return f"{path.relative_to(self.jadx).as_posix()}:{line}"

    def add(self, anchor: str, path: Path, line: int) -> None:
        self.found.setdefault(anchor, self.where(path, line))

    def scan_java(self, path: Path) -> None:
        """Opcodes, class names, string / DeviceProperty references and the Property.kt file of one source file."""
        text = read_text(path)
        rel = path.relative_to(self.src).as_posix()
        for m in re.finditer(r"\b(0x[0-9A-Fa-f]{6}|\d{6,8})\b", text):
            value = parse_int(m[1])
            if value in JUNG_SEND or value in JUNG_RECEIVE:
                self.add(f"opcode:{opcode_hex(value)}", path, line_of(text, m.start()))
        for m in re.finditer(r"int getOpCode\(\)\s*\{\s*return\s+([\w.]+);", text):
            resolved = resolve_int(m[1], text, self.src)
            kind = (
                "proxyop"
                if re.search(r"extends ProxyConfig\w*Message\b", text)
                else "opcode"
            )
            if resolved is not None:
                self.add(
                    f"{kind}:{opcode_hex(resolved)}", path, line_of(text, m.start())
                )
        for m in re.finditer(r"new GenericPropertyGet\(\s*(\d+)", text):
            self.add(f"opcode:{opcode_hex(int(m[1]))}", path, line_of(text, m.start()))
        if path.parent.name == "opcodes" and path.name.endswith("OpCodes.java"):
            kind = (
                "proxyop"
                if "ProxyConfig" in path.name
                else "ctlop"
                if "TransportLayer" in path.name
                else "opcode"
            )
            for m in re.finditer(rf"static final int \w+ = ({INT_LITERAL});", text):
                self.add(
                    f"{kind}:{opcode_hex(parse_int(m[1]))}",
                    path,
                    line_of(text, m.start()),
                )
        if path.name != "R.java":
            for m in re.finditer(r"\bR\.string\.(\w+)", text):
                self.string_refs.setdefault(
                    m[1], self.where(path, line_of(text, m.start()))
                )
        if not rel.startswith("no/"):
            for m in re.finditer(r"\bDeviceProperty\.([A-Z][A-Z0-9_]+)\b", text):
                self.device_property_refs.setdefault(
                    m[1], self.where(path, line_of(text, m.start()))
                )
        elif path.name == "DeviceProperty.java":
            self.device_property_file = path
        self.generic_property_lines += [
            line for line in text.splitlines() if "GenericProperty" in line
        ]
        if (
            self.property_file is None
            and "compiled from: Property.kt" in text
            and "super(" in text
        ):
            self.property_file = path
        self.scan_class(path, rel, text)

    def scan_class(self, path: Path, rel: str, text: str) -> None:
        name = path.stem
        if "$" in name or not readable(name):
            return
        decl = re.search(rf"\b(?:class|interface|enum) {name}\b", text)
        line = line_of(text, decl.start()) if decl else 1
        suffix = UI_SUFFIX.search(name)
        if rel.startswith(INTERACTORS):
            self.add(f"interactor:{name}", path, line)
        elif rel.startswith(UI_DIRS) and suffix:
            self.add(f"{suffix[1].lower()}:{name}", path, line)

    def scan_properties(self) -> None:
        """Every `super(<id>)` of the Property sealed class (SIG section = the one GenericProperty messages use)."""
        if self.property_file is None:
            return
        text = read_text(self.property_file)
        lines = text.splitlines()
        top = re.search(r"^public abstract class (\w+)", text, re.MULTILINE)
        if top is None:
            return
        sections: list[tuple[int, int, str]] = []
        for n, line in enumerate(lines):
            m = re.match(
                rf"(\s*)public static abstract class (\w+) extends {top[1]}\b", line
            )
            if m:
                end = next(
                    (j for j in range(n + 1, len(lines)) if lines[j] == f"{m[1]}}}"),
                    len(lines),
                )
                sections.append((n, end, m[2]))
        sig = {
            s
            for _, _, s in sections
            if any(f"{top[1]}.{s}." in g for g in self.generic_property_lines)
        }
        for n, line in enumerate(lines):
            m = re.search(r"(?:\bsuper|= new \w+)\(([\w.]+)\);", line)
            # the LED properties are families: their base id is what d() returns
            if m is None and n and re.search(r"\bint d\(\) \{", lines[n - 1]):
                m = re.search(rf"\breturn ({INT_LITERAL});", line)
            inside = [s for s in sections if s[0] < n < s[1]]
            value = None if m is None else resolve_int(m[1], text, self.src)
            if value is not None and inside:
                kind = "sigprop" if max(inside)[2] in sig else "prop"
                self.add(f"{kind}:0x{value:04x}", self.property_file, n + 1)

    def scan_device_properties(self) -> None:
        """SIG sensor properties the app names (`DeviceProperty.X` outside the Nordic library)."""
        if self.device_property_file is None:
            return
        text = read_text(self.device_property_file)
        for name, where in sorted(
            self.device_property_refs.items(), key=lambda kv: kv[1]
        ):
            m = re.search(rf"^\s*{name}\(([^,)]+)", text, re.MULTILINE)
            value = None if m is None else resolve_int(m[1], text, self.src)
            if value is not None:
                self.found.setdefault(f"sensorprop:0x{value:04x}", where)

    def scan_resources(self) -> None:
        """Referenced string resources (R.string.x in code, @string/x in layouts, menus, navigation, xml)."""
        res = self.jadx / "resources" / "res"
        strings = res / "values" / "strings.xml"
        defined = (
            set(re.findall(r'<string name="([^"]+)"', read_text(strings)))
            if strings.is_file()
            else set()
        )
        refs = dict(self.string_refs)
        for path in sorted(res.glob("*/*.xml")):
            if path.parent.name.startswith("values"):
                continue
            text = read_text(path)
            for m in re.finditer(r"@string/(\w+)", text):
                refs.setdefault(m[1], self.where(path, line_of(text, m.start())))
        for name in sorted(defined & refs.keys()):
            self.found.setdefault(f"string:{name}", refs[name])

    def scan_updates(self) -> None:
        """Product ids of the bundled firmware update descriptors."""
        for path in sorted(
            (self.jadx / "resources" / "assets" / "updates").glob("*_update.json")
        ):
            text = read_text(path)
            for m in re.finditer(r'"product_id"\s*:\s*(\d+)', text):
                self.add(f"product:0x{int(m[1]):04x}", path, line_of(text, m.start()))


def extract_anchors(jadx: Path) -> dict[str, str]:
    """Every anchor of a jadx output tree, sorted, with its first evidence."""
    ex = Extraction(jadx)
    for path in sorted(ex.src.rglob("*.java")):
        if path.relative_to(ex.src).parts[0] not in LIB_DIRS:
            ex.scan_java(path)
    ex.scan_properties()
    ex.scan_device_properties()
    ex.scan_resources()
    ex.scan_updates()
    return dict(sorted(ex.found.items()))


# ----------------------------------------------------------------------------- apk: mapping anchors to items


def kebab(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name).lower()


def normal_path(ref: str) -> str:
    """An apk_ref or evidence path as `de/…/Outer.java` / `res/…`: no jadx-out prefix, line, or `$inner` suffix."""
    path = ref.partition(":")[0]
    for prefix in ("android/jadx-out/", "sources/", "resources/"):
        path = path.removeprefix(prefix)
    return re.sub(r"\$[^/]*\.java$", ".java", path)


@dataclass
class Index:
    """Where the inventories mention things: by id, id suffix, cited file / string resource / package, and words."""

    ids: set[str] = field(default_factory=set)
    by_last: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_ref: dict[str, list[tuple[int, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    by_dir: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_word: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add_ref(
        self, ref: str, item: str, rank: int, string_lines: dict[int, str]
    ) -> None:
        path, line = normal_path(ref), ref.rpartition(":")[2]
        if (
            path == "res/values/strings.xml"
            and line.isdigit()
            and int(line) in string_lines
        ):
            self.by_ref[f"string:{string_lines[int(line)]}"].append((rank, item))
        # an item is about its first citation; the later ones are places it merely passes through
        if rank == 0:
            self.by_ref[path].append((rank, item))
            if path.endswith(".java"):
                self.by_dir[path.rpartition("/")[0]].append(item)


def build_index(parity: Parity, string_lines: dict[int, str]) -> Index:
    index = Index()
    for d in sorted(
        parity.domains.values(),
        key=lambda d: DOMAIN_ORDER.index(d.name) if d.name in DOMAIN_ORDER else 99,
    ):
        for item in d.items:
            iid = str(item.get("id"))
            index.ids.add(iid)
            index.by_last[iid.rsplit(":", 1)[-1]].append(iid)
            for n, ref in enumerate(item.get("apk_refs") or []):
                index.add_ref(str(ref), iid, min(n, 1), string_lines)
            for word in set(
                re.findall(r"\w+", f"{item.get('title', '')} {item.get('details', '')}")
            ):
                index.by_word[word].append(iid)
    return index


DIRECT = {
    "opcode": ("msg:op:{}", "mgmt:cfgop:{}", "mgmt:appop:{}"),
    "proxyop": ("msg:proxy:{}", "mgmt:proxyop:{}"),
    "ctlop": (),
    "prop": ("prop:{}",),
    "sigprop": ("prop:sig:{}", "prop:sensor:{}"),
    "sensorprop": ("prop:sensor:{}", "prop:sig:{}"),
    "product": ("prod:pid:{}",),
}


STRING_PART = re.compile(
    r"_(?:description|sub_?title|sub_?text|text|message|info|hint|explanation|label|header|title|button\w*|dialog\w*)(?:_\w+)?$"
)


def siblings(name: str) -> list[str]:
    """The control a string resource belongs to: `x_description` / `x_dialog_text` / ... -> `x_title`, `x`, `x_header`."""
    base = STRING_PART.sub("", name)
    return [
        s
        for s in (f"{base}_title", base, f"{base}_header", f"{base}_label")
        if s != name
    ]


def ranked(refs: list[tuple[int, str]]) -> list[str]:
    return [i for _, i in sorted(refs, key=lambda ri: ri[0])]


def evidence_path(anchor: str, found: dict[str, str]) -> str:
    """Where an anchor was found; a string first seen in an activity / fragment layout counts as that class's."""
    path = normal_path(found.get(anchor, ""))
    m = re.fullmatch(r"res/layout[^/]*/(activity|fragment)_(\w+)\.xml", path)
    if m:
        owner = f"{m[1]}:{''.join(p.capitalize() for p in m[2].split('_'))}{m[1].capitalize()}"
        path = normal_path(found.get(owner, path))
    return path


def candidates(anchor: str, found: dict[str, str], index: Index) -> list[list[str]]:
    """The item an anchor most likely belongs to, first match of: its mechanical id; an id named after it; an item
    citing the string resource; an item whose text names it (long compound names only); the same for the title of the
    control a description / hint / dialog string belongs to; an item about the file it was found in (the class itself,
    or the code using the string); for classes, an item about another class of the same package."""
    kind, _, value = anchor.partition(":")
    if kind in DIRECT:
        return [[i for p in DIRECT[kind] if (i := p.format(value)) in index.ids]]
    names = {value, value.lower(), kebab(value), kebab(value).removesuffix("-activity")}
    path = evidence_path(anchor, found)
    return [
        [i for n in sorted(names) for i in index.by_last.get(n, [])],
        [
            i
            for last, ids in index.by_last.items()
            if last.startswith(f"{value.lower()}.")
            for i in ids
        ],
        ranked(index.by_ref.get(f"string:{value}", [])),
        index.by_word.get(value, [])
        if len(value) >= 6 and re.search(r"_|[a-z][A-Z]", value)
        else [],
        [
            i
            for s in siblings(value)
            for i in index.by_last.get(s, [])
            + ranked(index.by_ref.get(f"string:{s}", []))
            + index.by_word.get(s, [])
        ]
        if kind == "string"
        else [],
        ranked(index.by_ref.get(path, [])),
        index.by_dir.get(path.rpartition("/")[0], []) if kind != "string" else [],
    ]


def auto_map(anchor: str, found: dict[str, str], index: Index) -> str | None:
    """The first candidate of the first tier that has one."""
    return next((tier[0] for tier in candidates(anchor, found, index) if tier), None)


def string_lines(jadx: Path) -> dict[int, str]:
    strings = jadx / "resources" / "res" / "values" / "strings.xml"
    if not strings.is_file():
        return {}
    return {
        n: m[1]
        for n, line in enumerate(read_text(strings).splitlines(), 1)
        if (m := re.search(r'<string name="([^"]+)"', line))
    }


def cmd_apk(args: argparse.Namespace) -> int:
    root, jadx = Path(args.root), Path(args.jadx)
    found = extract_anchors(jadx)
    path = root / PARITY / ANCHORS
    parity = load_parity(root)
    doc: dict[str, Any] = read_json(path) if path.exists() else {}
    doc.setdefault("apk", next((d.apk for d in parity.domains.values() if d.apk), ""))
    rules: list[dict[str, Any]] = doc.setdefault("rules", [])
    anchors: dict[str, Any] = doc.get("anchors", {})
    if args.write:
        index = build_index(parity, string_lines(jadx))
        doc["anchors"] = {
            a: anchors.get(a)
            if anchors.get(a) is not None or resolve_anchor(a, {}, rules) is not None
            else auto_map(a, found, index)
            for a in found
        }
        write_json(path, doc)
        anchors = doc["anchors"]
    new = [a for a in found if a not in anchors]
    stale = sorted(a for a in anchors if a not in found)
    unmapped = [
        a for a in found if a in anchors and resolve_anchor(a, anchors, rules) is None
    ]
    types = Counter(a.split(":", 1)[0] for a in found)
    print(
        f"{len(found)} anchors ("
        + ", ".join(f"{t} {n}" for t, n in sorted(types.items()))
        + f"), {len(anchors)} in {ANCHORS}"
    )
    for title, names in (
        ("not in anchors.json", new),
        ("in anchors.json, not in the APK", stale),
        ("unmapped", unmapped),
    ):
        if names:
            print(f"\n{title} ({len(names)}):")
            for a in names:
                print(f"  {a}\t{found.get(a, '')}")
    return 0 if args.write or not (new or stale or unmapped) else 1


# ----------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    root = argparse.ArgumentParser(add_help=False)
    root.add_argument(
        "--root", default=str(ROOT), help="repository root (default: this checkout)"
    )
    chk = sub.add_parser(
        "check", parents=[root], help="check the ledger and anchors.json"
    )
    chk.add_argument(
        "--build",
        action="store_true",
        help="also list the build rows by domain and verify",
    )
    chk.set_defaults(fn=cmd_check)
    air = sub.add_parser(
        "air", parents=[root], help="decoded captures vs inventory-air.json"
    )
    air.add_argument(
        "files", nargs="+", help="`mesh_sniff.py decode --json` output (NDJSON)"
    )
    air.set_defaults(fn=cmd_air)
    apk = sub.add_parser("apk", parents=[root], help="jadx anchors vs anchors.json")
    apk.add_argument("jadx", help="jadx output directory (sources/, resources/)")
    apk.add_argument(
        "--write",
        action="store_true",
        help="rewrite the anchors list, keeping existing mappings",
    )
    apk.set_defaults(fn=cmd_apk)
    return ap


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
