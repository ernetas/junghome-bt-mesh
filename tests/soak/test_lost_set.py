"""A load's Set lost on the air while a Get to the same element is out (review-4 D32), over the simulated mesh.

The flapping-link soak (`test_hub_soak.py`, seeds 4, 5 and 14) found it: the new link's refresh asks a load for
its state while a command to it is out; the command's Set is lost, the Get's Status shows the old state, and it
answered the Set — the service call succeeded although the load never changed. Here the loss is a targeted drop of
the Set, so the case comes every run, on virtual time (`conftest.py`): the Set's retry waits out a real-length
request timeout in no time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON, STATE_OFF, STATE_ON

from custom_components.junghome_ble.const import LINK_CONNECTED
from custom_components.junghome_ble.jhmesh import messages as M
from tests.conftest import setup_entry
from tests.helpers import LIGHT_OUT1, NODE_ACTUATOR, OUR_ADDRESS, entity_id

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.conftest import SimLinks
    from tests.sim import Mesh
    from tests.sim.loss import Packet

pytestmark = [pytest.mark.sim]

UID_FAR = f"{NODE_ACTUATOR}-0001"  # the 2-channel actuator's first channel (0400), four hops from the proxy


async def until(predicate: Callable[[], bool], what: str, limit: float = 60.0) -> None:
    """Let virtual time pass until `predicate` holds; fail after `limit` virtual seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while not predicate():
        assert loop.time() < deadline, f"{what} not reached within {limit:g} virtual s"
        await asyncio.sleep(0.01)


def load(mesh: Mesh, element: int) -> Any:
    """The simulated application state of `element`."""
    servers = mesh.node(element).servers
    assert servers is not None
    return servers.state[element]


async def test_a_set_lost_while_a_get_is_out_is_not_confirmed_by_the_gets_status(
    hass: HomeAssistant,
    sim_mesh: Mesh,
    sim_link: SimLinks,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    hub = mock_config_entry.runtime_data
    far = entity_id(hass, "light", UID_FAR)
    await until(lambda: hub.link_state == LINK_CONNECTED, "the connect-time refresh")
    assert hass.states.get(far).state == STATE_OFF

    def to_the_load(packet: Packet) -> bool:
        return (
            packet.bearer == "gatt-in"
            and packet.net is not None
            and (packet.net.src, packet.net.dst) == (OUR_ADDRESS, LIGHT_OUT1)
        )

    lost = sim_mesh.loss.drop(to_the_load, name="the Set")  # the next PDU to the load
    command = hass.async_create_task(
        hass.services.async_call(
            LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: far}, blocking=True
        )
    )
    await until(lambda: lost.exhausted, "the Set lost")
    # a refresh asks the load for its state while the command waits: answered with the old state, off
    refresh = hass.async_create_task(hub.async_refresh_element(LIGHT_OUT1, "switch"))
    await command
    await refresh
    assert load(sim_mesh, LIGHT_OUT1).on, "the command succeeded, the load is off"
    received = sim_mesh.node(LIGHT_OUT1).received
    sets = [m for m in received if m.src == OUR_ADDRESS and m.opcode == M.GEN_ONOFF_SET]
    assert len(sets) == 1  # the second attempt: the first was lost
    await until(lambda: hass.states.get(far).state == STATE_ON, "the light on")

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
