"""The hub over the simulated mesh (`tests/sim`): relays, the proxy filter, replay lists and SAR as nodes run them.

The rest of the suite drives the hub through `FakeProxyLink`, an idealised proxy that answers what a test tells it
to. Here every PDU the hub writes goes through the simulated proxy node onto a simulated air, to nodes that keep
their own state, sequence numbers and replay lists; every test ends on the simulation's invariants (`sim_mesh`:
no (SRC, IV, SEQ) twice, nothing replayed or undecryptable, nothing lost that the loss model did not drop).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)

from custom_components.junghome_ble.jhmesh import messages as M

from .conftest import setup_entry, wait_for_link, wait_until
from .helpers import (
    LIGHT_OUT1,
    LIGHT_SWITCH,
    NODE_ACTUATOR,
    OUR_ADDRESS,
    UID_LIGHT_SWITCH,
    entity_id,
)
from .property_helpers import real_wait
from .sim import Quirks

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import SimLinks
    from .sim import Mesh, SimNode

UID_LIGHT_OUT1 = f"{NODE_ACTUATOR}-0001"
ACTUATOR = LIGHT_OUT1  # the 2-channel actuator's node: its first channel is its primary element


def element_state(mesh: Mesh, element: int) -> Any:
    """The simulated application state of `element` (on / lightness / ...)."""
    servers = mesh.node(element).servers
    assert servers is not None
    return servers.state[element]


def got(node: SimNode, opcode: int) -> list[Any]:
    """The access messages with `opcode` the simulated node accepted from the hub."""
    return [m for m in node.received if m.src == OUR_ADDRESS and m.opcode == opcode]


async def start(hass: HomeAssistant, entry: MockConfigEntry, eid_uid: str) -> str:
    """Set the entry up over the simulated mesh; return the entity id of the light `eid_uid` once it has a state."""
    await setup_entry(hass, entry)
    await wait_for_link(hass, entry)
    eid = entity_id(hass, "light", eid_uid)
    await wait_until(
        hass,
        lambda: hass.states.get(eid).state in (STATE_ON, STATE_OFF),
        what=f"a state of {eid}",
    )
    return eid


async def test_the_entry_loads_and_a_lights_state_comes_from_the_simulated_server(
    hass: HomeAssistant, sim_mesh: Mesh, sim_link: SimLinks, sim_entry: MockConfigEntry
) -> None:
    element_state(
        sim_mesh, LIGHT_SWITCH
    ).on = True  # the load is on before Home Assistant asks
    eid = await start(hass, sim_entry, UID_LIGHT_SWITCH)
    assert hass.states.get(eid).state == STATE_ON
    hub = sim_entry.runtime_data
    assert hub.connected
    assert (
        hub.proxy_node == LIGHT_SWITCH
    )  # named by the simulated proxy's Filter Status
    assert sim_link.connect_count == 1
    assert got(sim_mesh.node(LIGHT_SWITCH), M.GEN_ONOFF_GET)  # asked, over the air


async def test_turning_a_light_on_changes_the_simulated_load(
    hass: HomeAssistant, sim_mesh: Mesh, init_sim_integration: MockConfigEntry
) -> None:
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    await wait_until(hass, lambda: hass.states.get(eid).state == STATE_OFF)
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert element_state(sim_mesh, LIGHT_SWITCH).on
    await wait_until(hass, lambda: hass.states.get(eid).state == STATE_ON)
    # the load answered JUNG's way: by publishing its new state to its element group, which the hub hears
    servers = sim_mesh.node(LIGHT_SWITCH).servers
    assert servers is not None
    assert any(element == LIGHT_SWITCH for element, _, _ in servers.published)


async def test_a_node_four_hops_away_answers(
    hass: HomeAssistant, sim_mesh: Mesh, sim_entry: MockConfigEntry
) -> None:
    assert sim_mesh.topology.hops(LIGHT_SWITCH, ACTUATOR) == 4
    element_state(sim_mesh, LIGHT_OUT1).on = True
    eid = await start(hass, sim_entry, UID_LIGHT_OUT1)
    assert (
        hass.states.get(eid).state == STATE_ON
    )  # the refresh's Get there and back, through four relays
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert not element_state(sim_mesh, LIGHT_OUT1).on
    await wait_until(hass, lambda: hass.states.get(eid).state == STATE_OFF)
    assert got(sim_mesh.node(ACTUATOR), M.GEN_ONOFF_SET)


@pytest.mark.parametrize(
    "sim_options",
    [{"quirks": Quirks(proxy_forwards_every_copy=True), "retransmissions": True}],
)
async def test_every_relayed_copy_the_proxy_forwards_is_handled_once(
    hass: HomeAssistant, sim_mesh: Mesh, init_sim_integration: MockConfigEntry
) -> None:
    """Network retransmissions and relays put several copies of each PDU on the air, and this proxy forwards them
    all (`FakeProxyLink` never does): the hub's replay protection hands each message out once."""
    eid = entity_id(hass, "light", UID_LIGHT_SWITCH)
    await wait_until(hass, lambda: hass.states.get(eid).state == STATE_OFF)
    changes: list[Event] = []
    hass.bus.async_listen(
        "state_changed",
        lambda event: changes.append(event) if event.data["entity_id"] == eid else None,
    )
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    await wait_until(hass, lambda: hass.states.get(eid).state == STATE_ON)
    await real_wait(0.2)  # the last copies land
    assert len(changes) == 1
    assert len(got(sim_mesh.node(LIGHT_SWITCH), M.GEN_ONOFF_SET)) == 1
    assert sim_mesh.proxy(LIGHT_SWITCH).repeats_forwarded  # copies did reach the hub
