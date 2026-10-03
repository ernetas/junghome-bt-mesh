# JUNG HOME Android app — mesh network-configuration logic

Source: jadx decompilation of the JUNG HOME Android app (`de.jung.junghome`). All paths are relative to
`android/jadx-out/sources/`; line numbers refer to the decompiled `.java` files there. Obfuscated packages
used below: `Y7/*` = device domain classes (`Device.kt` = `Y7/AbstractC0916e`, `BlindDevice.kt` = `Y7/C0892b`,
`DimLampDevice.kt` = `Y7/f0`, `LampDevice.kt` = `Y7/i0`, `SocketDevice.kt` = `Y7/p0`,
`TunableWhiteLampDevice.kt` = `Y7/t0`, `ControlSwitchDevice.kt` = `Y7/AbstractC0914c`, `Gateway.kt` = `Y7/g0`),
`p056e8/*` = `e8` domain items (`ElementConnectionGroup.kt` = `p056e8/b`, `Group.kt` = `p056e8/e`, `KeyMode.kt` =
`p056e8/i`, `Property.kt` = `p056e8/r`, `Scene.kt` = `p056e8/w`), `p065f8/*` = `f8` (`MeshElement.kt` = `p065f8/d,e`,
node = `p065f8/f`, `LocationId.kt` = `p065f8/c`), `p096i8/*`, `p024b9/*`, `p085h8/*`, `p278z8/*` = capability data
classes, `p234v7/*`, `D7/*`, `p254x7/*` = `*MessageBuilder.kt` (capability → Nordic `MeshMessage`),
`V7/*` = domain types (`Types.kt` = `V7/C0851h`, `V7/m`), `L7/*` = Room entities, `T7/h.java` = Koin DI module.
`grep -m1 'compiled from' <file>` gives the original Kotlin file name of any obfuscated class.

Several Kotlin-coroutine `work()` bodies did not decompile in the default jadx run ("Method dump skipped" /
`JadxOverflowException`). Those were recovered with
`jadx --decompilation-mode simple --show-bad-code --no-imports --no-res -d <out> android/xapk/de.jung.junghome.apk`
and are cited as `(simple-mode: <class>.<method>)`; the referenced statements are quoted inline so they can be
re-checked.

Companion documents: `transport-provisioning.md` (BLE transport, No-OOB provisioning, proxy filter, message queue,
TTL), `vendor-models.md` (vendor 0x0527 models, opcodes, property layouts) and `properties.md` (full device-property
catalogue with value encodings and enums). This document covers the
**addressing / pub-sub wiring, connections, post-provisioning configuration, control paths, state and gateway /
sharing logic** and only summarises the others where needed.

---

## 1. Address allocation

### 1.1 Provisioner ranges (unicast / group / scene)

A new provisioner is created by `MeshNetworkRepositoryImpl.h3(meshNetwork, name)`
(`de/jung/junghome/data/repositories/MeshNetworkRepositoryImpl.java:1001-1054`):

| Range | Size computed by `j3()` (`MeshNetworkRepositoryImpl.java:553-570`) | Nordic call | Result for the first provisioner |
|---|---|---|---|
| Unicast | `min((0x7FFF − 1)/10, free) = 3276` (`:1003`, bounds `1..END_UNICAST_ADDRESS`) | `nextAvailableUnicastAddressRange(3276)` → `[low, low+3276]` (`no/nordicsemi/android/mesh/MeshNetwork.java:72-77`) | `0x0001..0x0CCD` (the iOS app uses `0x0CCC`, i.e. `low+size−1`) |
| Group | `min((0xFEF4 − 0xC000)/10, free) = 1611` (`:1009`, upper bound `C0851h.f4829a.f1973a − 1 = 0xFEF4`) | `nextAvailableGroupAddressRange(1611)` → `[0xC000, 0xC64B]` (`MeshNetwork.java:23-27,540-548`) | `0xC000..0xC64B` (matches the iOS export) |
| Scene | `min((65535 − 1)/10, free) = 6553` (`:1004`) | `nextAvailableSceneAddressRange(6553)` → `[1, 6554]` (`MeshNetwork.java:48-53,550-558`) | `0x0001..0x199A` (iOS: `0x1999`) |

Every provisioner additionally gets the fixed **device-type range `0xFEF5..0xFEFF`** added to its group ranges
(`MeshNetworkRepositoryImpl.java:1029-1045`: if no provisioner already owns exactly `[0xFEF5, 0xFEFF]` it is
`addRange`d; `V7/C0851h.java:16-20` defines it as `END_GROUP_ADDRESS − 10 .. END_GROUP_ADDRESS`, i.e.
`65269..65279`, `V7/m.java:10` = `[0xC000, 0xFEFF]`). Group ranges of a provisioner that has none are repaired in
`T1()` (`:640-642`).

The provisioner's own unicast address is **random** inside its unicast range, avoiding `networkExclusions` and
existing nodes (`g3()`, `MeshNetworkRepositoryImpl.java:741-791`). Its TTL is the Nordic default 5 (see
`transport-provisioning.md` §3.6).

Node unicast addresses at provisioning time: `nextAvailableUnicastAddress(nodes.size(), provisioner)` with a
`+1` retry (`NodeProvisioningServiceImpl$startProvisioning$1.java:66-92`, see `transport-provisioning.md` §3.3).

### 1.2 Group address classes

All application groups live in the selected provisioner's **first** allocated group range and are allocated
with Nordic's `createGroup(provisioner, name, range, forbidden)` =
`nextAvailableGroupAddress()` — the lowest address in the range that is neither in the network's group list nor
in the *forbidden* list (`no/nordicsemi/android/mesh/MeshNetwork.java:874-886`, `:902-915`). The forbidden list
is `ForbiddenGroupAddressesService.a()` = all addresses in the Room table `GroupEntity` (rooms) ∪ all addresses in
`ElementConnectionGroupEntity` (element groups) (`de/jung/junghome/data/repositories/f.java:39-130`; entities
`L7/e.java`, `L7/d.java`). Rooms and element groups therefore share one sequential counter starting at `0xC000`
(in the iOS export: rooms `C00F`, `C010`, … interleaved with element groups `C005`, `C059`, `C061` …).

| Class | Address | Name in mesh CDB | Created by |
|---|---|---|---|
| **Room / user group** | next free in provisioner range (≥ `0xC000`) | user-typed name; 5 default rooms from `res/values/arrays.xml` `group_defaults` (*Living room, Kitchen, bathroom, bedroom, children's room*) created when no non-element, non-device-type group exists | `MeshUserGroupRepository.R0()` (`de/jung/junghome/data/repositories/MeshUserGroupRepository.java:594-621`), defaults at `:420-458`; DI list `"DefaultGroups"` (`T7/g.java:140-145`) |
| **Element group** | next free in provisioner range | `"element group #" + elementAddress` — *decimal* element address (`de/jung/junghome/app/ui/timer/profiles/d.java:54-56` is `str + int`; the iOS app writes `#0x…`) | `MeshElementConnectionGroupRepository.n1(elementAddress)` (`de/jung/junghome/data/repositories/MeshElementConnectionGroupRepository.java:596-763`): returns the cached `ElementConnectionGroupEntity(elementAddress, address)` if present (`:697-709`, `L7/d.java:15-17`), else `createGroup(selectedProvisioner, name, firstRange, forbidden)` + `addGroup` + insert into Room (`:728-747`) |
| **Device-type group** | fixed `0xFEF5..0xFEF9` (`V7/C0851h.java:20`) | `"device type group #" + address` (decimal, e.g. `device type group #65269`) | `DeviceTypeGroupRepositoryImpl.D1()` creates the five groups if missing (`de/jung/junghome/data/repositories/e.java:23-37`), called from `PrepareMeshNetwork` after network creation (`de/jung/junghome/domain/interactors/networking/PrepareMeshNetwork.java:199`) |
| **Time-keeper group** | fixed `0xFEFF` (`MeshAddress.END_GROUP_ADDRESS`) | `"#time_keeper_group#"` | `MeshUserGroupRepository.Z0()` (`MeshUserGroupRepository.java:780-792`); used as the Time Server publish address of the time keeper (`de/jung/junghome/domain/interactors/configuration/TimeKeeperConfiguration.java:117,715`, `EnsureTimeKeeper.java:344`) — see §6 |
| **All-nodes** | `0xFFFF` | – | Scene Client publish address for scene keys (§2.5) and always in the proxy white list (`transport-provisioning.md` §3.3) |

Element groups and device-type groups are hidden from the user: the room list filters out names starting with
`"element group #"` and addresses in `C0851h.f4830b` (`MeshUserGroupRepository.java:420-436`).

### 1.3 Device-type groups — full list

`AbstractC0916e.x1()` returns the list of device-type groups a device subscribes to (default empty,
`Y7/AbstractC0916e.java:500-502`). Overrides:

| Address | Constant | Device classes (Kotlin file) | Which models subscribe |
|---|---|---|---|
| `0xFEF5` (65269) | `C0851h.f4829a.first` | `LampDevice` `Y7/i0.java:15,42`; `DimLampDevice` `Y7/f0.java:205-208,304`; `SwitchLampDevice` `Y7/q0.java:188-191,283`; `MeasureLampDevice` `Y7/n0.java:212-215,320`; `TunableWhiteLampDevice` `Y7/t0.java:218-221,322` | all "supported server" models of the node (§1.4) |
| `0xFEF6` (65270) | `C0892b.f5743r0 = first+1` | `BlindDevice` `Y7/C0892b.java:1602-1606,1710` | supported servers on the **blind (position) element** = element of the node's first `GenericLevelServer` (`ConnectToDeviceTypeGroup.java:146-152`, `Y7/C0892b.java:1711-1724`) |
| `0xFEF7` (65271) | `C0892b.f5744s0 = first+2` | `BlindDevice` | supported servers on the **slat element** = `c2()` = `GenericLevelServer` on the last element of the device's range (`ConnectToDeviceTypeGroup.java:137-145`, `Y7/C0892b.java:1725-1751,1976-1982`) |
| `0xFEF8` (65272) | `p0.f6054p = first+3` | `SocketDevice` (`Y7/p0.java:18,65`) and its subclasses `SwitchSocketDevice`/`SwitchMeasureSocketDevice` (`Y7/s0`, `Y7/r0`) | all supported servers |
| `0xFEF9` (65273) | `RtrDevice.f46242w0 = first+4` | `RtrDevice` (`de/jung/junghome/domain/item/devices/RtrDevice.java:70`) | all supported servers |
| `0xFEFA..0xFEFE` | – | reserved in the range, unused | – |
| `0xFEFF` | – | time-keeper group (§1.2) | Time Server publication |

Control switches, detectors, mini actuators and the gateway have **no** device-type group (`x1()` not
overridden). Subscription is done by `ConnectToDeviceTypeGroup` (`de/jung/junghome/domain/interactors/configuration/ConnectToDeviceTypeGroup.java:185-190`
collects `e.b(node.elements, supportedServers)`, `:134-171` sends `ConnectToAddress(..., SUBSCRIBE_ONLY)` per
address). The app sends *all-off* / *all-on* style "central functions" to these addresses (§4). Messages to a
device-type address that has no matching device are silently ignored by the messenger
(`de/jung/junghome/data/mesh/c.java:626,641,995`).

### 1.4 Element groups — which elements get one and what is wired to it

`CreateElementConnectionGroups` (`de/jung/junghome/domain/interactors/elementConnectionGroups/CreateElementConnectionGroups.java`,
body recovered in simple mode) runs during device set-up step `CreateElementGroups` (§3) and can be re-run from
the device configuration screen:

1. `models = e.b(node.elements, "suppportedServers")` — the DI list `"suppportedServers"` (`T7/h.java:1302,1322`) is
   `[0x1000 Generic OnOff Server, 0x1002 Generic Level Server, 0x1300 Light Lightness Server, 0x1306 Light CTL
   Temperature Server, 0x1303 Light CTL Server, 0x0527:1013 LBC User Property Server, 0x1100 Sensor Server,
   0x1203 Scene Server, 0x0527:1011 LBC Admin Property Server]`.
2. One element group is created (`r.n1(elementAddress)`, simple-mode `:209`) for **every element of the node that
   has at least one of those models** — including button/key elements (they carry 0x0527:1011/1013) and the
   gateway's primary element. This is why every element of every node has an `element group #…` in the export.
3. Wiring depends on the developer switch *"use_status_message_optimization"*
   (`K7/i.java:233-235`, `SharedPreferences`, default **false**; toggled only from the hidden mesh-network activity
   `useStatusMessageOptimizationSwitch`, `Q7/C0670i0.java:49-51`):
   * **Default (flag off)** → `CreateElementConnectionGroups$work$6`: for every element group, **all** supported
     server models of that element **except `0x1100 Sensor Server`** (`removeAll { it is SensorServer }`,
     comparator `de/jung/junghome/data/mesh/resolver/N1.java:240-241`) get
     `ConnectToAddress(PUBLISH_AND_SUBSCRIBE, elementGroup)` (simple-mode
     `CreateElementConnectionGroups$work$6.invokeSuspend:70,96-104,138-150`). This matches the iOS export
     (e.g. `0148`: `1000`, `1203`, `05271013` publish+subscribe `C061`). Sensor Server publications are configured
     separately towards the gateway (§6.1).
   * **Flag on** → `CreateElementConnectionGroups.g()`: a reduced set (simple-mode `:143-179`, lambda
     `de/jung/junghome/app/ui/configuration/deviceParameter/cells/C1836q.java` case 5): per device type the
     *primary* server of the element (`TunableWhite` → first `LightCtlServer`; `DimLamp` → `LightLightnessServer`;
     `Blind` → `GenericLevelServer`; `Rtr` → `GenericLevelServer` + `GenericOnOffServer`; everything else →
     `GenericOnOffServer`) plus every `LBCGenericUserPropertyServer` on elements whose location is one of
     `64..67` (`p065f8/c.java:12`, button elements A–D) plus every `SceneServer`, with `PUBLISH_AND_SUBSCRIBE`;
     then, for tunable-white lamps only, the second `GenericLevelServer` (colour-temperature element) gets
     `SUBSCRIBE_ONLY` (`de/jung/junghome/domain/interactors/elementConnectionGroups/a.java:39-66`).

A load's server models publish their status to the element's own group; anything that wants status (the app's
proxy — always in the black-list-filtered proxy —, connected buttons, the gateway) receives it there.

### 1.5 The generic pub/sub primitive: `ConnectToAddress`

`ConnectToAddress.Params(deviceId, models, address, type, flags)`
(`de/jung/junghome/domain/interactors/elementConnectionGroups/ConnectToAddress.java:152-166`):
`type ∈ {PUBLISH_AND_SUBSCRIBE, PUBLISH_ONLY, SUBSCRIBE_ONLY}` (`:58-81`), `publicationSteps` defaults to
`PUBLICATION_STEPS_DEFAULT = 30` and `publicationStepsResolution` to `1` (`:153-154`) — **but every caller passes
flags `112`, which zeroes both** (bits 32/64 set → steps 0, resolution 0), so no periodic publishing is ever
configured by the app.

For each `(elementAddress, modelId)` (`ConnectToAddress$work$2.java:92-107`):

| Step | Capability → message | Wire parameters |
|---|---|---|
| if type ≠ SUBSCRIBE_ONLY | `PublicationCapability` (`p096i8/o.java`) → `PublicationMessageBuilder.c()` (`p234v7/C2383s0.java:40-59`) → **`ConfigModelPublicationSet`** | `elementAddress`, `publishAddress`, `appKeyIndex = appKey.boundNetKeyIndex` (= 0 in practice), `credentialFlag = false`, `publishTtl = 0xFF` (use node default), `periodSteps = 0`, `resolution = 0`, `retransmitCount = 0`, `retransmitInterval = 0`, `modelId` (`no/nordicsemi/android/mesh/transport/ConfigModelPublicationSet.java:118-135`) |
| if type ≠ PUBLISH_ONLY | `SubscriptionCapability` (`p024b9/b.java`) → `AddSubscriptionMessageBuilder.c()` (`D7/a.java:34-48`) → **`ConfigModelSubscriptionAdd(elementAddress, address, modelId)`** | expected reply `CONFIG_MODEL_SUBSCRIPTION_STATUS` (`D7/a.java:24`) |

Both are sent through `CommunicateWithDevice(deviceId, capability, UPDATE, waitForStatus = true)` to the node
(device key, unicast of the primary element; the config-message path is `transport-provisioning.md` §3.5). Removal
uses `ConfigModelPublicationSet` with `publishAddress = 0x0000` (`p085h8/f.java:57-59`, `e0()` returns 0) and
`ConfigModelSubscriptionDelete` (`D7/b.java:34-48`) — never *Subscription Delete All*.

### 1.6 Scene numbers

Scenes are Nordic CDB scenes allocated with `MeshNetwork.createScene(provisioner, name)` = lowest free number in the
provisioner's scene range (`no/nordicsemi/android/mesh/MeshNetwork.java:836-847,560-590`), so user scenes are
`1, 2, 3, …` (`MeshSceneRepository.m0()`, `de/jung/junghome/data/repositories/MeshSceneRepository.java:837-852`).
Timers also allocate scenes, named `"TimerScene <index> <deviceId>"` (`M1()`, `:194-203`). Scene *storage* on
load elements uses the SIG Scene Setup Server (§4.3).

### 1.7 What is persisted locally (Room DB, `de/jung/junghome/data/persistence/**`)

| Entity (`L7/*`) | Fields | Meaning |
|---|---|---|
| `ElementConnectionGroupEntity` (`L7/d.java`) | `elementAddress`, `address` | element → element-group map (= iOS `element_connection_groups.json`) |
| `GroupEntity` (`L7/e.java`) | `name`, `address`, `isFavorite`, `icon` | rooms (= `groups.json`) |
| `SceneEntity` (`L7/h.java`) | `name`, `isFavorite`, `sceneNumber`, `icon` | scenes |
| `SceneInfoEntity` (`L7/i.java`) | `sceneNumber`, `elementAddress`, `deviceId`, `onOff`, 5 optional ints | cached per-element scene contents |
| `DeviceEntity` (`L7/b.java`) | incl. `keyModeGroupConfigs: List<KeyModeGroupConfig>` (`de/jung/junghome/domain/item/mesh/KeyModeGroupConfig.java:14-24`: `elementAddress`, `groupAddress`, `publishAddress`, `function`) | = iOS `cachedGroupConnectionMetadata` |
| `MeshPropertyEntity.kt` (`L7/f.java`, 7 subclasses) → tables `ActuatorEntity`, `ButtonLayoutEntity`, `KeyModeSceneConfigEntity`, `SoftwareVersionEntity`, `StmSoftwareVersionEntity`, `RegisterCapacityEntity`, `BlindOperationModeEntity` (`K7/c.java` = `AppDatabase_Impl.kt` CREATE TABLE list) | `address` (element) + one value | per-element cache of device-reported properties (§5.3) |
| `DeviceInfoEntity` (`L7/c.java`), `SchedulerMetaInfoEntity` (`L7/j.java`), `TimerBackwardsCompatibleEntity` (`L7/a.java`,`L7/k.java`), `ProjectEntity` (`L7/g.java`) | – | see §5 / §6 |

The device-type groups need no table (fixed addresses); their subscriptions are visible in the CDB.

---

## 2. "Connections" (button / rocker / sensor → target)

### 2.1 Vocabulary

* **Key mode** (`p056e8/i.java:14-63`, byte written to the button element as vendor Admin property
  **20483 KeyMode**, §2.2): `Light = 0`, `Move (blind) = 1`, `Scene = 2`, `Property = 3`, `RTR = 4`, `Switch = 5`,
  `Gateway = 6`, `Unset = -1`. Names from `MiniActuatorConfigurationFragment.java:348-366` (see
  `vendor-models.md` §6.1).
* **Client models per key mode** (published from the button element) and **server models per key mode**
  (subscribed on the target element) — `GetModelsForDeviceConnection.g()`
  (`de/jung/junghome/domain/interactors/connection/GetModelsForDeviceConnection.java:152-190`) and
  `p056e8/j.java:16-43`:

| KeyMode | CLIENTS on the button element | SERVERS on the target element |
|---|---|---|
| Light (0) | `0x1001` Generic OnOff Client, `0x1003` Generic Level Client, `0x0527:1015` | `0x1000`, `0x1002` |
| Move (1) | `0x1003`, `0x0527:1015` | `0x1002` |
| Scene (2) | `0x1205` Scene Client | `0x1203` Scene Server |
| Property (3) | `0x0527:1015` | `0x0527:1013` LBC User Property Server |
| RTR (4) | `0x1003` | `0x1002` |
| Switch (5) | `0x1001`, `0x0527:1015` | `0x1000` |
| Gateway (6) | `0x0527:1015` | `0x0527:1013` |
| Unset (-1) | none | none |

  Only models that exist on the given element are used (`GetModelsForDeviceConnection.java:183-189`).
* **Key mode from the target** (`SetDeviceConnection.h()` = `getKeyMode`, `SetDeviceConnection.java:293-396`):
  explicit key mode in `DeviceParams` wins; a `SocketDevice` target, or an `AddressParams` whose *source* is an
  `RtrDevice`, gives `Switch(5)`; otherwise by the target's `ActuatorFunctionId`: `Blind → Move(1)`,
  `TwDimming / TwoGangDimming / Dimming → Light(0)`, `TwoGangSwitch / Switch → Switch(5)`, `Rtr → RTR(4)`,
  `Unset / Extension / NotAvailable / Gateway / Unknown → Unset(-1)`.
* **Group function** (`GroupConnection.Function`, `de/jung/junghome/domain/item/GroupConnection.java:76-100`):
  `LIGHT=0, LIGHT_PROPERTY_MODE=1, BLIND=2, BLIND_PROPERTY_MODE=3, SWITCH=4, SWITCH_PROPERTY_MODE=5,
  LIGHT_AND_SWITCH=6, LIGHT_AND_SWITCH_PROPERTY_MODE=7, RTR_PROPERTY_MODE=8`; mapped to key modes by
  `de/jung/junghome/domain/item/a.java:10-29`: `{0,6} → Light`, `{1,3,5,7,8} → Property`, `2 → Move`, `4 → Switch`.
  This is the `groupActuatorFunction` in the iOS metadata.
* **Target element** (`DeviceConnection.Element`, `de/jung/junghome/domain/item/DeviceConnection.java:54-62` and
  `SetDeviceConnection.java:171-195`): `MAIN(0)` / `DIM(4)` → first element of the target device's range;
  `BLIND(1)` → element of the blind `GenericLevelServer`; `SLAT(2)` → `c2()` slat element;
  `LIGHT_TEMPERATURE(3)` → tunable-white range start `+ 1`.
* **Element ↔ key position** of a control switch: `AbstractC0914c.d2(key)` picks the rocker (`Z7/f`) by
  position (`Y7/AbstractC0914c.java:181-196`): `TOP_LEFT/LEFT/TOP_CENTER/CENTER → first`, `BOTTOM_LEFT → second`,
  `TOP_RIGHT → second-to-last`, `BOTTOM_RIGHT/RIGHT/BOTTOM_CENTER → last` (`ControlSwitchKeyPosition.java:42-58`).
  Rockers are the node's elements that carry client models (`b2()`, `:75-99`; `c2()`, `:100-178`).

### 2.2 Vendor properties written on the button

All are LBC **Admin Property Set** (`C3 27 05`, payload `propId u16 LE ‖ userAccess=3 ‖ value`,
`G7/b.java:28-44`, `vendor-models.md` §3.3) sent to the button node, reply `C5 27 05`:

| Property (id) | Value | Builder |
|---|---|---|
| **KeyMode** 20483 (0x5003) | 1 byte key mode | `p254x7/a.java:37-51` (`KeyModeMessageBuilder`), capability `p278z8/a.java` |
| **KeyModeSceneConfig** 20482 (0x5002) | 6 bytes LE: `sceneNumber u16` ‖ `transitionTime u32 (ms)` (`p254x7/b.java:47-57`, `W.a()` = LE buffer `p234v7/W.java:24-28`). The `publishAddress` field of the local `KeyModeSceneConfig` object (`p056e8/k.java:25-29`) is *not* transmitted, only cached. | `p254x7/b.java` |
| **KeySetPropertyMode** 20486 (0x5006) | `propertyId u16 LE` + `mode u8` (0 STATELESS / 1 STATEFUL) | `p234v7/E.java:50-56` |
| **KeySetPropertyValueUpOn** 20487 / **KeySetPropertyValueDownOff** 20488 | raw value bytes of the target property (`capability.Y().array()`) | `p234v7/G.java`, `p234v7/F.java:34-42` |

`ResetKeySetPropertyMode` writes `KeySetPropertyMode = (0, STATELESS)` (no wait), then
`KeySetPropertyValueUpOn = ∅`, `KeySetPropertyValueDownOff = ∅` for every rocker in the request
(simple-mode `ResetKeySetPropertyMode.g:128-165`); it precedes every new connection.

### 2.3 Button element → one device (`SetDeviceConnection`)

`de/jung/junghome/domain/interactors/connection/SetDeviceConnection.java` (body recovered in simple mode,
`SetDeviceConnection.i:435-700`). Params: `deviceId` (button), `publicationCapable` (the button element,
`O()` = its address), target device + `Element`, optional key mode, note position, rocker list.

1. `targetGroup = r.a(connectWithAddress)` — the **target element's element group** (must already exist).
2. `ResetKeySetPropertyMode(button, rockers)` (§2.2).
3. `keyMode = getKeyMode(...)` (§2.1).
4. `RemoveConnectionForAddress.AllConnections(button, buttonElement, keepExcluded = true)` — deletes the
   publication (`ConfigModelPublicationSet` to `0x0000`) and *all* subscriptions (`ConfigModelSubscriptionDelete` per
   address) of every model on the button element, except models `0x1100`, `0x0527:1013`, `0x0527:1011`
   (`RemoveConnectionForAddress.java:344-355` exclusion list; `$work$2.java:169-186`; `$deletePublication$2.java:103-141`;
   `$deleteSubscriptions$2.java`).
5. `clients = GetModelsForDeviceConnection(buttonElement, keyMode, CLIENTS)`.
6. **`ConnectToAddress(button, clients, targetGroup, PUBLISH_AND_SUBSCRIBE)`** — the button's client models
   publish to the load's element group *and* subscribe to it (so they receive the load's status for LED feedback).
7. If a note position was given: `UpdateDeviceInfo.ClearNote`.
8. If `keyMode ≠ Unset` and the element has a `KeyModeCapability`: write **KeyMode** (§2.2), `waitForStatus =
   true` for `DeviceParams`, `= waitForKeyMode` for `AddressParams`.
9. If `DeviceParams` with a rocker: `ConfigurePublicationsForPropertyUser([button, target], rocker)`
   (`de/jung/junghome/domain/interactors/elementConnectionGroups/ConfigurePublicationsForPropertyUser.java`,
   simple-mode `:136,171`): for each device in the list (filtered to sockets, `:267`) every
   `LBCGenericUserPropertyServer` on its elements gets `PUBLISH_ONLY` to that element's own group, and each client
   model of the rocker gets `SUBSCRIBE_ONLY` to the same group.

Nothing is written to the **target** here: its server models already publish/subscribe to their own element
group from step `CreateElementGroups` (§1.4). The iOS observation "button client models publish straight to a
load's element group" is exactly this path.

UI entry points (`de/jung/junghome/app/ui/devices/connection/devices/ConnectionDeviceSelectionActivity.java:255-330`):
a control-switch key (`ConnectionSource.ControlSwitch`, rocker `d2(position)`) and a detector
(`ConnectionSource.Detector`, publication `e()`) both call `SetDeviceConnection.DeviceParams(..., keyMode = null)`
(key mode derived from the target); a mini-actuator binary input calls `ConfigureMiniActuatorConnection.DeviceParams`
with the key mode from the chosen category: `Switching → Switch(5)`, `Light → Light(0)`, `Hangings → Move(1)`,
`Temperature → RTR(4)` (`:316-330`); a mini actuator + RTR target uses `SetRtrPropertyConnection.DeviceParams` (`:264`).

Gateway link: the "connect key to gateway" screen calls `SetDeviceConnection.DeviceParams(button, key, gateway,
Element.MAIN, KeyMode Gateway(6), position, [rocker])`
(`de/jung/junghome/app/ui/devices/connection/gateway/ConnectionGatewayActivity.java:461-466`), i.e. the key's
`0x0527:1015` client publishes to the gateway's primary-element group (`0xC005` in the iOS export) and is set to key
mode 6. Mini actuators use `ConfigureMiniActuatorConnection.DeviceParams(...)` with the same key mode (`:470-476`).

### 2.4 Button element → room group (`SetGroupConnection` + `SetGroupFunction`)

`SetGroupConnection$work$2.invokeSuspend` (`de/jung/junghome/domain/interactors/connection/SetGroupConnection$work$2.java:130-262`);
params: button, button element, group `e`, `GroupConnection.Function`, rockers (`KeyModeParams` → key mode from the
function; `PropertyModeParams` → `Property(3)`, `SetGroupConnection.java:102-105,253-256`).

1. `ownGroup = r.a(buttonElement)` — the **button element's own element group** (`:147`).
2. `ResetKeySetPropertyMode` (`:153-159`).
3. `RemoveConnectionForAddress.GroupConnection(button, ownGroup, buttonElement, null)` then
   `AllConnections(button, buttonElement, true)` (`:167-183`).
4. `clients = GetModels(buttonElement, keyMode, CLIENTS)` (`:187-193`).
5. **`SetGroupFunction(button, buttonElement, room.address, publishAddress = ownGroup, function, keyMode)`**
   (`:202-209`; `de/jung/junghome/domain/interactors/group/SetGroupFunction.java`, simple-mode `SetGroupFunction.i:181-383`):
   * loads the room's devices (`T.Q0(groupAddress)`, `T.B1(group)`, simple-mode `:358,371`);
   * keeps only devices matching the function (`:297-329`): `LIGHT*` → `LampDevice`; `BLIND*` → `BlindDevice`;
     `SWITCH*` → `SocketDevice`; `LIGHT_AND_SWITCH*` → lamp or socket; `RTR_PROPERTY_MODE` → `RtrDevice`;
   * of those, the ones whose server models (for the key mode, on their first element) do not yet subscribe to
     `publishAddress` (`SetGroupFunction.h()`, `SetGroupFunction.java:107-178`);
   * for each such device and each server model: **`ConfigModelSubscriptionAdd(loadElement, ownGroup, model)`**
     via `SubscriptionCapability` (simple-mode `:253`);
   * stores `KeyModeGroupConfig(elementAddress = buttonElement, groupAddress = room, publishAddress = ownGroup,
     function)` on the button's `DeviceEntity` (`UpdateDeviceData.Params.UpdateGroupConfig`, simple-mode `:240`).
6. **`ConnectToAddress(button, clients, ownGroup, PUBLISH_AND_SUBSCRIBE)`** (`:217-224`) — the button publishes to
   its own element group; all room loads of the right type subscribe to it.
7. Write **KeyMode** on the rocker (`:256-260`).

Adding a device to a room later (`AddDeviceToGroups` → `AddGroupToDevices`,
`de/jung/junghome/domain/interactors/devices/AddGroupToDevices.java`):

* `addDevicesToGroup` (simple-mode `AddGroupToDevices.h`): for each device not yet in the group, models =
  `[0x1000, 0x1002]` on the device's first element (or, if the group already has a `KeyModeGroupConfig`, the server
  models for that function's key mode), filtered to those not yet subscribed →
  **`ConnectToAddress(device, models, room.address, SUBSCRIBE_ONLY)`** — room membership = OnOff/Level servers of the
  first element subscribe to the room address (matches `C00F WC` on the `0148` OnOff server in the export).
* `reconnectSwitchesWithGroup` (`AddGroupToDevices.java:140-190`, `$work$2.java:160-215`): for every
  `KeyModeGroupConfig` of every button connected to this room, `SetGroupFunction` is re-run so the new load also
  subscribes to that button's element group.
* Removing (`DeleteGroupFromDevices$work$2.java:322,440,…`): `RemoveConnectionForAddress.GroupConnection(device,
  room.address, firstElement, …)` = subscription deletes of the room address (and the linked element groups) on the
  models that carry them (`RemoveConnectionForAddress$work$2.java:187-196`); `deleteGroupConnection` additionally
  unsubscribes the room's loads from the button's element group and drops the `KeyModeGroupConfig`
  (simple-mode `RemoveConnectionForAddress$deleteGroupConnection$2.invokeSuspend:140,229,273-309,461-491`).

### 2.5 Button element → scene (`SetSceneConnection`)

`de/jung/junghome/domain/interactors/connection/SetSceneConnection.java:190-303` (+ `$work$2.java:76-84`),
params: button, button element, `Scene w`, `connectToAddress` = the button device's own **load** element for
that key (`ConnectionSceneSelectionActivity.java:248-268` picks it from `e2()`, the map of the node's
OnOff/Level/Lightness/Scene/CTL servers by element, `Y7/AbstractC0914c.java:201-262`).

1. `ownLoadGroup = r.a(connectToAddress)`; key mode = `Scene(2)`.
2. `RemoveConnectionForAddress.AllConnections(button, buttonElement, true)`.
3. clients = `[0x1205 Scene Client]` → **`ConnectToAddress(button, [SceneClient], 0xFFFF, PUBLISH_ONLY)`** — scene
   keys publish *Scene Recall* to **all nodes** (`:249`, confirmed by the iOS export: every Scene Client publishes
   to `FFFF`).
4. Write **KeyModeSceneConfig** = `(sceneNumber, transition 0 ms)` (`:281`, wait for status; the local copy also stores `ownLoadGroup`) and
   **KeyMode = 2** (no wait) (`$work$2.java:76-84`); cache the config in the `MeshPropertyRepository` (`A.H(elementAddress, config)`, `:302`, `R7/A.java` = `MeshPropertyRepository.kt`).

Scenes are recalled by the device firmware via the SIG Scene Client; nothing is configured on the loads (they
already store the scene, §4.3).

### 2.6 Locking function, RTR property connections, multi-connection

* **Locking function** (`SetLockingFunctionConnection`, simple-mode `SetLockingFunctionConnection.i:320-600`):
  for each rocker of the source button: `KeySetPropertyMode = (9 EnforceOutputProperty, STATEFUL)` (`:336`),
  `KeySetPropertyValueUpOn = LockFunction(command, priority, duration…)` derived from the chosen `LockingFunction`
  (`:361-425`, `C8/*` = `LockFunction*.kt`), `KeySetPropertyValueDownOff = LockFunction(cmd C8.c.b)` (`:387`);
  then either `SetDeviceConnection.DeviceParams(..., Element.MAIN, KeyMode Property(3), …)` (`:509`) or
  `SetGroupConnection.PropertyModeParams(...)` (`:521`), followed by a `REQUEST` of the lock-function state
  from the target (`:600`). Net effect on the mesh: the key's `0x0527:1015` publishes to the target's element
  group, whose `0x0527:1013` server is subscribed there (§1.4).
* **RTR "property" connections** (`SetRtrPropertyConnection`, `ConnectionType AUTO_MANU(0) / ECO_COMFORT(1)`,
  `SetRtrPropertyConnection.java:48-52`; simple-mode `:345-547`): binary inputs of a mini actuator / rocker get
  `KeySetPropertyMode = (rtrProperty, STATEFUL)`, up/down values (`HvacMode` or `RtrMode`), an
  `InputEdgeDetection` write (`:411`), then `SetGroupConnection.PropertyModeParams(…, RTR_PROPERTY_MODE, …)`
  (`:511`) or `SetDeviceConnection.DeviceParams(…, Element.MAIN, Property(3), …)` (`:547`).
* **Multi-connection** (`SetMultiConnection`, RTR → several loads; simple-mode `SetMultiConnection$work$1:252-638`):
  for every selected load `GetModels(firstElement, Switch(5), SERVER)` → **`ConnectToAddress(load, [OnOff server],
  rtrElementGroup, SUBSCRIBE_ONLY)`** (`:309`), `RtrOperationMode = TRUE` on the RTR (`:394`), and finally
  `SetDeviceConnection.AddressParams(rtr, rtr.publication, rtrElement, waitForKeyMode = false)` (`:638`) so the
  RTR's OnOff client publishes to **its own element group**.

### 2.7 How the app reads an existing connection back (`GetConnection`)

`de/jung/junghome/domain/interactors/connection/GetConnection.java:255-380` classifies a button element from
its CDB publication address `P`:

1. cached `KeyModeSceneConfig.sceneId ≠ 0` → **SceneConnection** (`:270-287`);
2. `P` is an element group (`r.o0(P)` → its element address, `:296-320`) → load the device owning that element.
   For a room link this is the **button itself** (it publishes to its own element group) and its
   `KeyModeGroupConfig` with `publishAddress == P` yields **GroupConnection(room, function)** (`:338,386-428`);
3. otherwise the owning device is the target: a load (lamp / blind / socket / RTR) or the gateway →
   **DeviceConnection(target, P, targetElement, buttonElement)** (`:340-374`; a physical attachment such as
   another push-button unit is rejected), or a **lock-function connection** when the key carries a
   `KeySetPropertyMode` (`:352-365`, `h()` `:97-125`).

A third-party controller that wants the app to display its wiring must therefore also (a) create element groups
for the elements it wires, (b) write the `KeyMode` property and (c) — for room links — the
`KeyModeGroupConfig` metadata in the shared project file (§6).

---

## 3. Adding a device (provisioning + configuration)

BLE-level details (scanning, JUNG advertising data, No-OOB provisioning, proxy filter dance, message queue) are in
`transport-provisioning.md` §2–3. This section gives the complete ordered sequence and the values that end up in
the network.

### 3.1 Provisioning

* Discovery: unprovisioned devices are keyed by MAC; the device UUID is the 16-byte 0x1827 service data
  (`de/jung/junghome/data/mesh/DeviceDiscoveringImpl$startDiscovering$2.java:72-113`); the JUNG manufacturer AD
  (`vendorId 0x0527`, `advType`, `productId`, `actuatorFunctionId`, `buttonLayout`, MAC) is parsed by
  `domain/item/bluetooth/LBCAdvertisementData.java:76-124`. The UUID embeds the MAC EUI-64 style (`ff-fe`,
  `de/jung/common/UtilsKt.java:100-114`), e.g. `30FB10FF-FE30-E756-…` = `30:FB:10:30:E7:56` in the export.
* `IdentifyDevice` (5 s timeout, 3 retries; `IdentifyDevice.java:70-85`) → `MeshManagerApi.identifyNode(uuid)`
  (attention timer 5 s) → `DeviceProvisioning` → `NodeProvisioningServiceImpl.Y0()`
  (`NodeProvisioningServiceImpl$startProvisioning$1.java:65-88`): unicast =
  `nextAvailableUnicastAddress(nodes.size(), provisioner)` (+1 on collision), `startProvisioningNoOOB` (FIPS P-256,
  auth method 0). On `PROVISIONING_COMPLETE` the node is **named after the advertised BLE name** (fallback
  localized default) (`NodeProvisioningServiceImpl.java:175-199`). Whole provisioning is wrapped in a 30 s timeout
  (`DeviceProvisioning$work$1.java:360`); on failure the app disconnects and runs `ResetDevice(id, false)` (`:225-245`).
* After `ProvisioningFinished` the app reconnects **to the new node itself as proxy** (scan 15 s for its MAC with
  0x1828 service data, `ConnectToDevice.Params.Specific`) and starts `ConfigureDevice(uuid, mac,
  requestButtonLayout = false)` (`app/ui/addNewDevice/AddNewDeviceViewModel$startProvisioning$1.java:103`).

### 3.2 `ConfigureDevice` step machine

`de/jung/junghome/domain/interactors/configuration/ConfigureDevice.java` + `ConfigureDevice$work$1.java`
(`:1290-1327`): a `StateFlow<DeviceSetupProgress>` drives one step per state (`ConfigureDevice.g()` `:421-426`
advances), the chain is retried twice (`RETRY_COUNT`, `:1321`) and on final failure sets `ProvisioningNotStarted`,
runs `ResetDevice(id, false)` (Config Node Reset + local delete) and rethrows (`:1212-1228`). Before the first step
`SetProxySettings(mac)` sends `ProxyConfigSetFilterType(BLACK_LIST)` to `0x0000` (`JungMeshApiImpl.java:332-361`).

| # | State | Messages (all to the node's primary unicast with the device key unless noted) | Source |
|---|---|---|---|
| 1 | `SetWhitelistFilter` | **Config AppKey Add** (netKey 0, appKey 0, `p234v7/C2353d.java:38`); **Proxy Set Filter Type = white list**; **Proxy Add Addresses** {node unicast, `0xFFFF`, provisioner unicast} (`ConfigureDevice.java:345-419`). Quirk: `AddProxyFilterAddressesMessageBuilder` casts each address to a *byte* and sends it twice (`p234v7/C2355e.java:46-51`), so only `0xFFFF` survives intact. | `$work$1.java:385-394` |
| 2 | `RequestCompositionData` | **Config Composition Data Get** page 0 (`p234v7/C2373n.java:29`); then `bindMeshModels`: **Config Model App Bind** (appKey 0) for every model of every element whose ID is in the bind list `P7/a.java:27` (§3.3) and is not yet bound (`JungMeshApiImpl.c1`, `data/mesh/c.java:186-197`). | `:405-413` |
| 3 | `SetConfiguration` | **Config GATT Proxy Set = 1** (`p234v7/C2381r0.java:41`); **Config AppKey Add** again + **Config Default TTL Set = 5** (`p127l8/c.java`, `K0.java:37`); **Config Relay Set** relay 1, count 3, interval 90 ms → steps 9 (`C2387u0.java:55`); **Config Network Transmit Set** count 3, interval 100 ms → steps 10 (`Q0.java:44`). | `:423-449` |
| 4 | `SetBlacklistFilter` | **Proxy Set Filter Type = black list** + **Proxy Add Addresses** {node unicast, `0xFFFF`} minus the provisioner (`ConfigureDevice.java:193-291`, default list `Y7/C0892b.java:2687-2695`); **Config Beacon Set = !isLowPower** (unacknowledged, `p234v7/C2375o.java:41`). | `:461` |
| 5 | `RequestRequiredData` | fails with `NodeNotConfigured` if composition data is missing; `t1()` asserts models 0x1012 and 0x0527:1012 exist (`Y7/AbstractC0916e.java:438-471`); **LBC User Property Get 0x0002 InsertId** (`CE 27 05`, `p234v7/C2351c.java:30`) → `ActuatorFunctionId`/`InsertType` (§5.5), `ActuatorFunctionNotSet` if unknown; with `requestButtonLayout` also **Admin Get 0x5001 ButtonLayout** (`C2371m.java:36`). | `:470-486`, simple-mode `ConfigureDevice.requestRequiredData:81-160` |
| 6 | `SetTime` | **Time Set (0x5C)** to the first Time-compatible device's Time Server element with the phone's TAI time (`ConfigureDevice.java:295-341`, `p234v7/M0.java:41`, `W7/c.java:32-57`); errors ignored. | `:501` |
| 7 | `CreateElementGroups` | `CreateElementConnectionGroups` (30 s budget, `$work$1.java:282-300`): element groups + publication/subscription as in §1.4. | `:512` |
| 8 | `FinishConfiguration` | §3.4 | `:517` |
| 9 | `DisableProxy` | low-power devices (`Q1()`: wall transmitters, battery mini actuator): **Config GATT Proxy Set = 0** (`ConfigureDevice$disableProxyIfRequired$2.java:55-84`). | `:529` |
| 10 | `ReconnectToDevice` → `Finished` | low-power node: connect to any *other* proxy; else BLE reset + reconnect to the node and `SetProxySettings` (black list, empty) (`update/ConnectToDevice$work$2.java:216-248`). | `:540-580` |

`isConfigured` (`configComplete` in the CDB) is Nordic's flag set when composition data has been stored
(`p065f8/f.java:456-458`: productId, companyId and elements present); the app's own notion of "configured" is the
`DeviceSetupProgress.Finished` state plus a stored MAC.

### 3.3 App-key bind list (`P7/a.java:27`, `MeshUtils.kt`)

`0x1011, 0x1004, 0x1002, 0x1003, 0x1012, 0x1000, 0x1001, 0x1006, 0x1007, 0x100E, 0x100F, 0x100C, 0x1303, 0x1305,
0x1304, 0x1306, 0x1300, 0x1302, 0x1301, 0x1203, 0x1205, 0x1204, 0x1206, 0x1207, 0x1100, 0x1101, 0x1200, 0x1201,
0x0527:1013, 0x0527:1011, 0x0527:1015, 0x0527:1016, 0x0527:1017` (SIG IDs decoded from the decimal constants; the
Configuration Server is skipped). Not in the list: 0x0002 Health, 0x1013 SIG User Property, 0x0527:1012, 0x1102
Sensor Client. However, `MeshMessengerImpl.processRequest` **auto-binds every model of the destination element**
(except the Config Server, model 0x0001 and unknown models) before sending any message to it
(`data/mesh/c.java:166-214`, 3 s wait each), which is why the export shows Health/0x0527:1012 bound as well. Only
AppKey index 0 is ever used (`P7/a.java:29-33`).

### 3.4 `FinishConfiguration` (simple-mode `ConfigureDevice.c:150-410`)

1. For **each** logical device of the node: `UpdateDeviceData.UpdateDeviceAddress(deviceId, mac)` and
   `ConnectToDeviceTypeGroup(deviceId)` → **Config Model Subscription Add** of the supported servers to the
   device-type group(s) (§1.3) (simple-mode `:343-344`).
2. `ConfigureDetector(first)` — motion/presence detectors (`ConfigureDetector.java:90-300`): for the detector's
   factory publication (its last-element `0x1001 OnOff Client` → element of the node's first OnOff server,
   `Y7/AbstractC0915d.java:109-150`) run `SetDeviceConnection.AddressParams` (§2.3) so the client publishes to the
   load element's group and **KeyMode** is written; then **Admin Set 0x6021 DetectorRepetitionTime = 50**
   (`:167`), **0x100B ManualOffEnable = true** for every ManualOffEnable-capable device (`:228`) and **0x1007
   TimedOnDuration = 120 000 ms** (capped 14 400 000) for every lamp device of the node (`:264-265`).
3. `ConfigureGateway(first)` — gateway only (§6.1).
4. `ConfigureSocket(first)` — measuring socket 0x0003 only, unacknowledged reads: SIG Admin Property Get (0x822D)
   of 0x006D TotalDevicePowerOnTime and 0x006A TotalDeviceEnergyUse, `SensorGet` 0x0081 Active Power, LBC User
   Property Get 0x5010/0x5011 energy charts (`ConfigureSocket$work$2.java:60-262`).
5. RTR present: **LBC Manufacturer Get 0x0005** STM32 version (`C7/b.java:28`).
6. `RequestManufacturerInfos` — SIG **Generic Manufacturer Property Get (0x822B)** ×3 for 0x0011 name, 0x001A
   software version, 0x0010 hardware revision, to the element with the SIG 0x1012 server
   (`manufacturer/RequestManufacturerInfos.java:37-60`, `p234v7/S.java:29`, `T.java:29`, `Q.java:29`).
7. Battery devices: **Generic Battery Get** (0x8223) (`p234v7/C2359g.java:23-30`).
8. `CheckForMissingDevices` — expects 2 logical devices for Blind/Dimming/Switch/TwDimming and 3 for TwoGang*,
   single-device products `{0x000A, 0x0003, 0x000C, 0x000B, 0x0015, 0x0016, 0x0006, 0x0005}` or function
   `Extension` (`configuration/CheckForMissingDevices.java:211`).
9. If the node is a legacy "PP2" puck (`K1()` = company 0x0527, PID 0x0010..0x0014, `Y7/AbstractC0916e.java:289-297,340-358`):
   create the local group `0xFEFF "#time_keeper_group#"` and **Config Model Subscription Add (Time Server 0x1200 →
   0xFEFF)** (simple-mode `ConfigureDevice.c:202-210`, `TimeKeeperConfiguration.c()`).

No user-visible defaults other than the ones listed are written; all other parameters (delays, dim mode, LED modes,
lock, thresholds …) are only written when the user changes them in the parameter screens (LBC Admin Property Set,
`vendor-models.md` §3).

### 3.5 Related flows

* **Control switch layout change** (`ConfigureControlSwitchLayout.java:118-135`): **Admin Set 0x5001
  ButtonLayout** (u16 LE `LayoutMode`, unacknowledged) followed by a full **reprovisioning**
  (`ReprovisioningDevice`: scan 60 s → `RemoveDevice` → identify → provision → `ConfigureDevice(requestButtonLayout
  = true)` → restore names), because the element layout of the node changes.
* **ResetDevice(id, isForceReset)** (simple-mode `ResetDevice.g:172-860`): RTR operation-mode reset → delete
  thresholds → (if not forced) remove every publication/subscription of *other* devices that points at this node's
  element groups (`RemoveConnectionForAddress`) → **Config Node Reset (0x8049)** awaiting `0x804A`
  (`data/mesh/api/a.java:19-52`) → `RemoveDevice` (local: device info, scheduler meta, element groups, room
  membership, scenes, properties, timers, then `meshNetwork.deleteNode`) → cloud export.
* **Firmware-update pre-config** `UpdateDeviceSettingsForForcedUpdate`: black-list filter, Composition Data Get,
  AppKey Add, Default TTL 5, GATT Proxy Set 1 (`configuration/UpdateDeviceSettingsForForcedUpdate.java:95-140`).

---

## 4. Control paths (what the app sends to drive devices)

### 4.1 Common rules

* Every control goes through `CommunicateWithDevice(deviceId, capability, REQUEST|UPDATE, waitForStatus)`
  (`de/jung/junghome/domain/interactors/manufacturer/CommunicateWithDevice.java:45-47,167,181`) →
  `JungMeshApiImpl.U1` (GET, builder `b()`) / `E1` (SET, builder `c()`) → `MeshRequest` → `MeshMessengerImpl`.
* **Destination** (`JungMeshApiImpl.g3()`, `de/jung/junghome/data/mesh/api/JungMeshApiImpl.java:74-86`): config
  messages → the node's primary unicast; proxy config → `0x0000`; every application message → **`capability.O()`**,
  the address baked into the capability object. Device capabilities are built in the device constructors with the
  element address of the hosting model (e.g. `new p096i8.g(genericOnOffServer.getElementAddress(), …)`,
  `Y7/q0.java:1306`; blinds `Y7/C0892b.java:2821,2829,2894`; RTR `RtrDevice.java:3251,3259`). Group commands
  construct fresh capabilities with a **group address** (§4.4) and scene recall uses **`0xFFFF`** (§4.3).
* **Ack**: per-device control is acknowledged (waits for the status from the same element, 3 × 3 s) whenever the
  caller passes `waitForStatus = true`; all group / broadcast sends use `c.a.b(api, cap, null, …, 6)` = uuid null =
  **unacknowledged** (`p128l9/c.java:17-29`). `GenericDeltaSet` (`p234v7/H.java:38`) and `LightCtlSet`
  (`p234v7/T0.java:39`) ignore the flag and always use the acknowledged opcode.
* **TID**: one global counter, incremented once per `E1()` call, byte-truncated (`JungMeshApiImpl.java:69`, simple-mode
  `JungMeshApiImpl.E1:160-165`). **Transition time / delay: never sent** (3-arg Nordic constructors,
  `GenericOnOffSet.java:23,33-35`). **TTL**: not set per message → provisioner default 5. **AppKey**: index 0.
* If the proxy is not connected and the target is not a battery transmitter, the request fails immediately with
  `DeviceNotReachable` (`data/mesh/c.java:503`, simple-mode `MeshMessengerImpl.processRequest:359-364`).
* Level ↔ percent: `level = round(−32768 + pct/100 · 65535)` and back (`domain/item/mesh/nodes/models/a.java:22,37`,
  `io/grpc/u.java:37-45,361-366`); lightness = `round(pct/100 · 65535)` (`p096i8/j.java:71-78`); colour temperature
  `K = rint((2000 + 8000·pct/100)/100)·100` (`p097i9/d.java:15`), range 2000..10000 K.

### 4.2 Per device kind

| Device | Action | Message → destination | Params / notes | Source |
|---|---|---|---|---|
| Lamp, switch lamp, socket, mini actuator output | tap on/off (`ToggleDevice`) | **`GenericOnOffSet` (0x8202)** → OnOff server element unicast, acknowledged | new state = OFF if current is ON *or unknown*, else ON; afterwards `Admin Get 0x0009` lock state if unknown | `domain/interactors/devices/ToggleDevice.java:111-139,199`; `p234v7/C2395z.java:42` |
| Dim lamp | slider (sampled every 500 ms) / ± | **`LightLightnessSet` (0x824C)** → Lightness server element, acked | `[lightness u16][tid]`; page open sends `LightLightnessRangeGet` + `LightLightnessGet` | `app/ui/devices/lamp/elements/dim/DimViewModel.java:85,113`, `dim/e.java:45-68`; `p234v7/J.java:45` |
| Tunable-white lamp | colour temperature (debounced 500 ms) | **`LightCtlSet` (0x825E)** → CTL server element, acked | `[lightness u16][temperature K u16][deltaUV 0][tid]`; a `null` lightness is sent as 0 (code quirk) | `tunableWhite/TunableWhiteViewModel.java:121`, `p234v7/T0.java:39` |
| Blinds | up / down / stop (one message per tap) | **`GenericDeltaSet` (0x8209)** delta `−1` / `+1` / `0` → position (first `GenericLevelServer`) element | `p085h8/j.java`: Decrease −1, Increase +1, Stop 0 | `app/ui/devices/blinds/elements/BlindsViewModel$blindsOpening$1.java:75`, `$blindsClosing$1.java:75`, `$stopBlinds$1.java:71`; `p234v7/H.java:38` |
| Blinds | position slider (on release) | **`GenericLevelSet` (0x8206)** → position element; 100 % = closed | | `$updateBlindLevel$1.java`, `slider/Blinds.java:307-316` |
| Blinds | slat slider | **`GenericLevelSet`** → slat element (second `GenericLevelServer`, last element of the device range) | | `$updateSlatLevel$1.java` |
| Blinds | reference run / move times / operation mode | LBC Admin Set 0x110D (`[01]`), 0x1102, 0x1103, 0x1104 | | `p234v7/C2385t0.java`, `C2363i.java`, `C2354d0.java`, `C2365j.java` |
| RTR thermostat | target temperature (slider, sampled 500 ms; ±0.5 °C) | **`GenericLevelSet`** → target-temperature `GenericLevelServer` element | `pct = round((t − 5) / 25 · 100)` (5..30 °C) → level | `p256x9/c.java:584-587`; `RoomTemperatureViewModel$updateTargetTemperature$1$1.java`; `p234v7/C2394y.java:46` |
| RTR | mode Comfort / Eco / Frost | LBC Admin Set **0x120B RtrHvacMode** `u8` 1/2/3 (or, if unsupported, the preset temperature as target) | | `p234v7/C2391w0.java:39`; `RoomTemperatureViewModel.java:234-270` |
| RTR | boost / scheduler on / valve type / set-points | Admin Set 0x120D (`u8`), 0x1246 (`u8`), 0x120A (`u8` 0 NC / 1 NO), 0x1203/0x1204/0x1205 (`i16` °C×100) | | `p234v7/C2369l.java:43`, `C0.java`, `X0.java:38`, `y0.java:68-82` |
| RTR | page open | `SensorGet` 0x004F (actual temperature), `GenericLevelGet`, Admin Get 0x1203/04/05, 0x120D, 0x1246 | | `RoomTemperatureViewModel$request*$1.java` |
| Measuring socket | consumption page (every 5 s) | SIG `GenericAdminPropertyGet` (0x822D) 0x006D / 0x006A; `SensorGet` 0x0081 | | `socket/consumption/PowerConsumptionViewModel$requestData$1.java:122-191` |
| Measuring socket | energy charts | LBC **User** Property Get `CE 27 05` 0x5010 / 0x5011 | values big-endian ×0.1 | `p234v7/U.java:39-46`, `J7/b.java:61-115` |
| Measuring socket | reset counters | SIG `GenericAdminPropertySet` (0x48) 0x006D / 0x006A = 0 | | `p234v7/P0.java:46-49` |
| Detector | presence test on/off | Admin Set **0x6001 PresenceTest** + **0x6003 PresenceControl** (`u8`), auto-off after 300 s; while active poll Manufacturer Get 0x6005 every 1 s | | `domain/interactors/detector/EnablePresenceTest.java:135-170`, `DisablePresenceTest.java:87-131` |
| Detector | PIR sensitivity / lux threshold / operation mode / forced off / site / day mode | Admin Set 0x6008-0x600A (`u8`), 0x600F (`u16` lux), 0x6006 (0 GUARD, 1 PRESENCE), 0x6016 (0 INACTIVE, 2 OFF, 3 ON), 0x6017 (7 INDOOR, 112 OUTDOOR), 0x6015 | | `p234v7/C2362h0.java`, `C2374n0.java`, `C2382s.java`, `r.java`, `C2372m0.java`, `C2379q.java` |
| Control switch / wall transmitter / mini-actuator input | – | **no runtime control message**; keys are configured by the connection flows of §2 and by Admin Set 0x5001 layout, 0x5009 input edge detection, 0xA0xx LED modes | | §2, `p234v7/C.java`, `p266y7/*` |
| Any lockable device | lock / unlock / lock-out / wind alarm | Admin Set **0x0009 EnforceOutput** `[cmd u8][priority u8][time u16 LE s][extra…]`: lock = `02 01 <s>`, unlock = `00 …`, lock-out protection = `02 FE <s>`, wind alarm = `01 FF 00 00 00 00`; not accepted as ack by the generic `D1 27 05` fallback | | `p234v7/O.java:41`; `C8/a.java:27-125`; `C8/c.java:14-43`; `app/ui/devices/LockFunctionViewModelDelegate$*` |
| Gateway | – | only reads of 0xC001..0xC003 (§6.1); everything else is HTTP | | |

### 4.3 Scenes

| Action | Message → destination | Source |
|---|---|---|
| **Recall** (scene tile / favourite / key) | **`SceneRecallUnacknowledged` (0x8243) → `0xFFFF`**, one broadcast, `[scene u16][tid]`, no transition | `domain/interactors/scene/ActivateScene.java:75-77`; `A7/d.java:48` |
| Store current state of a device into a scene | **`SceneStore` (0x8246)** → the device's Scene Setup Server element, acked; Nordic records the element in `Scene.addresses` (this is the "stored on elements" list in the export); then, if the node has `0x0527:1017`, vendor **`SceneActionSetupSet` (`D8 27 05`)** `[scene u16][actionType u8][5 bytes]` (1 Switching `[onoff]`, 2 Lightness `[u16]`, 3 Lightness+CTL `[u16][u16 K]`, 4 Blinds+Slats `[s16][s16]`, 5 TargetTemperature `[u16 ×100]`) | `StoreDeviceConfigurationInScene$work$2$1.java:104,126`; `A7/f.java:43`; `G7/h.java:19`; `S8/a.java:89-155`; `vendor-models.md` §4.2 |
| Remove device from scene / delete scene | **`SceneDelete` (0x829E)** → Scene Setup Server element (or `SceneActionSetupSet(NoAction)` when the sibling output of a 2-gang node still uses the scene) | `A7/c.java:43`; simple-mode `RemoveDeviceConfigurationFromScene:197-240` |
| Read | `SceneRegisterGet` (0x8244) → Scene Server; `SceneActionSetupGet` (`D6 27 05`, scene 0 = list) | `A7/e.java`, `A7/g.java:27`, `A7/b.java:30` |

Scene numbers: §1.6. The UI puts the device into the wanted state *before* sending `SceneStore`
(`app/ui/scene/create/configure/u.java:111`).

### 4.4 Central functions ("all lamps / all sockets / all blinds / all RTRs") and room pages

`UpdateDeviceTypeGroup` (`de/jung/junghome/domain/interactors/manufacturer/UpdateDeviceTypeGroup.java`) builds
capabilities with the device-type group as address and sends **one unacknowledged message**:

| Action | Message → address | Source |
|---|---|---|
| all lamps on/off | `GenericOnOffSetUnacknowledged` → **`0xFEF5`** | `UpdateDeviceTypeGroup.java:87,301,350` |
| dim all lamps | `LightLightnessSetUnacknowledged` → `0xFEF5` | `:108` |
| all sockets on/off | `GenericOnOffSetUnacknowledged` → **`0xFEF8`** | `:329` |
| all blinds up (0 %) / down (100 %) / position | `GenericLevelSetUnacknowledged` → **`0xFEF6`** | `:124,173` |
| all blinds stop | `GenericDeltaSet(0)` → `0xFEF6` (unacknowledged request) | `:190,252` |
| all slats | `GenericLevelSetUnacknowledged` → **`0xFEF7`** | `:152` |
| all RTRs target temperature | `GenericLevelSetUnacknowledged` → **`0xFEF9`**, pct = `(t−5)/25·100` | `:268` |

Inside a **room**, on/off, blind position, slats and RTR temperature are fanned out as **per-device unicast
unacknowledged** messages (`app/ui/devices/lamp/centralFunction/LampCentralFunctionsViewModel.java:42,59`,
`blinds/centralFunction/BlindCentralFunctionsViewModel.java:56-73`, `socket/…`), while *dim* and *blind stop* go to the
**room group address** (`Dim.Group` / `OpenClose.Group`, `p223u6/b.java:96`, `p125l6/c.java:185-187`) — which works
because room membership subscribes the first-element OnOff/Level servers to the room address (§2.4).

### 4.5 Timers, thresholds, keep-alive

See §6.3 for SIG Scheduler / JH Scheduler / astro / thresholds and §5.4 for the keep-alive (`Admin Get 0x5001` every
6 s to battery transmitters) and the app-start `TimeSet` broadcast.

---

## 5. Status / state: how the app learns device state

### 5.1 Receive path

* Every decrypted access message reaches `MeshNotificationChannel.onMeshMessageReceived(src, msg)`
  (`de/jung/junghome/data/mesh/MeshNotificationChannel.java:1469-1476`, the class is registered as Nordic
  `MeshStatusCallbacks` in its constructor `:159-166`). There is **no distinction between a solicited reply and an
  unsolicited publication** — both take the same path; pending requests just filter the same flow.
* `onMeshNotificationReceived` (`:200-445`): node = `meshNetwork.getNode(src)`; device = first device whose address
  range contains `src` (`DeviceRepositoryImpl.java:486-506`), fallback by node UUID (`:277-286`); then **all**
  `StatusMessageResolver`s (Koin `getAll`, `W5/k.java:206`) are applied (`resolver/Q1.java` `b()`), each returning
  an updated node or null (more than one non-null → `IllegalArgumentException "There is more than one updated
  node."`); if a value changed the device is persisted with `failedMessagesCounter = 0` (`:318-361`); finally
  `Pair(MeshStatusNotification(src, opcode, params), device)` is emitted on a `MutableSharedFlow` (buffer 300,
  `:58-59,367-398`). Vendor opcodes are re-packed as `(op|0xC0)<<16 | cidLo<<8 | cidHi` (`P7/a.java:56-64`),
  e.g. `0xD12705` = User Property Status.
* **State is keyed by the source element address**: each resolver stores its capability under
  `(CapabilityClass, elementAddress = msg.src)` in `Node.capabilities` (`p065f8/f.java:345-365`; base
  `resolver/P1.java:34-70`). Exception: `ConfigModelPublicationStatus` uses the element address from the payload
  (`Q1.java:88-91`, `resolver/J1.java`).
* Request/response matching (`de/jung/junghome/data/mesh/c.java`, `MeshMessengerImpl$processRequest$2.java:62-96`,
  `MeshNotificationChannel$waitForStatusMessage$1.java:172`): one request in flight, 3 attempts × 3000 ms, the
  reply is the first notification with `src == expected element && (opcode == expectedStatus || (vendorPropertyReq
  && opcode == 0xD12705))` — see `transport-provisioning.md` §3.5 and `vendor-models.md` §5. On
  `MaxRequestTimeoutException` the device's `failedMessagesCounter` is set to 3 = *unreachable* (`S1()` =
  `counter < 3`, `Y7/AbstractC0916e.java:361-363`; `MeshMessengerImpl$handleError$1.java:47-68`).

Live state (on/off, level, temperature …) is **only in memory** (`DeviceRepositoryImpl` `StateFlow`,
`DeviceRepositoryImpl.java:488`) plus Nordic's own `MeshNetworkDb`; it is re-read on every app start (§5.4).

### 5.2 SIG status → state (`StandardStatusMessageResolver.kt`, `de/jung/junghome/data/mesh/resolver/*`)

| Nordic status (opcode) | Resolver | Stored as |
|---|---|---|
| `GenericOnOffStatus` (0x8204) | `Y.java` | `GenericOnOffCapability` = presentState |
| `GenericLevelStatus` (0x8208) | `Q.java` | `GenericLevelCapability` present/target as **percent = round((level+32768)·100/65535)** (`domain/item/mesh/nodes/models/a.java:22`, `io/grpc/u.java:361-366`); also `TargetTemperatureCapability` (RTR set-point element, same conversion) |
| `LightLightnessStatus` (0x824E) | `L0.java` | lightness % of 0..65535; OnOff = lightness ≠ 0 |
| `LightCtlStatus` (0x8260) / `LightCtlTemperatureStatus` (0x8266) | `E0.java`, `F0.java` | `TunableWhiteValueCapability` lightness % and temperature % = `(K − 2000)/8000·100` |
| `LightLightnessDefault/RangeStatus`, `LightCtlDefault/TemperatureRangeStatus` | `H0`, `J0`, `a2`, `c2` | default / range capabilities |
| `GenericBatteryStatus` (0x8224) | `C1912f.java` | `BatteryCapability` indicator 0 CRITICAL, 1 LOW, 2 GOOD, 3 UNKNOWN |
| `GenericOnPowerUpStatus` (0x8212) | `C1898a0.java` | 0 OFF, 1 ON, 2 RESTORE |
| `GenericLocationGlobalStatus` (0x40) | `S.java` | `LocationCapability` |
| `SceneRegisterStatus` (0x8245) | `A1.java` | `SceneRegisterCapability` (scene list) + `SceneRecall.current`; scene list also persisted in `RegisterCapacityEntity.scenes` |
| `SchedulerStatus` (0x824A) / `SchedulerActionStatus` (0x5F) | `F1.java` / `D1.java` | timer register bitmask (persisted `RegisterCapacityEntity.schedulers`) / `Timer` entries |
| `TimeRoleStatus` (0x823B) | `V1.java` | `TimeRoleCapability` |
| `SensorStatus` (0x52) prop 0x004F Present Ambient Temperature | `F7/c.java` | `ActualTemperatureCapability` = raw × 0.5 °C, clamped 5..30 |
| `SensorStatus` (0x52) prop 0x0081 Active Power Load Side | `F7/b.java` | `ActivePowerCapability` = raw × 0.1 W |
| `SensorDescriptor/Cadence/Setting(s)Status` | `I1`, `H1`, `K1`, `L1` | raw Nordic values |
| `GenericPropertyStatus` 0x46 (SIG Manufacturer Property) props 0x0010, 0x0011, 0x001A, 0x006A, 0x006D | `U`, `V`, `X.java:318-375`, `X1`, `Y1` | hardware revision, name, **software version** (ASCII, persisted `SoftwareVersionEntity`), total energy, power-on time |
| `ConfigModelPublicationStatus` (0x8019) | `C1950s.java` (model ≠ 0x1100), `J1.java` (0x1100) | updates the model's publish address in the node / `SensorPublicationCapability` |
| `ConfigModelSubscriptionStatus`, `ConfigNetworkTransmitStatus`, `ConfigGattProxyStatus`, `ConfigKeyRefreshPhaseStatus`, `ConfigRelayStatus`, `ConfigBeaconStatus`, `ProxyConfigFilterStatus` | `C1953t`, `C1956u`, `C1959v`, `C1962w`, `C1965x`, `r`, `C1932l1` | corresponding config capabilities |

Not handled (no resolver): `SceneStatus` (0x5E), `TimeStatus` (0x5D), `GenericDefaultTransitionTimeStatus`. The
current scene is only known from `SceneRegisterStatus.currentScene`.

### 5.3 Vendor property status → state

All three LBC families (`C5`/`CB`/`D1 27 05`) share one parser (`resolver/AbstractC1972z0.java:38-67`:
`propertyId u16 LE`, `userAccess u8`, value LE) and ~90 per-property resolvers (`LBCPropertyStatusMessageResolver.kt`).
The property catalogue (IDs, names, value encodings) is in `vendor-models.md` §3.6 and `properties.md`; the ones that matter for the
network model and are additionally **persisted in Room** (`de/jung/junghome/data/persistence/**`, entities `L7/*`,
merge-on-load in `MeshPropertyRepositoryImpl$mergeWith$2.java:82-135`):

| Property | Meaning | Stored in |
|---|---|---|
| 0x0002 InsertId (User Property Get `CE 27 05`, `p234v7/C2351c.java`) | `u16 actuatorFunctionId` [+ `u16 insertType`] → `ActuatorId` (§5.5) | `ActuatorEntity(address, actuatorId JSON)` (= iOS cache type 0 *actuatorFunction*) |
| 0x5001 ButtonLayout | `u16 LayoutMode` | `ButtonLayoutEntity(address, layout)` (= type 1 *deviceLayout*) |
| 0x5002 KeyModeSceneConfig | `u16 sceneId`, `u32 transition ms` | `KeyModeSceneConfigEntity(address, sceneConfig JSON {sceneId, publishAddress, transitionTime})` (= type 2 *sceneNumber*) |
| 0x5003 KeyMode | `u8` key mode (§2.1); if ≠ Scene the cached scene config is reset (`resolver/C1960v0.java:178-204`) | in-memory `KeyModeCapability` (= type 3 *mode*) |
| 0x1104 MoveOperationMode | `u8` 0 BLINDS, 1 SHUTTER, 3 AWNING | `BlindOperationModeEntity` |
| 0x0005 STM32 version, SIG 0x001A software version | strings | `StmSoftwareVersionEntity`, `SoftwareVersionEntity` |
| 0x8245 scene register / 0x824A scheduler register | lists | `RegisterCapacityEntity(address, scenes[], schedulers[])` |

(= iOS type 4 *property* is the generic per-element property cache; on Android every other property lives only in
memory.) `KeySetPropertyMode` 0x5006 is written as `propertyId u16 LE ‖ mode u8` (`p234v7/E.java:52-53`).

### 5.4 What is requested, and when

| Trigger | Messages | Source |
|---|---|---|
| App start (`LoadInitialDeviceState`) — after project valid, network loaded, proxy connected | **`TimeSet` (0x5C) to `0xFFFF`, unacknowledged**, with the phone's TAI time (`W7/c.java`); then `Manufacturer Property Get` 0xC001/0xC002/0xC003 to every gateway; then `LoadStateForDevices(favorites)` | `networking/LoadInitialDeviceState$work$2.java:270-330`, `$3$1.java:62-72` |
| `LoadStateForDevices` | reachable devices in chunks of 5, 3 retries, 300 ms between chunks; per device without cached state: `GenericOnOffGet` (0x8201) to the OnOff element, blinds `GenericLevelGet` (0x8205) to the level element, RTR `SensorGet` (0x8231, prop 0x004F); all acknowledged | `networking/LoadStateForDevices.java` (`c()`, `d()`, `e()`), `$work$1.java:478,1240-1263`; builders `p234v7/C2395z.java:34`, `C2394y.java:34`, `C2349b.java:30` |
| Device page opened | lock function `Admin Get` 0x0009 (`DeviceDetailsViewModel$observeDevice$1.java:161-176`); OnOff Get if missing (`SimpleSwitchViewModel$requestGenericOnOffIfMissing`); blinds: `GenericLevelGet` for blind + slat elements and `Admin Get` 0x1104 (`BlindsViewModel$observeCompatibles$1.java:206-288`) | |
| `RequestSoftwareVersion` (remote-config gated) | SIG `Generic Manufacturer Property Get` (0x822B) prop 0x001A per physical device, then `Admin Get` 0x0005 for STM32 devices | `p234v7/T.java` |
| **Keep-alive** (`KeepLowPowerDeviceAwake`) | every **6 s** (simple-mode `KeepLowPowerDeviceAwake$work$1:90`) while no BLE process runs: acknowledged `Admin Get` **0x5001 ButtonLayout** to each button-layout-capable low-power device; retry after 1 s (`$work$2.java:44`) | `de/jung/junghome/domain/interactors/KeepLowPowerDeviceAwake.java:34-66` |
| Gateway REST polling | `GET /api/junghome/config` every 5 s (§6.1) | |

Unsolicited state therefore arrives through the element groups: every load's server models publish their status
to their own element group (§1.4) and the phone — connected to a proxy with an *empty black-list* filter
(`transport-provisioning.md` §3.4), i.e. receiving everything — sees it.

### 5.5 Enumerations a controller needs

* **ActuatorFunctionId** (`de/jung/junghome/domain/item/mesh/ActuatorFunctionId.java:22-277`): `0 Switch`,
  `1 TwoGangSwitch`, `2 Dimming`, `3 TwoGangDimming`, `4 TwDimming`, `5 Blind`, `6 Extension`, `7 NotAvailable`,
  `8 Rtr`, `9 Gateway`, `-1 Unset` (read from vendor User property 0x0002 InsertId, first `u16`).
* **InsertType** (`InsertType.java:40-110`): `0 Unknown`, `1 NoInsert`, `2 GenericInsert`, other `NotSupported`.
* **LayoutMode** (`domain/item/mesh/nodes/models/LayoutMode.java:29-35`, property 0x5001): `0 ONE_TOP_ONE_BOTTOM`,
  `1 ONE_ROCKER`, `2 TWO_LEFT_TWO_RIGHT`, `3 ONE_LEFT_TWO_RIGHT`, `4 TWO_LEFT_ONE_RIGHT`, `5 ONE_LEFT_ONE_RIGHT`,
  `255 UNKNOWN` (iOS cache "deviceLayout 5 = 2 full-surface buttons").
* **Product IDs → device class** (`Y7/C0919h.java:112-147`, company 0x0527, PID formatted `"0x%04X"`):
  `0x0001` push-button 1-gang (`Z7/a`), `0x0002` push-button 2-gang (`Z7/g`), `0x0003` measuring socket (`Y7/r0`),
  `0x0004` mini actuator hardwired switch (`p023b8/b`), `0x0005`/`0x0006` wall transmitter 1/2-gang (battery,
  `Z7/i`, `Z7/j`), `0x0007`/`0x0008` motion detector, `0x0009` presence detector (`p012a8/*`), `0x000A` RTR
  (`RtrDevice`), `0x000B` gateway (`Y7/g0`), `0x000C` switch socket (`Y7/s0`), `0x000D` mini actuator blind,
  `0x0010..0x0016` "puck" mini actuators (energy / 2-gang / dimming / blind v2 / dimming TW / 230 V / battery,
  `MiniActuatorDevice.java:513-556`). Virtual load devices are created per extra location id from the actuator
  function (`C0919h.java:150-205`: Blind → `BlindDevice`, Switch → `SwitchLampDevice` (`MeasureLampDevice` for
  PID 0x0010), Dimming → `DimLampDevice`, TwDimming → `TunableWhiteLampDevice`).
* Element **location IDs**: `0x0001..` load outputs, `64..67` (0x40..0x43) button/sensor elements A–D
  (`p065f8/c.java:12`; iOS export shows `0040`, `0042`, `0044`).

---

## 6. Gateway, time, scheduler, multi-provisioner and project file

### 6.1 Gateway node

* Recognised by `companyId == 0x0527 && productId == "0x000B"` (`Y7/h0.java:6-10`, `Y7/C0919h.java:78-79`). It has
  no device-type group (`x1()` not overridden), gets an element group for its primary element like every node
  (§1.4), and acts as an "actuator" target via the element that carries the `0x0527:1013` server
  (`Y7/g0.java:237`).
* **Buttons/keys → gateway** = `SetDeviceConnection` with key mode `Gateway(6)` (§2.3): the key's `0x0527:1015`
  client publishes to the gateway's element group (`0xC005` in the iOS export) and the gateway's `0x0527:1013` server
  is subscribed to that group (§1.4).
* **Sensor values → gateway** (`ConfigurePublicationForSensorServer`, per-device setting "sensor values for
  gateway", firmware ≥ 1.3.0.0, `P8/x.java:129-134`, `data/cloud/api/a.java:53-58`): for every `0x1100 Sensor
  Server` of the device, `ConnectToAddress(device, [sensorServer], elementGroupOf(sensorElement), PUBLISH_ONLY)`
  (simple-mode `ConfigurePublicationForSensorServer.g:154-177`) — i.e. **`ConfigModelPublicationSet` to the
  sensor element's own element group**, period 0 (no periodic publication is configured by the app; the firmware
  publishes on its own cadence). Nothing is subscribed on the gateway by the app. (Firmware: the gateway configures
  itself from the project file it holds (below) — its *self-config* subscribes the gateway's own client models to
  every element group the devices publish to, using local `test_add_local_model_sub` BGAPI commands, not Config
  messages (221 desired / 202 current subscriptions in a gateway log), **and** it polls every server it knows
  with a Get every 15 s (`device_state_poll_interval_sec`); the gateway is an ordinary node, not a Config Client.
  `docs/cross-repo-analysis.md` §1.1, D12.) Removal = publication to `0x0000`
  (`RemoveConnectionForAddress.Params.SensorConnection`, `ConfigurePublicationForSensorServer.java:257-270`).
  `CheckPublicationForSensorServer` sends `ConfigModelPublicationGet(element, 0x1100)` and treats a non-zero
  publish address as "on" (`CheckPublicationForSensorServer.java:268-321`, `p234v7/F0.java:17-26`).
* **Gateway network credentials over mesh** (`ConfigureGateway`, run at the end of device set-up, and again at app
  start / on HTTP 401/404/503): LBC **Manufacturer Property Get** (`C8 27 05`) of `0xC001 GatewayAPIToken`,
  `0xC002 GatewayIP`, `0xC003 GatewayFingerprint` (ASCII values; `p245w7/a,b,c.java`, `resolver/K,L,M.java`),
  3 s timeout (`ConfigureGateway$work$2.java:232-245`). They feed the REST client: `https://<ip>/api/junghome/…`,
  header `Token: <token>`, TLS pinned to the SHA-256 fingerprint (`de/jung/junghome/data/mesh/api/b.java`,
  `p212t7/a.java:12-38`, `data/gateway/a.java:25-48`). `GET config` is polled every 5 s (`PollGatewayConfig`);
  `POST config {"data":{"project_file": <ExportDto JSON>}}` uploads the project file (`GatewayRepositoryImpl.java:689-693`).
  The app never downloads a project file from the gateway.

### 6.2 Time

* Opcodes: `TimeSet 0x5C`, `TimeStatus 0x5D`, `TimeGet 0x8237`, `TimeRoleGet/Set/Status 0x8238/0x8239/0x823A`
  (`no/nordicsemi/android/mesh/opcodes/ApplicationMessageOpCodes.java:110-115`). TAI epoch 2000-01-01, TAI−UTC delta
  37 s, `subSeconds = millis/256` (only 0..3 — app quirk), `timeZoneOffset = minutes/15`, uncertainty 0, authority
  false (`W7/c.java:29-31,46,91-94`; Nordic packing `TimeSet.java:23-32`).
* `TimeSet` is sent **unicast to the Time Server element of every newly configured device** (`ConfigureDevice.e()`,
  `ConfigureDevice.java:295-341`, step `SetTime`) and **broadcast to 0xFFFF at every app start** (§5.4).
* **Time keeper** (`TimeKeeperConfiguration.java`, `EnsureTimeKeeper.java`): only needed while legacy "PP2" nodes
  (PID 0x0010..0x0014) exist (`EnsureTimeKeeper.java:68-105`); the gateway and battery transmitters are never
  chosen (`ObserveDevices.java:982`, `Y7/AbstractC0916e.java:340-358`); priority list `MiniActuatorHardwired,
  SwitchMeasureSocket, SwitchSocket, MotionDetector, PresenceDetector, TwoGangRocker, OneGangRocker, Rtr`
  (`ObserveDevices.java:96`). Configuration of the keeper: `ConfigModelPublicationSet` of its `0x1200 Time Server`
  to **`0xFEFF`** (PUBLISH_ONLY, period 0; `TimeKeeperConfiguration$setPublicationForTimeServer$2$1.java:61`),
  `TimeRoleSet = 2 MeshTimeRelay` on its `0x1201 Time Setup Server` (`TimeKeeperConfiguration.java:741-756`,
  `p234v7/N0.java:41-44`); every PP2 node's Time Server gets `ConfigModelSubscriptionAdd(0xFEFF)`
  (`EnsureTimeKeeper.java:281-354`). Removal: `TimeRoleSet = 3 MeshTimeClient` + publication `0x0000`.
  `TimeRole` values `None 0, MeshTimeAuthority 1, MeshTimeRelay 2, MeshTimeClient 3`
  (`domain/item/mesh/TimeRole.java:16-73`). Daylight-saving flag = Admin property 15 (`p234v7/C2357f.java:39-51`).

### 6.3 Schedules and timers

* **Vendor JH Scheduler** (`0x0527:1016`, opcodes `D2` Get / `D4` Set / `D3` Status): 16 slots, sub-commands
  Schedule 0 / Action 1 / EffectiveTime 2 / ScheduleList 15; bit layouts in `vendor-models.md` §4.1. Interactors
  `domain/interactors/schedules/*`: `CreateJHSchedule` picks the first *Available* slot, optionally sends
  `GenericLocationGlobalSet` for astro schedules, then Schedule Set + Action Set (`CreateJHSchedule.java:591-732`);
  `ToggleJHSchedule` sends the 2-byte "type only" Set (`ToggleJHScheduleKt.java:82-88`); `DeleteJHSchedule` sets type
  *Available* (`DeleteJHSchedule.java:142-143`); `RequestJHSchedules` = ScheduleList Get then EffectiveTime + Action
  Get per used slot (`RequestJHSchedules.java:180-207,330-343`).
* **SIG Scheduler** (`0x1206/0x1207`, "Timers", `domain/interactors/timer/*`): `SchedulerGet 0x8249` → register,
  `SchedulerActionGet 0x8248`, `SchedulerActionSet 0x60` (acked, `B7/g.java:42-51`), `SchedulerActionStatus 0x5F`.
  `CreateTimer` (`CreateTimer$work$2.java:137-244`): next free index (`GetNextAvailableTimerIndex.java:56-84`); for
  scene-recall timers it first creates a CDB scene `"TimerScene <index> <deviceId>"` and stores the current state
  in it (`StoreDeviceConfigurationInScene`); entry = `Timer(index, Year.Any=100, all months, Day.Any=0, h, m, 0,
  dayOfWeek, action, scene)` (`E7/i.java:115-318`); metadata in `SchedulerMetaInfoEntity(index, deviceId, action,
  scene)`. Astro timers additionally write Admin property 7 `AstroSchedulerRegister` (40-bit: registerId(4) mode(4:
  0 static, 1 sunrise, 2 sunset) offsetMin(8) earliestHH(5) earliestMM(6) latestHH(5) latestMM(6), `B7/a.java:59-70`)
  and read property 8 `AstroSchedulerStatus`.
* **Thresholds** (measuring sockets): Admin properties 0x5004 TurnOnThreshold / 0x5005 TurnOffThreshold
  (`p234v7/L0.java:34-63`); `CreateThreshold` wires the socket's `0x1001 OnOff Client` PUBLISH_AND_SUBSCRIBE to the
  socket's own element group and the target loads' OnOff servers SUBSCRIBE_ONLY to it (simple-mode
  `CreateThreshold:158-336`).

### 6.4 Provisioners, users and the project file

* Provisioner name = `Settings.Secure.ANDROID_ID` (`p122l1/G.java:314,384`); max 10 provisioners
  (`de/jung/junghome/domain/interactors/cloud/AcceptParticipantRequest.java:335-338`). Ranges: §1.1. Owner creates a cloud project with its provisioner UUID;
  a participant scans a QR code = Base64 `{"id": <projectId>, "key": <base64 AES key>}`
  (`domain/item/ProjectQrCode.java:10-12`) and files an access request; on acceptance the **owner creates a new
  provisioner named after the participant** (`z1(name)`, `domain/interactors/cloud/AcceptParticipantRequest.java:338`), exports, and the
  participant imports and selects "its" provisioner by UUID (`PrepareMeshNetwork(SelectUserProvisioner)` →
  `T1(name, uuid)`, `MeshNetworkRepositoryImpl.java:608-659`; missing → `NoProvisionerFoundForUserException`).
  `RemoveUser` only deletes the cloud member document — no provisioner removal, no key refresh
  (`domain/interactors/networking/RemoveUser$work$2.java:76-125`).
* **Project file** = `ExportDto` (Gson, `de/jung/junghome/domain/dto/ExportDto.java:139-141`):
  `{"version":"1.1", "appVersion":"2.2.0 (822956)", "platform":"Android (…)", "meta":{…}, "network":"<Base64 NO_WRAP
  of the Nordic mesh CDB JSON>"}` (`MeshNetworkRepositoryImpl.java:973-998` export, `:1105-1107` import via
  `importMeshNetworkJson`). `meta` (`MetaData.java`): `userGroups[{name,address,icon}]`,
  `elementConnectionGroups[{groupAddress,elementAddress}]`, `devices[{name,macAddress,deviceId{actuatorFunctionId,
  insertType,locationIds[],nodeId,productId},cachedGroupConnectionMetadata[]}]` (= `KeyModeGroupConfig`),
  `scenes[{name,number,icon}]`, `sceneInfo[…]`, `schedulerMetaInfo[…]`, `timer[…]`, `actuatorExports[{elementAddress,
  actuatorId}]`, `buttonLayoutExports[{elementAddress,mode}]`, `keyModeSceneConfigExports[{elementAddress,
  sceneConfig{sceneId,transitionStepSeconds,transitionResolution,publicationAddress}}]`. Device-type groups are
  **not** exported (they are fixed addresses). This is the Android counterpart of the iOS `groups.json`,
  `element_connection_groups.json`, `cachedGroupConnectionMetadata` files, all in one document.
* Local export: plain JSON `<filesDir>/JungHome.json` (`FileServiceImpl$exportNetworkFile$2.java:54-68`). Cloud:
  compact JSON encrypted AES-256-GCM (`IV(12) ‖ ciphertext+tag`, `p191r7/b.java:31-64`) uploaded to Firebase Storage
  `projects/<projectId>/JungHome.json`; Firestore `projects/<id>/members/<uid>{provisionerId, role, …}`
  (`CloudDto.java:17-32,214-221`). Any local change → 5 s debounce → export to cloud + gateway; a remote change
  (not uploaded by this user) → 5 s → import (`ObserveSyncEvent$work$2.java:73-97`,
  `ImportProjectFromCloudStorage$work$2$1.java:101-132`). **There is no merge: last writer wins**; the importer only
  re-selects its provisioner and keeps `seq = max(stored, imported)` (`MeshNetworkRepositoryImpl.java:322-373,1220-1250`).
* Nordic defaults are kept: mesh name `"nRF Mesh Network"`, NetKey `"Network Key 1"`, AppKeys `"Application Key
  1..3"` (`no/nordicsemi/android/mesh/BaseMeshNetwork.java:59`, `MeshManagerApi.java:558-589`); only the first
  AppKey is ever bound/used (`P7/a.java` `a()`, `de/jung/junghome/data/mesh/c.java:186-197`). (The iOS-created
  network in the export uses iOS names "Primary Network Key"/"Default Application Key".)
* **KeyRenewal** (manual, hidden mesh-network screen): NetKey-only refresh over all nodes incl. the gateway —
  `ConfigNetKeyUpdate` (0x8045) → `ConfigKeyRefreshPhaseSet(2)` (0x8016) → local switch → `ConfigKeyRefreshPhaseSet(3)`
  → export (`networking/KeyRenewal.java:203-273`, `p234v7/C2360g0.java:45-61`, `p234v7/W0.java:52-70`); no
  `ConfigAppKeyUpdate` exists in the app.

---

## 7. Recipe for a third-party controller

**To be a passive observer / controller on an existing JUNG HOME network** (import the project file's `network`
CDB JSON, use AppKey 0):

1. Control loads directly: acknowledged `Generic OnOff Set` / `Light Lightness Set` / `Light CTL Set` /
   `Generic Level Set` / `Generic Delta Set` to the **load element unicast** (§4.2), or unacknowledged sets to the
   fixed device-type groups `0xFEF5` (lamps), `0xFEF6` (blind position), `0xFEF7` (slats), `0xFEF8` (sockets),
   `0xFEF9` (RTR set-point) (§4.4), or to a **room group** (rooms subscribe the first-element OnOff/Level servers,
   §2.4), or to a **load's element group** (all its supported servers listen there, §1.4). Scenes: `Scene Recall
   Unacknowledged` to `0xFFFF` (§4.3). RTR set-point is a Generic Level with `pct = (°C − 5)/25·100`.
2. Learn state: subscribe (as a proxy client with an empty black-list filter, or by subscribing your own
   element) to the **element groups** — every load publishes its OnOff/Level/Lightness/CTL/Scene/vendor-property
   status there (§1.4, §5.1). Poll with `Generic OnOff Get` / `Generic Level Get` / `Sensor Get 0x004F` on start
   (§5.4). Keep battery transmitters awake with an `Admin Get 0x5001` every 6 s if you need to configure them.
3. Interpret buttons: a key element's publication address tells you what it does — a load's element group
   (device connection), its own element group (room connection; the members' subscriptions and the
   `cachedGroupConnectionMetadata` say which room/function), `0xFFFF` on the Scene Client (scene; number in
   the element's `KeyModeSceneConfig` property), the gateway's element group (gateway link) — plus the element's
   `KeyMode` property (§2.1, §2.7).

**To add or rewire devices the way the app does:**

1. Provision No-OOB; unicast = next free in your provisioner range; name the node after its BLE name (§3.1).
2. Run the configuration sequence of §3.2 (AppKey Add, Composition Data, bind list §3.3, Proxy on, TTL 5, relay
   3/90 ms, network transmit 3/100 ms, beacon on, InsertId read, Time Set, element groups, device-type groups,
   device-specific defaults §3.4, GATT proxy off for battery devices).
3. Element groups: allocate `element group #<elementAddress>` (next free address ≥ `0xC000`, shared counter with
   rooms) for every element that has a "supported server" model, and give **all** those models (except Sensor
   Server) publish **and** subscribe to it with `ConfigModelPublicationSet(ttl 0xFF, period 0, retransmit 0)` +
   `ConfigModelSubscriptionAdd` (§1.4, §1.5). Record the map (`elementConnectionGroups`) in the project file (§6.4).
4. Rooms: create a group, subscribe each member's first-element `0x1000`/`0x1002` servers to it (§2.4).
5. Button → device: subscribe nothing on the load; on the button element delete existing pub/sub, set the client
   models of the chosen key mode (§2.1) to publish+subscribe to the **load's element group**, write `KeyMode`
   (Admin property 0x5003) (§2.3). Button → room: publish+subscribe the client models to the **button's own element
   group**, subscribe the matching server models of every room member to that group, write `KeyMode`, and store a
   `KeyModeGroupConfig {elementAddress, groupAddress, publishAddress, function}` in the project file (§2.4). Button →
   scene: Scene Client publishes to `0xFFFF`, write `KeyModeSceneConfig` (0x5002) and `KeyMode = 2` (§2.5). Button →
   gateway: like button → device with key mode 6 and the gateway's element group as target (§2.3, §6.1). Lock /
   RTR-property links additionally write `KeySetPropertyMode/ValueUpOn/ValueDownOff` (§2.6).
6. Scenes: allocate the lowest free scene number, put each load in the wanted state, send `Scene Store` to its
   Scene Setup Server (+ vendor `SceneActionSetupSet` on 0x0527:1017 nodes) (§4.3).
7. Keep the project file (`ExportDto`) in sync so the app can display your changes (§6.4).

## 8. Open points / uncertainties

* `ConnectToAddress` publication period defaults (30 steps / resolution 1) are constant-folded by R8; every
  observed call site uses flags `112` → period 0, so no periodic publication is configured. Device firmware
  publishes status on change on its own.
* The exact PDU emitted by the vendor client `0x0527:1015` is not visible in the app (see `vendor-models.md` §7).
* The `use_status_message_optimization` reduced wiring (§1.4) was not observed in the field; the default branch
  matches the iOS export.
* Detector Sensor Server publications (to the sensor element group, §6.1) are configured with period 0; the
  cadence is decided by the firmware / gateway, not by the app.
* jadx inverted some `if` conditions in the default output (e.g. reachability check in `MeshMessengerImpl`, blind
  element selection in `ConnectToDeviceTypeGroup`); statements above were taken from the simple-mode decompile
  where the two disagreed.
