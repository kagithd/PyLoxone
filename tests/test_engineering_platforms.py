"""Tests for source-scoped read-only engineering platform entities."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import (
    DATA_DISPATCHER,
    async_dispatcher_connect,
    async_dispatcher_send,
)

from custom_components.loxone import binary_sensor, sensor
from custom_components.loxone.engineering_entities import (
    EngineeringEntitySpec,
    EngineeringPlatformReconciler,
    async_dispatch_engineering_state_updates,
    engineering_event_value,
    engineering_inventory_updated_signal,
    engineering_state_updated_signal,
)
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from custom_components.loxone.engineering_snapshot import StoredEngineeringState
from tests.engineering_fixtures import make_snapshot, numeric_binding, provider_inventory


def entity_spec(
    *,
    unique_id: str = "weather-value",
    state_uuid: str | None = None,
    platform: str = "sensor",
    value: float | bool | None = 18.5,
    available: bool = True,
) -> EngineeringEntitySpec:
    """Build one safe prepared entity contract."""
    return EngineeringEntitySpec(
        unique_id=unique_id,
        state_uuid=state_uuid or unique_id,
        platform=platform,
        name="Outdoor temperature" if platform == "sensor" else "Input I1",
        native_value=value,
        unit="°C" if platform == "sensor" else None,
        available=available,
        owner_identifier="serial-a:weather-server" if platform == "sensor" else "serial-a",
        owner_name="Weather Server" if platform == "sensor" else "Miniserver",
        owner_model="WeatherServer" if platform == "sensor" else "Miniserver",
        room="Office",
        loxone_type="WeatherData" if platform == "sensor" else "DigitalIn",
        io_name="WDC1" if platform == "sensor" else "I1",
        config_version=7,
        runtime_binding="uuid_state" if available else None,
        enabled_by_default=False,
    )


def test_engineering_binary_sensor_is_read_only_and_uses_boolean_value():
    entity = binary_sensor.LoxoneEngineeringBinarySensor(
        entity_spec(unique_id="digital-i1", platform="binary_sensor", value=True)
    )

    assert entity.unique_id == "digital-i1"
    assert entity.is_on is True
    assert entity.device_info == {"identifiers": {("loxone", "serial-a")}}
    assert entity.entity_registry_enabled_default is False
    assert not hasattr(entity, "turn_on")
    assert not hasattr(entity, "turn_off")


def test_cached_entity_spec_is_created_unavailable():
    entity = sensor.LoxoneEngineeringSensor(entity_spec(value=None, available=False))

    assert entity.available is False
    assert entity.native_value is None


def test_failed_rebind_retains_last_safe_value_but_marks_unavailable():
    entity = sensor.LoxoneEngineeringSensor(entity_spec(value=18.5))

    entity.update_spec(entity_spec(value=None, available=False))

    assert entity.available is False
    assert entity.native_value == 18.5


@pytest.mark.anyio
async def test_scoped_state_event_rebinds_cached_entity_and_rejects_unsafe_values(
    tmp_path,
    monkeypatch,
    caplog,
):
    hass = HomeAssistant(str(tmp_path))
    entity = sensor.LoxoneEngineeringSensor(
        entity_spec(state_uuid="weather-state", value=None, available=False),
        "entry-a",
    )
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()

    async_dispatcher_send(hass, engineering_state_updated_signal("entry-b", "weather-state"), 21.0)
    async_dispatcher_send(hass, engineering_state_updated_signal("entry-a", "weather-state"), 19.0)
    async_dispatcher_send(hass, engineering_state_updated_signal("entry-a", "weather-state"), float("nan"))
    async_dispatcher_send(
        hass,
        engineering_state_updated_signal("entry-a", "weather-state"),
        "19.5 private-label",
    )
    async_dispatcher_send(
        hass,
        engineering_state_updated_signal("entry-a", "weather-state"),
        10**1000,
    )

    assert entity.available is True
    assert entity.native_value == 19.0
    assert str(10**1000) not in caplog.text

    entity._call_on_remove_callbacks()
    async_dispatcher_send(hass, engineering_state_updated_signal("entry-a", "weather-state"), 20.0)
    assert entity.native_value == 19.0


@pytest.mark.anyio
async def test_binary_event_accepts_only_boolean_or_zero_one(
    tmp_path,
    monkeypatch,
    caplog,
):
    hass = HomeAssistant(str(tmp_path))
    entity = binary_sensor.LoxoneEngineeringBinarySensor(
        entity_spec(
            unique_id="digital-i1",
            state_uuid="digital-state",
            platform="binary_sensor",
            value=False,
        ),
        "entry-a",
    )
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()

    async_dispatcher_send(hass, engineering_state_updated_signal("entry-a", "digital-state"), 1.0)
    assert entity.is_on is True
    async_dispatcher_send(hass, engineering_state_updated_signal("entry-a", "digital-state"), 2.0)
    async_dispatcher_send(hass, engineering_state_updated_signal("entry-a", "digital-state"), "0")
    async_dispatcher_send(
        hass,
        engineering_state_updated_signal("entry-a", "digital-state"),
        -(10**1000),
    )
    assert entity.is_on is True
    assert str(-(10**1000)) not in caplog.text


@pytest.mark.parametrize("platform", ["sensor", "binary_sensor"])
@pytest.mark.parametrize("value", [10**1000, -(10**1000)])
def test_engineering_event_rejects_oversized_integers(platform, value):
    assert engineering_event_value(platform, value) is None


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["sensor", "binary_sensor"])
async def test_removed_spec_revokes_live_and_queued_events_until_accepted_restore(
    tmp_path,
    monkeypatch,
    platform,
):
    hass = HomeAssistant(str(tmp_path))
    spec = _platform_spec(platform)
    entity = _platform_entity(platform, spec)
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()
    signal = engineering_state_updated_signal("entry-a", spec.state_uuid)
    queued_callback = next(iter(hass.data[DATA_DISPATCHER][signal]))
    prepared = {spec.unique_id: entity}
    _install_empty_entity_registry(monkeypatch)
    reconciler = EngineeringPlatformReconciler(
        "entry-a",
        platform,
        frozenset(),
        lambda current: _platform_entity(platform, current),
        lambda entities: None,
        prepared,
    )

    await reconciler.async_reconcile(hass, ())
    rejected_value = 24.0 if platform == "sensor" else True
    async_dispatcher_send(hass, signal, rejected_value)
    queued_callback(rejected_value)

    assert entity.available is False
    assert _platform_value(entity, platform) == spec.native_value

    cached = replace(spec, native_value=None, available=False, runtime_binding=None)
    await reconciler.async_reconcile(hass, (cached,))
    restored_value = 22.0 if platform == "sensor" else False
    async_dispatcher_send(hass, signal, restored_value)

    assert entity.available is True
    assert _platform_value(entity, platform) == restored_value


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["sensor", "binary_sensor"])
async def test_identity_rejection_revokes_existing_live_entity(
    tmp_path,
    monkeypatch,
    platform,
):
    hass = HomeAssistant(str(tmp_path))
    spec = _platform_spec(platform)
    entity = _platform_entity(platform, spec)
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()
    registry_entry = SimpleNamespace(config_entry_id="entry-b")
    registry = SimpleNamespace(
        async_get_entity_id=lambda *args: f"{platform}.persisted",
        async_get=lambda entity_id: registry_entry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.er.async_get",
        lambda hass: registry,
    )
    reconciler = EngineeringPlatformReconciler(
        "entry-a",
        platform,
        frozenset(),
        lambda current: _platform_entity(platform, current),
        lambda entities: None,
        {spec.unique_id: entity},
    )

    await reconciler.async_reconcile(hass, (spec,))
    async_dispatcher_send(
        hass,
        engineering_state_updated_signal("entry-a", spec.state_uuid),
        24.0 if platform == "sensor" else False,
    )

    assert entity.available is False
    assert _platform_value(entity, platform) == spec.native_value


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["sensor", "binary_sensor"])
async def test_state_uuid_change_rejects_old_queued_callback(tmp_path, monkeypatch, platform):
    hass = HomeAssistant(str(tmp_path))
    spec = _platform_spec(platform)
    entity = _platform_entity(platform, spec)
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()
    old_signal = engineering_state_updated_signal("entry-a", spec.state_uuid)
    queued_callback = next(iter(hass.data[DATA_DISPATCHER][old_signal]))

    entity.update_spec(replace(spec, state_uuid="replacement-state"))
    stale_value = 25.0 if platform == "sensor" else False
    queued_callback(stale_value)
    async_dispatcher_send(hass, old_signal, stale_value)
    assert _platform_value(entity, platform) == spec.native_value
    current_value = 20.0 if platform == "sensor" else True
    async_dispatcher_send(
        hass,
        engineering_state_updated_signal("entry-a", "replacement-state"),
        current_value,
    )

    assert _platform_value(entity, platform) == current_value


@pytest.mark.anyio
async def test_active_sensor_update_refreshes_owner_name_and_semantic_metadata(
    tmp_path,
    monkeypatch,
):
    hass = HomeAssistant(str(tmp_path))
    spec = entity_spec(state_uuid="weather-state")
    entity = sensor.LoxoneEngineeringSensor(spec, "entry-a")
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()
    assert entity.device_class is SensorDeviceClass.TEMPERATURE
    assert entity.state_class is SensorStateClass.MEASUREMENT
    assert entity.native_unit_of_measurement == "°C"
    entity._sensor_option_unit_of_measurement = "°F"

    entity.update_spec(
        replace(
            spec,
            name="Energy total",
            native_value=4.0,
            unit="kWh",
            owner_identifier="serial-a:meter",
        )
    )

    assert entity.name == "Energy total"
    assert entity.native_value == 4.0
    assert entity.native_unit_of_measurement == "kWh"
    assert entity.unit_of_measurement == "kWh"
    assert entity.device_class is SensorDeviceClass.ENERGY
    assert entity.state_class is SensorStateClass.TOTAL_INCREASING
    assert entity.device_info == {"identifiers": {("loxone", "serial-a:meter")}}

    entity.update_spec(
        replace(
            spec,
            name="Analog voltage",
            native_value=7.5,
            unit="V",
            owner_identifier="serial-a:analog-extension",
        )
    )

    assert entity.name == "Analog voltage"
    assert entity.native_value == 7.5
    assert entity.native_unit_of_measurement == "V"
    assert entity.unit_of_measurement == "V"
    assert entity.device_class is None
    assert entity.state_class is SensorStateClass.MEASUREMENT
    assert not hasattr(entity, "entity_description")
    assert entity.device_info == {"identifiers": {("loxone", "serial-a:analog-extension")}}


@pytest.mark.anyio
async def test_active_binary_update_refreshes_owner_and_name(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    spec = _platform_spec("binary_sensor")
    entity = _platform_entity("binary_sensor", spec)
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()

    entity.update_spec(
        replace(
            spec,
            name="Input I2",
            owner_identifier="serial-a:digital-extension",
        )
    )

    assert entity.name == "Input I2"
    assert entity.device_info == {"identifiers": {("loxone", "serial-a:digital-extension")}}


@pytest.mark.anyio
async def test_removed_spec_stays_prepared_but_becomes_unavailable(monkeypatch):
    prepared = {"weather-value": sensor.LoxoneEngineeringSensor(entity_spec())}
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.er.async_get",
        lambda hass: SimpleNamespace(async_get_entity_id=lambda *args: None, async_get=lambda entity_id: None),
    )

    reconciler = EngineeringPlatformReconciler(
        "entry-a",
        "sensor",
        frozenset(),
        lambda spec: sensor.LoxoneEngineeringSensor(spec, "entry-a"),
        lambda entities: None,
        prepared,
    )
    await reconciler.async_reconcile(SimpleNamespace(), ())

    assert tuple(prepared) == ("weather-value",)
    assert prepared["weather-value"].available is False


@pytest.mark.anyio
async def test_entity_ownership_is_rechecked_immediately_before_add(monkeypatch):
    registry_entry = SimpleNamespace(config_entry_id="entry-b")
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: "sensor.persisted",
        async_get=lambda entity_id: registry_entry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.er.async_get",
        lambda hass: registry,
    )
    prepared = {}
    added = []

    reconciler = EngineeringPlatformReconciler(
        "entry-a",
        "sensor",
        frozenset(),
        lambda spec: sensor.LoxoneEngineeringSensor(spec, "entry-a"),
        added.extend,
        prepared,
    )
    await reconciler.async_reconcile(
        SimpleNamespace(),
        (entity_spec(unique_id="raced-uuid"),),
    )

    assert prepared == {}
    assert added == []


class _FakeMiniserver:
    """Minimal real platform dependency without network behavior."""

    def __init__(self) -> None:
        self.serial = "serial-a"
        self.lox_config = SimpleNamespace(json={"controls": {}})
        self.listeners = []

    def async_signal_new_device(self, device_type):
        return f"new-{device_type}"


@pytest.mark.anyio
async def test_sensor_platform_restores_cached_entities_then_rebinds_without_download(
    tmp_path,
    monkeypatch,
):
    hass = HomeAssistant(str(tmp_path))
    miniserver = _FakeMiniserver()
    coordinator = SimpleNamespace(engineering_runtime=None)
    hass.data["loxone"] = {"entry-a": coordinator}
    config_entry = SimpleNamespace(entry_id="entry-a")
    snapshot = make_snapshot()
    cached = StoredEngineeringState(
        snapshot=snapshot,
        registry_applied_generation=snapshot.generation_id,
    )
    monkeypatch.setattr(sensor, "get_miniserver_from_hass", lambda hass, entry: miniserver)
    monkeypatch.setattr(sensor, "async_load_engineering_state", lambda hass, entry_id: _return(cached))
    _install_empty_entity_registry(monkeypatch)
    added = []

    await sensor.async_setup_entry(
        hass,
        config_entry,
        lambda entities, *args, **kwargs: added.extend(entities),
    )

    engineering = [item for item in added if isinstance(item, sensor.LoxoneEngineeringSensor)]
    assert {item.unique_id for item in engineering} == {"weather-value", "system-variable"}
    assert all(item.available is False for item in engineering)

    coordinator.engineering_runtime = EngineeringRuntimeInventory(
        (
            numeric_binding("weather-value", 18.5, "WeatherData"),
            numeric_binding("system-variable", 1.0, "SysVar"),
        )
    )
    async_dispatcher_send(hass, engineering_inventory_updated_signal("entry-a"))
    await hass.async_block_till_done()

    assert all(item.available is True for item in engineering)


@pytest.mark.anyio
async def test_binary_platform_restores_cached_prepared_channel(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    miniserver = _FakeMiniserver()
    runtime = EngineeringRuntimeInventory((numeric_binding("digital-i1", 1.0, "DigitalIn"),))
    snapshot = make_snapshot(inventory=provider_inventory(), runtime=runtime)
    cached = StoredEngineeringState(
        snapshot=snapshot,
        registry_applied_generation=snapshot.generation_id,
    )
    coordinator = SimpleNamespace(engineering_runtime=None)
    hass.data["loxone"] = {"entry-a": coordinator}
    config_entry = SimpleNamespace(entry_id="entry-a")
    monkeypatch.setattr(binary_sensor, "get_miniserver_from_hass", lambda hass, entry: miniserver)
    monkeypatch.setattr(
        binary_sensor,
        "async_load_engineering_state",
        lambda hass, entry_id: _return(cached),
    )
    _install_empty_entity_registry(monkeypatch)
    added = []

    await binary_sensor.async_setup_entry(
        hass,
        config_entry,
        lambda entities, *args, **kwargs: added.extend(entities),
    )

    engineering = [item for item in added if isinstance(item, binary_sensor.LoxoneEngineeringBinarySensor)]
    assert len(engineering) == 1
    assert engineering[0].unique_id == "digital-i1"
    assert engineering[0].available is False
    assert engineering[0].is_on is None


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["sensor", "binary_sensor"])
async def test_pending_generation_revokes_existing_entity_until_applied_restore(
    tmp_path,
    monkeypatch,
    platform,
):
    hass = HomeAssistant(str(tmp_path))
    miniserver = _FakeMiniserver()
    if platform == "sensor":
        platform_module = sensor
        entity_type = sensor.LoxoneEngineeringSensor
        snapshot = make_snapshot()
        runtime = EngineeringRuntimeInventory(
            (
                numeric_binding("weather-value", 18.5, "WeatherData"),
                numeric_binding("system-variable", 1.0, "SysVar"),
            )
        )
        target_uuid = "weather-value"
        live_value = 19.0
        rejected_value = 24.0
    else:
        platform_module = binary_sensor
        entity_type = binary_sensor.LoxoneEngineeringBinarySensor
        runtime = EngineeringRuntimeInventory((numeric_binding("digital-i1", 1.0, "DigitalIn"),))
        snapshot = make_snapshot(inventory=provider_inventory(), runtime=runtime)
        target_uuid = "digital-i1"
        live_value = True
        rejected_value = False
    applied = StoredEngineeringState(
        snapshot=snapshot,
        registry_applied_generation=snapshot.generation_id,
    )
    stored = [applied]
    coordinator = SimpleNamespace(engineering_runtime=runtime)
    hass.data["loxone"] = {"entry-a": coordinator}
    config_entry = SimpleNamespace(entry_id="entry-a")
    monkeypatch.setattr(
        platform_module,
        "get_miniserver_from_hass",
        lambda hass, entry: miniserver,
    )
    monkeypatch.setattr(
        platform_module,
        "async_load_engineering_state",
        lambda hass, entry_id: _return(stored[0]),
    )
    _install_empty_entity_registry(monkeypatch)
    added = []
    await platform_module.async_setup_entry(
        hass,
        config_entry,
        lambda entities, *args, **kwargs: added.extend(entities),
    )
    entity = next(item for item in added if isinstance(item, entity_type) and item.unique_id == target_uuid)
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    await entity.async_added_to_hass()
    signal = engineering_state_updated_signal("entry-a", entity._spec.state_uuid)
    async_dispatcher_send(hass, signal, live_value)
    assert entity.available is True

    stored[0] = StoredEngineeringState(snapshot=snapshot)
    async_dispatcher_send(hass, engineering_inventory_updated_signal("entry-a"))
    await hass.async_block_till_done()
    async_dispatcher_send(hass, signal, rejected_value)

    assert entity.available is False
    assert _platform_value(entity, platform) == live_value

    stored[0] = applied
    async_dispatcher_send(hass, engineering_inventory_updated_signal("entry-a"))
    await hass.async_block_till_done()

    assert entity.available is True
    restored_event = 20.0 if platform == "sensor" else False
    async_dispatcher_send(hass, signal, restored_event)
    assert _platform_value(entity, platform) == restored_event


@pytest.mark.anyio
async def test_sensor_platform_does_not_expose_registry_pending_generation(tmp_path, monkeypatch):
    """A committed snapshot cannot create entities before its owner topology applies."""
    hass = HomeAssistant(str(tmp_path))
    miniserver = _FakeMiniserver()
    coordinator = SimpleNamespace(engineering_runtime=None)
    hass.data["loxone"] = {"entry-a": coordinator}
    config_entry = SimpleNamespace(entry_id="entry-a")
    pending = StoredEngineeringState(snapshot=make_snapshot())
    monkeypatch.setattr(sensor, "get_miniserver_from_hass", lambda hass, entry: miniserver)
    monkeypatch.setattr(sensor, "async_load_engineering_state", lambda hass, entry_id: _return(pending))
    _install_empty_entity_registry(monkeypatch)
    added = []

    await sensor.async_setup_entry(
        hass,
        config_entry,
        lambda entities, *args, **kwargs: added.extend(entities),
    )

    assert not any(isinstance(item, sensor.LoxoneEngineeringSensor) for item in added)


@pytest.mark.anyio
async def test_websocket_projection_is_private_and_source_scoped(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    received = []
    async_dispatcher_connect(
        hass,
        engineering_state_updated_signal("entry-a", "weather-state"),
        received.append,
    )

    async_dispatch_engineering_state_updates(
        hass,
        "entry-b",
        {"weather-state": 21.0},
    )
    async_dispatch_engineering_state_updates(
        hass,
        "entry-a",
        {"weather-state": 19.0},
    )

    assert received == [19.0]


async def _return(value):
    return value


def _platform_spec(platform):
    if platform == "sensor":
        return entity_spec(state_uuid="weather-state")
    return entity_spec(
        unique_id="digital-i1",
        state_uuid="digital-state",
        platform="binary_sensor",
        value=True,
    )


def _platform_entity(platform, spec):
    if platform == "sensor":
        return sensor.LoxoneEngineeringSensor(spec, "entry-a")
    return binary_sensor.LoxoneEngineeringBinarySensor(spec, "entry-a")


def _platform_value(entity, platform):
    return entity.native_value if platform == "sensor" else entity.is_on


def _install_empty_entity_registry(monkeypatch):
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: None,
        async_get=lambda entity_id: None,
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.er.async_get",
        lambda hass: registry,
    )
