# 23 — Gateway reauthentication flow

Phase P1 · Wave 5 · Size M · Closes: the quality-scale `reauthentication-flow` gap — H I-2, the reauth part of U4-5.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

When the JUNG HOME Gateway rejects the stored API token, HA starts its standard reauth flow; the user gets a new
token by the gateway password or by approving HA in the app; the token is stored and `gateway_token_rejected`
cleared; the mesh keeps working throughout.

## Background

`custom_components/junghome_ble` (HA integration) uses the gateway's REST API with a token for export fetch, upload
and status. A revoked token today only raises the `gateway_token_rejected` issue (`mesh_config.py:1159-1179`,
`gateway_status.py:95-135`) and the user must Reconfigure. `quality_scale.yaml` keeps `reauthentication-flow: todo`,
the one open Silver rule besides the Bronze ones.

## Read first

- `config_flow.py`: gateway steps (~605-726), `_async_pin` (~786-823), `_async_gateway_submit` (~898-945), the
  reconfigure refetch path.
- `mesh_config.py:1159-1179` (`report_token_rejected`), `gateway_status.py:95-135`, `gateway_api.py:236-440`.
- `quality_scale.yaml` (reauth rule and the top note), HA's config-flow reauth docs.
- `tests/test_config_flow.py`, `tests/test_gateway_status.py`, `tests/test_mesh_config.py` (token-rejected cases).

## Steps

1. Where the token is rejected (configurator and status poll), also call `entry.async_start_reauth(hass)` once per
   outage. Do not raise `ConfigEntryAuthFailed`: the mesh must stay up.
2. `async_step_reauth(entry_data)` → `async_step_reauth_confirm` showing the host; optional gateway password field.
   With a password → `register_by_password`; without → the existing approval progress step. Pin through
   `_async_pin` (the entry's fingerprint is trusted; the mesh vouches when the hub is loaded).
3. Factor the refetch / registration helpers so they work with `_get_reconfigure_entry()` or `_get_reauth_entry()`.
4. On success update the token with `async_update_entry` (no reload: `hub_data` ignores the token) and abort with
   `reauth_successful`; delete `gateway_token_rejected`. Keep the issue but reword it to point at the reauth
   notification.
5. Strings: `config.step.reauth_confirm` (title, description with `{host}`, data, data_description),
   `config.abort.reauth_successful`, reworded issue — in both files.
6. `quality_scale.yaml`: set `reauthentication-flow: done` with an honest comment, and change only the
   reauthentication sentence of the top note (`brands` is already done; touch nothing else in the file). Update the
   troubleshooting section ("no longer accepts Home Assistant").

## Tests to add

- A 401 from the status poll or the export fetch starts exactly one reauth flow.
- Password path: success and wrong password (`invalid_auth` on the field).
- Approval path through the progress step; timeout.
- Certificate mismatch during reauth (the certificate step, or the abort when the mesh vouched).
- Issue cleared after success; the entry is not reloaded; no entity becomes unavailable.

## Acceptance criteria

Gates green; a reauth card appears when the gateway rejects the token; the password never appears in logs.

## Verifiable on air here?

Yes, with the gateway, person needed: remove HA's access in the app (gateway access permissions), enable a gateway
diagnostic entity so the status poll runs, wait for the reauth card, re-approve or enter the password.

## Risks / off-by-default / "unverified on air"

Two starters at once (poll and configurator) — HA de-duplicates in-progress reauth flows. Never log the password or
token.

## Depends on

None. Brief 44 (repairs that fix) builds on it.

## Files touched

`config_flow.py`, `mesh_config.py`, `gateway_status.py`, `quality_scale.yaml`, `strings.json`,
`translations/en.json`, `tests/test_config_flow.py`, `tests/test_gateway_status.py`, `tests/test_mesh_config.py`,
`CHANGELOG.md`, `docs/ha-integration.md`.
