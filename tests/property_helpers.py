"""Helpers for the config-entity tests: a mesh that answers vendor property Gets / Sets, waits, a hub stand-in."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from custom_components.junghome_ble import const
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.devices import Metadata, build_devices
from custom_components.junghome_ble.jhmesh.pdu import decode_opcode

from .conftest import (
    CDB_PATH,
    META_DIR,
    FakeProxyLink,
    settle,
    setup_entry,
    wait_for_link,
)
from .helpers import NODE_ACTUATOR, NODE_LIGHT_CTL, NODE_LIGHT_SWITCH, NODE_SOCKET

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.junghome_ble.jhmesh.properties import PropertySpec

# properties of the fixture network's push-button 1-gang (WC mirror, 0148 / key 0149)
NODE_0148 = NODE_LIGHT_SWITCH
NODE_0232 = NODE_LIGHT_CTL  # 2-gang with a DALI insert, keys 0234 / 0235
NODE_0172 = NODE_SOCKET  # metering socket
NODE_0400 = NODE_ACTUATOR  # 2-channel actuator
PID_AUTO_DST, PID_RUN_ON, PID_MANUAL_OFF, PID_ON_DELAY = 0x000F, 0x1007, 0x100B, 0x1001
PID_DIM_MODE, PID_DIM_TO_WARM, PID_STATUS_LED = 0x0013, 0x100E, 0x5013
PID_LED1_ON, PID_LED1_OFF, PID_LED2_ON, PID_LED2_OFF = 0xA001, 0xA002, 0xA004, 0xA005
VENDOR_GET_STATUS = {0x02: 0x05, 0x08: 0x0B, 0x0E: 0x11}
VENDOR_SET_STATUS = {0x03: 0x05, 0x09: 0x0B, 0x0F: 0x11}


def vendor_status(opcode: int, pid: int, value: bytes, access: int = 3) -> bytes:
    """A vendor property Status PDU (`C5 / CB / D1 27 05 [pid][access][value]`)."""
    return M.vendor_property_status(
        {0x05: "admin", 0x0B: "manufacturer", 0x11: "user"}[opcode],
        pid,
        value,
        user_access=access,
    )


def default_value(spec: PropertySpec) -> bytes:
    """A plausible wire value for any config property (zero / off / first option / no colour / unlocked)."""
    codec = spec.codec
    if isinstance(codec, P.Enum):
        return codec.encode(codec.options[0])
    value: Any = 0
    if isinstance(codec, P.RgbMode):
        value = P.LedMode(0, 0, 0)
    elif isinstance(codec, P.Bool):
        value = False
    elif isinstance(codec, P.EnforcedOutputCodec):
        value = P.UNLOCK
    elif isinstance(codec, P.EdgeDetectionCodec):
        value = P.EdgeDetection(edge_mode=False)
    elif isinstance(codec, P.ThresholdCodec):
        value = P.Threshold(None, 0, False)
    elif isinstance(codec, P.Timestamp7):
        value = "2024-10-03T14:46:46"  # the boiler socket's record
    elif isinstance(codec, P.Text):
        value = "192.0.2.10"  # the gateway's address (a documentation address)
    return codec.encode(value)


class PropertyMesh:
    """Makes the fake proxy's mesh answer vendor property Gets and acknowledged Sets like JUNG devices.

    `values[(addr, pid)]` is what an element reports (a default per codec when unset); `silent` pairs never
    answer; an acknowledged Set is confirmed with a Status carrying the new value unless `confirm_sets` is off;
    `unsupported` pairs answer a Get or Set with the property id alone, as an element without the property does.
    """

    def __init__(
        self,
        link: FakeProxyLink,
        values: dict[tuple[int, int], bytes] | None = None,
        *,
        confirm_sets: bool = True,
    ) -> None:
        self.link = link
        self.values: dict[tuple[int, int], bytes] = dict(values or {})
        self.silent: set[tuple[int, int]] = set()
        self.unsupported: set[tuple[int, int]] = set()
        self.confirm_sets = confirm_sets
        self.gets: list[tuple[int, int]] = []  # (addr, pid) of every Get seen
        self.sets: list[
            tuple[int, int, bytes]
        ] = []  # (addr, pid, value) of every Set seen
        self._original = link.write_gatt_char
        link.write_gatt_char = self._write  # type: ignore[method-assign]

    async def _write(
        self, char: str, data: bytes, response: bool | None = None
    ) -> None:
        before = len(self.link.sent)
        await self._original(char, data, response)
        for src, dst, access in self.link.sent[before:]:
            op, cid, p = decode_opcode(access)
            if cid != M.JUNG_CID or len(p) < 2:
                continue
            pid = int.from_bytes(p[:2], "little")
            if (dst, pid) in self.unsupported:
                if op in VENDOR_GET_STATUS:
                    self.gets.append((dst, pid))
                if op in VENDOR_SET_STATUS:
                    self.sets.append((dst, pid, p[3:] if op == 0x03 else p[2:]))
                replies = VENDOR_GET_STATUS | VENDOR_SET_STATUS
                if op in replies:
                    reply = vendor_status(replies[op], pid, b"")[:-1]  # `[pid]` only
                    self.link.inject(dst, src, reply)
                continue
            if op in VENDOR_GET_STATUS:
                self.gets.append((dst, pid))
                if (dst, pid) in self.silent:
                    continue
                value = self.values.get((dst, pid))
                if value is None:
                    value = default_value(P.PROPERTIES[pid])
                self.link.inject(
                    dst, src, vendor_status(VENDOR_GET_STATUS[op], pid, value)
                )
            elif op in VENDOR_SET_STATUS:
                value = p[3:] if op == 0x03 else p[2:]
                self.sets.append((dst, pid, value))
                if self.confirm_sets and (dst, pid) not in self.silent:
                    self.values[dst, pid] = value
                    self.link.inject(
                        dst, src, vendor_status(VENDOR_SET_STATUS[op], pid, value)
                    )


async def real_wait(seconds: float) -> None:
    """Let real time pass (the `fast_sleep` fixture makes `asyncio.sleep` instant)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    loop.call_later(seconds, fut.set_result, None)
    await fut


def fake_hub(states: dict[int, Any] | None = None) -> Any:
    """A hub stand-in for the pure resolution functions: the fixture CDB, its devices, an empty registry map."""
    cdb = CDB.load(Path(CDB_PATH))
    meta = Metadata(
        Path(META_DIR) / "device_metadata.json", Path(META_DIR) / "scene_metadata.json"
    )
    return SimpleNamespace(
        cdb=cdb,
        devices=build_devices(cdb, meta),
        states=states or {},
        device_ids={},
        node_info=lambda unicast: {},  # nothing read: `JungHomeHub.node_info`
        entry=SimpleNamespace(title="test", entry_id="entry"),
    )


# --------------------------------------------------------------------------- fixtures (aliased by each test module)


@pytest.fixture
def fast_timeouts() -> Generator[None]:
    """Make an unanswered property Get / Set give up after milliseconds instead of the app's 3 s.

    Every reader (`config_entities`, `switch`, the JH Scheduler's `schedules`, `energy_history`) looks the
    constants up on the `const` module when it sends, so patching them there covers all of them, including a module
    imported after this fixture ran.
    """
    with (
        patch.object(const, "PROPERTY_READ_TIMEOUT", 0.01),
        patch.object(const, "PROPERTY_WRITE_TIMEOUT", 0.01),
    ):
        yield


@pytest.fixture
def mesh(fake_link: FakeProxyLink) -> PropertyMesh:
    """The fixture network's devices answer every property Get with a default value."""
    return PropertyMesh(fake_link)


@pytest.fixture
async def init_with_mesh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    mesh: PropertyMesh,
    fast_sleep: list[float],
) -> MockConfigEntry:
    """The integration set up against an answering mesh, every initial property read through."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    return mock_config_entry
