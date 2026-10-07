# Everyday use

JUNG devices are ordinary Home Assistant entities: put them on dashboards, in scenes, scripts and automations, ask
a voice assistant to switch them. This page says what each kind of device can do. The full detail of every entity
is in the [reference](../ha-integration.md#supported-functionality).

## Lights

- **Switch inserts and actuator outputs** switch on and off.
- **Dimmer inserts** also take a brightness.
- **DALI tunable-white inserts** also take a colour temperature, within the range the light reports.

A light switched at the wall, from the app or by a timer shows its new state in Home Assistant within about a
second. Transitions (fading over a few seconds) are not offered yet: whether JUNG lights fade when asked is still to
be checked.

**All lights** (on the mesh network device) switches every JUNG light at once with one message, as the app's
"all luminaires" does; **All lights in ‹room›** does the same for one room of the app. Their state is "on" while any
of their lights is on.

The room entities — *All lights / sockets / blinds / thermostats in ‹room›* — start **hidden**: automatically
generated dashboards and voice assistants leave them out, since the room's own lights and sockets are there already.
They work all the same. To show one, open the mesh network device's page, *+ N entities not shown*, pick the entity,
open its settings (the cog) and switch on *Visible*; to let a voice assistant use it, expose it under *Settings →
Voice assistants → Expose*. An installation set up before version 1.1.0 keeps its room entities as they were.

**Dimming like a held key:** the actions *Start dimming*, *Stop dimming* and *Dim by a step*
(`junghome_ble.start_dim`, `stop_dim`, `step_dim`) dim a dimmer up or down the way a held rocker does — useful with
a button that should dim while it is held (see [Buttons and automations](buttons-and-automations.md#dim-a-light-while-a-key-is-held)).

## Sockets

A socket is a switch. A metering socket also measures power and energy — see [Energy](energy.md). **All sockets**
and **All sockets in ‹room›** switch many at once.

## Locked loads

A light or socket can be **locked**: in the JUNG HOME app, by a key set up to lock it, or with its *Lock* switch in
Home Assistant (a setting, off by default). A locked load keeps its state; Home Assistant then refuses to switch it
and says so, instead of sending a command the device would not carry out. The attribute `locked` shows the lock,
`lock_until` when a timed lock ends. A locked light was seen to keep its state and to announce its lock to the
mesh, which lets Home Assistant see a lock set elsewhere at once. A new brightness for a light that is on counts only
when the light shows it: a locked light that keeps its old level makes the action fail for the lock. Home
Assistant's own side of it is **unverified on air.**

Likewise, a load that a room thermostat switches, or the relay of a detector held on or off by the detector's own
controls, refuses commands with a message saying why — as the app greys out its buttons. **Unverified on
hardware**: no room thermostat or detector was available.

## Blinds

**Unverified on real blinds** — the maintainer owns none; please report what works.

Each blind, shutter or awning is a cover: open, close, stop, a position, and the slats for blinds with slats.
Position 100 % means fully open. **All blinds** and **All blinds in ‹room›** move many at once. The blind's settings
(running time, slat times, behaviour after a power cut, ventilation positions) are configuration entities of the
blind device. While a **wind alarm** or a lock holds the blind, it refuses commands; the *Wind alarm* sensor shows
it.

## Room thermostats

**Unverified on a real room thermostat** — please report.

Each room thermostat is a climate entity: target temperature (5–30 °C), the measured room temperature, whether it is
heating, and the presets *comfort*, *eco*, *frost protection* and *boost* (five minutes of full power). The mode
*auto* is the thermostat's own automatic operation. **All thermostats** sets the target temperature of every
thermostat at once. The thermostat's settings (preset temperatures, sensor, display, valve) are configuration
entities, disabled at first.

## Scenes

Every scene of the app is a scene entity. Activating it does exactly what the app does: every device that belongs to
the scene goes to what it stored. The scene's attributes list its members and what each one does. A scene recalled
from a key, the app or the gateway counts too: the entity shows the time of the last recall, from wherever it came.

You can make and change scenes from Home Assistant as well — see
[Changing the installation](changing-the-installation.md#scenes).

## Detectors

**Unverified on real detectors** — please report.

A motion or presence detector shows motion or occupancy and the light level it measures, on its node device. The
light it switches is an ordinary light. Its settings (brightness threshold, activation areas, walking test) are
configuration entities, most of them disabled at first.

## Battery devices

Battery wall transmitters and battery mini sensors sleep between presses, to save their battery. Their keys report
like any other key (see [Buttons and automations](buttons-and-automations.md)). Their battery level is read right
after one of their keys was pressed — the only moment they are awake — so it can be hours old. To change one of their
settings, press one of their keys first, then make the change right away. **Unverified on hardware.**

## Device names

Renaming a light, socket, blind or push-buttons device in Home Assistant writes the new name into the installation's
export the way the app's own rename does, and hands it to the gateway when there is one. The app shows the new name
once it loads that export (it never downloads the project by itself; see
[Changing the installation](changing-the-installation.md#how-the-app-learns-about-changes)). The app refuses empty
names, names longer than 30 characters and a `%` sign; such a name stays in Home Assistant only, and a repair notice
says so.

## Finding a device

Every mains-powered JUNG device has an **Identify** button (on its device page, under diagnostics): pressing it
makes the device's LED blink for ten seconds — handy to tell which mini actuator in a junction box is which.

## Fresh values on demand

Home Assistant hears every change the devices announce. Settings changed in the app are the exception: the device
tells only the app, and Home Assistant reads settings again three hours after it last read them (on a reconnect, or
within the hour on a connection that holds). To
see such a change at once, run the action *Update entity* (`homeassistant.update_entity`) on the entity.

## Firmware

Each JUNG device has a **Firmware** entity (disabled at first) that shows whether the JUNG HOME app has newer
firmware for it. Updates are installed **with the JUNG HOME app**: Home Assistant only compares versions and never
installs firmware itself.

## Mesh health dashboard

Four entities on the mesh network device tell how the installation is doing (**unverified on air**):

- **Mesh connection** is on while Home Assistant is connected to the mesh. Off means every JUNG entity is
  unavailable — see [Everything is unavailable](maintenance.md#everything-is-unavailable).
- **Unreachable devices** counts the mains-powered devices that do not answer, and names them in its attribute
  `devices`. A device counts from the moment it leaves a request unanswered (or, with the *Node heartbeats* option,
  stops sending its sign of life) until it is heard again; battery devices sleep and never count. It is unavailable
  while there is no connection: then *Mesh connection* is the one that tells. The
  [Device offline](buttons-and-automations.md#blueprints) blueprint turns it into a notification.
- **Mesh overview** (a diagnostic) shows how many mains-powered devices answer, and has a row per device in its
  attribute `nodes`: `name`, `area`, `product`, `reachable` (`true` / `false`; empty for a battery device, which
  sleeps), `last_seen`, `rssi` (the signal in dBm), `scanner` (the Bluetooth adapter or proxy that hears the device
  best), `hops` (with *Node heartbeats* on), and `proxy` (the device Home Assistant is connected through). It is
  updated at most once a minute, and the list is not kept in the history.
- **Mesh topology** (a diagnostic image) draws the mesh: Home Assistant at the top with the device it is connected
  through (the *link proxy*, thick border), and every other device in a band by how many hops its last sign of life
  took (*Hops: 1*, *Hops: 2*, …; with the *Node heartbeats* option on — without it, and for battery devices, under
  *Hops: not known*, whose heading says so when the option is off). A change of the connection is drawn at once. Each device shows its name, area and address; whether it answers as a shape, a colour and a
  word (a dot *reachable*, a cross *unreachable* with when it was last heard, a square *asleep* for a battery
  device); and its roles as letters and words (**R** relay, **P** proxy, **F** friend, **L** low power). The legend
  under the picture explains them; its words are in your Home Assistant's language (the one under *Settings →
  System → General*). It is redrawn only when something it shows changed, at
  most once a minute. The lines between devices are not drawn: the mesh does not report which device passes on
  whose messages.

The entity ids follow the name of your mesh network device: `sensor.jung_home_mesh_mesh_overview` below stands
for yours (*Settings → Devices & services → Entities*, search for *Mesh overview*).

**A table of every device.** Edit a dashboard, add a **Markdown** card, switch to the code editor and paste:

```yaml
type: markdown
title: JUNG HOME mesh
content: |
  {% set overview = 'sensor.jung_home_mesh_mesh_overview' %}
  {% set nodes = state_attr(overview, 'nodes') or [] %}
  **{{ states(overview) }}** of {{ nodes | rejectattr('reachable', 'none') | list | count }} mains-powered devices answer.

  | Device | Area | Answers | Last seen | Signal | Heard by | Hops |
  |:--|:--|:--|:--|--:|:--|--:|
  {% for n in nodes | sort(attribute='name') -%}
  | {{ n.name }}{{ ' (proxy)' if n.proxy else '' }} | {{ n.area or '–' }} | {{ 'asleep' if n.reachable is none else ('yes' if n.reachable else '**no**') }} | {{ time_since(as_datetime(n.last_seen)) ~ ' ago' if n.last_seen else '–' }} | {{ n.rssi ~ ' dBm' if n.rssi is not none else '–' }} | {{ n.scanner or '–' }} | {{ n.hops if n.hops is not none else '–' }} |
  {% endfor %}
```

**The picture of the mesh.** Add a **Picture entity** card (the stock one, nothing to install), switch to the code
editor and paste (`image.jung_home_mesh_mesh_topology` stands for yours: search for *Mesh topology*):

```yaml
type: picture-entity
entity: image.jung_home_mesh_mesh_topology
show_name: false
show_state: false
```

A large mesh makes a tall picture: put the card in a **Panel** view (or a wide section) so the names stay readable.
The picture follows your browser's dark mode.

**A notification when devices stop answering**, five minutes after it happens (the entity ids as above):

```yaml
alias: JUNG HOME devices not answering
triggers:
  - trigger: numeric_state
    entity_id: sensor.jung_home_mesh_unreachable_devices
    above: 0
    for:
      minutes: 5
  - trigger: state
    entity_id: binary_sensor.jung_home_mesh_mesh_connection
    to: "off"
    for:
      minutes: 5
actions:
  - action: persistent_notification.create
    data:
      title: JUNG HOME
      message: >-
        {% if is_state('binary_sensor.jung_home_mesh_mesh_connection', 'off') %}
        No connection to the mesh.
        {% else %}
        Not answering: {{ state_attr('sensor.jung_home_mesh_unreachable_devices', 'devices') | join(', ') }}
        {% endif %}
```

## While Home Assistant is not running

Nothing changes for the installation: the keys, the app, the gateway, timers and schedules keep working, because the
devices do the work themselves. When Home Assistant comes back it asks every device for its state again. Key presses
made in the meantime are not reported afterwards.
