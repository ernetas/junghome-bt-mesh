"""What the nodes told about themselves, kept across reloads and restarts (`NODE_VERSIONS` and its store).

The hub fills `NODE_VERSIONS` from the nodes' answers and schedules a write of the entry's store
(`node_versions_store`); setup loads it back (`async_load_node_versions`) before any Get can answer. Nothing here
depends on the hub.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, NODE_INFO, SIG_SOFTWARE_VERSION

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


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
