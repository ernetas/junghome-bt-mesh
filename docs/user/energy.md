# Energy

## Which devices measure

- **Metering sockets** measure power, voltage and current, and count energy.
- The **energy puck** (switch actuator 1-gang 2-input energy) measures power and counts energy for its output —
  **unverified on hardware**.

Other JUNG devices do not measure.

## The sensors

| Sensor | What it shows |
|---|---|
| **Power** | What the load draws right now, in watts. Updated whenever it changes, within about a second. |
| **Energy** | Everything the load ever consumed, in kWh. Never goes backwards — the one for the Energy dashboard. Read every five minutes. |
| *Energy since reset* | The total the JUNG HOME app shows; *Reset consumption* (in the app, or the button here) sets it back to zero. Disabled at first. |
| *Energy since switched on* | Energy since the load was last switched on. Disabled at first. |
| *Voltage*, *Current*, *Power-on time* | Metering sockets only. Disabled at first. |
| *Installed* | When the socket was commissioned. |

Disabled sensors can be enabled on their entity page. The [entity reference](entities.md#sensors) lists all of them.

## Add a socket to the Energy dashboard

1. Open *Settings → Dashboards → Energy* (or the Energy dashboard's settings).
2. Under *Individual devices*, add a device and pick the socket's **Energy** sensor.

The dashboard's hourly bars come from the five-minute readings, so the live value is at most five minutes old.

## Why Home Assistant shows another number than the app

The app's consumption page shows the total **since the last reset** — the *Energy since reset* sensor here. The
*Energy* sensor counts from the day the socket was made and ignores resets, which is what the Energy dashboard needs.
Both are read from the socket itself; they only start at different points.

## After Home Assistant was down

While Home Assistant (or its Bluetooth connection) is down, nothing is recorded. When it comes back, the first
reading would put the whole gap's consumption into a single hour. The sockets keep their own hourly and daily
history, though — what the app draws its charts from — and Home Assistant reads it and fills in the missing hours
of the Energy dashboard, provided the history agrees with the energy counter; otherwise it leaves the gap as one
hour. How the socket's charts are laid out comes from the app and has not been checked on a real socket yet
(**unverified on air**).

## Washing machine finished

A metering socket's power drops to a few watts when the machine is done:

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

## Let a socket switch other devices by itself

A metering socket can switch other JUNG lights and sockets on its own when its power stays above or below a level for
a while — the app's *Automatic* profile. For example: when the TV's socket stays under 5 W for five minutes (standby),
switch off the soundbar and the lamp behind the TV. The socket does this without Home Assistant.

```yaml
action: junghome_ble.set_threshold
target:
  entity_id: switch.living_room_tv
data:
  threshold: switch_off
  power: 5
  duration: 300
  devices:
    - switch.living_room_soundbar
    - light.living_room_backlight
```

*Delete threshold* (`junghome_ble.delete_threshold`) removes both thresholds of a socket again. The *Switch-on
threshold* / *Switch-off threshold* sensors (disabled at first) show what is set. **Unverified on air**: not yet tried
on a real socket. More in the [reference](../ha-integration.md#actions-thresholds).
