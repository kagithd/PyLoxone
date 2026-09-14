"""Transaction and cold-process recovery tests for engineering refresh."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

import custom_components.loxone.coordinator as module
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from custom_components.loxone.engineering_snapshot import (
    EngineeringStoreCommitCancelledError,
    EngineeringStoreCommitOutcome,
    async_load_engineering_state,
)
from tests.engineering_fixtures import element, inventory_of, numeric_binding
from tests.test_engineering_registry import RegistryHarness, registries  # noqa: F401
from tests.test_engineering_runtime import _Session


class MemoryStore:
    """Persist copied wire payloads, with faults before acknowledged writes."""

    data = {}
    events = []
    fault = None

    def __init__(self, hass, version, key, **kwargs):
        self.key = key

    async def async_load(self):
        return deepcopy(self.data.get(self.key))

    async def async_save_acknowledged(self, data):
        if self.key.startswith("loxone.engineering_snapshot"):
            token = data["snapshot"]["generation_id"]
            phase = (
                "published"
                if data["impact_published_generation"] == token
                else "applied"
                if data["registry_applied_generation"] == token
                else "candidate"
            )
        else:
            phase = "maintenance"
        self.events.append(phase)
        if self.fault == phase:
            raise asyncio.CancelledError
        self.data[self.key] = deepcopy(data)
        if self.fault == phase + "_after":
            raise EngineeringStoreCommitCancelledError(
                EngineeringStoreCommitOutcome.CANCELLED,
                settled_outcome=EngineeringStoreCommitOutcome.COMMITTED,
            )

    async def async_save(self, data):
        await self.async_save_acknowledged(data)


@pytest.fixture
def transaction(monkeypatch, registries, tmp_path):  # noqa: F811 -- imported shared pytest fixture.
    monkeypatch.setattr(
        "custom_components.loxone.engineering_registry.async_load_engineering_state", async_load_engineering_state
    )
    monkeypatch.setattr("homeassistant.helpers.frame.report_usage", lambda *args, **kwargs: None)
    MemoryStore.data = {}
    MemoryStore.events = []
    MemoryStore.fault = None
    for path in (
        "engineering_snapshot.EngineeringStateStore",
        "engineering_entities.Store",
        "registry_maintenance.Store",
    ):
        monkeypatch.setattr(f"custom_components.loxone.{path}", MemoryStore)
    notifications = {}
    monkeypatch.setattr(
        "homeassistant.components.persistent_notification.async_create",
        lambda hass, message, *, title, notification_id: notifications.update({notification_id: message}),
    )
    monkeypatch.setattr(
        "homeassistant.components.persistent_notification.async_dismiss",
        lambda hass, notification_id: notifications.pop(notification_id, None),
    )
    monkeypatch.setattr(module, "async_dispatcher_send", lambda *args: MemoryStore.events.append("signal"))
    monkeypatch.setattr(
        module,
        "async_sync_engineering_area_conflict_issues",
        lambda hass, entry_id, **kwargs: asyncio.sleep(
            0,
            result=MemoryStore.events.append(f"repairs:{entry_id}"),
        ),
        raising=False,
    )
    monkeypatch.setattr("custom_components.loxone.config_impact.entity_sources", lambda hass: {})
    monkeypatch.setattr(
        "homeassistant.helpers.device_registry.async_entries_for_config_entry",
        lambda registry, entry_id: [d for d in registry.devices.values() if entry_id in d.config_entries],
    )
    monkeypatch.setattr(
        "homeassistant.helpers.entity_registry.async_entries_for_device",
        lambda registry, device_id: [e for e in registry.entities.values() if e.device_id == device_id],
    )
    inventory = inventory_of(
        element("ms", "LoxLIVE", room=None),
        element("device", "TreeDevice", parent_uuid="ms", room=None, title="ST-F01"),
        element("channel", "VoltageIn", parent_uuid="device", io_name="AI1", room=None),
    )
    calls = []
    unload_callbacks = []

    async def download(self):
        calls.append("download")
        return inventory

    async def probe(resolved, *, client):
        calls.append("probe")
        return EngineeringRuntimeInventory((numeric_binding("channel", 7.0),))

    monkeypatch.setattr(module.LoxoneCoordinator, "_async_download_engineering_inventory", download, raising=False)
    monkeypatch.setattr(module, "async_probe_engineering_runtime", probe)
    monkeypatch.setattr(module, "async_get_clientsession", lambda hass: object())

    def make():
        hass = HomeAssistant(str(tmp_path))
        entry = SimpleNamespace(
            entry_id="entry-a",
            async_on_unload=unload_callbacks.append,
            domain="loxone",
            state=ConfigEntryState.LOADED,
            options={"username": "", "password": "", "host": "", "port": 0},
        )
        hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [entry],
            async_get_entry=lambda entry_id: entry if entry_id == entry.entry_id else None,
        )
        coordinator = module.LoxoneCoordinator(hass, entry)
        coordinator.miniserver = SimpleNamespace(
            serial="serial-a", lox_config=SimpleNamespace(json={"lastModified": "revision-7", "controls": {}})
        )
        coordinator.api = SimpleNamespace(scheme="", url="", close=lambda: asyncio.sleep(0))
        hass.data["loxone"] = {"entry-a": coordinator}
        return coordinator

    return SimpleNamespace(
        make=make,
        registries=registries,
        calls=calls,
        notifications=notifications,
        inventory=inventory,
        unload_callbacks=unload_callbacks,
    )


def test_config_entry_unload_callback_suspends_without_deleting_repairs(
    transaction,
    monkeypatch,
):
    """Ordinary unload invokes the lifecycle fence without retiring issues."""
    events = []

    def register(hass, entry, coordinator):
        events.append(("register", hass, entry, coordinator))

        def unload():
            events.append(("unload", hass, entry, coordinator))

        return unload

    monkeypatch.setattr(
        module,
        "async_register_engineering_area_conflict_reconciler",
        register,
    )

    async def scenario():
        coordinator = transaction.make()
        transaction.unload_callbacks[-1]()
        return coordinator

    coordinator = asyncio.run(scenario())

    assert events == [
        ("register", coordinator.hass, coordinator.config_entry, coordinator),
        ("unload", coordinator.hass, coordinator.config_entry, coordinator),
    ]


@pytest.mark.parametrize(
    "value,want",
    [
        ("revision-7", "source:766bf8758e1ca18bbbcf477eff1bf58e59bb4c3f296a3b57e9d30cb3faa7a638"),
        (7, "source:7902699be42c8a8e46fbbb4501726517e86b22c56a189f7625a6da49081b2451"),
        (7.5, "source:c4495da75095c64bf4e0587e45d7426f496ca4fc9fe72111715005361cfe0041"),
        (
            "2026-09-14 13:00:00",
            "source:6c18b22ee55883e4e58d00006960638ad68bac7fa4ec699ce9929691ebd193a7",
        ),
        (True, None),
        ({"name": "unsafe"}, None),
        ([], None),
        (float("nan"), None),
    ],
)
def test_scalar_revision(value, want):
    assert getattr(module, "extract_loxapp_last_modified", lambda value: "missing")({"lastModified": value}) == want


def test_forced_refresh_and_unchanged_rebind(transaction, monkeypatch):
    async def scenario():
        coordinator = transaction.make()
        first = await coordinator.async_refresh_engineering_inventory(force=True)
        assert first.read_sequence == 1
        assert transaction.registries.device("serial-a:device") is not None
        assert (
            MemoryStore.events.index("candidate")
            < MemoryStore.events.index("applied")
            < MemoryStore.events.index("repairs:entry-a")
            < MemoryStore.events.index("signal")
            < MemoryStore.events.index("published")
            < MemoryStore.events.index("maintenance")
        )

        async def rebind(self):
            transaction.calls.append("rebind")

        monkeypatch.setattr(module.LoxoneCoordinator, "async_rebind_engineering_runtime", rebind)
        assert await coordinator.async_refresh_engineering_inventory() is None
        assert transaction.calls.count("download") == 1
        assert transaction.calls.count("rebind") == 1
        second = await coordinator.async_refresh_engineering_inventory(force=True)
        assert second.read_sequence == 2
        assert second.configuration_revision_id == first.configuration_revision_id
        assert second.generation_id != first.generation_id
        before = deepcopy(MemoryStore.data)
        await coordinator.async_drain_committed_engineering_state()
        assert MemoryStore.data == before

    asyncio.run(scenario())


def test_refresh_and_cold_drain_retain_room_mapping_and_pending_batch(transaction, monkeypatch):
    """An explicit coordinator state construction must not drop room recovery metadata."""

    async def scenario():
        from custom_components.loxone.engineering_snapshot import (
            EngineeringAreaDecision,
            EngineeringAreaBatchIntent,
            EngineeringAreaBatchGroup,
            EngineeringAreaBatchMember,
            async_store_engineering_state,
        )

        coordinator = transaction.make()
        first = await coordinator.async_refresh_engineering_inventory(force=True)
        state = await async_load_engineering_state(coordinator.hass, "entry-a")
        decision = EngineeringAreaDecision("room-a", "use_existing", area_id="area-a", conflict_tokens=("a" * 64,))
        pending = EngineeringAreaBatchIntent(
            "entry-a",
            "serial-a",
            first.generation_id,
            (
                EngineeringAreaBatchGroup(
                    decision, (EngineeringAreaBatchMember("a" * 64, "serial-a:device", None, True),)
                ),
            ),
        )
        state = replace(state, room_area_mappings={"room-a": "area-a"}, pending_area_batch=pending)
        await async_store_engineering_state(coordinator.hass, state)
        await coordinator.async_refresh_engineering_inventory(force=True)
        warm = await async_load_engineering_state(coordinator.hass, "entry-a")
        assert warm.room_area_mappings == {"room-a": "area-a"}
        assert warm.pending_area_batch == pending
        cold = transaction.make()
        await cold.async_drain_committed_engineering_state(startup=True)
        restored = await async_load_engineering_state(cold.hass, "entry-a")
        assert restored.room_area_mappings == warm.room_area_mappings
        assert restored.pending_area_batch == pending

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["candidate", "publication"])
def test_coordinator_does_not_overwrite_concurrent_batch_metadata(transaction, monkeypatch, phase):
    """Read/plan/publish awaits must not write old copies over batch mappings."""

    async def scenario():
        from custom_components.loxone.engineering_snapshot import async_store_engineering_state

        coordinator = transaction.make()
        await coordinator.async_refresh_engineering_inventory(force=True)
        original = (
            module.async_find_engineering_change_impacts
            if phase == "candidate"
            else module.async_publish_engineering_impact_plan
        )

        async def interleave(*args, **kwargs):
            value = await original(*args, **kwargs)
            latest = await async_load_engineering_state(coordinator.hass, "entry-a")
            await async_store_engineering_state(
                coordinator.hass, replace(latest, room_area_mappings={"room-a": "area-a"})
            )
            return value

        monkeypatch.setattr(
            module,
            "async_find_engineering_change_impacts"
            if phase == "candidate"
            else "async_publish_engineering_impact_plan",
            interleave,
        )
        if phase == "publication":
            coordinator._engineering_published_impact_plan = None
        await coordinator.async_refresh_engineering_inventory(force=True)
        latest = await async_load_engineering_state(coordinator.hass, "entry-a")
        assert latest.room_area_mappings == {"room-a": "area-a"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "phase",
    [
        "candidate",
        "candidate_after",
        "applied",
        "applied_after",
        "published",
        "published_after",
        "maintenance",
        "maintenance_after",
    ],
)
def test_cancelled_phase_cold_restart_reconciles_older_registry(transaction, monkeypatch, phase):
    async def scenario():
        coordinator = transaction.make()
        MemoryStore.fault = phase
        with pytest.raises(asyncio.CancelledError):
            await coordinator.async_refresh_engineering_inventory(force=True)
        stored = await async_load_engineering_state(coordinator.hass, "entry-a")
        if phase == "candidate":
            assert stored.snapshot is None
            assert coordinator.engineering_snapshot is None
            assert transaction.registries.mutations == 0
            assert "signal" not in MemoryStore.events
            return
        generation = stored.snapshot.generation_id
        if phase == "applied":
            assert "signal" not in MemoryStore.events
            assert "maintenance" not in MemoryStore.events
        # A new coordinator and old delayed HA registry, not reused memory.
        older = RegistryHarness()
        monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: older.devices)
        monkeypatch.setattr("homeassistant.helpers.area_registry.async_get", lambda hass: older.areas)
        monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: older.entities)
        transaction.notifications.clear()
        MemoryStore.fault = None
        restarted = transaction.make()

        async def rebind(self):
            MemoryStore.events.append("rebind")

        monkeypatch.setattr(module.LoxoneCoordinator, "async_rebind_engineering_runtime", rebind)
        await restarted.async_restore_engineering_snapshot()
        final = await async_load_engineering_state(restarted.hass, "entry-a")
        assert final.snapshot.generation_id == generation
        assert final.registry_applied_generation == generation
        assert final.impact_published_generation == generation
        assert older.device("serial-a:device") is not None
        assert MemoryStore.events[-1] == "rebind"
        before = deepcopy(MemoryStore.data)
        await restarted.async_drain_committed_engineering_state()
        assert MemoryStore.data == before

    asyncio.run(scenario())


def test_stale_observations_count_new_reads_not_replay_and_notifications_are_bounded(transaction, monkeypatch):
    async def scenario():
        transaction.registries.devices.add("serial-a:stale", "entry-a", name="ST-F02", model="TreeDevice")
        coordinator = transaction.make()
        first = await coordinator.async_refresh_engineering_inventory(force=True)
        maintenance_key = "loxone.registry_maintenance.entry-a"
        assert MemoryStore.data[maintenance_key]["missing_observations"]["serial-a:stale"] == 1
        assert all("ST-F02" not in text for text in transaction.notifications.values())
        assert MemoryStore.data[maintenance_key]["last_counted_engineering_generation"] == first.generation_id

        async def rebind(self):
            pass

        monkeypatch.setattr(module.LoxoneCoordinator, "async_rebind_engineering_runtime", rebind)
        restarted = transaction.make()
        await restarted.async_restore_engineering_snapshot()
        await restarted.async_refresh_engineering_inventory()
        assert MemoryStore.data[maintenance_key]["missing_observations"]["serial-a:stale"] == 1
        second = await restarted.async_refresh_engineering_inventory(force=True)
        assert MemoryStore.data[maintenance_key]["missing_observations"]["serial-a:stale"] == 2
        assert MemoryStore.data[maintenance_key]["last_counted_engineering_generation"] == second.generation_id

    asyncio.run(scenario())


@pytest.mark.parametrize("force", [False, True])
def test_partial_registry_failure_blocks_new_read_and_publication(transaction, force):
    async def scenario():
        coordinator = transaction.make()
        transaction.registries.devices.fail_mutation_number = 2
        with pytest.raises(RuntimeError):
            await coordinator.async_refresh_engineering_inventory(force=force)
        state = await async_load_engineering_state(coordinator.hass, "entry-a")
        assert state.snapshot is not None
        assert state.registry_applied_generation is None
        assert "signal" not in MemoryStore.events
        assert "maintenance" not in MemoryStore.events
        assert len(transaction.notifications) == 1
        transaction.registries.devices.fail_mutation_number = transaction.registries.devices.mutations + 1
        with pytest.raises(RuntimeError):
            await coordinator.async_refresh_engineering_inventory(force=force)
        assert transaction.calls.count("download") == 1
        assert (
            await async_load_engineering_state(coordinator.hass, "entry-a")
        ).snapshot.generation_id == state.snapshot.generation_id

    asyncio.run(scenario())


def test_cold_restore_recreates_matching_publication_cursor_only_while_applicable(transaction, monkeypatch):
    from homeassistant.components.search import ItemType

    from custom_components.loxone.engineering_changes import EngineeringEntityImpact, EngineeringImpactPlan
    from custom_components.loxone.engineering_snapshot import async_store_engineering_state

    references = {ItemType.AUTOMATION: {"automation.rule"}}

    class Search:
        def __init__(self, *args):
            pass

        def async_search(self, *args):
            return references

    monkeypatch.setattr("custom_components.loxone.config_impact.Searcher", Search)

    async def scenario():
        first = transaction.make()
        await first.async_refresh_engineering_inventory(force=True)
        entity = transaction.registries.entities.add(
            domain="sensor", platform="loxone", unique_id="removed-channel", config_entry_id="entry-a", device_id=None
        )
        state = await async_load_engineering_state(first.hass, "entry-a")
        plan = EngineeringImpactPlan(
            state.snapshot.generation_id,
            (
                EngineeringEntityImpact(
                    "removed-channel", (entity.entity_id,), "removed", {"automation": ("automation.rule",)}
                ),
            ),
        )
        await async_store_engineering_state(first.hass, replace(state, pending_impact_plan=plan))

        async def rebind(self):
            pass

        monkeypatch.setattr(module.LoxoneCoordinator, "async_rebind_engineering_runtime", rebind)
        transaction.notifications.clear()
        restarted = transaction.make()
        await restarted.async_restore_engineering_snapshot()
        key = "loxone_engineering_impact_entry-a_serial-a"
        assert key in transaction.notifications
        transaction.notifications.clear()  # User dismissal lasts this process.
        await restarted.async_refresh_engineering_inventory()
        assert key not in transaction.notifications
        await transaction.make().async_restore_engineering_snapshot()
        assert key in transaction.notifications
        references.clear()
        await transaction.make().async_restore_engineering_snapshot()
        assert key not in transaction.notifications

    asyncio.run(scenario())


@pytest.mark.parametrize("after_publish", [False, True])
def test_actual_task_cancellation_at_publication_recovers_cold(transaction, monkeypatch, after_publish):
    async def scenario():
        original_publish = module.async_publish_engineering_impact_plan
        reached = asyncio.Event()

        async def interrupted(*args, **kwargs):
            if after_publish:
                await original_publish(*args, **kwargs)
            reached.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(module, "async_publish_engineering_impact_plan", interrupted)
        coordinator = transaction.make()
        task = asyncio.create_task(coordinator.async_refresh_engineering_inventory(force=True))
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = await async_load_engineering_state(coordinator.hass, "entry-a")
        generation = state.snapshot.generation_id
        assert state.registry_applied_generation == generation
        assert state.impact_published_generation != generation
        assert "maintenance" not in MemoryStore.events
        older = RegistryHarness()
        monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: older.devices)
        monkeypatch.setattr("homeassistant.helpers.area_registry.async_get", lambda hass: older.areas)
        monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: older.entities)
        transaction.notifications.clear()
        monkeypatch.setattr(module, "async_publish_engineering_impact_plan", original_publish)

        async def rebind(self):
            pass

        monkeypatch.setattr(module.LoxoneCoordinator, "async_rebind_engineering_runtime", rebind)
        restarted = transaction.make()
        await restarted.async_restore_engineering_snapshot()
        assert older.device("serial-a:device") is not None
        assert (await async_load_engineering_state(restarted.hass, "entry-a")).impact_published_generation == generation

    asyncio.run(scenario())


def test_partial_registry_cancellation_replays_after_cold_registry_restore(transaction, monkeypatch):
    async def scenario():
        devices = transaction.registries.devices
        mutate = devices._mutate

        def cancel_second_mutation():
            if devices.mutations == 1:
                raise asyncio.CancelledError
            mutate()

        monkeypatch.setattr(devices, "_mutate", cancel_second_mutation)
        first = transaction.make()
        with pytest.raises(asyncio.CancelledError):
            await first.async_refresh_engineering_inventory(force=True)
        state = await async_load_engineering_state(first.hass, "entry-a")
        assert devices.mutations == 1
        assert state.snapshot is not None
        assert state.registry_applied_generation is None
        assert "signal" not in MemoryStore.events
        assert "maintenance" not in MemoryStore.events
        older = RegistryHarness()
        monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: older.devices)
        monkeypatch.setattr("homeassistant.helpers.area_registry.async_get", lambda hass: older.areas)
        monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: older.entities)
        transaction.notifications.clear()

        async def rebind(self):
            pass

        monkeypatch.setattr(module.LoxoneCoordinator, "async_rebind_engineering_runtime", rebind)
        restarted = transaction.make()
        await restarted.async_restore_engineering_snapshot()
        assert older.device("serial-a:device") is not None
        recovered = await async_load_engineering_state(restarted.hass, "entry-a")
        assert recovered.registry_applied_generation == state.snapshot.generation_id
        assert recovered.impact_published_generation == state.snapshot.generation_id

    asyncio.run(scenario())


def test_restore_rejects_provider_mismatch(transaction):
    async def scenario():
        coordinator = transaction.make()
        await coordinator.async_refresh_engineering_inventory(force=True)
        restarted = transaction.make()
        restarted.miniserver.serial = "serial-b"
        before = transaction.registries.mutations
        with pytest.raises(ValueError):
            await restarted.async_restore_engineering_snapshot()
        assert restarted.engineering_snapshot is None
        assert transaction.registries.mutations == before

    asyncio.run(scenario())


def test_debounce_and_cleanup_cancel_and_await_task(transaction, monkeypatch):
    async def scenario():
        coordinator = transaction.make()
        await coordinator.async_schedule_engineering_refresh(delay=60)
        first = coordinator._engineering_refresh_task
        await coordinator.async_schedule_engineering_refresh(delay=60)
        second = coordinator._engineering_refresh_task
        assert first is not second
        assert first.done()
        await coordinator.async_cleanup()
        assert second.done()
        assert not transaction.calls

    asyncio.run(scenario())


def test_concurrent_schedulers_leave_only_one_owned_task(transaction):
    async def scenario():
        coordinator = transaction.make()
        await coordinator.async_schedule_engineering_refresh(delay=60)
        tasks = [coordinator._engineering_refresh_task]

        async def schedule():
            await coordinator.async_schedule_engineering_refresh(delay=60)
            tasks.append(coordinator._engineering_refresh_task)

        await asyncio.gather(schedule(), schedule(), schedule())
        try:
            assert sum(not task.done() for task in set(tasks)) == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_candidate_persists_foreign_entity_identity_as_inventory_only(transaction):
    async def scenario():
        foreign = transaction.registries.entities.add(
            domain="sensor", platform="loxone", unique_id="channel", config_entry_id="entry-b", device_id=None
        )
        coordinator = transaction.make()
        snapshot = await coordinator.async_refresh_engineering_inventory(force=True)
        row = next(row for row in snapshot.rows if row.node.element.uuid == "channel")
        assert row.capability.exposure.value == "inventory_only"
        assert row.capability.reason == "entity_unique_id_owned_by_other_entry"
        assert foreign.config_entry_id == "entry-b"
        assert foreign.device_id is None

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [200, 401, 503])
def test_unchanged_rebind_uses_only_persisted_descriptor(transaction, monkeypatch, status):
    async def scenario():
        coordinator = transaction.make()
        snapshot = await coordinator.async_refresh_engineering_inventory(force=True)
        session = _Session([(status, b'<LL Code="200" u1="channel-state" v1="9"/>')])
        client = replace(coordinator._engineering_runtime_client(), session=session, base_url="")
        monkeypatch.setattr(coordinator, "_engineering_runtime_client", lambda: client)
        before = deepcopy(MemoryStore.data)
        await coordinator.async_refresh_engineering_inventory()
        assert transaction.calls.count("download") == 1
        assert session.targets == ["/dev/sps/io/channel/all"]
        assert MemoryStore.data == before
        assert coordinator.engineering_snapshot.generation_id == snapshot.generation_id
        if status == 200:
            assert coordinator.engineering_runtime.bindings[0].numeric_value == 9.0
        else:
            assert all(item.status != "bound" for item in coordinator.engineering_runtime.bindings)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid", ["empty", "cycle", "duplicate", "auth_error", "transport_error", "malformed_response", "save"]
)
def test_failed_candidate_preserves_prior_memory_and_registry(transaction, monkeypatch, invalid):
    async def scenario():
        coordinator = transaction.make()
        original = await coordinator.async_refresh_engineering_inventory(force=True)
        original_runtime = coordinator.engineering_runtime
        baseline = deepcopy(MemoryStore.data)
        mutations = transaction.registries.mutations
        if invalid in {"empty", "cycle", "duplicate"}:
            from tests.engineering_fixtures import cyclic_inventory

            candidate = {
                "empty": inventory_of(),
                "cycle": cyclic_inventory(),
                "duplicate": inventory_of(element("ms", "LoxLIVE", room=None), element("ms", "TreeDevice", room=None)),
            }[invalid]

            async def download(self):
                return candidate

            monkeypatch.setattr(module.LoxoneCoordinator, "_async_download_engineering_inventory", download)
        elif invalid == "save":
            MemoryStore.fault = "candidate"
        else:

            async def probe(resolved, *, client):
                return EngineeringRuntimeInventory((replace(numeric_binding("channel", 1), status=invalid),))

            monkeypatch.setattr(module, "async_probe_engineering_runtime", probe)
        with pytest.raises((ValueError, asyncio.CancelledError)):
            await coordinator.async_refresh_engineering_inventory(force=True)
        assert coordinator.engineering_snapshot is original
        assert coordinator.engineering_runtime is original_runtime
        assert MemoryStore.data == baseline
        assert transaction.registries.mutations == mutations

    asyncio.run(scenario())


def test_button_forces_complete_read_and_only_exposes_counts(transaction, monkeypatch):
    from custom_components.loxone.button import LoxoneEngineeringInventoryButton

    async def scenario():
        coordinator = transaction.make()
        await coordinator.async_refresh_engineering_inventory(force=True)
        button = LoxoneEngineeringInventoryButton(coordinator.config_entry, coordinator)
        button.hass = coordinator.hass
        monkeypatch.setattr(button, "async_write_ha_state", lambda: None)
        await button.async_press()
        assert transaction.calls.count("download") == 2
        assert coordinator.engineering_snapshot.read_sequence == 2
        assert set(button.extra_state_attributes) == {"status", "node_count", "prepared_count", "bound_count"}

    asyncio.run(scenario())


def test_button_error_does_not_expose_exception(transaction, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.loxone.button import LoxoneEngineeringInventoryButton

    async def scenario():
        coordinator = transaction.make()

        async def download(self):
            raise RuntimeError("PRIVATE_MARKER")

        monkeypatch.setattr(module.LoxoneCoordinator, "_async_download_engineering_inventory", download)
        button = LoxoneEngineeringInventoryButton(coordinator.config_entry, coordinator)
        button.hass = coordinator.hass
        monkeypatch.setattr(button, "async_write_ha_state", lambda: None)
        with pytest.raises(HomeAssistantError) as caught:
            await button.async_press()
        assert "PRIVATE_MARKER" not in str(caught.value)
        assert button.extra_state_attributes == {"status": "error", "error_stage": "download"}
        assert all("PRIVATE_MARKER" not in text for text in transaction.notifications.values())

    asyncio.run(scenario())


def test_setup_restores_before_platforms_and_schedules_after(monkeypatch):
    import custom_components.loxone as integration

    events = []

    class Finished(Exception):
        pass

    async def first_refresh():
        pass

    async def restore():
        assert hass.data["loxone"]["entry-a"] is coordinator
        events.append("restore")

    async def forward(entry, platforms):
        events.append("forward")

    async def schedule():
        events.append("schedule")

    async def prepare_view(_hass):
        pass

    def finish(*args):
        raise Finished

    coordinator = SimpleNamespace(
        async_config_entry_first_refresh=first_refresh,
        async_restore_engineering_snapshot=restore,
        async_schedule_engineering_refresh=schedule,
        miniserver=SimpleNamespace(serial="serial-a", lox_config=SimpleNamespace(json={})),
    )
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_forward_entry_setups=forward))
    entry = SimpleNamespace(entry_id="entry-a", options={"host": "", "port": 0})
    monkeypatch.setattr(integration, "LoxoneCoordinator", lambda *args: coordinator)
    monkeypatch.setattr(integration, "async_migrate_version_sensor_unique_id", lambda *args: 0)
    monkeypatch.setattr(integration, "async_prepare_engineering_view", prepare_view)
    monkeypatch.setattr(integration, "LOXONE_PLATFORMS", ())
    monkeypatch.setattr(integration, "async_warn_about_config_impacts", finish)
    with pytest.raises(Finished):
        asyncio.run(integration.async_setup_entry(hass, entry))
    assert events == ["restore", "forward", "schedule"]
