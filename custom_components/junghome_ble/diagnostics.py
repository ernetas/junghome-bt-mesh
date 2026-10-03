"""Diagnostics: network summary, link state, key refresh, cached element states, node information and the last audit.

Keys are never included; the export paths (they describe the host), the gateway's host and API token and the
Bluetooth MAC addresses are redacted, and so are the gateway's address and any secret property should one ever sit
in the state cache (`config_entities.redacted`). A JUNG node's UUID *is* its MAC (`jhmesh.advert.mac_from_uuid`: `30fb10ff-fe12-3456-…` is
`30:FB:10:12:34:56`), so wherever a UUID appears — the node, device identifiers, unique ids — its EUI-64 half is
replaced by the node's unicast address (`redact_node_uuids`): the document stays cross-referenced, the MAC stays out.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.helpers import issue_registry as ir

from .config_entities import redacted
from .const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_TOKEN,
    CONF_METADATA_DIR,
    DOMAIN,
    ISSUE_CARRY_OVER_CONFLICT,
    NODE_INFO,
    NODE_INFO_TIME_ROLE,
    NODE_INFO_UNSUPPORTED,
    NODE_INFO_VENDOR,
)
from .coordinator import issue_id
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

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

    from . import JungHomeConfigEntry
    from .coordinator import ElementState, JungHomeHub
    from .jhmesh.cdb import Node
    from .jhmesh.devices import Blind, Light, Socket

TO_REDACT_ENTRY = {
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_TOKEN,  # the gateway hands the export (every mesh key) to whoever holds it
    CONF_GATEWAY_FINGERPRINT,  # the pinned certificate: a stable, unique id of the user's gateway
}
TO_REDACT_LINK = {"proxy_address", "address"}  # Bluetooth MACs of the proxy nodes
# the property of each item of a node's information (`JungHomeHub.node_info`), whose codec renders it
NODE_INFO_SPECS = {name: P.SIG_PROPERTIES[pid] for pid, name in NODE_INFO.items()} | {
    name: P.PROPERTIES[pid] for pid, name in NODE_INFO_VENDOR.items()
}
# a UUID as a carry-over path names a node or app device (upper case, no dashes): a JUNG node's holds its MAC
_PATH_UUID = re.compile(r"[0-9A-Fa-f]{32}")
UUID_MAC_PART = (
    18  # `xxxxxxxx-xxxx-xxxx` — the EUI-64 the MAC is stuffed into, in a JUNG node UUID
)


def redact_node_uuids(hub: JungHomeHub, data: dict[str, Any]) -> dict[str, Any]:
    """Return `data` with the MAC half of every MAC-derived node UUID replaced by `xxxxxxxx-xxxx-<unicast>`.

    Applied to the finished document, so every place a UUID reaches (the node's `uuid`, `node:<uuid>` and
    `<uuid>-<location>` identifiers, the buttons' device id) is masked the same way and stays distinct per node.
    A UUID that is no MAC (the app's own provisioner node) is left alone.
    """
    masks = {
        node.uuid[:UUID_MAC_PART].lower(): f"xxxxxxxx-xxxx-{node.unicast:04x}"
        for node in hub.cdb.nodes
        if mac_from_uuid(node.uuid) is not None
    }

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

    return cast("dict[str, Any]", mask(data))


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
) -> dict[str, Any]:
    """Describe the link, our node state, the export summary and the derived devices."""
    hub = entry.runtime_data
    st = hub.proxy.state
    link = {
        "connected": hub.connected,
        "proxy_address": hub.proxy_address,
        "proxy_node": f"{hub.proxy_node:04X}" if hub.proxy_node else None,
        "connected_since": hub.connected_since,
        "mtu": hub.proxy.mtu,
        # this link's proxy configuration PDUs dropped: a wrong header, not ours, or a replay (review-4 P4-7)
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
    }
    return redact_node_uuids(
        hub,
        {
            "entry": async_redact_data(entry.data, TO_REDACT_ENTRY),
            "network": {
                "mesh_uuid": hub.cdb.mesh_uuid,
                "network_id": hub.proxy.nk.network_id.hex(),
                "nodes": len(hub.cdb.nodes),
                "groups": len(hub.cdb.groups),
                "scenes": sorted(hub.cdb.scenes),
            },
            "local": {
                "src": f"{st.src:04X}",
                "seq": st.seq,
                "iv_index": st.iv_index,
                "iv_update_active": st.iv_update_active,
                # the sequence-number store's back-pressure: how long sends have been held back (None: they are
                # not), the last write's error, and how many numbers may go out before the next hold
                "stalled_for": hub.state.stalled_for,
                "last_write_error": hub.state.last_write_error,
                "durable_headroom": hub.state.durable_headroom,
                # another client seen sending from our address (the open `address_shared` repair): the highest
                # [IV index, seq] it was seen with; None when none was
                "address_shared": None
                if hub.state.address_shared is None
                else list(hub.state.address_shared),
            },
            "link": async_redact_data(link, TO_REDACT_LINK),
            "heartbeats": _heartbeats(hub),
            # each node's last `audit_network` result (settings and findings, no keys) since the entry loaded
            "audit": {
                f"{unicast:04X}": result.as_dict()
                for unicast, result in sorted(hub.audits.items())
            },
            # where the last adopted app export kept its own version over Home Assistant's (the open
            # `carry_over_conflict` repair), UUIDs redacted
            "carry_over_conflicts": _carry_over_conflicts(hass, entry),
            # the followed key refresh, how far it is proven, and how far each device Home Assistant added came
            # through it (review-4 D11: phases and Network IDs, never a key)
            "key_refresh": hub.vault_refresh.diagnostics(),
            # nodes that left a full-budget request unanswered and were not heard from since
            "unreachable": [f"{unicast:04X}" for unicast in sorted(hub.unreachable)],
            "devices": {
                **_device_summary(hub),
                "scenes": [asdict(s) for s in hub.devices.scenes],
            },
            "states": {
                f"{addr:04X}": _state_dict(state) for addr, state in hub.states.items()
            },
        },
    )


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
        "configured": hub._heartbeats_configured_at is not None,  # noqa: SLF001  # the hub's own diagnostics
        "nodes": nodes,
    }


def _node_of(hub: JungHomeHub, identifiers: set[str]) -> Node | None:
    """Return the node behind one of our device identifiers (see entity.py).

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
) -> dict[str, Any]:
    """Describe the node behind a device page: composition, what we derived from it and the cached element states."""
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
