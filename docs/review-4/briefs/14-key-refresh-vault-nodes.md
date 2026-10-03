# 14 — Carry HA-provisioned nodes through a key refresh

Phase P1 · Wave 3 · Size M · Closes: D11 (P4-3); folds in P I-3 and the ledger rows `mgmt:builder:netkeyupdate…`.

Follow the [conventions](README.md#conventions) in full.

## Goal

When the JUNG app runs a NetKey renewal, nodes only HA knows (vault nodes it provisioned with `add_device`) receive
the new key and the phase transitions too, so they are not cut off at Phase 3; and no node is provisioned with a key
that is being retired.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`. HA follows the app's key refresh passively
(after brief 04: only on proof). The app sends NetKey Update "to every non-excluded node" of *its own* database
(`docs/android/transport-provisioning.md:420-423`), and the app never downloads the project (`docs/ha-integration.md`),
so a node HA added is not among them. At Phase 3 every other node and HA drop the old key; the HA-added node holds
only the old key, its traffic is discarded and HA no longer recognises its adverts. Recovery: factory reset and
re-provisioning.

- No caller of `netkey_update(` or `key_refresh_phase_set(` exists in `custom_components` or `tools` (verified); the
  builders and decoders exist (`jhmesh/config_messages.py:351-383`, `:954-966`).
- `onboard.py:188-194` provisions with `net_key=hub.proxy.nk.key` and `key_refresh = (phase == 2)`: during Phase 1 a
  new device gets the retiring key with KR=0 and misses the rest of the refresh.
- Evidence class b/c (app behaviour from the decompile; nothing run on air).

## Read first

Brief 04's result (`jhmesh/keyrefresh.py`, the follower events); `jhmesh/vault.py` (`Vault.nodes`, `VaultNode`);
`identity.py`; `onboard.py:153-330`; `jhmesh/config_messages.py:351-383`, `:954-966`; `ProxyClient.request_config`
(`jhmesh/client.py:1590-1621`); `coordinator.py` `_on_key_refresh` (`:4361`); `diagnostics.py`;
`docs/android/transport-provisioning.md` §4.2.

## Steps

1. On the follower's *proven* Phase 1 event, queue for each vault node with a device key: Config NetKey Update(new
   key) → expect NetKey Status 0.
2. On proven Phase 2 / 3, send Key Refresh Phase Set(2 / 3) to each vault node and expect a matching Phase Status.
   Never send Phase Set 3 before HA has proof the mesh is in Phase 3.
3. Retry on every new link until each node confirms; persist per-node progress in the vault.
4. When HA reaches Phase 3 with lagging vault nodes, raise a repair naming their addresses.
5. `add_device` refuses during Phase 1 with a translated error (Phase 2 already hands out the new key with KR=1).
6. Diagnostics: per-vault-node phase (no keys).
7. Ledger: the netkey-update / phase builders now have a caller; update the rows.

## Tests to add

- HA level with `FakeProxyLink.config_reply`: a vault node receives the Update and both Phase Sets in order.
- A node that never answers raises the repair; a later link retries and clears it.
- `add_device` refused in Phase 1.
- `tests/sim`: a vault node keeps working after an app-driven refresh completes.
- Never a Phase Set 3 without proof (a forged sequence from brief 04's tests sends nothing to vault nodes).

## Acceptance criteria

Gates green; docs updated ("A key refresh is followed", the provisioner identity section).

## Verifiable on air here?

No: it needs an HA-added device and the app's key renewal, which changes the real NetKey. Mark "unverified on air".

## Risks / off-by-default / "unverified on air"

Config writes to real nodes during a network-wide procedure the app drives; send only after proof. Only nodes in the
vault are ever written.

## Depends on

03 (vault entries), 04 (proof-gated follower).

## Files touched

`coordinator.py` (key-refresh hook), `onboard.py`, `jhmesh/vault.py`, `diagnostics.py`, `strings.json`,
`translations/en.json`, `docs/parity/ledger-mgmt.json`, `tests/test_key_refresh.py`, `tests/test_onboard.py`,
`tests/sim`, `CHANGELOG.md`, `docs/ha-integration.md`.
