"""Native Repairs flows for explicit engineering area decisions."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import DOMAIN
from .engineering_registry import (
    EngineeringAreaConflict,
    async_load_engineering_area_conflicts,
    async_resolve_engineering_area_conflict,
)
from .engineering_snapshot import (
    EngineeringSnapshotError,
    validate_engineering_presentation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_ISSUE_KIND = "engineering_area_conflict"
_ISSUE_VERSION = 1
_ISSUE_PREFIX = f"{_ISSUE_KIND}_"
_RECONCILER_DATA = f"{DOMAIN}_engineering_area_reconcilers"
_ENTRY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONFLICT_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({"apply_loxone_room", "keep_ha_room"})
_INVALID_IDENTITY = "invalid engineering area conflict identity"


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


def engineering_area_conflict_issue_id(entry_id: str, conflict_token: str) -> str:
    """Build an issue identity fenced by config entry and exact conflict token."""
    if not _ENTRY_ID.fullmatch(entry_id) or not _CONFLICT_TOKEN.fullmatch(conflict_token):
        raise ValueError(_INVALID_IDENTITY)
    return f"{_owned_issue_prefix(entry_id)}{conflict_token}"


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


def _room_presentation(
    conflict: EngineeringAreaConflict,
) -> tuple[str, dict[str, str]]:
    current_name = _safe_text(conflict.current_area_name)
    desired_name = _safe_text(conflict.desired_area_name)
    current_state = "none" if conflict.current_area_id is None else "named" if current_name is not None else "unknown"
    if not conflict.desired_action_valid:
        desired_state = "invalid"
    elif desired_name is not None:
        desired_state = "named"
    elif conflict.desired_area_id is None:
        desired_state = "none"
    else:
        desired_state = "unknown"
    placeholders = {"device": _device_reference(conflict)}
    if current_name is not None and current_state == "named":
        placeholders["current_room"] = current_name
    if desired_name is not None and desired_state == "named":
        placeholders["desired_room"] = desired_name
    return f"current_{current_state}_desired_{desired_state}", placeholders


def _issue_placeholders(conflict: EngineeringAreaConflict) -> dict[str, str]:
    return {"device": _device_reference(conflict)}


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


def _create_issue(
    hass: HomeAssistant,
    entry_id: str,
    conflict: EngineeringAreaConflict,
) -> str:
    issue_id = engineering_area_conflict_issue_id(entry_id, conflict.token)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        data={
            "kind": _ISSUE_KIND,
            "version": _ISSUE_VERSION,
            "entry_id": entry_id,
            "conflict_token": conflict.token,
        },
        is_fixable=True,
        is_persistent=True,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_ISSUE_KIND,
        translation_placeholders=_issue_placeholders(conflict),
    )
    return issue_id


async def _async_reconcile_locked(
    hass: HomeAssistant,
    entry_id: str,
    active: _ActiveBinding,
    *,
    require_loaded: bool,
) -> bool:
    """Load and reconcile one authoritative set while its entry lock is held."""
    conflicts = await async_load_engineering_area_conflicts(hass, entry_id)
    if not _binding_is_current(
        hass,
        entry_id,
        active,
        require_loaded=require_loaded,
    ):
        return False
    desired_issue_ids: set[str] = set()
    for conflict in conflicts:
        if (
            conflict.entry_id != entry_id
            or not _CONFLICT_TOKEN.fullmatch(conflict.token)
            or not _conflict_belongs_to_provider(conflict, active.provider_identifier)
        ):
            continue
        desired_issue_ids.add(_create_issue(hass, entry_id, conflict))
    for issue_id in _owned_issue_ids(hass, entry_id) - desired_issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    return True


async def async_sync_engineering_area_conflict_issues(
    hass: HomeAssistant,
    entry_id: str,
    *,
    config_entry: ConfigEntry | None = None,
    coordinator: object | None = None,
) -> None:
    """Serialize durable Task 5 conflicts into entry-scoped native issues."""
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
        "conflict_token",
    }:
        return None
    entry_id = data.get("entry_id")
    token = data.get("conflict_token")
    if (
        data.get("kind") != _ISSUE_KIND
        or data.get("version") != _ISSUE_VERSION
        or not isinstance(entry_id, str)
        or not isinstance(token, str)
        or not _ENTRY_ID.fullmatch(entry_id)
        or not _CONFLICT_TOKEN.fullmatch(token)
        or issue_id != engineering_area_conflict_issue_id(entry_id, token)
    ):
        return None
    return entry_id, token


class EngineeringAreaConflictFixFlow(RepairsFlow):
    """Show and resolve one exact current area conflict."""

    def __init__(
        self,
        issue_id: str,
        data: dict[str, str | int | float | None] | None,
    ) -> None:
        """Initialize a flow with untrusted issue registry input."""
        super().__init__()
        self._issue_id = issue_id
        self._flow_data = data

    async def _async_secondary_reconcile(
        self,
        entry_id: str,
        active: _ActiveBinding,
    ) -> bool:
        try:
            return await _async_reconcile_locked(
                self.hass,
                entry_id,
                active,
                require_loaded=True,
            )
        except Exception:  # noqa: BLE001 -- return only a translated bounded reason.
            return False

    async def _async_step(  # noqa: PLR0911, PLR0912 -- explicit Repairs outcomes are the flow state machine.
        self,
        user_input: dict[str, Any] | None,
    ) -> RepairsFlowResult:
        validated = _validated_flow_data(self._issue_id, self._flow_data)
        if validated is None:
            return self.async_abort(reason="invalid_repair")
        entry_id, token = validated
        active = _active_binding(self.hass, entry_id, require_loaded=True)
        if active is None:
            if _configured_entry(self.hass, entry_id) is None:
                async_remove_engineering_area_conflict_issues(self.hass, entry_id)
            return self.async_abort(reason="entry_unavailable")
        async with active.state.lock:
            if not _binding_is_current(self.hass, entry_id, active):
                return self.async_abort(reason="entry_unavailable")
            try:
                conflicts = await async_load_engineering_area_conflicts(self.hass, entry_id)
            except Exception:  # noqa: BLE001 -- never expose private storage details.
                return self.async_abort(reason="repair_unavailable")
            if not _binding_is_current(self.hass, entry_id, active):
                return self.async_abort(reason="entry_unavailable")
            conflict = next(
                (
                    item
                    for item in conflicts
                    if item.entry_id == entry_id
                    and item.token == token
                    and _conflict_belongs_to_provider(item, active.provider_identifier)
                ),
                None,
            )
            if conflict is None:
                if not await self._async_secondary_reconcile(entry_id, active):
                    return self.async_abort(reason="repair_unavailable")
                return self.async_abort(reason="conflict_changed")
            step_id, placeholders = _room_presentation(conflict)
            if user_input is None:
                actions = ("keep_ha_room",) if not conflict.desired_action_valid else tuple(sorted(_ACTIONS))
                return self.async_show_form(
                    step_id=step_id,
                    data_schema=vol.Schema(
                        {
                            vol.Required("action"): SelectSelector(
                                SelectSelectorConfig(
                                    options=list(actions),
                                    translation_key="engineering_area_conflict_action",
                                )
                            )
                        }
                    ),
                    description_placeholders=placeholders,
                )

            action = user_input.get("action")
            if set(user_input) != {"action"} or not isinstance(action, str) or action not in _ACTIONS:
                if not await self._async_secondary_reconcile(entry_id, active):
                    return self.async_abort(reason="repair_unavailable")
                return self.async_abort(reason="invalid_action")
            if not conflict.desired_action_valid and action != "keep_ha_room":
                if not await self._async_secondary_reconcile(entry_id, active):
                    return self.async_abort(reason="repair_unavailable")
                return self.async_abort(reason="invalid_desired_area")
            if not _binding_is_current(self.hass, entry_id, active):
                return self.async_abort(reason="entry_unavailable")
            try:
                result = await async_resolve_engineering_area_conflict(
                    self.hass,
                    entry_id,
                    conflict.token,
                    action,
                    is_current=lambda: _binding_is_current(
                        self.hass,
                        entry_id,
                        active,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- never surface private storage details.
                return self.async_abort(reason="resolution_failed")
            if not _binding_is_current(self.hass, entry_id, active):
                return self.async_abort(reason="entry_unavailable")
            reason = {
                "stale_conflict": "conflict_changed",
                "invalid_desired_area": "invalid_desired_area",
                "invalid_action": "invalid_action",
                "lifecycle_changed": "entry_unavailable",
            }.get(result.reason, "resolution_failed")
            reconciled = await self._async_secondary_reconcile(entry_id, active)
            if result.resolved:
                return self.async_create_entry(data={})
            if not reconciled:
                return self.async_abort(reason="repair_unavailable")
            return self.async_abort(reason=reason)

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> RepairsFlowResult:
        """Load the exact current conflict and apply only an explicit choice."""
        if user_input is getattr(self, "init_data", None) and user_input == {"issue_id": self._issue_id}:
            user_input = None
        return await self._async_step(user_input)

    async_step_current_named_desired_named = async_step_init
    async_step_current_named_desired_none = async_step_init
    async_step_current_named_desired_unknown = async_step_init
    async_step_current_named_desired_invalid = async_step_init
    async_step_current_none_desired_named = async_step_init
    async_step_current_none_desired_none = async_step_init
    async_step_current_none_desired_unknown = async_step_init
    async_step_current_none_desired_invalid = async_step_init
    async_step_current_unknown_desired_named = async_step_init
    async_step_current_unknown_desired_none = async_step_init
    async_step_current_unknown_desired_unknown = async_step_init
    async_step_current_unknown_desired_invalid = async_step_init


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a defensive flow that validates all issue input on first use."""
    del hass
    return EngineeringAreaConflictFixFlow(issue_id, data)
