"""Native Home Assistant Repairs for explicit engineering area choices."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from homeassistant import data_entry_flow
from homeassistant.components.repairs.const import DOMAIN as REPAIRS_DOMAIN
from homeassistant.components.repairs.issue_handler import RepairsFlowManager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import area_registry as ar

from custom_components.loxone.const import DOMAIN
from custom_components.loxone.engineering_registry import (
    EngineeringAreaConflict,
    EngineeringBatchAreaResolutionResult,
)


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
    room_uuid: str | None = "room-a",
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
        room_uuid=room_uuid,
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
    monkeypatch.setattr(
        ar,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get_area=lambda area_id: SimpleNamespace(id="office", name="Office") if area_id == "office" else None,
            async_list_areas=lambda: [SimpleNamespace(id="office", name="Office")],
        ),
    )
    return SimpleNamespace(
        module=repairs,
        hass=hass,
        current=current,
        registry=registry,
        created=created,
        deleted=deleted,
        unload_callbacks=unload_callbacks,
    )


async def _native_manager_harness(monkeypatch, tmp_path, conflicts):
    """Use HA's real Repairs manager and issue registry around this platform."""
    from custom_components.loxone import repairs

    current = {entry_id: list(items) for entry_id, items in conflicts.items()}
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = _ConfigEntries(*current)
    hass.data[DOMAIN] = {}
    for entry_id, entry in hass.config_entries._entries.items():
        provider = f"serial-{entry_id}"
        coordinator = SimpleNamespace(
            config_entry=entry,
            engineering_snapshot=SimpleNamespace(source=SimpleNamespace(provider_identifier=provider)),
            miniserver=SimpleNamespace(serial=provider),
        )
        hass.data[DOMAIN][entry_id] = coordinator
        repairs.async_register_engineering_area_conflict_reconciler(hass, entry, coordinator)

    async def load(_hass, entry_id):
        return tuple(current.get(entry_id, ()))

    class _Platforms:
        async def async_get_platform(self, handler):
            assert handler == DOMAIN
            return repairs

    monkeypatch.setattr(repairs, "async_load_engineering_area_conflicts", load)
    hass.data[REPAIRS_DOMAIN] = {"platforms": _Platforms()}
    return SimpleNamespace(
        module=repairs,
        hass=hass,
        current=current,
        manager=RepairsFlowManager(hass),
    )


def _fingerprint(*conflicts):
    return sha256("".join(sorted(item.token for item in conflicts)).encode()).hexdigest()


async def _open(harness):
    await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
    issue = next(item for (domain, _), item in harness.registry.issues.items() if domain == DOMAIN)
    flow = await harness.module.async_create_fix_flow(harness.hass, issue.issue_id, issue.data)
    flow.hass = harness.hass
    return flow


def _rows(form, key):
    return next(marker.default() for marker in form["data_schema"].schema if marker.schema == key)


def test_placement_rows_join_current_snapshot_without_becoming_authority(monkeypatch):
    """Stale placement, provider leakage, or using descriptions as intent fails."""
    from custom_components.loxone.engineering_config import parse_engineering_xml
    from tests.engineering_fixtures import SYNTHETIC_PARSE_CONTEXT, make_snapshot

    conflict = replace(_conflict(), device_identifier="serial-a:device")
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    coordinator = harness.hass.data[DOMAIN]["entry-a"]
    coordinator.miniserver.serial = "serial-a"

    def snapshot(cabinet):
        return make_snapshot(
            inventory=parse_engineering_xml(
                (
                    f'<C Type="LoxLIVE" U="ms"><C Type="FutureDevice" U="device" '
                    f'SwitchBoard="{cabinet}" SwitchBoardRow="2" /></C>'
                ).encode(),
                **SYNTHETIC_PARSE_CONTEXT,
            )
        )

    coordinator.engineering_snapshot = snapshot("Cabinet A")
    harness.module.async_register_engineering_area_conflict_reconciler(
        harness.hass,
        coordinator.config_entry,
        coordinator,
    )

    async def scenario():
        flow = await _open(harness)
        rooms = await flow.async_step_init()
        description = _rows(rooms, "rooms")[0]["description"]
        assert "Cabinet A" in description and len(description) <= 200
        issue = next(iter(harness.registry.issues.values()))
        assert "Cabinet" not in repr(issue.data) + repr(issue.translation_placeholders) + repr(conflict)
        coordinator.engineering_snapshot = snapshot("Cabinet B")
        devices = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha"}]})
        description = _rows(devices, "devices")[0]["description"]
        assert "Cabinet B" in description and "Cabinet A" not in description and len(description) <= 200
        assert "Cabinet" not in repr(flow._decisions)
        coordinator.engineering_snapshot = snapshot("Cabinet C")
        retry = flow._form("devices", (conflict,), {"devices": _rows(devices, "devices")}, "invalid_target")
        assert "Cabinet C" in _rows(retry, "devices")[0]["description"]
        assert "Cabinet B" not in _rows(retry, "devices")[0]["description"]
        coordinator.engineering_snapshot = snapshot("https://example.invalid")
        bounded = flow._form("devices", (conflict,))
        assert "example.invalid" not in _rows(bounded, "devices")[0]["description"]
        coordinator.engineering_snapshot = None
        missing = flow._form("devices", (conflict,))
        assert "Cabinet" not in _rows(missing, "devices")[0]["description"]
        coordinator.engineering_snapshot = snapshot("Cabinet C")
        coordinator.miniserver.serial = "serial-other"
        stale = flow._form("devices", (conflict,))
        assert "Cabinet" not in _rows(stale, "devices")[0]["description"]

    asyncio.run(scenario())


def test_aggregate_twenty_conflicts_privacy_and_zero(monkeypatch):
    conflicts = [
        replace(_conflict(token=f"{index:064x}"), device_identifier=f"serial-entry-a:device-{index}")
        for index in range(20)
    ]
    harness = _repairs_harness(monkeypatch, {"entry-a": conflicts})

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        assert len(harness.registry.issues) == 1
        issue = next(iter(harness.registry.issues.values()))
        assert issue.data == {
            "kind": "engineering_area_conflict",
            "version": 2,
            "entry_id": "entry-a",
            "conflict_fingerprint": _fingerprint(*conflicts),
        }
        assert issue.translation_placeholders == {"count": "20"}
        assert "device" not in repr(issue.data)
        harness.current["entry-a"].reverse()
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        assert len(harness.registry.issues) == 1
        harness.current["entry-a"] = []
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        assert not harness.registry.issues

    asyncio.run(scenario())


def test_step_one_groups_exact_room_identity_not_names(monkeypatch):
    conflicts = [
        _conflict(),
        replace(_conflict(token="b" * 64), device_identifier="serial-entry-a:second"),
        _conflict(token="c" * 64, room_uuid="room-b"),
        _conflict(token="d" * 64, room_uuid=None),
    ]
    harness = _repairs_harness(monkeypatch, {"entry-a": conflicts})

    async def scenario():
        form = await (await _open(harness)).async_step_init()
        assert form["step_id"] == "rooms"
        rows = _rows(form, "rooms")
        assert {row["group_key"] for row in rows} == {"room-a", "room-b"}
        assert len(rows) == 2
        marker, selector = next(iter(form["data_schema"].schema.items()))
        assert marker.description == {"suggested_value": rows}
        assert selector.serialize()["selector"]["object"]["multiple"]
        assert "area" in selector.serialize()["selector"]["object"]["fields"]["area_id"]["selector"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"group_key": "unknown", "action": "keep_ha"}],
        [{"group_key": "room-a", "action": "keep_ha"}] * 2,
        [{"group_key": "room-a", "action": "use_existing"}],
        [{"group_key": "room-a", "action": "use_existing", "area_id": "missing"}],
        [{"group_key": "room-a", "action": "use_existing", "area_id": "office", "area_name": "New"}],
        [{"group_key": "room-a", "action": "create", "area_name": " "}],
        [{"group_key": "room-a", "action": "create", "area_name": "New", "area_id": "office"}],
        [{"group_key": "room-a", "action": "keep_ha", "area_name": "New"}],
        [{"group_key": "room-a", "action": "clear"}],
        [{"group_key": ["room-a"], "action": "keep_ha"}],
        [{"group_key": "room-a", "action": "keep_ha", "extra": "untrusted"}],
    ],
)
def test_step_one_invalid_rows_preserve_input(monkeypatch, rows):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def scenario():
        form = await (await _open(harness)).async_step_rooms({"rooms": rows})
        assert form["type"] is data_entry_flow.FlowResultType.FORM
        assert form["step_id"] == "rooms"
        assert form["errors"]
        assert _rows(form, "rooms") == rows

    asyncio.run(scenario())


def test_create_collision_preserves_input_and_requires_existing_selection(monkeypatch):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})
    rows = [{"group_key": "room-a", "action": "create", "area_name": "  OFFICE  "}]

    async def scenario():
        form = await (await _open(harness)).async_step_rooms({"rooms": rows})
        assert form["step_id"] == "rooms"
        assert form["errors"] == {"base": "area_name_collision"}
        assert _rows(form, "rooms") == rows

    asyncio.run(scenario())


def test_mixed_room_overrides_and_no_room_build_exact_batch(monkeypatch):
    conflicts = [
        _conflict(),
        _conflict(token="b" * 64),
        _conflict(token="c" * 64, room_uuid=None),
        _conflict(token="d" * 64, room_uuid=None),
    ]
    harness = _repairs_harness(monkeypatch, {"entry-a": conflicts})

    async def resolve(_hass, entry_id, decisions, *, is_current):
        assert entry_id == "entry-a" and is_current()
        assert [(d.room_uuid, d.action, d.area_id, d.conflict_tokens, d.keep_conflict_tokens) for d in decisions] == [
            ("room-a", "use_existing", "office", ("a" * 64, "b" * 64), ("b" * 64,)),
            (None, "keep_ha", None, ("c" * 64,), ()),
            (None, "clear", None, ("d" * 64,), ()),
        ]
        harness.current["entry-a"] = []
        return EngineeringBatchAreaResolutionResult(3, 0, "resolved")

    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflicts", resolve)

    async def scenario():
        flow = await _open(harness)
        form = await flow.async_step_rooms(
            {"rooms": [{"group_key": "room-a", "action": "use_existing", "area_id": "office"}]}
        )
        assert form["step_id"] == "devices"
        assert {marker.schema for marker in form["data_schema"].schema} == {"devices", "no_room_devices"}
        rows = _rows(form, "devices")
        no_room_rows = _rows(form, "no_room_devices")
        assert len(rows) == len(no_room_rows) == 2
        for marker, selector in form["data_schema"].schema.items():
            actions = selector.serialize()["selector"]["object"]["fields"]["action"]["selector"]["select"]["options"]
            assert actions == (["apply_group", "keep_ha"] if marker.schema == "devices" else ["keep_ha", "clear"])
        for row, action in zip(rows + no_room_rows, ("apply_group", "keep_ha", "keep_ha", "clear"), strict=True):
            row["action"] = action
        result = await flow.async_step_devices({"devices": rows, "no_room_devices": no_room_rows})
        assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
        assert not harness.registry.issues

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["missing", "duplicate", "unknown", "no_room_group", "room_clear", "cross_list"])
def test_device_membership_and_action_validation_preserves_input(monkeypatch, case):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict(), _conflict(token="b" * 64, room_uuid=None)]})

    async def scenario():
        flow = await _open(harness)
        form = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha"}]})
        rows = _rows(form, "devices")
        no_room_rows = _rows(form, "no_room_devices")
        if case == "missing":
            rows.pop()
        elif case == "duplicate":
            rows.append(rows[0].copy())
        elif case == "unknown":
            rows[0]["device_key"] = "f" * 64
        elif case == "no_room_group":
            no_room_rows[0]["action"] = "apply_group"
        elif case == "cross_list":
            no_room_rows[0]["device_key"] = rows[0]["device_key"]
        else:
            rows[0]["action"] = "clear"
        form = await flow.async_step_devices({"devices": rows, "no_room_devices": no_room_rows})
        assert form["step_id"] == "devices" and form["errors"]
        assert _rows(form, "devices") == rows
        assert _rows(form, "no_room_devices") == no_room_rows

    asyncio.run(scenario())


@pytest.mark.parametrize("renew", [False, True])
def test_real_manager_completes_only_after_empty_reload(monkeypatch, tmp_path, renew):
    async def scenario():
        old, new = _conflict(), _conflict(token="b" * 64)
        harness = await _native_manager_harness(monkeypatch, tmp_path, {"entry-a": [old]})

        async def resolve(*_args, **kwargs):
            assert kwargs["is_current"]()
            harness.current["entry-a"] = [new] if renew else []
            return EngineeringBatchAreaResolutionResult(1, int(renew), "resolved" if not renew else "stale_conflict")

        monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflicts", resolve)
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        issue_id = next(iter(ir.async_get(harness.hass).issues))[1]
        form = await harness.manager.async_init(DOMAIN, data={"issue_id": issue_id})
        form = await harness.manager.async_configure(
            form["flow_id"], {"rooms": [{"group_key": "room-a", "action": "keep_ha"}]}
        )
        result = await harness.manager.async_configure(form["flow_id"], {"devices": _rows(form, "devices")})
        issues = ir.async_get(harness.hass).issues
        if renew:
            assert result["type"] is data_entry_flow.FlowResultType.ABORT
            assert len(issues) == 1
            assert _fingerprint(new) in next(iter(issues))[1]
        else:
            assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
            assert not issues

    asyncio.run(scenario())


def test_real_manager_resolved_result_preserves_original_remaining_conflict(monkeypatch, tmp_path):
    """A successful batch report cannot authorize deletion while its conflict remains."""

    async def scenario():
        conflict = _conflict()
        harness = await _native_manager_harness(monkeypatch, tmp_path, {"entry-a": [conflict]})

        async def resolve(*_args, **kwargs):
            assert kwargs["is_current"]()
            return EngineeringBatchAreaResolutionResult(1, 0, "resolved")

        monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflicts", resolve)
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        registry = ir.async_get(harness.hass)
        issue_key = next(iter(registry.issues))
        issue_data = registry.issues[issue_key].data.copy()
        form = await harness.manager.async_init(DOMAIN, data={"issue_id": issue_key[1]})
        form = await harness.manager.async_configure(
            form["flow_id"], {"rooms": [{"group_key": "room-a", "action": "keep_ha"}]}
        )
        result = await harness.manager.async_configure(form["flow_id"], {"devices": _rows(form, "devices")})

        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert set(registry.issues) == {issue_key}
        assert registry.issues[issue_key].data == issue_data

    asyncio.run(scenario())


def test_old_flow_resynchronizes_changed_fingerprint_without_deleting_new(monkeypatch):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def scenario():
        flow = await _open(harness)
        await flow.async_step_init()
        new = _conflict(token="b" * 64)
        harness.current["entry-a"] = [new]
        form = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha"}]})
        assert form["reason"] == "conflict_changed"
        assert len(harness.registry.issues) == 1
        assert _fingerprint(new) in next(iter(harness.registry.issues))[1]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["provider", "unload", "replace"])
def test_flow_rechecks_binding_at_await_and_between_steps(monkeypatch, change):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def scenario():
        flow = await _open(harness)
        await flow.async_step_init()
        coordinator = harness.hass.data[DOMAIN]["entry-a"]

        async def load(*_args):
            if change == "provider":
                coordinator.miniserver.serial = "replacement"
            elif change == "unload":
                harness.unload_callbacks["entry-a"]()
            else:
                harness.module.async_register_engineering_area_conflict_reconciler(
                    harness.hass, coordinator.config_entry, coordinator
                )
            await asyncio.sleep(0)
            return tuple(harness.current["entry-a"])

        monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", load)
        result = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha"}]})
        assert result["reason"] == "entry_unavailable"
        assert len(harness.registry.issues) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["foreign", "malformed", "duplicate"])
def test_untrusted_conflict_set_never_grants_partial_authority(monkeypatch, bad):
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})

    async def scenario():
        flow = await _open(harness)
        before = set(harness.registry.issues)
        other = (
            replace(conflict, token="b" * 64, device_identifier="foreign:device")
            if bad == "foreign"
            else replace(conflict, token="invalid")
            if bad == "malformed"
            else conflict
        )
        harness.current["entry-a"].append(other)
        await harness.module.async_sync_engineering_area_conflict_issues(harness.hass, "entry-a")
        result = await flow.async_step_init()
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert set(harness.registry.issues) == before

    asyncio.run(scenario())


def test_replaced_binding_reconciles_new_issue_only_after_old_lock_released(monkeypatch):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def scenario():
        flow = await _open(harness)
        await flow.async_step_init()
        coordinator = harness.hass.data[DOMAIN]["entry-a"]
        newer = _conflict(token="b" * 64)
        harness.current["entry-a"] = [newer]
        harness.module.async_register_engineering_area_conflict_reconciler(
            harness.hass, coordinator.config_entry, coordinator
        )
        # A recursive acquisition under the old lock would time out here.
        result = await asyncio.wait_for(
            flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha"}]}), 1
        )
        assert result["reason"] == "entry_unavailable"
        assert len(harness.registry.issues) == 1
        assert _fingerprint(newer) in next(iter(harness.registry.issues))[1]

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["extra", "version_bool", "fingerprint", "id"])
def test_exact_issue_payload_rejected_without_loading(monkeypatch, mutation):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def scenario():
        flow = await _open(harness)
        if mutation == "extra":
            flow._flow_data["provider"] = "foreign"
        elif mutation == "version_bool":
            flow._flow_data["version"] = True
        elif mutation == "fingerprint":
            flow._flow_data["conflict_fingerprint"] = "b" * 64
        else:
            flow._issue_id = "foreign"
        result = await flow.async_step_init()
        assert result["reason"] == "invalid_repair"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reason", ["pending_batch_exists", "created_area_missing", "created_area_changed", "area_name_collision"]
)
def test_bounded_batch_outcomes_do_not_silently_complete(monkeypatch, reason):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def resolve(*_args, **_kwargs):
        return EngineeringBatchAreaResolutionResult(0, 1, reason)

    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflicts", resolve)

    async def scenario():
        flow = await _open(harness)
        form = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha"}]})
        result = await flow.async_step_devices({"devices": _rows(form, "devices")})
        assert result["type"] is not data_entry_flow.FlowResultType.CREATE_ENTRY
        assert len(harness.registry.issues) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [False, 0, [], {}])
def test_falsey_target_values_are_not_silently_discarded(monkeypatch, value):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def scenario():
        flow = await _open(harness)
        form = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha", "area_id": value}]})
        assert form["step_id"] == "rooms" and form["errors"]

    asyncio.run(scenario())


def test_registry_validation_exception_never_becomes_an_error_identifier(monkeypatch):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    def fail():
        raise ValueError("private-storage-detail")

    monkeypatch.setattr(ar, "async_get", lambda _: SimpleNamespace(async_list_areas=fail))

    async def scenario():
        flow = await _open(harness)
        form = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "create", "area_name": "New"}]})
        assert "private-storage-detail" not in repr(form)

    asyncio.run(scenario())


def test_flow_cancellation_propagates_and_preserves_issue(monkeypatch):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def resolve(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflicts", resolve)

    async def scenario():
        flow = await _open(harness)
        form = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha"}]})
        before = set(harness.registry.issues)
        with pytest.raises(asyncio.CancelledError):
            await flow.async_step_devices({"devices": _rows(form, "devices")})
        assert set(harness.registry.issues) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["load_failure", "binding_change"])
def test_batch_success_never_completes_without_verified_final_read(monkeypatch, outcome):
    harness = _repairs_harness(monkeypatch, {"entry-a": [_conflict()]})

    async def fail(*_args):
        raise RuntimeError("private-storage-detail")

    async def resolve(*_args, **_kwargs):
        harness.current["entry-a"] = []
        if outcome == "load_failure":
            monkeypatch.setattr(harness.module, "async_load_engineering_area_conflicts", fail)
        else:
            harness.hass.data[DOMAIN]["entry-a"].miniserver.serial = "replacement"
        return EngineeringBatchAreaResolutionResult(1, 0, "resolved")

    monkeypatch.setattr(harness.module, "async_resolve_engineering_area_conflicts", resolve)

    async def scenario():
        flow = await _open(harness)
        form = await flow.async_step_rooms({"rooms": [{"group_key": "room-a", "action": "keep_ha"}]})
        result = await flow.async_step_devices({"devices": _rows(form, "devices")})
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert "private-storage-detail" not in repr(result)
        assert len(harness.registry.issues) == 1

    asyncio.run(scenario())


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
    assert any(_fingerprint(new) in issue_id for issue_id in issue_ids)
    assert not any(_fingerprint(old) in issue_id for issue_id in issue_ids)
    new_create = next(index for index, event in enumerate(events) if event == ("create", next(iter(issue_ids))))
    old_delete = next(
        index for index, event in enumerate(events) if event[0] == "delete" and _fingerprint(old) in event[1]
    )
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
        assert any(_fingerprint(second) in issue_id for _, issue_id in harness.registry.issues)
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


@pytest.mark.parametrize(
    ("serial", "snapshot_provider", "expected_provider"),
    (
        ("serial-entry-a", "serial-entry-a", "serial-entry-a"),
        (None, "entry-a", "entry-a"),
        ("serial-entry-a", None, "serial-entry-a"),
        (None, None, "entry-a"),
    ),
)
def test_current_provider_and_serialless_cold_start_open_repairs(
    monkeypatch,
    serial,
    snapshot_provider,
    expected_provider,
):
    """Matching live sources and genuine entry fallbacks remain available."""
    conflict = _conflict()
    if expected_provider == "entry-a":
        conflict = replace(
            conflict,
            device_identifier="entry-a:device",
        )
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    coordinator = harness.hass.data[DOMAIN]["entry-a"]
    coordinator.miniserver.serial = serial
    coordinator.engineering_snapshot = (
        None
        if snapshot_provider is None
        else SimpleNamespace(source=SimpleNamespace(provider_identifier=snapshot_provider))
    )
    harness.module.async_register_engineering_area_conflict_reconciler(
        harness.hass,
        harness.hass.config_entries._entries["entry-a"],
        coordinator,
    )

    async def scenario():
        issue_id = harness.module.engineering_area_conflict_issue_id("entry-a", _fingerprint(conflict))
        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            issue_id,
            {
                "kind": "engineering_area_conflict",
                "version": 2,
                "entry_id": "entry-a",
                "conflict_fingerprint": _fingerprint(conflict),
            },
        )
        flow.hass = harness.hass
        shown = await flow.async_step_init()
        assert shown["type"] is data_entry_flow.FlowResultType.FORM
        assert (
            harness.module._active_binding(
                harness.hass,
                "entry-a",
                require_loaded=True,
            ).provider_identifier
            == expected_provider
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("serial", "provider"),
    (
        pytest.param("serial-entry-a", "serial-entry-a", id="serial-backed"),
        pytest.param(None, "entry-a", id="initialized-serialless"),
    ),
)
def test_real_coordinator_constructor_defers_first_provider_until_setup_sync(
    monkeypatch,
    tmp_path,
    serial,
    provider,
):
    """Constructor registration defers identity until a provider is initialized."""
    from custom_components.loxone import repairs
    from custom_components.loxone.coordinator import LoxoneCoordinator

    conflict = replace(
        _conflict(),
        device_identifier=f"{provider}:device",
    )
    loader_calls = []

    async def load(_hass, entry_id):
        loader_calls.append(entry_id)
        return (conflict,)

    class _Platforms:
        async def async_get_platform(self, handler):
            assert handler == DOMAIN
            return repairs

    monkeypatch.setattr(
        "homeassistant.helpers.frame.report_usage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(repairs, "async_load_engineering_area_conflicts", load)

    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        unload_callbacks = []
        entry = SimpleNamespace(
            entry_id="entry-a",
            domain=DOMAIN,
            state=ConfigEntryState.SETUP_IN_PROGRESS,
            options={"username": "", "password": "", "host": "", "port": 0},
            async_on_unload=unload_callbacks.append,
        )
        entries = _ConfigEntries()
        entries._entries[entry.entry_id] = entry
        hass.config_entries = entries

        coordinator = LoxoneCoordinator(hass, entry)
        coordinator.miniserver = SimpleNamespace(serial=serial)
        coordinator.engineering_snapshot = SimpleNamespace(source=SimpleNamespace(provider_identifier=provider))
        hass.data[DOMAIN] = {entry.entry_id: coordinator}
        hass.data[REPAIRS_DOMAIN] = {"platforms": _Platforms()}

        await repairs.async_sync_engineering_area_conflict_issues(
            hass,
            entry.entry_id,
            config_entry=entry,
            coordinator=coordinator,
        )
        issue_id = repairs.engineering_area_conflict_issue_id(
            entry.entry_id,
            _fingerprint(conflict),
        )
        assert (DOMAIN, issue_id) in ir.async_get(hass).issues

        await repairs.async_sync_engineering_area_conflict_issues(
            hass,
            entry.entry_id,
            config_entry=entry,
            coordinator=coordinator,
        )
        assert loader_calls == [entry.entry_id, entry.entry_id]
        assert sum(domain == DOMAIN and current_id == issue_id for domain, current_id in ir.async_get(hass).issues) == 1

        entry.state = ConfigEntryState.LOADED
        opened = await RepairsFlowManager(hass).async_init(
            DOMAIN,
            data={"issue_id": issue_id},
        )
        assert opened["type"] is data_entry_flow.FlowResultType.FORM
        assert opened["step_id"] == "rooms"

        coordinator.miniserver.serial = "serial-replacement"
        before_issues = set(ir.async_get(hass).issues)
        before_loads = len(loader_calls)
        await repairs.async_sync_engineering_area_conflict_issues(
            hass,
            entry.entry_id,
        )
        assert len(loader_calls) == before_loads
        assert set(ir.async_get(hass).issues) == before_issues
        assert (
            repairs._active_binding(
                hass,
                entry.entry_id,
                require_loaded=True,
            )
            is None
        )

    asyncio.run(scenario())


def test_current_serial_mismatch_with_retained_snapshot_blocks_sync_and_open(
    monkeypatch,
):
    """A retained old snapshot cannot validate a changed current provider."""
    conflict = _conflict()
    newer = _conflict(token="b" * 64)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})

    async def scenario():
        await harness.module.async_sync_engineering_area_conflict_issues(
            harness.hass,
            "entry-a",
        )
        exact_id = harness.module.engineering_area_conflict_issue_id("entry-a", _fingerprint(conflict))
        newer_id = harness.module.engineering_area_conflict_issue_id("entry-a", _fingerprint(newer))
        foreign_id = "foreign_issue"
        harness.registry.issues[(DOMAIN, newer_id)] = SimpleNamespace()
        harness.registry.issues[(DOMAIN, foreign_id)] = SimpleNamespace()
        before = set(harness.registry.issues)

        coordinator = harness.hass.data[DOMAIN]["entry-a"]
        coordinator.miniserver.serial = "serial-replacement"
        harness.current["entry-a"] = []
        await harness.module.async_sync_engineering_area_conflict_issues(
            harness.hass,
            "entry-a",
        )
        assert set(harness.registry.issues) == before

        flow = await harness.module.async_create_fix_flow(
            harness.hass,
            exact_id,
            {
                "kind": "engineering_area_conflict",
                "version": 2,
                "entry_id": "entry-a",
                "conflict_fingerprint": _fingerprint(conflict),
            },
        )
        flow.hass = harness.hass
        blocked = await flow.async_step_init()
        assert blocked["type"] is data_entry_flow.FlowResultType.ABORT
        assert blocked["reason"] == "entry_unavailable"
        assert set(harness.registry.issues) == before

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
        issue_id = repairs.engineering_area_conflict_issue_id("entry-a", _fingerprint(conflict))
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


def test_genuine_removal_releases_objects_but_reuses_the_waiter_lock(monkeypatch):
    """Removal clears heavy references without replacing a lock held by waiters."""
    conflict = _conflict()
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})
    state = harness.module._reconciler(harness.hass, "entry-a")
    lock = state.lock
    old_callback = harness.unload_callbacks["entry-a"]

    harness.module.async_remove_engineering_area_conflict_issues(
        harness.hass,
        "entry-a",
    )

    assert state.lock is lock
    assert state.binding is None
    assert state.config_entry is None
    assert state.coordinator is None
    assert state.provider_identifier is None
    assert not state.active

    replacement = SimpleNamespace(
        entry_id="entry-a",
        domain=DOMAIN,
        state=ConfigEntryState.LOADED,
    )
    coordinator = SimpleNamespace(
        config_entry=replacement,
        engineering_snapshot=SimpleNamespace(source=SimpleNamespace(provider_identifier="serial-entry-a")),
        miniserver=SimpleNamespace(serial="serial-entry-a"),
    )
    harness.hass.config_entries._entries["entry-a"] = replacement
    harness.hass.data[DOMAIN]["entry-a"] = coordinator
    harness.module.async_register_engineering_area_conflict_reconciler(
        harness.hass,
        replacement,
        coordinator,
    )
    old_callback()

    assert state.lock is lock
    assert state.active
    assert state.config_entry is replacement
    assert state.coordinator is coordinator


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
