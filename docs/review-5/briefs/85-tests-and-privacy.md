# 85 — Tests and privacy: settle, busy loops, history scan, host names, citations

Review 5 · Wave 24 · Closes: Q5-1, Q5-2, Q5-3, Q5-4 (scan part), Q5-5, Q5-6, Q5-7, A5-4.

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

The test helpers fail loudly instead of passing quietly, and the privacy checks cover the history's content.

## Steps

1. Q5-1: `settle` waits for the hub's own work, not an idle loop with timers pending. Q5-2: an exhausted turn cap
   fails unless the test is marked; the IV beacon loop under `fast_sleep` stops spinning.
2. Q5-3: `.gitignore` covers `.coverage.*`; the in-suite scan tolerates files vanishing.
3. Q5-4: `tools/privacy_scan.py --history` scans the added lines of every commit (and tags) with `scan_text`,
   counting per kind, never printing values; a test with a fixture repository. The rewrite itself is decision M16.
4. Q5-5: the host names go; a scanner pattern for a half MAC next to a host-name or Bluetooth context, and a
   12-hex-digit MAC without separators next to `mac` / `address=`.
5. Q5-6: a Renovate rule holds `python-version` in every workflow job.
6. Q5-7: the real-time negative check rewritten on the virtual clock; `docs/dev/testing.md` true again.
7. A5-4: the review / brief citations go from docstrings and comments; a test keeps them out (an allowlist for the
   deliberate runtime string).
8. Improvements: `package_ha.sh` packs tracked files only; the nightly states the HA version it installed.

## Tests to add

Each check proven by a failing fixture first.

## Files touched

`tests/conftest.py`, `.gitignore`, `tools/privacy_scan.py`, `renovate.json`, `docs/dev/testing.md`, docstrings
