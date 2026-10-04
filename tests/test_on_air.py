"""tools/on_air.py: every "unverified on air" marker with the symbol holding it, per file type, and the committed tree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import on_air

ROOT = Path(__file__).resolve().parent.parent
CHECKLIST = ROOT / "docs" / "on-air-sweep.md"
# the rows review 4 brief 30 names, each carrying its on-air procedure in the ledger (the range rows only the part the
# sweep's CLI probe could not settle, Home Assistant's own Set)
BRIEF_ROWS = (
    "air:access:8264.missing",
    "msg:op:8241.note",
    "msg:op:826b.note",
    "prod:param:lamp:tunable-white-range.note",
    "net:uc:createthreshold.note",
    "net:uc:togglethreshold.note",
    "net:uc:deletethreshold.note",
)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def found(root: Path, rel: str, text: str) -> list[tuple[int, str, str]]:
    write(root, rel, text)
    return [(m.line, m.symbol, m.kind) for m in on_air.scan(root, [rel])]


# ----------------------------------------------------------------------------- phrases


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Unverified on air.", "air"),
        ("unverified on the air", "air"),
        ("**unverified on\n  air**", "air"),
        ("# Unverified on\n# air", "air"),
        ("Not yet tried on a real device.", "air"),
        ("not yet tried on a real socket", "air"),
        ("Not yet tried on the installation.", "air"),
        ("derived from the Bluetooth Mesh rules, **not yet seen on air**", "air"),
        ("bits 3 and 4 have not been seen on air", "air"),
        ("its bit 3 has not been seen on air", "air"),
        ("UNVERIFIED ON HARDWARE", "hardware"),
        ("**Unverified on a real room thermostat.**", "hardware"),
        ("**Unverified on real blinds — please report.**", "hardware"),
        ("**not yet seen working on hardware** (please report)", "hardware"),
        ("(experimental, unverified with the app)", "app"),
    ],
)
def test_marker_phrases(text: str, kind: str) -> None:
    assert [k for _, k in on_air.matches(text)] == [kind]


def test_other_text_is_no_marker() -> None:
    assert on_air.matches("verified on air in review 3; unverified assumption") == []


def test_context_cuts_a_long_line_around_the_marker() -> None:
    short = "x = 1\n# Unverified on air.\ny = 2"
    assert on_air.context(short, short.index("Unverified")) == "# Unverified on air."
    long = "a" * 300 + " unverified on air " + "b" * 300
    cut = on_air.context(long, long.index("unverified"))
    assert cut.startswith("…")
    assert cut.endswith("…")
    assert "unverified on air" in cut
    assert len(cut) <= on_air.CONTEXT + 2


# ----------------------------------------------------------------------------- symbols


def test_python_symbols(tmp_path: Path) -> None:
    text = (
        '"""Module docstring: unverified on air."""\n'
        "\n"
        "# a comment block over a constant,\n"
        "# unverified on air\n"
        "LIMIT = 3\n"
        "\n"
        "\n"
        "class Hub:\n"
        "    def run(self) -> None:\n"
        '        """Run it. Unverified on\n'
        '        air."""\n'
        "\n"
        "\n"
        "# a trailing comment, unverified on air\n"
    )
    assert found(tmp_path, "pkg/mod.py", text) == [
        (1, "<module>", "air"),
        (4, "LIMIT", "air"),
        (10, "Hub.run", "air"),
        (14, "<module>", "air"),
    ]


def test_markdown_symbols(tmp_path: Path) -> None:
    text = (
        "Unverified on air before any heading.\n"
        "# Title\n"
        "## Section\n"
        "\n"
        "```\n"
        "# not a heading\n"
        "```\n"
        "A paragraph, unverified on air.\n"
        "\n"
        "### Sub\n"
        "- **A long bullet whose first words name the item** and go on well past\n"
        "  the cut, unverified on air.\n"
        "- Short bullet, unverified on air.\n"
        "## Next\n"
        "Unverified on hardware.\n"
    )
    assert found(tmp_path, "docs/page.md", text) == [
        (1, "<top>", "air"),
        (8, "Section", "air"),
        (
            12,
            "Section > Sub > **A long bullet whose first words name the item** and go on …",
            "air",
        ),
        (13, "Section > Sub > Short bullet, unverified on air.", "air"),
        (15, "Next", "hardware"),
    ]


def test_changelog_counts_the_unreleased_section_only(tmp_path: Path) -> None:
    text = (
        "# Changelog\n"
        "## 1.2.0 (unreleased)\n"
        "### Fixed\n"
        "- A fix. Unverified on air.\n"
        "## 1.1.0\n"
        "- An old fix. Unverified on air.\n"
    )
    assert found(tmp_path, "CHANGELOG.md", text) == [
        (4, "1.2.0 (unreleased) > Fixed > A fix. Unverified on air.", "air")
    ]
    last = "## 1.0.0\n- Old. Unverified on air.\n## 2.0.0 (unreleased)\n- New. Unverified on air.\n"
    assert [line for line, _, _ in found(tmp_path, "CHANGELOG.md", last)] == [4]


def test_yaml_and_json_symbols(tmp_path: Path) -> None:
    yaml = (
        "# unverified on air, before any key\n"
        "rules:\n"
        "  reauth:\n"
        "    status: done\n"
        "    comment: Unverified on air.\n"
        "  others:\n"
        "    - name: x\n"
    )
    assert found(tmp_path, "q.yaml", yaml) == [
        (1, "<top>", "air"),
        (5, "rules.reauth.comment", "air"),
    ]
    strings = {
        "services": {"dim": {"description": "Dims. Not yet tried on a real device."}}
    }
    text = json.dumps(strings, indent=2)
    assert found(tmp_path, "strings.json", text) == [
        (4, "services.dim.description", "air")
    ]


def test_ledger_rows(tmp_path: Path) -> None:
    rows = [
        {"id": "msg:op:1", "status": "implemented", "note": "Unverified on air: do X."},
        {
            "id": "msg:op:2",
            "status": "partial",
            "missing": "on-air check: do Y; unverified on air until then",
        },
        {"id": "msg:op:3", "status": "partial", "missing": "a test"},
        {"id": 'odd"id', "status": "implemented", "note": "Unverified on hardware."},
    ]
    text = json.dumps({"domain": "msg", "rows": rows}, indent=1)
    assert found(tmp_path, "docs/parity/ledger-msg.json", text) == [
        (5, "msg:op:1.note", "air"),
        (10, "msg:op:2.missing", "air"),
        (20, 'odd"id.note', "hardware"),
    ]
    # an id written in another escape than json.dumps would write it is not found: the row's marker is at line 1
    raw = '{"rows": [\n{"id": "a\\/b", "note": "Unverified on air."}]}'
    assert found(tmp_path, "docs/parity/ledger-x.json", raw) == [(1, "a/b.note", "air")]


def test_files_skip_links_caches_other_types_and_the_checklist(tmp_path: Path) -> None:
    keep = write(tmp_path, "pkg/a.py", "# unverified on air\nX = 1\n")
    write(tmp_path, "pkg/__pycache__/a.py", "# unverified on air\n")
    write(tmp_path, "pkg/notes.txt", "unverified on air\n")
    write(tmp_path, "docs/on-air-sweep.md", "unverified on air\n")
    (tmp_path / "pkg" / "alias.py").symlink_to(keep)
    assert on_air.files(tmp_path, ["pkg", "docs/on-air-sweep.md"]) == [keep]
    assert [m.symbol for m in on_air.scan(tmp_path, ["pkg/a.py"])] == ["X"]


# ----------------------------------------------------------------------------- the command


def test_main_text_json_and_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, "a.md", "## S\nUnverified on air.\n\nUnverified on hardware.\n")
    assert on_air.main(["--root", str(tmp_path), "a.md"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "a.md:2: S [air] Unverified on air.",
        "a.md:4: S [hardware] Unverified on hardware.",
        "2 markers in 1 files: 1 air, 1 hardware",
    ]
    assert (
        on_air.main(["--root", str(tmp_path), "--json", "--kind", "hardware", "a.md"])
        == 0
    )
    docs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert docs == [
        {
            "path": "a.md",
            "line": 4,
            "symbol": "S",
            "kind": "hardware",
            "text": "Unverified on hardware.",
            "cite": None,
        }
    ]
    write(tmp_path, "b.md", "nothing here\n")
    assert on_air.main(["--root", str(tmp_path), "b.md"]) == 0
    assert capsys.readouterr().out == "0 markers in 0 files: none\n"


def test_citations_and_uncovered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        tmp_path,
        "pkg/mod.py",
        'def f() -> None:\n    """Unverified on air."""\n\n\ndef g() -> None:\n    """Unverified on air."""\n',
    )
    strings = json.dumps({"issues": {"x": {"title": "Unverified on air."}}}, indent=2)
    write(tmp_path, "pkg/strings.json", strings)
    write(tmp_path, "pkg/translations/en.json", strings)
    rows = {
        "rows": [
            {"id": "net:uc:a", "note": "Unverified on air."},
            {"id": "net:uc:b", "note": "Unverified on air."},
        ]
    }
    write(tmp_path, "docs/parity/ledger-net.json", json.dumps(rows, indent=1))
    write(tmp_path, "docs/page.md", "Unverified on air.\n")
    cites = [m.cite for m in on_air.scan(tmp_path, ["pkg", "docs"])]
    assert (
        cites
        == [
            None,
            "net:uc:a",
            "net:uc:b",
            "pkg/mod.py::f",
            "pkg/mod.py::g",
            "pkg/strings.json::issues.x.title",
            "pkg/strings.json::issues.x.title",  # the translation is cited by its strings.json twin
        ]
    )
    checklist = write(
        tmp_path,
        "check.md",
        "Covered: `pkg/mod.py::f`, `net:uc:a`, `pkg/strings.json::issues.x.title`; "
        "a bare pkg/mod.py::g or net:uc:b does not count.\n",
    )
    args = [
        "--root",
        str(tmp_path),
        "--uncovered",
        str(checklist),
        "--json",
        "pkg",
        "docs",
    ]
    assert on_air.main(args) == 0
    left = [json.loads(line)["cite"] for line in capsys.readouterr().out.splitlines()]
    assert left == ["net:uc:b", "pkg/mod.py::g"]


# ----------------------------------------------------------------------------- the committed tree


@pytest.fixture(scope="module")
def committed() -> list[on_air.Marker]:
    return on_air.scan(ROOT)


def test_committed_tree_lists_the_brief_rows(committed: list[on_air.Marker]) -> None:
    symbols = {m.symbol for m in committed if m.path.startswith("docs/parity/")}
    assert set(BRIEF_ROWS) <= symbols


def test_committed_tree_skips_released_history_and_the_checklist(
    committed: list[on_air.Marker],
) -> None:
    paths = {m.path for m in committed}
    assert paths >= {
        "docs/ha-integration.md",
        "docs/user/buttons-and-automations.md",  # the free-rocker recipe
        "docs/dev/architecture.md",  # the developer notes moved out of the reference
        # a blueprint's description
        "blueprints/automation/junghome_ble/rocker_light_control.yaml",
    }
    assert "docs/on-air-sweep.md" not in paths
    assert (
        "docs/research/README.md" not in paths
    )  # the project's log: a record, like the review plans
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    released = [
        n
        for n, line in enumerate(lines, 1)
        if line.startswith("## ") and "(unreleased)" not in line
    ]
    assert released  # 1.0.0 is out, and its section holds markers of its time
    assert all(m.line < released[0] for m in committed if m.path == "CHANGELOG.md")
    # the unreleased section's markers are read while there is one with a marker (none right after a release)
    unreleased = "\n".join(
        "\n".join(lines[start - 1 : end])
        for start, end in on_air.unreleased_spans(lines)
    )
    if any(kind.search(unreleased) for kind in on_air.KINDS.values()):
        assert "CHANGELOG.md" in paths


def test_the_checklist_cites_the_brief_rows_and_findings() -> None:
    text = CHECKLIST.read_text(encoding="utf-8")
    for symbol in BRIEF_ROWS:
        assert f"`{symbol.rsplit('.', 1)[0]}`" in text, symbol
    for finding in ("F10", "F4", "F15"):
        assert finding in text
