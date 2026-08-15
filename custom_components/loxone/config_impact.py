"""Warn about Loxone configuration changes that affect Home Assistant."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components import automation, persistent_notification, script
from homeassistant.components.search import ItemType, Searcher
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import entity_sources

from .const import DOMAIN
from .device_sync import control_identifiers_from_lox_config, device_rooms_from_lox_config

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

NOTIFICATION_ID_PREFIX = f"{DOMAIN}_config_impact"
REFERENCE_TYPES = (
    ItemType.AUTOMATION,
    ItemType.SCRIPT,
    ItemType.SCENE,
    ItemType.GROUP,
    ItemType.PERSON,
)


@dataclass(frozen=True)
class RemovedControlImpact:
    """A removed Loxone control still referenced by Home Assistant."""

    name: str
    identifier: str
    entity_ids: tuple[str, ...]
    references: Mapping[ItemType, tuple[str, ...]]


@dataclass(frozen=True)
class AreaChangeImpact:
    """A Loxone room move that changes area-based consumers."""

    name: str
    old_area: str
    new_area: str
    references: Mapping[ItemType, tuple[str, ...]]


def _relevant_references(
    results: Mapping[ItemType, set[str]],
) -> dict[ItemType, tuple[str, ...]]:
    """Return sorted Home Assistant consumers relevant to breakage warnings."""
    return {item_type: tuple(sorted(results[item_type])) for item_type in REFERENCE_TYPES if results.get(item_type)}


@callback
def find_removed_control_impacts(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lox_config: Mapping[str, Any],
) -> list[RemovedControlImpact]:
    """Find registry devices absent from Loxone but still referenced in HA."""
    active_identifiers = control_identifiers_from_lox_config(lox_config)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    sources = entity_sources(hass)
    impacts: list[RemovedControlImpact] = []

    for device in dr.async_entries_for_config_entry(device_registry, config_entry.entry_id):
        identifiers = sorted(
            identifier
            for domain, identifier in device.identifiers
            if domain == DOMAIN and identifier not in active_identifiers
        )
        if not identifiers:
            continue
        if isinstance(device.model, str) and device.model.startswith("Miniserver"):
            continue

        results = Searcher(hass, sources).async_search(ItemType.DEVICE, device.id)
        references = _relevant_references(results)
        if not references:
            continue

        entries = er.async_entries_for_device(entity_registry, device.id)
        impacts.append(
            RemovedControlImpact(
                name=device.name_by_user or device.name or identifiers[0],
                identifier=identifiers[0],
                entity_ids=tuple(sorted(entry.entity_id for entry in entries)),
                references=references,
            )
        )

    return impacts


@callback
def find_area_change_impacts(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lox_config: Mapping[str, Any],
) -> list[AreaChangeImpact]:
    """Find room moves that affect area-targeted automations or scripts."""
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    impacts: list[AreaChangeImpact] = []

    for identifier, room_name in device_rooms_from_lox_config(lox_config).items():
        device = device_registry.async_get_device_by_identifier((DOMAIN, identifier), config_entry.entry_id)
        if device is None:
            continue

        old_area = area_registry.async_get_area(device.area_id) if device.area_id else None
        new_area = area_registry.async_get_area_by_name(room_name)
        if old_area and old_area.name == room_name:
            continue

        area_ids = {area.id for area in (old_area, new_area) if area is not None}
        references: dict[ItemType, tuple[str, ...]] = {}
        automations = {
            entity_id for area_id in area_ids for entity_id in automation.automations_with_area(hass, area_id)
        }
        scripts = {entity_id for area_id in area_ids for entity_id in script.scripts_with_area(hass, area_id)}
        if automations:
            references[ItemType.AUTOMATION] = tuple(sorted(automations))
        if scripts:
            references[ItemType.SCRIPT] = tuple(sorted(scripts))
        if not references:
            continue

        impacts.append(
            AreaChangeImpact(
                name=device.name_by_user or device.name or identifier,
                old_area=old_area.name if old_area else "No area",
                new_area=room_name,
                references=references,
            )
        )

    return impacts


def _format_references(references: Mapping[ItemType, tuple[str, ...]]) -> str:
    """Format referenced Home Assistant consumers as Markdown."""
    labels = {
        ItemType.AUTOMATION: "Automations",
        ItemType.SCRIPT: "Scripts",
        ItemType.SCENE: "Scenes",
        ItemType.GROUP: "Groups",
        ItemType.PERSON: "Persons",
    }
    return "\n".join(
        f"  - {labels[item_type]}: {', '.join(f'`{item}`' for item in items)}"
        for item_type, items in references.items()
    )


def format_config_impact_message(
    removed: list[RemovedControlImpact],
    moved: list[AreaChangeImpact],
) -> str:
    """Format a persistent-notification message for detected impacts."""
    sections = ["Loxone is the master configuration. Review these Home Assistant references after the Loxone change."]
    if removed:
        lines = ["## Removed or replaced Loxone controls"]
        for impact in removed:
            entities = ", ".join(f"`{item}`" for item in impact.entity_ids) or "none"
            lines.extend(
                (
                    f"- **{impact.name}** (`{impact.identifier}`)",
                    f"  - Entities: {entities}",
                    _format_references(impact.references),
                )
            )
        sections.append("\n".join(lines))

    if moved:
        lines = ["## Loxone room changes affecting area targets"]
        for impact in moved:
            lines.extend(
                (
                    f"- **{impact.name}**: {impact.old_area} → {impact.new_area}",
                    _format_references(impact.references),
                )
            )
        sections.append("\n".join(lines))

    sections.append("Entity IDs are not changed automatically. Update or confirm the listed Home Assistant consumers.")
    return "\n\n".join(sections)


@callback
def async_warn_about_config_impacts(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lox_config: Mapping[str, Any],
) -> int:
    """Create a persistent warning when Loxone changes affect HA consumers."""
    removed = find_removed_control_impacts(hass, config_entry, lox_config)
    moved = find_area_change_impacts(hass, config_entry, lox_config)
    if not removed and not moved:
        return 0

    persistent_notification.async_create(
        hass,
        format_config_impact_message(removed, moved),
        title="PyLoxone configuration change requires review",
        notification_id=f"{NOTIFICATION_ID_PREFIX}_{config_entry.entry_id}",
    )
    return len(removed) + len(moved)
