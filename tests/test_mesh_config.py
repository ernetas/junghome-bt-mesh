"""mesh_config: rooms and key connections — the exact Config / vendor PDUs on air, the write-back, the failure paths.

Runs the real `ProxyClient` over the `FakeBleak` proxy of the jhmesh test harness (device-key crypto, segmentation
and acknowledgements included) against a copy of the synthetic export in a temporary directory; the hub is a
stub with just what `MeshConfigurator` touches (`hass.async_add_executor_job`, `entry.data`, `metadata`, `proxy`).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import shutil
import stat
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.junghome_ble import keep_awake as keep_awake_mod
from custom_components.junghome_ble import mesh_config as mc
from custom_components.junghome_ble.const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_LAST_SYNC,
    CONF_GATEWAY_SYNCED,
    CONF_METADATA_DIR,
)
from custom_components.junghome_ble.gateway_api import (
    GatewayAuthError,
    GatewayBusy,
    GatewayCertificateMismatch,
    GatewayError,
)
from custom_components.junghome_ble.identity import VaultKeeper
from custom_components.junghome_ble.jhmesh import client as client_mod
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.cdb import CDB, InvalidExport
from custom_components.junghome_ble.jhmesh.client import LocalState, ProxyClient
from custom_components.junghome_ble.jhmesh.devices import Metadata
from custom_components.junghome_ble.jhmesh.export import (
    ALLOCATION_MARGIN,
    AllocationCrowded,
    ProjectFile,
    as_int,
    function_code,
)
from custom_components.junghome_ble.jhmesh.fileio import backup_paths
from custom_components.junghome_ble.jhmesh.merge import MISSING, Change, Key
from custom_components.junghome_ble.jhmesh.pdu import (
    NetworkPDU,
    decode_opcode,
    encode_opcode,
)
from custom_components.junghome_ble.keep_awake import KeepAwake
from custom_components.junghome_ble.mesh_config import (
    APPLIED_NOTHING,
    MeshConfigurator,
    applied_unused_deleted,
)

from .conftest import CDB_PATH, META_DIR
from .helpers import NODE_LIGHT_CTL
from .jhmesh.conftest import FakeBleak, FastAsyncio

FIXTURES = Path(__file__).parent / "fixtures"
ANDROID_PATH = FIXTURES / "JungHome-android.json"
OUR_SRC = 0x0D00
GATEWAY = 0x00DC
GATEWAY_GROUP = 0xC005
SWITCH_NODE = 0x0148  # push-button 1-gang: load 0148 (WC), key 0149 -> gateway
SWITCH_LOAD, SWITCH_KEY = 0x0148, 0x0149
DALI_NODE = 0x0232  # push-button 2-gang: CTL load 0232, temperature 0233, rockers 0234 / 0235, aux 0236
DALI_LOAD, ROCKER_A, ROCKER_B, DALI_AUX = 0x0232, 0x0234, 0x0235, 0x0236
DALI_GROUP, ROCKER_A_GROUP = 0xC044, 0xC04F
SOCKET_NODE, SOCKET_SENSOR = 0x0172, 0x0173
SOCKET_GROUP, SENSOR_GROUP = 0xC000, 0xC001
DIMMER_NODE = 0x0300  # push-button 1-gang dimmer: load 0300 (WC), key 0301 linked to the WC room (android export)
DIMMER_LOAD, DIMMER_KEY = 0x0300, 0x0301
DIMMER_GROUP, DIMMER_KEY_GROUP = 0xC070, 0xC071
ACTUATOR_NODE, ACTUATOR_OUT1, ACTUATOR_OUT2 = 0x0400, 0x0400, 0x0401
WC, LIVING, KITCHEN = 0xC00F, 0xC010, 0xC011
LAMPS, SOCKETS = 0xFEF5, 0xFEF8
# Home Assistant's first new room and scene: the top of the app's ranges (C000..C64B, scenes 1..1999), clear of the
# app's next ones (review-4 W4-2)
NEW_ROOM, NEW_SCENE = 0xC64B, 0x1999
VENDOR_ADMIN_SET, VENDOR_ADMIN_GET, VENDOR_ADMIN_STATUS = 0x03, 0x02, 0x05
VENDOR_ADMIN_SET_UNACK = 0x04

STATUS_FOR = {
    C.CONFIG_MODEL_SUBSCRIPTION_ADD: C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
    C.CONFIG_MODEL_SUBSCRIPTION_DELETE: C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
    C.CONFIG_MODEL_PUBLICATION_SET: C.CONFIG_MODEL_PUBLICATION_STATUS,
    C.CONFIG_MODEL_APP_BIND: C.CONFIG_MODEL_APP_STATUS,
}


# ----------------------------------------------------------------------------- harness


def pub_set(element: int, address: int, model: str) -> bytes:
    return C.model_publication_set(element, address, model)


def sub_add(element: int, address: int, model: str) -> bytes:
    return C.model_subscription_add(element, address, model)


def sub_del(element: int, address: int, model: str) -> bytes:
    return C.model_subscription_delete(element, address, model)


def admin_set(prop: int, value: bytes, *, ack: bool = True) -> bytes:
    return M.vendor_property_set("admin", prop, value, ack=ack)


def admin_status(prop: int, value: bytes, access: int = 3) -> bytes:
    return (
        encode_opcode(VENDOR_ADMIN_STATUS, M.JUNG_CID)
        + prop.to_bytes(2, "little")
        + bytes([access])
        + value
    )


RESET_PROPERTY_MODE = [  # unacknowledged: the app never waits for them, and nothing must answer them
    admin_set(0x5006, b"\x00\x00\x00", ack=False),
    admin_set(0x5007, b"", ack=False),
    admin_set(0x5008, b"", ack=False),
]


class ConfigServer:
    """The nodes' Configuration Servers: every Config request gets a Success status carrying its own parameters,
    unless `refuse` names the (node, opcode, params) or `silent` swallows it. A request in `replay_before` is
    preceded by a duplicate of the *previous* request's status (a reply that came late and was retransmitted).
    `on_request` sees every request first and swallows it by returning True (a stop injected there); a Node
    Reset is confirmed."""

    def __init__(self, link: FakeBleak) -> None:
        self.link = link
        self.refuse: dict[bytes, int] = {}  # access pdu -> status code
        self.silent: set[bytes] = set()
        self.replay_before: set[bytes] = set()
        self.seen: list[tuple[int, bytes]] = []
        self.last: tuple[int, bytes] | None = None
        self.on_request: Callable[[int, bytes], bool] | None = None

    def __call__(self, node: int, access: bytes) -> bytes | None:
        self.seen.append((node, access))
        if self.on_request is not None and self.on_request(node, access):
            return None
        if access == C.node_reset():
            return encode_opcode(C.CONFIG_NODE_RESET_STATUS)
        if access in self.replay_before and self.last is not None:
            last_node, last_status = self.last
            asyncio.get_running_loop().call_soon(
                self.link.send_devkey,
                last_node,
                OUR_SRC,
                last_status,
                self.link.dev_key(last_node),
            )
        if access in self.silent:
            return None
        op, _cid, params = decode_opcode(access)
        status = self.refuse.get(access, C.STATUS_SUCCESS)
        reply = encode_opcode(STATUS_FOR[op]) + bytes([status]) + params
        self.last = (node, reply)
        return reply


class KeyServer:
    """The LBC Admin Property Servers of the key elements: records vendor Sets, answers Gets from what was set."""

    def __init__(self, link: FakeBleak, *, answer_sets: bool = True) -> None:
        self.link = link
        self.answer_sets = answer_sets
        self.answer_gets = True
        self.modes: dict[int, bytes] = {}  # element -> KeyMode value bytes
        self.values: dict[
            tuple[int, int], bytes
        ] = {}  # (element, property) -> other properties' value bytes
        self.refuse: set[int] = (
            set()
        )  # properties whose Sets are neither taken nor answered
        self.writes: list[
            tuple[int, bytes]
        ] = []  # (element, access pdu) of every vendor Set
        self._answered = 0
        link.responders.append(self.respond)

    def respond(self, n: NetworkPDU) -> None:
        if n.ctl or not n.transport_pdu[0] & 0x40:
            return  # AppKey messages only
        msgs = self.link.sent_access()
        for _src, dst, _ttl, _seq, access in msgs[self._answered :]:
            op, cid, p = decode_opcode(access)
            if cid != M.JUNG_CID:
                continue
            prop = int.from_bytes(p[:2], "little")
            if op == VENDOR_ADMIN_SET_UNACK:
                self.writes.append((dst, access))  # nothing to answer
            elif op == VENDOR_ADMIN_SET:
                self.writes.append((dst, access))
                if prop in self.refuse:
                    continue
                if prop == 0x5003:
                    self.modes[dst] = p[3:]
                else:
                    self.values[dst, prop] = p[3:]
                if self.answer_sets:
                    self.reply(dst, prop, p[3:])
            elif op == VENDOR_ADMIN_GET and self.answer_gets:
                value = (
                    self.modes.get(dst, b"\x06")
                    if prop == 0x5003
                    else self.values.get((dst, prop), b"")
                )
                self.reply(dst, prop, value)
        self._answered = len(msgs)

    def reply(self, element: int, prop: int, value: bytes) -> None:
        asyncio.get_running_loop().call_soon(
            self.link.send_access, element, OUR_SRC, admin_status(prop, value)
        )


class MemoryStore:
    """`Store` as `VaultKeeper` uses it: load, save, a path for the log; every save kept."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.saves: list[dict[str, Any]] = []
        self.path = "memory"

    async def async_load(self) -> dict[str, Any] | None:
        return self.data

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = data
        self.saves.append(data)

    async def async_remove(self) -> None:
        self.data = None


@dataclass
class FakeEntry:
    data: dict[str, Any]
    entry_id: str = "entry"
    options: dict[str, Any] = field(default_factory=dict)
    title: str = "JUNG HOME mesh test"
    state: ConfigEntryState = ConfigEntryState.LOADED
    runtime_data: Any = None  # the bench's hub (`make_bench`)
    setup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def async_create_background_task(
        self, hass: Any, target: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """The keep-alive task of a battery node (`KeepAwake.hold`)."""
        return asyncio.get_running_loop().create_task(target, name=name)


class FakeConfigEntries:
    """Only `async_update_entry`'s `data` replacement, which `_mark_synced` uses, and the bench's entry for a
    retried upload (`mesh_config._retry_upload`)."""

    def __init__(self) -> None:
        self.entries: dict[str, FakeEntry] = {}

    def async_get_entry(self, entry_id: str) -> FakeEntry | None:
        return self.entries.get(entry_id)

    def async_update_entry(self, entry: FakeEntry, *, data: dict[str, Any]) -> None:
        entry.data = data


class FakeHass:
    """Only what `MeshConfigurator` needs: the executor, the entries, `data` and background tasks."""

    def __init__(self) -> None:
        self.jobs: list[str] = []
        self.config_entries = FakeConfigEntries()
        self.data: dict[Any, Any] = {}
        self.is_stopping = False  # `MeshConfigurator._save` skips the gateway upload while Home Assistant stops

    def async_create_background_task(
        self, target: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """The retry of a failed upload (`MeshConfigurator._upload_or_retry`)."""
        return asyncio.get_running_loop().create_task(target, name=name)

    async def async_add_executor_job(self, fn: Callable[..., Any], *args: Any) -> Any:
        self.jobs.append(getattr(fn, "__name__", "job"))
        return fn(*args)


@dataclass
class FakeHub:
    hass: FakeHass
    entry: FakeEntry
    proxy: ProxyClient
    cdb: CDB
    metadata: Metadata = field(default_factory=Metadata)
    # `JungHomeHub.async_gateway_distrust`'s answer, and the certificate repairs it raised
    distrust: str | None = None
    certificate_issues: int = 0
    # the battery nodes' keep-alive (`keep_awake.py`); nothing here tracks traffic, so no node was ever heard from
    last_heard: dict[int, float] = field(default_factory=dict)
    keep_awake: KeepAwake = field(init=False)
    # the mesh's vault (`identity.py`) over an in-memory store
    vault: VaultKeeper = field(
        default_factory=lambda: VaultKeeper(MemoryStore(), lambda _stamp: MemoryStore())  # type: ignore[arg-type]
    )

    def __post_init__(self) -> None:
        self.keep_awake = KeepAwake(self)  # type: ignore[arg-type]

    async def async_gateway_distrust(self) -> str | None:
        return self.distrust

    async def async_follow_gateway(self) -> bool:
        return False

    def async_raise_certificate_issue(self) -> None:
        self.certificate_issues += 1


@dataclass
class Bench:
    path: Path
    hub: FakeHub
    link: FakeBleak
    config: ConfigServer
    keys: KeyServer
    configurator: MeshConfigurator
    original: bytes

    def config_pdus(self) -> list[tuple[int, bytes]]:
        """(node, access pdu) of every device-key message sent, in order."""
        return [
            (dst, access) for _src, dst, _ttl, _seq, access in self.link.sent_config()
        ]

    def app_pdus(self) -> list[tuple[int, bytes]]:
        """(element, access pdu) of every AppKey message sent, in order."""
        return [
            (dst, access) for _src, dst, _ttl, _seq, access in self.link.sent_access()
        ]

    def file_unchanged(self) -> bool:
        return self.path.read_bytes() == self.original

    def reload(self) -> ProjectFile:
        return ProjectFile.load(self.path)

    @property
    def journal(self) -> MemoryStore:
        """The entry's plan journal."""
        return self.hub.hass.data[mc.PLAN_JOURNALS][self.hub.entry.entry_id]  # type: ignore[no-any-return]


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> FastAsyncio:
    """Instant sleeps and short timeouts in the integration's copy of the mesh client."""
    fa = FastAsyncio()
    monkeypatch.setattr(client_mod, "asyncio", fa)
    monkeypatch.setattr(client_mod, "SEGMENT_ACK_TIMEOUT", 0.01)
    monkeypatch.setattr(mc, "CONFIG_TIMEOUT", 0.05)
    monkeypatch.setattr(mc, "KEY_MODE_TIMEOUT", 0.05)
    monkeypatch.setattr(mc, "SCENE_TIMEOUT", 0.05)
    return fa


async def make_bench(
    tmp_path: Path,
    source: Path = ANDROID_PATH,
    metadata_dir: str | None = None,
    *,
    answer_sets: bool = True,
) -> Bench:
    path = tmp_path / source.name
    shutil.copy(source, path)
    cdb = CDB.load(path)
    link = FakeBleak(cdb)
    link.auto_ack()
    config = ConfigServer(link)
    link.auto_config(config)
    keys = KeyServer(link, answer_sets=answer_sets)
    proxy = ProxyClient(cdb, LocalState(None, OUR_SRC))
    await proxy.attach(link)
    hub = FakeHub(
        FakeHass(),
        FakeEntry({CONF_CDB_PATH: str(path), CONF_METADATA_DIR: metadata_dir}),
        proxy,
        cdb,
        Metadata.from_export(cdb.export_meta),
    )
    hub.entry.runtime_data = hub
    hub.hass.config_entries.entries[hub.entry.entry_id] = hub.entry
    # the plan journal (`mesh_config.plan_journal`) in memory
    hub.hass.data[mc.PLAN_JOURNALS] = {hub.entry.entry_id: MemoryStore()}
    return Bench(
        path, hub, link, config, keys, MeshConfigurator(hub), path.read_bytes()
    )  # type: ignore[arg-type]


@pytest.fixture
async def bench(tmp_path: Path, fast: FastAsyncio) -> Bench:
    return await make_bench(tmp_path)


def subs(pf: ProjectFile, element: int, model: str) -> list[int]:
    el = pf.cdb.element(element)
    assert el is not None
    return el.subscriptions(model)


def pub(pf: ProjectFile, element: int, model: str) -> int | None:
    return pf.publication(element, model)


def link_rows(pf: ProjectFile) -> list[dict[str, Any]]:
    return [
        row
        for dev in pf.meta["devices"]
        if isinstance(dev, dict)
        for row in dev.get("cachedGroupConnectionMetadata") or []
    ]


def scene_key_rows(pf: ProjectFile) -> list[int | None]:
    """The key elements of `keyModeSceneConfigExports` (a key in scene mode and the scene it recalls)."""
    return [as_int(r["elementAddress"]) for r in pf.meta["keyModeSceneConfigExports"]]


# ----------------------------------------------------------------------------- key -> device (light mode)

CLEAR_ROCKER_A = [  # RemoveConnectionForAddress.AllConnections on 0234, which publishes / listens to C044
    (DALI_NODE, pub_set(ROCKER_A, 0x0000, "1001")),
    (DALI_NODE, sub_del(ROCKER_A, DALI_GROUP, "1001")),
    (DALI_NODE, pub_set(ROCKER_A, 0x0000, "05271015")),
    (DALI_NODE, sub_del(ROCKER_A, DALI_GROUP, "05271015")),
]
UNLISTEN_ROCKER_A = [  # what is left of the clear once the key's clients publish elsewhere: the old subscriptions
    (DALI_NODE, sub_del(ROCKER_A, DALI_GROUP, "1001")),
    (DALI_NODE, sub_del(ROCKER_A, DALI_GROUP, "05271015")),
]
WIRE_ROCKER_A_TO_DIMMER = [  # Publication Set + Subscription Add per client model of the light key mode
    (DALI_NODE, pub_set(ROCKER_A, DIMMER_GROUP, "1001")),
    (DALI_NODE, sub_add(ROCKER_A, DIMMER_GROUP, "1001")),
    (DALI_NODE, pub_set(ROCKER_A, DIMMER_GROUP, "1003")),
    (DALI_NODE, sub_add(ROCKER_A, DIMMER_GROUP, "1003")),
    (DALI_NODE, pub_set(ROCKER_A, DIMMER_GROUP, "05271015")),
    (DALI_NODE, sub_add(ROCKER_A, DIMMER_GROUP, "05271015")),
]


async def test_assign_key_to_a_dimmer_sends_the_apps_light_mode_sequence(
    bench: Bench,
) -> None:
    """network-logic.md §2.3, reordered: wire the light-mode clients, then drop what the key listened to before
    (the `Publication Set 0x0000` of the app's clear is superseded by the new publication and never sent), then
    the KeySetPropertyMode reset and KeyMode 0 — a plan that stops leaves the old link working."""
    assert await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD) is True
    # AppKey, after every Config step: the three unacknowledged resets, the KeyMode write last (acknowledged
    # Set, answered by the key)
    assert bench.app_pdus() == [(ROCKER_A, p) for p in RESET_PROPERTY_MODE] + [
        (ROCKER_A, admin_set(0x5003, b"\x00"))
    ]
    # DevKey: the new wiring first, the old subscriptions last
    assert bench.config_pdus() == [*WIRE_ROCKER_A_TO_DIMMER, *UNLISTEN_ROCKER_A]
    # byte-exact Publication Set: element, address, appkey 0 / master credentials, TTL 0xFF, no period, no retransmit
    assert bench.config_pdus()[0][1] == bytes.fromhex("03 3402 70c0 0000 ff 00 00 0110")
    assert bench.keys.modes == {ROCKER_A: b"\x00"}
    # the file: written after everything succeeded, round-trips, and only the key element changed
    pf = bench.reload()
    for model in ("1001", "1003", "05271015"):
        assert pub(pf, ROCKER_A, model) == DIMMER_GROUP
        assert subs(pf, ROCKER_A, model) == [DIMMER_GROUP]
    assert pub(pf, ROCKER_A, "05271013") == ROCKER_A_GROUP  # untouched (exclusion list)
    assert subs(pf, ROCKER_A, "05271013") == [ROCKER_A_GROUP]
    before = ProjectFile.load(ANDROID_PATH)
    assert subs(pf, DIMMER_LOAD, "1000") == subs(before, DIMMER_LOAD, "1000")
    assert pf.meta == before.meta
    assert pf.loaded_timestamp != before.loaded_timestamp
    assert bench.hub.hass.jobs == ["load_project", "save"]


async def test_assign_key_derives_the_mode_from_the_target(bench: Bench) -> None:
    """A switched light gets Switch (5): OnOff client + LBC User Property client only; a socket too."""
    await bench.configurator.assign_key(ROCKER_B, element=SWITCH_LOAD)
    wired = [
        a
        for _n, a in bench.config_pdus()
        if a[0] == C.CONFIG_MODEL_PUBLICATION_SET and a[3:5] != b"\x00\x00"
    ]
    assert wired == [
        pub_set(ROCKER_B, 0xC061, "1001"),
        pub_set(ROCKER_B, 0xC061, "05271015"),
    ]
    assert bench.keys.modes[ROCKER_B] == b"\x05"
    assert mc.derive_mode(bench.reload().cdb.element(DALI_LOAD)) == "light"  # type: ignore[arg-type]
    assert mc.derive_mode(bench.reload().cdb.element(SOCKET_NODE)) == "switch"  # type: ignore[arg-type]
    assert mc.derive_mode(bench.reload().cdb.element(GATEWAY)) == "gateway"  # type: ignore[arg-type]
    # MOD-04: DALI_LOAD + 1 is the CTL's temperature element (Level server only), not a blind — device-kind
    # classification (not "has a Level server, no OnOff") now correctly finds it no mode, same as DALI_AUX
    for no_mode in (DALI_LOAD + 1, DALI_AUX):
        with pytest.raises(ServiceValidationError) as exc:
            mc.derive_mode(bench.reload().cdb.element(no_mode))  # type: ignore[arg-type]
        assert exc.value.translation_key == "service_no_mode"
    blind = ProjectFile.load(FIXTURES / "Blinds.json").cdb.element(0x0700)
    assert blind is not None
    assert mc.derive_mode(blind) == "move"


async def test_assign_key_to_a_socket_wires_the_property_user_publications(
    bench: Bench,
) -> None:
    """`ConfigurePublicationsForPropertyUser`: the rocker's clients also listen to the socket's sensor element group."""
    await bench.configurator.assign_key(ROCKER_A, element=SOCKET_NODE, mode="switch")
    # the socket's User Property servers already publish to their element groups: only the extra subscriptions
    assert bench.config_pdus() == [
        (DALI_NODE, sub_add(ROCKER_A, SOCKET_GROUP, "1001")),
        (DALI_NODE, sub_add(ROCKER_A, SOCKET_GROUP, "05271015")),
        (DALI_NODE, sub_add(ROCKER_A, SENSOR_GROUP, "1001")),
        (DALI_NODE, sub_add(ROCKER_A, SENSOR_GROUP, "05271015")),
        (DALI_NODE, pub_set(ROCKER_A, SOCKET_GROUP, "1001")),
        (DALI_NODE, pub_set(ROCKER_A, SOCKET_GROUP, "05271015")),
        *UNLISTEN_ROCKER_A,
    ]
    pf = bench.reload()
    assert subs(pf, ROCKER_A, "1001") == [SOCKET_GROUP, SENSOR_GROUP]
    assert pub(pf, ROCKER_A, "1001") == SOCKET_GROUP


async def test_assign_key_to_the_gateway_uses_key_mode_6(bench: Bench) -> None:
    await bench.configurator.assign_key(ROCKER_A, element=GATEWAY)
    # the OnOff client is not part of the gateway mode: its publication is really cleared
    assert bench.config_pdus() == [
        (DALI_NODE, pub_set(ROCKER_A, GATEWAY_GROUP, "05271015")),
        (DALI_NODE, sub_add(ROCKER_A, GATEWAY_GROUP, "05271015")),
        (DALI_NODE, pub_set(ROCKER_A, 0x0000, "1001")),
        (DALI_NODE, sub_del(ROCKER_A, DALI_GROUP, "1001")),
        (DALI_NODE, sub_del(ROCKER_A, DALI_GROUP, "05271015")),
    ]
    assert bench.keys.modes[ROCKER_A] == b"\x06"


async def test_assign_key_binds_an_unbound_client_first(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    source = tmp_path / "unbound" / ANDROID_PATH.name
    source.parent.mkdir()
    shutil.copy(ANDROID_PATH, source)
    pf = ProjectFile.load(source)
    el = pf.cdb.element(ROCKER_A)
    assert el is not None
    next(m for m in el.raw_models if m["modelId"] == "1003")["bind"] = []
    pf.save(force=True)
    bench = await make_bench(tmp_path, source)
    await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD, mode="light")
    pdus = bench.config_pdus()
    i = pdus.index((DALI_NODE, pub_set(ROCKER_A, DIMMER_GROUP, "1003")))
    assert pdus[i - 1] == (DALI_NODE, C.model_app_bind(ROCKER_A, "1003", 0))
    el = bench.reload().cdb.element(ROCKER_A)
    assert el is not None
    assert next(m for m in el.raw_models if m["modelId"] == "1003")["bind"] == [0]


async def test_assign_key_fills_in_a_targets_missing_subscription(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """A target the app never wired completely: its key-mode servers are subscribed to their element group."""
    source = tmp_path / "unwired" / ANDROID_PATH.name
    source.parent.mkdir()
    shutil.copy(ANDROID_PATH, source)
    pf = ProjectFile.load(source)
    pf.set_subscriptions(DIMMER_NODE, 0, "1002", [LAMPS])
    pf.save(force=True)
    bench = await make_bench(tmp_path, source)
    await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert (
        DIMMER_NODE,
        sub_add(DIMMER_LOAD, DIMMER_GROUP, "1002"),
    ) in bench.config_pdus()
    assert subs(bench.reload(), DIMMER_LOAD, "1002") == [LAMPS, DIMMER_GROUP]


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"element": DIMMER_LOAD, "room": "WC"}, "service_one_target"),
        ({}, "service_one_target"),
        ({"element": DIMMER_LOAD, "mode": "light_and_switch"}, "service_invalid_mode"),
        ({"element": DIMMER_LOAD, "mode": "gateway"}, "service_invalid_mode"),
        ({"element": GATEWAY, "mode": "switch"}, "service_invalid_mode"),
        ({"room": "WC", "mode": "gateway"}, "service_invalid_mode"),
        ({"room": "Attic"}, "service_no_room"),
        ({"element": 0x0999}, "service_unknown_element"),
        ({"element": DALI_AUX}, "service_no_mode"),
        ({"element": DALI_AUX, "mode": "switch"}, "service_no_element_group"),
        ({"scene": "2", "room": "WC"}, "service_one_target"),
        ({"scene": "3"}, "service_unknown_scene"),
        ({"scene": "All off", "mode": "light"}, "service_invalid_mode"),
    ],
)
async def test_assign_key_validation(
    bench: Bench, kwargs: dict[str, Any], key: str
) -> None:
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.assign_key(ROCKER_A, **kwargs)
    assert exc.value.translation_key == key
    assert bench.config_pdus() == []
    assert bench.app_pdus() == []
    assert bench.file_unchanged()


async def test_assign_key_needs_a_client_model_of_the_mode(bench: Bench) -> None:
    """The socket's sensor element carries no Level client: no Move mode for it."""
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.assign_key(
            SOCKET_SENSOR, element=DIMMER_LOAD, mode="move"
        )
    assert exc.value.translation_key == "service_key_mode_unsupported"
    assert exc.value.translation_placeholders == {"address": "0173", "mode": "move"}
    assert bench.config_pdus() == []
    assert bench.file_unchanged()


async def test_unknown_key_element(bench: Bench) -> None:
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.assign_key(0x0999, element=DIMMER_LOAD)
    assert exc.value.translation_key == "service_unknown_element"
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.clear_key(0x0999)
    assert exc.value.translation_key == "service_unknown_element"


async def test_move_mode_is_accepted_but_flagged_untested(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    await bench.configurator.assign_key(ROCKER_A, element=DALI_LOAD + 1, mode="move")
    assert "never been tried" in caplog.text
    assert bench.config_pdus() == [
        (DALI_NODE, pub_set(ROCKER_A, 0xC04E, "1003")),
        (DALI_NODE, sub_add(ROCKER_A, 0xC04E, "1003")),
        (DALI_NODE, pub_set(ROCKER_A, 0xC04E, "05271015")),
        (DALI_NODE, sub_add(ROCKER_A, 0xC04E, "05271015")),
        (DALI_NODE, pub_set(ROCKER_A, 0x0000, "1001")),
        (DALI_NODE, sub_del(ROCKER_A, DALI_GROUP, "1001")),
        (DALI_NODE, sub_del(ROCKER_A, DALI_GROUP, "05271015")),
    ]
    assert bench.keys.modes[ROCKER_A] == b"\x01"


# ----------------------------------------------------------------------------- key -> scene (review-3 F15)


def scene_config(number: int) -> bytes:
    """KeyModeSceneConfig 0x5002 as the app writes it: the scene, no transition."""
    return number.to_bytes(2, "little") + bytes(4)


async def test_assign_key_to_a_scene_sends_the_apps_scene_sequence(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """`SetSceneConnection` (network-logic.md §2.5): the Scene Client publishes to all nodes — publish only —, the
    old connections are cleared, then KeyModeSceneConfig and KeyMode 2 (no KeySetPropertyMode reset, as the app);
    the `keyModeSceneConfigExports` row follows the fixture's (the app's) layout. Flagged as never tried."""
    assert await bench.configurator.assign_key(ROCKER_A, scene="All off") is True
    assert "never been tried" in caplog.text
    assert bench.config_pdus() == [
        (DALI_NODE, pub_set(ROCKER_A, 0xFFFF, "1205")),
        *CLEAR_ROCKER_A,
    ]
    assert bench.app_pdus() == [
        (ROCKER_A, admin_set(0x5002, scene_config(2))),
        (ROCKER_A, admin_set(0x5003, b"\x02")),
    ]
    assert bench.keys.modes == {ROCKER_A: b"\x02"}
    pf = bench.reload()
    assert pub(pf, ROCKER_A, "1205") == 0xFFFF
    assert subs(pf, ROCKER_A, "1205") == []
    assert pub(pf, ROCKER_A, "1001") is None
    rows = pf.meta["keyModeSceneConfigExports"]
    assert rows[0]["elementAddress"] == SWITCH_KEY  # the other key's row stays
    assert (
        rows[-1]
        == {
            "sceneConfig": {
                "transitionStepSeconds": 0,
                "sceneId": 2,
                "transitionResolution": 0,
                "publicationAddress": DALI_GROUP,  # the key's own load (the fixture's row names 0148's for 0149)
            },
            "elementAddress": ROCKER_A,
        }
    )
    assert list(rows[-1]) == list(rows[0])
    assert list(rows[-1]["sceneConfig"]) == list(rows[0]["sceneConfig"])
    # the device model reads the link back: the event entity's connection says scene 2
    assert Metadata.from_export(pf.meta).key_scenes[ROCKER_A] == 2


async def test_assign_key_to_a_scene_again_replaces_its_row(bench: Bench) -> None:
    """A key already in scene mode gets the new scene: one row, the new number; by number this time."""
    await bench.configurator.assign_key(SWITCH_KEY, scene="2")
    rows = bench.reload().meta["keyModeSceneConfigExports"]
    assert [(r["elementAddress"], r["sceneConfig"]["sceneId"]) for r in rows] == [
        (SWITCH_KEY, 2)
    ]
    assert rows[0]["sceneConfig"]["publicationAddress"] == 0xC061


async def test_a_key_without_a_scene_client_cannot_recall_a_scene(bench: Bench) -> None:
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.assign_key(DALI_AUX, scene="1")
    assert exc.value.translation_key == "service_key_mode_unsupported"
    assert exc.value.translation_placeholders == {"address": "0236", "mode": "scene"}
    assert bench.file_unchanged()


async def test_scene_config_read_back_when_the_set_is_not_answered(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    bench = await make_bench(tmp_path, answer_sets=False)
    await bench.configurator.assign_key(ROCKER_A, scene="1")
    gets = [
        a
        for _e, a in bench.app_pdus()
        if a[:3] == encode_opcode(VENDOR_ADMIN_GET, M.JUNG_CID)
    ]
    assert M.vendor_property_get("admin", 0x5002) in gets
    assert Metadata.from_export(bench.reload().meta).key_scenes[ROCKER_A] == 1


async def test_scene_config_not_taken_keeps_the_wiring_but_no_scene_row(
    bench: Bench,
) -> None:
    """The key refuses the scene: the Config steps stand (recorded), no row claims a scene, KeyMode is not sent."""
    bench.keys.refuse.add(0x5002)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, scene="1")
    assert exc.value.translation_key == "service_key_scene_not_applied"
    assert exc.value.translation_placeholders == {"address": "0234", "scene": "1"}
    assert ROCKER_A not in bench.keys.modes
    pf = bench.reload()
    assert pub(pf, ROCKER_A, "1205") == 0xFFFF
    assert scene_key_rows(pf) == [SWITCH_KEY]


async def test_scene_config_silence_says_what_was_applied(bench: Bench) -> None:
    bench.keys.refuse.add(0x5002)
    bench.keys.answer_gets = False
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, scene="1")
    assert exc.value.translation_key == "service_no_reply"
    assert exc.value.translation_placeholders["applied"] == mc.APPLIED_SCENE_WIRED
    assert pub(bench.reload(), ROCKER_A, "1205") == 0xFFFF


async def test_scene_link_row_mirrors_an_ios_style_file(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """Hex-string addresses in an existing row are copied for the new one."""
    source = tmp_path / "ios" / ANDROID_PATH.name
    source.parent.mkdir()
    shutil.copy(ANDROID_PATH, source)
    doc = json.loads(source.read_text())
    row = doc["meta"]["keyModeSceneConfigExports"][0]
    row["elementAddress"] = "0149"
    row["sceneConfig"]["publicationAddress"] = "C061"
    source.write_text(json.dumps(doc))
    bench = await make_bench(tmp_path, source)
    await bench.configurator.assign_key(ROCKER_A, scene="1")
    rows = bench.reload().meta["keyModeSceneConfigExports"]
    assert (
        rows[-1]["elementAddress"],
        rows[-1]["sceneConfig"]["publicationAddress"],
    ) == (
        "0234",
        "C044",
    )


async def test_scene_link_on_a_raw_cdb_warns_that_meta_is_not_written(
    tmp_path: Path, fast: FastAsyncio, caplog: pytest.LogCaptureFixture
) -> None:
    bench = await make_bench(tmp_path, Path(CDB_PATH), META_DIR)
    await bench.configurator.assign_key(ROCKER_A, scene="1")
    assert (
        "scene link of key 0234 is configured on the mesh but cannot be recorded"
        in caplog.text
    )
    pf = bench.reload()
    assert pub(pf, ROCKER_A, "1205") == 0xFFFF
    assert "meta" not in json.loads(pf.path.read_text())  # type: ignore[union-attr]


def test_scene_link_row_without_a_load_group() -> None:
    """A node without a load (here the gateway's element, standing in for a wall transmitter) has no load group:
    the row leaves `publicationAddress` out, as the Android export allows; the first row of a file has no
    template to mirror."""
    pf = ProjectFile.load(ANDROID_PATH)
    gateway = pf.cdb.element(GATEWAY)
    assert gateway is not None
    assert MeshConfigurator._scene_link_group(pf, gateway) is None
    pf.meta["keyModeSceneConfigExports"] = []
    MeshConfigurator._record_scene_link(pf, 0x0999, 4, None)
    assert pf.meta["keyModeSceneConfigExports"] == [
        {
            "sceneConfig": {
                "transitionStepSeconds": 0,
                "sceneId": 4,
                "transitionResolution": 0,
            },
            "elementAddress": 0x0999,
        }
    ]
    # beside an iOS-style row: a hex-string key, still no load group
    pf.meta["keyModeSceneConfigExports"] = [
        {"sceneConfig": {"sceneId": 1}, "elementAddress": "0149"}
    ]
    MeshConfigurator._record_scene_link(pf, 0x0999, 4, None)
    assert pf.meta["keyModeSceneConfigExports"][-1] == {
        "sceneConfig": {
            "sceneId": 4,
            "transitionStepSeconds": 0,
            "transitionResolution": 0,
        },
        "elementAddress": "0999",
    }


def test_confirms_property() -> None:
    status = admin_status(0x5002, scene_config(3))[3:]
    assert mc._confirms_property(status, 0x5002, scene_config(3))
    assert not mc._confirms_property(status, 0x5002, scene_config(4))
    assert not mc._confirms_property(status, 0x5003, scene_config(3))
    assert not mc._confirms_property(status[:5], 0x5002, scene_config(3))


# ----------------------------------------------------------------------------- key -> room


WIRE_ROCKER_A_TO_WC = [
    (SWITCH_NODE, sub_add(SWITCH_LOAD, ROCKER_A_GROUP, "1000")),
    (DIMMER_NODE, sub_add(DIMMER_LOAD, ROCKER_A_GROUP, "1000")),
    (DIMMER_NODE, sub_add(DIMMER_LOAD, ROCKER_A_GROUP, "1002")),
    (DALI_NODE, pub_set(ROCKER_A, ROCKER_A_GROUP, "1001")),
    (DALI_NODE, sub_add(ROCKER_A, ROCKER_A_GROUP, "1001")),
    (DALI_NODE, pub_set(ROCKER_A, ROCKER_A_GROUP, "1003")),
    (DALI_NODE, sub_add(ROCKER_A, ROCKER_A_GROUP, "1003")),
    (DALI_NODE, pub_set(ROCKER_A, ROCKER_A_GROUP, "05271015")),
    (DALI_NODE, sub_add(ROCKER_A, ROCKER_A_GROUP, "05271015")),
]


async def test_assign_key_to_a_room_wires_the_rooms_lamps_to_the_keys_group(
    bench: Bench,
) -> None:
    """network-logic.md §2.4: the WC loads (switch 0148, dimmer 0300) subscribe to rocker A's own group C04F,
    the rocker publishes there, and the link is cached in `meta` for the app."""
    await bench.configurator.assign_key(ROCKER_A, room="wc")  # case-insensitive
    assert bench.config_pdus() == [
        *WIRE_ROCKER_A_TO_WC,
        *UNLISTEN_ROCKER_A,
    ]
    assert bench.keys.modes[ROCKER_A] == b"\x00"
    pf = bench.reload()
    assert subs(pf, SWITCH_LOAD, "1000") == [
        0xC061,
        LAMPS,
        WC,
        DIMMER_KEY_GROUP,
        ROCKER_A_GROUP,
    ]
    # the rocker's gang had no `meta.devices[]` entry: one is synthesised, in the file's own style
    entry = pf.meta["devices"][-1]
    assert entry["name"] == "Push-button 2-gang 0232 buttons"
    assert entry["deviceId"] == {
        "actuatorFunctionId": 4,
        "locationIds": [64],
        "insertType": 2,
        "productId": 2,
        "nodeId": NODE_LIGHT_CTL.upper(),
    }
    assert entry["cachedGroupConnectionMetadata"] == [
        {
            "elementAddress": ROCKER_A,
            "groupAddress": WC,
            "publishAddress": ROCKER_A_GROUP,
            "function": "LIGHT",
        }
    ]
    # the existing link of the dimmer key is untouched
    assert len(link_rows(pf)) == 2


async def test_room_link_functions_filter_the_members(bench: Bench) -> None:
    """SWITCH only wires sockets (none in WC); LIGHT_AND_SWITCH wires every OnOff load; the app-style link row."""
    await bench.configurator.assign_key(ROCKER_A, room="Kitchen", mode="switch")
    subs_sent = [
        a
        for _n, a in bench.config_pdus()
        if a[:2] == encode_opcode(C.CONFIG_MODEL_SUBSCRIPTION_ADD)
    ]
    assert sub_add(SOCKET_NODE, ROCKER_A_GROUP, "1000") in subs_sent
    assert not any(a[2:4] == ACTUATOR_OUT1.to_bytes(2, "little") for a in subs_sent)
    assert bench.keys.modes[ROCKER_A] == b"\x05"
    pf = bench.reload()
    row = next(r for r in link_rows(pf) if r["elementAddress"] == ROCKER_A)
    assert (row["groupAddress"], row["function"]) == (KITCHEN, "SWITCH")

    bench.link.net_pdus.clear()
    await bench.configurator.assign_key(
        ROCKER_A, room="Kitchen", mode="light_and_switch"
    )
    pdus = bench.config_pdus()
    # re-assigning unwires the previous room link — except what the new link wires again: the socket keeps
    # listening to the rocker (the Subscription Delete would undo the Add sent before it, so it is not sent)
    assert (SOCKET_NODE, sub_del(SOCKET_NODE, ROCKER_A_GROUP, "1000")) not in pdus
    assert pdus[0] == (SOCKET_NODE, sub_add(SOCKET_NODE, ROCKER_A_GROUP, "1000"))
    assert (ACTUATOR_NODE, sub_add(ACTUATOR_OUT1, ROCKER_A_GROUP, "1000")) in pdus
    assert (ACTUATOR_NODE, sub_add(ACTUATOR_OUT2, ROCKER_A_GROUP, "1000")) in pdus
    assert (SOCKET_NODE, sub_add(SOCKET_NODE, ROCKER_A_GROUP, "1000")) in pdus
    assert bench.keys.modes[ROCKER_A] == b"\x00"
    rows = [r for r in link_rows(bench.reload()) if r["elementAddress"] == ROCKER_A]
    assert [r["function"] for r in rows] == ["LIGHT_AND_SWITCH"]


async def test_room_link_row_mirrors_an_ios_style_file(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """Hex-string addresses and ordinal functions in an existing row are copied for the new one."""
    source = tmp_path / "ios" / ANDROID_PATH.name
    source.parent.mkdir()
    shutil.copy(ANDROID_PATH, source)
    doc = json.loads(source.read_text())
    row = doc["meta"]["devices"][5]["cachedGroupConnectionMetadata"][0]
    row.update(
        {
            "elementAddress": "0301",
            "groupAddress": "C00F",
            "publishAddress": "C071",
            "function": 0,
        }
    )
    source.write_text(json.dumps(doc))
    bench = await make_bench(tmp_path, source)
    await bench.configurator.assign_key(ROCKER_A, room="WC", mode="light")
    rows = link_rows(bench.reload())
    assert rows[-1] == {
        "elementAddress": "0234",
        "groupAddress": "C00F",
        "publishAddress": "C04F",
        "function": 0,
    }


async def test_room_link_on_a_raw_cdb_warns_that_meta_is_not_written(
    tmp_path: Path, fast: FastAsyncio, caplog: pytest.LogCaptureFixture
) -> None:
    bench = await make_bench(tmp_path, Path(CDB_PATH), META_DIR)
    await bench.configurator.assign_key(ROCKER_A, room="WC")
    assert "cannot be recorded" in caplog.text
    pf = bench.reload()
    assert pf.flavour == "cdb"
    assert pub(pf, ROCKER_A, "1001") == ROCKER_A_GROUP
    assert subs(pf, SWITCH_LOAD, "1000") == [0xC061, LAMPS, WC, ROCKER_A_GROUP]
    assert "meta" not in json.loads(pf.path.read_text())  # type: ignore[union-attr]


# ----------------------------------------------------------------------------- clear


async def test_clear_key_drops_publications_subscriptions_and_the_room_link(
    bench: Bench,
) -> None:
    """The dimmer key 0301 is linked to WC: its loads stop listening to C071, then the key itself is cleared;
    KeyMode is not touched (no AppKey message at all), the `meta` row goes."""
    assert await bench.configurator.clear_key(DIMMER_KEY) is True
    assert bench.config_pdus() == [
        (SWITCH_NODE, sub_del(SWITCH_LOAD, DIMMER_KEY_GROUP, "1000")),
        (DIMMER_NODE, sub_del(DIMMER_LOAD, DIMMER_KEY_GROUP, "1000")),
        (DIMMER_NODE, sub_del(DIMMER_LOAD, DIMMER_KEY_GROUP, "1002")),
        (DIMMER_NODE, pub_set(DIMMER_KEY, 0x0000, "1001")),
        (DIMMER_NODE, sub_del(DIMMER_KEY, DIMMER_KEY_GROUP, "1001")),
        (DIMMER_NODE, pub_set(DIMMER_KEY, 0x0000, "05271015")),
        (DIMMER_NODE, sub_del(DIMMER_KEY, DIMMER_KEY_GROUP, "05271015")),
    ]
    assert bench.config_pdus()[3][1] == bytes.fromhex("03 0103 0000 0000 ff 00 00 0110")
    assert bench.app_pdus() == []
    pf = bench.reload()
    assert pub(pf, DIMMER_KEY, "1001") is None
    assert subs(pf, DIMMER_KEY, "1001") == []
    assert pub(pf, DIMMER_KEY, "05271013") == DIMMER_KEY_GROUP
    assert subs(pf, SWITCH_LOAD, "1000") == [0xC061, LAMPS, WC]
    assert link_rows(pf) == []
    # clearing again: nothing left to send or change, so nothing is written either (CFG-14)
    bench.link.net_pdus.clear()
    bench.hub.hass.jobs.clear()
    written = bench.path.read_bytes()
    assert await bench.configurator.clear_key(DIMMER_KEY) is False
    assert bench.config_pdus() == []
    assert bench.path.read_bytes() == written
    assert bench.hub.hass.jobs == ["load_project"]


async def test_clear_key_of_a_device_link(bench: Bench) -> None:
    await bench.configurator.clear_key(ROCKER_A)
    assert bench.config_pdus() == CLEAR_ROCKER_A
    pf = bench.reload()
    assert pub(pf, ROCKER_A, "1001") is None
    assert subs(pf, ROCKER_A, "05271015") == []


@pytest.mark.parametrize("change", ["clear", "room", "device"])
async def test_clearing_or_reassigning_a_scene_key_drops_its_scene_row(
    bench: Bench, change: str
) -> None:
    """Review-3 W2: key 0149 recalls scene 1 (its `keyModeSceneConfigExports` row). Cleared, or given a room or a
    device, it no longer does — the row left behind made the app still show it as "Scene 1"."""
    assert scene_key_rows(bench.reload()) == [SWITCH_KEY]
    if change == "clear":
        await bench.configurator.clear_key(SWITCH_KEY)
    elif change == "room":
        await bench.configurator.assign_key(SWITCH_KEY, room="Kitchen")
    else:
        await bench.configurator.assign_key(SWITCH_KEY, element=DIMMER_LOAD)
    pf = bench.reload()
    assert scene_key_rows(pf) == []
    assert pub(pf, SWITCH_KEY, "1001") != GATEWAY_GROUP


async def test_clear_key_leaves_virtual_and_fixed_group_subscriptions_alone(
    tmp_path: Path, fast: FastAsyncio, caplog: pytest.LogCaptureFixture
) -> None:
    """Review-3 W12: a key subscribed (by another tool) to a virtual address — with its Label UUID in the file or
    without — or to a fixed group made the clear raise building a Subscription Delete that cannot carry them.
    They are kept, as written, and logged; everything else is cleared."""
    label = "0123456789ABCDEF0123456789ABCDEF"

    def edit(net: dict[str, Any]) -> None:
        node = next(n for n in net["nodes"] if n["unicastAddress"] == "0232")
        rocker = node["elements"][ROCKER_A - DALI_NODE]
        model = next(m for m in rocker["models"] if m["modelId"] == "1001")
        model["subscribe"] = [label, "8123", "FFFD", "C044"]

    bench = await prepare(tmp_path, "virtual", lambda doc: edited_network(doc, edit))
    assert await bench.configurator.clear_key(ROCKER_A) is True
    assert bench.config_pdus() == CLEAR_ROCKER_A
    raw = mc.raw_model(bench.reload().cdb.element(ROCKER_A), "1001")  # type: ignore[arg-type]
    assert raw["subscribe"] == [label, "8123", "FFFD"]
    assert caplog.text.count("cannot be removed from here") == 3


async def test_socket_target_binds_the_property_server_before_its_publication(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """Review-3 W10: a socket element whose User Property server is not bound to the AppKey gets the Model App
    Bind before its Publication Set — a model publishes with the key it is bound to, as the app binds first."""

    def edit(net: dict[str, Any]) -> None:
        drop_sensor_publication(net)
        socket = next(n for n in net["nodes"] if n["unicastAddress"] == "0172")
        for m in socket["elements"][1]["models"]:
            if m["modelId"] == "05271013":
                m["bind"] = []

    bench = await prepare(tmp_path, "unbound", lambda doc: edited_network(doc, edit))
    await bench.configurator.assign_key(ROCKER_A, element=SOCKET_NODE)
    pdus = bench.config_pdus()
    i = pdus.index((SOCKET_NODE, pub_set(SOCKET_SENSOR, SENSOR_GROUP, "05271013")))
    assert pdus[i - 1] == (
        SOCKET_NODE,
        C.model_app_bind(SOCKET_SENSOR, "05271013", 0),
    )
    el = bench.reload().cdb.element(SOCKET_SENSOR)
    assert el is not None
    assert mc.raw_model(el, "05271013")["bind"] == [0]


# ----------------------------------------------------------------------------- rooms


async def test_set_room_moves_a_load_between_rooms(bench: Bench) -> None:
    """The DALI light leaves Living room and joins WC — which also wires it to the WC-linked dimmer key."""
    assert await bench.configurator.set_room(DALI_LOAD, "WC") is True
    # joining first: the OnOff / Level servers, then the same for the WC-linked key's publish group;
    # leaving last: every server that carried the old room (app-provisioned loads have them all on it)
    assert bench.config_pdus() == [
        (DALI_NODE, sub_add(DALI_LOAD, WC, "1000")),
        (DALI_NODE, sub_add(DALI_LOAD, WC, "1002")),
        (DALI_NODE, sub_add(DALI_LOAD, DIMMER_KEY_GROUP, "1000")),
        (DALI_NODE, sub_add(DALI_LOAD, DIMMER_KEY_GROUP, "1002")),
        *[
            (DALI_NODE, sub_del(DALI_LOAD, LIVING, m))
            for m in ("1000", "1002", "1300", "1301", "1303", "1304", "1203", "1204")
        ],
    ]
    assert bench.config_pdus()[0][1] == bytes.fromhex("801b 3202 0fc0 0010")
    pf = bench.reload()
    assert subs(pf, DALI_LOAD, "1000") == [DALI_GROUP, LAMPS, WC, DIMMER_KEY_GROUP]
    assert subs(pf, DALI_LOAD, "1002") == [DALI_GROUP, LAMPS, WC, DIMMER_KEY_GROUP]
    assert subs(pf, DALI_LOAD, "1300") == [
        DALI_GROUP,
        LAMPS,
    ]  # left the old room, not a membership model
    assert pf.meta == ProjectFile.load(ANDROID_PATH).meta
    # the device model derives the room from the export again
    cdb = CDB.load(bench.path)
    el = cdb.element(DALI_LOAD)
    assert el is not None
    assert [cdb.groups[a] for a in el.subscriptions("1000") if a in (WC, LIVING)] == [
        "WC"
    ]


async def test_set_room_creates_a_missing_room(bench: Bench) -> None:
    await bench.configurator.set_room(SOCKET_NODE, "Garage")
    pf = bench.reload()
    assert (
        pf.user_groups()[NEW_ROOM] == "Garage"
    )  # lowest free address of the provisioner's range
    assert pf.meta["userGroups"][-1] == {
        "name": "Garage",
        "address": NEW_ROOM,
        "icon": "ic_group_ground_plan",
    }
    assert bench.config_pdus() == [
        (SOCKET_NODE, sub_add(SOCKET_NODE, NEW_ROOM, "1000")),
        *[
            (SOCKET_NODE, sub_del(SOCKET_NODE, KITCHEN, m))
            for m in ("1000", "1004", "1006", "1007", "1203", "1204")
        ],
    ]
    assert subs(pf, SOCKET_NODE, "1000") == [SOCKET_GROUP, SOCKETS, NEW_ROOM]


async def test_set_room_is_idempotent(bench: Bench) -> None:
    """CFG-14: nothing to send and nothing changed: no rewrite, no upload, no reload (the call returns False)."""
    assert await bench.configurator.set_room(SWITCH_LOAD, "WC") is False
    assert bench.config_pdus() == []
    assert bench.file_unchanged()
    assert bench.hub.hass.jobs == ["load_project"]
    assert bench.reload().cdb.groups == CDB.load(ANDROID_PATH).groups


async def test_set_room_on_a_raw_cdb_writes_the_cdb_flavour(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    bench = await make_bench(tmp_path, Path(CDB_PATH), META_DIR)
    await bench.configurator.set_room(ACTUATOR_OUT2, "WC")
    assert bench.config_pdus() == [
        (ACTUATOR_NODE, sub_add(ACTUATOR_OUT2, WC, "1000")),
        *[
            (ACTUATOR_NODE, sub_del(ACTUATOR_OUT2, KITCHEN, m))
            for m in ("1000", "1004", "1006", "1007", "1203", "1204")
        ],
    ]
    doc = json.loads(bench.path.read_text())
    assert set(doc) == {"meshNetwork"}
    pf = bench.reload()
    assert subs(pf, ACTUATOR_OUT2, "1000") == [0xC081, LAMPS, WC]
    assert subs(pf, ACTUATOR_OUT1, "1000") == [0xC080, LAMPS, KITCHEN]
    assert bench.hub.hass.jobs == ["load_project", "save"]


async def test_create_rename_delete_room(bench: Bench) -> None:
    assert await bench.configurator.create_room("Attic") == NEW_ROOM
    assert bench.config_pdus() == []
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.create_room("attic")
    assert exc.value.translation_key == "service_room_exists"
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.create_room("  ")
    assert exc.value.translation_key == "service_invalid_room_name"

    assert await bench.configurator.rename_room("Attic", "Loft") is True
    pf = bench.reload()
    assert pf.user_groups()[NEW_ROOM] == "Loft"
    assert (
        next(g for g in pf.meta["userGroups"] if g["address"] == NEW_ROOM)["name"]
        == "Loft"
    )
    for bad, key in (("wc", "service_room_exists"), (" ", "service_invalid_room_name")):
        with pytest.raises(ServiceValidationError) as exc:
            await bench.configurator.rename_room("Loft", bad)
        assert exc.value.translation_key == key
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.rename_room("Cellar", "X")
    assert exc.value.translation_key == "service_no_room"

    assert await bench.configurator.delete_room("Loft") is True
    assert bench.config_pdus() == []
    assert NEW_ROOM not in bench.reload().cdb.groups
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.delete_room("Loft")
    assert exc.value.translation_key == "service_no_room"


@pytest.mark.parametrize(
    "name",
    [
        "element group #0x148",
        "device type group lamps",
        "#time_keeper_group#",
        " element group #1 ",
    ],
    ids=["element_group", "device_type_group", "time_keeper", "padded"],
)
async def test_room_names_the_mesh_reserves_are_refused(
    bench: Bench, name: str
) -> None:
    """A room called like an internal group would vanish from the app's room list (`export.is_room`)."""
    for op in (
        lambda: bench.configurator.create_room(name),
        lambda: bench.configurator.rename_room("WC", name),
        lambda: bench.configurator.set_room(SWITCH_LOAD, name),
    ):
        with pytest.raises(ServiceValidationError) as exc:
            await op()
        assert exc.value.translation_key == "service_room_name_reserved"
        assert exc.value.translation_placeholders == {"name": name.strip()}
    assert bench.file_unchanged()
    assert bench.config_pdus() == []


async def test_room_range_exhaustion_is_a_translated_error(bench: Bench) -> None:
    """Every address of the provisioner's group range taken: `service_room_range_full`, nothing written."""
    pf = bench.reload()
    low, high = pf._provisioner_range("Group") or (0xC000, 0xFEF4)
    for addr in range(low, high + 1):
        if addr not in pf.cdb.groups:
            pf.add_group(f"Room {addr:04X}", addr)
    pf.save()
    original = bench.path.read_bytes()
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.create_room("One more")
    assert exc.value.translation_key == "service_room_range_full"
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.set_room(SWITCH_LOAD, "One more")
    assert exc.value.translation_key == "service_room_range_full"
    assert bench.path.read_bytes() == original
    assert bench.config_pdus() == []


async def test_a_crowded_range_is_a_translated_error(bench: Bench) -> None:
    """Review-4 W4-2: Home Assistant's rooms and scenes come from the top of the app's ranges; once fewer than
    `ALLOCATION_MARGIN` free numbers are left below the next one, the app's own next ones would reach it: refused
    (`service_room_range_crowded` / `service_scene_range_crowded`), nothing written."""
    pf = bench.reload()
    low, high = pf._provisioner_range("Group") or (0xC000, 0xFEF4)
    for addr in range(low + 0x20, high + 1):
        if addr not in pf.cdb.groups:
            pf.add_group(f"Room {addr:04X}", addr)
    pf.save()
    original = bench.path.read_bytes()
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.create_room("One more")
    assert exc.value.translation_key == "service_room_range_crowded"
    assert int(exc.value.translation_placeholders["free"]) < ALLOCATION_MARGIN
    with (
        patch.object(
            ProjectFile,
            "free_scene_number",
            side_effect=AllocationCrowded("scene number", 0x30, 20),
        ),
        pytest.raises(ServiceValidationError) as exc,
    ):
        await bench.configurator.create_scene("One more")
    assert exc.value.translation_key == "service_scene_range_crowded"
    assert exc.value.translation_placeholders == {"free": "20"}
    assert bench.path.read_bytes() == original
    assert bench.config_pdus() == []


async def test_delete_room_unwires_members_and_linked_keys(bench: Bench) -> None:
    """WC holds 0148 and 0300 and is linked to the dimmer key 0301: the key is cleared, the members leave."""
    await bench.configurator.delete_room("WC")
    assert bench.config_pdus() == [
        # RemoveConnectionForAddress.GroupConnection + AllConnections on the linked key
        (SWITCH_NODE, sub_del(SWITCH_LOAD, DIMMER_KEY_GROUP, "1000")),
        (DIMMER_NODE, sub_del(DIMMER_LOAD, DIMMER_KEY_GROUP, "1000")),
        (DIMMER_NODE, sub_del(DIMMER_LOAD, DIMMER_KEY_GROUP, "1002")),
        (DIMMER_NODE, pub_set(DIMMER_KEY, 0x0000, "1001")),
        (DIMMER_NODE, sub_del(DIMMER_KEY, DIMMER_KEY_GROUP, "1001")),
        (DIMMER_NODE, pub_set(DIMMER_KEY, 0x0000, "05271015")),
        (DIMMER_NODE, sub_del(DIMMER_KEY, DIMMER_KEY_GROUP, "05271015")),
        # DeleteGroupFromDevices: every model of a member that carried the room address leaves it
        *[
            (SWITCH_NODE, sub_del(SWITCH_LOAD, WC, m))
            for m in ("1000", "1004", "1006", "1007", "1203", "1204")
        ],
        *[
            (DIMMER_NODE, sub_del(DIMMER_LOAD, WC, m))
            for m in ("1000", "1002", "1300", "1301", "1203", "1204")
        ],
    ]
    assert bench.app_pdus() == []
    pf = bench.reload()
    assert WC not in pf.cdb.groups
    assert not any(g["address"] == WC for g in pf.meta["userGroups"])
    assert link_rows(pf) == []
    assert subs(pf, SWITCH_LOAD, "1000") == [0xC061, LAMPS]


# ----------------------------------------------------------------------------- failure paths


async def test_a_refused_config_status_stops_the_plan_and_records_what_was_applied(
    bench: Bench,
) -> None:
    """Apply-and-record: the 4th of 8 messages is refused — the plan stops there (no roll-back, nothing after
    it, no KeyMode, no property-mode reset), and the export records exactly the three steps the node took."""
    refused = sub_add(ROCKER_A, DIMMER_GROUP, "1003")
    bench.config.refuse[refused] = 0x08  # Not a Subscribe Model
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert exc.value.translation_key == "service_config_refused"
    assert exc.value.translation_placeholders == {
        "node": "0232",
        "message": "Config Model Subscription Add elem=0234 address=C070 model=1003",
        "status": "Not a Subscribe Model",
        "applied": mc.applied_text(3, 8),
    }
    assert bench.config_pdus() == [*WIRE_ROCKER_A_TO_DIMMER[:4]]  # nothing after it
    assert bench.app_pdus() == []  # the reset and KeyMode only follow a complete plan
    assert bench.keys.modes == {}
    # the file holds what the mesh holds: the three accepted steps, nothing of the rest
    assert not bench.file_unchanged()
    pf = bench.reload()
    assert pub(pf, ROCKER_A, "1001") == DIMMER_GROUP
    assert subs(pf, ROCKER_A, "1001") == [
        DALI_GROUP,
        DIMMER_GROUP,
    ]  # the old link was not cleared yet
    assert pub(pf, ROCKER_A, "1003") == DIMMER_GROUP
    assert subs(pf, ROCKER_A, "1003") == []  # the refused step is not recorded
    assert pub(pf, ROCKER_A, "05271015") == DALI_GROUP
    assert subs(pf, ROCKER_A, "05271015") == [DALI_GROUP]
    assert pf.meta == ProjectFile.load(ANDROID_PATH).meta
    assert bench.hub.hass.jobs == ["load_project", "load_project", "save"]
    # running it again with the same target completes the plan (every message is idempotent)
    del bench.config.refuse[refused]
    bench.link.net_pdus.clear()
    assert await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    pf = bench.reload()
    for model in ("1001", "1003", "05271015"):
        assert pub(pf, ROCKER_A, model) == DIMMER_GROUP
        assert subs(pf, ROCKER_A, model) == [DIMMER_GROUP]
    assert bench.keys.modes == {ROCKER_A: b"\x00"}


async def test_a_silent_node_stops_the_plan_and_records_what_was_applied(
    bench: Bench,
) -> None:
    """The silent twin: the dimmer never answers the 2nd of 11 messages; the switch's subscription (1st) is
    recorded, and so is the room link's row (review-3 W5: without it, no later clear of the key would unsubscribe
    the switch from the key's group again); the error says what was applied."""
    silent = sub_add(DIMMER_LOAD, ROCKER_A_GROUP, "1000")
    bench.config.silent.add(silent)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, room="WC")
    assert exc.value.translation_key == "service_no_reply"
    assert exc.value.translation_placeholders == {
        "node": "0300",
        "message": "Config Model Subscription Add elem=0300 address=C04F model=1000",
        "applied": mc.applied_text(1, 11),
    }
    sent = [a for _n, a in bench.config_pdus()]
    assert sent.count(silent) == mc.CONFIG_RETRIES
    assert sent[-1] == silent
    assert bench.app_pdus() == []
    pf = bench.reload()
    assert subs(pf, SWITCH_LOAD, "1000") == [
        0xC061,
        LAMPS,
        WC,
        DIMMER_KEY_GROUP,
        ROCKER_A_GROUP,
    ]
    assert subs(pf, DIMMER_LOAD, "1000") == [DIMMER_GROUP, LAMPS, WC, DIMMER_KEY_GROUP]
    assert pub(pf, ROCKER_A, "1001") == DALI_GROUP  # the key itself was not touched yet
    assert link_rows(pf)[-1] == {
        "elementAddress": ROCKER_A,
        "groupAddress": WC,
        "publishAddress": ROCKER_A_GROUP,
        "function": "LIGHT",
    }
    assert len(link_rows(pf)) == 2  # beside the dimmer key's
    # so clearing the key unwires the switch the stopped link did reach
    bench.config.silent.clear()
    bench.link.net_pdus.clear()
    await bench.configurator.clear_key(ROCKER_A)
    assert (SWITCH_NODE, sub_del(SWITCH_LOAD, ROCKER_A_GROUP, "1000")) in (
        bench.config_pdus()
    )
    pf = bench.reload()
    assert ROCKER_A_GROUP not in subs(pf, SWITCH_LOAD, "1000")
    assert [r["elementAddress"] for r in link_rows(pf)] == [DIMMER_KEY]


async def test_a_stopped_room_relink_keeps_the_old_links_row_beside_the_new_one(
    bench: Bench,
) -> None:
    """Review-3 W5: the WC-linked dimmer key is moved to the kitchen and the second kitchen light never answers.
    The record holds the new link's row (the first kitchen light listens to the key now) and still the old one
    (the WC loads were not unwired yet); running it again leaves the kitchen row alone."""
    silent = sub_add(ACTUATOR_OUT2, DIMMER_KEY_GROUP, "1000")
    bench.config.silent.add(silent)
    with pytest.raises(HomeAssistantError):
        await bench.configurator.assign_key(DIMMER_KEY, room="Kitchen")
    pf = bench.reload()
    assert DIMMER_KEY_GROUP in subs(pf, ACTUATOR_OUT1, "1000")
    assert [(r["groupAddress"], r["function"]) for r in link_rows(pf)] == [
        (WC, "LIGHT"),
        (KITCHEN, "LIGHT"),
    ]
    bench.config.silent.clear()
    await bench.configurator.assign_key(DIMMER_KEY, room="Kitchen")
    pf = bench.reload()
    assert [(r["groupAddress"], r["function"]) for r in link_rows(pf)] == [
        (KITCHEN, "LIGHT")
    ]
    assert DIMMER_KEY_GROUP not in subs(pf, SWITCH_LOAD, "1000")
    assert DIMMER_KEY_GROUP in subs(pf, ACTUATOR_OUT2, "1000")


async def test_a_stopped_plan_into_a_new_room_records_the_room(bench: Bench) -> None:
    """`set_rooms` creates a missing room in the planned copy only; a stop must not record subscriptions to a
    group `_record`'s fresh read of the file has never heard of (CFG-02) — the next room HA or the app creates
    would otherwise take that same address and silently inherit these loads."""
    group = mc.load_project(str(bench.path), None).free_group_address()
    silent = sub_add(DIMMER_LOAD, group, "1000")
    bench.config.silent.add(silent)
    with pytest.raises(HomeAssistantError):
        await bench.configurator.set_rooms([SWITCH_LOAD, DIMMER_LOAD], "Garage")
    pf = bench.reload()
    assert group in subs(pf, SWITCH_LOAD, "1000")
    assert pf.user_groups().get(group) == "Garage"
    assert any(as_int(g.get("address")) == group for g in pf.meta["userGroups"])


async def test_a_silent_first_message_leaves_the_file_alone(bench: Bench) -> None:
    silent = sub_add(SWITCH_LOAD, ROCKER_A_GROUP, "1000")
    bench.config.silent.add(silent)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, room="WC")
    assert exc.value.translation_key == "service_no_reply"
    assert exc.value.translation_placeholders["applied"] == mc.APPLIED_NOTHING
    assert bench.file_unchanged()
    assert bench.hub.hass.jobs == ["load_project"]


async def test_a_stopped_clear_keeps_the_room_link_until_every_load_is_unwired(
    bench: Bench,
) -> None:
    """Clearing the WC-linked dimmer key: the first unsubscribe is taken, the second refused. 0300 still listens
    to the key's group, so the record keeps the link row instead of dropping it — otherwise a rerun would never
    unwire it (`_unlink_room_steps` only runs for a key that still has a row)."""
    refused = sub_del(DIMMER_LOAD, DIMMER_KEY_GROUP, "1000")
    bench.config.refuse[refused] = 0x05  # Insufficient Resources
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.clear_key(DIMMER_KEY)
    assert exc.value.translation_placeholders["applied"] == mc.applied_text(1, 7)
    pf = bench.reload()
    assert [r["elementAddress"] for r in link_rows(pf)] == [DIMMER_KEY]
    assert subs(pf, SWITCH_LOAD, "1000") == [0xC061, LAMPS, WC]
    assert subs(pf, DIMMER_LOAD, "1000") == [DIMMER_GROUP, LAMPS, WC, DIMMER_KEY_GROUP]
    assert pub(pf, DIMMER_KEY, "1001") == DIMMER_KEY_GROUP
    # rerun: the row is still there, so this time every remaining listener is unsubscribed
    del bench.config.refuse[refused]
    bench.link.net_pdus.clear()
    await bench.configurator.clear_key(DIMMER_KEY)
    assert (DIMMER_NODE, sub_del(DIMMER_LOAD, DIMMER_KEY_GROUP, "1000")) in (
        bench.config_pdus()
    )
    reloaded = bench.reload()
    assert DIMMER_KEY_GROUP not in subs(reloaded, DIMMER_LOAD, "1000")
    assert DIMMER_KEY_GROUP not in subs(reloaded, DIMMER_LOAD, "1002")
    assert link_rows(reloaded) == []


async def test_record_drops_a_keys_link_row_once_every_tagged_step_of_the_plan_is_done(
    bench: Bench,
) -> None:
    """`_record`'s own bookkeeping, exercised directly: `ordered()` always puts a key's tagged (destructive)
    steps last, so through `assign_key`/`clear_key` a stopped plan never has all of a key's tagged steps done
    with something else still pending — `finished` is always empty there. A future caller mixing more than one
    key's tagged steps in one plan would not be; this keeps that branch covered against it."""
    pf = await bench.configurator._load()
    key = bench.configurator._element(pf, DIMMER_KEY)
    accepted = bench.configurator._clear_steps(pf, key)
    extra = mc.ConfigStep(
        SOCKET_NODE,
        b"",
        0,
        change=mc.ModelChange(SOCKET_NODE, "1000", SOCKET_GROUP, "subscribe"),
    )
    await bench.configurator._record(accepted, [*accepted, extra])
    assert link_rows(bench.reload()) == []


async def test_a_record_that_cannot_be_written_is_logged_and_chained(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    refused = sub_add(ROCKER_A, DIMMER_GROUP, "1003")
    bench.config.refuse[refused] = 0x08
    original_save = ProjectFile.save

    def refuse_save(self: ProjectFile, *args: Any, **kwargs: Any) -> Path:
        if self.path == bench.path:
            raise OSError("read-only")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(ProjectFile, "save", refuse_save)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert exc.value.translation_key == "service_config_refused"
    assert isinstance(exc.value.__cause__, HomeAssistantError)
    assert exc.value.__cause__.translation_key == "service_export_write_failed"
    assert "could not be recorded" in caplog.text
    assert bench.file_unchanged()


async def test_a_duplicated_status_of_the_previous_step_cannot_mask_a_refusal(
    bench: Bench,
) -> None:
    """A late reply to step k, retransmitted, arrives right before step k+1's Status. Matched on node + opcode
    alone it would acknowledge step k+1; matched on element / address / model it is dropped and the refusal
    of k+1 is reported."""
    refused = sub_del(DALI_LOAD, LIVING, "1002")
    bench.config.replay_before.add(
        refused
    )  # preceded by a copy of the 1000 delete's Status
    bench.config.refuse[refused] = 0x05
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.set_room(DALI_LOAD, "WC")
    assert exc.value.translation_key == "service_config_refused"
    assert exc.value.translation_placeholders["message"] == (
        "Config Model Subscription Delete elem=0232 address=C010 model=1002"
    )
    assert exc.value.translation_placeholders["applied"] == mc.applied_text(5, 12)
    pf = bench.reload()
    assert subs(pf, DALI_LOAD, "1000") == [DALI_GROUP, LAMPS, WC, DIMMER_KEY_GROUP]
    assert subs(pf, DALI_LOAD, "1002") == [
        DALI_GROUP,
        LAMPS,
        LIVING,
        WC,
        DIMMER_KEY_GROUP,
    ]  # the refused delete is not recorded


async def test_a_malformed_config_status_counts_as_a_refusal(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mc.C, "decode_config", lambda _op, _p: None)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.set_room(DALI_LOAD, "WC")
    assert exc.value.translation_key == "service_config_refused"
    assert exc.value.translation_placeholders["status"] == "malformed status"
    assert bench.file_unchanged()


async def test_a_short_config_status_counts_as_a_refusal(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    def truncated(_op: int, _p: bytes) -> None:
        raise ValueError("short")

    monkeypatch.setattr(mc.C, "decode_config", truncated)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.set_room(DALI_LOAD, "WC")
    assert exc.value.translation_placeholders["status"] == "malformed status"


async def test_key_mode_is_read_back_when_the_set_is_not_answered(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    bench = await make_bench(tmp_path, answer_sets=False)
    await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert bench.app_pdus()[-2:] == [
        (ROCKER_A, admin_set(0x5003, b"\x00")),
        (ROCKER_A, M.vendor_property_get("admin", 0x5003)),
    ]
    assert not bench.file_unchanged()


async def test_key_mode_not_taken_aborts(tmp_path: Path, fast: FastAsyncio) -> None:
    bench = await make_bench(tmp_path, answer_sets=False)
    bench.keys.modes[ROCKER_A] = (
        b"\x06"  # the key reports the old mode, whatever we write
    )
    original_respond = bench.keys.respond

    def stubborn(n: NetworkPDU) -> None:
        original_respond(n)
        bench.keys.modes[ROCKER_A] = b"\x06"

    bench.link.responders[bench.link.responders.index(original_respond)] = stubborn
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert exc.value.translation_key == "service_key_mode_not_applied"
    assert exc.value.translation_placeholders == {"address": "0234", "mode": "light"}
    # every Config step was accepted: the wiring is recorded, only the mode is missing
    assert not bench.file_unchanged()
    assert pub(bench.reload(), ROCKER_A, "1001") == DIMMER_GROUP
    assert bench.hub.hass.jobs == ["load_project", "save"]


async def test_key_silent_on_key_mode_aborts(tmp_path: Path, fast: FastAsyncio) -> None:
    bench = await make_bench(tmp_path, answer_sets=False)
    bench.keys.answer_gets = False
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert exc.value.translation_key == "service_no_reply"
    assert exc.value.translation_placeholders["node"] == "0234"
    assert exc.value.translation_placeholders["applied"] == mc.APPLIED_KEY_WIRED
    assert not bench.file_unchanged()
    assert pub(bench.reload(), ROCKER_A, "05271015") == DIMMER_GROUP


async def test_key_mode_status_for_another_property_triggers_the_read_back(
    bench: Bench,
) -> None:
    original_reply = bench.keys.reply
    replies: list[int] = []

    def odd_reply(element: int, prop: int, value: bytes) -> None:
        if prop != 0x5003:
            return  # the reset writes go unanswered here
        replies.append(prop)
        if len(replies) == 1:
            original_reply(
                element, 0x5006, b"\x00\x00\x00"
            )  # a stray status of the reset write
        else:
            original_reply(element, prop, value)

    bench.keys.reply = odd_reply  # type: ignore[method-assign]
    await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert replies == [0x5003, 0x5003]
    assert bench.app_pdus()[-1] == (ROCKER_A, M.vendor_property_get("admin", 0x5003))


async def test_a_lost_link_surfaces_as_send_failed(bench: Bench) -> None:
    bench.link.write_error = ConnectionError("gone")
    for op, message in (
        (
            bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD),
            (
                "Config Model Publication Set elem=0234 publish=C070 model=1001 appkey=0 cred=0 ttl=255 "
                "period=0/0 retx=0/0"
            ),
        ),
        (
            bench.configurator.set_room(DALI_LOAD, "WC"),
            "Config Model Subscription Add elem=0232 address=C00F model=1000",
        ),
    ):
        with pytest.raises(HomeAssistantError) as exc:
            await op
        assert exc.value.translation_key == "service_send_failed"
        assert exc.value.translation_placeholders == {
            "node": "0232",
            "message": message,
            "applied": mc.APPLIED_NOTHING,
        }
    assert bench.file_unchanged()


async def test_a_lost_link_during_the_key_mode_write(bench: Bench) -> None:
    calls = 0
    original = bench.link.write_gatt_char

    async def flaky(char: str, data: bytes, response: bool | None = None) -> None:
        nonlocal calls
        calls += 1
        if (
            data[1:]
            and bench.link.net_pdus
            and len(bench.link.sent_access()) >= 3
            and len(bench.config_pdus()) >= 8
        ):
            raise ConnectionError("gone")
        await original(char, data, response)

    bench.link.write_gatt_char = flaky  # type: ignore[method-assign]
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert exc.value.translation_key == "service_send_failed"
    assert exc.value.translation_placeholders == {
        "node": "0234",
        "message": "LBC Admin Property Set prop 0x5003 access=3 value=00 key_mode=light",
        "applied": mc.APPLIED_KEY_WIRED,
    }
    assert bench.app_pdus() == [
        (ROCKER_A, p) for p in RESET_PROPERTY_MODE
    ]  # the write died
    assert not bench.file_unchanged()  # the wiring is recorded
    assert subs(bench.reload(), ROCKER_A, "1003") == [DIMMER_GROUP]


async def test_a_lost_link_during_the_property_mode_reset(bench: Bench) -> None:
    original = bench.link.write_gatt_char

    async def flaky(char: str, data: bytes, response: bool | None = None) -> None:
        if data[1:] and bench.link.net_pdus and len(bench.config_pdus()) >= 8:
            raise ConnectionError("gone")
        await original(char, data, response)

    bench.link.write_gatt_char = flaky  # type: ignore[method-assign]
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert exc.value.translation_key == "service_send_failed"
    assert exc.value.translation_placeholders["applied"] == mc.APPLIED_KEY_WIRED
    assert exc.value.translation_placeholders["message"].startswith(
        "LBC Admin Property Set Unack prop 0x5006"
    )
    assert not bench.file_unchanged()


async def test_a_newer_export_on_disk_is_never_overwritten(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app rewrote the file while we were talking to the mesh: the write is refused, nothing is lost."""
    original_load = mc.load_project

    def load_then_app_writes(path: str, metadata_dir: str | None) -> ProjectFile:
        pf = original_load(path, metadata_dir)
        newer = ProjectFile.load(Path(path))
        newer.add_group("From the app")
        newer.save(force=True)  # bumps the timestamp past ours
        return pf

    monkeypatch.setattr(mc, "load_project", load_then_app_writes)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.set_room(DALI_LOAD, "WC")
    assert exc.value.translation_key == "service_export_newer"
    assert exc.value.translation_placeholders == {"path": str(bench.path)}
    assert isinstance(exc.value.__cause__, mc.NewerExportError)
    assert "From the app" in bench.reload().user_groups().values()
    assert subs(bench.reload(), DALI_LOAD, "1000") == [DALI_GROUP, LAMPS, LIVING]
    assert (
        bench.config_pdus() != []
    )  # the mesh was configured; a retry re-sends the same messages


async def test_unreadable_or_unwritable_export(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench.path.write_text("{}")
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("X")
    assert exc.value.translation_key == "service_export_load_failed"
    assert exc.value.translation_placeholders["path"] == str(bench.path)
    assert (
        exc.value.translation_placeholders["error"] == "not a JUNG HOME mesh export"
    )  # the loader's own message (one parser, MOD-11)
    # a document with the wrong types: the loader's own message; an unreadable file: the Python error, named
    doc = json.loads(bench.original)
    inner = json.loads(base64.b64decode(doc["network"]))
    inner["nodes"][0]["unicastAddress"] = 5
    doc["network"] = base64.b64encode(json.dumps(inner).encode()).decode()
    bench.path.write_text(json.dumps(doc))
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("X")
    assert exc.value.translation_key == "service_export_load_failed"
    assert exc.value.translation_placeholders["error"] == (
        "nodes[0] unicastAddress is not a string"
    )
    bench.path.unlink()
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("X")
    assert exc.value.translation_key == "service_export_load_failed"
    assert exc.value.translation_placeholders["error"].startswith("FileNotFoundError: ")
    bench.path.write_bytes(bench.original)

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(ProjectFile, "save", refuse)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("X")
    assert exc.value.translation_key == "service_export_write_failed"
    assert exc.value.translation_placeholders["error"] == "read-only"


async def test_operations_are_serialised_by_the_lock(bench: Bench) -> None:
    gate = asyncio.Event()
    order: list[str] = []
    original_load = bench.configurator._load

    async def slow_load() -> ProjectFile:
        order.append("load")
        if len(order) == 1:
            await gate.wait()
        return await original_load()

    bench.configurator._load = slow_load  # type: ignore[method-assign]
    first = asyncio.ensure_future(bench.configurator.create_room("One"))
    second = asyncio.ensure_future(bench.configurator.create_room("Two"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert order == ["load"]  # the second waits for the lock
    assert bench.configurator.lock.locked()
    gate.set()
    assert await first == NEW_ROOM
    assert await second == NEW_ROOM - 1
    assert order == ["load", "load"]
    assert list(bench.reload().user_groups().values())[-2:] == ["One", "Two"]


# ----------------------------------------------------------------------------- helpers


def test_element_groups_fall_back_to_meta_rows(tmp_path: Path) -> None:
    pf = ProjectFile.load(ANDROID_PATH)
    groups = mc.element_groups(pf)
    assert groups[ROCKER_A] == ROCKER_A_GROUP
    assert groups[GATEWAY] == GATEWAY_GROUP
    # a CDB group renamed by hand: the meta row still maps it
    pf.net["groups"] = [
        g if g["address"] != "C04F" else {**g, "name": "renamed"}
        for g in pf.net["groups"]
    ]
    pf.cdb.groups[ROCKER_A_GROUP] = "renamed"
    assert mc.element_groups(pf)[ROCKER_A] == ROCKER_A_GROUP
    # a meta row for a group the CDB does not have is ignored
    pf.meta["elementConnectionGroups"].append(
        {"elementAddress": 0x0999, "groupAddress": 0xC0FF}
    )
    assert 0x0999 not in mc.element_groups(pf)
    pf.meta["elementConnectionGroups"].append(
        {"elementAddress": "zz", "groupAddress": True}
    )
    assert mc.element_groups(pf)[ROCKER_A] == ROCKER_A_GROUP


def test_confirms_key_mode() -> None:
    assert mc._confirms_key_mode(bytes.fromhex("0350 03 05"), 5)
    assert not mc._confirms_key_mode(bytes.fromhex("0350 03 05"), 0)
    assert not mc._confirms_key_mode(bytes.fromhex("0650 03 05"), 5)
    assert not mc._confirms_key_mode(bytes.fromhex("0350 03"), 5)


def test_function_code_and_as_int() -> None:
    assert function_code("light_and_switch") == 6
    assert function_code(3) == 3
    assert function_code("nonsense") is None
    assert as_int("C00F") == 0xC00F
    assert as_int(True) is None
    assert as_int(1.5) is None
    assert as_int("zz") is None


def test_config_step_describes_itself() -> None:
    step = mc.ConfigStep(
        DALI_NODE,
        sub_add(ROCKER_A, DIMMER_GROUP, "1001"),
        C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
    )
    assert (
        step.what == "Config Model Subscription Add elem=0234 address=C070 model=1001"
    )


# `_matches_function` no longer has an HA-side copy (MOD-04): it is `ProjectFile._matches_function`, tested
# directly in tests/jhmesh/test_export.py::test_matches_function_uses_the_device_kind.


# ----------------------------------------------------------------------------- edge cases of the app's bookkeeping


def edited_network(doc: dict[str, Any], edit: Callable[[dict[str, Any]], None]) -> None:
    """Apply `edit` to the CDB inside a share export's Base64 `network`."""
    net = json.loads(base64.b64decode(doc["network"]))
    edit(net)
    doc["network"] = base64.b64encode(json.dumps(net).encode()).decode()


async def prepare(
    tmp_path: Path, name: str, edit: Callable[[dict[str, Any]], None]
) -> Bench:
    """A bench on a copy of the Android export with `edit` applied to its parsed document."""
    source = tmp_path / name / ANDROID_PATH.name
    source.parent.mkdir()
    doc = json.loads(ANDROID_PATH.read_text())
    edit(doc)
    source.write_text(json.dumps(doc))
    return await make_bench(tmp_path, source)


def drop_sensor_publication(net: dict[str, Any]) -> None:
    socket = next(n for n in net["nodes"] if n["unicastAddress"] == "0172")
    for m in socket["elements"][1]["models"]:
        if m["modelId"] == "05271013":
            del m["publish"]


async def test_clear_key_drops_a_link_row_it_cannot_unwire(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """A cached room link without a publish group, on a key without an element group: nothing to send, row gone."""

    def edit(doc: dict[str, Any]) -> None:
        doc["meta"]["devices"][2]["cachedGroupConnectionMetadata"] = [
            {"elementAddress": DALI_AUX, "groupAddress": WC, "function": "LIGHT"}
        ]

    bench = await prepare(tmp_path, "stale-link", edit)
    await bench.configurator.clear_key(DALI_AUX)
    assert bench.config_pdus() == []
    assert [r["elementAddress"] for r in link_rows(bench.reload())] == [DIMMER_KEY]


async def test_room_link_needs_the_keys_element_group(bench: Bench) -> None:
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.assign_key(DALI_AUX, room="WC")
    assert exc.value.translation_key == "service_no_element_group"
    assert exc.value.translation_placeholders == {"address": "0236"}
    assert bench.config_pdus() == []
    assert bench.file_unchanged()


async def test_socket_target_repairs_a_missing_property_publication(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """A socket whose sensor element lost its User Property publication gets it back (Publication Set)."""
    bench = await prepare(
        tmp_path, "socket", lambda doc: edited_network(doc, drop_sensor_publication)
    )
    await bench.configurator.assign_key(ROCKER_A, element=SOCKET_NODE)
    pdus = bench.config_pdus()
    i = pdus.index((SOCKET_NODE, pub_set(SOCKET_SENSOR, SENSOR_GROUP, "05271013")))
    assert pdus[i + 1] == (DALI_NODE, sub_add(ROCKER_A, SENSOR_GROUP, "1001"))
    assert pub(bench.reload(), SOCKET_SENSOR, "05271013") == SENSOR_GROUP


async def test_socket_target_skips_an_element_without_a_group(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    def edit(doc: dict[str, Any]) -> None:
        edited_network(
            doc,
            lambda net: net["groups"].remove(
                next(g for g in net["groups"] if g["address"] == "C001")
            ),
        )
        doc["meta"]["elementConnectionGroups"] = [
            e
            for e in doc["meta"]["elementConnectionGroups"]
            if e["groupAddress"] != SENSOR_GROUP
        ]

    bench = await prepare(tmp_path, "no-sensor-group", edit)
    await bench.configurator.assign_key(ROCKER_A, element=SOCKET_NODE)
    assert not any(
        a[2:4] == SOCKET_SENSOR.to_bytes(2, "little") for _n, a in bench.config_pdus()
    )
    assert subs(bench.reload(), ROCKER_A, "1001") == [SOCKET_GROUP]


async def test_delete_room_ignores_a_link_row_of_a_vanished_key(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    def edit(doc: dict[str, Any]) -> None:
        doc["meta"]["devices"][2]["cachedGroupConnectionMetadata"] = [
            {
                "elementAddress": 0x0999,
                "groupAddress": KITCHEN,
                "publishAddress": 0xC0FF,
                "function": "LIGHT",
            }
        ]

    bench = await prepare(tmp_path, "vanished", edit)
    await bench.configurator.delete_room("Kitchen")
    pf = bench.reload()
    assert KITCHEN not in pf.cdb.groups
    assert [r["elementAddress"] for r in link_rows(pf)] == [DIMMER_KEY]
    assert (SOCKET_NODE, sub_del(SOCKET_NODE, KITCHEN, "1000")) in bench.config_pdus()


# ----------------------------------------------------------------------------- scenes (roadmap step 12)


class SceneServer:
    """The Scene Setup Servers and JUNG Scene Action Setup servers of the load elements.

    Keeps a scene register and an action record per element; answers Scene Register Get and Scene Action Setup
    Get always, the acknowledged Store / Delete / Action Set only when `answer_sets` (JUNG firmware may publish
    instead, which our client never hears). `full` elements refuse a Store with *Scene Register Full*.
    """

    def __init__(self, link: FakeBleak, *, answer_sets: bool = True) -> None:
        self.link = link
        self.answer_sets = answer_sets
        self.registers: dict[int, list[int]] = {}
        self.actions: dict[
            tuple[int, int], bytes
        ] = {}  # (element, scene) -> action bytes
        self.full: set[int] = set()
        self.ignore_action_sets: set[int] = set()
        self.seen: list[tuple[int, bytes]] = []
        self._answered = 0
        link.responders.append(self.respond)

    def register_status(self, element: int, status: int = 0) -> bytes:
        scenes = self.registers.get(element, [])
        return (
            encode_opcode(M.SCENE_REGISTER_STATUS)
            + bytes([status])
            + (scenes[0] if scenes else 0).to_bytes(2, "little")
            + b"".join(s.to_bytes(2, "little") for s in scenes)
        )

    def action_status(self, element: int, scene: int) -> bytes:
        if (
            scene == V.SCENE_LIST
        ):  # the list form: every scene the element holds an action for
            listed = sorted(n for e, n in self.actions if e == element)
            body = b"".join(n.to_bytes(2, "little") for n in [scene, *listed])
            return encode_opcode(V.SCENE_ACTION_SETUP_STATUS, M.JUNG_CID) + body
        body = scene.to_bytes(2, "little") + self.actions.get((element, scene), b"")
        return encode_opcode(V.SCENE_ACTION_SETUP_STATUS, M.JUNG_CID) + body

    def respond(self, n: NetworkPDU) -> None:
        if n.ctl or not n.transport_pdu[0] & 0x40:
            return
        msgs = self.link.sent_access()
        for _src, dst, _ttl, _seq, access in msgs[self._answered :]:
            op, cid, p = decode_opcode(access)
            reply: bytes | None = None
            if cid is None and op in (M.SCENE_STORE, M.SCENE_DELETE):
                self.seen.append((dst, access))
                scene = int.from_bytes(p[:2], "little")
                scenes = self.registers.setdefault(dst, [])
                if op == M.SCENE_STORE and dst in self.full:
                    reply = (
                        self.register_status(dst, M.SCENE_REGISTER_FULL)
                        if self.answer_sets
                        else None
                    )
                else:
                    if op == M.SCENE_STORE and scene not in scenes:
                        scenes.append(scene)
                    if op == M.SCENE_DELETE and scene in scenes:
                        scenes.remove(scene)
                    reply = self.register_status(dst) if self.answer_sets else None
            elif cid is None and op == M.SCENE_REGISTER_GET:
                reply = self.register_status(dst)
            elif cid == M.JUNG_CID and op == V.SCENE_ACTION_SETUP_SET:
                self.seen.append((dst, access))
                scene = int.from_bytes(p[:2], "little")
                if dst not in self.ignore_action_sets:
                    if len(p) > 2:
                        self.actions[(dst, scene)] = p[2:]
                    else:
                        self.actions.pop((dst, scene), None)
                reply = self.action_status(dst, scene) if self.answer_sets else None
            elif cid == M.JUNG_CID and op == V.SCENE_ACTION_SETUP_GET:
                reply = self.action_status(dst, int.from_bytes(p[:2], "little"))
            if reply is not None:
                asyncio.get_running_loop().call_soon(
                    self.link.send_access, dst, OUR_SRC, reply
                )
        self._answered = len(msgs)


@pytest.fixture
async def scenes(bench: Bench) -> SceneServer:
    server = SceneServer(bench.link)
    server.registers[SWITCH_LOAD] = [1]  # scene 1 is stored on 0148 in the fixture
    server.actions[(SWITCH_LOAD, 1)] = V.Action(V.ACTION_SWITCH, on=False).encode()
    return server


ON = V.Action(V.ACTION_SWITCH, on=True)


async def test_store_scene_stores_writes_the_action_and_records_the_member(
    bench: Bench, scenes: SceneServer
) -> None:
    """The app's sequence: Scene Store to the Scene Setup Server, Scene Action Setup Set, then the export's addresses."""
    assert await bench.configurator.store_scene("All off", DALI_LOAD, ON)
    assert scenes.seen == [
        (DALI_LOAD, M.scene_store(2)),
        (DALI_LOAD, V.scene_action_set(2, ON)),
    ]
    assert scenes.registers[DALI_LOAD] == [2]
    assert scenes.actions[(DALI_LOAD, 2)] == ON.encode()
    pf = bench.reload()
    assert pf.cdb.scenes[2] == [DALI_LOAD]
    assert pf.cdb.scenes[1] == [SWITCH_LOAD]
    # by number, a second member, no action record when the state is unknown
    assert await bench.configurator.store_scene("2", SOCKET_NODE, None)
    assert scenes.seen[-1] == (SOCKET_NODE, M.scene_store(2))
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD, SOCKET_NODE]
    # storing again keeps one entry
    assert await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD, SOCKET_NODE]


async def test_store_scene_reads_back_when_the_node_publishes_instead_of_replying(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    bench = await make_bench(tmp_path)
    server = SceneServer(bench.link, answer_sets=False)
    assert await bench.configurator.store_scene("All off", DALI_LOAD, ON)
    pdus = [pdu for _, pdu in bench.app_pdus()]
    assert pdus == [
        M.scene_register_get(),  # the capacity check
        M.scene_store(2),
        M.scene_register_get(),
        V.scene_action_set(2, ON),
        V.scene_action_get(2),
    ]
    assert server.registers[DALI_LOAD] == [2]
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD]


async def test_store_scene_failures_record_only_what_the_device_took(
    bench: Bench, scenes: SceneServer
) -> None:
    """A refused Store writes nothing; a Store that took but a description the device ignores records the member."""
    scenes.full.add(DALI_LOAD)
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert err.value.translation_key == "service_scene_not_stored"
    assert err.value.translation_placeholders == {
        "address": "0232",
        "scene": "2",
        "status": "Scene Register Full",
        "applied": mc.APPLIED_NOTHING,
    }
    assert bench.file_unchanged()
    scenes.full.clear()
    scenes.ignore_action_sets.add(DALI_LOAD)
    with pytest.raises(HomeAssistantError) as err2:
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert err2.value.translation_key == "service_scene_action_not_applied"
    assert err2.value.translation_placeholders == {
        "address": "0232",
        "scene": "2",
        "applied": mc.applied_scene_stored(DALI_LOAD, 2),
    }
    assert scenes.registers[DALI_LOAD] == [2]
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD]  # stored on the device: recorded
    recorded = bench.path.read_bytes()
    with pytest.raises(ServiceValidationError) as err3:
        await bench.configurator.store_scene("No such scene", DALI_LOAD, ON)
    assert err3.value.translation_key == "service_unknown_scene"
    with pytest.raises(ServiceValidationError) as err4:
        await bench.configurator.store_scene(
            2, ROCKER_A, ON
        )  # a key: no Scene Setup Server
    assert err4.value.translation_key == "service_not_a_scene_element"
    with pytest.raises(ServiceValidationError) as err5:
        await bench.configurator.store_scene(2, 0x0FFF, ON)
    assert err5.value.translation_key == "service_unknown_element"
    assert bench.path.read_bytes() == recorded


async def test_store_scene_the_node_refused_by_publication_is_not_reported_as_success(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """A full register refused the Store with a status we never hear (published, not replied): the read-back
    register carries status Success but no scene — the error must not say "(Success)"."""
    bench = await make_bench(tmp_path)
    server = SceneServer(bench.link, answer_sets=False)
    server.full.add(DALI_LOAD)
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert err.value.translation_key == "service_scene_not_stored"
    assert err.value.translation_placeholders["status"] == (
        "not in the register after read-back"
    )
    assert [pdu for _, pdu in bench.app_pdus()] == [
        M.scene_register_get(),  # the capacity check
        M.scene_store(2),
        M.scene_register_get(),
    ]
    assert bench.file_unchanged()


async def test_store_scene_silent_node_and_lost_link(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    bench = await make_bench(tmp_path)
    SceneServer(bench.link, answer_sets=False)
    bench.link.responders.clear()  # nobody answers anything
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert err.value.translation_key == "service_no_reply"
    assert bench.file_unchanged()
    bench.link.write_error = ConnectionError("gone")  # a lost link
    with pytest.raises(HomeAssistantError) as err2:
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    # CFG-07: the lost link names the node and the message, like a stopped plan does
    assert err2.value.translation_key == "service_send_failed"
    assert err2.value.translation_placeholders["node"] == "0232"
    with pytest.raises(HomeAssistantError) as err3:
        await bench.configurator.delete_scene(1)
    assert err3.value.translation_key == "service_send_failed"


async def test_store_scene_action_set_silent_after_the_store(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """The Store is acknowledged, the Action Set and its read-back are not: a translated `no reply`."""
    bench = await make_bench(tmp_path)
    server = SceneServer(bench.link)
    original = server.respond

    def respond(n: NetworkPDU) -> None:
        msgs = bench.link.sent_access()
        if msgs and decode_opcode(msgs[-1][4])[1] == M.JUNG_CID:
            server._answered = len(msgs)  # swallow every vendor message
            return
        original(n)

    bench.link.responders[-1] = respond
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert err.value.translation_key == "service_no_reply"
    assert err.value.translation_placeholders["applied"] == mc.applied_scene_stored(
        DALI_LOAD, 2
    )
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD]  # the Store took: recorded


async def test_remove_from_scene_and_delete_scene(
    bench: Bench, scenes: SceneServer
) -> None:
    await bench.configurator.store_scene(2, DALI_LOAD, ON)
    await bench.configurator.store_scene(2, SOCKET_NODE, None)
    scenes.seen.clear()
    assert await bench.configurator.remove_from_scene("All off", SOCKET_NODE)
    assert (
        scenes.seen
        == [  # the app's order: the channel's action first, then the (shared) register
            (SOCKET_NODE, V.scene_action_set(2)),
            (SOCKET_NODE, M.scene_delete(2)),
        ]
    )
    assert scenes.registers[SOCKET_NODE] == []
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD]
    scenes.seen.clear()
    assert await bench.configurator.delete_scene("all OFF")
    assert scenes.seen == [
        (DALI_LOAD, V.scene_action_set(2)),
        (DALI_LOAD, M.scene_delete(2)),
    ]
    assert (DALI_LOAD, 2) not in scenes.actions
    pf = bench.reload()
    assert 2 not in pf.cdb.scenes
    assert 2 not in pf.scene_names()
    assert [s["number"] for s in pf.meta["scenes"]] == [1]
    assert pf.scene_names() == {1: "WC off"}
    # a delete the node refuses to carry out
    scenes.answer_sets = False
    scenes.registers[SWITCH_LOAD] = [1]
    original = scenes.respond

    def respond(n: NetworkPDU) -> None:
        original(n)
        scenes.registers[SWITCH_LOAD] = [1]  # the register never lets go of scene 1

    bench.link.responders[-1] = respond
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.delete_scene(1)
    assert err.value.translation_key == "service_scene_not_deleted"
    assert 1 in bench.reload().cdb.scenes


async def test_a_numeric_scene_name_is_not_taken_for_another_scenes_number(
    bench: Bench, scenes: SceneServer
) -> None:
    """CFG-06: an all-digit name is a name the app allows; when another scene has that number, which one a call
    means cannot be told, so it is refused instead of deleting the scene with that number."""
    assert await bench.configurator.create_scene("1") == NEW_SCENE
    with pytest.raises(ServiceValidationError) as exc:
        await bench.configurator.delete_scene("1")
    assert exc.value.translation_key == "service_ambiguous_scene"
    assert 1 in bench.reload().cdb.scenes
    assert not [
        pdu
        for dst, pdu in bench.app_pdus()
        if dst == 0x0148 and pdu[:2] == M.scene_delete(1)[:2]
    ]
    pf = bench.reload()
    assert MeshConfigurator._scene(pf, "WC off") == 1
    assert MeshConfigurator._scene(pf, "2") == 2  # an unambiguous number still works
    assert await bench.configurator.rename_scene(str(NEW_SCENE), "7")
    assert (
        MeshConfigurator._scene(bench.reload(), "7") == NEW_SCENE
    )  # a digit name alone resolves (no scene 7)


async def test_create_and_rename_scene(bench: Bench, scenes: SceneServer) -> None:
    assert await bench.configurator.create_scene("Movie night") == NEW_SCENE
    pf = bench.reload()
    assert pf.cdb.scenes[NEW_SCENE] == []
    assert pf.scene_names()[NEW_SCENE] == "Movie night"
    assert (
        next(s for s in pf.meta["scenes"] if s["number"] == NEW_SCENE)["icon"]
        == "SceneDay"
    )
    assert (
        await bench.configurator.create_scene("Dinner", icon="SceneNight")
        == NEW_SCENE - 1
    )
    assert (
        next(s for s in bench.reload().meta["scenes"] if s["number"] == NEW_SCENE - 1)[
            "icon"
        ]
        == "SceneNight"
    )
    with pytest.raises(ServiceValidationError) as err:
        await bench.configurator.create_scene("movie NIGHT")
    assert err.value.translation_key == "service_scene_exists"
    assert await bench.configurator.rename_scene("Movie night", "Cinema")
    assert bench.reload().scene_names()[NEW_SCENE] == "Cinema"
    with pytest.raises(ServiceValidationError) as err2:
        await bench.configurator.rename_scene(NEW_SCENE, "Dinner")
    assert err2.value.translation_key == "service_scene_exists"
    assert not scenes.seen  # nothing went on air
    # the scene range exhausted (the fixture's provisioner owns 1..0x1999: patched rather than filled)
    with (
        patch.object(
            ProjectFile, "free_scene_number", side_effect=mc.ExportError("full")
        ),
        pytest.raises(ServiceValidationError) as err3,
    ):
        await bench.configurator.create_scene("One too many")
    assert err3.value.translation_key == "service_scene_range_full"


def test_scene_action_for_every_load_kind() -> None:
    state = SimpleNamespace(on=True, lightness=32768, kelvin=3000, level=None)
    assert mc.scene_action_for("switch", state) == ON
    assert mc.scene_action_for(
        "socket", SimpleNamespace(on=False, lightness=None, kelvin=None)
    ) == V.Action(V.ACTION_SWITCH, on=False)
    assert mc.scene_action_for("dimmer", state) == V.Action(
        V.ACTION_LIGHTNESS, lightness=32768
    )
    assert mc.scene_action_for(
        "dimmer", SimpleNamespace(on=False, lightness=32768, kelvin=None)
    ) == V.Action(V.ACTION_LIGHTNESS, lightness=0)
    assert mc.scene_action_for("ctl", state) == V.Action(
        V.ACTION_LIGHTNESS_CT, lightness=32768, temperature_k=3000
    )
    unknown = SimpleNamespace(on=None, lightness=None, kelvin=None)
    assert mc.scene_action_for("switch", unknown) is None
    assert mc.scene_action_for("dimmer", unknown) is None
    assert (
        mc.scene_action_for("ctl", SimpleNamespace(on=True, lightness=1, kelvin=None))
        is None
    )
    # a blind: its position and slats (a blind without slats repeats the position); a thermostat its set-point
    assert mc.scene_action_for("blind", state) is None
    blind = SimpleNamespace(level=-100)
    assert mc.scene_action_for("blind", blind) == V.Action(
        V.ACTION_BLINDS, blind=-100, slat=-100
    )
    assert mc.scene_action_for("blind", blind, SimpleNamespace(level=500)) == V.Action(
        V.ACTION_BLINDS, blind=-100, slat=500
    )
    assert mc.scene_action_for("blind", blind, SimpleNamespace(level=None)) is None
    assert mc.scene_action_for("thermostat", SimpleNamespace(level=None)) is None
    assert mc.scene_action_for("thermostat", SimpleNamespace(level=-32768)) == V.Action(
        V.ACTION_TEMPERATURE, temperature_c=5.0
    )
    assert mc.scene_action_for("detector", state) is None
    assert mc._scene_register_status_name(0x07) == "status 0x07"
    assert mc._scene_register_status_name(2) == "Scene Not Found"
    assert not mc._confirms_scene_action(b"\x01", 1, None)


async def test_scene_register_status_malformed_and_members_without_the_vendor_model(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """A truncated Scene Register Status is a refusal; a member without Scene Action Setup gets no vendor Set; an
    address the CDB no longer knows is skipped when a scene is deleted."""
    source = tmp_path / "source" / ANDROID_PATH.name
    source.parent.mkdir()
    shutil.copy(ANDROID_PATH, source)
    doc = json.loads(source.read_text())
    net = json.loads(base64.b64decode(doc["network"]).decode())
    for node in net["nodes"]:
        if node["unicastAddress"] == "0172":
            node["elements"][0]["models"] = [
                m for m in node["elements"][0]["models"] if m["modelId"] != "05271017"
            ]
    for scene in net["scenes"]:
        if scene["number"] == "0001":
            scene["addresses"] = ["0148", "0172", "0FFF"]  # 0FFF: no such element
    doc["network"] = base64.b64encode(json.dumps(net).encode()).decode()
    source.write_text(json.dumps(doc))
    bench = await make_bench(tmp_path, source)
    server = SceneServer(bench.link)
    server.registers[SWITCH_LOAD] = [1]
    server.registers[SOCKET_NODE] = [1]
    assert await bench.configurator.remove_from_scene(1, SOCKET_NODE)
    assert server.seen == [
        (SOCKET_NODE, M.scene_delete(1))
    ]  # no Scene Action Setup Set
    assert bench.reload().cdb.scenes[1] == [SWITCH_LOAD, 0x0FFF]
    server.seen.clear()
    assert await bench.configurator.delete_scene(1)
    assert server.seen == [
        (SWITCH_LOAD, V.scene_action_set(1)),
        (SWITCH_LOAD, M.scene_delete(1)),
    ]
    assert 1 not in bench.reload().cdb.scenes
    # a Scene Register Status too short to decode
    original = server.register_status
    server.register_status = lambda element, status=0: (
        encode_opcode(  # type: ignore[method-assign]
            M.SCENE_REGISTER_STATUS
        )
        + b"\x00"
    )
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert err.value.translation_key == "service_config_refused"
    assert err.value.translation_placeholders["status"] == "malformed status"
    server.register_status = original  # type: ignore[method-assign]


async def test_two_channel_node_shares_one_scene_register(
    bench: Bench, scenes: SceneServer
) -> None:
    """Both outputs of the 2-channel actuator store on the node's first Scene Setup Server (0400); the register is
    only deleted once no channel holds an action for the scene any more (network-features.md §3)."""
    lightness = V.Action(V.ACTION_LIGHTNESS, lightness=100)
    assert await bench.configurator.store_scene(2, ACTUATOR_OUT2, lightness)
    assert scenes.seen == [
        (ACTUATOR_OUT1, M.scene_store(2)),  # the register lives on the primary element
        (ACTUATOR_OUT2, V.scene_action_set(2, lightness)),  # the action on the channel
    ]
    assert bench.reload().cdb.scenes[2] == [ACTUATOR_OUT1]
    scenes.seen.clear()
    assert await bench.configurator.store_scene(2, ACTUATOR_OUT1, ON)
    assert scenes.seen == [
        (ACTUATOR_OUT1, M.scene_store(2)),
        (ACTUATOR_OUT1, V.scene_action_set(2, ON)),
    ]
    assert bench.reload().cdb.scenes[2] == [ACTUATOR_OUT1]  # recorded once
    # removing channel 2: its action goes, the register stays because channel 1 still uses the scene
    scenes.seen.clear()
    assert await bench.configurator.remove_from_scene(2, ACTUATOR_OUT2)
    assert scenes.seen == [(ACTUATOR_OUT2, V.scene_action_set(2))]
    assert scenes.registers[ACTUATOR_OUT1] == [2]
    assert bench.reload().cdb.scenes[2] == [ACTUATOR_OUT1]
    # removing channel 1 as well: nobody uses the scene, the register is cleared and the member dropped
    scenes.seen.clear()
    assert await bench.configurator.remove_from_scene(2, ACTUATOR_OUT1)
    assert scenes.seen == [
        (ACTUATOR_OUT1, V.scene_action_set(2)),
        (ACTUATOR_OUT1, M.scene_delete(2)),
    ]
    assert scenes.registers[ACTUATOR_OUT1] == []
    assert bench.reload().cdb.scenes[2] == []
    # a sibling that does not answer the check blocks the removal (nothing written)
    await bench.configurator.store_scene(2, ACTUATOR_OUT1, ON)
    original = scenes.respond

    def respond(n: NetworkPDU) -> None:
        msgs = bench.link.sent_access()
        if msgs and msgs[-1][1] == ACTUATOR_OUT2:
            scenes._answered = len(msgs)  # channel 2 stays silent
            return
        original(n)

    bench.link.responders[-1] = respond
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.remove_from_scene(2, ACTUATOR_OUT1)
    assert err.value.translation_key == "service_no_reply"
    assert bench.reload().cdb.scenes[2] == [ACTUATOR_OUT1]
    # a sibling answering something that is not a status counts as "not using the scene"
    scenes.action_status = lambda element, scene: (
        encode_opcode(  # type: ignore[method-assign]
            V.SCENE_ACTION_SETUP_STATUS, M.JUNG_CID
        )
        + (b"\x01" if element == ACTUATOR_OUT2 else scene.to_bytes(2, "little"))
    )
    bench.link.responders[-1] = original
    assert await bench.configurator.remove_from_scene(2, ACTUATOR_OUT1)
    assert scenes.registers[ACTUATOR_OUT1] == []
    del scenes.action_status  # back to the class's
    await bench.configurator.store_scene(2, ACTUATOR_OUT1, ON)
    # deleting the whole scene clears every channel's action and the register once, no sibling check
    scenes.actions[(ACTUATOR_OUT2, 2)] = lightness.encode()
    scenes.seen.clear()
    assert await bench.configurator.delete_scene(2)
    assert scenes.seen == [
        (ACTUATOR_OUT1, V.scene_action_set(2)),
        (ACTUATOR_OUT2, V.scene_action_set(2)),
        (ACTUATOR_OUT1, M.scene_delete(2)),
    ]
    assert 2 not in bench.reload().cdb.scenes


# ----------------------------------------------------------------------------- apply-and-record: the pieces


def test_config_step_matches_the_status_that_echoes_it() -> None:
    """The Status must echo the step's element / model / address; a foreign one is not this step's answer."""
    change = mc.ModelChange(ROCKER_A, "1001", DIMMER_GROUP, "subscribe")
    step = mc.ConfigStep(
        DALI_NODE, sub_add(ROCKER_A, DIMMER_GROUP, "1001"), 0x801F, change=change
    )

    def status(opcode: int, params: bytes) -> Any:
        sop, scid, sparams = decode_opcode(encode_opcode(opcode) + params)
        return SimpleNamespace(opcode=sop, company_id=scid, params=sparams)

    own = status(0x801F, b"\x00" + ROCKER_A.to_bytes(2, "little") + b"\x70\xc0\x01\x10")
    other_model = status(
        0x801F, b"\x00" + ROCKER_A.to_bytes(2, "little") + b"\x70\xc0\x03\x10"
    )
    other_address = status(
        0x801F, b"\x00" + ROCKER_A.to_bytes(2, "little") + b"\x44\xc0\x01\x10"
    )
    assert step.matches(own)
    assert not step.matches(other_model)
    assert not step.matches(other_address)
    assert step.matches(
        status(0x801F, b"\x00")
    )  # malformed: judged by `_request`, not ignored
    # a publication step compares the publish address; a clear echoes 0000
    clear = mc.ConfigStep(
        DALI_NODE,
        pub_set(ROCKER_A, 0, "1001"),
        0x8019,
        change=mc.ModelChange(ROCKER_A, "1001", 0, "publish"),
    )
    assert clear.matches(
        status(0x8019, b"\x00" + bytes.fromhex("3402 0000 0000 ff 00 00 0110"))
    )
    assert not clear.matches(
        status(0x8019, b"\x00" + bytes.fromhex("3402 70c0 0000 ff 00 00 0110"))
    )
    # a bind step compares element, model and AppKey index
    bind = mc.ConfigStep(
        DALI_NODE,
        C.model_app_bind(ROCKER_A, "1003", 0),
        0x803E,
        bind=(ROCKER_A, "1003"),
    )
    assert bind.matches(status(0x803E, b"\x00" + bytes.fromhex("3402 0000 0310")))
    assert not bind.matches(status(0x803E, b"\x00" + bytes.fromhex("3402 0100 0310")))
    assert not bind.matches(status(0x803E, b"\x00" + bytes.fromhex("3402 0000 0110")))
    assert bind.matches(
        own
    )  # another opcode's status: not judged here (the client matched the opcode)
    # a step without an edit (a bare message) matches whatever the client matched
    bare = mc.ConfigStep(DALI_NODE, sub_add(ROCKER_A, DIMMER_GROUP, "1001"), 0x801F)
    assert bare.matches(other_model)
    assert bare.additive


def test_ordered_puts_additions_first_and_drops_superseded_clears() -> None:
    sub = mc.ModelChange(ROCKER_A, "1001", DIMMER_GROUP, "subscribe")
    unsub_same = mc.ModelChange(ROCKER_A, "1001", DIMMER_GROUP, "unsubscribe")
    unsub_other = mc.ModelChange(ROCKER_A, "1001", DALI_GROUP, "unsubscribe")
    clear_pub = mc.ModelChange(ROCKER_A, "1001", 0, "publish")
    set_pub = mc.ModelChange(ROCKER_A, "1001", DIMMER_GROUP, "publish")
    clear_other_pub = mc.ModelChange(ROCKER_A, "1003", 0, "publish")
    steps = [
        mc.ConfigStep(DALI_NODE, b"a", 0, change=clear_pub),
        mc.ConfigStep(DALI_NODE, b"b", 0, change=unsub_same),
        mc.ConfigStep(DALI_NODE, b"c", 0, change=unsub_other),
        mc.ConfigStep(DALI_NODE, b"d", 0, change=clear_other_pub),
        mc.ConfigStep(DALI_NODE, b"e", 0, bind=(ROCKER_A, "1001")),
        mc.ConfigStep(DALI_NODE, b"f", 0, change=set_pub),
        mc.ConfigStep(DALI_NODE, b"g", 0, change=sub),
    ]
    assert [s.pdu for s in mc.ordered(steps)] == [b"e", b"f", b"g", b"c", b"d"]


def test_replay_applies_every_kind_of_step_idempotently() -> None:
    pf = ProjectFile.load(ANDROID_PATH)
    el = pf.cdb.element(ROCKER_A)
    assert el is not None
    raw = next(m for m in el.raw_models if m["modelId"] == "1003")
    raw["bind"] = []
    bind = mc.ConfigStep(DALI_NODE, b"", 0, bind=(ROCKER_A, "1003"))
    mc.replay(pf, bind)
    mc.replay(pf, bind)
    assert raw["bind"] == [0]
    for step in (
        mc.ConfigStep(
            DALI_NODE,
            b"",
            0,
            change=mc.ModelChange(ROCKER_A, "1001", DIMMER_GROUP, "subscribe"),
        ),
        mc.ConfigStep(
            DALI_NODE,
            b"",
            0,
            change=mc.ModelChange(ROCKER_A, "1001", DALI_GROUP, "unsubscribe"),
        ),
        mc.ConfigStep(
            DALI_NODE, b"", 0, change=mc.ModelChange(ROCKER_A, "1001", 0, "publish")
        ),
    ):
        mc.replay(pf, step)
        mc.replay(pf, step)
    assert subs(pf, ROCKER_A, "1001") == [DIMMER_GROUP]
    assert pub(pf, ROCKER_A, "1001") is None
    mc.replay(
        pf,
        mc.ConfigStep(
            DALI_NODE,
            b"",
            0,
            change=mc.ModelChange(ROCKER_A, "1001", DIMMER_GROUP, "publish"),
            unlinks=DIMMER_KEY,
        ),
    )
    assert pub(pf, ROCKER_A, "1001") == DIMMER_GROUP
    # `replay` no longer drops the row itself: `unlinks` only tags the step for `_record`'s own bookkeeping
    assert [r["elementAddress"] for r in link_rows(pf)] == [DIMMER_KEY]


def test_drop_link_rows_removes_only_the_named_keys_row() -> None:
    pf = ProjectFile.load(ANDROID_PATH)
    assert [r["elementAddress"] for r in link_rows(pf)] == [DIMMER_KEY]
    mc._drop_link_rows(pf, DIMMER_KEY)
    assert link_rows(pf) == []
    mc._drop_link_rows(pf, DIMMER_KEY)  # idempotent: no row left to drop
    assert link_rows(pf) == []
    # the scene row goes with its key's old wiring too, and only that key's
    assert scene_key_rows(pf) == [SWITCH_KEY]
    mc._drop_link_rows(pf, SWITCH_KEY)
    assert scene_key_rows(pf) == []


def test_applied_texts() -> None:
    assert mc.applied_text(0, 5) == mc.APPLIED_NOTHING
    assert mc.applied_text(2, 5).startswith("The 2 of 5 messages accepted before it")
    assert mc.applied_members(0, 3, 2) == mc.APPLIED_NOTHING
    assert mc.applied_members(1, 3, 2).startswith(
        "1 of 3 devices already forgot scene 2"
    )
    assert "0232" in mc.applied_scene_stored(DALI_LOAD, 2)
    assert mc.applied_scene_cleared(DALI_LOAD, 2).startswith(
        "The scene description of 0232"
    )
    assert mc.applied_scene_cleared(DALI_LOAD, 2, 2, 3).startswith(
        "2 of 3 devices already forgot scene 2"
    )


async def test_a_node_the_hub_does_not_know_is_a_translated_error(bench: Bench) -> None:
    """The export on disk names a node the running hub has no device key for (re-provisioned, no reload)."""
    doc = json.loads(bench.original)
    net = json.loads(base64.b64decode(doc["network"]))
    for node in net["nodes"]:
        if node["unicastAddress"] == "0300":
            node["unicastAddress"] = "0310"
            for element in node["elements"]:
                element["index"] = element.get("index", 0)
    doc["network"] = base64.b64encode(json.dumps(net).encode()).decode()
    bench.path.write_text(json.dumps(doc))
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.set_room(0x0310, "Kitchen")
    assert exc.value.translation_key == "service_export_unknown_node"
    assert exc.value.translation_placeholders == {
        "node": "0310",
        "path": str(bench.path),
        "applied": mc.APPLIED_NOTHING,
    }
    assert bench.config_pdus() == []


# ----------------------------------------------------------------------------- batches: what a stop records


async def test_store_scenes_records_the_loads_stored_before_the_one_that_failed(
    bench: Bench, scenes: SceneServer
) -> None:
    scenes.full.add(SOCKET_NODE)
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scenes(2, [(DALI_LOAD, ON), (SOCKET_NODE, None)])
    assert err.value.translation_key == "service_scene_not_stored"
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD]
    assert bench.hub.hass.jobs == ["load_project", "save"]
    # the first load failing: nothing recorded
    bench.hub.hass.jobs.clear()
    scenes.full.add(DALI_LOAD)
    with pytest.raises(HomeAssistantError):
        await bench.configurator.store_scenes(2, [(DALI_LOAD, ON), (SOCKET_NODE, None)])
    assert bench.hub.hass.jobs == ["load_project"]


async def test_store_scenes_second_load_description_failure_saves_once(
    bench: Bench, scenes: SceneServer
) -> None:
    """CFG-03: one save records what a stopped call stored (a second one uploaded twice and rotated `.bak` onto
    the first save's output); the load whose Store took is recorded although its description failed."""
    scenes.ignore_action_sets.add(DALI_LOAD)
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scenes(2, [(SWITCH_LOAD, ON), (DALI_LOAD, ON)])
    assert err.value.translation_key == "service_scene_action_not_applied"
    assert bench.hub.hass.jobs.count("save") == 1
    assert set(bench.reload().cdb.scenes[2]) == {SWITCH_LOAD, DALI_LOAD}


async def test_store_scenes_failure_after_stored_loads_says_they_were_recorded(
    bench: Bench, scenes: SceneServer
) -> None:
    """CFG-07: the error says what was recorded (not "nothing was written"), and a lost link names the node and
    the message instead of a bare "no proxy connected"."""
    scenes.full.add(SOCKET_NODE)
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scenes(2, [(DALI_LOAD, ON), (SOCKET_NODE, None)])
    assert err.value.translation_key == "service_scene_not_stored"
    assert "1 of 2" in err.value.translation_placeholders["applied"]
    assert DALI_LOAD in bench.reload().cdb.scenes[2]

    with (
        patch.object(bench.hub.proxy, "request", side_effect=ConnectionError("gone")),
        pytest.raises(HomeAssistantError) as lost,
    ):
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert lost.value.translation_key == "service_send_failed"
    assert lost.value.translation_placeholders["node"] == "0232"
    assert lost.value.translation_placeholders["applied"] == mc.APPLIED_NOTHING


async def test_a_null_device_row_does_not_crash_the_key_services(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """CFG-12: the loader tolerates a stray `null` in `meta.devices`; so do the key / room services now, walking
    `meta` with export's helpers instead of copies that called `.get` on it."""
    doc = json.loads(ANDROID_PATH.read_text())
    doc["meta"]["devices"].insert(0, None)
    source = tmp_path / "src" / ANDROID_PATH.name
    source.parent.mkdir()
    source.write_text(json.dumps(doc))
    bench = await make_bench(tmp_path, source)
    assert await bench.configurator.clear_key(DIMMER_KEY) is True
    assert link_rows(bench.reload()) == []


async def test_renaming_to_the_current_name_writes_nothing(
    bench: Bench, scenes: SceneServer
) -> None:
    """CFG-14: a rename that changes nothing is no write, upload or reload (the call returns False)."""
    assert await bench.configurator.rename_room("WC", "WC") is False
    assert bench.file_unchanged()
    # the fixture's scene 1 is "Scene #1" in the CDB and "WC off" in `meta`: the first rename aligns them
    assert await bench.configurator.rename_scene(1, "WC off") is True
    bench.hub.hass.jobs.clear()
    written = bench.path.read_bytes()
    assert await bench.configurator.rename_scene(1, "WC off") is False
    assert bench.path.read_bytes() == written
    assert "save" not in bench.hub.hass.jobs


def _device_name(pf: ProjectFile, uuid_prefix: str, locations: list[int]) -> str:
    return next(
        d["name"]
        for d in pf.meta["devices"]
        if d["deviceId"]["nodeId"].startswith(uuid_prefix)
        and d["deviceId"]["locationIds"] == locations
    )


async def test_rename_device_is_the_apps_update_device_name(bench: Bench) -> None:
    """`UpdateDeviceName`: `meta.devices[].name` of the entry covering the element, numbered when taken."""
    assert await bench.configurator.rename_device(SWITCH_LOAD, "Mirror") == "Mirror"
    pf = bench.reload()
    assert _device_name(pf, "00005EFF-FE00-5314", [1]) == "Mirror"
    node = pf.cdb.node_by_addr(SWITCH_LOAD)
    assert node is not None
    assert node.name == "Push-button 1-gang"  # the CDB node keeps the product's name
    # the dimmer's key: its own entry ([64]); "WC ceiling" is the dimmer's, so the app numbers it
    assert await bench.configurator.rename_device(DIMMER_KEY, "wc CEILING") == (
        "wc CEILING 3"
    )
    assert _device_name(bench.reload(), "00005EFF-FE00-5330", [64]) == "wc CEILING 3"
    # already called that: nothing is written
    written = bench.path.read_bytes()
    assert await bench.configurator.rename_device(SWITCH_LOAD, "Mirror") == "Mirror"
    assert bench.path.read_bytes() == written
    # an output the export has no entry for gets one of its own
    assert await bench.configurator.rename_device(ACTUATOR_OUT2, "Pantry") == "Pantry"
    assert _device_name(bench.reload(), "00005EFF-FE00-5340", [2]) == "Pantry"
    assert bench.config_pdus() == []  # nothing on air


@pytest.mark.parametrize(
    ("name", "key"),
    [("  ", "service_name_blank"), ("50% off", "service_name_not_allowed")],
)
async def test_names_the_app_refuses_are_refused(
    bench: Bench, scenes: SceneServer, name: str, key: str
) -> None:
    """`CheckNameInput`: blank, or not a valid format string (a lone %), for devices, rooms and scenes alike."""
    ops = [
        lambda: bench.configurator.rename_device(SWITCH_LOAD, name),
        lambda: bench.configurator.create_scene(name),
        lambda: bench.configurator.rename_scene(1, name),
    ]
    if (
        key == "service_name_not_allowed"
    ):  # a blank room name has its own error (service_invalid_room_name)
        ops += [
            lambda: bench.configurator.create_room(name),
            lambda: bench.configurator.rename_room("WC", name),
        ]
    for op in ops:
        with pytest.raises(ServiceValidationError) as exc:
            await op()
        assert exc.value.translation_key == key
    assert bench.file_unchanged()
    assert await bench.configurator.create_room("50%% off")  # a doubled % is a % sign


async def test_a_rename_past_the_rename_sheets_limit_is_refused(
    bench: Bench, scenes: SceneServer
) -> None:
    """The app's rename sheet takes 30 characters (`app:maxLength="30"`): renames only, creating has no limit."""
    long = "x" * 31
    for op in (
        lambda: bench.configurator.rename_device(SWITCH_LOAD, long),
        lambda: bench.configurator.rename_room("WC", long),
        lambda: bench.configurator.rename_scene(1, long),
    ):
        with pytest.raises(ServiceValidationError) as exc:
            await op()
        assert exc.value.translation_key == "service_name_too_long"
        assert exc.value.translation_placeholders == {"name": long, "max_length": "30"}
    assert bench.file_unchanged()
    assert await bench.configurator.create_room(long)


def test_a_device_row_with_unreadable_location_ids_is_skipped() -> None:
    """CFG-12: `locationIds` that are not numbers make a row unusable, not a crash (as in export's own lookup)."""
    pf = ProjectFile.load(ANDROID_PATH)
    key = pf.cdb.element(DIMMER_KEY)
    assert key is not None
    good = MeshConfigurator._device_entry(pf, key.node, key.location)
    assert good is not None
    pf.meta["devices"].insert(
        0, {"deviceId": {"nodeId": key.node.uuid, "locationIds": ["x"]}}
    )
    assert MeshConfigurator._device_entry(pf, key.node, key.location) is good


async def test_remove_from_scenes_records_the_loads_removed_before_the_one_that_failed(
    bench: Bench, scenes: SceneServer
) -> None:
    await bench.configurator.store_scenes(2, [(DALI_LOAD, ON), (SOCKET_NODE, None)])
    original = scenes.respond

    def respond(n: NetworkPDU) -> None:
        original(n)
        scenes.registers[SOCKET_NODE] = [2]  # the socket never lets go of the scene

    bench.link.responders[-1] = respond
    scenes.answer_sets = False
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.remove_from_scenes(2, [DALI_LOAD, SOCKET_NODE])
    assert err.value.translation_key == "service_scene_not_deleted"
    # review-3 W14: the error said only that the socket's description was cleared, not that the DALI load
    # before it had left the scene and was recorded so
    applied = err.value.translation_placeholders["applied"]
    assert applied == mc.applied_scene_cleared(SOCKET_NODE, 2, 1, 2)
    assert applied.startswith(
        "1 of 2 devices already forgot scene 2 and the mesh export records that. "
        "The scene description of 0172 for scene 2 was cleared"
    )
    assert bench.reload().cdb.scenes[2] == [
        SOCKET_NODE
    ]  # the DALI load is out: recorded


async def test_a_stop_before_any_description_was_cleared_does_not_claim_one_was(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """Review-3 W14: removing a load without a Scene Action Setup server clears no description, so a refused
    Scene Delete must not say "the scene description … was cleared" — nothing was applied at all."""

    def edit(net: dict[str, Any]) -> None:
        socket = next(n for n in net["nodes"] if n["unicastAddress"] == "0172")
        socket["elements"][0]["models"] = [
            m for m in socket["elements"][0]["models"] if m["modelId"] != "05271017"
        ]

    bench = await prepare(tmp_path, "legacy", lambda doc: edited_network(doc, edit))
    server = SceneServer(bench.link)
    await bench.configurator.store_scene(2, SOCKET_NODE, None)
    original = server.respond

    def respond(n: NetworkPDU) -> None:
        original(n)
        server.registers[SOCKET_NODE] = [2]  # the socket never lets go of the scene

    bench.link.responders[-1] = respond
    server.answer_sets = False  # the register is read back after the delete
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.remove_from_scene(2, SOCKET_NODE)
    assert err.value.translation_key == "service_scene_not_deleted"
    assert err.value.translation_placeholders["applied"] == mc.APPLIED_NOTHING


async def test_a_channel_of_unknown_state_is_not_stored_beside_a_sibling_channel(
    bench: Bench, scenes: SceneServer
) -> None:
    """Review-3 W6: a channel stored without its JUNG action is a member no one can tell — removing the node's
    other channel from the scene checks the sibling's action, finds none, and deletes the shared register. So a
    channel of a multi-channel node whose state is unknown is refused before anything is sent; a single-channel
    load of unknown state is stored as before (no sibling can drop it)."""
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scenes(
            2, [(DALI_LOAD, ON), (ACTUATOR_OUT2, None)]
        )
    assert err.value.translation_key == "service_scene_state_unknown"
    assert err.value.translation_placeholders == {
        "address": "0401",
        "scene": "2",
        "applied": mc.applied_scene_members(1, 2, 2),
    }
    assert ACTUATOR_OUT1 not in scenes.registers
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD]
    assert await bench.configurator.store_scene(2, SOCKET_NODE, None)
    assert SOCKET_NODE in bench.reload().cdb.scenes[2]


async def test_delete_scene_records_the_members_that_forgot_it_before_the_one_that_failed(
    bench: Bench, scenes: SceneServer
) -> None:
    await bench.configurator.store_scenes(2, [(DALI_LOAD, ON), (SOCKET_NODE, None)])
    original = scenes.respond

    def respond(n: NetworkPDU) -> None:
        original(n)
        scenes.registers[SOCKET_NODE] = [2]

    bench.link.responders[-1] = respond
    scenes.answer_sets = False
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.delete_scene(2)
    assert err.value.translation_key == "service_scene_not_deleted"
    assert err.value.translation_placeholders["applied"] == mc.applied_members(1, 2, 2)
    pf = bench.reload()
    assert pf.cdb.scenes[2] == [SOCKET_NODE]
    assert 2 in pf.scene_names()  # the scene itself stays until every member forgot it


# ----------------------------------------------------------------------------- the gateway's copy (bench level)


class FakeGateway:
    """`JungHomeGatewayApi` as the configurator uses it: `host`, `fetch_project`, `upload_project`."""

    def __init__(self, doc: Any) -> None:
        self.host = "junghome.local"
        self.doc = doc
        self.uploads: list[dict[str, Any]] = []
        self.refuse_upload: Exception | None = None
        self.serve_uploads = False  # a real gateway serves what was uploaded last; most tests pin `doc` instead

    async def fetch_project(self) -> dict[str, Any]:
        doc = self.doc() if callable(self.doc) else self.doc
        if isinstance(doc, Exception):
            raise doc
        assert isinstance(doc, dict)
        return doc

    async def upload_project(self, export: dict[str, Any]) -> None:
        if self.refuse_upload is not None:
            raise self.refuse_upload
        self.uploads.append(export)
        if self.serve_uploads:
            self.doc = export


def gateway_doc(
    path: Path, *, room: str | None = None, stamp: datetime | None = None
) -> dict[str, Any]:
    """The export the app would have uploaded: ours plus `room`, stamped a minute from now (or `stamp`)."""
    newer = ProjectFile.load(path)
    if room is not None:
        newer.add_group(room)
    newer.touch(stamp or datetime.now(UTC) + timedelta(minutes=1))
    return json.loads(newer.share_json())


@pytest.fixture
async def with_gateway(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[FakeGateway]:
    """Give the bench's configurator a gateway, already in sync (the repair-issue registry needs a real hass,
    stubbed here); a retry of a failed upload still pending at the end is cancelled, as the hub's stop does."""
    doc = json.loads(ProjectFile.load(bench.path).share_json())
    gateway = FakeGateway(doc)
    monkeypatch.setattr(MeshConfigurator, "gateway", property(lambda _self: gateway))
    monkeypatch.setattr(mc.ir, "async_create_issue", lambda *_a, **_k: None)
    monkeypatch.setattr(mc.ir, "async_delete_issue", lambda *_a, **_k: None)
    bench.hub.entry.data = {
        **bench.hub.entry.data,
        CONF_GATEWAY_SYNCED: mc.export_digest(doc),
    }
    yield gateway
    retry = bench.configurator.upload_retry
    bench.configurator.cancel_upload_retry()
    if retry is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await retry


async def test_a_stopped_plan_hands_its_record_to_the_gateway(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    refused = sub_add(ROCKER_A, DIMMER_GROUP, "1003")
    bench.config.refuse[refused] = 0x08
    with pytest.raises(HomeAssistantError):
        await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert len(with_gateway.uploads) == 1
    uploaded = ProjectFile.load(bench.path)  # what went up is what is on disk
    assert pub(uploaded, ROCKER_A, "1001") == DIMMER_GROUP
    assert (
        bench.hub.hass.jobs
        == [
            "_digest_on_disk",  # the gateway's copy compared with the file first
            "load_project",
            "load_project",
            "_keep_app_copy",  # the app's last upload, before HA's first change: the merge base
            "save",
            "_digest_on_disk",  # the upload checks again, right before the POST
        ]
    )


def app_upload(app: ProjectFile) -> dict[str, Any]:
    """What the app uploads: its own copy of the project, stamped a minute from now."""
    app.touch(datetime.now(UTC) + timedelta(minutes=1))
    return json.loads(app.share_json())


async def test_home_assistant_changes_survive_the_apps_next_upload(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """Review-3 W1: the app uploads its whole project after every change but never downloads one. Its next upload
    lacked the key HA had wired (still wired on the mesh), and adopting it made HA forget that wiring."""
    with_gateway.serve_uploads = True
    app = ProjectFile.load(bench.path)  # the app's copy: it never sees what HA changes
    await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    assert pub(bench.reload(), ROCKER_A, "1001") == DIMMER_GROUP
    assert mc.app_copy_path(bench.path).exists()
    app.add_group("From the app")
    with_gateway.doc = app_upload(app)
    await bench.configurator.create_room("Attic")
    merged = bench.reload()
    assert pub(merged, ROCKER_A, "1001") == DIMMER_GROUP  # HA's wiring kept ...
    assert {"From the app", "Attic"} <= set(
        merged.user_groups().values()
    )  # ... next to the app's
    assert "change(s) Home Assistant made that the app's export lacks" in caplog.text
    # the gateway got the merged export right away, then the room
    carried = ProjectFile.loads(json.dumps(with_gateway.uploads[-2]).encode())
    assert pub(carried, ROCKER_A, "1001") == DIMMER_GROUP
    assert "From the app" in carried.user_groups().values()
    # the app's upload is the base now: the next one is compared with it, HA's change still carried. The app
    # does not know HA's room "Attic" either, but HA took it from the top of the range: the app's own next room
    # gets another address, and both stay
    attic = next(a for a, n in merged.user_groups().items() if n == "Attic")
    second = app.add_group("Second from the app")
    assert second != attic
    caplog.clear()
    with_gateway.doc = app_upload(app)
    await bench.configurator.create_room("Cellar")
    merged = bench.reload()
    assert pub(merged, ROCKER_A, "1001") == DIMMER_GROUP
    rooms = merged.user_groups()
    assert rooms[attic] == "Attic"
    assert rooms[second] == "Second from the app"
    assert "Cellar" in rooms.values()
    assert "the app's version is kept" not in caplog.text


def test_a_conflict_says_what_the_nodes_hold_without_any_key() -> None:
    """`held` names an entry by its identifying fields, never writes one out whole (a node entry carries its device
    key) and never renders a key field."""
    room = {"address": "C64B", "name": "Attic", "parentAddress": "0000"}
    assert mc.held(Change(("network", "groups", Key("C64B")), MISSING, room)) == (
        "address C64B, name Attic"
    )
    node = {"deviceKey": "00" * 16, "security": "secure"}
    assert mc.held(Change(("network", "nodes", Key("X")), MISSING, node)) == "an entry"
    assert mc.held(Change(("network", "nodes", Key("X"), "deviceKey"), "a", "b")) == (
        "a key (not shown)"
    )
    assert mc.held(Change(("network", "groups", Key("C64B")), room, MISSING)) == (
        "nothing (Home Assistant removed it)"
    )
    subscribe = ("network", "nodes", Key("X"), "elements", Key(0), "subscribe")
    assert mc.held(Change(subscribe, [], ["C00F", "C64B"], members=True)) == (
        "C00F, C64B"
    )
    assert mc.held(Change(subscribe, ["C00F"], [], members=True)) == "none"


async def test_home_assistants_rooms_and_scenes_do_not_take_the_apps_next_numbers(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """Review-4 D6 (W4-2): without a provisioner of its own, Home Assistant took the lowest free group address and
    scene number of the app's range — the app's own next allocation. The app never downloads the project, so its
    next room and scene got the same numbers: on air both rooms were one group, and adopting the app's upload kept
    the app's room and scene and reported four conflicts. Home Assistant now allocates from the top of the range."""
    with_gateway.serve_uploads = True
    app = ProjectFile.load(bench.path)  # the app's copy: it never sees what HA adds
    room = await bench.configurator.create_room("Attic")
    scene = await bench.configurator.create_scene("Evening")
    app_room = app.add_group("From the app")
    app_scene = app.add_scene("Morning")
    assert room != app_room
    assert scene != app_scene
    with_gateway.doc = app_upload(app)
    await bench.configurator.create_room("Cellar")  # adopts the app's upload first
    merged = bench.reload()
    assert merged.user_groups()[room] == "Attic"
    assert merged.user_groups()[app_room] == "From the app"
    assert merged.scene_names()[scene] == "Evening"
    assert merged.scene_names()[app_scene] == "Morning"
    assert "the app's version is kept" not in caplog.text


async def test_the_app_wins_what_both_changed(
    bench: Bench,
    with_gateway: FakeGateway,
    issues: dict[str, dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The app's version is kept, and the conflict is a repair, no longer a log line only (review-4 W4-5): the
    nodes may still hold what Home Assistant wrote. The next adopt without a conflict clears it."""
    with_gateway.serve_uploads = True
    app = ProjectFile.load(bench.path)
    await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    rocker = app.cdb.element(ROCKER_A)
    assert rocker is not None
    app.set_publication(
        rocker.node, rocker, "1001", 0xC0FE
    )  # the app re-linked the same key
    with_gateway.doc = app_upload(app)
    await bench.configurator.create_room("Attic")
    assert pub(bench.reload(), ROCKER_A, "1001") == 0xC0FE
    assert "the app's version is kept for" in caplog.text
    issue = issues[f"carry_over_conflict_{bench.hub.entry.entry_id}"]
    path = issue["paths"]
    assert "models[1001].publish" in path
    # what the nodes still hold: HA's publication of the key, whose address is the dimmer's element group
    assert issue["held"] == f"{path}: {DIMMER_GROUP:04X}"
    # the app's next upload changes nothing Home Assistant changed since: a clean adopt clears the repair
    app.add_group("From the app")
    with_gateway.doc = app_upload(app)
    await bench.configurator.create_room("Cellar")
    assert "From the app" in bench.reload().user_groups().values()
    assert f"carry_over_conflict_{bench.hub.entry.entry_id}" not in issues


async def test_without_the_apps_copy_or_with_an_unreadable_one_the_upload_is_taken_as_it_is(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    app = ProjectFile.load(bench.path)
    await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    mc.app_copy_path(bench.path).write_text("not json")
    with_gateway.doc = app_upload(app)
    await bench.configurator.create_room("Attic")
    assert "changes were not carried over" in caplog.text
    assert pub(bench.reload(), ROCKER_A, "1001") != DIMMER_GROUP
    # no copy at all (an entry set up before the merge existed): same, silently
    mc.app_copy_path(bench.path).unlink()
    with_gateway.doc = app_upload(app)
    await bench.configurator.create_room("Loft")
    assert mc.app_copy_path(bench.path).exists()  # kept from now on


async def test_a_newer_gateway_export_is_adopted_and_an_unreadable_file_is_not_compared(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    with_gateway.doc = gateway_doc(bench.path, room="From the app")
    await bench.configurator.create_room("Attic")
    assert "adopted it before the change" in caplog.text
    rooms = list(bench.reload().user_groups().values())
    assert rooms[-2:] == ["From the app", "Attic"]
    assert len(with_gateway.uploads) == 1
    # the file on disk unreadable: no comparison, `_read` reports it
    bench.path.unlink()
    with_gateway.doc = gateway_doc(ANDROID_PATH, room="Elsewhere")
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("Loft")
    assert exc.value.translation_key == "service_export_load_failed"
    assert not bench.path.exists()
    # the file on disk is no export at all: the gateway's copy replaces it
    bench.path.write_text("{}")
    await bench.configurator.create_room("Loft")
    assert "than the copy on disk (no timestamp)" in caplog.text
    assert "Elsewhere" in bench.reload().user_groups().values()


async def test_an_adopted_export_is_written_atomically_with_a_backup(
    bench: Bench, with_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_gateway.doc = gateway_doc(bench.path, room="From the app")
    real_replace = Path.replace
    replaced: list[Path] = []

    def spy(self: Path, target: Path) -> Path:
        replaced.append(Path(target))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    await bench.configurator._adopt_gateway_export()
    bak = bench.path.with_name(bench.path.name + ".bak")
    assert bak.read_bytes() == bench.original
    assert "From the app" in bench.reload().user_groups().values()
    assert any(bench.path.resolve() == p.resolve() for p in replaced)
    # review-3 W7: the copy the adoption replaced outlives the rotating backups of the saves after it
    pre_adopt = mc.pre_adopt_path(bench.path)
    assert pre_adopt.read_bytes() == bench.original
    assert stat.S_IMODE(pre_adopt.stat().st_mode) == 0o600
    for _ in range(3):
        bench.reload().save()
    assert bench.original not in [p.read_bytes() for p in backup_paths(bench.path)]
    assert pre_adopt.read_bytes() == bench.original


async def test_an_adopted_export_that_cannot_be_written_is_a_translated_error(
    bench: Bench, with_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_gateway.doc = gateway_doc(bench.path, room="From the app")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(mc, "write_private_with_backup", refuse)
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("Attic")
    assert exc.value.translation_key == "service_export_write_failed"
    assert exc.value.translation_placeholders["error"] == "read-only"
    assert bench.file_unchanged()


async def test_sync_gateway_refuses_to_overwrite_a_newer_export(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    with_gateway.doc = gateway_doc(bench.path, room="From the app")
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.sync_gateway()
    assert exc.value.translation_key == "service_gateway_export_newer"
    assert exc.value.translation_placeholders["host"] == "junghome.local"
    assert (
        exc.value.translation_placeholders["gateway"]
        > (exc.value.translation_placeholders["file"])
    )
    assert with_gateway.uploads == []
    assert bench.file_unchanged()
    # the gateway's content matches what HA last synced again (a re-fetch of its own unchanged copy): allowed
    with_gateway.doc = json.loads(ProjectFile.load(bench.path).share_json())
    assert await bench.configurator.sync_gateway() is False
    assert len(with_gateway.uploads) == 1


def gateway_doc_without_timestamp(
    path: Path, *, room: str | None = None
) -> dict[str, Any]:
    """`gateway_doc` without the CDB `timestamp` field: some gateway response `CDB.parse` accepts anyway."""
    doc = gateway_doc(path, room=room)
    inner = json.loads(base64.b64decode(doc["network"]))
    del inner["timestamp"]
    doc["network"] = base64.b64encode(json.dumps(inner).encode()).decode()
    return doc


async def test_a_gateway_export_without_a_timestamp_is_still_compared_by_content(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """Change detection is by content digest now (CFG-11), not the CDB `timestamp`: a gateway response missing it
    (some firmware responses `CDB.parse` still accepts) is still recognised as changed and adopted — the empty
    timestamp only shows up in the log text, it never stops the digest comparison from working."""
    with_gateway.doc = gateway_doc_without_timestamp(bench.path, room="From the app")
    await bench.configurator.create_room("Attic")
    assert "adopted it before the change" in caplog.text
    rooms = list(bench.reload().user_groups().values())
    assert "Attic" in rooms
    assert "From the app" in rooms


async def test_a_legacy_entry_without_a_synced_digest_bootstraps_one_when_the_gateway_is_not_newer(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    """No digest recorded yet (an entry from before this fix) and the gateway is not ahead by the old timestamp
    rule: nothing has changed as far as HA can tell, so today's digest becomes the baseline instead of comparing
    against nothing forever."""
    bench.hub.entry.data = {
        k: v for k, v in bench.hub.entry.data.items() if k != CONF_GATEWAY_SYNCED
    }
    await bench.configurator.create_room("Attic")
    assert CONF_GATEWAY_SYNCED in bench.hub.entry.data
    assert "Attic" in bench.reload().user_groups().values()


async def test_a_legacy_entry_without_a_synced_digest_adopts_when_the_gateway_is_newer(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    """No digest recorded yet and the gateway *is* ahead by the old timestamp rule: treated as "gateway changed,
    local unchanged" — the best a legacy entry can do without history — so it adopts instead of refusing as
    "both changed"."""
    bench.hub.entry.data = {
        k: v for k, v in bench.hub.entry.data.items() if k != CONF_GATEWAY_SYNCED
    }
    with_gateway.doc = gateway_doc(bench.path, room="From the app")
    await bench.configurator.create_room("Attic")
    rooms = list(bench.reload().user_groups().values())
    assert "From the app" in rooms
    assert "Attic" in rooms


async def test_a_legacy_entrys_bootstrap_tolerates_an_on_disk_timestamp_it_cannot_read(
    bench: Bench, with_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bootstrap's own timestamp read is best-effort too: unreadable there (though the digest read moments
    earlier succeeded) just means "unknown", not a crash — the missing-on-disk-timestamp case prefers adopting
    the gateway, same as when nothing on disk exists yet."""
    bench.hub.entry.data = {
        k: v for k, v in bench.hub.entry.data.items() if k != CONF_GATEWAY_SYNCED
    }
    with_gateway.doc = gateway_doc(bench.path, room="From the app")

    def boom() -> str | None:
        raise OSError("gone")

    monkeypatch.setattr(bench.configurator, "_timestamp_on_disk", boom)
    await bench.configurator.create_room("Attic")
    rooms = list(bench.reload().user_groups().values())
    assert "From the app" in rooms
    assert "Attic" in rooms


def test_digest_on_disk_returns_none_for_unparsable_json(bench: Bench) -> None:
    bench.path.write_text("not json")
    assert bench.configurator._digest_on_disk() is None


async def test_disk_timestamp_or_none_swallows_an_unreadable_file(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> str | None:
        raise OSError("gone")

    monkeypatch.setattr(bench.configurator, "_timestamp_on_disk", boom)
    assert await bench.configurator._disk_timestamp_or_none() is None


async def test_a_meta_only_app_change_on_the_gateway_is_adopted_not_overwritten(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    """A device rename touches only `meta`, never the CDB `timestamp` (CFG-11): the old timestamp-only rule saw
    "not newer" and planned on HA's own (stale) copy, reverting the rename on both sides. Content digest catches
    it: the file and the next upload keep the app's rename."""
    doc = json.loads(ProjectFile.load(bench.path).share_json())
    doc["meta"]["devices"][0]["name"] = (
        "Island"  # meta only; the CDB timestamp is untouched
    )
    with_gateway.doc = doc
    await bench.configurator.create_room("Attic")
    assert ProjectFile.load(bench.path).meta["devices"][0]["name"] == "Island"
    assert with_gateway.uploads[-1]["meta"]["devices"][0]["name"] == "Island"


async def test_a_change_planned_while_the_gateway_was_busy_is_not_uploaded_over_its_newer_export(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """A fetch that fails leaves the pre-plan check unable to compare (CFG-04/05): the change still goes ahead
    on the copy on disk, but `_upload`'s own check — run again, right before the POST — refuses to hand a
    change the app might have added to since blindly to the gateway."""
    calls = 0

    def doc() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return GatewayBusy("busy")
        return gateway_doc(
            bench.path,
            room="From the app",
            stamp=datetime.now(UTC) - timedelta(seconds=30),
        )

    with_gateway.doc = doc
    await bench.configurator.create_room("Attic")
    assert "Attic" in bench.reload().user_groups().values()
    assert with_gateway.uploads == []
    assert "not handed to the gateway" in caplog.text
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.sync_gateway()
    assert exc.value.translation_key == "service_gateway_export_newer"
    assert with_gateway.uploads == []


async def test_a_failed_upload_is_retried_like_the_app_does(
    bench: Bench, with_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app tries a failed upload again twice, 15 s apart (`ProjectFileSyncServiceImpl`: `retry(2)`, 15 000
    ms): so does an automatic upload here, in the background, with the same export; the time of the upload that
    went through is recorded (`gateway_last_sync`)."""
    fa = FastAsyncio()
    monkeypatch.setattr(mc, "asyncio", fa)
    refusals: list[Exception] = [GatewayBusy("busy"), GatewayError("HTTP 500")]
    real_upload = with_gateway.upload_project

    async def upload_project(export: dict[str, Any]) -> None:
        if refusals:
            raise refusals.pop(0)
        await real_upload(export)

    with_gateway.upload_project = upload_project  # type: ignore[method-assign]
    with_gateway.serve_uploads = True
    assert CONF_GATEWAY_LAST_SYNC not in bench.hub.entry.data
    before = datetime.now(UTC)
    await bench.configurator.create_room("Attic")
    assert (
        with_gateway.uploads == []
    )  # the change stands; the upload is retried behind it
    retry = bench.configurator.upload_retry
    assert retry is not None
    await retry
    assert fa.sleeps == [mc.GATEWAY_UPLOAD_RETRY_DELAY] * 2 == [15.0, 15.0]
    assert len(with_gateway.uploads) == 1
    assert "Attic" in json.dumps(with_gateway.uploads[0]["meta"])
    assert bench.hub.entry.data[CONF_GATEWAY_SYNCED] == mc.export_digest(
        with_gateway.uploads[0]
    )
    assert (
        datetime.fromisoformat(bench.hub.entry.data[CONF_GATEWAY_LAST_SYNC]) >= before
    )
    assert bench.configurator.upload_retry is None

    # three failures in a row: the app gives up after the second retry, and so does this (the repair stays)
    refusals[:] = [GatewayBusy("busy")] * 4
    fa.sleeps.clear()
    await bench.configurator.create_room("Cellar")
    retry = bench.configurator.upload_retry
    assert retry is not None
    await retry
    assert fa.sleeps == [15.0, 15.0]
    assert len(refusals) == 1  # one attempt and two retries took three
    assert len(with_gateway.uploads) == 1


async def test_a_refused_upload_is_not_retried_and_a_newer_one_supersedes_a_retry(
    bench: Bench, with_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal of ours (the gateway holds changes HA has not seen, a rejected token) is final: no retry. A
    pending retry is dropped by the next change's own upload, and by `sync_gateway`."""
    fa = FastAsyncio()
    monkeypatch.setattr(mc, "asyncio", fa)
    with_gateway.refuse_upload = GatewayAuthError("POST config: HTTP 401")
    monkeypatch.setattr(mc.MeshConfigurator, "report_token_rejected", lambda *_a: None)
    await bench.configurator.create_room("Attic")
    assert bench.configurator.upload_retry is None
    calls = 0

    def doc() -> (
        Any
    ):  # checkable once (the plan), then newer than what HA synced (the upload's own check)
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.loads(bench.reload().share_json())
        return gateway_doc(bench.path, room="From the app")

    with_gateway.refuse_upload = None
    bench.hub.entry.data = {
        **bench.hub.entry.data,
        CONF_GATEWAY_SYNCED: mc.export_digest(json.loads(bench.reload().share_json())),
    }
    with_gateway.doc = doc
    await bench.configurator.create_room("Cellar")
    assert bench.configurator.upload_retry is None
    assert with_gateway.uploads == []

    # a pending retry: the next change's upload, or `sync_gateway`, replaces it
    with_gateway.doc = json.loads(bench.reload().share_json())
    bench.hub.entry.data = {
        **bench.hub.entry.data,
        CONF_GATEWAY_SYNCED: mc.export_digest(with_gateway.doc),
    }
    with_gateway.refuse_upload = GatewayBusy("busy")
    await bench.configurator.create_room("Loft")
    first = bench.configurator.upload_retry
    assert first is not None
    await bench.configurator.create_room("Den")
    assert first.cancelled() or first.cancelling()
    second = bench.configurator.upload_retry
    assert second is not None
    assert second is not first
    with_gateway.refuse_upload = None
    with_gateway.serve_uploads = True
    await bench.configurator.sync_gateway()
    assert second.cancelled() or second.cancelling()
    assert bench.configurator.upload_retry is None
    assert len(with_gateway.uploads) == 1


async def test_a_retry_follows_the_entry_through_a_reload_and_ends_without_it(
    bench: Bench,
    with_gateway: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retry is the entry's, not the configurator's: it waits out a reload (the setup lock held), then uploads
    the export on disk through the configurator the reload created. An entry that is not loaded after all, or an
    export that no longer loads, ends it."""
    fa = FastAsyncio()
    monkeypatch.setattr(mc, "asyncio", fa)
    entry = bench.hub.entry
    with_gateway.refuse_upload = GatewayBusy("busy")
    await entry.setup_lock.acquire()  # a reload in progress
    await bench.configurator.create_room("Attic")
    retry = bench.configurator.upload_retry
    assert retry is not None
    for _ in range(5):
        await asyncio.sleep(0)
    assert fa.sleeps[0] == mc.GATEWAY_UPLOAD_RETRY_DELAY
    assert set(fa.sleeps[1:]) == {mc.RELOAD_POLL}  # waiting, the attempt not spent
    # the reload replaced the hub's configurator; it takes the upload
    replacement = MeshConfigurator(bench.hub)  # type: ignore[arg-type]
    assert bench.hub.configurator is replacement  # type: ignore[attr-defined]
    with_gateway.refuse_upload = None
    with_gateway.serve_uploads = True
    entry.setup_lock.release()
    await retry
    assert len(with_gateway.uploads) == 1
    assert "Attic" in json.dumps(with_gateway.uploads[0]["meta"])
    assert replacement.upload_retry is None

    # not loaded once the lock is free (disabled, say): the retries end, nothing is sent
    with_gateway.serve_uploads = False
    with_gateway.refuse_upload = GatewayBusy("busy")
    await replacement.create_room("Cellar")
    entry.state = ConfigEntryState.NOT_LOADED
    retry = replacement.upload_retry
    assert retry is not None
    await retry
    assert "the entry is not loaded" in caplog.text
    assert len(with_gateway.uploads) == 1
    # gone altogether: the same
    entry.state = ConfigEntryState.LOADED
    await replacement.create_room("Loft")
    del bench.hub.hass.config_entries.entries[entry.entry_id]
    retry = replacement.upload_retry
    assert retry is not None
    await retry
    assert len(with_gateway.uploads) == 1
    bench.hub.hass.config_entries.entries[entry.entry_id] = entry

    # the export no longer loads: said so, the retries end
    await replacement.create_room("Den")
    retry = replacement.upload_retry
    assert retry is not None
    bench.path.write_text("not an export")
    await retry
    assert "Not handing the mesh export to the gateway again" in caplog.text
    assert len(with_gateway.uploads) == 1


async def test_both_sides_changed_is_refused_before_planning(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    """HA's own copy changed (an earlier upload never reached the gateway) and the app changed the gateway's
    copy too: neither side is silently dropped by planning on one and uploading over the other."""
    with_gateway.doc = GatewayBusy("busy")
    await bench.configurator.create_room(
        "Attic"
    )  # planned and saved locally; not uploaded
    assert "Attic" in bench.reload().user_groups().values()
    with_gateway.doc = gateway_doc(bench.path, room="From the app")
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("Loft")
    assert exc.value.translation_key == "service_gateway_export_newer"
    reloaded = bench.reload()
    assert "Loft" not in reloaded.user_groups().values()
    assert "From the app" not in reloaded.user_groups().values()
    assert "Attic" in reloaded.user_groups().values()  # kept: the earlier local change


async def test_a_cdb_only_gateway_export_never_replaces_a_share_export(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    """`/project/cdb`'s bare CDB fallback carries no `meta`: adopting it over a share export on disk would erase
    every device name and room link (CFG-04)."""
    inner = json.loads(
        base64.b64decode(gateway_doc(bench.path, room="From the app")["network"])
    )
    with_gateway.doc = {"meshNetwork": inner}
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.create_room("Attic")
    assert exc.value.translation_key == "service_gateway_export_incomplete"
    assert bench.file_unchanged()
    assert with_gateway.uploads == []


async def test_a_cdb_only_gateway_export_over_a_disk_file_with_no_meta_either_plans_on_disk(
    bench: Bench, with_gateway: FakeGateway
) -> None:
    """Neither side has anything worth protecting (both bare CDB documents, no `meta` anywhere): plan on the
    copy on disk as today, instead of refusing a change that could not have lost anything."""
    inner = json.loads(base64.b64decode(gateway_doc(bench.path)["network"]))
    with_gateway.doc = {"meshNetwork": inner}
    bench.path.write_text(json.dumps({"meshNetwork": inner}))
    await bench.configurator._adopt_gateway_export()  # no exception
    assert json.loads(bench.path.read_text()) == {"meshNetwork": inner}


def test_export_digest_ignores_whitespace_and_key_order() -> None:
    doc = json.loads(ProjectFile.load(CDB_PATH).share_json())
    compact = mc.export_digest(doc)
    net = json.loads(base64.b64decode(doc["network"]))
    reordered = dict(reversed(list(net.items())))
    doc2 = {
        **doc,
        "network": base64.b64encode(
            json.dumps(reordered, indent=4, sort_keys=False).encode()
        ).decode(),
    }
    assert mc.export_digest(doc2) == compact


def test_export_digest_matches_across_save_and_share_json() -> None:
    pf = ProjectFile.load(CDB_PATH)
    saved = json.loads(pf.render("share"))
    assert mc.export_digest(saved) == mc.export_digest(json.loads(pf.share_json()))


def test_export_digest_differs_for_a_meta_only_change() -> None:
    doc = json.loads(ProjectFile.load(ANDROID_PATH).share_json())
    changed = json.loads(json.dumps(doc))
    changed["meta"]["devices"][0]["name"] = "Renamed"
    assert mc.export_digest(changed) != mc.export_digest(doc)


def test_export_digest_is_none_for_a_cdb_only_document() -> None:
    inner = json.loads(
        base64.b64decode(json.loads(ProjectFile.load(CDB_PATH).share_json())["network"])
    )
    assert mc.export_digest({"meshNetwork": inner}) is None


def test_export_digest_unwraps_an_already_wrapped_network_field() -> None:
    doc = json.loads(ProjectFile.load(CDB_PATH).share_json())
    net = json.loads(base64.b64decode(doc["network"]))
    wrapped = {
        **doc,
        "network": base64.b64encode(json.dumps({"meshNetwork": net}).encode()).decode(),
    }
    assert mc.export_digest(wrapped) == mc.export_digest(doc)


async def test_a_change_that_is_already_so_still_reports_an_adopted_gateway_export(
    bench: Bench, with_gateway: FakeGateway, scenes: SceneServer
) -> None:
    """The app's newer export is adopted before the plan; the plan then finds nothing to do. The file changed all
    the same, so the call reports a changed device model (the entry reloads) and `recorded` says it was written."""
    for change in (
        lambda: bench.configurator.clear_key(DIMMER_KEY),
        lambda: bench.configurator.rename_scene(1, "WC off"),
    ):
        assert await change() is True
        with_gateway.doc = with_gateway.uploads[
            -1
        ]  # the gateway holds what was handed over
    no_ops: list[Callable[[], Any]] = [
        lambda: bench.configurator.rename_room("WC", "WC"),
        lambda: bench.configurator.set_room(SWITCH_LOAD, "WC"),
        lambda: bench.configurator.clear_key(DIMMER_KEY),
        lambda: bench.configurator.rename_scene(1, "WC off"),
    ]
    for n, no_op in enumerate(no_ops):
        # a new call: its flags start over, as `services._run` resets them
        bench.configurator.recorded = bench.configurator.adopted = False
        assert await no_op() is False  # in sync with the gateway: nothing to do
        assert bench.configurator.recorded is False
        with_gateway.doc = gateway_doc(bench.path, room=f"From the app {n}")
        assert await no_op() is True
        assert bench.configurator.recorded is True
        assert f"From the app {n}" in bench.reload().user_groups().values()


def test_a_scene_name_left_over_in_meta_does_not_shadow_a_scene(bench: Bench) -> None:
    """A `meta.scenes[]` row whose scene is gone from the CDB names no scene: its name neither makes a number
    ambiguous nor wins over the scene of that name; digits that are not ASCII are no number."""
    pf = bench.reload()
    pf.meta["scenes"].insert(0, {"number": 9, "name": "2"})
    pf.meta["scenes"].insert(0, {"number": 8, "name": "WC off"})
    assert 8 not in pf.cdb.scenes
    assert 9 not in pf.cdb.scenes
    assert MeshConfigurator._scene(pf, "2") == 2
    assert MeshConfigurator._scene(pf, "WC off") == 1
    with pytest.raises(ServiceValidationError) as exc:
        MeshConfigurator._scene(pf, "²")
    assert exc.value.translation_key == "service_unknown_scene"


# ----------------------------------------------------------------------------- gateway trust and access (bench level)


@pytest.fixture
def issues(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """The repair issues the configurator raised (id → translation placeholders); a deleted one is dropped."""
    raised: dict[str, dict[str, Any]] = {}

    def create(_hass: Any, _domain: str, issue_id: str, **kwargs: Any) -> None:
        raised[issue_id] = kwargs["translation_placeholders"]

    def delete(_hass: Any, _domain: str, issue_id: str) -> None:
        raised.pop(issue_id, None)

    class Registry:
        @staticmethod
        def async_get_issue(_domain: str, issue_id: str) -> dict[str, Any] | None:
            return raised.get(issue_id)

    monkeypatch.setattr(mc.ir, "async_create_issue", create)
    monkeypatch.setattr(mc.ir, "async_delete_issue", delete)
    monkeypatch.setattr(mc.ir, "async_get", lambda _hass: Registry)
    return raised


async def test_a_gateway_whose_pin_is_not_vouched_for_is_neither_asked_nor_uploaded_to(
    bench: Bench, with_gateway: FakeGateway, issues: dict[str, dict[str, Any]]
) -> None:
    """The hub has not had the pin confirmed by the gateway node (or the node contradicted it): the change goes
    through on disk, the gateway is not asked, nothing is handed to it, and the entry's sync issue says why."""
    with_gateway.doc = AssertionError("must not be asked")
    bench.hub.distrust = "not confirmed yet"
    await bench.configurator.create_room("Attic")
    assert "Attic" in bench.reload().user_groups().values()
    assert with_gateway.uploads == []
    assert issues == {
        "gateway_sync_failed_entry": {
            "host": "junghome.local",
            "error": "not confirmed yet",
        }
    }
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.sync_gateway()
    assert exc.value.translation_key == "service_gateway_sync_failed"
    assert exc.value.translation_placeholders == {
        "host": "junghome.local",
        "error": "not confirmed yet",
    }


async def test_a_rejected_token_raises_its_own_repair(
    bench: Bench,
    with_gateway: FakeGateway,
    issues: dict[str, dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HTTP 401 on the fetch or the upload: not "check reachability" but the token repair pointing to
    Reconfigure (its warning logged once while the repair is open); a change still goes through on disk,
    `sync_gateway` says so; the next answer clears it."""
    rejected = "access token: nothing is exchanged with it"
    in_sync = with_gateway.doc
    with_gateway.doc = GatewayAuthError("GET project/junghome: HTTP 401")
    await bench.configurator.create_room("Attic")
    assert "Attic" in bench.reload().user_groups().values()
    assert issues == {
        "gateway_token_rejected_entry": {
            "host": "junghome.local",
            "title": "JUNG HOME mesh test",
        }
    }
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.sync_gateway()
    assert exc.value.translation_key == "service_gateway_token_rejected"
    assert exc.value.translation_placeholders == {"host": "junghome.local"}
    assert caplog.text.count(rejected) == 1  # the repair is open: not logged again
    # the fetch passes again (a new token), the upload is refused
    with_gateway.doc = in_sync
    with_gateway.refuse_upload = GatewayAuthError("POST config: HTTP 401")
    issues.clear()
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.sync_gateway()
    assert exc.value.translation_key == "service_gateway_token_rejected"
    assert list(issues) == ["gateway_token_rejected_entry"]
    assert caplog.text.count(rejected) == 2  # raised anew
    await bench.configurator.create_room("Loft")  # not raised for a change
    assert with_gateway.uploads == []
    # accepted again: the repairs go, the legacy (not per-entry) sync issue with them
    issues["gateway_sync_failed"] = {}
    issues["gateway_sync_failed_entry"] = {}
    with_gateway.refuse_upload = None
    await bench.configurator.sync_gateway()
    assert len(with_gateway.uploads) == 1
    assert issues == {}


async def test_a_gateway_presenting_another_certificate_raises_the_certificate_repair(
    bench: Bench, with_gateway: FakeGateway, issues: dict[str, dict[str, Any]]
) -> None:
    """The pinned connection is refused at the handshake and the gateway node does not name another address:
    the hub raises the certificate repair, and the upload does not go out."""
    with_gateway.doc = GatewayCertificateMismatch(
        "junghome.local", "ab" * 32, "cd" * 32
    )
    await bench.configurator.create_room("Attic")
    assert bench.hub.certificate_issues == 2  # before the change and at the upload
    assert with_gateway.uploads == []
    assert (
        issues["gateway_sync_failed_entry"]["error"]
        == "it presents another certificate than the pinned one"
    )


async def test_the_unknown_node_refresh_logs_an_export_the_hub_could_not_load(
    bench: Bench,
    with_gateway: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(_text: str) -> set[str]:
        raise InvalidExport("no usable network key")

    monkeypatch.setattr(mc, "_listed_macs", broken)
    assert await bench.configurator.adopt_for_unknown_nodes(["30:FB:10:00:00:01"]) == []
    assert "does not parse: no usable network key" in caplog.text
    assert bench.file_unchanged()


def test_the_configurator_registers_with_its_hub(bench: Bench) -> None:
    assert bench.hub.configurator is bench.configurator  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------- battery nodes (review-3 W4 / F24)

DETECTORS_PATH = FIXTURES / "MeshNetwork-detectors.json"
TRANSMITTER, TRANSMITTER_KEY = (
    0x0520,
    0x0521,
)  # wall transmitter 1-gang (battery), its key -> gateway (C005)
TRANSMITTER_KEY_GROUP = 0xC0A5
MOTION_RELAY = 0x0500  # the motion detector's relay: the WC room's only load
KEEP_ALIVE = bytes.fromhex(
    "c2 2705 0150"
)  # the app's: LBC Admin Get of the ButtonLayout (0x5001)


@pytest.fixture
async def battery_bench(tmp_path: Path, fast: FastAsyncio) -> Bench:
    return await make_bench(tmp_path, DETECTORS_PATH)


def test_ordered_sends_the_sleepy_nodes_steps_first_within_each_half() -> None:
    """Additions still go before clears; within each half the battery node's steps lead, in plan order."""
    add = mc.ModelChange(ROCKER_A, "1001", DIMMER_GROUP, "subscribe")
    drop = mc.ModelChange(ROCKER_A, "1001", DALI_GROUP, "unsubscribe")
    steps = [
        mc.ConfigStep(DALI_NODE, b"mains drop", 0, change=drop),
        mc.ConfigStep(TRANSMITTER, b"sleepy drop", 0, change=drop),
        mc.ConfigStep(DALI_NODE, b"mains add", 0, change=add),
        mc.ConfigStep(TRANSMITTER, b"sleepy add 1", 0, change=add),
        mc.ConfigStep(TRANSMITTER, b"sleepy add 2", 0, bind=(ROCKER_A, "1001")),
    ]
    assert [s.pdu for s in mc.ordered(steps, {TRANSMITTER})] == [
        b"sleepy add 1",
        b"sleepy add 2",
        b"mains add",
        b"sleepy drop",
        b"mains drop",
    ]
    assert [s.pdu for s in mc.ordered(steps)] == [  # no battery node: the plan's order
        b"mains add",
        b"sleepy add 1",
        b"sleepy add 2",
        b"mains drop",
        b"sleepy drop",
    ]


async def test_a_battery_keys_steps_go_first_and_it_is_kept_awake_meanwhile(
    battery_bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A room link of the transmitter's key: its own Config steps before the relay's subscription (the plan lists
    the relay first), its clears last; the node gets the app's keep-alive while the plan and the vendor writes run
    — here in every quiet moment — and none once the action is done."""
    monkeypatch.setattr(keep_awake_mod, "KEEP_AWAKE_INTERVAL", 0.0)
    assert await battery_bench.configurator.assign_key(TRANSMITTER_KEY, room="WC")
    assert battery_bench.config_pdus() == [
        (TRANSMITTER, pub_set(TRANSMITTER_KEY, TRANSMITTER_KEY_GROUP, "1001")),
        (TRANSMITTER, sub_add(TRANSMITTER_KEY, TRANSMITTER_KEY_GROUP, "1001")),
        (TRANSMITTER, pub_set(TRANSMITTER_KEY, TRANSMITTER_KEY_GROUP, "1003")),
        (TRANSMITTER, sub_add(TRANSMITTER_KEY, TRANSMITTER_KEY_GROUP, "1003")),
        (TRANSMITTER, pub_set(TRANSMITTER_KEY, TRANSMITTER_KEY_GROUP, "05271015")),
        (TRANSMITTER, sub_add(TRANSMITTER_KEY, TRANSMITTER_KEY_GROUP, "05271015")),
        (MOTION_RELAY, sub_add(MOTION_RELAY, TRANSMITTER_KEY_GROUP, "1000")),
        (TRANSMITTER, sub_del(TRANSMITTER_KEY, GATEWAY_GROUP, "1001")),
        (TRANSMITTER, sub_del(TRANSMITTER_KEY, GATEWAY_GROUP, "05271015")),
    ]
    app = battery_bench.app_pdus()
    keep_alives = [dst for dst, pdu in app if pdu == KEEP_ALIVE]
    assert keep_alives
    assert set(keep_alives) == {TRANSMITTER}  # the node's primary element only
    assert [(dst, pdu) for dst, pdu in app if pdu != KEEP_ALIVE] == [
        (TRANSMITTER_KEY, p) for p in RESET_PROPERTY_MODE
    ] + [(TRANSMITTER_KEY, admin_set(0x5003, b"\x00"))]
    assert battery_bench.hub.keep_awake._tasks == {}
    for _ in range(20):
        await asyncio.sleep(0)
    assert battery_bench.app_pdus() == app  # stopped with the action


async def test_a_sleeping_battery_key_stops_the_plan_at_its_first_message(
    battery_bench: Bench,
) -> None:
    """The press-a-key-then-run guard: the transmitter's first step goes unanswered, so nothing was applied — the
    relay was never asked — and the error asks for a key press instead of blaming power or range."""
    first = pub_set(TRANSMITTER_KEY, TRANSMITTER_KEY_GROUP, "1001")
    battery_bench.config.silent.add(first)
    with pytest.raises(HomeAssistantError) as exc:
        await battery_bench.configurator.assign_key(TRANSMITTER_KEY, room="WC")
    assert exc.value.translation_key == "service_node_asleep"
    assert exc.value.translation_placeholders == {
        "node": "0520",
        "message": M.describe(first),
        "applied": mc.APPLIED_NOTHING,
    }
    assert set(battery_bench.config_pdus()) == {(TRANSMITTER, first)}
    assert battery_bench.file_unchanged()
    assert battery_bench.hub.keep_awake._tasks == {}


async def test_a_battery_key_asleep_at_its_key_mode_says_so(
    tmp_path: Path, fast: FastAsyncio
) -> None:
    """Every Config step was taken, then the key fell silent before its KeyMode: the same asleep error, saying the
    wiring is in and only the key mode is missing."""
    bench = await make_bench(tmp_path, DETECTORS_PATH, answer_sets=False)
    bench.keys.answer_gets = False
    with pytest.raises(HomeAssistantError) as exc:
        await bench.configurator.assign_key(TRANSMITTER_KEY, room="WC")
    assert exc.value.translation_key == "service_node_asleep"
    assert exc.value.translation_placeholders["node"] == "0521"
    assert exc.value.translation_placeholders["applied"] == mc.APPLIED_KEY_WIRED
    assert bench.hub.keep_awake._tasks == {}


async def test_a_stray_button_layout_status_does_not_answer_the_key_mode(
    battery_bench: Bench,
) -> None:
    """Every Admin Status of the transmitter's key comes after a ButtonLayout Status from it (a battery key's
    keep-alive answer, or a late one): neither the KeyMode Set nor a read-back takes that for its answer."""
    keys = battery_bench.keys
    reply = keys.reply

    def layout_first(element: int, prop: int, value: bytes) -> None:
        reply(element, 0x5001, b"\x03\x00")
        reply(element, prop, value)

    keys.reply = layout_first  # type: ignore[method-assign]
    assert await battery_bench.configurator.assign_key(TRANSMITTER_KEY, room="WC")
    assert keys.modes[TRANSMITTER_KEY] == b"\x00"
    assert M.vendor_property_get("admin", 0x5003) not in [
        pdu for _, pdu in battery_bench.app_pdus()
    ]  # the Set's own Status confirmed it: no read-back


# ----------------------------------------------------------------------------- scene hygiene (the app's use cases)


async def test_a_full_register_refuses_a_new_scene_before_the_store(
    bench: Bench, scenes: SceneServer
) -> None:
    """The app's capacity check: a node's register holds 16 scenes (timer scenes included); a 17th is refused
    before anything is stored. A scene it already holds is no new slot."""
    scenes.registers[DALI_LOAD] = list(range(100, 116))
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert err.value.translation_key == "service_scene_no_capacity"
    assert err.value.translation_placeholders == {
        "address": "0232",
        "scene": "2",
        "capacity": "16",
        "applied": mc.APPLIED_NOTHING,
    }
    assert scenes.seen == []  # no Store went out
    assert bench.file_unchanged()
    scenes.registers[DALI_LOAD] = [*range(100, 115), 2]
    assert await bench.configurator.store_scene(
        2, DALI_LOAD, ON
    )  # stored again: no new slot


async def test_a_channel_with_its_own_list_holds_eight_scenes(
    bench: Bench, scenes: SceneServer
) -> None:
    """A channel of a node whose channels keep their own scene list is asked for that list: 8 is full. A list it
    does not give stops the call (the app counts it full); one too short to be a list is no list."""
    action = V.Action(V.ACTION_SWITCH, on=True).encode()
    for n in range(100, 108):
        scenes.actions[(ACTUATOR_OUT2, n)] = action
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.store_scene(2, ACTUATOR_OUT2, ON)
    assert err.value.translation_key == "service_scene_no_capacity"
    assert err.value.translation_placeholders["address"] == "0401"
    assert err.value.translation_placeholders["capacity"] == "8"
    assert scenes.seen == []
    # the other channel's list is its own: room there
    assert await bench.configurator.store_scene(2, ACTUATOR_OUT1, ON)
    original = scenes.respond

    def silent_list(n: NetworkPDU) -> None:
        msgs = bench.link.sent_access()
        if msgs and msgs[-1][4] == V.scene_action_get():
            scenes._answered = len(msgs)  # swallowed
            return
        original(n)

    bench.link.responders[-1] = silent_list
    with pytest.raises(HomeAssistantError) as silent:
        await bench.configurator.store_scene(1, ACTUATOR_OUT1, ON)
    assert silent.value.translation_key == "service_no_reply"

    def short_list(n: NetworkPDU) -> None:
        msgs = bench.link.sent_access()
        if msgs and msgs[-1][4] == V.scene_action_get():
            bench.link.send_access(
                ACTUATOR_OUT1,
                OUR_SRC,
                encode_opcode(V.SCENE_ACTION_SETUP_STATUS, M.JUNG_CID) + b"\x00",
            )
            scenes._answered = len(msgs)  # answered here, not by the server
            return
        original(n)

    bench.link.responders[-1] = short_list
    assert await bench.configurator.store_scene(1, ACTUATOR_OUT1, ON)
    assert scenes.registers[ACTUATOR_OUT1] == [2, 1]


async def test_an_unreadable_register_does_not_block_the_store(
    bench: Bench, scenes: SceneServer
) -> None:
    """As in the app, a register that does not tell its size is no reason to refuse: the Store says if it took."""
    original = scenes.respond

    def short_register(n: NetworkPDU) -> None:
        msgs = bench.link.sent_access()
        if msgs and msgs[-1][4] == M.scene_register_get() and not scenes.seen:
            bench.link.send_access(
                DALI_LOAD, OUR_SRC, encode_opcode(M.SCENE_REGISTER_STATUS) + b"\x00"
            )
            scenes._answered = len(msgs)  # answered here, not by the server
            return
        original(n)

    bench.link.responders[-1] = short_register
    assert await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert scenes.registers[DALI_LOAD] == [2]


async def test_leaving_a_scene_clears_the_devices_keys_that_recall_it_and_its_scene_info(
    bench: Bench, scenes: SceneServer
) -> None:
    """The app's *remove device from scene*: the device's keys wired to recall the scene lose their connections
    first (`RemoveConnectionForAddress.SceneConnection`), and the device's `sceneInfo` row goes last."""
    assert await bench.configurator.assign_key(SWITCH_KEY, scene=1)
    pf = bench.reload()
    assert pub(pf, SWITCH_KEY, "1205") == 0xFFFF
    assert [r["scene"] for r in pf.meta["sceneInfo"]] == [1]  # 0148's row
    bench.config.seen.clear()
    assert await bench.configurator.remove_from_scene(1, SWITCH_LOAD)
    assert bench.config.seen[0] == (SWITCH_NODE, pub_set(SWITCH_KEY, 0x0000, "1205"))
    pf = bench.reload()
    assert pub(pf, SWITCH_KEY, "1205") is None
    assert scene_key_rows(pf) == []
    assert pf.meta["sceneInfo"] == []
    assert pf.cdb.scenes[1] == []


async def test_a_stale_scene_row_does_not_clear_a_key_wired_elsewhere(
    bench: Bench, scenes: SceneServer
) -> None:
    """The fixture's 0149 has a scene-1 row but drives the gateway: the row is a stale cache, the key is kept."""
    assert scene_key_rows(bench.reload()) == [SWITCH_KEY]
    bench.config.seen.clear()
    assert await bench.configurator.delete_scene(1)
    assert bench.config.seen == []
    assert pub(bench.reload(), SWITCH_KEY, "1001") == GATEWAY_GROUP


async def test_delete_scene_anyway_skips_what_does_not_answer(
    bench: Bench, scenes: SceneServer, caplog: pytest.LogCaptureFixture
) -> None:
    """The app's *Delete anyway*: a member that cannot be reached keeps the scene, the export forgets it all the
    same; without `force` the same silence stops the deletion."""
    assert await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert await bench.configurator.store_scene(2, SOCKET_NODE, None)
    original = scenes.respond

    def dali_gone(n: NetworkPDU) -> None:
        msgs = bench.link.sent_access()
        if msgs and msgs[-1][1] == DALI_LOAD:
            scenes._answered = len(msgs)  # swallowed
            return
        original(n)

    bench.link.responders[-1] = dali_gone
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.delete_scene(2)
    assert err.value.translation_key == "service_no_reply"
    assert 2 in bench.reload().cdb.scenes
    assert await bench.configurator.delete_scene(2, force=True)
    pf = bench.reload()
    assert 2 not in pf.cdb.scenes
    assert scenes.registers[SOCKET_NODE] == []  # the one that answered forgot it
    assert scenes.registers[DALI_LOAD] == [2]  # the silent one still holds it
    assert "still stored on 0232" in caplog.text


async def test_delete_scene_anyway_when_a_key_cannot_be_cleared(
    bench: Bench, scenes: SceneServer, caplog: pytest.LogCaptureFixture
) -> None:
    """A key of a member that does not take its clear stops a plain deletion; forced, the scene goes all the same
    and the export records what the key accepted, not what was planned for it."""
    assert await bench.configurator.assign_key(SWITCH_KEY, scene=1)
    bench.config.refuse[pub_set(SWITCH_KEY, 0x0000, "1205")] = 0x02  # Invalid Model
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.delete_scene(1)
    assert err.value.translation_key == "service_config_refused"
    assert 1 in bench.reload().cdb.scenes
    assert await bench.configurator.delete_scene(1, force=True)
    pf = bench.reload()
    assert 1 not in pf.cdb.scenes
    assert (
        pub(pf, SWITCH_KEY, "1205") == 0xFFFF
    )  # refused: still recalls (a scene that is gone)
    assert "the keys recalling it were not all cleared" in caplog.text


async def test_delete_unused_scenes_deletes_what_the_export_does_not_know(
    bench: Bench, scenes: SceneServer
) -> None:
    """The app's `DeleteUnusedScenes`: every register is read; numbers that are no scene of the export (nor a
    timer's, which are in it too) are deleted; a node that does not answer is reported and left alone."""
    scenes.registers[SWITCH_LOAD] = [1, 7]
    scenes.registers[SOCKET_NODE] = [9]
    silent = {DIMMER_LOAD}
    original = scenes.respond

    def some_silent(n: NetworkPDU) -> None:
        msgs = bench.link.sent_access()
        if msgs and msgs[-1][1] in silent:
            scenes._answered = len(msgs)  # swallowed
            return
        if (
            msgs
            and msgs[-1][1] == ACTUATOR_OUT1
            and msgs[-1][4] == M.scene_register_get()
        ):
            bench.link.send_access(
                ACTUATOR_OUT1, OUR_SRC, encode_opcode(M.SCENE_REGISTER_STATUS) + b"\x00"
            )
            scenes._answered = len(msgs)  # answered here, not by the server
            return
        original(n)

    bench.link.responders[-1] = some_silent
    result = await bench.configurator.delete_unused_scenes()
    assert result == {"0148": [7], "0172": [9], "unanswered": ["0300", "0400"]}
    assert scenes.registers[SWITCH_LOAD] == [1]
    assert scenes.registers[SOCKET_NODE] == []
    assert bench.file_unchanged()


async def test_delete_unused_scenes_stops_at_a_refused_delete(
    bench: Bench, scenes: SceneServer
) -> None:
    """The error names what was deleted before the refused Delete, not "nothing was applied"."""
    scenes.registers[SWITCH_LOAD] = [1, 5, 7]
    scenes.answer_sets = False  # read back: the register the node keeps
    original = scenes.respond

    def keeps_seven(n: NetworkPDU) -> None:
        original(n)
        if 7 not in scenes.registers[SWITCH_LOAD]:
            scenes.registers[SWITCH_LOAD].append(7)

    bench.link.responders[-1] = keeps_seven
    with pytest.raises(HomeAssistantError) as err:
        await bench.configurator.delete_unused_scenes()
    assert err.value.translation_key == "service_scene_not_deleted"
    assert err.value.translation_placeholders["scene"] == "7"
    assert scenes.registers[SWITCH_LOAD] == [1, 7]
    assert err.value.translation_placeholders["applied"] == applied_unused_deleted(
        {"0148": [5]}
    )
    assert "(0148: 5)" in applied_unused_deleted({"0148": [5]})
    assert applied_unused_deleted({}) == APPLIED_NOTHING
    assert bench.file_unchanged()


async def test_keys_cleared_before_a_member_that_stops_are_recorded(
    bench: Bench, scenes: SceneServer
) -> None:
    """The keys go first: when the member itself then does not answer, the export records the cleared key and the
    error says so rather than "nothing was applied"."""
    original = scenes.respond

    def load_gone(n: NetworkPDU) -> None:
        msgs = bench.link.sent_access()
        if msgs and msgs[-1][1] == SWITCH_LOAD:
            scenes._answered = len(msgs)  # swallowed
            return
        original(n)

    operations: list[Callable[[], Coroutine[Any, Any, bool]]] = [
        lambda: bench.configurator.remove_from_scene(1, SWITCH_LOAD),
        lambda: bench.configurator.delete_scene(1),
    ]
    for operation in operations:
        assert await bench.configurator.assign_key(SWITCH_KEY, scene=1)
        bench.link.responders[-1] = load_gone
        with pytest.raises(HomeAssistantError) as err:
            await operation()
        assert err.value.translation_key == "service_no_reply"
        assert (
            "keys of these devices that recalled scene 1 were cleared"
            in (err.value.translation_placeholders["applied"])
        )
        pf = bench.reload()
        assert pub(pf, SWITCH_KEY, "1205") is None
        assert pf.cdb.scenes[1] == [SWITCH_LOAD]
        bench.link.responders[-1] = original


# ----------------------------------------------------------------------------- cancelled plans (D12)


def cancel_at_request(bench: Bench, accepted: int) -> list[asyncio.Task[Any]]:
    """Cancel the task put into the returned list when Config request number `accepted` + 1 goes out: the
    nodes took the first `accepted` requests and never see that one (it is swallowed)."""
    tasks: list[asyncio.Task[Any]] = []
    seen = 0

    def hook(_node: int, _access: bytes) -> bool:
        nonlocal seen
        seen += 1
        if seen <= accepted:
            return False
        bench.config.on_request = None
        tasks[0].cancel()
        return True

    bench.config.on_request = hook
    return tasks


async def cancelled(
    bench: Bench, accepted: int, operation: Coroutine[Any, Any, Any]
) -> None:
    """Run `operation`, cancel it after `accepted` Config requests were taken, and see the cancellation through."""
    tasks = cancel_at_request(bench, accepted)
    task = asyncio.ensure_future(operation)
    tasks.append(task)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_to_end_holds_the_work_through_every_cancellation() -> None:
    """The caller is cancelled twice while the work runs: the work ends all the same, then the cancellation goes
    on; with the work failing meanwhile, the cancellation is chained to its error."""
    gate = asyncio.Event()
    done: list[str] = []

    async def work(fail: bool) -> str:
        await gate.wait()
        done.append("work")
        if fail:
            raise HomeAssistantError("write failed")
        return "written"

    gate.set()
    assert await mc.run_to_end(work(fail=False)) == "written"
    for fail in (False, True):
        gate.clear()
        task = asyncio.ensure_future(mc.run_to_end(work(fail)))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()  # still waiting for the work
        gate.set()
        with pytest.raises(asyncio.CancelledError) as exc:
            await task
        assert isinstance(exc.value.__cause__, HomeAssistantError) is fail
    assert done == ["work", "work", "work"]
    with pytest.raises(HomeAssistantError):  # no cancellation: the work's own error
        await mc.run_to_end(work(fail=True))


async def test_run_to_end_stops_when_the_work_itself_is_cancelled() -> None:
    """The loop shutting down cancels the work too: nothing is left to wait for."""
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        await asyncio.Event().wait()

    inner = asyncio.ensure_future(work())
    task = asyncio.ensure_future(mc.run_to_end(inner))  # type: ignore[arg-type]
    await started.wait()
    inner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_cancelled_key_assignment_records_the_steps_the_node_took(
    bench: Bench,
) -> None:
    """D12, the reviewers' repro: an `assign_key` cancelled after two accepted steps (an automation in
    `mode: restart`) records those two and nothing else, and the cancellation is re-raised."""
    await cancelled(
        bench, 2, bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    )
    assert bench.configurator.recorded
    assert (
        bench.config_pdus() == WIRE_ROCKER_A_TO_DIMMER[:3]
    )  # the third never answered
    pf = bench.reload()
    assert pub(pf, ROCKER_A, "1001") == DIMMER_GROUP
    assert subs(pf, ROCKER_A, "1001") == [DALI_GROUP, DIMMER_GROUP]
    assert pub(pf, ROCKER_A, "1003") is None  # in flight when cancelled: not recorded
    assert bench.keys.modes == {}
    # running it again completes it
    bench.link.net_pdus.clear()
    assert await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    pf = bench.reload()
    for model in ("1001", "1003", "05271015"):
        assert pub(pf, ROCKER_A, model) == DIMMER_GROUP
        assert subs(pf, ROCKER_A, model) == [DIMMER_GROUP]


async def test_a_plan_cancelled_before_its_first_answer_writes_nothing(
    bench: Bench,
) -> None:
    await cancelled(
        bench, 0, bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    )
    assert bench.file_unchanged()
    assert not bench.configurator.recorded


async def test_a_cancelled_room_move_records_the_room_it_created(
    bench: Bench,
) -> None:
    """`set_room` into a new room, cancelled after the first of the socket's steps: the room and that
    subscription are recorded, as for a refusal (CFG-02)."""
    group = mc.load_project(str(bench.path), None).free_group_address()
    await cancelled(bench, 1, bench.configurator.set_room(SOCKET_NODE, "Garage"))
    pf = bench.reload()
    assert pf.user_groups().get(group) == "Garage"
    assert subs(pf, SOCKET_NODE, "1000") == [SOCKET_GROUP, SOCKETS, KITCHEN, group]


async def test_a_cancellation_during_the_key_mode_write_records_the_whole_wiring(
    bench: Bench,
) -> None:
    """Every Config step was accepted, the vendor writes were under way: the wiring is recorded."""

    async def stop(*_args: Any) -> None:
        raise asyncio.CancelledError

    with (
        patch.object(MeshConfigurator, "_write_key_mode", stop),
        pytest.raises(asyncio.CancelledError),
    ):
        await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    pf = bench.reload()
    for model in ("1001", "1003", "05271015"):
        assert pub(pf, ROCKER_A, model) == DIMMER_GROUP


async def test_a_cancelled_scene_store_records_the_member_whose_store_took(
    bench: Bench, scenes: SceneServer
) -> None:
    """The Scene Store took, the description write was cancelled: the member is recorded."""

    async def stop(*_args: Any) -> None:
        raise asyncio.CancelledError

    with (
        patch.object(MeshConfigurator, "_scene_action", stop),
        pytest.raises(asyncio.CancelledError),
    ):
        await bench.configurator.store_scene(2, DALI_LOAD, ON)
    assert scenes.registers[DALI_LOAD] == [2]
    assert bench.reload().cdb.scenes[2] == [DALI_LOAD]


async def test_cancelled_scene_removals_record_the_loads_done_before(
    bench: Bench, scenes: SceneServer
) -> None:
    """`remove_from_scenes` and `delete_scene` (with *Delete anyway* too: a cancellation is no member to skip)
    cancelled at their second load: the first one's leaving is recorded."""
    calls = 0
    original = MeshConfigurator._delete_from_register

    async def second_cancelled(self: MeshConfigurator, *args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        await original(self, *args)

    operations: list[Callable[[], Coroutine[Any, Any, bool]]] = [
        lambda: bench.configurator.remove_from_scenes(2, bench.reload().cdb.scenes[2]),
        lambda: bench.configurator.delete_scene(2),
        lambda: bench.configurator.delete_scene(2, force=True),
    ]
    for operation in operations:
        await bench.configurator.store_scenes(2, [(DALI_LOAD, ON), (SOCKET_NODE, None)])
        members = bench.reload().cdb.scenes[2]
        assert len(members) == 2
        calls = 0
        with (
            patch.object(MeshConfigurator, "_delete_from_register", second_cancelled),
            pytest.raises(asyncio.CancelledError),
        ):
            await operation()
        assert bench.reload().cdb.scenes[2] == members[1:]


async def test_a_cancelled_removal_records_the_reset_node_as_excluded(
    bench: Bench,
) -> None:
    """`remove_node` cancelled after the Node Reset was confirmed, before any other node took its unwiring: the
    reset cannot be taken back, so the node is recorded as excluded."""
    await cancelled(bench, 1, bench.configurator.remove_node(DIMMER_NODE))
    assert bench.config_pdus()[0] == (DIMMER_NODE, C.node_reset())
    pf = bench.reload()
    assert pf.cdb.node_by_addr(DIMMER_NODE) is None
    assert {DIMMER_LOAD, DIMMER_KEY} <= pf.cdb.excluded_addresses


async def test_a_cancelled_plan_whose_record_fails_is_logged_and_still_cancelled(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    async def unwritable(*_args: Any) -> None:
        raise mc._failure("service_export_write_failed", path="x", error="disk full")

    with patch.object(MeshConfigurator, "_write", unwritable):
        await cancelled(
            bench, 2, bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
        )
    assert (
        "2 of 8 Config messages were applied on the mesh but could not be recorded"
        in caplog.text
    )
    assert bench.file_unchanged()


async def test_a_cancelled_save_is_written_but_left_to_sync_gateway(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """The write runs to its end; the upload that would follow is not held up for: `sync_gateway` does it."""
    writing = asyncio.Event()
    written = asyncio.Event()
    original = MeshConfigurator._write

    async def slow_write(self: MeshConfigurator, pf: ProjectFile) -> None:
        writing.set()
        await written.wait()
        await original(self, pf)

    with patch.object(MeshConfigurator, "_write", slow_write):
        task = asyncio.ensure_future(bench.configurator.create_room("Attic"))
        await writing.wait()
        task.cancel()
        await asyncio.sleep(0)
        written.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "Attic" in bench.reload().user_groups().values()
    assert with_gateway.uploads == []
    assert "not handed to the gateway (cancelled)" in caplog.text


async def test_no_upload_while_home_assistant_stops(
    bench: Bench, with_gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    bench.hub.hass.is_stopping = True
    assert await bench.configurator.create_room("Attic")
    assert "Attic" in bench.reload().user_groups().values()
    assert with_gateway.uploads == []
    assert "Home Assistant is stopping" in caplog.text


# ----------------------------------------------------------------------------- plan journal (W I1)


async def crashed(
    bench: Bench, accepted: int, operation: Coroutine[Any, Any, Any]
) -> dict[str, Any]:
    """Run `operation` until `accepted` Config requests were taken, then "crash": the task stops there and nothing
    records it (as when Home Assistant is killed); returns the journal left behind."""

    async def no_record(*_args: Any) -> None:
        return None

    with patch.object(MeshConfigurator, "_record_stopped", no_record):
        await cancelled(bench, accepted, operation)
    assert bench.journal.data is not None
    return dict(bench.journal.data)


def restart(bench: Bench) -> MeshConfigurator:
    """The configurator of the next start: the same entry, the journal on disk, nothing else carried over."""
    return MeshConfigurator(bench.hub)  # type: ignore[arg-type]


def recorded_state(pf: ProjectFile) -> tuple[str, Any, Any]:
    """What a record writes, without the timestamp of the write."""
    return json.dumps(pf.net["nodes"]), pf.net.get("groups"), pf.meta


async def test_the_journal_follows_the_plan_and_goes_with_its_record(
    bench: Bench,
) -> None:
    """Written before the first message and after every accepted one, removed once the export records the plan."""
    assert await bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    saves = bench.journal.saves
    assert [s.get("accepted") for s in saves] == [
        *range(9),
        None,
    ]  # emptied, then removed
    assert saves[0]["action"] == "junghome_ble.assign_key"
    assert len(saves[0]["steps"]) == 8
    assert saves[0]["steps"][0] == {
        "node": DALI_NODE,
        "pdu": pub_set(ROCKER_A, DIMMER_GROUP, "1001").hex(),
        "expect": C.CONFIG_MODEL_PUBLICATION_STATUS,
        "change": {
            "element": ROCKER_A,
            "model": "1001",
            "address": DIMMER_GROUP,
            "kind": "publish",
        },
        "bind": None,
        "unlinks": None,
    }
    assert saves[0]["steps"][-1]["unlinks"] == ROCKER_A  # the old link's clear
    assert bench.journal.data is None
    assert not bench.configurator.journaled
    # a plan with nothing to send writes no journal
    bench.journal.saves.clear()
    assert await bench.configurator.set_room(SWITCH_LOAD, "WC") is False
    assert bench.journal.saves == []


async def test_a_refused_first_message_leaves_no_journal(bench: Bench) -> None:
    bench.config.refuse[sub_add(SWITCH_LOAD, ROCKER_A_GROUP, "1000")] = 0x08
    with pytest.raises(HomeAssistantError):
        await bench.configurator.assign_key(ROCKER_A, room="WC")
    assert bench.file_unchanged()
    assert bench.journal.data is None


async def test_a_crash_mid_plan_is_recorded_at_the_next_start(
    bench: Bench, issues: dict[str, dict[str, Any]]
) -> None:
    """Killed after two accepted steps: the export still says the old wiring, the journal says two. The next
    start records them, raises the repair naming the action and removes the journal; a crash during that replay
    replays the same again (idempotent)."""
    journal = await crashed(
        bench, 2, bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    )
    assert bench.file_unchanged()
    assert journal["accepted"] == 2
    configurator = restart(bench)
    assert await configurator.async_replay_journal() is True
    pf = bench.reload()
    assert pub(pf, ROCKER_A, "1001") == DIMMER_GROUP
    assert subs(pf, ROCKER_A, "1001") == [DALI_GROUP, DIMMER_GROUP]
    assert pub(pf, ROCKER_A, "1003") is None
    assert bench.journal.data is None
    assert issues == {
        "plan_interrupted_entry": {
            "title": "JUNG HOME mesh test",
            "action": "junghome_ble.assign_key",
            "accepted": "2",
            "total": "8",
        }
    }
    # replayed again (a crash before the journal was removed): the same record
    first = recorded_state(pf)
    bench.journal.data = journal
    assert await restart(bench).async_replay_journal() is True
    assert recorded_state(bench.reload()) == first
    # nothing in the journal: nothing to do
    assert await restart(bench).async_replay_journal() is False


async def test_a_replayed_journal_applies_the_plans_bookkeeping_once(
    bench: Bench, issues: dict[str, dict[str, Any]]
) -> None:
    """The room a plan creates, a room link's row and a reset node's exclusion are in the journal too; replayed
    twice they are recorded once."""
    group = mc.load_project(str(bench.path), None).free_group_address()
    operations: list[tuple[Callable[[], Coroutine[Any, Any, Any]], int]] = [
        (lambda: bench.configurator.set_room(SOCKET_NODE, "Garage"), 1),
        (lambda: bench.configurator.assign_key(ROCKER_A, room="WC"), 1),
        (lambda: bench.configurator.remove_node(DIMMER_NODE), 1),
    ]
    for operation, accepted in operations:
        journal = await crashed(bench, accepted, operation())
        assert await restart(bench).async_replay_journal()
        first = recorded_state(bench.reload())
        bench.journal.data = journal
        assert await restart(bench).async_replay_journal()
        assert recorded_state(bench.reload()) == first
    pf = bench.reload()
    assert pf.user_groups().get(group) == "Garage"
    assert [r["elementAddress"] for r in link_rows(pf)].count(ROCKER_A) == 1
    assert pf.cdb.node_by_addr(DIMMER_NODE) is None
    exclusions = [a for row in pf.net["networkExclusions"] for a in row["addresses"]]
    assert exclusions.count(f"{DIMMER_LOAD:04X}") == 1


async def test_a_journal_of_nothing_accepted_is_dropped_with_a_notice(
    bench: Bench, issues: dict[str, dict[str, Any]]
) -> None:
    await crashed(
        bench, 0, bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    )
    assert await restart(bench).async_replay_journal() is False
    assert bench.file_unchanged()
    assert bench.journal.data is None
    assert issues["plan_interrupted_entry"]["accepted"] == "0"


async def test_a_journal_that_cannot_be_replayed(
    bench: Bench, issues: dict[str, dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    """Unreadable: dropped. A record that cannot be written: kept for the next start. One that does not apply
    to the export: dropped with the error logged. None of them raises the repair or stops the setup."""
    bench.journal.data = {"action": "x", "steps": [{"node": 1}], "accepted": 1}
    assert await restart(bench).async_replay_journal() is False
    assert bench.journal.data is None
    assert "Dropping an unreadable plan journal" in caplog.text

    journal = await crashed(
        bench, 2, bench.configurator.assign_key(ROCKER_A, element=DIMMER_LOAD)
    )

    async def unwritable(*_args: Any) -> None:
        raise mc._failure("service_export_write_failed", path="x", error="disk full")

    with patch.object(MeshConfigurator, "_write", unwritable):
        assert await restart(bench).async_replay_journal() is False
    assert bench.journal.data == journal
    assert "tried again at the next start" in caplog.text

    journal["prepare"] = {
        "kind": "room_link",
        "key": 0x7F00,  # no such element
        "room": WC,
        "publish": ROCKER_A_GROUP,
        "function": "LIGHT",
    }
    bench.journal.data = journal
    assert await restart(bench).async_replay_journal() is False
    assert bench.journal.data is None
    assert "Dropping a plan journal that does not apply" in caplog.text
    assert bench.file_unchanged()
    assert issues == {}


async def test_a_publication_reset_alone_closes_the_journal(bench: Bench) -> None:
    """`unwire_threshold`'s plan that changes nothing in the file still ends the journal."""
    await bench.configurator.set_threshold_devices(SOCKET_NODE, [DALI_LOAD])
    assert bench.journal.data is None
    await bench.configurator.unwire_threshold(SOCKET_NODE)
    assert bench.journal.data is None
    bench.journal.saves.clear()
    assert await bench.configurator.unwire_threshold(SOCKET_NODE) is False
    assert bench.journal.saves  # the publication reset went out under a journal
    assert bench.journal.data is None


def test_a_journalled_step_reads_back_as_it_was() -> None:
    steps = [
        mc.ConfigStep(
            DALI_NODE,
            sub_del(ROCKER_A, DALI_GROUP, "1001"),
            C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
            change=mc.ModelChange(ROCKER_A, "1001", DALI_GROUP, "unsubscribe"),
            unlinks=ROCKER_A,
        ),
        mc.ConfigStep(
            SOCKET_NODE,
            C.model_app_bind(SOCKET_SENSOR, "05271013", 0),
            C.CONFIG_MODEL_APP_STATUS,
            bind=(SOCKET_SENSOR, "05271013"),
        ),
    ]
    rows = json.loads(json.dumps([mc._step_json(s) for s in steps]))
    assert [mc._step_from_json(row) for row in rows] == steps
