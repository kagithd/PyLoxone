"""Native Home Assistant Repairs for explicit engineering area choices."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.loxone.const import DOMAIN
from custom_components.loxone.engineering_registry import (
    EngineeringAreaConflict,
    EngineeringAreaResolutionResult,
)


def _conflict(
    *,
    entry_id: str = "entry-a",
    token: str = "a" * 64,
    current_room: str | None = "Office",
    desired_room: str | None = "Workshop",
    desired_action_valid: bool = True,
) -> EngineeringAreaConflict:
    return EngineeringAreaConflict(
        token=token,
        entry_id=entry_id,
        device_identifier=f"serial-{entry_id}:device",
        display_name="ST-F07",
        current_area_id="office" if current_room else None,
        current_area_name=current_room,
        desired_area_id="workshop" if desired_room else None,
        desired_area_name=desired_room,
        generation_id="generation-a",
        reason="area_user_override_preserved",
        desired_action_valid=desired_action_valid,
    )


class _ConfigEntries:
    def __init__(self, *entry_ids: str) -> None:
        self._entries = {entry_id: SimpleNamespace(entry_id=entry_id, domain=DOMAIN) for entry_id in entry_ids}

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
    hass = SimpleNamespace(config_entries=_ConfigEntries(*current))

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
    assert first_call[2]["translation_placeholders"] == {
        "device": "ST-F07",
        "current_room": "Office",
        "desired_room": "Workshop",
    }
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
    """An explicit room clear is human-readable and remains token-bound."""
    conflict = _conflict(desired_room=None)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
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
        shown = await flow.async_step_init()
        assert shown["description_placeholders"]["desired_room"] == "No room"

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


def test_invalid_desired_conflict_aborts_but_keeps_actionable_issue(monkeypatch):
    """Unsafe desired metadata is never converted into an implicit room clear."""
    conflict = _conflict(desired_action_valid=False)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
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
        result = await flow.async_step_init()
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "invalid_desired_area"

    asyncio.run(scenario())
    assert any(conflict.token in issue_id for _, issue_id in harness.registry.issues)


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
