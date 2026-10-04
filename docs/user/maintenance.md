# Maintenance

## When devices go offline

### Everything is unavailable

Home Assistant has no Bluetooth connection to the installation. The *Link state* sensor on the mesh network device
says where it stands (*Searching for a proxy node*, *Connecting*, *Connection failed*, *No Bluetooth*, …).

- Check that the Bluetooth adapter or the ESPHome proxy is on and online (*Settings → Devices & services →
  Bluetooth*), and that an ESPHome proxy has `bluetooth_proxy: active: true`.
- Make sure a **mains-powered** JUNG device is within its reach (see
  [Getting started](getting-started.md#where-to-put-the-adapter-or-proxy)).
- After a short drop, entities stay available for 20 seconds while Home Assistant reconnects; a command given
  meanwhile waits for the new connection.

### One device is unavailable

A device that does not answer three times in a row is marked unavailable, as the app shows *No connection*. It
comes back by itself the moment it is heard again (a status, a key press, an answer); Home Assistant asks it again
every five minutes. Check its power (a tripped breaker, a switched-off socket strip). Battery devices are never marked
unavailable: they sleep.

With the **Node heartbeats** option on (*Settings → Devices & services → JUNG HOME Bluetooth Mesh → Configure*),
every mains-powered device sends Home Assistant a short sign of life every minute, so one that loses power is marked
unavailable within about three and a half minutes even when nobody uses it — and comes back by itself when power
returns. The option is off by default; switch it off before you remove the integration.

### Entities keep switching between available and unavailable

The connection to the JUNG device Home Assistant talks through is weak. The *Proxy node* sensor shows which one it
is. Move the adapter or proxy closer to a mains-powered JUNG device, or add a second ESPHome proxy elsewhere; make
sure the proxy has a free connection slot (other Bluetooth integrations use them too). The
[diagnostics](#diagnostics) list the last 20 connections and why each ended.

### A light or sensor shows "unknown"

It has not reported yet. Lights and sockets are asked once after every connection; switching one from the wall or
the app makes it report. Event entities stay *unknown* until their first key press.

## Setup problems

The messages the setup dialog can show, and what to do:

- **"No node of this mesh network is currently visible over Bluetooth"** — no device of the installation is in
  reach, or the ESPHome proxy lacks `active: true`, or the export is from another installation. Wait a minute after
  a restart and try again.
- **"The mesh export could not be read"** — the file is not the app's export, or (for a path) the path is wrong:
  it must be valid on the Home Assistant host, for example `/config/junghome/JungHome.json`.
- **"The network's keys were renewed after the export was made"** — the app renewed the network key since. Export
  the network again (or fetch it from the gateway again) and use the new one.
- **"The export belongs to a different mesh network"** — the file is from another installation.
- **"This mesh is already set up as another entry"** — use that entry's *Reconfigure* instead of adding it again.
- **"That address belongs to a node in the mesh"** — open *Advanced* and pick another *Our unicast address*, such as
  `0D02`.
- **"The gateway is busy with another configuration request"** — the app or another client is changing the
  installation through the gateway; submit again in a minute.
- **"The access request was not approved in time"** — approve *Home Assistant (Bluetooth Mesh)* in the app under
  *Settings → Gateway → Access permissions → Open requests* within three minutes, then submit again.
- **"The gateway holds no network export"** — open the JUNG HOME app once while it is connected to the gateway (it
  then hands its project over); gateways with firmware older than 2.1 cannot do this, use the upload instead.

More in the [reference](../ha-integration.md#troubleshooting).

## Repair notices

Home Assistant shows these under *Settings → System → Repairs*. Each notice's *Learn more* link opens its entry
below; each entry says what the notice means, what to do, and links to the full explanation in the reference. Most
clear themselves once the cause is gone. Those that can be fixed in place have a **Submit** button: the notice
explains what it will do, and nothing changes until you confirm.

### The export and the devices

#### JUNG HOME devices missing from the export

A device of your installation is new to Home Assistant: it was added in the app after the export was made. Set up
from the gateway, Home Assistant fetches the new export by itself; if the notice stays, open the app once while it is
connected to the gateway. **Submit** loads the new export: set up from the gateway, it fetches it again; set up from
a file, it asks you to upload the app's new export (*Project → Share via file*). **Unverified on air.**
[Details](../ha-integration.md#repair-issue-jung-home-devices-missing-from-the-export)

#### The JUNG HOME app changed the installation

Only for an entry set up from a file: Home Assistant saw the app change a device's configuration (a key connection, a
room, a scene), so its export may be behind. Once you are done in the app, **Submit** asks for the app's new export
(*Project → Share via file*); *Reconfigure* clears the notice too. An entry set up from the gateway fetches the new
export by itself instead. **Unverified on air.**
[Details](../ha-integration.md#repair-issue-the-jung-home-app-changed-the-installation)

#### JUNG HOME push-buttons with another insert than in the export

A push-button's insert was replaced (a dimmer instead of a switch, say). Check it in the app, export again and
*Reconfigure*. Seen raised for a real difference; that it clears is **unverified on air.** [Details](../ha-integration.md#repair-issue-jung-home-push-buttons-with-another-insert-than-in-the-export)

#### Device name not passed on to the JUNG HOME app

You gave a device a name the app does not accept (empty, over 30 characters, or with a `%` sign). **Submit** asks
for another name and checks it before anything changes; renaming the device again works too. **Unverified on air.**
[Details](../ha-integration.md#repair-issue-device-name-not-passed-on-to-the-jung-home-app)

#### Take over the JUNG HOME Gateway integration's entities

The gateway integration is set up too, so everything exists twice. Take its entities over, or ignore the notice.
See [Changing the installation](changing-the-installation.md#coming-from-the-gateway-integration).
[Details](../ha-integration.md#repair-issue-take-over-the-jung-home-gateway-integrations-entities)

#### Two entries cover the same JUNG HOME mesh

The same installation was added twice; only one entry can run. Delete one of them; if you deleted the one that
was running, reload the other. [Details](../ha-integration.md#repair-issue-two-entries-cover-the-same-jung-home-mesh)

### Changes made from Home Assistant

#### A JUNG HOME change on … was interrupted

Home Assistant stopped in the middle of a change (a crash, a power cut). What the devices had taken is recorded; run
the same action again to finish it. [Details](../ha-integration.md#repair-issue-a-jung-home-change-on--was-interrupted)

#### The JUNG HOME app overrode a change Home Assistant made on …

The app changed the same thing as Home Assistant (re-linked the same key, say); the app's version was kept. Check the
listed entries and set them again from one side. **Unverified on air.**
[Details](../ha-integration.md#repair-issue-the-jung-home-app-overrode-a-change-home-assistant-made-on-)

#### Devices still hold a deleted JUNG HOME scene on …

A scene was deleted while some of its devices could not be reached. When they are back, run *Delete unused scenes*
with *Dry run* off. [Details](../ha-integration.md#repair-issue-devices-still-hold-a-deleted-jung-home-scene-on-)

#### JUNG HOME export not handed to the gateway

A change could not be passed to the gateway. Home Assistant tries twice more; if the notice stays, check that the
gateway is reachable, then **Submit** the notice: it hands the export over as *Sync gateway*
(`junghome_ble.sync_gateway`) does. **Unverified on air.**
[Details](../ha-integration.md#repair-issue-jung-home-export-not-handed-to-the-gateway)

### The gateway

#### JUNG HOME Gateway certificate changed

The gateway presents another security certificate than before. If you replaced the gateway, *Reconfigure* and fetch
from it again while Home Assistant is connected to the installation; otherwise another device on your network may be
pretending to be the gateway. The devices keep working.
[Details](../ha-integration.md#repair-issue-jung-home-gateway-certificate-changed)

#### JUNG HOME Gateway no longer accepts Home Assistant

Home Assistant's access was removed in the app. Follow the re-authentication Home Assistant offers: enter the
network-key password or approve the new request in the app.
[Details](../ha-integration.md#repair-issue-jung-home-gateway-no-longer-accepts-home-assistant)

### Bluetooth and Home Assistant's address

#### No Bluetooth for the JUNG HOME mesh

No Bluetooth adapter or proxy is available. Check *Settings → Devices & services → Bluetooth*.
[Details](../ha-integration.md#repair-issue-no-bluetooth-for-the-jung-home-mesh)

#### JUNG HOME devices ignore Home Assistant

The devices drop Home Assistant's messages: another program uses the same address (a second Home Assistant, the
command-line tools), or Home Assistant's counter store was lost. Give Home Assistant a new address under
*Reconfigure*, or, if the store was lost, submit the notice.
[Details](../ha-integration.md#repair-issue-jung-home-devices-ignore-home-assistant)

#### Another client uses Home Assistant's JUNG HOME address

Something else sends from Home Assistant's address, so Home Assistant has stopped sending. Stop the other program or
give one of the two a new address, then submit the notice. (*… still uses …* means it was seen again: give Home
Assistant another address.) [Details](../ha-integration.md#repair-issue-another-client-uses-home-assistants-jung-home-address)

#### Home Assistant's JUNG HOME address is taken

A device of the installation has Home Assistant's address, so the integration does not start. **Submit** moves
Home Assistant to the free address the notice suggests and starts it again (the same as *Reconfigure → Advanced → Our
unicast address*); make sure nothing else, such as the command-line tools, sends from that address. **Unverified on
air.** [Details](../ha-integration.md#repair-issue-home-assistants-jung-home-address-is-taken)

#### Home Assistant's JUNG HOME address may be handed out

Home Assistant's address lies where the app may put a new device later. It works until then; move Home Assistant to
the free address the notice suggests, under *Reconfigure*.
[Details](../ha-integration.md#repair-issue-home-assistants-jung-home-address-may-be-handed-out)

### Keys and security

#### JUNG HOME mesh keys are changing

A key renewal is running that Home Assistant did not see start. Let it finish in the app, then **Submit** the
notice: it loads the new export, fetched from the gateway again or uploaded (**unverified on air**); *Reconfigure*
does the same. [Details](../ha-integration.md#repair-issue-jung-home-mesh-keys-are-changing)

#### JUNG HOME mesh keys have changed

The network key was renewed while Home Assistant was not running; nothing can be read any more. **Submit** loads
the new export, fetched from the gateway again or uploaded (**unverified on air**); *Reconfigure* does the same.
[Details](../ha-integration.md#repair-issue-jung-home-mesh-keys-have-changed)

### Sequence numbers

Every message carries a running number that must never repeat; Home Assistant stores its counter in its
configuration folder. Never restore that store (`.storage/junghome_ble.seq.*`) from an old copy by hand.

#### Sequence numbers of the JUNG HOME mesh … lost

The counter store has no usable record for Home Assistant's address, so the integration does not start. **Submit**
the notice: Home Assistant continues far past every number it may have used and starts again.
[Details](../ha-integration.md#repair-issue-sequence-numbers-of-the-jung-home-mesh--lost)

#### JUNG HOME sequence numbers cannot be saved

The disk is full or became read-only (a failing SD card, for example), so Home Assistant holds its messages back.
Free up space or repair the storage. [Details](../ha-integration.md#repair-issue-jung-home-sequence-numbers-cannot-be-saved)

#### JUNG HOME mesh sequence numbers running low

A device of the installation has used most of its numbers. The mesh moves on to new ones with an IV Update, which
Bluetooth Mesh expects the device running low to start itself; that JUNG HOME devices do is unverified on air. Home
Assistant follows the update, and if the repair stays open an administrator can have Home Assistant start one with
the action `junghome_ble.start_iv_update` (*Developer tools → Actions*, with `confirm: true`). It cannot be undone:
the IV index only goes up, and the mesh is back in normal operation 96 to 144 hours later. That a JUNG device takes
an IV Update from Home Assistant is unverified on air. Every restart of a device (a power cut, a tripped breaker)
skips its numbers far ahead, so a device that often loses power runs low first.
[Details](../ha-integration.md#repair-issue-jung-home-mesh-sequence-numbers-running-low),
[the action](../ha-integration.md#actions-iv-update)

#### JUNG HOME mesh is at another IV index

Home Assistant's counters are out of step with the installation (it was away for a very long time, or its store
belongs elsewhere). Give Home Assistant a new address under *Reconfigure*. When the notice says *Home Assistant is
ahead*, submit it instead. [Details](../ha-integration.md#repair-issue-jung-home-mesh-is-at-another-iv-index)

### Clocks

#### JUNG HOME devices with a wrong clock

Devices that run schedules have a wrong time or time zone. Submit the notice to set their clocks again; if it comes
back, check Home Assistant's time zone (*Settings → System → General*). **Unverified on air.**
[Details](../ha-integration.md#repair-issue-jung-home-devices-with-a-wrong-clock)

#### JUNG HOME pucks have no time keeper

The installation has older actuator pucks that need another device to pass the time on to them. Enable and turn on
the *Time keeper* switch of one always-powered device (a socket or a mini actuator). **Unverified on hardware.**
[Details](../ha-integration.md#repair-issue-jung-home-pucks-have-no-time-keeper)

### Devices Home Assistant added (experimental)

#### A device Home Assistant added is not recorded

Adding a device stopped half-way. Run *Reset pending device* with the address the notice names, then add the device
again. [Details](../ha-integration.md#repair-issue-a-device-home-assistant-added-is-not-recorded)

#### JUNG HOME device keys cannot be saved

The disk could not take the new device's key, so the device was not added. Free up space or repair the storage,
then add it again. [Details](../ha-integration.md#repair-issue-jung-home-device-keys-cannot-be-saved)

#### Devices Home Assistant added missed the new network key

After a key renewal, a device Home Assistant added has not confirmed the new key. Make sure it is powered and in
reach; Home Assistant keeps trying. [Details](../ha-integration.md#repair-issue-devices-home-assistant-added-missed-the-new-network-key)

## Diagnostics

*Settings → Devices & services → JUNG HOME Bluetooth Mesh → ⋮ → Download diagnostics* saves a file with the
state of the connection, the devices, the last connections and the mesh as the *Mesh topology* picture shows it
(`topology`) — never a key, with Bluetooth addresses and file
paths left out.
Each device's page has its own *Download diagnostics* too. Attach it when you report a problem.

## Debug logging

*⋮ → Enable debug logging* on the integration's page; reproduce the problem; select it again to stop and download
the log. It shows what Home Assistant sends to and hears from the devices (never a key).

That log has a line for every message on the mesh. To keep it shorter, or to see only the messages, set the loggers
in `configuration.yaml` instead:

```yaml
logger:
  logs:
    custom_components.junghome_ble: debug  # connections and what the integration decides
    jhmesh: debug  # the Bluetooth link: proxy, filter, lost links, unanswered requests
    jhmesh.trace: info  # one line per message sent or heard: `debug` to see them
```

With `jhmesh: warning` and `jhmesh.trace: debug` the log shows the messages alone. The
[reference](../ha-integration.md#enabling-debug-logging) lists every logger.

## Backups

Home Assistant backups include everything the integration needs. Restoring one is safe: Home Assistant notices the
restore and continues its counters far enough ahead (**unverified on air**). Do not restore the
integration's files by hand from an older copy.

## Removing the integration

1. Turn off the *Node heartbeats* option first, if you turned it on.
2. *Settings → Devices & services → JUNG HOME Bluetooth Mesh → ⋮ → Delete*.
3. Delete the export file from the host if you pointed the integration at one by path: it contains every key.

Nothing has to be undone in the JUNG HOME app. What Home Assistant changed on the devices (rooms, key connections,
scenes, settings) stays, like a change made in the app. Home Assistant keeps its counter store on purpose, in case you
set it up again. The [reference](../ha-integration.md#removal) lists every file it leaves.
