"""Follow the JUNG HOME app: changes made in the app reach Home Assistant without a Reconfigure.

The app uploads its project to the gateway after every change (`network-features.md` §8.2), and the phone running it
talks on the mesh while it is open: it reads states, switches loads and, for a key connection, a room or a scene,
sends the devices Config messages with their device keys, which the proxy forwards to Home Assistant like any other
traffic. A message from the phone — a source that is not Home Assistant's own address and no device (a node without a
JUNG product id, the phone's own entry in the export, or no node at all) — is the cue:

- **An entry set up from the gateway** fetches the gateway's export `APP_QUIET_AFTER` after the phone was last heard
  (a burst of edits is one fetch; the app has uploaded by then), at most once per `APP_SYNC_MIN_INTERVAL` for the
  phone's activity, and every `GATEWAY_SYNC_PERIOD` whatever was heard (what the app changed while Home Assistant
  did not hear the phone, or through the gateway from afar). `MeshConfigurator.adopt_if_gateway_changed` takes it
  over only when its digest moved since Home Assistant last synced, with every guard of the gateway's other uses;
  the device model then follows it in place where it can (`model_update.async_follow_export`), by a reload where it
  cannot. Automatically only with a pin the gateway node vouched for (so nothing is read over the mesh for it) and
  never while the gateway rejects the token. The *Fetch export from gateway* button does the same on demand.
- **An entry set up from a file** fetches nothing: once the phone was seen configuring a device (a Config Set, Add,
  Delete, Bind, Unbind or Node Reset with a device key, or a Scene Store or Delete), the `app_changed` repair asks for
  the app's new export (`repairs.NewExportFlow`). Raised once, kept across restarts, cleared when a new export
  loads (the repair, Reconfigure). Plain control from the phone raises nothing.

Both follow the options (`OPTION_FOLLOW_APP`, `OPTION_GATEWAY_CHECK`), on by default. A phone told apart wrongly costs
one GET whose digest has not moved. Unverified on air: whether the proxy forwards the phone's device-key messages, and
how soon after a change the app's upload reaches the gateway.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import (
    DEFAULT_FOLLOW_APP,
    DEFAULT_GATEWAY_CHECK,
    DOMAIN,
    GATEWAY_SYNC_PERIOD,
    ISSUE_APP_CHANGED,
    OPTION_FOLLOW_APP,
    OPTION_GATEWAY_CHECK,
    issue_id,
    learn_more_url,
)
from .jhmesh import config_messages as C
from .jhmesh import messages as M
from .mesh_config import token_rejected_open

if TYPE_CHECKING:
    from datetime import datetime

    from .coordinator import JungHomeHub
    from .jhmesh.client import AccessMessage

_LOGGER = logging.getLogger(__name__)


# Following the app (`app_follow.py`): the gateway's export is fetched APP_QUIET_AFTER seconds after the phone was last
# heard (the app uploads its project after each change; a burst of edits is one fetch), at most once per
# APP_SYNC_MIN_INTERVAL for the phone's activity, and every GATEWAY_SYNC_PERIOD seconds whatever was heard
APP_QUIET_AFTER: Final = 180.0
APP_SYNC_MIN_INTERVAL: Final = 900.0


# Config messages that change what the export records about a node (sent with its device key); the key refresh's
# own (NetKey / AppKey Update, Key Refresh Phase Set) are left to the key-refresh following
CONFIG_CHANGES = frozenset(
    {
        C.CONFIG_APPKEY_ADD,
        C.CONFIG_APPKEY_DELETE,
        C.CONFIG_BEACON_SET,
        C.CONFIG_DEFAULT_TTL_SET,
        C.CONFIG_GATT_PROXY_SET,
        C.CONFIG_MODEL_PUBLICATION_SET,
        C.CONFIG_MODEL_SUBSCRIPTION_ADD,
        C.CONFIG_MODEL_SUBSCRIPTION_DELETE,
        C.CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL,
        C.CONFIG_MODEL_SUBSCRIPTION_OVERWRITE,
        C.CONFIG_NETWORK_TRANSMIT_SET,
        C.CONFIG_RELAY_SET,
        C.CONFIG_MODEL_APP_BIND,
        C.CONFIG_MODEL_APP_UNBIND,
        C.CONFIG_NETKEY_ADD,
        C.CONFIG_NETKEY_DELETE,
        C.CONFIG_NODE_RESET,
    }
)
# ... and the scene edits, sent with the application key
SCENE_CHANGES = frozenset(
    {M.SCENE_STORE, M.SCENE_STORE_UNACK, M.SCENE_DELETE, M.SCENE_DELETE_UNACK}
)


def is_app_change(m: AccessMessage) -> bool:
    """Whether `m` changes the configuration the export records: a Config change with a device key, a scene edit."""
    if m.company_id is not None:
        return False
    if m.opcode in SCENE_CHANGES:
        return True
    return m.opcode in CONFIG_CHANGES and m.key.startswith("dev:")


class AppFollower:
    """Watch the mesh for the app and fetch the gateway's export after it, or raise `app_changed` for a file entry."""

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to `hub`; read the options (a change is applied in place, `apply_options`). Nothing runs until `start`."""
        self.hub = hub
        options = hub.entry.options
        self.follow = bool(options.get(OPTION_FOLLOW_APP, DEFAULT_FOLLOW_APP))
        self.periodic = bool(options.get(OPTION_GATEWAY_CHECK, DEFAULT_GATEWAY_CHECK))
        self._started = False
        self.issue = issue_id(hub.entry, ISSUE_APP_CHANGED)
        # the fetch once the phone is quiet, and the periodic one
        self._unsub_quiet: CALLBACK_TYPE | None = None
        self._unsub_periodic: CALLBACK_TYPE | None = None
        # when the last fetch started (monotonic), and the fetch in flight
        self._last_fetch: float | None = None
        self.task: asyncio.Task[bool] | None = None
        # `app_changed` raised, or found standing, in this run
        self._reported = False

    @callback
    def start(self) -> None:
        """Start the periodic check of an entry set up from the gateway, when the option asks for it."""
        self._started = True
        if self.periodic and self.hub.follows_gateway:
            self._unsub_periodic = async_track_time_interval(
                self.hub.hass, self._periodic, timedelta(seconds=GATEWAY_SYNC_PERIOD)
            )

    @callback
    def stop(self) -> None:
        """Cancel the timers; a fetch in flight is the entry's background task and ends with it."""
        for unsub in (self._unsub_quiet, self._unsub_periodic):
            if unsub is not None:
                unsub()
        self._unsub_quiet = self._unsub_periodic = None

    @callback
    def apply_options(self) -> None:
        """Follow the options as they are now: the phone's next message, the periodic check started or stopped.

        A fetch already waiting for the phone's quiet runs as it would have; one in flight finishes.
        """
        options = self.hub.entry.options
        self.follow = bool(options.get(OPTION_FOLLOW_APP, DEFAULT_FOLLOW_APP))
        periodic = bool(options.get(OPTION_GATEWAY_CHECK, DEFAULT_GATEWAY_CHECK))
        if periodic == self.periodic:
            return
        self.periodic = periodic
        if self._unsub_periodic is not None:
            self._unsub_periodic()
            self._unsub_periodic = None
        if self._started:
            self.start()

    def from_phone(self, m: AccessMessage) -> bool:
        """Whether `m` comes from the phone (or another client): not Home Assistant's address, and no device's."""
        if m.src == self.hub.proxy.state.src:
            return False
        node = self.hub.cdb.node_by_addr(m.src)
        return node is None or node.pid is None

    @callback
    def note(self, m: AccessMessage) -> None:
        """Account for a received message: the phone's arms the fetch, or raises `app_changed` for a file entry."""
        if not self.follow or not self.from_phone(m):
            return
        if self.hub.follows_gateway:
            self._arm()
        elif is_app_change(m):
            self._report(m)

    def _arm(self) -> None:
        """(Re)start the wait for the phone's quiet; the fetch it ends in keeps APP_SYNC_MIN_INTERVAL to the last one."""
        delay = APP_QUIET_AFTER
        if self._last_fetch is not None:
            delay = max(
                delay, self._last_fetch + APP_SYNC_MIN_INTERVAL - time.monotonic()
            )
        if self._unsub_quiet is not None:
            self._unsub_quiet()
        self._unsub_quiet = async_call_later(self.hub.hass, delay, self._quiet)

    @callback
    def _quiet(self, _now: datetime) -> None:
        self._unsub_quiet = None
        if self.task is not None and not self.task.done():
            self._arm()  # the fetch in flight may have asked before the app's upload
            return
        self.request("the app was used")

    @callback
    def _periodic(self, _now: datetime) -> None:
        self.request("the periodic check")

    @callback
    def request(self, why: str) -> None:
        """Fetch in the background, unless one runs or the gateway may not be asked without the mesh's word."""
        hub = self.hub
        if self.task is not None and not self.task.done():
            return
        if (
            hub.configurator is None
            or not hub.gateway_vouched
            or token_rejected_open(hub.hass, hub.entry)
        ):
            # the link's own check vouches for the pin; the re-authentication clears the token repair
            _LOGGER.debug("Not fetching the gateway's export for %s now", why)
            return
        _LOGGER.debug("Fetching the gateway's export: %s", why)
        self.task = hub.entry.async_create_background_task(
            hub.hass, self.async_fetch(), f"{DOMAIN} follow the app"
        )

    async def async_fetch(self, *, raise_errors: bool = False) -> bool:
        """Adopt the gateway's export when it changed, and have the hub follow it; True when it was adopted.

        `raise_errors`: the button's, which wants to hear why nothing was fetched.
        """
        configurator = self.hub.configurator
        assert configurator is not None  # registered by the setup, before any platform
        self._last_fetch = time.monotonic()
        adopted = await configurator.adopt_if_gateway_changed(raise_errors=raise_errors)
        if adopted:
            _LOGGER.info(
                "The gateway's export changed since Home Assistant last took it over; following it"
            )
            self.hub.follow_adopted_export()
        return adopted

    def _report(self, m: AccessMessage) -> None:
        """Raise `app_changed` once: persistent, so a restart keeps it until a new export loads."""
        if self._reported:
            return
        self._reported = True
        issue = ir.async_get(self.hub.hass).async_get_issue(DOMAIN, self.issue)
        if issue is not None and issue.active:
            return  # raised before a restart, still standing
        _LOGGER.warning(
            "The JUNG HOME app (%04X) changed the configuration of %04X in the mesh %s: load the app's new export",
            m.src,
            m.dst,
            self.hub.entry.title,
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            self.issue,
            is_fixable=True,  # its repair loads the new export (`repairs.NewExportFlow`)
            is_persistent=True,
            data={"entry_id": self.hub.entry.entry_id},
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_APP_CHANGED,
            learn_more_url=learn_more_url(ISSUE_APP_CHANGED),
            translation_placeholders={"title": self.hub.entry.title},
        )
