"""Diagnostics support for Pyloxone."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, config_entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN].get(config_entry.entry_id)
    if coordinator is None:
        return None

    diagnostics: dict[str, Any] = {
        "LoxAPP3.json": coordinator.miniserver.lox_config.json,
    }
    inventory = coordinator.engineering_inventory
    if inventory is not None:
        diagnostics["engineering_inventory"] = {
            "summary": inventory.summary(),
            "elements": [element.as_public_dict() for element in inventory.elements],
            "prepared_candidates": [element.as_public_dict() for element in inventory.candidates],
        }
    runtime = coordinator.engineering_runtime
    if runtime is not None:
        diagnostics["engineering_runtime"] = {
            "summary": runtime.summary(),
            "bindings": [binding.as_public_dict() for binding in runtime.bindings],
        }
    return diagnostics
