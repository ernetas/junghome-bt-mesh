"""Shared fixtures: synthetic mesh export, fake Bluetooth environment, fake GATT proxy link."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth.central_manager import CentralBluetoothManager
from habluetooth.models import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    get_test_config_dir,
)
from pytest_homeassistant_custom_component.syrupy import (
    HomeAssistantSnapshotExtension,
)

from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    DOMAIN,
    SIG_SOFTWARE_VERSION,
    STORAGE_DIR,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.advert import mac_from_uuid
from custom_components.junghome_ble.jhmesh.cdb import CDB
from custom_components.junghome_ble.jhmesh.client import (
    MESH_PROXY_DATA_IN,
    MESH_PROXY_DATA_OUT,
    MESH_PROXY_SERVICE,
)
from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial, aes_cmac
from custom_components.junghome_ble.jhmesh.export import raw_model
from custom_components.junghome_ble.jhmesh.pdu import (
    FILTER_WHITELIST,
    NONCE_APP,
    NONCE_DEVICE,
    PROXY_BEACON,
    PROXY_CONFIG,
    PROXY_NETWORK_PDU,
    NetworkPDU,
    ProxyReassembler,
    decode_opcode,
    encode_opcode,
    is_unicast,
    lower_segments_access,
    lower_unsegmented_access,
    network_decrypt,
    network_encrypt,
    proxy_frame,
    segment_ack,
    seq_auth_from,
    upper_decrypt,
    upper_encrypt_app,
    upper_encrypt_dev,
)

from .jhmesh.hypothesis_profiles import load as load_hypothesis_profile
from .jhmesh.hypothesis_profiles import name as hypothesis_profile
from .sim import Mesh, ProxyNode, SimGattClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from syrupy.assertion import SnapshotAssertion

    from custom_components.junghome_ble.jhmesh.crypto import AppKeyMaterial

load_hypothesis_profile()

FIXTURES = Path(__file__).parent / "fixtures"
CDB_PATH = str(FIXTURES / "MeshNetwork.json")
SHARE_EXPORT_PATH = str(
    FIXTURES / "JungHome.json"
)  # the app's "share via file" flavour of the same network
META_DIR = str(FIXTURES / "Application Support")
PROXY_ADDRESS = "00:00:5E:00:53:14"  # the MAC of node 0148 (`helpers.MAC_LIGHT_SWITCH`), which JUNG nodes advertise from
PROXY_NODE = 0x0148  # the fake proxy answers the filter status as this node

pytest_plugins = "pytest_homeassistant_custom_component"

# The directory pytest-homeassistant-custom-component hands every test as the configuration directory. It is shared
# by every test of every project using that virtualenv, so nothing of ours may ever be written there: the
# integration stores exports (with every mesh key) under `<config>/junghome_ble/`.
SHARED_TESTING_CONFIG = Path(get_test_config_dir())
# SIG setup states the fake's loads report (status opcode -> the models that serve it, the value until a Set):
# the values read from the installation's 0148 / 0232 (`docs/hidden-features.md` §3): restore after mains return,
# range 3084..65535, switch-on brightness 100 %, 2700 K
SCENE_SERVER = "1203"  # SIG Scene Server: answers Scene Get with its current scene

SETUP_SERVED: dict[int, tuple[frozenset[str], bytes]] = {
    M.GEN_ONPOWERUP_STATUS: (frozenset({"1007", "1301"}), bytes([2])),
    M.LIGHT_LIGHTNESS_RANGE_STATUS: (
        frozenset({"1301"}),
        bytes([0]) + (3084).to_bytes(2, "little") + b"\xff\xff",
    ),
    M.LIGHT_LIGHTNESS_DEFAULT_STATUS: (frozenset({"1301"}), b"\xff\xff"),
    M.LIGHT_CTL_DEFAULT_STATUS: (
        frozenset({"1304"}),
        b"\xff\xff" + (2700).to_bytes(2, "little") + b"\x00\x00",
    ),
}
SETUP_GETS = {
    M.GEN_ONPOWERUP_GET: M.GEN_ONPOWERUP_STATUS,
    M.LIGHT_LIGHTNESS_RANGE_GET: M.LIGHT_LIGHTNESS_RANGE_STATUS,
    M.LIGHT_LIGHTNESS_DEFAULT_GET: M.LIGHT_LIGHTNESS_DEFAULT_STATUS,
    M.LIGHT_CTL_DEFAULT_GET: M.LIGHT_CTL_DEFAULT_STATUS,
}
# acknowledged setup Set -> its status and the value it stores (from the Set's parameters)
SETUP_SETS: dict[int, tuple[int, Callable[[bytes], bytes]]] = {
    M.GEN_ONPOWERUP_SET: (M.GEN_ONPOWERUP_STATUS, lambda p: p[:1]),
    M.LIGHT_LIGHTNESS_RANGE_SET: (
        M.LIGHT_LIGHTNESS_RANGE_STATUS,
        lambda p: bytes([0]) + p[:4],
    ),
    M.LIGHT_LIGHTNESS_DEFAULT_SET: (M.LIGHT_LIGHTNESS_DEFAULT_STATUS, lambda p: p[:2]),
    M.LIGHT_CTL_DEFAULT_SET: (M.LIGHT_CTL_DEFAULT_STATUS, lambda p: p[:6]),
}
ELEMENT_GROUP = (
    0xC000  # where the fake's dimmers publish a Lightness Range Status, as JUNG's do
)
# state Get -> the unicast status the fake's loads answer it with when `FakeProxyLink.answer_state_gets` is on
STATE_GET_REPLIES: dict[int, bytes] = {
    M.GEN_ONOFF_GET: encode_opcode(M.GEN_ONOFF_STATUS) + b"\x00",
    M.LIGHT_LIGHTNESS_GET: encode_opcode(M.LIGHT_LIGHTNESS_STATUS) + bytes(2),
    M.LIGHT_CTL_GET: encode_opcode(M.LIGHT_CTL_STATUS)
    + bytes(2)
    + (4000).to_bytes(2, "little"),
    M.GEN_LEVEL_GET: encode_opcode(M.GEN_LEVEL_STATUS) + bytes(2),
    # a tunable-white light's temperature element (the refresh's read after the light's own Gets)
    M.LIGHT_CTL_TEMP_GET: encode_opcode(M.LIGHT_CTL_TEMP_STATUS)
    + (4000).to_bytes(2, "little")
    + bytes(2),
    # an answer, but not a range (status 2, Cannot Set Range Max): the light keeps its default limits
    M.LIGHT_CTL_TEMP_RANGE_GET: encode_opcode(M.LIGHT_CTL_TEMP_RANGE_STATUS)
    + bytes([2])
    + bytes(4),
}
# acknowledged state Set -> the status a JUNG load publishes to its element group once it applied it, built from the
# Set's parameters and the element's level before it (`FakeProxyLink.levels`, for a Delta / Move Set)
LOAD_SETS: dict[int, tuple[int, Callable[[bytes, int], bytes]]] = {
    M.GEN_ONOFF_SET: (M.GEN_ONOFF_STATUS, lambda p, _level: p[:1]),
    M.LIGHT_LIGHTNESS_SET: (M.LIGHT_LIGHTNESS_STATUS, lambda p, _level: p[:2]),
    M.LIGHT_CTL_SET: (M.LIGHT_CTL_STATUS, lambda p, _level: p[:4]),
    M.LIGHT_CTL_TEMP_SET: (M.LIGHT_CTL_TEMP_STATUS, lambda p, _level: p[:4]),
    M.GEN_LEVEL_SET: (M.GEN_LEVEL_STATUS, lambda p, _level: p[:2]),
    M.GEN_DELTA_SET: (
        M.GEN_LEVEL_STATUS,
        lambda p, level: max(
            -32768, min(32767, level + int.from_bytes(p[:4], "little", signed=True))
        ).to_bytes(2, "little", signed=True),
    ),
    M.GEN_MOVE_SET: (
        M.GEN_LEVEL_STATUS,
        lambda _p, level: level.to_bytes(2, "little", signed=True),
    ),
}


def load_sets(link: FakeProxyLink) -> list[tuple[int, bytes]]:
    """(element, access PDU) of every acknowledged state Set the hub sent so far (`LOAD_SETS`), attempts included."""
    return [(dst, a) for _, dst, a in link.sent if decode_opcode(a)[0] in LOAD_SETS]


@pytest.fixture
def answering_mesh(fake_link: FakeProxyLink) -> FakeProxyLink:
    """The fake's loads answer the connect-time refresh's state Gets (list it before `init_integration`).

    A command then never waits behind a refresh Get to the same element that nobody answers: the status the load
    publishes for the command would answer that older Get first (`ProxyClient.request`: the oldest waiter a status
    fits), and the command's own wait would run into its retry.
    """
    fake_link.answer_state_gets = True
    return fake_link


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components from the repository."""


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Give every test its own copy of the configuration directory (a `tmp_path`).

    Overrides the plugin's fixture suite-wide: exports fetched from the gateway or uploaded, the `.incoming-*`
    files of a running config flow and the `.bak` copies the room services keep all land under the configuration
    directory, and must never land in the plugin's shared `testing_config/`.
    """
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def shared_testing_config_untouched() -> Generator[None]:
    """Fail loudly when a test (or the state a previous one left) touches the shared `testing_config/`."""
    stray = SHARED_TESTING_CONFIG / STORAGE_DIR
    assert not stray.exists(), (
        f"{stray} exists before the test: something wrote into the shared testing_config directory"
    )
    yield
    assert not stray.exists(), (
        f"the test wrote into the shared testing_config directory: {stray}"
    )


@pytest.fixture(autouse=True)
def no_bluetooth_manager_of_an_earlier_test() -> None:
    """Start every test without the Bluetooth manager an earlier test's Home Assistant left behind.

    `habluetooth` keeps the manager in a process-wide global that the `bluetooth` integration's setup sets and
    nothing clears, so a test calling a Bluetooth helper without setting Bluetooth up passed only when an earlier
    test in the same process had (and against that test's stopped manager): an order dependence a shuffled run, or
    a test alone, turned into a failure.
    """
    CentralBluetoothManager.manager = None


@pytest.fixture
def cdb() -> CDB:
    return CDB.load(Path(CDB_PATH))


@pytest.fixture
def network_id(cdb: CDB) -> bytes:
    return cdb.net_keys[0].network_id


def make_service_info(
    network_id: bytes,
    address: str = PROXY_ADDRESS,
    rssi: int = -50,
    manufacturer_data: dict[int, bytes] | None = None,
) -> BluetoothServiceInfoBleak:
    dev = BLEDevice(address, None, {})
    adv = AdvertisementData(
        local_name=None,
        manufacturer_data=manufacturer_data or {},
        service_data={MESH_PROXY_SERVICE: b"\x00" + network_id},
        service_uuids=[MESH_PROXY_SERVICE],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak.from_device_and_advertisement_data(
        dev, adv, "local", 0.0, True
    )


def make_node_identity_info(
    cdb: CDB, unicast: int, address: str = PROXY_ADDRESS, rssi: int = -50
) -> BluetoothServiceInfoBleak:
    """A proxy advertising Node Identity for `unicast` (Mesh Profile §7.2.2.2.3): `0x01 | hash | random`."""
    rnd = bytes(range(8))
    sd = b"\x01" + cdb.net_keys[0].node_identity_hash(rnd, unicast) + rnd
    dev = BLEDevice(address, None, {})
    adv = AdvertisementData(
        local_name=None,
        manufacturer_data={},
        service_data={MESH_PROXY_SERVICE: sd},
        service_uuids=[MESH_PROXY_SERVICE],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak.from_device_and_advertisement_data(
        dev, adv, "local", 0.0, True
    )


@pytest.fixture
def service_info(network_id: bytes) -> BluetoothServiceInfoBleak:
    return make_service_info(network_id)


@pytest.fixture
def mock_bluetooth_env(
    service_info: BluetoothServiceInfoBleak,
) -> Generator[dict[str, Any]]:
    """Make HA's bluetooth helpers see one proxy node of the fixture network, through one connectable scanner.

    `scanners` is what `async_scanner_count` reports: 0 is a Home Assistant without Bluetooth. `heard_by` maps a MAC
    to what `async_scanner_devices_by_address` reports for it (the scanners hearing it; none by default).
    """
    env: dict[str, Any] = {
        "infos": [service_info],
        "callbacks": [],
        "scans": 0,
        "scanners": 1,
        "heard_by": {},
    }

    def discovered(
        hass: HomeAssistant, connectable: bool = True
    ) -> list[BluetoothServiceInfoBleak]:
        env["scans"] += 1
        return list(env["infos"])

    def register(
        hass: HomeAssistant, cb: Callable[..., None], matcher: Any, mode: Any
    ) -> Callable[[], None]:
        env["callbacks"].append(cb)
        return lambda: env["callbacks"].remove(cb)

    def device_for(hass: HomeAssistant, address: str, connectable: bool = True) -> Any:
        # the device advertising from `address` (the hub then connects to *that* MAC), None when nothing does
        return next((i.device for i in env["infos"] if i.address == address), None)

    with (
        patch(
            "custom_components.junghome_ble.coordinator.bluetooth.async_discovered_service_info",
            side_effect=discovered,
        ),
        patch(
            "custom_components.junghome_ble.config_flow.bluetooth.async_discovered_service_info",
            side_effect=discovered,
        ),
        patch(
            "custom_components.junghome_ble.coordinator.bluetooth.async_register_callback",
            side_effect=register,
        ),
        patch(
            "custom_components.junghome_ble.coordinator.bluetooth.async_ble_device_from_address",
            side_effect=device_for,
        ),
        # one module object: the same patch serves `__init__`'s not-ready check
        patch(
            "custom_components.junghome_ble.coordinator.bluetooth.async_scanner_count",
            side_effect=lambda hass, connectable=True: env["scanners"],
        ),
        patch(
            "custom_components.junghome_ble.coordinator.bluetooth.async_scanner_devices_by_address",
            side_effect=lambda hass, address, connectable=True: list(
                env["heard_by"].get(address, [])
            ),
        ),
    ):
        yield env


class FakeScheduler:
    """The JH Scheduler slots of the fixture's load elements, as `FakeProxyLink` serves them.

    `schedules` / `actions` hold what each element's slots are set to ((element, slot) -> value); an astro
    schedule's effective time is `SUNRISE` / `SUNSET` shifted by its offset. `silent` elements never answer;
    `quiet_sets` apply a Set without answering it (the element publishes instead, or not at all); a Set of a
    sub-command in `ignore_sets` is neither applied nor answered.
    """

    SUNRISE, SUNSET = (7, 12), (19, 48)

    def __init__(self) -> None:
        self.schedules: dict[tuple[int, int], V.Schedule] = {}
        self.actions: dict[tuple[int, int], V.Action] = {}
        self.silent: set[int] = set()
        self.quiet_sets = False
        self.ignore_sets: set[int] = set()  # sub-commands

    def _schedule(self, element: int, index: int) -> V.Schedule:
        return self.schedules.get(
            (element, index), V.Schedule(index, 0, frozenset(), (0, 0), (0, 0), 0)
        )

    def _effective(self, schedule: V.Schedule) -> bytes:
        trigger = schedule.type >> 1  # 1 timed, 2 sunrise, 3 sunset
        at = {2: self.SUNRISE, 3: self.SUNSET}.get(trigger, schedule.not_before)
        minutes = (at[0] * 60 + at[1] + (schedule.offset_min if trigger > 1 else 0)) % (
            24 * 60
        )
        effective = V.Schedule(
            schedule.index,
            schedule.type,
            schedule.days,
            divmod(minutes, 60),
            (0, 0),
            schedule.offset_min,
        )
        raw = bytearray(effective.encode()[3:])
        raw[0] = (V.SUB_EFFECTIVE_TIME << 4) | schedule.index
        return bytes(raw)

    def status(self, element: int, index: int, sub: int) -> bytes:
        """The Status params for a slot's sub-command, or the list (`sub` 15)."""
        if sub == V.SUB_LIST:
            bits = 0
            for i in range(V.SLOTS):
                kind = self._schedule(element, i).type
                code = 0 if kind == 0 else 3 if kind & 1 else 2
                bits |= code << (2 * i)
            return bytes([0xF0, 0]) + bits.to_bytes(4, "little")
        if sub == V.SUB_ACTION:
            action = self.actions.get((element, index), V.NO_ACTION)
            return V.scheduler_action_set(index, action)[3:]
        schedule = self._schedule(element, index)
        if sub == V.SUB_EFFECTIVE_TIME:
            return self._effective(schedule)
        return schedule.encode()[3:]

    def handle(self, element: int, is_set: bool, p: bytes) -> bytes | None:
        """Apply a Get / Set's params; return the Status params to answer with, None to stay silent."""
        if element in self.silent or not p:
            return None
        index, sub = p[0] & 0xF, p[0] >> 4
        if is_set:
            if sub in self.ignore_sets:
                return None
            if sub == V.SUB_ACTION:
                self.actions[element, index] = V.decode_action(p[1:])
            elif len(p) == 2:  # type only: 0 frees the slot
                if p[1] == 0:
                    self.schedules.pop((element, index), None)
                    self.actions.pop((element, index), None)
                else:
                    old = self._schedule(element, index)
                    self.schedules[element, index] = V.Schedule(
                        index,
                        p[1],
                        old.days,
                        old.not_before,
                        old.not_after,
                        old.offset_min,
                    )
            else:
                stored = V.decode_scheduler_status(p).schedule
                assert stored is not None
                self.schedules[element, index] = stored
            if self.quiet_sets:
                return None
        return self.status(element, index, sub)


# where the fake's nodes start counting: source `src` at `SRC_SEQ_BASE + (src << 8)`, so sources send different
# numbers (for their first 256 PDUs at least); far below the 24-bit end (and `SEQUENCE_SPACE_WARN`) even for the
# highest unicast address, and below the numbers tests send with on purpose (`test_node_diagnostics.send_from`)
SRC_SEQ_BASE = 0x100000


class FakeProxyLink:
    """A bleak-like client that behaves as a JUNG proxy node of the fixture network.

    Everything the hub writes is reassembled — the proxy SAR frames, then the lower-transport segments of a
    segmented message (§3.5.2.2) — and decrypted under the IV index the mesh is at: `sent` holds the AppKey
    access messages as (src, dst, access_pdu), `config_sent` the device-key ones, each once however many
    segments or retransmissions it took (`segments` lists every segment as written). Segments to a unicast
    element are acknowledged as the node would (§3.5.3.3, `ack_segments`); the Set Filter Type request is
    answered with a Filter Status of the type asked for (`filter_type`, an empty list) from `proxy_node` — the
    node whose MAC the hub connected to (`fake_link` sets it per connection), node 0148 for `PROXY_ADDRESS`. A
    Config request is answered by `config_reply` when set, else a Heartbeat Publication Set gets its Status
    (`answer_config` / `config_refuse`). An AppKey request is answered by `app_reply` when that returns a status;
    else by the nodes' built-in servers below. `tests/test_fake_conformance.py` holds it, the library's
    `FakeBleak` and the simulated proxy of `tests/sim` to one behaviour.

    `inject*` deliver traffic as the nodes send it, under the IV index the fake's own beacons announced
    (`inject_beacon` moves `iv_index`; while an update is in progress nodes keep transmitting under the old
    index, §3.10.5 — `tx_iv_index`). What a real proxy drops silently is recorded, never raised: a PDU the
    keys or the IV index cannot open lands in `undecryptable` (a test that expects the hub to be understood
    asserts it is empty), and `sent_acks` holds the Segment Acknowledgments the hub sent for injected segments.
    """

    def __init__(self, cdb: CDB) -> None:
        self.cdb = cdb
        self.nk: NetKeyMaterial = cdb.net_keys[0]
        self.ak: AppKeyMaterial = cdb.app_keys[0]
        self.address = PROXY_ADDRESS
        self.proxy_node = PROXY_NODE  # the node the Filter Status names; re-derived from the MAC at every connection
        self.node_by_mac: dict[str, int] = {
            mac: n.unicast for n in cdb.nodes if (mac := mac_from_uuid(n.uuid))
        }
        self.mtu_size = 247
        self.is_connected = True
        self.beacon_on_subscribe = True
        # the proxy filter's type: a connection starts on an empty white list (§6.6), Set Filter Type replaces it
        self.filter_type = FILTER_WHITELIST
        self.iv_index = 0  # what the mesh is at, as the fake's beacons say
        self.iv_update = False  # ... and whether an IV Update is in progress
        self.sent: list[tuple[int, int, bytes]] = []  # (src, dst, access_pdu) decrypted
        self.config_sent: list[
            tuple[int, int, bytes]
        ] = []  # (src, node, access_pdu) device-key messages, decrypted
        self.segments: list[
            tuple[int, int, int, int]
        ] = []  # (src, dst, seq_zero, seg_o) of every segment written, retransmissions included
        self.acked: list[
            tuple[int, int, int, int]
        ] = []  # (node, dst, seq_zero, block_ack) Segment Acks the fake's nodes sent
        self.sent_acks: list[
            tuple[int, int, int, int]
        ] = []  # (src, dst, seq_zero, block_ack) Segment Acks the hub sent for injected segments
        self.ack_segments = True  # acknowledge segments to a unicast element, as nodes do; False: a node that never acks
        self.undecryptable: list[
            tuple[str, int, bytes]
        ] = []  # (layer, index in `raw_writes` of the write completing it, payload) of what the mesh could not open
        self.expect_undecryptable = False  # the teardown fails on any entry unless the test sends such on purpose
        self.answer_config = True  # answer a Heartbeat Publication Set with a Success status, as a node does
        self.config_refuse = 0  # ... or with this status code
        self.config_reply: Callable[[int, bytes], bytes | None] | None = (
            None  # (node, access_pdu) -> status access PDU or None: the nodes' Configuration Servers
        )
        self.app_reply: Callable[[int, bytes], bytes | None] | None = (
            None  # (element, access_pdu) -> status access PDU or None: the elements' AppKey servers, tried first
        )
        self.raw_writes: list[bytes] = []
        # the mesh's replay protection (§3.8.8), shared by every node and kept across links as on air: the last
        # (IV index, sequence number) accepted per source; a PDU at or below it is dropped and listed in `replayed`
        # — the test's teardown fails on any (`fake_link`) unless the test expects them (`expect_replays`)
        self.rpl: dict[int, tuple[int, int]] = {}
        self.replayed: list[tuple[int, int, int]] = []  # (src, IV index, seq)
        self.expect_replays = False
        self._notify: Callable[[Any, bytearray], None] | None = None
        self._disconnected_callback: Callable[[Any], None] | None = None
        self._reasm = ProxyReassembler()
        self._segments: dict[
            tuple[int, int], dict[str, Any]
        ] = {}  # (src, seq_zero) -> reassembly context
        self.seq = 0x100000  # the fake's own proxy PDUs (the Filter Status), `_next()`
        # the last sequence number each source's traffic used (`_next_from`): every element keeps its own counter
        # (§3.4.4.3), so the hub's per-source replay list sees what it sees on air; a test may set one
        self.src_seq: dict[int, int] = {}
        self.connect_errors: list[
            Exception
        ] = []  # raised (one per attempt) by establish_connection
        self.write_error: Exception | None = (
            None  # raised by every GATT write while set
        )
        self.connect_count = 0
        self.versions: dict[
            int, bytes
        ] = {}  # node -> what it answers a SIG 0x001A Get with (ASCII digit pairs)
        # (node, property) -> what it answers a Get of SIG 0x0010 / 0x0011 or LBC 0x0003 .. 0x0005 with
        self.node_info: dict[tuple[int, int], bytes] = {}
        self.time_roles: dict[
            int, bytes
        ] = {}  # element -> its Time Role Status parameters

        self.setup: dict[
            tuple[int, int], bytes
        ] = {}  # (element, status opcode) -> its SIG setup state; `SETUP_SERVED`'s value until set
        self.setup_silent: set[int] = set()  # elements whose setup servers never answer
        self.current_scenes: dict[
            int, int
        ] = {}  # element -> the current scene its Scene Server answers a Scene Get with (0 until set)
        # elements that apply no acknowledged state Set and answer none (unplugged); the others publish its status
        self.sets_silent: set[int] = set()
        self.levels: dict[
            int, int
        ] = {}  # element -> its Generic Level after the Sets it answered
        # element -> (its present state, a remaining-time byte): it answers a state Set mid-transition, with the
        # status's long form `[present][target][remaining]`, the Set's state as the target (Mesh Model §3.2.1.4)
        self.fading: dict[int, tuple[bytes, int]] = {}
        # answer the connect-time refresh's state Gets (`STATE_GET_REPLIES`, unicast) as a healthy mesh does; off by
        # default, where a test drives every status itself (`answering_mesh`)
        self.answer_state_gets = False
        self.scheduler = (
            FakeScheduler()
        )  # the JH Scheduler of every element that hosts one

    @property
    def tx_iv_index(self) -> int:
        """IV index the mesh's nodes transmit under: the previous one while an update is in progress."""
        return self.iv_index - 1 if self.iv_update else self.iv_index

    def _rx_iv_index(self, n: NetworkPDU) -> int:
        """IV index a PDU from the hub was sent under: ours, or the previous one when its IVI bit says so."""
        return self.iv_index if (self.iv_index & 1) == n.ivi else self.iv_index - 1

    # -- bleak surface
    async def start_notify(
        self, char: str, cb: Callable[[Any, bytearray], None]
    ) -> None:
        assert char == MESH_PROXY_DATA_OUT
        self._notify = cb
        if (
            self.beacon_on_subscribe
        ):  # a JUNG proxy beacons right after the subscription
            asyncio.get_running_loop().call_soon(self.inject_beacon)

    async def write_gatt_char(
        self, char: str, data: bytes, response: bool | None = None
    ) -> None:
        assert char == MESH_PROXY_DATA_IN
        if self.write_error is not None:
            raise self.write_error
        self.raw_writes.append(bytes(data))
        r = self._reasm.feed(bytes(data))
        if r is None:
            return  # a SAR frame of a proxy PDU still being assembled
        msg_type, payload = r
        if msg_type == PROXY_CONFIG:
            n = network_decrypt(self.nk, self.iv_index, payload, proxy=True)
            if n is None:
                self._undecryptable("proxy-config", payload)
            elif self._replay(n):
                pass
            elif n.transport_pdu[0] == 0x00 and len(n.transport_pdu) >= 2:
                # Set Filter Type (§6.5.1): a new, empty filter of that type (§6.6), confirmed by a Filter Status
                self.filter_type = n.transport_pdu[1]
                self._answer_filter_status(n.src)
            return
        if msg_type != PROXY_NETWORK_PDU:
            return
        n = network_decrypt(self.nk, self.iv_index, payload)
        if n is None:
            self._undecryptable("network", payload)
            return
        if self._replay(n):
            return
        if n.ctl:
            self._on_control(n)
            return
        b0 = n.transport_pdu[0]
        akf, aid = bool(b0 & 0x40), b0 & 0x3F
        if b0 & 0x80:
            self._on_segment(n, akf, aid)
        else:
            self._on_access(n, akf, aid, n.seq, n.transport_pdu[1:], 0)

    async def disconnect(self) -> None:
        self.is_connected = False

    def _undecryptable(self, layer: str, payload: bytes) -> None:
        """Record a PDU the mesh drops unopened, with the position of the GATT write that completed it."""
        self.undecryptable.append((layer, len(self.raw_writes) - 1, payload))

    def _replay(self, n: NetworkPDU) -> bool:
        """Apply the replay protection to a PDU the hub wrote; True (and listed) when the mesh drops it."""
        seen = (self._rx_iv_index(n), n.seq)
        last = self.rpl.get(n.src)
        if last is not None and seen <= last:
            self.replayed.append((n.src, *seen))
            return True
        self.rpl[n.src] = seen
        return False

    # -- what the hub sent
    def _on_control(self, n: NetworkPDU) -> None:
        p = n.transport_pdu
        if p[0] & 0x7F == 0x00 and len(p) >= 7:  # Segment Acknowledgment (§3.5.3.3)
            hdr = int.from_bytes(p[1:3], "big")
            self.sent_acks.append(
                (n.src, n.dst, (hdr >> 2) & 0x1FFF, int.from_bytes(p[3:7], "big"))
            )

    def _on_segment(self, n: NetworkPDU, akf: bool, aid: int) -> None:
        """Collect one segment; acknowledge it (unicast only); deliver the message when the last one is in."""
        hdr = int.from_bytes(n.transport_pdu[1:4], "big")
        szmic, seq_zero, seg_o, seg_n = (
            (hdr >> 23) & 1,
            (hdr >> 10) & 0x1FFF,
            (hdr >> 5) & 0x1F,
            hdr & 0x1F,
        )
        self.segments.append((n.src, n.dst, seq_zero, seg_o))
        st = self._segments.setdefault(
            (n.src, seq_zero), {"parts": {}, "n": seg_n, "done": False}
        )
        st["parts"][seg_o] = n.transport_pdu[4:]
        block = 0
        for i in st["parts"]:
            block |= 1 << i
        if is_unicast(n.dst) and self.ack_segments:
            # one ack per received segment with the cumulative block, a little after the write as on air;
            # a retransmission after completion is acked again (the hub did not see the first ack)
            asyncio.get_running_loop().call_soon(
                self._send_ack, n.dst, n.src, seq_zero, block
            )
        if st["done"] or len(st["parts"]) != seg_n + 1:
            return
        st["done"] = True
        data = b"".join(st["parts"][i] for i in range(seg_n + 1))
        self._on_access(n, akf, aid, seq_auth_from(n.seq, seq_zero), data, szmic)

    def _on_access(
        self,
        n: NetworkPDU,
        akf: bool,
        aid: int,
        seq_auth: int,
        upper: bytes,
        szmic: int,
    ) -> None:
        """Decrypt a complete upper transport PDU from the hub and record it; answer a Config request."""
        iv = self._rx_iv_index(n)
        if akf:
            access = (
                upper_decrypt(
                    self.ak.key, NONCE_APP, iv, seq_auth, n.src, n.dst, upper, szmic
                )
                if aid == self.ak.aid
                else None
            )
            if access is None:
                self._undecryptable("app", upper)
                return
            self.sent.append((n.src, n.dst, access))
            if self.app_reply is not None:
                status = self.app_reply(n.dst, access)
                if status is not None:
                    self.inject(n.dst, n.src, status)
                    return
            if access == M.generic_property_get("manufacturer", SIG_SOFTWARE_VERSION):
                # every node's Manufacturer Property Server answers; no value = "unknown" unless a test set one
                self.inject(
                    n.dst,
                    n.src,
                    encode_opcode(M.GEN_MANU_PROP_STATUS)
                    + SIG_SOFTWARE_VERSION.to_bytes(2, "little")
                    + (
                        b"\x01" + self.versions[n.dst]
                        if n.dst in self.versions
                        else b""
                    ),
                )
            if access == M.scene_get() and self._hosts(n.dst, SCENE_SERVER):
                # a Scene Server answers with its current scene: none (`scenes` of the test, else 0)
                self.inject(
                    n.dst,
                    n.src,
                    encode_opcode(M.SCENE_STATUS)
                    + b"\x00"
                    + self.current_scenes.get(n.dst, 0).to_bytes(2, "little"),
                )
            self._answer_node_info(n.src, n.dst, access)
            self._answer_setup(n.src, n.dst, access)
            self._answer_scheduler(n.src, n.dst, access)
            self._answer_load_set(n.src, n.dst, access)
            return
        node = self.cdb.node_by_addr(n.dst)
        access = (
            upper_decrypt(
                node.dev_key, NONCE_DEVICE, iv, seq_auth, n.src, n.dst, upper, szmic
            )
            if node is not None
            else None
        )
        if node is None or access is None:
            self._undecryptable("dev", upper)
            return
        self.config_sent.append((n.src, n.dst, access))
        status = self._config_status(node.unicast, access)
        if status is not None:
            self.inject_config(node.unicast, n.src, status)

    def _hosts(self, address: int, model: str) -> bool:
        """Whether the element at `address` hosts `model`."""
        element = next(
            (e for n in self.cdb.nodes for e in n.elements if e.address == address),
            None,
        )
        return element is not None and model in element.models

    def _answer_node_info(self, src: int, dst: int, access: bytes) -> None:
        """Answer the rest of a node's information (`PropertyReader._read_version`) as every JUNG node does.

        SIG 0x0010 / 0x0011, LBC 0x0003 .. 0x0005 and a push-button's InsertId (User 0x0002) and ButtonLayout (Admin
        0x5001, `inserts.NodeInserts.read_unknown`) with the value `node_info` holds, else without one (the
        property unknown); a Time Role Get with `time_roles`' role, "client" (3) by default, as on air.
        """
        if access == M.time_role_get():
            self.inject(
                dst,
                src,
                encode_opcode(M.TIME_ROLE_STATUS) + self.time_roles.get(dst, b"\x03"),
            )
            return
        vendor = (
            ("manufacturer", 0x0003),
            ("manufacturer", 0x0004),
            ("manufacturer", 0x0005),
            ("user", 0x0002),
            ("admin", 0x5001),
        )
        for pid in (0x0010, 0x0011):
            if access == M.generic_property_get("manufacturer", pid):
                status = encode_opcode(M.GEN_MANU_PROP_STATUS)
                break
        else:
            for kind, pid in vendor:
                if access == M.vendor_property_get(kind, pid):
                    status = encode_opcode(
                        M.VENDOR_PROPERTY_STATUS_OPCODES[kind], M.JUNG_CID
                    )
                    break
            else:
                return
        value = self.node_info.get((dst, pid))
        self.inject(
            dst,
            src,
            status + pid.to_bytes(2, "little") + (b"\x01" + value if value else b""),
        )

    def _answer_setup(self, src: int, dst: int, access: bytes) -> None:
        """Answer a SIG setup Get or acknowledged Set as the load's setup server would, when the element hosts it.

        A CTL Default Set stores the Lightness Default too (the two share it); a Lightness Range Status is
        published to the element group, not sent to the client (`device-settings.md` §13 q.3).
        """
        op, _, p = decode_opcode(access)
        if op in SETUP_GETS:
            status = SETUP_GETS[op]
        elif op in SETUP_SETS:
            status, stored = SETUP_SETS[op]
            self.setup[dst, status] = stored(p)
            if status == M.LIGHT_CTL_DEFAULT_STATUS:
                self.setup[dst, M.LIGHT_LIGHTNESS_DEFAULT_STATUS] = p[:2]
        else:
            return
        element = next(
            (e for n in self.cdb.nodes for e in n.elements if e.address == dst), None
        )
        models, default = SETUP_SERVED[status]
        if (
            dst in self.setup_silent
            or element is None
            or models.isdisjoint(element.models)
        ):
            return
        to = (
            ELEMENT_GROUP
            if status == M.LIGHT_LIGHTNESS_RANGE_STATUS and op in SETUP_SETS
            else src
        )
        self.inject(
            dst, to, encode_opcode(status) + self.setup.get((dst, status), default)
        )

    def _answer_load_set(self, src: int, dst: int, access: bytes) -> None:
        """Answer a load's acknowledged state Set as JUNG firmware does: by publishing its status to the element group.

        Any unicast element answers (the fake checks neither the export nor the models), unless it is `sets_silent`.
        With `answer_state_gets` a state Get is answered too, by a unicast status at rest (off, level 0).
        """
        op, _, p = decode_opcode(access)
        if self.answer_state_gets and op in STATE_GET_REPLIES and dst < 0x8000:
            self.inject(dst, src, STATE_GET_REPLIES[op])
            return
        if op not in LOAD_SETS or dst in self.sets_silent or dst >= 0x8000:
            return
        status, params = LOAD_SETS[op]
        value = params(p, self.levels.get(dst, 0))
        if status == M.GEN_LEVEL_STATUS:
            self.levels[dst] = int.from_bytes(value, "little", signed=True)
        if dst in self.fading:
            present, remaining = self.fading[dst]
            value = present + value + bytes([remaining])
        self.inject(dst, ELEMENT_GROUP, encode_opcode(status) + value)

    def _answer_scheduler(self, src: int, dst: int, access: bytes) -> None:
        """Answer a JH Scheduler Get / Set as an element hosting the model would (`FakeScheduler`)."""
        op, cid, p = decode_opcode(access)
        if cid != M.JUNG_CID or op not in (V.JH_SCHEDULER_GET, V.JH_SCHEDULER_SET):
            return
        element = next(
            (e for n in self.cdb.nodes for e in n.elements if e.address == dst), None
        )
        if element is None or "05271016" not in element.models:
            return
        reply = self.scheduler.handle(dst, op == V.JH_SCHEDULER_SET, p)
        if reply is not None:
            self.inject(
                dst, src, encode_opcode(V.JH_SCHEDULER_STATUS, M.JUNG_CID) + reply
            )

    def _config_status(self, node: int, access: bytes) -> bytes | None:
        """What the node's Configuration Server answers `access` with (None: nothing)."""
        if self.config_reply is not None:
            return self.config_reply(node, access)
        op, _cid, params = decode_opcode(access)
        if op == C.CONFIG_HEARTBEAT_PUBLICATION_SET and self.answer_config:
            return (
                encode_opcode(C.CONFIG_HEARTBEAT_PUBLICATION_STATUS)
                + bytes([self.config_refuse])
                + params
            )
        if op == C.CONFIG_MODEL_PUBLICATION_GET and self.answer_config:
            return self._publication_status(params)
        return None

    def _publication_status(self, params: bytes) -> bytes | None:
        """A Config Model Publication Status with the export's publication of the model (none: address 0).

        `[status][element][publish address][AppKey index + credential][TTL][period][retransmit][model]`; nothing for an
        element or model the export does not have.
        """
        address = int.from_bytes(params[:2], "little")
        model_id = C.decode_model_id(params[2:])
        model = f"{model_id:08X}" if len(params) == 6 else f"{model_id:04X}"
        element = next(
            (e for n in self.cdb.nodes for e in n.elements if e.address == address),
            None,
        )
        if element is None:
            return None
        try:
            publish = raw_model(element, model).get("publish") or {}
        except KeyError:
            return None
        target = int(str(publish.get("address", "0000")), 16)
        return (
            encode_opcode(C.CONFIG_MODEL_PUBLICATION_STATUS)
            + b"\x00"
            + params[:2]
            + target.to_bytes(2, "little")
            + bytes(5)
            + params[2:]
        )

    # -- helpers for tests
    def _answer_filter_status(self, dst: int) -> None:
        pdu = network_encrypt(
            self.nk,
            self.tx_iv_index,
            ctl=True,
            ttl=0,
            seq=self._next(),
            src=self.proxy_node,
            dst=0x0000,
            transport_pdu=bytes([0x03, self.filter_type, 0, 0]),
            proxy=True,
        )
        self._deliver(PROXY_CONFIG, pdu)

    def _send_ack(self, node: int, dst: int, seq_zero: int, block: int) -> None:
        """Segment Acknowledgment from element `node` to the hub; nothing once the link is gone."""
        if not self.is_connected or self._notify is None:
            return
        self.acked.append((node, dst, seq_zero, block))
        self._deliver(
            PROXY_NETWORK_PDU,
            network_encrypt(
                self.nk,
                self.tx_iv_index,
                ctl=True,
                ttl=3,
                seq=self._next_from(node),
                src=node,
                dst=dst,
                transport_pdu=segment_ack(seq_zero, block),
            ),
        )

    def _next(self) -> int:
        """Next sequence number of the fake's own proxy PDUs."""
        self.seq += 1
        return self.seq

    def _next_from(self, src: int) -> int:
        """Next sequence number of element `src`; each source starts at its own offset (`SRC_SEQ_BASE`)."""
        self.src_seq[src] = self.src_seq.get(src, SRC_SEQ_BASE + (src << 8)) + 1
        return self.src_seq[src]

    def _deliver(self, msg_type: int, payload: bytes) -> None:
        assert self._notify is not None
        for frame in proxy_frame(msg_type, payload, self.mtu_size - 3):
            self._notify(None, bytearray(frame))

    def _deliver_lowers(
        self, src: int, dst: int, seq0: int, lowers: list[bytes], ttl: int, iv: int
    ) -> None:
        for i, lower in enumerate(lowers):
            self._deliver(
                PROXY_NETWORK_PDU,
                network_encrypt(
                    self.nk,
                    iv,
                    False,
                    ttl,
                    seq0 if i == 0 else self._next_from(src),
                    src,
                    dst,
                    lower,
                ),
            )

    def inject(
        self,
        src: int,
        dst: int,
        access_pdu: bytes,
        ttl: int = 3,
        *,
        iv_index: int | None = None,
    ) -> None:
        """Deliver an access message as if node `src` sent it; a long one arrives as segments (one network PDU each).

        Under the nodes' transmit IV index unless `iv_index` says otherwise.
        """
        iv = self.tx_iv_index if iv_index is None else iv_index
        seq = self._next_from(src)
        upper = upper_encrypt_app(self.ak, iv, seq, src, dst, access_pdu)
        if len(upper) <= 15:
            lowers = [lower_unsegmented_access(self.ak.aid, upper)]
        else:
            lowers = lower_segments_access(self.ak.aid, seq, upper)
        self._deliver_lowers(src, dst, seq, lowers, ttl, iv)

    def inject_from_provisioner(
        self, node_unicast: int, access_pdu: bytes, src: int = 0x0001
    ) -> None:
        """Deliver a Config message the provisioner (the app, `src`) sends a node, sealed with the node's device key."""
        node = self.cdb.node_by_addr(node_unicast)
        assert node is not None
        iv = self.tx_iv_index
        seq = self._next_from(src)
        upper = upper_encrypt_dev(node.dev_key, iv, seq, src, node_unicast, access_pdu)
        if len(upper) <= 15:
            lowers = [lower_unsegmented_access(0, upper, akf=False)]
        else:
            lowers = lower_segments_access(0, seq, upper, akf=False)
        self._deliver_lowers(src, node_unicast, seq, lowers, 3, iv)

    def inject_config(self, node_unicast: int, dst: int, access_pdu: bytes) -> None:
        """Deliver a device-key message from node `node_unicast` (a Config status), segmented when it is long."""
        node = self.cdb.node_by_addr(node_unicast)
        assert node is not None
        iv = self.tx_iv_index
        seq = self._next_from(node_unicast)
        upper = upper_encrypt_dev(node.dev_key, iv, seq, node_unicast, dst, access_pdu)
        if len(upper) <= 15:
            lowers = [lower_unsegmented_access(0, upper, akf=False)]
        else:
            lowers = lower_segments_access(0, seq, upper, akf=False)
        self._deliver_lowers(node_unicast, dst, seq, lowers, 3, iv)

    def inject_heartbeat(
        self, src: int, dst: int, init_ttl: int = 5, ttl: int = 4, features: int = 3
    ) -> None:
        """Deliver a Heartbeat transport control message from node `src`."""
        self._deliver(
            PROXY_NETWORK_PDU,
            network_encrypt(
                self.nk,
                self.tx_iv_index,
                ctl=True,
                ttl=ttl,
                seq=self._next_from(src),
                src=src,
                dst=dst,
                transport_pdu=bytes([0x0A, init_ttl]) + features.to_bytes(2, "big"),
            ),
        )

    def inject_beacon(
        self,
        iv_index: int | None = None,
        iv_update: bool = False,
        key_refresh: bool = False,
        new_key: bool = False,
    ) -> None:
        """Deliver a Secure Network Beacon; it states where the mesh is (`iv_index` None: where it already was).

        The mesh follows its own beacon: what the fake injects and expects from then on goes under that index.
        `new_key`: secured with another NetKey, as Key Refresh Phase 2 beacons are (§3.10.4) — ours cannot
        authenticate it.
        """
        if iv_index is not None:
            self.iv_index = iv_index
        self.iv_update = iv_update
        nk = NetKeyMaterial.derive(bytes(16)) if new_key else self.nk
        flags = (1 if key_refresh else 0) | (2 if iv_update else 0)
        body = bytes([flags]) + nk.network_id + self.iv_index.to_bytes(4, "big")
        self._deliver(PROXY_BEACON, b"\x01" + body + aes_cmac(nk.beacon_key, body)[:8])

    def drop_link(self) -> None:
        self.is_connected = False
        if self._disconnected_callback:
            self._disconnected_callback(self)


@pytest.fixture
def fake_link(cdb: CDB) -> Generator[FakeProxyLink]:
    link = FakeProxyLink(cdb)

    async def establish(
        client_class: Any,
        device: Any,
        name: str,
        disconnected_callback: Any = None,
        **kwargs: Any,
    ) -> FakeProxyLink:
        link.connect_count += 1
        if link.connect_errors:
            raise link.connect_errors.pop(0)
        link.is_connected = True
        link.address = device.address
        # the proxy is the node advertising from that MAC; a MAC the export does not know keeps the last node
        link.proxy_node = link.node_by_mac.get(device.address.upper(), link.proxy_node)
        link._reasm = ProxyReassembler()  # proxy SAR state belongs to the connection
        link.filter_type = FILTER_WHITELIST  # ... and so does the filter
        link._disconnected_callback = disconnected_callback
        return link

    with patch(
        "custom_components.junghome_ble.hub.link.establish_connection",
        side_effect=establish,
    ):
        yield link
    check_link_teardown(link)


def check_link_teardown(link: FakeProxyLink) -> None:
    """Fail unless the mesh understood everything the hub wrote (review-3 Q1), bar what the test expects.

    A sequence number the hub sends twice is a reused nonce, and the nodes drop the second PDU (`replayed`); a
    PDU the mesh cannot open (wrong key, IV index or nonce) is dropped as silently on air (`undecryptable`).
    The message names the layer, length and position of the first few, never their bytes.
    """
    assert link.expect_replays or not link.replayed, (
        f"the hub replayed (src, IV index, seq) {link.replayed[:5]}"
    )
    if link.undecryptable and not link.expect_undecryptable:
        # `pytest.fail`, not `assert`: the rewritten assertion would print the list, payloads and all
        pytest.fail(
            f"the mesh could not open {len(link.undecryptable)} PDU(s) the hub wrote; (write #, layer, length) "
            f"{[(i, layer, len(payload)) for layer, i, payload in link.undecryptable[:5]]}",
            pytrace=False,
        )


# ----------------------------------------------------------------------------- the hub over the simulated mesh


class SimLinks:
    """The links `sim_link` handed the hub: GATT connections to the simulated proxy nodes (`tests/sim`).

    The hub connects to the node advertising from the MAC it picked (node 0148 for `PROXY_ADDRESS`; a MAC of no
    simulated proxy keeps the last one). `connect_errors` are raised one per attempt, as `FakeProxyLink`'s are;
    `links` lists every link in the order handed out. Each new `ProxyClient` (a reload builds another hub) is
    watched by the mesh, so the teardown's invariants cover what it handed out and could not decrypt.
    """

    def __init__(self, mesh: Mesh, cdb: CDB) -> None:
        self.mesh = mesh
        self.proxy_node = PROXY_NODE
        self.node_by_mac: dict[str, int] = {
            mac: n.unicast for n in cdb.nodes if (mac := mac_from_uuid(n.uuid))
        }
        self.connect_errors: list[Exception] = []
        self.links: list[SimGattClient] = []
        self.connect_count = 0
        self._watched: list[Any] = []

    @property
    def link(self) -> SimGattClient | None:
        """The link handed out last, None before the first."""
        return self.links[-1] if self.links else None

    async def establish(
        self,
        client_class: Any,
        device: Any,
        name: str,
        disconnected_callback: Any = None,
        **kwargs: Any,
    ) -> SimGattClient:
        self.connect_count += 1
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        unicast = self.node_by_mac.get(device.address.upper(), self.proxy_node)
        if isinstance(self.mesh.nodes.get(unicast), ProxyNode):
            self.proxy_node = unicast
        # the callback is the hub's `ProxyClient.handle_disconnected`: its client is the one to watch
        client = getattr(disconnected_callback, "__self__", None)
        if client is not None and not any(c is client for c in self._watched):
            self._watched.append(client)
            self.mesh.watch(client)
        link = self.mesh.proxy(self.proxy_node).connect(mtu=247)
        link.disconnected_callback = disconnected_callback
        self.links.append(link)
        return link


@pytest.fixture
def sim_options() -> dict[str, Any]:
    """Keyword arguments of the `Mesh` `sim_mesh` builds (a test parametrizes it: quirks, retransmissions, loss)."""
    return {}


@pytest.fixture
async def sim_mesh(sim_options: dict[str, Any]) -> AsyncGenerator[Mesh]:
    """The simulated fixture network (`tests/sim`) on Home Assistant's test loop: the default hop matrix (a chain:
    0400 is four relays from the proxy 0148), no loss, JUNG's quirks — unless `sim_options` says otherwise.

    Its latencies are milliseconds of real time here. The teardown closes the mesh (nothing of it left on the loop,
    which Home Assistant's fixtures check) and asserts its invariants: no (SRC, IV, SEQ) twice, nothing replayed or
    undecryptable, nothing lost the loss model did not drop. Whatever drives the hub stops it first (`sim_entry`
    unloads the entry), so nothing the hub writes reaches a closed mesh.
    """
    mesh = Mesh(cdb_path=Path(CDB_PATH), **sim_options)
    yield mesh
    await mesh.close()
    mesh.assert_invariants()


@pytest.fixture
def sim_link(
    sim_mesh: Mesh, cdb: CDB, mock_bluetooth_env: dict[str, Any]
) -> Generator[SimLinks]:
    """`establish_connection` returns a link to the simulated proxy node the advert names, at MTU 247.

    The advert is `mock_bluetooth_env`'s (one proxy node of the fixture network, 0148, through one scanner).
    """
    links = SimLinks(sim_mesh, cdb)
    with patch(
        "custom_components.junghome_ble.hub.link.establish_connection",
        side_effect=links.establish,
    ):
        yield links


@pytest.fixture
async def sim_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    sim_link: SimLinks,
    fast_sleep: list[float],
) -> AsyncGenerator[MockConfigEntry]:
    """The config entry for a test that sets it up over the simulated mesh itself; unloaded before the mesh closes."""
    yield mock_config_entry
    if mock_config_entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def init_sim_integration(
    hass: HomeAssistant, sim_entry: MockConfigEntry
) -> MockConfigEntry:
    """The integration set up over the simulated mesh, the link up (the connect-time refresh may still run)."""
    await setup_entry(hass, sim_entry)
    await wait_for_link(hass, sim_entry)
    return sim_entry


@pytest.fixture(autouse=True)
def no_link_loss_grace(request: pytest.FixtureRequest) -> Generator[None]:
    """Entities go unavailable the moment a link is lost, as most tests expect; `link_loss_grace` tests keep it."""
    if "link_loss_grace" in request.keywords:
        yield
        return
    with patch("custom_components.junghome_ble.hub.link.LINK_LOSS_GRACE", 0.0):
        yield


@pytest.fixture
def no_connect_beacon(
    fake_link: FakeProxyLink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy that does not beacon when subscribed to; the hub then sends its filter without waiting for one.

    (That wait runs on loop time, which the frozen clock of these tests never advances.) List it before
    `init_integration` so it applies to the setup.
    """
    fake_link.beacon_on_subscribe = False
    monkeypatch.setattr(
        "custom_components.junghome_ble.hub.link.CONNECT_BEACON_WAIT", 0.0
    )


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="JUNG HOME mesh test",
        unique_id="1fbd2c61a4b6e5a4",
        data={
            CONF_CDB_PATH: CDB_PATH,
            CONF_METADATA_DIR: META_DIR,
            CONF_UNICAST: "0D00",
        },
    )


@pytest.fixture
def fast_sleep() -> Generator[list[float]]:
    """Make every `asyncio.sleep` (the connection loop's back-off, the proxy client's filter/refresh pauses)
    return after a single loop iteration; the requested non-zero delays are recorded for assertions."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, result: Any = None) -> Any:
        if delay > 0:
            delays.append(delay)
        await real_sleep(0)
        return result

    with patch("custom_components.junghome_ble.coordinator.asyncio.sleep", fake_sleep):
        yield delays


# `wait_until` measures its timeout on the real clock: the `freezer` fixture swaps every module attribute that *is*
# `time.monotonic` (and the loop's clock) for a frozen one, so the function is kept where that scan does not look.
_REAL_CLOCK = (time.monotonic,)
_real_sleep = asyncio.sleep  # `fast_sleep` and some tests patch `asyncio.sleep`; the loop is spun with the real one
WAIT_TIMEOUT = 10.0  # real seconds a condition may take: milliseconds under `fast_sleep`, so this only bounds a failure
SETTLE_MAX_TURNS = 5000  # loop turns `settle` spends at most on a loop that never goes idle (a connection loop retrying under `fast_sleep`)
CALL_BUDGET = 5.0  # real seconds a test's call phase may take, unless marked `slow_ok`


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Fail a test whose call phase took longer than `CALL_BUDGET` on the real clock (review-4 Q4-6, Q T8).

    Waits here run on patched sleeps, a frozen clock or a settled loop, so a test takes milliseconds; one that
    takes seconds is waiting out a real timeout (an unanswered Get, a delayed `Store` save) and would pass just
    as well, only slowly, if what it waits for never came. `@pytest.mark.slow_ok` opts a test out. The
    `thorough` Hypothesis profile runs every property test with many times the examples, so it opts out as a whole.
    """
    start = _REAL_CLOCK[0]()
    result = yield
    elapsed = _REAL_CLOCK[0]() - start
    if (
        elapsed > CALL_BUDGET
        and item.get_closest_marker("slow_ok") is None
        and hypothesis_profile() != "thorough"
    ):
        pytest.fail(
            f"the test took {elapsed:.1f} s, over the {CALL_BUDGET:g} s budget: a real-clock wait? "
            "(mark it `slow_ok` if the time is the point)",
            pytrace=False,
        )
    return result


def _loop_idle(loop: asyncio.AbstractEventLoop) -> bool:
    """Nothing is ready to run: what is left waits for time to pass (timers the tests drive) or for I/O."""
    ready = getattr(loop, "_ready", None)
    return ready is not None and not ready


def _running_executor_jobs(hass: HomeAssistant) -> list[asyncio.Future[Any]]:
    """Executor jobs still running that a background task (the connection loop, an export refresh) started.

    `hass.async_add_executor_job` files a job started outside HA's tracked tasks under `_background_tasks`, which
    `async_block_till_done()` does not wait for; while one runs the loop looks idle, although its caller resumes
    as soon as the worker thread is through (a slow disk, a busy CI runner).
    """
    return [
        job
        for job in hass._background_tasks
        if not isinstance(job, asyncio.Task) and not job.done()
    ]


async def wait_until(
    hass: HomeAssistant,
    predicate: Callable[[], bool],
    *,
    timeout: float = WAIT_TIMEOUT,
    what: str = "condition",
) -> None:
    """Spin the loop until `predicate` holds, then flush HA's tasks; fail naming `what` when it does not within `timeout`."""
    deadline = _REAL_CLOCK[0]() + timeout
    while not predicate():
        assert _REAL_CLOCK[0]() < deadline, f"{what} not reached within {timeout:g} s"
        await _real_sleep(0)
    await hass.async_block_till_done()


async def settle(hass: HomeAssistant, cycles: int | None = None) -> None:
    """Run the loop until nothing is left that can run without time passing, then flush HA's tasks.

    Every ready callback runs, every `fast_sleep` sleep returns and every executor job a background task started
    finishes before this comes back, so the connect-time refresh, the chunked property reads and a reconnect are
    through — whatever a test set up just before. Timers (`freezer` + `async_fire_time_changed`) and I/O are left
    alone. `cycles` is accepted for the older call sites and ignored: the loop, not a count, says when it is idle.
    """
    loop = asyncio.get_running_loop()
    for _ in range(
        2
    ):  # HA's flush can schedule more callbacks; one more round catches them
        for _ in range(SETTLE_MAX_TURNS):
            await _real_sleep(0)
            if _loop_idle(loop):
                if not (jobs := _running_executor_jobs(hass)):
                    break
                # the worker threads' results wake their callers: spin on until they are through too
                await asyncio.wait(jobs)
        await hass.async_block_till_done()


async def wait_for_link(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    connected: bool = True,
    cycles: int | None = None,
) -> None:
    """Wait until the hub reports the wanted link state (fully attached, not just a client handed over); fail if it never does.

    `cycles` is accepted for the older call sites and ignored (`wait_until` bounds the wait by `WAIT_TIMEOUT`).
    """

    def reached() -> bool:
        hub = entry.runtime_data
        return (hub.proxy_address is not None) == connected

    await wait_until(
        hass, reached, what=f"link {'up' if connected else 'down'} for {entry.title!r}"
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# what an entity shows while it has no state of its own: a reload passes every entity through them (review-4 D23)
NO_STATE = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})


@dataclass
class StateTransitions:
    """Every state change of an entity that had a state: (entity id, old state, new state), in order."""

    seen: list[tuple[str, str, str]] = field(default_factory=list)

    def lost(self) -> list[tuple[str, str, str]]:
        """The changes from a state of the entity's own to `unavailable` or `unknown`: what a reload writes."""
        return [t for t in self.seen if t[1] not in NO_STATE and t[2] in NO_STATE]


@pytest.fixture
def state_transitions(hass: HomeAssistant) -> Generator[StateTransitions]:
    """Record every state change from here on (`StateTransitions`); an entity added or removed is no change."""
    record = StateTransitions()

    @callback
    def changed(event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is not None and new is not None and old.state != new.state:
            record.seen.append((event.data["entity_id"], old.state, new.state))

    unsub = hass.bus.async_listen(EVENT_STATE_CHANGED, changed)
    yield record
    unsub()


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> MockConfigEntry:
    await setup_entry(hass, mock_config_entry)
    # let the background connection loop attach to the fake link and run the initial state refresh
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    return mock_config_entry


# ----------------------------------------------------------------------------- snapshot tests


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Return the snapshot fixture with Home Assistant's syrupy extension.

    Both `syrupy` and `pytest_homeassistant_custom_component` ship a plugin fixture named `snapshot`, and which
    one wins depends on plugin registration order, which is not stable across machines. Re-applying the extension
    from a conftest fixture settles it (conftest fixtures always take precedence over plugin fixtures, and
    re-wrapping an already-extended assertion is a no-op); Home Assistant core does the same in its conftest.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Enable every entity in the registry, whatever its `entity_registry_enabled_default`.

    `snapshot_platform` refuses disabled entities (it snapshots their state too), so the diagnostic sensors and
    the config entities that are off by default are enabled the way Home Assistant core's own fixture does it.
    """
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        yield


@pytest.fixture
async def init_platform(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
    entity_registry_enabled_by_default: None,
) -> AsyncGenerator[Callable[[str], Awaitable[MockConfigEntry]]]:
    """Return a factory that sets the integration up with a single platform loaded.

    `snapshot_platform` refuses to snapshot a config entry that owns entities from more than one domain, so the
    snapshot tests load exactly one platform at a time by patching `PLATFORMS`. Everything else (the fixture
    network, the fake proxy link, the instant back-offs) matches `init_integration`, so the snapshotted entities
    are the ones the rest of the suite exercises. The patch stays in place until the entry is unloaded on
    teardown, so the unload only touches the platform that was set up.
    """
    loaded: list[str] = []
    with patch("custom_components.junghome_ble.PLATFORMS", loaded):

        async def _setup(platform: str) -> MockConfigEntry:
            loaded[:] = [platform]
            await setup_entry(hass, mock_config_entry)
            await wait_for_link(hass, mock_config_entry)
            await settle(hass)
            return mock_config_entry

        yield _setup

        if mock_config_entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()
