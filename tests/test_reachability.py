"""Commands the app's way, reachability, and the link's state as Home Assistant shows it.

A load's own command is an acknowledged Set, retried and matched to the status the load publishes like the app's
`CommunicateWithDevice` (3 attempts x 3 s); one that none of its attempts gets an answer to fails the action with
`device_not_reachable` and marks the node unreachable at once (`MeshMessengerImpl$handleError$1`) — once the link
watchdog's probe showed the proxy still forwards: a proxy that stopped marks no node. The link
state sensor shows the app's connection states (`ObserveDeviceConnectionState`) and the screens before them; a Home
Assistant without any connectable Bluetooth scanner raises `bluetooth_unavailable` (`ObserveBluetoothState`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.junghome_ble import const, coordinator
from custom_components.junghome_ble import services as S
from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_BLUETOOTH_UNAVAILABLE,
    REQUEST_ATTEMPTS,
    SIGNAL_LINK_STATE,
    UNREACHABLE_RECHECK,
    UNREACHABLE_REPROBE,
)
from custom_components.junghome_ble.coordinator import JungHomeHub
from custom_components.junghome_ble.hub import liveness
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.client import ProxyClient
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode

from .conftest import (
    FakeProxyLink,
    load_sets,
    settle,
    setup_entry,
    wait_for_link,
    wait_until,
)
from .helpers import (
    LIGHT_CTL,
    LIGHT_CTL_TEMPERATURE,
    MESH_UUID,
    OUR_ADDRESS,
    SOCKET,
    SOCKET_SENSOR,
    UID_LIGHT_CTL,
    UID_SOCKET,
    ctl_status,
    entity_id,
    find_issue,
    onoff_status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

UID_LINK_STATE = f"{MESH_UUID}-link-state"
FAST_TIMEOUT = 0.01  # real seconds a request waits per attempt here, instead of `ProxyClient.request`'s 3 s


@pytest.fixture
def fast_requests() -> Generator[None]:
    """Every request that names no timeout (the hub's commands and state Gets) gives up after milliseconds."""
    timeout, *rest = ProxyClient.request.__defaults__ or ()
    assert (
        timeout == liveness.REQUEST_TIMEOUT
    )  # the hub relies on the library's default being the app's
    with patch.object(ProxyClient.request, "__defaults__", (FAST_TIMEOUT, *rest)):
        yield


@pytest.fixture(autouse=True)
def no_property_reads() -> Generator[None]:
    """Keep the config entities' reads and the loads' per-link lock reads (`config_entities.LoadLock`) out.

    Unanswered here, they hold an element's property reads for their 3 s attempts: the lock read that follows an
    unconfirmed command would wait behind them, past the verdict on the node.
    """
    with (
        patch(
            "custom_components.junghome_ble.config_entities.ConfigEntity._maybe_read"
        ),
        patch(
            "custom_components.junghome_ble.config_entities.LoadLock._maybe_read_lock"
        ),
    ):
        yield


def hub_of(entry: MockConfigEntry) -> JungHomeHub:
    return entry.runtime_data


async def switch_on(hass: HomeAssistant, eid: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
    )


def silent_for(link: FakeProxyLink, element: int, attempts: int) -> None:
    """Make `element` leave its first `attempts` acknowledged Sets unanswered (lost on air), then answer again."""
    original = link.write_gatt_char
    link.sets_silent.add(element)

    async def write(char: str, data: bytes, response: bool | None = None) -> None:
        await original(char, data, response)
        if sum(dst == element for dst, _ in load_sets(link)) >= attempts:
            link.sets_silent.discard(element)

    link.write_gatt_char = write  # type: ignore[method-assign]


# --------------------------------------------------------------------------- commands


async def test_a_load_command_is_retried_until_its_status_comes(
    hass: HomeAssistant,
    fast_requests: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The first Set is lost: the same PDU (same TID: the load applies it once) goes out again and is answered."""
    eid = entity_id(hass, "switch", UID_SOCKET)
    await settle(hass)
    fake_link.sent.clear()
    silent_for(fake_link, SOCKET, attempts=1)
    await switch_on(hass, eid)
    sets = load_sets(fake_link)
    assert len(sets) == 2
    assert sets[0] == sets[1] == (SOCKET, sets[0][1])
    assert sets[0][1] == M.generic_onoff_set(True, tid=sets[0][1][3], transition=0)
    assert (
        hass.states.get(eid).state == STATE_ON
    )  # what the socket published answering it
    assert not hub_of(init_integration).unreachable


async def test_an_unanswered_command_marks_the_node_unreachable_and_fails_the_action(
    hass: HomeAssistant,
    fast_requests: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """None of the app's three attempts answered: the action fails naming the entity, the link watchdog asks the
    proxy whether it still forwards anything, and as it does, the node is unreachable (its entities unavailable)."""
    hub = hub_of(init_integration)
    eid = entity_id(hass, "switch", UID_SOCKET)
    await settle(hass)
    fake_link.sent.clear()
    fake_link.sets_silent.add(SOCKET)
    keep_alive = AsyncMock(
        return_value=True
    )  # the proxy still forwards: the link stays
    with patch.object(JungHomeHub, "_keep_alive", keep_alive):
        with pytest.raises(HomeAssistantError) as exc:
            await switch_on(hass, eid)
        await wait_until(hass, lambda: bool(hub.unreachable), what="the verdict")
    keep_alive.assert_awaited_once()
    assert exc.value.translation_key == "device_not_reachable"
    assert exc.value.translation_placeholders == {"entity": eid}
    assert len(load_sets(fake_link)) == REQUEST_ATTEMPTS
    assert len(set(load_sets(fake_link))) == 1  # the same PDU each time
    node = hub.cdb.node_by_addr(SOCKET)
    assert node is not None
    assert hub.unreachable == {node.unicast}
    assert hass.states.get(eid).state == STATE_UNAVAILABLE
    assert "did not answer a request (3 attempts in 9 s)" in caplog.text
    assert hub.connected
    # a status from the socket (its own publication, a re-probe's answer): reachable again
    fake_link.sets_silent.discard(SOCKET)
    fake_link.inject(SOCKET, 0xC000, onoff_status(False))
    await settle(hass)
    assert not hub.unreachable
    assert hass.states.get(eid).state != STATE_UNAVAILABLE


async def test_commands_a_silent_proxy_leaves_unanswered_mark_no_node(
    hass: HomeAssistant,
    fast_requests: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The proxy stopped forwarding: every command in that window goes unanswered, and the watchdog's probe too. The
    actions fail, but the proxy is to blame, not the nodes: the link is dropped and no node is marked unreachable."""
    hub = hub_of(init_integration)
    socket = entity_id(hass, "switch", UID_SOCKET)
    light = entity_id(hass, "light", UID_LIGHT_CTL)
    await settle(hass)
    fake_link.sets_silent.update({SOCKET, LIGHT_CTL})

    both_failed = asyncio.Event()

    async def silent_proxy() -> bool:
        """Nothing comes through the proxy; answered once both commands gave up (a drop would fail the other one)."""
        await both_failed.wait()
        return False

    keep_alive = AsyncMock(side_effect=silent_proxy)
    with patch.object(JungHomeHub, "_keep_alive", keep_alive):
        results = await asyncio.gather(
            *(
                hass.services.async_call(
                    domain, SERVICE_TURN_ON, {ATTR_ENTITY_ID: eid}, blocking=True
                )
                for domain, eid in ((SWITCH_DOMAIN, socket), ("light", light))
            ),
            return_exceptions=True,
        )
        both_failed.set()
        await wait_until(
            hass, lambda: "dropping the link" in caplog.text, what="the drop"
        )
        await settle(hass)
    for result in results:
        assert isinstance(result, HomeAssistantError)
        assert result.translation_key == "device_not_reachable"
    assert "to a command nor to a keep-alive Get; dropping the link" in caplog.text
    assert not hub.unreachable
    assert not hub._unanswered
    assert "did not answer a request" not in caplog.text


async def test_a_link_lost_during_the_probe_charges_no_node(
    hass: HomeAssistant,
    fast_requests: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The link goes while the probe is out: no verdict on the proxy nor on the node, which the next link asks again."""
    hub = hub_of(init_integration)
    await settle(hass)
    fake_link.sets_silent.add(SOCKET)

    async def lost() -> bool:
        fake_link.drop_link()
        await settle(hass)
        return False

    keep_alive = AsyncMock(side_effect=lost)
    with patch.object(JungHomeHub, "_keep_alive", keep_alive):
        with pytest.raises(HomeAssistantError):
            await switch_on(hass, entity_id(hass, "switch", UID_SOCKET))
        await wait_until(hass, lambda: keep_alive.await_count > 0, what="the probe")
        await settle(hass)
    assert "dropping the link" not in caplog.text
    assert not hub.unreachable


async def test_a_node_heard_from_during_its_command_stays_reachable(
    hass: HomeAssistant,
    fast_requests: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The app completes a pending request on a User Property Status (`D1 27 05`) from the element; that status
    answers nothing here (it is also what nodes publish unsolicited) — but a node that sent it is there.

    A node that answered something but not the command may be locked (`config_entities.LoadLock`): its lock is read
    (here unanswered, quickly) before the action fails as unreachable."""
    hub = hub_of(init_integration)
    eid = entity_id(hass, "switch", UID_SOCKET)
    await settle(hass)
    fake_link.sets_silent.add(SOCKET)
    original = fake_link.write_gatt_char

    async def publish_meanwhile(
        char: str, data: bytes, response: bool | None = None
    ) -> None:
        sets = len(load_sets(fake_link))
        await original(char, data, response)
        if len(load_sets(fake_link)) > sets:  # an attempt of the command, not a Get
            fake_link.inject(
                SOCKET_SENSOR,
                0xC000,
                M.vendor_property_status("user", 0x5001, b"\x00\x00"),
            )

    fake_link.write_gatt_char = publish_meanwhile  # type: ignore[method-assign]
    keep_alive = AsyncMock(return_value=True)
    sent = len(fake_link.sent)
    with (
        patch.object(JungHomeHub, "_keep_alive", keep_alive),
        patch.object(const, "PROPERTY_READ_TIMEOUT", FAST_TIMEOUT),
    ):
        with pytest.raises(HomeAssistantError) as exc:
            await switch_on(hass, eid)
        await wait_until(
            hass, lambda: SOCKET in hub.liveness.recheck, what="the verdict"
        )
    keep_alive.assert_awaited_once()
    assert exc.value.translation_key == "device_not_reachable"
    assert (OUR_ADDRESS, SOCKET, M.vendor_property_get("admin", 0x0009)) in (
        fake_link.sent[sent:]
    )
    assert len(load_sets(fake_link)) == REQUEST_ATTEMPTS  # not taken for the answer
    assert not hub.unreachable
    assert hass.states.get(eid).state != STATE_UNAVAILABLE
    assert (
        SOCKET in hub.liveness.recheck
    )  # asked again later (the node's primary), with the full budget


LOCK_GET = M.vendor_property_get("admin", 0x0009)
LOCKED = M.vendor_property_status("admin", 0x0009, bytes.fromhex("02010000"))


async def test_a_locked_load_that_leaves_a_command_unanswered_stays_reachable(
    hass: HomeAssistant,
    fast_requests: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """F4-2: a load known to be locked may ignore a Set altogether; its silence to the command is the lock's, not a
    sign it is gone: no unreachable mark, a state Get later (whose silence would count). Unverified on air."""
    hub = hub_of(init_integration)
    await settle(hass)
    hub.element_state(SOCKET).note_lock(bytes.fromhex("02010000"))
    fake_link.sets_silent.add(SOCKET)
    with (
        patch.object(JungHomeHub, "_keep_alive", AsyncMock(return_value=True)),
        patch.object(hub.liveness, "_schedule_recheck") as recheck,
    ):
        with pytest.raises(TimeoutError):
            await hub.set_onoff(SOCKET, True)  # past the entity, which would refuse it
        await wait_until(hass, lambda: recheck.called, what="the verdict")
    recheck.assert_called_once_with(SOCKET, SOCKET, "switch", UNREACHABLE_RECHECK)
    assert not hub.unreachable


async def test_a_command_a_load_answers_with_its_old_state_is_reported_as_locked(
    hass: HomeAssistant,
    fast_requests: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """A lock nobody had read yet: the socket answers the Set with its old state — with no other request out to it,
    that answer counts for the Set (review-4 D32) — so the entity reads its lock and reports the action refused for
    the lock, rather than a success that changed nothing, and nothing is marked."""
    hub = hub_of(init_integration)
    eid = entity_id(hass, "switch", UID_SOCKET)
    await settle(hass)

    def reply(dst: int, access: bytes) -> bytes | None:
        if dst != SOCKET:
            return None
        if access == LOCK_GET:
            return LOCKED
        if decode_opcode(access)[0] == M.GEN_ONOFF_SET:
            return onoff_status(False)  # the lock holds the socket off
        return None

    fake_link.app_reply = reply
    with patch.object(JungHomeHub, "_keep_alive", AsyncMock(return_value=True)):
        with pytest.raises(ServiceValidationError) as exc:
            await switch_on(hass, eid)
        await settle(hass)
    assert exc.value.translation_key == "load_locked"
    assert len(load_sets(fake_link)) == 1
    assert hub.load_locked(SOCKET)
    assert hass.states.get(eid).attributes["locked"] is True
    assert not hub.unreachable


async def test_a_colour_temperature_command_is_accounted_to_the_light(
    hass: HomeAssistant,
    fast_requests: None,
    answering_mesh: FakeProxyLink,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
) -> None:
    """The temperature element's Set unanswered: the light's node is unreachable, re-probed with the light's CTL Get."""
    hub = hub_of(init_integration)
    await settle(hass)
    fake_link.inject(
        LIGHT_CTL, 0xC044, ctl_status(30000, 5000)
    )  # on: a temperature alone goes to its element
    await settle(hass)
    fake_link.sets_silent.add(LIGHT_CTL_TEMPERATURE)
    with (
        patch.object(JungHomeHub, "_keep_alive", AsyncMock(return_value=True)),
        patch.object(hub.liveness, "_schedule_recheck") as recheck,
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "light",
                SERVICE_TURN_ON,
                {
                    ATTR_ENTITY_ID: entity_id(hass, "light", UID_LIGHT_CTL),
                    "color_temp_kelvin": 3000,
                },
                blocking=True,
            )
        await wait_until(hass, lambda: bool(hub.unreachable), what="the verdict")
    assert [dst for dst, _ in load_sets(fake_link)][-REQUEST_ATTEMPTS:] == [
        LIGHT_CTL_TEMPERATURE
    ] * REQUEST_ATTEMPTS
    recheck.assert_called_once_with(LIGHT_CTL, LIGHT_CTL, "ctl", UNREACHABLE_REPROBE)
    assert hub.unreachable == {LIGHT_CTL}


async def test_a_scene_state_the_load_does_not_answer_is_reported(
    hass: HomeAssistant,
) -> None:
    """`store_scene` with a state sets the load first; a load that answers none of the attempts is named."""
    hub = MagicMock()
    hub.set_onoff = AsyncMock(side_effect=TimeoutError("no response from 0172"))
    device = MagicMock(address=SOCKET, unique_id="not-registered", kind="switch")
    with pytest.raises(HomeAssistantError) as exc:
        await S._apply_state(hass, hub, device, V.Action(V.ACTION_SWITCH, on=True))
    assert exc.value.translation_key == "device_not_reachable"
    assert exc.value.translation_placeholders == {"entity": "0172"}


# --------------------------------------------------------------------------- link state and Bluetooth


async def test_link_state_follows_the_link_and_bluetooth(
    hass: HomeAssistant,
    fast_requests: None,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    answering_mesh: FakeProxyLink,
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """connecting → updating (the connect-time refresh) → connected; a lost link, a search, no Bluetooth at all (a
    repair issue), a failed attempt; the sensor is a diagnostic, and shows each of them."""
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, UID_LINK_STATE, disabled_by=None)
    seen: list[str] = []
    # a `@callback`: run in the loop as the signal is sent, so it reads the state that was signalled. A plain
    # function would run in the executor, read the state whenever its thread got there and, under a busy `-n auto`
    # run, record the next state twice instead of a short-lived one (`failed` read as the `connecting` after it).
    async_dispatcher_connect(
        hass,
        SIGNAL_LINK_STATE.format(mock_config_entry.entry_id),
        callback(lambda: seen.append(mock_config_entry.runtime_data.link_state)),
    )
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    hub = hub_of(mock_config_entry)
    await wait_until(hass, lambda: hub.link_state == "connected", what="connected")
    assert seen == ["connecting", "updating", "connected"]
    eid = entity_id(hass, "sensor", UID_LINK_STATE)
    assert hass.states.get(eid).state == "connected"

    # the link goes and no proxy node is in range: disconnected, then searching
    infos = mock_bluetooth_env["infos"]
    mock_bluetooth_env["infos"] = []
    fake_link.drop_link()
    await wait_until(hass, lambda: hub.link_state == "searching", what="searching")
    assert seen[3:] == ["disconnected", "searching"]
    assert hass.states.get(eid).state == "searching"
    assert find_issue(hass, ISSUE_BLUETOOTH_UNAVAILABLE) is None

    # no connectable scanner at all: the app's "Bluetooth is off", a repair issue
    mock_bluetooth_env["scanners"] = 0
    hub._link_lost.set()  # wakes the search at once
    await wait_until(
        hass, lambda: hub.link_state == "bluetooth_off", what="no Bluetooth"
    )
    issue = find_issue(hass, ISSUE_BLUETOOTH_UNAVAILABLE)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"title": mock_config_entry.title}
    assert hass.states.get(eid).state == "bluetooth_off"
    assert "No connectable Bluetooth adapter or proxy is available" in caplog.text
    hub._link_lost.set()  # still none: raised once
    await settle(hass)
    assert caplog.text.count("No connectable Bluetooth adapter") == 1

    # Bluetooth back, a proxy node advertising, the first attempt failing
    mock_bluetooth_env["scanners"] = 1
    mock_bluetooth_env["infos"] = infos
    fake_link.connect_errors.append(BleakError("no free connection slot"))
    seen.clear()
    mock_bluetooth_env["callbacks"][0](infos[0], BluetoothChange.ADVERTISEMENT)
    await wait_for_link(hass, mock_config_entry)
    await wait_until(hass, lambda: hub.link_state == "connected", what="connected")
    assert seen == ["connecting", "failed", "connecting", "updating", "connected"]
    assert find_issue(hass, ISSUE_BLUETOOTH_UNAVAILABLE) is None


async def test_the_link_state_sensor_is_an_enabled_diagnostic(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Review-4 H I-5: the mesh's visible health indicator is on by default (new registrations)."""
    entry = er.async_get(hass).async_get(entity_id(hass, "sensor", UID_LINK_STATE))
    assert entry is not None
    assert entry.disabled_by is None
    assert entry.entity_category is not None
    assert entry.translation_key == "link_state"


async def test_the_wait_for_a_connection_is_bleak_retry_connectors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Not the app's 5 s: an ESPHome proxy waits for a free slot and gives up after its own establishment timeout
    (at least 10 s), so the hub keeps the library's per-attempt timeout and caps the attempts at two."""
    hub = hub_of(init_integration)
    info = hub.visible_proxies()[0]
    with (
        patch.object(
            coordinator, "establish_connection", AsyncMock(side_effect=BleakError("x"))
        ) as establish,
        pytest.raises(BleakError),
    ):
        await hub._connect_to(info)
    assert establish.call_args.kwargs["max_attempts"] == 2
    assert "timeout" not in establish.call_args.kwargs


async def test_an_unreachable_load_logs_one_line_and_no_library_warning(
    hass: HomeAssistant,
    fast_requests: None,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review-4 H4-7: the library logged a WARNING per unanswered attempt, so every dead load wrote several lines at
    each hub start. The attempts are DEBUG now; the hub's one WARNING per node that goes unreachable stays."""
    caplog.set_level(logging.DEBUG, logger="jhmesh")
    await setup_entry(
        hass, mock_config_entry
    )  # the fake answers no state Get: every load stays silent
    await wait_for_link(hass, mock_config_entry)
    hub = hub_of(mock_config_entry)
    await wait_until(hass, lambda: bool(hub.unreachable), what="a verdict")
    await settle(hass)
    attempts = [
        r
        for r in caplog.records
        if r.name == "jhmesh" and r.getMessage().startswith("no response from")
    ]
    assert attempts  # the attempts are still there to read, at DEBUG
    assert {r.levelno for r in attempts} == {logging.DEBUG}
    assert not [
        r for r in caplog.records if r.name == "jhmesh" and r.levelno >= logging.WARNING
    ]
    verdicts = [
        r for r in caplog.records if "did not answer a request" in r.getMessage()
    ]
    assert {r.levelno for r in verdicts} == {logging.WARNING}
    assert len(verdicts) == len(hub.unreachable)  # one line per node
