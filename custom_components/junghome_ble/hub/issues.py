"""The repair issues one hub raises and clears, and the fixes it applies for them.

From what the link and the store show: no Bluetooth left (`report_bluetooth_unavailable`), a store that holds
sends back for SEQ_STALL_ISSUE_AFTER (`seq_stall_started`), a source near the end of its sequence space
(`check_sequence_space`), an IV index out of reach (`check_iv_index`, fixed by `async_rewind_iv_index`), keys
that open nothing (`count_undecodable`, `report_export_stale`), a key refresh (`report_key_refresh`), PDUs the mesh
discards (`report_pdus_dropped`, fixed by `async_skip_ahead`), another client at our address
(`on_foreign_own_source`, fixed by `async_skip_past_shared`) and PP2 pucks without a time keeper
(`report_time_keeper`). `clear` drops them all when the hub starts and stops; the hub delegates the fixes the
repair flows call.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_ADDRESS_SHARED,
    ISSUE_ADDRESS_SHARED_AGAIN,
    ISSUE_BLUETOOTH_UNAVAILABLE,
    ISSUE_DUPLICATE_MESH,
    ISSUE_EXPORT_STALE,
    ISSUE_INSERT_MISMATCH,
    ISSUE_IV_INDEX_AHEAD,
    ISSUE_IV_INDEX_MISMATCH,
    ISSUE_KEY_REFRESH,
    ISSUE_NODE_CLOCK_WRONG,
    ISSUE_PDUS_DROPPED,
    ISSUE_SEQ_STORE_UNWRITABLE,
    ISSUE_SEQUENCE_SPACE_LOW,
    ISSUE_TIME_KEEPER_MISSING,
    ISSUE_UNKNOWN_NODES,
    ISSUE_VAULT_KEY_REFRESH,
    NODE_INFO_TIME_ROLE,
    SEQ_SKIP_AHEAD,
    issue_id,
    learn_more_url,
)
from custom_components.junghome_ble.jhmesh.devices import (
    PP2_PIDS,
    time_keeper_candidates,
)
from custom_components.junghome_ble.protocols import HubPort
from custom_components.junghome_ble.seq_store import async_rewind_seq_floor

if TYPE_CHECKING:
    from custom_components.junghome_ble.protocols import LinkView


class IssuesHub(HubPort, Protocol):
    """What the repair issues ask of the hub besides `HubPort`: the link, which a fix drops."""

    @property
    def link(self) -> LinkView:
        """The proxy link (`hub.link.LinkManager`)."""


_LOGGER = logging.getLogger(__name__)


SEQUENCE_SPACE_WARN: Final = 0xC00000
EXPORT_STALE_THRESHOLD: Final = 20  # undecryptable PDUs / unauthenticated beacons on one link, with nothing decodable, before the export counts as stale


# the time roles a node keeping the PP2 pucks' time answers (Time Role Status): authority, relay
TIME_KEEPER_ROLES = frozenset({1, 2})

# how long the store may refuse before `seq_store_unwritable` is raised (`HAState.report_unwritable`)
SEQ_STALL_ISSUE_AFTER = 60.0


class Issues:
    """The repair issues of one hub (module docstring)."""

    def __init__(self, hub: IssuesHub) -> None:
        """Bind to `hub` (its entry, link and store); nothing raised yet."""
        self.hub = hub
        # its timers are the hub's (`JungHomeHub.lifecycle`): `seq_stall`, the look `seq_stall_started` armed, and
        # `seq_check`, `check_sequence_space` (armed by `JungHomeHub.async_start`)
        self._lifecycle = hub.lifecycle
        self._bluetooth_off = False  # `bluetooth_unavailable` is raised
        self.pdus_dropped = False  # `pdus_dropped` is raised
        # the `address_shared` repair skipped past another client's numbers since this hub started: a new sighting
        # asks for another address (`report_address_shared`)
        self._address_shared_skipped = False
        self.export_stale = False  # `export_stale` is raised

    def clear(self) -> None:
        """Drop every issue the hub raises: it starts (raised again if still due) or stops (`JungHomeHub.async_stop`)."""
        for key in (
            ISSUE_SEQ_STORE_UNWRITABLE,
            ISSUE_IV_INDEX_MISMATCH,
            ISSUE_SEQUENCE_SPACE_LOW,
            ISSUE_KEY_REFRESH,
            ISSUE_PDUS_DROPPED,
            ISSUE_ADDRESS_SHARED,
            ISSUE_EXPORT_STALE,
            ISSUE_UNKNOWN_NODES,
            ISSUE_DUPLICATE_MESH,
            ISSUE_BLUETOOTH_UNAVAILABLE,
            ISSUE_VAULT_KEY_REFRESH,
            ISSUE_INSERT_MISMATCH,
            ISSUE_NODE_CLOCK_WRONG,
            ISSUE_TIME_KEEPER_MISSING,
        ):
            ir.async_delete_issue(self.hub.hass, DOMAIN, issue_id(self.hub.entry, key))

    def report_bluetooth_unavailable(self, off: bool) -> None:
        """Raise (or clear) the repair issue for a Home Assistant without a connectable Bluetooth scanner.

        Cleared as soon as a scanner is back or a proxy node of the mesh is seen at all.
        """
        if off == self._bluetooth_off:
            return
        self._bluetooth_off = off
        key = issue_id(self.hub.entry, ISSUE_BLUETOOTH_UNAVAILABLE)
        if not off:
            ir.async_delete_issue(self.hub.hass, DOMAIN, key)
            return
        _LOGGER.warning(
            "No connectable Bluetooth adapter or proxy is available: the JUNG mesh cannot be reached"
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            key,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_BLUETOOTH_UNAVAILABLE,
            learn_more_url=learn_more_url(ISSUE_BLUETOOTH_UNAVAILABLE),
            translation_placeholders={"title": self.hub.entry.title},
        )

    @callback
    def seq_stall_started(self) -> None:
        """Look again SEQ_STALL_ISSUE_AFTER after the store began holding sends back (`HAState.reserve_seq`)."""
        self._lifecycle.cancel_timer(
            "seq_stall"
        )  # an earlier stall's, which ended meanwhile
        self._lifecycle.set_timer(
            "seq_stall",
            async_call_later(
                self.hub.hass, SEQ_STALL_ISSUE_AFTER, self._seq_stall_overdue
            ),
        )

    @callback
    def _seq_stall_overdue(self, _now: datetime) -> None:
        self._lifecycle.set_timer("seq_stall", None)
        self.hub.state.report_unwritable()

    @callback
    def check_sequence_space(self, _now: datetime | None = None) -> None:
        """Raise `sequence_space_low` while a source is past SEQUENCE_SPACE_WARN; clear it after.

        Every source (node, app, Home Assistant) stops sending at the end of the 24-bit space of the current IV
        index; the IV Update that resets it is started by the gateway (Home Assistant only follows one). The
        numbers are those the replay protection accepted, so they are what the mesh really used.
        """
        highest = self.hub.highest_seq()
        key = issue_id(self.hub.entry, ISSUE_SEQUENCE_SPACE_LOW)
        if highest is None or highest[1] < SEQUENCE_SPACE_WARN:
            ir.async_delete_issue(self.hub.hass, DOMAIN, key)
            return
        src, seq = highest
        node = self.hub.cdb.node_by_addr(src)
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            key,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SEQUENCE_SPACE_LOW,
            learn_more_url=learn_more_url(ISSUE_SEQUENCE_SPACE_LOW),
            translation_placeholders={
                "title": self.hub.entry.title,
                "source": f"{node.name} {src:04X}"
                if node is not None
                else f"{src:04X}",
                "percent": f"{100 * seq // 0xFFFFFF}",
                "iv_index": str(self.hub.proxy.state.iv_index),
            },
        )

    @callback
    def report_time_keeper(self) -> None:
        """Raise the `time_keeper_missing` repair while the project has PP2 pucks and no node keeps their time.

        The app elects a time keeper itself whenever a PP2 puck is in the project (`EnsureTimeKeeper`,
        network-logic.md §6.2); Home Assistant leaves the choice to the user (`switch.JungHomeTimeKeeper`). Raised
        only once every node that could keep the time (`devices.time_keeper_candidates`) answered its time role (the
        connect-time Time Role Get): a role not asked yet is no evidence. One that answered relay or authority keeps
        it. Names the pucks' addresses. Unverified on air: no puck in the installation.
        """
        issue = issue_id(self.hub.entry, ISSUE_TIME_KEEPER_MISSING)
        pucks = sorted(n.unicast for n in self.hub.cdb.nodes if n.pid in PP2_PIDS)
        roles = [
            self.hub.node_info(n.unicast).get(NODE_INFO_TIME_ROLE)
            for n in time_keeper_candidates(self.hub.cdb)
        ]
        known = [role[0] for role in roles if role]
        if not pucks or len(known) < len(roles) or set(known) & TIME_KEEPER_ROLES:
            ir.async_delete_issue(self.hub.hass, DOMAIN, issue)
            return
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TIME_KEEPER_MISSING,
            learn_more_url=learn_more_url(ISSUE_TIME_KEEPER_MISSING),
            translation_placeholders={
                "title": self.hub.entry.title,
                "pucks": ", ".join(f"{a:04X}" for a in pucks),
            },
        )

    def check_iv_index(self, network: int) -> None:
        """Raise `iv_index_mismatch` when the mesh's IV index is one Home Assistant cannot follow.

        `LocalState.apply_beacon` follows an index up to 42 ahead (IV Index Recovery) and ignores an older one (a
        lagging node, one behind for up to 96 hours during an update). Further ahead — Home Assistant was away
        through more IV updates than recovery allows, or its store is from another mesh — or two and more behind —
        its store is ahead of the mesh — every node drops Home Assistant's messages and the beacon that says why
        used to be dropped silently. Cleared by the next beacon within reach.

        Home Assistant ahead (pushed there by forged beacons, or a store of another mesh) is fixable when going back
        keeps every nonce unique (`LocalState.can_rewind_to`): the repair (`ISSUE_IV_INDEX_AHEAD`'s text, the
        `iv_index_mismatch` fix flow) rewinds to the mesh's index, `async_rewind_iv_index`. Otherwise, and when the
        mesh is ahead, the way back is a new unicast address (Reconfigure).
        """
        state = self.hub.proxy.state
        key = issue_id(self.hub.entry, ISSUE_IV_INDEX_MISMATCH)
        if not state.iv_known or state.iv_index - 1 <= network <= state.iv_index + 42:
            ir.async_delete_issue(self.hub.hass, DOMAIN, key)
            return
        _LOGGER.warning(
            "The mesh is at IV index %d, Home Assistant at %d: out of the reach of IV Index Recovery",
            network,
            state.iv_index,
        )
        fixable = state.can_rewind_to(network)  # behind us, and the record knows enough
        translation_key = ISSUE_IV_INDEX_AHEAD if fixable else ISSUE_IV_INDEX_MISMATCH
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            key,
            is_fixable=fixable,
            severity=ir.IssueSeverity.ERROR,
            translation_key=translation_key,
            learn_more_url=learn_more_url(translation_key),
            translation_placeholders={
                "title": self.hub.entry.title,
                "mesh": str(network),
                "ours": str(state.iv_index),
            },
            data={"entry_id": self.hub.entry.entry_id, "network": network}
            if fixable
            else None,
        )

    async def async_rewind_iv_index(self, network: int) -> str | None:
        """Take Home Assistant's IV index back to the mesh's `network` (the fixable `iv_index_mismatch` repair).

        Only while Home Assistant is still ahead out of reach and the rewind keeps every nonce unique — the state may
        have moved since the issue was raised; the repair floor is written first (`async_rewind_seq_floor`), then
        `LocalState.rewind_iv_index` moves the counter past every number sent from the mesh's index on, guarded up
        to the old index, and `HAState.persist` writes both copies of the store at once (the transmit index
        changed). The caller reloads the entry, so the next setup starts at the mesh's index. Returns None when
        done, else the reason the repair flow aborts with.
        """
        state = self.hub.state
        if not (
            state.iv_known
            and network < state.iv_index - 1
            and state.can_rewind_to(network)
        ):
            return "iv_index_not_ahead"
        seq, guard = state.rewind_point()
        if not await async_rewind_seq_floor(
            self.hub.hass,
            self.hub.cdb.mesh_uuid,
            f"{state.src:04X}",
            network,
            seq,
            guard,
        ):
            _LOGGER.error(
                "Could not write the sequence-number floor of address %04X: not going back without it",
                state.src,
            )
            return "seq_store_not_written"
        # the IV state moved while the floor was written: what it holds may no longer cover it
        if not state.can_rewind_to(network) or state.rewind_point()[1] > guard:
            return "iv_index_not_ahead"
        seq = state.rewind_iv_index(network)
        _LOGGER.warning(
            "IV index of address %04X goes back to the mesh's %d; its sequence numbers continue from %06X",
            state.src,
            network,
            seq,
        )
        return None

    def count_undecodable(self) -> None:
        """Judge a PDU our keys cannot open, or a beacon they cannot authenticate: the proxy client counted it.

        One or two are normal (another mesh in range, a node the export does not know). A link that has forwarded
        EXPORT_STALE_THRESHOLD of them and nothing decodable is a mesh whose keys are not the export's: a key refresh
        completed after the export was made (`docs/cross-repo-analysis.md` §5). Without this the symptom is only a
        silent link dropped by the watchdog every LINK_IDLE_TIMEOUT.
        """
        if (
            not self.export_stale
            and self.hub.proxy.link_stats.messages == 0
            and self.undecodable_link >= EXPORT_STALE_THRESHOLD
        ):
            self.report_export_stale(True)

    @property
    def undecodable_link(self) -> int:
        """What the current link forwarded that our keys could not open: PDUs and beacons (`count_undecodable`)."""
        stats = self.hub.proxy.link_stats
        return stats.undecryptable + stats.beacons_unauthenticated

    def report_export_stale(self, stale: bool) -> None:
        """Raise (or clear) the repair issue for a mesh whose keys are not the ones in the export."""
        self.export_stale = stale
        if not stale:
            ir.async_delete_issue(
                self.hub.hass, DOMAIN, issue_id(self.hub.entry, ISSUE_EXPORT_STALE)
            )
            return
        _LOGGER.error(
            "Nothing heard through proxy node %s can be decrypted with the keys of the export (%d messages so far): "
            "the mesh keys changed — export the network again and reconfigure",
            self.hub.proxy_address,
            self.undecodable_link,
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_EXPORT_STALE),
            is_fixable=True,  # its repair loads a new export (`repairs.NewExportFlow`)
            data={"entry_id": self.hub.entry.entry_id},
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_EXPORT_STALE,
            learn_more_url=learn_more_url(ISSUE_EXPORT_STALE),
            translation_placeholders={"title": self.hub.entry.title},
        )

    def report_key_refresh(self) -> None:
        """Raise a repair issue: the keys in the export are being replaced, the mesh will stop accepting us."""
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_KEY_REFRESH),
            is_fixable=True,  # its repair loads a new export (`repairs.NewExportFlow`)
            data={"entry_id": self.hub.entry.entry_id},
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_KEY_REFRESH,
            learn_more_url=learn_more_url(ISSUE_KEY_REFRESH),
            translation_placeholders={"title": self.hub.entry.title},
        )

    async def async_skip_ahead(self) -> None:
        """Jump our counter SEQ_SKIP_AHEAD ahead and start a fresh link (the `pdus_dropped` repair).

        The nodes drop our messages as replays when they know our numbers higher than we do: a store restored
        from an older backup, or lost. Past them the new link's filter request and refresh get through.
        """
        seq = self.hub.state.skip_ahead(SEQ_SKIP_AHEAD)
        _LOGGER.warning(
            "Sequence numbers of address %04X continue from %06X; reconnecting",
            self.hub.state.src,
            seq,
        )
        if self.hub.proxy.connected:
            # not the proxy's fault: no verdict on it, and the entities keep the grace
            await self.hub.link.drop_link(
                "sequence numbers skipped ahead", penalise=False
            )

    def report_pdus_dropped(self, dropped: bool) -> None:
        """Raise (or clear) the repair issue for a mesh that ignores us although the link works (the caller logs why).

        Raised by the Filter Status watchdog and by an unanswered state refresh; cleared by a Filter Status or by
        the first message addressed to us (`JungHomeHub._on_message`). Not raised while another client is known to send from
        our address (`address_shared` names the cause, and its repair is the one that helps).
        """
        if dropped and self.hub.state.address_shared is not None:
            return
        self.pdus_dropped = dropped
        if not dropped:
            ir.async_delete_issue(
                self.hub.hass, DOMAIN, issue_id(self.hub.entry, ISSUE_PDUS_DROPPED)
            )
            return
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_PDUS_DROPPED),
            is_fixable=True,
            data={"entry_id": self.hub.entry.entry_id},
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_PDUS_DROPPED,
            learn_more_url=learn_more_url(ISSUE_PDUS_DROPPED),
            translation_placeholders={
                "title": self.hub.entry.title,
                "unicast": f"{self.hub.proxy.state.src:04X}",
            },
        )

    def on_foreign_own_source(self, iv_index: int, seq: int) -> None:
        """Another client sends from our address: the proxy delivered a PDU from it with a number we never sent.

        Its numbers and ours run into each other — every one both send is a reused nonce, and the nodes drop ours as
        replays below its last — so from here on nothing is sent (`HAState.note_address_shared`, `AddressShared`)
        until `address_shared` is repaired. That issue explains the dropped PDUs better than `pdus_dropped` does,
        which it replaces.
        """
        if self.hub.state.note_address_shared(iv_index, seq):
            _LOGGER.error(
                "Another Bluetooth mesh client sends from Home Assistant's address %04X (sequence number %06X under "
                "IV index %d, which Home Assistant never sent): nothing is sent to the mesh %s until the repair is "
                "confirmed",
                self.hub.state.src,
                seq,
                iv_index,
                self.hub.entry.title,
            )
        if self.pdus_dropped:
            self.report_pdus_dropped(False)
        self.report_address_shared()

    def report_address_shared(self) -> None:
        """Raise `address_shared`; once its repair skipped past the other client, with the text asking for another address.

        The same issue id either way (`ISSUE_ADDRESS_SHARED_AGAIN` is only its other translation key).
        """
        translation_key = (
            ISSUE_ADDRESS_SHARED_AGAIN
            if self._address_shared_skipped
            else ISSUE_ADDRESS_SHARED
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_ADDRESS_SHARED),
            is_fixable=True,
            data={"entry_id": self.hub.entry.entry_id},
            severity=ir.IssueSeverity.ERROR,
            translation_key=translation_key,
            learn_more_url=learn_more_url(translation_key),
            translation_placeholders={
                "title": self.hub.entry.title,
                "unicast": f"{self.hub.state.src:04X}",
            },
        )

    async def async_skip_past_shared(self) -> None:
        """Continue past the other client's numbers and send again (the `address_shared` repair).

        The issue goes at once; should the other client still send above the new counter, it comes back asking for
        another address. A link whose proxy never took our filter (its request was refused too) is renewed: on the
        default white list the proxy forwards next to nothing.
        """
        seq = self.hub.state.skip_past_shared()
        self._address_shared_skipped = True
        ir.async_delete_issue(
            self.hub.hass, DOMAIN, issue_id(self.hub.entry, ISSUE_ADDRESS_SHARED)
        )
        if seq is None:
            return
        _LOGGER.warning(
            "Sequence numbers of address %04X continue from %06X, past the other client's",
            self.hub.state.src,
            seq,
        )
        if self.hub.proxy.connected and self.hub.proxy.proxy_addr is None:
            await self.hub.link.drop_link(
                "sequence numbers skipped past another client's", penalise=False
            )
