"""Translation-file consistency checks.

`strings.json` is the source Home Assistant core's tooling reads; `translations/en.json` is what a running Home
Assistant actually loads for a custom integration (nothing generates it from `strings.json` here). The two must
stay in lockstep: a key added to one and not the other silently falls back to the raw translation key in the UI.
hassfest checks each file's schema but not that they agree, and it cannot know which keys the code uses, so the
checks below are the only automated guard:

- same leaf keys, and equal values once `[%key:...%]` references in `strings.json` are resolved,
- the same `{placeholder}` set per key,
- every `icons.json` entry points at an entity translation key or an action,
- every translation key the code uses (literal or a `CONSTANT` reference, per-property config entity, service
  error, repair issue, event type, device trigger, select option) exists in `strings.json`,
- `services.yaml` and the `services` section agree (names, fields), its select options match the enums the
  service handlers accept, and the config / options flow only shows steps, errors, aborts and data keys that have
  a translation.

Only "used but missing" is checked, never "translated but unused": a key may be added to `strings.json` ahead of
the code that raises or shows it.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
import yaml
from homeassistant.helpers import config_validation as cv

from custom_components.junghome_ble import (
    config_entities,
    config_flow,
    const,
    light,
    mesh_config,
    schedules,
    services,
)
from custom_components.junghome_ble.const import (
    DOMAIN,
    ISSUE_IV_INDEX_AHEAD,
    ISSUE_IV_INDEX_MISMATCH,
    ISSUE_KEY_REFRESH,
    ISSUE_PDUS_DROPPED,
    ISSUE_SEQ_STORE_LOST,
)
from custom_components.junghome_ble.device_trigger import (
    TRIGGER_SUBTYPES,
    TRIGGER_TYPES,
)
from custom_components.junghome_ble.event import EVENT_TYPES
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh import vendor_models as V
from custom_components.junghome_ble.jhmesh.properties import BLIND_MODE
from custom_components.junghome_ble.select import UNKNOWN_OPTION

COMPONENT = Path(__file__).parent.parent / "custom_components" / DOMAIN
STRINGS = COMPONENT / "strings.json"
EN = COMPONENT / "translations" / "en.json"
ICONS = COMPONENT / "icons.json"
SERVICES_YAML = COMPONENT / "services.yaml"
CONFIG_FLOW = COMPONENT / "config_flow.py"
SOURCES = sorted(COMPONENT.glob("*.py"))

REFERENCE = re.compile(r"^\[%key:(?P<path>[^%]+)%\]$")
OWN_REFERENCE_PREFIX = f"component::{DOMAIN}::"
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# `translation_key="x"` / `_attr_translation_key = "x"` literals, and the service error helpers' first argument
LITERAL_KEY = re.compile(r'translation_key\s*=\s*"(?P<key>[a-z0-9_]+)"')
# `translation_key=ISSUE_X` / `translation_key=ex.SOME_KEY`: a name, not a string (f-strings excluded by the
# lookahead — `f"..."` starts with a letter too). Lower-case names are runtime values and cannot be checked here.
CONSTANT_KEY = re.compile(r"translation_key\s*=\s*(?P<ref>[A-Za-z_][\w.]*)(?![\w\"'])")
SERVICE_ERROR = re.compile(r'\b_(?:validation|failure)\(\s*"(?P<key>[a-z0-9_]+)"')
# config flow: `errors["base"] = "x"` / `errors[CONF_X] = "x"` / `{"base": "x"}`, `reason="x"`, `step_id="x"` or
# `step_id=CONSTANT`, `menu_options=[...]` literal lists, `vol.Required(CONF_X` / `vol.Optional(CONF_X`
FLOW_ERROR = re.compile(r'(?:errors\[[^\]]+\]\s*=|"base"\s*:)\s*"(?P<key>[a-z_]+)"')
FLOW_RETURN = re.compile(r'return\s+"(?P<key>[a-z_]+)"')
FLOW_ABORT = re.compile(r'reason\s*=\s*"(?P<key>[a-z_]+)"')
FLOW_STEP = re.compile(r'(?<![\w])step_id\s*=\s*(?P<ref>"[a-z_]+"|[A-Z_]+)')
FLOW_MENU = re.compile(r"menu_options\s*=\s*\[(?P<items>[^\]]*)\]")
FLOW_FIELD = re.compile(r"vol\.(?:Required|Optional)\(\s*(?P<ref>[A-Z_]+)")


def _load(path: Path) -> dict[str, Any]:
    """Parse a translation file, rejecting duplicate keys.

    `json.load` keeps the last of a duplicated key, so a botched merge that writes the same block twice parses
    cleanly and compares equal. Reject it here instead.
    """

    def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"duplicate key {key!r} in {path.name}")
            seen.add(key)
        return dict(pairs)

    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates
    )


def _leaves(obj: Any, prefix: str = "") -> dict[str, str]:
    """Flatten a nested translation dict to {dotted.key: value}."""
    if not isinstance(obj, dict):
        return {prefix: obj}
    out: dict[str, str] = {}
    for key, value in obj.items():
        out.update(_leaves(value, f"{prefix}.{key}" if prefix else key))
    return out


def _resolve(value: str, leaves: dict[str, str], origin: str) -> str:
    """Follow `[%key:component::junghome_ble::a::b%]` references to their value.

    Only references into this integration can be resolved (and are the only ones that make sense in a custom
    integration: `en.json` must carry the literal text, there is no core string table to fall back to).
    """
    seen: list[str] = []
    while (match := REFERENCE.match(value)) is not None:
        path = match.group("path")
        assert path.startswith(OWN_REFERENCE_PREFIX), (
            f"{origin}: reference {value!r} points outside {DOMAIN}"
        )
        dotted = path.removeprefix(OWN_REFERENCE_PREFIX).replace("::", ".")
        assert dotted in leaves, f"{origin}: reference {value!r} does not resolve"
        assert dotted not in seen, f"{origin}: circular reference through {dotted}"
        seen.append(dotted)
        value = leaves[dotted]
    return value


@pytest.fixture(scope="module")
def strings() -> dict[str, Any]:
    return _load(STRINGS)


@pytest.fixture(scope="module")
def strings_leaves(strings: dict[str, Any]) -> dict[str, str]:
    """`strings.json` flattened, with every reference resolved."""
    raw = _leaves(strings)
    return {
        key: _resolve(value, raw, f"strings.json {key}") for key, value in raw.items()
    }


@pytest.fixture(scope="module")
def en_leaves() -> dict[str, str]:
    return _leaves(_load(EN))


# ----------------------------------------------------------------------------- strings.json <-> en.json


def test_en_has_exactly_the_keys_of_strings(
    strings_leaves: dict[str, str], en_leaves: dict[str, str]
) -> None:
    missing = sorted(set(strings_leaves) - set(en_leaves))
    extra = sorted(set(en_leaves) - set(strings_leaves))
    assert not missing, f"en.json is missing {len(missing)} keys: {missing}"
    assert not extra, f"en.json has {len(extra)} keys not in strings.json: {extra}"


def test_en_values_equal_resolved_strings(
    strings_leaves: dict[str, str], en_leaves: dict[str, str]
) -> None:
    """Same text in both, references resolved: `en.json` is what users see, `strings.json` what tooling reads."""
    different = {
        key: (value, en_leaves[key])
        for key, value in strings_leaves.items()
        if key in en_leaves and en_leaves[key] != value
    }
    assert not different, f"{len(different)} values differ: {different}"


def test_en_has_no_references(en_leaves: dict[str, str]) -> None:
    """Nothing resolves `[%key:...%]` at runtime for a custom integration; the UI would show it verbatim."""
    unresolved = sorted(
        key for key, value in en_leaves.items() if REFERENCE.match(value)
    )
    assert not unresolved, f"en.json has unresolved references: {unresolved}"


def test_placeholders_preserved(
    strings_leaves: dict[str, str], en_leaves: dict[str, str]
) -> None:
    """A renamed / dropped {placeholder} raises KeyError when Home Assistant formats the string."""
    mismatched = {
        key: (
            sorted(PLACEHOLDER.findall(value)),
            sorted(PLACEHOLDER.findall(en_leaves[key])),
        )
        for key, value in strings_leaves.items()
        if key in en_leaves
        and set(PLACEHOLDER.findall(value)) != set(PLACEHOLDER.findall(en_leaves[key]))
    }
    assert not mismatched, f"placeholder mismatch: {mismatched}"


@pytest.mark.parametrize("path", [STRINGS, EN], ids=lambda p: p.name)
def test_no_angle_brackets(path: Path) -> None:
    """`<` / `>` in translation text breaks Home Assistant's translation parser."""
    offending = sorted(
        key
        for key, value in _leaves(_load(path)).items()
        if "<" in value or ">" in value
    )
    assert not offending, f"{path.name}: angle brackets in {offending}"


# ----------------------------------------------------------------------------- icons.json


def test_icons_resolve_to_entity_translation_keys(strings: dict[str, Any]) -> None:
    """Every `entity.<platform>.<key>` icon belongs to an entity translation key (an orphan icon never shows),
    every `services.<name>` icon to an action."""
    icons = _load(ICONS)
    assert set(icons) == {"entity", "services"}, (
        f"icons.json sections not covered here: {sorted(icons)}"
    )
    assert set(icons["services"]) <= set(strings["services"]), "icons of no action"
    orphans = sorted(
        f"{platform}.{key}"
        for platform, keys in icons["entity"].items()
        for key in keys
        if key not in strings["entity"].get(platform, {})
    )
    assert not orphans, f"icons.json entries without a translation key: {orphans}"


# ----------------------------------------------------------------------------- keys the code uses


def _entity_keys(strings: dict[str, Any]) -> set[str]:
    return {key for keys in strings["entity"].values() for key in keys}


def test_literal_translation_keys_exist(strings: dict[str, Any]) -> None:
    """Every `translation_key="..."` literal in the integration names a key of some section."""
    known = _entity_keys(strings) | set(strings["exceptions"]) | set(strings["issues"])
    unknown = sorted(
        f"{path.name}:{number} {match.group('key')}"
        for path in SOURCES
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for match in LITERAL_KEY.finditer(line)
        if match.group("key") not in known
    )
    assert not unknown, (
        f"translation keys used in the code but not in strings.json: {unknown}"
    )


def test_service_error_keys_exist(strings: dict[str, Any]) -> None:
    """`_validation("...")` / `_failure("...")` raise with a key of the `exceptions` section."""
    found = {
        match.group("key")
        for path in SOURCES
        for match in SERVICE_ERROR.finditer(path.read_text(encoding="utf-8"))
    }
    assert found, "no service error helper calls found (regex out of date?)"
    unknown = sorted(found - set(strings["exceptions"]))
    assert not unknown, f"service errors without an exception string: {unknown}"


def test_issue_keys_exist(strings: dict[str, Any]) -> None:
    """Every `ISSUE_*` constant of `const.py` names a repair-issue translation."""
    issues = {
        name: value for name, value in vars(const).items() if name.startswith("ISSUE_")
    }
    assert {ISSUE_KEY_REFRESH, ISSUE_PDUS_DROPPED} <= set(issues.values())
    unknown = sorted(
        f"{name}={value!r}"
        for name, value in issues.items()
        if value not in strings["issues"]
    )
    assert not unknown, f"ISSUE_* constants without an issue translation: {unknown}"


def _resolve_constant(module_name: str, ref: str) -> Any:
    """Return the value a dotted `CONSTANT` / `obj.CONSTANT` reference has in the integration module `module_name`.

    The first segment is looked up in the module's namespace (constants are imported there), the rest is walked
    with `getattr`; the module itself is the fallback for a bare name it does not import (`const` is tried last).
    """
    module = importlib.import_module(f"custom_components.{DOMAIN}.{module_name}")
    first, *rest = ref.split(".")
    value = getattr(module, first, None)
    if value is None:
        value = getattr(const, first, None)
    for attribute in rest:
        value = getattr(value, attribute, None) if value is not None else None
    return value


def test_constant_translation_keys_exist(strings: dict[str, Any]) -> None:
    """Every `translation_key=CONSTANT` (or `obj.CONSTANT`) resolves to a key of some section.

    A constant that cannot be resolved (a typo, a name that is not a module-level string) fails too, so no
    reference slips through unchecked; lower-case names are runtime values and are skipped.
    """
    known = _entity_keys(strings) | set(strings["exceptions"]) | set(strings["issues"])
    problems: list[str] = []
    checked = 0
    for path in SOURCES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in CONSTANT_KEY.finditer(line):
                ref = match.group("ref")
                if not ref.rsplit(".", 1)[-1].isupper():
                    continue  # `key`, `spec.name`, `target.translation_key`: only known at runtime
                checked += 1
                value = _resolve_constant(path.stem, ref)
                where = f"{path.name}:{number} {ref}"
                if not isinstance(value, str):
                    problems.append(f"{where} does not resolve to a string constant")
                elif value not in known:
                    problems.append(f"{where}={value!r} is not in strings.json")
    assert checked, "no constant translation keys found (regex out of date?)"
    assert not problems, f"constant translation keys: {problems}"


# ----------------------------------------------------------------------------- services.yaml


@pytest.fixture(scope="module")
def services_yaml() -> dict[str, Any]:
    return yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))


def _schema_keys(schema: vol.Schema | vol.All | dict[Any, Any]) -> set[str]:
    """The field names a service schema accepts (the `vol.Schema` inside a `vol.All` of extra validators).

    An entity action's schema is the plain dict of its fields (`async_register_platform_entity_service` adds the
    target fields itself).
    """
    if isinstance(schema, dict):
        return {str(marker) for marker in schema}
    inner = schema.validators[0] if isinstance(schema, vol.All) else schema
    assert isinstance(inner, vol.Schema)
    return {str(marker) for marker in inner.schema}


def test_services_yaml_matches_strings_and_code(
    services_yaml: dict[str, Any], strings: dict[str, Any]
) -> None:
    """`services.yaml`, the `services` translations and the registered handlers name the same actions and fields.

    A field in the YAML without a translation shows its raw name; a field the schema accepts but the YAML lacks is
    invisible in the UI; a YAML field the schema rejects fails every call that uses it.
    """
    registered = {
        value
        for name, value in vars(services).items()
        if name.startswith("SERVICE_") and isinstance(value, str)
    }
    assert set(services_yaml) == registered, "services.yaml vs the SERVICE_* constants"
    assert set(services_yaml) == set(strings["services"]), (
        "services.yaml vs strings.services"
    )
    for name, service in services_yaml.items():
        fields = set(service.get("fields", {}))
        translated = set(strings["services"][name].get("fields", {}))
        assert fields == translated, f"{name}: fields in services.yaml vs strings.json"
        schema_keys = _schema_keys(getattr(services, f"{name.upper()}_SCHEMA"))
        assert fields <= schema_keys, f"{name}: YAML fields the schema does not accept"
        assert schema_keys - set(cv.ENTITY_SERVICE_FIELDS) <= fields, (
            f"{name}: schema fields missing from services.yaml"
        )


def _select_selectors(obj: Any, origin: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every `select:` selector in a services.yaml tree, with the field it belongs to."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "selector" and isinstance(value, dict) and "select" in value:
                found.append((origin, value["select"]))
            else:
                found.extend(
                    _select_selectors(value, f"{origin}.{key}" if origin else key)
                )
    return found


def test_service_select_options_match_the_code(
    services_yaml: dict[str, Any], strings: dict[str, Any]
) -> None:
    """The options a select field offers are the ones the handler accepts, and translated when they need to be."""
    selectors = _select_selectors(services_yaml)
    assert selectors, "no select selectors found in services.yaml"
    expected = {
        "key": set(services.KEY_LETTERS),
        "mode": set(mesh_config.MODES),
        "trigger": set(schedules.TRIGGERS),
        "weekdays": set(V.DAYS),
        "action": set(services.SCHEDULE_ACTIONS),
        "threshold": set(services.THRESHOLD_PROPERTIES),
        "flavour": set(services.EXPORT_FLAVOURS),
        "direction": set(light.DIM_DIRECTIONS),
    }
    for origin, select in selectors:
        field = origin.rsplit(".", 1)[-1]
        options = set(select["options"])
        assert field in expected, f"{origin}: no enum known for this select"
        assert options == expected[field], f"{origin}: options vs the code's enum"
        if (key := select.get("translation_key")) is not None:
            translated = set(strings["selector"][key]["options"])
            assert options == translated, f"{origin}: options vs strings.selector.{key}"
    assert "key_mode" in strings["selector"]
    assert set(strings["selector"]["key_mode"]["options"]) == set(mesh_config.MODES)


# ----------------------------------------------------------------------------- config / options flow


def _flow_constant(ref: str) -> str:
    """A `"literal"` or a module-level `CONSTANT` of config_flow.py, as its string value."""
    if ref.startswith('"'):
        return ref.strip('"')
    value = getattr(config_flow, ref, None)
    assert isinstance(value, str), f"config_flow.{ref} is not a string constant"
    return value


def test_config_flow_keys_exist(strings: dict[str, Any]) -> None:
    """Every error, abort reason, step, menu option and form field the flows show has a translation.

    `config` and `options` are checked together: the options flow's `init` step is the only one of its kind and
    every error / abort key the code uses is a config-flow key.
    """
    source = CONFIG_FLOW.read_text(encoding="utf-8")
    config, options = strings["config"], strings["options"]
    steps = {**options["step"], **config["step"]}

    errors = {m.group("key") for m in FLOW_ERROR.finditer(source)}
    errors |= {
        m.group("key")
        for m in FLOW_RETURN.finditer(inspect.getsource(config_flow._gateway_error_key))
    }
    assert errors, "no config-flow error keys found (regex out of date?)"
    assert errors <= set(config["error"]) | set(options.get("error", {})), (
        f"config-flow errors without a translation: {sorted(errors - set(config['error']))}"
    )

    aborts = {m.group("key") for m in FLOW_ABORT.finditer(source)}
    assert aborts, "no abort reasons found (regex out of date?)"
    assert aborts <= set(config["abort"]) | set(options.get("abort", {})), (
        f"abort reasons without a translation: {sorted(aborts - set(config['abort']))}"
    )

    shown = {_flow_constant(m.group("ref")) for m in FLOW_STEP.finditer(source)}
    assert shown, "no step ids found (regex out of date?)"
    assert shown <= set(steps), (
        f"steps without a translation: {sorted(shown - set(steps))}"
    )

    menus = {
        _flow_constant(item.strip())
        for m in FLOW_MENU.finditer(source)
        for item in m.group("items").split(",")
        if item.strip()
    }
    assert menus, "no literal menu_options found (regex out of date?)"
    menu_translations = {
        option for step in steps.values() for option in step.get("menu_options", {})
    }
    assert menus <= menu_translations, (
        f"menu options without a translation: {sorted(menus - menu_translations)}"
    )

    fields = {_flow_constant(m.group("ref")) for m in FLOW_FIELD.finditer(source)}
    assert fields, "no form fields found (regex out of date?)"
    data_translations = {key for step in steps.values() for key in step.get("data", {})}
    assert fields <= data_translations, (
        f"form fields without a data translation: {sorted(fields - data_translations)}"
    )

    option_keys = {str(marker) for marker in config_flow._options_schema({}).schema}
    assert option_keys == set(options["step"]["init"]["data"]), (
        "options form fields vs strings.options.step.init.data"
    )


def test_config_entity_keys_exist(strings: dict[str, Any]) -> None:
    """Each property entity's key exists under its platform, plus the `_key` variant of those a key letter can name.

    A property addressed to a key or to a key's LED is named after the key when the device groups several
    (`EntityTarget.translation_key`), so both spellings have to exist.
    """
    missing = sorted(
        f"{description.platform}.{key}"
        for description in config_entities.descriptions()
        for key in (
            description.translation_key,
            *(
                [f"{description.translation_key}_key"]
                if description.spec.element in ("key", "led")
                else []
            ),
        )
        if key not in strings["entity"].get(description.platform, {})
    )
    assert not missing, f"config entities without a translation: {missing}"
    assert "led_night_mode" in strings["entity"]["switch"]


def test_select_options_are_translated(strings: dict[str, Any]) -> None:
    """Every option a select can offer has a state translation (otherwise the UI shows the raw option)."""
    untranslated: list[str] = []
    for description in config_entities.descriptions():
        if description.platform != "select":
            continue
        codec = description.spec.codec
        options = (
            set(P.LED_COLOURS)
            if isinstance(codec, P.RgbMode)
            else {o for o in codec.options if o != UNKNOWN_OPTION}
        )
        keys = [description.translation_key]
        if description.spec.element in ("key", "led"):
            keys.append(f"{description.translation_key}_key")
        for key in keys:
            states = strings["entity"]["select"][key].get("state", {})
            untranslated += sorted(f"{key}.{o}" for o in options - set(states))
    assert not untranslated, (
        f"select options without a state translation: {untranslated}"
    )


def test_event_types_and_triggers_are_translated(strings: dict[str, Any]) -> None:
    states = strings["entity"]["event"]["button"]["state_attributes"]["event_type"][
        "state"
    ]
    assert set(EVENT_TYPES) == set(states)
    assert set(TRIGGER_TYPES) == set(strings["device_automation"]["trigger_type"])
    assert set(TRIGGER_SUBTYPES) == set(strings["device_automation"]["trigger_subtype"])


def test_cover_operation_modes_are_translated(strings: dict[str, Any]) -> None:
    """The cover's `operation_mode` attribute is a closed enum: every mode the table names, and `unknown`."""
    states = strings["entity"]["cover"]["blind"]["state_attributes"]["operation_mode"][
        "state"
    ]
    assert set(states) == {*BLIND_MODE.values(), "unknown"}


# ----------------------------------------------------------------------------- sequence-number store advice

# "restore the store", "restore the sequence-number store ..." as an instruction; "do not restore ..." is the warning
RESTORE_ADVICE = re.compile(
    r"(?<!not )(?<!never )\brestore (?:the |a |its )?(?:sequence-number )?store\b",
    re.IGNORECASE,
)
DOCS = Path(__file__).parent.parent / "docs"


@pytest.mark.parametrize(
    "issue", [ISSUE_SEQ_STORE_LOST, ISSUE_PDUS_DROPPED, ISSUE_IV_INDEX_AHEAD]
)
def test_seq_repairs_never_advise_restoring_the_store(
    strings_leaves: dict[str, str], en_leaves: dict[str, str], issue: str
) -> None:
    """A restored older seq record resends numbers the nodes saw (dropped as replays, AES-CCM nonces reused).

    The only safe way back from a lost record is the repair's skip ahead (or a fresh address), and both repairs
    say so outright instead of offering a backup restore as an alternative.
    """
    key = f"issues.{issue}.fix_flow.step.confirm.description"
    for leaves in (strings_leaves, en_leaves):
        text = leaves[key]
        assert not RESTORE_ADVICE.search(text), text
        assert "not restore" in text, text


def test_the_iv_index_mismatch_advises_a_new_address_not_store_surgery(
    strings_leaves: dict[str, str], en_leaves: dict[str, str]
) -> None:
    """Review-4 H4-3: the text told the user to remove the address's record from the store — the setup then went on
    from the `.backup` copy at the same index (or refused: `seq_store_lost`), and the next start overwrites a hand
    edit anyway. The way that works without the repair is a new unicast address."""
    for leaves in (strings_leaves, en_leaves):
        text = leaves[f"issues.{ISSUE_IV_INDEX_MISMATCH}.description"]
        assert not RESTORE_ADVICE.search(text), text
        assert "not restore" in text, text
        assert "new unicast address" in text, text
        assert "record from the store" not in text, text


def test_docs_never_advise_restoring_the_seq_store() -> None:
    offending = [
        f"{path.name}:{number}"
        for path in sorted(DOCS.glob("*.md"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if RESTORE_ADVICE.search(line)
    ]
    assert not offending
