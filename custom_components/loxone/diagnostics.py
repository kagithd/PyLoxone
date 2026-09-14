"""Diagnostics support for Pyloxone."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import DOMAIN
from .engineering_capabilities import EngineeringInventoryRow, ExposureStatus

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def _public_row(
    row: EngineeringInventoryRow,
    provider_identifier: str,
) -> dict[str, Any]:
    """Return one allowlisted diagnostics row without runtime values."""
    if row.capability.exposure is ExposureStatus.SUPPRESSED or row.node.sensitive:
        return {
            "kind": "structural",
            "capability_status": "sensitive",
            "exposure_status": ExposureStatus.SUPPRESSED.value,
            "reason": "sensitive_metadata_suppressed",
        }
    element = row.node.element
    return {
        "name": element.title,
        "technical_type": element.loxone_type,
        "uuid": element.uuid,
        "source_miniserver": provider_identifier,
        "owner": row.node.device_identifier,
        "bus": row.node.bus_kind,
        "topology_path": list(row.node.topology_path),
        "room": element.room,
        "suggested_home_assistant_platform": row.semantic_platform,
        "capability_status": row.capability.state.value,
        "exposure_status": row.capability.exposure.value,
        "reason": row.capability.reason,
    }


def _inventory_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize only public row classifications."""
    exposure: dict[str, int] = {}
    capability: dict[str, int] = {}
    for row in rows:
        exposure[row["exposure_status"]] = exposure.get(row["exposure_status"], 0) + 1
        capability[row["capability_status"]] = capability.get(row["capability_status"], 0) + 1
    return {
        "row_count": len(rows),
        "by_capability": dict(sorted(capability.items())),
        "by_exposure": dict(sorted(exposure.items())),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, config_entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN].get(config_entry.entry_id)
    if coordinator is None:
        return None

    snapshot = coordinator.engineering_snapshot
    if snapshot is None:
        return {
            "integration": {"entry_id": config_entry.entry_id},
            "engineering_inventory": {
                "summary": {"row_count": 0, "by_capability": {}, "by_exposure": {}},
                "rows": [],
            },
            "engineering_runtime": {"status": "not_loaded"},
        }
    rows = [_public_row(row, snapshot.source.provider_identifier) for row in snapshot.rows]
    diagnostics: dict[str, Any] = {
        "integration": {
            "entry_id": config_entry.entry_id,
            "provider_identifier": snapshot.source.provider_identifier,
            "config_version": snapshot.source.config_version,
            "config_timestamp": snapshot.source.config_timestamp.isoformat(),
            "captured_at": snapshot.captured_at.isoformat(),
        },
        "engineering_inventory": {
            "summary": _inventory_summary(rows),
            "rows": rows,
        },
    }
    runtime = coordinator.engineering_runtime
    if runtime is not None:
        diagnostics["engineering_runtime"] = {"status": "live", **runtime.summary()}
    else:
        diagnostics["engineering_runtime"] = {"status": "restored_without_live_probe"}
    return diagnostics
