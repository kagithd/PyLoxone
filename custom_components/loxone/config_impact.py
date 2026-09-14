"""Warn about Loxone configuration changes that affect Home Assistant."""

from __future__ import annotations

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
from .engineering_changes import EngineeringEntityImpact, EngineeringImpactPlan, diff_engineering_snapshots

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .engineering_snapshot import EngineeringSnapshot

NOTIFICATION_ID_PREFIX = f"{DOMAIN}_config_impact"
REFERENCE_TYPES = (
    ItemType.AUTOMATION,
    ItemType.SCRIPT,
    ItemType.SCENE,
    ItemType.GROUP,
    ItemType.PERSON,
)
_IMPACT_SCOPE_MISMATCH = "engineering impact evidence does not match its source generation"


async def async_find_engineering_change_impacts(  # noqa: PLR0912 -- keep scoped, mutation-free applicability checks together.
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    previous: EngineeringSnapshot | None,
    candidate: EngineeringSnapshot,
    previous_plan: EngineeringImpactPlan | None = None,
) -> EngineeringImpactPlan:
    """Reconcile unresolved and new impacts against pre-mutation source identity."""
    if candidate.source.entry_id != config_entry.entry_id or (
        previous_plan is not None and (previous is None or previous_plan.generation_id != previous.generation_id)
    ):
        raise ValueError(_IMPACT_SCOPE_MISMATCH)
    if previous is None:
        return EngineeringImpactPlan(candidate.generation_id, ())
    # The diff validates entry/provider equality before old evidence is reused.
    changes = diff_engineering_snapshots(previous, candidate)
    registry = er.async_get(hass)
    targets: dict[str, set[str]] = {}
    if previous_plan is not None:
        for impact in previous_plan.impacts:
            targets.setdefault(impact.unique_id, set()).update(
                entity_id.partition(".")[0] for entity_id in impact.entity_ids
            )
    for change in (*changes.removed, *changes.metadata_changed):
        if change in changes.metadata_changed and change.old_semantic_platform == change.new_semantic_platform:
            continue
        if change.old_semantic_platform is not None:
            targets.setdefault(change.unique_id, set()).add(change.old_semantic_platform)
    current_platforms = {row.node.element.uuid: row.semantic_platform for row in candidate.rows}
    impacts = []
    for unique_id, platforms in sorted(targets.items()):
        entity_ids = set()
        references: dict[str, set[str]] = {}
        for platform in sorted(platforms & {"sensor", "binary_sensor"}):
            if current_platforms.get(unique_id) == platform:
                continue
            entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
            entry = registry.async_get(entity_id) if entity_id else None
            if (
                entry is None
                or entry.config_entry_id != config_entry.entry_id
                or entry.unique_id != unique_id
                or entry.platform != DOMAIN
            ):
                continue
            results = Searcher(hass, entity_sources(hass)).async_search(ItemType.ENTITY, entity_id)
            current = {
                kind.value: values for kind, values in _relevant_references(results).items() if kind != ItemType.PERSON
            }
            if current:
                entity_ids.add(entity_id)
                for kind, values in current.items():
                    references.setdefault(kind, set()).update(values)
        if references:
            impacts.append(
                EngineeringEntityImpact(
                    unique_id,
                    tuple(sorted(entity_ids)),
                    "platform_changed" if unique_id in current_platforms else "removed",
                    {kind: tuple(sorted(values)) for kind, values in sorted(references.items())},
                )
            )
    return EngineeringImpactPlan(candidate.generation_id, tuple(impacts))


async def async_publish_engineering_impact_plan(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    provider_identifier: str,
    plan: EngineeringImpactPlan,
    *,
    recheck: bool = False,
) -> None:
    """Idempotently replace one source-scoped count-only impact notification."""
    notification_id = f"loxone_engineering_impact_{config_entry.entry_id}_{provider_identifier}"
    impacts = plan.impacts
    if recheck and impacts:
        registry = er.async_get(hass)
        sources = entity_sources(hass)
        applicable = []
        for impact in impacts:
            for entity_id in impact.entity_ids:
                entry = registry.async_get(entity_id)
                if (
                    entry is None
                    or entry.config_entry_id != config_entry.entry_id
                    or entry.unique_id != impact.unique_id
                ):
                    continue
                results = Searcher(hass, sources).async_search(ItemType.ENTITY, entity_id)
                if any(
                    set(results.get(kind, ())) & set(impact.references.get(kind.value, ()))
                    for kind in REFERENCE_TYPES
                    if kind != ItemType.PERSON
                ):
                    applicable.append(impact)
                    break
        impacts = tuple(applicable)
    if not impacts:
        persistent_notification.async_dismiss(hass, notification_id)
        return
    persistent_notification.async_create(
        hass,
        (
            f"{len(impacts)} engineering channel change(s) affect Home Assistant consumers. "
            "Review the affected configuration; entity identifiers were not renamed."
        ),
        title="PyLoxone engineering configuration review",
        notification_id=notification_id,
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
