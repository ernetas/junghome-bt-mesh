#!/usr/bin/env python3
"""Proof of concept: talk to the JUNG HOME mesh through any proxy node using the keys exported by the app.

    .venv/bin/python tools/mesh_poc.py scan [--adv]                 # proxies of our network (+ advertisement details)
    .venv/bin/python tools/mesh_poc.py listen --seconds 60 [--src 0293] [--dst C005]
    .venv/bin/python tools/mesh_poc.py get  <addr|group name>
    .venv/bin/python tools/mesh_poc.py set  <addr|group name> on|off          # also true/false, 1/0
    .venv/bin/python tools/mesh_poc.py blink <addr>            # get → toggle → restore
    .venv/bin/python tools/mesh_poc.py lightness <addr> [0-65535]
    .venv/bin/python tools/mesh_poc.py ctl <addr> <lightness> <kelvin>
    .venv/bin/python tools/mesh_poc.py ctlrange <addr>         # Light CTL Temperature Range Get (0x8262)
    .venv/bin/python tools/mesh_poc.py scene <addr|group> <number>
    .venv/bin/python tools/mesh_poc.py prop get  <addr> <name|ID> [--server admin|manufacturer|user|sig_admin|...]
    .venv/bin/python tools/mesh_poc.py prop set  <addr> <name|ID> <value> [--unack] [--access N] [--server ...]
                                                                   # a group target also needs --yes
    .venv/bin/python tools/mesh_poc.py prop list <product id> [--all]   # the catalogue (from the app / firmware)
    .venv/bin/python tools/mesh_poc.py prop lists <addr>               # what the element really serves (on air)
    .venv/bin/python tools/mesh_poc.py config get-composition <node>
    .venv/bin/python tools/mesh_poc.py config publication <node> <element> <model> [<group> --yes]  # Get without a group
    .venv/bin/python tools/mesh_poc.py config subscribe|unsubscribe <node> <element> <model> <group> --yes|--dry-run
    .venv/bin/python tools/mesh_poc.py config subscriptions <node> <element> <model>
    .venv/bin/python tools/mesh_poc.py config bind|unbind <node> <element> <model> --yes|--dry-run
    .venv/bin/python tools/mesh_poc.py config audit <node>            # export vs what the node holds (read-only)
    .venv/bin/python tools/mesh_poc.py export write <file> [--out <path>]  # round trip through ProjectFile, diff
    .venv/bin/python tools/mesh_poc.py provision --scan                    # unprovisioned devices (0x1827)
    .venv/bin/python tools/mesh_poc.py provision <uuid> --unicast 0D10 --yes   # PB-GATT, No OOB; device key to a 0600 file

Addresses, ids and model ids are hex (`0149`, `5003`, `05271013`; `0x` optional); property names are the
snake_case identifiers of `jhmesh.properties` (`key_mode`); groups also by name. `listen` prints every decoded
message with a millisecond timestamp and the property values decoded by the codec catalogue.

The Config writes (publication Set, subscribe, unsubscribe, bind, unbind) change the node at once and leave the
export as it was, and a property Set to a group changes every member: each needs `--yes`, and a Config write ends
with a reminder that `config audit` shows what now differs from the export.

Our own identity (unicast address + sequence number) lives in tools/.jhmesh_state_<ADDR>.json, one file per
source address (--source, default 7FFF). The HA integration uses 0D00 with its own store: two clients must never
share an address, because replay protection is per (source address, sequence number) and the nodes silently drop
whichever side's counter lags. The address must also be free in the export: no node's, not in networkExclusions and
outside every provisioner's allocated range (the app provisions there).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jhmesh import audit
from jhmesh import config_messages as C
from jhmesh import messages as M
from jhmesh import vendor_models as V
from jhmesh.cdb import CDB
from jhmesh.client import (
    AccessMessage,
    LocalState,
    ProxyCandidate,
    ProxyClient,
    StateInUse,
)
from jhmesh.devices import Devices, Metadata, build_devices
from jhmesh.pdu import is_unicast
from jhmesh.provisioning import Capabilities, ProvisioningData, ProvisioningError
from jhmesh.standalone import (
    StandaloneLink,
    connect,
    provision_device,
    scan_for_proxies,
    scan_unprovisioned,
)
from tools import cli_ops as ops

Handler = Callable[[ProxyClient, CDB], Awaitable[None]]  # what runs once the link is up
Command = Callable[[argparse.Namespace], Coroutine[Any, Any, None]]

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "ios/AppDomain-de.jung.junghome"
DEFAULT_CDB = APP_DIR / "Documents/MeshNetwork.json"
STATE_DIR = Path(__file__).resolve().parent
LEGACY_STATE = (
    STATE_DIR / ".jhmesh_state.json"
)  # the old single-file layout (address 0D00)
# 0D00 is the HA integration's default (custom_components/junghome_ble/const.py DEFAULT_UNICAST) and it keeps its
# own sequence counter in HA's storage, so the CLI refuses it: two clients on one address reuse nonces and get
# dropped by every node's replay list. The app gives each new provisioner (a second app user) the next free tenth of
# the unicast space from the bottom up (MeshNetworkRepositoryImpl.h3 / j3, nRF Mesh getNextAvailableUnicastRange):
# the first phone has 0001-0CCC, a second user the block from 0CCD, which holds 0D00 and the CLI's old default 0D01
# (so does Home Assistant's own provisioner range, 256 addresses from 0D00). Only a tenth app user's block reaches
# the top address; `source_problem` refuses it too once the export reserves it.
HA_SOURCE = 0x0D00
DEFAULT_SOURCE = 0x7FFF
ONOFF_VALUES = (
    "on",
    "off",
    "true",
    "false",
    "1",
    "0",
)  # `set`: anything else is an error, never "off"


def device_key_path(unicast: int) -> Path:
    """Where `provision` writes a new node's device key: tools/.jhmesh_devkey_<ADDR>.json (owner-only, never committed)."""
    return STATE_DIR / f".jhmesh_devkey_{unicast:04X}.json"


def state_path(src: int) -> Path:
    """Sequence-number store for one source address: tools/.jhmesh_state_<ADDR>.json.

    A counter belongs to exactly one address (replay protection is per source + SeqAuth), so switching
    --source must never reuse another address's file. The pre-suffix file is adopted once if it was
    written for the requested address, so an existing counter is not thrown away.
    """
    path = STATE_DIR / f".jhmesh_state_{src:04X}.json"
    if not path.exists() and LEGACY_STATE.exists():
        try:
            if int(json.loads(LEGACY_STATE.read_text())["src"], 16) == src:
                LEGACY_STATE.rename(path)
        except (ValueError, KeyError, TypeError, OSError):
            pass
    return path


def load_devices(cdb: CDB) -> Devices:
    aps = APP_DIR / "Library/Application Support"
    return build_devices(
        cdb, Metadata(aps / "device_metadata.json", aps / "scene_metadata.json")
    )


def print_scan(cands: list[ProxyCandidate], show_adv: bool) -> None:
    for c in cands:
        print(
            f"{c.rssi:5d} dBm  {c.kind:13s} {c.address}  {c.name or ''}"
            + (f"  node {c.node_addr:04X}" if c.node_addr else "")
        )
        if show_adv and c.adv is not None:
            # what a narrower Home Assistant bluetooth matcher could key on (docs/cross-repo-analysis.md §5)
            mfr = {
                f"0x{cid:04X}": data.hex()
                for cid, data in (
                    getattr(c.adv, "manufacturer_data", None) or {}
                ).items()
            }
            print(
                f"           local_name={getattr(c.adv, 'local_name', None)!r}"
                f" manufacturer_data={mfr or '{}'}"
                f" service_uuids={list(getattr(c.adv, 'service_uuids', None) or [])}"
            )


def config_writes(args: argparse.Namespace) -> bool:
    """Tell whether a `config` invocation changes the node (a Set, Add, Delete, Bind or Unbind) rather than reads."""
    if args.cmd != "config":
        return False
    return args.config_cmd in {"subscribe", "unsubscribe", "bind", "unbind"} or (
        args.config_cmd == "publication" and args.group is not None
    )


def refuse_unconfirmed_write(args: argparse.Namespace, cdb: CDB) -> None:
    """Exit before connecting when a write that is hard to take back was not confirmed with `--yes`.

    A Config write changes the node's wiring at once and nothing records it in the export (the app, the gateway and
    Home Assistant keep working from the old one); a property Set to a group changes every member in one message.
    A typo in either is a live change, so both are spelt out first (`config ... --dry-run` shows the message).
    """
    if getattr(args, "yes", True):
        return
    if config_writes(args):
        sys.exit(
            f"config {args.config_cmd} changes the node's configuration now and does not update the export:"
            " add --yes to send it (--dry-run prints the message instead)"
        )
    if args.cmd == "prop" and not is_unicast(
        dst := ops.resolve_or_exit(cdb, args.target)
    ):
        sys.exit(
            f"prop set to group {cdb.label(dst)} writes the property on every element subscribed to it:"
            " add --yes to send it"
        )


def source_problem(cdb: CDB, src: int) -> str | None:
    """Why `src` cannot be our source address; None when it can.

    What Home Assistant refuses or warns about for its own (review-3 W3, `CDB.unicast_is_free`): a node's element
    (the nodes drop whichever of us lags as a replay, and replies go astray), an address in `networkExclusions` (a
    removed node's: the nodes' replay lists still hold its sequence numbers until the IV index moved on twice), and
    one inside a provisioner's allocated range (the app provisions its next node there, or takes it for a phone).
    """
    used = cdb.used_unicasts()
    if src in used:
        return f"our address {src:04X} collides with a node in the CDB"
    if cdb.unicast_is_free(src, used):
        return None
    if src in cdb.excluded_addresses:
        why = "is in the export's networkExclusions (a removed node's, still in the nodes' replay lists)"
    else:
        low, high = next(
            (low, high)
            for low, high in cdb.provisioner_unicast_ranges
            if low <= src <= high
        )
        why = f"is inside a provisioner's allocated range {low:04X}-{high:04X} (the app provisions there)"
    # from the top down, where the app allocates last (DEFAULT_SOURCE)
    free = (a for a in range(0x7FFF, 0, -1) if cdb.unicast_is_free(a, used))
    hint = next(
        (f"pass --source {a:04X}" for a in free if a != HA_SOURCE), "no address is free"
    )
    return f"our address {src:04X} {why}: {hint}"


async def with_client(args: argparse.Namespace, fn: Handler | None) -> None:
    """Load the export, take our identity (refusing an address that is not free), connect and run `fn` (None: `scan`)."""
    if args.source == HA_SOURCE:
        sys.exit(
            f"{HA_SOURCE:04X} is the Home Assistant integration's own address (its sequence counter lives in HA's"
            f" storage, not here): pass --source {DEFAULT_SOURCE:04X} or another unused unicast"
        )
    cdb = ops.load_cdb(args.cdb)
    # before a state file exists for an address we cannot use
    if problem := source_problem(cdb, args.source):
        sys.exit(problem)
    # every command resolves these inside `fn`, once connected: a typo must not cost a real BLE connection first
    for text in (getattr(args, "target", None), getattr(args, "group", None)):
        if text is not None:
            ops.resolve_or_exit(cdb, text)
    refuse_unconfirmed_write(args, cdb)
    try:
        state = LocalState(
            state_path(args.source), args.source, configured_src_wins=True
        )
    except StateInUse as err:
        sys.exit(
            f"{err}\nanother mesh_poc.py is running as {args.source:04X}: wait for it, or pass a different"
            " --source (each address keeps its own counter)"
        )
    client = ProxyClient(cdb, state)
    if args.cmd == "scan":
        print_scan(await scan_for_proxies(client, args.scan), args.adv)
        return
    assert fn is not None
    if args.cmd == "listen":
        link = StandaloneLink(client, args.scan)  # reconnects if the proxy drops
        await link.start()
        try:
            await link.wait_connected()
            await fn(client, cdb)
        finally:
            await link.stop()
        return
    await connect(client, scan_seconds=args.scan)
    try:
        await fn(client, cdb)
    finally:
        await client.detach()


async def cmd_listen(args: argparse.Namespace) -> None:
    src = ops.parse_hex(args.src) if args.src else None
    dst = ops.parse_hex(args.dst) if args.dst else None

    def show(m: AccessMessage) -> None:
        if ops.message_matches(m, src, dst):
            print(ops.format_message(m, datetime.now()), flush=True)  # noqa: DTZ005  # local wall clock, like the log

    async def run(client: ProxyClient, cdb: CDB) -> None:
        client.on_message = show
        if not args.verbose:  # the INFO log would print every message a second time
            logging.getLogger("jhmesh").setLevel(logging.WARNING)
        print(
            f"listening for {args.seconds}s as {client.state.src:04X} (ms timestamps; property values decoded) …",
            file=sys.stderr,
        )
        await asyncio.sleep(args.seconds)

    await with_client(args, run)


def _onoff_report(cdb: CDB, msgs: list[AccessMessage]) -> None:
    for m in msgs:
        print(f"  {cdb.label(m.src)}: {'ON' if m.params[0] else 'OFF'}")


async def cmd_get(args: argparse.Namespace) -> None:
    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        if is_unicast(dst):
            m = await client.request(dst, M.generic_onoff_get(), M.GEN_ONOFF_STATUS)
            _onoff_report(cdb, [m])
        else:
            _onoff_report(
                cdb,
                await client.collect(
                    dst, M.generic_onoff_get(), M.GEN_ONOFF_STATUS, window=args.window
                ),
            )

    await with_client(args, run)


async def cmd_set(args: argparse.Namespace) -> None:
    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        on = args.value in ("on", "true", "1")  # the parser admits ONOFF_VALUES only
        pdu = M.generic_onoff_set(
            on, ack=not args.unack, transition=0 if args.t0 else None
        )
        if args.unack:
            await client.send_access(dst, pdu)
            await asyncio.sleep(args.window)
            return
        if is_unicast(dst):
            m = await client.request(
                dst, pdu, M.GEN_ONOFF_STATUS, timeout=args.timeout, retries=args.retries
            )
            _onoff_report(cdb, [m])
        else:
            _onoff_report(
                cdb,
                await client.collect(dst, pdu, M.GEN_ONOFF_STATUS, window=args.window),
            )

    await with_client(args, run)


async def cmd_blink(args: argparse.Namespace) -> None:
    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        assert is_unicast(dst), "blink needs a unicast element address"
        cur = (
            await client.request(dst, M.generic_onoff_get(), M.GEN_ONOFF_STATUS)
        ).params[0]
        print(
            f"  {cdb.label(dst)} is {'ON' if cur else 'OFF'}; toggling for {args.hold}s"
        )
        m = await client.request(dst, M.generic_onoff_set(not cur), M.GEN_ONOFF_STATUS)
        print(
            f"  status after set: {'ON' if m.params[0] else 'OFF'}"
            + (
                f" target={'ON' if m.params[1] else 'OFF'}"
                if len(m.params) >= 3
                else ""
            )
        )
        await asyncio.sleep(args.hold)
        m = await client.request(
            dst, M.generic_onoff_set(bool(cur)), M.GEN_ONOFF_STATUS
        )
        print(
            f"  restored: {'ON' if m.params[0] else 'OFF'}"
            + (
                f" target={'ON' if m.params[1] else 'OFF'}"
                if len(m.params) >= 3
                else ""
            )
        )

    await with_client(args, run)


async def cmd_lightness(args: argparse.Namespace) -> None:
    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        if args.value is None:
            m = await client.request(
                dst, M.light_lightness_get(), M.LIGHT_LIGHTNESS_STATUS
            )
        else:
            m = await client.request(
                dst, M.light_lightness_set(args.value), M.LIGHT_LIGHTNESS_STATUS
            )
        print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")

    await with_client(args, run)


async def cmd_ctl(args: argparse.Namespace) -> None:
    try:
        pdu = M.light_ctl_set(args.lightness, args.kelvin)
    except (
        ValueError
    ) as err:  # the codec's range (800..20000 K): an error line, not a traceback
        sys.exit(str(err))

    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        m = await client.request(dst, pdu, M.LIGHT_CTL_STATUS)
        print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")

    await with_client(args, run)


async def cmd_ctlrange(args: argparse.Namespace) -> None:
    """Light CTL Temperature Range Get (0x8262): tracker §8 wants it tried on a DALI node."""

    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        m = await client.request(
            dst,
            M.light_ctl_temperature_range_get(),
            M.LIGHT_CTL_TEMP_RANGE_STATUS,
            timeout=args.timeout,
            retries=args.retries,
        )
        print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")

    await with_client(args, run)


async def cmd_scene(args: argparse.Namespace) -> None:
    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        if is_unicast(dst):
            m = await client.request(dst, M.scene_recall(args.number), M.SCENE_STATUS)
            print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")
        else:
            for m in await client.collect(
                dst,
                M.scene_recall(args.number),
                M.SCENE_STATUS,
                window=args.window,
            ):
                print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")

    await with_client(args, run)


async def cmd_scene_actions(args: argparse.Namespace) -> None:
    """What an element does in each of its scenes (JUNG Scene Action Setup; the export does not hold this)."""

    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        numbers = [args.number] if args.number is not None else None
        if numbers is None:
            m = await client.request(
                dst,
                V.scene_action_get(),
                V.SCENE_ACTION_SETUP_STATUS,
                expect_cid=M.JUNG_CID,
                timeout=args.timeout,
                retries=args.retries,
            )
            on_node = list(V.decode_scene_action_status(m.params).scenes or ())
            in_export = sorted(n for n, members in cdb.scenes.items() if dst in members)
            print(
                f"  {cdb.label(dst)}: scenes with an action {on_node}, in the export {in_export}"
            )
            numbers = sorted(set(on_node) | set(in_export))
        for n in numbers:
            try:
                m = await client.request(
                    dst,
                    V.scene_action_get(n),
                    V.SCENE_ACTION_SETUP_STATUS,
                    expect_cid=M.JUNG_CID,
                    timeout=args.timeout,
                    retries=1,
                    quiet=True,
                )
            except TimeoutError:
                print(f"  scene {n}: no answer")
                continue
            print(
                "  "
                + ops.scene_action_text(cdb, V.decode_scene_action_status(m.params))
            )

    await with_client(args, run)


async def cmd_sched(args: argparse.Namespace) -> None:
    """The JH Scheduler slots of an element: the 16-slot summary, then every used slot (or `--slot N`) in full."""

    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        if args.slot is None:
            m = await client.request(
                dst,
                V.scheduler_list_get(args.central_id),
                V.JH_SCHEDULER_STATUS,
                expect_cid=M.JUNG_CID,
                timeout=args.timeout,
                retries=args.retries,
            )
            status = V.decode_scheduler_status(m.params)
            print(f"  {cdb.label(dst)}: {status.describe()}")
            slots = [i for i, code in enumerate(status.slots or ()) if code != 0]
        else:
            slots = [args.slot]
        for i in slots:
            for sub in (V.SUB_SCHEDULE, V.SUB_ACTION, V.SUB_EFFECTIVE_TIME):
                try:
                    m = await client.request(
                        dst,
                        V.scheduler_get(i, sub),
                        V.JH_SCHEDULER_STATUS,
                        expect_cid=M.JUNG_CID,
                        timeout=args.timeout,
                        retries=1,
                        quiet=True,
                    )
                except TimeoutError:
                    print(f"  slot {i} {V.SUB_NAMES[sub]}: no answer")
                    continue
                print("  " + V.decode_scheduler_status(m.params).describe())

    await with_client(args, run)


async def cmd_health(args: argparse.Namespace) -> None:
    """Registered Health faults of a node (or of every node behind a group address); `--clear`, `--test N` first."""

    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        if args.clear:
            # the acknowledged Clear (0x802F): the form this command always put on the air, and the one seen to
            # clear (`docs/hidden-features.md` §10); whether it is answered is not known, so read back instead
            await client.send_access(dst, M.health_fault_clear())
            print("  faults cleared (reading back)")
        if args.test is not None:
            m = await client.request(
                dst,
                M.health_fault_test(args.test),
                M.HEALTH_FAULT_STATUS,
                timeout=args.timeout,
                retries=args.retries,
            )
            print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")
        if is_unicast(dst):
            m = await client.request(
                dst,
                M.health_fault_get(),
                M.HEALTH_FAULT_STATUS,
                timeout=args.timeout,
                retries=args.retries,
            )
            replies = [m]
        else:
            replies = await client.collect(
                dst, M.health_fault_get(), M.HEALTH_FAULT_STATUS, window=args.window
            )
        for m in sorted(replies, key=lambda r: r.src):
            print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")

    await with_client(args, run)


async def cmd_config_hops(args: argparse.Namespace) -> None:
    """How many mesh hops lie between two nodes: `source` beats to all-nodes for a moment, `node` counts (both restored)."""

    async def run(client: ProxyClient, cdb: CDB) -> None:
        probe = ops.HopProbe(
            _node_of(cdb, args.node),
            _node_of(cdb, args.origin),
            args.beats,
            args.period,
        )
        before = C.decode_heartbeat_publication_status(
            (await _config_reply(client, probe.read_publication, args)).params
        )
        await _config_exchange(client, cdb, probe.subscribe, args)
        try:
            await _config_exchange(client, cdb, probe.publish, args)
            print(f"  waiting {probe.wait_seconds:.0f} s for the beats …", flush=True)
            await asyncio.sleep(probe.wait_seconds)
            status = C.decode_heartbeat_subscription_status(
                (await _config_reply(client, probe.read_subscription, args)).params
            )
            print("  " + ops.hops_text(cdb, probe, status))
        finally:
            # whatever ended the wait (Ctrl-C, a lost link, a silent node): the origin's Heartbeat Publication is
            # what HA's availability tracking set — put it back, then switch the counter's subscription off
            await _restore_hop_probe(client, cdb, probe, before, args)

    await with_client(args, run)


async def _restore_hop_probe(
    client: ProxyClient,
    cdb: CDB,
    probe: ops.HopProbe,
    before: C.HeartbeatPublicationStatus,
    args: argparse.Namespace,
) -> None:
    """Undo a hop probe; a failure is reported (with what to do) rather than raised over the original error."""
    try:
        await _config_exchange(client, cdb, probe.restore_publication(before), args)
    except (TimeoutError, ConnectionError) as err:
        print(
            f"  !! {cdb.label(probe.source)}: Heartbeat Publication NOT restored ({err}) — re-run"
            f" `config hops {probe.subscriber:04X} {probe.source:04X}` or set it again from Home Assistant",
            file=sys.stderr,
        )
    try:
        await _config_exchange(client, cdb, probe.unsubscribe, args)
    except (TimeoutError, ConnectionError) as err:
        print(
            f"  !! {cdb.label(probe.subscriber)}: Heartbeat Subscription not switched off ({err});"
            f" it expires by itself after {probe.wait_seconds:.0f} s",
            file=sys.stderr,
        )


async def cmd_config_hopmatrix(args: argparse.Namespace) -> None:
    """Hops between every pair of nodes: each node in turn beats to all-nodes while every other node counts.

    Per origin: read its Heartbeat Publication (to put it back), set a Heartbeat Subscription for it on every other
    node, make it beat `--beats` times every `--period` s with InitTTL 127, wait, read every subscription (count,
    min..max hops), restore the publication, switch the subscriptions off. Config exchanges run `--parallel` at a
    time. The gateway is a node like the others; a node that does not answer is skipped for that phase.
    """

    async def run(client: ProxyClient, cdb: CDB) -> None:
        nodes = [n.unicast for n in cdb.nodes if n.pid is not None]
        if args.nodes:
            wanted = {_node_of(cdb, a) for a in args.nodes}
            nodes = [n for n in nodes if n in wanted]
        sem = asyncio.Semaphore(args.parallel)

        async def exchange(req: ops.ConfigRequest) -> AccessMessage | None:
            async with sem:
                try:
                    return await client.request_config(
                        req.node,
                        req.pdu,
                        req.expect_opcode,
                        timeout=args.timeout,
                        retries=args.retries,
                    )
                except TimeoutError:
                    print(f"    {cdb.label(req.node)}: no answer to {req.describe}")
                    return None

        results: dict[tuple[int, int], ops.HopCell] = {}
        started = time.monotonic()
        for i, origin in enumerate(nodes, 1):
            print(f"[{i}/{len(nodes)}] {cdb.label(origin)} beats …", flush=True)
            probes = [
                ops.HopProbe(counter, origin, args.beats, args.period)
                for counter in nodes
                if counter != origin
            ]
            results.update(await _hop_round(exchange, probes))
        print()
        print(ops.hop_matrix_text(cdb, nodes, results))
        print(f"\n{len(results)} pairs in {time.monotonic() - started:.0f} s")
        if args.json:
            await asyncio.to_thread(
                Path(args.json).write_text,
                ops.hop_matrix_json(cdb, nodes, results, args.beats, args.period),
            )
            print(f"written to {args.json}")

    await with_client(args, run)


async def _hop_round(
    exchange: Callable[[ops.ConfigRequest], Awaitable[AccessMessage | None]],
    probes: list[ops.HopProbe],
) -> dict[tuple[int, int], ops.HopCell]:
    """One origin's round of the hop matrix: subscribe everyone, beat, read, restore; the cells by (origin, counter)."""
    if not probes:
        return {}
    first = probes[0]
    origin = first.source
    before_msg = await exchange(first.read_publication)
    if before_msg is None:
        print("    unreachable, skipped")
        return {(origin, p.subscriber): None for p in probes}
    before = C.decode_heartbeat_publication_status(before_msg.params)
    await asyncio.gather(*(exchange(p.subscribe) for p in probes))
    try:
        await exchange(first.publish)
        await asyncio.sleep(first.wait_seconds)
        statuses = await asyncio.gather(
            *(exchange(p.read_subscription) for p in probes)
        )
    finally:
        # Ctrl-C or a lost link mid-round must not leave the origin beating to all-nodes with HA's publication gone
        # (`exchange` prints and returns None on a silent node; a lost link raises out of here, after the report)
        try:
            restored = await exchange(first.restore_publication(before))
        except ConnectionError:
            restored = None
        if restored is None:
            print(
                f"    !! {origin:04X}: Heartbeat Publication NOT restored — re-run `config hops <node> {origin:04X}`"
                " or set it again from Home Assistant",
                file=sys.stderr,
            )
        await asyncio.gather(*(exchange(p.unsubscribe) for p in probes))
    cells: dict[tuple[int, int], ops.HopCell] = {}
    summary = []
    for probe, m in zip(probes, statuses, strict=True):
        if m is None:
            cells[origin, probe.subscriber] = None
            summary.append(f"{probe.subscriber:04X}=?")
            continue
        cell, text = ops.hop_cell(C.decode_heartbeat_subscription_status(m.params))
        cells[origin, probe.subscriber] = cell
        summary.append(f"{probe.subscriber:04X}={text}")
    print("    " + " ".join(summary), flush=True)
    return cells


async def _config_reply(
    client: ProxyClient, req: ops.ConfigRequest, args: argparse.Namespace
) -> AccessMessage:
    return await client.request_config(
        req.node, req.pdu, req.expect_opcode, timeout=args.timeout, retries=args.retries
    )


async def cmd_devices(args: argparse.Namespace) -> None:
    cdb = ops.load_cdb(args.cdb)
    d = load_devices(cdb)
    for light in d.lights:
        meter = f" meter={light.meter_address:04X}" if light.meter_address else ""
        print(
            f"light  {light.address:04X} {light.kind:6s} {light.name!r:34s} rooms={light.rooms}{meter}"
        )
    for s in d.sockets:
        sensor = f"{s.meter_address:04X}" if s.meter_address else "-"
        print(f"socket {s.address:04X} sensor={sensor} {s.name!r:34s} rooms={s.rooms}")
    for b in d.buttons:
        print(f"button {b.address:04X} loc={b.location:02X} {b.name!r}")
    for sc in d.scenes:
        print(f"scene  {sc.number:<3d} {sc.name!r}")


async def cmd_ctlget(args: argparse.Namespace) -> None:
    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        m = await client.request(dst, M.light_ctl_get(), M.LIGHT_CTL_STATUS)
        print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")
        if args.echo:  # re-send the same values with explicit transition/delay → 13-byte PDU → exercises segmented TX
            lightness, t = (
                int.from_bytes(m.params[:2], "little"),
                int.from_bytes(m.params[2:4], "little"),
            )
            pdu = M.light_ctl_set(lightness, t, transition=0, delay=0)
            print(
                f"  echoing CTL Set l={lightness} t={t} as a segmented message ({len(pdu)} bytes)"
            )

            m = await client.request(dst, pdu, M.LIGHT_CTL_STATUS)
            print(f"  {cdb.label(m.src)}: {M.describe(m.access_pdu)}")

    await with_client(args, run)


# ----------------------------------------------------------------------------- prop get / set / list


async def _property_exchange(
    client: ProxyClient,
    cdb: CDB,
    dst: int,
    req: ops.PropertyRequest,
    args: argparse.Namespace,
) -> None:
    print(f"  → {cdb.label(dst)}: {req.describe} [{req.server}]")
    m = await client.request(
        dst,
        req.pdu,
        req.expect_opcode,
        expect_cid=req.expect_cid,
        retries=args.retries,
        timeout=args.timeout,
    )
    print(f"  ← {cdb.label(m.src)}: {ops.property_status_text(m, req.pid, req.server)}")


async def cmd_prop_get(args: argparse.Namespace) -> None:
    try:
        pid, spec = ops.resolve_property(args.property)
        req = ops.property_get(pid, spec, args.server)
    except (argparse.ArgumentTypeError, ValueError) as err:
        sys.exit(str(err))
    if args.pad:  # forces segmentation above 11 bytes (test only)
        req = ops.PropertyRequest(
            req.pid,
            req.server,
            req.pdu + bytes(args.pad),
            req.expect_opcode,
            req.expect_cid,
        )

    async def run(client: ProxyClient, cdb: CDB) -> None:
        await _property_exchange(client, cdb, cdb.resolve(args.target), req, args)

    await with_client(args, run)


async def cmd_prop_set(args: argparse.Namespace) -> None:
    try:
        pid, spec = ops.resolve_property(args.property)
        req = ops.property_set(
            pid,
            spec,
            args.value,
            server=args.server,
            ack=not args.unack,
            user_access=args.access,
        )
    except (argparse.ArgumentTypeError, ValueError) as err:
        sys.exit(str(err))

    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        if args.unack:
            print(
                f"  → {cdb.label(dst)}: {req.describe} [{req.server}] (unacknowledged)"
            )
            await client.send_access(dst, req.pdu)
            await asyncio.sleep(args.window)
            return
        await _property_exchange(client, cdb, dst, req, args)

    await with_client(args, run)


async def cmd_prop_lists(args: argparse.Namespace) -> None:
    """Ask an element which properties each of its six property servers holds (plus its sensor descriptors)."""

    async def run(client: ProxyClient, cdb: CDB) -> None:
        dst = cdb.resolve(args.target)
        print(f"  {cdb.label(dst)}")
        for req in ops.property_list_requests():
            try:
                m = await client.request(
                    dst,
                    req.pdu,
                    req.expect_opcode,
                    expect_cid=req.expect_cid,
                    retries=1,
                    timeout=args.timeout,
                    quiet=True,
                )
            except TimeoutError:
                print(f"  {req.server:16s} no answer")
                continue
            print(f"  {req.server:16s} {ops.property_list_text(req.server, m)}")
        element = cdb.element(dst)
        if element is not None and "1100" in element.models:
            try:
                m = await client.request(
                    dst,
                    M.sensor_descriptor_get(),
                    M.SENSOR_DESCRIPTOR_STATUS,
                    retries=1,
                    timeout=args.timeout,
                    quiet=True,
                )
            except TimeoutError:
                print("  sensor           no descriptor answer")
            else:
                print(f"  sensor           {M.describe(m.access_pdu)}")

    await with_client(args, run)


def cmd_prop_list(args: argparse.Namespace) -> None:
    for row in ops.property_rows(args.product, include_firmware_only=args.all):
        print(row)


# ----------------------------------------------------------------------------- config


async def _config_exchange(
    client: ProxyClient, cdb: CDB, req: ops.ConfigRequest, args: argparse.Namespace
) -> None:
    print(f"  → {cdb.label(req.node)}: {req.describe}")
    m = await client.request_config(
        req.node, req.pdu, req.expect_opcode, timeout=args.timeout, retries=args.retries
    )
    print(f"  ← {cdb.label(m.src)}: {ops.config_status_text(m)}")


def _node_of(cdb: CDB, addr: int) -> int:
    """The primary unicast of the node owning `addr` (Config messages go to the primary element)."""
    node = cdb.node_by_addr(addr)
    if node is None:
        sys.exit(f"{addr:04X} is not an element of any node in the CDB")
    return node.unicast


async def cmd_config_audit(args: argparse.Namespace) -> None:
    """Compare a node's Configuration Server with the export (`jhmesh.audit`, device-key Gets only)."""

    async def run(client: ProxyClient, cdb: CDB) -> None:
        node = cdb.node_by_addr(_node_of(cdb, args.node))
        assert node is not None  # _node_of exited otherwise
        exchange = audit.client_exchange(client, timeout=args.timeout, retries=1)
        print(ops.audit_text(cdb, await audit.audit_node(exchange, node)))

    await with_client(args, run)


async def cmd_config(args: argparse.Namespace) -> None:
    if args.config_cmd == "audit":
        await cmd_config_audit(args)
        return
    if args.config_cmd == "hops":
        await cmd_config_hops(args)
        return
    if args.config_cmd == "hopmatrix":
        await cmd_config_hopmatrix(args)
        return

    if getattr(
        args, "dry_run", False
    ):  # show the Config message, touch nothing — a typo here is a live write
        cdb = ops.load_cdb(args.cdb)
        req = _config_request(args, cdb)
        print(f"  dry run — would send to {cdb.label(req.node)}: {req.describe}")
        return

    async def run(client: ProxyClient, cdb: CDB) -> None:
        req = _config_request(args, cdb)
        await _config_exchange(client, cdb, req, args)
        if config_writes(args):
            print(
                f"  export not updated — run `config audit {req.node:04X}` to compare it with the node"
            )

    await with_client(args, run)


def _config_request(args: argparse.Namespace, cdb: CDB) -> ops.ConfigRequest:
    """The Config message of a `config <sub-command>` invocation, resolved against the export (no link needed)."""
    node = _node_of(cdb, args.node)
    if args.config_cmd == "get-composition":
        return ops.config_composition(node, args.page)
    if args.config_cmd == "publication":
        group = ops.resolve_or_exit(cdb, args.group) if args.group else None
        return ops.config_publication(node, args.element, args.model, group)
    if args.config_cmd in ("subscribe", "unsubscribe"):
        return ops.config_subscription(
            node,
            args.element,
            args.model,
            ops.resolve_or_exit(cdb, args.group),
            add=args.config_cmd == "subscribe",
        )
    if args.config_cmd == "subscriptions":
        return ops.config_subscriptions(node, args.element, args.model)
    return ops.config_bind(  # bind / unbind
        node, args.element, args.model, bind=args.config_cmd == "bind"
    )


# ----------------------------------------------------------------------------- export


def cmd_export_write(args: argparse.Namespace) -> int:
    """Round-trip an export through `ProjectFile` without changes and show what the writer would alter.

    The diff never shows key material (`summarize_diff`); `--out` (written 0600) is the only way to see the
    rendered keys.
    """
    if args.out and Path(args.out).resolve() == Path(args.file).resolve():
        sys.exit(
            f"--out {args.out} is the input file: the rendering is written next to the original, never over it"
        )
    rendered, diff = ops.roundtrip_export_or_exit(Path(args.file))
    print(ops.summarize_diff(diff))
    if args.out:
        ops.write_private(Path(args.out), rendered)  # the export holds every mesh key
        print(f"written to {args.out} ({len(rendered)} bytes)")
    return 1 if diff and args.strict else 0


async def cmd_provision(args: argparse.Namespace) -> None:
    """`provision --scan` lists unprovisioned devices; `provision <uuid> --unicast X --yes` provisions one.

    Provisioning only (Invite → Complete over PB-GATT, No OOB): the node gets the export's primary NetKey, its IV
    index and the unicast address, and the device key is written to an owner-only file (`device_key_path`, or
    `--key-file`) whose path is printed — never the key itself. Nothing is configured and nothing is written to
    the export. An existing key file, or one whose directory cannot be written, is refused before any radio traffic:
    it may hold the only copy of an earlier device's key, and a new key nobody could write down would be lost. Without `--yes` it refuses before any radio traffic: the device leaves the unprovisioned state
    for good (only a factory reset brings it back).
    """
    if args.list_devices:
        print("\n".join(ops.unprovisioned_rows(await scan_unprovisioned(args.seconds))))
        return
    if args.uuid is None or args.unicast is None:
        sys.exit(
            "provision needs a device UUID and --unicast (or --scan to list devices)"
        )
    if not args.yes:
        sys.exit(
            f"provisioning {args.uuid} at {args.unicast:04X} is not reversible without a factory reset of the"
            " device: pass --yes to go ahead"
        )
    cdb = ops.load_cdb(args.cdb)
    known = next((n for n in cdb.nodes if n.uuid == args.uuid), None)
    if known is not None:
        sys.exit(
            f"{args.uuid} is node {known.unicast:04X} ({known.name}) in the export: remove it there first"
        )
    reserved = {HA_SOURCE, args.source}
    problem = ops.provision_address_problem(cdb, args.unicast, 1, reserved)
    if problem:
        sys.exit(problem)
    key_file = args.key_file or device_key_path(args.unicast)
    if key_file.exists():
        sys.exit(
            f"{key_file} already exists (an earlier device key?): move it away or pass --key-file"
        )
    if not os.access(key_file.parent, os.W_OK):
        # checked before the device is touched: a key that cannot be written once it exists is lost for good
        sys.exit(
            f"cannot write the device key to {key_file}: its directory is missing or not writable"
        )
    iv_index = cdb.iv_index if args.iv_index is None else args.iv_index
    if args.iv_index is None:
        print(
            f"IV index {iv_index} (the export's lower bound; pass --iv-index when the network's is higher)"
        )
    data = ProvisioningData(
        net_key=cdb.net_keys[0].key,
        unicast=args.unicast,
        iv_index=iv_index,
        iv_update=args.iv_update,
    )
    found = [d for d in await scan_unprovisioned(args.seconds) if d.uuid == args.uuid]
    if not found:
        sys.exit(
            f"{args.uuid} was not seen advertising the provisioning service within {args.seconds:g}s"
        )

    def check(caps: Capabilities) -> None:
        problem = ops.provision_address_problem(
            cdb, args.unicast, caps.elements, reserved
        )
        if problem:
            raise ProvisioningError(problem)

    try:
        result = await provision_device(found[0], data, check=check)
    except ProvisioningError as err:
        sys.exit(f"provisioning failed: {err}")
    ops.write_private(key_file, ops.device_key_record(args.uuid, result))
    print(ops.provisioned_text(result, key_file))


# ----------------------------------------------------------------------------- argument parser


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915  # one flat listing of every sub-command
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cdb", default=str(DEFAULT_CDB))
    ap.add_argument(
        "--source",
        type=ops.parse_address,
        default=DEFAULT_SOURCE,
        metavar="ADDR",
        help="our unicast address, hex (default 7FFF; the HA integration uses 0D00); it must be free in the export"
        " (no node's, not excluded, outside every provisioner's range). Each address has its own "
        "sequence store tools/.jhmesh_state_<ADDR>.json; never share an address with another client",
    )
    ap.add_argument(
        "--scan", type=float, default=4.0, help="proxy scan duration before connecting"
    )
    ap.add_argument(
        "--window", type=float, default=2.0, help="collection window for group requests"
    )
    ap.add_argument(
        "--timeout", type=float, default=3.0, help="per-attempt response timeout"
    )
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument(
        "--adv",
        action="store_true",
        help="also print local name / manufacturer data / service UUIDs of each advertisement",
    )
    p = sub.add_parser("listen")
    p.add_argument("--seconds", type=float, default=60.0, help="listen duration")
    p.add_argument("--src", help="only messages from this element (hex)")
    p.add_argument("--dst", help="only messages to this address (hex)")
    p = sub.add_parser("get")
    p.add_argument("target")
    p = sub.add_parser("set")
    p.add_argument("target")
    p.add_argument(
        "value",
        type=str.lower,
        choices=ONOFF_VALUES,
        help="on|off (true/false, 1/0); a typo is an error, not OFF",
    )
    p.add_argument(
        "--t0",
        action="store_true",
        help="send explicit transition time 0 (like the gateway does)",
    )
    p.add_argument("--unack", action="store_true", help="use Set Unacknowledged")
    p = sub.add_parser("blink")
    p.add_argument("target")
    p.add_argument("--hold", type=float, default=2.0)
    p = sub.add_parser("lightness")
    p.add_argument("target")
    p.add_argument(
        "value", nargs="?", type=ops.parse_uint16, help="0..65535; omitted = Get"
    )
    p = sub.add_parser("ctl")
    p.add_argument("target")
    p.add_argument("lightness", type=ops.parse_uint16, help="0..65535")
    p.add_argument(
        "kelvin", type=ops.parse_uint16, help="colour temperature, 800..20000 K"
    )
    p = sub.add_parser("ctlrange")
    p.add_argument("target")
    p = sub.add_parser("scene")
    p.add_argument("target")
    p.add_argument("number", type=ops.parse_scene_number)
    p = sub.add_parser(
        "scene-actions", help="what an element does in its scenes (Scene Action Setup)"
    )
    p.add_argument("target", help="element address (hex) or name")
    p.add_argument(
        "number",
        nargs="?",
        type=ops.parse_scene_or_list,
        help="one scene number; omitted = every scene",
    )
    p = sub.add_parser("sched", help="JH Scheduler slots of an element")
    p.add_argument("target", help="element address (hex) or name")
    p.add_argument(
        "--slot", type=int, help="dump this slot (0..15) instead of the used ones"
    )
    p.add_argument(
        "--central-id",
        type=int,
        default=0,
        help="centralScheduleId to match in the slot list",
    )
    p = sub.add_parser("health", help="Health faults of a node (or of all nodes: FFFF)")
    p.add_argument("target", help="node address (hex), name or group")
    p.add_argument(
        "--clear", action="store_true", help="clear the registered faults first"
    )
    p.add_argument("--test", type=int, help="run self-test N (company 0527) first")

    prop = sub.add_parser(
        "prop", help="device properties (codec-aware, see jhmesh/properties.py)"
    )
    psub = prop.add_subparsers(dest="prop_cmd", required=True)
    p = psub.add_parser("get")
    p.add_argument("target")
    p.add_argument("property", help="name (key_mode) or hex id (5003)")
    p.add_argument("--server", choices=ops.SERVERS, help="override the hosting server")
    p.add_argument(
        "--pad",
        type=int,
        default=0,
        help="append N zero bytes (forces segmentation above 11 bytes; test only)",
    )
    p = psub.add_parser("set")
    p.add_argument("target")
    p.add_argument("property", help="name (key_mode) or hex id (5003)")
    p.add_argument("value", help="codec text form, or hex:<bytes>")
    p.add_argument("--server", choices=ops.SERVERS, help="override the hosting server")
    p.add_argument("--unack", action="store_true", help="use Set Unacknowledged")
    p.add_argument(
        "--access",
        type=int,
        choices=range(4),
        help="userAccess byte of an Admin Set (default: what the app sends for this property)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="confirm a Set to a group (every member takes it); a unicast target needs none",
    )
    p = psub.add_parser("list")
    p.add_argument(
        "product",
        type=ops.parse_product,
        help="JUNG product id, hex (01 = 1-gang push-button)",
    )
    p.add_argument("--all", action="store_true", help="include firmware-only ids")
    p = psub.add_parser(
        "lists", help="ask an element on air which properties its servers hold"
    )
    p.add_argument("target", help="element address (hex) or name")

    config = sub.add_parser("config", help="Config Server messages (device key)")
    csub = config.add_subparsers(dest="config_cmd", required=True)
    p = csub.add_parser("get-composition")
    p.add_argument("node", type=ops.parse_address, help="any element of the node, hex")
    p.add_argument("--page", type=int, default=0)
    p = csub.add_parser(
        "audit",
        help="compare the node's settings, publications, subscriptions and AppKeys with the export (Gets only)",
    )
    p.add_argument("node", type=ops.parse_address, help="any element of the node, hex")
    p = csub.add_parser(
        "hops",
        help="mesh hops between two nodes (Heartbeat Subscription probe, everything restored)",
    )
    p.add_argument("node", type=ops.parse_address, help="the node that counts, hex")
    p.add_argument(
        "origin", type=ops.parse_address, help="the node that beats, hex"
    )  # not "source": that is our own address (--source)
    p.add_argument(
        "--beats", type=int, default=4, help="heartbeats to send (default 4)"
    )
    p.add_argument(
        "--period", type=int, default=2, help="seconds between them (default 2)"
    )
    p = csub.add_parser(
        "hopmatrix",
        help="hops between every pair of nodes (each beats in turn, all others count; everything restored)",
    )
    p.add_argument(
        "nodes",
        type=ops.parse_address,
        nargs="*",
        help="restrict to these nodes (any element, hex); default: every provisioned node",
    )
    p.add_argument(
        "--beats", type=int, default=4, help="heartbeats per origin (default 4)"
    )
    p.add_argument(
        "--period", type=int, default=2, help="seconds between them (default 2)"
    )
    p.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="config exchanges in flight at once (default 3)",
    )
    p.add_argument("--json", help="also write the pairs to this JSON file")
    for name, group_arg in (
        ("publication", "?"),
        ("subscribe", None),
        ("unsubscribe", None),
        ("subscriptions", ""),
        ("bind", ""),
        ("unbind", ""),
    ):
        p = csub.add_parser(name)
        p.add_argument(
            "node", type=ops.parse_address, help="any element of the node, hex"
        )
        p.add_argument("element", type=ops.parse_address, help="element address, hex")
        p.add_argument("model", type=ops.parse_model, help="model id (1000, 05271013)")
        if group_arg == "?":
            p.add_argument(
                "group", nargs="?", help="group address (hex) or name; omitted = Get"
            )
        elif group_arg is None:
            p.add_argument("group", help="group address (hex) or name")
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="print the Config message that would be sent and stop",
        )
        if (
            name != "subscriptions"
        ):  # the one read-only sub-command here (publication is one without a group)
            p.add_argument(
                "--yes",
                action="store_true",
                help="confirm the write: it changes the node now and does not update the export",
            )

    export = sub.add_parser("export", help="project-file writer checks")
    esub = export.add_subparsers(dest="export_cmd", required=True)
    p = esub.add_parser("write")
    p.add_argument("file")
    p.add_argument(
        "--out", help="also write the re-rendered file here (never the input)"
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when the round trip is not byte-identical",
    )

    p = sub.add_parser(
        "provision",
        help="provision an unprovisioned device over PB-GATT (No OOB); its device key goes to an owner-only file",
    )
    p.add_argument(
        "uuid",
        nargs="?",
        type=ops.parse_device_uuid,
        help="Device UUID as `provision --scan` shows it",
    )
    p.add_argument(
        "--scan",
        dest="list_devices",
        action="store_true",
        help="list the devices advertising the Mesh Provisioning Service and stop",
    )
    p.add_argument(
        "--unicast",
        type=ops.parse_address,
        help="primary unicast address for the new node, hex (all its elements must be free)",
    )
    p.add_argument(
        "--iv-index",
        type=ops.parse_iv_index,
        help="the network's current IV index (default: the export's lower bound)",
    )
    p.add_argument(
        "--iv-update",
        action="store_true",
        help="the network is in the IV Update procedure",
    )
    p.add_argument(
        "--seconds", type=float, default=10.0, help="scan duration (default 10)"
    )
    p.add_argument(
        "--key-file",
        type=Path,
        help="owner-only file for the new device key (default tools/.jhmesh_devkey_<unicast>.json; must not exist)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="really provision (the device only returns to unprovisioned by a factory reset)",
    )

    sub.add_parser("devices")
    p = sub.add_parser("ctlget")
    p.add_argument("target")
    p.add_argument("--echo", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(line_buffering=True)
    logging.getLogger("bleak").setLevel(logging.WARNING)
    if args.cmd == "prop" and args.prop_cmd == "list":
        cmd_prop_list(args)
        return 0
    if args.cmd == "export":
        return cmd_export_write(args)
    commands: dict[str, Command] = {
        "scan": lambda a: with_client(a, None),
        "listen": cmd_listen,
        "get": cmd_get,
        "set": cmd_set,
        "blink": cmd_blink,
        "lightness": cmd_lightness,
        "ctl": cmd_ctl,
        "ctlrange": cmd_ctlrange,
        "scene": cmd_scene,
        "scene-actions": cmd_scene_actions,
        "sched": cmd_sched,
        "health": cmd_health,
        "prop": lambda a: {
            "get": cmd_prop_get,
            "set": cmd_prop_set,
            "lists": cmd_prop_lists,
        }[a.prop_cmd](a),
        "config": cmd_config,
        "provision": cmd_provision,
        "devices": cmd_devices,
        "ctlget": cmd_ctlget,
    }
    asyncio.run(commands[args.cmd](args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
