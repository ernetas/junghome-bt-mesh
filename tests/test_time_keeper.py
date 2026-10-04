"""The PP2 pucks' time keeper (review-4 F4-14): the switch, the Config and Time Role messages, the repair issue.

The configurator runs against the `env` of `test_services.py` (a copy of the Android export, which has a PP2 puck at
0400, the Config Server answered by a stub), so the Config messages and the rewritten export are checked. Nothing of
this has been seen on air: there is no puck in the installation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.junghome_ble import services as svc
from custom_components.junghome_ble import switch as SW
from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_TIME_KEEPER_MISSING,
    NODE_INFO_TIME_ROLE,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import TIME_KEEPER_GROUP
from custom_components.junghome_ble.jhmesh.pdu import encode_opcode

from . import property_helpers as ph
from . import test_services as services_env
from .test_services import Env, settled

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

env, export_source = services_env.env, services_env.export_source
fast_timeouts = ph.fast_timeouts
no_entity_loses_its_state = services_env.no_entity_loses_its_state

SOCKET = 0x0172
PUCK = 0x0400
KEEPER_GROUP = 0xFEFF


def issue(hass: HomeAssistant, env: Env) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_TIME_KEEPER_MISSING}_{env.entry.entry_id}"
    )


# --------------------------------------------------------------------------- the configurator


async def test_set_time_keeper(hass: HomeAssistant, env: Env) -> None:
    """On: the Time Server publishes to FEFF, the CDB gets the app's `#time_keeper_group#`; off: Publication Set
    0x0000. Bound first where the export shows the Time Server unbound."""
    configurator = hass.data[svc.CONFIGURATORS][env.entry.entry_id]
    pf = env.reload()
    assert KEEPER_GROUP not in pf.cdb.groups
    await svc.async_configure(
        hass, env.entry.entry_id, lambda c: c.set_time_keeper(SOCKET, True)
    )
    await settled(hass, env)
    pf = env.reload()
    assert pf.cdb.groups[KEEPER_GROUP] == TIME_KEEPER_GROUP
    assert pf.publication(SOCKET, "1200") == KEEPER_GROUP
    assert [p for _n, p in env.config_calls] == [
        C.model_publication_set(SOCKET, KEEPER_GROUP, "1200")
    ]
    env.config_calls.clear()
    configurator = hass.data[svc.CONFIGURATORS][env.entry.entry_id]
    assert await configurator.set_time_keeper(SOCKET, False) is True
    assert [p for _n, p in env.config_calls] == [
        C.model_publication_set(SOCKET, 0, "1200")
    ]
    assert env.reload().publication(SOCKET, "1200") is None
    # unbound in the export: bound first; the group is there already
    pf = env.reload()
    model = next(m for m in pf.cdb.element(SOCKET).raw_models if m["modelId"] == "1200")
    model["bind"] = []
    pf.save(env.path, force=True)
    env.config_calls.clear()
    configurator = hass.data[svc.CONFIGURATORS][env.entry.entry_id]
    assert await configurator.set_time_keeper(SOCKET, True) is True
    assert [p for _n, p in env.config_calls] == [
        C.model_app_bind(SOCKET, "1200", 0),
        C.model_publication_set(SOCKET, KEEPER_GROUP, "1200"),
    ]
    assert sum(1 for g in env.reload().net["groups"] if g["address"] == "FEFF") == 1


async def test_set_time_keeper_refusals(hass: HomeAssistant, env: Env) -> None:
    configurator = hass.data[svc.CONFIGURATORS][env.entry.entry_id]
    for unicast in (0x7FFF, 0x0001):  # no such node; a phone without a Time Server
        with pytest.raises(ServiceValidationError) as exc:
            await configurator.set_time_keeper(unicast, True)
        assert exc.value.translation_key == "service_unknown_element"


# --------------------------------------------------------------------------- the switch


def test_the_switch_only_in_a_project_with_pucks() -> None:
    """Every mains node with a Time Server gets one — not the gateway, not a battery device — and only while the
    project has a PP2 puck."""
    hub = ph.fake_hub()
    entities = [
        e for e in SW.build_entities(hub) if isinstance(e, SW.JungHomeTimeKeeper)
    ]
    assert sorted(e.node.unicast for e in entities) == [
        0x0148,
        0x0172,
        0x0232,
        0x0300,
        0x0400,
    ]
    keeper = next(e for e in entities if e.node.unicast == SOCKET)
    assert keeper.unique_id == f"{keeper.node.uuid.lower()}-time_keeper"
    assert keeper.address == SOCKET  # the Time Setup Server's element
    assert keeper.entity_registry_enabled_default is False
    puck = hub.cdb.node_by_addr(PUCK)
    puck.pid = 0x0004  # a switch actuator mini: no PP2 puck left
    assert not any(isinstance(e, SW.JungHomeTimeKeeper) for e in SW.build_entities(hub))


async def test_the_switch_sets_the_role_after_the_publication(
    hass: HomeAssistant, env: Env
) -> None:
    hub = env.hub
    node = hub.cdb.node_by_addr(SOCKET)
    entity = SW.JungHomeTimeKeeper(hub, node)
    entity.hass = hass
    assert entity.is_on is False  # the fake mesh's nodes answer "client"
    configurator = AsyncMock()

    async def run(hass: HomeAssistant, entry_id: str, operation: Any) -> None:
        await operation(configurator)

    def role_status(role: int) -> Any:
        return AsyncMock(
            return_value=type(
                "Reply", (), {"params": bytes([role])}
            )()  # what the node answers to the Set
        )

    with (
        patch.object(SW, "async_configure", run),
        patch.object(hub.proxy, "request", role_status(2)) as request,
    ):
        await entity.async_turn_on()
    assert configurator.set_time_keeper.await_args_list == [((SOCKET, True),)]
    assert request.await_args.args[:3] == (
        SOCKET,
        M.time_role_set(2),
        M.TIME_ROLE_STATUS,
    )
    assert hub.node_info(SOCKET)[NODE_INFO_TIME_ROLE] == b"\x02"
    assert entity.is_on is True
    with (
        patch.object(SW, "async_configure", run),
        patch.object(hub.proxy, "request", role_status(3)) as request,
    ):
        await entity.async_turn_off()
    assert request.await_args.args[1] == M.time_role_set(3)
    assert entity.is_on is False
    # the node does not take its role: the error says the publication changed already
    for failure in (TimeoutError(), ConnectionError("gone"), role_status(9)):
        mock = (
            failure
            if isinstance(failure, AsyncMock)
            else AsyncMock(side_effect=failure)
        )
        with (
            patch.object(SW, "async_configure", run),
            patch.object(hub.proxy, "request", mock),
            pytest.raises(HomeAssistantError) as caught,
        ):
            await entity.async_turn_on()
        assert caught.value.translation_key == "time_keeper_role_failed"
        assert caught.value.translation_placeholders["address"] == "0172"
    assert hub.node_info(SOCKET)[NODE_INFO_TIME_ROLE] == b"\x03"


async def test_a_role_not_answered_yet_is_unknown(
    hass: HomeAssistant, env: Env
) -> None:
    hub = env.hub
    entity = SW.JungHomeTimeKeeper(hub, hub.cdb.node_by_addr(SOCKET))
    with patch.object(hub, "node_info", return_value={}):
        assert entity.is_on is None


# --------------------------------------------------------------------------- the repair


async def test_the_repair_while_no_node_keeps_the_pucks_time(
    hass: HomeAssistant, env: Env
) -> None:
    """Once every candidate answered its role and all said "client" (as every node on air does), the repair names
    the puck; a node answering relay or authority clears it; a role not asked yet raises nothing; without a puck
    there is none either."""
    hub = env.hub
    candidates = [0x0148, 0x0172, 0x0232, 0x0300, PUCK]
    for unicast in candidates:
        hub.node_info(unicast).pop(NODE_INFO_TIME_ROLE, None)
    hub.issues.report_time_keeper()
    assert issue(hass, env) is None
    for unicast in candidates:
        assert issue(hass, env) is None  # until the last one answered
        hub.remember_node_info(unicast, NODE_INFO_TIME_ROLE, b"\x03")
    found = issue(hass, env)
    assert found is not None
    assert found.translation_placeholders["pucks"] == "0400"
    assert not found.is_fixable
    hub.remember_node_info(SOCKET, NODE_INFO_TIME_ROLE, b"\x02")
    assert issue(hass, env) is None
    hub.remember_node_info(
        SOCKET, NODE_INFO_TIME_ROLE, b"\x01"
    )  # an authority keeps it too
    assert issue(hass, env) is None
    hub.remember_node_info(SOCKET, NODE_INFO_TIME_ROLE, b"\x03")
    assert issue(hass, env) is not None
    hub.cdb.node_by_addr(PUCK).pid = 0x0004
    hub.issues.report_time_keeper()
    assert issue(hass, env) is None


@pytest.mark.unavailable_ok  # enabling the switch takes a reload
async def test_the_switch_over_the_fake_mesh(
    hass: HomeAssistant, env: Env, fast_timeouts: None
) -> None:
    """End to end: the enabled switch's turn_on sends the Publication Set to FEFF, then Time Role Set to the Time
    Setup Server; the Status the node sends is kept as its role and shows."""
    registry = er.async_get(hass)
    node = env.hub.cdb.node_by_addr(SOCKET)
    eid = registry.async_get_entity_id(
        "switch", DOMAIN, f"{node.uuid.lower()}-time_keeper"
    )
    assert eid is not None
    registry.async_update_entity(eid, disabled_by=None)
    await hass.config_entries.async_reload(env.entry.entry_id)
    await settled(hass, env)
    serve = env.link.app_reply

    def role(element: int, access: bytes) -> bytes | None:
        if access[:2] == bytes.fromhex("8239"):
            env.app_calls.append((element, access))
            return encode_opcode(M.TIME_ROLE_STATUS) + access[2:3]
        return serve(element, access) if serve is not None else None

    env.link.app_reply = role
    env.config_calls.clear()
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": eid}, blocking=True
    )
    assert [p for _n, p in env.config_calls] == [
        C.model_publication_set(SOCKET, KEEPER_GROUP, "1200")
    ]
    assert (SOCKET, M.time_role_set(2)) in env.app_calls
    assert env.hub.node_info(SOCKET)[NODE_INFO_TIME_ROLE] == b"\x02"
    assert hass.states.get(eid).state == "on"
    assert issue(hass, env) is None
