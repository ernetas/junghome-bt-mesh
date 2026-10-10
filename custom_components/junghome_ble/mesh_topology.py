"""The topology snapshot of a hub (`topology_svg.Topology`): what the *Mesh topology* image and the diagnostics show.

Its rows come from `node_rows`, the one reading of the hub's nodes the *Mesh overview* sensor's rows come from too.
Read off the hub as it stands: each node's device name (a rename too) and area (`device_info.node_label`,
`node_areas`, as the *Mesh overview* shows them), its features from the export (`features`: 1 is enabled; 0
disabled and 2 unsupported are not shown), its distance (the fewest hops of its heartbeats over the last 15 minutes,
`JungHomeHub.node_hops`: one beat's hops jump about with the relay path that delivered it first), whether it
answers (`JungHomeHub.node_alive`; None for a battery node, which sleeps, and for every node without a link), and when
it was last heard for one that does not answer. Equal snapshots are equal values, so the image can tell a change from
a heartbeat that changed nothing. Seen on air (sweep A12): the link's proxy, the bands against the heartbeats, names
and areas, and the fewest hops staying put on the installation.

The picture's words (`topology_texts`) are the translations of the server's language, `common.topology_<key>` for
each key of `topology_svg.TEXTS`, English where it has none.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .device_info import node_areas, node_label
from .jhmesh.devices import BATTERY_PIDS
from .texts import cached_texts
from .topology_svg import TEXTS, Topology, TopologyNode

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from .coordinator import JungHomeHub
    from .jhmesh.cdb import Node

FEATURE_ENABLED = (
    1  # Mesh CDB `features` values: 0 disabled, 1 enabled, 2 not supported
)


def _feature(node: Node, name: str) -> bool:
    """Whether the export records the feature `name` (`relay`, `proxy`, `friend`, `lowPower`) as enabled."""
    features = node.raw.get("features")
    return isinstance(features, dict) and features.get(name) == FEATURE_ENABLED


@dataclass(frozen=True)
class NodeRow:
    """One provisioned node as the hub has it now: what *Mesh overview* and *Mesh topology* show of it.

    `reachable` is `JungHomeHub.node_alive`, None for a battery node (it sleeps) and for every node without a link;
    `proxy` whether the link goes through it.
    """

    node: Node
    name: str
    area: str | None
    battery: bool
    reachable: bool | None
    hops: int | None
    last_seen: datetime | None
    proxy: bool


def node_rows(hub: JungHomeHub) -> list[NodeRow]:
    """Return a row per provisioned node of the export (a phone, another company's node: none), in export order.

    Name and area as the node's device shows them (`device_info.node_label`, `node_areas`), its distance (the fewest
    hops of its recent heartbeats, `JungHomeHub.node_hops`) and when it was last heard.
    """
    registry = dr.async_get(hub.hass)
    areas = node_areas(hub)
    link = hub.link_available
    rows: list[NodeRow] = []
    for node in hub.cdb.nodes:
        if node.pid is None:
            continue  # a phone, another company's node: no device of ours
        battery = node.pid in BATTERY_PIDS
        rows.append(
            NodeRow(
                node=node,
                name=node_label(hub, registry, node),
                area=areas.get(node.unicast),
                battery=battery,
                reachable=None if battery or not link else hub.node_alive(node.unicast),
                hops=hub.node_hops(node.unicast),
                last_seen=hub.last_seen.get(node.unicast),
                proxy=link and node.unicast == hub.proxy_node,
            )
        )
    return rows


def topology_snapshot(hub: JungHomeHub) -> Topology:
    """Return the hub's topology now: Home Assistant, its link and proxy, every provisioned node sorted by address."""
    link = hub.link_available
    nodes = tuple(
        TopologyNode(
            unicast=row.node.unicast,
            name=row.name,
            room=row.area,
            relay=_feature(row.node, "relay"),
            proxy=_feature(row.node, "proxy"),
            friend=_feature(row.node, "friend"),
            low_power=_feature(row.node, "lowPower"),
            battery=row.battery,
            hops=row.hops,
            reachable=row.reachable,
            last_heard=None
            if row.last_seen is None or row.reachable
            else dt_util.as_local(row.last_seen).strftime("%Y-%m-%d %H:%M"),
        )
        for row in sorted(node_rows(hub), key=lambda r: r.node.unicast)
    )
    return Topology(
        address=hub.proxy.state.src,
        connected=link,
        proxy=hub.proxy_node if link else None,
        nodes=nodes,
        heartbeats=hub.heartbeats_enabled,
    )


def topology_texts(hass: HomeAssistant) -> dict[str, str]:
    """Return the picture's words in the server's language, English where it has none (a `TEXTS` key each)."""
    cached = cached_texts(hass, "common", "topology_")
    return {key: cached.get(key, english) for key, english in TEXTS.items()}
