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

**Dimming like a held key:** the actions *Start dimming*, *Stop dimming* and *Dim by a step*
(`junghome_ble.start_dim`, `stop_dim`, `step_dim`) dim a dimmer up or down the way a held rocker does — useful with
a button that should dim while it is held (see [Buttons and automations](buttons-and-automations.md#dim-a-light-while-a-key-is-held)).
**Unverified on air.**

## Sockets

A socket is a switch. A metering socket also measures power and energy — see [Energy](energy.md). **All sockets**
and **All sockets in ‹room›** switch many at once.

## Locked loads

A light or socket can be **locked**: in the JUNG HOME app, by a key set up to lock it, or with its *Lock* switch in
Home Assistant (a setting, off by default). A locked load keeps its state; Home Assistant then refuses to switch it
and says so, instead of sending a command the device ignores. The attribute `locked` shows the lock, `lock_until`
when a timed lock ends. **Unverified on air.**

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
tells only the app, and Home Assistant reads settings again only when it reconnects, at most every three hours. To
see such a change at once, run the action *Update entity* (`homeassistant.update_entity`) on the entity.

## Firmware

Each JUNG device has a **Firmware** entity (disabled at first) that shows whether the JUNG HOME app has newer
firmware for it. Updates are installed **with the JUNG HOME app**: Home Assistant only compares versions and never
installs firmware itself.

## While Home Assistant is not running

Nothing changes for the installation: the keys, the app, the gateway, timers and schedules keep working, because the
devices do the work themselves. When Home Assistant comes back it asks every device for its state again. Key presses
made in the meantime are not reported afterwards.
