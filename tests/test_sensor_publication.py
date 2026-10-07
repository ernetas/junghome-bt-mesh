"""*Sensor values for IoT systems* (the app's S10): the switch, and the Sensor Server publications it sets.

The configurator runs against the `env` of `test_services.py` (a copy of the Android export, the Config Server
answered by a stub), so the Config messages and the rewritten export are checked.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.junghome_ble import mesh_config
from custom_components.junghome_ble import switch as SW
from custom_components.junghome_ble.actions import common as action_common
from custom_components.junghome_ble.configurator import thresholds as thresholds_mod
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.entity import node_identifier
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import Metadata, build_devices
from custom_components.junghome_ble.jhmesh.export import raw_model
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode

from . import property_helpers as ph
from . import test_services as services_env
from .conftest import FIXTURES, wait_until
from .helpers import GATEWAY, SOCKET, SOCKET_SENSOR
from .test_services import Env, settled

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

env, export_source = services_env.env, services_env.export_source
fast_timeouts = ph.fast_timeouts
# no entity passes through `unavailable` / `unknown` here either (review-4 D23)
no_entity_loses_its_state = services_env.no_entity_loses_its_state

METER_GROUP = (
    0xC001  # the socket meter element's own group, where its Sensor Server publishes
)


def publication(env: Env) -> int | None:
    return env.reload().publication(env.reload().cdb.element(SOCKET_SENSOR), "1100")


# --------------------------------------------------------------------------- which nodes get the switch


def test_sensor_publication_nodes() -> None:
    """Nodes with a Sensor Server, in a project with a gateway, on device software 1.3.0.0 or later (or unknown)."""
    hub = ph.fake_hub()
    ((node, info),) = SW.sensor_publication_nodes(hub)
    assert node.unicast == SOCKET
    assert info["identifiers"] == {("junghome_ble", f"{node.uuid.lower()}-0001")}
    # an old device, a version that does not parse, no gateway
    with patch.object(SW, "node_version", return_value="1.2.9.9"):
        assert SW.sensor_publication_nodes(hub) == []
    with patch.object(SW, "node_version", return_value="not.a.version.at.all"):
        assert len(SW.sensor_publication_nodes(hub)) == 1
    gateway = next(n for n in hub.cdb.nodes if n.unicast == GATEWAY)
    gateway.pid = 0x01
    assert SW.sensor_publication_nodes(hub) == []


def test_sensor_publication_on_the_node_device() -> None:
    """A node without a socket (a thermostat) has the switch on its node device."""
    hub = ph.fake_hub()
    hub.cdb = CDB.load(Path(FIXTURES / "MeshNetwork-rtr.json"))
    hub.devices = build_devices(hub.cdb, Metadata())
    nodes = {node.unicast: info for node, info in SW.sensor_publication_nodes(hub)}
    rtr = hub.cdb.node_by_addr(0x0500)
    assert nodes[0x0500]["identifiers"] == {("junghome_ble", node_identifier(rtr))}


def test_sensor_publication_state() -> None:
    """On when a Sensor Server publishes to its own group; not to another address, not without a publication."""
    hub = ph.fake_hub()
    node = hub.cdb.node_by_addr(SOCKET)
    meta = hub.cdb.export_meta
    assert mesh_config.sensor_publication(hub.cdb, meta, node)
    model = raw_model(hub.cdb.element(SOCKET_SENSOR), "1100")
    model["publish"]["address"] = "C011"
    assert not mesh_config.sensor_publication(hub.cdb, meta, node)
    del model["publish"]
    assert not mesh_config.sensor_publication(hub.cdb, meta, node)


# --------------------------------------------------------------------------- the configurator


async def test_set_sensor_publication(hass: HomeAssistant, env: Env) -> None:
    """Off: Publication Set 0x0000; on: to the element's group, the model bound first when the export shows it
    unbound; nothing to do: no message, no reload."""
    configurator = hass.data[action_common.CONFIGURATORS][env.entry.entry_id]
    assert publication(env) == METER_GROUP
    assert await configurator.set_sensor_publication(SOCKET, True) is False
    assert env.config_calls == []

    await action_common.async_configure(
        hass, env.entry.entry_id, lambda c: c.set_sensor_publication(SOCKET, False)
    )
    await settled(hass, env)
    assert publication(env) is None
    assert [p for _n, p in env.config_calls] == [
        C.model_publication_set(SOCKET_SENSOR, 0, "1100")
    ]

    pf = env.reload()
    raw_model(pf.cdb.element(SOCKET_SENSOR), "1100")["bind"] = []
    pf.save(env.path, force=True)
    env.config_calls.clear()
    configurator = hass.data[action_common.CONFIGURATORS][env.entry.entry_id]
    assert await configurator.set_sensor_publication(SOCKET, True) is True
    assert [p for _n, p in env.config_calls] == [
        C.model_app_bind(SOCKET_SENSOR, "1100", 0),
        C.model_publication_set(SOCKET_SENSOR, METER_GROUP, "1100"),
    ]
    assert publication(env) == METER_GROUP


async def test_set_sensor_publication_plans_against_the_node(
    hass: HomeAssistant, env: Env
) -> None:
    """Review-4 W4-6: the export says off, the node publishes (the app turned it on since): turning it off sends
    the Publication Set the export alone would skip, and records it; a node that agrees with the export sends
    nothing."""
    pf = env.reload()
    pf.set_publication(SOCKET, pf.cdb.element(SOCKET_SENSOR), "1100", None)
    pf.save(env.path, force=True)
    configurator = hass.data[action_common.CONFIGURATORS][env.entry.entry_id]
    assert await configurator.set_sensor_publication(SOCKET, False) is False
    assert await configurator.set_sensor_publication(SOCKET, False, live=False) is False
    assert env.config_calls == []
    await action_common.async_configure(
        hass,
        env.entry.entry_id,
        lambda c: c.set_sensor_publication(SOCKET, False, live=True),
    )
    await settled(hass, env)
    assert [p for _n, p in env.config_calls] == [
        C.model_publication_set(SOCKET_SENSOR, 0, "1100")
    ]
    assert publication(env) is None


async def test_set_sensor_publication_refusals(hass: HomeAssistant, env: Env) -> None:
    configurator = hass.data[action_common.CONFIGURATORS][env.entry.entry_id]
    with pytest.raises(ServiceValidationError) as exc:
        await configurator.set_sensor_publication(0x7FFF, True)
    assert exc.value.translation_key == "service_unknown_element"
    with (
        patch.object(thresholds_mod, "element_groups", return_value={}),
        pytest.raises(ServiceValidationError) as exc,
    ):
        await configurator.set_sensor_publication(SOCKET, True)
    assert exc.value.translation_key == "service_no_element_group"


# --------------------------------------------------------------------------- the switch


async def test_sensor_publication_switch(hass: HomeAssistant, env: Env) -> None:
    """The state comes from the export; turning it on / off runs the configurator for the node."""
    hub = env.hub
    ((node, info),) = SW.sensor_publication_nodes(hub)
    entity = SW.JungHomeSensorPublication(hub, node, info)
    entity.hass = hass
    assert entity.unique_id == f"{node.uuid.lower()}-sensor_publication"
    assert entity.extra_state_attributes == {"mesh_address": "0172"}
    assert entity.is_on
    configurator = AsyncMock()
    calls: list[Any] = []

    async def run(hass: HomeAssistant, entry_id: str, operation: Any) -> None:
        calls.append(entry_id)
        await operation(configurator)

    with patch.object(SW, "async_configure", run):
        await entity.async_turn_off()
        await entity.async_turn_on()
    assert calls == [env.entry.entry_id] * 2
    assert configurator.set_sensor_publication.await_args_list == [
        ((SOCKET, False), {"live": None}),
        ((SOCKET, True), {"live": None}),
    ]
    # once the node answered, the switch plans against that (W4-6)
    entity._published = True
    configurator.reset_mock()
    with patch.object(SW, "async_configure", run):
        await entity.async_turn_off()
    assert configurator.set_sensor_publication.await_args_list == [
        ((SOCKET, False), {"live": True})
    ]
    assert P.parse_version("1.3.0.0") == SW.SENSOR_PUBLICATION_MIN_VERSION


def serve_publications(env: Env, publications: dict[int, bytes]) -> None:
    """Have the nodes' Configuration Servers answer a Publication Get with `publications[element]` (status, then
    the publish address), silence for an element not listed; every other Config request as before."""
    config_server = env.link.config_reply
    assert config_server is not None

    def reply(node: int, pdu: bytes) -> bytes | None:
        op, _cid, params = decode_opcode(pdu)
        if op != C.CONFIG_MODEL_PUBLICATION_GET:
            return config_server(node, pdu)
        env.config_calls.append((node, pdu))
        answer = publications.get(int.from_bytes(params[:2], "little"))
        if answer is None:
            return None
        # [status][element][publish address][AppKey index + credential][TTL][period][retransmit][model]
        return (
            encode_opcode(C.CONFIG_MODEL_PUBLICATION_STATUS)
            + answer[:1]
            + params[:2]
            + answer[1:3]
            + bytes(5)
            + params[2:]
        )

    env.link.config_reply = reply


@pytest.mark.unavailable_ok  # each answer is read by a reload of its own
@pytest.mark.slow_ok  # five reloads, each settling its link's refresh: every silent Get waits `REPLY_TIMEOUT`
async def test_sensor_publication_is_read_from_the_node(
    hass: HomeAssistant,
    env: Env,
    fast_timeouts: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """net:uc:checkpublicationforsensorserver: the node's Publication Get answer, not the export, once per link; a
    node that stays silent or refuses leaves the export's verdict; any publish address counts (the app's test)."""
    caplog.set_level(logging.DEBUG, logger=SW.__name__)
    ((node, _info),) = SW.sensor_publication_nodes(env.hub)
    uid = f"{node.uuid.lower()}-sensor_publication"
    registry = er.async_get(hass)
    eid = registry.async_get_entity_id("switch", DOMAIN, uid)
    assert eid is not None
    publications = {SOCKET_SENSOR: b"\x00\x00\x00"}  # success, publishes nowhere
    serve_publications(env, publications)
    get = (SOCKET, C.model_publication_get(SOCKET_SENSOR, "1100"))
    assert get not in env.config_calls  # a disabled entity asks nothing

    registry.async_update_entity(eid, disabled_by=None)
    await hass.config_entries.async_reload(env.entry.entry_id)
    await settled(hass, env)
    await wait_until(hass, lambda: hass.states.get(eid).state == "off")
    assert (
        env.config_calls.count(get) == 1
    )  # the export says on: the node's answer wins

    # another address than the element's group is on too (the app only tests for an address)
    publications[SOCKET_SENSOR] = b"\x00\x11\xc0"
    await hass.config_entries.async_reload(env.entry.entry_id)
    await settled(hass, env)
    await wait_until(hass, lambda: env.config_calls.count(get) == 2)
    await wait_until(hass, lambda: hass.states.get(eid).state == "on")

    # a refusal and silence are no answer: the export's verdict (on) stays
    for answer in (b"\x02\x00\x00", None):
        calls = env.config_calls.count(get)
        if answer is None:
            del publications[SOCKET_SENSOR]
        else:
            publications[SOCKET_SENSOR] = answer
        await hass.config_entries.async_reload(env.entry.entry_id)
        await settled(hass, env)
        await wait_until(hass, lambda calls=calls: env.config_calls.count(get) > calls)
        if answer is None:  # every attempt goes unanswered before the read gives up
            await wait_until(hass, lambda: "no publication of" in caplog.text)
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "on"


async def test_the_switch_asks_the_node_again_after_a_change(
    hass: HomeAssistant, env: Env, fast_timeouts: None
) -> None:
    """Followed in place (review-4 D23): the switch keeps its entity and the link; what the node answered before
    the change is dropped — the export, which recorded what the node took, shows meanwhile — and the node is asked
    again at once, as the entity a reload made would have done."""
    ((node, _info),) = SW.sensor_publication_nodes(env.hub)
    registry = er.async_get(hass)
    eid = registry.async_get_entity_id(
        "switch", DOMAIN, f"{node.uuid.lower()}-sensor_publication"
    )
    assert eid is not None
    publications = {SOCKET_SENSOR: b"\x00\x01\xc0"}  # success, publishes to its group
    serve_publications(env, publications)
    registry.async_update_entity(eid, disabled_by=None)
    await hass.config_entries.async_reload(env.entry.entry_id)  # enabling it takes one
    await settled(hass, env)
    env.transitions.seen.clear()
    await wait_until(hass, lambda: hass.states.get(eid).state == "on")
    get = (SOCKET, C.model_publication_get(SOCKET_SENSOR, "1100"))
    asked, hub = env.config_calls.count(get), env.hub
    publications[SOCKET_SENSOR] = (
        b"\x00\x00\x00"  # what the node answers after the change
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": eid}, blocking=True
    )
    await wait_until(hass, lambda: env.config_calls.count(get) > asked)
    await wait_until(hass, lambda: hass.states.get(eid).state == "off")
    assert env.hub is hub
    assert services_env.runs_the_export(env)
