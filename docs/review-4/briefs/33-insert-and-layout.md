# 33 — Insert function and button layout from the JUNG advertisement

Phase P2 · Wave 8 · Size S–M · Closes: — (F4-12; parity rows `prop:0x0002`, `prop:0x5001`,
`prod:actuator-function:*`, `prod:insert-type:*`, `net:uc:observecontrolswitchkeyassignment`,
`mgmt:flow:checkformissingdevices`).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Each node's device shows its insert ("DALI insert", "switch insert") and button layout; a node HA adds gets the right
load class without the app's cached export; a mismatch between the export's cached InsertId and the node's advert is
reported.

## Background

`custom_components/junghome_ble` (HA integration). JUNG nodes advertise a type-3 manufacturer record carrying product
id, actuator function and button layout (`docs/hidden-features.md` §8, decoder in `jhmesh/advert.py`). HA classifies
loads from the export (`jhmesh/cdb.py` `_note_insert_functions`, `jhmesh/devices.py` `insert_function`) and never
reads InsertId from a node (the ledger's `prod:insert-type:*` rows claim more; brief 07 corrects them).

## Read first

- `jhmesh/advert.py`; `coordinator.py:1395-1460` (advert history per MAC); `jhmesh/cdb.py:688-710`;
  `jhmesh/devices.py:56-74`, `:621-640`; `jhmesh/properties.py:848-880` (`ACTUATOR_FUNCTION`); `entity.py`
  device-info builders; `docs/hidden-features.md` §8.

## Steps

1. Keep the latest decoded advert per node (by its MAC → node mapping).
2. Device info: `model` / `hw_version` with the translated insert name; layout → key naming (top / bottom, gang).
3. `insert_function`: fall back to the advert when the export has none; read-only fallback Gets (LBC Manufacturer Get
   InsertId `0x0002`, Admin Get ButtonLayout `0x5001`) only when neither is known.
4. Repair `insert_mismatch` when the advert disagrees with the export's InsertId (an insert was swapped: re-export).
5. After `add_device`, check the number of devices against the product table (the app's missing-devices check).
6. Ledger rows updated.

## Tests to add

Advert → device info; fallback order (export, advert, Get); mismatch repair raised and cleared; missing-device check;
snapshot diff limited to device model / hw_version.

## Acceptance criteria

Gates green; snapshot diff reviewed and explained.

## Verifiable on air here?

Yes, read-only: push-buttons with the three insert types, mini actuators, sockets. First compare
`tools/mesh_poc.py scan` adverts with the export's InsertIds.

## Risks / off-by-default / "unverified on air"

None for the mesh (read-only). Device model text changes are user-visible: translate them.

## Depends on

None. Briefs 39 and 40 reuse the advert data.

## Files touched

`coordinator.py`, `jhmesh/cdb.py`, `jhmesh/devices.py`, `jhmesh/advert.py`, `entity.py`, `repairs.py` (issue only,
not a fix flow), `strings.json`, `translations/en.json`, `docs/parity/ledger-*.json`, `tests/test_coordinator.py`,
`tests/jhmesh/test_devices.py`, `tests/snapshots/test_snapshots.ambr`, `CHANGELOG.md`, `docs/ha-integration.md`.
