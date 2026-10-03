# 41 — Audit and locator extras, gateway approve, read-only firmware entity

Phase P2 · Wave 10 · Size M · Closes: F4-15, F4-17 (approve action only), F4-18 cheap half / U4-11.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

`audit_network` also checks the keys each node holds; a node can be made to advertise its identity to locate it; an
admin can approve a pending gateway API client from HA; each node shows a read-only firmware `update` entity that
compares its version with the one the JUNG app bundles.

## Background

The integration (`custom_components/junghome_ble`, library `jhmesh`) audits publications and subscriptions
(`jhmesh/audit.py`, action `audit_network`). Missing (F4-15): AppKey Get / List (`msg:op:8001/8002`), NetKey Get /
List (`8042/8043`), Node Identity Get / Set (`8046–8048`), Friend Get (`800f/8011`), Remove Addresses From Filter
(`msg:proxy:02`), the filter list size, a *Switch proxy* button. F4-17: the gateway's REST approve of a pending API
client (`net:http:post-config:permissionsdto`) is useful when the gateway integration runs next to this one;
`resetpermissionsdto` and IP settings are left out on purpose. F4-18 / U4-11: the device page shows the software
version (`entity.py:145-149`) but nothing compares it; the versions the app bundles per product id are listed in
`docs/android/firmware-products.md`; every node here already runs those versions.

## Read first

`jhmesh/audit.py`, `jhmesh/config_messages.py` (AppKey / NetKey / Node Identity builders), `services.py`
`audit_network`, `gateway_api.py:236-440`, `gateway_status.py`, `entity.py:140-150`,
`docs/android/firmware-products.md`, HA's `update` platform docs.

## Steps

1. Library: AppKey Get / NetKey Get builders and decoders (if missing), Node Identity Get / Set, Friend Get.
2. `audit_network` adds "keys held vs export" per node (indexes only, never key values).
3. Admin action `locate_node` (Node Identity Set on, then off after a timeout) — optional, behind the same admin gate.
4. Gateway: admin action `approve_gateway_client` (lists pending clients by name, approves one). Never offer
   reset-permissions or network settings.
5. New `update.py` platform: one `UpdateEntity` per node with `installed_version` from the node's version and
   `latest_version` from a table built from `firmware-products.md`; no install feature; release summary says "update
   with the JUNG HOME app"; disabled by default (decision with M9 style defaults).
6. Ledger rows for the messages above.

## Tests to add

Builders / decoders byte-exact (`tests/jhmesh`); audit result with a node missing an AppKey; approve action with the
fake gateway API (`tests/test_gateway_api.py` patterns), including refusal for non-admins; update entity states for an
equal, older and unknown version; snapshot update for the new platform.

## Acceptance criteria

All gates green; the new platform listed in `manifest.json` platforms / `PLATFORMS`; ledger rows updated.

## Verifiable on air here?

Partly: audit Gets and the locator on any light or socket (read-only / reversible); approve with the gateway (an API
client request from another tool or the gateway integration); the update entity read-only.

## Risks / off-by-default / "unverified on air"

Approving a client grants it full gateway access: admin-only, explicit. Node Identity left on drains nothing on mains
nodes but must be switched off after the timeout.

## Depends on

None.

## Files touched

`jhmesh/audit.py`, `jhmesh/config_messages.py`, `gateway_api.py`, `services.py`, `services.yaml`, `icons.json`, new
`update.py`, `const.py` (`PLATFORMS`), `strings.json`, `translations/en.json`, `docs/parity/ledger-msg.json`,
`docs/parity/ledger-net.json`, `docs/ha-integration.md`, `CHANGELOG.md`, tests, `tests/snapshots/`.
