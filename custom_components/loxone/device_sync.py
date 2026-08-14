"""Synchronize Loxone device metadata with Home Assistant."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr

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
