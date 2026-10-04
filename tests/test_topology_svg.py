"""The *Mesh topology* picture without Home Assistant (review-4 U4-14): golden files, escaping, order, size.

The golden files are `tests/snapshots/test_topology_svg/*.svg`; look at them (any browser, `rsvg-convert`) when they change,
and regenerate with `pytest tests/test_topology_svg.py --snapshot-update`.
"""

from __future__ import annotations

import random
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.parsers import expat

import pytest
from syrupy.extensions.image import SVGImageSnapshotExtension

from custom_components.junghome_ble.topology_svg import (
    BOX_W,
    GAP_X,
    HOP_BANDS,
    MARGIN,
    MAX_COLUMNS,
    NAME_CHARS,
    PALETTE,
    Topology,
    TopologyNode,
    clean,
    render_svg,
)

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion
    from syrupy.location import PyTestLocation

HA = 0x0D00
# a small mesh: the link's proxy, relays at two and three hops, an unreachable node, a battery node, unknown hops
SMALL = Topology(
    address=HA,
    connected=True,
    proxy=0x0110,
    nodes=(
        TopologyNode(
            unicast=0x0110,
            name="WC mirror - Push-button 1-gang",
            room="WC",
            relay=True,
            proxy=True,
            hops=1,
            reachable=True,
        ),
        TopologyNode(
            unicast=0x0172,
            name="Boiler - Socket (metering)",
            room="Kitchen",
            relay=True,
            proxy=True,
            hops=2,
            reachable=True,
        ),
        TopologyNode(
            unicast=0x00DC,
            name="Gateway 00DC",
            relay=True,
            proxy=True,
            friend=True,
            hops=2,
            reachable=True,
        ),
        TopologyNode(
            unicast=0x0400,
            name="2-channel actuator 0400",
            room="Living room",
            relay=True,
            hops=3,
            reachable=False,
            last_heard="2020-01-01 00:00",
        ),
        TopologyNode(
            unicast=0x0520,
            name="Wall transmitter 1-gang 0520",
            room="Hall",
            low_power=True,
            battery=True,
            last_heard="2020-01-01 00:00",
        ),
        TopologyNode(unicast=0x0300, name="Attic light", room="Attic", reachable=True),
    ),
)
# the same mesh without a link: nobody's state is known, no proxy
NO_LINK = replace(
    SMALL,
    connected=False,
    proxy=None,
    nodes=tuple(
        replace(n, reachable=None, last_heard="2020-01-01 00:00") for n in SMALL.nodes
    ),
)


class GoldenSVG(SVGImageSnapshotExtension):
    """One `.svg` file per golden picture, under `tests/snapshots/test_topology_svg/`."""

    @classmethod
    def dirname(cls, *, test_location: PyTestLocation) -> str:
        return str(
            Path(test_location.filepath).parent / "snapshots" / test_location.basename
        )


@pytest.fixture
def golden(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(GoldenSVG)


def parse(svg: str) -> list[tuple[str, dict[str, str]]]:
    """Parse `svg` as XML (raising on anything ill-formed); every element with its attributes, in order."""
    elements: list[tuple[str, dict[str, str]]] = []
    parser = expat.ParserCreate()
    parser.StartElementHandler = lambda name, attributes: elements.append(
        (name, attributes)
    )
    parser.Parse(svg, True)
    return elements


def texts(svg: str) -> list[str]:
    """The text of every `<text>` element, unescaped."""
    out: list[str] = []
    parser = expat.ParserCreate()
    current: list[str] | None = None

    def start(name: str, _attributes: Any) -> None:
        nonlocal current
        if name == "text":
            current = []

    def data(chunk: str) -> None:
        if current is not None:
            current.append(chunk)

    def end(name: str) -> None:
        nonlocal current
        if name == "text" and current is not None:
            out.append("".join(current))
            current = None

    parser.StartElementHandler = start
    parser.CharacterDataHandler = data
    parser.EndElementHandler = end
    parser.Parse(svg, True)
    return out


@pytest.mark.parametrize("topology", [SMALL, NO_LINK], ids=["small", "no-link"])
def test_golden(golden: SnapshotAssertion, topology: Topology) -> None:
    svg = render_svg(topology)
    parse(svg)
    assert svg == golden


def test_what_the_small_picture_says() -> None:
    """Every state and feature as a word, the bands in hop order, the proxy next to Home Assistant, a legend."""
    words = texts(render_svg(SMALL))
    assert words[:2] == [
        "Mesh topology",
        "6 devices \N{MIDDLE DOT} 4 reachable \N{MIDDLE DOT} 1 unreachable \N{MIDDLE DOT} 1 asleep",
    ]
    assert words.index("Home Assistant") < words.index(
        "WC mirror - Push-butto\N{HORIZONTAL ELLIPSIS}"
    )
    assert "link proxy \N{MIDDLE DOT} reachable" in words
    assert "relay \N{MIDDLE DOT} proxy \N{MIDDLE DOT} friend" in words
    assert "unreachable" in words
    assert "asleep (battery)" in words
    assert "last heard 2020-01-01 00:00" in words
    bands = [w for w in words if re.fullmatch(r"\d+ hops?|Hops not known", w)]
    assert bands == ["2 hops", "3 hops", "Hops not known"]
    assert words.index("Boiler - Socket (meter\N{HORIZONTAL ELLIPSIS}") < words.index(
        "Gateway 00DC"
    )  # by name within a band
    assert "Legend" in words
    assert words[-1].startswith("Bands: the hops")


def test_without_a_link() -> None:
    words = texts(render_svg(NO_LINK))
    assert "no link" in words
    assert (
        words.count("not known (no link)") == 5
    )  # the battery node is asleep, not unknown
    assert not any(w.startswith("link proxy \N{MIDDLE DOT}") for w in words)
    assert not any(
        name == "line" and a.get("stroke-width") == "3"
        for name, a in parse(render_svg(NO_LINK))
    )


def test_light_and_dark() -> None:
    """The light colours as attributes, the dark ones under the media query, a background with its own border."""
    svg = render_svg(SMALL)
    assert "@media (prefers-color-scheme: dark)" in svg
    for name, (light, dark) in PALETTE.items():
        assert f".f-{name}{{fill:{dark}}}" in svg
        assert light in svg
    background = parse(svg)[3]  # svg, title, style, the background
    assert background[0] == "rect"
    assert background[1]["class"] == "f-bg s-frame"


def test_names_are_escaped() -> None:
    node = TopologyNode(
        unicast=0x0101, name='<b>&"x\x00\x1b', room='<&">', reachable=True
    )
    svg = render_svg(Topology(address=HA, connected=True, proxy=None, nodes=(node,)))
    parse(svg)
    assert "<b>" not in svg
    assert "&lt;b&gt;&amp;&quot;x" in svg
    assert "&lt;&amp;&quot;&gt; \N{MIDDLE DOT} 0101" in svg
    assert '<b>&"x' in texts(svg)


def test_clean_cuts_before_it_escapes() -> None:
    """A cut never splits an entity: the text is cut first, escaped after."""
    assert (
        clean("&" * 40, NAME_CHARS)
        == "&amp;" * (NAME_CHARS - 1) + "\N{HORIZONTAL ELLIPSIS}"
    )
    assert clean("short", NAME_CHARS) == "short"
    assert clean("a\ud800b\x07c", NAME_CHARS) == "abc"


def test_the_order_of_the_nodes_does_not_matter() -> None:
    rng = random.Random(75)  # noqa: S311 - a reproducible shuffle
    expected = render_svg(SMALL)
    for _ in range(10):
        nodes = list(SMALL.nodes)
        rng.shuffle(nodes)
        shuffled = replace(SMALL, nodes=tuple(nodes))
        assert render_svg(shuffled) == expected
        assert shuffled.as_dict() == SMALL.as_dict()


def test_as_dict() -> None:
    """The diagnostics' `topology`: the proxy first, then the bands in the picture's order."""
    data = SMALL.as_dict()
    assert data["home_assistant"] == "0D00"
    assert data["connected"] is True
    assert data["proxy"] == "0110"
    assert [n["unicast"] for n in data["nodes"]] == [
        "0110",
        "0172",
        "00DC",
        "0400",
        "0300",
        "0520",
    ]
    assert data["nodes"][2] == {
        "unicast": "00DC",
        "name": "Gateway 00DC",
        "room": None,
        "features": ["relay", "proxy", "friend"],
        "battery": False,
        "hops": 2,
        "reachable": True,
        "last_heard": None,
    }
    assert NO_LINK.as_dict()["proxy"] is None


def test_eighty_nodes_stay_bounded() -> None:
    """At most MAX_COLUMNS boxes a row and HOP_BANDS + 1 bands below the top: the size grows with the count alone."""
    rng = random.Random(80)  # noqa: S311 - reproducible hops
    nodes = tuple(
        TopologyNode(
            unicast=0x0100 + i,
            name=f"Device {i:02d} with a rather long name indeed",
            room=f"Room {i % 9}",
            relay=i % 2 == 0,
            proxy=i % 3 == 0,
            hops=rng.choice([None, *range(1, 30)]),
            reachable=rng.choice([True, False]),
        )
        for i in range(80)
    )
    svg = render_svg(Topology(address=HA, connected=True, proxy=0x0100, nodes=nodes))
    root = parse(svg)[0][1]
    width, height = int(root["width"]), int(root["height"])
    assert width == 2 * MARGIN + MAX_COLUMNS * BOX_W + (MAX_COLUMNS - 1) * GAP_X
    assert height < 3200
    assert len(svg.encode()) < 160_000
    words = texts(svg)
    assert f"{HOP_BANDS} or more hops" in words
    assert (
        len(
            [
                w
                for w in words
                if re.fullmatch(r"\d+ (or more )?hops?|Hops not known", w)
            ]
        )
        <= HOP_BANDS + 1
    )
    assert words[1].startswith("80 devices")


def test_one_device_and_nothing_else() -> None:
    """The narrowest picture: one node, no band wider than the minimum."""
    node = TopologyNode(unicast=0x0101, name="Solo", hops=4)
    words = texts(
        render_svg(Topology(address=HA, connected=True, proxy=None, nodes=(node,)))
    )
    assert words[1] == "1 device"
    assert "4 hops" in words
