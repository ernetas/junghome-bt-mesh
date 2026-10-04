"""Mesh health at a glance (review-4 U4-7) and the room central entities hidden at first (U4-12, decision M9).

*Mesh connection* follows the link, *Unreachable devices* counts the mains nodes that do not answer (unreachable, or
dead with the heartbeat option) and names them, *Mesh overview* has a row per node, written at most once a minute;
the lists stay out of the recorder. The user guide's Markdown card is rendered with Home Assistant's own template
engine, against the overview as the integration writes it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.junghome_ble.const import DOMAIN, NODE_DIAGNOSTICS_INTERVAL
from custom_components.junghome_ble.entity import node_identifier
from custom_components.junghome_ble.hub.link import LinkManager
from custom_components.junghome_ble.sensor import best_scanner, node_label

from .conftest import (
    PROXY_ADDRESS,
    FakeProxyLink,
    make_service_info,
    settle,
    wait_for_link,
)
from .helpers import (
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    MAC_LIGHT_SWITCH,
    MESH_UUID,
    NODE_GATEWAY,
    NODE_SOCKET,
    OUR_ADDRESS,
    SOCKET,
    entity_id,
    onoff_status,
)
from .test_binary_sensor import make_detectors_entry, start_detectors
from .test_hub_liveness import HEARTBEAT_TIMEOUT, start_with_heartbeats

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.junghome_ble.coordinator import JungHomeHub

UID_CONNECTION = f"{MESH_UUID}-mesh-connection"
UID_UNREACHABLE = f"{MESH_UUID}-unreachable-devices"
UID_OVERVIEW = f"{MESH_UUID}-mesh-overview"
# the detectors network's
UID_UNREACHABLE_DETECTORS = "1baf3ade-0000-4000-8000-000000000002-unreachable-devices"
UID_OVERVIEW_DETECTORS = "1baf3ade-0000-4000-8000-000000000002-mesh-overview"
UID_ALL_LIGHTS = f"{MESH_UUID}-central-fef5"
UID_ROOM_LIGHTS = f"{MESH_UUID}-room-c00f-lights"  # the WC's lights
UID_ROOM_SOCKETS = f"{MESH_UUID}-room-c011-sockets"  # the Kitchen's sockets
GROUP_SWITCH = 0xC061  # the group LIGHT_SWITCH publishes its status to
GROUP_SOCKET = 0xC000
MAINS_NODES = 6  # the base network's: gateway, two 1-gang push-buttons, the socket, the 2-gang, the actuator
DOCS = Path(__file__).resolve().parent.parent / "docs" / "user" / "everyday-use.md"
DOCUMENTED_OVERVIEW = "sensor.jung_home_mesh_mesh_overview"
NONE = "\N{EN DASH}"  # what the card shows for a value the row does not have


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


def overview_rows(
    hass: HomeAssistant, uid: str = UID_OVERVIEW
) -> dict[str, dict[str, Any]]:
    """The *Mesh overview* rows by device name."""
    state = hass.states.get(entity_id(hass, "sensor", uid))
    assert state is not None
    return {row["name"]: row for row in state.attributes["nodes"]}


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await settle(hass)


def miss(hub: JungHomeHub, address: int) -> None:
    """Have the node of `address` leave a full-budget request unanswered: it is unreachable from now on."""
    hub.liveness.missed_answer(address, "onoff", time.monotonic() + 1)


@pytest.fixture
def overview_writes(hass: HomeAssistant) -> Generator[list[str]]:
    """The state of every write of the *Mesh overview* sensor from here on (attributes changing alone included)."""
    writes: list[str] = []

    @callback
    def changed(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is not None and new.entity_id.endswith("_mesh_overview"):
            writes.append(new.state)

    unsub = hass.bus.async_listen(EVENT_STATE_CHANGED, changed)
    yield writes
    unsub()


# --------------------------------------------------------------------------- the three entities


async def test_mesh_connection_follows_the_link(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """On while the link is up; off — not unavailable — without one, when *Unreachable devices* has nothing to say.

    The overview follows the link at once too, however recent its last write: no rate limit holds back a link change.
    """
    hub = hub_of(init_integration)
    connection = entity_id(hass, "binary_sensor", UID_CONNECTION)
    unreachable = entity_id(hass, "sensor", UID_UNREACHABLE)
    overview = entity_id(hass, "sensor", UID_OVERVIEW)
    reachable = hass.states.get(overview).state
    assert reachable != "0"
    state = hass.states.get(connection)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.CONNECTIVITY
    assert hass.states.get(unreachable).state == "0"
    entry = er.async_get(hass).async_get(connection)
    assert entry.entity_category is None
    assert entry.disabled_by is None
    assert entry.hidden_by is None
    device = dr.async_get(hass).async_get(entry.device_id)
    assert (DOMAIN, f"mesh:{MESH_UUID}") in device.identifiers

    with patch.object(LinkManager, "visible_proxies", return_value=[]):
        fake_link.drop_link()
        await settle(hass)
        assert not hub.link_available
        assert hass.states.get(connection).state == STATE_OFF
        assert hass.states.get(unreachable).state == STATE_UNAVAILABLE
        assert hass.states.get(overview).state == "0"
    hub.link._link_lost.set()  # a proxy advertises again
    await wait_for_link(hass, init_integration)
    await settle(hass)
    assert hass.states.get(connection).state == STATE_ON
    assert hass.states.get(unreachable).state == "0"
    assert hass.states.get(overview).state == reachable


async def test_a_node_that_does_not_answer_counts_until_heard(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    overview_writes: list[str],
) -> None:
    """*Unreachable devices* follows at once; the overview within the minute, one write however much happens in it."""
    hub = hub_of(init_integration)
    unreachable = entity_id(hass, "sensor", UID_UNREACHABLE)
    overview = entity_id(hass, "sensor", UID_OVERVIEW)
    await tick(
        hass, freezer, NODE_DIAGNOSTICS_INTERVAL
    )  # a write on the tick: the clock starts here
    assert hass.states.get(overview).state == str(MAINS_NODES)
    overview_writes.clear()

    miss(hub, SOCKET)
    await settle(hass)
    state = hass.states.get(unreachable)
    assert state.state == "1"
    assert state.attributes["devices"] == [
        "Boiler - Socket (metering)"
    ]  # its node device's name
    assert overview_writes == []  # held back: the last write is less than a minute old
    miss(hub, LIGHT_DIMMER)
    await settle(hass)
    assert hass.states.get(unreachable).attributes["devices"] == [
        "Boiler - Socket (metering)",
        "WC ceiling - Push-button 1-gang",
    ]
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL - 1)
    assert overview_writes == []
    await tick(hass, freezer, 1)
    assert overview_writes == [str(MAINS_NODES - 2)]  # both changes in one write
    rows = overview_rows(hass)
    assert rows["Boiler - Socket (metering)"]["reachable"] is False
    assert rows["WC ceiling - Push-button 1-gang"]["reachable"] is False
    assert rows["Gateway 00DC"]["reachable"] is True

    # heard again: back at once in the count, in the overview once the minute is over
    fake_link.inject(SOCKET, GROUP_SOCKET, onoff_status(True))
    await settle(hass)
    assert hass.states.get(unreachable).state == "1"
    assert hass.states.get(overview).state == str(MAINS_NODES - 2)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    assert hass.states.get(overview).state == str(MAINS_NODES - 1)
    assert overview_rows(hass)["Boiler - Socket (metering)"]["reachable"] is True

    # a write held back when the entry unloads is dropped with it
    miss(hub, SOCKET)
    await settle(hass)
    assert hass.states.get(overview).state == str(MAINS_NODES - 1)
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    assert hass.states.get(overview).state == STATE_UNAVAILABLE


async def test_dead_nodes_count_with_heartbeats(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """With the heartbeat option, a node silent for the timeout counts until it beats again; the overview shows hops."""
    hub = await start_with_heartbeats(hass, mock_config_entry, fake_link)
    unreachable = entity_id(hass, "sensor", UID_UNREACHABLE)
    assert hass.states.get(unreachable).state == "0"
    with (
        patch("custom_components.junghome_ble.hub.link.LINK_IDLE_TIMEOUT", 10 * 3600.0),
        patch.object(hub.liveness, "_reprobe_dead", AsyncMock()),
    ):
        await tick(
            hass, freezer, HEARTBEAT_TIMEOUT + 30
        )  # nobody beat: every node is dead
        assert hass.states.get(unreachable).state == str(MAINS_NODES)
        fake_link.inject_heartbeat(LIGHT_SWITCH, OUR_ADDRESS, init_ttl=5, ttl=3)
        await settle(hass)
        state = hass.states.get(unreachable)
        assert state.state == str(MAINS_NODES - 1)
        assert "WC mirror - Push-button 1-gang" not in state.attributes["devices"]
        await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
        row = overview_rows(hass)["WC mirror - Push-button 1-gang"]
        assert row["reachable"] is True
        assert row["hops"] == 2
        assert overview_rows(hass)["Boiler - Socket (metering)"]["reachable"] is False


async def test_overview_rows(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
    network_id: bytes,
) -> None:
    """A row per node: its device's name (a rename too), its area (its own, else its loads'), and how it is heard."""
    hub = hub_of(init_integration)
    devices = dr.async_get(hass)
    socket_node = hub.device_ids[f"node:{NODE_SOCKET}"]
    devices.async_update_device(socket_node, name_by_user="Boiler socket")
    utility = ar.async_get(hass).async_create("Utility room")
    devices.async_update_device(
        hub.device_ids[f"node:{NODE_GATEWAY}"], area_id=utility.id
    )
    mock_bluetooth_env["callbacks"][0](
        make_service_info(network_id, address=PROXY_ADDRESS, rssi=-71),
        BluetoothChange.ADVERTISEMENT,
    )
    mock_bluetooth_env["heard_by"][MAC_LIGHT_SWITCH] = [
        SimpleNamespace(
            scanner=SimpleNamespace(name="Far proxy"),
            advertisement=SimpleNamespace(rssi=-90),
        ),
        SimpleNamespace(
            scanner=SimpleNamespace(name="Near adapter"),
            advertisement=SimpleNamespace(rssi=-60),
        ),
    ]
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)

    rows = overview_rows(hass)
    assert list(rows) == [
        "Gateway 00DC",
        "WC mirror - Push-button 1-gang",
        "Living room DALI - Push-button 2-gang",
        "Boiler socket",
        "WC ceiling - Push-button 1-gang",
        "2-channel actuator 0400",
    ]
    assert rows["WC mirror - Push-button 1-gang"] == {
        "name": "WC mirror - Push-button 1-gang",
        "area": "WC",  # its light's: the node device has none
        "product": "Push-button 1-gang",
        "reachable": True,
        "last_seen": hub.last_seen[LIGHT_SWITCH].isoformat(),
        "rssi": -71,
        "scanner": "Near adapter",
        "hops": None,
        "proxy": True,
    }
    assert rows["Gateway 00DC"]["area"] == "Utility room"
    assert rows["Gateway 00DC"]["proxy"] is False
    assert rows["Gateway 00DC"]["scanner"] is None
    assert rows["Boiler socket"]["area"] == "Kitchen"

    # what the recorder leaves out
    for uid, attribute in ((UID_OVERVIEW, "nodes"), (UID_UNREACHABLE, "devices")):
        state = hass.states.get(entity_id(hass, "sensor", uid))
        assert state.state_info is not None
        assert attribute in state.state_info["unrecorded_attributes"]
    entry = er.async_get(hass).async_get(entity_id(hass, "sensor", UID_OVERVIEW))
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC

    # without a link the overview stays, with no node reachable and none the proxy
    with patch.object(LinkManager, "visible_proxies", return_value=[]):
        fake_link.drop_link()
        await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
        state = hass.states.get(entity_id(hass, "sensor", UID_OVERVIEW))
        assert state.state == "0"
        rows = {row["name"]: row for row in state.attributes["nodes"]}
        assert {row["reachable"] for row in rows.values()} == {False}
        assert {row["proxy"] for row in rows.values()} == {False}
        assert rows["WC mirror - Push-button 1-gang"]["last_seen"] is not None
    hub.link._link_lost.set()
    await wait_for_link(hass, init_integration)


async def test_a_battery_node_is_asleep_in_the_overview(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """A wall transmitter sleeps: `reachable` is empty rather than a verdict, and it never counts as unreachable."""
    await start_detectors(hass, make_detectors_entry(), fake_link)
    await tick(hass, freezer, NODE_DIAGNOSTICS_INTERVAL)
    rows = overview_rows(hass, UID_OVERVIEW_DETECTORS)
    assert rows["Wall transmitter 1-gang 0520"]["reachable"] is None
    assert rows["Wall transmitter 1-gang 0520"]["product"] == "Wall transmitter 1-gang"
    assert rows["Wall transmitter 2-gang 0530"]["reachable"] is None
    unreachable = hass.states.get(entity_id(hass, "sensor", UID_UNREACHABLE_DETECTORS))
    assert unreachable.state == "0"


async def test_the_helpers_without_a_device_or_an_address(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A node whose device is not registered is named after the node; a node with no MAC is heard by nobody."""
    hub = hub_of(init_integration)
    node = hub.cdb.node_by_addr(SOCKET)
    registry = dr.async_get(hass)
    assert node_label(hub, registry, node) == "Boiler - Socket (metering)"
    with patch.dict(hub.device_ids, {node_identifier(node): "gone"}):
        assert node_label(hub, registry, node) == f"{node.name} 0172"
    assert best_scanner(hass, None) is None


# --------------------------------------------------------------------------- the room central entities


async def test_room_central_entities_start_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A room's entities are hidden (and so not exposed to Assist); the home-wide ones stay visible."""
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    for uid, domain in (
        (UID_ROOM_LIGHTS, "light"),
        (UID_ROOM_SOCKETS, "switch"),
    ):
        entry = registry.async_get(entity_id(hass, domain, uid))
        assert entry.hidden_by is er.RegistryEntryHider.INTEGRATION
        assert entry.disabled_by is None
        assert hass.states.get(entry.entity_id) is not None  # it works all the same
        assert not async_should_expose(hass, "conversation", entry.entity_id)
    all_lights = registry.async_get(entity_id(hass, "light", UID_ALL_LIGHTS))
    assert all_lights.hidden_by is None
    assert async_should_expose(hass, "conversation", all_lights.entity_id)


async def test_an_existing_room_entity_keeps_its_visibility(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> None:
    """Decision M9: the default reaches new registrations only; one registered before stays as it was."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    before = registry.async_get_or_create(
        "light", DOMAIN, UID_ROOM_LIGHTS, config_entry=mock_config_entry
    )
    assert before.hidden_by is None
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    kept = registry.async_get(before.entity_id)
    assert kept.hidden_by is None
    assert kept.entity_id == before.entity_id
    new = registry.async_get(entity_id(hass, "switch", UID_ROOM_SOCKETS))
    assert new.hidden_by is er.RegistryEntryHider.INTEGRATION


# --------------------------------------------------------------------------- the user guide's card


def documented_card() -> dict[str, Any]:
    """The Markdown card of the user guide's *Mesh health dashboard* section."""
    text = DOCS.read_text(encoding="utf-8")
    section = text.split("## Mesh health dashboard", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"^```yaml\n(.*?)^```$", section, re.MULTILINE | re.DOTALL)
    cards = [card for block in blocks if (card := yaml.safe_load(block)).get("type")]
    assert len(cards) == 1
    return cards[0]


async def test_the_documented_card_renders_the_overview(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The card's template, rendered by Home Assistant's template engine, is the table the guide promises."""
    card = documented_card()
    assert card["type"] == "markdown"
    content = card["content"]
    assert DOCUMENTED_OVERVIEW in content
    overview = entity_id(hass, "sensor", UID_OVERVIEW)
    miss(hub_of(init_integration), SOCKET)
    fake_link.inject(LIGHT_SWITCH, GROUP_SWITCH, onoff_status(True))
    await tick(hass, freezer, 125)
    rendered = Template(
        content.replace(DOCUMENTED_OVERVIEW, overview), hass
    ).async_render(parse_result=False)
    lines = [line for line in rendered.splitlines() if line.strip()]
    assert (
        lines[0]
        == f"**{MAINS_NODES - 1}** of {MAINS_NODES} mains-powered devices answer."
    )
    assert (
        lines[1] == "| Device | Area | Answers | Last seen | Signal | Heard by | Hops |"
    )
    rows = lines[3:]
    assert len(rows) == MAINS_NODES
    assert rows[0].startswith(
        "| 2-channel actuator 0400 | Kitchen | yes |"
    )  # sorted by name
    assert (
        f"| WC mirror - Push-button 1-gang (proxy) | WC | yes | 2 minutes ago | {NONE} | {NONE} | {NONE} |"
        in rows
    )
    assert (
        f"| Boiler - Socket (metering) | Kitchen | **no** | 2 minutes ago | {NONE} | {NONE} | {NONE} |"
        in rows
    )
    # nothing to show: the card says so rather than failing
    empty = Template(content, hass).async_render(parse_result=False)
    assert "**unknown** of 0 mains-powered devices answer." in empty
