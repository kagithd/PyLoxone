"""Bounded engineering impact discovery and process-local notification replay."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.components.search import ItemType

import custom_components.loxone.config_impact as module
from custom_components.loxone.engineering_changes import EngineeringEntityImpact, EngineeringImpactPlan
from custom_components.loxone.engineering_registry import (
    EngineeringRegistryMetadata,
    async_plan_engineering_registry_sync,
)
from tests.engineering_fixtures import element, inventory_of, make_snapshot
from tests.test_engineering_registry import FakeIntentStore, registries  # noqa: F401


def test_impact_discovery_reads_pre_mutation_entity_consumers(registries, monkeypatch):  # noqa: F811
    previous = make_snapshot()
    current = make_snapshot(inventory=inventory_of(element("ms", "LoxLIVE", room=None)))
    removed = next(row for row in previous.rows if row.node.element.uuid and row.semantic_platform == "sensor")
    registries.entities.add(
        domain="sensor",
        platform="loxone",
        unique_id=removed.node.element.uuid,
        config_entry_id="entry-a",
        device_id=None,
    )
    monkeypatch.setattr(module, "entity_sources", lambda hass: {})

    class Search:
        def __init__(self, *args):
            pass

        def async_search(self, kind, identifier):
            assert kind == ItemType.ENTITY
            assert identifier == "sensor.entity_1"
            return {ItemType.AUTOMATION: {"automation.rule"}}

    monkeypatch.setattr(module, "Searcher", Search)
    before = registries.mutations
    assert hasattr(module, "async_find_engineering_change_impacts")
    plan = asyncio.run(
        module.async_find_engineering_change_impacts(
            registries.hass, SimpleNamespace(entry_id="entry-a"), previous, current
        )
    )
    assert len(plan.impacts) == 1
    assert plan.impacts[0].entity_ids == ("sensor.entity_1",)
    assert plan.impacts[0].references == {"automation": ("automation.rule",)}
    assert registries.mutations == before


def test_impact_publication_is_bounded_idempotent_and_dismissible(monkeypatch):
    notifications = {}
    monkeypatch.setattr(
        module.persistent_notification,
        "async_create",
        lambda hass, message, *, title, notification_id: notifications.update({notification_id: message}),
    )
    monkeypatch.setattr(
        module.persistent_notification,
        "async_dismiss",
        lambda hass, notification_id: notifications.pop(notification_id, None),
    )
    snapshot = make_snapshot()
    plan = EngineeringImpactPlan(
        snapshot.generation_id,
        (EngineeringEntityImpact("channel", ("sensor.channel",), "removed", {"automation": ("automation.rule",)}),),
    )
    assert hasattr(module, "async_publish_engineering_impact_plan")

    async def scenario():
        entry = SimpleNamespace(entry_id="entry-a")
        for _ in range(2):
            await module.async_publish_engineering_impact_plan(None, entry, snapshot.source.provider_identifier, plan)
        assert len(notifications) == 1
        message = next(iter(notifications.values()))
        assert "1" in message
        assert "sensor.channel" not in message
        assert "automation.rule" in message
        assert len(message) < 400
        await module.async_publish_engineering_impact_plan(
            None, entry, snapshot.source.provider_identifier, EngineeringImpactPlan(snapshot.generation_id, ())
        )
        assert not notifications

    asyncio.run(scenario())


def test_room_change_captures_only_area_targeted_consumers_before_mutation(
    registries,  # noqa: F811
    monkeypatch,
):
    """A real Task 5 owner move captures old/new area consumers in advance."""
    office = registries.areas.async_get_or_create("Office")
    workshop = registries.areas.async_get_or_create("Workshop")
    registries.devices.add(
        "serial-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    FakeIntentStore.data = {
        "managed_baselines": [
            {
                "identifier": "serial-a:device",
                "area_id": office.id,
                "process_token": "process-a",
            }
        ]
    }
    previous = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07", room="Office"),
        )
    )
    current = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07", room="Workshop"),
        ),
        read_sequence=2,
    )
    monkeypatch.setattr(
        module.automation,
        "automations_with_area",
        lambda hass, area_id: {"automation.office_rule"} if area_id == "area-1" else set(),
    )
    monkeypatch.setattr(
        module.script,
        "scripts_with_area",
        lambda hass, area_id: {"script.workshop_rule"} if area_id == "area-2" else set(),
    )
    before = registries.mutations

    async def scenario():
        registry_plan = await async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            current,
            EngineeringRegistryMetadata(
                frozenset({"serial-a:device"}),
                frozenset({"Office"}),
                {"serial-a:device": office.id},
            ),
        )
        assert registry_plan.device_operations[1].area_update_allowed
        return await module.async_find_engineering_change_impacts(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            previous,
            current,
            registry_plan=registry_plan,
        )

    plan = asyncio.run(scenario())

    assert len(plan.impacts) == 1
    impact = plan.impacts[0]
    assert impact.unique_id == "serial-a:device"
    assert impact.change_kind == "area_changed"
    assert impact.entity_ids == ()
    assert impact.target_area_ids == ("area-1", "area-2")
    assert impact.area_from_id == office.id
    assert impact.area_to_id == workshop.id
    assert impact.area_to_name == "Workshop"
    assert impact.references == {
        "automation": ("automation.office_rule",),
        "script": ("script.workshop_rule",),
    }
    assert registries.mutations == before


def test_area_impact_rejects_registry_plan_from_another_generation(
    registries,  # noqa: F811
    monkeypatch,
):
    """Pre-mutation owner evidence must belong to the candidate generation."""
    office = registries.areas.async_get_or_create("Office")
    registries.devices.add("serial-a:device", "entry-a", area_id=office.id, name="ST-F07")
    FakeIntentStore.data = {
        "managed_baselines": [
            {
                "identifier": "serial-a:device",
                "area_id": office.id,
                "process_token": "process-a",
            }
        ]
    }
    previous = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "TreeDevice", parent_uuid="ms", room="Office"),
        )
    )
    current = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "TreeDevice", parent_uuid="ms", room="Workshop"),
        ),
        read_sequence=2,
    )
    monkeypatch.setattr(
        module.automation,
        "automations_with_area",
        lambda *_: {"automation.office_rule"},
    )
    monkeypatch.setattr(module.script, "scripts_with_area", lambda *_: set())

    async def scenario():
        plan = await async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            current,
            EngineeringRegistryMetadata(
                frozenset({"serial-a:device"}),
                frozenset({"Office"}),
                {"serial-a:device": office.id},
            ),
        )
        with pytest.raises(ValueError, match="source generation"):
            await module.async_find_engineering_change_impacts(
                registries.hass,
                SimpleNamespace(entry_id="entry-a"),
                previous,
                current,
                registry_plan=replace(plan, generation_id="f" * 64),
            )
        inactive = await module.async_find_engineering_change_impacts(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            previous,
            current,
            registry_plan=replace(
                plan,
                metadata=EngineeringRegistryMetadata.empty(),
            ),
        )
        assert inactive.impacts == ()

    asyncio.run(scenario())


def test_unreferenced_room_change_does_not_create_an_impact(registries, monkeypatch):  # noqa: F811
    """A metadata-only room move remains silent when no area target is affected."""
    registries.areas.async_get_or_create("Office")
    registries.areas.async_get_or_create("Workshop")
    previous = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07", room="Office"),
        )
    )
    current = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07", room="Workshop"),
        ),
        read_sequence=2,
    )
    monkeypatch.setattr(module.automation, "automations_with_area", lambda hass, area_id: set())
    monkeypatch.setattr(module.script, "scripts_with_area", lambda hass, area_id: set())

    plan = asyncio.run(
        module.async_find_engineering_change_impacts(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            previous,
            current,
        )
    )

    assert plan.impacts == ()


def test_channel_room_metadata_never_claims_an_owner_area_move(
    registries,  # noqa: F811
    monkeypatch,
):
    """A child-channel room edit cannot fabricate a device-area impact."""
    office = registries.areas.async_get_or_create("Office")
    registries.areas.async_get_or_create("Workshop")
    registries.devices.add("serial-a:device", "entry-a", area_id=office.id, name="ST-F07")
    previous = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "TreeDevice", parent_uuid="ms", room="Office"),
            element("channel", "VoltageIn", parent_uuid="device", room="Office"),
        )
    )
    current = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "TreeDevice", parent_uuid="ms", room="Office"),
            element("channel", "VoltageIn", parent_uuid="device", room="Workshop"),
        ),
        read_sequence=2,
    )
    monkeypatch.setattr(
        module.automation,
        "automations_with_area",
        lambda hass, area_id: {"automation.office_rule"},
    )
    monkeypatch.setattr(module.script, "scripts_with_area", lambda hass, area_id: set())

    async def scenario():
        registry_plan = await async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            current,
            EngineeringRegistryMetadata.empty(),
        )
        return await module.async_find_engineering_change_impacts(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            previous,
            current,
            registry_plan=registry_plan,
        )

    assert asyncio.run(scenario()).impacts == ()


def test_user_preserved_or_missing_owner_never_creates_area_impact(
    registries,  # noqa: F811
    monkeypatch,
):
    """Task 5 fail-closed authority and exact scoped ownership gate warnings."""
    office = registries.areas.async_get_or_create("Office")
    registries.areas.async_get_or_create("Workshop")
    previous = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "TreeDevice", parent_uuid="ms", room="Office"),
        )
    )
    current = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "TreeDevice", parent_uuid="ms", room="Workshop"),
        ),
        read_sequence=2,
    )
    monkeypatch.setattr(
        module.automation,
        "automations_with_area",
        lambda hass, area_id: {"automation.office_rule"},
    )
    monkeypatch.setattr(module.script, "scripts_with_area", lambda hass, area_id: set())

    async def discover():
        registry_plan = await async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            current,
            EngineeringRegistryMetadata.empty(),
        )
        return await module.async_find_engineering_change_impacts(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            previous,
            current,
            registry_plan=registry_plan,
        )

    assert asyncio.run(discover()).impacts == ()

    foreign = registries.devices.add("serial-a:device", "entry-b", area_id=office.id, name="ST-F07")
    assert asyncio.run(discover()).impacts == ()
    registries.devices.devices.pop(foreign.id)

    registries.devices.add("serial-a:device", "entry-a", area_id=office.id, name="ST-F07")
    assert asyncio.run(discover()).impacts == ()


def test_area_impact_carries_only_while_owner_transition_is_applied(
    registries,  # noqa: F811
    monkeypatch,
):
    """Cold replay validates the scoped owner at the captured destination."""
    office = registries.areas.async_get_or_create("Office")
    workshop = registries.areas.async_get_or_create("Workshop")
    device = registries.devices.add("serial-a:device", "entry-a", area_id=office.id, name="ST-F07")
    FakeIntentStore.data = {
        "managed_baselines": [
            {
                "identifier": "serial-a:device",
                "area_id": office.id,
                "process_token": "process-a",
            }
        ]
    }
    previous = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "TreeDevice", parent_uuid="ms", room="Office"),
        )
    )
    current = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("device", "TreeDevice", parent_uuid="ms", room="Workshop"),
        ),
        read_sequence=2,
    )
    monkeypatch.setattr(
        module.automation,
        "automations_with_area",
        lambda hass, area_id: {"automation.office_rule"} if area_id == office.id else set(),
    )
    monkeypatch.setattr(module.script, "scripts_with_area", lambda hass, area_id: set())
    dismissed = []
    monkeypatch.setattr(module, "entity_sources", lambda hass: {})
    monkeypatch.setattr(
        module.persistent_notification,
        "async_create",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("inapplicable area impact must not publish")),
    )
    monkeypatch.setattr(
        module.persistent_notification,
        "async_dismiss",
        lambda hass, notification_id: dismissed.append(notification_id),
    )

    async def scenario():
        registry_plan = await async_plan_engineering_registry_sync(
            registries.hass,
            "entry-a",
            current,
            EngineeringRegistryMetadata(
                frozenset({"serial-a:device"}),
                frozenset({"Office"}),
                {"serial-a:device": office.id},
            ),
        )
        first = await module.async_find_engineering_change_impacts(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            previous,
            current,
            registry_plan=registry_plan,
        )
        registries.devices.async_update_device(device.id, area_id=workshop.id)
        next_snapshot = make_snapshot(
            inventory=inventory_of(
                element("ms", "LoxLIVE", room=None),
                element("device", "TreeDevice", parent_uuid="ms", room="Workshop"),
            ),
            read_sequence=3,
        )
        carried = await module.async_find_engineering_change_impacts(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            current,
            next_snapshot,
            first,
        )
        assert len(carried.impacts) == 1
        registries.devices.async_update_device(device.id, area_id=office.id)
        restored = await module.async_find_engineering_change_impacts(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            next_snapshot,
            make_snapshot(
                inventory=inventory_of(
                    element("ms", "LoxLIVE", room=None),
                    element(
                        "device",
                        "TreeDevice",
                        parent_uuid="ms",
                        room="Workshop",
                    ),
                ),
                read_sequence=4,
            ),
            carried,
        )
        assert restored.impacts == ()

        registries.devices.devices.pop(device.id)
        await module.async_publish_engineering_impact_plan(
            registries.hass,
            SimpleNamespace(entry_id="entry-a"),
            current.source.provider_identifier,
            first,
            recheck=True,
        )
        assert dismissed == ["loxone_engineering_impact_entry-a_serial-a"]

    asyncio.run(scenario())


def test_impact_warning_lists_only_safe_consumer_entity_ids(monkeypatch):
    """The warning is actionable without exposing channel IDs or unsafe text."""
    notifications = {}
    monkeypatch.setattr(
        module.persistent_notification,
        "async_create",
        lambda hass, message, *, title, notification_id: notifications.update({notification_id: message}),
    )
    snapshot = make_snapshot()
    plan = EngineeringImpactPlan(
        snapshot.generation_id,
        (
            EngineeringEntityImpact(
                "private-channel",
                ("sensor.private_channel",),
                "removed",
                {
                    "automation": ("automation.office_button",),
                    "script": ("script.review",),
                    "person": ("person.hidden",),
                    "scene": ("https://example.invalid/unsafe",),
                },
            ),
        ),
    )

    asyncio.run(
        module.async_publish_engineering_impact_plan(
            None,
            SimpleNamespace(entry_id="entry-a"),
            snapshot.source.provider_identifier,
            plan,
        )
    )

    message = next(iter(notifications.values()))
    assert "automation.office_button" in message
    assert "script.review" in message
    assert "sensor.private_channel" not in message
    assert "private-channel" not in message
    assert "person.hidden" not in message
    assert "example.invalid" not in message
