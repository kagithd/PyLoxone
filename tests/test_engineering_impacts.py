"""Bounded engineering impact discovery and process-local notification replay."""

import asyncio
from types import SimpleNamespace

from homeassistant.components.search import ItemType

import custom_components.loxone.config_impact as module
from custom_components.loxone.engineering_changes import EngineeringEntityImpact, EngineeringImpactPlan
from tests.engineering_fixtures import element, inventory_of, make_snapshot
from tests.test_engineering_registry import registries  # noqa: F401


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
        assert "automation.rule" not in message
        assert len(message) < 400
        await module.async_publish_engineering_impact_plan(
            None, entry, snapshot.source.provider_identifier, EngineeringImpactPlan(snapshot.generation_id, ())
        )
        assert not notifications

    asyncio.run(scenario())
