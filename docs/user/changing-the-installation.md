# Changing the installation

Rooms, key connections, scenes and devices can be changed in two places: in the JUNG HOME app, as before, or from
Home Assistant with its actions. Both write the same things to the devices.

## Changed something in the app?

Home Assistant learns your installation from the export it was set up with, so it needs the new export after you
added, removed or replaced a device, rewired a key, renamed things or edited rooms and scenes in the app.

- **Set up from the gateway:** Home Assistant follows the app by itself. The app hands its project to the gateway
  after each change; when Home Assistant hears your phone on the mesh, it asks the gateway for the project a few
  minutes after the phone went quiet (so a burst of edits is one request, at most one every 15 minutes), and
  every six hours whatever it heard. A device you add in the app shows up as soon as it does. To pick a change up
  right away, press **Fetch export from gateway** on the gateway's device (under *Configuration*). Both are options
  of the entry (*Configure*), on by default. **Unverified on air.**
- **Set up from a file:** share the export from the app again (*Project → Share via file*) and give it to Home
  Assistant: *Reconfigure → Upload the app's export file* (or point it at the new file on the host). When Home
  Assistant sees the app change a device's configuration, a repair notice reminds you (see
  [Maintenance](maintenance.md#the-jung-home-app-changed-the-installation)).

Home Assistant takes the new export over without a reload where it can (no entity goes unavailable), and reloads
otherwise. Devices that are no longer in it disappear; the others keep their entities, names and history. On the way
it asks again which area each room goes to (see [Areas](getting-started.md#areas)), with your previous choice filled
in. A device that the app reports as new but that is not in the export yet raises a repair
notice (see [Maintenance](maintenance.md#jung-home-devices-missing-from-the-export)).

After a **key renewal** in the app (rarely needed), Home Assistant follows along if it was running at the time;
otherwise a repair notice asks for a new export. See [Maintenance](maintenance.md#keys-and-security).

## Changing it from Home Assistant

The integration's actions change the installation the way the app does: they send the same messages to the
devices and record the change in the export. Run them from *Developer tools → Actions*, a script or an automation.
They are **for administrators**: a user who is not an administrator cannot run them.

A change is followed at once, without reloading: no entity goes unavailable. When a device does not answer, nothing
is recorded and the action tells you which device it was; run it again once the device is back.

### Try it first, and see what it did

The actions that rewire devices — *Set room*, *Add to room*, *Remove from room*, *Create room*, *Delete room*,
*Assign key*, *Clear key*, *Create scene*, *Delete scene* and *Remove device* — have a **Dry run** switch: the action
answers what it would send to which device and how the export would change, and writes and takes over nothing; it
sends only the reads described below. Turn on *Return response* in *Developer tools → Actions* to see the answer.

### When the devices no longer match the export

Before a change that removes or replaces something on a device — a key wired elsewhere or cleared, a device leaving
a room, a room deleted, a device taken out of a scene, a scene deleted, a threshold's wiring, a device removed — Home
Assistant first asks the devices what they hold there and compares it with the export. When a device holds
something else (you changed it in the app after the export was made, a device was reset, an earlier change stopped
half-way), nothing is sent and the action says which device differs and how: export the project from the app again
(or, set up from the gateway, press **Fetch export from gateway**) and run the action again. A dry run lists the
differences under `preflight`. A device that does not answer this read stops the action too, before anything is
changed. If you know the export is right, turn on **Even if the devices differ from the export** (`force: true`) to
run the action without the check. This is not yet tried on a real installation.

Run for real, the same actions answer how many of their messages the devices took (`applied` of `total`), whether
the export was written (`recorded`) and which devices changed (`nodes`), and the logbook gets a line such as
*Key 0234 (…) now drives room Kitchen; 8 messages* — or how far it got when a device stopped it. The last few are in
the integration's diagnostics too.

Instead of typing a room's name you can pick the **area** named like it (*Room by area*), and instead of a scene's
name its **scene entity** (*Scene entity*).

*Remove device*, and *Delete scene* with *Even if a device does not answer*, cannot be undone: they need
**Confirm** turned on (`confirm: true` in a script). A dry run needs none.

### Rooms

| What | Action |
|---|---|
| Put lights, sockets or blinds into a room (and out of every other) | *Set room* (`junghome_ble.set_room`); *Create the room* makes a new one |
| Add them to a room and keep their other rooms | *Add to room* (`junghome_ble.add_to_room`) |
| Take them out of one room | *Remove from room* (`junghome_ble.remove_from_room`) |
| Create, rename or delete a room | *Create room*, *Rename room*, *Delete room* |

A device without a Home Assistant area is placed in the room's area (the one chosen for the room under
[Areas](getting-started.md#areas), else the one named or aliased like it); with *Move devices along when their JUNG
room changes* on, a device still in its old room's area moves too. Keys connected to the room start or stop
switching the device with it. *Add to room* and *Remove from room* are **unverified on air**.

### Key connections

*Assign key* (`junghome_ble.assign_key`) connects a key to a light, a socket, a blind, a room thermostat, a room, a
scene or the gateway, with a mode as in the app (*light*, *switch*, *move* for blinds, *gateway*, …); *Clear key*
gives it no function. A detector can be connected the same way. See [Buttons and automations](buttons-and-automations.md)
for what each connection reports to Home Assistant. Connections to a scene, to a room thermostat, the lock mode and
detectors are **unverified on air**.

A key of a battery device answers only while awake: press one of its keys, then run the action right away.

### Scenes

1. Set the lights (and sockets, blinds, thermostats) the way the scene should leave them.
2. Run *Store scene* (`junghome_ble.store_scene`) on them with the scene's name or number. *Create scene* makes a new,
   empty scene first.

*Remove from scene* takes devices out, *Rename scene* and *Delete scene* do what they say, and *Delete unused scenes*
cleans scene numbers off the devices that no scene of the app uses any more — by default it only lists them; run it
with *Dry run* off to delete (on an installation set up from a file, also with *The mesh export is current*, after
making sure the file has every scene of the app). Blinds and thermostats in scenes are **unverified on hardware**.

### Schedules

JUNG lights, sockets, blinds and thermostats run schedules themselves, even without Home Assistant — the app's
*Automation* page. *Create schedule*, *Update schedule*, *Enable schedule*, *Disable schedule* and *Delete
schedule* manage them (a time of day, or sunrise / sunset with an offset, on chosen weekdays); *Get schedules* and the
*Schedules* sensor show them. **Unverified on air**: not yet tried on a real device.

The devices' clocks: Home Assistant sets the time and your home's location on the devices every time it connects
and once a day, as the app does when it starts. A device with a wrong clock raises a repair notice.

### Energy thresholds

See [Energy](energy.md#let-a-socket-switch-other-devices-by-itself).

### Adding and removing devices (experimental)

Adding and removing JUNG devices from Home Assistant is **experimental** and **unverified on air**, and off by
default: turn on *Allow Home Assistant to add and remove devices* under *Configure* first, and try it with a spare
device. *Find new devices* lists devices nearby that are not set up yet; *Add device* adds one the way the app does,
copying the setup of a device of the same kind already in the installation; *Remove device* resets one and removes
it. Details and caveats: [reference](../ha-integration.md#actions-adding-and-removing-devices-experimental).

## How the app learns about changes

The JUNG HOME app never downloads the project by itself; it only knows what it did itself, or what you import.

- **With a gateway:** Home Assistant hands every change to the gateway, as the app does after each of its own
  changes. When the app changes something later, Home Assistant carries its own changes over onto the app's newer
  project, so neither side's work is lost (**unverified on air**). If the app changed the very same thing, the app's version wins and a
  repair notice lists what Home Assistant had set there. The app itself keeps showing its own view until it loads the
  project again.
- **Without a gateway:** the app does not see Home Assistant's changes, and its next change can overwrite them. Get
  the current file with the action *Download export* (`junghome_ble.download_export`, see
  [Downloading the export](maintenance.md#downloading-the-export)) or *Export network*
  (`junghome_ble.export_network`, flavour `share`) and import it into the app, or make such changes in the app
  instead.

What Home Assistant wrote stays on the devices either way; only the app's view can lag behind.

## Coming from the gateway integration

If the [JUNG HOME Gateway integration](https://github.com/ernetas/junghome) is set up too, every light, socket and
key exists twice. Its entities can be **taken over** — they keep their entity IDs, names, areas and history, and
your automations and dashboards keep working: *Reconfigure → Import the entities of the JUNG HOME Gateway
integration*. The dialog shows what will move before anything happens. Afterwards check the result and delete the
gateway integration's entry. Automations on a key's *Down* entity need pointing at the lower half of the moved key
(for example the device trigger *Button A clicked (lower half)*). Details: [reference](../ha-integration.md#migrating-from-the-gateway-integration).

You can also keep both integrations; a repair notice suggests the import, and you can ignore it.
