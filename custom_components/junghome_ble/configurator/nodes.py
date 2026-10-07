"""Nodes: recording one Home Assistant provisioned, removing one from the network, the time keeper's publication.

`Nodes`: the configurator's operations on a node as a whole — the new node's entry in the export
(`record_node`), Config Node Reset and the unwiring of every link to the node (`remove_node`), and a PP2 puck's Time
Server publication (`set_time_keeper`).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir

from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_VAULT_UNWRITABLE,
    ISSUE_VAULT_UNWRITABLE_RECORDED,
    SERVICE_LINK_WAIT,
    issue_id,
    learn_more_url,
)
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh.devices import (
    GATEWAY_PID,
    TIME_KEEPER_ADDRESS,
    TIME_SERVER,
)
from custom_components.junghome_ble.jhmesh.export import hexaddr
from custom_components.junghome_ble.jhmesh.onboarding import (
    DeviceCount,
    missing_devices,
)
from custom_components.junghome_ble.jhmesh.onboarding import record as record_node
from custom_components.junghome_ble.jhmesh.plan import (
    ConfigStep,
    bind_step,
    config_steps,
)
from custom_components.junghome_ble.onboard import advertises_unprovisioned

from .executor import Operations
from .plan import APPLIED_NOTHING, applied_removed
from .store import _failure, _validation, applied_message, run_to_end

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.audit import NodeAudit
    from custom_components.junghome_ble.jhmesh.cdb import Node
    from custom_components.junghome_ble.jhmesh.commission import Plan
    from custom_components.junghome_ble.jhmesh.export import ProjectFile

_LOGGER = logging.getLogger(__name__)

NODE_RESET_TIMEOUT = (
    3.0  # seconds to wait for a Node Reset Status, per attempt (three attempts)
)
# an unconfirmed reset: how long to look for the node advertising as a new device, and how often
RESET_ADVERT_WAIT = 5.0
RESET_ADVERT_POLL = 0.5


class Nodes(Operations):
    """Nodes of one hub's mesh as a whole: recorded, removed, made the time keeper's."""

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
        """Record a node Home Assistant just provisioned and commissioned (`onboard.async_add_device`).

        On the export as it is now (the gateway's, when the app changed it meanwhile): the template's entry is
        turned into the new node's (`entry_for`), then `onboarding.record` adds it with what the node answered, its
        element groups, its app device rows (carrying `function`, the actuator function it advertised) and the
        app's InsertId / ButtonLayout rows (with `layout`, the button layout it advertised); saved and
        handed to the gateway like any change. Returns the app's missing-devices check of the recorded rows
        (`onboarding.missing_devices`): None when the node has the devices its product and insert call for.

        The vault is written after the export: until it is, the vault on disk still lists the node as pending. A
        write that does not land raises the `vault_unwritable` repair (`vault_unwritable_recorded`: the device is
        added, the export records it) and is logged; the call does not fail, the device works. The next start
        marks the node recorded from the export (`async_note_recorded_nodes`), and the next save that lands
        clears the repair. Unverified on air.
        """
        async with self.store.lock:
            pf = await self.store.load()
            raw = pf.node_entry(template.unicast)
            template_now = pf.cdb.node_by_addr(template.unicast)
            assert template_now is not None
            node = record_node(
                pf, template_now, entry_for(raw), audit, plan, name, function, layout
            )
            count = missing_devices(
                pf,
                node,
                function if function is not None else template_now.insert_function,
            )
            # the vault keeps what the file got for it: the app's next upload lacks the node
            self.hub.vault.identity().remember_recorded(pf, node.uuid)
            await self.store.save(pf)
            if not await self.hub.vault.async_save():
                self._vault_unwritable(node.unicast)
            _LOGGER.info("Recorded the new node %r in the export", name)
            return count

    def _vault_unwritable(self, unicast: int) -> None:
        """Raise the `vault_unwritable` repair for a node the export records but the vault on disk lists as pending."""
        keeper = self.hub.vault
        entry = self.hub.entry
        address = hexaddr(unicast)
        error = keeper.write_error or "not written"
        _LOGGER.error(
            "The new device at %s is recorded in the export, but the vault %s could not be written (%s): it still "
            "lists the device as pending until a write lands",
            address,
            keeper.path,
            error,
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(entry, ISSUE_VAULT_UNWRITABLE),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_VAULT_UNWRITABLE_RECORDED,
            learn_more_url=learn_more_url(ISSUE_VAULT_UNWRITABLE_RECORDED),
            translation_placeholders={
                "title": entry.title,
                "address": address,
                "path": keeper.path,
                "error": error,
            },
        )

    async def async_note_recorded_nodes(self) -> list[str]:
        """At setup: mark recorded every pending vault node the export records with its UUID and device key.

        A node `record_node` wrote into the export whose vault write did not land (`vault_unwritable_recorded`)
        is still pending in the vault on disk: it would be named by the `pending_device` repair, and
        `reset_pending_device` would reset a working device. The export holding the same UUID and device key is
        the proof it was recorded. Returns the addresses marked; the vault is written when there are any.
        """
        keeper = self.hub.vault
        vault = keeper.vault
        if vault is None or not vault.pending:
            return []
        async with self.store.lock:
            pf = await self.store.read()
            marked: list[str] = []
            for pending in vault.pending:
                node = next((n for n in pf.cdb.nodes if n.uuid == pending.uuid), None)
                if node is None or node.dev_key != pending.dev_key:
                    continue
                vault.remember_recorded(pf, pending.uuid)
                marked.append(hexaddr(pending.unicast))
            if marked:
                _LOGGER.warning(
                    "The export records the device(s) at %s, which the vault still listed as pending: marked recorded",
                    ", ".join(marked),
                )
                await keeper.async_save()
            return marked

    async def remove_node(self, unicast: int, *, force: bool = False) -> bool:
        """Remove the node whose primary element is `unicast` from the network (experimental).

        The reset first, unlike the app (which unwires first unless forced: RTR operation mode, thresholds, the
        others' wiring, then the reset), so a node that refuses the reset keeps its wiring: Config Node Reset to the
        node (it forgets its keys and becomes an unprovisioned device again); only once it confirmed — or with
        `force`, for a node that is gone for good —
        every other node's wiring to it is removed (`ProjectFile.remove_node`: its element groups, publications to
        it) and the file records it as excluded. The reset cannot be taken back, so a stop of that unwiring — a
        cancellation too — still records the removal itself (`ProjectFile.exclude_node`) with what the
        others accepted, and the vault forgets the node either way. The gateway node is refused: taking it out is
        a takeover of its own (plan N11).

        A node can take the reset and lose its status: an unconfirmed reset is looked into
        (`_reset_unconfirmed`) rather than reported as nothing changed. The node carrying Home Assistant's link
        (`hub.proxy_node`) is refused without `force`: its reset ends the link its confirmation would come back
        on. With `force` the link lost on its reset is that silence, and the unwiring waits for the next link.
        Both unverified on air. `force` does not skip the pre-flight comparison of the others' wiring
        (`_preflight_unwiring`); the action's `skip_preflight` does.
        """
        async with self.store.lock:
            pf = await self.store.load()
            node = pf.cdb.node_by_addr(unicast)
            if node is None or node.unicast != unicast:
                raise _validation("service_unknown_element", address=hexaddr(unicast))
            if node.pid == GATEWAY_PID:
                raise _validation("remove_device_gateway")
            carries_link = unicast == self.hub.proxy_node
            if carries_link and not force:
                raise _validation(
                    "remove_device_proxy", address=self.store.node_name(unicast)
                )
            if self.store.dry:
                changes = pf.remove_node(node, self.hub.proxy.state.iv_index)
                plan = self.executor.in_order(config_steps(pf, changes))[0]
                await self.executor.preflight(plan)
                self.store.planned(
                    plan,
                    first=[
                        f"{self.store.node_name(unicast)}: {M.describe(C.node_reset())}"
                    ],
                    nodes=[unicast],
                )
            await self._preflight_unwiring(pf, unicast)
            # scanners keep a device's advert data merged: one from before it was provisioned proves nothing later
            advertised = advertises_unprovisioned(self.hub.hass, node.uuid)
            relink = False
            try:
                await self.hub.proxy.request_config(
                    unicast,
                    C.node_reset(),
                    C.CONFIG_NODE_RESET_STATUS,
                    timeout=NODE_RESET_TIMEOUT,
                )
            except TimeoutError as err:
                await self._reset_unconfirmed(
                    node, force=force, advertised=advertised, err=err
                )
            except (ConnectionError, OSError) as err:
                if not carries_link:
                    raise _failure(
                        "service_send_failed",
                        node=self.store.node_name(unicast),
                        message="Config Node Reset",
                        applied=applied_message(self.hub.hass, APPLIED_NOTHING),
                    ) from err
                _LOGGER.warning(
                    "%s carried the link, which ended with its reset; removing it from the network all the same",
                    node.name,
                )
                relink = True
            iv_index = self.hub.proxy.state.iv_index
            changes = pf.remove_node(node, iv_index)
            if relink and changes:
                # the others' unwiring needs a link; without one it stops at its first message, recorded as such
                await self.hub.async_wait_connected(SERVICE_LINK_WAIT)
            try:
                await self.executor.send(
                    config_steps(pf, changes),
                    action="junghome_ble.remove_device",
                    check=False,  # read before the reset (`_preflight_unwiring`)
                    happened={
                        "kind": "excluded",
                        "node": unicast,
                        "iv_index": iv_index,
                    },
                    applied=lambda accepted, total: applied_removed(
                        self.store.node_name(unicast), accepted, total
                    ),
                )
                await self.store.save(pf)
            finally:
                # reset whatever the file says now: its device key opens nothing any more
                vault = self.hub.vault.vault
                if vault is not None and vault.forget(node.uuid):
                    await run_to_end(self.hub.vault.async_save())
            self.executor.outcome.summary = (
                "plan_device_removed",
                {"device": self.store.node_name(unicast)},
            )
            _LOGGER.info(
                "Removed %s (%04X) from the network, %d Config messages to the others",
                node.name,
                unicast,
                len(changes),
            )
            return True

    async def _preflight_unwiring(self, pf: ProjectFile, unicast: int) -> None:
        """Read what the others' unwiring would remove and compare it with the export, before the reset.

        The reset cannot be taken back, so the unwiring that follows it is planned on a copy first and its pre-flight
        reads (`PlanExecutor.preflight`) go out before the reset: a node that no longer holds what the export says
        stops the removal before anything changed.
        """
        probe = copy.deepcopy(pf)
        node = probe.cdb.node_by_addr(unicast)
        assert node is not None  # `remove_node` found it in `pf`
        changes = probe.remove_node(node, self.hub.proxy.state.iv_index)
        await self.executor.preflight(
            self.executor.in_order(config_steps(probe, changes))[0]
        )

    async def _reset_unconfirmed(
        self, node: Node, *, force: bool, advertised: bool, err: TimeoutError
    ) -> None:
        """Decide on a Node Reset the node did not confirm: return to go on with the removal, else raise.

        The status can be lost when the node took the reset (it forgets the keys that would seal it), so silence
        is no "nothing changed". A reset node advertises as a new device (the Mesh Provisioning Service with its
        UUID): seen within RESET_ADVERT_WAIT, the reset took. Not seen proves nothing — the scanners may not reach
        it — so the error says it may have been reset, and `force` is the way on for a node that is gone; one
        that already advertised so before the reset (a stale scanner cache) is not looked for. Unverified on air.
        """
        if force:
            _LOGGER.warning(
                "%s did not confirm its reset; removing it from the network all the same",
                node.name,
            )
            return
        if not advertised and await self._advertises_reset(node.uuid):
            _LOGGER.info(
                "%s did not confirm its reset but advertises as a new device: it was reset",
                node.name,
            )
            return
        raise _failure(
            "remove_device_unconfirmed", address=self.store.node_name(node.unicast)
        ) from err

    async def _advertises_reset(self, uuid: str) -> bool:
        """Whether the node `uuid` advertises as a new device within RESET_ADVERT_WAIT (looked at every POLL)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESET_ADVERT_WAIT
        while not advertises_unprovisioned(self.hub.hass, uuid):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(RESET_ADVERT_POLL)
        return True

    async def set_time_keeper(self, node_unicast: int, on: bool) -> bool:
        """Point the node's Time Server at the PP2 pucks' time keeper group, or stop: the app's time keeper.

        `TimeKeeperConfiguration` (network-logic.md §6.2): on, the node's Time Server (`1200`) publishes to `FEFF`
        — the CDB's `#time_keeper_group#`, added when the export lacks it — bound to AppKey 0 first where the export
        shows it unbound; off, that publication is removed (`0x0000`). Sent whatever the export says (the switch
        shows the node's time role, not the export). The Time Role Set that goes with it (2 relay, 3 client) is an
        AppKey message, the switch's (`switch.JungHomeTimeKeeper`). The publication parameters (TTL 0xFF, no
        period: the node publishes when it has a new time) are the app's for every publication it sets, inferred
        for this one. Unverified on air.
        """
        async with self.store.lock:
            pf = await self.store.load()
            node = pf.cdb.node_by_addr(node_unicast)
            element = (
                None
                if node is None
                else next((e for e in node.elements if TIME_SERVER in e.models), None)
            )
            if node is None or element is None:
                raise _validation(
                    "service_unknown_element", address=hexaddr(node_unicast)
                )
            steps: list[ConfigStep] = []
            if on:
                pf.ensure_time_keeper_group()
                if (bind := bind_step(element, TIME_SERVER)) is not None:
                    steps.append(bind)
            want = TIME_KEEPER_ADDRESS if on else None
            steps += config_steps(
                pf, pf.set_publication(node, element, TIME_SERVER, want)
            )
            # the switch shows the node's time role, not the export's: no pre-flight comparison with it
            await self.executor.send(
                steps, action="the switch Time keeper", check=False
            )
            await self.store.save(pf)
            _LOGGER.info(
                "Node %04X's Time Server %s the time keeper group",
                node.unicast,
                "publishes to" if on else "no longer publishes to",
            )
            return True
