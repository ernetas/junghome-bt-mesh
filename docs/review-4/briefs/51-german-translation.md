# 51 — German translation

Phase P3 · Wave 14 · Size M–L · Closes: U4-1 (U4 F1), U4-16 (translated logbook and model names); decision M4.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

A `translations/de.json` a German-speaking JUNG HOME owner recognises — the German app's terms — plus a test that keeps
every non-English file consistent with `en.json`, and logbook / model names that follow HA's language.

## Background

The integration (`custom_components/junghome_ble`) ships only `translations/en.json` (about 780 strings: services,
exceptions, config, issues, entity). JUNG's home market speaks German and the app ships German, French, Spanish and
Italian. HA falls back to English per missing key, so a partial `de.json` already helps. `logbook.py:40-89` and the
model names in `entity.py:78-316` are hard-coded English (U4-16).

## Read first

`translations/en.json`, `strings.json`, `tests/test_translations.py`, `logbook.py`, `entity.py:78-316`, HA's German UI
wording for settings paths (*Einstellungen → Geräte & Dienste*).

## Steps

1. Glossary (scratch only, not committed): the German app strings live in the main checkout's git-ignored
   `android/` (the German config split's `resources.arsc`, or the English
   `android/jadx-out/resources/res/values/strings.xml` matched by resource name). Read them from there for
   **terminology only** (parameter names, LED colours, modes, menu paths); never copy the files or long passages into
   the worktree; install nothing from the network.
2. Translate in this order, stopping at a consistent boundary if time runs out: `entity`, `config`, `options`,
   `device_automation`, `selector`, then `issues`, `exceptions`, `services`. Keep every `{placeholder}` and every
   backtick literal unchanged. Use the form of address and terms decision M4 settles (recommendation: "du", as HA and
   the app).
3. `de.json` mirrors the English structure for the keys it has, contains no key `en.json` lacks, no `[%key:…%]`.
4. `tests/test_translations.py`: a parametrised test over `translations/*.json` other than `en.json` — valid JSON,
   every leaf path exists in English, placeholder sets equal per key, no `[%key:`; print (not fail) the translated
   share per top-level section.
5. U4-16: `logbook.py` and the model names read the cached translations for `hass.config.language` (move the English
   texts into `strings.json` under suitable keys first), falling back to English.

## Tests to add

The parity test above; logbook descriptions in German with `hass.config.language = "de"`; model names translated.

## Acceptance criteria

All gates green; `de.json` complete for `entity`, `config`, `options`, `device_automation`, `selector`; the report lists
the glossary judgement calls (*Taster* / *Wippe*, *Raum* / *Bereich*).

## Verifiable on air here?

Local only: switch the HA user language to German and look at the integration's pages.

## Risks / off-by-default / "unverified on air"

Keys added later by other briefs fall back to English; rerun the parity test after merges. No vendor text beyond short
terms.

## Depends on

Decision M4; brief 47 (*soft*, its strings).

## Files touched

New `custom_components/junghome_ble/translations/de.json`, `tests/test_translations.py`, `logbook.py`, `entity.py`,
`strings.json`, `translations/en.json`, `docs/user/`, `CHANGELOG.md`.
