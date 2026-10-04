"""What a proxy link carried and dropped, counted.

`ProxyClient` keeps one `LinkStats` per link (`ProxyClient.link_stats`, started over by every `attach`) and their sum
since it was made (`ProxyClient.total_stats`). The counts are for the application to show — the HA integration's
diagnostics, a CLI summary — and for its own checks: a link that forwards only PDUs our keys cannot open is a mesh
whose keys changed, one whose requests go unanswered while other traffic flows is a mesh that discards our PDUs.

The per-PDU log lines behind these counts go to the `jhmesh.trace` logger, a child of `jhmesh`: traffic can be logged
on its own, or left out of a `jhmesh` debug log by setting `jhmesh.trace` to INFO.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

__all__ = ["LinkStats"]


@dataclass
class LinkStats:
    """Counts of one proxy link, or of several summed (`+`).

    Received: `rx` network PDUs the proxy delivered whole, of which `undecryptable` our keys could not open (network
    or upper transport) and `replays_dropped` the replay protection dropped (§3.8.8); `messages` access messages
    decoded and handed out, `messages_to_us` of them unicast to our address (proof that the nodes take our PDUs);
    `beacons_unauthenticated` Secure Network beacons our keys did not authenticate (with `undecryptable`, the sign of
    an export whose keys are stale); `garbage` proxy PDUs dropped before authentication for their size;
    `proxy_config_dropped` proxy configuration PDUs dropped (a wrong header, not ours, or a replay), of which
    `proxy_config_replays` replays.

    Sent: `tx` network and proxy configuration PDUs written; `segment_retransmissions` segments sent again because
    their acknowledgement did not cover them; `request_timeouts` request attempts that went unanswered within their
    timeout (each retry of a request counts).
    """

    tx: int = 0
    rx: int = 0
    messages: int = 0
    messages_to_us: int = 0
    undecryptable: int = 0
    beacons_unauthenticated: int = 0
    garbage: int = 0
    replays_dropped: int = 0
    segment_retransmissions: int = 0
    request_timeouts: int = 0
    proxy_config_dropped: int = 0
    proxy_config_replays: int = 0

    def __add__(self, other: LinkStats) -> LinkStats:
        """Sum two links' counts, field by field."""
        return LinkStats(
            **{
                f.name: getattr(self, f.name) + getattr(other, f.name)
                for f in fields(self)
            }
        )
