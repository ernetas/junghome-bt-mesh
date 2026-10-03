"""What the simulated air (and the GATT link) does to a PDU: seeded loss, duplication, reordering, targeted drops."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from jhmesh.pdu import NetworkPDU

Bearer = Literal["adv", "gatt-in", "gatt-out"]
Kind = Literal["network", "config", "beacon"]


@dataclass(frozen=True)
class Packet:
    """One transmission as a drop rule sees it.

    `bearer`: the advertising bearer between two nodes (`sender` → `receiver`, node addresses), or the GATT link
    of a proxy (`gatt-in` from the client, `gatt-out` to it). `net` is the decrypted network PDU (None for a
    beacon).
    """

    bearer: Bearer
    kind: Kind
    net: NetworkPDU | None
    sender: int | None = None
    receiver: int | None = None

    @property
    def is_segment_ack(self) -> bool:
        return (
            self.kind == "network"
            and self.net is not None
            and self.net.ctl
            and self.net.transport_pdu[0] == 0x00
        )

    @property
    def is_segment(self) -> bool:
        return (
            self.net is not None
            and not self.net.ctl
            and bool(self.net.transport_pdu[0] & 0x80)
        )

    @property
    def proxy_opcode(self) -> int | None:
        """The proxy configuration opcode (0x00 Set Filter Type, 0x03 Filter Status, ...) of a config PDU."""
        if self.kind != "config" or self.net is None:
            return None
        return self.net.transport_pdu[0]


@dataclass
class DropRule:
    """Drop every copy of the first `limit` PDUs the predicate matches (all hops, all retransmissions of them).

    A PDU is identified by (SRC, IV index, SEQ); a rule that picked one drops it wherever it shows up again, so
    `limit=2` on Segment Acks from a node loses exactly two of its acks. Beacons (no network PDU) are keyed by the
    rule's own count.
    """

    predicate: Callable[[Packet], bool]
    limit: int = 1
    name: str = "rule"
    picked: set[tuple[int, int, int]] = field(default_factory=set)
    beacons_dropped: int = 0

    def drops(self, packet: Packet) -> bool:
        if not self.predicate(packet):
            return False
        if packet.net is None:
            if self.beacons_dropped < self.limit:
                self.beacons_dropped += 1
                return True
            return False
        key = (packet.net.src, packet.net.iv_index, packet.net.seq)
        if key in self.picked:
            return True
        if len(self.picked) < self.limit:
            self.picked.add(key)
            return True
        return False

    @property
    def exhausted(self) -> bool:
        return len(self.picked) + self.beacons_dropped >= self.limit


@dataclass
class LossModel:
    """Seeded impairments of the advertising bearer (per transmission to one neighbour) and of the GATT link.

    `loss`: probability a transmission to one neighbour is lost; `duplicate`: probability it arrives twice;
    `reorder`: the largest extra random delay in seconds (PDUs overtake each other); `latency`: the delay of one
    radio hop; `gatt_loss`: probability a whole proxy PDU on a GATT link is lost; `rules`: targeted drops.
    """

    loss: float = 0.0
    duplicate: float = 0.0
    reorder: float = 0.0
    latency: float = 0.005
    gatt_latency: float = 0.0075
    gatt_loss: float = 0.0
    rules: list[DropRule] = field(default_factory=list)

    @property
    def perfect(self) -> bool:
        """Nothing is lost, doubled or reordered at random (targeted rules only drop, never reorder)."""
        return not (self.loss or self.duplicate or self.reorder or self.gatt_loss)

    def drop(
        self,
        predicate: Callable[[Packet], bool],
        limit: int = 1,
        name: str = "rule",
    ) -> DropRule:
        rule = DropRule(predicate, limit, name)
        self.rules.append(rule)
        return rule

    def ruled_out(self, packet: Packet) -> str | None:
        """The name of the rule that drops `packet`, if one does."""
        for rule in self.rules:
            if rule.drops(packet):
                return rule.name
        return None

    def lost(self, packet: Packet, rng: random.Random) -> str | None:
        """Why `packet` is lost (a rule's name, 'loss', 'gatt-loss'), None when it gets through."""
        if (name := self.ruled_out(packet)) is not None:
            return name
        if packet.bearer == "adv":
            return "loss" if self.loss and rng.random() < self.loss else None
        return "gatt-loss" if self.gatt_loss and rng.random() < self.gatt_loss else None
