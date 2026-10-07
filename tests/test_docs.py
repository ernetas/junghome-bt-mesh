"""The Markdown documentation's internal links: every relative link and `#anchor` resolves, as GitHub renders them.

HACS shows `README.md`, GitHub the rest; a link to a moved page or a renamed heading breaks silently there. So every
Markdown file at the top of the repository and under `docs/` is read, and each link that is not a URL must name a
file or directory that exists and, with a `#fragment`, a heading of that page under GitHub's anchor rules (the
heading's text in lower case, punctuation dropped, spaces as hyphens, a repeated heading numbered `-1`, `-2`, …) or
an explicit `<a id>`. No network access: URLs are not followed.

The reference's headings are also pinned: links from other pages, the repair issues' *learn more* links and readers'
bookmarks point at them, so renaming one is a deliberate change of this list.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# an inline link or image: [text](target "title"), the target optionally in <>
LINK = re.compile(
    r'(?<!\\)!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)'
)
REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
EXPLICIT_ANCHOR = re.compile(r'<a\s+(?:id|name)="([^"]+)"')
URL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
# a link to a file of this repository on GitHub: the README's are all absolute (HACS shows it, and resolves nothing
# relative), so they are checked as the relative links they stand for
REPOSITORY = "https://github.com/ernetas/junghome-bt-mesh/blob/main/"


def in_tree(root: Path, page: Path, target: str) -> str | None:
    """The target relative to `page` (in the tree at `root`): a relative link, or one to this repository; None else."""
    if target.startswith(REPOSITORY):
        up = [".."] * len(page.relative_to(root).parent.parts)
        return Path(*up, target.removeprefix(REPOSITORY)).as_posix()
    return None if URL.match(target) else target


def markdown_files(root: Path) -> list[Path]:
    """The top-level Markdown files and everything under `docs/`."""
    return sorted({*root.glob("*.md"), *(root / "docs").rglob("*.md")})


def slug(heading: str) -> str:
    """GitHub's anchor for a heading's text: links and markup reduced to their text, lower case, no punctuation."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link: its text
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML
    text = html.unescape(text.replace("`", "").replace("*", ""))
    text = re.sub(r"[^\w\- ]", "", text.lower())
    return text.replace(" ", "-")


def lines_outside_code(text: str) -> list[tuple[int, str]]:
    """(line number, line) of every line outside fenced code blocks, with inline code removed."""
    out: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append((number, re.sub(r"`[^`]*`", "", line)))
    return out


def anchors(text: str) -> set[str]:
    """Every anchor a page offers: its headings' slugs (repeats numbered) and explicit `<a id>` / `<a name>`."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        found.update(EXPLICIT_ANCHOR.findall(line))
        heading = HEADING.match(line)
        if heading is not None:
            base = slug(heading.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def links(text: str) -> list[tuple[int, str]]:
    """(line number, target) of every inline link, image and reference definition outside code."""
    out: list[tuple[int, str]] = []
    for number, line in lines_outside_code(text):
        out.extend((number, m.group(1)) for m in LINK.finditer(line))
        definition = REFERENCE_DEFINITION.match(line)
        if definition is not None:
            out.append((number, definition.group(1)))
    return out


def broken_links(root: Path, files: list[Path]) -> list[str]:
    """`path:line: target (why)` for every relative link of `files` that does not resolve."""
    cache: dict[Path, set[str]] = {}
    broken: list[str] = []
    for page in files:
        for number, link in links(page.read_text(encoding="utf-8")):
            if (target := in_tree(root, page, link)) is None:
                continue
            path, _, fragment = target.partition("#")
            dest = (page.parent / path).resolve() if path else page
            where = f"{page.relative_to(root)}:{number}: {link}"
            if not dest.exists():
                broken.append(f"{where} (no such file)")
            elif fragment and dest.suffix == ".md":
                if dest not in cache:
                    cache[dest] = anchors(dest.read_text(encoding="utf-8"))
                if fragment not in cache[dest]:
                    broken.append(f"{where} (no such heading)")
    return broken


# ----------------------------------------------------------------------------- the rules themselves


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        (
            "Device parameters (number, select, switch, button)",
            "device-parameters-number-select-switch-button",
        ),
        (
            'Repair issue "The JUNG HOME app overrode a change Home Assistant made on …"',
            "repair-issue-the-jung-home-app-overrode-a-change-home-assistant-made-on-",
        ),
        (
            'Repair issue "Another client uses Home Assistant\'s JUNG HOME address"',
            "repair-issue-another-client-uses-home-assistants-jung-home-address",
        ),
        (
            "Actions: adding and removing devices (experimental)",
            "actions-adding-and-removing-devices-experimental",
        ),
        ("The `jhmesh` library", "the-jhmesh-library"),
        ("**Bold** and [a link](x.md) &amp; more", "bold-and-a-link--more"),
        ("Schnellstart (Deutsch) — Übersicht", "schnellstart-deutsch--übersicht"),
        ("snake_case stays", "snake_case-stays"),
    ],
)
def test_slug_follows_github(heading: str, anchor: str) -> None:
    assert slug(heading) == anchor


def test_the_checker_finds_what_is_broken(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "dir").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        '# Page\n\n## Same\n\n## Same\n\n<a id="custom"></a>\n\n```\n[not a link](nowhere.md)\n```\n'
        "[ok](#same-1) [ok](#custom) [dir](dir/) [url](https://example.org/x.md#y) `[code](nowhere.md)`\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "[ok](docs/page.md#same) ![img](docs/page.md)\n"
        "[gone](docs/gone.md) [bad anchor](docs/page.md#nope) [self](#nope-here)\n"
        "[ref]: docs/also-gone.md\n",
        encoding="utf-8",
    )
    files = markdown_files(tmp_path)
    assert files == [tmp_path / "README.md", tmp_path / "docs" / "page.md"]
    assert broken_links(tmp_path, files) == [
        "README.md:2: docs/gone.md (no such file)",
        "README.md:2: docs/page.md#nope (no such heading)",
        "README.md:2: #nope-here (no such heading)",
        "README.md:3: docs/also-gone.md (no such file)",
    ]


# ----------------------------------------------------------------------------- the committed tree


def test_every_relative_link_and_anchor_resolves() -> None:
    files = markdown_files(ROOT)
    assert ROOT / "README.md" in files
    assert ROOT / "docs" / "user" / "README.md" in files
    assert broken_links(ROOT, files) == []


def test_the_pages_link_to_each_other() -> None:
    """Every page of the user guide, the German quick start and the developer docs is reachable from the README."""
    reachable: set[Path] = set()
    todo = [ROOT / "README.md"]
    while todo:
        page = todo.pop()
        if page in reachable or page.suffix != ".md":
            continue
        reachable.add(page)
        for _number, link in links(page.read_text(encoding="utf-8")):
            target = in_tree(ROOT, page, link)
            path = (target or "").partition("#")[0]
            if path:
                dest = (page.parent / path).resolve()
                if dest.is_file() and dest.is_relative_to(ROOT / "docs"):
                    todo.append(dest)
    pages = [
        p
        for folder in ("user", "de", "dev", "research")
        for p in sorted((ROOT / "docs" / folder).glob("*.md"))
    ]
    assert pages
    assert [p.relative_to(ROOT).as_posix() for p in pages if p not in reachable] == []


def test_manifest_documentation_is_the_user_guide() -> None:
    manifest = json.loads(
        (ROOT / "custom_components" / "junghome_ble" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    prefix = "https://github.com/ernetas/junghome-bt-mesh/blob/main/"
    assert manifest["documentation"] == prefix + "docs/user/README.md"
    assert (ROOT / manifest["documentation"].removeprefix(prefix)).is_file()


def test_yaml_examples_parse() -> None:
    """Every YAML example of the user guide and the reference is valid YAML (one document per `---`)."""
    pages = [
        *sorted((ROOT / "docs" / "user").glob("*.md")),
        ROOT / "docs" / "ha-integration.md",
    ]
    blocks = 0
    for page in pages:
        for block in re.findall(
            r"^```yaml\n(.*?)^```$",
            page.read_text(encoding="utf-8"),
            re.MULTILINE | re.DOTALL,
        ):
            list(yaml.safe_load_all(block))
            blocks += 1
    assert blocks > 10


# what links, the repair issues and bookmarks point at in the reference: renaming a heading is a change of this list
REFERENCE_ANCHORS = (
    "supported-devices",
    "supported-functionality",
    "light",
    "switch",
    "binary-sensor",
    "cover",
    "climate",
    "sensor",
    "event",
    "device-triggers",
    "scene",
    "device-parameters-number-select-switch-button",
    "devices-and-areas",
    "inserts-and-key-layouts",
    "firmware",
    "prerequisites",
    "installation",
    "configuration-parameters",
    "options",
    "home-assistant-as-a-provisioner-experimental",
    "reconfiguration",
    "migrating-from-the-gateway-integration",
    "data-updates",
    "use-cases",
    "automation-examples",
    "actions-triggers-and-conditions",
    "actions-rooms-and-key-connections",
    "following-a-change-without-a-reload",
    "actions-scenes",
    "actions-schedules",
    "actions-thresholds",
    "actions-network-audit",
    "actions-gateway-access-requests",
    "actions-adding-and-removing-devices-experimental",
    "known-limitations",
    "troubleshooting",
    "no-node-of-this-mesh-network-is-currently-visible-over-bluetooth",
    "entities-keep-switching-between-available-and-unavailable",
    "one-entity-is-unavailable-or-shows-an-unknown-state",
    "commands-are-accepted-but-nothing-happens",
    "that-address-belongs-to-a-node-in-the-mesh",
    "the-mesh-export-could-not-be-read",
    "the-networks-keys-were-renewed-after-the-export-was-made",
    "the-export-belongs-to-a-different-mesh-network",
    "this-mesh-is-already-set-up-as-another-entry",
    "repair-issue-jung-home-mesh-keys-are-changing",
    "repair-issue-jung-home-mesh-keys-have-changed",
    "repair-issue-jung-home-devices-missing-from-the-export",
    "repair-issue-jung-home-push-buttons-with-another-insert-than-in-the-export",
    "repair-issue-jung-home-devices-with-a-wrong-clock",
    "repair-issue-jung-home-pucks-have-no-time-keeper",
    "repair-issue-a-jung-home-change-on--was-interrupted",
    "repair-issue-the-jung-home-app-overrode-a-change-home-assistant-made-on-",
    "repair-issue-devices-still-hold-a-deleted-jung-home-scene-on-",
    "repair-issue-no-bluetooth-for-the-jung-home-mesh",
    "repair-issue-jung-home-devices-ignore-home-assistant",
    "repair-issue-another-client-uses-home-assistants-jung-home-address",
    "repair-issue-jung-home-sequence-numbers-cannot-be-saved",
    "repair-issue-jung-home-mesh-is-at-another-iv-index",
    "entities-go-unavailable-every-few-minutes",
    "repair-issue-jung-home-gateway-certificate-changed",
    "repair-issue-device-name-not-passed-on-to-the-jung-home-app",
    "repair-issue-a-device-home-assistant-added-is-not-recorded",
    "repair-issue-jung-home-device-keys-cannot-be-saved",
    "repair-issue-devices-home-assistant-added-missed-the-new-network-key",
    "repair-issue-jung-home-gateway-no-longer-accepts-home-assistant",
    "repair-issue-sequence-numbers-of-the-jung-home-mesh--lost",
    "repair-issue-jung-home-mesh-sequence-numbers-running-low",
    "repair-issue-home-assistants-jung-home-address-is-taken",
    "repair-issue-home-assistants-jung-home-address-may-be-handed-out",
    "repair-issue-jung-home-export-not-handed-to-the-gateway",
    "repair-issue-take-over-the-jung-home-gateway-integrations-entities",
    "repair-issue-two-entries-cover-the-same-jung-home-mesh",
    "the-access-request-was-not-approved-in-time",
    "the-gateway-holds-no-network-export",
    "enabling-debug-logging",
    "diagnostics",
    "removal",
    "developer-notes",
)


def test_the_reference_keeps_its_headings() -> None:
    offered = anchors((ROOT / "docs" / "ha-integration.md").read_text(encoding="utf-8"))
    assert [a for a in REFERENCE_ANCHORS if a not in offered] == []


def test_the_readme_links_absolutely() -> None:
    """HACS renders the README inside Home Assistant (`render_readme`), where a relative link leads nowhere."""
    relative = [
        f"README.md:{number}: {target}"
        for number, target in links((ROOT / "README.md").read_text(encoding="utf-8"))
        if not URL.match(target) and not target.startswith("#")
    ]
    assert relative == []
    assert any(
        t.startswith(REPOSITORY)
        for _n, t in links((ROOT / "README.md").read_text(encoding="utf-8"))
    )


def test_a_link_to_the_repository_is_checked_as_a_file_of_it(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
    page = tmp_path / "docs" / "b.md"
    page.write_text(
        f"[ok]({REPOSITORY}docs/a.md#a) [gone]({REPOSITORY}docs/gone.md) [web](https://example.org/x)\n",
        encoding="utf-8",
    )
    assert broken_links(tmp_path, [page]) == [
        f"docs/b.md:1: {REPOSITORY}docs/gone.md (no such file)"
    ]


# The gateway integration is the one listed as *JUNG HOME* under Devices & services; this one by its name.
OLD_NAME_PATH = re.compile(r"JUNG HOME(?! Bluetooth Mesh)[*_]*\s*→")


def test_no_page_sends_the_owner_to_this_integration_by_the_gateway_integrations_name() -> (
    None
):
    offending = [
        str(page.relative_to(ROOT))
        for page in markdown_files(ROOT)
        # the review records and the changelog quote what the texts said when they were written
        if page.name != "CHANGELOG.md"
        and not any(part.startswith("review-") for part in page.relative_to(ROOT).parts)
        and OLD_NAME_PATH.search(re.sub(r"\s+", " ", page.read_text(encoding="utf-8")))
    ]
    assert offending == []
