"""Tests for source-scoped read-only engineering platform entities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send

from custom_components.loxone import binary_sensor, sensor
from custom_components.loxone.engineering_entities import (
    EngineeringEntitySpec,
    EngineeringPlatformReconciler,
    async_dispatch_engineering_state_updates,
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

    assert entity.available is True
    assert entity.native_value == 19.0

    entity._call_on_remove_callbacks()
    async_dispatcher_send(hass, engineering_state_updated_signal("entry-a", "weather-state"), 20.0)
    assert entity.native_value == 19.0


@pytest.mark.anyio
async def test_binary_event_accepts_only_boolean_or_zero_one(tmp_path, monkeypatch):
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
    assert entity.is_on is True


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


def _install_empty_entity_registry(monkeypatch):
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: None,
        async_get=lambda entity_id: None,
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.er.async_get",
        lambda hass: registry,
    )
