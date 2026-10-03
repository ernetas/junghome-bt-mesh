# 44 — Repairs that fix, learn-more links, troubleshooting entries

Phase P3 · Wave 12 · Size M–L · Closes: U4-5 (without reauthentication, which brief 23 built), report 5 brief F
(troubleshooting gaps), U4 F6.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

Every repair issue says where to read more, the ones with an obvious fix offer it in place, and every repair has a
troubleshooting entry.

## Background

The integration (`custom_components/junghome_ble`) raises about 16 issue kinds; only `seq_store_lost` and
`pdus_dropped` are fixable (`repairs.py`; later briefs added `iv_index_mismatch`, `seq_store_unwritable` and others —
grep for the current list). None carries `learn_more_url`; most texts end in "Settings → Devices & services → JUNG
HOME → Reconfigure". `docs/ha-integration.md` has no troubleshooting entries for `seq_store_lost`, `address_in_use`,
`address_reserved`, `sequence_space_low`, `iv_index_mismatch`, `duplicate_mesh`, `gateway_import`. The
`iv_index_mismatch` wording was fixed by brief 09; service icons are brief 50's.

## Read first

`repairs.py`; every `ir.async_create_issue` site (`grep -rn async_create_issue custom_components`): `coordinator.py`,
`mesh_config.py`, `__init__.py`, `device_names.py`, `gateway_status.py`; `strings.json` `issues`; `config_flow.py`
(upload step, gateway refetch step, the reauth helpers from brief 23); `docs/ha-integration.md` §Troubleshooting and
the user guide's `maintenance.md` (brief 43); `tests/test_translations.py`.

## Steps

1. `const.py`: an `ISSUE_LEARN_MORE` map from issue key to a docs anchor. Every `async_create_issue` passes
   `learn_more_url = <manifest documentation base> + "#" + anchor`.
2. A test computes GitHub heading slugs of the target docs and asserts every anchor exists.
3. Fix flows (`is_fixable=True`, dispatched in `async_create_fix_flow` by issue prefix):
   - `gateway_sync_failed` → confirm → `sync_gateway()`; delete on success, abort with the error otherwise.
   - `address_in_use` → confirm "use {suggestion}" → update the entry's unicast (the update listener reloads).
   - `unknown_nodes`, `export_stale`, `key_refresh` → "provide a new export": gateway entries refetch with the stored
     token (reuse the reconfigure refetch helper); file entries get an upload form (factor the config flow's upload
     validation into a shared helper, do not copy it).
   - `device_name_rejected` → a text form validated by `device_names`' rules → `name_by_user` in the registry.
   - Keep the existing fixable flows unchanged.
4. Troubleshooting entries for every issue kind (what it means, what to do, which fix the repair offers), in the user
   guide's maintenance page and the reference.

## Tests to add

One test per fix flow: success and the abort paths (entry gone, not loaded, gateway refuses, invalid upload); the
anchor test; `learn_more_url` present on every created issue (parametrised over the creation sites).

## Acceptance criteria

All gates green; every issue kind carries `learn_more_url`; the listed fix flows exist; `test_translations.py` covers
the new `fix_flow` strings.

## Verifiable on air here?

Partly: `gateway_sync_failed` (block the gateway briefly), `unknown_nodes` refetch and `device_name_rejected` can be
produced here; the others are test-only.

## Risks / off-by-default / "unverified on air"

`address_in_use`'s fix changes HA's unicast: confirm explicitly and keep the sequence-store rules (a new address starts
its own record). The upload form handles a file with every key: consume it before any validation that can fail.

## Depends on

Brief 23 (reauth helpers), brief 43 (anchors).

## Files touched

`repairs.py`, `const.py`, the issue sites in `coordinator.py`, `mesh_config.py`, `__init__.py`, `device_names.py`,
`gateway_status.py`, `config_flow.py` (shared helpers), `strings.json`, `translations/en.json`, `docs/user/`,
`docs/ha-integration.md`, `CHANGELOG.md`, tests (new `tests/test_repairs.py`, `tests/test_translations.py`).
