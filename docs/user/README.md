# JUNG HOME (Bluetooth Mesh) — user guide

This integration lets Home Assistant control a JUNG HOME installation — lights, sockets, blinds, push-buttons,
room thermostats and detectors — directly over Bluetooth, without the JUNG HOME Gateway and without a cloud
account. It works next to the JUNG HOME app and the gateway: they keep working as before.

This guide answers "how do I…" questions in plain words. The [reference](../ha-integration.md) has every detail;
you do not need it to use the integration.

## Start here

1. [Getting started](getting-started.md) — what you need, installing, adding your installation, what appears in
   Home Assistant, areas.
2. [Everyday use](everyday-use.md) — lights, sockets, blinds, thermostats, scenes, detectors and battery devices.
3. [Buttons and automations](buttons-and-automations.md) — what your push-buttons report, device triggers,
   automations, and a rocker that only talks to Home Assistant.

## When you need it

- [Energy](energy.md) — power and energy of metering sockets, the Energy dashboard, thresholds.
- [Changing the installation](changing-the-installation.md) — rooms, key connections, scenes, schedules and new
  devices, from the app or from Home Assistant.
- [Maintenance](maintenance.md) — every repair notice and what to do about it, devices that go offline,
  diagnostics and debug logs, removing the integration.
- [FAQ](faq.md) — the gateway, the app, Bluetooth hardware, safety of the export, and more.
- [Entity reference](entities.md) — every entity the integration can create, generated from the code.

Auf Deutsch: [Schnellstart](../de/schnellstart.md). The integration itself speaks English and German: with Home
Assistant set to German, its pages, entities, actions and repair notices use the German JUNG HOME app's words
(*Taste*, *Wippe*, *Taster*, *Szene*), and a JUNG room is a *Raum* so it is not mixed up with a Home Assistant area
(*Bereich*). This guide quotes the English names ([Languages](../ha-integration.md#languages)).

## A word on "unverified"

The integration was built and is used on one real installation. Some devices (blinds, room thermostats,
detectors, battery wall transmitters, the energy puck) were not part of it, and some functions have not been tried
on real devices yet. Such places are marked **unverified** in this guide; they are written from the JUNG HOME app's
and the gateway's behaviour and are expected to work, but nobody has seen them do so yet. If you try one, please
report what happens in the repository's issue tracker.
