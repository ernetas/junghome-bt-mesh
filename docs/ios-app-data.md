# JUNG HOME iOS app data dump — file formats and findings

Source: iTunes-style backup of the iOS app `de.jung.junghome` v2.2.0 (build 823542)
(`ios/AppDomain-de.jung.junghome/`). The Android app is the same version (2.2.0, build 822956).

The app is built on Nordic Semiconductor's **nRF Mesh** library (iOS: `nRFMeshProvision`; Android: `no.nordicsemi.android.mesh`
— the Android manifest even requests `no.nordicsemi.android.LOG`). Every file format below that is not JUNG-specific is the
nRF Mesh library's own storage, which is open source, so the whole state is readable without the app.

## File inventory

| File | What it is |
|---|---|
| `Documents/MeshNetwork.json` | **The Bluetooth Mesh Configuration Database (CDB)** in the Bluetooth SIG JSON schema `mesh-cdb-1-0-1`. Contains NetKey, AppKey, every node's DevKey, unicast addresses, composition (elements/models), pub/sub config, groups, scenes, provisioner ranges, excluded addresses. This is everything needed to join the mesh from other software. |
| `Library/Preferences/<meshUUID>.plist` | nRF Mesh **sequence-number / replay-protection store** for the network `1BAF3ADE-…`. Keys: `S0001` = next outgoing sequence number of the local element 0x0001 (127851 at dump time); `<addr>` = last accepted SeqAuth from that source; `P<addr>` = previous SeqAuth; `IVIndex {index:0, updateActive:false}`; `provisioner` = local provisioner UUID. |
| `Library/Preferences/3DD16819-….plist` | Same store for an older/other mesh network (provisioner `52689C56-…`). Not the live one. |
| `Library/Application Support/device_metadata.json` | JUNG "device" objects (the things shown in the app UI): name, favourite, and the *identity key* `{nodeId, productIdentifier, actuatorFunction{actuatorFunctionId, insertType}, locationIds[]}`. One mesh node yields several app devices (load side vs button side). |
| `Library/Application Support/groups.json` | Rooms/groups as shown in the app: `{address, name, icon, isFavorite}`. |
| `Library/Application Support/scene_metadata.json` | Scene number → `{name, icon, isFavorite}`. |
| `Library/Application Support/element_connection_groups.json` | `{groupAddress, elementAddress, serverModelIds[]}` — the per-element group allocation ("element group #0x…"). |
| `Library/Application Support/device_type_groups.json` | Device-type groups: `0xFEF5` (all Generic OnOff servers of lights) and `0xFEF8` (sockets). |
| `Library/Application Support/node_gattdata.json` | node UUID → `{peripheralId (CoreBluetooth), macAddress {rawValue: base64(6 bytes), description}}`. |
| `Library/Application Support/nodes.json` | Just the list of node UUIDs. |
| `Library/Application Support/meshnotificationcache.json` | Cache of the last received status per element, base64-JSON payloads (see below). |
| `Library/Application Support/timer.json` | `[]` — no timers configured. |
| `Library/Preferences/de.jung.junghome.plist` | App prefs: `CloudCollection` id, `gatewayAutoSyncOn`, `storedConsumptionSettings` per socket node (price per kWh), theme, consent flags. |
| `Library/Preferences/AppInfoObserver.plist` | `{"version":"2.2.0","buildNumber":"823542"}`. |
| Firebase / Crashlytics / heartbeat files | Telemetry plumbing, irrelevant. |

### The app's share export (`JungHome.json`)
The "Export" button of the iOS app (2.2.0, iOS 27) writes the same `ExportDto` as Android
(`docs/gap-analysis/network-features.md` §8.1): `{"network": "<Base64 of the CDB JSON>", "appVersion": "2.2.0 (823542)",
"version": "1.1", "platform": "iOS (27.0)", "meta": {…}}` — compact JSON, UTF-8 names as-is, no trailing newline,
top-level key order `network, appVersion, version, platform, meta` (Android: `version, appVersion, platform, meta,
network`). The Base64 payload is the CDB with the `{"meshNetwork": …}` wrapper, byte-identical to
`Documents/MeshNetwork.json` above except for the `timestamp` (the network had not changed in the 11 days between
the two). `meta` holds `keyModeSceneConfigExports` (scene id per key element in Scene mode), `devices` (58 app
devices with `deviceId {nodeId, locationIds, insertType, productId, actuatorFunctionId}` and, for keys linked to a
room, `cachedGroupConnectionMetadata[{groupAddress, elementAddress, publishAddress, function: "LIGHT"}]` — the
enum *name*, as on Android), `scenes` (name, number, icon), `buttonLayoutExports` (mode per node: 0/1/2/5 here),
`elementConnectionGroups`, `userGroups` (rooms: name, icon, address), `actuatorExports`; `sceneInfo`, `timer` and
`schedulerMetaInfo` were empty. The provisioner entry carries no sequence number.

Both the HA integration and `jhmesh.export.ProjectFile` load this file directly; a load-and-render round trip
reproduces it byte for byte (plus our trailing newline) — `tests/jhmesh/test_export.py`.

### Swift `Codable` quirks
Dictionaries with non-`String` keys are serialised by Swift as a flat array `[key, value, key, value, …]`
(`device_metadata.json`, `scene_metadata.json`, `node_gattdata.json`, `meshnotificationcache.json`).
Enums with associated values serialise as `{"caseName": {"_0": payload}}`, e.g.
`{"productIdentifier": {"albrechtJung": {"_0": 2}}}` or `{"sig": {"_0": {"genericOnOff": {"_0": {"server": {}}}}}}`.

## Network facts (this installation)

- Mesh name "Standard network", UUID `1BAF3ADE-18C9-4EB3-9488-B3F149C46126`, IV index 0, no key refresh ever (phase 0).
- One NetKey (index 0, `minSecurity: insecure`), one AppKey (index 0) bound to it. Every model on every device is bound to AppKey 0.
- Provisioner "iPhone" node `0001` (CID `004C` = Apple): unicast range `0001–0CCC`, group range `C000–C64B`, scenes `0001–1999`.
- 30 nodes; 554 unicast addresses in `networkExclusions` (devices were re-provisioned many times — excluded addresses stay blocked until an IV Index update).
- All JUNG nodes: CID `0527` (Albrecht JUNG), VID `0001`, default TTL 5, relay+proxy enabled, friend/LPN unsupported, network transmit count 3 / interval 100 ms, relay retransmit 3 / 90 ms, secure network beacon on, `security: insecure` (provisioned without OOB authentication).
- Node UUID = MAC address in EUI-64 form: `30FB10FF-FE30-E756-0000-000000000000` ↔ `30:FB:10:30:E7:56`. OUIs seen: `30:FB:10`, `60:B6:47`, `90:AB:96`, `5C:C7:C1`, `98:0C:33`, `F0:82:C0`, `6C:5C:B1`, `50:32:5F` (gateway).

### Products (pid) and their composition

| pid | Name in app | Elements (location → models) |
|---|---|---|
| `0001` | Push-button 1-gang | loc `0001` load (OnOff/Power OnOff/DTT/Scene servers or Lightness+CTL for DALI); optional loc `0002` second load; loc `0040`(+`0041`) button(s); loc `0044` vendor-only element |
| `0002` | Push-button 2-gang | as above but buttons at `0040`,`0041`,`0042`,`0043` depending on layout; DALI variant has a 2nd element loc `0001` with Generic Level + Light CTL Temperature Server (standard CTL composition) |
| `0003` | Socket | loc `0001` OnOff load; loc `0040` = Sensor Server + Sensor Setup (power metering, publishes to own element group) + OnOff Client + Scene Client (the socket's physical button) |
| `0004` | Switch actuator 1-gang mini | loc `0001` OnOff load; loc `0040`/`0041` binary inputs (OnOff/Level/Lightness/Scene/CTL clients) |
| `000B` | Gateway | single element: Config/Health, Remote Provisioning Server (`0004`), BLOB Transfer Server+Client, Firmware Update Server (`1400/1401/1402`), Time Server/Setup, Sensor Server/Setup/Client, all the generic clients, Scene Client, HSL Client, Property servers/client; vendor `1011/1012/1013/1015` |
| `0009` | (unknown, insertType 2, seen only in stale metadata `F082C0FF-FE62-538A`) | — |

Every element of every JUNG node carries the vendor models `0527:1011`, `0527:1012`, `0527:1013`; load elements add
`0527:1016`, `0527:1017`; button/client elements add `0527:1015`. The phone node registers client models `0527:1015/1016/1017`.
The primary element also carries SIG `100E/100F` (Generic Location), `1200/1201` (Time), `1011/1012/1013` (Generic Property servers), `000E` (SAR Config Server).

### Element "location" = role
The element Location descriptor (GATT namespace) is used as a role tag, and `device_metadata.locationIds` lists which
locations make up one app-level device:

| location | role |
|---|---|
| `0x0001` | load / output 1 (also 2nd element of a CTL dimmer) |
| `0x0002` | load / output 2 (two-output actuator functions) |
| `0x0040`–`0x0043` | buttons / binary inputs A–D |
| `0x0044` | vendor-only element (models `1011/1012/1013/1015` only) — present on every push-button; purpose TBD (status LED? whole-rocker?) |

### App-level per-node attributes (from `meshnotificationcache.json`, notification types)
`id = (elementAddress << 16) | notificationType`; payload is base64 JSON.

| type | payload | values observed / meaning |
|---|---|---|
| 0 | `actuatorFunction {actuatorFunctionId, insertType}` | = vendor user property 0x0002 *InsertId*. insertType 1 = NoInsert (socket, mini actuator, gateway), 2 = GenericInsert (push-button with an insert). actuatorFunctionId (confirmed in `docs/android/properties.md` §2.1): 0 Switch, 1 TwoGangSwitch, 2 Dimming, 3 TwoGangDimming, 4 TwDimming (DALI / tunable white), 5 Blind, 6 Extension ("satellite" insert, no load), 7 NotAvailable, 8 Rtr, 9 Gateway |
| 1 | `deviceLayout` | = vendor admin property 0x5001 *ButtonLayout* (LayoutMode, `properties.md` §2.3): 0 ONE_TOP_ONE_BOTTOM (two keys → elements `0040`+`0041`), 1 ONE_ROCKER (one rocker element `0040`), 2 TWO_LEFT_TWO_RIGHT (four keys `0040..0043`), 3 ONE_LEFT_TWO_RIGHT, 4 TWO_LEFT_ONE_RIGHT, 5 ONE_LEFT_ONE_RIGHT (two rockers `0040`,`0042`). A *rocker* is one element (top = on/up, bottom = off/down); a *key* is one element each. Matches the element lists in this dump exactly |
| 2 | `sceneNumber` | per button element: scene recalled by that button (0 = none) |
| 3 | `mode` | = vendor admin property 0x5003 *KeyMode* per button element (`properties.md` §2.4): 0 Light (OnOff + Level, i.e. switch/dim), 1 Move (blinds), 2 Scene (uses 0x5002 scene config), 3 Property, 4 RTR, 5 Switch (OnOff only), 6 Gateway (publishes to the gateway's element group), 0xFF unknown |
| 4 | `property {propertyId {sig:{_0:26}}, value}` | SIG property 0x001A **Device Software Revision** (Fixed String 8): ASCII `"02020002"` (push-buttons) / `"02020001"` (actuators, sockets) → firmware 2.2.0.2 / 2.2.0.1 (the app splits the digit string into `2.2.0.2`, see `docs/android/properties.md`) |

### Wiring scheme (pub/sub)
- Every element gets a dedicated group address `element group #0x<elementAddr>` (allocated from `C000` upward; rooms are
  interleaved in the same range, e.g. `C00F` WC, `C010` Living room).
- **Load element** server models (`1000`, `1004`, `1006`, `1007`, `1203`, `1204`, and `1300/1303` on dimmers) subscribe to:
  its own element group, its room group(s), the device-type group (`FEF5` lights / `FEF8` sockets), and the element groups of
  buttons that were connected to it. They also *publish* status to their own element group (TTL 255 = default, no period).
- **Button element** client models (`1001`, `1003`, `1302`, `1305`, `1205`, `0527:1015`) publish to either their own element
  group (then loads subscribe to it) or directly to a target load's element group; they subscribe to the same address (so they see status → LED feedback).
- Buttons linked to the **Gateway** publish to the gateway's element group `C005`, which is how the gateway (and hence the
  local REST/WebSocket API) sees rocker events.
- `0527:1013` on every element publishes to and subscribes to that element's own group → a vendor status/event channel per element.
- Socket power sensor (`1100` on loc `0040`) publishes to its own element group `C001`.
- `cachedGroupConnectionMetadata` in `device_metadata.json` records "button → room group" connections:
  `{elementAddress (button), groupAddress (room), publishAddress (a group the button publishes to), groupActuatorFunction}`.

### Scenes
Scene numbers 1–13 (`scene_metadata.json` names, `MeshNetwork.json.scenes[].addresses` = elements where the scene is stored).
"All lights on/off" (2, 3) have no stored addresses — implemented via the device-type group `FEF5` with plain Generic OnOff Set.

### Cross-mapping to the JUNG HOME Gateway API
The gateway receives this same data as the project file the app uploads (`docs/android/network-logic.md` §6.1), so its
REST/WebSocket objects (used by the gateway-based integration `ernetas/junghome`) map 1:1 onto the CDB — derived from
comparing this dump with the gateway's live data of the same installation (`docs/cross-repo-analysis.md` §1):

| Gateway API object | CDB / app-dump equivalent |
|---|---|
| **function** (one controllable thing: OnOff, ColorLight, Socket, button …) | **one mesh element** (`nodes[].elements[]`); its datapoints are that element's server/client models. Counts match: 23 OnOff + 4 ColorLight + 2 Socket functions ↔ the 23 OnOff load elements, 4 CTL elements and 2 socket elements here |
| **group** `id` | `"id"` + the **decimal** group address: `id49186` = `0xC022` "Balcony A" (`groups.json` / `MeshNetwork.json.groups[]`) |
| **scene** `value` | the mesh **scene number** (`scene_metadata.json` key, `scenes[].number`) |
| button `up` / `down` datapoints | `0x5012` key events published to the gateway's element group `C005` (`docs/poc-gatt-proxy.md`) |

## Regenerating the topology report
```
python3 tools/mesh_report.py --anonymise > docs/network-topology.md  # keys redacted, MACs / UUIDs / names pseudonymised
python3 tools/mesh_report.py --keys --out /somewhere/private.md  # with keys (0600) — refused for a path under docs/
```
The tracked `docs/network-topology.md` is the redacted and pseudonymised rendering: it never contains key material,
and the MACs, node / mesh / provisioner UUIDs and room, scene and device names are stand-ins (the same stand-ins as in
the other docs; a MAC keeps its vendor OUI). `--anonymise` without `--salt` draws new address pseudonyms on every run;
names are numbered (`Room A`, `Scene 1`, `Device 1`), so a regenerated page no longer matches the hand-picked stand-ins
the other docs use.

## Security note
`MeshNetwork.json` holds the NetKey, AppKey and all DevKeys in clear text. Anyone with that file can control every device in the
flat and re-configure nodes. Keep the `ios/` directory out of any public repository.
