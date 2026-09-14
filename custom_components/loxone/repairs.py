"""Native Repairs flows for explicit engineering area decisions."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
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
    from homeassistant.core import HomeAssistant

_ISSUE_KIND = "engineering_area_conflict"
_ISSUE_VERSION = 1
_ISSUE_PREFIX = f"{_ISSUE_KIND}_"
_ENTRY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONFLICT_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({"apply_loxone_room", "keep_ha_room"})
_NO_ROOM = "No room"
_INVALID_IDENTITY = "invalid engineering area conflict identity"


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


def _safe_text(value: str | None, fallback: str) -> str:
    if value is None:
        return fallback
    try:
        safe = validate_engineering_presentation(value)
    except EngineeringSnapshotError:
        return fallback
    return safe or fallback


def _conflict_placeholders(conflict: EngineeringAreaConflict) -> dict[str, str]:
    device_fallback = f"Device {sha256(conflict.device_identifier.encode()).hexdigest()[:8]}"
    return {
        "device": _safe_text(conflict.display_name, device_fallback),
        "current_room": _safe_text(conflict.current_area_name, _NO_ROOM),
        "desired_room": _safe_text(conflict.desired_area_name, _NO_ROOM),
    }


def _configured_entry(hass: HomeAssistant, entry_id: str) -> object | None:
    manager = getattr(hass, "config_entries", None)
    getter = getattr(manager, "async_get_entry", None)
    if not callable(getter):
        return None
    entry = getter(entry_id)
    return entry if entry is not None and getattr(entry, "domain", None) == DOMAIN else None


def _owned_issue_ids(hass: HomeAssistant, entry_id: str) -> set[str]:
    prefix = _owned_issue_prefix(entry_id)
    issues = getattr(ir.async_get(hass), "issues", {})
    return {issue_id for domain, issue_id in tuple(issues) if domain == DOMAIN and issue_id.startswith(prefix)}


def async_remove_engineering_area_conflict_issues(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Remove only native area-conflict issues owned by one unloaded entry."""
    if not _ENTRY_ID.fullmatch(entry_id):
        return
    for issue_id in _owned_issue_ids(hass, entry_id):
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_sync_engineering_area_conflict_issues(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Reconcile durable Task 5 conflicts into entry-scoped native issues."""
    if not _ENTRY_ID.fullmatch(entry_id):
        return
    if _configured_entry(hass, entry_id) is None:
        async_remove_engineering_area_conflict_issues(hass, entry_id)
        return

    conflicts = await async_load_engineering_area_conflicts(hass, entry_id)
    desired_issue_ids: set[str] = set()
    for conflict in conflicts:
        if conflict.entry_id != entry_id or not _CONFLICT_TOKEN.fullmatch(conflict.token):
            continue
        issue_id = engineering_area_conflict_issue_id(entry_id, conflict.token)
        desired_issue_ids.add(issue_id)
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
            translation_placeholders=_conflict_placeholders(conflict),
        )

    for issue_id in _owned_issue_ids(hass, entry_id) - desired_issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


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

    async def _async_current_conflict(
        self,
    ) -> tuple[EngineeringAreaConflict | None, str]:
        validated = _validated_flow_data(self._issue_id, self._flow_data)
        if validated is None:
            return None, "invalid_repair"
        entry_id, token = validated
        if _configured_entry(self.hass, entry_id) is None:
            async_remove_engineering_area_conflict_issues(self.hass, entry_id)
            return None, "entry_unavailable"
        try:
            conflicts = await async_load_engineering_area_conflicts(self.hass, entry_id)
        except Exception:  # noqa: BLE001 -- flow reports a bounded retry reason.
            return None, "repair_unavailable"
        conflict = next(
            (item for item in conflicts if item.entry_id == entry_id and item.token == token),
            None,
        )
        if conflict is None:
            await async_sync_engineering_area_conflict_issues(self.hass, entry_id)
            return None, "conflict_changed"
        if not conflict.desired_action_valid:
            await async_sync_engineering_area_conflict_issues(self.hass, entry_id)
            return None, "invalid_desired_area"
        return conflict, ""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> RepairsFlowResult:
        """Display current evidence, then apply only an explicit exact choice."""
        conflict, abort_reason = await self._async_current_conflict()
        if conflict is None:
            return self.async_abort(reason=abort_reason)
        if user_input is None:
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(
                    {
                        vol.Required("action"): SelectSelector(
                            SelectSelectorConfig(
                                options=sorted(_ACTIONS),
                                translation_key="engineering_area_conflict_action",
                            )
                        )
                    }
                ),
                description_placeholders=_conflict_placeholders(conflict),
            )

        action = user_input.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            await async_sync_engineering_area_conflict_issues(self.hass, conflict.entry_id)
            return self.async_abort(reason="invalid_action")
        try:
            result = await async_resolve_engineering_area_conflict(
                self.hass,
                conflict.entry_id,
                conflict.token,
                action,
            )
        except Exception:  # noqa: BLE001 -- never surface private storage details.
            return self.async_abort(reason="resolution_failed")
        reason = {
            "stale_conflict": "conflict_changed",
            "invalid_desired_area": "invalid_desired_area",
            "invalid_action": "invalid_action",
        }.get(result.reason, "resolution_failed")
        flow_result = self.async_create_entry(data={}) if result.resolved else self.async_abort(reason=reason)
        try:
            await async_sync_engineering_area_conflict_issues(self.hass, conflict.entry_id)
        except Exception:  # noqa: BLE001 -- resolution result remains authoritative.
            if not result.resolved:
                flow_result = self.async_abort(reason="resolution_failed")
        return flow_result


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a defensive flow that validates all issue input on first use."""
    del hass
    return EngineeringAreaConflictFixFlow(issue_id, data)
