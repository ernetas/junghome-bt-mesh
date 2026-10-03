"""Rooms and key connections: the app's Config-message sequences on the mesh, then the project-file write-back.

Roadmap step 3, item 11 — the first features that use the device-key transport (`ProxyClient.request_config`)
and the project-file writer (`export.ProjectFile`). Every operation of `MeshConfigurator` runs the same pipeline,
one at a time per hub:

1. load the export the config entry points at, fresh from disk (so the newer-export guard judges the truth); an
   entry set up from a gateway first asks the gateway for its export and adopts it when the app changed the
   installation since (the app uploads after every change), so the plan is made — and later uploaded — on top
   of the app's changes, never over them;
2. mutate it through the `ProjectFile` mutators, collecting `ModelChange`s = the Config messages to send;
3. send them with the node's device key and check every Config status (`ConfigStatus.ok`, echoing the step's
   element / model / address so a late duplicate cannot pass for the next step's answer), additive messages
   before destructive ones so a stop leaves the old wiring working. The first refusal or silence stops the plan
   *apply-and-record*: the steps accepted before it are what the mesh now holds, so they are replayed into a
   fresh copy of the export and that copy is written (and handed to the gateway) before the error is raised —
   the file never disagrees with the nodes, and the error says what was applied and what was not. A plan
   cancelled from outside (an automation restarted, a script turned off, Home Assistant stopping — D12) is
   recorded the same way before the cancellation goes on, and a write once started runs to its end
   (`run_to_end`); one a crash cuts off is in the plan journal (`plan_journal`), which the next setup records
   (`async_replay_journal`). Every message is idempotent (Add / Delete / Publication Set / App Bind), so running the
   action again with the same target completes it;
4. for key connections, write the KeyMode property (`0x5003`, LBC Admin Property Set) and read it back;
5. save the file atomically (`NewerExportError` becomes a translated error asking for a fresh export);
6. when the entry knows a gateway (host, token, pinned certificate), hand the export to it the way the app does
   after every change (`POST config {"data": {"project_file": …}}`, roadmap step 14) so the gateway sees the same
   installation (the app never downloads it: its next upload lacks HA's changes, which `_carry_over` puts back
   before that upload is adopted — review-3 W1); a failed upload raises the `gateway_sync_failed` repair issue
   and is retried like the app retries it (twice, 15 s apart, `GATEWAY_UPLOAD_RETRIES`), and `sync_gateway()`
   retries it on demand — each time after checking, by content digest against what HA last synced, that the
   gateway does not hold a change of its own meanwhile. The digest and the time of the last successful upload are
   kept in the entry's `GatewaySync` record (the time is the app's `gateway_last_sync`).

The caller (`services.py`) then has the running hub take the new export over in place (`model_update`, review-4
D23), which is how the hub's device model — and with it the entities, their `rooms` attributes and the buttons'
devices — follows it; a change it cannot follow in place reloads the config entry, as every change did before.

The message sets mirror the JUNG HOME app (`docs/android/network-logic.md` §1.5, §2.3, §2.4;
`docs/gap-analysis/network-features.md` §1, §2, §8.3): room membership is a `Model Subscription Add` of the
load's OnOff / Level servers to the room address (plus the publish groups of the keys already linked to the
room); a key connection is `Reset KeySetPropertyMode` (AppKey), then every publication and subscription of the
key element cleared (`Publication Set 0x0000` / `Subscription Delete`, never *Delete All*), then the key mode's
client models wired to the target's element group (`Publication Set` + `Subscription Add`) — a room link wires
the room's loads to the key's own element group instead — then KeyMode. A scene link (`SetSceneConnection`,
network-logic.md §2.5) publishes the key's Scene Client to all nodes (`0xFFFF`, publish only), writes the scene
into the key (KeyModeSceneConfig `0x5002`) and records the app's `keyModeSceneConfigExports` row; it has no
KeySetPropertyMode reset, as in the app. Clearing a key ("no function") is the
clear step alone: the app leaves KeyMode as it is, and so do we. The app's *order* is not kept on air: the new
wiring goes out first and the old one is cleared last (minus what the new wiring re-adds), and the
KeySetPropertyMode reset goes out only once every Config step was accepted — a refused step 1 leaves a key that
was in property mode exactly as it was. A metering socket's thresholds (`set_threshold_devices`) are wired like a
room link from the socket's side: its OnOff Client (on the meter element) subscribes to — and publishes to — that
element's own group, and the loads it should switch subscribe their JUNG User Property and OnOff servers there;
`unwire_threshold` undoes it the app's way, with a publication reset sent in the app's order (`_send(as_planned=True)`).

No key material is logged or put into error messages.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Callable, Collection, Coroutine, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .climate import level_to_temperature
from .const import (
    CONF_CDB_PATH,
    CONF_GATEWAY_LAST_SYNC,
    CONF_GATEWAY_SYNCED,
    CONF_METADATA_DIR,
    DEFAULT_PROVISIONER_IDENTITY,
    DEFAULT_UNUSED_SCENES_DRY_RUN,
    DOMAIN,
    GATEWAY_UPLOAD_RETRIES,
    GATEWAY_UPLOAD_RETRY_DELAY,
    ISSUE_CARRY_OVER_CONFLICT,
    ISSUE_GATEWAY_CERTIFICATE,
    ISSUE_GATEWAY_SYNC,
    ISSUE_GATEWAY_TOKEN,
    ISSUE_PLAN_INTERRUPTED,
    ISSUE_SCENE_HELD,
    OPTION_PROVISIONER_IDENTITY,
    SERVICE_LINK_WAIT,
    SIGNAL_GATEWAY_SYNCED,
)
from .coordinator import issue_id
from .gateway_api import (
    GatewayAuthError,
    GatewayCertificateMismatch,
    GatewayError,
    GatewayUnreachable,
    JungHomeGatewayApi,
    api_for_entry,
)
from .jhmesh import config_messages as C
from .jhmesh import messages as M
from .jhmesh import properties as P
from .jhmesh import vendor_models as V
from .jhmesh.advert import mac_from_uuid
from .jhmesh.cdb import CDB, InvalidExport, canonical_uuid, parse_address
from .jhmesh.devices import (
    ALL_SCENES,
    GATEWAY_PID,
    GROUP_RANGE,
    KEY_LOCATION,
    SCENE_CLIENT,
    SOCKET_PIDS,
    Metadata,
    as_int,
    is_room,
    load_kind,
    meta_list,
)
from .jhmesh.export import (
    DEFAULT_SCENE_ICON,
    FUNCTION_KEY_MODE,
    GROUP_FUNCTIONS,
    KEY_MODE_GATEWAY,
    KEY_MODE_LIGHT,
    KEY_MODE_MOVE,
    KEY_MODE_SCENE,
    KEY_MODE_SERVERS,
    KEY_MODE_SWITCH,
    RENAME_MAX_LENGTH,
    AllocationCrowded,
    ExportError,
    InvalidName,
    ModelChange,
    NewerExportError,
    ProjectFile,
    cdb_element_groups,
    check_name,
    function_code,
    has_model,
    hexaddr,
    keeps_row,
    location_ids,
    meta_rows,
    raw_model,
    timestamp_advanced,
    write_private,
    write_private_with_backup,
)
from .jhmesh.merge import MISSING, Change, apply_changes, diff_documents
from .jhmesh.onboarding import DeviceCount, missing_devices
from .jhmesh.onboarding import record as record_node
from .jhmesh.pdu import ALL_PROXIES
from .jhmesh.vault import RangeError, Ranges
from .keep_awake import sleepy_node
from .onboard import advertises_unprovisioned

if TYPE_CHECKING:
    from .coordinator import JungHomeHub
    from .jhmesh.audit import NodeAudit
    from .jhmesh.cdb import Element, Node
    from .jhmesh.client import AccessMessage
    from .jhmesh.commission import Plan

_LOGGER = logging.getLogger(__name__)

# Service-facing mode names. `light_and_switch` is a room function (lamps and sockets together); `gateway` is only
# meaningful with the gateway's primary element as the target (network-logic.md §2.3, "key -> gateway").
MODE_LIGHT, MODE_SWITCH, MODE_MOVE, MODE_GATEWAY, MODE_LIGHT_AND_SWITCH = (
    "light",
    "switch",
    "move",
    "gateway",
    "light_and_switch",
)
KEY_MODES: dict[str, int] = {
    MODE_LIGHT: KEY_MODE_LIGHT,
    MODE_SWITCH: KEY_MODE_SWITCH,
    MODE_MOVE: KEY_MODE_MOVE,
    MODE_GATEWAY: KEY_MODE_GATEWAY,
    MODE_LIGHT_AND_SWITCH: KEY_MODE_LIGHT,
}
ROOM_FUNCTIONS: dict[
    str, str
] = {  # GroupConnection.Function per mode (network-logic.md §2.1)
    MODE_LIGHT: "LIGHT",
    MODE_SWITCH: "SWITCH",
    MODE_MOVE: "BLIND",
    MODE_LIGHT_AND_SWITCH: "LIGHT_AND_SWITCH",
}
DEVICE_MODES = frozenset({MODE_LIGHT, MODE_SWITCH, MODE_MOVE, MODE_GATEWAY})
ROOM_MODES = frozenset(ROOM_FUNCTIONS)
# a scene link's key mode: never a `mode` of the action, which names the scene instead (`assign_key(scene=…)`)
MODE_SCENE = "scene"
# blinds: no such device was ever wired from here; scene links: never tried on a real device (review-3 F15)
UNTESTED_MODES = frozenset({MODE_MOVE, MODE_SCENE})
MODES = tuple(KEY_MODES)

# Client models the key element publishes from, per key mode (network-logic.md §2.1).
KEY_MODE_CLIENTS: dict[int, tuple[str, ...]] = {
    KEY_MODE_LIGHT: ("1001", "1003", "05271015"),
    KEY_MODE_MOVE: ("1003", "05271015"),
    KEY_MODE_SWITCH: ("1001", "05271015"),
    KEY_MODE_GATEWAY: ("05271015",),
    KEY_MODE_SCENE: (SCENE_CLIENT,),  # publish only, to all nodes
}
# `RemoveConnectionForAddress` never touches these models of a key element (network-logic.md §2.3 step 4).
CLEAR_KEEP_MODELS = frozenset({"1100", "05271013", "05271011"})
USER_PROPERTY_SERVER = "05271013"
ONOFF_SERVER, ONOFF_CLIENT = "1000", "1001"
SENSOR_SERVER = "1100"

PROPERTY_KEY_MODE = 0x5003
PROPERTY_KEY_SCENE_CONFIG = (
    0x5002  # KeyModeSceneConfig: the scene a key in scene mode recalls
)
# The servers a key's own load element hosts in the app's pick for a scene link's cached publication address
# (`ConnectionSceneSelectionActivity` → `e2()`: OnOff / Level / Lightness / Scene / CTL servers, network-logic.md §2.5)
SCENE_LINK_LOAD_SERVERS = ("1000", "1002", "1300", "1203", "1303")
PROPERTY_KEY_PROPERTY_MODE, PROPERTY_KEY_VALUE_UP, PROPERTY_KEY_VALUE_DOWN = (
    0x5006,
    0x5007,
    0x5008,
)
VENDOR_ADMIN_STATUS = 0x05  # LBC Admin Property Status `C5 27 05`
APP_KEY_INDEX = 0

CONFIG_TIMEOUT = 3.0  # seconds per Config request attempt
CONFIG_RETRIES = 2
KEY_MODE_TIMEOUT = 2.0  # a vendor Set may be answered by a group publication or not at all; then we read back
SCENE_TIMEOUT = 2.0  # Scene Store / Delete and Scene Action Setup Set: same rule, read back when unanswered
SCENE_SETUP_SERVER, SCENE_ACTION_SETUP = "1204", "05271017"
# What the app lets a device hold before it refuses to store one more scene (`AbstractC0916e.B1()`,
# network-features.md §3 *Capacity check*): 8 per channel on a node whose channels keep their own scene list
# (Scene Action Setup), 16 in a node's SIG scene register otherwise. Timer scenes take slots like any other.
SCENE_ACTION_CAPACITY, SCENE_REGISTER_CAPACITY = 8, 16


@dataclass(frozen=True)
class ConfigStep:
    """One Config message of a plan: the node it goes to, the PDU, the status opcode that acknowledges it.

    `change` is the CDB edit the message mirrors (a Model App Bind carries `bind` = (element, model) instead), so
    an accepted step can be replayed into a fresh copy of the export and its Status checked against it;
    `unlinks` names the key element whose room link the step tears down (the link's `meta` row goes with it).
    """

    node: int
    pdu: bytes
    expect: int
    change: ModelChange | None = None
    bind: tuple[int, str] | None = None
    unlinks: int | None = None

    @property
    def what(self) -> str:
        """Describe the message for logs and errors."""
        return M.describe(self.pdu)

    @property
    def additive(self) -> bool:
        """Whether the step adds wiring (Bind, Subscription Add, Publication Set to a group) rather than removes it."""
        if self.change is None:
            return True
        return self.change.kind == "subscribe" or (
            self.change.kind == "publish" and self.change.address != 0
        )

    def matches(self, message: AccessMessage) -> bool:
        """Whether a Config Status is the answer to *this* step: it echoes the element, model and address sent.

        A node answers every request it receives, so a reply that came late — after the attempt timed out and
        the PDU was re-sent — arrives twice; matched on node + opcode alone the duplicate would acknowledge the
        next same-opcode step and mask its refusal. A Status that does not decode is left to `_request`, which
        counts it as a refusal (it cannot be told apart from anyone's).
        """
        try:
            status = C.decode_config(message.opcode, message.params)
        except ValueError:
            return True
        if self.bind is not None:
            element, model = self.bind
            return not isinstance(status, C.ModelAppStatus) or (
                status.element == element
                and status.model == C.model_id(model)
                and status.app_key_index == APP_KEY_INDEX
            )
        change = self.change
        if change is None:
            return True
        if isinstance(status, C.ModelSubscriptionStatus):
            return (
                status.element == change.element
                and status.model == C.model_id(change.model)
                and status.address == change.address
            )
        if isinstance(status, C.ModelPublicationStatus):
            return (
                status.element == change.element
                and status.model == C.model_id(change.model)
                and status.publish_address == change.address
            )
        return True


def ordered(
    steps: Iterable[ConfigStep], sleepy: Collection[int] = frozenset()
) -> list[ConfigStep]:
    """Put the additive steps of a plan before the destructive ones, dropping clears the additions supersede.

    The plans are computed in the app's order (clear the old wiring, then add the new) because the `ProjectFile`
    mutators derive each edit from the file's state; on air the new wiring goes out first so a plan that stops
    half-way leaves the old link working. A `Subscription Delete` the plan re-adds later, or a
    `Publication Set 0x0000` followed by a `Publication Set` of the same model, is not sent at all — sent after
    the addition it would undo it. Within each half the steps to the battery nodes in `sleepy` go first, in plan
    order (review-3 W4 / F24): such a node is awake for a moment after a key press, so its steps must not wait
    behind the mains nodes', and a node found asleep stops the plan at its first message — in every plan here
    with one such node the plan's first message, so nothing is applied — rather than half-way through.
    """
    additive = [s for s in steps if s.additive]
    destructive = [s for s in steps if not s.additive]
    added = {
        (c.element, c.model.upper(), c.address)
        for s in additive
        if (c := s.change) is not None and c.kind == "subscribe"
    }
    published = {
        (c.element, c.model.upper())
        for s in additive
        if (c := s.change) is not None and c.kind == "publish"
    }
    kept: list[ConfigStep] = []
    for step in destructive:
        change = step.change
        assert change is not None  # destructive steps always carry their edit
        if change.kind == "unsubscribe" and (
            (change.element, change.model.upper(), change.address) in added
        ):
            continue
        if change.kind == "publish" and (
            (change.element, change.model.upper()) in published
        ):
            continue
        kept.append(step)
    return sorted(additive, key=lambda s: s.node not in sleepy) + sorted(
        kept, key=lambda s: s.node not in sleepy
    )


def replay(pf: ProjectFile, step: ConfigStep) -> None:
    """Apply an accepted step's edit to `pf` — what the node holds now — through the same mutators the plan used."""
    if step.bind is not None:
        element, model = step.bind
        raw = raw_model(_element_of(pf, element), model)
        bound = [int(b) for b in raw.get("bind", [])]
        if APP_KEY_INDEX not in bound:
            raw["bind"] = [*bound, APP_KEY_INDEX]
    change = step.change
    if change is not None:
        if change.kind == "subscribe":
            pf.subscribe(change.element, change.model, change.address)
        elif change.kind == "unsubscribe":
            pf.unsubscribe(change.element, change.model, change.address)
        else:
            target = _element_of(pf, change.element)
            pf.set_publication(
                target.node, target, change.model, change.address or None
            )


def _drop_link_rows(pf: ProjectFile, key: int) -> None:
    """Remove `key`'s room-link and scene rows: every step tagged `unlinks=key` was accepted, the old wiring is gone."""
    for dev in meta_rows(meta_list(pf.meta.get("devices"))):
        dev["cachedGroupConnectionMetadata"] = [
            r
            for r in meta_list(dev.get("cachedGroupConnectionMetadata"))
            if keeps_row(r, "elementAddress", key)
        ]
    _drop_scene_key_row(pf, key)


def _drop_scene_key_row(pf: ProjectFile, key: int) -> None:
    """Remove `key`'s `keyModeSceneConfigExports` row (review-3 W2).

    The row is what makes the app show a key as recalling "Scene N"; a key cleared or given another function
    no longer does, whatever its KeyMode still says. A file without such a row is left byte-identical.
    """
    rows = meta_list(pf.meta.get("keyModeSceneConfigExports"))
    kept = [r for r in rows if keeps_row(r, "elementAddress", key)]
    if len(kept) != len(rows):
        pf.meta["keyModeSceneConfigExports"] = kept


def _deletable(address: int) -> bool:
    """Whether a Config Model Subscription Delete can name `address`: a group, not virtual nor a fixed group.

    A virtual subscription needs the Virtual Address variant (with its Label UUID), which nothing here sends,
    and the fixed groups (all-proxies … all-nodes) are no subscription the app ever adds.
    """
    return GROUP_RANGE[0] <= address < ALL_PROXIES


def _element_of(pf: ProjectFile, address: int) -> Element:
    element = pf.cdb.element(address)
    assert element is not None  # a step is only ever built for an element the CDB has
    return element


# What the error of a stopped plan says about the messages before the one that failed (a placeholder: the
# sentence differs per outcome, and the file must always tell the truth about what the mesh holds).
APPLIED_NOTHING = "Nothing before it was applied; the mesh export is unchanged."
APPLIED_KEY_WIRED = (
    "Every connection of the key was configured and is recorded in the mesh export; only the key mode is "
    "missing — run the action again with the same target to set it."
)
APPLIED_SCENE_WIRED = (
    "The key publishes its scene recalls to all devices and that is recorded in the mesh export; the scene it "
    "recalls and its key mode are missing — run the action again with the same scene to set them."
)


def applied_text(accepted: int, total: int) -> str:
    """Describe the accepted steps of a plan that stopped after `accepted` of `total` messages."""
    if accepted == 0:
        return APPLIED_NOTHING
    return (
        f"The {accepted} of {total} messages accepted before it were applied on the mesh and are recorded in the "
        "mesh export; run the action again with the same target to complete it."
    )


def applied_removed(node: int, accepted: int, total: int) -> str:
    """After a node's reset, when the plan taking the other nodes' wiring to it away stopped after `accepted`."""
    return (
        f"Device {hexaddr(node)} was reset and the mesh export records it as removed from the network; {accepted} "
        f"of the {total} messages taking the other devices' links to it away were applied and are recorded too. "
        "The links left on the other devices point at a device that no longer answers; the mesh export keeps them, "
        "so nothing new reuses their groups."
    )


def applied_scene_stored(store: int, number: int) -> str:
    """After a Scene Store took but the JUNG description did not."""
    return (
        f"Scene {number} is stored on device {hexaddr(store)} and the member is recorded in the mesh export; only "
        "the scene description is missing — run the action again to write it."
    )


def applied_scene_cleared(
    element: int, number: int, done: int = 0, total: int = 1
) -> str:
    """After a channel's JUNG scene description was cleared but the register still holds the scene.

    `done` of the call's `total` loads forgot the scene before this one (recorded): the error says so too.
    """
    before = (
        f"{done} of {total} devices already forgot scene {number} and the mesh export records that. "
        if done
        else ""
    )
    return (
        f"{before}The scene description of {hexaddr(element)} for scene {number} was cleared; the scene itself is "
        "still stored on the device and recorded in the mesh export — run the action again to finish."
    )


def applied_scene_members(done: int, total: int, number: int) -> str:
    """After `done` of `total` loads stored scene `number` and the next one did not."""
    if done == 0:
        return APPLIED_NOTHING
    return (
        f"{done} of {total} devices stored scene {number} and are recorded in the mesh export; run the action "
        "again to finish."
    )


def applied_members(
    done: int, total: int, number: int, *, keys_cleared: bool = False
) -> str:
    """After `done` of `total` members forgot scene `number` and the next one did not.

    `keys_cleared`: the call first cleared the members' keys that recalled the scene (recorded).
    """
    if done == 0:
        if keys_cleared:
            return (
                f"The keys of these devices that recalled scene {number} were cleared and the mesh export records "
                "that; run the action again to finish."
            )
        return APPLIED_NOTHING
    return (
        f"{done} of {total} devices already forgot scene {number} and the mesh export records that; run the "
        "action again to finish."
    )


def applied_unused_deleted(deleted: dict[str, list[int]]) -> str:
    """After `delete_unused_scenes` stopped: the scenes it deleted before, by register (none of it is in the export)."""
    if not deleted:
        return APPLIED_NOTHING
    done = "; ".join(
        f"{address}: {', '.join(str(n) for n in numbers)}"
        for address, numbers in deleted.items()
    )
    return (
        f"Scenes unknown to the mesh export were already deleted before it ({done}); the mesh export is unchanged, "
        "as it never held them — run the action again to finish."
    )


# A plan's bookkeeping that is no Config step, as data (`MeshConfigurator._bookkeeping` applies it): it goes into the
# plan journal, so a record made after a crash can apply it too. `{"kind": "room", "name", "address"}` — a room the
# plan creates; `{"kind": "room_link", "key", "room", "publish", "function"}` — a room link's `meta` row;
# `{"kind": "excluded", "node", "iv_index"}` — a node that confirmed its reset.
Note = dict[str, Any]


@dataclass(frozen=True)
class KeyPlan:
    """A planned key connection: the Config messages and the KeyMode to write once they are accepted.

    `prepare` is the plan's bookkeeping that is no Config step (a room link's `meta` row), for the record of a
    plan that stops (`MeshConfigurator._send`).
    """

    steps: list[ConfigStep]
    key_mode: int
    publish: int
    mode: str
    target: str  # for the log line
    prepare: Note | None = None
    # a scene link: the scene the key's KeyModeSceneConfig names, and the `meta` row recorded once the key took it
    scene: int | None = None
    record_scene: Callable[[ProjectFile], None] | None = None


def _validation(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def _name_error(err: InvalidName, name: str) -> ServiceValidationError:
    """Return the service error for a name the app refuses (blank, a lone `%`, a rename past the sheet's limit)."""
    if err.reason == "blank":
        return _validation("service_name_blank")
    if err.reason == "too_long":
        return _validation(
            "service_name_too_long", name=name, max_length=str(RENAME_MAX_LENGTH)
        )
    return _validation("service_name_not_allowed", name=name)


def _failure(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


class _GatewayUnusable(Exception):
    """The gateway is not asked at all: its pin is not vouched for, or it rejected the token (a retry cures neither)."""

    def __init__(self, cause: str, *, token: bool = False) -> None:
        super().__init__(cause)
        self.cause = cause
        self.token = token


class _NoMergeBase(Exception):
    """`_carry_over` was asked to merge but has no base: nothing tells HA's changes from the app's."""


TOKEN_REJECTED = "the gateway no longer accepts Home Assistant's access token"  # noqa: S105 - a log text


def _listed_macs(text: str) -> set[str]:
    """Blocking: the MACs of an export's nodes, parsed as the hub will load it (every node's keys derived)."""
    net, meta = CDB.parse(text)
    return {
        mac
        for node in CDB.from_network(net, meta).nodes
        if (mac := mac_from_uuid(node.uuid)) is not None
    }


def token_rejected_open(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Whether the entry's `gateway_token_rejected` repair is open: raised since Home Assistant started, not cleared.

    An issue raised before a restart comes back from the registry inactive (it is not persistent): it is not open,
    so the first rejection after the restart is reported — and the reauth started — again.
    """
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(entry, ISSUE_GATEWAY_TOKEN)
    )
    return issue is not None and issue.active


def export_digest(doc: dict[str, Any]) -> str | None:
    """SHA-256 of a gateway export's `(meta, network)` content, canonical so whitespace or key order don't matter.

    None for a document that carries no `meta` at all — the `{"meshNetwork": …}` shape `fetch_project`'s
    `/project/cdb` fallback returns. It can never legitimately equal a share export's digest, so treating it as
    "no digest" (rather than hashing `None` for `meta`) keeps a real change from silently comparing equal to it.
    """
    network = doc.get("network")
    if not isinstance(network, str):
        return None
    net = json.loads(base64.b64decode(network))
    if isinstance(net, dict) and "meshNetwork" in net:
        net = net["meshNetwork"]
    canonical = json.dumps(
        {"meta": doc.get("meta"), "network": net},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _subscribed(element: Element, model: str, address: int) -> bool:
    return address in element.subscriptions(raw_model(element, model)["modelId"])


def element_groups(pf: ProjectFile) -> dict[int, int]:
    """Map element address -> its element group (`element group #…` in the CDB, `meta.elementConnectionGroups`)."""
    return cdb_element_groups(pf.cdb, pf.meta)


def sensor_elements(node: Node) -> list[Element]:
    """Return the node's elements with a Sensor Server (a socket's meter, a detector's sensor, a thermostat)."""
    return [e for e in node.elements if has_model(e, SENSOR_SERVER)]


def sensor_publication(cdb: CDB, meta: dict[str, Any] | None, node: Node) -> bool:
    """Whether the node's sensor values are published: any Sensor Server publishing to its own element group."""
    groups = cdb_element_groups(cdb, meta)
    for element in sensor_elements(node):
        pub = raw_model(element, SENSOR_SERVER).get("publish")
        group = groups.get(element.address)
        if (
            pub
            and group is not None
            and parse_address(str(pub.get("address"))) == group
        ):
            return True
    return False


def threshold_client(node: Node) -> Element | None:
    """Return the element a metering socket's thresholds switch through: the one with the OnOff Client (the meter's)."""
    return next((e for e in node.elements if has_model(e, ONOFF_CLIENT)), None)


def threshold_devices(cdb: CDB, client: Element, group: int) -> list[Element]:
    """Return the load elements a socket's thresholds switch: every OnOff server on the client's element group.

    The app keeps no other record (`ObserveThresholdDevices` derives them from the subscriptions the same way).
    """
    return [
        element
        for node in cdb.nodes
        for element in node.elements
        if element is not client
        and has_model(element, ONOFF_SERVER)
        and _subscribed(element, ONOFF_SERVER, group)
    ]


# what a load a socket's thresholds switch subscribes to the socket's element group, in the app's order
# (`CreateThreshold`, on air: the JUNG User Property Server first, then the OnOff server)
THRESHOLD_TARGET_MODELS = (USER_PROPERTY_SERVER, ONOFF_SERVER)


def threshold_wiring(
    cdb: CDB, client: Element, group: int
) -> list[tuple[Element, str]]:
    """Return every (element, model) the socket's threshold wiring subscribed to the client's element group.

    The loads' OnOff servers (`threshold_devices`) and their JUNG User Property Servers (`0x0527:1013`): the app
    subscribes both, and a load HA wired before it did too has the OnOff server alone. The client's own
    `0x0527:1013` listens to its group by itself (every element's does) and is not wiring. OnOff server first,
    as the app's disable unsubscribes them.
    """
    return [
        (element, model)
        for node in cdb.nodes
        for element in node.elements
        if element is not client
        for model in (ONOFF_SERVER, USER_PROPERTY_SERVER)
        if has_model(element, model) and _subscribed(element, model, group)
    ]


def derive_mode(element: Element) -> str:
    """Key mode the app would pick for a target element (`SetDeviceConnection.getKeyMode`, network-logic.md §2.1)."""
    node = element.node
    if node.pid == GATEWAY_PID:
        return MODE_GATEWAY
    if node.pid in SOCKET_PIDS:
        return MODE_SWITCH
    if has_model(element, "1300") or has_model(element, "1303"):
        return MODE_LIGHT  # dimming / tunable white
    if load_kind(element) == "blind":
        return MODE_MOVE
    if has_model(element, "1000"):
        return MODE_SWITCH
    raise _validation("service_no_mode", address=hexaddr(element.address))


def _confirms_property(params: bytes, prop: int, value: bytes) -> bool:
    """Whether an LBC Admin Property Status `[propId u16][access u8][value…]` reports `prop` == `value`."""
    return (
        len(params) >= 3 + len(value)
        and int.from_bytes(params[:2], "little") == prop
        and params[3 : 3 + len(value)] == value
    )


NODE_RESET_TIMEOUT = (
    3.0  # seconds to wait for a Node Reset Status, per attempt (three attempts)
)
# an unconfirmed reset: how long to look for the node advertising as a new device, and how often (review-4 W4-7)
RESET_ADVERT_WAIT = 5.0
RESET_ADVERT_POLL = 0.5


def _confirms_key_mode(params: bytes, key_mode: int) -> bool:
    """Whether an LBC Admin Property Status reports KeyMode == `key_mode`."""
    return _confirms_property(params, PROPERTY_KEY_MODE, bytes([key_mode]))


# what a carried-over change may hold that is never shown: a key (`netKeys[].key`, `nodes[].deviceKey`, …)
_SECRET_FIELDS = frozenset({"key", "oldKey", "deviceKey"})
# the fields of an entry that say which one it is, in this order (a room, a scene, a node, a link row)
_IDENTITY_FIELDS = (
    "address",
    "number",
    "unicastAddress",
    "elementAddress",
    "groupAddress",
    "name",
)


def held(change: Change) -> str:
    """Say what Home Assistant wrote at a conflicting change's path — what the nodes still hold — without any key.

    For the `carry_over_conflict` repair: a value the change removed is "nothing"; an entry (a room, a scene, a
    node) is named by its identifying fields only, never written out whole (a node entry carries its device key);
    a list of plain values (subscriptions) is listed; a key field is never rendered.
    """
    value = change.new
    last = change.path[-1] if change.path else None
    if value is MISSING:
        return "nothing (Home Assistant removed it)"
    if isinstance(last, str) and last in _SECRET_FIELDS:
        return "a key (not shown)"
    if isinstance(value, dict):
        named = [
            f"{field} {value[field]}"
            for field in _IDENTITY_FIELDS
            if isinstance(value.get(field), (str, int))
        ]
        return ", ".join(named) or "an entry"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if isinstance(v, (str, int))) or "none"
    return str(value)


def app_copy_path(cdb_path: str | Path) -> Path:
    """Where the app's last upload is kept beside an export set up from a gateway (`<export>.app`).

    It is the base of the three-way merge that carries Home Assistant's changes over onto the app's next upload
    (`MeshConfigurator._carry_over`): the app never downloads the project, so its uploads lack them.
    """
    path = Path(cdb_path)
    return path.with_name(path.name + ".app")


def pre_adopt_path(cdb_path: str | Path) -> Path:
    """Where the export is kept as it was before the last adoption of the gateway's export (`<export>.pre-adopt`).

    Review-3 W7: an adoption replaces Home Assistant's copy wholesale (with its changes carried over, but a
    merge can be wrong), and the change planned on it usually saves right after — the rotating backups of the
    saves soon pass the copy by. This one stays until the next adoption.
    """
    path = Path(cdb_path)
    return path.with_name(path.name + ".pre-adopt")


def load_project(cdb_path: str, metadata_dir: str | None) -> ProjectFile:
    """Blocking: read the export (either flavour) with the optional iOS app-container names overlaid.

    New rooms and scenes of the file are allocated from the top of the app's ranges (review-4 W4-2): the app never
    downloads the project, so it does not know them until it imports a file, and gives its own next room or scene
    the lowest number it believes free. With the provisioner identity on they go into Home Assistant's own ranges.
    """
    metadata = (
        Metadata(
            Path(metadata_dir) / "device_metadata.json",
            Path(metadata_dir) / "scene_metadata.json",
        )
        if metadata_dir
        else None
    )
    pf = ProjectFile.load(Path(cdb_path), metadata)
    pf.allocation = "top"
    return pf


def _step_json(step: ConfigStep) -> dict[str, Any]:
    """Return a plan step as the plan journal keeps it."""
    change = step.change
    return {
        "node": step.node,
        "pdu": step.pdu.hex(),
        "expect": step.expect,
        "change": None
        if change is None
        else {
            "element": change.element,
            "model": change.model,
            "address": change.address,
            "kind": change.kind,
        },
        "bind": None if step.bind is None else list(step.bind),
        "unlinks": step.unlinks,
    }


def _step_from_json(row: dict[str, Any]) -> ConfigStep:
    """Return the plan step `_step_json` kept."""
    change, bind, unlinks = row["change"], row["bind"], row["unlinks"]
    return ConfigStep(
        int(row["node"]),
        bytes.fromhex(row["pdu"]),
        int(row["expect"]),
        change=None
        if change is None
        else ModelChange(
            int(change["element"]),
            str(change["model"]),
            int(change["address"]),
            change["kind"],
        ),
        bind=None if bind is None else (int(bind[0]), str(bind[1])),
        unlinks=None if unlinks is None else int(unlinks),
    )


PLAN_JOURNAL_VERSION = 1
PLAN_JOURNALS: HassKey[dict[str, Store[dict[str, Any]]]] = HassKey(
    f"{DOMAIN}_plan_journals"
)


def plan_journal(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the entry's plan journal (`.storage/junghome_ble.<entry id>.plan_journal`), one instance per entry.

    `{"action", "steps", "accepted", "prepare", "happened"}` of the plan being sent (`MeshConfigurator._send`);
    gone when no plan's outcome is left unrecorded. It holds Config PDUs and addresses, no key material.
    """
    journals = hass.data.setdefault(PLAN_JOURNALS, {})
    if entry_id not in journals:
        journals[entry_id] = Store(
            hass, PLAN_JOURNAL_VERSION, f"{DOMAIN}.{entry_id}.plan_journal"
        )
    return journals[entry_id]


HELD_SCENES_VERSION = 1
HELD_SCENES: HassKey[dict[str, Store[dict[str, Any]]]] = HassKey(
    f"{DOMAIN}_held_scenes"
)


def held_scenes(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the entry's held scene numbers (`.storage/junghome_ble.<entry id>.held_scenes`), one instance per entry.

    `{"held": [[number, element], ...]}`: the scene registers a forced `delete_scene` skipped, which still hold a
    number the export no longer names (review-4 W4-8). `create_scene` does not hand such a number out again — the
    skipped device would join every recall of the new scene — and `delete_unused_scenes` lets go of a pair once the
    register no longer holds it. Numbers and addresses, no key material.
    """
    stores = hass.data.setdefault(HELD_SCENES, {})
    if entry_id not in stores:
        stores[entry_id] = Store(
            hass, HELD_SCENES_VERSION, f"{DOMAIN}.{entry_id}.held_scenes"
        )
    return stores[entry_id]


GATEWAY_SYNC_VERSION = 1
# s: a burst of syncs (an adopt and its upload) is one write; a record a restart loses is no harm (`_identical`)
GATEWAY_SYNC_SAVE_DELAY = 1.0
GATEWAY_SYNCS: HassKey[dict[str, GatewaySync]] = HassKey(f"{DOMAIN}_gateway_syncs")


class GatewaySync:
    """What Home Assistant last exchanged with an entry's gateway (`.storage/junghome_ble.<entry id>.gateway_sync`).

    `synced`: the content digest (`export_digest`) the gateway and the file last both held; `last_sync`: when Home
    Assistant last uploaded its export (ISO 8601, UTC; the app's `gateway_last_sync`, the *Last export upload*
    sensor, told through `SIGNAL_GATEWAY_SYNCED`). Review-4 H I-10: both lived in `entry.data` up to 1.0.0, so
    every sync rewrote the config entries file and woke every listener of the entry. The first load takes them over
    from there (a new gateway entry's flow seeds `CONF_GATEWAY_SYNCED` the same way); the keys stay in `entry.data`,
    so a downgrade reads the value they had then. A digest, a time, no key material.
    """

    def __init__(
        self, hass: HomeAssistant, entry_id: str, store: Store[dict[str, Any]]
    ) -> None:
        """Bind to the entry's store; nothing is read until `async_load`."""
        self.hass = hass
        self.entry_id = entry_id
        self.store = store
        self.synced: str | None = None
        self.last_sync: str | None = None
        self.loaded = False

    def _data(self) -> dict[str, Any]:
        return {"synced": self.synced, "last_sync": self.last_sync}

    async def async_load(self, entry: ConfigEntry) -> None:
        """Read the record once per Home Assistant run (setup reads it); an entry without one takes `entry.data`'s."""
        if self.loaded:
            return
        data = await self.store.async_load()
        if data is None:
            data = {
                "synced": entry.data.get(CONF_GATEWAY_SYNCED),
                "last_sync": entry.data.get(CONF_GATEWAY_LAST_SYNC),
            }
            if any(v is not None for v in data.values()):
                await self.store.async_save(data)
        self.synced, self.last_sync = data.get("synced"), data.get("last_sync")
        self.loaded = True

    async def async_seed(self, entry: ConfigEntry, digest: str | None) -> None:
        """Record the digest of an export a reconfigure fetched from the gateway (the flow, before the reload)."""
        await self.async_load(entry)
        self.synced = digest
        await self.store.async_save(self._data())

    @callback
    def record(self, digest: str, *, uploaded: bool = False) -> None:
        """Record `digest` as synced; after an upload also its time, which the *Last export upload* sensor hears of."""
        self.synced = digest
        if uploaded:
            self.last_sync = datetime.now(UTC).isoformat()
        self.store.async_delay_save(self._data, GATEWAY_SYNC_SAVE_DELAY)
        if uploaded:
            async_dispatcher_send(
                self.hass, SIGNAL_GATEWAY_SYNCED.format(self.entry_id)
            )


def gateway_sync(hass: HomeAssistant, entry_id: str) -> GatewaySync:
    """Return the entry's `GatewaySync` record, one instance per entry (it outlives the entry's reloads)."""
    records = hass.data.setdefault(GATEWAY_SYNCS, {})
    if entry_id not in records:
        records[entry_id] = GatewaySync(
            hass,
            entry_id,
            Store(hass, GATEWAY_SYNC_VERSION, f"{DOMAIN}.{entry_id}.gateway_sync"),
        )
    return records[entry_id]


async def async_remove_gateway_sync(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the entry's `GatewaySync` record, with the entry."""
    await gateway_sync(hass, entry_id).store.async_remove()
    hass.data[GATEWAY_SYNCS].pop(entry_id, None)


async def run_to_end[T](work: Coroutine[Any, Any, T]) -> T:
    """Await `work` to its end even when the caller is cancelled meanwhile, then pass the cancellation on (D12).

    For what must not stop half-way once the mesh holds a change: the write that records it, the update (or
    reload) that makes the device model follow it. `asyncio.shield` returns at the first cancellation and leaves the work
    running behind the caller's back — past the lock the caller holds; here the caller keeps waiting, as often
    as it is cancelled, and raises `CancelledError` once the work is done (chained to the work's own error,
    should it fail). A cancellation of the work itself (the loop shutting down) ends the wait at once.
    """
    task = asyncio.ensure_future(work)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as err:
            cancelled = err
    if cancelled is None:
        return task.result()
    if not task.cancelled() and (error := task.exception()) is not None:
        raise cancelled from error
    raise cancelled


class MeshConfigurator:
    """Rooms, key connections and scenes of one hub; operations are serialised by `lock`.

    The mutating coroutines return True when the device model changed, which is the caller's cue to have the hub
    follow the export (an adopted gateway export changes it too, see `adopted`); `create_room` touches only the
    file's bookkeeping and returns the new room's address instead.
    """

    def __init__(self, hub: JungHomeHub) -> None:
        """Bind to `hub`; nothing is loaded until an operation runs."""
        self.hub = hub
        self.lock = asyncio.Lock()
        # whether the running operation wrote the export: `services._run` has the model follow it after a stopped
        # plan that recorded what the mesh accepted, as after a finished one (the device model must follow the file)
        self.recorded = False
        # whether it adopted the gateway's export first: the device model changed even when the change itself
        # then had nothing to do
        self.adopted = False
        # whether the plan journal holds a plan whose outcome the export does not record yet
        self.journaled = False
        hub.configurator = self  # the hub's unknown-node refresh adopts the gateway's export through us

    # ------------------------------------------------------------------ project file
    @property
    def path(self) -> str:
        """The export the config entry points at."""
        return str(self.hub.entry.data[CONF_CDB_PATH])

    async def _load(self, *, fresh: bool = False) -> ProjectFile:
        """Read the export a mutation plans against: the gateway's when that is newer, else the copy on disk.

        Every mutation starts here, under the lock. `recorded` and `adopted` are not reset here but per call
        (`services._run`): one call can run several mutations (`set_threshold` wires socket by socket), and a
        later one that fails must not hide the export an earlier one wrote. `fresh`: a gateway entry whose gateway
        did not answer is refused instead of planning on the copy on disk, which may lack what the app made since
        (review-4 W4-3: what judges by what the export *lacks* must not fall back silently).
        """
        answered = await self._adopt_gateway_export()
        if fresh and not answered and self.gateway is not None:
            raise _failure("service_gateway_export_unavailable")
        pf = await self._read()
        await self._with_identity(pf)
        return pf

    async def _read(self) -> ProjectFile:
        try:
            return await self.hub.hass.async_add_executor_job(
                load_project,
                self.path,
                self.hub.entry.data.get(CONF_METADATA_DIR) or None,
            )
        except InvalidExport as err:
            raise _failure(
                "service_export_load_failed", path=self.path, error=str(err)
            ) from err
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
            ExportError,
        ) as err:
            raise _failure(
                "service_export_load_failed",
                path=self.path,
                error=f"{type(err).__name__}: {err}",
            ) from err

    async def _save(self, pf: ProjectFile, *, upload: bool = True) -> None:
        """Write `pf`, then hand it to the gateway; once started, the write runs to its end whatever cancels the call.

        The file is what the mesh holds (D12): a call cancelled half-way through writing it would leave the nodes
        ahead of it; once written, the plan journal is done with. The upload is not held to its end: cancelled,
        skipped while Home Assistant stops (it would hold the shutdown up for a gateway that may not answer) or
        not asked for (`upload=False`), it is left to `sync_gateway` or the next change, which uploads the export
        as it is on disk then.
        """
        try:
            await run_to_end(self._save_file(pf))
        except asyncio.CancelledError:
            if self.gateway is not None:
                _LOGGER.warning(
                    "%s was written but not handed to the gateway (cancelled); sync_gateway or the next change does",
                    self.path,
                )
            raise
        if self.gateway is None or not upload:
            return
        if self.hub.hass.is_stopping:
            _LOGGER.warning(
                "%s was written but not handed to the gateway: Home Assistant is stopping; sync_gateway or the next "
                "change does",
                self.path,
            )
            return
        await self._upload_or_retry(pf)

    async def _save_file(self, pf: ProjectFile) -> None:
        await self._with_identity(pf)
        await self._write(pf)
        self.recorded = True
        await self._journal_close()

    # ------------------------------------------------------------------ plan journal (W I1)
    @property
    def _journal(self) -> Store[dict[str, Any]]:
        return plan_journal(self.hub.hass, self.hub.entry.entry_id)

    async def _journal_save(self, data: dict[str, Any]) -> None:
        # a copy: a save deferred to Home Assistant's final write must not see the next step's count
        await self._journal.async_save(dict(data))
        self.journaled = True

    async def _journal_close(self) -> None:
        """Remove the plan journal: the export records the plan's outcome, or the mesh holds nothing of it."""
        if self.journaled:
            # emptied first: `Store` keeps a write still pending (deferred while Home Assistant stops) or data it
            # loaded, and would hand that back to the next load in this run even after the file is gone
            await self._journal.async_save({})
            await self._journal.async_remove()
            self.journaled = False

    def _bookkeeping(self, record: ProjectFile, note: Note) -> None:
        """Apply a plan's bookkeeping that is no Config step (`Note`) to `record`; once applied, again is a no-op."""
        if note["kind"] == "room":
            if note["address"] not in record.cdb.groups:
                record.add_group(note["name"], address=note["address"])
        elif note["kind"] == "room_link":
            self._record_room_link(
                record,
                _element_of(record, note["key"]),
                note["room"],
                note["publish"],
                note["function"],
                keep=True,
            )
        elif (reset := record.cdb.node_by_addr(note["node"])) is not None:
            # "excluded": the node the plan's `_load` found, read again; a record that has it excluded no longer lists it
            record.exclude_node(reset, note["iv_index"])

    async def async_replay_journal(self) -> bool:
        """At setup: record what a plan Home Assistant stopped or crashed in the middle of left on the mesh.

        The journal says which plan ran and how many of its steps the nodes had accepted; they are replayed into a
        fresh read of the export as `_record` does after a stop (idempotent: a crash during this replay replays
        the same again), and a repair issue names the interrupted action. The record is not handed to the
        gateway here — the link that vouches for it is not up yet — but left to `sync_gateway` or the next
        change. A record that cannot be written keeps the journal for the next start; an unreadable journal is
        dropped. True when the export was written: the caller sets the entry up again from it.
        """
        data = await self._journal.async_load()
        if not data:
            return False
        self.journaled = True
        try:
            plan = [_step_from_json(row) for row in data["steps"]]
            accepted = plan[: int(data["accepted"])]
            action = str(data["action"])
            prepare, happened = data.get("prepare"), data.get("happened")
        except (KeyError, TypeError, ValueError, IndexError) as err:
            _LOGGER.warning("Dropping an unreadable plan journal: %s", err)
            await self._journal_close()
            return False
        async with self.lock:
            self.recorded = False
            try:
                await self._record(
                    accepted, plan, prepare=prepare, happened=happened, upload=False
                )
            except HomeAssistantError as err:
                _LOGGER.error(
                    "%s was interrupted after %d of %d Config messages; they could not be recorded in %s yet "
                    "(tried again at the next start): %s",
                    action,
                    len(accepted),
                    len(plan),
                    self.path,
                    err,
                )
                return False
            except Exception:
                # the replay must not stop the setup; it would fail the same way at every start
                _LOGGER.exception(
                    "Dropping a plan journal that does not apply to %s", self.path
                )
                await self._journal_close()
                return False
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_PLAN_INTERRUPTED),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PLAN_INTERRUPTED,
            translation_placeholders={
                "title": self.hub.entry.title,
                "action": action,
                "accepted": str(len(accepted)),
                "total": str(len(plan)),
            },
        )
        return self.recorded

    # ------------------------------------------------------------------ held scene numbers (review-4 W4-8)
    async def _held_scenes(self) -> set[tuple[int, int]]:
        """(scene number, register element) of every register a forced `delete_scene` skipped and that may hold it."""
        data = await held_scenes(self.hub.hass, self.hub.entry.entry_id).async_load()
        try:
            return {(int(n), int(e)) for n, e in (data or {}).get("held", [])}
        except (TypeError, ValueError, AttributeError) as err:
            _LOGGER.warning(
                "Ignoring an unreadable record of held scene numbers: %s", err
            )
            return set()

    async def _hold_scenes(self, pairs: set[tuple[int, int]]) -> None:
        """Keep `pairs` as the held scene numbers and let the `scene_held` repair name them (cleared when none).

        An empty record is written rather than the file removed: `Store` would hand data it loaded back to the
        next load in this run.
        """
        await held_scenes(self.hub.hass, self.hub.entry.entry_id).async_save(
            {"held": sorted([n, e] for n, e in pairs)}
        )
        issue = issue_id(self.hub.entry, ISSUE_SCENE_HELD)
        if not pairs:
            ir.async_delete_issue(self.hub.hass, DOMAIN, issue)
            return
        by_number: dict[int, list[int]] = {}
        for number, element in sorted(pairs):
            by_number.setdefault(number, []).append(element)
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue,
            is_fixable=False,
            # the record outlives a restart, so must the issue: nothing raises it again at setup
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SCENE_HELD,
            translation_placeholders={
                "title": self.hub.entry.title,
                "members": "; ".join(
                    f"{number}: {', '.join(self._member_name(e) for e in elements)}"
                    for number, elements in by_number.items()
                ),
            },
        )

    def _member_name(self, element: int) -> str:
        """`0232 (Kitchen)`: a register element by its address, with the name of its load when the hub has one."""
        device = self.hub.devices.by_address.get(element)
        if device is None:
            return hexaddr(element)
        return f"{hexaddr(element)} ({device.name})"

    async def _write(self, pf: ProjectFile) -> None:
        """Write `pf` to the entry's export (the app's last upload kept first as the merge base, with a gateway)."""
        if self.gateway is not None:
            # the file before Home Assistant's first change is the app's last upload: the merge base from now on
            await self.hub.hass.async_add_executor_job(self._keep_app_copy)
        try:
            await self.hub.hass.async_add_executor_job(pf.save)
        except NewerExportError as err:
            raise _failure("service_export_newer", path=self.path) from err
        except (OSError, ExportError) as err:
            raise _failure(
                "service_export_write_failed", path=self.path, error=str(err)
            ) from err
        _LOGGER.info("Wrote the mesh export %s", self.path)

    # ------------------------------------------------------------------ provisioner identity (review-3 N1)
    @property
    def identity_enabled(self) -> bool:
        """Whether the entry's *provisioner identity* option is on (off by default: nothing of it reaches a file)."""
        return bool(
            self.hub.entry.options.get(
                OPTION_PROVISIONER_IDENTITY, DEFAULT_PROVISIONER_IDENTITY
            )
        )

    async def _with_identity(self, pf: ProjectFile) -> bool:
        """With the option on, put Home Assistant's provisioner entry, its node and the vault's nodes into `pf`.

        `jhmesh.vault.Vault.merge_into`, idempotent: True when `pf` changed. Groups and scenes of `pf` are then
        allocated in Home Assistant's ranges. A file no range fits (Home Assistant's address taken, a space full)
        or that would not load with them is left as it is, with a warning — the change it is part of goes on. The
        vault is saved when the choice of ranges or its nodes changed. With the option off nothing happens here.
        """
        if not self.identity_enabled:
            return False
        keeper = self.hub.vault
        vault = keeper.identity()
        try:
            result = await self.hub.hass.async_add_executor_job(
                vault.merge_into, pf, self.hub.proxy.state.src
            )
        except (RangeError, InvalidExport) as err:
            _LOGGER.warning(
                "Home Assistant's provisioner entry was left out of %s: %s",
                self.path,
                err,
            )
            return False
        except Exception as err:
            # `merge_into` put the file back as it was; the action stops here rather than write half a merge.
            # Only the type is logged: a malformed stored entry may carry a device key
            _LOGGER.error(
                "Merging Home Assistant's provisioner entry into %s failed (%s)",
                self.path,
                type(err).__name__,
            )
            raise _failure(
                "provisioner_identity_failed", error=type(err).__name__
            ) from err
        for uuid in result.stale:
            vault.forget(uuid)
            _LOGGER.info(
                "The export holds node %s in another shape than Home Assistant recorded it (removed or provisioned "
                "anew); the vault's copy is dropped",
                uuid,
            )
        for uuid, why in result.skipped.items():
            _LOGGER.warning(
                "The node %s Home Assistant provisioned was not put back into %s: %s",
                uuid,
                self.path,
                why,
            )
        await keeper.async_save()
        return result.changed

    async def _identity_text(self, text: str) -> tuple[str, int]:
        """`_with_identity` on an export's text (an adopted gateway export): (the text, 1 when it changed, else 0)."""
        try:
            pf = await self.hub.hass.async_add_executor_job(
                ProjectFile.loads, text.encode()
            )
        except (InvalidExport, ValueError, KeyError, TypeError, AttributeError) as err:
            _LOGGER.warning(
                "Home Assistant's provisioner entry was not added to the gateway's export: %s",
                err,
            )
            return text, 0
        if not await self._with_identity(pf):
            return text, 0
        pf.touch()
        return pf.render(), 1

    async def async_identity_ranges(self) -> Ranges:
        """Home Assistant's ranges in the export on disk, chosen now if need be (and kept in the vault).

        For `add_device` with the option on: the new node goes into Home Assistant's unicast range and its element
        groups into its group range. A translated error when no range fits.
        """
        async with self.lock:
            pf = await self._read()
            keeper = self.hub.vault
            try:
                ranges = await self.hub.hass.async_add_executor_job(
                    keeper.identity().ensure_ranges, pf, self.hub.proxy.state.src
                )
            except RangeError as err:
                raise _failure("provisioner_identity_no_range", error=str(err)) from err
            await keeper.async_save()
            return ranges

    def _keep_app_copy(self) -> None:
        """Blocking: copy the export on disk to `app_copy_path` unless one is kept already.

        Called right before a save, after `_load` read the file: it exists.
        """
        target = app_copy_path(self.path)
        if not target.exists():
            write_private(target, Path(self.path).read_bytes())

    def _keep_pre_adopt_copy(self) -> None:
        """Blocking: copy the export on disk to `pre_adopt_path`, right before an adopted gateway export replaces it.

        `_gateway_state` digested the file just before: it exists (an `OSError` otherwise fails the adoption).
        """
        write_private(pre_adopt_path(self.path), Path(self.path).read_bytes())

    def _carry_over(
        self, text: str, required: bool = False
    ) -> tuple[str, int, list[Change]]:
        """Blocking: put Home Assistant's changes onto the gateway's export `text`; (result, applied, conflicts).

        Review-3 W1: the app uploads its whole project after every change but never downloads one, so its upload
        lacks what Home Assistant changed since — which the nodes still hold. The changes are the difference
        between the app's previous upload (`app_copy_path`) and the file on disk; `jhmesh.merge` applies them
        to the new upload, and a change the app overrode meanwhile (its Config messages went out later) is left
        as the app has it and reported. Without a kept copy, or with an unreadable one, the upload is taken as
        it is (the behaviour before the merge existed) — unless `required` (the file changed since the last
        sync, too): then `_NoMergeBase`, and the caller refuses rather than drop HA's changes.
        """
        base_path = app_copy_path(self.path)
        try:
            base = ProjectFile.load(base_path)
            ours = ProjectFile.load(Path(self.path))
            theirs = ProjectFile.loads(text.encode())
        except FileNotFoundError as err:
            if required:
                raise _NoMergeBase from err
            return text, 0, []
        except (
            InvalidExport,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
            ExportError,
        ) as err:
            _LOGGER.warning(
                "Home Assistant's changes were not carried over onto the gateway's export: %s",
                err,
            )
            if required:
                raise _NoMergeBase from err
            return text, 0, []
        changes = diff_documents(base.snapshot(), ours.snapshot())
        if not changes:
            return text, 0, []
        doc = {"network": theirs.net, "meta": theirs.meta}
        before = theirs.snapshot()
        applied, conflicts = apply_changes(doc, changes)
        if theirs.snapshot() == before:
            return text, 0, conflicts
        theirs.touch()
        return theirs.render(), len(applied), conflicts

    # ------------------------------------------------------------------ gateway sync (roadmap step 14)
    @property
    def gateway(self) -> JungHomeGatewayApi | None:
        """The entry's gateway client, or None when the entry was not set up from a gateway."""
        return api_for_entry(self.hub.hass, self.hub.entry)

    async def _upload_or_retry(self, pf: ProjectFile) -> None:
        """Hand `pf` to the gateway after a change; a failure is tried again in the background, as the app does.

        The app retries a failed upload twice, 15 s apart (`ProjectFileSyncServiceImpl`: flow `retry(2)`, delay
        15 000 ms), whatever failed. So does this, for the failures a later attempt can fix: the gateway could not
        be asked or refused the POST (unreachable, busy with another configuration request, an HTTP error). A
        refusal of ours is final: a gateway holding changes HA has not seen, a pin it contradicts, a rejected
        token — the repairs say what to do. A later change, or `sync_gateway`, supersedes a pending retry.

        The retry is Home Assistant's task, not the entry's, and kept in `hass.data` by entry id: a change the hub
        cannot follow in place reloads the entry right after (`services._run`), which replaces the hub and this
        configurator while the retry waits, and would cancel a task of the entry's.
        """
        self.cancel_upload_retry()
        if await self._upload(pf, raise_on_failure=False) == "failed":
            hass, entry_id = self.hub.hass, self.hub.entry.entry_id
            hass.data.setdefault(UPLOAD_RETRIES, {})[entry_id] = (
                hass.async_create_background_task(
                    _retry_upload(hass, entry_id, GATEWAY_UPLOAD_RETRIES),
                    f"{DOMAIN} gateway upload retry",
                )
            )

    @property
    def upload_retry(self) -> asyncio.Task[None] | None:
        """The entry's pending retry of a failed automatic upload, if any (`_upload_or_retry`)."""
        return self.hub.hass.data.get(UPLOAD_RETRIES, {}).get(self.hub.entry.entry_id)

    def cancel_upload_retry(self) -> None:
        """Drop the entry's pending retry of a failed upload: a newer upload supersedes it."""
        cancel_upload_retry(self.hub.hass, self.hub.entry.entry_id)

    async def _upload(  # noqa: PLR0911  # one outcome per check
        self, pf: ProjectFile, *, raise_on_failure: bool
    ) -> Literal["synced", "failed", "refused"]:
        """Hand the export to the gateway, after checking it still holds what HA last synced (or what the file holds).

        The gateway rebuilds its whole installation from what is POSTed, so uploading over a change the app made
        since would erase it — the check that guards `_adopt_gateway_export` runs again here, right before the
        POST, because time passes between planning and saving. Nothing goes to a gateway whose pin the gateway
        node has not vouched for (`JungHomeHub.async_gateway_distrust`), nor with a token it rejected. Returns
        (unless `raise_on_failure` raised) "synced", "failed" when trying again later may work (the gateway could
        not be asked, or refused the POST), or "refused" when it cannot (`_upload_or_retry`).
        """
        try:
            state = await self._gateway_state()
        except _GatewayUnusable as err:
            api = self.gateway
            assert api is not None
            if err.token:
                if raise_on_failure:
                    raise _failure(
                        "service_gateway_token_rejected", host=api.host
                    ) from err
                return "refused"
            self._sync_refused(
                api,
                err.cause,
                raise_on_failure,
                "service_gateway_sync_failed",
                host=api.host,
                error=err.cause,
            )
            return "refused"
        api = (
            self.gateway
        )  # after the check: it may have followed the gateway to a new address
        assert api is not None
        if state is None:
            self._sync_refused(
                api,
                "the gateway could not be checked",
                raise_on_failure,
                "service_gateway_sync_failed",
                host=api.host,
                error="the gateway could not be checked",
            )
            return "failed"
        _text, stamp, gateway_digest, disk_digest = state
        if gateway_digest is not None and gateway_digest == disk_digest:
            self._identical(gateway_digest)
        elif gateway_digest != self._synced_digest(disk_digest):
            disk_stamp = await self._disk_timestamp_or_none()
            self._sync_refused(
                api,
                "the gateway holds changes Home Assistant has not seen; fetch the export again",
                raise_on_failure,
                "service_gateway_export_newer",
                host=api.host,
                gateway=stamp,
                file=disk_stamp or "no timestamp",
            )
            return "refused"
        doc = json.loads(pf.share_json())
        try:
            await api.upload_project(doc)
        except GatewayAuthError as err:
            self.report_token_rejected(api)
            if raise_on_failure:
                raise _failure("service_gateway_token_rejected", host=api.host) from err
            return "refused"
        except GatewayError as err:
            _LOGGER.warning(
                "The changed mesh export could not be handed to the gateway %s: %s — the app will show the old "
                "state until an upload succeeds (a change's is tried again twice, or `junghome_ble.sync_gateway`)",
                api.host,
                err,
            )
            ir.async_create_issue(
                self.hub.hass,
                DOMAIN,
                issue_id(self.hub.entry, ISSUE_GATEWAY_SYNC),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_GATEWAY_SYNC,
                translation_placeholders={"host": api.host, "error": str(err)},
            )
            if raise_on_failure:
                raise _failure(
                    "service_gateway_sync_failed", host=api.host, error=str(err)
                ) from err
            return "failed"
        # the per-entry issue, and the one issue of every entry before it was per entry
        for issue in (issue_id(self.hub.entry, ISSUE_GATEWAY_SYNC), ISSUE_GATEWAY_SYNC):
            ir.async_delete_issue(self.hub.hass, DOMAIN, issue)
        uploaded_digest = export_digest(doc)
        assert uploaded_digest is not None  # `pf.share_json()` always carries `meta`
        self._mark_synced(uploaded_digest, uploaded=True)
        _LOGGER.info("Handed the mesh export to the gateway %s", api.host)
        return "synced"

    def _sync_refused(
        self,
        api: JungHomeGatewayApi,
        cause: str,
        raise_on_failure: bool,
        key: str,
        **placeholders: str,
    ) -> None:
        """Raise the repair issue for an upload that did not go out, and the translated error when asked to."""
        _LOGGER.warning(
            "The changed mesh export was not handed to the gateway %s: %s",
            api.host,
            cause,
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue_id(self.hub.entry, ISSUE_GATEWAY_SYNC),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GATEWAY_SYNC,
            translation_placeholders={"host": api.host, "error": cause},
        )
        if raise_on_failure:
            raise _failure(key, **placeholders)

    def report_token_rejected(self, api: JungHomeGatewayApi) -> None:
        """Raise the repair for a token the gateway rejects, and start Home Assistant's reauthentication.

        Logged, and the reauth flow started, once per outage — while the repair is open: a change's check,
        `sync_gateway` and the status polls all end up here (and Home Assistant starts no second reauth flow while
        one is in progress). Not `ConfigEntryAuthFailed`: that would stop the entry, and only the gateway sync
        needs the token — the mesh keeps working. The reauth flow's success clears the repair. A reload aborts the
        flow; the set-up entry starts it again while the repair is open (`__init__.async_setup_entry`).
        """
        hass, entry = self.hub.hass, self.hub.entry
        first = not token_rejected_open(hass, entry)
        if first:
            _LOGGER.warning(
                "The gateway %s no longer accepts Home Assistant's access token: the export is not handed to it "
                "until access is granted again (Home Assistant asks for it: Settings → Devices & services)",
                api.host,
            )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry, ISSUE_GATEWAY_TOKEN),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GATEWAY_TOKEN,
            translation_placeholders={"host": api.host, "title": entry.title},
        )
        if first:
            entry.async_start_reauth(hass)

    async def _gateway_state(self) -> tuple[str, str, str | None, str | None] | None:
        """(text, stamp, gateway digest, disk digest) of the gateway's export against what is on disk.

        None when the gateway cannot be asked, its export does not parse, or belongs to another mesh
        (`_gateway_export` already logs why); `_GatewayUnusable` when it must not be asked. A legacy entry
        (nothing synced yet) gets a baseline here: if the gateway is not ahead by the old timestamp rule, nothing
        has changed as far as HA can tell, so today's digest becomes the baseline; otherwise the digest stays
        unset for this call, and `_synced_digest` falls back to the disk digest, which treats the whole
        difference as the gateway's change alone — the best a legacy entry can do without history.
        """
        await self._sync.async_load(self.hub.entry)
        fetched = await self._gateway_export()
        if fetched is None:
            return None
        text, stamp = fetched
        gateway_digest = export_digest(json.loads(text))
        try:
            disk_digest = await self.hub.hass.async_add_executor_job(
                self._digest_on_disk
            )
        except OSError:
            return None  # unreadable file: not a judgeable "changed" — `_read` explains it, next
        if gateway_digest is not None and self._sync.synced is None:
            try:
                on_disk_stamp = await self.hub.hass.async_add_executor_job(
                    self._timestamp_on_disk
                )
            except OSError:
                on_disk_stamp = None
            newer = bool(stamp) and (
                on_disk_stamp is None or timestamp_advanced(stamp, on_disk_stamp)
            )
            if not newer:
                self._mark_synced(gateway_digest)
        return text, stamp, gateway_digest, disk_digest

    @property
    def _sync(self) -> GatewaySync:
        """The entry's record of what it last exchanged with the gateway (`_gateway_state` loads it)."""
        return gateway_sync(self.hub.hass, self.hub.entry.entry_id)

    def _synced_digest(self, disk_digest: str | None) -> str | None:
        """Return what HA last synced with the gateway; a legacy entry with nothing recorded falls back to the disk digest."""
        synced = self._sync.synced
        return synced if synced is not None else disk_digest

    def _digest_on_disk(self) -> str | None:
        """Blocking: the digest of the export on disk (None when it parses but carries no `meta`).

        Raises `OSError` for a file that cannot even be read — `_gateway_state`'s caller lets that fail the
        whole check rather than judge "changed" from nothing, the same way a failed gateway fetch does.
        """
        try:
            return export_digest(json.loads(Path(self.path).read_bytes()))
        except ValueError:
            return None

    def _mark_synced(self, digest: str, *, uploaded: bool = False) -> None:
        """Record the digest HA last exchanged with the gateway in the entry's `GatewaySync` record, not `entry.data`.

        After an upload also its time (what the app keeps as `gateway_last_sync` and the *Last export upload* sensor
        shows); an adopted gateway export is no upload and leaves it.
        """
        self._sync.record(digest, uploaded=uploaded)

    def _identical(self, digest: str) -> None:
        """Record the export both the gateway and the file hold as synced, whatever the record says (review-4 S4-6).

        An upload whose record was lost (Home Assistant stopped between the POST and the record's delayed save)
        left the record behind both copies: judged by it, every later change was refused as "both changed".
        """
        if self._sync.synced != digest:
            _LOGGER.info(
                "The gateway holds the export on disk: recorded as synced with it"
            )
            self._mark_synced(digest)

    async def _gateway_export(self) -> tuple[str, str] | None:
        """Ask the gateway for this mesh's export: (text, CDB timestamp), or None when there is nothing usable.

        None when the entry has no gateway, the gateway cannot be asked (logged; the upload after the change
        will raise the repair issue and `sync_gateway` asks again), what it hands out does not parse or is
        another mesh's export. `_GatewayUnusable` when it must not be asked: its pin is not vouched for, it
        presents another certificate (the `gateway_certificate_changed` repair), or it rejects the token (the
        `gateway_token_rejected` repair). An answer clears both repairs: the pinned gateway took the token.
        """
        api = self.gateway
        if api is None:
            return None
        if (distrust := await self.hub.async_gateway_distrust()) is not None:
            raise _GatewayUnusable(distrust)
        try:
            try:
                doc = await api.fetch_project()
            except (GatewayUnreachable, GatewayCertificateMismatch):
                # moved to another address: the gateway node says where it is now
                if not await self.hub.async_follow_gateway():
                    raise
                followed = self.gateway
                assert (
                    followed is not None
                )  # the entry still names the gateway it followed
                api = followed
                doc = await api.fetch_project()
        except GatewayAuthError as err:
            self.report_token_rejected(api)
            raise _GatewayUnusable(TOKEN_REJECTED, token=True) from err
        except GatewayCertificateMismatch as err:
            self.hub.async_raise_certificate_issue()
            raise _GatewayUnusable(
                "it presents another certificate than the pinned one"
            ) from err
        except GatewayError as err:
            _LOGGER.warning(
                "Could not ask the gateway %s for its export: %s", api.host, err
            )
            return None
        for issue in (ISSUE_GATEWAY_TOKEN, ISSUE_GATEWAY_CERTIFICATE):
            ir.async_delete_issue(
                self.hub.hass, DOMAIN, issue_id(self.hub.entry, issue)
            )
        text = json.dumps(doc)
        try:
            net, _meta = CDB.parse(text)
            mesh_uuid = str(net.get("meshUUID", ""))
        except (InvalidExport, ValueError, KeyError, TypeError, AttributeError) as err:
            _LOGGER.warning(
                "The gateway %s handed out an export that does not parse: %s",
                api.host,
                err,
            )
            return None
        if mesh_uuid.lower() != self.hub.cdb.mesh_uuid.lower():
            _LOGGER.warning(
                "The gateway %s holds the export of another mesh (%s); not used",
                api.host,
                mesh_uuid,
            )
            return None
        return text, str(net.get("timestamp", ""))

    def _timestamp_on_disk(self) -> str | None:
        """Blocking: the CDB timestamp of the export on disk (None when the bytes are not an export)."""
        return ProjectFile.file_timestamp(Path(self.path).read_bytes())

    async def _disk_timestamp_or_none(self) -> str | None:
        """`_timestamp_on_disk`, for a "both changed" error message only: an unreadable file names no timestamp."""
        try:
            return await self.hub.hass.async_add_executor_job(self._timestamp_on_disk)
        except OSError:
            return None

    async def _adopt_gateway_export(self) -> bool:
        """Replace the copy on disk by the gateway's export when it, and only it, changed since HA last synced.

        The app uploads its project to the gateway after every change (network-features.md §8.2) and the file of
        an entry set up from a gateway is written by nobody else, so a gateway digest that differs from what HA
        last synced means the app added, renamed or linked something HA does not know yet. Planning on HA's copy
        and uploading the result would make the gateway "rebuild its device database" without those changes — and
        the gateway would then serve an export without them. Adopting first keeps them; the plan is then
        made on the gateway's copy (the hub's device model follows it after the change). When HA's
        own copy changed too (an earlier upload never reached the gateway), HA's changes are carried over onto it
        all the same (`_adopt`); only without the app's previous upload to tell them apart is the change refused
        until the entry is fetched again. A gateway that must not be asked is not: the plan is
        made on disk, and the upload after the change reports why it did not go out. True when the copy on disk
        now holds what the gateway's export holds: adopted, or the gateway unchanged since HA last synced; False
        without a gateway, when it was not asked or did not answer, and for a bare database left beside a bare
        file (nothing tells what either lacks).
        """
        try:
            state = await self._gateway_state()
        except _GatewayUnusable as err:
            _LOGGER.debug("Planning on the copy on disk: %s", err.cause)
            return False
        if state is None:
            return (
                False  # the upload after the change decides later, from its own check
            )
        if await self._adopt(state, "before the change"):
            self.recorded = self.adopted = True
            return True
        return state[2] is not None

    async def _adopt(
        self,
        state: tuple[str, str, str | None, str | None],
        purpose: str,
        *,
        bare: bool = False,
    ) -> bool:
        """Write the gateway's export (`_gateway_state`), HA's changes carried over, over the copy on disk; True when written.

        Only when the gateway changed since HA last synced, and not into what the file holds already (identical
        content is recorded as synced). When the file changed too — an upload that never reached the gateway —
        HA's changes are carried over all the same, as when only the gateway changed (review-4 S4-6: refusing
        left fetching again as the only way out, which dropped them): the app's previous upload (`app_copy_path`)
        tells them apart, and a change the app overrode is reported (`_report_conflicts`); without that copy the
        change is refused (`service_gateway_export_newer`). A gateway export without `meta` (the bare
        `/project/cdb` database) never replaces a share export; over a file without one either it is taken only
        when `bare` (the unknown-node refresh — a change plans on such a file as it is). The file is written
        atomically with its backups — the copy it replaces kept apart as well (`pre_adopt_path`) — and the digest
        recorded as synced. Unverified on air: no app has imported a file merged this way yet.
        """
        text, stamp, gateway_digest, disk_digest = state
        api = self.gateway
        assert api is not None  # `_gateway_state` is None without one
        both_changed = False
        if gateway_digest is None:
            if disk_digest is None and not bare:
                return False  # the disk copy has nothing worth protecting either; plan on it as today
            if disk_digest is not None:
                _LOGGER.warning(
                    "The gateway %s answered with the bare device database only (no device names or room "
                    "links); not adopted over the share export on disk",
                    api.host,
                )
                raise _failure("service_gateway_export_incomplete", host=api.host)
        elif disk_digest is not None:
            # the disk holds a real export: compare by content against what HA last synced with the gateway
            synced = self._synced_digest(disk_digest)
            if gateway_digest == disk_digest:
                self._identical(gateway_digest)
                return False  # the same export on both sides: plan on disk
            if gateway_digest == synced:
                return False  # unchanged: plan on disk
            both_changed = disk_digest != synced
        # disk_digest is None: the file on disk is not a real export (first run, or something else overwrote it)
        # while the gateway's is — the opposite of an unreadable file, where preferring the gateway is unsafe
        disk_stamp = (
            await self._disk_timestamp_or_none()
        )  # for the log and a refusal; read before the write below
        try:
            merged, carried, conflicts = await self.hub.hass.async_add_executor_job(
                self._carry_over, text, both_changed
            )
        except _NoMergeBase as err:
            # HA's own copy changed too, and nothing tells which of its differences are its own changes
            raise _failure(
                "service_gateway_export_newer",
                host=api.host,
                gateway=stamp,
                file=disk_stamp or "no timestamp",
            ) from err
        if self.identity_enabled:
            merged, identity_added = await self._identity_text(merged)
            carried += identity_added
        try:
            await self.hub.hass.async_add_executor_job(self._keep_pre_adopt_copy)
            await self.hub.hass.async_add_executor_job(
                write_private_with_backup, Path(self.path), merged.encode()
            )
            # the app's own upload is the base the next one is compared with
            await self.hub.hass.async_add_executor_job(
                write_private, app_copy_path(self.path), text.encode()
            )
        except OSError as err:
            raise _failure(
                "service_export_write_failed", path=self.path, error=str(err)
            ) from err
        if gateway_digest is not None:
            self._mark_synced(gateway_digest)
        _LOGGER.info(
            "The gateway %s holds a newer export (%s) than the copy on disk (%s): adopted it %s",
            api.host,
            stamp,
            disk_stamp or "no timestamp",
            purpose,
        )
        self._report_conflicts(conflicts)
        if carried:
            _LOGGER.info(
                "Kept %d change(s) Home Assistant made that the app's export lacks; handing the result to the gateway",
                carried,
            )
            await self._upload_or_retry(await self._read())
        return True

    def _report_conflicts(self, conflicts: list[Change]) -> None:
        """Raise the `carry_over_conflict` repair for an adopt that kept the app's version over Home Assistant's.

        Review-4 W4-5: such an adopt used to be a log line only, while the file lost what the nodes still hold (a
        room Home Assistant created is gone from the export, its members still subscribed to it). An adopt
        without conflicts clears the repair. The paths also go into the issue's data, for diagnostics.
        """
        issue = issue_id(self.hub.entry, ISSUE_CARRY_OVER_CONFLICT)
        if not conflicts:
            ir.async_delete_issue(self.hub.hass, DOMAIN, issue)
            return
        paths = [c.where() for c in conflicts]
        _LOGGER.warning(
            "The app changed what Home Assistant had changed too; the app's version is kept for: %s",
            ", ".join(paths),
        )
        ir.async_create_issue(
            self.hub.hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_CARRY_OVER_CONFLICT,
            translation_placeholders={
                "title": self.hub.entry.title,
                "paths": ", ".join(paths),
                "held": "; ".join(
                    f"{where}: {held(c)}"
                    for where, c in zip(paths, conflicts, strict=True)
                ),
            },
            data={"paths": "\n".join(paths)},
        )

    async def adopt_for_unknown_nodes(self, macs: Sequence[str]) -> list[str]:
        """Adopt the gateway's export when it lists nodes of `macs` (this mesh's unknown nodes); return those.

        The unknown-node refresh (`JungHomeHub._refresh_export_from_gateway`), on the path of every gateway
        write here: under the lock, fetched by `_gateway_state` (this mesh's export, from a gateway whose pin is
        vouched for) and written by `_adopt` — only when the gateway alone changed since HA last synced, never a
        bare `/project/cdb` database over a share export, with the `.bak` and the synced digest recorded (a later
        change would otherwise take the gateway for ahead of HA). [] when nothing was adopted, which is logged;
        nothing is raised — nobody asked for this.
        """
        async with self.lock:
            try:
                state = await self._gateway_state()
            except _GatewayUnusable as err:
                _LOGGER.warning(
                    "The gateway's export was not fetched for the unknown node(s) %s: %s",
                    ", ".join(macs),
                    err.cause,
                )
                return []
            if state is None:
                return []  # `_gateway_export` said why
            try:
                listed = await self.hub.hass.async_add_executor_job(
                    _listed_macs, state[0]
                )
            except (
                InvalidExport,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
            ) as err:
                _LOGGER.warning(
                    "The gateway handed out an export that does not parse: %s", err
                )
                return []
            found = [mac for mac in macs if mac in listed]
            if not found:
                _LOGGER.info(
                    "The gateway's export does not list the unknown node(s) %s either",
                    ", ".join(macs),
                )
                return []
            try:
                adopted = await self._adopt(state, f"for {', '.join(found)}", bare=True)
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "The gateway's export lists %s but was not adopted: %s",
                    ", ".join(found),
                    err.translation_key,
                )
                return []
            if not adopted:
                _LOGGER.info(
                    "The gateway's export lists %s, but nothing Home Assistant has not synced already",
                    ", ".join(found),
                )
            return found if adopted else []

    async def async_current_export(self) -> ProjectFile:
        """Return the export as a change would plan on it now, for a plan made outside the configurator (`add_device`).

        What `_load` reads, under the lock: the gateway's export when only it changed since Home Assistant last
        synced (adopted — `recorded` / `adopted` then tell `services._run` to follow it even if the call fails
        later), refused when both sides changed, else the copy on disk; with the provisioner identity on, the vault's
        nodes merged in. The running hub's CDB is the export as the hub last took it over, which misses what the app
        added since.
        """
        async with self.lock:
            return await self._load()

    async def record_node(
        self,
        template: Node,
        entry_for: Callable[[dict[str, Any]], dict[str, Any]],
        audit: NodeAudit,
        plan: Plan,
        name: str,
        function: int | None = None,
    ) -> DeviceCount | None:
        """Record a node Home Assistant just provisioned and commissioned (review-3 N3; `onboard.async_add_device`).

        On the export as it is now (the gateway's, when the app changed it meanwhile): the template's entry is
        turned into the new node's (`entry_for`), then `onboarding.record` adds it with what the node answered, its
        element groups and its app device rows (carrying `function`, the actuator function it advertised); saved and
        handed to the gateway like any change. Returns the app's missing-devices check of the recorded rows
        (`onboarding.missing_devices`): None when the node has the devices its product and insert call for.
        """
        async with self.lock:
            pf = await self._load()
            raw = next(
                n
                for n in pf.net["nodes"]
                if parse_address(str(n.get("unicastAddress", "0"))) == template.unicast
            )
            template_now = pf.cdb.node_by_addr(template.unicast)
            assert template_now is not None
            node = record_node(
                pf, template_now, entry_for(raw), audit, plan, name, function
            )
            count = missing_devices(
                pf,
                node,
                function if function is not None else template_now.insert_function,
            )
            # the vault keeps what the file got for it (review-3 N1): the app's next upload lacks the node
            self.hub.vault.identity().remember_recorded(pf, node.uuid)
            await self._save(pf)
            await self.hub.vault.async_save()
            _LOGGER.info("Recorded the new node %r in the export", name)
            return count

    async def remove_node(self, unicast: int, *, force: bool = False) -> bool:
        """Remove the node whose primary element is `unicast` from the network (review-3 N4, experimental).

        The app's order, with the reset first: Config Node Reset to the node (it forgets its keys and becomes an
        unprovisioned device again); only once it confirmed — or with `force`, for a node that is gone for good —
        every other node's wiring to it is removed (`ProjectFile.remove_node`: its element groups, publications to
        it) and the file records it as excluded. The reset cannot be taken back, so a stop of that unwiring — a
        cancellation too (D12) — still records the removal itself (`ProjectFile.exclude_node`) with what the
        others accepted, and the vault forgets the node either way. The gateway node is refused: taking it out is
        a takeover of its own (plan N11).

        A node can take the reset and lose its status (review-4 W4-7): an unconfirmed reset is looked into
        (`_reset_unconfirmed`) rather than reported as nothing changed. The node carrying Home Assistant's link
        (`hub.proxy_node`) is refused without `force`: its reset ends the link its confirmation would come back
        on. With `force` the link lost on its reset is that silence, and the unwiring waits for the next link.
        Both unverified on air.
        """
        async with self.lock:
            pf = await self._load()
            node = pf.cdb.node_by_addr(unicast)
            if node is None or node.unicast != unicast:
                raise _validation("service_unknown_element", address=hexaddr(unicast))
            if node.pid == GATEWAY_PID:
                raise _validation("remove_device_gateway")
            carries_link = unicast == self.hub.proxy_node
            if carries_link and not force:
                raise _validation("remove_device_proxy", address=hexaddr(unicast))
            # scanners keep a device's advert data merged: one from before it was provisioned proves nothing later
            advertised = advertises_unprovisioned(self.hub.hass, node.uuid)
            relink = False
            try:
                await self.hub.proxy.request_config(
                    unicast,
                    C.node_reset(),
                    C.CONFIG_NODE_RESET_STATUS,
                    timeout=NODE_RESET_TIMEOUT,
                )
            except TimeoutError as err:
                await self._reset_unconfirmed(
                    node, force=force, advertised=advertised, err=err
                )
            except (ConnectionError, OSError) as err:
                if not carries_link:
                    raise _failure(
                        "service_send_failed",
                        node=hexaddr(unicast),
                        message="Config Node Reset",
                        applied=APPLIED_NOTHING,
                    ) from err
                _LOGGER.warning(
                    "%s carried the link, which ended with its reset; removing it from the network all the same",
                    node.name,
                )
                relink = True
            iv_index = self.hub.proxy.state.iv_index
            changes = pf.remove_node(node, iv_index)
            if relink and changes:
                # the others' unwiring needs a link; without one it stops at its first message, recorded as such
                await self.hub.async_wait_connected(SERVICE_LINK_WAIT)
            try:
                await self._send(
                    self._steps(pf, changes),
                    action="junghome_ble.remove_device",
                    happened={
                        "kind": "excluded",
                        "node": unicast,
                        "iv_index": iv_index,
                    },
                    applied=lambda accepted, total: applied_removed(
                        unicast, accepted, total
                    ),
                )
                await self._save(pf)
            finally:
                # reset whatever the file says now: its device key opens nothing any more
                vault = self.hub.vault.vault
                if vault is not None and vault.forget(node.uuid):
                    await run_to_end(self.hub.vault.async_save())
            _LOGGER.info(
                "Removed %s (%04X) from the network, %d Config messages to the others",
                node.name,
                unicast,
                len(changes),
            )
            return True

    async def _reset_unconfirmed(
        self, node: Node, *, force: bool, advertised: bool, err: TimeoutError
    ) -> None:
        """Decide on a Node Reset the node did not confirm: return to go on with the removal, else raise.

        The status can be lost when the node took the reset (it forgets the keys that would seal it), so silence
        is no "nothing changed". A reset node advertises as a new device (the Mesh Provisioning Service with its
        UUID): seen within RESET_ADVERT_WAIT, the reset took. Not seen proves nothing — the scanners may not reach
        it — so the error says it may have been reset, and `force` is the way on for a node that is gone; one
        that already advertised so before the reset (a stale scanner cache) is not looked for. Unverified on air.
        """
        if force:
            _LOGGER.warning(
                "%s did not confirm its reset; removing it from the network all the same",
                node.name,
            )
            return
        if not advertised and await self._advertises_reset(node.uuid):
            _LOGGER.info(
                "%s did not confirm its reset but advertises as a new device: it was reset",
                node.name,
            )
            return
        raise _failure(
            "remove_device_unconfirmed", address=hexaddr(node.unicast)
        ) from err

    async def _advertises_reset(self, uuid: str) -> bool:
        """Whether the node `uuid` advertises as a new device within RESET_ADVERT_WAIT (looked at every POLL)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESET_ADVERT_WAIT
        while not advertises_unprovisioned(self.hub.hass, uuid):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(RESET_ADVERT_POLL)
        return True

    async def async_export(self, flavour: str) -> dict[str, Any]:
        """Return the export on disk rendered as `flavour` (`share` / `cdb`), with its mesh and timestamp."""
        async with self.lock:
            pf = await self._read()
            await self._with_identity(pf)  # as the next save would write it
        rendered = pf.render("share" if flavour == "share" else "cdb")
        return {
            "flavour": flavour,
            "mesh_uuid": pf.cdb.mesh_uuid,
            "timestamp": pf.loaded_timestamp,
            "export": json.loads(rendered),
        }

    async def sync_gateway(self) -> bool:
        """Upload the export the entry points at to the gateway, as it is on disk (the retry of a failed sync).

        Refused when the gateway holds changes Home Assistant has not seen: uploading would erase them (the
        gateway rebuilds its installation from what is POSTed), and nothing is merged here — the next change
        takes the gateway's export over and carries HA's changes onto it (`_adopt`). The same export on both
        sides is no refusal (`_identical`).
        """
        if self.gateway is None:
            raise _validation("service_no_gateway")
        async with self.lock:
            pf = await self._read()
            if await self._with_identity(pf):
                # the file first (`ProjectFile.save` bumps its timestamp like every writer), so the gateway gets
                # what is on disk: the synced digest compares the two
                await self._write(pf)
            self.cancel_upload_retry()  # this upload supersedes it
            await self._upload(pf, raise_on_failure=True)
            return False

    # ------------------------------------------------------------------ lookups
    @staticmethod
    def _element(pf: ProjectFile, address: int) -> Element:
        element = pf.cdb.element(address)
        if element is None:
            raise _validation("service_unknown_element", address=hexaddr(address))
        return element

    @staticmethod
    def _room(pf: ProjectFile, name: str) -> int:
        wanted = name.strip().lower()
        for addr, room in pf.user_groups().items():
            if room.lower() == wanted:
                return addr
        raise _validation("service_no_room", room=name)

    @staticmethod
    def _room_name(name: str) -> str:
        """Validate a room name as the services accept it: non-empty, one the app can use, not one the mesh reserves.

        A name with a lone `%` is refused as the app's `CheckNameInput` refuses it (`check_name`). `element group
        #…`, `device type group…` and `#time_keeper_group#` name the internal groups the app filters out of its
        room list (`devices.is_room`); a room called that would vanish from the app.
        """
        wanted = name.strip()
        if not wanted:
            raise _validation("service_invalid_room_name")
        try:
            check_name(wanted)
        except InvalidName as err:
            raise _name_error(err, wanted) from err
        if not is_room(GROUP_RANGE[0], wanted):
            raise _validation("service_room_name_reserved", name=wanted)
        return wanted

    @classmethod
    def _add_room(cls, pf: ProjectFile, name: str) -> int:
        wanted = cls._room_name(name)
        try:
            return pf.add_group(wanted)
        except ValueError as err:
            raise _validation("service_room_exists", room=name) from err
        except AllocationCrowded as err:
            raise _validation(
                "service_room_range_crowded", free=str(err.below)
            ) from err
        except ExportError as err:  # the provisioner's group range is used up
            raise _validation("service_room_range_full") from err

    @staticmethod
    def _rooms_of(pf: ProjectFile, element: Element) -> list[int]:
        rooms = pf.user_groups()
        return sorted(
            {
                a
                for m in element.raw_models
                for a in element.subscriptions(m["modelId"])
                if a in rooms
            }
        )

    @staticmethod
    def _device_entry(
        pf: ProjectFile, node: Node, location: int
    ) -> dict[str, Any] | None:
        """Return the `meta.devices[]` entry covering an element location: the most specific one, as the app resolves it."""
        best: dict[str, Any] | None = None
        best_size = 0
        for dev in meta_rows(meta_list(pf.meta.get("devices"))):
            did = dev.get("deviceId")
            if (
                not isinstance(did, dict)
                or canonical_uuid(str(did.get("nodeId", ""))) != node.uuid
            ):
                continue
            locations = location_ids(did.get("locationIds"))
            if locations is None:
                continue
            if location in locations and (best is None or len(locations) < best_size):
                best, best_size = dev, len(locations)
        return best

    # ------------------------------------------------------------------ Config plans
    @staticmethod
    def _step(pf: ProjectFile, change: ModelChange) -> ConfigStep:
        """Build the Config message that mirrors one CDB edit, addressed to the element's node."""
        node = pf.cdb.node_by_addr(change.element)
        assert node is not None  # the mutators only edit elements the CDB has
        if change.kind == "subscribe":
            pdu = C.model_subscription_add(change.element, change.address, change.model)
            expect = C.CONFIG_MODEL_SUBSCRIPTION_STATUS
        elif change.kind == "unsubscribe":
            pdu = C.model_subscription_delete(
                change.element, change.address, change.model
            )
            expect = C.CONFIG_MODEL_SUBSCRIPTION_STATUS
        else:
            pdu = C.model_publication_set(change.element, change.address, change.model)
            expect = C.CONFIG_MODEL_PUBLICATION_STATUS
        return ConfigStep(node.unicast, pdu, expect, change=change)

    def _steps(
        self, pf: ProjectFile, changes: Iterable[ModelChange]
    ) -> list[ConfigStep]:
        return [self._step(pf, c) for c in changes]

    @staticmethod
    def _bind_step(element: Element, model: str) -> ConfigStep | None:
        """Model App Bind for a model the CDB shows unbound (the app auto-binds before its first message)."""
        raw = raw_model(element, model)
        bound = [int(b) for b in raw.get("bind", [])]
        if APP_KEY_INDEX in bound:
            return None
        raw["bind"] = [*bound, APP_KEY_INDEX]
        return ConfigStep(
            element.node.unicast,
            C.model_app_bind(element.address, model, APP_KEY_INDEX),
            C.CONFIG_MODEL_APP_STATUS,
            bind=(element.address, model),
        )

    def _unlink_room_steps(
        self, pf: ProjectFile, key: Element, publish: int, key_mode: int
    ) -> list[ConfigStep]:
        """`RemoveConnectionForAddress.GroupConnection`: the loads listening to the key's publish group stop."""
        steps: list[ConfigStep] = []
        for node in pf.cdb.nodes:
            for element in node.elements:
                if element is key:
                    continue
                for model in KEY_MODE_SERVERS[key_mode]:
                    if has_model(element, model) and _subscribed(
                        element, model, publish
                    ):
                        steps += self._steps(
                            pf, pf.unsubscribe(element, model, publish)
                        )
        return steps

    def _clear_steps(self, pf: ProjectFile, key: Element) -> list[ConfigStep]:
        """`RemoveConnectionForAddress`: drop the key's room link and every publication / subscription it has.

        A room link (`cachedGroupConnectionMetadata` row) first unsubscribes the room's loads from the key's publish
        group (`GroupConnection`); then every model of the key element except the Sensor / LBC servers loses its
        publication (`Publication Set 0x0000`) and each of its subscriptions (`Subscription Delete` per address).
        Every step is tagged with the key: a record of a stopped plan drops the link's `meta` row only once every
        tagged step was accepted, so a partly-unwired load still has a row naming the group it is stuck on.
        """
        return [replace(s, unlinks=key.address) for s in self._clear_plan(pf, key)]

    def _clear_plan(self, pf: ProjectFile, key: Element) -> list[ConfigStep]:
        steps: list[ConfigStep] = []
        own_group = element_groups(pf).get(key.address)
        for dev in meta_rows(meta_list(pf.meta.get("devices"))):
            cached = meta_list(dev.get("cachedGroupConnectionMetadata"))
            links = [
                r
                for r in meta_rows(cached)
                if as_int(r.get("elementAddress")) == key.address
            ]
            if not links:
                continue
            for row in links:
                publish = as_int(row.get("publishAddress")) or own_group
                key_mode = FUNCTION_KEY_MODE.get(
                    function_code(row.get("function")) or 0, KEY_MODE_LIGHT
                )
                if publish is not None:
                    steps += self._unlink_room_steps(pf, key, publish, key_mode)
            dev["cachedGroupConnectionMetadata"] = [r for r in cached if r not in links]
        _drop_scene_key_row(pf, key.address)
        for raw in key.raw_models:
            model = raw["modelId"]
            if model.upper() in CLEAR_KEEP_MODELS:
                continue
            if pf.publication(key, model) is not None:
                steps += self._steps(pf, pf.set_publication(key.node, key, model, None))
            for group in key.subscriptions(model):
                if not _deletable(group):
                    # review-3 W12: no Subscription Delete can carry it; the key keeps it, the rest is cleared
                    _LOGGER.warning(
                        "Key %04X keeps its subscription to %04X (model %s): a virtual or fixed group address "
                        "cannot be removed from here",
                        key.address,
                        group,
                        model,
                    )
                    continue
                steps += self._steps(pf, pf.unsubscribe(key, model, group))
        return steps

    def _record_room_link(
        self,
        pf: ProjectFile,
        key: Element,
        room: int,
        publish: int,
        function: str,
        *,
        keep: bool = False,
    ) -> None:
        """Store the `KeyModeGroupConfig` the app needs to show (and later re-wire) a room link.

        It replaces the key's earlier rows, unless `keep`: the record of a stopped plan keeps them beside the new
        one, since the old link's loads may still listen to the key — its rows are what makes the next clear
        unwire them — and `_record` drops them once every step tearing the old link down was accepted.
        """
        entry = self._device_entry(pf, key.node, key.location)
        if entry is None:
            found = self.hub.metadata.entry_for(key.node.uuid, key.location)
            locations, name = found or (
                [key.location],
                f"{key.node.name} {key.node.unicast:04X} buttons",
            )
            entry = pf.set_device_name(key.node, locations, name)
        template = next(
            (
                r
                for dev in meta_rows(meta_list(pf.meta.get("devices")))
                for r in meta_rows(meta_list(dev.get("cachedGroupConnectionMetadata")))
            ),
            None,
        )
        row: dict[str, Any] = {
            "elementAddress": key.address,
            "groupAddress": room,
            "publishAddress": publish,
            "function": function,
        }
        if (
            template is not None
        ):  # mirror the file's own style: int vs hex-string addresses, enum name vs ordinal
            if isinstance(template.get("elementAddress"), str):
                row = {
                    k: hexaddr(v) if isinstance(v, int) else v for k, v in row.items()
                }
            if isinstance(template.get("function"), int):
                row["function"] = GROUP_FUNCTIONS[function]
            row = {k: row[k] for k in template if k in row} | row
        kept = [
            r
            for r in meta_list(entry.get("cachedGroupConnectionMetadata"))
            if (r != row if keep else keeps_row(r, "elementAddress", key.address))
        ]
        entry["cachedGroupConnectionMetadata"] = [*kept, row]

    def _plan_room_link(
        self, pf: ProjectFile, key: Element, room: str, mode: str | None
    ) -> KeyPlan:
        """`SetGroupConnection` + `SetGroupFunction`: the room's loads of the function's kind listen to the key."""
        mode = mode or MODE_LIGHT
        if mode not in ROOM_MODES:
            raise _validation("service_invalid_mode", mode=mode, target="room")
        room_address = self._room(pf, room)
        function = ROOM_FUNCTIONS[mode]
        code = GROUP_FUNCTIONS[function]
        key_mode = FUNCTION_KEY_MODE[code]
        publish = element_groups(pf).get(key.address)
        if publish is None:
            raise _validation("service_no_element_group", address=hexaddr(key.address))
        steps = self._clear_steps(pf, key)
        for member in pf.group_members(room_address):
            if member is key or not pf._matches_function(member, code):  # noqa: SLF001
                continue
            for model in KEY_MODE_SERVERS[key_mode]:
                if has_model(member, model):
                    steps += self._steps(pf, pf.subscribe(member, model, publish))
        self._record_room_link(pf, key, room_address, publish, function)
        if pf.flavour == "cdb":
            _LOGGER.warning(
                "%s is a raw MeshNetwork.json: the room link of key %04X is configured on the mesh but cannot be "
                "recorded in the file; the app will not show it and later room members are not wired to the key",
                self.path,
                key.address,
            )
        # review-3 W5: loads the stopped plan did subscribe need the row, or no later clear unwires them
        prepare: Note = {
            "kind": "room_link",
            "key": key.address,
            "room": room_address,
            "publish": publish,
            "function": function,
        }
        return KeyPlan(
            steps,
            key_mode,
            publish,
            mode,
            f"room {room!r} ({room_address:04X})",
            prepare,
        )

    def _plan_device_link(
        self, pf: ProjectFile, key: Element, address: int, mode: str | None
    ) -> KeyPlan:
        """`SetDeviceConnection`: the key's clients go to the target's element group; the target itself is untouched.

        Its servers already sit on that group since provisioning; a target the app never wired completely gets the
        missing subscriptions. A socket target additionally gets `ConfigurePublicationsForPropertyUser`: its User
        Property servers publish to their element groups and the key's clients subscribe there.
        """
        target = self._element(pf, address)
        mode = mode or derive_mode(target)
        if mode not in DEVICE_MODES or (
            (mode == MODE_GATEWAY) != (target.node.pid == GATEWAY_PID)
        ):
            raise _validation(
                "service_invalid_mode", mode=mode, target=hexaddr(target.address)
            )
        key_mode = KEY_MODES[mode]
        groups = element_groups(pf)
        publish = groups.get(target.address)
        if publish is None:
            raise _validation(
                "service_no_element_group", address=hexaddr(target.address)
            )
        steps = self._clear_steps(pf, key)
        for model in KEY_MODE_SERVERS[key_mode]:
            if has_model(target, model) and not _subscribed(target, model, publish):
                steps += self._steps(pf, pf.subscribe(target, model, publish))
        if target.node.pid in SOCKET_PIDS:
            clients = [m for m in KEY_MODE_CLIENTS[key_mode] if has_model(key, m)]
            for element in target.node.elements:
                group = groups.get(element.address)
                if group is None or not has_model(element, USER_PROPERTY_SERVER):
                    continue
                if pf.publication(element, USER_PROPERTY_SERVER) != group:
                    # a model publishes with the AppKey it is bound to: bind first, as the app does (review-3 W10)
                    bind = self._bind_step(element, USER_PROPERTY_SERVER)
                    if bind is not None:
                        steps.append(bind)
                    steps += self._steps(
                        pf,
                        pf.set_publication(
                            element.node, element, USER_PROPERTY_SERVER, group
                        ),
                    )
                for model in clients:
                    steps += self._steps(pf, pf.subscribe(key, model, group))
        return KeyPlan(steps, key_mode, publish, mode, f"element {target.address:04X}")

    def _plan_scene_link(
        self, pf: ProjectFile, key: Element, scene: str | int, mode: str | None
    ) -> KeyPlan:
        """`SetSceneConnection` (network-logic.md §2.5): the key recalls a scene on every node. Unverified on air.

        The key's connections are cleared (`RemoveConnectionForAddress.AllConnections`) and its Scene Client
        publishes to `0xFFFF` (publish only: `assign_key` adds no subscription for it); once that is accepted the
        key gets the scene number (`_write_scene_config`), the app's `keyModeSceneConfigExports` row
        (`_record_scene_link`) and KeyMode 2. The target is a scene, so `mode` must be left out.
        """
        number = self._scene(pf, scene)
        if mode is not None:
            raise _validation(
                "service_invalid_mode", mode=mode, target=f"scene {number}"
            )
        steps = self._clear_steps(pf, key)
        if pf.flavour == "cdb":
            _LOGGER.warning(
                "%s is a raw MeshNetwork.json: the scene link of key %04X is configured on the mesh but cannot be "
                "recorded in the file; the app will not show which scene the key recalls",
                self.path,
                key.address,
            )
        own_group = self._scene_link_group(pf, key)

        def record(project: ProjectFile) -> None:
            self._record_scene_link(project, key.address, number, own_group)

        return KeyPlan(
            steps,
            KEY_MODE_SCENE,
            ALL_SCENES,
            MODE_SCENE,
            f"scene {number}",
            scene=number,
            record_scene=record,
        )

    @staticmethod
    def _scene_link_group(pf: ProjectFile, key: Element) -> int | None:
        """Return the element group of the key's own load, which the app caches with a scene link (never sent on air).

        The app takes the load element it maps to the key (`e2()`, network-logic.md §2.5); here, the node's first
        load element with one of those servers — on the fixture export the recorded row names that one. None for a
        node without a load (a wall transmitter, a binary-input puck): the Android export makes the field optional.
        """
        groups = element_groups(pf)
        for element in key.node.elements:
            if element.location < KEY_LOCATION and any(
                has_model(element, model) for model in SCENE_LINK_LOAD_SERVERS
            ):
                return groups.get(element.address)
        return None

    @staticmethod
    def _record_scene_link(
        pf: ProjectFile, key: int, number: int, publication: int | None
    ) -> None:
        """Store the `keyModeSceneConfigExports` row that makes the app show the key as recalling scene `number`.

        `{"sceneConfig": {transitionStepSeconds, sceneId, transitionResolution, publicationAddress?},
        "elementAddress"}` (`MeshPropertyExport.java:153-161`), no transition as the app writes it; it replaces the
        key's earlier row and mirrors an existing row's style (field order, int vs hex-string addresses).
        """
        rows = meta_list(pf.meta.get("keyModeSceneConfigExports"))
        template = next(
            (r for r in meta_rows(rows) if isinstance(r.get("sceneConfig"), dict)),
            None,
        )
        config: dict[str, Any] = {
            "transitionStepSeconds": 0,
            "sceneId": number,
            "transitionResolution": 0,
        }
        if publication is not None:
            config["publicationAddress"] = publication
        row: dict[str, Any] = {"sceneConfig": config, "elementAddress": key}
        if template is not None:
            if isinstance(template.get("elementAddress"), str):
                row["elementAddress"] = hexaddr(key)
                if publication is not None:
                    config["publicationAddress"] = hexaddr(publication)
            old = template["sceneConfig"]
            row["sceneConfig"] = {k: config[k] for k in old if k in config} | config
            row = {k: row[k] for k in template if k in row} | row
        kept = [r for r in rows if keeps_row(r, "elementAddress", key)]
        pf.meta["keyModeSceneConfigExports"] = [*kept, row]

    # ------------------------------------------------------------------ sending
    async def _send(
        self,
        steps: Iterable[ConfigStep],
        *,
        action: str,
        prepare: Note | None = None,
        happened: Note | None = None,
        applied: Callable[[int, int], str] = applied_text,
        as_planned: bool = False,
    ) -> None:
        """Send the plan, additive steps first; the first refusal, silence or lost link stops it, apply-and-record.

        The steps accepted before the stop are what the mesh holds now: they are replayed into a fresh copy of
        the export (the planned file claims the whole plan) and that copy is written — and handed to the gateway
        — before the error is raised, which says how many messages were applied. Nothing is rolled back: every
        Config message is idempotent, so running the action again with the same target completes the plan.
        `prepare`, when given, is the plan's bookkeeping that isn't one of `steps` (e.g. a room the plan
        creates), applied to the fresh record before the accepted steps are replayed into it. `happened` is what
        the mesh already holds from before the plan (a node's reset): it goes into the record even when no step
        was accepted. `applied` words the error's account of the stop from (accepted, total).

        A battery node's steps go first (`ordered`) and the node is kept awake while the plan is sent
        (`KeepAwake.hold`); one it does not answer stops the plan as *asleep* (`_request`). `as_planned` sends the
        steps in the order given instead, none dropped: a plan that repeats the app's own sequence, such as a
        publication reset (`Publication Set 0x0000`, then the address again).

        A plan cancelled from outside (D12: an automation in `mode: restart`, `script.turn_off`, Home Assistant
        stopping) is recorded the same way, held to its end (`run_to_end`), before the cancellation goes on. The
        step in flight when it came is not recorded: the node may or may not have taken it, as when it stays
        silent, and the next run sends it again. What a crash stops is in the plan journal (W I1): the plan and
        how many of its steps were accepted, written before its first message and after every accepted one, and
        removed once the export records the outcome; the next setup records what it says (`async_replay_journal`).
        `action` names the plan there, for the repair issue.

        A plan with a step to a node the hub counts as unreachable (`JungHomeHub.node_alive`: a request it left
        unanswered, or its heartbeats missing) is refused before its first message (review-4 W I5), naming them:
        it would only stop at that node after CONFIG_TIMEOUT times (1 + CONFIG_RETRIES), with the steps before it
        applied. `happened` is still recorded. Battery nodes are never marked so; they go the keep-awake way.
        Unverified on air.
        """
        steps = list(steps)
        sleepy = {
            unicast
            for s in steps
            if (unicast := sleepy_node(self.hub.cdb, s.node)) is not None
        }
        plan = steps if as_planned else ordered(steps, sleepy)
        accepted: list[ConfigStep] = []
        problem = self._unreachable(plan, sleepy)
        journal: dict[str, Any] = {
            "action": action,
            "steps": [_step_json(s) for s in plan],
            "accepted": 0,
            "prepare": prepare,
            "happened": happened,
        }
        try:
            if problem is None:
                if plan or happened is not None:
                    await self._journal_save(journal)
                async with self.hub.keep_awake.hold(sleepy):
                    for step in plan:
                        problem = await self._request(step)
                        if problem is not None:
                            break
                        accepted.append(step)
                        journal["accepted"] = len(accepted)
                        await self._journal_save(journal)
        except asyncio.CancelledError:
            await self._record_stopped(accepted, plan, prepare, happened)
            raise
        if problem is None:
            return
        key, placeholders = problem
        err = _failure(key, **placeholders, applied=applied(len(accepted), len(plan)))
        if (
            record_err := await self._record_stopped(accepted, plan, prepare, happened)
        ) is not None:
            raise err from record_err
        raise err

    async def _record_stopped(
        self,
        accepted: list[ConfigStep],
        plan: list[ConfigStep],
        prepare: Note | None,
        happened: Note | None,
    ) -> HomeAssistantError | None:
        """`_record` a stopped plan, held to its end; the error (logged) when the record could not be written."""
        try:
            await run_to_end(
                self._record(accepted, plan, prepare=prepare, happened=happened)
            )
        except HomeAssistantError as record_err:
            _LOGGER.error(
                "%d of %d Config messages were applied on the mesh but could not be recorded in %s: %s",
                len(accepted),
                len(plan),
                self.path,
                record_err,
            )
            return record_err
        return None

    def _unreachable(
        self, plan: list[ConfigStep], sleepy: set[int]
    ) -> tuple[str, dict[str, str]] | None:
        """Return the error key and placeholders refusing `plan` for its unreachable nodes; None when all are there."""
        nodes = sorted(
            {
                s.node
                for s in plan
                if s.node not in sleepy and not self.hub.node_alive(s.node)
            }
        )
        if not nodes:
            return None
        return "service_nodes_unreachable", {
            "nodes": ", ".join(hexaddr(n) for n in nodes)
        }

    def _silence(self, node: int) -> str:
        """Return the error key for a node that did not answer: *asleep* for a battery node (press a key, then run)."""
        if sleepy_node(self.hub.cdb, node) is not None:
            return "service_node_asleep"
        return "service_no_reply"

    async def _request(self, step: ConfigStep) -> tuple[str, dict[str, str]] | None:
        """Send one Config message and judge its Status; None when accepted, else the error key + placeholders.

        The Status must echo the step (`ConfigStep.matches`): a duplicate of the previous step's answer is not
        this one's. A node the running hub does not know (the export on disk was replaced by a newer one naming
        a re-provisioned node, without a reload) has no device key here: `ProxyClient._dev_key` raises
        `ValueError`, reported as the export being newer than what is loaded. A battery node that stays silent is
        reported as asleep (`_silence`), asking for a key press and a new run instead of the no-reply error.
        """
        what = step.what
        try:
            reply = await self.hub.proxy.request_config(
                step.node,
                step.pdu,
                step.expect,
                timeout=CONFIG_TIMEOUT,
                retries=CONFIG_RETRIES,
                match=step.matches,
            )
        except TimeoutError:
            return self._silence(step.node), {
                "node": hexaddr(step.node),
                "message": what,
            }
        except ValueError:
            return "service_export_unknown_node", {
                "node": hexaddr(step.node),
                "path": self.path,
            }
        except (ConnectionError, OSError):
            return "service_send_failed", {"node": hexaddr(step.node), "message": what}
        try:
            status = C.decode_config(reply.opcode, reply.params)
        except ValueError:
            status = None
        if not isinstance(status, C.ConfigStatus) or not status.ok:
            name = (
                status.status_name
                if isinstance(status, C.ConfigStatus)
                else "malformed status"
            )
            return "service_config_refused", {
                "node": hexaddr(step.node),
                "message": what,
                "status": name,
            }
        _LOGGER.debug("%04X accepted %s", step.node, what)
        return None

    async def _record(
        self,
        accepted: list[ConfigStep],
        plan: list[ConfigStep],
        *,
        prepare: Note | None = None,
        happened: Note | None = None,
        upload: bool = True,
    ) -> None:
        """Write what the mesh holds after a plan stopped: a fresh copy of the export with the accepted steps replayed.

        `happened` (what the mesh holds from before the plan) goes in first, and alone is reason enough to write.
        A key's room-link row is dropped only once every step of `plan` tagged `unlinks=key` was accepted: while
        any of them is still pending, the row is what makes the next run's `_unlink_room_steps` unsubscribe the
        loads that never got there this time. `prepare` runs next — after that drop, so a new room link's row it
        records is not taken for the old one's — and before the replay, so a step that subscribes to a room the
        plan itself creates has something to subscribe to in this fresh copy too. Every part of it is idempotent,
        so a journal replayed twice records the same. `upload=False` leaves the gateway to `sync_gateway` or the
        next change.
        """
        if not accepted and happened is None:
            await (
                self._journal_close()
            )  # the mesh holds nothing new: nothing to record, now or after a crash
            return
        record = await self._read()
        # as `_load` planned it: with the provisioner identity on, the nodes only the vault had are in it too
        await self._with_identity(record)
        if happened is not None:
            self._bookkeeping(record, happened)
        done = {id(s) for s in accepted}
        pending = {
            s.unlinks for s in plan if s.unlinks is not None and id(s) not in done
        }
        finished = {s.unlinks for s in accepted if s.unlinks is not None} - pending
        for key in finished:
            _drop_link_rows(record, key)
        if prepare is not None:
            self._bookkeeping(record, prepare)
        for step in accepted:
            replay(record, step)
        await self._save(record, upload=upload)
        _LOGGER.warning(
            "The plan stopped after %d accepted Config message(s); %s records what the mesh holds now",
            len(accepted),
            self.path,
        )

    async def _reset_property_mode(self, key: int) -> None:
        """`ResetKeySetPropertyMode`: KeySetPropertyMode (0, stateless), up / down values empty.

        Unacknowledged Sets (`C4 27 05`): the app fires them and never waits, and an acknowledged Set's three
        late Admin Statuses would be taken for the KeyMode Status `_write_key_mode` waits for next. Sent only
        once every Config step was accepted, so a refused plan leaves a key in property mode as it was.
        """
        writes = (
            (
                PROPERTY_KEY_PROPERTY_MODE,
                P.encode(PROPERTY_KEY_PROPERTY_MODE, P.PropertyMode(0, stateful=False)),
            ),
            (PROPERTY_KEY_VALUE_UP, b""),
            (PROPERTY_KEY_VALUE_DOWN, b""),
        )
        for prop, value in writes:
            pdu = M.vendor_property_set("admin", prop, value, ack=False)
            try:
                await self.hub.proxy.send_access(key, pdu)
            except (ConnectionError, OSError) as err:
                raise _failure(
                    "service_send_failed",
                    node=hexaddr(key),
                    message=M.describe(pdu),
                    applied=APPLIED_KEY_WIRED,
                ) from err

    async def _admin_status(
        self,
        key: int,
        pdu: bytes,
        prop: int,
        timeout: float,
        retries: int,
        applied: str = APPLIED_KEY_WIRED,
    ) -> AccessMessage | None:
        """Send an LBC Admin request to the key element and wait for its Admin Status; None when it stays silent.

        Only a Status of `prop` answers it: not a battery key's keep-alive (`keep_awake.py`), nor a late one.
        A lost link names the node and the message, and says what `applied` before it.
        """
        try:
            return await self.hub.proxy.request(
                key,
                pdu,
                VENDOR_ADMIN_STATUS,
                timeout=timeout,
                retries=retries,
                expect_cid=M.JUNG_CID,
                match=lambda m: m.params[:2] == prop.to_bytes(2, "little"),
            )
        except TimeoutError:
            return None
        except (ConnectionError, OSError) as err:
            raise _failure(
                "service_send_failed",
                node=hexaddr(key),
                message=M.describe(pdu),
                applied=applied,
            ) from err

    async def _write_key_property(
        self, key: int, prop: int, value: bytes, applied: str
    ) -> bool:
        """LBC Admin Property Set of `prop` on the key, then confirm: the Set's status, or a Get when none arrives.

        JUNG firmware answers a state-changing acknowledged Set by publishing the status to the element's group
        (if at all), so the Set is given a short wait and the value is read back otherwise. Returns whether the key
        reports `value`; silence raises, saying what was `applied` before.
        """
        pdu = M.vendor_property_set("admin", prop, value)
        reply = await self._admin_status(key, pdu, prop, KEY_MODE_TIMEOUT, 1, applied)
        if reply is None or not _confirms_property(reply.params, prop, value):
            _LOGGER.debug(
                "%04X: no status for property %04X, reading it back", key, prop
            )
            reply = await self._admin_status(
                key,
                M.vendor_property_get("admin", prop),
                prop,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
            )
            if reply is None:
                raise _failure(
                    self._silence(key),
                    node=hexaddr(key),
                    message=M.describe(pdu),
                    applied=applied,
                )
        return _confirms_property(reply.params, prop, value)

    async def _write_key_mode(self, key: int, key_mode: int) -> None:
        """Write KeyMode 0x5003 and confirm it. Runs after the Config plan: its errors say the key is wired already."""
        value = P.encode(PROPERTY_KEY_MODE, key_mode)
        if not await self._write_key_property(
            key, PROPERTY_KEY_MODE, value, APPLIED_KEY_WIRED
        ):
            raise _failure(
                "service_key_mode_not_applied",
                address=hexaddr(key),
                mode=str(P.KEY_MODE.get(key_mode, key_mode)),
            )

    async def _write_scene_config(self, key: int, scene: int) -> None:
        """Write KeyModeSceneConfig 0x5002 = (scene, no transition), as the app does, and confirm it.

        `[scene u16 LE][transition u32 LE ms]` (network-logic.md §2.2); the app waits for its status. Unverified on
        air (review-3 F15).
        """
        value = P.encode(PROPERTY_KEY_SCENE_CONFIG, P.SceneConfig(scene))
        if not await self._write_key_property(
            key, PROPERTY_KEY_SCENE_CONFIG, value, APPLIED_SCENE_WIRED
        ):
            raise _failure(
                "service_key_scene_not_applied",
                address=hexaddr(key),
                scene=str(scene),
            )

    # ------------------------------------------------------------------ device names
    async def rename_device(self, address: int, name: str) -> str:
        """Rename the app device the element at `address` belongs to, as the app's rename does; returns the name.

        `ProjectFile.rename_device`: the app's name check (`service_name_blank` / `service_name_not_allowed` /
        `service_name_too_long`), its
        number suffix when another device has the name, `meta.devices[].name` only. The device is the export's
        `meta.devices[]` entry covering the element (the most specific, as the app resolves it); an element no entry
        covers gets one of its own. Nothing goes on air; the export is written and handed to the gateway only when
        the name changed.
        """
        async with self.lock:
            pf = await self._load()
            element = self._element(pf, address)
            entry = self._device_entry(pf, element.node, element.location)
            locations = (
                location_ids(entry.get("deviceId", {}).get("locationIds"))
                if entry is not None
                else None
            ) or [element.location]
            before = pf.snapshot()
            try:
                written = pf.rename_device(element.node, locations, name)
            except InvalidName as err:
                raise _name_error(err, name) from err
            if pf.snapshot() != before:
                await self._save(pf)
                _LOGGER.info("Renamed device %04X to %r", address, written)
            return written

    # ------------------------------------------------------------------ rooms
    async def create_room(self, name: str) -> int:
        """Create a room (CDB `groups[]` + `meta.userGroups[]`); nothing goes on air. Returns its address."""
        async with self.lock:
            pf = await self._load()
            address = self._add_room(pf, name)
            await self._save(pf)
            _LOGGER.info("Created room %r at %04X", name, address)
            return address

    async def rename_room(self, room: str, name: str) -> bool:
        """Rename a room in the CDB and in `meta.userGroups[]`; nothing goes on air."""
        async with self.lock:
            pf = await self._load()
            address = self._room(pf, room)
            wanted = self._room_name(name)
            before = pf.snapshot()
            try:
                pf.rename_group(address, wanted)
            except InvalidName as err:
                raise _name_error(err, wanted) from err
            except ValueError as err:
                raise _validation("service_room_exists", room=name) from err
            if pf.snapshot() == before:
                return self.adopted  # already called that
            await self._save(pf)
            _LOGGER.info("Renamed room %04X to %r", address, name)
            return True

    async def delete_room(self, room: str) -> bool:
        """Delete a room: members unsubscribed, room-linked keys cleared, CDB and `meta` entries dropped."""
        async with self.lock:
            pf = await self._load()
            address = self._room(pf, room)
            steps: list[ConfigStep] = []
            for row in list(pf.room_connections(address)):
                key_addr = as_int(row.get("elementAddress"))
                key = pf.cdb.element(key_addr) if key_addr is not None else None
                if key is not None:
                    steps += self._clear_steps(pf, key)
            steps += self._steps(pf, pf.remove_group(address))
            await self._send(steps, action="junghome_ble.delete_room")
            await self._save(pf)
            _LOGGER.info(
                "Deleted room %r (%04X), %d Config messages", room, address, len(steps)
            )
            return True

    async def set_room(self, address: int, room: str, *, create: bool = False) -> bool:
        """Put the load element at `address` into `room` (created when missing with `create`), leaving every other room."""
        return await self.set_rooms([address], room, create=create)

    async def set_rooms(
        self, addresses: Iterable[int], room: str, *, create: bool = False
    ) -> bool:
        """Put every load element in `addresses` into `room`, leaving every other room.

        Membership is what `AddGroupToDevices` sends: the element's OnOff / Level servers subscribe to the room, and
        to the publish group of every key already linked to the room (`reconnectSwitchesWithGroup`); leaving a room
        is the mirror image (`DeleteGroupFromDevices`). One plan, one file rewrite and one gateway upload for
        all the loads of a service call — the gateway reconfigures itself on every upload. A room the export does
        not have is created only with `create`: a typo used to make a new room and move the loads into it
        (review-4 W4-12).
        """
        return await self._change_rooms(
            addresses, room, action="junghome_ble.set_room", create=create, only=True
        )

    async def add_to_room(
        self, address: int, room: str, *, create: bool = False
    ) -> bool:
        """Put the load element at `address` into `room` as well (created when missing with `create`)."""
        return await self.add_to_rooms([address], room, create=create)

    async def add_to_rooms(
        self, addresses: Iterable[int], room: str, *, create: bool = False
    ) -> bool:
        """Put every load element in `addresses` into `room` as well, keeping the rooms it is in (review-4 F4-5).

        The app's `AddDeviceToGroups`: a device can be in several rooms at once. The same `AddGroupToDevices`
        messages as `set_rooms` (the room's Subscription Adds, then each key linked to the room), without leaving
        any other room. A load already in the room sends nothing. Unverified on air.
        """
        return await self._change_rooms(
            addresses, room, action="junghome_ble.add_to_room", create=create
        )

    async def remove_from_room(
        self, address: int, room: str, *, force: bool = False
    ) -> bool:
        """Take the load element at `address` out of `room`, leaving it in its other rooms."""
        return await self.remove_from_rooms([address], room, force=force)

    async def remove_from_rooms(
        self, addresses: Iterable[int], room: str, *, force: bool = False
    ) -> bool:
        """Take every load element in `addresses` out of `room`, keeping the other rooms it is in (review-4 F4-5).

        The app's `DeleteDeviceFromGroups` (`DeleteGroupFromDevices`): the load stops listening to every key linked
        to the room, then every model carrying the room drops it (`ProjectFile.set_room(member=False)`). A load a
        key's room link drives — it listens to the key's group — is refused unless `force`, naming the key: taking
        it out of the room unwires it from the key too, which the user may not expect from a room change. A load
        that is in no room afterwards is fine; the app allows that too. A load not in the room sends nothing.
        Unverified on air.
        """
        return await self._change_rooms(
            addresses,
            room,
            action="junghome_ble.remove_from_room",
            join=False,
            force=force,
        )

    async def _change_rooms(
        self,
        addresses: Iterable[int],
        room: str,
        *,
        action: str,
        join: bool = True,
        only: bool = False,
        create: bool = False,
        force: bool = False,
    ) -> bool:
        """Join `room` (leaving every other room as well with `only`), or leave it: one plan, one rewrite, one upload.

        `create` makes a missing room to join (its creation is the plan's `prepare` note, so a stopped plan
        records it); leaving a room needs one the export has. `force`: leave even where a key's room link drives
        the load (`_room_keys`).
        """
        async with self.lock:
            pf = await self._load()
            before = pf.snapshot()
            elements = [self._element(pf, a) for a in addresses]
            created: str | None = None
            try:
                group = self._room(pf, room)
            except ServiceValidationError:
                if not (join and create):
                    raise
                created = self._room_name(room)
                group = self._add_room(pf, room)
            changes: list[ModelChange] = []
            if join:
                for element in elements:
                    for other in self._rooms_of(pf, element) if only else ():
                        if other != group:
                            changes += pf.set_room(element, other, member=False)
                    changes += pf.set_room(element, group)
            else:
                members = [e for e in elements if group in self._rooms_of(pf, e)]
                if not force:
                    self._refuse_room_keys(pf, members, group)
                for element in members:
                    changes += pf.set_room(element, group, member=False)
            prepare: Note | None = (
                {"kind": "room", "name": created, "address": group}
                if created is not None
                else None
            )
            steps = self._steps(pf, changes)
            if not steps and pf.snapshot() == before:
                return self.adopted  # already so: nothing to send, write or upload
            await self._send(steps, action=action, prepare=prepare)
            await self._save(pf)
            _LOGGER.info(
                "Element(s) %s %s room %r (%04X), %d Config messages",
                ", ".join(f"{e.address:04X}" for e in elements),
                "now in" if join else "taken out of",
                pf.cdb.groups[group],
                group,
                len(changes),
            )
            return True

    @staticmethod
    def _room_keys(pf: ProjectFile, element: Element, group: int) -> list[int]:
        """Return the key elements whose room link to `group` drives `element`: it listens to the link's publish group."""
        listened = {
            address
            for raw in element.raw_models
            for address in element.subscriptions(raw["modelId"])
        }
        return [
            key
            for row in pf.room_connections(group)
            if (key := as_int(row.get("elementAddress"))) is not None
            and as_int(row.get("publishAddress")) in listened
        ]

    def _refuse_room_keys(
        self, pf: ProjectFile, elements: Iterable[Element], group: int
    ) -> None:
        """Refuse taking a load out of `group` while a key's room link to it drives the load (the first one found)."""
        for element in elements:
            if keys := self._room_keys(pf, element, group):
                raise _validation(
                    "service_room_key_drives_load",
                    device=self._member_name(element.address),
                    button=", ".join(self._member_name(k) for k in keys),
                    room=pf.cdb.groups[group],
                )

    # ------------------------------------------------------------------ key connections
    async def assign_key(
        self,
        key_address: int,
        *,
        element: int | None = None,
        room: str | None = None,
        scene: str | int | None = None,
        mode: str | None = None,
    ) -> bool:
        """Wire the key element at `key_address` to a load element (`element`), a `room` or a `scene`.

        `mode` picks the key mode (`light` / `switch` / `move` / `gateway`, rooms also `light_and_switch`); left
        out, a device target gets the mode the app derives from it and a room gets `light`. A scene (number or
        name) is recalled on every node, in key mode *scene*; it takes no `mode`.
        """
        if sum(target is not None for target in (element, room, scene)) != 1:
            raise _validation("service_one_target")
        async with self.lock:
            pf = await self._load()
            key = self._element(pf, key_address)
            if room is not None:
                plan = self._plan_room_link(pf, key, room, mode)
            elif scene is not None:
                plan = self._plan_scene_link(pf, key, scene, mode)
            else:
                assert element is not None  # exactly one target, checked above
                plan = self._plan_device_link(pf, key, element, mode)
            clients = [m for m in KEY_MODE_CLIENTS[plan.key_mode] if has_model(key, m)]
            if not clients:
                raise _validation(
                    "service_key_mode_unsupported",
                    address=hexaddr(key.address),
                    mode=plan.mode,
                )
            if plan.mode in UNTESTED_MODES:
                _LOGGER.warning(
                    "Key mode %r has never been tried on a real device from Home Assistant; check the result in the app",
                    plan.mode,
                )
            steps = list(plan.steps)
            for model in clients:
                bind = self._bind_step(key, model)
                if bind is not None:
                    steps.append(bind)
                steps += self._steps(
                    pf, pf.set_publication(key.node, key, model, plan.publish)
                )
                if (
                    plan.scene is None
                ):  # a scene key only publishes (`ConnectToAddress … PUBLISH_ONLY`)
                    steps += self._steps(pf, pf.subscribe(key, model, plan.publish))
            # a battery key stays held from its first Config step to its KeyMode write (review-3 W4 / F24)
            async with self.hub.keep_awake.hold([key.address]):
                await self._send(
                    steps, action="junghome_ble.assign_key", prepare=plan.prepare
                )
                # every Config step was accepted: the key is wired as planned, whatever the vendor writes do next
                try:
                    if plan.scene is None:
                        await self._reset_property_mode(key.address)
                    else:
                        await self._write_scene_config(key.address, plan.scene)
                        assert (
                            plan.record_scene is not None
                        )  # a scene plan always has it
                        plan.record_scene(pf)
                    await self._write_key_mode(key.address, plan.key_mode)
                except (HomeAssistantError, asyncio.CancelledError):
                    await self._save(pf)
                    raise
            await self._save(pf)
            _LOGGER.info(
                "Key %04X now drives %s in mode %r (publishes to %04X), %d Config messages",
                key.address,
                plan.target,
                plan.mode,
                plan.publish,
                len(steps),
            )
            return True

    async def clear_key(self, key_address: int) -> bool:
        """Give the key no function: drop its room link and every publication / subscription; KeyMode stays (as in the app)."""
        async with self.lock:
            pf = await self._load()
            before = pf.snapshot()
            key = self._element(pf, key_address)
            steps = self._clear_steps(pf, key)
            if not steps and pf.snapshot() == before:
                return self.adopted  # nothing left to clear
            await self._send(steps, action="junghome_ble.clear_key")
            await self._save(pf)
            _LOGGER.info(
                "Key %04X cleared, %d Config messages", key.address, len(steps)
            )
            return True

    # ------------------------------------------------------------------ thresholds (roadmap step 13)
    def _threshold_plan(
        self, pf: ProjectFile, socket_address: int, devices: Iterable[int]
    ) -> tuple[Element, Element, list[Element], int | None]:
        """Return the socket, its OnOff Client, the wanted loads and the client's element group, or refuse.

        The group is None only for an empty list: nothing listens to a group the client does not have.
        """
        socket = self._element(pf, socket_address)
        client = threshold_client(socket.node)
        if client is None:
            raise _validation("threshold_not_supported", name=hexaddr(socket.address))
        wanted = [self._element(pf, address) for address in devices]
        for element in wanted:
            if not has_model(element, ONOFF_SERVER):
                raise _validation("service_not_a_load", name=hexaddr(element.address))
        group = element_groups(pf).get(client.address)
        if group is None and wanted:
            raise _validation(
                "service_no_element_group", address=hexaddr(client.address)
            )
        return socket, client, wanted, group

    async def check_threshold_devices(
        self, socket_address: int, devices: Iterable[int]
    ) -> None:
        """Refuse what `set_threshold_devices` would refuse, writing nothing: the caller checks before the threshold."""
        async with self.lock:
            self._threshold_plan(await self._load(), socket_address, devices)

    async def set_threshold_devices(
        self,
        socket_address: int,
        devices: Iterable[int],
        *,
        applied: Callable[[int, int], str] = applied_text,
    ) -> bool:
        """Make the socket's thresholds switch exactly `devices` (load elements), the wiring of `CreateThreshold`.

        In the app's order (on air): the socket's OnOff Client subscribes to its element's own group
        and publishes there, then each load subscribes its JUNG User Property Server (`0x0527:1013`, where it has
        one) and its OnOff server to that group; a load no longer wanted leaves it (`threshold_wiring`). Both
        thresholds of the socket share the list: the app wires one client for both. The caller writes the
        threshold first, as the app does; `applied` words a stop, with what the call wrote before (W4-13).
        """
        async with self.lock:
            pf = await self._load()
            before = pf.snapshot()
            socket, client, wanted, group = self._threshold_plan(
                pf, socket_address, devices
            )
            if group is None:
                return (
                    self.adopted
                )  # no group, so no load listens to one: nothing to unwire
            steps: list[ConfigStep] = []
            if wanted:
                bind = self._bind_step(client, ONOFF_CLIENT)
                if bind is not None:
                    steps.append(bind)
                steps += self._steps(pf, pf.subscribe(client, ONOFF_CLIENT, group))
                if pf.publication(client, ONOFF_CLIENT) != group:
                    steps += self._steps(
                        pf, pf.set_publication(client.node, client, ONOFF_CLIENT, group)
                    )
            for element, model in threshold_wiring(pf.cdb, client, group):
                if element not in wanted:
                    steps += self._steps(pf, pf.unsubscribe(element, model, group))
            for element in wanted:
                for model in THRESHOLD_TARGET_MODELS:
                    if has_model(element, model):
                        steps += self._steps(pf, pf.subscribe(element, model, group))
            if not steps and pf.snapshot() == before:
                return self.adopted  # already wired so
            await self._send(
                steps, action="junghome_ble.set_threshold", applied=applied
            )
            await self._save(pf)
            _LOGGER.info(
                "Socket %04X's thresholds now switch %s (group %04X), %d Config messages",
                socket.address,
                ", ".join(hexaddr(e.address) for e in wanted) or "nothing",
                group,
                len(steps),
            )
            return True

    async def unwire_threshold(
        self,
        socket_address: int,
        *,
        applied: Callable[[int, int], str] = applied_text,
    ) -> bool:
        """Stop the socket's thresholds switching anything, as the app does when it disables or deletes one.

        On air (the app settings session), once no threshold of the socket is active: every load leaves
        the client's element group (`threshold_wiring`: OnOff server, then `0x0527:1013`), then the client's
        publication is reset — `Publication Set 0x0000` (TTL 0, as the app sends it), then its element group
        again — even when no load was left to unwire. The steps go out in that order (`_send(as_planned=True)`).
        A publication the client does not have to its group is left alone. The app also writes KeyMode 5 to the
        meter element around it, which that element does not hold (`air:access:03-0527:0x5003`): not sent.
        `applied` words a stop, with what the call wrote before (W4-13).
        """
        async with self.lock:
            pf = await self._load()
            before = pf.snapshot()
            socket = self._element(pf, socket_address)
            client = threshold_client(socket.node)
            if client is None:
                raise _validation(
                    "threshold_not_supported", name=hexaddr(socket.address)
                )
            group = element_groups(pf).get(client.address)
            if group is None:
                return self.adopted  # no group, so no load listens to one
            steps: list[ConfigStep] = []
            for element, model in threshold_wiring(pf.cdb, client, group):
                steps += self._steps(pf, pf.unsubscribe(element, model, group))
            if pf.publication(client, ONOFF_CLIENT) == group:
                [off] = pf.set_publication(client.node, client, ONOFF_CLIENT, None)
                steps.append(
                    replace(
                        self._step(pf, off),
                        pdu=C.model_publication_set(
                            client.address, 0, ONOFF_CLIENT, ttl=0
                        ),
                    )
                )
                steps += self._steps(
                    pf, pf.set_publication(client.node, client, ONOFF_CLIENT, group)
                )
            if not steps:
                return self.adopted
            await self._send(
                steps,
                action="junghome_ble.set_threshold / delete_threshold",
                applied=applied,
                as_planned=True,
            )
            if pf.snapshot() == before:
                await (
                    self._journal_close()
                )  # nothing to write: the plan is over all the same
                return (
                    self.adopted
                )  # the publication reset alone: the file already says so
            await self._save(pf)
            _LOGGER.info(
                "Socket %04X's thresholds switch nothing now (group %04X), %d Config messages",
                socket.address,
                group,
                len(steps),
            )
            return True

    # ------------------------------------------------------------------ sensor values for IoT systems
    async def set_sensor_publication(
        self, node_unicast: int, on: bool, *, live: bool | None = None
    ) -> bool:
        """Publish the node's sensor values or stop: the app's *Sensor values for IoT systems*.

        `ConfigurePublicationForSensorServer`: every Sensor Server of the node publishes to its element's own group,
        where the gateway (and Home Assistant) hear it. Off is a `Publication Set` to `0x0000`. The app's publication parameters (TTL 0xFF, no period: the node
        publishes on change) are inferred, not captured.

        `live` is what the node last answered (the switch's read, None when it has not): a node that differs from
        `on` gets its Publication Sets even where the export already agrees (review-4 W4-6) — the app changed it
        since, or never recorded it, and the switch shows the node's state, so a skip would leave it unchangeable.
        Unverified on air.
        """
        async with self.lock:
            pf = await self._load()
            before = pf.snapshot()
            node = pf.cdb.node_by_addr(node_unicast)
            if node is None:
                raise _validation(
                    "service_unknown_element", address=hexaddr(node_unicast)
                )
            groups = element_groups(pf)
            steps: list[ConfigStep] = []
            for element in sensor_elements(node):
                group = groups.get(element.address)
                if on and group is None:
                    raise _validation(
                        "service_no_element_group", address=hexaddr(element.address)
                    )
                want = group if on else None
                if pf.publication(element, SENSOR_SERVER) == want and live in (
                    None,
                    on,
                ):
                    continue
                if on and (bind := self._bind_step(element, SENSOR_SERVER)) is not None:
                    steps.append(bind)
                steps += self._steps(
                    pf, pf.set_publication(node, element, SENSOR_SERVER, want)
                )
            if not steps and pf.snapshot() == before:
                return self.adopted  # already so
            await self._send(steps, action="the switch Sensor values for IoT systems")
            await self._save(pf)
            _LOGGER.info(
                "Node %04X's sensor values %s, %d Config messages",
                node.unicast,
                "published" if on else "no longer published",
                len(steps),
            )
            return True

    # ------------------------------------------------------------------ scenes (roadmap step 12)
    @staticmethod
    def _scene(pf: ProjectFile, scene: str | int) -> int:
        """Resolve a scene given by number ("5") or by the app's name (case-insensitive).

        The services pass both as text, and the app allows an all-digit name: text that is one scene's number and
        another scene's name cannot be told apart, so it is refused rather than guessed (a guess could delete the
        wrong scene from every member node).
        """
        text = str(scene).strip()
        by_name = next(
            (
                n
                for n, name in pf.scene_names().items()
                # a `meta.scenes[]` row left over from a deleted scene names nothing
                if n in pf.cdb.scenes and name.strip().lower() == text.lower()
            ),
            None,
        )
        # `isdigit` alone takes "²" too, which `int` refuses
        number_text = text.isascii() and text.isdigit()
        by_number = int(text) if number_text and int(text) in pf.cdb.scenes else None
        if by_name is not None and by_number is not None and by_name != by_number:
            raise _validation("service_ambiguous_scene", scene=text)
        number = by_name if by_name is not None else by_number
        # scene 0 is not a scene the Scene Store / Scene Action Setup messages can carry (the builders refuse it)
        if number is None or number < 1 or number not in pf.cdb.scenes:
            raise _validation("service_unknown_scene", scene=text)
        return number

    async def _reply(  # the request, its wait, and what an error says was applied before it
        self,
        element: int,
        pdu: bytes,
        expect: int,
        timeout: float,
        retries: int,
        applied: str,
        scene: int | None = None,
    ) -> AccessMessage | None:
        """Send an AppKey request to `element` and wait for its status; None when it stays silent.

        `scene`: a Scene Action Setup request's scene — a status naming another scene does not answer it (review-3
        Q2): matched on source and opcode alone, a late duplicate of the element's answer about another scene would
        pass for it. One too short to name any scene still does: the callers treat it as malformed.
        A lost link names the node and the message, and says what `applied` before it (as a stopped plan does).
        """
        fits = None if scene is None else V.scene_action_reply_to(scene)
        try:
            return await self.hub.proxy.request(
                element,
                pdu,
                expect,
                timeout=timeout,
                retries=retries,
                expect_cid=M.JUNG_CID
                if expect == V.SCENE_ACTION_SETUP_STATUS
                else None,
                match=None
                if fits is None
                else lambda m: len(m.params) < 2 or fits(m.params),
            )
        except TimeoutError:
            return None
        except (ConnectionError, OSError) as err:
            raise _failure(
                "service_send_failed",
                node=hexaddr(element),
                message=M.describe(pdu),
                applied=applied,
            ) from err

    async def _scene_register(
        self, element: int, pdu: bytes, applied: str
    ) -> tuple[M.SceneRegister, bool]:
        """Send a Scene Store / Delete and return the element's Scene Register afterwards, and whether it was read back.

        The acknowledged Store / Delete is given a short wait for its Scene Register Status; JUNG firmware tends to
        publish state changes to the model's (unset) publish address instead of replying, so the register is read
        back when nothing arrives. A read-back register always carries status Success — whether the Store took
        is then told by the scene being in it or not, which the caller words accordingly. `applied` is what the
        error says about the steps before this one.
        """
        read_back = False
        reply = await self._reply(
            element, pdu, M.SCENE_REGISTER_STATUS, SCENE_TIMEOUT, 1, applied
        )
        if reply is None:
            _LOGGER.debug("%04X: no Scene Register Status, reading it back", element)
            read_back = True
            reply = await self._reply(
                element,
                M.scene_register_get(),
                M.SCENE_REGISTER_STATUS,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
            )
        if reply is None:
            raise _failure(
                "service_no_reply",
                node=hexaddr(element),
                message=M.describe(pdu),
                applied=applied,
            )
        try:
            return M.decode_scene_register_status(reply.params), read_back
        except ValueError as err:
            raise _failure(
                "service_config_refused",
                node=hexaddr(element),
                message=M.describe(pdu),
                status="malformed status",
                applied=applied,
            ) from err

    async def _scene_action(
        self, element: int, scene: int, action: V.Action | None, applied: str
    ) -> None:
        """Scene Action Setup Set (`action` None removes) and confirm it — from the Set's status or a Get."""
        pdu = V.scene_action_set(scene, action or V.NO_ACTION)
        reply = await self._reply(
            element, pdu, V.SCENE_ACTION_SETUP_STATUS, SCENE_TIMEOUT, 1, applied, scene
        )
        if reply is None or not _confirms_scene_action(reply.params, scene, action):
            _LOGGER.debug(
                "%04X: no Scene Action Setup Status, reading it back", element
            )
            reply = await self._reply(
                element,
                V.scene_action_get(scene),
                V.SCENE_ACTION_SETUP_STATUS,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
                scene,
            )
            if reply is None:
                raise _failure(
                    "service_no_reply",
                    node=hexaddr(element),
                    message=M.describe(pdu),
                    applied=applied,
                )
        if not _confirms_scene_action(reply.params, scene, action):
            raise _failure(
                "service_scene_action_not_applied",
                address=hexaddr(element),
                scene=str(scene),
                applied=applied,
            )

    def _scene_load(self, pf: ProjectFile, address: int) -> tuple[Element, Element]:
        """Return the load element at `address` and the element its scenes are stored on.

        The app stores a device's scenes on the node's *first* Scene Setup Server — the primary element, for both
        channels of a two-channel node (network-features.md §3) — and writes the JUNG scene action to the channel
        element itself when that has a Scene Action Setup server.
        """
        element = self._element(pf, address)
        store = next(
            (e for e in element.node.elements if has_model(e, SCENE_SETUP_SERVER)),
            None,
        )
        if store is None or not (
            has_model(element, SCENE_SETUP_SERVER)
            or has_model(element, SCENE_ACTION_SETUP)
        ):
            raise _validation("service_not_a_scene_element", address=hexaddr(address))
        return element, store

    async def _sibling_uses_scene(
        self, element: Element, number: int, applied: str
    ) -> bool:
        """Whether another channel of the node still holds a JUNG action for `number` (its register is shared).

        `applied` is what an error says was done before the check (review-3 W14: the caller knows whether this
        channel's description was cleared at all, and which loads of the call were removed before it).
        """
        for other in _sibling_channels(element):
            reply = await self._reply(
                other.address,
                V.scene_action_get(number),
                V.SCENE_ACTION_SETUP_STATUS,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
                number,
            )
            if reply is None:
                raise _failure(
                    "service_no_reply",
                    node=hexaddr(other.address),
                    message=M.describe(V.scene_action_get(number)),
                    applied=applied,
                )
            try:
                status = V.decode_scene_action_status(reply.params)
            except ValueError:
                continue
            if status.scenes is None and status.action is not None:
                return True
        return False

    async def create_scene(self, name: str, icon: str | None = None) -> int:
        """Create an empty scene (CDB `scenes[]` + `meta.scenes[]`); nothing goes on air. Returns its number.

        Not a number a device still holds after a forced deletion skipped it (`held_scenes`): that device would
        join every recall of the new scene (review-4 W4-8).
        """
        async with self.lock:
            pf = await self._load()
            held_numbers = {number for number, _ in await self._held_scenes()}
            try:
                number = pf.add_scene(
                    name, icon=icon or DEFAULT_SCENE_ICON, avoid=held_numbers
                )
            except InvalidName as err:
                raise _name_error(err, name) from err
            except ValueError as err:
                raise _validation("service_scene_exists", scene=name) from err
            except AllocationCrowded as err:
                raise _validation(
                    "service_scene_range_crowded", free=str(err.below)
                ) from err
            except ExportError as err:
                raise _validation("service_scene_range_full") from err
            await self._save(pf)
            _LOGGER.info("Created scene %r as number %d", name, number)
            return number

    async def rename_scene(self, scene: str | int, name: str) -> bool:
        """Rename a scene in the CDB and `meta.scenes[]`; nothing goes on air."""
        async with self.lock:
            pf = await self._load()
            number = self._scene(pf, scene)
            before = pf.snapshot()
            try:
                pf.rename_scene(number, name)
            except InvalidName as err:
                raise _name_error(err, name) from err
            except ValueError as err:
                raise _validation("service_scene_exists", scene=name) from err
            if pf.snapshot() == before:
                return self.adopted  # already called that
            await self._save(pf)
            _LOGGER.info("Renamed scene %d to %r", number, name)
            return True

    async def store_scene(
        self, scene: str | int, address: int, action: V.Action | None
    ) -> bool:
        """Store the load's current state under `scene`, as the app's "save device into scene" does."""
        return await self.store_scenes(scene, [(address, action)])

    async def store_scenes(
        self, scene: str | int, loads: Iterable[tuple[int, V.Action | None]]
    ) -> bool:
        """Store every load's current state under `scene`, one file rewrite and gateway upload for all of them.

        Per load: `Scene Store` to the node's Scene Setup Server (the SIG register — what a Scene Recall
        restores), then the JUNG `Scene Action Setup Set` with the load's action on the channel element (what the
        app shows for the member; None when the state is unknown, which leaves the vendor record alone), then the
        export's `scenes[].addresses` (the element the Store went to, as the app's library records it). A load
        that fails stops the call; the members stored before it are recorded (apply-and-record).
        """
        async with self.lock:
            pf = await self._load()
            number = self._scene(pf, scene)
            targets = [(*self._scene_load(pf, a), action) for a, action in loads]
            before = list(pf.cdb.scenes.get(number, []))
            for done, (element, store, action) in enumerate(targets):
                applied = applied_scene_members(done, len(targets), number)
                try:
                    await self._store_one(pf, number, element, store, action, applied)
                except (HomeAssistantError, asyncio.CancelledError):
                    # the one save of a stopped (or cancelled) call: the loads stored before, and one whose Store
                    # took but whose description did not — recorded either way
                    if pf.cdb.scenes.get(number, []) != before:
                        await self._save(pf)
                    raise
            await self._save(pf)
            return True

    async def _store_one(
        self,
        pf: ProjectFile,
        number: int,
        element: Element,
        store: Element,
        action: V.Action | None,
        applied: str,
    ) -> None:
        """Store one load's scene; `applied` says what the loads before it left recorded (for the error).

        A channel whose state is unknown (`action` None) is not stored beside another light or socket channel of
        its node with a Scene Action Setup server (review-3 W6): the app — and `_forget_scene` after it — tells
        the members of such a node by their JUNG action, so removing the other channel from the scene would
        delete the shared register and drop this one without a word. (A blind's slat element is no channel a
        scene is stored on by itself.)
        """
        if (
            action is None
            and has_model(element, SCENE_ACTION_SETUP)
            and any(
                load_kind(other) in ("light", "socket")
                for other in _sibling_channels(element)
            )
        ):
            raise _failure(
                "service_scene_state_unknown",
                address=hexaddr(element.address),
                scene=str(number),
                applied=applied,
            )
        await self._check_capacity(element, store, number, applied)
        register, read_back = await self._scene_register(
            store.address, M.scene_store(number), applied
        )
        if not register.ok or number not in register.scenes:
            raise _failure(
                "service_scene_not_stored",
                address=hexaddr(store.address),
                scene=str(number),
                status=(
                    "not in the register after read-back"
                    if read_back and register.ok
                    else _scene_register_status_name(register.status)
                ),
                applied=applied,
            )
        # stored: the member is recorded whatever the description write does next
        pf.set_scene_addresses(number, [*pf.cdb.scenes.get(number, []), store.address])
        if action is not None and has_model(element, SCENE_ACTION_SETUP):
            await self._scene_action(
                element.address,
                number,
                action,
                applied_scene_stored(store.address, number),
            )
        _LOGGER.info(
            "Stored scene %d on %04X (%s)",
            number,
            element.address,
            action.describe() if action else "no action record",
        )

    async def _check_capacity(
        self, element: Element, store: Element, number: int, applied: str
    ) -> None:
        """Refuse a Scene Store the device has no room for, before it is sent (the app's capacity check).

        A channel of a node whose channels keep their own scene list (Scene Action Setup, beside a sibling) is
        asked for that list (`Scene Action Setup Get` scene 0) and may hold `SCENE_ACTION_CAPACITY`; any other
        load's node is asked for its register (`Scene Register Get`), which may hold `SCENE_REGISTER_CAPACITY`.
        Unlike the app, a scene the device already holds is no new slot: storing it again is always allowed. As in
        the app, a register that does not answer is no reason to refuse (the Store says whether it took); a
        channel list that does not answer stops the call — the app takes it for a full one.
        """
        if has_model(element, SCENE_ACTION_SETUP) and _sibling_channels(element):
            reply = await self._reply(
                element.address,
                V.scene_action_get(),
                V.SCENE_ACTION_SETUP_STATUS,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
                V.SCENE_LIST,
            )
            if reply is None:
                raise _failure(
                    "service_no_reply",
                    node=hexaddr(element.address),
                    message=M.describe(V.scene_action_get()),
                    applied=applied,
                )
            try:
                held = V.decode_scene_action_status(reply.params).scenes or ()
            except ValueError:
                held = ()  # a status too short to name the list: no list, as for an unanswered register
            where, capacity = element.address, SCENE_ACTION_CAPACITY
        else:
            reply = await self._reply(
                store.address,
                M.scene_register_get(),
                M.SCENE_REGISTER_STATUS,
                CONFIG_TIMEOUT,
                CONFIG_RETRIES,
                applied,
            )
            try:
                held = (
                    ()
                    if reply is None
                    else M.decode_scene_register_status(reply.params).scenes
                )
            except ValueError:
                held = ()
            where, capacity = store.address, SCENE_REGISTER_CAPACITY
        if number not in held and len(held) >= capacity:
            raise _failure(
                "service_scene_no_capacity",
                address=hexaddr(where),
                scene=str(number),
                capacity=str(capacity),
                applied=applied,
            )

    def _scene_key_steps(
        self, pf: ProjectFile, nodes: Iterable[Node], number: int
    ) -> list[ConfigStep]:
        """`RemoveConnectionForAddress.SceneConnection`: clear the keys of `nodes` wired to recall scene `number`.

        The app runs it first when a device leaves a scene (network-features.md §3 *Remove device from scene*): a
        key of that device whose cached scene link (`keyModeSceneConfigExports`, the app's `KeyModeSceneConfig`
        cache) names the scene loses its connections, as `clear_key` clears a key. Keys of other devices keep
        their link, as in the app. Unlike the app, the row alone is not enough: the key's Scene Client must still
        publish to all nodes, the scene key's wiring — a row can outlive the link it cached (review-3 W2), and
        clearing a key wired to a load or the gateway because of it would take a working key away.
        """
        linked = {
            as_int(row.get("elementAddress"))
            for row in meta_rows(meta_list(pf.meta.get("keyModeSceneConfigExports")))
            if isinstance(config := row.get("sceneConfig"), dict)
            and as_int(config.get("sceneId")) == number
        }
        steps: list[ConfigStep] = []
        for node in {n.unicast: n for n in nodes}.values():
            for key in node.elements:
                if (
                    key.address in linked
                    and has_model(key, SCENE_CLIENT)
                    and pf.publication(key, SCENE_CLIENT) == ALL_SCENES
                ):
                    _LOGGER.info(
                        "Key %04X recalls scene %d: its connections are cleared with the scene",
                        key.address,
                        number,
                    )
                    steps += self._clear_steps(pf, key)
        return steps

    async def remove_from_scene(self, scene: str | int, address: int) -> bool:
        """Take a load out of a scene: its JUNG action removed, `Scene Delete` unless a sibling channel still uses it."""
        return await self.remove_from_scenes(scene, [address])

    async def remove_from_scenes(
        self, scene: str | int, addresses: Iterable[int]
    ) -> bool:
        """Take every load in `addresses` out of a scene; one file rewrite and gateway upload for all of them."""
        async with self.lock:
            pf = await self._load()
            number = self._scene(pf, scene)
            targets = [self._scene_load(pf, a) for a in addresses]
            # the keys of those devices that recall the scene first, as the app does (a stop records itself)
            keys = self._scene_key_steps(pf, [e.node for e, _ in targets], number)
            await self._send(keys, action="junghome_ble.remove_from_scene")
            for done, (element, store) in enumerate(targets):
                try:
                    await self._forget_scene(
                        pf, element, store, number, done, len(targets), bool(keys)
                    )
                except (HomeAssistantError, asyncio.CancelledError):
                    if done or keys:  # the loads before this one, the keys: recorded
                        await self._save(pf)
                    raise
                pf.remove_scene_info(number, element.node, element.location)
                _LOGGER.info("Removed %04X from scene %d", element.address, number)
            await self._save(pf)
            return True

    async def delete_scene(self, scene: str | int, *, force: bool = False) -> list[str]:
        """Delete a scene: every element that stored it forgets it (every channel's action too), then the CDB / `meta` entries go.

        The keys of the members that recall the scene are cleared first (`_scene_key_steps`, as the app's
        *remove device from scene* does for every member). A member that cannot be reached, or refuses, stops the
        deletion with what was done recorded — unless `force`, the app's *Delete anyway*
        (`removeScene(scene, force)`): then that member is skipped, keeps the scene in its register, and the scene
        leaves the export all the same. A skipped member's number is held (`held_scenes`, the `scene_held` repair
        names it) until `delete_unused_scenes` deletes it there: a new scene with that number would also recall
        the skipped member (review-4 W4-8). Returns the skipped members (`["0232"]`); the device model always
        changes.
        """
        async with self.lock:
            pf = await self._load()
            number = self._scene(pf, scene)
            members = [
                e
                for a in pf.cdb.scenes.get(number, [])
                if (e := pf.cdb.element(a)) is not None
            ]
            keys = self._scene_key_steps(pf, [e.node for e in members], number)
            try:
                await self._send(keys, action="junghome_ble.delete_scene")
            except HomeAssistantError as err:
                if not force:
                    raise
                _LOGGER.warning(
                    "Scene %d: the keys recalling it were not all cleared (%s); deleted anyway",
                    number,
                    err,
                )
                pf = (
                    await self._load()
                )  # what the stopped plan recorded, not what it planned
            skipped: list[int] = []
            for done, stored_on in enumerate(members):
                applied = applied_members(
                    done, len(members), number, keys_cleared=bool(keys)
                )
                try:
                    for channel in stored_on.node.elements:
                        if has_model(channel, SCENE_ACTION_SETUP):
                            await self._scene_action(
                                channel.address, number, None, applied
                            )
                    await self._delete_from_register(pf, stored_on, number, applied)
                except (HomeAssistantError, asyncio.CancelledError) as err:
                    if force and isinstance(err, HomeAssistantError):
                        _LOGGER.warning(
                            "Scene %d: %04X did not forget it (%s); deleted anyway",
                            number,
                            stored_on.address,
                            err,
                        )
                        skipped.append(stored_on.address)
                        continue
                    if done or keys:  # the members before this one, the keys: recorded
                        await self._save(pf)
                    raise
            if skipped:
                # held before the export lets the number go, so no later call can hand it out in between
                await self._hold_scenes(
                    await self._held_scenes() | {(number, a) for a in skipped}
                )
            pf.remove_scene(number)
            await self._save(pf)
            _LOGGER.info(
                "Deleted scene %d%s",
                number,
                f" (still stored on {', '.join(hexaddr(a) for a in skipped)})"
                if skipped
                else "",
            )
            return [hexaddr(a) for a in skipped]

    async def delete_unused_scenes(
        self,
        *,
        dry_run: bool = DEFAULT_UNUSED_SCENES_DRY_RUN,
        numbers: Collection[int] | None = None,
        confirm_stale_export: bool = False,
    ) -> dict[str, list[int] | list[str]]:
        """Delete from every node's scene register the scenes the export does not know (the app's `DeleteUnusedScenes`).

        `Scene Register Get` to each node's first Scene Setup Server, then `Scene Delete` for every number that is
        no scene of the export — the app's scenes and the scenes of its timers are all in the CDB, so neither is
        touched. The app does this unasked, per device, whenever its timer list opens; here it is an action.
        Returns the deleted numbers by register element (`"0148": [5]`), plus `"unanswered"`: the elements that
        did not answer the Get, left alone. A Delete the node does not carry out stops the call, as elsewhere,
        and the error names the numbers deleted before it (`applied_unused_deleted`); nothing of this is in the
        export, so nothing is written.

        Judged by what the export *lacks*, so only on an export known to be current (review-4 W4-3): the app's
        scenes made since a file was exported are no scene of that file, and the call deleted them from every
        device while the app still listed them. A `dry_run` (the default, decision M3) sends the Gets only and
        answers what it would delete. A gateway entry plans on the gateway's export or not at all
        (`service_gateway_export_unavailable`, dry run included); an entry set up from a file deletes only with
        `confirm_stale_export` (the user vouches for the file) or the `numbers` to delete, which restrict the
        call either way and must be no scene of the export. A held number (`held_scenes`) is let go once its
        register no longer holds it. Unverified on air: an app scene taken over from the gateway before the dry run.
        """
        async with self.lock:
            pf = await self._load(fresh=True)
            known = set(pf.cdb.scenes)
            if numbers is not None and (named := sorted(set(numbers) & known)):
                raise _validation(
                    "service_unused_scenes_known",
                    numbers=", ".join(str(n) for n in named),
                )
            if (
                not dry_run
                and self.gateway is None
                and numbers is None
                and not confirm_stale_export
            ):
                raise _validation("service_unused_scenes_stale_export")
            wanted = None if numbers is None else set(numbers)
            registers = [
                store
                for node in pf.cdb.nodes
                if node.pid is not None
                and (
                    store := next(
                        (e for e in node.elements if has_model(e, SCENE_SETUP_SERVER)),
                        None,
                    )
                )
                is not None
            ]
            held_before = await self._held_scenes()
            still_held = set(held_before)
            deleted: dict[str, list[int]] = {}
            unanswered: list[int] = []
            try:
                for store in registers:
                    reply = await self._reply(
                        store.address,
                        M.scene_register_get(),
                        M.SCENE_REGISTER_STATUS,
                        CONFIG_TIMEOUT,
                        CONFIG_RETRIES,
                        applied_unused_deleted(deleted),
                    )
                    try:
                        holds = (
                            None
                            if reply is None
                            else M.decode_scene_register_status(reply.params).scenes
                        )
                    except ValueError:
                        holds = None
                    if holds is None:
                        unanswered.append(store.address)
                        continue
                    on_node = {e.address for e in store.node.elements}
                    if not dry_run:  # a dry run changes nothing, not even this record
                        still_held -= {
                            (n, e)
                            for n, e in still_held
                            if e in on_node and n not in holds
                        }
                    for number in holds:
                        if number in known or (
                            wanted is not None and number not in wanted
                        ):
                            continue
                        if not dry_run:
                            await self._delete_unused(store, number, deleted)
                            still_held -= {
                                (n, e)
                                for n, e in still_held
                                if n == number and e in on_node
                            }
                        deleted.setdefault(hexaddr(store.address), []).append(number)
            finally:
                if still_held != held_before:
                    await self._hold_scenes(still_held)
            result: dict[str, list[int] | list[str]] = dict(deleted)
            result["unanswered"] = [hexaddr(a) for a in unanswered]
            return result

    async def _delete_unused(
        self, store: Element, number: int, deleted: dict[str, list[int]]
    ) -> None:
        """`Scene Delete` of a number the export does not know, checked; `deleted` is what went before it."""
        register, _read_back = await self._scene_register(
            store.address, M.scene_delete(number), applied_unused_deleted(deleted)
        )
        if number in register.scenes:
            raise _failure(
                "service_scene_not_deleted",
                address=hexaddr(store.address),
                scene=str(number),
                status=_scene_register_status_name(register.status),
                applied=applied_unused_deleted(deleted),
            )
        _LOGGER.info(
            "Deleted scene %d, unknown to the export, from %04X", number, store.address
        )

    async def _forget_scene(
        self,
        pf: ProjectFile,
        element: Element,
        store: Element,
        number: int,
        done: int,
        total: int,
        keys_cleared: bool = False,
    ) -> None:
        """Run the app's *remove device from scene* for one channel, the `done`-th of the call's `total` loads.

        Clear the channel's JUNG action, then delete the scene from the node's (shared) register unless another
        channel of the node still holds an action for it. A stop after the action was cleared leaves the file
        as it is — it records the register, which is still held — and says so; a stop before says what the
        loads before this one left recorded.
        """
        applied = applied_members(done, total, number, keys_cleared=keys_cleared)
        if has_model(element, SCENE_ACTION_SETUP):
            await self._scene_action(element.address, number, None, applied)
            applied = applied_scene_cleared(element.address, number, done, total)
        if await self._sibling_uses_scene(element, number, applied):
            _LOGGER.debug(
                "%04X: another channel still uses scene %d, register kept",
                store.address,
                number,
            )
            return
        await self._delete_from_register(pf, store, number, applied)

    async def _delete_from_register(
        self, pf: ProjectFile, store: Element, number: int, applied: str
    ) -> None:
        """`Scene Delete` on the element holding the register, checked, then the export's member list."""
        register, _read_back = await self._scene_register(
            store.address, M.scene_delete(number), applied
        )
        if number in register.scenes:
            raise _failure(
                "service_scene_not_deleted",
                address=hexaddr(store.address),
                scene=str(number),
                status=_scene_register_status_name(register.status),
                applied=applied,
            )
        pf.set_scene_addresses(
            number, [a for a in pf.cdb.scenes.get(number, []) if a != store.address]
        )


def _sibling_channels(element: Element) -> list[Element]:
    """Return the node's other elements with a Scene Action Setup server: the channels sharing its register."""
    return [
        other
        for other in element.node.elements
        if other is not element and has_model(other, SCENE_ACTION_SETUP)
    ]


def _confirms_scene_action(params: bytes, scene: int, action: V.Action | None) -> bool:
    """Whether a Scene Action Setup Status reports `action` for `scene` (None = no action stored)."""
    try:
        status = V.decode_scene_action_status(params)
    except ValueError:
        return False
    return status.scenes is None and status.scene == scene and status.action == action


def _scene_register_status_name(status: int) -> str:
    return {
        0: "Success",
        M.SCENE_REGISTER_FULL: "Scene Register Full",
        M.SCENE_NOT_FOUND: "Scene Not Found",
    }.get(status, f"status 0x{status:02X}")


def scene_action_for(kind: str, state: Any, slat: Any = None) -> V.Action | None:
    """Return the JUNG scene action describing a load's present state, per load kind (`Device.kind`).

    Switch inserts and sockets store *switch on/off*; dimmers a lightness (0 when off); tunable-white lights a
    lightness with the colour temperature — what the app writes and what the nodes were seen to hold. A blind
    stores its position and slat levels (`slat`: the slat element's state; None for a blind without slats, whose
    slat field repeats the position), a thermostat its set-point (`p044d6/i.java`; neither seen on a device).
    None when the state needed is not known yet (the Scene Store still happens; only the vendor record is left
    alone) and for kinds without a record.
    """
    action: V.Action | None = None
    if kind == "blind":
        slat_level = state.level if slat is None else slat.level
        if state.level is not None and slat_level is not None:
            action = V.Action(V.ACTION_BLINDS, blind=state.level, slat=slat_level)
        return action
    if kind == "thermostat":
        if state.level is not None:
            action = V.Action(
                V.ACTION_TEMPERATURE, temperature_c=level_to_temperature(state.level)
            )
        return action
    known = state.on is not None or state.lightness is not None
    if kind in ("switch", "socket") and state.on is not None:
        action = V.Action(V.ACTION_SWITCH, on=state.on)
    elif kind == "dimmer" and known:
        action = V.Action(V.ACTION_LIGHTNESS, lightness=_lit(state))
    elif kind == "ctl" and known and state.kelvin is not None:
        action = V.Action(
            V.ACTION_LIGHTNESS_CT, lightness=_lit(state), temperature_k=state.kelvin
        )
    return action


def _lit(state: Any) -> int:
    """Return a dimmer's lightness for its scene record: 0 when it is off, else what it shows."""
    if state.on is False:
        return 0
    return int(state.lightness or 0)


# the pending retry of each entry's failed automatic upload (`MeshConfigurator._upload_or_retry`), by entry id
UPLOAD_RETRIES: HassKey[dict[str, asyncio.Task[None]]] = HassKey(
    f"{DOMAIN}_upload_retry"
)
RELOAD_POLL = 1.0  # seconds between looks at an entry a retry found mid-reload


async def _retry_upload(hass: HomeAssistant, entry_id: str, left: int) -> None:
    """Upload the entry's export again every `GATEWAY_UPLOAD_RETRY_DELAY` seconds, `left` times at most.

    Each attempt goes through the entry's configurator of the moment (a reload after the change may have replaced
    the one that failed), under its lock, with the export as it is on disk then: every later save cancels this before it
    uploads its own, so that is still the changed export. An attempt that comes while the entry is being set up
    or reloaded (its setup lock held) waits for that; an entry not loaded after all (unloaded, disabled, its
    setup failed) or an export that does not load ends the retries.
    """
    try:
        for attempt in range(1, left + 1):
            await asyncio.sleep(GATEWAY_UPLOAD_RETRY_DELAY)
            entry = hass.config_entries.async_get_entry(entry_id)
            while entry is not None and entry.setup_lock.locked():
                await asyncio.sleep(RELOAD_POLL)
                entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.state is not ConfigEntryState.LOADED:
                _LOGGER.info(
                    "Not handing the mesh export to the gateway again: the entry is not loaded"
                )
                break
            configurator: MeshConfigurator = entry.runtime_data.configurator
            async with configurator.lock:
                _LOGGER.info(
                    "Handing the mesh export to the gateway again (retry %d of %d)",
                    attempt,
                    left,
                )
                try:
                    pf = await configurator._read()  # noqa: SLF001  # the module's own class
                except HomeAssistantError as err:
                    _LOGGER.warning(
                        "Not handing the mesh export to the gateway again: %s", err
                    )
                    break
                if await configurator._upload(pf, raise_on_failure=False) != "failed":  # noqa: SLF001
                    break
    finally:
        retries = hass.data.get(UPLOAD_RETRIES, {})
        if retries.get(entry_id) is asyncio.current_task():
            del retries[entry_id]


@callback
def cancel_upload_retry(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the entry's pending retry of a failed upload (a newer upload supersedes it, or the entry is removed)."""
    if (task := hass.data.get(UPLOAD_RETRIES, {}).pop(entry_id, None)) is not None:
        task.cancel()
