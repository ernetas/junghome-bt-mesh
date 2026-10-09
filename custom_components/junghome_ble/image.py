"""Image: the *Mesh topology* picture on the mesh network device.

The mesh's shape for any dashboard, with the stock picture-entity card and no frontend resource to install: Home
Assistant, the node it is connected through, the other nodes by distance (the fewest hops of their recent heartbeats),
which relay, which are proxies, which
do not answer (`topology_svg.py` draws it, `mesh_topology.py` takes the snapshot). An SVG, rendered when the snapshot
changes and served from memory. Its words are in the server's language (`mesh_topology.topology_texts`).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, EntityCategory
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    NODE_DIAGNOSTICS_INTERVAL,
    SIGNAL_CONNECTION,
    SIGNAL_REACHABILITY,
)
from .entity import JungHomeEntity, async_setup_platform, hub_device_info
from .mesh_topology import topology_snapshot, topology_texts
from .topology_svg import render_svg

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .topology_svg import Topology

PARALLEL_UPDATES = (
    0  # nothing is read over the mesh: the picture is drawn from what the hub knows
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the *Mesh topology* image of the entry's mesh."""
    async_setup_platform(
        entry.runtime_data, "image", build_entities, async_add_entities
    )


def build_entities(hub: JungHomeHub) -> list[ImageEntity]:
    """Return the one image of the mesh network device."""
    return [JungHomeMeshTopology(hub)]


class JungHomeMeshTopology(JungHomeEntity, ImageEntity):
    """The mesh as a picture: an SVG of the topology snapshot, redrawn when the snapshot changes.

    The snapshot is taken when the link or a node's reachability changes and once every NODE_DIAGNOSTICS_INTERVAL
    (heartbeats and a sleeping node's last message arrive in between); `image_last_updated` moves only when it
    differs from the one shown, and at most once per interval — a change within it is shown when it is over — but a
    change of the link at once (`_link_changed`): the picture said "connected" through a proxy already gone for up
    to a whole interval. A heartbeat that changes nothing changes nothing: the bands are each node's fewest hops over
    the last 15 minutes (`Liveness.hop_range`), which a beat over a longer relay path leaves as they were — with the
    last beat's hops the picture moved nodes between bands every minute (on-air sweep A12). Always available: without
    a link the picture says so. Diagnostic, on by default (only configuration entities are off by default). Seen on
    air (A12): the link's proxy, the bands, the names and areas. Unverified on air: the picture in dark mode, and that
    it stays put with the fewest hops.

    Its words follow the server's language: a new one is loaded and drawn at once when it is chosen, and any look
    finding other words than those shown redraws too.
    """

    _attr_translation_key = "mesh_topology"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_content_type = "image/svg+xml"
    _shown: Topology | None = None  # the snapshot the picture is of
    _texts: Mapping[str, str] | None = None  # ... and its words
    _svg: bytes | None = None
    _drawn_at = float("-inf")  # when the picture last changed (`time.monotonic()`)
    _pending: CALLBACK_TYPE | None = None  # the look held back by the interval

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to the mesh (service) device."""
        JungHomeEntity.__init__(
            self,
            hub,
            0,
            f"{hub.cdb.mesh_uuid.lower()}-mesh-topology",
            hub_device_info(hub),
        )
        ImageEntity.__init__(self, hub.hass)

    @property
    def available(self) -> bool:
        """Always available: a mesh without a link is worth a picture too."""
        return True

    async def async_image(self) -> bytes | None:
        """Return the SVG of the snapshot shown."""
        return self._svg

    async def async_added_to_hass(self) -> None:
        """Draw the picture, then follow the link and the nodes' reachability, and look again once per interval."""
        self._draw(topology_snapshot(self.hub))
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(self.hub.entry.entry_id),
                self._link_changed,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_REACHABILITY.format(self.hub.entry.entry_id),
                self._look,
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._tick, timedelta(seconds=NODE_DIAGNOSTICS_INTERVAL)
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE,
                self._language_changed,
                event_filter=_language_in,
            )
        )
        self.async_on_remove(self._cancel_pending)

    @callback
    def async_model_rebound(self) -> None:
        """Look again: a new export may rename or add nodes."""
        self._look()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._look()

    @callback
    def _link_changed(self) -> None:
        """Redraw now, with whatever was held back: a link change is shown at once, as *Mesh overview* shows it."""
        self._cancel_pending()
        self._draw_if_changed()

    @callback
    def _look(self) -> None:
        """Redraw when the snapshot changed: now, or when the interval since the last change is over (once)."""
        if self._pending is not None:
            return
        wait = self._drawn_at + NODE_DIAGNOSTICS_INTERVAL - time.monotonic()
        if wait > 0:
            self._pending = async_call_later(self.hass, wait, self._held_back)
            return
        self._draw_if_changed()

    async def _language_changed(self, _event: Event[Any]) -> None:
        """Load the server's new language (Home Assistant may not have yet) and draw its words now."""
        await async_get_translations(
            self.hass, self.hass.config.language, "common", {DOMAIN}
        )
        self._draw_if_changed()

    @callback
    def _draw_if_changed(self) -> None:
        snapshot = topology_snapshot(self.hub)
        if snapshot != self._shown or topology_texts(self.hass) != self._texts:
            self._draw(snapshot)
            self.async_write_ha_state()

    @callback
    def _held_back(self, _now: datetime) -> None:
        self._pending = None
        self._look()

    def _draw(self, snapshot: Topology) -> None:
        self._shown = snapshot
        self._texts = topology_texts(self.hass)
        self._svg = render_svg(snapshot, self._texts).encode()
        self._drawn_at = time.monotonic()
        self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending()
            self._pending = None


@callback
def _language_in(data: Mapping[str, Any]) -> bool:
    """Whether a core configuration update sets the language."""
    return "language" in data
