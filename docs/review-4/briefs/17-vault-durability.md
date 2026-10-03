# 17 — Vault durability, key recorded before Provisioning Data

Phase P1 · Wave 4 · Size M · Closes: D15 (S4-3); folds in S I6.

Follow the [conventions](README.md#conventions) in full.

## Goal

A device key HA provisioned is never lost silently: vault writes are tracked like the sequence store's, a failed write
raises a repair, an unreadable vault is never deleted before its copy landed, a `.backup` copy exists, and the pending
record is on disk before the device receives its Provisioning Data.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`. The vault (`jhmesh/vault.py`, kept by
`VaultKeeper` in `identity.py`) holds the device keys of nodes HA provisioned (`add_device`); they are irreplaceable —
without one, the node can only be factory-reset.

- `VaultKeeper.async_save` (`identity.py:116-124`) calls HA's `Store.async_save`, which swallows `WriteError` and only
  logs, then sets `self._written = data` regardless; later saves of the same content are skipped. On a full or
  read-only disk a failed commissioning leaves the key in RAM only; the next restart loses it. `_keep_key`
  (`onboard.py:133-150`) never sees the failure.
- `async_load` (`identity.py:58-79`) sets an unreadable vault aside with `await aside.async_save(data)` (failure
  swallowed) and then removes the original.
- The sequence store solved the same problem with `SeqStore.written` (`coordinator.py:491-540`); the vault has no
  equivalent and no `.backup`.

## Read first

`identity.py` (whole); `jhmesh/vault.py` (`pending`, `remember_provisioned`, `to_dict` / `from_dict`); `onboard.py`
(`_keep_key`, `async_add_device` around `:218-228`); `jhmesh/provisioning.py` (where the device key becomes known
relative to sending Provisioning Data); `coordinator.py` `SeqStore`; `tests/test_identity.py`, `tests/test_onboard.py`.

## Steps

1. A `written`-tracking store for the vault (reuse `SeqStore` or factor a small `TrackedStore`), `private=True`;
   `async_save` returns whether the write landed and sets `_written` only from `store.written`.
2. A `.backup` copy written alongside; load falls back to it.
3. `async_load`: remove an unreadable vault only when `aside.written is data`; otherwise keep it, log an ERROR and
   start an in-memory vault without deleting anything.
4. `_keep_key`: a failed write raises repair `vault_unwritable` naming the node's address (never the key).
5. Provisioning hook (optional parameter, library API stays compatible): `on_device_key(dev_key)` fires once the key
   is derived and before Provisioning Data is sent; the integration records the node as pending and waits for the
   write; if it did not land, abort provisioning with a translated error.

## Tests to add

- A vault store whose writes fail: repair raised, a later save retries and clears it.
- An unreadable vault with a failing aside write: the original is kept.
- Backup fallback on load.
- Provisioning aborts before the data PDU when the pending record cannot be written (fake provisioner).
- Old vault files still load; `jhmesh` at 100 % line + branch.

## Acceptance criteria

Gates green.

## Verifiable on air here?

No: needs a spare unprovisioned JUNG device; verify offline. Mark the hook "unverified on air".

## Risks / off-by-default / "unverified on air"

The provisioning hook changes the library API: keep it optional. A store version change must still load old vaults.

## Depends on

03 (same `onboard.py` / `vault.py`). Brief 40 builds on it.

## Files touched

`identity.py`, `onboard.py` (`_keep_key`, provisioning call), `jhmesh/provisioning.py`, `jhmesh/vault.py` (if needed),
`strings.json`, `translations/en.json`, `tests/test_identity.py`, `tests/test_onboard.py`,
`tests/jhmesh/test_provisioning.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
