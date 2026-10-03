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
import ipaddress
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Container, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
from homeassistant.util.hass_dict import HassKey

from .const import (
    ATTR_REASON,
    AUDIT_RETRIES,
    AUDIT_TIMEOUT,
    BUTTON_REPEAT_WINDOW,
    COMMAND_ECHO_TIMEOUT,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_HEARTBEATS_PUBLISHING,
    CONF_SOURCE,
    CONNECT_BACKOFF_MAX,
    CONNECT_BACKOFF_MIN,
    CONNECT_BEACON_WAIT,
    CONNECT_STEP_FRESH,
    DEFAULT_CLICK_DELAY,
    DEFAULT_HEARTBEATS,
    DIM_HOLD_MAX,
    DIM_HOLD_QUIET,
    DOMAIN,
    DOUBLE_CLICK_WINDOW,
    ENERGY_HISTORY_INTERVAL,
    ENERGY_POLL_INTERVAL,
    EXPORT_REFRESH_BACKOFF,
    EXPORT_STALE_THRESHOLD,
    FAILED_PROXY_COOLDOWN,
    FILTER_STATUS_TIMEOUT,
    HEARTBEAT_CHECK_INTERVAL,
    HEARTBEAT_MISSED_BEATS,
    HEARTBEAT_PERIOD_LOG,
    HEARTBEAT_RECONFIGURE_INTERVAL,
    HEARTBEAT_REPROBE_INTERVAL,
    HOLD_END_LINK_LOST,
    HOLD_END_STOPPED,
    HOLD_END_TIMEOUT,
    HUB_DATA_KEYS,
    IDENTIFY_SECONDS,
    ISSUE_ADDRESS_SHARED,
    ISSUE_ADDRESS_SHARED_AGAIN,
    ISSUE_BLUETOOTH_UNAVAILABLE,
    ISSUE_DUPLICATE_MESH,
    ISSUE_EXPORT_STALE,
    ISSUE_GATEWAY_CERTIFICATE,
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
    ISSUE_UNKNOWN_NODES_GATEWAY,
    ISSUE_VAULT_KEY_REFRESH,
    KEEP_ALIVE_ATTEMPTS,
    KEEP_ALIVE_TIMEOUT,
    KEY_EVENT_RELEASE,
    KEY_EVENT_SIDE_DOWN,
    KEY_EVENT_SIDE_UP,
    KEY_EVENTS,
    LINK_BLUETOOTH_OFF,
    LINK_CONNECTED,
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
    OFFSET_CHANGE_DELAY,
    OFFSET_SEARCH_DAYS,
    OPTION_CLICK_DELAY,
    OPTION_HEARTBEATS,
    PIN_FROM_MESH,
    PROXY_ADVERT_MAX_AGE,
    REFRESH_CHUNK,
    REFRESH_RETRIES,
    REQUEST_ATTEMPTS,
    REQUEST_TIMEOUT,
    RESTART_BLOCK,
    RESTART_SLACK,
    SCENE_RECALL_WINDOW,
    SEQ_SKIP_AHEAD,
    SEQ_SKIP_UNKNOWN,
    SEQUENCE_CHECK_INTERVAL,
    SEQUENCE_SPACE_WARN,
    SHORT_LINK,
    SHORT_LINK_STREAK,
    SIG_SOFTWARE_VERSION,
    SIGNAL_CONNECTION,
    SIGNAL_LINK_STATE,
    SIGNAL_NODE,
    SIGNAL_REACHABILITY,
    SIGNAL_SCENES,
    SIGNAL_UPDATE,
    STOP_TIMEOUT,
    TID_REPEAT_WINDOW,
    TIME_SET_INTERVAL,
    UNREACHABLE_RECHECK,
    UNREACHABLE_REPROBE,
    learn_more_url,
)
from .energy_history import async_backfill, floor_hour
from .entity import PRODUCT_NAMES, update_node_device
from .gateway_api import JungHomeGatewayApi, api_for_entry
from .identity import async_vault_keeper
from .inserts import NodeInserts
from .jhmesh import config_messages as C
from .jhmesh import messages as M
from .jhmesh import vendor_models as V
from .jhmesh.advert import JungAdvertisement, mac_from_uuid, parse_manufacturer_data
from .jhmesh.audit import NodeAudit, audit_node, client_exchange
from .jhmesh.cdb import CDB, Node
from .jhmesh.client import (
    IV_INDEX_MAX,
    MESH_PROXY_SERVICE,
    NET_KEY_INDEX,
    SEQ_GUARD_FIRST_BEACON,
    SEQ_MAX,
    SEQ_TX_LIMIT,
    AccessMessage,
    Heartbeat,
    LocalState,
    ProxyClient,
    SequenceExhausted,
    SequenceStalled,
    _check_range,
)
from .jhmesh.crypto import NetKeyMaterial
from .jhmesh.devices import (
    BATTERY_PIDS,
    GATEWAY_PID,
    PP2_PIDS,
    Button,
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
from .tls import normalize_fingerprint
from .vault_refresh import VaultKeyRefresh

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .app_follow import AppFollower
    from .entity import TrackedPlatform
    from .gateway_status import GatewayPolls
    from .identity import VaultKeeper
    from .mesh_config import MeshConfigurator

_LOGGER = logging.getLogger(__name__)

# the time roles a node keeping the PP2 pucks' time answers (Time Role Status): authority, relay
TIME_KEEPER_ROLES = frozenset({1, 2})

# the gateway node's own LBC Manufacturer properties
GATEWAY_IP, GATEWAY_FINGERPRINT = 0xC002, 0xC003
# why the gateway is not used (`JungHomeHub.async_gateway_distrust`), for the logs and the sync repair
GATEWAY_UNVERIFIED = (
    "the gateway node has not confirmed its certificate over the mesh yet"
)
GATEWAY_CERTIFICATE_CHANGED = (
    "the gateway no longer matches the certificate pinned for it"
)
_HOST_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)", re.IGNORECASE)
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

STORAGE_VERSION = 1
# The sequence-number store's minor version: 2 adds an address record's optional `seq_guard` (`LocalState.seq_guard`,
# written by the `seq_store_lost` repair), 3 its optional `in_backup` (the mark of a Home Assistant backup being
# taken, `backup.py`), 4 the mesh-level part next to `addresses`, `{"mesh": {"key_refresh": …}}` (the key refresh
# followed, `HAState`: the address's own record keeps a copy, for an older reader), 5 an address record's optional
# `address_shared` (`[IV index, seq]` another client sent from the address, `HAState.address_shared`). A minor bump: Home Assistant
# loads a store of a higher minor version with the same major one as it is when the reader has no migration for it,
# so an older integration reads these records (and ignores the fields) rather than failing to start.
SEQ_STORAGE_MINOR_VERSION = 5
# Sequence-number persistence (`HAState`): the margin added to a counter found in use, and how many numbers may
# pass between two forced store writes — see the class docstring for the arithmetic.
SEQ_RESTART_MARGIN = 512
SEQ_SAVE_EVERY = 64
# how far the counter may run past the floor entry the `.floor` file durably holds before `HAState` writes a new one
# (review-4 S4-8): a quarter of what a repair with nothing else left continues past it (SEQ_SKIP_UNKNOWN), so a
# floor write that fails has three more chances before sends are held back for it
SEQ_FLOOR_EVERY = 1 << 20
SEQ_STALL_RETRY = 5.0  # seconds between forced-save retries while reserve_seq() is refusing to hand out numbers
# how long one send waits for the store to catch up (`JungHomeHub._while_seq_stalls`) before it is given up on: a
# healthy store lands its write within a second, one that refuses for this long will not do so by waiting
SEQ_STALL_DEADLINE = 120.0
# how long the store may refuse before `seq_store_unwritable` is raised (`HAState.report_unwritable`)
SEQ_STALL_ISSUE_AFTER = 60.0
SENSOR_POWER, SENSOR_VOLTAGE, SENSOR_CURRENT = 0x0081, 0x005D, 0x005C
# What the connect-time refresh asks a socket's meter element for, one property-qualified `Sensor Get` each: on air
# the meter ignores an unqualified Sensor Get (no property id; two attempts, no reply, every connection) and answers
# a qualified one within ~100 ms — the gateway polls the same way. Another metered load (the energy puck's output)
# is asked for its power only: all the app reads there (`docs/android/properties.md` §4, `MeasureLampDevice`), and
# voltage and current are the metering socket's properties (`properties.SOCKET_METERING`); unverified on air.
SENSOR_READINGS = (SENSOR_POWER, SENSOR_VOLTAGE, SENSOR_CURRENT)
METER_READINGS = (SENSOR_POWER,)
SENSOR_FIELDS = {  # `ElementState` field each reading lands in
    SENSOR_POWER: "power_w",
    SENSOR_VOLTAGE: "voltage_v",
    SENSOR_CURRENT: "current_a",
}
# SIG device properties a metering socket keeps on its Generic Property servers, none of them ever published, so
# `_poll_energy` reads them every ENERGY_POLL_INTERVAL: the power-on hours on the *main* element's Admin server
# (`docs/gap-analysis/control-and-state.md` §2.4) and the energy counters on the *meter* element (the Sensor
# Server's, `docs/hidden-features.md` §2): the lifetime total 0x0072 and the energy since turn-on 0x000D on its
# Manufacturer server, the resettable total 0x006A (the app's "reset consumption") on its Admin server. Another
# metered load (the energy puck's output) has the meter's counters only: the app reads no power-on hours there
# (`docs/gap-analysis/device-settings.md` §6, "no 0x006D"; `counter_element`), unverified on air.
PROPERTY_POWER_ON_TIME = 0x006D
PROPERTY_TOTAL_ENERGY = 0x006A
PROPERTY_PRECISE_TOTAL_ENERGY = 0x0072
PROPERTY_ENERGY_SINCE_TURN_ON = 0x000D


@dataclass(frozen=True)
class CounterRead:
    """One counter of a metering socket: which property, on which server, of which element, into which field."""

    pid: int
    server: (
        str  # "admin" | "manufacturer" — the Generic Property server kind that holds it
    )
    meter: bool  # False = the socket's main element, True = its meter element (`counter_element`)
    field: (
        str  # `ElementState` attribute the decoded value lands in (on the load's state)
    )


class CounterNotReset(Exception):
    """A socket answered a counter reset with a value other than 0 (it kept counting), or with none."""

    def __init__(self, address: int, pid: int) -> None:
        """Name the element and the counter."""
        super().__init__(f"{address:04X} did not reset property {pid:04X}")
        self.address = address
        self.pid = pid


COUNTER_READS: tuple[CounterRead, ...] = (
    CounterRead(PROPERTY_POWER_ON_TIME, "admin", False, "power_on_hours"),
    CounterRead(PROPERTY_PRECISE_TOTAL_ENERGY, "manufacturer", True, "energy_wh"),
    CounterRead(PROPERTY_TOTAL_ENERGY, "admin", True, "energy_resettable_wh"),
    CounterRead(
        PROPERTY_ENERGY_SINCE_TURN_ON, "manufacturer", True, "energy_since_on_wh"
    ),
)
COUNTER_FIELDS = {read.pid: read.field for read in COUNTER_READS}
# the app's "reset consumption" (`docs/gap-analysis/device-settings.md` §5.2): an acknowledged Admin Property Set of
# a 0 to each of these, in the app's order (`ConfigurationConsumptionViewModel.resetTotalConsumption`: the power-on
# hours first, then the resettable total); `meter` as in CounterRead. The lifetime total 0x0072 has no reset.
COUNTER_RESETS: tuple[tuple[int, bool], ...] = (
    (PROPERTY_POWER_ON_TIME, False),
    (PROPERTY_TOTAL_ENERGY, True),
)


def meter_readings(load: MeteredLoad) -> tuple[int, ...]:
    """Return the readings the connect-time refresh asks the load's meter for: SENSOR_READINGS or METER_READINGS."""
    return SENSOR_READINGS if isinstance(load, Socket) else METER_READINGS


def lacks_precise_energy(cdb: CDB, load: MeteredLoad) -> bool:
    """Whether a load other than a socket has no Generic Manufacturer Property Server (`1012`) on its meter.

    That server holds 0x0072 and 0x000D (`docs/hidden-features.md` §2, read on the metering socket only); the app
    reads neither on the energy puck (`docs/android/properties.md` §4), so the composition decides. Without it the
    load's *Energy* is 0x006A (`ElementState.energy_total`). A socket keeps its field-tested reads.
    """
    if isinstance(load, Socket) or load.meter_address is None:
        return False
    meter = cdb.element(load.meter_address)
    return meter is not None and "1012" not in meter.models


def counter_element(load: MeteredLoad, meter: bool) -> int | None:
    """Return the element that holds one of the load's counters (`CounterRead.meter`); None where it has no such counter.

    The meter element for the energy counters; the main element for the power-on hours, which only the metering
    socket keeps (`SIG_PROPERTIES[0x006D].products`).
    """
    if meter:
        return load.meter_address
    return load.address if isinstance(load, Socket) else None


SIG_PROPERTY_STATUS_OPCODES = (
    M.GEN_USER_PROP_STATUS,
    M.GEN_ADMIN_PROP_STATUS,
    M.GEN_MANU_PROP_STATUS,
)
SIG_PROPERTY_STATUS_BY_SERVER = {
    "admin": M.GEN_ADMIN_PROP_STATUS,
    "manufacturer": M.GEN_MANU_PROP_STATUS,
    "user": M.GEN_USER_PROP_STATUS,
}
# The vendor message gateway-mode keys publish their gestures with (docs/cross-repo-analysis.md §1.2).
VENDOR_USER_PROPERTY_SET_UNACK = 0x10  # LBC User Property Set Unacknowledged
PROPERTY_BUTTON_EVENT = 0x5012  # KEY_EVT: [counter][code]
PROPERTY_LOCK = 0x0009  # EnforceOutput: a load's lock (`ElementState.note_lock`)
GENERIC_LEVEL_OPCODES = frozenset(
    {M.GEN_LEVEL_SET, M.GEN_LEVEL_SET_UNACK, 0x8209, 0x820A, 0x820B, 0x820C}
)
# Offset of the TID in the parameters of the client messages a rocker sends (OnOff Set, Scene Recall, Level/Delta/Move Set).
TID_OFFSET = {
    M.GEN_ONOFF_SET: 1,
    M.GEN_ONOFF_SET_UNACK: 1,
    M.SCENE_RECALL: 2,
    M.SCENE_RECALL_UNACK: 2,
    M.GEN_LEVEL_SET: 2,
    M.GEN_LEVEL_SET_UNACK: 2,
    0x8209: 4,
    0x820A: 4,
    0x820B: 2,
    0x820C: 2,
}
# State Get and the status opcode that answers it, per load kind (`Light.kind` / "switch" / a blind's "level"), plus
# the colour-temperature range a CTL light supports ("ctl_range": read once per connection, it is a device property)
# and the colour temperature on its temperature element ("ctl_temperature": Light CTL Temperature Get to the element
# after the light's, as the gateway reads it; its silence is not counted toward reachability, see `_refresh_all`).
# A socket's meter element is not here: its readings need one qualified Sensor Get each (`_get_readings`).
STATE_GETS: dict[str, tuple[Callable[[], bytes], int]] = {
    "ctl": (M.light_ctl_get, M.LIGHT_CTL_STATUS),
    "dimmer": (M.light_lightness_get, M.LIGHT_LIGHTNESS_STATUS),
    "ctl_range": (M.light_ctl_temperature_range_get, M.LIGHT_CTL_TEMP_RANGE_STATUS),
    "ctl_temperature": (M.light_ctl_temperature_get, M.LIGHT_CTL_TEMP_STATUS),
    "level": (M.generic_level_get, M.GEN_LEVEL_STATUS),  # blind position / slats
}
ONOFF_GET: tuple[Callable[[], bytes], int] = (M.generic_onoff_get, M.GEN_ONOFF_STATUS)
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

EventListener = Callable[[str, dict[str, Any]], None]

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


def issue_id(entry: ConfigEntry, key: str) -> str:
    """Return the repair-issue id of `key` (an `ISSUE_*` translation key) for `entry`: one issue per mesh, not per domain."""
    return f"{key}_{entry.entry_id}"


# One lock per entry, kept across reloads, around everything that works on a hub and may replace it: the service
# calls (`services._run`) and the unknown-node refresh's reload (`JungHomeHub._reload_for_export`).
ENTRY_LOCKS: HassKey[dict[str, asyncio.Lock]] = HassKey(f"{DOMAIN}_service_locks")


def entry_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Return the entry's lock (`ENTRY_LOCKS`), created on first use."""
    return hass.data.setdefault(ENTRY_LOCKS, {}).setdefault(entry_id, asyncio.Lock())


# What the nodes of each entry told about themselves (`NODE_INFO`, `NODE_INFO_VENDOR`, the time role), by node
# unicast, then by item name, raw: kept for the life of `hass` so a reloaded hub starts with them — config-entity
# setup (`_candidates`) reads the software version before any Get can answer, the device registry shows the
# identity — and in a store of the entry (`node_versions_store`), so the first setup after a restart has them too.
NODE_VERSIONS: HassKey[dict[str, dict[int, dict[str, bytes]]]] = HassKey(
    f"{DOMAIN}_node_versions"
)
NODE_VERSIONS_STORAGE_VERSION = 1
NODE_VERSIONS_STORAGE_MINOR_VERSION = (
    2  # 1.1 was `{"<unicast hex>": "<version hex>"}`: the software version alone
)
NODE_VERSIONS_SAVE_DELAY = (
    10.0  # seconds: the connect-time reads of every node land in one write
)


class NodeInfoStore(Store[dict[str, dict[str, str]]]):
    """The entry's store of `NODE_VERSIONS`: `{"<unicast hex>": {"<item name>": "<raw value hex>"}}`."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Open the entry's store; its key is older than the items other than the version."""
        super().__init__(
            hass,
            NODE_VERSIONS_STORAGE_VERSION,
            f"{DOMAIN}.{entry_id}.node_versions",
            minor_version=NODE_VERSIONS_STORAGE_MINOR_VERSION,
        )

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> Any:
        """1.1 → 1.2: a node's software version becomes the one item of its record (bad rows go to the load)."""
        if old_major_version != NODE_VERSIONS_STORAGE_VERSION:
            raise NotImplementedError
        rows = old_data if isinstance(old_data, dict) else {}
        return {
            key: {NODE_INFO[SIG_SOFTWARE_VERSION]: value}
            if isinstance(value, str)
            else value
            for key, value in rows.items()
        }


NODE_VERSION_STORES: HassKey[dict[str, NodeInfoStore]] = HassKey(
    f"{DOMAIN}_node_version_stores"
)


def node_versions_store(hass: HomeAssistant, entry_id: str) -> NodeInfoStore:
    """Return the entry's store of node information (`NodeInfoStore`).

    One instance per entry, so the removal of the entry cancels the delayed save it removes the file of.
    """
    stores = hass.data.setdefault(NODE_VERSION_STORES, {})
    if entry_id not in stores:
        stores[entry_id] = NodeInfoStore(hass, entry_id)
    return stores[entry_id]


async def async_load_node_versions(hass: HomeAssistant, entry_id: str) -> None:
    """Fill `NODE_VERSIONS` for the entry from its store, once per run of Home Assistant; bad rows are skipped."""
    cache = hass.data.setdefault(NODE_VERSIONS, {})
    if entry_id in cache:
        return
    data = await node_versions_store(hass, entry_id).async_load()
    nodes: dict[int, dict[str, bytes]] = {}
    for key, items in (data if isinstance(data, dict) else {}).items():
        try:
            nodes[int(key, 16)] = {
                str(name): bytes.fromhex(value) for name, value in items.items()
            }
        except (AttributeError, TypeError, ValueError):
            _LOGGER.debug("skipped the stored node information %r: %r", key, items)
    cache.setdefault(entry_id, nodes)


async def async_remove_node_versions(hass: HomeAssistant, entry_id: str) -> None:
    """Forget the entry's node information, in memory and on disk."""
    await node_versions_store(hass, entry_id).async_remove()
    hass.data.get(NODE_VERSION_STORES, {}).pop(entry_id, None)
    hass.data.get(NODE_VERSIONS, {}).pop(entry_id, None)


SEQ_STORES: HassKey[dict[str, SeqStore]] = HassKey(f"{DOMAIN}_seq_stores")
SEQ_OWNERS: HassKey[dict[str, HAState]] = HassKey(f"{DOMAIN}_seq_owners")
# the mark of the Home Assistant backup being taken right now (`backup.async_pre_backup`): every record written
# while it is set carries it (`HAState._snapshot`), so a start that finds a record with a mark this process did not
# set knows the record came back from a backup, or that Home Assistant stopped during one
SEQ_BACKUP_TOKEN: HassKey[str] = HassKey(f"{DOMAIN}_seq_backup_token")


class SeqStore(Store[dict[str, Any]]):
    """A `Store` that remembers the last payload it actually wrote to disk (`written`).

    `Store._async_handle_write_data` catches a `WriteError` (a full disk, a filesystem remounted read-only —
    the usual way an SD card dies) with only a log line, and by then it has already cleared its own pending
    data, so nothing else notices the write never landed. `HAState.reserve_seq` reads `written` to tell what a
    restart would actually load, so it can hold back sends the moment that stops matching what has been sent.
    `write_error` keeps the text of the last write's `WriteError` (None once a write lands) for the repair that
    names it (`seq_store_unwritable`) and the diagnostics.
    """

    written: dict[str, Any] | None = None
    write_error: str | None = None

    def __init__(
        self, hass: HomeAssistant, version: int, key: str, **kwargs: Any
    ) -> None:
        """Set up a `Store` at the sequence store's minor version (`SEQ_STORAGE_MINOR_VERSION`) unless told otherwise."""
        kwargs.setdefault("minor_version", SEQ_STORAGE_MINOR_VERSION)
        super().__init__(hass, version, key, **kwargs)

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> Any:
        """1.1 … 1.4 → 1.5: the records stay as they are (no `seq_guard`: no guard pending; no `in_backup`: no mark).

        No `mesh` part: the key refresh is read from the address's own record (`_stored_key_refresh`); no
        `address_shared`: no other client seen.
        """
        if old_major_version != STORAGE_VERSION:
            raise NotImplementedError
        return old_data

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        try:
            await super()._async_write_data(data)
        except WriteError as err:
            self.write_error = str(err)
            raise  # `Store` logs it; `written` still lags on a failure
        self.written = data["data"]
        self.write_error = None


def seq_store_for_uuid(hass: HomeAssistant, mesh_uuid: str) -> SeqStore:
    """Return the sequence-number store of a mesh, keyed by its (lower-cased) UUID directly.

    `seq_store` is the usual way in (it has a `CDB` to read the UUID from); this one is for the few callers that
    only have the UUID itself — `async_migrate_legacy_seq_store` runs before a hub, and so before a `CDB`, exists.

    One `Store` object per mesh UUID for the life of `hass` (cached in `SEQ_STORES`), not a fresh one per call:
    the config flow refuses a second entry for an already-configured mesh (`_mesh_uuid_taken`), but an
    installation upgraded from before that check — or a store edited by hand — could still have two live hubs
    for one mesh. `Store` never lets an older *scheduled* write clobber a newer one (a single pending-write slot,
    consumed once: see `homeassistant.helpers.storage.Store._async_handle_write_data`), but only within one
    object; two separate `Store` instances for the same key share none of that and would each queue its own
    write, the two racing to be the one the OS actually wrote last. Sharing the object at least serialises the
    two through the same slot; it does not make `HAState` aware of a sibling's address (`_addresses` is a
    snapshot taken when the hub was built, never refreshed — the guards against that are `_mesh_uuid_taken`, one
    entry per mesh, and `_refuse_duplicate_mesh`, one running hub per mesh, not anything here).

    `atomic_writes=True` (HA's `write_utf8_file_atomic`) fsyncs the temp file and the directory before the
    rename, unlike the default writer — the same reasoning as `LocalState._write`'s own atomic write. `private=True`
    keeps it owner-only (0600; HA writes 0644 otherwise): a record holds the new NetKey while a key refresh is
    followed (`LocalState.to_stored`). The `.backup` and `.floor` stores are created the same way.
    """
    stores = hass.data.setdefault(SEQ_STORES, {})
    key = mesh_uuid.lower()
    if key not in stores:
        stores[key] = SeqStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.seq.{key}",
            private=True,
            atomic_writes=True,
        )
    return stores[key]


def seq_store(hass: HomeAssistant, cdb: CDB) -> SeqStore:
    """Return the sequence-number store of a mesh: keyed by the mesh UUID, shared by its entries and addresses."""
    return seq_store_for_uuid(hass, cdb.mesh_uuid)


SEQ_BACKUP_STORES: HassKey[dict[str, SeqStore]] = HassKey(f"{DOMAIN}_seq_backup_stores")


def seq_backup_store(hass: HomeAssistant, cdb: CDB) -> SeqStore:
    """Return the mesh's `.backup` copy of its sequence-number store, one object per mesh UUID like `seq_store`.

    Mirrors `LocalState`'s `.bak`: `HAState.persist` refreshes it every time it forces an immediate write of the
    primary, and `JungHomeHub.async_create` falls back to it when the primary, or our address's record in it, is
    unusable (HA already renamed a corrupt primary aside and returned `None`; see `Store._async_load_data`).
    """
    return seq_backup_store_for_uuid(hass, cdb.mesh_uuid)


def seq_backup_store_for_uuid(hass: HomeAssistant, mesh_uuid: str) -> SeqStore:
    """`seq_backup_store` by the mesh UUID itself (the `seq_store_lost` repair has no `CDB`)."""
    stores = hass.data.setdefault(SEQ_BACKUP_STORES, {})
    key = mesh_uuid.lower()
    if key not in stores:
        stores[key] = SeqStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.seq.{key}.backup",
            private=True,
            atomic_writes=True,
        )
    return stores[key]


SEQ_FLOOR_STORES: HassKey[dict[str, SeqStore]] = HassKey(f"{DOMAIN}_seq_floor_stores")


def seq_floor_store_for_uuid(hass: HomeAssistant, mesh_uuid: str) -> SeqStore:
    """Return the mesh's repair floor, `.storage/junghome_ble.seq.<mesh uuid>.floor`, one object per mesh UUID.

    Per address, an (IV index, sequence number) the address is known to have reached: where the `seq_store_lost`
    repair continued from (`async_skip_seq_store_ahead`), where a restored record or an `iv_index_mismatch` rewind
    continued from, and — so it keeps up with the counter (review-4 S4-8) — where `HAState` was whenever its
    transmit index changed and every SEQ_FLOOR_EVERY numbers. The two copies of the store are what the repair
    replaces when they are lost; this file is not, so a second loss of both still knows where the address got to —
    without it, "nothing readable" meant SEQ_SKIP_UNKNOWN from 0 every time, the very numbers the address had sent
    since the first repair, and a floor written by the repair alone was outrun once the address had sent about
    SEQ_SKIP_UNKNOWN more. A record here also counts as history (`JungHomeHub.async_create`).
    """
    stores = hass.data.setdefault(SEQ_FLOOR_STORES, {})
    key = mesh_uuid.lower()
    if key not in stores:
        stores[key] = SeqStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.seq.{key}.floor",
            private=True,
            atomic_writes=True,
        )
    return stores[key]


def is_gateway_host(text: str) -> bool:
    """Whether `text` is an IPv4 address or a DNS host name: all a gateway address read off the mesh may be.

    It ends up in `https://{host}/...`, so nothing else (no port, path, user part or IPv6 literal) is taken.
    """
    if re.fullmatch(r"[0-9.]+", text):
        try:
            ipaddress.IPv4Address(text)
        except ValueError:
            return False
        return True
    return len(text) <= 253 and all(
        _HOST_LABEL.fullmatch(label) for label in text.split(".")
    )


def _newest_corrupt_seq_store(path: str) -> str | None:
    """Name of the newest `<path>.corrupt.<timestamp>` HA's storage layer renamed a JSON-decode failure to.

    Run in the executor: this is a directory listing, and `JungHomeHub.async_create` runs on the event loop.
    """
    matches = sorted(Path(path).parent.glob(f"{Path(path).name}.corrupt.*"))
    return matches[-1].name if matches else None


def _usable_record(data: Any, key: str) -> dict[str, Any] | None:
    """Address `key`'s record in a loaded store when `LocalState` can resume from it, else None (absent or unusable)."""
    try:
        record = data["addresses"][key]
        LocalState.parse_record({**record, "src": key})
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        return None
    return dict(record)


def _has_history(data: Any, key: str) -> bool:
    """Whether a loaded store says address `key` sent before: it holds a record for it, or is not a store at all."""
    if data is None:
        return False
    try:
        return key in data["addresses"]
    except (KeyError, IndexError, TypeError):
        return True  # something is there, but nothing says what: assume the worst


def _without_mark(record: Any, token: str | None) -> Any:
    """`record` without the mark of the backup `token` this process is taking (`HAState._snapshot` adds it back).

    A mark of any other backup stays: it says the record came back from that one, or that Home Assistant stopped
    during it, and the address skips ahead when it is next used (`_async_skip_restored_record`).
    """
    if token is None or not isinstance(record, dict):
        return record
    if record.get("in_backup") != token:
        return record
    return {name: value for name, value in record.items() if name != "in_backup"}


def _addresses_of(data: Any) -> dict[str, Any] | None:
    """Return the `addresses` map of a loaded store when it is one."""
    addresses = data.get("addresses") if isinstance(data, dict) else None
    return addresses if isinstance(addresses, dict) else None


def _mesh_of(*datas: Any) -> dict[str, Any]:
    """Return the mesh-level part (`{"mesh": …}`, minor version 4) of the first loaded store that has one, else {}."""
    for data in datas:
        mesh = data.get("mesh") if isinstance(data, dict) else None
        if isinstance(mesh, dict):
            return mesh
    return {}


def _store_with(data: Any, addresses: dict[str, Any]) -> dict[str, Any]:
    """Return a store holding `addresses`, with `data`'s mesh-level part (a rewrite of the records must not drop it)."""
    mesh = _mesh_of(data)
    return {"addresses": addresses, **({"mesh": mesh} if mesh else {})}


def _stored_key_refresh(data: Any, key: str) -> Any:
    """Return the key refresh a loaded store records for a hub at address `key` (stored form; None: there is none).

    The mesh-level one (review-4 S4-9: a key refresh belongs to the mesh, not to an address, so a new address after
    a followed refresh keeps its key), whatever it says, None included; a store an older version wrote has none and
    the address's own record holds it. A mesh-level one that does not parse falls back to the record's too.
    """
    mesh = _mesh_of(data)
    if "key_refresh" in mesh:
        stored = mesh["key_refresh"]
        try:
            if stored is not None:
                KeyRefreshRecord.from_stored(stored)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.warning(
                "The mesh's key refresh in the sequence-number store is unusable (%s): reading the address's own",
                err,
            )
        else:
            return stored
    record = _usable_record(data, key)
    return None if record is None else record.get("key_refresh")


def _landed(store: SeqStore, loaded: Any) -> Any:
    """Return what `store` durably holds: its last write that landed (`written`), else what was `loaded` from disk.

    A write still on its way, which `Store.async_load` hands back, is not on disk yet and may never be.
    """
    return loaded if store.written is None else store.written


def _furthest(records: Sequence[Any]) -> tuple[int, int] | None:
    """Return the furthest (transmit IV index, seq) of the `records` that still read as numbers in range, if any.

    Out of range is not a number a record can hold (review-4 S4-7): an IV index past 2^32 - 1 made the repair write
    a record no start could use, and a floor no later repair got past; a negative counter put the target at 0 under
    its index, over the numbers sent there.
    """
    best: tuple[int, int] | None = None
    for record in records:
        try:
            _check_range("IV index", int(record.get("iv_index", 0)), 0, IV_INDEX_MAX)
            _check_range("sequence number", int(record.get("seq", 0)), 0, SEQ_MAX)
            rank = _tx_rank(record)
        except (AttributeError, ValueError, TypeError):
            continue
        if best is None or rank > best:
            best = rank
    return best


def _seq_skip_target(records: Sequence[Any], floor: Any = None) -> tuple[int, int]:
    """(IV index, sequence number) to continue from when no record is usable: past the furthest one left.

    Whatever still reads as numbers in range counts, however broken the rest of its record; SEQ_SKIP_AHEAD beyond
    it. When nothing does, SEQ_SKIP_UNKNOWN from the last point known for sure: the floor entry (`floor`, as the
    `.floor` file durably holds it: `_landed`), else 0 — the floor also wins over records that are behind it. Never
    past `SEQ_TX_LIMIT`.
    """
    best = _furthest(records)
    known = _furthest([floor])
    if known is not None and (best is None or known > best):
        iv_index, seq = known[0], known[1] + SEQ_SKIP_UNKNOWN
    elif best is None:
        iv_index, seq = 0, SEQ_SKIP_UNKNOWN
    else:
        iv_index, seq = best[0], best[1] + SEQ_SKIP_AHEAD
    return max(iv_index, 0), min(max(seq, 0), SEQ_TX_LIMIT)


def _carried_seq_guard(records: Sequence[Any]) -> int:
    """Return the highest index a `seq_guard` of the `records` (a floor entry) names, else SEQ_GUARD_FIRST_BEACON."""
    guard = SEQ_GUARD_FIRST_BEACON
    for record in records:
        try:
            value = LocalState.parse_seq_guard(record)
        except (AttributeError, ValueError, TypeError):
            continue
        if value is not None:
            guard = max(guard, value)
    return guard


async def async_rewind_seq_floor(
    hass: HomeAssistant, mesh_uuid: str, key: str, iv_index: int, seq: int, guard: int
) -> bool:
    """Record in the repair floor where the `iv_index_mismatch` repair continues address `key` from (review-4 D10).

    Written before the address goes back to the mesh's `iv_index` (`JungHomeHub.async_rewind_iv_index`), like the
    `seq_store_lost` repair's own floor: should both copies of the store be lost later, that repair continues from
    here, under the mesh's index and with the guard over every index the address used while ahead (an entry at
    the old index would put it back ahead of the mesh). An earlier entry is merged in: the higher counter, and a
    guard over its index too. False when the write did not land.
    """
    floor_store = seq_floor_store_for_uuid(hass, mesh_uuid)
    floors = _addresses_of(await floor_store.async_load()) or {}
    known = _furthest([floors.get(key)])
    if known is not None:
        seq = max(seq, known[1])
        guard = max(guard, known[0], _carried_seq_guard([floors.get(key)]))
    data = {
        "addresses": {
            **floors,
            key: {"iv_index": iv_index, "seq": seq, "seq_guard": guard},
        }
    }
    await floor_store.async_save(data)
    return floor_store.written is data


async def async_skip_seq_store_ahead(
    hass: HomeAssistant, mesh_uuid: str, key: str
) -> int | None:
    """Write a record for address `key` past every number it may have sent (the `seq_store_lost` repair).

    The repair floor (`seq_floor_store_for_uuid`) is written first, then both copies of the mesh's store; the
    other addresses' records stay as they are. Returns the new counter; `HAState` adds the restart margin on top
    (the record is not marked clean). None, with nothing else written, when the floor's write did not land: a
    later repair would not know this one's numbers, so the setup stays refused rather than continue without it.
    """
    store = seq_store_for_uuid(hass, mesh_uuid)
    backup = seq_backup_store_for_uuid(hass, mesh_uuid)
    floor_store = seq_floor_store_for_uuid(hass, mesh_uuid)
    data = await store.async_load()
    backup_data = await backup.async_load()
    floors = _addresses_of(_landed(floor_store, await floor_store.async_load())) or {}
    records = [
        addresses.get(key)
        for addresses in (_addresses_of(data), _addresses_of(backup_data))
        if addresses is not None
    ]
    iv_index, seq = _seq_skip_target(records, floors.get(key))
    # a guard an earlier `iv_index_mismatch` repair left (`async_rewind_seq_floor`): numbers were sent under every
    # index up to it, so it stays — the first beacon raises it to its own index + 1 if that is higher
    guard = _carried_seq_guard([floors.get(key)])
    floor_entry: dict[str, Any] = {"iv_index": iv_index, "seq": seq}
    if guard != SEQ_GUARD_FIRST_BEACON:
        floor_entry["seq_guard"] = guard
    floor = {"addresses": {**floors, key: floor_entry}}
    await floor_store.async_save(floor)
    if floor_store.written is not floor:
        _LOGGER.error(
            "Could not write the sequence-number floor of address %s: not skipping ahead without it",
            key,
        )
        return None
    base = _addresses_of(data) or _addresses_of(backup_data) or {}
    new = _store_with(
        data if _mesh_of(data) else backup_data,
        {
            **base,
            key: {
                "seq": seq,
                "iv_index": iv_index,
                "iv_update_active": False,
                "iv_known": False,  # the first authenticated beacon decides
                "rpl": {},
                "clean": False,
                # whatever was lost was sent under an index nobody knows: no restart at 0 under any index up to
                # the first beacon's (`LocalState.apply_beacon`)
                "seq_guard": guard,
            },
        },
    )
    await store.async_save(new)
    await backup.async_save(new)
    _LOGGER.warning(
        "Sequence numbers of address %s continue from %06X (IV index %d), past any it may have sent",
        key,
        seq,
        iv_index,
    )
    return seq


async def _async_skip_restored_record(
    hass: HomeAssistant,
    mesh_uuid: str,
    key: str,
    data: dict[str, Any] | None,
    backup_data: dict[str, Any] | None,
    floor_data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Continue past a record restored from a Home Assistant backup (review-4 D5); return the store to start from.

    A backup restores the whole configuration directory, so the store, its `.backup` copy and the floor come back
    together, readable, `clean` or not, and nothing else could tell that every number sent since the backup was
    taken is in use: the start resumed below them, and an IV Update since then restarted the counter at 0 under
    the index they went out with. Every record written while a backup is being taken carries its mark (`in_backup`,
    `backup.async_pre_backup`); one that carries a mark this process did not set (a reload during the backup is no
    restore) is rewritten as the `seq_store_lost` repair does: SEQ_SKIP_AHEAD past the further copy (never past
    `SEQ_TX_LIMIT`), not `clean`, `seq_guard` pending the first beacon, the rest kept. A concrete guard the record,
    its copy or the floor already holds (an `iv_index_mismatch` rewind: numbers went out under every index up to
    it) is kept instead, with the index stored as not known, so the first beacon still raises it to one past its
    own index (`LocalState.apply_beacon`) rather than leaving it below the indexes used since. The floor first, then both
    copies, all before the `HAState` is built. A Home Assistant that stopped during a backup leaves the mark too:
    its next start skips ahead once, for nothing.

    Not ready, with nothing else written, when the floor's write does not land: a later loss of both copies would
    not know these numbers.
    """
    record = _usable_record(data, key)
    if record is None:
        return data
    mark = record.get("in_backup")
    if mark is None or mark == hass.data.get(SEQ_BACKUP_TOKEN):
        return data
    other = _usable_record(backup_data, key)
    if other is not None and _tx_rank(other) > _tx_rank(record):
        record = other
    tx, seq = _tx_rank(record)
    seq = min(seq + SEQ_SKIP_AHEAD, SEQ_TX_LIMIT)
    floors = _addresses_of(floor_data) or {}
    guard = _carried_seq_guard([record, other, floors.get(key)])
    _LOGGER.warning(
        "The sequence-number record of address %s was restored from a backup, or Home Assistant stopped during "
        "one: its numbers continue from %06X, past any sent since",
        key,
        seq,
    )
    floor_store = seq_floor_store_for_uuid(hass, mesh_uuid)
    known = _furthest([floors.get(key)])
    entry: dict[str, Any] = (
        {"iv_index": tx, "seq": seq}
        if known is None or (tx, seq) > known
        else dict(floors[key])
    )
    if guard != SEQ_GUARD_FIRST_BEACON:
        entry["seq_guard"] = guard
    floor = {"addresses": {**floors, key: entry}}
    await floor_store.async_save(floor)
    if floor_store.written is not floor:
        _LOGGER.error(
            "Could not write the sequence-number floor of address %s: not starting from a restored record "
            "without it",
            key,
        )
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="seq_floor_not_written"
        )
    restored = {name: value for name, value in record.items() if name != "in_backup"}
    data = _store_with(
        data,
        {
            **(_addresses_of(data) or {}),
            key: {
                **restored,
                "seq": seq,
                "clean": False,
                "seq_guard": guard,
                **({} if guard == SEQ_GUARD_FIRST_BEACON else {"iv_known": False}),
            },
        },
    )
    await seq_store_for_uuid(hass, mesh_uuid).async_save(data)
    await seq_backup_store_for_uuid(hass, mesh_uuid).async_save(data)
    return data


async def async_apply_followed_key_refresh(
    hass: HomeAssistant, cdb: CDB, unicast: int
) -> None:
    """Put the NetKey of a key refresh the hub followed to its end in place of the export's stale one (review-3 N2b).

    The client stores the new key with the sequence numbers (`LocalState.key_refresh`, phase 3; at mesh level, so
    whichever address the hub uses: `_stored_key_refresh`) until the export holds it. Without this a setup would
    look for proxies of the old Network ID (none left) and talk with the revoked key. Only the in-memory `cdb` changes; the file is the gateway's or the user's. An export
    written mid key refresh (`CDB.net_key_refresh`) whose old key is the followed one is newer: it is left alone.

    Only a completion the client proved (review-4 D4: the proxy's beacon under the new key, or the nodes' own
    statuses) is put in: one without proof was recorded before proofs were kept or forged by a node, and the client
    takes its key up as a candidate only (`KeyRefreshFollower.resume`).
    """
    data = await seq_store(hass, cdb).async_load()
    stored = _stored_key_refresh(data, f"{unicast:04X}")
    if stored is None:
        return
    refresh = KeyRefreshRecord.from_stored(stored)
    if refresh.phase != 3:
        return
    if not refresh.proven:
        _LOGGER.warning(
            "The stored key refresh is complete but unproven (recorded before proofs were kept, or forged by a "
            "node): keeping the export's network key"
        )
        return
    key = refresh.key
    exported = cdb.net_key_refresh.get(NET_KEY_INDEX)
    if exported is not None and exported[0].key == key:
        return
    if key != cdb.net_keys[NET_KEY_INDEX].key:
        _LOGGER.info(
            "Using the network key of the completed key refresh; the export still has the old one"
        )
        cdb.net_keys[NET_KEY_INDEX] = NetKeyMaterial.derive(key)
    # the export's own refresh, if it caught one, is over too: the old key is revoked
    cdb.net_key_refresh.pop(NET_KEY_INDEX, None)


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


def next_utc_offset_change(now: datetime) -> datetime | None:
    """Return the first moment (UTC) after `now` at which `now`'s time zone changes its UTC offset, within a year.

    Found by stepping a day at a time, then bisecting the day to the second: zoneinfo keeps its transitions to
    itself. None when the offset does not change within the year (no daylight-saving time).
    """
    tz = now.tzinfo
    assert tz is not None
    offset = now.utcoffset()
    step = timedelta(days=1)
    probe = now.astimezone(UTC)
    for _ in range(OFFSET_SEARCH_DAYS):
        nxt = probe + step
        if nxt.astimezone(tz).utcoffset() != offset:
            low, high = probe, nxt  # the change lies after `low`, at or before `high`
            while high - low > timedelta(seconds=1):
                mid = low + (high - low) / 2
                if mid.astimezone(tz).utcoffset() == offset:
                    low = mid
                else:
                    high = mid
            return high.replace(microsecond=0)
        probe = nxt
    return None


def _report_seq_store_lost(
    hass: HomeAssistant,
    entry: ConfigEntry,
    mesh_uuid: str,
    key: str,
    corrupt: str | None,
) -> NoReturn:
    """Refuse to start an address with history from 0 (nonce reuse); offer the skip-ahead as a repair."""
    _LOGGER.error(
        "Neither copy of the sequence-number record of address %s is usable (%s): starting it at 0 would reuse "
        "nonces already sent — repair the issue to continue past them, or restore the store",
        key,
        f"the corrupt store was saved as {corrupt}" if corrupt else "both unreadable",
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry, ISSUE_SEQ_STORE_LOST),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_SEQ_STORE_LOST,
        learn_more_url=learn_more_url(ISSUE_SEQ_STORE_LOST),
        translation_placeholders={"title": entry.title, "unicast": key},
        data={"entry_id": entry.entry_id, "mesh_uuid": mesh_uuid, "unicast": key},
    )
    raise ConfigEntryError(
        translation_domain=DOMAIN,
        translation_key="seq_store_lost",
        translation_placeholders={"unicast": key},
    )


def _tx_rank(record: dict[str, Any]) -> tuple[int, int]:
    """(transmit IV index, seq) of a stored record, for ranking which of two is further along.

    The transmit index is `iv_index - 1` while `iv_update_active` — nodes keep sending with the old index
    during an update — so ranking by the stored `iv_index` alone can prefer a record that is actually behind:
    `{iv_index: 5, iv_update_active: True, seq: 100}` (transmits under IV 4) would outrank
    `{iv_index: 5, iv_update_active: False, seq: 50}` (transmits under IV 5) even though the second is the one
    further along.
    """
    iv = int(record.get("iv_index", 0))
    return (iv - 1 if record.get("iv_update_active") else iv, int(record.get("seq", 0)))


def merge_legacy_seq_store(
    data: dict[str, Any] | None, old: dict[str, Any]
) -> dict[str, Any]:
    """Fold a pre-0.3 per-entry store (one address: `{src, seq, iv_index, iv_update_active}`) into the per-mesh map.

    Two records for the same address (another entry of the same mesh already migrated, or a reconfigured address
    coming back): the one further along by transmit IV index, then sequence number, is the one to continue.
    """
    addresses = dict((data or {}).get("addresses") or {})
    src = str(old["src"]).upper()
    record = {key: value for key, value in old.items() if key != "src"}
    have = addresses.get(src)
    if have is None or _tx_rank(record) >= _tx_rank(have):
        addresses[src] = record
    return _store_with(data, addresses)


async def async_migrate_legacy_seq_store(
    hass: HomeAssistant, entry: ConfigEntry, mesh_uuid: str
) -> bool:
    """Fold a 0.2 per-entry sequence-number store (`junghome_ble.<entry_id>`) into its mesh's store, then remove it.

    Called from `async_setup_entry` right after `load_network` succeeds, before the not-ready check that can
    retry indefinitely without ever reaching `JungHomeHub.async_create` (HAC-06: an entry stuck in that retry
    never migrated, and removing it then stranded the legacy record — the very record a remove-and-re-add is
    supposed to preserve through `seq_store`); `async_create` also calls it, a no-op by the time it does; and
    `async_remove_entry` calls it too, so an entry that never got this far still migrates before its data is
    gone. A legacy record is deleted only after it has been saved into its mesh's store — never the reverse.

    "Saved" means the write landed (`SeqStore.written`): `Store.async_save` returns normally when the write
    failed (it only logs the `WriteError`) and writes nothing at all while Home Assistant is stopping. False
    when the legacy record is still there for that reason — the mesh's store does not hold its numbers yet.
    """
    legacy: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
    )
    old = await legacy.async_load()
    if old is None:
        return True
    store = seq_store_for_uuid(hass, mesh_uuid)
    data = merge_legacy_seq_store(await store.async_load(), old)
    await store.async_save(data)
    if store.written is not data:
        _LOGGER.warning(
            "Could not save the sequence-number record of address %s into the mesh's store; keeping %s",
            old.get("src"),
            legacy.path,
        )
        return False
    await legacy.async_remove()
    _LOGGER.info(
        "Moved the sequence-number record of address %s into the mesh's store",
        old.get("src"),
    )
    return True


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


class AddressShared(SequenceStalled):
    """Another client sends from our address: no number is handed out until the `address_shared` repair skips past it.

    A `SequenceStalled` so whatever treats a refusal as "cannot send now" rather than as a lost link or real
    exhaustion does here too (the link stays, receive-only; a keep-alive refused is no verdict); not retried by
    `JungHomeHub._while_seq_stalls`, which only waits for a store to catch up — this one waits for the user.
    """


def _stored_address_shared(record: Any) -> tuple[int, int] | None:
    """Return the (IV index, seq) another client sent from the address, as its record holds it; None without one.

    A value that is not one (a store edited by hand) is dropped with a WARNING rather than failing the start: the
    next PDU of the other client raises the issue again.
    """
    raw = record.get("address_shared") if isinstance(record, dict) else None
    if raw is None:
        return None
    try:
        iv_index, seq = (int(value) for value in raw)
        _check_range("IV index", iv_index, 0, IV_INDEX_MAX)
        _check_range("sequence number", seq, 0, SEQ_MAX)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Ignoring an unusable record of another client on our address: %r", raw
        )
        return None
    return iv_index, seq


class HAState(LocalState):
    """Sequence-number store backed by HA's storage helper: one store per mesh, one counter per address ever used.

    The nonce of every PDU we send is (SRC, SEQ, IV index), so the counter has to outlive everything that can be
    re-created for the same mesh and address: the config entry (remove + add is the standard troubleshooting step)
    and the address itself (reconfigured away and back). The store is keyed by the mesh UUID (`seq_store`; it
    survives a key refresh, the Network ID does not) and maps every address this Home Assistant ever sent from in
    that mesh to what `LocalState.to_stored` holds for it (without the address: the counter, the IV state and the
    replay list): `{"addresses": {"0D00": {"seq": …, "iv_index": …, "iv_update_active": …, "rpl": …, "clean": …}}}`.
    What belongs to the mesh rather than to an address sits next to it, `{"mesh": {"key_refresh": …}}`: the key
    refresh followed (review-4 S4-9) — a new address after one keeps its key, and a copy in our own record keeps
    it for an older reader. An address the store does not know starts where `JungHomeHub.async_create` decides
    (`_evidence_of_use`): 0, or SEQ_SKIP_AHEAD when it may have sent before.

    Another client seen sending from the address (`address_shared`, review-4 S I2) is stored with its record, so a
    restart does not resume sending into that client's numbers: every send is refused (`AddressShared`) until the
    `address_shared` repair skips past them (`skip_past_shared`).

    With a `floor` (the mesh's `.floor` file) the floor entry of our address keeps up with the counter (review-4
    S4-8): a new one whenever the transmit index moved past the entry's and every SEQ_FLOOR_EVERY numbers, and
    sends wait for the file to durably hold our transmit index and never run SEQ_SKIP_UNKNOWN past its number
    (`_limit`) — the index and the distance the `seq_store_lost` repair continues from when both copies of the store
    are lost. Without that, the floor was only the last repair's, and the repair after a second loss reused every
    number sent beyond that distance, or under an index above the floor's when the first beacon after it was lower.

    Margin arithmetic: a change schedules a save 2 s after the *last* change (`Store.async_delay_save` moves a
    pending write to the latest time asked for, so a connect-time burst on a large installation defers it for as
    long as the burst lasts), and every SEQ_SAVE_EVERY numbers a write is started at once, outside that debounce.
    A crash therefore loses at most SEQ_SAVE_EVERY numbers plus the handful sent while that write is on its way —
    well inside the SEQ_RESTART_MARGIN added to a counter found in use. A counter closed cleanly (`async_close`,
    the last thing `JungHomeHub.async_stop` does, after the link is gone) is marked `clean` and continues without
    the margin: a reload (options, a fresh export, a reconfiguration) then costs none of the 16.7 M numbers per IV
    index. The first save after a load is an immediate one that clears the mark again, so a crash right after a
    start is covered by the margin too.
    """

    def __init__(
        self,
        store: SeqStore,
        data: dict[str, Any] | None,
        default_src: int,
        key: str,
        backup: SeqStore | None = None,
        entry_id: str | None = None,
        floor: SeqStore | None = None,
    ) -> None:
        """Wrap `store` (and, if given, its `.backup` copy and the `.floor`) and continue the configured address.

        `key` is the mesh UUID `seq_store` keys `store` by. Claiming ownership of it (`SEQ_OWNERS`) here, before
        `super().__init__` runs `persist()` for the first time, makes this the one `HAState` allowed to write —
        a reload can start a successor while `JungHomeHub.async_stop` is still finishing an old one behind it
        (`ConfigEntry._async_process_on_unload`'s 10 s wait does not block the reload), and the old one's
        `persist()`/`async_close()` must not overwrite what the new one has already sent (HAC-02). `entry_id`
        names the config entry whose hub this is: a successor must be of the same entry (`JungHomeHub.async_create`
        refuses another entry's hub for a mesh that already has a running one).
        """
        self._store = store
        self._key = key
        self.entry_id = entry_id
        self._backup_store = backup
        self._floor_store = floor
        # (tx IV index, seq) of the floor write last asked for: another is asked SEQ_FLOOR_EVERY on, or by a stall
        self._floor_asked: tuple[int, int] | None = None
        # `_restart_point` per copy (allow_clean: True = the store, False = the backup): the `written` object it was
        # read from (kept, so its id cannot be reused by another), our address then, and the point (None: no record)
        self._restart_points: dict[bool, tuple[Any, int, tuple[int, int] | None]] = {}
        hass = store.hass
        hass.data.setdefault(SEQ_OWNERS, {})[key] = self
        # a backup being taken right now (`backup.async_pre_backup`): its mark goes into every record this one
        # writes too, or a reload during the backup would leave the archive a record without it
        self.backup_token: str | None = hass.data.get(SEQ_BACKUP_TOKEN)
        self._addresses: dict[str, dict[str, Any]] = {
            src: _without_mark(record, self.backup_token)
            for src, record in ((data or {}).get("addresses") or {}).items()
        }
        self._closed = False
        self._saved: tuple[int, int] | None = (
            None  # (tx IV index, seq) of the last forced save
        )
        self._stalled_at: float | None = (
            None  # monotonic time reserve_seq() last forced a save while refusing (the retry throttle)
        )
        # monotonic time reserve_seq() first refused since the last one that succeeded; told to `stall_listener`
        # (the hub: it times `report_unwritable`, and cancels that timer when it stops)
        self._stalled_since: float | None = None
        # seconds of the stalls that ended (`held_back_total`: the link history's per-link share)
        self._stalled_before = 0.0
        self.stall_listener: Callable[[], None] | None = None
        # `seq_store_unwritable` was raised and not cleared since (the hub's stop clears it too, `_clear_issues`)
        self._stall_issue_open = False
        self._mesh = _mesh_of(data)
        src = f"{default_src:04X}"
        record = self._addresses.get(src)
        # (IV index, seq), the highest another client was seen sending from our address with; None: none seen (or
        # skipped past). Set before `super().__init__`, whose first `persist()` writes it back
        self.address_shared = _stored_address_shared(record)
        self._record = (
            None
            if record is None
            else {**record, "src": src, "key_refresh": _stored_key_refresh(data, src)}
        )
        margin = 0 if record is not None and record.get("clean") else SEQ_RESTART_MARGIN
        super().__init__(
            None, default_src, restart_margin=margin, configured_src_wins=True
        )

    def _owns_the_store(self) -> bool:
        """Whether this is still the mesh's current `HAState` (HAC-02): a superseded one must never write again."""
        return self._store.hass.data.get(SEQ_OWNERS, {}).get(self._key) is self

    def load(self) -> dict[str, Any] | None:
        """Return the configured address's record (with the address), as the store held it when the hub was created.

        Its key refresh is the mesh's (`_stored_key_refresh`).
        """
        return self._record

    def persist_now(self) -> None:
        """Start a write of both copies now rather than after the 2 s debounce (`set_key_refresh`)."""
        self._saved = None  # force the immediate-save branch
        self.persist()

    def persist(self) -> None:
        """Schedule a save: debounced by 2 s, or started at once every SEQ_SAVE_EVERY numbers, on an IV change and after a load.

        Called by `LocalState.__init__` too (the store is set before that runs): that first call is an immediate
        one, clearing the `clean` mark of a store that was closed properly. The immediate write is a task of its
        own (`Store.async_save`), not a zero delay: a pending delayed write would swallow the latter.

        `_addresses` (every *other* address' record) is the snapshot the store held when this `HAState` was
        built and is never refreshed: `seq_store` hands out one `Store` object per mesh (`SEQ_STORES`) so two
        live hubs of one mesh — the config flow refuses a second entry for an already-configured mesh
        (`_mesh_uuid_taken`), but a store edited by hand, or an installation upgraded from before that check,
        could still have two — take turns *scheduling* writes rather than racing independent files (`Store`
        never lets an older *scheduled* write land after a newer one, `homeassistant.helpers.storage.Store`'s
        single pending-write slot), but neither hub's `_addresses` ever learns the other moved: one running hub
        per mesh (`_refuse_duplicate_mesh`) is what actually avoids the race, not this store.

        A no-op once a successor `HAState` has taken over `_key` (HAC-02): the only numbers this instance has
        not itself persisted are fewer than `SEQ_SAVE_EVERY` plus whatever was in flight, all below what the
        successor loaded (it added `SEQ_RESTART_MARGIN`), so silently dropping this write is safe.
        """
        if not self._owns_the_store():
            return
        self._closed = False
        current = (self.tx_iv_index, self.seq)
        if (
            self._saved is None
            or current[0] != self._saved[0]
            or current[1] - self._saved[1] >= SEQ_SAVE_EVERY
        ):
            self._saved = current
            self._store.hass.async_create_task(
                self._store.async_save(self._snapshot()), f"{DOMAIN} seq store"
            )
            if self._backup_store is not None:
                # always `clean: False`: a restore from the backup must add the margin, it can lag by up to
                # SEQ_SAVE_EVERY plus whatever was in flight when the primary was last written
                self._backup_store.hass.async_create_task(
                    self._backup_store.async_save(self._snapshot(force_dirty=True)),
                    f"{DOMAIN} seq store backup",
                )
        else:
            self._store.async_delay_save(self._snapshot, 2)
        self._advance_floor()

    def _floor_point(self) -> tuple[int, int]:
        """(tx IV index, seq) of the floor entry the `.floor` file durably holds for us; (0, 0) without one.

        Without one, a repair continues SEQ_SKIP_UNKNOWN from 0 (`_seq_skip_target`): the same as an entry there.
        """
        written = None if self._floor_store is None else self._floor_store.written
        point = _furthest([(_addresses_of(written) or {}).get(f"{self.src:04X}")])
        return (0, 0) if point is None else point

    def _advance_floor(self) -> None:
        """Ask for a new floor entry when the counter moved past the one on disk (`SEQ_FLOOR_EVERY`, the class docstring).

        Never one behind it (a restored record or a rewind can leave the counter there). A concrete `seq_guard` the
        entry or the counter holds that still covers our index is carried: numbers went out under every index up to
        it (`async_rewind_seq_floor`). Written at once, as a task; `_limit` holds sends back until one lands, should
        the counter otherwise run too far past the last one that did.
        """
        if self._floor_store is None:
            return
        current = (self.tx_iv_index, self.seq)
        landed = self._floor_point()
        if current[0] < landed[0]:
            return
        for point in (landed, self._floor_asked):
            if (
                point is not None
                and current[0] == point[0]
                and current[1] - point[1] < SEQ_FLOOR_EVERY
            ):
                return
        self._floor_asked = current
        key = f"{self.src:04X}"
        floors = _addresses_of(self._floor_store.written) or {}
        entry: dict[str, Any] = {"iv_index": current[0], "seq": current[1]}
        guard = _carried_seq_guard([floors.get(key), {"seq_guard": self.seq_guard}])
        if guard >= current[0]:
            entry["seq_guard"] = guard
        self._floor_store.hass.async_create_task(
            self._floor_store.async_save({"addresses": {**floors, key: entry}}),
            f"{DOMAIN} seq floor",
        )

    def _limit(self) -> tuple[int, int]:
        """Return the (tx IV index, seq) every restart could continue from, given what both copies *durably* hold.

        A restart resumes from the store, or from the `.backup` copy when the store is lost or damaged (review-3
        S1), so both bound what may be sent: each copy's restart point is taken (`_restart_point`) and the lower
        one wins — a copy on another transmit index than ours is no bound at all, and holds sends back like a
        store that has written nothing. The backup's writes fail on their own (EIO on its file, a full disk
        between the two writes): bounding by the store alone let sends run on while the copy stayed behind, and
        a restore from it after the store was lost reused every number in between (found by the property tests'
        state machine).
        """
        tx, limit = self._restart_point(self._store.written, allow_clean=True)
        if self._backup_store is not None:
            backup_tx, backup_limit = self._restart_point(
                self._backup_store.written, allow_clean=False
            )
            if backup_tx != tx:
                return self.tx_iv_index, 0
            limit = min(limit, backup_limit)
        if self._floor_store is not None:
            floor_tx, floor_seq = self._floor_point()
            if floor_tx < tx:
                # a repair after both copies are lost continues under the floor's index, and its guard keeps the
                # counter going only up to one past the first beacon's — which may lie below ours (S4-8)
                return tx, 0
            if floor_tx == tx:
                # ... and this far past the floor's number; a floor ahead of us bounds nothing here
                limit = min(limit, floor_seq + SEQ_SKIP_UNKNOWN)
        return tx, limit

    def _restart_point(self, written: Any, *, allow_clean: bool) -> tuple[int, int]:
        """(tx IV index, seq) a restart from one copy's durably written content continues from.

        Nothing durable, nothing (usable) for our address: a restart from it starts at 0, so nothing may be
        reserved until a save lands. A `clean` record needs no margin (that is what marks a clean close) — in the
        store only: a restore from the backup always adds it, as `JungHomeHub.async_create` reads it (the copy is
        written with `clean: False`, `_snapshot`). Any other record gets `SEQ_RESTART_MARGIN`, exactly as a real
        restart's `LocalState.load()` would add.

        Read once per written content (review-4 R4-9): checking the record parses the whole replay list, and
        `reserve_seq` asks for every PDU sent — a quarter of a millisecond with 600 sources, twice. A write never
        edits what landed before, it replaces `written` (`SeqStore._async_write_data`, the setup's own
        `store.written = data`), so the object it is read from tells whether the point still holds.
        """
        cached = self._restart_points.get(allow_clean)
        if cached is not None and cached[0] is written and cached[1] == self.src:
            point = cached[2]
        else:
            record = _usable_record(written, f"{self.src:04X}")
            if record is None:
                point = None
            else:
                tx, seq = _tx_rank(record)
                clean = allow_clean and bool(record.get("clean"))
                point = (tx, seq + (0 if clean else SEQ_RESTART_MARGIN))
            self._restart_points[allow_clean] = (written, self.src, point)
        return (self.tx_iv_index, 0) if point is None else point

    def reserve_seq(self, count: int) -> int:
        """Refuse to hand out numbers a restart could not tell were already used (HAC-05).

        `LocalState.reserve_seq` alone assumes every number it hands out is durably written soon after; nothing
        enforced that here, so a stalled or failing store write let sends run arbitrarily far ahead of what a
        restart would actually load, reusing nonces once it did. Retried every `SEQ_STALL_RETRY` seconds instead
        of once, because the immediate save this forces is itself async — see `persist`.

        A superseded `HAState` (HAC-02) refuses outright: it can no longer persist what it hands out, and `_limit`
        reads the shared store's `written`, which the successor keeps advancing into the numbers it sends with.

        The first refusal since a number was last handed out starts a stall (`stalled_for`), which `stall_listener`
        (the hub) hears of, to raise `seq_store_unwritable` if it lasts; the next number handed out ends it.
        """
        if not self._owns_the_store():
            raise SequenceExhausted(
                "a newer hub of this mesh owns the sequence numbers now"
            )
        if self.address_shared is not None:
            raise AddressShared(
                f"another client sends from address {self.src:04X}: nothing is sent until the repair skips past it"
            )
        if self._held_back(count):
            now = time.monotonic()
            if self._stalled_since is None:
                self._stalled_since = now
                if self.stall_listener is not None:
                    self.stall_listener()
            if self._stalled_at is None or now - self._stalled_at >= SEQ_STALL_RETRY:
                if self._stalled_at is None:
                    _LOGGER.warning(
                        "Sequence-number store not written yet: holding back sends so no nonce is reused"
                    )
                self._stalled_at = now
                self._saved = None  # force the immediate-save branch
                self._floor_asked = (
                    None  # and a floor write, should that be what holds sends back
                )
                self.persist()
            raise SequenceStalled(
                "sequence-number store not written yet: holding back to keep nonces unique"
            )
        self._stalled_at = None
        if self._stalled_since is not None or self._stall_issue_open:
            self._end_stall()
        return super().reserve_seq(count)

    def _held_back(self, count: int) -> bool:
        """Whether `count` more numbers lie beyond what a restart could continue from (`_limit`)."""
        tx, limit = self._limit()
        return self.tx_iv_index != tx or self.seq + count > limit

    def report_unwritable(self) -> None:
        """Raise `seq_store_unwritable` if sends are still held back (the hub calls it SEQ_STALL_ISSUE_AFTER into a stall).

        Without it a store that never lands a write (a full disk, an SD card remounted read-only) only showed as
        `pdus_dropped` once the proxy filter went unanswered, and that repair's skip-ahead cannot be written either.
        A store that caught up meanwhile, with nothing sent since, ends the stall here instead; a superseded
        `HAState` (HAC-02) reports nothing, its successor has the store now.
        """
        if self._stalled_since is None or not self._owns_the_store():
            return
        if not self._held_back(1):
            self._close_stall()
            return
        path, error = self._stall_cause()
        _LOGGER.error(
            "The sequence-number store %s has not been written for %.0f s (%s): Home Assistant sends nothing to "
            "the mesh until a write lands",
            path,
            time.monotonic() - self._stalled_since,
            error or "no error reported",
        )
        hass = self._store.hass
        entry = (
            None
            if self.entry_id is None
            else hass.config_entries.async_get_entry(self.entry_id)
        )
        if entry is None:
            return
        self._stall_issue_open = True
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry, ISSUE_SEQ_STORE_UNWRITABLE),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_SEQ_STORE_UNWRITABLE,
            learn_more_url=learn_more_url(ISSUE_SEQ_STORE_UNWRITABLE),
            translation_placeholders={
                "title": entry.title,
                "path": path,
                "error": error or "none reported",
            },
        )

    def _stall_cause(self) -> tuple[str, str | None]:
        """(path, last write error) of the copy holding sends back: the first one whose last write failed (or the floor's)."""
        stores = [self._store]
        if self._backup_store is not None:
            stores.append(self._backup_store)
        if self._floor_store is not None:
            stores.append(self._floor_store)
        for store in stores:
            if store.write_error is not None:
                return store.path, store.write_error
        return self._store.path, None

    def _end_stall(self) -> None:
        """End a stall once a number was handed out: the store caught up, so its repair goes."""
        if self._stalled_since is not None:
            _LOGGER.info(
                "Sequence-number store written again after %.0f s: sending resumes",
                time.monotonic() - self._stalled_since,
            )
        self._close_stall()
        if self._stall_issue_open and self.entry_id is not None:
            ir.async_delete_issue(
                self._store.hass,
                DOMAIN,
                f"{ISSUE_SEQ_STORE_UNWRITABLE}_{self.entry_id}",
            )
        self._stall_issue_open = False

    def _close_stall(self) -> None:
        """Add the stall that ends now to `held_back_total`."""
        if self._stalled_since is not None:
            self._stalled_before += time.monotonic() - self._stalled_since
        self._stalled_since = None

    @property
    def held_back_total(self) -> float:
        """Seconds sends were held back in all, the running stall included (the hub's link history takes differences)."""
        return self._stalled_before + (self.stalled_for or 0.0)

    @property
    def stalled_for(self) -> float | None:
        """Seconds since sends were first held back, None while they are not (the hub's watchdog, diagnostics)."""
        if self._stalled_since is None:
            return None
        return time.monotonic() - self._stalled_since

    @property
    def last_write_error(self) -> str | None:
        """The last `WriteError` of the store or its `.backup` copy (`_stall_cause`); None when both last wrote."""
        return self._stall_cause()[1]

    @property
    def durable_headroom(self) -> int:
        """How many more numbers may go out before a send is held back for the store to catch up (`_limit`)."""
        tx, limit = self._limit()
        return max(limit - self.seq, 0) if tx == self.tx_iv_index else 0

    def skip_ahead(self, count: int) -> int:
        """Move the counter `count` numbers ahead (never past `SEQ_TX_LIMIT`), saved at once; return the new counter.

        Sends are held back until that save lands (`reserve_seq`), so nothing goes out that a restart could reuse.
        A counter past `SEQ_TX_LIMIT` already (its last number sent) stays: capping it moved it back onto that
        number (found by the property tests' state machine).
        """
        self.seq = max(self.seq, min(self.seq + count, SEQ_TX_LIMIT))
        self._saved = None  # force the immediate-save branch
        self.persist()
        return self.seq

    def note_address_shared(self, iv_index: int, seq: int) -> bool:
        """Record that another client sent (`iv_index`, `seq`) from our address; True when it is the first such record.

        Saved at once with the counter: from here on `reserve_seq` refuses (`AddressShared`), across a restart too.
        """
        seen = self.address_shared
        if seen is None or (iv_index, seq) > seen:
            self.address_shared = (iv_index, seq)
            self._saved = None  # force the immediate-save branch
            self.persist()
        return seen is None

    def skip_past_shared(self) -> int | None:
        """Continue past the other client's numbers (the `address_shared` repair); return the new counter, None if none.

        The counter goes SEQ_RESTART_MARGIN past the highest number seen (never past SEQ_MAX, which is never sent):
        the other client may have sent a few more since, and the nodes' replay lists know them. Seen under the next
        IV index (the mesh is in an IV Update, the other client already transmits under the new index), `seq_guard`
        keeps the counter from restarting at 0 there. Saved at once; sends wait for that save (`reserve_seq`).
        """
        seen = self.address_shared
        if seen is None:
            return None
        iv_index, seq = seen
        self.seq = max(self.seq, min(seq + 1 + SEQ_RESTART_MARGIN, SEQ_MAX))
        if iv_index > self.tx_iv_index:
            if self.seq_guard is None:
                self.seq_guard = iv_index
            elif self.seq_guard != SEQ_GUARD_FIRST_BEACON:
                self.seq_guard = max(self.seq_guard, iv_index)
            # else: the first beacon raises it past the index it states, this one at least
        self.address_shared = None
        self._saved = None  # force the immediate-save branch
        self.persist()
        return self.seq

    async def async_close(self) -> None:
        """Write the counter now, marked cleanly closed: the next load of this address needs no restart margin.

        A no-op once superseded (HAC-02): a slow `async_stop` finishing after a reload's successor has already
        started must not write its own, now-stale, counter over the successor's — see `persist`.
        """
        if not self._owns_the_store():
            return
        self._closed = True
        await self._store.async_save(self._snapshot())

    def _snapshot(self, *, force_dirty: bool = False) -> dict[str, Any]:
        """Return the whole store: every address's record, ours from the live state.

        `force_dirty` is for the backup copy: it must never claim a clean close, since it is always somewhat
        behind the primary and a restart from it must add the margin regardless of how this hub actually ended.

        While a backup is being taken (`backup_token`) every record carries its mark, the other addresses' too: an
        address switched to after the backup and back again before a restore comes back just as stale as ours. A
        record that already carries an earlier backup's mark keeps it (it skips ahead either way when next used).
        """
        record = {key: value for key, value in self.to_stored().items() if key != "src"}
        record["clean"] = False if force_dirty else self._closed
        if self.address_shared is not None:
            record["address_shared"] = list(self.address_shared)
        addresses = {**self._addresses, f"{self.src:04X}": record}
        if (token := self.backup_token) is not None:
            addresses = {
                src: {"in_backup": token, **other} if isinstance(other, dict) else other
                for src, other in addresses.items()
            }
        # the mesh's key refresh, whatever it is (None too: it ends a stale one another address's record still has)
        mesh = {**self._mesh, "key_refresh": record.get("key_refresh")}
        return {"addresses": addresses, "mesh": mesh}

    async def async_save_now(self) -> None:
        """Write both copies now and wait for the writes (the backup hooks: `backup.py`).

        `_closed` is left as it is — a hub stopped before the backup keeps its record `clean` — and a superseded
        `HAState` writes nothing (HAC-02).
        """
        if not self._owns_the_store():
            return
        saves = [self._store.async_save(self._snapshot())]
        if self._backup_store is not None:
            saves.append(
                self._backup_store.async_save(self._snapshot(force_dirty=True))
            )
        await asyncio.gather(*saves)

    def carries_backup_token(self, token: str) -> bool:
        """Whether what both copies durably hold for our address carries the backup's mark `token`."""
        stores = [self._store, self._backup_store]
        return all(
            (record := _usable_record(store.written, f"{self.src:04X}")) is not None
            and record.get("in_backup") == token
            for store in stores
            if store is not None
        )


# the states of an entry whose hub may still send with its mesh's counters (FAILED_UNLOAD: its hub was never stopped)
HUB_RUNNING = (
    ConfigEntryState.SETUP_IN_PROGRESS,
    ConfigEntryState.LOADED,
    ConfigEntryState.FAILED_UNLOAD,
)


def _refuse_duplicate_mesh(
    hass: HomeAssistant, entry: ConfigEntry, mesh_uuid: str
) -> None:
    """Refuse a hub for a mesh another entry's hub is running: raise `ISSUE_DUPLICATE_MESH` for `entry`, then fail.

    `config_flow._mesh_uuid_taken` refuses a second entry for an already-configured mesh, so nothing created
    since that check exists can trigger this; an installation upgraded from before it (or an entry edited by
    hand) can still have two. Both running would share one store with two stale views of it (`HAState.persist`),
    and the later `HAState` takes the counters over (`SEQ_OWNERS`), so the first hub would refuse every send from
    then on — muted, with nothing but a DEBUG line to show. So the first one keeps running and this one does not
    start. The issue is `entry`'s: its removal deletes it (`async_remove_entry`), and so does its next successful
    start (`JungHomeHub._clear_issues`). An owner of the same entry (a reload's predecessor) is no obstacle, nor is
    one whose entry is gone or no longer running.
    """
    owner = hass.data.get(SEQ_OWNERS, {}).get(mesh_uuid.lower())
    if owner is None or owner.entry_id in (None, entry.entry_id):
        return
    other = hass.config_entries.async_get_entry(owner.entry_id)
    if other is None or other.state not in HUB_RUNNING:
        return
    _LOGGER.error(
        "%s already runs the JUNG HOME mesh %s: %s is not started — remove one of the two entries",
        other.title,
        mesh_uuid,
        entry.title,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry, ISSUE_DUPLICATE_MESH),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_DUPLICATE_MESH,
        learn_more_url=learn_more_url(ISSUE_DUPLICATE_MESH),
        translation_placeholders={"title": entry.title, "others": other.title},
    )
    raise ConfigEntryError(
        translation_domain=DOMAIN,
        translation_key=ISSUE_DUPLICATE_MESH,
        translation_placeholders={"others": other.title},
    )


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

        Only a load other than a metering socket falls back (`JungHomeHub._get_counters`, `_on_sig_property_status`),
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


@dataclass
class DimHold:
    """A key's dimming hold in progress (`JungHomeHub._dim_hold`): how it started and its end timers.

    `kind` is the message that started it (`move` / `delta`), `tid` its transaction, `direction` `up` / `down`,
    `target` the address the key dims (hex); `quiet` cancels the end of a Delta transaction's hold, `limit` its end
    at DIM_HOLD_MAX.
    """

    kind: str
    tid: int
    direction: str
    target: str
    quiet: Callable[[], None] | None = None
    limit: Callable[[], None] | None = None

    def cancel_quiet(self) -> None:
        """Stop the quiet timer, if one runs."""
        if self.quiet is not None:
            self.quiet()
            self.quiet = None

    def cancel_timers(self) -> None:
        """Stop both end timers."""
        self.cancel_quiet()
        if self.limit is not None:
            self.limit()
            self.limit = None


@dataclass
class KeyHold:
    """A gateway-mode key's hold (`JungHomeHub._button_event`): the side it started on and its end at DIM_HOLD_MAX.

    `ended`: the hold was ended without its release (DIM_HOLD_MAX, the link) and stays here only so that release,
    when it still comes, ends nothing a second time.
    """

    side: str | None
    limit: Callable[[], None] | None = None
    ended: bool = False

    def cancel_limit(self) -> None:
        """Stop the end timer, if one runs."""
        if self.limit is not None:
            self.limit()
            self.limit = None


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
        self.proxy = ProxyClient(
            cdb,
            state,
            on_message=self._on_message,
            on_disconnect=self._on_disconnect,
            on_beacon=self._on_beacon,
            on_filter_status=self._on_filter_status,
            on_undecryptable=self._on_undecryptable,
            on_heartbeat=self._on_heartbeat,
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
        self.link_state = LINK_SEARCHING  # one of LINK_STATES (`_set_link_state`), for the link state sensor
        self._bluetooth_off = False  # `bluetooth_unavailable` is raised
        self._task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._energy_task: asyncio.Task[None] | None = None
        self._energy_history_at: float | None = (
            None  # monotonic time of the last energy-chart import (`energy_history`)
        )
        # per socket (main address): its counter Gets and a reset share reply opcodes, so they take turns
        self._counter_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # per metered load (main address): the on-demand read running now, which later callers wait for
        self._meter_refreshes: dict[int, asyncio.Event] = {}
        self._unsub_time: Callable[[], None] | None = None
        self._unsub_energy: Callable[[], None] | None = None
        self._link_lost = asyncio.Event()
        self._stop = False
        self._unsub_adv: Callable[[], None] | None = None
        self._was_available = False
        self._last_rx = time.monotonic()  # when the proxy last forwarded anything we could decode, or named itself (link watchdog)
        self._rx_messages = 0  # decoded access messages, any destination
        self._rx_to_us = (
            0  # ... of which unicast to our address: proof that nodes accept our PDUs
        )
        self._pdus_dropped = False
        # the `address_shared` repair skipped past another client's numbers since this hub started: a new sighting
        # asks for another address (`_report_address_shared`)
        self._address_shared_skipped = False
        # per link: whether the proxy's Secure Network Beacon authenticated (it sends one right after we subscribe),
        # and the watchdog waiting for its Filter Status (`_filter_status_overdue`)
        self._beacon_authenticated = False
        self._unsub_filter_watch: Callable[[], None] | None = None
        # Option: report a `click` only once a second click can no longer turn it into a double click.
        self.click_delay = bool(
            entry.options.get(OPTION_CLICK_DELAY, DEFAULT_CLICK_DELAY)
        )
        # Option: node heartbeats — per-node liveness (`heartbeats`, `_configure_heartbeats`, `node_alive`)
        self.heartbeats_enabled = bool(
            entry.options.get(OPTION_HEARTBEATS, DEFAULT_HEARTBEATS)
        )
        self.heartbeats: dict[int, Heartbeat] = {}  # node unicast → its last Heartbeat
        # node unicast → its last Configuration Server audit (`async_audit`), for the diagnostics
        self.audits: dict[int, NodeAudit] = {}
        # scene number → {member element → its JUNG scene action (None: stored without a description)}, read from
        # the members' Scene Action Setup servers at link-up (`_get_scene_actions`, `_connect_step`)
        self.scene_actions: dict[int, dict[int, V.Action | None]] = {}
        # the channels whose scene list answered (`_get_scene_actions_of`): only theirs narrows `scenes_of`
        self.scene_lists_read: set[int] = set()
        # element → the scene numbers its register held when it last reported it (Scene Register Status)
        self.scene_registers: dict[int, tuple[int, ...]] = {}
        # scene number → when EVENT_SCENE_RECALLED last fired for it (`note_scene_recall`, SCENE_RECALL_WINDOW)
        self._scene_recalls: dict[int, float] = {}
        # elements asked for their current scene right now (`_get_current_scene_of`): their Scene Status is an
        # answer, even if the firmware publishes it to its group instead of sending it to us
        self._scene_gets: set[int] = set()
        self.node_by_mac = nodes_by_mac(cdb)  # the node behind a Bluetooth address
        self._export_refresh: asyncio.Task[None] | None = (
            None  # the gateway export fetch in flight, if any
        )
        self._export_refresh_failures = (
            0  # unanswered fetches in a row: index into EXPORT_REFRESH_BACKOFF
        )
        self._unsub_export_refresh: CALLBACK_TYPE | None = (
            None  # the next fetch, when one is scheduled
        )
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
        # one mesh read of the gateway's certificate at a time, and a pin the gateway node contradicted (not used)
        self._gateway_check = asyncio.Lock()
        self._distrusted_pin: str | None = None
        self._reprobed_at: dict[
            int, float
        ] = {}  # dead node → when it was last asked for heartbeats again
        self._reprobe_task: asyncio.Task[None] | None = None
        self.unknown_nodes: dict[
            str, JungAdvertisement | None
        ] = {}  # MAC → what it advertises; nodes of our network the export does not know
        self._alive_deadline: dict[
            int, float
        ] = {}  # node unicast → monotonic time after which it counts as dead
        self._dead_nodes: set[int] = set()
        # nodes that left a full-budget request unanswered (`_missed_answer`): their entities are unavailable until
        # the node is heard from (`_heard_from`)
        self.unreachable: set[int] = set()
        self.last_heard: dict[
            int, float
        ] = {}  # node unicast → monotonic time of its last message
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
        # how the link before the current one ended, for the connect-time steps (`_connect_step`)
        self._previous_link = NO_LINK
        # connect-time step → when its last complete round ended (monotonic; `_connect_step`)
        self._connect_steps_done: dict[str, float] = {}
        # proxy MAC → its links in a row that ended within SHORT_LINK (`_judge_link`)
        self._short_links: dict[str, int] = {}
        self._link_loss_listeners: list[Callable[[LinkEnd], None]] = []
        # the last LINK_HISTORY links, oldest first (`_link_ended`); for the current one: how long its state refresh
        # took (None until it is through) and the store's `held_back_total` when it came up
        self.link_history: deque[LinkRecord] = deque(maxlen=LINK_HISTORY)
        self._link_refresh: float | None = None
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
        self._unsub_offset_change: CALLBACK_TYPE | None = (
            None  # the Time Set after a DST change
        )
        # link diagnostics (review-3 F8, F9): when each node was last heard (wall clock), the signal strength of
        # its last advertisement, when it last restarted, and the last sequence number per source
        self.last_seen: dict[int, datetime] = {}
        self.node_rssi: dict[int, int] = {}
        self.restarted: dict[int, datetime] = {}
        self._last_seq: dict[int, int] = {}
        self._node_signalled: dict[int, float] = {}
        self._unsub_seq_check: CALLBACK_TYPE | None = None
        self._recheck: dict[
            int, CALLBACK_TYPE
        ] = {}  # node unicast → its pending re-ask
        # load element → the pending read of its state after a transition (`_reread_after_transition`)
        self._transition_reread: dict[int, CALLBACK_TYPE] = {}
        # node unicast → the pending end of its Node Identity advert (`async_locate`)
        self._locating: dict[int, CALLBACK_TYPE] = {}
        self._heartbeats_configured_at: float | None = None
        self._rebuilding = (
            False  # `async_begin_rebuild` ran: a reload replaces this hub
        )
        self._unsub_heartbeats: Callable[[], None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        # what this hub was built from; `needs_rebuild` tells the update listener whether the entry moved away from it
        self._built_from = (hub_data(entry.data), dict(entry.options))
        self._delayed_clicks: dict[
            int, tuple[Callable[[], None], int, str | None]
        ] = {}  # element → (cancel the timer, counter and side of the click held back)
        # per link: what the proxy forwarded that our keys could open, and what they could not (stale export detection)
        self._rx_decoded_link = 0
        self._rx_undecodable_link = 0
        self._export_stale = False
        self._button_last_click: dict[
            int, tuple[float, str | None]
        ] = {}  # element → (when, side) of the last click, for double clicks
        self._key_holds: dict[
            int, KeyHold
        ] = {}  # element → the gateway-mode hold in progress, whose side is handed to its release
        self._dim_holds: dict[
            int, DimHold
        ] = {}  # element → the dimming hold in progress
        self._unknown_codes: set[tuple[int, int]] = set()  # (element, code) logged once
        self._button_recent: dict[
            int, list[tuple[int, float]]
        ] = {}  # element → (counter, when) of recent events
        self._sig_recent: dict[
            tuple[int, int, bytes], float
        ] = {}  # (src, opcode, params) → when
        self._event_listeners: dict[int, list[EventListener]] = {}
        self.async_on_link_loss(self._end_holds_on_link_loss)
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

        The energy poll is armed by every connection (`_arm_energy_poll`), so its grid starts with the link.
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
        self._unsub_time = async_track_time_interval(
            self.hass, self._send_time_daily, timedelta(seconds=TIME_SET_INTERVAL)
        )
        self._arm_offset_change()
        if self.heartbeats_enabled:
            self._unsub_heartbeats = async_track_time_interval(
                self.hass,
                self._check_heartbeats,
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

    def _arm_energy_poll(self) -> None:
        """(Re)start the energy poll timer so its grid is anchored on the connection just made.

        Anchored on the start of the integration instead, a poll could fall right before the link watchdog's
        deadline on a mesh where the poll's reply is the only traffic. No timer without a metered load.
        """
        if self._unsub_energy is not None:
            self._unsub_energy()
            self._unsub_energy = None
        if self.devices.metered:
            self._unsub_energy = async_track_time_interval(
                self.hass,
                self._poll_energy_periodic,
                timedelta(seconds=ENERGY_POLL_INTERVAL),
            )

    async def async_stop(self) -> None:
        """Stop the connection loop and background work, then drop the link."""
        self._stop = True
        for unsub in (
            self._unsub_adv,
            self._unsub_time,
            self._unsub_energy,
            self._unsub_heartbeats,
            self._unsub_seq_check,
            self._unsub_grace,
            self._unsub_ha_stop,
            self._unsub_echo,
            self._unsub_offset_change,
            self._unsub_seq_stall,
        ):
            if unsub:
                unsub()
        self._unsub_grace = self._unsub_ha_stop = self._unsub_echo = None
        self._unsub_offset_change = self._unsub_seq_stall = None
        self._unsub_adv = self._unsub_time = self._unsub_energy = None
        self._unsub_heartbeats = self._unsub_seq_check = None
        self._cancel_export_refresh_timer()
        self._cancel_filter_watch()
        # not a pending retry of a failed upload: it is the entry's and outlives the reload most changes end with
        # (`MeshConfigurator._upload_or_retry`); removing the entry cancels it
        for cancel in (
            *self._recheck.values(),
            *self._transition_reread.values(),
            *self._locating.values(),  # the nodes stop by themselves within 60 s
        ):
            cancel()
        self._recheck.clear()
        self._transition_reread.clear()
        self._locating.clear()
        for addr in list(self._delayed_clicks):
            self._cancel_delayed_click(addr)
        self._end_holds(HOLD_END_STOPPED)
        self._key_holds.clear()
        try:
            await self._cancel(self._task)
            self._task = None
            await self._cancel(self._refresh_task)
            self._refresh_task = None
            await self._cancel(self._energy_task)
            self._energy_task = None
            await self._cancel(self._heartbeat_task)
            self._heartbeat_task = None
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
            and not self._stop
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
                self._unsub_heartbeats is not None
            )  # armed by `async_start` with the option on
            self._unsub_heartbeats()
            self._unsub_heartbeats = None
            await self._cancel(self._heartbeat_task)
            await self._cancel(self._reprobe_task)
            self._heartbeat_task = self._reprobe_task = None
            await self.async_disable_heartbeats()
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
        if adopted := [mac for mac in self.unknown_nodes if mac in self.node_by_mac]:
            for mac in adopted:
                del self.unknown_nodes[mac]
            if self.unknown_nodes:
                self._report_unknown_nodes()
            else:
                self._cancel_export_refresh_timer()
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
            self._connect_steps_done.pop(step, None)
        if self.connected:
            self.entry.async_create_background_task(
                self.hass, self._read_scenes(), f"{DOMAIN} scene reads"
            )

    async def _read_scenes(self) -> None:
        await self._connect_step("scene actions", self._get_scene_actions)
        await self._connect_step("current scenes", self._get_current_scenes)

    async def _welcome(self, loads: set[int], nodes: list[Node]) -> None:
        """Ask the loads an export added for their state, and its mains nodes for heartbeats, over the current link."""
        try:
            await self._chunked(self._state_jobs(loads))
        except ConnectionError as err:
            _LOGGER.debug("state read of the new loads aborted: %s", err)
            return
        if (
            self.heartbeats_enabled
            and any(n.pid is not None and n.pid not in BATTERY_PIDS for n in nodes)
            and (self._heartbeat_task is None or self._heartbeat_task.done())
        ):
            self._heartbeats_configured_at = (
                None  # a round for every node: the new ones are among them
            )
            await self._configure_heartbeats()

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
            if self._ours(info)
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
        if not self.proxy.connected and self._ours(info):
            self._link_lost.set()  # wake the loop: a candidate appeared
        if (node := self.node_for_address(info.address)) is not None:
            self.node_rssi[node.unicast] = info.rssi
            self._signal_node(node.unicast)
            # its JUNG record, merged into the proxy advert's data: insert and key layout (`inserts.py`)
            self.inserts.note_advert(
                node, parse_manufacturer_data(info.manufacturer_data)
            )
        self._check_unknown_node(info)

    def _ours(self, info: bluetooth.BluetoothServiceInfoBleak) -> bool:
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

    def _check_unknown_node(self, info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Notice a node of *our* network advertising from a MAC the export does not know: the export is behind.

        The proxy advertisement carries our Network ID, so the node is provisioned into this mesh, and its address is
        the MAC the export would hold in the node UUID — a node added or re-provisioned after the export. Raised
        once as a repair issue listing the devices (product from the JUNG manufacturer record when the advert
        carries one); cleared when the export is reloaded with them in it (a new hub starts with an empty list).
        Addresses that are not MACs (macOS hands out UUIDs) cannot be checked.
        """
        address = info.address.upper()
        if address in self.unknown_nodes or address in self.node_by_mac:
            return
        if len(address) != 17 or address.count(":") != 5:
            return
        if not self._ours(info):
            return
        self.unknown_nodes[address] = parse_manufacturer_data(info.manufacturer_data)
        _LOGGER.warning(
            "JUNG node %s belongs to this mesh but is not in the export (%s): export the network again",
            address,
            self._describe_unknown(address),
        )
        # the app uploads its project to the gateway after a change: fetch it before bothering the user (a new
        # node restarts the back-off; the issue below stands until the reload that follows a successful fetch)
        self._export_refresh_failures = 0
        self._request_export_refresh()
        self._report_unknown_nodes()

    def _request_export_refresh(self) -> None:
        """Ask the gateway for its export now, for the unknown nodes (review-3 C1: once per MAC was not enough).

        Nothing to do without unknown nodes or a gateway the export may come from, while a fetch runs, before the
        configurator exists, or without a link — the fetch vouches for the gateway over the mesh
        (`_gateway_state`), so the next link asks instead (`_connect_to`). A pending back-off timer is replaced.
        """
        if (
            not self.unknown_nodes
            or self._stop
            or self._gateway_for_refresh() is None
            or (self._export_refresh is not None and not self._export_refresh.done())
            or self.configurator is None
            or not self.connected
        ):
            return
        self._cancel_export_refresh_timer()
        self._export_refresh = self.entry.async_create_background_task(
            self.hass, self._refresh_export_from_gateway(), f"{DOMAIN} export refresh"
        )

    def _cancel_export_refresh_timer(self) -> None:
        if self._unsub_export_refresh is not None:
            self._unsub_export_refresh()
            self._unsub_export_refresh = None

    def _schedule_export_refresh(self) -> None:
        """Ask again after the next EXPORT_REFRESH_BACKOFF delay: the app may not have uploaded yet."""
        delay = EXPORT_REFRESH_BACKOFF[
            min(self._export_refresh_failures, len(EXPORT_REFRESH_BACKOFF) - 1)
        ]
        self._export_refresh_failures += 1
        self._cancel_export_refresh_timer()

        @callback
        def again(_now: datetime) -> None:
            self._unsub_export_refresh = None
            self._request_export_refresh()

        self._unsub_export_refresh = async_call_later(self.hass, delay, again)

    @property
    def follows_gateway(self) -> bool:
        """Whether the entry was set up from the gateway, whose export may replace ours (`_gateway_for_refresh`)."""
        return self._gateway_for_refresh() is not None

    def _gateway_for_refresh(self) -> JungHomeGatewayApi | None:
        """Return the gateway API when its export may replace ours: only for an entry set up *from* the gateway.

        An entry reconfigured to a file of the user's own (source `path` or `upload`) keeps the gateway host and
        token for the flow's re-fetch, but its file is not ours to overwrite — the fetched export would replace a
        hand-maintained `MeshNetwork.json` (with `metadata_dir` names the share format cannot carry) in place.
        """
        if self.entry.data.get(CONF_SOURCE) != "gateway":
            return None
        return api_for_entry(self.hass, self.entry)

    def _report_unknown_nodes(self) -> None:
        """Raise (or update) the `unknown_nodes` repair; an entry from the gateway gets the wording that names it.

        Two translation keys rather than a sentence in a placeholder (review-4 H4-8): translators get the whole
        text, and the gateway variant's only extra placeholder is its `host`.
        """
        gateway = self._gateway_for_refresh()
        placeholders = {
            "title": self.entry.title,
            "count": str(len(self.unknown_nodes)),
            "devices": ", ".join(
                self._describe_unknown(mac) for mac in sorted(self.unknown_nodes)
            ),
        }
        key = ISSUE_UNKNOWN_NODES
        if gateway is not None:
            key = ISSUE_UNKNOWN_NODES_GATEWAY
            placeholders["host"] = gateway.host
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry, ISSUE_UNKNOWN_NODES),
            is_fixable=True,  # its repair loads a new export (`repairs.NewExportFlow`)
            data={"entry_id": self.entry.entry_id},
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            learn_more_url=learn_more_url(key),
            translation_placeholders=placeholders,
        )

    async def async_follow_gateway(self) -> bool:
        """Read the gateway's address off the mesh; store it when it moved. True when the entry now names another host.

        The gateway node serves its address and certificate fingerprint as LBC Manufacturer properties (`0xC002`
        IP, `0xC003` SHA-256 of its certificate, `docs/android/transport-provisioning.md` §5.1), which is how the
        app finds a gateway whose DHCP address changed or whose certificate was renewed: it re-reads them whenever
        the gateway stops answering. The address is followed (when it is one, `is_gateway_host`); the certificate
        is not: anyone holding the NetKey and AppKey — any one node's keys — can answer on the mesh, and the pin
        is what keeps the token and the export (every DevKey) from a host that is not the gateway. A certificate
        other than the pinned one stops the gateway's use and raises the `gateway_certificate_changed` repair
        instead; the reconfigure flow reads it again and asks. The token (`0xC001`) is the app's, not the one Home
        Assistant registered for itself, and is never taken.
        """
        if self.entry.data.get(CONF_SOURCE) != "gateway" or not self.connected:
            return False
        node = self._gateway_node()
        if node is None:
            return False
        host = await self._gateway_text(node.unicast, GATEWAY_IP)
        fingerprint = normalize_fingerprint(
            await self._gateway_text(node.unicast, GATEWAY_FINGERPRINT)
        )
        data = self.entry.data
        moved = bool(host) and host != data.get(CONF_GATEWAY_HOST)
        if moved and not is_gateway_host(str(host)):
            _LOGGER.warning(
                "The gateway node reports an address that is no IP address or host name (%r); not followed",
                host,
            )
            moved = False
        if moved:
            _LOGGER.info("The gateway node says it is at %s; following it", host)
            # not hub data (`HUB_DATA_KEYS`): the update listener does not reload for it
            self.hass.config_entries.async_update_entry(
                self.entry, data={**data, CONF_GATEWAY_HOST: host}
            )
        pin = normalize_fingerprint(data.get(CONF_GATEWAY_FINGERPRINT))
        if fingerprint is not None and fingerprint != pin:
            self._gateway_contradicted(pin)
            return False
        return moved

    async def async_gateway_distrust(self) -> str | None:
        """Why the entry's gateway must not be used now, or None when it may be (fetched from, uploaded to).

        The pin of an entry set up at first contact is whatever answered at the address then: a LAN impostor at
        setup would stay pinned and receive every later upload — the full export with every key. So before the
        first exchange a pin the gateway node has not vouched for is compared with the certificate the node
        reports over the mesh (`0xC003`, where the app takes its pin from): equal, it counts as vouched for from
        then on (`PIN_FROM_MESH` in the entry); different, the gateway is not used and the
        `gateway_certificate_changed` repair points to Reconfigure; unanswered, nothing is exchanged with the
        gateway yet (logged) and the next use asks again. The mesh changes nothing else: devices work as ever.
        """
        pin = normalize_fingerprint(self.entry.data.get(CONF_GATEWAY_FINGERPRINT))
        async with self._gateway_check:
            if pin is not None and pin == self._distrusted_pin:
                return GATEWAY_CERTIFICATE_CHANGED
            if self.entry.data.get(CONF_GATEWAY_PIN_SOURCE) == PIN_FROM_MESH:
                return None
            node = self._gateway_node()
            reported = (
                normalize_fingerprint(
                    await self._gateway_text(node.unicast, GATEWAY_FINGERPRINT)
                )
                if node is not None and self.connected
                else None
            )
            host = self.entry.data.get(CONF_GATEWAY_HOST)
            if reported is None:
                _LOGGER.warning(
                    "The gateway node has not confirmed the certificate pinned for the gateway %s over the mesh "
                    "yet: nothing is fetched from or handed to the gateway until it does",
                    host,
                )
                return GATEWAY_UNVERIFIED
            if reported != pin:
                self._gateway_contradicted(pin)
                return GATEWAY_CERTIFICATE_CHANGED
            _LOGGER.info(
                "The gateway node confirmed the certificate pinned for the gateway %s over the mesh",
                host,
            )
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_GATEWAY_PIN_SOURCE: PIN_FROM_MESH},
            )
            return None

    @property
    def gateway_vouched(self) -> bool:
        """Whether the gateway node vouched for the entry's pin and nothing contradicted it since.

        `async_gateway_distrust` without its mesh read: a poll of the gateway's status uses the gateway only when
        this holds, and leaves the vouching to the check every link runs (`_check_gateway_pin`).
        """
        pin = normalize_fingerprint(self.entry.data.get(CONF_GATEWAY_FINGERPRINT))
        return self.entry.data.get(CONF_GATEWAY_PIN_SOURCE) == PIN_FROM_MESH and (
            pin is None or pin != self._distrusted_pin
        )

    def _check_gateway_pin(self) -> None:
        """On every link while the pin is not vouched for: compare it with the gateway node's report, in the background."""
        if (
            self._gateway_for_refresh() is not None
            and self.entry.data.get(CONF_GATEWAY_PIN_SOURCE) != PIN_FROM_MESH
            and self._distrusted_pin is None
        ):
            self.entry.async_create_background_task(
                self.hass,
                self.async_gateway_distrust(),
                f"{DOMAIN} gateway certificate check",
            )

    def _gateway_contradicted(self, pin: str | None) -> None:
        """Stop using the gateway: its node reports another certificate than `pin`. Raises the repair."""
        self._distrusted_pin = pin
        _LOGGER.warning(
            "The gateway node reports another certificate over the mesh than the one pinned for the gateway %s: "
            "the gateway is not used until the entry is reconfigured",
            self.entry.data.get(CONF_GATEWAY_HOST),
        )
        self.async_raise_certificate_issue()

    @callback
    def async_raise_certificate_issue(self) -> None:
        """Raise the repair pointing to Reconfigure: the gateway, or its node over the mesh, contradicts the pin."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry, ISSUE_GATEWAY_CERTIFICATE),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GATEWAY_CERTIFICATE,
            learn_more_url=learn_more_url(ISSUE_GATEWAY_CERTIFICATE),
            translation_placeholders={
                "host": str(self.entry.data.get(CONF_GATEWAY_HOST) or "")
            },
        )

    def _gateway_node(self) -> Node | None:
        return next((n for n in self.cdb.nodes if n.pid == GATEWAY_PID), None)

    async def _gateway_text(self, unicast: int, pid: int) -> str | None:
        """Return one of the gateway node's text properties (LBC Manufacturer Get); None when it does not say."""
        key = pid.to_bytes(2, "little")
        try:
            reply = await self.proxy.request(
                unicast,
                M.vendor_property_get("manufacturer", pid),
                M.VENDOR_PROPERTY_STATUS_OPCODES["manufacturer"],
                expect_cid=M.JUNG_CID,
                retries=1,
                match=lambda m: m.params[:2] == key,
            )
        except (TimeoutError, ConnectionError):
            _LOGGER.debug("The gateway node %04X did not say %04X", unicast, pid)
            return None
        return PROPERTIES[pid].codec.decode(reply.params[3:]) or None

    async def _refresh_export_from_gateway(self) -> None:
        """Adopt the gateway's export when it knows the unknown nodes, then reload the entry with it.

        The dynamic-devices path for an entry set up from a gateway: the app uploads its project to the gateway
        after every change, so a node it just provisioned is usually there already. The adoption is the
        configurator's (`MeshConfigurator.adopt_for_unknown_nodes`), under its lock and with every guard of its
        gateway writes; otherwise the repair issue stays (its text then says the gateway was asked) and the fetch
        is repeated with back-off (`_schedule_export_refresh`) and on every new link.
        """
        configurator = self.configurator
        assert configurator is not None  # `_request_export_refresh` checked
        found = await configurator.adopt_for_unknown_nodes(sorted(self.unknown_nodes))
        if not found:
            self._schedule_export_refresh()
            return
        _LOGGER.info(
            "Fetched the export from the gateway %s: it lists %s; following it",
            self.entry.data.get(CONF_GATEWAY_HOST),
            ", ".join(found),
        )
        self.follow_adopted_export()

    @callback
    def follow_adopted_export(self) -> None:
        """Have the device model follow an export adopted from the gateway, in a task of its own (`_reload_for_export`).

        Not one of the entry's background tasks: a reload in its place unloads the entry, which cancels those.
        """
        self.hass.async_create_task(
            self._reload_for_export(), f"{DOMAIN} follow the gateway's export"
        )

    async def _reload_for_export(self) -> None:
        """Follow the adopted export, under the entry's lock (`ENTRY_LOCKS`, review-3 W11): in place, else by a reload.

        A service call holds that lock while it works on this hub — waiting for its link, planning, sending — and
        has the entry follow the export itself afterwards; a reload in between would tear the hub down under it.
        Once the lock is ours, a hub that is no longer the entry's (a reload read the adopted export already) or an
        entry that is no longer loaded needs nothing more. The new nodes' devices show up without a reload
        (`model_update.async_follow_export`, review-4 D23).
        """
        async with entry_lock(self.hass, self.entry.entry_id):
            if (
                self.entry.state is ConfigEntryState.LOADED
                and self.entry.runtime_data is self
            ):
                assert self.follow_export is not None  # set by the setup
                await self.follow_export()

    def _describe_unknown(self, mac: str) -> str:
        advert = self.unknown_nodes.get(mac)
        if advert is None:
            return mac
        return f"{PRODUCT_NAMES.get(advert.product_id, f'product {advert.product_id}')} {mac}"

    async def _connection_loop(self) -> None:
        """Keep a link: connect to the best proxy node in range, watch it, and connect again when it goes.

        An unexpected error in one pass is logged and the loop goes on after a pause (review-3 C6: it used to end
        the task, and with it every link until a reload).
        """
        failed: dict[str, float] = {}
        # shared with `_connection_pass`, which doubles it on a failure or a short link and resets it after a long one
        backoff = [CONNECT_BACKOFF_MIN]
        while not self._stop:
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
                self._set_link_state(LINK_FAILED)
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
        self._set_link_state(LINK_CONNECTING)
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
            self._set_link_state(LINK_FAILED)
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
                        self._missed_answer(address, kind, asked, command=True)
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

        A Get is the one message JUNG firmware always answers (`_refresh_all`), so an unanswered keep-alive means
        the proxy no longer forwards (or the element is gone: up to KEEP_ALIVE_ATTEMPTS distinct elements are
        tried). Traffic of any kind arriving meanwhile counts as well. A send the sequence-number store holds
        back is waited for (`_while_seq_stalls`), not taken for a dead link; one that cannot go out at all (the
        store refused past SEQ_STALL_DEADLINE, the sequence space is used up) is no verdict either way, so what
        arrived since decides alone — with nothing, the watchdog drops a silent proxy as after any unanswered
        keep-alive.
        """
        before = self._last_rx
        for addr in self._keep_alive_targets()[:KEEP_ALIVE_ATTEMPTS]:
            asked = time.monotonic()
            try:
                await self._while_seq_stalls(
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
                self._missed_answer(addr, "switch", asked, full=False)
                if self._last_rx != before:
                    return True  # not that element, but the proxy forwarded something else meanwhile
                continue
            except ConnectionError as err:
                _LOGGER.debug("keep-alive not sent: %s", err)
                break
            return True
        return self._last_rx != before

    def _set_link_state(self, state: str) -> None:
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
        self._set_link_state(LINK_BLUETOOTH_OFF if off else LINK_SEARCHING)

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
        self._rx_decoded_link = self._rx_undecodable_link = 0
        self._beacon_authenticated = False
        # counted before the attach: `proxy.connected` turns True inside it, and an entity added right then must
        # already see the new link's number (`config_entities.PropertyEntity._maybe_read`)
        self.link_count += 1
        await self.proxy.attach(client, beacon_wait=CONNECT_BEACON_WAIT)
        if not self.proxy.connected:
            # lost while attach() settled after the filter request (review-4 R4-3): a failed connection, not a link
            # to report as up for a moment and then as lost — the entities would flap
            raise ConnectionError("the link was lost while it was set up")
        self._previous_link = self._link_end or NO_LINK
        self._link_end = None
        self._link_since = time.monotonic()
        self._link_refresh = None
        self._held_back_at_link = self.state.held_back_total
        self._connect_failure_logged = False
        self.proxy_address = info.address
        # silence while the link was down was the link's fault, not the nodes': every node gets a full timeout
        # from here (a dead node stays dead until heard from — `_mark_alive` revives it)
        now = time.monotonic()
        for unicast in self._alive_deadline:
            self._alive_deadline[unicast] = now + self.heartbeat_timeout
        # ... and no re-ask pending (the refresh asks everyone); an unreachable node stays so until heard from
        for unicast in list(self._recheck):
            self._cancel_recheck(unicast)
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
        self._set_link_state(
            LINK_UPDATING
        )  # until the connect-time state refresh is through (`_after_connect`)
        self._cancel_refresh()  # a refresh still running from the previous link would keep polling through this one
        if (
            self.proxy.proxy_addr is None
        ):  # the Filter Status itself is still due, whatever the address told us
            self._unsub_filter_watch = async_call_later(
                self.hass, FILTER_STATUS_TIMEOUT, self._filter_status_overdue
            )
        self._arm_energy_poll()
        self._refresh_task = self.entry.async_create_background_task(
            self.hass, self._after_connect(), f"{DOMAIN} refresh"
        )
        self._check_gateway_pin()
        self._request_export_refresh()  # unknown nodes seen before this link (or during setup) are asked about now
        self.vault_refresh.schedule()  # a device Home Assistant added that missed a key refresh step: again now

    def _cancel_refresh(self) -> None:
        """Cancel the per-link background work: the connect-time refresh, a running energy poll, the Filter Status watchdog."""
        for task in (self._refresh_task, self._energy_task):
            if task is not None:
                task.cancel()
        self._refresh_task = self._energy_task = None
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
            or not self._beacon_authenticated
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
        self._report_pdus_dropped(True)

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
                self._link_refresh,
                self.state.held_back_total - self._held_back_at_link,
            )
        )
        self._cancel_refresh()
        self._start_grace()
        self._set_link_state(LINK_DISCONNECTED)
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
            self._report_pdus_dropped(False)
        if self.proxy_node == proxy_unicast:
            return
        self.proxy_node = proxy_unicast
        _LOGGER.debug("Proxy %s is mesh node %04X", self.proxy_address, proxy_unicast)
        async_dispatcher_send(self.hass, SIGNAL_CONNECTION.format(self.entry.entry_id))

    async def _after_connect(self) -> None:
        """Run a link's connect-time sequence: the clock and location, the state refresh, then the slower reads.

        Time Set and the location go first, right after the proxy filter `attach` wrote (review-4 R I-5): two
        unacknowledged broadcasts, they need no refresh to be through, and sent after it they never went out on a
        link that dropped before the refresh ended — a flapping link left the nodes' clocks unset. The scene and
        fault reads are not repeated soon after a round on a link that held (`_connect_step`); the heartbeat
        configuration has a longer interval of its own (`_configure_heartbeats`). The new order is unverified on air.
        """
        await self._send_time()
        await self._send_location()
        if not await self._refresh_all():
            return
        # a link lost meanwhile cancelled this task (`_cancel_refresh`): the link is still the one refreshed
        self._link_refresh = time.monotonic() - self._link_since
        self._set_link_state(LINK_CONNECTED)
        await self._poll_energy()
        await self._backfill_energy_history()
        await self._configure_heartbeats()
        if not self.heartbeats_enabled and self.heartbeats_publishing:
            # the option went off while some nodes could not be told (link down, a node silent or refusing)
            await self.async_disable_heartbeats()
        await self._connect_step("scene actions", self._get_scene_actions)
        await self._connect_step("faults", self._get_faults)
        await self._connect_step("current scenes", self._get_current_scenes)
        await self._connect_step("inserts", self.inserts.read_unknown)

    async def _connect_step(
        self, name: str, step: Callable[[], Awaitable[bool]]
    ) -> None:
        """Run a connect-time read, unless its last complete round is recent and the link before this one held.

        A link that lasted SHORT_LINK kept the hub hearing the nodes' publications (a Scene Status after every
        recall); a round within CONNECT_STEP_FRESH of this link is current enough, and repeating it on every link
        is what made a link that comes and goes keep the mesh busy (review-4 R I-5). After a short link — a failed
        connection to `_judge_link` — the round runs again: what the hub heard through that link proves little.
        `step` returns True when it got through. Unverified on air.
        """
        done = self._connect_steps_done.get(name)
        if (
            done is not None
            and time.monotonic() - done < CONNECT_STEP_FRESH
            and self._previous_link.lasted >= SHORT_LINK
        ):
            _LOGGER.debug(
                "%s read %.0f s ago: not asked again on this link",
                name,
                time.monotonic() - done,
            )
            return
        if await step():
            self._connect_steps_done[name] = time.monotonic()

    async def _get_faults(self) -> bool:
        """Ask every mains node for its registered Health faults (Health Fault Get to its primary element).

        JUNG nodes keep the vendor faults 0x81 / 0x80 registered (meaning unknown, `docs/hidden-features.md` §10)
        and nothing publishes the register, so the fault binary sensors are filled at link-up (`_connect_step`),
        one unicast Get per node REFRESH_CHUNK at a time; a single all-nodes Get loses answers in the collision.
        False when the link went away first.
        """
        try:
            await self._chunked(
                [
                    partial(self._get_faults_of, node.unicast)
                    for node in self.cdb.nodes
                    if node.pid is not None and node.pid not in BATTERY_PIDS
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("fault read aborted: %s", err)
            return False
        return True

    async def _get_faults_of(self, addr: int) -> None:
        """Read one node's fault register; the Health Fault Status handler stores it."""
        try:
            await self.proxy.request(
                addr,
                M.health_fault_get(),
                M.HEALTH_FAULT_STATUS,
                retries=REFRESH_RETRIES,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer its Health Fault Get", addr)

    async def _get_scene_actions(self) -> bool:
        """Ask every scene member what it does in its scenes (JUNG Scene Action Setup), for the scene entities.

        One list Get per member channel, then one Get per scene it names — a few dozen messages on a typical
        installation, at link-up (`_connect_step`). The export knows the members, not their actions; nothing publishes
        them, so this is the only way to show "what does this scene do". The export lists the element the Scene
        Store went to (the node's primary, for both channels of a two-channel node), so every channel of that node
        is asked, as the app asks each channel for its own list (`GetScenesForDevice`). False when the link went
        away first.
        """
        members = sorted(
            {
                channel
                for addresses in self.cdb.scenes.values()
                for addr in addresses
                for channel in self.scene_action_channels(addr)
            }
        )
        if not members:
            return True
        try:
            await self._chunked(
                [partial(self._get_scene_actions_of, addr) for addr in members]
            )
        except ConnectionError as err:
            _LOGGER.debug("scene action read aborted: %s", err)
            return False
        async_dispatcher_send(self.hass, SIGNAL_SCENES.format(self.entry.entry_id))
        return True

    async def _get_current_scenes(self) -> bool:
        """Ask every element holding a scene register for its current scene (Scene Get), as the app reads it.

        The Scene Status handler stores the answer (an answer to us, not a publication: it fires no event). After
        that the nodes keep it current themselves: they publish a Scene Status after every recall. False when the
        link went away first.
        """
        registers = sorted(
            {a for addresses in self.cdb.scenes.values() for a in addresses}
        )
        if not registers:
            return True
        try:
            await self._chunked(
                [partial(self._get_current_scene_of, addr) for addr in registers]
            )
        except ConnectionError as err:
            _LOGGER.debug("current scene read aborted: %s", err)
            return False
        return True

    async def _get_current_scene_of(self, addr: int) -> None:
        self._scene_gets.add(addr)
        try:
            await self.proxy.request(
                addr,
                M.scene_get(),
                M.SCENE_STATUS,
                retries=REFRESH_RETRIES,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer its Scene Get", addr)
        finally:
            self._scene_gets.discard(addr)

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

    async def _get_scene_actions_of(self, addr: int) -> None:
        """Read one element's scene list and the action of each scene into `scene_actions`.

        Each reply must name the scene it was asked for (`V.scene_action_reply_to`): matched on source and opcode
        alone, a late duplicate answer for one scene would land on the next. An answered list replaces what was
        known of the element, so a scene it no longer lists stops showing its old action.
        """

        def answers(scene: int) -> Callable[[AccessMessage], bool]:
            fits = V.scene_action_reply_to(scene)
            return lambda m: fits(m.params)

        try:
            reply = await self.proxy.request(
                addr,
                V.scene_action_get(),
                V.SCENE_ACTION_SETUP_STATUS,
                expect_cid=M.JUNG_CID,
                retries=REFRESH_RETRIES,
                match=answers(V.SCENE_LIST),
            )
            listed = V.decode_scene_action_status(reply.params).scenes or ()
            self.scene_lists_read.add(addr)
            for scene in [s for s in self.scene_actions if s not in listed]:
                self.scene_actions[scene].pop(addr, None)
                if not self.scene_actions[scene]:
                    del self.scene_actions[scene]
            for scene in listed:
                reply = await self.proxy.request(
                    addr,
                    V.scene_action_get(scene),
                    V.SCENE_ACTION_SETUP_STATUS,
                    expect_cid=M.JUNG_CID,
                    retries=REFRESH_RETRIES,
                    match=answers(scene),
                )
                status = V.decode_scene_action_status(reply.params)
                self.scene_actions.setdefault(scene, {})[addr] = status.action
        except TimeoutError:  # a status too short to name its scene is no answer either
            _LOGGER.debug("%04X did not answer its Scene Action Setup Get", addr)

    async def _refresh_all(self) -> bool:
        """Ask every load for its state, a few at a time (the app does the same on start), waiting for the replies.

        A blind is asked for its position and, when it has one, its slat level (Generic Level Get to each element).
        CTL lights are also asked for their colour-temperature range (after the states: it changes nothing visible
        until a temperature is set), then their temperature element for its colour temperature (Light CTL
        Temperature Get, the gateway's read of a tunable-white light: range from the light, temperature from the
        element after it). That last Get is a third one to the same node in the same refresh and one the app never
        sends, so its silence does not count toward the node's reachability (`counted=False`): under the app's rule
        one unanswered request marks the node unreachable at once (`_missed_answer`), and the light's own state Get
        already decides that for the node. Returns False when the link went away before the refresh was through.

        A metered load's meter element gets one job that asks for its readings property by property (`_get_readings`).

        Gets are the one message JUNG firmware always answers with a unicast status, so a refresh nobody answered while
        other nodes' traffic kept arriving means the mesh discards our PDUs: a stale sequence number (lost store) or
        another client using our address. So does one nobody answered on a link whose beacon authenticated and which
        forwarded nothing decodable at all: a proxy that dropped our filter request keeps its default (empty)
        whitelist, so there *is* no other traffic to hear. Sends are otherwise fire-and-forget, so this is the only
        place to notice.
        """
        jobs = self._state_jobs()
        heard, answered = self._rx_messages, self._rx_to_us
        try:
            await self._chunked(jobs)
        except ConnectionError as err:
            _LOGGER.debug("refresh aborted: %s", err)
            return False
        if self._rx_to_us > answered:
            self._report_pdus_dropped(False)
        elif jobs and (
            self._rx_messages > heard
            or (self._beacon_authenticated and self._rx_decoded_link == 0)
        ):
            _LOGGER.error(
                "No JUNG device answered the state refresh although the link works: the nodes discard our messages "
                "(stale sequence number, or address %04X is used by another client)",
                self.proxy.state.src,
            )
            self._report_pdus_dropped(True)
        return True

    def _state_jobs(
        self, only: Container[int] | None = None
    ) -> list[Callable[[], Awaitable[None]]]:
        """Return the state refresh's Gets (`_refresh_all`): of every load, or of the loads at the addresses in `only`."""
        devices = self.devices
        lights = [d for d in devices.lights if only is None or d.address in only]
        jobs: list[Callable[[], Awaitable[None]]] = [
            partial(self._get_state, light.address, light.kind) for light in lights
        ]
        jobs += [
            partial(self._get_state, sock.address, "switch")
            for sock in devices.sockets
            if only is None or sock.address in only
        ]
        jobs += [
            partial(self._get_readings, load)
            for load in devices.metered
            if only is None or load.address in only
        ]
        jobs += [
            partial(self._get_state, addr, "level")
            for blind in devices.blinds
            if only is None or blind.address in only
            for addr in blind.level_elements
        ]
        jobs += [
            partial(self._get_state, light.address, "ctl_range")
            for light in lights
            if light.kind == "ctl"
        ]
        jobs += [
            partial(
                self._get_state,
                light.temperature_address,
                "ctl_temperature",
                counted=False,
            )
            for light in lights
            if light.kind == "ctl" and light.temperature_address is not None
        ]
        return jobs

    async def _chunked(self, jobs: Sequence[Callable[[], Awaitable[object]]]) -> None:
        """Run the jobs REFRESH_CHUNK at a time with a short pause in between (as the app does).

        Each job is retried while the sequence-number store holds sends back (`_while_seq_stalls`).
        """
        for i in range(0, len(jobs), REFRESH_CHUNK):
            if i:
                await asyncio.sleep(0.5)
            await asyncio.gather(
                *(self._while_seq_stalls(job) for job in jobs[i : i + REFRESH_CHUNK])
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

    async def _while_seq_stalls[T](self, send: Callable[[], Awaitable[T]]) -> T:
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

    async def _poll_energy(self) -> None:
        """Read the counters of every metered load (energy; a socket's power-on hours); nothing ever publishes them.

        A load's answers share opcodes (Generic Property Statuses), so a load gets its Gets one after the
        other; different loads are polled REFRESH_CHUNK at a time like the state refresh.
        """
        try:
            await self._chunked(
                [partial(self._get_counters, load) for load in self.devices.metered]
            )
        except ConnectionError as err:
            _LOGGER.debug("energy poll aborted: %s", err)

    async def async_refresh_meter(self, load: MeteredLoad) -> None:
        """Read one metered load's readings and counters now: `homeassistant.update_entity` on one of its sensors.

        The app reads them every 5 s while its consumption page is open (`PowerConsumptionViewModel.requestData`);
        here nothing is open, so they are read every ENERGY_POLL_INTERVAL and on demand. The meter's Sensor Gets
        (`_get_readings`) go first, then the counters (`_get_counters`), as the connect-time refresh orders them.
        A call while one is running for the same load waits for it instead of asking again: an update of all of a
        load's sensors at once is one read. Best effort: a lost link or a silent meter leaves the cached values.
        """
        running = self._meter_refreshes.get(load.address)
        if running is not None:
            await running.wait()
            return
        done = self._meter_refreshes[load.address] = asyncio.Event()
        try:
            await self._get_readings(load)
            await self._get_counters(load)
        except ConnectionError as err:
            _LOGGER.debug("%04X: meter not read: %s", load.address, err)
        finally:
            del self._meter_refreshes[load.address]
            done.set()

    async def _backfill_energy_history(self) -> None:
        """Import what each metered load counted while nobody saw it into its Energy statistics (`energy_history`).

        Right after the connect-time poll, so the counter the charts are checked against is fresh. Needs the
        recorder (an optional dependency: without it there are no statistics to fill); once per link, and not again
        within ENERGY_HISTORY_INTERVAL of the last try — the link before this one lasted until less than that ago,
        so no whole hour can be missing. Loads go REFRESH_CHUNK at a time like the poll.
        """
        now = time.monotonic()
        if (
            "recorder" not in self.hass.config.components
            or self.connected_since is None
            or (
                self._energy_history_at is not None
                and now - self._energy_history_at < ENERGY_HISTORY_INTERVAL
            )
        ):
            return
        self._energy_history_at = now
        link_up = floor_hour(datetime.fromtimestamp(self.connected_since, UTC))
        try:
            await self._chunked(
                [
                    partial(async_backfill, self, load, link_up)
                    for load in self.devices.metered
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("energy history import aborted: %s", err)

    @callback
    def _poll_energy_periodic(self, _now: datetime) -> None:
        """Start a poll every ENERGY_POLL_INTERVAL while a link is up and the previous one is through.

        The connect-time refresh ends with a poll of its own (`_after_connect`), so a tick during it is skipped too.
        """
        if not self.connected or any(
            task is not None and not task.done()
            for task in (self._refresh_task, self._energy_task)
        ):
            return
        self._energy_task = self.entry.async_create_background_task(
            self.hass, self._poll_energy(), f"{DOMAIN} energy"
        )

    async def _send_time(self, destination: int = ALL_NODES) -> None:
        """Broadcast Time Set (unacknowledged, to all nodes) as the app does after every connection.

        Devices with timers or astro schedules have no clock source but this message: the gateway never publishes
        time (its publish interval is configured to 0), so without a phone nearby their schedules drift.
        `destination`: one element instead (a new node's Time Server, `onboard`'s SetTime phase).
        """
        now = dt_util.now()
        try:
            pdu = M.time_set(now)
        except (
            ValueError
        ):  # a zone offset the message cannot carry: better a UTC clock than none
            pdu = M.time_set(now, zone_offset=timedelta(0))
        try:
            await self._while_seq_stalls(
                partial(self.proxy.send_access, destination, pdu)
            )
        except ConnectionError as err:
            _LOGGER.debug("Time Set not sent: %s", err)
        else:
            _LOGGER.debug(
                "Sent Time Set %s to %04X",
                now.isoformat(timespec="seconds"),
                destination,
            )

    async def _send_location(self) -> None:
        """Broadcast Home Assistant's home location (Generic Location Global Set Unacknowledged, to all nodes).

        The nodes compute their sunrise / sunset times from it (astro schedules, `schedules.py`); the app only sends
        the phone's position when it creates such a schedule. Every node hosts the Location Setup Server on its
        primary element, which the all-nodes address reaches, as for Time Set. Unverified on air.
        """
        config = self.hass.config
        pdu = M.generic_location_global_set(
            config.latitude, config.longitude, int(config.elevation)
        )
        try:
            await self._while_seq_stalls(
                partial(self.proxy.send_access, ALL_NODES, pdu)
            )
        except ConnectionError as err:
            _LOGGER.debug("Location not sent: %s", err)
        else:
            _LOGGER.debug("Sent the home location to all nodes")

    async def async_send_time(self, destination: int = ALL_NODES) -> None:
        """Broadcast Time Set now (the `node_clock_wrong` repair's fix); without a link nothing goes out.

        `destination`: one element instead of all nodes (`onboard`: the new node's Time Server).
        """
        await self._send_time(destination)

    @callback
    def _send_time_daily(self, _now: datetime) -> None:
        if self.connected:
            self.entry.async_create_background_task(
                self.hass, self._send_time_and_read_clocks(), f"{DOMAIN} time"
            )

    async def _send_time_and_read_clocks(self) -> None:
        """Broadcast Time Set, then ask the mains nodes for their time, zone and location (`NodeClocks.read_all`).

        Once a day and after a change of the local UTC offset, not on every link: the nodes answer the Time Set of
        each link with their Time Status all the same. Unverified on air.
        """
        await self._send_time()
        await self.clocks.read_all()

    def _arm_offset_change(self) -> None:
        """Send Time Set again right after the next change of the local UTC offset (review-3 F17).

        A Time Set carries the zone offset in force when it is sent; the nodes' timers and astro schedules run on it
        until the next one — up to TIME_SET_INTERVAL after a daylight-saving change, an hour off meanwhile.
        """
        change = next_utc_offset_change(dt_util.now())
        if change is None:
            return

        @callback
        def changed(_now: datetime) -> None:
            self._unsub_offset_change = None
            self._send_time_daily(_now)
            self._arm_offset_change()

        self._unsub_offset_change = async_track_point_in_utc_time(
            self.hass, changed, change + timedelta(seconds=OFFSET_CHANGE_DELAY)
        )

    async def async_refresh_element(
        self, addr: int, kind: str, *, quiet: bool = False
    ) -> None:
        """Ask one load for its state now, with the connect-time refresh's Get for its kind; best effort.

        The reply lands in `states` through `_on_message`, as every status does. A lost link is left for the
        caller's next send to report. `quiet`: one attempt, its miss no verdict on the node (the periodic re-probe of
        a node already known to be unreachable).
        """
        try:
            await self._get_state(addr, kind, quiet=quiet)
        except ConnectionError as err:
            _LOGGER.debug("%04X: state Get not sent: %s", addr, err)

    async def _get_state(
        self, addr: int, kind: str, *, quiet: bool = False, counted: bool = True
    ) -> None:
        """Send `kind`'s state Get to `addr` and wait for its status; a miss counts toward reachability if `counted`."""
        get, status = STATE_GETS.get(kind, ONOFF_GET)
        asked = time.monotonic()
        try:
            await self.proxy.request(
                addr,
                get(),
                status,
                retries=1 if quiet else REFRESH_RETRIES,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer its state Get", addr)
            if counted:
                self._missed_answer(addr, kind, asked, full=not quiet)

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

    async def _get_readings(self, load: MeteredLoad) -> None:
        """Ask a load's meter element for its readings, one property-qualified Sensor Get per `meter_readings` entry.

        The meter ignores an unqualified Sensor Get, so the readings are asked for one at a time; each reply is a
        Sensor Status `_on_sensor_status` stores. One attempt each: the meter publishes every change afterwards,
        so a missed reading is filled in by its next publication.
        """
        addr = load.meter_address
        assert addr is not None  # only metered loads are asked
        for pid in meter_readings(load):
            try:
                await self.proxy.request(
                    addr, M.sensor_get(pid), M.SENSOR_STATUS, retries=1
                )
            except TimeoutError:
                _LOGGER.debug(
                    "%04X did not answer its Sensor Get for property %04X", addr, pid
                )

    async def _get_counters(self, load: MeteredLoad) -> None:
        """Read the counters of one metered load: a Generic Property Get per COUNTER_READS entry it has (`counter_element`)."""
        no_manufacturer = lacks_precise_energy(self.cdb, load)
        if no_manufacturer:
            self.element_state(load.address).energy_fallback = True
        async with self._counter_locks[load.address]:
            for read in COUNTER_READS:
                if (addr := counter_element(load, read.meter)) is None:
                    continue
                if no_manufacturer and read.server == "manufacturer":
                    continue  # no server to answer it
                try:
                    await self.proxy.request(
                        addr,
                        M.generic_property_get(read.server, read.pid),
                        SIG_PROPERTY_STATUS_BY_SERVER[read.server],
                        retries=1,
                    )
                except TimeoutError:
                    _LOGGER.debug(
                        "%04X did not answer its property Get %04X", addr, read.pid
                    )

    async def reset_consumption(self, load: MeteredLoad) -> None:
        """Zero a metered load's resettable energy total (a socket's power-on hours too), as the app's "reset consumption" does.

        One acknowledged Admin Property Set per COUNTER_RESETS entry the load has (`counter_element`), each answered
        by an Admin Property Status that `_on_sig_property_status` stores. JUNG firmware may only publish the status
        of a Set that changed something, so an unanswered Set is read back with a Get. The first counter that does
        not read 0 stops the reset with CounterNotReset; an unanswered read-back raises TimeoutError. The energy
        puck's reset (0x006A on its meter) is the app's (`device-settings.md` §5.2), unverified on air.
        """
        async with self._counter_locks[load.address]:
            for pid, meter in COUNTER_RESETS:
                if (addr := counter_element(load, meter)) is not None:
                    await self._reset_counter(addr, pid)

    async def _reset_counter(self, addr: int, pid: int) -> None:
        codec = SIG_PROPERTIES[pid].codec
        key = pid.to_bytes(2, "little")

        def for_pid(m: AccessMessage) -> bool:
            return m.params[:2] == key

        try:
            status = await self.proxy.request(
                addr,
                M.generic_property_set("admin", pid, codec.encode(0)),
                M.GEN_ADMIN_PROP_STATUS,
                retries=1,
                match=for_pid,
            )
        except TimeoutError:
            status = await self.proxy.request(
                addr,
                M.generic_property_get("admin", pid),
                M.GEN_ADMIN_PROP_STATUS,
                retries=1,
                match=for_pid,
            )
        value = status.params[3:]
        if not value or codec.decode(value) != 0:
            raise CounterNotReset(addr, pid)

    # ------------------------------------------------------------------ Configuration Server audit
    async def async_audit(self, nodes: Sequence[Node]) -> list[NodeAudit]:
        """Compare the nodes' Configuration Servers with the export (`jhmesh.audit`: device-key Gets, never a Set).

        One node after the other, its Gets paced like the connect-time refresh (`_chunked`: REFRESH_CHUNK at a
        time, each retried while the sequence-number store holds sends back); a node that stays silent is a result,
        not an error.
        Each node's result is kept for the diagnostics. A lost link raises `ConnectionError`.
        """
        exchange = client_exchange(
            self.proxy, timeout=AUDIT_TIMEOUT, retries=AUDIT_RETRIES
        )
        results = []
        for node in nodes:
            result = await audit_node(exchange, node, run=self._chunked)
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

    # ------------------------------------------------------------------ heartbeats (per-node liveness)
    @property
    def heartbeat_nodes(self) -> list[Node]:
        """The nodes asked for heartbeats: every provisioned mains device (battery nodes sleep and would not beat)."""
        return [
            n for n in self.cdb.nodes if n.pid is not None and n.pid not in BATTERY_PIDS
        ]

    @property
    def heartbeat_timeout(self) -> float:
        """Seconds without a beat (or any message) after which a node counts as dead."""
        period = C.heartbeat_period_seconds(HEARTBEAT_PERIOD_LOG)
        return period * (HEARTBEAT_MISSED_BEATS + 0.5)

    def node_alive(self, address: int) -> bool:
        """Whether the node owning element `address` counts as there: it answers its requests, and beats.

        Unreachable after one request asked with the app's full budget went unanswered (`_missed_answer`, always
        on), dead after a heartbeat timeout (only with the heartbeat option); either ends with the next message from it.
        """
        if not self.unreachable and not (self.heartbeats_enabled and self._dead_nodes):
            return True
        node = self.cdb.node_by_addr(address)
        if node is None:
            return True
        return node.unicast not in self.unreachable and not (
            self.heartbeats_enabled and node.unicast in self._dead_nodes
        )

    def load_locked(self, address: int) -> bool:
        """Whether the load at `address` last reported a lock that has not run out (`ElementState.locked`)."""
        st = self.states.get(address)
        return st is not None and st.locked

    def _missed_answer(
        self,
        address: int,
        kind: str,
        asked: float,
        *,
        full: bool = True,
        command: bool = False,
    ) -> None:
        """Take note that the element at `address` left a request sent at `asked` (`time.monotonic()`) unanswered.

        The app's rule (`MeshMessengerImpl$handleError$1`: a request timeout sets the device's failed-message counter
        to the unreachable mark at once): a `full` budget exhausted — REQUEST_ATTEMPTS attempts of a state Get or a
        load's command — marks the node unreachable, and its entities unavailable until it is heard from
        (`_heard_from`). Two exceptions keep it: a node heard from since `asked` is there, only busy (the app
        completes a pending request on any User Property Status from the element, and resets the counter on any
        status, so the same node would not be unreachable there either); and a shorter probe (the link watchdog's
        one-attempt keep-alive) is no verdict — the element is asked again with a full-budget Get (its `kind`'s)
        after UNREACHABLE_RECHECK. An unreachable node is asked again every UNREACHABLE_REPROBE for as long as the
        link lasts (nothing else would ask it: its entities are unavailable, so nobody can operate them).

        Battery nodes sleep between key presses: they are never asked for their state, and a command they miss says
        nothing about whether their keys still work, so they are never marked (the app does not show them as "No
        connection" either).

        Nor is a load known to be locked (`load_locked`) for a `command` it left unanswered: a locked load may well
        ignore the Set, and is not gone for it (review-4 F4-2) — it is asked again with a state Get after
        UNREACHABLE_RECHECK, whose silence counts as any other. Whether a locked load answers a Set at all is
        unverified on air (`docs/hidden-features.md` §12).
        """
        node = self.cdb.node_by_addr(address)
        if node is None or node.pid in BATTERY_PIDS:
            return
        if node.unicast in self.unreachable:
            self._schedule_recheck(node.unicast, address, kind, UNREACHABLE_REPROBE)
            return
        if (
            not full
            or self.last_heard.get(node.unicast, -1e9) >= asked
            or (command and self.load_locked(address))
        ):
            self._schedule_recheck(node.unicast, address, kind, UNREACHABLE_RECHECK)
            return
        self._cancel_recheck(node.unicast)
        self.unreachable.add(node.unicast)
        _LOGGER.warning(
            "%s did not answer a request (%d attempts in %.0f s): marking it unavailable",
            node.name,
            REQUEST_ATTEMPTS,
            REQUEST_ATTEMPTS * REQUEST_TIMEOUT,
        )
        self._notify_node(node)
        self._schedule_recheck(node.unicast, address, kind, UNREACHABLE_REPROBE)

    def _schedule_recheck(
        self, unicast: int, address: int, kind: str, delay: float
    ) -> None:
        """Ask the node again (its element `address`, `kind`'s Get) after `delay`, unless a re-ask is pending."""
        if unicast not in self._recheck and not self._stop:
            self._recheck[unicast] = async_call_later(
                self.hass, delay, partial(self._recheck_node, unicast, address, kind)
            )

    def _cancel_recheck(self, unicast: int) -> None:
        if (cancel := self._recheck.pop(unicast, None)) is not None:
            cancel()

    @callback
    def _recheck_node(
        self, unicast: int, address: int, kind: str, _now: datetime
    ) -> None:
        """Ask a node that missed a state Get again, unless it was heard from meanwhile or the link is down."""
        del self._recheck[unicast]
        if not self.connected:
            return  # the new link's refresh asks every node again
        self.entry.async_create_background_task(
            self.hass,
            self.async_refresh_element(
                address, kind, quiet=unicast in self.unreachable
            ),
            f"{DOMAIN} reachability {address:04X}",
        )

    def _heard_from(self, address: int) -> None:
        node = self.cdb.node_by_addr(address)
        if node is None:
            return
        self.last_heard[node.unicast] = time.monotonic()
        self.last_seen[node.unicast] = dt_util.utcnow()
        self._signal_node(node.unicast)
        self._cancel_recheck(node.unicast)
        if node.unicast in self.unreachable:
            self.unreachable.discard(node.unicast)
            _LOGGER.info("%s is reachable again", node.name)
            self._notify_node(node)

    def _signal_node(self, unicast: int, *, force: bool = False) -> None:
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
            self._signal_node(node.unicast, force=True)

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

    def heartbeat_age(self, node: Node) -> float | None:
        """Seconds since the node's last Heartbeat; None when none was seen since the start."""
        beat = self.heartbeats.get(node.unicast)
        return None if beat is None else time.monotonic() - beat.received

    async def _configure_heartbeats(self) -> None:
        """Ask every mains node to publish Heartbeats to our address (`OPTION_HEARTBEATS`), at most every few hours.

        A Config Heartbeat Publication Set with the device key, one node at a time in the usual chunks; the
        publication persists in the node, so this is not repeated on every link — only when the last round is
        older than HEARTBEAT_RECONFIGURE_INTERVAL (a node that lost it, e.g. after a mains failure, gets it back
        then; until then any message it sends keeps it alive anyway). Every node gets a fresh deadline: silence
        counts only from here.
        """
        if not self.heartbeats_enabled:
            return
        now = time.monotonic()
        if (
            self._heartbeats_configured_at is not None
            and now - self._heartbeats_configured_at < HEARTBEAT_RECONFIGURE_INTERVAL
        ):
            return
        pdu = C.heartbeat_publication_set(
            self.proxy.state.src, HEARTBEAT_PERIOD_LOG, ttl=self.proxy.ttl
        )
        # before the first Set: any node may take it
        self._set_heartbeats_publishing({node.unicast for node in self.heartbeat_nodes})
        try:
            await self._chunked(
                [
                    partial(self._set_heartbeat, node, pdu, "configure")
                    for node in self.heartbeat_nodes
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("heartbeat configuration aborted: %s", err)
            return
        self._heartbeats_configured_at = time.monotonic()

    async def async_disable_heartbeats(self) -> None:
        """Tell the mains nodes to stop publishing Heartbeats (the option was switched off).

        The nodes still to be told are `heartbeats_publishing` (every heartbeat node when nothing is recorded);
        each that confirms leaves the list, and the next link with the option off asks the rest again
        (`_after_connect`) — the publication has CountLog 0xFF, a node told nothing publishes forever.
        """
        pending = self.heartbeats_publishing
        nodes = [n for n in self.heartbeat_nodes if not pending or n.unicast in pending]
        pdu = C.heartbeat_publication_set(0x0000, C.HEARTBEAT_PERIOD_OFF, count_log=0)
        confirmed: set[int] = set()
        try:
            await self._chunked(
                [
                    partial(self._disable_heartbeat, node, pdu, confirmed)
                    for node in nodes
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("disabling heartbeats aborted: %s", err)
        # what is left to tell: of the nodes asked, those that did not confirm (a node no longer in the export
        # cannot be asked, nor needs to be)
        self._set_heartbeats_publishing({n.unicast for n in nodes} - confirmed)

    async def _disable_heartbeat(
        self, node: Node, pdu: bytes, confirmed: set[int]
    ) -> None:
        if await self._set_heartbeat(node, pdu, "disable"):
            confirmed.add(node.unicast)

    @property
    def heartbeats_publishing(self) -> set[int]:
        """Nodes that may still publish heartbeats to us (`CONF_HEARTBEATS_PUBLISHING`, unicasts as hex)."""
        recorded = self.entry.data.get(CONF_HEARTBEATS_PUBLISHING)
        if (
            recorded is True
        ):  # one flag, as an earlier 0.3.0 build wrote it: any node may
            return {node.unicast for node in self.heartbeat_nodes}
        return {int(address, 16) for address in recorded or []}

    def _set_heartbeats_publishing(self, nodes: set[int]) -> None:
        """Record which nodes may still publish heartbeats to us; not hub data, so this reloads nothing."""
        if (
            CONF_HEARTBEATS_PUBLISHING in self.entry.data
            and self.heartbeats_publishing == nodes
        ):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_HEARTBEATS_PUBLISHING: [f"{n:04X}" for n in sorted(nodes)],
            },
        )

    async def _set_heartbeat(self, node: Node, pdu: bytes, what: str) -> bool:
        """Send one Heartbeat Publication Set; whether the node confirmed it."""
        try:
            reply = await self.proxy.request_config(
                node.unicast, pdu, C.CONFIG_HEARTBEAT_PUBLICATION_STATUS, retries=1
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer the heartbeat %s", node.unicast, what)
            if what == "configure":
                # a node that did not even answer the Set (off, out of range) is the case liveness is for: it gets
                # a deadline like the others, counts as dead when it passes and is asked again every
                # HEARTBEAT_REPROBE_INTERVAL (`_check_heartbeats`) — an answer then revives it. A node that did
                # answer earlier keeps its deadline (`setdefault`).
                self._alive_deadline.setdefault(
                    node.unicast, time.monotonic() + self.heartbeat_timeout
                )
            return False
        try:
            status = C.decode_heartbeat_publication_status(reply.params)
        except ValueError as err:
            _LOGGER.debug(
                "%04X: malformed Heartbeat Publication Status: %s", node.unicast, err
            )
            return False
        if not status.ok:
            _LOGGER.warning(
                "%s refused the heartbeat %s: %s", node.name, what, status.status_name
            )
            return False
        if what == "disable" and status.enabled:
            # a success status only says the Set was taken; what the node publishes now is in the rest of it
            _LOGGER.warning(
                "%s answered the heartbeat disable but still publishes to %04X",
                node.name,
                status.destination,
            )
            return False
        if what == "configure":
            self._alive_deadline[node.unicast] = (
                time.monotonic() + self.heartbeat_timeout
            )
        elif what == "reconfigure":
            self._mark_alive(node.unicast)  # it answered: back, and publishing again
        return True

    def _beats_stopped(self, node: Node, now: float) -> bool:
        """Whether the node beat since the current configuration but not for a whole timeout: its publication is gone although it talks."""
        beat = self.heartbeats.get(node.unicast)
        configured = self._heartbeats_configured_at
        return (
            beat is not None
            and configured is not None
            and beat.received >= configured
            and now - beat.received >= self.heartbeat_timeout
        )

    async def _reprobe_dead(self, nodes: list[Node]) -> None:
        """Send the heartbeat configuration to dead (or no longer beating) nodes again; an answer revives a dead one."""
        pdu = C.heartbeat_publication_set(
            self.proxy.state.src, HEARTBEAT_PERIOD_LOG, ttl=self.proxy.ttl
        )
        try:
            await self._chunked(
                [
                    partial(self._set_heartbeat, node, pdu, "reconfigure")
                    for node in nodes
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("heartbeat reprobe aborted: %s", err)

    @callback
    def _on_heartbeat(self, beat: Heartbeat) -> None:
        """Remember a node's Heartbeat and mark the node alive."""
        node = self.cdb.node_by_addr(beat.src)
        if node is None:
            return
        self.heartbeats[node.unicast] = beat
        self._mark_alive(beat.src)
        self._signal_node(
            node.unicast, force=True
        )  # hops change rarely: show them at once

    def _mark_alive(self, address: int) -> None:
        node = self.cdb.node_by_addr(address)
        if node is None or node.unicast not in self._alive_deadline:
            return  # not a node we asked for heartbeats (yet)
        self._alive_deadline[node.unicast] = time.monotonic() + self.heartbeat_timeout
        if node.unicast in self._dead_nodes:
            self._dead_nodes.discard(node.unicast)
            _LOGGER.info("%s is back (heard from it again)", node.name)
            self._notify_node(node)

    @callback
    def _check_heartbeats(self, _now: datetime) -> None:
        """Mark the nodes whose deadline passed as dead and tell their entities; renew the publications when due.

        A link that stays up for days would otherwise never repeat the configuration (`_after_connect` runs it
        once per link). A dead node is asked again every HEARTBEAT_REPROBE_INTERVAL (`_reprobe_dead`): a node
        that rebooted — a mains blip, seen on a mini actuator — starts with an empty heartbeat
        publication and would otherwise stay unavailable until the next renewal, hours later. So is a node whose
        beats stopped while its other traffic keeps it alive (a metering socket after a power cut:
        it publishes readings every minute, so it never counts as dead, and it would not beat again before the
        renewal either).
        """
        now = time.monotonic()
        if (
            self.connected
            and self._heartbeats_configured_at is not None
            and now - self._heartbeats_configured_at >= HEARTBEAT_RECONFIGURE_INTERVAL
            and (self._heartbeat_task is None or self._heartbeat_task.done())
        ):
            self._heartbeat_task = self.entry.async_create_background_task(
                self.hass, self._configure_heartbeats(), f"{DOMAIN} heartbeats"
            )
        for node in self.heartbeat_nodes if self.connected else ():
            # while the link is down nobody can be heard: the silence is the link's (`_connect_to` renews the
            # deadlines when it is back), and the entities are unavailable anyway
            deadline = self._alive_deadline.get(node.unicast)
            if deadline is None or now < deadline or node.unicast in self._dead_nodes:
                continue
            self._dead_nodes.add(node.unicast)
            _LOGGER.warning(
                "%s has not been heard from for %.0f s: marking it unavailable",
                node.name,
                self.heartbeat_timeout,
            )
            self._notify_node(node)
        due = [
            node
            for node in self.heartbeat_nodes
            if (node.unicast in self._dead_nodes or self._beats_stopped(node, now))
            and now - self._reprobed_at.get(node.unicast, -HEARTBEAT_REPROBE_INTERVAL)
            >= HEARTBEAT_REPROBE_INTERVAL
        ]
        if (
            due
            and self.connected
            and (self._reprobe_task is None or self._reprobe_task.done())
        ):
            for node in due:
                self._reprobed_at[node.unicast] = now
            self._reprobe_task = self.entry.async_create_background_task(
                self.hass, self._reprobe_dead(due), f"{DOMAIN} heartbeat reprobe"
            )

    def _notify_node(self, node: Node) -> None:
        """Tell the node's entities, and the mesh health sensors, that its reachability changed."""
        for element in node.elements:
            async_dispatcher_send(
                self.hass, SIGNAL_UPDATE.format(self.entry.entry_id, element.address)
            )
        async_dispatcher_send(
            self.hass, SIGNAL_REACHABILITY.format(self.entry.entry_id)
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
        self._rx_messages += 1
        self._rx_decoded_link += 1
        self._note_seq(m.src, m.seq)
        self._heard_from(m.src)
        if self.heartbeats_enabled:
            self._mark_alive(m.src)  # any message is as good as a heartbeat
        if self._export_stale:
            self._report_export_stale(
                False
            )  # our keys opened something: the export fits the mesh after all
        if m.dst == self.proxy.state.src:
            self._rx_to_us += 1
            if self._pdus_dropped:
                self._report_pdus_dropped(
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
        """Store a SIG Generic Property Status `[pid u16][user access u8][value]`: a socket counter `_poll_energy` reads.

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
        if self._from_button(m, p) and p:
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
        if len(p) < 2 or self._is_repeat(m.src, m.opcode, p):
            return
        number = int.from_bytes(p[:2], "little")
        if self._is_button(m.src):
            self.fire_button(m.src, "scene", {"scene": number})
            return
        from .event import fire_scene_recalled  # noqa: PLC0415

        fire_scene_recalled(self.hass, self, number, m.src)

    @register_status_handler(M.SCENE_STATUS)
    def _on_scene_status(self, m: AccessMessage, p: bytes) -> None:
        """Store an element's current scene; a published one after a recall nobody reported is the scene event.

        JUNG nodes publish `[status][current]` to their group after each recall they carry out; an answer to a
        Scene Get only updates the state: ours (`_get_current_scenes`) — sent to us, or published while the Get
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
            and m.src not in self._scene_gets
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
        """Report a rocker dimming its load (Generic Level / Delta / Move Set), then the hold that makes (`_dim_hold`)."""
        if self._from_button(m, p):
            self.fire_button(m.src, "dim", {"target": f"{m.dst:04X}", "raw": p.hex()})
            self._dim_hold(m, p)

    def _dim_hold(self, m: AccessMessage, p: bytes) -> None:
        """Derive `hold_start` / `hold_end` with a `direction` from a key's Move / Delta Sets (review-3 F12).

        The vendor gestures of a gateway-mode key say when a hold starts and ends; a key wired straight to a dimmer
        only sends its Level client's messages. Unverified on air (`DIM_HOLD_QUIET`), from the SIG semantics: a Move
        Set with a delta starts a hold (its sign is the direction: `up` brighter, `down` darker) and a Move Set 0
        ends it; the first Delta Set of a transaction (TID) starts one, the next ones of the same transaction
        continue it, and it ends `DIM_HOLD_QUIET` seconds after the last — or at a Delta Set 0. A new start while a
        hold runs (its stop was lost) ends that hold first. A Level Set moves to a level: no hold. Whatever its kind,
        a hold ends `DIM_HOLD_MAX` seconds after it started, with `reason: timeout` (review-4 R4-7: a lost Move 0
        used to leave it open for good); a Move 0 that still comes then ends nothing.
        """
        if m.opcode in (M.GEN_MOVE_SET, M.GEN_MOVE_SET_UNACK) and len(p) >= 3:
            kind, delta, tid = (
                "move",
                int.from_bytes(p[:2], "little", signed=True),
                p[2],
            )
        elif m.opcode in (M.GEN_DELTA_SET, M.GEN_DELTA_SET_UNACK) and len(p) >= 5:
            kind, delta, tid = (
                "delta",
                int.from_bytes(p[:4], "little", signed=True),
                p[4],
            )
        else:
            return
        addr, hold = m.src, self._dim_holds.get(m.src)
        if delta == 0:
            if hold is not None:
                self._end_dim_hold(addr)
            return
        direction = KEY_EVENT_SIDE_UP if delta > 0 else KEY_EVENT_SIDE_DOWN
        if hold is not None and (hold.kind, hold.tid, hold.direction) == (
            kind,
            tid,
            direction,
        ):
            if kind == "delta":  # the transaction goes on: its end is later
                self._quiet_dim_hold(addr, hold)
            return
        if hold is not None:
            self._end_dim_hold(addr)
        hold = DimHold(kind, tid, direction, f"{m.dst:04X}")
        self._dim_holds[addr] = hold
        if kind == "delta":
            self._quiet_dim_hold(addr, hold)
        hold.limit = async_call_later(
            self.hass, DIM_HOLD_MAX, partial(self._dim_hold_limit, addr)
        )
        self.fire_button(
            addr, "hold_start", {"target": hold.target, "direction": direction}
        )

    def _quiet_dim_hold(self, addr: int, hold: DimHold) -> None:
        """(Re)arm the end of a Delta transaction's hold, `DIM_HOLD_QUIET` seconds from now."""
        hold.cancel_quiet()
        hold.quiet = async_call_later(
            self.hass, DIM_HOLD_QUIET, partial(self._dim_hold_quiet, addr)
        )

    @callback
    def _dim_hold_quiet(self, addr: int, _now: datetime) -> None:
        self._dim_holds[addr].quiet = None
        self._end_dim_hold(addr)

    @callback
    def _dim_hold_limit(self, addr: int, _now: datetime) -> None:
        self._dim_holds[addr].limit = None
        self._end_dim_hold(addr, HOLD_END_TIMEOUT)

    def _end_dim_hold(self, addr: int, reason: str | None = None) -> None:
        """End the dimming hold of `addr`: its `hold_end`, with `reason` when its stop did not come (HOLD_END_REASONS)."""
        hold = self._dim_holds.pop(addr)
        hold.cancel_timers()
        attrs: dict[str, Any] = {"target": hold.target, "direction": hold.direction}
        if reason is not None:
            attrs[ATTR_REASON] = reason
        self.fire_button(addr, "hold_end", attrs)

    def _end_key_hold(self, addr: int, reason: str) -> None:
        """End the gateway-mode hold of `addr` without its release: `hold_end` with its side and `reason`.

        The hold stays known as ended, so the release that may still come is not a second `hold_end`.
        """
        hold = self._key_holds[addr]
        hold.cancel_limit()
        hold.ended = True
        attrs: dict[str, Any] = {ATTR_REASON: reason}
        if hold.side is not None:
            attrs["side"] = hold.side
        self.fire_button(addr, "hold_end", attrs)

    @callback
    def _key_hold_limit(self, addr: int, _now: datetime) -> None:
        self._key_holds[addr].limit = None
        self._end_key_hold(addr, HOLD_END_TIMEOUT)

    def _end_holds(self, reason: str) -> None:
        """End every hold in progress, its `hold_end` saying why (decision M11: a `reason`, not a silent drop).

        Without a link the stop cannot be heard, and a stopping hub hears nothing more, so a dim-while-held
        automation would otherwise never be told to stop. The `reason` lets it tell this from a real release: the
        key may still be held, and a node wired straight to a dimmer may still be dimming.
        """
        for addr in list(self._dim_holds):
            self._end_dim_hold(addr, reason)
        for addr, hold in list(self._key_holds.items()):
            if not hold.ended:
                self._end_key_hold(addr, reason)

    @callback
    def _end_holds_on_link_loss(self, _end: LinkEnd) -> None:
        self._end_holds(HOLD_END_LINK_LOST)

    @register_status_handler(VENDOR_USER_PROPERTY_SET_UNACK, company_id=M.JUNG_CID)
    def _on_vendor_property_set(self, m: AccessMessage, p: bytes) -> None:
        """Gateway-mode keys publish their gestures as User Property 0x5012 `[counter][code]`."""
        if len(p) >= 4 and int.from_bytes(p[:2], "little") == PROPERTY_BUTTON_EVENT:
            self._button_event(m.src, p[2], p[3])

    # ------------------------------------------------------------------ buttons
    def _is_button(self, addr: int) -> bool:
        return isinstance(self.devices.by_address.get(addr), Button)

    def _from_button(self, m: AccessMessage, p: bytes) -> bool:
        """Whether `m` is a client message from a key element, and not the firmware's second copy of it."""
        return self._is_button(m.src) and not self._is_repeat(m.src, m.opcode, p)

    @callback
    def add_event_listener(self, addr: int, cb: EventListener) -> Callable[[], None]:
        """Register `cb` for button events from `addr`; returns the unsubscribe callable."""
        self._event_listeners.setdefault(addr, []).append(cb)
        return lambda: self._event_listeners[addr].remove(cb)

    @callback
    def fire_button(self, addr: int, event: str, attrs: dict[str, Any]) -> None:
        """Deliver a button event of the element at `addr` to its listeners, then publish it on the bus.

        The bus event (`event.publish_button_event`) comes from here rather than from the key's event entity, so a
        disabled entity no longer silences the key's device triggers and logbook lines (review-4 H4-2); it follows
        the listeners, so an automation it starts sees the entity's new state.
        """
        from .event import publish_button_event  # noqa: PLC0415

        for cb in self._event_listeners.get(addr, []):
            cb(event, attrs)
        publish_button_event(self.hass, self, addr, event, attrs)

    def _is_repeat(self, src: int, op: int, p: bytes) -> bool:
        """Second copy of a client message (the firmware publishes everything twice, ~1 s apart, fresh SEQ, same TID).

        The payload is part of the key on purpose: a Delta Set transaction legitimately reuses its TID with growing
        deltas while the key is held, and those must all reach the `dim` listeners.
        """
        offset = TID_OFFSET.get(op)
        if offset is None or len(p) <= offset:
            return False  # no TID to compare
        now = time.monotonic()
        self._sig_recent = {
            k: t for k, t in self._sig_recent.items() if now - t < TID_REPEAT_WINDOW
        }
        key = (src, op, p)
        if key in self._sig_recent:
            return True
        self._sig_recent[key] = now
        return False

    @staticmethod
    def _event_attrs(counter: int, side: str | None) -> dict[str, Any]:
        """Return the attributes of a vendor button event: the counter, plus the rocker side when the code carries one."""
        attrs: dict[str, Any] = {"counter": counter}
        if side is not None:
            attrs["side"] = side
        return attrs

    def _button_event(self, addr: int, counter: int, code: int) -> None:
        """Turn a KEY_EVT code into a gesture event (`KEY_EVENTS`), deduplicated and with double clicks derived.

        A rocker half's codes (0-3) carry the side as the `side` attribute; a release (4) ends the hold that
        started on the same element and reports that hold's side, so `hold_start` / `hold_end` pair up. A double
        click is two clicks of the same key, hence of the same side, within DOUBLE_CLICK_WINDOW. A code the
        firmware table does not know is still delivered (as `code_xx`: the key is awake, which the battery sensor
        cares about) and logged once per key and code.
        """
        now = time.monotonic()
        # every event is published twice by the firmware, 1-2 s apart; on a double press the copies interleave with
        # the second press (16 05, 17 05, 16 05, 17 05), so the last counter alone is not enough to spot them
        recent = [
            (c, t)
            for c, t in self._button_recent.get(addr, ())
            if now - t < BUTTON_REPEAT_WINDOW
        ]
        self._button_recent[addr] = recent
        if any(c == counter for c, _ in recent):
            return
        recent.append((counter, now))
        if (known := KEY_EVENTS.get(code)) is None:
            if (addr, code) not in self._unknown_codes:
                self._unknown_codes.add((addr, code))
                _LOGGER.info(
                    "Key %04X sent button event code 0x%02X (counter %d), which is not in the firmware's table; "
                    "further ones are not logged",
                    addr,
                    code,
                    counter,
                )
            name, side = f"code_{code:02x}", None
        else:
            name, side = known
        if name == "hold_start":
            self._start_key_hold(addr, side)
        elif code == KEY_EVENT_RELEASE:
            hold = self._key_holds.pop(addr, None)
            if hold is not None:
                hold.cancel_limit()
                if hold.ended:
                    return  # its hold_end went out already (DIM_HOLD_MAX, the link)
                side = hold.side
        if name == "click":
            last = self._button_last_click.get(addr)
            self._button_last_click[addr] = (now, side)
            if (
                last is not None
                and last[1] == side
                and now - last[0] <= DOUBLE_CLICK_WINDOW
            ):
                # a click held back is the first half of this double click: never reported on its own
                self._cancel_delayed_click(addr)
                self.fire_button(addr, "double_click", self._event_attrs(counter, side))
                return
            # a click of the other half of the rocker is no double click: a click held back is reported first
            self._flush_delayed_click(addr)
            if self.click_delay:
                # hold it back until a second click can no longer follow
                cancel = async_call_later(
                    self.hass,
                    DOUBLE_CLICK_WINDOW,
                    partial(self._delayed_click_due, addr, counter, side),
                )
                self._delayed_clicks[addr] = (cancel, counter, side)
                return
        else:
            # any other gesture ends the wait: the click is reported first, then the gesture, in order
            self._flush_delayed_click(addr)
        self.fire_button(addr, name, self._event_attrs(counter, side))

    def _start_key_hold(self, addr: int, side: str | None) -> None:
        """Note a gateway-mode hold of `addr`, ending at DIM_HOLD_MAX unless its release comes first.

        A hold still open (its release was lost) ends first, with a plain `hold_end`, as a dimming hold does.
        """
        if (running := self._key_holds.pop(addr, None)) is not None:
            running.cancel_limit()
            if not running.ended:
                self._flush_delayed_click(addr)
                attrs: dict[str, Any] = {}
                if running.side is not None:
                    attrs["side"] = running.side
                self.fire_button(addr, "hold_end", attrs)
        self._key_holds[addr] = KeyHold(
            side,
            async_call_later(
                self.hass, DIM_HOLD_MAX, partial(self._key_hold_limit, addr)
            ),
        )

    @callback
    def _delayed_click_due(
        self, addr: int, counter: int, side: str | None, _now: datetime
    ) -> None:
        self._delayed_clicks.pop(addr, None)
        self.fire_button(addr, "click", self._event_attrs(counter, side))

    def _cancel_delayed_click(self, addr: int) -> tuple[int, str | None] | None:
        """Drop the click held back for `addr`, if any; returns its counter and side."""
        if (held := self._delayed_clicks.pop(addr, None)) is None:
            return None
        held[0]()
        return held[1], held[2]

    def _flush_delayed_click(self, addr: int) -> None:
        """Report the click held back for `addr` now, if any."""
        if (held := self._cancel_delayed_click(addr)) is not None:
            self.fire_button(addr, "click", self._event_attrs(*held))

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
        self._beacon_authenticated = True
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
        self._rx_undecodable_link += 1
        if (
            not self._export_stale
            and self._rx_decoded_link == 0
            and self._rx_undecodable_link >= EXPORT_STALE_THRESHOLD
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
            self._rx_undecodable_link,
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

    def _report_pdus_dropped(self, dropped: bool) -> None:
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
            self._report_pdus_dropped(False)
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
        if not self.connected and self._lost_at is not None and not self._stop:
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
        the link watchdog probes the proxy at once, and the node is marked unreachable (`_missed_answer`: `load` and
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
