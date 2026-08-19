"""Safely reconcile the Home Assistant registry with the Loxone structure."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components import automation, persistent_notification, script
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    CONF_STALE_DEVICE_AUTO_CLEANUP,
    CONF_STALE_DEVICE_GRACE_HOURS,
    CONF_STALE_DEVICE_GRACE_MODE,
    CONF_STALE_DEVICE_GRACE_OBSERVATIONS,
    DEFAULT_STALE_DEVICE_AUTO_CLEANUP,
    DEFAULT_STALE_DEVICE_GRACE_HOURS,
    DEFAULT_STALE_DEVICE_GRACE_MODE,
    DEFAULT_STALE_DEVICE_GRACE_OBSERVATIONS,
    DOMAIN,
)
from .device_sync import (
    async_cleanup_stale_devices,
    control_identifiers_from_lox_config,
)
from .engineering_entities import async_load_engineering_registry_metadata

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.registry_maintenance"
NOTIFICATION_ID_PREFIX = f"{DOMAIN}_registry_maintenance"


@dataclass(frozen=True)
class StaleDevice:
    """A Home Assistant device missing from the current Loxone structure."""

    name: str
    identifier: str
    entity_ids: tuple[str, ...]
    observations: int
    missing_since: float


@dataclass(frozen=True)
class OrphanRoom:
    """A former Loxone room whose Home Assistant area is now empty."""

    name: str
    automations: tuple[str, ...]
    scripts: tuple[str, ...]


@dataclass(frozen=True)
class RegistryMaintenanceResult:
    """Result of one successful registry reconciliation pass."""

    audit_only: bool
    pending: tuple[StaleDevice, ...]
    removed: tuple[StaleDevice, ...]
    orphan_rooms: tuple[OrphanRoom, ...]
    removed_devices: int = 0
    removed_entities: int = 0
    skipped: bool = False


def room_names_from_lox_config(lox_config: Mapping[str, Any]) -> set[str]:
    """Return configured Loxone room names."""
    rooms = lox_config.get("rooms", {})
    if not isinstance(rooms, Mapping):
        return set()

    return {
        name
        for room in rooms.values()
        if isinstance(room, Mapping)
        and isinstance((name := room.get("name")), str)
        and name
    }


def _stale_devices(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lox_config: Mapping[str, Any],
    observations: Mapping[str, int],
    missing_since: Mapping[str, float],
    additional_active_identifiers: set[str] | None = None,
) -> list[StaleDevice]:
    """Return registry devices absent from the current Loxone structure."""
    active_identifiers = control_identifiers_from_lox_config(lox_config)
    active_identifiers.update(additional_active_identifiers or set())
    miniserver_serial = lox_config.get("msInfo", {}).get("serialNr")
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    stale: list[StaleDevice] = []

    for device in dr.async_entries_for_config_entry(
        device_registry, config_entry.entry_id
    ):
        identifiers = sorted(
            identifier
            for domain, identifier in device.identifiers
            if domain == DOMAIN
            and identifier != miniserver_serial
            and identifier not in active_identifiers
        )
        if not identifiers:
            continue

        identifier = identifiers[0]
        entities = er.async_entries_for_device(entity_registry, device.id)
        stale.append(
            StaleDevice(
                name=device.name_by_user or device.name or identifier,
                identifier=identifier,
                entity_ids=tuple(
                    sorted(
                        entity.entity_id
                        for entity in entities
                        if entity.config_entry_id == config_entry.entry_id
                        and entity.platform == DOMAIN
                    )
                ),
                observations=observations.get(identifier, 0),
                missing_since=missing_since.get(identifier, 0.0),
            )
        )

    return stale


def _orphan_rooms(
    hass: HomeAssistant,
    previous_rooms: set[str],
    current_rooms: set[str],
) -> tuple[OrphanRoom, ...]:
    """Return former Loxone rooms that are now empty HA areas."""
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    orphaned: list[OrphanRoom] = []

    for room_name in sorted(previous_rooms - current_rooms):
        area = area_registry.async_get_area_by_name(room_name)
        if area is None:
            continue
        if dr.async_entries_for_area(device_registry, area.id):
            continue
        if er.async_entries_for_area(entity_registry, area.id):
            continue
        orphaned.append(
            OrphanRoom(
                name=room_name,
                automations=tuple(
                    sorted(automation.automations_with_area(hass, area.id))
                ),
                scripts=tuple(sorted(script.scripts_with_area(hass, area.id))),
            )
        )

    return tuple(orphaned)


def format_registry_maintenance_message(
    result: RegistryMaintenanceResult,
    grace_mode: str,
    grace_observations: int,
    grace_hours: int,
) -> str:
    """Format a complete registry audit notification."""
    grace_description = {
        "observations": f"{grace_observations} consecutive observations",
        "time": f"{grace_hours} elapsed hours",
        "combined": (
            f"both {grace_observations} consecutive observations and "
            f"{grace_hours} elapsed hours"
        ),
    }[grace_mode]
    mode = (
        f"audit only; automatic deletion is disabled (configured grace: {grace_description})"
        if result.audit_only
        else f"automatic cleanup after {grace_description}"
    )
    sections = [f"Mode: **{mode}**."]
    now = _utc_timestamp()

    if result.pending:
        lines = ["## Missing Loxone devices pending review"]
        for device in result.pending:
            entities = ", ".join(f"`{item}`" for item in device.entity_ids) or "none"
            lines.extend(
                (
                    f"- **{device.name}** (`{device.identifier}`)",
                    f"  - Entities: {entities}",
                )
            )
            if grace_mode in {"observations", "combined"}:
                lines.append(
                    "  - Confirmed in "
                    f"{device.observations}/{grace_observations} successful structure loads"
                )
            if grace_mode in {"time", "combined"}:
                missing_hours = max(0.0, (now - device.missing_since) / 3600)
                lines.append(
                    f"  - Missing for {missing_hours:.1f}/{grace_hours} hours"
                )
        sections.append("\n".join(lines))

    if result.removed:
        lines = ["## Removed stale registry devices"]
        for device in result.removed:
            entities = ", ".join(f"`{item}`" for item in device.entity_ids) or "none"
            lines.extend(
                (
                    f"- **{device.name}** (`{device.identifier}`)",
                    f"  - Removed entities: {entities}",
                )
            )
        sections.append("\n".join(lines))

    if result.orphan_rooms:
        lines = ["## Former Loxone rooms now unused in Home Assistant"]
        for room in result.orphan_rooms:
            lines.append(f"- **{room.name}**")
            if room.automations:
                lines.append(
                    "  - Automations: "
                    + ", ".join(f"`{item}`" for item in room.automations)
                )
            if room.scripts:
                lines.append(
                    "  - Scripts: "
                    + ", ".join(f"`{item}`" for item in room.scripts)
                )
        lines.append(
            "\nThese areas were not deleted because other Home Assistant configuration may still refer to them."
        )
        sections.append("\n".join(lines))

    sections.append(
        "Loxone remains the master. UUIDs are compared only after a complete, non-empty structure was loaded."
    )
    return "\n\n".join(sections)


def _utc_timestamp() -> float:
    """Return the current UTC timestamp for persistent grace tracking."""
    return datetime.now(UTC).timestamp()


def _grace_reached(
    mode: str,
    observations: int,
    missing_since: float,
    now: float,
    grace_observations: int,
    grace_hours: int,
) -> bool:
    """Return whether the selected grace rule has been met."""
    observations_reached = observations >= grace_observations
    time_reached = now - missing_since >= grace_hours * 3600
    if mode == "time":
        return time_reached
    if mode == "combined":
        return observations_reached and time_reached
    return observations_reached


async def async_run_registry_maintenance(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lox_config: Mapping[str, Any],
) -> RegistryMaintenanceResult:
    """Audit and optionally clean registry entries after a grace period."""
    engineering_identifiers, engineering_rooms = (
        await async_load_engineering_registry_metadata(hass, config_entry.entry_id)
    )
    active_identifiers = control_identifiers_from_lox_config(lox_config)
    active_identifiers.update(engineering_identifiers)
    if not active_identifiers:
        return RegistryMaintenanceResult(
            audit_only=not config_entry.options.get(
                CONF_STALE_DEVICE_AUTO_CLEANUP,
                DEFAULT_STALE_DEVICE_AUTO_CLEANUP,
            ),
            pending=(),
            removed=(),
            orphan_rooms=(),
            skipped=True,
        )

    auto_cleanup = config_entry.options.get(
        CONF_STALE_DEVICE_AUTO_CLEANUP,
        DEFAULT_STALE_DEVICE_AUTO_CLEANUP,
    )
    grace_observations = int(
        config_entry.options.get(
            CONF_STALE_DEVICE_GRACE_OBSERVATIONS,
            DEFAULT_STALE_DEVICE_GRACE_OBSERVATIONS,
        )
    )
    grace_observations = max(1, grace_observations)
    grace_mode = config_entry.options.get(
        CONF_STALE_DEVICE_GRACE_MODE,
        DEFAULT_STALE_DEVICE_GRACE_MODE,
    )
    if grace_mode not in {"observations", "time", "combined"}:
        grace_mode = DEFAULT_STALE_DEVICE_GRACE_MODE
    grace_hours = max(
        1,
        int(
            config_entry.options.get(
                CONF_STALE_DEVICE_GRACE_HOURS,
                DEFAULT_STALE_DEVICE_GRACE_HOURS,
            )
        ),
    )
    store: Store[dict[str, Any]] = Store(
        hass,
        STORAGE_VERSION,
        f"{STORAGE_KEY_PREFIX}.{config_entry.entry_id}",
        private=True,
    )
    stored = await store.async_load() or {}
    previous_observations = {
        str(identifier): int(count)
        for identifier, count in stored.get("missing_observations", {}).items()
    }
    previous_missing_since = {
        str(identifier): float(timestamp)
        for identifier, timestamp in stored.get("missing_since", {}).items()
    }
    stale_before = _stale_devices(
        hass,
        config_entry,
        lox_config,
        previous_observations,
        previous_missing_since,
        engineering_identifiers,
    )
    current_stale_ids = {device.identifier for device in stale_before}
    now = _utc_timestamp()
    observations = {
        identifier: min(
            previous_observations.get(identifier, 0) + 1,
            grace_observations,
        )
        for identifier in current_stale_ids
    }
    missing_since = {
        identifier: previous_missing_since.get(identifier, now)
        for identifier in current_stale_ids
    }
    stale_confirmed = tuple(
        StaleDevice(
            name=device.name,
            identifier=device.identifier,
            entity_ids=device.entity_ids,
            observations=observations[device.identifier],
            missing_since=missing_since[device.identifier],
        )
        for device in stale_before
    )

    removable_ids = {
        device.identifier
        for device in stale_confirmed
        if auto_cleanup
        and _grace_reached(
            grace_mode,
            device.observations,
            device.missing_since,
            now,
            grace_observations,
            grace_hours,
        )
    }
    removed = tuple(
        device for device in stale_confirmed if device.identifier in removable_ids
    )
    pending = tuple(
        device for device in stale_confirmed if device.identifier not in removable_ids
    )
    removed_devices = 0
    removed_entities = 0
    if removable_ids:
        removed_devices, removed_entities = async_cleanup_stale_devices(
            hass,
            config_entry,
            lox_config,
            identifiers_to_remove=removable_ids,
        )
        for identifier in removable_ids:
            observations.pop(identifier, None)
            missing_since.pop(identifier, None)

    current_rooms = room_names_from_lox_config(lox_config) | engineering_rooms
    previous_rooms = set(stored.get("loxone_rooms", []))
    orphan_rooms = _orphan_rooms(hass, previous_rooms, current_rooms)
    await store.async_save(
        {
            "missing_observations": observations,
            "missing_since": missing_since,
            "loxone_rooms": sorted(current_rooms),
        }
    )

    result = RegistryMaintenanceResult(
        audit_only=not auto_cleanup,
        pending=pending,
        removed=removed,
        orphan_rooms=orphan_rooms,
        removed_devices=removed_devices,
        removed_entities=removed_entities,
    )
    notification_id = f"{NOTIFICATION_ID_PREFIX}_{config_entry.entry_id}"
    if pending or removed or orphan_rooms:
        persistent_notification.async_create(
            hass,
            format_registry_maintenance_message(
                result,
                grace_mode,
                grace_observations,
                grace_hours,
            ),
            title="PyLoxone registry audit",
            notification_id=notification_id,
        )
    else:
        persistent_notification.async_dismiss(hass, notification_id)

    return result
