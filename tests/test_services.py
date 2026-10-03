"""Service actions: schema validation, id resolution through the registries, a happy path per service, the
error translations, and the reload of the config entry after a change.

The config entry points at a temporary copy of the Android share export; the mesh side is the hub's real
`ProxyClient` over the fake proxy link (review-3 Q2): Config requests go through it to the nodes' Configuration
Servers (`FakeProxyLink.config_reply`), AppKey requests to the load elements' servers (`FakeProxyLink.app_reply`),
so the transport's matching, status checks and link state apply to every request.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.junghome_ble import mesh_config, repairs
from custom_components.junghome_ble import services as svc
from custom_components.junghome_ble.climate import temperature_to_level
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_GATEWAY_TOKEN,
    CONF_SOURCE,
    CONF_UNICAST,
    DOMAIN,
    EVENT_PLAN,
    ISSUE_DEVICE_NAME,
    ISSUE_GATEWAY_SYNC,
    PIN_FROM_MESH,
)
from custom_components.junghome_ble.coordinator import JungHomeHub, issue_id
from custom_components.junghome_ble.cover import closedness_to_level
from custom_components.junghome_ble.gateway_api import (
    GatewayError,
    GatewayUnreachable,
    JungHomeGatewayApi,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.client import ProxyClient
from custom_components.junghome_ble.jhmesh.export import (
    KEY_MODE_MOVE,
    ModelChange,
    ProjectFile,
)
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode, encode_opcode

from .conftest import (
    FakeProxyLink,
    StateTransitions,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_OUT2,
    LIGHT_SWITCH,
    NODE_ACTUATOR,
    NODE_GATEWAY,
    NODE_LIGHT_CTL,
    NODE_LIGHT_SWITCH,
    NODE_MOTION,
    NODE_PRESENCE,
    NODE_SOCKET,
    NODE_THERMOSTAT,
    ROCKER_A,
    ROCKER_B,
    SOCKET,
    UID_BUTTON_WC,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_LIGHT_OUT2,
    UID_LIGHT_SWITCH,
    UID_ROCKER_A,
    UID_SOCKET,
    entity_id,
)
from .test_logbook import describers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

FIXTURES = Path(__file__).parent / "fixtures"
ANDROID_PATH = FIXTURES / "JungHome-android.json"
BLINDS_PATH = FIXTURES / "Blinds.json"
OUR = 0x0D00
DALI_NODE, DIMMER_GROUP, DALI_GROUP, ROCKER_A_GROUP = 0x0232, 0xC070, 0xC044, 0xC04F
GATEWAY_NODE, GATEWAY_GROUP = 0x00DC, 0xC005
WC, LIVING = 0xC00F, 0xC010
NODE_GATEWAY_ID = f"node:{NODE_GATEWAY}"
NODE_ACTUATOR_ID = f"node:{NODE_ACTUATOR}"
BUTTONS_DALI = f"{NODE_LIGHT_CTL}-0040-buttons"
BUTTONS_WC = f"{NODE_LIGHT_SWITCH}-0040-buttons"
REPLY_TIMEOUT = 0.05  # real seconds `ProxyClient.request` waits per attempt when the caller names no timeout
THRESHOLD_PIDS = (
    b"\x04\x50",
    b"\x05\x50",
)  # 0x5004 / 0x5005, as the Get / Set carries them
STATUS_FOR = {
    C.CONFIG_MODEL_SUBSCRIPTION_ADD: C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
    C.CONFIG_MODEL_SUBSCRIPTION_DELETE: C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
    C.CONFIG_MODEL_PUBLICATION_SET: C.CONFIG_MODEL_PUBLICATION_STATUS,
    C.CONFIG_MODEL_APP_BIND: C.CONFIG_MODEL_APP_STATUS,
}


@dataclass
class Env:
    entry: MockConfigEntry
    path: Path
    link: FakeProxyLink
    config_calls: list[tuple[int, bytes]] = field(default_factory=list)
    refuse: dict[bytes, int] = field(default_factory=dict)
    silent: set[bytes] = field(default_factory=set)  # Config requests nobody answers
    modes: dict[int, bytes] = field(default_factory=dict)
    hubs: list[Any] = field(default_factory=list)
    # every state change since the setup began (`no_entity_loses_its_state`)
    transitions: StateTransitions = field(default_factory=StateTransitions)
    # the load elements' scene registers and JUNG scene action records (element -> scenes; (element, scene) -> bytes)
    registers: dict[int, list[int]] = field(default_factory=lambda: {0x0148: [1]})
    scene_actions: dict[tuple[int, int], bytes] = field(default_factory=dict)
    app_calls: list[tuple[int, bytes]] = field(default_factory=list)
    # elements that answer a Generic OnOff Get (element -> on); the answer reaches the hub like a real reply
    onoff: dict[int, bool] = field(default_factory=dict)
    # the metering socket's thresholds ((element, property) -> value); `threshold_sets` False: Sets are not taken
    thresholds: dict[tuple[int, int], bytes] = field(default_factory=dict)
    threshold_sets: bool = True
    # state Gets a load answers ((element, Get opcode) -> Status params, one per Get, the last one repeated)
    state_replies: dict[tuple[int, int], list[bytes]] = field(default_factory=dict)

    @property
    def hub(self) -> Any:
        return self.entry.runtime_data

    def reload(self) -> ProjectFile:
        return ProjectFile.load(self.path)


# What the env's elements answer for a value other settings follow (`config_entities.VALUE_GATES`) instead of the
# catch-all 0x06: a detector out of day mode, so no setting turns unavailable when the answer comes in mid-test.
GATE_VALUES = {(0x6015).to_bytes(2, "little"): b"\x00"}


def onoff_status(*, on: bool) -> bytes:
    """A Generic OnOff Status: what an element in `env.onoff` answers a Get with."""
    return encode_opcode(M.GEN_ONOFF_STATUS) + bytes([on])


STATE_STATUS = {
    M.LIGHT_LIGHTNESS_GET: M.LIGHT_LIGHTNESS_STATUS,
    M.LIGHT_CTL_GET: M.LIGHT_CTL_STATUS,
    M.GEN_LEVEL_GET: M.GEN_LEVEL_STATUS,
    M.GEN_ONOFF_GET: M.GEN_ONOFF_STATUS,
}


def state_status(env: Env, dst: int, op: int) -> bytes:
    """Answer a state Get from `env.state_replies` (`env.onoff` for an OnOff Get)."""
    if (dst, op) not in env.state_replies:
        return onoff_status(on=env.onoff[dst])
    queue = env.state_replies[dst, op]
    params = queue.pop(0) if len(queue) > 1 else queue[0]
    return encode_opcode(STATE_STATUS[op]) + params


def threshold_status(env: Env, dst: int, op: int, p: bytes) -> bytes:
    """Answer a threshold Get / Set (LBC Admin) from `env.thresholds`."""
    pid = int.from_bytes(p[:2], "little")
    if op == 0x03 and env.threshold_sets:
        env.thresholds[dst, pid] = p[3:]
    return (
        encode_opcode(0x05, M.JUNG_CID)
        + p[:2]
        + b"\x03"
        + env.thresholds.get((dst, pid), b"")
    )


def serve_config(env: Env, link: FakeProxyLink, export: Path) -> None:
    """Answer Config requests through the fake link (review-3 Q2): the real transport, matching and status checks.

    The nodes' Configuration Servers echo the request's fields after a status code (`env.refuse` names a code for
    a request); `env.silent` requests get no answer. The fake mesh opens device-key traffic with its own nodes'
    keys, so those of another export are added to it.
    """

    def config_server(node: int, pdu: bytes) -> bytes | None:
        env.config_calls.append((node, pdu))
        if pdu in env.silent:
            return None
        op, _cid, params = decode_opcode(pdu)
        return encode_opcode(STATUS_FOR[op]) + bytes([env.refuse.get(pdu, 0)]) + params

    link.config_reply = config_server
    for node in CDB.load(export).nodes:
        if link.cdb.node_by_addr(node.unicast) is None:
            link.cdb.nodes.append(node)


@pytest.fixture
def export_source() -> Path:
    """The fixture export the `env` entry starts from; a test parametrizes it to use another one."""
    return ANDROID_PATH


@pytest.fixture
async def env(
    hass: HomeAssistant,
    tmp_path: Path,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    export_source: Path,
    state_transitions: StateTransitions,
) -> AsyncGenerator[Env]:
    path = tmp_path / "JungHome.json"
    shutil.copy(export_source, path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={CONF_CDB_PATH: str(path), CONF_UNICAST: "0D00"},
    )
    env = Env(entry, path, fake_link, transitions=state_transitions)

    serve_config(env, fake_link, path)

    def register_status(dst: int) -> bytes:
        scenes = env.registers.get(dst, [])
        return (
            encode_opcode(M.SCENE_REGISTER_STATUS)
            + b"\x00"
            + (scenes[0] if scenes else 0).to_bytes(2, "little")
            + b"".join(n.to_bytes(2, "little") for n in scenes)
        )

    def app_server(dst: int, pdu: bytes) -> bytes | None:  # noqa: PLR0911  # one branch per message
        """The load elements' AppKey servers, answering over the fake link (None: this element stays silent)."""
        op, cid, p = decode_opcode(pdu)
        env.app_calls.append((dst, pdu))
        if cid == M.JUNG_CID and op in (0x02, 0x03) and p[:2] in THRESHOLD_PIDS:
            return threshold_status(env, dst, op, p)
        if cid is None and (
            (dst, op) in env.state_replies
            or (op == M.GEN_ONOFF_GET and dst in env.onoff)
        ):
            return state_status(env, dst, op)
        if cid == M.JUNG_CID and op in (0x02, 0x03):
            if op == 0x03:
                env.modes[dst] = p[3:]
            value = env.modes.get(dst, GATE_VALUES.get(p[:2], b"\x06"))
            return encode_opcode(0x05, M.JUNG_CID) + p[:2] + b"\x03" + value
        if cid is None and op in (M.SCENE_STORE, M.SCENE_DELETE):
            scene = int.from_bytes(p[:2], "little")
            scenes = env.registers.setdefault(dst, [])
            if op == M.SCENE_STORE and scene not in scenes:
                scenes.append(scene)
            if op == M.SCENE_DELETE and scene in scenes:
                scenes.remove(scene)
            return register_status(dst)
        if cid is None and op == M.SCENE_REGISTER_GET:
            return register_status(dst)
        if cid == M.JUNG_CID and op in (
            V.SCENE_ACTION_SETUP_SET,
            V.SCENE_ACTION_SETUP_GET,
        ):
            scene = int.from_bytes(p[:2], "little")
            if op == V.SCENE_ACTION_SETUP_SET:
                if len(p) > 2:
                    env.scene_actions[(dst, scene)] = p[2:]
                else:
                    env.scene_actions.pop((dst, scene), None)
            if scene == 0:  # the list
                listed = sorted(n for (el, n) in env.scene_actions if el == dst)
                body = (
                    b"\x00\x00"
                    + b"".join(n.to_bytes(2, "little") for n in listed)
                    + b"\x00\x00"
                )
            else:
                body = p[:2] + env.scene_actions.get((dst, scene), b"")
            return encode_opcode(V.SCENE_ACTION_SETUP_STATUS, M.JUNG_CID) + body
        return None  # the state refresh after (re)connecting goes unanswered

    fake_link.app_reply = app_server
    # a silent load is the rule here (the refresh after every reload): each unanswered attempt costs milliseconds
    timeout, *rest = ProxyClient.request.__defaults__ or ()
    assert timeout == 3.0
    with (
        patch.object(ProxyClient.request, "__defaults__", (REPLY_TIMEOUT, *rest)),
        # ... and marks none of them unreachable, which would refuse every plan to it (review-4 W I5)
        patch.object(JungHomeHub, "_missed_answer", lambda *_args, **_kwargs: None),
    ):
        await setup_entry(hass, entry)
        await wait_for_link(hass, entry)
        await settle(hass)
        # the unanswered refresh through: a Get of it still out would take the status a load publishes for a Set
        # the test sends (the oldest waiter a status fits), and that Set would go out again
        hub = entry.runtime_data
        await wait_until(
            hass,
            lambda: hub._refresh_task is None or hub._refresh_task.done(),
            what="the connect-time refresh",
        )
        env.hubs.append(hub)
        yield env


@pytest.fixture(autouse=True)
def no_entity_loses_its_state(
    request: pytest.FixtureRequest, state_transitions: StateTransitions
) -> Generator[None]:
    """Review-4 D23: an action has the hub follow the export in place — no entity passes through `unavailable` or
    `unknown`, as a reload made every one of them do, and the link the hub came up with is still the one it has.
    Every test on the `env` entry, where the actions run; one that reloads or unloads the entry, or drops the
    link, on purpose says so (`unavailable_ok`)."""
    yield
    if "env" in request.fixturenames and "unavailable_ok" not in request.keywords:
        assert state_transitions.lost() == []
        hass: HomeAssistant = request.getfixturevalue("hass")
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            assert entry.runtime_data.link_count == 1


async def call(hass: HomeAssistant, service: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


async def settled(hass: HomeAssistant, env: Env) -> None:
    """Let the reloaded entry attach to the fake link again."""
    await wait_for_link(hass, env.entry)
    await settle(hass)


async def refreshed(hass: HomeAssistant, env: Env) -> None:
    """Wait for the hub's connect-time refresh to finish (the scene members' actions are read last).

    Its Gets to silent loads time out on the real clock (`REPLY_TIMEOUT` each), so an idle loop does not mean
    it is through.
    """
    await wait_until(
        hass,
        lambda: (task := env.hub._refresh_task) is None or task.done(),
        what="the connect-time refresh",
    )


def runs_the_export(env: Env) -> bool:
    """Whether the running hub's device model is the export on disk: it followed the last change (review-4 D23)."""
    return bool(env.hub.cdb.raw == CDB.load(env.path).raw)


# the response of a rewiring call that sent nothing and wrote nothing (`MeshConfigurator.plan_response`)
NO_PLAN = {"applied": 0, "total": 0, "recorded": False, "nodes": []}


def device_id(hass: HomeAssistant, identifier: str) -> str:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )
    assert device is not None, identifier
    return device.id


def subs(pf: ProjectFile, element: int, model: str) -> list[int]:
    el = pf.cdb.element(element)
    assert el is not None
    return el.subscriptions(model)


# ----------------------------------------------------------------------------- registration


@pytest.mark.unavailable_ok
async def test_services_are_registered_once_and_survive_entry_unload(
    hass: HomeAssistant, env: Env
) -> None:
    """Services are registered in async_setup (once per HA run); entries only add/remove their configurator."""
    names = {
        "set_room",
        "create_room",
        "rename_room",
        "delete_room",
        "assign_key",
        "clear_key",
    }
    assert names <= set(hass.services.async_services_for_domain(DOMAIN))
    svc.async_setup_services(hass)  # idempotent
    other = MockConfigEntry(domain=DOMAIN, unique_id="other", data={})
    other.add_to_hass(hass)
    other.runtime_data = env.hub
    svc.async_register_configurator(hass, other)
    assert set(hass.data[svc.CONFIGURATORS]) == {env.entry.entry_id, other.entry_id}
    svc.async_unregister_configurator(hass, other)
    assert await hass.config_entries.async_unload(env.entry.entry_id)
    await hass.async_block_till_done()
    # still registered, and a call now fails with the translated "not loaded" error
    assert names <= set(hass.services.async_services_for_domain(DOMAIN))
    assert hass.data[svc.CONFIGURATORS] == {}
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "create_room", {"name": "Attic"}, blocking=True
        )
    assert err.value.translation_key == "service_entry_not_loaded"


# ----------------------------------------------------------------------------- schema validation


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("set_room", {}),
        ("set_room", {"room": "WC"}),
        ("set_room", {"device_id": "x"}),
        ("create_room", {}),
        ("rename_room", {"room": "WC"}),
        ("delete_room", {}),
        ("assign_key", {"room": "WC"}),
        ("assign_key", {"key_entity": "event.a"}),
        ("assign_key", {"key_entity": "event.a", "key_device": "d", "room": "WC"}),
        (
            "assign_key",
            {"key_entity": "event.a", "room": "WC", "target_entity": "light.a"},
        ),
        ("assign_key", {"key_entity": "event.a", "room": "WC", "mode": "disco"}),
        ("assign_key", {"key_entity": "not an entity id", "room": "WC"}),
        ("assign_key", {"key_device": "d", "key": "E", "room": "WC"}),
        ("assign_key", {"key_entity": "event.a", "room": "WC", "scene": "1"}),
        ("clear_key", {}),
        ("clear_key", {"key_entity": "event.a", "key_device": "d"}),
    ],
)
async def test_schema_rejects_bad_calls(
    hass: HomeAssistant, env: Env, service: str, data: dict[str, Any]
) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, service, data)
    assert env.config_calls == []


# ----------------------------------------------------------------------------- assign_key / clear_key


async def test_assign_key_by_event_entity_to_a_light_entity(
    hass: HomeAssistant, env: Env
) -> None:
    key = entity_id(hass, "event", UID_ROCKER_A)
    light = entity_id(hass, "light", UID_LIGHT_DIMMER)
    old_hub = env.hub
    await call(hass, "assign_key", {"key_entity": key, "target_entity": light})
    await settled(hass, env)
    # the new wiring first, the old subscriptions last (the old publications are superseded, not cleared)
    assert env.config_calls == [
        (DALI_NODE, C.model_publication_set(ROCKER_A, DIMMER_GROUP, "1001")),
        (DALI_NODE, C.model_subscription_add(ROCKER_A, DIMMER_GROUP, "1001")),
        (DALI_NODE, C.model_publication_set(ROCKER_A, DIMMER_GROUP, "1003")),
        (DALI_NODE, C.model_subscription_add(ROCKER_A, DIMMER_GROUP, "1003")),
        (DALI_NODE, C.model_publication_set(ROCKER_A, DIMMER_GROUP, "05271015")),
        (DALI_NODE, C.model_subscription_add(ROCKER_A, DIMMER_GROUP, "05271015")),
        (DALI_NODE, C.model_subscription_delete(ROCKER_A, DALI_GROUP, "1001")),
        (DALI_NODE, C.model_subscription_delete(ROCKER_A, DALI_GROUP, "05271015")),
    ]
    # the unacknowledged KeySetPropertyMode resets, then the acknowledged KeyMode Set, over the real link (AppKey)
    assert [pdu for _s, d, pdu in env.link.sent if d == ROCKER_A] == [
        M.vendor_property_set("admin", 0x5006, b"\x00\x00\x00", ack=False),
        M.vendor_property_set("admin", 0x5007, b"", ack=False),
        M.vendor_property_set("admin", 0x5008, b"", ack=False),
        M.vendor_property_set("admin", 0x5003, b"\x00"),
    ]
    assert env.modes == {ROCKER_A: b"\x00"}
    assert env.reload().publication(ROCKER_A, "1001") == DIMMER_GROUP
    # the hub took the new export over in place (review-4 D23): the same hub, the link never dropped
    assert env.entry.state is ConfigEntryState.LOADED
    assert env.hub is old_hub
    assert runs_the_export(env)
    assert env.hub.connected
    assert env.hub.link_count == 1
    assert hass.states.get(light) is not None


async def test_assign_key_to_a_scene(hass: HomeAssistant, env: Env) -> None:
    """A scene by name: the Scene Client publishes to all nodes, the key gets the scene and key mode 2 (F15)."""
    key = entity_id(hass, "event", UID_ROCKER_A)
    await call(hass, "assign_key", {"key_entity": key, "scene": "All off"})
    await settled(hass, env)
    assert (
        DALI_NODE,
        C.model_publication_set(ROCKER_A, 0xFFFF, "1205"),
    ) in env.config_calls
    scene_config = (2).to_bytes(2, "little") + bytes(4)
    assert (
        ROCKER_A,
        M.vendor_property_set("admin", 0x5002, scene_config),
    ) in env.app_calls
    assert env.modes == {ROCKER_A: b"\x02"}
    rows = env.reload().meta["keyModeSceneConfigExports"]
    assert (rows[-1]["elementAddress"], rows[-1]["sceneConfig"]["sceneId"]) == (
        ROCKER_A,
        2,
    )
    # after the reload the key's event entity says what it recalls
    attrs = hass.states.get(key).attributes
    assert (attrs["connection"], attrs["connection_scene"]) == ("scene", 2)


async def test_assign_key_by_device_and_letter_to_a_room_and_the_gateway(
    hass: HomeAssistant, env: Env
) -> None:
    buttons = device_id(hass, BUTTONS_DALI)
    await call(
        hass,
        "assign_key",
        {"key_device": buttons, "key": "b", "room": "WC", "mode": "light_and_switch"},
    )
    await settled(hass, env)
    assert env.modes == {ROCKER_B: b"\x00"}
    pf = env.reload()
    assert pf.publication(ROCKER_B, "1001") == 0xC050
    assert subs(pf, LIGHT_SWITCH, "1000")[-1] == 0xC050
    rows = [
        r
        for d in pf.meta["devices"]
        for r in d.get("cachedGroupConnectionMetadata") or []
        if r["elementAddress"] == ROCKER_B
    ]
    assert rows == [
        {
            "elementAddress": ROCKER_B,
            "groupAddress": WC,
            "publishAddress": 0xC050,
            "function": "LIGHT_AND_SWITCH",
        }
    ]

    env.config_calls.clear()
    await call(
        hass,
        "assign_key",
        {
            "key_device": buttons,
            "key": "A",
            "target_device": device_id(hass, NODE_GATEWAY_ID),
        },
    )
    await settled(hass, env)
    assert env.modes[ROCKER_A] == b"\x06"
    assert (
        DALI_NODE,
        C.model_publication_set(ROCKER_A, GATEWAY_GROUP, "05271015"),
    ) in env.config_calls


async def test_assign_key_to_a_target_device_and_a_single_key_device(
    hass: HomeAssistant, env: Env
) -> None:
    """The WC button device has one key: no letter needed; a load device is a valid target."""
    await call(
        hass,
        "assign_key",
        {
            "key_device": device_id(hass, BUTTONS_WC),
            "target_device": device_id(hass, UID_LIGHT_CTL),
            "mode": "light",
        },
    )
    await settled(hass, env)
    assert env.modes == {0x0149: b"\x00"}
    assert env.reload().publication(0x0149, "1003") == DALI_GROUP


@pytest.mark.parametrize("export_source", [FIXTURES / "MeshNetwork-rtr.json"])
async def test_assign_key_to_a_thermostat_entity(hass: HomeAssistant, env: Env) -> None:
    """A room thermostat is a key target: key mode 4 (*temperature*) on its set-point (review-4 F4-16)."""
    rtr = env.hub.devices.thermostats[0]
    climate = entity_id(hass, "climate", rtr.unique_id)
    key = entity_id(hass, "event", f"{NODE_LIGHT_SWITCH}-0040")
    await call(hass, "assign_key", {"key_entity": key, "target_entity": climate})
    await settled(hass, env)
    assert env.modes == {0x0149: b"\x04"}
    assert env.reload().publication(0x0149, "1003") == 0xC090


@pytest.mark.parametrize("export_source", [FIXTURES / "MeshNetwork-detectors.json"])
async def test_assign_key_from_a_detector(hass: HomeAssistant, env: Env) -> None:
    """A detector is a key source, named by one of its entities or its device: it drives one device and takes no
    key mode (review-4 F4-16)."""
    motion = env.hub.devices.detectors[0]
    relay = entity_id(hass, "light", f"{NODE_PRESENCE}-0001")
    sensor = entity_id(hass, "binary_sensor", f"{motion.unique_id}-motion")
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "assign_key",
            {"key_device": device_id(hass, f"node:{NODE_MOTION}"), "room": "WC"},
        )
    assert exc.value.translation_key == "service_detector_device_only"
    await call(hass, "assign_key", {"key_entity": sensor, "target_entity": relay})
    await settled(hass, env)
    assert env.modes == {}
    assert env.reload().publication(motion.address, "1001") == 0xC0A2


async def test_clear_key(hass: HomeAssistant, env: Env) -> None:
    await call(
        hass, "clear_key", {"key_entity": entity_id(hass, "event", UID_ROCKER_A)}
    )
    await settled(hass, env)
    assert env.config_calls == [
        (DALI_NODE, C.model_publication_set(ROCKER_A, 0x0000, "1001")),
        (DALI_NODE, C.model_subscription_delete(ROCKER_A, DALI_GROUP, "1001")),
        (DALI_NODE, C.model_publication_set(ROCKER_A, 0x0000, "05271015")),
        (DALI_NODE, C.model_subscription_delete(ROCKER_A, DALI_GROUP, "05271015")),
    ]
    assert env.modes == {}
    assert env.reload().publication(ROCKER_A, "1001") is None


ROCKER_GANG = "Push-button 2-gang 0232 buttons"  # the Android export names no rocker gang: node label + "buttons"
E_ROCKER_A, E_BUTTON_WC, E_LIGHT_SWITCH = (
    ("event", UID_ROCKER_A),
    ("event", UID_BUTTON_WC),
    ("light", UID_LIGHT_SWITCH),
)


@pytest.mark.parametrize(
    ("data", "key", "placeholders"),
    [
        (
            {"key_entity": "event.nope", "room": "WC"},
            "service_unknown_device",
            {"id": "event.nope"},
        ),
        (
            {"key_entity": E_LIGHT_SWITCH, "room": "WC"},
            "service_not_a_key",
            {"name": E_LIGHT_SWITCH},
        ),
        (
            {"key_device": "nope", "room": "WC"},
            "service_unknown_device",
            {"id": "nope"},
        ),
        (
            {"key_device": UID_LIGHT_SWITCH, "room": "WC"},
            "service_not_a_key",
            {"name": "WC mirror"},
        ),
        (
            {"key_device": BUTTONS_DALI, "room": "WC"},
            "service_key_required",
            {"name": ROCKER_GANG, "keys": "A, B"},
        ),
        (
            {"key_device": BUTTONS_DALI, "key": "C", "room": "WC"},
            "service_unknown_key",
            {"name": ROCKER_GANG, "letter": "C", "keys": "A, B"},
        ),
        (
            {"key_entity": E_ROCKER_A, "target_entity": E_BUTTON_WC},
            "service_not_a_load",
            {"name": E_BUTTON_WC},
        ),
        (
            {"key_entity": E_ROCKER_A, "target_entity": "light.nope"},
            "service_unknown_device",
            {"id": "light.nope"},
        ),
        (
            {"key_entity": E_ROCKER_A, "target_device": NODE_ACTUATOR_ID},
            "service_not_a_load",
            {"name": "2-channel actuator 0400"},
        ),
        (
            {"key_entity": E_ROCKER_A, "target_device": BUTTONS_WC},
            "service_not_a_load",
            {"name": "WC mirror button"},
        ),
        (
            {"key_entity": E_ROCKER_A, "room": "Attic"},
            "service_no_room",
            {"room": "Attic"},
        ),
        (
            {"key_entity": E_ROCKER_A, "room": "WC", "mode": "gateway"},
            "service_invalid_mode",
            {"mode": "gateway", "target": "room"},
        ),
    ],
)
async def test_assign_key_resolution_errors(
    hass: HomeAssistant,
    env: Env,
    data: dict[str, Any],
    key: str,
    placeholders: dict[str, Any],
) -> None:
    """`(domain, unique id)` tuples stand for entity ids, identifiers for device ids; both resolve at run time."""

    def resolve(value: Any, kind: str) -> Any:
        if isinstance(value, tuple):
            return entity_id(hass, *value)
        if kind == "device" and value != "nope":
            return device_id(hass, value)
        return value

    data = {
        k: resolve(v, "device" if k.endswith("_device") else "entity")
        for k, v in data.items()
    }
    placeholders = {k: resolve(v, "text") for k, v in placeholders.items()}
    hub = env.hub
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "assign_key", data)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == key
    assert exc.value.translation_placeholders == placeholders
    assert env.config_calls == []
    assert env.hub is hub  # no reload without a change


async def test_assign_key_across_networks_is_refused(
    hass: HomeAssistant, env: Env
) -> None:
    """A target entity registered to another config entry of ours."""
    other = MockConfigEntry(domain=DOMAIN, unique_id="other", data={})
    other.add_to_hass(hass)
    other.mock_state(hass, ConfigEntryState.LOADED)
    other.runtime_data = env.hub
    registry = er.async_get(hass)
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    registry.async_update_entity(light, config_entry_id=other.entry_id)
    try:
        with pytest.raises(ServiceValidationError) as exc:
            await call(
                hass,
                "assign_key",
                {
                    "key_entity": entity_id(hass, "event", UID_ROCKER_A),
                    "target_entity": light,
                },
            )
        assert exc.value.translation_key == "service_target_other_network"
    finally:
        registry.async_update_entity(light, config_entry_id=env.entry.entry_id)
        other.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_a_refused_config_status_is_a_translated_error_and_a_reload(
    hass: HomeAssistant, env: Env
) -> None:
    """The plan stops at the refusal; what was applied before it is recorded (apply-and-record), and the hub's
    device model follows the recorded export (CFG-15), in place (review-4 D23)."""
    refused = C.model_subscription_add(ROCKER_A, DIMMER_GROUP, "1003")
    env.refuse[refused] = 0x08
    hub = env.hub
    with pytest.raises(HomeAssistantError) as exc:
        await call(
            hass,
            "assign_key",
            {
                "key_entity": entity_id(hass, "event", UID_ROCKER_A),
                "target_entity": entity_id(hass, "light", UID_LIGHT_DIMMER),
            },
        )
    assert exc.value.translation_key == "service_config_refused"
    assert exc.value.translation_placeholders["status"] == "Not a Subscribe Model"
    assert exc.value.translation_placeholders["applied"] == mesh_config.applied_text(
        3, 8
    )
    assert env.config_calls[-1] == (DALI_NODE, refused)
    await settled(hass, env)
    assert env.hub is hub
    assert runs_the_export(env)
    assert env.modes == {}
    pf = env.reload()
    assert pf.publication(ROCKER_A, "1001") == DIMMER_GROUP  # accepted: recorded
    assert subs(pf, ROCKER_A, "1003") == []  # refused: not recorded
    assert subs(pf, ROCKER_A, "1001") == [DALI_GROUP, DIMMER_GROUP]  # not cleared yet


async def test_a_late_duplicate_of_the_previous_status_does_not_answer_the_next_step(
    hass: HomeAssistant,
    env: Env,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-3 Q2, through the real transport: the node answers the first Publication Set, then — instead of the
    next Publication Set's answer — only a late duplicate of that first status arrives. Same node, same opcode, same
    device key; only the echoed model tells it apart, and it must not pass for the answer (the plan would record a
    publication the node never took)."""
    monkeypatch.setattr(mesh_config, "CONFIG_TIMEOUT", 0.05)
    first = C.model_publication_set(ROCKER_A, DIMMER_GROUP, "1001")
    silent = C.model_publication_set(ROCKER_A, DIMMER_GROUP, "1003")
    env.silent.add(silent)
    answer = fake_link.config_reply
    assert answer is not None

    def with_a_stale_duplicate(node: int, pdu: bytes) -> bytes | None:
        reply = answer(node, pdu)
        if pdu == silent:  # the node repeats its answer to the first Set instead
            stale = answer(node, first)
            assert stale is not None
            fake_link.inject_config(node, OUR, stale)
        return reply

    fake_link.config_reply = with_a_stale_duplicate
    with pytest.raises(HomeAssistantError) as exc:
        await call(
            hass,
            "assign_key",
            {
                "key_entity": entity_id(hass, "event", UID_ROCKER_A),
                "target_entity": entity_id(hass, "light", UID_LIGHT_DIMMER),
            },
        )
    assert exc.value.translation_key == "service_no_reply"
    await settled(hass, env)
    pf = env.reload()
    assert pf.publication(ROCKER_A, "1001") == DIMMER_GROUP  # answered: recorded
    assert (
        pf.publication(ROCKER_A, "1003") != DIMMER_GROUP
    )  # only the stale status came: not recorded


# ----------------------------------------------------------------------------- set_room


async def test_set_room_by_device_and_entity_targets(
    hass: HomeAssistant, env: Env
) -> None:
    ctl = device_id(hass, UID_LIGHT_CTL)
    socket = entity_id(hass, "switch", UID_SOCKET)
    await call(hass, "set_room", {"device_id": ctl, "entity_id": socket, "room": "WC"})
    await settled(hass, env)
    pf = env.reload()
    assert WC in subs(pf, LIGHT_CTL, "1000")
    assert LIVING not in subs(pf, LIGHT_CTL, "1000")
    assert WC in subs(pf, SOCKET, "1000")
    assert env.config_calls[:2] == [
        (DALI_NODE, C.model_subscription_add(LIGHT_CTL, WC, "1000")),
        (DALI_NODE, C.model_subscription_add(LIGHT_CTL, WC, "1002")),
    ]
    assert (DALI_NODE, C.model_subscription_delete(LIGHT_CTL, LIVING, "1000")) in (
        env.config_calls
    )
    # both loads in one plan: one export rewrite, one `.bak` (the state before the call)
    assert ProjectFile.load(env.path.with_name(env.path.name + ".bak")).cdb.groups == (
        ProjectFile.load(ANDROID_PATH).cdb.groups
    )
    assert LIVING in subs(
        ProjectFile.load(env.path.with_name(env.path.name + ".bak")), LIGHT_CTL, "1000"
    )
    # the device keeps the area it already had; the entity's rooms attribute follows the reload
    registry = dr.async_get(hass)
    living = ar.async_get(hass).async_get_area_by_name("Living room")
    assert living is not None
    device = registry.async_get(ctl)
    assert device is not None
    assert device.area_id == living.id
    state = hass.states.get(entity_id(hass, "light", UID_LIGHT_CTL))
    assert state is not None
    assert state.attributes["rooms"] == ["WC"]


async def test_set_room_keeps_an_existing_area_and_expands_areas(
    hass: HomeAssistant, env: Env
) -> None:
    registry = dr.async_get(hass)
    areas = ar.async_get(hass)
    bathroom = areas.async_get_or_create("Bathroom")
    dimmer = device_id(hass, UID_LIGHT_DIMMER)
    registry.async_update_device(dimmer, area_id=bathroom.id)
    # a foreign device in the same area is skipped, ours is picked up
    foreign = registry.async_get_or_create(
        config_entry_id=env.entry.entry_id,
        identifiers={("other", "x")},
        name="Not ours",
    )
    registry.async_update_device(foreign.id, area_id=bathroom.id)
    await call(hass, "set_room", {"area_id": bathroom.id, "room": "Kitchen"})
    await settled(hass, env)
    pf = env.reload()
    assert subs(pf, LIGHT_DIMMER, "1000")[-1] == 0xC011
    device = registry.async_get(dimmer)
    assert device is not None
    assert device.area_id == bathroom.id


async def test_set_room_creates_the_room_and_places_an_area_less_device(
    hass: HomeAssistant, env: Env
) -> None:
    """With `create` only (review-4 W4-12): without it, an unknown room is refused before anything is sent."""
    registry = dr.async_get(hass)
    switch = device_id(hass, UID_LIGHT_SWITCH)
    registry.async_update_device(switch, area_id=None)
    target = {"entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH), "room": "Attic"}
    before = env.path.read_bytes()
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "set_room", target)
    assert exc.value.translation_key == "service_no_room"
    assert env.path.read_bytes() == before
    assert env.config_calls == []
    assert ar.async_get(hass).async_get_area_by_name("Attic") is None
    await call(hass, "set_room", {**target, "create": True})
    await settled(hass, env)
    pf = env.reload()
    assert "Attic" in pf.user_groups().values()
    area = ar.async_get(hass).async_get_area_by_name("Attic")
    assert area is not None
    device = registry.async_get(switch)
    assert device is not None
    assert device.area_id == area.id


async def test_set_room_by_entity_label_and_entity_area(
    hass: HomeAssistant, env: Env
) -> None:
    """Entities referenced indirectly count: a label on the light entity itself, an entity moved into an area on
    its own; indirectly referenced entities that are not loads of ours (a key, a foreign entity, a stale entry of
    ours) are skipped without an error."""
    registry = er.async_get(hass)
    label = lr.async_get(hass).async_create("Wired").label_id
    dimmer = entity_id(hass, "light", UID_LIGHT_DIMMER)
    registry.async_update_entity(dimmer, labels={label})
    registry.async_update_entity(entity_id(hass, "event", UID_ROCKER_A), labels={label})
    foreign = registry.async_get_or_create("light", "other", "x")
    registry.async_update_entity(foreign.entity_id, labels={label})
    stale = registry.async_get_or_create(
        "light", DOMAIN, "stale-uid", config_entry=env.entry
    )
    registry.async_update_entity(stale.entity_id, labels={label})
    attic = ar.async_get(hass).async_get_or_create("Attic")
    switch = entity_id(hass, "light", UID_LIGHT_SWITCH)
    registry.async_update_entity(switch, area_id=attic.id)  # the device stays in WC

    await call(
        hass,
        "set_room",
        {"label_id": label, "area_id": attic.id, "room": "Kitchen"},
    )
    await settled(hass, env)
    pf = env.reload()
    assert 0xC011 in subs(pf, LIGHT_DIMMER, "1000")
    assert 0xC011 in subs(pf, LIGHT_SWITCH, "1000")
    assert LIGHT_CTL not in {
        node for node, _ in env.config_calls
    }  # nothing else was touched
    # a directly named entity that is not a load is still an error
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "set_room",
            {"entity_id": entity_id(hass, "event", UID_ROCKER_A), "room": "Kitchen"},
        )
    assert exc.value.translation_key == "service_not_a_load"


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"device_id": "nope"}, "service_unknown_device"),
        ({"device_id": BUTTONS_WC}, "service_not_a_load"),
        ({"entity_id": "event.wc_wc_mirror_button"}, "service_not_a_load"),
        ({"entity_id": "light.nope"}, "service_unknown_device"),
        ({"area_id": "empty"}, "service_no_loads"),
    ],
)
async def test_set_room_resolution_errors(
    hass: HomeAssistant, env: Env, data: dict[str, Any], key: str
) -> None:
    if data.get("area_id") == "empty":
        data["area_id"] = ar.async_get(hass).async_get_or_create("Empty").id
    if data.get("device_id") == BUTTONS_WC:
        data["device_id"] = device_id(hass, BUTTONS_WC)
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "set_room", {**data, "room": "WC"})
    assert exc.value.translation_key == key
    assert env.config_calls == []


async def test_set_room_of_a_foreign_device_in_the_registry(
    hass: HomeAssistant, env: Env
) -> None:
    registry = dr.async_get(hass)
    foreign = registry.async_get_or_create(
        config_entry_id=env.entry.entry_id,
        identifiers={("other", "x")},
        name="Not ours",
    )
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "set_room", {"device_id": foreign.id, "room": "WC"})
    assert exc.value.translation_key == "service_unknown_device"
    assert exc.value.translation_placeholders == {"id": foreign.id}


# ----------------------------------------------------------------------------- add_to_room / remove_from_room

DIMMER_KEY, DIMMER_KEY_GROUP = (
    0x0301,
    0xC071,
)  # the WC-linked key of the dimmer push-button, its own group


async def test_add_to_room_keeps_the_room_a_load_is_in(
    hass: HomeAssistant, env: Env
) -> None:
    """Review-4 F4-5: the DALI light joins WC and stays in Living room, wired to the WC-linked key as well; its
    device keeps its area."""
    ctl = entity_id(hass, "light", UID_LIGHT_CTL)
    await call(hass, "add_to_room", {"entity_id": ctl, "room": "WC"})
    assert env.config_calls == [
        (DALI_NODE, C.model_subscription_add(LIGHT_CTL, WC, "1000")),
        (DALI_NODE, C.model_subscription_add(LIGHT_CTL, WC, "1002")),
        (DALI_NODE, C.model_subscription_add(LIGHT_CTL, DIMMER_KEY_GROUP, "1000")),
        (DALI_NODE, C.model_subscription_add(LIGHT_CTL, DIMMER_KEY_GROUP, "1002")),
    ]
    pf = env.reload()
    assert {WC, LIVING} <= set(subs(pf, LIGHT_CTL, "1000"))
    assert hass.states.get(ctl).attributes["rooms"] == ["WC", "Living room"]
    living = ar.async_get(hass).async_get_area_by_name("Living room")
    device = dr.async_get(hass).async_get(device_id(hass, UID_LIGHT_CTL))
    assert living is not None
    assert device is not None
    assert device.area_id == living.id
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "add_to_room", {"entity_id": ctl, "room": "Attic"})
    assert exc.value.translation_key == "service_no_room"


async def test_remove_from_room_needs_force_for_a_load_a_key_drives(
    hass: HomeAssistant, env: Env
) -> None:
    """The WC mirror listens to the WC-linked dimmer key: taking it out of WC is refused, naming the key, until
    `force`; then it is in no room, its area stays and the key keeps driving the WC ceiling."""
    mirror = entity_id(hass, "light", UID_LIGHT_SWITCH)
    before = env.path.read_bytes()
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "remove_from_room", {"entity_id": mirror, "room": "WC"})
    assert exc.value.translation_key == "service_room_key_drives_load"
    placeholders = exc.value.translation_placeholders
    assert placeholders["device"] == "0148 (WC mirror)"
    assert placeholders["button"].startswith("0301 (")
    assert placeholders["room"] == "WC"
    assert env.config_calls == []
    assert env.path.read_bytes() == before
    unchanged = dr.async_get(hass).async_get(device_id(hass, UID_LIGHT_SWITCH))
    assert unchanged is not None
    await call(
        hass, "remove_from_room", {"entity_id": mirror, "room": "WC", "force": True}
    )
    assert env.config_calls[0] == (
        LIGHT_SWITCH,
        C.model_subscription_delete(LIGHT_SWITCH, DIMMER_KEY_GROUP, "1000"),
    )
    assert (
        LIGHT_SWITCH,
        C.model_subscription_delete(LIGHT_SWITCH, WC, "1000"),
    ) in env.config_calls
    pf = env.reload()
    assert WC not in subs(pf, LIGHT_SWITCH, "1000")
    assert DIMMER_KEY_GROUP in subs(pf, LIGHT_DIMMER, "1000")
    assert hass.states.get(mirror).attributes["rooms"] == []
    device = dr.async_get(hass).async_get(device_id(hass, UID_LIGHT_SWITCH))
    assert device is not None
    assert device.area_id == unchanged.area_id


# ----------------------------------------------------------------------------- blinds


def blind_entity(hass: HomeAssistant, env: Env, address: int) -> str:
    unique_id = env.hub.devices.by_address[address].unique_id
    eid = er.async_get(hass).async_get_entity_id("cover", DOMAIN, unique_id)
    assert eid is not None
    return eid


@pytest.mark.parametrize("export_source", [BLINDS_PATH])
async def test_assign_key_to_a_blind_uses_move_mode(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-07: a blind is a key target like a light: the key moves it (KeyMode *move*, Level client
    publishing to the blind's element group), by its cover entity or by its device."""
    key = env.hub.devices.by_address[0x0149]
    key_entity = er.async_get(hass).async_get_entity_id("event", DOMAIN, key.unique_id)
    await call(
        hass,
        "assign_key",
        {"key_entity": key_entity, "target_entity": blind_entity(hass, env, 0x0500)},
    )
    await settled(hass, env)
    pf = env.reload()
    group = pf.publication(0x0149, "1003")
    assert group is not None
    assert 0x0500 in [e.address for e in pf.group_members(group)]
    assert env.modes[0x0149] == bytes([KEY_MODE_MOVE])

    blind = env.hub.devices.by_address[0x0700]
    await call(
        hass,
        "assign_key",
        {"key_entity": key_entity, "target_device": device_id(hass, blind.unique_id)},
    )
    await settled(hass, env)
    assert 0x0700 in [
        e.address
        for e in env.reload().group_members(env.reload().publication(0x0149, "1003"))
    ]


@pytest.mark.parametrize("export_source", [BLINDS_PATH])
async def test_assign_key_to_a_blinds_slats(hass: HomeAssistant, env: Env) -> None:
    """Brief 38: `target_element: slat` makes the key's Level client alone publish to the slat element's group (the
    blinds mini's 0501); a room takes no target element. Unverified on air."""
    key = env.hub.devices.by_address[0x0149]
    key_entity = er.async_get(hass).async_get_entity_id("event", DOMAIN, key.unique_id)
    await call(
        hass,
        "assign_key",
        {
            "key_entity": key_entity,
            "target_entity": blind_entity(hass, env, 0x0500),
            "target_element": "slat",
        },
    )
    await settled(hass, env)
    pf = env.reload()
    group = pf.publication(0x0149, "1003")
    assert group is not None
    assert [e.address for e in pf.group_members(group)] == [
        0x0149,
        0x0501,
    ]  # the key listens too
    assert pf.publication(0x0149, "1001") is None
    assert env.modes[0x0149] == bytes([KEY_MODE_MOVE])
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "assign_key",
            {"key_entity": key_entity, "room": "Kitchen", "target_element": "slat"},
        )
    assert exc.value.translation_key == "service_target_element_needs_device"


@pytest.mark.parametrize("export_source", [BLINDS_PATH])
async def test_set_room_and_scenes_take_a_blind(hass: HomeAssistant, env: Env) -> None:
    """PLT-07: a blind joins a room like any load; a scene stores its position and slats as the JUNG blind action.

    An awning (no slat element) repeats its position in the slat field; a blind whose levels are not known yet
    is asked for them with a Level Get (position and slats), and stored without the action when it stays silent.
    """
    cover = blind_entity(hass, env, 0x0700)
    await call(hass, "set_room", {"entity_id": cover, "room": "Kitchen"})
    await settled(hass, env)
    assert 0xC011 in subs(env.reload(), 0x0700, "1002")
    await hass.services.async_call(
        DOMAIN, "create_scene", {"name": "Evening"}, blocking=True, return_response=True
    )
    await settled(hass, env)
    number = mesh_config.MeshConfigurator._scene(env.reload(), "Evening")
    env.hub.element_state(0x0700).level = -6554
    await call(hass, "store_scene", {"entity_id": cover, "scene": "Evening"})
    await settled(hass, env)
    assert env.scene_actions[0x0700, number] == (
        V.Action(V.ACTION_BLINDS, blind=-6554, slat=-6554).encode()
    )
    env.hub.element_state(0x0500).level = 0
    env.hub.element_state(0x0501).level = 32767
    await call(
        hass,
        "store_scene",
        {"entity_id": blind_entity(hass, env, 0x0500), "scene": "Evening"},
    )
    await settled(hass, env)
    assert env.scene_actions[0x0500, number] == (
        V.Action(V.ACTION_BLINDS, blind=0, slat=32767).encode()
    )
    env.app_calls.clear()
    await call(
        hass,
        "store_scene",
        {"entity_id": blind_entity(hass, env, 0x0600), "scene": "Evening"},
    )
    assert (0x0600, M.generic_level_get()) in env.app_calls
    assert (0x0601, M.generic_level_get()) in env.app_calls
    assert (0x0600, number) not in env.scene_actions


@pytest.mark.parametrize("export_source", [BLINDS_PATH])
async def test_store_scene_takes_a_blinds_position_without_moving_it(
    hass: HomeAssistant, env: Env
) -> None:
    """Review-3 W8: `store_scene` had no `position` / `tilt_position`, so a blind could only be stored where it
    stood. Given, they become the blind's JUNG scene action (the tilt defaults to the position); the blind is
    not moved, which would take longer than the call can wait. A light in the same call is refused, in the
    scene's own words."""
    await hass.services.async_call(
        DOMAIN, "create_scene", {"name": "Evening"}, blocking=True, return_response=True
    )
    await settled(hass, env)
    number = mesh_config.MeshConfigurator._scene(env.reload(), "Evening")
    env.app_calls.clear()
    await call(
        hass,
        "store_scene",
        {
            "entity_id": blind_entity(hass, env, 0x0500),
            "scene": "Evening",
            "position": 25,
            "tilt_position": 100,
        },
    )
    await settled(hass, env)
    assert env.scene_actions[0x0500, number] == (
        V.Action(
            V.ACTION_BLINDS,
            blind=closedness_to_level(75),
            slat=closedness_to_level(0),
        ).encode()
    )
    assert not sets_to(env, 0x0500, LEVEL_SETS)
    assert not sets_to(env, 0x0501, LEVEL_SETS)
    await call(
        hass,
        "store_scene",
        {"entity_id": blind_entity(hass, env, 0x0700), "scene": number, "position": 60},
    )
    await settled(hass, env)
    assert env.scene_actions[0x0700, number] == (
        V.Action(
            V.ACTION_BLINDS,
            blind=closedness_to_level(40),
            slat=closedness_to_level(40),
        ).encode()
    )
    light = entity_id(hass, "light", UID_LIGHT_SWITCH)
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "store_scene",
            {"entity_id": light, "scene": "Evening", "position": 60},
        )
    assert exc.value.translation_key == "scene_state_not_applicable"
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "store_scene",
            {
                "entity_id": blind_entity(hass, env, 0x0700),
                "scene": "Evening",
                "tilt_position": 5,
            },
        )
    assert exc.value.translation_key == "scene_state_needs_position"


def sets_to(env: Env, dst: int, opcodes: set[int]) -> list[bytes]:
    """The parameters of every Set of `opcodes` that went out to `dst` over the fake link."""
    out = []
    for _src, to, pdu in env.link.sent:
        op, _cid, params = decode_opcode(pdu)
        if to == dst and op in opcodes:
            out.append(params)
    return out


LIGHTNESS_SETS = {M.LIGHT_LIGHTNESS_SET, M.LIGHT_LIGHTNESS_SET_UNACK}
CTL_SETS = {M.LIGHT_CTL_SET, M.LIGHT_CTL_SET_UNACK}
ONOFF_SETS = {M.GEN_ONOFF_SET, M.GEN_ONOFF_SET_UNACK}
LEVEL_SETS = {M.GEN_LEVEL_SET, M.GEN_LEVEL_SET_UNACK}


async def test_store_scene_sets_the_state_first(hass: HomeAssistant, env: Env) -> None:
    """A state in the call: each load is set to it and asked until its ramp is over, then its reported state is
    stored — a dimmer that lands a step off what was asked is recorded as it is."""
    lit = round(0.4 * 0xFFFF)
    env.state_replies[LIGHT_DIMMER, M.LIGHT_LIGHTNESS_GET] = [
        (0x1000).to_bytes(2, "little") + lit.to_bytes(2, "little") + b"\x05",  # ramping
        (lit - 1).to_bytes(2, "little"),  # there
    ]
    env.app_calls.clear()
    await call(
        hass,
        "store_scene",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_DIMMER),
            "scene": "All off",
            "brightness_pct": 40,
        },
    )
    assert [p[:2] for p in sets_to(env, LIGHT_DIMMER, LIGHTNESS_SETS)] == [
        lit.to_bytes(2, "little")
    ]
    gets = [pdu for dst, pdu in env.app_calls if dst == LIGHT_DIMMER]
    # up to the Scene Store: the reloaded hub's connect-time refresh may ask again before the call returns
    stored = gets.index(M.scene_store(2))
    assert gets[:stored].count(M.light_lightness_get()) == 2
    assert env.scene_actions[LIGHT_DIMMER, 2] == (
        V.Action(V.ACTION_LIGHTNESS, lightness=lit - 1).encode()
    )


async def test_store_scene_switch_and_tunable_white(
    hass: HomeAssistant, env: Env
) -> None:
    """A switch insert is switched; a tunable-white light gets its colour temperature clamped to its own range."""
    env.onoff[LIGHT_SWITCH] = False
    await call(
        hass,
        "store_scene",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
            "scene": "All off",
            "action": "off",
        },
    )
    assert [p[:1] for p in sets_to(env, LIGHT_SWITCH, ONOFF_SETS)] == [b"\x00"]
    assert env.scene_actions[LIGHT_SWITCH, 2] == (
        V.Action(V.ACTION_SWITCH, on=False).encode()
    )
    await settled(hass, env)
    await refreshed(
        hass, env
    )  # no refresh Get left to take the CTL Status answering the Set
    st = env.hub.element_state(LIGHT_CTL)
    st.kelvin_min, st.kelvin_max = 2700, 6500
    env.state_replies[LIGHT_CTL, M.LIGHT_CTL_GET] = [
        (0xFFFF).to_bytes(2, "little") + (2700).to_bytes(2, "little")
    ]
    await call(
        hass,
        "store_scene",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_CTL),
            "scene": "All off",
            "brightness_pct": 100,
            "color_temp_kelvin": 2000,
        },
    )
    assert [p[:4] for p in sets_to(env, LIGHT_CTL, CTL_SETS)] == [
        (0xFFFF).to_bytes(2, "little") + (2700).to_bytes(2, "little")
    ]
    assert env.scene_actions[LIGHT_CTL, 2] == (
        V.Action(V.ACTION_LIGHTNESS_CT, lightness=0xFFFF, temperature_k=2700).encode()
    )


async def test_store_scene_state_never_arrives(hass: HomeAssistant, env: Env) -> None:
    """A load still ramping, or silent, after every attempt: the call fails and nothing is stored."""
    env.state_replies[LIGHT_DIMMER, M.LIGHT_LIGHTNESS_GET] = [
        (0x1000).to_bytes(2, "little") + (0x2000).to_bytes(2, "little") + b"\x05"
    ]
    dimmer = entity_id(hass, "light", UID_LIGHT_DIMMER)
    data = {"entity_id": dimmer, "scene": "All off", "action": "on"}
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, "store_scene", data)
    assert exc.value.translation_key == "scene_state_not_reached"
    assert exc.value.translation_placeholders == {"name": dimmer}
    del env.state_replies[LIGHT_DIMMER, M.LIGHT_LIGHTNESS_GET]
    with pytest.raises(HomeAssistantError):
        await call(hass, "store_scene", data)
    assert (LIGHT_DIMMER, 2) not in env.scene_actions


async def test_store_scene_state_is_checked_first(
    hass: HomeAssistant, env: Env
) -> None:
    """A state that does not fit one of the loads refuses the call before anything is sent; a lost link fails it."""
    socket = entity_id(hass, "switch", UID_SOCKET)
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "store_scene",
            {
                "entity_id": [entity_id(hass, "light", UID_LIGHT_DIMMER), socket],
                "scene": "All off",
                "brightness_pct": 50,
            },
        )
    # review-3 W8: the reason is a translation of its own, not the schedule helper's English text in a placeholder
    assert exc.value.translation_key == "scene_state_not_applicable"
    assert exc.value.translation_placeholders == {
        "name": socket,
        "fields": "brightness_pct",
    }
    assert str(exc.value) == (
        f"The state does not fit {socket}: brightness_pct does not apply to it"
    )
    assert set(svc.SCENE_STATE_ERRORS.values()) <= set(
        json.loads(
            (Path(svc.__file__).parent / "strings.json").read_text(encoding="utf-8")
        )["exceptions"]
    )
    assert not sets_to(env, LIGHT_DIMMER, LIGHTNESS_SETS)
    with (
        patch.object(
            type(env.hub), "set_onoff", AsyncMock(side_effect=ConnectionError)
        ),
        pytest.raises(HomeAssistantError) as exc,
    ):
        await call(
            hass,
            "store_scene",
            {"entity_id": socket, "scene": "All off", "action": "on"},
        )
    assert exc.value.translation_key == "send_failed"


@pytest.mark.parametrize("export_source", [FIXTURES / "MeshNetwork-rtr.json"])
async def test_store_scene_sets_a_thermostat(hass: HomeAssistant, env: Env) -> None:
    rtr = env.hub.devices.thermostats[0]
    climate = er.async_get(hass).async_get_entity_id("climate", DOMAIN, rtr.unique_id)
    await hass.services.async_call(
        DOMAIN, "create_scene", {"name": "Warm"}, blocking=True, return_response=True
    )
    await settled(hass, env)
    number = mesh_config.MeshConfigurator._scene(env.reload(), "Warm")
    level = temperature_to_level(22.0)
    env.state_replies[rtr.address, M.GEN_LEVEL_GET] = [
        level.to_bytes(2, "little", signed=True)
    ]
    await call(
        hass, "store_scene", {"entity_id": climate, "scene": "Warm", "temperature": 22}
    )
    assert [p[:2] for p in sets_to(env, rtr.address, LEVEL_SETS)] == [
        level.to_bytes(2, "little", signed=True)
    ]
    assert env.scene_actions[rtr.address, number] == (
        V.Action(V.ACTION_TEMPERATURE, temperature_c=22.0).encode()
    )


@pytest.mark.parametrize("export_source", [FIXTURES / "MeshNetwork-rtr.json"])
async def test_scenes_take_a_thermostat(hass: HomeAssistant, env: Env) -> None:
    """A thermostat stores its set-point as the JUNG target-temperature action (unverified on hardware)."""
    rtr = env.hub.devices.thermostats[0]
    climate = er.async_get(hass).async_get_entity_id("climate", DOMAIN, rtr.unique_id)
    assert climate is not None
    await hass.services.async_call(
        DOMAIN, "create_scene", {"name": "Warm"}, blocking=True, return_response=True
    )
    await settled(hass, env)
    number = mesh_config.MeshConfigurator._scene(env.reload(), "Warm")
    env.hub.element_state(rtr.address).level = temperature_to_level(21.0)
    await call(hass, "store_scene", {"entity_id": climate, "scene": "Warm"})
    assert env.scene_actions[rtr.address, number] == (
        V.Action(V.ACTION_TEMPERATURE, temperature_c=21.0).encode()
    )


# ----------------------------------------------------------------------------- rooms


async def test_create_rename_delete_room(hass: HomeAssistant, env: Env) -> None:
    hub = env.hub
    await call(hass, "create_room", {"name": "Attic"})
    assert env.hub is hub  # nothing on the mesh, no reload
    assert "Attic" in env.reload().user_groups().values()

    await call(
        hass,
        "rename_room",
        {"room": "Attic", "new_name": "Loft", "config_entry_id": env.entry.entry_id},
    )
    await settled(hass, env)
    assert "Loft" in env.reload().user_groups().values()

    registry = er.async_get(hass)
    mesh = env.hub.cdb.mesh_uuid.lower()
    wc_lights = registry.async_get_entity_id(
        "light", DOMAIN, f"{mesh}-room-c00f-lights"
    )
    assert wc_lights is not None
    await call(hass, "delete_room", {"room": "WC"})
    await settled(hass, env)
    pf = env.reload()
    assert WC not in pf.cdb.groups
    # its central entity (on the mesh device, which stays) goes with the room; the other rooms' stay
    assert registry.async_get(wc_lights) is None
    assert hass.states.get(wc_lights) is None
    assert registry.async_get_entity_id("light", DOMAIN, f"{mesh}-room-c011-lights")
    assert (0x0148, C.model_subscription_delete(0x0148, WC, "1000")) in env.config_calls
    state = hass.states.get(entity_id(hass, "light", UID_LIGHT_SWITCH))
    assert state is not None
    assert state.attributes["rooms"] == []


async def test_room_services_need_a_loaded_entry(hass: HomeAssistant, env: Env) -> None:
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "create_room", {"name": "X", "config_entry_id": "nope"})
    assert (
        exc.value.translation_key == "service_unknown_entry"
    )  # not one of ours at all
    stale = MockConfigEntry(domain=DOMAIN, unique_id="stale", data={})
    stale.add_to_hass(hass)
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass, "delete_room", {"room": "WC", "config_entry_id": stale.entry_id}
        )
    assert exc.value.translation_key == "service_entry_not_loaded"
    # two loaded entries: the call must name one
    stale.mock_state(hass, ConfigEntryState.LOADED)
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "rename_room", {"room": "WC", "new_name": "X"})
    assert exc.value.translation_key == "service_entry_ambiguous"
    stale.mock_state(hass, ConfigEntryState.NOT_LOADED)
    # a loaded entry without a configurator (torn down under our feet)
    hass.data[svc.CONFIGURATORS].pop(env.entry.entry_id)
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass, "create_room", {"name": "X", "config_entry_id": env.entry.entry_id}
        )
    assert exc.value.translation_key == "service_entry_not_loaded"


async def test_calls_for_one_entry_are_serialised(
    hass: HomeAssistant, env: Env
) -> None:
    """Two calls at once: the second waits for the first, including its reload, and sees the new hub."""
    first = hass.services.async_call(
        DOMAIN,
        "set_room",
        {"entity_id": entity_id(hass, "light", UID_LIGHT_CTL), "room": "WC"},
        blocking=True,
    )
    second = hass.services.async_call(
        DOMAIN,
        "set_room",
        {"entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH), "room": "Kitchen"},
        blocking=True,
    )
    await asyncio.gather(first, second)
    await settled(hass, env)
    pf = env.reload()
    assert WC in subs(pf, LIGHT_CTL, "1000")
    assert 0xC011 in subs(pf, LIGHT_SWITCH, "1000")
    assert svc._lock(hass, env.entry.entry_id).locked() is False
    assert env.entry.state is ConfigEntryState.LOADED


async def test_a_call_waits_for_the_link_of_the_reloaded_entry(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-04: every call that changes the device model reloads the entry, and the reloaded hub connects in the
    background; the next call must wait for that link rather than fail with "send failed" straight away."""
    # set before the first call: `call()` settles the loop, which would otherwise let the reload reconnect
    env.link.connect_errors = [ConnectionError("busy")] * 3
    await call(
        hass,
        "set_room",
        {"entity_id": entity_id(hass, "light", UID_LIGHT_CTL), "room": "WC"},
    )
    await call(
        hass,
        "set_room",
        {
            "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
            "room": "Kitchen",
        },
    )
    await settled(hass, env)
    assert 0xC011 in subs(env.reload(), LIGHT_SWITCH, "1000")


@pytest.mark.unavailable_ok
async def test_a_call_gives_up_when_no_link_comes(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-04: the wait is bounded; with no link in sight the call is refused with its own message, untouched."""
    before = env.path.read_bytes()
    with patch.object(svc, "SERVICE_LINK_WAIT", 0.01):
        env.link.connect_errors = [ConnectionError("busy")] * 100_000
        env.link.drop_link()
        await hass.async_block_till_done()
        with pytest.raises(HomeAssistantError) as exc:
            await call(
                hass,
                "set_room",
                {
                    "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                    "room": "Kitchen",
                },
            )
        assert exc.value.translation_key == "service_not_connected"
        # review-3 W13: the mesh out of reach is not bad input, so not a validation error
        assert not isinstance(exc.value, ServiceValidationError)
        assert env.path.read_bytes() == before

        # file-only operations do not need the link at all
        for service, data in (
            ("create_room", {"name": "Attic"}),
            ("rename_room", {"room": "Attic", "new_name": "Loft"}),
            ("create_scene", {"name": "Evening"}),
            ("rename_scene", {"scene": "Evening", "new_name": "Night"}),
        ):
            await call(hass, service, data)
        pf = env.reload()
        assert "Loft" in pf.user_groups().values()
        assert "Night" in pf.scene_names().values()
    env.link.connect_errors = []


@pytest.mark.unavailable_ok
async def test_a_call_waits_again_when_the_hub_was_replaced_meanwhile(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-04: an options / reconfigure reload outside the service lock can swap the hub during the wait; the call
    re-resolves the configurator and waits for the new hub's link too, instead of running on a torn-down one."""
    old = env.hub
    waited: list[Any] = []
    replaced: list[Any] = []

    async def wait_and_replace(self: Any, timeout: float) -> bool:
        waited.append(self)
        if self is old:
            await hass.config_entries.async_reload(env.entry.entry_id)
            replaced.append(env.hub)
            return True
        return await real_wait(self, timeout)

    real_wait = type(old).async_wait_connected
    with patch.object(type(old), "async_wait_connected", wait_and_replace):
        await call(
            hass,
            "set_room",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                "room": "Kitchen",
            },
        )
    assert waited == [old, replaced[0]]
    assert replaced[0] is not old
    await settled(hass, env)
    assert 0xC011 in subs(env.reload(), LIGHT_SWITCH, "1000")


@pytest.mark.unavailable_ok
async def test_a_call_gives_up_when_the_replaced_hub_does_not_connect_in_time(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-04: the second wait, on a hub that replaced the first during its wait, is bounded as well."""
    old = env.hub
    before = env.path.read_bytes()

    async def wait_and_replace(self: Any, timeout: float) -> bool:
        if self is old:
            await hass.config_entries.async_reload(env.entry.entry_id)
            return True
        return False  # the new hub's link never comes up in the time left

    with (
        patch.object(type(old), "async_wait_connected", wait_and_replace),
        patch.object(svc, "SERVICE_LINK_WAIT", 0.05),
        pytest.raises(HomeAssistantError) as exc,
    ):
        await call(
            hass,
            "set_room",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                "room": "Kitchen",
            },
        )
    assert exc.value.translation_key == "service_not_connected"
    assert not isinstance(exc.value, ServiceValidationError)
    assert env.path.read_bytes() == before


async def test_a_stopped_plan_still_has_the_model_follow_the_export(
    hass: HomeAssistant, env: Env
) -> None:
    """CFG-15: a plan that stopped after recording what the mesh accepted raises, but the export changed: HA's
    device model follows it, as after a finished plan — in place (review-4 D23)."""

    async def stopped(self: Any, addresses: Any, room: str, **_kwargs: Any) -> bool:
        self.recorded = True
        raise HomeAssistantError("stopped")

    with (
        patch.object(mesh_config.MeshConfigurator, "set_rooms", stopped),
        patch.object(
            svc, "async_follow_export", wraps=svc.async_follow_export
        ) as follow,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
        pytest.raises(HomeAssistantError, match="stopped"),
    ):
        await call(
            hass,
            "set_room",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                "room": "Kitchen",
            },
        )
    follow.assert_awaited_once_with(hass, env.entry.entry_id, scenes=False)
    reload.assert_not_awaited()


async def test_a_failed_operation_that_recorded_nothing_does_not_reload(
    hass: HomeAssistant, env: Env
) -> None:
    """CFG-15: an operation that failed before writing anything leaves the entry and its model alone."""
    with (
        patch.object(
            svc, "async_follow_export", wraps=svc.async_follow_export
        ) as follow,
        pytest.raises(ServiceValidationError),
    ):
        await call(hass, "delete_room", {"room": "No such room"})
    follow.assert_not_awaited()


async def test_a_failure_does_not_reload_for_what_an_earlier_call_recorded(
    hass: HomeAssistant, env: Env
) -> None:
    """`recorded` belongs to the running call: `create_room` wrote the export without changing the model, and a
    later call that fails before planning anything must not have the model follow on that account."""
    await call(hass, "create_room", {"name": "Attic"})

    async def refused(self: Any, addresses: Any, room: str, **_kwargs: Any) -> bool:
        raise HomeAssistantError("refused before planning")

    with (
        patch.object(mesh_config.MeshConfigurator, "set_rooms", refused),
        patch.object(
            svc, "async_follow_export", wraps=svc.async_follow_export
        ) as follow,
        pytest.raises(HomeAssistantError, match="refused before planning"),
    ):
        await call(
            hass,
            "set_room",
            {"entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH), "room": "WC"},
        )
    follow.assert_not_awaited()


async def test_a_room_assignment_that_is_already_so_does_not_reload(
    hass: HomeAssistant, env: Env
) -> None:
    """CFG-14 through the service: `set_room` has the model follow only when the plan changed it."""
    target = {
        "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
        "room": "Attic",
        "create": True,
    }
    with patch.object(
        svc, "async_follow_export", wraps=svc.async_follow_export
    ) as follow:
        await call(hass, "set_room", target)
        follow.assert_awaited_once_with(hass, env.entry.entry_id, scenes=False)
        await settled(hass, env)
        follow.reset_mock()
        before = env.path.read_bytes()
        await call(hass, "set_room", target)
        follow.assert_not_awaited()
    assert env.path.read_bytes() == before


async def test_creating_a_room_follows_the_gateways_export_it_adopted(
    hass: HomeAssistant, env: Env
) -> None:
    """A new room alone changes no device, but the gateway export adopted before it can: the model follows it."""
    create_room = mesh_config.MeshConfigurator.create_room

    async def adopting(self: mesh_config.MeshConfigurator, name: str) -> int:
        address = await create_room(self, name)
        self.adopted = True
        return address

    with patch.object(
        svc, "async_follow_export", wraps=svc.async_follow_export
    ) as follow:
        await call(hass, "create_room", {"name": "Attic"})
        follow.assert_not_awaited()
        with patch.object(mesh_config.MeshConfigurator, "create_room", adopting):
            await call(hass, "create_room", {"name": "Cellar"})
        follow.assert_awaited_once_with(hass, env.entry.entry_id, scenes=False)
    assert runs_the_export(env)


async def test_key_letter_with_a_key_entity_is_rejected(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-09: `key` names a key of a `key_device`; with a `key_entity` it was silently ignored, rewiring the
    entity's key while the user meant another."""
    before = env.path.read_bytes()
    for service in ("clear_key", "assign_key"):
        data: dict[str, Any] = {
            "key_entity": entity_id(hass, "event", UID_ROCKER_A),
            "key": "B",
        }
        if service == "assign_key":
            data["room"] = "WC"
        with pytest.raises(vol.Invalid, match="key_device"):
            await call(hass, service, data)
    assert env.path.read_bytes() == before


async def test_a_config_entity_is_not_a_load(hass: HomeAssistant, env: Env) -> None:
    """PLT-10: one of our entities that is no mesh load (a config switch) is "not a load", not an unknown device;
    as a key, "not a key"."""
    devices = {d.unique_id for d in env.hub.devices.by_address.values()}
    config_switch = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(
            er.async_get(hass), env.entry.entry_id
        )
        if e.domain == "switch" and e.unique_id not in devices
    )
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "set_room", {"entity_id": config_switch, "room": "Kitchen"})
    assert exc.value.translation_key == "service_not_a_load"
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "clear_key", {"key_entity": config_switch})
    assert exc.value.translation_key == "service_not_a_key"


@pytest.mark.unavailable_ok
async def test_a_call_follows_a_hub_replaced_while_the_link_was_down(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-04 re-review: a hub torn down by an unlocked reload while disconnected never connects again; the wait
    moves on to its replacement instead of sitting out the whole SERVICE_LINK_WAIT on it."""
    loop = asyncio.get_running_loop()
    with patch.object(svc, "SERVICE_LINK_WAIT", 5.0):
        env.link.connect_errors = [ConnectionError("busy")] * 100_000
        env.link.drop_link()
        await hass.async_block_till_done()
        started = loop.time()
        task = asyncio.create_task(
            call(
                hass,
                "set_room",
                {
                    "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                    "room": "Kitchen",
                },
            )
        )
        await asyncio.sleep(0)
        env.link.connect_errors = []
        await hass.config_entries.async_reload(
            env.entry.entry_id
        )  # an options change, say
        await task
        assert loop.time() - started < 4.0
    await settled(hass, env)
    assert 0xC011 in subs(env.reload(), LIGHT_SWITCH, "1000")


async def test_a_wait_that_catches_the_entry_mid_reload_looks_again(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-04 re-review: an entry briefly not loaded (a reload in progress) when the wait looks it up costs a step,
    not the call."""
    real = svc._configurator
    lookups = [0]

    def configurator(hass_: HomeAssistant, entry_id: str) -> Any:
        lookups[0] += 1
        if lookups[0] == 2:  # the first look after the first wait step
            raise svc._validation("service_entry_not_loaded")
        return real(hass_, entry_id)

    with patch.object(svc, "_configurator", configurator):
        await call(
            hass,
            "set_room",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                "room": "Kitchen",
            },
        )
    assert lookups[0] >= 3
    await settled(hass, env)
    assert 0xC011 in subs(env.reload(), LIGHT_SWITCH, "1000")


async def test_a_wait_gives_up_when_the_entry_stays_unloaded(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-04 re-review: an entry that never comes back within the wait is "not loaded", not a hang."""
    real = svc._configurator
    lookups = [0]

    def configurator(hass_: HomeAssistant, entry_id: str) -> Any:
        lookups[0] += 1
        if lookups[0] > 1:  # gone for good after the call started
            raise svc._validation("service_entry_not_loaded")
        return real(hass_, entry_id)

    with (
        patch.object(svc, "_configurator", configurator),
        patch.object(svc, "SERVICE_LINK_WAIT", 0.05),
        patch.object(svc, "LINK_WAIT_SLICE", 0.01),
        pytest.raises(ServiceValidationError) as exc,
    ):
        await call(
            hass,
            "set_room",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                "room": "Kitchen",
            },
        )
    assert exc.value.translation_key == "service_entry_not_loaded"


def test_bound_handler_forwards_hass() -> None:
    seen: list[Any] = []

    async def handler(hass: Any, call_: Any) -> None:
        seen.append((hass, call_))

    bound: Callable[[Any], Any] = svc._bound("hass", handler)  # type: ignore[arg-type]
    asyncio.run(bound("call"))
    assert seen == [("hass", "call")]


async def test_unknown_button_event_entity_unique_id(
    hass: HomeAssistant, env: Env
) -> None:
    """An entity of ours whose unique id matches no device (a stale registry entry): ours, but no key (PLT-10)."""
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "event", DOMAIN, "stale-uid", config_entry=env.entry
    )
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "clear_key", {"key_entity": stale.entity_id})
    assert exc.value.translation_key == "service_not_a_key"
    assert exc.value.translation_placeholders == {"name": stale.entity_id}


async def test_key_device_may_be_the_node_device(hass: HomeAssistant, env: Env) -> None:
    """The node device of a 1-gang push-button stands for its single key too."""
    node = device_id(hass, f"node:{NODE_LIGHT_SWITCH}")
    await call(hass, "clear_key", {"key_device": node})
    await settled(hass, env)
    assert env.config_calls[0] == (
        0x0148,
        C.model_publication_set(0x0149, 0x0000, "1001"),
    )
    assert entity_id(hass, "event", UID_BUTTON_WC)


async def test_devices_of_other_integrations_are_not_ours(
    hass: HomeAssistant, env: Env
) -> None:
    """A device that belongs to another integration's config entry, even with an identifier of ours."""
    other = MockConfigEntry(domain="other", unique_id="x", data={})
    other.add_to_hass(hass)
    registry = dr.async_get(hass)
    foreign = registry.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("other", "y"), (DOMAIN, BUTTONS_WC)},
        name="Theirs",
    )
    for service, data in (
        ("set_room", {"device_id": foreign.id, "room": "WC"}),
        ("assign_key", {"key_device": foreign.id, "room": "WC"}),
        ("clear_key", {"key_device": foreign.id}),
    ):
        with pytest.raises(ServiceValidationError) as exc:
            await call(hass, service, data)
        assert exc.value.translation_key == "service_unknown_device"
        assert exc.value.translation_placeholders == {"id": foreign.id}
    assert env.config_calls == []


@pytest.mark.unavailable_ok
async def test_room_services_without_any_loaded_entry(
    hass: HomeAssistant, env: Env
) -> None:
    assert await hass.config_entries.async_unload(env.entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as exc:
        svc._entry_for_hub_services(hass, {})
    assert exc.value.translation_key == "service_entry_not_loaded"


# ----------------------------------------------------------------------------- scenes


async def test_create_rename_and_delete_scene(hass: HomeAssistant, env: Env) -> None:
    response = await hass.services.async_call(
        DOMAIN,
        "create_scene",
        {"name": "Movie night"},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    # the top of the range; no plan, the export written
    assert response == {
        "scene": 0x1999,
        "name": "Movie night",
        **NO_PLAN,
        "recorded": True,
    }
    await settled(hass, env)
    assert env.reload().scene_names()[0x1999] == "Movie night"
    assert (
        hass.states.get("scene.movie_night") is not None
    )  # the reload added the entity
    await call(hass, "rename_scene", {"scene": "Movie night", "new_name": "Cinema"})
    await settled(hass, env)
    assert env.reload().scene_names()[0x1999] == "Cinema"
    # deleting the fixture's scene 1 tells its one member (the WC light) to forget it
    env.app_calls.clear()
    await call(
        hass, "delete_scene", {"scene": "WC off", "config_entry_id": env.entry.entry_id}
    )
    await settled(hass, env)
    assert (LIGHT_SWITCH, M.scene_delete(1)) in env.app_calls
    assert (LIGHT_SWITCH, V.scene_action_set(1)) in env.app_calls
    assert env.registers[LIGHT_SWITCH] == []
    assert 1 not in env.reload().cdb.scenes
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "delete_scene", {"scene": "WC off"})
    assert exc.value.translation_key == "service_unknown_scene"
    # `force` needs `confirm` (decision as M3 / M8): it cannot be undone
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "delete_scene", {"scene": "Cinema", "force": True})
    assert exc.value.translation_key == "delete_scene_force_needs_confirm"
    # `force` reaches the configurator, and the members it skipped come back
    with patch.object(
        mesh_config.MeshConfigurator,
        "delete_scene",
        autospec=True,
        return_value=["0232"],
    ) as delete:
        response = await hass.services.async_call(
            DOMAIN,
            "delete_scene",
            {"scene": "Cinema", "force": True, "confirm": True},
            blocking=True,
            return_response=True,
        )
    assert response == {"skipped": ["0232"], **NO_PLAN}
    assert delete.call_args.args[1:] == ("Cinema",)
    assert delete.call_args.kwargs == {"force": True}
    await settled(hass, env)
    # a scene number no scene of the export has: listed by default (a dry run), deleted when told so
    env.registers[LIGHT_SWITCH] = [9]
    env.app_calls.clear()
    response = await hass.services.async_call(
        DOMAIN, "delete_unused_scenes", {}, blocking=True, return_response=True
    )
    assert response == {"0148": [9], "unanswered": []}
    assert env.registers[LIGHT_SWITCH] == [9]
    assert (LIGHT_SWITCH, M.scene_delete(9)) not in env.app_calls
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "delete_unused_scenes", {"dry_run": False})
    assert exc.value.translation_key == "service_unused_scenes_stale_export"
    response = await hass.services.async_call(
        DOMAIN,
        "delete_unused_scenes",
        {"dry_run": False, "numbers": ["9"]},
        blocking=True,
        return_response=True,
    )
    assert response == {"0148": [9], "unanswered": []}
    assert env.registers[LIGHT_SWITCH] == []
    await call(
        hass, "delete_unused_scenes", {"dry_run": False, "confirm_stale_export": True}
    )  # nothing left, no response asked


async def test_store_and_remove_from_scene(hass: HomeAssistant, env: Env) -> None:
    """The targeted loads store their present state; the scene entity then shows what each member does."""
    env.hub.element_state(LIGHT_CTL).on = True
    env.hub.element_state(LIGHT_CTL).lightness = 0xFFFF
    env.hub.element_state(LIGHT_CTL).kelvin = 2700
    socket = entity_id(
        hass, "switch", UID_SOCKET
    )  # state unknown: stored without an action record
    env.app_calls.clear()
    await call(
        hass,
        "store_scene",
        {
            "device_id": device_id(hass, UID_LIGHT_CTL),
            "entity_id": socket,
            "scene": "All off",
        },
    )
    await settled(hass, env)
    ctl_action = V.Action(V.ACTION_LIGHTNESS_CT, lightness=0xFFFF, temperature_k=2700)
    scene_calls = [  # the config entities' property reads interleave; only the scene messages matter here
        (dst, pdu)
        for dst, pdu in env.app_calls
        if dst == LIGHT_CTL
        and decode_opcode(pdu)[0] in (M.SCENE_STORE, V.SCENE_ACTION_SETUP_SET)
    ]
    assert scene_calls == [
        (LIGHT_CTL, M.scene_store(2)),
        (LIGHT_CTL, V.scene_action_set(2, ctl_action)),
    ]
    assert (SOCKET, M.scene_store(2)) in env.app_calls
    assert (SOCKET, V.scene_action_set(2, V.NO_ACTION)) not in env.app_calls
    assert env.registers[LIGHT_CTL] == [2]
    assert env.registers[SOCKET] == [2]
    assert env.reload().cdb.scenes[2] == [LIGHT_CTL, SOCKET]
    # the reloaded hub read the members' actions after connecting; the scene entity shows them
    await refreshed(hass, env)
    state = hass.states.get("scene.all_off")
    assert state is not None
    assert state.attributes["scene_number"] == 2
    assert state.attributes["members"] == {
        "Living room DALI": "lightness 100% 2700K",
        "Boiler": "stored",
    }
    await call(hass, "remove_from_scene", {"entity_id": socket, "scene": "2"})
    await settled(hass, env)
    assert env.registers[SOCKET] == []
    assert env.reload().cdb.scenes[2] == [LIGHT_CTL]
    await refreshed(hass, env)
    state = hass.states.get("scene.all_off")
    assert state is not None
    assert state.attributes["members"] == {"Living room DALI": "lightness 100% 2700K"}


async def test_a_late_scene_action_status_of_another_scene_does_not_answer_the_sibling_check(
    hass: HomeAssistant, env: Env, fake_link: FakeProxyLink
) -> None:
    """Review-3 Q2, AppKey side: taking one channel of the 2-channel actuator out of a scene asks the other channel
    whether it still holds an action for that scene. A late duplicate of that channel's earlier answer — its
    action for another scene — arrives first: same element, same opcode, same key; only the scene number it names
    tells it apart. Taken for the answer, the shared register would be kept for a scene nobody uses."""
    env.hub.element_state(LIGHT_OUT2).on = True
    await call(
        hass,
        "store_scene",
        {"entity_id": entity_id(hass, "light", UID_LIGHT_OUT2), "scene": "All off"},
    )
    await settled(hass, env)
    assert env.registers[LIGHT_OUT1] == [2]  # the node's register, on its first element
    assert env.reload().cdb.scenes[2] == [LIGHT_OUT1]
    answer = fake_link.app_reply
    assert answer is not None
    # the other channel was stored in scene 1 some time ago; its status for that Set is what repeats late
    earlier = answer(
        LIGHT_OUT1, V.scene_action_set(1, V.Action(V.ACTION_SWITCH, on=True))
    )
    assert earlier is not None

    def with_a_stale_duplicate(dst: int, pdu: bytes) -> bytes | None:
        if dst == LIGHT_OUT1 and pdu == V.scene_action_get(2):
            fake_link.inject(LIGHT_OUT1, OUR, earlier)
        return answer(dst, pdu)

    fake_link.app_reply = with_a_stale_duplicate
    await call(
        hass,
        "remove_from_scene",
        {"entity_id": entity_id(hass, "light", UID_LIGHT_OUT2), "scene": "2"},
    )
    await settled(hass, env)
    assert (
        LIGHT_OUT1,
        V.scene_action_get(2),
    ) in env.app_calls  # the check was made, and the stale status came
    assert (
        env.registers[LIGHT_OUT1] == []
    )  # no channel uses scene 2 any more: deleted from the register
    assert env.reload().cdb.scenes[2] == []


@pytest.mark.unavailable_ok  # the test empties a load's cached state, so an entity of it may show `unknown`
async def test_store_scene_after_a_reload_records_the_present_state(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-05: a `store_scene` right after another call's reload runs on a hub whose state cache is still empty;
    it asks the load for its state instead of recording nothing (or reading the torn-down hub it picked before
    the lock). The cache is emptied by hand here, so an entity of that load writing its state in between shows
    `unknown`: the transitions check (`unavailable_ok`) does not apply."""
    env.hub.element_state(LIGHT_CTL).on = True
    env.hub.element_state(LIGHT_CTL).lightness = 0xFFFF
    env.hub.element_state(LIGHT_CTL).kelvin = 2700
    await call(
        hass,
        "store_scene",
        {"entity_id": entity_id(hass, "light", UID_LIGHT_CTL), "scene": "All off"},
    )
    await settled(hass, env)
    env.hub.states.pop(LIGHT_SWITCH, None)
    env.onoff[LIGHT_SWITCH] = True
    env.app_calls.clear()
    await call(
        hass,
        "store_scene",
        {"entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH), "scene": "All off"},
    )
    assert (
        LIGHT_SWITCH,
        V.scene_action_set(2, V.Action(V.ACTION_SWITCH, on=True)),
    ) in env.app_calls


async def test_store_scene_refuses_a_load_the_replacing_hub_does_not_have(
    hass: HomeAssistant, env: Env
) -> None:
    """PLT-05: the actions are built from the hub the lock handed over; a load a reload made disappear meanwhile
    is refused by name rather than raising a KeyError."""

    async def wait_and_lose_the_load(self: Any, timeout: float) -> bool:
        self.devices.by_address.pop(LIGHT_SWITCH)
        return True

    with (
        patch.object(type(env.hub), "async_wait_connected", wait_and_lose_the_load),
        pytest.raises(ServiceValidationError) as exc,
    ):
        await call(
            hass,
            "store_scene",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                "scene": "All off",
            },
        )
    assert exc.value.translation_key == "service_unknown_device"


async def test_refreshing_one_element_is_best_effort(env: Env) -> None:
    """PLT-05: the single-element state Get leaves a lost link to the caller's next send to report."""
    env.link.write_error = ConnectionError("gone")  # the Get's GATT write fails
    await env.hub.async_refresh_element(LIGHT_SWITCH, "switch")
    assert LIGHT_SWITCH not in env.hub.states or env.hub.states[LIGHT_SWITCH].on is None


async def test_scene_services_validate_their_targets(
    hass: HomeAssistant, env: Env
) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, "store_scene", {"scene": "All off"})  # no target
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "store_scene",
            {"entity_id": entity_id(hass, "switch", UID_SOCKET), "scene": "Nope"},
        )
    assert exc.value.translation_key == "service_unknown_scene"
    with pytest.raises(ServiceValidationError) as exc2:
        await call(hass, "create_scene", {"name": "WC off"})
    assert exc2.value.translation_key == "service_scene_exists"
    assert env.reload().cdb.scenes == {1: [LIGHT_SWITCH], 2: []}


# ----------------------------------------------------------------------------- gateway sync (roadmap step 14)

GATEWAY_HOST = "junghome.local"
GATEWAY_DATA = {
    CONF_GATEWAY_HOST: GATEWAY_HOST,
    CONF_GATEWAY_TOKEN: "tok.en",
    CONF_GATEWAY_FINGERPRINT: "ab" * 32,
    CONF_GATEWAY_PIN_SOURCE: PIN_FROM_MESH,  # vouched for: the gateway is used without asking the mesh first
}


def sync_issue(hass: HomeAssistant, env: Env) -> ir.IssueEntry | None:
    """The entry's `gateway_sync_failed` repair issue (one per entry)."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(env.entry, ISSUE_GATEWAY_SYNC)
    )


async def use_gateway(hass: HomeAssistant, env: Env) -> None:
    """Make the entry a gateway entry; a new source is hub data, so wait for the reload it causes.

    That reload is the entry update's, not an action's: the states it took away are no action's doing.
    """
    hass.config_entries.async_update_entry(
        env.entry, data={**env.entry.data, **GATEWAY_DATA, CONF_SOURCE: "gateway"}
    )
    await settled(hass, env)
    env.transitions.seen.clear()


async def test_a_moved_gateway_is_followed_before_a_change(
    hass: HomeAssistant, env: Env
) -> None:
    """Unreachable at its stored host: the hub asks the gateway node where it is, and the check and the upload go
    there (`JungHomeHub.async_follow_gateway` has its own tests)."""
    await use_gateway(hass, env)
    uploads: list[str] = []
    fetched: list[str] = []
    held = json.loads(env.reload().share_json())  # the gateway holds what HA has

    async def follow(self: Any) -> bool:
        hass.config_entries.async_update_entry(
            env.entry, data={**env.entry.data, CONF_GATEWAY_HOST: "192.0.2.77"}
        )
        return True

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        fetched.append(self.host)
        if self.host == GATEWAY_HOST:
            raise GatewayUnreachable("GET project/junghome: ClientConnectorError")
        return held

    async def upload_project(self: JungHomeGatewayApi, export: dict[str, Any]) -> None:
        uploads.append(self.host)

    with (
        patch.object(type(env.hub), "async_follow_gateway", follow),
        patch.object(JungHomeGatewayApi, "fetch_project", fetch_project),
        patch.object(JungHomeGatewayApi, "upload_project", upload_project),
    ):
        await call(hass, "create_room", {"name": "Attic"})
    assert fetched[:2] == [GATEWAY_HOST, "192.0.2.77"]
    assert uploads == ["192.0.2.77"]


@pytest.fixture
def no_upload_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """No automatic retry of a failed upload: it would repair each failure a test sets up to see (the retries have
    their own tests in test_mesh_config.py)."""
    monkeypatch.setattr(mesh_config, "GATEWAY_UPLOAD_RETRIES", 0)


@pytest.mark.usefixtures("no_upload_retries")
async def test_every_write_back_is_handed_to_the_gateway(
    hass: HomeAssistant, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """With a gateway in the entry, a saved export goes to `POST /config` once the gateway is checked to still
    hold what HA last synced with it; a failure raises the repair issue and the `sync_gateway` action retries and
    clears it; without a gateway the action is refused. Unreachable, the mesh change and the file still go
    through, but nothing is uploaded blind — that risks erasing a change the app made in the meantime (CFG-05)."""
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "sync_gateway", {})
    assert exc.value.translation_key == "service_no_gateway"

    await use_gateway(hass, env)
    uploads: list[dict[str, Any]] = []
    failures: list[Exception] = []
    gateway_doc: dict[str, Any] | None = None  # None: unreachable

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        assert self.token == "tok.en"
        if gateway_doc is None:
            raise GatewayUnreachable("GET project/junghome: ClientConnectorError")
        return gateway_doc

    async def upload_project(self: JungHomeGatewayApi, export: dict[str, Any]) -> None:
        nonlocal gateway_doc
        assert self.host == GATEWAY_HOST
        assert self.token == "tok.en"
        if failures:
            raise failures.pop()
        uploads.append(export)
        gateway_doc = export  # the gateway now holds what HA uploaded

    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch_project),
        patch.object(JungHomeGatewayApi, "upload_project", upload_project),
    ):
        # unreachable: the local change still goes through, but nothing is uploaded blind
        await call(hass, "create_room", {"name": "Attic"})
        assert "Attic" in env.reload().user_groups().values()
        assert "Could not ask the gateway junghome.local for its export" in caplog.text
        assert uploads == []
        issue = sync_issue(hass, env)
        assert issue is not None
        assert issue.translation_placeholders["host"] == GATEWAY_HOST

        # the gateway becomes reachable, already holding exactly what HA has: the next change is uploaded
        gateway_doc = json.loads(env.reload().share_json())
        await call(hass, "create_room", {"name": "Cellar"})
        assert "Cellar" in env.reload().user_groups().values()
        assert len(uploads) == 1
        assert uploads[0]["network"]  # the share export, Base64 network and meta
        assert "Cellar" in json.dumps(uploads[0]["meta"], ensure_ascii=False)
        assert sync_issue(hass, env) is None
        # the gateway refuses: the mesh change and the file stand, the issue is raised, the call succeeds
        failures.append(GatewayError("POST config: HTTP 500 (boom)"))
        await call(hass, "create_room", {"name": "Loft"})
        assert "Loft" in env.reload().user_groups().values()
        issue = sync_issue(hass, env)
        assert issue is not None
        assert issue.translation_placeholders == {
            "host": GATEWAY_HOST,
            "error": "POST config: HTTP 500 (boom)",
        }
        assert "could not be handed to the gateway" in caplog.text
        # the manual retry fails the same way, as a translated error
        failures.append(GatewayError("POST config: HTTP 429"))
        with pytest.raises(HomeAssistantError) as err:
            await call(hass, "sync_gateway", {"config_entry_id": env.entry.entry_id})
        assert err.value.translation_key == "service_gateway_sync_failed"
        assert err.value.translation_placeholders == {
            "host": GATEWAY_HOST,
            "error": "POST config: HTTP 429",
        }
        # and succeeds once the gateway takes it
        await call(hass, "sync_gateway", {})
        assert len(uploads) == 2
        assert sync_issue(hass, env) is None


@pytest.mark.unavailable_ok
async def test_a_failed_upload_is_retried_across_the_reload_of_its_change(
    hass: HomeAssistant, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """A change the hub cannot follow in place reloads the entry right after it is saved (`model_update`), which
    replaces the hub and its configurator: the retry of its failed upload survives that, and goes through the new
    configurator with the export on disk."""
    await use_gateway(hass, env)
    uploads: list[dict[str, Any]] = []
    failures: list[Exception] = []
    gateway_doc = json.loads(env.reload().share_json())  # the gateway holds what HA has

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        return gateway_doc

    async def upload_project(self: JungHomeGatewayApi, export: dict[str, Any]) -> None:
        nonlocal gateway_doc
        if failures:
            raise failures.pop()
        uploads.append(export)
        gateway_doc = export

    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch_project),
        patch.object(JungHomeGatewayApi, "upload_project", upload_project),
    ):
        await call(hass, "create_room", {"name": "Attic"})
        assert len(uploads) == 1
        before = hass.data[svc.CONFIGURATORS][env.entry.entry_id]
        failures.append(GatewayError("POST config: HTTP 500 (boom)"))
        with patch.object(JungHomeHub, "model_refusal", return_value="a test"):
            await call(hass, "rename_room", {"room": "Attic", "new_name": "Loft"})
        await settled(hass, env)
        assert (
            hass.data[svc.CONFIGURATORS][env.entry.entry_id] is not before
        )  # reloaded
        await wait_until(hass, lambda: len(uploads) == 2, what="the retried upload")
        assert "Loft" in json.dumps(uploads[1]["meta"], ensure_ascii=False)
        assert sync_issue(hass, env) is None
        assert "could not be handed to the gateway" in caplog.text
        assert "retry 1 of 2" in caplog.text
        assert env.entry.entry_id not in hass.data[mesh_config.UPLOAD_RETRIES]

    # removing the entry drops a pending retry: nothing is left to hand over
    pending = hass.async_create_background_task(asyncio.Event().wait(), "retry")
    hass.data[mesh_config.UPLOAD_RETRIES][env.entry.entry_id] = pending
    assert await hass.config_entries.async_remove(env.entry.entry_id)
    await hass.async_block_till_done()
    assert pending.cancelled()
    assert env.entry.entry_id not in hass.data[mesh_config.UPLOAD_RETRIES]


@pytest.mark.unavailable_ok
async def test_sync_gateway_is_refused_without_a_usable_gateway(
    hass: HomeAssistant, env: Env
) -> None:
    """A malformed fingerprint means no client (nothing is sent); an export from a path or an upload is the
    user's file, whatever credentials the entry still carries."""
    hass.config_entries.async_update_entry(
        env.entry,
        data={
            **env.entry.data,
            **GATEWAY_DATA,
            CONF_SOURCE: "gateway",
            CONF_GATEWAY_FINGERPRINT: "zz",
        },
    )
    await settled(hass, env)  # a new source is hub data: the entry reloads
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "sync_gateway", {})
    assert exc.value.translation_key == "service_no_gateway"
    hass.config_entries.async_update_entry(
        env.entry,
        data={**env.entry.data, **GATEWAY_DATA, CONF_SOURCE: "path"},
    )
    await settled(hass, env)  # the source is hub data: the entry reloaded
    with pytest.raises(ServiceValidationError) as exc2:
        await call(hass, "sync_gateway", {})
    assert exc2.value.translation_key == "service_no_gateway"


def newer_export(path: Path, room: str) -> dict[str, Any]:
    """The export the app would have uploaded to the gateway: ours plus a room, stamped a minute later.

    A minute past ours, not past the clock: a save never stamps behind the file it was made from, so ours can
    already be a minute ahead after an earlier adoption of such an export.
    """
    newer = ProjectFile.load(path)
    newer.add_group(room)
    ours = datetime.fromisoformat(newer.loaded_timestamp)
    newer.touch(max(datetime.now(UTC), ours) + timedelta(minutes=1))
    return json.loads(newer.share_json())


@pytest.mark.usefixtures("no_upload_retries")
async def test_a_newer_export_on_the_gateway_is_adopted_before_a_change_and_blocks_sync(
    hass: HomeAssistant, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """The gateway is the source of truth for an entry set up from it: a change is planned on top of what the
    app changed since (never over it), and `sync_gateway` refuses to overwrite a newer export."""
    await use_gateway(hass, env)
    uploads: list[dict[str, Any]] = []
    gateway_holds: list[Any] = [json.loads(ProjectFile.load(env.path).share_json())]

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        doc = gateway_holds[-1]
        if isinstance(doc, Exception):
            raise doc
        assert isinstance(doc, dict)
        return doc

    async def upload_project(self: JungHomeGatewayApi, export: dict[str, Any]) -> None:
        uploads.append(export)

    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch_project),
        patch.object(JungHomeGatewayApi, "upload_project", upload_project),
    ):
        # the gateway holds what we hold: nothing to adopt
        await call(hass, "create_room", {"name": "Attic"})
        assert "adopted" not in caplog.text
        assert len(uploads) == 1
        # the app added a room since: the gateway's export is adopted first, the change made on top of it
        gateway_holds.append(newer_export(env.path, "From the app"))
        await call(hass, "create_room", {"name": "Cellar"})
        assert "holds a newer export" in caplog.text
        assert "adopted it before the change" in caplog.text
        rooms = list(env.reload().user_groups().values())
        assert "From the app" in rooms
        assert "Cellar" in rooms
        assert "Attic" in rooms
        assert len(uploads) == 2
        assert "From the app" in json.dumps(uploads[-1]["meta"], ensure_ascii=False)
        # the app changed the installation again and our upload is pending: `sync_gateway` must not clobber it
        gateway_holds.append(newer_export(env.path, "Garage"))
        with pytest.raises(HomeAssistantError) as err:
            await call(hass, "sync_gateway", {})
        assert err.value.translation_key == "service_gateway_export_newer"
        assert err.value.translation_placeholders["host"] == GATEWAY_HOST
        assert (
            err.value.translation_placeholders["gateway"]
            > (err.value.translation_placeholders["file"])
        )
        assert len(uploads) == 2
        # the same export again (our upload went through): the sync goes ahead
        gateway_holds.append(json.loads(ProjectFile.load(env.path).share_json()))
        await call(hass, "sync_gateway", {})
        assert len(uploads) == 3
        # an export of another mesh, or something that is no export: not adopted, said so
        other = newer_export(env.path, "Elsewhere")
        network = json.loads(base64.b64decode(other["network"]))
        network["meshUUID"] = "00000000-0000-0000-0000-000000000000"
        other["network"] = base64.b64encode(json.dumps(network).encode()).decode()
        gateway_holds.append(other)
        await call(hass, "create_room", {"name": "Loft"})
        assert "holds the export of another mesh" in caplog.text
        assert "Elsewhere" not in env.reload().user_groups().values()
        gateway_holds.append({"network": "not base64!", "meta": {}})
        await call(hass, "create_room", {"name": "Porch"})
        assert "does not parse" in caplog.text
        assert "Porch" in env.reload().user_groups().values()


async def test_a_wrong_or_foreign_config_entry_id_is_told_apart(
    hass: HomeAssistant, env: Env
) -> None:
    other = MockConfigEntry(domain="other", title="Other", data={})
    other.add_to_hass(hass)
    for bad in ("no-such-entry", other.entry_id):
        with pytest.raises(ServiceValidationError) as exc:
            await call(hass, "create_room", {"name": "X", "config_entry_id": bad})
        assert exc.value.translation_key == "service_unknown_entry"
        assert exc.value.translation_placeholders == {"id": bad}
    assert env.config_calls == []


async def test_entity_id_all_is_refused_with_its_own_message(
    hass: HomeAssistant, env: Env
) -> None:
    for service, data in (
        ("set_room", {"room": "WC"}),
        ("store_scene", {"scene": "1"}),
    ):
        with pytest.raises(ServiceValidationError) as exc:
            await call(hass, service, {"entity_id": "all", **data})
        assert exc.value.translation_key == "service_all_not_supported"
    assert env.config_calls == []


async def test_store_and_remove_from_scene_write_the_export_once_per_call(
    hass: HomeAssistant, env: Env
) -> None:
    """Two loads in one call: one plan, one export rewrite (`.bak` = the state before the call), one upload."""
    await use_gateway(hass, env)
    uploads: list[dict[str, Any]] = []
    gateway_doc = json.loads(ProjectFile.load(env.path).share_json())

    async def fetch_project(self: JungHomeGatewayApi) -> dict[str, Any]:
        return gateway_doc

    async def upload_project(self: JungHomeGatewayApi, export: dict[str, Any]) -> None:
        nonlocal gateway_doc
        uploads.append(export)
        gateway_doc = export  # the gateway now holds what HA uploaded

    ctl = entity_id(hass, "light", UID_LIGHT_CTL)
    socket = entity_id(hass, "switch", UID_SOCKET)
    bak = env.path.with_name(env.path.name + ".bak")
    with (
        patch.object(JungHomeGatewayApi, "fetch_project", fetch_project),
        patch.object(JungHomeGatewayApi, "upload_project", upload_project),
    ):
        await call(hass, "store_scene", {"entity_id": [ctl, socket], "scene": "2"})
        await settled(hass, env)
        assert env.reload().cdb.scenes[2] == [LIGHT_CTL, SOCKET]
        assert ProjectFile.load(bak).cdb.scenes[2] == []  # the state before the call
        assert len(uploads) == 1
        await call(
            hass, "remove_from_scene", {"entity_id": [ctl, socket], "scene": "2"}
        )
        await settled(hass, env)
        assert env.reload().cdb.scenes[2] == []
        assert ProjectFile.load(bak).cdb.scenes[2] == [LIGHT_CTL, SOCKET]
        assert len(uploads) == 2
        await call(
            hass,
            "set_room",
            {"entity_id": [ctl, socket], "room": "Attic", "create": True},
        )
        await settled(hass, env)
        assert "Attic" not in ProjectFile.load(bak).user_groups().values()
        assert len(uploads) == 3


async def test_export_network_answers_the_export_to_administrators_only(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_read_only_user: Any,
) -> None:
    """Review-3 N12: the export as the app's share file (default) or the mesh database — for a backup, or for the
    app to import with what Home Assistant changed. It carries every key, so only an administrator may ask."""
    share = await hass.services.async_call(
        DOMAIN, "export_network", {}, blocking=True, return_response=True
    )
    assert share is not None
    assert share["flavour"] == "share"
    assert share["mesh_uuid"] == init_integration.runtime_data.cdb.mesh_uuid
    assert set(share["export"]) >= {"meta", "network"}
    cdb = await hass.services.async_call(
        DOMAIN,
        "export_network",
        {"flavour": "cdb"},
        blocking=True,
        return_response=True,
    )
    assert cdb is not None
    assert "meshNetwork" in cdb["export"]
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "export_network",
            {},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )


# Valid data for every action that is for administrators only (review-4 W4-9): the schema is checked before the
# user is, so each call must pass it to meet the refusal
ADMIN_CALLS: dict[str, dict[str, Any]] = {
    "set_room": {"entity_id": "light.any", "room": "WC"},
    "add_to_room": {"entity_id": "light.any", "room": "WC"},
    "remove_from_room": {"entity_id": "light.any", "room": "WC"},
    "create_room": {"name": "Attic"},
    "rename_room": {"room": "WC", "new_name": "Loo"},
    "delete_room": {"room": "WC"},
    "assign_key": {"key_entity": "event.any", "room": "WC"},
    "clear_key": {"key_entity": "event.any"},
    "create_scene": {"name": "Evening"},
    "rename_scene": {"scene": "1", "new_name": "Night"},
    "store_scene": {"scene": "1", "entity_id": "light.any"},
    "remove_from_scene": {"scene": "1", "entity_id": "light.any"},
    "delete_scene": {"scene": "1"},
    "delete_unused_scenes": {},
    "sync_gateway": {},
    "create_schedule": {
        "trigger": "time",
        "time": "07:00",
        "action": "on",
        "entity_id": "light.any",
    },
    "update_schedule": {
        "slot": 0,
        "trigger": "time",
        "time": "07:00",
        "action": "on",
        "entity_id": "light.any",
    },
    "enable_schedule": {"slot": 0, "entity_id": "light.any"},
    "disable_schedule": {"slot": 0, "entity_id": "light.any"},
    "delete_schedule": {"slot": 0, "entity_id": "light.any"},
    "set_threshold": {"threshold": "switch_on", "entity_id": "switch.any"},
    "delete_threshold": {"entity_id": "switch.any"},
    "export_network": {},
    "add_device": {"address": "AA:BB:CC:DD:EE:FF", "name": "New"},
    "remove_device": {"device": "any"},
    "reset_pending_device": {"unicast": "0D20"},
    "locate_node": {"device": "any"},
    "approve_gateway_client": {"client": "ioBroker"},
}


async def test_rewiring_actions_are_for_administrators_only(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_read_only_user: Any,
) -> None:
    """Review-4 W4-9 (decision M8): every action that rewires, deletes or writes the export or the devices refuses
    a user who is no administrator; the reading ones stay open (`svc.USER_SERVICES`), and so do the dimming entity
    actions. A new action must take a side here."""
    registered = set(hass.services.async_services_for_domain(DOMAIN))
    dimming = {"start_dim", "stop_dim", "step_dim"}
    assert set(ADMIN_CALLS) == registered - svc.USER_SERVICES - dimming
    user = Context(user_id=hass_read_only_user.id)
    for name, data in ADMIN_CALLS.items():
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                name,
                data,
                blocking=True,
                context=user,
                return_response=name == "export_network",
            )
    found = await hass.services.async_call(
        DOMAIN,
        "find_new_devices",
        {},
        blocking=True,
        return_response=True,
        context=user,
    )
    assert found == {"devices": []}


# --------------------------------------------------------------------------- device renames (device_names.py)


def export_name(pf: ProjectFile, uuid: str, locations: list[int]) -> str:
    """The `meta.devices[]` name of the app device `(uuid, locations)`."""
    return next(
        d["name"]
        for d in pf.meta["devices"]
        if d["deviceId"]["nodeId"].lower() == uuid
        and d["deviceId"]["locationIds"] == locations
    )


RENAME_TASK = f"{DOMAIN} device rename"


async def renamed(hass: HomeAssistant, device: str, name: str | None) -> None:
    """Name a registry device as a user does, and let the rename it starts finish.

    The rename is a Home Assistant background task, not one of the entry's (W4-10), so `async_block_till_done`
    does not wait for it: it is awaited by its name (waiting for every background task would wait for the link).
    """
    dr.async_get(hass).async_update_device(device, name_by_user=name)
    await hass.async_block_till_done()
    renames = [
        t
        for t in hass._background_tasks
        if isinstance(t, asyncio.Task) and t.get_name() == RENAME_TASK
    ]
    if renames:
        await asyncio.gather(*renames)
    await hass.async_block_till_done()


async def test_a_device_named_in_home_assistant_is_renamed_in_the_export(
    hass: HomeAssistant, env: Env
) -> None:
    """The app's `UpdateDeviceName`: `meta.devices[].name` written, numbered when taken; the registry follows it."""
    registry = dr.async_get(hass)
    hub = env.hub
    mirror = device_id(hass, UID_LIGHT_SWITCH)
    await renamed(hass, mirror, "Mirror")
    assert export_name(env.reload(), NODE_LIGHT_SWITCH, [1]) == "Mirror"
    device = registry.async_get(mirror)
    assert device is not None
    assert (device.name, device.name_by_user) == ("Mirror", None)
    assert env.hub is hub  # a new name is not worth a reload
    assert env.config_calls == []  # nothing on air

    # a name another device has: the app's number
    ceiling = device_id(hass, UID_LIGHT_DIMMER)
    await renamed(hass, ceiling, "mirror")
    device = registry.async_get(ceiling)
    assert device is not None
    assert (device.name, device.name_by_user) == ("mirror 3", None)
    # a socket is a load too
    await renamed(hass, device_id(hass, UID_SOCKET), "Kettle")
    assert export_name(env.reload(), NODE_SOCKET, [1, 64]) == "Kettle"
    # a gang of keys is the app device of its keys
    await renamed(hass, device_id(hass, BUTTONS_WC), "Mirror keys")
    assert export_name(env.reload(), NODE_LIGHT_SWITCH, [64, 68]) == "Mirror keys"

    # the mesh device and an actuator's node device are no app devices: the export stays
    before = env.path.read_bytes()
    await renamed(hass, device_id(hass, NODE_ACTUATOR_ID), "Junction box")
    await renamed(hass, device_id(hass, f"mesh:{hub.cdb.mesh_uuid.lower()}"), "Home")
    assert env.path.read_bytes() == before


@pytest.mark.parametrize(
    ("export_source", "node", "unit"),
    [
        (FIXTURES / "MeshNetwork-rtr.json", NODE_THERMOSTAT, 0x0500),
        (FIXTURES / "MeshNetwork-detectors.json", NODE_MOTION, 0x0501),
    ],
)
async def test_a_thermostat_or_detector_is_renamed_through_its_node_device(
    hass: HomeAssistant, env: Env, node: str, unit: int
) -> None:
    """A room thermostat or detector is the app device its node device stands for (`node_device_info`)."""
    registry = dr.async_get(hass)
    device = device_id(hass, f"node:{node}")
    with patch.object(
        mesh_config.MeshConfigurator,
        "rename_device",
        AsyncMock(return_value="Hallway"),
    ) as rename:
        await renamed(hass, device, "Hallway")
    rename.assert_awaited_once_with(unit, "Hallway")
    entry = registry.async_get(device)
    assert entry is not None
    assert (entry.name, entry.name_by_user) == ("Hallway", None)


@pytest.mark.parametrize("export_source", [BLINDS_PATH])
async def test_a_blind_is_renamed_in_the_export(hass: HomeAssistant, env: Env) -> None:
    blind = next(b for b in env.hub.devices.blinds if b.name == "Kitchen blind")
    await renamed(hass, device_id(hass, blind.unique_id), "Patio blind")
    assert export_name(env.reload(), blind.node.uuid.lower(), [1]) == "Patio blind"


async def test_a_name_the_app_refuses_raises_a_repair_issue(
    hass: HomeAssistant, env: Env
) -> None:
    mirror = device_id(hass, UID_LIGHT_SWITCH)
    before = env.path.read_bytes()
    await renamed(hass, mirror, "50% off")
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(env.entry, ISSUE_DEVICE_NAME)
    )
    assert issue is not None
    assert issue.translation_placeholders == {"name": "50% off", "device": "WC mirror"}
    assert env.path.read_bytes() == before
    device = dr.async_get(hass).async_get(mirror)
    assert device is not None
    assert device.name_by_user == "50% off"  # Home Assistant's own name stays
    # the next accepted rename clears the issue
    await renamed(hass, mirror, "50%% off")
    assert export_name(env.reload(), NODE_LIGHT_SWITCH, [1]) == "50%% off"
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(env.entry, ISSUE_DEVICE_NAME)
        )
        is None
    )


async def test_a_name_longer_than_the_apps_rename_takes_raises_the_repair_issue(
    hass: HomeAssistant, env: Env
) -> None:
    before = env.path.read_bytes()
    await renamed(
        hass, device_id(hass, UID_LIGHT_SWITCH), "The mirror light over the WC sink"
    )
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(env.entry, ISSUE_DEVICE_NAME)
    )
    assert env.path.read_bytes() == before


async def test_a_rename_that_fails_otherwise_is_logged(
    hass: HomeAssistant, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    mirror = device_id(hass, UID_LIGHT_SWITCH)
    for error in (
        ServiceValidationError(
            translation_domain=DOMAIN, translation_key="service_entry_not_loaded"
        ),
        HomeAssistantError("the gateway answered nonsense"),
    ):
        with patch.object(
            mesh_config.MeshConfigurator, "rename_device", AsyncMock(side_effect=error)
        ):
            await renamed(hass, mirror, "Mirror")
        await renamed(hass, mirror, None)  # reset: nothing to write
    assert caplog.text.count("The new name of WC mirror was not written") == 2
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(env.entry, ISSUE_DEVICE_NAME)
        )
        is None
    )


async def test_a_rename_runs_outside_the_entrys_tasks(
    hass: HomeAssistant, env: Env
) -> None:
    """Review-4 W4-10: a rename that took the gateway's export over has the model follow it, which reloads the
    entry when it cannot follow in place. As one of the entry's tasks the unload would wait for it — 10 s, as it
    waits for that very reload — so it is a Home Assistant background task. Followed in place, the hub stays
    (review-4 D23)."""
    hub = env.hub
    mirror = device_id(hass, UID_LIGHT_SWITCH)

    async def adopting(
        self: mesh_config.MeshConfigurator, _address: int, name: str
    ) -> str:
        self.adopted = True
        return name

    background: list[str] = []
    entry_tasks: list[str] = []
    real_background = hass.async_create_background_task
    real_entry_task = env.entry.async_create_task

    def on_hass(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
        background.append(name)
        return real_background(target, name, *args, **kwargs)

    def on_entry(
        hass: HomeAssistant,
        target: Any,
        name: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        entry_tasks.append(name or "")
        return real_entry_task(hass, target, name, *args, **kwargs)

    with (
        patch.object(mesh_config.MeshConfigurator, "rename_device", adopting),
        patch.object(hass, "async_create_background_task", on_hass),
        patch.object(env.entry, "async_create_task", on_entry),
    ):
        await renamed(hass, mirror, "Mirror")
    assert RENAME_TASK in background
    assert RENAME_TASK not in entry_tasks
    await settled(hass, env)
    assert env.hub is hub  # the model followed the adopted export in place
    assert runs_the_export(env)


async def test_a_device_removed_during_its_rename_is_left_alone(
    hass: HomeAssistant, env: Env
) -> None:
    registry = dr.async_get(hass)
    mirror = device_id(hass, UID_LIGHT_SWITCH)

    async def rename_and_remove(_self: Any, _address: int, name: str) -> str:
        registry.async_remove_device(mirror)
        return name

    with patch.object(mesh_config.MeshConfigurator, "rename_device", rename_and_remove):
        await renamed(hass, mirror, "Mirror")
    assert registry.async_get(mirror) is None


# ------------------------------------------------------------------ cancelled calls (D12)


async def test_a_cancelled_call_that_recorded_reloads_and_stays_cancelled(
    hass: HomeAssistant, env: Env
) -> None:
    """D12: a call cancelled after its plan recorded what the mesh accepted (an automation in `mode: restart`)
    has the model follow the export like a stopped one — to its end, through a second cancellation meanwhile —
    and the cancellation is never swallowed."""
    reached = asyncio.Event()
    reloading = asyncio.Event()
    reload_may_end = asyncio.Event()
    reloads: list[str] = []
    plans = async_capture_events(hass, EVENT_PLAN)

    async def recorded_then_waits(configurator: Any) -> bool:
        configurator.recorded = True
        # one of the plan's four messages accepted (`_send` counts them)
        configurator.outcome.action = "junghome_ble.set_room"
        configurator.outcome.applied, configurator.outcome.total = 1, 4
        reached.set()
        await asyncio.Event().wait()
        return True  # pragma: no cover - cancelled before

    async def slow_follow(_hass: HomeAssistant, entry_id: str, **_kwargs: Any) -> None:
        reloading.set()
        await reload_may_end.wait()
        reloads.append(entry_id)

    with patch.object(svc, "async_follow_export", slow_follow):
        task = asyncio.ensure_future(
            svc._run(hass, env.entry.entry_id, recorded_then_waits, needs_link=False)
        )
        await reached.wait()
        task.cancel()
        await reloading.wait()
        task.cancel()  # a second cancellation while the reload runs
        await asyncio.sleep(0)
        assert not task.done()
        reload_may_end.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert reloads == [env.entry.entry_id]
    assert not svc._lock(hass, env.entry.entry_id).locked()
    # the logbook says how far it got (review-4 W I7)
    assert plans[-1].data["outcome"] == "cancelled"
    assert describers(hass)[EVENT_PLAN](plans[-1])["message"] == (
        "junghome_ble.set_room was cancelled after 1 of 4 messages; the mesh export records what was applied"
    )


async def test_a_cancelled_call_that_recorded_nothing_does_not_reload(
    hass: HomeAssistant, env: Env
) -> None:
    async def cancelled(_configurator: Any) -> bool:
        raise asyncio.CancelledError

    with (
        patch.object(
            svc, "async_follow_export", wraps=svc.async_follow_export
        ) as follow,
        pytest.raises(asyncio.CancelledError),
    ):
        await svc._run(hass, env.entry.entry_id, cancelled, needs_link=False)
    follow.assert_not_awaited()


async def test_no_reload_while_home_assistant_stops(
    hass: HomeAssistant, env: Env
) -> None:
    """Home Assistant stopping cancelled the call: the next start sets the entry up from the recorded export."""

    async def recorded(configurator: Any) -> bool:
        configurator.recorded = True
        raise asyncio.CancelledError

    with (
        patch.object(type(hass), "is_stopping", property(lambda _self: True)),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
        pytest.raises(asyncio.CancelledError),
    ):
        await svc._run(hass, env.entry.entry_id, recorded, needs_link=False)
    reload.assert_not_awaited()


@pytest.mark.unavailable_ok
async def test_setup_records_a_plan_a_crash_interrupted_and_sets_up_again(
    hass: HomeAssistant, env: Env
) -> None:
    """W I1: the journal of a plan Home Assistant was killed in the middle of (one of two steps accepted) is
    recorded into the export at the next setup, the entry is set up again from it, and a repair names the action;
    confirming the repair dismisses it."""
    step = mesh_config.ConfigStep(
        SOCKET,
        C.model_subscription_add(SOCKET, WC, "1000"),
        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
        change=ModelChange(SOCKET, "1000", WC, "subscribe"),
    )
    pending = mesh_config.ConfigStep(
        SOCKET,
        C.model_subscription_delete(SOCKET, LIVING, "1000"),
        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
        change=ModelChange(SOCKET, "1000", LIVING, "unsubscribe"),
    )
    journal = mesh_config.plan_journal(hass, env.entry.entry_id)
    await journal.async_save(
        {
            "action": "junghome_ble.set_room",
            "steps": [mesh_config._step_json(s) for s in (step, pending)],
            "accepted": 1,
            "prepare": None,
            "happened": None,
        }
    )
    hub = env.entry.runtime_data
    await hass.config_entries.async_reload(env.entry.entry_id)
    await hass.async_block_till_done()
    await settled(hass, env)
    assert WC in subs(env.reload(), SOCKET, "1000")
    # set up again from the recorded export: the device model has the step too
    assert env.entry.runtime_data is not hub
    element = env.entry.runtime_data.cdb.element(SOCKET)
    assert WC in element.subscriptions("1000")
    assert await journal.async_load() is None
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(env.entry, "plan_interrupted")
    )
    assert issue is not None
    assert issue.translation_placeholders == {
        "title": env.entry.title,
        "action": "junghome_ble.set_room",
        "accepted": "1",
        "total": "2",
    }
    flow = await repairs.async_create_fix_flow(hass, issue.issue_id, issue.data)
    assert isinstance(flow, ConfirmRepairFlow)


# ----------------------------------------------------------------------------- dry runs, responses, the logbook (review-4 W I3, W I6, W I7, W I9, U4-13)


async def respond(
    hass: HomeAssistant, service: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Call an action asking for its response."""
    response = await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )
    await hass.async_block_till_done()
    assert isinstance(response, dict)
    return response


async def test_a_dry_run_through_the_action_sends_writes_and_places_nothing(
    hass: HomeAssistant, env: Env
) -> None:
    """`dry_run`: the plan answered, nothing on the mesh, the export, the device's area or the logbook; the hub
    keeps the export it runs."""
    registry = dr.async_get(hass)
    switch = device_id(hass, UID_LIGHT_SWITCH)
    registry.async_update_device(switch, area_id=None)
    plans = async_capture_events(hass, EVENT_PLAN)
    before = env.path.read_bytes()
    target = {
        "entity_id": entity_id(hass, "light", UID_LIGHT_DIMMER),
        "room": "Kitchen",
    }
    for service, data in (
        ("set_room", target),
        ("add_to_room", target),
        (
            "set_room",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_SWITCH),
                "room": "Attic",
                "create": True,
            },
        ),
        ("remove_from_room", {**target, "room": "WC", "force": True}),
        ("delete_room", {"room": "WC"}),
        ("create_room", {"name": "Attic"}),
        ("create_scene", {"name": "Movie night"}),
        ("delete_scene", {"scene": "WC off"}),
        ("delete_scene", {"scene": "WC off", "force": True}),  # no `confirm` needed
        ("clear_key", {"key_entity": entity_id(hass, "event", UID_ROCKER_A)}),
        (
            "assign_key",
            {
                "key_entity": entity_id(hass, "event", UID_ROCKER_A),
                "target_entity": entity_id(hass, "light", UID_LIGHT_DIMMER),
            },
        ),
    ):
        response = await respond(hass, service, {**data, "dry_run": True})
        assert response["dry_run"] is True, service
        assert response["diff"], service
    assert response["steps"][0].startswith("0232 (")  # named by the device
    # without a response asked for, a dry run answers nothing and does nothing either
    await call(hass, "set_room", {**target, "dry_run": True})
    assert env.config_calls == []
    # nothing written to a load: only the hub's own reads go on (and its Time Set to all nodes, `node_clocks.py`)
    written = [
        text
        for dst, pdu in env.app_calls
        if dst != 0xFFFF
        and re.search(r"\b(?:Set|Store|Delete)\b", text := M.describe(pdu))
    ]
    assert written == []
    assert env.path.read_bytes() == before
    assert registry.async_get(switch).area_id is None  # type: ignore[union-attr]
    assert ar.async_get(hass).async_get_area_by_name("Attic") is None
    assert plans == []
    assert runs_the_export(env)


async def test_a_rewiring_action_answers_what_it_applied_and_logs_it(
    hass: HomeAssistant, env: Env
) -> None:
    """The response (`applied`, `total`, `recorded`, `nodes`), a logbook line in the integration's words, and the
    plan in the diagnostics' history (step texts, no key)."""
    plans = async_capture_events(hass, EVENT_PLAN)
    key = entity_id(hass, "event", UID_ROCKER_A)
    response = await respond(
        hass,
        "assign_key",
        {
            "key_entity": key,
            "target_entity": entity_id(hass, "light", UID_LIGHT_DIMMER),
        },
    )
    await settled(hass, env)
    assert response == {
        "applied": 8,
        "total": 8,
        "recorded": True,
        "nodes": ["0232 (Living room DALI)"],
    }
    assert plans[-1].data["outcome"] == "finished"
    assert plans[-1].data["message"] == "plan_key_device"
    line = describers(hass)[EVENT_PLAN](plans[-1])
    assert line == {
        "name": "JUNG HOME mesh test",
        "message": "0234 (Push-button 2-gang 0232 buttons A) now drives 0300 (WC ceiling); 8 messages",
    }
    history = list(mesh_config.plan_history(hass, env.entry.entry_id))
    assert history[-1]["outcome"] == "finished"
    assert history[-1]["steps"][0].startswith("0232: Config Model Publication Set")
    # the other summaries
    response = await respond(hass, "assign_key", {"key_entity": key, "room": "WC"})
    assert describers(hass)[EVENT_PLAN](plans[-1])["message"].startswith(
        "0234 (Push-button 2-gang 0232 buttons A) now drives room WC; "
    )
    await respond(hass, "assign_key", {"key_entity": key, "scene": "All off"})
    assert "now recalls scene 2" in describers(hass)[EVENT_PLAN](plans[-1])["message"]
    response = await respond(hass, "clear_key", {"key_entity": key})
    assert response["applied"] == response["total"] > 0
    assert "has no function now" in describers(hass)[EVENT_PLAN](plans[-1])["message"]
    light = entity_id(hass, "light", UID_LIGHT_DIMMER)
    await respond(hass, "add_to_room", {"entity_id": light, "room": "Kitchen"})
    assert describers(hass)[EVENT_PLAN](plans[-1])["message"].startswith(
        "0300 (WC ceiling) now in room Kitchen; "
    )
    await respond(hass, "remove_from_room", {"entity_id": light, "room": "Kitchen"})
    assert (
        "taken out of room Kitchen"
        in describers(hass)[EVENT_PLAN](plans[-1])["message"]
    )
    response = await respond(hass, "delete_room", {"room": "Kitchen"})
    assert describers(hass)[EVENT_PLAN](plans[-1])["message"].startswith(
        "Room Kitchen deleted; "
    )
    await respond(hass, "delete_scene", {"scene": "All off"})
    assert describers(hass)[EVENT_PLAN](plans[-1])["message"].startswith(
        "Scene 2 deleted; "
    )
    assert response["recorded"] is True
    assert (
        len(mesh_config.plan_history(hass, env.entry.entry_id)) == 5
    )  # the last few only
    await settled(hass, env)


async def test_a_stopped_plan_says_how_far_it_got(
    hass: HomeAssistant, env: Env
) -> None:
    """A refused step: the error carries what the response would have (`outcome_*`), the logbook says how far it
    got, the diagnostics keep it with its error."""
    plans = async_capture_events(hass, EVENT_PLAN)
    env.refuse[C.model_subscription_add(ROCKER_A, DIMMER_GROUP, "1003")] = 0x08
    with pytest.raises(HomeAssistantError) as exc:
        await respond(
            hass,
            "assign_key",
            {
                "key_entity": entity_id(hass, "event", UID_ROCKER_A),
                "target_entity": entity_id(hass, "light", UID_LIGHT_DIMMER),
            },
        )
    placeholders = exc.value.translation_placeholders or {}
    assert (
        placeholders["node"] == "0232 (Living room DALI)"
    )  # a name, not an address alone
    assert {k: v for k, v in placeholders.items() if k.startswith("outcome_")} == {
        "outcome_applied": "3",
        "outcome_total": "8",
        "outcome_recorded": "true",
        "outcome_nodes": "0232 (Living room DALI)",
    }
    assert plans[-1].data["outcome"] == "stopped"
    message = describers(hass)[EVENT_PLAN](plans[-1])["message"]
    assert message.startswith("junghome_ble.assign_key stopped after 3 of 8 messages: ")
    history = mesh_config.plan_history(hass, env.entry.entry_id)[-1]
    assert history["error"] == "service_config_refused"
    assert (history["applied"], history["total"]) == (3, 8)
    await settled(hass, env)


async def test_alternative_selectors_resolve_to_the_same_plan(
    hass: HomeAssistant, env: Env
) -> None:
    """Review-4 U4-13: the area named like a room stands for the room, a scene entity for its scene."""
    area = ar.async_get(hass).async_get_or_create("WC")
    light = entity_id(hass, "light", UID_LIGHT_DIMMER)
    key = entity_id(hass, "event", UID_ROCKER_A)
    scene = entity_id(hass, "scene", f"{env.hub.cdb.mesh_uuid.lower()}-scene-2")
    for service, by_name, by_selector in (
        (
            "set_room",
            {"entity_id": light, "room": "WC"},
            {"entity_id": light, "room_area": area.id},
        ),
        ("delete_room", {"room": "WC"}, {"room_area": area.id}),
        (
            "assign_key",
            {"key_entity": key, "room": "WC"},
            {"key_entity": key, "room_area": area.id},
        ),
        (
            "assign_key",
            {"key_entity": key, "scene": "All off"},
            {"key_entity": key, "scene_entity": scene},
        ),
        ("delete_scene", {"scene": "All off"}, {"scene_entity": scene}),
    ):
        named = await respond(hass, service, {**by_name, "dry_run": True})
        selected = await respond(hass, service, {**by_selector, "dry_run": True})
        assert named == selected, service
    assert env.config_calls == []
    # the real calls by selector
    await call(
        hass, "rename_scene", {"scene_entity": scene, "new_name": "Everything off"}
    )
    await settled(hass, env)
    assert env.reload().scene_names()[2] == "Everything off"
    await call(hass, "rename_room", {"room_area": area.id, "new_name": "Toilet"})
    await settled(hass, env)
    assert "Toilet" in env.reload().user_groups().values()
    await call(hass, "remove_from_scene", {"entity_id": light, "scene_entity": scene})
    await settled(hass, env)
    # what the selectors cannot stand for
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "set_room", {"entity_id": light, "room_area": "nowhere"})
    assert exc.value.translation_key == "service_unknown_area"
    for not_a_scene in (light, "scene.not_ours"):
        with pytest.raises(ServiceValidationError) as exc:
            await call(
                hass, "store_scene", {"entity_id": light, "scene_entity": not_a_scene}
            )
        assert exc.value.translation_key == "service_not_a_scene"
    with pytest.raises(vol.Invalid):
        await call(
            hass, "set_room", {"entity_id": light, "room": "WC", "room_area": area.id}
        )
    with pytest.raises(vol.Invalid):
        await call(
            hass,
            "delete_scene",
            {"scene_entity": scene, "config_entry_id": env.entry.entry_id},
        )


async def test_a_scene_entity_of_another_network_is_refused(
    hass: HomeAssistant, env: Env
) -> None:
    """A scene entity names its network's scene: a key or loads of another network are refused."""
    other_entry = MockConfigEntry(domain=DOMAIN, unique_id="other mesh")
    other_entry.add_to_hass(hass)
    other = er.async_get(hass).async_get_or_create(
        "scene", DOMAIN, "other-mesh-scene-2", config_entry=other_entry
    )
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "assign_key",
            {
                "key_entity": entity_id(hass, "event", UID_ROCKER_A),
                "scene_entity": other.entity_id,
            },
        )
    assert exc.value.translation_key == "service_target_other_network"
    with pytest.raises(ServiceValidationError) as exc:
        await call(
            hass,
            "remove_from_scene",
            {
                "entity_id": entity_id(hass, "light", UID_LIGHT_DIMMER),
                "scene_entity": other.entity_id,
            },
        )
    assert exc.value.translation_key == "service_target_other_network"
    assert env.config_calls == []
