"""The link-free half of `tools/mesh_poc.py`: argument parsing, property codecs, Config messages, export round trip.

Everything here is a pure function over `jhmesh` (no Bluetooth, no asyncio), so `tests/test_cli.py` covers it
without a proxy link. `mesh_poc.py` only adds the transport around these helpers.

Conventions shared with the rest of the CLI: addresses and ids are hex without a prefix (`0149`, `5003`; a `0x`
prefix is accepted), property names are the snake_case identifiers of `jhmesh.properties` (`key_mode`), model
ids are CDB strings (`1000`, `05271013`).
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jhmesh import config_messages as C
from jhmesh import messages as M
from jhmesh import properties as P
from jhmesh import vendor_models as V
from jhmesh.cdb import CDB, canonical_uuid
from jhmesh.devices import Metadata
from jhmesh.export import ExportError, ProjectFile
from jhmesh.export import write_private as write_private_bytes
from jhmesh.pdu import ALL_NODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from jhmesh.audit import Finding, ModelAudit, NodeAudit
    from jhmesh.client import AccessMessage
    from jhmesh.provisioning import ProvisioningResult, UnprovisionedDevice

VendorServer = Literal["admin", "manufacturer", "user"]
VENDOR_SERVERS: tuple[VendorServer, ...] = ("admin", "manufacturer", "user")
# the SIG Generic Property servers (`M.generic_property_get/set` kinds) and the Sensor server
SIG_SERVERS = ("sig_admin", "sig_manufacturer", "sig_user", "sensor")
SERVERS = (*VENDOR_SERVERS, *SIG_SERVERS)
SIG_STATUS_OPCODES = {  # Generic Property Status per SIG server kind
    "admin": M.GEN_ADMIN_PROP_STATUS,
    "manufacturer": M.GEN_MANU_PROP_STATUS,
    "user": M.GEN_USER_PROP_STATUS,
}
HEX_PREFIX = "hex:"  # `prop set ... hex:0102` bypasses the codec


# ----------------------------------------------------------------------------- argument parsing


def parse_address(text: str) -> int:
    """Hex unicast address ("0D01" or "0x0D01") for `--source` and node / element arguments."""
    try:
        addr = int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a hex address") from None
    if not 0x0001 <= addr <= 0x7FFF:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a unicast address (0001-7FFF)"
        )
    return addr


def parse_hex(text: str) -> int:
    """A hex number with or without `0x` (property ids, product ids, group addresses)."""
    try:
        return int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a hex number") from None


def _scene_number(text: str, low: int) -> int:
    for base in (10, 0):
        try:
            value = int(text, base)
        except ValueError:
            continue
        if low <= value <= 0xFFFF:
            return value
        break
    raise argparse.ArgumentTypeError(f"{text!r} is not a scene number ({low}..65535)")


def parse_scene_number(text: str) -> int:
    """A scene number 1..65535, decimal (`07` is 7) or `0x`-hex; an argparse error otherwise (scene 0 is prohibited)."""
    return _scene_number(text, 1)


def parse_scene_or_list(text: str) -> int:
    """`parse_scene_number`, or 0 — the Scene Action Setup Get for the list of scenes."""
    return _scene_number(text, 0)


def load_cdb(path: str | Path) -> CDB:
    """Load an export for a command, or exit with one line naming the file (missing, unreadable, not an export)."""
    try:
        return CDB.load(Path(path))
    except (OSError, ValueError) as err:
        sys.exit(f"cannot read {path}: {err}")


def roundtrip_export_or_exit(path: Path) -> tuple[str, list[str]]:
    """`roundtrip_export`, or exit with one line naming the file when it cannot be read as an export."""
    try:
        return roundtrip_export(path)
    except (OSError, ValueError, ExportError) as err:
        sys.exit(f"cannot read {path}: {err}")


def resolve_or_exit(cdb: CDB, text: str) -> int:
    """Resolve a target / group (a group name or a hex address 0001..FFFF), or exit saying which it was not."""
    try:
        address = cdb.resolve(text)
    except ValueError:
        address = 0
    if not 1 <= address <= 0xFFFF:  # 0000 is the unassigned address, nothing answers it
        sys.exit(f"{text!r} is neither a group name nor a hex address")
    return address


def parse_uint16(text: str) -> int:
    """A 0..65535 value (lightness, colour temperature in K), decimal or `0x`-hex; an argparse error otherwise.

    The message builders pack these with `struct`, which would turn `70000` into a traceback instead of an error.
    """
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0 <= value <= 0xFFFF:
        raise argparse.ArgumentTypeError(f"{value} is outside 0..65535")
    return value


def parse_transition(text: str) -> int:
    """`--transition SECONDS` as the transition-time byte (`messages.encode_transition`: the nearest, finest step).

    An argparse error for anything but 0..37200 s (62 steps of 10 minutes), never the prohibited 63 steps.
    """
    try:
        return M.encode_transition(float(text))
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"{text!r}: {err}") from None


def parse_model(text: str) -> int | str:
    """A model id as the CDB writes it (`1000`, `05271013`) or with `0x`; validated by `config_messages.model_id`."""
    try:
        return C.model_id_str(C.model_id(text.removeprefix("0x").removeprefix("0X")))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a model id") from None


def resolve_property(text: str) -> tuple[int, P.PropertySpec | None]:
    """`key_mode` / `0x5003` / `5003` -> (id, spec); the spec is None for an id the catalogue does not know.

    Names win over numbers; a numeric id is looked up in the vendor catalogue first, then in the SIG one.
    """
    try:
        spec = P.by_name(text)
    except KeyError:
        pass
    else:
        return spec.id, spec
    try:
        pid = int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is neither a property name nor a hex id"
        ) from None
    if not 0 <= pid <= 0xFFFF:
        raise argparse.ArgumentTypeError(f"property id {text!r} is not 16-bit")
    return pid, P.PROPERTIES.get(pid) or P.SIG_PROPERTIES.get(pid)


def parse_product(text: str) -> int:
    """A JUNG product id (`01`, `0x10`) for `prop list`."""
    pid = parse_hex(text)
    if pid not in P.ALL_PRODUCTS:
        known = ", ".join(f"{p:02X}" for p in sorted(P.ALL_PRODUCTS))
        raise argparse.ArgumentTypeError(f"unknown product {text!r}; known: {known}")
    return pid


# ----------------------------------------------------------------------------- property values


def parse_value(spec: P.PropertySpec | None, text: str) -> bytes:
    """Encode the command-line `text` for the property with its codec (`hex:…` or an unknown id: raw bytes).

    bool: on/off/true/false/1/0 · int: `12`, `0x0C` · float/duration/percent: `1.5` · enum: the option name
    (or its number) · flags: `a,b` (`none` for no flag) · version: `2.2.0.2` · string: as is · raw / struct /
    rgb_mode / list: `hex:` bytes (see `properties.py` for the layouts).
    """
    if text.startswith(HEX_PREFIX) or spec is None:
        return _hex_bytes(text.removeprefix(HEX_PREFIX))
    kind = spec.codec.kind
    value: Any
    if kind == "bool":
        lowered = text.lower()
        if lowered not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
            raise ValueError(f"{text!r} is not a boolean (on/off)")
        value = lowered in ("on", "true", "1", "yes")
    elif kind == "int":
        value = int(text, 0)
    elif kind in ("float", "duration", "percent"):
        value = float(text)
    elif kind == "enum":
        value = int(text, 0) if _is_number(text) else text
    elif kind == "flags":
        value = (
            int(text, 0)
            if _is_number(text)
            else {name: True for name in text.split(",") if name and name != "none"}
        )
    elif kind in ("string", "version"):
        value = text
    elif kind == "raw":
        value = _hex_bytes(text)
    else:  # struct, rgb_mode, list: no text form, the layout is in properties.py
        raise ValueError(
            f"{spec.name} ({kind}) has no text form; give the wire bytes as {HEX_PREFIX}<hex>"
        )
    return spec.codec.encode(value)


def _is_number(text: str) -> bool:
    try:
        int(text, 0)
    except ValueError:
        return False
    return True


def _hex_bytes(text: str) -> bytes:
    try:
        return bytes.fromhex(text.removeprefix("0x"))
    except ValueError:
        raise ValueError(f"{text!r} is not hex") from None


# ----------------------------------------------------------------------------- property requests


@dataclass(frozen=True)
class PropertyRequest:
    """One Get / Set to send with `ProxyClient.request()` and how to recognise its answer."""

    pid: int
    server: str
    pdu: bytes
    expect_opcode: int
    expect_cid: int | None

    @property
    def describe(self) -> str:
        """The request as `messages.describe` prints it."""
        return M.describe(self.pdu)


def property_server(pid: int, spec: P.PropertySpec | None, server: str | None) -> str:
    """The server to address: the override, else the catalogue's hosting server; an unknown id needs the override."""
    if server is not None:
        if server not in SERVERS:
            raise ValueError(f"server must be one of {', '.join(SERVERS)}")
        return server
    if spec is None:
        raise ValueError(
            f"property 0x{pid:04X} is not in the catalogue: say which server hosts it with --server"
        )
    return spec.server


def property_get(
    pid: int, spec: P.PropertySpec | None, server: str | None = None
) -> PropertyRequest:
    """Build the Get for a property: LBC vendor Get, SIG Generic Property Get, or Sensor Get."""
    host = property_server(pid, spec, server)
    if host in VENDOR_SERVERS:
        return PropertyRequest(
            pid,
            host,
            M.vendor_property_get(host, pid),
            M.VENDOR_PROPERTY_STATUS_OPCODES[host],
            M.JUNG_CID,
        )
    if host == "sensor":
        return PropertyRequest(pid, host, M.sensor_get(pid), M.SENSOR_STATUS, None)
    kind = host.removeprefix("sig_")
    return PropertyRequest(
        pid, host, M.generic_property_get(kind, pid), SIG_STATUS_OPCODES[kind], None
    )


def property_set(
    pid: int,
    spec: P.PropertySpec | None,
    text: str,
    *,
    server: str | None = None,
    ack: bool = True,
    user_access: int | None = None,
) -> PropertyRequest:
    """Build the Set for a property: the value from `parse_value`, the access byte from the catalogue unless given."""
    host = property_server(pid, spec, server)
    value = parse_value(spec, text)
    access = (spec.set_access if spec else 3) if user_access is None else user_access
    if host in VENDOR_SERVERS:
        return PropertyRequest(
            pid,
            host,
            M.vendor_property_set(host, pid, value, ack=ack, user_access=access),
            M.VENDOR_PROPERTY_STATUS_OPCODES[host],
            M.JUNG_CID,
        )
    if host == "sensor":
        raise ValueError("sensor properties are read-only")
    kind = host.removeprefix("sig_")
    return PropertyRequest(
        pid,
        host,
        M.generic_property_set(kind, pid, value, user_access=access, ack=ack),
        SIG_STATUS_OPCODES[kind],
        None,
    )


def property_status_text(
    reply: AccessMessage, pid: int, server: str | None = None
) -> str:
    """`key_mode=gateway (access=3 raw=06)` for the Status answering a property request, or why it is not one.

    Every property Status is `[pid u16][userAccess u8][value…]`; a Sensor Status carries marshalled values instead.
    `server` (the request's) decides whether the id is a SIG or a JUNG property — the two spaces overlap
    (0x000D is `energy_since_turn_on` on a SIG server, a schema version on an LBC one); without it the JUNG
    catalogue wins unless only the SIG one knows the id.
    """
    if reply.opcode == M.SENSOR_STATUS and reply.company_id is None:
        for prop, raw in M.sensor_values(reply.params):
            if prop == pid:
                return f"{P.describe_status(pid, raw, sig=True)} (raw={raw.hex()})"
        return f"sensor status without property 0x{pid:04X}: {M.describe(reply.access_pdu)}"
    p = reply.params
    if len(p) < 2:
        return f"malformed status: {M.describe(reply.access_pdu)}"
    got = int.from_bytes(p[:2], "little")
    if got != pid:
        return f"status of another property: {M.describe(reply.access_pdu)}"
    access = p[2] if len(p) > 2 else None
    value = p[3:]
    if server is not None:
        sig = server in SIG_SERVERS
    else:
        sig = pid not in P.PROPERTIES and pid in P.SIG_PROPERTIES
    text = P.describe_status(pid, value, sig=sig)
    return (
        f"{text} (access={'?' if access is None else access} raw={value.hex() or '-'})"
    )


def property_rows(product_id: int, *, include_firmware_only: bool = False) -> list[str]:
    """One line per property of a product for `prop list`: id, name, server, access, element, codec, unit, range."""
    rows = []
    for spec in P.for_product(product_id, include_firmware_only=include_firmware_only):
        limits = (
            f" {spec.min:g}..{spec.max:g}"
            if spec.min is not None and spec.max is not None
            else ""
        )
        version = (
            f" fw>={'.'.join(map(str, spec.firmware_min))}" if spec.firmware_min else ""
        )
        source = "" if spec.source == "app" else " (firmware only)"
        rows.append(
            f"0x{spec.id:04X} {spec.name:34s} {spec.server:16s} {spec.access:2s} {spec.element:8s}"
            f" {spec.codec.kind}{f' {spec.unit}' if spec.unit else ''}{limits}{version}{source}"
        )
    return rows


# ----------------------------------------------------------------------------- Config messages


@dataclass(frozen=True)
class ConfigRequest:
    """A Config message for `ProxyClient.request_config()` plus the status opcode that answers it."""

    node: int
    pdu: bytes
    expect_opcode: int

    @property
    def describe(self) -> str:
        """The request as `messages.describe` prints it."""
        return M.describe(self.pdu)


def config_composition(node: int, page: int = 0) -> ConfigRequest:
    """Composition Data Get."""
    return ConfigRequest(
        node, C.composition_data_get(page), C.CONFIG_COMPOSITION_DATA_STATUS
    )


def config_publication(
    node: int, element: int, model: int | str, group: int | None
) -> ConfigRequest:
    """Publication Set to `group` (0 disables publishing, as the app does), or Publication Get without a group."""
    if group is None:
        pdu = C.model_publication_get(element, model)
    else:
        pdu = C.model_publication_set(element, group, model)
    return ConfigRequest(node, pdu, C.CONFIG_MODEL_PUBLICATION_STATUS)


def config_subscription(
    node: int, element: int, model: int | str, group: int, *, add: bool
) -> ConfigRequest:
    """Subscription Add / Delete."""
    build = C.model_subscription_add if add else C.model_subscription_delete
    return ConfigRequest(
        node, build(element, group, model), C.CONFIG_MODEL_SUBSCRIPTION_STATUS
    )


def config_subscriptions(node: int, element: int, model: int | str) -> ConfigRequest:
    """SIG / Vendor Model Subscription Get (the list opcode depends on the model kind)."""
    expect = (
        C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST
        if C.is_vendor_model(model)
        else C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST
    )
    return ConfigRequest(node, C.model_subscription_get(element, model), expect)


def config_bind(
    node: int, element: int, model: int | str, *, bind: bool = True
) -> ConfigRequest:
    """Model App Bind / Unbind of AppKey 0."""
    build = C.model_app_bind if bind else C.model_app_unbind
    return ConfigRequest(node, build(element, model), C.CONFIG_MODEL_APP_STATUS)


def _setting_text(value: Any) -> str:
    """A node-wide state as the audit reports it: `?` unanswered, `3x100ms` a transmit, `on` / `off` the beacon."""
    if value is None:
        return "?"
    if isinstance(value, dict):
        return f"{value['count']}x{value['interval']}ms"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _keys_text(value: list[int] | None) -> str:
    """The key indexes a node holds as the audit reports them: `?` unanswered, `-` none, `0,1` otherwise."""
    if value is None:
        return "?"
    return ",".join(map(str, value)) or "-"


def audit_row_text(row: ModelAudit) -> str:
    """`0148 1203     publish C061 → C061  subscribe C00F,C061,FEF5 → C061  appkeys 0 → 0`.

    Each node value is `?` when unanswered and the status name in brackets when refused; `-` is nothing.
    """

    def addrs(a: tuple[int, ...]) -> str:
        return ",".join(f"{x:04X}" for x in a) or "-"

    def node(kind: str, value: Any, text: Callable[[Any], str]) -> str:
        if kind in row.refused:
            return f"({row.refused[kind]})"
        return "?" if value is None else text(value)

    publish = node("publication", row.node_publish, lambda a: f"{a:04X}")
    subscribe = node("subscriptions", row.node_subscribe, addrs)
    keys = node("app_keys", row.node_app_keys, lambda k: ",".join(map(str, k)) or "-")
    export_keys = ",".join(map(str, row.export_app_keys)) or "-"
    return (
        f"{row.element:04X} {row.model:8s} publish {row.export_publish:04X} → {publish}"
        f"  subscribe {addrs(row.export_subscribe)} → {subscribe}  appkeys {export_keys} → {keys}"
    )


def finding_text(finding: Finding) -> str:
    """`0148 1203 scene_subscriptions_missing: expected C00F,FEF5` / `gatt_proxy setting_differs: expected 2, node 1`."""

    def value(v: Any) -> str:
        return ",".join(map(str, v)) if isinstance(v, list) else _setting_text(v)

    where = [f"{finding.element:04X}"] if finding.element is not None else []
    where += [part for part in (finding.model, finding.setting) if part]
    what = [
        f"{label} {value(v)}"
        for label, v in (("expected", finding.expected), ("node", finding.actual))
        if v is not None
    ]
    return " ".join([*where, finding.kind]) + (f": {', '.join(what)}" if what else "")


def audit_text(cdb: CDB, audit: NodeAudit) -> str:
    """One node's audit for the terminal: its node-wide states and keys, the models worth a row, every finding, a verdict.

    A model gets a row when it publishes or subscribes, in the export or on the node, or has a finding — the
    AppKey binding alone (AppKey 0 on nearly every model) would bury them. Notes (`·`) follow the findings (`!`)
    and do not count against the verdict.
    """
    lines = [cdb.label(audit.node)]
    if not audit.answered:
        return "\n".join([*lines, "  no answer (off, asleep or out of reach)"])
    lines.append(
        "  "
        + "  ".join(
            f"{name} {_setting_text(s['node'])}"
            + (
                ""
                if s["export"] in (None, s["node"])
                else f" (export {_setting_text(s['export'])})"
            )
            for name, s in audit.settings.items()
        )
    )
    if audit.keys:
        lines.append(
            "  "
            + "  ".join(
                f"{name} {_keys_text(k['node'])}"
                + (
                    ""
                    if k["export"] in (None, k["node"])
                    else f" (export {_keys_text(k['export'])})"
                )
                for name, k in audit.keys.items()
            )
        )
    flagged = {(f.element, f.model) for f in audit.findings}
    lines += [
        "  " + audit_row_text(m)
        for m in audit.models
        if m.export_publish
        or m.export_subscribe
        or m.node_publish
        or m.node_subscribe
        or (m.element, m.model) in flagged
    ]
    lines += ["  ! " + finding_text(f) for f in audit.findings]
    lines += ["  · " + finding_text(f) for f in audit.notes]
    lines.append(
        f"  {len(audit.models)} models checked; "
        + (
            f"{len(audit.findings)} findings"
            if audit.findings
            else "everything matches the export"
        )
        + (
            f" ({len(audit.notes)} harmless note{'s' if len(audit.notes) > 1 else ''})"
            if audit.notes
            else ""
        )
    )
    return "\n".join(lines)


def config_status_text(reply: AccessMessage) -> str:
    """The decoded Config status, one line; Composition Data as one line per element."""
    decoded = C.decode_config(reply.opcode, reply.params)
    if isinstance(decoded, C.CompositionData):
        head = (
            f"cid={decoded.cid:04X} pid={decoded.pid:04X} vid={decoded.vid:04X} crpl={decoded.crpl}"
            f" features: relay={int(decoded.relay)} proxy={int(decoded.proxy)}"
            f" friend={int(decoded.friend)} lpn={int(decoded.low_power)}"
        )
        lines = [head]
        for i, element in enumerate(decoded.elements):
            lines.append(
                f"  element {i} loc={element.location:04X} models={','.join(element.model_ids)}"
            )
        return "\n".join(lines)
    return M.describe(reply.access_pdu)


# ----------------------------------------------------------------------------- property lists


def property_list_requests() -> list[PropertyRequest]:
    """The six "Properties Get" of an element: LBC admin / manufacturer / user, then the SIG servers.

    `pid` is 0 (a list has none); the reply's `params` are the property ids (`messages.property_ids`).
    """
    out = [
        PropertyRequest(
            0,
            kind,
            M.vendor_properties_get(kind),
            M.VENDOR_PROPERTY_LIST_OPCODES[kind][1],
            M.JUNG_CID,
        )
        for kind in VENDOR_SERVERS
    ]
    out += [
        PropertyRequest(
            0,
            f"sig_{kind}",
            M.generic_properties_get(kind),
            M.SIG_PROPERTY_LIST_OPCODES[kind][1],
            None,
        )
        for kind in ("admin", "manufacturer", "user")
    ]
    return out


def property_list_text(server: str, reply: AccessMessage) -> str:
    """`21 ids: 0001 device_lock, 5001 button_layout, …` — one line per server, names from the matching catalogue."""
    ids = M.property_ids(reply.params)
    sig = server.startswith("sig_")
    catalogue = P.SIG_PROPERTIES if sig else P.PROPERTIES
    items = [
        f"{pid:04X}" + (f" {catalogue[pid].name}" if pid in catalogue else " ?")
        for pid in ids
    ]
    return f"{len(ids)} ids: {', '.join(items)}" if ids else "empty"


# ----------------------------------------------------------------------------- scene actions / heartbeat hops


def scene_action_text(cdb: CDB, status: V.SceneActionStatus) -> str:
    """`scene 8 "Kitchen on": switch on` — the app's scene name (share export `meta`, else the CDB's) when known."""
    if status.scenes is not None:
        return status.describe()
    name = Metadata.from_export(cdb.export_meta).scenes.get(
        status.scene
    ) or cdb.scene_names.get(status.scene)
    what = status.action.describe() if status.action else "no action"
    return f"scene {status.scene}" + (f' "{name}"' if name else "") + f": {what}"


@dataclass(frozen=True)
class HopProbe:
    """The messages of a Heartbeat Subscription hop count between two nodes and the wait between them.

    The source publishes `beats` Heartbeats to all-nodes every `period` seconds with InitTTL 127; the subscriber
    counts them over a window that covers them; afterwards both are put back (the source to the publication it
    had, the subscriber's subscription off).
    """

    subscriber: int
    source: int
    beats: int
    period: int

    @property
    def count_log(self) -> int:
        """CountLog for `beats` (2^(n-1) beats)."""
        return max(1, (self.beats - 1).bit_length() + 1)

    @property
    def period_log(self) -> int:
        """PeriodLog of the source's publication."""
        return C.heartbeat_period_log(self.period)

    @property
    def wait_seconds(self) -> float:
        """How long the beats take, plus a margin for the last one to arrive."""
        beats = 1 << (self.count_log - 1)
        return float(C.heartbeat_period_seconds(self.period_log) * beats + 2)

    @property
    def subscribe(self) -> ConfigRequest:
        """Heartbeat Subscription Set on the subscriber (window ≥ the beats)."""
        return ConfigRequest(
            self.subscriber,
            C.heartbeat_subscription_set(
                self.source, ALL_NODES, C.heartbeat_period_log(self.wait_seconds + 2)
            ),
            C.CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS,
        )

    @property
    def publish(self) -> ConfigRequest:
        """Heartbeat Publication Set on the source: `beats` beats to all-nodes, InitTTL 127."""
        return ConfigRequest(
            self.source,
            C.heartbeat_publication_set(
                ALL_NODES,
                self.period_log,
                count_log=self.count_log,
                ttl=C.HEARTBEAT_TTL_MAX,
            ),
            C.CONFIG_HEARTBEAT_PUBLICATION_STATUS,
        )

    @property
    def read_publication(self) -> ConfigRequest:
        """Heartbeat Publication Get on the source, to know what to put back."""
        return ConfigRequest(
            self.source,
            C.heartbeat_publication_get(),
            C.CONFIG_HEARTBEAT_PUBLICATION_STATUS,
        )

    @property
    def read_subscription(self) -> ConfigRequest:
        """Heartbeat Subscription Get on the subscriber: the count and the hop range."""
        return ConfigRequest(
            self.subscriber,
            C.heartbeat_subscription_get(),
            C.CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS,
        )

    def restore_publication(
        self, before: C.HeartbeatPublicationStatus
    ) -> ConfigRequest:
        """Heartbeat Publication Set on the source with the fields it reported before the probe.

        A finite CountLog reads as the beats *remaining*, so the restored publication is the tail of the old
        one; an indefinite one (0xFF, what the HA integration sets) is put back as is.
        """
        return ConfigRequest(
            self.source,
            C.heartbeat_publication_set(
                before.destination,
                before.period_log,
                count_log=before.count_log,
                ttl=before.ttl,
                features=before.features,
                net_key_index=before.net_key_index,
            ),
            C.CONFIG_HEARTBEAT_PUBLICATION_STATUS,
        )

    @property
    def unsubscribe(self) -> ConfigRequest:
        """Heartbeat Subscription Set off on the subscriber."""
        return ConfigRequest(
            self.subscriber,
            C.heartbeat_subscription_off(),
            C.CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS,
        )


def hops_text(cdb: CDB, probe: HopProbe, status: C.HeartbeatSubscriptionStatus) -> str:
    """`01A4 (…) → 0148 (…): 4 beats sent, 4..7 counted, 1..1 hops`."""
    lo, hi = C.heartbeat_count_range(status.count_log)
    counted = f"{lo}" if lo == hi else f"{lo}..{hi}"
    hops = (
        "no beat arrived"
        if status.count_log == 0
        else f"{status.min_hops}..{status.max_hops} hops"
    )
    return (
        f"{cdb.label(probe.source)} → {cdb.label(probe.subscriber)}: {probe.beats} beats sent,"
        f" {counted} counted, {hops}"
    )


HopCell = (
    tuple[int, int | None, int | None] | None
)  # (beats counted, min hops, max hops); None = no answer


def hop_cell(status: C.HeartbeatSubscriptionStatus) -> tuple[HopCell, str]:
    """A matrix cell from a Heartbeat Subscription Status, with its short text (`2`, `1..3`, `-` for no beat)."""
    if status.count_log == 0:
        return (0, None, None), "-"
    lo, _hi = C.heartbeat_count_range(status.count_log)
    hops = (
        f"{status.min_hops}"
        if status.min_hops == status.max_hops
        else f"{status.min_hops}..{status.max_hops}"
    )
    return (lo, status.min_hops, status.max_hops), hops


def hop_matrix_json(
    cdb: CDB,
    nodes: list[int],
    results: dict[tuple[int, int], HopCell],
    beats: int,
    period: int,
) -> str:
    """The matrix as JSON: the nodes with labels, the probe parameters and one record per (origin, counter)."""
    return (
        json.dumps(
            {
                "nodes": [f"{n:04X}" for n in nodes],
                "labels": {f"{n:04X}": cdb.label(n) for n in nodes},
                "beats": beats,
                "period": period,
                "pairs": [
                    {
                        "origin": f"{o:04X}",
                        "counter": f"{c:04X}",
                        "counted": None if r is None else r[0],
                        "min_hops": None if r is None else r[1],
                        "max_hops": None if r is None else r[2],
                    }
                    for (o, c), r in sorted(results.items())
                ],
            },
            indent=1,
        )
        + "\n"
    )


def hop_matrix_text(
    cdb: CDB, nodes: list[int], results: dict[tuple[int, int], HopCell]
) -> str:
    """The hop matrix as text: one row per origin (the node that beat), one column per counting node, the cell
    the minimum hops the counter saw (`-` = no beat arrived, `?` = the counter did not answer, `·` = itself).

    Followed by a legend line per node (address → label) so the table itself stays narrow.
    """
    head = "      " + " ".join(f"{n:04X}" for n in nodes)
    rows = [head]
    for origin in nodes:
        cells = []
        for counter in nodes:
            if counter == origin:
                cells.append("   ·")
                continue
            cell = results.get((origin, counter))
            if cell is None:
                cells.append("   ?")
            elif cell[1] is None:
                cells.append("   -")
            else:
                cells.append(f"{cell[1]:4d}")
        rows.append(f"{origin:04X}  " + " ".join(cells))
    rows.append("")
    rows.extend(f"{n:04X}  {cdb.label(n)}" for n in nodes)
    return "\n".join(rows)


# ----------------------------------------------------------------------------- listen


def format_message(m: AccessMessage, when: datetime, label: str | None = None) -> str:
    """One capture line: wall-clock time with milliseconds, then the message (with decoded property values)."""
    stamp = when.strftime("%H:%M:%S.") + f"{when.microsecond // 1000:03d}"
    return f"{stamp} {m}" + (f"  # {label}" if label else "")


def message_matches(m: AccessMessage, src: int | None, dst: int | None) -> bool:
    """The `listen --src/--dst` filter: both None = everything."""
    return (src is None or m.src == src) and (dst is None or m.dst == dst)


# ----------------------------------------------------------------------------- provisioning


def parse_device_uuid(text: str) -> str:
    """A Device UUID as `provision --scan` prints it (dashed or 32 hex digits, any case), in the canonical form."""
    raw = text.replace("-", "")
    if len(raw) != 32 or not re.fullmatch(r"[0-9A-Fa-f]{32}", raw):
        raise argparse.ArgumentTypeError(f"{text!r} is not a device UUID")
    return canonical_uuid(raw)


def parse_iv_index(text: str) -> int:
    """An IV index: a 32-bit value, decimal or `0x`-hex."""
    try:
        value = int(text, 0)
    except ValueError:
        value = -1
    if not 0 <= value <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError(f"{text!r} is not an IV index (0..4294967295)")
    return value


def unprovisioned_rows(devices: list[UnprovisionedDevice]) -> list[str]:
    """One line per device of `provision --scan`: signal, address, Device UUID, OOB information, product, name."""
    if not devices:
        return ["no device advertises the Mesh Provisioning Service (0x1827)"]
    return [
        f"{d.rssi:5d} dBm  {d.address}  {d.uuid}  oob {d.oob_info:04X}"
        + (f" ({', '.join(d.oob_names)})" if d.oob_info else "")
        + (f"  product {d.product_id:04X}" if d.product_id is not None else "")
        + (f"  {d.name}" if d.name else "")
        for d in devices
    ]


def provision_address_problem(
    cdb: CDB, unicast: int, elements: int, reserved: set[int]
) -> str | None:
    """Why `elements` addresses from `unicast` cannot go to a new node; None when they can.

    Each must be a unicast address that no node uses, that is not excluded (replay lists still know it), not
    inside a provisioner's allocated range (the phones provision there: `CDB.unicast_is_free`) and not one of
    the `reserved` client addresses (Home Assistant's, the CLI's own).
    """
    last = unicast + elements - 1
    if last > 0x7FFF:
        return f"{elements} element(s) from {unicast:04X} run past the unicast range (7FFF)"
    used = cdb.used_unicasts()
    bad = [
        a
        for a in range(unicast, last + 1)
        if a in reserved or not cdb.unicast_is_free(a, used)
    ]
    if bad:
        return (
            f"{', '.join(f'{a:04X}' for a in bad)} cannot go to the new node (a node's, excluded, inside a"
            " provisioner's range or a client's own address): pick another --unicast"
        )
    return None


def device_key_record(uuid: str, result: ProvisioningResult) -> str:
    """The JSON `provision` writes to its device-key file: what the export needs to take the node in."""
    return (
        json.dumps(
            {
                "uuid": uuid,
                "unicast": f"{result.unicast:04X}",
                "elements": result.elements,
                "deviceKey": result.device_key.hex().upper(),
            },
            indent=2,
        )
        + "\n"
    )


def provisioned_text(result: ProvisioningResult, key_file: Path) -> str:
    """What `provision` prints on success: the addresses, where the device key is, and what is still to do.

    The key itself is never printed (a terminal scrollback, a `tee` log or a pasted transcript would keep it): it
    is in `key_file`, written owner-only.
    """
    return (
        f"provisioned: unicast {result.unicast:04X}..{result.addresses[-1]:04X}"
        f" ({result.elements} element(s))\n"
        f"device key: written to {key_file} (owner-only)\n"
        "not written to the export: add the node (UUID, unicast, device key, elements) before configuring it"
        " (jhmesh.commission plans the app's configuration); the device key exists nowhere else but that file"
    )


# ----------------------------------------------------------------------------- export round trip


def roundtrip_export(path: Path) -> tuple[str, list[str]]:
    """Load an export with `ProjectFile` and render it again unchanged; return the text and its diff to the file.

    A faithful writer gives an empty diff for an iOS `MeshNetwork.json`; for an Android `JungHome.json` only
    the Base64 `network` line may differ (Gson escaping / key order inside the payload, tracker §8).
    """
    original = Path(path).read_text()
    rendered = ProjectFile.load(Path(path)).render()
    diff = list(
        difflib.unified_diff(
            original.splitlines(),
            rendered.splitlines(),
            fromfile=str(path),
            tofile=f"{path} (rewritten)",
            lineterm="",
            n=1,
        )
    )
    return rendered, diff


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` atomically, readable by the owner only (0600): an export holds every mesh key."""
    write_private_bytes(Path(path), text.encode())


# A 128-bit key in hex (NetKey, AppKey, DevKey); it may also catch a dash-less node UUID, which is fine to hide.
_KEY_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")
# An app export's `network` value: the Base64 of the whole CDB, every key included.
_NETWORK_VALUE = re.compile(r'("network"\s*:\s*")([^"]*)(")')


def _redact(line: str) -> str:
    """Hide the key material a diff line of an export can carry: the `network` payload and every hex key."""
    line = _NETWORK_VALUE.sub(
        lambda m: f"{m[1]}<Base64 payload, {len(m[2])} chars>{m[3]}", line
    )
    return _KEY_HEX.sub("<key redacted>", line)


def summarize_diff(diff: list[str], limit: int = 40) -> str:
    """The diff for the terminal: `identical` or the first `limit` lines (long lines cut short).

    Key material is never shown: the lines of an export's keys (changed, or context around a change) and its
    Base64 `network` payload are redacted before anything is printed.
    """
    if not diff:
        return "identical"
    shown = [_clip(_redact(line)) for line in diff[:limit]]
    if len(diff) > limit:
        shown.append(f"... {len(diff) - limit} more lines")
    return "\n".join(shown)


def _clip(line: str) -> str:
    return line if len(line) <= 160 else line[:157] + "..."
