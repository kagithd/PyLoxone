"""Tests for conservative engineering entity onboarding."""

from datetime import UTC, datetime

from custom_components.loxone.engineering_config import (
    EngineeringElement,
    EngineeringInventory,
)
from custom_components.loxone.engineering_entities import (
    build_engineering_sensor_specs,
    engineering_inventory_updated_signal,
    normalize_engineering_unit,
)
from custom_components.loxone.engineering_runtime import (
    EngineeringRuntimeBinding,
    EngineeringRuntimeInventory,
)
from custom_components.loxone.sensor import LoxoneEngineeringSensor


def _element(
    uuid: str,
    element_type: str,
    *,
    parent_uuid: str | None = None,
    platform: str | None = None,
) -> EngineeringElement:
    return EngineeringElement(
        key=uuid,
        xml_element="C",
        loxone_type=element_type,
        title="Cellar Temperature" if platform == "sensor" else "1-Wire Device",
        uuid=uuid,
        io_name="AWI1" if platform == "sensor" else None,
        parent_uuid=parent_uuid,
        room_uuid="room-1",
        room="Cellar",
        category_uuid="category-1",
        category="Sensors",
        suggested_platform=platform,
        attributes={},
    )


def _inventory(*elements: EngineeringElement) -> EngineeringInventory:
    now = datetime.now(UTC)
    return EngineeringInventory("test.zip", 1, now, now, 1, elements)


def test_only_bound_numeric_sensor_channels_are_prepared():
    device = _element("device-1", "Lox1wireDevice")
    sensor = _element("sensor-1", "Lox1wireAsensor", parent_uuid=device.uuid, platform="sensor")
    switch = _element("switch-1", "Switch", parent_uuid=device.uuid, platform="switch")
    runtime = EngineeringRuntimeInventory(
        bindings=(
            EngineeringRuntimeBinding(
                "sensor-1", "AWI1", sensor.loxone_type, sensor.title, sensor.room, "sensor", "bound", numeric_value=21.5
            ),
            EngineeringRuntimeBinding(
                "switch-1", "AQ1", switch.loxone_type, switch.title, switch.room, "switch", "bound", numeric_value=1.0
            ),
        )
    )

    specs = build_engineering_sensor_specs(_inventory(device, sensor, switch), runtime)

    assert [spec.element.uuid for spec in specs] == ["sensor-1"]
    assert specs[0].device == device


def test_text_and_unbound_channels_are_not_prepared():
    first = _element("sensor-1", "Lox1wireAsensor", platform="sensor")
    second = _element("sensor-2", "Lox1wireAsensor", platform="sensor")
    runtime = EngineeringRuntimeInventory(
        bindings=(
            EngineeringRuntimeBinding(
                "sensor-1", "AWI1", first.loxone_type, first.title, first.room, "sensor", "bound", numeric_value=None
            ),
            EngineeringRuntimeBinding(
                "sensor-2",
                "AWI2",
                second.loxone_type,
                second.title,
                second.room,
                "sensor",
                "not_found",
                numeric_value=22.0,
            ),
        )
    )

    assert build_engineering_sensor_specs(_inventory(first, second), runtime) == ()


def test_degree_is_only_normalized_for_temperature_context():
    assert normalize_engineering_unit("°", title="Temperatur", loxone_type="Lox1wireAsensor") == "°C"
    assert normalize_engineering_unit("°", title="Wind direction", loxone_type="WeatherData") == "°"
    assert normalize_engineering_unit("kWh", title="Energy", loxone_type="Sensor") == "kWh"


def test_refresh_signal_is_scoped_to_config_entry():
    assert engineering_inventory_updated_signal("entry-1") != engineering_inventory_updated_signal("entry-2")


def test_prepared_sensor_uses_engineering_uuid_and_starts_disabled():
    device = _element("device-1", "Lox1wireDevice")
    element = _element("sensor-1", "Lox1wireAsensor", parent_uuid=device.uuid, platform="sensor")
    binding = EngineeringRuntimeBinding(
        "sensor-1",
        "AWI1",
        element.loxone_type,
        element.title,
        element.room,
        "sensor",
        "bound",
        numeric_value=21.5,
        unit="°",
    )
    spec = build_engineering_sensor_specs(
        _inventory(device, element),
        EngineeringRuntimeInventory(bindings=(binding,)),
    )[0]

    entity = LoxoneEngineeringSensor(spec, "miniserver-serial")

    assert entity.unique_id == "sensor-1"
    assert entity.entity_registry_enabled_default is False
    assert entity.native_value == 21.5
    assert entity.native_unit_of_measurement == "°C"
    assert entity.device_info["identifiers"] == {("loxone", "device-1")}
    assert entity.device_info["via_device"] == ("loxone", "miniserver-serial")
    assert entity.extra_state_attributes["engineering_config_version"] == 1
