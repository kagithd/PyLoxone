"""Prepared Home Assistant entities derived from verified engineering channels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.helpers.storage import Store
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.core import callback

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .engineering_config import EngineeringElement, EngineeringInventory
    from .engineering_runtime import EngineeringRuntimeBinding, EngineeringRuntimeInventory

ENGINEERING_REGISTRY_STORAGE_VERSION = 1
ENGINEERING_REGISTRY_STORAGE_KEY = "loxone.engineering_registry"


@dataclass(frozen=True, slots=True)
class EngineeringSensorSpec:
    """A verified numeric engineering channel safe to prepare as a sensor."""

    element: EngineeringElement
    binding: EngineeringRuntimeBinding
    device: EngineeringElement | None
    config_version: int


def engineering_inventory_updated_signal(entry_id: str) -> str:
    """Return the config-entry-scoped engineering refresh signal."""
    return f"loxone_engineering_inventory_updated_{entry_id}"


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
    if unit != "°":
        return unit
    context = f"{title or ''} {loxone_type or ''}".casefold()
    if any(keyword in context for keyword in ("temperatur", "temperature", "temp")):
        return "°C"
    return unit


async def async_store_engineering_registry_metadata(
    hass: HomeAssistant,
    entry_id: str,
    specs: tuple[EngineeringSensorSpec, ...],
) -> None:
    """Persist only identities and rooms needed by registry reconciliation."""
    identifiers = sorted({identifier for spec in specs if (identifier := (spec.device or spec.element).uuid)})
    room_names = sorted({spec.element.room for spec in specs if spec.element.room})
    store: Store[dict[str, list[str]]] = Store(
        hass,
        ENGINEERING_REGISTRY_STORAGE_VERSION,
        f"{ENGINEERING_REGISTRY_STORAGE_KEY}.{entry_id}",
        private=True,
    )
    await store.async_save(
        {
            "active_device_identifiers": identifiers,
            "room_names": room_names,
        }
    )


async def async_load_engineering_registry_metadata(
    hass: HomeAssistant,
    entry_id: str,
) -> tuple[set[str], set[str]]:
    """Load the last confirmed engineering registry identities and rooms."""
    store: Store[dict[str, list[str]]] = Store(
        hass,
        ENGINEERING_REGISTRY_STORAGE_VERSION,
        f"{ENGINEERING_REGISTRY_STORAGE_KEY}.{entry_id}",
        private=True,
    )
    stored = await store.async_load() or {}
    identifiers = {item for item in stored.get("active_device_identifiers", []) if isinstance(item, str) and item}
    room_names = {item for item in stored.get("room_names", []) if isinstance(item, str) and item}
    return identifiers, room_names


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
