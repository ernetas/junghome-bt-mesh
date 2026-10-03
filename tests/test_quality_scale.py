"""quality_scale.yaml: hassfest does not read it for a custom integration, so nothing else checks that it parses."""

from __future__ import annotations

from pathlib import Path

import yaml

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "junghome_ble"
STATUSES = {"done", "todo", "exempt"}


def test_quality_scale_parses_and_every_rule_has_a_status() -> None:
    rules = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    for name, rule in rules.items():
        status = rule if isinstance(rule, str) else rule["status"]
        assert status in STATUSES, name
        if status == "exempt":
            assert isinstance(rule, dict), name
            assert rule.get("comment"), f"{name}: an exemption says why"


def test_brands_is_done_only_with_the_images_shipped() -> None:
    rule = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"][
        "brands"
    ]
    assert rule["status"] == "done"
    assert (INTEGRATION / "brand" / "icon.png").is_file()
