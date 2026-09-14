"""Pure, source-scoped changes for sanitized engineering snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .engineering_capabilities import EngineeringInventoryRow
    from .engineering_snapshot import EngineeringSnapshot


@dataclass(frozen=True, slots=True)
class EngineeringNodeChange:
    """Previous and candidate metadata for one stable engineering UUID."""

    unique_id: str
    old_name: str | None = None
    new_name: str | None = None
    old_room: str | None = None
    new_room: str | None = None
    old_owner_identifier: str | None = None
    new_owner_identifier: str | None = None
    old_via_identifier: str | None = None
    new_via_identifier: str | None = None
    old_semantic_platform: str | None = None
    new_semantic_platform: str | None = None


@dataclass(frozen=True, slots=True)
class EngineeringChangeSet:
    """Stable UUID additions, removals, and in-place metadata changes."""

    added: tuple[EngineeringNodeChange, ...] = ()
    removed: tuple[EngineeringNodeChange, ...] = ()
    metadata_changed: tuple[EngineeringNodeChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Return whether the snapshot has no stable-identity changes."""
        return not (self.added or self.removed or self.metadata_changed)


@dataclass(frozen=True, slots=True)
class EngineeringEntityImpact:
    """Sanitized Home Assistant consumer references for one changed entity."""

    unique_id: str
    entity_ids: tuple[str, ...]
    change_kind: Literal["area_changed", "removed", "platform_changed"]
    references: Mapping[str, tuple[str, ...]]
    target_area_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Freeze caller-owned collections at the recovery boundary."""
        object.__setattr__(self, "entity_ids", tuple(self.entity_ids))
        object.__setattr__(self, "target_area_ids", tuple(self.target_area_ids))
        object.__setattr__(
            self,
            "references",
            MappingProxyType({key: tuple(values) for key, values in sorted(self.references.items())}),
        )


@dataclass(frozen=True, slots=True)
class EngineeringImpactPlan:
    """Pre-mutation consumer impact intent tied to one committed generation."""

    generation_id: str
    impacts: tuple[EngineeringEntityImpact, ...]

    def __post_init__(self) -> None:
        """Freeze the ordered impact collection for replay."""
        object.__setattr__(self, "impacts", tuple(self.impacts))


def _source_scope(snapshot: EngineeringSnapshot) -> tuple[str, str]:
    return snapshot.source.entry_id, snapshot.source.provider_identifier


def _rows_by_uuid(
    snapshot: EngineeringSnapshot,
) -> dict[str, EngineeringInventoryRow]:
    return {row.node.element.uuid: row for row in snapshot.rows if row.node.element.uuid is not None}


def _change(
    previous: EngineeringInventoryRow | None,
    current: EngineeringInventoryRow | None,
    unique_id: str,
) -> EngineeringNodeChange:
    return EngineeringNodeChange(
        unique_id=unique_id,
        old_name=previous.node.element.title if previous else None,
        new_name=current.node.element.title if current else None,
        old_room=previous.node.element.room if previous else None,
        new_room=current.node.element.room if current else None,
        old_owner_identifier=previous.node.device_identifier if previous else None,
        new_owner_identifier=current.node.device_identifier if current else None,
        old_via_identifier=previous.node.via_device_identifier if previous else None,
        new_via_identifier=current.node.via_device_identifier if current else None,
        old_semantic_platform=previous.semantic_platform if previous else None,
        new_semantic_platform=current.semantic_platform if current else None,
    )


def diff_engineering_snapshots(
    previous: EngineeringSnapshot,
    current: EngineeringSnapshot,
) -> EngineeringChangeSet:
    """Compare stable UUIDs only inside one config-entry/provider source scope."""
    if _source_scope(previous) != _source_scope(current):
        from .engineering_snapshot import EngineeringSnapshotError  # noqa: PLC0415

        message = "snapshot source scope does not match"
        raise EngineeringSnapshotError(message)

    old_rows = _rows_by_uuid(previous)
    new_rows = _rows_by_uuid(current)
    old_ids = set(old_rows)
    new_ids = set(new_rows)
    added = tuple(_change(None, new_rows[unique_id], unique_id) for unique_id in sorted(new_ids - old_ids))
    removed = tuple(_change(old_rows[unique_id], None, unique_id) for unique_id in sorted(old_ids - new_ids))
    metadata_changed: list[EngineeringNodeChange] = []
    for unique_id in sorted(old_ids & new_ids):
        change = _change(old_rows[unique_id], new_rows[unique_id], unique_id)
        if any(
            (
                change.old_name != change.new_name,
                change.old_room != change.new_room,
                change.old_owner_identifier != change.new_owner_identifier,
                change.old_via_identifier != change.new_via_identifier,
                change.old_semantic_platform != change.new_semantic_platform,
            )
        ):
            metadata_changed.append(change)
    return EngineeringChangeSet(added, removed, tuple(metadata_changed))
