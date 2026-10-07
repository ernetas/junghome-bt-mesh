"""The *Mesh topology* image and the diagnostics' `topology` (review-4 U4-14).

The image sits on the mesh network device, diagnostic and enabled; it serves an SVG of the hub's topology snapshot,
and its state (`image_last_updated`) moves only when the snapshot changed — at most once a minute, a change within
the minute shown when it is over. Its words are the server's language's, redrawn when that changes. The picture
itself is `tests/test_topology_svg.py`'s.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.image import async_get_image
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from custom_components.junghome_ble.const import (
    DOMAIN,
    NODE_DIAGNOSTICS_INTERVAL,
    SIGNAL_REACHABILITY,
)
from custom_components.junghome_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.junghome_ble.mesh_topology import (
    topology_snapshot,
    topology_texts,
)
from custom_components.junghome_ble.topology_svg import TEXTS, render_svg

from .helpers import (
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    MESH_UUID,
    NODE_SOCKET,
    OUR_ADDRESS,
    SOCKET,
    entity_id,
)
from .test_binary_sensor import make_detectors_entry, start_detectors
from .test_mesh_health import documented_cards, hub_of, tick

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.junghome_ble.image import JungHomeMeshTopology

    from .conftest import FakeProxyLink

UID = f"{MESH_UUID}-mesh-topology"


def image_id(hass: HomeAssistant) -> str:
    return entity_id(hass, "image", UID)


def state(hass: HomeAssistant) -> str:
    current = hass.states.get(image_id(hass))
    assert current is not None
    return current.state


async def picture(hass: HomeAssistant) -> str:
    image = await async_get_image(hass, image_id(hass))
    assert image.content_type == "image/svg+xml"
    return image.content.decode()


async def test_the_image_on_the_mesh_network_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Diagnostic and enabled on the mesh device; the SVG of the snapshot, its state when it was drawn."""
    hub = hub_of(init_integration)
    entry = er.async_get(hass).async_get(image_id(hass))
    assert entry is not None
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC
    assert entry.disabled_by is None
    assert entry.translation_key == "mesh_topology"
    device = dr.async_get(hass).async_get(entry.device_id)
    assert (DOMAIN, f"mesh:{MESH_UUID}") in device.identifiers
    image: JungHomeMeshTopology = hub.platforms["image"].entities[UID]  # type: ignore[assignment]
    svg = await picture(hass)
    assert svg.startswith("<svg ")
    assert image._shown is not None
    assert svg == render_svg(image._shown)
    assert "Home Assistant" in svg
    assert datetime.fromisoformat(state(hass)) <= dt_util.utcnow()


async def test_redrawn_only_when_the_snapshot_changes(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """A heartbeat with new hops redraws the picture at the next look; the same heartbeat again changes nothing.

    The mesh answers the refresh: no node turns unreachable meanwhile.
    """
    hub = hub_of(init_integration)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    before = state(hass)
    fake_link.inject_heartbeat(SOCKET, OUR_ADDRESS, init_ttl=5, ttl=4)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    drawn = state(hass)
    assert drawn != before
    hops = hub.heartbeats[SOCKET].hops
    assert {"unicast": f"{SOCKET:04X}", "hops": hops}.items() <= _node(
        topology_snapshot(hub).as_dict(), SOCKET
    ).items()
    assert f"Hops: {hops}" in await picture(hass)

    fake_link.inject_heartbeat(SOCKET, OUR_ADDRESS, init_ttl=5, ttl=4)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    assert state(hass) == drawn  # the same hops: nothing to draw

    fake_link.inject_heartbeat(SOCKET, OUR_ADDRESS, init_ttl=5, ttl=2)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    assert state(hass) != drawn


async def test_a_change_within_the_minute_waits_for_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
) -> None:
    """A change announced long after the last redraw is drawn at once; the next, within the minute, when it is over."""
    hub = hub_of(init_integration)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    image: JungHomeMeshTopology = hub.platforms["image"].entities[UID]  # type: ignore[assignment]
    image._cancel_pending()  # no look waiting from the setup ...
    freezer.tick(NODE_DIAGNOSTICS_INTERVAL)  # ... and the last redraw a minute ago
    before = state(hass)

    unreachable(hass, init_integration, SOCKET)
    drawn = state(hass)
    assert drawn != before

    unreachable(hass, init_integration, LIGHT_DIMMER)
    assert state(hass) == drawn
    assert image._pending is not None
    image.async_model_rebound()  # a look asked for meanwhile joins the one waiting
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL - 2)
    assert state(hass) == drawn
    await tick(hass, freezer, 2)
    assert state(hass) != drawn
    assert image._pending is None
    nodes = topology_snapshot(hub).as_dict()
    assert _node(nodes, SOCKET)["reachable"] is False
    assert _node(nodes, LIGHT_DIMMER)["reachable"] is False
    assert "unreachable" in await picture(hass)


def unreachable(hass: HomeAssistant, entry: MockConfigEntry, unicast: int) -> None:
    """Mark the node unreachable and announce it, as `Liveness.missed_answer` does (without a later revival)."""
    hub_of(entry).liveness.unreachable.add(unicast)
    async_dispatcher_send(hass, SIGNAL_REACHABILITY.format(entry.entry_id))


async def test_a_look_waiting_ends_with_the_entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
) -> None:
    hub = hub_of(init_integration)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    image: JungHomeMeshTopology = hub.platforms["image"].entities[UID]  # type: ignore[assignment]
    image.async_model_rebound()  # within the minute: held back
    assert image._pending is not None
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert image._pending is None


def _node(topology: dict[str, Any], unicast: int) -> dict[str, Any]:
    return next(n for n in topology["nodes"] if n["unicast"] == f"{unicast:04X}")


async def test_the_diagnostics_show_the_snapshot(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """`topology`: Home Assistant, the link's proxy first, each node with its device's name and area, its features."""
    hub = hub_of(init_integration)
    devices = dr.async_get(hass)
    utility = ar.async_get(hass).async_create("Utility room")
    devices.async_update_device(
        hub.device_ids[f"node:{NODE_SOCKET}"],
        name_by_user="Boiler socket",
        area_id=utility.id,
    )
    hub.liveness.unreachable.add(SOCKET)
    hub.last_seen[SOCKET] = datetime(2020, 1, 1, 12, 30, tzinfo=UTC)
    data = await async_get_config_entry_diagnostics(hass, init_integration)
    topology = data["topology"]
    assert topology["home_assistant"] == f"{OUR_ADDRESS:04X}"
    assert topology["connected"] is True
    assert topology["proxy"] == f"{hub.proxy_node:04X}"
    assert topology["nodes"][0]["unicast"] == topology["proxy"]
    assert len(topology["nodes"]) == len([n for n in hub.cdb.nodes if n.pid])
    assert _node(topology, SOCKET) == {
        "unicast": f"{SOCKET:04X}",
        "name": "Boiler socket",
        "room": "Utility room",
        "features": ["relay", "proxy"],
        "battery": False,
        "hops": None,
        "reachable": False,
        "last_heard": dt_util.as_local(hub.last_seen[SOCKET]).strftime(
            "%Y-%m-%d %H:%M"
        ),
    }
    # a node that answers: no time, it would change with every message
    assert _node(topology, LIGHT_SWITCH)["last_heard"] is None
    assert _node(topology, LIGHT_SWITCH)["reachable"] is True


async def test_a_battery_node_sleeps_in_the_picture(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """A wall transmitter has no verdict and no hops; when it was last heard is shown, and its low-power feature."""
    entry = await start_detectors(hass, make_detectors_entry(), fake_link)
    hub = hub_of(entry)
    transmitter = next(
        n for n in hub.cdb.nodes if n.name.startswith("Wall transmitter")
    )
    hub.last_seen[transmitter.unicast] = datetime(2020, 1, 1, tzinfo=UTC)
    node = _node(topology_snapshot(hub).as_dict(), transmitter.unicast)
    assert node["battery"] is True
    assert node["reachable"] is None
    assert node["hops"] is None
    assert node["last_heard"] is not None
    assert "asleep (battery)" in render_svg(topology_snapshot(hub))


async def test_the_documented_card_shows_the_image(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The user guide's picture-entity card names the image as Home Assistant names it for the mesh device."""
    card = documented_cards()["picture-entity"]
    assert card["entity"] == image_id(hass).replace("_mesh_test_", "_mesh_")
    assert card["entity"] == "image.jung_home_mesh_mesh_topology"


async def test_the_picture_speaks_the_server_language(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
) -> None:
    """Its words are the server's language's: a language chosen is drawn at once, one found at a look then."""
    hub = hub_of(init_integration)
    image: JungHomeMeshTopology = hub.platforms["image"].entities[UID]  # type: ignore[assignment]
    assert image._texts == TEXTS
    english = state(hass)
    assert "<title>Mesh topology</title>" in await picture(hass)

    freezer.tick(1)
    await hass.config.async_update(language="de")
    await hass.async_block_till_done()
    svg = await picture(hass)
    assert "<title>Mesh-Topologie</title>" in svg
    assert ">Legende<" in svg
    assert state(hass) != english
    assert svg == render_svg(topology_snapshot(hub), topology_texts(hass))

    german = state(hass)
    freezer.tick(1)
    await hass.config.async_update(location_name="Elsewhere")  # not the language
    await hass.async_block_till_done()
    assert state(hass) == german

    hass.config.language = "fi"  # set without the event: English until it is cached
    assert topology_texts(hass) == TEXTS
    await async_get_translations(hass, "fi", "common", {DOMAIN})
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    assert "<title>Mesh-verkon topologia</title>" in await picture(hass)
