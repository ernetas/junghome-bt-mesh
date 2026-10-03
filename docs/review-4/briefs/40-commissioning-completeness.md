# 40 — Commissioning completeness, time keeper, OOB capabilities

Phase P2 · Wave 10 · Size M–L · Closes: F4-13, F4-14, P4-8 (low) with improvement P I-6.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

`add_device` commissions a node the way the app does — plan from the received composition, rule-based element groups,
relay / NetTx values, time, finish — cleans up a failed provisioning, offers a time keeper for projects with legacy
pucks, and uses the strongest provisioning method a device offers.

## Background

The integration (`custom_components/junghome_ble`, library `jhmesh`) provisions over PB-GATT
(`jhmesh/provisioning.py`) and commissions from a template (`jhmesh/commission.py`, `onboard.py`). Ledger rows still
open (F4-13): `mgmt:setup:settime`, `mgmt:setup:finishconfiguration`, `mgmt:cfgop:02` (plan from the received
composition, not a template), `mgmt:err:configuredeviceerror.nodenotconfigured`,
`mgmt:flow:configurecontrolswitchfunctionality`, `mgmt:flow:configuredetector`, `mgmt:flow:devicesetupprogress`,
`mgmt:setup:provisioningaborting`, `mgmt:flow:deviceprovisioning` (failure clean-up, 30 s budget),
`net:uc:connecttodevicetypegroup`, `net:uc:createelementconnectiongroups`, `mgmt:setup:setconfiguration`.
F4-14: Time Role Set (`msg:op:8239`) plus Time Server publication for legacy PP2 pucks (product ids `0x0010–0x0014`),
`mgmt:flow:ensuretimekeeper`. P4-8: provisioning always uses algorithm 0 with No OOB, whatever `Capabilities` offers
(`provisioning.py:310-315`, `:518-530`); an attacker in radio range during the window could MITM the ECDH exchange.

## Read first

`onboard.py` (whole), `jhmesh/commission.py`, `jhmesh/provisioning.py:300-600`, `jhmesh/vault.py`; the `mgmt` rows
above in `docs/parity/ledger-mgmt.json`; `docs/android/transport-provisioning.md`; Mesh Protocol 1.1 §5.4 (algorithms,
OOB); brief 33 (InsertId / layout data).

## Steps

1. Plan commissioning from the composition data received after provisioning (Composition Data Get page 0) rather than
   only the product template; keep the template as a cross-check and refuse on a mismatch.
2. Element groups by the app's rules (`net:uc:createelementconnectiongroups`), device-type groups
   (`connecttodevicetypegroup`), relay / NetTx copied from the app's values (`setconfiguration`), Time Set, the finish
   step.
3. Failure clean-up within a total budget: on an abort, Node Reset with the vault key and a translated error that names
   what happened (`nodenotconfigured`).
4. Progress reporting: optional response of `add_device` lists the steps done.
5. Time keeper: an expert switch (disabled by default) on a mains node that sends Time Role Set and wires Time Server
   publication; a repair when a project with PP2 pucks has none.
6. Capabilities: record the offered algorithms and OOB methods in the vault and diagnostics; prefer the HMAC-SHA256
   algorithm and Static OOB when offered (falls back to today's path); document the No-OOB risk in the `add_device`
   description.

## Tests to add

Commissioning plans from synthetic composition data (each product class in the fixtures); clean-up on abort (fake
link + fake provisioner); time-keeper switch messages; capability selection, including the fallback; the vault
records the capabilities (never a key).

## Acceptance criteria

All gates green; the `mgmt` ledger rows updated (implemented or partial with "on-air on a spare device" as missing).

## Verifiable on air here?

No: it needs a **spare unprovisioned JUNG device** (and someone at home to factory-reset it). No puck exists here, so
the time-keeper part stays "unverified on air".

## Risks / off-by-default / "unverified on air"

Medium: a half-configured node is recoverable only by Node Reset. Everything new is "unverified on air"; the time
keeper is off by default.

## Depends on

Briefs 03 (pending nodes), 17 (vault durability), 33 (insert and layout).

## Files touched

`jhmesh/commission.py`, `onboard.py`, `jhmesh/provisioning.py`, `jhmesh/vault.py`, `switch.py` (time keeper),
`repairs` / issue site in `coordinator.py`, `services.yaml`, `strings.json`, `translations/en.json`,
`docs/parity/ledger-mgmt.json`, `docs/ha-integration.md`, `CHANGELOG.md`, tests (`tests/test_onboard.py`,
`tests/jhmesh/test_commission.py`, `tests/jhmesh/test_provisioning.py`).
