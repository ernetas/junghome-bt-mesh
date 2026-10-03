# JUNG HOME (Bluetooth Mesh)

> **Looking for how to do something?** Start with the [user guide](user/README.md): setting up, everyday use,
> buttons and automations, energy, changing the installation, maintenance and an FAQ, in plain words. This page is
> the complete reference behind it; every entity is listed in the [entity reference](user/entities.md), and the
> notes for developers are in [docs/dev](dev/README.md).

The **JUNG HOME (Bluetooth Mesh)** integration (`junghome_ble`) connects Home Assistant directly to the Bluetooth
Mesh network of a [JUNG HOME](https://www.jung.de/) installation by Albrecht JUNG GmbH & Co. KG. JUNG HOME
push-buttons (with switch, dimmer or DALI tunable-white inserts), sockets and actuators form a Bluetooth SIG Mesh
network that is normally operated from the JUNG HOME app and, optionally, the JUNG HOME Gateway. This integration
needs neither the gateway nor the cloud: it uses the network export of the JUNG HOME app (which contains the mesh
keys) to talk to the devices through the Bluetooth adapter of the Home Assistant host or an ESPHome Bluetooth proxy,
and it listens to the status messages the devices publish, so a light switched at the wall or from the app is updated
in Home Assistant within about a second.

Home Assistant joins the mesh as one additional node with its own address; it is not provisioned, and nothing on
the devices changes until you use a setting or an action that changes the installation — rooms, key connections,
scenes, schedules, thresholds, sensor publications, the device parameters, the *Node heartbeats* option. Those write
what the app writes (and record it in the export), so the app and the gateway keep working as before.

> **Status.** The mesh layer has been proven on air with the standalone tools in this repository
> (`docs/poc-gatt-proxy.md`). The Home Assistant layer runs on the maintainer's installation (over ESPHome
> Bluetooth proxies) and is unit-tested against a simulated proxy node; what is marked *not yet tried on a real
> device* below is implemented from the specifications only.

## Supported devices

The integration was developed against a network created with JUNG HOME app 2.2.0 and devices running firmware 2.2.x.
Every node listed in the export appears as a device in Home Assistant; entities are created for the products below.

| Product | Entities |
|---|---|
| Push-button 1-gang / 2-gang with switch, dimmer or DALI tunable-white insert | One `light` per output, one `event` per rocker or key |
| Socket (metering) | `switch` (outlet) plus power, voltage and current `sensor`s |
| Socket (without metering) | `switch` (outlet) |
| Switch actuator 1-gang mini | `light` for the output, one `event` per binary input (*Input E1* / *Input E2*, as in the app) and an input-state `binary_sensor` per input (off by default, **unverified on hardware**) |
| Energy puck (switch actuator 1-gang 2-input energy, 0x10) | As the mini, plus power and energy `sensor`s and *Reset consumption* on the output's light — **unverified on hardware** (see [Sensor](#sensor)) |
| 2-channel actuator configured as two switched outputs | One `light` per output |
| Scenes | One `scene` per scene, with what each member does; created, filled and deleted with the scene actions |
| JUNG HOME Gateway | Listed as a device with the diagnostic node entities below only; rocker events of buttons linked to the gateway are decoded |
| Motion detector 1 m / 2 m, presence detector | `light` for the relay output, a motion / occupancy `binary_sensor` and an illuminance `sensor` on the node device — **unverified on hardware** (see [Binary sensor](#binary-sensor)) |
| Wall transmitter 1-gang / 2-gang, mini sensor 2-input battery | One `event` per key, a battery `sensor` on the node device — **unverified on hardware** (see [Sensor](#sensor)) |

Every mains-powered node (the gateway included) also gets an *Identify* `button`, a *Fault* `binary_sensor` and a
*Clear faults* `button`, all diagnostic (see [Device parameters](#device-parameters-number-select-switch-button));
battery nodes sleep and would not answer them, so they get none. Any output that exposes a Generic OnOff, Light
Lightness or Light CTL server in the export becomes a light, so future JUNG products that follow the same
composition are picked up automatically.

Implemented from the specifications only, **not yet seen working on hardware** (please report): blinds
([Cover](#cover)), the room thermostat ([Climate](#climate)), detectors and battery products
([Binary sensor](#binary-sensor), [Sensor](#sensor)).

Not supported yet (the node is listed as a device, but has no entities):

- Mains binary-input pucks beyond their keys (their keys are exposed as `event` entities when the export lists them as
  On/Off clients, but this has not been tested)

## Supported functionality

Names come from the JUNG HOME app when the optional metadata directory is configured (see
[Prerequisites](#prerequisites)); otherwise lights, sockets and buttons are named after the node in the export
(for example `Push-button 1-gang 0148`) and scenes `Scene 1`, `Scene 2`, and so on.

### Light

One `light` entity per output. The entity is the device, so its name is the name of the load in the app.

| Insert / output type | Colour mode | Features |
|---|---|---|
| Switch insert, mini actuator, 2-channel actuator output | `onoff` | On/off |
| Dimmer insert | `brightness` | On/off, brightness |
| DALI tunable-white insert | `color_temp` | On/off, brightness, colour temperature within the range the light reports (2000–6000 K until it has) |

- The 2000–6000 K limits are only the defaults, taken from the JUNG HOME Gateway, which clamps to that range in its
  own software. After every connection each tunable-white light is asked for its supported range (*Light CTL
  Temperature Range Get*; the app lets you set 2000–10000 K, and so do the *Minimum / Maximum colour temperature*
  entities below), and the slider and the clamp applied to commands follow the light's answer (see
  [Data updates](#data-updates)).
- Commands are sent without a transition time, and only the narrowest message for what was asked: turning a
  tunable-white light on with only a brightness sends a *Light Lightness Set* (no colour temperature is sent or
  guessed, the light keeps its own); with only a colour temperature while the light is on, a *Light CTL Temperature
  Set* to its temperature element, with an explicit transition time 0 as the gateway sends it (the brightness is not
  sent, so a stale cached one cannot make the light jump);
  with a colour temperature and a brightness, or to switch it on, a *Light CTL Set* carrying the brightness (the last
  known one, full brightness when none is known or the light is off); with neither, a *Generic OnOff Set*.
- **Transitions** are not offered yet: neither the app nor the gateway ever sends a transition time, and whether a
  JUNG dimmer, DALI light or switch insert fades a Set that carries one — or ignores the Set altogether — is still to
  be probed on air (`docs/hidden-features.md` §11). The support is built and switched off: once a kind of light is
  known to fade, its lights offer `transition`, which goes into the Set as the nearest transition time (100 ms steps
  up to 6.2 s, then 1 s, 10 s and 10 min steps), *All lights* passes it on when all its lights fade, and the light is
  asked for its state a second after the fade it announced ends. Until then a `transition` (from a light profile, say)
  is ignored and the commands are sent exactly as before. **Unverified on air.**
- A colour temperature changed elsewhere is followed from the light's *Light CTL Status* and also from a *Light CTL
  Temperature Status* of its temperature element (whether JUNG lights publish the latter is not observed yet).
- The second output of a two-output insert is a separate light; without app metadata it is named `<node> out 2`.
- Attributes: `mesh_address` (element address, hex), `rooms` (the app rooms the load belongs to), `locked` and
  `lock_until` (see *Locked loads* below).
- **Locked loads** (review-4 F4-2). A light locked in the JUNG HOME app, by a key or by its *Lock* switch (the lock
  function `0x0009`, see [Device parameters](#device-parameters-number-select-switch-button)) keeps its state
  against every command, and the app disables its controls. Each light reads its lock once per connection, behind
  the state refresh and five loads at a time like it (the *Lock* switch shares that read instead of sending its
  own), and shows it as `locked` (`true` / `false`; empty until read) and, for a lock with a time limit,
  `lock_until` (when it should end, ISO 8601 UTC, counted from the lock's report; empty otherwise). While locked,
  turning it on or off, `start_dim` and `step_dim` fail with *… is locked (in the JUNG HOME app, by a key or by its
  Lock switch) and keeps its state until it is unlocked*; nothing is sent. The lock is read again first, as it may
  have ended unseen — lifted in the app (asked unless the light answered within the last 10 s), or past its time
  limit (always asked; a light that stays silent then is commanded). A command a light answers with its old state
  (a lock nobody had read yet) fails the same way once the lock read says so, and a light known to be locked is not
  marked unavailable for leaving a command unanswered. *All lights* and the room lights send to every member
  unacknowledged and are not refused: a locked member ignores them, as in the app. A toggle of a light whose state is
  unknown turns it on (Home Assistant's default; the app sends off), deliberately. **Unverified on air**: what a
  locked load answers to a Set is still to be probed (`docs/hidden-features.md` §12).
- **Loads a room thermostat switches, and detector relays** (review-4 F4-16). The app disables a load's controls in
  two more cases, and so does Home Assistant. A light or socket a room thermostat switches — the app's thermostat
  link: the load's *Generic OnOff* server listens to the element group the thermostat's OnOff client publishes to —
  has the attribute `controlled_by` (the thermostats' names) and refuses on and off with *… is switched by the room
  thermostat …*: the thermostat would switch it back. Its *Run-on time*, *Manual switch-off*, *Switch-on / -off
  delay*, *Minimum switching repeat time* and *Switch-off warning* are unavailable meanwhile, as the app disables
  them. A detector's relay has the attribute `continuous_on_off` (`inactive`, `on`, `off`; empty until read): the
  detector's own *continuous on / off*, read once per connection with the lock. While it is `on` or `off` the relay
  refuses commands with the detector's instruction to end it — the switch for automatic operation into the middle
  position (motion detector 1 m), the ON / OFF button (motion detector 2 m), the programming button (presence
  detector), as the app's banner says; the state is read again first. Other lights have neither attribute.
  **Unverified on air**: there is no room thermostat or detector here.
- **All lights**, on the mesh device, is the app's central "all luminaires" function: one unacknowledged message to
  the lamps' device-type group `FEF5`, which every lamp listens to since it was added in the app, switches them all
  at the same moment (rather than one message per light). With a brightness, the dimmers go to it and the switched
  loads just switch on. It is on while any member is; its brightness is the mean of the dimmable members that are
  on. Attributes: `mesh_address` (`FEF5`), `members` (the lamps' names).
- **All lights in &lt;room&gt;**, also on the mesh device, one per room with lights: the app's central control of an area.
  A brightness is one unacknowledged *Light Lightness Set* to the room's group address (the app's "dim the area",
  which the room's dimmers take); on and off go to each light of the room as an unacknowledged *Generic OnOff Set* of
  its own, as the app sends them — the room's address would switch its sockets too. The same state and attributes as
  *All lights* (`mesh_address` is the room's address). These entities are deliberately not placed in the room's
  area: an action targeting the area already reaches every light in it. The dimmers taking the room-addressed
  brightness is **not yet verified on air**. When a room is deleted, or no longer has lights, its entity is removed
  from Home Assistant as soon as the export says so (the action's own change, or the next export loaded); so are a
  room's *All sockets / blinds / thermostats in* entities. A renamed room renames them, and a light joining or leaving
  the room joins or leaves them, without a reload.
- **Hold-to-dim** — dimmers and tunable-white channels can be dimmed the way a held rocker dims them, with three
  actions targeting their `light` entities (not *All lights*, not a switched light):
  **`junghome_ble.start_dim`** (`direction` `up` / `down`, optional `speed` in % of the range per second, 1–100,
  default 20) sends a *Generic Move Set* to the light's level server — it keeps dimming until
  **`junghome_ble.stop_dim`** (*Generic Move Set* 0) or the end of the range; **`junghome_ble.step_dim`** (`step`,
  −100…100 % of the range) sends a *Generic Delta Set*. After a stop or a step the light is asked for its brightness.
  The app never dims this way (it sends brightness values); **not yet tried on a real device**.

### Switch

One `switch` entity per socket with device class `outlet`. The entity is the device, so its name is the socket's name
in the app. Attributes: `mesh_address`, `rooms`, `locked`, `lock_until`: a locked socket shows it and refuses to
switch, exactly as a light does (*Locked loads* under [Light](#light); **unverified on air**); a socket a room
thermostat switches has `controlled_by` and refuses too (see [Light](#light), **unverified on air**). **All sockets**
(mesh device) does the same for the sockets'
group `FEF8` as *All lights* does for the lamps. Neither exists when no load listens to its group. **All sockets in
&lt;room&gt;** (mesh device, one per room with sockets) switches each socket of the room with an unacknowledged
*Generic OnOff Set* of its own, as the app's area control does.

**Time keeper** (configuration, off by default; review-4 F4-14, *unverified on air*): only in a network with one of
the older JUNG actuator pucks (products `0x0010`–`0x0014`, "PP2"), on every mains device with a Time Server — not the
gateway, not a battery device. The pucks take their time from a *time keeper*, a device that relays the time to the
group `FEFF` they listen to; the JUNG HOME app picks one itself whenever it configures a puck. On: the device's Time
Server publishes to `FEFF` (the export gets the app's `#time_keeper_group#` when it lacks it; written back like any
change), then *Time Role Set* makes it a relay; off: the publication is removed and the role set back to client. The
switch shows the role the device last answered (unknown until it did). The repair issue
[*JUNG HOME pucks have no time keeper*](#repair-issue-jung-home-pucks-have-no-time-keeper) asks for one while
there is none.

### Binary sensor

> **Unverified on hardware.** There is no detector in the network the integration was developed against; everything
> below follows the app, the gateway firmware and the Bluetooth Mesh specifications
> (`docs/gap-analysis/control-and-state.md` §2.8, `docs/cross-repo-analysis.md` §1.4). Please report what a real
> detector does.

One `binary_sensor` per motion or presence detector, on the detector's node device: device class `motion` for the wall
detectors (products 1 m / 2 m), `occupancy` for the ceiling presence detector. The detector's relay output is a
separate `light` as with any other insert. Two sources feed the entity:

| Source | Message | Effect |
|---|---|---|
| Presence Detected | `Sensor Status` with SIG property `0x004D` (one byte) from the detector's sensor element — published to its element group when *Sensor values for gateway* is switched on for the device in the JUNG HOME app, and the answer to the `Sensor Get` sent after every connection | Sets the state directly (on / off) and cancels a running hold |
| The detector switching its load | The `Generic OnOff Set` the detector publishes to its relay's element group or to a room, exactly as a rocker does | *On* switches the entity on for the relay's *Run-on time* (`0x1007`, the time the detector keeps its load on; `DETECTOR_MOTION_HOLD`, 120 s, the app's default for detector loads, until it has been read or while it is 0) and every further *on* restarts that hold; *off* switches it off at once |

The state starts `unknown` and is `unavailable` without a proxy link. Attributes: `mesh_address` (the sensor
element), `relay` (the node's own relay output), `target` (the address the detector's on/off client publishes to)
and `source` (`sensor_status` or `onoff_set`: which of the two set the current state). Nothing is polled; if a detector
never publishes either message, the entity stays `unknown`.

Every blind (unverified on hardware, like the [cover](#cover)) has two more, on the blind device, each one vendor
property Get of the position element per connection:

- **Wind alarm** (device class `safety`): on while the blind's lock function (`0x0009`) holds a wind alarm — the app's
  rule, a lock with priority 255, whoever started it (the app, a rocker's locking function, the *Lock function*
  select). The cover refuses commands meanwhile.
- **Reference run** (device class `running`, diagnostic): `0x110D`, 1 while the drive runs to its reference position.
  As the app does, it is read again when a run should be over (the running time `0x1102` plus 10 s; 600 s, the
  longest the app allows, while the running time is not known) and after the running time or *Inverse operation*
  changed, after which the app checks for a reference run.

The gateway's node device has **API available** and **Client awaiting approval** (diagnostic): the two flags of its
API status (`0xC000`, bit 0 and bit 1 per the gateway firmware; the app never reads it), read once per connection
from the gateway node. *Client awaiting approval* is on while an access request — Home Assistant's own, during setup —
waits to be approved in the app. It is off by default: the firmware sets the bit whenever its
`api_client_name_asking` setting is not empty, and a gateway whose configuration lacks that setting altogether reports
it set with nothing pending (seen on this installation, checked against the gateway's `GET /config`). *API
available* and the *IP address* matched the gateway's own API on air.

An entry set up **from the gateway** also shows what the app's gateway pages show, from the gateway's REST API (all
diagnostic and off by default; `gateway_status.py`): *Firmware version*, *Firmware build*, *Serial number*, *Access
requests* (access requests waiting for approval in the app, their names as an attribute — the app's permissions
indicator), *API clients* (the clients the gateway accepts), the indicators *Network problem*, *Bluetooth mesh
problem*, *Cloud problem* and *Cloud connection* (`GET /api/junghome/config`), and the *Error log*: the number of its
non-debug entries, the latest ten as an attribute (`GET /api/junghome/healthstatus`; the app hides DEBUG entries by
default too). The status is read every 30 s and the error log every 5 minutes, and only while one of their entities
is enabled; the app reads the status every 5 s, but only while it is open. The gateway is asked under the same rules
as the export's upload: only once the gateway node vouched for its certificate, with the token and certificate
repairs raised as the upload raises them (while the token repair is open the polls do not ask at all: nothing is
sent with a rejected token until access is granted again, see
[the repair](#repair-issue-jung-home-gateway-no-longer-accepts-home-assistant)), and a gateway that stops answering
is looked for over the mesh (its address, `0xC002`) once per outage.

**Mini-actuator inputs.** Every binary input of a mini actuator or puck also has an **Input state** `binary_sensor`
(*Input state E1* / *E2*, on the input's buttons device, off by default): a door or window contact or a switch wired
to the input is a state, not only presses. The input sends what its key mode sends — a `Generic OnOff Set` — and the
entity keeps the last value it published (on / off; restored after a restart, since the input only publishes when
it changes; unrelated messages such as a scene recall leave it alone). With the input's **Edge evaluation** on and
the edges set to *Switch on* (rising) / *Switch off* (falling), that value is the level on the input; the review
plan expects the same with edge evaluation off (the app's "state" mode). **Unverified on hardware** — please
report what a real input sends. Which of on / off means "open" depends on the contact, so the entity has no device
class: pick *Door* or *Window* under *Show as* in the entity settings.

### Cover

**Unverified on real blinds — please report.** The maintainer owns no blind actuator: this platform was written from
the JUNG HOME Gateway's firmware and the app's decompiled code (`docs/cross-repo-analysis.md` §1.4,
`docs/gap-analysis/control-and-state.md` §2.6), and no message below has been seen on air yet. If you have a blinds
actuator mini, a blinds PP2 puck or a push-button with a blinds insert, please open an issue with what works and what
does not (the [diagnostics download](#diagnostics) and a debug log of one open / close / stop help).

One `cover` entity per blind, shutter or awning: a load element with a Generic Level server and no lamp server next
to it, on a blinds actuator mini (0x0D), a blinds PP2 puck (0x13) or a push-button with a blinds insert. The entity is
the device, so its name is the blind's name in the app; the node's second Generic Level element, when it has one, is
the slat drive.

| Operation mode (app parameter *Operation mode*, property `0x1104`) | Device class | Features |
|---|---|---|
| Blinds (0) | `blind` | Open, close, stop, position, slat tilt (open / close / stop / position) from the slat element |
| Roller shutter (1) | `shutter` | Open, close, stop, position |
| Awning (3) | `awning` | Open, close, stop, position |
| Not read yet / unknown value | `shutter` | Open, close, stop, position |

- The operation mode is read from the device once after every connection (one vendor property Get, after the state
  refresh). Until it answers, the cover is a plain shutter; the class and the tilt controls appear when it does. The
  reported mode is the `operation_mode` attribute (`unknown` while not read, and for a mode the table does not
  know).
- **Position mapping.** JUNG counts *closedness*: the mesh level −32768 is 0 % = "Open" in the app, +32767 is 100 % =
  "Closed" (`pct = round((level + 32768) × 100 / 65535)`). Home Assistant's position is percent *open*, so
  `position = 100 − pct`: 100 = fully open, 0 = fully closed, `is_closed` at 0. Slats map the same way (tilt 100 =
  slats open). Nothing is inverted per mode: the app labels an awning "Open" / "Closed" exactly like a shutter, and
  the *Inverse operation* parameter (`0x1108`) swaps the motor relays inside the device, so it changes nothing here
  either — if your awning or roof hatch reads the wrong way round, please report which mode it is in.
- **Commands.** Open / close / stop send what the JUNG HOME Gateway sends: `Generic Move Set` with delta 0x8000
  (up) / 0x7FFF (down) / 0 (stop), acknowledged, transition time 70 s (the encoding of the 65 534 ms the gateway's
  middleware requests; the exact byte its Bluetooth chip puts on air is not captured — `COVER_MOVE_TRANSITION` in
  `const.py` if it needs changing). The app uses `Generic Delta Set` −1 / +1 / 0 instead; `JungHomeHub.delta_level`
  sends that, should Move Set turn out to be ignored. A position or tilt is one acknowledged `Generic Level Set`
  with the closedness level, as the app's sliders send it. A stop is followed by a `Generic Level Get`, because
  whether the device publishes its position after a stop is unknown. Stopping the slats sends the same stop to the
  slat element: no JUNG client does that (the app has a slat slider only), it is the plain Bluetooth Mesh meaning of
  the message.
- **End positions** (review-4 F4-16), as the app's arrows: *open* sends nothing at all while the blind reports itself
  fully open (0 % closed), *close* nothing while fully closed; the slats (*open / close tilt*, a tilt position) are
  refused while the blind is fully open, where the app disables its slat slider (*… is fully open: its slats cannot
  be moved until it is lowered*). A position not known yet skips and refuses nothing; the app also disables the
  slat slider then, but the cover offers tilt only once the mode is known and reads the position at every
  connection. **Unverified on air.**
- **Locks.** A blind held by its lock function (`0x0009`: a lock, lock-out protection or a wind alarm) ignores
  commands, and the app disables its controls meanwhile. The cover refuses them with an error that names the
  reason ("is locked", "a wind alarm holds …") when the lock state is known — the *Wind alarm* sensor reads it once
  per connection — after reading it again (a timed lock ends on its own); while it is not known, commands are sent.
- **State.** Nothing is written optimistically: position and tilt change when the element publishes or answers a
  `Generic Level Status`. A status carrying a target below / above the present level shows as `opening` /
  `closing` until the next one; both elements are asked for their level at every (re)connection.
- Attributes: `mesh_address`, `slat_address` (when the node has a slat element), `rooms`, `operation_mode`.
- The blind parameters of the app (running time, motor reversal and slat times, operation mode, behaviour and
  positions on power-up, invert direction, ventilation positions, and *Reference run* as a button) are configuration
  entities of the blind device (see [Device parameters](#device-parameters-number-select-switch-button)); the
  ventilation positions and *Time change active* are enabled by default, the rest are expert parameters. So are the
  *Lock* switch and the *Lock function* select (lock, lock-out protection, wind alarm; disabled by default). As in
  the app's Parameters tab, the slat cells follow the operation mode: *Ventilation position slats* and *Slat
  position after mains return* only for *blinds*, *Slat change-over time* not for a shutter (from 300 ms for
  blinds, from 0 ms as an awning's reversal time), and the two positions after mains return only while the
  behaviour after mains return is *Stored position*; otherwise they are unavailable. *Behaviour after mains
  voltage return* offers the app's four options (no reaction, up, down, stored position; a *stop* or *position for
  network failure* set elsewhere shows as unknown). Choosing the operation mode writes `0x1104` alone, as the app's
  Parameters tab does; the per-mode slat time the app's setup assistant writes with it (2000 ms blinds, 0 ms
  shutter, 300 ms awning) stays with the app's setup assistant — set *Slat change-over time* yourself after a change
  of mode. **Unverified on air.**
- Two binary sensors on the blind device, read once per connection: **Wind alarm** (`safety`) and **Reference run**
  (`running`, diagnostic) — see [Binary sensor](#binary-sensor).
- **All blinds**, on the mesh device, is the app's central "all blinds": one unacknowledged message moves every blind
  that listens to the blinds' device-type group `FEF6` — open and close as the levels of 0 % and 100 % closed, a
  position as its level, stop as `Generic Delta Set` 0 (after which each blind is asked where it stopped), the
  slats through their own group `FEF7` (tilt is offered when slat elements listen to it). Position and tilt are
  the means of the members', closed when every member that reported is. **Unverified on hardware**, like the blinds.
- **All blinds in &lt;room&gt;** (mesh device, one per room with blinds) is the app's area control: open, close,
  position and the slats go to each blind of the room as an unacknowledged *Generic Level Set* of its own; stop is one
  unacknowledged *Generic Delta Set* 0 to the room's group address (then each blind is asked where it stopped).

### Climate

One `climate` entity per room thermostat (product ID `0x000A`), on the thermostat's node device (the node *is* the
thermostat, so the entity carries the device's name, `Room thermostat <address>`; rename the device in Home Assistant
if you like). **Unverified on a real room thermostat**: the maintainer owns none, so everything below is implemented
from the JUNG HOME app's and the gateway firmware's decompiled behaviour (`docs/gap-analysis/control-and-state.md`
§2.7, `docs/cross-repo-analysis.md` §1.4 and D9) and tested against a synthetic export only. If you have one, please
report what works — the `RX` lines of the debug log (see [Troubleshooting](#troubleshooting)) show what the device
sends.

| Aspect | Behaviour | Mesh |
|---|---|---|
| Mode | `heat` (manual) or `auto` (the app's *Automatic operation*: the thermostat follows its own comfort / eco profile); `heat` until the thermostat reported it. The regulator has no off (frost protection is the closest thing). Selecting the mode it already has sends nothing | Vendor property `0x1246` (1 automatic, 0 manual), acknowledged Set |
| Target temperature | 5–30 °C in 0.5 °C steps, the app's slider; shown to 0.5 °C, like the room temperature (the level itself has 0.25 °C resolution: the level range maps to percent) | *Generic Level* of the set-point element: `pct = (°C − 5) / 25 · 100` mapped to −32768..32767; *Generic Level Set* (acknowledged) to change it |
| Current temperature | The room temperature the thermostat measures, 0.5 °C resolution; `unknown` until reported (or while the device says "unknown") | *Sensor Status* property `0x004F` (Present Ambient Temperature) of the thermostat's sensor element |
| Current action (`hvac_action`) | `heating` while the thermostat's heating output is on, `idle` while off — the regulator's PWM output, so it cycles every few minutes; not shown until the device answered | The thermostat's own *Generic OnOff* server (the state the JUNG HOME Gateway exposes as the thermostat's "switch") |
| Presets | `comfort`, `eco`, `frost` (frost protection), `boost` and `none`. Selecting one of the first three sends the matching preset temperature as the target (every firmware) and, on firmware 2.2.0.0 or newer, the thermostat's own mode property as the app does. `boost` heats at full power for five minutes; the thermostat ends it by itself and tells no one, so the entity reads it back five minutes after it started (and every five minutes while it still reads on). While boosting, any other preset ends the boost first, as the app offers nothing else meanwhile. `none` is what is shown when the target matches no preset temperature; selecting it ends a boost and otherwise sends nothing (there is no mode to command). While boosting a target temperature is refused (*… is boosting: choose another preset to end the boost first*), as the app disables its slider and +/- (unverified on air). The preset shown is `boost` while boosting, else the mode property when the thermostat reports one, otherwise the preset whose temperature equals the target | Preset temperatures `0x1203` / `0x1204` / `0x1205`, mode `0x120B` and boost `0x120D` — the same vendor properties the *Comfort / ECO / Frost protection temperature*, *Operating mode* and *Boost* configuration entities expose (disabled by default) |

After every (re)connection the entity asks the thermostat for its set-point, heating output and room temperature
(*Generic Level Get*, *Generic OnOff Get*, *Sensor Get 0x004F*, a few seconds after the link is up so that the hub's
own state refresh goes first), then reads the preset temperatures, the mode, boost and automatic operation the way the
configuration entities read theirs (the mode only on firmware that has it, when the version is known); afterwards it
relies on the thermostat publishing its changes (the set-point element and the sensor publish to the element group by
the app's standard wiring — whether a wheel turn or a local mode change actually produces such a publication is one
of the open questions). `homeassistant.update_entity` runs the same reads at once, boost included, so an automation
can see a boost started on the thermostat itself (unverified on air). Boost is also read every minute while the link
is up and no boost is known to run (review-4 F4-16; the app reads it every 5 s, but only while its thermostat page is
open), so such a boost shows within a minute; a read another entity made within the last 10 s counts. **Unverified on
air.** A preset temperature the thermostat has not reported is read when the preset is selected; if it still does
not answer, the selection fails. *Boost* and *Automatic operation* are also the `boost_mode` / `scheduler_enabled`
configuration switches of the node device (disabled by default; they share the values the climate entity shows).
A reported *scheduler function status* (`0x1249`) moves the automatic operation like `0x1246` does, as the app's
resolver takes it; it is also a **Scheduler function** `binary_sensor` (diagnostic, disabled by default) on the node
device, read once per connection (unverified on air).
Attributes: `mesh_address`, `rooms`, `controlled_loads` (the lights and sockets the thermostat switches through the
app's thermostat link, read from the export; see [Light](#light); unverified on air).

An **Open window** `binary_sensor` (device class `window`, disabled by default) on the node device shows the
thermostat's window-open detection: firmware property `0x1225` (`RTR_DROP_OF_TEMP_STATE`, "drop of temperature"), read
once per connection and taken from any status the thermostat publishes; non-zero is open. The app declares the state
(under another id) but never shows it, so what the value holds beyond zero / non-zero is unknown.

Not used, because nothing documents their layout or how the app would reach them: the firmware's cooling (`0x1202`,
`0x1206`), holiday temperature (`0x120E`), set-point limits (`0x1242` / `0x1243`) and sensor reading (`0x1223`)
properties (`docs/android/properties.md` §1.10).

**All thermostats**, on the mesh device, is the app's central "all RTRs": one unacknowledged *Generic Level Set* to
the thermostats' device-type group `FEF9` sets every set-point at once. Heat-only, no presets (the app's central
function sets the temperature only); target and room temperature are the means of the members'. **All thermostats in
&lt;room&gt;** (one per room with thermostats) sets each thermostat of the room with an unacknowledged *Generic Level
Set* of its own, as the app's area control does. **Unverified on hardware**, like the thermostats.

Known limitations of the spec-only implementation: the composition of a real thermostat is not captured — the entity
resolves the set-point, heating output and sensor elements per node (the node's first Generic Level, Generic OnOff and
Sensor server) and expects the app's device-type rule (OnOff + Level + ambient temperature on one element) to hold;
the thermostat's own mode property, when it reports one, is trusted as-is (whether it is published on change is
unknown, so it may lag until the next connection); the "unknown" ambient value is the SIG marker `0x7F`.

### Sensor

| Entity | Device class | Unit | Enabled by default | Notes |
|---|---|---|---|---|
| Power | `power` | W | Yes | Metered loads only (metering sockets, the energy puck's output); 0.1 W resolution, published by the meter when the value changes and asked for once after every connection |
| Voltage | `voltage` | V | No (diagnostic) | Metering sockets only; 1 V resolution |
| Current | `current` | A | No (diagnostic) | Metering sockets only; 0.01 A resolution |
| Energy | `energy` | kWh (Wh on the device) | Yes | Metered loads only; the lifetime energy counter (SIG `0x0072` on the meter element, `total_increasing` — what the Energy dashboard wants), read every 5 minutes — nothing publishes it. On an energy puck whose meter has no `0x0072`, the `0x006A` total instead (see below) |
| Energy since reset | `energy` | kWh | No (diagnostic) | Metered loads only; the counter the app shows as total consumption and its "reset consumption" zeroes (`0x006A`), same poll |
| Energy since switched on | `energy` | Wh | No (diagnostic) | Metered loads only; energy since the load was last switched on (`0x000D`), same poll |
| Power-on time | `duration` | h | No (diagnostic) | Metering sockets only; hours the socket has been switched on, read every 5 minutes — nothing publishes it |
| Installed | `timestamp` | – | Yes (diagnostic) | Metered loads only (on the energy puck from the firmware's property list, unverified); the moment the load was commissioned, a date and time the meter element keeps (JUNG firmware property `0x5014`, `hidden-features.md` §10 — the two sockets that revealed it were installed on the days it names, October 2024). Local wall time on the device, shown in Home Assistant's time zone; read once per link, never polled |
| Illuminance | `illuminance` | lx | Yes | Detectors only, on the node device — **unverified on hardware**: the Present Illuminance (SIG `0x0055`) of the detector's `Sensor Status`, published like the presence value (see [Binary sensor](#binary-sensor)) and asked for once after every connection; the device reports 0.01 lx steps (whole lux on device software up to 1.4.0.0, when the version is known), shown as whole lux. While the detector has delivered no such value (or reports all ones), the detector's own *Current brightness* instead (vendor property `0x6004`, whole lux, what the app's parameter page shows), read every minute while the link is up; attribute `source` (`present_illuminance` / `brightness`) says which. `homeassistant.update_entity` asks for the Present Illuminance at once, and for the brightness when the answer carries no reading |
| Continuous on/off | `enum` | – | No (diagnostic) | Detectors only, on the node device — **unverified on hardware**: `inactive`, `off` or `on`, whether the detector holds its load off or on through its own slider or keys (vendor property `0x6016`). The app only shows it (on the load's page), so it is read once per link and never written |
| Battery | `battery` | % | Yes (diagnostic) | Battery wall transmitters and battery mini sensors, on the node device — **unverified on hardware**: read with `Generic Battery Get` right after one of the node's keys reported an event (the node sleeps otherwise and would not answer), never polled; the level from before a restart until then; while the node reports no level (0xFF) the level its battery indicator stands for (good 50 %, low 15 %, critically low 5 %) |
| Sleep mode | `enum` | – | No (diagnostic) | Battery wall transmitters and battery mini sensors, on the node device (review-4 F4-16) — **unverified on air**: the app's *Power saving mode*: `awake` while the node was heard from (a key event, an answer, the keep-alive of a change) within the last 6 s — the app's keep-alive period — `asleep` after, `unknown` until it was heard since the start. How long a node really stays awake is not known |
| Schedules | – | – | No (diagnostic) | Every light, socket, blind and room thermostat whose element hosts the JH Scheduler (all current products): how many of the 16 schedule slots the device holds; attribute `schedules` lists them in the fields [`create_schedule`](#actions-schedules) takes (not recorded in the history). Read once per link, updated by the schedule actions; not yet tried on a real device |
| Switches off at | `timestamp` | – | No | Every light and socket: the moment the load will be off, when its last `Generic OnOff Status` said it is on, heading off, with a known remaining time (a run-on time running out, a fade to off); `unknown` otherwise. **Unverified on air**: whether a JUNG load with a *Run-on time* reports the time left this way is not known (the app ignores the field), so the sensor may stay `unknown` for good. Read-only; `homeassistant.update_entity` asks the load for its state |
| Switch-on threshold, Switch-off threshold | `power` | W | No (diagnostic) | Metering sockets only: the power level of the socket's two [thresholds](#actions-thresholds) (LBC Admin `0x5004` / `0x5005`), `unknown` while none is set; attributes `duration` (s), `enabled` and `devices` (the lights and sockets both thresholds switch, from the export's wiring). Read once per link, updated by the actions; not yet tried on a real socket |
| IP address | – | – | Yes (diagnostic) | On the gateway's node device: the address the gateway node serves over the mesh (`0xC002`, where the app finds the gateway), read once per connection; redacted from the diagnostics |
| Proxy node | – | – | Yes (diagnostic) | On the *mesh network* device: the JUNG node Home Assistant is currently connected through (known from the node's Bluetooth address the moment the link is up — JUNG nodes advertise from their MAC — and confirmed by the proxy's own Filter Status), `unknown` while disconnected |
| Link state | `enum` | – | Yes (diagnostic; off on installations that registered it before 1.1.0) | On the *mesh network* device: where the link stands, the mesh's health at a glance, the JUNG HOME app's connection states and the screens before them — `bluetooth_off` (Home Assistant has no connectable Bluetooth adapter or proxy at all; see the repair issue [No Bluetooth](#repair-issue-no-bluetooth-for-the-jung-home-mesh)), `searching` (no proxy node of the mesh in range), `connecting`, `updating` (connected, the connect-time state refresh running: the app's "the status of your devices is being updated"), `connected`, `failed` (the last attempt failed; the next follows after a back-off, the reason is in the log) and `disconnected` (the link went; the next attempt follows at once) |
| Clock offset | `duration` | s | No (diagnostic) | Every mains node with a Time Server (`1200`), on the node device: how many seconds its clock was off Home Assistant's at its last Time Status — the answer each node gives the Time Set broadcast after every connection and once a day, or the answer to the Time Get that follows the daily Time Set; `unknown` while it has not answered, or answered that it has no time. See the repair issue [devices with a wrong clock](#repair-issue-jung-home-devices-with-a-wrong-clock). **Unverified on air** |
| Last seen | `timestamp` | – | No (diagnostic) | Every node, on the node device: when Home Assistant last heard anything from it (a status, a key press, an answer, a heartbeat); kept without a link, updated at most once a minute |
| Signal strength | `signal_strength` | dBm | No (diagnostic) | Every node, on the node device: the strength of its last Bluetooth advertisement as the adapter or ESPHome proxy that heard it received it; `unavailable` without a link |
| Hops | – | – | No (diagnostic) | Every mains node, on the node device: how many relays its last heartbeat crossed on the way to Home Assistant's proxy node; only with the *Node heartbeats* [option](#options) on, `unknown` otherwise |
| Last restart | `timestamp` | – | Yes (diagnostic) | Every mains node, on the node device: the last time the node started again (its sequence numbers jumped to a fresh block: a power cut, a tripped breaker, a firmware reset), as far as Home Assistant was running to see it |
| Switching cycles, Power-on cycles | – | – | No (diagnostic) | Lights and sockets of the products whose firmware lists the counters (`0x100F` / `0x1010`, LBC Admin; read on a socket on air, `docs/hidden-features.md` §2), on the light or socket device: how often the output has switched, and how often the device was powered up, over its lifetime; read once per connection |
| IV index | – | – | Yes (diagnostic) | On the *mesh network* device: the mesh's current IV index (attributes `iv_update_active`, `transmit_iv_index`); known without a link |
| Sequence numbers used | – | % | Yes (diagnostic) | On the *mesh network* device: how much of the sequence-number space of the current IV index Home Assistant's own address has used (attribute `source`, the address) |
| Mesh sequence numbers used | – | % | Yes (diagnostic) | On the *mesh network* device: the same for the sender furthest along in the mesh (attributes `source`, `source_name`) — every sender stops at the end of the space until the mesh moves to the next IV index, which the JUNG HOME Gateway starts; the repair issue *JUNG HOME mesh sequence numbers running low* warns at three quarters |

After (re)connecting the integration asks each metering socket for its measurements once — one `Sensor Get` per
property (power `0x0081`, voltage `0x005D`, current `0x005C`): the socket's sensor server answers only
property-qualified Gets and ignores a plain "all values" `Sensor Get` (verified on air). Afterwards values update
whenever the socket publishes a change. The counters are the exception to push: the socket never publishes them,
so they are read right after the connect-time refresh and then every five minutes while a proxy link is up — one
`Generic Property Get` each, one after the other per socket and five sockets at a time, no retries (a missed read
simply waits for the next round): the power-on hours (`0x006D`, Admin server of the socket's main element) and the
three energy counters, which live on the socket's *meter* element (the one with the Sensor Server): `0x0072` on its
Manufacturer server, `0x006A` on its Admin server, `0x000D` on its Manufacturer server (`docs/hidden-features.md`
§2 — an earlier probe had asked the main element and concluded there was no energy counter). A socket answering
"unknown" (all ones) shows `unknown`. The *Energy* sensor is the lifetime counter, so it never goes backwards; the
app's own total (*Energy since reset*) restarts from zero when someone presses "reset consumption" in the app,
which HA's `total_increasing` statistics handle as a meter reset.

**Energy puck and other metered loads** (**unverified on hardware**: no puck is in the test installation). What
makes a load *metered* is the export's composition, not the product: a node with a Sensor Server element at a key
location (`0040` and up) — on anything but a detector or a room thermostat, whose Sensor Server is their own
sensor — has a meter, and the load on its primary element gets the meter entities above on its own device. A Sensor
Server on a load element meters nothing. That is the metering socket (0x03, meter element at location `0040`) and
the energy puck (0x10), whose output stays a `light` (the app makes it a lamp with the socket's consumption page,
`MeasureLampDevice`). On the puck the app reads *Power* (`0x0081`), the resettable total `0x006A` (and resets it)
and the two energy charts (`docs/android/properties.md` §4, `docs/gap-analysis/device-settings.md` §6); that much
is what the evidence gives the puck: *Power* (its meter is asked for `0x0081` alone after connecting), *Energy
since reset*, *Reset consumption* and the energy history import. The lifetime total `0x0072`, `0x000D` (*Energy
since switched on*) and `0x5014` (*Installed*) are offered too because the firmware lists them for the puck
(`properties.ENERGY_HOSTS`), but they have only been read on the metering socket. Where the puck's meter says it
has no `0x0072` — its `Generic Manufacturer Property Status` carries the property id alone, or the export shows no
Manufacturer Property Server (`1012`) on the meter element — its *Energy* sensor shows `0x006A` instead, and the
energy history import checks the charts against that counter. A puck that merely does not answer keeps waiting for
`0x0072`: `0x006A` is never above it, so switching to it after a silence and back once `0x0072` answered would put
the whole difference into one hour of the Energy dashboard. On `0x006A`, the app's "reset consumption" shows as a
meter reset, which `total_increasing` statistics handle. A metering socket always shows `0x0072`. Voltage, current,
power-on time and the thresholds remain the metering socket's: the app reads none of them on the puck, and the
properties name the socket alone.

**Energy history after a gap.** While Home Assistant is down, or the mesh link is, the *Energy* sensor has no value
and the recorder keeps no hourly statistics for it, so the first reading afterwards would put the whole gap's
consumption into one hour of the Energy dashboard. The socket's meter element keeps the two charts the app draws its
consumption page from: `0x5010`, the energy of each of the last 24 hours, and `0x5011`, of each of the last 31 days
(LBC User Property Get, big-endian samples × 0.1 Wh, newest first). When the recorder is loaded, the integration
reads them right after the connect-time counter poll — once per link, at most once an hour, the daily chart only
when the gap is longer than the hourly one reaches — and imports the missing hours into the *Energy* sensor's own
long-term statistics: every hour after the last recorded one that ended before the link came up, and beyond the
hourly chart one row per day, on the day's last hour. It never overwrites an hour the statistics already hold and
never changes the totals: every imported sum lies between the last recorded one and what the counter reads now. The
unit, the order of the samples and their alignment on clock hours and local days come from the app's decoder and
have not been checked on air yet, so each import is checked against the lifetime counter first: when the charts'
energy since the last recorded hour does not match what the counter moved (within 20 Wh + 2 %), nothing is imported
and the gap stays in one hour as before. Nothing is imported in a time zone whose offset is not a whole number of
hours (`energy_history.py`).

The socket's **Reset consumption** button (diagnostic, off by default like the two counters it zeroes) does what
the app's "reset consumption" does, in its order: an acknowledged `Generic Admin Property Set` of 0 to `0x006D`
on the main element, then to `0x006A` on the meter element (on the energy puck's output only the second: it keeps
no power-on hours). Each counter must then read 0, from the Set's Status or,
when the socket only publishes that Status, from a `Generic Admin Property Get` right after it; a socket that
keeps its value is reported as "did not reset its counter", and the energy total is not touched after the
power-on hours failed. The lifetime *Energy* counter (`0x0072`) has no reset. Not yet tried on a real socket.

The battery sensor keeps the flags of the last `Generic Battery Status` as attributes: `indicator` (`critically-low`,
`low`, `good`, `unknown`), `presence`, `charging`, `serviceability`, `discharge_minutes` and `charge_minutes` (the app
itself only shows the indicator), and `level_source`: `reported`, `indicator` or `restored`. One read per key event: a second event while a read is in flight adds nothing, and
once a read was answered the next key events do not read again for six hours; a read the node did not answer (asleep
again) is retried at its next key event.

### Event

One `event` entity per rocker or key (device class `button`), grouped in a *Push-buttons* device **per gang** — the
keys the JUNG HOME app presents as one device, named as there (a 2-gang push-button set up as two devices in the app
is two devices here as well, even when both carry the same name, each linked to the node device). A gang with a
single key exposes an entity named after the gang; gangs with several keys expose *Button A* … *Button D*. The binary
inputs of a mini actuator or puck are named as in the app: *Input E1* / *Input E2* (key `E1` / `E2`). Keys
without an app name share one device per node.

| Event type | Fired by | Attributes |
|---|---|---|
| `click` | Keys and rockers linked to the JUNG HOME Gateway (key mode *Gateway*) | `counter`, `side` (rockers only) |
| `double_click` | Same; two clicks of the same key or rocker half within 0.5 s | `counter`, `side` (rockers only) |
| `hold_start` | Same; key held down | `counter`, `side` (rockers only) |
| `hold_end` | Same; key released after a hold — or the hold ended without its release (see below) | `counter`, `side` (rockers only), `reason` (only without a release) |
| `press_on` / `press_off` | Rockers wired directly to a load or a room (key modes *Light* / *Switch*): upper / lower half pressed | `target` (mesh address the rocker addressed) |
| `scene` | Rockers wired to a scene | `scene` (scene number) |
| `dim` | Rockers wired to a dimmer while dimming (Generic Level / Delta / Move messages) | `target`, `raw` (message payload, hex) |
| `hold_start` / `hold_end` | Also rockers wired to a dimmer: a *Generic Move Set* with a delta starts a hold and one with 0 ends it; the first *Generic Delta Set* of a transaction starts one and it ends 1.5 s after the last (or at a Delta 0) — derived from the Bluetooth Mesh rules, **not yet seen on air** | `target`, `direction` (`up` brighter / `down` darker), `reason` (only without a stop) |

- Only what the mesh carries can be reported: a rocker linked to the gateway produces the four gesture events; a rocker
  wired directly to a load produces the messages it sends to that load. A rocker cannot be both.
- A double press produces a `click` for the first press and a `double_click` for the second — unless the
  [option](#options) *Report clicks only once a double click is ruled out* is on, in which case only `double_click`
  is fired.
- The JUNG firmware publishes every gateway event twice; duplicates are suppressed.
- **Every hold ends.** A hold — of either kind above — whose release or stop never arrives still gets its `hold_end`,
  with a `reason` attribute saying why: `timeout` (30 s after the hold started; nobody holds a key that long on
  purpose), `link_lost` (the Bluetooth link ended: a release could not be heard) or `stopped` (the integration
  stopped or reloaded). A release or Move 0 that still arrives afterwards ends nothing a second time. A
  gateway-mode `hold_start` while a hold runs ends that hold first with a plain `hold_end`. An automation that must
  only react to a real release checks that `reason` is absent; one that dims while a key is held stops either way —
  which is the point: the key may still be held, and a node wired straight to a dimmer may still be dimming on its
  own. Unverified on air (no lost release has been provoked on the installation yet).
- A *rocker* linked to the gateway is one element with two halves: its events carry `side` — `"up"` (upper half)
  or `"down"` (lower half); `hold_end` reports the side of the hold it ends, and a double click is two clicks of the
  same half (a click of the other half is a separate `click`). Single keys have no `side`. The attribute is also part
  of the `junghome_ble_button_action` bus event.
- Attributes: `mesh_address`, `location` (element location, `0040`–`0043` = key A–D, on a mini actuator `0040` /
  `0041` = input E1 / E2), `position` once the node's key layout is known (push-buttons and wall transmitters:
  `top` / `bottom`, `rocker`, `left_top` … `right_bottom`, `left_rocker` / `right_rocker`; see
  [Inserts and key layouts](#inserts-and-key-layouts)), and what the key drives, read
  from the export's publications the way the app shows it: `connection` — `device` (a load's element group),
  `room` (the key's own group, which the room's loads listen to), `scene` (recalls to all scenes), `gateway` (linked
  to the JUNG HOME Gateway: the gesture events above), `group` (some other group) or `none` (no function) —
  `connection_address` (the load element, the room group or the group), `connection_name` (the load's, room's or
  scene's name, when the export has it) and `connection_scene` (a scene key's number, from share exports). It
  follows the `assign_key` / `clear_key` actions at once, without a reload. A key whose device or room link
  publishes from its LBC User Property client alone is in key mode *property*, which the export cannot tell apart
  further: its entity asks the key once per connection for `0x5006` / `0x5007` (not a battery key, which sleeps), and
  a key that locks its target says `lock` (the address and name are the locked load's) with `connection_lock_seconds`
  (the lock's time limit, `0` = until unlocked) — **unverified on air**.
- A diagnostic **Key mode** sensor per key (`0x5003`, off by default: one read per key and connection) shows the
  mode the device itself holds: `light`, `move` (blinds), `scene`, `property`, `rtr` (temperature), `switch`,
  `gateway`. It is read-only; `assign_key` changes it.
- Every event is also fired on the Home Assistant event bus as `junghome_ble_button_action` with `device_id` (the
  buttons device), `entity_id` (the key's event entity), `key` (`A`–`D`, `E1` / `E2`), `type` (the event type from the table) and the
  attributes of that event (`counter`, `side`, `target`, `direction`, `scene`, `raw`, `reason`). Use an *event*
  trigger on it when one automation should handle several keys, or the [device triggers](#device-triggers) below for
  a single key. The integration fires it, not the entity: a key whose event entity is disabled keeps firing it, then
  without `entity_id`, so its device triggers and logbook lines keep working.
- A key wired to a scene additionally fires `junghome_ble_scene_recalled` with `scene` (number), `name` (the app's scene
  name), `source` (mesh address of the key), `entry_id`, `device_id` and, when the scene exists in the export,
  `entity_id` of the `scene` entity (also when the key's event entity is disabled); a scene activated from Home
  Assistant fires the same event without `device_id`. So does a recall by the app or the gateway (`source` is
  theirs). A recall Home Assistant did not hear is still reported:
  the devices publish a *Scene Status* to their group after every recall, and one naming a scene no recall reported in
  the last 5 s fires the event without `source`, with `reported_by` (the device that published it) instead; the statuses that
  follow a recall Home Assistant did hear fire nothing more. The logbook shows both events ("Living room rocker Button
  A clicked", "All off was recalled by Living room rocker").

### Device triggers

Every buttons device offers device triggers, so a key can be picked straight from the device page (*Automations → Add
automation → Device*): one trigger per key and event type, shown as *Button A clicked*, *Button A hold started*,
*Button B recalled a scene*, and so on. Only the keys the device has are offered. The subtypes are the event entity's
eight event types plus, for the four gateway gestures, one per rocker half (*Button A clicked (upper half)*,
`click_up` / `click_down`, … — 16 in all; the plain `click` matches either half), and each key offers those its
wiring produces:

| Key wired to | Subtypes offered |
|---|---|
| The gateway (`connection: gateway`, key mode *Gateway*) | `click`, `double_click`, `hold_start`, `hold_end` and their `_up` / `_down` halves |
| A load, a room or another group (`device`, `room`, `group`; key modes *Light*, *Switch*) | `press_on`, `press_off`, `dim`, `hold_start`, `hold_end` (the holds derived from its dimming) |
| A scene (`scene`; key mode *Scene*) | `scene` |
| Unknown (no connection in the export, and no key mode read or one of *Move*, *Property*, *RTR*) | all 16 |

The mode the device itself reports (the diagnostic *Key mode* sensor, once enabled and read) decides first, as it is
what the key sends now; otherwise the export's `connection`. An automation saved with a subtype the key no longer
lists (made before this filter, or before the key was rewired) is still accepted and runs whenever that event comes.
The triggers listen to the `junghome_ble_button_action` event, so they keep working when the key's event entity is
disabled. In YAML:

```yaml
triggers:
  - trigger: device
    domain: junghome_ble
    device_id: 0123456789abcdef0123456789abcdef
    type: a          # key A (e1 / e2: a mini actuator's inputs)
    subtype: click   # click, double_click, hold_start, hold_end, press_on, press_off, scene, dim,
                     # or click_up / click_down, double_click_up, … hold_end_down for one rocker half
```

A trigger for a key the device does not have is rejected when the automation is saved; while the mesh export is not
loaded the configuration is accepted unchecked so a restart never breaks existing automations.

### Scene

One `scene` entity per scene stored in the mesh, named as in the app. Activating the scene sends one *Scene Recall*
broadcast to all nodes, exactly as the app does, so every device that has the scene stored reacts. Scenes are
network-wide and not attached to a device. Attributes: `scene_number` and `members` — what each device that stored
the scene does when it is recalled (`"switch on"`, `"lightness 100% 2000K"`, …), read from the devices' JUNG *Scene
Action Setup* servers after a connection (the export only lists the members; `"stored"` until a device has
answered; not again within 15 minutes of the last complete read when the link before lasted a minute — unverified on
air). The export lists a two-channel device by its first channel whichever channel stored the scene, so every
channel is asked, and each one holding an action for the scene is a member, as in the app. `active_members` names
the members whose scene register reports this scene as its current one (read with a *Scene Get* after a connection,
on the same terms as the actions, then from the *Scene Status* the devices publish after each recall; a device that changes state since
reports no current scene). A recall from a key, the app or the gateway counts as an activation like one from Home
Assistant: the entity's state is the time of the last one. The scenes the app makes for its timers (named
`TimerScene …`) get no entity, as the app's scene list leaves them out. Scenes are created, filled and removed with
the [scene actions](#actions-scenes) — or in the app, after which the export must be loaded again. A `transition`
given to `scene.turn_on` is ignored for now: whether the devices fade a recall that carries one is still to be probed
on air (`docs/hidden-features.md` §11); once it is known, the one recall carries it for every device (**unverified on
air**).

Every load a scene can be stored on also has a diagnostic **Scenes** sensor (off by default): how many of the app's
scenes it is in, with their names as the `scenes` attribute — the export's members, narrowed on a channel of a
two-channel device to the scenes its own list names, as the app's device page lists them.

### Device parameters (number, select, switch, button)

The settings of the JUNG HOME app's *Parameters* tab are exposed as configuration entities on the device they belong to
(category *Configuration*, so they sit in the device page's configuration block, not on dashboards). Loads (switch,
dimmer and DALI inserts, actuator outputs) get *Run-on time*, *Manual switch-off during run-on time*, *Time change
active* (once per node, and only on a node with a load: a push-button with an extension or without an insert has no
lamp, socket or blind page in the app to show it on) and, on DALI inserts, *Warm dimming*; the expert parameters —
*Switch-on delay*, *Switch-off delay*, *Minimum switching repeat time*, *Switch-off warning*, *Invert switching
output*, *Dim mode* (dimmer inserts only; the app offers none on a tunable-white DALI load) — exist but are disabled
by default, exactly like the app hides them behind expert mode; enable them in the entity settings. A switch-on or
switch-off delay reads as the app shows it: a value outside 0 ms to 24 h (the factory `0xFFFFFFFF`, seen on air) is
0, one above 4 h is 4 h. Push-buttons and sockets get *LED colour (switched on)* / *(switched off)* selects with the
app's colour palette (per rocker on a 2-gang, suffixed A/B) and an *LED night mode* switch (not on a battery wall
transmitter, where the app hides it too; the *Dim mode*, night mode and *Automatic daylight saving time* entities an
earlier version created where the app has no such setting are removed at start); a 2-gang push-button or wall
transmitter also gets the app's *Synchronise buttons* as a **Synchronise LED colours** switch: on writes the left
rocker's colours to both rockers (in the app's order: `0xA001`, `0xA004`, `0xA002`, `0xA005`), and while it is on a
left-rocker colour is copied to the right rocker and the right rocker's colour selects are unavailable; off writes
nothing. A colour the device does not take fails like a colour select's does, and the switch stays off. The device
has no such setting — the app keeps the flag itself — so Home Assistant keeps it as the switch's restored state.
Every key of a push-button also gets a *Status LED*
switch, driven the way the JUNG HOME Gateway drives it (it only has an effect on keys linked to the gateway and cannot
be read back, so its state is assumed; it follows the gateway's own writes of the LED too, which the gateway sends to
the key the same way). Values are read from the device once when the entity is added or enabled — a
few seconds after the connection is up, five devices at a time (a battery device's right after one of its keys
reported, the moment it is awake) — and read again on the first connection three hours or more after that (a
battery device's at its first key event after three hours): a setting changed in the JUNG HOME app is answered to
the app only, so Home Assistant hears nothing of it. They are never polled. To see such a change at once, call
`homeassistant.update_entity` on the entity (not on a battery device: it sleeps; see
[Data updates](#data-updates)); a change is written as an acknowledged command and confirmed
by the device's reply (or read back half a second later). A change the device answers neither way fails with *did not
answer*, one the read-back shows another value for with *did not take the new value*, and one the device answers with
the property id alone (as an element answers for a property it does not have) with *does not have the setting* — like
the app, without resending or reading back. A reply or publication without a value never clears a value already
read (the app ignores it too). An entity stays *unknown* when its device does not answer. A battery device (wall transmitter, battery binary-input puck) answers only while awake:
press one of its keys, then change the setting right away. While the change runs the integration keeps the device
awake the way the app does (an `Admin Get` of its button layout, `0x5001`, whenever it was quiet for 6 s); a device
that answers neither the read of the current value nor the change fails with *it is asleep — press one of its keys
to wake it, then make the change again*, adding the app's hint that a device reacting to no key press either usually
has an empty battery (unverified on air: how long a transmitter stays awake is not known). Attributes: `mesh_address`, `property_id`. Room thermostats get theirs (the
*Comfort / ECO / Frost protection temperature*, *Operating mode*, *Boost*, *Automatic operation*, sensor selection and
offset, valve output, display settings) on the thermostat's node device next to the `climate` entity; blinds get
theirs (see [Cover](#cover)) on the blind device; detector parameters sit on the detector's node device next to its
motion / occupancy and illuminance entities — all three unverified on hardware, like the entities they sit beside.
A detector also gets a *Walking test* switch (off by default, unverified on air): on sets the test flag (`0x6001`) and
the presence control (`0x6003`) as the app does, asks for the PIR zones (`0x6005`) every second while the test runs
— attribute `pir_zones`, the triggered zones `a` / `b` (/ `c` on the ceiling detector) — and sets both back to 0
after five minutes, like the app; a test found running (started in the app) is ended five minutes after it was seen.
The firmware's constant-light and night-light properties (`0x6018`–`0x6020`) are not exposed: their layout is not
documented. Key connections and thresholds are [actions](#actions-rooms-and-key-connections).

Like the app's cells, some parameters follow another value of the device (review-4 F4-16; **unverified on air**,
no blind, detector or room thermostat here): a blind's slat cells and positions after mains return follow its
operation mode and behaviour after mains return (see [Cover](#cover)); a detector's *Brightness threshold* is
unavailable while *Daytime operation* is on, as the app disables it; a load a room thermostat switches has its
run-on time, manual switch-off, delays, switching repeat time and switch-off warning unavailable (see
[Light](#light)). While the other value has not been read, or holds a value the integration cannot name, the
parameter stays available; the other value's own entity reads it (the cover reads the operation mode at every
connection, the *Behaviour after mains voltage return* select once enabled). A detector's *Activation area* numbers take the app's detents, 0 / 25 / 50 / 75 / 100 % (a value between
two is written as the nearer one), and its *Brightness threshold* the app's 5 lx steps; *Activation area C* exists
on the presence detector only — the motion detectors have two areas, and an *Activation area C* an earlier version
created on one is removed at start.

The JUNG firmware lists more properties than the app uses (`docs/hidden-features.md` §2): *transmission settings*
(`0x0F00`, on keys and the socket's meter), the runtime statistics (`0x0F01` / `0x0F02`), *key toggle enable*
(`0x500C`) and, on the DALI insert, the hotel / basic-light / night dim values and the presentation mode (`0x1008`,
`0x1009`, `0x1011`–`0x1013`). What they do is not known, so none of them is an entity and Home Assistant never
writes them; a firmware-only property becomes a configuration entity (disabled by default) only once a supervised
probe on the devices has settled its layout and effect (`config_entities.FIRMWARE_ENTITIES`, empty today; the probe
is item C6 of `docs/on-air-sweep.md`). The one firmware-only trigger whose effect is known, `0x000E` (a dimmer
publishes all its light states at once), is not needed: Home Assistant reads the states itself. A key or socket LED
colour outside the app's palette, which the device may hold, shows as `unknown` in the colour selects.

The **device lock** (`0x0001`, one 16-bit word per node) is a switch per flag the app offers: **Lock operation**
(no operation on the device itself) and **Lock factory reset** on every device, **Key lock** and **Lock
configuration on the device** on a room thermostat. The app's encoder and decoder disagree about which bit is which;
the switches follow the encoder (bit 1 factory reset, bit 2 operation, bit 3 key lock, bit 4 configuration; the
gateway's value map agrees), and the app's own writes confirmed bits 1 and 2 on air. **Lock operation**
is enabled by default, like in the app's normal parameter list; the others are disabled by default (*Lock factory
reset* and *Lock configuration* are expert parameters; *Key lock*, normal in the app, waits for its bit to be seen on
a thermostat). On an install from an earlier version, which registered every flag disabled, *Lock operation* is
enabled at the next start unless you disabled it yourself (Home Assistant then reloads the integration once). A change
rewrites the whole word with the other flags — and the bits without a name — as last read
(read first when not known; refused while the device has not answered), one change at a time per device.

A node with a Sensor Server — the meter of a metering socket or energy puck, a detector, a room thermostat — has the app's **Sensor
values for IoT systems** switch (config, disabled by default; offered, like in the app, only with a gateway in the
project and on device software 1.3.0.0 or later): on, every Sensor Server of the node publishes its values to its
element's own group, where the gateway and Home Assistant hear them; off, it publishes nothing (`Publication Set`
0x0000). The state is read from the node once per connection, as the app reads it (a `Config Model Publication Get`
of each Sensor Server: on while one publishes to any address); until the node has answered it is the export's. A
change is Config messages and an export rewrite like the key actions, followed without a reload; the node is asked
for its publications again right after. It is planned
against what the node answered: when the node publishes and the export says it does not (the app changed it since),
switching off still sends the *Publication Set* and records it, rather than finding the export already off
(review-4 W4-6; unverified on air). The publication parameters (TTL, no period) are inferred from the app, not captured.

Every input of a mini actuator (E1 / E2, on the input's buttons device) has the app's **edge evaluation** (`0x5009`,
config, disabled by default — the app has it on the inputs' Display tab): an **Edge evaluation** switch (on = the
input's edges act, off = the input is evaluated as key presses, short or long) and **Rising edge** / **Falling
edge** selects (*No reaction*, *Switch on*, *Switch off*, *Toggle*), used in edge mode — for a switch or contact
wired to the input. All three are one byte on the device: it is read once for the three, and a change rewrites it
with the other two fields as last read (read first when not known; a change is refused while the input has not
answered). Not yet tried on a real device.

Five app parameters are Bluetooth Mesh setup states rather than JUNG properties, read and written the same way
(attribute `mesh_address` only). Every light and socket has **Behaviour after mains return** (Generic OnPowerUp:
*off*, *on*, *restore* — the app's "Switched OFF / ON / Previous state"; expert, disabled by default; a blind has
its own property for this, above). A dimmer or DALI insert has **Minimum brightness** / **Maximum brightness**
(Light Lightness Range, 1–100 %; a change keeps the other end as last read), **Switch-on brightness** (Light
Lightness Default, 1–100 %) and **Use previous brightness** (on = Lightness Default 0: switch on at the last
brightness; off = 100 %, as the app does); a DALI insert also has **Switch-on colour temperature** (Light CTL
Default, within the temperature range the light reports — the app's fixed 2000–10000 K until it has — with the
switch-on brightness, as last set or read, as its lightness, like the app, and the delta UV sent back unchanged) and
**Minimum colour temperature** / **Maximum colour temperature** (Light CTL Temperature Range, the app's expert "White
area": 2000–10000 K in 100 K steps; a change sends both ends, the other one as last read and clamped into
2000–10000 K as the app does, and a minimum above the maximum is refused; the values are the range the connect-time
refresh reads, which is also the light's own colour-temperature limits). While *Use previous brightness* is on, the
switch-on brightness and colour temperature are unavailable, as the app greys them out; turn it off first. The four
brightness entities and the colour temperature are on the app's first Parameters page and enabled by default; the
colour-temperature range is expert and disabled by default. A change to the brightness range is confirmed by the
dimmer's group publication, not a reply. The installation's DALI insert neither answered nor applied a
colour-temperature range Set when probed (`hidden-features.md` §9), so there a change fails as *not applied* after
the read-back. Not yet tried on a real device.

The app's **lock function** (`0x0009` EnforceOutput) is a **Lock** switch on every light, socket and blind (config,
disabled by default — the app has it on the device page, not the Parameters page). On sends the app's "lock":
`[02][01][time u16 LE s]`, the output keeps its current state and ignores its keys, scenes and HA until unlocked;
off sends command 0 with the priority, time and value last read, as the app does (so it also ends a wind alarm or a
lock-out protection set elsewhere). Its **Lock time limit** number next to it (seconds, 0 = no limit, up to 17999 —
4:59:59, the end of the app's H:MM:SS picker; kept by HA, not a device setting) is the time sent with the next lock;
a limit kept in minutes by an earlier version is converted. The lock state is read once
per connection — no device publishes it — in one Get shared with the light or socket, which reads it itself and
shows it as `locked` (see *Locked loads* under [Light](#light)), and read back 5 s after a timed lock should have
ended, since the device unlocks itself silently. Attributes while locked: `lock_mode` (`keep_state`,
`lockout_protection`, `wind_alarm`, `enforced_value` — the last is what a rocker's "turn on and lock" key leaves)
and `lock_time_limit` (s). A lock set from a rocker or the app shows in HA only at the next read. Not yet tried on a
real device.

A blind also has the lock functions of its page in the app as a **Lock function** select (config, disabled by
default, unverified on hardware): *Unlocked* (command 0 with the fields last read, like the switch's off), *Locked
(keep position)* (`02 01 <time>`), *Lock-out protection* (`02 FE <time>`) — both for the load's *Lock time limit* —
and *Wind alarm* (`01 FF 00 00 00 00`: move to 0 % and hold there with the wind-alarm priority, no time limit). A lock
that enforces a value of its own (a rocker's "turn on and lock") shows no option. The *Wind alarm* binary sensor and
the cover's refusal follow the same state.

Every node also has an **Identify** button (diagnostic): pressing it sends the Bluetooth Mesh *Health Attention
Set* (10 s) to the node, whose LED then blinks (verified on a push-button) — the way to tell which mini
actuator in a junction box is which, or which push-button an address belongs to, without the app (which uses
attention only while provisioning). The button sits on the device whose LED it blinks: a push-button's *buttons*
device (the keys in the wall), a socket's socket device; a node with nothing visible (mini actuators, the gateway)
keeps it on its node device. The node answers with its attention timer; a node that does not answer raises
"did not answer" (a failed write, "could not be sent").

Next to it sits the node's **Fault** binary sensor (diagnostic, device class *problem*): the Health Server's
registered faults, read after a connection with a `Health Fault Get` to every mains node (after the scene reads, in
the same chunks of five; like them not again within 15 minutes of the last complete read when the link before lasted
a minute — unverified on air) and stored in `ElementState.faults`. *Problem* while the register holds a fault, the
codes as the `faults` attribute (`0x81 (vendor)`, `0x01 battery low warning`, …); an explicit *no fault* entry (`0x00`, left
by a Health Fault Test) is not a problem; `unknown` until the node answered. JUNG nodes register the vendor codes
`0x81` (every device) and `0x80` (about half) — what they mean is unknown, the app never looks and nothing
publishes the register (`docs/hidden-features.md` §10) — so most entities show *problem* from the first read on.
The **Clear faults** button next to it (diagnostic) sends an acknowledged `Health Fault Clear` (`0x802F`; whether
JUNG nodes answer it is not observed yet) and reads the register back with a `Health Fault Get`, after which the
entity shows what registers anew (the CLI does the same: `tools/mesh_poc.py health <node> --clear`). A
node that does not answer the read-back raises "did not answer" — the Clear may still have gone through; the
next connection's survey tells. Pressing many at once can lose a Clear on the way (three of 25 pressed five at a
time read back their old register); one at a time is reliable.

### Devices and areas

| Device | Represents | Details |
|---|---|---|
| *JUNG HOME mesh &lt;uuid&gt;* (service) | The mesh network | Hosts the *Proxy node* sensor |
| *&lt;node name&gt; &lt;address&gt;* | One physical JUNG node | Identifier `node:<node uuid>`; model from the product ID — a push-button's with its insert once known, *Push-button 2-gang (DALI insert)* (see [Inserts and key layouts](#inserts-and-key-layouts)) — serial number and Bluetooth connection = the node's MAC address, linked to the mesh device; firmware, hardware revision and manufacturer as the node reports them (SIG `0x001A` / `0x0010` / `0x0011`, read once a node has a device parameter, the version once per start and again after the node restarted (a firmware update restarts it), the other two once and kept; *JUNG* until then). Hosts the entities that belong to the node as a whole: a detector's motion / occupancy and illuminance, a battery product's battery level, a room thermostat's `climate` entity, and the node-level device parameters. A thermostat's or detector's node device takes its app name and first room |
| Light / socket device | One output of a node | Identifier `<node uuid>-<element location>` (`0001` / `0002`); model *Switched light*, *Dimmable light* or *Tunable-white (DALI) light* for a light, the product name (*Socket (metering)*, *Socket*) for a socket; linked to the node device |
| Blind device | One blind / shutter / awning drive of a node | Identifier `<node uuid>-<location of the position element>`, the same scheme as a light; model *Blind / shutter drive*; hosts the `cover` and the blind parameters; linked to the node device |
| *Push-buttons* device | One **gang** of keys: the keys the app presents as one device (a 2-gang push-button set up as two devices in the app gives two of these, both linked to the same node device) | Identifier `<node uuid>-<lowest key location>-buttons`; model *Push-buttons*, with the node's key layout once known (*Push-buttons (Rocker &#124; Button)*); linked to the node device |

The first room a load belongs to in the app is the *suggested area* of its light, socket or blind device (and of a
room thermostat's or detector's node device). Home Assistant applies it on its own, once, when it first registers
the device: the device goes into the area of that name, which Home Assistant creates when there is none. Nothing is
asked, and nothing follows later: a device you moved to another area stays there, and a room changed in the app
afterwards does not move it (the room actions below place a device that has no area yet).

**Renaming a device** in Home Assistant writes the new name where the app's own rename does, so the JUNG HOME app
shows it once it loads that export (an app that never downloads the project keeps its own name until then): a
light, socket or blind device, a *Push-buttons* device, or the node device of a room thermostat or detector. The new
name goes into the app's device list of the mesh export (`meta.devices[].name`; the node's own Bluetooth name stays),
and the export is handed to the gateway, when there is one, as after every change Home Assistant makes. Nothing goes
over Bluetooth and the integration is not reloaded: the running device model takes the new name over like any
action's change (a newer export of the app's the gateway held, which the rename takes over first, included). The
rename runs as a Home Assistant background task, not one of the entry's: a reload in its place (see
[Following a change without a reload](#following-a-change-without-a-reload)) does not wait 10 s for the rename that
started it (review-4 W4-10). A name another device already has (ignoring case) gets the app's
number, e.g. a second *Lamp* becomes *Lamp 3*; afterwards the device is named as the app names it, and a later rename
in the app reaches Home Assistant with the next export it loads. The app refuses a blank name and a name with a lone
`%` sign (it takes `%` for the start of a placeholder; only `%%` and `%n` pass), and its rename takes at most 30
characters (the number it adds may go past that): such a rename stays in Home Assistant only and raises a repair
issue (see [Troubleshooting](#repair-issue-device-name-not-passed-on-to-the-jung-home-app)). Renaming the mesh device
or the node device of an actuator or the gateway is Home Assistant's alone. The same name rules apply to
`create_room`, `rename_room`, `create_scene` and `rename_scene`; the 30-character limit only to the renames, as the
app's screens for a new room or scene set none.

### Inserts and key layouts

A JUNG push-button takes any insert — switch, dimmer, DALI, blinds or an extension without a load — and its
composition does not say which; the app reads the node's *InsertId* (LBC User property `0x0002`) when it adds it and
caches it in the export. Every JUNG node also says it to anyone listening, every 1.2 s and without a key: its JUNG
manufacturer record carries its actuator function and its *button layout* (`0x5001`: which of its key elements are
keys and which rockers). Home Assistant takes them in this order (review-4 F4-12):

1. the export: the InsertId the app cached for the node (`meta.devices[].deviceId`, or the app's metadata files),
   and the layout the Android app's share export caches (`meta.buttonLayoutExports`);
2. the node's latest advertisement, from Home Assistant's Bluetooth cache at setup and from every advert it sees;
3. only for a push-button neither told about: an *LBC User Get* of its InsertId and an *LBC Admin Get* of its layout,
   read-only, once per connection until it answers (a connect-time step after the others); the answer is kept with
   what the node told about itself, so it is asked once. Unverified on air.

Where the export has no insert for a push-button (a `MeshNetwork.json` loaded without the app's metadata), the
reported one decides whether its outputs are lights or a blind — at setup when it is known by then, else from the
next reload (a log line says so); the node device names the insert at once. The node device's model names the
insert, a buttons device's model the layout, and each key's event entity gets a `position` attribute, in Home
Assistant's language. The positions of the mixed layouts (*Rocker | Button*, *Button | Rocker*) follow the documented
element order and are unverified on air. A push-button that advertises another insert than the export's raises the
repair issue [*JUNG HOME push-buttons with another insert than in the export*](#repair-issue-jung-home-push-buttons-with-another-insert-than-in-the-export);
the export's insert keeps deciding its devices until a new export is loaded. The device diagnostics show, per node,
what the export, the advertisement and an answer said (`insert`) and the decoded answers (`node_info`).

### Firmware

Every node but the gateway has a **Firmware** `update` entity on its node device (diagnostic, off by default;
review-4 F4-18, U4-11). It compares the software version the node reports (the device page's firmware, SIG `0x001A`)
with the version the JUNG HOME app 2.2.0 bundles for the node's product (`update.BUNDLED_FIRMWARE`, from
`docs/android/firmware-products.md`): *Up-to-date* when it is the same or newer, *Update available* when the node runs
an older one, unknown while the node has not reported its version or for a product the app has no image for. The
release summary says where the update comes from: **the JUNG HOME app**, which streams the image to the device over its
own Bluetooth connection. Home Assistant only compares — it never downloads, transfers or installs firmware: the entity
has no install feature, so there is no *Install* button and `update.install` is refused (a failed update can leave a
device out of the network; review-3 N9). Only the application image is compared, by product: the hardware revisions an
image is meant for, the bootloader and secure-element images and the room thermostat's STM32 co-processor image are
not (the node reports none of them). The gateway gets none: the app has no image for it, it updates itself (its version
is the *Firmware version* sensor of an entry set up from the gateway). Every node of this installation runs the version
its app bundles; an *Update available* has not been seen on air.

## Prerequisites

1. **The mesh export of the JUNG HOME app.** You can provide it in three ways; the first needs no file handling at all:
   - **From the JUNG HOME Gateway** (if you have one, firmware 2.1 or newer). The gateway holds a copy of the app's
     project file. During setup enter the gateway's address (`junghome.local` or its IP from the app under *Settings →
     Gateway*) and either its network-key password (from the app; access is granted at once) or leave the password
     empty and approve the request *Home Assistant (Bluetooth Mesh)* in the app under *Settings → Gateway → Access
     permissions → Open requests* within three minutes. Home Assistant downloads the export and keeps the gateway's
     access token so it can fetch a fresh copy later with one click.
   - **Upload**: in the app open *Project → Share via file*, send yourself the `JungHome.json` it creates (it contains
     the mesh network, Base64-encoded in its `network` field, **and** the names of your loads, buttons and scenes in its
     `meta` block), and upload it in the setup dialog.
   - **A file on the Home Assistant host**: copy `JungHome.json`, or `MeshNetwork.json` from an iOS backup (make a local
     backup of the iPhone, open it with a backup browser and extract the container of `de.jung.junghome`; the file is
     `Documents/MeshNetwork.json`), to the host, e.g. `/config/junghome/`, and enter the path.
2. **Optional: the app's metadata for names** (only needed with `MeshNetwork.json` from an iOS backup). The container
   also holds `Library/Application Support/` with `device_metadata.json` (names of loads and buttons) and
   `scene_metadata.json` (scene names). Copy that directory next to the export and enter it as the metadata directory.
3. **A Bluetooth path to the mesh.** Either a Bluetooth adapter on the Home Assistant host or an
   [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) configured with
   `bluetooth_proxy: active: true`, placed within Bluetooth range of a **mains-powered** JUNG node (push-button, socket,
   actuator). Every mains-powered JUNG node is a mesh proxy and relay, so one node in range is enough to reach the whole
   installation. The [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/) must be set up.
4. For the path source only: copy the export (and the metadata directory) to the Home Assistant host, for example to
   `/config/junghome/`.

> **Keep the export private.** Every export flavour contains the network key, application key and every device key in
> clear text. Anyone holding the file can control and reconfigure every device. Do not share it or commit it anywhere.
> Fetched and uploaded exports are stored as `<config>/junghome_ble/<mesh UUID>.json`, readable by the Home Assistant
> user only — keep that directory out of shared backups. The gateway access token stored in the config entry grants the
> same download from the gateway; diagnostics redact both, and diagnostics downloads never include keys.
>
> **Security of the gateway connection.** The JUNG HOME Gateway serves its API with a self-signed certificate, and its
> default name `junghome.local` is an mDNS name any device on the LAN could claim. The integration therefore pins the
> gateway's certificate (SHA-256) and refuses any responder that presents another one at the TLS handshake — before
> the network-key password, the access request or the stored token is sent. The pin comes from the gateway itself
> over the (AppKey-authenticated) mesh when Home Assistant is connected, otherwise from what the entry recorded at
> its last fetch, and on first contact it is learned through a bare handshake that sends nothing (trust on first
> use). A pin learned that way (or confirmed by you) is compared with the certificate the gateway node reports over
> the mesh before the integration first fetches from or uploads to the gateway; until the node has confirmed it,
> nothing is exchanged with the gateway (the log says so; devices keep working), and when the node reports another
> certificate the gateway is not used and the repair issue *JUNG HOME Gateway certificate changed* points to
> Reconfigure. If the gateway later presents a different certificate — a replaced gateway, a regenerated
> certificate, or an impostor — the same repair issue appears, and Reconfigure shows both fingerprints; continue
> only if you replaced the gateway or its certificate yourself. A pin the gateway node vouched for is not
> overridden that way: Reconfigure then refuses (a renewed certificate is reported over the mesh, so reconfigure
> while Home Assistant is connected to it). While the integration runs, a gateway that stops answering at its
> address is looked up on the mesh the way the app does it: the gateway node's own address (`0xC002`) is read and
> followed when it is an IP address or host name, and the request is retried once; a certificate reported there
> (`0xC003`) is never adopted — anyone with a node's keys can answer on the mesh — it raises the repair issue
> instead. The token Home Assistant registered is kept; when the gateway rejects it, Home Assistant asks for access
> again (its re-authentication, next to the repair issue *JUNG HOME Gateway no longer accepts Home Assistant*): the
> network-key password or an approval in the app brings a new token, pinned to the same certificate as every request.
> Exports and the `.bak` copies the room actions keep are stored with mode 0600, and the loader refuses anything
> that is not a well-formed JUNG HOME mesh export. Every other file that can hold a key is owner-only (0600) too,
> whatever the umask: the sequence-number store and its `.backup` and `.floor` siblings (a record holds the new
> network key while a key refresh is followed) and the vault; `SECURITY.md` lists them all.

## Installation

This is a custom integration: HACS installs it from this repository (a custom repository of type *Integration*; it
offers the tagged releases), or it is copied into your configuration directory by hand. The integration is
self-contained: the `jhmesh` mesh library is a regular package inside it (`custom_components/junghome_ble/jhmesh/`;
the repository's top-level `jhmesh` is only a symlink to it, for the CLI tools).

1. For a manual install take `junghome_ble.zip` from the release, or build it on a computer with this repository
   checked out: `./scripts/package_ha.sh` creates `dist/junghome_ble.zip`, a copy of `custom_components/junghome_ble/`
   under a top-level `junghome_ble/` folder. Copying `custom_components/junghome_ble/` from a checkout by hand works
   as well.
2. Unpack it so that the files end up in `<config>/custom_components/junghome_ble/` (for example
   `unzip junghome_ble.zip -d /config/custom_components/`).
3. Restart Home Assistant.
4. Add the integration:
   - **Discovery.** As soon as a Bluetooth Mesh proxy is seen, a *Bluetooth Mesh network &lt;network id&gt;* card appears
     under **Settings → Devices & services → Discovered**. Select **Add** and confirm; you are then asked for the
     export. A mesh that is already set up is not offered again: neither by its Network ID, nor during or after a
     key refresh (by the new key's Network ID once Home Assistant follows the refresh, and by the MACs of the
     export's nodes whatever they advertise). When the refresh completes, a card of the new Network ID still
     pending, or one you ignored, is removed. The key-refresh cases are unverified on air.
   - **Manually.** Go to **Settings → Devices & services → Add integration**, search for *JUNG HOME (Bluetooth Mesh)*,
     choose where the export comes from (gateway, upload, or a path on the host) and fill in the
     [configuration parameters](#configuration-parameters). The discovery card leads to the same choice.

The integration checks that the export can be read, that the chosen address is free and that at least one node of
*that* network is currently visible over Bluetooth before it creates the entry; nodes of the export visible under
another Network ID mean a stale export (see the troubleshooting entry *The network's keys were renewed after the
export was made*). Each mesh network can be added once.

## Configuration parameters

All three settings are entered in the configuration dialog and can be changed later through
[Reconfiguration](#reconfiguration). Runtime behaviour switches live in the separate [Options](#options) dialog.

| Parameter | Required | Default | Description |
|---|---|---|---|
| Gateway address | Gateway source only | `junghome.local` | IP address or hostname of the JUNG HOME Gateway. |
| Network-key password | No | – | The gateway's network-key password from the app. With it access is granted immediately; without it you approve the request in the app. Never stored. |
| Mesh export file (upload) | Upload source only | – | `JungHome.json` from *Share via file* (or `MeshNetwork.json`). |
| Mesh export file | Path source only | – | Absolute path on the Home Assistant host to the app's mesh export, for example `/config/junghome/MeshNetwork.json`. Must be the CDB JSON with a top-level `meshNetwork` object (see [Prerequisites](#prerequisites)). |
| App metadata directory | No | empty | Absolute path to a directory containing the app's `device_metadata.json` and/or `scene_metadata.json` (the iOS app's `Application Support` folder). Must be an existing directory if given. Missing files inside it are ignored. |
| Our unicast address | Yes | `0D00` | Hexadecimal mesh address Home Assistant uses as its own node, `0001`–`7FFF`. It must not be the address of any element in the export; the default lies outside the address range the app allocates. Use different addresses if more than one Home Assistant instance joins the same mesh. |

## Options

Open **Settings → Devices & services → JUNG HOME (Bluetooth Mesh) → Configure** (the entry's options). Saving reloads
the integration when something changed.

| Option | Default | Description |
|---|---|---|
| Report clicks only once a double click is ruled out | off | Off: a press fires `click` at once and the second press of a double press fires `double_click` as well, so an automation on `click` also runs on every double press. On: every `click` is held back for 0.5 s (the double-click window) and dropped when a second click arrives, so a double press fires only `double_click` — at the price of a 0.5 s delay on single clicks. A hold that follows a click within the window ends the wait early: the `click` is fired first, then `hold_start`. |
| Allow Home Assistant to add and remove devices (experimental) | off | Enables `add_device` and `remove_device`, see [Actions: adding and removing devices](#actions-adding-and-removing-devices-experimental). |
| Write Home Assistant into the network's file as a provisioner (experimental, unverified with the app) | off | See [Home Assistant as a provisioner](#home-assistant-as-a-provisioner-experimental) below. Off: every file is written exactly as without it. |
| Node heartbeats (mark a silent device unavailable) | off | On: after each connection every mains-powered device is asked (a standard Bluetooth Mesh *Heartbeat Publication* setting the JUNG app leaves off, sent with the device key once and then at most every six hours) to send a heartbeat to Home Assistant every 64 s. A device that sends neither a heartbeat nor anything else for about 3½ minutes has its entities marked **unavailable**, with a warning in the log, until it is heard again; while it is missing — or while its heartbeats have stopped although it still talks (a metering socket after a power cut keeps publishing readings) — it is asked for heartbeats again every two minutes, so a device that restarted (and lost the setting) comes back, and beats again, by itself. Off (the default): only the rule below marks a device unavailable. Switching the option off tells the devices to stop beating. |

The click option only concerns rockers linked to the gateway (key mode *Gateway*), the only ones that report
clicks. Heartbeats are small control messages (one per device per minute, relayed like everything else); battery
devices are left out because they sleep. The **Diagnostics** download lists, per device, the age of its last
heartbeat and how many relays it crossed (`heartbeats`). Verified on the maintainer's devices
(`docs/hidden-features.md` §4).

### Home Assistant as a provisioner (experimental)

**Unverified with the app: try it on a spare app install first.** No JUNG app has imported a file with this entry
yet; every rule it follows comes from the Android app's decompile, and the installations it was written for run the
iOS app. Before your own app or gateway sees such a file, export it (`junghome_ble.export_network`, flavour `share`)
and import it into the app on a spare phone or a spare app install, with the real app and gateway kept away from it.

The export lists every provisioner — each phone that set the network up or joined it — with the unicast, group and
scene address ranges it allocates from. Home Assistant sends from an address of its own that no range reserves, so
the next provisioner the app creates (a second app user) may be handed a range that covers it (the default `0D00` is
inside the range a second user gets); and a device Home Assistant added is missing from the app's next upload,
because the app never downloads the project. With this option on:

- every file Home Assistant writes (the export on disk, what `export_network` returns) and every upload to the
  gateway gets a provisioner entry *Home Assistant* — **appended after the app's own**, as the iOS library takes the
  first provisioner of an imported file for its own — with ranges of its own: 256 unicast addresses from its own
  address upwards (fewer where less is free), and the top 256 free group addresses (below JUNG's fixed groups
  `FEF5`–`FEFF`) and scene numbers. They are chosen clear of every other provisioner's ranges and every address in
  use, kept in the vault (below) and kept as long as no other provisioner's range reaches into them. The export
  also gets a node entry at Home Assistant's address, the way it records the phones' addresses, so the app treats
  that address as taken;
- rooms and scenes Home Assistant creates, and devices it adds (`add_device`, with their element groups), take their
  addresses from those ranges instead of the app's, so the app cannot allocate them a second time;
- devices Home Assistant added are put back into a file that lacks them (the app's next upload), with their element
  groups and app device rows, unless their addresses have been taken meanwhile (the log says so).

Home Assistant's address stays what the entry says. If no range fits — Home Assistant's address is inside another
provisioner's range or used by a device — or the file names no provisioner of the app's (Home Assistant's entry would
then come first), the log says so and the files are written without the entry (change the address in
[Reconfiguration](#reconfiguration)). Should the merge fail otherwise, the action stops with an error and the file
stays as it was. With the option off (the default) none of this reaches a file.

**The vault.** `.storage/junghome_ble.vault.<mesh uuid>` (readable by the owner only) keeps Home Assistant's provisioner
identity, its ranges and, whatever the option says, the device key and planned element groups of every device Home
Assistant adds — written before the device receives its Provisioning Data, so a device whose configuration then fails
can still be reached, and its addresses and groups are never handed out again — and, once recorded, what the export got
for it; and how far each such device came through the app's last key refresh, which Home Assistant carries it through
(see *A key refresh is followed* under [Known limitations](#known-limitations); the phase and the new key's Network ID,
never a key). It is local data only; Home Assistant backups include it. Every write is checked (Home Assistant's storage
only logs one that fails) and a failed one is retried by the next save; a `.backup` copy (`…vault.<mesh uuid>.backup`)
follows every write that landed and is read when the vault is missing or does not read back. A vault that does not read
back is kept as `…vault.<mesh uuid>.unreadable.<UTC time>` (one copy each time, never overwritten or deleted by the
integration) and removed from its place only once that copy is written — until then it stays untouched and the vault is
kept in memory — and the backup copy, or else a new vault, takes its place. A vault lost or set aside while the export
already names Home Assistant as a provisioner is recovered at setup: Home Assistant recognises its entry (a provisioner
*Home Assistant* whose node sits at its address with a Config Client only) and takes that identity back, so its address
is not reported as another node's. The keys of the devices it added are not recovered that way — they are in the export
once recorded.

## Reconfiguration

The devices, rooms and scenes known to Home Assistant come from the export file, which is read when the integration
loads. Export the network again and update the integration whenever you change the installation in the JUNG HOME app:
after adding, removing or re-provisioning a device, changing what a rocker controls, renaming things, changing rooms or
editing scenes, and after a key refresh (see [Known limitations](#known-limitations)).

1. Open **Settings → Devices & services → JUNG HOME (Bluetooth Mesh)**, the entry's menu, **Reconfigure**.
2. Pick a source. If the entry was set up from the gateway, **Fetch it again from the gateway** downloads the current
   project with one click (the app uploads its project to the gateway automatically after every change). Should the
   gateway no longer accept Home Assistant's access (for example after *Reset permissions* in the app), you are asked
   to approve a new request in the app. Otherwise upload the new `JungHome.json` or point at the new file. The address
   Home Assistant uses in the mesh can be changed in the same dialog.
3. The new export must belong to the same mesh network; otherwise the dialog refuses it.

A fetched or uploaded export replaces the file the integration keeps for the entry; the file it replaces is kept
beside it as `<export>.pre-reconfigure` (owner-only, it holds the mesh keys) until the next reconfigure, so rooms,
scenes and key connections Home Assistant had made — which the devices still use — can be looked up there.

The integration reloads with the new export. Devices that are no longer in the export are removed automatically,
together with their entities; devices that keep their node identity keep their entity IDs and history, because devices
are keyed by the node's MAC address, not by its mesh address. A device that has disappeared from the export can also be
deleted by hand from its device page.

## Migrating from the gateway integration

If the [JUNG HOME Gateway integration](https://github.com/ernetas/junghome) (`junghome`) is set up on the same Home
Assistant, every light, socket, key and scene exists twice once this integration is added. Since Home Assistant
2026.8 a device belongs to exactly one integration, so the two sets of devices cannot be merged — but the gateway's
*entities* can be moved over, and an entity that moves keeps its entity ID, and with it its recorder history and
long-term statistics, every automation, script and dashboard card that names it, its user-given name, icon, area,
labels and aliases. While an enabled gateway entry exists next to this integration, a repair issue *Take over the
JUNG HOME Gateway integration's entities* points here (ignore it if you want to keep both).

**What is matched.** Each entity of this integration is paired with the gateway entity that stands for the same
thing, by the device name (the name from the JUNG HOME app, which both integrations use — the gateway integration
slugifies it into its identifiers) and the entity type:

| This integration | The gateway integration's counterpart on the device with the same name |
|---|---|
| The `light` of a light device | its `light` |
| The `switch` of a socket device | its socket `switch` |
| `sensor` power / voltage / current of a socket | its sensor of the same name |
| The `event` of a key of a *Push-buttons* device | the `Up` event entity of the gateway's device for that key (`<gang name> <letter>`, the way it registers keys); on an older gang-named registration, `Up` for key A and `Down` for key B |
| The *Status LED* `switch` of a key | the *Status LED* switch of the gateway's device for that key; on a gang-named registration key A's |
| A `scene`, by the app's scene name | its scene with the same name |

Everything else has no counterpart and stays as it is: keys C and D, the *Power-on time* and *Proxy node* sensors,
the device parameters (number / select / config switches), and on the gateway's side covers, thermostats, detector
sensors, its socket *Energy* sensors (not matched with the *Energy* sensor here, which reads the socket's own
lifetime counter, see [Sensor](#sensor)) and its own connectivity sensor. A device name the gateway integration
could not tell apart (two devices with the same name) is skipped as well. Keys: the gateway integration has one
device per key element with an *Up* and a *Down* event entity and a *Status LED* switch; one event entity here
carries both directions as event types, so it takes over the key's *Up* entity and the *Down* entity stays with the
gateway (listed as gateway-only) — an automation on a *Down* entity has to be pointed at the lower half of the
moved key: a [device trigger](#device-triggers) with a `_down` subtype (*Button A clicked (lower half)*,
`click_down`, …) for a key linked to the gateway, `press_off` for a rocker wired to a load, or a trigger on the moved
event entity with a condition on its `side` attribute being `down` (see [Event](#event)). Device area / name / labels
are copied from the gateway device with the same name as the device here (a light, a socket, a gang), not from the
per-key devices.

An entity of this integration that you have already renamed, moved to an area, given an icon, labels or aliases, or
hidden / disabled by hand is left alone — its gateway counterpart stays with the gateway entry. Move entities before
customising them here.

**Steps.**

1. Keep both integrations set up and loaded; do not delete the gateway entry yet.
2. Open **Settings → Devices & services → JUNG HOME (Bluetooth Mesh)**, the entry's menu, **Reconfigure**, and pick
   **Import the entities of the JUNG HOME Gateway integration**.
3. The dialog shows the plan: which gateway entities will move onto which entities here, which are kept because
   they were customised, which have no counterpart on either side. Nothing has been changed at this point; close
   the dialog to abort.
4. Submit. Both integrations are unloaded, each matched gateway entity replaces the corresponding entity here
   (taking over its unique ID and device), the area, custom name and labels of each matched gateway *device* are
   copied to the corresponding device here (an area you already set here is kept when the gateway device has none),
   the gateway entry is **disabled** (so it does not register its entities again at the next start), and this
   integration is reloaded. The result lists what moved. Should either entry fail to unload, nothing is moved and
   every entry the import unloaded is set up again.
5. Check the moved entities, then delete the gateway entry (**Settings → Devices & services → JUNG HOME**, the
   entry's menu, **Delete**). Its remaining devices and entities go with it.

Running the import again is harmless: it finds nothing left to move. The gateway's energy sensor stays with the
gateway entry (and goes with it when you delete the entry); this integration's *Energy* sensor takes its place in
the Energy dashboard (its history starts anew).

## Data updates

The integration is **local push**. It holds one GATT connection to a JUNG node and asks that node to forward every
message it hears. Because all mains-powered JUNG nodes relay, this covers the whole installation:

- Lights and sockets publish a status message whenever their state changes, whoever changed it (a rocker, the app, the
  gateway, a timer or Home Assistant). Entities are updated from these publications, typically within a second.
- Metering sockets publish power, voltage and current when the values change.
- Detectors are expected to publish presence and illuminance the same way (unverified), and are asked for both once
  after every connection; their on/off publications to their load are taken as motion as well. Battery levels are read
  only right after a key of the battery node reported an event.
- Rocker events are decoded from the messages the rockers themselves send.
- Right after (re)connecting, the integration asks every light and socket for its state once, five at a time, then
  tunable-white lights for their colour-temperature range and their temperature element for its colour temperature
  (`Light CTL Temperature Get`, as the JUNG HOME Gateway reads it), so that the entities are filled in; a metering socket is
  asked for its power, voltage and current one property at a time (its sensor server ignores an unqualified `Sensor
  Get`), an energy puck's meter for its power. The only periodic polling is the metered loads' counters (energy, and
  a socket's power-on time), read every five minutes. `homeassistant.update_entity` on any of a metered load's power,
  voltage, current, energy or power-on sensors reads that load's meter readings and counters at once (the app reads
  them every 5 s while its consumption page is open); an update of several of its sensors together is one read.
- `homeassistant.update_entity` asks the device for a fresh value on the other entities too: a light, socket or cover
  gets the state Get of the connect-time refresh (a cover's slat element too), a room thermostat its set-point, heating
  output, room temperature, preset temperatures, mode, boost and automatic operation, a detector's illuminance its
  Present Illuminance (or its brightness), a device parameter its value — useful after changing a setting in the JUNG
  HOME app, which answers the app only. The same value of the same device is asked at most once every 2 s (an update
  of several entities together, or an automation in a loop, sends one request). A battery device is not asked (it
  sleeps; its values are read at its next key event), and without a link nothing is: the entity keeps what it shows
  and the action does not fail. The central entities (*All lights*, a room's lights) read nothing of their own: their
  members publish. The cover, thermostat and detector reads are unverified on air.
- The colour-temperature limits come from the light itself: after every connection the integration asks each
  tunable-white light for its supported range (`Light CTL Temperature Range Get`), and the light's
  `min_color_temp_kelvin` / `max_color_temp_kelvin` attributes and the clamp applied to commands follow that answer.
  Until a light has answered, the 2000–6000 K defaults of the JUNG HOME Gateway apply.
- A command to one device (a light, socket, blind or thermostat) is sent the way the JUNG HOME app sends it: an
  acknowledged mesh message, answered by the status the device publishes once it applied it (JUNG firmware confirms a
  change that way), up to three attempts of 3 s each with the same message. The action returns when that status
  arrived, and the entity shows what the device reported. A device that answers none of the attempts fails the action
  (*… did not answer the command (3 attempts in 9 s); it may not have been carried out …*); the link watchdog then asks
  the proxy node whether it still forwards anything, and only if it does is the device marked unreachable, as the app
  shows *No connection* (see [One entity is unavailable](#one-entity-is-unavailable-or-shows-an-unknown-state)); a
  proxy that stopped forwarding is dropped instead, and the devices are asked again over the next one. Commands to a
  group (a room, *All lights*, a scene) are unacknowledged, as in the app, and so are the movements the app does not
  send (a blind's open / close / stop, the hold-to-dim actions): whether a blind reports its position when it starts
  to run or only when it stops is not known, so waiting for an answer could fail a blind that works. A command fails at once only
  when no proxy node is connected (after the short grace a lost link gets) or the Bluetooth write fails (or does not
  complete within 5 s).

**Choosing the proxy node.** The integration watches the Bluetooth advertisements of the network's nodes through Home
Assistant's Bluetooth stack (local adapters and ESPHome proxies alike) and connects to the node with the strongest
signal, skipping nodes that failed within the last two minutes if there is an alternative. When the link drops —
however it ends: the node switched off, the link watchdog dropping a silent proxy, the *devices ignore Home Assistant*
repair renewing it — the entities, *All lights* and the rooms' central entities included, stay available for 20 s, and
a command sent meanwhile waits for the next link instead of failing (a load's command already out when the link went
is sent once more on the next one); only then do they become unavailable. The integration reconnects to the best node
currently visible (which may be the same one) — after one second on a link that lasted, and with a back-off from 2 s
up to 60 s after failed attempts. A link lost within a minute of connecting counts as a failed attempt: the back-off
grows, and a node that loses three links in a row that way is passed over for two minutes in favour of the next node
in range (a node that is the only one in range is still used); only a link that lasts a minute starts the back-off
over. A link lost while it is still being set up is a failed attempt too, never shown as connected. (The short-link
rule, the grace after Home Assistant's own drops and the second send of an interrupted command are unverified on
air.) If no node of the network is visible at all, it waits for an advertisement of the network (another network's
proxies in range do not wake it) and retries at least every 30 s. The *Proxy node* diagnostic
sensor shows which node is in use, the *Link state* sensor where the link stands. The wait for one
connection is the Bluetooth stack's own (Home Assistant's `bleak-retry-connector`, two attempts), not the JUNG HOME
app's 5 s: an ESPHome proxy first waits for a free connection slot and only gives up after its own timeout of at
least 10 s, so a shorter wait would give up on a slow proxy that was about to connect.

## Use cases

- **Replace or complement the JUNG HOME Gateway.** Control every light and socket, recall scenes and react to rockers
  from Home Assistant without the gateway, its cloud account or the Ethernet drop it needs — one adapter or a cheap
  ESPHome proxy in range of any socket is enough.
- **Wall buttons for everything.** A rocker that is not wired to a JUNG load can still trigger Home Assistant
  automations (`click`, `double_click`, `hold_start`, `hold_end` when the rocker is linked to the gateway), so a
  JUNG push-button can run a vacuum, a media player or a non-JUNG light.
- **Energy monitoring.** The metering sockets report power in 0.1 W steps on every change, and their lifetime
  *Energy* counter goes straight into the Energy dashboard; the power sensor itself detects when a washing machine,
  dryer or dishwasher has finished.
- **Bring JUNG lights into Home Assistant scenes, schedules and adaptive lighting.** Dimmers and DALI tunable-white
  channels accept brightness and colour temperature like any other Home Assistant light.
- **Keep the app.** Home Assistant is only an extra participant on the mesh, so family members keep using the JUNG
  HOME app and every button keeps its configured function.

## Automation examples

Entity IDs below depend on the names in your app; check them under **Settings → Devices & services**.

**Double-click a rocker linked to the gateway to run a scene**

The simplest way is a [device trigger](#device-triggers): *Automations → Add automation → Device*, pick the
push-buttons device, then *Button A double-clicked*. In YAML (the editor fills in the device ID):

```yaml
alias: Sofa rocker double click
triggers:
  - trigger: device
    domain: junghome_ble
    device_id: 0123456789abcdef0123456789abcdef
    type: a
    subtype: double_click
actions:
  - action: scene.turn_on
    target:
      entity_id: scene.movie_night
```

The same on the key's [event entity](#event), with Home Assistant's *Event received* trigger (it ignores the
entity coming back from *unavailable* after a reconnect, so nothing runs twice):

```yaml
alias: Sofa rocker double click
triggers:
  - trigger: event.received
    target:
      entity_id: event.sofa_buttons_button_a
    options:
      event_type: double_click
actions:
  - action: scene.turn_on
    target:
      entity_id: scene.movie_night
```

Use `hold_start` and `hold_end` for press-and-hold actions, or `press_on` / `press_off` for a rocker that is wired
directly to a JUNG load. One automation for many keys listens to the `junghome_ble_button_action` bus event
instead (see [Event](#event)). A `state` trigger on the event entity works too, with a template condition on
`trigger.to_state.attributes.event_type` and `not_from: [unavailable, unknown]`, so that the entity coming back
after a reconnect (the last event is restored with it) does not run the automation again.

**Notify when the washing machine on a metering socket has finished**

```yaml
alias: Washing machine finished
triggers:
  - trigger: numeric_state
    entity_id: sensor.washing_machine_power
    below: 5
    for:
      minutes: 3
conditions:
  - condition: numeric_state
    entity_id: sensor.washing_machine_power
    above: 0
actions:
  - action: notify.notify
    data:
      message: The washing machine has finished.
```

**Recall a JUNG scene at sunset**

```yaml
alias: Evening lighting
triggers:
  - trigger: sun
    event: sunset
    offset: "-00:20:00"
actions:
  - action: scene.turn_on
    target:
      entity_id: scene.evening
```

JUNG lights and sockets are ordinary Home Assistant entities, so area targets work as well, for example
`light.turn_off` with `area_id: living_room` (devices land in the area of their first app room when they are first
set up, see [Devices and areas](#devices-and-areas)).

## Actions, triggers and conditions

Lights, sockets and scenes use the standard `light`, `switch` and `scene` actions. Push-buttons offer
[device triggers](#device-triggers) and fire the `junghome_ble_button_action` / `junghome_ble_scene_recalled` bus
events described under [Event](#event). The integration registers no conditions of its own.

**Who may run them.** Every `junghome_ble` action that rewires, deletes or writes the export or the devices — rooms,
key connections, scenes (`store_scene` included: it writes every member's scene register and the export, and can
add members), schedules, thresholds, `sync_gateway`, `export_network` and adding or removing devices — is for
administrators only: a call from a user who is no administrator, or with such a user's long-lived token, fails with
*Unauthorized* (review-4 W4-9). Automations triggered by the system run with no user and are not affected; a
script or dashboard button a non-administrator starts runs as that user and is refused. Open to every user:
`get_schedules`, `audit_network`, `find_new_devices` (they only read) and the dimming actions of the lights; recalling
a scene is the `scene` entities' own `scene.turn_on`.

### Actions: rooms and key connections

The integration can change the installation itself — the same operations as the JUNG HOME app's *Areas* tab and *Key
connection* screens — through these actions (Developer tools → Actions). Every action sends the exact Config messages
the app would (device-key encrypted, to the node's primary address), checks each node's answer, and **only then
rewrites the mesh export file** the integration was configured with (atomically, keeping the previous contents
as `<file>.bak`, `<file>.bak.1` and `<file>.bak.2`, newest first). The
integration then follows the new export without a reload, so devices and the `rooms` attribute follow at once
([Following a change without a reload](#following-a-change-without-a-reload)). Nothing is written if a node refuses or stays
silent; simply retry. A plan with a message to a device Home Assistant counts as unreachable — one that left a request
unanswered, whose entities are unavailable (see
[Troubleshooting](#one-entity-is-unavailable-or-shows-an-unknown-state)) — is refused before anything is sent, naming
those devices (*Not sent: … count as unreachable*), instead of stopping at that device's first message after all its
attempts with the messages before it applied (review-4 W I5). Run it again once the device answers. Battery devices
are never counted so; a plan to one keeps it awake as below. Unverified on air. If the file changed after Home Assistant loaded it (the app wrote it, or it was edited by
hand), the action refuses with *"export from the app again"* — export, replace the file (Reconfigure), retry.

- **`junghome_ble.set_room`** — target: lights, sockets, blinds (device, entity, area or label); `room`: name of a
  room of the mesh; `create: true` creates it when there is none of that name. Without `create` an unknown name is
  refused (*There is no room named …*), so a typo no longer makes a new room and moves the devices into it
  (review-4 W4-12; before 1.1.0 every unknown name created a room).
  The device leaves every other room; keys already connected to the room start driving it. A device without a Home
  Assistant area is placed in the area of the same name.
- **`junghome_ble.add_to_room`** — the same target, `room` and `create` as `set_room`, but the device **stays in the
  rooms it is in**: a device can be in several JUNG rooms, as in the app (review-4 F4-5). It sends the same
  messages as `set_room`'s joining half (the room's subscriptions on the device's OnOff / Level servers, then the
  groups of the keys already linked to the room) and leaves nothing. A device already in the room sends nothing. The
  `rooms` attribute lists every room, and each room's *All lights in …* / *All sockets in …* entity counts the
  device. A device without an area gets the room's, as with `set_room`.
- **`junghome_ble.remove_from_room`** — target as above, `room`; takes the device out of that room only (the app's
  *remove from room*): the device stops listening to the keys linked to the room, then every model carrying the room
  drops it. Its other rooms stay; a device may end up in none, as in the app. A device a key drives through its link
  to the room (it listens to that key) is refused, naming the key (*Not taken out of room …: … is switched by …*):
  leaving the room would unwire it from the key too. `force: true` takes it out all the same; the key keeps driving
  the room's other devices. A device not in the room sends nothing. Areas are not touched.

  Both are unverified on air, and so is the app's view after it imports an export where Home Assistant put a device
  into several rooms. Check: add a light to a second room, switch both rooms' *All lights in …*, remove it from one,
  then `tools/mesh_poc.py config audit <node>` (or `audit_network`) reports no difference from the export.
- **`create_room` / `rename_room` / `delete_room`** — `name` / `room` / `new_name`; `config_entry_id` only when
  several networks are configured. Deleting a room removes its devices from it and gives keys connected to the room *no
  function* (as the app does); the devices stay operable.

  New rooms (also those `set_room` creates) and new scenes take the **highest** free group address and scene number
  of the app's range, not the lowest as the app does: the app never downloads the project and does not see Home
  Assistant's additions until it imports a file, so it gives its own next room or scene the lowest number it believes
  free — the one Home Assistant would otherwise have just taken, and on the devices both rooms would become one group.
  Comparing addresses with the app, Home Assistant's rooms therefore sit at the end of the range (`C64B`, `C64A`, …
  in a range `C000`–`C64B`). Once fewer than 64 free numbers are left below the next one, the app's own next rooms
  would soon reach it: the action refuses (*delete unused rooms, or turn on the provisioner identity*). With
  [Home Assistant as a provisioner](#home-assistant-as-a-provisioner-experimental) on, rooms and scenes go into Home
  Assistant's own ranges instead, lowest first. A scene number a key still recalls (the row a deleted scene leaves in
  the export) is never given to a new scene. Unverified on air: create a room in Home Assistant, then one in the app,
  and compare their group addresses (`audit_network`).
- **`assign_key`** — `key_entity` (the button event entity) or `key_device` + `key` (A–D, E1 / E2 for a mini
  actuator's inputs); target `target_entity` /
  `target_device` (a light, a socket, a blind, or the gateway device — a gateway-connected key reports click / double
  click / hold events to Home Assistant), `room`, or `scene` (a scene's number or name: the key recalls it on every
  device — its Scene Client publishes to all nodes, the key is told the scene (`0x5002`) and gets key mode *scene*,
  and the export records the app's `keyModeSceneConfigExports` row so the app shows it; leave `mode` empty; **not
  yet tried on a real device**); `mode` optional (`light` = on/off + dimming, `switch` = on/off, `move` = blinds —
  untested, `gateway`, `lock`, `temperature` = a room thermostat's set-point up / down, key mode 4, the app's
  *Temperature* category — **unverified on air**; rooms also `light_and_switch`). Leave `mode` empty to get what the
  app would pick (`temperature` for a room thermostat, whose `climate` entity or device is a valid target; a mode
  other than `temperature` is refused for a thermostat, and `temperature` for anything else).
  Device targets also take, **unverified on air** (written from the app's code; `docs/on-air-sweep.md` D9 captures
  the app making them):
  - `target_element` — `color_temperature`: the key's Level client alone publishes to a tunable-white light's
    temperature element (the app's *light temperature* connection, key mode *light*); `slat`: to a blind's slat
    element (key mode *move*). A light without a temperature element, a blind without slats, or a room thermostat is
    refused.
  - `mode: lock` — the app's locking function on a light or socket: the key's LBC User Property client alone
    publishes to the load's element group, the key gets *KeySetPropertyMode* `0x5006` = (`0x0009` lock function,
    stateful), up / on `0x5007` = lock the current state (`02 01 <s>`), down / off `0x5008` = unlock (`00 01 00 00`),
    each confirmed, then key mode *property* (3); the load is then asked for its lock. `lock_seconds` (0–65535, empty
    or 0 = until unlocked) is the lock's time limit. Blinds (lock-out protection, wind alarm), room thermostats and
    rooms are refused. The app keeps no `meta` row for such a link; the key itself holds it.
  A socket or mini-actuator target also gets the app's *property user* wiring: the User Property servers of all its
  elements (a mini actuator's inputs included) publish to their element groups and the key's clients listen there
  (unverified on air for a mini actuator).
  A **detector** is a source too, as in the app's "What should the detector control?" (review-4 F4-16, **unverified
  on air**): give one of its entities (its motion / occupancy sensor) as `key_entity` or its device as
  `key_device`, and a device target; the detector's on/off client then publishes to that device as a key's would.
  A detector has no key mode, so none is written (nor is its property mode reset), and it cannot drive a room or a
  scene (*Detector … can only drive one device*). For the same reason it only drives a light, a socket or a blind in
  the mode the target gives (`light`, `switch`, `move`): `lock`, `temperature` (a room thermostat), `gateway` and a
  `target_element` live in a key's key mode or property mode and are refused for a detector (*Detector … cannot
  drive a target in mode …*).
- **`clear_key`** — the app's *No function*.

A key of a **battery device** (wall transmitter, battery binary-input puck) only answers while its device is awake:
press one of the device's keys, then run `assign_key` / `clear_key` right away. The device's own messages are sent
first, and while the action runs the integration keeps it awake as the app does (an acknowledged `Admin Get` of its
button layout, `0x5001`, whenever it was quiet for 6 s; retried 1 s after one went unanswered). A device that does
not answer fails the action with *it is asleep — press one of its keys to wake it, then run the action again*;
asleep at its first message, nothing was applied (unverified on air, like the keep-alive itself).

Requirements and limits: the target must have been wired once by the app (its *element group* must exist — always true
for app-provisioned devices). Room connections are only recorded in the file with the app's `JungHome.json` share
export; with an iOS `MeshNetwork.json` the mesh is configured but the app will not show the link and later `set_room`
calls will not wire new members to that key (re-run `assign_key`). Each successful action is followed without a
reload: no entity goes *unavailable*. When the entry was set up **from the gateway** (or fetched from it once), every
rewritten export is also handed to the gateway, exactly as the app does after each of its changes (`POST
/api/junghome/config {"data": {"project_file": …}}`), so the gateway shows what Home Assistant changed. The app does
**not** download the project from the gateway (`docs/android/network-logic.md` §6): its next upload lacks Home
Assistant's changes, which the nodes still hold. Home Assistant keeps a copy of the app's last upload beside the export
(`<export>.app`) and carries its own changes — the difference between that copy and its file — over onto the app's
next upload before adopting it, then hands the result back to the gateway. Where the app changed the same thing (it
re-linked the same key Home Assistant had linked), the app's version is kept, a warning names it and the repair issue
[*The JUNG HOME app overrode a change Home Assistant made*](#repair-issue-the-jung-home-app-overrode-a-change-home-assistant-made-on-)
lists each such entry. Rows are matched by what identifies them — a node by its UUID, a room by its address, a key's
mode, scene and load rows (`buttonLayoutExports`, `keyModeSceneConfigExports`, `actuatorExports`) by the element's
address — so a key both sides set keeps one row, the app's. The same carry-over runs when Home Assistant's own file
changed since the last sync too (a change whose upload never reached the gateway, then a change in the app): the
merged file replaces the old one, which is kept as `<export>.pre-adopt` until the next takeover, and goes up to the
gateway. Only an entry without the copy of the app's last upload (or with one that does not load) still refuses such
a change with *holds a newer export*. A gateway holding exactly what the file holds counts as in sync whatever Home
Assistant recorded (an upload whose record was lost to a restart no longer blocks every change). Unverified on air:
the merge has not met the iOS app's import of such a file yet. The app itself keeps showing its own view until it is re-imported. Should the upload fail, the change on the mesh and in the file
stands, a repair issue *"JUNG HOME export not handed to the gateway"* appears (one per entry), and the upload is
tried again twice, 15 s apart, in the background — as the app retries its own (a gateway that could not be asked or
refused the upload: unreachable, busy, an HTTP error; not a refusal of Home Assistant's own, such as a gateway holding
changes it has not seen), also when the change reloaded the integration (one it could not follow in place). The
next change's upload, or
**`junghome_ble.sync_gateway`**, replaces a pending retry; the
action retries on demand (also useful after editing the file by hand). The time of the last upload that went through
is the gateway device's *Last export upload* sensor (diagnostic, off by default; the app's "last change"). It and
the content digest of what was last synced are kept in `.storage/junghome_ble.<entry id>.gateway_sync` (a digest and
a time, no key material), no longer in the config entry: a sync no longer rewrites the config entries file. Version
1.0.0 kept them in the entry; the first start takes them over and leaves the entry's copy as it was, so going back to
1.0.0 finds the values of the upgrade — its first change may then ask to fetch the export again. Nothing is uploaded to a
gateway whose certificate the gateway node has not confirmed (see the security note under
[Prerequisites](#prerequisites)); a gateway that rejects Home Assistant's token raises *"JUNG HOME Gateway no longer
accepts Home Assistant"* instead and Home Assistant asks for access again (re-authentication). An entry set up from a
file is not synced: reconfigure it from the gateway to enable this, or re-import the file in the app — otherwise the
next app change overwrites what Home Assistant did.

### Following a change without a reload

An action that changes the export — the room, key, scene, threshold and *Sensor values for IoT systems* actions, a
device renamed in Home Assistant, and the export an entry set up from the gateway takes over for a device it did not
know — is followed by the running integration in place (review-4 D23): the export is read again as the setup reads
it, every platform builds its entities from it, and the change is carried over to the entities that already run —
new ones (a new scene, a room's *All lights in*, a new device's) are added, those the export no longer gives are
removed, and every other one takes its new name, `rooms`, members or connection while keeping what it learnt over
the link. No entity passes through *unavailable* or *unknown*, so a `state` trigger without `from:` does not fire
for it; the Bluetooth link stays up, and nothing is read from the devices again but what the change itself touched
(the scene actions of a scene action, a node's sensor publications, a new device's state). Devices and their names
follow in the device registry as after a reload.

The integration still reloads — a few seconds of *unavailable* — when it cannot follow with confidence: adding or
removing a device with Home Assistant (`add_device`, `remove_device`), an export of another mesh or with other
network keys, a device the export dropped, moved to another address or gave another key, Home Assistant's address
taken by a device of the export, an entity whose element changed, and any error while following; it logs why at
DEBUG (`custom_components.junghome_ble.model_update`). Saving the options, a reconfiguration and a change of the
data the entry was built from reload as before. Unverified on air.

### Actions: scenes

Scenes are edited the way the app edits them (`docs/android/network-logic.md` §4.3): a device stores its *present*
state under the scene number (*Scene Store* to its Scene Setup Server — what a later *Scene Recall* restores),
together with the JUNG description of that state (*Scene Action Setup Set*: switch on/off, lightness, lightness +
colour temperature — what the app shows for the member), and the export records the member. So set the lights the
way the scene should leave them, then store:

```yaml
action: light.turn_on
target: { entity_id: light.kitchen_table }
data: { brightness_pct: 100, color_temp_kelvin: 2000 }
---
action: junghome_ble.store_scene
target: { entity_id: [light.kitchen_table, switch.kitchen_led] }
data: { scene: "Dinner" }
```

- **`junghome_ble.create_scene`** — `name` (`config_entry_id` when several networks are configured). Creates an empty
  scene in the export, nothing goes on air; with *Response* on, answers `{"scene": <number>, "name": …}`.
- **`store_scene`** — target: lights / sockets / blinds / thermostats (device, entity, area or label); optionally
  the state to store — `action` on / off, `brightness_pct`, `color_temp_kelvin`, `position` / `tilt_position`,
  `temperature` — in which case every load is first set to it and asked until it reports having arrived (dimmers
  ramp; the load's own rounding is what is stored; a load that never gets there fails the call and nothing is
  stored); a blind takes too long to move and is not moved: its JUNG description carries the `position` (and
  `tilt_position`, else the position again) given; `scene`: name (as the app shows it) or
  number. Each device stores its present state; a device whose state Home Assistant does not know yet is stored
  without the JUNG description (the recall still works) — except a channel of a multi-channel device, which is
  refused: without its description, taking the other channel out of the scene would drop it too.
- **`remove_from_scene`** — same target and `scene`: the devices forget the scene (*Scene Delete*, description
  removed) and leave the export's member list and its `sceneInfo` rows. A key of those devices wired to recall the
  scene (a scene-mode key whose link the export records) is cleared first, as the app does.
- **`rename_scene`** — `scene`, `new_name`; export only.
- **`delete_scene`** — `scene`: the members' keys wired to recall it are cleared, every member forgets it, then it is
  removed from the export. `force` (the app's *Delete anyway*): a member that cannot be reached or refuses is
  skipped — it keeps the scene in its register — and the scene is removed from the export all the same. With
  *Response* on, answers the skipped members (`{"skipped": ["0232"]}`). A skipped member would join every recall of
  a new scene with the same number, so Home Assistant holds that number (kept in
  `.storage/junghome_ble.<entry id>.held_scenes`, numbers and addresses only): `create_scene` gives it to no new
  scene, and the repair issue [*Devices still hold a deleted JUNG HOME scene*](#repair-issue-devices-still-hold-a-deleted-jung-home-scene-on-)
  names the members until `delete_unused_scenes` deletes it from them (review-4 W4-8). The app does not know the held
  numbers and may still give one to a scene of its own.
- **`delete_unused_scenes`** — reads every device's scene register and lists, or deletes, the scene numbers the
  export does not know (neither a scene nor a timer's scene of the app): what the app does, device by device, each
  time its timer list opens. With *Response* on, answers the numbers per register (`{"0148": [5], "unanswered":
  ["0300"]}`); a device that does not answer is left alone. A delete a device does not carry out stops the action;
  its error lists the numbers already deleted before it. Since the judgement is by what the export *lacks*, a stale
  export would delete the app's newer scenes and timer scenes from every device while the app still lists them
  (review-4 W4-3), so:
  - `dry_run` (default **on**): only the *Scene Register Get*s go out, nothing is deleted; the answer lists what
    would be. Run the action with `dry_run: false` to delete.
  - An entry set up from the **gateway** asks the gateway for its current export first (taking it over when the app
    changed something) and refuses — dry run included — when the gateway does not answer or must not be asked; it
    never falls back to the copy on disk here. Unverified on air: a scene made in the app, then a dry run, should
    not list that scene's number.
  - An entry set up from a **file** deletes only with `confirm_stale_export: true` (you vouch that the file holds
    every scene of the app, including those made since the export) or with `numbers`, the scene numbers to delete.
  - `numbers` restricts the call to those numbers in any case; a number that is a scene of the export is refused
    (`delete_scene` deletes those).

  A real run also lets go of a held number (see `delete_scene`) once the member's register no longer holds it.

Before a *Scene Store*, the device is asked whether it has room, as the app does: a device's register holds 16
scenes (the app's timer scenes included), a channel of a two-channel device keeps its own list of 8. A full one
fails the action with *no room for scene …* before anything is sent; a scene the device already holds can always
be stored again.

Every device answers *Scene Store* / *Scene Delete* with its Scene Register (verified on air: the JUNG firmware
replies by unicast and also publishes the register to the element's group) and the description Set with a status; a
device that stays silent is read back, and one that refuses (*Scene Register Full*, a description not taken) aborts
the action before the export is written. Each storing / removing action is followed without a reload: a new
scene's entity appears, a deleted one's goes, and the scene members are asked again what they do, so the scene
entities' `members` follow. A blind stores its position and slat levels (an awning without slats repeats
its position), a thermostat its set-point — the app's *blinds and slats position* / *target temperature* actions;
both are unverified on hardware.

Each stored member also gets the row the app shows its stored values from (`meta.sceneInfo`, review-4 F4-6): one
per scene and app device, rewritten in place when the device is stored again, with the values the load settled on in
the app's units — `lightness` in percent (a switch or socket: 100 on, 0 off, the only on / off the app's import reads
back), `colorTemperature` in Kelvin (100 K steps, 2000…10000), `blindPosition` / `slatPosition` in the JUNG percent
(0 open … 100 closed), `temperatureValue` in °C. A device stored without its state known gets no row (an old one is
dropped), and a load the export has no app device for (`meta.devices`) none at all. The row copies the device's
`deviceId` as the file holds it and the layout of the rows already there, so a file the app wrote stays its own but
for the new rows. **Unverified with the app:** the shapes come from the Android app's decompile
(`SceneInfoRepositoryImpl`, `Infos`); no app has been seen importing a row Home Assistant wrote, the iOS app's import
of these rows not at all — try it on a spare app install first ([on-air sweep](on-air-sweep.md), section F).

### Actions: schedules

JUNG loads run schedules themselves, on the time Home Assistant sends them (see [Data updates](#data-updates)):
the app's *Automation* page, up to 16 slots per load, each a time of day, sunrise or sunset trigger on a set of
weekdays with what the load then does (`docs/gap-analysis/network-features.md` §4.1). They keep working without
Home Assistant and the app shows them; nothing about them is in the export, so these actions reload nothing. Not
yet tried on a real device.

```yaml
action: junghome_ble.create_schedule
target: { entity_id: light.kitchen_table }
data: { trigger: sunset, offset: -15, not_before: "18:00", brightness_pct: 60, color_temp_kelvin: 2700 }
response_variable: created   # {"light.kitchen_table": {"slot": 0}}
```

- **`junghome_ble.get_schedules`** — target: lights, sockets, blinds, thermostats. Answers (response only)
  `{entity_id: {"schedules": [...]}}`, each used slot with `slot`, `trigger`, `enabled`, `weekdays`, `time` (timed)
  or `not_before` / `not_after` / `offset` / `effective_time` (the sunrise / sunset time the node computed), and the
  action fields below. The *Schedules* sensor shows the same.
- **`create_schedule`** — same target; `trigger`: `time` (with `time`) or `sunrise` / `sunset` (with optional
  `not_before` / `not_after`, the window it is kept in, and `offset` in minutes, −128…127); `weekdays` (every day
  when left out); `enabled` (default on). What the load does: `action` on / off for switches and sockets, plus
  `brightness_pct` for dimmers (100 % when on without one) and `color_temp_kelvin` (100 K steps) for tunable white;
  `position` / `tilt_position` (0 closed … 100 open; the slats follow the position when left out) for blinds;
  `temperature` (5…30 °C) for thermostats. A field that does not fit a targeted load refuses the whole call before
  anything is sent, and so does a targeted load without a free slot. Each load takes it into its first free slot,
  written inactive and made active once its action is in; a sunrise / sunset schedule is preceded by Home
  Assistant's home location (latitude, longitude, elevation), sent to the node's Location Setup Server as the app
  sends the phone's. With *Response* on, answers `{entity_id: {"slot": n}}`.
- **`update_schedule`** — target, `slot` (0–15) and the fields of `create_schedule`, all of them given again (a
  field left out takes its default, nothing is kept from the slot): rewrites that used slot in place, as the app's
  edit does (review-4 F4-6) — location first for a sunrise / sunset one, the schedule inactive, its action, then
  active. A free slot, or one the app did not write, is refused. When a write fails, the slot's old schedule and
  action are written back (a warning in the log says when even that is not taken: the slot is then left inactive).
  Calling it again with the same fields is harmless. Unverified on air.
- **`enable_schedule`** / **`disable_schedule`** — target and `slot` (0–15): the slot fires again / stays in place
  without firing.
- **`delete_schedule`** — target and `slot`: frees the slot.

Every write is confirmed from the load's answer, or read back when it stays silent; a load that did not take it,
has no free slot, or holds nothing in the slot named fails the action (a new schedule that was not fully taken is
freed again; it goes in inactive, so it never fires with a slot's old action). Slots a central scheduler owns are
left out, as in the app. To change a schedule in place, `update_schedule` it.

### Actions: thresholds

A metering socket can switch other loads by itself, the app's *Automatic* profile (`docs/gap-analysis/network-features.md`
§4.3): when its power stays at a level for a while, the *switch-on* threshold switches them on, the *switch-off*
threshold off — say, the TV's socket switches the soundbar and the lamp behind the screen off when the TV has
been in standby (under 5 W) for five minutes. The socket does this without Home Assistant, and the app shows it.
Not yet tried on a real socket.

```yaml
action: junghome_ble.set_threshold
target: { entity_id: switch.living_room_tv }
data:
  threshold: switch_off
  power: 5
  duration: 300
  devices: [switch.living_room_soundbar, light.living_room_backlight]
```

- **`junghome_ble.set_threshold`** — target: metering sockets; `threshold`: `switch_on` / `switch_off`; `power` (W,
  0.1 W steps) and `duration` (s, up to 65535), each left as the socket holds it when omitted; `enabled` (a disabled
  threshold keeps its level; left out, the threshold keeps its state, a new one is on); `devices`: the lights and
  sockets **both** thresholds switch — it replaces the current list, left out it stays. The threshold is an LBC
  Admin property Set (`0x5004` / `0x5005`), confirmed from its Status or read back, and goes out first, as in the
  app; `devices` is wiring the app does the same way (seen on air): the socket's OnOff Client (on its meter element)
  subscribes to that element's group and publishes there, then each load subscribes its JUNG User Property Server
  (`0x0527:1013`) and its OnOff server to it, so a change of `devices` rewrites the export, followed without a
  reload like the key actions. A `devices` list the integration cannot wire (a load that is no light or
  socket, a meter element without its group) is refused before anything is written, so the socket keeps its
  threshold as it was.
  Disabling a threshold (`enabled: false`, no `devices`) while the socket's other one is not active either unwires
  the loads as the app's disable does: each leaves the group (OnOff server, then `0x0527:1013`), and the OnOff
  Client's publication is reset (`0x0000`, then the group again). Enabling it again later needs `devices` once
  more; while the other threshold is active, or when the socket does not say, the loads stay wired. Only a call
  with `enabled: false` disables (the app's toggle): a new `power` or `duration` for a threshold that is already
  disabled is written and leaves the loads wired.
- **`delete_threshold`** — target: metering sockets. Clears both thresholds (no level, not active, as the app's
  delete does), then unwires every load they switched and resets the OnOff Client's publication the same way.
  The app also writes KeyMode 5 to the meter element around these steps; that element holds no such property (it
  answers without a value), so Home Assistant leaves it out.

Both actions go socket by socket, each its threshold(s) first, then its wiring. A failure names what the call
already wrote (*Before it, socket 0172 was set as asked and the switch-on threshold of socket 0180 was written*), not
"nothing before it was applied" (review-4 W4-13). `set_threshold` checks every socket's `devices` and reads the values
it keeps before it writes the first one, so a refused call leaves every socket as it was.

### Actions: network audit

The export says how every device is wired; the devices themselves are what counts, and nothing on the mesh ever
reports that the two drifted apart (a Config message that never arrived, a device re-added by hand). The audit
asks, with device-key *Gets* only — it never changes anything — and compares:

- per device: Relay (with its retransmit), Network Transmit, Default TTL, Secure Network Beacon, GATT Proxy and
  Friend against the export's node entry (a state the export does not record is shown, not compared);
- per device, the keys it holds (review-4 F4-15): *NetKey Get*, and *AppKey Get* for every network key the export
  gives it, against its `netKeys` and `appKeys` — by index only: the answers carry no key, and no key is compared or
  shown;
- per model (all but the Configuration Server): its publication, its subscriptions and its bound AppKeys against
  the model's `publish`, `subscribe` and `bind`.

```yaml
action: junghome_ble.audit_network
data: { device: 8b1f0c… }   # optional: a light, key, socket or node device; the mesh device or nothing = every device
response_variable: audit
```

- **`junghome_ble.audit_network`** — optional `device` (only the node behind it) or `config_entry_id`. Answers
  (response only) `nodes` — per node address its `name`, whether it `answered`, its `settings` (`export` / `node`
  value each; transmits as `count` transmissions `interval` ms apart), its `keys` (`net_keys` / `app_keys`, the
  `export` and `node` indexes each), the number of `models` checked and its `findings` — plus `unanswered` (the silent nodes), `findings` (their total) and `skipped` (battery devices: they
  sleep, so the network-wide audit leaves them out; name one as `device` to ask it anyway). A finding has a `kind`
  and, as it applies, `element`, `model`, `setting`, `expected` (what the export has and the device lacks) and
  `actual` (what the device holds instead): `setting_differs`, `setting_unanswered`, `node_unanswered`,
  `publication_differs`, `subscriptions_missing`, `subscriptions_extra`, `app_keys_unbound`, `app_keys_extra`, and
  `publication_` / `subscriptions_` / `app_keys_` + `unanswered` or `refused` (the device answered with an error
  status although the export expects something), and for the keys `keys_missing` (an index the export gives the
  device and it lacks), `keys_extra`, `keys_unanswered` and `keys_refused`, each with `setting` `net_keys` or
  `app_keys`. `scene_subscriptions_missing` is the one expected on a healthy
  installation: the export lists room and device-type groups on the Scene (Setup) Servers that the app never sent
  to the devices; scenes are recalled to all devices, so they do no harm. The gateway's GATT Proxy shows as a
  `setting_differs` too: its export entry says *not supported*, it runs one.

A device takes three Gets per model: about a hundred for a push-button, sent five at a time like the state refresh
after a connection, so auditing every device takes a few minutes. A device that answers none of the eight
device-wide Gets (six states, two key lists) is reported unanswered and not asked about its models. The last result per device stays in the
diagnostics until the integration reloads or follows a changed export. Not yet run on the installation; the CLI's earlier `config audit`
(publications and subscriptions only) was, with the results in `docs/hidden-features.md` §9. The Friend and key
Gets are unverified on air.

**Locating a device.** `junghome_ble.locate_node` (administrators only) has one device advertise its *Node
Identity* — the Mesh Proxy advertisement that names the device by a hash only this mesh's keys resolve — instead of
the network's, so a Bluetooth scanner (Home Assistant's Bluetooth advertisement monitor, a phone app) tells its radio
from the others: a Config *Node Identity Set* with the device key, and the same Set off after `duration` seconds (5
to 60, default 60; a device stops by itself after 60 s whatever happens, so an off lost on the way leaves nothing
running). It changes nothing else.

```yaml
action: junghome_ble.locate_node
data: { device: 8b1f0c…, duration: 30 }   # any of the device's devices; answers {node, seconds}
```

A device that answers *not supported*, refuses or does not answer fails the action and gets no off; a battery device
is asleep and answers only while awake. Unverified on air.

A hop matrix (how many hops every device is from every other) is not offered: measuring it needs Heartbeat
Publication and Subscription *Sets* on every pair of devices (`tools/mesh_poc.py config hopmatrix`,
`docs/hop-matrix.md`), and the heartbeat option's publications only reach Home Assistant — their hop count to
Home Assistant is in the diagnostics (`heartbeats`).

### Actions: gateway access requests

Another program that talks to the JUNG HOME Gateway — the gateway integration next to this one, a script — asks the
gateway for access, and the request waits to be approved in the app (*Settings → Gateway → Access permissions →
Open requests*; the *Access requests* sensor counts them). `junghome_ble.approve_gateway_client` approves one from
Home Assistant instead (review-4 F4-17), for an entry set up from the gateway:

```yaml
action: junghome_ble.approve_gateway_client
data: {}                    # lists the waiting requests: {approved: null, waiting: ["ioBroker"]}
response_variable: requests
---
action: junghome_ble.approve_gateway_client
data: { client: ioBroker }  # approves that one: {approved: "ioBroker", waiting: []}
```

- **Administrators only, and only explicit.** An approved client gets the gateway's whole API — the network export
  with every mesh key included — so nothing is approved without a `client`, and only a name the gateway lists as
  waiting right now (`GET /api/junghome/config`, `api_client_name_asking`), spelt exactly as listed; anything else is
  refused and nothing is sent. Approve only a request you started yourself.
- **The export's rules.** The gateway is asked over the pinned connection, only once the gateway node vouched for the
  pinned certificate, and not while it rejects Home Assistant's own token; a rejected token or another certificate
  raises the same repair as an upload (`POST /api/junghome/config {"data": {"api_client_accept": "<name>"}}`, the
  app's own request). Neither the token nor the answer bodies are logged.
- **Left to the app, on purpose:** revoking access (*Access permissions → reset*, `api_client_reset`: it revokes every
  client, Home Assistant's own token included) and the gateway's network settings (a wrong address makes the gateway
  unreachable for the app and Home Assistant alike).

Unverified on air.

### Actions: adding and removing devices (experimental)

**`junghome_ble.find_new_devices`** lists the JUNG devices nearby that are not in a network yet (they advertise the
Mesh Provisioning Service 0x1827): Bluetooth address, Device UUID, product id, signal. **`junghome_ble.add_device`**
(`address`, `name`, `static_oob`; administrators only) adds one the way the app does, and only when the entry's option
*Allow Home Assistant to add devices* is on (it is off by default):

1. the name is checked first, as the app checks one — not blank, at most 30 characters, no `%` sign but `%%` and `%n`
   — and numbered as the app numbers a name another device already has (`Hall light` → `Hall light 2`; the action's
   answer says the name used); a node of the network with the same product becomes the *template* — the first device
   of a product has to be added with the app; for a push-button, one with the insert the device advertises when the
   export has one;
2. the device gets addresses above every provisioner's range (no app allocates there), planned on the export as it is
   now (the gateway's, adopted first, when the app changed the network since the last reload), its element groups
   at the top of the app's group range like the rooms Home Assistant creates (the app does not see them until it
   imports a file, and gives its own next room the lowest free group; refused when fewer than 64 free addresses are
   left below them) — with the option
   [Home Assistant as a provisioner](#home-assistant-as-a-provisioner-experimental) on, inside Home Assistant's own
   range, its element groups in Home Assistant's group range — and never the addresses or element groups of a device
   Home Assistant added before, recorded or not (see *pending devices* below). It is provisioned over PB-GATT (the
   export's NetKey and the mesh's IV state; `jhmesh.provisioning`, checked against the specification's sample
   data) with the strongest method the device offers — see *Provisioning methods* below — within the app's 30 s for
   the whole provisioning, only once a network beacon on the current Bluetooth connection confirmed that IV state (otherwise the action
   asks to try again after the next connection: the device would keep a wrong one), and is refused before it learns
   an address when its element count differs from the template's, and not at all while the app's key refresh is in
   Phase 1 (the device would get the key being retired; in Phase 2 it gets the new one); its device key and planned
   element groups are written to the vault before the device receives its Provisioning Data (the key is derived one
   step earlier; *unverified on air*): when that write does not land, provisioning stops there, the device still
   new, with the repair issue
   [*JUNG HOME device keys cannot be saved*](#repair-issue-jung-home-device-keys-cannot-be-saved). Once it is
   provisioned, Home Assistant forgets what its replay protection remembered for the new addresses
   (a device reset since may have sent from them; the new one starts its sequence numbers from 0);
3. the app's post-provisioning sequence goes out through the proxy link (`jhmesh.commission`; review-4 F4-13), each
   step's status checked: AppKey, then *Composition Data Get* — the device's own composition, which plans the rest
   by the app's rules and must be the template's (another company, product, element layout or model list, or one
   without the servers the app requires, is refused before anything is bound) — the bindings, the app's relay / TTL
   / transmit settings, a push-button's InsertId (refused when it carries another insert than the one advertised,
   or the template's: its element groups and device-type groups would be another device's), *Time Set* to its Time
   Server, an element group for each element with a supported server (each of them publishing and subscribing to
   it, the Sensor Server and the LBC Admin server left out as on the installation's devices), the device-type groups
   of its class (lamps `FEF5`, blind position `FEF6`, slats `FEF7`, sockets `FEF8`, room thermostats `FEF9`) and a
   puck's time keeper group `FEFF` — within 3 minutes. A failure here, or one of the provisioning after the device
   received its data, sends the device a *Config Node Reset* with its key, as the app does: once it confirmed, it
   is a new device again, nothing stays reserved, and the error names the step it stopped in
   (*… could not be configured at step …*); unconfirmed, it is *pending* (below). *Unverified on air*;
4. the node's configuration is read back (the audit's Gets) and recorded in the export exactly as the node answered —
   node entry, element groups, the app's device rows copied from the template (carrying the insert the device
   advertised, not the template's), and the app's per-element InsertId and button-layout rows (`actuatorExports`,
   `buttonLayoutExports`) where the template has them, with the insert and layout the device advertised (review-4
   F4-6; unverified with the app: shapes from the Android decompile) — which is handed to the gateway like any
   change; the entry reloads with the new
   device. The app's check of the number of devices a node yields follows (one for a socket, room thermostat,
   gateway, wall transmitter, mini sensor or extension insert, three for a 2-gang switch or dimmer, two otherwise):
   a difference is logged and answered as `missing_devices` (`recorded`, `expected`); the device stays added.

The answer also lists the `steps` done (`Provisioning`, the app's phases, `ReadBack`, `Recording`; each is logged as
it begins) and the `provisioning` method used. Stopping the action (a script or automation cancelled) closes the
Bluetooth connection: a device that had not received its data yet forgets the half-finished provisioning.

**Provisioning methods** (review-4 P4-8, *unverified on air*). The app always provisions with *No OOB*: nothing
authenticates the key exchange, so someone in Bluetooth range during those seconds could sit between Home Assistant
and the device and read the network's keys. Home Assistant uses the strongest method the device's *Provisioning
Capabilities* offer (Mesh Protocol 1.1 §5.4.1.2, §5.4.1.3, §5.4.2.4): *Static OOB* when the device offers it and
`static_oob` gives its value (16 bytes for the original algorithm, 32 for the HMAC-SHA256 one, in hexadecimal; never
logged), the HMAC-SHA256 algorithm over the original one when offered, else No OOB as the app. A device that takes only
authenticated provisioning is refused without its value, a value for a device that does not offer Static OOB is
refused too — both before the device learns anything. No JUNG device is known to offer more than No OOB, so in
practice this is the app's method; add devices where nobody else is in range. What a device offered and the method
used are kept in the vault and shown in the diagnostics (`added_devices`).

**`junghome_ble.remove_device`** (`device`, `force`; administrators only, same option) takes a device out the app's
way, reset first: *Config Node Reset* to the node (it forgets the network's keys and becomes a new device again) and,
only once it confirmed, every other device's wiring to it is removed — its element groups with whoever subscribed or
published to them, publications to its elements — its elements leave the scenes, its app device rows and room-link
rows go, and so do the other rows the app keeps of it (review-4 F4-6: its scene values `sceneInfo`, the legacy timer
rows `schedulerMetaInfo` / `timer`, its `actuatorExports` / `buttonLayoutExports`, its key-scene rows; that the app's
re-import then shows nothing of it is unverified with the app), and the export keeps the node entry marked
`excluded` with its addresses in `networkExclusions` (nodes still
remember its sequence numbers, so no one may reuse them before the IV index moved on twice). `force` records the
removal of a device that does not confirm its reset (one that is gone for good). A device can take the reset and
lose its confirmation (review-4 W4-7): without one, Home Assistant looks for the device advertising as a new device
(the Mesh Provisioning Service with its UUID) for 5 s; seen, the reset took and the removal goes on. Not seen, the
error says it *may have been reset*: no scanner may reach it, so run the action again, or use `force` once it is gone
for good; a device a scanner already saw advertising so before the reset (left from before it was added) is not
taken as proof. The device Home Assistant is connected through (the *Proxy node* sensor) is refused without `force`:
its reset ends the connection its confirmation would come back on — wait until Home Assistant connects through
another device, or use `force`, which takes the lost connection for that reset and does the rest once a connection
is back. Unverified on air. When a message to another device
fails after the reset, the export still records the device as removed, with the messages that were accepted; the
links the others keep to it are left in the export (they point at nothing) and the error says so. The gateway is
refused. Removing cannot be undone: the device has to be added again.

**Pending devices.** A device whose provisioning was not confirmed once it had been sent its data (it may hold the
network's keys: a lost *Complete* looks like any other failure) or whose configuration failed after it was provisioned
— and which did not confirm the reset Home Assistant sent it then — or which was configured but could not be recorded
in the export, has the network's keys while neither the app nor the
export knows it: the app cannot reset it. Home Assistant keeps it *pending* in the vault — its addresses and planned
element groups stay reserved, so no later device gets them — and raises the repair issue [*A device Home Assistant added
is not recorded*](#repair-issue-a-device-home-assistant-added-is-not-recorded); the action's error names its address.
**`junghome_ble.reset_pending_device`** (`uuid` and / or `unicast`, `force`; administrators only, same option) sends it
a *Config Node Reset* with the device key only the vault holds — it becomes a new device again and can be added once
more — and forgets it once it confirmed. The device is named by its UUID or its primary address; given both, they must
name the same pending device. No reset is sent when a device of the export sits at one of its addresses by now. `force`
forgets a device that does not confirm its reset: one already reset by hand (factory reset), or gone for good — such a
device keeps its addresses reserved until then. A pending device from a vault an earlier version wrote has no planned
element groups recorded: the log says so when the next device is added, as its groups cannot be kept free. *Unverified
on air*: `reset_pending_device` has not run against a real device yet.

What the sequence leaves out is listed in `jhmesh.commission.NOT_COVERED` (the factory rocker wiring of push-buttons,
detector key connections, the reads the app does at the end, the app's own choice of a time keeper). **None of this
has run against a real device yet**: try it with a spare device first. A device left half-configured by a failure
that did not confirm the reset Home Assistant sent it is reset with `reset_pending_device` and added again.

## Known limitations

- **Device coverage.** Switched and dimmed lights, tunable-white channels, sockets (with power metering), rockers, scenes and
  the device parameters of those devices are supported. Blinds (cover), the room thermostat (climate), detectors
  (motion / occupancy, illuminance), the energy puck's meter and the battery level of battery wall transmitters are
  implemented from the specifications only and have not been seen working on hardware. Battery
  devices sleep: their battery level and *parameters* are read only right after one of their keys reported. A change
  (a key action, a parameter) needs a key press right before it; the integration then keeps the device awake with the
  app's 6 s keep-alive while the change runs, and a device that stays silent is reported as asleep. The keep-alive
  and how long a transmitter stays awake are unverified on air.
- **Configuration changes reach the gateway one way only, and only for gateway-sourced entries.** Rooms, key
  connections, scenes and the loads a threshold switches, changed through the [actions](#actions-rooms-and-key-connections) are written to the export
  file the integration uses and, for an entry set up from the gateway, handed to the gateway as the app does after
  each of its own changes (a failed upload raises a repair issue and is tried again twice, 15 s apart, as the app
  does; `junghome_ble.sync_gateway` retries it on demand). Nothing
  flows the other way on its own: a change made in the app afterwards is only picked up when the gateway's export is
  fetched again (an unknown node of the mesh advertising triggers that; otherwise *Reconfiguration → fetch again*).
  Before every change (and before `junghome_ble.sync_gateway`) the integration does fetch the gateway's export once
  more: when the app changed the installation since the last fetch, that newer copy is adopted and the change is
  planned on top of it — `sync_gateway` refuses instead (*the gateway holds a newer export*), so fetch it through
  Reconfiguration and repeat. An entry set up from a file is never synced, whatever credentials it carries — import
  the rewritten file in the app.
- **A change a node refuses half-way is not rolled back.** Every plan is a sequence of Config messages; the steps a
  node accepted stay applied in the mesh and are recorded in the export (apply-and-record), the error says how many
  of them were, and running the same action again with the same target completes the rest. New wiring is always sent
  before the old one is cleared, so a stopped `assign_key` leaves the previous connection working. An action that is
  cancelled — an automation in `mode: restart` starting over, `script.turn_off`, Home Assistant stopping — is
  recorded the same way before the cancellation goes on, and the integration follows it; once started, the write
  of the export and that update run to their end. While Home Assistant stops, the export is not handed to the gateway:
  `junghome_ble.sync_gateway`, or the next change, does it. A crash or power cut in the middle is caught up at the
  next start: while an action runs, its messages and how many of them the devices accepted are kept in
  `.storage/junghome_ble.<entry id>.plan_journal` (no key material; removed once the export records the outcome),
  the next setup records what it says, sets the entry up again and raises the repair issue *A JUNG HOME change on …
  was interrupted* naming the action — run it again to finish. That record is not handed to the gateway at setup;
  the next change or `junghome_ble.sync_gateway` does it.
- **One connection per network.** The integration keeps a single GATT connection to one node of the mesh. That is
  enough to hear the whole installation, but every message goes through that node; if it goes away, entities are
  unavailable until the link is re-established with another node.
- **A key refresh is followed, not started.** The app's manual "key renewal" (it never happens on its own) sends
  every node a *Config NetKey Update* with the new NetKey, then *Key Refresh Phase Set* 2 and 3, all sealed with the
  nodes' device keys — which the export gives Home Assistant, and which its proxy filter lets through. The integration
  learns the new key from a NetKey Update addressed to a device's primary element, opened with *that* device's key
  and not sent from a device, and from then on accepts both keys. It moves on only on **proof that the mesh moved**,
  never on the requests themselves (a device can seal those with its own device key, and the app aborts a refresh
  when a device lags): it transmits with the new key from Phase 2 and drops the old one at Phase 3 once the proxy
  node's Secure Network beacon is secured with the new key — or its Mesh Private beacon opens with it, see *Mesh
  Protocol 1.1 privacy* below — (Key Refresh flag set: Phase 2, clear: Phase 3), or once
  *Key Refresh Phase Status* answers from two distinct devices — or from the proxy node itself — each sealed with
  the device's own key, report the phase. A status counts for the key that device was sent, so a device sending
  itself a key of its choice moves nothing but its own vote. The log names the proof of every step (never the key).
  If neither a beacon nor the statuses are heard, Home Assistant stays a phase behind, which is safe: devices in
  Phase 2 still accept the old key, and the proxy's beacon of Phase 3 moves it on. This proof rule is *unverified on
  air* (no key refresh has been run on the installation it was built against). The new key, the phase and its proof
  are stored with the sequence numbers — written at once, and for the mesh rather than for Home Assistant's address,
  so neither a crash right after a step nor a new unicast address loses them — so a restart during the refresh
  resumes it, and after it completes every setup uses the new key in place of the export's old one (a gateway export
  fetched later has it anyway); the entry's unique id follows the new Network ID. A stored Phase 2 or 3 without its proof (written before proofs were kept)
  is taken up as an accepted key only, until the proxy's next beacon proves it.

  **Devices Home Assistant added** (`add_device`) are not in the app's database, so the app never hands them the new
  key: at Phase 3 they would be cut off until factory-reset. Home Assistant hands it to them itself (*unverified on
  air*), sealed with the device keys only its vault holds, and only what was proven: a NetKey Update with the new key
  once Phase 1 is proven — the new key confirmed (a *NetKey Status*, or a Phase Status reporting phase 1) by two
  distinct devices, each under its own device key, or by the proxy node; an export written mid refresh; or a proven
  Phase 2 — then *Key Refresh Phase Set 2* once Phase 2 is proven, and *Phase Set 3* once the refresh is proven
  complete, never earlier. Each step goes out only once the one before was confirmed; the NetKey Update is sealed
  under the old network key, the only one a device without the new key accepts, so a device that missed Phase 1
  still gets it in Phase 2. A key one device made up is never handed to anyone. It runs whenever the followed
  refresh moves and on every new connection, so a device that was off or out of range is taken up later; how far
  each device came (the phase and the new key's Network ID, never a key) is kept in the vault and shown in the
  diagnostics. A device that has not confirmed the end once Home Assistant reached it raises the repair issue
  [*Devices Home Assistant added missed the new network key*](#repair-issue-devices-home-assistant-added-missed-the-new-network-key).
  Without such a device nothing is sent: in effect this is off unless `add_device` (itself behind an option, off by
  default) was used. `add_device` refuses while a refresh is in Phase 1 (the device would get the key being
  retired); in Phase 2 it hands out the new key with the Key Refresh flag, and the device is taken on to Phase 3 like
  the others.

  Only a refresh whose NetKey Updates
  Home Assistant did not hear (it was not running, or out of
  range) cannot be followed: the nodes' Phase 2 beacons, secured with the new key and carrying the Key Refresh flag,
  then raise the repair issue *JUNG HOME mesh keys are changing* (a hint — the flag itself is not authenticated), and
  control stops once the refresh completes until you export the network again and update the file through
  [Reconfiguration](#reconfiguration) (the entry is matched on the mesh UUID, so a new NetKey is accepted).
- **The sequence-number store must be kept.** Bluetooth Mesh drops messages whose sequence number is not higher than
  the last one seen from that sender. The integration stores its sequence numbers in
  `.storage/junghome_ble.seq.<mesh uuid>` — one file per mesh, with a record for every address Home Assistant has
  ever used in it (an entry from version 0.2, `.storage/junghome_ble.<entry id>`, is migrated into it on the first
  start after the update). The file, its `.backup` copy and the repair's `.floor` are readable by the owner only
  (0600): while a key refresh is followed, a record holds the new network key; a copy an older version wrote
  0644 is replaced 0600 by its next write (the store and its backup are written at every start). A clean unload or
  reload stores the exact counter;
  only a crash costs a safety margin (+512) at the next start. The `.floor` file keeps, per address, a point the
  counter is known to have reached — a new one with every new IV index and every 2^20 numbers; Home Assistant sends
  nothing under an IV index the file does not hold yet, nor 2^22 numbers past the last entry written — so the
  *sequence numbers lost* repair continues past every number sent however often both copies of the store are lost. Removing and re-adding the integration, or changing the address away and back, continues
  the counters, so no number is ever reused. **A Home Assistant backup** is safe to restore (unverified on air): while
  a backup is taken the integration marks every record of the store and waits — at most 10 s, never failing the
  backup — until both copies on disk carry the mark, and removes it once the backup is done. A start that finds a mark
  it did not set itself (the backup was restored, or Home Assistant stopped during one) logs *The sequence-number
  record of address … was restored from a backup, or Home Assistant stopped during one* and continues 2^20 numbers
  past the record — everything sent since the backup, up to a million messages — and keeps counting on, rather than
  restarting at 0, under every IV index up to one past the network's index the first beacon names (the numbers sent
  since may have gone out under a newer index than the record's). A restore costs 2^20 of the 2^24 numbers of the IV
  index, once (so does a Home Assistant that stopped during a backup, at its next start): nothing on disk says how
  many numbers went out after the backup, so the skip has to cover a generous worst case. The floor is written first,
  so a later loss of the store still knows them. Supervisor backups call the same hooks (`backup/start` and
  `backup/end`). Not
  covered: restoring the same backup a second time, or an older backup after a newer one (nothing outside the
  restored files remembers the first restore: the second continues from the same point and repeats what the first
  sent); a mesh whose entry was not loaded at all since Home Assistant started (nothing marks its records); a backup
  whose store writes did not land (logged: *… was not written before the backup*); and more than 2^20 numbers sent
  between the backup and the restore. In those cases give Home Assistant a new address (Reconfigure) after the
  restore. If the file is deleted or a copy of the configuration directory taken some other way is restored, the
  mesh will ignore Home Assistant's commands until the address is changed. The integration notices
  this on every connection — the proxy node itself drops the filter request the link starts with and never answers it
  (nothing at all is forwarded then), and the state refresh that follows goes unanswered (Gets are always answered) —
  and raises the repair issue *JUNG HOME devices ignore Home Assistant*; see [Troubleshooting](#troubleshooting).
  Never restore `junghome_ble.seq.*` on its own from a copy to undo a loss: an older copy (without the backup's
  mark) repeats every sequence number sent since it was taken, which the nodes drop as replays and which reuses
  AES-CCM nonces. The repair (skip ahead) or a fresh address is the way back.
- **Changing the address restarts the sequence numbers — once.** A new address entered in *Reconfigure* is used
  immediately with a fresh sequence-number space if the store has never seen it. When something says the address may
  have sent before — the export has Home Assistant's provisioner node or any node there, the vault keeps Home
  Assistant's identity in this mesh, or the store knows other addresses (as it does after any earlier address) — the
  numbers start 2^20 in rather than at 0, and pending the first beacon they keep counting under its index (the log
  says *Address XXXX has no sequence-number record, but …*): a lost record of that address no longer leaves the
  devices ignoring Home Assistant until the *devices ignore Home Assistant* repair. It costs 2^20 of the 2^24 numbers
  of the IV index once, also on an address that really is new; unverified on air. An address another client uses is
  still wrong — pick an unused one then. An address the store already knows continues where it left off. The
  diagnostics download shows the address in use under `local.src`.
- **The app and the gateway keep working in parallel**, and so do their timers, thresholds and scene edits. Home
  Assistant broadcasts the mesh time (Time Set, with its own time zone) first on every connection, right after the
  proxy filter and before the state refresh (a link lost before the refresh is through still sets the clocks), and
  once a day, as the app does at start — the gateway never publishes time, so device timers and astro schedules would
  otherwise drift without a phone nearby. After the time it broadcasts Home Assistant's home location (Generic Location Global Set,
  latitude / longitude / elevation from the general settings), which the nodes compute sunrise and sunset from;
  the app only sends the phone's position when it creates an astro schedule. Not yet checked on air. Each node answers
  the Time Set with its Time Status, which Home Assistant keeps; after the daily Time Set it also asks every mains
  node for its time, time zone and stored location (Time Get, Time Zone Get, Generic Location Global Get; five nodes
  at a time, battery nodes not at all). The *Clock offset* sensor and the diagnostics show what they answered, and a
  wrong clock raises the repair [devices with a wrong clock](#repair-issue-jung-home-devices-with-a-wrong-clock).
  Unverified on air.
- **ESPHome proxies need active connections.** An ESPHome Bluetooth proxy forwards GATT connections only with
  `bluetooth_proxy: active: true`, and each proxy offers a small number of connection slots (three by default) that all
  Bluetooth integrations share. This integration occupies one slot permanently.
- **Discovery reacts to any Bluetooth Mesh proxy.** The discovery card is shown for every Bluetooth Mesh network in
  range, not only JUNG HOME; ignore cards for networks you do not own. The matcher (`manifest.json`) keys on the Mesh
  Proxy service `0x1828` alone. The proxy advertisement itself carries nothing vendor-specific (the Network ID is a
  hash of the NetKey, the Node Identity form a hash of the node address); provisioned JUNG devices send the JUNG
  manufacturer record (company `0x0527`, 1319) in separate, non-connectable advertisements from the same MAC, which
  Home Assistant merges into the device's data (`docs/bluetooth-recheck.md` §5; the gateway sends none). Narrowing
  discovery to JUNG — `"manufacturer_id": 1319` in the matcher, or a `not_jung` abort in the discovery step — is a
  follow-up held for decision M10: it needs an on-air check that the merged record is already there when Home
  Assistant matches a JUNG proxy (the gateway's proxy, which sends no record, would no longer be discovered at all),
  since a too-narrow matcher hides the real mesh. `tools/mesh_poc.py scan --adv` prints the manufacturer data of
  the proxies in range. Keying the entry on the mesh UUID instead of the Network ID (also M10) is not done either.
- **Mesh Protocol 1.1 privacy is followed, unverified on air.** A proxy node with Proxy Privacy on advertises a
  Private Network Identity or a Private Node Identity (a hash of the Network ID or of the node address under the
  network key, with a random value) instead of the Network ID, and sends Mesh Private beacons instead of Secure
  Network beacons. Setup counts such a proxy as in range, the hub connects to it (`jhmesh.client.classify_proxy_advert`),
  and a Mesh Private beacon moves the IV index and proves a key refresh step exactly like a Secure Network beacon. The
  installation's devices do not use privacy as far as known, so none of this has been seen on air. Discovery still
  needs a proxy that advertises its Network ID — a private advertisement hides it — and the *keys were renewed after
  the export* check recognises a stale export only from Network IDs. The private beacon is tested against the
  specification's sample data; the two private identity hashes only against their formula (the specification's
  sample values were not at hand).
- **Sensor values start unknown.** Power, voltage and current are asked for once after every connection and then
  follow the socket's publications; power-on time after the read that follows the connect-time state refresh (then
  every five minutes).
- **Energy updates every five minutes**, with the other counters — the socket never publishes them, so the Energy
  dashboard's hourly bars are exact and the live value is at most five minutes old.

## Troubleshooting

### "No node of this mesh network is currently visible over Bluetooth"

Shown in the configuration dialog, or the entry stays in *Retrying setup*. The Network ID derived from the export's
network key did not match any Bluetooth Mesh proxy advertisement Home Assistant currently sees as connectable.

1. Check that the [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/) is set up and that a
   mains-powered JUNG node is within range of the adapter or ESPHome proxy (battery devices do not act as proxies).
2. If you use an ESPHome Bluetooth proxy, make sure its configuration contains `bluetooth_proxy: active: true`;
   without it the nodes are seen but not connectable and are ignored.
3. Check that the export is the one of *this* installation: an export from another JUNG HOME project has a different
   network key. (Nodes of *this* export seen under another Network ID give *The network's keys were renewed after the
   export was made* instead — a node UUID that is not its MAC cannot be recognised, though.)
4. Wait a minute after a restart; advertisements have to be received before the check can pass.

### Entities keep switching between available and unavailable

Every drop of the GATT connection makes all entities unavailable until the integration has reconnected. Frequent
drops mean the link to the chosen node is marginal or the Bluetooth path is overloaded.

1. Look at the *Proxy node* sensor to see which node is used, and enable debug logging (below) to see the connect
   attempts. The [diagnostics download](#diagnostics) lists the last 20 links: how long each lasted and why it ended. The integration prefers the strongest signal, so a flapping node is usually the closest one at the edge of
   range of the adapter.
2. Move the adapter or add an ESPHome proxy closer to a mains-powered JUNG node.
3. Check other Bluetooth integrations sharing the same adapter or proxy: an ESPHome proxy with all connection slots in
   use cannot hold this link.
4. On hosts with a USB adapter, use a short USB 2.0 extension cable away from USB 3.0 ports and SSDs, which are known
   sources of interference.

### One entity is unavailable or shows an unknown state

- If **all** entities are unavailable, no proxy node is connected; see the previous sections.
- If the entities of **one device** are unavailable while the rest work, the device did not answer a request
  through all of its three attempts (log: "… did not answer a request (3 attempts in 9 s): marking it unavailable"),
  the JUNG app's rule for *No connection*: the state request every connection asks each device, or a command (counted
  once the proxy node answered the watchdog's check, so a proxy that stopped forwarding marks no device). It is
  asked again every five minutes while the link lasts, and any message from the device — a status, a key press, an
  answer to one of those requests — makes it available again (log: "… is reachable again"). A device that sent
  anything while it was being asked is only busy, not marked (it is asked again a minute later); so is one that
  missed the link watchdog's single keep-alive request. This rule is always on; it only counts requests every JUNG
  device answers (state requests and commands, not settings a device might not have; of a tunable-white light's
  requests per connection, the one to its temperature element, which the app never sends, does not count), and
  battery devices are never marked (they sleep between key presses; the app does not show them as *No connection*
  either).
- If the entities of **one device** are unavailable while the rest work and the *Node heartbeats* option is on,
  that device has not been heard from for 3½ minutes (log: "… has not been heard from …"): it lost mains power or
  fell out of radio range of every relay. It comes back by itself when it beats or publishes again — and a device
  that restarted (a mains blip; it starts with its heartbeat setting empty) is asked for heartbeats again every two
  minutes while it counts as missing, so it is back within minutes of powering up (log: "… is back").
- A light or socket with state `unknown` has not reported yet. It is queried once after connecting; if it stays
  unknown, toggle it from the app or the wall to force a status publication.
- Socket sensors are queried once after connecting; if they stay `unknown`, wait for the socket's next measurement
  publication (it publishes on change).
- Rocker `event` entities have no state until the first event.

### Commands are accepted but nothing happens

Every message carries the sender's address and a sequence number, and every node remembers the highest sequence
number it has seen from each address; anything not higher is dropped silently (replay protection). Home Assistant
therefore gets no error — its commands and queries simply have no effect, while lights switched from the wall or the
app still update, because those are received, not sent. Two situations cause it:

- **Another client uses the same address.** The command-line tool `tools/mesh_poc.py` of this repository, a second
  Home Assistant instance, or any other software built on `jhmesh` that sends with the same *Our unicast address*.
  Each client keeps its own counter, so the nodes accept only the one that is currently ahead and drop the other
  one until it catches up. The CLI defaults to `7FFF` for this reason and Home Assistant to `0D00`; if you changed
  either, make sure they differ (`tools/mesh_poc.py --source`). Given Home Assistant's `.storage` directory
  (`tools/mesh_poc.py --ha-storage <config>/.storage`), the CLI refuses every address Home Assistant's store of the
  mesh holds a counter for.
- **The sequence store was lost.** `.storage/junghome_ble.seq.<mesh uuid>` was deleted, or an older copy of the
  configuration directory was restored (other than a Home Assistant backup restored once: that one skips ahead by
  itself, see [Known limitations](#known-limitations)): Home Assistant restarts its counter far below what the nodes
  remember.
  (Removing and re-adding the integration is *not* this case — the store is kept and the counters continue.)

Fix: give Home Assistant an address the mesh has never seen: enter a different *Our unicast address* (for example
`0D02`) through **Reconfigure**; an address without a record in the store starts with a fresh sequence-number space
(the log says so: *Address 0D02 has no sequence-number record in this mesh's store*). Do not reuse an address a
command-line tool has used. The diagnostics download shows the address and counter in use under `local`. Restoring
the store from a backup is not a fix: an older copy only repeats numbers the nodes have already seen.

### "That address belongs to a node in the mesh"

The unicast address you entered is used by an element of a JUNG node. Pick another free address such as `0D02`
(`7FFF` is the default of the command-line tool in this repository).

### "The mesh export could not be read"

- The path must be absolute and valid *inside* the Home Assistant container or host (`/config/...` on Home Assistant
  OS and Container installations).
- The file must be either the app's `JungHome.json` (*Share via file*) or the raw CDB JSON with a top-level
  `meshNetwork` object (`MeshNetwork.json` from an iOS backup).
- Check the file permissions of the file and the directory.

### "The network's keys were renewed after the export was made"

Nodes of the export are visible over Bluetooth, but none advertises a Network ID the export's network key derives:
the mesh completed a key refresh (a key renewal in the JUNG HOME app) that Home Assistant did not follow — it was
not running, or the entry did not exist yet. The setup dialog shows this as an error (`export_keys_stale`); an entry
waiting at setup shows it as its reason and keeps retrying. Export the network again from the JUNG HOME app (or fetch
it from the gateway again) and use it in the setup dialog, or for an existing entry under **Reconfigure**. The nodes
are recognised by their MAC (the export's node UUID), so a proxy of another network nearby does not cause it.
Unverified on air: no key refresh has been made on this installation.

### "The export belongs to a different mesh network"

During discovery or reconfiguration, the network key in the file does not belong to the network that was discovered or
already configured. Use the export of the right project, or add the other network as a separate entry.

### "This mesh is already set up as another entry"

*Add integration* was used (instead of the existing entry's *Reconfigure*) with an export of a mesh Home Assistant
already has an entry for — most often because the app refreshed the network key since, which changes the Network ID
the "already configured" check normally catches. Use the existing entry's *Reconfigure* to pick up the new export
instead: two entries for the same mesh would fight over its sequence-number store (`.storage/junghome_ble.seq.<mesh
uuid>`, shared by every entry of that mesh) and could roll the counter backwards for the other one.

Two such entries left over from an older version (or made by hand) are not both started: the one already running
keeps running, the other fails to set up with *already runs this JUNG HOME mesh*, and the repair issue *Two entries
cover the same JUNG HOME mesh* names both. Remove one of them; if you removed the running one, reload the other.

### Repair issue "JUNG HOME mesh keys are changing"

The proxy node Home Assistant is connected to sent a Secure Network Beacon with the Key Refresh flag set that neither
the export's key nor a key Home Assistant followed can authenticate — a key refresh whose new NetKey Home Assistant
did not hear being handed out (a refresh it sees from the start is followed and raises nothing). Let it finish in the
app, export the network again and update the file through **Reconfigure**.
The issue disappears when the integration starts again (with the new export, or — if no key renewal was made in the
app — after a reload; the flag is not authenticated, so a forged beacon can raise it too).

### Repair issue "JUNG HOME mesh keys have changed"

Home Assistant is connected to a proxy node, but nothing that node forwards can be decrypted with the keys of the
export: every network message fails, and so does the node's Secure Network Beacon. The mesh completed a key refresh
after the export was made (typically while Home Assistant was not running, so the *keys are changing* warning above
was never seen). Export the network again from the JUNG HOME app and update the file through **Reconfigure**. The
issue clears as soon as a message decrypts, or when the integration starts with the new export. It is only raised
after 20 undecryptable messages on one link with nothing decodable in between, so a foreign mesh in range or a node
the export does not know does not trigger it.

### Repair issue "JUNG HOME devices missing from the export"

Some node of *this* mesh advertises over Bluetooth from an address the export does not know. JUNG nodes advertise
from their MAC address, which the export records in the node identifier, so an address with the mesh's Network ID
that is missing from the export is a device added (or re-provisioned) in the JUNG HOME app after the export was
made — it has no entities here. The issue lists the devices (product and address; the product comes from the
node's own advertisement). An entry set up from the gateway helps itself: it fetches the gateway's current export
(the app uploads its project right after provisioning) and, when that export lists the device and only the
gateway changed since Home Assistant last synced with it, replaces its file (keeping `<file>.bak`, and the replaced
copy as `<file>.pre-adopt` until the next such replacement; the gateway's copy recorded as synced) and takes it
over without a reload — the device appears without any action, its loads are asked for their state over the link
that is up, and the issue clears. For an entry set up
from a file, or when the gateway's export does not know the device either (open the app once while it is connected
to the gateway so it uploads), export again from the app and update the integration (**Reconfigure**); the issue is
cleared when the new export loads and is raised again only for nodes still missing from it.

### Repair issue "JUNG HOME push-buttons with another insert than in the export"

A push-button advertises another insert (switch, dimmer, DALI, blinds, extension) than the one the export cached for
it, listed as *export → device*: the insert was replaced after the export was made. Its devices are still built from
the export's insert, so a blind may show as a light or the other way round, in the app too. Check the device in the
JUNG HOME app, export the network again and update the integration (**Reconfigure**); the issue clears as soon as
the device advertises the export's insert again, and is not raised again by an export that names the new insert.
Unverified on air.

### Repair issue "JUNG HOME devices with a wrong clock"

A mains device that may run schedules — one of its lights, sockets, blinds or thermostats hosts the JH Scheduler and
its slots are not known to be empty (the *Schedules* sensor or `get_schedules` read them) — answered with no time, a
clock more than a minute off Home Assistant's, or another time zone offset than the one Home Assistant's Time Set
carried (the local offset, or UTC where Time Set cannot carry it). Its schedules run at the wrong time. The issue lists
each device with what is wrong (`+75 s`, `no time`, `UTC+01:00`). **Submit** sends Time Set now and asks those devices
for their time again; the issue clears once they answer right and stays while one does not answer. If it comes back,
check Home Assistant's time zone (**Settings → System → General**) and whether the devices are reachable. The stored
location is shown in the diagnostics (as `home`, `elsewhere` or `not configured`), not in this issue. Unverified on
air.

### Repair issue "JUNG HOME pucks have no time keeper"

The network has older JUNG actuator pucks (products `0x0010`–`0x0014`) — the issue names their addresses — which take
their time from a *time keeper*, and every device that could be one answered that it is not (Home Assistant asks each
device's time role once). The pucks' timers and schedules drift. Turn on the *Time keeper* switch (a configuration
entity, disabled by default: enable it first) of one device that is always powered — a socket or a mini actuator, as
the app prefers — see [Switch](#switch). The JUNG HOME app chooses one itself whenever it configures a puck. The issue
clears once a device answers that it keeps the time. Unverified on air: there is no puck in the network the
integration was developed against.

### Repair issue "A JUNG HOME change on … was interrupted"

Home Assistant stopped — a crash, a power cut, a kill — in the middle of a room, key, scene, threshold or removal
action. At the next start the integration recorded in the export the messages the devices had accepted (the issue
says how many of how many, and which action), so the export says what the devices hold, and set the entry up again
from it. Run the named action again with the same target to finish it; with a gateway, the next change or
`junghome_ble.sync_gateway` hands the recorded export to the gateway. Confirming the issue dismisses it.

### Repair issue "The JUNG HOME app overrode a change Home Assistant made on …"

Home Assistant took over the export the app last handed to the gateway and carried its own changes onto it (see
*Gateway sync* above), but the app had changed some of the same entries meanwhile — it re-linked a key Home Assistant
had linked, for example. The app's version is kept in the export (its messages reached the devices later); the issue
lists each entry by its path in the export and what Home Assistant had written there, which the devices may still
hold. Check those rooms, scenes and connections in the app and in Home Assistant and set them again from one side.
The issue clears with the next takeover of the gateway's export that has no such conflict; diagnostics list the
paths while it is open (node UUIDs redacted). Unverified on air.

### Repair issue "Devices still hold a deleted JUNG HOME scene on …"

`junghome_ble.delete_scene` ran with `force` (*Even if a device does not answer*) while some members of the scene
could not be reached or refused: the scene is gone from the export, but those devices still hold its number and
would join every recall of a scene with that number. The issue lists each number with the members that hold it (the
register element's address and, when known, its load's name). Home Assistant gives none of these numbers to a new
scene meanwhile. When the devices are reachable again, run `junghome_ble.delete_unused_scenes` with `dry_run: false`
(on an entry set up from a file, with `confirm_stale_export: true` or `numbers: [<the number>]`): it deletes the
numbers from them, and the issue clears once no member holds one any more. The issue stays across restarts.

### Repair issue "No Bluetooth for the JUNG HOME mesh"

Home Assistant has no connectable Bluetooth scanner left: the local adapter is switched off, unplugged or has failed,
or every ESPHome Bluetooth proxy (with `bluetooth_proxy: active: true`) is offline. All devices of the mesh are
unavailable, the *Link state* sensor shows `bluetooth_off` (the JUNG HOME app's "Bluetooth is off" screen). Check
**Settings → Devices & services → Bluetooth**. The issue clears by itself as soon as an adapter or proxy is back, or
a proxy node of the mesh is seen at all. The same condition while the entry is being set up shows as *Retrying setup*
with "Home Assistant has no connectable Bluetooth adapter or proxy".

### Repair issue "JUNG HOME devices ignore Home Assistant"

The nodes discard our messages: either the proxy node authenticated the mesh beacon but never answered the proxy
filter request Home Assistant sends on every connection (logged as a warning within 10 s of connecting; the proxy then
forwards nothing at all), or no device answered the state refresh although the link works. Either the sequence-number
store was lost (old backup restored), or another client — `tools/mesh_poc.py`, a second Home Assistant — transmits
with the same address (`mesh_poc.py` defaults to `7FFF`; it used `0D01` before, and before that `0D00`, the
integration's default). Pick a new address through **Reconfigure** (it must not be one any other client uses) or, when
the store was lost, confirm the repair to skip the sequence numbers ahead. Do not restore the store from a backup: an
older copy only repeats numbers the nodes have already seen.
The issue clears by itself as soon as the proxy answers the filter request or a device answers. It is not raised for a
filter request that never went out — none written on that link, or the sequence-number store holding sends back — as
that says nothing about the proxy; a store that cannot be written has its own repair (next section). Nor is it raised
while *Another client uses Home Assistant's JUNG HOME address* is open: once the other client is seen, that issue
names the cause and replaces this one.

### Repair issue "Another client uses Home Assistant's JUNG HOME address"

The proxy forwarded a message sent from Home Assistant's own unicast address with a sequence number Home Assistant
never used — above its counter, or under an IV index it never transmitted under. Its own messages relayed back by the
mesh carry numbers it did send and are ignored as before; this one comes from another client on the same address: a
second Home Assistant installation, `tools/mesh_poc.py` configured with this address, or a copy of this installation
started elsewhere. Two senders on one address reuse each other's sequence numbers (the same nonce for two messages)
and the devices drop whatever lies below the other client's last number, so from the first sighting Home Assistant
sends nothing to the mesh (commands fail, the state is not refreshed; the link stays up and keeps receiving). The
sighting is saved with the counter (`address_shared` in the address's record), so a restart keeps refusing. The log
says *Another Bluetooth mesh client sends from Home Assistant's address …*, the library's *… which we never sent:
another client uses this address*; the diagnostics show the highest number seen under `local.address_shared`.

Stop the other client or give one of the two another unused address (**Reconfigure** for Home Assistant), then
confirm the repair: the counter continues 512 past the highest number the other client was seen with, and sending
resumes (a link whose proxy never took the filter request is renewed). If the other client sends from the address
again afterwards, the issue comes back asking for another address for Home Assistant — skipping ahead does not help
while it keeps sending. Do not restore the sequence-number store from a backup to fix it. Unverified on air: only
simulated; to check it, run `tools/mesh_poc.py` with `--source` set to Home Assistant's address for a single command
and look for the issue, and do not leave two clients on one address longer than that.

### Repair issue "JUNG HOME sequence numbers cannot be saved"

Every message Home Assistant sends carries a new sequence number, and it only sends numbers its store
(`.storage/junghome_ble.seq.<mesh uuid>` and its `.backup` copy) has saved, so that a restart never repeats one. When
writes fail — a full disk, a filesystem remounted read-only (the usual way an SD card dies) — sends are held back:
each waits up to 2 minutes for the store to catch up (retrying every 5 s) and then fails like a lost link, so commands
fail and the state is not refreshed. After a minute of this the repair names the file and the last write error (also
in the log, and in the diagnostics as `stalled_for`, `last_write_error` and `durable_headroom`). Free up space or
repair the storage (a filesystem remounted read-only usually needs the host restarted); do not edit, delete or replace
the store's files. The issue clears itself with the first number handed out once a write lands. The link watchdog
keeps working meanwhile: a keep-alive that cannot be sent decides nothing, and a proxy that stays silent is dropped as
usual. Tested with injected write failures only: a store that cannot be written has not been seen on this
installation.

### Repair issue "JUNG HOME mesh is at another IV index"

An authenticated beacon states an IV index Home Assistant cannot follow (`JungHomeHub._check_iv_index`): the mesh is
more than 42 ahead (Home Assistant was away through many IV Updates), or Home Assistant is two or more ahead of the
mesh, which the mesh itself never is (its store belongs to another mesh, or beacons forged with the network key
pushed it there). The devices ignore Home Assistant until it is fixed.

- **Home Assistant ahead** (*Home Assistant is ahead of the JUNG HOME mesh's IV index*): confirm the repair. It
  takes Home Assistant back to the mesh's index and sets the entry up again (`async_rewind_iv_index`,
  `LocalState.rewind_iv_index`): the counter continues above every number sent from that index on (each record keeps
  the highest number reached under its earlier indexes, `seq_peak`, from `seq_peak_from` on) and `seq_guard` keeps it
  from restarting at 0 under any index up to the old one. The repair floor is written first, then both copies of the
  store; the index is stored as not known, so the proxy's first beacon is adopted as it is. Unverified on air: an IV
  index ahead of the mesh has not been seen on this installation.
- **Mesh ahead**, or a record from before this version (it knows nothing below its own index, `can_rewind_to`): give
  Home Assistant a new unicast address (**Reconfigure**); it starts from the mesh's IV index there.

Do not remove the address's record from the store: setup then went on from the `.backup` copy at the same index (or
refused with *sequence numbers lost*), and a hand edit is overwritten at the next start. Do not restore the store from
a backup either: an older record repeats numbers the devices already saw. The issue clears itself when a beacon within
reach arrives.

### Entities go unavailable every few minutes

If the proxy node stops forwarding traffic (a rebooting node keeps a stale GATT link, a proxy hiccup), the
integration notices: after 11 minutes without any mesh traffic it sends a keep-alive Get to a load through the proxy,
and only when that goes unanswered as well is the link dropped and another node preferred for the next connection. A
quiet mesh (nothing publishing at night, no gateway polling) therefore keeps its link. A
mesh whose keys changed after a completed key refresh shows the same pattern, together with the repair issue *JUNG
HOME mesh keys have changed* — export and reconfigure.

### Repair issue "JUNG HOME Gateway certificate changed"

The gateway's address answers with another TLS certificate than the pinned one, or the gateway node reports another
certificate over the mesh. Home Assistant stops using the gateway — no token, no export goes there; the devices keep
working over Bluetooth. If you replaced the gateway or renewed its certificate, open **Reconfigure** and fetch the
export from the gateway again, while Home Assistant is connected to the mesh (a pin the gateway node vouched for is
only replaced by what the node reports). Otherwise another device on your network may be answering as the gateway.

### Repair issue "Device name not passed on to the JUNG HOME app"

A device was renamed in Home Assistant to a name the JUNG HOME app refuses: a blank one, one longer than the 30
characters the app's rename takes, or one with a `%` sign the app would take for the start of a placeholder (anything
but `%%` and `%n`). Home Assistant keeps the name,
the app and the mesh export keep the old one. Rename the device again; the issue clears with the next rename that is
passed on.

### Repair issue "A device Home Assistant added is not recorded"

`junghome_ble.add_device` provisioned a device, but its configuration or its recording in the export failed. The
issue names its address (never a key). The device has the network's keys, but the app and the export do not know it,
and its addresses and element groups stay reserved. Run `junghome_ble.reset_pending_device` with that address, then
add the device again; for a device already reset by hand, or gone, add `force`. See
[Actions: adding and removing devices](#actions-adding-and-removing-devices-experimental) (*Pending devices*). The
issue clears when no such device is left.

### Repair issue "JUNG HOME device keys cannot be saved"

`junghome_ble.add_device` writes the new device's key to the vault (`.storage/junghome_ble.vault.<mesh uuid>`) before
the device receives its Provisioning Data: until the export records the device, nothing else holds that key. The
write did not land — a full disk, a filesystem remounted read-only (often a failing SD card) — so provisioning
stopped there: the device received nothing and is still new, and nothing is reserved for it. The issue names the
address it was to get, the file and the last write error (never a key). Free up space or repair the storage (a
filesystem remounted read-only usually needs the host restarted), then add the device again; do not edit, delete or
replace the vault's files. The issue clears itself as soon as a write of the vault lands. Tested with injected write
failures only; *unverified on air*.

### Repair issue "Devices Home Assistant added missed the new network key"

The app renewed the network key and Home Assistant followed the renewal to its end, but a device Home Assistant added
with `junghome_ble.add_device` did not confirm the new key (the app does not know such devices, so Home Assistant
hands them the key itself; see *A key refresh is followed* under [Known limitations](#known-limitations)). The issue
names its address. Make sure the device is powered and in range: Home Assistant tries again with every new Bluetooth
connection, and the issue clears once every such device confirmed. A device that never received the new key cannot
be reached any more: factory-reset it and add it again (forget it first with `junghome_ble.reset_pending_device` or
`junghome_ble.remove_device`, with `force`). *Unverified on air.*

### Repair issue "JUNG HOME Gateway no longer accepts Home Assistant"

The gateway rejects the access token Home Assistant registered (typically removed in the app under *Settings →
Gateway → Access permissions*). Changes made in Home Assistant still go to the mesh and its own export, but no longer
to the gateway; the devices keep working. Home Assistant asks for access again on its own: under *Settings → Devices &
services* JUNG HOME asks to re-authenticate (*Repairs* lists it as expired authentication too). Follow it and either
enter the gateway's network-key password (from the app; access is granted at once) or leave it empty and approve the
request *Home Assistant (Bluetooth Mesh)* in the app under *Settings → Gateway → Access permissions → Open requests*
within three minutes. Only the access is renewed — the export is not fetched again and the integration is not
reloaded — and the repair clears. Run the action *Sync gateway* (`junghome_ble.sync_gateway`) to hand over the changes
made since, or make the next change. If the gateway presents another certificate than the pinned one, the same
certificate check as in Reconfigure applies (see the security note under [Prerequisites](#prerequisites)).

The question is asked once per outage (and again when the integration is reloaded while the repair is open). The
old way still works: **Reconfigure → Fetch it again from the gateway** requests access anew and loads the gateway's
current export, which replaces Home Assistant's file (kept as `.pre-reconfigure`). The re-authentication is
*unverified on air*.

### "The access request was not approved in time"

The gateway keeps a request open for three minutes. Open the JUNG HOME app, go to *Settings → Gateway → Access
permissions → Open requests*, approve *Home Assistant (Bluetooth Mesh)*, then submit the dialog again.

### "The gateway holds no network export"

The gateway only stores a project once the app has been connected to it (the app uploads its project after every
change); open the app while connected to the gateway once. Gateway firmware older than 2.1 has no project routes —
use the upload or path source instead.

### Enabling debug logging

Open **Settings → Devices & services → JUNG HOME (Bluetooth Mesh)** and select **Enable debug logging** in the entry's
menu (select it again to disable and download the log), or add to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.junghome_ble: debug
    jhmesh: debug
```

`custom_components.junghome_ble` logs connection attempts and entity-level decisions; `jhmesh` logs every decrypted
mesh message (addresses, opcodes and values, never keys), sent commands and undecryptable packets. Without debug
logging a device that does not answer leaves one warning when it is marked unavailable ("… did not answer a request
…") and one line when it is back; each unanswered attempt is a `jhmesh` debug line ("no response from … (attempt
1/3)").

### Diagnostics

Open the entry's menu and select **Download diagnostics**. The file contains the address in use (the configured file
paths and Bluetooth addresses are redacted), a summary of the network (mesh UUID, network ID, number of nodes, groups, scene numbers), Home Assistant's own sequence
number and IV index (with how long the sequence-number store has held sends back, its last write error and how many
numbers may go out before the next hold; the file path in that error is redacted, the error itself kept), the link
state (connected node, MTU, connection time, visible proxy nodes) and the last 20 links, newest first — the proxy node
by its mesh address, how long ago it ended, how long it lasted, why it ended (`the proxy disconnected`, `the proxy
went silent`, `sequence numbers skipped ahead`, …), how long its connect-time state refresh took (`null`: the link went
first) and how long sends were held back for the sequence-number store during it —, the entry's options, the open
repair issues, the derived device list, the last known state of every element, each node's last
[network audit](#actions-network-audit) result and the followed key refresh (its phase, how far it is proven, and the
phase each device Home Assistant added confirmed, with the new key's Network ID), under `added_devices` each device
Home Assistant added (recorded or pending) with what it offered for its provisioning and the method used (algorithm and
OOB names and sizes, never a value), and under `clocks` each node's
clock as it last answered: when, its offset in seconds, whether it has a time, its zone offset and the one Time Set
carried (minutes), whether its stored location is the home's (`home`, `elsewhere`, `not configured` — never the
coordinates) and what is wrong with it, if anything. Keys are never included. A device's own menu offers **Download diagnostics** as well: the node's elements and
models, what the node told about itself (`node_info`, the app's node details: software version, hardware revision,
manufacturer name, secure element and bootloader versions, a room thermostat's STM32 version and the node's time
role), its clock (`clock`, as above), its devices, the cached state of its elements and its last audit result.

The download works whatever state the entry is in. An entry that is not loaded — waiting for a proxy node
(*retrying setup*), failed (its export cannot be read), or disabled — has no link to describe; its download shows the
entry's data and options, its state and the reason (`reason_key`, e.g. `no_proxy_visible`, `bluetooth_unavailable`,
`cannot_load`; any file path redacted), the number of connectable Bluetooth scanners, every node advertising the Mesh
Proxy service (its Bluetooth address redacted; whether it advertises a Network ID or a Node Identity — or, with Mesh
Protocol 1.1 privacy on, a Private Network or Node Identity — whether that fits the export and, for a node identity,
which node it names), the export's summary (or the kind of error that kept it
from loading) and the open repair issues. A device's download shows the same while the entry is not loaded.

## Removal

The integration follows the standard removal procedure: go to **Settings → Devices & services → JUNG HOME (Bluetooth
Mesh)**, open the entry's menu and select **Delete**. If you no longer want the integration itself, also delete
`custom_components/junghome_ble/` and restart Home Assistant.

Afterwards:

1. Delete the export file and the metadata directory from the Home Assistant host if you pointed the integration at
   them by path; they contain the mesh keys. Exports the integration fetched or uploaded itself
   (`<config>/junghome_ble/<mesh UUID>.json`), their backup copies (`.bak*`, `.app`, `.pre-adopt`,
   `.pre-reconfigure`), any orphaned `.incoming-*` file, the entry's records in `.storage`
   (`junghome_ble.<entry id>.plan_journal`, `.held_scenes`, `.gateway_sync`) and the entry's repair issues are removed
   with the entry; the directory itself stays. (Stale `.incoming-*` files older
   than an hour — a setup dialog that died — are also swept at every start.)
2. `.storage/junghome_ble.seq.<mesh uuid>` (the sequence-number store, shared by every entry for that mesh) is kept
   on purpose: if you set the integration up again for the same address, the counters continue and the mesh accepts
   the messages (it would otherwise reject reused sequence numbers). Delete it by hand only if you never will, or use
   a different unicast address next time.
3. `.storage/junghome_ble.vault.<mesh uuid>` (Home Assistant's provisioner identity and the device keys of the devices
   it added) and its `.backup` copy are kept too: a device Home Assistant added may be in no export, and only this file
   lets it be configured or reset. Delete them (and any `.unreadable.*` copy) by hand once those devices are gone or in
   the app's hands; the integration never deletes them, not even with the entry.

Nothing needs to be undone in the JUNG HOME app: Home Assistant was never provisioned as a node. (With the
provisioner option on, the files it wrote name it as a provisioner; an entry the app keeps reserves its ranges and
nothing else.) What its actions
and settings changed on the devices (rooms, key connections, scenes, schedules, thresholds, parameters) stays, like
a change made in the app; switch *Node heartbeats* off before deleting the entry, as a deleted entry can no longer
tell the devices to stop beating.

## Developer notes

The developer notes moved to the [developer documentation](dev/README.md): the module map and the behaviour
worth knowing before changing the hub in [Architecture](dev/architecture.md), the fixtures, fakes and the
simulated mesh in [Testing](dev/testing.md), and how a version is released in [Releases](dev/release.md).
