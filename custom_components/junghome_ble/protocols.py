"""The hub's typed surface, as the modules it is made of see it.

`coordinator.JungHomeHub` builds its parts (`hub/`, `keep_awake`, `inserts`, `node_clocks`, `vault_refresh`, …) and
the device model (`device_info`) uses it; each of them annotated its `hub` with `JungHomeHub` itself, which made every
one of them and the hub a single import cycle for the type checker. They name one of these Protocols instead — what
they reach, not the class that has it — and `JungHomeHub` satisfies each one structurally:

- `MeshPort`: the mesh behind a hub — Home Assistant, the entry, the export and the proxy client (`keep_awake`,
  `tls`, `energy_history` and, through `HubPort`, every part);
- `HubPort`: what the hub's parts share besides (the device model, the state cache, the store, the lifecycle, the
  helpers that send in chunks or wait for the link); a part that also calls another part adds that in its own
  Protocol;
- `HubView`: what the entities' device model needs (`device_info.py`);
- `LinkView`, `InsertsView`, `ConfiguratorView`, `AppFollowView`, `GatewayPollsView`, `GatewayHost`: the few
  members of a part, or of what the setup attaches to the hub, that a module below it calls.

`TrackedPlatform`, the hub's record of a platform's entities, is here for the same reason: generic in the hub its
builder takes, it names no hub class.

Nothing here imports a module of the integration at run time; the classes named under `TYPE_CHECKING` import none of
these Protocols' users.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.components import bluetooth
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .element_state import ElementState
    from .hub.lifecycle import Lifecycle
    from .identity import VaultKeeper
    from .jhmesh.cdb import CDB, Node
    from .jhmesh.client import AccessMessage, ProxyClient
    from .jhmesh.devices import Devices
    from .seq_store import HAState


class MeshPort(Protocol):
    """The mesh behind a hub: Home Assistant, the config entry, the export (`cdb`) and the proxy client."""

    hass: HomeAssistant
    entry: ConfigEntry
    cdb: CDB
    proxy: ProxyClient

    @property
    def connected(self) -> bool:
        """Whether a proxy link is up."""


class InsertsView(Protocol):
    """What the device model asks a node's insert (`inserts.NodeInserts`)."""

    def node_model(self, node: Node) -> str:
        """Return the node's device model."""

    def buttons_model(self, node: Node) -> str:
        """Return the model of the node's buttons device."""


class HubView(Protocol):
    """What the entities' device model needs of a hub (`device_info.py`)."""

    hass: HomeAssistant
    entry: ConfigEntry
    cdb: CDB
    devices: Devices
    device_ids: dict[str, str]
    states: dict[int, ElementState]

    @property
    def inserts(self) -> InsertsView:
        """Each node's insert and key layout."""

    def node_info(self, unicast: int) -> dict[str, bytes]:
        """Return what the node at `unicast` told about itself: raw values by item name."""


class HubPort(MeshPort, HubView, Protocol):
    """What the hub's parts reach besides the mesh: the device model, the state cache, the store and the helpers."""

    devices: Devices
    state: HAState
    vault: VaultKeeper
    lifecycle: Lifecycle
    stopping: bool
    states: dict[int, ElementState]
    last_seen: dict[int, datetime]
    node_rssi: dict[int, int]
    connected_since: float | None
    proxy_address: str | None
    proxy_node: int | None
    beacon_authenticated: bool

    @property
    def last_heard(self) -> dict[int, float]:
        """When each node was last heard from, monotonic, by node unicast."""

    @property
    def link_up(self) -> bool:
        """Whether a link is up for sending: attached all the way, not only connected."""

    def element_state(self, addr: int) -> ElementState:
        """Return the cached state of the element at `addr`, creating an empty one on first contact."""

    def node_info(self, unicast: int) -> dict[str, bytes]:
        """Return what the node at `unicast` told about itself: raw values by item name."""

    def remember_node_info(self, unicast: int, name: str, raw: bytes) -> None:
        """Keep one item of what the node at `unicast` told about itself."""

    def signal_node(self, unicast: int, *, force: bool = False) -> None:
        """Tell the node's diagnostic entities."""

    def node_alive(self, address: int) -> bool:
        """Whether the node owning element `address` counts as there."""

    def load_locked(self, address: int) -> bool:
        """Whether the load at `address` last reported a lock that has not run out."""

    def highest_seq(self) -> tuple[int, int] | None:
        """Return (source, sequence number) of the source furthest into the current IV index's space."""

    def node_for_address(self, address: str) -> Node | None:
        """Return the node advertising from Bluetooth `address`."""

    def publish_button_event(
        self, addr: int, event: str, attrs: dict[str, Any]
    ) -> None:
        """Publish a button event on the bus."""

    async def chunked(self, jobs: Sequence[Callable[[], Awaitable[object]]]) -> None:
        """Run the jobs a few at a time with a short pause in between."""

    async def while_seq_stalls[T](self, send: Callable[[], Awaitable[T]]) -> T:
        """Run `send`, again while the sequence-number store holds it back, for a while."""

    async def async_refresh_element(
        self, addr: int, kind: str, *, quiet: bool = False
    ) -> None:
        """Ask one load for its state now."""

    async def async_wait_connected(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a proxy link (`link_up`); whether one is up."""

    async def async_send_time(self, destination: int = ...) -> None:
        """Broadcast Time Set now."""


class LinkView(Protocol):
    """What the hub's parts below the link ask of it (`hub.link.LinkManager`)."""

    link_refresh: float | None

    @property
    def link_since(self) -> float:
        """When the current (or last) link came up, a `time.monotonic()`."""

    @property
    def previous_link(self) -> LinkSummary:
        """How the previous link ended."""

    def ours(self, info: bluetooth.BluetoothServiceInfoBleak) -> bool:
        """Whether an advertisement is one of this mesh's proxies."""

    def set_link_state(self, state: str) -> None:
        """Move the link state sensor to `state`."""

    def refresh_through(self) -> None:
        """Note that the current link's state refresh is through."""

    async def drop_link(self, reason: str, *, penalise: bool | None) -> None:
        """End the link."""


class LinkSummary(Protocol):
    """How long a link lasted (`hub.link.LinkEnd`)."""

    @property
    def lasted(self) -> float:
        """How long the link was up, seconds."""


class GatewayHost(Protocol):
    """The gateway a call went to, as a repair names it (`gateway_api.JungHomeGatewayApi`)."""

    @property
    def host(self) -> str:
        """The gateway's host name or address."""


class ConfiguratorView(Protocol):
    """What the hub's parts and the setup ask of the entry's configurator (`mesh_config.MeshConfigurator`)."""

    async def adopt_for_unknown_nodes(self, macs: Sequence[str]) -> list[str]:
        """Adopt the gateway's export when it lists unknown nodes."""

    async def adopt_if_gateway_changed(self, *, raise_errors: bool = False) -> bool:
        """Adopt the gateway's export when it changed."""

    def report_token_rejected(self, api: GatewayHost) -> None:
        """Raise the repair for a token the gateway rejects."""

    async def async_replay_journal(self) -> bool:
        """At setup: record what an interrupted plan left on the mesh."""

    async def async_note_recorded_nodes(self) -> list[str]:
        """At setup: mark recorded the pending vault nodes the export records."""


class AppFollowView(Protocol):
    """What the hub and its button ask of the follower of the JUNG HOME app (`app_follow.AppFollower`)."""

    def note(self, m: AccessMessage) -> None:
        """Account for a received message."""

    async def async_fetch(self, *, raise_errors: bool = False) -> bool:
        """Adopt the gateway's export when it changed, and have the hub follow it; True when it was adopted."""


class GatewayPollsView(Protocol):
    """What the hub keeps up to date in the gateway's REST status polls (`gateway_status.GatewayPolls`)."""

    node: Node


class ScheduleSlots(Protocol):
    """What the clocks read of an entry's scheduler (`schedules.Scheduler`): the slots each load was read with."""

    @property
    def slots(self) -> Mapping[int, Sequence[object]]:
        """Each load's JH Scheduler slots as last read, by load element."""


@dataclass
class TrackedPlatform[H]:
    """A platform's entities as its builder made them, kept with the hub to follow a new export in place (`model_update`).

    `entities` holds every entity the builder produced, by unique id — the disabled ones too, which Home Assistant
    never adds — and `built` what each one's constructor set (its `vars()` right after it ran): what the entity took
    from the device model, as opposed to what it learnt since. `H` is the hub the builder takes.
    """

    build: Callable[[H], Iterable[Entity]]
    add: AddConfigEntryEntitiesCallback
    entities: dict[str, Entity] = field(default_factory=dict)
    built: dict[str, dict[str, Any]] = field(default_factory=dict)
