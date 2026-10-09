"""Taking over the JUNG HOME Gateway integration's entities: planning, applying, idempotency, the repair issue.

The gateway integration is not installed here; its config entry, devices and entities are created straight in the
registries with its identity scheme (`("junghome", slugify(label))` devices, `{slug}_{suffix}[_{qualifier}]` unique
ids, `{scope}_{slug}_scene` scenes, `up` / `down` event translation keys), *before* ours registers, as in a real
migration: the gateway owns the plain entity ids, ours got the `_2` ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import (
    ConfigEntryDisabler,
    ConfigEntryState,
    OperationNotAllowed,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome_ble.const import DOMAIN, GATEWAY_DOMAIN
from custom_components.junghome_ble.migration import (
    ImportAborted,
    Thing,
    async_apply_import,
    async_update_gateway_issue,
    build_import_plan,
    classify,
    device_slugs,
    issue_id,
)

from .conftest import FakeProxyLink, settle, setup_entry, wait_for_link
from .helpers import (
    MESH_UUID,
    NODE_ACTUATOR,
    NODE_LIGHT_CTL,
    NODE_LIGHT_DIMMER,
    NODE_LIGHT_SWITCH,
    UID_BUTTON_DIMMER,
    UID_BUTTON_WC,
    UID_LIGHT_CTL,
    UID_LIGHT_DIMMER,
    UID_LIGHT_OUT2,
    UID_LIGHT_SWITCH,
    UID_ROCKER_A,
    UID_ROCKER_B,
    UID_SOCKET,
    entity_id,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

UID_SCENE_WC_OFF = f"{MESH_UUID}-scene-1"
UID_SCENE_ALL_OFF = f"{MESH_UUID}-scene-2"
UID_STATUS_LED_WC = f"{UID_BUTTON_WC}-key_status_led"
UID_STATUS_LED_ROCKER_B = f"{UID_ROCKER_B}-key_status_led"


class Gateway:
    """A synthetic gateway integration: its entry plus helpers to fill the registries in its scheme."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.entry = MockConfigEntry(
            domain=GATEWAY_DOMAIN,
            title="JUNG HOME Gateway",
            unique_id="serial-1",
            data={"identity_anchor": "gw1"},
        )
        self.entry.add_to_hass(hass)
        hass.config.components.add(
            GATEWAY_DOMAIN
        )  # "loaded" as far as reloads are concerned
        self.devices: dict[str, dr.DeviceEntry] = {}

    def device(self, slug: str, name: str, **customisation: Any) -> dr.DeviceEntry:
        dev = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(GATEWAY_DOMAIN, slug)},
            name=name,
            manufacturer="Jung",
            model="OnOff",
        )
        if customisation:
            dev = dr.async_get(self.hass).async_update_device(dev.id, **customisation)
        self.devices[slug] = dev
        return dev

    def entity(
        self,
        domain: str,
        unique_id: str,
        object_id: str,
        *,
        device: dr.DeviceEntry | None = None,
        translation_key: str | None = None,
        original_name: str | None = None,
        has_entity_name: bool = True,
        disabled: bool = False,
        **customisation: Any,
    ) -> er.RegistryEntry:
        reg = er.async_get(self.hass)
        entry = reg.async_get_or_create(
            domain,
            GATEWAY_DOMAIN,
            unique_id,
            config_entry=self.entry,
            device_id=device.id if device else None,
            has_entity_name=has_entity_name,
            original_name=original_name,
            translation_key=translation_key,
            suggested_object_id=object_id,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION if disabled else None,
        )
        if customisation:
            entry = reg.async_update_entity(entry.entity_id, **customisation)
        assert entry.entity_id == f"{domain}.{object_id}"
        return entry


@pytest.fixture
def gateway(hass: HomeAssistant) -> Gateway:
    """The gateway integration's registries for the fixture network, with a few user customisations."""
    gw = Gateway(hass)
    bathroom = ar.async_get(hass).async_get_or_create("Bathroom").id
    mirror = gw.device(
        "wc_mirror",
        "WC mirror",
        area_id=bathroom,
        name_by_user="Mirror light",
        labels={"lights"},
    )
    gw.entity(
        "light",
        "wc_mirror_001",
        "wc_mirror",
        device=mirror,
        name="Mirror",
        icon="mdi:mirror",
    )
    button = gw.device("wc_mirror_button", "WC mirror button")
    gw.entity(
        "event",
        "wc_mirror_button_00c_event",
        "wc_mirror_button_up",
        device=button,
        translation_key="up",
        original_name="Up",
    )
    gw.entity(
        "event",
        "wc_mirror_button_00d_event",
        "wc_mirror_button_down",
        device=button,
        translation_key="down",
        original_name="Down",
    )
    gw.entity(
        "switch",
        "wc_mirror_button_00e_switch",
        "wc_mirror_button_status_led",
        device=button,
        translation_key="status_led",
        original_name="Status LED",
    )
    rocker = gw.device("living_room_rocker", "Living room rocker")
    gw.entity(
        "event",
        "living_room_rocker_00c_event",
        "living_room_rocker_up",
        device=rocker,
        translation_key="up",
        original_name="Up",
    )
    gw.entity(
        "event",
        "living_room_rocker_00d_event",
        "living_room_rocker_down",
        device=rocker,
        translation_key="down",
        original_name="Down",
    )
    boiler = gw.device("boiler", "Boiler")
    gw.entity("switch", "boiler_001", "boiler", device=boiler)
    for word in ("power", "voltage", "current", "frequency"):
        gw.entity(
            "sensor",
            f"boiler_010_{word}",
            f"boiler_{word}",
            device=boiler,
            translation_key=word,
            original_name=word.capitalize(),
            disabled=word in ("voltage", "current", "frequency"),
        )
    gw.entity(
        "sensor",
        "boiler_099_energy",
        "boiler_energy",
        device=boiler,
        translation_key="energy",
        original_name="Energy",
    )  # the gateway's energy sensor: no counterpart here (JUNG sockets report no total-energy counter)
    # two lights with the same label slug: the gateway integration cannot tell them apart, neither can we
    kitchen = gw.device("kitchen_ceiling", "Kitchen ceiling")
    gw.entity("light", "kitchen_ceiling_001", "kitchen_ceiling", device=kitchen)
    gw.entity("light", "kitchen_ceiling_002", "kitchen_ceiling_2", device=kitchen)
    gw.entity(
        "scene",
        "gw1_wc_off_scene",
        "wc_off",
        original_name="WC off",
        has_entity_name=False,
    )
    gw.entity(
        "scene",
        "gw1_movie_night_scene",
        "movie_night",
        original_name="Movie night",
        has_entity_name=False,
    )
    hub = gw.device("gateway_gw1", "JUNG HOME Gateway")
    gw.entity(
        "binary_sensor",
        "gateway_gw1_connectivity",
        "jung_home_gateway_connection",
        device=hub,
        original_name="Connection",
    )
    return gw


@pytest.fixture
async def ours(
    hass: HomeAssistant,
    gateway: Gateway,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    fast_sleep: list[float],
) -> MockConfigEntry:
    """Our integration, set up after the gateway's registrations exist."""
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    return mock_config_entry


def _moves(plan: Any) -> dict[str, str]:
    return {m.theirs.entity_id: m.ours.unique_id for m in plan.matches}


EXPECTED_MOVES = {
    "light.wc_mirror": UID_LIGHT_SWITCH,
    "event.wc_mirror_button_up": UID_BUTTON_WC,
    "switch.wc_mirror_button_status_led": UID_STATUS_LED_WC,
    "event.living_room_rocker_up": UID_ROCKER_A,
    "event.living_room_rocker_down": UID_ROCKER_B,
    "switch.boiler": UID_SOCKET,
    "sensor.boiler_power": f"{UID_SOCKET}-power",
    "sensor.boiler_voltage": f"{UID_SOCKET}-voltage",
    "sensor.boiler_current": f"{UID_SOCKET}-current",
    "scene.wc_off": UID_SCENE_WC_OFF,
}


# --------------------------------------------------------------------------- planning


async def test_plan_matches_by_device_name_domain_and_qualifier(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    plan = build_import_plan(hass, ours)
    assert plan.gateway_entries == [gateway.entry]
    assert _moves(plan) == EXPECTED_MOVES
    assert plan.customised == []
    unmatched = {e.unique_id for e in plan.unmatched}
    assert (
        unmatched
        == {
            UID_LIGHT_CTL,  # "Living room DALI": no gateway device
            UID_LIGHT_DIMMER,  # "WC ceiling"
            UID_LIGHT_OUT2,
            f"{NODE_ACTUATOR}-0001",  # "Kitchen ceiling": two gateway lights, ambiguous
            UID_SCENE_ALL_OFF,
            f"{NODE_LIGHT_DIMMER}-0040",  # WC ceiling button + its status LED
            f"{NODE_LIGHT_DIMMER}-0040-key_status_led",
            f"{UID_ROCKER_A}-key_status_led",  # the gateway device has no status LED switch here
            UID_STATUS_LED_ROCKER_B,  # only key A could get the gateway's one status LED anyway
        }
    )
    assert {g.entity_id for g in plan.gateway_only} == {
        "event.wc_mirror_button_down",  # a single key has no key B
        "sensor.boiler_frequency",
        "sensor.boiler_energy",  # we have no energy sensor to take it over with
        "scene.movie_night",
        "light.kitchen_ceiling",
        "light.kitchen_ceiling_2",
    }  # the hub's binary_sensor is not a domain we take over, so it is not even listed
    assert {(o.name, t.name) for o, t in plan.devices} == {
        ("WC mirror", "WC mirror"),
        ("WC mirror button", "WC mirror button"),
        ("Living room rocker", "Living room rocker"),
        ("Boiler", "Boiler"),
    }
    # a dry run: nothing moved
    reg = er.async_get(hass)
    assert reg.async_get("light.wc_mirror").platform == GATEWAY_DOMAIN
    assert (
        reg.async_get_entity_id("light", DOMAIN, UID_LIGHT_SWITCH) != "light.wc_mirror"
    )
    assert not plan.empty


async def test_plan_matches_keys_on_the_gateways_per_key_devices(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    """The gateway integration registers one device per key, `<gang> <letter>` (slug `<gang>_<letter>`), with `up` /
    `down` events and a status LED (the live registry): every key's event takes its `up`, every
    key's status LED its switch; `down` stays gateway-only; a per-key device without an event falls back to the
    gang device's rule; device customisations are copied from gang-level devices only.
    """
    ceiling_a = gateway.device("wc_ceiling_button_a", "WC ceiling button A")
    gateway.entity(
        "event",
        "wc_ceiling_button_a_00c_event",
        "wc_ceiling_button_a_up",
        device=ceiling_a,
        translation_key="up",
        original_name="Up",
    )
    gateway.entity(
        "event",
        "wc_ceiling_button_a_00d_event",
        "wc_ceiling_button_a_down",
        device=ceiling_a,
        translation_key="down",
        original_name="Down",
    )
    gateway.entity(
        "switch",
        "wc_ceiling_button_a_00e_switch",
        "wc_ceiling_button_a_status_led",
        device=ceiling_a,
        translation_key="status_led",
        original_name="Status LED",
    )
    # the rocker's key B has its own gateway device with a status LED only: the event keeps the gang rule (down)
    rocker_b = gateway.device("living_room_rocker_b", "Living room rocker B")
    gateway.entity(
        "switch",
        "living_room_rocker_b_00e_switch",
        "living_room_rocker_b_status_led",
        device=rocker_b,
        translation_key="status_led",
        original_name="Status LED",
    )
    plan = build_import_plan(hass, ours)
    moves = _moves(plan)
    assert moves == {
        **EXPECTED_MOVES,
        "event.wc_ceiling_button_a_up": UID_BUTTON_DIMMER,
        "switch.wc_ceiling_button_a_status_led": f"{UID_BUTTON_DIMMER}-key_status_led",
        "switch.living_room_rocker_b_status_led": UID_STATUS_LED_ROCKER_B,
    }
    assert "event.wc_ceiling_button_a_down" in {g.entity_id for g in plan.gateway_only}
    assert f"{UID_ROCKER_A}-key_status_led" in {e.unique_id for e in plan.unmatched}
    assert (
        {(o.name, t.name) for o, t in plan.devices}
        == {
            ("WC mirror", "WC mirror"),
            ("WC mirror button", "WC mirror button"),
            ("Living room rocker", "Living room rocker"),
            ("Boiler", "Boiler"),
        }
    )  # not ("WC ceiling button", "WC ceiling button A"): per-key devices carry no gang customisation


async def test_keys_c_and_d_never_take_an_untranslated_event_and_per_key_slugs_must_look_like_keys(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    """A gang-named gateway device gives `up` to key A and `down` to key B; a key C has no side there, so an event
    without a translation key (another gateway version) is not taken by it. A device behind a per-key slug that
    holds a light is not a key's device: the key falls back to the gang rule."""
    reg = er.async_get(hass)
    rocker_a = reg.async_get(entity_id(hass, "event", UID_ROCKER_A))
    assert rocker_a is not None
    key_c = reg.async_get_or_create(
        "event",
        DOMAIN,
        f"{NODE_LIGHT_CTL}-0042",
        config_entry=ours,
        device_id=rocker_a.device_id,
    )
    assert classify(key_c) == Thing("key", "C")
    rocker = gateway.devices["living_room_rocker"]
    gateway.entity(
        "event",
        "living_room_rocker_00e_event",
        "living_room_rocker_something",
        device=rocker,
        original_name="Something",
    )  # no translation key at all
    # `living_room_rocker_a`: our key A's per-key slug, but the device behind it is a lamp with an event
    lamp = gateway.device("living_room_rocker_a", "Living room rocker A")
    gateway.entity(
        "light", "living_room_rocker_a_001", "living_room_rocker_a", device=lamp
    )
    gateway.entity(
        "event",
        "living_room_rocker_a_00c_event",
        "living_room_rocker_a_up",
        device=lamp,
        translation_key="up",
        original_name="Up",
    )
    plan = build_import_plan(hass, ours)
    moves = _moves(plan)
    assert moves == EXPECTED_MOVES  # key A still takes the gang's `up`, key C nothing
    assert key_c in plan.unmatched
    gateway_only = {g.entity_id for g in plan.gateway_only}
    assert "event.living_room_rocker_something" in gateway_only
    assert "event.living_room_rocker_a_up" in gateway_only
    assert "light.living_room_rocker_a" in gateway_only


def test_device_slugs_try_the_positional_then_the_ordinal_letter() -> None:
    """A 2-gang's right key is our C but the gateway integration's B: both per-key slugs are tried, then the gang."""
    assert device_slugs("room_a_dk", Thing("key", "C"), "b") == [
        "room_a_dk_c",
        "room_a_dk_b",
        "room_a_dk",
    ]
    assert device_slugs("room_a_dk", Thing("status_led", "A"), "a") == [
        "room_a_dk_a",
        "room_a_dk",
    ]
    assert device_slugs("room_a_dk", Thing("key", "B")) == [
        "room_a_dk_b",
        "room_a_dk",
    ]
    assert device_slugs("boiler", Thing("socket")) == ["boiler"]


async def test_plan_leaves_customised_entities_alone(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    reg = er.async_get(hass)
    renamed = entity_id(hass, "light", UID_LIGHT_SWITCH)
    reg.async_update_entity(renamed, name="My mirror")
    # Home Assistant 2026.10 refuses an area of its own on an entity named after its device, so the second
    # customisation is an icon
    reg.async_update_entity(
        entity_id(hass, "switch", UID_SOCKET), icon="mdi:water-boiler"
    )
    plan = build_import_plan(hass, ours)
    assert {m.ours.unique_id for m in plan.customised} == {UID_LIGHT_SWITCH, UID_SOCKET}
    assert "light.wc_mirror" not in _moves(plan)
    assert "light.wc_mirror" not in {g.entity_id for g in plan.gateway_only}
    assert ("WC mirror", "WC mirror") not in {
        (o.name, t.name) for o, t in plan.devices
    }  # no fresh entity on that device: its area / name are not copied either
    assert ("Boiler", "Boiler") in {
        (o.name, t.name) for o, t in plan.devices
    }  # the sensors still move


async def test_plan_without_gateway_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    plan = build_import_plan(hass, init_integration)
    assert plan.gateway_entries == []
    assert plan.empty
    assert plan.gateway_only == []
    assert UID_LIGHT_SWITCH in {e.unique_id for e in plan.unmatched}


async def test_plan_skips_our_entities_without_a_device(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    orphan = er.async_get(hass).async_get_or_create(
        "light", DOMAIN, f"{NODE_LIGHT_SWITCH}-0003", config_entry=ours
    )
    plan = build_import_plan(hass, ours)
    assert orphan in plan.unmatched


@pytest.mark.parametrize(
    ("domain", "unique_id", "original_name", "expected"),
    [
        ("light", UID_LIGHT_SWITCH, None, Thing("light")),
        ("switch", UID_SOCKET, None, Thing("socket")),
        ("switch", UID_STATUS_LED_WC, "Status LED", Thing("status_led", "A")),
        ("switch", f"{UID_SOCKET}-automatic_dst", "x", None),
        (
            "switch",
            f"{NODE_LIGHT_SWITCH}-0044-key_status_led",
            "x",
            None,
        ),
        ("sensor", f"{UID_SOCKET}-power", "Power", Thing("sensor", "power")),
        ("sensor", f"{UID_SOCKET}-power_on_time", "Power-on time", None),
        ("sensor", f"{MESH_UUID}-proxy", "Proxy node", None),
        ("event", UID_ROCKER_B, "Button B", Thing("key", "B")),
        ("event", f"{NODE_LIGHT_SWITCH}-0044", "x", None),
        ("scene", UID_SCENE_WC_OFF, "WC off", Thing("scene", "WC off")),
        ("scene", UID_SCENE_WC_OFF, None, None),
        ("scene", "not-a-mesh-scene", "WC off", None),
        ("number", f"{UID_SOCKET}-off_delay", "Switch-off delay", None),
    ],
)
async def test_classify(
    hass: HomeAssistant,
    domain: str,
    unique_id: str,
    original_name: str | None,
    expected: Thing | None,
) -> None:
    entry = er.async_get(hass).async_get_or_create(
        domain, DOMAIN, unique_id, original_name=original_name
    )
    assert classify(entry) == expected


# --------------------------------------------------------------------------- applying


async def test_apply_moves_entities_and_copies_device_customisations(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    our_mirror_device = reg.async_get(
        entity_id(hass, "light", UID_LIGHT_SWITCH)
    ).device_id
    our_boiler_device = reg.async_get(entity_id(hass, "switch", UID_SOCKET)).device_id
    assert (
        dev_reg.async_get(our_mirror_device).area_id == "wc"
    )  # the app's room, suggested at creation
    gateway.entry.mock_state(hass, ConfigEntryState.LOADED)

    plan = await async_apply_import(hass, ours)
    await hass.async_block_till_done()

    assert _moves(plan) == EXPECTED_MOVES
    for theirs_id, our_uid in EXPECTED_MOVES.items():
        moved = reg.async_get(theirs_id)
        assert moved is not None, theirs_id
        assert moved.platform == DOMAIN
        assert moved.unique_id == our_uid
        assert moved.config_entry_id == ours.entry_id
        assert reg.async_get_entity_id(moved.domain, DOMAIN, our_uid) == theirs_id
    mirror = reg.async_get("light.wc_mirror")
    assert (mirror.name, mirror.icon) == (
        "Mirror",
        "mdi:mirror",
    )  # user customisations survive
    assert mirror.previous_unique_id == "wc_mirror_001"
    assert mirror.device_id == our_mirror_device
    assert reg.async_get("scene.wc_off").device_id is None
    assert (
        reg.async_get("sensor.boiler_voltage").disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )
    # our devices took over the gateway devices' placement and names; an area we already had stays
    mirror_device = dev_reg.async_get(our_mirror_device)
    assert (
        mirror_device.area_id
        == ar.async_get(hass).async_get_area_by_name("Bathroom").id
    )
    assert mirror_device.name_by_user == "Mirror light"
    assert mirror_device.labels == {"lights"}
    assert dev_reg.async_get(our_boiler_device).area_id == "kitchen"
    # the gateway entry is left disabled with what did not move; ours is up again with the moved entities live
    assert gateway.entry.disabled_by is ConfigEntryDisabler.USER
    assert gateway.entry.state is ConfigEntryState.NOT_LOADED
    assert reg.async_get("event.wc_mirror_button_down").platform == GATEWAY_DOMAIN
    assert (
        reg.async_get("light.kitchen_ceiling").config_entry_id == gateway.entry.entry_id
    )
    assert ours.state is ConfigEntryState.LOADED
    assert hass.states.get("light.wc_mirror") is not None
    assert hass.states.get("event.living_room_rocker_down") is not None
    assert hass.states.get("scene.wc_off") is not None
    assert hass.states.get("light.wc_mirror").name == "Mirror"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(ours)) is None


async def test_apply_twice_is_a_no_op(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    await async_apply_import(hass, ours)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    before = {
        e.entity_id: (e.unique_id, e.platform, e.config_entry_id)
        for e in reg.entities.values()
    }

    plan = build_import_plan(hass, ours)
    assert plan.empty
    assert plan.customised == []
    assert {g.entity_id for g in plan.gateway_only} == {
        "event.wc_mirror_button_down",
        "sensor.boiler_frequency",
        "sensor.boiler_energy",
        "scene.movie_night",
        "light.kitchen_ceiling",
        "light.kitchen_ceiling_2",
    }
    assert UID_LIGHT_SWITCH in {
        e.unique_id for e in plan.unmatched
    }  # the moved entity: no gateway counterpart is left for it

    await hass.config_entries.async_unload(
        ours.entry_id
    )  # applying from an unloaded entry leaves it unloaded
    plan = await async_apply_import(hass, ours)
    await hass.async_block_till_done()
    assert plan.empty
    assert {
        e.entity_id: (e.unique_id, e.platform, e.config_entry_id)
        for e in reg.entities.values()
    } == before
    assert ours.state is ConfigEntryState.NOT_LOADED
    assert gateway.entry.disabled_by is ConfigEntryDisabler.USER


async def test_apply_touches_only_gateway_entries_that_contributed(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    """A second gateway entry (another installation, nothing matched) is neither unloaded nor disabled."""
    other = MockConfigEntry(
        domain=GATEWAY_DOMAIN,
        title="JUNG HOME Gateway (garden)",
        unique_id="serial-2",
        data={"identity_anchor": "gw2"},
    )
    other.add_to_hass(hass)
    other.mock_state(hass, ConfigEntryState.LOADED)
    gateway.entry.mock_state(hass, ConfigEntryState.LOADED)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={(GATEWAY_DOMAIN, "garden_lamp")},
        name="Garden lamp",
    )
    gw_light = er.async_get(hass).async_get_or_create(
        "light",
        GATEWAY_DOMAIN,
        "garden_lamp_001",
        config_entry=other,
        device_id=dev.id,
        suggested_object_id="garden_lamp",
    )
    unloaded: list[str] = []
    real_unload = hass.config_entries.async_unload

    async def spy_unload(entry_id: str, *args: Any, **kwargs: Any) -> bool:
        unloaded.append(entry_id)
        return await real_unload(entry_id, *args, **kwargs)

    with patch.object(hass.config_entries, "async_unload", spy_unload):
        plan = await async_apply_import(hass, ours)
    await hass.async_block_till_done()
    assert plan.contributing_entries == [gateway.entry]
    assert _moves(plan) == EXPECTED_MOVES
    # the contributing gateway entry and ours (later unloads: the disable and our reload); never the other one
    assert unloaded[:2] == [gateway.entry.entry_id, ours.entry_id]
    assert other.entry_id not in unloaded
    assert gateway.entry.disabled_by is ConfigEntryDisabler.USER
    assert other.disabled_by is None
    assert other.state is ConfigEntryState.LOADED
    assert er.async_get(hass).async_get(gw_light.entity_id).platform == GATEWAY_DOMAIN
    assert gw_light.entity_id in {g.entity_id for g in plan.gateway_only}


async def test_apply_with_an_already_disabled_gateway_entry(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    """A gateway entry the user disabled before importing still contributes; it is not disabled twice."""
    await hass.config_entries.async_set_disabled_by(
        gateway.entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    with patch.object(
        hass.config_entries,
        "async_set_disabled_by",
        wraps=hass.config_entries.async_set_disabled_by,
    ) as disable:
        plan = await async_apply_import(hass, ours)
    await hass.async_block_till_done()
    assert _moves(plan) == EXPECTED_MOVES
    assert plan.contributing_entries == [gateway.entry]
    disable.assert_not_called()
    assert er.async_get(hass).async_get("light.wc_mirror").platform == DOMAIN


async def test_apply_keeps_customised_entities_of_ours(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    reg = er.async_get(hass)
    renamed = entity_id(hass, "light", UID_LIGHT_SWITCH)
    reg.async_update_entity(renamed, name="My mirror")
    await async_apply_import(hass, ours)
    await hass.async_block_till_done()
    assert reg.async_get(renamed).name == "My mirror"
    assert reg.async_get(renamed).unique_id == UID_LIGHT_SWITCH
    theirs = reg.async_get("light.wc_mirror")
    assert theirs.platform == GATEWAY_DOMAIN
    assert (
        theirs.disabled_by is er.RegistryEntryDisabler.CONFIG_ENTRY
    )  # its entry was disabled


def _refusing_unload(hass: HomeAssistant, refused: str, how: Any = False) -> Any:
    """`async_unload` that refuses `refused` (False, or raising `how`) and unloads everything else."""
    real_unload = hass.config_entries.async_unload

    async def unload(entry_id: str, *args: Any, **kwargs: Any) -> bool:
        if entry_id == refused:
            if isinstance(how, Exception):
                raise how
            return False
        return await real_unload(entry_id, *args, **kwargs)

    return unload


@pytest.mark.parametrize(
    "how", [False, OperationNotAllowed("setup in progress")], ids=["false", "raises"]
)
async def test_apply_stops_before_the_registries_when_a_gateway_entry_does_not_unload(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry, how: Any
) -> None:
    """A loaded gateway entity cannot be moved: nothing is touched, ours is never unloaded, the gateway stays."""
    gateway.entry.mock_state(hass, ConfigEntryState.LOADED)
    with (
        patch.object(
            hass.config_entries,
            "async_unload",
            _refusing_unload(hass, gateway.entry.entry_id, how),
        ),
        pytest.raises(ImportAborted) as err,
    ):
        await async_apply_import(hass, ours)
    await hass.async_block_till_done()
    assert err.value.title == gateway.entry.title
    assert er.async_get(hass).async_get("light.wc_mirror").platform == GATEWAY_DOMAIN
    assert gateway.entry.disabled_by is None
    assert ours.state is ConfigEntryState.LOADED


async def test_apply_sets_the_gateway_entry_up_again_when_ours_does_not_unload(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    gateway.entry.mock_state(hass, ConfigEntryState.LOADED)
    with (
        patch.object(
            hass.config_entries,
            "async_unload",
            _refusing_unload(hass, ours.entry_id),
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        pytest.raises(ImportAborted) as err,
    ):
        await async_apply_import(hass, ours)
    assert err.value.title == ours.title
    reload.assert_called_once_with(gateway.entry.entry_id)  # ours is still loaded
    assert er.async_get(hass).async_get("light.wc_mirror").platform == GATEWAY_DOMAIN
    assert gateway.entry.disabled_by is None


async def test_import_step_aborts_when_an_entry_does_not_unload(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    result = await _start_import(hass, ours)
    with patch(
        "custom_components.junghome_ble.config_flow.async_apply_import",
        side_effect=ImportAborted("JUNG HOME Gateway"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "import_unload_failed"
    assert result["description_placeholders"] == {"title": "JUNG HOME Gateway"}


# --------------------------------------------------------------------------- repair issue


async def test_issue_offers_the_import_while_a_gateway_entry_is_enabled(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, issue_id(ours))
    assert issue is not None
    assert issue.translation_key == "gateway_import"
    assert issue.translation_placeholders == {"title": ours.title}
    assert not issue.is_fixable

    await hass.config_entries.async_set_disabled_by(
        gateway.entry.entry_id, ConfigEntryDisabler.USER
    )
    async_update_gateway_issue(hass, ours)
    assert issues.async_get_issue(DOMAIN, issue_id(ours)) is None


async def test_no_issue_without_a_gateway_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(init_integration)) is None
    )


# --------------------------------------------------------------------------- the reconfigure step


async def _start_import(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert "import_gateway" in result["menu_options"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "import_gateway"}
    )


async def test_import_step_shows_the_plan_then_applies_it(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    result = await _start_import(hass, ours)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import_gateway"
    placeholders = result["description_placeholders"]
    assert placeholders["count"] == str(len(EXPECTED_MOVES))
    assert "- `light.wc_mirror` → `" in placeholders["matched"]
    assert placeholders["customised"] == "*none*"
    assert "- `scene.all_off`" in placeholders["unmatched"]
    assert "- `scene.movie_night`" in placeholders["gateway_only"]
    assert (
        er.async_get(hass).async_get("light.wc_mirror").platform == GATEWAY_DOMAIN
    )  # still a dry run

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "import_successful"
    assert result["description_placeholders"]["count"] == str(len(EXPECTED_MOVES))
    assert er.async_get(hass).async_get("light.wc_mirror").platform == DOMAIN
    assert gateway.entry.disabled_by is ConfigEntryDisabler.USER
    assert ours.state is ConfigEntryState.LOADED


async def test_import_step_with_nothing_left_to_import(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    await async_apply_import(hass, ours)
    await hass.async_block_till_done()
    result = await _start_import(hass, ours)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "nothing_to_import"
    assert "- `scene.movie_night`" in result["description_placeholders"]["gateway_only"]


async def test_import_step_without_a_gateway_entry(
    hass: HomeAssistant, gateway: Gateway, ours: MockConfigEntry
) -> None:
    """The gateway entry was deleted between the menu and the step."""
    result = await ours.start_reconfigure_flow(hass)
    await hass.config_entries.async_remove(gateway.entry.entry_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "import_gateway"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_gateway_entry"
