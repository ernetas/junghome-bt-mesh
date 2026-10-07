"""Chaining a second consumer onto a message type the hub already handles.

`coordinator.STATUS_HANDLERS` keeps exactly one handler per message type (`register_status_handler`: a later
registration replaces the earlier one). A platform that also wants a type the coordinator or another platform handles
— the detectors and the thermostat on Sensor Status, the detectors on OnOff Set — registers through
`chain_status_handler`, which keeps the earlier handler and runs the new one after it. The earlier handler is the one
the table holds when the decorator runs, so the order is the modules' import order; the decorators stay in the
modules that own the features.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .coordinator import STATUS_HANDLERS, register_status_handler

if TYPE_CHECKING:
    from .coordinator import JungHomeHub, StatusHandler
    from .jhmesh.access import AccessMessage


def chain_status_handler(
    *opcodes: int,
) -> Callable[[StatusHandler], StatusHandler]:
    """Register the decorated handler for the SIG `opcodes` *behind* the handler each opcode already has.

    `register_status_handler` keeps one handler per message type; this keeps the coordinator's (the socket meter
    reading of a Sensor Status, the rocker event of an OnOff Set) and runs the new one after it. Opcodes that
    shared a handler share the chained one too.
    """

    def register(handler: StatusHandler) -> StatusHandler:
        chained_for: dict[StatusHandler | None, StatusHandler] = {}
        for opcode in opcodes:
            previous = STATUS_HANDLERS.get((None, opcode))
            if previous not in chained_for:
                chained_for[previous] = _chained(previous, handler)
            register_status_handler(opcode)(chained_for[previous])
        return handler

    return register


def _chained(previous: StatusHandler | None, handler: StatusHandler) -> StatusHandler:
    def chained(hub: JungHomeHub, m: AccessMessage, p: bytes) -> None:
        if previous is not None:
            previous(hub, m, p)
        handler(hub, m, p)

    return chained
