"""Power thresholds of the metering socket (`thresholds.py`): the two sensors, `set_threshold`, `delete_threshold`.

The actions run against the `env` of `test_services.py` (a copy of the Android export, the Config Server and the
socket's two threshold properties answered by stubs), so the wiring they plan is checked in the rewritten export.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.junghome_ble import mesh_config
from custom_components.junghome_ble import services as svc
from custom_components.junghome_ble import thresholds as T
from custom_components.junghome_ble.actions import common
from custom_components.junghome_ble.actions import thresholds as threshold_actions
from custom_components.junghome_ble.config_entities import PropertyReader
from custom_components.junghome_ble.configurator import thresholds as thresholds_mod
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.jhmesh import config_messages as C
from custom_components.junghome_ble.jhmesh import properties as P
from custom_components.junghome_ble.jhmesh.export import raw_model

from . import property_helpers as ph
from . import test_services as services_env
from .conftest import FakeProxyLink, settle, setup_entry, wait_for_link
from .helpers import (
    LIGHT_DIMMER,
    LIGHT_SWITCH,
    SOCKET,
    UID_BUTTON_WC,
    UID_LIGHT_DIMMER,
    UID_LIGHT_SWITCH,
    UID_SOCKET,
    english,
    entity_id,
    export_model_status,
    is_model_get,
)
from .test_services import Env, settled, subs

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

mesh = ph.mesh
env, export_source = services_env.env, services_env.export_source
# no entity passes through `unavailable` / `unknown` here either (review-4 D23)
no_entity_loses_its_state = services_env.no_entity_loses_its_state

METER = 0x0173  # the socket's meter element: its OnOff Client switches the thresholds' loads
METER_GROUP = 0xC001  # its element group
SWITCH_ON, SWITCH_OFF = 0x5004, 0x5005
CODEC = P.ThresholdCodec()
UID_SWITCH_OFF = f"{UID_SOCKET}-switch_off_threshold"
UID_SWITCH_ON = f"{UID_SOCKET}-switch_on_threshold"


def wire(threshold: P.Threshold) -> bytes:
    return CODEC.encode(threshold)


def socket(hass: HomeAssistant) -> str:
    return entity_id(hass, "switch", UID_SOCKET)


async def call(hass: HomeAssistant, service: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


def on_air(env: Env) -> list[bytes]:
    """Record the threshold writes and the Config messages in the order they reach the mesh, from now on."""
    sent: list[bytes] = []
    config, app = env.link.config_reply, env.link.app_reply
    assert config is not None
    assert app is not None

    def config_reply(node: int, pdu: bytes) -> bytes | None:
        sent.append(pdu)
        return config(node, pdu)

    def app_reply(dst: int, pdu: bytes) -> bytes | None:
        if pdu[:3] == ADMIN_SET:  # a threshold write
            sent.append(pdu)
        return app(dst, pdu)

    env.link.config_reply, env.link.app_reply = config_reply, app_reply
    return sent


ADMIN_SET = bytes.fromhex("c32705")  # LBC Admin Property Set


def admin_set(pid: int, threshold: P.Threshold) -> bytes:
    """The LBC Admin Property Set that writes a threshold, as the app sends it (access 3)."""
    return ADMIN_SET + pid.to_bytes(2, "little") + bytes([3]) + wire(threshold)


# the socket's OnOff Client publication reset of the app's disable and delete: 0x0000 with TTL 0, then the group
PUBLICATION_RESET = [
    C.model_publication_set(METER, 0, "1001", ttl=0),
    C.model_publication_set(METER, METER_GROUP, "1001"),
]
# the pre-flight reads (review-4 brief 70) before the dimmer leaves the meter's group and the publication is reset
READ_DIMMER_LEAVES = [
    C.model_subscription_get(LIGHT_DIMMER, "1000"),
    C.model_subscription_get(LIGHT_DIMMER, "05271013"),
]
READ_PUBLICATION = [C.model_publication_get(METER, "1001")]


# --------------------------------------------------------------------------- which sockets have them


def test_threshold_targets() -> None:
    """Both thresholds of the metering socket, off by default, on the socket device; nothing for other loads."""
    hub = ph.fake_hub()
    targets = T.threshold_targets(hub)
    assert [(t.address, t.which) for t in targets] == [
        (SOCKET, "switch_on"),
        (SOCKET, "switch_off"),
    ]
    assert [t.unique_id for t in targets] == [UID_SWITCH_ON, UID_SWITCH_OFF]
    assert [t.translation_key for t in targets] == [
        "switch_on_threshold",
        "switch_off_threshold",
    ]
    assert [t.specs for t in targets] == [
        (P.PROPERTIES[SWITCH_ON],),
        (P.PROPERTIES[SWITCH_OFF],),
    ]
    assert not any(t.enabled_default for t in targets)
    # a socket that does not measure (another product) has none; nor one without the OnOff Client
    sock = hub.devices.by_address[SOCKET]
    sock.node.pid = 0x0C
    assert T.threshold_targets(hub) == []
    sock.node.pid = 0x03
    hub.cdb.element(METER).models.remove("1001")
    assert T.threshold_targets(hub) == []


def test_switched_devices() -> None:
    """The loads whose OnOff server listens to the meter element's group; none without the client or the group."""
    hub = ph.fake_hub()
    sock = hub.devices.by_address[SOCKET]
    assert T.switched_devices(hub, sock) == []
    dimmer = hub.cdb.element(LIGHT_DIMMER)
    next(m for m in dimmer.raw_models if m["modelId"] == "1000")["subscribe"].append(
        "C001"
    )
    assert T.switched_devices(hub, sock) == [LIGHT_DIMMER]
    with patch.object(T, "cdb_element_groups", return_value={}):
        assert T.switched_devices(hub, sock) == []
    hub.cdb.element(METER).models.remove("1001")
    assert T.switched_devices(hub, sock) == []


def test_planned_threshold() -> None:
    """The call's fields, the socket's current ones for what it leaves out, power in the socket's 0.1 W steps."""
    current = P.Threshold(12.5, 60, True)
    assert T.planned_threshold(None, {"power": 3.04, "duration": 9}, "s") == (
        P.Threshold(3.0, 9, True)
    )
    assert T.planned_threshold(current, {"enabled": False}, "s") == P.Threshold(
        12.5, 60, False
    )
    assert T.planned_threshold(current, {"power": 20}, "s") == P.Threshold(
        20.0, 60, True
    )
    # left out, `enabled` keeps a threshold's state; one the socket does not hold is written enabled
    disabled = P.Threshold(12.5, 60, False)
    assert T.planned_threshold(disabled, {"power": 20}, "s") == P.Threshold(
        20.0, 60, False
    )
    assert T.planned_threshold(T.CLEARED, {"power": 1, "duration": 2}, "s") == (
        P.Threshold(1.0, 2, True)
    )
    with pytest.raises(ServiceValidationError) as err:
        T.planned_threshold(P.Threshold(None, 0, False), {"duration": 5}, "switch.x")
    assert err.value.translation_key == "threshold_incomplete"
    with pytest.raises(ServiceValidationError):
        T.planned_threshold(None, {"power": 5}, "switch.x")


# --------------------------------------------------------------------------- set_threshold / delete_threshold


async def test_set_threshold_wires_and_writes(hass: HomeAssistant, env: Env) -> None:
    """The app's order on air: the threshold, the client's subscription and publication to the meter's group, then
    each load's JUNG User Property Server and OnOff server subscribed to it."""
    hub = env.hub
    sent = on_air(env)
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_off",
            "power": 5,
            "duration": 300,
            "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
        },
    )
    await settled(hass, env)
    assert env.hub is hub  # the export changed: the model followed it in place
    assert services_env.runs_the_export(env)
    pf = env.reload()
    meter = pf.cdb.element(METER)
    assert pf.publication(meter, "1001") == METER_GROUP
    assert METER_GROUP in subs(pf, METER, "1001")
    assert METER_GROUP in subs(pf, LIGHT_DIMMER, "1000")
    assert METER_GROUP in subs(pf, LIGHT_DIMMER, "05271013")
    assert sent == [
        admin_set(SWITCH_OFF, P.Threshold(5.0, 300, True)),
        C.model_subscription_add(METER, METER_GROUP, "1001"),
        C.model_publication_set(METER, METER_GROUP, "1001"),
        C.model_subscription_add(LIGHT_DIMMER, METER_GROUP, "05271013"),
        C.model_subscription_add(LIGHT_DIMMER, METER_GROUP, "1000"),
    ]
    assert env.thresholds[SOCKET, SWITCH_OFF] == wire(P.Threshold(5.0, 300, True))

    # another load instead: the first one leaves the group
    env.config_calls.clear()
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_on",
            "power": 100,
            "duration": 10,
            "enabled": False,
            "devices": [entity_id(hass, "light", UID_LIGHT_SWITCH)],
        },
    )
    await settled(hass, env)
    pf = env.reload()
    assert METER_GROUP not in subs(pf, LIGHT_DIMMER, "1000")
    assert METER_GROUP not in subs(pf, LIGHT_DIMMER, "05271013")
    assert METER_GROUP in subs(pf, LIGHT_SWITCH, "1000")
    assert [pdu for _n, pdu in env.config_calls] == [
        *READ_DIMMER_LEAVES,  # the pre-flight reads, then the additive steps first
        C.model_subscription_add(LIGHT_SWITCH, METER_GROUP, "05271013"),
        C.model_subscription_add(LIGHT_SWITCH, METER_GROUP, "1000"),
        C.model_subscription_delete(LIGHT_DIMMER, METER_GROUP, "1000"),
        C.model_subscription_delete(LIGHT_DIMMER, METER_GROUP, "05271013"),
    ]
    assert env.thresholds[SOCKET, SWITCH_ON] == wire(P.Threshold(100.0, 10, False))


async def test_set_threshold_keeps_what_it_is_not_given(
    hass: HomeAssistant, env: Env
) -> None:
    """Only `enabled`: the socket's power and duration are read and kept; no devices, no wiring, no reload."""
    hub = env.hub
    env.thresholds[SOCKET, SWITCH_ON] = wire(P.Threshold(42.0, 30, True))
    await call(
        hass,
        "set_threshold",
        {"entity_id": socket(hass), "threshold": "switch_on", "enabled": False},
    )
    assert env.hub is hub
    assert env.config_calls == []
    assert env.thresholds[SOCKET, SWITCH_ON] == wire(P.Threshold(42.0, 30, False))


async def test_set_threshold_keeps_a_disabled_threshold_disabled(
    hass: HomeAssistant, env: Env
) -> None:
    """A new level and duration without `enabled`: the socket's state is read and kept, not switched back on."""
    env.thresholds[SOCKET, SWITCH_OFF] = wire(P.Threshold(42.0, 30, False))
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_off",
            "power": 5,
            "duration": 300,
        },
    )
    assert env.thresholds[SOCKET, SWITCH_OFF] == wire(P.Threshold(5.0, 300, False))


async def test_set_threshold_enabled_without_devices_leaves_the_wiring(
    hass: HomeAssistant, env: Env
) -> None:
    """An active threshold without `devices`: written, nothing wired or unwired, the other threshold not asked."""
    hub = env.hub
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_on",
            "power": 7,
            "duration": 9,
        },
    )
    assert env.hub is hub
    assert env.config_calls == []
    assert env.thresholds[SOCKET, SWITCH_ON] == wire(P.Threshold(7.0, 9, True))
    assert (SOCKET, SWITCH_OFF) not in env.thresholds


async def test_set_threshold_devices_edge_cases(hass: HomeAssistant, env: Env) -> None:
    """No devices: the loads leave, the client keeps its wiring (no reset: that is the disable's and delete's);
    a load without a JUNG User Property Server gets its OnOff server subscribed alone; without an element group
    nothing listens, so an empty list is already so; nothing wired, nothing to unwire."""
    configurator = hass.data[svc.CONFIGURATORS][env.entry.entry_id]
    assert not await configurator.unwire_threshold(
        SOCKET
    )  # the export's meter: no wiring, no publication
    assert env.config_calls == []
    with patch.object(thresholds_mod, "element_groups", return_value={}):
        assert not await configurator.set_threshold_devices(SOCKET, [])
    pf = env.reload()
    switch = pf.cdb.element(LIGHT_SWITCH)
    switch.raw_models[:] = [m for m in switch.raw_models if m["modelId"] != "05271013"]
    switch.models.remove("05271013")
    pf.save(env.path, force=True)
    assert await configurator.set_threshold_devices(SOCKET, [LIGHT_SWITCH])
    assert [pdu for _n, pdu in env.config_calls][-1:] == [
        C.model_subscription_add(LIGHT_SWITCH, METER_GROUP, "1000")
    ]
    env.config_calls.clear()
    assert await configurator.set_threshold_devices(SOCKET, [])
    assert [pdu for _n, pdu in env.config_calls] == [
        C.model_subscription_get(LIGHT_SWITCH, "1000"),
        C.model_subscription_delete(LIGHT_SWITCH, METER_GROUP, "1000"),
    ]
    assert env.reload().publication(METER, "1001") == METER_GROUP


async def test_set_threshold_same_devices_does_not_reload(
    hass: HomeAssistant, env: Env
) -> None:
    dimmer = entity_id(hass, "light", UID_LIGHT_DIMMER)
    data = {
        "entity_id": socket(hass),
        "threshold": "switch_off",
        "power": 1,
        "duration": 1,
        "devices": [dimmer],
    }
    await call(hass, "set_threshold", data)
    await settled(hass, env)
    hub = env.hub
    env.config_calls.clear()
    await call(hass, "set_threshold", data)
    assert env.hub is hub
    assert env.config_calls == []


async def test_a_later_socket_failing_still_has_the_model_follow_an_earlier_one(
    hass: HomeAssistant, env: Env
) -> None:
    """One call wires socket by socket, each its own plan: when the second socket fails after the first one's
    wiring was written to the export, the device model still follows the file."""
    real_sockets = threshold_actions._threshold_sockets
    real_wiring = mesh_config.MeshConfigurator.set_threshold_devices
    wired: list[int] = []

    async def two_sockets(
        hass: HomeAssistant, service_call: Any
    ) -> dict[str, list[int]]:
        return {
            entry: [*addresses, *addresses]
            for entry, addresses in (await real_sockets(hass, service_call)).items()
        }

    async def second_fails(
        self: mesh_config.MeshConfigurator,
        address: int,
        devices: Any,
        **kwargs: Any,
    ) -> bool:
        wired.append(address)
        if len(wired) == 1:
            return await real_wiring(self, address, devices, **kwargs)
        with patch.object(thresholds_mod, "threshold_client", return_value=None):
            return await real_wiring(
                self, address, devices, **kwargs
            )  # fails after its _load

    with (
        patch.object(threshold_actions, "_threshold_sockets", two_sockets),
        patch.object(
            mesh_config.MeshConfigurator, "set_threshold_devices", second_fails
        ),
        patch.object(
            common, "async_follow_export", wraps=common.async_follow_export
        ) as follow,
        pytest.raises(ServiceValidationError) as err,
    ):
        await call(
            hass,
            "set_threshold",
            {
                "entity_id": socket(hass),
                "threshold": "switch_off",
                "power": 5,
                "duration": 300,
                "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
            },
        )
    assert err.value.translation_key == "threshold_not_supported"
    assert len(wired) == 2
    assert METER_GROUP in subs(env.reload(), LIGHT_DIMMER, "1000")  # the first one's
    follow.assert_awaited_once_with(hass, env.entry.entry_id, scenes=False)
    assert services_env.runs_the_export(env)


async def test_set_threshold_without_a_level(hass: HomeAssistant, env: Env) -> None:
    """A socket that answers with nothing: `enabled` alone cannot be written."""
    with pytest.raises(ServiceValidationError) as err:
        await call(
            hass,
            "set_threshold",
            {"entity_id": socket(hass), "threshold": "switch_on", "enabled": True},
        )
    assert err.value.translation_key == "threshold_incomplete"
    assert err.value.translation_placeholders == {"name": socket(hass)}


async def test_a_malformed_threshold_is_none(hass: HomeAssistant, env: Env) -> None:
    """A threshold too short for its layout is no threshold: nothing to keep, and a write it answers did not take."""
    env.thresholds[SOCKET, SWITCH_ON] = b"\x81\x00"
    with pytest.raises(ServiceValidationError) as err:
        await call(
            hass,
            "set_threshold",
            {"entity_id": socket(hass), "threshold": "switch_on", "enabled": True},
        )
    assert err.value.translation_key == "threshold_incomplete"
    env.threshold_sets = False  # the Set is answered with the malformed value
    with pytest.raises(HomeAssistantError) as err:
        await call(
            hass,
            "set_threshold",
            {
                "entity_id": socket(hass),
                "threshold": "switch_on",
                "power": 5,
                "duration": 5,
            },
        )
    assert err.value.translation_key == "threshold_switch_on_not_applied"


@pytest.mark.parametrize(
    ("which", "words"), [("switch_on", "switch-on"), ("switch_off", "switch-off")]
)
async def test_threshold_not_taken(
    hass: HomeAssistant, env: Env, which: str, words: str
) -> None:
    """One key per threshold, so the message names it in words in every language, not as `switch_on`."""
    env.threshold_sets = False
    with pytest.raises(HomeAssistantError) as err:
        await call(
            hass,
            "set_threshold",
            {
                "entity_id": socket(hass),
                "threshold": which,
                "power": 5,
                "duration": 5,
            },
        )
    assert err.value.translation_key == f"threshold_{which}_not_applied"
    assert err.value.translation_placeholders == {
        "address": "Boiler (0172)",
        "applied": english(mesh_config.APPLIED_NOTHING),
    }
    assert str(err.value) == (
        f"The JUNG socket Boiler (0172) did not take its {words} threshold. {english(mesh_config.APPLIED_NOTHING)}"
    )


@pytest.mark.parametrize(
    ("which", "words"), [("switch_on", "switch-on"), ("switch_off", "switch-off")]
)
async def test_threshold_lost_link(
    hass: HomeAssistant, env: Env, which: str, words: str
) -> None:
    with (
        patch.object(PropertyReader, "write", side_effect=ConnectionError),
        pytest.raises(HomeAssistantError) as err,
    ):
        await call(
            hass,
            "set_threshold",
            {
                "entity_id": socket(hass),
                "threshold": which,
                "power": 5,
                "duration": 5,
            },
        )
    assert err.value.translation_key == f"threshold_{which}_send_failed"
    assert err.value.translation_placeholders == {
        "address": "Boiler (0172)",
        "applied": english(mesh_config.APPLIED_NOTHING),
    }
    assert str(err.value) == (
        f"The {words} threshold could not be sent to the JUNG socket Boiler (0172); no proxy node is connected. "
        f"{english(mesh_config.APPLIED_NOTHING)}"
    )


def test_threshold_errors_name_the_threshold_in_every_language() -> None:
    """Each threshold has its own key in every language, and no text shows the raw `switch_on` / `switch_off`."""
    keys = [*T.NOT_APPLIED.values(), *T.SEND_FAILED.values()]
    assert len(set(keys)) == 4
    folder = Path(T.__file__).parent
    for path in [folder / "strings.json", *sorted(folder.glob("translations/*.json"))]:
        exceptions = json.loads(path.read_text(encoding="utf-8"))["exceptions"]
        messages = [exceptions[key]["message"] for key in keys]
        assert len(set(messages)) == 4, path.name
        for message in messages:
            assert "switch_" not in message, (path.name, message)
            assert "{which}" not in message, (path.name, message)


async def test_a_threshold_failure_names_what_was_written_before_it(
    hass: HomeAssistant, env: Env
) -> None:
    """Review-4 W4-13: a write that fails after others of the same call took says which — the thresholds of the
    socket under way, the sockets done — not that nothing before it was applied."""
    real_sockets = threshold_actions._threshold_sockets
    real_write = PropertyReader.write
    writes: list[int] = []
    fail_at = 0

    async def two_sockets(
        hass: HomeAssistant, service_call: Any
    ) -> dict[str, list[int]]:
        return {
            entry: [*addresses, *addresses]
            for entry, addresses in (await real_sockets(hass, service_call)).items()
        }

    async def write(self: PropertyReader, *args: Any, **kwargs: Any) -> Any:
        writes.append(1)
        if len(writes) == fail_at:
            raise ConnectionError
        return await real_write(self, *args, **kwargs)

    # the write that fails: the switch-off threshold of the first socket, then both of the second
    expected = {
        2: (
            "threshold_switch_off_send_failed",
            (
                "The switch-on threshold of socket Boiler (0172) was written before it. Run the action again with the "
                "same target to finish."
            ),
        ),
        3: (
            "threshold_switch_on_send_failed",
            "Before it, socket Boiler (0172) was set as asked. Run the action again with the same target to finish.",
        ),
        4: (
            "threshold_switch_off_send_failed",
            (
                "Before it, socket Boiler (0172) was set as asked. The switch-on threshold of socket Boiler (0172) was written "
                "before it. Run the action again with the same target to finish."
            ),
        ),
    }
    with (
        patch.object(threshold_actions, "_threshold_sockets", two_sockets),
        patch.object(PropertyReader, "write", write),
    ):
        for fail_at, (key, applied) in expected.items():  # noqa: B007  # read by `write`
            writes.clear()
            with pytest.raises(HomeAssistantError) as err:
                await call(hass, "delete_threshold", {"entity_id": socket(hass)})
            assert err.value.translation_key == key
            assert err.value.translation_placeholders["applied"] == applied
            await settled(hass, env)


def test_threshold_progress_words_a_stopped_wiring_plan() -> None:
    """The wiring plan's own account follows what the call wrote before it; with nothing before, the usual one."""
    progress = T.ThresholdProgress()
    assert progress.applied(0, 4) == mesh_config.APPLIED_NOTHING
    assert progress.applied(1, 4) == mesh_config.applied_text(1, 4)
    progress.wrote(SOCKET, "switch_on")
    assert english(progress.done()) == (
        "The switch-on threshold of socket 0172 was written before it."
    )
    progress.wrote(SOCKET, "switch_off")
    done = "Both thresholds of socket 0172 were written before it."
    assert english(progress.applied(0, 4)) == (
        f"{done} Run the action again with the same target to finish."
    )
    assert english(progress.applied(3, 4)) == (
        f"{done} {english(mesh_config.applied_text(3, 4))}"
    )
    progress.finish(SOCKET)
    progress.wrote(SOCKET + 1, "switch_off")
    assert english(progress.done()) == (
        "Before it, socket 0172 was set as asked. "
        "The switch-off threshold of socket 0173 was written before it."
    )
    progress.finish(SOCKET + 1)
    assert english(progress.done()) == (
        "Before it, sockets 0172, 0173 were set as asked."
    )
    # a socket `write_threshold` named goes by that name (`address_label`)
    progress.names[SOCKET] = "Boiler (0172)"
    assert english(progress.done()) == (
        "Before it, sockets Boiler (0172), 0173 were set as asked."
    )


async def test_set_threshold_disabled_unwires_like_the_app(
    hass: HomeAssistant, env: Env
) -> None:
    """Disabled while the other threshold is not active either: the app's disable on air, in its order — the
    threshold, each load leaving the group (OnOff server, then `0x0527:1013`), the client's publication reset; the
    unwiring's pre-flight reads before the threshold is written (review-5 W5-4)."""
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_on",
            "power": 20,
            "duration": 5,
            "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
        },
    )
    await settled(hass, env)
    disable = {"entity_id": socket(hass), "threshold": "switch_on", "enabled": False}
    # the other threshold is still active: the loads stay wired for it
    env.thresholds[SOCKET, SWITCH_OFF] = wire(P.Threshold(5.0, 60, True))
    env.config_calls.clear()
    await call(hass, "set_threshold", disable)
    assert env.config_calls == []
    # ... now it is not (the socket publishes its cleared switch-off threshold)
    env.thresholds[SOCKET, SWITCH_OFF] = wire(T.CLEARED)
    env.link.inject(SOCKET, 0xC000, ph.vendor_status(0x05, SWITCH_OFF, wire(T.CLEARED)))
    await hass.async_block_till_done()
    sent = on_air(env)
    await call(hass, "set_threshold", disable)
    await settled(hass, env)
    assert sent == [
        *READ_DIMMER_LEAVES,
        *READ_PUBLICATION,
        admin_set(SWITCH_ON, P.Threshold(20.0, 5, False)),
        C.model_subscription_delete(LIGHT_DIMMER, METER_GROUP, "1000"),
        C.model_subscription_delete(LIGHT_DIMMER, METER_GROUP, "05271013"),
        *PUBLICATION_RESET,
    ]
    pf = env.reload()
    assert METER_GROUP not in subs(pf, LIGHT_DIMMER, "1000")
    assert METER_GROUP not in subs(pf, LIGHT_DIMMER, "05271013")
    assert pf.publication(METER, "1001") == METER_GROUP


async def test_editing_a_disabled_threshold_keeps_the_wiring(
    hass: HomeAssistant, env: Env
) -> None:
    """Only a disable unwires (the app's `ToggleThreshold`): a new level for a threshold that is already disabled,
    with the other one not active either, is written and the loads wired while both were off stay wired."""
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_on",
            "power": 20,
            "duration": 5,
            "enabled": False,
            "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
        },
    )
    await settled(hass, env)
    assert METER_GROUP in subs(env.reload(), LIGHT_DIMMER, "1000")
    env.thresholds[SOCKET, SWITCH_OFF] = wire(T.CLEARED)
    hub = env.hub
    sent = on_air(env)
    await call(
        hass,
        "set_threshold",
        {"entity_id": socket(hass), "threshold": "switch_on", "power": 30},
    )
    assert sent == [admin_set(SWITCH_ON, P.Threshold(30.0, 5, False))]
    assert env.hub is hub
    pf = env.reload()
    assert METER_GROUP in subs(pf, LIGHT_DIMMER, "1000")
    assert METER_GROUP in subs(pf, LIGHT_DIMMER, "05271013")


async def test_refused_wiring_writes_no_threshold(
    hass: HomeAssistant, env: Env
) -> None:
    """The configurator's checks run before the threshold is written: a refused call leaves the socket as it was
    rather than holding a new, active threshold with the old wiring."""
    with (
        patch.object(thresholds_mod, "element_groups", return_value={}),
        pytest.raises(ServiceValidationError) as err,
    ):
        await call(
            hass,
            "set_threshold",
            {
                "entity_id": socket(hass),
                "threshold": "switch_on",
                "power": 20,
                "duration": 5,
                "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
            },
        )
    assert err.value.translation_key == "service_no_element_group"
    assert (SOCKET, SWITCH_ON) not in env.thresholds
    assert env.config_calls == []


async def test_publication_reset_is_sent_as_planned(
    hass: HomeAssistant, env: Env
) -> None:
    """The reset's two steps go out in the app's order, neither dropped: a refused second step leaves the export
    saying the client publishes nothing, which is what the socket holds."""
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_on",
            "power": 20,
            "duration": 5,
            "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
        },
    )
    await settled(hass, env)
    env.refuse[PUBLICATION_RESET[1]] = 0x04  # Invalid Publish Parameters
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "delete_threshold", {"entity_id": socket(hass)})
    # both thresholds were cleared before the plan (W4-13)
    assert err.value.translation_placeholders["applied"] == (
        "Both thresholds of socket Boiler (0172) were written before it. "
        f"{english(mesh_config.applied_text(3, 4))}"
    )
    await settled(hass, env)
    assert [pdu for _n, pdu in env.config_calls[-2:]] == PUBLICATION_RESET
    assert env.reload().publication(METER, "1001") is None


async def test_delete_threshold(hass: HomeAssistant, env: Env) -> None:
    """Both thresholds cleared, every load unwired, then the client's publication reset as the app does; with
    nothing left to unwire, the reset alone and no reload (the export already says so)."""
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_off",
            "power": 5,
            "duration": 300,
            "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
        },
    )
    await settled(hass, env)
    sent = on_air(env)
    await call(hass, "delete_threshold", {"entity_id": socket(hass)})
    await settled(hass, env)
    cleared = wire(T.CLEARED)
    assert env.thresholds[SOCKET, SWITCH_ON] == cleared
    assert env.thresholds[SOCKET, SWITCH_OFF] == cleared
    assert sent == [
        *READ_DIMMER_LEAVES,
        *READ_PUBLICATION,
        admin_set(SWITCH_ON, T.CLEARED),
        admin_set(SWITCH_OFF, T.CLEARED),
        C.model_subscription_delete(LIGHT_DIMMER, METER_GROUP, "1000"),
        C.model_subscription_delete(LIGHT_DIMMER, METER_GROUP, "05271013"),
        *PUBLICATION_RESET,
    ]
    pf = env.reload()
    assert METER_GROUP not in subs(pf, LIGHT_DIMMER, "1000")
    assert METER_GROUP not in subs(pf, LIGHT_DIMMER, "05271013")
    hub = env.hub
    env.config_calls.clear()
    await call(hass, "delete_threshold", {"entity_id": socket(hass)})
    assert env.hub is hub
    assert [pdu for _n, pdu in env.config_calls] == [
        *READ_PUBLICATION,
        *PUBLICATION_RESET,
    ]


async def test_delete_threshold_unwires_what_the_app_wired(
    hass: HomeAssistant, env: Env
) -> None:
    """A `0x0527:1013` subscription left without its OnOff server is removed too; the meter's own `0x0527:1013`
    on its group is not wiring and stays, and a client that publishes nothing gets no publication reset."""
    pf = env.reload()
    pf.subscribe(LIGHT_SWITCH, "05271013", METER_GROUP)
    pf.save(env.path, force=True)
    await call(hass, "delete_threshold", {"entity_id": socket(hass)})
    await settled(hass, env)
    # the export's meter publishes nothing: no publication reset
    assert [pdu for _n, pdu in env.config_calls] == [
        C.model_subscription_get(LIGHT_SWITCH, "05271013"),
        C.model_subscription_delete(LIGHT_SWITCH, METER_GROUP, "05271013"),
    ]
    pf = env.reload()
    assert METER_GROUP not in subs(pf, LIGHT_SWITCH, "05271013")
    assert METER_GROUP in subs(pf, METER, "05271013")


async def test_threshold_targets_are_checked(hass: HomeAssistant, env: Env) -> None:
    """Only metering sockets have thresholds; they switch lights and sockets of their own network."""
    with pytest.raises(ServiceValidationError) as err:
        await call(
            hass,
            "delete_threshold",
            {"entity_id": entity_id(hass, "light", UID_LIGHT_DIMMER)},
        )
    assert err.value.translation_key == "service_not_a_load"
    with (
        patch.object(threshold_actions, "has_thresholds", return_value=False),
        pytest.raises(ServiceValidationError) as err,
    ):
        await call(hass, "delete_threshold", {"entity_id": socket(hass)})
    assert err.value.translation_key == "threshold_not_supported"
    data = {"entity_id": socket(hass), "threshold": "switch_on", "power": 1}
    key = entity_id(hass, "event", UID_BUTTON_WC)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "set_threshold", {**data, "devices": [key]})
    assert err.value.translation_key == "service_not_a_load"
    real = threshold_actions._device_of_entity
    dimmer = entity_id(hass, "light", UID_LIGHT_DIMMER)

    def elsewhere(hass: HomeAssistant, entity: str) -> Any:
        entry, device, registry_id = real(hass, entity)
        return ("another-entry" if entity == dimmer else entry), device, registry_id

    with (
        patch.object(threshold_actions, "_device_of_entity", elsewhere),
        pytest.raises(ServiceValidationError) as err,
    ):
        await call(
            hass,
            "set_threshold",
            {**data, "devices": [dimmer]},
        )
    assert err.value.translation_key == "threshold_other_network"
    assert env.config_calls == []


async def test_set_threshold_devices_refusals(hass: HomeAssistant, env: Env) -> None:
    """The configurator's own checks: a client, an element group, OnOff servers only."""
    configurator = hass.data[svc.CONFIGURATORS][env.entry.entry_id]
    with pytest.raises(ServiceValidationError) as err:
        await configurator.set_threshold_devices(SOCKET, [0x0149])  # a key
    assert err.value.translation_key == "service_not_a_load"
    with (
        patch.object(thresholds_mod, "element_groups", return_value={}),
        pytest.raises(ServiceValidationError) as err,
    ):
        await configurator.set_threshold_devices(SOCKET, [LIGHT_DIMMER])
    assert err.value.translation_key == "service_no_element_group"
    with (
        patch.object(thresholds_mod, "threshold_client", return_value=None),
        pytest.raises(ServiceValidationError) as err,
    ):
        await configurator.set_threshold_devices(SOCKET, [])
    assert err.value.translation_key == "threshold_not_supported"
    with (
        patch.object(thresholds_mod, "threshold_client", return_value=None),
        pytest.raises(ServiceValidationError) as err,
    ):
        await configurator.unwire_threshold(SOCKET)
    assert err.value.translation_key == "threshold_not_supported"
    assert err.value.translation_placeholders == {
        "name": "Boiler (0172)"
    }  # as the message wants
    assert env.config_calls == []


async def test_nothing_to_unwire_without_an_element_group(
    hass: HomeAssistant, env: Env
) -> None:
    """A client the app never gave an element group has no load listening to one: `delete_threshold` clears
    both thresholds and is done, rather than failing after the clear for want of the group."""
    hub = env.hub
    env.thresholds[SOCKET, SWITCH_ON] = wire(P.Threshold(42.0, 30, True))
    with patch.object(thresholds_mod, "element_groups", return_value={}):
        await call(hass, "delete_threshold", {"entity_id": socket(hass)})
    assert env.thresholds[SOCKET, SWITCH_ON] == wire(T.CLEARED)
    assert env.thresholds[SOCKET, SWITCH_OFF] == wire(T.CLEARED)
    assert env.hub is hub
    assert env.config_calls == []


# --------------------------------------------------------------------------- the sensors


async def test_threshold_sensors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth_env: dict[str, Any],
    fake_link: FakeProxyLink,
    mesh: ph.PropertyMesh,
    fast_sleep: list[float],
) -> None:
    """The power level as the state; duration, enabled and the loads switched as attributes; unknown when unset."""
    registry = er.async_get(hass)
    for uid in (UID_SWITCH_ON, UID_SWITCH_OFF):
        registry.async_get_or_create("sensor", DOMAIN, uid, disabled_by=None)
    mesh.values[SOCKET, SWITCH_ON] = wire(P.Threshold(250.5, 120, True))
    await setup_entry(hass, mock_config_entry)
    await wait_for_link(hass, mock_config_entry)
    await settle(hass)
    on = hass.states.get(entity_id(hass, "sensor", UID_SWITCH_ON))
    assert on.state == "250.5"
    assert on.attributes["unit_of_measurement"] == "W"
    assert (
        on.attributes["duration"],
        on.attributes["enabled"],
        on.attributes["devices"],
        on.attributes["property_id"],
    ) == (120, True, [], "0x5004")
    off = hass.states.get(entity_id(hass, "sensor", UID_SWITCH_OFF))
    assert off.state == "unknown"  # the default: no threshold
    assert off.attributes["enabled"] is False

    # the loads it switches, by entity (an element without one by its address)
    sensor = entity_id(hass, "sensor", UID_SWITCH_ON)
    with patch(
        "custom_components.junghome_ble.sensor.switched_devices",
        return_value=[LIGHT_DIMMER, 0x7FFF],
    ):
        hub = mock_config_entry.runtime_data
        hub.notify_update(SOCKET)
        await hass.async_block_till_done()
        assert hass.states.get(sensor).attributes["devices"] == [
            entity_id(hass, "light", UID_LIGHT_DIMMER),
            "7FFF",
        ]
    # a value that does not decode: unknown, no threshold attributes
    fake_link.inject(SOCKET, 0xC000, ph.vendor_status(0x05, SWITCH_ON, b"\x01"))
    await hass.async_block_till_done()
    state = hass.states.get(sensor)
    assert state.state == "unknown"
    assert "duration" not in state.attributes


async def test_unbound_client_is_bound_first(hass: HomeAssistant, env: Env) -> None:
    """A client the export shows without the AppKey gets a Model App Bind before its publication."""
    pf = env.reload()
    raw_model(pf.cdb.element(METER), "1001")["bind"] = []
    pf.save(env.path, force=True)
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_on",
            "power": 1,
            "duration": 1,
            "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
        },
    )
    await settled(hass, env)
    assert C.model_app_bind(METER, "1001", 0) in [p for _n, p in env.config_calls]
    assert raw_model(env.reload().cdb.element(METER), "1001")["bind"] == [0]


async def test_a_meter_the_app_rewired_stops_the_unwiring_and_force_runs_it(
    hass: HomeAssistant, env: Env
) -> None:
    """Review-4 brief 70: `delete_threshold` reads the meter's publication before resetting it; one the app moved
    stops the call before the thresholds are cleared (review-5 W5-4: the reads come first) and says so; `force`
    runs it."""
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_off",
            "power": 5,
            "duration": 300,
            "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
        },
    )
    await settled(hass, env)
    answer = env.link.config_reply
    assert answer is not None
    read = C.model_publication_get(METER, "1001")

    def moved(node: int, pdu: bytes) -> bytes | None:
        if pdu == read:
            env.config_calls.append((node, pdu))
            return export_model_status(
                env.path, pdu, publications={(METER, "1001"): 0xC0FE}
            )
        return answer(node, pdu)

    env.link.config_reply = moved
    env.config_calls.clear()
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "delete_threshold", {"entity_id": socket(hass)})
    assert err.value.translation_key == "service_preflight_differs"
    placeholders = err.value.translation_placeholders or {}
    assert (placeholders["expected"], placeholders["found"]) == (
        "element group #0x173 (C001)",  # the export's name for the group
        "C0FE",  # a group the export does not name
    )
    assert placeholders["applied"].startswith("Nothing before it was applied")
    assert not any(not is_model_get(pdu) for _n, pdu in env.config_calls)
    assert env.thresholds[SOCKET, SWITCH_OFF] != wire(T.CLEARED)  # not cleared
    await call(hass, "delete_threshold", {"entity_id": socket(hass), "force": True})
    await settled(hass, env)
    assert [pdu for _n, pdu in env.config_calls if not is_model_get(pdu)][-2:] == (
        PUBLICATION_RESET
    )


async def test_set_threshold_compares_before_it_writes_the_threshold(
    hass: HomeAssistant, env: Env
) -> None:
    """Review-5 W5-4: `set_threshold` with `devices` reads what its wiring removes before the threshold is
    written: a load the app rewired since the export stops the call with the threshold as it was, nothing
    written (it used to be written first, and kept switching the old loads)."""
    await call(
        hass,
        "set_threshold",
        {
            "entity_id": socket(hass),
            "threshold": "switch_off",
            "power": 5,
            "duration": 300,
            "devices": [entity_id(hass, "light", UID_LIGHT_DIMMER)],
        },
    )
    await settled(hass, env)
    written = env.thresholds[SOCKET, SWITCH_OFF]
    answer = env.link.config_reply
    assert answer is not None

    def rewired(node: int, pdu: bytes) -> bytes | None:
        if pdu in READ_DIMMER_LEAVES:
            env.config_calls.append((node, pdu))
            return export_model_status(
                env.path, pdu, subscriptions={(LIGHT_DIMMER, "1000"): []}
            )
        return answer(node, pdu)

    env.link.config_reply = rewired
    env.config_calls.clear()
    with pytest.raises(HomeAssistantError) as err:
        await call(
            hass,
            "set_threshold",
            {
                "entity_id": socket(hass),
                "threshold": "switch_off",
                "power": 50,
                "devices": [],
            },
        )
    assert err.value.translation_key == "service_preflight_differs"
    assert (err.value.translation_placeholders or {})["applied"].startswith(
        "Nothing before it was applied"
    )
    assert env.thresholds[SOCKET, SWITCH_OFF] == written  # not written
    assert all(is_model_get(pdu) for _n, pdu in env.config_calls)
