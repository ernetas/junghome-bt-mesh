# 50 — Zeroconf gateway, unicast under Advanced, icons and string nits

Phase P3 · Wave 14 · Size S–M · Closes: U4-8 (U4 F5, F10 icons), H4-9.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

A gateway owner sees "JUNG HOME Gateway found" and clicks through; nobody needs to understand *unicast address* to set
up; every action has an icon; the small flow nits are gone.

## Background

The integration (`custom_components/junghome_ble`) discovers Bluetooth mesh proxies only; the gateway advertises
`_junghome._tcp` over mDNS with a `serial=` TXT record (`docs/roadmap.md:129`), but `manifest.json` has no `zeroconf`
and the user types `junghome.local` or an IP. *Our unicast address* is a required field on path, upload, gateway and
refetch forms (`config_flow.py:175-181`, `:192`, `:203`, `:722`). H4-9: `icons.json` covers 16 of 28 actions (missing
`set_room`, `create_room`, `rename_room`, `delete_room`, `assign_key`, `clear_key`, `create_scene`, `rename_scene`,
`store_scene`, `remove_from_scene`, `delete_scene`, `sync_gateway`, plus any added since); `config.error.
certificate_changed` is unused; `async_step_bluetooth_confirm` (`config_flow.py:496-506`) lacks
`_set_confirm_only()`; `GatewayBusy` (HTTP 429) maps to the generic `gateway_error` (`config_flow.py:280-289`).

## Read first

`config_flow.py` (whole), `manifest.json`, `icons.json`, `services.yaml`, `strings.json` `config`,
`tests/test_config_flow.py`, `tests/test_translations.py`, HA docs for `ZeroconfServiceInfo` and data-entry-flow
`section`.

## Steps

1. `manifest.json`: `"zeroconf": [{"type": "_junghome._tcp.local."}]` (keys sorted as hassfest expects).
2. `async_step_zeroconf`: host from `discovery_info.host`, serial from the TXT `serial`; abort `already_configured`
   when an entry has that gateway host; flow unique id `gateway-<serial>` so duplicates collapse (the entry's final
   unique id stays the Network ID, set later with `raise_on_progress=False`); `title_placeholders` with the host;
   `zeroconf_confirm` → the gateway step with the host prefilled. TLS pinning is unchanged; never trust the host.
3. Unicast in a collapsed `section("advanced")` on path, upload, gateway and refetch; read
   `user_input["advanced"][CONF_UNICAST]`; default stays `0D00`; strings under `config.step.<step>.sections.advanced`.
4. `bluetooth_confirm`: `_set_confirm_only()`; title "Is this your JUNG HOME installation?" and a description saying
   other brands' meshes may appear.
5. Icons for every action in `services.yaml` (e.g. `mdi:home-plus-outline` for `create_room`,
   `mdi:gesture-tap-button` for `assign_key`, `mdi:cloud-upload` for `sync_gateway`); extend `test_translations.py` to
   assert every service has an icon.
6. Remove `config.error.certificate_changed` if still unused; map `GatewayBusy` to a translated `gateway_busy` error.

## Tests to add

Zeroconf: new flow → confirm → gateway form prefilled; duplicate host aborts; two announcements make one flow. The
section field parsed in every source step, an invalid address reported on it. Confirm-only. `gateway_busy`. The icon
test.

## Acceptance criteria

All gates green; hassfest-relevant manifest keys valid; docs mention zeroconf discovery and the *Advanced* section.

## Verifiable on air here?

Yes: the gateway's mDNS announcement appears in HA's discovered list; finishing the flow is optional (the entry exists
already — it must abort `already_configured`).

## Risks / off-by-default / "unverified on air"

Low. A wrong zeroconf type only means no discovery. Behaviour for existing entries is unchanged.

## Depends on

Brief 24 (`manifest.json` matcher, `config_flow.py` bluetooth steps), brief 23 (refetch helper shared with reauth).

## Files touched

`manifest.json`, `config_flow.py`, `icons.json`, `strings.json`, `translations/en.json`, `docs/user/`,
`docs/ha-integration.md`, `CHANGELOG.md`, tests (`tests/test_config_flow.py`, `tests/test_translations.py`).
