"""Prepared Home Assistant entities derived from verified engineering channels."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, ENGINEERING_STATE_SIGNAL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .engineering_capabilities import EngineeringInventoryRow
    from .engineering_config import EngineeringElement, EngineeringInventory
    from .engineering_registry import EngineeringRegistryMetadata
    from .engineering_runtime import (
        EngineeringRuntimeBinding,
        EngineeringRuntimeInventory,
    )


@dataclass(frozen=True, slots=True)
class EngineeringEntitySpec:
    """Platform-neutral entity definition from a proven safe event binding."""

    unique_id: str
    state_uuid: str
    platform: Literal["sensor", "binary_sensor"]
    name: str
    native_value: float | bool | None
    unit: str | None
    available: bool
    owner_identifier: str
    owner_name: str
    owner_model: str
    room: str | None
    loxone_type: str | None
    io_name: str | None
    config_version: int
    runtime_binding: str | None
    enabled_by_default: bool = False


class PreparedEngineeringEntity(Protocol):
    """Minimal platform entity surface used by the shared reconciler."""

    def update_spec(self, spec: EngineeringEntitySpec) -> None:
        """Apply a current prepared specification."""

    def mark_unavailable(self) -> None:
        """Retain the entity while marking its live binding unavailable."""


ENGINEERING_REGISTRY_STORAGE_VERSION = 1
ENGINEERING_REGISTRY_STORAGE_KEY = "loxone.engineering_registry"


@dataclass(frozen=True, slots=True)
class EngineeringSensorSpec:
    """A verified numeric engineering channel safe to prepare as a sensor."""

    element: EngineeringElement
    binding: EngineeringRuntimeBinding
    device: EngineeringElement | None
    config_version: int


def build_engineering_entity_specs(
    rows: tuple[EngineeringInventoryRow, ...],
    runtime: EngineeringRuntimeInventory | None,
) -> tuple[EngineeringEntitySpec, ...]:
    """Prepare only explicit event mappings; scalar reads remain inventory-only."""
    specs: list[EngineeringEntitySpec] = []
    for row in rows:
        node, binding = row.node, row.binding
        if (
            row.capability.exposure.value != "prepared_disabled"
            or row.semantic_platform is None
            or not node.element.uuid
            or binding is None
            or not binding.event_binding_proven
            or not binding.state_uuid
            or not node.device_identifier
        ):
            continue
        live = (
            None
            if runtime is None
            else next(
                (item for item in runtime.bindings if item.engineering_uuid == node.element.uuid),
                None,
            )
        )
        live_unit = (
            None
            if live is None
            else normalize_engineering_unit(live.unit, title=live.title, loxone_type=live.loxone_type)
        )
        live_is_compatible = (
            live is not None
            and live.status == "bound"
            and live.binding_method == binding.binding_method
            and live.state_uuid == binding.state_uuid
            and live.value_kind == binding.value_kind
            and live.numeric_value is not None
            and math.isfinite(live.numeric_value)
            and (live.unit is None or live_unit is not None)
            and (live_unit == binding.safe_unit)
        )
        if row.semantic_platform == "binary_sensor" and live_is_compatible:
            live_is_compatible = live.numeric_value in {0.0, 1.0}
        numeric = live.numeric_value if live_is_compatible else None
        native_value: float | bool | None = numeric
        if row.semantic_platform == "binary_sensor" and numeric is not None:
            native_value = bool(numeric)
        specs.append(
            EngineeringEntitySpec(
                unique_id=node.element.uuid,
                state_uuid=binding.state_uuid,
                platform=row.semantic_platform,
                name=node.element.title or node.element.io_name or node.element.uuid,
                native_value=native_value,
                unit=binding.safe_unit,
                available=live_is_compatible,
                owner_identifier=node.device_identifier,
                owner_name=row.owner_name or node.device_identifier,
                owner_model=row.owner_model or "Engineering device",
                room=node.element.room,
                loxone_type=node.element.loxone_type,
                io_name=node.element.io_name,
                config_version=row.config_version,
                runtime_binding=binding.binding_method if live_is_compatible else None,
            )
        )
    return tuple(specs)


def engineering_inventory_updated_signal(entry_id: str) -> str:
    """Return the config-entry-scoped engineering refresh signal."""
    return f"loxone_engineering_inventory_updated_{entry_id}"


def engineering_state_updated_signal(entry_id: str, state_uuid: str) -> str:
    """Return the private signal for one config-entry and proven state UUID."""
    return f"{ENGINEERING_STATE_SIGNAL}:{entry_id}:{state_uuid}"


@callback
def async_dispatch_engineering_state_updates(
    hass: HomeAssistant,
    entry_id: str,
    message: object,
) -> None:
    """Project websocket values onto private source-scoped state signals."""
    if not isinstance(message, Mapping):
        return
    for state_uuid, value in message.items():
        if isinstance(state_uuid, str) and state_uuid:
            async_dispatcher_send(
                hass,
                engineering_state_updated_signal(entry_id, state_uuid),
                value,
            )


def filter_existing_loxapp_entities(
    specs: tuple[EngineeringEntitySpec, ...],
    existing_uuids: set[str] | frozenset[str],
) -> tuple[EngineeringEntitySpec, ...]:
    """Keep normal LoxAPP entities authoritative for matching UUIDs."""
    return tuple(spec for spec in specs if spec.unique_id not in existing_uuids)


def engineering_event_value(
    platform: Literal["sensor", "binary_sensor"],
    value: object,
) -> float | bool | None:
    """Return a platform-safe live value, or None for an invalid event."""
    if platform == "binary_sensor":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = _finite_engineering_number(value)
            if numeric in {0.0, 1.0}:
                return bool(numeric)
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _finite_engineering_number(value)
    return None


def _finite_engineering_number(value: float) -> float | None:
    """Convert numeric input at the single authoritative finite boundary."""
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


@dataclass(slots=True)
class EngineeringPlatformReconciler:
    """Source-scoped owner of one platform's prepared entity set."""

    entry_id: str
    platform: Literal["sensor", "binary_sensor"]
    existing_uuids: frozenset[str]
    entity_factory: Callable[[EngineeringEntitySpec], PreparedEngineeringEntity]
    async_add_entities: Callable[[list[Any]], None]
    prepared_entities: MutableMapping[str, PreparedEngineeringEntity] = field(default_factory=dict)

    async def async_reconcile(
        self,
        hass: HomeAssistant,
        specs: tuple[EngineeringEntitySpec, ...],
    ) -> None:
        """Update prepared entities without stealing global identities."""
        from .engineering_registry import async_filter_entity_identity_conflicts  # noqa: PLC0415

        platform_specs = tuple(spec for spec in specs if spec.platform == self.platform)
        platform_specs = filter_existing_loxapp_entities(platform_specs, self.existing_uuids)
        accepted, _rejected = await async_filter_entity_identity_conflicts(
            hass,
            self.entry_id,
            platform_specs,
        )
        current_specs = {spec.unique_id: spec for spec in accepted}
        for unique_id, entity in self.prepared_entities.items():
            if spec := current_specs.get(unique_id):
                entity.update_spec(spec)
            else:
                entity.mark_unavailable()

        new_specs = tuple(spec for spec in accepted if spec.unique_id not in self.prepared_entities)
        if not new_specs:
            return
        last_checked, _rejected = await async_filter_entity_identity_conflicts(
            hass,
            self.entry_id,
            new_specs,
        )
        new_entities: list[Any] = []
        for spec in last_checked:
            entity = self.entity_factory(spec)
            self.prepared_entities[spec.unique_id] = entity
            new_entities.append(entity)
        if new_entities:
            self.async_add_entities(new_entities)


def _is_physical_device(element: EngineeringElement) -> bool:
    """Conservatively identify hardware containers in the engineering tree."""
    element_type = (element.loxone_type or "").casefold()
    return (
        "device" in element_type
        or "extension" in element_type
        or element_type.endswith("dev")
        or element_type in {"loxair", "loxtree", "modbusserver"}
    )


def _nearest_device(
    element: EngineeringElement,
    elements_by_uuid: dict[str, EngineeringElement],
) -> EngineeringElement | None:
    """Find the nearest physical ancestor without trusting cyclic input."""
    parent_uuid = element.parent_uuid
    seen: set[str] = set()
    while parent_uuid and parent_uuid not in seen:
        seen.add(parent_uuid)
        parent = elements_by_uuid.get(parent_uuid)
        if parent is None:
            return None
        if _is_physical_device(parent):
            return parent
        parent_uuid = parent.parent_uuid
    return None


def build_engineering_sensor_specs(
    inventory: EngineeringInventory,
    runtime: EngineeringRuntimeInventory,
) -> tuple[EngineeringSensorSpec, ...]:
    """Select only UUID-bound, numeric channels classified as sensors."""
    elements_by_uuid = {element.uuid: element for element in inventory.elements if element.uuid}
    bindings_by_uuid = {binding.engineering_uuid: binding for binding in runtime.bindings if binding.engineering_uuid}
    specs: list[EngineeringSensorSpec] = []
    for element in inventory.candidates:
        if element.suggested_platform != "sensor" or not element.uuid:
            continue
        binding = bindings_by_uuid.get(element.uuid)
        if binding is None or binding.status != "bound" or binding.numeric_value is None:
            continue
        specs.append(
            EngineeringSensorSpec(
                element=element,
                binding=binding,
                device=_nearest_device(element, elements_by_uuid),
                config_version=inventory.config_version,
            )
        )
    return tuple(specs)


def normalize_engineering_unit(
    unit: str | None,
    *,
    title: str | None,
    loxone_type: str | None,
) -> str | None:
    """Normalize only units whose meaning can be established conservatively."""
    safe_units = {
        "%",
        "°C",
        "°F",
        "C",
        "F",
        "V",
        "A",
        "W",
        "kW",
        "Wh",
        "kWh",
        "Hz",
        "lx",
        "Pa",
        "bar",
        "ppm",
        "s",
        "min",
        "h",
    }
    if unit is None:
        return None
    if unit != "°":
        return unit if unit in safe_units else None
    context = f"{title or ''} {loxone_type or ''}".casefold()
    if any(keyword in context for keyword in ("temperatur", "temperature", "temp")):
        return "°C"
    return unit


async def async_store_engineering_registry_metadata(
    hass: HomeAssistant,
    entry_id: str,
    metadata: object,
) -> None:
    """Store applied metadata or the unchanged coordinator's legacy audit scope."""
    from dataclasses import replace  # noqa: PLC0415

    from .engineering_registry import EngineeringRegistryMetadata  # noqa: PLC0415
    from .engineering_snapshot import (  # noqa: PLC0415
        async_load_engineering_state,
        async_store_engineering_state,
    )

    if not isinstance(metadata, EngineeringRegistryMetadata):
        if not isinstance(metadata, tuple) or not all(isinstance(item, EngineeringSensorSpec) for item in metadata):
            return
        identifiers = sorted({identifier for spec in metadata if (identifier := (spec.device or spec.element).uuid)})
        room_names = sorted({spec.element.room for spec in metadata if spec.element.room})
        legacy_store: Store[dict[str, list[str]]] = Store(
            hass,
            ENGINEERING_REGISTRY_STORAGE_VERSION,
            f"{ENGINEERING_REGISTRY_STORAGE_KEY}.{entry_id}",
            private=True,
        )
        await legacy_store.async_save(
            {
                "active_device_identifiers": identifiers,
                "room_names": room_names,
            }
        )
        return
    state = await async_load_engineering_state(hass, entry_id)
    if state.snapshot is None or metadata.applied_generation != state.snapshot.generation_id:
        return
    await async_store_engineering_state(
        hass,
        replace(
            state,
            registry_applied_generation=metadata.applied_generation,
            managed_area_ids=metadata.managed_area_ids,
        ),
    )


async def async_load_engineering_registry_metadata(
    hass: HomeAssistant,
    entry_id: str,
) -> EngineeringRegistryMetadata:
    """Reconstruct applied registry metadata from the private Task 4 envelope."""
    from .engineering_registry import (  # noqa: PLC0415
        EngineeringRegistryMetadata,
        registry_metadata_from_snapshot,
    )
    from .engineering_snapshot import async_load_engineering_state  # noqa: PLC0415

    state = await async_load_engineering_state(hass, entry_id)
    legacy_store: Store[dict[str, list[str]]] = Store(
        hass,
        ENGINEERING_REGISTRY_STORAGE_VERSION,
        f"{ENGINEERING_REGISTRY_STORAGE_KEY}.{entry_id}",
        private=True,
    )
    legacy = await legacy_store.async_load() or {}
    legacy_identifiers = frozenset(
        item for item in legacy.get("active_device_identifiers", ()) if isinstance(item, str) and item
    )
    legacy_rooms = frozenset(item for item in legacy.get("room_names", ()) if isinstance(item, str) and item)
    if state.snapshot is None:
        return EngineeringRegistryMetadata(
            legacy_identifiers,
            legacy_rooms,
            {},
            None,
            None,
        )
    applied = (
        state.snapshot.generation_id if state.registry_applied_generation == state.snapshot.generation_id else None
    )
    current = registry_metadata_from_snapshot(
        state.snapshot,
        managed_area_ids=state.managed_area_ids,
        applied_generation=applied,
    )
    if applied is not None:
        return current
    return EngineeringRegistryMetadata(
        current.active_device_identifiers | legacy_identifiers,
        current.room_names | legacy_rooms,
        current.managed_area_ids,
        None,
        current.provider_identifier,
    )


@callback
def async_sync_engineering_sensor_registry(
    hass: HomeAssistant,
    entry_id: str,
    miniserver_serial: str,
    specs: tuple[EngineeringSensorSpec, ...],
) -> int:
    """Make Loxone names, rooms, and physical grouping authoritative."""
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    updated = 0

    for spec in specs:
        device_element = spec.device or spec.element
        device_uuid = device_element.uuid or spec.element.uuid
        if not device_uuid:
            continue
        device_name = device_element.title or device_element.io_name or spec.element.title or device_uuid
        device = device_registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, device_uuid)},
            name=device_name,
            manufacturer="Loxone",
            model=device_element.loxone_type or "Engineering device",
            suggested_area=spec.element.room,
            via_device=(DOMAIN, miniserver_serial),
        )
        if spec.element.room:
            area = area_registry.async_get_area_by_name(spec.element.room)
            if area is None:
                area = area_registry.async_get_or_create(spec.element.room)
            if device.area_id != area.id:
                device = (
                    device_registry.async_update_device(
                        device.id,
                        area_id=area.id,
                    )
                    or device
                )
                updated += 1

        entity_id = entity_registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            spec.element.uuid,
        )
        if entity_id is None:
            continue
        entry = entity_registry.async_get(entity_id)
        desired_name = spec.element.title or spec.element.io_name or spec.element.uuid
        if entry is not None and (
            entry.device_id != device.id or entry.original_name != desired_name or entry.area_id is not None
        ):
            entity_registry.async_update_entity(
                entity_id,
                device_id=device.id,
                original_name=desired_name,
                area_id=None,
            )
            updated += 1
    return updated
