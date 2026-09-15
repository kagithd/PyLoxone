"""Native Repairs flows for explicit engineering area decisions."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import (
    AreaSelector,
    ObjectSelector,
    ObjectSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)

from .const import DOMAIN
from .engineering_config import installation_placement_to_dict
from .engineering_hierarchy import build_engineering_hierarchy
from .engineering_registry import (
    EngineeringAreaConflict,
    async_load_engineering_area_conflicts,
    async_resolve_engineering_area_conflicts,
)
from .engineering_snapshot import (
    EngineeringAreaDecision,
    EngineeringSnapshot,
    EngineeringSnapshotError,
    normalize_engineering_area_decision,
    validate_engineering_presentation,
    validate_engineering_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_ISSUE_KIND = "engineering_area_conflict"
_ISSUE_VERSION = 2
_ISSUE_PREFIX = f"{_ISSUE_KIND}_"
_RECONCILER_DATA = f"{DOMAIN}_engineering_area_reconcilers"
_ENTRY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONFLICT_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_INVALID_IDENTITY = "invalid engineering area conflict identity"
_INVALID_ROWS = "invalid_rows"
_INVALID_TARGET = "invalid_target"
_AREA_COLLISION = "area_name_collision"
_ENTRY_UNAVAILABLE = "entry_unavailable"
_REPAIR_UNAVAILABLE = "repair_unavailable"
_ROOM_UUID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class _FlowError(ValueError):
    """Only fixed, translated validation codes cross the form boundary."""


@dataclass(slots=True)
class _EntryReconciler:
    """One event-loop-local serialized issue reconciliation boundary."""

    lock: asyncio.Lock
    binding: object | None = None
    config_entry: object | None = None
    coordinator: object | None = None
    provider_identifier: str | None = None
    active: bool = False


@dataclass(frozen=True, slots=True)
class _ActiveBinding:
    """Exact lifecycle/provider evidence captured across awaited operations."""

    state: _EntryReconciler
    binding: object
    config_entry: object
    coordinator: object
    provider_identifier: str


def _entry_scope(entry_id: str) -> str:
    """Return an opaque fixed-width issue namespace for one config entry."""
    return sha256(entry_id.encode()).hexdigest()[:16]


def _owned_issue_prefix(entry_id: str) -> str:
    return f"{_ISSUE_PREFIX}{_entry_scope(entry_id)}_"


def engineering_area_conflict_issue_id(entry_id: str, conflict_fingerprint: str) -> str:
    """Build an issue identity fenced by config entry and exact aggregate fingerprint."""
    if not _ENTRY_ID.fullmatch(entry_id) or not _CONFLICT_TOKEN.fullmatch(conflict_fingerprint):
        raise ValueError(_INVALID_IDENTITY)
    return f"{_owned_issue_prefix(entry_id)}{conflict_fingerprint}"


def _reconciler(hass: HomeAssistant, entry_id: str) -> _EntryReconciler:
    states = hass.data.setdefault(_RECONCILER_DATA, {})
    state = states.get(entry_id)
    if state is None:
        state = _EntryReconciler(asyncio.Lock())
        states[entry_id] = state
    return state


def _provider_identifier(coordinator: object) -> str | None:
    config_entry = getattr(coordinator, "config_entry", None)
    entry_id = getattr(config_entry, "entry_id", None)
    miniserver = getattr(coordinator, "miniserver", None)
    if miniserver is None:
        return None
    serial = getattr(miniserver, "serial", None)
    provider = serial or entry_id
    if not isinstance(provider, str) or not provider:
        return None
    snapshot = getattr(coordinator, "engineering_snapshot", None)
    snapshot_provider = getattr(
        getattr(snapshot, "source", None),
        "provider_identifier",
        None,
    )
    if snapshot is not None and snapshot_provider != provider:
        return None
    return provider


def async_register_engineering_area_conflict_reconciler(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: object,
) -> Callable[[], None]:
    """Activate one exact coordinator binding and return its unload callback."""
    entry_id = config_entry.entry_id
    state = _reconciler(hass, entry_id)
    binding = object()
    state.binding = binding
    state.config_entry = config_entry
    state.coordinator = coordinator
    state.provider_identifier = _provider_identifier(coordinator)
    state.active = True
    setattr(coordinator, "_engineering_repairs_binding", binding)  # noqa: B010

    def async_unload() -> None:
        if state.binding is binding:
            state.active = False

    return async_unload


def _configured_entry(hass: HomeAssistant, entry_id: str) -> object | None:
    manager = getattr(hass, "config_entries", None)
    getter = getattr(manager, "async_get_entry", None)
    if not callable(getter):
        return None
    entry = getter(entry_id)
    return entry if entry is not None and getattr(entry, "domain", None) == DOMAIN else None


def _active_binding(
    hass: HomeAssistant,
    entry_id: str,
    *,
    require_loaded: bool,
    config_entry: object | None = None,
    coordinator: object | None = None,
) -> _ActiveBinding | None:
    configured = _configured_entry(hass, entry_id)
    if configured is None or (config_entry is not None and configured is not config_entry):
        return None
    entry_state = getattr(configured, "state", None)
    if (require_loaded and entry_state is not ConfigEntryState.LOADED) or (
        not require_loaded and entry_state in {ConfigEntryState.NOT_LOADED, ConfigEntryState.UNLOAD_IN_PROGRESS}
    ):
        return None
    state = _reconciler(hass, entry_id)
    candidate = coordinator
    if candidate is None:
        domain_data = hass.data.get(DOMAIN, {})
        candidate = domain_data.get(entry_id) if isinstance(domain_data, dict) else None
    if (
        candidate is None
        or not state.active
        or state.config_entry is not configured
        or state.coordinator is not candidate
        or getattr(candidate, "config_entry", None) is not configured
        or state.binding is None
        or getattr(candidate, "_engineering_repairs_binding", None) is not state.binding
    ):
        return None
    provider = _provider_identifier(candidate)
    if provider is None:
        return None
    if coordinator is not None and state.provider_identifier is None:
        state.provider_identifier = provider
    if state.provider_identifier != provider:
        return None
    return _ActiveBinding(state, state.binding, configured, candidate, provider)


def _binding_is_current(
    hass: HomeAssistant,
    entry_id: str,
    active: _ActiveBinding,
    *,
    require_loaded: bool = True,
) -> bool:
    """Revalidate exact lifecycle/provider evidence after an await."""
    current = _active_binding(
        hass,
        entry_id,
        require_loaded=require_loaded,
        config_entry=active.config_entry,
        coordinator=active.coordinator,
    )
    return bool(
        current is not None
        and current.state is active.state
        and current.binding is active.binding
        and current.provider_identifier == active.provider_identifier
    )


def _conflict_belongs_to_provider(
    conflict: EngineeringAreaConflict,
    provider_identifier: str,
) -> bool:
    identifier = conflict.device_identifier
    return identifier == provider_identifier or identifier.startswith(f"{provider_identifier}:")


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_engineering_presentation(value) or None
    except EngineeringSnapshotError:
        return None


def _device_reference(conflict: EngineeringAreaConflict) -> str:
    return _safe_text(conflict.display_name) or f"#{sha256(conflict.device_identifier.encode()).hexdigest()[:8]}"


def _owned_issue_ids(hass: HomeAssistant, entry_id: str) -> set[str]:
    prefix = _owned_issue_prefix(entry_id)
    issues = getattr(ir.async_get(hass), "issues", {})
    return {issue_id for domain, issue_id in tuple(issues) if domain == DOMAIN and issue_id.startswith(prefix)}


def async_remove_engineering_area_conflict_issues(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Retire issues only after the config entry is genuinely removed."""
    if not _ENTRY_ID.fullmatch(entry_id):
        return
    for issue_id in _owned_issue_ids(hass, entry_id):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    states = hass.data.get(_RECONCILER_DATA, {})
    state = states.get(entry_id) if isinstance(states, dict) else None
    if state is not None:
        state.binding = None
        state.config_entry = None
        state.coordinator = None
        state.provider_identifier = None
        state.active = False


def _trusted_conflicts(
    conflicts: tuple[EngineeringAreaConflict, ...],
    entry_id: str,
    active: _ActiveBinding,
) -> bool:
    """Reject the entire observation if any member cannot grant authority."""
    tokens: set[str] = set()
    for conflict in conflicts:
        if (
            not isinstance(conflict, EngineeringAreaConflict)
            or conflict.entry_id != entry_id
            or not isinstance(conflict.token, str)
            or not _CONFLICT_TOKEN.fullmatch(conflict.token)
            or conflict.token in tokens
            or not isinstance(conflict.device_identifier, str)
            or not _conflict_belongs_to_provider(conflict, active.provider_identifier)
            or (
                conflict.room_uuid is not None
                and (not isinstance(conflict.room_uuid, str) or not _ROOM_UUID.fullmatch(conflict.room_uuid))
            )
        ):
            return False
        tokens.add(conflict.token)
    return True


def _fingerprint(conflicts: tuple[EngineeringAreaConflict, ...]) -> str:
    """Hash fixed-width exact tokens without names or traversal ordering."""
    return sha256("".join(sorted(item.token for item in conflicts)).encode()).hexdigest()


def _publish_conflicts(
    hass: HomeAssistant,
    entry_id: str,
    conflicts: tuple[EngineeringAreaConflict, ...],
) -> None:
    """Publish the validated current aggregate before retiring obsolete issues."""
    desired: set[str] = set()
    if conflicts:
        fingerprint = _fingerprint(conflicts)
        issue_id = engineering_area_conflict_issue_id(entry_id, fingerprint)
        desired.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            data={
                "kind": _ISSUE_KIND,
                "version": _ISSUE_VERSION,
                "entry_id": entry_id,
                "conflict_fingerprint": fingerprint,
            },
            is_fixable=True,
            is_persistent=True,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_ISSUE_KIND,
            translation_placeholders={"count": str(min(len(conflicts), 99999))},
        )
    for issue_id in _owned_issue_ids(hass, entry_id) - desired:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _async_reconcile_locked(
    hass: HomeAssistant,
    entry_id: str,
    active: _ActiveBinding,
    *,
    require_loaded: bool,
) -> bool:
    """Load and reconcile one authoritative set while its entry lock is held."""
    conflicts = await async_load_engineering_area_conflicts(hass, entry_id)
    if not _binding_is_current(hass, entry_id, active, require_loaded=require_loaded):
        return False
    if not _trusted_conflicts(conflicts, entry_id, active):
        return False
    _publish_conflicts(hass, entry_id, conflicts)
    return True


async def async_sync_engineering_area_conflict_issues(
    hass: HomeAssistant,
    entry_id: str,
    *,
    config_entry: ConfigEntry | None = None,
    coordinator: object | None = None,
) -> None:
    """Serialize durable engineering conflicts into entry-scoped native issues."""
    if not _ENTRY_ID.fullmatch(entry_id):
        return
    configured = _configured_entry(hass, entry_id)
    if configured is None:
        state = _reconciler(hass, entry_id)
        async with state.lock:
            if _configured_entry(hass, entry_id) is None:
                async_remove_engineering_area_conflict_issues(hass, entry_id)
        return
    require_loaded = config_entry is None or coordinator is None
    active = _active_binding(
        hass,
        entry_id,
        require_loaded=require_loaded,
        config_entry=config_entry,
        coordinator=coordinator,
    )
    if active is None:
        return
    async with active.state.lock:
        if not _binding_is_current(
            hass,
            entry_id,
            active,
            require_loaded=require_loaded,
        ):
            return
        await _async_reconcile_locked(
            hass,
            entry_id,
            active,
            require_loaded=require_loaded,
        )


def _validated_flow_data(
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> tuple[str, str] | None:
    if not isinstance(data, dict) or set(data) != {
        "kind",
        "version",
        "entry_id",
        "conflict_fingerprint",
    }:
        return None
    entry_id = data.get("entry_id")
    token = data.get("conflict_fingerprint")
    if (
        data.get("kind") != _ISSUE_KIND
        or type(data.get("version")) is not int
        or data.get("version") != _ISSUE_VERSION
        or not isinstance(entry_id, str)
        or not isinstance(token, str)
        or not _ENTRY_ID.fullmatch(entry_id)
        or not _CONFLICT_TOKEN.fullmatch(token)
        or issue_id != engineering_area_conflict_issue_id(entry_id, token)
    ):
        return None
    return entry_id, token


def _room_groups(conflicts: tuple[EngineeringAreaConflict, ...]) -> dict[str, tuple[EngineeringAreaConflict, ...]]:
    return {
        room: tuple(item for item in conflicts if item.room_uuid == room)
        for room in sorted({item.room_uuid for item in conflicts if item.room_uuid})
    }


def _device_key(conflict: EngineeringAreaConflict) -> str:
    return sha256(conflict.token.encode()).hexdigest()


def _exact_rows(user_input: Any, field: str, key: str, expected: set[str], allowed: set[str]) -> list[dict[str, Any]]:
    """Treat native object rows as untrusted, including edits to identity fields."""
    if not isinstance(user_input, dict) or set(user_input) != {field}:
        raise _FlowError(_INVALID_ROWS)
    rows = user_input[field]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise _FlowError(_INVALID_ROWS)
    seen: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not set(row) <= allowed
            or not isinstance(row.get(key), str)
            or row[key] not in expected
            or row[key] in seen
            or not isinstance(row.get("action"), str)
            or ("description" in row and _safe_text(row["description"]) != row["description"])
        ):
            raise _FlowError(_INVALID_ROWS)
        seen.add(row[key])
    return rows


class EngineeringAreaConflictFixFlow(RepairsFlow):
    """Two native forms fenced by one exact aggregate and lifecycle binding."""

    def __init__(self, issue_id: str, data: dict[str, str | int | float | None] | None) -> None:
        """Capture untrusted data and bind the first opened lifecycle."""
        super().__init__()
        self._issue_id = issue_id
        self._flow_data = dict(data) if isinstance(data, dict) else data
        self._active: _ActiveBinding | None = None
        self._room_input: dict[str, Any] | None = None
        self._decisions: tuple[EngineeringAreaDecision, ...] | None = None

    def _placement_descriptions(self) -> dict[str, str]:
        """Join only the current validated snapshot, never persisted conflict data."""
        active = self._active
        if active is None:
            return {}
        entry_id = active.config_entry.entry_id
        if not _binding_is_current(self.hass, entry_id, active):
            return {}
        snapshot = getattr(active.coordinator, "engineering_snapshot", None)
        if (
            not isinstance(snapshot, EngineeringSnapshot)
            or snapshot.source.entry_id != entry_id
            or snapshot.source.provider_identifier != active.provider_identifier
        ):
            return {}
        try:
            validate_engineering_snapshot(snapshot)
            hierarchy = build_engineering_hierarchy(snapshot)
        except (EngineeringSnapshotError, ValueError):
            return {}
        summaries = {}
        pending = [hierarchy.root]
        while pending:
            node = pending.pop()
            pending.extend(node.children)
            placement = installation_placement_to_dict(node.placement)
            if placement:
                summary = " · ".join(
                    f"{key}: {value}" for key, value in placement.items() if _safe_text(str(value)) is not None
                )[:200]
                if summary:
                    summaries[node.identifier] = summary
        return summaries

    def _form(
        self,
        step: str,
        conflicts: tuple[EngineeringAreaConflict, ...],
        user_input: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> RepairsFlowResult:
        schema = {}
        placements = self._placement_descriptions()

        def description(conflict: EngineeringAreaConflict) -> str:
            reference = _device_reference(conflict)
            placement = placements.get(conflict.device_identifier)
            return (reference + " · " + placement)[:200] if placement else reference

        if step == "rooms":
            row_sets = {
                "rooms": [
                    {
                        "group_key": room,
                        "description": (
                            (_safe_text(members[0].desired_area_name) or "#" + sha256(room.encode()).hexdigest()[:8])
                            + " · "
                            + ", ".join(description(item) for item in members[:3])
                        )[:200],
                        "action": "keep_ha",
                    }
                    for room, members in _room_groups(conflicts).items()
                ]
            }
        else:
            row_sets = {
                field: [
                    {
                        "device_key": _device_key(item),
                        "description": description(item),
                        "action": "apply_group" if item.room_uuid else "keep_ha",
                    }
                    for item in sorted(conflicts, key=lambda item: item.token)
                    if bool(item.room_uuid) == (field == "devices")
                ]
                for field in ("devices", "no_room_devices")
            }
            row_sets = {field: rows for field, rows in row_sets.items() if rows}
        for field, rows in row_sets.items():
            actions = {
                "rooms": ["use_existing", "create", "keep_ha"],
                "devices": ["apply_group", "keep_ha"],
                "no_room_devices": ["keep_ha", "clear"],
            }[field]
            fields = {
                "group_key" if step == "rooms" else "device_key": {"required": True, "selector": TextSelector()},
                "description": {"selector": TextSelector()},
                "action": {
                    "required": True,
                    "selector": SelectSelector(
                        SelectSelectorConfig(
                            options=actions,
                            translation_key="engineering_area_decision",
                        )
                    ),
                },
            }
            if step == "rooms":
                fields.update({"area_id": {"selector": AreaSelector()}, "area_name": {"selector": TextSelector()}})
            values = user_input.get(field, rows) if isinstance(user_input, dict) else rows
            if isinstance(values, list):
                identity = "group_key" if step == "rooms" else "device_key"
                descriptions = {row[identity]: row["description"] for row in rows}
                values = [
                    {**item, "description": descriptions[item[identity]]}
                    if isinstance(item, dict)
                    and "description" in item
                    and isinstance(item.get(identity), str)
                    and item[identity] in descriptions
                    else item
                    for item in values
                ]
            schema[
                vol.Required(
                    field,
                    default=values,
                    description={"suggested_value": values},
                )
            ] = ObjectSelector(
                ObjectSelectorConfig(
                    multiple=True,
                    fields=fields,
                    description_field="description",
                    translation_key="engineering_area_" + step,
                )
            )
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(schema),
            errors={"base": error} if error else {},
        )

    def _validate_rooms(
        self, user_input: dict[str, Any], conflicts: tuple[EngineeringAreaConflict, ...]
    ) -> tuple[EngineeringAreaDecision, ...]:
        groups = _room_groups(conflicts)
        rows = _exact_rows(
            user_input,
            "rooms",
            "group_key",
            set(groups),
            {"group_key", "description", "action", "area_id", "area_name"},
        )
        decisions = []
        names: set[str] = set()
        areas = ar.async_get(self.hass)
        for row in rows:
            if any(
                row.get(field) is not None and not isinstance(row[field], str) for field in ("area_id", "area_name")
            ):
                raise _FlowError(_INVALID_TARGET)
            if row["action"] not in {"use_existing", "create", "keep_ha"}:
                raise _FlowError(_INVALID_TARGET)
            try:
                decision = normalize_engineering_area_decision(
                    EngineeringAreaDecision(
                        room_uuid=row["group_key"],
                        action=row["action"],
                        area_id=row.get("area_id") or None,
                        area_name=row.get("area_name") or None,
                        conflict_tokens=tuple(item.token for item in groups[row["group_key"]]),
                    )
                )
            except EngineeringSnapshotError:
                raise _FlowError(_INVALID_TARGET) from None
            if decision.action == "use_existing" and areas.async_get_area(decision.area_id) is None:
                raise _FlowError(_INVALID_TARGET)
            if decision.action == "create":
                normalized = ar.normalize_name(decision.area_name)
                if normalized in names or any(
                    ar.normalize_name(area.name) == normalized for area in areas.async_list_areas()
                ):
                    raise _FlowError(_AREA_COLLISION)
                names.add(normalized)
            decisions.append(decision)
        return tuple(sorted(decisions, key=lambda item: item.room_uuid))

    def _validate_devices(
        self, user_input: dict[str, Any], conflicts: tuple[EngineeringAreaConflict, ...]
    ) -> tuple[EngineeringAreaDecision, ...]:
        by_key = {_device_key(item): item for item in conflicts}
        memberships = {
            field: {key for key, item in by_key.items() if bool(item.room_uuid) == (field == "devices")}
            for field in ("devices", "no_room_devices")
        }
        memberships = {field: keys for field, keys in memberships.items() if keys}
        if not isinstance(user_input, dict) or set(user_input) != set(memberships):
            raise _FlowError(_INVALID_ROWS)
        rows = []
        for field, keys in memberships.items():
            rows.extend(
                _exact_rows(
                    {field: user_input[field]},
                    field,
                    "device_key",
                    keys,
                    {"device_key", "description", "action"},
                )
            )
        actions = {}
        for row in rows:
            conflict = by_key[row["device_key"]]
            allowed = {"apply_group", "keep_ha"} if conflict.room_uuid else {"keep_ha", "clear"}
            if row["action"] not in allowed:
                raise _FlowError(_INVALID_TARGET)
            actions[conflict.token] = row["action"]
        decisions = [
            replace(
                decision,
                keep_conflict_tokens=tuple(token for token in decision.conflict_tokens if actions[token] == "keep_ha"),
            )
            for decision in self._decisions
        ]
        decisions.extend(
            EngineeringAreaDecision(room_uuid=None, action=actions[item.token], conflict_tokens=(item.token,))
            for item in sorted(conflicts, key=lambda item: item.token)
            if item.room_uuid is None
        )
        return tuple(decisions)

    async def _load(self, entry_id: str, active: _ActiveBinding) -> tuple[EngineeringAreaConflict, ...]:
        conflicts = await async_load_engineering_area_conflicts(self.hass, entry_id)
        if not _binding_is_current(self.hass, entry_id, active):
            raise _FlowError(_ENTRY_UNAVAILABLE)
        if not _trusted_conflicts(conflicts, entry_id, active):
            raise _FlowError(_REPAIR_UNAVAILABLE)
        return conflicts

    async def _async_step(self, step: str, user_input: dict[str, Any] | None) -> RepairsFlowResult:
        result = await self._async_step_bound(step, user_input)
        if result.get("reason") != "entry_unavailable":
            return result
        # The old flow's lock is released. A fresh binding may synchronize only
        # its issues; it never supplies mutation authority to this old flow.
        validated = _validated_flow_data(self._issue_id, self._flow_data)
        if validated is not None and _active_binding(self.hass, validated[0], require_loaded=True) is not None:
            # Normal coordinator synchronization remains the fallback.
            with suppress(Exception):
                await async_sync_engineering_area_conflict_issues(self.hass, validated[0])
        return result

    async def _async_step_bound(  # noqa: PLR0911, PLR0912 -- explicit bounded Repairs state transitions.
        self, step: str, user_input: dict[str, Any] | None
    ) -> RepairsFlowResult:
        validated = _validated_flow_data(self._issue_id, self._flow_data)
        if validated is None:
            return self.async_abort(reason="invalid_repair")
        entry_id, fingerprint = validated
        active = self._active or _active_binding(self.hass, entry_id, require_loaded=True)
        if active is None or not _binding_is_current(self.hass, entry_id, active):
            return self.async_abort(reason="entry_unavailable")
        self._active = active
        async with active.state.lock:
            if not _binding_is_current(self.hass, entry_id, active):
                return self.async_abort(reason="entry_unavailable")
            try:
                conflicts = await self._load(entry_id, active)
                if not conflicts or _fingerprint(conflicts) != fingerprint:
                    _publish_conflicts(self.hass, entry_id, conflicts)
                    return self.async_abort(reason="conflict_changed")
                if user_input is None:
                    if step == "rooms" and not _room_groups(conflicts):
                        self._decisions = ()
                        return self._form("devices", conflicts)
                    return self._form(step, conflicts)
                if step == "rooms":
                    try:
                        self._decisions = self._validate_rooms(user_input, conflicts)
                    except _FlowError as err:
                        return self._form("rooms", conflicts, user_input, str(err))
                    self._room_input = user_input
                    return self._form("devices", conflicts)
                if self._decisions is None:
                    return self._form("rooms", conflicts, error="invalid_rows")
                try:
                    decisions = self._validate_devices(user_input, conflicts)
                except _FlowError as err:
                    return self._form("devices", conflicts, user_input, str(err))
                # Repeat exact observation immediately before entering the batch boundary.
                conflicts = await self._load(entry_id, active)
                if not conflicts or _fingerprint(conflicts) != fingerprint:
                    _publish_conflicts(self.hass, entry_id, conflicts)
                    return self.async_abort(reason="conflict_changed")
                result = await async_resolve_engineering_area_conflicts(
                    self.hass,
                    entry_id,
                    decisions,
                    is_current=lambda: _binding_is_current(self.hass, entry_id, active),
                )
                if not _binding_is_current(self.hass, entry_id, active):
                    return self.async_abort(reason="entry_unavailable")
                remaining = await self._load(entry_id, active)
                _publish_conflicts(self.hass, entry_id, remaining)
                if not remaining and result.reason == "resolved" and result.unresolved_groups == 0:
                    # No await between authoritative empty publication and manager completion.
                    return self.async_create_entry(data={})
                if remaining and _fingerprint(remaining) != fingerprint:
                    return self.async_abort(reason="conflict_changed")
                if result.reason == "area_name_collision":
                    return self._form("rooms", remaining, self._room_input, "area_name_collision")
                return self.async_abort(reason="resolution_failed")
            except asyncio.CancelledError:
                raise
            except _FlowError as err:
                reason = str(err) if str(err) in {"entry_unavailable", "repair_unavailable"} else "repair_unavailable"
                return self.async_abort(reason=reason)
            except Exception:  # noqa: BLE001 -- no storage errors or private values leave the adapter.
                return self.async_abort(reason="repair_unavailable")

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Discard only HA manager's exact initialization envelope."""
        if user_input is getattr(self, "init_data", None) and user_input == {"issue_id": self._issue_id}:
            user_input = None
        return await self._async_step("rooms", user_input)

    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Validate every room-group choice without writing any registry."""
        return await self._async_step("rooms", user_input)

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Validate overrides and submit the exact batch to the registry boundary."""
        return await self._async_step("devices", user_input)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a defensive flow with untrusted issue registry input."""
    del hass
    return EngineeringAreaConflictFixFlow(issue_id, data)
