# 74 — Housekeeping: localised `{applied}`, typed `hass.data`, Renovate's pre-commit manager, a flaky test

Phase P5 · Wave 22 · Size S–M · Closes: leftovers noted while landing waves 14b–21.

Follow the [conventions](README.md#conventions) in full.

## Goal

Four small leftovers, each its own commit.

## Steps

1. **`{applied}` in English.** The threshold and plan error messages in `strings.json` (`exceptions`, e.g. the node
   refused / did not answer / unreachable / asleep messages) end with an `{applied}` placeholder that the code fills
   with an English sentence about what was already written. Make that part translatable: either separate exception
   keys for "nothing applied" and "partly applied: …" (preferred if the number of variants is small), or a translated
   fragment looked up through the translation cache. The placeholder's list of what was applied (node / element names)
   may stay data. Update every `translations/*.json`, translated. Tests: every variant, in English and in one other
   language through the translation machinery.
2. **Typed `hass.data`.** Replace the remaining raw `hass.data[...]` keys of the integration with
   `homeassistant.util.hass_dict.HassKey` constants (the pattern the rest of the integration uses); no behaviour
   change.
3. **Renovate and pre-commit.** Enable Renovate's `pre-commit` manager in `renovate.json` (it is off by default) so
   the hooks' `rev` pins in `.pre-commit-config.yaml` are updated, under the same minimum-release-age rule as the
   other pins; describe it like the existing rules' `description` fields. Keep `renovate.json` valid against its
   schema (CI or a test validates it if one exists; add one if cheap).
4. **The APK-anchor parity test is intermittent under `pytest -n auto`** (about 2 failures in 13 full runs; never
   alone). Find the test (`tests/test_parity.py`, the check that ledger rows' APK anchors resolve), reproduce with
   `-n auto` and `-p randomly` seeds in a loop, find the shared state or order dependence, and fix the cause (not a
   retry). If it cannot be reproduced in 40 full runs, write down what was tried in the commit message and harden the
   obvious suspect (shared mutable module state, a cache, a temp path).

CHANGELOG under `## 1.3.0 (unreleased)` (create above `## 1.2.0` if missing; never edit released sections): step 1
under *Fixed* (messages fully translated); steps 2–4 under *Internal*.

## Acceptance criteria

Gates green; 10 consecutive full `-n auto` runs without a failure after step 4.

## Verifiable on air here?

No (local only).
