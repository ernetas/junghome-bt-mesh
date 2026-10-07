# 83 — Texts and docs: the integration's name, the download link, device names, stale docs

Review 5 · Wave 24 · Closes: U5-1, U5-3, U5-4, U5-6, U5-9, U5-10, U5-11, U5-12, F5-6, F5-7, F5-8.

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

What users read says what the integration does.

## Steps

1. U5-1: every text that sends users to *JUNG HOME* in *Devices & services* names *JUNG HOME Bluetooth Mesh*, in
   every language.
2. U5-3: the download texts say the link works for anyone who has it, for five minutes; the action answers an
   absolute URL (`get_url`) as well as the path.
3. U5-4: the FAQ's export-safety list is right for each setup kind (file on the host: no copy of its own).
4. U5-6: errors name devices by their Home Assistant device name (as the pre-flight error does), address in
   brackets.
5. U5-9: `download_export` warns (response field and docs) when *the JUNG HOME app changed the installation* is open.
6. U5-10, U5-11, U5-12, F5-6, F5-7, F5-8: the device-name format, the German quick start's rooms-and-areas step, PyPI
   (not published; say so), the CTL range docs, the roadmap's "where we are", the removal order (the app unwires
   first).
7. Improvement: the README's links absolute (HACS renders it), readable pre-flight addresses ("room Kitchen (C005)").

## Tests to add

`tests/test_docs.py` and the translation tests stay green; a test that no text names the old integration name.

## Files touched

`strings.json`, translations, `docs/`, `README.md`, `actions/download.py`, error raising sites
