"""The connect-time reads of one hub: the state refresh and the slower reads that follow it (review-4 A4-3).

Every link starts with `after_connect`: the clock and location broadcasts, the state refresh of every load
(`state_jobs`, `_refresh_all`, which also notices a mesh that discards our PDUs), the energy poll, the heartbeat
configuration, then the scene actions, the Health faults, the current scenes and the inserts (`connect_step`: not
again soon after a round on a link that held). `async_refresh_element` asks one load again on demand.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Container
from functools import partial
from typing import TYPE_CHECKING, Protocol

from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.junghome_ble.const import (
    CONNECT_STEP_FRESH,
    LINK_CONNECTED,
    REFRESH_RETRIES,
    SHORT_LINK,
    SIGNAL_SCENES,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.devices import BATTERY_PIDS
from custom_components.junghome_ble.protocols import HubPort

if TYPE_CHECKING:
    from custom_components.junghome_ble.inserts import NodeInserts
    from custom_components.junghome_ble.jhmesh.client import AccessMessage
    from custom_components.junghome_ble.protocols import LinkView

    from .clock import Clock
    from .energy import Energy
    from .issues import Issues
    from .liveness import Liveness


class RefreshHub(HubPort, Protocol):
    """What the connect-time reads ask of the hub besides `HubPort`: the other parts and the scene caches."""

    scene_actions: dict[int, dict[int, V.Action | None]]
    scene_lists_read: set[int]

    @property
    def clock(self) -> Clock:
        """The time and location broadcasts."""

    @property
    def energy(self) -> Energy:
        """The metered loads' readings and polls."""

    @property
    def inserts(self) -> NodeInserts:
        """Each node's insert and key layout."""

    @property
    def issues(self) -> Issues:
        """The repair issues."""

    @property
    def link(self) -> LinkView:
        """The proxy link (`hub.link.LinkManager`)."""

    @property
    def liveness(self) -> Liveness:
        """The nodes' reachability and heartbeats."""

    @property
    def heartbeats_enabled(self) -> bool:
        """Whether the heartbeat option is on."""

    def scene_action_channels(self, addr: int) -> list[int]:
        """Return the channels whose JUNG scene action describes the load at `addr` in a scene."""


_LOGGER = logging.getLogger(__name__)

# State Get and the status opcode that answers it, per load kind (`Light.kind` / "switch" / a blind's "level"), plus
# the colour-temperature range a CTL light supports ("ctl_range": read once per connection, it is a device property)
# and the colour temperature on its temperature element ("ctl_temperature": Light CTL Temperature Get to the element
# after the light's, as the gateway reads it; its silence is not counted toward reachability, see `_refresh_all`).
# A socket's meter element is not here: its readings need one qualified Sensor Get each (`Energy.get_readings`).
STATE_GETS: dict[str, tuple[Callable[[], bytes], int]] = {
    "ctl": (M.light_ctl_get, M.LIGHT_CTL_STATUS),
    "dimmer": (M.light_lightness_get, M.LIGHT_LIGHTNESS_STATUS),
    "ctl_range": (M.light_ctl_temperature_range_get, M.LIGHT_CTL_TEMP_RANGE_STATUS),
    "ctl_temperature": (M.light_ctl_temperature_get, M.LIGHT_CTL_TEMP_STATUS),
    "level": (M.generic_level_get, M.GEN_LEVEL_STATUS),  # blind position / slats
}
ONOFF_GET: tuple[Callable[[], bytes], int] = (M.generic_onoff_get, M.GEN_ONOFF_STATUS)


class Refresh:
    """The connect-time reads of one hub (module docstring)."""

    def __init__(self, hub: RefreshHub) -> None:
        """Bind to `hub` (its link, devices and the other components); nothing read yet."""
        self.hub = hub  # its task, the connect-time sequence of the link, is the hub's `refresh` (`lifecycle`)
        # connect-time step → when its last complete round ended (monotonic; `connect_step`)
        self.connect_steps_done: dict[str, float] = {}
        # elements asked for their current scene right now (`_get_current_scene_of`): their Scene Status is an
        # answer, even if the firmware publishes it to its group instead of sending it to us
        self.scene_gets: set[int] = set()

    async def after_connect(self) -> None:
        """Run a link's connect-time sequence: the clock and location, the state refresh, then the slower reads.

        Time Set and the location go first, right after the proxy filter `attach` wrote (review-4 R I-5): two
        unacknowledged broadcasts, they need no refresh to be through, and sent after it they never went out on a
        link that dropped before the refresh ended — a flapping link left the nodes' clocks unset. The scene and
        fault reads are not repeated soon after a round on a link that held (`connect_step`); the heartbeat
        configuration has a longer interval of its own (`Liveness.configure_heartbeats`). The new order is unverified on air.
        """
        await self.hub.clock.send_time()
        await self.hub.clock.send_location()
        if not await self._refresh_all():
            return
        # a link lost meanwhile cancelled this task (`LinkManager.cancel_refresh`): the link is still the one refreshed
        self.hub.link.link_refresh = time.monotonic() - self.hub.link.link_since
        self.hub.link.set_link_state(LINK_CONNECTED)
        await self.hub.energy.poll()
        await self.hub.energy.backfill_history()
        await self.hub.liveness.configure_heartbeats()
        if not self.hub.heartbeats_enabled and self.hub.liveness.heartbeats_publishing:
            # the option went off while some nodes could not be told (link down, a node silent or refusing)
            await self.hub.liveness.async_disable_heartbeats()
        await self.connect_step("scene actions", self.get_scene_actions)
        await self.connect_step("faults", self._get_faults)
        await self.connect_step("current scenes", self.get_current_scenes)
        await self.connect_step("inserts", self.hub.inserts.read_unknown)

    async def connect_step(
        self, name: str, step: Callable[[], Awaitable[bool]]
    ) -> None:
        """Run a connect-time read, unless its last complete round is recent and the link before this one held.

        A link that lasted SHORT_LINK kept the hub hearing the nodes' publications (a Scene Status after every
        recall); a round within CONNECT_STEP_FRESH of this link is current enough, and repeating it on every link
        is what made a link that comes and goes keep the mesh busy (review-4 R I-5). After a short link — a failed
        connection to `LinkManager._judge_link` — the round runs again: what the hub heard through that link proves little.
        `step` returns True when it got through. Unverified on air.
        """
        done = self.connect_steps_done.get(name)
        if (
            done is not None
            and time.monotonic() - done < CONNECT_STEP_FRESH
            and self.hub.link.previous_link.lasted >= SHORT_LINK
        ):
            _LOGGER.debug(
                "%s read %.0f s ago: not asked again on this link",
                name,
                time.monotonic() - done,
            )
            return
        if await step():
            self.connect_steps_done[name] = time.monotonic()

    async def _get_faults(self) -> bool:
        """Ask every mains node for its registered Health faults (Health Fault Get to its primary element).

        JUNG nodes keep the vendor faults 0x81 / 0x80 registered (meaning unknown, `docs/hidden-features.md` §10)
        and nothing publishes the register, so the fault binary sensors are filled at link-up (`connect_step`),
        one unicast Get per node REFRESH_CHUNK at a time; a single all-nodes Get loses answers in the collision.
        False when the link went away first.
        """
        try:
            await self.hub.chunked(
                [
                    partial(self._get_faults_of, node.unicast)
                    for node in self.hub.cdb.nodes
                    if node.pid is not None and node.pid not in BATTERY_PIDS
                ]
            )
        except ConnectionError as err:
            _LOGGER.debug("fault read aborted: %s", err)
            return False
        return True

    async def _get_faults_of(self, addr: int) -> None:
        """Read one node's fault register; the Health Fault Status handler stores it."""
        try:
            await self.hub.proxy.request(
                addr,
                M.health_fault_get(),
                M.HEALTH_FAULT_STATUS,
                retries=REFRESH_RETRIES,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer its Health Fault Get", addr)

    async def get_scene_actions(self) -> bool:
        """Ask every scene member what it does in its scenes (JUNG Scene Action Setup), for the scene entities.

        One list Get per member channel, then one Get per scene it names — a few dozen messages on a typical
        installation, at link-up (`connect_step`). The export knows the members, not their actions; nothing publishes
        them, so this is the only way to show "what does this scene do". The export lists the element the Scene
        Store went to (the node's primary, for both channels of a two-channel node), so every channel of that node
        is asked, as the app asks each channel for its own list (`GetScenesForDevice`). False when the link went
        away first.
        """
        members = sorted(
            {
                channel
                for addresses in self.hub.cdb.scenes.values()
                for addr in addresses
                for channel in self.hub.scene_action_channels(addr)
            }
        )
        if not members:
            return True
        try:
            await self.hub.chunked(
                [partial(self._get_scene_actions_of, addr) for addr in members]
            )
        except ConnectionError as err:
            _LOGGER.debug("scene action read aborted: %s", err)
            return False
        async_dispatcher_send(
            self.hub.hass, SIGNAL_SCENES.format(self.hub.entry.entry_id)
        )
        return True

    async def get_current_scenes(self) -> bool:
        """Ask every element holding a scene register for its current scene (Scene Get), as the app reads it.

        The Scene Status handler stores the answer (an answer to us, not a publication: it fires no event). After
        that the nodes keep it current themselves: they publish a Scene Status after every recall. False when the
        link went away first.
        """
        registers = sorted(
            {a for addresses in self.hub.cdb.scenes.values() for a in addresses}
        )
        if not registers:
            return True
        try:
            await self.hub.chunked(
                [partial(self._get_current_scene_of, addr) for addr in registers]
            )
        except ConnectionError as err:
            _LOGGER.debug("current scene read aborted: %s", err)
            return False
        return True

    async def _get_current_scene_of(self, addr: int) -> None:
        self.scene_gets.add(addr)
        try:
            await self.hub.proxy.request(
                addr,
                M.scene_get(),
                M.SCENE_STATUS,
                retries=REFRESH_RETRIES,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer its Scene Get", addr)
        finally:
            self.scene_gets.discard(addr)

    async def _get_scene_actions_of(self, addr: int) -> None:
        """Read one element's scene list and the action of each scene into `scene_actions`.

        Each reply must name the scene it was asked for (`V.scene_action_reply_to`): matched on source and opcode
        alone, a late duplicate answer for one scene would land on the next. An answered list replaces what was
        known of the element, so a scene it no longer lists stops showing its old action.
        """

        def answers(scene: int) -> Callable[[AccessMessage], bool]:
            fits = V.scene_action_reply_to(scene)
            return lambda m: fits(m.params)

        try:
            reply = await self.hub.proxy.request(
                addr,
                V.scene_action_get(),
                V.SCENE_ACTION_SETUP_STATUS,
                expect_cid=M.JUNG_CID,
                retries=REFRESH_RETRIES,
                match=answers(V.SCENE_LIST),
            )
            listed = V.decode_scene_action_status(reply.params).scenes or ()
            self.hub.scene_lists_read.add(addr)
            for scene in [s for s in self.hub.scene_actions if s not in listed]:
                self.hub.scene_actions[scene].pop(addr, None)
                if not self.hub.scene_actions[scene]:
                    del self.hub.scene_actions[scene]
            for scene in listed:
                reply = await self.hub.proxy.request(
                    addr,
                    V.scene_action_get(scene),
                    V.SCENE_ACTION_SETUP_STATUS,
                    expect_cid=M.JUNG_CID,
                    retries=REFRESH_RETRIES,
                    match=answers(scene),
                )
                status = V.decode_scene_action_status(reply.params)
                self.hub.scene_actions.setdefault(scene, {})[addr] = status.action
        except TimeoutError:  # a status too short to name its scene is no answer either
            _LOGGER.debug("%04X did not answer its Scene Action Setup Get", addr)

    async def _refresh_all(self) -> bool:
        """Ask every load for its state, a few at a time (the app does the same on start), waiting for the replies.

        A blind is asked for its position and, when it has one, its slat level (Generic Level Get to each element).
        CTL lights are also asked for their colour-temperature range (after the states: it changes nothing visible
        until a temperature is set), then their temperature element for its colour temperature (Light CTL
        Temperature Get, the gateway's read of a tunable-white light: range from the light, temperature from the
        element after it). That last Get is a third one to the same node in the same refresh and one the app never
        sends, so its silence does not count toward the node's reachability (`counted=False`): under the app's rule
        one unanswered request marks the node unreachable at once (`Liveness.missed_answer`), and the light's own state Get
        already decides that for the node. Returns False when the link went away before the refresh was through.

        A metered load's meter element gets one job that asks for its readings property by property (`Energy.get_readings`).

        Gets are the one message JUNG firmware always answers with a unicast status, so a refresh nobody answered while
        other nodes' traffic kept arriving means the mesh discards our PDUs: a stale sequence number (lost store) or
        another client using our address. So does one nobody answered on a link whose beacon authenticated and which
        forwarded nothing decodable at all: a proxy that dropped our filter request keeps its default (empty)
        whitelist, so there *is* no other traffic to hear. Sends are otherwise fire-and-forget, so this is the only
        place to notice.
        """
        jobs = self.state_jobs()
        before = self.hub.proxy.total_stats
        try:
            await self.hub.chunked(jobs)
        except ConnectionError as err:
            _LOGGER.debug("refresh aborted: %s", err)
            return False
        after = self.hub.proxy.total_stats
        if after.messages_to_us > before.messages_to_us:
            self.hub.issues.report_pdus_dropped(False)
        elif jobs and (
            after.messages > before.messages
            or (
                self.hub.beacon_authenticated
                and self.hub.proxy.link_stats.messages == 0
            )
        ):
            _LOGGER.error(
                "No JUNG device answered the state refresh although the link works: the nodes discard our messages "
                "(stale sequence number, or address %04X is used by another client)",
                self.hub.proxy.state.src,
            )
            self.hub.issues.report_pdus_dropped(True)
        return True

    def state_jobs(
        self, only: Container[int] | None = None
    ) -> list[Callable[[], Awaitable[None]]]:
        """Return the state refresh's Gets (`_refresh_all`): of every load, or of the loads at the addresses in `only`."""
        devices = self.hub.devices
        lights = [d for d in devices.lights if only is None or d.address in only]
        jobs: list[Callable[[], Awaitable[None]]] = [
            partial(self._get_state, light.address, light.kind) for light in lights
        ]
        jobs += [
            partial(self._get_state, sock.address, "switch")
            for sock in devices.sockets
            if only is None or sock.address in only
        ]
        jobs += [
            partial(self.hub.energy.get_readings, load)
            for load in devices.metered
            if only is None or load.address in only
        ]
        jobs += [
            partial(self._get_state, addr, "level")
            for blind in devices.blinds
            if only is None or blind.address in only
            for addr in blind.level_elements
        ]
        jobs += [
            partial(self._get_state, light.address, "ctl_range")
            for light in lights
            if light.kind == "ctl"
        ]
        jobs += [
            partial(
                self._get_state,
                light.temperature_address,
                "ctl_temperature",
                counted=False,
            )
            for light in lights
            if light.kind == "ctl" and light.temperature_address is not None
        ]
        return jobs

    async def async_refresh_element(
        self, addr: int, kind: str, *, quiet: bool = False
    ) -> None:
        """Ask one load for its state now, with the connect-time refresh's Get for its kind; best effort.

        The reply lands in `states` through `JungHomeHub._on_message`, as every status does. A lost link is left for the
        caller's next send to report. `quiet`: one attempt, its miss no verdict on the node (the periodic re-probe of
        a node already known to be unreachable).
        """
        try:
            await self._get_state(addr, kind, quiet=quiet)
        except ConnectionError as err:
            _LOGGER.debug("%04X: state Get not sent: %s", addr, err)

    async def _get_state(
        self, addr: int, kind: str, *, quiet: bool = False, counted: bool = True
    ) -> None:
        """Send `kind`'s state Get to `addr` and wait for its status; a miss counts toward reachability if `counted`."""
        get, status = STATE_GETS.get(kind, ONOFF_GET)
        asked = time.monotonic()
        try:
            await self.hub.proxy.request(
                addr,
                get(),
                status,
                retries=1 if quiet else REFRESH_RETRIES,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer its state Get", addr)
            if counted:
                self.hub.liveness.missed_answer(addr, kind, asked, full=not quiet)
