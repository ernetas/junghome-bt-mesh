# Getting started

## What you need

- **A JUNG HOME installation set up with the JUNG HOME app.** Home Assistant learns your devices, rooms, scenes
  and key connections from the app's network export (see [below](#add-your-installation)).
- **Bluetooth in reach of the installation**, one of:
  - a Bluetooth adapter on the Home Assistant host, or
  - an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) (an ESP32 board) with
    `bluetooth_proxy: active: true` in its configuration — without `active: true` it cannot connect.

  The [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/) of Home Assistant must be set
  up.

### Where to put the adapter or proxy

Place it within Bluetooth range of **one mains-powered JUNG device**: a push-button with an insert, a socket, an
actuator. Every mains-powered JUNG device passes messages on to the others, so one device in reach is enough for the
whole installation. Battery devices (wall transmitters, battery mini sensors) do not count: they sleep. Home
Assistant connects to the device with the strongest signal and switches to another one by itself when that one goes
away; more proxies in different rooms give it more choice.

An ESPHome proxy has a few connection slots (three by default) that every Bluetooth integration shares; this
integration keeps one of them busy all the time. On a host with a USB adapter, a short USB extension cable away from
USB 3 ports and SSDs avoids interference.

## Install

**With HACS:** *HACS → Integrations → ⋮ → Custom repositories*, add this repository's URL with the category
*Integration*, install *JUNG HOME (Bluetooth Mesh)*, then restart Home Assistant.

**By hand:** take `junghome_ble.zip` from the latest release and unpack it into your configuration's
`custom_components/` folder (so that the files end up in `custom_components/junghome_ble/`), then restart Home
Assistant.

## Add your installation

Home Assistant may find the installation by itself, under *Settings → Devices & services → Discovered*:

- a card *JUNG HOME Gateway …* when a JUNG HOME Gateway is on your network. Select **Add** and confirm: you go straight
  to the gateway form below, with the gateway's address filled in. (Setting up from this card has not been tried on
  a real installation yet: **unverified on air**.)
- a card *Bluetooth Mesh …* when a Bluetooth Mesh network is in range. Home Assistant offers every one it sees, other
  brands' too: confirm only if it is your JUNG HOME installation.

Otherwise go to *Settings → Devices & services → Add integration* and search for **JUNG HOME (Bluetooth Mesh)**.
Either way (but for the gateway card) you are asked where the network export comes from. Pick one of the three:

### From the JUNG HOME Gateway

If you have a JUNG HOME Gateway (firmware 2.1 or newer), this is the easiest: nothing to copy.

1. Choose **Fetch it from the JUNG HOME Gateway**.
2. Enter the gateway's address (a discovered gateway's is filled in already): `junghome.local` usually works;
   otherwise its IP address, which the app shows under *Settings → Gateway*.
3. Either enter the gateway's network-key password from the app — access is granted at once — or leave it empty:
   the app then shows an access request *Home Assistant (Bluetooth Mesh)* under *Settings → Gateway → Access
   permissions → Open requests*. Approve it within three minutes.

With the gateway, Home Assistant also picks up devices you add in the app later, by itself, and hands every change
it makes back to the gateway.

### Upload the app's export

1. In the JUNG HOME app open *Project → Share via file*. The app creates a file `JungHome.json` and offers to share
   it.
2. Choose **Upload the app's export file** in Home Assistant and upload that file.

**From the phone itself:** you can do both steps on the phone that runs the JUNG HOME app. Save `JungHome.json`
to the phone's files (for example *Save to Files* on an iPhone, or *Downloads* on Android), then open the **Home
Assistant companion app** on the same phone, add the integration as above and pick the file in the upload dialog.
This uses Home Assistant's ordinary file upload; it has not been tried with every phone and app version
(**unverified**). If the file picker does not offer the file, send it to a computer and upload it from there.

### A file on the Home Assistant host

For people who copy files to the host anyway: copy `JungHome.json` (or `MeshNetwork.json` from an iPhone backup of
the app) to the host, for example into `/config/junghome/`, choose **Use a file on the Home Assistant host** and
enter the path. With `MeshNetwork.json` from a backup, the device names are in a separate folder
(`Library/Application Support/` of the app's backup); enter it as the *App metadata directory*, or your devices are
named by type and address.

### Advanced

Each of these forms has a collapsed *Advanced* section with one field, *Our unicast address*: the address Home
Assistant uses in the JUNG HOME network. Leave it closed to keep the default `0D00`. Only if you run a second Home
Assistant (or the repository's command-line tools) on the same installation does each one need an address of its own.

Before the setup finishes, Home Assistant checks that it can read the file and that it sees at least one device of
*this* installation over Bluetooth. If it cannot, see [Maintenance](maintenance.md#setup-problems).

> **Keep the export private.** It holds every key of your installation: anyone with the file can control and
> reconfigure every device. Do not share it or post it anywhere. Home Assistant keeps its copy readable by itself
> only, and leaves the keys out of diagnostics downloads. See also the [FAQ](faq.md#is-my-export-safe).

## What appears in Home Assistant

Every JUNG device of the export becomes a device in Home Assistant, named as in the app. A single JUNG device often
shows up as **several Home Assistant devices**, because the app shows it that way too:

- the **node device** — the JUNG device itself (a push-button, a socket, an actuator), with its firmware, its
  diagnostics and the settings that belong to the device as a whole; it is named after the one output (or, without
  an output, the one gang of keys) the app named on it, with the product behind, e.g. *WC mirror (Push-button
  1-gang)*, and otherwise after the product and its address, e.g. *2-channel actuator 0400*;
- a **light, socket or blind device** for each output, named as the load in the app — this is where you switch it;
- a **push-buttons device** per gang of keys, with an event entity per key (*Button A*, *Button B*, …; *Input E1* /
  *Input E2* on a mini actuator);
- one **mesh network device** for the whole installation, with *All lights*, *All sockets*, one *All lights in …*
  per room (hidden at first, see [Everyday use](everyday-use.md#lights)), the connection status, and the
  [mesh health](everyday-use.md#mesh-health-dashboard): *Mesh connection*, *Unreachable devices* and *Mesh overview*.

Each scene of the app becomes a scene entity. Many settings and diagnostics exist but are **disabled** at first, as
the app keeps them in its expert mode; the [entity reference](entities.md) lists them all and says which are on.

The first time Home Assistant sees the installation it asks every device for its state, so lights and sockets show
their real state within a few seconds. From then on it hears every change, whoever made it — a wall switch, the app,
a timer — usually within a second.

## Areas

Once the export is read, the setup asks **which Home Assistant area each room of the app goes to** (*Rooms and
areas*). Each room is prefilled with the area of the same name, or else the area that has the room's name as an
**alias** — so a room *Küche* lands in your area *Kitchen* when that area lists *Küche* as an alias, instead of a
second area appearing. Leave a room empty to get an area named after the room (created when there is none);
switch *Put the JUNG HOME devices in areas* off to leave every device without an area.

The devices then start in these areas:

- a light, socket or blind device in its first room's area;
- a push-buttons device in the room of the load next to it; a wall transmitter, which has no load, in the room its
  keys switch (**unverified on air**: none has been seen);
- a node device in the room of the first thing it carries; a room thermostat or detector in its own room;
- the gateway and the mesh network device in none.

This happens once, when the device first appears: afterwards move devices to any area you like.

To change the choice later, open **Settings → Devices & services → JUNG HOME (Bluetooth Mesh)**, the entry's menu,
**Reconfigure → Change which area each room's devices go to**. It moves the devices that are still in the area the
previous choice gave them, or in none, and says how many moved. **A device you placed in another area yourself is
never moved.**

Changing rooms in the app, or with the room actions, does not move devices — unless you switch on *Move devices
along when their JUNG room changes* in the integration's options (**Configure**; off by default). Then a device
whose room changes moves to the new room's area by the same rule: once Home Assistant takes over the app's new
export, or right after *Set room* and the other [room actions](changing-the-installation.md#rooms). **Unverified on
air.**

## Next steps

- Switch things and learn what each device offers: [Everyday use](everyday-use.md).
- Make the push-buttons do something in Home Assistant: [Buttons and automations](buttons-and-automations.md).
- Put the metering sockets on the Energy dashboard: [Energy](energy.md).
- Coming from the JUNG HOME Gateway integration? Its entities can be taken over with their history:
  [Changing the installation](changing-the-installation.md#coming-from-the-gateway-integration).
