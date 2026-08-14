"""Tests for synchronizing Loxone device names."""

from types import SimpleNamespace

from custom_components.loxone.const import DOMAIN
from custom_components.loxone.device_sync import (
    async_sync_device_names,
    device_names_from_lox_config,
)


class FakeDeviceRegistry:
    """Minimal device registry used by the synchronization tests."""

    def __init__(self, devices):
        self.devices = devices
        self.lookups = []
        self.updates = []

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.lookups.append((identifier, config_entry_id))
        return self.devices.get(identifier)

    def async_update_device(self, device_id, **changes):
        self.updates.append((device_id, changes))
        device = next(device for device in self.devices.values() if device.id == device_id)
        for key, value in changes.items():
            setattr(device, key, value)


def test_device_names_from_lox_config_uses_action_uuid_and_control_key():
    """Only usable names and identifiers are included."""
    lox_config = {
        "controls": {
            "fallback-uuid": {"name": "ST-F01"},
            "control-key": {"uuidAction": "action-uuid", "name": "ST-F02"},
            "missing-name": {"uuidAction": "ignored"},
            "empty-name": {"uuidAction": "ignored-too", "name": ""},
            "invalid-control": None,
        }
    }

    assert device_names_from_lox_config(lox_config) == {
        "fallback-uuid": "ST-F01",
        "action-uuid": "ST-F02",
    }


def test_sync_updates_integration_name_and_preserves_user_name(monkeypatch):
    """A Loxone rename must not overwrite a manual Home Assistant name."""
    device = SimpleNamespace(
        id="device-id",
        name="Smart Socket Air",
        name_by_user="Schreibtisch-Steckdose",
    )
    registry = FakeDeviceRegistry({(DOMAIN, "socket-uuid"): device})
    monkeypatch.setattr("custom_components.loxone.device_sync.dr.async_get", lambda hass: registry)

    updated = async_sync_device_names(
        object(),
        SimpleNamespace(entry_id="entry-id"),
        {
            "controls": {
                "socket-uuid": {
                    "uuidAction": "socket-uuid",
                    "name": "ST-F07",
                }
            }
        },
    )

    assert updated == 1
    assert device.name == "ST-F07"
    assert device.name_by_user == "Schreibtisch-Steckdose"
    assert registry.updates == [("device-id", {"name": "ST-F07"})]
    assert registry.lookups == [((DOMAIN, "socket-uuid"), "entry-id")]


def test_sync_ignores_unchanged_and_not_yet_registered_devices(monkeypatch):
    """Only existing devices whose Loxone name changed are updated."""
    existing = SimpleNamespace(id="existing-id", name="ST-F01")
    registry = FakeDeviceRegistry({(DOMAIN, "existing-uuid"): existing})
    monkeypatch.setattr("custom_components.loxone.device_sync.dr.async_get", lambda hass: registry)

    updated = async_sync_device_names(
        object(),
        SimpleNamespace(entry_id="entry-id"),
        {
            "controls": {
                "existing-uuid": {"name": "ST-F01"},
                "not-registered": {"name": "ST-F02"},
            }
        },
    )

    assert updated == 0
    assert registry.updates == []
