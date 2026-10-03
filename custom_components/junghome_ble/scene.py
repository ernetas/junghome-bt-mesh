"""Scenes stored in the mesh (recalled with one broadcast, as the app does).

A scene entity carries what the export knows (`scene_number`, the member loads) and, once the hub has asked the
members after a connection, what each of them does when the scene is recalled (`members`: load name → "switch on",
"lightness 100% 2000K", …, read from the JUNG Scene Action Setup servers; loads sharing a name are told apart
by their mesh address). Scenes are edited with the `store_scene` / `remove_from_scene` / `create_scene` /
`rename_scene` / `delete_scene` actions (`services.py`).

The scenes the app makes for its SIG timers (`TimerScene …`, `devices.SceneDef.timer`) are left out, as the app's
scene list leaves them out. A recall heard on the mesh — a key's, the app's, the gateway's, or one only the members'
Scene Status told of — counts as an activation, as HA's own does (the entity's state is the last one), and
`active_members` names the members whose Scene Server reports the scene as its current one.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from homeassistant.components.scene import Scene
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_SCENE_RECALLED, SIGNAL_SCENES
from .entity import JungHomeEntity, async_setup_platform

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import JungHomeConfigEntry
    from .coordinator import JungHomeHub
    from .jhmesh.devices import SceneDef

PARALLEL_UPDATES = 0  # push-based


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of `build_entities`; drop the entities an earlier version made for timer scenes.

    The entities are kept with the hub to follow a new export in place (`model_update`).
    """
    hub = entry.runtime_data
    registry = er.async_get(hass)
    for scene in hub.devices.scenes:
        if scene.timer and (
            entity_id := registry.async_get_entity_id(
                "scene", DOMAIN, scene_unique_id(hub, scene.number)
            )
        ):
            registry.async_remove(entity_id)
    async_setup_platform(hub, "scene", build_entities, add_entities)


def build_entities(hub: JungHomeHub) -> list[JungHomeScene]:
    """Return one scene entity per scene the app shows."""
    return [
        JungHomeScene(hub, scene) for scene in hub.devices.scenes if not scene.timer
    ]


def scene_unique_id(hub: JungHomeHub, number: int) -> str:
    """Return the unique id of scene `number`'s entity (the scene event links the entity by it, `event.py`)."""
    return f"{hub.cdb.mesh_uuid.lower()}-scene-{number}"


class JungHomeScene(JungHomeEntity, Scene):
    """Scenes are network-wide, so they are not attached to a device; the app's scene name is the entity name."""

    def __init__(self, hub: JungHomeHub, scene: SceneDef) -> None:
        """Bind to `scene`; the entity name is the app's scene name."""
        super().__init__(hub, 0, scene_unique_id(hub, scene.number), None)
        self.scene = scene
        self._attr_name = scene.name

    async def async_added_to_hass(self) -> None:
        """Also follow the hub's re-reads of the members' scene actions and current scenes, and the mesh's recalls."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SCENES.format(self.hub.entry.entry_id),
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SCENE_RECALLED.format(self.hub.entry.entry_id),
                self._on_recalled,
            )
        )

    @callback
    def _on_recalled(self, number: int, source: int | None) -> None:
        """Record a recall of this scene that did not come from this entity (its own activation records itself)."""
        if number != self.scene.number or source == self.hub.proxy.state.src:
            return
        self._async_record_activation()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """`scene_number`, `members` and `active_members` of the scene.

        `members`: what each stored load does in this scene ("stored" until it was read); `active_members`: the
        members whose register reports this scene as its current one.
        """
        actions = self.hub.scene_actions.get(self.scene.number, {})
        # the export lists the element the scene was stored on (a two-channel node's primary, for either channel):
        # once read, the member is each channel of that node holding an action for the scene, as in the app
        addresses: dict[int, None] = {}
        current: set[int] = (
            set()
        )  # the member addresses whose register has this scene current
        for stored_on in self.hub.cdb.scenes.get(self.scene.number, []):
            acting = [
                channel
                for channel in self.hub.scene_action_channels(stored_on)
                if actions.get(channel) is not None
            ]
            addresses.update(dict.fromkeys(acting or [stored_on]))
            state = self.hub.states.get(stored_on)
            if state is not None and state.scene == self.scene.number:
                current.update(acting or [stored_on])
        rows: list[tuple[str, int, str]] = []
        for address in addresses:
            device = self.hub.devices.by_address.get(address)
            name = device.name if device is not None else f"{address:04X}"
            action = actions.get(address)
            rows.append((name, address, action.describe() if action else "stored"))
        names = Counter(name for name, _, _ in rows)
        # keyed by name alone, two loads of one name would collapse into one member
        members = {
            (name if names[name] == 1 else f"{name} ({address:04X})"): text
            for name, address, text in rows
        }
        return {
            "scene_number": self.scene.number,
            "members": members,
            "active_members": [
                (name if names[name] == 1 else f"{name} ({address:04X})")
                for name, address, _ in rows
                if address in current
            ],
        }

    async def async_activate(self, **kwargs: Any) -> None:
        """Recall the scene on every node."""
        try:
            await self.hub.recall_scene(self.scene.number)
        except (ConnectionError, OSError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err
