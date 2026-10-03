# Architecture

How the repository and the integration are put together. What a user sees is in the [user guide](../user/README.md)
and the [reference](../ha-integration.md); this page is for changing the code.

## Repository layout

```
custom_components/junghome_ble/          the Home Assistant integration (HA Bluetooth stack / ESPHome proxies), the mesh stack inside:
custom_components/junghome_ble/jhmesh/   Bluetooth Mesh stack: crypto, PDUs (segmentation), GATT-proxy client, device model (PyPI `jhmesh`)
jhmesh                                   symlink to the above, so the CLI tools import it as top-level `jhmesh`
pyproject.toml, MANIFEST.in              the `jhmesh` sdist + wheel (README-pypi.md is its PyPI page); ruff, mypy, pytest, coverage settings
scripts/package_ha.sh                    builds dist/junghome_ble.zip for unzipping into HA's custom_components/
blueprints/automation/junghome_ble/      automation blueprints for keys, presence and appliances (not in the zip; tests/test_blueprints.py)
tools/mesh_poc.py                        CLI: scan / listen (sniffer) / get / set / blink / lightness / ctl / scene / scene-actions / sched / health / prop / config / devices
tools/mesh_sniff.py                      passive capture with a Nordic nRF Sniffer dongle (key-free) + offline/live decoding
tools/trace_to_fixture.py                turns a decoded capture into a replay trace of the fixture network (tests/traces/), see testing.md
tools/mesh_report.py                     renders docs/network-topology.md from the iOS dump
tools/on_air.py                          lists every "unverified on air" marker with the symbol holding it
tools/parity.py                          keeps the parity ledger (docs/parity/) closed
tools/gen_entity_reference.py            generates docs/user/entities.md from strings.json and the registry snapshot
tests/                                   HA integration + mesh library test-suites (synthetic keys and identities), see testing.md
docs/user/                               the user guide (task-based, for Home Assistant users)
docs/de/                                 the German quick start
docs/ha-integration.md                   the integration reference: every device, entity, action, repair and limitation in detail
docs/dev/                                this developer documentation
docs/research/                           the index of the reverse-engineering notes (protocol, app, firmware, captures)
ios/, android/                           developer-only inputs, git-ignored: the iOS app backup (KEYS INSIDE — never publish) and the APK decompile
```

The integration is self-contained: the `jhmesh` mesh library is a regular package inside it, also published on its
own to PyPI (`README-pypi.md`). The command-line tools and how to run them are in the
[research notes](../research/README.md#running-the-poc).

## Integration modules

Layout of `custom_components/junghome_ble/`:

| File | Role |
|---|---|
| `__init__.py` | Loads the export (`load_network`), refuses to set up without a visible proxy (`ConfigEntryNotReady`), prunes stale devices, starts the hub, forwards the platforms; the update listener (`_async_entry_updated`) reloads the entry when its options or the data the hub was built from (`HUB_DATA_KEYS`) changed |
| `model_update.py` | Follows a rewritten export without a reload (review-4 D23): reads it as the setup does (`check_our_address`, `remove_stale_devices` live here), checks the running hub can take it over, rebuilds every platform's entities from it and carries the change over to the running ones; reloads when it cannot |
| `config_flow.py` | User, Bluetooth-discovery, zeroconf (the gateway's `_junghome._tcp` announcement: one flow per serial, the gateway form prefilled), reconfigure and reauth steps (reauth renews the gateway token alone; plus the gateway-import step and the options flow); our unicast address in a collapsed `advanced` section of every source form; validates by loading the CDB, checking the address and the visible proxies of the mesh (`0x1828` service data, `jhmesh.client.classify_proxy_advert`: Network ID, Node Identity and their Mesh Protocol 1.1 private forms; nodes of the export under another Network ID: `export_keys_stale`, `mesh_proxies_without_match`); unique ID = Network ID; discovery also aborts on what `coordinator.KnownMesh` knows a configured mesh by (`async_known_mesh_of`); the steps of a new export for an existing entry (`async_take_upload`, `async_fetch_to_store`, `async_replace_export`) are shared with `repairs.py` |
| `migration.py` | Import from the gateway integration: matches our registry entries against the gateway's identity scheme (`ImportPlan`), moves them with `er.async_update_entity_platform`, copies device area / name / labels, raises the `gateway_import` issue |
| `coordinator.py` | `JungHomeHub`: connection loop over HA's Bluetooth stack (`bleak_retry_connector.establish_connection`), state cache per element (`ElementState`), the `STATUS_HANDLERS` message table (the key handlers among them, handing the gestures to `hub.gestures`), command helpers, the repair issues (`key_refresh`, `pdus_dropped` — from the Filter Status watchdog, for a filter request actually written while the store lets sends through, or an unanswered refresh —, `export_stale`); the hub builds its `HAState` (`seq_store.py`) and node information (`node_info.py`) in `async_create` |
| `seq_store.py` | Where nonce reuse is decided, independent of the hub: `HAState` persists the sequence numbers through `Store` (`junghome_ble.seq.<mesh uuid>`, one record per address and the mesh's followed key refresh next to them, written at once; 2 s debounce with an immediate write every 64 numbers, on an IV change and after a load; exact counter on a clean close, +512 margin only after a crash), keeps the `.floor` entry of its address up with the counter (every IV change and `SEQ_FLOOR_EVERY` numbers; nothing sent under an index it does not hold yet, nor `SEQ_SKIP_UNKNOWN` past it), starts an address without a record `SEQ_SKIP_AHEAD` in when it may have sent before (`coordinator._evidence_of_use`), holds sends back while what a restart would load lags (`SequenceStalled`, retried by `JungHomeHub._while_seq_stalls` for `SEQ_STALL_DEADLINE`) and raises `seq_store_unwritable` after `SEQ_STALL_ISSUE_AFTER` of that, and refuses every send while another client is known to use its address (`AddressShared`, the `address_shared` repair); the store, its `.backup` copy and the `.floor` (`seq_store_for_uuid`, `seq_backup_store_for_uuid`, `seq_floor_store_for_uuid`), the skip-ahead of the `seq_store_lost` repair and of a restored backup, the legacy per-entry migration |
| `node_info.py` | What the nodes told about themselves (`NODE_VERSIONS`: software version, vendor information, time role), kept for the life of `hass` and in the entry's store (`NodeInfoStore`, `junghome_ble.<entry id>.node_versions`) |
| `entity.py` | Device-registry model (mesh service device → node devices with MAC → load / button devices), their names and rooms (`node_device_name`, `gang_room`, `node_room`, `device_rooms`), `JungHomeEntity` (dispatcher-driven, available while connected) |
| `light.py`, `switch.py`, `sensor.py`, `event.py`, `scene.py` | The platforms of the verified devices, all push-based (`PARALLEL_UPDATES = 0`): lights, sockets and the config switches, socket / detector / battery / proxy sensors, rocker events, scenes |
| `cover.py`, `climate.py` | Blinds (Generic Level position / slat elements, `0x1104` operation mode) and the room thermostat (set-point level, `0x004F` temperature, OnOff heating output, preset properties) — spec-only, unverified on hardware |
| `binary_sensor.py` | Detector motion / occupancy: the Sensor Status and OnOff Set handlers for detector elements (chained behind the coordinator's), the per-connection `Sensor Get`, the motion hold timer; the per-mains-node *Fault* register entity (the coordinator's `Health Fault Status` handler and connect-time survey feed it) |
| `number.py`, `select.py`, `button.py` (+ the config switches in `switch.py`) | The device-parameter entities generated from `config_entities.py` (`PropertySpec` → platform by codec) |
| `device_trigger.py`, `logbook.py` | Device triggers for the rocker events (per key, the event types its wiring produces) and the logbook descriptions of the `junghome_ble_button_action` / `junghome_ble_scene_recalled` bus events |
| `services.py`, `mesh_config.py`, `services.yaml` | The room / key-connection actions and the mesh configurator behind them: `mesh_config.MeshConfigurator` is the facade every caller uses (each operation delegates; a planner's `PlanError` becomes the translated service error) |
| `actions/` | The actions' handlers and schemas, one module per domain (`rooms`, `keys`, `scenes`, `schedules`, `thresholds`, `devices`, `audit`); `common.py` runs an operation (the entry's lock, the link wait, following the export, the plan's logbook line), `resolve.py` turns the ids a call names into mesh elements; `services.py` is the registration table |
| `configurator/` | The configurator's parts (review-4 brief 55): `plan.py` (the plan model — `ConfigStep`, `ordered`, `replay`, what a stopped plan applied, `KeyPlan`, `PlanError`) and `wiring.py` (modes and models, the wiring read from an export, the export's paths and digest, the room / key-link / scene planners as plain functions), both without a Home Assistant import; `store.py` (`ExportStore`: the export read and written, the provisioner identity, the plan journal, dry runs, the gateway's copy adopted, uploaded and retried); `executor.py` (`PlanExecutor`: a plan sent and judged step by step, a stop recorded, the journal replayed, the requests that wait for an answer and their timeouts); `rooms.py`, `scenes.py`, `thresholds.py`, `nodes.py` (the operations) |
| `schedules.py` | `Scheduler` (one per hub, `scheduler`): a load's JH Scheduler slots — read once per link for the *Schedules* sensor, written by the `get_schedules` … `delete_schedule` actions (`update_schedule` rewrites a slot in place); an astro schedule is preceded by Home Assistant's home location. Not tried on a device yet |
| `thresholds.py` | A metering socket's switch-on / switch-off thresholds (`0x5004` / `0x5005`): the two sensors and the reads and writes behind `set_threshold` / `delete_threshold`; the loads they switch are wiring, done by `MeshConfigurator` |
| `onboard.py` | The experimental `add_device` / `remove_device` / `reset_pending_device` actions: template choice, addresses clear of every provisioner and of the vault's nodes, provisioning (`jhmesh.provisioning`), the app's commissioning (`jhmesh.commission`), read-back and recording; the `pending_device` and `vault_unwritable` repairs |
| `areas.py` | Rooms to areas (review-4 U4-2): the area a room's devices start in (`area_name_for`: the flow's mapping, else the area named or aliased like the room) and moving devices to a changed mapping or room without touching one the user placed (`async_move_devices`) |
| `device_names.py` | A device renamed in Home Assistant is written into the app's device list of the export (`meta.devices[].name`) the way the app's rename does, in a background task; a name the app would refuse raises `device_name_rejected` |
| `config_entities.py` | The config entities' bases (`ConfigEntity`, `PropertyEntity`, `SetupStateEntity`, the lock function's, a load's `LoadLock`); re-exports what the platforms import from `properties/` |
| `properties/targets.py` | Which config entities exist: property → platform by codec (`describe`), the element and device each one is bound to (`config_targets` and the other `*_targets`), the setup states; no entity code |
| `properties/reader.py` | `PropertyReader` (initial reads — held while there is no link —, writes) and the status handlers that cache the values (registered when `config_entities.py` imports it) |
| `identity.py` | `VaultKeeper` (`hub.vault`): the mesh's `jhmesh.vault.Vault` in `.storage/junghome_ble.vault.<mesh uuid>` and its `.backup` copy (private, atomic, every write checked through `TrackedStore.written`: `async_save` says whether it landed, a failed one is retried; an unreadable one is set aside under a timestamped name, never overwritten, removed only once that copy landed; `async_recover` takes Home Assistant's entry back from the export when the vault lost it, `jhmesh.vault.recognise`); the configurator merges it into every file it writes or uploads only with `OPTION_PROVISIONER_IDENTITY` (`configurator.store.ExportStore.with_identity`) |
| `vault_refresh.py` | `VaultKeyRefresh` (`hub.vault_refresh`): the vault's devices taken through the app's key refresh as far as it is proven (`jhmesh.vaultrefresh`; NetKey Update, Phase Set 2, Phase Set 3), in the background on every move of the followed refresh and every new link; the `vault_key_refresh_lagging` repair; its diagnostics section. Unverified on air |
| `inserts.py` | `NodeInserts` (`hub.inserts`): each node's insert and key layout from the export, its JUNG advertisement (`_adv_seen`) or a read-only Get (a connect-time step); the device models and key positions they give; the `insert_mismatch` repair; `apply_reported` before the devices are registered. Unverified on air |
| `node_clocks.py` | `NodeClocks` (`hub.clocks`): each node's clock offset, zone offset and stored location from the Time Status, Time Zone Status and Generic Location Global Status it sends (the handlers are in `coordinator.py`); the read after the daily Time Set (`read_all`: mains nodes only, five at a time); the fixable `node_clock_wrong` repair (`async_fix`); the *Clock offset* sensor's value and the diagnostics. Unverified on air |
| `energy_history.py` | After a gap, imports the hours a metered load's *Energy* sensor missed from the meter's 24-hour and 31-day charts (`0x5010` / `0x5011`) into its long-term statistics, checked against the lifetime counter first |
| `app_follow.py` | `AppFollower` (`hub.app_follow`, made by the setup): follows the JUNG HOME app (review-4 U4-6, decision M12). A message from a source that is neither Home Assistant's address nor a device's is the phone's; a gateway entry fetches the gateway's export `APP_QUIET_AFTER` after the phone's last message (at most every `APP_SYNC_MIN_INTERVAL`) and every `GATEWAY_SYNC_PERIOD`, through `MeshConfigurator.adopt_if_gateway_changed` and the hub's follow; a file entry raises the persistent `app_changed` repair on the phone's Config or scene changes. Unverified on air |
| `keep_awake.py` | `KeepAwake` (`hub.keep_awake`): the app's keep-alive for a battery node while a Config plan or a parameter change addresses it — one task per node, reference-counted holds, an `Admin Get 0x5001` once the node was quiet for `KEEP_AWAKE_INTERVAL`, none while there is no link |
| `hub_gestures.py` | `ButtonGestures` (`hub.gestures`, review-4 A4-3): the keys' gestures from what the coordinator's key handlers hand it — clicks held back for the `click_delay` option, double clicks, gateway-mode and dimming holds with their end timers (`DimHold`, `KeyHold`), the firmware's repeated copies dropped (`TID_OFFSET`), the event listeners and `fire_button`; ends the holds on link loss and everything pending when the hub stops (`cancel_all`). Imports no platform: the bus event goes out through `JungHomeHub.publish_button_event` |
| `update.py` | The read-only *Firmware* `update` entity per node: the node's software version against `BUNDLED_FIRMWARE`, the app's bundled image per product id; no install feature (review-4 F4-18) |
| `gateway_api.py`, `tls.py` | The JUNG HOME Gateway REST client the config flow uses (access request / password registration, project download) and the certificate pinning it relies on (the gateway's certificate is self-signed; the pin comes over the mesh, `0xC003`, or is confirmed in the flow and then checked against `0xC003` by the hub before the gateway is used) |
| `gateway_status.py` | The gateway's own status over its REST API for an entry set up from the gateway (firmware, serial, access requests, API clients, the problem indicators, the error log): two pollers that run only while one of their entities is enabled, under the same pinning and token rules as the export's upload |
| `diagnostics.py` | Entry and device diagnostics, in every entry state (an entry not loaded: its state, reason, visible proxies and export summary); the link history; keys, token, paths (in error texts too) and Bluetooth addresses redacted |
| `repairs.py` | The fix flows of the fixable repair issues: skip the sequence numbers ahead (`seq_store_lost`, `pdus_dropped`, `address_shared`), take the IV index back (`iv_index_mismatch` when Home Assistant is ahead), send Time Set again (`node_clock_wrong`), dismiss a notice (`plan_interrupted`), sync the gateway (`gateway_sync_failed`), move to a free address (`address_in_use`), load a new export through the config flow's helpers (`unknown_nodes`, `export_stale`, `key_refresh`, `app_changed`), ask for another device name (`device_name_rejected`). Every issue's *Learn more* link is `const.ISSUE_LEARN_MORE` |
| `backup.py` | The backup platform: `async_pre_backup` marks every mesh's sequence-number records with a token of the backup (`in_backup`, `HAState.backup_token`) and waits up to `BACKUP_WRITE_TIMEOUT` for both copies to carry it; `async_post_backup` removes it. `JungHomeHub.async_create` skips a record carrying a token this process did not set `SEQ_SKIP_AHEAD` ahead (`_async_skip_restored_record`) |
| `const.py` | Constants shared by every module: configuration keys, timings, signal names, repair issue ids (`issue_id`) |
| `dispatch.py` | `chain_status_handler`: a second consumer of a message type that already has a `STATUS_HANDLERS` row (the detectors on Sensor Status and OnOff Set, the thermostat on Sensor Status), run after the earlier handler |
| `errors.py` | `mesh_errors`: the context manager that turns a command's transport errors into the translated `HomeAssistantError` (`TimeoutError` → the site's own key or `send_failed`, `ConnectionError` / `OSError` → `send_failed`) |
| `conversions.py` | Pure Generic Level conversions — thermostat set-point (`temperature_to_level`, `level_to_temperature`) and blind closedness (`closedness_to_level`, `level_to_closedness`) — used by the platforms, the configurator, the actions and the schedules without importing a platform |
| `strings.json`, `translations/en.json`, `translations/de.json`, `icons.json` | Config-flow, entity, exception and issue translations (English, German); icons |
| `quality_scale.yaml` | Rule status for the Integration Quality Scale |
| `brand/` | The integration's icon (`icon.png`, `icon@2x.png`, `dark_icon.png`, `dark_icon@2x.png`): the JUNG HOME brand images Home Assistant's brands repository publishes for `custom_integrations/junghome`. Home Assistant 2026.3 and later take a custom integration's icon from here; the HACS `brands` check accepts it |
| `jhmesh/` | The mesh stack (crypto, PDUs with segmentation, GATT-proxy client, CDB loader, device model), bundled as a regular package; also published on its own to PyPI as `jhmesh` (`pyproject.toml`). The repository's top-level `jhmesh` is a symlink to it (the CLI tools import it from there); `scripts/package_ha.sh` zips the whole directory, `standalone.py` (the CLI's plain-`bleak` link, unused by the integration) included |

## The `jhmesh` library

The public modules, as `README-pypi.md` lists them for the PyPI page: `cdb` (parse an export, the keys and the
node list), `devices` (what each node is), `messages` / `config_messages` / `vendor_models` (build and decode access
messages), `client` (`ProxyClient`: connect through a node's GATT proxy, send, receive, request / response with
acks), `state` (`LocalState`: our address, sequence numbers, IV index state and replay list, persisted to a locked file
or, subclassed, to Home Assistant's store — `HAState`; `client` re-exports it), `standalone` (the same over a plain
`bleak` scanner, for scripts), `provisioning` (PB-GATT), `commission` (the JUNG app's post-provisioning
configuration, planned as data), `onboarding` (adding a node end to end: its addresses, commissioning, read-back and
recording in the export), `vault` (a provisioner entry of your own and the device keys of the nodes you provisioned)
and `sniffer` (decode passive nRF Sniffer captures). The rest
(`crypto`, `pdu`, `properties`, `export`, `merge`, `audit`, `advert`, `keyrefresh`, `vaultrefresh`, `fileio`) serves
those and the integration. Every module's `__all__` is its public API, pinned by `tests/jhmesh/test_api_surface.py`
(a change to it is a change to the published package); underscore names are private, and `jhmesh/__init__.py` imports
nothing, so `import jhmesh` loads neither `bleak` nor `cryptography`.

## Behaviour worth knowing

- `jhmesh.client.ProxyClient` is transport-agnostic: HA hands it a `BleakClientWithServiceCache`; the CLI in
  `tools/mesh_poc.py` hands it a plain `BleakClient` through `jhmesh/standalone.py`. After `attach()` it subscribes to
  Mesh Proxy Data Out, sets the proxy filter to *blacklist, empty* (receive everything) — both the hub and
  `standalone.connect` first wait up to 1 s for the proxy's beacon (`CONNECT_BEACON_WAIT`, `standalone.BEACON_WAIT`) —
  and records the proxy node's address from the Filter Status reply (shown by the *Proxy node* sensor). The
  subscription and `detach()`'s disconnect are bounded by `GATT_TIMEOUT` (5 s) like every GATT write; a disconnect
  that times out is logged as a warning and left behind (review-4 R4-11, unverified on air).
- Control uses acknowledged Sets (`Generic OnOff Set` with transition 0, `Light Lightness Set`, `Light CTL Set`,
  `Light CTL Temperature Set` to the element after a tunable-white light's, which hosts its `1306` server, in the
  gateway's 7-byte form with transition 0 and delay 0) but
  never waits for the reply: JUNG firmware answers a state change only by publishing the status to the element's group,
  which is what updates the entity. Scene recall is one `Scene Recall Unacknowledged` to `0xFFFF`. The setters take
  a `transition` in seconds (`JungHomeHub.set_lightness` and the others, `recall_scene`, `central_command`,
  `room_command`): it becomes the Set's transition-time byte (`jhmesh.messages.encode_transition`, delay 0), and a
  status announcing a remaining time schedules a state Get that much later plus a second
  (`_reread_after_transition`). The entities pass one only for a kind in `light.TRANSITION_KINDS` and when
  `scene.SCENE_TRANSITIONS` is set, both empty / off until the probe of `docs/hidden-features.md` §11 (unverified on
  air).
- After every connect `_after_connect` broadcasts Time Set and the location first (unacknowledged, right after the
  proxy filter), then `_refresh_all` sends one Get per light and socket (`REFRESH_CHUNK = 5` jobs between 0.5 s
  pauses), one job per metered load's meter element (`Devices.metered`, `jhmesh.devices.meter_element`) that sends
  its property-qualified `Sensor Get`s one after the other (`_get_readings`: `0x0081`, `0x005D`, `0x005C` on a
  socket, `0x0081` alone on another metered load — `meter_readings`; one attempt each; the meter answers
  only a property-qualified Get — an unqualified `Sensor Get` got no reply on air, ever), plus one `Light CTL
  Temperature Range Get` per CTL light and one `Light CTL Temperature Get` to its temperature element
  (`STATE_GETS["ctl_temperature"]`; `async_refresh_meter` reads one load's readings and counters on demand, for
  `homeassistant.update_entity`); `_poll_energy` then reads the counters of every metered load
  (`_get_counters`: `0x006D` on a socket's main element's Admin server, `0x0072` / `0x000D` on the meter element's
  Manufacturer server and `0x006A` on its Admin server — `counter_element`; an early probe of the *main* element had
  found no energy counter) and repeats every
  `ENERGY_POLL_INTERVAL` (`_poll_energy_periodic`, skipped while disconnected or while a poll is running; cancelled
  with the link). Statuses in the long form carry present and target; the entities show **present** (as the app
  does) and `ElementState.target_*` keeps the target.
- Config entities (`config_entities.py`, `properties/targets.py`, `properties/reader.py`): every `PropertySpec` with
  `access` rw/wo and an `app` source is mapped by codec to `number` / `select` / `switch` / `button`; `PropertyReader`
  schedules the initial reads (3 s after the first job, 5 distinct elements per round, 0.5 s pause; a job is queued
  once per element and key, counted for the link it was last queued on, and dropped when its link is gone — review-4
  R4-5; a node's version read once per hub and again after `hub.restarted` names a restart since), one status handler
  for the three vendor Status opcodes fills `ElementState.properties`. The status LED is written with a User Property
  *Status* (never read); such a Status the gateway sends to a push-button's key is cached as that key's value
  (`properties.reader.status_owner`). An entity whose read got every value queues it again on a later link once
  `CONFIG_REREAD_INTERVAL` (3 h) has passed, once per link (`ConfigEntity._read_due`; review-4 H4-10).
- `homeassistant.update_entity` (`JungHomeEntity.async_update`): each entity names what it reads (`_update_read`:
  the state Get of `_refresh_kind` for a light or socket, both level elements of a cover, the meter, the thermostat's
  refresh with `since`, the detector's illuminance, a config entity's values with `since` so a read within
  `PROPERTY_READ_FRESH` does not answer it); the base skips battery nodes and a missing link, keeps one read per
  element and name per `UPDATE_READ_INTERVAL` (`entity.update_reads`, per entry) and logs instead of raising.
- Services (`services.py` → `actions/` → `mesh_config.py`): registered once in `async_setup`; each entry contributes a
  `MeshConfigurator` (per-hub lock). Operations plan `ProjectFile` mutations, send the Config messages over the device
  key with `request_config`, write the KeyMode property, save the file atomically and have the hub follow the
  export (`model_update`).
- Incoming messages: `_on_message` only accounts for the traffic (link watchdog, drop detection) and then looks the
  message up in `coordinator.STATUS_HANDLERS`, a table keyed by `(company id, opcode)` (`None` for SIG opcodes) that
  the `@register_status_handler(*opcodes, company_id=None)` decorator fills. One small handler per message type
  (`_on_onoff_status`, `_on_ctl_status`, `_on_sensor_status`, `_on_onoff_set`, `_on_vendor_property_set`, …); a
  handler gets `(hub, message, params)`, reads or creates the cache entry with `hub.element_state(addr)`, calls
  `hub.notify_update(addr)` once it changed it (that is what re-renders the entities) or `hub.fire_button(...)` for a
  gesture. A message type without a row is ignored. **A new message type** (Generic Level Status for blinds,
  Admin/User Property Status for config entities and the power-on-hour counter) is one more decorated function, in the
  coordinator or in the module that owns the feature; `ElementState` already has `level`, `battery` and
  `properties` slots for them. A message type that *already has* a row (Sensor Status, OnOff Set) gets a second
  consumer through `dispatch.chain_status_handler`, which re-registers the row with a wrapper that runs the
  previous handler first — the detector handlers work that way, so the coordinator keeps its socket-meter and
  rocker-event logic untouched.
- Detectors (`binary_sensor.py`, `sensor.py`): a detector's Sensor Status readings `0x004D` / `0x0055` are cached in
  `ElementState.properties` under their SIG ids (diagnostics show them) and pushed to the motion / occupancy entity
  through `SIGNAL_DETECTOR` as `("presence", bool)`; an OnOff Set from a detector element arrives as
  `("motion", bool)` and is held for `DETECTOR_MOTION_HOLD`. The entity sends one `Sensor Get` per connection itself
  (the hub's `_refresh_all` only covers loads). Battery products (`Button.battery`, `BATTERY_PIDS` 0x05 / 0x06 /
  0x16): the battery sensor subscribes to the hub's event listeners of the node's keys and sends `Generic Battery
  Get` from there (one attempt, `BATTERY_READ_TIMEOUT`); `_on_battery_status` (sensor.py) stores the level in
  `ElementState.battery` and broadcasts the decoded flags on `SIGNAL_BATTERY`. All of it is unverified on hardware.
- Battery nodes being configured (`keep_awake.py`, review-3 W4 / F24): `configurator.executor.PlanExecutor.send` orders a plan's steps
  so a battery node's go first within the additive and the destructive half (`ordered(steps, sleepy)`) and holds
  `hub.keep_awake` for those nodes while it sends; `assign_key` holds the key's node from its first Config step to
  its KeyMode write. A config entity's change holds it through `ConfigEntity.changing` (with the `modifying` lock)
  and `PropertyEntity.async_write_value`. The keep-alive task sends the app's `Admin Get 0x5001` to the node's
  primary element once `last_heard` is `KEEP_AWAKE_INTERVAL` old (so it stays quiet while the operation talks to
  the node), 1 s after an unanswered one, matched on the property id — as are the operation's own Admin requests
  (`PropertyReader._get` / `write`, `configurator.executor.PlanExecutor._admin_status`), so a keep-alive overlapping one waiting out
  its retries answers neither; it is cancelled when the last hold ends, and one that died is replaced by the next
  hold. A battery node's silence is `service_node_asleep` / `setting_asleep` instead of the no-reply errors; a lost
  link stays `send_failed` / `service_send_failed`.
- Device model (`jhmesh/devices.py`): `build_devices` walks every element of every product node through
  `ELEMENT_RULES`, a list of `ElementRule(kind, matches(node, element, pid), build(ctx, node, element))`; the first
  rule that claims an element builds its `Device` (`Light`, `Socket`, `Button` share the `Device` base: `address`,
  `unique_id`, `name`, `node`, `kind`, `rooms`), `Devices.add` files it under `by_address` and in its typed list.
  **A new device kind** (blinds on a `1002` level element, a room thermostat, a `1001`+`1100` detector, a mini
  input) is one appended rule plus a `Device` subclass; `build_devices(cdb, meta, rules=...)` takes a custom table
  for tests. Built-in rules: element location `0001`/`0002` with an OnOff or Lightness server = load (socket when
  the node's PID is a socket, else `ctl` if a CTL server is present, else `dimmer` if Lightness, else `switch`);
  a blinds-only product's non-first Level elements are its blind's slats, never a load, whatever else they host;
  location `0040`+ with an OnOff client and no Sensor server = button; a Sensor server at location `0040`+ is the
  socket's meter, or — on a detector product (`DETECTOR_PIDS`) — the `Detector` itself (`relay_address` = the node's
  own load, `target` = where its OnOff client publishes, `presence` = ceiling presence detector). Rooms = names of
  the subscribed groups that are neither element groups (`element group #0x…`
  from iOS, `element group #…` decimal from Android) nor device-type groups; the first one is the suggested area.
  `Button.gang` is the location set of the app device entry the key sits in (`Metadata.entry_for`); the buttons
  device is keyed on it (`entity.py`), so two gangs with the same app name stay separate devices.
- Button events: vendor `LBC User Property Set Unacknowledged` (opcode `0x10`) with property `0x5012`
  `[counter][code]` (`0x05` click, `0x06` hold start, `0x04` hold end), de-duplicated on `(src, counter)`;
  `double_click` = second click within `DOUBLE_CLICK_WINDOW` (0.5 s); with the `click_delay` option a `click` is
  held back (`async_call_later`) for that window and dropped when the second click turns it into a double click.
  Direct-wired rockers are recognised by the SIG
  messages they send from a button element (OnOff Set → `press_on`/`press_off`, Scene Recall → `scene`, Generic
  Level/Delta/Move → `dim`).
- Sensor Status properties: `0x0081` power (uint24, 0.1 W), `0x005D` voltage (uint16, 1 V), `0x005C` current
  (uint16, 0.01 A); on detectors `0x004D` presence (uint8) and `0x0055` illuminance (LE, 0.01 lx; whole lux on device
  software ≤ 1.4.0.0 per the gateway firmware, all ones = unknown) — the last two unverified. The socket's sensor
  server answers only property-qualified `Sensor Get`s (`M.sensor_get(pid)`, ~100 ms on air) and ignores an
  unqualified one; the detector's `Sensor Get` in `binary_sensor.py` is still unqualified (unverified hardware).
- IV Update is followed from the Secure Network Beacon (`LocalState.apply_beacon`): during "update in progress" we
  transmit with the old index and reset the sequence number only when the transmit index rises (a same-index "in
  progress" beacon after we completed the update is a lagging node and is ignored). An IV change re-sends the proxy
  filter. Timing (Mesh Protocol 1.1 §3.10.5–§3.10.6, review-4 D10): once an index is known, a step between Normal
  Operation and IV Update in Progress needs 96 h since the last change of the IV state (`IV_UPDATE_MIN_STATE`), an IV
  Index Recovery 192 h since the last one (`IV_RECOVERY_MIN_INTERVAL`), on the wall clock (`_wall_now`, stored with
  the counter as `iv_changed_at` / `iv_recovered_at`; absent = no restriction; the fresh state's first beacon stamps
  nothing). A stored time later than the clock (the clock went back) is pulled back to it, so a backwards jump costs
  at most one more period. Refusals are logged at WARNING once per index. Without the timing, ten authenticated
  beacons each 42 ahead took the index from 5 to 425 (`tests/jhmesh/test_iv_timing.py`). A beacon that fails authentication with the export's NetKey but carries the Key Refresh flag raises the
  `key_refresh` repair issue (`JungHomeHub._on_beacon`; not fixable; deleted when the coordinator starts or stops):
  Phase 2 beacons are secured with the new key (Mesh Profile §3.10.4), so a flagged beacon *our* key authenticates
  means our keys are the new ones already and raises nothing. The flag is outside anything our key can verify, so it
  is a hint (a forged beacon raises it too). A refresh heard from its start is followed instead
  (`ProxyClient._follow_key_refresh`: the new key from the provisioner's device-key-sealed Config NetKey Update, old and
  new key material side by side, transmit with the new one from Phase Set 2 or a flagged new-key beacon, old one
  dropped at Phase Set 3 or an unflagged new-key beacon; `LocalState.key_refresh` keeps it across restarts — `HAState` at mesh level, saved at once (`persist_now`) — and
  `async_apply_followed_key_refresh` puts a completed one in place of the export's key at setup). `ProxyClient` logs an unauthenticated flagged beacon at
  WARNING, an authenticated one at INFO. `ProxyClient.rx_undecryptable` counts the PDUs a link forwards that neither
  the NetKey nor the AppKey / device keys open (`on_undecryptable`); together with beacons that fail authentication
  they raise `export_stale` once `EXPORT_STALE_THRESHOLD` (20) of them arrived on one link with nothing decodable —
  deleted by the first decoded message or a restart. The proxy node's address (`proxy_node`, the diagnostic sensor)
  is filled in by `on_filter_status` when the proxy's Filter Status arrives, not at `attach()` time. A proxy
  configuration PDU counts only as a control PDU to the unassigned address with a sequence number above the last one
  taken on the link (`_on_proxy_config`, review-4 P4-7); the rest is dropped and counted (`rx_proxy_config_dropped`,
  `link.proxy_config_dropped` in the diagnostics).
- Own-source detection (`address_shared`, review-4 S I2): a PDU from our own address is our echo when it carries a
  number we handed out (`ProxyClient._handed_out`: below the counter under the transmit index, below the counter and
  `seq_peak` under an older index from `seq_peak_from` on, nothing under a newer one unless `seq_guard` covers it);
  anything else goes to `on_foreign_own_source` (at most once per `FOREIGN_SOURCE_REPORT_INTERVAL` per link, with the
  highest seen). The hub records it (`HAState.note_address_shared`, saved at once, in the address's record), refuses
  every number with `AddressShared` (a `SequenceStalled` that `_while_seq_stalls` does not retry), and raises the
  fixable issue; the fix (`async_skip_past_shared`) moves the counter `SEQ_RESTART_MARGIN` past the sighting (a
  sighting under the next IV index also raises `seq_guard` to it) and renews a link without a Filter Status. A
  second sighting on the same hub uses the `address_shared_again` text.
- Drop detection (`pdus_dropped`): a source address whose sequence numbers the nodes have already seen higher is
  ignored by *every* node, the proxy included — on air the beacon authenticated but the proxy never answered the
  Set Filter, so the link stayed on the default empty whitelist and nothing was forwarded. Hence the Filter Status
  watchdog: `_connect_to` arms `async_call_later(FILTER_STATUS_TIMEOUT)` when `attach()` returned without a Filter
  Status; `_filter_status_overdue` raises the issue (WARNING, once per link) when the beacon authenticated
  (`_beacon_authenticated`, per link) and `proxy_node` is still None. `_on_filter_status` cancels the watchdog and
  clears the issue, as does the first unicast reply (`_on_message`). The refresh-based check in `_refresh_all` also
  counts a link as dropped when the beacon authenticated and it decoded nothing at all (`_rx_decoded_link == 0`)
  and nothing answered — with a stuck whitelist there is no other traffic to hear by construction.
- Reload after a reconfiguration: HA 2026.9 deprecates `async_update_reload_and_abort` for integrations with an
  update listener ("should use it for scheduling a reload", breaks in 2026.12). `_async_finish_checked` therefore
  only calls `async_update_entry` and aborts; the listener `__init__._async_entry_updated` reloads a loaded entry
  whose `hub_data` (`HUB_DATA_KEYS`: path, metadata dir, address, mesh UUID, source — not the gateway host / token /
  fingerprint) or options changed (`JungHomeHub.needs_rebuild`, compared with a snapshot taken when the hub was
  built). The flow schedules the reload itself when the listener will not run: an entry that is not loaded (no
  listener registered) or unchanged data (a re-fetched / re-uploaded export lands in the same file). The entry state
  is read *before* `async_update_entry`, since listeners start eagerly from inside it.
- Segmented messages (access PDUs above 11 bytes: long vendor property Sets, Config Model Publication Set) are sent
  with block-ack retransmission to a unicast destination (up to `SEGMENT_RETRIES` 4 rounds, `SEGMENT_ACK_TIMEOUT`
  1.5 s each; only the unacknowledged segments are repeated, with fresh sequence numbers) and twice without acks to a
  group. Each round holds the send lock only while it reserves and writes its segments, not while it waits for the
  acknowledgement, so other messages are not queued behind an absent node; segmented messages to one destination go
  one at a time. When the IV index changes mid-way the message starts over under the new index. Received segmented
  messages are reassembled and, when addressed to us, acknowledged (a partial ack after the last segment, the full
  ack again when a completed message's segments are repeated).
- Replay protection (Mesh Profile §3.8.8) covers access and control PDUs: the last accepted (IV index, sequence
  number) per source address (`LocalState.rpl`), stored with our sequence counter in `HAState`'s record so a PDU
  recorded off the air is not accepted again after a restart. The list holds up to 2048 sources and refuses new ones
  when full (no eviction); an IV index change drops only the entries older than the previous index. A segmented
  message is checked on the sequence number of the segment that starts its reassembly, and a message already
  delivered is recognised by its SeqAuth.
- Connection loop constants: `FAILED_PROXY_COOLDOWN` 120 s, `CONNECT_BACKOFF_MIN` 2 s, `CONNECT_BACKOFF_MAX` 60 s,
  `SHORT_LINK` 60 s and `SHORT_LINK_STREAK` 3 (a link lost sooner doubles the back-off, three in a row set the node
  aside for the cooldown), `LINK_LOSS_GRACE` 20 s, two connect attempts per candidate, 30 s wait when nothing is
  visible (woken early by the advertisement callback, for an advertisement of this network only: `JungHomeHub._ours`,
  review-4 R4-8). `ProxyClient.classify_service_data` keeps its verdicts by service data (`CLASSIFY_CACHE_SIZE` 256,
  least recently seen dropped first): a Node Identity costs one AES per node per key. They are dropped whenever the
  accepted keys change (`_kr.rx_keys`) and in `add_node` / `remove_node`. Every end of a link goes through
  `JungHomeHub._link_ended` (the transport's disconnect, or `_drop_link` for the hub's own drops), which records a
  `LinkEnd` (reason, verdict on the proxy, duration), starts the grace and calls the listeners registered with
  `async_on_link_loss`.
- Connect-time steps (`_connect_step`): the scene actions, the fault survey and the current scenes record when their
  last complete round ended; a link whose predecessor lasted `SHORT_LINK` skips a step done within
  `CONNECT_STEP_FRESH` (900 s). The heartbeat configuration keeps its own `HEARTBEAT_RECONFIGURE_INTERVAL`; the
  clock, the location, the state refresh and the energy poll run on every link (review-4 R I-5, unverified on air).
- Per-message hot paths (review-4 R4-9, R I-10): `CDB.element` / `node_by_addr` are a dictionary built on first use.
  Whatever changes `CDB.nodes` or an element's address calls `CDB.reindex()` (`ProxyClient.add_node` /
  `remove_node`; the export's edits build a new `CDB`); a list that grew or shrank without it is re-indexed anyway, an
  element moved in place is not (`CDB.index_is_current()` checks it). `Devices.by_meter` / `by_temperature` are kept
  by `Devices.add`. `HAState._restart_point` reads a copy's record once per `written` object (a write replaces it,
  never edits it) instead of parsing the replay list for every sent PDU. `JungHomeEntity._handle_update` compares
  what it would write (`_async_calculate_state`) with the state machine and writes only a difference — availability
  is in the state string, so a change of it is always written. `PYTHONPATH=. .venv/bin/python scripts/bench_mesh.py`
  times these paths against the fixture network grown to 300 nodes.

## Not done yet

- Live test in a running Home Assistant with a local adapter (the first run over ESPHome proxies is done; it found
  the property-qualified Sensor Gets, the energy counters on the meter element, the Filter Status watchdog and the
  reload deprecation above).
- Blinds (Generic Level on the 2-channel actuators), room thermostat; detectors and battery levels exist but need a
  device to verify them (walking test, forced-off and PIR parameters of detectors; the 6 s keep-alive that keeps a
  battery device awake while it is configured); the lock function (`0x0009`) on a real device, and lock-out protection / wind alarm
  for blinds.
- Room groups as light groups.
