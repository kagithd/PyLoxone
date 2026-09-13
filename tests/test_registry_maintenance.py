"""Tests for guarded Loxone registry maintenance."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from custom_components.loxone.const import DOMAIN
from custom_components.loxone.engineering_registry import EngineeringRegistryMetadata
from custom_components.loxone.registry_maintenance import (
    async_run_registry_maintenance,
)


class FakeStore:
    """In-memory replacement for Home Assistant storage."""

    data = None
    engineering_generation = "generation-a"
    engineering_provider = None
    fail_save = False
    fail_save_attempt = None
    save_attempts = 0

    def __init__(self, *args, **kwargs):
        pass

    async def async_load(self):
        return self.__class__.data

    async def async_save(self, data):
        self.__class__.save_attempts += 1
        if self.__class__.fail_save or (self.__class__.fail_save_attempt == self.__class__.save_attempts):
            raise RuntimeError("injected maintenance store failure")
        self.__class__.data = deepcopy(data)

    async def async_save_acknowledged(self, data):
        await self.async_save(data)


class FakeDeviceRegistry:
    """Minimal mutable device registry."""

    def __init__(self, devices):
        self.devices = list(devices)
        self.removed = []
        self.updated = []

    def async_remove_device(self, device_id):
        self.removed.append(device_id)
        self.devices = [device for device in self.devices if device.id != device_id]

    def async_update_device(self, device_id, **changes):
        self.updated.append((device_id, changes))


class FakeEntityRegistry:
    """Minimal mutable entity registry."""

    def __init__(self, entities=()):
        self.entities = list(entities)
        self.removed = []

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        self.entities = [entity for entity in self.entities if entity.entity_id != entity_id]


def _install_registry_fakes(monkeypatch, devices, entities=(), areas=()):
    device_registry = FakeDeviceRegistry(devices)
    entity_registry = FakeEntityRegistry(entities)
    area_registry = SimpleNamespace(
        async_get_area_by_name=lambda name: next((area for area in areas if area.name == name), None)
    )
    notifications = []
    dismissed = []

    async def load_engineering_metadata(hass, entry_id):
        del hass, entry_id
        return EngineeringRegistryMetadata(
            frozenset(),
            frozenset(),
            {},
            FakeStore.engineering_generation,
            FakeStore.engineering_provider,
        )

    monkeypatch.setattr("custom_components.loxone.registry_maintenance.Store", FakeStore)
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.async_load_engineering_registry_metadata",
        load_engineering_metadata,
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.dr.async_get",
        lambda hass: device_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.er.async_get",
        lambda hass: entity_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.ar.async_get",
        lambda hass: area_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.dr.async_entries_for_config_entry",
        lambda registry, entry_id: list(registry.devices),
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.er.async_entries_for_device",
        lambda registry, device_id: [entity for entity in registry.entities if entity.device_id == device_id],
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.dr.async_entries_for_area",
        lambda registry, area_id: [device for device in registry.devices if device.area_id == area_id],
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.er.async_entries_for_area",
        lambda registry, area_id: [entity for entity in registry.entities if entity.area_id == area_id],
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.persistent_notification.async_create",
        lambda hass, message, title, notification_id: notifications.append((message, title, notification_id)),
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.persistent_notification.async_dismiss",
        lambda hass, notification_id: dismissed.append(notification_id),
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.automation.automations_with_area",
        lambda hass, area_id: {"automation.old_room"},
    )
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.script.scripts_with_area",
        lambda hass, area_id: {"script.old_room"},
    )
    return device_registry, entity_registry, notifications, dismissed


@pytest.fixture(autouse=True)
def _reset_fake_store():
    FakeStore.data = None
    FakeStore.engineering_generation = "generation-a"
    FakeStore.engineering_provider = None
    FakeStore.fail_save = False
    FakeStore.fail_save_attempt = None
    FakeStore.save_attempts = 0


def _device(device_id, identifier, name="Device", area_id=None):
    return SimpleNamespace(
        id=device_id,
        identifiers={(DOMAIN, identifier)},
        config_entries={"entry-id"},
        model="Switch",
        name=name,
        name_by_user=None,
        area_id=area_id,
    )


def _entity(entity_id, device_id, area_id=None):
    return SimpleNamespace(
        entity_id=entity_id,
        unique_id=entity_id,
        config_entry_id="entry-id",
        platform=DOMAIN,
        device_id=device_id,
        area_id=area_id,
    )


def _config_entry(
    auto_cleanup=True,
    grace=2,
    mode="observations",
    hours=24,
):
    return SimpleNamespace(
        entry_id="entry-id",
        options={
            "stale_device_auto_cleanup": auto_cleanup,
            "stale_device_grace_mode": mode,
            "stale_device_grace_observations": grace,
            "stale_device_grace_hours": hours,
        },
    )


def _lox_config():
    return {
        "msInfo": {"serialNr": "serial"},
        "rooms": {"room-id": {"name": "Wohnzimmer"}},
        "controls": {"active-uuid": {"name": "Active", "room": "room-id"}},
    }


def test_cleanup_requires_two_consecutive_successful_structure_loads(monkeypatch):
    """A single missing observation must not delete a registry device."""
    FakeStore.data = None
    stale = _device("stale-device", "stale-uuid", "Old switch")
    entity = _entity("switch.old", "stale-device")
    device_registry, entity_registry, notifications, _ = _install_registry_fakes(monkeypatch, [stale], [entity])

    first = asyncio.run(async_run_registry_maintenance(object(), _config_entry(), _lox_config()))

    assert first.removed == ()
    assert first.pending[0].observations == 1
    assert device_registry.removed == []
    assert "1/2" in notifications[-1][0]

    same_generation = asyncio.run(async_run_registry_maintenance(object(), _config_entry(), _lox_config()))
    assert same_generation.pending[0].observations == 1
    assert device_registry.removed == []

    FakeStore.engineering_generation = "generation-b"
    second = asyncio.run(async_run_registry_maintenance(object(), _config_entry(), _lox_config()))

    assert second.pending == ()
    assert second.removed[0].identifier == "stale-uuid"
    assert device_registry.removed == ["stale-device"]
    assert entity_registry.removed == ["switch.old"]
    assert "Removed stale registry devices" in notifications[-1][0]


def test_audit_only_mode_never_removes_confirmed_devices(monkeypatch):
    """Audit mode reports stale devices without mutating registries."""
    FakeStore.data = {"missing_observations": {"stale-uuid": 7}, "loxone_rooms": []}
    stale = _device("stale-device", "stale-uuid")
    device_registry, entity_registry, notifications, _ = _install_registry_fakes(
        monkeypatch, [stale], [_entity("switch.old", "stale-device")]
    )

    result = asyncio.run(async_run_registry_maintenance(object(), _config_entry(auto_cleanup=False), _lox_config()))

    assert result.audit_only is True
    assert result.pending[0].observations == 2
    assert result.removed == ()
    assert device_registry.removed == []
    assert entity_registry.removed == []
    assert "audit only" in notifications[-1][0]


def test_confirmed_engineering_device_is_not_reported_as_stale(monkeypatch):
    """A persisted engineering UUID supplements the public structure safely."""
    FakeStore.data = None
    engineering = _device("engineering-device", "engineering-uuid")
    _, _, notifications, dismissed = _install_registry_fakes(monkeypatch, [engineering])

    async def load_engineering_metadata(hass, entry_id):
        del hass, entry_id
        return EngineeringRegistryMetadata(
            frozenset({"engineering-uuid"}),
            frozenset({"Keller"}),
            {},
            FakeStore.engineering_generation,
        )

    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.async_load_engineering_registry_metadata",
        load_engineering_metadata,
    )

    result = asyncio.run(async_run_registry_maintenance(object(), _config_entry(), _lox_config()))

    assert result.pending == ()
    assert result.removed == ()
    assert notifications == []
    assert dismissed


def test_cleanup_defaults_to_audit_only_when_option_is_absent(monkeypatch):
    """Existing entries without the new option must never delete by default."""
    FakeStore.data = {
        "missing_observations": {"stale-uuid": 7},
        "loxone_rooms": [],
    }
    stale = _device("stale-device", "stale-uuid")
    device_registry, entity_registry, notifications, _ = _install_registry_fakes(
        monkeypatch, [stale], [_entity("switch.old", "stale-device")]
    )

    result = asyncio.run(
        async_run_registry_maintenance(object(), SimpleNamespace(entry_id="entry-id", options={}), _lox_config())
    )

    assert result.audit_only is True
    assert result.removed == ()
    assert device_registry.removed == []
    assert entity_registry.removed == []
    assert "automatic deletion is disabled" in notifications[-1][0]


def test_time_mode_waits_for_elapsed_time_and_a_new_structure_load(monkeypatch):
    """Time mode removes only after time elapsed and another successful load."""
    FakeStore.data = None
    now = 1_000.0
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance._utc_timestamp",
        lambda: now,
    )
    stale = _device("stale-device", "stale-uuid")
    device_registry, _, notifications, _ = _install_registry_fakes(monkeypatch, [stale])
    config_entry = _config_entry(mode="time", hours=1)

    first = asyncio.run(async_run_registry_maintenance(object(), config_entry, _lox_config()))
    assert first.pending[0].missing_since == 1_000.0

    now = 4_599.0
    second = asyncio.run(async_run_registry_maintenance(object(), config_entry, _lox_config()))
    assert second.removed == ()
    assert device_registry.removed == []

    now = 4_600.0
    third = asyncio.run(async_run_registry_maintenance(object(), config_entry, _lox_config()))
    assert third.removed[0].identifier == "stale-uuid"
    assert device_registry.removed == ["stale-device"]
    assert "1 elapsed hours" in notifications[-1][0]


def test_combined_mode_requires_observations_and_elapsed_time(monkeypatch):
    """Combined mode uses an AND rule for both independently tracked limits."""
    FakeStore.data = None
    now = 1_000.0
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance._utc_timestamp",
        lambda: now,
    )
    stale = _device("stale-device", "stale-uuid")
    device_registry, _, _, _ = _install_registry_fakes(monkeypatch, [stale])
    config_entry = _config_entry(mode="combined", grace=2, hours=1)

    asyncio.run(async_run_registry_maintenance(object(), config_entry, _lox_config()))
    FakeStore.engineering_generation = "generation-b"
    now = 2_000.0
    second = asyncio.run(async_run_registry_maintenance(object(), config_entry, _lox_config()))
    assert second.pending[0].observations == 2
    assert device_registry.removed == []

    now = 4_600.0
    third = asyncio.run(async_run_registry_maintenance(object(), config_entry, _lox_config()))
    assert third.removed[0].identifier == "stale-uuid"
    assert device_registry.removed == ["stale-device"]


def test_pending_engineering_generation_does_not_advance_grace(monkeypatch):
    """A snapshot whose registry phase is pending must not count as an observation."""
    stale = _device("stale-device", "stale-uuid")
    device_registry, _, notifications, dismissed = _install_registry_fakes(monkeypatch, [stale])

    async def load_pending_metadata(hass, entry_id):
        del hass, entry_id
        return EngineeringRegistryMetadata.empty()

    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.async_load_engineering_registry_metadata",
        load_pending_metadata,
    )

    result = asyncio.run(async_run_registry_maintenance(object(), _config_entry(), _lox_config()))

    assert result.skipped is False
    assert result.audit_only is True
    assert result.pending[0].observations == 0
    assert device_registry.removed == []
    assert FakeStore.data["missing_observations"] == {"stale-uuid": 0}
    assert notifications
    assert dismissed == []


def test_counter_store_failure_prevents_cleanup(monkeypatch):
    """Deletion before the counted-token save would make replay double-count or lose work."""
    stale = _device("stale-device", "stale-uuid")
    device_registry, _, _, _ = _install_registry_fakes(monkeypatch, [stale])
    FakeStore.fail_save = True

    with pytest.raises(RuntimeError, match="injected maintenance store failure"):
        asyncio.run(
            async_run_registry_maintenance(
                object(),
                _config_entry(auto_cleanup=True, grace=1),
                _lox_config(),
            )
        )

    assert device_registry.removed == []
    assert FakeStore.data is None


def test_cleanup_replays_after_counted_token_was_saved(monkeypatch):
    """A cleanup failure after the atomic counter save must retry without another count."""
    stale = _device("stale-device", "stale-uuid")
    device_registry, _, _, _ = _install_registry_fakes(monkeypatch, [stale])
    attempts = 0

    def flaky_cleanup(hass, config_entry, lox_config, identifiers_to_remove):
        nonlocal attempts
        del hass, config_entry, lox_config
        attempts += 1
        assert FakeStore.data["last_counted_engineering_generation"] == "generation-a"
        assert FakeStore.data["missing_observations"] == {"stale-uuid": 1}
        if attempts == 1:
            raise RuntimeError("injected cleanup failure")
        assert identifiers_to_remove == {"stale-uuid"}
        device_registry.async_remove_device("stale-device")
        return (1, 0)

    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.async_cleanup_stale_devices",
        flaky_cleanup,
    )

    with pytest.raises(RuntimeError, match="injected cleanup failure"):
        asyncio.run(
            async_run_registry_maintenance(
                object(),
                _config_entry(auto_cleanup=True, grace=1),
                _lox_config(),
            )
        )
    assert FakeStore.data["missing_observations"] == {"stale-uuid": 1}

    result = asyncio.run(
        async_run_registry_maintenance(
            object(),
            _config_entry(auto_cleanup=True, grace=1),
            _lox_config(),
        )
    )
    assert result.removed[0].observations == 1
    assert attempts == 2


def test_successful_cleanup_retains_replay_authorization_until_uuid_is_active(
    monkeypatch,
):
    """A tombstone survives absence and is pruned only by authoritative reappearance."""
    stale = _device("stale-device", "stale-uuid")
    device_registry, _, _, _ = _install_registry_fakes(monkeypatch, [stale])

    asyncio.run(
        async_run_registry_maintenance(
            object(),
            _config_entry(auto_cleanup=True, grace=1),
            _lox_config(),
        )
    )
    assert device_registry.removed == ["stale-device"]
    assert FakeStore.data["missing_observations"] == {"stale-uuid": 1}
    assert FakeStore.data["cleanup_authorizations"] == ["stale-uuid"]

    result = asyncio.run(
        async_run_registry_maintenance(
            object(),
            _config_entry(auto_cleanup=True, grace=1),
            {
                **_lox_config(),
                "controls": {"stale-uuid": {"name": "Active"}},
            },
        )
    )

    assert result.pending == ()
    assert result.removed == ()
    assert FakeStore.data["missing_observations"] == {}
    assert FakeStore.data["cleanup_authorizations"] == []


@pytest.mark.parametrize("mode", ("observations", "time", "combined"))
@pytest.mark.parametrize(
    ("root_identifier", "serial"),
    (("serial", "serial"), ("entry-id", None)),
)
def test_authoritative_miniserver_root_is_never_counted_or_deleted(
    monkeypatch,
    mode,
    root_identifier,
    serial,
):
    """Serial and entry-ID provider roots stay protected in every grace mode."""
    now = 5_000.0
    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance._utc_timestamp",
        lambda: now,
    )
    FakeStore.engineering_provider = root_identifier
    FakeStore.data = {
        "missing_observations": {root_identifier: 9, "stale-uuid": 1},
        "missing_since": {root_identifier: 1_000.0, "stale-uuid": 1_000.0},
        "loxone_rooms": [],
        "last_counted_engineering_generation": "generation-a",
    }
    root = _device("root-device", root_identifier, "Miniserver")
    stale = _device("stale-device", "stale-uuid")
    root_entity = _entity("sensor.root", "root-device")
    stale_entity = _entity("switch.stale", "stale-device")
    device_registry, entity_registry, _, _ = _install_registry_fakes(
        monkeypatch,
        [root, stale],
        [root_entity, stale_entity],
    )
    lox_config = _lox_config()
    lox_config["msInfo"] = {} if serial is None else {"serialNr": serial}

    result = asyncio.run(
        async_run_registry_maintenance(
            object(),
            _config_entry(auto_cleanup=True, grace=1, mode=mode, hours=1),
            lox_config,
        )
    )

    assert [item.identifier for item in result.removed] == ["stale-uuid"]
    assert device_registry.removed == ["stale-device"]
    assert entity_registry.removed == ["switch.stale"]
    assert root_identifier not in FakeStore.data["missing_observations"]
    assert root_identifier not in FakeStore.data["missing_since"]


def test_cleanup_authorization_replays_after_old_ha_registry_is_restored(monkeypatch):
    """A delayed HA registry save cannot erase an already-authorized decision."""
    stale = _device("stale-device", "stale-uuid")
    first_registry, _, _, _ = _install_registry_fakes(
        monkeypatch,
        [stale],
        [_entity("switch.stale", "stale-device")],
    )
    config_entry = _config_entry(auto_cleanup=True, grace=1)

    first = asyncio.run(async_run_registry_maintenance(object(), config_entry, _lox_config()))
    assert first_registry.removed == ["stale-device"]
    assert first.removed[0].observations == 1
    assert FakeStore.data["cleanup_authorizations"] == ["stale-uuid"]

    restored_registry, restored_entities, _, _ = _install_registry_fakes(
        monkeypatch,
        [_device("stale-device", "stale-uuid")],
        [_entity("switch.stale", "stale-device")],
    )
    replay = asyncio.run(async_run_registry_maintenance(object(), config_entry, _lox_config()))

    assert replay.removed[0].observations == 1
    assert restored_registry.removed == ["stale-device"]
    assert restored_entities.removed == ["switch.stale"]
    assert FakeStore.data["missing_observations"] == {"stale-uuid": 1}
    assert FakeStore.data["cleanup_authorizations"] == ["stale-uuid"]


def test_cleanup_authorization_replays_partial_entity_deletion(monkeypatch):
    """A crash between entity and device deletion resumes from the tombstone."""
    stale = _device("stale-device", "stale-uuid")
    entity = _entity("switch.stale", "stale-device")
    device_registry, entity_registry, _, _ = _install_registry_fakes(
        monkeypatch,
        [stale],
        [entity],
    )
    attempts = 0

    def partial_cleanup(hass, config_entry, lox_config, identifiers_to_remove):
        nonlocal attempts
        del hass, config_entry, lox_config
        attempts += 1
        assert identifiers_to_remove == {"stale-uuid"}
        if attempts == 1:
            entity_registry.async_remove("switch.stale")
            raise RuntimeError("injected partial cleanup failure")
        device_registry.async_remove_device("stale-device")
        return (1, 0)

    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.async_cleanup_stale_devices",
        partial_cleanup,
    )

    with pytest.raises(RuntimeError, match="injected partial cleanup failure"):
        asyncio.run(
            async_run_registry_maintenance(
                object(),
                _config_entry(auto_cleanup=True, grace=1),
                _lox_config(),
            )
        )
    assert FakeStore.data["cleanup_authorizations"] == ["stale-uuid"]
    assert entity_registry.removed == ["switch.stale"]

    replay = asyncio.run(
        async_run_registry_maintenance(
            object(),
            _config_entry(auto_cleanup=True, grace=1),
            _lox_config(),
        )
    )
    assert replay.removed[0].identifier == "stale-uuid"
    assert device_registry.removed == ["stale-device"]
    assert attempts == 2


def test_no_engineering_snapshot_keeps_public_registry_audit_read_only(monkeypatch):
    """The unchanged startup remains useful without inventing a generation token."""
    FakeStore.data = {
        "missing_observations": {"stale-uuid": 1},
        "missing_since": {"stale-uuid": 1_000.0},
        "loxone_rooms": ["Old Room"],
    }
    stale = _device("stale-device", "stale-uuid")
    old_area = SimpleNamespace(id="old-area", name="Old Room")
    device_registry, _, notifications, _ = _install_registry_fakes(
        monkeypatch,
        [stale],
        areas=[old_area],
    )

    async def no_engineering_snapshot(hass, entry_id):
        del hass, entry_id
        return EngineeringRegistryMetadata.empty()

    monkeypatch.setattr(
        "custom_components.loxone.registry_maintenance.async_load_engineering_registry_metadata",
        no_engineering_snapshot,
    )

    result = asyncio.run(
        async_run_registry_maintenance(
            object(),
            _config_entry(auto_cleanup=True, grace=1),
            _lox_config(),
        )
    )

    assert result.skipped is False
    assert result.audit_only is True
    assert result.pending[0].observations == 1
    assert result.removed == ()
    assert result.orphan_rooms[0].name == "Old Room"
    assert device_registry.removed == []
    assert FakeStore.data["missing_observations"] == {"stale-uuid": 1}
    assert "audit only" in notifications[-1][0]


def test_empty_structure_skips_storage_notifications_and_cleanup(monkeypatch):
    """An empty structure is never considered an authoritative deletion."""
    FakeStore.data = {"missing_observations": {"stale-uuid": 1}}
    stale = _device("stale-device", "stale-uuid")
    device_registry, _, notifications, dismissed = _install_registry_fakes(monkeypatch, [stale])

    result = asyncio.run(async_run_registry_maintenance(object(), _config_entry(), {"controls": {}}))

    assert result.skipped is True
    assert device_registry.removed == []
    assert notifications == []
    assert dismissed == []
    assert FakeStore.data == {"missing_observations": {"stale-uuid": 1}}


def test_reappearing_uuid_clears_its_missing_observation(monkeypatch):
    """A control that reappears must leave the cleanup queue immediately."""
    FakeStore.data = {
        "missing_observations": {"active-uuid": 1},
        "loxone_rooms": ["Wohnzimmer"],
    }
    active = _device("active-device", "active-uuid")
    device_registry, _, notifications, dismissed = _install_registry_fakes(monkeypatch, [active])

    result = asyncio.run(async_run_registry_maintenance(object(), _config_entry(), _lox_config()))

    assert result.pending == ()
    assert result.removed == ()
    assert device_registry.removed == []
    assert FakeStore.data["missing_observations"] == {}
    assert FakeStore.data["missing_since"] == {}
    assert notifications == []
    assert dismissed == ["loxone_registry_maintenance_entry-id"]


def test_removed_empty_loxone_room_is_reported_but_not_deleted(monkeypatch):
    """Former empty Loxone areas are audit findings, never auto-deleted."""
    FakeStore.data = {
        "missing_observations": {},
        "loxone_rooms": ["Alter Raum", "Wohnzimmer"],
    }
    area = SimpleNamespace(id="old-area", name="Alter Raum")
    _, _, notifications, _ = _install_registry_fakes(monkeypatch, [], areas=[area])

    result = asyncio.run(async_run_registry_maintenance(object(), _config_entry(), _lox_config()))

    assert result.orphan_rooms[0].name == "Alter Raum"
    assert result.orphan_rooms[0].automations == ("automation.old_room",)
    assert result.orphan_rooms[0].scripts == ("script.old_room",)
    assert "Alter Raum" in notifications[-1][0]
    assert "automation.old_room" in notifications[-1][0]
    assert "script.old_room" in notifications[-1][0]
    assert "were not deleted" in notifications[-1][0]
