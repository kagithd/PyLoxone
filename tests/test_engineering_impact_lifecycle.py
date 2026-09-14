"""Unresolved consumer evidence survives later engineering generations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from homeassistant.components.search import ItemType

import custom_components.loxone.config_impact as impacts_module
import custom_components.loxone.coordinator as coordinator_module
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from custom_components.loxone.engineering_snapshot import async_load_engineering_state
from tests.engineering_fixtures import element, inventory_of, numeric_binding
from tests.test_engineering_coordinator import MemoryStore, transaction  # noqa: F401
from tests.test_engineering_registry import RegistryHarness, registries  # noqa: F401
from tests.test_engineering_runtime import _Session

NOTIFICATION_ID = "loxone_engineering_impact_entry-a_serial-a"


class ImpactLifecycle:
    """Use the real transaction with synthetic transport/search boundary data."""

    def __init__(self, harness, monkeypatch):
        self.harness = harness
        self.inventory = harness.inventory
        self.references = {}
        self.publications = []
        lifecycle = self

        class Search:
            def __init__(self, *args):
                pass

            def async_search(self, kind, identifier):
                assert kind == ItemType.ENTITY
                return lifecycle.references.get(identifier, {})

        async def download(coordinator):
            return self.inventory

        async def probe(resolved, *, client):
            return EngineeringRuntimeInventory(
                tuple(
                    numeric_binding(node.element.uuid, 7.0, node.element.loxone_type)
                    for node in resolved.nodes
                    if node.element.loxone_type == "VoltageIn"
                )
            )

        original_publish = coordinator_module.async_publish_engineering_impact_plan

        async def publish(*args, **kwargs):
            await original_publish(*args, **kwargs)
            self.publications.append(args[3].impacts)

        monkeypatch.setattr(impacts_module, "Searcher", Search)
        monkeypatch.setattr(coordinator_module.LoxoneCoordinator, "_async_download_engineering_inventory", download)
        monkeypatch.setattr(coordinator_module, "async_probe_engineering_runtime", probe)
        monkeypatch.setattr(coordinator_module, "async_publish_engineering_impact_plan", publish)
        monkeypatch.setattr(
            coordinator_module,
            "async_get_clientsession",
            lambda hass: _Session([(200, b'<LL Code="200" u1="channel-state" v1="7"/>')] * 8),
        )

    def configuration(self, *channels, unrelated=False):
        nodes = list(self.harness.inventory.elements[:2])
        nodes.extend(
            element(uuid, type_name, parent_uuid="device", io_name="AI1", room=None) for uuid, type_name in channels
        )
        if unrelated:
            nodes.append(element("device-2", "TreeDevice", parent_uuid="ms", title="ST-F02", room=None))
        self.inventory = inventory_of(*nodes)

    def register(self, uuid):
        entity = self.harness.registries.entities.add(
            domain="sensor",
            platform="loxone",
            unique_id=uuid,
            config_entry_id="entry-a",
            device_id=None,
        )
        self.references[entity.entity_id] = {ItemType.AUTOMATION: {"automation.rule"}}
        return entity

    async def state(self, coordinator):
        return await async_load_engineering_state(coordinator.hass, "entry-a")

    async def seed_removal(self):
        coordinator = self.harness.make()
        await coordinator.async_refresh_engineering_inventory(force=True)
        self.register("channel")
        self.configuration()
        await coordinator.async_refresh_engineering_inventory(force=True)
        assert NOTIFICATION_ID in self.harness.notifications
        assert len((await self.state(coordinator)).pending_impact_plan.impacts) == 1
        return coordinator


@pytest.mark.parametrize("dismiss", [False, True])
def test_unresolved_impacts_survive_forced_rereads_unrelated_changes_and_cold_start(transaction, monkeypatch, dismiss):  # noqa: F811
    async def scenario():
        lifecycle = ImpactLifecycle(transaction, monkeypatch)
        coordinator = await lifecycle.seed_removal()
        previous = await lifecycle.state(coordinator)
        publications = len(lifecycle.publications)
        if dismiss:
            transaction.notifications.pop(NOTIFICATION_ID)
        for unrelated in (False, False, True):
            lifecycle.configuration(unrelated=unrelated)
            await coordinator.async_refresh_engineering_inventory(force=True)
            current = await lifecycle.state(coordinator)
            assert current.snapshot.read_sequence == previous.snapshot.read_sequence + 1
            assert current.pending_impact_plan.generation_id == current.snapshot.generation_id
            assert len(current.pending_impact_plan.impacts) == 1
            assert current.pending_impact_plan.impacts[0].unique_id == "channel"
            assert (NOTIFICATION_ID in transaction.notifications) is not dismiss
            assert len(lifecycle.publications) == publications
            previous = current
        # Integration storage survives; HA delayed registries and notifications do not.
        older = RegistryHarness()
        older.entities = transaction.registries.entities
        monkeypatch.setattr("homeassistant.helpers.device_registry.async_get", lambda hass: older.devices)
        monkeypatch.setattr("homeassistant.helpers.area_registry.async_get", lambda hass: older.areas)
        monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: older.entities)
        transaction.notifications.clear()
        restarted = transaction.make()
        await restarted.async_restore_engineering_snapshot()
        assert older.device("serial-a:device") is not None
        assert NOTIFICATION_ID in transaction.notifications
        assert len(lifecycle.publications) == publications + 1
        assert (await lifecycle.state(restarted)).pending_impact_plan.impacts == previous.pending_impact_plan.impacts
        await restarted.async_refresh_engineering_inventory()
        assert len(lifecycle.publications) == publications + 1

    asyncio.run(scenario())


@pytest.mark.parametrize("resolution", ["restored", "reference_removed", "other_entry", "other_integration"])
def test_carried_impact_is_removed_only_when_condition_or_scoped_reference_resolves(
    transaction,  # noqa: F811 -- imported shared pytest fixture.
    monkeypatch,
    resolution,
):
    async def scenario():
        lifecycle = ImpactLifecycle(transaction, monkeypatch)
        coordinator = await lifecycle.seed_removal()
        for _ in range(2):
            await coordinator.async_refresh_engineering_inventory(force=True)
            assert len((await lifecycle.state(coordinator)).pending_impact_plan.impacts) == 1
        entry = next(iter(transaction.registries.entities.entities.values()))
        if resolution == "restored":
            lifecycle.configuration(("channel", "VoltageIn"))
        elif resolution == "reference_removed":
            lifecycle.references.clear()
        elif resolution == "other_entry":
            entry.config_entry_id = "entry-b"
        else:
            entry.platform = "other"
        await coordinator.async_refresh_engineering_inventory(force=True)
        assert (await lifecycle.state(coordinator)).pending_impact_plan.impacts == ()
        assert NOTIFICATION_ID not in transaction.notifications
        await transaction.make().async_restore_engineering_snapshot()
        assert NOTIFICATION_ID not in transaction.notifications

    asyncio.run(scenario())


def test_mixed_old_new_impacts_deduplicate_and_resolve_independently(transaction, monkeypatch):  # noqa: F811
    async def scenario():
        lifecycle = ImpactLifecycle(transaction, monkeypatch)
        lifecycle.configuration(("channel", "VoltageIn"), ("channel-2", "VoltageIn"))
        coordinator = transaction.make()
        await coordinator.async_refresh_engineering_inventory(force=True)
        lifecycle.register("channel")
        lifecycle.register("channel-2")
        lifecycle.configuration(("channel-2", "VoltageIn"))
        await coordinator.async_refresh_engineering_inventory(force=True)
        lifecycle.configuration()
        await coordinator.async_refresh_engineering_inventory(force=True)
        assert tuple(item.unique_id for item in (await lifecycle.state(coordinator)).pending_impact_plan.impacts) == (
            "channel",
            "channel-2",
        )
        lifecycle.configuration(("channel", "VoltageIn"))
        await coordinator.async_refresh_engineering_inventory(force=True)
        assert tuple(item.unique_id for item in (await lifecycle.state(coordinator)).pending_impact_plan.impacts) == (
            "channel-2",
        )
        assert "1 engineering" in transaction.notifications[NOTIFICATION_ID]
        await coordinator.async_refresh_engineering_inventory(force=True)
        assert tuple(item.unique_id for item in (await lifecycle.state(coordinator)).pending_impact_plan.impacts) == (
            "channel-2",
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["candidate", "candidate_after", "published", "published_after"])
def test_carried_evidence_survives_save_cancellation_and_dismissal(transaction, monkeypatch, phase):  # noqa: F811
    async def scenario():
        lifecycle = ImpactLifecycle(transaction, monkeypatch)
        coordinator = await lifecycle.seed_removal()
        baseline = deepcopy(MemoryStore.data)
        publications = len(lifecycle.publications)
        transaction.notifications.pop(NOTIFICATION_ID)
        MemoryStore.fault = phase
        with pytest.raises(asyncio.CancelledError):
            await coordinator.async_refresh_engineering_inventory(force=True)
        if phase == "candidate":
            assert MemoryStore.data == baseline
        MemoryStore.fault = None
        assert len((await lifecycle.state(coordinator)).pending_impact_plan.impacts) == 1
        await coordinator.async_drain_committed_engineering_state()
        assert len(lifecycle.publications) == publications
        assert NOTIFICATION_ID not in transaction.notifications
        transaction.notifications.clear()
        await transaction.make().async_restore_engineering_snapshot()
        assert NOTIFICATION_ID in transaction.notifications

    asyncio.run(scenario())


def test_platform_change_evidence_resolves_only_when_compatible_platform_returns(transaction, monkeypatch):  # noqa: F811
    async def scenario():
        lifecycle = ImpactLifecycle(transaction, monkeypatch)
        coordinator = transaction.make()
        await coordinator.async_refresh_engineering_inventory(force=True)
        lifecycle.register("channel")
        lifecycle.configuration(("channel", "DigitalIn"))
        for _ in range(3):
            await coordinator.async_refresh_engineering_inventory(force=True)
            plan = (await lifecycle.state(coordinator)).pending_impact_plan
            assert len(plan.impacts) == 1
            assert plan.impacts[0].change_kind == "platform_changed"
        await transaction.make().async_restore_engineering_snapshot()
        assert NOTIFICATION_ID in transaction.notifications
        lifecycle.configuration(("channel", "VoltageIn"))
        await coordinator.async_refresh_engineering_inventory(force=True)
        assert (await lifecycle.state(coordinator)).pending_impact_plan.impacts == ()
        assert NOTIFICATION_ID not in transaction.notifications

    asyncio.run(scenario())


@pytest.mark.parametrize("after_publish", [False, True])
def test_publication_cancellation_keeps_mixed_old_and_new_impacts(transaction, monkeypatch, after_publish):  # noqa: F811
    async def scenario():
        lifecycle = ImpactLifecycle(transaction, monkeypatch)
        lifecycle.configuration(("channel", "VoltageIn"), ("channel-2", "VoltageIn"))
        coordinator = transaction.make()
        await coordinator.async_refresh_engineering_inventory(force=True)
        lifecycle.register("channel")
        lifecycle.register("channel-2")
        lifecycle.configuration(("channel-2", "VoltageIn"))
        await coordinator.async_refresh_engineering_inventory(force=True)
        lifecycle.configuration()
        publish = coordinator_module.async_publish_engineering_impact_plan
        reached = asyncio.Event()

        async def interrupted(*args, **kwargs):
            if after_publish:
                await publish(*args, **kwargs)
            reached.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(coordinator_module, "async_publish_engineering_impact_plan", interrupted)
        task = asyncio.create_task(coordinator.async_refresh_engineering_inventory(force=True))
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = await lifecycle.state(coordinator)
        assert tuple(item.unique_id for item in state.pending_impact_plan.impacts) == ("channel", "channel-2")
        assert state.registry_applied_generation == state.snapshot.generation_id
        assert state.impact_published_generation != state.snapshot.generation_id
        monkeypatch.setattr(coordinator_module, "async_publish_engineering_impact_plan", publish)
        transaction.notifications.clear()
        restarted = transaction.make()
        await restarted.async_restore_engineering_snapshot()
        assert "2 engineering" in transaction.notifications[NOTIFICATION_ID]
        assert len([key for key in transaction.notifications if key.startswith("loxone_engineering_impact_")]) == 1
        await restarted.async_refresh_engineering_inventory(force=True)
        assert tuple(item.unique_id for item in (await lifecycle.state(restarted)).pending_impact_plan.impacts) == (
            "channel",
            "channel-2",
        )
        assert len([key for key in transaction.notifications if key.startswith("loxone_engineering_impact_")]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", ["entry", "provider", "generation"])
def test_prior_impact_evidence_requires_matching_source_and_generation(transaction, monkeypatch, mismatch):  # noqa: F811
    async def scenario():
        lifecycle = ImpactLifecycle(transaction, monkeypatch)
        coordinator = await lifecycle.seed_removal()
        state = await lifecycle.state(coordinator)
        candidate = state.snapshot
        plan = state.pending_impact_plan
        if mismatch == "generation":
            plan = replace(plan, generation_id="gen:" + "0" * 64)
        elif mismatch == "entry":
            candidate = replace(candidate, source=replace(candidate.source, entry_id="entry-b"))
        else:
            candidate = replace(candidate, source=replace(candidate.source, serial_number="serial-b"))
        baseline = deepcopy(MemoryStore.data)
        mutations = transaction.registries.mutations
        with pytest.raises(ValueError):
            await impacts_module.async_find_engineering_change_impacts(
                coordinator.hass,
                coordinator.config_entry,
                state.snapshot,
                candidate,
                plan,
            )
        assert MemoryStore.data == baseline
        assert transaction.registries.mutations == mutations

    asyncio.run(scenario())
