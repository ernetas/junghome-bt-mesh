"""What one hub watches the export and the gateway for: unknown nodes, the export refresh, the gateway's trust.

A node of the mesh advertising from a MAC the export lacks is reported (`check_unknown_node`, the `unknown_nodes`
repair); for an entry set up from the gateway its export is fetched and adopted with back-off
(`request_refresh`, EXPORT_REFRESH_BACKOFF), and the hub follows it (`follow_adopted_export`). The gateway is used
only once its node vouched over the mesh for the pinned certificate (`async_gateway_distrust`, `check_pin`); a
node contradicting the pin raises the `gateway_certificate_changed` repair, and the node's address is followed
when it moves (`async_follow_gateway`). The hub delegates what the platforms, the configurator and the actions call.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from custom_components.junghome_ble.const import (
    CONF_GATEWAY_FINGERPRINT,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PIN_SOURCE,
    CONF_SOURCE,
    DOMAIN,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_UNKNOWN_NODES,
    ISSUE_UNKNOWN_NODES_GATEWAY,
    PIN_FROM_MESH,
    issue_id,
    learn_more_url,
)
from custom_components.junghome_ble.data import entry_lock
from custom_components.junghome_ble.device_info import PRODUCT_NAMES
from custom_components.junghome_ble.gateway_api import JungHomeGatewayApi, api_for_entry
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.advert import (
    JungAdvertisement,
    parse_manufacturer_data,
)
from custom_components.junghome_ble.jhmesh.devices import GATEWAY_PID
from custom_components.junghome_ble.jhmesh.properties import PROPERTIES
from custom_components.junghome_ble.protocols import HubPort
from custom_components.junghome_ble.tls import normalize_fingerprint

from .lifecycle import Backoff

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.components import bluetooth

    from custom_components.junghome_ble.jhmesh.cdb import Node
    from custom_components.junghome_ble.protocols import ConfiguratorView, LinkView


class ExportWatchHub(HubPort, Protocol):
    """What the export watch asks of the hub besides `HubPort`: the link, the configurator, the nodes by MAC."""

    node_by_mac: dict[str, Node]
    link_count: int

    @property
    def link(self) -> LinkView:
        """The proxy link (`hub.link.LinkManager`)."""

    @property
    def configurator(self) -> ConfiguratorView | None:
        """The entry's configurator, once registered (`mesh_config.MeshConfigurator`)."""

    @property
    def follow_export(self) -> Callable[[], Awaitable[None]] | None:
        """What makes the hub follow the export after a change, set by the setup."""


_LOGGER = logging.getLogger(__name__)


# A node the export does not know (`unknown_nodes`) makes the hub ask the gateway for its export; the app uploads
# its project there after a change, but not always before the new node first advertises. Unanswered, the question is
# asked again after each of these delays (the last one repeating) and on every new link, until the export has them.
EXPORT_REFRESH_BACKOFF: Final = (60.0, 300.0, 900.0, 3600.0)


# the gateway node's own LBC Manufacturer properties
GATEWAY_IP, GATEWAY_FINGERPRINT = 0xC002, 0xC003
# why the gateway is not used (`async_gateway_distrust`), for the logs and the sync repair
GATEWAY_UNVERIFIED = (
    "the gateway node has not confirmed its certificate over the mesh yet"
)
GATEWAY_CERTIFICATE_CHANGED = (
    "the gateway no longer matches the certificate pinned for it"
)
_HOST_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)", re.IGNORECASE)


def is_gateway_host(text: str) -> bool:
    """Whether `text` is an IPv4 address or a DNS host name: all a gateway address read off the mesh may be.

    It ends up in `https://{host}/...`, so nothing else (no port, path, user part or IPv6 literal) is taken.
    """
    if re.fullmatch(r"[0-9.]+", text):
        try:
            ipaddress.IPv4Address(text)
        except ValueError:
            return False
        return True
    return len(text) <= 253 and all(
        _HOST_LABEL.fullmatch(label) for label in text.split(".")
    )


class ExportWatch:
    """The unknown nodes, the export refresh and the gateway's trust of one hub (module docstring)."""

    def __init__(self, hub: ExportWatchHub) -> None:
        """Bind to `hub` (its entry, link and configurator); no unknown node, nothing fetched, nothing distrusted."""
        self.hub = hub
        self.unknown_nodes: dict[
            str, JungAdvertisement | None
        ] = {}  # MAC → what it advertises; nodes of our network the export does not know
        self._export_refresh: asyncio.Task[None] | None = (
            None  # the gateway export fetch in flight, if any
        )
        # the delay before the next fetch, one step on per unanswered fetch in a row (EXPORT_REFRESH_BACKOFF); the
        # fetch itself, when one is scheduled, is the hub's `export_refresh` timer (`JungHomeHub.lifecycle`)
        self._backoff = Backoff(schedule=EXPORT_REFRESH_BACKOFF)
        self._lifecycle = hub.lifecycle
        # one mesh read of the gateway's certificate at a time, and a pin the gateway node contradicted (not used)
        self._gateway_check = asyncio.Lock()
        self._distrusted_pin: str | None = None
        # the link (`link_count`) the pin was last checked on (`check_pin`), and whether "not confirmed" was a WARNING
        # already: on a flapping link, or with a node that never answers, it used to be one per link
        self._pin_checked_link: int | None = None
        self._unconfirmed_logged = False

    def check_unknown_node(self, info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Notice a node of *our* network advertising from a MAC the export does not know: the export is behind.

        The proxy advertisement carries our Network ID, so the node is provisioned into this mesh, and its address is
        the MAC the export would hold in the node UUID — a node added or re-provisioned after the export. Raised
        once as a repair issue listing the devices (product from the JUNG manufacturer record when the advert
        carries one); cleared when the export is reloaded with them in it (a new hub starts with an empty list).
        Addresses that are not MACs (macOS hands out UUIDs) cannot be checked.
        """
        address = info.address.upper()
        if address in self.unknown_nodes or address in self.hub.node_by_mac:
            return
        if len(address) != 17 or address.count(":") != 5:
            return
        if not self.hub.link.ours(info):
            return
        self.unknown_nodes[address] = parse_manufacturer_data(info.manufacturer_data)
        _LOGGER.warning(
            "JUNG node %s belongs to this mesh but is not in the export (%s): export the network again",
            address,
            self._describe_unknown(address),
        )
        # the app uploads its project to the gateway after a change: fetch it before bothering the user (a new
        # node restarts the back-off; the issue below stands until the reload that follows a successful fetch)
        self._backoff.reset()
        self.request_refresh()
        self.report_unknown_nodes()

    def request_refresh(self) -> None:
        """Ask the gateway for its export now, for the unknown nodes (once per MAC was not enough).

        Nothing to do without unknown nodes or a gateway the export may come from, while a fetch runs, before the
        configurator exists, or without a link — the fetch vouches for the gateway over the mesh
        (`_gateway_state`), so the next link asks instead (`LinkManager._connect_to`). A pending back-off timer is replaced.
        """
        if (
            not self.unknown_nodes
            or self.hub.stopping
            or self._gateway_for_refresh() is None
            or (self._export_refresh is not None and not self._export_refresh.done())
            or self.hub.configurator is None
            or not self.hub.connected
        ):
            return
        self.cancel_timer()
        self._export_refresh = self.hub.entry.async_create_background_task(
            self.hub.hass,
            self._refresh_export_from_gateway(),
            f"{DOMAIN} export refresh",
        )

    def cancel_timer(self) -> None:
        """Cancel the next export fetch, if one is scheduled: no unknown node is left, or the hub stops."""
        self._lifecycle.cancel_timer("export_refresh")

    def _schedule_export_refresh(self) -> None:
        """Ask again after the next EXPORT_REFRESH_BACKOFF delay: the app may not have uploaded yet."""
        delay = self._backoff.grow()
        self.cancel_timer()

        @callback
        def again(_now: datetime) -> None:
            self._lifecycle.set_timer("export_refresh", None)
            self.request_refresh()

        self._lifecycle.set_timer(
            "export_refresh", async_call_later(self.hub.hass, delay, again)
        )

    @property
    def follows_gateway(self) -> bool:
        """Whether the entry was set up from the gateway, whose export may replace ours (`_gateway_for_refresh`)."""
        return self._gateway_for_refresh() is not None

    def _gateway_for_refresh(self) -> JungHomeGatewayApi | None:
        """Return the gateway API when its export may replace ours: only for an entry set up *from* the gateway.

        An entry reconfigured to a file of the user's own (source `path` or `upload`) keeps the gateway host and
        token for the flow's re-fetch, but its file is not ours to overwrite — the fetched export would replace a
        hand-maintained `MeshNetwork.json` (with `metadata_dir` names the share format cannot carry) in place.
        """
        if self.hub.entry.data.get(CONF_SOURCE) != "gateway":
            return None
        return api_for_entry(self.hub.hass, self.hub.entry)

    def report_unknown_nodes(self) -> None:
        """Raise (or update) the `unknown_nodes` repair; an entry from the gateway gets the wording that names it.

        Two translation keys rather than a sentence in a placeholder: translators get the whole
        text, and the gateway variant's only extra placeholder is its `host`.
        """
        gateway = self._gateway_for_refresh()
        placeholders = {
            "title": self.hub.entry.title,
            "count": str(len(self.unknown_nodes)),
            "devices": ", ".join(
                self._describe_unknown(mac) for mac in sorted(self.unknown_nodes)
            ),
        }
        key = ISSUE_UNKNOWN_NODES
        if gateway is not None:
            key = ISSUE_UNKNOWN_NODES_GATEWAY
            placeholders["host"] = gateway.host
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_UNKNOWN_NODES),
            is_fixable=True,  # its repair loads a new export (`repairs.NewExportFlow`)
            data={"entry_id": self.hub.entry.entry_id},
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            learn_more_url=learn_more_url(key),
            translation_placeholders=placeholders,
        )

    async def async_follow_gateway(self) -> bool:
        """Read the gateway's address off the mesh; store it when it moved. True when the entry now names another host.

        The gateway node serves its address and certificate fingerprint as LBC Manufacturer properties (`0xC002`
        IP, `0xC003` SHA-256 of its certificate, `docs/android/transport-provisioning.md` §5.1), which is how the
        app finds a gateway whose DHCP address changed or whose certificate was renewed: it re-reads them whenever
        the gateway stops answering. The address is followed (when it is one, `is_gateway_host`); the certificate
        is not: anyone holding the NetKey and AppKey — any one node's keys — can answer on the mesh, and the pin
        is what keeps the token and the export (every DevKey) from a host that is not the gateway. A certificate
        other than the pinned one stops the gateway's use and raises the `gateway_certificate_changed` repair
        instead; the reconfigure flow reads it again and asks. The token (`0xC001`) is the app's, not the one Home
        Assistant registered for itself, and is never taken.
        """
        if self.hub.entry.data.get(CONF_SOURCE) != "gateway" or not self.hub.connected:
            return False
        node = self._gateway_node()
        if node is None:
            return False
        host = await self._gateway_text(node.unicast, GATEWAY_IP)
        fingerprint = normalize_fingerprint(
            await self._gateway_text(node.unicast, GATEWAY_FINGERPRINT)
        )
        data = self.hub.entry.data
        moved = bool(host) and host != data.get(CONF_GATEWAY_HOST)
        if moved and not is_gateway_host(str(host)):
            _LOGGER.warning(
                "The gateway node reports an address that is no IP address or host name (%r); not followed",
                host,
            )
            moved = False
        if moved:
            _LOGGER.info("The gateway node says it is at %s; following it", host)
            # not hub data (`HUB_DATA_KEYS`): the update listener does not reload for it
            self.hub.hass.config_entries.async_update_entry(
                self.hub.entry, data={**data, CONF_GATEWAY_HOST: host}
            )
        pin = normalize_fingerprint(data.get(CONF_GATEWAY_FINGERPRINT))
        if fingerprint is not None and fingerprint != pin:
            self._gateway_contradicted(pin)
            return False
        return moved

    async def async_gateway_distrust(self) -> str | None:
        """Why the entry's gateway must not be used now, or None when it may be (fetched from, uploaded to).

        The pin of an entry set up at first contact is whatever answered at the address then: a LAN impostor at
        setup would stay pinned and receive every later upload — the full export with every key. So before the
        first exchange a pin the gateway node has not vouched for is compared with the certificate the node
        reports over the mesh (`0xC003`, where the app takes its pin from): equal, it counts as vouched for from
        then on (`PIN_FROM_MESH` in the entry); different, the gateway is not used and the
        `gateway_certificate_changed` repair points to Reconfigure; unanswered, nothing is exchanged with the
        gateway yet (a WARNING the first time per hub, DEBUG after) and the next use asks again. The mesh changes
        nothing else: devices work as ever.
        """
        pin = normalize_fingerprint(self.hub.entry.data.get(CONF_GATEWAY_FINGERPRINT))
        async with self._gateway_check:
            if pin is not None and pin == self._distrusted_pin:
                return GATEWAY_CERTIFICATE_CHANGED
            if self.hub.entry.data.get(CONF_GATEWAY_PIN_SOURCE) == PIN_FROM_MESH:
                return None
            node = self._gateway_node()
            reported = (
                normalize_fingerprint(
                    await self._gateway_text(node.unicast, GATEWAY_FINGERPRINT)
                )
                if node is not None and self.hub.connected
                else None
            )
            host = self.hub.entry.data.get(CONF_GATEWAY_HOST)
            if reported is None:
                _LOGGER.log(
                    logging.DEBUG if self._unconfirmed_logged else logging.WARNING,
                    "The gateway node has not confirmed the certificate pinned for the gateway %s over the mesh "
                    "yet: nothing is fetched from or handed to the gateway until it does",
                    host,
                )
                self._unconfirmed_logged = True
                return GATEWAY_UNVERIFIED
            if reported != pin:
                self._gateway_contradicted(pin)
                return GATEWAY_CERTIFICATE_CHANGED
            _LOGGER.info(
                "The gateway node confirmed the certificate pinned for the gateway %s over the mesh",
                host,
            )
            self.hub.hass.config_entries.async_update_entry(
                self.hub.entry,
                data={**self.hub.entry.data, CONF_GATEWAY_PIN_SOURCE: PIN_FROM_MESH},
            )
            return None

    @property
    def gateway_vouched(self) -> bool:
        """Whether the gateway node vouched for the entry's pin and nothing contradicted it since.

        `async_gateway_distrust` without its mesh read: a poll of the gateway's status uses the gateway only when
        this holds, and leaves the vouching to the check each link runs after its refresh (`check_pin`).
        """
        pin = normalize_fingerprint(self.hub.entry.data.get(CONF_GATEWAY_FINGERPRINT))
        return self.hub.entry.data.get(CONF_GATEWAY_PIN_SOURCE) == PIN_FROM_MESH and (
            pin is None or pin != self._distrusted_pin
        )

    def check_pin(self) -> None:
        """While the pin is not vouched for: compare it with the gateway node's report, in the background.

        Once per link at most, once its state refresh is through (`Refresh.after_connect`): the `0xC003` Get used to
        go out at every link-up, beside the Time Set and the refresh.
        """
        if (
            self._gateway_for_refresh() is not None
            and self.hub.entry.data.get(CONF_GATEWAY_PIN_SOURCE) != PIN_FROM_MESH
            and self._distrusted_pin is None
            and self._pin_checked_link != self.hub.link_count
        ):
            self._pin_checked_link = self.hub.link_count
            self.hub.entry.async_create_background_task(
                self.hub.hass,
                self.async_gateway_distrust(),
                f"{DOMAIN} gateway certificate check",
            )

    def _gateway_contradicted(self, pin: str | None) -> None:
        """Stop using the gateway: its node reports another certificate than `pin`. Raises the repair."""
        self._distrusted_pin = pin
        _LOGGER.warning(
            "The gateway node reports another certificate over the mesh than the one pinned for the gateway %s: "
            "the gateway is not used until the entry is reconfigured",
            self.hub.entry.data.get(CONF_GATEWAY_HOST),
        )
        self.async_raise_certificate_issue()

    @callback
    def async_raise_certificate_issue(self) -> None:
        """Raise the repair pointing to Reconfigure: the gateway, or its node over the mesh, contradicts the pin."""
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_GATEWAY_CERTIFICATE),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GATEWAY_CERTIFICATE,
            learn_more_url=learn_more_url(ISSUE_GATEWAY_CERTIFICATE),
            translation_placeholders={
                "host": str(self.hub.entry.data.get(CONF_GATEWAY_HOST) or "")
            },
        )

    def _gateway_node(self) -> Node | None:
        return next((n for n in self.hub.cdb.nodes if n.pid == GATEWAY_PID), None)

    async def _gateway_text(self, unicast: int, pid: int) -> str | None:
        """Return one of the gateway node's text properties (LBC Manufacturer Get); None when it does not say.

        An answer whose value does not decode says nothing either (logged): it used to raise out of the background
        check (`check_pin`).
        """
        key = pid.to_bytes(2, "little")
        try:
            reply = await self.hub.proxy.request(
                unicast,
                M.vendor_property_get("manufacturer", pid),
                M.VENDOR_PROPERTY_STATUS_OPCODES["manufacturer"],
                expect_cid=M.JUNG_CID,
                retries=1,
                match=lambda m: m.params[:2] == key,
            )
        except (TimeoutError, ConnectionError):
            _LOGGER.debug("The gateway node %04X did not say %04X", unicast, pid)
            return None
        try:
            text = PROPERTIES[pid].codec.decode(reply.params[3:])
        except ValueError as err:
            _LOGGER.debug(
                "The gateway node %04X answered %04X with a value that does not decode: %s",
                unicast,
                pid,
                err,
            )
            return None
        return text or None

    async def _refresh_export_from_gateway(self) -> None:
        """Adopt the gateway's export when it knows the unknown nodes, then reload the entry with it.

        The dynamic-devices path for an entry set up from a gateway: the app uploads its project to the gateway
        after every change, so a node it just provisioned is usually there already. The adoption is the
        configurator's (`MeshConfigurator.adopt_for_unknown_nodes`), under its lock and with every guard of its
        gateway writes; otherwise the repair issue stays (its text then says the gateway was asked) and the fetch
        is repeated with back-off (`_schedule_export_refresh`) and on every new link.
        """
        configurator = self.hub.configurator
        assert configurator is not None  # `request_refresh` checked
        found = await configurator.adopt_for_unknown_nodes(sorted(self.unknown_nodes))
        if not found:
            self._schedule_export_refresh()
            return
        _LOGGER.info(
            "Fetched the export from the gateway %s: it lists %s; following it",
            self.hub.entry.data.get(CONF_GATEWAY_HOST),
            ", ".join(found),
        )
        self.follow_adopted_export()

    @callback
    def follow_adopted_export(self) -> None:
        """Have the device model follow an export adopted from the gateway, in a task of its own (`_reload_for_export`).

        Not one of the entry's background tasks: a reload in its place unloads the entry, which cancels those.
        """
        self.hub.hass.async_create_task(
            self._reload_for_export(), f"{DOMAIN} follow the gateway's export"
        )

    async def _reload_for_export(self) -> None:
        """Follow the adopted export, under the entry's lock (`entry_lock`): in place, else by a reload.

        A service call holds that lock while it works on this hub — waiting for its link, planning, sending — and
        has the entry follow the export itself afterwards; a reload in between would tear the hub down under it.
        Once the lock is ours, a hub that is no longer the entry's (a reload read the adopted export already) or an
        entry that is no longer loaded needs nothing more. The new nodes' devices show up without a reload
        (`model_update.async_follow_export`).
        """
        async with entry_lock(self.hub.hass, self.hub.entry.entry_id):
            if (
                self.hub.entry.state is ConfigEntryState.LOADED
                and self.hub.entry.runtime_data is self.hub
            ):
                assert self.hub.follow_export is not None  # set by the setup
                await self.hub.follow_export()

    def _describe_unknown(self, mac: str) -> str:
        advert = self.unknown_nodes.get(mac)
        if advert is None:
            return mac
        return f"{PRODUCT_NAMES.get(advert.product_id, f'product {advert.product_id}')} {mac}"
