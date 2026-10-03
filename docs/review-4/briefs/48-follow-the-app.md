# 48 — Follow the app automatically

Phase P3 · Wave 13 · Size M · Closes: U4-6 (U4 F7); decision M12.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

A rename, room change, scene edit or key rewiring made in the JUNG HOME app reaches HA without the owner running
Reconfigure. Gateway entries do it by themselves; file entries are told.

## Background

The integration (`custom_components/junghome_ble`) re-fetches the gateway's export only when a *new node* appears
(`coordinator.py:2113-2136`, `_refresh_export_from_gateway`); everything else waits for Reconfigure → fetch again
(`docs/ha-integration.md:1258-1259`). The app uploads its project to the gateway after each change; HA can see the
phone's traffic on the mesh (the blacklist filter forwards it; the phone is a source without a product id, or inside a
provisioner's unicast range — `coordinator.py:2317` already skips it). `mesh_config.py` has `adopt_for_unknown_nodes`
with the guards (`_gateway_state`, `_adopt`) since brief 18 made both-changed merges safe.

## Read first

`coordinator.py` (`_refresh_export_from_gateway`, `_reload_for_export`, the per-message handling around `:2317`),
`mesh_config.py` (`adopt_for_unknown_nodes`, `_gateway_state`, `_adopt`, `_upload`), `jhmesh/client.py` (whether
device-key Config messages from the phone reach the hub — they do for key-refresh following), `button.py`,
`gateway_api.py`, `tests/test_gateway_api.py`, `tests/test_coordinator.py` export-refresh tests.

## Steps

1. Detect app activity: an access message from the phone (not HA's own address, not a node). If Config *Set / Add /
   Delete* opcodes from the phone are visible, treat them as "changed"; otherwise any phone traffic as "maybe changed".
2. Gateway entries: after the phone has been quiet for `APP_QUIET_AFTER` (a few minutes), at most every
   `APP_SYNC_MIN_INTERVAL`, call a new public `adopt_if_gateway_changed()` next to `adopt_for_unknown_nodes`, sharing
   its guards. Adopted → reload (or brief 28's in-place apply) under the entry lock. Also every `GATEWAY_SYNC_PERIOD`
   (a few hours) and from a *Fetch from gateway* button (config category, gateway entries only).
3. Every existing guard holds: pin vouched for, no request while the token repair is open, both-changed handled as
   brief 18 defined.
4. File entries: only when Config changes were positively detected, raise `app_changed` (not fixable; brief 44's
   upload fix can be attached), cleared when a new export loads.
5. Docs: what triggers a sync, how often at most, that a reload makes entities briefly unavailable (unless brief 28
   landed), how to sync on demand.

## Tests to add

Fake link + fake gateway API: phone traffic then quiet → one fetch → adopt and reload; unchanged digest → no reload; a
burst → one fetch; the rate limit; the periodic path; the button; the guards (token issue open, certificate not
vouched, HA ahead); the file-entry issue raised and cleared.

## Acceptance criteria

All gates green; no new mesh transmissions; at most one gateway GET per `APP_SYNC_MIN_INTERVAL` from activity plus the
periodic one.

## Verifiable on air here?

Yes, with the gateway: rename a device in the app (person with the phone), wait, check HA picked it up without
Reconfigure.

## Risks / off-by-default / "unverified on air"

Extra gateway requests (decision M12). A misdetected phone source only costs a GET with an unchanged digest.

## Depends on

Brief 18 (merge and sync), brief 28 (*soft*), brief 44 (*soft*, upload fix for file entries).

## Files touched

`coordinator.py`, `mesh_config.py`, `button.py`, `const.py`, `strings.json`, `translations/en.json`, `docs/user/`,
`docs/ha-integration.md`, `CHANGELOG.md`, tests (`tests/test_coordinator.py`, `tests/test_mesh_config.py`,
`tests/test_button.py`).
