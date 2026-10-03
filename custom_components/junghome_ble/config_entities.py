"""Config entities: the device parameters of the JUNG HOME app's *Parameters* tab as HA entities.

Every parameter is a JUNG vendor property (`jhmesh/properties.py`, catalogue of `docs/android/properties.md`).
This module maps each writable `PropertySpec` to an HA platform by its codec (`describe`), resolves the mesh
element it is addressed to and the HA device it belongs to (`config_targets`), reads and writes the values
(`PropertyReader`) and caches them in `ElementState.properties` through one status handler for the three
vendor Status opcodes. `number.py`, `select.py`, `switch.py` and `button.py` only wrap the targets in the
platform's entity class.

Read / write flow, the app's (`docs/gap-analysis/device-settings.md` §1.2): one acknowledged Get when the entity
is added or enabled and the link is up (never polled), an acknowledged Set on change followed by the Status
reply, or a re-read when nothing answered (a change neither answers nor shows is an error, not a success);
unsolicited Status publications (`C5 / CB / D1 27 05`) are applied as they arrive. Initial reads go through a
platform-level scheduler, `PROPERTY_READ_CHUNK` at a time, so a large installation does not flood the mesh when
the link comes up. A battery node sleeps then: its entities are read right after one of its keys reported. A change
to one keeps it awake the app's way while it runs (`keep_awake.py`), and one it does not answer fails as *asleep*,
asking for a key press first (review-3 W4 / F24).

Enabled by default are only the parameters the app shows on the *first* Parameters page of the device type
(`FIRST_PAGE`, `device-settings.md` §1.1); the expert-mode ones exist but are disabled in the entity registry.

A few app parameters are SIG setup-server states instead (`SETUP_STATES`: behaviour after mains return, the
brightness range, the switch-on brightness and colour temperature; `CTL_TEMPERATURE_RANGE`, the white area): same
flow, their own Get / Set / Status (`SetupTarget`, `SetupStateEntity`), cached in `ElementState.setup`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from . import const
from .const import (
    DOMAIN,
    LINK_WAIT_STEP,
    LOCK_EXPIRY_MARGIN,
    NODE_INFO,
    NODE_INFO_TIME_ROLE,
    NODE_INFO_UNSUPPORTED,
    NODE_INFO_VENDOR,
    PROPERTY_READ_CHUNK,
    PROPERTY_READ_DELAY,
    PROPERTY_READ_FRESH,
    PROPERTY_READ_PAUSE,
    PROPERTY_READ_RETRIES,
    PROPERTY_REREAD_DELAY,
    SIG_HARDWARE_REVISION,
    SIG_MANUFACTURER_NAME,
    SIG_SOFTWARE_VERSION,
)
from .coordinator import JungHomeHub, register_status_handler
from .entity import (
    JungHomeEntity,
    blind_device_info,
    button_gang,
    buttons_device_info,
    light_device_info,
    node_device_info,
    socket_device_info,
    software_version,
)
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh.devices import (
    BATTERY_PIDS,
    LOAD_LOCATIONS,
    Blind,
    Button,
    Light,
    Socket,
)
from .jhmesh.pdu import decode_opcode

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.helpers.device_registry import DeviceInfo

    from .jhmesh.cdb import Element, Node
    from .jhmesh.client import AccessMessage
    from .jhmesh.properties import PropertySpec

_LOGGER = logging.getLogger(__name__)

Platform = Literal["number", "select", "switch", "button"]
WriteMethod = Literal["set", "status"]
# how a write ended: confirmed (the Status answering the Set, or the read-back), the read-back reporting another
# value, neither the Set nor the read-back answered, or the Set answered by a Status without a value (the element
# does not have the property)
WriteOutcome = Literal["applied", "not_applied", "no_answer", "not_supported"]
VendorServer = Literal["admin", "manufacturer", "user"]
Page = Literal[
    "lamp", "socket", "control_switch", "mini", "rtr", "detector", "blind", "gateway"
]

PROPERTY_STATUS_LED = 0x5013
PROPERTY_REFERENCE_RUN = 0x110D
PROPERTY_LOCK = 0x0009  # EnforceOutput, the app's lock (`JungHomeLockSwitch`)
PROPERTY_DEVICE_LOCK = 0x0001  # the node's lock flags (`device_lock_targets`)
GATEWAY_API_STATUS, GATEWAY_IP = (
    0xC000,
    0xC002,
)  # the gateway's status its entities show
PROPERTY_EDGE_DETECTION = 0x5009  # a mini-actuator input's edge evaluation
PROPERTY_KEY_MODE = (
    0x5003  # a key's mode: read-only here (`key_mode_targets`), written by `assign_key`
)
PROPERTY_DIM_MODE, PROPERTY_DIM_TO_WARM = 0x0013, 0x100E
PROPERTY_AUTOMATIC_DST = 0x000F
AUX_LOCATION = 0x0044
TIME_SETUP_SERVER = (
    "1201"  # the model a Time Role Get goes to (`PropertyReader._ask_time_role`)
)

# Writable properties the app has, but which must not be exposed as plain entities.
UNSAFE_PROPERTIES: frozenset[int] = frozenset(
    {
        0x5001,  # button layout: the app resets and re-provisions the node after writing it
        0x5003,  # key mode: only meaningful together with the publication/subscription config of a connection
        0x1014,  # rtr operation mode: written by the room-thermostat connection flow, not a user setting
        0x1208,  # heating optimisation: declared by the app, no UI (device-settings.md §13.10)
        0x6001,  # walking test: paired with 0x6003, a 1 s poll of 0x6005 and an auto-off (`switch.JungHomeWalkingTest`)
        0x6003,
        0x6016,  # continuous on / off: set on the detector itself, the app only shows it (`sensor.JungHomeForcedOff`)
    }
)
PROPERTY_WALKING_TEST, PROPERTY_PRESENCE_CONTROL = 0x6001, 0x6003
PROPERTY_FORCED_OFF = 0x6016
# Load-element properties that only apply to some load kinds (`Light.kind`); the rest apply to every load.
LAMP_KINDS = frozenset({"switch", "dimmer", "ctl"})
# The dim mode only on a dimmer: the app's `DimLampDevice` is `DimModeCompatible`, its `TunableWhiteLampDevice`
# (a DALI tunable-white load, kind "ctl") is not (`Y7/f0.java`, `Y7/t0.java`).
LOAD_KINDS: dict[int, frozenset[str]] = {
    PROPERTY_DIM_MODE: frozenset({"dimmer"}),
    PROPERTY_DIM_TO_WARM: frozenset({"ctl"}),
    PROPERTY_LOCK: LAMP_KINDS | {"socket", "blind"},
}
BLIND_PROPERTIES = range(0x1100, 0x1200)  # a blind load: no such device is derived yet
# Keys with an LED: mini-actuator inputs publish key events too, but have nothing to light. Mains push-buttons only:
# a battery wall transmitter sleeps between key presses, so an unacknowledged write to it is lost, yet the switch
# would show the written value as applied for good (review-3 P1).
STATUS_LED_PRODUCTS = P.PB_MAINS

# The parameters on the first (non-expert) Parameters page per device type, `device-settings.md` §1.1 / §2-§9.
# The status LED is not an app setting; it is enabled like the gateway integration's `status_led` switch.
FIRST_PAGE: dict[Page, frozenset[int]] = {
    "lamp": frozenset({0x100E, 0x000F, 0x1007, 0x100B}),
    "socket": frozenset({0x000F, 0x1007, 0x100B, 0xA001, 0xA002}),
    "control_switch": frozenset({0xA001, 0xA002, 0xA004, 0xA005, PROPERTY_STATUS_LED}),
    "mini": frozenset(),
    "rtr": frozenset({0x1247, 0x1240}),
    "detector": frozenset({0x600F, 0x6015, 0x6017}),
    "blind": frozenset({0x110A, 0x110B, 0x000F}),
}

# ----------------------------------------------------------------------------- spec -> platform


@dataclass(frozen=True, kw_only=True)
class PropertyEntityDescription:
    """How one property becomes an entity: the platform, the translation key and the read / write method."""

    spec: PropertySpec
    platform: Platform
    translation_key: str
    read: bool = (
        True  # False: never ask the device (the status LED has no readable state)
    )
    write: WriteMethod = "set"  # "status": written with a vendor Status, as the gateway drives the status LED

    @property
    def property_id(self) -> int:
        """The property id."""
        return self.spec.id


def describe(spec: PropertySpec) -> PropertyEntityDescription | None:
    """Map a property to its platform by codec, or None when it is not a config entity.

    Read-only ids are sensors (another wave), firmware-only ids have unknown layouts, struct codecs (thresholds,
    key scene / property configuration, edge detection, astro registers) and the `0x0001` lock flags need
    dedicated entities (`device_lock_targets`: one switch per flag). The lock function's struct has one: a switch
    of its own class.
    """
    if spec.id == PROPERTY_STATUS_LED:
        return PropertyEntityDescription(
            spec=spec,
            platform="switch",
            translation_key="status_led",
            read=False,
            write="status",
        )
    if spec.source != "app" or spec.access == "ro" or spec.id in UNSAFE_PROPERTIES:
        return None
    if spec.id == PROPERTY_REFERENCE_RUN or spec.access == "wo":
        return PropertyEntityDescription(  # a button has no state to read
            spec=spec, platform="button", translation_key=spec.name, read=False
        )
    codec = spec.codec
    if isinstance(codec, P.RgbMode):
        slot = "on" if spec.name.endswith("_on") else "off"
        return PropertyEntityDescription(
            spec=spec, platform="select", translation_key=f"led_colour_{slot}"
        )
    platform: Platform
    translation_key = spec.name
    if spec.id == PROPERTY_LOCK:  # a struct, with a switch class of its own
        platform = "switch"
        translation_key = "lock"
    elif isinstance(codec, P.Int | P.Scaled | P.Percent | P.Duration):
        platform = "number"
    elif isinstance(codec, P.Enum):
        platform = "select"
    elif isinstance(codec, P.Bool):
        platform = "switch"
    else:
        return None
    return PropertyEntityDescription(
        spec=spec, platform=platform, translation_key=translation_key
    )


def descriptions() -> list[PropertyEntityDescription]:
    """Every property that becomes a config entity, by id."""
    return [d for spec in P.PROPERTIES.values() if (d := describe(spec)) is not None]


# ----------------------------------------------------------------------------- element resolution


@dataclass(frozen=True, kw_only=True)
class EntityTarget:
    """What an entity is bound to: the element it talks to, the HA device it shows under, its name and default."""

    node: Node
    address: int
    unique_id: str
    device_info: DeviceInfo
    page: Page
    key: str | None = (
        None  # key letter, when the device groups several keys and the name needs it
    )
    enabled_default: bool = True
    read: bool = True

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The properties the entity reads."""
        raise NotImplementedError

    @property
    def base_translation_key(self) -> str:
        """The translation key without the key-letter variant."""
        raise NotImplementedError

    @property
    def translation_key(self) -> str:
        """The entity's translation key; `_key` variants carry the key letter in their name."""
        key = self.base_translation_key
        return f"{key}_key" if self.key else key


@dataclass(frozen=True, kw_only=True)
class PropertyTarget(EntityTarget):
    """One property of one element."""

    description: PropertyEntityDescription

    @property
    def spec(self) -> PropertySpec:
        """The property."""
        return self.description.spec

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The property, as a tuple."""
        return (self.description.spec,)

    @property
    def base_translation_key(self) -> str:
        """The description's translation key."""
        return self.description.translation_key


@dataclass(frozen=True, kw_only=True)
class NightModeTarget(EntityTarget):
    """The LED night-mode switch of one node: every LED mode property of the node, under LED 1's device."""

    property_ids: tuple[int, ...]

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The LED mode properties."""
        return tuple(P.PROPERTIES[pid] for pid in self.property_ids)

    @property
    def base_translation_key(self) -> str:
        """Always `led_night_mode`."""
        return "led_night_mode"


# The app's *Synchronise buttons* (`LedColorParametersProvider`): LED 1's on / off colour is copied to LED 2, on the
# 2-gang push-button and wall transmitter only (`LedColorParametersProvider.a`: `Z7.g`, `Z7.j`).
LED_SYNC_PRODUCTS = frozenset({0x02, 0x06})
LED_SYNC_PAIRS = {
    0xA001: 0xA004,
    0xA002: 0xA005,
}  # LED 1's property -> LED 2's it is copied to


@dataclass(frozen=True, kw_only=True)
class LedSyncTarget(EntityTarget):
    """The LED colour synchronisation of a 2-gang node, under LED 1's device like the night mode."""

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """LED 1's on / off modes, each followed by LED 2's it is copied to: the order the app writes them in."""
        return tuple(
            P.PROPERTIES[pid] for pair in LED_SYNC_PAIRS.items() for pid in pair
        )

    @property
    def base_translation_key(self) -> str:
        """Always `led_colour_sync`."""
        return "led_colour_sync"


EdgePart = Literal["mode", "rising", "falling"]
EDGE_PARTS: dict[EdgePart, Platform] = {
    "mode": "switch",
    "rising": "select",
    "falling": "select",
}


@dataclass(frozen=True, kw_only=True)
class EdgeDetectionTarget(EntityTarget):
    """One field of a mini-actuator input's edge evaluation (0x5009): the mode, or one edge's behaviour.

    The app's Display tab for the inputs E1 / E2 (`docs/gap-analysis/device-settings.md` §6.2): *Edge evaluation*
    on / off and, per edge, no reaction / on / off / toggle — three entities over one byte.
    """

    part: EdgePart

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The edge-detection property."""
        return (P.PROPERTIES[PROPERTY_EDGE_DETECTION],)

    @property
    def base_translation_key(self) -> str:
        """`input_edge_mode`, `input_edge_rising` or `input_edge_falling`."""
        return f"input_edge_{self.part}"


@dataclass(frozen=True, kw_only=True)
class KeyModeTarget(EntityTarget):
    """A key's mode (0x5003) as a diagnostic sensor: shown, never written.

    A mode means nothing without the publications of a connection, which `assign_key` sets up together with it
    (`UNSAFE_PROPERTIES`).
    """

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The key-mode property."""
        return (P.PROPERTIES[PROPERTY_KEY_MODE],)

    @property
    def base_translation_key(self) -> str:
        """Always `key_mode`."""
        return "key_mode"


@dataclass(frozen=True, kw_only=True)
class ValueTarget(EntityTarget):
    """One property of one element behind an entity of its own class, named `name` (not a codec-mapped parameter).

    A read-only state (the detector's continuous on / off, the thermostat's open window, the blind's reference-run
    state `0x110D`, the gateway's status `0xC000` / `0xC002`), a setting of its own kind (the blind's lock function and
    wind alarm, `0x0009`), or a property the app writes as part of a procedure (the walking test).
    """

    property_id: int
    name: str

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The one property."""
        return (P.PROPERTIES[self.property_id],)

    @property
    def base_translation_key(self) -> str:
        """The entity's name."""
        return self.name


@dataclass(frozen=True, kw_only=True)
class FlagTarget(ValueTarget):
    """One named bit of a bit-field property (`P.Flags`): a flag of the device lock, of the gateway's API status."""

    flag: str

    @property
    def bit(self) -> int:
        """The flag's bit index in the word (the codec's order)."""
        codec = self.specs[0].codec
        assert isinstance(codec, P.Flags)
        return codec.names.index(self.flag)


@dataclass(frozen=True, kw_only=True)
class SetupState:
    """A SIG setup-server state of a load: the Get, the Status that answers it (and every Set), its hosting models.

    `models`: the element hosts one of them — the setup server, or one that extends it (the Light Lightness
    Setup Server extends the Generic Power OnOff Setup Server). `size`: the Status's minimum length. `coded`: the
    Status starts with a status code, and one other than 0 (Success) answers a Set that did not take.
    """

    name: str
    get: Callable[[], bytes]
    status: int
    models: frozenset[str]
    size: int
    coded: bool = False


ON_POWER_UP = SetupState(
    name="on_power_up",
    get=M.generic_onpowerup_get,
    status=M.GEN_ONPOWERUP_STATUS,
    models=frozenset({"1007", "1301"}),
    size=1,  # [state u8] 0 off, 1 on (default), 2 restore
)
LIGHTNESS_RANGE = SetupState(
    name="lightness_range",
    get=M.light_lightness_range_get,
    status=M.LIGHT_LIGHTNESS_RANGE_STATUS,
    models=frozenset({"1301"}),
    size=5,  # [status u8][min u16][max u16]
    coded=True,  # 1 Cannot Set Range Min, 2 Cannot Set Range Max
)
LIGHTNESS_DEFAULT = SetupState(
    name="lightness_default",
    get=M.light_lightness_default_get,
    status=M.LIGHT_LIGHTNESS_DEFAULT_STATUS,
    models=frozenset({"1301"}),
    size=2,  # [lightness u16], 0 = the last lightness
)
CTL_DEFAULT = SetupState(
    name="ctl_default",
    get=M.light_ctl_default_get,
    status=M.LIGHT_CTL_DEFAULT_STATUS,
    models=frozenset({"1304"}),
    size=6,  # [lightness u16][temperature K u16][delta UV s16]
)
# The app's "White area" (`p234v7/V0.java`), on the Light CTL Setup Server's element like the CTL Default. Not in
# SETUP_STATES: its Status is the light's temperature range too, and the hub's handler caches it for both
# (`JungHomeHub._on_ctl_range_status`; a message type has one handler).
CTL_TEMPERATURE_RANGE = SetupState(
    name="ctl_temperature_range",
    get=M.light_ctl_temperature_range_get,
    status=M.LIGHT_CTL_TEMP_RANGE_STATUS,
    models=frozenset({"1304"}),
    size=5,  # [status u8][min K u16][max K u16]
    coded=True,  # 1 Cannot Set Range Min, 2 Cannot Set Range Max
)
SETUP_STATES = {
    s.status: s for s in (ON_POWER_UP, LIGHTNESS_RANGE, LIGHTNESS_DEFAULT, CTL_DEFAULT)
}
# entity (its translation key) -> platform, state, enabled by default: the lamp's first Parameters page has the
# brightness range, the switch-on values and "use previous value"; the behaviour after mains return and the white
# area are expert (`device-settings.md` §1.1, S5, §4.2, §4.3)
SETUP_ENTITIES: dict[str, tuple[Platform, SetupState, bool]] = {
    "power_on_behaviour": ("select", ON_POWER_UP, False),
    "lightness_min": ("number", LIGHTNESS_RANGE, True),
    "lightness_max": ("number", LIGHTNESS_RANGE, True),
    "default_lightness": ("number", LIGHTNESS_DEFAULT, True),
    "use_last_lightness": ("switch", LIGHTNESS_DEFAULT, True),
    "default_color_temp": ("number", CTL_DEFAULT, True),
    "color_temp_min": ("number", CTL_TEMPERATURE_RANGE, False),
    "color_temp_max": ("number", CTL_TEMPERATURE_RANGE, False),
}


@dataclass(frozen=True, kw_only=True)
class SetupTarget(EntityTarget):
    """One entity over a SIG setup state of a light or socket; no vendor property behind it."""

    state: SetupState
    entity: str

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """None: a setup state is not a property."""
        return ()

    @property
    def base_translation_key(self) -> str:
        """The entity's key in `SETUP_ENTITIES`."""
        return self.entity


@dataclass(frozen=True, kw_only=True)
class _Element:
    """An element a property resolves to, with the device it shows under."""

    address: int
    device_info: DeviceInfo
    page: Page
    location: int
    key: str | None = None
    load_kind: str | None = None


def node_version(hub: JungHomeHub, node: Node) -> str | None:
    """Return the node's software version (SIG 0x001A) once it has been read into the state cache, else None."""
    return software_version(hub, node)


def _node_page(node: Node) -> Page:
    pid = node.pid or 0
    if pid in P.GATEWAY:
        return "gateway"
    if pid in P.RTR:
        return "rtr"
    if pid in P.DETECTORS:
        return "detector"
    if pid in P.SOCKETS:
        return "socket"
    if pid in P.MINI_ACTUATORS:
        return "mini"
    return "control_switch"


def _load_element(hub: JungHomeHub, element: Element) -> _Element | None:
    """Return the element as a load (under its light / socket device) when a load device was derived from it."""
    device = hub.devices.by_address.get(element.address)
    if isinstance(device, Light):
        return _Element(
            address=element.address,
            device_info=light_device_info(hub, device),
            page="lamp",
            location=element.location,
            load_kind=device.kind,
        )
    if isinstance(device, Socket):
        return _Element(
            address=element.address,
            device_info=socket_device_info(hub, device),
            page="socket",
            location=element.location,
            load_kind="socket",
        )
    if isinstance(device, Blind):
        return _Element(
            address=element.address,
            device_info=blind_device_info(hub, device),
            page="blind",
            location=element.location,
            load_kind="blind",
        )
    return None


def _other(hub: JungHomeHub, node: Node, element: Element) -> _Element:
    return _Element(
        address=element.address,
        device_info=node_device_info(hub, node),
        page=_node_page(node),
        location=element.location,
    )


def _primary(hub: JungHomeHub, node: Node) -> _Element:
    """Return the primary element: under its load device when it hosts one (push-buttons, sockets, pucks), else the node's."""
    element = node.elements[0]
    return _load_element(hub, element) or _other(hub, node, element)


def _loads(hub: JungHomeHub, node: Node) -> Iterator[_Element]:
    for element in node.elements:
        if element.location in LOAD_LOCATIONS and (load := _load_element(hub, element)):
            yield load


def _key(hub: JungHomeHub, button: Button) -> _Element:
    gang = button_gang(hub, button)
    return _Element(
        address=button.address,
        device_info=buttons_device_info(hub, gang),
        page="control_switch" if button.node.pid in P.PUSH_BUTTONS else "mini",
        location=button.location,
        key=button.key if len(gang) > 1 else None,
    )


def _keys(hub: JungHomeHub, node: Node) -> Iterator[_Element]:
    for button in hub.devices.buttons:
        if button.node is node:
            yield _key(hub, button)


def _led(hub: JungHomeHub, node: Node, led: int) -> _Element:
    """LED `led` (1-based) is addressed at the primary element (the app's rule) and belongs to key `led` of the node.

    Key `led` is the node's `led`-th key in element-location order — the app's `LedPosition` is the rocker
    *ordinal* (docs/android/properties.md §LedPosition), not a location: a 2-gang whose keys sit at 0x40 / 0x42
    has LED 2 on key C, not on nothing. The same ordinal `migration.py` derives for the gateway's key letters.
    A socket has no key: its LED stays under the socket device.
    """
    primary = _primary(hub, node)
    keys = sorted(
        (b for b in hub.devices.buttons if b.node is node), key=lambda b: b.location
    )
    button = keys[led - 1] if led - 1 < len(keys) else None
    if button is None:
        return primary
    key = _key(hub, button)
    return _Element(
        address=primary.address,
        device_info=key.device_info,
        page=key.page,
        location=primary.location,
        key=key.key,
    )


def _led_index(spec: PropertySpec) -> int:
    return (spec.id - 0xA000) // 3 + 1


def _elements_for(hub: JungHomeHub, node: Node, spec: PropertySpec) -> list[_Element]:
    """Return the elements of `node` a property is addressed to, per its `element` rule (`properties.py`).

    *Automatic daylight saving time* is a node property the app shows on a lamp, socket or blind page only
    (`AutomaticDaylightSavingTimeEnabledCompatible`: `LampDevice`, `SocketDevice`, `BlindDevice`): a node without a
    load (a push-button with an extension or no insert) has no such page, so no entity.
    """
    if spec.element == "node":
        on_a_page = spec.id != PROPERTY_AUTOMATIC_DST or any(_loads(hub, node))
        return [_primary(hub, node)] if on_a_page else []
    if spec.element == "load":
        return [e for e in _loads(hub, node) if _applies_to_load(spec, e.load_kind)]
    if spec.element == "key":
        return list(_keys(hub, node))
    if spec.element == "led":
        return [_led(hub, node, _led_index(spec))]
    if spec.element == "detector":
        return [_other(hub, node, node.elements[-1])]
    return [  # aux
        _other(hub, node, e) for e in node.elements if e.location == AUX_LOCATION
    ]


def _applies_to_load(spec: PropertySpec, kind: str | None) -> bool:
    if spec.id in LOAD_KINDS:
        return kind in LOAD_KINDS[spec.id]
    if spec.id in BLIND_PROPERTIES:
        return kind == "blind"
    return kind in LAMP_KINDS or kind == "socket"


def _candidates(hub: JungHomeHub, node: Node) -> Iterator[PropertyEntityDescription]:
    pid = node.pid or 0
    version = node_version(hub, node)
    for description in descriptions():
        spec = description.spec
        products = (
            STATUS_LED_PRODUCTS if spec.id == PROPERTY_STATUS_LED else spec.products
        )
        if pid in products and P.supported(spec, version):
            yield description


def config_targets(
    hub: JungHomeHub, platform: Platform | None = None
) -> list[PropertyTarget]:
    """Every config entity of the network (optionally one platform's), in node / property order."""
    out: list[PropertyTarget] = []
    for node in hub.cdb.nodes:
        if node.pid is None:
            continue
        for description in _candidates(hub, node):
            if platform is not None and description.platform != platform:
                continue
            spec = description.spec
            for element in _elements_for(hub, node, spec):
                page: Page = "blind" if spec.id in BLIND_PROPERTIES else element.page
                out.append(
                    PropertyTarget(
                        description=description,
                        node=node,
                        address=element.address,
                        unique_id=f"{node.uuid.lower()}-{element.location:04x}-{spec.name}",
                        device_info=element.device_info,
                        page=page,
                        key=element.key,
                        enabled_default=spec.id in FIRST_PAGE[page],
                        read=description.read,
                    )
                )
    return out


def lock_targets(hub: JungHomeHub) -> list[PropertyTarget]:
    """Return the lock function of every lockable load: lights, sockets, blinds (`LOAD_KINDS`)."""
    return [t for t in config_targets(hub, "switch") if t.spec.id == PROPERTY_LOCK]


def edge_detection_targets(
    hub: JungHomeHub, platform: Platform
) -> list[EdgeDetectionTarget]:
    """Return the edge-evaluation entities of `platform` for every input of every mini actuator, off by default.

    The app keeps them on the inputs' Display tab, not the Parameters page; an input is a key (`_keys`).
    """
    spec = P.PROPERTIES[PROPERTY_EDGE_DETECTION]
    parts = [part for part, p in EDGE_PARTS.items() if p == platform]
    out: list[EdgeDetectionTarget] = []
    for node in hub.cdb.nodes:
        if (node.pid or 0) not in spec.products or not P.supported(
            spec, node_version(hub, node)
        ):
            continue
        for element in _keys(hub, node):
            out += [
                EdgeDetectionTarget(
                    node=node,
                    address=element.address,
                    unique_id=f"{node.uuid.lower()}-{element.location:04x}-input_edge_{part}",
                    device_info=element.device_info,
                    page=element.page,
                    key=element.key,
                    enabled_default=False,
                    part=part,
                )
                for part in parts
            ]
    return out


def key_mode_targets(hub: JungHomeHub) -> list[KeyModeTarget]:
    """Return the key-mode sensor of every key and input, off by default: one read per key and connection."""
    spec = P.PROPERTIES[PROPERTY_KEY_MODE]
    out: list[KeyModeTarget] = []
    for button in hub.devices.buttons:
        node = button.node
        if (node.pid or 0) not in spec.products or not P.supported(
            spec, node_version(hub, node)
        ):
            continue
        element = _key(hub, button)
        out.append(
            KeyModeTarget(
                node=node,
                address=button.address,
                unique_id=f"{node.uuid.lower()}-{button.location:04x}-key_mode",
                device_info=element.device_info,
                page=element.page,
                key=element.key,
                enabled_default=False,
            )
        )
    return out


# The device-lock flags the app offers, with the products it offers them on: *Lock operation* and *Lock factory
# reset* on the Parameters page of every device type with a node of its own, *Key lock* and *Lock configuration on
# the unit* on the room thermostat's (`device-settings.md` §3.2, §5.1, §6.1, §8.2, §9.2). Bit 0, the local
# factory-reset lock, has no place in the app.
DEVICE_LOCK_FLAGS: dict[str, frozenset[int]] = {
    "local_devices_lock": P.NOT_GATEWAY,
    "factory_reset_time_limit": P.NOT_GATEWAY,
    "key_lock": P.RTR,
    "configuration_lock": P.RTR,
}
# Enabled by default: *Lock operation*, in the app's normal list on every page it is on, now that its bit is
# confirmed on air (the app settings session: 0x0004). *Key lock* is in the room thermostat's normal list
# too, but its bit 3 has not been seen on air (no thermostat here); the other two are expert parameters.
DEVICE_LOCK_ENABLED = frozenset({"local_devices_lock"})


def device_lock_targets(hub: JungHomeHub) -> list[FlagTarget]:
    """Return the device-lock switches of every node; only *Lock operation* is on by default (`DEVICE_LOCK_ENABLED`).

    The app addresses `0x0001` to the primary element, the detector's too (`device-settings.md` §1.2); the
    switches show under that element's device, like the other node parameters.
    """
    spec = P.PROPERTIES[PROPERTY_DEVICE_LOCK]
    out: list[FlagTarget] = []
    for node in hub.cdb.nodes:
        pid = node.pid or 0
        if pid not in spec.products:
            continue
        element = _primary(hub, node)
        out += [
            FlagTarget(
                node=node,
                address=element.address,
                unique_id=f"{node.uuid.lower()}-{element.location:04x}-{flag}",
                device_info=element.device_info,
                page=element.page,
                enabled_default=flag in DEVICE_LOCK_ENABLED,
                property_id=PROPERTY_DEVICE_LOCK,
                name=flag,
                flag=flag,
            )
            for flag, products in DEVICE_LOCK_FLAGS.items()
            if pid in products
        ]
    return out


def property_id_targets(
    hub: JungHomeHub, property_id: int, translation: str
) -> list[ValueTarget]:
    """Return one target per element the property is addressed to on every node that has it; off by default.

    Only for entities nobody has seen work on a real device yet (a detector's, a thermostat's): the unique id is the
    config entities' (`<node uuid>-<location>-<property name>`).
    """
    spec = P.PROPERTIES[property_id]
    out: list[ValueTarget] = []
    for node in hub.cdb.nodes:
        if (node.pid or 0) not in spec.products or not P.supported(
            spec, node_version(hub, node)
        ):
            continue
        out += [
            ValueTarget(
                node=node,
                address=element.address,
                unique_id=f"{node.uuid.lower()}-{element.location:04x}-{spec.name}",
                device_info=element.device_info,
                page=element.page,
                enabled_default=False,
                property_id=property_id,
                name=translation,
            )
            for element in _elements_for(hub, node, spec)
        ]
    return out


def blind_targets(
    hub: JungHomeHub, property_id: int, name: str, *, enabled_default: bool
) -> list[ValueTarget]:
    """Return one entity per blind over a property of its position element, under the blind's device."""
    spec = P.PROPERTIES[property_id]
    return [
        ValueTarget(
            node=blind.node,
            address=blind.address,
            unique_id=f"{blind.unique_id}-{name}",
            device_info=blind_device_info(hub, blind),
            page="blind",
            enabled_default=enabled_default,
            property_id=property_id,
            name=name,
        )
        for blind in hub.devices.blinds
        if (blind.node.pid or 0) in spec.products
    ]


def _gateways(hub: JungHomeHub) -> Iterator[tuple[Node, _Element]]:
    """Every gateway node with its primary element, whose Manufacturer server holds the gateway's status."""
    for node in hub.cdb.nodes:
        if (node.pid or 0) in P.GATEWAY:
            yield node, _other(hub, node, node.elements[0])


def gateway_status_targets(hub: JungHomeHub) -> list[FlagTarget]:
    """Return a binary sensor per flag of every gateway's API status (`0xC000`): API available, client waiting.

    Client waiting is off by default: a gateway whose configuration has no `api_client_name_asking` setting reports
    it set with nothing pending (`JungHomeGatewayStatus`).
    """
    return [
        FlagTarget(
            node=node,
            address=element.address,
            unique_id=f"node:{node.uuid.lower()}-gateway_{flag}",
            device_info=element.device_info,
            page="gateway",
            property_id=GATEWAY_API_STATUS,
            name=f"gateway_{flag}",
            flag=flag,
            enabled_default=flag != "client_waiting_for_approval",
        )
        for node, element in _gateways(hub)
        for flag in P.GATEWAY_STATUS_FLAGS
    ]


def gateway_ip_targets(hub: JungHomeHub) -> list[ValueTarget]:
    """Return the IP address sensor of every gateway (`0xC002`)."""
    return [
        ValueTarget(
            node=node,
            address=element.address,
            unique_id=f"node:{node.uuid.lower()}-gateway_ip",
            device_info=element.device_info,
            page="gateway",
            property_id=GATEWAY_IP,
            name="gateway_ip",
        )
        for node, element in _gateways(hub)
    ]


def led_sync_targets(hub: JungHomeHub) -> list[LedSyncTarget]:
    """One LED colour synchronisation per 2-gang node, on the first page there; its four colours always exist.

    Nothing is read for it: the app keeps the flag itself (`UpdateSyncLed`), so does Home Assistant (the switch's
    restored state).
    """
    out: list[LedSyncTarget] = []
    for node in hub.cdb.nodes:
        if (node.pid or 0) not in LED_SYNC_PRODUCTS:
            continue
        element = _led(hub, node, 1)  # node-wide, so no key letter in the name
        out.append(
            LedSyncTarget(
                node=node,
                address=element.address,
                unique_id=f"{node.uuid.lower()}-{element.location:04x}-led_colour_sync",
                device_info=element.device_info,
                page=element.page,
                read=False,
            )
        )
    return out


def night_mode_targets(hub: JungHomeHub) -> list[NightModeTarget]:
    """One night-mode switch per node with LEDs (mains push-buttons and sockets); it is on the first page there.

    Not on a battery wall transmitter: the app hides the cell on battery devices (`G(0)` = `!Q1()`,
    `PROV/G.java:27-30`).
    """
    out: list[NightModeTarget] = []
    for node in hub.cdb.nodes:
        if node.pid in BATTERY_PIDS:
            continue
        pids = tuple(
            d.property_id
            for d in _candidates(hub, node)
            if isinstance(d.spec.codec, P.RgbMode)
        )
        if not pids:
            continue
        element = _led(hub, node, 1)  # node-wide, so no key letter in the name
        out.append(
            NightModeTarget(
                node=node,
                address=element.address,
                unique_id=f"{node.uuid.lower()}-{element.location:04x}-led_night_mode",
                device_info=element.device_info,
                page=element.page,
                property_ids=pids,
            )
        )
    return out


def setup_targets(hub: JungHomeHub, platform: Platform) -> list[SetupTarget]:
    """Return the setup-state entities of `platform` for every light and socket whose element hosts the state.

    A blind hosts the Generic Power OnOff Setup Server too, but its behaviour after mains return is its own
    property (0x1105, `move_on_power_mode`).
    """
    out: list[SetupTarget] = []
    for node in hub.cdb.nodes:
        for element in node.elements:
            load = (
                _load_element(hub, element)
                if element.location in LOAD_LOCATIONS
                else None
            )
            if load is None or load.load_kind == "blind":
                continue
            out += [
                SetupTarget(
                    node=node,
                    address=element.address,
                    unique_id=f"{node.uuid.lower()}-{element.location:04x}-{name}",
                    device_info=load.device_info,
                    page=load.page,
                    enabled_default=enabled,
                    state=state,
                    entity=name,
                )
                for name, (p, state, enabled) in SETUP_ENTITIES.items()
                if p == platform and not state.models.isdisjoint(element.models)
            ]
    return out


# ----------------------------------------------------------------------------- status handler


def is_secret(pid: int) -> bool:
    """Whether the vendor property is a credential (the gateway's API token, 0xC001): never cached, never shown."""
    spec = P.PROPERTIES.get(pid)
    return (
        spec is not None and isinstance(spec.codec, P.Text) and bool(spec.codec.secret)
    )


def cacheable(pid: int) -> bool:
    """Whether a vendor property Status value may be kept in `ElementState.properties`.

    Only catalogued properties are, and of the gateway's own block (0xC00x: API status, API token, IP, certificate
    fingerprint) only what its entities show, the API status and the IP (`gateway_status_targets`): the gateway
    answers the phone app's reads of the others over the mesh too, the proxy forwards those replies to us, nothing
    here renders them, and the cache ends up in the diagnostics.
    """
    spec = P.PROPERTIES.get(pid)
    return (
        spec is not None
        and not is_secret(pid)
        and (spec.products != P.GATEWAY or pid in (GATEWAY_API_STATUS, GATEWAY_IP))
    )


def redacted(pid: int) -> bool:
    """Whether the diagnostics hide a cached property: a secret, or the gateway's address (the entry's is too)."""
    return is_secret(pid) or pid == GATEWAY_IP


@register_status_handler(
    *M.VENDOR_PROPERTY_STATUS_OPCODES.values(), company_id=M.JUNG_CID
)
def _on_vendor_property_status(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
    """Cache the value of a catalogued vendor property Status (`[pid u16][access u8][value…]`), solicited or not.

    A property the catalogue does not know, or a secret (`cacheable`), is left alone: a Status is still counted
    as traffic and answers a pending request either way. So is a Status without a value — the id alone, or the id
    and the access byte — which is how an element answers for a property it does not have (the metering socket's
    meter element to an Admin Set of 0x5003, on air): the app's resolver drops it
    (`StatusMessageResolver` `AbstractC1972z0`, `UtilsKt.k`) and keeps the value it had, so does this. The value is
    the sender's, except for a status LED written as a Status (`status_owner`).
    """
    if len(p) <= 3:
        return
    pid = int.from_bytes(p[:2], "little")
    if not cacheable(pid):
        return
    owner = status_owner(hub, m, pid)
    hub.element_state(owner).properties[pid] = bytes(p[3:])
    hub.notify_update(owner)


def status_owner(hub: JungHomeHub, m: AccessMessage, pid: int) -> int:
    """Return the element whose value the vendor Status `m` of `pid` carries: its sender, or the key it writes.

    The gateway drives a key's status LED with a User Property Status *to* the key element (on air: gateway →
    push-button, `air:access:11-0527:0x5013`; no reply follows), the way `PropertyReader.write_status` does. Such a
    Status is a write: the value is the receiving key's, so its status-LED switch follows what the gateway set.
    Only a User Status of the status LED to a mains push-button's element, from a node other than that push-button
    and not to Home Assistant, counts; every other Status describes its sender.
    """
    if (
        pid == PROPERTY_STATUS_LED
        and m.opcode == M.VENDOR_PROPERTY_STATUS_OPCODES["user"]
        and m.dst != hub.proxy.state.src
    ):
        node = hub.cdb.node_by_addr(m.dst)
        if (
            node is not None
            and node.pid in STATUS_LED_PRODUCTS
            and hub.cdb.node_by_addr(m.src) is not node
        ):
            return m.dst
    return m.src


@register_status_handler(*SETUP_STATES)
def _on_setup_status(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
    """Cache a SIG setup-state Status, solicited or published; a CTL Default carries the Lightness Default too.

    The Light CTL Default state's lightness *is* the Light Lightness Default (Mesh Model spec §6.1.3.4): each Status
    updates the other's copy, so a CTL Default Set built from the cache keeps the switch-on brightness set last.
    """
    state = SETUP_STATES[m.opcode]
    if len(p) < state.size:
        return
    setup = hub.element_state(m.src).setup
    setup[state.status] = bytes(p[: state.size])
    if state is CTL_DEFAULT:
        setup[LIGHTNESS_DEFAULT.status] = bytes(p[:2])
    elif state is LIGHTNESS_DEFAULT and (ctl := setup.get(CTL_DEFAULT.status)):
        setup[CTL_DEFAULT.status] = bytes(p[:2]) + ctl[2:]
    hub.notify_update(m.src)


def property_id_of(m: AccessMessage) -> int | None:
    """Return the property id a vendor Status carries, None when it is too short."""
    return int.from_bytes(m.params[:2], "little") if len(m.params) >= 2 else None


def has_value(m: AccessMessage) -> bool:
    """Whether the vendor Status `m` carries a value after its property id and access byte."""
    return len(m.params) > 3


def is_status_of(pid: int, m: AccessMessage) -> bool:
    """Whether the vendor Status `m` carries property `pid` (the `match` of a request for it)."""
    return property_id_of(m) == pid


def applied(spec: PropertySpec, sent: bytes, held: bytes | None) -> bool:
    """Whether the value an element reports shows that a Set of `sent` took: the same bytes.

    A lock only by being locked or not: the time and value it reports are what it keeps, not what was sent (a
    lock of the current state carries no value).
    """
    if held is None:
        return False
    if not isinstance(spec.codec, P.EnforcedOutputCodec):
        return held == sent
    try:
        return spec.codec.decode(held).locked == spec.codec.decode(sent).locked
    except ValueError:
        return False


def vendor_server(spec: PropertySpec) -> VendorServer:
    """Return the LBC server hosting `spec`; config entities exist for vendor properties only."""
    if spec.server in ("admin", "manufacturer", "user"):
        return spec.server
    raise ValueError(f"{spec.name} is not a vendor property")


# ----------------------------------------------------------------------------- reads and writes

READERS: HassKey[dict[str, PropertyReader]] = HassKey(f"{DOMAIN}_property_readers")
Job = Callable[[], Awaitable[None]]


@dataclass(eq=False)
class _Queued:
    """A job in the reader's queue: the element it reads, what it reads (`key`), the link it was queued for.

    `link` is the hub's `link_count` the job was queued on; None for one queued while no link was up, which waits
    for the next link, whichever it is.
    """

    addr: int
    key: object
    link: int | None
    job: Job


class PropertyReader:
    """The mesh side of the config entities of one hub: rate-limited initial reads, serialised per element.

    `schedule` queues a job (an entity's first read) for an element. The worker starts `PROPERTY_READ_DELAY`
    after its first job, so the hub's connect-time state refresh goes first, then works the queue
    `PROPERTY_READ_CHUNK` jobs at a time (to distinct elements, since exchanges with one element are serialised)
    with a `PROPERTY_READ_PAUSE` between chunks, like that refresh. `read`, `write` and `write_status` talk to
    the element, one exchange per element at a time, so a Status is never taken for the answer to another
    property's Get. A property several entities share (the LED colours and the night mode) is read once: a read
    that succeeded within `PROPERTY_READ_FRESH` is not repeated.

    A job is queued once (review-4 R4-5): one still waiting is kept in its place and counted for the current link,
    and one queued on a link that went away is dropped when its turn comes — its entity queues it again on the next
    link if it still wants it. Several quick drops used to leave a copy per link in the queue, each read in turn.
    Unverified on air.

    An entity that rewrites a value other entities write too (an LED colour and the night-mode byte, the three
    fields of an edge-evaluation byte, the two ends of a lightness range) holds `modifying(addr)` from reading the
    current value to its write: two such changes at once would otherwise both start from the same value and the
    second Set would undo the first's.
    """

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to `hub`; no worker until the first job."""
        self.hub = hub
        self._jobs: deque[_Queued] = deque()
        # (address, key) -> its entry in `_jobs`, while it waits there
        self._queued: dict[tuple[int, object], _Queued] = {}
        self._worker: asyncio.Task[None] | None = None
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # per element, around a whole read-modify-write; `_locks` is taken inside it by each exchange
        self._modify_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._read_at: dict[
            tuple[int, int | str], float
        ] = {}  # (address, property id or setup state name) -> when it last answered
        # node unicast -> `hub.link_count` its version was last queued on (`schedule_version`)
        self._version_link: dict[int, int] = {}
        # node unicast -> when the last read of its node information that got every answer started (`_read_version`)
        self._version_read: dict[int, datetime] = {}
        # load address -> the time limit (s, 0 = none) its lock switch sends, set by its `number` entity
        self.lock_time_limits: dict[int, int] = {}
        # LED element address -> whether LED 1's colours are copied to LED 2 (`switch.JungHomeLedColourSync`)
        self.led_sync: dict[int, bool] = {}

    @callback
    def schedule(self, addr: int, job: Job, *, key: object = None) -> None:
        """Queue `job`, a read of the element at `addr`, and make sure the worker runs.

        `key` names what the job reads (by default the job itself: an entity's bound method equals itself on every
        call). A job with the same address and key still waiting is not queued again: it keeps its place and is
        counted for the current link.
        """
        link = self.hub.link_count if self.hub.connected else None
        ident = (addr, job if key is None else key)
        queued = self._queued.get(ident)
        if queued is not None:
            queued.link, queued.job = link, job
        else:
            self._queued[ident] = item = _Queued(addr, ident[1], link, job)
            self._jobs.append(item)
        if self._worker is None or self._worker.done():
            self._worker = self.hub.entry.async_create_background_task(
                self.hub.hass, self._run(), f"{DOMAIN} property reads"
            )

    def _take_chunk(self) -> list[Job]:
        """Dequeue up to `PROPERTY_READ_CHUNK` jobs for distinct elements, oldest first; drop those of lost links.

        One pass that rebuilds the queue: taking each job out with `deque.remove` cost a scan of the queue per job.
        """
        current = self.hub.link_count
        chunk: list[Job] = []
        addrs: set[int] = set()
        rest: deque[_Queued] = deque()
        for item in self._jobs:
            if item.link is not None and item.link != current:
                del self._queued[item.addr, item.key]  # queued on a link that is gone
            elif len(chunk) < PROPERTY_READ_CHUNK and item.addr not in addrs:
                del self._queued[item.addr, item.key]
                chunk.append(item.job)
                addrs.add(item.addr)
            else:
                rest.append(item)
        self._jobs = rest
        return chunk

    async def _wait_for_setup(self) -> bool:
        """Hold the first reads until every platform has queued its jobs (the entry is LOADED).

        Entities schedule their reads while their platform is being set up; starting the worker before the last
        platform is through would let the first chunk skip elements that are still to come. Returns False when
        the entry never loaded (the reads would go nowhere).
        """
        entry = self.hub.entry
        state = entry.state  # a local: the attribute changes while we wait below
        if state is not ConfigEntryState.SETUP_IN_PROGRESS:
            return state is ConfigEntryState.LOADED
        settled = asyncio.Event()
        unsub = entry.async_on_state_change(
            lambda: (
                settled.set()
                if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS
                else None
            )
        )
        try:
            await settled.wait()
        finally:
            unsub()
        return entry.state is ConfigEntryState.LOADED

    async def _run(self) -> None:
        if not await self._wait_for_setup():
            return
        await asyncio.sleep(PROPERTY_READ_DELAY)
        while self._jobs:
            # no link: wait for one rather than run the queue into "not connected" (each read lost for nothing)
            while not self.hub.connected:
                await self.hub.async_wait_connected(LINK_WAIT_STEP)
            for result in await asyncio.gather(
                *(job() for job in self._take_chunk()), return_exceptions=True
            ):
                if isinstance(result, Exception):
                    _LOGGER.debug("property read failed: %r", result)
            if self._jobs:
                await asyncio.sleep(PROPERTY_READ_PAUSE)

    @callback
    def schedule_version(self, node: Node) -> None:
        """Queue a read of what the node tells about itself (`_read_version`), once per hub and node.

        The firmware gates (`node_version`: illuminance scaling, `_candidates`, the thermostat's property set)
        need the software version and nothing else asks for it. The hub keeps what it learns across the entry's
        reloads and on disk (`coordinator.NODE_VERSIONS`), so `_candidates` — which runs once, at setup, before any
        read — applies it from the next setup on, a restart's included.

        A read that got every answer is not repeated until the hub sees the node restart (`hub.restarted`: a
        firmware update restarts it; review-4 R4-5) — asked on every link, it cost a Get per node at every link-up
        and piled up in the queue when links came and went. One that went unanswered is queued again on the next
        link, at most once per link. Unverified on air.
        """
        unicast = node.unicast
        read = self._version_read.get(unicast)
        restarted = self.hub.restarted.get(unicast)
        if self._version_link.get(unicast) == self.hub.link_count or (
            read is not None and (restarted is None or restarted < read)
        ):
            return
        self._version_link[unicast] = self.hub.link_count
        self.schedule(unicast, partial(self._read_version, node), key="version")

    async def _read_version(self, node: Node) -> None:
        """Ask the node for its software version, then for what else of its node information is not known yet.

        The app reads the identity block (SIG 0x0011, 0x001A, 0x0010) and the time role on every opening of the
        device page (the settings session); here the software version is asked once per hub and restart of the
        node (`schedule_version`) and the rest once for good — a node's hardware revision, manufacturer name and
        LBC version blocks (0x0003 .. 0x0005, `NODE_INFO_VENDOR`) do not change while it keeps its address, and
        every Get is traffic at link-up. The time role is kept the same way: only the device diagnostics show it,
        nothing acts on it, Home Assistant never sets it, and every node on air answered "client". A node that does
        not answer the first Get is not asked the rest on this link. SIG Statuses land in the hub's cache through
        its property handler, the others through `remember_node_info` here.

        An item the node answers without a value (it does not have it) is remembered as not supported under the
        software version it just answered (`NODE_INFO_UNSUPPORTED`), so it is not asked on every link either; a
        firmware update asks again. Silence is not an answer: that item, and the version with it, is asked on the
        next link.
        """
        addr = node.unicast
        started = dt_util.utcnow()
        async with self._locks[addr]:
            version = await self._ask_sig(addr, SIG_SOFTWARE_VERSION)
            if version is None:
                _LOGGER.debug(
                    "%04X did not answer the Get of its software version", addr
                )
                return
            known = self.hub.node_info(addr)
            complete = True

            def wanted(name: str) -> bool:
                return (
                    name not in known
                    and known.get(name + NODE_INFO_UNSUPPORTED) != version
                )

            for pid in (SIG_HARDWARE_REVISION, SIG_MANUFACTURER_NAME):
                name = NODE_INFO[pid]
                if wanted(name):
                    answer = await self._ask_sig(addr, pid)
                    complete = complete and answer is not None
                    if answer == b"":
                        self._unsupported(addr, name, version)
            for pid, name in NODE_INFO_VENDOR.items():
                if wanted(name) and (node.pid or 0) in P.PROPERTIES[pid].products:
                    answer = await self._ask_vendor_info(addr, pid, name)
                    complete = complete and answer is not None
                    if answer == b"":
                        self._unsupported(addr, name, version)
            if NODE_INFO_TIME_ROLE not in known:
                complete = await self._ask_time_role(node) and complete
            if complete:
                self._version_read[addr] = started

    def _unsupported(self, addr: int, name: str, version: bytes) -> None:
        """Remember that the node at `addr` has no item `name` under software version `version` (`_read_version`)."""
        _LOGGER.debug("%04X has no %s (software version %s)", addr, name, version.hex())
        self.hub.remember_node_info(addr, name + NODE_INFO_UNSUPPORTED, version)

    async def _ask(
        self,
        addr: int,
        pdu: bytes,
        opcode: int,
        what: str,
        *,
        cid: int | None = None,
        match: Callable[[AccessMessage], bool] | None = None,
    ) -> AccessMessage | None:
        """Send one Get of the node's information; its answer, None when it stayed silent."""
        try:
            return await self.hub.proxy.request(
                addr,
                pdu,
                opcode,
                timeout=const.PROPERTY_READ_TIMEOUT,
                retries=PROPERTY_READ_RETRIES,
                expect_cid=cid,
                match=match,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer the Get of its %s", addr, what)
            return None

    async def _ask_sig(self, addr: int, pid: int) -> bytes | None:
        """Ask for SIG Manufacturer property `pid` (the hub's handler keeps the answer).

        Returns the value answered (empty: a Status without one), None when the node stayed silent.
        """
        wanted = pid.to_bytes(2, "little")
        reply = await self._ask(
            addr,
            M.generic_property_get("manufacturer", pid),
            M.GEN_MANU_PROP_STATUS,
            NODE_INFO[pid],
            match=lambda m: m.params[:2] == wanted,
        )
        return None if reply is None else bytes(reply.params[3:])

    async def _ask_vendor_info(self, addr: int, pid: int, name: str) -> bytes | None:
        """Ask for LBC Manufacturer property `pid` and keep a value the node answers with as its `name`.

        Returns the value answered (empty: a Status without one), None when the node stayed silent.
        """
        reply = await self._ask(
            addr,
            M.vendor_property_get("manufacturer", pid),
            M.VENDOR_PROPERTY_STATUS_OPCODES["manufacturer"],
            name,
            cid=M.JUNG_CID,
            match=partial(is_status_of, pid),
        )
        if reply is None:
            return None
        value = bytes(reply.params[3:])
        if value:
            self.hub.remember_node_info(addr, name, value)
        return value

    async def _ask_time_role(self, node: Node) -> bool:
        """Send Time Role Get to the node's Time Setup Server (`1201`, every JUNG node has one) and keep its role.

        Returns False when the role is still to be asked: the node stayed silent or answered no role.
        """
        element = next(
            (e for e in node.elements if TIME_SETUP_SERVER in e.models), None
        )
        if element is None:
            return True
        reply = await self._ask(
            element.address, M.time_role_get(), M.TIME_ROLE_STATUS, NODE_INFO_TIME_ROLE
        )
        if reply is None:
            return False
        try:
            M.decode_time_role_status(reply.params)
        except ValueError as err:
            _LOGGER.debug("%04X: %s", element.address, err)
            return False
        self.hub.remember_node_info(node.unicast, NODE_INFO_TIME_ROLE, reply.params[:1])
        return True

    def modifying(self, addr: int) -> asyncio.Lock:
        """Return the lock a read-modify-write of a value of the element at `addr` holds (class docstring)."""
        return self._modify_locks[addr]

    def cached(self, addr: int, spec: PropertySpec) -> bytes | None:
        """Return the cached wire value of the property, None until the element reported it."""
        st = self.hub.states.get(addr)
        return st.properties.get(spec.id) if st else None

    async def read(
        self, addr: int, spec: PropertySpec, *, since: float | None = None
    ) -> bool:
        """Ask the element for the property; True when its value is cached afterwards (fresh, or just answered).

        `since` (a `time.monotonic()`) asks unless the element answered at or after that moment, however recent the
        last read: the value changes on the device's own (a timed lock ends), and several entities showing it that
        want it read back at once (the lock switch, select and wind alarm) share one Get.

        False when the element stayed silent through the attempts or the link went away: the caller keeps the read
        open and asks again on the next link (`PropertyEntity._maybe_read`, the cover's mode read) — a device
        that was asleep, out of range or drowned out by the connect-time traffic usually answers the next time.
        """
        try:
            return await self.fetch(addr, spec, since=since)
        except ConnectionError as err:
            _LOGGER.debug("read of %s from %04X aborted: %s", spec.name, addr, err)
            return False

    async def fetch(
        self, addr: int, spec: PropertySpec, *, since: float | None = None
    ) -> bool:
        """`read`, but a lost link raises `ConnectionError` instead of counting as silence.

        For a change that has to tell the two apart: a battery node that stays silent is asleep, a lost link is not
        (`PropertyEntity.read_current`).
        """
        async with self._locks[addr]:
            read_at = self._read_at.get((addr, spec.id), -1e9)
            fresh = (
                read_at >= since
                if since is not None
                else time.monotonic() - read_at < PROPERTY_READ_FRESH
            )
            if fresh and self.cached(addr, spec) is not None:
                return True
            await self._get(addr, spec)
        return self.cached(addr, spec) is not None

    async def _get(self, addr: int, spec: PropertySpec) -> bool:
        """Send the Get; True when the element answered with the property's Status.

        Only a Status of `spec` answers it: another property's (a battery node's keep-alive, `keep_awake.py`, or a
        late one) is not taken for it.
        """
        server = vendor_server(spec)
        try:
            await self.hub.proxy.request(
                addr,
                M.vendor_property_get(server, spec.id),
                M.VENDOR_PROPERTY_STATUS_OPCODES[server],
                timeout=const.PROPERTY_READ_TIMEOUT,
                retries=PROPERTY_READ_RETRIES,
                expect_cid=M.JUNG_CID,
                match=partial(is_status_of, spec.id),
            )
        except TimeoutError:
            _LOGGER.debug(
                "%04X did not answer the Get of %s (0x%04X)", addr, spec.name, spec.id
            )
            return False
        self._read_at[addr, spec.id] = time.monotonic()
        return True

    async def write(self, addr: int, spec: PropertySpec, value: Any) -> WriteOutcome:
        """Send an acknowledged Set with the encoded `value`; re-read the property when no Status answered it.

        Returns how it ended (`WriteOutcome`): a Status answering the Set counts as applied, whatever value it
        carries; a read-back only when it reports the value sent (`applied`). A Status without a value (`has_value`)
        answers the Set too, as in the app, which neither resends nor reads back then: the element does not have
        the property, and nothing is cached.
        """
        server = vendor_server(spec)
        raw = spec.codec.encode(value)
        pdu = M.vendor_property_set(
            server, spec.id, raw, ack=True, user_access=spec.set_access
        )
        async with self._locks[addr]:
            try:
                reply = await self.hub.proxy.request(
                    addr,
                    pdu,
                    M.VENDOR_PROPERTY_STATUS_OPCODES[server],
                    timeout=const.PROPERTY_WRITE_TIMEOUT,
                    retries=1,
                    expect_cid=M.JUNG_CID,
                    match=partial(is_status_of, spec.id),  # as `_get`
                )
            except TimeoutError:
                reply = None
            if reply is not None and not has_value(reply):
                _LOGGER.debug(
                    "%04X answered the Set of %s without a value: not supported",
                    addr,
                    spec.name,
                )
                return "not_supported"
            if reply is None or property_id_of(reply) != spec.id:
                _LOGGER.debug(
                    "%04X did not confirm the Set of %s; reading it back",
                    addr,
                    spec.name,
                )
                await asyncio.sleep(PROPERTY_REREAD_DELAY)
                if not await self._get(addr, spec):
                    return "no_answer"
                if not applied(spec, raw, self.cached(addr, spec)):
                    return "not_applied"
        return "applied"

    async def write_status(self, addr: int, spec: PropertySpec, value: Any) -> None:
        """Write the property the way the gateway drives the status LED: a User Property Status, no reply."""
        raw = spec.codec.encode(value)
        await self.hub.proxy.send_access(
            addr, M.vendor_property_status("user", spec.id, raw)
        )
        self.hub.element_state(addr).properties[spec.id] = raw
        self.hub.notify_update(addr)

    def cached_setup(self, addr: int, state: SetupState) -> bytes | None:
        """Return the cached Status parameters of the setup state, None until the element reported it."""
        st = self.hub.states.get(addr)
        return st.setup.get(state.status) if st else None

    async def read_setup(self, addr: int, state: SetupState) -> bool:
        """Ask the element for the setup state, like `read`: True when it is cached afterwards."""
        async with self._locks[addr]:
            fresh = time.monotonic() - self._read_at.get((addr, state.name), -1e9)
            if (
                fresh < PROPERTY_READ_FRESH
                and self.cached_setup(addr, state) is not None
            ):
                return True
            try:
                await self._get_setup(addr, state)
            except ConnectionError as err:
                _LOGGER.debug("read of %s from %04X aborted: %s", state.name, addr, err)
                return False
        return self.cached_setup(addr, state) is not None

    async def _get_setup(self, addr: int, state: SetupState) -> bool:
        """Send the Get; True when the element answered it."""
        try:
            await self.hub.proxy.request(
                addr,
                state.get(),
                state.status,
                timeout=const.PROPERTY_READ_TIMEOUT,
                retries=PROPERTY_READ_RETRIES,
            )
        except TimeoutError:
            _LOGGER.debug("%04X did not answer the Get of its %s", addr, state.name)
            return False
        self._read_at[addr, state.name] = time.monotonic()
        return True

    async def write_setup(
        self, addr: int, state: SetupState, pdu: bytes
    ) -> WriteOutcome:
        """Send an acknowledged setup Set; re-read the state when no Status answered it. Returns how it ended.

        The Lightness Range Set is answered by a *publication* of its Status only (`device-settings.md` §13 q.3);
        a reply is matched on source and opcode, whatever its destination; the CTL Temperature Range Set was not
        answered at all on air (`hidden-features.md` §9), so its outcome is the read-back's. A Status whose status
        code is not Success (a range's Cannot Set Range Min / Max) answers a Set that did not take. A read-back
        counts as applied when the Status repeats the Set's parameters (a range's after its status code).
        """
        async with self._locks[addr]:
            try:
                reply = await self.hub.proxy.request(
                    addr,
                    pdu,
                    state.status,
                    timeout=const.PROPERTY_WRITE_TIMEOUT,
                    retries=1,
                )
            except TimeoutError:
                _LOGGER.debug(
                    "%04X did not confirm the Set of its %s; reading it back",
                    addr,
                    state.name,
                )
            else:
                if state.coded and reply.params[:1] != b"\x00":
                    _LOGGER.debug(
                        "%04X refused the Set of its %s: status code %s",
                        addr,
                        state.name,
                        reply.params[:1].hex() or "missing",
                    )
                    return "not_applied"
                return "applied"
            await asyncio.sleep(PROPERTY_REREAD_DELAY)
            if not await self._get_setup(addr, state):
                return "no_answer"
            _, _, params = decode_opcode(pdu)
            held = self.cached_setup(addr, state) or b""
            return "applied" if held.endswith(params) else "not_applied"


def property_reader(hass: HomeAssistant, hub: JungHomeHub) -> PropertyReader:
    """Return the hub's reader, created on first use and dropped when the entry unloads."""
    readers = hass.data.setdefault(READERS, {})
    entry_id = hub.entry.entry_id
    if entry_id not in readers:
        readers[entry_id] = PropertyReader(hub)

        def forget() -> None:
            readers.pop(entry_id, None)

        hub.entry.async_on_unload(forget)
    return readers[entry_id]


# ----------------------------------------------------------------------------- entity base


def check_outcome(
    outcome: WriteOutcome, entity_id: str, *, compare: bool = True
) -> None:
    """Raise the translated error for a write the element did not confirm; `compare`: also for another value."""
    if outcome in ("no_answer", "not_supported") or (
        compare and outcome == "not_applied"
    ):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=f"setting_{outcome}",
            translation_placeholders={"entity": entity_id},
        )


def cached_value(hub: JungHomeHub, addr: int, spec: PropertySpec) -> Any:
    """Return the decoded cached value of `spec` on element `addr`, None when unknown or malformed."""
    st = hub.states.get(addr)
    raw = st.properties.get(spec.id) if st else None
    if raw is None:
        return None
    try:
        return spec.codec.decode(raw)
    except ValueError:
        return None


class ConfigEntity(JungHomeEntity):
    """A config entity of one element, read once per link through the hub's reader (`_read`).

    A battery node's (`BATTERY_PIDS`) sleeps at link-up and would not answer: it is read when one of the node's
    keys reports an event instead (`_on_key_event`). A change to it runs under `changing` and a silent node is
    reported as asleep (`asleep`): the user wakes it with a key press and tries again (review-3 W4 / F24).
    """

    _attr_entity_category: EntityCategory | None = EntityCategory.CONFIG

    def __init__(self, hub: JungHomeHub, target: EntityTarget) -> None:
        """Bind to `target`."""
        super().__init__(hub, target.address, target.unique_id, target.device_info)
        self.target = target
        self._attr_translation_key = target.translation_key
        if target.key:
            self._attr_translation_placeholders = {"key": target.key}
        self._attr_entity_registry_enabled_default = target.enabled_default
        self._attr_extra_state_attributes = {"mesh_address": f"{target.address:04X}"}
        self._read_done = not target.read
        self._read_pending = False
        self._read_link: int | None = (
            None  # `hub.link_count` of the link the read was last queued on
        )
        self._battery = target.node.pid in BATTERY_PIDS

    @property
    def reader(self) -> PropertyReader:
        """The hub's property reader."""
        return property_reader(self.hass, self.hub)

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates, then read the values once the link is up (a battery node's: its keys too)."""
        await super().async_added_to_hass()
        if self._battery:
            for button in self.hub.devices.buttons:
                if button.node is self.target.node:
                    self.async_on_remove(
                        self.hub.add_event_listener(button.address, self._on_key_event)
                    )
        self._maybe_read()

    @callback
    def _handle_update(self) -> None:
        self._maybe_read()
        super()._handle_update()

    @callback
    def _maybe_read(self) -> None:
        """Queue the read when the link is up and no value is cached yet — once per link.

        A read that got no answer (or was cut by a lost link) is queued again on the next link, not on the next
        update of this one: the element was asked and stayed silent, asking again through the same link would only
        add to the traffic that may have drowned the first attempt. Never for a battery node (`_on_key_event`).
        A read still queued from a lost link is queued again regardless: the reader drops the lost link's copy
        (`PropertyReader.schedule`) and keeps one.
        """
        if not self.hub.connected or self._battery:
            return
        self.reader.schedule_version(self.target.node)
        if self._read_done or self._read_link == self.hub.link_count:
            return
        self._read_pending = True
        self._read_link = self.hub.link_count
        self.reader.schedule(self.address, self._initial_read)

    @callback
    def _on_key_event(self, event: str, attrs: dict[str, Any]) -> None:
        """Read now that a key of the battery node reported: it is awake for a moment, too short for the queue.

        A read that got no answer is tried again at the next key event, as the battery level is
        (`sensor.JungHomeBatterySensor`).
        """
        if not self.hub.connected or self._read_done or self._read_pending:
            return
        self._read_pending = True
        self.hub.entry.async_create_background_task(
            self.hass,
            self._initial_read(),
            f"{DOMAIN} property read {self.address:04X}",
        )

    async def _initial_read(self) -> None:
        try:
            self._read_done = await self._read()
        finally:
            self._read_pending = False

    async def _read(self) -> bool:
        """Read what the entity shows; True when all of it is cached afterwards."""
        raise NotImplementedError

    @asynccontextmanager
    async def changing(self) -> AsyncIterator[None]:
        """Hold the element's `modifying` lock and keep a battery node awake, from reading the value to writing it."""
        async with (
            self.reader.modifying(self.address),
            self.hub.keep_awake.hold([self.address]),
        ):
            yield

    def asleep(self) -> HomeAssistantError:
        """Return the error for a battery node that did not answer a change: press one of its keys, then change again."""
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="setting_asleep",
            translation_placeholders={"entity": self.entity_id},
        )


class PropertyEntity(ConfigEntity):
    """A config entity bound to the properties of one element: values from the state cache, writes through the reader."""

    def __init__(self, hub: JungHomeHub, target: EntityTarget) -> None:
        """Bind to `target`."""
        super().__init__(hub, target)
        self.specs = target.specs
        self.spec = self.specs[0]
        self._attr_extra_state_attributes["property_id"] = ", ".join(
            f"0x{s.id:04X}" for s in self.specs
        )

    async def _read(self) -> bool:
        done = True
        for spec in self.specs:
            done = await self.reader.read(self.address, spec) and done
        return done

    async def read_current(self, spec: PropertySpec) -> None:
        """Read `spec` before a change that keeps the rest of its value; a battery node that stays silent is asleep.

        A lost link fails the change as a send failure, a battery node's included: it is not the node's sleep.
        """
        try:
            answered = await self.reader.fetch(self.address, spec)
        except (ConnectionError, OSError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err
        if not answered and self._battery:
            raise self.asleep()

    def value_of(self, spec: PropertySpec) -> Any:
        """Return the decoded cached value of `spec`, None when unknown or malformed."""
        raw = self.reader.cached(self.address, spec)
        if raw is None:
            return None
        try:
            return spec.codec.decode(raw)
        except ValueError:
            return None

    @property
    def property_value(self) -> Any:
        """The decoded value of the (first) property."""
        return self.value_of(self.spec)

    async def async_write_value(
        self,
        value: Any,
        spec: PropertySpec | None = None,
        *,
        compare: bool | None = None,
    ) -> None:
        """Write `value` (a decoded Python value) to `spec` (default: the entity's property).

        `compare`: whether a read-back showing another value is an error (default: when the entity reads its value;
        a trigger reads back what it is doing). An entity that is not read itself but writes readable properties
        passes True.
        """
        spec = self.spec if spec is None else spec
        by_status = (
            isinstance(self.target, PropertyTarget)
            and self.target.description.write == "status"
        )
        try:
            async with self.hub.keep_awake.hold([self.address]):
                if by_status:
                    await self.reader.write_status(self.address, spec, value)
                    return
                outcome = await self.reader.write(self.address, spec, value)
        except ValueError as err:  # the value does not fit the codec
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="value_rejected",
                translation_placeholders={"entity": self.entity_id, "error": str(err)},
            ) from err
        except (ConnectionError, OSError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err
        if outcome == "no_answer" and self._battery:
            raise self.asleep()
        # a trigger (a button, nothing to read) reads back what it is doing, not the value written
        check_outcome(
            outcome,
            self.entity_id,
            compare=self.target.read if compare is None else compare,
        )


class EdgeDetectionEntity(PropertyEntity):
    """One field of an input's edge evaluation; a change rewrites the byte with the other two fields as read."""

    target: EdgeDetectionTarget

    @property
    def edge_detection(self) -> P.EdgeDetection | None:
        """The decoded byte; None until the input reported it."""
        value = self.property_value
        return value if isinstance(value, P.EdgeDetection) else None

    async def async_write_part(self, **change: Any) -> None:
        """Write the byte with `change` applied; the current byte is read first when not known, never guessed."""
        async with self.changing():
            current = self.edge_detection
            if current is None:
                await self.read_current(self.spec)
                current = self.edge_detection
            if current is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="edge_detection_unknown",
                    translation_placeholders={"entity": self.entity_id},
                )
            await self.async_write_value(replace(current, **change))


class FlagEntity(PropertyEntity):
    """An entity over one flag of a bit-field property (`FlagTarget`): the device lock, the gateway's API status."""

    target: FlagTarget

    @property
    def word(self) -> int | None:
        """The whole bit field as the element reported it, bits without a name included; None until read (or short)."""
        value = self.property_value  # decodes only a complete word
        raw = self.reader.cached(self.address, self.spec)
        if value is None or raw is None:
            return None
        codec = self.spec.codec
        assert isinstance(codec, P.Flags)
        return int.from_bytes(raw[: codec.size], "little")

    @property
    def flag(self) -> bool | None:
        """The entity's flag; None until the element reported the word."""
        word = self.word
        return None if word is None else bool(word >> self.target.bit & 1)


LOCK_MODES = {  # (command, priority) -> the `lock_mode` attribute
    (P.ENFORCE_LOCK, P.PRIORITY_NORMAL): "keep_state",
    (P.ENFORCE_LOCK, P.PRIORITY_LOCKOUT): "lockout_protection",
}


def lock_mode(value: P.EnforcedOutput) -> str:
    """Name a lock the way the app tells them apart: kept state, lock-out protection, wind alarm, enforced value."""
    if value.wind_alarm:
        return "wind_alarm"
    return LOCK_MODES.get((value.command, value.priority), "enforced_value")


class LockFunctionEntity(PropertyEntity):
    """An entity over a load's lock function (0x0009): the *Lock* switch, a blind's *Lock function* and *Wind alarm*.

    Nothing publishes the lock state (`docs/gap-analysis/control-and-state.md` §5 q. 1): it is read once per link like
    every config entity, and read back when a timed lock should have ended — the device ends it on its own and
    tells no one. Entities of the same load that want the read-back at the same moment share one Get
    (`PropertyReader.read`'s `since`).
    """

    _expiry: CALLBACK_TYPE | None = None

    @property
    def lock(self) -> P.EnforcedOutput | None:
        """The lock state; None until the element reported it."""
        value = self.property_value
        return value if isinstance(value, P.EnforcedOutput) else None

    async def async_will_remove_from_hass(self) -> None:
        """Drop a pending read-back."""
        self._cancel_expiry()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_update(self) -> None:
        """Follow a timed lock whoever set it (the app, a key, another controller): read it back once it should end.

        A lock reported with a time limit gets the same read-back as one this entity sent; a read-back that still
        finds it locked schedules the next one.
        """
        value = self.lock
        if value is not None and value.locked and value.time_s:
            if self._expiry is None:
                self._expiry = async_call_later(
                    self.hass, value.time_s + LOCK_EXPIRY_MARGIN, self._lock_expired
                )
        else:
            self._cancel_expiry()
        super()._handle_update()

    async def async_lock(self, value: P.EnforcedOutput) -> None:
        """Send a lock; one with a time limit is read back when it should have ended, counted from now."""
        self._cancel_expiry()
        await self.async_write_value(value)
        if value.time_s:  # with the time sent, whatever the Status reported
            self._cancel_expiry()
            self._expiry = async_call_later(
                self.hass, value.time_s + LOCK_EXPIRY_MARGIN, self._lock_expired
            )

    async def async_unlock(self) -> None:
        """Unlock: command 0 with the priority, time and value last read, as the app does."""
        self._cancel_expiry()
        current = self.lock
        if current is not None:
            await self.async_write_value(replace(current, command=P.ENFORCE_UNLOCK))
        else:
            await self.async_write_value(P.UNLOCK)

    @callback
    def _cancel_expiry(self) -> None:
        if self._expiry is not None:
            self._expiry()
            self._expiry = None

    @callback
    def _lock_expired(self, _now: Any) -> None:
        """Read the lock back once its time limit has passed: the device unlocked itself, and says so to no one."""
        self._expiry = None
        self.hub.entry.async_create_background_task(
            self.hass,
            self.reader.read(self.address, self.spec, since=time.monotonic()),
            f"{DOMAIN} lock read-back {self.address:04X}",
        )


def u16(raw: bytes, offset: int = 0, *, signed: bool = False) -> int:
    """Return the little-endian 16-bit field of a Status at `offset`."""
    return int.from_bytes(raw[offset : offset + 2], "little", signed=signed)


class SetupStateEntity(ConfigEntity):
    """A config entity over a SIG setup state: its value is the cached Status, a change is the state's Set."""

    target: SetupTarget

    async def _read(self) -> bool:
        return await self.reader.read_setup(self.address, self.target.state)

    @property
    def setup_value(self) -> bytes | None:
        """The cached Status parameters; None until the element reported them."""
        return self.reader.cached_setup(self.address, self.target.state)

    async def async_current(self) -> bytes:
        """Return the state for a Set that keeps its other fields: read first when not known, never guessed."""
        raw = self.setup_value
        if raw is None:
            await self.reader.read_setup(self.address, self.target.state)
            raw = self.setup_value
        if raw is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="setup_state_unknown",
                translation_placeholders={"entity": self.entity_id},
            )
        return raw

    async def async_write_setup(self, build: Callable[..., bytes], *args: int) -> None:
        """Send the Set `build(*args)` makes; a value the builder refuses is the user's error, not a send failure."""
        try:
            pdu = build(*args)
        except ValueError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="value_rejected",
                translation_placeholders={"entity": self.entity_id, "error": str(err)},
            ) from err
        try:
            outcome = await self.reader.write_setup(
                self.address, self.target.state, pdu
            )
        except (ConnectionError, OSError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="send_failed"
            ) from err
        check_outcome(outcome, self.entity_id)


def retired_unique_ids(hub: JungHomeHub) -> list[tuple[Platform, str]]:
    """Return the config entities an earlier version created that the app's gates now leave out, with their platform.

    The dim mode of a tunable-white load (`LOAD_KINDS`), *Automatic daylight saving time* on a node without a load
    (`_elements_for`) and *Night mode* on a battery wall transmitter (`night_mode_targets`): nothing else would ever
    clear their registry entries (`drop_retired_entities`).
    """
    out: list[tuple[Platform, str]] = []
    for node in hub.cdb.nodes:
        if node.pid is None:
            continue
        uuid = node.uuid.lower()
        night_mode = False
        for description in _candidates(hub, node):
            spec = description.spec
            if spec.id == PROPERTY_DIM_MODE:
                out += [
                    (description.platform, f"{uuid}-{e.location:04x}-{spec.name}")
                    for e in _loads(hub, node)
                    if e.load_kind == "ctl"
                ]
            elif spec.id == PROPERTY_AUTOMATIC_DST and not any(_loads(hub, node)):
                location = _primary(hub, node).location
                out.append((description.platform, f"{uuid}-{location:04x}-{spec.name}"))
            night_mode |= isinstance(spec.codec, P.RgbMode)
        if night_mode and node.pid in BATTERY_PIDS:
            location = _led(hub, node, 1).location
            out.append(("switch", f"{uuid}-{location:04x}-led_night_mode"))
    return out
