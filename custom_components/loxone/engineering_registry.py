"""Plan and apply source-scoped Home Assistant engineering registry topology."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from hashlib import sha256
from secrets import token_hex
from types import MappingProxyType
from typing import TYPE_CHECKING

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .engineering_capabilities import CapabilityState, ExposureStatus
from .engineering_entities import (
    EngineeringEntitySpec,
    build_engineering_entity_specs,
)
from .engineering_snapshot import (
    EngineeringSnapshot,
    EngineeringSnapshotError,
    EngineeringStateStore,
    StoredEngineeringState,
    async_load_engineering_state,
    async_store_engineering_state,
    validate_engineering_presentation,
    validate_engineering_snapshot,
)
from .engineering_topology import NodeKind, ResolutionStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant


class EngineeringRegistryError(RuntimeError):
    """Raised when a deterministic registry plan cannot be fully applied."""


_SNAPSHOT_ENTRY_MISMATCH = "engineering snapshot belongs to a different config entry"
_STATE_PLAN_MISMATCH = "committed engineering state does not match registry plan"
_DEVICE_MISSING = "engineering device was not created"
_VIA_DEVICE_MISSING = "engineering via device was not created"
_ENTITY_OWNER_MISSING = "engineering entity owner was not created"
_CONFIG_ENTRY_LOOKUP_ERRORS = (AttributeError, TypeError, ValueError)
_INTENT_STORAGE_VERSION = 1
_INTENT_STORAGE_KEY = f"{DOMAIN}.engineering_registry_intent"
_AREA_PROCESS_TOKEN = token_hex(16)
_AREA_CONFLICT_REASONS = frozenset({"area_assignment_unverified", "area_user_override_preserved"})
_AREA_RESOLUTION_ACTIONS = frozenset({"apply_loxone_room", "keep_ha_room"})
_MAX_AREA_TEXT_LENGTH = 160
_CONTROL_CHARACTER_LIMIT = 32
_CONFLICT_TOKEN_LENGTH = 64
_AREA_OPERATION_LOCKS = "loxone_engineering_area_operation_locks"
_AREA_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")

# Kept as a named seam so cold-restart tests exercise acknowledged Store behavior.
EngineeringRegistryIntentStore = EngineeringStateStore


def _area_operation_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Serialize whole journal/apply/resolution transitions for one entry."""
    locks = hass.data.setdefault(_AREA_OPERATION_LOCKS, {})
    return locks.setdefault(entry_id, asyncio.Lock())


@dataclass(frozen=True, slots=True)
class EngineeringRegistryMetadata:
    """Safe integration-owned registry reconciliation metadata."""

    active_device_identifiers: frozenset[str]
    room_names: frozenset[str]
    managed_area_ids: Mapping[str, str]
    applied_generation: str | None = None
    provider_identifier: str | None = None

    def __post_init__(self) -> None:
        """Detach managed metadata from mutable caller-owned mappings."""
        object.__setattr__(
            self,
            "managed_area_ids",
            MappingProxyType(dict(sorted(self.managed_area_ids.items()))),
        )

    @classmethod
    def empty(cls) -> EngineeringRegistryMetadata:
        """Return metadata for an entry without an applied engineering snapshot."""
        return cls(frozenset(), frozenset(), {}, None)


@dataclass(frozen=True, slots=True)
class EngineeringDeviceOperation:
    """One stable desired device-registry end state."""

    entry_id: str
    identifier: str
    name: str
    model: str
    room: str | None
    via_identifier: str | None
    previous_managed_area_id: str | None
    area_update_allowed: bool
    expected_area_id: str | None = None
    area_intent_replay: bool = False
    area_conflict_reason: str | None = None
    area_released: bool = False
    area_process_token: str | None = None


@dataclass(frozen=True, slots=True)
class EngineeringEntityOperation:
    """One existing entity's desired engineering owner association."""

    entry_id: str
    entity_id: str
    unique_id: str
    owner_identifier: str
    original_name: str
    platform: str
    spec: EngineeringEntitySpec
    legacy_reassociation: bool = False


@dataclass(frozen=True, slots=True)
class EngineeringEntityIdentityRejection:
    """Inventory-only result for an unsafe global entity identity."""

    spec: EngineeringEntitySpec
    entity_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class EngineeringRegistryPlan:
    """Immutable mutation-free registry plan for one committed generation."""

    generation_id: str
    device_operations: tuple[EngineeringDeviceOperation, ...]
    entity_operations: tuple[EngineeringEntityOperation, ...]
    metadata: EngineeringRegistryMetadata
    rejected_entities: tuple[EngineeringEntityIdentityRejection, ...] = ()
    ambiguous_legacy_identifiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineeringRegistrySyncResult:
    """Observable result of one fully applied registry plan."""

    created_identifiers: tuple[str, ...]
    updated_identifiers: tuple[str, ...]
    migrated_entities: int
    metadata: EngineeringRegistryMetadata
    rejected_entities: tuple[EngineeringEntityIdentityRejection, ...] = ()
    ambiguous_legacy_identifiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineeringAreaConflict:
    """Sanitized immutable area conflict for a later Repairs UI."""

    token: str
    entry_id: str
    device_identifier: str
    display_name: str | None
    current_area_id: str | None
    current_area_name: str | None
    desired_area_id: str | None
    desired_area_name: str | None
    generation_id: str
    reason: str
    desired_action_valid: bool = True

    def __post_init__(self) -> None:
        """Sanitize permitted operational fields without manufacturing a clear."""
        desired_name = _optional_bounded_string(self.desired_area_name)
        desired_id = _optional_area_id(self.desired_area_id)
        object.__setattr__(
            self,
            "desired_action_valid",
            self.desired_action_valid is True
            and desired_name == self.desired_area_name
            and desired_id == self.desired_area_id,
        )
        for field in ("display_name", "current_area_name", "desired_area_name"):
            object.__setattr__(self, field, _optional_bounded_string(getattr(self, field)))
        object.__setattr__(self, "desired_area_id", desired_id)


@dataclass(frozen=True, slots=True)
class EngineeringAreaResolutionResult:
    """Idempotent result returned by the Task 8 resolution boundary."""

    resolved: bool
    action: str
    reason: str
    conflict: EngineeringAreaConflict | None = None


@dataclass(frozen=True, slots=True)
class _AreaIntent:
    """One acknowledged pre-mutation area assignment intent."""

    identifier: str
    from_area_id: str | None
    to_room: str | None
    process_token: str | None = None


@dataclass(frozen=True, slots=True)
class _ManagedAreaBaseline:
    """Last acknowledged integration-owned area, including an explicit clear."""

    identifier: str
    area_id: str | None
    process_token: str | None


@dataclass(frozen=True, slots=True)
class _AreaRegistryState:
    """Private acknowledged area synchronization state."""

    generation_id: str | None
    intents: Mapping[str, _AreaIntent]
    baselines: Mapping[str, _ManagedAreaBaseline]
    conflicts: Mapping[str, EngineeringAreaConflict]
    released_identifiers: frozenset[str]


@dataclass(frozen=True, slots=True)
class _AreaConflictContext:
    """One bounded conflict observation."""

    current_area_id: str | None
    desired_area_id: str | None
    generation_id: str
    reason: str


def _area_state_store(hass: HomeAssistant, entry_id: str) -> EngineeringRegistryIntentStore:
    """Return the private acknowledged area-state Store."""
    return EngineeringRegistryIntentStore(
        hass,
        _INTENT_STORAGE_VERSION,
        f"{_INTENT_STORAGE_KEY}.{entry_id}",
        private=True,
    )


def _optional_bounded_string(value: object) -> str | None:
    """Accept only bounded display data safe for persisted Repairs details."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_AREA_TEXT_LENGTH
        or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value)
        or "://" in value
        or "@" in value
    ):
        return None
    try:
        return validate_engineering_presentation(value)
    except EngineeringSnapshotError:
        return None


def _optional_area_id(value: object) -> str | None:
    """Validate an opaque Home Assistant area identifier."""
    if value is None:
        return None
    return value if isinstance(value, str) and _AREA_IDENTIFIER_PATTERN.fullmatch(value) else None


async def _async_load_area_state(
    hass: HomeAssistant,
    entry_id: str,
) -> _AreaRegistryState:
    """Load bounded private state without consulting registry timestamps."""
    store = _area_state_store(hass, entry_id)
    raw = await store.async_load()
    if not isinstance(raw, dict):
        raw = {}
    generation_id = raw.get("generation_id")
    if not isinstance(generation_id, str):
        generation_id = None
    intents: dict[str, _AreaIntent] = {}
    for item in raw.get("area_intents", ()):
        if not isinstance(item, dict):
            continue
        identifier = item.get("identifier")
        from_area_id = item.get("from_area_id")
        to_room = item.get("to_room")
        process_token = item.get("process_token")
        if (
            not isinstance(identifier, str)
            or not identifier
            or (from_area_id is not None and not isinstance(from_area_id, str))
            or (to_room is not None and not isinstance(to_room, str))
            or (process_token is not None and not isinstance(process_token, str))
        ):
            continue
        intents[identifier] = _AreaIntent(
            identifier,
            from_area_id,
            to_room,
            process_token,
        )
    baselines: dict[str, _ManagedAreaBaseline] = {}
    for item in raw.get("managed_baselines", ()):
        if not isinstance(item, dict):
            continue
        identifier = item.get("identifier")
        area_id = item.get("area_id")
        process_token = item.get("process_token")
        if (
            not isinstance(identifier, str)
            or not identifier
            or (area_id is not None and not isinstance(area_id, str))
            or (process_token is not None and not isinstance(process_token, str))
        ):
            continue
        baselines[identifier] = _ManagedAreaBaseline(identifier, area_id, process_token)
    conflicts: dict[str, EngineeringAreaConflict] = {}
    for item in raw.get("area_conflicts", ()):
        if not isinstance(item, dict):
            continue
        token = item.get("token")
        identifier = item.get("device_identifier")
        conflict_entry = item.get("entry_id")
        conflict_generation = item.get("generation_id")
        reason = item.get("reason")
        if (
            not isinstance(token, str)
            or len(token) != _CONFLICT_TOKEN_LENGTH
            or not isinstance(identifier, str)
            or not identifier
            or conflict_entry != entry_id
            or _optional_area_id(identifier) != identifier
            or _optional_area_id(item.get("current_area_id")) != item.get("current_area_id")
            or not isinstance(conflict_generation, str)
            or reason not in _AREA_CONFLICT_REASONS
        ):
            continue
        conflicts[identifier] = EngineeringAreaConflict(
            token=token,
            entry_id=entry_id,
            device_identifier=identifier,
            display_name=item.get("display_name"),
            current_area_id=_optional_area_id(item.get("current_area_id")),
            current_area_name=item.get("current_area_name"),
            desired_area_id=item.get("desired_area_id"),
            desired_area_name=item.get("desired_area_name"),
            generation_id=conflict_generation,
            reason=reason,
            desired_action_valid=item.get("desired_action_valid", True),
        )
    released = frozenset(item for item in raw.get("released_identifiers", ()) if isinstance(item, str) and item)
    return _AreaRegistryState(
        generation_id,
        MappingProxyType(intents),
        MappingProxyType(baselines),
        MappingProxyType(conflicts),
        released,
    )


async def _async_store_area_state(
    hass: HomeAssistant,
    entry_id: str,
    state: _AreaRegistryState,
) -> None:
    """Acknowledge the complete bounded area state."""
    payload = {
        "generation_id": state.generation_id,
        "area_intents": [
            {
                "identifier": item.identifier,
                "from_area_id": item.from_area_id,
                "to_room": item.to_room,
                "process_token": item.process_token,
            }
            for item in sorted(state.intents.values(), key=lambda value: value.identifier)
        ],
        "managed_baselines": [
            {
                "identifier": item.identifier,
                "area_id": item.area_id,
                "process_token": item.process_token,
            }
            for item in sorted(state.baselines.values(), key=lambda value: value.identifier)
        ],
        "area_conflicts": [
            {
                "token": item.token,
                "entry_id": item.entry_id,
                "device_identifier": item.device_identifier,
                "display_name": item.display_name,
                "current_area_id": item.current_area_id,
                "current_area_name": item.current_area_name,
                "desired_area_id": item.desired_area_id,
                "desired_area_name": item.desired_area_name,
                "generation_id": item.generation_id,
                "reason": item.reason,
                "desired_action_valid": item.desired_action_valid,
            }
            for item in sorted(state.conflicts.values(), key=lambda value: value.device_identifier)
        ],
        "released_identifiers": sorted(state.released_identifiers),
    }
    await _area_state_store(hass, entry_id).async_save_acknowledged(payload)


async def async_load_engineering_area_conflicts(
    hass: HomeAssistant,
    entry_id: str,
) -> tuple[EngineeringAreaConflict, ...]:
    """Return immutable sanitized conflicts for the future native Repairs flow."""
    state = await _async_load_area_state(hass, entry_id)
    return tuple(sorted(state.conflicts.values(), key=lambda item: item.device_identifier))


def _conflict_for_token(state: _AreaRegistryState, token: str) -> EngineeringAreaConflict | None:
    """Find one exact immutable conflict token."""
    return next(
        (item for item in state.conflicts.values() if item.token == token),
        None,
    )


def _resolution_state(
    state: _AreaRegistryState,
    identifier: str,
    *,
    baseline: _ManagedAreaBaseline | None,
    release: bool,
) -> _AreaRegistryState:
    """Return a state with one conflict resolved without mutating the input."""
    intents = dict(state.intents)
    baselines = dict(state.baselines)
    conflicts = dict(state.conflicts)
    released = set(state.released_identifiers)
    intents.pop(identifier, None)
    conflicts.pop(identifier, None)
    if baseline is None:
        baselines.pop(identifier, None)
    else:
        baselines[identifier] = baseline
    if release:
        released.add(identifier)
    else:
        released.discard(identifier)
    return _AreaRegistryState(
        state.generation_id,
        MappingProxyType(intents),
        MappingProxyType(baselines),
        MappingProxyType(conflicts),
        frozenset(released),
    )


def _area_resolution_result(
    action: str,
    reason: str,
    conflict: EngineeringAreaConflict | None = None,
    *,
    resolved: bool,
) -> EngineeringAreaResolutionResult:
    """Build one explicit resolution result without positional booleans."""
    return EngineeringAreaResolutionResult(
        resolved=resolved,
        action=action,
        reason=reason,
        conflict=conflict,
    )


async def _async_keep_ha_area(
    hass: HomeAssistant,
    entry_id: str,
    state: _AreaRegistryState,
    conflict: EngineeringAreaConflict,
) -> EngineeringAreaResolutionResult:
    """Permanently release automatic ownership for an exact conflict."""
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device_by_identifier((DOMAIN, conflict.device_identifier), entry_id)
    current_area = getattr(device, "area_id", None) if device else None
    if device is None or current_area != conflict.current_area_id:
        return _area_resolution_result("keep_ha_room", "stale_conflict", conflict, resolved=False)
    resolved_state = _resolution_state(
        state,
        conflict.device_identifier,
        baseline=None,
        release=True,
    )
    await _async_store_area_state(hass, entry_id, resolved_state)
    device = device_registry.async_get_device_by_identifier((DOMAIN, conflict.device_identifier), entry_id)
    current_area = getattr(device, "area_id", None) if device else None
    if device is None or current_area != conflict.current_area_id:
        refreshed = await _async_persist_resolution_race(hass, entry_id, resolved_state, conflict, current_area)
        return _area_resolution_result("keep_ha_room", "stale_conflict", refreshed, resolved=False)
    return _area_resolution_result("keep_ha_room", "resolved", conflict, resolved=True)


async def _async_persist_resolution_race(
    hass: HomeAssistant,
    entry_id: str,
    state: _AreaRegistryState,
    conflict: EngineeringAreaConflict,
    current_area_id: str | None,
) -> EngineeringAreaConflict:
    """Relinquish authority and persist a concurrent HA assignment."""
    refreshed = _refresh_area_conflict(
        conflict,
        ar.async_get(hass),
        current_area_id,
        "area_user_override_preserved",
    )
    conflicts = dict(state.conflicts)
    conflicts[conflict.device_identifier] = refreshed
    intents = dict(state.intents)
    intents.pop(conflict.device_identifier, None)
    baselines = dict(state.baselines)
    baselines.pop(conflict.device_identifier, None)
    while True:
        await _async_store_area_state(
            hass,
            entry_id,
            replace(
                state,
                intents=MappingProxyType(intents),
                baselines=MappingProxyType(baselines),
                conflicts=MappingProxyType(dict(conflicts)),
            ),
        )
        device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, conflict.device_identifier), entry_id)
        current_area = getattr(device, "area_id", None) if device else None
        if current_area == refreshed.current_area_id:
            return refreshed
        refreshed = _refresh_area_conflict(refreshed, ar.async_get(hass), current_area, "area_user_override_preserved")
        conflicts[conflict.device_identifier] = refreshed


def _resolution_desired_area_id(
    area_registry: object,
    conflict: EngineeringAreaConflict,
) -> tuple[bool, str | None]:
    """Resolve or create the explicitly selected Loxone area."""
    desired_area_id = conflict.desired_area_id
    if conflict.desired_area_name is None:
        return True, desired_area_id
    desired_area = area_registry.async_get_area_by_name(conflict.desired_area_name)
    if desired_area is None:
        desired_area = area_registry.async_get_or_create(conflict.desired_area_name)
    if desired_area_id is not None and desired_area.id != desired_area_id:
        return False, None
    return True, desired_area.id


async def _async_apply_loxone_area(
    hass: HomeAssistant,
    entry_id: str,
    state: _AreaRegistryState,
    conflict: EngineeringAreaConflict,
) -> EngineeringAreaResolutionResult:
    """Apply one exact explicit Loxone-area decision with await rechecks."""
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    device = device_registry.async_get_device_by_identifier((DOMAIN, conflict.device_identifier), entry_id)
    current_area = getattr(device, "area_id", None) if device else None
    if device is None or current_area != conflict.current_area_id:
        refreshed = await _async_persist_resolution_race(hass, entry_id, state, conflict, current_area)
        return _area_resolution_result("apply_loxone_room", "stale_conflict", refreshed, resolved=False)

    intent = _AreaIntent(
        conflict.device_identifier,
        conflict.current_area_id,
        conflict.desired_area_name,
        _AREA_PROCESS_TOKEN,
    )
    intents = dict(state.intents)
    intents[conflict.device_identifier] = intent
    prepared = replace(state, intents=MappingProxyType(intents))
    await _async_store_area_state(hass, entry_id, prepared)
    reloaded = await _async_load_area_state(hass, entry_id)
    current_conflict = _conflict_for_token(reloaded, conflict.token)
    current_device = device_registry.async_get_device_by_identifier((DOMAIN, conflict.device_identifier), entry_id)
    rechecked_area_id = getattr(current_device, "area_id", None) if current_device else None
    if current_conflict is None or current_device is None or rechecked_area_id != conflict.current_area_id:
        if current_conflict is not None:
            current_conflict = await _async_persist_resolution_race(
                hass,
                entry_id,
                reloaded,
                current_conflict,
                rechecked_area_id,
            )
        return _area_resolution_result("apply_loxone_room", "stale_conflict", current_conflict, resolved=False)
    desired_valid, desired_area_id = _resolution_desired_area_id(area_registry, conflict)
    if not desired_valid:
        return _area_resolution_result("apply_loxone_room", "stale_conflict", current_conflict, resolved=False)
    current_device = device_registry.async_get_device_by_identifier((DOMAIN, conflict.device_identifier), entry_id)
    if current_device is None or getattr(current_device, "area_id", None) != conflict.current_area_id:
        return _area_resolution_result("apply_loxone_room", "stale_conflict", current_conflict, resolved=False)
    if conflict.current_area_id != desired_area_id:
        device_registry.async_update_device(
            current_device.id,
            area_id=desired_area_id,
        )
    resolved_state = _resolution_state(
        reloaded,
        conflict.device_identifier,
        baseline=_ManagedAreaBaseline(
            conflict.device_identifier,
            desired_area_id,
            _AREA_PROCESS_TOKEN,
        ),
        release=False,
    )
    await _async_store_area_state(hass, entry_id, resolved_state)
    current_device = device_registry.async_get_device_by_identifier((DOMAIN, conflict.device_identifier), entry_id)
    final_area_id = getattr(current_device, "area_id", None) if current_device else None
    if current_device is None or final_area_id != desired_area_id:
        refreshed = await _async_persist_resolution_race(
            hass,
            entry_id,
            resolved_state,
            conflict,
            final_area_id,
        )
        return _area_resolution_result("apply_loxone_room", "stale_conflict", refreshed, resolved=False)
    return _area_resolution_result("apply_loxone_room", "resolved", conflict, resolved=True)


async def async_resolve_engineering_area_conflict(
    hass: HomeAssistant,
    entry_id: str,
    conflict_token: str,
    action: str,
) -> EngineeringAreaResolutionResult:
    """Apply an explicit stale-safe Task 8 area-conflict decision."""
    async with _area_operation_lock(hass, entry_id):
        return await _async_resolve_engineering_area_conflict(hass, entry_id, conflict_token, action)


async def _async_resolve_engineering_area_conflict(
    hass: HomeAssistant,
    entry_id: str,
    conflict_token: str,
    action: str,
) -> EngineeringAreaResolutionResult:
    """Resolve against current state while holding the entry transition lock."""
    if action not in _AREA_RESOLUTION_ACTIONS:
        return _area_resolution_result(action, "invalid_action", resolved=False)
    state = await _async_load_area_state(hass, entry_id)
    conflict = _conflict_for_token(state, conflict_token)
    if conflict is None:
        return _area_resolution_result(action, "stale_conflict", resolved=False)
    if action == "keep_ha_room":
        return await _async_keep_ha_area(hass, entry_id, state, conflict)
    if not conflict.desired_action_valid:
        return _area_resolution_result(action, "invalid_desired_area", conflict, resolved=False)
    return await _async_apply_loxone_area(hass, entry_id, state, conflict)


def _device_config_entries(device: object) -> frozenset[str]:
    """Read persisted ownership across supported HA device-registry shapes."""
    config_entry_id = getattr(device, "config_entry_id", None)
    if isinstance(config_entry_id, str) and config_entry_id:
        return frozenset({config_entry_id})
    config_entries = getattr(device, "config_entries", ())
    return frozenset(item for item in config_entries if isinstance(item, str) and item)


def _all_devices(device_registry: object) -> tuple[object, ...]:
    """Return persisted registry entries without consulting loaded coordinators."""
    devices = getattr(device_registry, "devices", ())
    values = getattr(devices, "values", None)
    return tuple(values()) if callable(values) else tuple(devices)


def _registry_claimants(device_registry: object, unique_id: str) -> frozenset[str]:
    """Return persisted unscoped and provider-scoped registry claimants."""
    unscoped = (DOMAIN, unique_id)
    scoped_suffix = f":{unique_id}"
    claimants: set[str] = set()
    for device in _all_devices(device_registry):
        identifiers = getattr(device, "identifiers", ())
        if unscoped in identifiers or any(
            domain == DOMAIN and isinstance(identifier, str) and identifier.endswith(scoped_suffix)
            for domain, identifier in identifiers
        ):
            claimants.update(_device_config_entries(device))
    return frozenset(claimants)


def _snapshot_claims(
    snapshot: EngineeringSnapshot,
    unique_ids: frozenset[str],
) -> frozenset[str]:
    """Return candidate identities present in one validated private snapshot."""
    return frozenset(node.element.uuid for node in snapshot.nodes if node.element.uuid in unique_ids)


def _configured_entry_ids(hass: HomeAssistant) -> tuple[tuple[str, ...], bool]:
    """Return unique configured PyLoxone entry IDs and evidence validity."""
    manager = getattr(hass, "config_entries", None)
    async_entries = getattr(manager, "async_entries", None)
    if not callable(async_entries):
        return (), True
    try:
        entries = tuple(async_entries(DOMAIN))
    except _CONFIG_ENTRY_LOOKUP_ERRORS:
        return (), False
    entry_ids: list[str] = []
    for config_entry in entries:
        entry_id = getattr(config_entry, "entry_id", None)
        if not isinstance(entry_id, str) or not entry_id:
            return (), False
        if entry_id not in entry_ids:
            entry_ids.append(entry_id)
    return tuple(entry_ids), True


async def _all_snapshot_claimants(
    hass: HomeAssistant,
    entry_id: str,
    unique_ids: frozenset[str],
    *,
    current_snapshot: EngineeringSnapshot | None,
) -> tuple[Mapping[str, frozenset[str]], bool]:
    """Collect claimant evidence from every configured entry's persisted snapshot."""
    claimants: dict[str, set[str]] = {unique_id: set() for unique_id in unique_ids}
    if current_snapshot is not None:
        for unique_id in _snapshot_claims(current_snapshot, unique_ids):
            claimants[unique_id].add(entry_id)
    else:
        for unique_id in unique_ids:
            claimants[unique_id].add(entry_id)

    entry_ids, complete = _configured_entry_ids(hass)
    for candidate_entry_id in entry_ids:
        if candidate_entry_id == entry_id:
            continue
        try:
            state = await async_load_engineering_state(hass, candidate_entry_id)
        except Exception:  # noqa: BLE001 - unavailable evidence must reject migration
            complete = False
            continue
        if state.snapshot is None:
            complete = False
            continue
        for unique_id in _snapshot_claims(state.snapshot, unique_ids):
            claimants[unique_id].add(candidate_entry_id)
    return (
        MappingProxyType({key: frozenset(value) for key, value in claimants.items()}),
        complete,
    )


def _eligible_device_keys(snapshot: EngineeringSnapshot) -> frozenset[str]:
    """Select real registry nodes and useful provider services only."""
    service_keys = {
        row.node.owner_key
        for row in snapshot.rows
        if row.node.kind is NodeKind.CHANNEL
        and row.node.owner_key
        and row.capability.state is CapabilityState.READABLE
        and row.capability.exposure in {ExposureStatus.PREPARED_DISABLED, ExposureStatus.INVENTORY_ONLY}
        and row.semantic_platform is not None
        and row.binding is not None
        and not row.node.sensitive
    }
    return frozenset(
        node.element.key
        for node in snapshot.nodes
        if not node.sensitive
        and node.resolution_status is ResolutionStatus.RESOLVED
        and node.device_identifier
        and (
            node.kind
            in {
                NodeKind.MINISERVER,
                NodeKind.BUS,
                NodeKind.BRIDGE,
                NodeKind.PHYSICAL_DEVICE,
            }
            or (node.kind is NodeKind.SERVICE_MODULE and node.element.key in service_keys)
        )
    )


def registry_metadata_from_snapshot(
    snapshot: EngineeringSnapshot,
    *,
    managed_area_ids: Mapping[str, str] | None = None,
    applied_generation: str | None = None,
) -> EngineeringRegistryMetadata:
    """Derive sanitized active identifiers and rooms from a validated snapshot."""
    validate_engineering_snapshot(snapshot)
    eligible = _eligible_device_keys(snapshot)
    provider = snapshot.source.provider_identifier
    active = frozenset(
        node.device_identifier
        for node in snapshot.nodes
        if node.element.key in eligible and node.device_identifier is not None and node.device_identifier != provider
    )
    rooms = frozenset(node.element.room for node in snapshot.nodes if not node.sensitive and node.element.room)
    return EngineeringRegistryMetadata(
        active,
        rooms,
        {identifier: area_id for identifier, area_id in (managed_area_ids or {}).items() if identifier in active},
        applied_generation,
        provider,
    )


async def async_filter_entity_identity_conflicts(
    hass: HomeAssistant,
    entry_id: str,
    specs: tuple[EngineeringEntitySpec, ...],
) -> tuple[
    tuple[EngineeringEntitySpec, ...],
    tuple[EngineeringEntityIdentityRejection, ...],
]:
    """Reject globally owned identities without mutating their persisted entries."""
    entity_registry = er.async_get(hass)
    accepted: list[EngineeringEntitySpec] = []
    rejected: list[EngineeringEntityIdentityRejection] = []
    for spec in specs:
        entity_id = entity_registry.async_get_entity_id(
            spec.platform,
            DOMAIN,
            spec.unique_id,
        )
        entry = entity_registry.async_get(entity_id) if entity_id else None
        if entry is not None and entry.config_entry_id != entry_id:
            rejected.append(
                EngineeringEntityIdentityRejection(
                    spec,
                    entity_id,
                    "entity_unique_id_owned_by_other_entry",
                )
            )
            continue
        accepted.append(spec)
    return tuple(accepted), tuple(rejected)


def _area_operation_policy(
    baseline: _ManagedAreaBaseline | None,
    existing_conflict: EngineeringAreaConflict | None,
    current: object | None,
    *,
    released: bool,
    intent_replay: bool,
) -> tuple[bool, str | None]:
    """Return automatic authority and any bounded conflict reason."""
    current_area = getattr(current, "area_id", None) if current else None
    baseline_matches = (
        baseline is not None and baseline.process_token == _AREA_PROCESS_TOKEN and current_area == baseline.area_id
    )
    allowed = not released and existing_conflict is None and (current is None or baseline_matches or intent_replay)
    if released:
        return allowed, None
    if existing_conflict is not None:
        return allowed, existing_conflict.reason
    if baseline is not None and not baseline_matches and not intent_replay:
        reason = (
            "area_user_override_preserved"
            if baseline.process_token == _AREA_PROCESS_TOKEN
            else "area_assignment_unverified"
        )
        return allowed, reason
    return allowed, "area_assignment_unverified" if current is not None and not allowed else None


async def async_plan_engineering_registry_sync(
    hass: HomeAssistant,
    entry_id: str,
    snapshot: EngineeringSnapshot,
    previous: EngineeringRegistryMetadata,
) -> EngineeringRegistryPlan:
    """Build a desired registry topology without performing any mutations."""
    validate_engineering_snapshot(snapshot)
    if snapshot.source.entry_id != entry_id:
        raise EngineeringRegistryError(_SNAPSHOT_ENTRY_MISMATCH)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    entity_registry = er.async_get(hass)
    area_state = await _async_load_area_state(hass, entry_id)
    eligible = _eligible_device_keys(snapshot)
    device_operations: list[EngineeringDeviceOperation] = []

    for node in snapshot.nodes:
        if node.element.key not in eligible or node.device_identifier is None:
            continue
        identifier = node.device_identifier
        current = device_registry.async_get_device_by_identifier(
            (DOMAIN, identifier),
            entry_id,
        )
        previous_area = previous.managed_area_ids.get(identifier)
        current_area = getattr(current, "area_id", None) if current else None
        prior_intent = area_state.intents.get(identifier)
        baseline = area_state.baselines.get(identifier)
        existing_conflict = area_state.conflicts.get(identifier)
        desired_area = area_registry.async_get_area_by_name(node.element.room) if node.element.room else None
        intent_replay = (
            area_state.generation_id == snapshot.generation_id
            and prior_intent is not None
            and prior_intent.process_token == _AREA_PROCESS_TOKEN
            and prior_intent.to_room == node.element.room
            and (
                (current is None and prior_intent.from_area_id is None)
                or current_area
                in {
                    prior_intent.from_area_id,
                    desired_area.id if desired_area is not None else None,
                }
            )
        )
        baseline = baseline or (
            _ManagedAreaBaseline(identifier, previous_area, None) if previous_area is not None else None
        )
        baseline_area = baseline.area_id if baseline is not None else None
        released = identifier in area_state.released_identifiers
        allowed, conflict_reason = _area_operation_policy(
            baseline,
            existing_conflict,
            current,
            released=released,
            intent_replay=intent_replay,
        )
        if (
            conflict_reason is None
            and not released
            and current is not None
            and desired_area is not None
            and current_area != desired_area.id
        ):
            conflict_reason = "area_user_override_preserved"
        device_operations.append(
            EngineeringDeviceOperation(
                entry_id=entry_id,
                identifier=identifier,
                name=(node.element.title or node.element.io_name or node.element.loxone_type or identifier),
                model=node.element.loxone_type or "Engineering device",
                room=node.element.room,
                via_identifier=node.via_device_identifier,
                previous_managed_area_id=baseline_area,
                area_update_allowed=allowed,
                expected_area_id=current_area,
                area_intent_replay=intent_replay,
                area_conflict_reason=conflict_reason,
                area_released=released,
                area_process_token=_AREA_PROCESS_TOKEN,
            )
        )

    specs = build_engineering_entity_specs(snapshot.rows, None)
    accepted, rejected = await async_filter_entity_identity_conflicts(
        hass,
        entry_id,
        specs,
    )
    entity_operations: list[EngineeringEntityOperation] = []
    ambiguous: set[str] = set()
    devices_by_id = {getattr(device, "id", ""): device for device in _all_devices(device_registry)}
    unique_ids = frozenset(spec.unique_id for spec in accepted)
    snapshot_claimants, snapshot_evidence_complete = await _all_snapshot_claimants(
        hass,
        entry_id,
        unique_ids,
        current_snapshot=snapshot,
    )
    for spec in accepted:
        entity_id = entity_registry.async_get_entity_id(
            spec.platform,
            DOMAIN,
            spec.unique_id,
        )
        entity = entity_registry.async_get(entity_id) if entity_id else None
        if entity is None:
            continue
        current_device = devices_by_id.get(entity.device_id or "")
        is_legacy = current_device is not None and (
            DOMAIN,
            spec.unique_id,
        ) in getattr(current_device, "identifiers", ())
        if is_legacy:
            claimants = _registry_claimants(
                device_registry,
                spec.unique_id,
            ) | snapshot_claimants.get(spec.unique_id, frozenset())
            if not snapshot_evidence_complete or claimants != frozenset({entry_id}):
                ambiguous.add(spec.unique_id)
                continue
        entity_operations.append(
            EngineeringEntityOperation(
                entry_id=entry_id,
                entity_id=entity_id,
                unique_id=spec.unique_id,
                owner_identifier=spec.owner_identifier,
                original_name=spec.name,
                platform=spec.platform,
                spec=spec,
                legacy_reassociation=is_legacy,
            )
        )

    metadata = registry_metadata_from_snapshot(
        snapshot,
        managed_area_ids=previous.managed_area_ids,
        applied_generation=snapshot.generation_id,
    )
    return EngineeringRegistryPlan(
        snapshot.generation_id,
        tuple(device_operations),
        tuple(sorted(entity_operations, key=lambda item: item.entity_id)),
        metadata,
        rejected,
        tuple(sorted(ambiguous)),
    )


def _desired_area_id(area_registry: object, room: str | None) -> str | None:
    """Resolve an existing desired area without creating it."""
    area = area_registry.async_get_area_by_name(room) if room else None
    return area.id if area is not None else None


def _candidate_area_intents(
    plan: EngineeringRegistryPlan,
    device_registry: object,
    area_registry: object,
    *,
    compatibility_mode: bool,
    prior_state: _AreaRegistryState,
) -> dict[str, _AreaIntent]:
    """Select intentions still authorized by the current registry state."""
    intents: dict[str, _AreaIntent] = {}
    for operation in plan.device_operations:
        if (
            not operation.area_update_allowed
            or operation.area_process_token != _AREA_PROCESS_TOKEN
            or operation.area_released
            or operation.identifier in prior_state.released_identifiers
            or operation.identifier in prior_state.conflicts
            or operation.identifier not in plan.metadata.active_device_identifiers
        ):
            continue
        current = device_registry.async_get_device_by_identifier(
            (DOMAIN, operation.identifier),
            operation.entry_id,
        )
        current_area = getattr(current, "area_id", None) if current else None
        persisted = prior_state.intents.get(operation.identifier)
        persisted_matches = (
            prior_state.generation_id == plan.generation_id
            and persisted is not None
            and persisted.process_token == _AREA_PROCESS_TOKEN
            and persisted.to_room == operation.room
        )
        desired_area_id = _desired_area_id(area_registry, operation.room)
        persisted_replay_is_authorized = persisted_matches and (
            (current is None and persisted.from_area_id is None)
            or current_area in {persisted.from_area_id, desired_area_id}
        )
        if persisted_replay_is_authorized:
            intents[operation.identifier] = persisted
            continue
        compatibility_replay = (
            compatibility_mode
            and operation.expected_area_id is None
            and current is not None
            and current_area == desired_area_id
        )
        original_state_unchanged = (current is None and operation.expected_area_id is None) or (
            current_area == operation.expected_area_id
            and (baseline := prior_state.baselines.get(operation.identifier)) is not None
            and baseline.process_token == _AREA_PROCESS_TOKEN
            and baseline.area_id == current_area
        )
        if compatibility_replay or original_state_unchanged:
            intents[operation.identifier] = _AreaIntent(
                operation.identifier,
                operation.expected_area_id,
                operation.room,
                _AREA_PROCESS_TOKEN,
            )
    return intents


def _recheck_area_intents(
    plan: EngineeringRegistryPlan,
    device_registry: object,
    area_registry: object,
    intents: Mapping[str, _AreaIntent],
) -> dict[str, _AreaIntent]:
    """Drop user changes made while the intent Store write yielded."""
    rechecked: dict[str, _AreaIntent] = {}
    operations = {item.identifier: item for item in plan.device_operations}
    for identifier, intent in intents.items():
        operation = operations[identifier]
        current = device_registry.async_get_device_by_identifier(
            (DOMAIN, identifier),
            operation.entry_id,
        )
        current_area = getattr(current, "area_id", None) if current else None
        desired_area_id = _desired_area_id(area_registry, intent.to_room)
        if (
            (current is None and intent.from_area_id is None)
            or current_area == intent.from_area_id
            or (intent.process_token == _AREA_PROCESS_TOKEN and current_area == desired_area_id)
        ):
            rechecked[identifier] = intent
    return rechecked


async def _async_prepare_area_intents(
    hass: HomeAssistant,
    plan: EngineeringRegistryPlan,
    committed_state: StoredEngineeringState | None,
    device_registry: object,
    area_registry: object,
) -> tuple[dict[str, _AreaIntent], _AreaRegistryState]:
    """Persist and recheck area intent before registry mutation."""
    area_state = await _async_load_area_state(hass, _plan_entry_id(plan))
    intents = _candidate_area_intents(
        plan,
        device_registry,
        area_registry,
        compatibility_mode=committed_state is None,
        prior_state=area_state,
    )
    if committed_state is None:
        return intents, area_state
    entry_id = committed_state.snapshot.source.entry_id
    while True:
        candidate_state = replace(
            area_state,
            generation_id=plan.generation_id,
            intents=MappingProxyType(dict(intents)),
        )
        await _async_store_area_state(
            hass,
            entry_id,
            candidate_state,
        )
        rechecked = _recheck_area_intents(
            plan,
            device_registry,
            area_registry,
            intents,
        )
        if rechecked == intents:
            return rechecked, candidate_state
        intents = rechecked


def _apply_device_properties(
    plan: EngineeringRegistryPlan,
    device_registry: object,
    area_registry: object,
    area_intents: Mapping[str, _AreaIntent],
) -> tuple[set[str], set[str], dict[str, str]]:
    """Create devices and apply non-topology properties in pass one."""
    created: set[str] = set()
    updated: set[str] = set()
    managed_areas: dict[str, str] = {}
    for operation in plan.device_operations:
        identifier = (DOMAIN, operation.identifier)
        before = device_registry.async_get_device_by_identifier(
            identifier,
            operation.entry_id,
        )
        device = device_registry.async_get_or_create(
            config_entry_id=operation.entry_id,
            identifiers={identifier},
            name=operation.name,
            manufacturer="Loxone",
            model=operation.model,
            suggested_area=operation.room,
        )
        if before is None:
            created.add(operation.identifier)
        desired_fields = {
            "name": operation.name,
            "manufacturer": "Loxone",
            "model": operation.model,
        }
        changes: dict[str, object] = {
            field_name: value
            for field_name, value in desired_fields.items()
            if getattr(device, field_name, None) != value
        }
        desired_area = area_registry.async_get_area_by_name(operation.room) if operation.room else None
        if operation.room and desired_area is None:
            desired_area = area_registry.async_get_or_create(operation.room)
        desired_area_id = desired_area.id if desired_area is not None else None
        if operation.identifier in area_intents:
            if getattr(device, "area_id", None) != desired_area_id:
                changes["area_id"] = desired_area_id
            if desired_area_id is not None and operation.identifier in plan.metadata.active_device_identifiers:
                managed_areas[operation.identifier] = desired_area_id
        if changes:
            device_registry.async_update_device(device.id, **changes)
            updated.add(operation.identifier)
    return created, updated, managed_areas


def _area_name_by_id(area_registry: object, area_id: str | None) -> str | None:
    """Resolve an area display name without relying on private registry storage."""
    if area_id is None:
        return None
    async_get_area = getattr(area_registry, "async_get_area", None)
    area = async_get_area(area_id) if callable(async_get_area) else None
    if area is None:
        areas = getattr(area_registry, "areas", {})
        area = areas.get(area_id) if hasattr(areas, "get") else None
    return _optional_bounded_string(getattr(area, "name", None))


def _area_conflict(
    operation: EngineeringDeviceOperation,
    area_registry: object,
    context: _AreaConflictContext,
) -> EngineeringAreaConflict:
    """Create a deterministic sanitized conflict record."""
    current_name = _area_name_by_id(area_registry, context.current_area_id)
    desired_name = operation.room
    token_source = "\x1f".join(
        (
            operation.entry_id,
            operation.identifier,
            context.current_area_id or "",
            context.desired_area_id or "",
            operation.room or "",
            context.generation_id,
            context.reason,
        )
    )
    return EngineeringAreaConflict(
        token=sha256(token_source.encode()).hexdigest(),
        entry_id=operation.entry_id,
        device_identifier=operation.identifier,
        display_name=_optional_bounded_string(operation.name),
        current_area_id=context.current_area_id,
        current_area_name=current_name,
        desired_area_id=context.desired_area_id,
        desired_area_name=desired_name,
        generation_id=context.generation_id,
        reason=context.reason,
    )


def _refresh_area_conflict(
    conflict: EngineeringAreaConflict,
    area_registry: object,
    current_area_id: str | None,
    reason: str,
) -> EngineeringAreaConflict:
    """Retoken a conflict after a concurrent HA area change."""
    token_source = "\x1f".join(
        (
            conflict.entry_id,
            conflict.device_identifier,
            current_area_id or "",
            conflict.desired_area_id or "",
            conflict.desired_area_name or "",
            conflict.generation_id,
            reason,
        )
    )
    return replace(
        conflict,
        token=sha256(token_source.encode()).hexdigest(),
        current_area_id=current_area_id,
        current_area_name=_area_name_by_id(area_registry, current_area_id),
        reason=reason,
    )


def _reconcile_area_state_after_apply(
    plan: EngineeringRegistryPlan,
    state: _AreaRegistryState,
    intents: Mapping[str, _AreaIntent],
    device_registry: object,
    area_registry: object,
) -> tuple[_AreaRegistryState, dict[str, str]]:
    """Drop authority on unexplained state and build sanitized conflicts."""
    active = plan.metadata.active_device_identifiers
    baselines = {identifier: value for identifier, value in state.baselines.items() if identifier in active}
    conflicts = {identifier: value for identifier, value in state.conflicts.items() if identifier in active}
    remaining_intents = {identifier: value for identifier, value in state.intents.items() if identifier in active}
    released = set(state.released_identifiers)
    managed: dict[str, str] = {}
    for operation in plan.device_operations:
        identifier = operation.identifier
        if identifier not in plan.metadata.active_device_identifiers:
            baselines.pop(identifier, None)
            conflicts.pop(identifier, None)
            remaining_intents.pop(identifier, None)
            continue
        if identifier in released:
            baselines.pop(identifier, None)
            remaining_intents.pop(identifier, None)
            continue
        device = device_registry.async_get_device_by_identifier((DOMAIN, identifier), operation.entry_id)
        current_area = getattr(device, "area_id", None) if device else None
        desired_area = _desired_area_id(area_registry, operation.room)
        intent = intents.get(identifier)
        if intent is not None and current_area == desired_area:
            baselines[identifier] = _ManagedAreaBaseline(identifier, desired_area, _AREA_PROCESS_TOKEN)
            conflicts.pop(identifier, None)
            remaining_intents.pop(identifier, None)
            if desired_area is not None:
                managed[identifier] = desired_area
            continue
        baseline = baselines.get(identifier)
        if (
            intent is None
            and baseline is not None
            and baseline.process_token == _AREA_PROCESS_TOKEN
            and current_area == baseline.area_id
            and operation.area_conflict_reason is None
        ):
            if current_area is not None:
                managed[identifier] = current_area
            continue
        mismatch_after_plan = current_area != operation.expected_area_id
        reason = operation.area_conflict_reason
        if reason is None and baseline is None and intent is None:
            reason = "area_assignment_unverified"
        if baseline is not None and baseline.process_token != _AREA_PROCESS_TOKEN:
            reason = "area_assignment_unverified"
        if (intent is not None and current_area != desired_area) or mismatch_after_plan:
            reason = "area_user_override_preserved"
        elif baseline is not None and current_area != baseline.area_id:
            reason = (
                "area_user_override_preserved"
                if baseline.process_token == _AREA_PROCESS_TOKEN
                else "area_assignment_unverified"
            )
        if reason is not None:
            conflicts[identifier] = _area_conflict(
                operation,
                area_registry,
                _AreaConflictContext(
                    current_area,
                    desired_area,
                    plan.generation_id,
                    reason,
                ),
            )
            baselines.pop(identifier, None)
            remaining_intents.pop(identifier, None)
            continue
        baselines.pop(identifier, None)
    return (
        _AreaRegistryState(
            plan.generation_id,
            MappingProxyType(remaining_intents),
            MappingProxyType(baselines),
            MappingProxyType(conflicts),
            frozenset(released),
        ),
        managed,
    )


async def _async_finalize_area_state(
    hass: HomeAssistant,
    plan: EngineeringRegistryPlan,
    state: _AreaRegistryState,
    intents: Mapping[str, _AreaIntent],
) -> tuple[_AreaRegistryState, dict[str, str]]:
    """Acknowledge applied baselines, then fail closed across the Store await."""
    entry_id = _plan_entry_id(plan)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    candidate, managed = _reconcile_area_state_after_apply(plan, state, intents, device_registry, area_registry)
    await _async_store_area_state(hass, entry_id, candidate)
    rechecked, rechecked_managed = _reconcile_area_state_after_apply(
        plan, candidate, {}, device_registry, area_registry
    )
    if rechecked != candidate:
        await _async_store_area_state(hass, entry_id, rechecked)
        return rechecked, rechecked_managed
    return candidate, managed


def _apply_device_via_links(
    plan: EngineeringRegistryPlan,
    device_registry: object,
) -> set[str]:
    """Apply concrete nearest-owner registry IDs in pass two."""
    updated: set[str] = set()
    for operation in plan.device_operations:
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, operation.identifier),
            operation.entry_id,
        )
        if device is None:
            raise EngineeringRegistryError(_DEVICE_MISSING)
        desired_via_id = None
        if operation.via_identifier is not None:
            via = device_registry.async_get_device_by_identifier(
                (DOMAIN, operation.via_identifier),
                operation.entry_id,
            )
            if via is None:
                raise EngineeringRegistryError(_VIA_DEVICE_MISSING)
            desired_via_id = via.id
        if getattr(device, "via_device_id", None) != desired_via_id:
            device_registry.async_update_device(
                device.id,
                via_device_id=desired_via_id,
            )
            updated.add(operation.identifier)
    return updated


def _plan_entry_id(plan: EngineeringRegistryPlan) -> str:
    """Return the source entry carried by stable operations."""
    if plan.device_operations:
        return plan.device_operations[0].entry_id
    if plan.entity_operations:
        return plan.entity_operations[0].entry_id
    return ""


async def _async_apply_entity_operations(
    hass: HomeAssistant,
    plan: EngineeringRegistryPlan,
    committed_state: StoredEngineeringState | None,
    device_registry: object,
    entity_registry: object,
) -> tuple[
    int,
    tuple[EngineeringEntityIdentityRejection, ...],
    tuple[str, ...],
]:
    """Recheck global identity and claimant evidence immediately before mutation."""
    rejected = list(plan.rejected_entities)
    ambiguous = set(plan.ambiguous_legacy_identifiers)
    legacy_unique_ids = frozenset(
        operation.unique_id for operation in plan.entity_operations if operation.legacy_reassociation
    )
    snapshot_claimants, evidence_complete = await _all_snapshot_claimants(
        hass,
        _plan_entry_id(plan),
        legacy_unique_ids,
        current_snapshot=(committed_state.snapshot if committed_state is not None else None),
    )
    devices_by_id = {getattr(device, "id", ""): device for device in _all_devices(device_registry)}
    migrated = 0
    for operation in plan.entity_operations:
        entity_id = entity_registry.async_get_entity_id(
            operation.platform,
            DOMAIN,
            operation.unique_id,
        )
        entry = entity_registry.async_get(entity_id) if entity_id else None
        if (
            entity_id != operation.entity_id
            or entry is None
            or entry.config_entry_id != operation.entry_id
            or entry.unique_id != operation.unique_id
            or entry.platform != DOMAIN
        ):
            rejected.append(
                EngineeringEntityIdentityRejection(
                    operation.spec,
                    entity_id or operation.entity_id,
                    "entity_identity_changed_during_apply",
                )
            )
            continue
        owner = device_registry.async_get_device_by_identifier(
            (DOMAIN, operation.owner_identifier),
            operation.entry_id,
        )
        if owner is None:
            raise EngineeringRegistryError(_ENTITY_OWNER_MISSING)
        current_device = devices_by_id.get(entry.device_id or "")
        if operation.legacy_reassociation and entry.device_id != owner.id:
            is_still_legacy = current_device is not None and (
                DOMAIN,
                operation.unique_id,
            ) in getattr(current_device, "identifiers", ())
            claimants = _registry_claimants(
                device_registry,
                operation.unique_id,
            ) | snapshot_claimants.get(operation.unique_id, frozenset())
            if not is_still_legacy or not evidence_complete or claimants != frozenset({operation.entry_id}):
                ambiguous.add(operation.unique_id)
                rejected.append(
                    EngineeringEntityIdentityRejection(
                        operation.spec,
                        operation.entity_id,
                        "legacy_claimants_changed_during_apply",
                    )
                )
                continue
        changes = {}
        if entry.device_id != owner.id:
            changes["device_id"] = owner.id
        if entry.original_name != operation.original_name:
            changes["original_name"] = operation.original_name
        if changes:
            entity_registry.async_update_entity(
                operation.entity_id,
                config_entry_id=operation.entry_id,
                **changes,
            )
            migrated += 1
    return migrated, tuple(rejected), tuple(sorted(ambiguous))


async def async_apply_engineering_registry_plan(
    hass: HomeAssistant,
    plan: EngineeringRegistryPlan,
    *,
    committed_state: StoredEngineeringState | None = None,
) -> EngineeringRegistrySyncResult:
    """Apply both registry passes idempotently and then persist its recovery cursor."""
    async with _area_operation_lock(hass, _plan_entry_id(plan)):
        return await _async_apply_engineering_registry_plan(hass, plan, committed_state=committed_state)


async def _async_apply_engineering_registry_plan(
    hass: HomeAssistant,
    plan: EngineeringRegistryPlan,
    *,
    committed_state: StoredEngineeringState | None = None,
) -> EngineeringRegistrySyncResult:
    """Apply with exclusive ownership of entry area-state transitions."""
    if committed_state is not None and (
        committed_state.snapshot is None or committed_state.snapshot.generation_id != plan.generation_id
    ):
        raise EngineeringRegistryError(_STATE_PLAN_MISMATCH)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    entity_registry = er.async_get(hass)
    area_intents, area_state = await _async_prepare_area_intents(
        hass,
        plan,
        committed_state,
        device_registry,
        area_registry,
    )
    created, updated, initial_managed_areas = _apply_device_properties(
        plan,
        device_registry,
        area_registry,
        area_intents,
    )
    updated.update(_apply_device_via_links(plan, device_registry))
    migrated, rejected_entities, ambiguous_identifiers = await _async_apply_entity_operations(
        hass,
        plan,
        committed_state,
        device_registry,
        entity_registry,
    )

    managed_areas = initial_managed_areas
    entry_id = _plan_entry_id(plan)
    if committed_state is not None:
        area_state, managed_areas = await _async_finalize_area_state(
            hass,
            plan,
            area_state,
            area_intents,
        )

    metadata = replace(
        plan.metadata,
        managed_area_ids=managed_areas,
        applied_generation=plan.generation_id,
    )
    result = EngineeringRegistrySyncResult(
        tuple(operation.identifier for operation in plan.device_operations if operation.identifier in created),
        tuple(operation.identifier for operation in plan.device_operations if operation.identifier in updated),
        migrated,
        metadata,
        rejected_entities,
        ambiguous_identifiers,
    )
    if committed_state is not None:
        await async_store_engineering_state(
            hass,
            replace(
                committed_state,
                registry_applied_generation=plan.generation_id,
                managed_area_ids=managed_areas,
            ),
        )
        rechecked_state, rechecked_managed = _reconcile_area_state_after_apply(
            plan,
            area_state,
            {},
            device_registry,
            area_registry,
        )
        if rechecked_state != area_state:
            await _async_store_area_state(hass, entry_id, rechecked_state)
            managed_areas = rechecked_managed
            metadata = replace(metadata, managed_area_ids=managed_areas)
            result = replace(result, metadata=metadata)
            await async_store_engineering_state(
                hass,
                replace(
                    committed_state,
                    registry_applied_generation=plan.generation_id,
                    managed_area_ids=managed_areas,
                ),
            )
    return result


async def async_sync_engineering_devices(
    hass: HomeAssistant,
    entry_id: str,
    snapshot: EngineeringSnapshot,
    previous: EngineeringRegistryMetadata,
) -> EngineeringRegistrySyncResult:
    """Compatibility wrapper that plans and applies one complete snapshot."""
    plan = await async_plan_engineering_registry_sync(
        hass,
        entry_id,
        snapshot,
        previous,
    )
    return await async_apply_engineering_registry_plan(hass, plan)
