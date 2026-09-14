"""Tests for conservative engineering entity onboarding."""

from datetime import UTC, datetime
import asyncio
from dataclasses import replace
from types import SimpleNamespace

from custom_components.loxone.engineering_config import (
    EngineeringElement,
    EngineeringInventory,
)
from custom_components.loxone.engineering_entities import (
    EngineeringEntitySpec,
    build_engineering_sensor_specs,
    engineering_inventory_updated_signal,
    filter_existing_loxapp_entities,
    normalize_engineering_unit,
)
from custom_components.loxone.engineering_runtime import (
    EngineeringRuntimeBinding,
    EngineeringRuntimeInventory,
)
from custom_components.loxone.sensor import LoxoneEngineeringSensor


def test_metadata_adapter_serializes_with_batch_state_and_loads_mappings(monkeypatch):
    """The compatibility adapter cannot publish stale metadata during a batch transaction."""
    from custom_components.loxone import engineering_entities as entities
    from custom_components.loxone.engineering_registry import (
        engineering_area_operation_lock,
        registry_metadata_from_snapshot,
    )
    from custom_components.loxone.engineering_snapshot import (
        StoredEngineeringState,
        async_load_engineering_state,
        async_store_engineering_state,
        EngineeringAreaDecision,
        EngineeringAreaBatchIntent,
        EngineeringAreaBatchGroup,
        EngineeringAreaBatchMember,
    )
    from tests.engineering_fixtures import make_snapshot
    from tests.test_engineering_coordinator import MemoryStore

    MemoryStore.data = {}
    MemoryStore.events = []
    MemoryStore.fault = None
    monkeypatch.setattr("custom_components.loxone.engineering_snapshot.EngineeringStateStore", MemoryStore)
    monkeypatch.setattr(entities, "Store", MemoryStore)

    async def scenario():
        hass = SimpleNamespace(data={})
        snapshot = make_snapshot()
        state = StoredEngineeringState(snapshot)
        await async_store_engineering_state(hass, state)
        metadata = registry_metadata_from_snapshot(snapshot, applied_generation=snapshot.generation_id)
        decision = EngineeringAreaDecision("room-a", "use_existing", area_id="area-a", conflict_tokens=("a" * 64,))
        batch = EngineeringAreaBatchIntent(
            "entry-a",
            "serial-a",
            snapshot.generation_id,
            (EngineeringAreaBatchGroup(decision, (EngineeringAreaBatchMember("a" * 64, "serial-a:device", None),)),),
        )
        async with engineering_area_operation_lock(hass, "entry-a"):
            task = asyncio.create_task(entities.async_store_engineering_registry_metadata(hass, "entry-a", metadata))
            await asyncio.sleep(0)
            assert (await async_load_engineering_state(hass, "entry-a")).registry_applied_generation is None
            await async_store_engineering_state(
                hass, replace(state, room_area_mappings={"room-a": "area-a"}, pending_area_batch=batch)
            )
        await task
        latest = await async_load_engineering_state(hass, "entry-a")
        assert latest.room_area_mappings == {"room-a": "area-a"}
        assert latest.pending_area_batch == batch
        assert latest.registry_applied_generation == snapshot.generation_id
        assert (await entities.async_load_engineering_registry_metadata(hass, "entry-a")).room_area_mappings == {
            "room-a": "area-a"
        }

    asyncio.run(scenario())


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


def _entity_spec(unique_id: str = "sensor-1") -> EngineeringEntitySpec:
    return EngineeringEntitySpec(
        unique_id=unique_id,
        state_uuid=f"{unique_id}-state",
        platform="sensor",
        name="Cellar Temperature",
        native_value=21.5,
        unit="°C",
        available=True,
        owner_identifier="serial-a:device-1",
        owner_name="ST-F01",
        owner_model="Lox1wireDevice",
        room="Cellar",
        loxone_type="Lox1wireAsensor",
        io_name="AWI1",
        config_version=7,
        runtime_binding="uuid_all",
        enabled_by_default=False,
    )


def test_prepared_sensor_uses_engineering_uuid_and_resolved_owner_only():
    entity = LoxoneEngineeringSensor(_entity_spec())

    assert entity.unique_id == "sensor-1"
    assert entity.entity_registry_enabled_default is False
    assert entity.native_value == 21.5
    assert entity.native_unit_of_measurement == "°C"
    assert entity.device_info == {"identifiers": {("loxone", "serial-a:device-1")}}
    assert entity.extra_state_attributes == {
        "uuid": "sensor-1",
        "io_name": "AWI1",
        "loxone_type": "Lox1wireAsensor",
        "engineering_config_version": 7,
        "runtime_binding": "uuid_all",
    }


def test_public_loxapp_uuid_suppresses_duplicate_engineering_entity():
    assert filter_existing_loxapp_entities((_entity_spec("existing-uuid"),), {"existing-uuid"}) == ()
