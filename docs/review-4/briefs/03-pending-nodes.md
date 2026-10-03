# 03 — Pending nodes: no address reuse, a way back

Phase P0 · Wave 1 · Size M · Closes: D2 (W4-1 = S4-2 = P4-4), D-low P4-6, D-low W4-11; folds in P I-7, W I8.

Follow the [conventions](README.md#conventions) in full.

## Goal

A node that `add_device` provisioned but did not record (commissioning or recording failed) never shares its unicast
block or element groups with a later node, HA's replay list does not drop a node newly placed at a remembered
address, provisioning uses an IV index learnt on the current link, the name is validated before anything
irreversible, and the user can reset a pending node from Home Assistant.

## Background

HA custom integration `custom_components/junghome_ble`; library `jhmesh`. `add_device` (admin action, behind the
provisioning option) provisions a JUNG device over PB-GATT, then commissions it from a template and records it in the
mesh export. The device key goes into the vault (`jhmesh/vault.py`) as *pending* until recorded.

- `onboard._place` (`onboard.py:109-131`) passes `avoid=[hub.proxy.state.src]` only; `free_unicast_block`
  (`jhmesh/onboarding.py:65`) sees the export, exclusions and `avoid`. `Vault.pending` (`vault.py:408-410`) is read only
  by tests; `merge_into` (`vault.py:526`) puts back recorded nodes only. `commission.plan` allocates element groups
  from the export only (`jhmesh/commission.py:417-442`).
- Scenario (reproduced by two reviewers): provision device A at the top block, commissioning refuses a step
  (`onboard.py:237-245`); the next `add_device` gets the same block and the same element groups. Two nodes then share
  a source address (nonce collision, replay drops mesh-wide, misattributed replies) and `hub.proxy.add_node` replaces A's
  device key, so A cannot be reset from HA. The error text (`strings.json` `add_device_commissioning_failed`) tells the
  user to remove it in the app, which never saw it.
- If A was reset first, the new node starts at SEQ 0 while HA's persisted RPL holds A's numbers: HA drops its replies.
- P4-6: provisioning takes `state.iv_index` (`onboard.py:187-194`) without requiring `iv_known` or a beacon on this link.
- W4-11: the name is not checked before provisioning (`services.py:327-333` is `cv.string`; `export.py:1478-1500`).

## Read first

`onboard.py` (whole); `jhmesh/onboarding.py:46-79`; `jhmesh/commission.py:400-445`; `jhmesh/vault.py:200-230`,
`:400-460`, `:526-560`; `jhmesh/client.py` (`LocalState.rpl`, `_beacon_seen`); `mesh_config.py:1502-1530`;
`device_names.py` (`check_name`); `services.py:320-335`, `:495-530`; `tests/test_onboard.py:299-372`;
`docs/ha-integration.md` "Home Assistant as a provisioner".

## Steps

1. Vault: `remember_provisioned(..., groups=[...])` stores the planned element groups; `from_dict` accepts entries
   without them (older vaults: warn, avoid nothing for groups).
2. `_place`: `avoid` = HA's address + every vault node's `unicast..unicast+elements-1` (pending and recorded).
3. `commission.plan(..., reserved_groups=…)` adds pending nodes' groups to the used set.
4. After `provision()` succeeds, delete `state.rpl` entries for the new addresses and persist (safe: HA assigned them).
5. Before provisioning require `hub.proxy.state.iv_known` and an authenticated beacon on the current link: add a
   public `ProxyClient.beacon_seen` property; otherwise raise a translated validation error.
6. Validate the name with the `device_names` rules (and the app's duplicate-suffix rule) before `establish_connection`.
7. New admin action `reset_pending_device` (uuid or unicast; registered only with the provisioning option): Config
   Node Reset with the vault key via a temporary `hub.proxy.add_node`, forget on confirmation or with `force`. Key the
   entry by UUID and require the unicast to match.
8. Repair issue `pending_device` while `vault.pending` is non-empty (names the address, never keys); new error text
   for a failed commissioning naming the address and the new action, and a distinct one when `record_node` fails
   ("configured but not recorded at {unicast}").
9. `services.yaml`, `icons.json`, strings and docs for the action and issue.

## Tests to add

- Library: `free_unicast_block` with a vault holding a pending node never returns that block; Hypothesis over random
  pending sets.
- HA: commissioning refused at step k, then a second `add_device` → different unicast and different element groups
  (this is the reviewers' repro: today both calls return the same block).
- RPL entries for the new addresses cleared after provisioning.
- `add_device` refused without a beacon on the link; refused with an invalid name before any provisioning traffic.
- `reset_pending_device`: answered, silent, forced; repair raised and cleared; vault round-trip with groups.

## Acceptance criteria

Gates green; a pending node's block and groups are never handed out again; `jhmesh` at 100 % line + branch.

## Verifiable on air here?

Partly. The reuse fix is offline only (a commissioning failure cannot be forced safely). `reset_pending_device` needs
a spare, unprovisioned JUNG device (a spare light or socket); lights, sockets and push-buttons already in the mesh
cannot exercise it.

## Risks / off-by-default / "unverified on air"

A pending node the user already factory-reset keeps its addresses reserved until `reset_pending_device --force`;
document it. A Node Reset to the wrong device if the vault entry is stale: key by UUID, match the unicast. The action
is "unverified on air" until run on a spare device.

## Depends on

None. Brief 10 and 17 build on it (same files).

## Files touched

`onboard.py`, `jhmesh/onboarding.py`, `jhmesh/commission.py`, `jhmesh/vault.py`, `jhmesh/client.py` (one property),
`services.py`, `services.yaml`, `icons.json`, `strings.json`, `translations/en.json`, `tests/test_onboard.py`,
`tests/jhmesh/` (onboarding, vault), `CHANGELOG.md`, `docs/ha-integration.md`.
