# Gap analysis — network / project-level features of the JUNG HOME app

Scope: every feature of the JUNG HOME Android app (2.2.0) that configures the *installation* rather than one
device's parameters — rooms, connections, scenes, timers, central functions, naming, gateway, project file /
cloud — and what it would take to do the same from Home Assistant without the app. Per-device parameter screens are
in `device-settings.md`, runtime control and state in `control-and-state.md` (sibling documents in this directory);
they are only cross-referenced here.

Sources: jadx decompile under `android/jadx-out/sources/` (paths below are relative to it; obfuscated package aliases
as in `docs/android/network-logic.md`). Facts already established in `docs/android/network-logic.md` (§ numbers
below refer to it unless another document is named), `docs/android/vendor-models.md`, `docs/android/properties.md`
and `docs/android/transport-provisioning.md` are referenced, not repeated; new claims carry file:line citations.
Bodies that jadx skipped were read from the simple-mode decompile and are marked *(simple-mode)*.

Our code: `custom_components/junghome_ble/jhmesh/*.py` (mesh stack) and `custom_components/junghome_ble/*.py`
(HA integration). Status legend used in every table:

| Status | Meaning |
|---|---|
| `done` | implemented end-to-end in the HA integration |
| `lib` | the `jhmesh` library can already send / parse / read it (or it is derived from the CDB export), but nothing in HA exposes it — or only a builder is missing while the transport path exists |
| `todo` | not possible today; needs new library capability (see §12) |

Key column: **AppKey** = application message encrypted with AppKey 0 (what `ProxyClient.send_access` does today);
**DevKey** = Configuration-Server message encrypted with the node's device key (AKF = 0, nonce type 0x02) — the
library sends them (`ProxyClient.send_config` / `request_config`: device nonce, AKF 0, AID 0, to the node's primary
unicast, segmented above 11 bytes like AppKey messages; builders and status decoders in `jhmesh/config_messages.py`)
and decrypts them on receive (`_deliver` tries `dev_key_of_element`). The rooms, connections, thresholds and
sensor-publication features below send them; `config audit` (`tools/mesh_poc.py`) and the `audit_network` action
read every node's Configuration Server with them.

---

## 0. What the library can do today (baseline for the "Our status" column)

| Capability | Where | Notes |
|---|---|---|
| Load + parse the app's `JungHome.json` (`version/appVersion/platform/meta/network`) or a raw CDB | `jhmesh/cdb.py:49-81` | `meta` is kept as a dict (`CDB.export_meta`); only `devices[].name/deviceId` and `scenes[].name/number` are consumed (`jhmesh/devices.py:100-113`) |
| View of nodes, elements, models, **pub/sub of every model** (`Element.raw_models`), groups, scenes | `cdb.py` | written back by `jhmesh/export.py` (`ProjectFile`: CDB + `meta`, both file flavours) and merged with the app's next upload by `jhmesh/merge.py` |
| AppKey and DevKey access messages, unsegmented and segmented (with Segment-Ack retransmission), request/response matching, group collection | `client.py` (`send_access` / `send_config`, `request` / `request_config`) | TTL 5, AppKey 0 or the node's device key, own unicast from config |
| Builders: Generic OnOff / Level / OnPowerUp / Location / Property, Light Lightness / CTL (incl. Default and Range), Scene Recall / Get / Register Get / Store / Delete, Sensor Get, LBC vendor property **Get and Set**, Time Set, Health; Config messages (`config_messages.py`); JH scheduler and Scene Action Setup (`vendor_models.py`) | `jhmesh/messages.py`, `config_messages.py`, `vendor_models.py` | no SIG Scheduler (`0x1206` / `0x1207`) builders |
| Decoders for SIG status, sensor values, LBC property status (incl. button events 0x5012), Scene Register Status, Config statuses, JH scheduler and Scene Action Setup statuses | `messages.py`, `config_messages.py`, `vendor_models.py` | no SIG Scheduler Status / Scheduler Action Status (`0x824A` / `0x5F`) decoder |
| HA: light, switch, sensor (power/voltage/current), event (buttons), scene (recall); rooms = suggested area | `light.py`, `switch.py`, `sensor.py`, `event.py`, `scene.py`, `entity.py` | see `docs/ha-integration.md` |

---

## 1. Rooms / groups ("areas" in the UI)

Background: §1.2 (address allocation, default rooms, hidden element / device-type groups), §2.4 (membership =
`ConfigModelSubscriptionAdd` of the member's first-element `0x1000`/`0x1002` servers to the room address;
`reconnectSwitchesWithGroup`), §1.7 (`GroupEntity`).

| Feature (UI) | Mesh operations | Key | App-only state (project file / Room DB) | Our status | Notes / HA representation |
|---|---|---|---|---|---|
| **Create area** (Areas tab "+", `app/ui/groups/GroupsFragment.java:118-160`; inline from *Add groups to device*, `app/ui/addGroupsToDevice/…$createGroup$1.java:47`) | none. `CreateGroup` (`domain/interactors/group/CreateGroup.java:38-44`, `$work$2.java:71-82`) → `CheckNameInput(GROUP)` → `MeshUserGroupRepository.R0`: allocate next free group address ≥ `0xC000` (shared counter with element groups, §1.2), `meshNetwork.addGroup`, Room insert | – | CDB `groups[]` entry `{name, address}`; `meta.userGroups[{name, address, icon}]` (`domain/dto/GroupExport.java:11-17`); `GroupEntity.isFavorite` (local only) | `done` (`create_room`; `set_room` creates a missing room too) | HA: an *area*. Creating a room from HA = append a CDB group + `meta.userGroups` entry and re-export; nothing on the mesh until a member is added |
| **Rename area** (live, 300 ms debounce, `GroupDetailsActivity.java:1419`; long-press → Rename) | none. `UpdateGroup.Params.UpdateName` → `MeshUserGroupRepository.save` `:342-394`: Room upsert **and** Nordic `group.setName(); updateGroup()` (`MeshUserGroupRepository.java:382-386`) | – | CDB group name + `meta.userGroups[].name` (both must change) | `done` (`rename_room`) | name uniqueness is case-insensitive (`CheckNameInput`, `MeshUserGroupRepository.java:1332`) |
| **Change icon** (bottom sheet, `app/ui/groupDetail/iconSelection/GroupIconSelectionViewModel.java`) | none. `UpdateGroup.Params.UpdateIcon` → Room only (`MeshUserGroupRepository.java:796-862`) | – | `meta.userGroups[].icon` = file name string, 32 values `ic_group_*` (`domain/item/icon/GroupIcon.java:7,26`; default `ic_group_ground_plan`, unknown → `ic_group_attic`, `GroupIconKt.java:24`) | `todo` (writer) | purely cosmetic; keep whatever is there when rewriting the file |
| **Favourite** (star on the area page, `groupDetail/a.java:32-102` case 0) | none | – | `GroupEntity.isFavorite` — **not exported**, per phone (`domain/dto/GroupExport.java` has no favourite field) | n/a | nothing to sync; HA has its own favourites |
| **Add devices to area** (`app/ui/addDevicesToGroup/AddDevicesToGroupViewModel.java:128-157`; *Add groups to device* is multi-select — a device may be in any number of rooms, `addGroupsToDevice/c.java:75-99`) | `AddGroupToDevices` (§2.4): per new member **`Config Model Subscription Add`** (`0x801B`) of `0x1000` and `0x1002` on the device's *first* element ← room address (or the key-mode server models if a `KeyModeGroupConfig` already exists for this room); then `reconnectSwitchesWithGroup` re-runs `SetGroupFunction` for every button linked to the room → more `Subscription Add` (member's server models ← that button's element group) | **DevKey** | none beyond the CDB subscription lists (the app derives membership from the CDB at read time, `MeshUserGroupRepository.java:78-127`) | `done` (`set_room`, including the re-wiring of room-linked keys; unlike the app it takes the device out of every other room) | HA: assign device to area + subscribe. Without the button re-wiring step, a room-connected rocker will not drive the new load |
| **Remove device from area** (swipe on the area page, `GroupDetailsActivity.java:1554-1561`; unchecking in either multi-select) | `DeleteGroupFromDevices` (§2.4): **`Config Model Subscription Delete`** (`0x801C`) of the room address on the models that carry it; `deleteGroupConnection` additionally unsubscribes the load from every room-linked button's element group and drops that button's `KeyModeGroupConfig` for this room *(simple-mode `RemoveConnectionForAddress$deleteGroupConnection$2`)* | **DevKey** | `meta.devices[].cachedGroupConnectionMetadata` of the affected *buttons* may change | `done` as part of `set_room` (leaving the other rooms) and `delete_room`; no action leaves a device in no room | |
| **Delete area** (dialog "All devices contained remain in the app and operable", `strings.xml` `group_detail_remove_dialog_message`) | `DeleteGroup` (`domain/interactors/group/DeleteGroup.java:70-180`): `DeleteGroupFromDevices(address, all members)` (Subscription Deletes as above), then for every device that holds a `KeyModeGroupConfig` for this room `RemoveConnectionForAddress.GroupConnection` (`DeleteGroupFromDevices$work$2.java:305-330`) — i.e. room-connected buttons lose their publication/subscriptions; finally `meshNetwork.removeGroup` (`MeshUserGroupRepository.java:1197-1200`) + Room delete | **DevKey** | CDB group removed; `meta.userGroups` entry removed; `cachedGroupConnectionMetadata` entries for the room removed | `done` (`delete_room`: members unsubscribed, room-linked keys cleared) | no guard against connected buttons: they simply stop working |
| **Central control inside an area** (button on the area page → `CentralFunctionsActivity(groupAddress)`) | see §5 | AppKey | – | `lib` | |
| Ordering | none; favourites first, then non-empty groups (`ObserveGroups$work$lambda$11$$inlined$compareBy$1.java:15`); no user sort field | – | – | n/a | |

**Recipe (controller):** create = allocate address + CDB/meta entry; membership = DevKey `Subscription Add/Delete`
on the first element's OnOff/Level servers; delete = unsubscribe members, rewire linked buttons, drop CDB group.
Every step ends with re-exporting the project file (§8) or the app will show stale rooms (it rebuilds membership
from the CDB, but the room *list* comes from `meta.userGroups` / CDB `groups`).

---

## 2. Connections (key / rocker / detector / binary input → target)

Background: §2 covers the interactors in full (`SetDeviceConnection`, `SetGroupConnection` + `SetGroupFunction`,
`SetSceneConnection`, locking function, RTR property links, multi-connection, `RemoveConnectionForAddress`,
`GetConnection`). This section adds the UI surface, the exact message set per connection type and the
DevKey/AppKey split. All config messages go to the node's **primary unicast** with its **device key**; all vendor
property writes are **LBC Admin Property Set `C3 27 05`** with AppKey 0 (`vendor-models.md` §3.3).

### 2.1 What the UI offers (`app/ui/devices/connection/**`)

* Sources (`ConnectionSource.java:19-26, 91-95, 153-163`): a control-switch key (`ControlSwitchKeyPosition`, 9
  positions `keyAssignment/ControlSwitchKeyPosition.java:42-59`), a detector, a mini-actuator binary input
  (`BinaryInputNumber` FIRST/SECOND/BOTH, `binaryInputAssignment/BinaryInputNumber.java:26-30`).
* Category cards per source (`ConnectionCategoryViewModel$state$1.java:51-82`):
  detector → *Device, Group*; control-switch key → *Device, Group, Scene, Gateway* + *Locking functions* only for
  whole-rocker positions LEFT/RIGHT/CENTER; mini-actuator input → *Switching, Light, Temperature, Hangings, Group,
  Scene, Gateway, Locking functions*. A "No function" card removes the current connection (`r.java:760-769`).
* Group functions offered (`ConnectionGroupSelectionViewModel$observeDevice$1.java:74-84`): keys and inputs
  `LIGHT / BLIND / SWITCH / LIGHT_AND_SWITCH`; detectors `LIGHT / SWITCH / LIGHT_AND_SWITCH`; RTR-mode links
  `RTR_PROPERTY_MODE` (room must contain an RTR with HVAC-mode support, `SelectGroupListViewModel$selectGroup$1.java:71-114`).
* Target element choice (`p148n6/a.java:161-232`): blind → BLIND or SLAT (only in BLINDS operation mode),
  tunable-white → DIM or LIGHT_TEMPERATURE, lamp → DIM, else MAIN.
* Locking functions (`connection/LockingFunction.java:29-38`): `SWITCH(0), ON(1), OFF(2), LOCKOUT_PROTECTION(3),
  WIND_ALARM(4)`; targets filtered by function (`ConnectionLockingFunctionViewModel.java:70-97`): SWITCH/ON/OFF →
  OnOff-capable devices / `*_PROPERTY_MODE` room functions; LOCKOUT/WIND → blinds / `BLIND_PROPERTY_MODE`; a
  h/min/s duration picker (WIND_ALARM = 0).
* Temperature (mini-actuator only, `app/ui/configuration/deviceParameter/j.java:166-212`): *Temperature control*
  (input drives the RTR set-point, KeyMode RTR(4)), *Eco/Comfort* and *Auto/Manu* (`SetRtrPropertyConnection`
  ECO_COMFORT / AUTO_MANU to an RTR device or a room). Control-switch keys cannot get these.
* RTR → heating actuators ("multi connection", `MultiConnectionActivity`, filter `DeviceFilter.ConnectToRTRDevices`
  = relay outputs: switch lamps, measure lamps, sockets; `ObserveDevices.java:1376-1437`).
* Current assignment display (`ObserveControlSwitchKeyAssignment`, `ObserveBinaryInputAssignment`) is derived from the
  CDB publication address + cached `KeyModeSceneConfig` / `KeySetPropertyMode` via `GetConnection` (§2.7); the raw
  KeyMode property is not re-read for display.

### 2.2 Message sets

| Feature (UI) | Mesh operations (in order) | Key | App-only state | Our status | Notes / HA representation |
|---|---|---|---|---|---|
| **Key → device** (`SetDeviceConnection`, §2.3) | (1) `ResetKeySetPropertyMode`: Admin Set 0x5006 `(0, STATELESS)`, 0x5007 ∅, 0x5008 ∅ per rocker; (2) `RemoveConnectionForAddress.AllConnections`: **Config Model Publication Set** (`0x03`, publish 0x0000) + **Config Model Subscription Delete** (`0x801C`) for every model of the key element except `0x1100`, `0x0527:1011`, `0x0527:1013`; (3) **Publication Set** (clients of the key mode → *target's element group*, ttl 0xFF, period 0, retransmit 0) + **Subscription Add** (`0x801B`, same clients ← same group); (4) Admin Set **0x5003 KeyMode**; (5) sockets: `ConfigurePublicationsForPropertyUser` — `0x0527:1013` PUBLISH_ONLY to own group, rocker clients SUBSCRIBE_ONLY | DevKey (2,3,5) + AppKey (1,4) | none (derived: publication address = a load's element group) | `done` (`assign_key` with a device or entity target) | needs the target's element group to exist (§1.4) — always true for app-provisioned loads |
| **Key → area** (`SetGroupConnection` + `SetGroupFunction`, §2.4) | (1),(2) as above but also `RemoveConnectionForAddress.GroupConnection(button, ownGroup)`; (3) **Subscription Add** on every matching room load: server models for the key mode (first element) ← *button's own element group*; (4) **Publication Set + Subscription Add** of the key's clients ↔ own element group; (5) Admin Set 0x5003 | DevKey + AppKey | **`meta.devices[].cachedGroupConnectionMetadata[{elementAddress, groupAddress, publishAddress, function}]`** (`domain/item/mesh/KeyModeGroupConfig.java:15-24`, `function` = enum name e.g. `"LIGHT_AND_SWITCH"`) — without it the app cannot show the room link and `AddGroupToDevices` will not re-wire new members | `done` (`assign_key` with a `room`) | |
| **Key → scene** (`SetSceneConnection`, §2.5) | (2); **Publication Set** (`0x1205` Scene Client → `0xFFFF`); Admin Set **0x5002 KeyModeSceneConfig** `[scene u16 LE][transition u32 LE = 0]`; Admin Set 0x5003 = 2 | DevKey + AppKey | `meta.keyModeSceneConfigExports[{elementAddress, sceneConfig{sceneId, transitionStepSeconds, transitionResolution, publicationAddress}}]` (`domain/dto/MeshPropertyExport.java:153-161`) — cache the app uses to classify the key without asking the device | `done` (`assign_key` with a `scene`; unverified on air) | |
| **Key → gateway** (`ConnectionGatewayActivity.java:461-476`) | = key → device with the gateway's primary-element group as target and KeyMode **6** | DevKey + AppKey | per-key free-text *note* (see §6) — local only | `done` (`assign_key`, mode `gateway`) | the gateway then sees the key's `0x0527:1015` events (its REST/WebSocket API) |
| **Locking function** (`SetLockingFunctionConnection`, §2.6) | Admin Set 0x5006 `(9 EnforceOutput, STATEFUL)`, 0x5007 = `LockFunction(up)`, 0x5008 = `LockFunction(down)` per rocker; then key → device (KeyMode 3) or key → area (`PropertyModeParams`); Admin Get 0x0009 on the target | DevKey + AppKey | `cachedGroupConnectionMetadata` for the room variant | `todo` | mapping LockingFunction → `[cmd][prio][time]` is in `properties.md` §2.9; `SetLockingFunctionConnection.g()` did not decompile (`SetLockingFunctionConnection.java:297`) |
| **RTR property links** (Eco/Comfort, Auto/Manu; `SetRtrPropertyConnection`, §2.6) | Admin Set 0x5006 `(rtrProperty, STATEFUL)`, 0x5007/0x5008 = `HvacMode` / `RtrMode` values, Admin Set 0x5009 `InputEdgeDetection`; then group (`RTR_PROPERTY_MODE`) or device (KeyMode 3) connection | DevKey + AppKey | as above | `todo` | |
| **Temperature control** (input → RTR set-point) | `ConfigureMiniActuatorConnection.DeviceParams` with KeyMode **RTR(4)** → key → device (clients `0x1003` → RTR's set-point element group) | DevKey + AppKey | – | `todo` | |
| **RTR → heating actuators** (`SetMultiConnection`, §2.6) | per load: **Subscription Add** (load's `0x1000` server ← *RTR's element group*); Admin Set **0x1014 RtrOperationMode = 1** on the load; finally the RTR's `0x1001` client **Publication Set** → its own element group | DevKey + AppKey | none (`GetConnectedDevices$work$1.java:120-160` derives the list from the RTR client's publish address) | `todo` | HA: a climate entity's "controlled outputs" |
| **Remove RTR → load** (`RemoveMultiConnection$work$1.java:174-365`) | Admin Set 0x1014 = 0 on the load (unless another RTR still drives it) and verify; **Subscription Delete** (OnOff server, RTR element group) via `SpecificConnection`; verify by re-reading the CDB | DevKey + AppKey | – | `todo` | |
| **Remove connection** ("No function" card, `ConnectionCategoryActivity$onCreate$3$1$1$3$1$1.java:48-77`) | room / group-lock link → `RemoveConnectionForAddress.GroupConnection(source, room, element, elementGroup)`; device / scene / device-lock link → `AllConnections(source, element, keepExcluded=true)`: **Publication Set 0x0000** + **Subscription Delete** per address (never *Delete All*); room variant also unsubscribes the room's loads from the key's element group | **DevKey only** | drops the `KeyModeGroupConfig`; **KeyMode, KeyModeSceneConfig and KeySetPropertyMode are *not* reset** (no such write in the `RemoveConnectionForAddress*` family) | `done` (`clear_key`) | a key with a stale KeyMode but no publication is "unassigned" in the app |
| **Factory rocker wiring after provisioning** (`ConfigureControlSwitchFunctionality.java:218-248`) | for each factory publication `SetDeviceConnection.AddressParams` → same as key → device | DevKey + AppKey | – | out of scope (provisioning, §10) | |
| **Read back** (`GetConnection`, §2.7) | none | – | CDB pub/sub + `meta` caches | `lib` (we parse `Element.raw_models`; HA only labels buttons, `jhmesh/devices.py:160-163`) | a "what does this key do" attribute on the event entity is cheap |

**Element groups** (§1.4) are the pivot of every connection: a target that was never wired by the app has none.
A controller that adds *new* devices must create them (DevKey `Publication Set` + `Subscription Add` for every
supported server model except the Sensor Server) and record `meta.elementConnectionGroups[{elementAddress, groupAddress}]`
(`domain/dto/ElementConnectionGroupExport.java:10-11`) — otherwise `GetConnection` cannot resolve the key's
publish address and shows the key as unassigned.

---

## 3. Scenes

Background: §1.6 (scene numbers = lowest free in the provisioner's scene range, 1, 2, 3 …), §4.3 (recall / store /
delete / vendor `SceneActionSetupSet`), `vendor-models.md` §4.2 (payload). Icons: 30 values
`domain/item/icon/SceneIcon.java:26` (`SceneDay`, `SceneAbsent` …, stored as the class name string,
`SceneIconKt.java:12-30`).

| Feature (UI) | Mesh operations | Key | App-only state | Our status | Notes / HA representation |
|---|---|---|---|---|---|
| **Recall** (tile, favourite, key, timer) | `Scene Recall Unacknowledged` (`0x8243`) → `0xFFFF` | AppKey | – | `done` (`scene.py`, `coordinator.py:351`) | |
| **Create scene** (name → icon → devices → per-device state pages) | CDB scene is created **before** any device is stored (`CreateSceneViewModel.java:76-90`; `MeshSceneRepository.java:838-852`: `createScene` + `addScene` + Nordic `Scene.setName`); then per device the *store* sequence below; abort → `DeleteScene(force)` | AppKey | CDB `scenes[{name, number, addresses[]}]` (Nordic maintains `addresses` from `Scene Register Status`); `meta.scenes[{name, number, icon}]` (`domain/dto/SceneExport.java:11-17`); `meta.sceneInfo[]` (below) | `done` (`create_scene` + `store_scene`, `mesh_config.py`) | HA: a scene entity per CDB scene; `create_scene` allocates the number, `store_scene` stores the members and writes the export |
| **Store a device's current state** (`StoreDeviceConfigurationInScene$work$2$1.java`) | (1) **`Scene Store`** (`0x8246`, `[scene u16 LE]`, acked → `Scene Register Status 0x8245`) to the element of the node's **first `0x1204` Scene Setup Server** = primary element for every class, incl. both channels of a 2-gang node and the blind (one message, no slat store) (`Y7/q0.java:982-1006`, `C0892b.java:2861`, `RtrDevice.java:2128-2136`); (2) if the device's own element has `0x0527:1017`: **`Scene Action Setup Set`** (`D8 27 05`, `[scene u16][action u8][40-bit payload]`) to **that channel's element** (`Y7/q0.java:1007-1028`, `f0.java:1100-1125`; blind/RTR/TW: first in node) with `Switching / Lightness / LightnessAndCTL / BlindsAndSlatsPosition / TargetTemperature` (`JHSceneAction.java:229-249`) | AppKey | `meta.sceneInfo[{scene, deviceId, infos{blindPosition?, slatPosition?, lightness?, colorTemperature?, temperatureValue?}}]` (`domain/dto/SceneInfoExport.java:11-17`, `Infos.java:11-15`) = the values captured from the device's current capabilities at store time (`M7/i.java:15-41`) — used by the app to *display* legacy (non-vendor) members | `done` (`store_scene`: Scene Store on the node's first Scene Setup Server, Action Set on the channel, read-back when unanswered; verified on `0148`) | the UI puts the device into the wanted state first only for legacy devices (`ToggleDevice`, `configure/u.java:93-112`); for vendor devices the state is carried inline by the Action Set. A controller should send the SIG Set (light on/level), then Store, then Action Set |
| **Capacity check** (`AbstractC0916e.B1()`, `Y7/AbstractC0916e.java:145-190`) | vendor multi-channel node: `Scene Action Setup Get` scene 0 (list) → **< 8 per channel**; else `Scene Register Get` (`0x8244`) → **< 16** | AppKey | `RegisterCapacityEntity` (local cache) | `lib` (decoders for the register and the Scene Action Setup list, `decode_scene_action_status`; `store_scene` checks no capacity first, a full register fails with *Scene Register Full*) | dialogs `scenes_no_capacity_*` |
| **Edit membership** (scene details → edit; `SceneDetailsViewModel.java:129-152`) | every remaining member is **re-stored** (`AddDevicesForScene(scene, all items)`), then `DeleteDevicesFromScene` for removed ones | AppKey | `meta.sceneInfo` rows | `todo` (HA re-stores through `store_scene` per member) | |
| **Remove device from scene** (`RemoveDeviceConfigurationFromScene`, *simple-mode* `:81-298`) | (1) `RemoveConnectionForAddress.SceneConnection` — drops key→scene subscriptions on the node whose cached `KeyModeSceneConfig.sceneId == scene` (DevKey; in practice rarely matches); (2) vendor node: `Scene Action Setup Get(scene)` on every channel of the node; if **both** channels still hold an action → `Scene Action Setup Set(scene, NoAction)` (2-byte payload) to this channel only; else (3) **`Scene Delete`** (`0x829E`) to the Scene Setup Server element; (4) delete `SceneInfo` row | AppKey (+DevKey in 1) | `meta.sceneInfo` | `done` (`remove_from_scene`: steps 2–3 incl. the sibling-channel check; step 1 not needed, key→scene links are reset by `assign_key`) | the SIG register is shared by both channels of a 2-gang node, hence the NoAction dance |
| **Rename** (`UpdateSceneName$work$2.java:74-81`) | none. Room + **Nordic `Scene.setName`** (`MeshSceneRepository.java:157-161`) | – | CDB scene name **and** `meta.scenes[].name` | `done` (`rename_scene`) | case-insensitive uniqueness (`MeshSceneRepository.java:489-495`) |
| **Icon** (`UpdateSceneIcon.java:105-117`) | none | – | `meta.scenes[].icon` | `todo` (writer) | |
| **Favourite** | none | – | `SceneEntity.isFavorite` — not exported | n/a | |
| **Delete scene** (`DeleteScene.java:168-196`) | for every device that has the scene (`Scene.addresses` → all non-attachment devices of those nodes): the *remove device* sequence; then Nordic `removeScene(scene, force)` (refuses while `addresses` non-empty unless forced) + Room delete | AppKey | CDB scene + `meta.scenes` + `meta.sceneInfo` entries removed | `done` (`delete_scene`) | timers are not touched (timer scenes are separate, §4) |
| **Orphan clean-up** (`DeleteUnusedScenes`, run when the SIG timer list opens, `TimerProfilesViewModel$state$1.java:98-115`) | `Scene Register Get`; every register entry unknown to the app and not used by a timer → `Scene Delete` | AppKey | – | n/a | **a scene created by HA that is not in the project file will be deleted from the device the next time the app opens that device's timer list** (owner role only) — one more reason to write the project file back |
| **List / which scenes contain a device** | legacy: CDB `Scene.addresses`; vendor multi-channel: `Scene Action Setup Get(0)` and intersect (`GetScenesForDevice.java:116-146`) | AppKey | – | `lib` (`cdb.scenes`) | |
| **"Automation" for a scene** (`scene_scheduling_*` strings) | opens the timer editor of a member device (§4) | | | | |

Devices that can be in a scene: lamps (all kinds), sockets, blinds, RTR (`SceneCompatible`, `compatibles/n.java`);
mini-actuator *inputs*, detectors, control switches cannot. No transition time anywhere (`A7/d.java:50`).

**Recipe (controller):** number = lowest free ≥ 1 not in CDB `scenes[]`; for each member: SIG Set to the wanted state
→ `Scene Store` (primary element) → `Scene Action Setup Set` (channel element, if `0x0527:1017` present); add
`{name, number, icon}` to `meta.scenes`, the element addresses to the CDB scene's `addresses`, and (for legacy
devices) a `meta.sceneInfo` row; re-export.

---

## 4. Timers and schedules

Background: §6.2 (Time Set, time keeper, `TimeRole`), §6.3 (interactor overview), `vendor-models.md` §4.1 (JH
scheduler bit layout), `properties.md` §1.9 (astro properties 7/8). Two scheduler implementations coexist; the
app picks per device **by model presence only**: `device.X1()` = one of the device's own elements has
`0x0527:1016` → JH scheduler UI, else SIG scheduler UI (`app/ui/devices/d.java:509-533`, `Y7/AbstractC0916e.java:396-414`).
Current firmware (2.2.x) exposes `0x0527:1016` on every load element and no `0x1206/0x1207`
(`docs/network-topology.md`), so **only the JH path matters in the field**; the SIG path is kept for legacy
devices. Both share the same UI (weekday set, hh:mm, astro widget, per-device action page) and a 16-entry limit.

### 4.1 JH vendor scheduler (`0x0527:1016`, `domain/interactors/schedules/*`)

| Feature (UI) | Mesh operations | Key | App-only state | Our status | Notes / HA representation |
|---|---|---|---|---|---|
| **List** (device page → Automation) | `JH Scheduler Get` sub-command 15 (`D2 27 05`, `[F0][centralScheduleId=0]`) → 16 × 2-bit status; then for each used slot `Get` EffectiveTime (sub 2) and Action (sub 1) (`RequestJHSchedules.java:180-207,330-343`). Slots reporting `CentralScheduleIdMatched (1)` or `Available (0)` are hidden (`ObserveJHSchedules$work$1.java:122-136`) | AppKey | none (the device is the source of truth) | `done` (`get_schedules`, the *Schedules* sensor; `schedules.Scheduler.read`; unverified on a device) | HA: the used slots as the sensor's `schedules` attribute and the action's response |
| **Create** (`CreateJHSchedule.java:590-732`; `Params.Create(deviceId, notBeforeHour, notBeforeMinute, dayOfWeek, action, astroTimer?, locationPoint)`) | (1) slot = first `Available` index 0..15 (`:590-608`); (2) astro: `Generic Location Global Set Unacknowledged` (`0x42`, `int32 lat = floor(lat/90·(2³¹−1))`, `int32 lon = floor(lon/180·(2³¹−1))`, `int16 alt`, LE; `p234v7/N.java:22-35`) to the device's Location Setup Server element with the **phone's GPS position** (fallback Berlin 52.516811/13.408333, `V7/r.java:35-37`); (3) **`JH Scheduler Set` Schedule** (sub 0, 8 bytes: type `TimedActive 3 / SunriseActive 5 / SunsetActive 7`, weekday mask, notBefore hh:mm, notAfter hh:mm (31:xx = unset), offset int8 min); (4) **`JH Scheduler Set` Action** (sub 1, 7 bytes: action code + 40-bit payload — `Switching / Lightness / LightnessAndColorTemperature (K) / BlindsAndSlatsPosition / TargetTemperature (°C×100)`, chosen by device class `p044d6/i.java:8-27`) | AppKey | **nothing** — no project-file field; the phone location is not stored either | `done` (`create_schedule`, `schedules.Scheduler.create`; each Set confirmed from its Status or a Get; unverified on a device) | action is inline (no scene). HA: location from Home Assistant's home (`hass.config`) |
| **Edit** (`Params.Update(index, …)`) | same two Sets to the existing index | AppKey | – | `todo` | HA: delete + create for now |
| **Enable / disable** (`ToggleJHSchedule`) | 2-byte type-only `Set` (`[index|sub 0][type]`): `TimedActive ↔ TimedInactive` (3↔2), `Sunrise…` (5↔4), `Sunset…` (7↔6) (`ToggleJHScheduleKt.java:66-76`) | AppKey | – | `done` (`enable_schedule` / `disable_schedule`) | services rather than a switch per slot |
| **Delete** (`DeleteJHSchedule.java:142-143`) | type-only `Set` with type `Available (0)` | AppKey | – | `done` (`delete_schedule`) | |
| **Limits** | 16 slots; minute resolution; astro offset int8 minutes (UI allows up to 2 h 59 min — wire wraps), earliest/latest 5-bit hour with 31 = unset; RTR 5–30 °C | | | | |
| **Time-keeper gate** | legacy PP2 pucks (PID 0x0010..0x0014) need a time keeper (§6.2), checked before the list opens (`JHSchedulerScheduleListViewModel.java:706-737`) | | | | not relevant for current products |

`centralScheduleId` is never set by the app (`V8/d.java:27-35`); slots owned by a "central" scheduler (presumably
the gateway/cloud) are read-only for the app.

### 4.2 SIG Scheduler ("timers", `0x1206/0x1207`, `domain/interactors/timer/*`) — legacy devices

| Feature (UI) | Mesh operations | Key | App-only state | Our status | Notes |
|---|---|---|---|---|---|
| **List** (`GetAllTimer.java:118-155, 228-237`) | `Scheduler Get` (`0x8249`) → 16-bit register; `Scheduler Action Get` (`0x8248`) × 16; Admin Get **8** AstroSchedulerStatus; Admin Get **7** (+1 byte index) for every astro register | AppKey | `meta.schedulerMetaInfo[{schedulerIndex, deviceId, action, scene}]` (`domain/dto/SchedulerMetaInfoExport.java:13-16`) remembers action/scene of *disabled* slots; `meta.timer[…]` (below) | `todo` (no SIG Scheduler builder or decoder in `jhmesh`; `describe` shows the timer messages as raw opcodes) | |
| **Create** (`CreateTimer$work$2.java:137-244`) | index = first slot with all-zero entry (`GetNextAvailableTimerIndex.java:56-84`); **on/off devices**: `Scheduler Action Set` (`0x60`, 10 bytes, standard Mesh layout: index(4) year(7)=100 any, month(12)=0xFFF, day(5)=0, hour(5), minute(6), second(6)=0, dayOfWeek(7), action(4) `TurnOff 0 / TurnOn 1`, transition(8)=0x40, scene(16)=0; `no/nordicsemi/android/mesh/data/ScheduleEntry.java:399-410`); **dim / TW / blind / RTR devices** (`Timer.java:143-153`): first create CDB scene `"TimerScene <index> <deviceId>"` (`MeshSceneRepository.java:194-203`) and run the *store* sequence of §3 with the device's current state, then `Scheduler Action Set` with action `SceneRecall 2` + that scene number; then `Scheduler Get`; then `SchedulerMetaInfo` + `TimerBackwardsCompatible` rows | AppKey | `meta.schedulerMetaInfo`, `meta.timer`, CDB scene + `meta.sceneInfo` for the timer scene | `todo` | timer scenes consume the same 16 scene-register slots (`NoCapacityDialogType.SCENES_AND_AUTOMATION`) |
| **Astro** (`CreateAstroTimer.java:244-290`) | `Generic Location Global Set Unacknowledged` (if the device has a Location Setup Server); `CreateTimer` with hh:mm = "no later than" (23:59 if unset); Admin Set **7** (5 bytes, 40-bit: registerId, mode 1 sunrise / 2 sunset, offset int8, earliest hh/mm, latest hh/mm) | AppKey | as above | `todo` | |
| **Enable / disable** (`UpdateTimerStatus$work$2.java:67-96`) | Admin Set 7 (astro register, or the static default `mode 0, 31:00, 31:00, offset 0`), optional Location Set, `Scheduler Action Set` with **action = NoAction (15)** and all other fields unchanged (disable) or the stored action (enable) | AppKey | `meta.schedulerMetaInfo` (kept while disabled), `meta.timer[].enabled` | `todo` | |
| **Edit** (`UpdateTimer.java:167-224`) | re-store the timer scene (SceneRecall timers), then as *enable* | AppKey | | `todo` | the editor first recalls the scene on the device (`TimerProfileViewModel.java:169-170`) |
| **Delete** (`DeleteTimer.java:147-233`) | `DeleteScene(timerScene)` (§3); Admin Set 7 static default; `Scheduler Action Set` with **all-zero entry** (year 0, months 0, …, action 15, scene 0, `Timer.java:1151-1156`); `Scheduler Get` | AppKey | rows removed | `todo` | |
| Random / every-15-min | domain model knows Nordic `Hour.Random`, `Minute.Every15/20/Random` but no UI writes them | | | n/a | |

`meta.timer[]` (`domain/dto/TimerExport.java:16-45`; `p224u7/d.java:20-37`) is a **write-only denormalised copy** of
every SIG timer including the concrete dim / blind / temperature value the scene hides:
`{"id": "<uuid>", "index", "deviceId", "enabled", "year" (0-99, 100 = any), "month": ["JANUARY", …], "day",
"hour", "minute", "second", "dayOfWeek": ["MONDAY", …], "action" (0/1/2/15), "scene", "additionalDimInfo": {"value",
"temperature"?} | null, "additionalBlindInfo": {"blind", "slat"?} | null, "additionalTemperatureInfo": {"value"} | null}`.
Kept for older app versions / iOS; import only restores the rows (`p224u7/d.java:40-80`).

### 4.3 Thresholds ("Automatic" of measuring sockets, `domain/interactors/timer/CreateThreshold` etc.)

| Feature (UI) | Mesh operations | Key | App-only state | Our status | Notes |
|---|---|---|---|---|---|
| **Create ON / OFF threshold** ("If %sW for %ss, TURN ON/OFF devices…", one of each per socket) | Admin Set **0x5004** (ON) / **0x5005** (OFF), value **8 bytes**: `[u16 LE 0x0081 ActivePowerLoadSide][u16 LE seconds][u24 LE power ×0.1 W, 0xFFFFFF = unset][u8 active]` (`p234v7/L0.java:34-63`; decoder `p057e9/a.java:8-22`); wiring *(simple-mode `CreateThreshold:158-336`)*: socket's `0x1001` OnOff Client **Publication Set + Subscription Add** ↔ socket's own element group; each target load's `0x1000` server **Subscription Add** ← that group | AppKey (property) + **DevKey** (wiring) | none — targets are derived from subscriptions (`ObserveThresholdDevices$work$1.java:104-121`) | `done` (`set_threshold`, `thresholds.py` + `MeshConfigurator.set_threshold_devices`; the client is the meter element's `0x1001`, its own element group the one wired; unverified on a device) | the firmware then sends OnOff Set from the socket's client; HA could do the same with an automation, but the app would not show it |
| **Enable / disable** (`ToggleThreshold.java:199-206`) | same Admin Set with `active` = 0/1, value kept | AppKey | – | `done` (`set_threshold` with `enabled` only: the socket's level and duration are read and kept) | |
| **Delete** (`DeleteThreshold`, *simple-mode* `DeleteThreshold.java:113-169`) | for every target load `RemoveConnectionForAddress.GroupConnection(load, socketElementGroup)` (Subscription Delete); Admin Set 0x5004 and 0x5005 with `value 0xFFFFFF, time 0, active 0` | DevKey + AppKey | – | `done` (`delete_threshold`: both properties first, then the unwiring) | |
| **Read** | Admin Get 0x5004, 0x5005 | AppKey | – | `done` (the *Switch-on* / *Switch-off threshold* sensors, `properties.ThresholdCodec`) | |

"Profile" in the UI is just one automation entry (`TimerViewType {AUTOMATIC, THRESHOLD}`,
`app/ui/timer/profiles/TimerViewType.java:20-22`).

### 4.4 Time, time keeper, daylight saving

| Feature | Mesh operations | Key | App-only state | Our status | Notes |
|---|---|---|---|---|---|
| **Time Set at every app start** (§5.4, §6.2) | `Time Set` (`0x5C`, 10 bytes: TAI seconds 40 bit, subsecond, uncertainty, authority, TAI-UTC delta, zone offset/15 min; `W7/c.java`) → `0xFFFF`, unacknowledged; also unicast to every newly configured device | AppKey | – | `todo` (builder; listed in `docs/ha-integration.md` "Not done yet") | **required for any schedule to fire correctly** if the app / gateway is not around. HA should send it on connect and daily |
| **Time keeper** (legacy PP2 pucks only, `EnsureTimeKeeper.java:68-105`) | keeper: **Publication Set** of `0x1200` Time Server → `0xFEFF` (DevKey) + `Time Role Set = 2` (`0x8239`, AppKey); every PP2 node: **Subscription Add** (`0x1200` ← `0xFEFF`) | DevKey + AppKey | CDB group `0xFEFF "#time_keeper_group#"` | n/a for current products | |
| **Daylight saving** ("Time change active" per device parameter, `strings.xml` `device_parameter_changing_clocks_title`) | Admin Set **15** `[u8 0/1]` (`p234v7/C2357f.java:29-52`), primary channel only | AppKey | – | `todo` (vendor Set) | see `device-settings.md`; the mesh time itself carries the zone offset |
| **Debug time/location screen** (`app/ui/meshnetwork/timeLocation/*`) | `Time Get` (`0x8237`), `Time Set` with a chosen date/time, `Generic Location Global Get/Set` (`0x8225`/`0x41`) | AppKey | – | `todo` | |

---

## 5. Central functions ("Central control")

Background: §4.4. Tabs `RTR / SOCKET / LAMP / BLIND` (`app/ui/centralFunction/CentralFunctionsTab.java:29-37`),
shown only for device classes present (`CentralFunctionsHelper$getAvailableDevices$1.java:63-85`); reachable from
the Home page (whole installation) and from an area page (that room).

| Feature (UI) | Mesh operations | Key | App-only state | Our status | Notes / HA representation |
|---|---|---|---|---|---|
| All luminaires on/off, dim all | `Generic OnOff Set Unacknowledged` / `Light Lightness Set Unacknowledged` → **`0xFEF5`** | AppKey | none — relies on the device-type-group subscriptions made at provisioning (§1.3, §3.4); **no configuration** is ever written by these screens | **done** (*All lights*, `light.JungHomeAllLights`) | HA: a light group / script sending to `0xFEF5`; or plain HA groups over per-device entities |
| All sockets on/off | `Generic OnOff Set Unack` → **`0xFEF8`** | AppKey | – | **done** (*All sockets*) | |
| All blinds up/down/position/stop, all slats | `Generic Level Set Unack` → **`0xFEF6`** / **`0xFEF7`**, `Generic Delta Set 0` for stop | AppKey | – | **done** (*All blinds*, unverified on hardware) | see `control-and-state.md` |
| All RTR set-points | `Generic Level Set Unack` → **`0xFEF9`** | AppKey | – | **done** (*All thermostats*, unverified on hardware) | |
| Inside an area | per-device unicast unacknowledged fan-out; *dim* and *blind stop* go to the **room address** | AppKey | – | `lib` | the room address works because members subscribe their first-element servers (§2.4) |
| Combined "everything off" | does not exist (lamps and sockets are separate buttons) | | | n/a | trivial in HA |

The only relevant thing for a controller that *adds* devices: subscribe the new node's supported servers to the
matching device-type group (`ConnectToDeviceTypeGroup`, DevKey `Subscription Add`), otherwise the app's central
functions skip it.

---

## 6. Device naming, favourites, notes

| Feature (UI) | Mesh operations | App-only state | Our status | Notes / HA representation |
|---|---|---|---|---|
| **Rename device** (long-press / device info; `UpdateDeviceData.Params.UpdateDeviceName`, `UpdateDeviceData.java:293-360`) | **none** — the CDB node name is never changed after provisioning (`grep setNodeName` → only `NodeProvisioningServiceImpl.java:189-193`) | `DeviceEntity.name` → **`meta.devices[].name`** (`DeviceRepositoryImpl.java:810-816`); duplicate names get an auto suffix `"name N"` (`:328-350`) | **done** (`ProjectFile.rename_device` with the app's name check and suffix, `MeshConfigurator.rename_device`; a device renamed in Home Assistant is written through `device_names.py`) | HA: entity/device name. Two names exist per node: CDB `nodes[].name` = BLE name at provisioning (what the app shows for a node without an entity) and the per-*device* name in `meta` (one node can host several devices: two outputs + the button unit, keyed by `deviceId.locationIds`). Writing back = edit `meta.devices[].name` only |
| **Favourite** (device / area / scene) | none | `DeviceEntity.isFavorite`, `GroupEntity.isFavorite`, `SceneEntity.isFavorite` — **none exported** (`domain/dto/*Export.java` have no favourite field) | n/a | per phone; nothing to sync |
| **Notes** ("Note" field, max 300 chars, shown only for a key whose connection targets the **gateway**: `app/ui/devices/controlSwitch/configuration/d.java:407-430`, mini actuator `MiniActuatorConfigurationFragment.java:252-283`; hint "record which function you have stored for this device in your IoT system") | none | `DeviceInfoEntity.positionNotes: Map<keyPosition, String>` (`L7/c.java:30-38`) — **local only**, `DeviceInfoRepository` is not `Exportable` | n/a | free text; nothing for HA |
| Consumption settings (price per kWh, visibility flags, `syncLeds`) | none | `DeviceInfoEntity` — local only | n/a | |
| **Device identity in the project file** | – | `meta.devices[].deviceId = {actuatorFunctionId, locationIds[], insertType, productId, nodeId}` (`domain/dto/DeviceIdentifierExport.java:12-16`), `macAddress`; `meta.actuatorExports[{actuatorId{actuatorFunctionId, insertType}, elementAddress}]`, `meta.buttonLayoutExports[{mode, elementAddress}]` (`MeshPropertyExport.java:16-24, 86-93`) = caches of vendor properties 0x0002 / 0x5001 | `lib` (read) | a new device added by HA must get these rows or the app cannot build its device objects without re-reading the node |
| **Remove device** (long-press → remove) | `ResetDevice` → `Config Node Reset` (§3.5) | everything about the device removed from CDB + `meta` | out of scope (§10) | |

---

## 7. Gateway

Background: §6.1 (key → gateway, sensor publication, 0xC001–0xC003, REST polling/upload),
`transport-provisioning.md` §5.1 (DTOs). The gateway is a normal mesh node; **everything user-facing about it is
HTTPS**, not mesh. Only two URL paths are used by the app (`de/jung/junghome/data/mesh/api/b.java:24-49`): `GET/POST
/api/junghome/config` and `GET /api/junghome/healthstatus`; all commands are `POST config` with a `{"data":{…}}` body.
(Firmware: the gateway's API 1.5.0 has many more routes — those the gateway-based integration `ernetas/junghome`
uses are `GET project/junghome`, **`GET project/cdb`** (returns the mesh CDB *including all keys* to any client with
a token), `PATCH project` (fw 1.5.0+), `config/parameter/version_release`, `functions/:id/datapoints/:id`,
`scenes/:id`, `groups`, `states/:state_id`, plus a WebSocket; the gateway is found over mDNS as `_junghome._tcp`
with a `serial=` TXT record. `docs/cross-repo-analysis.md` §1.4.)

| Feature (UI, `app/ui/project/gateway/**`) | Mesh operations | HTTPS (`Token: <0xC001>`, TLS pinned to 0xC003, base `https://<0xC002>`) | App-only state | Our status | Notes / HA |
|---|---|---|---|---|---|
| **Credentials discovery** (`ConfigureGateway`, app start, on 401/404/503; poll failure) | LBC **Manufacturer Property Get** (`C8 27 05`) 0xC001 token, 0xC002 IP, 0xC003 fingerprint | – | cached in the gateway device's capability | `lib` (`vendor_property_get("manufacturer", 0xC001)` — field-tested read path) | lets HA find the gateway's LAN API without the app |
| **Status page** (Network / Bluetooth Mesh / Cloud indicators, version, build, serial) | – | `GET config` → `GatewayConfigDTO` `{version_release, version_build, system_serial, ip_address, ip_subnet, ip_dns, ip_gateway, ip_mac, ip_dhcp, project_file, cloud_register, cloud_connect, btmesh_device_not_available, btmesh_error, cloud_error, ip_error, api_clients[], api_client_name_asking[]}` (`data/model/GatewayConfigDTO.java:15-67`), polled every 5 s | – | `done` (`gateway_status.py`: diagnostic sensors / binary sensors, polled every 30 s while enabled) | HA: diagnostic sensors |
| **Register gateway in My JUNG Cloud** (`GatewayCloudLoginFragment`) | – | `POST {"cloud_username","cloud_password","cloud_register":true}` (`GatewayLoginUserDTO.java:16-23`) — credentials go to the gateway in clear JSON over the pinned link; the app never talks to the cloud itself | – | out of scope | |
| **Unregister** / **Connect** / **Disconnect** cloud | – | `POST {"cloud_register":false}` / `POST {"cloud_connect": bool}` (`GatewayLogoutDataDTO.java`, `GatewayConnectDataDTO.java`) | – | out of scope | |
| **Network settings** (DHCP switch, IP / subnet / DNS, MAC read-only) | – | `POST {"ip_dhcp", "ip_address", "ip_subnet", "ip_dns"}` (strings `null` when DHCP) (`GatewayRepositoryImpl.java:76-150`) | manual IP override in prefs (`UpdateGatewayDevice.java:107-145`) | `todo` (optional) | |
| **Access permissions** (third-party API clients: list, accept pending, revoke all) | – | `POST {"api_client_accept": "<name>"}` / `POST {"api_client_reset": true}` (`PermissionsDTO.java`, `ResetPermissionsDTO.java`) | – | `todo` (optional) | this is how a REST client (incl. HA via the gateway) gets its own token |
| **Error log** | – | `GET healthstatus` → `[{"level","time","description","details"}]` (`GatewayHealthStatusEntryDto.java:15-25`); DEBUG hidden unless pref `display_gateway_debug_log` | – | `done` (*Error log* sensor: non-DEBUG count, latest ten as an attribute, every 5 min while enabled) | |
| **Updates page** | – | **none** — layout only (`GatewayDetailUpdateFragment`), no listener, no endpoint; the gateway updates itself | – | n/a | |
| **System page**: project-file name, "Last change", **Share project file** button | – | `POST {"data":{"project_file": <ExportDto>}}` (`GatewayProjectFileDTO.java:14-15`); automatic on every change with 2 retries 15 s apart (`ProjectFileSyncServiceImpl$syncProjectFile$3.java:51,62`), only on Wi-Fi; stores `gateway_network_hash = meta.hashCode()` (`K7/i.java:70-76`) | `gateway_upload_failed` → "Project file out of date" dialog (`CheckProjectFileModified$work$1.java:85-119`) | `done` (`MeshConfigurator._upload` after every save, 2 retries 15 s apart as the app, repair issue + `sync_gateway` on failure; *Last export upload* sensor) | HA POSTs the file after every change it makes (§8); the reverse (fetching the gateway's export when an unknown node advertises) covers devices the app adds |
| **Sensor values for IoT systems** page | none — info page only (`GatewayDetailSensorValuesViewModel` just sets a "viewed" pref) | – | – | n/a | the real switch is the per-device parameter (below) |
| **Per-device "sensor values for gateway"** parameter (§6.1, `ConfigurePublicationForSensorServer`) | **Publication Set** of every `0x1100` Sensor Server → the sensor element's own element group (DevKey); off = publish 0x0000; read back with **Publication Get** (`0x8018`) | – | – | `done` (*Sensor values for IoT systems* switch, state from the export) | HA benefits directly: with it on, sockets/detectors/RTRs publish sensor values that HA hears |
| **Global sensor-publication switch** (hidden debug screen) | same for all devices (`MeshNetworkViewModel$togglePublicationForSensorServers$1.java:55-57`) | | | `todo` | |
| **Key → gateway** | see §2 | | | `done` | |
| **Remove gateway** | `Config Node Reset` | | | out of scope | |

Errors: any transport failure is turned into a synthetic HTTP 404 by the interceptor (`p212t7/a.java:61-66`), which
is what triggers the mesh re-read of 0xC001–0xC003.

---

## 8. Project file, cloud and multi-user sync

Background: §6.4 (provisioners, QR sharing, `ExportDto` outline, AES cloud blob, last-writer-wins),
`transport-provisioning.md` §4.5. This section pins down **what a third-party controller must write back** so the
app (and the gateway, which receives the same file) stay consistent.

### 8.1 `ExportDto` — exact shape (Gson, identity field names, `domain/dto/*`)

```
{ "version": "1.1", "appVersion": "2.2.0 (822956)", "platform": "Android (…)",
  "meta": {
    "userGroups":              [{"name", "address", "icon"}],                                  // GroupExport
    "elementConnectionGroups": [{"elementAddress", "groupAddress"}],                          // ElementConnectionGroupExport
    "devices":                 [{"name", "macAddress",
                                 "deviceId": {"actuatorFunctionId", "locationIds": [int], "insertType", "productId", "nodeId": "<UUID>"},
                                 "cachedGroupConnectionMetadata": [{"elementAddress", "groupAddress", "publishAddress", "function": "<enum name>"}] | null}],
    "scenes":                  [{"name", "number", "icon"}],                                   // SceneExport
    "sceneInfo":               [{"scene", "deviceId", "infos": {"blindPosition"?, "slatPosition"?, "lightness"?, "colorTemperature"?, "temperatureValue"?}}],
    "schedulerMetaInfo":       [{"schedulerIndex", "deviceId", "action", "scene"}],
    "timer":                   [ … see §4.2 … ],
    "actuatorExports":         [{"actuatorId": {"actuatorFunctionId"?, "insertType"?}, "elementAddress"}],
    "buttonLayoutExports":     [{"mode", "elementAddress"}],
    "keyModeSceneConfigExports": [{"sceneConfig": {"transitionStepSeconds", "sceneId", "transitionResolution", "publicationAddress"?}, "elementAddress"}]
  },
  "network": "<Base64 (NO_WRAP) of the Nordic mesh CDB JSON>" }
```

(`ExportDto.java:12-16`, `MetaData.java:15-24`; nested DTOs cited in §1, §3, §4, §6). Nothing else: no timestamp,
project id, provisioner id or checksum; `version` is **never read** on import. Not in the file: favourites, notes,
consumption settings, software-version caches, `ProjectEntity`, device-type groups (fixed addresses), phone location.

### 8.2 Flows

| Feature (UI) | What happens | Our status | Notes / what HA must do |
|---|---|---|---|
| **Automatic export** (any local change → 5 s debounce, `ObserveSyncEvent$work$2.java:73-97`) | `CreateProjectFile` → local `<filesDir>/JungHome.json` (pretty JSON) + cloud upload (AES-256-GCM, compact JSON) + gateway `POST project_file` | `lib` (we only *read* the file) | after every HA-side change: rebuild `network` (CDB JSON with the new groups / scenes / pub-sub lists / `seq`) and `meta`, write the file where the app will pick it up (share-via-file import, or the cloud blob if the project is synced, or at least the gateway) |
| **Share via file** ("Project handover", `app/ui/project/shareViaFile/*`) | `ACTION_SEND` of the plain JSON; import via `ACTION_OPEN_DOCUMENT` (extension must be `.json`, `FileServiceImpl.java:99-114`); hidden while cloud sync is on | `lib` | this is how the export we consume is produced today |
| **Backup** (`app/ui/projectSync/backup/*`) | same export, offered when enabling sync | n/a | |
| **Import** (`ImportProject`, `RemoveLocalData$work$2.java:48-65`) | **clears every Room table, replaces the Nordic CDB** (`MeshNetworkRepositoryImpl.java:1105-1107`), re-selects the user's provisioner, keeps `seq = max(stored, imported)`; **no validation** of `version`, platform or mesh UUID; **nothing is sent on the mesh** | – | an HA-written file is accepted as-is; make it complete (every table above) because the import is a full replace |
| **Cloud sync / participants** (`projectSync/**`, `domain/interactors/cloud/*`) | Firebase Storage blob + Firestore members; participant QR `{"id","key"}`; owner creates a provisioner per participant (max 10); callables `createNewProject`, `checkProjectExists`, `createNewAccessRequest`, `acceptAccessRequest`, `assumeOwnerRole` (`data/cloud/api/CloudApiImpl.java:68-254`) | out of scope | HA should **not** become a cloud participant; it can be a plain provisioner entry in the CDB (`transport-provisioning.md` §6) so its unicast is reserved. If the project is cloud-synced, an HA-written file must go into the same encrypted blob or the phones will overwrite it (last writer wins) |
| **Admin / member mode** (`User.Role` OWNER/MEMBER, `domain/item/User.java:49-51`; UI "Admin mode / Member mode", gated by remote-config `role_concept_enabled`) | switching to OWNER first force-imports from the cloud then calls `assumeOwnerRole`; members get a reduced UI, cannot export, cannot run `DeleteUnusedScenes` | n/a | |
| **Multi-provisioner consistency** (`seq`, `networkExclusions`, ranges §1.1) | the app continues its own provisioner's sequence number from the CDB (`seq`) | `done` for our own address (`LocalState`, `client.py:63-131`) | when re-exporting, write our provisioner's current `seq` into the CDB so an app that imports it does not replay-drop us; never touch other provisioners' entries |
| **Key renewal** (hidden screen) | NetKey-only refresh (§6.4) | followed passively: the new key from the app's device-key-sealed Config NetKey Update, both keys accepted, transmit with the new one from Phase 2, old one dropped at Phase 3; kept across restarts until the export has it. A refresh HA did not hear from the start still only raises the `key_refresh` repair hint | an HA-initiated refresh (plan N6) |
| **Reset network** (hidden screen, `ResetNetwork.java`) | `ResetDevice` (Node Reset) for every device, delete the network, `RemoveLocalData` | out of scope | |

### 8.3 Minimum write-back per HA operation

| HA operation | CDB (`network`) | `meta` |
|---|---|---|
| create / rename / delete room | `groups[]` | `userGroups[]` |
| add / remove room member | `nodes[].elements[].models[].subscribe[]` of the member (and of room-linked buttons) | `devices[].cachedGroupConnectionMetadata` of affected buttons (unchanged unless buttons rewired) |
| key → device / gateway / scene | button element models' `publish` / `subscribe` | `keyModeSceneConfigExports[]` (scene links) |
| key → room | button + members' `publish` / `subscribe` | `devices[].cachedGroupConnectionMetadata[]` |
| create / edit / delete scene | `scenes[{name, number, addresses[]}]` | `scenes[]`, `sceneInfo[]` |
| SIG timer | (`TimerScene` scene if any) | `schedulerMetaInfo[]`, `timer[]`, `sceneInfo[]` |
| JH schedule / threshold | – (thresholds: members' `subscribe[]`, socket client `publish`) | – |
| rename device | – | `devices[].name` |
| any of the above | our provisioner's `seq` | – |

---

## 9. Firmware update (out of scope — inventory only)

Mechanics are in `transport-provisioning.md` §5.2 (direct Silicon Labs OTA-DFU over GATT, `.gbl` images bundled in
the APK under `assets/updates/`). The UI (`app/ui/update/**`) offers: an **overview** of rooms with updatable devices
(`CheckForUpdate`, versions compared against the bundled manifests — **local assets, not a server**;
`CheckForUpdate$work$1.java:239-248`), a per-room **device list**, a **per-device update** screen (connect / identify
for battery devices / start / restart / progress / abort; devices are reset and re-provisioned after the update —
dialog "connections will be lost"), and a **forced update** of an unprovisioned device before commissioning
(`AddNewDeviceViewModel$checkForDeviceUpdate$1.java:140`). There is **no "update all"**, no gateway firmware path,
and `app/ui/forceUpdate/**` is the *app-store* forced update, not firmware. Nothing here can or should be done from HA.

## 10. Provisioning and removal (out of scope — one paragraph)

Adding a device = No-OOB provisioning over GATT + the `ConfigureDevice` step machine (AppKey Add, Composition
Data, App Bind list, proxy/TTL/relay/network-transmit, InsertId read, Time Set, element groups, device-type groups,
detector/lamp defaults, GATT proxy off for battery devices; §3, `transport-provisioning.md` §3), followed by the
factory rocker wiring (`ConfigureControlSwitchFunctionality`). Removal = `ResetDevice`: unwire other devices'
links to the node's element groups, `Config Node Reset`, delete everything locally, export (§3.5). Both need a
provisioner role (DevKey generation, unicast allocation from *our* range, CDB write-back) that the library does not
have and that this document does not plan; HA users are expected to commission with the app. Layout changes of a
control switch also re-provision the node (§3.5).

## 11. Hidden mesh-network debug screens (reference only)

Reached by tapping the version text 7 times in Settings (`SettingsFragment$initBinding$1$1$1$1.java:12-19`). They
expose the raw operations the normal UI composes — useful as a reference for what the firmware accepts:
per-model **App Bind**, **Publication Set/Get** (address, TTL, period), **Subscription Add/Delete/Get**, vendor
property read/write, `Scene Register Get`, `Scheduler Get`, `Time Get/Set`, `Time Role Get/Set`, Time-Server pub/sub
(`app/ui/meshnetwork/model/MeshModelViewModel$*.java`); per node **Config Beacon Set**, **GATT Proxy Set**,
**Key Refresh Phase Get**, **Node Reset**, re-run `ConfigureDevice` (`nodeDetails/NodeDetailsViewModel.java:63-66,278-279`);
**Relay Set** / **Network Transmit Set** (`NetworkSettingsViewModel$*.java`); Sensor Descriptor/Cadence/Settings
Get/Set; group on/off and CDB group delete; provisioner create / select / random re-address / **set sequence
number**; local IV-index bump and IV-update test mode; key renewal; reset network; export of log file, Room DB and
the pretty `ExportDto` JSON. No heartbeat, friend, node-identity, App Unbind or TTL controls exist anywhere in the app.
The `use_status_message_optimization` switch is written and read only by this screen and affects nothing
(`MeshNetworkViewModel.java:1258`; §1.4 describes the code path it was meant to select).

---

## 12. Prioritised TODO

Ordered by value for "configure a JUNG HOME installation from HA without the app" and by dependency.

1. ~~**DevKey transport in `jhmesh`** (blocks 1, 2, 4.3, 7): `send_config(node, access_pdu)` = upper-transport
   AES-CCM with the node's device key, nonce type `0x02`, `AKF = 0`, `AID = 0`, destination = node primary unicast;
   segmented like AppKey messages (Publication Set is always segmented: 12/14-byte access PDU + 4-byte MIC > 15);
   `request()` matching on the config status opcodes.~~ **done** (`ProxyClient.send_config` / `request_config`).
2. ~~**Config message builders + status decoders**: `Config Model Publication Set` (`0x03`, 11/13 bytes, layout
   `no/nordicsemi/android/mesh/transport/ConfigModelPublicationSet.java:32-70`) / `Publication Get` (`0x8018`) /
   `Publication Status` (`0x8019`); `Subscription Add` / `Delete` (`0x801B`/`0x801C`, 6/8 bytes) / `Status`
   (`0x801F`); `SIG/Vendor Model Subscription Get` (`0x8029`/`0x802B`); `Model App Bind` (`0x803D`) / `Status`
   (`0x803E`). Vendor model ids on the wire: CID LE then model LE.~~ **done** (`jhmesh/config_messages.py`).
3. **Project-file writer**: serialise the CDB back to Nordic JSON (groups, scenes, per-model `publish`/`subscribe`,
   our provisioner `seq`) and rebuild the `meta` block (§8.1) — the app *and the gateway* need it. Include a
   "diff against the export the user loaded" so the integration can refuse to overwrite a newer app export.
4. **Time Set on connect + daily** (`0x5C` → `0xFFFF`; layout in §6.2) — cheap and required for schedules.
5. **Rooms** (§1): create / rename / delete via CDB + `meta`; membership via `Subscription Add/Delete`; re-wire
   room-linked buttons (`cachedGroupConnectionMetadata`) when members change.
6. **Vendor property Set builder** (LBC Admin Property Set `C3 27 05`, payload `propId u16 LE ‖ 0x03 ‖ value`):
   unlocks KeyMode (0x5003), KeyModeSceneConfig (0x5002), KeySetPropertyMode/Values (0x5006–0x5008), thresholds
   (0x5004/0x5005), astro register (7), daylight saving (15), RtrOperationMode (0x1014) — shared with
   `device-settings.md`.
7. **Connections** (§2): key → device / room / scene / gateway and removal, as the exact sequences above; expose
   as HA services (`junghome_ble.assign_key`, `junghome_ble.clear_key`) plus a read-only "assignment" attribute on
   the event entity derived from the CDB.
8. **Scenes** (§3): `Scene Store` (`0x8246`), `Scene Delete` (`0x829E`), `Scene Register Get` (`0x8244`) builders;
   `Scene Action Setup Set/Get` (`D8/D6 27 05`) with the 40-bit action payloads (bit packer of `vendor-models.md`
   §4.0); capacity checks (16 / 8 per channel); HA services `create_scene`, `store_scene_member`, `delete_scene`.
9. ~~**JH scheduler** (§4.1): `JH Scheduler Get/Set` builders and status decoders (Schedule, Action, EffectiveTime,
   ScheduleList); `Generic Location Global Set` (`0x41/0x42`) from `zone.home`~~ **done** (HA: the
   *Schedules* sensor + services `get_schedules` / `create_schedule` / `enable_schedule` / `disable_schedule` /
   `delete_schedule`). SIG scheduler (§4.2) only if legacy devices are found in a real
   installation (none in the captured network).
10. ~~**Thresholds** (§4.3): property Set (item 6) + wiring (items 1–2).~~ **done** (`set_threshold` /
    `delete_threshold`, the two threshold sensors).
11. **Gateway HTTP client** (§7): read `0xC001–0xC003` over mesh (`lib`), `GET config` diagnostics, `POST
    project_file` after every write-back (needed for the gateway to see HA-made changes), optionally permissions.
    With a token, `GET project/cdb` also hands over the whole CDB (keys included) — an alternative to the phone
    export for the config flow (mDNS `_junghome._tcp` discovery → permission request → fetch), see `roadmap.md` step
    14 and `cross-repo-analysis.md` §5 "CDB from the gateway".
12. ~~**Sensor-server publication switch** (§7, DevKey `Publication Set` of `0x1100`): improves HA's own sensor coverage.~~
    **done**.
13. Central-function group entities (§5) and blind / RTR support — tracked in `control-and-state.md`.

## 13. Library capabilities that must be added first

| Capability | Needed by | Wire facts |
|---|---|---|
| ~~DevKey upper-transport encryption + `send_config` / `request_config`~~ **done** | rooms, connections, thresholds, sensor publication, element groups | nonce `0x02`, AKF 0, AID 0, dst = node unicast; statuses arrive DevKey-encrypted (already decrypted by `_deliver`) |
| ~~`ConfigModelPublicationSet` / `Get` / `Status`, `ConfigModelSubscriptionAdd` / `Delete` / `Status`, `Config*ModelSubscriptionGet`, `ConfigModelAppBind` / `Status`~~ **done** (`config_messages.py`) | same | opcodes `0x03`, `0x8018`, `0x8019`, `0x801B`, `0x801C`, `0x801F`, `0x8029`/`0x802B`, `0x803D`, `0x803E`; publish params `ttl 0xFF, period 0, retransmit 0, appKeyIndex 0, credential 0` |
| LBC Admin Property **Set** builder (`C3 27 05`) and value packers for 0x5002, 0x5003, 0x5004/5, 0x5006–0x5008, 0x5009, 7, 15, 0x1014 | connections, thresholds, astro, DST | `vendor-models.md` §3.3; segmented when value > 5 bytes |
| `Scene Store` / `Scene Delete` / `Scene Register Get` + `Scene Action Setup Set/Get/Status` | scenes, SIG timers | `0x8246`, `0x829E`, `0x8244`, `0x8245`; vendor `D8/D6/D7 27 05` |
| `JH Scheduler Get/Set/Status` (sub-commands 0, 1, 2, 15) + LSB-first bit packer | JH schedules | `D2/D4/D3 27 05`, `vendor-models.md` §4.1 |
| `Scheduler Action Set/Get`, `Scheduler Get` + status decoders (still missing: neither builders nor decoders exist) | legacy SIG timers | `0x60`, `0x8248`, `0x8249`, `0x5F`, `0x824A`; entry layout §4.2 |
| `Time Set` (+ `Time Get`, `Time Role Set`) | schedules, time keeper | `0x5C`, `0x8237`, `0x8239`; TAI epoch 2000-01-01, TAI−UTC 37 s |
| `Generic Location Global Set/Get` | astro schedules | `0x41`/`0x42`/`0x8225` |
| CDB **writer** (groups, scenes, `publish`/`subscribe`, provisioner `seq`) + `ExportDto`/`meta` writer/merger | every configuration feature | §8.1; Base64 NO_WRAP of the Nordic JSON in `network` |
| Gateway HTTPS client (token header, pinned fingerprint, `GET config`, `POST config {project_file}`) | gateway sync | `transport-provisioning.md` §5.1, §7 above |
| Group-address / scene-number allocator honouring the provisioner ranges and the shared `0xC000…` counter | rooms, scenes | §1.1, §1.2, §1.6 |
