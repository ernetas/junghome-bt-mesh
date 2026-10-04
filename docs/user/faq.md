# FAQ

## Do I need the JUNG HOME Gateway?

No. Home Assistant talks to the devices directly over Bluetooth. A gateway helps in two ways if you have one: Home
Assistant can fetch the network export from it (no file to copy) and picks up devices you add in the app by itself,
and it passes Home Assistant's changes on to it. The gateway also starts the mesh's occasional switch to fresh
sequence numbers, which a very busy installation needs after a long time.

## Does this break the JUNG HOME app?

No. Home Assistant joins the installation as one more participant, like a second phone that only listens and
switches. Nothing on the devices changes until you change something from Home Assistant on purpose (a room, a key
connection, a scene, a setting), and then it writes exactly what the app would. The app and the gateway keep
working. One thing to know: the app does not see what Home Assistant changed until it loads the project again — see
[Changing the installation](changing-the-installation.md#how-the-app-learns-about-changes).

## Should I keep the JUNG HOME Gateway integration?

You can keep both; everything then exists twice. Most people move over: the gateway integration's entities can be
taken over with their history, names and areas, so automations and dashboards keep working — see
[Changing the installation](changing-the-installation.md#coming-from-the-gateway-integration).

## Why does my rocker not report clicks or double clicks?

Only keys connected to the **gateway** in the app send clicks, double clicks and holds. A key connected to a light
reports `press_on` / `press_off` (and `dim` while held) — automations can use those too. To get clicks, connect the
key to the gateway; without a gateway, or for a key that should only drive Home Assistant, see
[A key that only talks to Home Assistant](buttons-and-automations.md#a-key-that-only-talks-to-home-assistant).

## Why is there a "Bluetooth Mesh network" in Discovered that is not mine?

Home Assistant offers every JUNG HOME installation it sees over Bluetooth — a neighbour's too. Other brands' Bluetooth
Mesh networks are not offered. The card (*Bluetooth Mesh* and the network's id) asks *Is this your JUNG HOME
installation?*; if it is not, ignore the card. Your own installation is offered once and not again after you set it up. A *JUNG HOME Gateway* card
is different: only a JUNG HOME Gateway announces itself that way, and one an entry already uses at that address is
not offered.

## Why are there three devices for one push-button?

Because the app shows them that way too: the push-button itself (the *node device*, with its firmware and
diagnostics), the light it switches (named as the light in the app), and its keys (a *push-buttons device*). A
2-gang push-button may have two lights and two sets of keys. See
[Getting started](getting-started.md#what-appears-in-home-assistant).

## Why does the Energy dashboard show another number than the app?

The app shows the consumption since its last reset; the *Energy* sensor counts from the day the socket was made. See
[Energy](energy.md#why-home-assistant-shows-another-number-than-the-app).

## What happens while Home Assistant is down?

Nothing changes for the installation: keys, the app, the gateway, timers and schedules work as always. When Home
Assistant is back, it reads every device's state again and fills the energy statistics in from the sockets' own
history. Key presses made in the meantime are not reported later.

## Which Bluetooth hardware do I need?

Any Bluetooth adapter Home Assistant supports, on the Home Assistant host, or an ESPHome Bluetooth proxy (an ESP32
board) with `bluetooth_proxy: active: true`. It must be within reach of one mains-powered JUNG device; a proxy in
another room is often the easiest. See [Getting started](getting-started.md#what-you-need).

## What does "unverified" mean?

Places in these pages marked **unverified** (in the reference: *unverified on air*, *unverified on hardware*) describe
behaviour that has not been seen working on a real installation yet: devices the maintainer does not own (blinds,
room thermostats, detectors, battery transmitters, the energy puck) and some functions not tried yet. They follow
the JUNG HOME app and the gateway closely and are covered by tests, but a real device might still behave
differently. Reports of what happens are welcome.

## Is my export safe?

The export file holds **every key** of your installation: with it, anyone could control and reconfigure your
devices. So:

- never share it, post it or commit it anywhere;
- Home Assistant stores its copy (and its backup copies) readable by itself only, in `junghome_ble/` of the
  configuration folder; keep that folder out of backups you share;
- diagnostics downloads and logs never contain a key;
- if you used a file on the host, delete it once you no longer need it.

If an export did leak, renew the network key in the JUNG HOME app (a *key renewal*), then give Home Assistant the
new export.

## Can I run two Home Assistant installations on one JUNG HOME installation?

Yes, if each one has its own *Our unicast address* (under *Advanced* in the setup and Reconfigure forms; for example
`0D00` and `0D02`). Two installations with the same
address get in each other's way, and a repair notice says so.

## Does it work with battery wall transmitters?

Their keys report like any other key. They sleep between presses, so their battery level is read only right after a
press, and a setting can only be changed right after pressing one of their keys. **Unverified on hardware.**
