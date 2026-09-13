"""Plan and apply source-scoped Home Assistant engineering registry topology."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    EngineeringStateStore,
    StoredEngineeringState,
    async_load_engineering_state,
    async_store_engineering_state,
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

# Kept as a named seam so cold-restart tests exercise acknowledged Store behavior.
EngineeringRegistryIntentStore = EngineeringStateStore


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
class _AreaIntent:
    """One acknowledged pre-mutation area assignment intent."""

    identifier: str
    from_area_id: str | None
    to_room: str | None


async def _async_load_area_intents(
    hass: HomeAssistant,
    entry_id: str,
) -> tuple[str | None, Mapping[str, _AreaIntent]]:
    """Load a bounded registry intent journal without mutating registries."""
    store = EngineeringRegistryIntentStore(
        hass,
        _INTENT_STORAGE_VERSION,
        f"{_INTENT_STORAGE_KEY}.{entry_id}",
        private=True,
    )
    raw = await store.async_load()
    if not isinstance(raw, dict) or not isinstance(raw.get("generation_id"), str):
        return None, {}
    intents: dict[str, _AreaIntent] = {}
    for item in raw.get("area_intents", ()):
        if not isinstance(item, dict):
            continue
        identifier = item.get("identifier")
        from_area_id = item.get("from_area_id")
        to_room = item.get("to_room")
        if (
            not isinstance(identifier, str)
            or not identifier
            or (from_area_id is not None and not isinstance(from_area_id, str))
            or (to_room is not None and not isinstance(to_room, str))
        ):
            continue
        intents[identifier] = _AreaIntent(identifier, from_area_id, to_room)
    return raw["generation_id"], MappingProxyType(intents)


async def _async_store_area_intents(
    hass: HomeAssistant,
    entry_id: str,
    generation_id: str,
    intents: Mapping[str, _AreaIntent],
) -> None:
    """Acknowledge area intent before any corresponding registry mutation."""
    store = EngineeringRegistryIntentStore(
        hass,
        _INTENT_STORAGE_VERSION,
        f"{_INTENT_STORAGE_KEY}.{entry_id}",
        private=True,
    )
    payload = (
        {
            "generation_id": generation_id,
            "area_intents": [
                {
                    "identifier": item.identifier,
                    "from_area_id": item.from_area_id,
                    "to_room": item.to_room,
                }
                for item in sorted(intents.values(), key=lambda value: value.identifier)
            ],
        }
        if intents
        else {}
    )
    await store.async_save_acknowledged(payload)


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
    intent_generation, prior_area_intents = await _async_load_area_intents(
        hass,
        entry_id,
    )
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
        prior_intent = prior_area_intents.get(identifier)
        desired_area = area_registry.async_get_area_by_name(node.element.room) if node.element.room else None
        intent_replay = (
            intent_generation == snapshot.generation_id
            and prior_intent is not None
            and prior_intent.to_room == node.element.room
            and current is not None
            and current_area == (desired_area.id if desired_area is not None else None)
        )
        device_operations.append(
            EngineeringDeviceOperation(
                entry_id=entry_id,
                identifier=identifier,
                name=(node.element.title or node.element.io_name or node.element.loxone_type or identifier),
                model=node.element.loxone_type or "Engineering device",
                room=node.element.room,
                via_identifier=node.via_device_identifier,
                previous_managed_area_id=previous_area,
                area_update_allowed=(
                    current is None
                    or current_area is None
                    or (previous_area is not None and current_area == previous_area)
                    or intent_replay
                ),
                expected_area_id=current_area,
                area_intent_replay=intent_replay,
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
    prior_intent_state: tuple[str | None, Mapping[str, _AreaIntent]],
) -> dict[str, _AreaIntent]:
    """Select intentions still authorized by the current registry state."""
    intent_generation, persisted_intents = prior_intent_state
    intents: dict[str, _AreaIntent] = {}
    for operation in plan.device_operations:
        if not operation.area_update_allowed:
            continue
        current = device_registry.async_get_device_by_identifier(
            (DOMAIN, operation.identifier),
            operation.entry_id,
        )
        current_area = getattr(current, "area_id", None) if current else None
        persisted = persisted_intents.get(operation.identifier)
        replay_is_authorized = (
            operation.area_intent_replay
            and intent_generation == plan.generation_id
            and persisted is not None
            and persisted.to_room == operation.room
        )
        compatibility_replay = (
            compatibility_mode
            and operation.expected_area_id is None
            and current is not None
            and current_area == _desired_area_id(area_registry, operation.room)
        )
        original_state_unchanged = (
            current is None and operation.expected_area_id is None
        ) or current_area == operation.expected_area_id
        if compatibility_replay or (
            original_state_unchanged and (not operation.area_intent_replay or replay_is_authorized)
        ):
            intents[operation.identifier] = _AreaIntent(
                operation.identifier,
                operation.expected_area_id,
                operation.room,
            )
    return intents


def _recheck_area_intents(
    plan: EngineeringRegistryPlan,
    device_registry: object,
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
        if (current is None and intent.from_area_id is None) or (current_area == intent.from_area_id):
            rechecked[identifier] = intent
    return rechecked


async def _async_prepare_area_intents(
    hass: HomeAssistant,
    plan: EngineeringRegistryPlan,
    committed_state: StoredEngineeringState | None,
    device_registry: object,
    area_registry: object,
) -> dict[str, _AreaIntent]:
    """Persist and recheck area intent before registry mutation."""
    intent_generation: str | None = None
    persisted_intents: Mapping[str, _AreaIntent] = {}
    if committed_state is not None:
        intent_generation, persisted_intents = await _async_load_area_intents(
            hass,
            committed_state.snapshot.source.entry_id,
        )
    intents = _candidate_area_intents(
        plan,
        device_registry,
        area_registry,
        compatibility_mode=committed_state is None,
        prior_intent_state=(intent_generation, persisted_intents),
    )
    if committed_state is None:
        return intents
    entry_id = committed_state.snapshot.source.entry_id
    await _async_store_area_intents(
        hass,
        entry_id,
        plan.generation_id,
        intents,
    )
    rechecked = _recheck_area_intents(plan, device_registry, intents)
    if rechecked != intents:
        await _async_store_area_intents(
            hass,
            entry_id,
            plan.generation_id,
            rechecked,
        )
    return rechecked


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
    if committed_state is not None and (
        committed_state.snapshot is None or committed_state.snapshot.generation_id != plan.generation_id
    ):
        raise EngineeringRegistryError(_STATE_PLAN_MISMATCH)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    entity_registry = er.async_get(hass)
    area_intents = await _async_prepare_area_intents(
        hass,
        plan,
        committed_state,
        device_registry,
        area_registry,
    )
    created, updated, managed_areas = _apply_device_properties(
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
        await _async_store_area_intents(
            hass,
            committed_state.snapshot.source.entry_id,
            plan.generation_id,
            {},
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
