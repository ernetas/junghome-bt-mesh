# Changelog

## 1.1.0 (unreleased)

### Upgrading

- **`delete_unused_scenes` is a dry run by default** (decision M3): a call without fields, as an automation made it
  before, now only lists the numbers it would delete (the response is the same shape) and deletes nothing. Add
  `dry_run: false` to delete; on an entry set up from a file also `confirm_stale_export: true` (the file holds every
  scene of the app) or `numbers: [...]`. On a gateway entry it refuses while the gateway does not answer.
- **Rewiring and deleting actions are for administrators only** (decision M8, review-4 W4-9): `set_room`,
  `create_room`, `rename_room`, `delete_room`, `assign_key`, `clear_key`, every scene action (`store_scene`
  included), `sync_gateway`, the schedule actions but `get_schedules`, `set_threshold` and `delete_threshold` now
  fail with *Unauthorized* when a user who is no administrator calls them, from a dashboard, a script that user
  starts or that user's long-lived token. Automations the system triggers are not affected. `get_schedules`,
  `audit_network`, `find_new_devices` and the dimming actions stay open to every user; `export_network` and adding
  or removing devices were administrators only already.
- **`hold_end` can come without a release** (decision M11, review-4 R4-7): a hold now ends at the latest 30 s after
  it started, when the link is lost and when the entry stops (a reload included), with a `reason` attribute
  (`timeout`, `link_lost`, `stopped`) on the event entity and the `junghome_ble_button_action` event. An automation
  that must react to a real release only checks that `reason` is absent. The device-trigger picker lists only what
  a key's wiring produces; automations saved with another subtype keep working.
- **`set_room` no longer creates a room it does not know** (review-4 W4-12): a mistyped room name used to create a
  new room and move the devices into it. An unknown name is now refused (*There is no room named …*); add
  `create: true` to create the room, as automations that relied on it need to.

### Fixed — mesh safety

- Sequence-number back-pressure is bounded and no longer mistaken for exhaustion (review-4 D9, R4-2): a send the
  store holds back is retried every 5 s for 2 minutes at most, then fails like a lost link; a really used-up sequence
  space (or a newer hub owning the numbers) fails at once. Both used to be retried for as long as the link was up, so
  the link watchdog's keep-alive never returned and a silent proxy was never dropped; a keep-alive that cannot be sent
  now decides nothing, and the watchdog drops a silent proxy as usual.
- A sequence-number store that cannot be written (a full disk, an SD card remounted read-only) raises its own repair,
  *JUNG HOME sequence numbers cannot be saved*, after a minute, naming the file and the last write error; it clears
  itself once a write lands. Review 3 listed this repair as done, but it was never built: the user got *JUNG HOME
  devices ignore Home Assistant* instead, whose fix cannot be written either. That issue is no longer raised for a
  proxy filter request that was never sent, nor while the store holds sends back. Diagnostics show `stalled_for`,
  `last_write_error` and `durable_headroom`.
- A send with no link fails before it takes a sequence number (review-4 R4-4): every send path (commands, segmented
  messages, the proxy filter, segment acknowledgements) used to persist a number and only then find the link gone, and
  one after the entry stopped cleared the clean-close mark, so the next start added the restart margin for nothing. The
  property reads and a battery node's keep-alive wait for the link instead of sending into "not connected".
- `delete_unused_scenes` no longer deletes the app's scenes from a stale export (review-4 W4-3). It deletes every
  scene number the export does not know, and an entry set up from a file — or a gateway entry whose gateway did not
  answer, which silently planned on the copy on disk — lacked the scenes and timer scenes made in the app since:
  they were deleted from every device while the app still listed them. It is now a dry run unless told
  `dry_run: false` (it reads the scene registers and answers what it would delete); a gateway entry works only on
  the gateway's current export (taking it over first when the app changed something) and refuses when the gateway
  does not answer, dry run included; a file entry deletes only with `confirm_stale_export: true` or the `numbers` to
  delete. A number that is a scene of the export is refused in `numbers`. Unverified on air.
- A scene number a device still holds after `delete_scene` with `force` skipped it is no longer handed to a new
  scene, which that device would have joined (review-4 W4-8): the number is held
  (`.storage/junghome_ble.<entry id>.held_scenes`, removed with the entry) until `delete_unused_scenes` deletes it
  from the device. The skipped members are answered (`{"skipped": ["0232"]}`, with *Response* on) and named by the
  new repair issue *Devices still hold a deleted JUNG HOME scene*, instead of a log line only.
- Devices Home Assistant added (`add_device`) are carried through the app's key renewal (review-4 D11). The app
  hands the new network key only to the devices of its own database, so such a device kept the old key alone and
  was cut off when the renewal completed. Home Assistant now sends it the same messages, sealed with the device key
  only its vault holds, and only as far as the renewal is proven: NetKey Update once the new key is confirmed by two
  devices or by the proxy node (or the export was written mid renewal), Key Refresh Phase Set 2 at a proven Phase 2,
  Phase Set 3 once the renewal is proven complete — never earlier, and never a key one device made up. Each step
  waits for the one before it to be confirmed, is retried on every new connection, and is recorded in the vault
  (the phase and the new key's Network ID, no key; also in the diagnostics). A device that does not confirm the end
  raises the repair issue *Devices Home Assistant added missed the new network key*. Nothing is sent without such a
  device. `add_device` refuses while a renewal is in Phase 1 (the device would get the key being retired).
  Unverified on air.
- The *sequence numbers lost* repair only trusts numbers a record can hold (review-4 S4-7): an IV index past 32
  bits made it write a record no start could use and a floor no later repair got past, and a negative counter put
  it back at 0 under its index, over the numbers sent there. When its floor cannot be written it now says so
  (*could not be written: nothing was changed*) and the issue stays, instead of disappearing with the integration
  still stopped.
- The repair floor (`.storage/junghome_ble.seq.<mesh uuid>.floor`) keeps up with the counter (review-4 S4-8): Home
  Assistant writes a new entry with every new IV index and every 2^20 numbers, and sends nothing under an index the
  file does not hold yet, nor 2^22 numbers past its entry (the distance the repair continues past it). A floor that
  cannot be written holds sends back like the store and names itself in *sequence numbers cannot be saved*. Written only by the repair before, it protected a second
  loss of both copies of the store only until the address had sent 2^22 more numbers.
- A followed key refresh is saved at once and for the mesh (review-4 S4-9): it went out with the 2 s delayed save,
  so Home Assistant stopped right after a step came back without the new key, and it was kept with the address, so a
  new unicast address after a completed refresh started with the revoked key. The store's minor version is 4 (the
  mesh-level part next to the addresses; the address's record keeps a copy): 1.0.0 still reads it.
- An address without a sequence-number record starts 2^20 numbers in, not at 0, when something says it may have sent
  before (review-4 S I5): the export has Home Assistant's provisioner node or another node at it, the vault keeps
  Home Assistant's identity in the mesh, or the store knows other addresses — a lost record of that address left
  the devices ignoring Home Assistant until the *devices ignore Home Assistant* repair. Pending the first beacon the
  counter keeps on under that beacon's index. This spends 2^20 of the 2^24 numbers of the IV index once, also on an
  address that really is new on such an installation (any address changed to in *Reconfigure*). Unverified on air.
- The *devices ignore Home Assistant* repair no longer moves a counter that used its last number back onto it: past
  the end of the sequence space its skip ahead stopped at the last number and handed it out again (found by the
  property tests' state machine).
- A device key Home Assistant hands out is no longer lost to a vault write that failed silently (review-4 D15).
  Home Assistant's storage only logs a failed write (a full disk, an SD card remounted read-only), and the vault took
  it for written: the key of a device whose configuration then failed lived in memory only, and the next restart lost
  it — such a device can only be factory-reset. Every vault write is now checked and a failed one retried by the next
  save; `add_device` writes the device's key to the vault before the device receives its Provisioning Data (the key
  is known one step earlier), and when that write does not land it stops right there, the device still new, with the
  new repair issue *JUNG HOME device keys cannot be saved* (it clears itself once a write lands). A provisioning not
  confirmed after the data went out keeps the device pending (`reset_pending_device`). The vault has a `.backup` copy,
  read when the vault is missing or does not read back, and an unreadable vault is removed only once its copy aside
  was written — before, a failed copy lost it. The library's `provision()` takes an optional `on_device_key` hook for
  this. Unverified on air.
- Another client sending from Home Assistant's address is told apart from a stale counter (review-4 S I2): a PDU from
  that address with a number Home Assistant never sent (above its counter, or under an IV index it never transmitted
  under) used to be dropped as its own echo, and the clash showed at best as *JUNG HOME devices ignore Home
  Assistant*, whose skip ahead does not help while the other client keeps sending. It now raises the repair issue
  *Another client uses Home Assistant's JUNG HOME address* and Home Assistant sends nothing to the mesh until it is
  fixed, so no sequence number is used by both (a reused nonce); the sighting is saved with the counter, so a
  restart keeps refusing. The fix continues the counter past the highest number seen (plus 512); seen again after
  that, the issue asks for another unicast address. *Devices ignore Home Assistant* is not raised while it is open.
  Our own PDUs relayed back stay ignored. The store's minor version is 5 (the optional `address_shared` of an
  address's record): 1.0.0 still reads it. Diagnostics show `address_shared`. Unverified on air.
- Proxy configuration PDUs (the proxy's Filter Status) get the checks every other PDU gets (review-4 P4-7): they must
  be control PDUs to the unassigned address, and one at or below the last sequence number taken on the link is
  dropped as a replay. A recorded Filter Status could be played back to stand in for the acknowledgement of a filter
  the proxy never took. Drops are counted in the diagnostics (`proxy_config_dropped`).

### Fixed — link

- A proxy node that drops the link right after every connection is no longer picked again and again (review-4 D13,
  R4-1): a link lost within a minute counts as a failed connection, so the pause before the next attempt grows, and
  after three in a row the node is passed over for two minutes in favour of the next one in range (a node that is the
  only one in range is still used). Each of those links used to restart the connect-time refresh, so the time and
  location were never sent. Only a link that lasts a minute resets the back-off. Unverified on air.
- Entities keep the 20 s link-loss grace however the link ends (review-4 D14, R4-3): a link Home Assistant dropped
  itself — the watchdog finding the proxy silent, the *devices ignore Home Assistant* repair, an error in the
  connection loop — made every entity unavailable at once, and an action skipped them, dropping its command. A link
  lost while it was still being set up is a failed connection, no longer shown as connected for a moment. Unverified
  on air.
- *All lights*, *All sockets* and the rooms' central entities keep the link-loss grace too, and their commands wait for
  the next link like the loads' (review-4 R4-6, H4-5); they used to go unavailable the moment the link went.
- A load's command whose link was lost (or replaced) while it waited for the answer is sent once more on the next link
  instead of failing (review-4 R I-11). Unverified on air.
- The time and the home location go out first on every link, right after the proxy filter (review-4 R I-5): they were
  sent only once the state refresh was through, so a link lost before that never set the nodes' clocks. The scene
  actions, fault registers and current scenes are not read again by a link that comes within 15 minutes of their last
  complete read after a link that lasted a minute; the state refresh and the energy poll stay on every link.
  Unverified on air.
- A node's software version (and the rest of what it tells about itself) is asked once per start, and again only after
  Home Assistant saw the node restart, instead of on every link (review-4 R4-5). A read is queued once: several links
  in quick succession used to leave one copy per link in the queue, each read in turn, and a read of a link that is
  gone is dropped for the one the next link queues. Unverified on air.
- Another network's proxies no longer wake the search for a proxy node (review-4 R4-8): while Home Assistant had no
  link, every proxy advertisement in range — a neighbour's mesh included — woke the connection loop for a pass over
  every advertisement Home Assistant holds; only one of this network does now. What an advertisement is (this
  network's Network ID, one of its nodes' Node Identity, or nobody's) is worked out once per advertisement content
  (up to 256 kept) rather than once per advertisement: a Node Identity costs one AES per node, about 1.6 ms on a mesh
  of 300. The kept answers are dropped when a key refresh accepts or retires a key and when a node is added.

### Fixed — gateway sync and rewiring

- Taking over the app's upload no longer duplicates a key's rows (review-4 D16, S4-4). The `meta` rows of a key's
  mode, scene and load (`buttonLayoutExports`, `keyModeSceneConfigExports`, `actuatorExports`) and the network
  exclusions were matched by their whole content: Home Assistant setting key 328 to one mode while the app set it to
  another left two rows for the same element, and no conflict was reported. They are matched by element address (by
  IV index) now: one row stays, the app's, and the conflict repair names it. A property test checks the merge on
  random edits of both sides. Unverified on air against the iOS app's import.
- A change made while Home Assistant's own file and the app's upload had both changed is merged instead of refused
  (review-4 D17, S4-6). It failed with *holds a newer export* until the export was fetched again, and fetching it
  again replaced the file without a copy — losing the rooms, scenes and connections Home Assistant had made that the
  devices still use. With the copy of the app's last upload (`<export>.app`) Home Assistant's changes are carried
  onto the app's upload as when only the app changed (the replaced file kept as `.pre-adopt`, conflicts in the
  repair, the result uploaded); only an entry without that copy still refuses. A gateway holding exactly what the
  file holds now counts as synced: an upload whose record a restart lost blocked every later change. A reconfigure
  keeps the export it replaces as `<export>.pre-reconfigure`. Unverified on air.
- What an entry last synced with its gateway (the content digest, the *Last export upload* time) moved from the
  config entry to `.storage/junghome_ble.<entry id>.gateway_sync` (review-4 H I-10): every sync rewrote the config
  entries file, and the sensor listened to every entry update for it. The first start takes the values over; the
  entry keeps its copy as of the upgrade, so after a downgrade to 1.0.0 the first change may ask to fetch the export
  again.
- The *Sensor values for IoT systems* switch can change what it shows (review-4 D19, W4-6). It shows the node's
  answer, but the change was planned against the export: when the app had switched the publication on and the
  export still said off, turning it off sent nothing. A node whose answer differs from the wanted state now gets the
  *Publication Set* even where the export agrees, and the export records it. Unverified on air.
- `remove_device` tells a reset device from an absent one (review-4 D20, W4-7). A device that took the reset but
  whose confirmation was lost was reported as *did not confirm its reset; nothing was changed*. Without a
  confirmation Home Assistant now looks for the device advertising as a new device for 5 s and, seen, records the
  removal; not seen, the error says it *may have been reset* (use `force` once it is gone). The device Home Assistant
  is connected through is refused without `force`. Unverified on air.
- A plan to a device Home Assistant counts as unreachable (its entities unavailable after an unanswered request) is
  refused before anything is sent, naming the devices (review-4 W I5): it used to stop there only after all the
  attempts of that device's first message, with the messages before it applied. Battery devices are never counted
  so. Unverified on air.
- `set_threshold` / `delete_threshold` failures say what was already written (review-4 W4-13): thresholds and
  whole sockets written earlier in the same call were reported as *nothing before it was applied*. A lost link while
  writing a threshold names the socket and the threshold. `set_threshold` checks and reads every socket before it
  writes the first one.

### Fixed — Home Assistant

- The room, key, scene, threshold and *Sensor values for IoT systems* actions, a device rename and the export taken
  over from the gateway for a device the integration did not know no longer reload the integration (decision M5,
  review-4 D23, H4-1, H I-1). Each reload removed every entity first: lights, sockets and sensors went *unavailable*,
  then *unknown*, then back to their state — a `rename_room` that sends nothing on air included — so every `state`
  trigger without `from:` fired again, and every enabled setting was read over the mesh once more. The running
  integration now takes the rewritten export over in place: new entities are added, those the export no longer has
  are removed, the others take their new names, rooms, members and key connections while keeping their states, and
  the Bluetooth link stays up. Whatever it cannot follow with confidence (a device added or removed with Home
  Assistant, other network keys, a device dropped or moved, an error) still reloads, logged at DEBUG with the reason.
  Unverified on air.
- Renaming a device whose rename took over the app's newer export from the gateway no longer holds up the reload
  that follows for 10 s (review-4 W4-10): the rename runs as a Home Assistant background task instead of one of the
  entry's, which the entry's unload waited for.
- Device triggers and logbook lines of a key keep working when its event entity is disabled (review-4 D24, H4-2):
  the `junghome_ble_button_action` bus event was fired by the entity, so disabling `event.<key>` silently stopped
  every device-trigger automation of that key. The hub publishes it now, once per event, without `entity_id` while the
  entity is disabled (the logbook then names the device and key). A key's scene recall publishes
  `junghome_ble_scene_recalled` with the key as its source either way.
- Every hold ends (review-4 R4-7): only a *Generic Delta Set* hold had an end timer; a *Generic Move Set* hold whose
  Move 0 was lost, and a gateway-mode key's hold whose release was lost, never sent `hold_end`, so a dim-while-held
  automation never stopped. Each hold now ends at the latest 30 s after it started, and on link loss and on stop,
  with `reason` (decision M11); a release that still comes after that ends nothing a second time. A gateway-mode
  `hold_start` while a hold runs ends that hold first, as a dimming hold did already. The holds derived from a key
  wired to a dimmer stay unverified on air.
- The device-trigger picker offers what each key can produce (review-4 H I-3, U4-9) instead of all 16 subtypes on
  every key: a key linked to the gateway clicks and holds (and their rocker halves), a key wired to a load, a room or
  another group presses, dims and holds, a key wired to a scene recalls. The key mode the device reported (once its
  *Key mode* sensor is enabled) decides first, then the export's connection; a key neither tells about offers all.
- The configured mesh is no longer offered as a new discovery after a key refresh (review-4 H4-4). Discovery matched
  only the entry's unique id (the Network ID), which follows a refresh only once it completes: mid refresh, and
  after one the export does not have, the proxies were offered as a new *Bluetooth Mesh network* card, which could
  only end at *already set up as another entry*. Discovery now also recognises the Network ID of a refresh Home
  Assistant follows (from its first step on) and the export's nodes by their MAC, aborts as *already configured*,
  and retries an entry still waiting for a proxy at once. When the refresh completes, a discovery of the new Network
  ID still pending is dropped and one you *Ignored* is removed (logged at INFO) before the unique id moves — that used
  to log *already in use* as an error and raise a core repair; the same in *Reconfigure*. The key-refresh cases are
  unverified on air. Discovery still offers any Bluetooth Mesh proxy: narrowing it to JUNG proxies (`manufacturer_id`
  1319) waits for an on-air check (decision M10).
- A stale export gets its own error (review-4 H I-6): when nodes of the export are visible but advertise another
  Network ID — the commonest case after the app renewed the network key — the setup dialog says *the network's keys
  were renewed after the export was made* (`export_keys_stale`), and an entry waiting at setup shows the same reason,
  pointing at a new export and *Reconfigure*, instead of *no node of this mesh network is visible*. Unverified on
  air.
- **Download diagnostics** works while the entry retries or failed (review-4 H4-6): it failed with an error exactly
  when it would help, a proxy out of range or the Bluetooth adapter gone. Such an entry's download shows its state
  and why, what Bluetooth sees (proxy nodes, MACs redacted, and whether each fits the export) and the export's
  summary. Every download now includes the entry's options and the open repair issues; the file path in the
  sequence-number store's last write error is redacted (it can name the user), the error itself kept.
- A device that does not answer logs one warning, when it is marked unavailable (review-4 H4-7): the library logged a
  warning for every unanswered attempt as well, several lines per device at every start. The attempts are debug
  lines now.
- The repair *JUNG HOME devices missing from the export* is translated as a whole (review-4 H4-8): an entry set up from
  the gateway got an English paragraph through a placeholder. It has its own text now, naming the gateway's host.
- The *Link state* sensor is on by default for new installations (review-4 H I-5): it is the mesh's health at a
  glance. An installation that registered it disabled keeps it so; enable it on the *mesh network* device.
- Less work per message on a large mesh (review-4 R4-9, R I-6, R I-10): the device behind an address, a meter's load
  and a tunable-white light's temperature element are looked up in a table instead of a search of every node or
  load (tens of microseconds per lookup with 300 nodes, several per message), the sequence-number store's bound on
  a send is worked out once per write that landed instead of once per sent message (a quarter of a millisecond with
  600 sources in the replay list), and an entity whose state and attributes a status leaves as they are no longer
  writes its state (a busy element has over twenty entities listening; their `last_reported` stays put). A change of
  availability is always written.
- A light, socket, blind or set-point command no longer succeeds when it never reached the load (review-4 D32):
  when its Set was lost on the air while the hub asked the same element for its state (the refresh after a
  reconnection, say), the state reply, still showing the old value, was taken as the command's answer. A reply now
  answers a command only when it shows the state the command asked for (at present or as the target of a running
  transition, within the load's own 1 % or 100 K step) or nothing else waits for it; otherwise the command is sent
  again within its usual attempts. Found by the flapping-link soak over the simulated mesh; unverified on air.
- A device parameter changed in the JUNG HOME app now shows in Home Assistant (review-4 H4-10): the device answers
  such a change to the app only, and each parameter was read once, so Home Assistant kept the old value until the
  next restart or reload. A parameter is now read again on the first connection three hours or more after its last
  read (a battery device's at its first key event after that), through the same paced queue as the first read.

### Added

- The diagnostics list the last 20 links (review-4 R I-9): the proxy node (by mesh address), how long each lasted, why
  it ended, how long its state refresh took and how long sends were held back during it.

- Re-authentication with the JUNG HOME Gateway (review-4 H I-2, U4-5; the quality scale's `reauthentication-flow`).
  When the gateway rejects Home Assistant's access token — on an export fetch, an upload or a status poll — Home
  Assistant now asks for access again with its standard re-authentication card, besides the repair *JUNG HOME Gateway
  no longer accepts Home Assistant*: enter the gateway's network-key password, or leave it empty and approve the
  access request in the app. Only the token is renewed, pinned to the gateway's certificate as before: the export is
  not fetched again, the entry is not reloaded, and the devices keep working throughout. The repair clears on
  success; the action *Sync gateway* then hands over the changes made meanwhile. *Reconfigure → Fetch it again from
  the gateway* still works too. The question is asked once per outage and again after a reload while the repair is
  open; a repair raised before a restart no longer keeps the gateway status polls silent after it. Unverified on air.

- Proxy nodes with Mesh Protocol 1.1 privacy on are followed (review-4 P I-4, P I-5): such a node advertises a
  Private Network Identity or Private Node Identity instead of the Network ID, and sends Mesh Private beacons instead
  of Secure Network beacons. Setup now counts it as in range and the hub connects to it; a Mesh Private beacon moves
  the IV index and proves a key refresh step like a Secure Network beacon; the diagnostics name the private kinds. A
  firmware update turning privacy on used to leave Home Assistant with no proxy to connect to, and without the IV
  index and key-refresh news. Discovery still needs a proxy advertising its Network ID, which a private advertisement
  hides. Unverified on air: the installation's devices do not use privacy.

- Transitions are prepared for lights, *All lights* and scenes (review-4 F4-1), switched off until an on-air probe
  shows which JUNG loads fade: neither the app nor the gateway ever sends a transition time, so a load might ignore
  it or ignore the whole Set. Once a light kind is listed in `light.TRANSITION_KINDS`, its lights declare the
  `transition` feature and HA's `transition` goes into the OnOff, Lightness, CTL or CTL Temperature Set (and into
  *All lights*' Unacknowledged Sets when every member fades); the light is read again a second after the remaining
  time its status announced. `scene.SCENE_TRANSITIONS` does the same for a scene's recall. Until then nothing
  changes on air: no light declares the feature and every Set keeps the bytes it had. Unverified on air.
- `homeassistant.update_entity` reads the device now on every JUNG entity, not only the meter sensors (review-4
  H4-10, F4-7, H I-13): lights, sockets and covers get a state Get, a room thermostat its set-point, temperatures,
  presets, mode and boost, a detector's illuminance its reading, a device parameter its value — so an automation can
  pick up a change made in the app at once. One request per value and device every 2 s at most; a battery device is
  not asked (it sleeps), and without a link the entity keeps its state and the action does not fail. Covers,
  thermostats and detectors are unverified on air.
- Each push-button's insert and key layout (review-4 F4-12). Every JUNG node advertises, without a key, its actuator
  function and button layout; Home Assistant now keeps the latest of each node's records. The node device's model
  names its insert (*Push-button 2-gang (DALI insert)*), a buttons device's model the key layout (*Push-buttons
  (Rocker | Button)*), and each key's event entity has a `position` attribute (`top`, `left_rocker`,
  `right_bottom`, ...), all in Home Assistant's language. Where the export has no insert for a push-button (a
  `MeshNetwork.json` without the app's metadata), the advertised insert decides whether its load is a light or a
  blind; one that advertised nothing is asked once, read-only (its InsertId and button layout), and its answer kept.
  An insert learnt only after the setup shows at once and decides the devices from the next reload.
  A push-button advertising another insert than the export's (the insert was replaced after the export was made)
  raises the repair *JUNG HOME push-buttons with another insert than in the export*. The layout is also read from the
  Android app's share export. Unverified on air: the Gets, and the key positions of the mixed layouts.

- `add_device` shapes a push-button after a node of the export with the insert it advertises when there is one,
  writes the advertised insert into its app device rows instead of the template's, and runs the app's check of the
  number of devices a node yields: a difference is logged and returned in the response (`missing_devices`).
  Unverified on air.

### CLI tools and library

- Transition times (review-4 F4-1): `messages.encode_transition(seconds)` gives the transition-time byte nearest to a
  time at the finest resolution (never the prohibited 63 steps), `decode_transition(byte)` its seconds, and
  `remaining_time(opcode, params)` the time a load's or a Scene Status still announces. `light_lightness_set` and
  `scene_recall` take `transition` / `delay` like the other Sets (without them, the bytes are unchanged), and
  `describe` shows the transition of a Lightness, CTL or Scene Recall Set. `tools/mesh_poc.py set`, `lightness`,
  `ctl` and `scene` take `--transition SECONDS`, a new `delta <addr> <delta>` sends an acknowledged Generic Delta Set
  (also with `--transition`), and `set` shows an OnOff Status's target and remaining time: the probe of
  `docs/hidden-features.md` §11.

- `jhmesh.client.classify_proxy_advert(service_data, keys, unicasts)` is the one classifier of Mesh Proxy service
  data (review-4 P I-4): `ProxyClient.classify_service_data` (so the CLI's scan), the setup check and the diagnostics
  use it, and it knows the private kinds `private-network-id` and `private-node-identity`, whose hashes
  `NetKeyMaterial.private_network_identity(random)` and `private_node_identity(random, address)` compute.
  `ProxyClient` opens Mesh Private beacons with every key it accepts (`SecureNetworkBeacon.private`, logged as
  `private beacon: …`); a key refresh proven by one logs "the proxy's beacon under the new key", as one proven by a
  Secure Network beacon now does. The private beacon is pinned to the specification's sample data (Mesh Protocol 1.1
  §8.4.6) in both directions instead of a test that built it with the code under test; the private identity hashes
  are tested against their formula only, the specification's sample values (§8.6) not being at hand.

- `tools/mesh_poc.py --ha-storage <config>/.storage` refuses every address Home Assistant's sequence store of the
  mesh holds a counter for (review-4 S4-11), as `--source` and as `provision --unicast`: only the integration's
  default `0D00` was refused, not the address it is configured with. `LocalState.persist_now` writes at once; a
  key refresh is persisted through it.
- `ProxyClient` takes an `on_foreign_own_source(iv_index, seq)` callback: a PDU from the client's own address with a
  number it never handed out (`foreign_own_source` holds the highest on the link) is reported at most once a minute
  per link and logged as a WARNING — the CLI says so when another client uses its `--source`. Proxy configuration
  PDUs are header- and replay-checked (`rx_proxy_config_dropped`).
- The CLI's link waits up to a second for the proxy's beacon before its filter request, as Home Assistant's does
  (review-4 R4-10): after an IV Update the request went out under the stored IV index and the proxy dropped it.
- `ProxyClient.request` logs each unanswered attempt at DEBUG (review-4 H4-7); its `quiet` argument is gone, the
  `TimeoutError` after the last attempt is unchanged. The CLI's `-v` output still shows the attempts.
- `CDB.element` / `node_by_addr` look addresses up in an index built on first use (review-4 R4-9): whoever changes
  `CDB.nodes` or an element's address calls `CDB.reindex()` (`ProxyClient.add_node` / `remove_node` do;
  `CDB.index_is_current()` checks it). `Devices.by_meter` is a lookup too, and `Devices.by_temperature` returns the
  CTL light of a temperature element. `ProxyClient.classify_service_data` keeps its answers by service data
  (`CLASSIFY_CACHE_SIZE`). `scripts/bench_mesh.py` times these hot paths against a synthetic mesh of 300 nodes.
- `ProxyClient` bounds the subscription when it attaches and the disconnect when it detaches by `GATT_TIMEOUT`
  (review-4 R4-11): a transport call that never returned held the connection loop, and its connection slot, for
  good. A disconnect that times out is logged as a warning and left behind. Unverified on air.
- Internal: the integration's hub is tested over the simulated mesh of `tests/sim` as well as over the fake proxy
  link (review-4 Q4-19): `tests/test_hub_sim.py` sets the entry up through the simulated proxy node, so relays, the
  proxy filter, the nodes' replay lists and segmentation work as on air, and every test ends on the simulation's
  invariants. A seeded soak (`tests/soak/`, marker `sim`) runs Home Assistant and the hub on virtual time over a
  synthetic network of 80 nodes (300 with `SIM_SOAK=long`) whose proxy keeps dropping the link on a lossy air, and
  checks unique sequence numbers, the link-loss grace, bounded queues and tasks, and a complete refresh once the
  link holds. `tests/test_fake_conformance.py` holds the three proxy fakes to one behaviour (filter type answers,
  segmentation and acknowledgements, replay protection, IV Update): the integration's fake now answers the filter
  type it was asked for, and the library's drops a replayed PDU and fails its test on one.
- Internal: `docs/on-air-sweep.md` is the checklist of the on-air sweep (review-4 brief 30): every behaviour still
  unverified that this installation can check, from watch-only to key-changing, with its steps, captures, pass
  criterion, hardware and whether it can be undone, and what cannot be checked here and why. `tools/on_air.py`
  lists every such marker of the code, docs, ledger and unreleased changelog with the symbol holding it, and with
  `--uncovered` the ones the checklist does not cite.

## 1.0.0

The fixes from the first code review (`docs/review-1/`; every change and its reasoning is in
`10-implementation-log.md` there), then those from the second review (`docs/review-2/plan.md`), then those from the
third (`docs/review-3/plan.md`, which also records what is left).

### Upgrading

- Config entries migrate to version 1.2 on the first start: every entry records where its export came from
  (gateway, upload or a path of your own). Nothing to do.
- The sequence-number store is one file per mesh, `.storage/junghome_ble.seq.<mesh uuid>`, with a record per address
  ever used, so removing and re-adding the integration, or changing the address away and back, continues the
  counters. A 0.2 entry's own store (`.storage/junghome_ble.<entry id>`) is folded into it at setup, and now also when
  the entry never finished setting up or is removed first.
- If you use **Node heartbeats**, switch the option off before removing the integration: a removed entry can no
  longer tell the devices to stop.
- An entry set up from the gateway exchanges nothing with the gateway until the gateway node has confirmed the
  pinned certificate over the mesh (once, on a connection after the update); the log says when it is waiting.
- Two entries for the same mesh (left from an older version, or made by hand) no longer both run: the second one
  fails to set up and a repair issue names both — remove one.
- `.storage/junghome_ble.vault.<mesh uuid>` is new: Home Assistant's provisioner identity and the device keys of the
  devices it added. It holds key material, like the export, and is kept when the entry is removed (so is a copy of
  one that did not read back, `….unreadable.<time>`).
- *Lock operation* switches an earlier version registered disabled are enabled at the first start (it is now on by
  default, as in the app's normal parameter list); one you disabled yourself stays disabled. The *Dim mode* select
  of a tunable-white DALI load, the *LED night mode* switch of a battery wall transmitter and *Time change active*
  (automatic daylight saving time) on a node without a light, socket or blind load are no longer provided; their
  entities are removed at start.
- The per-entry node store (`.storage/junghome_ble.<entry id>.node_versions`) moves to layout 1.2 by itself: it now
  keeps everything a node told about itself, not only its software version.
- *Lock time limit* is kept in seconds now; a limit an earlier version saved in minutes is converted.

### Fixed — mesh safety

- A followed key refresh moves on only on proof that the mesh moved (review-4 D4): the proxy node's beacon secured
  with the new key, or *Key Refresh Phase Status* answers from two devices (or from the proxy node), each sealed with
  the device's own key. A single device could seal NetKey Update / Phase Set 2 / Phase Set 3 with its own device key
  and switch Home Assistant to a key of its choice — transmitting with it, dropping the real one and keeping that
  across restarts — and a refresh the app aborted moved Home Assistant too. A new key learnt meanwhile is only
  accepted; no accepted key is dropped before a proven Phase 3. The log names the proof of each step. A completed
  refresh stored by an earlier build (no proof recorded) is taken up as an accepted key until the proxy's next
  beacon proves it. Unverified on air.
- The IV index follows the spec's timing (review-4 D10): a step of an IV Update (to *in progress* and back to normal
  operation) at least 96 hours after the last change of the IV state, an IV Index Recovery at least 192 hours after
  the last one. Authenticated beacons used to move it as fast as they came — ten beacons each 42 ahead took Home
  Assistant from index 5 to 425, after which every device ignored it and no real beacon was ever ahead of it again.
  The times are kept with the sequence numbers (`iv_changed_at`, `iv_recovered_at`; a record without them restricts
  nothing until its next change), and a clock that jumps back delays the next step by one period at most. A refused
  beacon is logged as a warning once per index.
- *Mesh is at another IV index* can be repaired when Home Assistant is ahead of the mesh: the repair takes it back to
  the mesh's index, continuing above every sequence number it sent from there on (each record now keeps the highest
  number reached under its earlier indexes, `seq_peak`) and guarded over the indexes it used while ahead, written to
  the repair floor first and then to both copies of the store. A record from before this version knows nothing
  below its own index and gets the manual advice instead. The text no longer tells you to remove the address's
  record from the store (the next start went on from the backup copy, or refused); the manual way is a new unicast
  address (Reconfigure). Unverified on air.
- Sequence numbers are never reused: not by segment retransmissions around an IV Update, not after a failed or
  stuck store write, not by a hub still shutting down while its successor starts, and not after restoring from a
  backup copy. The store is written atomically and keeps a backup.
- Restoring a Home Assistant backup no longer reuses sequence numbers (review-4 D5). A backup brings the
  sequence-number store, its backup copy and the repair's floor back together, readable, and the next start resumed
  below every number sent since the backup — reusing AES-CCM nonces, and once the IV index had moved on in between,
  restarting at 0 under the index those numbers went out with. A new backup platform marks every record while a
  backup is being taken (and waits, at most 10 s, until both copies on disk carry the mark); a start that finds a
  mark it did not set logs a warning and continues 2^20 numbers past the record (of 2^24 per IV index, once per
  restore), held until the first beacon names the network's index, writing the repair's floor first. A Home
  Assistant that stops during a backup skips ahead the same way at its next start. Store format 1.3 (an older
  version reads it and ignores the mark). Unverified on air.
- Replayed control messages (heartbeats, segment acknowledgements) are ignored.
- A client that has never learnt the network's IV index adopts the first authenticated beacon, so it can join a
  network whose index is above 42.
- A status a node published while a request was still queued (behind a long segmented message) is no longer taken
  as that request's answer.
- Messages still delivered by a released or replaced Bluetooth connection are dropped.
- Rooms and scenes Home Assistant creates, and the element groups of a device it adds, no longer take the app's own
  next group address or scene number (review-4 W4-2). The app never downloads the project, so it gave its next room
  the same group as Home Assistant's (on the devices, both rooms became one) and its next scene the same number.
  Without the provisioner identity option, Home Assistant now allocates from the top of the app's ranges (its first
  room here is `C64B`, its first scene `6553`), and refuses with a translated error once fewer than 64 free numbers
  are left below the next one. With the option on, nothing changes: Home Assistant's own ranges. The app cannot see
  what Home Assistant added until it imports a file. The library keeps the app's lowest-free rule by default (the
  CLI). Unverified on air.
- A scene number a key still recalls (the app's `keyModeSceneConfigExports` row a deleted scene leaves behind) is no
  longer handed to a new scene, which the old key would then have recalled (review-4 W4-8).
- Removing a device also removes an element group the export knows only from the app's `elementConnectionGroups`
  rows, not by its name (review-4 W4-14).
- A Bluetooth write that never completes is reported as a lost link after 5 s instead of blocking every later send.
- A full replay list refuses new sources instead of evicting old ones (which would have let recorded messages be
  replayed).
- A sequence-number record with out-of-range values counts as damaged (its backup is used) instead of making every
  send fail.
- Upgrading from 0.2 keeps the old sequence-number record when saving it into the mesh's store failed, instead of
  deleting it and restarting the counters.
- A sequence-number store that holds sends back no longer aborts the steps after a connection (time, location,
  energy, heartbeats, scene actions, faults) or the keep-alive: they wait and retry.
- The sequence-number store's backup copy bounds sends like the store itself: when only the backup cannot be
  written, sends are held back instead of running on while the copy stays behind, which a restore from it after
  losing the store would have repeated.
- The CLI's state file keeps its `.bak` copy on the IV index it sends with: a copy that failed to be written at an
  IV Update is written again before the next send, instead of staying on the old index for the next 256 numbers
  (a restore from it went back to that index and then repeated the new one's numbers from 0).
- A CLI start whose first write of the state file fails releases the file again: a retry in the same process no
  longer finds it "in use by another process", its own.
- After the *sequence-number store lost* repair, the counter no longer restarts at 0 when the first beacons take
  it to the mesh's IV index: the numbers the lost record had sent were under that index (or an earlier one), so it
  keeps counting on under every index up to one past the first beacon's (that proxy may still be an update
  behind), and starts over only with the IV Update after that. The guard is stored with the counter (store version 1.2; an older version reads the store and ignores it).
- A second *sequence-number store lost* repair with nothing readable no longer continues from the same number as the
  first one did, repeating every number sent since: the repair records where it continued in
  `.storage/junghome_ble.seq.<mesh uuid>.floor`, and the next one continues 4 194 304 past that. A repair whose
  floor cannot be written changes nothing and the issue stays.
- The *sequence-number store lost* and *devices ignore Home Assistant* repairs, and the documentation, no longer
  suggest restoring the sequence-number store from a backup: an older copy resends numbers the nodes have already
  seen (dropped as replays, and the same AES-CCM nonce used twice). They point to the repair's skip ahead, or a fresh
  address, instead.

- A request queued behind two others got its reply (it was registered on a list a finished request had replaced,
  and timed out). A proxy filter held back by the sequence-number store is sent once the store catches up, and a
  filter request the proxy does not answer is sent again: every group publication used to be lost for such a link.
- A lost or damaged sequence-number store never restarts an address with history from 0: the backup's record is
  used, and when neither copy is usable the setup stops with a fixable repair that continues past every number the
  mesh may have seen. The *devices ignore Home Assistant* repair is fixable the same way.
- Segmented messages no longer hold every other command back while they wait for their acknowledgements, and a
  cancelled send still writes its whole proxy PDU.
- A segment acknowledgement or proxy filter request is no longer sent between the segments of a long message with a
  higher sequence number than the segments after it: the devices dropped those as replays, and the message had to be
  sent again.
- An authenticated beacon with an IV index Home Assistant cannot follow raises a repair instead of being ignored.
- An export saved during a key refresh (`netKeys[].phase` 1 or 2) is used as the Mesh CDB schema says: its old key
  (`oldKey`) is still sent with in phase 1, and proxies and messages under either key are recognised until the
  refresh completes. Only the new key was read, so such an export found no proxy and was not heard.
- Every file that can hold key material is owner-only (0600) whatever the umask: the sequence-number store
  (`.storage/junghome_ble.seq.<mesh uuid>`, with its `.backup` and `.floor`) was written 0644 although a record
  holds the new network key while a key refresh is followed; the next write after the update replaces it 0600. The
  CLI's state file and its `.bak` were world-readable on a default umask for the same reason; one an older version
  wrote is made owner-only when it is loaded. `SECURITY.md` lists every such file, what it holds and its mode.
- A saved export's CDB `timestamp` never goes behind the one it was loaded with (a clock behind the app's phone
  stamped a change older than the file it was made from), each export backup copy is flushed to disk before the
  write that follows it, and two threads saving one export no longer share a temporary file.
- `add_device` (experimental): a device provisioned but not configured or not recorded no longer shares its
  addresses or element groups with the next device added (both sent from the same address, and the mesh dropped one
  as a replay of the other); Home Assistant forgets what its replay protection held for a new device's addresses; it
  provisions only with an IV index a beacon confirmed on the current connection; and the name is checked (and
  numbered like the app numbers a duplicate) before anything goes on air.

### Fixed — gateway sync and rewiring

- The app never downloads the project, so its next upload lacked what Home Assistant had changed: those changes are
  now carried over onto it (a copy of the app's last upload is kept beside the export; where both changed the same
  thing, the app's version wins, and the repair issue *The JUNG HOME app overrode a change Home Assistant made*
  names each entry and what the devices may still hold — review-4 W4-5; the next clean takeover clears it, and
  diagnostics list the entries). Fetching or uploading the export again in Reconfigure drops
  that copy, which is out of date from then on: the next merge took everything the app had changed in between for
  Home Assistant's own changes.
- A device added in the app is fetched from the gateway again (after 1, 5, 15 and then every 60 minutes, and on every
  new link) until the export has it, not once.
- Home Assistant's own mesh address is checked against the export at every setup: a node on it stops the setup, an
  address inside a provisioner's range raises a warning.

- Changes made in the app (renames, timers, deletions) are detected by content, not by the export's timestamp, and
  are no longer overwritten by the next change made in Home Assistant. When both sides changed, the change is
  refused until the export is fetched again. An unreachable or busy gateway no longer gets an unchecked upload,
  and its bare device database is never adopted over the full export.
- A key action that stops partway keeps the room link it has not finished undoing, so running it again completes
  it; a stopped room assignment into a new room records the room.
- A room, key, scene, threshold or removal action that is cancelled partway — an automation in `mode: restart`
  restarting, `script.turn_off`, Home Assistant stopping — records what the devices had accepted, like one a device
  refused, and the entry reloads; before, the export kept claiming the old wiring, and a later `clear_key` or
  `delete_room` left the unrecorded subscriptions on the loads for good (a cancelled `remove_device` after the reset
  left the reset device recorded as present). A write of the export, and the reload after it, run to their end once
  started; while Home Assistant stops, the upload to the gateway is left to `junghome_ble.sync_gateway` or the next
  change. A load leaving a room drops the room-linked keys' groups before the room itself, so a stop in between is
  finished by running the action again.
- A crash or power cut in the middle of such an action is caught up at the next start: the plan and how many of its
  messages the devices had accepted are kept in `.storage/junghome_ble.<entry id>.plan_journal` while it runs (no
  key material), the next setup records them in the export, sets the entry up again from it and raises *A JUNG HOME
  change on … was interrupted*, naming the action to run again. With a gateway, the recorded export is handed on
  by the next change or `junghome_ble.sync_gateway`.
- Exports are written atomically, with a backup.
- Any change to the loaded export file since Home Assistant read it (a rename in the app, a hand edit, a skewed
  clock) refuses the next change, not only one that advanced the file's timestamp.
- The gateway's export adopted for an unknown node is recorded as synced (later room, key, scene and threshold
  actions no longer fail with *the gateway holds a newer export* until a reconfigure), under the same lock, backup
  and checks as every other gateway write; a bare device database never replaces the full export.
- The gateway certificate is no longer adopted from the mesh, where anyone holding a node's keys can answer: the
  first use of a gateway entry compares the pin with the certificate the gateway node reports (`0xC003`), and a
  mismatch — then or later — stops using the gateway and raises *JUNG HOME Gateway certificate changed*. A pin the
  gateway node vouched for cannot be overridden with one click in Reconfigure. A gateway address read from the mesh
  is still followed.
- Reconfigure no longer takes the gateway node's IP address report for its answer about the certificate (and then
  fell back to trusting whatever answered at the address).
- A gateway that rejects Home Assistant's token raises its own repair issue, *JUNG HOME Gateway no longer accepts
  Home Assistant*, pointing to Reconfigure → fetch again, instead of a misleading "check reachability".
- *JUNG HOME export not handed to the gateway* is per entry and removed with it.
- `set_threshold` / `delete_threshold` over several sockets no longer hide an earlier socket's saved change when a
  later one fails; `delete_threshold` on a socket with nothing wired no longer fails; the "not a metering socket"
  error names the socket.
- A key of a battery wall transmitter or battery puck can be wired or cleared: its device's messages go first, it is
  kept awake with the app's keep-alive (a button-layout Get whenever it was quiet for 6 s) while the action runs, and
  a device that does not answer fails with *asleep — press one of its keys, then run the action again* instead of
  asking whether it is powered (a lost link still fails as one). Device parameter changes on those devices work the
  same way. Unverified on air.
- `add_device` / `remove_device` wait for the entry's lock and link like the other actions, and run on the hub a
  previous action's reload left. `add_device` plans the new device's addresses on the export as it is now (the
  gateway's, adopted first, when the app changed the network since the last reload), and its element groups avoid
  every group address the export uses (rooms kept only by the app, room links, live subscriptions), not only the
  listed groups. A `remove_device` whose messages to the other devices stop after the reset still records the
  device as removed (and out of the vault), says so in the error and reloads; a lost link during the reset is a
  translated error.
- A failed upload of the changed export to the gateway is now **tried again twice, 15 s apart**, as the app does.
  Only failures a retry can fix are retried (unreachable, busy, an HTTP error). The next change or
  `junghome_ble.sync_gateway` replaces a pending retry, and the repair issue clears once an upload goes through.
- Socket thresholds follow the app's message sequence as captured on air (not yet tried on a real socket):
  - `set_threshold` writes the threshold before wiring, and each switched load also subscribes its JUNG User
    Property Server (`0x0527:1013`).
  - Disabling a threshold while the other one is inactive unwires the loads and resets the socket's OnOff Client
    publication (`0x0000`, then its group), like the app's disable. Give `devices` again when re-enabling.
  - `delete_threshold` also removes the `0x0527:1013` subscriptions and resets the publication.

### Fixed — Home Assistant

- A service call right after another one's reload waits for the mesh link instead of failing.
- Scenes store the present state of each load (asking the load when Home Assistant does not know it yet), keep
  their actions on the right scene numbers, and drop actions a device no longer has.
- Two meshes with overlapping addresses no longer show each other's detector and battery states.
- Old-firmware detectors report illuminance in whole lux (the device software version is now read, and kept
  across restarts).
- A scene with an all-digit name can no longer be mistaken for another scene's number.
- Setting up a mesh by hand while its discovery card is showing no longer fails at the last step.
- Removing an old gateway or upload entry deletes the export it stored.
- A partly applied change reloads the entry so Home Assistant shows what the mesh now holds; a change that alters
  nothing no longer rewrites the export, uploads it or reloads.
- Clearer errors: what was recorded when a scene action stops, the node and message when the link is lost, and
  "not a load" / "not a key" for our own non-load entities.
- Device parameter changes the device answered neither way, or read back with another value, now fail (*did not
  answer* / *did not take the new value*) instead of reporting success. So does a brightness range the dimmer
  answers with *Cannot Set Range Min / Max*.
- Setting *Switch-on colour temperature* no longer reverts *Switch-on brightness*.
- A device parameter read or change, and a key's key mode or scene write, no longer take another property's
  Status from the same device (a late one, or a battery device's keep-alive answer) for their own answer.
- Removing one channel of a two-channel device from a scene no longer takes a late answer about another scene
  from the other channel for its answer (which kept the scene in the device's register); storing or clearing a
  channel's scene action likewise only accepts a status for that scene.
- An LED colour change on a device whose night mode was not read yet reads it first instead of switching night mode
  off.
- Clear faults sends the acknowledged Health Fault Clear: the two Clear opcodes were swapped in the library.
- Battery nodes get no *Identify* / *Fault* / *Clear faults* entities (they sleep and never answered) and are left out
  of the fault survey; their settings are read right after one of their keys reported, when they are awake.
- A blinds actuator's slat element that also hosts an On/Off server is no longer a spurious light.
- A colour temperature changed elsewhere follows from a Light CTL Temperature Status as well.
- `create_schedule` checks every targeted load for a free slot before it writes anything, so a failure no longer
  leaves the schedule on some loads; a new schedule is written inactive and enabled once its action is in.
  A short schedule list is reported as *did not answer* (not as all slots used); a failed read-back after
  enabling / disabling no longer leaves the *Schedules* sensor stale; schedule errors are translated.
- `set_threshold` without `enabled` keeps the threshold's current state instead of enabling it.
- `store_scene` with a state waits until each load reports the requested values, within one overall deadline and
  without a warning per unanswered Get.
- The heartbeat disable round counts a device as stopped only when it reports heartbeats off, and a concurrent
  re-probe can no longer undo it.
- Deleting a device of an entry that is not loaded works.
- *All thermostats* no longer logs a warning on every unload.
- The gateway import aborts with a message when an entry fails to unload, before it changes anything, and sets the
  unloaded entries up again.
- An uploaded export is removed from Home Assistant's upload folder even when the address check fails.
- A push-button with a blinds insert is a cover even where its elements also host lamp servers: the insert the app
  recorded for the device decides, the composition only when the export has none. A room thermostat's own
  On/Off server is no longer an extra light.
- A node the export marks as being removed (`excluded`) and another maker's node get no devices.
- A timed lock set in the app or elsewhere is read back once it should have ended, as one set from Home Assistant
  is.
- An LED colour and night mode, the fields of an input's edge evaluation, or both ends of a brightness range
  changed at the same moment no longer undo each other.
- A scene lists two members of the same name separately (with their mesh addresses).
- A scene's `members` include the second channel of a two-channel device: its scene action is read from its own
  element, not only from the first channel's (the one the export names for both).
- Room thermostat and detector devices take the name and room the app gave them.
- The battery level of a battery transmitter is kept across restarts (it sleeps, and is only asked after a key
  press); a transmitter that reports no level shows the level of its battery indicator (good 50 %, low 15 %,
  critical 5 %, `level_source` says which).
- A blind's `operation_mode` attribute is always one of its translated states; a mode the table does not know is
  *unknown*.
- A detector's *Continuous on/off* is a read-only diagnostic sensor (off by default) instead of a select: the
  detector's own slider or keys set it, the app never writes it. The old select is removed from the registry.

- A device that was off for a while is asked again every five minutes while the link lasts, instead of staying
  unavailable until the next link; the keep-alive asks devices that can answer (not battery, unreachable or dead
  ones) and no longer drops a healthy quiet link.
- A lost link keeps the entities available for 20 seconds while the next proxy takes over, and a command in that
  time waits for it; a command nothing answers makes Home Assistant probe the link at once. Home Assistant stopping
  closes the link cleanly. The connection loop survives unexpected errors.
- The key-refresh repair is raised for a refresh Home Assistant could not follow (it used to wait for a beacon that
  never comes). Battery wall transmitters no longer get a Status LED switch that could not work.
- Setup and Reconfigure put the network key of a key refresh Home Assistant followed in place of the export's old
  one alike: Reconfigure with an export from before the refresh said no proxy was in range.
- *Lock operation* / *Lock factory reset* and the switch-on delay, switch-off delay and run-on time are written with
  user access 1 (read-only for the User Property server), as the app does on air; they were written with 3, which
  left a node's device lock writable through its User server.
- A command to a light, socket, blind or thermostat is sent the way the JUNG HOME app sends it: an acknowledged
  message, tried up to three times with 3 s per try, until the device's own status answers it. The action returns
  once the device has reported its new state. A device that answers none of the tries fails the action ("… did not
  answer the command (3 attempts in 9 s)") and is shown unavailable right away, as the app shows *No connection*.
  Group commands (rooms, *All lights*, scenes) and blind movements are unchanged.
- A device is marked unavailable as soon as one request goes unanswered through all three tries: the state request
  at connect, or a command. This is the app's rule, and it replaces "three unanswered state requests in a row". A
  device that sent anything while it was being asked is not marked, and neither is one that only missed the link
  watchdog's single keep-alive request or its temperature element's colour-temperature read (below); the first two
  are asked again a minute later. Battery devices are never marked. State and device-parameter reads get three tries
  (the app's) instead of two.
- Changing only the colour temperature sends Light CTL Temperature Set with transition 0, as the JUNG HOME Gateway
  does, so the light no longer uses its own default fade time. After every connection, tunable-white lights are also
  asked for their colour temperature on their temperature element.
- A property reply from a device that carries no value (only the property id, or the id and its access byte) no
  longer clears the value Home Assistant knows; the JUNG HOME app ignores these replies too. A change that a device
  answers this way fails with *does not have the setting*, without a resend or a read-back; before, it counted as
  applied.
- Device parameters are offered where the app offers them: *Dim mode* on dimmer inserts only (not on a tunable-white
  DALI load), no *LED night mode* on battery wall transmitters, and *Time change active* only on a node with a
  light, socket or blind load.
- *Switch-on brightness* and *Switch-on colour temperature* are unavailable while *Use previous brightness* is on,
  as in the app. A change to the switch-on colour temperature sends the switch-on brightness (Light Lightness
  Default) as its lightness, like the app. Until the light has reported its own colour temperature range, the range
  shown is the app's 2000–10000 K (it was 2000–6000 K).
- *Lock time limit* is set in seconds, up to 17999 (4:59:59, the end of the app's picker), instead of in whole
  minutes. *Lock operation* (device lock bit 2) is enabled by default, as in the app's normal parameter list; its
  bit was confirmed on air. The other device-lock flags stay disabled by default.
- *Switch-on delay* and *Switch-off delay* read as the app shows them: the factory value `0xFFFFFFFF` (and anything
  outside 0 to 24 h) is 0, and anything above 4 h is 4 h. Writing works as before.
- A key's **Status LED** switch follows the JUNG HOME Gateway: the gateway sets the LED by sending a User Property
  Status to the key, and that value is now taken as the key's own. Before, it was stored under the gateway and the
  switch never changed.
- *Reset consumption* zeroes the power-on hours (`0x006D`) before the resettable energy total (`0x006A`), in the
  JUNG HOME app's order.
- The scenes the app makes for its timers (`TimerScene …`) no longer get a scene entity, as the app's scene list
  leaves them out; an entity made for one earlier is removed.
- `store_scene` checks the device has room before storing, as the app does: 16 scenes per device, timer scenes
  included, or 8 per channel of a two-channel device. A full device fails the action before anything is sent.
  `remove_from_scene` and `delete_scene` first clear the keys of those devices that are wired to recall the scene
  (as the app does), and `remove_from_scene` drops the device's `sceneInfo` row.
- Names are checked the way the app checks them, for devices, rooms and scenes: a blank name, or a name with a lone
  `%` (anything but `%%` and `%n`), is refused, and a rename longer than the app's rename sheet takes (30
  characters).

### Added

- Following the app's key refresh: the new network key is learnt from the app's own messages to the devices, used
  from phase 2, kept across restarts, and put in place of the export's old key at every setup.
- Per device: *Last seen*, *Signal strength*, *Hops* and *Last restart* diagnostics; firmware version and product id
  on the device page; *Switching cycles* and *Power-on cycles* per light and socket. On the mesh device: *IV index*,
  *Sequence numbers used* and *Mesh sequence numbers used*, with a repair when a sender nears the end of its
  sequence space.
- Energy history after a gap: a metering socket's hourly and daily charts are imported into its Energy statistics
  when they agree with the lifetime counter.
- Colour temperature changes without resending the brightness; a Time Set right after every daylight-saving change.
- `export_network` (administrators): the export as the app's share file or as the mesh database.
- Adding and removing devices from Home Assistant (`find_new_devices`, `add_device`, `remove_device`; experimental,
  behind an option that is off by default, not yet tried on a real device).
- Home Assistant as a provisioner of its own (experimental, behind an option that is off by default, not yet
  imported by any JUNG app): every file it writes or hands to the gateway gets a provisioner entry for Home
  Assistant, with address ranges clear of the app's and Home Assistant's address inside them, and the devices it
  added are put back where the app's upload lacks them. Rooms, scenes and added devices take their addresses from
  its own ranges. With the option off the files are written exactly as before.
- The device key of a device Home Assistant adds is kept in `.storage/junghome_ble.vault.<mesh uuid>` from the
  moment provisioning completes, so a device whose configuration then fails can still be reached (and reset).
- `reset_pending_device` (administrators, experimental, not yet tried on a real device): resets a device `add_device`
  provisioned but could not configure or record, with the device key only Home Assistant holds, and frees its
  addresses; `force` forgets one that was reset by hand or is gone. A repair issue names such devices while there
  are any.

- Device triggers for each half of a gateway-mode rocker (`click_up`, `click_down`, …).
- Blinds can be targets of `assign_key` and `set_room`.
- A *Reset consumption* button per metering socket (diagnostic, off by default) zeroes the energy total the app
  shows and the power-on hours, as the app's "reset consumption" does. The lifetime *Energy* sensor is not reset.
- The app's lock function: a *Lock* switch per light, socket and blind (config, off by default) locks the output
  in its current state against local and remote operation, for the time its *Lock time limit* sets (0 = until
  unlocked, up to 4 h 59 min like the app). Not yet tried on a real device.
- Blinds get the rest of their page in the app: a *Lock function* select (lock, lock-out protection, wind alarm,
  unlock; config, off by default), a *Wind alarm* safety sensor and a *Reference run* diagnostic sensor, and the
  cover stops the slats too. A blind that reports a lock refuses commands with an error that says so instead of
  sending what it would ignore. Unverified on hardware.
- The device lock of every node: *Lock operation* and *Lock factory reset* switches (and *Key lock* / *Lock
  configuration on the device* on room thermostats), config, off by default — which bit is which is not yet
  verified on a device.
- The gateway's node device shows its *IP address* and its API status (*API available*, *Client awaiting
  approval*), read over the mesh once per connection (diagnostic). *Client awaiting approval* is off by default: a
  gateway whose configuration has no pending-name setting reports it on with nothing pending.
- Mini-actuator inputs get the app's edge evaluation: per input an *Edge evaluation* switch and *Rising edge* /
  *Falling edge* selects (no reaction / switch on / switch off / toggle), config, off by default. Not yet tried on
  a real device.
- The app's power-up and switch-on parameters: *Behaviour after mains return* (off / on / previous state) per
  light and socket (config, off by default), and on dimmers *Minimum* / *Maximum brightness*, *Switch-on
  brightness*, *Use previous brightness* and, on DALI inserts, *Switch-on colour temperature* (config, on by
  default). Not yet tried on a real device.
- *All lights* and *All sockets* on the mesh device, the app's central functions: one group message switches every
  lamp (dimmers to the brightness given) or every socket at the same moment. Not yet tried on the installation.
  *All blinds* (position, stop, slats) and *All thermostats* (set-point) do the same for blinds and room
  thermostats — unverified on hardware.
- A device that leaves three state requests in a row unanswered has its entities marked unavailable until it is
  heard from again, the JUNG app's *No connection* rule; a device that misses one is asked again a minute later.
  Settings reads a device might not support do not count.
- Each key's event entity says what the key drives (`connection`: device / room / scene / gateway, with the
  target's address and name), read from the export; a diagnostic *Key mode* sensor per key (off by default)
  shows the mode the device holds.
- Schedules the loads run themselves, the app's *Automation* page: `get_schedules`, `create_schedule` (time of
  day, sunrise or sunset, with the weekdays and what the load does), `enable_schedule`, `disable_schedule` and
  `delete_schedule` on lights, sockets, blinds and thermostats, and a diagnostic *Schedules* sensor per load (off by
  default). A sunrise / sunset schedule first sends the node Home Assistant's home location. Not yet tried on a
  real device.
- Power thresholds of metering sockets, the app's *Automatic* profile: `set_threshold` has the socket switch
  chosen lights and sockets on or off by itself when its power stays at a level for a time, `delete_threshold`
  removes both; a diagnostic *Switch-on* / *Switch-off threshold* sensor per metering socket (off by default) shows
  them. Not yet tried on a real socket.
- Blinds and room thermostats can be stored into scenes and removed from them (`store_scene` /
  `remove_from_scene`): a blind keeps its position and slats, a thermostat its set-point. Unverified on hardware.
- `store_scene` can set the state first (`action`, `brightness_pct`, `color_temp_kelvin`, `temperature`): each load
  is switched, dimmed or set and waited for until it has arrived, then stored as it reports itself.
- *Sensor values for IoT systems* switch per node with a Sensor Server (metering socket, detector, thermostat;
  config, off by default), the app's parameter: whether the node publishes its sensor values.
- A gateway that moved to another address is found again over the mesh (it serves its address and certificate as
  properties of its own node), and the entry follows it; a certificate reported there raises the repair issue
  instead (see above).
- After every connection, right after the time, Home Assistant's home location goes to all nodes, which compute
  sunrise and sunset from it.
- Room thermostats: *Boost* is a preset of the climate entity (read back when the thermostat should have ended it),
  *Automatic operation* its `auto` mode (`heat` is manual), temperatures are shown to 0.5 °C, and an *Open window*
  binary sensor (off by default) shows the thermostat's window-open detection. Unverified on hardware.
- Detectors: a *Walking test* switch (config, off by default) runs the app's walking test and shows the triggered
  PIR zones; the motion held after the detector switched its load lasts the load's run-on time instead of a fixed
  two minutes; the illuminance falls back to the detector's own brightness reading when it sends no Present
  Illuminance. Unverified on hardware.
- Mini-actuator inputs are named *E1* / *E2* as in the app (event entities, device triggers, `assign_key`), and
  each has an *Input state* binary sensor (off by default) that keeps the last on / off the input sent, for a
  contact wired to it. Unverified on hardware.
- Dimming like a held rocker: `start_dim`, `stop_dim` and `step_dim` on lights, and `hold_start` / `hold_end`
  events when a rocker wired to a dimmer is held. Unverified on hardware.
- `assign_key` can make a key recall a scene (`scene`), as the app's scene connection does. Not yet tried on a real
  device.
- The energy puck (switch actuator with energy metering, product 0x10) measures its output as the metering socket
  does: the output's light gets *Power*, *Energy* (and the two diagnostic energy counters), *Installed*, *Reset
  consumption* and the energy-history import, and its *Sensor values for IoT systems* switch; voltage, current,
  power-on time and thresholds stay the socket's, as in the app. The app reads only power, `0x006A` and the charts
  on the puck; the lifetime total `0x0072`, `0x000D` and *Installed* come from the firmware's property list. Where
  the puck's meter says it has no `0x0072` (a Status with the id alone, or no Manufacturer Property Server in the
  export), its *Energy* shows `0x006A` instead; a meter that merely stays silent does not switch. Any load whose
  node has a Sensor Server element at a key location (a meter element) gets the same. Unverified on hardware.
- `audit_network` compares what the devices' mesh configuration holds with the export, without changing anything:
  relay, network transmit, default TTL, beacon and GATT proxy per device, and every model's publication,
  subscriptions and bound AppKeys. It answers the differences per device (the devices that stay silent included);
  the last result per device is in the diagnostics.
- Scenes follow the mesh: the devices' Scene Status keeps each scene's `active_members` (the members whose current
  scene it is, read with a Scene Get after every connection), and a recall by a key, the app or the gateway counts
  as the scene entity's activation. `junghome_ble_scene_recalled` also fires for the app's and the gateway's recalls
  (their address as `source`) and for a recall Home Assistant did not hear, which is only known from the Scene
  Status the devices publish after every recall (no `source`; `reported_by` names the device). The statuses that
  follow a recall Home Assistant did hear fire nothing more.
- `delete_scene` gains `force`, the app's "Delete anyway": a device that cannot be reached or refuses keeps the
  scene, and the scene is removed from the export anyway. New `delete_unused_scenes` action (the app's
  *DeleteUnusedScenes*): deletes from every device's scene register the scene numbers the export does not know, and
  can answer what it deleted per device. New diagnostic *Scenes* sensor per load (off by default): how many of the
  app's scenes the load is in, with their names.
- *Link state* diagnostic sensor on the mesh device (off by default): `bluetooth_off`, `searching`, `connecting`,
  `updating` (connected, the device states are being read), `connected`, `failed`, `disconnected`, the JUNG HOME
  app's connection states.
- Repair issue *No Bluetooth for the JUNG HOME mesh* when Home Assistant has no connectable Bluetooth adapter or
  proxy left. It clears itself when one is back. During setup, the same condition shows as the retry message instead
  of "no proxy node visible".
- DALI inserts get **Minimum colour temperature** and **Maximum colour temperature**, the app's expert "White area"
  (config, disabled by default, 2000–10000 K in 100 K steps). A change sends both ends of the range in one Light CTL
  Temperature Range Set, with the other end as last read. If the light keeps its old range, the change is reported
  as not applied.
- Entries set up from the gateway get the app's **gateway status pages** as diagnostic entities on the gateway
  device, all off by default:
  - sensors: *Firmware version*, *Firmware build*, *Serial number*, *Access requests* (waiting for approval in the
    app), *API clients*, *Error log* (non-debug entries, the latest ten as an attribute) and *Last export upload*;
  - binary sensors: *Network problem*, *Bluetooth mesh problem*, *Cloud problem* and *Cloud connection*.

  They are read from the gateway's REST API (`GET /api/junghome/config` every 30 s, `GET /api/junghome/healthstatus`
  every 5 minutes) only while one of them is enabled. The gateway is asked only once its node has confirmed the
  gateway's certificate over the mesh.
- Renaming a device in Home Assistant renames it in the JUNG HOME app too, the way the app's own rename does:
  lights, sockets, blinds, *Push-buttons* devices, and the node device of a room thermostat or detector. The new
  name goes into `meta.devices[].name` of the export (the node's own Bluetooth name stays), and the export goes to
  the gateway. A name another device already has gets the app's number (a second *Lamp* becomes *Lamp 3*).
  Afterwards the device is named as the app names it. Nothing goes on air and the entry is not reloaded. A name the
  app would refuse raises the *Device name not passed on to the JUNG HOME app* repair issue; the next accepted
  rename clears it.
- *All lights / sockets / blinds / thermostats in &lt;room&gt;* on the mesh device, one per room with such devices:
  the app's central control of an area. A brightness is one unacknowledged *Light Lightness Set* to the room's
  address, and a blind stop is one *Generic Delta Set* 0 to it. On / off, positions, slats and set-points are one
  unacknowledged message per device, as the app sends them. The entities are not placed in the room's area, so an
  action on the area does not reach its devices twice.
- Metering sockets: `homeassistant.update_entity` on a power, voltage, current, energy or power-on sensor reads the
  socket's meter and counters at once instead of waiting for the 5-minute poll. Several sensors of one socket
  updated together share one read.
- Node devices show the manufacturer name and hardware revision the node reports (SIG `0x0011` / `0x0010`, "Albrecht
  Jung GmbH & Co.KG" / `10000000`) next to the firmware. These are read once per node, together with the time role
  and the LBC secure-element / bootloader versions (and a room thermostat's STM32 version); only the software
  version is read on every connection. An item a node answers without a value (it does not have it) is remembered
  as not supported and asked again only after a firmware update, not on every connection. The device diagnostics
  show all of it decoded under `node_info`, the app's node details.
- *Synchronise LED colours* switch on 2-gang push-buttons and wall transmitters, the app's *Synchronise buttons*.
  Switching it on writes the left rocker's colours to both rockers in the app's order (`0xA001`, `0xA004`, `0xA002`,
  `0xA005`). While it is on, a left-rocker colour is copied to the right rocker and the right rocker's colour
  selects are unavailable. Switching it off writes nothing. The flag lives in Home Assistant (restored state), as it
  lives in the app.
- *Sensor values for IoT systems* reads its state from the node, as the app does: one Config Model Publication Get
  per Sensor Server and connection. It is on while one publishes to any address, and shows the export's value until
  the node answers.

### CLI tools and library

- `jhmesh.keyrefresh`: `KeyRefreshFollower` holds the evidence of a followed key refresh and decides when it moves
  (`ProxyClient` feeds it); `LocalState.key_refresh` is a `KeyRefreshRecord` (key, phase, proof, the nodes sent the
  key and those that confirmed each phase) instead of a `(key, phase)` tuple. The stored form keeps its `key` and
  `phase` fields and adds the others when there are any.
- `mesh_poc.py export write` never prints key material.
- `mesh_poc.py provision` no longer prints the new device key: it writes it, with the node's UUID, unicast address
  and element count, to an owner-only `tools/.jhmesh_devkey_<unicast>.json` (or `--key-file`) and prints the path; an
  existing key file is refused before any radio traffic, so an earlier device's key is never overwritten.
- `jhmesh.fileio.atomic_write` is the one atomic writer of every private file (`export.write_private`,
  `write_private_with_backup`, `ProjectFile.save`, `LocalState`'s state file and `.bak`): temp file named after the
  process and thread, created 0600, fsynced, renamed, directory fsynced.
- The decoder names the SIG property statuses after their model (*Generic Manufacturer / Admin / User Property
  Status*, opcodes 0x46 / 0x4A / 0x4E) instead of *SIG Property Status (46)*.
- `mesh_poc.py` Config writes (`config publication` with a group, `subscribe`, `unsubscribe`, `bind`, `unbind`) and a
  `prop set` to a group are refused without `--yes`; a Config write ends with a reminder that the export was not
  updated and `config audit` shows the difference.
- The `jhmesh` package states its licence as an SPDX expression (PEP 639); the sdist and wheel on PyPI are the files
  CI built and checked on the tag.
- `mesh_poc.py config audit` also compares the node-wide states and the AppKey bindings; the library's
  `jhmesh.audit` does the work for it and for `audit_network`, and `config_messages` gains SIG / Vendor Model App
  Get.
- `jhmesh.vault`: Home Assistant's provisioner identity — ranges chosen clear of every other provisioner and of every
  address in use (`choose_ranges`), a provisioner entry and node merged into a project file after the app's — and
  the vault of the nodes it provisioned (device keys, recorded entries), put back into a file that lacks them.
  `CDB.provisioners` lists each provisioner with its ranges; `free_unicast_block(within=)` allocates inside a range.
- `jhmesh.provisioning` provisions a new node over PB-GATT the way the JUNG app does (No OOB, FIPS P-256; checked
  against the specification's sample data), and `jhmesh.commission` plans the app's configuration of the new node
  from a node of the same product in the export — as data, nothing is sent yet. `mesh_poc.py provision --scan`
  lists unprovisioned devices; `mesh_poc.py provision <uuid> --unicast <addr> --yes` provisions one and prints its
  device key (nothing is written to the export).
- Bad input (targets, scene numbers, properties, files, MAC addresses) is a one-line error before anything
  connects, not a traceback.
- `mesh_poc.py` refuses a `--source` in the export's `networkExclusions` or inside any provisioner's allocated
  unicast range, not only a node's address, and suggests a free one. Its default address moved from `0D01`, which a
  second app user's provisioner range (and Home Assistant's own) covers, to `7FFF`, the address the app hands out
  last; the new address starts its own sequence store.
- The sniffer no longer reports retransmitted segments as a second message.
- `CDB.net_key_refresh` holds the `oldKey` and `phase` of a NetKey caught mid key refresh and `CDB.rx_net_keys` both
  of its keys; `ProxyClient` starts in that phase, and the sniffer decrypts traffic under either key.
- The sniffer names every beacon type instead of calling all but the Secure Network beacon foreign: Unprovisioned
  Device beacons (kind `unprovisioned`, with the device's UUID, OOB information and URI hash) and Mesh Private
  beacons, opened with the private beacon key of the export's NetKey (their IV index is followed like a Secure
  Network beacon's); a private beacon no key of ours opens is shown as one of an unknown network.
- A Time Set / Time Status with a zone offset of +24:00 or more (the field goes to +47:45) is shown with its time
  in logs and the sniffer instead of as undecoded bytes.
- Many smaller robustness fixes in the library's parsers and builders; the PyPI package no longer bundles the
  repository README.
- The transaction identifier starts at a random value in every process: a second CLI command within six
  seconds of the first is no longer taken for a retransmission.
- Property ids in logs are named from the catalogue of the server the message addresses (the SIG and JUNG ids
  overlap). A battery level above 100 is unknown. `AccessMessage`'s repr no longer prints the payload (the
  sniffer decrypts key-carrying configuration messages).
- An export nested too deeply, a name that is not text, or a malformed `cid` / `excluded` is refused as an invalid
  export instead of raising an unexpected error.
- `messages.HEALTH_FAULT_CLEAR` / `HEALTH_FAULT_CLEAR_UNACK` had their opcodes swapped (`0x802F` is the acknowledged
  Clear); `health <node> --clear` still sends `0x802F`. `ProxyClient.request`'s `timeout` is documented as the reply
  wait per attempt, on top of the queued send.
- `messages.time_role_get()` and `messages.decode_time_role_status()`. The SIG hardware revision (`0x0010`) decodes
  as text.
- Internal: the tests' fake proxy link fails a test whenever Home Assistant wrote a PDU the mesh could not open
  (wrong key, IV index or nonce), not only when it reused a sequence number; a test that sends such traffic on
  purpose says so (`expect_undecryptable`). The fake's nodes keep one sequence counter per source address, as on air.
- Internal: CI stores its artifacts only on a release tag, so a branch push or a pull request needs no artifact
  storage; the `jhmesh` sdist and wheel are built, checked and tested in one job on the oldest Python
  `requires-python` admits (read from `pyproject.toml`, so Renovate cannot move it); every job starts at once, the
  integration tests run in parallel and list the slowest; actionlint, shellcheck and zizmor check the workflows and
  scripts; the release checks and notes are `scripts/release_checks.sh`, tested on every push.
- Internal: no test waits on the real clock any more, and one taking over 5 s fails (`@pytest.mark.slow_ok` opts
  out); the property timeouts are read from `const` when a Get or Set goes out, so the tests shorten them in one
  place (same values). The registry snapshot pins every fixture network's identities (blinds, RTR, detectors, puck
  and the Android export besides the base network); the fixtures are checked to regenerate byte for byte; the key
  scan covers derived keys, the key-refresh key and every common encoding, in diagnostics and, after a key refresh,
  in files, stores and logs. Two flaky tests are deterministic now: the link-state sequence (its listener ran in the
  executor and read the state late) and the sequence-store line only some random property examples reached; every
  test starts without the Bluetooth manager an earlier one left behind, so the order of a shuffled run no longer
  matters.
- Internal: the parity ledgers (`docs/parity/`) cite code by symbol, `path::symbol` (or `path:line::symbol` into a
  long one), and `tools/parity.py check` fails a citation whose symbol the file does not define or whose line lies
  outside it; the existing line citations, many of which had drifted onto the neighbouring function, were converted
  by reading each line in the tree it was written against. Rows corrected after review 4: behaviour that exists only
  in the CLI is marked *CLI only*, absent behaviour is no longer *implemented*, built behaviour waiting only for an
  on-air check is *implemented* with an *Unverified on air* note, dup chains point at the row that holds the gap, and
  the declined features (Light LC / HSL, Sensor Cadence / Settings / Series / Column, `0x0052`, Default Transition
  Time, virtual addresses, Friend / LPN, Output / Input OOB, proxy filter address lists) cite one-line decision
  records in `docs/roadmap.md`.
