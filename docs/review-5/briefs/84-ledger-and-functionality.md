# 84 — Ledger and functionality: stale rows, insert mismatch, transitions, run-on

Review 5 · Wave 24 · Closes: F5-1, F5-5, P5-4, F5-3, F5-4 (decision M19), F5-9.

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

The parity ledger matches the tree, and the sweep's settled facts are used.

## Steps

1. F5-1, F5-5: flip the stale rows (time keeper, the SAR ack timer and the others F5-5 lists); `tools/parity.py
   check` warns when a `gap` / `partial` row's `missing` names a symbol the tree now defines.
2. P5-4: the audit's client-model list: `1008` in, `1009` out.
3. F5-3: `insert_mismatch` becomes fixable: "use the advertised insert" sets a per-node override and reloads.
4. F5-9: *Switches off at* from the run-on time: an OnOff Status on with `0x1007` non-zero sets `off_at = seen +
   run-on`; restarted on every on, cleared on off.
5. M19: transitions as decided (brightness changes of DALI / dimmer loads only; scenes only when every member fades),
   built and off by default until a person has watched a fade; `docs/on-air-sweep.md` B8 updated.

## Tests to add

`tests/test_parity.py`, a regression test per item.

## Files touched

`docs/parity/`, `tools/parity.py`, `jhmesh/audit.py`, `inserts.py`, `repairs.py`, `sensor.py`, `light.py`, `scene.py`
