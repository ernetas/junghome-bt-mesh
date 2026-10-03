"""The fixture network's addresses and the one status builder the library tests share.

Deliberately tiny and Home-Assistant-free: ``tests/jhmesh`` runs in CI on a Python that Home Assistant does not
support (the ``library`` job), with only ``jhmesh`` and pytest installed, so nothing here may reach for
``tests/helpers.py`` or ``tests/conftest.py`` (both import Home Assistant).
"""

from __future__ import annotations

from jhmesh import messages as M
from jhmesh.pdu import encode_opcode

OUR_ADDRESS = (
    0x0D00  # the unicast address the client (and the HA integration) sends from
)
LIGHT_SWITCH = (
    0x0148  # "WC mirror", push-button 1-gang: element 0 = light, 0x0149 = button
)
SOCKET = 0x0172  # "Boiler", metering socket: 0x0173 = power sensor element


def onoff_status(on: bool, target: bool | None = None, remaining: int = 0) -> bytes:
    """Generic OnOff Status `[present][target][remaining]` (Mesh Model §3.2.1.4)."""
    p = bytes([1 if on else 0])
    if target is not None:
        p += bytes([1 if target else 0, remaining])
    return encode_opcode(M.GEN_ONOFF_STATUS) + p
