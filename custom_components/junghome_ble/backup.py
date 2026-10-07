"""Backup platform: mark the sequence-number records while Home Assistant takes a backup.

A restored backup brings `.storage/junghome_ble.seq.<mesh uuid>`, its `.backup` copy and the repair's `.floor` back
together, readable, and nothing in them could tell that numbers were sent since: the next start resumed below them
and reused AES-CCM nonces under the mesh's keys. `async_pre_backup` therefore marks every record with a token of
this backup and the time it began (`backup_at`), and waits until both copies on disk carry it, so the archive holds
only marked records; `async_post_backup` removes the mark again. A start that finds a mark this process did not set
(`JungHomeHub.async_create`) continues past the record by what the address may have sent since: twice its measured
send rate over the backup's age (every record keeps when it was written and its rate, `seq_store.SendRate`), at
least SEQ_SKIP_AHEAD — review-5 S5-1: a fixed 2^20 was outrun within months by the integration's own polls. A skip
that would pass the end of the sequence space sends nothing under that IV index and raises `restore_too_old` (an IV
Update, or a new address, is the way on). A record an older version wrote (no write time) continues
SEQ_SKIP_UNKNOWN; a clock behind the record's time keeps the setup not ready until it is set. So does the start
after a Home Assistant that stopped between the two hooks. Unverified on air: a backup taken and restored on this
installation, with the lights answering at once afterwards, is the check.

Supervisor backups run the same hooks: the Supervisor calls Home Assistant's `backup/start` and `backup/end` before
and after it archives the configuration directory (read from Home Assistant's source; unverified on air).

What a restore rolls back besides the counters (review-5 S5-4): the device vault (`.storage/junghome_ble.vault.*`,
the device keys of the devices Home Assistant added) and the export with Home Assistant's own changes go back to
the backup together. A device Home Assistant provisioned after the backup is in neither any more: its device key is
gone, so it can only be reset by hand (and added again), and nothing lists its addresses. It still sends from them,
though, so every allocation of a new device keeps clear of every source heard on air (the replay list, the last
sequence number heard per source, the nodes last seen) and of the group addresses heard, besides the export and the
vault (`onboard._place`, `onboard._reserved_groups`); one not heard since the restart is not protected. A gateway
entry gets the node back with the gateway's export if Home Assistant uploaded it before the backup was restored.

What is not covered: a mesh whose entry was not loaded during this run of Home Assistant (no `HAState`, nothing to
mark: its records come back unmarked); a backup whose writes did not land within BACKUP_WRITE_TIMEOUT (logged; the
backup is never blocked); a send rate that more than doubled since the backup; and the same backup restored
twice, or an older one after a newer one, or one taken before a `seq_store_lost` or `pdus_dropped` skip — nothing
outside the restored files survives a restore, so the second start knows nothing of the first one's numbers, and
no rate counts a skip.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING

from . import seq_store
from .const import DOMAIN
from .coordinator import SEQ_BACKUP_AT, SEQ_BACKUP_TOKEN, SEQ_OWNERS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import HAState

_LOGGER = logging.getLogger(__name__)

# How long the hooks wait for both copies of every mesh's store to be written (seconds); a backup is never held
# back longer than this
BACKUP_WRITE_TIMEOUT = 10.0


async def async_pre_backup(hass: HomeAssistant) -> None:
    """Mark every mesh's records with a fresh token and the backup's start; wait (bounded) until both copies carry it."""
    token = secrets.token_hex(16)
    at = seq_store.wall_now()
    hass.data[SEQ_BACKUP_TOKEN] = token
    hass.data[SEQ_BACKUP_AT] = at
    owners = list(hass.data.get(SEQ_OWNERS, {}).values())
    for state in owners:
        state.backup_token = token
        state.backup_at = at
    await _async_save_all(hass, owners)
    unmarked = [state for state in owners if not state.carries_backup_token(token)]
    if unmarked:
        _LOGGER.warning(
            "The sequence-number store of %s was not written before the backup: a restore of this backup can "
            "repeat sequence numbers sent after it",
            ", ".join(f"address {state.src:04X}" for state in unmarked),
        )


async def async_post_backup(hass: HomeAssistant) -> None:
    """Remove the mark again: the records written from now on are not in the archive."""
    hass.data.pop(SEQ_BACKUP_TOKEN, None)
    hass.data.pop(SEQ_BACKUP_AT, None)
    owners = list(hass.data.get(SEQ_OWNERS, {}).values())
    for state in owners:
        state.backup_token = None
        state.backup_at = None
    await _async_save_all(hass, owners)


async def _async_save_all(hass: HomeAssistant, owners: list[HAState]) -> None:
    """Write every owner's two copies now; wait at most BACKUP_WRITE_TIMEOUT for them (they go on afterwards)."""
    if not owners:
        return
    saves = [
        hass.async_create_task(state.async_save_now(), f"{DOMAIN} seq store backup")
        for state in owners
    ]
    await asyncio.wait(saves, timeout=BACKUP_WRITE_TIMEOUT)
