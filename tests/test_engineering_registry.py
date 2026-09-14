"""Tests for idempotent engineering topology registry synchronization."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import ClassVar

import pytest

import custom_components.loxone.engineering_registry as engineering_registry
from custom_components.loxone.const import DOMAIN
from custom_components.loxone.engineering_capabilities import (
    resolve_engineering_capabilities,
)
from custom_components.loxone.engineering_entities import (
    async_load_engineering_registry_metadata,
    async_store_engineering_registry_metadata,
    build_engineering_entity_specs,
    build_engineering_sensor_specs,
)
from custom_components.loxone.engineering_registry import (
    EngineeringRegistryMetadata,
    async_apply_engineering_registry_plan,
    async_filter_entity_identity_conflicts,
    async_plan_engineering_registry_sync,
    async_sync_engineering_devices,
)
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from custom_components.loxone.engineering_snapshot import (
    EngineeringSnapshot,
    StoredEngineeringState,
    engineering_configuration_revision_id,
    engineering_generation_id,
    engineering_safe_content_digest,
)
from custom_components.loxone.engineering_topology import resolve_engineering_topology
from tests.engineering_fixtures import (
    element,
    inventory_of,
    make_snapshot,
    numeric_binding,
    reference_link_inventory,
    source,
    text_binding,
)

_UNDEFINED = object()


def test_all_devices_uses_public_registry_iteration():
    """A registry mapping lookup would reintroduce HA's deprecated API warning."""
    expected = (SimpleNamespace(id="one"), SimpleNamespace(id="two"))

    class PublicItems:
        def __iter__(self):
            return iter(expected)

        def values(self):
            raise AssertionError("deprecated mapping accessed")

    class PublicRegistry:
        devices = PublicItems()

    assert engineering_registry._all_devices(PublicRegistry()) == expected


def _seed_managed_baseline(area_id, identifiers=("serial-a:device",)):
    """Give managed-transition tests explicit acknowledged process evidence."""
    FakeIntentStore.data = {
        "managed_baselines": [
            {"identifier": identifier, "area_id": area_id, "process_token": "process-a"} for identifier in identifiers
        ]
    }


def _area_recovery_case(registries, monkeypatch, room="Workshop"):
    """Build an acknowledged current-process managed transition."""
    office = registries.areas.async_get_or_create("Office")
    registries.devices.add("serial-a:device", "entry-a", area_id=office.id, name="ST-F07")
    FakeIntentStore.data = {
        "managed_baselines": [{"identifier": "serial-a:device", "area_id": office.id, "process_token": "process-a"}]
    }
    monkeypatch.setattr(engineering_registry, "async_store_engineering_state", lambda hass, state: asyncio.sleep(0))
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07", room=room),
        )
    )
    return office, snapshot, StoredEngineeringState(snapshot=snapshot)


@pytest.mark.parametrize("room", [None, "Workshop"])
@pytest.mark.parametrize("evidence", ["cold", "legacy", "old-plan", "missing-state"])
def test_matching_persisted_area_cannot_authorize_move_or_clear(registries, monkeypatch, room, evidence):
    """Equality with a prior-process or legacy baseline never proves ownership."""
    office, snapshot, committed = _area_recovery_case(registries, monkeypatch, room)
    metadata = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}), frozenset({"Office"}), {"serial-a:device": office.id}
    )

    async def scenario():
        if evidence in {"old-plan", "missing-state"}:
            plan = await async_plan_engineering_registry_sync(registries.hass, "entry-a", snapshot, metadata)
        if evidence in {"legacy", "missing-state"}:
            FakeIntentStore.data = None
        monkeypatch.setattr(engineering_registry, "_AREA_PROCESS_TOKEN", "process-b")
        if evidence not in {"old-plan", "missing-state"}:
            plan = await async_plan_engineering_registry_sync(registries.hass, "entry-a", snapshot, metadata)
        result = await async_apply_engineering_registry_plan(registries.hass, plan, committed_state=committed)
        assert registries.device("serial-a:device").area_id == office.id
        assert "serial-a:device" not in result.metadata.managed_area_ids
        conflicts = await engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a")
        assert len(conflicts) == 1
        assert conflicts[0].reason == "area_assignment_unverified"

    asyncio.run(scenario())


async def _record_override(registries, snapshot, committed):
    """Exercise the real planner and applier to persist one conflict."""
    plan = await async_plan_engineering_registry_sync(
        registries.hass, "entry-a", snapshot, EngineeringRegistryMetadata.empty()
    )
    await async_apply_engineering_registry_plan(registries.hass, plan, committed_state=committed)
    return (await engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))[0]


async def _prepare_resolution_conflict(registries, snapshot, committed, office):
    """Apply once, then create a real user-override conflict at Office."""
    plan = await async_plan_engineering_registry_sync(
        registries.hass,
        "entry-a",
        snapshot,
        EngineeringRegistryMetadata.empty(),
    )
    await async_apply_engineering_registry_plan(
        registries.hass,
        plan,
        committed_state=committed,
    )
    registries.devices.async_update_device(
        registries.device("serial-a:device").id,
        area_id=office.id,
    )
    return await _record_override(registries, snapshot, committed)


@pytest.mark.parametrize("room", ["Workshop", None])
@pytest.mark.parametrize("race", ["none", "move", "clear", "disappear", "before-disappear", "twice"])
def test_apply_resolution_refetches_replacing_device_entries(registries, monkeypatch, race, room):
    """HA replaces entries; normal resolution and race records must use live entries."""
    _, snapshot, committed = _area_recovery_case(registries, monkeypatch, room)
    original_update = registries.devices.async_update_device

    def replacing_update(device_id, **changes):
        registries.devices.devices[device_id] = SimpleNamespace(**vars(registries.devices.devices[device_id]))
        return original_update(device_id, **changes)

    monkeypatch.setattr(registries.devices, "async_update_device", replacing_update)
    device_id = registries.device("serial-a:device").id
    living = registries.areas.async_get_or_create("Living Room")
    replacing_update(device_id, area_id=living.id)

    async def scenario():
        conflict = await _record_override(registries, snapshot, committed)
        changed = registries.areas.async_get_or_create("User Area")

        def callback():
            if race in {"move", "twice"}:
                replacing_update(device_id, area_id=changed.id)
            elif race == "clear":
                replacing_update(device_id, area_id=None)
            else:
                registries.devices.devices.pop(device_id)

        if race != "none":
            FakeIntentStore.save_callbacks = [callback] if race == "before-disappear" else [lambda: None, callback]
        if race == "twice":
            FakeIntentStore.save_callbacks.append(lambda: replacing_update(device_id, area_id=None))
        result = await engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass, "entry-a", conflict.token, "apply_loxone_room"
        )
        conflicts = await engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a")
        if race == "none":
            assert result.resolved
            assert not conflicts
            expected = registries.areas.async_get_area_by_name("Workshop").id if room else None
            assert registries.device("serial-a:device").area_id == expected
            assert FakeIntentStore.data["managed_baselines"][0]["process_token"] == "process-a"
        elif race == "clear" and room is None:
            assert result.resolved
            assert registries.device("serial-a:device").area_id is None
        else:
            assert not result.resolved
            expected = changed.id if race == "move" else None
            assert conflicts[0].current_area_id == expected
            assert result.conflict == conflicts[0]
            assert conflicts[0].token != conflict.token
            assert not FakeIntentStore.data["managed_baselines"]
            assert not FakeIntentStore.data["area_intents"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "unsafe",
    [
        "Office https://example.invalid/path",
        "Office 192.0.2.1",
        "Office 2001:db8::1",
        "Office\nextra",
        "Office user@example.invalid",
    ],
)
def test_conflict_presentation_is_safe_through_journal_and_reload(registries, monkeypatch, unsafe):
    """Persisted HA presentation receives the same address/URL rejection as snapshots."""
    _, snapshot, committed = _area_recovery_case(registries, monkeypatch)
    area = registries.areas.async_get_or_create(unsafe)
    registries.devices.async_update_device(registries.device("serial-a:device").id, area_id=area.id)

    async def scenario():
        conflict = await _record_override(registries, snapshot, committed)
        assert unsafe not in repr(FakeIntentStore.data)
        assert conflict.current_area_name is None
        assert conflict.display_name == "ST-F07"
        assert conflict.desired_area_name == "Workshop"
        raw = FakeIntentStore.data["area_conflicts"][0]
        raw["display_name"] = unsafe
        raw["current_area_name"] = unsafe
        loaded = (await engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))[0]
        assert loaded.display_name is None
        assert loaded.current_area_name is None
        assert loaded.token == conflict.token

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("desired_area_name", "Office 192.0.2.1"),
        ("desired_area_name", "https://example.invalid"),
        ("desired_area_id", []),
        ("desired_area_id", "bad identifier"),
    ],
)
def test_invalid_nonnull_conflict_action_never_becomes_clear(registries, monkeypatch, field, value):
    """Sanitization must not turn malformed non-null action data into a clear choice."""
    _, snapshot, committed = _area_recovery_case(registries, monkeypatch, None)
    living = registries.areas.async_get_or_create("Living Room")
    registries.devices.async_update_device(registries.device("serial-a:device").id, area_id=living.id)

    async def scenario():
        conflict = await _record_override(registries, snapshot, committed)
        FakeIntentStore.data["area_conflicts"][0][field] = value
        result = await engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass, "entry-a", conflict.token, "apply_loxone_room"
        )
        assert not result.resolved
        assert registries.device("serial-a:device").area_id == living.id
        loaded = (await engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))[0]
        kept = await engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass, "entry-a", loaded.token, "keep_ha_room"
        )
        assert kept.resolved

    asyncio.run(scenario())


@pytest.mark.parametrize("room", ["Workshop", None])
def test_keep_retires_old_plan_and_pending_intent(registries, monkeypatch, room):
    """An old allowed plan cannot regain authority after an exact Keep choice."""
    office, snapshot, committed = _area_recovery_case(registries, monkeypatch, room)

    async def scenario():
        old_plan = await async_plan_engineering_registry_sync(
            registries.hass, "entry-a", snapshot, EngineeringRegistryMetadata.empty()
        )
        await async_apply_engineering_registry_plan(registries.hass, old_plan, committed_state=committed)
        registries.devices.async_update_device(registries.device("serial-a:device").id, area_id=office.id)
        conflict = await _record_override(registries, snapshot, committed)
        kept = await engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass, "entry-a", conflict.token, "keep_ha_room"
        )
        assert kept.resolved
        await async_apply_engineering_registry_plan(registries.hass, old_plan, committed_state=committed)
        assert registries.device("serial-a:device").area_id == office.id
        assert FakeIntentStore.data["released_identifiers"] == ["serial-a:device"]
        assert not FakeIntentStore.data["area_intents"]

    asyncio.run(scenario())


def test_keep_after_preexisting_room_change_refreshes_actionable_conflict(
    registries,
    monkeypatch,
):
    """Keep cannot loop forever on a token captured before another HA room move."""
    _, snapshot, committed = _area_recovery_case(registries, monkeypatch)
    living = registries.areas.async_get_or_create("Living Room")

    async def scenario():
        initial_plan = await async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
        await async_apply_engineering_registry_plan(
            registries.hass,
            initial_plan,
            committed_state=committed,
        )
        office = registries.areas.async_get_area_by_name("Office")
        registries.devices.async_update_device(
            registries.device("serial-a:device").id,
            area_id=office.id,
        )
        conflict = await _record_override(registries, snapshot, committed)
        device = registries.device("serial-a:device")
        registries.devices.async_update_device(device.id, area_id=living.id)
        mutations = registries.devices.mutations

        result = await engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass,
            "entry-a",
            conflict.token,
            "keep_ha_room",
        )
        loaded = await engineering_registry.async_load_engineering_area_conflicts(
            registries.hass,
            "entry-a",
        )

        assert not result.resolved
        assert result.reason == "stale_conflict"
        assert result.conflict == loaded[0]
        assert result.conflict.token != conflict.token
        assert result.conflict.current_area_id == living.id
        assert result.conflict.current_area_name == "Living Room"
        assert registries.devices.mutations == mutations

    asyncio.run(scenario())


@pytest.mark.parametrize("room", ["Workshop", None])
def test_keep_write_serializes_old_refresh_and_rechecks_user_change(registries, monkeypatch, room):
    """A queued stale refresh cannot overwrite Keep or hide a new HA clear."""
    office, snapshot, committed = _area_recovery_case(registries, monkeypatch, room)

    async def scenario():
        old_plan = await async_plan_engineering_registry_sync(
            registries.hass, "entry-a", snapshot, EngineeringRegistryMetadata.empty()
        )
        await async_apply_engineering_registry_plan(registries.hass, old_plan, committed_state=committed)
        registries.devices.async_update_device(registries.device("serial-a:device").id, area_id=office.id)
        conflict = await _record_override(registries, snapshot, committed)
        entered, resume = asyncio.Event(), asyncio.Event()
        original_save = FakeIntentStore.async_save_acknowledged

        async def held_save(store, data):
            await original_save(store, data)
            if data.get("released_identifiers") and not entered.is_set():
                entered.set()
                await resume.wait()

        monkeypatch.setattr(FakeIntentStore, "async_save_acknowledged", held_save)
        keep = asyncio.create_task(
            engineering_registry.async_resolve_engineering_area_conflict(
                registries.hass, "entry-a", conflict.token, "keep_ha_room"
            )
        )
        await entered.wait()
        refresh = asyncio.create_task(
            async_apply_engineering_registry_plan(registries.hass, old_plan, committed_state=committed)
        )
        await asyncio.sleep(0)
        assert registries.device("serial-a:device").area_id == office.id
        registries.devices.async_update_device(registries.device("serial-a:device").id, area_id=None)
        resume.set()
        result = await keep
        await refresh
        assert not result.resolved
        assert result.conflict.current_area_id is None
        assert result.conflict.token != conflict.token
        assert registries.device("serial-a:device").area_id is None
        assert FakeIntentStore.data["released_identifiers"] == ["serial-a:device"]
        conflicts = await engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a")
        assert conflicts == (result.conflict,)

    asyncio.run(scenario())


class FakeIntentStore:
    """Cold-restart fake for the acknowledged registry intent journal."""

    data: dict[str, object] | None = None
    fail_save = False
    save_callbacks: ClassVar[list[Callable[[], None]]] = []
    save_count = 0

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def async_load(self):
        return deepcopy(self.__class__.data)

    async def async_save_acknowledged(self, data, *, before_commit=None):
        if self.__class__.fail_save:
            raise RuntimeError("injected registry intent store failure")
        if before_commit is not None:
            before_commit()
        self.__class__.data = deepcopy(data)
        self.__class__.save_count += 1
        if self.__class__.save_callbacks:
            self.__class__.save_callbacks.pop(0)()


class FakeLegacyMetadataStore:
    """Pre-Task-5 metadata store fake used by the unchanged coordinator path."""

    data: dict[str, object] | None = None

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def async_load(self):
        return deepcopy(self.__class__.data)

    async def async_save(self, data):
        self.__class__.data = deepcopy(data)


class FakeAreaRegistry:
    """In-memory area registry with the installed HA call signatures."""

    def __init__(self) -> None:
        self.areas: dict[str, SimpleNamespace] = {}
        self.mutations = 0

    def async_get_area_by_name(self, name: str):
        return next((area for area in self.areas.values() if area.name == name), None)

    def async_get_or_create(self, name: str):
        if area := self.async_get_area_by_name(name):
            return area
        self.mutations += 1
        area_id = f"area-{len(self.areas) + 1}"
        area = SimpleNamespace(id=area_id, name=name)
        self.areas[area_id] = area
        return area


class DeviceEntries(dict[str, SimpleNamespace]):
    """Match HA's iterable device-entry collection while retaining test lookups."""

    def __iter__(self):
        return iter(self.values())


class FakeDeviceRegistry:
    """In-memory device registry with scoped identifier lookup."""

    def __init__(self, areas: FakeAreaRegistry) -> None:
        self._areas = areas
        self.devices: DeviceEntries = DeviceEntries()
        self.mutations = 0
        self.fail_mutation_number: int | None = None

    def _mutate(self) -> None:
        self.mutations += 1
        if self.fail_mutation_number == self.mutations:
            raise RuntimeError("injected registry failure")

    def add(
        self,
        identifier: str,
        entry_id: str,
        *,
        area_id: str | None = None,
        name: str | None = None,
        model: str | None = None,
        via_device_id: str | None = None,
    ) -> SimpleNamespace:
        device_id = f"device-{len(self.devices) + 1}"
        device = SimpleNamespace(
            id=device_id,
            identifiers={(DOMAIN, identifier)},
            config_entries={entry_id},
            area_id=area_id,
            name=name,
            name_by_user=None,
            manufacturer="Loxone",
            model=model,
            via_device_id=via_device_id,
        )
        self.devices[device_id] = device
        return device

    def async_get_device_by_identifier(self, identifier: tuple[str, str], config_entry_id: str):
        return next(
            (
                device
                for device in self.devices.values()
                if identifier in device.identifiers and config_entry_id in device.config_entries
            ),
            None,
        )

    def async_get_or_create(
        self,
        *,
        config_entry_id: str,
        identifiers: set[tuple[str, str]],
        name: str | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        suggested_area: str | None = None,
    ):
        identifier = next(iter(identifiers))
        if device := self.async_get_device_by_identifier(identifier, config_entry_id):
            return device
        self._mutate()
        area_id = None
        if suggested_area:
            area_id = self._areas.async_get_or_create(suggested_area).id
        return self.add(
            identifier[1],
            config_entry_id,
            area_id=area_id,
            name=name,
            model=model,
        )

    def async_update_device(
        self,
        device_id: str,
        *,
        area_id=_UNDEFINED,
        manufacturer=_UNDEFINED,
        model=_UNDEFINED,
        name=_UNDEFINED,
        via_device_id=_UNDEFINED,
    ):
        device = self.devices[device_id]
        changes = {
            "area_id": area_id,
            "manufacturer": manufacturer,
            "model": model,
            "name": name,
            "via_device_id": via_device_id,
        }
        actual = {key: value for key, value in changes.items() if value is not _UNDEFINED}
        if not actual or all(getattr(device, key) == value for key, value in actual.items()):
            return device
        self._mutate()
        for key, value in actual.items():
            setattr(device, key, value)
        return device


class FakeEntityRegistry:
    """In-memory global entity registry independent of loaded coordinators."""

    def __init__(self) -> None:
        self.entities: dict[str, SimpleNamespace] = {}
        self.mutations = 0

    def add(
        self,
        *,
        domain: str,
        platform: str,
        unique_id: str,
        config_entry_id: str,
        device_id: str | None,
        original_name: str | None = None,
        area_id: str | None = None,
    ) -> SimpleNamespace:
        entity_id = f"{domain}.entity_{len(self.entities) + 1}"
        entry = SimpleNamespace(
            entity_id=entity_id,
            domain=domain,
            platform=platform,
            unique_id=unique_id,
            config_entry_id=config_entry_id,
            device_id=device_id,
            original_name=original_name,
            area_id=area_id,
        )
        self.entities[entity_id] = entry
        return entry

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        return next(
            (
                entry.entity_id
                for entry in self.entities.values()
                if entry.domain == domain and entry.platform == platform and entry.unique_id == unique_id
            ),
            None,
        )

    def async_get(self, entity_id: str):
        return self.entities.get(entity_id)

    def async_update_entity(
        self,
        entity_id: str,
        *,
        config_entry_id=_UNDEFINED,
        device_id=_UNDEFINED,
        original_name=_UNDEFINED,
    ):
        entry = self.entities[entity_id]
        changes = {
            "config_entry_id": config_entry_id,
            "device_id": device_id,
            "original_name": original_name,
        }
        actual = {key: value for key, value in changes.items() if value is not _UNDEFINED}
        if actual and any(getattr(entry, key) != value for key, value in actual.items()):
            self.mutations += 1
            for key, value in actual.items():
                setattr(entry, key, value)
        return entry


class RegistryHarness:
    """One source-scoped registry test harness."""

    def __init__(self) -> None:
        self.areas = FakeAreaRegistry()
        self.devices = FakeDeviceRegistry(self.areas)
        self.entities = FakeEntityRegistry()
        self.hass = SimpleNamespace(data={})
        self.miniserver = self.devices.add("serial-a", "entry-a", name="Miniserver", model="Miniserver")

    @property
    def mutations(self) -> int:
        return self.areas.mutations + self.devices.mutations + self.entities.mutations

    def device(self, identifier: str, entry_id: str = "entry-a"):
        return self.devices.async_get_device_by_identifier((DOMAIN, identifier), entry_id)


@pytest.fixture
def registries(monkeypatch) -> RegistryHarness:
    FakeIntentStore.data = None
    FakeIntentStore.fail_save = False
    FakeIntentStore.save_callbacks = []
    FakeIntentStore.save_count = 0
    monkeypatch.setattr(
        engineering_registry,
        "_AREA_PROCESS_TOKEN",
        "process-a",
        raising=False,
    )
    harness = RegistryHarness()
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.ar.async_get",
        lambda hass: harness.areas,
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.dr.async_get",
        lambda hass: harness.devices,
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.er.async_get",
        lambda hass: harness.entities,
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.EngineeringRegistryIntentStore",
        FakeIntentStore,
    )
    return harness


def _snapshot_for(
    entry_id: str,
    serial: str | None,
    *,
    inventory=None,
    runtime: EngineeringRuntimeInventory | None = None,
) -> EngineeringSnapshot:
    """Build a valid snapshot for claimant and provider-isolation tests."""
    raw = inventory or inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("global-states", "GlobalStates", room=None),
        element(
            "shared-channel",
            "SysVar",
            parent_uuid="global-states",
            io_name="SYS1",
        ),
    )
    context = replace(source(entry_id, serial or "temporary"), serial_number=serial)
    resolved = resolve_engineering_topology(raw, context)
    bindings = runtime or EngineeringRuntimeInventory(bindings=(numeric_binding("shared-channel", 1.0, "SysVar"),))
    rows = resolve_engineering_capabilities(resolved, bindings)
    return EngineeringSnapshot(
        source=context,
        nodes=resolved.nodes,
        rows=rows,
        configuration_revision_id=engineering_configuration_revision_id(context),
        safe_content_digest=engineering_safe_content_digest(
            context,
            resolved.nodes,
            rows,
        ),
        read_sequence=1,
        generation_id=engineering_generation_id(
            context,
            resolved.nodes,
            rows,
            read_sequence=1,
        ),
        captured_at=make_snapshot().captured_at,
    )


def test_registry_creates_entityless_tree_device_with_full_via_chain(registries):
    """Dropping entityless physical devices would hide the configured Tree path."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("tree", "LoxTree", parent_uuid="ms", title="Tree", room=None),
            element("branch", "TreeCaption", parent_uuid="tree", title="Branch A"),
            element("nfc", "NfcCodeTouch", parent_uuid="branch", title="NFC Code Touch"),
        )
    )

    result = asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    tree = registries.device("serial-a:tree")
    endpoint = registries.device("serial-a:nfc")
    assert tree.via_device_id == registries.miniserver.id
    assert endpoint.via_device_id == tree.id
    assert registries.device("serial-a:branch") is None
    assert result.created_identifiers == ("serial-a:tree", "serial-a:nfc")


def test_registry_builds_link_bridge_endpoint_chains(registries):
    """Flattening every engineering node to the Miniserver would lose Link ancestry."""
    asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            make_snapshot(inventory=reference_link_inventory()),
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert registries.device("serial-a:air-extension").via_device_id == registries.device("serial-a:link").id
    assert registries.device("serial-a:air-device").via_device_id == registries.device("serial-a:air-extension").id
    assert registries.device("serial-a:wire-sensor").via_device_id == registries.device("serial-a:wire-extension").id


def test_service_module_is_created_only_when_it_has_supported_channels(registries):
    """Creating empty service modules would add registry clutter without a capability."""
    runtime = EngineeringRuntimeInventory(bindings=(numeric_binding("weather-value", 18.5, "WeatherData"),))
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("weather-server", "WeatherServer", room=None),
            element("weather-value", "WeatherData", parent_uuid="weather-server", io_name="WDC1"),
            element("empty-service", "GlobalStates", room=None),
        ),
        runtime=runtime,
    )

    asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert registries.device("serial-a:weather-server") is not None
    assert registries.device("serial-a:empty-service") is None


def test_registry_planning_is_mutation_free(registries):
    """A failed candidate commit must not leave registry or area mutations behind."""
    before = registries.mutations

    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            make_snapshot(inventory=reference_link_inventory()),
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert plan.device_operations
    assert registries.mutations == before
    assert registries.device("serial-a:link") is None


def test_loxone_area_move_updates_only_integration_managed_assignment(registries):
    """A Loxone room move must preserve an explicit Home Assistant area override."""
    old_area = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(old_area.id)
    managed = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=old_area.id,
        name="ST-F07",
        model="TreeDevice",
    )
    previous = EngineeringRegistryMetadata(
        active_device_identifiers=frozenset({"serial-a:device"}),
        room_names=frozenset({"Office"}),
        managed_area_ids={"serial-a:device": old_area.id},
        applied_generation=None,
    )
    moved = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07", room="Workshop"),
        )
    )

    first = asyncio.run(async_sync_engineering_devices(registries.hass, "entry-a", moved, previous))
    workshop = registries.areas.async_get_area_by_name("Workshop")
    assert managed.area_id == workshop.id
    assert first.metadata.managed_area_ids["serial-a:device"] == workshop.id

    user_area = registries.areas.async_get_or_create("Living Room")
    managed.area_id = user_area.id
    second = asyncio.run(async_sync_engineering_devices(registries.hass, "entry-a", moved, previous))
    assert managed.area_id == user_area.id
    assert "serial-a:device" not in second.metadata.managed_area_ids


def test_global_entity_collision_rejects_unloaded_other_owner(registries):
    """An unloaded config entry's persisted entity must never be stolen."""
    snapshot = make_snapshot()
    specs = build_engineering_entity_specs(snapshot.rows, None)
    registries.entities.add(
        domain="sensor",
        platform=DOMAIN,
        unique_id=specs[0].unique_id,
        config_entry_id="entry-b",
        device_id="owner-device",
    )

    accepted, rejected = asyncio.run(async_filter_entity_identity_conflicts(registries.hass, "entry-a", specs))

    assert specs[0] not in accepted
    assert rejected[0].reason == "entity_unique_id_owned_by_other_entry"
    assert registries.entities.async_get(rejected[0].entity_id).device_id == "owner-device"


def test_single_persisted_legacy_claimant_is_reassociated(registries):
    """Failing to migrate a unique legacy pseudo-device would retain the flat topology."""
    snapshot = make_snapshot()
    specs = build_engineering_entity_specs(snapshot.rows, None)
    spec = specs[0]
    legacy = registries.devices.add(spec.unique_id, "entry-a", name=spec.name, model="Engineering device")
    entity = registries.entities.add(
        domain=spec.platform,
        platform=DOMAIN,
        unique_id=spec.unique_id,
        config_entry_id="entry-a",
        device_id=legacy.id,
    )

    result = asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert entity.device_id == registries.device(spec.owner_identifier).id
    assert registries.device(spec.unique_id) is legacy
    assert result.migrated_entities == 1


def test_ambiguous_persisted_legacy_device_is_not_reassociated(registries):
    """Multiple persisted claimants must leave the legacy association untouched."""
    snapshot = make_snapshot()
    spec = build_engineering_entity_specs(snapshot.rows, None)[0]
    legacy_a = registries.devices.add(spec.unique_id, "entry-a", name=spec.name)
    registries.devices.add(spec.unique_id, "entry-b", name=spec.name)
    entity = registries.entities.add(
        domain=spec.platform,
        platform=DOMAIN,
        unique_id=spec.unique_id,
        config_entry_id="entry-a",
        device_id=legacy_a.id,
    )

    result = asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert entity.device_id == legacy_a.id
    assert result.migrated_entities == 0
    assert result.ambiguous_legacy_identifiers == (spec.unique_id,)


def test_registry_plan_replays_after_partial_application(registries):
    """A partial two-pass failure must be safely replayable without duplicate devices."""
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            make_snapshot(inventory=reference_link_inventory()),
            EngineeringRegistryMetadata.empty(),
        )
    )
    registries.devices.fail_mutation_number = 3
    with pytest.raises(RuntimeError, match="injected registry failure"):
        asyncio.run(async_apply_engineering_registry_plan(registries.hass, plan))

    registries.devices.fail_mutation_number = None
    first = asyncio.run(async_apply_engineering_registry_plan(registries.hass, plan))
    mutation_count = registries.mutations
    second = asyncio.run(async_apply_engineering_registry_plan(registries.hass, plan))

    assert first.metadata == second.metadata
    assert registries.mutations == mutation_count
    identifiers = [
        identifier
        for device in registries.devices.devices.values()
        for domain, identifier in device.identifiers
        if domain == DOMAIN
    ]
    assert len(identifiers) == len(set(identifiers))


def test_state_metadata_is_persisted_only_after_complete_application(registries, monkeypatch):
    """Persisting the applied cursor before pass two succeeds would skip required replay."""
    snapshot = make_snapshot(inventory=reference_link_inventory())
    state = StoredEngineeringState(snapshot=snapshot)
    saved: list[StoredEngineeringState] = []

    async def save_state(hass, candidate):
        del hass
        saved.append(candidate)

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        save_state,
    )
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )
    registries.devices.fail_mutation_number = 3
    with pytest.raises(RuntimeError):
        asyncio.run(
            async_apply_engineering_registry_plan(
                registries.hass,
                plan,
                committed_state=state,
            )
        )
    assert saved == []

    registries.devices.fail_mutation_number = None
    result = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            plan,
            committed_state=state,
        )
    )
    assert saved[-1] == replace(
        state,
        registry_applied_generation=snapshot.generation_id,
        managed_area_ids=result.metadata.managed_area_ids,
    )


def test_serialless_snapshot_carries_entry_provider_identity(registries):
    """The config-entry fallback is the authoritative root when serial is absent."""
    snapshot = _snapshot_for("entry-a", None)

    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert plan.metadata.provider_identifier == "entry-a"
    assert plan.device_operations[0].identifier == "entry-a"


@pytest.mark.parametrize(
    ("binding", "expected"),
    (
        (text_binding("service-value"), False),
        (replace(numeric_binding("service-value", 1.0, "SysVar"), unit="invalid"), False),
        (None, False),
        (
            replace(
                numeric_binding("service-value", 1.0, "SysVar"),
                binding_method="uuid_state",
                state_uuid=None,
            ),
            True,
        ),
        (numeric_binding("service-value", 1.0, "SysVar"), True),
    ),
)
def test_service_module_requires_a_supported_safe_read_capability(
    registries,
    binding,
    expected,
):
    """Semantic type alone must not create configured-only or unsupported services."""
    runtime = EngineeringRuntimeInventory(bindings=() if binding is None else (binding,))
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("service", "GlobalStates", room=None),
            element(
                "service-value",
                "SysVar",
                parent_uuid="service",
                io_name="SYS1",
            ),
        ),
        runtime=runtime,
    )

    result = asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    identifier = "serial-a:service"
    assert (registries.device(identifier) is not None) is expected
    assert (identifier in result.metadata.active_device_identifiers) is expected


def test_mixed_service_keeps_supported_scalar_and_ignores_unsupported_child(registries):
    """One safe scalar keeps its service useful without promoting a text sibling."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("service", "GlobalStates", room=None),
            element("safe-value", "SysVar", parent_uuid="service", io_name="SYS1"),
            element("text-value", "SysVar", parent_uuid="service", io_name="SYS2"),
        ),
        runtime=EngineeringRuntimeInventory(
            bindings=(
                replace(
                    numeric_binding("safe-value", 1.0, "SysVar"),
                    binding_method="uuid_state",
                    state_uuid=None,
                ),
                text_binding("text-value"),
            )
        ),
    )

    asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert registries.device("serial-a:service") is not None


def test_sensitive_only_service_is_not_created(registries):
    """Suppressed descendants cannot make a logical service registry-visible."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("service", "GlobalStates", room=None),
            element("private", "Credential", parent_uuid="service", room=None),
            element(
                "service-value",
                "SysVar",
                parent_uuid="private",
                io_name="SYS1",
            ),
        ),
        runtime=EngineeringRuntimeInventory(bindings=(numeric_binding("service-value", 1.0, "SysVar"),)),
    )

    asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert registries.device("serial-a:service") is None


def test_area_intent_survives_metadata_failure_and_fresh_replan(registries, monkeypatch):
    """A cold replan must retain ownership of an integration-made room move."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
        model="TreeDevice",
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    first_snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )
    state = StoredEngineeringState(snapshot=first_snapshot)

    async def fail_metadata_save(hass, candidate):
        del hass, candidate
        raise RuntimeError("injected metadata failure")

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        fail_metadata_save,
    )
    first_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            first_snapshot,
            previous,
        )
    )
    with pytest.raises(RuntimeError, match="injected metadata failure"):
        asyncio.run(
            async_apply_engineering_registry_plan(
                registries.hass,
                first_plan,
                committed_state=state,
            )
        )
    workshop = registries.areas.async_get_area_by_name("Workshop")
    assert device.area_id == workshop.id
    assert FakeIntentStore.data

    saved: list[StoredEngineeringState] = []

    async def save_metadata(hass, candidate):
        del hass
        saved.append(candidate)

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        save_metadata,
    )
    cold_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            first_snapshot,
            previous,
        )
    )
    recovered = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            cold_plan,
            committed_state=state,
        )
    )
    assert recovered.metadata.managed_area_ids["serial-a:device"] == workshop.id
    assert FakeIntentStore.data

    second_snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Living Room",
            ),
        ),
        read_sequence=2,
    )
    second_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            second_snapshot,
            recovered.metadata,
        )
    )
    second = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            second_plan,
            committed_state=StoredEngineeringState(snapshot=second_snapshot),
        )
    )
    living = registries.areas.async_get_area_by_name("Living Room")
    assert device.area_id == living.id
    assert second.metadata.managed_area_ids["serial-a:device"] == living.id
    assert saved


def test_same_plan_replay_retains_managed_area_after_metadata_failure(registries, monkeypatch):
    """The original deterministic plan must recognize its persisted move."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )

    async def fail_metadata_save(hass, candidate):
        del hass, candidate
        raise RuntimeError("injected metadata failure")

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        fail_metadata_save,
    )
    state = StoredEngineeringState(snapshot=snapshot)
    with pytest.raises(RuntimeError, match="injected metadata failure"):
        asyncio.run(
            async_apply_engineering_registry_plan(
                registries.hass,
                plan,
                committed_state=state,
            )
        )
    workshop = registries.areas.async_get_area_by_name("Workshop")
    assert device.area_id == workshop.id
    assert FakeIntentStore.data

    async def save_metadata(hass, candidate):
        del hass, candidate

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        save_metadata,
    )
    recovered = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            plan,
            committed_state=state,
        )
    )

    assert device.area_id == workshop.id
    assert recovered.metadata.managed_area_ids["serial-a:device"] == workshop.id
    assert FakeIntentStore.data


def test_cross_process_old_area_is_preserved_for_explicit_resolution(
    registries,
    monkeypatch,
):
    """An older HA area after restart is never inferred to be a rollback."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )

    async def save_metadata(hass, candidate):
        del hass, candidate

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        save_metadata,
    )
    state = StoredEngineeringState(snapshot=snapshot)
    first_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )
    first = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            first_plan,
            committed_state=state,
        )
    )
    workshop = registries.areas.async_get_area_by_name("Workshop")
    assert device.area_id == workshop.id
    assert FakeIntentStore.data

    device.area_id = office.id
    monkeypatch.setattr(engineering_registry, "_AREA_PROCESS_TOKEN", "process-b")
    cold_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            first.metadata,
        )
    )
    recovered = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            cold_plan,
            committed_state=state,
        )
    )
    conflicts = asyncio.run(
        engineering_registry.async_load_engineering_area_conflicts(
            registries.hass,
            "entry-a",
        )
    )
    assert device.area_id == office.id
    assert "serial-a:device" not in recovered.metadata.managed_area_ids
    assert conflicts[0].reason == "area_assignment_unverified"


@pytest.mark.parametrize("override_room", ("Office", None))
def test_same_process_unexplained_area_change_is_preserved_and_conflicted(
    registries,
    monkeypatch,
    override_room,
):
    """A post-apply user change is never inferred from its area value."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )
    monkeypatch.setattr(
        engineering_registry,
        "async_store_engineering_state",
        lambda hass, state: asyncio.sleep(0),
    )
    state = StoredEngineeringState(snapshot=snapshot)
    first_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )
    first = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            first_plan,
            committed_state=state,
        )
    )
    override_area = registries.areas.async_get_or_create(override_room) if override_room is not None else None
    device.area_id = override_area.id if override_area else None
    mutations = registries.devices.mutations

    replay_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            first.metadata,
        )
    )
    replayed = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            replay_plan,
            committed_state=state,
        )
    )
    conflicts = asyncio.run(
        engineering_registry.async_load_engineering_area_conflicts(
            registries.hass,
            "entry-a",
        )
    )

    assert registries.devices.mutations == mutations
    assert device.area_id == (override_area.id if override_area else None)
    assert "serial-a:device" not in replayed.metadata.managed_area_ids
    assert conflicts[0].reason == "area_user_override_preserved"


@pytest.mark.parametrize("durable_room", ("Office", "Workshop"))
@pytest.mark.parametrize("modified_at", (None, "same", "clock-rollback"))
def test_rapid_generations_cold_old_area_pauses_without_timestamp_inference(
    registries,
    monkeypatch,
    durable_room,
    modified_at,
):
    """Cold older registry states become conflicts regardless of timestamps."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    device.modified_at = modified_at
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    monkeypatch.setattr(
        engineering_registry,
        "async_store_engineering_state",
        lambda hass, state: asyncio.sleep(0),
    )

    metadata = previous
    snapshots = []
    for sequence, room in enumerate(("Workshop", "Living Room"), 1):
        snapshot = make_snapshot(
            inventory=inventory_of(
                element("ms", "LoxLIVE", title="Miniserver", room=None),
                element(
                    "device",
                    "TreeDevice",
                    parent_uuid="ms",
                    title="ST-F07",
                    room=room,
                ),
            ),
            read_sequence=sequence,
        )
        snapshots.append(snapshot)
        plan = asyncio.run(
            async_plan_engineering_registry_sync(
                registries.hass,
                "entry-a",
                snapshot,
                metadata,
            )
        )
        applied = asyncio.run(
            async_apply_engineering_registry_plan(
                registries.hass,
                plan,
                committed_state=StoredEngineeringState(snapshot=snapshot),
            )
        )
        metadata = applied.metadata
    durable = registries.areas.async_get_area_by_name(durable_room)
    device.area_id = durable.id
    monkeypatch.setattr(engineering_registry, "_AREA_PROCESS_TOKEN", "process-b")
    mutations = registries.devices.mutations

    cold_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshots[-1],
            metadata,
        )
    )
    result = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            cold_plan,
            committed_state=StoredEngineeringState(snapshot=snapshots[-1]),
        )
    )
    conflicts = asyncio.run(
        engineering_registry.async_load_engineering_area_conflicts(
            registries.hass,
            "entry-a",
        )
    )

    assert registries.devices.mutations == mutations
    assert device.area_id == durable.id
    assert "serial-a:device" not in result.metadata.managed_area_ids
    assert conflicts[0].reason == "area_assignment_unverified"


def test_managed_area_clear_is_explicit_then_allows_following_room_move(
    registries,
    monkeypatch,
):
    """A managed Office to None to Workshop sequence remains intentional."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    metadata = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    monkeypatch.setattr(
        engineering_registry,
        "async_store_engineering_state",
        lambda hass, state: asyncio.sleep(0),
    )

    snapshots = []
    for sequence, room in enumerate((None, "Workshop"), 1):
        snapshot = make_snapshot(
            inventory=inventory_of(
                element("ms", "LoxLIVE", title="Miniserver", room=None),
                element(
                    "device",
                    "TreeDevice",
                    parent_uuid="ms",
                    title="ST-F07",
                    room=room,
                ),
            ),
            read_sequence=sequence,
        )
        snapshots.append(snapshot)
        plan = asyncio.run(
            async_plan_engineering_registry_sync(
                registries.hass,
                "entry-a",
                snapshot,
                metadata,
            )
        )
        result = asyncio.run(
            async_apply_engineering_registry_plan(
                registries.hass,
                plan,
                committed_state=StoredEngineeringState(snapshot=snapshot),
            )
        )
        metadata = result.metadata
        if room is None:
            assert device.area_id is None
            assert "serial-a:device" not in metadata.managed_area_ids
    workshop = registries.areas.async_get_area_by_name("Workshop")
    assert device.area_id == workshop.id
    assert metadata.managed_area_ids["serial-a:device"] == workshop.id


def test_managed_clear_after_cold_ambiguous_restart_is_paused(registries, monkeypatch):
    """A cold mismatch cannot authorize an otherwise managed clear."""
    office = registries.areas.async_get_or_create("Office")
    user_area = registries.areas.async_get_or_create("User Area")
    device = registries.devices.add("serial-a:device", "entry-a", area_id=office.id, name="ST-F07")
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room=None,
            ),
        )
    )
    FakeIntentStore.data = {
        "generation_id": "older",
        "managed_baselines": [
            {
                "identifier": "serial-a:device",
                "area_id": office.id,
                "process_token": "process-a",
            }
        ],
    }
    device.area_id = user_area.id
    monkeypatch.setattr(engineering_registry, "_AREA_PROCESS_TOKEN", "process-b")
    monkeypatch.setattr(
        engineering_registry,
        "async_store_engineering_state",
        lambda hass, state: asyncio.sleep(0),
    )

    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )
    result = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            plan,
            committed_state=StoredEngineeringState(snapshot=snapshot),
        )
    )

    assert device.area_id == user_area.id
    assert "serial-a:device" not in result.metadata.managed_area_ids


def test_user_change_during_metadata_await_relinquishes_area_authority(
    registries,
    monkeypatch,
):
    """The final awaited metadata publication cannot hide a user override."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    user_area = registries.areas.async_get_or_create("User Area")
    device = registries.devices.add("serial-a:device", "entry-a", area_id=office.id, name="ST-F07")
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )
    saves = 0

    async def save_metadata(hass, state):
        nonlocal saves
        del hass, state
        saves += 1
        if saves == 1:
            device.area_id = user_area.id

    monkeypatch.setattr(
        engineering_registry,
        "async_store_engineering_state",
        save_metadata,
    )
    plan = asyncio.run(async_plan_engineering_registry_sync(registries.hass, "entry-a", snapshot, previous))
    result = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            plan,
            committed_state=StoredEngineeringState(snapshot=snapshot),
        )
    )
    conflicts = asyncio.run(engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))

    assert saves == 2
    assert device.area_id == user_area.id
    assert "serial-a:device" not in result.metadata.managed_area_ids
    assert conflicts[0].reason == "area_user_override_preserved"


def test_area_conflict_round_trip_and_explicit_resolution_actions(
    registries,
    monkeypatch,
):
    """Task 8 gets immutable, stale-safe Apply and Keep resolution contracts."""
    office = registries.areas.async_get_or_create("Office")
    workshop = registries.areas.async_get_or_create("Workshop")
    device = registries.devices.add("serial-a:device", "entry-a", area_id=office.id, name="ST-F07")
    FakeIntentStore.data = {
        "generation_id": "generation-a",
        "managed_baselines": [
            {
                "identifier": "serial-a:device",
                "area_id": workshop.id,
                "process_token": "process-old",
            }
        ],
    }
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Workshop"}),
        {"serial-a:device": workshop.id},
        "generation-a",
        "serial-a",
    )
    monkeypatch.setattr(
        engineering_registry,
        "async_store_engineering_state",
        lambda hass, state: asyncio.sleep(0),
    )
    plan = asyncio.run(async_plan_engineering_registry_sync(registries.hass, "entry-a", snapshot, previous))
    asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            plan,
            committed_state=StoredEngineeringState(snapshot=snapshot),
        )
    )
    conflict = asyncio.run(engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))[0]
    assert conflict.device_identifier == "serial-a:device"
    assert conflict.display_name == "ST-F07"
    assert conflict.current_area_name == "Office"
    assert conflict.desired_area_name == "Workshop"
    with pytest.raises(FrozenInstanceError):
        conflict.reason = "changed"

    stale = asyncio.run(
        engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass,
            "entry-a",
            "not-the-current-token",
            "apply_loxone_room",
        )
    )
    assert not stale.resolved
    assert device.area_id == office.id

    user_area = registries.areas.async_get_or_create("User Area")
    FakeIntentStore.save_callbacks = [
        lambda: None,
        lambda: setattr(device, "area_id", user_area.id),
    ]
    raced = asyncio.run(
        engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass,
            "entry-a",
            conflict.token,
            "apply_loxone_room",
        )
    )
    assert not raced.resolved
    assert device.area_id == user_area.id
    race_conflict = asyncio.run(engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))[
        0
    ]
    assert race_conflict.current_area_id == user_area.id

    applied = asyncio.run(
        engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass,
            "entry-a",
            race_conflict.token,
            "apply_loxone_room",
        )
    )
    assert applied.resolved
    assert device.area_id == workshop.id
    assert not asyncio.run(engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))
    replay = asyncio.run(
        engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass,
            "entry-a",
            conflict.token,
            "apply_loxone_room",
        )
    )
    assert not replay.resolved

    device.area_id = office.id
    plan = asyncio.run(async_plan_engineering_registry_sync(registries.hass, "entry-a", snapshot, previous))
    asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            plan,
            committed_state=StoredEngineeringState(snapshot=snapshot),
        )
    )
    keep_conflict = asyncio.run(engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))[
        0
    ]
    kept = asyncio.run(
        engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass,
            "entry-a",
            keep_conflict.token,
            "keep_ha_room",
        )
    )
    assert kept.resolved
    mutations = registries.devices.mutations
    replay_plan = asyncio.run(async_plan_engineering_registry_sync(registries.hass, "entry-a", snapshot, previous))
    asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            replay_plan,
            committed_state=StoredEngineeringState(snapshot=snapshot),
        )
    )
    assert registries.devices.mutations == mutations
    assert device.area_id == office.id
    assert not asyncio.run(engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))


def test_retained_area_evidence_is_discarded_for_a_user_override(registries, monkeypatch):
    """A user area outside retained previous/desired evidence cannot be reclaimed."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )

    async def save_metadata(hass, candidate):
        del hass, candidate

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        save_metadata,
    )
    state = StoredEngineeringState(snapshot=snapshot)
    first_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )
    first = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            first_plan,
            committed_state=state,
        )
    )
    user_area = registries.areas.async_get_or_create("User Area")
    device.area_id = user_area.id

    cold_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            first.metadata,
        )
    )
    recovered = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            cold_plan,
            committed_state=state,
        )
    )

    assert device.area_id == user_area.id
    assert "serial-a:device" not in recovered.metadata.managed_area_ids
    assert not any(
        item["identifier"] == "serial-a:device" for item in (FakeIntentStore.data or {}).get("area_intents", ())
    )
    replayed = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            cold_plan,
            committed_state=state,
        )
    )
    assert device.area_id == user_area.id
    assert "serial-a:device" not in replayed.metadata.managed_area_ids


def test_each_journal_save_rechecks_all_remaining_area_mutations(registries, monkeypatch):
    """User changes during consecutive journal saves both win."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id, ("serial-a:device-a", "serial-a:device-b"))
    user_area = registries.areas.async_get_or_create("User Area")
    first_device = registries.devices.add(
        "serial-a:device-a",
        "entry-a",
        area_id=office.id,
        name="ST-F01",
    )
    second_device = registries.devices.add(
        "serial-a:device-b",
        "entry-a",
        area_id=office.id,
        name="ST-F02",
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device-a", "serial-a:device-b"}),
        frozenset({"Office"}),
        {
            "serial-a:device-a": office.id,
            "serial-a:device-b": office.id,
        },
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device-a",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F01",
                room="Workshop",
            ),
            element(
                "device-b",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F02",
                room="Workshop",
            ),
        )
    )
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )
    FakeIntentStore.save_callbacks = [
        lambda: setattr(first_device, "area_id", user_area.id),
        lambda: setattr(second_device, "area_id", user_area.id),
    ]

    async def save_metadata(hass, candidate):
        del hass, candidate

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        save_metadata,
    )
    result = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            plan,
            committed_state=StoredEngineeringState(snapshot=snapshot),
        )
    )

    assert FakeIntentStore.save_count >= 3
    assert first_device.area_id == user_area.id
    assert second_device.area_id == user_area.id
    assert "serial-a:device-a" not in result.metadata.managed_area_ids
    assert "serial-a:device-b" not in result.metadata.managed_area_ids
    retained = (FakeIntentStore.data or {}).get("area_intents", ())
    assert not any(item["identifier"] in {"serial-a:device-a", "serial-a:device-b"} for item in retained)


@pytest.mark.parametrize("user_room", ("User Area", "Workshop", None))
def test_user_area_change_between_plan_and_apply_is_never_claimed(
    registries,
    monkeypatch,
    user_room,
):
    """A current-area recheck preserves a user override made after planning."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )
    user_area = registries.areas.async_get_or_create(user_room) if user_room is not None else None
    device.area_id = user_area.id if user_area else None
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        lambda hass, state: asyncio.sleep(0),
    )

    result = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            plan,
            committed_state=StoredEngineeringState(snapshot=snapshot),
        )
    )

    assert device.area_id == (user_area.id if user_area else None)
    assert "serial-a:device" not in result.metadata.managed_area_ids
    conflicts = asyncio.run(engineering_registry.async_load_engineering_area_conflicts(registries.hass, "entry-a"))
    assert conflicts[0].reason == "area_user_override_preserved"


def test_user_area_change_before_cold_replan_cancels_persisted_intent(
    registries,
    monkeypatch,
):
    """A user override after partial apply wins when the pending plan is rebuilt."""
    office = registries.areas.async_get_or_create("Office")
    _seed_managed_baseline(office.id)
    device = registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    previous = EngineeringRegistryMetadata(
        frozenset({"serial-a:device"}),
        frozenset({"Office"}),
        {"serial-a:device": office.id},
        "old-generation",
        "serial-a",
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element(
                "device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F07",
                room="Workshop",
            ),
        )
    )

    async def fail_metadata_save(hass, candidate):
        del hass, candidate
        raise RuntimeError("injected metadata failure")

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        fail_metadata_save,
    )
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )
    with pytest.raises(RuntimeError, match="injected metadata failure"):
        asyncio.run(
            async_apply_engineering_registry_plan(
                registries.hass,
                plan,
                committed_state=StoredEngineeringState(snapshot=snapshot),
            )
        )
    user_area = registries.areas.async_get_or_create("User Area")
    device.area_id = user_area.id

    cold_plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            previous,
        )
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_store_engineering_state",
        lambda hass, state: asyncio.sleep(0),
    )
    recovered = asyncio.run(
        async_apply_engineering_registry_plan(
            registries.hass,
            cold_plan,
            committed_state=StoredEngineeringState(snapshot=snapshot),
        )
    )

    assert device.area_id == user_area.id
    assert "serial-a:device" not in recovered.metadata.managed_area_ids


def test_area_intent_store_failure_precedes_all_registry_mutations(
    registries,
):
    """An unacknowledged intent cannot authorize an area mutation."""
    snapshot = make_snapshot(inventory=reference_link_inventory())
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )
    before = registries.mutations
    FakeIntentStore.fail_save = True

    with pytest.raises(RuntimeError, match="intent store failure"):
        asyncio.run(
            async_apply_engineering_registry_plan(
                registries.hass,
                plan,
                committed_state=StoredEngineeringState(snapshot=snapshot),
            )
        )

    assert registries.mutations == before


def _install_legacy_entity(registries, snapshot):
    spec = build_engineering_entity_specs(snapshot.rows, None)[0]
    legacy = registries.devices.add(spec.unique_id, "entry-a", name=spec.name)
    entity = registries.entities.add(
        domain=spec.platform,
        platform=DOMAIN,
        unique_id=spec.unique_id,
        config_entry_id="entry-a",
        device_id=legacy.id,
    )
    return spec, legacy, entity


@pytest.mark.parametrize("entry_order", (("entry-a", "entry-b"), ("entry-b", "entry-a")))
def test_unloaded_snapshot_claimant_blocks_legacy_reassociation(
    registries,
    monkeypatch,
    entry_order,
):
    """Persisted snapshots, not coordinator load order, establish claimants."""
    snapshot_a = _snapshot_for("entry-a", "serial-a")
    snapshot_b = _snapshot_for("entry-b", "serial-b")
    spec, legacy, entity = _install_legacy_entity(registries, snapshot_a)
    states = {
        "entry-b": StoredEngineeringState(snapshot=snapshot_b),
    }
    registries.hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [SimpleNamespace(entry_id=item) for item in entry_order]
    )

    async def load_state(hass, entry_id):
        del hass
        return states.get(entry_id, StoredEngineeringState(snapshot=None))

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_load_engineering_state",
        load_state,
    )

    result = asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot_a,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert entity.device_id == legacy.id
    assert spec.unique_id in result.ambiguous_legacy_identifiers


def test_existing_scoped_claimant_blocks_legacy_reassociation(registries):
    """A persisted scoped device claim is evidence even without a loaded snapshot."""
    snapshot = _snapshot_for("entry-a", "serial-a")
    spec, legacy, entity = _install_legacy_entity(registries, snapshot)
    registries.devices.add(
        f"serial-b:{spec.unique_id}",
        "entry-b",
        name=spec.name,
    )

    result = asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert entity.device_id == legacy.id
    assert spec.unique_id in result.ambiguous_legacy_identifiers


def test_unavailable_other_snapshot_blocks_legacy_reassociation(
    registries,
    monkeypatch,
):
    """A configured entry without claimant evidence makes migration ambiguous."""
    snapshot = _snapshot_for("entry-a", "serial-a")
    spec, legacy, entity = _install_legacy_entity(registries, snapshot)
    registries.hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id="entry-a"),
            SimpleNamespace(entry_id="entry-b"),
        ]
    )

    async def load_state(hass, entry_id):
        del hass, entry_id
        return StoredEngineeringState(snapshot=None)

    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_load_engineering_state",
        load_state,
    )

    result = asyncio.run(
        async_sync_engineering_devices(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )

    assert entity.device_id == legacy.id
    assert spec.unique_id in result.ambiguous_legacy_identifiers


def test_entity_identity_change_after_plan_is_rejected(registries):
    """The global entity identity is resolved again immediately before mutation."""
    snapshot = _snapshot_for("entry-a", "serial-a")
    spec, legacy, entity = _install_legacy_entity(registries, snapshot)
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )
    entity.unique_id = "changed-identity"

    result = asyncio.run(async_apply_engineering_registry_plan(registries.hass, plan))

    assert entity.device_id == legacy.id
    assert spec.unique_id in {rejection.spec.unique_id for rejection in result.rejected_entities}


def test_new_claimant_after_plan_blocks_legacy_reassociation(registries):
    """A claimant appearing across the plan/apply boundary prevents takeover."""
    snapshot = _snapshot_for("entry-a", "serial-a")
    spec, legacy, entity = _install_legacy_entity(registries, snapshot)
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
    )
    registries.devices.add(
        f"serial-b:{spec.unique_id}",
        "entry-b",
        name=spec.name,
    )

    result = asyncio.run(async_apply_engineering_registry_plan(registries.hass, plan))

    assert entity.device_id == legacy.id
    assert spec.unique_id in result.ambiguous_legacy_identifiers


def test_legacy_metadata_round_trip_preserves_audit_scope_without_generation(
    monkeypatch,
):
    """The unchanged coordinator payload remains readable during Task-5 migration."""
    FakeLegacyMetadataStore.data = None
    inventory = inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("device", "Lox1wireDevice", parent_uuid="ms", title="ST-F07"),
        element(
            "temperature",
            "Lox1wireAsensor",
            parent_uuid="device",
            title="Temperature",
            io_name="AI1",
            platform="sensor",
        ),
    )
    specs = build_engineering_sensor_specs(
        inventory,
        EngineeringRuntimeInventory(bindings=(numeric_binding("temperature", 21.0, "Lox1wireAsensor"),)),
    )
    monkeypatch.setattr(
        "custom_components.loxone.engineering_entities.Store",
        FakeLegacyMetadataStore,
    )

    async def no_snapshot(hass, entry_id):
        del hass, entry_id
        return StoredEngineeringState(snapshot=None)

    monkeypatch.setattr(
        "custom_components.loxone.engineering_snapshot.async_load_engineering_state",
        no_snapshot,
    )

    asyncio.run(
        async_store_engineering_registry_metadata(
            SimpleNamespace(),
            "entry-a",
            specs,
        )
    )
    metadata = asyncio.run(async_load_engineering_registry_metadata(SimpleNamespace(), "entry-a"))

    assert metadata.active_device_identifiers == frozenset({"device"})
    assert metadata.room_names == frozenset({"Office"})
    assert metadata.applied_generation is None
    assert metadata.provider_identifier is None


@pytest.mark.parametrize(
    ("action", "room", "boundary"),
    (
        ("apply_loxone_room", "Workshop", "task5_lock"),
        ("apply_loxone_room", None, "intent_commit"),
        ("apply_loxone_room", "Workshop", "reload"),
        ("keep_ha_room", "Workshop", "keep_commit"),
    ),
)
def test_resolution_lifecycle_guard_fences_every_awaited_mutation_boundary(
    registries,
    monkeypatch,
    action,
    room,
    boundary,
):
    """A revoked caller cannot mutate an area or commit a Keep after waiting."""
    office, snapshot, committed = _area_recovery_case(
        registries,
        monkeypatch,
        room,
    )
    current = True

    async def scenario():
        nonlocal current
        conflict = await _prepare_resolution_conflict(
            registries,
            snapshot,
            committed,
            office,
        )
        before = deepcopy(FakeIntentStore.data)
        entered = asyncio.Event()
        release = asyncio.Event()

        if boundary == "task5_lock":
            lock = engineering_registry._area_operation_lock(
                registries.hass,
                "entry-a",
            )
            await lock.acquire()
        elif boundary in {"intent_commit", "keep_commit"}:
            original_save = FakeIntentStore.async_save_acknowledged

            async def held_save(store, data, *, before_commit=None):
                entered.set()
                await release.wait()
                await original_save(
                    store,
                    data,
                    before_commit=before_commit,
                )

            monkeypatch.setattr(
                FakeIntentStore,
                "async_save_acknowledged",
                held_save,
            )
        else:
            original_load = FakeIntentStore.async_load
            loads = 0

            async def held_reload(store):
                nonlocal loads
                loads += 1
                if loads == 2:
                    entered.set()
                    await release.wait()
                return await original_load(store)

            monkeypatch.setattr(FakeIntentStore, "async_load", held_reload)

        task = asyncio.create_task(
            engineering_registry.async_resolve_engineering_area_conflict(
                registries.hass,
                "entry-a",
                conflict.token,
                action,
                is_current=lambda: current,
            )
        )
        if boundary == "task5_lock":
            await asyncio.sleep(0)
        else:
            await entered.wait()
        current = False
        if boundary == "task5_lock":
            lock.release()
        else:
            release.set()
        result = await task

        assert not result.resolved
        assert result.reason == "lifecycle_changed"
        assert registries.device("serial-a:device").area_id == office.id
        if boundary in {"task5_lock", "intent_commit", "keep_commit"}:
            assert FakeIntentStore.data == before

    asyncio.run(scenario())


def test_resolution_lifecycle_guard_preserves_cancellation_and_retry(
    registries,
    monkeypatch,
):
    """Cancellation at the Task 5 lock propagates and leaves its lock reusable."""
    office, snapshot, committed = _area_recovery_case(
        registries,
        monkeypatch,
    )

    async def scenario():
        conflict = await _prepare_resolution_conflict(
            registries,
            snapshot,
            committed,
            office,
        )
        lock = engineering_registry._area_operation_lock(
            registries.hass,
            "entry-a",
        )
        await lock.acquire()
        task = asyncio.create_task(
            engineering_registry.async_resolve_engineering_area_conflict(
                registries.hass,
                "entry-a",
                conflict.token,
                "apply_loxone_room",
                is_current=lambda: True,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lock.release()

        result = await engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass,
            "entry-a",
            conflict.token,
            "apply_loxone_room",
            is_current=lambda: True,
        )
        assert result.resolved
        assert registries.device("serial-a:device").area_id != office.id

    asyncio.run(scenario())


def test_revocation_during_admitted_intent_write_cannot_authorize_replay(
    registries,
    monkeypatch,
):
    """A committed prepared intent remains fenced by its durable conflict."""
    office, snapshot, committed = _area_recovery_case(
        registries,
        monkeypatch,
    )
    current = True

    async def scenario():
        nonlocal current
        old_plan = await async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            snapshot,
            EngineeringRegistryMetadata.empty(),
        )
        await async_apply_engineering_registry_plan(
            registries.hass,
            old_plan,
            committed_state=committed,
        )
        registries.devices.async_update_device(
            registries.device("serial-a:device").id,
            area_id=office.id,
        )
        conflict = await _record_override(registries, snapshot, committed)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_save = FakeIntentStore.async_save_acknowledged

        async def admitted_save(store, data, *, before_commit=None):
            assert before_commit is not None
            before_commit()
            entered.set()
            await release.wait()
            await original_save(store, data)

        monkeypatch.setattr(
            FakeIntentStore,
            "async_save_acknowledged",
            admitted_save,
        )
        resolution = asyncio.create_task(
            engineering_registry.async_resolve_engineering_area_conflict(
                registries.hass,
                "entry-a",
                conflict.token,
                "apply_loxone_room",
                is_current=lambda: current,
            )
        )
        await entered.wait()
        current = False
        release.set()
        result = await resolution
        assert not result.resolved
        assert result.reason == "lifecycle_changed"
        assert registries.device("serial-a:device").area_id == office.id
        assert FakeIntentStore.data["area_intents"]
        assert FakeIntentStore.data["area_conflicts"]

        monkeypatch.setattr(
            FakeIntentStore,
            "async_save_acknowledged",
            original_save,
        )
        await async_apply_engineering_registry_plan(
            registries.hass,
            old_plan,
            committed_state=committed,
        )
        assert registries.device("serial-a:device").area_id == office.id
        assert FakeIntentStore.data["area_conflicts"]

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ("intent_save", "reload"))
def test_resolution_guarded_await_cancellation_propagates_and_retries(
    registries,
    monkeypatch,
    boundary,
):
    """Cancellation at Task 5 persistence boundaries never becomes success."""
    office, snapshot, committed = _area_recovery_case(
        registries,
        monkeypatch,
    )

    async def scenario():
        conflict = await _prepare_resolution_conflict(
            registries,
            snapshot,
            committed,
            office,
        )
        entered = asyncio.Event()
        if boundary == "intent_save":
            original = FakeIntentStore.async_save_acknowledged

            async def blocked_save(store, data, *, before_commit=None):
                del store, data, before_commit
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(
                FakeIntentStore,
                "async_save_acknowledged",
                blocked_save,
            )
        else:
            original = FakeIntentStore.async_load
            loads = 0

            async def blocked_reload(store):
                nonlocal loads
                loads += 1
                if loads == 2:
                    entered.set()
                    await asyncio.Event().wait()
                return await original(store)

            monkeypatch.setattr(FakeIntentStore, "async_load", blocked_reload)

        task = asyncio.create_task(
            engineering_registry.async_resolve_engineering_area_conflict(
                registries.hass,
                "entry-a",
                conflict.token,
                "apply_loxone_room",
                is_current=lambda: True,
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert registries.device("serial-a:device").area_id == office.id

        monkeypatch.setattr(
            FakeIntentStore,
            ("async_save_acknowledged" if boundary == "intent_save" else "async_load"),
            original,
        )
        retry = await engineering_registry.async_resolve_engineering_area_conflict(
            registries.hass,
            "entry-a",
            conflict.token,
            "apply_loxone_room",
            is_current=lambda: True,
        )
        assert retry.resolved

    asyncio.run(scenario())
