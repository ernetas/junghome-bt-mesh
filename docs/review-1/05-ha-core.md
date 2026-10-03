# HAC — coordinator, setup, migration, entity base

> Fixed and removed from this file: HAC-01, HAC-02, HAC-04, HAC-05, HAC-06, HAC-08, HAC-03, HAC-07, HAC-09, HAC-10, HAC-11, HAC-12, HAC-13. What changed and why is in `10-implementation-log.md`.

## Cross-shard notes
- MSG-06 confirmed. `coordinator._get_scene_actions_of` (coordinator.py:1228-1250) calls `proxy.request(..., V.SCENE_ACTION_SETUP_STATUS, retries=REFRESH_RETRIES)` with no `match` and then stores `status.action` under the *requested* `scene` without checking `status.scene`, although `decode_scene_action_status` returns the scene the status is about. A late reply to attempt 1 of scene N therefore resolves the waiter for scene N+1, and each later action shifts by one scene. The list Get can likewise be answered by a stale single-scene status and yield `scenes=None`. Fix sketch (for MSG-06): `match=lambda m: int.from_bytes(m.params[:2], "little") == scene` (use `V.SCENE_LIST` for the list Get), plus `if status.scene != scene: continue` as a guard.

## Shard summary

| Severity | Count | IDs |
|---|---|---|
| P0 | 0 | — |
| P1 | 3 | HAC-01, HAC-05, HAC-06 |
| P2 | 4 | HAC-02, HAC-03, HAC-04, HAC-07 |
| P3 | 6 | HAC-08 – HAC-13 |

The main theme is SEQ persistence. The per-mesh store that the previous fix pass introduced is correct as far as its key goes. What it still lacks is durability: no fsync (HAC-01) and no back-pressure when writes fail or lag (HAC-05). The legacy fold also only runs on a *successful* setup (HAC-06), and a superseded hub can still write to the shared store (HAC-02, HAC-04). HAC-05's fix (limit sends to what the *written* record makes a restart skip) also covers the IV-change and clean-start windows. It is the one to implement first; HAC-01 goes with it.

Checked and found clean:
- `migration.py`: planning is pure over the registries, and a second run finds nothing. Entities move while both entries are unloaded (`async_update_entity_platform` checks `entity_sources`). Gateway entries are disabled only after the moves, so moved entities escape the config-entry disable cascade. Ambiguous gateway devices are refused by `_one`.
- `entity.py`: every device identifier a platform creates is in `current_device_identifiers`, so `_remove_stale_devices` never prunes a live device, and `via_device_id` parents are registered first. Dispatcher subscriptions go through `async_on_remove`. The only issue is HAC-03's scoping.
- The Bluetooth API: a fresh `BLEDevice` from `async_ble_device_from_address` with a stale-info fallback; `establish_connection` with `BleakClientWithServiceCache`; the disconnected callback checks client identity; `attach` releases a half-attached client on any `BaseException`; the advertisement callback and every interval/call-later timer are removed in `async_stop`; per-link work is cancelled on disconnect.
- Threading: every bleak callback and notification runs on the event loop. Status-handler exceptions are contained by `ProxyClient._deliver`. `Store` serializes in the event loop by default, so `_snapshot` never races `rpl` mutation. Scheduled writes to one `Store` are ordered by its single `_data` slot.
- Setup and unload: `ConfigEntryNotReady` is raised before the HAState exists, so retries burn no SEQ. The update listener is registered only on success and removed by unload. `async_unload_entry` leaves the hub to its on-unload job, which HA awaits for up to 10 s (see HAC-02 for the overrun). `runtime_data` is touched only for loaded entries.
- `const.py`: values match their comments (LINK_IDLE_TIMEOUT is at least 2 × the energy poll plus a margin; SEQ_SAVE_EVERY < SEQ_RESTART_MARGIN). The only issue is HAC-03's signal formats.
