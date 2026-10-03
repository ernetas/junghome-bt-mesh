"""Home Assistant's provisioner identity and key vault in `.storage` (review-3 N1; `jhmesh.vault`).

One vault per mesh, `.storage/junghome_ble.vault.<mesh uuid>` (owner-only, written atomically), kept like the
mesh's sequence-number store when an entry is removed: it holds the device keys of the nodes Home Assistant
provisioned, which nothing else may have (a node whose commissioning failed is in no export), and Home Assistant's
provisioner UUID, which must stay the same for the file's entry to stay Home Assistant's.

Written whenever there is something to keep — a node provisioned, recorded or removed, ranges chosen — whatever
the options say: it is local data only. What reaches the network's file (and the gateway) is decided by the
*provisioner identity* option alone (`mesh_config.MeshConfigurator._with_identity`), off by default. No key is
ever logged.

A vault that does not read back is copied aside to `.storage/junghome_ble.vault.<mesh uuid>.unreadable.<UTC time>`
(one copy per occurrence, never overwritten, never deleted by the integration) and removed, and a new one begun.
A vault lost or set aside while the file already names Home Assistant as a provisioner would leave its own entry
looking like someone else's — its address "in use", its range "taken": `async_recover` recognises that entry
(`jhmesh.vault.recognise`) and takes its identity back.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
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


class VaultKeeper:
    """The vault of one mesh and the store it lives in; `vault` is None until something was kept."""

    def __init__(
        self,
        store: Store[dict[str, Any]],
        aside: Callable[[str], Store[dict[str, Any]]],
    ) -> None:
        """Keep the vault in `store`; an unreadable one is copied to `aside(<UTC time>)`, a store of its own."""
        self._store = store
        self._aside = aside
        self.vault: Vault | None = None
        self._written: dict[str, Any] | None = None

    async def async_load(self) -> None:
        """Read the stored vault; one that does not read back is set aside (kept, never overwritten) and a new one begun."""
        data = await self._store.async_load()
        if data is None:
            return
        try:
            self.vault = Vault.from_dict(data)
        except VaultError as err:
            aside = self._aside(dt_util.utcnow().strftime("%Y%m%dT%H%M%S%fZ"))
            _LOGGER.error(
                "The stored provisioner vault %s does not read back (%s); kept as %s, a new one is begun",
                self._store.path,
                err,
                aside.path,
            )
            await aside.async_save(data)
            await (
                self._store.async_remove()
            )  # copied: the next start must not set it aside again
            return
        self._written = data

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

    async def async_save(self) -> None:
        """Write the vault when it changed since it was last read or written (at once: it may hold a new device key)."""
        if self.vault is None:
            return
        data = self.vault.to_dict()
        if data == self._written:
            return
        await self._store.async_save(data)
        self._written = data


async def async_vault_keeper(hass: HomeAssistant, mesh_uuid: str) -> VaultKeeper:
    """Return the mesh's vault keeper, loaded; one per mesh UUID for the life of `hass` (like the sequence store)."""
    keepers = hass.data.setdefault(VAULT_KEEPERS, {})
    key = mesh_uuid.lower()
    if key not in keepers:
        keeper = VaultKeeper(
            Store(
                hass,
                VAULT_STORAGE_VERSION,
                f"{DOMAIN}.vault.{key}",
                private=True,
                atomic_writes=True,
            ),
            lambda stamp: Store(
                hass,
                VAULT_STORAGE_VERSION,
                f"{DOMAIN}.vault.{key}.unreadable.{stamp}",
                private=True,
                atomic_writes=True,
            ),
        )
        await keeper.async_load()
        keepers[key] = keeper
    return keepers[key]
