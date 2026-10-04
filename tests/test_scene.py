"""Scene platform: scenes stored in the mesh."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.components.light import ATTR_TRANSITION
from homeassistant.components.scene import DOMAIN as SCENE_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.junghome_ble import scene as scene_platform
from custom_components.junghome_ble.const import (
    DOMAIN,
    EVENT_SCENE_RECALLED,
    SCENE_RECALL_WINDOW,
    SIGNAL_SCENES,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.pdu import ALL_NODES, encode_opcode
from custom_components.junghome_ble.scene import JungHomeScene
from custom_components.junghome_ble.sensor import scene_list_sensors

from .conftest import FakeProxyLink, settle
from .helpers import (
    LIGHT_CTL,
    LIGHT_DIMMER,
    LIGHT_OUT1,
    LIGHT_OUT2,
    LIGHT_SWITCH,
    MESH_UUID,
    OUR_ADDRESS,
    ROCKER_B,
    SOCKET,
    entity_id,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_entities(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    for number, eid, name in (
        (1, "scene.wc_off", "WC off"),
        (2, "scene.all_off", "All off"),
    ):
        assert entity_id(hass, "scene", f"{MESH_UUID}-scene-{number}") == eid
        state = hass.states.get(eid)
        assert state is not None
        assert state.state == STATE_UNKNOWN
        assert state.attributes["friendly_name"] == name
        assert state.attributes["scene_number"] == number
        entry = registry.async_get(eid)
        assert entry is not None
        assert entry.device_id is None  # network-wide, not tied to a device


async def test_activate(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.sent.clear()
    await hass.services.async_call(
        SCENE_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "scene.all_off"}, blocking=True
    )
    src, dst, pdu = fake_link.sent[-1]
    assert (src, dst) == (
        OUR_ADDRESS,
        ALL_NODES,
    )  # one broadcast, unacknowledged, as the app does
    assert pdu == M.scene_recall(2, ack=False, tid=pdu[4])
    assert len(fake_link.sent) == 1
    assert (
        hass.states.get("scene.all_off").state != STATE_UNKNOWN
    )  # the activation time


async def test_a_transition_is_ignored_until_the_probe(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """F4-1: until the on-air probe showed JUNG nodes fade a recall (`SCENE_TRANSITIONS`), a `transition` is
    dropped silently: the Recall keeps its bytes, nothing raises."""
    fake_link.sent.clear()
    await hass.services.async_call(
        SCENE_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "scene.all_off", ATTR_TRANSITION: 3},
        blocking=True,
    )
    ((_, dst, pdu),) = fake_link.sent
    assert dst == ALL_NODES
    assert pdu == M.scene_recall(2, ack=False, tid=pdu[4])


async def test_activate_with_a_transition(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4-1: once recalls fade, HA's `transition` goes into the one Recall every node takes; without one the
    Recall is as before."""
    monkeypatch.setattr(scene_platform, "SCENE_TRANSITIONS", True)
    fake_link.sent.clear()
    await hass.services.async_call(
        SCENE_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "scene.all_off", ATTR_TRANSITION: 3},
        blocking=True,
    )
    await hass.services.async_call(
        SCENE_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "scene.wc_off"}, blocking=True
    )
    (_, dst, faded), (_, _, plain) = fake_link.sent
    assert dst == ALL_NODES
    assert faded == M.scene_recall(2, ack=False, tid=faded[4], transition=0x1E)
    assert faded[5:] == bytes([0x1E, 0])  # 30 x 100 ms, no delay
    assert plain == M.scene_recall(1, ack=False, tid=plain[4])


async def test_send_failure_raises(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.write_error = ConnectionError("proxy disconnected")
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            SCENE_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: "scene.wc_off"},
            blocking=True,
        )
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "send_failed"


async def test_availability_follows_the_link(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    mock_bluetooth_env: dict[str, Any],
) -> None:
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await settle(hass)
    assert hass.states.get("scene.wc_off").state == STATE_UNAVAILABLE


async def test_members_of_the_same_name_are_both_listed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Two loads the user gave one name (a "Ceiling" in two rooms) stay two members, told apart by address."""
    hub = init_integration.runtime_data
    hub.cdb.scenes[2] = [LIGHT_CTL, SOCKET, 0x0999]
    for address in (LIGHT_CTL, SOCKET):
        hub.devices.by_address[address].name = "Ceiling"
    hub.scene_actions[2] = {SOCKET: None}
    async_dispatcher_send(hass, SIGNAL_SCENES.format(init_integration.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get("scene.all_off").attributes["members"] == {
        f"Ceiling ({LIGHT_CTL:04X})": "stored",
        f"Ceiling ({SOCKET:04X})": "stored",
        "0999": "stored",
    }


async def test_members_are_the_channels_holding_an_action(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A two-channel node's scene is stored on its primary for either channel: once read, each channel holding an
    action is a member (the second one too), and a channel without one is not; before the read it is "stored"."""
    hub = init_integration.runtime_data
    hub.cdb.scenes[2] = [LIGHT_OUT1]
    for address, name in ((LIGHT_OUT1, "Kitchen 1"), (LIGHT_OUT2, "Kitchen 2")):
        hub.devices.by_address[address].name = name
    async_dispatcher_send(hass, SIGNAL_SCENES.format(init_integration.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get("scene.all_off").attributes["members"] == {
        "Kitchen 1": "stored"
    }

    on = V.Action(V.ACTION_SWITCH, on=True)
    hub.scene_actions[2] = {LIGHT_OUT1: on, LIGHT_OUT2: on}
    async_dispatcher_send(hass, SIGNAL_SCENES.format(init_integration.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get("scene.all_off").attributes["members"] == {
        "Kitchen 1": "switch on",
        "Kitchen 2": "switch on",
    }

    hub.scene_actions[2] = {LIGHT_OUT1: None, LIGHT_OUT2: on}
    async_dispatcher_send(hass, SIGNAL_SCENES.format(init_integration.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get("scene.all_off").attributes["members"] == {
        "Kitchen 2": "switch on"
    }


# ----------------------------------------------------------------------------- current scene and outside recalls

APP = 0x0001  # the app's provisioner node (no product id), as in the decoded settings session
MEMBER_GROUP = (
    0xC016  # a member's element group: where the nodes publish their Scene Status
)


def scene_status(current: int, status: int = 0) -> bytes:
    """The Scene Status the nodes publish after a recall (on air: `5E 00 0100`)."""
    return (
        encode_opcode(M.SCENE_STATUS) + bytes([status]) + current.to_bytes(2, "little")
    )


async def test_a_recall_home_assistant_did_not_hear_is_reported_by_the_members_status(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The members publish a Scene Status after every recall: one of a scene no recall reported is the scene event
    (without a source, naming the member), the scene entity's activation, and the member's current scene. The
    other members' statuses of the same recall repeat nothing."""
    hub = init_integration.runtime_data
    events = async_capture_events(hass, EVENT_SCENE_RECALLED)
    assert hass.states.get("scene.wc_off").state == STATE_UNKNOWN
    assert hass.states.get("scene.wc_off").attributes["active_members"] == []
    fake_link.inject(LIGHT_SWITCH, MEMBER_GROUP, scene_status(1))
    await hass.async_block_till_done()
    assert [e.data for e in events] == [
        {
            "scene": 1,
            "name": "WC off",
            "reported_by": "0148",
            "entry_id": init_integration.entry_id,
            "entity_id": "scene.wc_off",
        }
    ]
    assert hub.states[LIGHT_SWITCH].scene == 1
    state = hass.states.get("scene.wc_off")
    assert state.state != STATE_UNKNOWN  # the activation time
    assert state.attributes["active_members"] == ["WC mirror"]
    fake_link.inject(SOCKET, 0xC061, scene_status(1))  # another member, same recall
    await hass.async_block_till_done()
    assert len(events) == 1
    assert hub.states[SOCKET].scene == 1
    # the load changed state after the recall: its Scene Server clears the current scene
    fake_link.inject(LIGHT_SWITCH, MEMBER_GROUP, scene_status(0))
    await hass.async_block_till_done()
    assert hass.states.get("scene.wc_off").attributes["active_members"] == []
    assert len(events) == 1
    # later than the window, the same scene again is a new recall
    hub._scene_recalls[1] -= SCENE_RECALL_WINDOW
    fake_link.inject(LIGHT_SWITCH, MEMBER_GROUP, scene_status(1))
    await hass.async_block_till_done()
    assert len(events) == 2


async def test_statuses_of_a_heard_recall_repeat_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A key's, the app's and our own recall already fired the scene event: the statuses that follow it do not."""
    events = async_capture_events(hass, EVENT_SCENE_RECALLED)
    fake_link.inject(ROCKER_B, ALL_NODES, M.scene_recall(1, ack=False, tid=3))
    fake_link.inject(LIGHT_SWITCH, MEMBER_GROUP, scene_status(1))
    await hass.async_block_till_done()
    assert [e.data.get("source") for e in events] == ["0235"]
    await hass.services.async_call(
        SCENE_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "scene.all_off"}, blocking=True
    )
    fake_link.inject(LIGHT_SWITCH, MEMBER_GROUP, scene_status(2))
    await hass.async_block_till_done()
    assert [e.data.get("source") for e in events] == ["0235", "0D00"]


async def test_a_keys_recall_is_published_without_its_event_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A key whose event entity is disabled (no listener) still publishes its recall, the hub does (review-4 H4-2):
    with the key as the source, and the members' Scene Status that follows adds nothing."""
    hub = init_integration.runtime_data
    events = async_capture_events(hass, EVENT_SCENE_RECALLED)
    with patch.dict(hub.gestures._event_listeners, {ROCKER_B: []}):
        fake_link.inject(ROCKER_B, ALL_NODES, M.scene_recall(1, ack=False, tid=4))
        fake_link.inject(LIGHT_SWITCH, MEMBER_GROUP, scene_status(1))
        await hass.async_block_till_done()
    assert [(e.data.get("source"), e.data.get("reported_by")) for e in events] == [
        ("0235", None)
    ]


async def test_our_own_activation_is_recorded_once(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The entity records its own activation; the scene event our recall publishes does not record it again."""
    with patch.object(
        JungHomeScene, "_async_record_activation", autospec=True
    ) as record:
        await hass.services.async_call(
            SCENE_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: "scene.all_off"},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert record.call_count == 1


async def test_the_apps_recall_is_a_scene_event_and_an_activation(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A recall from a node that is no key (the app, the gateway) is published with its sender as the source; the
    firmware's second copy (same TID) is not a second recall."""
    events = async_capture_events(hass, EVENT_SCENE_RECALLED)
    recall = M.scene_recall(2, ack=False, tid=67)
    fake_link.inject(APP, ALL_NODES, recall)
    fake_link.inject(APP, ALL_NODES, recall)
    await hass.async_block_till_done()
    assert [e.data for e in events] == [
        {
            "scene": 2,
            "name": "All off",
            "source": "0001",
            "entry_id": init_integration.entry_id,
            "entity_id": "scene.all_off",
        }
    ]
    assert hass.states.get("scene.all_off").state != STATE_UNKNOWN
    assert hass.states.get("scene.wc_off").state == STATE_UNKNOWN  # another scene's


async def test_status_answers_and_refusals_fire_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A Scene Status sent to us answers our Scene Get: state only. A refused one, or one too short, changes nothing;
    a Scene Register Status keeps the register and, when it succeeded, the current scene."""
    hub = init_integration.runtime_data
    events = async_capture_events(hass, EVENT_SCENE_RECALLED)
    fake_link.inject(LIGHT_SWITCH, OUR_ADDRESS, scene_status(1))
    await hass.async_block_till_done()
    assert hub.states[LIGHT_SWITCH].scene == 1
    fake_link.inject(LIGHT_SWITCH, MEMBER_GROUP, scene_status(2, status=2))
    fake_link.inject(
        LIGHT_SWITCH, MEMBER_GROUP, encode_opcode(M.SCENE_STATUS) + b"\x00"
    )
    await hass.async_block_till_done()
    assert hub.states[LIGHT_SWITCH].scene == 1
    assert events == []
    register = encode_opcode(M.SCENE_REGISTER_STATUS) + bytes.fromhex(
        "00 0200 0100 0200"
    )
    fake_link.inject(SOCKET, OUR_ADDRESS, register)
    await hass.async_block_till_done()
    assert hub.scene_registers[SOCKET] == (1, 2)
    assert hub.states[SOCKET].scene == 2
    full = encode_opcode(M.SCENE_REGISTER_STATUS) + bytes.fromhex("01 0000 0100")
    fake_link.inject(SOCKET, OUR_ADDRESS, full)
    fake_link.inject(
        SOCKET, OUR_ADDRESS, encode_opcode(M.SCENE_REGISTER_STATUS) + b"\x00"
    )
    await hass.async_block_till_done()
    assert hub.scene_registers[SOCKET] == (1,)
    assert hub.states[SOCKET].scene == 2  # a refusal's current scene is not taken
    assert events == []


async def test_answers_to_the_apps_and_the_gateways_scene_get_fire_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The proxy filter lets through what the nodes send to the app (0001) and the gateway (00DC): a Scene Status
    answering their Scene Get is state, not a recall; only a publication to a group is."""
    hub = init_integration.runtime_data
    events = async_capture_events(hass, EVENT_SCENE_RECALLED)
    fake_link.inject(LIGHT_SWITCH, APP, scene_status(1))
    fake_link.inject(SOCKET, 0x00DC, scene_status(2))
    await hass.async_block_till_done()
    assert hub.states[LIGHT_SWITCH].scene == 1
    assert hub.states[SOCKET].scene == 2
    assert events == []
    assert hass.states.get("scene.wc_off").state == STATE_UNKNOWN


async def test_current_scenes_are_read_after_the_connection(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """Every element holding a scene register gets a Scene Get; one that stays silent, or a lost link, is left."""
    hub = init_integration.runtime_data
    fake_link.sent.clear()
    fake_link.current_scenes[LIGHT_SWITCH] = 1
    await hub.refresh.get_current_scenes()
    await hass.async_block_till_done()
    assert fake_link.sent == [(OUR_ADDRESS, LIGHT_SWITCH, M.scene_get())]
    assert hub.states[LIGHT_SWITCH].scene == 1
    # an answer the firmware publishes to its group while our Get waits is still an answer: no recall
    events = async_capture_events(hass, EVENT_SCENE_RECALLED)

    async def published(addr: int, *_args: Any, **_kwargs: Any) -> None:
        fake_link.inject(addr, MEMBER_GROUP, scene_status(2))
        await hass.async_block_till_done()

    with patch.object(hub.proxy, "request", side_effect=published):
        await hub.refresh.get_current_scenes()
    assert hub.states[LIGHT_SWITCH].scene == 2
    assert events == []
    hub.cdb.scenes = {}
    fake_link.sent.clear()
    await hub.refresh.get_current_scenes()
    assert fake_link.sent == []  # no scene stored anywhere: nothing to ask
    hub.cdb.scenes = {1: [LIGHT_CTL]}
    with patch.object(hub.proxy, "request", side_effect=TimeoutError):
        await hub.refresh.get_current_scenes()
    with patch.object(hub.proxy, "request", side_effect=ConnectionError("gone")):
        await hub.refresh.get_current_scenes()
    assert hub.element_state(LIGHT_CTL).scene is None


# ----------------------------------------------------------------------------- timer scenes


async def test_timer_scenes_have_no_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The app's scene list leaves its timer scenes out: so do the scene entities, and one an earlier version
    registered is removed."""
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("scene", DOMAIN, f"{MESH_UUID}-scene-1")
    with patch(
        "custom_components.junghome_ble.jhmesh.devices.TIMER_SCENE_PREFIX", "WC"
    ):  # "WC off" named as the app names a timer's scene
        assert await hass.config_entries.async_reload(init_integration.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get_entity_id("scene", DOMAIN, f"{MESH_UUID}-scene-1") is None
    assert hass.states.get("scene.wc_off") is None
    assert hass.states.get("scene.all_off") is not None


# ----------------------------------------------------------------------------- a device's scenes


async def test_scenes_of_a_load(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The export's members, narrowed on a channel with its own list once the list was read; timer scenes and
    unknown numbers are not named by the *Scenes* sensor."""
    hub = init_integration.runtime_data
    assert hub.scenes_of(LIGHT_SWITCH) == [1]
    assert hub.scenes_of(0x0999) == []
    hub.cdb.scenes = {1: [LIGHT_OUT1], 2: [LIGHT_OUT1], 5: [LIGHT_OUT1]}
    assert hub.scenes_of(LIGHT_OUT2) == [
        1,
        2,
        5,
    ]  # the shared register, before any list was read
    hub.scene_lists_read.add(LIGHT_OUT2)
    hub.scene_actions = {2: {LIGHT_OUT2: None}, 5: {LIGHT_OUT2: None}}
    assert hub.scenes_of(LIGHT_OUT2) == [2, 5]
    hub.devices.scenes[1].timer = True  # "All off" (2)
    sensor = next(s for s in scene_list_sensors(hub) if s.address == LIGHT_OUT2)
    assert sensor.native_value == 0  # 2 is a timer's scene, 5 no scene of the app
    assert sensor.extra_state_attributes == {"mesh_address": "0401", "scenes": []}
    hub.scene_actions[1] = {LIGHT_OUT2: None}
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["scenes"] == ["WC off"]
    # a *Scenes* sensor is for a load whose node holds a register: not for a key, nor a node without one
    assert {s.address for s in scene_list_sensors(hub)} == {
        LIGHT_SWITCH,
        LIGHT_CTL,
        SOCKET,
        LIGHT_DIMMER,
        LIGHT_OUT1,
        LIGHT_OUT2,
    }
    rocker = hub.cdb.element(ROCKER_B)
    assert rocker is not None
    rocker.models.append("05271017")
    socket = hub.cdb.element(SOCKET)
    assert socket is not None
    socket.models.remove("1204")
    assert SOCKET not in {s.address for s in scene_list_sensors(hub)}
    assert ROCKER_B not in {s.address for s in scene_list_sensors(hub)}
