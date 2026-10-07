"""The topology snapshot of a hub (`topology_svg.Topology`): what the *Mesh topology* image and the diagnostics show.

Read off the hub as it stands: each node's device name (a rename too) and area (`device_info.node_label`,
`node_areas`, as the *Mesh overview* shows them), its features from the export (`features`: 1 is enabled; 0
disabled and 2 unsupported are not shown), the hops of its last heartbeat, whether it answers (`JungHomeHub.
node_alive`; None for a battery node, which sleeps, and for every node without a link), and when it was last heard
for one that does not answer. Equal snapshots are equal values, so the image can tell a change from a heartbeat that
changed nothing. Unverified on air: the picture against the installation (the link's proxy, the nodes' hop counts).

The picture's words (`topology_texts`) are the translations of the server's language, `common.topology_<key>` for
each key of `topology_svg.TEXTS`, English where it has none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .device_info import node_areas, node_label
from .jhmesh.devices import BATTERY_PIDS
from .texts import cached_texts
from .topology_svg import TEXTS, Topology, TopologyNode

if TYPE_CHECKING:
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


def topology_snapshot(hub: JungHomeHub) -> Topology:
    """Return the hub's topology now: Home Assistant, its link and proxy, every provisioned node sorted by address."""
    registry = dr.async_get(hub.hass)
    areas = node_areas(hub)
    link = hub.link_available
    nodes: list[TopologyNode] = []
    for node in sorted(hub.cdb.nodes, key=lambda n: n.unicast):
        if node.pid is None:
            continue  # a phone, another company's node: no device of ours
        battery = node.pid in BATTERY_PIDS
        reachable = None if battery or not link else hub.node_alive(node.unicast)
        beat = hub.heartbeats.get(node.unicast)
        seen = hub.last_seen.get(node.unicast)
        nodes.append(
            TopologyNode(
                unicast=node.unicast,
                name=node_label(hub, registry, node),
                room=areas.get(node.unicast),
                relay=_feature(node, "relay"),
                proxy=_feature(node, "proxy"),
                friend=_feature(node, "friend"),
                low_power=_feature(node, "lowPower"),
                battery=battery,
                hops=None if beat is None else beat.hops,
                reachable=reachable,
                last_heard=None
                if seen is None or reachable
                else dt_util.as_local(seen).strftime("%Y-%m-%d %H:%M"),
            )
        )
    return Topology(
        address=hub.proxy.state.src,
        connected=link,
        proxy=hub.proxy_node if link else None,
        nodes=tuple(nodes),
        heartbeats=hub.heartbeats_enabled,
    )


def topology_texts(hass: HomeAssistant) -> dict[str, str]:
    """Return the picture's words in the server's language, English where it has none (a `TEXTS` key each)."""
    cached = cached_texts(hass, "common", "topology_")
    return {key: cached.get(key, english) for key, english in TEXTS.items()}
