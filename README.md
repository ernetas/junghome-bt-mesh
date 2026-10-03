# junghome-bt-mesh — JUNG HOME over Bluetooth Mesh, without the gateway

`junghome_ble` is a Home Assistant integration that controls JUNG HOME lights, sockets, blinds, push-buttons,
thermostats and detectors directly over Bluetooth Mesh — through Home Assistant's Bluetooth adapter or an ESPHome
Bluetooth proxy, no JUNG HOME Gateway and no cloud needed. It works next to the JUNG HOME app and a gateway, and
keeps the gateway in sync.

- **Lights, sockets, blinds and thermostats** as Home Assistant entities, updated within about a second whoever
  switched them — a wall switch, the app, a timer.
- **Push-buttons** as event entities and device triggers, for automations of your own.
- **Energy** from metering sockets, ready for the Energy dashboard.
- **Rooms, key connections, scenes and schedules** changeable from Home Assistant, the way the app changes them.

Devices the maintainer does not own (blinds, room thermostats, detectors, battery transmitters, the energy puck)
are implemented from the app's and the gateway's behaviour and are not yet verified on real hardware — reports are
welcome.

## Install

- **HACS:** *HACS → Integrations → ⋮ → Custom repositories* → this repository's URL, category *Integration*; install
  *JUNG HOME (Bluetooth Mesh)* and restart Home Assistant.
- **By hand:** unpack `junghome_ble.zip` from the latest release into your configuration's `custom_components/`
  (the mesh library is inside it) and restart Home Assistant.

## Set up

*Settings → Devices & services → Add integration → JUNG HOME (Bluetooth Mesh)*, then fetch the network from your
JUNG HOME Gateway, or upload the export the JUNG HOME app shares (*Project → Share via file*, `JungHome.json`).
Everything else — devices, rooms, scenes, key connections — comes from that export. Step by step:
[Getting started](docs/user/getting-started.md).

## Documentation

- **[User guide](docs/user/README.md)** — getting started, everyday use, buttons and automations, energy, changing
  the installation, maintenance and repairs, FAQ, and the [entity reference](docs/user/entities.md).
- **[Schnellstart auf Deutsch](docs/de/schnellstart.md)**.
- **[Reference](docs/ha-integration.md)** — every device, entity, action, option, repair and known limitation in
  full detail.
- **[Developer documentation](docs/dev/README.md)** — architecture, testing, releases.
- **[Research notes](docs/research/README.md)** — how the JUNG HOME system was reverse-engineered, the command-line
  tools, the protocol and app notes.
- The `jhmesh` Bluetooth Mesh library inside the integration is also published on PyPI
  ([README-pypi.md](README-pypi.md)).

## Security

The app's export holds every key of your mesh: anyone with the file can control and reconfigure every device.
Keep it private. Home Assistant stores its copy readable by itself only and never puts a key into diagnostics or
logs. See [SECURITY.md](SECURITY.md) for what is stored where and how to report a vulnerability.

## Disclaimer & legal

This is an **independent, unofficial** project. It is **not** affiliated with, authorized, sponsored, or endorsed by
Albrecht JUNG GmbH & Co. KG. "JUNG", "JUNG HOME", and "LB Connect" are trademarks of their respective owner and are
used here **only descriptively** (nominative use) to identify the devices this software interoperates with.

- **Purpose — interoperability.** The code here is an independent implementation written against the public Bluetooth
  SIG Mesh specification, so that owners can operate **their own** devices without the vendor gateway. In the EU,
  studying, observing, and — where necessary — decompiling software to achieve interoperability of an independently
  created program is expressly permitted (Directive 2009/24/EC, Arts. 5(3) and 6), and Art. 8 renders contract terms
  that purport to forbid it unenforceable for that purpose.
- **No vendor software is redistributed here.** This repository contains **no** decompiled app source and **no**
  firmware images. The local `android/` decompile and the `ios/` app backup (which contains network keys) are
  developer-only inputs and are git-ignored — do not commit them. The integration's icon
  (`custom_components/junghome_ble/brand/`) is the JUNG HOME brand image as Home Assistant's brands repository
  publishes it for custom integrations; the JUNG and JUNG HOME names and marks belong to their owner.
- **Use with your own devices only.** Operating a Bluetooth Mesh network requires the network's keys, which belong to
  its owner. Use this only on devices and networks you own or are authorized to manage. You are responsible for your
  use of it.
- **No warranty.** Provided "as is" under the MIT License, without warranty of any kind. Interacting with device
  firmware carries risk (misconfiguration, loss of function, or voided manufacturer warranty). **Use at your own
  risk.**

See [DISCLAIMER.md](DISCLAIMER.md) for the full notice.
