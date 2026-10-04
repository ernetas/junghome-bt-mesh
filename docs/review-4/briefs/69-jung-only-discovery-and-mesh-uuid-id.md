# 69 — JUNG-only discovery and the mesh UUID as the entry's unique id

Phase P3 · Wave 18 · Size M · Closes: H I-7 (JUNG-only matcher), H I-9 (unique id = mesh UUID); decision M10.

Follow the [conventions](README.md#conventions) in full.

## Goal

The Bluetooth discovery card appears for JUNG mesh proxies only, and an entry's unique id is its mesh UUID, which a
key refresh never changes.

## Background

Brief 24 left both behind decision M10, which is taken: yes to both. The probe brief 24 asked for is done: in Home
Assistant's stored Bluetooth advertisements of the installation, 28 of the 29 JUNG proxy nodes of the export carry
manufacturer data with company id 0x0527 (1319) next to the Mesh Proxy service; one node's stored advertisement had
none, so a matcher on the manufacturer id finds the mesh through any of the others. Today the manifest matches every
Mesh Proxy (`00001828-…`), and the entry's unique id is the Network ID (`network_id.hex()`), which a key refresh
changes: `coordinator.py` moves it with `async_update_entry(unique_id=…)` and discovery dedups on it.

## Read first

`manifest.json` (`bluetooth`), `config_flow.py` (`async_step_bluetooth`, the user / gateway / reconfigure steps,
`async_set_unique_id` calls, the migration helpers and `VERSION` / `MINOR_VERSION`), `__init__.py`
(`async_migrate_entry`), `coordinator.py` (`_on_key_refresh` and the unique-id move), brief 24 and its tests,
`jhmesh/advert.py` (`JUNG_COMPANY_ID`), `tests/test_config_flow.py`, `tests/test_init.py` / migration tests.

## Steps

1. Manifest matcher: the Mesh Proxy service UUID **and** `"manufacturer_id": 1319` in one matcher. Keep
   `async_step_bluetooth`'s own check too: an advert without JUNG manufacturer data aborts `not_jung` (a translated
   abort reason, in every translation file as English until translated).
2. Unique id = the mesh UUID (lower-case hex, as the export holds it, without dashes or with them — pick the export's
   form and say which) for every entry set up from a file or a gateway export. A gateway entry's id stays as it is
   if it is not the Network ID today (check `gateway-{serial}`; leave it unless it is the Network ID).
3. A minor-version entry migration: read the mesh UUID from the entry's export and set it as the unique id; an export
   that cannot be read leaves the entry as it is and retries at the next start (no failed setup). Two entries of one
   mesh (should not exist) keep their ids and log once.
4. Discovery: a proxy advert matches a configured entry by the Network ID or Node Identity of any configured entry's
   keys (current and, during a refresh, the new ones), not by the unique id; it aborts `already_configured` silently,
   as today. Set the flow's unique id from the export's mesh UUID once the user has given the export.
5. `_on_key_refresh`: no unique-id move any more (the mesh UUID does not change); keep what else it does.
6. CHANGELOG under `## 1.2.0 (unreleased)` (create the section above `## 1.1.0` if it is missing; never edit
   released sections): *Upgrading* — the entry's unique id becomes the mesh UUID by itself at the first start; the
   discovery card only for JUNG proxies. Docs: `docs/ha-integration.md` discovery section, the plan's Status.

## Tests to add

Matcher: a hassfest-style test that the manifest matcher needs both the service UUID and 0x0527; a non-JUNG advert
aborts `not_jung`; a JUNG advert of a configured mesh aborts silently, also across a key refresh. Migration: an entry
with a Network-ID unique id migrates to the mesh UUID; an unreadable export leaves it and setup still succeeds; the
version bumps. Key refresh no longer changes the unique id. New entries get the mesh UUID.

## Acceptance criteria

Gates green; hassfest in CI passes; snapshots change only where the unique id is shown, reviewed.

## Verifiable on air here?

The probe is done (above). After the merge: the existing entry migrates at the first start (check the entry's
unique id in `core.config_entries`, read only), and no discovery card appears for the configured mesh.

## Risks / off-by-default / "unverified on air"

A matcher too narrow hides a mesh whose proxies carry no JUNG data; the probe showed they do. The migration must
never fail setup.

## Depends on

24, 60. Decision M10.

## Files touched

`manifest.json`, `config_flow.py`, `__init__.py` (migration), `coordinator.py`, `strings.json` and
`translations/*.json` (`not_jung`), tests, `CHANGELOG.md`, `docs/ha-integration.md`.
