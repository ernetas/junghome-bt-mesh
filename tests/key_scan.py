"""Find key material in text, in any encoding a careless line of code could write it in (review-4 Q4-13, Q T8).

A key leaks as `key.hex()` far less often than one would hope to catch it: as upper-case hex from a `%X`-style
format, as Base64 from a JSON or export writer, as `list(key)` from a dataclass `asdict`, or as `repr(key)` from
an f-string or a log argument. `secrets` names every key of a network together with the keys derived from each
NetKey (encryption, privacy, identity, beacon and private beacon keys), which travel through the code as plain
`bytes` as well; `leaks` reports which of them a text contains, in which encoding.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from custom_components.junghome_ble.jhmesh.cdb import CDB
    from custom_components.junghome_ble.jhmesh.crypto import NetKeyMaterial


def encodings(secret: bytes) -> dict[str, str]:
    """`secret` written out every way the scan looks for, by the name of the encoding."""
    b64 = base64.b64encode(secret).decode()
    return {
        "hex": secret.hex(),
        "HEX": secret.hex().upper(),
        "base64": b64,
        "base64 unpadded": b64.rstrip("="),
        "base64url": base64.urlsafe_b64encode(secret).decode().rstrip("="),
        "list(bytes)": str(list(secret)),
        "repr(bytes)": repr(secret),
    }


def netkey_secrets(name: str, material: NetKeyMaterial) -> dict[str, bytes]:
    """A NetKey and every key derived from it (the NID and the Network ID are public: on air in every PDU)."""
    return {
        name: material.key,
        f"{name} encryption key": material.enc_key,
        f"{name} privacy key": material.priv_key,
        f"{name} identity key": material.identity_key,
        f"{name} beacon key": material.beacon_key,
        f"{name} private beacon key": material.private_beacon_key,
    }


def secrets(cdb: CDB) -> dict[str, bytes]:
    """Every key of `cdb`'s network by name: its NetKeys with what derives from them, AppKeys, device keys.

    A key of one repeated byte (the fixtures' placeholder device keys, all zeros) is left out: its encodings are
    runs of `0`, `A` or `[0, 0, …]`, which padding and empty fields produce as well.
    """
    found: dict[str, bytes] = {}
    for index, material in cdb.net_keys.items():
        found |= netkey_secrets(f"NetKey {index}", material)
    for index, (material, _phase) in cdb.net_key_refresh.items():
        found |= netkey_secrets(f"old NetKey {index}", material)
    for index, app in cdb.app_keys.items():
        found[f"AppKey {index}"] = app.key
    for node in cdb.nodes:
        found[f"device key of {node.unicast:04X}"] = node.dev_key
    return {name: key for name, key in found.items() if len(set(key)) > 1}


def leaks(text: str, keys: dict[str, bytes]) -> list[str]:
    """`<key name> as <encoding>` for every key of `keys` that `text` contains, in any of its encodings.

    An encoding is also looked for as a JSON string carries it (`repr(bytes)` has backslashes and quotes to escape),
    so a dump of a dict holding one is caught as well as the text it came from.
    """
    found = []
    for name, key in keys.items():
        for encoding, needle in encodings(key).items():
            if needle in text or json.dumps(needle)[1:-1] in text:
                found.append(f"{name} as {encoding}")
    return found
