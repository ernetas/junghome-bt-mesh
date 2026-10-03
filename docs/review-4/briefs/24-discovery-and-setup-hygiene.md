# 24 — Discovery and setup: own mesh after a key refresh, stale export, JUNG-only

Phase P1 · Wave 6 · Size S–M · Closes: H4-4 — folds in H I-6 (`export_keys_stale`), H I-7 (JUNG-only matcher),
H4-9's `_set_confirm_only` is left to brief 50; H I-9 (unique id = mesh UUID) optional by decision M10.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

The integration's own mesh is never offered again as a new discovery after a key refresh; a stale export (proxies of
a mesh visible but none matching the export's Network ID) gets its own clear error; optionally only JUNG proxies
start discovery.

## Background

`custom_components/junghome_ble` (HA integration) discovers Bluetooth Mesh proxies (`manifest.json` matches only
service UUID 0x1828) and keys config entries by Network ID.

- **H4-4 (plausible).** `config_flow.py:481-494` dedups discovery only on the entry `unique_id` (the Network ID);
  `_on_key_refresh` (`coordinator.py:4360-4373`) updates it only at phase 0 and never aborts the discovery flow the
  new Network ID started. If the user clicks *Ignore*, the later `async_update_entry(unique_id=…)` hits HA's
  collision path (an ERROR "already in use" and a core repair).
- **H I-6.** `config_flow.py:348-370`, `:419-420` and `__init__.py:189-198` say "no node of this mesh network is
  visible" when proxies advertise an unmatched Network ID — the commonest support case after a key refresh.
- **H I-7.** Any SIG Mesh proxy offers "JUNG HOME (Bluetooth Mesh)"; JUNG nodes also advertise company ID 0x0527
  (`jhmesh/advert.py`), in separate non-connectable packets.

## Read first

- `manifest.json`; `config_flow.py:348-370`, `:481-506`, `:1059-1119`; `__init__.py:189-198`.
- `coordinator.py` `_on_key_refresh` (~4360), `async_apply_followed_key_refresh` (~744) — after brief 04.
- `jhmesh/advert.py` (`JUNG_COMPANY_ID`); HA `config_entries.py` `_abort_if_unique_id_configured` and the collision
  path.
- `tests/test_config_flow.py`, `tests/test_init.py`, `tests/test_key_refresh.py`.

## Steps

1. `async_step_bluetooth`: abort `already_configured` when any configured entry's export or followed key refresh
   yields this Network ID; cache per-entry Network IDs on the hub, load the CDB in the executor for entries that are
   not loaded; schedule a reload of an entry in `SETUP_RETRY`, as HA does.
2. `_on_key_refresh`: before `async_update_entry(unique_id=…)`, abort in-progress flows for the new id; remove an
   *ignored* entry holding it (log at INFO).
3. A helper `mesh_proxies_without_match(hass, cdb)`; in `validate_input` and setup use a new `export_keys_stale`
   error / not-ready reason pointing at re-export or Reconfigure (strings in both files).
4. JUNG-only discovery, **only after the on-air check below**: add `"manufacturer_id": 1319` to the manifest matcher
   if HA's service info for the proxy MAC carries 0x0527; otherwise check `manufacturer_data` in
   `async_step_bluetooth` and abort `not_jung`.
5. Optional, decision M10: entry unique id = mesh UUID with a minor-version migration; discovery matched through key
   material. Leave out unless the maintainer agreed.

## Tests to add

- Discovery of a configured entry's followed-refresh Network ID → abort.
- Ignored entry + key refresh → no collision log, ignored entry removed.
- Stale export → `export_keys_stale` in the flow and at setup.
- Non-JUNG advert → abort (manifest-matcher variant: a hassfest-style manifest test).

## Acceptance criteria

Gates green; hassfest in CI passes after the manifest change.

## Verifiable on air here?

Partly. First, the probe: in HA's Bluetooth advertisement monitor (or `tools/mesh_poc.py scan`), check whether a
JUNG proxy's record carries manufacturer data 0x0527. Discovery of the configured mesh must abort silently. The
key-refresh paths cannot be produced here.

## Risks / off-by-default / "unverified on air"

A too-narrow matcher hides the real mesh — never ship it without the probe. Loading CDBs of non-loaded entries
during discovery adds executor work: cache it.

## Depends on

04 (`_on_key_refresh` shape). Decision M10. Brief 50 edits `manifest.json` / `config_flow.py` after it.

## Files touched

`config_flow.py`, `coordinator.py`, `__init__.py`, `manifest.json`, `strings.json`, `translations/en.json`,
`tests/test_config_flow.py`, `tests/test_init.py`, `tests/test_key_refresh.py`, `CHANGELOG.md`,
`docs/ha-integration.md`.
