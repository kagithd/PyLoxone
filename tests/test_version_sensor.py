"""Tests for Miniserver metadata sensors."""

from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.loxone.device_sync import async_migrate_version_sensor_unique_id
from custom_components.loxone.sensor import LoxoneKeepAliveSensor, LoxoneVersionSensor


@pytest.mark.anyio
async def test_migrated_version_sensor_reuses_registry_identity(tmp_path):
    """Registering the real sensor after migration must reuse the original registry row."""
    hass = HomeAssistant(str(tmp_path))
    config_entry = SimpleNamespace(entry_id="entry-a", pref_disable_new_entities=False, disabled_by=None)
    hass.config_entries = SimpleNamespace(async_get_entry=lambda _entry_id: config_entry)
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    registry = er.async_get(hass)
    original = registry.async_get_or_create(
        "sensor",
        "loxone",
        "serial-17.1.6.30",
        config_entry=config_entry,
        suggested_object_id="loxone_software_version",
    )

    assert async_migrate_version_sensor_unique_id(hass, config_entry, "serial") == 1
    sensor = LoxoneVersionSensor("serial", [17, 1, 7, 27])
    registered = registry.async_get_or_create("sensor", "loxone", sensor.unique_id, config_entry=config_entry)

    assert registered.id == original.id
    assert registered.entity_id == original.entity_id
    assert len(registry.entities) == 1
    assert async_migrate_version_sensor_unique_id(hass, config_entry, "serial") == 0


def test_version_sensor_unique_id_does_not_include_software_version():
    """A firmware update must update state instead of creating a new entity."""
    old = LoxoneVersionSensor("serial", [17, 1, 6, 30])
    new = LoxoneVersionSensor("serial", [17, 1, 7, 27])

    assert old.unique_id == "serial-loxone_software_version_uuid"
    assert new.unique_id == old.unique_id
    assert old.native_value == "17.1.6.30"
    assert new.native_value == "17.1.7.27"


def test_miniserver_metadata_sensor_ids_use_only_stable_identifiers():
    """Runtime values must never participate in metadata sensor identities."""
    keep_alive = LoxoneKeepAliveSensor("serial")
    version = LoxoneVersionSensor("serial", [17, 1, 7, 27])

    assert keep_alive.unique_id == "serial-loxone_keep_alive_sensor_uuid"
    assert version.unique_id == "serial-loxone_software_version_uuid"
    assert version.native_value not in version.unique_id
