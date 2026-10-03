# JUNG HOME – vendor mesh models and messages (Android app, wire level)

Source: jadx decompilation of the JUNG HOME Android app. All paths below are relative to
`android/jadx-out/sources/`. Line numbers refer to the decompiled `.java` files.
Nordic's nRF Mesh library (`no/nordicsemi/android/mesh/**`) is the transport; JUNG's own
code lives in `de/jung/junghome/**` plus ProGuard-shortened packages (`G7`, `H7`, `P7`,
`A7`, `B7`, `C7`, `p234v7` = `v7`, `p056e8` = `e8`, `S8`, `V8`, ...).

Company ID: **0x0527 (1319) "Albrecht JUNG"** (`no/nordicsemi/android/mesh/utils/CompanyIdentifiers.java:1347`).

---

## 1. Vendor model IDs

The Nordic library packs a vendor model ID as `(CID << 16) | ModelID`
(`no/nordicsemi/android/mesh/transport/ConfigCompositionDataStatus.java:119-121`,
`no/nordicsemi/android/mesh/models/VendorModel.java:56-62`). JUNG compares them with plain `==`
(`de/jung/junghome/domain/item/mesh/nodes/MeshModelId.java:39-41`). The decimal constants in the
code are therefore `0x0527xxxx`:

| Model (CID:ID) | Decimal in code | JUNG class / name string | Role | SIG model it mirrors | What it does |
|---|---|---|---|---|---|
| 0x0527:**0x1011** | 86446097 | `LBCGenericAdminPropertyServer` ("LBC Generic Admin Property Server") – `de/jung/junghome/domain/item/mesh/nodes/server/LBCGenericAdminPropertyServer.java:18`, name at `E7/f.java:117` | Server | Generic Admin Property Server 0x1011 | Device configuration parameters ("admin" properties: on/off delays, blind times, key modes, RTR setpoints, LED modes, astro timers, ...). Almost every `*MessageBuilder` targets this model. |
| 0x0527:**0x1012** | 86446098 | `LBCGenericManufacturerPropertyServer` ("LBCGenericManufacturerPropertyServer") – `.../LBCGenericManufacturerPropertyServer.java:18`, `E7/f.java:271` | Server | Generic Manufacturer Property Server 0x1012 | Firmware/bootloader/secure-element versions, gateway IP/token/fingerprint, PIR presence brightness/PIR control. |
| 0x0527:**0x1013** | 86446099 | `LBCGenericUserPropertyServer` ("LBC Generic User Property Server") – `.../LBCGenericUserPropertyServer.java:18`, `E7/f.java:113` | Server | Generic User Property Server 0x1013 | Runtime "user" properties: actuator InsertId, energy charts; also the *target* model that a button in key-mode *Property*/*Gateway* publishes to (`p056e8/j.java:25,37`). |
| 0x0527:**0x1015** | 86446101 | `g8.a.e` with name `"LBCGenericPropertyClient"` – `E7/f.java:240-245`, `p076g8/a.java:43,436` | **Client** | Generic Property Client 0x1015 | Client model on button/sensor elements. The app only configures its publication address; it never sends to it. Listed as a "connection" client model for key modes Light(0), Move(1), Property(3), Switch(5), Gateway(6) (`de/jung/junghome/domain/interactors/connection/GetModelsForDeviceConnection.java:161-179`). |
| 0x0527:**0x1016** | 86446102 | `MeshModel.JHSchedulerServer` ("JH Scheduler") – `de/jung/junghome/domain/item/mesh/nodes/server/MeshModel.java:1816,1825`, `E7/f.java:333` | Server | none (loosely SIG Scheduler 0x1206/0x1207) | JUNG's own 16-slot scheduler with timed / sunrise / sunset schedules, per-slot action and "effective time" read-back. Messages in `H7/*`, builders in `B7/c,d,e,f`. |
| 0x0527:**0x1017** | 86446103 | `MeshModel.SceneActionSetupServer` ("SceneActionSetupServer") – `MeshModel.java:2704,2842`, `E7/f.java:339` | Server | none (companion of SIG Scene Setup 0x1204) | Stores per-scene *actions* (what this element does when a scene is recalled) and lists stored scenes. Messages `G7/g,h`, builders `A7/b,g`. |

The full list of model IDs the app knows (SIG + these vendor ones) is `P7/a.java:27` (`MeshUtils.kt`).
Nordic-side mapping of every model ID → JUNG class is `E7/f.java` (`NodeMapper.kt`, lines 49-350).

---

## 2. Opcode encoding convention

### 2.1 Sending (int in code → wire bytes)

Every JUNG vendor message extends `no.nordicsemi.android.mesh.transport.VendorModelMessageAcked`
(`VendorModelMessageAcked.java:14-21`: ctor `(appKey, modelId, companyId, opCode, parameters)`).
No JUNG code uses `VendorModelMessageUnacked`.

Transmission path: `VendorModelMessageAckedState.java:24-26` → `MeshTransport.createVendorMeshMessage`
→ `AccessLayer.createCustomAccessMessage` (`no/nordicsemi/android/mesh/transport/AccessLayer.java:33-49`)
→ `MeshParserUtils.createVendorOpCode(opCode, cid)` (`no/nordicsemi/android/mesh/utils/MeshParserUtils.java:86-88`):

```java
return new byte[]{ (byte)(opCode | 0xC0), (byte)(cid & 0xFF), (byte)((cid >> 8) & 0xFF) };
```

So the wire opcode is always **3 octets: `(0xC0 | op6) , CID_lo , CID_hi`** = `Cx 27 05` / `Dx 27 05`,
exactly the Bluetooth Mesh "11xxxxxx + CID little-endian" vendor format. Only the *low byte* of the Java
int survives the `(byte)` cast; the upper bytes are ignored.

JUNG encodes the int as `(0x0527 << 8) | low_byte`, i.e. it prefixes its own CID for readability:

* `337679 = 0x05270F` → `(byte)(0x05270F | 0xC0) = 0xCF` → wire **`CF 27 05`** (User Property Set, `JungMeshApiImpl.java:318`).
* `337874 = 0x0527D2` → low byte already has the two top bits set → wire **`D2 27 05`** (JH Scheduler Get, `H7/a.java:19`).

Warning: `MeshParserUtils.getOpCode(int)` (`MeshParserUtils.java:483-492`, used only for SIG messages)
would turn `0x05270F` into `C5 27 0F` – that is *not* what goes on the wire for vendor messages.

### 2.2 Receiving (wire bytes → int in code)

`AccessLayer.parseAccessLayerPDU` (`AccessLayer.java:60-70`): first byte ≥ 0xC0 → 3-byte opcode;
`opCode = pdu[0] & 0x3F` (6-bit value, `MeshParserUtils.getOpCode(byte[],int)` line 147-152),
`companyIdentifier = pdu[2]<<8 | pdu[1]`; parameters = the rest.
`DefaultNoOperationMessageState.java:335-350` wraps every 3-octet-opcode message with CID ≠ 0 into a
`VendorModelMessageStatus` (model id 0 when unsolicited) and hands it to `MeshStatusCallbacks.onMeshMessageReceived`.

JUNG then re-encodes it with `P7.a.e(VendorModelMessageStatus)` (`P7/a.java:56-60`):

```java
byte[] b = MeshParserUtils.createVendorOpCode(status.getOpCode(), status.getCompanyIdentifier()); // {0xC0|op, 0x27, 0x05}
return b[2] | (b[1] << 8) | (b[0] << 16);   // e.g. 0xD12705 = 13707013
```

So **received** opcodes are compared as `0x??2705` big-endian ints (`12920581 = 0xC52705`, etc.), while
**sent** opcodes are `0x0527??`. Both describe the same three wire bytes.

### 2.3 Complete opcode table (CID 0x0527)

| 6-bit op | Wire bytes | Int used when sending | Int used when receiving | Message | Class (send) / parser (recv) |
|---|---|---|---|---|---|
| 0x02 | `C2 27 05` | 337666 (0x052702) | – | LBC Generic **Admin** Property **Get** | `G7/a.java:25` (`LBCGenericAdminPropertyGet.kt`) |
| 0x03 | `C3 27 05` | 337667 (0x052703) | – | LBC Generic Admin Property **Set** (acked) | `G7/b.java:39` (`LBCGenericAdminPropertySet.kt`) |
| 0x05 | `C5 27 05` | – | 12920581 (0xC52705) | LBC Generic Admin Property **Status** | `de/jung/junghome/data/mesh/resolver/AbstractC1972z0.java:43` |
| 0x08 | `C8 27 05` | 337672 (0x052708) | – | LBC Generic **Manufacturer** Property **Get** | `G7/c.java:20` |
| 0x09 | `C9 27 05` | 337673 (0x052709) | – | LBC Generic Manufacturer Property **Set** | `G7/d.java:25` |
| 0x0B | `CB 27 05` | – | 13313797 (0xCB2705) | LBC Generic Manufacturer Property **Status** | `AbstractC1972z0.java:43` |
| 0x0E | `CE 27 05` | 337678 (0x05270E) | – | LBC Generic **User** Property **Get** | `G7/e.java:20` |
| 0x0F | `CF 27 05` | 337679 (0x05270F) | – | LBC Generic User Property **Set** | `G7/f.java` (`LBCGenericUserPropertySet.kt`; body lost by jadx, see call site `de/jung/junghome/data/mesh/api/JungMeshApiImpl.java:318`) |
| 0x11 | `D1 27 05` | – | 13707013 (0xD12705) | LBC Generic User Property **Status** | `AbstractC1972z0.java:43`; also accepted as generic reply in `MeshNotificationChannel$waitForStatusMessage$1.java:172` |
| 0x12 | `D2 27 05` | 337874 (0x0527D2) | – | **JH Scheduler Get** | `H7/a.java:19` (`JHSchedulerGet.kt`) |
| 0x13 | `D3 27 05` | – | 13838085 (0xD32705) | JH Scheduler **Status** | `resolver/AbstractC1934m0.java:26` |
| 0x14 | `D4 27 05` | 337876 (0x0527D4) | – | JH Scheduler **Set** | `H7/b.java:19` (`JHSchedulerSet.kt`) |
| 0x16 | `D6 27 05` | 337878 (0x0527D6) | – | **Scene Action Setup Get** | `G7/g.java:19` (`SceneActionSetupGet.kt`) |
| 0x17 | `D7 27 05` | – | 14100229 (0xD72705) | Scene Action Setup **Status** | `resolver/AbstractC1970y1.java:26` |
| 0x18 | `D8 27 05` | 337880 (0x0527D8) | – | Scene Action Setup **Set** | `G7/h.java:19` (`SceneActionSetupSet.kt`) |

Direction: all *Get/Set* are app → node (unicast to the element address, `capability.O()`,
`JungMeshApiImpl.java:74-87`); all *Status* are node → app (reply or unsolicited publication).

Not observed in the app but strongly suggested by the numbering (each property family occupies a block of
six opcodes, mirroring SIG's `Properties Get / Properties Status / Property Get / Property Set /
Property Set Unacknowledged / Property Status`): 0x00/0x01 Admin Properties Get/Status, 0x04 Admin Set
Unack; 0x06/0x07, 0x0A for Manufacturer; 0x0C/0x0D, 0x10 for User; 0x15 (JH Scheduler Set Unack?),
0x19 (Scene Action Setup Set Unack?). **Verified on air** for the three *Properties Get* opcodes
(`C0`, `C6`, `CC 27 05`, no parameters): every node answered with the matching *Properties Status* (`C1`, `C7`,
`CD`) carrying the property ids as `u16 LE`, and the gateway firmware's opcode table lists the same numbering
(`docs/cross-repo-analysis.md` §1.3). The lists themselves are in `docs/hidden-features.md` §2
(`tools/mesh_poc.py prop lists`, `jhmesh.messages.vendor_properties_get`). **Admin Property Set Unack (`C4`) verified
too** (applied silently, confirmed by read-back); the Manufacturer / User Set Unack and the 0x15 / 0x19
opcodes remain unverified.

---

## 3. LBC Generic Property family (0x1011 / 0x1012 / 0x1013) – message layouts

All multi-byte integers are **little-endian** unless stated.

### 3.1 Property ID

`e8.r.a()` (`p056e8/r.java:2446-2453`) serialises the property ID as a 2-byte little-endian short:
`ByteBuffer.allocate(2).order(LITTLE_ENDIAN).putShort(id)`. `e8.r.a.a(int)` (`p056e8/r.java:25-45`) maps a
received ID back to a property class (LED-mode ranges first, then the known lists, else `r.d(id)` unknown).

### 3.2 UserAccess byte

`de/jung/junghome/domain/item/mesh/UserAccess.java:23-26`: `READ = 1`, `WRITE = 2`, `READ_AND_WRITE = 3`,
`UNKNOWN = 0` (same values as SIG Generic Property "User Access"). Every builder that sends an Admin Set
uses `READ_AND_WRITE` (e.g. `p254x7/a.java:31`, `p234v7/C2371m.java:33`).

### 3.3 Layouts

| Message | Wire opcode | Parameters (offset: size – meaning) | Built / parsed by |
|---|---|---|---|
| Admin Property **Get** | `C2 27 05` | `0:2` PropertyID u16 LE; `2:n` optional extra parameter bytes (usually empty; AstroSchedulerRegister Get passes 1 byte register-id) | `G7/a.java:25` (`property.a() + bArr`); extra bytes e.g. `B7/a.java:20-26` |
| Admin Property **Set** | `C3 27 05` | `0:2` PropertyID u16 LE; `2:1` UserAccess; `3:n` value | `G7/b.java:33-39` (`property.a() + [userAccess] + parameter`) |
| Admin Property **Status** | `C5 27 05` | `0:2` PropertyID u16 LE; `2:1` UserAccess; `3:n` value | `AbstractC1972z0.java:46-63` |
| Manufacturer Property **Get** | `C8 27 05` | `0:2` PropertyID u16 LE | `G7/c.java:20` |
| Manufacturer Property **Set** | `C9 27 05` | `0:2` PropertyID u16 LE; `2:n` value (**no** UserAccess byte, unlike SIG) | `G7/d.java:25` |
| Manufacturer Property **Status** | `CB 27 05` | `0:2` PropertyID; `2:1` UserAccess; `3:n` value | `AbstractC1972z0.java:46-63` |
| User Property **Get** | `CE 27 05` | `0:2` PropertyID u16 LE | `G7/e.java:20` |
| User Property **Set** | `CF 27 05` | `0:2` PropertyID u16 LE; `2:n` value | `JungMeshApiImpl.java:318` (`m.u(r.a.a(s6).a(), bArrI)` = id ‖ value) |
| User Property **Status** | `D1 27 05` | `0:2` PropertyID; `2:1` UserAccess; `3:n` value | `AbstractC1972z0.java:46-63` |

Status parsing detail (`AbstractC1972z0.d`, `resolver/AbstractC1972z0.java:38-67`):
1. accept only if `P7.a.e(msg) ∈ {0xD12705, 0xC52705, 0xCB2705}` (line 43) – the three families share one parser;
2. `propertyId = getBits(8) | getBits(8) << 8` (line 47, LE);
3. `userAccess = getBits(8)` (line 49-51);
4. remaining bytes = value; if empty the message is ignored (line 57);
5. two views of the value are prepared for the concrete resolver: a Nordic `BitReader` over the **reversed**
   bytes (`n.Q` = `reversedArray`, line 62; `kotlin/collections/n.java:257`) so `getBits(16)` yields the LE
   u16, and a `ByteBuffer.order(LITTLE_ENDIAN)` (line 63). Both mean *values are little-endian*.
6. the concrete resolver (`LBCPropertyStatusMessageResolver.kt`, ~90 classes `resolver/A.java` … `resolver/f2.java`,
   each `extends AbstractC1972z0<e8.r.c.X>`) decodes the value for one property ID.

Received property status with an unexpected family (e.g. an Admin *Get* answered by a User *Status*) is
still accepted as the reply; see §6.2.

### 3.4 `readProperty` / `writeProperty` (raw property API in `JungMeshApiImpl`)

`JungMeshApiImpl.n0(short propertyId, int dstAddress, int modelId)` = `readProperty` (`JungMeshApiImpl.java:232-288`):

| modelId | message built |
|---|---|
| 0x0527:1011 (LBC Admin) | `G7.a(appKey, r.a.a(id), byte[0])` → `C2 27 05 id_lo id_hi` (line 255) |
| 0x0527:1013 (LBC User) | `G7.e(appKey, r.a.a(id))` → `CE 27 05 id_lo id_hi` (line 257) |
| 0x1013 SIG User | `GenericPropertyGet(0x822F=33327, key, id)` (line 259) |
| 0x1011 SIG Admin | `GenericPropertyGet(0x822D=33325, …)` (line 261) |
| 0x0527:1012 (LBC Mfr) | `G7.c(appKey, r.a.a(id))` → `C8 27 05 id_lo id_hi` (line 263) |
| 0x1012 SIG Mfr | `GenericPropertyGet(0x822B=33323, …)` (line 265) |
| 0x1100 Sensor Server | `SensorGet(key, DeviceProperty.from(id))` (line 270) |

`JungMeshApiImpl.q1(short propertyId, int dst, String hexValue, UserAccess ua, int byteLength, int modelId)`
= `writeProperty` (`JungMeshApiImpl.java:290-346`): value = `UtilsKt.i(byteLength, hexValue)`
(`de/jung/common/UtilsKt.java:153-179`: hex string zero-padded on the left to `byteLength` bytes, bytes
emitted in the order typed):

| modelId | message |
|---|---|
| 0x0527:1011 | `G7.b(key, prop, ua, value)` → `C3 27 05 id_lo id_hi ua value…` (line 314) |
| 0x0527:1013 | `G7.f(key, MODEL_ID, 1319, 337679, id_lo id_hi ‖ value)` → `CF 27 05 id_lo id_hi value…` (line 318) |
| 0x1011 SIG | `GenericAdminPropertySet(key, id, ua, value)` (line 320) |
| 0x0527:1012 | `G7.d(key, prop, value)` → `C9 27 05 id_lo id_hi value…` (line 322) |
| 0x1012 SIG | `GenericManufacturerPropertySet(key, id, ua)` (line 324) |
| 0x1013 SIG | `GenericUserPropertySet(key, id, value)` (line 329) |

Both are sent with `h3()` → `c.b(dst, msg)` = *unacknowledged request* (`JungMeshApiImpl.java:197-230`,
`de/jung/junghome/data/mesh/api/c.java`, class `b`), i.e. the app does not wait for a Status here.

### 3.5 Normal (capability-based) flow

* `JungMeshApiImpl.E1` (read) / `U1` (write) (`JungMeshApiImpl.java:109,166`; bodies not decompiled) select a
  `MessageBuilder` `p234v7.W<Capability, GetMessage>` (`p234v7/W.java`): `b()` builds the Get, `c()` the Set,
  `d()` returns the **expected status opcode** in the receive encoding (`p234v7/W.java:30-34`).
* Result is wrapped in `p234v7.V(statusOpCode, meshMessage)` (`p234v7/V.java:9-12`) and turned into
  `c.a Acknowledged(destination = capability.O() /*element address*/, msg, statusOpCode, statusAddressSrc)`
  (`JungMeshApiImpl.java:74-87`).
* `MeshMessengerImpl.processRequest` (`de/jung/junghome/data/mesh/MeshMessengerImpl$processRequest$2.java:47-97`)
  calls `meshManagerApi.createMeshPdu(dst, msg)` and then `waitForStatusMessage(src, statusOpCode, alsoAcceptUserStatus)`.

Expected status opcodes used by builders (`this.f…b = …` in each builder): `12920581` (0xC52705 Admin
Status) for ~60 builders, `13313797` (0xCB2705 Mfr Status) for `C7/a,b,c`, `p245w7/a,b,c`,
`p234v7/C2366j0`, `p234v7/C2370l0`; `13707013` (0xD12705 User Status) for `p234v7/C2351c`
(ActuatorMessageBuilder) and `p234v7/U` (MeasureConsumptionMessageBuilder); `13838085` (0xD32705) for
`B7/c,d,e,f`; `14100229` (0xD72705) for `A7/b,g`.

### 3.6 Property-ID numbers seen attached to vendor messages

(IDs only – semantics/value layouts are catalogued separately. Source: the builders listed in §3.5 and
`p056e8/r.java`.)

**Via LBC Admin Property Get/Set (`C2`/`C3 27 05`)** – expected reply `C5 27 05`:
1 DeviceKeyLock, 7 AstroSchedulerRegister, 8 AstroSchedulerStatus, 9 EnforceOutput (LockFunction),
15 AutomaticDaylightSavingTime, 18 RtrTouchSensitivity, 19 DimMode,
4097 GeneralOnDelay, 4098 GeneralOffDelay, 4103 TimedOnDuration, 4106 Prewarning, 4107 ManualOffEnable,
4108 InvertOutput, 4109 SwitchBlockingTime, 4110 DimToWarm, 4116 RtrOperationMode,
4353 MoveRevisionTime, 4354 MoveUpDownTime/BlindMoveUpDownTime, 4355 MoveSlatsTime, 4356 MoveOperationMode,
4357 MoveOnPowerMode, 4358 MoveBlindPositionOnPower, 4359 MoveSlatPositionOnPower, 4360 BlindsInvertOutput,
4362 MoveBlindsVentilationPosition, 4363 MoveSlatsVentilationPosition, 4365 ReferenceRun,
4609 ControllerType, 4611 TempComfort, 4612 TempStandby, 4613 TempFreeze, 4616 HeatingOption,
4618 RTRValveOutput, 4619 RtrHvacMode, 4621 RtrBoostMode, 4641 RtrSensorSelection, 4644 RtrSensorOffset,
4672 RtrMainPage, 4678 SchedulerEnabled, 4679 BlAutoOff,
20481 ButtonLayout, 20482 KeyModeSceneConfig, 20483 KeyMode, 20484 TurnOnThreshold, 20485 TurnOffThreshold,
20486 KeySetPropertyMode, 20487 KeySetPropertyValueUpOn, 20488 KeySetPropertyValueDownOff, 20489 InputEdgeDetection,
24577 PresenceTest, 24579 PresenceControl, 24582 DetectorOperationMode, 24584/24585/24586 PirSensorA/B/C,
24591 SwitchOnBrightness, 24597 DayMode, 24598 DetectorForcedOff, 24599 DetectorOperationSite, 24609 DetectorRepetitionTime,
40961 + 3·(led-1) LedModeOn, 40962 + 3·(led-1) LedModeOff (led = 1..6; `p266y7/a.java:35`, `p266y7/b.java:35`, range check `p056e8/r.java:912-921`).

**Via LBC Manufacturer Property Get/Set (`C8`/`C9 27 05`)** – reply `CB 27 05`:
3 SecureElementVersion, 4 BootloaderVersion, 5 STM32Version (`C7/a,b,c`), 24580 PresenceBrightness (Get+Set),
24581 PresenceControlPir (Get+Set), 49153 (0xC001) GatewayAPIToken, 49154 (0xC002) GatewayIP,
49155 (0xC003) GatewayFingerprint (`p245w7/a,b,c`). (49152 = 0xC000 GatewayAPIStatus exists as a property
class, `p056e8/r.java:2358-2372`, but no builder was found.)

**Via LBC User Property Get (`CE 27 05`)** – reply `D1 27 05`:
2 InsertIdProperty (`p234v7/C2351c.java:19`), 20496 DailyEnergyChart, 20497 MonthlyEnergyChart (`p234v7/U.java`).
User Property *Set* (`CF 27 05`) is only used by the raw `writeProperty` API in the app; the 0x1015 client on
buttons presumably sends it (see §6.1).

The SIG (non-vendor) property models are also used for 16 ManufacturerHardwareRevision, 17 ManufacturerName,
26 ManufacturerSoftwareVersion, 106 TotalDeviceEnergyUse, 109 TotalDevicePowerOnTime (`GenericPropertyGet`,
expected status 70 = 0x46 / 74 = 0x4A) – listed here only to avoid confusion.

---

## 4. Non-property vendor messages

### 4.0 JUNG's bit packer (LSB-first)

All JH Scheduler and Scene-Action payloads are produced by a JUNG bit writer that jadx merged into
`androidx/compose/animation/core/k0.java` (`n(value, nBits)` at lines 98,128-146; `m()` at 78,88-94) and
parsed by the matching reader merged into `androidx/compose/foundation/layout/M.java` (`M(byte[])` line 124,
`f(nBits)` lines 64-75). Semantics:

* fields are appended **starting at bit 0 of byte 0**, each field's LSB first
  (`i12 |= ((bArr[i14/8] >> (i14 % 8)) & 1) << i13`);
* a field wider than the remaining bits of a byte continues in the next byte;
* the writer flushes a partial last byte with zero padding.

Consequently an 8-bit field that starts on a byte boundary is just that byte, and a 16-bit field on a byte
boundary is a **little-endian u16**. "Byte 0 = (subCommand << 4) | index" below follows from this.

### 4.1 JH Scheduler (model 0x0527:1016) – `D2` Get / `D4` Set / `D3` Status

**Verified on air** for empty slots (`hidden-features.md` §10): sub 0 answers 8 bytes, sub 1 7, sub 2
8, sub 15 6 with the `centralScheduleId` echoed in byte 1; an undefined sub-command echoes the header byte alone
and a parameterless Get answers an empty status. Decoders / builders: `jhmesh/vendor_models.py`.

Common header (all three): `bits 0-3` = **schedulerIndex** (0..15), `bits 4-7` = **subCommand**
(`p056e8/h.java`, `JHSchedulerStatus.kt`): `0 = Schedule` (line 63), `1 = Action` (17), `2 = EffectiveTime` (40),
`15 = ScheduleList` (86). Status dispatch on the sub-command nibble: `resolver/AbstractC1934m0.java:26-49`
(`m9.f(4)` index discarded, `m9.f(4)` = sub-command).

A **1-byte Status** (header only) means "nothing stored / acknowledged" – resolvers return the node
unchanged (`resolver/C1928k0.java:95-97`, `C1931l0.java:79-81`, `C1940o0.java:77-79`) or `null`
(`C1937n0.java:83-85`).

Enumerations:
* `JHSchedulerType` (`de/jung/junghome/domain/item/mesh/JHSchedulerType.java:24-32`): 0 Available, 1 Reserved,
  2 TimedInactive, 3 TimedActive, 4 SunriseInactive, 5 SunriseActive, 6 SunsetInactive, 7 SunsetActive.
* `DayOfWeek` bitmask (`DayOfWeek.java:24-30`): Mon=0x01, Tue=0x02, Wed=0x04, Thu=0x08, Fri=0x10, Sat=0x20, Sun=0x40
  (`com/google/firebase/b.java:271-293` `N()`/`O()` convert set↔mask).
* `offsetMin` (`W7/d.java`, `AstroTimer.kt`): 8-bit two's complement minutes relative to sunrise/sunset:
  `After(n)` → `n`; `Before(n)` → `(~n & 0xFF) + 1` (line 72); on receive a value > 128 is `Before(256-v)`
  (`C1840v.java:130`, `C1931l0.java:97-98`).
* `JHSchedulerStatus` (`JHSchedulerStatus.java:25-30`): 0 Available, 1 CentralScheduleIdMatched, 2 Inactive, 3 Active.

#### 4.1.1 Schedule (sub-command 0)

**Get** (`B7/f.java:42-46`, 1 byte): `bits0-3 index, bits4-7 = 0`.

**Set – full** (`B7/f.java:88-103`, 64 bits = 8 bytes; field names from `V8/c.java:196-215` and accessor
mapping `V8/c.java:69-75,142-150,220-227`):

| bits | width | field |
|---|---|---|
| 0-3 | 4 | schedulerIndex |
| 4-7 | 4 | subCommand = 0 |
| 8-11 | 4 | type (JHSchedulerType) |
| 12-15 | 4 | 0 |
| 16-22 | 7 | dayOfWeek mask |
| 23 | 1 | 0 |
| 24-29 | 6 | notBeforeMM |
| 30-34 | 5 | notBeforeHH |
| 35-40 | 6 | notAfterMM |
| 41-45 | 5 | notAfterHH |
| 46-47 | 2 | 0 |
| 48-55 | 8 | offsetMin (two's complement) |
| 56-63 | 8 | 0 |

**Set – type only / "toggle"** (`B7/f.java:74-80`, 2 bytes): `index(4) | sub=0(4) | type(4) | 0(4)`.
Used when only `schedulerIndex`, `subCommand`, `type` are set (e.g. type = Available to clear a slot).

**Status** (`C1940o0.java:82-85` → `C1840v.java:113-130`): same layout as the full Set, 7 bytes needed
(index, sub, type, pad4, days7, pad1, notBeforeMM6, notBeforeHH5, notAfterMM6, notAfterHH5, pad2, offsetMin8).

#### 4.1.2 Action (sub-command 1)

**Get** (`B7/c.java:35-39`, 1 byte): `index(4) | sub=1(4)`.

**Set** (`B7/c.java:52-54` → `V8/a.java:99-139`, 56 bits = 7 bytes):

| bits | width | field |
|---|---|---|
| 0-3 | 4 | schedulerIndex |
| 4-7 | 4 | subCommand = 1 |
| 8-15 | 8 | actionCode (`e8.f.j()`: 0 NoAction, 1 Switching, 2 Lightness, 3 LightnessAndColorTemperature, 4 BlindsAndSlatsPosition, 5 TargetTemperature – `p056e8/f.java:42-43,73-74,112-113,139-140,170-171,202-203`) |
| 16-55 | 40 | action payload, always padded to 40 bits: |

| actionCode | payload (LSB-first from bit 16) |
|---|---|
| 0 NoAction | 40 × 0 (`V8/a.java:112`) |
| 1 Switching | `on` u8 (0/1), then 32 × 0 (line 127-128) |
| 2 Lightness | `lightness` u16 = percent·65535/100 (`U8.a.C0058a.a(x,2)` → `io/grpc/u.java:37-45` range 0..65535), then 24 × 0 (119-120) |
| 3 Lightness+CT | `lightness` u16, `colorTemperature` u16 **in Kelvin** (2000..10000, rounded to 100 K; `io/grpc/u.java:393-399`), then 8 × 0 (123-125) |
| 4 Blinds+Slats | `blindLevel` s16 (percent → -32768..32767, Generic Level range), `slatLevel` s16, then 8 × 0 (115-117) |
| 5 TargetTemperature | `temperature × 100` u16 (centi-°C), then 24 × 0 (133-134) |

**Status** (`C1928k0.java:99-119`): identical layout; decoding back to percent: 1 raw; 2/3 `u.u(0..65535→%)`;
4 `u.v(s16→%)`; colour temperature `round((K − 2000) / 8000 · 100)` %; 5 `/100.0`.

#### 4.1.3 EffectiveTime (sub-command 2)

**Get** (`B7/d.java:36-40`, 1 byte): `index(4) | sub=2(4)`. **Set: not implemented** in the app
(`B7/d.java:47` throws `NotImplementedError`).

**Status** (`C1931l0.java:83-98` → `e8.g`, `p056e8/g.java:53-55`), 56 bits = 7 bytes:

| bits | width | field |
|---|---|---|
| 0-3 | 4 | schedulerIndex |
| 4-7 | 4 | subCommand = 2 |
| 8-11 | 4 | type (JHSchedulerType) |
| 12-15 | 4 | ignored |
| 16-22 | 7 | dayOfWeek mask |
| 23 | 1 | ignored |
| 24-29 | 6 | effectiveTimeMM |
| 30-34 | 5 | effectiveTimeHH |
| 35-47 | 13 | ignored |
| 48-55 | 8 | offsetMin |

(i.e. the *computed* next trigger time for astro schedules.)

#### 4.1.4 ScheduleList (sub-command 15)

**Get** (`B7/e.java:30-36`, 2 bytes): byte0 = `0xF0` (index 0, sub 15); byte1 = `centralScheduleId` u8
(`V8/d.java:97-99`, 0 if unset). **Set: not implemented** (`B7/e.java:43`).

**Status** (`C1937n0.java:79-92`), 6 bytes: bits 0-15 skipped (header byte + presumably the echoed
centralScheduleId), then **16 × 2-bit `JHSchedulerStatus`** for slots 0..15 (bits 16-47), LSB-first, i.e.
slot 0 = byte2 bits 0-1, slot 3 = byte2 bits 6-7, slot 4 = byte3 bits 0-1, …

### 4.2 Scene Action Setup (model 0x0527:1017) – `D6` Get / `D8` Set / `D7` Status

**Verified on air** on nine nodes (`hidden-features.md` §10, `jhmesh/vendor_models.py`): the list form
(`00 00 08 00 09 00 0A 00 0B 00 00 00`), switching (`08 00 01 01 00 00 00 00`), lightness + colour temperature
(`08 00 03 FF FF D0 07 00`), the absent scene (`FF FF`), and a 2-byte scene number in the Get is accepted too.

Scene action codes are the same 0..5 set as the scheduler (`de/jung/junghome/domain/item/mesh/JHSceneAction.java:22-220`:
NoAction 0, Switching 1, Lightness 2, LightnessAndCTL 3, BlindsAndSlatsPosition 4, TargetTemperature 5).

**Get** (`A7/b.java:26-30`, `A7/g.java:27`; `Capability.a.a` = LE ByteBuffer,
`de/jung/junghome/domain/item/mesh/nodes/models/Capability.java:30-37`): **4 bytes**, `sceneNumber` as u32 LE
(only the low 16 bits are meaningful). `sceneNumber = 0` ⇒ "list all scenes" (`ScenesMessageBuilder`).

**Set** (`A7/b.java:35-42` → `S8/a.java:89-155`):

| offset | size | field |
|---|---|---|
| 0 | 2 | sceneNumber u16 LE (packed LSB-first, lines 99-118) |
| 2 | 1 | actionCode (only if action ≠ NoAction, line 119-121) |
| 3 | 5 | 40-bit action payload exactly as §4.1.2 (lines 122-151) |

A Set with **only the 2-byte scene number** (action = NoAction) removes the stored action.

**Status** (`AbstractC1970y1.java:31-38` decides list vs. single):
* bits 0-15 (u16 LE) = sceneNumber. `0` ⇒ **Scenes list** (`resolver/C1.java:73-90`): followed by u16 LE scene
  numbers, terminated by a `0x0000` entry (loop `for (v = f(16); v != 0; v = f(16))`, line 86).
* non-zero ⇒ **Action for that scene** (`resolver/C1967x1.java:97-121`): total length 2 ⇒ NoAction; otherwise
  byte 2 = actionCode, then the 40-bit payload (same decode as §4.1.2; for code 4/5 the s16→% conversion is
  used, line 30-33).
* length 1 ⇒ ignored (line 101-103 / 78-80).

---

## 5. Request / response matching and unsolicited status

* Every received `MeshMessage` goes through `MeshNotificationChannel.onMeshMessageReceived`
  (`de/jung/junghome/data/mesh/MeshNotificationChannel.java:1469-1477`) → `onMeshNotificationReceived`
  (`MeshNotificationChannel.java:200-445`): the node is looked up by `src`, **all** resolvers are applied via `Q1.b`
  (`resolver/Q1.java:b`, called at `MeshNotificationChannel.java:305`) whether or not a request is pending, the
  node state is stored, and then a tuple `(k9.a(src, opcodeInt, params), node)` is emitted on a shared flow
  (`MeshNotificationChannel.java:376-398`, vendor opcode via `P7.a.e` at line 380/425). Hence **unsolicited
  vendor Status publications are consumed and applied** (e.g. `D1 27 05` User Property Status for InsertId /
  actuator state, `D3 27 05` scheduler changes).
* `waitForStatusMessage(src, expectedOpcode, alsoAcceptUserStatus)`
  (`MeshNotificationChannel$waitForStatusMessage$1.java:172`) completes on the first message from `src`
  whose opcode equals `expectedOpcode` **or**, when the flag is set, equals `13707013` (`D1 27 05`, LBC User
  Property Status). The flag is computed in `MeshMessengerImpl$processRequest$2.java:97`: it is `true` for every
  request except an Admin Get/Set of property **9 (EnforceOutputProperty)** (`p056e8/r.java:2223,2235`).
  Practical meaning: nodes may answer an Admin/Manufacturer request with a *User* Property Status (or publish
  one right after a Set) and the app treats that as the acknowledgement.
* If the request opcode equals the expected status opcode the app does not wait
  (`MeshMessengerImpl$processRequest$2.java:89-91`).
* App-key binding: the app binds its app key to every model of every element with `ConfigModelAppBind`
  (`de/jung/junghome/data/mesh/c.java:197`), vendor models included, so all of the above are app-key
  encrypted access messages.

---

## 6. Other notable points

### 6.1 The 0x0527:0x1015 client and what buttons publish
`GetModelsForDeviceConnection.java:161-179` / `p056e8/j.java` pair *client* models on the sending element with
*server* models on the receiving element per key mode (`p056e8/i.java:14-56`, names from
`de/jung/junghome/app/ui/devices/miniActuator/configuration/MiniActuatorConfigurationFragment.java:348-366`):

| KeyMode (byte) | client models published from (button) | server models subscribed (target) |
|---|---|---|
| Light (0) | 0x1001 OnOff Client, 0x1003 Level Client, **0x0527:1015** | 0x1000 OnOff Server, 0x1002 Level Server |
| Move (1) | 0x1003, **0x0527:1015** | 0x1002 |
| Scene (2) | 0x1205 Scene Client | 0x1203 Scene Server |
| Property (3) | **0x0527:1015** | **0x0527:1013 LBC User Property Server** |
| RTR (4) | 0x1003 | 0x1002 |
| Switch (5) | 0x1001, **0x0527:1015** | 0x1000 |
| Gateway (6) | **0x0527:1015** | **0x0527:1013** |

So the vendor client publishes LBC User Property messages (most likely `CF 27 05` Set or its unacked
variant) to the group; in *Property* mode the property and values are configured on the button with Admin
properties 20486 KeySetPropertyMode (`u16 propertyId LE` + `u8 mode` 0 STATELESS / 1 STATEFUL,
`p234v7/E.java:50-56`, `de/jung/junghome/domain/item/mesh/PropertySetMode.java:30-32`), 20487
KeySetPropertyValueUpOn and 20488 KeySetPropertyValueDownOff (raw value bytes of the target property,
`p234v7/F.java:35-43`). The actual PDU emitted by the button is not visible in the app.

### 6.2 Status-family leniency
The property status parser accepts any of the three family opcodes for any property
(`AbstractC1972z0.java:43`), and the request waiter accepts `D1 27 05` for almost everything (§5).

### 6.3 Model IDs 0x1016 / 0x1017
0x1016 is the JUNG scheduler ("JH Scheduler", §4.1) – independent of the SIG Scheduler models 0x1206/0x1207,
which the app also drives (`B7/g.java` SchedulerActionGet, `B7/h.java` SchedulerGet) for the "Timer"
feature. 0x1017 stores scene *actions* per element (§4.2); scene *registration/recall* itself uses the SIG
Scene models (`A7/c,d,e,f` → `SceneGet`, `SceneRegisterGet`). Status 94 = `0x5E` is the *Scene Status* (the answer
to Scene Get / Recall); *Scene Register Get* `0x8244` is answered by *Scene Register Status* `0x8245` (Mesh Model
spec, `network-logic.md` §5.2, `jhmesh/messages.py`; seen on air, `hidden-features.md`).

### 6.4 Nordic-side pitfalls for re-implementers
* Use `createVendorOpCode`, not `getOpCode(int)`, to build vendor opcodes (§2.1).
* Received vendor status carries `modelIdentifier = 0` when unsolicited (`DefaultNoOperationMessageState.java:350`).
* Nordic's `BitReader` (`no/nordicsemi/android/mesh/utils/BitReader.java`) is MSB-first per byte; JUNG's own
  reader/writer are LSB-first (§4.0). JUNG only uses Nordic's reader for whole-byte reads (property ID /
  user access) and over reversed value bytes.

---

## 7. Confidence / open questions

| # | Item | Confidence | Notes |
|---|---|---|---|
| 1 | Wire bytes of the 15 opcodes in §2.3 | High | Derived from constants + `createVendorOpCode`; the six Set/Get classes and five received constants are all in code. On air: `C2/C5`, `C8/CB`, `CE/D1` (below), `D2/D3` and `D6/D7/D8` (`hidden-features.md`). |
| 2 | Unacknowledged / "Properties Get" opcodes (0x00,0x01,0x04,0x06,0x07,0x0A,0x0C,0x0D,0x10,0x15,0x19) | High for 0x00/0x01, 0x06/0x07, 0x0C/0x0D, 0x04, 0x10; Low for 0x0A, 0x15, 0x19 | Never used by the app. Verified on air: the three *Properties Get/Status* pairs and Admin Set Unack `C4` (§2.3), User Set Unack `D0` (published by keys, below); the gateway firmware's table has the same numbering (`cross-repo-analysis.md` §1.3). 0x0A, 0x15, 0x19 remain inference. |
| 3 | Property ID byte order in Status | High (LE) | `AbstractC1972z0.java:47` reads LE; `Q1.java:78` (`hasValueChanged`) appears as `(b0<<8)|b1` in jadx output – probably an operand-order artefact of decompilation (both come from `StatusMessageResolver.kt`); if real it only affects change-detection, whose default is "changed". |
| 4 | Manufacturer Property Set has no UserAccess byte | High | `G7/d.java:25`. |
| 5 | Admin Get may carry extra parameter bytes | Medium | Only AstroSchedulerRegister uses it (`B7/a.java:20-26`); whether other properties accept/ignore extra bytes is unknown. |
| 6 | JH Scheduler bit layouts | High for Schedule/Action/EffectiveTime (app code), High for ScheduleList | ScheduleList Status byte 1 is the `centralScheduleId` echo — confirmed on air (`F0 01 …` for id 1, `hidden-features.md`); the 16 skipped bits are not decoded by the app. Only empty slots were seen on air; the *filled* Schedule / Action / EffectiveTime layouts are still the app's. |
| 7 | Colour temperature on the wire is Kelvin | High | Encode `2000 + 8000·pct` rounded to 100 K (`io/grpc/u.java:393-399`); decode `(v−2000)/8000`. Confirmed on air in Scene Action Setup Status: a dimmer's stored action `03 FF FF D0 07 00` = lightness 100 %, `0x07D0` = 2000 K (`hidden-features.md`). |
| 8 | Scene list terminator | High | Parser loops until a `0x0000` u16 and throws if it runs out of bits (`M.java:67-68`). Confirmed on air: `00 00 08 00 09 00 0A 00 0B 00 00 00` = scenes 8–11, terminated by 0; a scene-less element answers `00 00 00 00` (`hidden-features.md`). |
| 9 | What 0x0527:1015 actually transmits | High (resolved) | Captured: `D0 27 05` (User Property Set Unacknowledged) with property `0x5012` in key mode *Gateway* (Field-tested additions below). |
| 10 | Meaning of 1-byte vendor Status replies | Medium-High | Treated as "no data / ack" by all resolvers. On air the JH Scheduler answers an undefined sub-command by echoing its header byte alone (`30` for sub 3) — "unknown command", not "slot empty" (`hidden-features.md`); other 1-byte statuses not seen. |
| 11 | Admin Set UserAccess semantics | Medium | App always sends 3 (READ_AND_WRITE); whether the node honours other values is unknown. |
| 12 | `G7/f` (LBCGenericUserPropertySet) | High | jadx emitted an empty class body; the only call site (`JungMeshApiImpl.java:318`) shows payload = `propertyId(LE) ‖ value`. |
| 13 | 0x1016 / 0x1017 have no client-side counterparts in the app | High | No `0x0527:1018+` IDs anywhere in JUNG code (`grep 8644610[4-9]`). |


---

## Field-tested additions (see `docs/poc-gatt-proxy.md`)

- **Opcode `0x10` (`D0 27 05`) = LBC User Property Set Unacknowledged — confirmed.** Button elements in key mode
  *Gateway* publish it from their `0527:1015` client model to the gateway's element group with property `0x5012`
  `[counter u8][event u8]` (`05` click, `06` hold start, `04` hold end from single keys; rocker halves use `00`–`03`,
  see `properties.md` §1.6). This is the PDU the 0x1015 client emits (open question in §7 resolved for key mode 6).
- **Every publication is sent twice.** The `0x5012` events, but equally every model status publication (OnOff, CTL,
  Sensor …), go out twice ≈1 s apart with a fresh network sequence number and an unchanged payload (same counter /
  TID). The CDB configures retransmit count 0 on every publication, so this is the device firmware's (2.2.0.x)
  application-level policy, not mesh publish-retransmission; the gateway firmware processes both copies (it has no
  dedupe, `docs/cross-repo-analysis.md` §1.2). Dedupe on `(src, counter)` for `0x5012` and on `(src, opcode, TID)`
  for SIG messages; whether `Scene Recall` / `OnOff Set` from keys are doubled too is still to be captured.
- The gateway drives a key's status LED through the companion property `0x5013` by sending a **User Property
  Status** (`D1 27 05`) *to* the button element — the status opcode used as a write (`properties.md` §1.6).
- Vendor property Get/Status framing confirmed on air for Admin/Manufacturer/User (`C2/C5`, `C8/CB`, `CE/D1` + `27 05`).
