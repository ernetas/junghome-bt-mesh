# 79 — IV Update follow-ups: confirmation, key refresh, a deadline, restored mid-update

Review 5 · Wave 24 · Closes: P5-1, P5-2, P5-3 = S5-3, S5-2.

Follow the [conventions](../../review-4/briefs/README.md#conventions) in full, with these additions: CHANGELOG bullets
go under `## 1.5.0 (unreleased)` (create it above the newest released section; never edit released sections); every
new "unverified on air" marker is cited in `docs/on-air-sweep.md`. The findings are summarised in
[`../plan.md`](../plan.md); the review reports with each finding's full evidence are handed over in the prompt.

## Goal

Home Assistant's own IV Update (brief 71) follows Mesh Protocol 1.1 §3.11.5 in every path: the 96 h are counted
from when the mesh took it, beacons carry the key refresh phase, an update the mesh never takes ends, and a restored
record cannot complete one early.

## Steps

1. P5-1: return to Normal Operation 96 h after the confirmation (the proxy's beacon back), not after the local start.
2. P5-2: beacons during the window are authenticated with the key of the current key-refresh phase and carry its
   Key Refresh flag (§3.10.3), the repeat and the completion beacon alike.
3. P5-3 / S5-3: an update not confirmed within the §3.11.5 bound (144 h) is abandoned: the state goes back to the
   index the mesh uses (nothing was sent under the new one, or say what was), a repair says the mesh did not take it,
   `sequence_space_low` is evaluated again; plus an `abort_iv_update` action (admin, confirm) and a visible state
   (sensor attribute) "waiting for the mesh since …".
4. S5-2: one rule that every IV-state move goes through (`apply_beacon`, `complete_iv_update`, `rewind_iv_index`,
   the abandon above) with the pending-guard check in it.
5. Improvements: record the mesh's last IV change seen in a beacon and use it in the `too_early` / `iv_unknown` texts;
   section labels say Mesh Protocol 1.1 numbers (§3.11.5 / §3.11.6), not Mesh Profile's; the heartbeat `hops`
   docstring off-by-one (§3.4.6.3).
6. Strings in every translation, translated; `docs/on-air-sweep.md` E6 updated.

## Tests to add

One initiator state-machine test (Hypothesis): the proxy refuses, takes it late, takes it at once, a key refresh
starts mid-update, Home Assistant restarts mid-update, the link is lost at the completion, a backup taken mid-update is
restored. Each P5 / S5 case as a regression test. `jhmesh` stays at 100 % branch coverage.

## Files touched

`jhmesh/state.py`, `jhmesh/client.py`, `actions/iv_update.py`, `hub/issues.py`, `sensor.py`, strings
