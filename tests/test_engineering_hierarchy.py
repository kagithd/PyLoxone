"""Contract tests for the privacy-safe engineering hierarchy projection."""

from __future__ import annotations

import json

from custom_components.loxone.engineering_hierarchy import (
    build_engineering_hierarchy,
    hierarchy_to_dict,
)
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from tests.engineering_fixtures import element, inventory_of, make_snapshot, numeric_binding


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
                }
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
                                    "protected_count": 0,
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
            "unsupported": 1,
            "protected": 1,
        },
    }
    rendered = json.dumps(payload, sort_keys=True)
    assert "synthetic-secret" not in rendered
    assert "Synthetic private value" not in rendered
    assert "PRIVATE1" not in rendered
    assert '"NfcCode"' not in rendered
    assert "topology_path" not in rendered
    assert "owner_key" not in rendered
    assert "attributes" not in rendered
