"""A node's insert and key layout, as the export, the node's advertisement or the node itself tells them (F4-12).

A JUNG push-button takes any insert (switch, dimmer, DALI, blinds, extension), and its composition does not say
which; the app reads the node's InsertId (LBC User `0x0002`) when it adds it, caches it in its export
(`meta.devices[].deviceId`) and builds the load devices from it. The node also tells anyone listening, every 1.2 s
and without a key: its JUNG manufacturer record (`jhmesh.advert`) carries the actuator function and the
ButtonLayout (`0x5001`: which keys and rockers its key elements are). Here, in that order:

- the export's InsertId (`Node.insert_function`) and cached layout (`Node.button_layout`, Android share exports),
- the node's latest advertisement (`NodeInserts.adverts`, by node unicast; HA's Bluetooth cache at setup,
  `node_adverts`, then every advert the hub's callback sees),
- a read-only Get of the InsertId and the ButtonLayout (`NodeInserts.read_unknown`, a connect-time step), only of
  a push-button neither of the two above told about; its answer is kept with the node information
  (`NODE_INFO_INSERT`), so it is asked once.

What the node reported decides a push-button's load class where the export has no InsertId
(`devices.insert_function`, `apply_reported` before the device model is used); one learnt after the setup shows on
the device at once and builds the devices at the next reload. A push-button advertising another insert than the
export's (an insert was swapped after the export was made) raises the `insert_mismatch` repair: only a new export
tells the app's devices of the new insert. The node device's model names the insert, the buttons device's the
layout, and each key's event entity where the key sits (`position`), in the user's language (`selector.insert`,
`selector.button_layout` in `strings.json`). Unverified on air: the Gets, and the key positions of the mixed layouts.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components import bluetooth
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.translation import async_get_translations

from . import const
from .const import DOMAIN, ISSUE_INSERT_MISMATCH, NODE_INFO_INSERT, SIGNAL_UPDATE
from .entity import product_name, update_buttons_devices, update_node_device
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh.advert import mac_from_uuid, parse_manufacturer_data
from .jhmesh.devices import (
    KEY_LAYOUT_PIDS,
    KEY_POSITIONS,
    PUSH_BUTTON_PIDS,
    build_devices,
    insert_function,
    insert_mismatch,
    key_position,
    pick_insert,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .coordinator import JungHomeHub
    from .jhmesh.advert import JungAdvertisement
    from .jhmesh.cdb import CDB, Node
    from .jhmesh.devices import Button, Devices

_LOGGER = logging.getLogger(__name__)

INSERT_ID, BUTTON_LAYOUT = 0x0002, 0x5001
INSERT_ITEM, LAYOUT_ITEM = NODE_INFO_INSERT[INSERT_ID], NODE_INFO_INSERT[BUTTON_LAYOUT]
# the LBC servers the two Gets go to: InsertId on the User server, ButtonLayout on the Admin server
LBC_USER_SERVER, LBC_ADMIN_SERVER = "05271013", "05271011"
# the translated names: `selector.insert.options.<ACTUATOR_FUNCTION name>`, `selector.button_layout.options.<...>`
LABELS = f"component.{DOMAIN}.selector."
BUTTONS_MODEL = "Push-buttons"


def _value(info: dict[str, bytes], item: str) -> int | None:
    """Return the u16 a node answered for `item` (`NODE_INFO_INSERT`: the function / layout comes first), else None."""
    raw = info.get(item)
    return int.from_bytes(raw[:2], "little") if raw and len(raw) >= 2 else None


def node_adverts(hass: HomeAssistant, cdb: CDB) -> dict[int, JungAdvertisement]:
    """Return the JUNG record each node of `cdb` last advertised, by unicast, from Home Assistant's Bluetooth cache.

    A node advertises from its public MAC, the one its UUID holds; a record naming another product is not its own.
    """
    by_mac = {mac: n for n in cdb.nodes if (mac := mac_from_uuid(n.uuid)) is not None}
    out: dict[int, JungAdvertisement] = {}
    for info in bluetooth.async_discovered_service_info(hass, connectable=False):
        node = by_mac.get(info.address.upper())
        advert = parse_manufacturer_data(info.manufacturer_data)
        if node is not None and advert is not None and advert.product_id == node.pid:
            out[node.unicast] = advert
    return out


def apply_reported(
    cdb: CDB,
    devices: Devices,
    adverts: dict[int, JungAdvertisement],
    node_info: Callable[[int], dict[str, bytes]],
) -> Devices:
    """Give each push-button what it reported of its insert; return the device model, rebuilt when that changed it.

    The advertisement first, else the answer to an earlier InsertId Get (`node_info` by unicast, `NODE_INFO_INSERT`).
    It decides only where the export cached no InsertId (`devices.insert_function`).
    """
    changed = False
    for node in cdb.nodes:
        if node.pid not in PUSH_BUTTON_PIDS:
            continue
        before = insert_function(node)
        advert = adverts.get(node.unicast)
        node.reported_function = (
            advert.actuator_function_id
            if advert is not None
            else _value(node_info(node.unicast), INSERT_ITEM)
        )
        changed = changed or insert_function(node) != before
    return build_devices(cdb, devices.metadata) if changed else devices


async def async_load_labels(hass: HomeAssistant) -> dict[str, str]:
    """Return the insert and layout names in Home Assistant's language, by `insert.options.<key>` and the like."""
    translations = await async_get_translations(
        hass, hass.config.language, "selector", {DOMAIN}
    )
    return {
        key.removeprefix(LABELS): text
        for key, text in translations.items()
        if key.startswith((f"{LABELS}insert.", f"{LABELS}button_layout."))
    }


class NodeInserts:
    """What the hub knows of each node's insert and key layout (module docstring), and what it shows of them."""

    def __init__(self, hub: JungHomeHub, issue: str) -> None:
        """Bind to `hub`; `issue` is the entry's `insert_mismatch` repair id."""
        self.hub = hub
        self.issue = issue
        # node unicast → its latest JUNG record (`note_advert`)
        self.adverts: dict[int, JungAdvertisement] = {}
        self.labels: dict[str, str] = {}  # `async_load_labels`
        # nodes that answered a Get without a value (they do not have it): not asked again by this hub
        self._unsupported: set[int] = set()
        # nodes whose insert became known after their devices were built (logged once)
        self._late: set[int] = set()

    async def async_setup(self) -> None:
        """Take what the nodes reported before the devices are registered: the cached adverts, earlier answers.

        A push-button whose export has no InsertId gets the load class of the insert it advertised or answered
        (`apply_reported`); the insert and layout names are loaded in Home Assistant's language.
        """
        hub = self.hub
        self.adverts.update(node_adverts(hub.hass, hub.cdb))
        hub.devices = apply_reported(hub.cdb, hub.devices, self.adverts, hub.node_info)
        self.labels = await async_load_labels(hub.hass)

    # ------------------------------------------------------------------ what is known
    def reported_function(self, node: Node) -> int | None:
        """Return the actuator function the node reported: its latest advert, else its answer to a Get, else at setup."""
        if (advert := self.adverts.get(node.unicast)) is not None:
            return advert.actuator_function_id
        answered = _value(self.hub.node_info(node.unicast), INSERT_ITEM)
        return answered if answered is not None else node.reported_function

    def function(self, node: Node) -> int | None:
        """Return the push-button's insert: the export's InsertId, else what the node reported (`devices.pick_insert`)."""
        return pick_insert(node.pid, node.insert_function, self.reported_function(node))

    def layout(self, node: Node) -> int | None:
        """Return the ButtonLayout of a push-button or wall transmitter (`KEY_LAYOUT_PIDS`): export, advert, Get; else None."""
        if node.pid not in KEY_LAYOUT_PIDS:
            return None
        advert = self.adverts.get(node.unicast)
        for layout in (
            node.button_layout,
            advert.button_layout if advert is not None else None,
            _value(self.hub.node_info(node.unicast), LAYOUT_ITEM),
        ):
            if layout in KEY_POSITIONS:
                return layout
        return None

    def position(self, button: Button) -> str | None:
        """Return where the key sits on its node (`devices.key_position`); None for an input or while the layout is unknown."""
        return (
            None
            if button.input
            else key_position(self.layout(button.node), button.location)
        )

    def _label(self, kind: str, names: dict[int, str], value: int | None) -> str | None:
        name = names.get(value) if value is not None else None
        return self.labels.get(f"{kind}.options.{name}") if name is not None else None

    def node_model(self, node: Node) -> str:
        """Return the node device's model: the product, and a push-button's insert once known (`Push-button 1-gang (DALI insert)`)."""
        product = product_name(node.pid)
        insert = self._label("insert", P.ACTUATOR_FUNCTION, self.function(node))
        return f"{product} ({insert})" if insert else product

    def buttons_model(self, node: Node) -> str:
        """Return a buttons device's model: push-buttons, and the node's key layout once known (`Push-buttons (Rocker | Button)`)."""
        layout = self._label("button_layout", P.BUTTON_LAYOUT, self.layout(node))
        return f"{BUTTONS_MODEL} ({layout})" if layout else BUTTONS_MODEL

    # ------------------------------------------------------------------ learning
    def note_advert(self, node: Node, advert: JungAdvertisement | None) -> None:
        """Keep the node's latest JUNG record; a new function or layout shows on its devices and in the repair."""
        if advert is None or advert.product_id != node.pid:
            return
        old = self.adverts.get(node.unicast)
        self.adverts[node.unicast] = advert
        if old is None or (old.actuator_function_id, old.button_layout) != (
            advert.actuator_function_id,
            advert.button_layout,
        ):
            self.changed(node)

    def changed(self, node: Node) -> None:
        """Show what is now known of the node: its devices' models, its keys' positions, the mismatch repair."""
        update_node_device(self.hub.hass, self.hub, node)
        update_buttons_devices(self.hub.hass, self.hub, node)
        for button in self.hub.devices.buttons:
            if button.node is node:  # the key's event entity shows its `position`
                async_dispatcher_send(
                    self.hub.hass,
                    SIGNAL_UPDATE.format(self.hub.entry.entry_id, button.address),
                )
        if (
            self.function(node) != insert_function(node)
            and node.unicast not in self._late
        ):
            self._late.add(node.unicast)
            _LOGGER.info(
                "Node %04X reports its insert now: its devices follow at the next reload of the entry",
                node.unicast,
            )
        self.report_mismatch()

    def report_mismatch(self) -> None:
        """Raise (or update, or clear) the `insert_mismatch` repair: push-buttons advertising another insert than the export's."""
        swapped = [
            (node, advert.actuator_function_id)
            for node in self.hub.cdb.nodes
            if (advert := self.adverts.get(node.unicast)) is not None
            and insert_mismatch(
                node.pid, node.insert_function, advert.actuator_function_id
            )
        ]
        if not swapped:
            ir.async_delete_issue(self.hub.hass, DOMAIN, self.issue)
            return

        def name(function: int | None) -> str:
            return self._label("insert", P.ACTUATOR_FUNCTION, function) or str(function)

        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            self.issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_INSERT_MISMATCH,
            translation_placeholders={
                "title": self.hub.entry.title,
                "devices": ", ".join(
                    f"{node.name} {node.unicast:04X} ({name(node.insert_function)} → {name(reported)})"
                    for node, reported in swapped
                ),
            },
        )

    # ------------------------------------------------------------------ the fallback Gets
    def _unknown(self, node: Node) -> bool:
        """Whether nothing told the push-button's insert: no export InsertId, no advert, no earlier answer."""
        return (
            node.pid in PUSH_BUTTON_PIDS
            and node.unicast not in self._unsupported
            and pick_insert(node.pid, node.insert_function, None) is None
            and node.unicast not in self.adverts
            and node.reported_function is None
            and INSERT_ITEM not in self.hub.node_info(node.unicast)
        )

    async def read_unknown(self) -> bool:
        """Ask each push-button nothing told about for its InsertId and its ButtonLayout (read-only; a connect step).

        True when every node asked answered (or there was none to ask): the step is then not repeated soon
        (`JungHomeHub._connect_step`). False when one stayed silent or the link went away.
        """
        complete = True
        for node in [n for n in self.hub.cdb.nodes if self._unknown(n)]:
            try:
                function = await self._ask(node, "user", INSERT_ID, LBC_USER_SERVER)
                layout = (
                    await self._ask(node, "admin", BUTTON_LAYOUT, LBC_ADMIN_SERVER)
                    if self.layout(node) is None
                    else b""
                )
            except ConnectionError:
                return False
            if function is None or layout is None:
                complete = False
            elif not function:
                self._unsupported.add(node.unicast)
            self.changed(node)
        return complete

    async def read_insert(self, node: Node) -> int | None:
        """Ask a push-button `add_device` is commissioning for its InsertId (the app's RequestRequiredData).

        Returns the insert it carries (`devices.pick_insert`), None when it stayed silent or answered none a
        push-button can carry; an answer is kept with the node information like every other (`_ask`).
        """
        value = await self._ask(node, "user", INSERT_ID, LBC_USER_SERVER)
        raw = int.from_bytes(value[:2], "little") if value and len(value) >= 2 else None
        return pick_insert(node.pid, raw, None)

    async def _ask(self, node: Node, kind: str, pid: int, server: str) -> bytes | None:
        """Get LBC property `pid` from the node's `kind` server; keep a value it answers (`NODE_INFO_INSERT`).

        Returns the value (empty: a Status without one, or no such server on the node), None when it stayed silent.
        """
        element = next((e for e in node.elements if server in e.models), None)
        if element is None:
            return b""
        wanted = pid.to_bytes(2, "little")
        try:
            reply = await self.hub.proxy.request(
                element.address,
                M.vendor_property_get(kind, pid),
                M.VENDOR_PROPERTY_STATUS_OPCODES[kind],
                timeout=const.PROPERTY_READ_TIMEOUT,
                retries=const.PROPERTY_READ_RETRIES,
                expect_cid=M.JUNG_CID,
                match=lambda m: bytes(m.params[:2]) == wanted,
            )
        except TimeoutError:
            _LOGGER.debug(
                "%04X did not answer the Get of its %s",
                node.unicast,
                NODE_INFO_INSERT[pid],
            )
            return None
        value = bytes(reply.params[3:])
        if value:
            self.hub.remember_node_info(node.unicast, NODE_INFO_INSERT[pid], value)
        return value
