"""Plan and apply source-scoped Home Assistant engineering registry topology."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .engineering_entities import (
    EngineeringEntitySpec,
    build_engineering_entity_specs,
)
from .engineering_snapshot import (
    EngineeringSnapshot,
    StoredEngineeringState,
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


@dataclass(frozen=True, slots=True)
class EngineeringRegistryMetadata:
    """Safe integration-owned registry reconciliation metadata."""

    active_device_identifiers: frozenset[str]
    room_names: frozenset[str]
    managed_area_ids: Mapping[str, str]
    applied_generation: str | None = None

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


@dataclass(frozen=True, slots=True)
class EngineeringEntityOperation:
    """One existing entity's desired engineering owner association."""

    entry_id: str
    entity_id: str
    unique_id: str
    owner_identifier: str
    original_name: str


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


def _legacy_claimants(device_registry: object, unique_id: str) -> frozenset[str]:
    """Return every persisted config-entry claimant of an unscoped identifier."""
    identifier = (DOMAIN, unique_id)
    claimants: set[str] = set()
    for device in _all_devices(device_registry):
        if identifier in getattr(device, "identifiers", ()):
            claimants.update(_device_config_entries(device))
    return frozenset(claimants)


def _eligible_device_keys(snapshot: EngineeringSnapshot) -> frozenset[str]:
    """Select real registry nodes and useful provider services only."""
    service_keys = {
        row.node.owner_key
        for row in snapshot.rows
        if row.node.kind is NodeKind.CHANNEL
        and row.node.owner_key
        and row.semantic_platform is not None
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
    entity_registry = er.async_get(hass)
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
                ),
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
        if is_legacy and _legacy_claimants(device_registry, spec.unique_id) != frozenset({entry_id}):
            ambiguous.add(spec.unique_id)
            continue
        entity_operations.append(
            EngineeringEntityOperation(
                entry_id,
                entity_id,
                spec.unique_id,
                spec.owner_identifier,
                spec.name,
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


async def async_apply_engineering_registry_plan(  # noqa: PLR0912, PLR0915
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

        current_area = getattr(device, "area_id", None)
        desired_area = None
        if operation.room:
            desired_area = area_registry.async_get_area_by_name(operation.room)
            if desired_area is None:
                desired_area = area_registry.async_get_or_create(operation.room)
        desired_area_id = desired_area.id if desired_area is not None else None
        if operation.area_update_allowed and current_area in {
            None,
            operation.previous_managed_area_id,
            desired_area_id,
        }:
            if current_area != desired_area_id:
                changes["area_id"] = desired_area_id
            if desired_area_id is not None and operation.identifier in plan.metadata.active_device_identifiers:
                managed_areas[operation.identifier] = desired_area_id
        if changes:
            device_registry.async_update_device(device.id, **changes)
            updated.add(operation.identifier)

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

    migrated = 0
    for operation in plan.entity_operations:
        entry = entity_registry.async_get(operation.entity_id)
        if entry is None or entry.config_entry_id != operation.entry_id:
            continue
        owner = device_registry.async_get_device_by_identifier(
            (DOMAIN, operation.owner_identifier),
            operation.entry_id,
        )
        if owner is None:
            raise EngineeringRegistryError(_ENTITY_OWNER_MISSING)
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
        plan.rejected_entities,
        plan.ambiguous_legacy_identifiers,
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
