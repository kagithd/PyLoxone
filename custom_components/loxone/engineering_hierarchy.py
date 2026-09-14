"""Privacy-safe generic hierarchy projection for engineering snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from .engineering_capabilities import CapabilityState, ExposureStatus
from .engineering_topology import NodeKind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .engineering_capabilities import EngineeringInventoryRow
    from .engineering_snapshot import EngineeringSnapshot

HierarchyRole = Literal[
    "provider",
    "internal_service",
    "bus",
    "physical_device",
    "structural",
]
HierarchyStatus = Literal[
    "active_entity",
    "prepared",
    "inventory_only",
    "unsupported",
]

_ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_MISSING_PROVIDER = "engineering snapshot has no safe provider root"


@dataclass(frozen=True, slots=True)
class HierarchyFunction:
    """One safe function attached to its resolved owner."""

    key: str
    label: str | None
    technical_type: str | None
    status: HierarchyStatus
    reason: str
    entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class HierarchyNode:
    """One immutable provider, service, bus, or physical hierarchy node."""

    identifier: str
    role: HierarchyRole
    label: str | None
    technical_type: str | None
    bus_kind: str | None
    functions: tuple[HierarchyFunction, ...]
    children: tuple[HierarchyNode, ...]
    protected_count: int = 0


@dataclass(frozen=True, slots=True)
class HierarchySummary:
    """Bounded counts for the hierarchy's visible capability states."""

    active_entity: int = 0
    prepared: int = 0
    inventory_only: int = 0
    unsupported: int = 0
    protected: int = 0


@dataclass(frozen=True, slots=True)
class EngineeringHierarchy:
    """One complete privacy-safe hierarchy rooted at its provider."""

    root: HierarchyNode
    summary: HierarchySummary


@dataclass(slots=True)
class _MutableHierarchyNode:
    """Private assembly value frozen before crossing the module boundary."""

    identifier: str
    role: HierarchyRole
    label: str | None
    technical_type: str | None
    bus_kind: str | None
    functions: list[HierarchyFunction] = field(default_factory=list)
    children: list[_MutableHierarchyNode] = field(default_factory=list)
    protected_count: int = 0


def _role(kind: NodeKind) -> HierarchyRole | None:
    if kind is NodeKind.MINISERVER:
        return "provider"
    if kind is NodeKind.SERVICE_MODULE:
        return "internal_service"
    if kind in {NodeKind.BUS, NodeKind.BRIDGE}:
        return "bus"
    if kind is NodeKind.PHYSICAL_DEVICE:
        return "physical_device"
    if kind is NodeKind.STRUCTURAL:
        return "structural"
    return None


def _sort_key(
    item: HierarchyFunction | HierarchyNode | _MutableHierarchyNode,
) -> tuple[str, str, str]:
    identifier = item.key if isinstance(item, HierarchyFunction) else item.identifier
    return (
        (item.label or "").casefold(),
        (item.technical_type or "").casefold(),
        identifier,
    )


def _status(
    row: EngineeringInventoryRow,
    entity_ids_by_unique_id: Mapping[str, str],
) -> tuple[HierarchyStatus, str | None]:
    uuid = row.node.element.uuid
    entity_id = entity_ids_by_unique_id.get(uuid or "")
    if isinstance(entity_id, str) and _ENTITY_ID_PATTERN.fullmatch(entity_id):
        return "active_entity", entity_id
    if row.capability.state is CapabilityState.UNSUPPORTED:
        return "unsupported", None
    if row.capability.exposure is ExposureStatus.PREPARED_DISABLED:
        return "prepared", None
    return "inventory_only", None


def _freeze(node: _MutableHierarchyNode) -> HierarchyNode:
    return HierarchyNode(
        identifier=node.identifier,
        role=node.role,
        label=node.label,
        technical_type=node.technical_type,
        bus_kind=node.bus_kind,
        functions=tuple(sorted(node.functions, key=_sort_key)),
        children=tuple(_freeze(child) for child in sorted(node.children, key=_sort_key)),
        protected_count=node.protected_count,
    )


def _build_nodes(
    rows: tuple[EngineeringInventoryRow, ...],
) -> tuple[
    dict[str, _MutableHierarchyNode],
    dict[str, _MutableHierarchyNode],
]:
    by_key: dict[str, _MutableHierarchyNode] = {}
    by_identifier: dict[str, _MutableHierarchyNode] = {}
    for row in rows:
        node = row.node
        role = _role(node.kind)
        identifier = node.device_identifier
        if role is None or identifier is None:
            continue
        builder = _MutableHierarchyNode(
            identifier=identifier,
            role=role,
            label=node.element.title or node.element.io_name,
            technical_type=node.element.loxone_type,
            bus_kind=node.bus_kind,
        )
        by_key[node.element.key] = builder
        by_identifier[identifier] = builder
    return by_key, by_identifier


def _attach_nodes(
    rows: tuple[EngineeringInventoryRow, ...],
    root: _MutableHierarchyNode,
    by_key: dict[str, _MutableHierarchyNode],
    by_identifier: dict[str, _MutableHierarchyNode],
) -> None:
    for row in rows:
        node = row.node
        builder = by_key.get(node.element.key)
        if builder is None or builder is root:
            continue
        parent = by_identifier.get(node.via_device_identifier or "")
        if parent is None and node.owner_key != node.element.key:
            parent = by_key.get(node.owner_key or "")
        if parent is not None:
            parent.children.append(builder)


def _attach_functions(
    rows: tuple[EngineeringInventoryRow, ...],
    by_key: dict[str, _MutableHierarchyNode],
    by_identifier: dict[str, _MutableHierarchyNode],
    entity_ids: Mapping[str, str],
) -> dict[HierarchyStatus, int]:
    counts: dict[HierarchyStatus, int] = {
        "active_entity": 0,
        "prepared": 0,
        "inventory_only": 0,
        "unsupported": 0,
    }
    for row in rows:
        node = row.node
        if node.kind is not NodeKind.CHANNEL:
            continue
        owner = by_key.get(node.owner_key or "")
        if owner is None:
            owner = by_identifier.get(node.device_identifier or "")
        if owner is None:
            continue
        status, entity_id = _status(row, entity_ids)
        counts[status] += 1
        owner.functions.append(
            HierarchyFunction(
                key=node.element.uuid or node.element.key,
                label=node.element.title or node.element.io_name,
                technical_type=node.element.loxone_type,
                status=status,
                reason=row.capability.reason,
                entity_id=entity_id,
            )
        )
    return counts


def _attach_protected(
    snapshot: EngineeringSnapshot,
    root: _MutableHierarchyNode,
    by_key: dict[str, _MutableHierarchyNode],
    by_identifier: dict[str, _MutableHierarchyNode],
) -> int:
    protected = 0
    for row in snapshot.rows:
        node = row.node
        if not node.sensitive:
            continue
        protected += 1
        owner = by_key.get(node.owner_key or "")
        if owner is None:
            owner = by_identifier.get(node.device_identifier or "")
        (owner or root).protected_count += 1
    return protected


def build_engineering_hierarchy(
    snapshot: EngineeringSnapshot,
    *,
    entity_ids_by_unique_id: Mapping[str, str] | None = None,
) -> EngineeringHierarchy:
    """Project a validated snapshot using resolved technical ownership only."""
    entity_ids = entity_ids_by_unique_id or {}
    safe_rows = tuple(row for row in snapshot.rows if not row.node.sensitive)
    root_row = next(
        (
            row
            for row in safe_rows
            if row.node.kind is NodeKind.MINISERVER
            and row.node.device_identifier == snapshot.source.provider_identifier
        ),
        None,
    )
    if root_row is None:
        raise ValueError(_MISSING_PROVIDER)

    builders_by_key, builders_by_identifier = _build_nodes(safe_rows)
    root = builders_by_key[root_row.node.element.key]
    _attach_nodes(safe_rows, root, builders_by_key, builders_by_identifier)
    counts = _attach_functions(safe_rows, builders_by_key, builders_by_identifier, entity_ids)
    protected = _attach_protected(snapshot, root, builders_by_key, builders_by_identifier)

    return EngineeringHierarchy(
        root=_freeze(root),
        summary=HierarchySummary(
            active_entity=counts["active_entity"],
            prepared=counts["prepared"],
            inventory_only=counts["inventory_only"],
            unsupported=counts["unsupported"],
            protected=protected,
        ),
    )


def _function_to_dict(item: HierarchyFunction) -> dict[str, object]:
    return {
        "key": item.key,
        "label": item.label,
        "technical_type": item.technical_type,
        "status": item.status,
        "reason": item.reason,
        "entity_id": item.entity_id,
    }


def _node_to_dict(node: HierarchyNode, *, root: bool = False) -> dict[str, object]:
    sections = tuple(child for child in node.children if child.role == "internal_service")
    children = tuple(child for child in node.children if child.role != "internal_service")
    result: dict[str, object] = {
        "identifier": node.identifier,
        "role": node.role,
        "label": node.label,
        "technical_type": node.technical_type,
        "bus_kind": node.bus_kind,
        "functions": [_function_to_dict(item) for item in node.functions],
    }
    if root:
        result["sections"] = [_node_to_dict(child) for child in sections]
    result["children"] = [_node_to_dict(child) for child in children]
    result["protected_count"] = node.protected_count
    return result


def hierarchy_to_dict(hierarchy: EngineeringHierarchy) -> dict[str, object]:
    """Serialize a hierarchy without raw snapshot or XML attributes."""
    summary = hierarchy.summary
    return {
        "root": _node_to_dict(hierarchy.root, root=True),
        "summary": {
            "active_entity": summary.active_entity,
            "prepared": summary.prepared,
            "inventory_only": summary.inventory_only,
            "unsupported": summary.unsupported,
            "protected": summary.protected,
        },
    }
