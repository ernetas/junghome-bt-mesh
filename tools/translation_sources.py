#!/usr/bin/env python3
"""Keep `tests/translation_sources.json`: for every key of `translations/en.json`, a hash of the English the 25 other
languages were translated from.

    tools/translation_sources.py                           # refresh the record after re-translating
    tools/translation_sources.py --since REV               # ... the English changed in a commit after REV
    tools/translation_sources.py --same-meaning KEY ...    # ... KEY's English changed, its meaning did not
    tools/translation_sources.py --check                   # exit 1 when the record is not the English

`tests/test_translations.py` fails while a key's English differs from its record, so a change to the English is not
done until the translations follow. The refresh accepts a changed key only when every other language's text of it
changed too since `--since` (default `HEAD`: the work not yet committed), and names the languages that did not; a
change that keeps the meaning (a typo, a renamed label the translations already use) is accepted with
`--same-meaning`. A new key needs no proof here: the test already fails on a language that lacks it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TRANSLATIONS = ROOT / "custom_components" / "junghome_ble" / "translations"
EN = TRANSLATIONS / "en.json"
RECORD = ROOT / "tests" / "translation_sources.json"


def leaves(obj: Any, prefix: str = "") -> dict[str, str]:
    """Flatten a nested translation dict to {dotted.key: value}."""
    if not isinstance(obj, dict):
        return {prefix: obj}
    out: dict[str, str] = {}
    for key, value in obj.items():
        out.update(leaves(value, f"{prefix}.{key}" if prefix else key))
    return out


def source_hash(text: str) -> str:
    """The record's hash of one English text (16 hex digits; twelve would read as a MAC to the privacy scan)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def expected() -> dict[str, str]:
    """The record the current English calls for."""
    english = leaves(json.loads(EN.read_text(encoding="utf-8")))
    return {key: source_hash(english[key]) for key in sorted(english)}


def _at(rev: str, path: Path) -> dict[str, str]:
    """A translation file's leaves at `rev` (empty when the file did not exist)."""
    relative = path.relative_to(ROOT).as_posix()
    shown = subprocess.run(  # noqa: S603  # no shell; the revision is the developer's own argument
        ["git", "show", f"{rev}:{relative}"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return leaves(json.loads(shown.stdout)) if shown.returncode == 0 else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="only compare, write nothing"
    )
    parser.add_argument(
        "--since", default="HEAD", help="the revision the re-translation started from"
    )
    parser.add_argument(
        "--same-meaning",
        nargs="*",
        default=[],
        metavar="KEY",
        help="changed English, same meaning",
    )
    args = parser.parse_args(argv)

    record = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else {}
    wanted = expected()
    changed = sorted(
        key for key in wanted if key in record and record[key] != wanted[key]
    )
    if args.check:
        stale = changed + sorted(set(wanted) ^ set(record))
        for key in stale:
            print(f"stale: {key}")
        return 1 if stale else 0

    if args.since.startswith("-"):
        parser.error("--since takes a revision")
    unknown = sorted(set(args.same_meaning) - set(changed))
    if unknown:
        print(
            f"--same-meaning names keys whose English did not change: {unknown}",
            file=sys.stderr,
        )
        return 2
    behind: dict[str, list[str]] = {}
    for path in sorted(p for p in TRANSLATIONS.glob("*.json") if p != EN):
        now, before = (
            leaves(json.loads(path.read_text(encoding="utf-8"))),
            _at(args.since, path),
        )
        for key in changed:
            if (
                key not in args.same_meaning
                and key in before
                and now.get(key) == before[key]
            ):
                behind.setdefault(key, []).append(path.stem)
    if behind:
        for key, languages in behind.items():
            print(
                f"{key}: English changed, not re-translated since {args.since} in {', '.join(languages)}",
                file=sys.stderr,
            )
        print(
            "re-translate them (or pass --same-meaning KEY, or --since the revision before the English changed)",
            file=sys.stderr,
        )
        return 1
    RECORD.write_text(json.dumps(wanted, indent=2) + "\n", encoding="utf-8")
    print(
        f"{RECORD.relative_to(ROOT)}: {len(changed)} changed, {len(set(wanted) - set(record))} new, "
        f"{len(set(record) - set(wanted))} gone"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
