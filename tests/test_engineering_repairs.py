"""Native Home Assistant Repairs for explicit engineering area choices."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.loxone.const import DOMAIN
from custom_components.loxone.engineering_registry import (
    EngineeringAreaConflict,
    EngineeringAreaResolutionResult,
)
from tests.test_engineering_registry import FakeIntentStore, registries  # noqa: F401


def _conflict(
    *,
    entry_id: str = "entry-a",
    token: str = "a" * 64,
    current_room: str | None = "Office",
    desired_room: str | None = "Workshop",
    current_area_id: str | None | object = Ellipsis,
    desired_area_id: str | None | object = Ellipsis,
    display_name: str | None = "ST-F07",
    desired_action_valid: bool = True,
) -> EngineeringAreaConflict:
    return EngineeringAreaConflict(
        token=token,
        entry_id=entry_id,
        device_identifier=f"serial-{entry_id}:device",
        display_name=display_name,
        current_area_id=("office" if current_room else None) if current_area_id is Ellipsis else current_area_id,
        current_area_name=current_room,
        desired_area_id=("workshop" if desired_room else None) if desired_area_id is Ellipsis else desired_area_id,
        desired_area_name=desired_room,
        generation_id="generation-a",
        reason="area_user_override_preserved",
        desired_action_valid=desired_action_valid,
    )


class _ConfigEntries:
    def __init__(self, *entry_ids: str) -> None:
        self._entries = {
            entry_id: SimpleNamespace(entry_id=entry_id, domain=DOMAIN, state=ConfigEntryState.LOADED)
            for entry_id in entry_ids
        }

    def async_get_entry(self, entry_id: str):
        return self._entries.get(entry_id)


class _IssueRegistry:
    def __init__(self) -> None:
        self.issues = {}


def _repairs_harness(monkeypatch, conflicts):
    from custom_components.loxone import repairs

    current = {entry_id: list(items) for entry_id, items in conflicts.items()}
    registry = _IssueRegistry()
    created = []
    deleted = []
    config_entries = _ConfigEntries(*current)
    hass = SimpleNamespace(config_entries=config_entries, data={DOMAIN: {}})
    unload_callbacks = {}
    for entry_id, entry in config_entries._entries.items():
        provider = f"serial-{entry_id}"
        coordinator = SimpleNamespace(
            config_entry=entry,
            engineering_snapshot=SimpleNamespace(source=SimpleNamespace(provider_identifier=provider)),
            miniserver=SimpleNamespace(serial=provider),
        )
        hass.data[DOMAIN][entry_id] = coordinator
        if hasattr(repairs, "async_register_engineering_area_conflict_reconciler"):
            callback = repairs.async_register_engineering_area_conflict_reconciler(hass, entry, coordinator)
            unload_callbacks[entry_id] = callback

    async def load(_hass, entry_id):
        return tuple(current.get(entry_id, ()))

    def create(_hass, domain, issue_id, **kwargs):
        created.append((domain, issue_id, kwargs))
        registry.issues[(domain, issue_id)] = SimpleNamespace(
            domain=domain,
            issue_id=issue_id,
            **kwargs,
        )

    def delete(_hass, domain, issue_id):
        deleted.append((domain, issue_id))
        registry.issues.pop((domain, issue_id), None)

    monkeypatch.setattr(repairs, "async_load_engineering_area_conflicts", load)
    monkeypatch.setattr(repairs.ir, "async_get", lambda _hass: registry)
    monkeypatch.setattr(repairs.ir, "async_create_issue", create)
    monkeypatch.setattr(repairs.ir, "async_delete_issue", delete)
    return SimpleNamespace(
        module=repairs,
        hass=hass,
        current=current,
        registry=registry,
        created=created,
        deleted=deleted,
        unload_callbacks=unload_callbacks,
    )


def test_issue_sync_is_entry_scoped_idempotent_and_minimal(monkeypatch):
    """Changing one entry or replaying sync must not duplicate or delete peers."""
    first = _conflict(entry_id="entry-a", token="a" * 64)
    second = _conflict(entry_id="entry-b", token="b" * 64)
    harness = _repairs_harness(monkeypatch, {"entry-a": [first], "entry-b": [second]})

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-b")
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")

    asyncio.run(scenario())

    issue_ids = {issue_id for domain, issue_id in harness.registry.issues if domain == DOMAIN}
    assert len(issue_ids) == 2
    assert all(first.token in issue_id or second.token in issue_id for issue_id in issue_ids)
    first_call = next(call for call in harness.created if first.token in call[1])
    assert first_call[2]["is_fixable"] is True
    assert first_call[2]["severity"].value == "warning"
    assert first_call[2]["data"] == {
        "kind": "engineering_area_conflict",
        "version": 1,
        "entry_id": "entry-a",
        "conflict_token": first.token,
    }
    assert first_call[2]["translation_placeholders"] == {"device": "ST-F07"}
    assert harness.deleted == []


def test_sync_replaces_changed_token_and_preserves_unrelated_issues(monkeypatch):
    """A genuine conflict change gets a new issue without touching other issues."""
    old = _conflict(token="a" * 64)
    current = _conflict(token="b" * 64, current_room="Living Room")
    harness = _repairs_harness(monkeypatch, {"entry-a": [old]})
    unrelated = (DOMAIN, "unrelated_issue")
    harness.registry.issues[unrelated] = SimpleNamespace()

    asyncio.run(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a"))
    harness.current["entry-a"] = [current]
    asyncio.run(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a"))

    ids = {key[1] for key in harness.registry.issues if key[0] == DOMAIN}
    assert "unrelated_issue" in ids
    assert any(current.token in issue_id for issue_id in ids)
    assert not any(old.token in issue_id for issue_id in ids)


def test_unchanged_sync_preserves_native_ignored_state(monkeypatch, tmp_path):
    """Recreating an exact issue must retain Home Assistant's dismissal marker."""
    from custom_components.loxone import repairs

    conflict = _conflict()

    async def load(_hass, entry_id):
        assert entry_id == "entry-a"
        return (conflict,)

    monkeypatch.setattr(repairs, "async_load_engineering_area_conflicts", load)

    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = _ConfigEntries("entry-a")
        entry = hass.config_entries._entries["entry-a"]
        coordinator = SimpleNamespace(
            config_entry=entry,
            engineering_snapshot=SimpleNamespace(source=SimpleNamespace(provider_identifier="serial-entry-a")),
            miniserver=SimpleNamespace(serial="serial-entry-a"),
        )
        hass.data[DOMAIN] = {"entry-a": coordinator}
        repairs.async_register_engineering_area_conflict_reconciler(hass, entry, coordinator)
        issue_id = repairs.engineering_area_conflict_issue_id("entry-a", conflict.token)
        await repairs.async_sync_engineering_area_conflict_issues(hass, "entry-a")
        ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
        dismissed_version = ir.async_get(hass).issues[(DOMAIN, issue_id)].dismissed_version

        await repairs.async_sync_engineering_area_conflict_issues(hass, "entry-a")

        assert dismissed_version is not None
        assert ir.async_get(hass).issues[(DOMAIN, issue_id)].dismissed_version == dismissed_version

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ("apply_loxone_room", "keep_ha_room"))
def test_flow_displays_safe_choices_and_resolves_exact_current_token(
    monkeypatch,
    action,
):
    """The form reloads safe labels and delegates one exact public resolution."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    calls = []

    async def resolve(_hass, entry_id, token, selected_action):
        calls.append((entry_id, token, selected_action))
        harness.current[entry_id] = []
        return EngineeringAreaResolutionResult(
            resolved=True,
            action=selected_action,
            reason="resolved",
            conflict=conflict,
        )

    monkeypatch.setattr(
        harness.module,
        "async_resolve_engineering_area_conflict",
        resolve,
    )
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        shown = await flow.async_step_init()
        assert shown["type"] is data_entry_flow.FlowResultType.FORM
        assert shown["description_placeholders"] == {
            "device": "ST-F07",
            "current_room": "Office",
            "desired_room": "Workshop",
        }
        assert shown["data_schema"]({"action": action}) == {"action": action}
        result = await flow.async_step_init({"action": action})
        assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY

    asyncio.run(scenario())
    assert calls == [("entry-a", conflict.token, action)]


def test_flow_presents_no_room_without_python_none(monkeypatch):
    """An explicit room clear selects a localized no-room flow variant."""
    conflict = _conflict(desired_room=None)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        shown = await flow.async_step_init()
        assert shown["step_id"] == "current_named_desired_none"
        assert "None" not in repr(shown["description_placeholders"])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("issue_id", "data", "reason"),
    [
        ("not-a-loxone-area-issue", None, "invalid_repair"),
        (
            "engineering_area_conflict_invalid",
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-b",
                "conflict_token": "a" * 64,
            },
            "invalid_repair",
        ),
    ],
)
def test_malformed_or_foreign_flow_aborts_without_resolution(
    monkeypatch,
    issue_id,
    data,
    reason,
):
    """Untrusted issue input cannot select another entry or resolver action."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    calls = []

    async def resolve(*args):
        calls.append(args)
        raise AssertionError("resolver must not run")

    monkeypatch.setattr(
        harness.module,
        "async_resolve_engineering_area_conflict",
        resolve,
    )

    async def scenario():
        flow = await harness.module.async_create_fix_flow(harness.hass, issue_id, data)
        flow.hass = harness.hass
        result = await flow.async_step_init()
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == reason

    asyncio.run(scenario())
    assert calls == []


def test_stale_or_concurrently_changed_flow_preserves_new_issue(monkeypatch):
    """A stale decision aborts while a retokenized actionable Repair remains."""
    old = _conflict(token="a" * 64)
    new = _conflict(token="b" * 64, current_room="Living Room")
    harness = _repairs_harness(monkeypatch, {"entry-a": [old]})
    calls = []

    async def resolve(_hass, entry_id, token, action):
        calls.append((entry_id, token, action))
        harness.current[entry_id] = [new]
        return EngineeringAreaResolutionResult(
            resolved=False,
            action=action,
            reason="stale_conflict",
            conflict=new,
        )

    monkeypatch.setattr(
        harness.module,
        "async_resolve_engineering_area_conflict",
        resolve,
    )
    old_issue = harness.module.engineering_area_conflict_issue_id("entry-a", old.token)

    async def scenario():
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            old_issue,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": old.token,
            },
        )
        flow.hass = harness.hass
        result = await flow.async_step_init({"action": "keep_ha_room"})
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "conflict_changed"

    asyncio.run(scenario())
    assert calls == [("entry-a", old.token, "keep_ha_room")]
    assert any(new.token in issue_id for _, issue_id in harness.registry.issues)


def test_resolver_failure_aborts_with_bounded_reason_and_keeps_issue(monkeypatch):
    """A storage/resolution failure must not escape the native Repairs flow."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})

    async def resolve(*args):
        del args
        raise RuntimeError("arbitrary private failure")

    monkeypatch.setattr(
        harness.module,
        "async_resolve_engineering_area_conflict",
        resolve,
    )
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        result = await flow.async_step_init({"action": "keep_ha_room"})
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "resolution_failed"

    asyncio.run(scenario())
    assert any(conflict.token in item for _, item in harness.registry.issues)


def test_invalid_desired_conflict_offers_only_safe_keep_action(monkeypatch):
    """Unsafe desired metadata remains truthful and offers only Keep HA."""
    conflict = _conflict(desired_action_valid=False)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        shown = await flow.async_step_init()
        assert shown["type"] is data_entry_flow.FlowResultType.FORM
        assert shown["step_id"] == "current_named_desired_invalid"
        assert shown["data_schema"]({"action": "keep_ha_room"}) == {"action": "keep_ha_room"}
        with pytest.raises(Exception):  # voluptuous rejects hidden Apply.
            shown["data_schema"]({"action": "apply_loxone_room"})

    asyncio.run(scenario())
    assert any(conflict.token in issue_id for _, issue_id in harness.registry.issues)


def test_invalid_desired_keep_uses_public_resolver_and_completes(monkeypatch):
    """Keep HA remains an explicit Task 5 action for malformed desired data."""
    conflict = _conflict(desired_action_valid=False)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    calls = []

    async def resolve(_hass, entry_id, token, action):
        calls.append((entry_id, token, action))
        harness.current[entry_id] = []
        return EngineeringAreaResolutionResult(True, action, "resolved", conflict)

    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflict", resolve)
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)

    async def scenario():
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        result = await flow.async_step_init({"action": "keep_ha_room"})
        assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY

    asyncio.run(scenario())
    assert calls == [("entry-a", conflict.token, "keep_ha_room")]


def test_invalid_desired_apply_fails_closed_without_resolver(monkeypatch):
    """A crafted Apply submission cannot turn invalid metadata into a clear."""
    conflict = _conflict(desired_action_valid=False)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    calls = []

    async def resolve(*args):
        calls.append(args)
        raise AssertionError("resolver must not run")

    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflict", resolve)
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)

    async def scenario():
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        result = await flow.async_step_init({"action": "apply_loxone_room"})
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "invalid_desired_area"

    asyncio.run(scenario())
    assert calls == []


def test_unavailable_or_replaced_entry_cannot_resolve(monkeypatch):
    """Only the loaded exact config-entry/coordinator binding may resolve."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    calls = []

    async def resolve(*args):
        calls.append(args)
        raise AssertionError("resolver must not run")

    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflict", resolve)
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)
    data = {
        "kind": "engineering_area_conflict",
        "version": 1,
        "entry_id": "entry-a",
        "conflict_token": conflict.token,
    }

    async def scenario():
        entry = harness.hass.config_entries._entries["entry-a"]
        entry.state = ConfigEntryState.NOT_LOADED
        flow = await harness.module.async_create_fix_flow(harness.hass, issue_id, data)
        flow.hass = harness.hass
        opened = await flow.async_step_init()
        assert opened["type"] is data_entry_flow.FlowResultType.ABORT
        assert opened["reason"] == "entry_unavailable"

        entry.state = ConfigEntryState.LOADED
        available_flow = await harness.module.async_create_fix_flow(harness.hass, issue_id, data)
        available_flow.hass = harness.hass
        shown = await available_flow.async_step_init()
        assert shown["type"] is data_entry_flow.FlowResultType.FORM
        entry.state = ConfigEntryState.NOT_LOADED
        unavailable_submission = await available_flow.async_step_init({"action": "keep_ha_room"})
        assert unavailable_submission["type"] is data_entry_flow.FlowResultType.ABORT
        assert unavailable_submission["reason"] == "entry_unavailable"

        entry.state = ConfigEntryState.LOADED
        replacement = SimpleNamespace(entry_id="entry-a", domain=DOMAIN, state=ConfigEntryState.LOADED)
        harness.hass.config_entries._entries["entry-a"] = replacement
        submitted = await flow.async_step_init({"action": "keep_ha_room"})
        assert submitted["type"] is data_entry_flow.FlowResultType.ABORT
        assert submitted["reason"] == "entry_unavailable"

    asyncio.run(scenario())
    assert calls == []


def test_explicit_startup_sync_cannot_publish_for_not_loaded_entry(monkeypatch):
    """The setup exception admits setup-in-progress, never an unloaded entry."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    entry = harness.hass.config_entries._entries["entry-a"]
    coordinator = harness.hass.data[DOMAIN]["entry-a"]
    entry.state = ConfigEntryState.NOT_LOADED

    asyncio.run(
        harness.module.async_sync_engineering_area_conflict_issues(
            harness.hass,
            "entry-a",
            config_entry=entry,
            coordinator=coordinator,
        )
    )

    assert harness.registry.issues == {}
    assert harness.created == []


def test_unload_during_flow_load_cannot_cross_resolver_boundary(monkeypatch):
    """An unload observed after the awaited load prevents the public mutation."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def load(_hass, _entry_id):
        entered.set()
        await release.wait()
        return (conflict,)

    async def resolve(*args):
        calls.append(args)
        raise AssertionError("resolver must not run")

    monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)
    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflict", resolve)
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)

    async def scenario():
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        task = asyncio.create_task(flow.async_step_init({"action": "keep_ha_room"}))
        await entered.wait()
        harness.hass.config_entries._entries["entry-a"].state = ConfigEntryState.UNLOAD_IN_PROGRESS
        if callback := harness.unload_callbacks.get("entry-a"):
            callback()
        release.set()
        result = await task
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "entry_unavailable"

    asyncio.run(scenario())
    assert calls == []


def test_sync_serializes_observation_and_create_before_prune(monkeypatch):
    """An older awaited observation cannot prune a newer exact-token issue."""
    old = _conflict(token="a" * 64)
    new = _conflict(token="b" * 64)
    harness = _repairs_harness(monkeypatch, {"entry-a": [old]})
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    loads = 0
    events = []

    async def load(_hass, _entry_id):
        nonlocal loads
        loads += 1
        if loads == 1:
            first_entered.set()
            await release_first.wait()
            return (old,)
        return (new,)

    original_create = harness.module.ir.async_create_issue
    original_delete = harness.module.ir.async_delete_issue

    def create(*args, **kwargs):
        events.append(("create", args[2]))
        original_create(*args, **kwargs)

    def delete(*args, **kwargs):
        events.append(("delete", args[2]))
        original_delete(*args, **kwargs)

    monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)
    monkeypatch.setattr(harness.module.ir, "async_create_issue", create)
    monkeypatch.setattr(harness.module.ir, "async_delete_issue", delete)

    async def scenario():
        older = asyncio.create_task(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a"))
        await first_entered.wait()
        newer = asyncio.create_task(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a"))
        await asyncio.sleep(0)
        assert loads == 1
        release_first.set()
        await asyncio.gather(older, newer)

    asyncio.run(scenario())
    issue_ids = {issue_id for domain, issue_id in harness.registry.issues if domain == DOMAIN}
    assert any(new.token in issue_id for issue_id in issue_ids)
    assert not any(old.token in issue_id for issue_id in issue_ids)
    new_create = next(index for index, event in enumerate(events) if event == ("create", next(iter(issue_ids))))
    old_delete = next(index for index, event in enumerate(events) if event[0] == "delete" and old.token in event[1])
    assert new_create < old_delete


def test_sync_locks_are_entry_scoped(monkeypatch):
    """A blocked entry does not delay an unrelated entry's issue reconciliation."""
    first = _conflict(entry_id="entry-a", token="a" * 64)
    second = _conflict(entry_id="entry-b", token="b" * 64)
    harness = _repairs_harness(monkeypatch, {"entry-a": [first], "entry-b": [second]})
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def load(_hass, entry_id):
        if entry_id == "entry-a":
            blocked.set()
            await release.wait()
        return tuple(harness.current[entry_id])

    monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)

    async def scenario():
        task = asyncio.create_task(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a"))
        await blocked.wait()
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-b")
        assert any(second.token in issue_id for _, issue_id in harness.registry.issues)
        release.set()
        await task

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ("entry", "provider", "unloaded", "removed"))
def test_sync_discards_observation_after_binding_replacement(monkeypatch, replacement):
    """An awaited old binding cannot publish or retire Repair issues."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    entered = asyncio.Event()
    release = asyncio.Event()

    async def load(_hass, _entry_id):
        entered.set()
        await release.wait()
        return (conflict,)

    monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)

    async def scenario():
        task = asyncio.create_task(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a"))
        await entered.wait()
        if replacement == "entry":
            harness.hass.config_entries._entries["entry-a"] = SimpleNamespace(
                entry_id="entry-a", domain=DOMAIN, state=ConfigEntryState.LOADED
            )
        elif replacement == "provider":
            coordinator = harness.hass.data[DOMAIN]["entry-a"]
            coordinator.engineering_snapshot.source.provider_identifier = "serial-new"
            coordinator.miniserver.serial = "serial-new"
        elif replacement == "unloaded":
            harness.hass.config_entries._entries["entry-a"].state = ConfigEntryState.UNLOAD_IN_PROGRESS
            harness.unload_callbacks["entry-a"]()
        else:
            harness.unload_callbacks["entry-a"]()
            harness.hass.config_entries._entries.pop("entry-a")
            harness.hass.data[DOMAIN].pop("entry-a")
        release.set()
        await task

    asyncio.run(scenario())
    assert harness.registry.issues == {}
    assert harness.created == []
    assert harness.deleted == []


def test_flow_reconcile_and_coordinator_sync_share_one_entry_fence(monkeypatch):
    """A flow retry cannot overwrite a newer coordinator observation."""
    old = _conflict(token="a" * 64)
    new = _conflict(token="b" * 64)
    harness = _repairs_harness(monkeypatch, {"entry-a": [old]})
    second_entered = asyncio.Event()
    release_second = asyncio.Event()
    loads = 0

    async def load(_hass, _entry_id):
        nonlocal loads
        loads += 1
        if loads == 1:
            return ()
        if loads == 2:
            second_entered.set()
            await release_second.wait()
            return (old,)
        return (new,)

    monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", old.token)

    async def scenario():
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": old.token,
            },
        )
        flow.hass = harness.hass
        retry = asyncio.create_task(flow.async_step_init())
        await second_entered.wait()
        coordinator = asyncio.create_task(
            harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        )
        await asyncio.sleep(0)
        assert loads == 2
        release_second.set()
        result = await retry
        await coordinator
        assert result["reason"] == "conflict_changed"

    asyncio.run(scenario())
    issue_ids = {issue_id for _, issue_id in harness.registry.issues}
    assert any(new.token in issue_id for issue_id in issue_ids)
    assert not any(old.token in issue_id for issue_id in issue_ids)


def test_cancelled_sync_never_prunes_and_can_retry(monkeypatch):
    """Cancellation during the authoritative load leaves issues untouched."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        before = set(harness.registry.issues)
        entered = asyncio.Event()

        async def blocked_load(_hass, _entry_id):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", blocked_load)
        task = asyncio.create_task(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert set(harness.registry.issues) == before

        monkeypatch.setattr(
            harness.module,
            "async_load_engineering_area_conflicts",
            lambda _hass, _entry_id: asyncio.sleep(0, result=(conflict,)),
        )
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")

    asyncio.run(scenario())


def test_cancelled_waiter_never_loads_or_disturbs_active_sync(monkeypatch):
    """Cancellation while waiting for the entry lock has no side effects."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    entered = asyncio.Event()
    release = asyncio.Event()
    loads = 0

    async def load(_hass, _entry_id):
        nonlocal loads
        loads += 1
        entered.set()
        await release.wait()
        return (conflict,)

    monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)

    async def scenario():
        active = asyncio.create_task(
            harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        )
        await entered.wait()
        waiting = asyncio.create_task(
            harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        )
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert loads == 1
        release.set()
        await active

    asyncio.run(scenario())
    assert loads == 1
    assert len(harness.registry.issues) == 1


def test_failed_sync_is_not_an_empty_authoritative_set(monkeypatch):
    """A loader failure preserves the last actionable exact-token issue."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        before = set(harness.registry.issues)

        async def fail(_hass, _entry_id):
            raise RuntimeError("synthetic-private-marker")

        monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", fail)
        with pytest.raises(RuntimeError, match="synthetic-private-marker"):
            await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        assert set(harness.registry.issues) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ("missing", "invalid_action", "invalid_desired", "stale_result"))
def test_secondary_reconciliation_failure_is_bounded(monkeypatch, caplog, case):
    """A second-load failure returns a translated reason without unsafe mutation."""
    conflict = _conflict(desired_action_valid=case != "invalid_desired")
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)
    calls = 0
    resolver_calls = []

    async def load(_hass, _entry_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic-private-marker")
        return () if case == "missing" else (conflict,)

    async def resolve(*args):
        resolver_calls.append(args)
        if case == "stale_result":
            return EngineeringAreaResolutionResult(
                resolved=False,
                action="keep_ha_room",
                reason="stale_conflict",
            )
        raise AssertionError("resolver must not run")

    monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)
    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflict", resolve)

    async def scenario():
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        input_data = {
            "missing": None,
            "invalid_action": {"action": "invalid"},
            "invalid_desired": {"action": "apply_loxone_room"},
            "stale_result": {"action": "keep_ha_room"},
        }[case]
        result = await flow.async_step_init(input_data)
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "repair_unavailable"
        assert "synthetic-private-marker" not in repr(result)

    asyncio.run(scenario())
    assert (len(resolver_calls) == 1) is (case == "stale_result")
    assert "synthetic-private-marker" not in caplog.text


def test_successful_resolution_is_authoritative_when_final_sync_fails(monkeypatch):
    """A bounded post-resolution load failure cannot undo a successful choice."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    calls = 0

    async def load(_hass, _entry_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic-private-marker")
        return (conflict,)

    async def resolve(*_args):
        return EngineeringAreaResolutionResult(
            resolved=True,
            action="keep_ha_room",
            reason="resolved",
            conflict=conflict,
        )

    monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)
    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflict", resolve)

    async def scenario():
        issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        flow.hass = harness.hass
        result = await flow.async_step_init({"action": "keep_ha_room"})
        assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY

    asyncio.run(scenario())


def test_invalid_desired_keep_reopens_after_real_task5_stale_refresh(
    registries,  # noqa: F811
    monkeypatch,
):
    """The native flow follows Task 5's refreshed token, then safely keeps HA."""
    from custom_components.loxone import repairs

    office = registries.areas.async_get_or_create("Office")
    living = registries.areas.async_get_or_create("Living Room")
    device = registries.devices.add(
        "serial-entry-a:device",
        "entry-a",
        area_id=office.id,
        name="ST-F07",
    )
    FakeIntentStore.data = {
        "generation_id": "generation-a",
        "area_intents": [],
        "managed_baselines": [],
        "area_conflicts": [
            {
                "token": "a" * 64,
                "entry_id": "entry-a",
                "device_identifier": "serial-entry-a:device",
                "display_name": "ST-F07",
                "current_area_id": office.id,
                "current_area_name": "Office",
                "desired_area_id": [],
                "desired_area_name": None,
                "generation_id": "generation-a",
                "reason": "area_user_override_preserved",
                "desired_action_valid": True,
            }
        ],
        "released_identifiers": [],
    }
    entries = _ConfigEntries("entry-a")
    coordinator = SimpleNamespace(
        config_entry=entries._entries["entry-a"],
        engineering_snapshot=SimpleNamespace(source=SimpleNamespace(provider_identifier="serial-entry-a")),
        miniserver=SimpleNamespace(serial="serial-entry-a"),
    )
    registry = _IssueRegistry()
    registries.hass.config_entries = entries
    registries.hass.data[DOMAIN] = {"entry-a": coordinator}
    repairs.async_register_engineering_area_conflict_reconciler(
        registries.hass, entries._entries["entry-a"], coordinator
    )

    def create(_hass, domain, issue_id, **kwargs):
        registry.issues[(domain, issue_id)] = SimpleNamespace(**kwargs)

    monkeypatch.setattr(repairs.ir, "async_get", lambda _hass: registry)
    monkeypatch.setattr(repairs.ir, "async_create_issue", create)
    monkeypatch.setattr(
        repairs.ir,
        "async_delete_issue",
        lambda _hass, domain, issue_id: registry.issues.pop((domain, issue_id), None),
    )

    async def flow_for(token):
        flow = await repairs.async_create_fix_flow(
            registries.hass,
            repairs.engineering_area_conflict_issue_id("entry-a", token),
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": token,
            },
        )
        flow.hass = registries.hass
        return flow

    async def scenario():
        await repairs.async_sync_engineering_area_conflict_issues(registries.hass, "entry-a")
        old_flow = await flow_for("a" * 64)
        shown = await old_flow.async_step_init()
        assert shown["type"] is data_entry_flow.FlowResultType.FORM
        assert shown["step_id"].endswith("desired_invalid")

        registries.devices.async_update_device(device.id, area_id=living.id)
        stale = await old_flow.async_step_init({"action": "keep_ha_room"})
        assert stale["type"] is data_entry_flow.FlowResultType.ABORT
        assert stale["reason"] == "conflict_changed"
        refreshed = (await repairs.async_load_engineering_area_conflicts(registries.hass, "entry-a"))[0]
        assert refreshed.token != "a" * 64
        assert any(refreshed.token in issue_id for _, issue_id in registry.issues)
        assert not any("a" * 64 in issue_id for _, issue_id in registry.issues)

        fresh_flow = await flow_for(refreshed.token)
        reopened = await fresh_flow.async_step_init()
        assert reopened["type"] is data_entry_flow.FlowResultType.FORM
        kept = await fresh_flow.async_step_init({"action": "keep_ha_room"})
        assert kept["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
        assert (await repairs.async_load_engineering_area_conflicts(registries.hass, "entry-a")) == ()
        assert device.area_id == living.id
        assert registry.issues == {}

    asyncio.run(scenario())


def test_invalid_desired_keep_cancellation_preserves_retry(monkeypatch):
    """Cancellation propagates without retiring the unresolved exact issue."""
    conflict = _conflict(desired_action_valid=False)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    entered = asyncio.Event()

    async def cancelled_resolver(*_args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        harness.module,
        "async_resolve_engineering_area_conflict",
        cancelled_resolver,
    )

    async def flow():
        issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)
        created = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 1,
                "entry_id": "entry-a",
                "conflict_token": conflict.token,
            },
        )
        created.hass = harness.hass
        return created

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        before = set(harness.registry.issues)
        first = await flow()
        task = asyncio.create_task(first.async_step_init({"action": "keep_ha_room"}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert set(harness.registry.issues) == before

        async def resolved(*_args):
            harness.current["entry-a"] = []
            return EngineeringAreaResolutionResult(
                resolved=True,
                action="keep_ha_room",
                reason="resolved",
                conflict=conflict,
            )

        monkeypatch.setattr(
            harness.module,
            "async_resolve_engineering_area_conflict",
            resolved,
        )
        retry = await flow()
        result = await retry.async_step_init({"action": "keep_ha_room"})
        assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY

    asyncio.run(scenario())


def test_unload_preserves_ignored_issue_but_genuine_removal_retires_it(monkeypatch, tmp_path):
    """Reload keeps HA ignored metadata; actual entry removal retires its issue."""
    from custom_components.loxone import repairs

    conflict = _conflict()

    async def load(_hass, _entry_id):
        return (conflict,)

    monkeypatch.setattr(repairs, "async_load_engineering_area_conflicts", load)

    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        entries = _ConfigEntries("entry-a")
        hass.config_entries = entries
        coordinator = SimpleNamespace(
            config_entry=entries._entries["entry-a"],
            engineering_snapshot=SimpleNamespace(source=SimpleNamespace(provider_identifier="serial-entry-a")),
            miniserver=SimpleNamespace(serial="serial-entry-a"),
        )
        hass.data[DOMAIN] = {"entry-a": coordinator}
        callback = repairs.async_register_engineering_area_conflict_reconciler(
            hass, entries._entries["entry-a"], coordinator
        )
        issue_id = repairs.engineering_area_conflict_issue_id("entry-a", conflict.token)
        await repairs.async_sync_engineering_area_conflict_issues(hass, "entry-a")
        ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
        dismissed = ir.async_get(hass).issues[(DOMAIN, issue_id)].dismissed_version

        entries._entries["entry-a"].state = ConfigEntryState.UNLOAD_IN_PROGRESS
        callback()
        assert ir.async_get(hass).issues[(DOMAIN, issue_id)].dismissed_version == dismissed

        entries._entries["entry-a"].state = ConfigEntryState.LOADED
        callback = repairs.async_register_engineering_area_conflict_reconciler(
            hass, entries._entries["entry-a"], coordinator
        )
        await repairs.async_sync_engineering_area_conflict_issues(hass, "entry-a")
        assert ir.async_get(hass).issues[(DOMAIN, issue_id)].dismissed_version == dismissed

        callback()
        entries._entries.pop("entry-a")
        hass.data[DOMAIN].pop("entry-a")
        await repairs.async_sync_engineering_area_conflict_issues(hass, "entry-a")
        assert (DOMAIN, issue_id) not in ir.async_get(hass).issues

    asyncio.run(scenario())


def test_room_state_variants_and_generic_device_never_expose_raw_ids(monkeypatch):
    """No-room, unknown-room and generic-device states stay distinct and bounded."""
    cases = (
        (
            _conflict(current_room=None, desired_room="Workshop"),
            "current_none_desired_named",
        ),
        (
            _conflict(current_room=None, current_area_id="hidden-current"),
            "current_unknown_desired_named",
        ),
        (
            _conflict(desired_room=None, desired_area_id="hidden-desired"),
            "current_named_desired_unknown",
        ),
        (
            _conflict(display_name=None),
            "current_named_desired_named",
        ),
    )

    async def scenario():
        for index, (conflict, step_id) in enumerate(cases):
            conflict = EngineeringAreaConflict(
                token=f"{index + 1:x}" * 64,
                entry_id=conflict.entry_id,
                device_identifier=conflict.device_identifier,
                display_name=conflict.display_name,
                current_area_id=conflict.current_area_id,
                current_area_name=conflict.current_area_name,
                desired_area_id=conflict.desired_area_id,
                desired_area_name=conflict.desired_area_name,
                generation_id=conflict.generation_id,
                reason=conflict.reason,
                desired_action_valid=conflict.desired_action_valid,
            )
            harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
            issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", conflict.token)
            flow = await harness.module.async_create_fix_flow(
                harness.hass,
                issue_id,
                {
                    "kind": "engineering_area_conflict",
                    "version": 1,
                    "entry_id": "entry-a",
                    "conflict_token": conflict.token,
                },
            )
            flow.hass = harness.hass
            shown = await flow.async_step_init()
            assert shown["step_id"] == step_id
            serialized = repr(shown["description_placeholders"])
            assert "hidden-current" not in serialized
            assert "hidden-desired" not in serialized
            if conflict.display_name is None:
                assert shown["description_placeholders"]["device"].startswith("#")
                assert "Device" not in shown["description_placeholders"]["device"]

    asyncio.run(scenario())


def test_every_safe_room_state_has_english_and_german_copy():
    """All runtime step IDs have complete localized text in both languages."""
    root = Path(__file__).parents[1] / "custom_components" / "loxone"
    expected = {
        f"current_{current}_desired_{desired}"
        for current in ("named", "none", "unknown")
        for desired in ("named", "none", "unknown", "invalid")
    }
    for path in (
        root / "strings.json",
        root / "translations" / "en.json",
        root / "translations" / "de.json",
    ):
        document = json.loads(path.read_text(encoding="utf-8"))
        steps = document["issues"]["engineering_area_conflict"]["fix_flow"]["step"]
        assert expected <= steps.keys()


def test_entry_issue_cleanup_never_deletes_peer_or_unrelated_issue(monkeypatch):
    """The unload helper owns only the exact hashed entry namespace."""
    first = _conflict(entry_id="entry-a", token="a" * 64)
    second = _conflict(entry_id="entry-b", token="b" * 64)
    harness = _repairs_harness(monkeypatch, {"entry-a": [first], "entry-b": [second]})
    asyncio.run(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a"))
    asyncio.run(harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-b"))
    harness.registry.issues[(DOMAIN, "unrelated_issue")] = SimpleNamespace()

    harness.module.async_remove_engineering_area_conflict_issues(harness.hass, "entry-a")

    remaining = {issue_id for domain, issue_id in harness.registry.issues if domain == DOMAIN}
    assert "unrelated_issue" in remaining
    assert any(second.token in issue_id for issue_id in remaining)
    assert not any(first.token in issue_id for issue_id in remaining)


def test_config_entry_removal_hook_retires_its_repairs(monkeypatch):
    """HA's genuine removal hook, not ordinary unload, retires the issue scope."""
    import custom_components.loxone as integration

    calls = []
    monkeypatch.setattr(
        integration,
        "async_remove_engineering_area_conflict_issues",
        lambda hass, entry_id: calls.append((hass, entry_id)),
        raising=False,
    )
    hass = object()
    entry = SimpleNamespace(entry_id="entry-a")

    asyncio.run(integration.async_remove_entry(hass, entry))

    assert calls == [(hass, "entry-a")]
