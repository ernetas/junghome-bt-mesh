# 22 — Sensor publication from the node's state, honest `remove_device`, small action fixes

Phase P1 · Wave 5 · Size M · Closes: D19 (W4-6), D20 (W4-7), W4-10, W4-12, W4-13 — folds in W I5 (fail fast on
unreachable nodes).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Where an entity shows the node's state, writes act on that state; `remove_device` tells a reset node from an absent
one; renames do not stall a reload; `set_room` no longer creates rooms by typo; threshold errors say what was
written; plans to unreachable nodes fail at once.

## Background

`custom_components/junghome_ble` (HA integration) rewires JUNG HOME mesh nodes through `MeshConfigurator`
(`mesh_config.py`), planning from the app's export and recording accepted steps back into it.

- **W4-6.** The *Sensor values for IoT systems* switch shows the node's answer (`switch.py:613-618`) but
  `set_sensor_publication` skips an element whose *export* publication already equals the wanted one
  (`mesh_config.py:2721-2723`): when the app changed it, the switch cannot change it back.
- **W4-7.** `remove_node` (`mesh_config.py:1550-1561`) maps a Node Reset timeout to `remove_device_no_answer`
  ("did not confirm its reset; nothing was changed", `strings.json` ~1207), although a node that took the reset and
  lost its status (likely when it is the current proxy, `hub.proxy_node`) is already gone.
- **W4-10.** A rename that adopted the gateway's export reloads from inside an entry task (`device_names.py:100-118`,
  `services.py:829-830`); HA waits 10 s for that task at unload.
- **W4-12.** `set_room` with an unknown room name creates the room and moves the loads (`mesh_config.py:2436-2446`).
- **W4-13.** `delete_threshold` / multi-socket `set_threshold` failures say "nothing before it was applied"
  although thresholds or sockets were already written (`services.py:1520-1525`, `:1551-1554`).
- **W I5.** A plan to an unreachable node stops only after `CONFIG_TIMEOUT × CONFIG_RETRIES` per step
  (`mesh_config.py:222-223`); the hub already tracks reachability.

## Read first

- `switch.py:585-690`, `mesh_config.py:2698-2739`, `:1532-1602`, `:2419-2470`, `:2066-2121` (`_send`, after brief 05).
- `onboard.py:81-98` (`unprovisioned_devices`), `coordinator.py` `proxy_node`, `unreachable`.
- `device_names.py`, `services.py` `_run` (~803-830) and the threshold handlers (~1444-1558).
- `tests/test_sensor_publication.py`, `tests/test_mesh_config.py`, `tests/test_services.py`, `tests/test_thresholds.py`.

## Steps

1. `set_sensor_publication(unicast, on, live=None)`: when `live` differs from `on`, send the Publication Set even if
   the export agrees, and record it; the switch passes its last read value.
2. `remove_node`: on timeout, scan briefly for the node's UUID among unprovisioned adverts; found → proceed as
   confirmed; not found → a new error "may have been reset; use `force` if it is gone". Refuse removing
   `hub.proxy_node` while it carries the link unless `force` (or reconnect through another proxy first).
3. Rename: run it with `hass.async_create_background_task` (not tied to the entry), or schedule the reload with
   `hass.config_entries.async_schedule_reload`.
4. `set_room`: add `create: false` (default); an unknown room without it raises `service_no_room`.
5. Thresholds: pass an `applied` text naming thresholds written and sockets finished, as `applied_members` does.
6. Before `_send`, refuse a plan whose nodes are marked unreachable (translated error naming them), or send their
   steps last; battery nodes keep the keep-awake path.
7. Update `services.yaml` (the `create` field), strings in both files, docs, CHANGELOG (the `set_room` change is a
   behaviour change).

## Tests to add

- Export off, node on → turning the switch off sends one Publication Set and records it.
- Removal: timeout + unprovisioned advert → recorded as removed; timeout without → new error text; proxy-node guard.
- Rename that adopts → no reload from inside an entry task (assert via the background-task path).
- `set_room` unknown room without `create` → refused; with it → created.
- Threshold failure after partial writes → the error names what was written.
- Unreachable node → refused without a send.

## Acceptance criteria

Gates green; services tests updated where the error text changed.

## Verifiable on air here?

Partly. The publication switch: yes, on a metering socket (reversible). Fail-fast: pull a breaker on one light and
run `set_room`. `remove_device` only with a spare device; the proxy-node guard can be checked without removing.

## Risks / off-by-default / "unverified on air"

The advert check depends on scanner coverage — absence proves nothing, keep `force`. `create: false` breaks
automations that relied on implicit creation (CHANGELOG).

## Depends on

05 (`_send` shape). Brief 49 builds dry runs on top.

## Files touched

`switch.py`, `mesh_config.py`, `services.py`, `services.yaml`, `device_names.py`, `strings.json`,
`translations/en.json`, `tests/test_sensor_publication.py`, `tests/test_mesh_config.py`, `tests/test_services.py`,
`tests/test_thresholds.py`, `CHANGELOG.md`, `docs/ha-integration.md`.
