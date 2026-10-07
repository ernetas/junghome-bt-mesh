"""A received access message with its network-layer context (`AccessMessage`).

A value of its own, apart from the `ProxyClient` that delivers it (`client`, which re-exports it): the sniffer, the
audit and the plan model read messages without the link.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .messages import describe

__all__ = ["AccessMessage"]


def _now() -> float:
    """Return the monotonic clock, looked up at call time (a frozen clock in tests then stamps messages consistently)."""
    return time.monotonic()


@dataclass
class AccessMessage:
    """A received access message with its network-layer context.

    `repr()` leaves the payload out: a device-key message the sniffer decrypted may be a Config AppKey Add or
    NetKey Update carrying a mesh key in the clear, and a stray `%r` / debugger print must not leak it. `str()`
    shows the redacting `describe` of the payload instead.
    """

    src: int
    dst: int
    ttl: int
    seq: int
    opcode: int
    company_id: int | None
    params: bytes = field(repr=False)
    access_pdu: bytes = field(repr=False)
    key: str  # 'app0' | 'dev:<addr>'
    received: float = field(default_factory=_now)

    def __str__(self) -> str:
        """Format the message for logs: route, TTL, sequence number, key and a decoded description.

        A device-key message may be a key refresh carrying new keys: `describe` shows nothing undecoded of it.
        """
        return (
            f"{self.src:04X}→{self.dst:04X} ttl={self.ttl} seq={self.seq:06X} [{self.key}]"
            f" {describe(self.access_pdu, devkey=self.key.startswith('dev:'))}"
        )
