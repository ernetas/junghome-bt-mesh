"""Following a rewritten export without a reload (review-4 D23, `model_update.py`).

The actions run against the `env` of `test_services.py` (a copy of the Android export, the nodes' servers answered
by stubs over the fake link), whose check applies here too: no entity passes through `unavailable` or `unknown`,
and the hub keeps the link it came up with. Then what each kind of change does to the entities, and the reload that
stands in for whatever cannot be followed in place.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity

from custom_components.junghome_ble import model_update
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.coordinator import JungHomeHub, load_network
from custom_components.junghome_ble.entity import JungHomeEntity
from custom_components.junghome_ble.gateway_api import GatewayError, JungHomeGatewayApi
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.crypto import AppKeyMaterial, NetKeyMaterial
from custom_components.junghome_ble.jhmesh.export import ProjectFile
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode

from . import test_services as services_env
from .conftest import StateTransitions, settle, wait_until
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    MESH_UUID,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    UID_ROCKER_A,
    entity_id,
)
from .test_services import OUR, Env, call, runs_the_export, use_gateway

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

env, export_source = services_env.env, services_env.export_source
no_entity_loses_its_state = services_env.no_entity_loses_its_state

LIGHTS = (UID_LIGHT_SWITCH, UID_LIGHT_DIMMER, UID_LIGHT_CTL)
STATE_GETS = frozenset({M.GEN_ONOFF_GET, M.LIGHT_LIGHTNESS_GET, M.LIGHT_CTL_GET})
VENDOR_GET = 0x02  # LBC Property Get (JUNG): what a config entity's read sends


def room_entity(hass: HomeAssistant, room: int, kind: str = "lights") -> str | None:
    """The entity id of a room's central entity, None once the registry has none."""
    domain = {"lights": "light", "sockets": "switch"}[kind]
    unique_id = f"{MESH_UUID.lower()}-room-{room:04x}-{kind}"
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


async def switch_on(
    hass: HomeAssistant, env: Env, *addresses: int, on: bool = True
) -> None:
    """The loads publish a Generic OnOff Status, as after a key press."""
    for address in addresses:
        env.link.inject(address, OUR, encode_opcode(M.GEN_ONOFF_STATUS) + bytes([on]))
    await hass.async_block_till_done()


def reads(env: Env) -> list[tuple[int, int]]:
    """The state and property Gets sent so far: (element, opcode)."""
    out = []
    for _src, dst, pdu in env.link.sent:
        op, cid, _params = decode_opcode(pdu)
        if (cid is None and op in STATE_GETS) or (
            cid == M.JUNG_CID and op == VENDOR_GET
        ):
            out.append((dst, op))
    return out


# ----------------------------------------------------------------------------- the actions


ACTIONS: dict[str, tuple[str, Any]] = {
    "rename_room": ("rename_room", {"room": "WC", "new_name": "Loo"}),
    "set_room": (
        "set_room",
        {"entity_id": ("light", UID_LIGHT_SWITCH), "room": "Living room"},
    ),
    "add_to_room": (
        "add_to_room",
        {"entity_id": ("light", UID_LIGHT_SWITCH), "room": "Living room"},
    ),
    "remove_from_room": (
        "remove_from_room",
        {"entity_id": ("light", UID_LIGHT_CTL), "room": "Living room"},
    ),
    "assign_key": (
        "assign_key",
        {
            "key_entity": ("event", UID_ROCKER_A),
            "target_entity": ("light", UID_LIGHT_DIMMER),
        },
    ),
    "store_scene": (
        "store_scene",
        {"entity_id": ("light", UID_LIGHT_DIMMER), "scene": "1"},
    ),
    "create_scene": ("create_scene", {"name": "Movie night"}),
    "delete_room": ("delete_room", {"room": "Kitchen"}),
}


@pytest.mark.parametrize("action", ACTIONS)
async def test_an_action_leaves_the_lights_as_they_are(
    hass: HomeAssistant, env: Env, action: str
) -> None:
    """The headline of D23: `rename_room` (nothing on air), `set_room`, `assign_key`, `store_scene` … keep every light
    in its state — not even `last_changed` moves — on the same hub, link and states cache, and nothing is read over
    the mesh again (a reload asked every load and every enabled setting)."""
    hub, states = env.hub, env.hub.states
    await switch_on(hass, env, LIGHT_SWITCH, LIGHT_DIMMER, LIGHT_CTL)
    lights = [entity_id(hass, "light", uid) for uid in LIGHTS]
    before = {eid: hass.states.get(eid) for eid in lights}
    assert {s.state for s in before.values() if s is not None} == {"on"}
    asked = reads(env)
    service, data = ACTIONS[action]
    data = {
        key: entity_id(hass, *value) if isinstance(value, tuple) else value
        for key, value in data.items()
    }
    await call(hass, service, data)
    await settle(hass)
    assert env.hub is hub
    assert hub.states is states
    assert (hub.connected, hub.link_count) == (True, 1)
    assert runs_the_export(env)
    for eid, old in before.items():
        now = hass.states.get(eid)
        assert now is not None
        assert old is not None
        assert (now.state, now.last_changed) == ("on", old.last_changed)
    assert reads(env) == asked


async def test_a_renamed_room_renames_its_entities_and_the_lights_rooms(
    hass: HomeAssistant, env: Env
) -> None:
    """The room's *All lights in* entity takes the new name (its translation placeholder), keeping its entity id;
    the lights list the new name in `rooms`."""
    central = room_entity(hass, 0xC00F)
    assert central is not None
    old_name = hass.states.get(central).attributes["friendly_name"]
    assert "WC" in old_name
    await call(hass, "rename_room", {"room": "WC", "new_name": "Loo"})
    assert room_entity(hass, 0xC00F) == central
    new_name = hass.states.get(central).attributes["friendly_name"]
    assert new_name == old_name.replace("WC", "Loo")
    assert er.async_get(hass).async_get(central).original_name == "All lights in Loo"
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    assert hass.states.get(light).attributes["rooms"] == ["Loo"]


async def test_a_light_moving_room_moves_between_the_room_entities(
    hass: HomeAssistant, env: Env
) -> None:
    """`set_room`: the members follow, and so do the room entities' subscriptions — the room the light joined
    follows its state from then on, the room it left no longer does."""
    wc, living = room_entity(hass, 0xC00F), room_entity(hass, 0xC010)
    assert wc is not None
    assert living is not None
    await switch_on(hass, env, LIGHT_SWITCH, on=True)
    await switch_on(hass, env, LIGHT_DIMMER, LIGHT_CTL, on=False)
    assert (hass.states.get(wc).state, hass.states.get(living).state) == ("on", "off")
    await call(
        hass,
        "set_room",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
            "room": "Living room",
        },
    )
    assert set(hass.states.get(living).attributes["members"]) == {
        "WC mirror",
        "Living room DALI",
    }
    assert hass.states.get(wc).attributes["members"] == ["WC ceiling"]
    assert (hass.states.get(wc).state, hass.states.get(living).state) == ("off", "on")
    await switch_on(hass, env, LIGHT_SWITCH, on=False)
    assert hass.states.get(living).state == "off"  # it hears the light now
    await switch_on(hass, env, LIGHT_SWITCH, on=True)
    assert hass.states.get(wc).state == "off"  # and the WC room no longer does
    assert hass.states.get(living).state == "on"


async def test_a_light_in_two_rooms_counts_in_both_and_leaves_one_in_place(
    hass: HomeAssistant, env: Env, state_transitions: StateTransitions
) -> None:
    """Review-4 F4-5: `add_to_room` puts the WC light into Living room as well — both room entities list it and
    follow it — and `remove_from_room` takes it out of WC (`force`: the WC-linked key drives it) — WC no longer
    follows it. In place: the light never changes state, and no room entity flaps through `unavailable` or
    `unknown`; their only changes are the ones the light's own state explains."""
    wc, living = room_entity(hass, 0xC00F), room_entity(hass, 0xC010)
    assert wc is not None
    assert living is not None
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    await switch_on(hass, env, LIGHT_SWITCH, on=True)
    await switch_on(hass, env, LIGHT_DIMMER, LIGHT_CTL, on=False)
    state_transitions.seen.clear()
    await call(hass, "add_to_room", {"entity_id": light, "room": "Living room"})
    assert hass.states.get(light).attributes["rooms"] == ["WC", "Living room"]
    assert set(hass.states.get(wc).attributes["members"]) == {
        "WC mirror",
        "WC ceiling",
    }
    assert set(hass.states.get(living).attributes["members"]) == {
        "WC mirror",
        "Living room DALI",
    }
    assert (hass.states.get(wc).state, hass.states.get(living).state) == ("on", "on")
    await switch_on(hass, env, LIGHT_SWITCH, on=False)
    assert (hass.states.get(wc).state, hass.states.get(living).state) == (
        "off",
        "off",
    )
    await switch_on(hass, env, LIGHT_SWITCH, on=True)
    await call(
        hass, "remove_from_room", {"entity_id": light, "room": "WC", "force": True}
    )
    assert hass.states.get(light).attributes["rooms"] == ["Living room"]
    assert hass.states.get(wc).attributes["members"] == ["WC ceiling"]
    assert (hass.states.get(wc).state, hass.states.get(living).state) == ("off", "on")
    await switch_on(hass, env, LIGHT_SWITCH, on=False)
    await switch_on(hass, env, LIGHT_SWITCH, on=True)
    assert hass.states.get(wc).state == "off"  # WC no longer hears it
    assert state_transitions.lost() == []
    assert [t for t in state_transitions.seen if t[0] == light] == [
        (light, "on", "off"),
        (light, "off", "on"),
        (light, "on", "off"),
        (light, "off", "on"),
    ]  # the statuses it published only: neither action moved it
    assert all(
        {old, new} <= {"on", "off"}
        for eid, old, new in state_transitions.seen
        if eid.startswith("light.")
    )


async def test_a_deleted_room_takes_its_entities_with_it(
    hass: HomeAssistant, env: Env
) -> None:
    """`delete_room`: the room's central entities leave the registry and the state machine (removed, not
    `unavailable`); its loads no longer list it."""
    kitchen_lights, kitchen_sockets = (
        room_entity(hass, 0xC011),
        room_entity(hass, 0xC011, "sockets"),
    )
    assert kitchen_lights is not None
    assert kitchen_sockets is not None
    await call(hass, "delete_room", {"room": "Kitchen"})
    assert room_entity(hass, 0xC011) is None
    assert room_entity(hass, 0xC011, "sockets") is None
    assert hass.states.get(kitchen_lights) is None
    assert hass.states.get(kitchen_sockets) is None


async def test_scenes_come_and_go_without_a_reload(
    hass: HomeAssistant, env: Env
) -> None:
    """A new scene's entity is added, a deleted one's removed — also one whose registry entry the user removed."""
    hub = env.hub
    await call(hass, "create_scene", {"name": "Movie night"})
    registry = er.async_get(hass)
    movie = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, env.entry.entry_id)
        if e.domain == "scene" and e.original_name == "Movie night"
    )
    assert hass.states.get(movie) is not None
    registry.async_remove(movie)  # the user deleted it
    await hass.async_block_till_done()
    await call(hass, "delete_scene", {"scene": "Movie night"})
    wc_off = entity_id(hass, "scene", f"{MESH_UUID.lower()}-scene-1")
    await call(hass, "delete_scene", {"scene": "WC off"})
    assert registry.async_get(wc_off) is None
    assert hass.states.get(wc_off) is None
    assert env.hub is hub
    assert runs_the_export(env)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_every_entity_follows_in_place(
    hass: HomeAssistant, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """With every entity enabled — the config entities, the diagnostics, the gateway's REST sensors of a gateway
    entry — each one is carried over to the new model (none refuses, nothing reloads) and none loses its state."""
    caplog.set_level(logging.DEBUG, logger=model_update.__name__)
    failing = AsyncMock(side_effect=GatewayError("not in this test"))

    async def fetch_project(_api: JungHomeGatewayApi) -> dict[str, Any]:
        """The gateway holds what Home Assistant uploaded last."""
        return dict(json.loads(ProjectFile.load(env.path).share_json()))

    with (
        patch.object(JungHomeGatewayApi, "config", failing),
        patch.object(JungHomeGatewayApi, "health_status", failing),
        patch.object(JungHomeGatewayApi, "fetch_project", fetch_project),
        patch.object(JungHomeGatewayApi, "upload_project", AsyncMock()),
    ):
        await use_gateway(hass, env)
        await follow_twice(hass, env)
    assert "reloading to follow the export" not in caplog.text


async def follow_twice(hass: HomeAssistant, env: Env) -> None:
    """Two actions in a row on the same hub: the second carries over what the first carried over already."""
    hub = env.hub
    await call(hass, "rename_room", {"room": "WC", "new_name": "Loo"})
    await call(
        hass,
        "set_room",
        {"entity_id": entity_id(hass, "light", UID_LIGHT_CTL), "room": "Kitchen"},
    )
    assert env.hub is hub
    assert runs_the_export(env)


# ----------------------------------------------------------------------------- the fallback


@pytest.mark.unavailable_ok
async def test_a_change_the_hub_cannot_take_over_reloads_the_entry(
    hass: HomeAssistant, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """Refused (here: the hub's own judgement) — the entry reloads as before, and DEBUG says why."""
    caplog.set_level(logging.DEBUG, logger=model_update.__name__)
    hub = env.hub
    with patch.object(JungHomeHub, "model_refusal", return_value="a test refusal"):
        await call(hass, "rename_room", {"room": "WC", "new_name": "Loo"})
    await services_env.settled(hass, env)
    assert "reloading to follow the export: a test refusal" in caplog.text
    assert env.hub is not hub
    assert runs_the_export(env)


@pytest.mark.unavailable_ok
async def test_an_error_while_applying_reloads_the_entry(
    hass: HomeAssistant, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected error half-way through the apply: the reload rebuilds everything, so nothing stays half-done."""
    caplog.set_level(logging.DEBUG, logger=model_update.__name__)
    hub = env.hub
    with patch.object(
        JungHomeHub, "async_apply_model", side_effect=RuntimeError("boom")
    ):
        await call(hass, "rename_room", {"room": "WC", "new_name": "Loo"})
    await services_env.settled(hass, env)
    assert "applying it in place failed (RuntimeError('boom'))" in caplog.text
    assert env.hub is not hub
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    assert hass.states.get(light).attributes["rooms"] == ["Loo"]


async def test_an_entity_that_cannot_take_the_model_over_leaves_the_hub_as_it_was(
    hass: HomeAssistant, env: Env
) -> None:
    """The entities are checked before anything changes: a refusal raises with the hub on its own model again."""
    hub = env.hub
    cdb, devices = hub.cdb, hub.devices
    new = await hass.async_add_executor_job(load_network, str(env.path), None)
    with (
        patch.object(
            JungHomeEntity, "rebind_refusal", return_value="its element moved"
        ),
        pytest.raises(model_update.ApplyRefused, match="its element moved"),
    ):
        model_update._build(hass, hub, *new)
    assert hub.cdb is cdb
    assert hub.devices is devices


@pytest.mark.parametrize(
    ("patches", "reason"),
    [
        ({"mesh": True}, "another mesh"),
        ({"recover": True}, "provisioner identity"),
        ({"address": True}, "has our address"),
    ],
)
async def test_what_the_setup_would_refuse_is_left_to_a_reload(
    hass: HomeAssistant, env: Env, patches: dict[str, bool], reason: str
) -> None:
    hub = env.hub
    cdb, devices = await hass.async_add_executor_job(load_network, str(env.path), None)
    if patches.get("mesh"):
        cdb.mesh_uuid = "00000000-0000-4000-8000-000000000099"
    with (
        patch.object(model_update, "load_network", return_value=(cdb, devices)),
        patch.object(
            type(hub.vault),
            "async_recover",
            AsyncMock(return_value=bool(patches.get("recover"))),
        ),
        patch.object(
            model_update,
            "check_our_address",
            side_effect=ConfigEntryError("in use") if patches.get("address") else None,
        ),
        pytest.raises(model_update.ApplyRefused, match=reason),
    ):
        await model_update._async_follow(hass, env.entry, hub)
    assert hub.cdb is not cdb


async def test_an_entry_set_up_again_meanwhile_is_left_to_its_reload(
    hass: HomeAssistant, env: Env
) -> None:
    """An options change reloads without the service lock: one that came while the export was read wins."""
    hub = env.hub

    async def reloaded(*_args: Any) -> Any:
        env.entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
        return MagicMock()

    with (
        patch.object(model_update, "async_known_mesh", reloaded),
        pytest.raises(model_update.ApplyRefused, match="set up again meanwhile"),
    ):
        await model_update._async_follow(hass, env.entry, hub)
    env.entry.mock_state(hass, ConfigEntryState.LOADED)
    with (
        patch.object(env.entry, "runtime_data", object()),
        patch.object(model_update, "async_known_mesh", AsyncMock()),
        pytest.raises(model_update.ApplyRefused, match="set up again meanwhile"),
    ):
        await model_update._async_follow(hass, env.entry, hub)
    assert env.hub is hub


async def test_an_entry_that_is_not_loaded_is_set_up_again(
    hass: HomeAssistant, env: Env
) -> None:
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        env.entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
        try:
            await model_update.async_follow_export(hass, env.entry.entry_id)
        finally:
            env.entry.mock_state(hass, ConfigEntryState.LOADED)
        await model_update.async_follow_export(hass, "no such entry")
    assert [c.args for c in reload.await_args_list] == [
        (env.entry.entry_id,),
        ("no such entry",),
    ]


def test_the_hub_refuses_other_keys_and_nodes_that_left_or_moved(env: Env) -> None:
    """`model_refusal`: the mesh, its keys and the nodes the client knows stay; a node added is fine."""
    hub = env.hub

    def fresh() -> CDB:
        return CDB.load(env.path)

    assert hub.model_refusal(fresh()) is None
    other = fresh()
    other.mesh_uuid = "00000000-0000-4000-8000-000000000099"
    assert hub.model_refusal(other) == "the export is of another mesh"
    for change in (
        lambda c: c.app_keys.__setitem__(0, AppKeyMaterial.derive(bytes(16))),
        lambda c: c.net_keys.__setitem__(0, NetKeyMaterial.derive(bytes(16))),
    ):
        keys = fresh()
        change(keys)
        assert hub.model_refusal(keys) == "the network or application keys changed"
    gone = fresh()
    left = gone.nodes.pop()
    assert hub.model_refusal(gone) == f"node {left.unicast:04X} left the export"
    moved = fresh()
    moved.nodes[-1].dev_key = bytes(b ^ 0xFF for b in moved.nodes[-1].dev_key)
    assert hub.model_refusal(moved) == (
        f"node {moved.nodes[-1].unicast:04X} changed its address, key or elements"
    )
    fewer = fresh()
    fewer.nodes.pop()
    with patch.object(
        hub, "cdb", fewer
    ):  # the hub knew one node less: the export added one
        assert hub.model_refusal(fresh()) is None


# ----------------------------------------------------------------------------- carrying an entity over


class Probe(Entity):
    """An entity whose constructor binds the model (`members`, attributes, a name) next to run-time state."""

    def __init__(
        self, members: list[str], *, name: str | None = None, mode: int | None = None
    ) -> None:
        self.members = members
        self.learnt: str | None = None
        self._attr_extra_state_attributes = {"members": members}
        if name is not None:
            self._attr_name = name
        if mode is not None:
            self.mode = mode


class OtherProbe(Probe):
    pass


def test_an_entity_takes_the_model_over_and_keeps_what_it_learnt() -> None:
    old = Probe(["a"], name="Old", mode=1)
    built = dict(vars(old))
    old.learnt = "a value read"  # run time
    old.mode = 2  # run time too, and the new constructor no longer sets it
    assert old.extra_state_attributes == {"members": ["a"]}  # cached
    built = model_update._rebind(old, built, Probe(["b"]))
    assert old.members == ["b"]
    assert old.extra_state_attributes == {"members": ["b"]}  # the cache was dropped
    assert old.learnt == "a value read"
    assert old.mode == 2
    assert "__attr_name" not in vars(
        old
    )  # no longer set by the constructor: the class default again
    # the snapshot now holds what the entity took over; a second model is carried over the same way
    model_update._rebind(old, built, Probe(["c"], name="New"))
    assert (old.members, old.name, old.learnt) == (["c"], "New", "a value read")


def test_an_entity_of_another_class_or_element_is_refused() -> None:
    assert (
        model_update._rebind_refusal(Probe([]), OtherProbe([]))
        == "it is a OtherProbe now"
    )
    assert model_update._rebind_refusal(Probe([]), Probe(["x"])) is None
    hub = MagicMock()
    old, moved = (
        JungHomeEntity(hub, 0x0100, "u", None),
        JungHomeEntity(hub, 0x0200, "u", None),
    )
    assert (
        model_update._rebind_refusal(old, moved)
        == "its element moved from 0100 to 0200"
    )
    assert (
        model_update._rebind_refusal(old, JungHomeEntity(hub, 0x0100, "u", None))
        is None
    )
    with patch.object(
        JungHomeEntity,
        "listened",
        new_callable=PropertyMock,
        side_effect=[(0x0101,), ()],  # the new one's, then the running one's
    ):
        assert (
            model_update._rebind_refusal(old, JungHomeEntity(hub, 0x0100, "u", None))
            == "it follows other elements now"
        )


def test_an_entity_without_a_device_leaves_the_registries_alone() -> None:
    registry, entities = MagicMock(), MagicMock()
    model_update._follow_device(MagicMock(), registry, entities, Probe([]), None)
    registry.async_get_or_create.assert_not_called()
    entities.async_update_entity.assert_not_called()


# ----------------------------------------------------------------------------- the hub's own follow-up


async def test_new_loads_are_asked_and_new_nodes_get_heartbeats(env: Env) -> None:
    """Over the link of the moment, as the next link would have done; a lost link ends it quietly."""
    hub = env.hub
    node = hub.cdb.node_by_addr(LIGHT_SWITCH)
    assert node is not None
    with (
        patch.object(hub.liveness, "heartbeats_enabled", True),
        patch.object(hub, "chunked", AsyncMock()) as chunked,
        patch.object(hub.liveness, "configure_heartbeats", AsyncMock()) as configure,
    ):
        await hub._welcome({LIGHT_SWITCH}, [node])
        assert len(chunked.await_args.args[0]) == 1  # the switch's OnOff Get
        configure.assert_awaited_once()
        chunked.side_effect = ConnectionError("gone")
        await hub._welcome({LIGHT_SWITCH}, [node])
        configure.assert_awaited_once()


async def test_scenes_are_read_again_on_the_next_link_when_none_is_up(env: Env) -> None:
    hub = env.hub
    hub.refresh.connect_steps_done["scene actions"] = hub.refresh.connect_steps_done[
        "current scenes"
    ] = 1.0
    with (
        patch.object(
            JungHomeHub, "connected", new_callable=PropertyMock, return_value=False
        ),
        patch.object(hub.entry, "async_create_background_task") as task,
    ):
        hub._reread_scenes()
    task.assert_not_called()
    assert "scene actions" not in hub.refresh.connect_steps_done
    assert "current scenes" not in hub.refresh.connect_steps_done


async def test_the_transitions_recorder_sees_a_lost_state(
    hass: HomeAssistant, env: Env, state_transitions: StateTransitions
) -> None:
    """The check every services test runs would catch a reload: it sees the states go."""
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    hass.states.async_set(light, "on")
    hass.states.async_set(light, "unavailable")
    hass.states.async_set(light, "unknown")
    assert (light, "on", "unavailable") in state_transitions.lost()
    assert (light, "unavailable", "unknown") not in state_transitions.lost()
    state_transitions.seen.clear()


async def test_a_scene_stored_again_shows_what_it_does_now(
    hass: HomeAssistant, env: Env
) -> None:
    """Storing a scene a load is a member of already leaves the export's members as they were, but not what the
    load does in it: the members are asked again (a reload asked them on its first link)."""
    st = env.hub.element_state(LIGHT_CTL)
    st.on, st.lightness, st.kelvin = True, 0xFFFF, 2700
    ctl = entity_id(hass, "light", UID_LIGHT_CTL)

    def shown() -> Any:
        return (
            hass.states.get("scene.all_off")
            .attributes["members"]
            .get("Living room DALI")
        )

    await call(hass, "store_scene", {"entity_id": ctl, "scene": "All off"})
    await wait_until(hass, lambda: shown() == "lightness 100% 2700K")
    members = CDB.load(env.path).scenes[2]
    st.lightness = 0x8000
    await call(hass, "store_scene", {"entity_id": ctl, "scene": "All off"})
    assert CDB.load(env.path).scenes[2] == members
    await wait_until(hass, lambda: shown() == "lightness 50% 2700K")
