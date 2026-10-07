"""Power thresholds of the metering socket: the app's *Automatic* profile (`docs/gap-analysis/network-features.md` §4.3).

A metering socket switches other loads by itself when its power stays at a level for a while: the *switch-on*
threshold (`0x5004`) sends *on*, the *switch-off* one (`0x5005`) *off*, each "`power` W for `duration` s" and
enabled or not, as an 8-byte LBC Admin property (`properties.ThresholdCodec`). What it switches is wiring, not a
property: the socket's OnOff Client publishes to its element group and every target load's JUNG User Property
Server and OnOff server subscribe there (`mesh_config.MeshConfigurator.set_threshold_devices`); both thresholds
share that list. Once neither is active (disabled or deleted) the app unwires the loads again and resets the
client's publication (`MeshConfigurator.unwire_threshold`).

The *Switch-on threshold* / *Switch-off threshold* sensors (diagnostic, off by default) show each, read once per
link; the `set_threshold` / `delete_threshold` actions (`actions/thresholds.py`) write them. Formats and wiring are the
app's, the message sequences as captured on air; HA's own have not been tried on a device yet. Only the metering socket
has them, not every metered load: the app gives the energy puck's output (`MeasureLampDevice`) the consumption page but
no thresholds (`docs/android/properties.md` §4, `docs/gap-analysis/device-settings.md` §5.3), and the properties name
the socket alone (`SOCKET_METERING`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .config_entities import EntityTarget, node_version, property_reader
from .const import DOMAIN
from .device_info import address_label
from .entity import socket_device_info
from .jhmesh import properties as P
from .jhmesh.export import cdb_element_groups
from .mesh_config import (
    APPLIED_NOTHING,
    Applied,
    applied_message,
    applied_text,
    said,
    threshold_client,
    threshold_devices,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import JungHomeHub
    from .jhmesh.devices import Socket
    from .jhmesh.properties import PropertySpec

Which = Literal["switch_on", "switch_off"]
THRESHOLD_PROPERTIES: dict[Which, int] = {"switch_on": 0x5004, "switch_off": 0x5005}
OTHER_THRESHOLD: dict[Which, Which] = {
    "switch_on": "switch_off",
    "switch_off": "switch_on",
}
# the error of a write, one key per threshold: Home Assistant translates a message, never a placeholder's value, so
# a `{which}` would show the raw `switch_on` in every language
NOT_APPLIED: dict[Which, str] = {
    "switch_on": "threshold_switch_on_not_applied",
    "switch_off": "threshold_switch_off_not_applied",
}
SEND_FAILED: dict[Which, str] = {
    "switch_on": "threshold_switch_on_send_failed",
    "switch_off": "threshold_switch_off_send_failed",
}
# the app's delete: no power, no time, not active
CLEARED = P.Threshold(None, 0, False)


@dataclass(frozen=True, kw_only=True)
class ThresholdTarget(EntityTarget):
    """One of a metering socket's two thresholds, shown on its socket device."""

    which: Which

    @property
    def spec(self) -> PropertySpec:
        """The threshold's property."""
        return P.PROPERTIES[THRESHOLD_PROPERTIES[self.which]]

    @property
    def specs(self) -> tuple[PropertySpec, ...]:
        """The threshold's property, as a tuple."""
        return (self.spec,)

    @property
    def base_translation_key(self) -> str:
        """`switch_on_threshold` or `switch_off_threshold`."""
        return f"{self.which}_threshold"


def has_thresholds(hub: JungHomeHub, socket: Socket) -> bool:
    """Whether the socket measures (the product has the properties) and has the client its thresholds switch with."""
    node = socket.node
    spec = P.PROPERTIES[THRESHOLD_PROPERTIES["switch_on"]]
    return (
        (node.pid or 0) in spec.products
        and P.supported(spec, node_version(hub, node))
        and threshold_client(node) is not None
    )


def threshold_targets(hub: JungHomeHub) -> list[ThresholdTarget]:
    """Return both threshold sensors of every metering socket, off by default."""
    out: list[ThresholdTarget] = []
    for socket in hub.devices.sockets:
        if not has_thresholds(hub, socket):
            continue
        element = hub.cdb.element(socket.address)
        assert element is not None  # a socket is derived from an element of the export
        out += [
            ThresholdTarget(
                node=socket.node,
                address=socket.address,
                unique_id=f"{socket.node.uuid.lower()}-{element.location:04x}-{which}_threshold",
                device_info=socket_device_info(hub, socket),
                page="socket",
                enabled_default=False,
                which=which,
            )
            for which in THRESHOLD_PROPERTIES
        ]
    return out


def switched_devices(hub: JungHomeHub, socket: Socket) -> list[int]:
    """Return the load elements the socket's thresholds switch, as the loaded export wires them."""
    client = threshold_client(socket.node)
    group = (
        None
        if client is None
        else cdb_element_groups(hub.cdb, hub.cdb.export_meta).get(client.address)
    )
    if client is None or group is None:
        return []
    return [e.address for e in threshold_devices(hub.cdb, client, group)]


@dataclass
class ThresholdProgress:
    """What a threshold action wrote so far, for the error of the write or wiring plan that stops it.

    A call writes socket after socket, each its threshold(s) and then its wiring: the error of a later one used
    to say that nothing before it was applied, though thresholds and whole sockets were. `written` are the
    thresholds of the socket under way, `finished` the sockets done. Said as data (`Applied`): one sentence per
    threshold or pair of a socket, so no `{which}` word is a placeholder's value. `names`: each socket as the
    errors name it (`address_label`), noted by `write_threshold`; one it did not note goes by its address.
    """

    written: dict[int, list[Which]] = field(default_factory=dict)
    finished: list[int] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)

    def name(self, address: int) -> str:
        """Return the socket at `address` as the errors name it."""
        return self.names.get(address, f"{address:04X}")

    def wrote(self, address: int, which: Which) -> None:
        """Note that the socket at `address` took its `which` threshold."""
        self.written.setdefault(address, []).append(which)

    def finish(self, address: int) -> None:
        """Note that the socket at `address` holds everything the call asked of it."""
        self.finished.append(address)
        self.written.clear()

    def done(self) -> Applied:
        """Return the sentences naming what was written; empty when nothing was."""
        done = Applied()
        if len(self.finished) == 1:
            done += said("socket_set", socket=self.name(self.finished[0]))
        elif self.finished:
            sockets = ", ".join(self.name(a) for a in self.finished)
            done += said("sockets_set", sockets=sockets)
        for address, which in self.written.items():
            key = "thresholds" if len(set(which)) > 1 else which[0]
            done += said(f"{key}_written", socket=self.name(address))
        return done

    def text(self) -> Applied:
        """Return the `applied` of a threshold write that failed."""
        done = self.done()
        if not done:
            return APPLIED_NOTHING
        return done + said("rerun")

    def applied(self, accepted: int, total: int) -> Applied:
        """Return the `applied` of a socket's wiring plan that stopped after `accepted` of `total` messages."""
        if accepted == 0:
            return self.text()
        return self.done() + applied_text(accepted, total)


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


async def current_threshold(
    hass: HomeAssistant, hub: JungHomeHub, socket: Socket, which: Which
) -> P.Threshold | None:
    """Return the threshold as the socket holds it, read when not known yet; None when it does not say."""
    reader = property_reader(hass, hub)
    spec = P.PROPERTIES[THRESHOLD_PROPERTIES[which]]
    raw = reader.cached(socket.address, spec)
    if raw is None:
        await reader.read(
            socket.address, spec
        )  # False, and still nothing cached, when it stays silent
        raw = reader.cached(socket.address, spec)
    try:
        return None if raw is None else spec.codec.decode(raw)
    except ValueError:
        return None


def planned_threshold(
    current: P.Threshold | None, data: dict[str, Any], name: str
) -> P.Threshold:
    """Return the threshold a `set_threshold` call asks for: its fields, the socket's current ones for the rest.

    Left out, `enabled` keeps the state of a threshold the socket holds; one it does not hold (none, or one
    cleared to no power) is written enabled.
    """
    power = data.get("power", current.power_w if current else None)
    duration = data.get("duration", current.time_s if current else None)
    if power is None or duration is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="threshold_incomplete",
            translation_placeholders={"name": name},
        )
    active = True if current is None or current.power_w is None else current.active
    enabled = data.get("enabled", active)
    # 0.1 W on air: the value the socket answers with, for the comparison after the write
    return P.Threshold(round(float(power) * 10) / 10, int(duration), bool(enabled))


async def write_threshold(
    hass: HomeAssistant,
    hub: JungHomeHub,
    socket: Socket,
    which: Which,
    value: P.Threshold,
    progress: ThresholdProgress | None = None,
) -> None:
    """Write one threshold and check the socket holds it afterwards (its Status, or the read-back).

    A failure says what the call wrote before it (`progress`, which records this write once it took).
    """
    progress = progress if progress is not None else ThresholdProgress()
    reader = property_reader(hass, hub)
    spec = P.PROPERTIES[THRESHOLD_PROPERTIES[which]]
    address = progress.names[socket.address] = address_label(hub, socket.address)
    try:
        await reader.write(socket.address, spec, value)
    except OSError as err:  # a lost link (ConnectionError)
        raise _error(
            SEND_FAILED[which],
            address=address,
            applied=applied_message(hass, progress.text()),
        ) from err
    raw = reader.cached(socket.address, spec)
    try:
        held = None if raw is None else spec.codec.decode(raw)
    except ValueError:
        held = None
    if held != value:
        raise _error(
            NOT_APPLIED[which],
            address=address,
            applied=applied_message(hass, progress.text()),
        )
    progress.wrote(socket.address, which)
