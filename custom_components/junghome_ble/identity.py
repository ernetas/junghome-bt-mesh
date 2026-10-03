"""Home Assistant's provisioner identity and key vault in `.storage` (review-3 N1; `jhmesh.vault`).

One vault per mesh, `.storage/junghome_ble.vault.<mesh uuid>` (owner-only, written atomically), kept like the
mesh's sequence-number store when an entry is removed: it holds the device keys of the nodes Home Assistant
provisioned, which nothing else may have (a node whose commissioning failed is in no export), and Home Assistant's
provisioner UUID, which must stay the same for the file's entry to stay Home Assistant's.

Written whenever there is something to keep — a node provisioned, recorded or removed, ranges chosen — whatever
the options say: it is local data only. What reaches the network's file (and the gateway) is decided by the
*provisioner identity* option alone (`configurator.store.ExportStore.with_identity`), off by default. No key is
ever logged.

Every write is checked (`TrackedStore`, as the sequence store's): Home Assistant's `Store.async_save` only logs a
failed write (a full disk, a filesystem remounted read-only) and returns as if it had landed, so `async_save` says
whether it did, and one that did not is retried by the next save, changed or not (review-4 D15). A `.backup` copy
(`….vault.<mesh uuid>.backup`) is written after every write that landed; it is read when the vault is missing (Home
Assistant renames a file that is no JSON aside) or does not read back.

A vault that does not read back is copied aside to `.storage/junghome_ble.vault.<mesh uuid>.unreadable.<UTC time>`
(one copy per occurrence, never overwritten, never deleted by the integration) and removed only once that copy
landed; until then it stays where it is, untouched, and the vault is kept in memory (each save tries the copy again
before it writes). A new one is begun when the backup copy cannot help either.
A vault lost or set aside while the file already names Home Assistant as a provisioner would leave its own entry
looking like someone else's — its address "in use", its range "taken": `async_recover` recognises that entry
(`jhmesh.vault.recognise`) and takes its identity back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .jhmesh.vault import Vault, VaultError, recognise

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .jhmesh.cdb import CDB

_LOGGER = logging.getLogger(__name__)

VAULT_STORAGE_VERSION: Final = 1
VAULT_KEEPERS: HassKey[dict[str, VaultKeeper]] = HassKey(f"{DOMAIN}_vaults")


class TrackedStore(Store[dict[str, Any]]):
    """A `Store` that remembers the last payload it actually wrote to disk (`written`), as `seq_store.SeqStore`.

    `Store._async_handle_write_data` catches a `WriteError` with only a log line, so nothing else notices a write
    that never landed. `write_error` keeps the text of the last write's `WriteError` (None once a write lands).
    """

    written: dict[str, Any] | None = None
    write_error: str | None = None

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        try:
            await super()._async_write_data(data)
        except WriteError as err:
            self.write_error = str(err)
            raise  # `Store` logs it; `written` still lags on a failure
        self.written = data["data"]
        self.write_error = None


class VaultKeeper:
    """The vault of one mesh and the stores it lives in; `vault` is None until something was kept."""

    def __init__(
        self,
        store: TrackedStore,
        aside: Callable[[str], TrackedStore],
        backup: TrackedStore | None = None,
    ) -> None:
        """Keep the vault in `store` and its copy in `backup`; an unreadable one goes to `aside(<UTC time>)`."""
        self._store = store
        self._aside = aside
        self._backup = backup
        self.vault: Vault | None = None
        # the last save's failure (None once one landed): why the vault on disk lags the one in memory
        self.write_error: str | None = None
        # a stored copy that does not read back and is not set aside yet: its content and where it goes
        self._unreadable: dict[TrackedStore, tuple[dict[str, Any], TrackedStore]] = {}
        self._listeners: list[Callable[[], None]] = []
        # one save at a time: each checks that its own payload landed
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        """Where the vault is kept (for the log and the repair; it holds keys, its content is never shown)."""
        return str(self._store.path)

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` after every save (landed or not, `write_error`); return the function that removes it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def async_load(self) -> None:
        """Read the stored vault, else its `.backup` copy; a copy that does not read back is set aside (module docstring)."""
        stamp = dt_util.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        self.vault = await self._read(self._store, stamp)
        if self._backup is None:
            return
        stored = self._store.written
        if stored is not None:
            # an older version kept no copy, or the copy's last write failed: it follows the vault as stored
            if await self._backup.async_load() == stored:
                self._backup.written = stored
            else:
                await self._write(self._backup, stored)
            return
        self.vault = await self._read(self._backup, f"{stamp}.backup")
        if self.vault is not None:
            _LOGGER.warning(
                "The provisioner vault %s is missing or does not read back; its backup copy %s is used",
                self._store.path,
                self._backup.path,
            )
            await self.async_save()  # back in its place

    async def _read(self, store: TrackedStore, stamp: str) -> Vault | None:
        """Return the vault `store` holds; None when it holds none, or one that does not read back (set aside)."""
        data = await store.async_load()
        if data is None:
            return None
        try:
            vault = Vault.from_dict(data)
        except VaultError as err:
            aside = self._aside(stamp)
            _LOGGER.error(
                "The stored provisioner vault %s does not read back (%s); it is copied to %s",
                store.path,
                err,
                aside.path,
            )
            self._unreadable[store] = (data, aside)
            await self._set_aside(store)
            return None
        store.written = data
        return vault

    async def _set_aside(self, store: TrackedStore) -> bool:
        """Copy the unreadable content of `store` aside, then remove it; True once done, False while the copy fails.

        Removed only once the copy landed (`TrackedStore.written`): until then it stays where it is, and nothing is
        written over it.
        """
        data, aside = self._unreadable[store]
        await aside.async_save(data)
        if aside.written is not data:
            _LOGGER.error(
                "The unreadable provisioner vault %s could not be copied to %s (%s): it is left as it is, and the "
                "vault is kept in memory only until the copy can be written",
                store.path,
                aside.path,
                aside.write_error or "not written",
            )
            return False
        await store.async_remove()  # copied: the next start must not set it aside again
        del self._unreadable[store]
        return True

    async def async_recover(self, cdb: CDB, own_address: int) -> bool:
        """Take back Home Assistant's identity from `cdb` when the vault lost it; True when it did.

        When the vault has no identity, or one the file does not know while the file holds Home Assistant's entry
        at `own_address` (`jhmesh.vault.recognise`): the entry's UUID, node key and ranges become the vault's, its
        nodes stay. Saved at once.
        """
        found = recognise(cdb, own_address)
        if found is None or found.uuid == self.own_uuid:
            return False
        if self.vault is None:
            self.vault = found
        elif cdb.own_provisioner(self.vault.uuid) is None:
            self.vault.adopt_identity(found)
        else:
            return False  # the file knows our current identity too: not ours to replace
        _LOGGER.warning(
            "Home Assistant's provisioner entry %s at %04X was taken back from the export: the vault did not have it",
            found.uuid,
            own_address,
        )
        await self.async_save()
        return True

    @property
    def own_uuid(self) -> str | None:
        """Home Assistant's provisioner UUID, when it has one (a file may carry its entry from an earlier run)."""
        return None if self.vault is None else self.vault.uuid

    def identity(self) -> Vault:
        """Return the vault, begun with a new identity when there was none (saved with the next `async_save`)."""
        if self.vault is None:
            self.vault = Vault.create()
        return self.vault

    async def async_save(self) -> bool:
        """Write the vault (at once: it may hold a new device key); True when it is on disk now.

        Skipped when the vault on disk is the one in memory already; a save that failed is retried by the next one
        even then. The `.backup` copy follows every write that landed (a copy that fails is retried the same way,
        and does not fail the save). Listeners hear of every save (`async_add_listener`).
        """
        async with self._lock:
            landed = await self._save()
        for listener in list(self._listeners):
            listener()
        return landed

    async def _save(self) -> bool:
        if self.vault is None:
            return True
        data = self.vault.to_dict()
        self.write_error = await self._write(self._store, data)
        if self.write_error is not None:
            return False
        if self._backup is not None:
            await self._write(self._backup, data)
        return True

    async def _write(self, store: TrackedStore, data: dict[str, Any]) -> str | None:
        """Write `data` to `store` unless it holds it already; None when it does now, else why not."""
        if store in self._unreadable and not await self._set_aside(store):
            return "the unreadable vault in its place could not be copied aside"
        if store.write_error is None and store.written == data:
            return None
        await store.async_save(data)
        if store.written is not data:
            return store.write_error or "not written"
        return None


async def async_vault_keeper(hass: HomeAssistant, mesh_uuid: str) -> VaultKeeper:
    """Return the mesh's vault keeper, loaded; one per mesh UUID for the life of `hass` (like the sequence store)."""
    keepers = hass.data.setdefault(VAULT_KEEPERS, {})
    key = mesh_uuid.lower()
    if key not in keepers:
        name = f"{DOMAIN}.vault.{key}"

        def store(suffix: str = "") -> TrackedStore:
            # owner-only (it holds device keys), written atomically
            return TrackedStore(
                hass,
                VAULT_STORAGE_VERSION,
                name + suffix,
                private=True,
                atomic_writes=True,
            )

        keeper = VaultKeeper(
            store(), lambda stamp: store(f".unreadable.{stamp}"), store(".backup")
        )
        await keeper.async_load()
        keepers[key] = keeper
    return keepers[key]
