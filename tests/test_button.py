"""Button platform: write-only trigger properties (the blind reference run), Identify and Clear faults per node,
Reset consumption per metering socket."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.junghome_ble import config_entities as C
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.client import ProxyClient

from . import property_helpers as ph
from .conftest import FakeProxyLink, settle, setup_entry, wait_for_link, wait_until
from .helpers import (
    LIGHT_SWITCH,
    NODE_ACTUATOR,
    NODE_LIGHT_CTL,
    NODE_LIGHT_SWITCH,
    NODE_SOCKET,
    OUR_ADDRESS,
    PROPERTY_POWER_ON_TIME,
    PROPERTY_TOTAL_ENERGY,
    SOCKET,
    SOCKET_SENSOR,
    UID_LIGHT_SWITCH,
    UID_SOCKET,
    admin_property_status,
    device_name_of,
    entity_id,
    onoff_status,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .property_helpers import PropertyMesh

fast_timeouts, mesh, init_with_mesh = ph.fast_timeouts, ph.mesh, ph.init_with_mesh


@pytest.fixture
def fast_requests() -> Generator[None]:
    """Make an unanswered Health request give up after milliseconds instead of `ProxyClient.request`'s 3 s."""
    original = ProxyClient.request

    async def quick(self: ProxyClient, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", 0.01)
        return await original(self, *args, **kwargs)

    with patch.object(ProxyClient, "request", quick):
        yield


PID_REFERENCE_RUN = 0x110D
UID_REFERENCE_RUN = f"{UID_LIGHT_SWITCH}-reference_run"


async def test_no_trigger_button_without_a_blind(
    hass: HomeAssistant, init_with_mesh: MockConfigEntry
) -> None:
    """The reference run belongs to blind loads; the fixture network has none, so only the per-node buttons exist."""
    registry = er.async_get(hass)
    buttons = [
        e
        for e in er.async_entries_for_config_entry(registry, init_with_mesh.entry_id)
        if e.domain == "button"
    ]
    assert {e.translation_key for e in buttons} == {
        "identify",
        "clear_faults",
        "reset_consumption",
    }
    # one Identify and one Clear faults per provisioned node (the phone has none), Reset consumption on the socket
    assert len(buttons) == 13


async def test_identify_button_sits_where_its_led_is(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A push-button's Identify joins its keys' device, a socket's the socket device, a puck's stays on the node.

    The LED that blinks is the one people look at: the WC mirror's insert blinks the key in the wall, the boiler
    socket its own LED, while the 2-channel actuator in a junction box has nothing visible.
    """
    wc = entity_id(hass, "button", f"node:{NODE_LIGHT_SWITCH}-identify")
    assert wc == "button.wc_wc_mirror_button_identify"
    assert device_name_of(hass, wc) == "WC mirror button"
    rocker = entity_id(hass, "button", f"node:{NODE_LIGHT_CTL}-identify")
    assert device_name_of(hass, rocker) == "Living room rocker"  # the node's first gang
    socket = entity_id(hass, "button", f"node:{NODE_SOCKET}-identify")
    assert socket == "button.kitchen_boiler_identify"
    assert device_name_of(hass, socket) == "Boiler"  # the area is in the entity id only
    puck = entity_id(hass, "button", f"node:{NODE_ACTUATOR}-identify")
    assert device_name_of(hass, puck) == "2-channel actuator 0400"


async def test_identify_button_asks_the_node_for_attention(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_requests: None,
) -> None:
    """Pressing Identify sends Health Attention Set (10 s) to the node's primary element; no answer → an error
    of its own (an Attention Set is never re-checked, so the generic text's promise would be false)."""
    uid = f"node:{NODE_LIGHT_SWITCH}-identify"
    eid = entity_id(hass, "button", uid)
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert entry.original_device_class == "identify"
    assert hass.states.get(eid).state == STATE_UNKNOWN

    async def answer(char: str, data: bytes, response: bool | None = None) -> None:
        before = len(fake_link.sent)
        await original(char, data, response)
        for src, dst, access in fake_link.sent[before:]:
            if access == M.health_attention_set(10):
                fake_link.inject(
                    dst, src, M.encode_opcode(M.HEALTH_ATTENTION_STATUS) + b"\x0a"
                )

    original = fake_link.write_gatt_char
    fake_link.write_gatt_char = answer  # type: ignore[method-assign]
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert fake_link.sent[-1] == (OUR_ADDRESS, LIGHT_SWITCH, M.health_attention_set(10))

    fake_link.write_gatt_char = original  # type: ignore[method-assign]
    with (
        patch(
            "custom_components.junghome_ble.jhmesh.client.asyncio.sleep",
            return_value=None,
        ),
        pytest.raises(HomeAssistantError, match="did not answer") as exc,
    ):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
        )
    assert exc.value.translation_key == "identify_no_answer"
    assert "checked again" not in str(exc.value)
    fake_link.write_error = OSError("GATT write failed")
    with pytest.raises(HomeAssistantError, match="could not be sent"):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
        )
    fake_link.write_error = None


async def test_clear_faults_button_empties_the_register_and_reads_it_back(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_link: FakeProxyLink,
    fast_requests: None,
) -> None:
    """Pressing Clear faults sends the acknowledged Health Fault Clear, then a Get whose answer updates the Fault entity."""
    hub = init_integration.runtime_data
    uid = f"node:{NODE_LIGHT_SWITCH}-clear-faults"
    eid = entity_id(hass, "button", uid)
    assert eid == "button.wc_wc_mirror_button_clear_faults"
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert device_name_of(hass, eid) == "WC mirror button"
    fault = "binary_sensor.wc_wc_mirror_button_fault"
    fake_link.inject(
        LIGHT_SWITCH,
        OUR_ADDRESS,
        M.encode_opcode(M.HEALTH_FAULT_STATUS) + bytes([0, 0x27, 0x05, 0x81]),
    )
    await hass.async_block_till_done()
    assert hass.states.get(fault).state == STATE_ON

    async def answer(char: str, data: bytes, response: bool | None = None) -> None:
        before = len(fake_link.sent)
        await original(char, data, response)
        for src, dst, access in fake_link.sent[before:]:
            if access == M.health_fault_get():
                fake_link.inject(
                    dst,
                    src,
                    M.encode_opcode(M.HEALTH_FAULT_STATUS) + bytes([0, 0x27, 0x05]),
                )

    original = fake_link.write_gatt_char
    fake_link.write_gatt_char = answer  # type: ignore[method-assign]
    fake_link.sent.clear()
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    assert fake_link.sent == [
        (OUR_ADDRESS, LIGHT_SWITCH, M.health_fault_clear()),
        (OUR_ADDRESS, LIGHT_SWITCH, M.health_fault_get()),
    ]
    assert hass.states.get(fault).state == STATE_OFF
    assert hub.states[LIGHT_SWITCH].faults == ()

    # the Clear went out but the node did not answer the read-back: said so, the register is re-read next link
    fake_link.write_gatt_char = original  # type: ignore[method-assign]
    with (
        patch(
            "custom_components.junghome_ble.jhmesh.client.asyncio.sleep",
            return_value=None,
        ),
        pytest.raises(HomeAssistantError, match="did not answer"),
    ):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
        )
    fake_link.write_error = OSError("GATT write failed")
    with pytest.raises(HomeAssistantError, match="could not be sent"):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
        )


@pytest.fixture
def blind_on_the_switch_load() -> Any:
    """Let the reference-run property apply to the WC mirror's switch load, as if it were a blind insert."""
    with patch.dict(C.LOAD_KINDS, {PID_REFERENCE_RUN: frozenset({"switch"})}):
        yield


async def test_press_sends_the_trigger_set(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], blind_on_the_switch_load: None,
) -> None:  # fmt: skip
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "button", DOMAIN, UID_REFERENCE_RUN, disabled_by=None
    )  # a setup-assistant step, not on the first page
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    eid = entity_id(hass, "button", UID_REFERENCE_RUN)
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["property_id"] == "0x110D"
    entry = registry.async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG
    assert (
        LIGHT_SWITCH,
        PID_REFERENCE_RUN,
    ) not in mesh.gets  # a button has nothing to read

    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    # the app's start: `C3 27 05 [0D 11][03][01]`
    assert mesh.link.sent[-1] == (
        OUR_ADDRESS,
        LIGHT_SWITCH,
        M.vendor_property_set("admin", PID_REFERENCE_RUN, b"\x01"),
    )
    assert mesh.sets == [(LIGHT_SWITCH, PID_REFERENCE_RUN, b"\x01")]
    assert hass.states.get(eid).state != STATE_UNKNOWN  # pressed


async def test_disabled_by_default_and_send_failure(
    hass: HomeAssistant, mesh: PropertyMesh, mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any], fast_sleep: list[float], blind_on_the_switch_load: None,
    fake_link: FakeProxyLink,
) -> None:  # fmt: skip
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    registry = er.async_get(hass)
    eid = entity_id(hass, "button", UID_REFERENCE_RUN)
    entry = registry.async_get(eid)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(eid) is None

    registry.async_update_entity(eid, disabled_by=None)
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass, 200)
    assert hass.states.get(eid) is not None
    fake_link.write_error = ConnectionError("proxy disconnected")
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
        )
    assert exc.value.translation_key == "send_failed"


UID_RESET_CONSUMPTION = f"{UID_SOCKET}-reset_consumption"
ZERO = bytes(4)  # the app's reset value: a 4-byte 0 for both counters


@pytest.fixture
async def init_reset_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_requests: None,
) -> MockConfigEntry:
    """The fixture network set up and connected, with the socket's Reset consumption button enabled by the user."""
    er.async_get(hass).async_get_or_create(
        "button", DOMAIN, UID_RESET_CONSUMPTION, disabled_by=None
    )
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    # the connect-time refresh ends with an energy poll whose counter Gets nobody answers here: `settle` stops at
    # their (real-time) timeouts, and a test clearing `fake_link.sent` must not see them land afterwards
    hub = mock_config_entry.runtime_data
    await wait_until(
        hass,
        lambda: (
            hub.lifecycle.task("refresh") is None
            or hub.lifecycle.task("refresh").done()
        ),
        what="the connect-time refresh",
    )
    # nobody answered the refresh either (every request gives up after milliseconds here), so every node counts as
    # unreachable (`Liveness.missed_answer`); a status from the socket brings it back, as any message from it does
    fake_link.inject(SOCKET, 0xC000, onoff_status(False))
    await hass.async_block_till_done()
    return mock_config_entry


def answer_counters(
    fake_link: FakeProxyLink, set_value: bytes | None, get_value: bytes | None
) -> None:
    """Let the socket answer a counter's Admin Property Set with `set_value` and its Get with `get_value`.

    None leaves that request unanswered (a Set whose state changed may only be published, not answered by unicast).
    The Status comes from the element asked, for the property asked, like the socket's own.
    """
    original = fake_link.write_gatt_char

    async def answer(char: str, data: bytes, response: bool | None = None) -> None:
        before = len(fake_link.sent)
        await original(char, data, response)
        for src, dst, access in fake_link.sent[before:]:
            for pid in (PROPERTY_TOTAL_ENERGY, PROPERTY_POWER_ON_TIME):
                if access == M.generic_property_set("admin", pid, ZERO):
                    value = set_value
                elif access == M.generic_property_get("admin", pid):
                    value = get_value
                else:
                    continue
                if value is not None:
                    fake_link.inject(dst, src, admin_property_status(pid, value))

    fake_link.write_gatt_char = answer  # type: ignore[method-assign]


async def press(hass: HomeAssistant, eid: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
    )


async def test_reset_consumption_button_is_a_hidden_diagnostic_of_the_socket(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Only the metering socket has one, under the socket device; off by default, like the counters it zeroes."""
    eid = entity_id(hass, "button", UID_RESET_CONSUMPTION)
    assert eid == "button.kitchen_boiler_reset_consumption"
    assert device_name_of(hass, eid) == "Boiler"
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(eid) is None


async def test_reset_consumption_zeroes_both_counters_like_the_app(
    hass: HomeAssistant, init_reset_enabled: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """The app's two acknowledged Sets, in its order: 0x006D on the main element, then 0x006A on the meter element.

    The lifetime total 0x0072 (the Energy sensor) is left alone. Each Set's Status zeroes the cached counter.
    """
    hub = init_reset_enabled.runtime_data
    eid = entity_id(hass, "button", UID_RESET_CONSUMPTION)
    st = hub.element_state(SOCKET)
    st.energy_wh, st.energy_resettable_wh, st.power_on_hours = 210198, 210040, 4321
    answer_counters(fake_link, set_value=ZERO, get_value=None)
    fake_link.sent.clear()
    await press(hass, eid)
    assert fake_link.sent == [
        (OUR_ADDRESS, SOCKET, bytes.fromhex("486d0003") + ZERO),
        (
            OUR_ADDRESS,
            SOCKET_SENSOR,
            bytes.fromhex("486a0003") + ZERO,  # `[6A 00][access 3][0 u32]`
        ),
    ]
    assert (st.energy_wh, st.energy_resettable_wh, st.power_on_hours) == (
        210198,
        0,
        0,
    )
    assert hass.states.get(eid).state != STATE_UNKNOWN  # pressed


async def test_reset_consumption_reads_back_an_unanswered_set(
    hass: HomeAssistant, init_reset_enabled: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    """A Set the socket only publishes (no unicast Status) is checked with a Get of the same counter."""
    hub = init_reset_enabled.runtime_data
    answer_counters(
        fake_link, set_value=None, get_value=b"\x00\x00\x00"
    )  # hours: uint24 on air
    fake_link.sent.clear()
    with patch(
        "custom_components.junghome_ble.jhmesh.client.asyncio.sleep", return_value=None
    ):
        await press(hass, entity_id(hass, "button", UID_RESET_CONSUMPTION))
    resets = {
        M.generic_property_set("admin", pid, ZERO)
        for pid in (PROPERTY_TOTAL_ENERGY, PROPERTY_POWER_ON_TIME)
    }
    assert [
        (dst, access)
        for _, dst, access in fake_link.sent
        if access in resets or access[:2] == bytes.fromhex("822d")  # Admin Get
    ] == [
        (SOCKET, M.generic_property_set("admin", PROPERTY_POWER_ON_TIME, ZERO)),
        (SOCKET, M.generic_property_get("admin", PROPERTY_POWER_ON_TIME)),
        (SOCKET_SENSOR, M.generic_property_set("admin", PROPERTY_TOTAL_ENERGY, ZERO)),
        (SOCKET_SENSOR, M.generic_property_get("admin", PROPERTY_TOTAL_ENERGY)),
    ]
    st = hub.states[SOCKET]
    assert (st.energy_resettable_wh, st.power_on_hours) == (0, 0)


@pytest.mark.parametrize(
    ("set_value", "get_value", "key"),
    [
        # the socket keeps counting from where it was: refused
        pytest.param(
            (210040).to_bytes(4, "little"),
            None,
            "reset_consumption_refused",
            id="refused",
        ),
        # a Status with no value: the element does not have the counter
        pytest.param(b"", None, "reset_consumption_refused", id="no-value"),
        # neither the Set nor the read-back answered
        pytest.param(None, None, "no_answer", id="silent"),
    ],
)
async def test_reset_consumption_reports_a_counter_not_reset(
    hass: HomeAssistant,
    init_reset_enabled: MockConfigEntry,
    fake_link: FakeProxyLink,
    set_value: bytes | None,
    get_value: bytes | None,
    key: str,
) -> None:
    """A counter the socket did not zero is an error; the second counter is not written after the first failed."""
    answer_counters(fake_link, set_value=set_value, get_value=get_value)
    fake_link.sent.clear()
    with (
        patch(
            "custom_components.junghome_ble.jhmesh.client.asyncio.sleep",
            return_value=None,
        ),
        pytest.raises(HomeAssistantError) as exc,
    ):
        await press(hass, entity_id(hass, "button", UID_RESET_CONSUMPTION))
    assert exc.value.translation_key == key
    assert (
        OUR_ADDRESS,
        SOCKET_SENSOR,
        M.generic_property_set("admin", PROPERTY_TOTAL_ENERGY, ZERO),
    ) not in fake_link.sent


async def test_reset_consumption_without_a_link(
    hass: HomeAssistant, init_reset_enabled: MockConfigEntry, fake_link: FakeProxyLink
) -> None:
    fake_link.write_error = OSError("GATT write failed")
    with pytest.raises(HomeAssistantError) as exc:
        await press(hass, entity_id(hass, "button", UID_RESET_CONSUMPTION))
    assert exc.value.translation_key == "send_failed"
    fake_link.write_error = None
