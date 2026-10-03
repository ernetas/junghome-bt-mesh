"""Take over the entities of the JUNG HOME Gateway integration (`junghome`) so a switch to the mesh keeps history.

Since Home Assistant 2026.8 a device belongs to exactly one config entry, so the two integrations' devices can never
merge; what can move is the *entity registry entry*: `er.async_update_entity_platform` re-homes a gateway entity
onto this integration's config entry, unique id and device, and the entity keeps its entity id (hence its recorder
history and every automation that names it), its user-given name, area, icon, labels and aliases. The gateway entry
must be unloaded for that, and it is left disabled afterwards, for the user to delete.

Matching works on the registries alone (no gateway integration code, no loaded hub), so it can be planned and tested
against synthetic registries. Our entity is decoded from its unique id; the gateway's from the label-slug scheme of
its `const.py` (`{slug}_{datapoint suffix}[_{qualifier}]`, devices identified by `("junghome", slug)`, scenes by
`{scope}_{slug}_scene`):

| ours | the gateway's counterpart on the device whose slug is `slugify(our device name)` |
|---|---|
| light `{node}-{loc}` | the one `light` with unique id `{slug}_{suffix}` |
| socket switch `{node}-{loc}` | the one `switch` with unique id `{slug}_{suffix}` (no qualifier) |
| status LED switch `…-key_status_led` of key X | the `switch` `{slug}_x_{suffix}_switch` on the key's own device, else key A's on the gang device |
| sensor `…-{power,voltage,current}` | the `sensor` `{slug}_{suffix}_{same word}` |
| event of key X | the `up` event on the key's own device `{slug}_x`, else `up` / `down` on the gang device for key A / B |
| scene `{mesh}-scene-{n}` named N | the `scene` whose unique id ends in `_{slugify(N)}_scene` |

Keys: the gateway integration exposes one `RockerSwitch` function per key element with an `up` and a `down` event
entity and a status-LED switch, on a device named after the app's key label — `<gang name> <letter>` (slug
`<gang slug>_<letter>`, seen live) or, on older registrations, the gang itself. Our one event entity
per key carries both directions as event types, so it takes over the key's `up` entity and the `down` entity stays
with the gateway (listed as gateway-only); on a gang-named device the `up` entity goes to key A and `down` to key
B. Device customisations (area, name, labels) are copied from the gateway device with the gang's own slug only.
Keys C and D on gang-named devices, the power-on-time sensor, the proxy sensor and the device-parameter entities
have no gateway counterpart, and the gateway's energy sensor has none here (the sockets report no total-energy
counter over the mesh).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import slugify

from .const import DOMAIN, GATEWAY_DOMAIN, ISSUE_GATEWAY_IMPORT, learn_more_url
from .jhmesh.devices import BUTTON_LETTERS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

SUPPORTED_DOMAINS = frozenset({"light", "switch", "sensor", "event", "scene"})
SENSOR_KEYS = frozenset({"power", "voltage", "current"})
KEY_SIDES = {
    "A": "up",
    "B": "down",
}  # our key letter → the gateway's event translation key (see module docstring)

_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_ELEMENT_UID = re.compile(rf"^{_UUID}-(?P<location>[0-9a-f]{{4}})(?:-(?P<key>.+))?$")
_SCENE_UID = re.compile(rf"^{_UUID}-scene-\d+$")


@dataclass(frozen=True)
class Thing:
    """What one of our entities represents: `kind` and the qualifier the gateway id must carry as well."""

    kind: str  # light | socket | status_led | sensor | key | scene
    qualifier: str | None = None  # sensor word, key letter or scene name


@dataclass(frozen=True)
class Match:
    """Our fresh entity and the gateway entity that stands for the same thing."""

    ours: er.RegistryEntry
    theirs: er.RegistryEntry


@dataclass
class ImportPlan:
    """What the import would do, or did: computed from the registries, so a second run finds nothing left."""

    gateway_entries: list[ConfigEntry] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    customised: list[Match] = field(
        default_factory=list
    )  # matched, but our entity was already renamed / customised: left alone
    unmatched: list[er.RegistryEntry] = field(
        default_factory=list
    )  # ours without a gateway counterpart
    gateway_only: list[er.RegistryEntry] = field(
        default_factory=list
    )  # the gateway's without a counterpart of ours
    devices: list[tuple[dr.DeviceEntry, dr.DeviceEntry]] = field(
        default_factory=list
    )  # (ours, theirs) whose area / name / labels are copied

    @property
    def empty(self) -> bool:
        """Nothing to move."""
        return not self.matches

    @property
    def contributing_entries(self) -> list[ConfigEntry]:
        """The gateway entries at least one matched entity belongs to: the ones the import unloads and disables."""
        owners = {m.theirs.config_entry_id for m in self.matches}
        return [gw for gw in self.gateway_entries if gw.entry_id in owners]


# ------------------------------------------------------------------ classification


def classify(entity: er.RegistryEntry) -> Thing | None:
    """Decode one of our registry entries into a matchable thing; None for entities the gateway never had."""
    if entity.domain == "scene":
        named = _SCENE_UID.match(entity.unique_id) and entity.original_name
        return Thing("scene", entity.original_name) if named else None
    m = _ELEMENT_UID.match(entity.unique_id)
    if m is None:
        return None
    key = m["key"]
    letter = BUTTON_LETTERS.get(int(m["location"], 16))
    if key is None:
        by_domain = {
            "light": Thing("light"),
            "switch": Thing("socket"),
            "event": Thing("key", letter) if letter else None,
        }
        return by_domain.get(entity.domain)
    if entity.domain == "switch" and key == "key_status_led" and letter:
        return Thing("status_led", letter)
    if entity.domain == "sensor" and key in SENSOR_KEYS:
        return Thing("sensor", key)
    return None


def scene_slug(label: str) -> str:
    """Return the gateway integration's slug of a scene label (`const.scene_slug` there)."""
    slug = slugify(label)
    return slug if slug and slug != "unknown" else "scene"


def _one(candidates: Iterable[er.RegistryEntry]) -> er.RegistryEntry | None:
    found = list(candidates)
    return found[0] if len(found) == 1 else None


def device_slugs(slug: str, thing: Thing, ordinal: str | None = None) -> list[str]:
    """Return the gateway device slugs to try for `thing` on our device `slug`, most specific first.

    A key's own device carries its letter: ours is positional (element location, so a 2-gang with its two keys
    left and right is A and C), the gateway integration's is ordinal (the same keys are its A and B) — the
    positional slug is tried first, then the `ordinal` one, then the gang's own slug.
    """
    if thing.kind in ("key", "status_led") and thing.qualifier:
        slugs = [f"{slug}_{thing.qualifier.lower()}"]
        if ordinal and ordinal != thing.qualifier.lower():
            slugs.append(f"{slug}_{ordinal}")
        return [*slugs, slug]
    return [slug]


def counterpart(
    thing: Thing, slug: str, on_device: list[er.RegistryEntry], *, per_key: bool = False
) -> er.RegistryEntry | None:
    """Return the single gateway entity among `on_device` (its entities on the device `slug`) standing for `thing`.

    `per_key`: the device is one key's own (`<gang>_<letter>`), so its one status LED and its `up` event are the
    key's whatever its letter; otherwise the gang rule applies (status LED and `up` to key A, `down` to key B) —
    and keys C / D have no side there, so they match nothing (not an event that merely lacks a translation key).
    A per-key slug is only trusted when the device behind it looks like a key's: events and a status LED, no
    light, socket or sensor (a gang whose *name* ends in a letter cannot be told from a key's device — a naming
    collision the gateway integration's own slug scheme shares).
    """
    if per_key and not _looks_like_a_key(on_device):
        return None
    primary = re.compile(rf"^{re.escape(slug)}_[0-9a-f]+$")
    rules: dict[str, Callable[[er.RegistryEntry], bool]] = {
        "light": lambda g: g.domain == "light" and bool(primary.match(g.unique_id)),
        "socket": lambda g: g.domain == "switch" and bool(primary.match(g.unique_id)),
        "status_led": lambda g: (
            g.domain == "switch"
            and (per_key or thing.qualifier == "A")
            and g.unique_id.endswith("_switch")
        ),
        "sensor": lambda g: (
            g.domain == "sensor" and g.unique_id.endswith(f"_{thing.qualifier}")
        ),
        "key": lambda g: (
            g.domain == "event" and side is not None and g.translation_key == side
        ),
    }
    side = "up" if per_key else KEY_SIDES.get(thing.qualifier or "")
    return _one(g for g in on_device if rules[thing.kind](g))


def _looks_like_a_key(on_device: Iterable[er.RegistryEntry]) -> bool:
    """Whether a gateway device's entities are those of one key: `up` / `down` events and a status LED, nothing else."""
    entries = list(on_device)
    return bool(entries) and all(
        (g.domain == "event" and g.translation_key in ("up", "down"))
        or (g.domain == "switch" and g.unique_id.endswith("_switch"))
        for g in entries
    )


def is_fresh(entity: er.RegistryEntry) -> bool:
    """Whether nobody customised the entity yet, so replacing its registry entry loses nothing."""
    user_aliases = [
        a for a in entity.aliases if isinstance(a, str)
    ]  # the computed name is always listed
    return (
        entity.name is None
        and entity.icon is None
        and entity.area_id is None
        and not entity.labels
        and not user_aliases
        and entity.hidden_by is None
        and entity.disabled_by in (None, er.RegistryEntryDisabler.INTEGRATION)
    )


# ------------------------------------------------------------------ planning


@callback
def build_import_plan(hass: HomeAssistant, entry: ConfigEntry) -> ImportPlan:
    """Match our entities against the gateway integration's; touches nothing."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    plan = ImportPlan(gateway_entries=hass.config_entries.async_entries(GATEWAY_DOMAIN))
    theirs: list[er.RegistryEntry] = [
        e
        for gw in plan.gateway_entries
        for e in er.async_entries_for_config_entry(ent_reg, gw.entry_id)
        if e.platform == GATEWAY_DOMAIN and e.domain in SUPPORTED_DOMAINS
    ]
    by_slug: dict[str, list[er.RegistryEntry]] = {}
    their_device: dict[str, dr.DeviceEntry] = {}
    scenes: list[er.RegistryEntry] = []
    for g in theirs:
        if g.domain == "scene":
            scenes.append(g)
        device = (
            dev_reg.async_get(g.device_id, include_child_devices=False)
            if g.device_id
            else None
        )
        if device is None:
            continue
        for domain, slug in device.identifiers:
            if domain == GATEWAY_DOMAIN:
                by_slug.setdefault(slug, []).append(g)
                their_device[slug] = device

    mine = [
        (ours, thing)
        for ours in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if (thing := classify(ours)) is not None
    ]
    # the letters of each gang's keys, so a key's ordinal letter (the gateway integration's) can be derived
    keys_of: dict[str, set[str]] = {}
    for ours, thing in mine:
        if thing.kind == "key" and thing.qualifier and ours.device_id:
            keys_of.setdefault(ours.device_id, set()).add(thing.qualifier)

    taken: set[str] = set()
    paired_devices: dict[str, tuple[dr.DeviceEntry, dr.DeviceEntry]] = {}
    for ours, thing in mine:
        device = (
            dev_reg.async_get(ours.device_id, include_child_devices=False)
            if ours.device_id
            else None
        )
        if thing.kind == "scene":
            assert thing.qualifier is not None
            suffix = f"_{scene_slug(thing.qualifier)}_scene"
            found = _one(g for g in scenes if g.unique_id.endswith(suffix))
        elif device is None or not device.name:
            found = None
        else:
            slug = slugify(device.name)
            found = None
            letters = sorted(keys_of.get(device.id, ()))
            ordinal = (
                chr(ord("a") + letters.index(thing.qualifier))
                if thing.qualifier in letters
                else None
            )
            for candidate in device_slugs(slug, thing, ordinal):
                found = counterpart(
                    thing,
                    candidate,
                    by_slug.get(candidate, []),
                    per_key=candidate != slug,
                )
                if found is not None:
                    break
            if found is not None and candidate == slug:
                paired_devices.setdefault(device.id, (device, their_device[slug]))
        if found is None:
            plan.unmatched.append(ours)
            continue
        taken.add(found.entity_id)
        (plan.matches if is_fresh(ours) else plan.customised).append(Match(ours, found))
    plan.gateway_only = [g for g in theirs if g.entity_id not in taken]
    plan.devices = [
        pair
        for pair in paired_devices.values()
        if any(m.ours.device_id == pair[0].id for m in plan.matches)
    ]
    return plan


# ------------------------------------------------------------------ applying


class ImportAborted(Exception):
    """An entry the import has to unload did not unload; nothing was moved. `title` names it."""

    def __init__(self, title: str) -> None:
        """Name the entry that stayed loaded."""
        super().__init__(f"{title} could not be unloaded")
        self.title = title


async def _async_unload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unload `entry` for the import, or raise `ImportAborted` when it does not unload."""
    try:
        unloaded = await hass.config_entries.async_unload(entry.entry_id)
    except HomeAssistantError:  # OperationNotAllowed: a setup in progress, say
        unloaded = False
    if not unloaded:
        raise ImportAborted(entry.title)


async def async_apply_import(hass: HomeAssistant, entry: ConfigEntry) -> ImportPlan:
    """Move every matched gateway entity onto ours; returns the plan that was applied.

    The gateway entries that contribute a matched entity and ours are unloaded first (a loaded entity cannot be
    migrated; ours is set up again at the end), the plan is computed afresh from the registries, then per match our
    fresh registry entry is removed and the gateway's takes its unique id and device. Those gateway entries are
    left disabled: they would otherwise register their entities anew at the next start; a gateway entry for
    another installation (nothing of it matched) is not touched. Running it again moves nothing (the gateway
    entities are ours by then). An entry that does not unload stops the import before the registries are
    touched (`ImportAborted`): every entry unloaded until then is set up again, so nothing is left half-moved.
    """
    unloaded: list[
        ConfigEntry
    ] = []  # set up again when the import stops before moving anything
    was_loaded = entry.state is ConfigEntryState.LOADED
    try:
        try:
            for gw in build_import_plan(hass, entry).contributing_entries:
                if gw.state is ConfigEntryState.LOADED:
                    await _async_unload(hass, gw)
                    unloaded.append(gw)
            if was_loaded:
                await _async_unload(hass, entry)
        except ImportAborted:
            for gw in unloaded:
                hass.config_entries.async_schedule_reload(gw.entry_id)
            raise
        plan = build_import_plan(hass, entry)
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        for match in plan.matches:
            ent_reg.async_remove(match.ours.entity_id)
            ent_reg.async_update_entity_platform(
                match.theirs.entity_id,
                DOMAIN,
                new_config_entry_id=entry.entry_id,
                new_unique_id=match.ours.unique_id,
                new_device_id=match.ours.device_id,
            )
        for ours, theirs in plan.devices:
            dev_reg.async_update_device(
                ours.id,
                area_id=theirs.area_id or ours.area_id,
                name_by_user=ours.name_by_user or theirs.name_by_user,
                labels=ours.labels | theirs.labels,
            )
        for gw in plan.contributing_entries:
            if gw.disabled_by is None:
                await hass.config_entries.async_set_disabled_by(
                    gw.entry_id, ConfigEntryDisabler.USER
                )
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
        return plan
    finally:
        # ours comes back whatever happened; one that failed to unload cannot be set up again from here
        if was_loaded and entry.state is ConfigEntryState.NOT_LOADED:
            hass.config_entries.async_schedule_reload(entry.entry_id)


# ------------------------------------------------------------------ repair issue


def issue_id(entry: ConfigEntry) -> str:
    """Return the id of the issue offering the import for `entry`."""
    return f"{ISSUE_GATEWAY_IMPORT}_{entry.entry_id}"


@callback
def async_update_gateway_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise the issue offering the import while an enabled gateway entry exists next to ours; clear it otherwise."""
    if any(
        gw.disabled_by is None
        for gw in hass.config_entries.async_entries(GATEWAY_DOMAIN)
    ):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GATEWAY_IMPORT,
            learn_more_url=learn_more_url(ISSUE_GATEWAY_IMPORT),
            translation_placeholders={"title": entry.title},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry))


def drop_retired_entities(
    hass: HomeAssistant, retired: Iterable[tuple[str, str]]
) -> None:
    """Remove the registry entries of the entities no longer created: (platform, unique id) pairs.

    `properties.targets.retired_unique_ids` names them; an enabled one would otherwise linger as *no longer provided*.
    """
    registry = er.async_get(hass)
    for platform, unique_id in retired:
        if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
            registry.async_remove(entity_id)


def enable_now_default(
    hass: HomeAssistant, platform: str, unique_ids: Iterable[str]
) -> None:
    """Enable the entities now on by default that an earlier version registered disabled by the integration.

    `enabled_default` only applies when the registry entry is created: *Lock operation* was off by default before
    its bit was confirmed on air (`properties.targets.DEVICE_LOCK_ENABLED`). One the user disabled stays so, and
    nothing registers these disabled by the integration any more, so an entry changes once; HA reloads the config
    entry after it, as when the user enables an entity.
    """
    registry = er.async_get(hass)
    for unique_id in unique_ids:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        entry = registry.async_get(entity_id) if entity_id else None
        if (
            entry is not None
            and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        ):
            registry.async_update_entity(entry.entity_id, disabled_by=None)
