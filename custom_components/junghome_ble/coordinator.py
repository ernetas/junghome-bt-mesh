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
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

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

from .bus_events import fire_scene_recalled, publish_button_event
from .const import (
    DEFAULT_HEARTBEATS,
    DOMAIN,
    HUB_DATA_KEYS,
    IDENTIFY_SECONDS,
    ISSUE_INSERT_MISMATCH,
    ISSUE_KEY_REFRESH,
    ISSUE_NODE_CLOCK_WRONG,
    ISSUE_UNKNOWN_NODES,
    ISSUE_VAULT_KEY_REFRESH,
    LINK_SEARCHING,
    LIVE_OPTIONS,
    NODE_DIAGNOSTICS_INTERVAL,
    NODE_INFO,
    NODE_INFO_TIME_ROLE,
    OPTION_HEARTBEATS,
    REBUILD_OPTION_DEFAULTS,
    REFRESH_CHUNK,
    REQUEST_ATTEMPTS,
    SIGNAL_NODE,
    SIGNAL_SCENES,
    SIGNAL_UPDATE,
    STOP_TIMEOUT,
    TIME_SET_INTERVAL,
    issue_id,
)
from .device_info import update_node_device
from .element_state import ElementState
from .hub.clock import Clock
from .hub.energy import (
    COUNTER_FIELDS,
    PROPERTY_PRECISE_TOTAL_ENERGY,
    SENSOR_FIELDS,
    Energy,
)
from .hub.export_watch import (
    ExportWatch,
)
from .hub.gestures import ButtonGestures, EventListener
from .hub.issues import Issues
from .hub.lifecycle import Lifecycle, async_cancel_task
from .hub.link import LINK_HISTORY, LinkEnd, LinkManager, LinkRecord
from .hub.liveness import Liveness
from .hub.refresh import ONOFF_GET, STATE_GETS, Refresh
from .identity import async_vault_keeper
from .inserts import NodeInserts
from .jhmesh import config_messages as C
from .jhmesh import messages as M
from .jhmesh import vendor_models as V
from .jhmesh.access import AccessMessage
from .jhmesh.advert import JungAdvertisement, mac_from_uuid
from .jhmesh.audit import NodeAudit, audit_node, client_exchange
from .jhmesh.cdb import CDB, Node
from .jhmesh.client import (
    MESH_PROXY_SERVICE,
    NET_KEY_INDEX,
    Heartbeat,
    ProxyClient,
    SequenceStalled,
    classify_proxy_advert,
)
from .jhmesh.crypto import NetKeyMaterial
from .jhmesh.devices import (
    BATTERY_PIDS,
    Devices,
    Light,
    Metadata,
    MeteredLoad,
    Socket,
    build_devices,
)
from .jhmesh.keyrefresh import KeyRefreshRecord
from .jhmesh.pdu import ALL_NODES, SecureNetworkBeacon, is_group, is_unicast
from .jhmesh.properties import (
    SIG_PROPERTIES,
    SIG_SOFTWARE_VERSION,
    Scaled,
)
from .keep_awake import KeepAwake
from .node_clocks import NodeClocks
from .node_info import (
    NODE_VERSIONS,
    NODE_VERSIONS_SAVE_DELAY,
    async_load_node_versions,
    node_versions_store,
)
from .seq_store import (
    SEQ_STALL_RETRY,
    AddressShared,
    HAState,
    _stored_key_refresh,
    async_load_state,
    async_migrate_legacy_seq_store,
    seq_store,
)
from .vault_refresh import VaultKeyRefresh

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .identity import VaultKeeper
    from .protocols import (
        AppFollowView,
        ConfiguratorView,
        GatewayPollsView,
        TrackedPlatform,
    )

_LOGGER = logging.getLogger(__name__)


# The hub's timings (they lived in `const.py`). How often the heartbeat deadlines are checked
# (`OPTION_HEARTBEATS`, `Liveness.check_heartbeats`).
HEARTBEAT_CHECK_INTERVAL: Final = 30.0
# A Scene Status a node publishes this soon after a recall of the same scene that fired EVENT_SCENE_RECALLED (a
# key's, the app's, ours) is that recall's echo; later, it is a recall Home Assistant did not hear (captured on air:
# the members' Scene Status follow the app's Scene Recall within 50 ms in the app settings capture).
SCENE_RECALL_WINDOW: Final = 5.0
# The Configuration Server audit (`junghome_ble.audit_network`): per Get, the wait for its status and the attempts
# before it counts as unanswered — the budget of a configuration change's messages
# (`configurator.executor.CONFIG_TIMEOUT`)
AUDIT_TIMEOUT: Final = 3.0
AUDIT_RETRIES: Final = 2
# a group command or a movement the mesh does not answer within this has the link watchdog probe the proxy at once
# (`const.LINK_LOSS_GRACE`)
COMMAND_ECHO_TIMEOUT: Final = 5.0
# how often the sequence space of every source is checked (`sequence_space_low`, `Issues.check_sequence_space`)
SEQUENCE_CHECK_INTERVAL: Final = 600.0
# JUNG firmware stores its sequence number in blocks of 0x10000 and continues from the next block after a restart
# (seen on two nodes): a jump into a new block that lands near its start, from a number not near the end of the
# previous one, is a restart (a mains blip, a breaker), not the counter running on.
RESTART_BLOCK: Final = 0x10000
RESTART_SLACK: Final = 0x0400


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
SIG_PROPERTY_STATUS_OPCODES = (
    M.GEN_USER_PROP_STATUS,
    M.GEN_ADMIN_PROP_STATUS,
    M.GEN_MANU_PROP_STATUS,
)
# The vendor message gateway-mode keys publish their gestures with (docs/cross-repo-analysis.md §1.2).
VENDOR_USER_PROPERTY_SET_UNACK = 0x10  # LBC User Property Set Unacknowledged
PROPERTY_BUTTON_EVENT = 0x5012  # KEY_EVT: [counter][code]
GENERIC_LEVEL_OPCODES = frozenset(
    {M.GEN_LEVEL_SET, M.GEN_LEVEL_SET_UNACK, 0x8209, 0x820A, 0x820B, 0x820C}
)
SCENE_ACTION_SETUP = (
    "05271017"  # the JUNG vendor model that says what an element does in a scene
)
SCENE_SETUP_SERVER = (
    "1204"  # SIG Scene Setup Server: the element holding a node's scene register
)
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


@dataclass
class KnownMesh:
    """What Bluetooth discovery recognises an entry's mesh by: its keys and its nodes.

    Not the entry's unique id: that is the mesh UUID, which no advertisement carries. `keys`: the
    export's NetKeys (both of an export written mid key refresh) and the key of a refresh the hub follows, whatever
    its phase — a proxy advertising the Network ID or a Node Identity of any of them is this mesh
    (`jhmesh.client.classify_proxy_advert` over `unicasts`, the export's nodes). `macs`: the export's nodes, which
    advertise from their public MAC (`jhmesh.advert.mac_from_uuid`) whatever key they hold — a node of this mesh
    after a key refresh the export lacks is still this mesh, and a new entry for it could only end at
    `mesh_already_configured`. Unverified on air (no key refresh on this installation); the MAC is the node UUID on
    every node here.
    """

    keys: list[NetKeyMaterial]
    unicasts: tuple[int, ...]
    macs: frozenset[str]

    @property
    def network_ids(self) -> set[bytes]:
        """Return the Network IDs of `keys`."""
        return {key.network_id for key in self.keys}

    def add_key(self, key: NetKeyMaterial) -> None:
        """Recognise the mesh under `key` too (a key refresh the hub follows)."""
        if key.network_id not in self.network_ids:
            self.keys.append(key)

    def recognises(self, service_data: bytes, address: str) -> bool:
        """Whether a proxy advertising `service_data` (0x1828) from `address` is a node of this mesh."""
        return (
            address.upper() in self.macs
            or classify_proxy_advert(service_data, self.keys, self.unicasts) is not None
        )


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
    known = KnownMesh(
        list(cdb.rx_net_keys(NET_KEY_INDEX)),
        tuple(n.unicast for n in cdb.nodes),
        node_macs(cdb),
    )
    data = await seq_store(hass, cdb).async_load()
    if (stored := _stored_key_refresh(data, f"{unicast:04X}")) is not None:
        known.add_key(NetKeyMaterial.derive(KeyRefreshRecord.from_stored(stored).key))
    return known


def remember_known_mesh(hass: HomeAssistant, entry_id: str, known: KnownMesh) -> None:
    """Record what discovery knows the entry's mesh by (`KNOWN_MESHES`), in place of anything recorded before."""
    hass.data.setdefault(KNOWN_MESHES, {})[entry_id] = known


def forget_known_mesh(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the entry's `KNOWN_MESHES` record: its export changed or it is gone."""
    hass.data.get(KNOWN_MESHES, {}).pop(entry_id, None)


@callback
def abort_discovery_flows(
    hass: HomeAssistant, network_id: str, *, keep_flow: str | None = None
) -> None:
    """Abort our flows holding the unique id `network_id` (a discovery's, until its export is given) but `keep_flow`."""
    for flow in hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, include_uninitialized=True, match_context={"unique_id": network_id}
    ):
        if flow["flow_id"] != keep_flow:
            hass.config_entries.flow.async_abort(flow["flow_id"])


async def async_release_network_id(
    hass: HomeAssistant,
    entry: ConfigEntry,
    network_id: str,
    *,
    keep_flow: str | None = None,
) -> None:
    """Drop what discovery left for `network_id`, `entry`'s own mesh: its flows, an ignored entry holding it.

    After a key refresh the proxies advertise the new Network ID: a discovery flow it started before Home Assistant
    knew the key as this mesh's (its unique id is the Network ID until an export is given) is the entry's own mesh,
    and so is an entry the user made by *Ignore* on such a flow. The entry's own unique id is the mesh UUID and
    never moves. `keep_flow` is the flow asking (a reconfigure), which must not abort itself.
    Unverified on air.
    """
    abort_discovery_flows(hass, network_id, keep_flow=keep_flow)
    holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, network_id)
    if holder is not None and holder.source == SOURCE_IGNORE:
        _LOGGER.info(
            "Removing the ignored discovery of %s's mesh: it advertised the mesh's new network key",
            entry.title,
        )
        await hass.config_entries.async_remove(holder.entry_id)


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


def hub_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return the options a hub is built from: all but `LIVE_OPTIONS`, a default standing for an option not stored.

    The update listener reloads the entry when they changed; a change to the others applies in place.
    """
    return {
        **REBUILD_OPTION_DEFAULTS,
        **{key: value for key, value in options.items() if key not in LIVE_OPTIONS},
    }


# a config entry of this integration: its `runtime_data` is the entry's hub
type JungHomeConfigEntry = ConfigEntry[JungHomeHub]


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
        # the mesh's provisioner identity and device-key vault (`identity.py`)
        self.vault = vault
        self.devices = devices
        self.state = (
            state  # our address, sequence number and IV state, persisted per mesh
        )
        # every timer and task the hub cancels when it stops, by name (`hub/lifecycle.py`)
        self.lifecycle = Lifecycle()
        # the repair issues and their fixes (`hub/issues.py`); it watches the store's stalls
        self.issues = Issues(self)
        state.stall_listener = self.issues.seq_stall_started
        # the nodes' reachability and heartbeats (`hub/liveness.py`)
        self.liveness = Liveness(self)
        # the proxy link: its loop, watchdog and grace (`hub/link.py`)
        self.link = LinkManager(self)
        self.proxy: ProxyClient = ProxyClient(
            cdb,
            state,
            on_message=self._on_message,
            on_disconnect=self.link.on_disconnect,
            on_beacon=self._on_beacon,
            on_filter_status=self.link.on_filter_status,
            on_undecryptable=self._on_undecryptable,
            on_heartbeat=self.liveness.on_heartbeat,
            on_key_refresh=self._on_key_refresh,
            on_foreign_own_source=self.issues.on_foreign_own_source,
            on_iv_update_abandoned=self.issues.check_iv_update,
            on_control=self._on_control,
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
        self.link_count = 0  # links made so far: entities tell "asked on this link already" from "ask again" by it
        self.link_state = LINK_SEARCHING  # one of LINK_STATES (`LinkManager.set_link_state`), for the link state sensor
        # the connect-time reads of every link (`hub/refresh.py`)
        self.refresh = Refresh(self)
        # the metered loads' polls and reads, and the time and location broadcasts (`hub/energy.py`, `hub/clock.py`)
        self.energy = Energy(self)
        self.clock = Clock(self)
        self.stopping = False  # `async_stop` began: nothing new is scheduled
        # what the proxy forwarded — decoded access messages, those unicast to us (proof that nodes accept our PDUs),
        # what our keys could not open — is counted by the proxy client, per link and in total (`proxy.link_stats`,
        # `proxy.total_stats`): drop detection and stale-export detection read it there
        # per link: whether the proxy's Secure Network Beacon authenticated (it sends one right after we subscribe)
        self.beacon_authenticated = False
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
        self.configurator: ConfiguratorView | None = None
        # what follows the changes made in the JUNG HOME app (`app_follow.py`), set by the setup
        self.app_follow: AppFollowView | None = None
        # the platforms' entities, by platform (`entity.async_setup_platform`), and what makes the hub follow the
        # export after a change, in place or by a reload (`model_update.async_follow_export`, set by the setup)
        self.platforms: dict[str, TrackedPlatform[JungHomeHub]] = {}
        self.follow_export: Callable[[], Awaitable[None]] | None = None
        # the gateway's REST status polls (`gateway_status.gateway_polls`), made by the first platform that asks
        self.gateway_polls: GatewayPollsView | None = None
        # the unknown nodes, the export refresh and the gateway's trust (`hub/export_watch.py`)
        self.export_watch = ExportWatch(self)
        # the battery nodes a Config plan or a property change keeps awake (`keep_awake.py`)
        self.keep_awake: KeepAwake = KeepAwake(self)
        # the devices Home Assistant added, carried through the app's key refresh (`vault_refresh.py`)
        self.vault_refresh = VaultKeyRefresh(
            self, issue_id(entry, ISSUE_VAULT_KEY_REFRESH)
        )
        # each node's insert and key layout: export, advert, a Get (`inserts.py`)
        self.inserts: NodeInserts = NodeInserts(
            self, issue_id(entry, ISSUE_INSERT_MISMATCH)
        )
        # each node's clock, zone offset and stored location as it answers them (`node_clocks.py`)
        self.clocks = NodeClocks(self, issue_id(entry, ISSUE_NODE_CLOCK_WRONG))
        # the last LINK_HISTORY links, oldest first (`LinkManager._link_ended`)
        self.link_history: deque[LinkRecord] = deque(maxlen=LINK_HISTORY)
        # link diagnostics: when each node was last heard (wall clock), the signal strength of
        # its last advertisement, when it last restarted, and the last sequence number per source
        self.last_seen: dict[int, datetime] = {}
        self.node_rssi: dict[int, int] = {}
        self.restarted: dict[int, datetime] = {}
        self._last_seq: dict[int, int] = {}
        self._node_signalled: dict[int, float] = {}
        # every group address a message heard on this run was sent to: a new device's element groups keep clear of
        # them, as of the sources heard (`heard_sources`; `onboard._reserved_groups`)
        self.heard_groups: set[int] = set()
        # load element → the pending read of its state after a transition (`_reread_after_transition`)
        self._transition_reread = self.lifecycle.keyed("transition_reread")
        # node unicast → the pending end of its Node Identity advert (`async_locate`)
        self._locating = self.lifecycle.keyed("locating")
        self._rebuilding = (
            False  # `async_begin_rebuild` ran: a reload replaces this hub
        )
        # what this hub was built from; `needs_rebuild` tells the update listener whether the entry moved away from it
        self._built_from = (hub_data(entry.data), hub_options(entry.options))
        # the `LIVE_OPTIONS` as `async_options_updated` last applied them
        self._live_options = {key: entry.options.get(key) for key in LIVE_OPTIONS}
        # the keys' gestures: clicks held back (`click_delay`, `double_click_keys`, read from the entry here), holds, repeat
        # suppression and the event listeners (`hub/gestures.py`); it ends its holds on link loss
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
        Where the address's numbers start from then is `seq_store.async_load_state`.
        """
        if not await async_migrate_legacy_seq_store(hass, entry, cdb.mesh_uuid):
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="seq_store_not_written"
            )
        await async_load_node_versions(hass, entry.entry_id)
        vault = await async_vault_keeper(hass, cdb.mesh_uuid)
        state = await async_load_state(hass, entry, cdb, unicast, vault)
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
        self.issues.clear()
        self.inserts.report_mismatch()
        self.issues.report_time_keeper()
        self.issues.report_iv_update_not_taken()
        if self.state.address_shared is not None:
            # stored by an earlier run: sends stay refused until the repair, across the restart too
            self.issues.report_address_shared()
        lifecycle = self.lifecycle
        lifecycle.set_timer(
            "adv",
            bluetooth.async_register_callback(
                self.hass,
                self.link.adv_seen,
                {"service_uuid": MESH_PROXY_SERVICE, "connectable": True},
                bluetooth.BluetoothScanningMode.PASSIVE,
            ),
        )
        lifecycle.set_task(
            "link",
            self.entry.async_create_background_task(
                self.hass, self.link.connection_loop(), f"{DOMAIN} link"
            ),
        )
        lifecycle.set_timer(
            "time",
            async_track_time_interval(
                self.hass,
                self.clock.send_time_daily,
                timedelta(seconds=TIME_SET_INTERVAL),
            ),
        )
        self.clock.arm_offset_change()
        if self.heartbeats_enabled:
            lifecycle.set_timer(
                "heartbeats",
                async_track_time_interval(
                    self.hass,
                    self.liveness.check_heartbeats,
                    timedelta(seconds=HEARTBEAT_CHECK_INTERVAL),
                ),
            )
        lifecycle.set_timer(
            "seq_check",
            async_track_time_interval(
                self.hass,
                self.issues.check_sequence_space,
                timedelta(seconds=SEQUENCE_CHECK_INTERVAL),
            ),
        )

        async def on_ha_stop(_event: Event) -> None:
            # Home Assistant does not unload entries when it stops: close the link (an ESPHome proxy's slot) and
            # store the counter as cleanly closed, so the next start needs no restart margin
            lifecycle.set_timer("ha_stop", None)
            await self.async_stop()

        lifecycle.set_timer(
            "ha_stop",
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_ha_stop),
        )

    async def async_stop(self) -> None:
        """Stop the connection loop and background work, then drop the link.

        Everything registered goes through the registry (`hub/lifecycle.py`), in its fixed order: the timers, the
        timers per node (a Node Identity advert stops by itself within 60 s), the gestures pending, then the tasks
        and the key refresh's. Not a pending retry of a failed upload: it is the entry's and outlives the reload
        most changes end with (`MeshConfigurator._upload_or_retry`); removing the entry cancels it.

        `stopping` comes first: the link is detached last, and what it still delivers while the stop waits for the
        tasks arms no new timer — the gestures take no key event (`ButtonGestures.cancel_all`), a link that ends
        starts no grace (`LinkManager._start_grace`).
        """
        self.stopping = True
        self.lifecycle.cancel_timers()
        self.lifecycle.cancel_keyed()
        self.gestures.cancel_all()
        try:
            await self.lifecycle.async_cancel_tasks()
            await async_cancel_task(self.vault_refresh.task)
            self.vault_refresh.task = None
        finally:
            # always reached, even if one of the cancels above still raised: an unclosed link keeps holding a
            # connection slot, and an unclosed counter keeps persisting into the shared store forever
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
            self.issues.clear()

    @property
    def connected(self) -> bool:
        """Whether a proxy link is up."""
        return self.proxy.connected

    # ------------------------------------------------------------------ the link (`hub/link.py`)
    @property
    def link_since(self) -> float:
        """When the current (or last) link came up, a `time.monotonic()` (`LinkManager.link_since`)."""
        return self.link.link_since

    @property
    def link_available(self) -> bool:
        """Whether the entities count as reachable: a link, or its grace (`LinkManager.link_available`)."""
        return self.link.link_available

    @property
    def link_up(self) -> bool:
        """Whether a link is up for sending: attached all the way, not only connected (`LinkManager.link_up`)."""
        return self.link.link_up

    async def async_wait_connected(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a proxy link; whether one is up (`LinkManager.async_wait_connected`)."""
        return await self.link.async_wait_connected(timeout)

    async def async_wait_refreshed(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the link's state refresh; whether it is through (`LinkManager.async_wait_refreshed`)."""
        return await self.link.async_wait_refreshed(timeout)

    def visible_proxies(self) -> list[bluetooth.BluetoothServiceInfoBleak]:
        """Proxy nodes of this network currently advertising, strongest first (`LinkManager.visible_proxies`)."""
        return self.link.visible_proxies()

    @callback
    def async_on_link_loss(self, listener: Callable[[LinkEnd], None]) -> CALLBACK_TYPE:
        """Call `listener` with every link's end; returns the unsubscribe (`LinkManager.async_on_link_loss`)."""
        return self.link.async_on_link_loss(listener)

    @property
    def metadata(self) -> Metadata:
        """The app's names for loads, gangs and scenes, as `load_network` read them with the export."""
        return self.devices.metadata

    @property
    def needs_rebuild(self) -> bool:
        """Whether the data or the options the hub was built from (`hub_data`, `hub_options`) changed since it started."""
        return self._built_from != (
            hub_data(self.entry.data),
            hub_options(self.entry.options),
        )

    @callback
    def async_options_updated(self) -> None:
        """Apply a change of `LIVE_OPTIONS` in place: the gestures read theirs as they go, the app follower is told.

        The keys' event entities write their state again (their `waits_for_double_click`): seen on air, no reload
        (`docs/on-air-sweep.md` B13).
        """
        live = {key: self.entry.options.get(key) for key in LIVE_OPTIONS}
        if live == self._live_options:
            return  # a new title, the gateway's address: nothing of the hub's
        self._live_options = live
        if self.app_follow is not None:
            self.app_follow.apply_options()
        event = self.platforms.get("event")
        for entity in () if event is None else event.entities.values():
            if entity.hass is not None:
                entity.async_write_ha_state()

    async def async_begin_rebuild(self) -> bool:
        """Prepare this hub's replacement by a reload; False when a rebuild already started.

        The update listener runs again for every entry update while the reload is pending — recording which nodes
        confirmed the heartbeat disable round is one — and must not run the round, or the reload, twice. When the
        heartbeat option went off, the nodes are told to stop first; the per-link refresh is cancelled before, and
        so is the rest of the heartbeat work (the check timer, a renewal or reprobe round in flight), so none of
        their configure Sets can interleave with the disable round and switch a node that confirmed it back on.

        Never for a stopped hub: Home Assistant's stop ends it (`async_stop`) while its entry stays loaded, and an
        options change after that found the heartbeat timer gone (the next start takes the options anyway).
        """
        if self._rebuilding or self.stopping:
            return False
        self._rebuilding = True
        if self.heartbeats_enabled and not self.entry.options.get(
            OPTION_HEARTBEATS, DEFAULT_HEARTBEATS
        ):
            self.link.cancel_refresh()
            assert (
                self.lifecycle.timer("heartbeats") is not None
            )  # armed by `async_start` with the option on
            self.lifecycle.cancel_timer("heartbeats")
            await self.lifecycle.async_cancel_task("heartbeats")
            await async_cancel_task(self.liveness.reprobe_task)
            self.liveness.reprobe_task = None
            await self.liveness.async_disable_heartbeats()
        return True

    # ------------------------------------------------------------------ following the export in place
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
        """Take over a new device model in place of a reload (`model_update`); `model_refusal` passed.

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
            and ((task := self.lifecycle.task("heartbeats")) is None or task.done())
        ):
            self.liveness.configured_at = (
                None  # a round for every node: the new ones are among them
            )
            await self.liveness.configure_heartbeats()

    # ------------------------------------------------------------------ connection loop

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
        """Have `node` advertise its Node Identity, and ask it to stop after `seconds`.

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

    def heard_sources(self) -> set[int]:
        """Every unicast source heard on air: in the replay list (kept with the counter), on this run, last seen.

        What a new device's addresses keep clear of besides the export and the vault (`onboard._heard_unicasts`).
        """
        return {*self.proxy.state.rpl, *self._last_seq, *self.last_seen}

    def _note_seq(self, src: int, seq: int) -> None:
        """Follow the source's sequence numbers; a jump into a fresh block of the counter is a restart.

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
        """Return (source, sequence number) of the source furthest into the current IV index's space, us included.

        The mesh's index (`LocalState.mesh_iv_index`): while an IV Update Home Assistant started waits for the mesh,
        the senders still use up the old one.
        """
        iv = self.proxy.state.mesh_iv_index
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
        self.link.last_rx = time.monotonic()
        self._note_seq(m.src, m.seq)
        if is_group(m.dst):
            self.heard_groups.add(m.dst)
        self.liveness.heard_from(m.src)
        if self.heartbeats_enabled:
            self.liveness.mark_alive(m.src)  # any message is as good as a heartbeat
        if self.issues.export_stale:
            self.issues.report_export_stale(
                False
            )  # our keys opened something: the export fits the mesh after all
        if m.dst == self.proxy.state.src and self.issues.pdus_dropped:
            self.issues.report_pdus_dropped(
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

        It also keeps `off_at`, the *Switches off at* sensor. A load that is on, heading off with a known remaining
        time (switched off with a transition: `on, target off, remaining`), is off then. A JUNG load running out its
        run-on time (`0x1007`) does not report the time left (sweep C6.4: the short form, then off by itself), so a
        load with a run-on time that is seen switching on — a Status on after one off, or one it published (a key,
        the app, an automation switched it) — is off that long after it (`ElementState.run_on`); a later on restarts
        it, and an answer to Home Assistant's own Get of a load already on leaves it as it was. Off clears it, and so
        does an on without a run-on time (unknown, or 0: the load stays on).
        Unverified on air: that the run-on time runs from the Status, and that a load already on restarts it.
        """
        if not p:
            return
        st = self.element_state(m.src)
        was_on = st.on
        st.on = bool(p[0])
        st.target_on = bool(p[1]) if len(p) >= 3 else st.on
        remaining = M.remaining_time(M.GEN_ONOFF_STATUS, p)
        run_on = st.run_on
        if st.on and not st.target_on and remaining:
            st.off_at = dt_util.utcnow() + timedelta(seconds=remaining)
        elif not st.on or not run_on:
            st.off_at = None
        elif was_on is False or m.dst != self.proxy.state.src:
            st.off_at = dt_util.utcnow() + timedelta(seconds=run_on)
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
            self.issues.report_time_keeper()
        node_versions_store(self.hass, self.entry.entry_id).async_delay_save(
            lambda: {
                f"{a:04X}": {k: v.hex() for k, v in items.items()}
                for a, items in nodes.items()
            },
            NODE_VERSIONS_SAVE_DELAY,
        )
        if (node := self.cdb.node_by_addr(unicast)) is not None:
            update_node_device(self.hass, self, node)

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

    # ------------------------------------------------------------------ buttons (`hub/gestures.py`)
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
        """Publish a button event on the bus (`event.publish_button_event`), for `ButtonGestures._fire`."""
        publish_button_event(self.hass, self, addr, event, attrs)

    # ------------------------------------------------------------------ repairs (`hub/issues.py`)
    async def async_rewind_iv_index(self, network: int) -> str | None:
        """Take Home Assistant's IV index back to the mesh's `network` (`Issues.async_rewind_iv_index`)."""
        return await self.issues.async_rewind_iv_index(network)

    async def async_skip_ahead(self) -> None:
        """Jump our counter ahead and start a fresh link (`Issues.async_skip_ahead`)."""
        await self.issues.async_skip_ahead()

    async def async_skip_past_shared(self) -> None:
        """Continue past the other client's numbers and send again (`Issues.async_skip_past_shared`)."""
        await self.issues.async_skip_past_shared()

    def _on_control(self, _src: int, _opcode: int) -> None:
        """Count an authenticated control PDU (a Heartbeat, a Segment Ack) as traffic for the link watchdog.

        A quiet mesh whose nodes only beat paid a keep-alive Get every LINK_IDLE_TIMEOUT: a Heartbeat the proxy
        forwarded proves the link as well as any status. Unverified on air.
        """
        self.link.last_rx = time.monotonic()

    def _on_beacon(self, beacon: SecureNetworkBeacon) -> None:
        """Account for a beacon of the proxy; one flagging a key refresh that our key cannot open raises an issue.

        Key refresh Phase 2 beacons are secured with the *new* NetKey (Mesh Profile §3.10.4), so a refresh of the
        keys the export holds shows up as an unauthenticated beacon with the Key Refresh flag — "authenticated and
        flagged" means our keys are the new ones already (the issue used to wait for exactly that,
        which never happens). On a GATT link only the proxy node talks, so it is a strong hint, not proof: the
        flag itself is not authenticated. An authenticated one brings the IV repairs up to date (`check_iv_index`,
        `check_iv_update`): the client has applied it already.
        """
        self.link.last_rx = time.monotonic()
        if not beacon.authenticated:
            self.issues.count_undecodable()
            if beacon.key_refresh:
                self.issues.report_key_refresh()
            return
        self.beacon_authenticated = True
        self.issues.check_iv_index(beacon.iv_index)
        self.issues.check_iv_update()

    def _on_undecryptable(self) -> None:
        self.issues.count_undecodable()

    @callback
    def _on_key_refresh(self, phase: int, key: NetKeyMaterial) -> None:
        """Clear the key-refresh issue: the provisioner's refresh moved on and the client followed it.

        The export keeps the old key until it is fetched again; every setup puts the followed one in its place
        (`async_apply_followed_key_refresh`). Every move — a proven Phase 1 included — takes the devices Home
        Assistant added along as far as it is proven (`vault_refresh.py`). The entry's unique id is
        the mesh UUID, which a key refresh does not change: nothing moves it.

        From the first move on, discovery recognises the new key as this mesh (`KNOWN_MESHES`); once
        the refresh completes (phase 0, reported only on proof that the mesh moved) a discovery flow it
        started before is aborted and an ignored entry holding its Network ID removed (`async_release_network_id`).
        """
        ir.async_delete_issue(
            self.hass, DOMAIN, issue_id(self.entry, ISSUE_KEY_REFRESH)
        )
        self.vault_refresh.schedule()
        if (
            known := self.hass.data.get(KNOWN_MESHES, {}).get(self.entry.entry_id)
        ) is not None:
            known.add_key(key)
        if phase == 0:
            self.hass.async_create_task(
                async_release_network_id(self.hass, self.entry, key.network_id.hex()),
                eager_start=True,
            )

    # ------------------------------------------------------------------ commands

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
        await self.link.wait_for_link()
        await self.proxy.send_access(dst, access_pdu)
        if self.lifecycle.timer("echo") is not None:
            return  # the first command of a burst is watched: anything the mesh sends answers them all
        sent_at = self.link.last_rx

        @callback
        def check(_now: datetime) -> None:
            self.lifecycle.set_timer("echo", None)
            if self.connected and self.link.last_rx == sent_at:
                _LOGGER.debug(
                    "No answer to a command within %.0f s", COMMAND_ECHO_TIMEOUT
                )
                self.link.probe_link.set()

        self.lifecycle.set_timer(
            "echo", async_call_later(self.hass, COMMAND_ECHO_TIMEOUT, check)
        )

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
        answers a state Get out to the element rather than the Set (the Set was lost, the Get's reply
        shows the old state), and the Set goes out again on its next attempt (`ProxyClient.request`). Unanswered,
        the link watchdog probes the proxy at once, and the node is marked unreachable (`Liveness.missed_answer`: `load` and
        `kind` name the load element and state Get its re-asks use, the light's for a colour-temperature element)
        only once the proxy answered the probe: a proxy that stopped forwarding leaves every command unanswered, and
        the nodes are not to blame (the link is dropped, and the next one's refresh asks them all). The TimeoutError
        is raised for the caller to report either way.

        A link that ended or changed while the command was out says nothing about the load: the
        command is sent once more, on the next link (`LinkManager.wait_for_link`) — same PDU, same TID, so a load that did
        apply it only answers. Unverified on air. Returns the status that confirmed the Set.
        """
        retry = False
        while True:
            await self.link.wait_for_link()
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
                    self.link.unanswered.append(
                        (dst if load is None else load, kind, asked)
                    )
                    self.link.probe_link.set()
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
        unknown; a newer Set replaces the pending read. Only a Lightness Set carries a transition (`set_lightness`);
        on air the DALI insert answered one with its target and remaining time and published a final
        status at the end (sweep B8, CLI only), so the read is a safeguard for a load that does not. Unverified on air
        from Home Assistant.
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

    async def set_onoff(self, addr: int, on: bool) -> None:
        """Switch the element at `addr` on or off, with transition time 0.

        Never with a transition (sweep B8): a switch insert waits it out before it switches off,
        and the DALI insert switches on at once whatever it says.
        """
        self._note_request(addr, on=on)
        await self._load_command(
            addr, M.generic_onoff_set(on, transition=0), M.GEN_ONOFF_STATUS, "switch"
        )

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
        self, group: int, on: bool, lightness: int | None = None
    ) -> None:
        """Switch every load listening to a device-type group at once, as the app's central functions do.

        Unacknowledged Sets (an acknowledged one would have every member answer at the same moment): a lightness
        first, which only the dimmable members take, then OnOff, which switches the others and leaves a dimmer that
        is already on where the lightness put it. No transition: the group's Lightness Set also switches on the
        members that are off, and an *on* takes none.
        """
        if lightness is not None:
            await self._command(
                group, M.light_lightness_set(max(1, min(65535, lightness)), ack=False)
            )
        await self._command(group, M.generic_onoff_set(on, ack=False, transition=0))

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
        self, room: int, members: Sequence[int], on: bool, lightness: int | None = None
    ) -> None:
        """Switch a room's lights or sockets as the app's area sheet does: a dim level to the room, on / off per load.

        A lightness is the app's `Dim.Group`: one Unacknowledged Light Lightness Set to the room address, taken by
        the dimmable members (the Lightness server shares the room subscription of the OnOff / Level servers it
        extends). On / off never goes to the room address, where the lights and the sockets both listen: every
        member gets an Unacknowledged OnOff Set of its own (`CommunicateWithDevice` not waiting for a status), after
        the lightness, as `central_command` orders them, and as it without a transition.
        """
        if lightness is not None:
            await self._command(
                room, M.light_lightness_set(max(1, min(65535, lightness)), ack=False)
            )
        onoff = M.generic_onoff_set(on, ack=False, transition=0)
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
        The one Set that takes a transition (`const.TRANSITION_KINDS`): a brightness change of a dimmer
        or DALI load, which the DALI insert fades and reports (sweep B8). Unverified on air from Home Assistant.
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

    async def set_ctl(self, addr: int, lightness: int, kelvin: int) -> None:
        """Set lightness (0-65535) and colour temperature (Kelvin) of the element at `addr`.

        Never with a transition: the DALI insert answers a CTL Set at once and reports no fade
        (sweep B8).
        """
        lightness = max(0, min(65535, lightness))
        self._note_request(addr, lightness=lightness, kelvin=kelvin)
        await self._load_command(
            addr, M.light_ctl_set(lightness, kelvin), M.LIGHT_CTL_STATUS, "ctl"
        )

    async def set_ctl_temperature(self, light: Light, kelvin: int) -> None:
        """Set the colour temperature (Kelvin) of a CTL light alone: Light CTL Temperature Set to its temperature element.

        A full CTL Set would have to carry a lightness, and the cached one lags behind a light that is dimming or
        was dimmed elsewhere without a status reaching us: the light jumped back to it. The temperature element
        answers with a Light CTL Temperature Status, which lands on the light (`_ctl_light_of`); the Set is noted
        on the light too, whose Light CTL Get `async_wait_settled` reads. The Set is the gateway's 7-byte form,
        transition 0 and delay 0: without them the light would fall back to its Default Transition Time. A colour
        temperature never fades.
        """
        assert light.temperature_address is not None  # the caller checks it has one
        self._note_request(light.address, kelvin=kelvin)
        await self._load_command(
            light.temperature_address,
            M.light_ctl_temperature_set(kelvin, transition=0),
            M.LIGHT_CTL_TEMP_STATUS,
            "ctl",
            load=light.address,
        )

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

        fire_scene_recalled(self.hass, self, number, self.proxy.state.src)
