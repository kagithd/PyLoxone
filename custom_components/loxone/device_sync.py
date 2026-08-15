"""Synchronize Loxone device metadata with Home Assistant."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def device_names_from_lox_config(
    lox_config: Mapping[str, Any],
) -> dict[str, str]:
    """Return integration-controlled device names keyed by Loxone UUID."""
    controls = lox_config.get("controls", {})
    if not isinstance(controls, Mapping):
        return {}

    device_names: dict[str, str] = {}
    for control_uuid, control in controls.items():
        if not isinstance(control, Mapping):
            continue

        identifier = control.get("uuidAction", control_uuid)
        name = control.get("name")
        if isinstance(identifier, str) and identifier and isinstance(name, str) and name:
            device_names[identifier] = name

    return device_names


def device_rooms_from_lox_config(
    lox_config: Mapping[str, Any],
) -> dict[str, str]:
    """Return Loxone room names keyed by integration device UUID."""
    controls = lox_config.get("controls", {})
    rooms = lox_config.get("rooms", {})
    if not isinstance(controls, Mapping):
        return {}

    device_rooms: dict[str, str] = {}
    for control_uuid, control in controls.items():
        if not isinstance(control, Mapping):
            continue

        identifier = control.get("uuidAction", control_uuid)
        room_reference = control.get("room")
        if not isinstance(identifier, str) or not identifier:
            continue
        if not isinstance(room_reference, str) or not room_reference:
            continue

        room_name = room_reference
        if isinstance(rooms, Mapping):
            room = rooms.get(room_reference)
            if isinstance(room, Mapping):
                configured_name = room.get("name")
                if isinstance(configured_name, str) and configured_name:
                    room_name = configured_name

        device_rooms[identifier] = room_name

    return device_rooms


@callback
def async_sync_device_names(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lox_config: Mapping[str, Any],
) -> int:
    """Update registry device names from the current Loxone configuration."""
    device_registry = dr.async_get(hass)
    updated = 0

    for identifier, name in device_names_from_lox_config(lox_config).items():
        device = device_registry.async_get_device_by_identifier((DOMAIN, identifier), config_entry.entry_id)
        if device is None or device.name == name:
            continue

        device_registry.async_update_device(device.id, name=name)
        updated += 1

    return updated


@callback
def async_sync_device_areas(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lox_config: Mapping[str, Any],
) -> int:
    """Make Loxone room assignments authoritative for registry devices."""
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    updated = 0

    for identifier, room_name in device_rooms_from_lox_config(lox_config).items():
        device = device_registry.async_get_device_by_identifier((DOMAIN, identifier), config_entry.entry_id)
        if device is None:
            continue

        area = area_registry.async_get_area_by_name(room_name)
        if area is None:
            area = area_registry.async_get_or_create(room_name)
        device_changed = False
        if device.area_id != area.id:
            device_registry.async_update_device(device.id, area_id=area.id)
            device_changed = True

        for entity in er.async_entries_for_device(entity_registry, device.id):
            if (
                entity.config_entry_id == config_entry.entry_id
                and entity.platform == DOMAIN
                and entity.area_id is not None
            ):
                entity_registry.async_update_entity(entity.entity_id, area_id=None)
                device_changed = True

        if device_changed:
            updated += 1

    return updated
