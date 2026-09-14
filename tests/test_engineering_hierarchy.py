"""Contract tests for the privacy-safe engineering hierarchy projection."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from custom_components.loxone.engineering_hierarchy import (
    build_engineering_hierarchy,
    hierarchy_to_dict,
)
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from tests.engineering_fixtures import element, inventory_of, make_snapshot, numeric_binding


@pytest.mark.parametrize("proven_owner", (False, True))
def test_unknown_safe_functions_remain_visible_without_inferred_ownership(proven_owner):
    """Unknown rows survive exactly once, using only explicit owner metadata."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "FutureDevice", parent_uuid="ms", title="Shared label"),
            element("function", "FutureFunction", parent_uuid="device", title="Shared label"),
            element("unowned", "FutureFunction", title="Shared label"),
            element("channel", "VoltageIn", parent_uuid="device"),
            element("hidden", "NfcCode", parent_uuid="device", title="Synthetic secret"),
        )
    )
    if proven_owner:
        snapshot = replace(
            snapshot,
            rows=tuple(
                replace(row, node=replace(row.node, owner_key="device")) if row.node.element.key == "function" else row
                for row in snapshot.rows
            ),
        )
    payload = hierarchy_to_dict(build_engineering_hierarchy(snapshot))
    device = payload["root"]["children"][0]
    groups = [section for section in payload["root"]["sections"] if section["identifier"] == "unassigned"]
    assert len(groups) == 1
    unassigned = groups[0]
    assert {fn["key"] for fn in device["functions"]} == ({"channel", "function"} if proven_owner else {"channel"})
    assert {fn["key"] for fn in unassigned["functions"]} == ({"unowned"} if proven_owner else {"function", "unowned"})
    functions = device["functions"] + unassigned["functions"]
    assert len(functions) == 3
    for function in functions:
        if function["technical_type"] == "FutureFunction":
            assert function["status"] == "unsupported"
            assert function["reason"] == "unsupported_technical_type"
    assert payload["summary"]["unsupported"] == 2
    assert "Synthetic secret" not in json.dumps(payload)


def test_placement_carriers_are_direct_safe_and_conflicts_are_order_independent():
    """Carrier or inheritance leaks and duplicate-owner selection must fail."""
    from dataclasses import replace
    from custom_components.loxone.engineering_config import parse_engineering_xml
    from custom_components.loxone.engineering_snapshot import snapshot_from_dict, snapshot_to_dict
    from custom_components.loxone.engineering_snapshot import EngineeringSnapshotError
    import pytest
    from tests.engineering_fixtures import SYNTHETIC_PARSE_CONTEXT

    parsed = parse_engineering_xml(
        b"""<C Type="LoxLIVE" U="ms" Installation="Synthetic installation">
      <C Type="LoxLink" U="link" SwitchBoard="Cabinet A">
        <C Type="AirBaseExtension" U="bridge" SwitchBoardRow="2">
          <C Type="FutureDevice" U="device" SwitchBoardPos="4">
            <C Type="VoltageIn" U="input" Installation="Channel placement" />
          </C>
          <C Type="FutureDevice" U="empty" />
        </C>
      </C>
      <C Type="WeatherServer" U="service" Installation="Service placement" />
      <C Type="Page" U="page" Installation="Structural placement">
        <C Type="FutureDevice" U="page-device" />
      </C>
      <C Type="User" U="protected" Installation="Protected placement">
        <C Type="FutureDevice" U="hidden" SwitchBoard="Hidden placement" />
      </C>
    </C>""",
        **SYNTHETIC_PARSE_CONTEXT,
    )
    snapshot = make_snapshot(inventory=parsed)
    restored = snapshot_from_dict(snapshot_to_dict(snapshot))
    payload = hierarchy_to_dict(build_engineering_hierarchy(restored))

    def flatten(node):
        return [node] + [
            item for child in node.get("children", []) + node.get("sections", []) for item in flatten(child)
        ]

    nodes = {node["identifier"]: node for node in flatten(payload["root"])}
    assert nodes["serial-a"]["placement"] == {"installation": "Synthetic installation"}
    assert nodes["serial-a:link"]["placement"] == {"switchboard": "Cabinet A"}
    assert nodes["serial-a:bridge"]["placement"] == {"row": 2}
    assert nodes["serial-a:device"]["placement"] == {"position": 4}
    assert all("placement" not in nodes[key] for key in ("serial-a:empty", "serial-a:page-device", "serial-a:service"))
    assert all("placement" not in fn for node in nodes.values() for fn in node["functions"])
    stored = json.dumps(snapshot_to_dict(snapshot))
    for forbidden in (
        "Channel placement",
        "Service placement",
        "Structural placement",
        "Protected placement",
        "Hidden placement",
    ):
        assert forbidden not in stored
    assert all(node.element.placement is None for node in snapshot.nodes if node.sensitive)
    for key in ("input", "service", "page", "protected", "hidden"):
        tampered = snapshot_to_dict(snapshot)
        next(node for node in tampered["nodes"] if node["key"] == key)["placement"] = {"row": 2}
        with pytest.raises(EngineeringSnapshotError):
            snapshot_from_dict(tampered)

    # Duplicate provider records legitimately share a hierarchy identifier.
    provider = next(item for item in parsed.elements if item.uuid == "ms")
    alternate = replace(
        provider,
        key="ms-other",
        uuid="ms-other",
        placement=replace(provider.placement, installation="Synthetic alternate"),
    )
    for providers in ((provider, alternate), (alternate, provider)):
        duplicates = make_snapshot(inventory=inventory_of(*providers))
        projected = hierarchy_to_dict(build_engineering_hierarchy(duplicates))
        assert "placement" not in json.dumps(projected)


def test_generic_hierarchy_projects_ownership_statuses_and_protected_content():
    """Wrong ownership, status mapping, or sensitive disclosure must fail together."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Provider", room=None),
            element("io", "IoData", parent_uuid="ms", room=None),
            element("local-input", "VoltageIn", parent_uuid="io", title="Local input", io_name="AI1"),
            element("weather-service", "WeatherServer", title="Weather", room=None),
            element(
                "weather-value",
                "WeatherData",
                parent_uuid="weather-service",
                title="Weather value",
                io_name="W1",
            ),
            element("link", "LoxLink", parent_uuid="ms", title="Link", room=None),
            element("bridge", "AirBaseExtension", parent_uuid="link", title="Bridge", room=None),
            element("future-device", "FutureDevice", parent_uuid="bridge", title="Unknown hardware"),
            element("orphan-device", "FutureDevice", title="Standalone hardware", room=None),
            element(
                "prepared-input",
                "VoltageIn",
                parent_uuid="future-device",
                title="Prepared input",
                io_name="AI2",
            ),
            element(
                "inventory-output",
                "Actor",
                parent_uuid="future-device",
                title="Inventory output",
                io_name="Q1",
            ),
            element(
                "unsupported-input",
                "DigitalIn",
                parent_uuid="future-device",
                title="Unsupported input",
                io_name="I1",
            ),
            element("touch", "NfcCodeTouch", parent_uuid="bridge", title="Access device"),
            element(
                "synthetic-secret",
                "NfcCode",
                parent_uuid="touch",
                title="Synthetic private value",
                io_name="PRIVATE1",
            ),
            element(
                "private-state",
                "VoltageIn",
                parent_uuid="synthetic-secret",
                title="Synthetic private state",
                io_name="PRIVATE2",
            ),
        ),
        runtime=EngineeringRuntimeInventory(
            (
                numeric_binding("local-input", 2.0),
                numeric_binding("weather-value", 18.5, "WeatherData"),
                numeric_binding("prepared-input", 3.0),
                numeric_binding("inventory-output", 1.0, "Actor"),
                numeric_binding("unsupported-input", 2.0, "DigitalIn"),
            )
        ),
    )

    payload = hierarchy_to_dict(
        build_engineering_hierarchy(
            snapshot,
            entity_ids_by_unique_id={"weather-value": "sensor.synthetic_weather"},
        )
    )

    assert payload == {
        "root": {
            "identifier": "serial-a",
            "role": "provider",
            "label": "Provider",
            "technical_type": "LoxLIVE",
            "bus_kind": None,
            "functions": [
                {
                    "key": "local-input",
                    "label": "Local input",
                    "technical_type": "VoltageIn",
                    "status": "prepared",
                    "reason": "read_only_event_binding_proven",
                    "entity_id": None,
                }
            ],
            "sections": [
                {
                    "identifier": "serial-a:weather-service",
                    "role": "internal_service",
                    "label": "Weather",
                    "technical_type": "WeatherServer",
                    "bus_kind": None,
                    "functions": [
                        {
                            "key": "weather-value",
                            "label": "Weather value",
                            "technical_type": "WeatherData",
                            "status": "active_entity",
                            "reason": "read_only_event_binding_proven",
                            "entity_id": "sensor.synthetic_weather",
                        }
                    ],
                    "children": [],
                    "protected_count": 0,
                },
                {
                    "identifier": "unassigned",
                    "role": "structural",
                    "label": None,
                    "technical_type": None,
                    "bus_kind": None,
                    "functions": [
                        {
                            "key": "io",
                            "label": "IoData",
                            "technical_type": "IoData",
                            "status": "unsupported",
                            "reason": "unsupported_technical_type",
                            "entity_id": None,
                        }
                    ],
                    "children": [
                        {
                            "identifier": "serial-a:orphan-device",
                            "role": "physical_device",
                            "label": "Standalone hardware",
                            "technical_type": "FutureDevice",
                            "bus_kind": None,
                            "functions": [],
                            "children": [],
                            "protected_count": 0,
                        }
                    ],
                    "protected_count": 0,
                },
            ],
            "children": [
                {
                    "identifier": "serial-a:link",
                    "role": "bus",
                    "label": "Link",
                    "technical_type": "LoxLink",
                    "bus_kind": "link",
                    "functions": [],
                    "children": [
                        {
                            "identifier": "serial-a:bridge",
                            "role": "bus",
                            "label": "Bridge",
                            "technical_type": "AirBaseExtension",
                            "bus_kind": "link",
                            "functions": [],
                            "children": [
                                {
                                    "identifier": "serial-a:touch",
                                    "role": "physical_device",
                                    "label": "Access device",
                                    "technical_type": "NfcCodeTouch",
                                    "bus_kind": "link",
                                    "functions": [],
                                    "children": [],
                                    "protected_count": 1,
                                },
                                {
                                    "identifier": "serial-a:future-device",
                                    "role": "physical_device",
                                    "label": "Unknown hardware",
                                    "technical_type": "FutureDevice",
                                    "bus_kind": "link",
                                    "functions": [
                                        {
                                            "key": "inventory-output",
                                            "label": "Inventory output",
                                            "technical_type": "Actor",
                                            "status": "inventory_only",
                                            "reason": "output_write_contract_not_enabled",
                                            "entity_id": None,
                                        },
                                        {
                                            "key": "prepared-input",
                                            "label": "Prepared input",
                                            "technical_type": "VoltageIn",
                                            "status": "prepared",
                                            "reason": "read_only_event_binding_proven",
                                            "entity_id": None,
                                        },
                                        {
                                            "key": "unsupported-input",
                                            "label": "Unsupported input",
                                            "technical_type": "DigitalIn",
                                            "status": "unsupported",
                                            "reason": "binary_semantics_not_proven",
                                            "entity_id": None,
                                        },
                                    ],
                                    "children": [],
                                    "protected_count": 0,
                                },
                            ],
                            "protected_count": 0,
                        }
                    ],
                    "protected_count": 0,
                }
            ],
            "protected_count": 1,
        },
        "summary": {
            "active_entity": 1,
            "prepared": 2,
            "inventory_only": 1,
            "unsupported": 2,
            "protected": 2,
        },
    }
    rendered = json.dumps(payload, sort_keys=True)
    assert "synthetic-secret" not in rendered
    assert "Synthetic private value" not in rendered
    assert "PRIVATE1" not in rendered
    assert "private-state" not in rendered
    assert "Synthetic private state" not in rendered
    assert "PRIVATE2" not in rendered
    assert '"NfcCode"' not in rendered
    assert "topology_path" not in rendered
    assert "owner_key" not in rendered
    assert "attributes" not in rendered
