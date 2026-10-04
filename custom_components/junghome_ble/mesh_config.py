"""Rooms and key connections: the app's Config-message sequences on the mesh, then the project-file write-back.

Roadmap step 3, item 11 — the first features that use the device-key transport (`ProxyClient.request_config`)
and the project-file writer (`export.ProjectFile`). Every operation of `MeshConfigurator` runs the same pipeline,
one at a time per hub:

1. load the export the config entry points at, fresh from disk (so the newer-export guard judges the truth); an
   entry set up from a gateway first asks the gateway for its export and adopts it when the app changed the
   installation since (the app uploads after every change), so the plan is made — and later uploaded — on top
   of the app's changes, never over them;
2. mutate it through the `ProjectFile` mutators, collecting `ModelChange`s = the Config messages to send;
   a plan that removes or replaces what the export says a node holds first reads that from the nodes and compares
   (`PlanExecutor.preflight`): a node that differs — the app changed it since the export, a reset, a stopped plan —
   stops the plan before its first message, unless the action's `force` skips the comparison;
3. send them with the node's device key and check every Config status (`ConfigStatus.ok`, echoing the step's
   element / model / address so a late duplicate cannot pass for the next step's answer), additive messages
   before destructive ones so a stop leaves the old wiring working. The first refusal or silence stops the plan
   *apply-and-record*: the steps accepted before it are what the mesh now holds, so they are replayed into a
   fresh copy of the export and that copy is written (and handed to the gateway) before the error is raised —
   the file never disagrees with the nodes, and the error says what was applied and what was not. A plan
   cancelled from outside (an automation restarted, a script turned off, Home Assistant stopping) is
   recorded the same way before the cancellation goes on, and a write once started runs to its end
   (`run_to_end`); one a crash cuts off is in the plan journal (`plan_journal`), which the next setup records
   (`async_replay_journal`). Every message is idempotent (Add / Delete / Publication Set / App Bind), so running the
   action again with the same target completes it;
4. for key connections, write the KeyMode property (`0x5003`, LBC Admin Property Set) and read it back;
5. save the file atomically (`NewerExportError` becomes a translated error asking for a fresh export);
6. when the entry knows a gateway (host, token, pinned certificate), hand the export to it the way the app does
   after every change (`POST config {"data": {"project_file": …}}`, roadmap step 14) so the gateway sees the same
   installation (the app never downloads it: its next upload lacks HA's changes, which `ExportStore._carry_over` puts
   back before that upload is adopted); a failed upload raises the `gateway_sync_failed` repair issue
   and is retried like the app retries it (twice, 15 s apart, `GATEWAY_UPLOAD_RETRIES`), and `sync_gateway()`
   retries it on demand — each time after checking, by content digest against what HA last synced, that the
   gateway does not hold a change of its own meanwhile. The digest and the time of the last successful upload are
   kept in the entry's `GatewaySync` record (the time is the app's `gateway_last_sync`).

A dry run (`MeshConfigurator.dry_run`) goes through step 2 on the export on disk only, its pre-flight reads
included, and ends where step 3 or 5 would begin, answering the plan's messages, how the export would change and
what the reads found; nothing else is sent, and nothing is written or adopted. What a call's plans did is counted
per call (`PlanOutcome`) for its answer, its error and the logbook.

The caller (`actions.common._run`) then has the running hub take the new export over in place (`model_update`),
which is how the hub's device model — and with it the entities, their `rooms` attributes and the buttons'
devices — follows it; a change it cannot follow in place reloads the config entry, as every change did before.

The message sets mirror the JUNG HOME app (`docs/android/network-logic.md` §1.5, §2.3, §2.4;
`docs/gap-analysis/network-features.md` §1, §2, §8.3): room membership is a `Model Subscription Add` of the
load's OnOff / Level servers to the room address (plus the publish groups of the keys already linked to the
room); a key connection is `Reset KeySetPropertyMode` (AppKey), then every publication and subscription of the
key element cleared (`Publication Set 0x0000` / `Subscription Delete`, never *Delete All*), then the key mode's
client models wired to the target's element group (`Publication Set` + `Subscription Add`) — a room link wires
the room's loads to the key's own element group instead — then KeyMode. A scene link (`SetSceneConnection`,
network-logic.md §2.5) publishes the key's Scene Client to all nodes (`0xFFFF`, publish only), writes the scene
into the key (KeyModeSceneConfig `0x5002`) and records the app's `keyModeSceneConfigExports` row; it has no
KeySetPropertyMode reset, as in the app. Clearing a key ("no function") is the
clear step alone: the app leaves KeyMode as it is, and so do we. The app's *order* is not kept on air: the new
wiring goes out first and the old one is cleared last (minus what the new wiring re-adds), and the
KeySetPropertyMode reset goes out only once every Config step was accepted — a refused step 1 leaves a key that
was in property mode exactly as it was. A metering socket's thresholds (`set_threshold_devices`) are wired like a
room link from the socket's side: its OnOff Client (on the meter element) subscribes to — and publishes to — that
element's own group, and the loads it should switch subscribe their JUNG User Property and OnOff servers there;
`unwire_threshold` undoes it the app's way, with a publication reset sent in the app's order
(`PlanExecutor.send(as_planned=True)`).

No key material is logged or put into error messages.

The code is split into the `configurator` package: `plan` (the texts of what a stop applied; the
plan model — `ConfigStep`, `ordered`, `replay` — is the library's `jhmesh.plan`) and `wiring` (the modes and models, the wiring read from an export and the
planners) are pure — a `ProjectFile` in, steps out, a `PlanError` for a refusal —; `store` reads and writes the export
and the gateway's copy (`ExportStore`), `executor` sends the plans and records a stop (`PlanExecutor`), and `rooms`,
`scenes`, `thresholds` and `nodes` hold the operations. `MeshConfigurator` here is the facade every caller uses: each
operation delegates, and a planner's `PlanError` becomes the translated service error it names.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Collection, Coroutine, Iterable, Sequence
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

from .configurator.executor import PlanExecutor
from .configurator.nodes import Nodes
from .configurator.plan import (
    APPLIED_KEY_WIRED,
    APPLIED_LOCK_WIRED,
    APPLIED_NOTHING,
    APPLIED_SCENE_WIRED,
    applied_members,
    applied_removed,
    applied_scene_cleared,
    applied_scene_members,
    applied_scene_stored,
    applied_text,
    applied_unused_deleted,
)
from .configurator.rooms import Keys, Rooms
from .configurator.scenes import Scenes, held_scenes, scene_action_for
from .configurator.store import (
    GATEWAY_SYNCS,
    RELOAD_POLL,
    UPLOAD_RETRIES,
    ExportStore,
    GatewaySync,
    PlanOutcome,
    async_remove_gateway_sync,
    cancel_upload_retry,
    gateway_sync,
    plan_history,
    plan_journal,
    run_to_end,
    token_rejected_open,
    translated,
)
from .configurator.thresholds import Thresholds
from .configurator.wiring import (
    LOCK_SECONDS_MAX,
    MODES,
    SENSOR_SERVER,
    TARGET_ELEMENTS,
    app_copy_path,
    export_digest,
    find_scene,
    held,
    pre_adopt_path,
    sensor_elements,
    sensor_publication,
    shown,
    threshold_client,
    threshold_devices,
)
from .configurator.wiring import derive_mode as plan_mode
from .const import DEFAULT_UNUSED_SCENES_DRY_RUN
from .jhmesh.plan import ConfigStep, ordered, replay

if TYPE_CHECKING:
    import asyncio

    from .coordinator import JungHomeHub
    from .gateway_api import JungHomeGatewayApi
    from .jhmesh import vendor_models as V
    from .jhmesh.audit import NodeAudit
    from .jhmesh.cdb import Element, Node
    from .jhmesh.commission import Plan
    from .jhmesh.export import ProjectFile
    from .jhmesh.onboarding import DeviceCount
    from .jhmesh.vault import Ranges
    from .protocols import GatewayHost

__all__ = [
    "APPLIED_KEY_WIRED",
    "APPLIED_LOCK_WIRED",
    "APPLIED_NOTHING",
    "APPLIED_SCENE_WIRED",
    "GATEWAY_SYNCS",
    "LOCK_SECONDS_MAX",
    "MODES",
    "RELOAD_POLL",
    "SENSOR_SERVER",
    "TARGET_ELEMENTS",
    "UPLOAD_RETRIES",
    "ConfigStep",
    "GatewaySync",
    "MeshConfigurator",
    "PlanOutcome",
    "app_copy_path",
    "applied_members",
    "applied_removed",
    "applied_scene_cleared",
    "applied_scene_members",
    "applied_scene_stored",
    "applied_text",
    "applied_unused_deleted",
    "async_remove_gateway_sync",
    "cancel_upload_retry",
    "derive_mode",
    "export_digest",
    "gateway_sync",
    "held",
    "held_scenes",
    "ordered",
    "plan_history",
    "plan_journal",
    "pre_adopt_path",
    "replay",
    "run_to_end",
    "scene_action_for",
    "sensor_elements",
    "sensor_publication",
    "shown",
    "threshold_client",
    "threshold_devices",
    "token_rejected_open",
]


def derive_mode(element: Element) -> str:
    """Key mode the app would pick for a target element (`wiring.derive_mode`); a translated error when it has none."""
    with translated():
        return plan_mode(element)


def _translated[**P, R](
    operation: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Run an operation with a planner's refusal (`PlanError`) raised as the translated service error it names."""

    @functools.wraps(operation)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with translated():
            return await operation(*args, **kwargs)

    return run


class MeshConfigurator:
    """Rooms, key connections and scenes of one hub; operations are serialised by `lock`.

    The mutating coroutines return True when the device model changed, which is the caller's cue to have the hub
    follow the export (an adopted gateway export changes it too, see `adopted`); `create_room` touches only the
    file's bookkeeping and returns the new room's address instead. Every operation delegates to the part of the
    `configurator` package that holds it.
    """

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to `hub`; nothing is loaded until an operation runs."""
        self.store = ExportStore(hub)
        self.executor = PlanExecutor(self.store)
        self.rooms = Rooms(self.store, self.executor)
        self.keys = Keys(self.store, self.executor)
        self.scenes = Scenes(self.store, self.executor)
        self.thresholds = Thresholds(self.store, self.executor)
        self.nodes = Nodes(self.store, self.executor)
        hub.configurator = self  # the hub's unknown-node refresh adopts the gateway's export through us

    # ------------------------------------------------------------------ the store's and the executor's state
    @property
    def hub(self) -> JungHomeHub:
        """The hub this configurator configures."""
        return self.store.hub

    @hub.setter
    def hub(self, hub: JungHomeHub) -> None:
        self.store.hub = hub

    @property
    def path(self) -> str:
        """The export the config entry points at."""
        return self.store.path

    @property
    def lock(self) -> asyncio.Lock:
        """The lock every operation holds (`ExportStore.lock`)."""
        return self.store.lock

    @lock.setter
    def lock(self, lock: asyncio.Lock) -> None:
        self.store.lock = lock

    @property
    def recorded(self) -> bool:
        """Whether the running call wrote the export (`ExportStore.recorded`)."""
        return self.store.recorded

    @recorded.setter
    def recorded(self, recorded: bool) -> None:
        self.store.recorded = recorded

    @property
    def adopted(self) -> bool:
        """Whether the running call adopted the gateway's export first (`ExportStore.adopted`)."""
        return self.store.adopted

    @adopted.setter
    def adopted(self, adopted: bool) -> None:
        self.store.adopted = adopted

    @property
    def journaled(self) -> bool:
        """Whether the plan journal holds a plan whose outcome the export does not record yet."""
        return self.store.journaled

    @property
    def outcome(self) -> PlanOutcome:
        """What the running call's plans did (`PlanExecutor.outcome`; `actions.common._run` starts one per call)."""
        return self.executor.outcome

    @outcome.setter
    def outcome(self, outcome: PlanOutcome) -> None:
        self.executor.outcome = outcome

    @property
    def dry(self) -> bool:
        """Whether the running task's operation is a dry run (`dry_run`): it must send, write and adopt nothing."""
        return self.store.dry

    @property
    def upload_retry(self) -> asyncio.Task[None] | None:
        """The entry's pending retry of a failed automatic upload, if any (`ExportStore.upload_retry`)."""
        return self.store.upload_retry

    @property
    def gateway(self) -> JungHomeGatewayApi | None:
        """The entry's gateway client, or None when the entry was not set up from a gateway."""
        return self.store.gateway

    @property
    def identity_enabled(self) -> bool:
        """Whether the entry's *provisioner identity* option is on (`ExportStore.identity_enabled`)."""
        return self.store.identity_enabled

    @staticmethod
    def _scene(pf: ProjectFile, scene: str | int) -> int:
        """Resolve a scene given by number or by name (`wiring.find_scene`); a translated error when none is meant."""
        with translated():
            return find_scene(pf, scene)

    # ------------------------------------------------------------------ dry runs, outcomes, the export, the gateway
    def forcing(self, force: bool) -> AbstractContextManager[None]:
        """Run the block's operations with an action's `force`: no pre-flight comparison (`ExportStore.forcing`)."""
        return self.store.forcing(force)

    async def dry_run(
        self, operation: Callable[[MeshConfigurator], Coroutine[Any, Any, Any]]
    ) -> dict[str, Any]:
        """Run `operation` as far as its plan and answer what it would do (`ExportStore.dry_run`)."""
        return await self.store.dry_run(lambda: operation(self))

    def plan_response(self) -> dict[str, Any]:
        """Answer what the call's plans did (`PlanExecutor.plan_response`)."""
        return self.executor.plan_response()

    async def async_replay_journal(self) -> bool:
        """At setup: record what an interrupted plan left on the mesh (`PlanExecutor.async_replay_journal`)."""
        return await self.executor.async_replay_journal()

    @_translated
    async def async_identity_ranges(self) -> Ranges:
        """Home Assistant's ranges in the export on disk (`ExportStore.async_identity_ranges`)."""
        return await self.store.async_identity_ranges()

    def cancel_upload_retry(self) -> None:
        """Drop the entry's pending retry of a failed upload: a newer upload supersedes it."""
        self.store.cancel_upload_retry()

    def report_token_rejected(self, api: GatewayHost) -> None:
        """Raise the repair for a token the gateway rejects (`ExportStore.report_token_rejected`)."""
        self.store.report_token_rejected(api)

    async def adopt_for_unknown_nodes(self, macs: Sequence[str]) -> list[str]:
        """Adopt the gateway's export when it lists unknown nodes (`ExportStore.adopt_for_unknown_nodes`)."""
        return await self.store.adopt_for_unknown_nodes(macs)

    async def adopt_if_gateway_changed(self, *, raise_errors: bool = False) -> bool:
        """Adopt the gateway's export when it changed (`ExportStore.adopt_if_gateway_changed`)."""
        return await self.store.adopt_if_gateway_changed(raise_errors=raise_errors)

    @_translated
    async def async_current_export(self) -> ProjectFile:
        """Return the export as a change would plan on it now (`ExportStore.async_current_export`)."""
        return await self.store.async_current_export()

    @_translated
    async def async_export(self, flavour: str) -> dict[str, Any]:
        """Return the export on disk rendered as `flavour` (`ExportStore.async_export`)."""
        return await self.store.async_export(flavour)

    @_translated
    async def sync_gateway(self) -> bool:
        """Upload the export on disk to the gateway (`ExportStore.sync_gateway`)."""
        return await self.store.sync_gateway()

    # ------------------------------------------------------------------ nodes
    @_translated
    async def record_node(
        self,
        template: Node,
        entry_for: Callable[[dict[str, Any]], dict[str, Any]],
        audit: NodeAudit,
        plan: Plan,
        name: str,
        function: int | None = None,
        layout: int | None = None,
    ) -> DeviceCount | None:
        """Record a node Home Assistant just provisioned and commissioned (`Nodes.record_node`)."""
        return await self.nodes.record_node(
            template, entry_for, audit, plan, name, function, layout
        )

    @_translated
    async def remove_node(self, unicast: int, *, force: bool = False) -> bool:
        """Remove the node whose primary element is `unicast` from the network (`Nodes.remove_node`)."""
        return await self.nodes.remove_node(unicast, force=force)

    @_translated
    async def set_time_keeper(self, node_unicast: int, on: bool) -> bool:
        """Point the node's Time Server at the time keeper group, or stop (`Nodes.set_time_keeper`)."""
        return await self.nodes.set_time_keeper(node_unicast, on)

    # ------------------------------------------------------------------ device names and rooms
    @_translated
    async def rename_device(self, address: int, name: str) -> str:
        """Rename the app device of the element at `address` (`Rooms.rename_device`); returns the name."""
        return await self.rooms.rename_device(address, name)

    @_translated
    async def create_room(self, name: str) -> int:
        """Create a room (`Rooms.create_room`); returns its address."""
        return await self.rooms.create_room(name)

    @_translated
    async def rename_room(self, room: str, name: str) -> bool:
        """Rename a room (`Rooms.rename_room`)."""
        return await self.rooms.rename_room(room, name)

    @_translated
    async def delete_room(self, room: str) -> bool:
        """Delete a room (`Rooms.delete_room`)."""
        return await self.rooms.delete_room(room)

    async def set_room(self, address: int, room: str, *, create: bool = False) -> bool:
        """Put the load element at `address` into `room` (created when missing with `create`), leaving every other room."""
        return await self.set_rooms([address], room, create=create)

    @_translated
    async def set_rooms(
        self, addresses: Iterable[int], room: str, *, create: bool = False
    ) -> bool:
        """Put every load element in `addresses` into `room`, leaving every other room (`Rooms.set_rooms`)."""
        return await self.rooms.set_rooms(addresses, room, create=create)

    async def add_to_room(
        self, address: int, room: str, *, create: bool = False
    ) -> bool:
        """Put the load element at `address` into `room` as well (created when missing with `create`)."""
        return await self.add_to_rooms([address], room, create=create)

    @_translated
    async def add_to_rooms(
        self, addresses: Iterable[int], room: str, *, create: bool = False
    ) -> bool:
        """Put every load element in `addresses` into `room` as well (`Rooms.add_to_rooms`)."""
        return await self.rooms.add_to_rooms(addresses, room, create=create)

    async def remove_from_room(
        self, address: int, room: str, *, force: bool = False
    ) -> bool:
        """Take the load element at `address` out of `room`, leaving it in its other rooms."""
        return await self.remove_from_rooms([address], room, force=force)

    @_translated
    async def remove_from_rooms(
        self, addresses: Iterable[int], room: str, *, force: bool = False
    ) -> bool:
        """Take every load element in `addresses` out of `room` (`Rooms.remove_from_rooms`)."""
        return await self.rooms.remove_from_rooms(addresses, room, force=force)

    # ------------------------------------------------------------------ key connections
    @_translated
    async def assign_key(
        self,
        key_address: int,
        *,
        element: int | None = None,
        room: str | None = None,
        scene: str | int | None = None,
        mode: str | None = None,
        target_element: str | None = None,
        lock_seconds: int | None = None,
    ) -> bool:
        """Wire the key element at `key_address` to a load element, a room or a scene (`Keys.assign_key`)."""
        return await self.keys.assign_key(
            key_address,
            element=element,
            room=room,
            scene=scene,
            mode=mode,
            target_element=target_element,
            lock_seconds=lock_seconds,
        )

    @_translated
    async def clear_key(self, key_address: int) -> bool:
        """Give the key no function (`Keys.clear_key`)."""
        return await self.keys.clear_key(key_address)

    # ------------------------------------------------------------------ thresholds and sensor values
    @_translated
    async def check_threshold_devices(
        self, socket_address: int, devices: Iterable[int]
    ) -> None:
        """Refuse what `set_threshold_devices` would refuse, writing nothing (`Thresholds.check_threshold_devices`)."""
        await self.thresholds.check_threshold_devices(socket_address, devices)

    @_translated
    async def set_threshold_devices(
        self,
        socket_address: int,
        devices: Iterable[int],
        *,
        applied: Callable[[int, int], str] = applied_text,
    ) -> bool:
        """Make the socket's thresholds switch exactly `devices` (`Thresholds.set_threshold_devices`)."""
        return await self.thresholds.set_threshold_devices(
            socket_address, devices, applied=applied
        )

    @_translated
    async def unwire_threshold(
        self,
        socket_address: int,
        *,
        applied: Callable[[int, int], str] = applied_text,
    ) -> bool:
        """Stop the socket's thresholds switching anything (`Thresholds.unwire_threshold`)."""
        return await self.thresholds.unwire_threshold(socket_address, applied=applied)

    @_translated
    async def set_sensor_publication(
        self, node_unicast: int, on: bool, *, live: bool | None = None
    ) -> bool:
        """Publish the node's sensor values or stop (`Thresholds.set_sensor_publication`)."""
        return await self.thresholds.set_sensor_publication(node_unicast, on, live=live)

    # ------------------------------------------------------------------ scenes
    @_translated
    async def create_scene(self, name: str, icon: str | None = None) -> int:
        """Create an empty scene (`Scenes.create_scene`); returns its number."""
        return await self.scenes.create_scene(name, icon)

    @_translated
    async def rename_scene(self, scene: str | int, name: str) -> bool:
        """Rename a scene (`Scenes.rename_scene`)."""
        return await self.scenes.rename_scene(scene, name)

    async def store_scene(
        self, scene: str | int, address: int, action: V.Action | None
    ) -> bool:
        """Store the load's current state under `scene`, as the app's "save device into scene" does."""
        return await self.store_scenes(scene, [(address, action)])

    @_translated
    async def store_scenes(
        self, scene: str | int, loads: Iterable[tuple[int, V.Action | None]]
    ) -> bool:
        """Store every load's current state under `scene` (`Scenes.store_scenes`)."""
        return await self.scenes.store_scenes(scene, loads)

    async def remove_from_scene(self, scene: str | int, address: int) -> bool:
        """Take a load out of a scene: its JUNG action removed, `Scene Delete` unless a sibling channel still uses it."""
        return await self.remove_from_scenes(scene, [address])

    @_translated
    async def remove_from_scenes(
        self, scene: str | int, addresses: Iterable[int]
    ) -> bool:
        """Take every load in `addresses` out of a scene (`Scenes.remove_from_scenes`)."""
        return await self.scenes.remove_from_scenes(scene, addresses)

    @_translated
    async def delete_scene(self, scene: str | int, *, force: bool = False) -> list[str]:
        """Delete a scene from its members and the export (`Scenes.delete_scene`); returns the skipped members."""
        return await self.scenes.delete_scene(scene, force=force)

    @_translated
    async def delete_unused_scenes(
        self,
        *,
        dry_run: bool = DEFAULT_UNUSED_SCENES_DRY_RUN,
        numbers: Collection[int] | None = None,
        confirm_stale_export: bool = False,
    ) -> dict[str, list[int] | list[str]]:
        """Delete the scenes the export does not know from every register (`Scenes.delete_unused_scenes`)."""
        return await self.scenes.delete_unused_scenes(
            dry_run=dry_run, numbers=numbers, confirm_stale_export=confirm_stale_export
        )
