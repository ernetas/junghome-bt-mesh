# 82 — Home Assistant: cached names, discovery suppression, deprecations, translations, options

Review 5 · Wave 24 · Closes: H5-1, H5-2, H5-3, H5-4, H5-5, U5-2, U5-5, U5-7.

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

The Home Assistant findings of review 5.

## Steps

1. H5-1: following an export in place also clears HA's `functools.cached_property` values of a kept entity.
2. H5-2: remove a device with the current device-registry API (no `remove_config_entry_id`).
3. H5-3: translate `start_iv_update` (16 keys) in all 25 languages; make `tests/test_translations.py` fail on a key
   missing from a translation instead of printing a share.
4. H5-4: `carry_over_conflict` deleted on unload and on removal.
5. H5-5: the diagnostics mask or redact every entry of `double_click_keys`, loaded or not.
6. U5-2: the gateway discovery card is suppressed by the gateway's identity (serial, or the mesh it serves), not only
   an exact host match.
7. U5-5: no connectable scanner at all gets its own setup error that names *active* ESPHome proxies.
8. U5-7: options that do not need a rebuild (`double_click_keys`, `click_delay`, …) apply without reloading the
   entry; the first save of an unchanged form reloads nothing.
9. Improvements: Home Assistant's own warnings (`frame` "Detected that custom integration", translation placeholder
   warnings) fail the tests; `download_export` works for an unloaded entry; `cached_text` guarded against format
   errors; `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)`; `sequence_space_low` fixable through
   `start_iv_update`'s guards; the options form hints (double-click keys hidden while `click_delay` is on).

## Tests to add

A regression test per finding.

## Files touched

`model_update.py`, `config_flow.py`, `__init__.py`, `diagnostics.py`, `hub/issues.py`, `coordinator.py`, strings
