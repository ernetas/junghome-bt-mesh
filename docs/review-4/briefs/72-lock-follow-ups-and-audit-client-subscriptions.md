# 72 — Lock follow-ups from the sweep; client subscriptions in the audit

Phase P2 · Wave 22 · Size S–M · Closes: the on-air sweep's follow-ups 2–4 (`docs/on-air-sweep.md` results C3, A8).

Follow the [conventions](README.md#conventions) in full.

## Goal

What the sweep found about locked loads is used fully, and the audit names the key elements' client subscriptions
for what they are.

## Background

Seen on air (`docs/hidden-features.md` §12, `docs/on-air-sweep.md` results C3): a locked load publishes an LBC *User*
Property Status of `0x0009` to its element group when locked and on every Set it refuses; while locked it answers an
OnOff or Lightness Set with a Status of its unchanged state. The second pass of brief 30 left three follow-ups:

- A Lightness Set to a locked light that is already on gets its old level back, which Home Assistant counts as a
  success (only on / off is compared); that only matters when the lock publication was missed.
- A lock heard from the group publication still triggers one Get before a refusal is reported, because only an
  answered Get counts as fresh.
- The audit (A8) found a light node's key element's Light Lightness Client and Light CTL Client (`1302`, `1305`)
  subscribed to the element group the export does not list there; `audit_network` reports that as
  `subscriptions_extra`, like a real stray subscription. Besides them, the known phantom Scene Server / Scene Setup
  Server entries have their own kind (`docs/hidden-features.md` §9).

## Read first

`config_entities.py` (`LockFunctionEntity`, `LoadLock`), `element_state.py` (`ElementState.lock` and how freshness
is kept), `coordinator.py` / `hub/` (where a vendor Status lands, the refusal path after a Set), `light.py`,
`jhmesh/audit.py` (finding kinds, the phantom-scene kind), `docs/hidden-features.md` §9 and §12, the tests added by
the second pass (`tests/test_reachability.py::test_a_lock_the_load_publishes_is_known_at_once`,
`test_a_locked_light_refusing_a_lightness_set_as_on_air`, `tests/test_switch.py::test_unlock_never_sends_priority_0`).

## Steps

1. A Lightness (and CTL lightness) Set is confirmed only when the answered present level is within `STATE_STEP` of
   the target; an answer with the old level (no transition reported) is a refusal, and the existing lock read and
   lock-refusal error follow. A real transition (target and remaining time in the Status) still counts as accepted.
2. A lock state learned from the load's own publication counts as fresh for the refusal path (no extra Get); keep the
   Get when nothing was heard.
3. `jhmesh/audit.py`: a separate, benign finding kind for client models (Generic OnOff / Level / Lightness / CTL
   clients and the like) of a key element subscribed to the element group of the load they drive, reported apart from
   `subscriptions_extra` and not counted as a problem; the HA side (`audit_network`'s response and diagnostics) shows
   it. Document it in `docs/hidden-features.md` §9 next to the phantom scene entries.
4. Docs where these behaviours are described; CHANGELOG under `## 1.3.0 (unreleased)` (create above `## 1.2.0` if
   missing; never edit released sections). New user-visible strings in `strings.json`, `en.json` and every
   translation.

## Tests to add

Step 1: locked light already on, Lightness Set answered with the old level → refusal, the lock read follows; a fade
answer → accepted. Step 2: publication heard, then a refused Set → no Get, error at once. Step 3: audit of a node
with the client subscriptions → the new kind, not `subscriptions_extra`; a real stray subscription still is.
Bytes as on air, re-encoded with fixture keys.

## Acceptance criteria

Gates green; snapshots change only where the audit's output gains the new kind.

## Verifiable on air here?

Yes (CLI, as in sweep C3); unverified on air until rerun.
