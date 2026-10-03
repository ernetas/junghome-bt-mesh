"""Configuration Server messages (Mesh Profile 1.0.1 §4.3.2): builders, status decoders, `describe_config`.

Every message here travels DevKey-encrypted to a node's *primary* unicast (`ProxyClient.send_config` /
`request_config`). Covered is the subset the JUNG HOME app uses (docs/gap-analysis/network-features.md §13,
docs/android/network-logic.md §1.5 and §3.2): AppKey Add, Composition Data, Model App Bind/Unbind, Model
Publication Get/Set, Model Subscription Add/Delete/Delete All/Get, GATT Proxy, Default TTL, Relay, Network
Transmit, Beacon, Node Reset, and the SIG / Vendor Model App Get the read-only audit adds (`audit.py`) — plus the key management messages of a key refresh (NetKey / AppKey Add, Update
and Delete, Key Refresh Phase; the app's KeyRenewal sends NetKey Update and Key Refresh Phase Set, network-logic.md
§6), which `describe_config` shows without their key bytes. Virtual-address forms, heartbeat, friend and node
identity are out of scope (the app never sends them).

Model identifiers: a SIG model is its 16-bit id (0x1000); a vendor model is `company_id << 16 | model_id`
(0x05271013) — exactly what the CDB's model strings ("1000", "05271013") parse to, so builders accept either
form. On the wire a vendor model is company id LE followed by model id LE (§4.3.1.2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from .pdu import encode_opcode, is_unicast

# ----------------------------------------------------------------------------- opcodes (§4.3.4)

CONFIG_APPKEY_ADD, CONFIG_APPKEY_UPDATE = 0x00, 0x01
CONFIG_APPKEY_DELETE, CONFIG_APPKEY_STATUS = 0x8000, 0x8003
CONFIG_COMPOSITION_DATA_GET, CONFIG_COMPOSITION_DATA_STATUS = 0x8008, 0x02
CONFIG_BEACON_GET, CONFIG_BEACON_SET, CONFIG_BEACON_STATUS = 0x8009, 0x800A, 0x800B
CONFIG_DEFAULT_TTL_GET, CONFIG_DEFAULT_TTL_SET, CONFIG_DEFAULT_TTL_STATUS = (
    0x800C,
    0x800D,
    0x800E,
)
CONFIG_GATT_PROXY_GET, CONFIG_GATT_PROXY_SET, CONFIG_GATT_PROXY_STATUS = (
    0x8012,
    0x8013,
    0x8014,
)
(
    CONFIG_KEY_REFRESH_PHASE_GET,
    CONFIG_KEY_REFRESH_PHASE_SET,
    CONFIG_KEY_REFRESH_PHASE_STATUS,
) = 0x8015, 0x8016, 0x8017
CONFIG_MODEL_PUBLICATION_GET = 0x8018
CONFIG_MODEL_PUBLICATION_SET = 0x03
CONFIG_MODEL_PUBLICATION_STATUS = 0x8019
(
    CONFIG_MODEL_SUBSCRIPTION_ADD,
    CONFIG_MODEL_SUBSCRIPTION_DELETE,
    CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL,
    CONFIG_MODEL_SUBSCRIPTION_OVERWRITE,
    CONFIG_MODEL_SUBSCRIPTION_STATUS,
) = 0x801B, 0x801C, 0x801D, 0x801E, 0x801F
(
    CONFIG_NETWORK_TRANSMIT_GET,
    CONFIG_NETWORK_TRANSMIT_SET,
    CONFIG_NETWORK_TRANSMIT_STATUS,
) = (
    0x8023,
    0x8024,
    0x8025,
)
CONFIG_RELAY_GET, CONFIG_RELAY_SET, CONFIG_RELAY_STATUS = 0x8026, 0x8027, 0x8028
CONFIG_SIG_MODEL_SUBSCRIPTION_GET, CONFIG_SIG_MODEL_SUBSCRIPTION_LIST = 0x8029, 0x802A
CONFIG_VENDOR_MODEL_SUBSCRIPTION_GET, CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST = (
    0x802B,
    0x802C,
)
CONFIG_MODEL_APP_BIND, CONFIG_MODEL_APP_STATUS, CONFIG_MODEL_APP_UNBIND = (
    0x803D,
    0x803E,
    0x803F,
)
(
    CONFIG_NETKEY_ADD,
    CONFIG_NETKEY_DELETE,
    CONFIG_NETKEY_STATUS,
    CONFIG_NETKEY_UPDATE,
) = 0x8040, 0x8041, 0x8044, 0x8045
CONFIG_NODE_RESET, CONFIG_NODE_RESET_STATUS = 0x8049, 0x804A
(
    CONFIG_SIG_MODEL_APP_GET,
    CONFIG_SIG_MODEL_APP_LIST,
    CONFIG_VENDOR_MODEL_APP_GET,
    CONFIG_VENDOR_MODEL_APP_LIST,
) = 0x804B, 0x804C, 0x804D, 0x804E
(
    CONFIG_HEARTBEAT_PUBLICATION_GET,
    CONFIG_HEARTBEAT_PUBLICATION_SET,
    CONFIG_HEARTBEAT_PUBLICATION_STATUS,
) = 0x8038, 0x8039, 0x06
(
    CONFIG_HEARTBEAT_SUBSCRIPTION_GET,
    CONFIG_HEARTBEAT_SUBSCRIPTION_SET,
    CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS,
) = 0x803A, 0x803B, 0x803C
HEARTBEAT_INDEFINITE = 0xFF  # CountLog: publish until told otherwise
HEARTBEAT_PERIOD_OFF = 0x00  # PeriodLog 0 / destination 0x0000: publication disabled
HEARTBEAT_TTL_MAX = 0x7F  # InitTTL for a hop count: hops = InitTTL - TTL + 1, so start as high as the field allows

# Messages whose parameters carry a 16-byte key: `describe_config` never prints their bytes, not even malformed.
KEY_CARRYING_OPCODES = frozenset(
    {CONFIG_APPKEY_ADD, CONFIG_APPKEY_UPDATE, CONFIG_NETKEY_ADD, CONFIG_NETKEY_UPDATE}
)

CONFIG_NAMES = {
    CONFIG_APPKEY_ADD: "AppKey Add",
    CONFIG_APPKEY_UPDATE: "AppKey Update",
    CONFIG_APPKEY_DELETE: "AppKey Delete",
    CONFIG_APPKEY_STATUS: "AppKey Status",
    CONFIG_COMPOSITION_DATA_GET: "Composition Data Get",
    CONFIG_COMPOSITION_DATA_STATUS: "Composition Data Status",
    CONFIG_BEACON_GET: "Beacon Get",
    CONFIG_BEACON_SET: "Beacon Set",
    CONFIG_BEACON_STATUS: "Beacon Status",
    CONFIG_DEFAULT_TTL_GET: "Default TTL Get",
    CONFIG_DEFAULT_TTL_SET: "Default TTL Set",
    CONFIG_DEFAULT_TTL_STATUS: "Default TTL Status",
    CONFIG_GATT_PROXY_GET: "GATT Proxy Get",
    CONFIG_GATT_PROXY_SET: "GATT Proxy Set",
    CONFIG_GATT_PROXY_STATUS: "GATT Proxy Status",
    CONFIG_KEY_REFRESH_PHASE_GET: "Key Refresh Phase Get",
    CONFIG_KEY_REFRESH_PHASE_SET: "Key Refresh Phase Set",
    CONFIG_KEY_REFRESH_PHASE_STATUS: "Key Refresh Phase Status",
    CONFIG_MODEL_PUBLICATION_GET: "Model Publication Get",
    CONFIG_MODEL_PUBLICATION_SET: "Model Publication Set",
    CONFIG_MODEL_PUBLICATION_STATUS: "Model Publication Status",
    CONFIG_MODEL_SUBSCRIPTION_ADD: "Model Subscription Add",
    CONFIG_MODEL_SUBSCRIPTION_DELETE: "Model Subscription Delete",
    CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL: "Model Subscription Delete All",
    CONFIG_MODEL_SUBSCRIPTION_OVERWRITE: "Model Subscription Overwrite",
    CONFIG_MODEL_SUBSCRIPTION_STATUS: "Model Subscription Status",
    CONFIG_NETWORK_TRANSMIT_GET: "Network Transmit Get",
    CONFIG_NETWORK_TRANSMIT_SET: "Network Transmit Set",
    CONFIG_NETWORK_TRANSMIT_STATUS: "Network Transmit Status",
    CONFIG_RELAY_GET: "Relay Get",
    CONFIG_RELAY_SET: "Relay Set",
    CONFIG_RELAY_STATUS: "Relay Status",
    CONFIG_SIG_MODEL_SUBSCRIPTION_GET: "SIG Model Subscription Get",
    CONFIG_SIG_MODEL_SUBSCRIPTION_LIST: "SIG Model Subscription List",
    CONFIG_VENDOR_MODEL_SUBSCRIPTION_GET: "Vendor Model Subscription Get",
    CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST: "Vendor Model Subscription List",
    CONFIG_MODEL_APP_BIND: "Model App Bind",
    CONFIG_MODEL_APP_STATUS: "Model App Status",
    CONFIG_MODEL_APP_UNBIND: "Model App Unbind",
    CONFIG_NETKEY_ADD: "NetKey Add",
    CONFIG_NETKEY_DELETE: "NetKey Delete",
    CONFIG_NETKEY_STATUS: "NetKey Status",
    CONFIG_NETKEY_UPDATE: "NetKey Update",
    CONFIG_NODE_RESET: "Node Reset",
    CONFIG_SIG_MODEL_APP_GET: "SIG Model App Get",
    CONFIG_SIG_MODEL_APP_LIST: "SIG Model App List",
    CONFIG_VENDOR_MODEL_APP_GET: "Vendor Model App Get",
    CONFIG_VENDOR_MODEL_APP_LIST: "Vendor Model App List",
    CONFIG_HEARTBEAT_PUBLICATION_GET: "Heartbeat Publication Get",
    CONFIG_HEARTBEAT_PUBLICATION_SET: "Heartbeat Publication Set",
    CONFIG_HEARTBEAT_PUBLICATION_STATUS: "Heartbeat Publication Status",
    CONFIG_HEARTBEAT_SUBSCRIPTION_GET: "Heartbeat Subscription Get",
    CONFIG_HEARTBEAT_SUBSCRIPTION_SET: "Heartbeat Subscription Set",
    CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS: "Heartbeat Subscription Status",
    CONFIG_NODE_RESET_STATUS: "Node Reset Status",
}

# Status codes (§4.3.2, Table 4.108)
STATUS_SUCCESS = 0x00
STATUS_NAMES = {
    0x00: "Success",
    0x01: "Invalid Address",
    0x02: "Invalid Model",
    0x03: "Invalid AppKey Index",
    0x04: "Invalid NetKey Index",
    0x05: "Insufficient Resources",
    0x06: "Key Index Already Stored",
    0x07: "Invalid Publish Parameters",
    0x08: "Not a Subscribe Model",
    0x09: "Storage Failure",
    0x0A: "Feature Not Supported",
    0x0B: "Cannot Update",
    0x0C: "Cannot Remove",
    0x0D: "Cannot Bind",
    0x0E: "Temporarily Unable to Change State",
    0x0F: "Cannot Set",
    0x10: "Unspecified Error",
    0x11: "Invalid Binding",
}

# GATT Proxy / Relay / Beacon state values (§4.2.11, §4.2.8, §4.2.10)
FEATURE_DISABLED, FEATURE_ENABLED, FEATURE_NOT_SUPPORTED = 0, 1, 2

# The app's publication parameters (network-logic.md §1.5): node default TTL, no period, no retransmission.
PUBLISH_TTL_DEFAULT = 0xFF

# ----------------------------------------------------------------------------- model identifiers


def model_id(model: int | str) -> int:
    """Normalise a model identifier: CDB string ("1000", "05271013") or int; vendor = company << 16 | model."""
    m = int(model, 16) if isinstance(model, str) else model
    if not 0 <= m <= 0xFFFFFFFF:
        raise ValueError(f"model id out of range: {model!r}")
    return m


def is_vendor_model(model: int | str) -> bool:
    """Tell whether `model` is a vendor model (has a company id)."""
    return model_id(model) > 0xFFFF


def model_id_str(model: int | str) -> str:
    """Return the CDB form of a model identifier: "1000" for SIG, "05271013" for vendor."""
    m = model_id(model)
    return f"{m:08X}" if m > 0xFFFF else f"{m:04X}"


def encode_model_id(model: int | str) -> bytes:
    """Encode a model identifier as on the wire: SIG id LE (2 bytes), or company id LE + model id LE (4 bytes)."""
    m = model_id(model)
    if m > 0xFFFF:
        return (m >> 16).to_bytes(2, "little") + (m & 0xFFFF).to_bytes(2, "little")
    return m.to_bytes(2, "little")


def decode_model_id(b: bytes) -> int:
    """Decode a 2-byte SIG or 4-byte vendor model identifier."""
    if len(b) == 2:
        return int.from_bytes(b, "little")
    if len(b) == 4:
        return (int.from_bytes(b[:2], "little") << 16) | int.from_bytes(b[2:], "little")
    raise ValueError(f"model identifier must be 2 or 4 bytes, got {len(b)}")


# ----------------------------------------------------------------------------- field helpers


def _u16(value: int, name: str) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} out of range: {value:#x}")
    return value.to_bytes(2, "little")


def _is_virtual(address: int) -> bool:
    return 0x8000 <= address <= 0xBFFF


def _element(address: int) -> bytes:
    if not is_unicast(address):
        raise ValueError(f"element address must be unicast, got {address:#06x}")
    return address.to_bytes(2, "little")


def _key_index(index: int, name: str) -> int:
    if not 0 <= index <= 0xFFF:
        raise ValueError(f"{name} must be a 12-bit value, got {index:#x}")
    return index


def _pack_key_indexes(first: int, second: int) -> bytes:
    """Pack two 12-bit key indexes into 3 octets (§4.3.1.1): first in the low 12 bits, second above, LE."""
    return (
        (_key_index(second, "second key index") << 12)
        | _key_index(first, "first key index")
    ).to_bytes(3, "little")


def _unpack_key_indexes(b: bytes) -> tuple[int, int]:
    v = int.from_bytes(b[:3], "little")
    return v & 0xFFF, v >> 12


def _unpack_key_index_list(b: bytes, what: str) -> list[int]:
    """Unpack a list of key indexes (§4.3.1.1): two per 3 octets, an odd last one alone in 2 octets."""
    out: list[int] = []
    i = 0
    while len(b) - i >= 3:
        out.extend(_unpack_key_indexes(b[i : i + 3]))
        i += 3
    if len(b) - i == 1:
        raise ValueError(f"{what}: odd key index bytes")
    if len(b) - i == 2:
        out.append(int.from_bytes(b[i:], "little") & 0xFFF)
    return out


def _transmit_byte(count: int, interval_steps: int, what: str) -> bytes:
    """Count (3 bits) in the low bits, interval steps (5 bits) above — Relay, Network Transmit, Publish Retransmit."""
    if not 0 <= count <= 7:
        raise ValueError(f"{what} count must be 0..7, got {count}")
    if not 0 <= interval_steps <= 31:
        raise ValueError(f"{what} interval steps must be 0..31, got {interval_steps}")
    return bytes([(interval_steps << 3) | count])


def _split_transmit_byte(b: int) -> tuple[int, int]:
    return b & 0x07, b >> 3


def _feature_state(enabled: bool | int, what: str) -> bytes:
    state = int(enabled)
    if state not in (FEATURE_DISABLED, FEATURE_ENABLED):
        raise ValueError(f"{what} must be 0 (disabled) or 1 (enabled), got {state}")
    return bytes([state])


# ----------------------------------------------------------------------------- builders


def _key(key: bytes, what: str) -> bytes:
    if len(key) != 16:
        raise ValueError(f"{what} must be 16 bytes, got {len(key)}")
    return key


def appkey_add(app_key: bytes, app_key_index: int = 0, net_key_index: int = 0) -> bytes:
    """Config AppKey Add: `[netKeyIndex ‖ appKeyIndex, 3 bytes packed][AppKey 16]`."""
    return (
        encode_opcode(CONFIG_APPKEY_ADD)
        + _pack_key_indexes(net_key_index, app_key_index)
        + _key(app_key, "AppKey")
    )


def appkey_update(
    app_key: bytes, app_key_index: int = 0, net_key_index: int = 0
) -> bytes:
    """Config AppKey Update (key refresh phase 1), same layout as Add."""
    return (
        encode_opcode(CONFIG_APPKEY_UPDATE)
        + _pack_key_indexes(net_key_index, app_key_index)
        + _key(app_key, "AppKey")
    )


def appkey_delete(app_key_index: int = 0, net_key_index: int = 0) -> bytes:
    """Config AppKey Delete: `[netKeyIndex ‖ appKeyIndex, 3 bytes packed]`."""
    return encode_opcode(CONFIG_APPKEY_DELETE) + _pack_key_indexes(
        net_key_index, app_key_index
    )


def netkey_add(net_key: bytes, net_key_index: int = 0) -> bytes:
    """Config NetKey Add: `[netKeyIndex u16][NetKey 16]`."""
    return (
        encode_opcode(CONFIG_NETKEY_ADD)
        + _key_index(net_key_index, "NetKey index").to_bytes(2, "little")
        + _key(net_key, "NetKey")
    )


def netkey_update(net_key: bytes, net_key_index: int = 0) -> bytes:
    """Config NetKey Update (key refresh phase 1, what the app's KeyRenewal sends), same layout as Add."""
    return (
        encode_opcode(CONFIG_NETKEY_UPDATE)
        + _key_index(net_key_index, "NetKey index").to_bytes(2, "little")
        + _key(net_key, "NetKey")
    )


def netkey_delete(net_key_index: int) -> bytes:
    """Config NetKey Delete: `[netKeyIndex u16]`."""
    return encode_opcode(CONFIG_NETKEY_DELETE) + _key_index(
        net_key_index, "NetKey index"
    ).to_bytes(2, "little")


def key_refresh_phase_get(net_key_index: int = 0) -> bytes:
    """Config Key Refresh Phase Get: `[netKeyIndex u16]`."""
    return encode_opcode(CONFIG_KEY_REFRESH_PHASE_GET) + _key_index(
        net_key_index, "NetKey index"
    ).to_bytes(2, "little")


def key_refresh_phase_set(transition: int, net_key_index: int = 0) -> bytes:
    """Config Key Refresh Phase Set: `[netKeyIndex u16][transition u8]` — 2 (use new keys) or 3 (revoke old keys)."""
    if transition not in (2, 3):
        raise ValueError(f"key refresh transition must be 2 or 3, got {transition}")
    return (
        encode_opcode(CONFIG_KEY_REFRESH_PHASE_SET)
        + _key_index(net_key_index, "NetKey index").to_bytes(2, "little")
        + bytes([transition])
    )


def composition_data_get(page: int = 0) -> bytes:
    """Config Composition Data Get for `page` (0 is the only page 1.0.1 nodes have)."""
    if not 0 <= page <= 0xFF:
        raise ValueError(f"page must be 0..255, got {page}")
    return encode_opcode(CONFIG_COMPOSITION_DATA_GET) + bytes([page])


def model_app_bind(element: int, model: int | str, app_key_index: int = 0) -> bytes:
    """Config Model App Bind: `[element u16][appKeyIndex u16][model 2|4]`."""
    return (
        encode_opcode(CONFIG_MODEL_APP_BIND)
        + _element(element)
        + _key_index(app_key_index, "AppKey index").to_bytes(2, "little")
        + encode_model_id(model)
    )


def model_app_unbind(element: int, model: int | str, app_key_index: int = 0) -> bytes:
    """Config Model App Unbind, same layout as Bind."""
    return (
        encode_opcode(CONFIG_MODEL_APP_UNBIND)
        + _element(element)
        + _key_index(app_key_index, "AppKey index").to_bytes(2, "little")
        + encode_model_id(model)
    )


def model_publication_get(element: int, model: int | str) -> bytes:
    """Config Model Publication Get: `[element u16][model 2|4]`."""
    return (
        encode_opcode(CONFIG_MODEL_PUBLICATION_GET)
        + _element(element)
        + encode_model_id(model)
    )


def model_publication_set(
    element: int,
    publish_address: int,
    model: int | str,
    *,
    app_key_index: int = 0,
    credential: bool = False,
    ttl: int = PUBLISH_TTL_DEFAULT,
    period_steps: int = 0,
    period_resolution: int = 0,
    retransmit_count: int = 0,
    retransmit_interval_steps: int = 0,
) -> bytes:
    """Config Model Publication Set (§4.3.2.16), 11 or 13 parameter bytes — always segmented on the wire.

    Layout: `[element u16][publishAddress u16][appKeyIndex 12 bits + credentialFlag 1 bit + RFU 3 bits, u16]
    [publishTTL u8][period: steps 6 bits + resolution 2 bits][retransmit: count 3 bits + interval steps 5 bits]
    [model 2|4]`. The defaults are the app's (`ConnectToAddress`): node default TTL 0xFF, no period, no
    retransmission, AppKey 0, master credentials. `publish_address` 0x0000 disables publishing (what the app
    sends to remove a connection). Virtual (label UUID) publishing is not supported.
    """
    if _is_virtual(publish_address):
        raise ValueError(
            "virtual publish addresses need Model Publication Virtual Address Set (not supported)"
        )
    if ttl != PUBLISH_TTL_DEFAULT and not 0 <= ttl <= 0x7F:
        raise ValueError(f"publish TTL must be 0..127 or 0xFF, got {ttl:#x}")
    if not 0 <= period_steps <= 63:
        raise ValueError(f"period steps must be 0..63, got {period_steps}")
    if not 0 <= period_resolution <= 3:
        raise ValueError(f"period resolution must be 0..3, got {period_resolution}")
    key_and_cred = _key_index(app_key_index, "AppKey index") | (
        (1 if credential else 0) << 12
    )
    return (
        encode_opcode(CONFIG_MODEL_PUBLICATION_SET)
        + _element(element)
        + _u16(publish_address, "publish address")
        + key_and_cred.to_bytes(2, "little")
        + bytes([ttl, (period_resolution << 6) | period_steps])
        + _transmit_byte(retransmit_count, retransmit_interval_steps, "retransmit")
        + encode_model_id(model)
    )


def _subscription(opcode: int, element: int, address: int, model: int | str) -> bytes:
    if not 0xC000 <= address <= 0xFFFF or address in (0xFFFC, 0xFFFD, 0xFFFE, 0xFFFF):
        raise ValueError(
            f"subscription address must be a group address 0xC000..0xFFFB, got {address:#06x}"
        )
    return (
        encode_opcode(opcode)
        + _element(element)
        + address.to_bytes(2, "little")
        + encode_model_id(model)
    )


def model_subscription_add(element: int, address: int, model: int | str) -> bytes:
    """Config Model Subscription Add: `[element u16][group address u16][model 2|4]`."""
    return _subscription(CONFIG_MODEL_SUBSCRIPTION_ADD, element, address, model)


def model_subscription_delete(element: int, address: int, model: int | str) -> bytes:
    """Config Model Subscription Delete, same layout as Add."""
    return _subscription(CONFIG_MODEL_SUBSCRIPTION_DELETE, element, address, model)


def model_subscription_overwrite(element: int, address: int, model: int | str) -> bytes:
    """Config Model Subscription Overwrite (replace the whole list with one address), same layout as Add."""
    return _subscription(CONFIG_MODEL_SUBSCRIPTION_OVERWRITE, element, address, model)


def model_subscription_delete_all(element: int, model: int | str) -> bytes:
    """Config Model Subscription Delete All: `[element u16][model 2|4]` (the app never uses it)."""
    return (
        encode_opcode(CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL)
        + _element(element)
        + encode_model_id(model)
    )


def model_subscription_get(element: int, model: int | str) -> bytes:
    """Config SIG / Vendor Model Subscription Get (picked by the model kind): `[element u16][model 2|4]`."""
    op = (
        CONFIG_VENDOR_MODEL_SUBSCRIPTION_GET
        if is_vendor_model(model)
        else CONFIG_SIG_MODEL_SUBSCRIPTION_GET
    )
    return encode_opcode(op) + _element(element) + encode_model_id(model)


def model_app_get(element: int, model: int | str) -> bytes:
    """Config SIG / Vendor Model App Get (picked by the model kind): `[element u16][model 2|4]`."""
    op = (
        CONFIG_VENDOR_MODEL_APP_GET
        if is_vendor_model(model)
        else CONFIG_SIG_MODEL_APP_GET
    )
    return encode_opcode(op) + _element(element) + encode_model_id(model)


def gatt_proxy_get() -> bytes:
    """Config GATT Proxy Get."""
    return encode_opcode(CONFIG_GATT_PROXY_GET)


def gatt_proxy_set(enabled: bool) -> bytes:
    """Config GATT Proxy Set (the app sends 1 during set-up, 0 for low-power nodes at the end)."""
    return encode_opcode(CONFIG_GATT_PROXY_SET) + _feature_state(enabled, "GATT proxy")


def default_ttl_get() -> bytes:
    """Config Default TTL Get."""
    return encode_opcode(CONFIG_DEFAULT_TTL_GET)


def default_ttl_set(ttl: int = 5) -> bytes:
    """Config Default TTL Set (0 or 2..127; the app sets 5)."""
    if ttl == 1 or not 0 <= ttl <= 0x7F:
        raise ValueError(f"default TTL must be 0 or 2..127, got {ttl}")
    return encode_opcode(CONFIG_DEFAULT_TTL_SET) + bytes([ttl])


def relay_get() -> bytes:
    """Config Relay Get."""
    return encode_opcode(CONFIG_RELAY_GET)


def relay_set(
    relay: bool = True, retransmit_count: int = 3, retransmit_interval_steps: int = 9
) -> bytes:
    """Config Relay Set: `[relay u8][count 3 bits + interval steps 5 bits]`.

    Interval = (steps + 1) x 10 ms. Defaults are the app's set-up values (network-logic.md §3.2 step 3).
    """
    return (
        encode_opcode(CONFIG_RELAY_SET)
        + _feature_state(relay, "relay")
        + _transmit_byte(
            retransmit_count, retransmit_interval_steps, "relay retransmit"
        )
    )


def network_transmit_get() -> bytes:
    """Config Network Transmit Get."""
    return encode_opcode(CONFIG_NETWORK_TRANSMIT_GET)


def network_transmit_set(count: int = 3, interval_steps: int = 10) -> bytes:
    """Config Network Transmit Set: `[count 3 bits + interval steps 5 bits]`, interval = (steps + 1) x 10 ms.

    Defaults are the app's set-up values (network-logic.md §3.2 step 3).
    """
    return encode_opcode(CONFIG_NETWORK_TRANSMIT_SET) + _transmit_byte(
        count, interval_steps, "network transmit"
    )


def beacon_get() -> bytes:
    """Config Beacon Get."""
    return encode_opcode(CONFIG_BEACON_GET)


def beacon_set(enabled: bool) -> bytes:
    """Config Beacon Set (Secure Network Beacon broadcasting; the app enables it except on low-power nodes)."""
    return encode_opcode(CONFIG_BEACON_SET) + _feature_state(enabled, "beacon")


def node_reset() -> bytes:
    """Config Node Reset — the node forgets the network; answered with Node Reset Status."""
    return encode_opcode(CONFIG_NODE_RESET)


def heartbeat_publication_get() -> bytes:
    """Config Heartbeat Publication Get."""
    return encode_opcode(CONFIG_HEARTBEAT_PUBLICATION_GET)


def heartbeat_publication_set(
    destination: int,
    period_log: int,
    count_log: int = HEARTBEAT_INDEFINITE,
    ttl: int = 5,
    features: int = 0,
    net_key_index: int = 0,
) -> bytes:
    """Config Heartbeat Publication Set (§4.3.2.62): `[dst u16][CountLog][PeriodLog][TTL][Features u16][NetKeyIndex u16]`.

    The node then sends a Heartbeat (transport control message) to `destination` every 2^(PeriodLog-1) seconds,
    2^(CountLog-1) times (0xFF = indefinitely, 0 = none); `features` selects the feature changes that trigger an
    extra beat (bit 0 relay, 1 proxy, 2 friend, 3 low power). Destination 0x0000 or PeriodLog 0 disables it.
    The JUNG app never sends this; `docs/hidden-features.md` §4.
    """
    if not 0 <= period_log <= 0x11:
        raise ValueError(f"PeriodLog {period_log} is not 0..0x11")
    if not (0 <= count_log <= 0x11 or count_log == HEARTBEAT_INDEFINITE):
        raise ValueError(f"CountLog {count_log} is not 0..0x11 or 0xFF")
    if not 0 <= ttl <= 0x7F:
        raise ValueError(f"TTL {ttl} is not 0..127")
    if features & ~0x000F:
        raise ValueError(f"features {features:#x} use reserved bits")
    if _is_virtual(destination):  # §4.3.2.62: unassigned, unicast or group only
        raise ValueError(
            f"heartbeat destination {destination:#06x} is a virtual address"
        )
    return (
        encode_opcode(CONFIG_HEARTBEAT_PUBLICATION_SET)
        + _u16(destination, "destination")
        + bytes([count_log, period_log, ttl])
        + _u16(features, "features")
        + _u16(_key_index(net_key_index, "netkey"), "netkey")
    )


def heartbeat_subscription_get() -> bytes:
    """Config Heartbeat Subscription Get (§4.3.2.65)."""
    return encode_opcode(CONFIG_HEARTBEAT_SUBSCRIPTION_GET)


def heartbeat_subscription_set(source: int, destination: int, period_log: int) -> bytes:
    """Config Heartbeat Subscription Set (§4.3.2.66): `[source u16][destination u16][PeriodLog]`.

    The node then counts the Heartbeats `source` sends to `destination` (its own unicast or a group it can hear,
    0xFFFF included) for 2^(PeriodLog-1) seconds and keeps the hop range in its Heartbeat Subscription state —
    `HeartbeatSubscriptionStatus.min_hops` / `max_hops` say how far `source` is from this node. All three zero
    (`heartbeat_subscription_off()`) stops counting. Verified on `0148` ← `01A4`
    (`docs/hidden-features.md` §10).
    """
    if not 0 <= period_log <= 0x11:
        raise ValueError(f"PeriodLog {period_log} is not 0..0x11")
    if source != 0 and not is_unicast(source):  # §4.3.2.66: unassigned or unicast only
        raise ValueError(f"heartbeat source {source:#06x} is not a unicast address")
    if _is_virtual(destination):
        raise ValueError(
            f"heartbeat destination {destination:#06x} is a virtual address"
        )
    return (
        encode_opcode(CONFIG_HEARTBEAT_SUBSCRIPTION_SET)
        + _u16(source, "source")
        + _u16(destination, "destination")
        + bytes([period_log])
    )


def heartbeat_subscription_off() -> bytes:
    """Config Heartbeat Subscription Set that disables the subscription (source, destination and period 0)."""
    return heartbeat_subscription_set(0x0000, 0x0000, HEARTBEAT_PERIOD_OFF)


def heartbeat_period_seconds(period_log: int) -> int:
    """Seconds between heartbeats for a PeriodLog (0 = disabled)."""
    return 0 if period_log == 0 else 2 ** (period_log - 1)


def heartbeat_period_log(seconds: float) -> int:
    """Return the smallest PeriodLog whose period covers `seconds` (1 s → 1, 3 s → 3, 64 s → 7); 0x11 at most."""
    log = 1
    while heartbeat_period_seconds(log) < seconds and log < 0x11:
        log += 1
    return log


def heartbeat_count_range(count_log: int) -> tuple[int, int]:
    """Heartbeats a Subscription Status CountLog stands for (§4.2.18.7): n is 2^(n-1) .. 2^n - 1.

    0 → 0, 1 → 1, 2 → 2..3, 3 → 4..7, …, 0x10 → 32768..65534, 0xFF → 0xFFFF (more than 0xFFFE). This is the
    Subscription rule (Zephyr's `hb_log`), not the Publication CountLog's; 0x11..0xFE are prohibited here.
    """
    if count_log == 0:
        return (0, 0)
    if count_log == HEARTBEAT_INDEFINITE:
        return (0xFFFF, 0xFFFF)
    if not 1 <= count_log <= 0x10:
        raise ValueError(f"Subscription CountLog {count_log:#x} is prohibited")
    # the counter is 16-bit and 0xFFFF means "indefinite": the last range ends at 0xFFFE
    return (1 << (count_log - 1), min((1 << count_log) - 1, 0xFFFE))


# ----------------------------------------------------------------------------- status decoders


@dataclass(frozen=True)
class ConfigStatus:
    """Base of every Config status that carries a status code (§4.3.2, Table 4.108)."""

    status: int

    @property
    def ok(self) -> bool:
        """Whether the node reported Success."""
        return self.status == STATUS_SUCCESS

    @property
    def status_name(self) -> str:
        """Human-readable status code."""
        return STATUS_NAMES.get(self.status, f"status 0x{self.status:02X}")


@dataclass(frozen=True)
class AppKeyStatus(ConfigStatus):
    """Config AppKey Status: `[status][netKeyIndex ‖ appKeyIndex packed]`."""

    net_key_index: int
    app_key_index: int


@dataclass(frozen=True)
class NetKeyStatus(ConfigStatus):
    """Config NetKey Status: `[status][netKeyIndex u16]`."""

    net_key_index: int


@dataclass(frozen=True)
class KeyRefreshPhaseStatus(ConfigStatus):
    """Config Key Refresh Phase Status: `[status][netKeyIndex u16][phase u8]` (0 normal, 1 distributing, 2 switched, 3 revoking)."""

    net_key_index: int
    phase: int


@dataclass(frozen=True)
class CompositionElement:
    """One element of Composition Data page 0: location descriptor and the model ids it hosts."""

    location: int
    sig_models: list[int]
    vendor_models: list[int]  # company << 16 | model, as `model_id` normalises

    @property
    def model_ids(self) -> list[str]:
        """Model ids in CDB form ("1000", "05271013"), SIG first — comparable with `cdb.Element.models`."""
        return [model_id_str(m) for m in self.sig_models + self.vendor_models]


@dataclass(frozen=True)
class CompositionData:
    """Config Composition Data Status page 0 (§4.2.1.1)."""

    page: int
    cid: int
    pid: int
    vid: int
    crpl: int
    features: int
    elements: list[CompositionElement]

    @property
    def relay(self) -> bool:
        """Relay feature supported."""
        return bool(self.features & 0x01)

    @property
    def proxy(self) -> bool:
        """GATT Proxy feature supported."""
        return bool(self.features & 0x02)

    @property
    def friend(self) -> bool:
        """Friend feature supported."""
        return bool(self.features & 0x04)

    @property
    def low_power(self) -> bool:
        """Low Power feature supported."""
        return bool(self.features & 0x08)


@dataclass(frozen=True)
class ModelAppStatus(ConfigStatus):
    """Config Model App Status: `[status][element u16][appKeyIndex u16][model 2|4]`."""

    element: int
    app_key_index: int
    model: int


@dataclass(frozen=True)
class ModelPublicationStatus(ConfigStatus):
    """Config Model Publication Status: the Publication Set fields behind a status byte."""

    element: int
    publish_address: int
    app_key_index: int
    credential: bool
    ttl: int
    period_steps: int
    period_resolution: int
    retransmit_count: int
    retransmit_interval_steps: int
    model: int


@dataclass(frozen=True)
class ModelSubscriptionStatus(ConfigStatus):
    """Config Model Subscription Status: `[status][element u16][address u16][model 2|4]`."""

    element: int
    address: int
    model: int


@dataclass(frozen=True)
class ModelSubscriptionList(ConfigStatus):
    """Config SIG / Vendor Model Subscription List: `[status][element u16][model 2|4][addresses u16…]`."""

    element: int
    model: int
    addresses: list[int]


@dataclass(frozen=True)
class ModelAppList(ConfigStatus):
    """Config SIG / Vendor Model App List: `[status][element u16][model 2|4][AppKey indexes, packed]`."""

    element: int
    model: int
    app_key_indexes: list[int]


@dataclass(frozen=True)
class GattProxyStatus:
    """Config GATT Proxy Status: `[state]` (0 disabled, 1 enabled, 2 not supported)."""

    gatt_proxy: int


@dataclass(frozen=True)
class DefaultTtlStatus:
    """Config Default TTL Status: `[ttl]`."""

    ttl: int


@dataclass(frozen=True)
class HeartbeatPublicationStatus(ConfigStatus):
    """Config Heartbeat Publication Status: `[status][dst u16][CountLog][PeriodLog][TTL][Features u16][NetKeyIndex u16]`."""

    destination: int
    count_log: int
    period_log: int
    ttl: int
    features: int
    net_key_index: int

    @property
    def enabled(self) -> bool:
        """Whether the node is publishing heartbeats at all."""
        return (
            self.destination != 0
            and self.period_log != HEARTBEAT_PERIOD_OFF
            and self.count_log != 0
        )


@dataclass(frozen=True)
class HeartbeatSubscriptionStatus(ConfigStatus):
    """Config Heartbeat Subscription Status: `[status][src u16][dst u16][PeriodLog][CountLog][MinHops][MaxHops]`.

    `count_log` counts the Heartbeats received in the period (log form, 0 = none); `min_hops` / `max_hops` are the
    fewest and most hops any of them took (`0x7F` / `0` until one arrives). A disabled subscription reads all
    zero, hops included, once the node has been asked to stop; a node that has counted keeps the hop range in a
    disabled state until the next Set (`0148` read `hops=1..1` after being switched off).
    """

    source: int
    destination: int
    period_log: int
    count_log: int
    min_hops: int
    max_hops: int

    @property
    def active(self) -> bool:
        """Whether the node is (still) counting: a source, a destination and time left in the period."""
        return (
            self.source != 0
            and self.destination != 0
            and self.period_log != HEARTBEAT_PERIOD_OFF
        )


@dataclass(frozen=True)
class RelayStatus:
    """Config Relay Status: `[relay][count 3 bits + interval steps 5 bits]`."""

    relay: int
    retransmit_count: int
    retransmit_interval_steps: int


@dataclass(frozen=True)
class NetworkTransmitStatus:
    """Config Network Transmit Status: `[count 3 bits + interval steps 5 bits]`."""

    count: int
    interval_steps: int


@dataclass(frozen=True)
class BeaconStatus:
    """Config Beacon Status: `[beacon]` (0 off, 1 on)."""

    beacon: int


@dataclass(frozen=True)
class NodeResetStatus:
    """Config Node Reset Status (no parameters): the node has left the network."""


def _need(params: bytes, n: int, what: str) -> None:
    if len(params) < n:
        raise ValueError(f"{what}: need {n} bytes, got {len(params)}")


def _model_tail(tail: bytes, what: str) -> int:
    if len(tail) not in (2, 4):
        raise ValueError(
            f"{what}: model identifier must be 2 or 4 bytes, got {len(tail)}"
        )
    return decode_model_id(tail)


def decode_appkey_status(params: bytes) -> AppKeyStatus:
    """Decode Config AppKey Status."""
    _need(params, 4, "AppKey Status")
    net, app = _unpack_key_indexes(params[1:4])
    return AppKeyStatus(params[0], net, app)


def decode_netkey_status(params: bytes) -> NetKeyStatus:
    """Decode Config NetKey Status."""
    _need(params, 3, "NetKey Status")
    return NetKeyStatus(params[0], int.from_bytes(params[1:3], "little") & 0xFFF)


def decode_key_refresh_phase_status(params: bytes) -> KeyRefreshPhaseStatus:
    """Decode Config Key Refresh Phase Status."""
    _need(params, 4, "Key Refresh Phase Status")
    return KeyRefreshPhaseStatus(
        params[0], int.from_bytes(params[1:3], "little") & 0xFFF, params[3]
    )


def decode_composition_data(params: bytes) -> CompositionData:
    """Decode Config Composition Data Status; only page 0 is understood (ValueError otherwise)."""
    _need(params, 11, "Composition Data Status")
    page = params[0]
    if page != 0:
        raise ValueError(f"only Composition Data page 0 is supported, got page {page}")
    cid, pid, vid, crpl, features = (
        int.from_bytes(params[i : i + 2], "little") for i in range(1, 11, 2)
    )
    elements: list[CompositionElement] = []
    i = 11
    while i < len(params):
        _need(params, i + 4, "Composition Data element header")
        loc = int.from_bytes(params[i : i + 2], "little")
        num_s, num_v = params[i + 2], params[i + 3]
        i += 4
        _need(params, i + 2 * num_s + 4 * num_v, "Composition Data model list")
        sig = [
            int.from_bytes(params[j : j + 2], "little")
            for j in range(i, i + 2 * num_s, 2)
        ]
        i += 2 * num_s
        vendor = [
            decode_model_id(params[j : j + 4]) for j in range(i, i + 4 * num_v, 4)
        ]
        i += 4 * num_v
        elements.append(CompositionElement(loc, sig, vendor))
    return CompositionData(page, cid, pid, vid, crpl, features, elements)


def decode_model_app_status(params: bytes) -> ModelAppStatus:
    """Decode Config Model App Status."""
    _need(params, 7, "Model App Status")
    return ModelAppStatus(
        params[0],
        int.from_bytes(params[1:3], "little"),
        int.from_bytes(params[3:5], "little") & 0xFFF,
        _model_tail(params[5:], "Model App Status"),
    )


def decode_model_publication_status(params: bytes) -> ModelPublicationStatus:
    """Decode Config Model Publication Status (12 or 14 bytes)."""
    _need(params, 12, "Model Publication Status")
    key_and_cred = int.from_bytes(params[5:7], "little")
    count, steps = _split_transmit_byte(params[9])
    return ModelPublicationStatus(
        params[0],
        int.from_bytes(params[1:3], "little"),
        int.from_bytes(params[3:5], "little"),
        key_and_cred & 0xFFF,
        bool(key_and_cred & 0x1000),
        params[7],
        params[8] & 0x3F,
        params[8] >> 6,
        count,
        steps,
        _model_tail(params[10:], "Model Publication Status"),
    )


def decode_model_subscription_status(params: bytes) -> ModelSubscriptionStatus:
    """Decode Config Model Subscription Status."""
    _need(params, 7, "Model Subscription Status")
    return ModelSubscriptionStatus(
        params[0],
        int.from_bytes(params[1:3], "little"),
        int.from_bytes(params[3:5], "little"),
        _model_tail(params[5:], "Model Subscription Status"),
    )


def decode_model_subscription_list(opcode: int, params: bytes) -> ModelSubscriptionList:
    """Decode Config SIG (0x802A) or Vendor (0x802C) Model Subscription List; the opcode fixes the model width."""
    if opcode not in (
        CONFIG_SIG_MODEL_SUBSCRIPTION_LIST,
        CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST,
    ):
        raise ValueError(f"not a Model Subscription List opcode: {opcode:#06x}")
    width = 4 if opcode == CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST else 2
    _need(params, 3 + width, "Model Subscription List")
    rest = params[3 + width :]
    if len(rest) % 2:
        raise ValueError("Model Subscription List: odd address bytes")
    return ModelSubscriptionList(
        params[0],
        int.from_bytes(params[1:3], "little"),
        decode_model_id(params[3 : 3 + width]),
        [int.from_bytes(rest[i : i + 2], "little") for i in range(0, len(rest), 2)],
    )


def decode_model_app_list(opcode: int, params: bytes) -> ModelAppList:
    """Decode Config SIG (0x804C) or Vendor (0x804E) Model App List; the opcode fixes the model width."""
    if opcode not in (CONFIG_SIG_MODEL_APP_LIST, CONFIG_VENDOR_MODEL_APP_LIST):
        raise ValueError(f"not a Model App List opcode: {opcode:#06x}")
    width = 4 if opcode == CONFIG_VENDOR_MODEL_APP_LIST else 2
    _need(params, 3 + width, "Model App List")
    return ModelAppList(
        params[0],
        int.from_bytes(params[1:3], "little"),
        decode_model_id(params[3 : 3 + width]),
        _unpack_key_index_list(params[3 + width :], "Model App List"),
    )


def decode_gatt_proxy_status(params: bytes) -> GattProxyStatus:
    """Decode Config GATT Proxy Status."""
    _need(params, 1, "GATT Proxy Status")
    return GattProxyStatus(params[0])


def decode_default_ttl_status(params: bytes) -> DefaultTtlStatus:
    """Decode Config Default TTL Status."""
    _need(params, 1, "Default TTL Status")
    return DefaultTtlStatus(params[0])


def decode_heartbeat_publication_status(params: bytes) -> HeartbeatPublicationStatus:
    """Decode Config Heartbeat Publication Status."""
    _need(params, 10, "Heartbeat Publication Status")
    return HeartbeatPublicationStatus(
        params[0],
        int.from_bytes(params[1:3], "little"),
        params[3],
        params[4],
        params[5],
        int.from_bytes(params[6:8], "little"),
        int.from_bytes(params[8:10], "little") & 0xFFF,
    )


def decode_heartbeat_subscription_status(
    params: bytes,
) -> HeartbeatSubscriptionStatus:
    """Decode Config Heartbeat Subscription Status."""
    _need(params, 9, "Heartbeat Subscription Status")
    return HeartbeatSubscriptionStatus(
        params[0],
        int.from_bytes(params[1:3], "little"),
        int.from_bytes(params[3:5], "little"),
        params[5],
        params[6],
        params[7],
        params[8],
    )


def decode_relay_status(params: bytes) -> RelayStatus:
    """Decode Config Relay Status."""
    _need(params, 2, "Relay Status")
    count, steps = _split_transmit_byte(params[1])
    return RelayStatus(params[0], count, steps)


def decode_network_transmit_status(params: bytes) -> NetworkTransmitStatus:
    """Decode Config Network Transmit Status."""
    _need(params, 1, "Network Transmit Status")
    return NetworkTransmitStatus(*_split_transmit_byte(params[0]))


def decode_beacon_status(params: bytes) -> BeaconStatus:
    """Decode Config Beacon Status."""
    _need(params, 1, "Beacon Status")
    return BeaconStatus(params[0])


def decode_node_reset_status(params: bytes) -> NodeResetStatus:
    """Decode Config Node Reset Status (carries nothing)."""
    return NodeResetStatus()


ConfigDecoded = (
    AppKeyStatus
    | NetKeyStatus
    | KeyRefreshPhaseStatus
    | CompositionData
    | ModelAppStatus
    | ModelPublicationStatus
    | ModelSubscriptionStatus
    | ModelSubscriptionList
    | ModelAppList
    | GattProxyStatus
    | DefaultTtlStatus
    | RelayStatus
    | NetworkTransmitStatus
    | BeaconStatus
    | NodeResetStatus
    | HeartbeatPublicationStatus
    | HeartbeatSubscriptionStatus
)


_STATUS_DECODERS: dict[int, Callable[[bytes], ConfigDecoded]] = {
    CONFIG_APPKEY_STATUS: decode_appkey_status,
    CONFIG_NETKEY_STATUS: decode_netkey_status,
    CONFIG_KEY_REFRESH_PHASE_STATUS: decode_key_refresh_phase_status,
    CONFIG_COMPOSITION_DATA_STATUS: decode_composition_data,
    CONFIG_MODEL_APP_STATUS: decode_model_app_status,
    CONFIG_MODEL_PUBLICATION_STATUS: decode_model_publication_status,
    CONFIG_MODEL_SUBSCRIPTION_STATUS: decode_model_subscription_status,
    CONFIG_SIG_MODEL_SUBSCRIPTION_LIST: partial(
        decode_model_subscription_list, CONFIG_SIG_MODEL_SUBSCRIPTION_LIST
    ),
    CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST: partial(
        decode_model_subscription_list, CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST
    ),
    CONFIG_SIG_MODEL_APP_LIST: partial(
        decode_model_app_list, CONFIG_SIG_MODEL_APP_LIST
    ),
    CONFIG_VENDOR_MODEL_APP_LIST: partial(
        decode_model_app_list, CONFIG_VENDOR_MODEL_APP_LIST
    ),
    CONFIG_GATT_PROXY_STATUS: decode_gatt_proxy_status,
    CONFIG_DEFAULT_TTL_STATUS: decode_default_ttl_status,
    CONFIG_RELAY_STATUS: decode_relay_status,
    CONFIG_NETWORK_TRANSMIT_STATUS: decode_network_transmit_status,
    CONFIG_BEACON_STATUS: decode_beacon_status,
    CONFIG_NODE_RESET_STATUS: decode_node_reset_status,
    CONFIG_HEARTBEAT_PUBLICATION_STATUS: decode_heartbeat_publication_status,
    CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS: decode_heartbeat_subscription_status,
}


def decode_config(opcode: int, params: bytes) -> ConfigDecoded | None:
    """Decode any Config status by opcode; None for opcodes that are not a status decoded here."""
    decoder = _STATUS_DECODERS.get(opcode)
    return decoder(params) if decoder else None


# ----------------------------------------------------------------------------- describe for the CLI and logs


def describe_config(opcode: int, params: bytes, *, devkey: bool = False) -> str:
    """Human-readable one-liner for a Config message (either direction); never raises (malformed → hex).

    A key-carrying message (`KEY_CARRYING_OPCODES`) shows `key=<16 bytes>`, and when malformed only the byte
    count: the new NetKey / AppKey of a key refresh must never reach a log. With `devkey` (the PDU travelled
    under a device key, so *any* undecoded bytes may be key material — the guard `messages.describe` applies)
    every fallback prints the byte count, including the one for an opcode this module does not name.
    """
    name = CONFIG_NAMES.get(opcode)
    if name is None:
        return (
            f"Config op {opcode:04X} {byte_count(params) if devkey else params.hex()}"
        )
    try:
        return f"Config {name} {_describe_params(opcode, params)}".rstrip()
    except ValueError:
        raw = (
            byte_count(params)
            if devkey or opcode in KEY_CARRYING_OPCODES
            else params.hex()
        )
        return f"Config {name} ?? {raw}"


def byte_count(p: bytes) -> str:
    """`<N bytes>`: what a describe fallback prints instead of hex when the bytes may be key material."""
    return f"<{len(p)} byte{'' if len(p) == 1 else 's'}>"


def _describe_params(opcode: int, p: bytes) -> str:  # noqa: PLR0911  # flat opcode → text dispatch
    decoded = decode_config(opcode, p)
    if decoded is not None:
        return _describe_status(decoded)
    if opcode in (CONFIG_APPKEY_ADD, CONFIG_APPKEY_UPDATE):
        _need(p, 19, CONFIG_NAMES[opcode])
        net, app = _unpack_key_indexes(p[:3])
        return f"netkey={net} appkey={app} key=<16 bytes>"  # never log key material
    if opcode == CONFIG_APPKEY_DELETE:
        _need(p, 3, "AppKey Delete")
        net, app = _unpack_key_indexes(p[:3])
        return f"netkey={net} appkey={app}"
    if opcode in (CONFIG_NETKEY_ADD, CONFIG_NETKEY_UPDATE):
        _need(p, 18, CONFIG_NAMES[opcode])
        return f"netkey={int.from_bytes(p[:2], 'little') & 0xFFF} key=<16 bytes>"
    if opcode in (CONFIG_NETKEY_DELETE, CONFIG_KEY_REFRESH_PHASE_GET):
        _need(p, 2, CONFIG_NAMES[opcode])
        return f"netkey={int.from_bytes(p[:2], 'little') & 0xFFF}"
    if opcode == CONFIG_KEY_REFRESH_PHASE_SET:
        _need(p, 3, "Key Refresh Phase Set")
        return f"netkey={int.from_bytes(p[:2], 'little') & 0xFFF} transition={p[2]}"
    if opcode == CONFIG_COMPOSITION_DATA_GET:
        _need(p, 1, "Composition Data Get")
        return f"page={p[0]}"
    if opcode in (CONFIG_MODEL_APP_BIND, CONFIG_MODEL_APP_UNBIND):
        _need(p, 6, name := CONFIG_NAMES[opcode])
        return (
            f"elem={int.from_bytes(p[:2], 'little'):04X} appkey={int.from_bytes(p[2:4], 'little') & 0xFFF}"
            f" model={model_id_str(_model_tail(p[4:], name))}"
        )
    if opcode == CONFIG_MODEL_PUBLICATION_SET:
        s = decode_model_publication_status(b"\x00" + p)
        return _publication_fields(s)
    if opcode in (
        CONFIG_MODEL_PUBLICATION_GET,
        CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL,
        CONFIG_SIG_MODEL_SUBSCRIPTION_GET,
        CONFIG_VENDOR_MODEL_SUBSCRIPTION_GET,
        CONFIG_SIG_MODEL_APP_GET,
        CONFIG_VENDOR_MODEL_APP_GET,
    ):
        _need(p, 4, name := CONFIG_NAMES[opcode])
        return f"elem={int.from_bytes(p[:2], 'little'):04X} model={model_id_str(_model_tail(p[2:], name))}"
    if opcode in (
        CONFIG_MODEL_SUBSCRIPTION_ADD,
        CONFIG_MODEL_SUBSCRIPTION_DELETE,
        CONFIG_MODEL_SUBSCRIPTION_OVERWRITE,
    ):
        _need(p, 6, name := CONFIG_NAMES[opcode])
        return (
            f"elem={int.from_bytes(p[:2], 'little'):04X} address={int.from_bytes(p[2:4], 'little'):04X}"
            f" model={model_id_str(_model_tail(p[4:], name))}"
        )
    if opcode in (CONFIG_GATT_PROXY_SET, CONFIG_BEACON_SET):
        _need(p, 1, CONFIG_NAMES[opcode])
        return f"{'enabled' if p[0] else 'disabled'}"
    if opcode == CONFIG_DEFAULT_TTL_SET:
        _need(p, 1, "Default TTL Set")
        return f"ttl={p[0]}"
    if opcode == CONFIG_RELAY_SET:
        return _describe_status(decode_relay_status(p))
    if opcode == CONFIG_NETWORK_TRANSMIT_SET:
        return _describe_status(decode_network_transmit_status(p))
    if opcode == CONFIG_HEARTBEAT_PUBLICATION_SET:
        return _describe_status(decode_heartbeat_publication_status(b"\x00" + p))[
            len("Success: ") :
        ]
    if opcode == CONFIG_HEARTBEAT_SUBSCRIPTION_SET:
        _need(p, 5, "Heartbeat Subscription Set")
        return (
            f"src={int.from_bytes(p[:2], 'little'):04X} dst={int.from_bytes(p[2:4], 'little'):04X}"
            f" period_log={p[4]} ({heartbeat_period_seconds(p[4])}s)"
        )
    return p.hex()  # the parameterless Gets and Node Reset


def _publication_fields(s: ModelPublicationStatus) -> str:
    return (
        f"elem={s.element:04X} publish={s.publish_address:04X} model={model_id_str(s.model)}"
        f" appkey={s.app_key_index} cred={int(s.credential)} ttl={s.ttl}"
        f" period={s.period_steps}/{s.period_resolution} retx={s.retransmit_count}/{s.retransmit_interval_steps}"
    )


def _describe_status(d: ConfigDecoded) -> str:  # noqa: PLR0911  # one branch per status type
    prefix = f"{d.status_name}: " if isinstance(d, ConfigStatus) else ""
    if isinstance(d, AppKeyStatus):
        return f"{prefix}netkey={d.net_key_index} appkey={d.app_key_index}"
    if isinstance(d, NetKeyStatus):
        return f"{prefix}netkey={d.net_key_index}"
    if isinstance(d, KeyRefreshPhaseStatus):
        return f"{prefix}netkey={d.net_key_index} phase={d.phase}"
    if isinstance(d, CompositionData):
        els = "; ".join(
            f"loc={e.location:04X} models={','.join(e.model_ids)}" for e in d.elements
        )
        return (
            f"cid={d.cid:04X} pid={d.pid:04X} vid={d.vid:04X} crpl={d.crpl} features={d.features:04X}"
            f" elements=[{els}]"
        )
    if isinstance(d, ModelAppStatus):
        return f"{prefix}elem={d.element:04X} appkey={d.app_key_index} model={model_id_str(d.model)}"
    if isinstance(d, ModelPublicationStatus):
        return f"{prefix}{_publication_fields(d)}"
    if isinstance(d, ModelSubscriptionStatus):
        return f"{prefix}elem={d.element:04X} address={d.address:04X} model={model_id_str(d.model)}"
    if isinstance(d, ModelSubscriptionList):
        addrs = ",".join(f"{a:04X}" for a in d.addresses)
        return f"{prefix}elem={d.element:04X} model={model_id_str(d.model)} addresses=[{addrs}]"
    if isinstance(d, ModelAppList):
        keys = ",".join(str(i) for i in d.app_key_indexes)
        return f"{prefix}elem={d.element:04X} model={model_id_str(d.model)} appkeys=[{keys}]"
    if isinstance(d, GattProxyStatus):
        return _feature_name(d.gatt_proxy)
    if isinstance(d, DefaultTtlStatus):
        return f"ttl={d.ttl}"
    if isinstance(d, RelayStatus):
        return f"{_feature_name(d.relay)} retx={d.retransmit_count}/{d.retransmit_interval_steps}"
    if isinstance(d, NetworkTransmitStatus):
        return f"count={d.count} steps={d.interval_steps}"
    if isinstance(d, BeaconStatus):
        return "enabled" if d.beacon else "disabled"
    if isinstance(d, HeartbeatPublicationStatus):
        return (
            f"{prefix}dst={d.destination:04X} count_log={d.count_log} period_log={d.period_log}"
            f" ({heartbeat_period_seconds(d.period_log)}s) ttl={d.ttl} features={d.features:04X}"
            f" netkey={d.net_key_index}"
        )
    if isinstance(d, HeartbeatSubscriptionStatus):
        return (
            f"{prefix}src={d.source:04X} dst={d.destination:04X} period_log={d.period_log}"
            f" ({heartbeat_period_seconds(d.period_log)}s) count_log={d.count_log}"
            f" hops={d.min_hops}..{d.max_hops}"
        )
    return ""  # NodeResetStatus


def _feature_name(state: int) -> str:
    return {0: "disabled", 1: "enabled", 2: "not supported"}.get(
        state, f"state {state}"
    )
