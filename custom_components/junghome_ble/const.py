"""Constants for the JUNG HOME Bluetooth Mesh integration."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .schedules import Scheduler

DOMAIN: Final = "junghome_ble"

CONF_CDB_PATH: Final = "cdb_path"
CONF_METADATA_DIR: Final = "metadata_dir"
CONF_UNICAST: Final = "unicast"
CONF_MESH_UUID: Final = (
    "mesh_uuid"  # the export's meshUUID: the identity that survives a NetKey refresh
)
CONF_SOURCE: Final = "source"  # where the export came from: "gateway", "upload" or "path"; every flow sets it.
# An entry from before this key existed gets one from `__init__.async_migrate_entry` (entry version 1.2), judged by
# `config_flow.infer_source`; `async_remove_entry` judges a never-migrated entry the same way.
CONF_GATEWAY_HOST: Final = "gateway_host"
CONF_GATEWAY_FINGERPRINT: Final = (
    "gateway_fingerprint"  # SHA-256 hex of the pinned gateway certificate
)
# where the pinned certificate came from: the gateway node vouched for it over the mesh (`0xC003`), or the user trusted
# it in the config flow (learned at first contact, or confirmed as the new one); an entry without it is unverified.
# Only a pin the mesh vouched for is used at runtime (`JungHomeHub.async_gateway_distrust`) and it cannot be
# overridden by one click in the flow. Not in HUB_DATA_KEYS: recording it never reloads.
CONF_GATEWAY_PIN_SOURCE: Final = "gateway_pin_source"
PIN_FROM_MESH: Final = "mesh"
PIN_FROM_USER: Final = "user"
CONF_GATEWAY_TOKEN: Final = "gateway_token"  # noqa: S105 - the gateway's API token: it hands out the export with every mesh key, so it is redacted like the keys
CONF_GATEWAY_PASSWORD: Final = "gateway_password"  # noqa: S105 - form field only, never stored
CONF_EXPORT_FILE: Final = "export_file"  # form field only: the id of the uploaded file
# the digest of the export a new gateway entry was fetched with: the first `mesh_config.GatewaySync` record of the entry
# (review-4 H I-10: every sync rewrote it in `entry.data` up to 1.0.0; the record took it over, the key is left in
# place for a downgrade). Not in HUB_DATA_KEYS: a reconfigure writing it never reloads by itself
CONF_GATEWAY_SYNCED: Final = "gateway_synced_digest"
# when Home Assistant last handed its export to the gateway (ISO 8601, UTC), the app's `gateway_last_sync`: in
# `entry.data` up to 1.0.0, taken over by the `GatewaySync` record like `CONF_GATEWAY_SYNCED`
CONF_GATEWAY_LAST_SYNC: Final = "gateway_last_sync"
# the mains nodes that may still publish heartbeats to us (`OPTION_HEARTBEATS` configured them, and no disable round
# got their OK yet), unicasts as hex: not in HUB_DATA_KEYS either; the next link with the option off tells them
CONF_HEARTBEATS_PUBLISHING: Final = "heartbeats_publishing"

# The entry data the hub is built from (`__init__.async_setup_entry`): the update listener reloads the entry when
# one of them changes. The gateway host, token and certificate fingerprint only serve the config flow's re-fetch.
HUB_DATA_KEYS: Final = (
    CONF_CDB_PATH,
    CONF_METADATA_DIR,
    CONF_UNICAST,
    CONF_MESH_UUID,
    CONF_SOURCE,
)

# Options (entry.options, set through the options flow; a change reloads the entry through the update listener).
OPTION_CLICK_DELAY: Final = "click_delay"  # hold every `click` back for DOUBLE_CLICK_WINDOW so a double press reports no click
DEFAULT_CLICK_DELAY: Final = False
# let `add_device` provision new devices (review-3 N3, experimental: nothing of it has run on a real device yet)
OPTION_ALLOW_PROVISIONING: Final = "allow_provisioning"
DEFAULT_ALLOW_PROVISIONING: Final = False
# write Home Assistant into the network's file as a provisioner of its own, with its own address ranges, and put
# back the nodes it provisioned (review-3 N1, experimental: no app has imported such a file yet — `identity.py`)
OPTION_PROVISIONER_IDENTITY: Final = "provisioner_identity"
DEFAULT_PROVISIONER_IDENTITY: Final = False
# `delete_unused_scenes` lists what it would delete unless told `dry_run: false` (review-4 W4-3, decision M3): a call
# without fields, as an automation made it before, must not delete the app's scenes from a stale export
DEFAULT_UNUSED_SCENES_DRY_RUN: Final = True
# seconds a freshly provisioned node is given to restart as a mesh node before its configuration starts
NODE_BOOT_DELAY: Final = 5.0
OPTION_HEARTBEATS: Final = "heartbeats"  # ask every mains node for periodic Heartbeats; a silent node's entities go unavailable
DEFAULT_HEARTBEATS: Final = False
# Follow the app (review-4 U4-6, decision M12, `app_follow.py`): the phone heard on the mesh makes an entry set up from
# the gateway fetch its export once the phone went quiet, and an entry set up from a file raise `app_changed` when the
# phone was seen configuring a device; and an entry set up from the gateway checks its export every few hours
OPTION_FOLLOW_APP: Final = "follow_app"
DEFAULT_FOLLOW_APP: Final = True
OPTION_GATEWAY_CHECK: Final = "gateway_check"
DEFAULT_GATEWAY_CHECK: Final = True
# Rooms to areas (`areas.py`, review-4 U4-2): the area each JUNG room's devices start in, chosen in the flow's `areas`
# step — room name -> area id, or None for an area named after the room (created when missing); a room the mapping
# does not know (added later) goes to the area named or aliased like it, else to a new one named after it
CONF_ROOM_AREAS: Final = "room_areas"
# give new devices an area at all; off, they start without one (the `areas` step's switch)
OPTION_ASSIGN_AREAS: Final = "assign_areas"
DEFAULT_ASSIGN_AREAS: Final = True
# after an export adoption or a room action, move the devices whose room changed, unless the user placed them by hand
OPTION_SYNC_AREAS: Final = "sync_areas"
DEFAULT_SYNC_AREAS: Final = False

# Heartbeats (`OPTION_HEARTBEATS`, `docs/hidden-features.md` §4): Config Heartbeat Publication with this PeriodLog
# (2^(n-1) s) to our own address; a node is dead after HEARTBEAT_MISSED_BEATS periods (plus half a period of slack)
# without a beat or any other message; the check runs every HEARTBEAT_CHECK_INTERVAL and the publications are
# (re)configured at most every HEARTBEAT_RECONFIGURE_INTERVAL — they persist in the nodes (CountLog 0xFF).
HEARTBEAT_PERIOD_LOG: Final = 7  # 64 s
HEARTBEAT_MISSED_BEATS: Final = 3
HEARTBEAT_CHECK_INTERVAL: Final = 30.0
HEARTBEAT_RECONFIGURE_INTERVAL: Final = 6 * 3600.0
HEARTBEAT_REPROBE_INTERVAL: Final = 120.0  # a node marked dead is asked again for heartbeats this often: a rebooted node lost its publication

# SIG Device Software Revision (Generic Manufacturer Property 0x001A, ASCII digit pairs): the firmware gates
# (`config_entities.node_version`) read it from the node's primary element in the state cache.
SIG_SOFTWARE_VERSION: Final = 0x001A
SIG_HARDWARE_REVISION: Final = 0x0010  # ASCII, NUL-padded: b"10000000" on air
SIG_MANUFACTURER_NAME: Final = (
    0x0011  # UTF-8, NUL-padded: "Albrecht Jung GmbH & Co.KG" on air
)
# What a node tells about itself and the hub keeps (`coordinator.NODE_VERSIONS`), under the property catalogue's
# names: the SIG identity block (read on each opening of a device page in the app, `RequestManufacturerInfos`), the
# LBC version blocks of its node details (Manufacturer server; the STM32 one on a room thermostat only) and the role
# of its Time Setup Server.
NODE_INFO: Final = {
    SIG_SOFTWARE_VERSION: "software_version",
    SIG_HARDWARE_REVISION: "hardware_revision",
    SIG_MANUFACTURER_NAME: "manufacturer_name",
}
NODE_INFO_VENDOR: Final = {
    0x0003: "secure_element_version",
    0x0004: "bootloader_version",
    0x0005: "stm32_version",
}
NODE_INFO_TIME_ROLE: Final = "time_role"
# A push-button's insert and key layout as the node answered them (`inserts.py`): its InsertId (LBC User 0x0002) and
# ButtonLayout (LBC Admin 0x5001), asked only of a node whose export and advertisement told neither
NODE_INFO_INSERT: Final = {0x0002: "insert_id", 0x5001: "button_layout"}
# Suffix of the item that records a node answering an item of `NODE_INFO` / `NODE_INFO_VENDOR` without a value (it
# does not have it): `"<item>.unsupported"` holds the raw software version it answered under, so the Get is not sent
# again on every link, and a firmware update (another version) asks again.
NODE_INFO_UNSUPPORTED: Final = ".unsupported"

# A service action that goes on air waits this long for the entry's proxy link (a reload reconnects in the
# background, so the call right after another one's reload would otherwise find no link yet).
SERVICE_LINK_WAIT: Final = 30.0

# The "Identify" button: Health Attention Set for this many seconds (the node's LED blinks; the app uses 5 s while
# provisioning). Accepted by every node, `docs/hidden-features.md` §3.
IDENTIFY_SECONDS: Final = 10
# The `locate_node` action (review-4 F4-15): seconds of Node Identity advertising. A node stops by itself after 60 s
# (Mesh Profile §7.2.2.2.3), so longer would not hold; the Set off follows after the time asked for.
LOCATE_SECONDS: Final = 60
LOCATE_MIN_SECONDS: Final = 5

# The JUNG HOME Gateway integration (github.com/ernetas/junghome) whose entities `migration.py` takes over.
GATEWAY_DOMAIN: Final = "junghome"

GATEWAY_DEFAULT_HOST: Final = (
    "junghome.local"  # the gateway's generic mDNS name (and TLS certificate CN)
)
GATEWAY_USER_NAME: Final = (
    "Home Assistant (Bluetooth Mesh)"  # how the access request shows up in the app
)
GATEWAY_REGISTER_TIMEOUT: Final = 190.0  # the gateway holds the request for 180 s while the user approves it in the app
GATEWAY_PROBE_TIMEOUT: Final = (
    10.0  # reachability check before the long registration wait
)
GATEWAY_REQUEST_TIMEOUT: Final = 30.0
GATEWAY_UPLOAD_TIMEOUT: Final = (
    90.0  # the gateway self-configures from the CDB before it answers a project upload
)
# a failed automatic upload is tried again twice, 15 s apart, as the app does (`ProjectFileSyncServiceImpl`: flow
# `retry(2)` with a 15 000 ms delay)
GATEWAY_UPLOAD_RETRIES: Final = 2
GATEWAY_UPLOAD_RETRY_DELAY: Final = 15.0
# the gateway's status (`GET config`) is polled every 30 s while one of its entities is enabled; the app polls every
# 5 s (10 s after an error), but only while it is open — Home Assistant runs all day. Its error log (`GET
# healthstatus`, read by the app only while its log page is open) every 5 minutes.
GATEWAY_STATUS_INTERVAL: Final = 30.0
GATEWAY_HEALTH_INTERVAL: Final = 300.0

STORAGE_DIR: Final = "junghome_ble"  # under the configuration directory: exports fetched from the gateway or uploaded

DEFAULT_UNICAST: Final = "0D00"
PLATFORMS: Final = [
    "binary_sensor",
    "button",
    "cover",
    "climate",
    "event",
    "light",
    "number",
    "scene",
    "select",
    "sensor",
    "switch",
    "update",
]

SIGNAL_UPDATE: Final = f"{DOMAIN}_update_{{}}_{{}}"  # per entry id and element address: unicasts repeat across meshes
SIGNAL_CONNECTION: Final = f"{DOMAIN}_connection_{{}}"  # per entry id
# per entry id: the link's state (`LINK_STATES`) changed; only the link state sensor follows it, not every entity
SIGNAL_LINK_STATE: Final = f"{DOMAIN}_link_state_{{}}"
# per entry id and node unicast: the node's link diagnostics changed (last seen, signal, hops, restart)
SIGNAL_NODE: Final = f"{DOMAIN}_node_{{}}_{{}}"
# per entry id: a node was marked unreachable or dead, or is back (`JungHomeHub._notify_node`); the mesh health sensors
SIGNAL_REACHABILITY: Final = f"{DOMAIN}_reachability_{{}}"
SIGNAL_SCENES: Final = (
    f"{DOMAIN}_scenes_{{}}"  # per entry id: the scene members' actions were (re)read
)
SIGNAL_SCENE_RECALLED: Final = (
    f"{DOMAIN}_scene_recalled_{{}}"  # per entry id: (scene number, source or None)
)
SIGNAL_DETECTOR: Final = f"{DOMAIN}_detector_{{}}_{{}}"  # per entry id and detector sensor element: (event, value) from binary_sensor.py
SIGNAL_BATTERY: Final = f"{DOMAIN}_battery_{{}}_{{}}"  # per entry id and primary element: the decoded Generic Battery Status (sensor.py)
# per entry id: an upload to the gateway went through (`mesh_config.GatewaySync`); the *Last export upload* sensor
SIGNAL_GATEWAY_SYNCED: Final = f"{DOMAIN}_gateway_synced_{{}}"

# Detectors (binary_sensor.py / sensor.py; `docs/gap-analysis/control-and-state.md` §2.8, unverified on hardware).
DETECTOR_PROPERTY_PRESENCE: Final = 0x004D  # SIG Presence Detected: 1 byte, 0 / 1
DETECTOR_PROPERTY_ILLUMINANCE: Final = 0x0055  # SIG Present Illuminance: LE 0.01 lx (firmware > 1.4.0.0), all ones = unknown
DETECTOR_ILLUMINANCE_RAW_LUX_MAX_VERSION: Final = (
    1,
    4,
    0,
    0,
)  # up to this device software version the value is whole lux
DETECTOR_MOTION_HOLD: Final = 120.0  # seconds a motion signalled by an OnOff Set publication is kept on when no Presence Detected status follows and the relay's run-on time (0x1007) is unknown (the app's default run-on time for detector loads)
# The app's walking test (`control-and-state.md` §2.8): it stops the test itself after five minutes and asks for the
# PIR zones (0x6005) every second while it runs.
DETECTOR_WALKING_TEST_DURATION: Final = 300.0
DETECTOR_WALKING_TEST_POLL: Final = 1.0
# seconds between reads of a detector's own brightness (0x6004) while it has delivered no Present Illuminance (the app
# reads it every 5 s while its parameter page is open; nothing reads it otherwise)
DETECTOR_BRIGHTNESS_POLL: Final = 60.0

# Battery products (sensor.py; `control-and-state.md` §2.9). They sleep: a Generic Battery Get is only sent right after
# a key event (the node is awake for a moment), never polled.
BATTERY_READ_INTERVAL: Final = (
    21600.0  # seconds after an answered read before a key event triggers the next one
)
BATTERY_READ_TIMEOUT: Final = 3.0  # seconds to wait for the Battery Status (the app's property-read timeout); one attempt
# While Home Assistant configures a battery node it keeps it awake the app's way (`keep_awake.py`, review-3 W4 / F24):
# an Admin Get of its ButtonLayout once the node was quiet this long (`KeepLowPowerDeviceAwake`, every 6 s) ...
KEEP_AWAKE_INTERVAL: Final = 6.0
KEEP_AWAKE_RETRY: Final = 1.0  # ... asked again this many seconds after one went unanswered, as the app does ...
KEEP_AWAKE_TIMEOUT: Final = 3.0  # ... each waiting this long (the app's property-read timeout; the notes give the keep-alive none of its own)

MIN_KELVIN: Final = 2000
MAX_KELVIN: Final = 6000
# The colour temperatures the app lets a tunable-white light's range ("White area") span, and clamps both ends of the
# range it writes to (`p097i9/c.java`: TunableWhiteValueCapable, 2000..10000 K).
WHITE_RANGE_MIN_KELVIN: Final = 2000
WHITE_RANGE_MAX_KELVIN: Final = 10000
# Room thermostat set-point range and step, as the app's slider (docs/gap-analysis/control-and-state.md §2.7).
CLIMATE_MIN_TEMP: Final = 5.0
CLIMATE_MAX_TEMP: Final = 30.0
CLIMATE_TEMP_STEP: Final = 0.5
# Boost heats at full power for five minutes and the thermostat ends it itself, telling no one (control-and-state.md
# §2.7: the app polls 0x120D while its page is open); the climate entity reads it back this long after it started.
RTR_BOOST_DURATION: Final = 300.0
RTR_BOOST_READBACK_MARGIN: Final = 10.0
# A boost started on the thermostat itself is seen only if it publishes the Status (unverified): the climate entity
# also asks for 0x120D this often while the link is up — the app every 5 s, but only while its page is open
# (`RoomTemperatureViewModel.requestBoostFunction`); a minute shows such a boost within a fifth of its run.
RTR_BOOST_POLL_INTERVAL: Final = 60.0
DOUBLE_CLICK_WINDOW: Final = 0.5  # seconds between two clicks to report a double click
BUTTON_REPEAT_WINDOW: Final = 3.0  # a vendor button event with a counter seen this recently is the firmware's second copy
# (the sniffer measured the copy spacing of status publications at 0.9-2.3 s, docs/sniffer.md; the counter is per press,
# so a real second press is never mistaken for a copy whatever the window)
# Hold-to-dim (review-3 F12). What a rocker wired straight to a dimmer sends while it is held is not captured on air;
# the SIG ways are a Generic Move Set (a delta to start, 0 to stop) or a Generic Delta Set transaction (one TID,
# growing deltas while held, `TID_REPEAT_WINDOW`). A Delta transaction has no stop message: its hold is taken to end
# this many seconds after its last Set (the key's cadence is a guess; the firmware's second copies are dropped first).
DIM_HOLD_QUIET: Final = 1.5
# Every hold — a Move or Delta one above, or a gateway-mode key's vendor `hold_start` — ends at the latest this many
# seconds after it started (review-4 R4-7): a lost stop (a Move 0, a release) used to leave it open, and a
# dim-while-held automation dimming for ever. Fading through the whole range takes a few seconds; nobody holds a key
# this long on purpose. A hold ended without its stop carries `reason` (HOLD_END_REASONS) in its `hold_end`.
DIM_HOLD_MAX: Final = 30.0
HOLD_END_TIMEOUT: Final = "timeout"  # DIM_HOLD_MAX passed
HOLD_END_LINK_LOST: Final = (
    "link_lost"  # the link ended: the stop could not be heard (decision M11)
)
HOLD_END_STOPPED: Final = (
    "stopped"  # the entry stopped (unload, reload, Home Assistant shutting down)
)
HOLD_END_REASONS: Final = (HOLD_END_TIMEOUT, HOLD_END_LINK_LOST, HOLD_END_STOPPED)
# `junghome_ble.start_dim`: Generic Move Set with a transition time of one 100 ms step (Mesh Model §3.1.3: 0b00
# resolution, 1 step) and a delta per step from the speed, in % of the full range per second. Unverified on air.
DIM_MOVE_TRANSITION: Final = 0x01
DIM_STEPS_PER_SECOND: Final = 10
DIM_DEFAULT_SPEED: Final = 20  # % of the range per second: from off to full in 5 s
# KEY_EVT (vendor property 0x5012 `[counter][code]`) codes, per the gateway firmware's decoder
# (`docs/cross-repo-analysis.md` §1.2). A rocker in key mode *Gateway* is one element with two halves and reports
# which half with codes 0-3; a single key reports 4-6 without a side. Code -> (event type, side): the side becomes
# the `side` attribute of the event (`"down"` / `"up"`); a release (4) takes the side of the hold it ends.
KEY_EVENT_SIDE_DOWN: Final = "down"
KEY_EVENT_SIDE_UP: Final = "up"
KEY_EVENT_RELEASE: Final = 0x04
KEY_EVENTS: Final[dict[int, tuple[str, str | None]]] = {
    0x00: ("click", KEY_EVENT_SIDE_DOWN),  # pushed_down: lower half of a rocker
    0x01: ("click", KEY_EVENT_SIDE_UP),  # pushed_up: upper half
    0x02: ("hold_start", KEY_EVENT_SIDE_DOWN),  # held_down
    0x03: ("hold_start", KEY_EVENT_SIDE_UP),  # held_up
    KEY_EVENT_RELEASE: ("hold_end", None),  # released: ends a hold of either kind
    0x05: ("click", None),  # pushed: a single key
    0x06: ("hold_start", None),  # held: a single key
}
TID_REPEAT_WINDOW: Final = 6.0  # same for SIG client messages: one transaction (TID) lives 6 s (Mesh Model §3.3.1.2)
# A Scene Status a node publishes this soon after a recall of the same scene that fired EVENT_SCENE_RECALLED (a
# key's, the app's, ours) is that recall's echo; later, it is a recall Home Assistant did not hear (captured on air:
# the members' Scene Status follow the app's Scene Recall within 50 ms in the app settings capture).
SCENE_RECALL_WINDOW: Final = 5.0
REFRESH_CHUNK: Final = (
    5  # state Gets in flight at once before a short pause (same as the app)
)
# The app's budget for every request it waits a status for (`MeshMessengerImpl.processRequest`: 3 attempts x 3000 ms,
# matched on source element + status opcode): a state Get, a property Get, and a load's acknowledged Set
REQUEST_ATTEMPTS: Final = 3
REQUEST_TIMEOUT: Final = 3.0  # what `ProxyClient.request` waits per attempt by default (the hub relies on it)
REFRESH_RETRIES: Final = (
    REQUEST_ATTEMPTS  # attempts per state Get before the element counts as unanswered
)
# The Configuration Server audit (`junghome_ble.audit_network`): per Get, the wait for its status and the attempts
# before it counts as unanswered — the budget of a configuration change's messages (`mesh_config.CONFIG_TIMEOUT`)
AUDIT_TIMEOUT: Final = 3.0
AUDIT_RETRIES: Final = 2
ENERGY_POLL_INTERVAL: Final = 300.0  # seconds between reads of the metered loads' counters, which nothing publishes
# seconds between two imports of the metered loads' energy charts into the statistics (`energy_history.py`): one per
# link, and a link that came back sooner than this after the last import missed no whole hour
ENERGY_HISTORY_INTERVAL: Final = 3600.0
# The proxy answers the filter request every link starts with by a Filter Status within a few hundred ms. A link
# whose beacon authenticated but whose Filter Status never came within this many seconds is a proxy that discards our
# PDUs (stale sequence number, address collision): it keeps its default empty whitelist and forwards nothing, so
# the refresh-based drop detection (which needs other traffic) would never fire. Raises `pdus_dropped`.
FILTER_STATUS_TIMEOUT: Final = 10.0
# a JUNG proxy sends its Secure Network Beacon right after the subscription: the first filter request waits that long
# for it, so it goes out under the network's current IV index instead of a stale stored one the proxy would drop
CONNECT_BEACON_WAIT: Final = 1.0
# Link watchdog. A mesh without a gateway can be silent for a long time (nothing publishes at night; the energy poll
# is the only traffic), so silence alone proves nothing: after LINK_IDLE_TIMEOUT without a PDU, beacon or Filter
# Status from the proxy, a keep-alive Get is sent and the link is dropped only when that goes unanswered too. The
# timeout leaves room for two energy polls (anchored on the connection) plus a margin, so a healthy quiet mesh never
# reaches the keep-alive.
LINK_IDLE_TIMEOUT: Final = 660.0  # seconds >= 2 * ENERGY_POLL_INTERVAL + margin
# Link-loss UX (review-3): a link that drops is usually back within seconds (the next proxy node takes over), so the
# entities stay available for LINK_LOSS_GRACE and a command in that time waits for the new link instead of failing.
# A group command (unacknowledged: a room, "all lights", a scene) or a movement (a blind's or hold-to-dim's Move /
# Delta Set) the mesh does not answer within COMMAND_ECHO_TIMEOUT (JUNG loads publish their new state at once) makes
# the link watchdog probe the proxy at once instead of after LINK_IDLE_TIMEOUT of silence; a load's own Set waits for
# its status (REQUEST_ATTEMPTS).
LINK_LOSS_GRACE: Final = 20.0
COMMAND_ECHO_TIMEOUT: Final = 5.0
# `async_stop` gives the link this long to close (a transport whose disconnect never returns must not hold up an
# unload or Home Assistant's shutdown)
STOP_TIMEOUT: Final = 10.0
KEEP_ALIVE_TIMEOUT: Final = 3.0  # seconds to wait for the status answering one keep-alive Get (the app's read timeout)
KEEP_ALIVE_ATTEMPTS: Final = 3  # distinct elements asked before the proxy counts as silent (one may be unplugged)
# Per-node reachability, the app's rule (`MeshMessengerImpl$handleError$1`, `docs/gap-analysis/control-and-state.md`
# §1.2): a node is unreachable as soon as a request it was asked with the full budget (REQUEST_ATTEMPTS x
# REQUEST_TIMEOUT) goes unanswered — a state Get or a command — unless it was heard from meanwhile, and reachable
# again with any message from it. A shorter probe it missed (the link watchdog's one-attempt keep-alive) is no verdict:
# the element is asked again with a full-budget Get after UNREACHABLE_RECHECK seconds.
UNREACHABLE_RECHECK: Final = 60.0
# ... and an unreachable node is asked again this often while the link lasts: a breaker that was off for a few minutes
# must not leave its entities unavailable until the next link (review-3 C3)
UNREACHABLE_REPROBE: Final = 300.0
# A node the export does not know (`unknown_nodes`) makes the hub ask the gateway for its export; the app uploads
# its project there after a change, but not always before the new node first advertises. Unanswered, the question is
# asked again after each of these delays (the last one repeating) and on every new link, until the export has them.
EXPORT_REFRESH_BACKOFF: Final = (60.0, 300.0, 900.0, 3600.0)
# Following the app (`app_follow.py`): the gateway's export is fetched APP_QUIET_AFTER seconds after the phone was last
# heard (the app uploads its project after each change; a burst of edits is one fetch), at most once per
# APP_SYNC_MIN_INTERVAL for the phone's activity, and every GATEWAY_SYNC_PERIOD seconds whatever was heard
APP_QUIET_AFTER: Final = 180.0
APP_SYNC_MIN_INTERVAL: Final = 900.0
GATEWAY_SYNC_PERIOD: Final = 6 * 3600.0
TIME_SET_INTERVAL: Final = 86400.0  # seconds between Time Set broadcasts; the first one follows every connection
# a daylight-saving change sends Time Set again this many seconds after it (review-3 F17); the change is looked for up to
# a year ahead
OFFSET_CHANGE_DELAY: Final = 5.0
OFFSET_SEARCH_DAYS: Final = 400
# The nodes' clocks (`node_clocks.py`, review-4 F4-8): a node that may run schedules and whose clock is more than
# CLOCK_OFFSET_MAX seconds off Home Assistant's (or has no time, or another zone offset than the one Time Set carries)
# raises `node_clock_wrong`. A clock read is a Get per item: CLOCK_READ_PAUSE seconds between two chunks of
# REFRESH_CHUNK nodes. A stored location within LOCATION_TOLERANCE degrees of the home's counts as the home's
# (0.01 degree is about a kilometre: sunrise and sunset move by seconds).
CLOCK_OFFSET_MAX: Final = 60.0
CLOCK_READ_PAUSE: Final = 0.5
LOCATION_TOLERANCE: Final = 0.01
# the JH Scheduler model (`schedules.py`) and the entry's schedulers, here so the clocks can see what they cached
SCHEDULER_MODEL: Final = "05271016"
SCHEDULERS: HassKey[dict[str, Scheduler]] = HassKey(f"{DOMAIN}_schedulers")
# The connection loop's pause after a failed connection doubles from CONNECT_BACKOFF_MIN up to CONNECT_BACKOFF_MAX; a
# link that lasted SHORT_LINK counts as a working one and starts it over.
CONNECT_BACKOFF_MIN: Final = 2.0
CONNECT_BACKOFF_MAX: Final = 60.0
FAILED_PROXY_COOLDOWN: Final = 120.0
# Short-link penalty (review-4 R4-1). A proxy whose link comes up and is lost again within SHORT_LINK seconds failed
# the connection just as much as one that never connected, only later: the pause before the next attempt doubles
# (CONNECT_BACKOFF_MIN ..), and after SHORT_LINK_STREAK such links in a row the node is set aside for
# FAILED_PROXY_COOLDOWN like a node that cannot be connected to — the strongest node is otherwise picked again and
# again, each link restarting the connect-time refresh. A node that is the only one in range is still used, and one
# short link alone sets no node aside (a node restarting right after we connected is no flapping proxy).
SHORT_LINK: Final = 60.0
SHORT_LINK_STREAK: Final = 3
# Connect-time steps that read what changes rarely or is published anyway (the scene actions, the fault registers,
# the current scenes; review-4 R I-5) are not repeated by a link that comes within CONNECT_STEP_FRESH seconds of
# their last complete round when the link before it held for SHORT_LINK: the hub heard the nodes' publications
# meanwhile. The state refresh, the energy poll, Time Set and the location go out on every link. Unverified on air.
CONNECT_STEP_FRESH: Final = 900.0
# a proxy advertisement older than this ranks behind every fresher one: its node may be off (JUNG nodes advertise
# several times a second)
PROXY_ADVERT_MAX_AGE: Final = 60.0

# Config entities (device parameters read/written as JUNG vendor properties, see config_entities.py).
PROPERTY_READ_DELAY: Final = 3.0  # seconds after the link is up (or an entity was enabled) before the initial property reads start: the state refresh goes first
PROPERTY_READ_CHUNK: Final = (
    5  # initial property reads in flight at once before a pause (like REFRESH_CHUNK)
)
PROPERTY_READ_PAUSE: Final = 0.5  # seconds between two chunks of initial reads
# A background sender with no link (the property reads, a battery node's keep-alive) waits for one instead of sending
# into "not connected"; this long per wait (`JungHomeHub.async_wait_connected`) before it looks again.
LINK_WAIT_STEP: Final = 60.0
# The two property timeouts are read as `const.PROPERTY_READ_TIMEOUT` / `const.PROPERTY_WRITE_TIMEOUT` when a Get or
# Set is sent (not imported by name), so one patch here reaches every module that sends one (`tests/property_helpers`).
PROPERTY_READ_TIMEOUT: Final = (
    3.0  # seconds to wait for the Status answering a property Get (the app waits 3 s)
)
PROPERTY_READ_RETRIES: Final = REQUEST_ATTEMPTS  # Get attempts before a property counts as unanswered (stays unknown)
PROPERTY_READ_FRESH: Final = 10.0  # seconds within which a property answered once is not read again by another entity
# A config entity whose values were read is read again on a later link once this many seconds have passed since
# (`ConfigEntity._maybe_read`): a value changed in the app is answered to the app's address, so nothing else tells
# Home Assistant (review-4 H4-10). Once per link at most, through the reader's queue.
CONFIG_REREAD_INTERVAL: Final = 3 * 3600.0
# `homeassistant.update_entity` asks an element for the same thing at most once per this many seconds
# (`JungHomeEntity.async_update`); a call within it keeps the cached state.
UPDATE_READ_INTERVAL: Final = 2.0
PROPERTY_WRITE_TIMEOUT: Final = (
    3.0  # seconds to wait for the Status answering an acknowledged property Set
)
PROPERTY_REREAD_DELAY: Final = (
    0.5  # seconds before re-reading a property whose Set was not answered
)
LOCK_EXPIRY_MARGIN: Final = (
    5.0  # seconds after a timed lock should have ended before its switch reads it back
)
LOCK_TIME_LIMIT_MAX: Final = (
    4 * 3600
    + 59 * 60
    + 59  # seconds: the app's H:MM:SS time-limit picker ends at 4:59:59
)

# Covers (blinds / shutters / awnings on Generic Level elements, see cover.py). UNVERIFIED ON HARDWARE: derived from
# the gateway firmware and the app, the maintainer owns no blinds.
# Generic Level ends: JUNG closedness 0 % (open, slats open) and 100 % (closed).
COVER_LEVEL_OPEN: Final = -32768
COVER_LEVEL_CLOSED: Final = 32767
# Generic Move Set deltas the gateway sends: 0x8000 up (open), 0x7FFF down (close), 0 stop.
COVER_MOVE_UP: Final = -32768
COVER_MOVE_DOWN: Final = 32767
COVER_MOVE_STOP: Final = 0
# Transition-time byte of those Move Sets. The gateway asks its Silicon Labs NCP for 0xFFFE ms ("max duration for
# movement", `PositionState.js`); mesh stacks encode 65.5 s in 10-second steps rounded up: 0b10 << 6 | 7 = 70 s.
# The byte the NCP really puts on air is not captured yet.
COVER_MOVE_TRANSITION: Final = 0x87
# MOVE_OPERATION_MODE: 0 blinds (with slats), 1 shutter, 3 awning; decides the cover's device class.
COVER_MODE_PROPERTY: Final = 0x1104
# A reference run is read back after the blind's running time plus this, as the app waits for it
# (`ReferenceRunLoadingViewModel`); while the running time (0x1102) is not known, the longest the app accepts.
REFERENCE_RUN_MARGIN: Final = 10.0
REFERENCE_RUN_LONGEST: Final = 600.0

ISSUE_KEY_REFRESH: Final = "key_refresh"
# no connectable Bluetooth adapter or proxy is left (the app's "Bluetooth is off" screen, `ObserveBluetoothState`)
ISSUE_BLUETOOTH_UNAVAILABLE: Final = "bluetooth_unavailable"
# The link's state as the link state sensor shows it, the app's `ObserveDeviceConnectionState` (X7.a) plus the two
# screens before it: no Bluetooth at all, no proxy node in range; `updating` is the app's "the status of your devices
# is being updated" (proxy_loading_new_connection_description): connected, the connect-time state refresh running.
LINK_BLUETOOTH_OFF: Final = "bluetooth_off"
LINK_SEARCHING: Final = "searching"
LINK_CONNECTING: Final = "connecting"
LINK_UPDATING: Final = "updating"
LINK_CONNECTED: Final = "connected"
LINK_FAILED: Final = "failed"
LINK_DISCONNECTED: Final = "disconnected"
LINK_STATES: Final = (
    LINK_BLUETOOTH_OFF,
    LINK_SEARCHING,
    LINK_CONNECTING,
    LINK_UPDATING,
    LINK_CONNECTED,
    LINK_FAILED,
    LINK_DISCONNECTED,
)
ISSUE_PDUS_DROPPED: Final = "pdus_dropped"
ISSUE_ADDRESS_IN_USE: Final = (
    "address_in_use"  # a node of the export has Home Assistant's address
)
# HA's address lies in a provisioner's range, or is excluded
ISSUE_ADDRESS_RESERVED: Final = "address_reserved"
# an authenticated beacon states an IV index Home Assistant cannot follow (review-3 T5)
ISSUE_IV_INDEX_MISMATCH: Final = "iv_index_mismatch"
# ... the same issue (its id stays `iv_index_mismatch_<entry id>`) when Home Assistant is ahead of the mesh and can go
# back to its index: the fixable text (a translation key cannot hold both a description and a fix flow)
ISSUE_IV_INDEX_AHEAD: Final = "iv_index_ahead"
# a source of the mesh (a node, the app, Home Assistant) used most of the sequence space of the current IV index
ISSUE_SEQUENCE_SPACE_LOW: Final = "sequence_space_low"
ISSUE_SEQ_STORE_LOST: Final = "seq_store_lost"  # our address has history, but neither copy of its sequence-number record is usable
# the sequence-number store has refused every write for a while: sends are held back until one lands
ISSUE_SEQ_STORE_UNWRITABLE: Final = "seq_store_unwritable"
# a PDU from Home Assistant's own address with a number it never sent: another client uses the address (review-4
# S I2); sends are refused until the repair skips past it
ISSUE_ADDRESS_SHARED: Final = "address_shared"
# ... the same issue (its id stays `address_shared_<entry id>`) seen again after that repair skipped past it once:
# the text that asks for another address (a translation key cannot hold two descriptions)
ISSUE_ADDRESS_SHARED_AGAIN: Final = "address_shared_again"
# How far the counter jumps when the numbers already sent are not known for sure (`seq_store_lost`, `pdus_dropped`):
# past what the nodes may remember from the best record left, or — nothing left at all — this far from 0. A year of
# a busy link (polls, refreshes, keep-alives) is well under the first; the 24-bit space holds 16 of them.
SEQ_SKIP_AHEAD: Final = 1 << 20
# Diagnostics (review-3 F8, F9, N2). A node's link diagnostics are signalled at most once per this many seconds
# (last seen and signal strength change with every message and advertisement); the sequence space of every source
# is checked this often, and a source past SEQUENCE_SPACE_WARN (3/4 of the 24-bit space) raises
# `sequence_space_low`: the mesh needs an IV Update before it runs out, and only the gateway starts one.
NODE_DIAGNOSTICS_INTERVAL: Final = 60.0
SEQUENCE_CHECK_INTERVAL: Final = 600.0
SEQUENCE_SPACE_WARN: Final = 0xC00000
# JUNG firmware stores its sequence number in blocks of 0x10000 and continues from the next block after a restart
# (seen on two nodes): a jump into a new block that lands near its start, from a number not near the end of the
# previous one, is a restart (a mains blip, a breaker), not the counter running on.
RESTART_BLOCK: Final = 0x10000
RESTART_SLACK: Final = 0x0400
SEQ_SKIP_UNKNOWN: Final = 1 << 22
ISSUE_GATEWAY_IMPORT: Final = "gateway_import"  # a gateway integration entry is active next to ours: offer the import
ISSUE_EXPORT_STALE: Final = "export_stale"
ISSUE_GATEWAY_SYNC: Final = (
    "gateway_sync_failed"  # a changed export could not be handed to the gateway
)
ISSUE_PLAN_INTERRUPTED: Final = (
    "plan_interrupted"  # a configuration plan was cut off by a stop or crash (D12)
)
ISSUE_DEVICE_NAME: Final = "device_name_rejected"  # a device renamed in HA to a name the app refuses (device_names.py)
ISSUE_PENDING_DEVICE: Final = "pending_device"  # a device Home Assistant provisioned was never recorded (onboard.py)
# the vault could not be written while a device was added: provisioning stopped before the device got anything
ISSUE_VAULT_UNWRITABLE: Final = "vault_unwritable"
# devices Home Assistant added that did not confirm the end of the app's key refresh (vault_refresh.py)
ISSUE_VAULT_KEY_REFRESH: Final = "vault_key_refresh_lagging"
ISSUE_CARRY_OVER_CONFLICT: Final = "carry_over_conflict"  # an adopted app export overrode what HA had changed (mesh_config.py)
ISSUE_SCENE_HELD: Final = "scene_held"  # a forced delete_scene skipped devices that still hold the scene (mesh_config.py)
ISSUE_GATEWAY_TOKEN: Final = "gateway_token_rejected"  # noqa: S105 - an issue id: the gateway no longer accepts the entry's token
ISSUE_GATEWAY_CERTIFICATE: Final = "gateway_certificate_changed"  # the gateway, or its node over the mesh, contradicts the pin
ISSUE_UNKNOWN_NODES: Final = (
    "unknown_nodes"  # nodes of our network advertise from MACs the export does not know
)
# ... the same issue's wording for an entry set up from the gateway, whose export Home Assistant fetches by itself
ISSUE_UNKNOWN_NODES_GATEWAY: Final = "unknown_nodes_gateway"
# the phone was seen configuring a device of an entry set up from a file: the export may be behind (`app_follow.py`)
ISSUE_APP_CHANGED: Final = "app_changed"
# push-buttons advertising another insert than the one the export cached for them: an insert was swapped (`inserts.py`)
ISSUE_INSERT_MISMATCH: Final = "insert_mismatch"
# a node that may run schedules has a wrong clock or zone offset (`node_clocks.py`): fixed by sending Time Set now
ISSUE_NODE_CLOCK_WRONG: Final = "node_clock_wrong"
# the project has PP2 pucks and no node keeps their time (`JungHomeHub._report_time_keeper`, `switch.py`)
ISSUE_TIME_KEEPER_MISSING: Final = "time_keeper_missing"
ISSUE_DUPLICATE_MESH: Final = (
    "duplicate_mesh"  # another entry already covers this mesh UUID: their sequence-number
    # records can roll each other back (the config flow refuses this for anything set up
    # after that check was added; an older or hand-edited installation can still have it)
)
# Every repair issue's *Learn more* link (review-4 U4-5): its entry on the user guide's maintenance page as GitHub
# renders it, by translation key (an issue with two wordings has both). `tests/test_repairs.py` checks that every
# translation key has one, that the page is the published one next to the manifest's `documentation`, and that each
# anchor is a heading of it.
LEARN_MORE_PAGE: Final = (
    "https://github.com/ernetas/junghome-bt-mesh/blob/main/docs/user/maintenance.md"
)
ISSUE_LEARN_MORE: Final[Mapping[str, str]] = MappingProxyType(
    {
        ISSUE_UNKNOWN_NODES: "jung-home-devices-missing-from-the-export",
        ISSUE_UNKNOWN_NODES_GATEWAY: "jung-home-devices-missing-from-the-export",
        ISSUE_APP_CHANGED: "the-jung-home-app-changed-the-installation",
        ISSUE_INSERT_MISMATCH: "jung-home-push-buttons-with-another-insert-than-in-the-export",
        ISSUE_DEVICE_NAME: "device-name-not-passed-on-to-the-jung-home-app",
        ISSUE_GATEWAY_IMPORT: "take-over-the-jung-home-gateway-integrations-entities",
        ISSUE_DUPLICATE_MESH: "two-entries-cover-the-same-jung-home-mesh",
        ISSUE_PLAN_INTERRUPTED: "a-jung-home-change-on--was-interrupted",
        ISSUE_CARRY_OVER_CONFLICT: "the-jung-home-app-overrode-a-change-home-assistant-made-on-",
        ISSUE_SCENE_HELD: "devices-still-hold-a-deleted-jung-home-scene-on-",
        ISSUE_GATEWAY_SYNC: "jung-home-export-not-handed-to-the-gateway",
        ISSUE_GATEWAY_CERTIFICATE: "jung-home-gateway-certificate-changed",
        ISSUE_GATEWAY_TOKEN: "jung-home-gateway-no-longer-accepts-home-assistant",
        ISSUE_BLUETOOTH_UNAVAILABLE: "no-bluetooth-for-the-jung-home-mesh",
        ISSUE_PDUS_DROPPED: "jung-home-devices-ignore-home-assistant",
        ISSUE_ADDRESS_SHARED: "another-client-uses-home-assistants-jung-home-address",
        ISSUE_ADDRESS_SHARED_AGAIN: "another-client-uses-home-assistants-jung-home-address",
        ISSUE_ADDRESS_IN_USE: "home-assistants-jung-home-address-is-taken",
        ISSUE_ADDRESS_RESERVED: "home-assistants-jung-home-address-may-be-handed-out",
        ISSUE_KEY_REFRESH: "jung-home-mesh-keys-are-changing",
        ISSUE_EXPORT_STALE: "jung-home-mesh-keys-have-changed",
        ISSUE_SEQ_STORE_LOST: "sequence-numbers-of-the-jung-home-mesh--lost",
        ISSUE_SEQ_STORE_UNWRITABLE: "jung-home-sequence-numbers-cannot-be-saved",
        ISSUE_SEQUENCE_SPACE_LOW: "jung-home-mesh-sequence-numbers-running-low",
        ISSUE_IV_INDEX_MISMATCH: "jung-home-mesh-is-at-another-iv-index",
        ISSUE_IV_INDEX_AHEAD: "jung-home-mesh-is-at-another-iv-index",
        ISSUE_NODE_CLOCK_WRONG: "jung-home-devices-with-a-wrong-clock",
        ISSUE_TIME_KEEPER_MISSING: "jung-home-pucks-have-no-time-keeper",
        ISSUE_PENDING_DEVICE: "a-device-home-assistant-added-is-not-recorded",
        ISSUE_VAULT_UNWRITABLE: "jung-home-device-keys-cannot-be-saved",
        ISSUE_VAULT_KEY_REFRESH: "devices-home-assistant-added-missed-the-new-network-key",
    }
)


def learn_more_url(translation_key: str) -> str:
    """Return the *Learn more* link of the repair issue raised with `translation_key` (`ISSUE_LEARN_MORE`)."""
    return f"{LEARN_MORE_PAGE}#{ISSUE_LEARN_MORE[translation_key]}"


def issue_id(entry: ConfigEntry, key: str) -> str:
    """Return the repair-issue id of `key` (an `ISSUE_*` translation key) for `entry`: one issue per mesh, not per domain."""
    return f"{key}_{entry.entry_id}"


EXPORT_STALE_THRESHOLD: Final = 20  # undecryptable PDUs / unauthenticated beacons on one link, with nothing decodable, before the export counts as stale

# Bus events. Device triggers can only attach to something on the Home Assistant bus, so the hub publishes every
# event of a key as EVENT_BUTTON_ACTION (`device_trigger.py` matches on it) — whether or not the key's event entity
# is enabled — and a Scene Recall heard on the mesh as EVENT_SCENE_RECALLED. `logbook.py` describes both.
EVENT_BUTTON_ACTION: Final = f"{DOMAIN}_button_action"
EVENT_SCENE_RECALLED: Final = f"{DOMAIN}_scene_recalled"
# an action's plan finished, stopped or was cancelled (review-4 W I7): `entry_id`, `name` (the entry's title),
# `action`, `outcome`, and the logbook line as a translation key of the `exceptions` section with its placeholders
EVENT_PLAN: Final = f"{DOMAIN}_plan"
# Keys of the bus events' data, next to HA's own ATTR_DEVICE_ID / ATTR_ENTITY_ID / ATTR_NAME and CONF_TYPE:
# the key letter A-D of the buttons device (EVENT_BUTTON_ACTION), the scene number (both events), the element
# address of the node that recalled the scene as 4 hex digits (absent when only a member's Scene Status told of the
# recall: `reported_by` is that member), and the config entry (mesh network) of the event.
ATTR_KEY: Final = "key"
ATTR_SCENE: Final = "scene"
ATTR_SOURCE: Final = "source"
ATTR_REPORTED_BY: Final = "reported_by"
ATTR_ENTRY_ID: Final = "entry_id"
ATTR_REASON: Final = (
    "reason"  # why a `hold_end` came without its stop (HOLD_END_REASONS)
)
