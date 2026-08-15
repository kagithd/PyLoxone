"""Tests for synchronizing Loxone device metadata."""

from types import SimpleNamespace

from custom_components.loxone.const import DOMAIN
from custom_components.loxone.device_sync import (
    async_cleanup_stale_devices,
    async_migrate_version_sensor_unique_id,
    async_sync_device_areas,
    async_sync_device_names,
    device_names_from_lox_config,
    device_rooms_from_lox_config,
)


class FakeDeviceRegistry:
    """Minimal device registry used by the synchronization tests."""

    def __init__(self, devices):
        self.devices = devices
        self.lookups = []
        self.updates = []
        self.removed = []

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.lookups.append((identifier, config_entry_id))
        return self.devices.get(identifier)

    def async_update_device(self, device_id, **changes):
        self.updates.append((device_id, changes))
        device = next(device for device in self.devices.values() if device.id == device_id)
        for key, value in changes.items():
            setattr(device, key, value)

    def async_remove_device(self, device_id):
        self.removed.append(device_id)


class FakeAreaRegistry:
    """Minimal area registry used by the synchronization tests."""

    def __init__(self, areas=()):
        self.areas = {area.name: area for area in areas}
        self.created = []

    def async_get_area_by_name(self, name):
        return self.areas.get(name)

    def async_get_or_create(self, name):
        area = SimpleNamespace(id=name.lower().replace(" ", "_"), name=name)
        self.areas[name] = area
        self.created.append(name)
        return area


class FakeEntityRegistry:
    """Minimal entity registry used by the synchronization tests."""

    def __init__(self, entities=()):
        self.entities = list(entities)
        self.updates = []
        self.removed = []

    def async_update_entity(self, entity_id, **changes):
        self.updates.append((entity_id, changes))
        entity = next(entity for entity in self.entities if entity.entity_id == entity_id)
        for key, value in changes.items():
            setattr(entity, "unique_id" if key == "new_unique_id" else key, value)

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        self.entities = [entity for entity in self.entities if entity.entity_id != entity_id]


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


def test_device_rooms_from_lox_config_resolves_room_uuid_and_name():
    """Raw room UUIDs and platform-resolved room names are supported."""
    lox_config = {
        "rooms": {"room-uuid": {"name": "Wohnzimmer"}},
        "controls": {
            "raw": {"uuidAction": "raw-action", "room": "room-uuid"},
            "resolved": {"room": "B\u00fcro"},
            "unassigned": {"room": ""},
            "invalid-control": None,
        },
    }

    assert device_rooms_from_lox_config(lox_config) == {
        "raw-action": "Wohnzimmer",
        "resolved": "B\u00fcro",
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


def test_sync_areas_moves_existing_device_and_creates_missing_area(monkeypatch):
    """Loxone rooms override stale Home Assistant device assignments."""
    device = SimpleNamespace(id="device-id", name="ST-F07", area_id="burro")
    device_registry = FakeDeviceRegistry({(DOMAIN, "socket-uuid"): device})
    area_registry = FakeAreaRegistry()
    entity = SimpleNamespace(
        entity_id="switch.st_f07",
        config_entry_id="entry-id",
        platform=DOMAIN,
        area_id="burro",
        device_id="device-id",
    )
    entity_registry = FakeEntityRegistry([entity])
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.dr.async_get",
        lambda hass: device_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.ar.async_get",
        lambda hass: area_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_get",
        lambda hass: entity_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_entries_for_device",
        lambda registry, device_id: [entry for entry in registry.entities if entry.device_id == device_id],
    )

    updated = async_sync_device_areas(
        object(),
        SimpleNamespace(entry_id="entry-id"),
        {
            "controls": {
                "socket-uuid": {"name": "ST-F07", "room": "B\u00fcro"},
            }
        },
    )

    assert updated == 1
    assert area_registry.created == ["B\u00fcro"]
    assert device.area_id == "b\u00fcro"
    assert device_registry.updates == [("device-id", {"area_id": "b\u00fcro"})]
    assert entity.area_id is None
    assert entity_registry.updates == [("switch.st_f07", {"area_id": None})]


def test_sync_areas_ignores_unchanged_and_unknown_devices(monkeypatch):
    """Only registered devices assigned to a different area are updated."""
    device = SimpleNamespace(id="device-id", name="ST-F01", area_id="wohnzimmer")
    device_registry = FakeDeviceRegistry({(DOMAIN, "existing-uuid"): device})
    area_registry = FakeAreaRegistry([SimpleNamespace(id="wohnzimmer", name="Wohnzimmer")])
    entity_registry = FakeEntityRegistry()
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.dr.async_get",
        lambda hass: device_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.ar.async_get",
        lambda hass: area_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_get",
        lambda hass: entity_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_entries_for_device",
        lambda registry, device_id: [],
    )

    updated = async_sync_device_areas(
        object(),
        SimpleNamespace(entry_id="entry-id"),
        {
            "controls": {
                "existing-uuid": {"room": "Wohnzimmer"},
                "not-registered": {"room": "K\u00fcche"},
            }
        },
    )

    assert updated == 0
    assert area_registry.created == []
    assert device_registry.updates == []


def test_cleanup_removes_only_devices_absent_from_current_loxone(monkeypatch):
    """Stale Loxone registry devices and their entities are removed safely."""
    active = SimpleNamespace(
        id="active-device",
        identifiers={(DOMAIN, "active-uuid")},
        config_entries={"entry-id"},
        model="Switch",
    )
    stale = SimpleNamespace(
        id="stale-device",
        identifiers={(DOMAIN, "stale-uuid")},
        config_entries={"entry-id"},
        model="Switch",
    )
    miniserver = SimpleNamespace(
        id="miniserver-device",
        identifiers={(DOMAIN, "serial")},
        config_entries={"entry-id"},
        model="Miniserver Gen. 2",
    )
    device_registry = FakeDeviceRegistry(
        {
            (DOMAIN, "active-uuid"): active,
            (DOMAIN, "stale-uuid"): stale,
            (DOMAIN, "serial"): miniserver,
        }
    )
    active_entity = SimpleNamespace(
        entity_id="switch.active",
        config_entry_id="entry-id",
        platform=DOMAIN,
        device_id="active-device",
    )
    stale_entity = SimpleNamespace(
        entity_id="switch.stale",
        config_entry_id="entry-id",
        platform=DOMAIN,
        device_id="stale-device",
    )
    entity_registry = FakeEntityRegistry([active_entity, stale_entity])
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.dr.async_get",
        lambda hass: device_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_get",
        lambda hass: entity_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.dr.async_entries_for_config_entry",
        lambda registry, entry_id: list(registry.devices.values()),
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_entries_for_device",
        lambda registry, device_id: [
            entity for entity in registry.entities if entity.device_id == device_id
        ],
    )

    removed = async_cleanup_stale_devices(
        object(),
        SimpleNamespace(entry_id="entry-id"),
        {
            "msInfo": {"serialNr": "serial"},
            "controls": {"active-uuid": {"name": "Active"}},
        },
    )

    assert removed == (1, 1)
    assert device_registry.removed == ["stale-device"]
    assert entity_registry.removed == ["switch.stale"]


def test_cleanup_does_nothing_when_structure_contains_no_controls(monkeypatch):
    """A malformed or empty structure must never trigger mass deletion."""
    device_registry = FakeDeviceRegistry({})
    entity_registry = FakeEntityRegistry()
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.dr.async_get",
        lambda hass: device_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_get",
        lambda hass: entity_registry,
    )

    assert async_cleanup_stale_devices(
        object(), SimpleNamespace(entry_id="entry-id"), {"controls": {}}
    ) == (0, 0)
    assert device_registry.removed == []


def test_version_sensor_registry_identity_is_migrated_before_setup(monkeypatch):
    """The existing entity ID survives a Miniserver software upgrade."""
    entity = SimpleNamespace(
        entity_id="sensor.loxone_software_version",
        unique_id="serial-17.1.6.30",
        config_entry_id="entry-id",
        platform=DOMAIN,
    )
    entity_registry = FakeEntityRegistry([entity])
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_get",
        lambda hass: entity_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.device_sync.er.async_entries_for_config_entry",
        lambda registry, entry_id: list(registry.entities),
    )

    migrated = async_migrate_version_sensor_unique_id(
        object(), SimpleNamespace(entry_id="entry-id"), "serial"
    )

    assert migrated == 1
    assert entity.entity_id == "sensor.loxone_software_version"
    assert entity.unique_id == "serial-loxone_software_version"
