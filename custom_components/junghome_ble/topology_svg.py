"""The mesh's shape as a picture: a topology snapshot rendered as SVG, without Home Assistant.

A snapshot (`Topology`) is what the *Mesh topology* image shows and the diagnostics' `topology` lists: Home
Assistant (its own address, whether it has a link), the node it is connected through (the link's proxy) and every
node of the export with its name, room, features (relay, proxy, friend, low power, from the export), the hops of its
last heartbeat, whether it answers and, for one that does not or sleeps, when it was last heard. `mesh_topology.py`
takes it from the hub; equal snapshots render the same bytes, so the entity redraws only when it changes.

The picture (`render_svg`) puts Home Assistant at the top with the link's proxy next to it, and the other nodes in
bands by hop count below (*Hops: 1*, *Hops: 2*, …; `HOP_BANDS` and more share the last), the nodes whose hops are
not known in a band of their own under a dashed line. Nothing claims a path the mesh does not report: the only line
drawn is Home Assistant's link to its proxy. Each node's state is a marker shape *and* colour *and* a word (reachable,
unreachable, asleep, not known); its features are lettered badges *and* a line of words; the link's proxy has a thick
border and says so; a legend explains all of it. The layout is sorted (band, name, address), so the picture does not
jump between updates; at most `MAX_COLUMNS` boxes a row, so its size grows with the node count alone (bounded for
large meshes). Names are stripped of characters XML cannot hold, cut to fit and escaped. The colours are the light
ones as attributes and the dark ones under `prefers-color-scheme: dark`, on a neutral background with a border of its
own, so the picture reads on either theme.

Its words are `TEXTS`, English, or the mapping `render_svg` is given: the entity passes the translations of the
server's language (`strings.json` `common.topology_*`, the same names). A count is a number after a word ("Hops: 3",
"reachable: 4"), never inside a sentence: the backend has no plural rules, and the integration's languages need from
one form to four. Every text is cut to the room it has, a wide (CJK) character counting twice; the footnote wraps
once. "Home Assistant" is a name, the same in every language.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

# the geometry, in px
MARGIN: Final = 16
BOX_W: Final = 220
BOX_H: Final = 92
GAP_X: Final = 12
GAP_Y: Final = 14
LINK_GAP: Final = 96  # between Home Assistant's box and its proxy's: room for the link's line and its word
BAND_HEAD: Final = 24  # a band's heading above its boxes
BAND_GAP: Final = 18
MIN_COLUMNS: Final = (
    3  # the top band (two boxes and the link) and the legend need this width
)
MAX_COLUMNS: Final = 4
HOP_BANDS: Final = 8  # nodes this many hops away and more share one band
# what fits, in narrow characters (a wide one counts twice): left of a box's badge column (bold 12 px for the name,
# 11 px for the rest), between Home Assistant and its proxy, across the picture, in a legend column
NAME_CHARS: Final = 23
LINE_CHARS: Final = 29
LINK_CHARS: Final = 14
TITLE_CHARS: Final = 60
HEAD_CHARS: Final = 80
LEGEND_CHARS: Final = 48
WIDE_CHARS: Final = 96
BADGE_CHARS: Final = 2  # a letter, or one wide character
LEGEND_LINE: Final = 18

# colour name → (light, dark): the light one as the element's attribute, the dark one in the style's media query
PALETTE: Final = {
    "bg": ("#f6f7f9", "#1b1c1f"),
    "frame": ("#c7c9cf", "#45474d"),
    "card": ("#ffffff", "#26282c"),
    "edge": ("#9a9ca4", "#6b6e75"),
    "text": ("#1d1f23", "#e7e8ea"),
    "muted": ("#5d6068", "#a3a6ad"),
    "ok": ("#1a7f37", "#3fb950"),
    "bad": ("#c62828", "#f47067"),
    "sleep": ("#6e7781", "#9198a1"),
    "link": ("#0969da", "#58a6ff"),
    "relay": ("#9a6700", "#d29922"),
    "proxy": ("#0969da", "#58a6ff"),
    "friend": ("#8250df", "#a371f7"),
    "low_power": ("#1b7c83", "#39c5cf"),
    "badge_text": ("#ffffff", "#0d1117"),
}
# the features a node has (export `features`, 1 = enabled); the word is `feature_<key>`, the badge's letter `badge_<key>`
FEATURES: Final = ("relay", "proxy", "friend", "low_power")
# a node's state → its marker's colour; the word is `state_<state>`, the legend's `legend_<state>`
STATES: Final = {
    "reachable": "ok",
    "unreachable": "bad",
    "asleep": "sleep",
    "unknown": "sleep",
}
# the legend's entries in order: the states, the link's proxy, the features
LEGEND: Final = (*STATES, "link", *FEATURES)
# every word of the picture, English; `strings.json` has each as `common.topology_<key>`, translated
TEXTS: Final[Mapping[str, str]] = {
    "title": "Mesh topology",
    "summary_devices": "Devices: {count}",
    "summary_reachable": "reachable: {count}",
    "summary_unreachable": "unreachable: {count}",
    "summary_asleep": "asleep: {count}",
    "address": "address {address}",
    "connected": "connected",
    "no_link": "no link",
    "link": "link",
    "link_proxy": "link proxy",
    "state_reachable": "reachable",
    "state_unreachable": "unreachable",
    "state_asleep": "asleep (battery)",
    "state_unknown": "not known (no link)",
    "feature_relay": "relay",
    "feature_proxy": "proxy",
    "feature_friend": "friend",
    "feature_low_power": "low power",
    "badge_relay": "R",
    "badge_proxy": "P",
    "badge_friend": "F",
    "badge_low_power": "L",
    "last_heard": "last heard {time}",
    "band_hops": "Hops: {hops}",
    "band_more": "Hops: {hops} or more",
    "band_unknown": "Hops: not known",
    "band_unknown_off": "Hops: not known (option Node heartbeats is off)",
    "legend": "Legend",
    "legend_reachable": "reachable: answers",
    "legend_unreachable": "unreachable: did not answer",
    "legend_asleep": "asleep: a battery device",
    "legend_unknown": "not known: no link",
    "legend_link": "link proxy: Home Assistant's way in",
    "legend_relay": "relay: passes messages on",
    "legend_proxy": "proxy: offers a Bluetooth link",
    "legend_friend": "friend: keeps messages for sleepers",
    "legend_low_power": "low power: sleeps, has a friend",
    "footnote": "Bands: the hops of each device's last heartbeat (option Node heartbeats).",
}
# what XML 1.0 cannot carry at all, not even escaped
_NOT_XML = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")


@dataclass(frozen=True, kw_only=True)
class TopologyNode:
    """One node as the picture shows it.

    `reachable` is None for a battery node (it sleeps) and for every node without a link; `hops` None while no
    heartbeat of it was heard; `last_heard` (local time, minutes) only for a node that is not reachable — one that
    answers is heard all the time, and the picture would change with every message.
    """

    unicast: int
    name: str
    room: str | None = None
    relay: bool = False
    proxy: bool = False
    friend: bool = False
    low_power: bool = False
    battery: bool = False
    hops: int | None = None
    reachable: bool | None = None
    last_heard: str | None = None

    @property
    def state(self) -> str:
        """The node's state, a key of `STATES`."""
        if self.reachable is not None:
            return "reachable" if self.reachable else "unreachable"
        return "asleep" if self.battery else "unknown"

    def as_dict(self) -> dict[str, Any]:
        """Return the node as the diagnostics show it: its address in hex, the rest as shown."""
        return {
            "unicast": f"{self.unicast:04X}",
            "name": self.name,
            "room": self.room,
            "features": [key for key in FEATURES if getattr(self, key)],
            "battery": self.battery,
            "hops": self.hops,
            "reachable": self.reachable,
            "last_heard": self.last_heard,
        }


@dataclass(frozen=True, kw_only=True)
class Topology:
    """What the picture shows: Home Assistant (`address`), whether it has a link, through which node, every node.

    `heartbeats`: whether the option that brings the hops (*Node heartbeats*) is on; off, the band of the nodes whose
    hops are not known says why in its heading (it is off by default, so on most installations every node is there).
    """

    address: int
    connected: bool
    proxy: int | None
    nodes: tuple[TopologyNode, ...]
    heartbeats: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return the snapshot as the diagnostics show it, its nodes in the picture's order."""
        proxy, bands = _bands(self)
        return {
            "home_assistant": f"{self.address:04X}",
            "connected": self.connected,
            "proxy": None if proxy is None else f"{proxy.unicast:04X}",
            "nodes": [n.as_dict() for n in ([proxy] if proxy else [])]
            + [n.as_dict() for _, members in bands for n in members],
        }


def _sort_key(node: TopologyNode) -> tuple[str, int]:
    return (node.name.casefold(), node.unicast)


def _say(texts: Mapping[str, str], key: str, **values: object) -> str:
    """Return the text `key` with its placeholders filled in; the English one where the given text does not fit."""
    try:
        return texts[key].format_map(values)
    except (KeyError, IndexError, ValueError):
        return TEXTS[key].format_map(values)


def _band_title(texts: Mapping[str, str], hops: int | None, heartbeats: bool) -> str:
    if hops is None:
        return _say(texts, "band_unknown" if heartbeats else "band_unknown_off")
    if hops >= HOP_BANDS:
        return _say(texts, "band_more", hops=HOP_BANDS)
    return _say(texts, "band_hops", hops=hops)


def _bands(
    topology: Topology,
) -> tuple[TopologyNode | None, list[tuple[int | None, list[TopologyNode]]]]:
    """Return the link's proxy (None without one) and the other nodes by band, sorted: (hops, nodes), nearest first.

    The last band's hops are `HOP_BANDS` (and more); None is the band of the nodes whose hops are not known.
    """
    proxy = next(
        (
            n
            for n in topology.nodes
            if topology.connected and n.unicast == topology.proxy
        ),
        None,
    )
    grouped: dict[int | None, list[TopologyNode]] = {}
    for node in topology.nodes:
        if node is not proxy:
            band = None if node.hops is None else min(node.hops, HOP_BANDS)
            grouped.setdefault(band, []).append(node)
    order = sorted(
        grouped, key=lambda band: (band is None, 0 if band is None else band)
    )
    return proxy, [(band, sorted(grouped[band], key=_sort_key)) for band in order]


def _width(char: str) -> int:
    """Return the room `char` takes, in narrow characters: two for a wide (CJK) one."""
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def _fitting(text: str, limit: int) -> int:
    """Return how many of `text`'s first characters fit in `limit` narrow characters."""
    used = 0
    for count, char in enumerate(text):
        used += _width(char)
        if used > limit:
            return count
    return len(text)


def clean(text: str, limit: int) -> str:
    """`text` without what XML cannot hold, cut to `limit` narrow characters (an ellipsis marks a cut), escaped."""
    text = _NOT_XML.sub("", text)
    if _fitting(text, limit) < len(text):
        text = text[: _fitting(text, limit - 1)].rstrip() + "\N{HORIZONTAL ELLIPSIS}"
    return escape(text, quote=True)


def _wrap(text: str, limit: int) -> list[str]:
    """`text` on one line, or two, broken at the last space that fits (the second cut to fit), each line clean."""
    text = _NOT_XML.sub("", text)
    fits = _fitting(text, limit)
    if fits == len(text):
        return [escape(text, quote=True)]
    cut = text.rfind(" ", 0, fits + 1)
    if cut <= 0:  # no space to break at (a CJK sentence): break where it is full
        cut = fits
    return [
        escape(text[:cut].rstrip(), quote=True),
        clean(text[cut:].lstrip(), limit),
    ]


def _paint(fill: str | None = None, stroke: str | None = None) -> str:
    """Return the attributes painting an element: its classes (`f-`/`s-` + a `PALETTE` name) and the light values.

    No `fill` paints the inside `none` (an outline); every element has a fill or a stroke colour.
    """
    classes = [f"f-{fill}"] if fill else []
    attributes = [f'fill="{PALETTE[fill][0]}"' if fill else 'fill="none"']
    if stroke:
        classes.append(f"s-{stroke}")
        attributes.append(f'stroke="{PALETTE[stroke][0]}"')
    return f'class="{" ".join(classes)}" ' + " ".join(attributes)


def _style() -> str:
    """Return the dark colours: every palette colour as a fill class and a stroke class."""
    rules = "".join(
        f".f-{name}{{fill:{dark}}}.s-{name}{{stroke:{dark}}}"
        for name, (_, dark) in PALETTE.items()
    )
    return f"<style>@media (prefers-color-scheme: dark){{{rules}}}</style>"


def _text(
    x: int,
    y: int,
    text: str,
    *,
    size: int = 11,
    colour: str = "text",
    bold: bool = False,
    middle: bool = False,
) -> str:
    """Return a line of (already clean) text at `x`, `y` (its baseline), from its start or, `middle`, centred."""
    weight = ' font-weight="bold"' if bold else ""
    anchor = ' text-anchor="middle"' if middle else ""
    return f'<text x="{x}" y="{y}" font-size="{size}"{weight}{anchor} {_paint(colour)}>{text}</text>'


def _marker(state: str, cx: int, cy: int) -> str:
    """Return the shape of a state: a filled circle, a cross, a hollow square, a diamond; a house for Home Assistant."""
    if state == "reachable":
        return f'<circle cx="{cx}" cy="{cy}" r="6" {_paint("ok")}/>'
    if state == "unreachable":
        return (
            f'<path d="M{cx - 5} {cy - 5}L{cx + 5} {cy + 5}M{cx + 5} {cy - 5}L{cx - 5} {cy + 5}" '
            f'stroke-width="3" {_paint(stroke="bad")}/>'
        )
    if state == "asleep":
        return (
            f'<rect x="{cx - 5}" y="{cy - 5}" width="10" height="10" stroke-width="2" '
            f"{_paint(stroke='sleep')}/>"
        )
    if state == "unknown":
        return (
            f'<path d="M{cx} {cy - 6}L{cx + 6} {cy}L{cx} {cy + 6}L{cx - 6} {cy}Z" stroke-width="2" '
            f"{_paint(stroke='sleep')}/>"
        )
    # Home Assistant
    return f'<path d="M{cx - 6} {cy + 6}V{cy}L{cx} {cy - 6}L{cx + 6} {cy}V{cy + 6}Z" {_paint("link")}/>'


def _badge(texts: Mapping[str, str], key: str, cx: int, cy: int) -> str:
    letter = clean(_say(texts, f"badge_{key}"), BADGE_CHARS)
    return f'<circle cx="{cx}" cy="{cy}" r="7" {_paint(key)}/>' + _text(
        cx, cy + 3, letter, size=9, colour="badge_text", bold=True, middle=True
    )


def _box(x: int, y: int, *, link: bool = False, state: str | None = None) -> str:
    """Return a node's frame: thick in the link colour for the link's proxy (and Home Assistant), dashed unreachable."""
    if link:
        stroke = f'stroke-width="3" {_paint("card", "link")}'
    elif state == "unreachable":
        stroke = f'stroke-width="1.5" stroke-dasharray="6 4" {_paint("card", "bad")}'
    else:
        stroke = f'stroke-width="1" {_paint("card", "edge")}'
    return f'<rect x="{x}" y="{y}" width="{BOX_W}" height="{BOX_H}" rx="6" {stroke}/>'


def _node(
    texts: Mapping[str, str], node: TopologyNode, x: int, y: int, *, link: bool = False
) -> str:
    """One node's box: marker, name, room and address, state, features as words and badges, when last heard."""
    state = node.state
    word = _say(texts, f"state_{state}")
    where = (
        f"{node.room} \N{MIDDLE DOT} {node.unicast:04X}"
        if node.room
        else f"{node.unicast:04X}"
    )
    status = f"{_say(texts, 'link_proxy')} \N{MIDDLE DOT} {word}" if link else word
    features = [key for key in FEATURES if getattr(node, key)]
    lines = [(where, "muted"), (status, STATES[state])]
    if features:
        words = (_say(texts, f"feature_{key}") for key in features)
        lines.append((" \N{MIDDLE DOT} ".join(words), "muted"))
    if node.last_heard is not None:
        lines.append((_say(texts, "last_heard", time=node.last_heard), "muted"))
    parts = [
        _box(x, y, link=link, state=state),
        _marker(state, x + 16, y + 17),
        _text(x + 30, y + 21, clean(node.name, NAME_CHARS), size=12, bold=True),
    ]
    parts += [
        _text(x + 30, y + 38 + 16 * i, clean(line, LINE_CHARS), colour=line_colour)
        for i, (line, line_colour) in enumerate(lines)
    ]
    parts += [
        _badge(texts, key, x + BOX_W - 13, y + 15 + 18 * i)
        for i, key in enumerate(features)
    ]
    return f"<g>{''.join(parts)}</g>"


def _home_assistant(
    texts: Mapping[str, str], topology: Topology, x: int, y: int
) -> str:
    key, colour = ("connected", "ok") if topology.connected else ("no_link", "bad")
    address = _say(texts, "address", address=f"{topology.address:04X}")
    return (
        f"<g>{_box(x, y, link=True)}{_marker('home', x + 16, y + 17)}"
        f"{_text(x + 30, y + 21, 'Home Assistant', size=12, bold=True)}"
        f"{_text(x + 30, y + 38, clean(address, LINE_CHARS), colour='muted')}"
        f"{_text(x + 30, y + 54, clean(_say(texts, key), LINE_CHARS), colour=colour)}</g>"
    )


def _summary(texts: Mapping[str, str], topology: Topology) -> str:
    """Return the line under the title: how many devices, and how many of them are in each state."""
    counts: dict[str, int] = {}
    for node in topology.nodes:
        counts[node.state] = counts.get(node.state, 0) + 1
    parts = [_say(texts, "summary_devices", count=len(topology.nodes))]
    if not topology.connected:
        parts.append(_say(texts, "no_link"))
    parts += [
        _say(texts, f"summary_{state}", count=counts[state])
        for state in ("reachable", "unreachable", "asleep")
        if counts.get(state)
    ]
    return " \N{MIDDLE DOT} ".join(parts)


def _legend(
    texts: Mapping[str, str], x: int, y: int, width: int
) -> tuple[list[str], int]:
    """Return the legend's elements from `y` down, in two columns, and the height it takes."""
    title = clean(_say(texts, "legend"), HEAD_CHARS)
    out = [_text(x, y + 12, title, size=12, colour="muted", bold=True)]
    half = (len(LEGEND) + 1) // 2
    column = width // 2
    for i, key in enumerate(LEGEND):
        words = _say(texts, f"legend_{key}")
        cx = x + 8 + (i // half) * column
        cy = y + 30 + (i % half) * LEGEND_LINE
        if key in STATES:
            sample = _marker(key, cx, cy - 4)
        elif key == "link":
            sample = (
                f'<rect x="{cx - 7}" y="{cy - 10}" width="14" height="12" rx="2" stroke-width="3" '
                f"{_paint('card', 'link')}/>"
            )
        else:
            sample = _badge(texts, key, cx, cy - 4)
            words = f"{_say(texts, f'badge_{key}')} {words}"
        out += [sample, _text(cx + 14, cy, clean(words, LEGEND_CHARS))]
    bottom = y + 30 + half * LEGEND_LINE
    footnote = _wrap(_say(texts, "footnote"), WIDE_CHARS)
    out += [
        _text(x, bottom + i * (LEGEND_LINE - 4), line, colour="muted")
        for i, line in enumerate(footnote)
    ]
    return out, bottom + 8 + (len(footnote) - 1) * (LEGEND_LINE - 4) - y


def render_svg(topology: Topology, texts: Mapping[str, str] | None = None) -> str:
    """Return the picture of `topology` (module docstring): the same snapshot, the same text, whatever the order.

    `texts` are its words by `TEXTS` key (the translations of a language), English for every one it lacks.
    """
    texts = {**TEXTS, **(texts or {})}
    title = clean(_say(texts, "title"), TITLE_CHARS)
    proxy, bands = _bands(topology)
    widest = max((len(members) for _, members in bands), default=0)
    columns = max(MIN_COLUMNS, min(MAX_COLUMNS, widest))
    width = 2 * MARGIN + columns * BOX_W + (columns - 1) * GAP_X
    inner = width - 2 * MARGIN
    body = [
        _text(MARGIN, MARGIN + 18, title, size=16, bold=True),
        _text(
            MARGIN,
            MARGIN + 36,
            clean(_summary(texts, topology), WIDE_CHARS + 24),
            colour="muted",
        ),
    ]
    y = MARGIN + 52
    # the top band: Home Assistant and, with a link, the node it goes through
    body.append(_home_assistant(texts, topology, MARGIN, y))
    if proxy is not None:
        px = MARGIN + BOX_W + LINK_GAP
        mid = y + BOX_H // 2
        body += [
            (
                f'<line x1="{MARGIN + BOX_W}" y1="{mid}" x2="{px}" y2="{mid}" stroke-width="3" '
                f"{_paint(stroke='link')}/>"
            ),
            _text(
                MARGIN + BOX_W + LINK_GAP // 2,
                mid - 6,
                clean(_say(texts, "link"), LINK_CHARS),
                colour="link",
                middle=True,
            ),
            _node(texts, proxy, px, y, link=True),
        ]
    y += BOX_H + BAND_GAP
    for band, members in bands:
        if band is None:
            body.append(
                f'<line x1="{MARGIN}" y1="{y}" x2="{width - MARGIN}" y2="{y}" stroke-width="1" '
                f'stroke-dasharray="4 4" {_paint(stroke="edge")}/>'
            )
            y += 6
        head = clean(_band_title(texts, band, topology.heartbeats), HEAD_CHARS)
        body.append(_text(MARGIN, y + 16, head, size=12, colour="muted", bold=True))
        y += BAND_HEAD
        for i, node in enumerate(members):
            row, column = divmod(i, columns)
            body.append(
                _node(
                    texts,
                    node,
                    MARGIN + column * (BOX_W + GAP_X),
                    y + row * (BOX_H + GAP_Y),
                )
            )
        rows = -(-len(members) // columns)
        y += rows * (BOX_H + GAP_Y) - GAP_Y + BAND_GAP
    legend, legend_height = _legend(texts, MARGIN, y, inner)
    body += legend
    height = y + legend_height + MARGIN
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" role="img" aria-label="{title}">'
        f"<title>{title}</title>{_style()}"
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="8" stroke-width="1" '
        f"{_paint('bg', 'frame')}/>"
        f"{''.join(body)}</svg>\n"
    )
