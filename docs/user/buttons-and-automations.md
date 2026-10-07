# Buttons and automations

Every key of a JUNG push-button, wall transmitter or mini actuator input has an **event entity** in Home Assistant
(*Button A*, *Button B*, …, *Input E1*, *Input E2*), on the push-buttons device of its gang. What a key reports
depends on what it is connected to in the JUNG HOME app.

## What your keys report

| The key is connected to… (in the app) | Home Assistant receives |
|---|---|
| **the JUNG HOME Gateway** (key mode *Gateway*) | `click`, `double_click`, `hold_start` (held down) and `hold_end` (released) — for a rocker, separately for its upper and lower half |
| **a light, a socket or a room** (key modes *Light*, *Switch*) | `press_on` (upper half) and `press_off` (lower half); while it dims, `dim`, and `hold_start` / `hold_end` around the dimming |
| **a scene** | `scene`, with the scene's number |
| **nothing** (*No function*) | nothing |

Only what the key actually sends can be reported: a key connected to a light sends nothing that tells a single
click from a double click. A key cannot be connected to two things at once. The event entity's attribute
`connection` says what the key is connected to.

- Want clicks and double clicks? Connect the key to the gateway in the app, or with Home Assistant's
  *Assign key* action (target: the gateway device) — this needs a gateway in the installation.
- No gateway, or want the key to do only Home Assistant things? See
  [A key that only talks to Home Assistant](#a-key-that-only-talks-to-home-assistant).
- A key keeps doing its JUNG job (switching its light) **and** reports to Home Assistant: you can add automations to
  a key that switches a light, without changing it.

## Make an automation from a key

The easiest way is a **device trigger**: *Settings → Automations & scenes → Create automation → Add trigger →
Device*, pick the push-buttons device, then the key and what it should react to — *Button A clicked*, *Button A
double-clicked*, *Button A hold started*, *Button A pressed on / up*, … Only what the key can report is offered.

Another way is the key's **event entity** with Home Assistant's *Event received* trigger (pick the entity, then
the event types). In YAML:

```yaml
alias: Hall key double click turns everything off
triggers:
  - trigger: event.received
    target:
      entity_id: event.hall_buttons_button_a
    options:
      event_type: double_click
actions:
  - action: light.turn_off
    target:
      entity_id: all
```

Entity IDs follow the names in your app; look them up under *Settings → Devices & services → Entities*.

Both kinds keep working when the key's event entity is disabled. For **one automation for many keys**, use a
trigger on the event `junghome_ble_button_action`: it carries the key (`key`), the device (`device_id`) and what
happened (`type`). The [reference](../ha-integration.md#event) lists everything it carries.

### Single click and double click

By default a double press reports a `click` for the first press and a `double_click` for the second, so an
automation on `click` also runs on a double press. If a key should do one thing on a click and another on a double
click, pick it under **Keys that wait for a double click** in *Settings → Devices & services → JUNG HOME Bluetooth
Mesh → Configure*. That key then reports its clicks half a second late, and a double press only the
`double_click`; every other key keeps reporting its clicks at once. The key's event entity shows it with the
attribute `waits_for_double_click`. **Unverified on air** per key. The press that completes a double click starts
nothing new: four quick presses are two double clicks, three a double click and a click.

The trade-off is that half second: a light switched on a `click` of a key that waits comes on half a second later.
Pick only the keys with a double-click automation. The list shows the keys that can click while the integration is
running; a key you picked that is removed or wired to something else drops out the next time you save the options.
The older switch **Report clicks only once a double click is ruled out** still makes every key wait.

### Dim a light while a key is held

`hold_start` comes when the key is held down, `hold_end` when it is released. To dim another (non-JUNG) light while
a key is held, start a dimming loop on `hold_start` and stop it on `hold_end`. A JUNG dimmer can be dimmed the
same way with *Start dimming* on `hold_start` and *Stop dimming* on `hold_end` (the two actions have dimmed and
stopped a real dimmer; this automation is **unverified on air** with a real key):

```yaml
alias: Key B dims the desk lamp
triggers:
  - trigger: event.received
    target:
      entity_id: event.study_buttons_button_b
    options:
      event_type: hold_start
    id: start
  - trigger: event.received
    target:
      entity_id: event.study_buttons_button_b
    options:
      event_type: hold_end
    id: stop
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: start
        sequence:
          - action: junghome_ble.start_dim
            target:
              entity_id: light.desk_lamp
            data:
              direction: up
    default:
      - action: junghome_ble.stop_dim
        target:
          entity_id: light.desk_lamp
```

A hold always ends, even when its release is never heard (the Bluetooth connection dropped, or Home Assistant
restarted): then `hold_end` comes with a `reason` attribute (`timeout` after 30 seconds, `link_lost`, `stopped`).
**Unverified on air.**

## A key that only talks to Home Assistant

**Unverified on air** — this recipe has not been tried on a real key yet. It needs no gateway.

A key that is connected to an empty room switches nothing by itself, but still reports `press_on` / `press_off`
(and `dim` while held) to Home Assistant. Two actions set that up (*Developer tools → Actions*; both are for
administrators):

1. **Create the room** — action *Create room* (`junghome_ble.create_room`) with the name `Home Assistant`:

   ```yaml
   action: junghome_ble.create_room
   data:
     name: Home Assistant
   ```

2. **Connect the key to it** — action *Assign key* (`junghome_ble.assign_key`) with the key's event entity, the
   room and the mode `light`:

   ```yaml
   action: junghome_ble.assign_key
   data:
     key_entity: event.hall_buttons_button_a
     room: Home Assistant
     mode: light
   ```

The key's previous connection is removed; from now on its upper half reports `press_on`, its lower half
`press_off`, and holding it reports `dim`. Use them in device triggers (*Button A pressed on / up*, …) or with
*Event received*. Leave the room empty: a light put into it would be switched by the key again. The room shows up
in the app once the app loads an export that has it.

**To undo it**, connect the key back to what it did before with *Assign key* (its light, socket or room), or give it
no function with *Clear key* (`junghome_ble.clear_key`). When no key uses the room any more, delete it with *Delete
room* (`junghome_ble.delete_room`).

## Blueprints

Blueprints are ready-made automations: you import one once, then make an automation from it by filling in a form —
which key, which lights — without writing YAML. The project offers six. **Unverified on air:** none of the key
blueprints has been run with a real key yet, and the offline blueprint has not seen a device lose power.

| Blueprint | What it does | |
|---|---|---|
| [Key switches and dims lights](../../blueprints/automation/junghome_ble/rocker_light_control.yaml) | Any lights, JUNG or not: upper half on, lower half off, a single key toggles; holding a half dims up or down in steps until released; an optional double-click action | [Import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fernetas%2Fjunghome-bt-mesh%2Fmain%2Fblueprints%2Fautomation%2Fjunghome_ble%2Frocker_light_control.yaml) |
| [Key dims a JUNG light](../../blueprints/automation/junghome_ble/rocker_dim_jung_light.yaml) | A JUNG dimmer or tunable-white light dims smoothly while a key is held (*Start dimming* / *Stop dimming*), the way a rocker wired to it does | [Import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fernetas%2Fjunghome-bt-mesh%2Fmain%2Fblueprints%2Fautomation%2Fjunghome_ble%2Frocker_dim_jung_light.yaml) |
| [Key runs up to six actions](../../blueprints/automation/junghome_ble/rocker_scene_selector.yaml) | A scene, script or anything else for each of click, double click and hold, on the upper and the lower half | [Import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fernetas%2Fjunghome-bt-mesh%2Fmain%2Fblueprints%2Fautomation%2Fjunghome_ble%2Frocker_scene_selector.yaml) |
| [Lights on with presence](../../blueprints/automation/junghome_ble/presence_lighting.yaml) | Lights on when a motion, occupancy or presence sensor detects someone (optionally only below an illuminance), off a set time after it clears | [Import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fernetas%2Fjunghome-bt-mesh%2Fmain%2Fblueprints%2Fautomation%2Fjunghome_ble%2Fpresence_lighting.yaml) |
| [Appliance finished](../../blueprints/automation/junghome_ble/appliance_finished.yaml) | A notification when a washing machine, dryer or dishwasher on a metering socket has finished (see [Energy](energy.md#washing-machine-finished)) | [Import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fernetas%2Fjunghome-bt-mesh%2Fmain%2Fblueprints%2Fautomation%2Fjunghome_ble%2Fappliance_finished.yaml) |
| [Device offline](../../blueprints/automation/junghome_ble/device_offline_notify.yaml) | A notification when a JUNG device has stopped answering for a set time (from *Unreachable devices*, see [Mesh health dashboard](everyday-use.md#mesh-health-dashboard)), and optionally another when it answers again | [Import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fernetas%2Fjunghome-bt-mesh%2Fmain%2Fblueprints%2Fautomation%2Fjunghome_ble%2Fdevice_offline_notify.yaml) |

**Before you use the key blueprints:**

- Clicks, double clicks and holds of each half need a key **connected to the gateway** (see
  [What your keys report](#what-your-keys-report)), or a key connected to an empty room as in
  [A key that only talks to Home Assistant](#a-key-that-only-talks-to-home-assistant) (unverified on air) — that
  key reports presses and holds, no clicks.
- A key **connected to a light or socket** keeps switching it; the blueprints add to what it does. Its upper and
  lower half (`press_on` / `press_off`) count as the clicks of the upper and lower half.
- Keep the key's **event entity enabled**: the blueprints pick the key by it.
- The presence blueprint takes any motion, occupancy or presence sensor, from any integration. A JUNG detector's
  should work as well; there was none to try it with.

**Import:** the *Import* links above open the import dialog of your own Home Assistant (through
my.home-assistant.io); confirm the address it shows and choose *Preview* → *Import blueprint*. Then *Settings →
Automations & scenes → Blueprints*, pick the blueprint and *Create automation*. A blueprint imported this way can
be updated later with *Re-import blueprint* in its menu.

**Manual install:** copy the YAML files from
[`blueprints/automation/junghome_ble/`](../../blueprints/automation/junghome_ble/) into
`<config>/blueprints/automation/junghome_ble/` of your Home Assistant (create the folder; `<config>` is the folder
that holds `configuration.yaml`). They show up under *Settings → Automations & scenes → Blueprints*; a file replaced
later takes effect after *Reload automations* (*Developer tools → YAML*) or a restart.

## More examples

**Notify when the washing machine has finished** (on a metering socket): see [Energy](energy.md#washing-machine-finished).

**Recall a JUNG scene at sunset:**

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

**Switch a room off by area:** JUNG lights are ordinary lights, so `light.turn_off` with an `area_id` works.

**React to a scene recalled anywhere** (a key, the app, the gateway or Home Assistant): the event
`junghome_ble_scene_recalled` carries the scene's number and name.
