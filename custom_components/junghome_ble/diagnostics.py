"""Diagnostics: network summary, link state, key refresh, cached element states, node information and the last audit.

Keys are never included; the export paths (they describe the host), the gateway's host and API token and the
Bluetooth MAC addresses are redacted, and so are the gateway's address and any secret property should one ever sit
in the state cache (`properties.reader.redacted`). A JUNG node's UUID *is* its MAC (`jhmesh.advert.mac_from_uuid`: `30fb10ff-fe12-3456-…` is
`30:FB:10:12:34:56`), so wherever a UUID appears — the node, device identifiers, unique ids — its EUI-64 half is
replaced by the node's unicast address (`redact_node_uuids`): the document stays cross-referenced, the MAC stays out.
An OS error text names the file it failed on, whose path can carry a user name: the path is redacted, the error kept
(`redact_paths`). The location a node stores for its astro schedules is only compared with Home Assistant's home
(`node_clocks.NodeClocks.diagnostics`): the coordinates stay out.

The download works in every entry state: an entry that is retrying or failed — exactly when the
download helps — has no hub, and gets its state, why it is not loaded, what Bluetooth sees and what its export holds
instead (`_unloaded_diagnostics`).
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, TypedDict, cast

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

from .config_entities import redacted
from .config_flow import LOAD_ERRORS
from .const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_SERIAL,
    CONF_GATEWAY_TOKEN,
    CONF_METADATA_DIR,
    DOMAIN,
    ISSUE_CARRY_OVER_CONFLICT,
    NODE_INFO,
    NODE_INFO_INSERT,
    NODE_INFO_TIME_ROLE,
    NODE_INFO_UNSUPPORTED,
    NODE_INFO_VENDOR,
    OPTION_DOUBLE_CLICK_KEYS,
    issue_id,
)
from .coordinator import load_network
from .entity import (
    button_gang,
    buttons_device_id,
    mesh_identifier,
    node_identifier,
    product_name,
)
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh.advert import mac_from_uuid
from .jhmesh.client import MESH_PROXY_SERVICE, classify_proxy_advert
from .mesh_config import plan_history
from .mesh_topology import topology_snapshot
from .seq_store import iv_update_summary

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .element_state import ElementState
    from .jhmesh.cdb import CDB, Node
    from .jhmesh.devices import Blind, Light, Socket

TO_REDACT_ENTRY = {
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_TOKEN,  # the gateway hands the export (every mesh key) to whoever holds it
    CONF_GATEWAY_FINGERPRINT,  # the pinned certificate: a stable, unique id of the user's gateway
    CONF_GATEWAY_SERIAL,  # the same
}
# the keys that wait for a double click are named by unique id, whose node UUID is a MAC: an entry that is not loaded
# has no hub to mask them with (`redact_node_uuids`)
TO_REDACT_UNLOADED_OPTIONS = TO_REDACT_ENTRY | {OPTION_DOUBLE_CLICK_KEYS}
TO_REDACT_LINK = {"proxy_address", "address"}  # Bluetooth MACs of the proxy nodes
# the property of each item of a node's information (`JungHomeHub.node_info`), whose codec renders it
NODE_INFO_SPECS = (
    {name: P.SIG_PROPERTIES[pid] for pid, name in NODE_INFO.items()}
    | {name: P.PROPERTIES[pid] for pid, name in NODE_INFO_VENDOR.items()}
    | {name: P.PROPERTIES[pid] for pid, name in NODE_INFO_INSERT.items()}
)
# a UUID as a carry-over path names a node or app device (upper case, no dashes): a JUNG node's holds its MAC
_PATH_UUID = re.compile(r"[0-9A-Fa-f]{32}")
UUID_MAC_PART = (
    18  # `xxxxxxxx-xxxx-xxxx` — the EUI-64 the MAC is stuffed into, in a JUNG node UUID
)
# a file name in an error text: quoted, as `OSError` writes it (`[Errno 28] No space left on device: '/config/…'`), or
# a bare absolute path
_PATH = re.compile(r"""'[^']*'|"[^"]*"|(?<![\w.:/~])~?/[^\s'",;)]*""")
# the first byte of Mesh Proxy service data (Mesh Profile §7.2.2.2.2-3; 2 and 3 are the Mesh 1.1 private kinds)
PROXY_KINDS = {
    0: "network_id",
    1: "node_identity",
    2: "private_network_identity",  # Mesh Protocol 1.1 Proxy Privacy
    3: "private_node_identity",
}


class NetworkSummary(TypedDict):
    """The export's summary in the diagnostics (`_network`): the mesh, its public Network ID and how much it holds."""

    mesh_uuid: str
    network_id: str
    nodes: int
    groups: int
    scenes: list[int]


class LocalDiagnostics(TypedDict):
    """Our node's state in the diagnostics (`local`): address, sequence number, IV index, the store's back-pressure."""

    src: str
    seq: int
    iv_index: int
    iv_update_active: bool
    iv_update: dict[str, Any]
    stalled_for: float | None
    last_write_error: str | None
    durable_headroom: int
    address_shared: list[int] | None
    send_rates: dict[str, dict[str, Any]]


class LinkDiagnostics(TypedDict):
    """The proxy link in the diagnostics (`link`): the node, the proxies in range, the unknown nodes, the history."""

    connected: bool
    proxy_address: str | None
    proxy_node: str | None
    connected_since: float | None
    mtu: int
    proxy_config_dropped: int
    visible_proxies: list[dict[str, Any]]
    unknown_nodes: list[dict[str, Any]]
    history: list[dict[str, Any]]


class LinkStatsDiagnostics(TypedDict):
    """What the proxy links carried and dropped (`link_stats`, `jhmesh.stats.LinkStats` as dicts)."""

    current: dict[str, Any]
    total: dict[str, Any]
    links: int


class EntryDiagnostics(TypedDict):
    """The diagnostics of a loaded entry (`async_get_config_entry_diagnostics`), node UUIDs and keys redacted."""

    entry: dict[str, Any]
    options: dict[str, Any]
    network: NetworkSummary
    local: LocalDiagnostics
    link: LinkDiagnostics
    link_stats: LinkStatsDiagnostics
    heartbeats: dict[str, Any]
    topology: dict[str, Any]
    audit: dict[str, dict[str, Any]]
    plans: list[dict[str, Any]]
    carry_over_conflicts: list[str]
    key_refresh: dict[str, Any]
    added_devices: dict[str, Any]
    unreachable: list[str]
    clocks: dict[str, dict[str, Any] | None]
    issues: list[str]
    devices: dict[str, Any]
    states: dict[str, dict[str, Any]]


class UnloadedDiagnostics(TypedDict):
    """The diagnostics of an entry that is not loaded (`_unloaded_diagnostics`): no hub, what can be had without."""

    entry: dict[str, Any]
    options: dict[str, Any]
    state: str
    reason: str | None
    reason_key: str | None
    network: NetworkSummary | dict[str, str]
    bluetooth: dict[str, Any]
    issues: list[str]


def redact_paths(text: str | None) -> str | None:
    """Return `text` with every file path in it redacted: an OS error's text without the path that can name the user."""
    return None if text is None else _PATH.sub(REDACTED, text)


def _node_masks(hub: JungHomeHub) -> dict[str, str]:
    """Return the MAC half of each MAC-derived node UUID of the export, lower case, with what replaces it."""
    return {
        node.uuid[:UUID_MAC_PART].lower(): f"xxxxxxxx-xxxx-{node.unicast:04x}"
        for node in hub.cdb.nodes
        if mac_from_uuid(node.uuid) is not None
    }


def _options(hub: JungHomeHub, options: Mapping[str, Any]) -> dict[str, Any]:
    """Return a loaded entry's options for the diagnostics, double-click keys `redact_node_uuids` cannot mask redacted.

    `TO_REDACT_ENTRY` as for the data. The option names keys by unique id, a MAC-derived node UUID; one whose node has left the export since (removed
    with `remove_device` or in the app) has no mask, and would show its MAC.
    """
    out = async_redact_data(dict(options), TO_REDACT_ENTRY)
    if isinstance(keys := out.get(OPTION_DOUBLE_CLICK_KEYS), list):
        masks = _node_masks(hub)
        out[OPTION_DOUBLE_CLICK_KEYS] = [
            key
            if isinstance(key, str) and any(m in key.lower() for m in masks)
            else REDACTED
            for key in keys
        ]
    return out


def redact_node_uuids[T: Mapping[str, Any]](hub: JungHomeHub, data: T) -> T:
    """Return `data` with the MAC half of every MAC-derived node UUID replaced by `xxxxxxxx-xxxx-<unicast>`.

    Applied to the finished document, so every place a UUID reaches (the node's `uuid`, `node:<uuid>` and
    `<uuid>-<location>` identifiers, the buttons' device id) is masked the same way and stays distinct per node.
    A UUID that is no MAC (the app's own provisioner node) is left alone.
    """
    masks = _node_masks(hub)

    def mask(value: Any) -> Any:
        if isinstance(value, str):
            lowered = value.lower()
            for prefix, replacement in masks.items():
                if prefix in lowered:
                    return lowered.replace(prefix, replacement)
            return value
        if isinstance(value, dict):
            return {mask(k): mask(v) for k, v in value.items()}
        if isinstance(value, list):
            return [mask(v) for v in value]
        return value

    return cast("T", mask(data))


def _state_dict(state: ElementState) -> dict[str, Any]:
    """Render a cached element state as JSON-serialisable data, its raw values as hex.

    Property values become `{hex pid: hex value}`, SIG setup states `{hex status opcode: hex value}`. The status handler never caches a secret property, but the diagnostics redact one all the same, and
    the gateway's IP address (`0xC002`, which its sensor shows).
    """
    out = asdict(state)
    out["properties"] = {
        f"{pid:04X}": REDACTED if redacted(pid) else raw.hex()
        for pid, raw in state.properties.items()
    }
    out["setup"] = {f"{opcode:04X}": raw.hex() for opcode, raw in state.setup.items()}
    return out


def _node_info(hub: JungHomeHub, node: Node) -> dict[str, str]:
    """Render what the node told about itself, the app's node details: versions, manufacturer, time role.

    Each item is decoded with its property's codec (the time role by name); one that does not decode shows raw, in hex.
    An item the node answered without a value (`NODE_INFO_UNSUPPORTED`) shows as not supported.
    """
    out: dict[str, str] = {}
    info = hub.node_info(node.unicast)
    for name, raw in sorted(info.items()):
        if name.endswith(NODE_INFO_UNSUPPORTED):
            item = name.removesuffix(NODE_INFO_UNSUPPORTED)
            if item not in info:
                out[item] = "not supported"
            continue
        try:
            if name == NODE_INFO_TIME_ROLE:
                out[name] = M.decode_time_role_status(raw)
            else:
                out[name] = str(NODE_INFO_SPECS[name].codec.decode(raw))
        except (KeyError, ValueError):
            out[name] = raw.hex()
    return out


def _insert(hub: JungHomeHub, node: Node) -> dict[str, Any]:
    """Describe what is known of the node's insert and key layout, and from where (`inserts.py`).

    The export's cached InsertId and ButtonLayout, the node's latest JUNG advertisement (its function and layout,
    not its MAC), the function the device model was built with and the insert and layout shown now.
    """
    advert = hub.inserts.adverts.get(node.unicast)
    return {
        "export_function": node.insert_function,
        "export_layout": node.button_layout,
        "advert": None
        if advert is None
        else {
            "function": advert.actuator_function_id,
            "layout": advert.button_layout,
        },
        "built_with": node.reported_function,
        "function": hub.inserts.function(node),
        "layout": hub.inserts.layout(node),
    }


def _device_summary(hub: JungHomeHub, node: Node | None = None) -> dict[str, Any]:
    """Return the derived device model, restricted to one node when given."""
    lights = [
        light for light in hub.devices.lights if node is None or light.node is node
    ]
    sockets = [s for s in hub.devices.sockets if node is None or s.node is node]
    buttons = [b for b in hub.devices.buttons if node is None or b.node is node]
    blinds = [b for b in hub.devices.blinds if node is None or b.node is node]
    thermostats = [t for t in hub.devices.thermostats if node is None or t.node is node]
    detectors = [d for d in hub.devices.detectors if node is None or d.node is node]
    return {
        "lights": [
            {
                "address": f"{light.address:04X}",
                "name": light.name,
                "kind": light.kind,
                "rooms": light.rooms,
                "meter": f"{light.meter_address:04X}" if light.meter_address else None,
            }
            for light in lights
        ],
        "sockets": [
            {
                "address": f"{s.address:04X}",
                "name": s.name,
                "sensor": f"{s.meter_address:04X}" if s.meter_address else None,
            }
            for s in sockets
        ],
        "buttons": [
            {
                "address": f"{b.address:04X}",
                "name": b.name,
                "device": buttons_device_id(button_gang(hub, b)),
            }
            for b in buttons
        ],
        "blinds": [
            {
                "address": f"{b.address:04X}",
                "name": b.name,
                "slat": f"{b.slat_address:04X}" if b.slat_address else None,
                "rooms": b.rooms,
            }
            for b in blinds
        ],
        "thermostats": [
            {
                "address": f"{t.address:04X}",
                "name": t.name,
                "onoff": f"{t.onoff_address:04X}" if t.onoff_address else None,
                "sensor": f"{t.sensor_address:04X}" if t.sensor_address else None,
            }
            for t in thermostats
        ],
        "detectors": [
            {
                "address": f"{d.address:04X}",
                "name": d.name,
                "relay": f"{d.relay_address:04X}" if d.relay_address else None,
                "presence": d.presence,
            }
            for d in detectors
        ],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JungHomeConfigEntry
) -> EntryDiagnostics | UnloadedDiagnostics:
    """Describe the link, our node state, the export summary and the derived devices; an entry not loaded briefly."""
    if entry.state is not ConfigEntryState.LOADED:
        return await _unloaded_diagnostics(hass, entry)
    hub = entry.runtime_data
    st = hub.proxy.state
    link: LinkDiagnostics = {
        "connected": hub.connected,
        "proxy_address": hub.proxy_address,
        "proxy_node": f"{hub.proxy_node:04X}" if hub.proxy_node else None,
        "connected_since": hub.connected_since,
        "mtu": hub.proxy.mtu,
        # this link's proxy configuration PDUs dropped: a wrong header, not ours, or a replay
        "proxy_config_dropped": hub.proxy.rx_proxy_config_dropped,
        "visible_proxies": [
            {
                "address": i.address,
                "rssi": i.rssi,
                "node": f"{node.unicast:04X}" if node is not None else None,
            }
            for i in hub.visible_proxies()
            for node in (hub.node_for_address(i.address),)
        ],
        "unknown_nodes": [  # nodes of this mesh the export does not know; their MACs are redacted like the rest
            {
                "address": mac,
                "product_id": None if advert is None else advert.product_id,
            }
            for mac, advert in sorted(hub.unknown_nodes.items())
        ],
        # why the last links ended, newest first
        "history": _link_history(hub),
    }
    data: EntryDiagnostics = {
        "entry": async_redact_data(entry.data, TO_REDACT_ENTRY),
        "options": _options(hub, entry.options),
        "network": _network(hub.cdb, hub.proxy.nk.network_id),
        "local": {
            "src": f"{st.src:04X}",
            "seq": st.seq,
            "iv_index": st.iv_index,
            "iv_update_active": st.iv_update_active,
            # the last IV Update: started by Home Assistant (`start_iv_update`) or a beacon, when, whether the mesh
            # took one Home Assistant started, and when Normal Operation is due (`seq_store.iv_update_summary`)
            "iv_update": iv_update_summary(st),
            # the sequence-number store's back-pressure: how long sends have been held back (None: they are
            # not), the last write's error, and how many numbers may go out before the next hold
            "stalled_for": hub.state.stalled_for,
            "last_write_error": redact_paths(hub.state.last_write_error),
            "durable_headroom": hub.state.durable_headroom,
            # another client seen sending from our address (the open `address_shared` repair): the highest
            # [IV index, seq] it was seen with; None when none was
            "address_shared": None
            if hub.state.address_shared is None
            else list(hub.state.address_shared),
            # per address of the sequence-number store (ours live): numbers sent per day, and for a backup taken
            # now the age up to which its restore skips only the minimum and the age past which its restore would
            # send nothing (`seq_store.restore_coverage`)
            "send_rates": hub.state.send_rates(),
        },
        "link": async_redact_data(link, TO_REDACT_LINK),
        # what the proxy links carried and dropped (`jhmesh.stats.LinkStats`): the current link
        # (the last one while none is up), every link since the entry loaded, and how many links that was
        "link_stats": {
            "current": asdict(hub.proxy.link_stats),
            "total": asdict(hub.proxy.total_stats),
            "links": hub.link_count,
        },
        "heartbeats": _heartbeats(hub),
        # what the *Mesh topology* image shows: Home Assistant, its link and proxy, every node with its room,
        # features, hops, reachability and, when it does not answer, when it was last heard (`mesh_topology.py`)
        "topology": topology_snapshot(hub).as_dict(),
        # each node's last `audit_network` result (settings and findings, no keys) since the entry loaded
        "audit": {
            f"{unicast:04X}": result.as_dict()
            for unicast, result in sorted(hub.audits.items())
        },
        # the last calls that ran a plan, oldest first: action, outcome, messages accepted, step texts and the
        # error key (addresses and message names, no key material)
        "plans": list(plan_history(hass, entry.entry_id)),
        # where the last adopted app export kept its own version over Home Assistant's (the open
        # `carry_over_conflict` repair), UUIDs redacted
        "carry_over_conflicts": _carry_over_conflicts(hass, entry),
        # the followed key refresh, how far it is proven, and how far each device Home Assistant added came
        # through it (phases and Network IDs, never a key)
        "key_refresh": hub.vault_refresh.diagnostics(),
        # the devices Home Assistant added (`add_device`): recorded or pending, and what each offered for its
        # provisioning and the method used (names and flags, never a value or a key)
        "added_devices": _added_devices(hub),
        # nodes that left a full-budget request unanswered and were not heard from since
        "unreachable": [f"{unicast:04X}" for unicast in sorted(hub.unreachable)],
        # each node's clock offset, zone offset and stored location (compared with home, never shown) as it
        # last answered them (`node_clocks.py`)
        "clocks": {
            f"{unicast:04X}": hub.clocks.diagnostics(unicast)
            for unicast in sorted(hub.clocks.clocks)
        },
        "issues": _issues(hass),
        "devices": {
            **_device_summary(hub),
            "scenes": [asdict(s) for s in hub.devices.scenes],
        },
        "states": {
            f"{addr:04X}": _state_dict(state) for addr, state in hub.states.items()
        },
    }
    return redact_node_uuids(hub, data)


def _network(cdb: CDB, network_id: bytes) -> NetworkSummary:
    """Summarise the export: the mesh, the public Network ID it is known by and how much it holds."""
    return {
        "mesh_uuid": cdb.mesh_uuid,
        "network_id": network_id.hex(),
        "nodes": len(cdb.nodes),
        "groups": len(cdb.groups),
        "scenes": sorted(cdb.scenes),
    }


def _issues(hass: HomeAssistant) -> list[str]:
    """Return the ids of this integration's open repair issues (each names its entry by id, `issue_id`)."""
    return sorted(
        issue.issue_id
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.active
    )


def _added_devices(hub: JungHomeHub) -> dict[str, Any]:
    """Return each vault node by primary address: whether it is recorded and its provisioning capabilities."""
    vault = hub.vault.vault
    nodes = sorted(vault.nodes.values(), key=lambda n: n.unicast) if vault else []
    return {
        f"{n.unicast:04X}": {"recorded": n.recorded, "provisioning": n.capabilities}
        for n in nodes
    }


def _link_history(hub: JungHomeHub) -> list[dict[str, Any]]:
    """Render the hub's last links, newest first: proxy node (never its MAC), age, length, why it ended.

    `refresh` is how long the connect-time state refresh took (None: the link went first), `held_back` how long the
    sequence-number store held sends back during the link. Times in seconds, `ended_ago` counted from this download.
    """
    now = time.monotonic()
    return [
        {
            "proxy_node": None if r.proxy_node is None else f"{r.proxy_node:04X}",
            "ended_ago": round(now - r.ended),
            "lasted": round(r.lasted, 1),
            "reason": r.reason,
            "penalised": r.penalise,
            "refresh": None if r.refresh is None else round(r.refresh, 1),
            "held_back": round(r.held_back, 1),
        }
        for r in reversed(hub.link_history)
    ]


async def _unloaded_diagnostics(
    hass: HomeAssistant, entry: JungHomeConfigEntry
) -> UnloadedDiagnostics:
    """Describe an entry that is not loaded (retrying, failed, set up or unloaded): no hub, so what can be had without.

    Its state and why (any path in the reason redacted), the Mesh Proxy nodes Bluetooth sees (MACs redacted; their
    advert's kind and whether it fits the export), the export's summary when it loads, and the open repairs.
    """
    cdb: CDB | None = None
    network: NetworkSummary | dict[str, str]
    try:
        loaded, _devices = await hass.async_add_executor_job(
            load_network,
            entry.data[CONF_CDB_PATH],
            entry.data.get(CONF_METADATA_DIR) or None,
        )
    except LOAD_ERRORS as err:
        network = {"error": type(err).__name__}
    else:
        cdb = loaded
        network = _network(loaded, loaded.net_keys[0].network_id)
    return {
        "entry": async_redact_data(entry.data, TO_REDACT_ENTRY),
        "options": async_redact_data(dict(entry.options), TO_REDACT_UNLOADED_OPTIONS),
        "state": entry.state.value,
        "reason": redact_paths(entry.reason),
        "reason_key": entry.error_reason_translation_key,
        "network": network,
        "bluetooth": {
            "connectable_scanners": bluetooth.async_scanner_count(
                hass, connectable=True
            ),
            "proxies": async_redact_data(_visible_proxies(hass, cdb), TO_REDACT_LINK),
        },
        "issues": _issues(hass),
    }


def _visible_proxies(hass: HomeAssistant, cdb: CDB | None) -> list[dict[str, Any]]:
    """Every connectable node advertising Mesh Proxy service data, strongest first, checked against the export.

    `matches_export` (None without an export) says whether a Network ID is the export's, or a Node Identity
    resolves to one of its nodes (`node`), or their Mesh Protocol 1.1 private forms do — the check the setup makes
    (`config_flow.proxy_in_range`), mid key refresh with either key. Another mesh's proxies are listed too, by kind
    only.
    """
    keys = () if cdb is None else cdb.rx_net_keys(0)
    unicasts = [] if cdb is None else [n.unicast for n in cdb.nodes]
    out = []
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        sd = info.service_data.get(MESH_PROXY_SERVICE)
        if not sd:
            continue
        data = bytes(sd)
        node: int | None = None
        matches: bool | None = None
        if cdb is not None:
            verdict = classify_proxy_advert(data, keys, unicasts)
            matches = verdict is not None
            node = None if verdict is None else verdict[1]
        out.append(
            {
                "address": info.address,
                "rssi": info.rssi,
                "kind": PROXY_KINDS.get(data[0], f"type {data[0]:02X}"),
                "matches_export": matches,
                "node": None if node is None else f"{node:04X}",
            }
        )
    return sorted(out, key=lambda p: -(p["rssi"] or -127))


def _carry_over_conflicts(hass: HomeAssistant, entry: JungHomeConfigEntry) -> list[str]:
    """Return the paths of the open `carry_over_conflict` repair, every UUID in them redacted; [] without one."""
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(entry, ISSUE_CARRY_OVER_CONFLICT)
    )
    if issue is None or not issue.data:
        return []
    return [
        _PATH_UUID.sub(REDACTED, path) for path in str(issue.data["paths"]).split("\n")
    ]


def _heartbeats(hub: JungHomeHub) -> dict[str, Any]:
    """Describe the heartbeat option: whether it is on and, per node, the age of the last beat, its hops, liveness."""
    if not hub.heartbeats_enabled:
        return {"enabled": False}
    nodes: dict[str, Any] = {}
    for node in hub.heartbeat_nodes:
        beat = hub.heartbeats.get(node.unicast)
        age = hub.heartbeat_age(node)
        nodes[f"{node.unicast:04X}"] = {
            "alive": hub.node_alive(node.unicast),
            "last_beat_age": None if age is None else round(age),
            "hops": None if beat is None else beat.hops,
        }
    return {
        "enabled": True,
        "timeout": hub.heartbeat_timeout,
        "configured": hub.liveness.configured_at is not None,
        "nodes": nodes,
    }


def _node_of(hub: JungHomeHub, identifiers: set[str]) -> Node | None:
    """Return the node behind one of our device identifiers (see device_info.py).

    `node:{uuid}` is the node device itself (thermostats, detectors and the parameter entities of a node live
    there); `{uuid}-{location}` a light, socket or blind device; `{uuid}-{location}-buttons` a gang of keys.
    """
    for node in hub.cdb.nodes:
        if node_identifier(node) in identifiers:
            return node
    loads: list[Light | Socket | Blind] = [
        *hub.devices.lights,
        *hub.devices.sockets,
        *hub.devices.blinds,
    ]
    for load in loads:
        if load.unique_id in identifiers:
            return load.node
    for button in hub.devices.buttons:
        if buttons_device_id(button_gang(hub, button)) in identifiers:
            return button.node
    return None


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: JungHomeConfigEntry, device: DeviceEntry
) -> Mapping[str, Any]:
    """Describe the node behind a device page: composition, what we derived from it and the cached element states.

    Without a loaded entry there is no device model to look the node up in: the entry's own diagnostics stand in.
    """
    if entry.state is not ConfigEntryState.LOADED:
        return await _unloaded_diagnostics(hass, entry)
    hub = entry.runtime_data
    identifiers = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    if mesh_identifier(hub) in identifiers:
        return await async_get_config_entry_diagnostics(hass, entry)
    if (node := _node_of(hub, identifiers)) is None:
        return redact_node_uuids(
            hub, {"identifiers": sorted(identifiers), "node": None}
        )
    return redact_node_uuids(
        hub,
        {
            "identifiers": sorted(identifiers),
            "node": {
                "uuid": node.uuid,
                "name": node.name,
                "unicast": f"{node.unicast:04X}",
                "pid": node.pid,
                "product": product_name(node.pid),
                "elements": [
                    {
                        "address": f"{e.address:04X}",
                        "location": f"{e.location:04X}",
                        "models": e.models,
                    }
                    for e in node.elements
                ],
            },
            "node_info": _node_info(hub, node),
            "insert": _insert(hub, node),
            "clock": hub.clocks.diagnostics(node.unicast),
            "devices": _device_summary(hub, node),
            "audit": audit.as_dict()
            if (audit := hub.audits.get(node.unicast))
            else None,
            "states": {
                f"{e.address:04X}": _state_dict(hub.states[e.address])
                for e in node.elements
                if e.address in hub.states
            },
        },
    )
