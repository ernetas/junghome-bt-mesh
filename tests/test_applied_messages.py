"""What a stopped plan applied — an error's `{applied}` — is worded in Home Assistant's language.

Every variant (`configurator.plan`, `thresholds.ThresholdProgress`) is an `applied_<key>` message of `exceptions`
with numbers, addresses and device names as placeholders, read through the translation cache
(`configurator.store.applied_message`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers.translation import async_load_integrations

from custom_components.junghome_ble import mesh_config as mc
from custom_components.junghome_ble.const import DOMAIN
from custom_components.junghome_ble.thresholds import ThresholdProgress

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.junghome_ble.configurator.plan import Applied

COMPONENT = Path(mc.__file__).parent
SOCKET = 0x0172


def _progress(*steps: tuple[str, int, str | None]) -> ThresholdProgress:
    progress = ThresholdProgress()
    for kind, address, which in steps:
        if kind == "wrote":
            progress.wrote(address, which)  # type: ignore[arg-type]
        else:
            progress.finish(address)
    return progress


# every variant, with the English it reads as
VARIANTS: dict[str, tuple[Applied, str]] = {
    "nothing": (
        mc.APPLIED_NOTHING,
        "Nothing before it was applied; the mesh export is unchanged.",
    ),
    "partly": (
        mc.applied_text(3, 8),
        (
            "The 3 of 8 messages accepted before it were applied on the mesh and are recorded in the mesh export; run "
            "the action again with the same target to complete it."
        ),
    ),
    "key_wired": (
        mc.APPLIED_KEY_WIRED,
        (
            "Every connection of the key was configured and is recorded in the mesh export; only the key mode is "
            "missing — run the action again with the same target to set it."
        ),
    ),
    "lock_wired": (
        mc.APPLIED_LOCK_WIRED,
        (
            "Every connection of the key was configured and is recorded in the mesh export; its lock function and key "
            "mode are missing — run the action again with the same target to set them."
        ),
    ),
    "scene_wired": (
        mc.APPLIED_SCENE_WIRED,
        (
            "The key publishes its scene recalls to all devices and that is recorded in the mesh export; the scene it "
            "recalls and its key mode are missing — run the action again with the same scene to set them."
        ),
    ),
    "removed": (
        mc.applied_removed("WC mirror (0300)", 1, 2),
        (
            "Device WC mirror (0300) was reset and the mesh export records it as removed from the network; 1 of the 2 messages "
            "taking the other devices' links to it away were applied and are recorded too. The links left on the other "
            "devices point at a device that no longer answers; the mesh export keeps them, so nothing new reuses their "
            "groups."
        ),
    ),
    "scene_stored": (
        mc.applied_scene_stored("Living room DALI (0232)", 2),
        (
            "Scene 2 is stored on device Living room DALI (0232) and the member is recorded in the mesh export; only the scene description "
            "is missing — run the action again to write it."
        ),
    ),
    "scene_cleared": (
        mc.applied_scene_cleared("Living room DALI (0232)", 2),
        (
            "The scene description of Living room DALI (0232) for scene 2 was cleared; the scene itself is still stored on the device and "
            "recorded in the mesh export — run the action again to finish."
        ),
    ),
    "scene_forgotten": (
        mc.applied_scene_cleared("Boiler (0172)", 2, 1, 2),
        (
            "1 of 2 devices already forgot scene 2 and the mesh export records that. The scene description of Boiler (0172) for "
            "scene 2 was cleared; the scene itself is still stored on the device and recorded in the mesh export — run "
            "the action again to finish."
        ),
    ),
    "scene_members": (
        mc.applied_scene_members(1, 3, 4),
        "1 of 3 devices stored scene 4 and are recorded in the mesh export; run the action again to finish.",
    ),
    "keys_cleared": (
        mc.applied_members(0, 3, 4, keys_cleared=True),
        (
            "The keys of these devices that recalled scene 4 were cleared and the mesh export records that; run the "
            "action again to finish."
        ),
    ),
    "members": (
        mc.applied_members(2, 3, 4),
        "2 of 3 devices already forgot scene 4 and the mesh export records that; run the action again to finish.",
    ),
    "unused_deleted": (
        mc.applied_unused_deleted({"0148": [5, 6], "0172": [7]}),
        (
            "Scenes unknown to the mesh export were already deleted before it (0148: 5, 6; 0172: 7); the mesh export is "
            "unchanged, as it never held them — run the action again to finish."
        ),
    ),
    "switch_on_written": (
        _progress(("wrote", SOCKET, "switch_on")).text(),
        (
            "The switch-on threshold of socket 0172 was written before it. Run the action again with the same target "
            "to finish."
        ),
    ),
    "switch_off_written": (
        _progress(("wrote", SOCKET, "switch_off")).applied(2, 4),
        (
            "The switch-off threshold of socket 0172 was written before it. The 2 of 4 messages accepted before it were "
            "applied on the mesh and are recorded in the mesh export; run the action again with the same target to "
            "complete it."
        ),
    ),
    "thresholds_written": (
        _progress(
            ("wrote", SOCKET, "switch_on"), ("wrote", SOCKET, "switch_off")
        ).text(),
        (
            "Both thresholds of socket 0172 were written before it. Run the action again with the same target to "
            "finish."
        ),
    ),
    "socket_set": (
        _progress(("finish", SOCKET, None), ("wrote", 0x0180, "switch_on")).text(),
        (
            "Before it, socket 0172 was set as asked. The switch-on threshold of socket 0180 was written before it. "
            "Run the action again with the same target to finish."
        ),
    ),
    "sockets_set": (
        _progress(("finish", SOCKET, None), ("finish", 0x0180, None)).applied(0, 3),
        "Before it, sockets 0172, 0180 were set as asked. Run the action again with the same target to finish.",
    ),
}


def _exceptions(language: str) -> dict[str, str]:
    path = COMPONENT / "translations" / f"{language}.json"
    data = json.loads(path.read_text(encoding="utf-8"))["exceptions"]
    return {key: value["message"] for key, value in data.items()}


def _worded(applied: Applied, messages: dict[str, str]) -> str:
    return " ".join(
        messages[f"applied_{key}"].format_map(placeholders)
        for key, placeholders in applied.sentences
    )


def test_every_applied_message_has_a_variant() -> None:
    """Each `applied_*` text is one some stop says, and each variant says only texts that exist."""
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    keys = {key for key in strings["exceptions"] if key.startswith("applied_")}
    said = {
        f"applied_{key}"
        for applied, _ in VARIANTS.values()
        for key, _ in applied.sentences
    }
    assert said == keys


def test_placeholders_are_numbers_addresses_and_device_names() -> None:
    """Home Assistant translates a message, never a placeholder's value: no word to translate is one. A device goes
    by the owner's own name for it, which no language translates, with its address (`address_label`)."""
    for applied, _ in VARIANTS.values():
        for _key, placeholders in applied.sentences:
            for value in placeholders.values():
                assert re.fullmatch(r"[0-9A-F:;, ]+|[^()]+ \([0-9A-F]{4}\)", value), (
                    value
                )


@pytest.mark.parametrize("variant", VARIANTS)
async def test_english(hass: HomeAssistant, variant: str) -> None:
    await async_load_integrations(hass, {DOMAIN})
    applied, english = VARIANTS[variant]
    assert mc.applied_message(hass, applied) == english


@pytest.mark.parametrize("variant", VARIANTS)
async def test_german(hass: HomeAssistant, variant: str) -> None:
    """In the server's language, through the translation cache Home Assistant fills when it sets the integration up."""
    hass.config.language = "de"
    await async_load_integrations(hass, {DOMAIN})
    applied, english = VARIANTS[variant]
    german = mc.applied_message(hass, applied)
    assert german == _worded(applied, _exceptions("de"))
    assert german != english


async def test_german_reads_as_written(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    await async_load_integrations(hass, {DOMAIN})
    assert mc.applied_message(hass, VARIANTS["partly"][0]) == (
        "Die 3 von 8 Nachrichten, die davor angenommen wurden, sind im Mesh übernommen und im Mesh-Export "
        "festgehalten. Führe die Aktion mit demselben Ziel erneut aus, um sie abzuschließen."
    )
    assert mc.applied_message(hass, VARIANTS["socket_set"][0]) == (
        "Davor wurde Steckdose 0172 wie gewünscht eingestellt. Die Einschaltschwelle der Steckdose 0180 wurde davor "
        "geschrieben. Führe die Aktion mit demselben Ziel erneut aus, um abzuschließen."
    )


async def test_a_language_without_the_text_falls_back(hass: HomeAssistant) -> None:
    """English where the server's language has no text; the key where no language has one (a newer version's)."""
    await async_load_integrations(hass, {DOMAIN})
    hass.config.language = "xx"  # nothing cached for it
    applied, english = VARIANTS["nothing"]
    assert mc.applied_message(hass, applied) == english
    assert (
        mc.applied_message(hass, applied + mc.said("unknown", n=1))
        == f"{english} applied_unknown"
    )
