"""Hub: keeps a GATT-proxy connection to the JUNG mesh through Home Assistant's Bluetooth stack.

HA's Bluetooth stack hands us bleak-compatible clients for local adapters *and* ESPHome Bluetooth
proxies, so the same code works either way. All mesh logic lives in the `jhmesh` package; this module
only drives the link, caches element state and translates mesh messages into entity updates.

Incoming access messages are dispatched through `STATUS_HANDLERS`, a table keyed by opcode (plus the company id
for vendor opcodes) that `register_status_handler` fills: one small handler per message type, each updating the
element state cache (`JungHomeHub.element_state` + `notify_update`) or firing button events. A new message type
is one more registered handler, in this module or in another one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    AUDIT_RETRIES,
    AUDIT_TIMEOUT,
    COMMAND_ECHO_TIMEOUT,
    CONNECT_BACKOFF_MAX,
    CONNECT_BACKOFF_MIN,
    CONNECT_BEACON_WAIT,
    DEFAULT_HEARTBEATS,
    DOMAIN,
    EXPORT_STALE_THRESHOLD,
    FAILED_PROXY_COOLDOWN,
    FILTER_STATUS_TIMEOUT,
    HEARTBEAT_CHECK_INTERVAL,
    HUB_DATA_KEYS,
    IDENTIFY_SECONDS,
    ISSUE_ADDRESS_SHARED,
    ISSUE_ADDRESS_SHARED_AGAIN,
    ISSUE_BLUETOOTH_UNAVAILABLE,
    ISSUE_DUPLICATE_MESH,
    ISSUE_EXPORT_STALE,
    ISSUE_INSERT_MISMATCH,
    ISSUE_IV_INDEX_AHEAD,
    ISSUE_IV_INDEX_MISMATCH,
    ISSUE_KEY_REFRESH,
    ISSUE_NODE_CLOCK_WRONG,
    ISSUE_PDUS_DROPPED,
    ISSUE_SEQ_STORE_LOST,
    ISSUE_SEQ_STORE_UNWRITABLE,
    ISSUE_SEQUENCE_SPACE_LOW,
    ISSUE_TIME_KEEPER_MISSING,
    ISSUE_UNKNOWN_NODES,
    ISSUE_VAULT_KEY_REFRESH,
    KEEP_ALIVE_ATTEMPTS,
    KEEP_ALIVE_TIMEOUT,
    LINK_BLUETOOTH_OFF,
    LINK_CONNECTING,
    LINK_DISCONNECTED,
    LINK_FAILED,
    LINK_IDLE_TIMEOUT,
    LINK_LOSS_GRACE,
    LINK_SEARCHING,
    LINK_UPDATING,
    NODE_DIAGNOSTICS_INTERVAL,
    NODE_INFO,
    NODE_INFO_TIME_ROLE,
    OPTION_HEARTBEATS,
    PROXY_ADVERT_MAX_AGE,
    REFRESH_CHUNK,
    REQUEST_ATTEMPTS,
    RESTART_BLOCK,
    RESTART_SLACK,
    SCENE_RECALL_WINDOW,
    SEQ_SKIP_AHEAD,
    SEQUENCE_CHECK_INTERVAL,
    SEQUENCE_SPACE_WARN,
    SHORT_LINK,
    SHORT_LINK_STREAK,
    SIG_SOFTWARE_VERSION,
    SIGNAL_CONNECTION,
    SIGNAL_LINK_STATE,
    SIGNAL_NODE,
    SIGNAL_SCENES,
    SIGNAL_UPDATE,
    STOP_TIMEOUT,
    TIME_SET_INTERVAL,
    issue_id,
    learn_more_url,
)
from .entity import update_node_device
from .hub.clock import Clock, next_utc_offset_change
from .hub.energy import (
    COUNTER_FIELDS,
    PROPERTY_PRECISE_TOTAL_ENERGY,
    SENSOR_FIELDS,
    CounterNotReset,
    Energy,
    lacks_precise_energy,
)
from .hub.export_watch import (
    GATEWAY_CERTIFICATE_CHANGED,
    GATEWAY_UNVERIFIED,
    ExportWatch,
    is_gateway_host,
)
from .hub.liveness import Liveness
from .hub.refresh import ONOFF_GET, STATE_GETS, Refresh
from .hub_gestures import ButtonGestures, EventListener
from .identity import async_vault_keeper
from .inserts import NodeInserts
from .jhmesh import config_messages as C
from .jhmesh import messages as M
from .jhmesh import vendor_models as V
from .jhmesh.advert import JungAdvertisement, mac_from_uuid, parse_manufacturer_data
from .jhmesh.audit import NodeAudit, audit_node, client_exchange
from .jhmesh.cdb import CDB, Node
from .jhmesh.client import (
    MESH_PROXY_SERVICE,
    NET_KEY_INDEX,
    SEQ_GUARD_FIRST_BEACON,
    SEQ_TX_LIMIT,
    AccessMessage,
    Heartbeat,
    ProxyClient,
    SequenceStalled,
)
from .jhmesh.crypto import NetKeyMaterial
from .jhmesh.devices import (
    BATTERY_PIDS,
    PP2_PIDS,
    Devices,
    Light,
    Metadata,
    MeteredLoad,
    Socket,
    build_devices,
    time_keeper_candidates,
)
from .jhmesh.keyrefresh import KeyRefreshRecord
from .jhmesh.pdu import ALL_NODES, SecureNetworkBeacon, is_unicast
from .jhmesh.properties import PROPERTIES, SIG_PROPERTIES, EnforcedOutput, Scaled
from .jhmesh.vault import recognise
from .keep_awake import KeepAwake
from .node_clocks import NodeClocks
from .node_info import (
    NODE_VERSION_STORES,
    NODE_VERSIONS,
    NODE_VERSIONS_SAVE_DELAY,
    NODE_VERSIONS_STORAGE_VERSION,
    NodeInfoStore,
    async_load_node_versions,
    async_remove_node_versions,
    node_versions_store,
)
from .seq_store import (
    SEQ_BACKUP_STORES,
    SEQ_BACKUP_TOKEN,
    SEQ_FLOOR_EVERY,
    SEQ_FLOOR_STORES,
    SEQ_OWNERS,
    SEQ_RESTART_MARGIN,
    SEQ_SAVE_EVERY,
    SEQ_STALL_RETRY,
    SEQ_STORAGE_MINOR_VERSION,
    SEQ_STORES,
    STORAGE_VERSION,
    AddressShared,
    HAState,
    SeqStore,
    _addresses_of,
    _async_skip_restored_record,
    _has_history,
    _landed,
    _mesh_of,
    _newest_corrupt_seq_store,
    _refuse_duplicate_mesh,
    _report_seq_store_lost,
    _store_with,
    _stored_key_refresh,
    _usable_record,
    async_apply_followed_key_refresh,
    async_migrate_legacy_seq_store,
    async_rewind_seq_floor,
    async_skip_seq_store_ahead,
    merge_legacy_seq_store,
    seq_backup_store,
    seq_floor_store_for_uuid,
    seq_store,
    seq_store_for_uuid,
)
from .vault_refresh import VaultKeyRefresh

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .app_follow import AppFollower
    from .entity import TrackedPlatform
    from .gateway_status import GatewayPolls
    from .identity import VaultKeeper
    from .mesh_config import MeshConfigurator

_LOGGER = logging.getLogger(__name__)

# Moved out of this module (review-4 A4-1: the persistence into `seq_store.py` and `node_info.py`, `issue_id` into
# `const.py`; A4-3: the hub's components into `hub/`); re-exported for the modules and tests that import them from here.
__all__ = [
    "GATEWAY_CERTIFICATE_CHANGED",
    "GATEWAY_UNVERIFIED",
    "NODE_VERSIONS",
    "NODE_VERSIONS_SAVE_DELAY",
    "NODE_VERSIONS_STORAGE_VERSION",
    "NODE_VERSION_STORES",
    "SEQ_BACKUP_STORES",
    "SEQ_BACKUP_TOKEN",
    "SEQ_FLOOR_EVERY",
    "SEQ_FLOOR_STORES",
    "SEQ_GUARD_FIRST_BEACON",
    "SEQ_OWNERS",
    "SEQ_RESTART_MARGIN",
    "SEQ_SAVE_EVERY",
    "SEQ_STALL_RETRY",
    "SEQ_STORAGE_MINOR_VERSION",
    "SEQ_STORES",
    "SEQ_TX_LIMIT",
    "STORAGE_VERSION",
    "AddressShared",
    "CounterNotReset",
    "HAState",
    "NodeInfoStore",
    "SeqStore",
    "async_apply_followed_key_refresh",
    "async_migrate_legacy_seq_store",
    "async_remove_node_versions",
    "async_skip_seq_store_ahead",
    "is_gateway_host",
    "issue_id",
    "lacks_precise_energy",
    "merge_legacy_seq_store",
    "next_utc_offset_change",
    "seq_backup_store",
    "seq_floor_store_for_uuid",
    "seq_store",
    "seq_store_for_uuid",
]

# the time roles a node keeping the PP2 pucks' time answers (Time Role Status): authority, relay
TIME_KEEPER_ROLES = frozenset({1, 2})

# `async_wait_settled`: state Gets while a load ramps to a new state, the pause between them (JUNG dimmers fade
# with their own ramps, a few seconds at most), and how long it asks at most, however many Gets go unanswered
SETTLE_ATTEMPTS = 20
SETTLE_PAUSE = 0.5
SETTLE_TIMEOUT = 15.0
# ... and how far a reported value may lie from the one asked for (`_took_effect`): a load keeps to its own steps,
# the app's percent of the 16-bit range for a lightness or level, and a colour temperature to a light's own
SETTLE_SLACK = {"lightness": 0x0290, "level": 0x0290, "kelvin": 100}
# A Set with a transition time: the load's state is read again this long after the remaining time its Status gave,
# as its last publication may come before the fade ends (`_reread_after_transition`)
TRANSITION_REREAD_SLACK = 1.0

# how long one send waits for the store to catch up (`JungHomeHub.while_seq_stalls`) before it is given up on: a
# healthy store lands its write within a second, one that refuses for this long will not do so by waiting
SEQ_STALL_DEADLINE = 120.0
# how long the store may refuse before `seq_store_unwritable` is raised (`HAState.report_unwritable`)
SEQ_STALL_ISSUE_AFTER = 60.0
SIG_PROPERTY_STATUS_OPCODES = (
    M.GEN_USER_PROP_STATUS,
    M.GEN_ADMIN_PROP_STATUS,
    M.GEN_MANU_PROP_STATUS,
)
# The vendor message gateway-mode keys publish their gestures with (docs/cross-repo-analysis.md §1.2).
VENDOR_USER_PROPERTY_SET_UNACK = 0x10  # LBC User Property Set Unacknowledged
PROPERTY_BUTTON_EVENT = 0x5012  # KEY_EVT: [counter][code]
PROPERTY_LOCK = 0x0009  # EnforceOutput: a load's lock (`ElementState.note_lock`)
GENERIC_LEVEL_OPCODES = frozenset(
    {M.GEN_LEVEL_SET, M.GEN_LEVEL_SET_UNACK, 0x8209, 0x820A, 0x820B, 0x820C}
)
SCENE_ACTION_SETUP = (
    "05271017"  # the JUNG vendor model that says what an element does in a scene
)
SCENE_SETUP_SERVER = (
    "1204"  # SIG Scene Setup Server: the element holding a node's scene register
)
# SIG model id of a Generic OnOff Server, as the export lists it: every element that hosts one answers an OnOff Get
# (loads, sockets, actuator channels, detectors, thermostats), which makes it a keep-alive target for the watchdog.
GENERIC_ONOFF_SERVER = "1000"
# the range of a colour temperature (Mesh Model §6.1.3.1)
CTL_KELVIN_MIN, CTL_KELVIN_MAX = 800, 20000

# ------------------------------------------------------------------ status handler registry
type StatusHandler = Callable[[JungHomeHub, AccessMessage, bytes], None]
# (company id, opcode); company id None = a SIG opcode
type MessageKey = tuple[int | None, int]

STATUS_HANDLERS: dict[MessageKey, StatusHandler] = {}


def register_status_handler[H: StatusHandler](
    *opcodes: int, company_id: int | None = None
) -> Callable[[H], H]:
    """Route the access messages with one of `opcodes` (a vendor opcode with its `company_id`) to the decorated handler.

    A message type has exactly one handler (a later registration replaces the earlier one). The handler gets the
    hub, the decoded message and its parameters (`m.params`) and is responsible for everything that follows:
    parsing, `hub.element_state(addr)` to read or create the cached state, `hub.notify_update(addr)` once it
    changed, or `hub.fire_button(...)` for gestures. Messages without a handler are ignored.
    """

    def register(handler: H) -> H:
        for opcode in opcodes:
            STATUS_HANDLERS[(company_id, opcode)] = handler
        return handler

    return register


# One lock per entry, kept across reloads, around everything that works on a hub and may replace it: the service
# calls (`actions.common._run`) and the unknown-node refresh's reload (`ExportWatch._reload_for_export`).
ENTRY_LOCKS: HassKey[dict[str, asyncio.Lock]] = HassKey(f"{DOMAIN}_service_locks")


def entry_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Return the entry's lock (`ENTRY_LOCKS`), created on first use."""
    return hass.data.setdefault(ENTRY_LOCKS, {}).setdefault(entry_id, asyncio.Lock())


@dataclass
class KnownMesh:
    """What Bluetooth discovery recognises an entry's mesh by (review-4 H4-4), beyond the entry's unique id.

    `network_ids`: the export's keys (both of an export written mid key refresh) and the key of a refresh the hub
    follows, whatever its phase — mid refresh, and after one completed while the hub was not looking at the unique
    id, the proxies advertise a Network ID the unique id does not hold. `macs`: the export's nodes, which advertise
    from their public MAC (`jhmesh.advert.mac_from_uuid`) whatever key they hold — a node of this mesh after a key
    refresh the export lacks is still this mesh, and a new entry for it could only end at `mesh_already_configured`.
    Unverified on air (no key refresh on this installation); the MAC is the node UUID on every node here.
    """

    network_ids: set[bytes]
    macs: frozenset[str]


# `KnownMesh` by entry id: every setup records it (`__init__.async_setup_entry`, before the not-ready check), the hub
# adds a followed key as the refresh moves (`JungHomeHub._on_key_refresh`), discovery loads the export of an entry
# that never got that far once (`config_flow.async_known_mesh_of`). A reconfigure and the removal forget it.
KNOWN_MESHES: HassKey[dict[str, KnownMesh]] = HassKey(f"{DOMAIN}_known_meshes")


def node_macs(cdb: CDB) -> frozenset[str]:
    """Return the public MACs the export's nodes advertise from (upper case); a node UUID of another shape has none."""
    return frozenset(
        mac for n in cdb.nodes if (mac := mac_from_uuid(n.uuid)) is not None
    )


async def async_known_mesh(hass: HomeAssistant, cdb: CDB, unicast: int) -> KnownMesh:
    """Return what discovery knows `cdb`'s mesh by: its keys, the stored followed refresh's key, its nodes' MACs.

    The followed refresh is the one a hub at address `unicast` reads (`_stored_key_refresh`), proven or not: an
    unproven key only stops discovery offering that Network ID, it is never used to transmit.
    """
    ids = {nk.network_id for nk in cdb.rx_net_keys(NET_KEY_INDEX)}
    data = await seq_store(hass, cdb).async_load()
    if (stored := _stored_key_refresh(data, f"{unicast:04X}")) is not None:
        ids.add(
            NetKeyMaterial.derive(KeyRefreshRecord.from_stored(stored).key).network_id
        )
    return KnownMesh(ids, node_macs(cdb))


def remember_known_mesh(hass: HomeAssistant, entry_id: str, known: KnownMesh) -> None:
    """Record what discovery knows the entry's mesh by (`KNOWN_MESHES`), in place of anything recorded before."""
    hass.data.setdefault(KNOWN_MESHES, {})[entry_id] = known


def forget_known_mesh(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the entry's `KNOWN_MESHES` record: its export changed or it is gone."""
    hass.data.get(KNOWN_MESHES, {}).pop(entry_id, None)


async def async_release_network_id(
    hass: HomeAssistant,
    entry: ConfigEntry,
    network_id: str,
    *,
    keep_flow: str | None = None,
) -> None:
    """Free `network_id` for `entry`'s unique id: no discovery flow of it left, no ignored entry holding it (H4-4).

    After a key refresh the proxies advertise the new Network ID before the entry's unique id holds it: a discovery
    flow it started is the entry's own mesh, and an entry the user made by *Ignore* on that flow would make the
    unique-id update collide (Home Assistant logs an error and raises a core repair). `keep_flow` is the flow
    asking (a reconfigure), which must not abort itself. Unverified on air.
    """
    for flow in hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, include_uninitialized=True, match_context={"unique_id": network_id}
    ):
        if flow["flow_id"] != keep_flow:
            hass.config_entries.flow.async_abort(flow["flow_id"])
    holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, network_id)
    if holder is not None and holder.source == SOURCE_IGNORE:
        _LOGGER.info(
            "Removing the ignored discovery of %s's mesh: it advertised the mesh's new network key",
            entry.title,
        )
        await hass.config_entries.async_remove(holder.entry_id)


def _evidence_of_use(
    cdb: CDB, unicast: int, keeper: VaultKeeper, *stores: Any
) -> str | None:
    """Why address `unicast`, which has no sequence-number record, may have sent before; None when nothing says so.

    The export holds Home Assistant's provisioner node, or any node, at it (`jhmesh.vault.recognise`); the vault
    keeps Home Assistant's identity in this mesh (it ran here, so a store existed); or the loaded `stores` (the store,
    its `.backup` copy, the floor) know other addresses (this Home Assistant sent in this mesh, a lost record of
    this one would look the same). Wrong only for a genuinely fresh address on an installation that matches: that
    one spends SEQ_SKIP_AHEAD of its 2^24 numbers, once.
    """
    if recognise(cdb, unicast) is not None:
        return "the export holds Home Assistant's provisioner node there"
    if cdb.element(unicast) is not None:
        return "a node of the export has that address"
    if keeper.vault is not None:
        return "the vault keeps Home Assistant's identity in this mesh"
    known = sorted({src for data in stores for src in _addresses_of(data) or {}})
    if known:
        return f"the store knows other addresses ({', '.join(known)})"
    return None


def _settled(st: ElementState, kind: str) -> bool:
    """Whether the element's last Status of `kind` shows no transition: its present values equal their targets."""
    if kind == "level":
        return st.level is not None and st.level == st.target_level
    if kind == "ctl":
        return (
            st.lightness is not None
            and st.lightness == st.target_lightness
            and st.kelvin == st.target_kelvin
        )
    if kind == "dimmer":
        return st.lightness is not None and st.lightness == st.target_lightness
    return st.on is not None and st.on == st.target_on


@dataclass
class ElementState:
    """Last known state of one mesh element (a load, a socket, a level or battery element, a property owner).

    `on` / `lightness` / `kelvin` are the *present* values of the last status (what the element is doing now, what
    the app shows); `target_*` is where a transition is heading, equal to the present value when the element is idle
    or the status came in its short form. Nothing renders the targets yet: they are kept because they are free.
    """

    on: bool | None = None
    lightness: int | None = None  # 0..65535
    kelvin: int | None = None
    target_on: bool | None = None
    target_lightness: int | None = None
    target_kelvin: int | None = None
    # when the element will be off by its last Generic OnOff Status: on, heading off, with a known remaining time
    # (a run-on time running out, or a fade to off); None otherwise (`_on_onoff_status`)
    off_at: datetime | None = None
    # a CTL light's own temperature range (Light CTL Temperature Range Status); None until read
    kelvin_min: int | None = None
    kelvin_max: int | None = None
    level: int | None = None  # Generic Level, -32768..32767 (blinds position / slat)
    target_level: int | None = None  # a moving blind's destination; level when idle
    battery: int | None = None  # Generic Battery level, percent
    power_w: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    # the polled SIG counters of a metering socket (COUNTER_READS); None until read or while the socket says "unknown"
    power_on_hours: int | None = None
    energy_wh: int | None = (
        None  # 0x0072 precise total energy: lifetime, nothing resets it
    )
    energy_resettable_wh: int | None = (
        None  # 0x006A total energy: what the app shows and its "reset" zeroes
    )
    energy_since_on_wh: int | None = (
        None  # 0x000D energy since the socket was switched on
    )
    # the load's meter does not serve 0x0072, so its *Energy* is 0x006A (`energy_total`); never set on a socket
    energy_fallback: bool = False
    properties: dict[int, bytes] = field(
        default_factory=dict
    )  # raw property values by property id (SIG or JUNG)
    # raw SIG setup-server states by their Status opcode (OnPowerUp, Lightness Range / Default, CTL Default)
    setup: dict[int, bytes] = field(default_factory=dict)
    # a node's registered Health faults (its primary element; JUNG's vendor codes 0x81 / 0x80); None until read
    faults: tuple[int, ...] | None = None
    # the element's current scene (Scene Status / Scene Register Status; 0 = none): what its Scene Server says it
    # last recalled and still shows; None until the element said
    scene: int | None = None
    # a load's lock function (0x0009, `note_lock`): None until it reported one; and when a timed lock should end
    lock: EnforcedOutput | None = None
    lock_until: datetime | None = None
    updated: float = field(default_factory=time.monotonic)

    def note_lock(self, raw: bytes) -> None:
        """Take a reported lock function (0x0009); its time limit counts from now, as the *Lock* switch counts it.

        Whether a Status carries the time left or the time the lock was set for is not known (a read-back of a
        timed lock that still holds pushes `lock_until` on by the whole limit again). A malformed value is ignored.
        """
        try:
            value: EnforcedOutput = PROPERTIES[PROPERTY_LOCK].codec.decode(raw)
        except ValueError:
            return
        self.lock = value
        self.lock_until = (
            dt_util.utcnow() + timedelta(seconds=value.time_s)
            if value.locked and value.time_s
            else None
        )

    @property
    def locked(self) -> bool:
        """Whether the load reported a lock that has not run out yet (its time limit, when it had one)."""
        if self.lock is None or not self.lock.locked:
            return False
        return self.lock_until is None or dt_util.utcnow() < self.lock_until

    @property
    def energy_total(self) -> int | None:
        """The *Energy* sensor's counter: the lifetime total 0x0072, or 0x006A where the meter does not serve it.

        Only a load other than a metering socket falls back (`Energy._get_counters`, `_on_sig_property_status`),
        and only on a definite sign, never because 0x0072 went unanswered: 0x006A is at most 0x0072, so switching to
        it after a silence and back once 0x0072 answers would put the whole difference into one hour of the Energy
        dashboard. 0x006A is resettable; the app's "reset consumption" then shows as a meter reset, which
        `total_increasing` statistics handle.
        """
        return self.energy_resettable_wh if self.energy_fallback else self.energy_wh


def load_network(cdb_path: str, metadata_dir: str | None) -> tuple[CDB, Devices]:
    """Blocking: parse the app export (and optional metadata) into the device model."""
    cdb = CDB.load(Path(cdb_path))
    if metadata_dir:
        meta = Metadata(
            Path(metadata_dir) / "device_metadata.json",
            Path(metadata_dir) / "scene_metadata.json",
        )
    else:
        meta = Metadata.from_export(
            cdb.export_meta
        )  # names travel inside the app's share export
    return cdb, build_devices(cdb, meta)


def _key_material(
    cdb: CDB,
) -> tuple[dict[int, bytes], dict[int, bytes], set[tuple[int, bytes, int]]]:
    """Return the keys an export carries — NetKeys, AppKeys, a key refresh it caught — to compare, never to show."""
    return (
        {i: k.key for i, k in cdb.net_keys.items()},
        {i: k.key for i, k in cdb.app_keys.items()},
        {(i, k.key, phase) for i, (k, phase) in cdb.net_key_refresh.items()},
    )


def _node_identity(node: Node) -> tuple[int, bytes, int | None, tuple[int, ...]]:
    """Return what the proxy client knows a node by: its address, device key, product and element addresses."""
    return node.unicast, node.dev_key, node.pid, tuple(e.address for e in node.elements)


def nodes_by_mac(cdb: CDB) -> dict[str, Node]:
    """Return the export's JUNG nodes by the public MAC they advertise from, the excluded ones (being removed) too.

    JUNG nodes advertise from their public MAC, which the export encodes in the node UUID (`jhmesh.advert`): the
    node behind a Bluetooth address is known before any message is exchanged. A node the export marks excluded is no
    device but still no stranger: its adverts must not ask for a new export.
    """
    return {
        mac: n
        for n in (*cdb.nodes, *cdb.excluded_nodes)
        if n.pid is not None and (mac := mac_from_uuid(n.uuid)) is not None
    }


def hub_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the entry data a hub is built from (`HUB_DATA_KEYS`); the update listener reloads the entry when it changed.

    The gateway host, token and certificate fingerprint are left out: they only serve the config flow's re-fetch.
    """
    return {key: data.get(key) for key in HUB_DATA_KEYS}


@dataclass(frozen=True)
class LinkEnd:
    """How a proxy link ended (`JungHomeHub._link_ended`): why, what it says about the proxy, how long it lasted.

    `penalise` True counts the end against the proxy (it went silent), False not at all (we ended a working link
    ourselves: the repair's skip-ahead, an error of our own), None by how long the link lasted (`SHORT_LINK`).
    """

    reason: str
    penalise: bool | None
    lasted: float  # seconds the link was up


# what `JungHomeHub._link_end` holds while no link is up: before the first, and while one is being set up
NO_LINK = LinkEnd("no link yet", False, 0.0)
LINK_HISTORY = 20  # links the diagnostics describe (`JungHomeHub.link_history`)


@dataclass(frozen=True)
class LinkRecord:
    """One past link, for the diagnostics (review-4 R I-9: nothing told why the links of the last hours ended).

    The proxy is named by its mesh address only, never by its Bluetooth MAC.
    """

    proxy_node: (
        int | None
    )  # the proxy's unicast address, None when it never named itself
    ended: float  # monotonic time the link ended
    lasted: float  # seconds the link was up
    reason: str
    penalise: bool | None  # as in `LinkEnd`
    refresh: (
        float | None
    )  # seconds from link-up to the end of its state refresh, None when it never got through
    held_back: float  # seconds sends were held back for the sequence-number store during the link


class JungHomeHub:
    """One mesh network: connection loop, state cache, commands, button gestures."""

    def __init__(  # noqa: PLR0915  # one attribute per line of state the hub keeps
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        cdb: CDB,
        devices: Devices,
        state: HAState,
        vault: VaultKeeper,
    ) -> None:
        """Bind the mesh export to HA: proxy client, empty state cache, no link yet."""
        self.hass = hass
        self.entry = entry
        self.cdb = cdb
        # the mesh's provisioner identity and device-key vault (review-3 N1, `identity.py`)
        self.vault = vault
        self.devices = devices
        self.state = (
            state  # our address, sequence number and IV state, persisted per mesh
        )
        state.stall_listener = self._seq_stall_started
        self._unsub_seq_stall: CALLBACK_TYPE | None = None
        # the nodes' reachability and heartbeats (`hub/liveness.py`, review-4 A4-3)
        self.liveness = Liveness(self)
        self.proxy = ProxyClient(
            cdb,
            state,
            on_message=self._on_message,
            on_disconnect=self._on_disconnect,
            on_beacon=self._on_beacon,
            on_filter_status=self._on_filter_status,
            on_undecryptable=self._on_undecryptable,
            on_heartbeat=self.liveness.on_heartbeat,
            on_key_refresh=self._on_key_refresh,
            on_foreign_own_source=self._on_foreign_own_source,
        )
        self.states: dict[int, ElementState] = {}
        for unicast, info in (
            hass.data.get(NODE_VERSIONS, {}).get(entry.entry_id, {}).items()
        ):
            if (raw := info.get(NODE_INFO[SIG_SOFTWARE_VERSION])) is not None:
                self.element_state(unicast).properties[SIG_SOFTWARE_VERSION] = raw
        self.device_ids: dict[
            str, str
        ] = {}  # our identifier → device registry id (parents for via_device_id)
        self.proxy_address: str | None = None
        self.proxy_node: int | None = None
        self.connected_since: float | None = None
        # set with `connected_since`, cleared when the link goes down
        self._link_up = asyncio.Event()
        self.link_count = 0  # links made so far: entities tell "asked on this link already" from "ask again" by it
        self._connect_failure_logged = False  # the first failure of a link-down period is a WARNING, the rest DEBUG
        self.link_state = LINK_SEARCHING  # one of LINK_STATES (`set_link_state`), for the link state sensor
        self._bluetooth_off = False  # `bluetooth_unavailable` is raised
        self._task: asyncio.Task[None] | None = None
        # the connect-time reads of every link (`hub/refresh.py`)
        self.refresh = Refresh(self)
        # the metered loads' polls and reads, and the time and location broadcasts (`hub/energy.py`, `hub/clock.py`)
        self.energy = Energy(self)
        self.clock = Clock(self)
        self._link_lost = asyncio.Event()
        self.stopping = False  # `async_stop` began: nothing new is scheduled
        self._unsub_adv: Callable[[], None] | None = None
        self._was_available = False
        self._last_rx = time.monotonic()  # when the proxy last forwarded anything we could decode, or named itself (link watchdog)
        self.rx_messages = 0  # decoded access messages, any destination
        self.rx_to_us = (
            0  # ... of which unicast to our address: proof that nodes accept our PDUs
        )
        self._pdus_dropped = False
        # the `address_shared` repair skipped past another client's numbers since this hub started: a new sighting
        # asks for another address (`_report_address_shared`)
        self._address_shared_skipped = False
        # per link: whether the proxy's Secure Network Beacon authenticated (it sends one right after we subscribe),
        # and the watchdog waiting for its Filter Status (`_filter_status_overdue`)
        self.beacon_authenticated = False
        self._unsub_filter_watch: Callable[[], None] | None = None
        # node unicast → its last Configuration Server audit (`async_audit`), for the diagnostics
        self.audits: dict[int, NodeAudit] = {}
        # scene number → {member element → its JUNG scene action (None: stored without a description)}, read from
        # the members' Scene Action Setup servers at link-up (`Refresh.get_scene_actions`, `Refresh.connect_step`)
        self.scene_actions: dict[int, dict[int, V.Action | None]] = {}
        # the channels whose scene list answered (`Refresh._get_scene_actions_of`): only theirs narrows `scenes_of`
        self.scene_lists_read: set[int] = set()
        # element → the scene numbers its register held when it last reported it (Scene Register Status)
        self.scene_registers: dict[int, tuple[int, ...]] = {}
        # scene number → when EVENT_SCENE_RECALLED last fired for it (`note_scene_recall`, SCENE_RECALL_WINDOW)
        self._scene_recalls: dict[int, float] = {}
        self.node_by_mac = nodes_by_mac(cdb)  # the node behind a Bluetooth address
        # the entry's configurator, which registers itself: the unknown-node refresh adopts through it, under its lock
        self.configurator: MeshConfigurator | None = None
        # what follows the changes made in the JUNG HOME app (`app_follow.py`, review-4 U4-6), set by the setup
        self.app_follow: AppFollower | None = None
        # the platforms' entities, by platform (`entity.async_setup_platform`), and what makes the hub follow the
        # export after a change, in place or by a reload (`model_update.async_follow_export`, set by the setup)
        self.platforms: dict[str, TrackedPlatform] = {}
        self.follow_export: Callable[[], Awaitable[None]] | None = None
        # the gateway's REST status polls (`gateway_status.gateway_polls`), made by the first platform that asks
        self.gateway_polls: GatewayPolls | None = None
        # the unknown nodes, the export refresh and the gateway's trust (`hub/export_watch.py`)
        self.export_watch = ExportWatch(self)
        # the battery nodes a Config plan or a property change keeps awake (`keep_awake.py`, review-3 W4 / F24)
        self.keep_awake = KeepAwake(self)
        # the devices Home Assistant added, carried through the app's key refresh (`vault_refresh.py`, review-4 D11)
        self.vault_refresh = VaultKeyRefresh(
            self, issue_id(entry, ISSUE_VAULT_KEY_REFRESH)
        )
        # each node's insert and key layout: export, advert, a Get (`inserts.py`, review-4 F4-12)
        self.inserts = NodeInserts(self, issue_id(entry, ISSUE_INSERT_MISMATCH))
        # each node's clock, zone offset and stored location as it answers them (`node_clocks.py`, review-4 F4-8)
        self.clocks = NodeClocks(self, issue_id(entry, ISSUE_NODE_CLOCK_WRONG))
        self._lost_at: float | None = (
            None  # monotonic time the last link was lost, while no new one is up
        )
        self._unsub_grace: CALLBACK_TYPE | None = None  # the end of the link-loss grace
        # how the last link ended, None while one is up (`_link_ended`); when the current one came up (monotonic)
        self._link_end: LinkEnd | None = NO_LINK
        self._link_since = 0.0
        # how the link before the current one ended, for the connect-time steps (`Refresh.connect_step`)
        self.previous_link = NO_LINK
        # proxy MAC → its links in a row that ended within SHORT_LINK (`_judge_link`)
        self._short_links: dict[str, int] = {}
        self._link_loss_listeners: list[Callable[[LinkEnd], None]] = []
        # the last LINK_HISTORY links, oldest first (`_link_ended`); for the current one: how long its state refresh
        # took (None until it is through) and the store's `held_back_total` when it came up
        self.link_history: deque[LinkRecord] = deque(maxlen=LINK_HISTORY)
        self.link_refresh: float | None = None
        self._held_back_at_link = 0.0
        self._probe_link = (
            asyncio.Event()
        )  # a command went unanswered: the watchdog asks the proxy now
        # load commands that went unanswered, waiting for the probe's verdict on the link (`_load_command`):
        # (element the miss is accounted to, its state Get's kind, when it was asked)
        self._unanswered: list[tuple[int, str, float]] = []
        self._unsub_ha_stop: CALLBACK_TYPE | None = None
        self._unsub_echo: CALLBACK_TYPE | None = (
            None  # the check that a command was answered (`_command`)
        )
        # link diagnostics (review-3 F8, F9): when each node was last heard (wall clock), the signal strength of
        # its last advertisement, when it last restarted, and the last sequence number per source
        self.last_seen: dict[int, datetime] = {}
        self.node_rssi: dict[int, int] = {}
        self.restarted: dict[int, datetime] = {}
        self._last_seq: dict[int, int] = {}
        self._node_signalled: dict[int, float] = {}
        self._unsub_seq_check: CALLBACK_TYPE | None = None
        # load element → the pending read of its state after a transition (`_reread_after_transition`)
        self._transition_reread: dict[int, CALLBACK_TYPE] = {}
        # node unicast → the pending end of its Node Identity advert (`async_locate`)
        self._locating: dict[int, CALLBACK_TYPE] = {}
        self._rebuilding = (
            False  # `async_begin_rebuild` ran: a reload replaces this hub
        )
        # what this hub was built from; `needs_rebuild` tells the update listener whether the entry moved away from it
        self._built_from = (hub_data(entry.data), dict(entry.options))
        # per link: what the proxy forwarded that our keys could open, and what they could not (stale export detection)
        self.rx_decoded_link = 0
        self.rx_undecodable_link = 0
        self._export_stale = False
        # the keys' gestures: clicks held back (the `click_delay` option, read from the entry here), holds, repeat
        # suppression and the event listeners (`hub_gestures.py`, review-4 A4-3); it ends its holds on link loss
        self.gestures = ButtonGestures(self)
        # element → (what its last state Set asked for, its cached values before it), for `async_wait_settled`
        self._requested: dict[
            int, tuple[dict[str, bool | int], dict[str, bool | int | None]]
        ] = {}

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    async def async_create(
        cls,
        hass: HomeAssistant,
        entry: ConfigEntry,
        cdb: CDB,
        devices: Devices,
        unicast: int,
    ) -> JungHomeHub:
        """Create the hub with the mesh's sequence-number store loaded (a per-entry store of 0.2 is folded in first).

        The 0.2 store was keyed by the entry id, so a removed and re-added entry started its sequence numbers
        over; `async_migrate_legacy_seq_store` merges its one record into the per-mesh store and removes it — a
        no-op here when `async_setup_entry` already called it (the normal path), a safety net for a caller that
        goes straight to `async_create`. A legacy record that still could not be saved stops the setup (not
        ready): the mesh's store does not know its numbers, so the address would start over at 0 and reuse them.

        One running hub per mesh: another entry's hub already owning the mesh's counters (`SEQ_OWNERS`) refuses
        this one (`_refuse_duplicate_mesh`). A record restored from a Home Assistant backup continues past every
        number sent since (`_async_skip_restored_record`). An address without a record anywhere starts at 0 only
        when nothing says it was used before (`_evidence_of_use`); otherwise SEQ_SKIP_AHEAD on.
        """
        if not await async_migrate_legacy_seq_store(hass, entry, cdb.mesh_uuid):
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="seq_store_not_written"
            )
        await async_load_node_versions(hass, entry.entry_id)
        vault = await async_vault_keeper(hass, cdb.mesh_uuid)
        store = seq_store(hass, cdb)
        backup = seq_backup_store(hass, cdb)
        floor = seq_floor_store_for_uuid(hass, cdb.mesh_uuid)
        data = await store.async_load()
        backup_data = await backup.async_load()
        floor_data = _landed(floor, await floor.async_load())
        floor.written = floor_data  # what `HAState` keeps the floor up from
        corrupt = (
            await hass.async_add_executor_job(_newest_corrupt_seq_store, store.path)
            if data is None
            else None
        )
        key = f"{unicast:04X}"
        if _usable_record(data, key) is None:
            # review-3 S1: a record that parses as JSON but not as a counter used to start at 0 (reusing nonces)
            # and its first save overwrote the good backup; a lost store with history did the same
            if (record := _usable_record(backup_data, key)) is not None:
                _LOGGER.warning(
                    "Sequence-number record of address %s unusable in the store, continuing from the backup copy",
                    key,
                )
                base = _addresses_of(data) or _addresses_of(backup_data) or {}
                data = _store_with(
                    data if _mesh_of(data) else backup_data, {**base, key: record}
                )
                await store.async_save(data)
            elif (
                _has_history(data, key)
                or _has_history(backup_data, key)
                or _has_history(floor_data, key)  # an earlier repair's floor
                or corrupt is not None
            ):
                _report_seq_store_lost(hass, entry, cdb.mesh_uuid, key, corrupt)
            elif data is None and backup_data is not None:
                _LOGGER.warning(
                    "sequence-number store unreadable, continuing from the backup copy"
                )
                data = backup_data
                await store.async_save(data)
        restored = await _async_skip_restored_record(
            hass, cdb.mesh_uuid, key, data, backup_data, floor_data
        )
        if restored is not data:
            data = backup_data = restored
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry, ISSUE_SEQ_STORE_LOST))
        # what is on disk right now, before anything new is reserved from it (both copies bound it: `_limit`)
        store.written = data
        backup.written = backup_data
        start = data
        if _usable_record(data, key) is None and (
            why := _evidence_of_use(cdb, unicast, vault, data, backup_data, floor_data)
        ):
            # review-4 S I5: only in what the `HAState` starts from — on disk it is the first save, which sends
            # wait for (`_limit`): a crash before it lands starts here again, with nothing sent
            _LOGGER.warning(
                "Address %s has no sequence-number record, but %s: its numbers start at %06X, past any it may "
                "have sent, rather than at 0",
                key,
                why,
                SEQ_SKIP_AHEAD,
            )
            start = _store_with(
                data,
                {
                    **(_addresses_of(data) or {}),
                    key: {
                        "seq": SEQ_SKIP_AHEAD,
                        "iv_index": 0,
                        "iv_update_active": False,
                        "iv_known": False,
                        "rpl": {},
                        "clean": False,
                        # sent under an index nobody knows: keep counting on up to the first beacon's
                        "seq_guard": SEQ_GUARD_FIRST_BEACON,
                    },
                },
            )
        # no await from here to the HAState: two entries set up at once cannot both pass the check
        _refuse_duplicate_mesh(hass, entry, cdb.mesh_uuid)
        state = HAState(
            store,
            start,
            unicast,
            cdb.mesh_uuid.lower(),
            backup,
            entry.entry_id,
            floor=floor,
        )
        return cls(hass, entry, cdb, devices, state, vault)

    async def async_start(self) -> None:
        """Start watching for proxy advertisements, the connection loop and the daily Time Set.

        The energy poll is armed by every connection (`Energy.arm_poll`), so its grid starts with the link.
        """
        # a successful (re)start after a reconfiguration means the user followed the key-refresh advice; a mesh that is
        # still refreshing raises the issue again with its next beacon, one whose keys still do not fit raises the
        # stale-export issue again as soon as the first link carries traffic; likewise a new export knows the nodes
        # the last hub saw advertising: any it still lacks are re-reported (`async_stop` cleared them all already;
        # this covers a hub whose predecessor never ran)
        self._clear_issues()
        self.inserts.report_mismatch()
        self._report_time_keeper()
        if self.state.address_shared is not None:
            # stored by an earlier run: sends stay refused until the repair, across the restart too
            self._report_address_shared()
        self._unsub_adv = bluetooth.async_register_callback(
            self.hass,
            self._adv_seen,
            {"service_uuid": MESH_PROXY_SERVICE, "connectable": True},
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
        self._task = self.entry.async_create_background_task(
            self.hass, self._connection_loop(), f"{DOMAIN} link"
        )
        self.clock.unsub_time = async_track_time_interval(
            self.hass, self.clock.send_time_daily, timedelta(seconds=TIME_SET_INTERVAL)
        )
        self.clock.arm_offset_change()
        if self.heartbeats_enabled:
            self.liveness.unsub_heartbeats = async_track_time_interval(
                self.hass,
                self.liveness.check_heartbeats,
                timedelta(seconds=HEARTBEAT_CHECK_INTERVAL),
            )
        self._unsub_seq_check = async_track_time_interval(
            self.hass,
            self._check_sequence_space,
            timedelta(seconds=SEQUENCE_CHECK_INTERVAL),
        )

        async def on_ha_stop(_event: Event) -> None:
            # Home Assistant does not unload entries when it stops: close the link (an ESPHome proxy's slot) and
            # store the counter as cleanly closed, so the next start needs no restart margin
            self._unsub_ha_stop = None
            await self.async_stop()

        self._unsub_ha_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, on_ha_stop
        )

    async def async_stop(self) -> None:
        """Stop the connection loop and background work, then drop the link."""
        self.stopping = True
        for unsub in (
            self._unsub_adv,
            self.clock.unsub_time,
            self.energy.unsub_energy,
            self.liveness.unsub_heartbeats,
            self._unsub_seq_check,
            self._unsub_grace,
            self._unsub_ha_stop,
            self._unsub_echo,
            self.clock.unsub_offset_change,
            self._unsub_seq_stall,
        ):
            if unsub:
                unsub()
        self._unsub_grace = self._unsub_ha_stop = self._unsub_echo = None
        self.clock.unsub_offset_change = self._unsub_seq_stall = None
        self._unsub_adv = self.clock.unsub_time = self.energy.unsub_energy = None
        self.liveness.unsub_heartbeats = self._unsub_seq_check = None
        self.export_watch.cancel_timer()
        self._cancel_filter_watch()
        # not a pending retry of a failed upload: it is the entry's and outlives the reload most changes end with
        # (`MeshConfigurator._upload_or_retry`); removing the entry cancels it
        for cancel in (
            *self.liveness.recheck.values(),
            *self._transition_reread.values(),
            *self._locating.values(),  # the nodes stop by themselves within 60 s
        ):
            cancel()
        self.liveness.recheck.clear()
        self._transition_reread.clear()
        self._locating.clear()
        self.gestures.cancel_all()
        try:
            await self._cancel(self._task)
            self._task = None
            await self._cancel(self.refresh.task)
            self.refresh.task = None
            await self._cancel(self.energy.task)
            self.energy.task = None
            await self._cancel(self.liveness.heartbeat_task)
            self.liveness.heartbeat_task = None
            await self._cancel(self.vault_refresh.task)
            self.vault_refresh.task = None
        finally:
            # always reached, even if one of the cancels above still raised: an unclosed link keeps holding a
            # connection slot, and an unclosed counter keeps persisting into the shared store forever (HAC-04)
            try:
                await asyncio.wait_for(self.proxy.detach(), STOP_TIMEOUT)
            except TimeoutError:
                _LOGGER.warning(
                    "The Bluetooth link did not close within %.0f s; leaving it",
                    STOP_TIMEOUT,
                )
            # nothing is sent after the detach: the counter can be stored as exact; and an unloaded mesh has no
            # live problems to show — a restart raises them again if they persist
            await self.state.async_close()
            self._clear_issues()

    def _clear_issues(self) -> None:
        for key in (
            ISSUE_SEQ_STORE_UNWRITABLE,
            ISSUE_IV_INDEX_MISMATCH,
            ISSUE_SEQUENCE_SPACE_LOW,
            ISSUE_KEY_REFRESH,
            ISSUE_PDUS_DROPPED,
            ISSUE_ADDRESS_SHARED,
            ISSUE_EXPORT_STALE,
            ISSUE_UNKNOWN_NODES,
            ISSUE_DUPLICATE_MESH,
            ISSUE_BLUETOOTH_UNAVAILABLE,
            ISSUE_VAULT_KEY_REFRESH,
            ISSUE_INSERT_MISMATCH,
            ISSUE_NODE_CLOCK_WRONG,
            ISSUE_TIME_KEEPER_MISSING,
        ):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.entry, key))

    @staticmethod
    async def _cancel(task: asyncio.Task[None] | None) -> None:
        """Cancel `task` and wait for it, without raising an error it had already failed with (HAC-04).

        `cancel()` is a no-op on a task that is already done; a task that finished with an exception before we
        got here then re-raises it from `await task`, which is not an error this cancel caused and was never
        ours to raise — `async_stop` used to abort right there, skipping the disconnect and the counter close
        that must always run. A cancellation of the caller itself (this coroutine's own task being torn down,
        not merely the cancellation we just issued to `task`) still propagates.
        """
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except (
            Exception
        ):  # a task that had already failed: its error was not ours to raise here
            _LOGGER.debug(
                "background task %s had failed", task.get_name(), exc_info=True
            )

    @property
    def connected(self) -> bool:
        """Whether a proxy link is up."""
        return self.proxy.connected

    @property
    def link_since(self) -> float:
        """When the current (or last) link came up, a `time.monotonic()`: a read made since is this link's."""
        return self._link_since

    @property
    def link_available(self) -> bool:
        """Whether the entities count as reachable: a link is up, or one was lost less than LINK_LOSS_GRACE ago.

        A lost link is usually replaced within seconds by the next proxy node; flapping every entity to unavailable
        and back for that (and failing a command sent meanwhile) is worse than a short wait (`_command`). A link
        counts once `_connect_to` took it: `connected` turns True inside `attach()` already, and a link lost before
        that is a failed connection, not one to show as up for a moment (review-4 R4-3).
        """
        if self.connected and self._link_end is None:
            return True
        return (
            self._lost_at is not None
            and not self.stopping
            and time.monotonic() - self._lost_at < LINK_LOSS_GRACE
        )

    async def async_wait_connected(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a proxy link; whether one is up.

        The event is re-cleared before each wait rather than trusted: the proxy client drops `connected` in its
        disconnect callback a moment before `_set_available(False)` clears the event, so a still-set event
        must not end the wait while there is no link.
        """
        try:
            async with asyncio.timeout(timeout):
                while not self.connected:
                    self._link_up.clear()
                    await self._link_up.wait()
        except TimeoutError:
            return False
        return True

    @property
    def metadata(self) -> Metadata:
        """The app's names for loads, gangs and scenes, as `load_network` read them with the export."""
        return self.devices.metadata

    @property
    def needs_rebuild(self) -> bool:
        """Whether the entry's options or the data the hub was built from (`hub_data`) changed since it started."""
        return self._built_from != (hub_data(self.entry.data), dict(self.entry.options))

    async def async_begin_rebuild(self) -> bool:
        """Prepare this hub's replacement by a reload; False when a rebuild already started.

        The update listener runs again for every entry update while the reload is pending — recording which nodes
        confirmed the heartbeat disable round is one — and must not run the round, or the reload, twice. When the
        heartbeat option went off, the nodes are told to stop first; the per-link refresh is cancelled before, and
        so is the rest of the heartbeat work (the check timer, a renewal or reprobe round in flight), so none of
        their configure Sets can interleave with the disable round and switch a node that confirmed it back on.
        """
        if self._rebuilding:
            return False
        self._rebuilding = True
        if self.heartbeats_enabled and not self.entry.options.get(
            OPTION_HEARTBEATS, DEFAULT_HEARTBEATS
        ):
            self._cancel_refresh()
            assert (
                self.liveness.unsub_heartbeats is not None
            )  # armed by `async_start` with the option on
            self.liveness.unsub_heartbeats()
            self.liveness.unsub_heartbeats = None
            await self._cancel(self.liveness.heartbeat_task)
            await self._cancel(self.liveness.reprobe_task)
            self.liveness.heartbeat_task = self.liveness.reprobe_task = None
            await self.liveness.async_disable_heartbeats()
        return True

    # ------------------------------------------------------------------ following the export in place (D23)
    def model_refusal(self, cdb: CDB) -> str | None:
        """Why the running hub cannot take over `cdb` in place (`model_update`); None when it can.

        The link, the sequence numbers and the proxy client were set up for this mesh and its keys, and the client
        knows each element's device key (`ProxyClient`): those must stay as they are. A node may be added — the
        unknown-node adoption — but none may leave, move or be keyed anew: a node gone takes devices, pending work
        and its client state with it, which only a reload clears. Nothing here names a key.
        """
        old = self.cdb
        if cdb.mesh_uuid.lower() != old.mesh_uuid.lower():
            return "the export is of another mesh"
        if _key_material(cdb) != _key_material(old):
            return "the network or application keys changed"
        new = {n.uuid: n for n in cdb.nodes}
        for node in old.nodes:
            if (other := new.get(node.uuid)) is None:
                return f"node {node.unicast:04X} left the export"
            if _node_identity(other) != _node_identity(node):
                return f"node {node.unicast:04X} changed its address, key or elements"
        return None

    def swap_model(self, cdb: CDB, devices: Devices) -> tuple[CDB, Devices]:
        """Put `cdb` and `devices` in the place of the hub's model; return the ones they replaced.

        Only the model itself (and the gateway polls' node, which their entities are built from): `model_update`
        builds the platforms' entities from a new export with it and swaps back before anything else runs;
        `async_apply_model` makes it stick.
        """
        previous = self.cdb, self.devices
        self.cdb, self.devices = cdb, devices
        if (polls := self.gateway_polls) is not None:
            polls.node = next(
                (n for n in cdb.nodes if n.uuid == polls.node.uuid), polls.node
            )
        return previous

    @callback
    def async_apply_model(
        self, cdb: CDB, devices: Devices, *, scenes: bool = False
    ) -> None:
        """Take over a new device model in place of a reload (`model_update`, review-4 D23); `model_refusal` passed.

        The states cache, the link and everything learnt over it stay. What a reload would have redone follows:
        the client learns the nodes that were added (their device keys and elements), the nodes are known by MAC
        again (a node of the unknown-node repair now in the export leaves it), the audits are of the old export and
        go, the scene members are asked again what they do when the scenes changed — or the change stored or
        deleted scenes on them (`scenes`), which the export does not tell (`_reread_scenes`) — and over the link of
        the moment the new loads are asked for their state and the new mains nodes for heartbeats, as the next
        link would have done.
        """
        old_cdb, old_devices = self.swap_model(cdb, devices)
        self.proxy.cdb = cdb
        known = {n.uuid for n in old_cdb.nodes}
        added = [n for n in cdb.nodes if n.uuid not in known]
        for node in added:
            self.proxy.add_node(node)
        self.node_by_mac = nodes_by_mac(cdb)
        self.audits.clear()
        unknown = self.export_watch.unknown_nodes
        if adopted := [mac for mac in unknown if mac in self.node_by_mac]:
            for mac in adopted:
                del unknown[mac]
            if unknown:
                self.export_watch.report_unknown_nodes()
            else:
                self.export_watch.cancel_timer()
                ir.async_delete_issue(
                    self.hass, DOMAIN, issue_id(self.entry, ISSUE_UNKNOWN_NODES)
                )
        if scenes or cdb.scenes != old_cdb.scenes:
            self._reread_scenes()
        new_loads = set(devices.by_address) - set(old_devices.by_address)
        if self.connected and (new_loads or added):
            self.entry.async_create_background_task(
                self.hass, self._welcome(new_loads, added), f"{DOMAIN} new devices"
            )

    def _reread_scenes(self) -> None:
        """Ask the scene members for their actions and current scenes again: now, or on the next link."""
        for step in ("scene actions", "current scenes"):
            self.refresh.connect_steps_done.pop(step, None)
        if self.connected:
            self.entry.async_create_background_task(
                self.hass, self._read_scenes(), f"{DOMAIN} scene reads"
            )

    async def _read_scenes(self) -> None:
        await self.refresh.connect_step("scene actions", self.refresh.get_scene_actions)
        await self.refresh.connect_step(
            "current scenes", self.refresh.get_current_scenes
        )

    async def _welcome(self, loads: set[int], nodes: list[Node]) -> None:
        """Ask the loads an export added for their state, and its mains nodes for heartbeats, over the current link."""
        try:
            await self.chunked(self.refresh.state_jobs(loads))
        except ConnectionError as err:
            _LOGGER.debug("state read of the new loads aborted: %s", err)
            return
        if (
            self.heartbeats_enabled
            and any(n.pid is not None and n.pid not in BATTERY_PIDS for n in nodes)
            and (
                self.liveness.heartbeat_task is None
                or self.liveness.heartbeat_task.done()
            )
        ):
            self.liveness.configured_at = (
                None  # a round for every node: the new ones are among them
            )
            await self.liveness.configure_heartbeats()

    # ------------------------------------------------------------------ connection loop
    def visible_proxies(self) -> list[bluetooth.BluetoothServiceInfoBleak]:
        """Proxy nodes of *this* network currently advertising, strongest first.

        Those heard within PROXY_ADVERT_MAX_AGE come first (review-3 C5): a node switched off keeps its last,
        possibly strongest, advertisement in the history for a long time, and connecting to it costs a timeout.
        """
        out = [
            info
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            )
            if self.ours(info)
        ]
        now = bluetooth.MONOTONIC_TIME()
        return sorted(
            out,
            key=lambda i: (now - i.time > PROXY_ADVERT_MAX_AGE, -(i.rssi or -127)),
        )

    @callback
    def _adv_seen(
        self,
        info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        if not self.proxy.connected and self.ours(info):
            self._link_lost.set()  # wake the loop: a candidate appeared
        if (node := self.node_for_address(info.address)) is not None:
            self.node_rssi[node.unicast] = info.rssi
            self.signal_node(node.unicast)
            # its JUNG record, merged into the proxy advert's data: insert and key layout (`inserts.py`)
            self.inserts.note_advert(
                node, parse_manufacturer_data(info.manufacturer_data)
            )
        self.export_watch.check_unknown_node(info)

    def ours(self, info: bluetooth.BluetoothServiceInfoBleak) -> bool:
        """Whether a proxy advert is of *this* network (Network ID or one of our nodes' Node Identity).

        Only such an advert wakes the connection loop while unlinked (review-4 R4-8): every other network's proxies
        in range woke it before, each wake-up a `visible_proxies` pass over every advert HA holds, to find nothing.
        """
        if not (sd := info.service_data.get(MESH_PROXY_SERVICE)):
            return False
        return self.proxy.classify_service_data(bytes(sd)) is not None

    def node_for_address(self, address: str) -> Node | None:
        """Return the node advertising from Bluetooth `address` (its public MAC); None when the export has no such node."""
        return self.node_by_mac.get(address.upper())

    # ------------------------------------------------------------------ the export and the gateway (`hub/export_watch.py`)
    @property
    def unknown_nodes(self) -> dict[str, JungAdvertisement | None]:
        """The nodes of the mesh the export does not know, by MAC (`ExportWatch.unknown_nodes`)."""
        return self.export_watch.unknown_nodes

    @property
    def follows_gateway(self) -> bool:
        """Whether the entry was set up from the gateway, whose export may replace ours (`ExportWatch.follows_gateway`)."""
        return self.export_watch.follows_gateway

    @property
    def gateway_vouched(self) -> bool:
        """Whether the gateway node vouched for the entry's pin (`ExportWatch.gateway_vouched`)."""
        return self.export_watch.gateway_vouched

    async def async_follow_gateway(self) -> bool:
        """Read the gateway's address off the mesh and follow it (`ExportWatch.async_follow_gateway`)."""
        return await self.export_watch.async_follow_gateway()

    async def async_gateway_distrust(self) -> str | None:
        """Why the entry's gateway must not be used now, or None (`ExportWatch.async_gateway_distrust`)."""
        return await self.export_watch.async_gateway_distrust()

    @callback
    def async_raise_certificate_issue(self) -> None:
        """Raise the repair pointing to Reconfigure (`ExportWatch.async_raise_certificate_issue`)."""
        self.export_watch.async_raise_certificate_issue()

    @callback
    def follow_adopted_export(self) -> None:
        """Have the device model follow an export adopted from the gateway (`ExportWatch.follow_adopted_export`)."""
        self.export_watch.follow_adopted_export()

    async def _connection_loop(self) -> None:
        """Keep a link: connect to the best proxy node in range, watch it, and connect again when it goes.

        An unexpected error in one pass is logged and the loop goes on after a pause (review-3 C6: it used to end
        the task, and with it every link until a reload).
        """
        failed: dict[str, float] = {}
        # shared with `_connection_pass`, which doubles it on a failure or a short link and resets it after a long one
        backoff = [CONNECT_BACKOFF_MIN]
        while not self.stopping:
            try:
                await self._connection_pass(failed, backoff)
            except Exception:
                _LOGGER.exception(
                    "Unexpected error in the JUNG mesh connection loop; trying again in %.0f s",
                    CONNECT_BACKOFF_MAX,
                )
                await self._drop_link(
                    "an unexpected error in the connection loop", penalise=False
                )
                self._set_available(False)
                self.set_link_state(LINK_FAILED)
                await asyncio.sleep(CONNECT_BACKOFF_MAX)

    async def _connection_pass(
        self, failed: dict[str, float], backoff: list[float]
    ) -> None:
        """One pass of `_connection_loop`: wait for a candidate, connect, watch the link until it goes."""
        cands = self.visible_proxies()
        now = time.monotonic()
        cands = [
            c
            for c in cands
            if now - failed.get(c.address, -FAILED_PROXY_COOLDOWN)
            > FAILED_PROXY_COOLDOWN
        ] or cands
        if not cands:
            self._set_available(False)
            self._check_bluetooth()
            self._link_lost.clear()
            try:
                await asyncio.wait_for(self._link_lost.wait(), 30)
            except TimeoutError:
                pass
            return
        info = cands[0]
        self._report_bluetooth_unavailable(
            False
        )  # a proxy node was seen: something hears the mesh
        self.set_link_state(LINK_CONNECTING)
        try:
            await self._connect_to(info)
        except Exception as err:  # any BLE failure: try the next node
            failed[info.address] = time.monotonic()
            # the first failure of a down period is what the user gets to see (a link that never comes up —
            # no free connection slot on the ESPHome proxy, an adapter gone — would otherwise leave every
            # entity unavailable with nothing in the log); the retries are DEBUG
            _LOGGER.log(
                logging.DEBUG if self._connect_failure_logged else logging.WARNING,
                "connecting to %s failed: %s; retry in %.0fs",
                info.address,
                err,
                backoff[0],
            )
            self._connect_failure_logged = True
            self._set_available(False)
            self.set_link_state(LINK_FAILED)
            await asyncio.sleep(backoff[0])
            backoff[0] = min(backoff[0] * 2, CONNECT_BACKOFF_MAX)
            return
        self._link_lost.clear()
        await self._watch_link()
        if self._link_end is None:
            # gone without the disconnected callback (a transport that only turned `is_connected` False): the
            # client is still attached and nothing ended the link yet
            await self._drop_link("the transport reported it closed", penalise=None)
        self._set_available(False)
        await asyncio.sleep(self._judge_link(info.address, failed, backoff))

    def _judge_link(
        self, address: str, failed: dict[str, float], backoff: list[float]
    ) -> float:
        """Weigh the link to `address` that just ended (`_link_end`) against its proxy; the pause before the next pass.

        A link lost within SHORT_LINK is a failed connection that only took longer to show (review-4 R4-1): the
        back-off doubles, and after SHORT_LINK_STREAK of them in a row the node is set aside like one that cannot be
        connected to (`failed`), so the next pass prefers another node — the strongest one was otherwise picked
        again and again, each new link restarting the connect-time refresh. Only a long link resets the back-off.
        A silent proxy is set aside at once, as before; a link we ended for reasons of our own counts for nothing.
        A node that reached the streak is set aside again by its next short link, until it holds one for SHORT_LINK.
        Unverified on air.
        """
        end = self._link_end
        assert end is not None  # `_connection_pass` ended it
        if end.penalise is False:
            return 1.0
        now = time.monotonic()
        if end.penalise:  # a proxy that went silent: prefer another node for a while
            failed[address] = now
        if end.lasted >= SHORT_LINK:
            self._short_links.pop(address, None)
            backoff[0] = CONNECT_BACKOFF_MIN
            return 1.0
        streak = self._short_links[address] = self._short_links.get(address, 0) + 1
        if streak >= SHORT_LINK_STREAK:
            failed[address] = now
            if streak == SHORT_LINK_STREAK:
                _LOGGER.warning(
                    "Proxy node %s lost %d links in a row within %.0f s of connecting; preferring another node "
                    "for a while",
                    address,
                    streak,
                    SHORT_LINK,
                )
        pause = backoff[0]
        backoff[0] = min(pause * 2, CONNECT_BACKOFF_MAX)
        return pause

    async def _watch_link(self) -> None:
        """Block while the link is up; drop it (`_drop_link`) when the proxy went silent.

        A GATT proxy that stops forwarding (stuck filter, half-dead relay) never disconnects by itself. A mesh with a
        gateway is never quiet (it polls every load every 15 s), but one without can be silent for hours at night, so
        LINK_IDLE_TIMEOUT of silence only triggers a keep-alive Get (`_keep_alive`); the link is dropped when that
        goes unanswered as well. A link that went away while the keep-alive was out was lost, not silent.

        A probe a load command asked for (`_load_command`) also settles whether the nodes that left their commands
        unanswered are unreachable: only when the proxy answered it is the silence theirs.
        """
        self._unanswered.clear()  # misses of an earlier link: the new link's refresh asks those nodes again
        while self.proxy.connected and not self._link_lost.is_set():
            idle = time.monotonic() - self._last_rx
            if idle >= LINK_IDLE_TIMEOUT:
                if await self._keep_alive():
                    continue
                if self._link_lost.is_set():
                    break
                _LOGGER.warning(
                    "Nothing received from the JUNG mesh through proxy node %s for %.0f s and no answer to a "
                    "keep-alive Get; dropping the link",
                    self.proxy_address,
                    time.monotonic() - self._last_rx,
                )
                await self._drop_link("the proxy went silent", penalise=True)
                return
            if self._probe_link.is_set():
                self._probe_link.clear()
                unanswered, self._unanswered = self._unanswered, []
                # a command went unanswered: ask the proxy now rather than after LINK_IDLE_TIMEOUT of silence
                if await self._keep_alive():
                    for address, kind, asked in unanswered:
                        self.liveness.missed_answer(address, kind, asked, command=True)
                    continue
                if not self._link_lost.is_set():
                    _LOGGER.warning(
                        "No answer from the JUNG mesh through proxy node %s to a command nor to a keep-alive "
                        "Get; dropping the link",
                        self.proxy_address,
                    )
                    await self._drop_link(
                        "the proxy answered neither a command nor a keep-alive Get",
                        penalise=True,
                    )
                    return
                continue
            lost = asyncio.ensure_future(self._link_lost.wait())
            probe = asyncio.ensure_future(self._probe_link.wait())
            try:
                await asyncio.wait(
                    (lost, probe),
                    timeout=LINK_IDLE_TIMEOUT - idle,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                lost.cancel()
                probe.cancel()

    def _keep_alive_targets(self) -> list[int]:
        """One Generic OnOff Server element per node, nodes heard from before those never heard, the proxy's last.

        An answer from another node travels the mesh through the proxy, which proves it still forwards in both
        directions; the proxy node's own element only proves the GATT link (better than nothing on a mesh where
        it is the only load). Nodes that cannot answer are left out (review-3 C4: a healthy quiet link was dropped
        for asking an unplugged node, a dead one or a sleeping battery transmitter three times) — unless nothing
        else is left, when any element is better than none.
        """
        others: list[tuple[bool, int]] = []
        own: list[int] = []
        fallback: list[int] = []
        for node in self.cdb.nodes:
            if node.pid is None:
                continue  # the phone / another client: not a device
            element = next(
                (e for e in node.elements if GENERIC_ONOFF_SERVER in e.models), None
            )
            if element is None:
                continue
            fallback.append(element.address)
            if node.pid in BATTERY_PIDS or not self.node_alive(node.unicast):
                continue
            if node.unicast == self.proxy_node:
                own.append(element.address)
            else:
                others.append((node.unicast not in self.last_heard, element.address))
        others.sort(
            key=lambda target: target[0]
        )  # stable: export order within each group
        return [address for _, address in others] + own or fallback

    async def _keep_alive(self) -> bool:
        """Ask a node for its state to tell a quiet mesh from a dead link; True when the proxy delivered anything.

        A Get is the one message JUNG firmware always answers (`Refresh._refresh_all`), so an unanswered keep-alive means
        the proxy no longer forwards (or the element is gone: up to KEEP_ALIVE_ATTEMPTS distinct elements are
        tried). Traffic of any kind arriving meanwhile counts as well. A send the sequence-number store holds
        back is waited for (`while_seq_stalls`), not taken for a dead link; one that cannot go out at all (the
        store refused past SEQ_STALL_DEADLINE, the sequence space is used up) is no verdict either way, so what
        arrived since decides alone — with nothing, the watchdog drops a silent proxy as after any unanswered
        keep-alive.
        """
        before = self._last_rx
        for addr in self._keep_alive_targets()[:KEEP_ALIVE_ATTEMPTS]:
            asked = time.monotonic()
            try:
                await self.while_seq_stalls(
                    partial(
                        self.proxy.request,
                        addr,
                        M.generic_onoff_get(),
                        M.GEN_ONOFF_STATUS,
                        timeout=KEEP_ALIVE_TIMEOUT,
                        retries=1,
                    )
                )
            except TimeoutError:
                _LOGGER.debug("%04X did not answer the keep-alive Get", addr)
                # an OnOff Get, like the refresh of a switch; one attempt is no verdict on the node
                self.liveness.missed_answer(addr, "switch", asked, full=False)
                if self._last_rx != before:
                    return True  # not that element, but the proxy forwarded something else meanwhile
                continue
            except ConnectionError as err:
                _LOGGER.debug("keep-alive not sent: %s", err)
                break
            return True
        return self._last_rx != before

    def set_link_state(self, state: str) -> None:
        """Record the link's state (one of `LINK_STATES`) and tell the link state sensor when it changed."""
        if state == self.link_state:
            return
        self.link_state = state
        async_dispatcher_send(self.hass, SIGNAL_LINK_STATE.format(self.entry.entry_id))

    def _check_bluetooth(self) -> None:
        """No proxy node in range: tell a mesh out of range from a Home Assistant that has no Bluetooth left.

        The app locks its screen while the phone's Bluetooth is off (`ObserveBluetoothState`); here the equivalent
        is no connectable scanner at all — the adapter is off, unplugged or failed, every ESPHome proxy is gone —
        which raises `bluetooth_unavailable` (`_report_bluetooth_unavailable`).
        """
        off = bluetooth.async_scanner_count(self.hass, connectable=True) == 0
        self._report_bluetooth_unavailable(off)
        self.set_link_state(LINK_BLUETOOTH_OFF if off else LINK_SEARCHING)

    def _report_bluetooth_unavailable(self, off: bool) -> None:
        """Raise (or clear) the repair issue for a Home Assistant without a connectable Bluetooth scanner.

        Cleared as soon as a scanner is back or a proxy node of the mesh is seen at all.
        """
        if off == self._bluetooth_off:
            return
        self._bluetooth_off = off
        key = issue_id(self.entry, ISSUE_BLUETOOTH_UNAVAILABLE)
        if not off:
            ir.async_delete_issue(self.hass, DOMAIN, key)
            return
        _LOGGER.warning(
            "No connectable Bluetooth adapter or proxy is available: the JUNG mesh cannot be reached"
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            key,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_BLUETOOTH_UNAVAILABLE,
            learn_more_url=learn_more_url(ISSUE_BLUETOOTH_UNAVAILABLE),
            translation_placeholders={"title": self.entry.title},
        )

    async def _connect_to(self, info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Connect to the proxy node `info` advertised from and start the link's work.

        The wait for the connection is bleak-retry-connector's (up to `max_attempts` attempts of its own timeout),
        not the app's 5 s (`ConnectToDevice`): the app's phone radio connects at once or not at all, while an ESPHome
        proxy first waits for a free connection slot and gives up only after its own establishment timeout (at least
        10 s); cutting that short would turn a slow proxy into a failed one and rotate away from the only node in
        range. A failed attempt is retried by the connection loop with a back-off either way.
        """
        ble_device = (
            bluetooth.async_ble_device_from_address(
                self.hass, info.address, connectable=True
            )
            or info.device
        )
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            f"JUNG proxy {info.address}",
            disconnected_callback=self.proxy.handle_disconnected,
            max_attempts=2,
            use_services_cache=True,
        )
        self.rx_decoded_link = self.rx_undecodable_link = 0
        self.beacon_authenticated = False
        # counted before the attach: `proxy.connected` turns True inside it, and an entity added right then must
        # already see the new link's number (`config_entities.PropertyEntity._maybe_read`)
        self.link_count += 1
        await self.proxy.attach(client, beacon_wait=CONNECT_BEACON_WAIT)
        if not self.proxy.connected:
            # lost while attach() settled after the filter request (review-4 R4-3): a failed connection, not a link
            # to report as up for a moment and then as lost — the entities would flap
            raise ConnectionError("the link was lost while it was set up")
        self.previous_link = self._link_end or NO_LINK
        self._link_end = None
        self._link_since = time.monotonic()
        self.link_refresh = None
        self._held_back_at_link = self.state.held_back_total
        self._connect_failure_logged = False
        self.proxy_address = info.address
        # every node gets a full timeout from here, and no re-ask stays pending
        self.liveness.link_up()
        # the proxy's Filter Status names the node a little after the filter request (`_on_filter_status`); its
        # Bluetooth address usually names it already (JUNG nodes advertise from their MAC, `node_for_address`)
        node = self.node_for_address(info.address)
        self.proxy_node = self.proxy.proxy_addr or (
            node.unicast if node is not None else None
        )
        self.connected_since = time.time()
        self._lost_at = None
        self._cancel_grace()
        self._link_up.set()
        self._last_rx = time.monotonic()
        self._set_available(True)
        self.set_link_state(
            LINK_UPDATING
        )  # until the connect-time state refresh is through (`Refresh.after_connect`)
        self._cancel_refresh()  # a refresh still running from the previous link would keep polling through this one
        if (
            self.proxy.proxy_addr is None
        ):  # the Filter Status itself is still due, whatever the address told us
            self._unsub_filter_watch = async_call_later(
                self.hass, FILTER_STATUS_TIMEOUT, self._filter_status_overdue
            )
        self.energy.arm_poll()
        self.refresh.task = self.entry.async_create_background_task(
            self.hass, self.refresh.after_connect(), f"{DOMAIN} refresh"
        )
        self.export_watch.check_pin()
        self.export_watch.request_refresh()  # unknown nodes seen before this link (or during setup) are asked about now
        self.vault_refresh.schedule()  # a device Home Assistant added that missed a key refresh step: again now

    def _cancel_refresh(self) -> None:
        """Cancel the per-link background work: the connect-time refresh, a running energy poll, the Filter Status watchdog."""
        for task in (self.refresh.task, self.energy.task):
            if task is not None:
                task.cancel()
        self.refresh.task = self.energy.task = None
        self._cancel_filter_watch()

    def _cancel_filter_watch(self) -> None:
        if self._unsub_filter_watch is not None:
            self._unsub_filter_watch()
            self._unsub_filter_watch = None

    @callback
    def _filter_status_overdue(self, _now: datetime) -> None:
        """FILTER_STATUS_TIMEOUT after attaching, no Filter Status yet: the proxy discards our PDUs.

        The filter request is the first PDU of every link and the one the proxy answers by itself. When its
        beacon authenticated (so the keys fit) but the status never came, the proxy dropped the request as a
        replay — a stale sequence number, or another client using our address — and the link stays on the default
        whitelist: nothing at all is forwarded, so the refresh-based detection (which needs other traffic) never
        fires. Seen on air with an address whose sequence numbers the nodes already knew higher.

        Nothing to report when the request never went out — no filter request written on this link (the store held
        every attempt back) — or while the store holds sends back: the proxy was not asked, and the repair's
        skip-ahead could not be written either (`seq_store_unwritable` reports that).
        """
        self._unsub_filter_watch = None
        if (
            not self.connected
            or self.proxy.proxy_addr is not None  # the status did arrive
            or not self.beacon_authenticated
            or self.proxy.filter_writes == 0
            or self.state.stalled_for is not None
        ):
            return
        _LOGGER.warning(
            "Proxy node %s authenticated the mesh beacon but did not answer the proxy filter request within "
            "%.0f s: it discards our messages (stale sequence number, or address %04X is used by another client)",
            self.proxy_address,
            FILTER_STATUS_TIMEOUT,
            self.proxy.state.src,
        )
        self.report_pdus_dropped(True)

    def _set_available(self, available: bool) -> None:
        """Record the link state and tell the entities, unless it is "still unavailable" with nothing to clear.

        The no-proxy branch of the connection loop passes every 30 s for as long as the mesh is out of range: a
        signal each time would make every entity write its state twice a minute, for hours. A (re)connect always
        signals: the proxy the entities show may have changed.
        """
        changed = (
            available
            or available != self._was_available
            or self.proxy_address is not None
            or self.proxy_node is not None
        )
        if available != self._was_available:
            if available:
                _LOGGER.info(
                    "Connected to the JUNG mesh through proxy node %s",
                    self.proxy_address,
                )
            else:
                _LOGGER.warning(
                    "Lost the connection to the JUNG mesh; reconnecting to another proxy node"
                )
            self._was_available = available
        if not available:
            self.proxy_address = None
            self.proxy_node = None
            self.connected_since = None
            self._link_up.clear()
        if changed:
            async_dispatcher_send(
                self.hass, SIGNAL_CONNECTION.format(self.entry.entry_id)
            )

    def _on_disconnect(self) -> None:
        """Handle the link the proxy client lost (its transport's disconnected callback)."""
        self._link_ended("the proxy disconnected", None)
        self._link_lost.set()

    async def _drop_link(self, reason: str, *, penalise: bool | None) -> None:
        """End the current link ourselves, for `reason`; `penalise` as in `LinkEnd`.

        Every path that detaches a link it still had goes through here (review-4 R4-3: only a transport's
        disconnect used to start the link-loss grace, so a link the watchdog or the `pdus_dropped` repair dropped
        made every entity unavailable at once, and Home Assistant skipped them in a command meanwhile). The end is
        recorded — and the grace started — before the detach: `detach` clears `connected` at once and then waits
        for the transport, and a command arriving in that wait must find the grace and wait for the next link
        (`_wait_for_link`) rather than fail. The detach is bounded like the one of `async_stop`. Unverified on air.
        """
        self._link_ended(reason, penalise)
        try:
            await asyncio.wait_for(self.proxy.detach(), STOP_TIMEOUT)
        except TimeoutError:
            _LOGGER.warning(
                "The Bluetooth link did not close within %.0f s; leaving it",
                STOP_TIMEOUT,
            )
        except Exception:  # pragma: no cover - detach logs and swallows its own errors
            _LOGGER.debug("detach failed", exc_info=True)
        self._link_lost.set()  # the watchdog, when another task dropped the link

    def _link_ended(self, reason: str, penalise: bool | None) -> None:
        """Handle the end of a link, whoever ended it: record why (`link_history`), start the grace, tell the listeners.

        Nothing to do when no link is up: one lost while `attach()` was still settling is a failed connection
        (`_connect_to`), and a link ends once however many paths notice.
        """
        if self._link_end is not None:
            return
        now = time.monotonic()
        end = self._link_end = LinkEnd(reason, penalise, now - self._link_since)
        self.link_history.append(
            LinkRecord(
                self.proxy_node,
                now,
                end.lasted,
                reason,
                penalise,
                self.link_refresh,
                self.state.held_back_total - self._held_back_at_link,
            )
        )
        self._cancel_refresh()
        self._start_grace()
        self.set_link_state(LINK_DISCONNECTED)
        _LOGGER.info(
            "The link through proxy node %s ended after %.0f s: %s",
            self.proxy_address,
            end.lasted,
            reason,
        )
        for listener in list(self._link_loss_listeners):
            listener(end)

    @callback
    def async_on_link_loss(self, listener: Callable[[LinkEnd], None]) -> CALLBACK_TYPE:
        """Call `listener` with every link's `LinkEnd` the moment it ends, before the grace runs; returns the unsubscribe."""
        self._link_loss_listeners.append(listener)
        return partial(self._link_loss_listeners.remove, listener)

    def _cancel_grace(self) -> None:
        if self._unsub_grace is not None:
            self._unsub_grace()
            self._unsub_grace = None

    def _start_grace(self) -> None:
        """Keep the entities available for LINK_LOSS_GRACE after a link loss; tell them when it ends."""
        self._lost_at = time.monotonic()
        self._cancel_grace()

        @callback
        def ended(_now: datetime) -> None:
            self._unsub_grace = None
            async_dispatcher_send(
                self.hass, SIGNAL_CONNECTION.format(self.entry.entry_id)
            )

        self._unsub_grace = async_call_later(self.hass, LINK_LOSS_GRACE, ended)

    def _on_filter_status(self, proxy_unicast: int) -> None:
        """Record which node of the mesh we talk through, now that the proxy's Filter Status named it.

        The status is the proxy talking to us, so it feeds the link watchdog like any decoded PDU; and the proxy
        accepted the request it answers, so our PDUs are not being discarded (any more).
        """
        self._last_rx = time.monotonic()
        self._cancel_filter_watch()
        if self._pdus_dropped:
            self.report_pdus_dropped(False)
        if self.proxy_node == proxy_unicast:
            return
        self.proxy_node = proxy_unicast
        _LOGGER.debug("Proxy %s is mesh node %04X", self.proxy_address, proxy_unicast)
        async_dispatcher_send(self.hass, SIGNAL_CONNECTION.format(self.entry.entry_id))

    def scenes_of(self, addr: int) -> list[int]:
        """Return the scenes the load element at `addr` is a member of (the app's `GetScenesForDevice`).

        The export lists the element each scene was stored on (the node's first Scene Setup Server, shared by the
        channels of a two-channel node); a channel with its own scene list (Scene Action Setup) narrows that to the
        scenes it lists, once the list was read.
        """
        element = self.cdb.element(addr)
        if element is None:
            return []
        store = next(
            (
                e.address
                for e in element.node.elements
                if SCENE_SETUP_SERVER in e.models
            ),
            None,
        )
        numbers = [
            n for n, members in sorted(self.cdb.scenes.items()) if store in members
        ]
        if addr in self.scene_lists_read:
            numbers = [n for n in numbers if addr in self.scene_actions.get(n, {})]
        return numbers

    @callback
    def note_scene_recall(self, number: int) -> bool:
        """Note that EVENT_SCENE_RECALLED fires for `number` now; False when it fired within SCENE_RECALL_WINDOW.

        The members' Scene Status publications follow every recall: this is what keeps them from repeating the
        event of a recall that was already reported.
        """
        now = time.monotonic()
        last = self._scene_recalls.get(number)
        self._scene_recalls[number] = now
        return last is None or now - last >= SCENE_RECALL_WINDOW

    def scene_action_channels(self, addr: int) -> list[int]:
        """Return the elements of `addr`'s node with a Scene Action Setup server: the channels its scenes act on."""
        element = self.cdb.element(addr)
        if element is None:
            return []
        return [
            e.address for e in element.node.elements if SCENE_ACTION_SETUP in e.models
        ]

    async def chunked(self, jobs: Sequence[Callable[[], Awaitable[object]]]) -> None:
        """Run the jobs REFRESH_CHUNK at a time with a short pause in between (as the app does).

        Each job is retried while the sequence-number store holds sends back (`while_seq_stalls`).
        """
        for i in range(0, len(jobs), REFRESH_CHUNK):
            if i:
                await asyncio.sleep(0.5)
            await asyncio.gather(
                *(self.while_seq_stalls(job) for job in jobs[i : i + REFRESH_CHUNK])
            )

    @callback
    def _seq_stall_started(self) -> None:
        """Look again SEQ_STALL_ISSUE_AFTER after the store began holding sends back (`HAState.reserve_seq`)."""
        if self._unsub_seq_stall is not None:
            self._unsub_seq_stall()  # an earlier stall's, which ended meanwhile
        self._unsub_seq_stall = async_call_later(
            self.hass, SEQ_STALL_ISSUE_AFTER, self._seq_stall_overdue
        )

    @callback
    def _seq_stall_overdue(self, _now: datetime) -> None:
        self._unsub_seq_stall = None
        self.state.report_unwritable()

    async def while_seq_stalls[T](self, send: Callable[[], Awaitable[T]]) -> T:
        """Run `send`, again every SEQ_STALL_RETRY seconds while the sequence-number store holds it back, for a while.

        `HAState.reserve_seq` refuses numbers while the store's last save has not landed (`SequenceStalled`, a
        `ConnectionError`): back-pressure, not a lost link. Taken as one, it used to end the whole connect-time
        sequence at its first send — no Time Set, location, energy, heartbeats, scene actions or faults on that
        link — and a keep-alive refused that way dropped a working link. Once the link is gone, or the store has
        refused for SEQ_STALL_DEADLINE (it is not catching up: `seq_store_unwritable` says why), the refusal is
        raised like any lost link. Real exhaustion (plain `SequenceExhausted`: the 24-bit space used up, or a newer
        hub owns the numbers) is raised at once — retried, it held the link watchdog's keep-alive forever, so a
        silent proxy was never dropped.
        """
        waited = 0.0  # summed rather than read off the clock: the retries are what is bounded
        while True:
            try:
                return await send()
            except SequenceStalled as err:
                if (
                    isinstance(err, AddressShared)  # waits for the user, not the store
                    or not self.connected
                    or waited >= SEQ_STALL_DEADLINE
                ):
                    raise
                _LOGGER.debug(
                    "send held back (%s); trying again in %.0f s", err, SEQ_STALL_RETRY
                )
                await asyncio.sleep(SEQ_STALL_RETRY)
                waited += SEQ_STALL_RETRY

    async def async_refresh_element(
        self, addr: int, kind: str, *, quiet: bool = False
    ) -> None:
        """Ask one load for its state now; best effort (`Refresh.async_refresh_element`)."""
        await self.refresh.async_refresh_element(addr, kind, quiet=quiet)

    async def async_wait_settled(self, addr: int, kind: str) -> bool:
        """Ask a load for its state until it answers with no transition left (present = target); True once it does.

        A state-changing Set is unacknowledged and answered by a group publication at most, and a dimmer ramps to
        its new level: what a Scene Store would record is the present state, so it must have arrived first.
        Only an answer counts — a cached state from before the Set says nothing — and only one showing the Set
        took effect (`_took_effect`): a Get the load answered before it got to the Set (or after losing it) shows
        the old state at rest. `kind` picks the Get (`STATE_GETS`, `level` for a blind or thermostat).

        At most SETTLE_ATTEMPTS Gets within SETTLE_TIMEOUT: a load that does not answer at all costs one deadline,
        not a full timeout per Get, and its silence is logged at DEBUG (as every unanswered attempt) — the caller reports it.
        """
        get, status = STATE_GETS.get(kind, ONOFF_GET)
        try:
            async with asyncio.timeout(SETTLE_TIMEOUT):
                for attempt in range(SETTLE_ATTEMPTS):
                    if attempt:
                        await asyncio.sleep(SETTLE_PAUSE)
                    try:
                        await self.proxy.request(addr, get(), status, retries=1)
                    except TimeoutError:
                        continue
                    st = self.states.get(addr)
                    if (
                        st is not None
                        and _settled(st, kind)
                        and self._took_effect(addr, st)
                    ):
                        return True
        except TimeoutError:
            _LOGGER.debug("%04X did not settle within %.0f s", addr, SETTLE_TIMEOUT)
        return False

    def _note_request(self, addr: int, **wanted: bool | int) -> None:
        """Remember the values a state Set is about to ask of `addr`, and the element's cached ones before it."""
        st = self.states.get(addr)
        self._requested[addr] = (
            wanted,
            {name: None if st is None else getattr(st, name) for name in wanted},
        )

    def _took_effect(self, addr: int, st: ElementState) -> bool:
        """Whether the state shows the element's last state Set (`_note_request`) applied.

        Its values are the requested ones, within a step of the load's own (SETTLE_SLACK), or at least no longer
        the ones cached before the Set: a load clamps a lightness to its range, and the scene records what it
        reports. A value not known before the Set cannot tell either way, nor can an element never set through
        the hub.
        """
        if addr not in self._requested:
            return True
        wanted, before = self._requested[addr]
        present = {name: getattr(st, name) for name in wanted}
        near = all(
            value is not None and abs(value - wanted[name]) <= SETTLE_SLACK.get(name, 0)
            for name, value in present.items()
        )
        return near or present != before or None in before.values()

    # ------------------------------------------------------------------ energy and time (`hub/energy.py`, `hub/clock.py`)
    async def async_refresh_meter(self, load: MeteredLoad) -> None:
        """Read one metered load's readings and counters now (`Energy.async_refresh_meter`)."""
        await self.energy.async_refresh_meter(load)

    async def reset_consumption(self, load: MeteredLoad) -> None:
        """Zero a metered load's resettable counters, as the app does (`Energy.reset_consumption`)."""
        await self.energy.reset_consumption(load)

    async def async_send_time(self, destination: int = ALL_NODES) -> None:
        """Broadcast Time Set now; without a link nothing goes out (`Clock.async_send_time`)."""
        await self.clock.async_send_time(destination)

    # ------------------------------------------------------------------ Configuration Server audit
    async def async_audit(self, nodes: Sequence[Node]) -> list[NodeAudit]:
        """Compare the nodes' Configuration Servers with the export (`jhmesh.audit`: device-key Gets, never a Set).

        One node after the other, its Gets paced like the connect-time refresh (`chunked`: REFRESH_CHUNK at a
        time, each retried while the sequence-number store holds sends back); a node that stays silent is a result,
        not an error.
        Each node's result is kept for the diagnostics. A lost link raises `ConnectionError`.
        """
        exchange = client_exchange(
            self.proxy, timeout=AUDIT_TIMEOUT, retries=AUDIT_RETRIES
        )
        results = []
        for node in nodes:
            result = await audit_node(exchange, node, run=self.chunked)
            self.audits[node.unicast] = result
            results.append(result)
        return results

    # ------------------------------------------------------------------ the locator (Node Identity)
    async def async_locate(self, node: Node, seconds: float) -> C.NodeIdentityStatus:
        """Have `node` advertise its Node Identity, and ask it to stop after `seconds` (review-4 F4-15).

        A Config Node Identity Set (device key, like the audit's Gets) to the node's primary unicast: the node then
        advertises the Mesh Proxy service with its Node Identity — a hash only the mesh's keys resolve to this node
        — instead of the Network ID, so a scanner tells its radio from the others. The Set off follows after
        `seconds` (a second call restarts the count). The node stops by itself after 60 s at most (Mesh Profile
        §7.2.2.2.3), so an off lost on the way, or never sent because the entry stopped, leaves nothing running. A
        node that answers *not supported* or an error status gets no off. Unanswered raises TimeoutError, no link
        ConnectionError. Unverified on air.
        """
        status = await self._node_identity(node, running=True)
        if status.ok and status.identity == C.NODE_IDENTITY_RUNNING:
            if (cancel := self._locating.pop(node.unicast, None)) is not None:
                cancel()

            @callback
            def stop(_now: datetime) -> None:
                self._locating.pop(node.unicast, None)
                self.entry.async_create_background_task(
                    self.hass,
                    self._stop_locating(node),
                    f"{DOMAIN} locate {node.unicast:04X} off",
                )

            self._locating[node.unicast] = async_call_later(self.hass, seconds, stop)
        return status

    async def _stop_locating(self, node: Node) -> None:
        """Send the Node Identity Set off; the node stops by itself anyway, so a failure is only logged."""
        try:
            await self._node_identity(node, running=False)
        except (TimeoutError, ConnectionError, OSError) as err:
            _LOGGER.debug(
                "%04X: Node Identity not switched off (%r); it stops by itself",
                node.unicast,
                err,
            )

    async def _node_identity(
        self, node: Node, *, running: bool
    ) -> C.NodeIdentityStatus:
        """Send a Node Identity Set for the primary NetKey and return the Node Identity Status that answers it."""

        def decodes(message: AccessMessage) -> bool:
            try:
                C.decode_config(message.opcode, message.params)
            except ValueError:
                return False
            return True

        reply = await self.proxy.request_config(
            node.unicast,
            C.node_identity_set(running),
            C.CONFIG_NODE_IDENTITY_STATUS,
            timeout=AUDIT_TIMEOUT,
            retries=AUDIT_RETRIES,
            match=decodes,
        )
        status = C.decode_config(reply.opcode, reply.params)
        assert isinstance(status, C.NodeIdentityStatus)  # what the opcode decodes to
        return status

    # ------------------------------------------------------------------ liveness (`hub/liveness.py`)
    @property
    def heartbeats_enabled(self) -> bool:
        """Whether the heartbeat option is on (`Liveness.heartbeats_enabled`)."""
        return self.liveness.heartbeats_enabled

    @property
    def heartbeats(self) -> dict[int, Heartbeat]:
        """Each node's last Heartbeat, by node unicast (`Liveness.heartbeats`)."""
        return self.liveness.heartbeats

    @property
    def unreachable(self) -> set[int]:
        """The nodes that left a full-budget request unanswered (`Liveness.unreachable`)."""
        return self.liveness.unreachable

    @property
    def last_heard(self) -> dict[int, float]:
        """When each node was last heard from, monotonic, by node unicast (`Liveness.last_heard`)."""
        return self.liveness.last_heard

    @property
    def heartbeat_nodes(self) -> list[Node]:
        """The nodes asked for heartbeats (`Liveness.heartbeat_nodes`)."""
        return self.liveness.heartbeat_nodes

    @property
    def heartbeat_timeout(self) -> float:
        """Seconds without a beat after which a node counts as dead (`Liveness.heartbeat_timeout`)."""
        return self.liveness.heartbeat_timeout

    def node_alive(self, address: int) -> bool:
        """Whether the node owning element `address` counts as there (`Liveness.node_alive`)."""
        return self.liveness.node_alive(address)

    def heartbeat_age(self, node: Node) -> float | None:
        """Seconds since the node's last Heartbeat (`Liveness.heartbeat_age`)."""
        return self.liveness.heartbeat_age(node)

    def load_locked(self, address: int) -> bool:
        """Whether the load at `address` last reported a lock that has not run out (`ElementState.locked`)."""
        st = self.states.get(address)
        return st is not None and st.locked

    def signal_node(self, unicast: int, *, force: bool = False) -> None:
        """Tell the node's diagnostic entities, at most once per NODE_DIAGNOSTICS_INTERVAL unless `force`d."""
        now = time.monotonic()
        if (
            not force
            and now - self._node_signalled.get(unicast, -NODE_DIAGNOSTICS_INTERVAL)
            < NODE_DIAGNOSTICS_INTERVAL
        ):
            return
        self._node_signalled[unicast] = now
        async_dispatcher_send(
            self.hass, SIGNAL_NODE.format(self.entry.entry_id, unicast)
        )

    def _note_seq(self, src: int, seq: int) -> None:
        """Follow the source's sequence numbers; a jump into a fresh block of the counter is a restart (review-3 F8).

        JUNG firmware continues from the next RESTART_BLOCK after a restart. Only a jump that lands within
        RESTART_SLACK of a block start counts, and only from a number that was not about to reach that block
        anyway — the counter running on into the next block is no restart. The replay protection has already
        accepted the message, so the number only ever grows within one IV index; a lower one (a new IV index)
        just restarts the tracking.
        """
        last = self._last_seq.get(src)
        self._last_seq[src] = seq
        if last is None or seq <= last:
            return
        if (
            seq // RESTART_BLOCK > last // RESTART_BLOCK
            and seq % RESTART_BLOCK < RESTART_SLACK
            and last % RESTART_BLOCK < RESTART_BLOCK - RESTART_SLACK
        ):
            node = self.cdb.node_by_addr(src)
            if node is None or (
                node.unicast in self.restarted
                and (dt_util.utcnow() - self.restarted[node.unicast])
                < timedelta(seconds=NODE_DIAGNOSTICS_INTERVAL)
            ):
                return  # another element of the same node, restarted with it
            self.restarted[node.unicast] = dt_util.utcnow()
            _LOGGER.info(
                "%s restarted (its sequence number jumped from %06X to %06X)",
                node.name,
                last,
                seq,
            )
            self.signal_node(node.unicast, force=True)

    def highest_seq(self) -> tuple[int, int] | None:
        """Return (source, sequence number) of the source furthest into the current IV index's space, us included."""
        iv = self.proxy.state.iv_index
        sources = [
            (seq, src)
            for src, (entry_iv, seq) in self.proxy.state.rpl.items()
            if entry_iv == iv
        ]
        if self.proxy.state.tx_iv_index == iv:
            sources.append((self.proxy.state.seq, self.proxy.state.src))
        if not sources:
            return None
        seq, src = max(sources)
        return src, seq

    @callback
    def _check_sequence_space(self, _now: datetime | None = None) -> None:
        """Raise `sequence_space_low` while a source is past SEQUENCE_SPACE_WARN (review-3 N2); clear it after.

        Every source (node, app, Home Assistant) stops sending at the end of the 24-bit space of the current IV
        index; the IV Update that resets it is started by the gateway (Home Assistant only follows one). The
        numbers are those the replay protection accepted, so they are what the mesh really used.
        """
        highest = self.highest_seq()
        key = issue_id(self.entry, ISSUE_SEQUENCE_SPACE_LOW)
        if highest is None or highest[1] < SEQUENCE_SPACE_WARN:
            ir.async_delete_issue(self.hass, DOMAIN, key)
            return
        src, seq = highest
        node = self.cdb.node_by_addr(src)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            key,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SEQUENCE_SPACE_LOW,
            learn_more_url=learn_more_url(ISSUE_SEQUENCE_SPACE_LOW),
            translation_placeholders={
                "title": self.entry.title,
                "source": f"{node.name} {src:04X}"
                if node is not None
                else f"{src:04X}",
                "percent": f"{100 * seq // 0xFFFFFF}",
                "iv_index": str(self.proxy.state.iv_index),
            },
        )

    # ------------------------------------------------------------------ incoming messages
    def element_state(self, addr: int) -> ElementState:
        """Return the cached state of the element at `addr`, creating an empty one on first contact."""
        return self.states.setdefault(addr, ElementState())

    @callback
    def notify_update(self, addr: int) -> None:
        """Stamp the element's cached state and tell its entities to re-read it."""
        self.element_state(addr).updated = time.monotonic()
        async_dispatcher_send(
            self.hass, SIGNAL_UPDATE.format(self.entry.entry_id, addr)
        )

    def _on_message(self, m: AccessMessage) -> None:
        """Account for the traffic (link watchdog, drop detection, stale-export detection, the app), then hand the message to its handler."""
        self._last_rx = time.monotonic()
        self.rx_messages += 1
        self.rx_decoded_link += 1
        self._note_seq(m.src, m.seq)
        self.liveness.heard_from(m.src)
        if self.heartbeats_enabled:
            self.liveness.mark_alive(m.src)  # any message is as good as a heartbeat
        if self._export_stale:
            self._report_export_stale(
                False
            )  # our keys opened something: the export fits the mesh after all
        if m.dst == self.proxy.state.src:
            self.rx_to_us += 1
            if self._pdus_dropped:
                self.report_pdus_dropped(
                    False
                )  # a node answered us: our PDUs get through again
        if self.app_follow is not None:
            self.app_follow.note(m)
        handler = STATUS_HANDLERS.get((m.company_id, m.opcode))
        if handler is not None:
            handler(self, m, m.params)

    # The three load statuses come as `[present][target][remaining]` while the element is in a transition and as
    # `[present]` alone otherwise. The entities show the *present* field, as the JUNG HOME app does
    # (`docs/gap-analysis/control-and-state.md` §1.2): it is what the light is doing now, and a rocker hold fading a
    # dimmer reports present ≠ target for seconds (the target being the end of the fade), which HA would otherwise
    # render as an instant jump. Our own commands carry no transition unless one is asked for, and JUNG's firmware
    # confirms them by publishing the final state, so nothing is lost by not showing the target; it is kept in
    # `ElementState.target_*`. After a Set with a transition the state is read again when it is over
    # (`_reread_after_transition`).

    @register_status_handler(M.GEN_ONOFF_STATUS)
    def _on_onoff_status(self, m: AccessMessage, p: bytes) -> None:
        """Store a Generic OnOff Status `[present u8]` or `[present u8][target u8][remaining u8]`: present is the state.

        A load that is on, heading off with a known remaining time, also sets `off_at` (the *Switches off at*
        sensor); any other Status clears it. Whether a JUNG load with a run-on time (`0x1007`) reports the time left
        this way is unverified on air (review-4 F4-11): the app ignores the field, and no capture showed it yet.
        """
        if not p:
            return
        st = self.element_state(m.src)
        st.on = bool(p[0])
        st.target_on = bool(p[1]) if len(p) >= 3 else st.on
        remaining = M.remaining_time(M.GEN_ONOFF_STATUS, p)
        st.off_at = (
            dt_util.utcnow() + timedelta(seconds=remaining)
            if st.on and not st.target_on and remaining
            else None
        )
        self.notify_update(m.src)

    @register_status_handler(M.LIGHT_LIGHTNESS_STATUS)
    def _on_lightness_status(self, m: AccessMessage, p: bytes) -> None:
        """Store a Light Lightness Status `[present u16]` or `[present u16][target u16][remaining u8]`: present is the state."""
        if len(p) < 2:
            return
        st = self.element_state(m.src)
        st.lightness = int.from_bytes(p[:2], "little")
        st.target_lightness = (
            int.from_bytes(p[2:4], "little") if len(p) >= 5 else st.lightness
        )
        st.on = st.lightness > 0
        self.notify_update(m.src)

    @register_status_handler(M.LIGHT_CTL_STATUS)
    def _on_ctl_status(self, m: AccessMessage, p: bytes) -> None:
        """Store a Light CTL Status `[lightness u16][temperature u16]` (+ `[target l][target t][remaining]`): present is the state."""
        if len(p) < 4:
            return
        st = self.element_state(m.src)
        st.lightness = int.from_bytes(p[:2], "little")
        st.kelvin = int.from_bytes(p[2:4], "little")
        if len(p) >= 9:
            st.target_lightness = int.from_bytes(p[4:6], "little")
            st.target_kelvin = int.from_bytes(p[6:8], "little")
        else:
            st.target_lightness, st.target_kelvin = st.lightness, st.kelvin
        st.on = st.lightness > 0
        self.notify_update(m.src)

    @register_status_handler(M.LIGHT_CTL_TEMP_STATUS)
    def _on_ctl_temperature_status(self, m: AccessMessage, p: bytes) -> None:
        """Store a Light CTL Temperature Status `[temperature u16][delta UV s16]` (+ `[target t][target uv][remaining]`).

        It comes from the light's temperature element (the CTL Temperature Server, the element after the CTL
        Server's), so it lands on the light's state (`_ctl_light_of`). A colour temperature changed elsewhere (the
        gateway polls a CTL Temperature Get) may only ever come as this one: unverified on air, so it is taken
        defensively — only the temperature, never the lightness or on/off, and nothing outside the spec's range.
        """
        if len(p) < 4 or (light := self._ctl_light_of(m.src)) is None:
            return
        kelvin = int.from_bytes(p[:2], "little")
        target = int.from_bytes(p[4:6], "little") if len(p) >= 9 else kelvin
        if not (
            CTL_KELVIN_MIN <= kelvin <= CTL_KELVIN_MAX
            and CTL_KELVIN_MIN <= target <= CTL_KELVIN_MAX
        ):
            return
        st = self.element_state(light)
        st.kelvin, st.target_kelvin = kelvin, target
        self.notify_update(light)

    def _ctl_light_of(self, addr: int) -> int | None:
        """Return the CTL light whose temperature element is `addr` (or which `addr` is); None for anything else.

        The pairing is the device model's (`Light.temperature_address`), the one `set_ctl_temperature` sends to.
        """
        device = self.devices.by_address.get(addr)
        if isinstance(device, Light):
            return addr if device.kind == "ctl" else None
        light = self.devices.by_temperature(addr)
        return None if light is None else light.address

    @register_status_handler(M.GEN_LEVEL_STATUS)
    def _on_level_status(self, m: AccessMessage, p: bytes) -> None:
        """Store a Generic Level Status `[present s16]` or `[present s16][target s16][remaining u8]`: present is the state.

        A blind's position and slat elements answer with it (the app reads the level the same way, ignoring the
        remaining time); the target is kept so the cover can tell an opening from a closing blind while they differ.
        """
        if len(p) < 2:
            return
        st = self.element_state(m.src)
        st.level = int.from_bytes(p[:2], "little", signed=True)
        st.target_level = (
            int.from_bytes(p[2:4], "little", signed=True) if len(p) >= 5 else st.level
        )
        self.notify_update(m.src)

    @register_status_handler(M.LIGHT_CTL_TEMP_RANGE_STATUS)
    def _on_ctl_range_status(self, m: AccessMessage, p: bytes) -> None:
        """Store a Light CTL Temperature Range Status `[status u8][min K u16][max K u16]`: the light's own slider limits.

        Only a success status (0) carries a range; a range that is empty or inverted is not one either. The Status
        is also the colour-temperature range setup state (`config_entities.CTL_TEMPERATURE_RANGE`, the *White range*
        numbers), cached raw in `ElementState.setup` here: a message type has one handler.
        """
        if len(p) < 5 or p[0] != 0:
            return
        kelvin_min, kelvin_max = (
            int.from_bytes(p[1:3], "little"),
            int.from_bytes(p[3:5], "little"),
        )
        if not 0 < kelvin_min <= kelvin_max:
            return
        st = self.element_state(m.src)
        st.kelvin_min, st.kelvin_max = kelvin_min, kelvin_max
        st.setup[M.LIGHT_CTL_TEMP_RANGE_STATUS] = bytes(p[:5])
        self.notify_update(m.src)

    @register_status_handler(*SIG_PROPERTY_STATUS_OPCODES)
    def _on_sig_property_status(self, m: AccessMessage, p: bytes) -> None:
        """Store a SIG Generic Property Status `[pid u16][user access u8][value]`: a socket counter `Energy.poll` reads.

        The value is decoded with the property's codec from `properties.SIG_PROPERTIES` (an LE counter of whatever
        length the firmware sends, 3 or 4 bytes on air); the GSS "not known" / "not valid" markers (all-ones, all-ones
        minus one — the firmware's "unknown") decode to None and clear the field. A counter answered by the socket's meter element lands on the socket's own state, where its
        sensors read it. Other SIG properties are not modelled here: a status carrying one is ignored — so is a
        status without a value, the id alone or the id and the access byte, which is how an element reports a
        property it does not have (`0x006A` asked of the socket's main element instead of its meter element,
        `docs/hidden-features.md` §2): the app's resolver drops it too and keeps the last value
        (`StatusMessageResolver` `AbstractC1929k1`, `UtilsKt.k`).

        The exceptions are the node's identity (`NODE_INFO`: the software version 0x001A, the hardware revision
        0x0010 and the manufacturer name 0x0011, which `PropertyReader.schedule_version` asks for): kept raw by
        `remember_node_info` for the device registry and the next hub of the entry, the software version on the
        answering element too, where `config_entities.node_version` reads it.
        """
        if len(p) == 2 and int.from_bytes(p, "little") == PROPERTY_PRECISE_TOTAL_ENERGY:
            self._lacks_precise_energy(m.src)
        if len(p) < 3:
            return
        pid = int.from_bytes(p[:2], "little")
        if pid in NODE_INFO and len(p) > 3:
            raw = bytes(p[3:])
            if pid == SIG_SOFTWARE_VERSION:
                self.element_state(m.src).properties[pid] = raw
            self.remember_node_info(m.src, NODE_INFO[pid], raw)
            self.notify_update(m.src)
            return
        if pid not in COUNTER_FIELDS or len(p) == 3:
            return
        decoded = SIG_PROPERTIES[pid].codec.decode(p[3:])
        load = self.devices.by_meter(m.src)
        addr = load.address if load is not None else m.src
        setattr(self.element_state(addr), COUNTER_FIELDS[pid], decoded)
        self.notify_update(addr)

    def node_info(self, unicast: int) -> dict[str, bytes]:
        """Return what the node at `unicast` told about itself (`NODE_VERSIONS`): raw values by item name."""
        return (
            self.hass.data.get(NODE_VERSIONS, {})
            .get(self.entry.entry_id, {})
            .get(unicast, {})
        )

    def remember_node_info(self, unicast: int, name: str, raw: bytes) -> None:
        """Keep one item of what the node at `unicast` told about itself; a change is saved and shown on its device."""
        nodes = self.hass.data.setdefault(NODE_VERSIONS, {}).setdefault(
            self.entry.entry_id, {}
        )
        info = nodes.setdefault(unicast, {})
        if info.get(name) == raw:
            return
        info[name] = raw
        if name == NODE_INFO_TIME_ROLE:
            self._report_time_keeper()
        node_versions_store(self.hass, self.entry.entry_id).async_delay_save(
            lambda: {
                f"{a:04X}": {k: v.hex() for k, v in items.items()}
                for a, items in nodes.items()
            },
            NODE_VERSIONS_SAVE_DELAY,
        )
        if (node := self.cdb.node_by_addr(unicast)) is not None:
            update_node_device(self.hass, self, node)

    @callback
    def _report_time_keeper(self) -> None:
        """Raise the `time_keeper_missing` repair while the project has PP2 pucks and no node keeps their time (F4-14).

        The app elects a time keeper itself whenever a PP2 puck is in the project (`EnsureTimeKeeper`,
        network-logic.md §6.2); Home Assistant leaves the choice to the user (`switch.JungHomeTimeKeeper`). Raised
        only once every node that could keep the time (`devices.time_keeper_candidates`) answered its time role (the
        connect-time Time Role Get): a role not asked yet is no evidence. One that answered relay or authority keeps
        it. Names the pucks' addresses. Unverified on air: no puck in the installation.
        """
        issue = issue_id(self.entry, ISSUE_TIME_KEEPER_MISSING)
        pucks = sorted(n.unicast for n in self.cdb.nodes if n.pid in PP2_PIDS)
        roles = [
            self.node_info(n.unicast).get(NODE_INFO_TIME_ROLE)
            for n in time_keeper_candidates(self.cdb)
        ]
        known = [role[0] for role in roles if role]
        if not pucks or len(known) < len(roles) or set(known) & TIME_KEEPER_ROLES:
            ir.async_delete_issue(self.hass, DOMAIN, issue)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TIME_KEEPER_MISSING,
            learn_more_url=learn_more_url(ISSUE_TIME_KEEPER_MISSING),
            translation_placeholders={
                "title": self.entry.title,
                "pucks": ", ".join(f"{a:04X}" for a in pucks),
            },
        )

    def _lacks_precise_energy(self, src: int) -> None:
        """Fall back to 0x006A for the *Energy* of a load other than a socket whose meter `src` says it has no 0x0072.

        The id alone is how a JUNG element answers for a property it does not have (the socket's main element asked
        for 0x006A, `docs/hidden-features.md` §2). A socket never falls back (`ElementState.energy_total`).
        """
        load = self.devices.by_meter(src)
        if load is None or isinstance(load, Socket):
            return
        st = self.element_state(load.address)
        if not st.energy_fallback:
            st.energy_fallback = True
            self.notify_update(load.address)

    @register_status_handler(M.HEALTH_FAULT_STATUS)
    def _on_health_fault_status(self, m: AccessMessage, p: bytes) -> None:
        """Store a Health Fault Status `[test id][company id u16][fault ids…]`: the node's registered faults.

        Only the *registered* faults are kept (what the Fault Get asks for). A Health Current Status (0x04, the faults
        present now, published only when someone configures the Health Server's publication) has no handler: on
        JUNG nodes it reports none while the register holds 0x81, and mixing the two would flap the entity.
        """
        try:
            status = M.decode_health_fault_status(p)
        except ValueError as err:
            _LOGGER.debug("%04X: malformed Health Fault Status: %s", m.src, err)
            return
        st = self.element_state(m.src)
        st.faults = status.faults
        self.notify_update(m.src)

    @register_status_handler(M.TIME_STATUS)
    def _on_time_status(self, m: AccessMessage, p: bytes) -> None:
        """Keep a node's Time Status (`NodeClocks.note_time`): the answer to Time Set or Time Get, or a published one."""
        self.clocks.note_time(m.src, p)

    @register_status_handler(M.TIME_ZONE_STATUS)
    def _on_time_zone_status(self, m: AccessMessage, p: bytes) -> None:
        """Keep a node's Time Zone Status (`NodeClocks.note_zone`)."""
        self.clocks.note_zone(m.src, p)

    @register_status_handler(M.GEN_LOCATION_GLOBAL_STATUS)
    def _on_location_global_status(self, m: AccessMessage, p: bytes) -> None:
        """Keep the location a node stores for its astro schedules (`NodeClocks.note_location`)."""
        self.clocks.note_location(m.src, p)

    @register_status_handler(M.SENSOR_STATUS)
    def _on_sensor_status(self, m: AccessMessage, p: bytes) -> None:
        """Store the readings of a metered load's meter element on the load's own element.

        Each value goes through its property's codec (`properties.SIG_PROPERTIES`), so the GSS "value is not
        known" marker (all-ones: Power 0xFFFFFF, Current / Voltage 0xFFFF) clears the reading instead of showing
        1.6 MW; a truncated status is dropped.
        """
        if (load := self.devices.by_meter(m.src)) is None:
            return
        try:
            values = M.sensor_values(p)
        except ValueError as err:
            _LOGGER.debug("%04X: malformed Sensor Status: %s", m.src, err)
            return
        st = self.element_state(load.address)
        for prop, raw in values:
            if (field_name := SENSOR_FIELDS.get(prop)) is None:
                continue
            codec = SIG_PROPERTIES[prop].codec
            # a value shorter than the characteristic (a Format A entry with a smaller length) is its low bytes:
            # padded, as every reading was read before the codecs took over; it cannot be a marker
            size = codec.size if isinstance(codec, Scaled) else len(raw)
            setattr(st, field_name, codec.decode(raw.ljust(size, b"\0")))
        self.notify_update(load.address)

    @register_status_handler(M.GEN_ONOFF_SET, M.GEN_ONOFF_SET_UNACK)
    def _on_onoff_set(self, m: AccessMessage, p: bytes) -> None:
        """Report the upper / lower half of a rocker wired to a load or a room being pressed."""
        if self.gestures.from_button(m, p) and p:
            self.fire_button(
                m.src, "press_on" if p[0] else "press_off", {"target": f"{m.dst:04X}"}
            )

    @register_status_handler(M.SCENE_RECALL, M.SCENE_RECALL_UNACK)
    def _on_scene_recall(self, m: AccessMessage, p: bytes) -> None:
        """Report a scene recall: a rocker wired to the scene being pressed, else the app's or the gateway's recall.

        A key's recall is the key's `scene` event (whose publication publishes the scene event too,
        `event.publish_button_event`, whether or not the key's event entity is enabled); any other sender's is
        published as the scene event directly, with the sender as its source. The firmware's second copy of either
        (same TID) is dropped. The recall is noted where the scene event fires (`fire_scene_recalled`), so the
        members' Scene Status that follows reports only a recall no key event or sender told of.
        """
        if len(p) < 2 or self.gestures.is_repeat(m.src, m.opcode, p):
            return
        number = int.from_bytes(p[:2], "little")
        if self.gestures.is_button(m.src):
            self.fire_button(m.src, "scene", {"scene": number})
            return
        from .event import fire_scene_recalled  # noqa: PLC0415

        fire_scene_recalled(self.hass, self, number, m.src)

    @register_status_handler(M.SCENE_STATUS)
    def _on_scene_status(self, m: AccessMessage, p: bytes) -> None:
        """Store an element's current scene; a published one after a recall nobody reported is the scene event.

        JUNG nodes publish `[status][current]` to their group after each recall they carry out; an answer to a
        Scene Get only updates the state: ours (`Refresh.get_current_scenes`) — sent to us, or published while the Get
        waits for it, as JUNG firmware does with other answers — and the app's or the gateway's, sent to their
        unicast address (the proxy filter is a blacklist, so those reach us too). A publication (to a group, or any
        address that is no unicast one) of a scene no recall reported within SCENE_RECALL_WINDOW (a recall Home
        Assistant did not hear) fires the scene event without a source, naming the element that reported it.
        """
        try:
            status = M.decode_scene_status(p)
        except ValueError:
            _LOGGER.debug("%04X: malformed Scene Status %s", m.src, p.hex())
            return
        if not status.ok:
            return  # a refused recall / get changes nothing
        self._set_current_scene(m.src, status.current)
        if (
            status.current
            and not is_unicast(m.dst)
            and m.src not in self.refresh.scene_gets
            and self.note_scene_recall(status.current)
        ):
            from .event import fire_scene_recalled  # noqa: PLC0415

            fire_scene_recalled(
                self.hass, self, status.current, None, reported_by=m.src
            )

    @register_status_handler(M.SCENE_REGISTER_STATUS)
    def _on_scene_register_status(self, m: AccessMessage, p: bytes) -> None:
        """Keep an element's scene register (the app's `RegisterCapacityEntity`) and its current scene."""
        try:
            register = M.decode_scene_register_status(p)
        except ValueError:
            _LOGGER.debug("%04X: malformed Scene Register Status %s", m.src, p.hex())
            return
        self.scene_registers[m.src] = register.scenes
        if register.ok:
            self._set_current_scene(m.src, register.current)

    def _set_current_scene(self, addr: int, number: int) -> None:
        """Store the element's current scene; the scene entities re-read it when it changed."""
        st = self.element_state(addr)
        if st.scene == number:
            return
        st.scene = number
        self.notify_update(addr)
        async_dispatcher_send(self.hass, SIGNAL_SCENES.format(self.entry.entry_id))

    @register_status_handler(*GENERIC_LEVEL_OPCODES)
    def _on_level_set(self, m: AccessMessage, p: bytes) -> None:
        """Report a rocker dimming its load (Generic Level / Delta / Move Set), then the hold that makes (`ButtonGestures.dim_hold`)."""
        if self.gestures.from_button(m, p):
            self.fire_button(m.src, "dim", {"target": f"{m.dst:04X}", "raw": p.hex()})
            self.gestures.dim_hold(m, p)

    @register_status_handler(VENDOR_USER_PROPERTY_SET_UNACK, company_id=M.JUNG_CID)
    def _on_vendor_property_set(self, m: AccessMessage, p: bytes) -> None:
        """Gateway-mode keys publish their gestures as User Property 0x5012 `[counter][code]`."""
        if len(p) >= 4 and int.from_bytes(p[:2], "little") == PROPERTY_BUTTON_EVENT:
            self.gestures.button_event(m.src, p[2], p[3])

    # ------------------------------------------------------------------ buttons (`hub_gestures.py`)
    @property
    def click_delay(self) -> bool:
        """Whether a `click` is held back until a double click can no longer follow (the `click_delay` option)."""
        return self.gestures.click_delay

    @callback
    def add_event_listener(self, addr: int, cb: EventListener) -> Callable[[], None]:
        """Register `cb` for button events from `addr`; returns the unsubscribe callable."""
        return self.gestures.add_event_listener(addr, cb)

    @callback
    def fire_button(self, addr: int, event: str, attrs: dict[str, Any]) -> None:
        """Deliver a button event of the element at `addr` to its listeners, then publish it (`ButtonGestures.fire_button`)."""
        self.gestures.fire_button(addr, event, attrs)

    @callback
    def publish_button_event(
        self, addr: int, event: str, attrs: dict[str, Any]
    ) -> None:
        """Publish a button event on the bus (`event.publish_button_event`), for `ButtonGestures.fire_button`."""
        from .event import publish_button_event  # noqa: PLC0415

        publish_button_event(self.hass, self, addr, event, attrs)

    # ------------------------------------------------------------------ repairs
    def _on_beacon(self, beacon: SecureNetworkBeacon) -> None:
        """Account for a beacon of the proxy; one flagging a key refresh that our key cannot open raises an issue.

        Key refresh Phase 2 beacons are secured with the *new* NetKey (Mesh Profile §3.10.4), so a refresh of the
        keys the export holds shows up as an unauthenticated beacon with the Key Refresh flag — "authenticated and
        flagged" means our keys are the new ones already (review-3 T3: the issue used to wait for exactly that,
        which never happens). On a GATT link only the proxy node talks, so it is a strong hint, not proof: the
        flag itself is not authenticated.
        """
        self._last_rx = time.monotonic()
        if not beacon.authenticated:
            self._count_undecodable()
            if beacon.key_refresh:
                self.report_key_refresh()
            return
        self.beacon_authenticated = True
        self._check_iv_index(beacon.iv_index)

    def _check_iv_index(self, network: int) -> None:
        """Raise `iv_index_mismatch` when the mesh's IV index is one Home Assistant cannot follow (review-3 T5).

        `LocalState.apply_beacon` follows an index up to 42 ahead (IV Index Recovery) and ignores an older one (a
        lagging node, one behind for up to 96 hours during an update). Further ahead — Home Assistant was away
        through more IV updates than recovery allows, or its store is from another mesh — or two and more behind —
        its store is ahead of the mesh — every node drops Home Assistant's messages and the beacon that says why
        used to be dropped silently. Cleared by the next beacon within reach.

        Home Assistant ahead (pushed there by forged beacons, or a store of another mesh) is fixable when going back
        keeps every nonce unique (`LocalState.can_rewind_to`): the repair (`ISSUE_IV_INDEX_AHEAD`'s text, the
        `iv_index_mismatch` fix flow) rewinds to the mesh's index, `async_rewind_iv_index`. Otherwise, and when the
        mesh is ahead, the way back is a new unicast address (Reconfigure).
        """
        state = self.proxy.state
        key = issue_id(self.entry, ISSUE_IV_INDEX_MISMATCH)
        if not state.iv_known or state.iv_index - 1 <= network <= state.iv_index + 42:
            ir.async_delete_issue(self.hass, DOMAIN, key)
            return
        _LOGGER.warning(
            "The mesh is at IV index %d, Home Assistant at %d: out of the reach of IV Index Recovery",
            network,
            state.iv_index,
        )
        fixable = state.can_rewind_to(network)  # behind us, and the record knows enough
        translation_key = ISSUE_IV_INDEX_AHEAD if fixable else ISSUE_IV_INDEX_MISMATCH
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            key,
            is_fixable=fixable,
            severity=ir.IssueSeverity.ERROR,
            translation_key=translation_key,
            learn_more_url=learn_more_url(translation_key),
            translation_placeholders={
                "title": self.entry.title,
                "mesh": str(network),
                "ours": str(state.iv_index),
            },
            data={"entry_id": self.entry.entry_id, "network": network}
            if fixable
            else None,
        )

    async def async_rewind_iv_index(self, network: int) -> str | None:
        """Take Home Assistant's IV index back to the mesh's `network` (the fixable `iv_index_mismatch` repair).

        Only while Home Assistant is still ahead out of reach and the rewind keeps every nonce unique — the state may
        have moved since the issue was raised; the repair floor is written first (`async_rewind_seq_floor`), then
        `LocalState.rewind_iv_index` moves the counter past every number sent from the mesh's index on, guarded up
        to the old index, and `HAState.persist` writes both copies of the store at once (the transmit index
        changed). The caller reloads the entry, so the next setup starts at the mesh's index. Returns None when
        done, else the reason the repair flow aborts with.
        """
        state = self.state
        if not (
            state.iv_known
            and network < state.iv_index - 1
            and state.can_rewind_to(network)
        ):
            return "iv_index_not_ahead"
        seq, guard = state.rewind_point()
        if not await async_rewind_seq_floor(
            self.hass, self.cdb.mesh_uuid, f"{state.src:04X}", network, seq, guard
        ):
            _LOGGER.error(
                "Could not write the sequence-number floor of address %04X: not going back without it",
                state.src,
            )
            return "seq_store_not_written"
        # the IV state moved while the floor was written: what it holds may no longer cover it
        if not state.can_rewind_to(network) or state.rewind_point()[1] > guard:
            return "iv_index_not_ahead"
        seq = state.rewind_iv_index(network)
        _LOGGER.warning(
            "IV index of address %04X goes back to the mesh's %d; its sequence numbers continue from %06X",
            state.src,
            network,
            seq,
        )
        return None

    def _on_undecryptable(self) -> None:
        self._count_undecodable()

    def _count_undecodable(self) -> None:
        """Count a PDU our keys cannot open, or a beacon they cannot authenticate.

        One or two are normal (another mesh in range, a node the export does not know). A link that has forwarded
        EXPORT_STALE_THRESHOLD of them and nothing decodable is a mesh whose keys are not the export's: a key refresh
        completed after the export was made (`docs/cross-repo-analysis.md` §5). Without this the symptom is only a
        silent link dropped by the watchdog every LINK_IDLE_TIMEOUT.
        """
        self.rx_undecodable_link += 1
        if (
            not self._export_stale
            and self.rx_decoded_link == 0
            and self.rx_undecodable_link >= EXPORT_STALE_THRESHOLD
        ):
            self._report_export_stale(True)

    def _report_export_stale(self, stale: bool) -> None:
        """Raise (or clear) the repair issue for a mesh whose keys are not the ones in the export."""
        self._export_stale = stale
        if not stale:
            ir.async_delete_issue(
                self.hass, DOMAIN, issue_id(self.entry, ISSUE_EXPORT_STALE)
            )
            return
        _LOGGER.error(
            "Nothing heard through proxy node %s can be decrypted with the keys of the export (%d messages so far): "
            "the mesh keys changed — export the network again and reconfigure",
            self.proxy_address,
            self.rx_undecodable_link,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry, ISSUE_EXPORT_STALE),
            is_fixable=True,  # its repair loads a new export (`repairs.NewExportFlow`)
            data={"entry_id": self.entry.entry_id},
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_EXPORT_STALE,
            learn_more_url=learn_more_url(ISSUE_EXPORT_STALE),
            translation_placeholders={"title": self.entry.title},
        )

    @callback
    def _on_key_refresh(self, phase: int, key: NetKeyMaterial) -> None:
        """Clear the key-refresh issue: the provisioner's refresh moved on and the client followed it (review-3 N2b).

        Once it completes (phase 0, reported only on proof that the mesh moved: review-4 D4) the Network ID is the
        new key's: the entry's unique id follows, so discovery keeps recognising this mesh. The export keeps the old key until it is fetched again; every setup puts the
        followed one in its place (`async_apply_followed_key_refresh`). Every move — a proven Phase 1 included —
        takes the devices Home Assistant added along as far as it is proven (`vault_refresh.py`, review-4 D11).

        Review-4 H4-4: from the first move on, discovery recognises the new key's Network ID as this mesh
        (`KNOWN_MESHES`); at completion a discovery flow it started before is aborted and an ignored entry holding
        it removed before the unique id moves (`async_release_network_id`).
        """
        ir.async_delete_issue(
            self.hass, DOMAIN, issue_id(self.entry, ISSUE_KEY_REFRESH)
        )
        self.vault_refresh.schedule()
        if (
            known := self.hass.data.get(KNOWN_MESHES, {}).get(self.entry.entry_id)
        ) is not None:
            known.network_ids.add(key.network_id)
        if phase == 0 and self.entry.unique_id != (network_id := key.network_id.hex()):
            # eager: without an ignored entry to remove, the unique id has moved when this returns
            self.hass.async_create_task(
                self._async_follow_network_id(network_id), eager_start=True
            )

    async def _async_follow_network_id(self, network_id: str) -> None:
        """Move the entry's unique id to the completed refresh's Network ID, once nothing else holds it."""
        await async_release_network_id(self.hass, self.entry, network_id)
        if self.hass.config_entries.async_get_entry(self.entry.entry_id) is self.entry:
            self.hass.config_entries.async_update_entry(
                self.entry, unique_id=network_id
            )

    def report_key_refresh(self) -> None:
        """Raise a repair issue: the keys in the export are being replaced, the mesh will stop accepting us."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry, ISSUE_KEY_REFRESH),
            is_fixable=True,  # its repair loads a new export (`repairs.NewExportFlow`)
            data={"entry_id": self.entry.entry_id},
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_KEY_REFRESH,
            learn_more_url=learn_more_url(ISSUE_KEY_REFRESH),
            translation_placeholders={"title": self.entry.title},
        )

    async def async_skip_ahead(self) -> None:
        """Jump our counter SEQ_SKIP_AHEAD ahead and start a fresh link (the `pdus_dropped` repair).

        The nodes drop our messages as replays when they know our numbers higher than we do: a store restored
        from an older backup, or lost. Past them the new link's filter request and refresh get through.
        """
        seq = self.state.skip_ahead(SEQ_SKIP_AHEAD)
        _LOGGER.warning(
            "Sequence numbers of address %04X continue from %06X; reconnecting",
            self.state.src,
            seq,
        )
        if self.proxy.connected:
            # not the proxy's fault: no verdict on it, and the entities keep the grace (review-4 R4-3)
            await self._drop_link("sequence numbers skipped ahead", penalise=False)

    def report_pdus_dropped(self, dropped: bool) -> None:
        """Raise (or clear) the repair issue for a mesh that ignores us although the link works (the caller logs why).

        Raised by the Filter Status watchdog and by an unanswered state refresh; cleared by a Filter Status or by
        the first message addressed to us (`_on_message`). Not raised while another client is known to send from
        our address (`address_shared` names the cause, and its repair is the one that helps).
        """
        if dropped and self.state.address_shared is not None:
            return
        self._pdus_dropped = dropped
        if not dropped:
            ir.async_delete_issue(
                self.hass, DOMAIN, issue_id(self.entry, ISSUE_PDUS_DROPPED)
            )
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry, ISSUE_PDUS_DROPPED),
            is_fixable=True,
            data={"entry_id": self.entry.entry_id},
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_PDUS_DROPPED,
            learn_more_url=learn_more_url(ISSUE_PDUS_DROPPED),
            translation_placeholders={
                "title": self.entry.title,
                "unicast": f"{self.proxy.state.src:04X}",
            },
        )

    def _on_foreign_own_source(self, iv_index: int, seq: int) -> None:
        """Another client sends from our address: the proxy delivered a PDU from it with a number we never sent (S I2).

        Its numbers and ours run into each other — every one both send is a reused nonce, and the nodes drop ours as
        replays below its last — so from here on nothing is sent (`HAState.note_address_shared`, `AddressShared`)
        until `address_shared` is repaired. That issue explains the dropped PDUs better than `pdus_dropped` does,
        which it replaces.
        """
        if self.state.note_address_shared(iv_index, seq):
            _LOGGER.error(
                "Another Bluetooth mesh client sends from Home Assistant's address %04X (sequence number %06X under "
                "IV index %d, which Home Assistant never sent): nothing is sent to the mesh %s until the repair is "
                "confirmed",
                self.state.src,
                seq,
                iv_index,
                self.entry.title,
            )
        if self._pdus_dropped:
            self.report_pdus_dropped(False)
        self._report_address_shared()

    def _report_address_shared(self) -> None:
        """Raise `address_shared`; once its repair skipped past the other client, with the text asking for another address.

        The same issue id either way (`ISSUE_ADDRESS_SHARED_AGAIN` is only its other translation key).
        """
        translation_key = (
            ISSUE_ADDRESS_SHARED_AGAIN
            if self._address_shared_skipped
            else ISSUE_ADDRESS_SHARED
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry, ISSUE_ADDRESS_SHARED),
            is_fixable=True,
            data={"entry_id": self.entry.entry_id},
            severity=ir.IssueSeverity.ERROR,
            translation_key=translation_key,
            learn_more_url=learn_more_url(translation_key),
            translation_placeholders={
                "title": self.entry.title,
                "unicast": f"{self.state.src:04X}",
            },
        )

    async def async_skip_past_shared(self) -> None:
        """Continue past the other client's numbers and send again (the `address_shared` repair).

        The issue goes at once; should the other client still send above the new counter, it comes back asking for
        another address. A link whose proxy never took our filter (its request was refused too) is renewed: on the
        default white list the proxy forwards next to nothing.
        """
        seq = self.state.skip_past_shared()
        self._address_shared_skipped = True
        ir.async_delete_issue(
            self.hass, DOMAIN, issue_id(self.entry, ISSUE_ADDRESS_SHARED)
        )
        if seq is None:
            return
        _LOGGER.warning(
            "Sequence numbers of address %04X continue from %06X, past the other client's",
            self.state.src,
            seq,
        )
        if self.proxy.connected and self.proxy.proxy_addr is None:
            await self._drop_link(
                "sequence numbers skipped past another client's", penalise=False
            )

    # ------------------------------------------------------------------ commands
    async def _wait_for_link(self) -> None:
        """During the link-loss grace, wait for the new link: a command then goes out on it instead of failing."""
        if not self.connected and self._lost_at is not None and not self.stopping:
            remaining = LINK_LOSS_GRACE - (time.monotonic() - self._lost_at)
            if remaining > 0:
                await self.async_wait_connected(remaining)

    async def _command(self, dst: int, access_pdu: bytes) -> None:
        """Send a command without waiting for its status, then expect an echo from the mesh.

        An unacknowledged group command (a room, "all lights", a scene), or a movement (`move_level`, `delta_level`):
        the app never sends a Move Set, and whether a blind publishes its level when it starts to run or only when
        it stops is not known (`control-and-state.md` §5), so waiting for a status could fail a working blind or
        dimmer and mark it unreachable. JUNG loads publish their new state right after a Set; when nothing at all
        arrives from the mesh within
        COMMAND_ECHO_TIMEOUT, the link watchdog probes the proxy at once (a proxy that stopped forwarding is
        otherwise only noticed after LINK_IDLE_TIMEOUT of silence, and every command until then is lost).
        """
        await self._wait_for_link()
        await self.proxy.send_access(dst, access_pdu)
        if self._unsub_echo is not None:
            return  # the first command of a burst is watched: anything the mesh sends answers them all
        sent_at = self._last_rx

        @callback
        def check(_now: datetime) -> None:
            self._unsub_echo = None
            if self.connected and self._last_rx == sent_at:
                _LOGGER.debug(
                    "No answer to a command within %.0f s", COMMAND_ECHO_TIMEOUT
                )
                self._probe_link.set()

        self._unsub_echo = async_call_later(self.hass, COMMAND_ECHO_TIMEOUT, check)

    async def _load_command(
        self,
        dst: int,
        access_pdu: bytes,
        status: int,
        kind: str,
        *,
        load: int | None = None,
    ) -> AccessMessage:
        """Send a load's acknowledged Set and wait for its status, the app's way (`CommunicateWithDevice` UPDATE).

        Up to REQUEST_ATTEMPTS attempts of REQUEST_TIMEOUT, each the same PDU (same TID: the load applies it once and
        answers the repeat), matched on the element and the status opcode — JUNG firmware answers a Set that changed
        something by publishing that status to the element group. A status that does not show the requested state
        answers a state Get out to the element rather than the Set (review-4 D32: the Set was lost, the Get's reply
        shows the old state), and the Set goes out again on its next attempt (`ProxyClient.request`). Unanswered,
        the link watchdog probes the proxy at once, and the node is marked unreachable (`Liveness.missed_answer`: `load` and
        `kind` name the load element and state Get its re-asks use, the light's for a colour-temperature element)
        only once the proxy answered the probe: a proxy that stopped forwarding leaves every command unanswered, and
        the nodes are not to blame (the link is dropped, and the next one's refresh asks them all). The TimeoutError
        is raised for the caller to report either way.

        A link that ended or changed while the command was out (review-4 R I-11) says nothing about the load: the
        command is sent once more, on the next link (`_wait_for_link`) — same PDU, same TID, so a load that did
        apply it only answers. Unverified on air. Returns the status that confirmed the Set.
        """
        retry = False
        while True:
            await self._wait_for_link()
            link = self.link_count if self.connected else None
            asked = time.monotonic()
            try:
                return await self.proxy.request(
                    dst,
                    access_pdu,
                    status,
                    retries=REQUEST_ATTEMPTS,
                )
            except (TimeoutError, ConnectionError) as err:
                same_link = self.connected and self.link_count == link
                if link is not None and not same_link and not retry:
                    _LOGGER.debug(
                        "The link changed during a command to %04X; sending it again on the next one",
                        dst,
                    )
                    retry = True
                    continue
                if isinstance(err, TimeoutError) and same_link:
                    # else the link went meanwhile: the new link's refresh asks the node again
                    self._unanswered.append(
                        (dst if load is None else load, kind, asked)
                    )
                    self._probe_link.set()
                raise

    @staticmethod
    def _transition_byte(seconds: float | None) -> int | None:
        """Return the transition-time byte for `seconds` (HA's `transition`), clamped to what one carries; None for none."""
        if seconds is None:
            return None
        return M.encode_transition(
            max(0.0, min(float(seconds), M.TRANSITION_MAX_SECONDS))
        )

    def _reread_after_transition(self, load: int, reply: AccessMessage) -> None:
        """Ask `load` for its state once the transition its Set's status announced is over, plus a second.

        A status answering a Set with a transition time carries the present state, the target and the time still to
        run; the entity shows the present one (see the status handlers), which a load that publishes nothing at the
        end of its fade would leave behind. Nothing is scheduled for a status at rest or one whose remaining time is
        unknown; a newer Set replaces the pending read. Unverified on air: no JUNG firmware was seen taking a
        transition time (`docs/hidden-features.md` §11).
        """
        remaining = M.remaining_time(reply.opcode, reply.params)
        if not remaining:
            return
        if (cancel := self._transition_reread.pop(load, None)) is not None:
            cancel()
        kind = getattr(self.devices.by_address.get(load), "kind", "switch")
        self._transition_reread[load] = async_call_later(
            self.hass,
            remaining + TRANSITION_REREAD_SLACK,
            partial(self._transition_over, load, kind),
        )

    @callback
    def _transition_over(self, load: int, kind: str, _now: datetime) -> None:
        """Read the load's state after its transition, unless the link is down (the next one's refresh reads it)."""
        del self._transition_reread[load]
        if not self.connected:
            return
        self.entry.async_create_background_task(
            self.hass,
            self.async_refresh_element(load, kind),
            f"{DOMAIN} transition {load:04X}",
        )

    async def set_onoff(
        self, addr: int, on: bool, transition: float | None = None
    ) -> None:
        """Switch the element at `addr` on or off, over `transition` seconds when given (else transition time 0).

        A transition is unverified on air (`_reread_after_transition`).
        """
        self._note_request(addr, on=on)
        byte = self._transition_byte(transition)
        reply = await self._load_command(
            addr,
            M.generic_onoff_set(on, transition=0 if byte is None else byte),
            M.GEN_ONOFF_STATUS,
            "switch",
        )
        if byte is not None:
            self._reread_after_transition(addr, reply)

    async def identify(self, node: Node, seconds: int = IDENTIFY_SECONDS) -> None:
        """Make `node` draw attention to itself (Health Attention Set to its primary element; its LED blinks)."""
        await self.proxy.request(
            node.unicast,
            M.health_attention_set(seconds),
            M.HEALTH_ATTENTION_STATUS,
            retries=1,
        )

    async def clear_faults(self, node: Node) -> None:
        """Forget `node`'s registered Health faults (Health Fault Clear) and read the register back.

        The acknowledged Clear (`0x802F`, the form seen clearing JUNG nodes, `docs/hidden-features.md` §10) goes
        out without waiting for its Fault Status, which is not observed yet; a Health Fault Get then fetches the
        emptied register for the Fault entity (a Status answering the Clear lands in the same handler). A node that
        does not answer the Get raises TimeoutError.
        """
        await self.proxy.send_access(node.unicast, M.health_fault_clear())
        await self.proxy.request(
            node.unicast, M.health_fault_get(), M.HEALTH_FAULT_STATUS, retries=1
        )

    async def central_command(
        self,
        group: int,
        on: bool,
        lightness: int | None = None,
        transition: float | None = None,
    ) -> None:
        """Switch every load listening to a device-type group at once, as the app's central functions do.

        Unacknowledged Sets (an acknowledged one would have every member answer at the same moment): a lightness
        first, which only the dimmable members take, then OnOff, which switches the others and leaves a dimmer that
        is already on where the lightness put it. Both carry `transition` seconds when given (unverified on air).
        """
        byte = self._transition_byte(transition)
        if lightness is not None:
            await self._command(
                group,
                M.light_lightness_set(
                    max(1, min(65535, lightness)), ack=False, transition=byte
                ),
            )
        await self._command(
            group,
            M.generic_onoff_set(on, ack=False, transition=0 if byte is None else byte),
        )

    async def central_level(self, group: int, level: int) -> None:
        """Send every member of a device-type group one Unacknowledged Generic Level Set: blinds, slats, set-points."""
        await self._command(
            group, M.generic_level_set(max(-32768, min(32767, level)), ack=False)
        )

    async def central_stop(self, group: int) -> None:
        """Stop every blind of a group: Unacknowledged Generic Delta Set 0, as the app's "all blinds stop".

        A device-type group (`OpenClose.Blinds`) or a room address (`OpenClose.Group`, the area sheet's stop).
        """
        await self._command(group, M.generic_delta_set(0, ack=False))

    async def room_command(
        self,
        room: int,
        members: Sequence[int],
        on: bool,
        lightness: int | None = None,
        transition: float | None = None,
    ) -> None:
        """Switch a room's lights or sockets as the app's area sheet does: a dim level to the room, on / off per load.

        A lightness is the app's `Dim.Group`: one Unacknowledged Light Lightness Set to the room address, taken by
        the dimmable members (the Lightness server shares the room subscription of the OnOff / Level servers it
        extends). On / off never goes to the room address, where the lights and the sockets both listen: every
        member gets an Unacknowledged OnOff Set of its own (`CommunicateWithDevice` not waiting for a status), after
        the lightness, as `central_command` orders them, and with its `transition`.
        """
        byte = self._transition_byte(transition)
        if lightness is not None:
            await self._command(
                room,
                M.light_lightness_set(
                    max(1, min(65535, lightness)), ack=False, transition=byte
                ),
            )
        onoff = M.generic_onoff_set(
            on, ack=False, transition=0 if byte is None else byte
        )
        for addr in members:
            await self._command(addr, onoff)

    async def room_level(self, addresses: Sequence[int], level: int) -> None:
        """One Unacknowledged Generic Level Set per element: a room's blind positions, slats or set-points.

        The app's area sheet sends these per device (`CommunicateWithDevice`, no status awaited), not to the room
        address, which the dimmers' Level servers share.
        """
        level = max(-32768, min(32767, level))
        for addr in addresses:
            await self._command(addr, M.generic_level_set(level, ack=False))

    async def set_lightness(
        self, addr: int, lightness: int, transition: float | None = None
    ) -> None:
        """Set the lightness (0-65535) of the element at `addr`, over `transition` seconds when given.

        Without a transition the Set carries none (the light's Default Transition Time, 0 on every JUNG load seen).
        """
        lightness = max(0, min(65535, lightness))
        self._note_request(addr, lightness=lightness)
        byte = self._transition_byte(transition)
        reply = await self._load_command(
            addr,
            M.light_lightness_set(lightness, transition=byte),
            M.LIGHT_LIGHTNESS_STATUS,
            "dimmer",
        )
        if byte is not None:
            self._reread_after_transition(addr, reply)

    async def set_ctl(
        self, addr: int, lightness: int, kelvin: int, transition: float | None = None
    ) -> None:
        """Set lightness (0-65535) and colour temperature (Kelvin) of the element at `addr`, over `transition` s."""
        lightness = max(0, min(65535, lightness))
        self._note_request(addr, lightness=lightness, kelvin=kelvin)
        byte = self._transition_byte(transition)
        reply = await self._load_command(
            addr,
            M.light_ctl_set(lightness, kelvin, transition=byte),
            M.LIGHT_CTL_STATUS,
            "ctl",
        )
        if byte is not None:
            self._reread_after_transition(addr, reply)

    async def set_ctl_temperature(
        self, light: Light, kelvin: int, transition: float | None = None
    ) -> None:
        """Set the colour temperature (Kelvin) of a CTL light alone: Light CTL Temperature Set to its temperature element.

        A full CTL Set would have to carry a lightness, and the cached one lags behind a light that is dimming or
        was dimmed elsewhere without a status reaching us: the light jumped back to it. The temperature element
        answers with a Light CTL Temperature Status, which lands on the light (`_ctl_light_of`); the Set is noted
        on the light too, whose Light CTL Get `async_wait_settled` reads. The Set is the gateway's 7-byte form,
        transition 0 and delay 0: without them the light would fall back to its Default Transition Time — or with
        `transition` seconds when given (unverified on air).
        """
        assert light.temperature_address is not None  # the caller checks it has one
        self._note_request(light.address, kelvin=kelvin)
        byte = self._transition_byte(transition)
        reply = await self._load_command(
            light.temperature_address,
            M.light_ctl_temperature_set(kelvin, transition=0 if byte is None else byte),
            M.LIGHT_CTL_TEMP_STATUS,
            "ctl",
            load=light.address,
        )
        if byte is not None:
            self._reread_after_transition(light.address, reply)

    async def set_level(self, addr: int, level: int) -> None:
        """Set the Generic Level (-32768..32767) of the element at `addr`: a blind's position or slat target."""
        level = max(-32768, min(32767, level))
        self._note_request(addr, level=level)
        await self._load_command(
            addr, M.generic_level_set(level), M.GEN_LEVEL_STATUS, "level"
        )

    async def delta_level(self, addr: int, delta: int) -> None:
        """Send Generic Delta Set `delta` to the element at `addr` (the app's blind up / down / stop: -1 / +1 / 0)."""
        await self._command(addr, M.generic_delta_set(delta))

    async def move_level(self, addr: int, delta: int, transition: int) -> None:
        """Send Generic Move Set `delta` (-32768..32767) with a transition-time byte to the element at `addr`.

        The gateway drives blinds with it: 0x7FFF down, 0x8000 (-32768) up, 0 stop (`docs/cross-repo-analysis.md`
        §1.4); a Move Set without a transition time would be ignored by a spec-compliant Level server.
        """
        await self._command(addr, M.generic_move_set(delta, transition=transition))

    async def recall_scene(self, number: int, transition: float | None = None) -> None:
        """Recall a scene on every node (unacknowledged); the proxy never echoes our own PDU, so the bus event is ours to fire.

        `transition` seconds go into the Recall when given (one for every node; unverified on air).
        """
        await self._command(
            ALL_NODES,
            M.scene_recall(
                number, ack=False, transition=self._transition_byte(transition)
            ),
        )
        # event.py imports this module, hence the late import
        from .event import fire_scene_recalled  # noqa: PLC0415

        fire_scene_recalled(self.hass, self, number, self.proxy.state.src)
