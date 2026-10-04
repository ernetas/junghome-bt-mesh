"""What a node tells about itself: the identity block, the LBC version blocks and the time role (A18, A10 parity).

The app reads SIG 0x0011 / 0x001A / 0x0010 and the time role on every opening of a device page (on air
settings session); the reader asks the version once per hub and restart of the node, the rest (the time role too)
once for good, and the node's device shows the manufacturer name and hardware revision.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from custom_components.junghome_ble.config_entities import PropertyReader
from custom_components.junghome_ble.const import (
    NODE_INFO_TIME_ROLE,
)
from custom_components.junghome_ble.coordinator import (
    NODE_VERSIONS_STORAGE_VERSION,
    NodeInfoStore,
)
from custom_components.junghome_ble.entity import node_identifier, update_node_device
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.cdb import Element, Node
from custom_components.junghome_ble.jhmesh.pdu import encode_opcode
from custom_components.junghome_ble.jhmesh.properties import (
    SIG_HARDWARE_REVISION,
    SIG_MANUFACTURER_NAME,
    SIG_SOFTWARE_VERSION,
)

from . import property_helpers as ph
from .conftest import settle, wait_for_link, wait_until
from .helpers import OUR_ADDRESS
from .test_binary_sensor import RELAY_MOTION, make_detectors_entry, start_detectors

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeProxyLink

fast_timeouts = ph.fast_timeouts
# the vectors of the app's settings session (on air), NUL-padded as the nodes send them
HARDWARE_REVISION = b"10000000" + bytes(8)
MANUFACTURER_NAME = b"Albrecht Jung GmbH & Co.KG" + bytes(10)
RTR = 0x0A


def sig_status(pid: int, value: bytes) -> bytes:
    """A Generic Manufacturer Property Status of SIG `pid` with `value`."""
    return (
        encode_opcode(M.GEN_MANU_PROP_STATUS)
        + pid.to_bytes(2, "little")
        + b"\x01"
        + value
    )


# --------------------------------------------------------------------------- the reads


def _node(pid: int, *, time_server: bool = True) -> Node:
    node = Node(
        "AAAAAAAA-0000-0000-0000-000000000001", "synthetic", 0x0700, bytes(16), pid
    )
    node.elements = [
        Element(0x0700, 0x0001, ["0000"], node),
        Element(0x0701, 0x0001, ["1201"] if time_server else [], node),
    ]
    return node


def _reader(answers: dict[bytes, bytes | None], known: dict[str, bytes] | None = None):
    """A reader over a hub whose node answers each Get in `answers` with those parameters (None: silence)."""
    sent: list[tuple[int, bytes]] = []
    remembered: dict[str, bytes] = {}

    async def request(addr: int, pdu: bytes, opcode: int, **kwargs: Any) -> Any:
        sent.append((addr, pdu))
        params = answers.get(pdu)
        if params is None:
            raise TimeoutError
        return SimpleNamespace(params=params)

    hub = SimpleNamespace(
        proxy=SimpleNamespace(request=AsyncMock(side_effect=request)),
        node_info=lambda unicast: known or {},
        remember_node_info=lambda unicast, name, raw: remembered.__setitem__(name, raw),
    )
    return PropertyReader(hub), sent, remembered  # type: ignore[arg-type]


def sig_get(pid: int) -> bytes:
    return M.generic_property_get("manufacturer", pid)


def lbc_get(pid: int) -> bytes:
    return M.vendor_property_get("manufacturer", pid)


async def test_everything_is_asked_on_the_first_link() -> None:
    """The version, the identity block, the LBC version blocks (0x0005 on a room thermostat) and the time role (to
    the element with the Time Setup Server); the LBC values and the role are kept here, the SIG ones by the hub."""
    answers = {
        sig_get(SIG_SOFTWARE_VERSION): b"\x1a\x00\x0102020002",
        sig_get(SIG_HARDWARE_REVISION): b"\x10\x00\x01" + HARDWARE_REVISION,
        sig_get(SIG_MANUFACTURER_NAME): b"\x11\x00\x01" + MANUFACTURER_NAME,
        lbc_get(0x0003): b"\x03\x00\x01\x0d\x02\x01\x00",
        lbc_get(0x0004): b"\x04\x00\x01\x01\x00\x00\x00",
        lbc_get(0x0005): b"\x05\x00\x01",  # no value: the node does not have it
        M.time_role_get(): b"\x03",
    }
    reader, sent, remembered = _reader(answers)
    await reader._read_version(_node(RTR))
    assert sent == [
        (0x0700, sig_get(SIG_SOFTWARE_VERSION)),
        (0x0700, sig_get(SIG_HARDWARE_REVISION)),
        (0x0700, sig_get(SIG_MANUFACTURER_NAME)),
        (0x0700, lbc_get(0x0003)),
        (0x0700, lbc_get(0x0004)),
        (0x0700, lbc_get(0x0005)),
        (0x0701, M.time_role_get()),
    ]
    assert remembered == {
        "secure_element_version": b"\x0d\x02\x01\x00",
        "bootloader_version": b"\x01\x00\x00\x00",
        # not supported, under the software version it answered
        "stm32_version.unsupported": b"02020002",
        NODE_INFO_TIME_ROLE: b"\x03",
    }


async def test_what_is_known_is_not_asked_again() -> None:
    """On a later link only the version is asked (the time role is known too); a push-button has no STM32 block."""
    known = {
        "hardware_revision": HARDWARE_REVISION,
        "manufacturer_name": MANUFACTURER_NAME,
        "secure_element_version": b"\x0d\x02\x01\x00",
        "bootloader_version": b"\x01\x00\x00\x00",
        NODE_INFO_TIME_ROLE: b"\x03",
    }
    answers = {sig_get(SIG_SOFTWARE_VERSION): b"\x1a\x00", M.time_role_get(): b"\x03"}
    reader, sent, _ = _reader(answers, known)
    await reader._read_version(_node(0x02))
    assert [pdu for _, pdu in sent] == [sig_get(SIG_SOFTWARE_VERSION)]


async def test_a_known_time_role_is_not_asked_again() -> None:
    """The time role is read once per node, like the identity block: only the diagnostics show it, and nothing
    in Home Assistant depends on it being fresh. A role not known yet is asked on the next link."""
    known = {"hardware_revision": b"1", "manufacturer_name": b"J"}
    answers = {sig_get(SIG_SOFTWARE_VERSION): b"\x1a\x00", M.time_role_get(): b"\x03"}
    reader, sent, _ = _reader(answers, known)
    await reader._read_version(_node(0x02))
    assert [pdu for _, pdu in sent][-1] == M.time_role_get()
    reader, sent, _ = _reader(answers, {**known, NODE_INFO_TIME_ROLE: b"\x03"})
    await reader._read_version(_node(0x02))
    assert M.time_role_get() not in [pdu for _, pdu in sent]


async def test_a_silent_node_is_asked_nothing_more() -> None:
    reader, sent, remembered = _reader({})
    await reader._read_version(_node(RTR))
    assert [pdu for _, pdu in sent] == [sig_get(SIG_SOFTWARE_VERSION)]
    assert remembered == {}


@pytest.mark.parametrize("role", [None, b"\x09", b""])
async def test_a_time_role_that_is_not_one_is_not_kept(role: bytes | None) -> None:
    """Silence, a prohibited role (4..255) and an empty Status leave the role unknown."""
    known = {"hardware_revision": b"1", "manufacturer_name": b"J"}
    answers = {sig_get(SIG_SOFTWARE_VERSION): b"\x1a\x00", M.time_role_get(): role}
    reader, sent, remembered = _reader(answers, known)
    await reader._read_version(_node(0x02))
    assert sent[-1][1] == M.time_role_get()
    assert NODE_INFO_TIME_ROLE not in remembered


async def test_no_time_role_get_without_a_time_setup_server() -> None:
    answers = {sig_get(SIG_SOFTWARE_VERSION): b"\x1a\x00"}
    known = {"hardware_revision": b"1", "manufacturer_name": b"J"}
    reader, sent, _ = _reader(answers, known)
    await reader._read_version(_node(0x02, time_server=False))
    assert M.time_role_get() not in [pdu for _, pdu in sent]


VERSION = b"\x1a\x00\x0102020002"
VALUELESS = {
    sig_get(SIG_HARDWARE_REVISION): b"\x10\x00\x01",
    sig_get(SIG_MANUFACTURER_NAME): b"\x11\x00",  # the id alone
    lbc_get(0x0003): b"\x03\x00\x01",
    lbc_get(0x0004): b"\x04\x00\x01",
    lbc_get(0x0005): b"\x05\x00\x01",
}
UNSUPPORTED = {
    "hardware_revision.unsupported": b"02020002",
    "manufacturer_name.unsupported": b"02020002",
    "secure_element_version.unsupported": b"02020002",
    "bootloader_version.unsupported": b"02020002",
    "stm32_version.unsupported": b"02020002",
}


async def test_an_item_answered_without_a_value_is_remembered_as_not_supported() -> (
    None
):
    """A Status without a value is how a node says it does not have the item: remembered (with the software
    version it answered under), not a value. Silence is not remembered: that item is asked on the next link."""
    answers = {sig_get(SIG_SOFTWARE_VERSION): VERSION, **VALUELESS}
    reader, _, remembered = _reader(answers, {NODE_INFO_TIME_ROLE: b"\x03"})
    await reader._read_version(_node(RTR))
    assert remembered == UNSUPPORTED

    silent = {sig_get(SIG_SOFTWARE_VERSION): VERSION}
    reader, sent, remembered = _reader(silent, {NODE_INFO_TIME_ROLE: b"\x03"})
    await reader._read_version(_node(RTR))
    assert len(sent) == 6
    assert remembered == {}


async def test_an_item_the_node_lacks_is_not_asked_again_until_a_firmware_update() -> (
    None
):
    """Not supported under the same software version: not asked on the next link. Another version (a firmware
    update) asks everything again."""
    known = {NODE_INFO_TIME_ROLE: b"\x03", **UNSUPPORTED}
    answers = {sig_get(SIG_SOFTWARE_VERSION): VERSION, **VALUELESS}
    reader, sent, _ = _reader(answers, known)
    await reader._read_version(_node(RTR))
    assert [pdu for _, pdu in sent] == [sig_get(SIG_SOFTWARE_VERSION)]

    updated = {**answers, sig_get(SIG_SOFTWARE_VERSION): b"\x1a\x00\x0102030000"}
    reader, sent, remembered = _reader(updated, known)
    await reader._read_version(_node(RTR))
    assert [pdu for _, pdu in sent] == [
        sig_get(SIG_SOFTWARE_VERSION),
        *VALUELESS,
    ]
    assert remembered == dict.fromkeys(UNSUPPORTED, b"02030000")


# all a push-button tells about itself but its time role
KNOWN_BUT_THE_ROLE = {
    "hardware_revision": b"1",
    "manufacturer_name": b"J",
    "secure_element_version": b"\x0d\x02\x01\x00",
    "bootloader_version": b"\x01\x00\x00\x00",
}


def _scheduling(
    reader: PropertyReader,
) -> list[tuple[int, object, Callable[[], Awaitable[None]]]]:
    """Give the reader's hub a link and record what `schedule_version` queues instead of running a worker."""
    hub = reader.hub
    hub.link_count, hub.connected, hub.restarted = 1, True, {}  # type: ignore[misc]
    queued: list[tuple[int, object, Callable[[], Awaitable[None]]]] = []

    def schedule(addr: int, job: Callable[[], Awaitable[None]], *, key: object) -> None:
        queued.append((addr, key, job))

    reader.schedule = schedule  # type: ignore[method-assign]
    return queued


async def test_the_version_is_asked_once_per_hub_and_again_after_a_restart() -> None:
    """Review-4 R4-5: asked on every link, the version cost a Get per node at each link-up. A read that got every
    answer is not repeated until the node restarts (a firmware update restarts it); one still unanswered is asked
    again on the next link, once."""
    answers = {sig_get(SIG_SOFTWARE_VERSION): b"\x1a\x00", M.time_role_get(): b"\x03"}
    reader, sent, _ = _reader(answers, KNOWN_BUT_THE_ROLE)
    queued = _scheduling(reader)
    hub = reader.hub
    node = _node(0x02)
    reader.schedule_version(node)
    reader.schedule_version(node)  # the same link
    assert [(addr, key) for addr, key, _ in queued] == [(0x0700, "version")]
    hub.link_count = 2  # not read yet: the next link asks again
    reader.schedule_version(node)
    assert len(queued) == 2
    await queued[-1][2]()
    assert sent[0] == (0x0700, sig_get(SIG_SOFTWARE_VERSION))
    hub.link_count = 3  # read, every item answered: not on later links
    reader.schedule_version(node)
    assert len(queued) == 2
    hub.restarted[0x0700] = dt_util.utcnow()  # the hub saw it restart since
    reader.schedule_version(node)
    assert len(queued) == 3


async def test_a_read_with_an_item_unanswered_is_asked_again_on_the_next_link() -> None:
    """The version answered, the time role did not: the read is not through, the next link asks again."""
    answers = {sig_get(SIG_SOFTWARE_VERSION): b"\x1a\x00"}
    reader, sent, _ = _reader(answers, KNOWN_BUT_THE_ROLE)
    queued = _scheduling(reader)
    node = _node(0x02)
    reader.schedule_version(node)
    await queued[-1][2]()
    assert [pdu for _, pdu in sent] == [
        sig_get(SIG_SOFTWARE_VERSION),
        M.time_role_get(),
    ]
    reader.hub.link_count = 2  # type: ignore[attr-defined]
    reader.schedule_version(node)
    assert len(queued) == 2


# --------------------------------------------------------------------------- the hub and the device registry


async def test_the_node_device_shows_its_manufacturer_and_hardware_revision(
    hass: HomeAssistant,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    fast_timeouts: None,  # the relay's lock read, unanswered here, may be queued before its node's reads
) -> None:
    """prop:sig:0x0010 / 0x0011: read at link-up, shown on the node's device, kept for the next setup; a blank
    name leaves JUNG; the LBC blocks and the time role are kept too."""
    fake_link.node_info[RELAY_MOTION, SIG_HARDWARE_REVISION] = HARDWARE_REVISION
    fake_link.node_info[RELAY_MOTION, SIG_MANUFACTURER_NAME] = MANUFACTURER_NAME
    fake_link.node_info[RELAY_MOTION, 0x0003] = bytes.fromhex("0d020100")
    entry = await start_detectors(hass, make_detectors_entry(), fake_link)
    hub = entry.runtime_data
    await wait_until(hass, lambda: NODE_INFO_TIME_ROLE in hub.node_info(RELAY_MOTION))
    assert (
        hub.node_info(RELAY_MOTION)
        == {
            "hardware_revision": HARDWARE_REVISION,
            "manufacturer_name": MANUFACTURER_NAME,
            "secure_element_version": bytes.fromhex("0d020100"),
            "bootloader_version.unsupported": b"",  # a Status without a value, under no version
            NODE_INFO_TIME_ROLE: b"\x03",
        }
    )
    node = hub.cdb.node_by_addr(RELAY_MOTION)
    registry = dr.async_get(hass)
    device = registry.async_get(hub.device_ids[node_identifier(node)])
    assert device is not None
    assert (device.manufacturer, device.hw_version) == (
        "Albrecht Jung GmbH & Co.KG",
        "10000000",
    )
    # the next link: nothing asked again but the version (the time role neither)
    fake_link.sent.clear()
    await hass.config_entries.async_reload(entry.entry_id)
    await wait_for_link(hass, entry)
    await wait_until(
        hass,
        lambda: any(p == sig_get(SIG_SOFTWARE_VERSION) for _, _, p in fake_link.sent),
    )
    await settle(hass)
    asked = [pdu for _, dst, pdu in fake_link.sent if dst == RELAY_MOTION]
    assert sig_get(SIG_SOFTWARE_VERSION) in asked
    assert sig_get(SIG_HARDWARE_REVISION) not in asked
    assert lbc_get(0x0003) not in asked
    assert lbc_get(0x0004) not in asked  # not supported: not asked again on every link
    assert M.time_role_get() not in [pdu for _, _, pdu in fake_link.sent]
    assert registry.async_get(device.id).manufacturer == "Albrecht Jung GmbH & Co.KG"

    # a blank name is no name
    fake_link.inject(
        RELAY_MOTION, OUR_ADDRESS, sig_status(SIG_MANUFACTURER_NAME, bytes(4))
    )
    await hass.async_block_till_done()
    assert registry.async_get(device.id).manufacturer == "JUNG"
    assert registry.async_get(device.id).hw_version == "10000000"
    # a device removed meanwhile is left alone
    registry.async_remove_device(device.id)
    update_node_device(hass, hub, node)
    assert registry.async_get(device.id) is None


async def test_the_first_store_layout_is_migrated_and_a_newer_one_refused(
    hass: HomeAssistant,
) -> None:
    store = NodeInfoStore(hass, "entry")
    assert await store._async_migrate_func(
        NODE_VERSIONS_STORAGE_VERSION, 1, {"0500": "3031", "0501": {"x": "00"}}
    ) == {"0500": {"software_version": "3031"}, "0501": {"x": "00"}}
    assert await store._async_migrate_func(NODE_VERSIONS_STORAGE_VERSION, 1, None) == {}
    with pytest.raises(NotImplementedError):
        await store._async_migrate_func(NODE_VERSIONS_STORAGE_VERSION + 1, 1, {})
