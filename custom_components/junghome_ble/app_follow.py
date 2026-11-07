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
  loads (the repair, Reconfigure). Plain control from the phone raises nothing, and neither does a Config message
  that sets what the loaded export already records (`export_records`): the app re-sends some settings as they are
  (seen on the gateway node), and a new export would be the same file with a new timestamp.

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
from .jhmesh.audit import export_keys, export_settings
from .jhmesh.cdb import model_publication, model_subscriptions
from .mesh_config import token_rejected_open

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from .coordinator import JungHomeHub
    from .jhmesh.access import AccessMessage
    from .jhmesh.cdb import CDB, Element, Node

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


def _u16(p: bytes, at: int) -> int:
    return int.from_bytes(p[at : at + 2], "little")


def _transmit(byte: int) -> dict[str, int]:
    """Put a Relay / Network Transmit byte in the export's terms: transmissions, and ms apart (`audit`)."""
    count, steps = byte & 0x07, byte >> 3
    return {"count": count + 1, "interval": (steps + 1) * 10}


def _model_of(cdb: CDB, node: Node, p: bytes, model_at: int) -> dict[str, Any] | None:
    """Return the CDB entry of the model a `[element u16]…[model 2|4]` message names on `node`; None without one."""
    element: Element | None = cdb.element(_u16(p, 0))
    if element is None or element.node is not node:
        return None
    return element.model_entry(C.model_id_str(C.decode_model_id(p[model_at:])))


# What each Config change sets, against the node's entry: True when the export records it already. The
# parameters are the message's; a length the message cannot have, or a state the export does not record, is False.


def _app_keys(node: Node, op: int, p: bytes, _cdb: CDB) -> bool:
    keys = export_keys(node)
    adding = op == C.CONFIG_APPKEY_ADD
    if len(p) != (19 if adding else 3) or None in keys.values():
        return False
    packed = int.from_bytes(p[:3], "little")
    net, app = packed & 0xFFF, packed >> 12
    held = app in (keys["app_keys"] or ())
    return held and net in (keys["net_keys"] or ()) if adding else not held


def _net_keys(node: Node, op: int, p: bytes, _cdb: CDB) -> bool:
    nets = export_keys(node)["net_keys"]
    adding = op == C.CONFIG_NETKEY_ADD
    if len(p) != (18 if adding else 2) or nets is None:
        return False
    held = (_u16(p, 0) & 0xFFF) in nets
    return held if adding else not held


def _setting(node: Node, op: int, p: bytes, _cdb: CDB) -> bool:
    """Beacon, Default TTL, GATT Proxy, Network Transmit and Relay Set; a feature the node lacks it keeps lacking."""
    settings: dict[str, object] = export_settings(node)
    if len(p) != (2 if op == C.CONFIG_RELAY_SET else 1):
        return False
    if op == C.CONFIG_BEACON_SET:
        return settings["beacon"] is not None and settings["beacon"] == bool(p[0])
    if op == C.CONFIG_DEFAULT_TTL_SET:
        return settings["default_ttl"] == p[0]
    if op == C.CONFIG_GATT_PROXY_SET:
        return settings["gatt_proxy"] in (p[0], C.FEATURE_NOT_SUPPORTED)
    if op == C.CONFIG_NETWORK_TRANSMIT_SET:
        return settings["network_transmit"] == _transmit(p[0])
    relay = settings["relay"]
    return relay == C.FEATURE_NOT_SUPPORTED or (
        relay == p[0] and settings["relay_retransmit"] == _transmit(p[1])
    )


def _binding(node: Node, op: int, p: bytes, cdb: CDB) -> bool:
    entry = _model_of(cdb, node, p, 4)
    if entry is None or not isinstance(bound := entry.get("bind"), list):
        return False
    held = (_u16(p, 2) & 0xFFF) in bound
    return held if op == C.CONFIG_MODEL_APP_BIND else not held


def _subscription(node: Node, op: int, p: bytes, cdb: CDB) -> bool:
    delete_all = op == C.CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL
    entry = _model_of(cdb, node, p, 2 if delete_all else 4)
    if entry is None:
        return False
    subscribed = model_subscriptions(entry)
    if delete_all:
        return not subscribed
    address = _u16(p, 2)
    if op == C.CONFIG_MODEL_SUBSCRIPTION_ADD:
        return address in subscribed
    if op == C.CONFIG_MODEL_SUBSCRIPTION_DELETE:
        return address not in subscribed
    return subscribed == [address]  # Overwrite


def _publication(node: Node, _op: int, p: bytes, cdb: CDB) -> bool:
    """Compare a Model Publication Set with the model's `publish`, field by field; one the export leaves out is not.

    An address of 0x0000 (no publication) is what an entry without `publish` records.
    """
    entry = _model_of(cdb, node, p, 9)
    if entry is None:
        return False
    publish = entry.get("publish")
    if not isinstance(publish, dict) or "address" not in publish:
        return _u16(p, 2) == 0
    key_and_cred, period, retransmit = _u16(p, 4), p[7], p[8]
    count, steps = retransmit & 0x07, retransmit >> 3
    wanted: dict[str, Any] = {
        "index": key_and_cred & 0xFFF,
        "credentials": (key_and_cred >> 12) & 1,
        "ttl": p[6],
        # the export's period is steps x resolution in ms; with no retransmissions their interval means nothing
        "period": (100, 1_000, 10_000, 600_000)[period >> 6] * (period & 0x3F),
        "retransmit": {"count": count, "interval": (steps + 1) * 50 if count else None},
    }
    recorded = dict(publish)
    if isinstance(period_raw := recorded.get("period"), dict):
        n, resolution = period_raw.get("numberOfSteps"), period_raw.get("resolution")
        recorded["period"] = (
            n * resolution
            if isinstance(n, int) and isinstance(resolution, int)
            else None
        )
    if isinstance(retransmit_raw := recorded.get("retransmit"), dict):
        recorded["retransmit"] = {
            "count": retransmit_raw.get("count"),
            "interval": retransmit_raw.get("interval") if count else None,
        }
    return model_publication(entry) == _u16(p, 2) and all(
        recorded[name] == value for name, value in wanted.items() if name in recorded
    )


_RECORDED_BY = {
    C.CONFIG_APPKEY_ADD: _app_keys,
    C.CONFIG_APPKEY_DELETE: _app_keys,
    C.CONFIG_NETKEY_ADD: _net_keys,
    C.CONFIG_NETKEY_DELETE: _net_keys,
    C.CONFIG_BEACON_SET: _setting,
    C.CONFIG_DEFAULT_TTL_SET: _setting,
    C.CONFIG_GATT_PROXY_SET: _setting,
    C.CONFIG_NETWORK_TRANSMIT_SET: _setting,
    C.CONFIG_RELAY_SET: _setting,
    C.CONFIG_MODEL_APP_BIND: _binding,
    C.CONFIG_MODEL_APP_UNBIND: _binding,
    C.CONFIG_MODEL_SUBSCRIPTION_ADD: _subscription,
    C.CONFIG_MODEL_SUBSCRIPTION_DELETE: _subscription,
    C.CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL: _subscription,
    C.CONFIG_MODEL_SUBSCRIPTION_OVERWRITE: _subscription,
    C.CONFIG_MODEL_PUBLICATION_SET: _publication,
}


def export_records(cdb: CDB, m: AccessMessage) -> bool:
    """Whether a Config change sets what the export already records for its node: the app re-sending a setting.

    A Node Reset, a scene edit, a message to a node the export does not have (or to an element that is not a node's
    primary), and one this cannot read are changes.
    """
    check = _RECORDED_BY.get(m.opcode) if m.company_id is None else None
    node = cdb.node_by_addr(m.dst)
    if check is None or node is None or node.unicast != m.dst:
        return False
    # a model identifier of the wrong length, an address the export cannot hold
    try:
        return check(node, m.opcode, m.params, cdb)
    except ValueError:
        return False


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
            if export_records(self.hub.cdb, m):
                _LOGGER.debug(
                    "The JUNG HOME app re-sent what the export records already: %s", m
                )
                return
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
            "The JUNG HOME app (%04X) changed the configuration of %04X in the mesh %s: load the app's new export (%s)",
            m.src,
            m.dst,
            self.hub.entry.title,
            m,  # `AccessMessage.__str__` redacts key material
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
