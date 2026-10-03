# 43 — User guide, reference split, generated entity reference

Phase P3 · Wave 11 (solo) · Size M–L · Closes: D29 (Q4-8), Q4-10, Q4-11; improvements U4-4, U4-10 (recipe), report 7
brief B5 (docs part), the doc defects in U4 F2, F4(c)(d), F9.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

A JUNG HOME owner finds "how do I…" answers without reading protocol detail; contributors get their own notes; the
entity reference cannot drift from `strings.json`.

## Background

The repository is a Home Assistant custom integration (`custom_components/junghome_ble`) with a bundled mesh library
(`jhmesh`). HACS renders `README.md`; `docs/ha-integration.md` (about 1800 lines) is user guide, entity reference,
troubleshooting and developer notes in one file.

- About a dozen entities appear nowhere in the docs: *Last seen*, *Signal strength*, *Last restart*, *Sequence numbers
  used*, *Mesh sequence numbers used*, *Switching cycles*, *Power-on cycles*, *Key mode {key}*, *Bluetooth mesh
  problem*; *Hops* only in prose (D29; defined around `sensor.py:1134-1170`, `:154`).
- `README.md:124-126` says real identifiers appear in the installation docs; since the scrub they are pseudonyms
  (Q4-10). The module table (`docs/ha-integration.md:1627-1650`) lacks `device_names.py`, `onboard.py`, `repairs.py`,
  `schedules.py`, `thresholds.py`; `README-pypi.md` omits `jhmesh.onboarding`; coverage and node-count numbers drift
  (Q4-11).
- Defects: `docs/ha-integration.md:621-622` says areas are applied "when you accept the suggestion" (HA applies them on
  its own, once); `:844` tells migrating users to use a `pushed_down` event no entity emits; `:941-962` leads with a
  state trigger plus template instead of the device trigger or `event.received`; *Actions: adding and removing
  devices* sits under *Troubleshooting*.

## Read first

`README.md`, `README-pypi.md`, `docs/ha-integration.md` (whole), `strings.json` `entity`, every platform's entity
descriptions (category, enabled default), `event.py:50` (event types), `device_trigger.py`, `manifest.json`
(`documentation`), `docs/parity/README.md`.

## Steps

1. **User guide** `docs/user/`: `README.md` (index), `getting-started.md` (adapter / ESPHome proxy placement, the three
   export sources incl. uploading the export from the phone through the HA companion app, what appears, areas),
   `everyday-use.md`, `buttons-and-automations.md` (what each wiring reports in plain words, device triggers,
   `event.received`, a placeholder link for brief 46's blueprints), `energy.md`, `changing-the-installation.md`,
   `maintenance.md` (one short section per repair, diagnostics, offline devices), `faq.md` (do I need the gateway,
   does this break the app, keep the gateway integration, why no rocker clicks, why a foreign mesh in Discovered, why
   three devices per switch, why Energy differs, what while HA is down, which Bluetooth hardware, what "unverified on
   air" means, is my export safe), `entities.md` (generated, step 3).
2. **Free-rocker recipe** (U4-10), marked **unverified on air**: `create_room` *Home Assistant*, then `assign_key`
   with the key and that room (mode `light`); the key then reports `press_on` / `press_off` / `dim`; how to undo.
3. `tools/gen_entity_reference.py`: generate `docs/user/entities.md` from `strings.json` and the entity descriptions
   (platform, translated name, category, enabled by default, which devices). `tests/test_docs_reference.py`
   regenerates it into `tmp_path` and compares with the committed file, and fails when a translated entity name is
   missing.
4. **Reference** `docs/ha-integration.md` stays at its path with an opening line pointing to the guide. Fix the defects
   above; move *Actions: adding and removing devices* out of *Troubleshooting*. **Keep every other heading text
   unchanged**: brief 44's `learn_more_url` anchors depend on them.
5. **Developer notes** to `docs/dev/` (architecture, module map with the missing modules, testing, release); leave a
   short section at the old place linking there. Research docs stay where they are; add `docs/research/README.md`
   as an index of them.
6. `README.md`: landing only (what, install, set up, security, disclaimer); fix Q4-10 and the numbers (derive or drop
   them); move the reverse-engineering log to `docs/research/README.md`. `README-pypi.md`: add `jhmesh.onboarding`.
7. German quick start `docs/de/schnellstart.md`: installation, the three sources with the German app menu names,
   first steps, help; links to the English guide.
8. `manifest.json` `documentation`: the user guide index.
9. `tests/test_docs.py`: every relative link and `#anchor` in `README.md`, `docs/user/**`, `docs/de/**`, `docs/dev/**`
   and `docs/ha-integration.md` resolves (GitHub slug rules, no network).

The first-release CHANGELOG rewrite is **not** part of this brief (brief 65).

## Tests to add

`tests/test_docs_reference.py`, `tests/test_docs.py`; the existing translations test.

## Acceptance criteria

All gates green; every translated entity name appears in `docs/user/entities.md`; no broken internal link or anchor;
hassfest still accepts `manifest.json`; a reader gets from HACS to a working rocker automation using `docs/user/`
only.

## Verifiable on air here?

Local only (documentation). The recipe stays "unverified on air" until the maintainer tries it on a spare key.

## Risks / off-by-default / "unverified on air"

External links to old anchors: keep headings and stubs. HACS renders `README.md` only: relative links must work on
GitHub. This brief runs alone in its wave because it restructures the file every other brief edits.

## Depends on

None. Briefs 44 (anchors), 46 (blueprint links) and 65 build on it.

## Files touched

`docs/**` (new `docs/user/`, `docs/dev/`, `docs/de/`, `docs/research/README.md`, `docs/ha-integration.md`),
`README.md`, `README-pypi.md`, `custom_components/junghome_ble/manifest.json` (`documentation`), new
`tools/gen_entity_reference.py`, new `tests/test_docs_reference.py`, new `tests/test_docs.py`, `CHANGELOG.md`.
