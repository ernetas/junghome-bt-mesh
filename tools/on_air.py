#!/usr/bin/env python3
"""List every "unverified on air" marker of the tree, each with the symbol that holds it (docs/on-air-sweep.md).

    tools/on_air.py                      # every marker: path:line: symbol [kind] text
    tools/on_air.py --kind air           # only one kind of phrase (air, hardware, app)
    tools/on_air.py --json               # one JSON object per marker (path, line, symbol, kind, text, cite)
    tools/on_air.py docs/user custom_components/junghome_ble/coordinator.py   # only these
    tools/on_air.py --uncovered docs/on-air-sweep.md   # markers outside the docs the checklist does not cite

What counts as a marker: the phrases the conventions use for behaviour nobody has seen working on an installation
(`unverified on air`, `not yet tried on a real device`, ...; kind `air`), for hardware the maintainer lacks
(`unverified on hardware`, `on a real room thermostat`, ...; kind `hardware`) and for files no app has imported yet
(`unverified with the app`; kind `app`), plus a parity-ledger row whose `missing` text is only its `on-air check:`.
The symbol is what the parity ledger would cite (`docs/parity/README.md`): a Python function, class or
assignment (a comment block names the statement under it, a module docstring `<module>`), a JSON key's dotted
path, a ledger row's id and field, a YAML key path, a Markdown heading path. In `CHANGELOG.md` only the unreleased
section counts: a released section is history and is never edited. A marker's citation (`cite`) is how the
checklist names it: `path::symbol` for code, strings and YAML (a `translations/en.json` key by its `strings.json`
twin), the row id for a ledger row; a Markdown marker has none, it restates what the code says.

The second pass of the on-air sweep removes a marker once its check passed; run this before and after to see what
is left. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import parity

ROOT = Path(__file__).resolve().parent.parent
# where markers live: the code, the reference, the user guide, the developer notes, the German quick start, the ledger
# and the unreleased changelog section; the review plans, briefs, gap analyses and the research index are records of
# their time, and the sweep's own checklist quotes the phrases
SCAN = (
    "custom_components/junghome_ble",
    "tools",
    "docs/ha-integration.md",
    "docs/user",
    "docs/dev",
    "docs/de",
    "docs/parity",
    "CHANGELOG.md",
)
SKIP = frozenset({"tools/on_air.py", "docs/on-air-sweep.md", "docs/parity/README.md"})
SUFFIXES = frozenset({".py", ".json", ".md", ".yaml"})
CHANGELOG = "CHANGELOG.md"
STRINGS, TRANSLATION = "/strings.json", "/translations/en.json"
LEDGER = re.compile(r"(?:^|/)ledger-[^/]+\.json$")
# the gap between two words of a phrase: a line break, a comment's `#`, Markdown emphasis
GAP = r"[\s#*]+"


def phrase(pattern: str) -> str:
    """`pattern` with every space free to be a line break (a wrapped docstring, comment or Markdown paragraph)."""
    return GAP.join(pattern.split(" "))


KINDS: dict[str, re.Pattern[str]] = {
    "air": re.compile(
        "|".join(
            phrase(p)
            for p in (
                r"unverified on (?:the )?air",
                r"not yet (?:seen|tried|heard) on (?:the )?air",
                r"not yet tried on (?:a real (?:device|socket)|the installation)",
                r"ha(?:s|ve) not been seen on air",
            )
        ),
        re.IGNORECASE,
    ),
    "hardware": re.compile(
        "|".join(
            phrase(p)
            for p in (
                r"unverified on (?:hardware|a real \w+(?: thermostat)?|real \w+)",
                r"not yet seen working on hardware",
            )
        ),
        re.IGNORECASE,
    ),
    "app": re.compile(phrase("unverified with the app"), re.IGNORECASE),
}
ON_AIR_CHECK = "on-air check:"
HEADING = re.compile(r"(?P<level>#{1,6})\s+(?P<title>.+?)\s*#*$")
YAML_KEY = re.compile(r"(?P<indent>\s*)(?:-\s+)?(?P<key>[\w.-]+):")
LIST_ITEM = re.compile(r"\s*(?:[-*]|\d+\.)\s+(?P<lead>.*)")
ITEM_LEAD = 60
CONTEXT = 120


@dataclass(frozen=True)
class Marker:
    """One marker: where it is, the symbol holding it, which kind of check it waits for and the text around it."""

    path: str
    line: int
    symbol: str
    kind: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.symbol} [{self.kind}] {self.text}"

    @property
    def cite(self) -> str | None:
        """How the checklist cites this marker; None for a Markdown one."""
        if self.path.endswith(".md"):
            return None
        if LEDGER.search(self.path):
            return self.symbol.rsplit(".", 1)[0]
        return f"{self.path.replace(TRANSLATION, STRINGS)}::{self.symbol}"


def matches(text: str) -> list[tuple[int, str]]:
    """Every marker phrase in `text`: (offset, kind), in text order."""
    found = [(m.start(), kind) for kind, rx in KINDS.items() for m in rx.finditer(text)]
    return sorted(found)


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def context(text: str, pos: int) -> str:
    """The line holding `pos`, stripped; a long one (a JSON string) cut to a window around it."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start : len(text) if end < 0 else end]
    if len(line.strip()) <= CONTEXT:
        return line.strip()
    offset = pos - start
    return (
        "…" + line[max(0, offset - CONTEXT // 2) : offset + CONTEXT // 2].strip() + "…"
    )


# ----------------------------------------------------------------------------- symbols per file type


def python_symbol(text: str, line: int, spans: parity.Spans) -> str:
    """The innermost symbol holding `line`; for a comment outside every symbol, the statement under the comment."""
    held = parity.innermost_symbol(spans, line)
    if held is not None:
        return held
    lines = text.splitlines()
    below = line
    while below < len(lines) and lines[below - 1].lstrip().startswith("#"):
        below += 1
    return parity.innermost_symbol(spans, below) or "<module>"


def markdown_symbol(lines: list[str], line: int) -> str:
    """The heading path above `line` (`1.1.0 (unreleased) > Fixed — link`) and the list item it is in, if any.

    The document's title (`# …`) is left out: every line is under it. `<top>` before the first heading.
    """
    path: list[tuple[int, str]] = []
    fenced = False
    for raw in lines[: line - 1]:
        if raw.startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(raw)
        if m is not None and len(m["level"]) > 1:
            level = len(m["level"])
            path = [(lvl, title) for lvl, title in path if lvl < level]
            path.append((level, m["title"]))
    titles = [title for _, title in path]
    item = list_item(lines, line)
    return " > ".join([*titles, *([item] if item else [])]) or "<top>"


def list_item(lines: list[str], line: int) -> str | None:
    """The first words of the list item `line` belongs to (a changelog bullet), None outside a list."""
    for raw in reversed(lines[:line]):
        if not raw.strip() or HEADING.match(raw):
            return None
        m = LIST_ITEM.match(raw)
        if m is not None:
            lead = m["lead"].strip()
            if len(lead) <= ITEM_LEAD:
                return lead
            return lead[:ITEM_LEAD].rsplit(" ", 1)[0] + " …"
    return None


def yaml_symbol(lines: list[str], line: int) -> str:
    """The key path of `line` in a block-style YAML file, read from the indentation."""
    path: list[tuple[int, str]] = []
    for raw in lines[:line]:
        m = YAML_KEY.match(raw)
        if m is None:
            continue
        indent = len(m["indent"])
        path = [(i, key) for i, key in path if i < indent]
        path.append((indent, m["key"]))
    return ".".join(key for _, key in path) or "<top>"


def unreleased_spans(lines: list[str]) -> list[tuple[int, int]]:
    """The line spans of the changelog's `## … (unreleased)` sections."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for n, raw in enumerate(lines, 1):
        if raw.startswith("## "):
            if start is not None:
                spans.append((start, n - 1))
            start = n if "(unreleased)" in raw else None
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def ledger_markers(rel: str, text: str) -> list[Marker]:
    """A ledger's markers by row: a `note` or `missing` that says it, or a `missing` that is only an on-air check."""
    out: list[Marker] = []
    for row in json.loads(text).get("rows", []):
        row_id = str(row.get("id"))
        at = re.search(r'"id":\s*' + re.escape(json.dumps(row_id)), text)
        line = line_of(text, at.start()) if at else 1
        for field_name in ("missing", "note"):
            value = str(row.get(field_name, ""))
            kinds = [kind for _, kind in matches(value)]
            if field_name == "missing" and value.startswith(ON_AIR_CHECK):
                kinds.insert(0, "air")
            symbol = f"{row_id}.{field_name}"
            out.extend(
                Marker(rel, line, symbol, kind, value[:CONTEXT])
                for kind in dict.fromkeys(kinds)
            )
    return out


def file_markers(rel: str, text: str) -> list[Marker]:
    """Every marker of one file, with its symbol."""
    if rel.endswith(".json") and LEDGER.search(rel):
        return ledger_markers(rel, text)
    lines = text.splitlines()
    keep = unreleased_spans(lines) if rel == CHANGELOG else [(1, len(lines))]
    py_spans = parity.python_symbols(text, rel) if rel.endswith(".py") else {}
    json_spans = parity.json_symbols(text) if rel.endswith(".json") else {}
    out: list[Marker] = []
    for pos, kind in matches(text):
        line = line_of(text, pos)
        if not any(a <= line <= b for a, b in keep):
            continue
        if rel.endswith(".py"):
            symbol = python_symbol(text, line, py_spans)
        elif rel.endswith(".json"):
            symbol = parity.innermost_symbol(json_spans, line) or "<top>"
        elif rel.endswith(".yaml"):
            symbol = yaml_symbol(lines, line)
        else:
            symbol = markdown_symbol(lines, line)
        out.append(Marker(rel, line, symbol, kind, context(text, pos)))
    return out


def files(root: Path, targets: list[str]) -> list[Path]:
    """The files under `targets` (relative to `root`) a marker may be in; symlinks (the `jhmesh` alias) skipped."""
    out: set[Path] = set()
    for target in targets:
        base = root / target
        candidates = [base] if base.is_file() else base.rglob("*")
        for path in candidates:
            rel = path.relative_to(root).as_posix()
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix in SUFFIXES
                and rel not in SKIP
                and "__pycache__" not in rel
            ):
                out.add(path)
    return sorted(out)


def scan(root: Path, targets: list[str] | None = None) -> list[Marker]:
    """Every marker under `targets` (default SCAN), ordered by file and line."""
    out: list[Marker] = []
    for path in files(root, list(targets or SCAN)):
        rel = path.relative_to(root).as_posix()
        out.extend(file_markers(rel, path.read_text(encoding="utf-8")))
    return out


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "paths", nargs="*", help="files or directories (default: the code and docs)"
    )
    ap.add_argument(
        "--root", default=str(ROOT), help="repository root (default: this checkout)"
    )
    ap.add_argument(
        "--kind", choices=sorted(KINDS), action="append", help="only this kind"
    )
    ap.add_argument("--json", action="store_true", help="one JSON object per line")
    ap.add_argument(
        "--uncovered",
        metavar="CHECKLIST",
        help="only the markers with a citation this file does not hold (in backticks)",
    )
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    cited = None
    if args.uncovered is not None:
        cited = Path(args.uncovered).read_text(encoding="utf-8")
    found = [
        m
        for m in scan(root, args.paths or None)
        if (not args.kind or m.kind in args.kind)
        and (cited is None or (m.cite is not None and f"`{m.cite}`" not in cited))
    ]
    for marker in found:
        doc: dict[str, Any] = {**asdict(marker), "cite": marker.cite}
        print(json.dumps(doc, ensure_ascii=False) if args.json else marker)
    if not args.json:
        kinds = Counter(m.kind for m in found)
        summary = ", ".join(f"{kinds[k]} {k}" for k in sorted(kinds)) or "none"
        n_files = len({m.path for m in found})
        print(f"{len(found)} markers in {n_files} files: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
