# 45 — Mesh health at a glance, calmer dashboard

Phase P3 · Wave 12 · Size M · Closes: U4-7, U4-12 (U4 F8, F11); decision M9.

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
Claude-Session trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock times,
CHANGELOG bullet + docs update).

## Goal

One alertable entity says whether the mesh works and one says which devices do not; room central entities stop
cluttering auto-generated dashboards and Assist.

## Background

The integration (`custom_components/junghome_ble`) keeps reachability, last-seen, RSSI, hops and the proxy node on the
hub (`hub.unreachable`, `hub.node_alive`, `hub.last_seen`, `hub.node_rssi`, `hub.heartbeats`), but *Link state* is
disabled by default (`sensor.py:1077-1089`; brief 25 may have enabled it), the node diagnostics are per node and
disabled (`sensor.py:1133-1170`), and nothing counts offline devices. Every room gets *All lights / sockets / blinds /
thermostats in <room>* entities, deliberately outside areas, so they land as unassigned on auto dashboards and are
exposed to Assist; nothing sets `entity_registry_visible_default` (`docs/ha-integration.md:92-99`).

## Read first

`binary_sensor.py`, `sensor.py` (link state, `NODE_DIAGNOSTICS`, `JungHomeNodeDiagnostic` rate limit),
`coordinator.py` (reachability, signals `SIGNAL_CONNECTION`), the central entities in `light.py`, `switch.py`,
`cover.py`, `climate.py`, `entity.py` (`JungHomeCentralEntity`), `tests/test_snapshots.py`.

## Steps

1. `binary_sensor` *Mesh connection* (device class `connectivity`) on the mesh device, enabled by default, always
   available, on while `hub.link_available`.
2. `sensor` *Unreachable devices*: count of mains nodes unreachable or not alive; attribute `devices` (names),
   unrecorded.
3. `sensor` *Mesh overview*: state = reachable mains nodes; attribute `nodes` = list of `{name, area, product,
   reachable, last_seen, rssi, scanner, hops, proxy}`, unrecorded (`_unrecorded_attributes`), updated at most once a
   minute. `scanner`: best-RSSI scanner from `bluetooth.async_scanner_devices_by_address(hass, mac, connectable=True)`.
   Battery nodes appear with `reachable: null` (asleep).
4. Room central entities: `_attr_entity_registry_visible_default = False` (new registrations only); the network-wide
   *All …* stay visible (decision M9).
5. Docs: a "Mesh health dashboard" section with a ready Markdown card rendering the overview table from
   `state_attr('sensor.<mesh>_mesh_overview', 'nodes')`; how to unhide central entities.

## Tests to add

Values against the fake link (connected, disconnected; a node unreachable then heard; heartbeats on and a node dead);
attributes unrecorded; the visible-default flag in the snapshot; the documented card template rendered with HA's
template engine in a test.

## Acceptance criteria

All gates green; a fresh install shows *Mesh connection* and *Unreachable devices* on the mesh device and hides room
central entities; the snapshot diff is limited to the new entities and `hidden_by`.

## Verifiable on air here?

Yes: switch off the breaker of a light or socket and watch *Unreachable devices*; reconnect. A person at home flips the
breaker.

## Risks / off-by-default / "unverified on air"

Hidden-by-default applies to new registrations only (say so). The overview attribute can be large on a big mesh: keep
it unrecorded and rate-limited.

## Depends on

Brief 25 (link state default, diagnostics); richer after brief 47 (areas). Decision M9.

## Files touched

`binary_sensor.py`, `sensor.py`, central entities in `light.py`, `switch.py`, `cover.py`, `climate.py`, `entity.py`,
`strings.json`, `translations/en.json`, `icons.json`, `docs/user/`, `docs/ha-integration.md`, `CHANGELOG.md`, tests,
`tests/snapshots/`.
