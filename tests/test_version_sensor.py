"""Tests for Miniserver metadata sensors."""

from custom_components.loxone.sensor import LoxoneKeepAliveSensor, LoxoneVersionSensor


def test_version_sensor_unique_id_does_not_include_software_version():
    """A firmware update must update state instead of creating a new entity."""
    old = LoxoneVersionSensor("serial", [17, 1, 6, 30])
    new = LoxoneVersionSensor("serial", [17, 1, 7, 27])

    assert old.unique_id == "serial-loxone_software_version"
    assert new.unique_id == old.unique_id
    assert old.native_value == "17.1.6.30"
    assert new.native_value == "17.1.7.27"


def test_miniserver_metadata_sensor_ids_use_only_stable_identifiers():
    """Runtime values must never participate in metadata sensor identities."""
    keep_alive = LoxoneKeepAliveSensor("serial")
    version = LoxoneVersionSensor("serial", [17, 1, 7, 27])

    assert keep_alive.unique_id == "serial-loxone_keep_alive_sensor_uuid"
    assert version.unique_id == "serial-loxone_software_version"
    assert version.native_value not in version.unique_id
