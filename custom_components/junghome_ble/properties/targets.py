"""Config entity targets: which config entities exist, the element each one talks to and the device it shows under.

Every parameter is a JUNG vendor property (`jhmesh/properties.py`, catalogue of `docs/android/properties.md`).
This module maps each writable `PropertySpec` to an HA platform by its codec (`describe`) and resolves the mesh
element it is addressed to and the HA device it belongs to (`config_targets` and the other `*_targets`);
`properties/reader.py` reads and writes the values, `config_entities.py` holds the entity classes. Nothing here is
an entity.

Enabled by default are only the parameters the app shows on the *first* Parameters page of the device type
(`FIRST_PAGE`, `device-settings.md` §1.1); the expert-mode ones exist but are disabled in the entity registry.

A few app parameters are SIG setup-server states instead (`SETUP_STATES`: behaviour after mains return, the
brightness range, the switch-on brightness and colour temperature; `CTL_TEMPERATURE_RANGE`, the white area): same
flow, their own Get / Set / Status (`SetupTarget`, `SetupStateEntity`), cached in `ElementState.setup`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from custom_components.junghome_ble.entity import (
    blind_device_info,
    button_gang,
    buttons_device_info,
    light_device_info,
    node_device_info,
    socket_device_info,
    software_version,
)
from custom_components.junghome_ble.jhmesh import messages as M
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.devices import (
    BATTERY_PIDS,
    LOAD_LOCATIONS,
    Blind,
    Button,
    Light,
    Socket,
)

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo

    from custom_components.junghome_ble.coordinator import JungHomeHub
    from custom_components.junghome_ble.jhmesh.cdb import Element, Node
    from custom_components.junghome_ble.jhmesh.properties import PropertySpec

Platform = Literal["number", "select", "switch", "button"]
WriteMethod = Literal["set", "status"]
Page = Literal[
    "lamp", "socket", "control_switch", "mini", "rtr", "detector", "blind", "gateway"
]

PROPERTY_STATUS_LED = 0x5013
PROPERTY_REFERENCE_RUN = 0x110D
PROPERTY_LOCK = 0x0009  # EnforceOutput, the app's lock (`JungHomeLockSwitch`, `ElementState.note_lock`)
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
# Firmware-only ids (`PropertySpec.source == "firmware"`) whose layout and effect a supervised probe settled on air:
# only these become config entities, and always disabled by default (`config_targets`); an id without a codec
# stays Raw and unmapped here even when listed. The on-air sweep (`docs/on-air-sweep.md` A7 / C6,
# `docs/hidden-features.md` §13) settled the DALI insert's hotel function: its enable (0x1009), the level an off
# leaves the light at (0x1008) and the night level beside it (0x1011, effect unseen). Left out: 0x0F00 (no effect
# seen and no meaning in the app), 0x500C (the app has no such setting, its effect needs a person at the key) and
# the presentation records 0x1012 / 0x1013 (layout unknown).
PROPERTY_HOTEL_VALUE, PROPERTY_HOTEL_FUNCTION, PROPERTY_NIGHT_VALUE = (
    0x1008,
    0x1009,
    0x1011,
)
DALI_INSERT_PROPERTIES = frozenset(
    {PROPERTY_HOTEL_VALUE, PROPERTY_HOTEL_FUNCTION, PROPERTY_NIGHT_VALUE}
)
FIRMWARE_ENTITIES: frozenset[int] = DALI_INSERT_PROPERTIES
PROPERTY_WALKING_TEST, PROPERTY_PRESENCE_CONTROL = 0x6001, 0x6003
PIR_SENSOR_C = P.PROPERTIES[0x600A]  # a presence detector's only (`retired_unique_ids`)
# Load-element properties that only apply to some load kinds (`Light.kind`); the rest apply to every load.
LAMP_KINDS = frozenset({"switch", "dimmer", "ctl"})
# The dim mode only on a dimmer: the app's `DimLampDevice` is `DimModeCompatible`, its `TunableWhiteLampDevice`
# (a DALI tunable-white load, kind "ctl") is not (`Y7/f0.java`, `Y7/t0.java`).
# The hotel function only on a tunable-white (DALI) load: the sweep saw a switch insert answer 0x1008 with the id
# alone, and has no dimmer insert to try.
LOAD_KINDS: dict[int, frozenset[str]] = {
    PROPERTY_DIM_MODE: frozenset({"dimmer"}),
    PROPERTY_DIM_TO_WARM: frozenset({"ctl"}),
    PROPERTY_LOCK: LAMP_KINDS | {"socket", "blind"},
    **dict.fromkeys(DALI_INSERT_PROPERTIES, frozenset({"ctl"})),
}
BLIND_PROPERTIES = range(0x1100, 0x1200)  # a blind load: no such device is derived yet
# Keys with an LED: mini-actuator inputs publish key events too, but have nothing to light. Mains push-buttons only:
# a battery wall transmitter sleeps between key presses, so an unacknowledged write to it is lost, yet the switch
# would show the written value as applied for good.
STATUS_LED_PRODUCTS = P.PB_MAINS
# The products an entity is offered on where they are narrower than the catalogue's: the status LED's above, and the
# hotel function on the push-button's DALI insert, the only one seen serving it (a DALI mini actuator is untried).
ENTITY_PRODUCTS: dict[int, frozenset[int]] = {
    PROPERTY_STATUS_LED: STATUS_LED_PRODUCTS,
    **dict.fromkeys(DALI_INSERT_PROPERTIES, P.PB_MAINS),
}

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

    Read-only ids are sensors (another wave), firmware-only ids have unknown layouts (except `FIRMWARE_ENTITIES`,
    the ones a probe settled), struct codecs (thresholds, key scene / property configuration, edge detection,
    astro registers) and the `0x0001` lock flags need dedicated entities (`device_lock_targets`: one switch per
    flag). The lock function's struct has one: a switch of its own class.
    """
    if spec.id == PROPERTY_STATUS_LED:
        return PropertyEntityDescription(
            spec=spec,
            platform="switch",
            translation_key="status_led",
            read=False,
            write="status",
        )
    settled = spec.source == "app" or spec.id in FIRMWARE_ENTITIES
    if not settled or spec.access == "ro" or spec.id in UNSAFE_PROPERTIES:
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
        products = ENTITY_PRODUCTS.get(spec.id, spec.products)
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
                        # an expert parameter, or a settled firmware-only one: disabled by default
                        enabled_default=spec.id in FIRST_PAGE[page]
                        and spec.id not in FIRMWARE_ENTITIES,
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


def retired_unique_ids(hub: JungHomeHub) -> list[tuple[Platform, str]]:
    """Return the config entities an earlier version created that the app's gates now leave out, with their platform.

    The dim mode of a tunable-white load (`LOAD_KINDS`), *Automatic daylight saving time* on a node without a load
    (`_elements_for`), *Night mode* on a battery wall transmitter (`night_mode_targets`) and *Activation area C* on a
    motion detector, which has two PIR segments (`P.PRESENCE_DETECTOR`): nothing else would ever clear their
    registry entries (`drop_retired_entities`).
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
        if node.pid in P.DETECTORS - P.PRESENCE_DETECTOR:
            location = node.elements[-1].location
            out.append(("number", f"{uuid}-{location:04x}-{PIR_SENSOR_C.name}"))
    return out
