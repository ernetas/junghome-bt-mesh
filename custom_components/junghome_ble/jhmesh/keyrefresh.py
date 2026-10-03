"""Following the provisioner's NetKey refresh only on proof that the mesh moved (review-4 D4).

The JUNG app refreshes the NetKey by sending every node a Config NetKey Update with the new key, then Config Key
Refresh Phase Set 2 and 3 (`docs/android/transport-provisioning.md` §4.2), each sealed with the node's device key.
The export gives Home Assistant every device key, so it can read them — but so can a node read the ones sealed with
*its own* device key, and forge them: one compromised node (or its extracted flash) sending itself NetKey Update
(its key), Phase Set 2, Phase Set 3 moved Home Assistant onto a key of the attacker's choice, dropped the real one and
persisted that, deaf and mute across restarts. An app that aborts a refresh (it does when a node lags) moved it too,
since the *requests* alone counted.

`KeyRefreshFollower` keeps the evidence and decides; it does no crypto and no I/O (`ProxyClient` feeds it what it
decoded and persists `record()`):

- a **candidate** key is learnt from a NetKey Update the caller checked came from a provisioner: addressed to a
  node's primary element, opened with *that* node's device key, from an address that is not a JUNG node's. It is
  harmless: Phase 1 only adds it to the keys we accept. Each node counts for the key last sent to it, so a forged
  Update moves only the forger's own vote, and up to `MAX_CANDIDATES` keys are kept at once.
- **confirmations**: a NetKey Status (success) or Key Refresh Phase Status (success) a node sealed with its own
  device key counts for the key that node was sent — a Phase Status reporting phase 2 for Phase 2; one reporting
  phase 0 after a Phase Set 3 to that node, from a node that confirmed holding the key, for Phase 3.
- **proof** moves the refresh on, never a request: a Secure Network beacon authenticated under the candidate, or a
  Mesh Private beacon it opens (`PROOF_BEACON`; Key Refresh flag set = Phase 2, clear = Phase 3, §3.10.4.1), Phase Status confirmations from at
  least two distinct nodes (`PROOF_STATUSES`) or from the proxy node itself (`PROOF_PROXY`). An export written mid
  key refresh is its own proof (`PROOF_EXPORT`).
- Phase 1 is **proven** the same way (review-4 D11): the candidate confirmed held — a NetKey Status or a Phase Status
  reporting phase 1 — by two distinct nodes or by the proxy node, the export's own refresh, or a proven Phase 2. It
  moves nothing here (the key was accepted already); it is what `distribution` waits for before Home Assistant hands
  the key to the nodes only it knows (`vaultrefresh`): a key one node made up is never sent anywhere.

The proxy node is trusted on its own word (its beacon, its status), as it is for everything the link carries: a
compromised proxy can still move the refresh; any other single node cannot.

No key we accept is dropped except at a proven Phase 3, and a proven Phase 2 stays until then (a later candidate
only joins the keys we accept). If the proxy sends no beacon on a phase change and the statuses are not heard, Home
Assistant stays a phase behind; that is safe: Phase 2 nodes still accept the old key, and a Phase 3 beacon moves it
on. The proof rule is unverified on air (no key refresh was run on the installation it was built against).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# a Secure Network beacon from the proxy authenticated under the new key, or a Mesh Private beacon the new key opens
PROOF_BEACON = "beacon"
PROOF_STATUSES = "statuses"  # Key Refresh Phase Status from two distinct nodes, each under its own device key
PROOF_PROXY = "proxy"  # Key Refresh Phase Status from the proxy node Home Assistant is connected to
PROOF_EXPORT = (
    "export"  # the export was written mid key refresh (`CDB.net_key_refresh`)
)
PROOFS = frozenset({PROOF_BEACON, PROOF_STATUSES, PROOF_PROXY, PROOF_EXPORT})

# candidate keys kept at once; when a new one comes, the one fewest nodes vouch for goes (the newest of those), so a
# node forging Update after Update cannot push out the key the provisioner sent everyone
MAX_CANDIDATES = 4

_PHASES = (1, 2, 3)


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != 16:
        raise ValueError("key refresh key is not 16 bytes")
    return key


def _nodes(value: Any, what: str) -> frozenset[int]:
    if not isinstance(value, list):
        raise TypeError(f"key refresh {what} is not a list")
    out = set()
    for entry in value:
        addr = int(entry, 16)
        if not 1 <= addr <= 0x7FFF:
            raise ValueError(
                f"key refresh {what} holds {addr:#06x}, not a unicast address"
            )
        out.add(addr)
    return frozenset(out)


@dataclass(frozen=True)
class KeyRefreshRecord:
    """A key refresh in progress (phase 1 or 2) or completed (3, until the export holds the new key), as stored.

    `proof` says how the phase was proven (one of `PROOFS`); None = not proven. A phase 1 needs none to be followed
    (the key is only accepted); one with a proof is the provisioner's key, which `distribution` hands out.
    `nodes` are the nodes the new key's NetKey Update was seen addressed to and `confirmed` maps a phase (1, 2, 3)
    to the nodes that confirmed it, so a restart keeps counting.
    """

    key: bytes = field(repr=False)  # key material: never in logs or error text
    phase: int
    proof: str | None = None
    nodes: frozenset[int] = frozenset()
    confirmed: Mapping[int, frozenset[int]] = field(default_factory=dict)

    @property
    def proven(self) -> bool:
        """Whether the phase was proven (a phase 1 needs no proof to be followed: the key is only accepted)."""
        return self.proof is not None

    def to_stored(self) -> dict[str, Any]:
        """Return the JSON form: `{"key", "phase"}` as before review 4, plus the proof fields when there are any."""
        stored: dict[str, Any] = {"key": self.key.hex(), "phase": self.phase}
        if self.proof is not None:
            stored["proof"] = self.proof
        if self.nodes:
            stored["nodes"] = [f"{n:04X}" for n in sorted(self.nodes)]
        confirmed = {
            str(p): [f"{n:04X}" for n in sorted(nodes)]
            for p, nodes in sorted(self.confirmed.items())
            if nodes
        }
        if confirmed:
            stored["confirmed"] = confirmed
        return stored

    @classmethod
    def from_stored(cls, d: Any) -> KeyRefreshRecord:
        """Parse `to_stored()` output; ValueError / KeyError / TypeError when it is not one (absent proof = none)."""
        if not isinstance(d, dict):
            raise TypeError("key refresh record is not an object")
        key = _check_key(bytes.fromhex(d["key"]))
        phase = d["phase"]
        if (
            isinstance(phase, bool)
            or not isinstance(phase, int)
            or phase not in _PHASES
        ):
            raise ValueError(f"key refresh phase {phase!r} is not 1, 2 or 3")
        proof = d.get("proof")
        if proof is not None and proof not in PROOFS:
            raise ValueError(f"key refresh proof {proof!r} is not one we know")
        confirmed_raw = d.get("confirmed") or {}
        if not isinstance(confirmed_raw, dict):
            raise TypeError("key refresh confirmations are not an object")
        confirmed: dict[int, frozenset[int]] = {}
        for p, nodes in confirmed_raw.items():
            if p not in ("1", "2", "3"):
                raise ValueError(f"key refresh confirmations name phase {p!r}")
            confirmed[int(p)] = _nodes(nodes, "confirmations")
        return cls(key, phase, proof, _nodes(d.get("nodes") or [], "nodes"), confirmed)


@dataclass(frozen=True)
class Moved:
    """The followed refresh moved: phase 1 (a new key learnt, or proven), 2 (transmitting with it) or 0 (complete).

    At 1 the key is only accepted; at 0 it is the only one. `proof` and the `nodes` whose statuses gave it are for
    the log; a phase 1 comes twice: when the key is learnt (no proof) and when the nodes prove it the provisioner's.
    """

    phase: int
    key: bytes = field(repr=False)
    proof: str | None = None
    nodes: tuple[int, ...] = ()


def describe_proof(moved: Moved) -> str:
    """How `moved` was proven, for the log (never the key)."""
    nodes = ", ".join(f"{n:04X}" for n in moved.nodes)
    if moved.phase == 1 and moved.proof == PROOF_STATUSES:
        return f"the new key confirmed by nodes {nodes}"
    if moved.phase == 1 and moved.proof == PROOF_PROXY:
        return f"the new key confirmed by the proxy node {nodes}"
    if moved.proof == PROOF_BEACON:
        return "the proxy's beacon under the new key"
    if moved.proof == PROOF_STATUSES:
        return f"Key Refresh Phase Status from nodes {nodes}"
    if moved.proof == PROOF_PROXY:
        return f"Key Refresh Phase Status from the proxy node {nodes}"
    return "none"


@dataclass
class _Candidate:
    # learning order: the newest of the least vouched-for goes first when full
    order: int
    # never evicted: the export's own refresh, or one the mesh was proven to use (only proofs add these)
    kept: bool = False
    # how its Phase 2 was proven, once it was
    proof: str | None = None
    # how it was proven the provisioner's key (Phase 1), once it was: `distribution` hands out no other
    held: str | None = None
    confirmed: dict[int, set[int]] = field(
        default_factory=lambda: {p: set() for p in _PHASES}
    )


class KeyRefreshFollower:
    """The NetKey state of one subnet through a key refresh: what we transmit with, what we accept, and the evidence."""

    def __init__(self, current: bytes) -> None:
        """Start in normal operation on `current`, the key the export (or a proven refresh) gave."""
        self.current = _check_key(current)
        self._candidates: dict[bytes, _Candidate] = {}
        # node -> key of the last NetKey Update seen addressed to it
        self._updated: dict[int, bytes] = {}
        # nodes a Phase Set 3 was seen addressed to
        self._revoking: set[int] = set()
        # the candidate proven at Phase 2: transmitted with
        self._switched: bytes | None = None
        # the key statuses of nodes we saw no NetKey Update to count for: the export's own refresh, or a proven one
        self._trusted: bytes | None = None
        # a proven completion the export does not hold yet
        self._done: KeyRefreshRecord | None = None
        self._learnt = 0

    # ------------------------------------------------------------------ what the client uses
    @property
    def phase(self) -> int:
        """0 = normal operation, 1 = new keys accepted only, 2 = transmitting with the proven new key."""
        if self._switched is not None:
            return 2
        return 1 if self._candidates else 0

    @property
    def tx_key(self) -> bytes:
        """The key we transmit with: the proven Phase 2 key, else the current one (§3.10.4.1)."""
        return self._switched if self._switched is not None else self.current

    @property
    def rx_keys(self) -> tuple[bytes, ...]:
        """Every key we accept: the current one first, then the candidates in the order they were learnt."""
        return (self.current, *self._candidates)

    @property
    def new_key(self) -> bytes | None:
        """The candidate the refresh is heading for: the proven Phase 2 key, else the one most nodes vouch for."""
        if self._switched is not None:
            return self._switched
        if not self._candidates:
            return None
        return max(
            self._candidates,
            key=lambda k: (self._support(k), self._candidates[k].order),
        )

    @property
    def distribution(self) -> tuple[int, bytes] | None:
        """The phase the nodes only Home Assistant knows may be taken to, and the new key: proven only (review-4 D11).

        2 with the proven Phase 2 key; 1 with a candidate proven the provisioner's (`held`; the one most nodes vouch
        for, should there be several); 3 with the key of a proven completion the export does not hold yet; None when
        there is no refresh or nothing about it was proven. A candidate learnt from one node's word alone is never
        handed out.
        """
        if self._switched is not None:
            return 2, self._switched
        held = [k for k, c in self._candidates.items() if c.held is not None]
        if held:
            return 1, max(
                held, key=lambda k: (self._support(k), self._candidates[k].order)
            )
        if self._done is not None:
            return 3, self._done.key
        return None

    def record(self) -> KeyRefreshRecord | None:
        """Return what to persist: the refresh in progress (its `new_key`), else a completion the export lacks.

        A completion the export lacks wins over a later refresh still in Phase 1: a restart then keeps transmitting
        with the completed key (losing an only-accepted candidate) rather than going back to the export's revoked
        one. From a proven Phase 2 on, the later refresh is what a restart needs.
        """
        key = self.new_key
        if key is None or (self._done is not None and self._switched is None):
            return self._done
        c = self._candidates[key]
        switched = key == self._switched
        return KeyRefreshRecord(
            key,
            2 if switched else 1,
            c.proof if switched else c.held,
            frozenset(self._targets(key)),
            {p: frozenset(nodes) for p, nodes in c.confirmed.items() if nodes},
        )

    # ------------------------------------------------------------------ evidence
    def learn(self, key: bytes, node: int) -> Moved | None:
        """Note a provisioner's NetKey Update of `key` addressed to `node` (the caller checked who sealed it).

        Returns `Moved(1)` when a new key joins while nothing is switched yet; a key learnt during a proven Phase 2
        is only accepted (`rx_keys`), the switched one stays.
        """
        _check_key(key)
        if key == self.current:
            return None  # the app re-sending what the node has: nothing to follow
        self._updated[node] = key
        if key in self._candidates:
            return None
        self._add(key)
        return Moved(1, key) if self._switched is None else None

    def requested(self, node: int, transition: int) -> None:
        """Note a provisioner's Key Refresh Phase Set addressed to `node`: a request, never a proof by itself."""
        if transition == 3:
            self._revoking.add(node)

    def netkey_status(
        self, node: int, ok: bool, proxy: int | None = None
    ) -> Moved | None:
        """Note a NetKey Status `node` sealed with its own device key: success = it holds the key it was sent.

        Returns `Moved(1)` with its proof when this status proves the key the provisioner's (`distribution`;
        `proxy`: the proxy node, whose word alone is enough).
        """
        key = self._attributed(node)
        if not ok or key is None:
            return None
        self._candidates[key].confirmed[1].add(node)
        return self._check(key, proxy)

    def phase_status(self, node: int, phase: int, proxy: int | None) -> Moved | None:
        """Count a successful Key Refresh Phase Status `node` sealed with its own device key (`proxy`: the proxy)."""
        key = self._attributed(node)
        if key is None:
            return None
        c = self._candidates[key]
        if phase in (1, 2):
            c.confirmed[phase].add(node)
        elif (
            phase == 0
            and node in self._revoking
            and (key == self._trusted or node in c.confirmed[1] | c.confirmed[2])
        ):
            # back to normal operation after a Phase Set 3 to a node that held the key: it revoked the old one (a node
            # that never took the key answers phase 0 too, hence "held")
            c.confirmed[3].add(node)
        return self._check(key, proxy)

    def beacon(self, key: bytes, key_refresh: bool) -> Moved | None:
        """Take a beacon authenticated under candidate `key` as proof: Phase 2, or 3 with the flag clear.

        A Secure Network beacon whose MAC verified under `key`, or a Mesh Private beacon `key` opened (Mesh Protocol
        1.1 §3.10.4, unverified on air): both carry the same Key Refresh flag, and only the key's holder makes either.
        """
        if key not in self._candidates:
            return None
        if not key_refresh:
            return self._complete(key, PROOF_BEACON, ())
        if key == self._switched:
            return None
        return self._switch(key, PROOF_BEACON, ())

    # ------------------------------------------------------------------ resuming
    @classmethod
    def resume(
        cls,
        current: bytes,
        exported: tuple[bytes, int] | None,
        stored: KeyRefreshRecord | None,
    ) -> tuple[KeyRefreshFollower, bool]:
        """Take up the refresh the export was written in, then the one `stored` followed.

        `exported`: the export's new key and phase (`current` is then its old key). Returns the follower and whether
        `stored` is still worth keeping. A stored Phase 2 or 3 counts only with its proof: without it (forged, or written before proofs were kept) the
        key is a candidate again, accepted and not used, until a beacon or statuses prove it — after a refresh that
        really completed, the first beacon of the proxy does.
        """
        f = cls(current)
        if exported is not None:
            new, phase = exported
            f._add(new, kept=True)
            f._trusted = new
            f._candidates[new].held = PROOF_EXPORT
            if phase == 2:
                f._switched = new
                f._candidates[new].proof = PROOF_EXPORT
        if stored is None:
            return f, True
        if stored.key == current:
            # the export has the new key already: the refresh is behind us (phase 3: the caller may have put the new
            # key in for the export's stale one — keep the record for the next setup)
            return f, stored.phase == 3
        if stored.phase == 3 and stored.proven:
            done = cls(stored.key)
            done._done = stored
            return done, True
        if stored.key not in f._candidates:
            f._add(stored.key)
        c = f._candidates[stored.key]
        for node in stored.nodes:
            f._updated.setdefault(node, stored.key)
        for phase, nodes in stored.confirmed.items():
            c.confirmed[phase] |= nodes
        if stored.phase == 2 and stored.proven:
            f._switched = f._trusted = stored.key
            c.proof, c.kept = stored.proof, True
            c.held = c.held or stored.proof
        elif stored.phase == 1 and stored.proven:
            c.held, c.kept = c.held or stored.proof, True
        return f, True

    # ------------------------------------------------------------------ internals
    def _targets(self, key: bytes) -> Iterable[int]:
        return (n for n, k in self._updated.items() if k == key)

    def _support(self, key: bytes) -> int:
        c = self._candidates[key]
        return len(set(self._targets(key)).union(*c.confirmed.values()))

    def _add(self, key: bytes, *, kept: bool = False) -> None:
        evictable = [k for k, c in self._candidates.items() if not c.kept]
        if len(self._candidates) >= MAX_CANDIDATES and evictable:
            gone = min(
                evictable, key=lambda k: (self._support(k), -self._candidates[k].order)
            )
            del self._candidates[gone]
            self._updated = {n: k for n, k in self._updated.items() if k != gone}
        self._learnt += 1
        self._candidates[key] = _Candidate(self._learnt, kept=kept)

    def _attributed(self, node: int) -> bytes | None:
        """Return the candidate a status of `node` speaks for: the key last sent to it, else the trusted one."""
        key = self._updated.get(node, self._trusted)
        return key if key in self._candidates else None

    def _check(self, key: bytes, proxy: int | None) -> Moved | None:
        c = self._candidates[key]
        for phase in (3, 2, 1):
            nodes = c.confirmed[phase]
            by: tuple[int, ...]
            if proxy is not None and proxy in nodes:
                proof, by = PROOF_PROXY, (proxy,)
            elif len(nodes) >= 2:
                proof, by = PROOF_STATUSES, tuple(sorted(nodes))
            else:
                continue
            if phase == 3:
                return self._complete(key, proof, by)
            if phase == 2 and key != self._switched:
                return self._switch(key, proof, by)
            if phase == 1 and c.held is None:
                c.held, c.kept = proof, True
                return Moved(1, key, proof, by)
        return None

    def _switch(self, key: bytes, proof: str, by: tuple[int, ...]) -> Moved:
        self._switched = self._trusted = key
        c = self._candidates[key]
        c.proof, c.kept = proof, True
        c.held = c.held or proof  # Phase 2 proves Phase 1 too
        return Moved(2, key, proof, by)

    def _complete(self, key: bytes, proof: str, by: tuple[int, ...]) -> Moved:
        confirmed = self._candidates[key].confirmed
        # kept until the export holds the new key: a restart would otherwise go back to the old one
        self._done = KeyRefreshRecord(
            key,
            3,
            proof,
            frozenset(self._targets(key)),
            {p: frozenset(n) for p, n in confirmed.items() if n},
        )
        self.current = key
        self._candidates.clear()
        self._updated.clear()
        self._revoking.clear()
        self._switched = self._trusted = None
        return Moved(0, key, proof, by)
