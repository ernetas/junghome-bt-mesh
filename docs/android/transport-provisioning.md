# JUNG HOME Android app — Bluetooth transport, discovery, provisioning, proxy and security

Source: JUNG HOME 2.2.0 APK, decompiled with jadx into `android/jadx-out/sources/`. All citations below are
`path:line` relative to that directory. JUNG's code lives in `de/jung/junghome/**` (Kotlin, names intact; a few
classes were minified to single letters, e.g. `de/jung/junghome/data/mesh/c.java` is `MeshMessengerImpl.kt`).
Nordic's nRF Mesh library is `no/nordicsemi/android/mesh/**`, Nordic's Android-BLE-Library is `no/nordicsemi/android/ble/**`,
and Nordic's scanner-compat is `no/nordicsemi/android/support/v18/scanner/**`.

The system is a plain **Bluetooth SIG Mesh 1.0.x** network. The phone is a GATT-only provisioner/client that reaches the
mesh through the Mesh Proxy service of any provisioned node. Nothing JUNG-specific happens below the access layer, so a
third-party client that has the NetKey/AppKey/DevKeys from `MeshNetwork.json` can talk to every device with a
standard mesh stack. Everything proprietary is in the vendor models (see `vendor-models.md`) and in the *firmware
update* path, which does not use the mesh at all (section 5).

---

## 1. Bundled Nordic library and JUNG patches

### 1.1 Version

- `no/nordicsemi/android/mesh/BuildConfig.java:4-8` carries no version (`BUILD_TYPE="release"`, `DEBUG=false`,
  `LIBRARY_PACKAGE_NAME` only). No Nordic entry exists in `resources/META-INF` either.
- Fingerprints: Room schema **version 12**, identity hash `8394ae9cb3679dd212c1ebfb9789bdc2`
  (`no/nordicsemi/android/mesh/MeshNetworkDb_Impl.java:125`, `:142`), migrations 1→12 registered in
  `no/nordicsemi/android/mesh/MeshNetworkDb.java:64-140` (9→10 adds `partial`, 10→11 adds `networkExclusions` /
  renames blacklisted→excluded, 11→12 rebuilds `mesh_network` with `network_exclusions`; DB file
  `mesh_network_database.db`, `:205`). `logger/MeshLogger.java:40` (`setLogHandler`),
  `MeshManagerApi.java:801` (`allowIvIndexRecoveryOver42`), `:1050` (`setIvUpdateTestModeActive`), `:1103`
  (`identifyNode(UUID, int attentionTimer)`) and `:1111` (`exportMeshNetwork(NetworkKeysConfig, …)`) are all late-3.3.x
  features. Conclusion: **stock nRF Mesh Android library 3.3.x** (late 3.3 series).
- Exported JSON: `$schema = http://json-schema.org/draft-04/schema#`,
  `id = https://www.bluetooth.com/specifications/specs/mesh-cdb-1-0-1-schema.json#`, `version = "1.0.1"`
  (`no/nordicsemi/android/mesh/BaseMeshNetwork.java:47-55`, written at `MeshNetworkDeserializer.java:356-359`).
- BLE library: `no/nordicsemi/android/ble/BleManager.java:356` (`setConnectionParametersListener`),
  `BleManagerHandler.java:647-653` (API-33 value callbacks), `:1620-1623` (`SDK_INT >= 34` `connectGatt` autoConnect
  path) → **Android-BLE-Library 2.7.x**, plus `no/nordicsemi/android/support/v18/scanner` (scanner-compat).

### 1.2 Mesh 1.1 support: names only

`no/nordicsemi/android/mesh/models/SigModelParser.java` knows the Mesh 1.1 model IDs — Remote Provisioning 0x0004/0x0005
(`:65-66`), Private Beacon 0x000A/0x000B (`:59-60`), On-Demand Private Proxy 0x000C/0x000D (`:61-62`), SAR Configuration
0x000E/0x000F (`:67-68`), Opcodes Aggregator 0x0010/0x0011 (`:63-64`), Large Composition Data 0x0012/0x0013 (`:39-40`),
Solicitation PDU RPL 0x0014/0x0015 (`:78-79`), BLOB Transfer 0x1400/0x1401 (`:7-8`), Firmware Update 0x1402/0x1403
(`:13-14`), Firmware Distribution 0x1404/0x1405 (`:11-12`) — but each maps to a 42-line name-only stub
(e.g. `models/BlobTransferServer.java:26-28`). There are **no** 1.1 messages or opcodes:
`opcodes/ConfigMessageOpCodes.java:5-75` and `ApplicationMessageOpCodes.java:5-119` contain only Mesh 1.0 opcodes,
`transport/` has no Blob/Firmware/PrivateBeacon/SAR/LargeComp/Aggregator/RemoteProvisioning classes, and the only
provisioning algorithm is FIPS P-256 (`utils/AlgorithmType.java:11`). This is exactly upstream 3.3.x. Consequently the
app cannot do mesh DFU; firmware updates go over a direct GATT connection (section 5).

### 1.3 JUNG modifications inside `no.nordicsemi`

`grep -rn -i "jung\|LBC\|0x0527\|1319" no/nordicsemi/` finds only `import de.jung.junghome.app.ui.timer.profiles.d`
in ~40 transport classes; that class is an R8-outlined string-builder helper
(`de/jung/junghome/app/ui/timer/profiles/d.java:23` "compiled from: R8$$SyntheticClass"), not a patch. Real deltas
versus upstream:

1. `MeshManagerCallbacks` gained `onNodeAdded`, `onNodeDeleted`, `onSceneAdded`, `onSceneDeleted`
   (`no/nordicsemi/android/mesh/MeshManagerCallbacks.java:22-28`). `MeshManagerApi` calls them at `:227`, `:395-399`,
   `:402-407` (`onNodeDeleted` *replaces* the upstream `onMeshNetworkUpdated()` call, so deleting a node no longer
   bumps the network timestamp), `:439-450`. The app implements all four as no-ops
   (`de/jung/junghome/data/repositories/MeshNetworkRepositoryImpl.java:1370-1385`).
2. `BaseMeshNetwork.updateNodeSequenceNumber(ProvisionedMeshNode, Integer)` (`BaseMeshNetwork.java:974-981`, bridged
   at `MeshNetwork.java:785-786`) — lets the app overwrite the provisioner's sequence number (used on import and from the
   debugging UI, `MeshNetworkRepositoryImpl.java:1220-1248`, `de/jung/junghome/data/repositories/p.java:196`).
3. `MeshNetwork.setIvIndex(IvIndex)` is public and used by the debugging UI to force an IV index
   (`MeshNetwork.java:697-700`, `de/jung/junghome/data/repositories/p.java:147`).

Transport, crypto (`utils/SecureUtils.java:25-41` standard salts), provisioning state machine, beacon handling and
opcodes are unmodified.

### 1.4 Library constants a client should mirror

| Item | Value | Source |
|---|---|---|
| Provisioner / node default TTL | 5 | `Provisioner.java:66`, `transport/ProvisionedBaseMeshNode.java:65` |
| Proxy-configuration TTL | 0 | `transport/MeshTransport.java:13`, `:64-65` |
| Block-ack timer | `150 + 50·TTL` ms | `transport/LowerTransportLayer.java:14`, `:155`, `:167` |
| Incomplete (SAR RX) timer | 10 s | `LowerTransportLayer.java:15`, `:143-146` |
| Segment payload | 12 B (control 8 B), unsegmented max 15 B (control 11 B) | `transport/UpperTransportLayer.java:19-22` |
| Proxy PDU SAR | bits 7..6 of byte 0: 0 complete, 1 start (OR `0x40`), 2 cont (OR `0x80`), 3 end (OR `0xC0`); reassembly timeout 20 s | `MeshManagerApi.java:56-70`, `:509-538`, `:748-755` |
| Proxy PDU types | 0 network, 1 beacon, 2 proxy config, 3 provisioning | `MeshManagerApi.java:65-68` |
| MTU | app requests 517, gives the library `MTU-3` | `BluetoothMessagingServiceImpl.java:438`, `:584` |
| Publish TTL used by app | 255 (node default) | `p234v7/C2383s0.java:58` |
| Secure Network Beacon | authenticated with every NetKey, only primary subnet accepted, IV index applied if greater and `canOverwrite` (≤ +42, 96 h rule unless test mode); **provisioner sequence number reset to 0 when the TX IV index grows**; exclusions older than `ivIndex-2` purged | `MeshManagerApi.java:684-742`, `SecureNetworkBeacon.java:59-69` |
| Key-refresh flag in beacons | parsed but never acted on | `SecureNetworkBeacon.java:42`, no other consumer |
| SeqAuth replay check | accept iff no SeqAuth stored for the source or `stored < seqAuth` | `LowerTransportLayer.java:172-175` |
| Sequence number per PDU | `ProvisionedMeshNode.incrementSequenceNumber()`; proxy-config messages consume **two** numbers (upstream quirk) | `transport/NetworkLayer.java:281-282`, `:306`, `MeshTransport.java:54` |

---

## 2. Device discovery

### 2.1 Scanner set-up

All scanning goes through one `BluetoothScanServiceImpl` built on Nordic's `BluetoothLeScannerCompat`:

- Settings: `setLegacy(false)`, `setScanMode(2)` (= `SCAN_MODE_LOW_LATENCY`), `setReportDelay(0)`, callback type 1
  (`CALLBACK_TYPE_ALL_MATCHES`) — `de/jung/junghome/data/bluetooth/BluetoothScanServiceImpl.java:84`, `:95`, `:214`.
- **No hardware scan filter is used**: `a(ScanFilter.Builder)` starts the scan with an empty `ScanFilter.Builder().build()`
  (`:94-96`, `:204`, `:213-215`). Filtering by service UUID happens in software on the scan record's *service UUID list*
  (`scanRecord.getServiceUuids().contains(ParcelUuid(uuid))`, `:285`) in `b0(UUID)` = `startScanning(serviceUuid)`,
  or by MAC address (`E1(UUID, address)` = `startScanningForOneDevice`, case-insensitive compare, `:157-164`).
- Stop = `flushPendingScanResults` + `stopScan` (`:320-321`).

### 2.2 Unprovisioned devices (`DeviceDiscoveringImpl`)

`DeviceDiscoveringImpl.q1(previousMac)` = `startDiscovering()` subscribes to `startScanning(null)` (no UUID filter,
`de/jung/junghome/data/mesh/DeviceDiscoveringImpl.java:65`) and for every `ScanResult`
(`DeviceDiscoveringImpl$startDiscovering$2.java`):

1. **Unprovisioned Device beacon over GATT advertising**: the record must carry *service data* for
   `MeshManagerApi.MESH_PROVISIONING_UUID` = `00001827-0000-1000-8000-00805F9B34FB`
   (`no/nordicsemi/android/mesh/MeshManagerApi.java:63`). The first 16 bytes of that service data are hex-encoded and
   split `8-4-4-4-12` into the **Device UUID** (`DeviceDiscoveringImpl$startDiscovering$2.java:80-91`). The trailing
   2-byte OOB-information field of the 0x1827 service data is ignored.
2. RSSI classes: `>= -30` strong, `>= -70` medium, else weak (`:102-105`).
3. The result is stored as an `UnprovisionedDevice` (`de/jung/junghome/domain/item/b.java`): `uuid`, `rssi`, `name`,
   `macAddress` (= `BluetoothDevice.getAddress()`), `LBCAdvertisementData?`, `softwareVersion`, `stm32Version`
   (`b.java:36-45`). Devices are keyed by **MAC address** (`:118`, `:197`).
4. **Manufacturer-specific data**: the raw scan record is parsed into AD structures
   (`:127-167`), the first structure with AD type `0xFF` (`:176`) is fed to `LBCAdvertisementData(previousMac, data)`
   (`:182`); a vendor mismatch is silently ignored (`:184`).

The scan runs for 30 s by default (`de/jung/junghome/domain/interactors/devices/ScanUnprovisionedDevices$work$1.java:311`).

### 2.3 JUNG manufacturer-specific AD structure (`LBCAdvertisementData`)

`de/jung/junghome/domain/item/bluetooth/LBCAdvertisementData.java:76-124`, little-endian (`:78`). `data` is the payload
of the `0xFF` AD structure **including** the company ID:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 2 | `vendorId` | must be `1319` = `0x0527` Albrecht JUNG (`:83`), else `NoVendorIdMatchException` |
| 2 | 1 | `jungAdvType` | 1, 2 or 3 → selects the layout below (`:94`, `:103`, `:112`) |
| 3 | 2 | `productId` | JUNG product ID (same numbering as the Composition Data PID, see `firmware-products.md`) |
| type 1 (total 7 B) | 1+1 | `actuatorFunctionId` (u8), `buttonLayout` (u8) | `:94-101` |
| type 2 (total 9 B) | 2+2 | `actuatorFunctionId` (u16), `buttonLayout` (u16) | `:103-110` |
| type 3 (total 15 B) | 2+2+6 | `actuatorFunctionId` (u16), `buttonLayout` (u16), **MAC address** (6 B, stored reversed; app prints it byte-reversed, `:`-joined, upper-case, `:126-136`) | `:116-123` |

Any other type/length gives `ActuatorFunctionId.Unknown(-1)` / `LayoutMode.UNKNOWN` (`:112-115`).
The type-3 MAC field lets the app re-identify a device even when the vendor ID does not match: if the caller passed a
`previousMac` and the trailing 6 bytes equal it, parsing continues (`:84-92`); otherwise `NoVendorIdOrMacMatchException`.

Product-ID → device name mapping used on the scan list (`de/jung/junghome/app/ui/project/gateway/detail/z.java:309-383`,
string getters resolved in `de/jung/junghome/app/utils/o.java`):

| productId | Device |
|---|---|
| 0x0001 / 0x0002 | push-button 1-gang / 2-gang (`device_name_one_gang_rocker` / `two_gang_rocker`) |
| 0x0003, 0x000C | socket |
| 0x0004 | mini actuator; 0x000D mini actuator blind |
| 0x0005 / 0x0006 | wall transmitter 1-/2-gang (battery) |
| 0x0007 / 0x0008 | motion detector short / long |
| 0x0009 | presence detector |
| 0x000A | room thermostat (RTR) |
| 0x000B | **gateway** |
| 0x0010 … 0x0016 | "puck" family: switch, switch 2-gang, dimming, blind, dimming TW, 230 V, 230 V battery |

`UnprovisionedDevice.c()/d()/e()` classify 0x0001/0x0005, 0x0002/0x0006 and 0x0004/0x000D respectively
(`de/jung/junghome/domain/item/b.java:79-116`).

### 2.4 Device UUID ↔ MAC address, `node_gattdata`

The Device UUID is **generated by the device firmware**, not by the app: the app takes it verbatim from the 0x1827
service data (2.2 step 1). The observed pattern `30FB10FF-FE30-E756-0000-000000000000` ↔ `30:FB:10:30:E7:56` is the
EUI-64 expansion (`FF FE` inserted after the OUI) that the Silicon Labs Bluetooth Mesh stack uses by default for its
device UUID; the app nowhere computes it. What the app *does* store is the MAC it connected to when provisioning
succeeded: `NodeProvisioningServiceImpl.onProvisioningCompleted()` takes `bluetoothDevice.getAddress()` of the current
GATT connection and publishes `(nodeUuid, macAddress)` through `DeviceSetupService.e(uuid, address)`
(`de/jung/junghome/data/mesh/NodeProvisioningServiceImpl.java:183-198`, `de/jung/junghome/domain/interactors/networking/DeviceSetupServiceImpl.java:69-75`).
That MAC ends up in the app's own Room table `deviceentity` (columns `id, name, macAddress, keyModeGroupConfigs,
isFavorite, value`; `de/jung/junghome/data/persistence/daos/C1976c.java:48-54`; domain accessor
`AbstractC0916e.G1()` = `macAddress`) — the Android equivalent of the iOS `node_gattdata` peripheralId/macAddress
mapping — and is exported in the project file as `meta.devices[].macAddress` next to `deviceId.nodeId`
(`de/jung/junghome/domain/dto/DeviceExport.java:20`, `DeviceIdentifierExport.java:18`, `ExportDto.java:126`). It is
what proxy selection matches against (`DeviceRepositoryImpl.x1()` = `findDevicesByMacAddress`,
`de/jung/junghome/data/repositories/DeviceRepositoryImpl.java:1143-1183`). Because JUNG devices use their public
static address for all advertising, a third-party client can derive the MAC from the node UUID (`bytes 0-2 + bytes 5-7`)
and vice-versa.

### 2.5 Provisioned devices (proxy candidates)

`ConnectToProxy` scans for records whose *service UUID list* contains `MESH_PROXY_UUID` =
`00001828-0000-1000-8000-00805F9B34FB` (`no/nordicsemi/android/mesh/MeshManagerApi.java:64`; injected as a Koin
singleton, `W5/k.java:211`), then checks the 0x1828 service data with `MeshNetworkRepositoryImpl.d0()`
(`de/jung/junghome/data/repositories/MeshNetworkRepositoryImpl.java:697-718`):

- **Network ID** advertising (`Identification Type 0x00`, 8-byte NetworkID = k3(NetKey)) →
  `MeshManagerApi.isAdvertisingWithNetworkIdentity()` / `networkIdMatches()` accepts the current *or the old* network ID
  of any NetKey (`MeshManagerApi.java:986`, `:1121-1129`; offsets `ADVERTISED_NETWORK_ID_OFFSET = 1`, length 8, `:50-51`).
- **Node Identity** advertising (`0x01`, 8-byte hash + 8-byte random) → `isAdvertisedWithNodeIdentity()` and
  `nodeIdentityMatches(node, data)` for every non-provisioner node (`P7/a.java:62-76`; hash offset 1, random offset 9,
  `MeshManagerApi.java:48-53`).

Only devices whose MAC is known in the app database are connected to (`ConnectToProxy$connectToProxy$2.java`, call to
`findDevicesByMacAddress`, then `!isEmpty()`), see section 3.

---

## 3. GATT connection, provisioning and proxy

### 3.1 The single BLE manager (`BluetoothMessagingServiceImpl`)

One `BleManager` subclass handles unprovisioned devices, proxies and OTA
(`de/jung/junghome/data/bluetooth/BluetoothMessagingServiceImpl.java:42`). GATT UUIDs (`de/jung/common/c.java:41-73`):

| Constant | UUID | Meaning |
|---|---|---|
| `c.f32657a` | `00002ADD-…` | Mesh Proxy Data In (write) |
| `c.f32658b` | `00002ADE-…` | Mesh Proxy Data Out (notify) |
| `c.f32659c` | `00002ADB-…` | Mesh Provisioning Data In (write) |
| `c.f32660d` | `00002ADC-…` | Mesh Provisioning Data Out (notify) |
| `c.f32661e` | `1d14d6ee-fd63-4fa1-bfa4-8f47b42119f0` | Silicon Labs OTA service |
| `c.f` | `984227f3-34fc-4045-a5d0-2c581f81a153` | Silabs OTA Data |
| `c.f32662g` | `f7bf3564-fb6d-4e53-88a4-5e37e0326063` | Silabs OTA Control |
| `c.f32663h` | `0000180A-…` | Device Information Service |
| `c.f32664i` | `00002A28-…` | Software Revision String |
| `c.f32665j` | `5d40aa0e-0a15-412d-a961-a3763c30c5eb` | JUNG: STM32 co-processor version (room thermostat) |
| `c.f32666k` | `946A8BF1-5F9B-4B23-A220-361CA026BDB1` | JUNG: storage-schema versions |

`isRequiredServiceSupported()` (`BluetoothMessagingServiceImpl.java:452-485`) looks up DIS and OTA characteristics,
then accepts the peer if it has **either** the Mesh Proxy service (→ `isProxy = true`, characteristics 0x2ADD/0x2ADE)
**or** the Mesh Provisioning service (→ 0x2ADB/0x2ADC). `initialize()` requests **MTU 517** and enables notifications
on the Data-Out characteristic of whichever service was found (`:434-449`). Cache is cleared on every disconnect
(`shouldClearCacheWhenDisconnected() = true`, `:578`).

- Connect: `connect(device).retry(3, 200).await()` (3 retries, 200 ms apart), after disconnecting any current peer
  (`BluetoothMessagingServiceImpl$connectTo$2.java:56-75`; 200 = `DisplayText.DISPLAY_TEXT_MAXIMUM_SIZE`).
- Outgoing PDUs: `B(byte[])` writes to Provisioning-Data-In if present else Proxy-Data-In, with the characteristic's
  own write type (default `WRITE_TYPE_DEFAULT` = 2) and `.split()` so the BLE library chunks to MTU (`:367-381`).
  Each sent chunk is echoed as `MessageType.DATA_SENT`, each notification as `DATA_RECEIVED` (`:372-380`,
  `de/jung/junghome/data/bluetooth/d.java:79-85`).
- `t0()` = `getMtu() - 3` is the MTU handed to Nordic (`:583-585`).
- Connection state machine `X7.a` (`DeviceState.kt`): `Connecting`, `Connected(macAddress)` (set in `onDeviceReady`,
  `:539-542`), `Disconnecting`, `Disconnected(expected = status == 0)`, `FailedToConnect(reason)`,
  `CommunicationError(status)`.

### 3.2 Bridging to Nordic (`MeshNetworkRepositoryImpl`)

`MeshNetworkRepositoryImpl` implements `MeshManagerCallbacks` (`de/jung/junghome/data/repositories/MeshNetworkRepositoryImpl.java:53`, `:470-471`):

- `DATA_SENT` → `meshManagerApi.handleWriteCallbacks(mtu, pdu)`, `DATA_RECEIVED` → `handleNotifications(mtu, pdu)`
  (`:125-136`). Nordic reassembles proxy PDUs by the 2 SAR bits (`GATT_SAR_MASK = 0xC0`, complete 0 / start 1 /
  continuation 2 / end 3, `MeshManagerApi.java:56-61`) with a 20 s reassembly timeout (`PROXY_SAR_TRANSFER_TIME_OUT`,
  `:69`, `:776`).
- `onMeshPduCreated(pdu)` and `sendProvisioningPdu(node, pdu)` both go to `B()` (`:1252-1259`, `:1345-1349`).
- `getMtu()` → `t0()` (`:880-882`).

### 3.3 Provisioning flow (No-OOB, FIPS P-256)

Entry points: `NodeProvisioningServiceImpl` (`de/jung/junghome/data/mesh/NodeProvisioningServiceImpl.java`), driven by
the `IdentifyDevice` and `DeviceProvisioning` interactors.

**Identify** (`NodeProvisioningServiceImpl$identify$1.java`):
1. `BluetoothAdapter.getRemoteDevice(macAddress)` and `connectTo()` (`:166-171`).
2. Before any mesh PDU, the app **reads GATT characteristics of the unprovisioned device**: DIS Software Revision
   String 0x2A28 → `softwareVersion` (`BluetoothMessagingServiceImpl$getSoftwareVersion$2.java:72-78`, parsed as a
   plain string, `d.java:49-55`), and for `productId == 0x000A` (RTR) the JUNG STM32-version characteristic
   `5d40aa0e-…` (`identify$1.java:174-180`, `BluetoothMessagingServiceImpl.java:588-590`; value is 4 bytes printed
   reversed and dot-joined, `d.java:34-48`). Each read has a 3 s timeout and falls back to `"-"`
   (`NodeProvisioningServiceImpl.java:152-154`).
3. When the connection reports `Connected`, `meshManagerApi.identifyNode(deviceUuid)` is called
   (`NodeProvisioningServiceImpl$startIdentifyAfterConnectionIsEstablished$1.java:86`). This is the 1-arg overload with
   **Attention Timer = 5 s** (`no/nordicsemi/android/mesh/MeshManagerApi.java:933-935`), which builds the
   `UnprovisionedMeshNode` with the primary NetKey, provisioning flags, current IV index and global TTL and sends
   *Provisioning Invite* (`:1103-1108`, `MeshProvisioningHandler.java:325-329`).
4. `IdentifyDevice` waits at most **5 s** for `PROVISIONING_CAPABILITIES` and retries the whole identify **3 times**
   (`de/jung/junghome/domain/interactors/devices/provisioning/IdentifyDevice$work$2.java:144`,
   `IdentifyDevice.java:121` (`retryWhen`, 3)). On capabilities the node is cached and progress `IdentifiedDevice`
   emitted (`NodeProvisioningServiceImpl.java:218-222`).

**Provision** (`NodeProvisioningServiceImpl$startProvisioning$1.java:66-92`):
1. Unicast address: `meshNetwork.nextAvailableUnicastAddress(meshNetwork.getNodes().size(), selectedProvisioner)` —
   note the app passes the *node count* as the `elementCount` argument (Nordic's
   `MeshNetwork.java:591-633` returns the first run of `elementCount` consecutive addresses inside the provisioner's
   range that are neither used by any element nor listed in `networkExclusions[ivIndex]`/`[ivIndex-1]`), so small gaps
   left by deleted nodes are skipped and new nodes land above the highest address in use; on
   `IllegalArgumentException` it retries with `address + 1` (`:78-84`).
2. `meshManagerApi.startProvisioning(node)` = **`startProvisioningNoOOB`**
   (`MeshManagerApi.java:1075-1079`, `MeshProvisioningHandler.java:398-400`). No static/output/input OOB is ever used
   (the only calls in JUNG code: `NodeProvisioningServiceImpl$startProvisioning$1.java:85` and
   `…$startIdentifyAfterConnectionIsEstablished$1.java:86`). The CAMERA permission is only used for project sharing
   (section 4), not for provisioning.
3. Provisioning Start PDU = `03 02 <algorithm> <pubKeyType> <authMethod=0> 00 00`
   (`no/nordicsemi/android/mesh/provisionerstates/ProvisioningStartState.java:59-72`). The only algorithm the library
   knows is `FIPS_P_256_ELLIPTIC_CURVE` (value 0 = BTM_ECDH_P256_CMAC_AES128_AES_CCM,
   `no/nordicsemi/android/mesh/utils/AlgorithmType.java:11`); OOB public key is used only if the device advertised one.
   This is why every node in the exported JSON has `"security": "insecure"` (section 4).
4. On `PROVISIONING_COMPLETE` the node is named after the scan-list name (or "unknown") and the `(uuid, MAC)` pair is
   published (`NodeProvisioningServiceImpl.java:176-199`); on failure `FailedProvisioning`.

**Post-provisioning configuration** (`DeviceProvisioning$work$1.java`, then `ConfigureDevice$work$1.java`):
after `ProvisioningFinished` the app disconnects, then reconnects **to the freshly provisioned node itself as proxy**
(`ConnectToDevice.Params.Specific(mac)`: scan for that MAC with 0x1828 service data matching the network, 15 s scan
timeout, 180 s connect timeout — `de/jung/junghome/domain/interactors/update/ConnectToDevice.java:162-164`,
`ConnectToDevice$getDevice$2$scanResult$1.java`) and runs the configuration steps in this order
(`de/jung/junghome/domain/interactors/configuration/ConfigureDevice$work$1.java:600-760`):

| Step | Messages (builder → Nordic message) |
|---|---|
| `SetWhitelistFilter` | `ConfigAppKeyAdd(netKey, appKey)` (`ConfigureDevice$work$1.java:607`, `p234v7/C2353d.java:38`), then *Proxy Set Filter Type* **white list** + *Add Addresses* = existing list ∪ {provisioner unicast} ∪ {0xFFFF} (`ConfigureDevice.java:392-416`; builders `p234v7/C2380q0.java:41` → `ProxyConfigSetFilterType`, `p234v7/C2355e.java:53` → `ProxyConfigAddAddressToFilter`) |
| `RequestCompositionData` | `ConfigCompositionDataGet()` (`p234v7/C2373n.java:29`), then `JungMeshApiImpl.c1()` = `bindMeshModels` (`JungMeshApiImpl$bindMeshModels$1.java:12`): `ConfigModelAppBind` for every model (`de/jung/junghome/data/mesh/c.java:197`) |
| `SetConfiguration` | `ConfigGattProxySet(1)` (`ConfigureDevice$work$1.java:645`, `p234v7/C2381r0.java:41`); `ConfigDefaultTtlSet(5)` (`:653`, `p234v7/K0.java:37`); relay `ConfigRelaySet(relay=1, count=3, steps=90/10=9)` (`:661` `new u(3, true, 90)`, `p234v7/C2387u0.java:55`); `ConfigNetworkTransmitSet(count=3, steps=100/10=10)` (`:669` `new z(3, 100)`, `p234v7/Q0.java:44`); on-air encodings `(steps<<3) OR count` (`no/nordicsemi/android/mesh/transport/ConfigNetworkTransmitSet.java:24`, `ConfigRelaySet.java:29`) |
| `SetBlacklistFilter` | *Set Filter Type* **black list** with the previous list minus the provisioner's own address (`ConfigureDevice.java:261-278`), then `ConfigBeaconSet(enabled = !isBatteryDevice)` (`:281-289`) |
| `RequestRequiredData`, `SetTime`, `CreateElementGroups` (30 s budget) | vendor property reads / Time Set / subscriptions — see `vendor-models.md`, `network-logic.md` |
| `FinishConfiguration`, `DisableProxy` | battery devices (`WallTransmitter*`, `MiniActuatorWallTransmitter`, `Y7/AbstractC0916e.java:335-337`) get `ConfigGattProxySet(0)` (`ConfigureDevice$disableProxyIfRequired$2.java`) |
| `ReconnectToDevice`, `Finished` | reconnect to a normal proxy |

These are exactly the values seen in the iOS export (default TTL 5, network transmit 3/100 ms, relay 3/90 ms,
secure network beacon on).

### 3.4 Proxy selection and connection (`ConnectToProxy`)

`de/jung/junghome/domain/interactors/networking/ConnectToProxy.java` combines the BLE state, the "BLE process running"
flag, scan errors, Bluetooth on/off and the device list (`:109-111`) and, whenever the state is
`Disconnected`/`CommunicationError`/`FailedToConnect`, Bluetooth is on and the network has devices
(`ConnectToProxy$work$1.java`), starts `startScanning(MESH_PROXY_UUID)`. For every scan result
(`ConnectToProxy$connectToProxy$2.java`):

1. If already `Connected` → ignore.
2. Service data of 0x1828 must match the network (`MeshNetworkRepositoryImpl.d0()`, section 2.5).
3. The advertiser's **MAC must belong to a known device** (`findDevicesByMacAddress(address)` non-empty,
   `ConnectToProxy$connectToProxy$2.java:276-280`) — the app never connects to a proxy whose MAC it has not recorded
   at provisioning time.
4. `connectTo(device)` and wait for `Connected` with a **5 s** timeout (`ConnectToProxy$connectToProxy$2.java:272-283`).
5. `SetProxySettings(address)`: look up the node by MAC and send **`ProxyConfigSetFilterType(BLACK_LIST_FILTER)`** to
   destination `0x0000` (`JungMeshApiImpl.u0()`, `de/jung/junghome/data/mesh/api/JungMeshApiImpl.java:350-382`:
   `c.a(0, new ProxyConfigSetFilterType(new ProxyFilterType(1)), statusOpCode = 3 /* Filter Status */, 0)` with the
   proxy's unicast address as the expected status source). It then waits up to **500 ms** for the device model to report
   `FilterType.BLACK_LIST_FILTER`, else `WrongProxySettings` (`SetProxySettings$work$2.java:160-165`, `:91`, `:52`).
   No addresses are added to the filter: an empty black list means the proxy forwards **everything** to the phone.
   (The white/black list with explicit addresses is only written during initial device configuration, 3.3.)

There is no "preferred proxy": the first matching advertiser wins (scan results arrive RSSI-unsorted). On any error the
flow logs "Retry to connect to proxy", cancels the scan, waits 5 s and starts again indefinitely
(`ConnectToProxy$work$2.java:47`, `ConnectToProxy$work$1.java:145-148`, `ConnectToProxy.java:111` = `retry`).
`ProxyConnectionServiceImpl.C()` simply reports `state is Connected` (`de/jung/junghome/data/bluetooth/ProxyConnectionServiceImpl.java:26-104`).
After an OTA update the manager waits up to 7 s for the peer to come back before the proxy loop takes over
(`BluetoothMessagingServiceImpl$reconnectToNetwork$1.java:147`).

### 3.5 Message queue, acknowledgements, retries (`MeshMessengerImpl` = `data/mesh/c.java`)

- Requests are `MeshRequest.Acknowledged(destination, meshMessage, statusOpCode, statusAddressSrc)` or
  `Unacknowledged(destination, meshMessage)` (`de/jung/junghome/data/mesh/api/c.java:13-119`) wrapped in
  `Request(meshRequest, alternativeSrcAddress, CompletableDeferred, id)` (`c.java:66-93`).
- A `MutableSharedFlow(replay 0, extraBufferCapacity 300)` is collected with `onEach { processRequest }` →
  **strictly one request in flight at a time** (`c.java:158-160`).
- `processRequest` (`c.java:353-…`): looks up the target device by address (`:620-648`, missing devices only logged),
  consults the proxy connection state (`K.C()`, `:662`) together with `isBatteryDevice` (`Q1()`) and fails fast with
  `DeviceNotReachable` when the node cannot be reached (`:429`, `:503-506` — the exact branch polarity is garbled by
  jadx's coroutine decompilation); if the model has no AppKey bound yet it first sends `ConfigModelAppBind`
  (`:445-481`). Then `sendWithTimeoutRetry(3, 3000 ms)` (`:493`, `de/jung/common/FlowOperatorsKt.java:62-149`):
  each attempt calls `meshManagerApi.createMeshPdu(dst, message)` and — for acknowledged requests — waits for a
  status whose `src == statusAddressSrc (or alternativeSrcAddress)` and `opCode == statusOpCode`
  (`MeshMessengerImpl$processRequest$2.java:94`, `MeshNotificationChannel$waitForStatusMessage$1.java:172` filter
  `aVar.f49702a == src && (opcode == expected || (vendorFlag && opcode == 0xD12705))`; `0xD12705` is JUNG's generic
  vendor status, opcode byte `0xD1`, company `0x0527`). If the request's opcode equals the status opcode the wait is
  skipped ("Can't wait for the status notification…", `$processRequest$2.java:90`). **3 attempts × 3 s**, then `MaxRequestTimeoutException`, which
  marks the device unreachable (`MeshMessengerImpl$handleError$1.java`); other exceptions bump a failure counter.
- Nordic's own segmented-transport retransmissions and block-ack handling run underneath; the app only logs them
  (`MeshNotificationChannel.java:1450-1492`).

### 3.6 TTL, sequence numbers, IV index

- **TTL**: every outgoing access message uses the *provisioner node's* TTL (`MeshTransport.java:38`, `:103`, `:163`),
  which is `Provisioner.globalTtl = 5` (`no/nordicsemi/android/mesh/Provisioner.java:66`, copied to the node at
  `ProvisionedMeshNode.java:492`). Nodes are configured with default TTL 5 too. Model publications are set with
  TTL 255 (= use node default) (`p234v7/C2383s0.java:58`).
- **Source address** = `selectedProvisioner.getProvisionerAddress()` (`MeshManagerApi.java:821-833`). A new provisioner
  gets a **random** unicast address inside its allocated range, avoiding exclusions and existing nodes
  (`MeshNetworkRepositoryImpl.g3()`, `MeshNetworkRepositoryImpl.java:741-791`), and a group range carved out of the
  free space (`:640-642`).
- **Sequence numbers** live in the Nordic Room DB column `nodes.seq_number` of the provisioner's own node
  (`no/nordicsemi/android/mesh/data/ProvisionedMeshNodeDao_Impl.java:26`), incremented per PDU
  (`ProvisionedMeshNode.java:170-173`), and are **not part of the JSON export** (`NodeDeserializer.serialize`,
  `no/nordicsemi/android/mesh/transport/NodeDeserializer.java:283-341` has no `sequenceNumber`). On import the app
  restores `max(stored, imported)` for the selected provisioner (`MeshNetworkRepositoryImpl.java:362-373`, `:1220-1248`).
- **IV index** is read once from the network (`MeshNetworkRepositoryImpl.java:468`) and used only to pick the
  `networkExclusions` bucket (`:747`). The observed networks are at IV index 0; the library processes Secure Network
  Beacons normally (section 1).

---

## 4. Key material and security

### 4.1 Network creation, keys, provisioners

- The app never calls `createMeshNetwork()` itself; `MeshNetworkRepositoryImpl` calls `loadMeshNetwork()` in its
  constructor (`MeshNetworkRepositoryImpl.java:471`) and Nordic generates a network when the DB is empty
  (`MeshManagerApi.java:318-322`, `:1009-1011`, `generateMeshNetwork()` `:569-581`). `resetMeshNetwork()` (used by the
  app's "reset network", `:1183`) does the same (`:1040-1047`).
- Generated material: mesh UUID = random v4 (`:570`); **1 NetKey** (index 0, `SecureUtils.generateRandomNumber()` =
  16 bytes from `SecureRandom`, `:583-589`, `utils/SecureUtils.java:282-286`), **3 AppKeys** (indices 0-2, `:558-566`);
  names stay Nordic's "Network Key 1"/"Application Key n" and `meshName` stays "nRF Mesh Network" (`BaseMeshNetwork.java:59`;
  no `setMeshName` call in `de/jung/junghome`). No hard-coded keys exist anywhere in the app.
- **Only AppKey index 0 is used** for all application traffic (`P7/a.java:29-33` = `appKeys.firstOrNull()`, used by
  `JungMeshApiImpl.java:252`, `:310`; `ConfigAppKeyAdd` binds that same key, 3.3). Keys 1 and 2 exist in the JSON but
  are never distributed to nodes.
- Provisioners are created by the app (`PrepareMeshNetwork.h()` → `resetProvisioners` removes all, then
  `MeshNetworkRepositoryImpl.y0/h3()` creates one, `PrepareMeshNetwork.java:203-215`, `:865-875`,
  `MeshNetworkRepositoryImpl.java:1001-1050`): unicast range size = `min((0x7FFF-1)/10, free)` and likewise for scenes
  (`:555-570`, `:1003-1005`), group range from `0xC000` up to `0xFEF4` (JUNG reserves the top 11 group addresses `0xFEF5-0xFEFF`,
  `V7/C0851h.java:16-20` with `MeshAddress.END_GROUP_ADDRESS = 0xFEFF`), provisioner **name = Android `android_id`** (`Settings.Secure.getString(…, "android_id")`, `p122l1/G.java:384`;
  Koin qualifier `"deviceId"`, `T7/h.java:2549`), UUID random (`BaseMeshNetwork.java:1045-1057`), address random within the range
  (3.6). Adding a provisioner also inserts it as a node holding *all* net/app keys (`BaseMeshNetwork.java:412-414`) —
  that is why phones appear in `nodes[]` of the export. `globalTtl` is never changed from 5.
- A second phone never generates keys: the owner pre-creates the joiner's provisioner (max **10** provisioners,
  `de/jung/junghome/domain/interactors/cloud/AcceptParticipantRequest.java:335-343`), uploads the network and calls the
  cloud function `acceptAccessRequest{projectId, requestId, provisionerId}` (`:130-138` → `CloudApiImpl.java:235`,
  `data/cloud/api/dto/AcceptAccessRequestBodyDto.java`); the joiner imports the JSON and
  selects the provisioner whose UUID equals its cloud user's `provisionerId` (`PrepareMeshNetwork.java:453`,
  `:496-505`). Orphaned provisioners are only garbage-collected when the owner accepts the next participant
  (`AcceptParticipantRequest.java:231-283`).

### 4.2 Key refresh, node reset, exclusions

- **Key refresh is manual only**: the only trigger is a button in the diagnostic `MeshNetworkActivity`
  (`de/jung/junghome/app/ui/meshnetwork/ViewOnClickListenerC1872d.java:57` → `KeyRenewal.Params.CheckRenewalOrStartRenewal`).
  Phases (`de/jung/junghome/domain/item/mesh/RefreshPhase.java:23-27`): 0 → `distributeNetKey(primary, random)` and
  `ConfigNetKeyUpdate` to every non-excluded node (`KeyRenewal$startRenewal$2.java`, `p.java:171`,
  `p234v7/C2360g0.java:56`); 1 → `ConfigKeyRefreshPhaseSet(2)` (`p234v7/W0.java:65`); 2 → `ConfigKeyRefreshPhaseGet` on
  all nodes, abort to phase 0 if any node lags, else `switchToNewKey` (`KeyRenewal$startUsingNewKeys$2.java:68-104`);
  3 → `ConfigKeyRefreshPhaseSet(3)` + local `revokeOldKey` (`p.java:45`). **AppKeys are never refreshed**
  (`ConfigAppKeyUpdate` unused in `de/jung/junghome`). Beacon KR flags are ignored (1.4).
- **Deleting a device**: DAO delete → `meshNetwork.deleteNode()` (retried 3×, `MeshNodeRepository.java:333`) →
  Nordic marks the node `excluded` and appends all its element addresses to `networkExclusions[ivIndex]`
  (`BaseMeshNetwork.java:120-133`, `:446-457`) → `ConfigNodeReset` to the node, acknowledged by
  `CONFIG_NODE_RESET_STATUS` (`de/jung/junghome/data/mesh/api/a.java:50-57`, `ResetDevice$work$2.java:155-166`).
  No key refresh follows, so a removed device (or a removed user, below) keeps working keys until someone presses the
  refresh button. The 554 excluded addresses in the iOS export are the result of this policy.
- **Removing a user** (`RemoveUser$work$2.java:68-115`) deletes the Firestore member (and wipes local data if it is
  yourself) but does *not* remove the provisioner and does *not* refresh keys.

### 4.3 `security` and `minSecurity`

`"security": "secure"|"insecure"` on a node is Nordic's `ProvisionedBaseMeshNode.security` (1 only when the provisionee
public key was delivered OOB, `provisionerstates/ProvisioningPublicKeyState.java:77-82`; serialized at
`NodeDeserializer.java:292`); `minSecurity` on a NetKey is cleared when a node is added without OOB
(`MeshManagerApi.java:783-795`). JUNG never reads either (`grep -rn "isSecurelyProvisioned\|isMinSecurity\|markAsInsecure" de/jung/junghome` → 0),
so both are purely informational; because provisioning is always No-OOB (3.3) real exports say `insecure`.

### 4.4 Storage

- Mesh CDB: Nordic Room DB `mesh_network_database.db`, plain SQLite; `network_key.key/old_key`, `nodes.device_key`
  are unencrypted BLOBs (`MeshNetworkDb_Impl.java:129`, `:135`).
- App DB `jung-home-db` (`p202s7/b.java:260`), plain Room; entities listed in
  `de/jung/junghome/data/persistence/AppDatabase_Impl.java:880` (DeviceEntity with `macAddress`, section 2.4).
- The only encrypted store is `EncryptedSharedPreferences` (AndroidKeyStore master key `masterKeyAlias`, AES-256-GCM,
  `p122l1/G.java:322-356`, `:638-663`) holding the **project AES key** (`public_aes_key`) and the gateway's
  `gateway_ip_address`, `gateway_ip_token`, `gateway_fingerprint` (`p191r7/d.java:40-123`).

### 4.5 Project sharing (QR / cloud / file)

- Project secret: AES-256 key from `KeyGenerator.getInstance("AES")` (`p122l1/G.java:630-631`), created with the
  cloud project (`CloudApiImpl.java:68`, `ProjectRepositoryImpl$createProject$2.java:86-92`).
- **QR code** = Base64 of JSON `{"id": <cloud projectId>, "key": <Base64 32-byte AES key>}`
  (`de/jung/junghome/domain/interactors/cloud/CreateQrCode.java:91-102`, `domain/item/ProjectQrCode.java:10-13`),
  parsed by `ParseQrCodeAndCheckIfProjectExists$work$2.java:76`. This is what the CAMERA permission is for.
- **Cloud blob**: `ExportDto{version:"1.1", appVersion, platform, meta, network}` where `network` = **Base64 of the
  unmodified Nordic `exportMeshNetwork()` JSON including device keys** (`de/jung/junghome/domain/interactors/CreateProjectFile$work$2.java:79-103`,
  `MeshNetworkRepositoryImpl.java:973-998`, `ImportExportUtils.java:246`) and `meta` = JUNG extras (devices with MAC,
  groups, scenes, timers, …; `domain/dto/MetaData.java:14-23`). Serialized with Gson, then
  **AES/GCM/NoPadding, random 12-byte IV prepended, no AAD, 128-bit tag** under the project key
  (`p191r7/b.java:49-64`) and uploaded to Firebase Storage `projects/<projectId>/JungHome.json`
  (`CloudStorageImpl$uploadFileBytes$2.java:76-80`). Import reverses it and calls `importMeshNetworkJson`
  (`MeshNetworkRepositoryImpl.java:1105-1107`). Phones authenticate to Firebase anonymously
  (`AuthServiceImpl.java:71`); there is no phone-to-phone BLE/Nearby path.
- **Share via file / backup** writes the same `ExportDto` as **plaintext** `JungHome.json`
  (`FileServiceImpl$exportNetworkFile$2.java:52-66`) — this is the file a third-party client should ask the user for.
- Roles: `owner` / `member` (`domain/item/User.java:48-51`); only owners export to the cloud by default
  (`de/jung/junghome/domain/interactors/cloud/IsExportAllowed$work$1.java:70`).

---

## 5. Gateway and firmware update

### 5.1 Gateway on the mesh

The gateway (JUNG product 0x000B) is a **normal provisioned node**: it advertises the same JUNG manufacturer data and
0x1827 service data, is provisioned No-OOB like everything else, has the standard configuration capabilities (proxy,
relay, TTL, NetKey update, key-refresh phase) and takes part in key refresh (`Y7/g0.java`, "Gateway.kt", capability
fields `:21-64`). There is **no gateway-specific GATT service** — `grep -rn "UUID.fromString"` over
`data/gateway`, `domain/interactors/gateway`, `app/ui/gateway`, `app/ui/project/gateway` finds nothing; the only
UUIDs the app knows are in `de/jung/common/c.java` (3.1).

What is special is a set of JUNG **vendor properties served by the gateway node** and read over the mesh after it has
been provisioned (`GatewayNetworkCapability`, `p246w8/a.java:130-138`; requested with `CommunicationType.REQUEST` by
`ConfigureGateway$work$2.java:232-235` and `GatewayConnectionViewModel$requestData$1.java`):

| Property ID | Name | Stored as |
|---|---|---|
| `0xC001` (49153) | `GatewayAPIToken` | `gateway_ip_token` (`p056e8/r.java:192-204` = `CipherSuite.TLS_ECDH_ECDSA_WITH_NULL_SHA`, resolver `data/mesh/resolver/K.java:81-84`) |
| `0xC002` (49154) | `GatewayIP` | `gateway_ip_address` (`r.java:284-296`, resolver `M.java`) |
| `0xC003` (49155) | `GatewayFingerprint` | `gateway_fingerprint` (`r.java:238-250`, resolver `L.java`) |

They are ordinary LBC property Get/Status exchanges (vendor opcodes with company `0x0527`, see `vendor-models.md`),
so anyone holding the AppKey can read the gateway's LAN API token and certificate fingerprint off the mesh. The phone
then talks HTTPS to `https://<gateway_ip>/api/junghome/config` and `/healthstatus`
(`de/jung/junghome/data/mesh/api/b.java:23-48`), header `Token: <gateway_ip_token>` (`p212t7/a.java:37-58`), with a
custom `X509TrustManager` that accepts only the certificate whose SHA-256 equals the stored fingerprint
(`de/jung/junghome/data/gateway/a.java:26-47`). The gateway receives the **plaintext `ExportDto`** (Base64 Nordic JSON
with all keys + JUNG meta) over that channel (`GatewayRepositoryImpl.java:659-693`, `GatewayProjectDTO.java:11`),
which is how its own mesh stack obtains NetKey/AppKey/DevKeys and provisioner list; it reports
`GatewayConfigDTO{systemSerial, macAddress, projectFile, registered, connected, apiClients, …}`
(`data/model/GatewayConfigDTO.java:16-67`). Cloud registration (`GatewayLoginDTO{userName, userPassword, cloudRegister}`,
`GatewayRepositoryImpl.java:489-491`) and third-party API-client permissions (`PermissionsDTO{clientName}`) also go over
this HTTPS API, never over BLE. The gateway's IP can also be entered manually (`UpdateGatewayDevice.java:40-50`).

### 5.2 Firmware update is a direct Silicon Labs OTA-DFU session, not mesh DFU

Nothing in `de/jung/junghome/` references BLOB Transfer / Firmware Update / Firmware Distribution
(`grep -rn "BlobTransfer\|FirmwareUpdate\|FirmwareDistribution\|0x140[0-5]" de/jung/junghome/` → no hits; the Nordic
classes are name-only stubs, section 1.2). Instead the app opens a **point-to-point GATT connection to the node** and
streams a Gecko Bootloader image to the **Silicon Labs OTA service** that every JUNG node exposes next to the Mesh
Proxy service:

| Step | What happens (with sources) |
|---|---|
| 0 | Decide: `assets/updates/*_update.json` are loaded from the APK (`FileServiceImpl.c()`, `de/jung/junghome/data/sharing/FileServiceImpl.java:125-166`), keyed by `update_infos[0].products[].product_id` (= Composition Data PID); the candidate image's `version` is compared component-wise with the device's version (`de/jung/junghome/domain/interactors/update/CheckForDeviceUpdate$work$1.java:72-105`, `VersionComparison.java:45-85`). Device version = SIG property **0x001A Device Software Revision** read through JUNG's *LBC Generic Manufacturer Property* vendor model (`0x0527:0012`, Get opcode `C8 27 05` + `1A 00`, status `CB 27 05`; `G7/c.java:19-20`, `p056e8/r.java:100-118`), ASCII `"02020001"` → `2.2.0.1`; for the RTR's STM32 co-processor JUNG vendor property **0x0005** (4 raw bytes → `a.b.c.d`, `p056e8/r.java:2287-2305`, `de/jung/junghome/data/mesh/resolver/C1964w1.java:79-115`). |
| 1 | Reset check: read GATT `946A8BF1-…` (storage-schema versions, 6 or 8 bytes, LSB of each LE u16 → `[master, stack, application, (stm)]`, `de/jung/junghome/data/bluetooth/d.java:56-78`) and compare with the manifest `storage_schema_version` (`FileServiceImpl.java:300-455`); if the device is behind, it is re-provisioned after the update (`UpdateDevice.java:1449-1536`). |
| 2 | Connect to the node **by MAC**: scan without filter, take the first result whose MAC equals the node's and whose 0x1828 service data matches the network; 180 s budget; then `SetProxySettings` (black-list filter) so the same link keeps working as a mesh proxy (`ConnectToDevice.java:156-164`, `ConnectToDevice$getDevice$2$scanResult$1.java`, `ConnectToDevice$connectToDeviceAndWaitForConnection$2.java`). Battery devices (PIDs 0x0005/0x0006/0x0016) first get `ConfigGattProxySet(1)` over the mesh so they start advertising, then the battery level is checked (`ConnectToLowPowerDeviceForUpdate$work$1.java:321`). |
| 3 | `requestConnectionPriority(HIGH)`; write **`0x00`** to OTA Control `f7bf3564-…` (`BluetoothMessagingServiceImpl.java:566-573`). |
| 4 | Write the whole `.gbl` to OTA Data `984227f3-…` with **`WRITE_TYPE_NO_RESPONSE`**, split into `MTU-3` byte chunks back-to-back (`BluetoothMessagingServiceImpl$updateDevice$1.java:72-79`, splitter `de/jung/common/g.java:40-54`; progress assumes 244-byte payloads, `B4/c.java:504-511`). |
| 5 | Write **`0x03`** to OTA Control, then disconnect (`BluetoothMessagingServiceImpl.java:411-420`, `$setOtaControlAndDisconnectGatt$2.java:43-65`). Abort = same `0x03` write (`:401-409`). No other control bytes are used. |
| 6 | Wait **17 s** (EFR32 application image) or **120 s** (`stm` image) for the reboot (`de/jung/junghome/domain/interactors/update/UpdateDevice.java:465-473`), re-scan/re-connect by MAC (≤180 s), re-read the version over the mesh and compare with the manifest (`UpdateDevice$confirmUpdate$2.java:92-172`, mismatch → `WrongSoftwareVersion`), send a Time Set (`TimeCapability`, `UpdateDevice.java:1606`), disable the proxy feature again on battery devices. |

Image format: `lb-connect-*.gbl` are Silicon Labs **GBL** containers (header tag `EB 17 A6 03`, type flags
`0x00000101` = encrypted + signed, then a `FA 06 06 FA` encryption header with nonce); the file-name suffix
`application-secure-secure_bootloader-seupgrade-sign-encrypt-lzma` says each carries application + secure bootloader +
Secure-Element upgrade, LZMA-compressed, AES-encrypted and signed — so they cannot be inspected without JUNG's keys, and
the target SoC is a Silicon Labs EFR32 running the Silabs Bluetooth Mesh stack. The manifest schema and the product-ID
table are in `firmware-products.md`.

**Correction to `firmware-products.md`:** `STM32_Image_block_compressed_V4-4-5.gbl` is *not* a gateway image. Its
manifest lists `product_id: 10` (RTR room thermostat, hw rev 1), `image_type: "stm"`, `storage_schema_version {stm: 7}`,
`requires application 1.9.2.1`, i.e. it is the thermostat's STM32 co-processor firmware ("BIZ0" block-compressed
container, Cortex-M vector table at `0x0800xxxx`), delivered through the same OTA Data/Control characteristics; the
EFR32 forwards it to the STM32. The device-info screen shows the separate STM32 version
(`de/jung/junghome/app/ui/configuration/deviceInfo/g.java:248-250`). There is **no** update image for the gateway
(product 0x000B) and no gateway code path in `domain/interactors/update` or `app/ui/update`
(`grep -rni gateway` → no hits); `UpdateGatewayDevice.java:40-50` only stores the gateway's IP address.

---

## 6. Joining the mesh from your own code

What you need from the export (`MeshNetwork.json`, Mesh CDB 1.0.1 format — on Android it is the Base64 `network`
field of `JungHome.json`, 4.5): `netKeys[0].key` (index 0, phase 0; plus `oldKey` if `phase != 0`),
`appKeys[0].key` (the only AppKey ever bound to nodes: `P7/a.java:29-33`, `JungMeshApiImpl.java:252`, `:310`; keys
1 and 2 are unused), `ivIndex` (0 on all observed networks), the
nodes' `unicastAddress`/`deviceKey`/elements/models, the groups, and — for anything that must not collide with the
phones — the provisioners' `allocatedUnicastRange`s and `networkExclusions`.

Minimal sequence (mirrors what the app does):

1. **Pick a source address.** Either reuse the phone's provisioner address (then you *must* continue its sequence
   number, see pitfalls) or, cleaner, allocate your own unicast address outside every provisioner's
   `allocatedUnicastRange` and outside `networkExclusions[ivIndex]`. The app itself picks a random free address in its
   own range (`MeshNetworkRepositoryImpl.java:741-791`). Optionally add yourself as a provisioner in the JSON so the
   phones will not reuse the address.
2. **Scan** for advertisements listing service `0x1828`; take the 0x1828 *service data*: byte 0 = `0x00` → bytes 1..8
   are the Network ID `k3(NetKey)`; `0x01` → Node Identity (hash 8 B + random 8 B, hash = last 8 bytes of `AES(IdentityKey, 0^6‖random‖unicast)`,
   `utils/SecureUtils.java:130-142`, checked with the current and old IdentityKey, `MeshManagerApi.java:1023-1037`).
   Compare the Network ID against `k3` of the NetKey (and of the old NetKey during key refresh) (`MeshManagerApi.java:1121-1129`).
   Any JUNG mains-powered node is a proxy (GATT Proxy is enabled during configuration, section 3.3); on battery wall
   transmitters the app disables the proxy feature at the end of configuration.
3. **Connect**, request a large MTU (app: 517), discover `0x1828` → write characteristic `0x2ADD`, notify
   characteristic `0x2ADE`, enable notifications (`BluetoothMessagingServiceImpl.java:434-449`).
4. **Proxy PDU framing**: byte 0 = `SAR<<6 | type` (`type`: 0 network PDU, 1 mesh beacon, 2 proxy configuration,
   3 provisioning; SAR 0 complete / 1 first / 2 continuation / 3 last, `MeshManagerApi.java:56-68`). Split at `MTU-3`.
   The first thing the proxy sends you is a Secure Network Beacon (type 1, 22 bytes: flags, NetworkID, IV index, auth).
   Verify it with `k3`/beacon key and take the IV index from it (`SecureNetworkBeacon.java:29-47`).
5. **Proxy filter** (optional but what the app does): send a *Set Filter Type* = black list (`0x00 0x01`) as a proxy
   configuration message (type 2, TTL 0, `dst = 0x0000`, encrypted with the NetKey's proxy-nonce keys), expect
   *Filter Status* opcode `0x03` from the proxy's unicast address (`JungMeshApiImpl.java:368-371`,
   `opcodes/ProxyConfigMessageOpCodes.java:5-8`). With an empty black list the proxy forwards every network PDU it
   hears, so you receive group publications and status messages from all nodes without whitelisting.
6. **Send an access message**, e.g. Generic OnOff Set (opcode `0x8202`, params `onoff tid [trans delay]`) or
   Set Unacknowledged (`0x8203`) to a group (`0xC000+`) or unicast. Upper transport: AppKey (`AKF=1`, `AID=k4(AppKey)`),
   nonce type 0x01, `SEQ`, `SRC`, `DST`, IV index, MIC 4 B; lower transport unsegmented if ≤ 15 B else 12-byte
   segments (`UpperTransportLayer.java:19-22`); network layer: NID/EncryptionKey/PrivacyKey = `k2(NetKey, 0x00)`,
   **TTL 5**, CTL 0, obfuscate, network MIC 4 B (`Provisioner.java:66`). Increment `SEQ` for every network PDU
   (including each segment and each proxy-config message).
   JUNG's vendor messages use company ID `0x0527`; the acknowledged app-level traffic is documented in
   `vendor-models.md`.
7. **Wait for status** the way the app does: 3 s per attempt, 3 attempts, matching on `src == dst you addressed` and
   the expected status opcode (`de/jung/common/FlowOperatorsKt.java:62-149`, `MeshNotificationChannel$waitForStatusMessage$1.java`).
   Segmented replies are acknowledged by the receiver with a Segment Acknowledgement (control opcode 0x00) within
   `150 + 50·TTL` ms; if you do not send it, the node will retransmit and eventually give up.

Pitfalls (all observed in the sources):

- **Sequence numbers.** Replay protection is per source address: every node keeps the last SeqAuth seen from each
  `SRC` and drops anything lower (`LowerTransportLayer.java:172-175` is the phone's side of the same rule). If you reuse
  a phone's provisioner address you must start above the phone's current counter, which is *not* in the JSON export —
  Android keeps it in Room (`nodes.seq_number`, `ProvisionedMeshNodeDao_Impl.java:26`), iOS in the
  `<meshUUID>.plist` (`S0001` key, see `docs/ios-app-data.md`) — and the phone will then be locked out until it
  catches up. Using your own address avoids the problem entirely; only the IV index must match.
- **IV index.** Nordic resets its local sequence number to 0 when the transmit IV index increases
  (`MeshManagerApi.java:721-724`). Observed networks are at IV index 0 with no update in progress; if a beacon ever
  announces IV update, follow the spec (use `ivIndex-1` for TX while `IV Update active`, `IvIndex.java:70-73`).
- **Exclusions.** `networkExclusions` addresses are dead until IV index +2 (`MeshManagerApi.java:729-735`); never pick
  one of them as your source.
- **Proxy filter defaults.** Per spec every new proxy connection starts with an *empty white list* (the proxy then
  adds the source address of PDUs you send it), so by default you only receive PDUs addressed to your own unicast
  address. That is why the app's very first action after connecting is *Set Filter Type = black list* (3.4), and why it
  briefly uses a white list of {provisioner, 0xFFFF} only while a node is being configured (3.3). Do the same or add
  the group/all-nodes addresses you care about explicitly.
- **MTU.** The app relies on MTU 517 (`requestMtu(517)`) so that composition data and vendor property replies fit in
  few proxy segments; with the default 23-byte MTU the 20 s proxy-SAR timeout (`MeshManagerApi.java:69`) and the
  10 s incomplete timer (`LowerTransportLayer.java:15`) are easy to hit.
- **Key refresh.** If the network is in key-refresh phase 1/2 (`netKeys[].phase`, `oldKey` present), proxies may still
  advertise the old Network ID; the app accepts both (`MeshManagerApi.java:1126`). Follow the spec: phase 1 transmit
  with the old key and accept both, phase 2 transmit with the new key and accept both, phase 3/0 new key only. JUNG
  only ever refreshes on manual request (4.2), so `phase` is normally 0.
- **Proxy choice.** The app only ever connects to a MAC it provisioned itself (`ConnectToProxy$connectToProxy$2.java`),
  but any node with the proxy feature works; the gateway (product 0x000B) is a normal proxy node too (section 5).
