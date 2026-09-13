"""Private persistence for validated, sanitized engineering snapshots."""

# Validation deliberately raises field-specific bounded messages at each trust
# boundary; moving dozens of those static messages to module constants would
# make the policy harder to audit without changing runtime behavior.
# ruff: noqa: EM101, EM102, TRY003

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from ipaddress import ip_address
import json
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .engineering_capabilities import (
    CapabilityState,
    EngineeringCapability,
    EngineeringInventoryRow,
    ExposureStatus,
    SafeRuntimeBindingDescriptor,
)
from .engineering_changes import EngineeringEntityImpact, EngineeringImpactPlan
from .engineering_config import EngineeringElement
from .engineering_topology import (
    EngineeringSourceContext,
    NodeKind,
    ResolvedEngineeringNode,
    ResolutionStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from homeassistant.core import HomeAssistant

ENGINEERING_SNAPSHOT_STORAGE_VERSION = 2
ENGINEERING_SNAPSHOT_STORAGE_KEY = "loxone.engineering_snapshot"
_DIGEST_PATTERN = re.compile(r"^(?:rev|safe|gen):[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_OPAQUE_KEY_PATTERN = re.compile(r"^xml:(?:\d{6}|fixture)$")
_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_HA_ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_URL_PATTERN = re.compile(r"(?:https?|ftp)://", re.IGNORECASE)
_ADDRESS_CONTENT = re.compile(r"[0-9A-Fa-f:.]+")
_SEMANTIC_PLATFORMS = frozenset({"sensor", "binary_sensor"})
_VALUE_KINDS = frozenset({"number", "boolean"})
_MAX_SAFE_STRING_LENGTH = 256
_FIRST_CONTROL_CHARACTER = 32


class EngineeringSnapshotError(ValueError):
    """Raised when a candidate or stored snapshot is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class EngineeringSnapshot:
    """One complete source-scoped last-known-good engineering observation."""

    source: EngineeringSourceContext
    nodes: tuple[ResolvedEngineeringNode, ...]
    rows: tuple[EngineeringInventoryRow, ...]
    configuration_revision_id: str
    safe_content_digest: str
    read_sequence: int
    generation_id: str
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class StoredEngineeringState:
    """Atomic recovery envelope for a committed engineering generation."""

    snapshot: EngineeringSnapshot | None
    registry_applied_generation: str | None = None
    pending_impact_plan: EngineeringImpactPlan | None = None
    impact_published_generation: str | None = None
    managed_area_ids: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Detach integration-owned metadata from caller-mutable mappings."""
        object.__setattr__(
            self,
            "managed_area_ids",
            MappingProxyType(dict(sorted(self.managed_area_ids.items()))),
        )


def _canonical_digest(prefix: str, value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{prefix}:{hashlib.sha256(rendered).hexdigest()}"


def _normalized_timestamp(value: datetime, field_name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EngineeringSnapshotError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _source_scope(source: EngineeringSourceContext) -> dict[str, str | None]:
    return {
        "entry_id": source.entry_id,
        "serial_number": source.serial_number,
    }


def _normalized_revision(source: EngineeringSourceContext) -> dict[str, Any]:
    revision = source.loxapp_last_modified
    if revision is not None:
        if not isinstance(revision, str):
            raise EngineeringSnapshotError("source revision must be a scalar string")
        normalized = revision.strip()
        if not normalized or normalized != revision or not _IDENTIFIER_PATTERN.fullmatch(normalized):
            raise EngineeringSnapshotError("source revision has an unsafe shape")
        return {"kind": "last_modified", "value": normalized}
    return {
        "kind": "archive_fallback",
        "config_version": source.config_version,
        "config_timestamp": _normalized_timestamp(
            source.config_timestamp,
            "config_timestamp",
        ),
    }


def engineering_configuration_revision_id(source: EngineeringSourceContext) -> str:
    """Hash stable provider and source revision metadata, excluding capture time."""
    return _canonical_digest(
        "rev",
        {"source": _source_scope(source), "revision": _normalized_revision(source)},
    )


def _node_to_dict(node: ResolvedEngineeringNode) -> dict[str, Any]:
    element = node.element
    return {
        "key": element.key,
        "parent_key": element.parent_key,
        "uuid": element.uuid,
        "loxone_type": element.loxone_type,
        "name": element.title,
        "room": element.room,
        "io_name": element.io_name,
        "kind": node.kind.value,
        "owner_key": node.owner_key,
        "owner_identifier": node.device_identifier,
        "via_identifier": node.via_device_identifier,
        "bus_kind": node.bus_kind,
        "topology_path": list(node.topology_path),
        "resolution_status": node.resolution_status.value,
        "resolution_reason": node.resolution_reason,
        "sensitive": node.sensitive,
    }


def _row_to_dict(row: EngineeringInventoryRow) -> dict[str, Any]:
    binding = row.binding
    return {
        "node_key": row.node.element.key,
        "capability_state": row.capability.state.value,
        "capability_platform": row.capability.platform,
        "exposure": row.capability.exposure.value,
        "capability_reason": row.capability.reason,
        "semantic_platform": row.semantic_platform,
        "has_binding": binding is not None,
        "binding_method": binding.binding_method if binding else None,
        "value_kind": binding.value_kind if binding else None,
        "safe_unit": binding.safe_unit if binding else None,
        "state_uuid": binding.state_uuid if binding else None,
        "event_binding_proven": binding.event_binding_proven if binding else False,
    }


def _content_payload(
    source: EngineeringSourceContext,
    nodes: Sequence[ResolvedEngineeringNode],
    rows: Sequence[EngineeringInventoryRow],
) -> dict[str, Any]:
    return {
        "source_scope": _source_scope(source),
        "nodes": sorted((_node_to_dict(node) for node in nodes), key=lambda item: item["key"]),
        "rows": sorted((_row_to_dict(row) for row in rows), key=lambda item: item["node_key"]),
    }


def engineering_safe_content_digest(
    source: EngineeringSourceContext,
    nodes: Sequence[ResolvedEngineeringNode],
    rows: Sequence[EngineeringInventoryRow],
) -> str:
    """Hash canonical sanitized topology, capability, and binding metadata."""
    return _canonical_digest("safe", _content_payload(source, nodes, rows))


def engineering_generation_id(
    source: EngineeringSourceContext,
    nodes: Sequence[ResolvedEngineeringNode],
    rows: Sequence[EngineeringInventoryRow],
    *,
    read_sequence: int,
) -> str:
    """Create one application/observation token for a complete committed read."""
    if isinstance(read_sequence, bool) or not isinstance(read_sequence, int) or read_sequence < 1:
        raise EngineeringSnapshotError("read_sequence must be a positive integer")
    return _canonical_digest(
        "gen",
        {
            "source": _source_scope(source),
            "configuration_revision_id": engineering_configuration_revision_id(source),
            "safe_content_digest": engineering_safe_content_digest(source, nodes, rows),
            "read_sequence": read_sequence,
        },
    )


def next_engineering_read_sequence(previous: EngineeringSnapshot | None) -> int:
    """Return the sequence for a new complete read; replay keeps its snapshot."""
    if previous is None:
        return 1
    validate_engineering_snapshot(previous)
    return previous.read_sequence + 1


def _require_dict(
    value: Any,
    field_name: str,
    *,
    required: frozenset[str],
    allowed: frozenset[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EngineeringSnapshotError(f"{field_name} must be an object")
    actual = set(value)
    permitted = allowed or required
    if not required <= actual or not actual <= permitted:
        raise EngineeringSnapshotError(f"{field_name} has invalid fields")
    return value


def _require_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise EngineeringSnapshotError(f"{field_name} must be a list")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise EngineeringSnapshotError(f"{field_name} must be boolean")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > _MAX_SAFE_STRING_LENGTH
        or any(ord(item) < _FIRST_CONTROL_CHARACTER for item in value)
    ):
        raise EngineeringSnapshotError(f"{field_name} must be a bounded string")
    if _URL_PATTERN.search(value):
        raise EngineeringSnapshotError(f"{field_name} contains unsafe URL data")
    for candidate in _ADDRESS_CONTENT.findall(value):
        try:
            ip_address(candidate)
        except ValueError:
            continue
        raise EngineeringSnapshotError(f"{field_name} contains network address data")
    return value


def _required_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise EngineeringSnapshotError(f"{field_name} has an unsafe identifier shape")
    return value


def _optional_identifier(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_identifier(value, field_name)


def _required_reason(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _REASON_PATTERN.fullmatch(value):
        raise EngineeringSnapshotError(f"{field_name} must be a fixed reason code")
    return value


def _required_digest(value: Any, kind: str, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value) or not value.startswith(f"{kind}:"):
        raise EngineeringSnapshotError(f"{field_name} has an invalid token")
    return value


def _datetime_from_string(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise EngineeringSnapshotError(f"{field_name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise EngineeringSnapshotError(f"{field_name} must be an ISO timestamp") from err
    if parsed.tzinfo is None:
        raise EngineeringSnapshotError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _source_to_dict(source: EngineeringSourceContext) -> dict[str, Any]:
    return {
        "entry_id": source.entry_id,
        "serial_number": source.serial_number,
        "config_version": source.config_version,
        "config_timestamp": _normalized_timestamp(source.config_timestamp, "config_timestamp"),
        "loxapp_last_modified": source.loxapp_last_modified,
    }


def _source_from_dict(value: Any) -> EngineeringSourceContext:
    data = _require_dict(
        value,
        "source",
        required=frozenset(
            {
                "entry_id",
                "serial_number",
                "config_version",
                "config_timestamp",
                "loxapp_last_modified",
            }
        ),
    )
    entry_id = _required_identifier(data["entry_id"], "source entry_id")
    serial = _optional_identifier(data["serial_number"], "source serial_number")
    version = data["config_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise EngineeringSnapshotError("source config_version must be a non-negative integer")
    revision = _optional_identifier(data["loxapp_last_modified"], "source revision")
    return EngineeringSourceContext(
        entry_id=entry_id,
        serial_number=serial,
        title=None,
        model=None,
        source_archive="",
        config_version=version,
        config_timestamp=_datetime_from_string(data["config_timestamp"], "config_timestamp"),
        loxapp_last_modified=revision,
    )


_NODE_FIELDS = frozenset(
    {
        "key",
        "parent_key",
        "uuid",
        "loxone_type",
        "name",
        "room",
        "io_name",
        "kind",
        "owner_key",
        "owner_identifier",
        "via_identifier",
        "bus_kind",
        "topology_path",
        "resolution_status",
        "resolution_reason",
        "sensitive",
    }
)
_ROW_FIELDS = frozenset(
    {
        "node_key",
        "capability_state",
        "capability_platform",
        "exposure",
        "capability_reason",
        "semantic_platform",
        "has_binding",
        "binding_method",
        "value_kind",
        "safe_unit",
        "state_uuid",
        "event_binding_proven",
    }
)


def _node_from_dict(value: Any) -> ResolvedEngineeringNode:
    data = _require_dict(value, "nodes item", required=_NODE_FIELDS)
    key = _required_identifier(data["key"], "node key")
    uuid = _optional_identifier(data["uuid"], "node uuid")
    if uuid is None:
        if not _OPAQUE_KEY_PATTERN.fullmatch(key):
            raise EngineeringSnapshotError("node key is not an opaque parser key")
    elif key != uuid:
        raise EngineeringSnapshotError("node key must equal its stable uuid")
    parent_key = _optional_identifier(data["parent_key"], "node parent_key")
    loxone_type = _optional_identifier(data["loxone_type"], "node loxone_type")
    owner_key = _optional_identifier(data["owner_key"], "node owner_key")
    topology = _require_list(data["topology_path"], "node topology_path")
    topology_path = tuple(_optional_string(item, "node topology path item") or "" for item in topology)
    if any(not item for item in topology_path):
        raise EngineeringSnapshotError("node topology_path contains an empty item")
    try:
        kind = NodeKind(data["kind"])
    except (TypeError, ValueError) as err:
        raise EngineeringSnapshotError("node kind is invalid") from err
    try:
        status = ResolutionStatus(data["resolution_status"])
    except (TypeError, ValueError) as err:
        raise EngineeringSnapshotError("node resolution_status is invalid") from err
    sensitive = _require_bool(data["sensitive"], "node sensitive")
    name = _optional_string(data["name"], "node name")
    room = _optional_string(data["room"], "node room")
    io_name = _optional_string(data["io_name"], "node io_name")
    if sensitive and (name is not None or room is not None or io_name is not None or topology_path):
        raise EngineeringSnapshotError("sensitive node contains presentation metadata")
    element = EngineeringElement(
        key=key,
        xml_element="C",
        loxone_type=loxone_type,
        title=name,
        uuid=uuid,
        io_name=io_name,
        parent_uuid=None,
        room_uuid=None,
        room=room,
        category_uuid=None,
        category=None,
        suggested_platform=None,
        attributes=MappingProxyType({}),
        parent_key=parent_key,
    )
    return ResolvedEngineeringNode(
        element=element,
        kind=kind,
        owner_key=owner_key,
        device_identifier=_optional_identifier(
            data["owner_identifier"],
            "node owner_identifier",
        ),
        via_device_identifier=_optional_identifier(
            data["via_identifier"],
            "node via_identifier",
        ),
        bus_kind=_optional_identifier(data["bus_kind"], "node bus_kind"),
        topology_path=topology_path,
        resolution_status=status,
        resolution_reason=_required_reason(
            data["resolution_reason"],
            "node resolution_reason",
        ),
        sensitive=sensitive,
    )


def _safe_unit(value: Any, node: ResolvedEngineeringNode) -> str | None:
    unit = _optional_string(value, "row safe unit")
    if unit is None:
        return None
    from .engineering_entities import normalize_engineering_unit  # noqa: PLC0415

    normalized = normalize_engineering_unit(
        unit,
        title=node.element.title,
        loxone_type=node.element.loxone_type,
    )
    if normalized != unit:
        raise EngineeringSnapshotError("row unit is not canonical or allowlisted")
    return unit


def _optional_platform(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _SEMANTIC_PLATFORMS:
        raise EngineeringSnapshotError(f"{field_name} is invalid")
    return value


def _row_from_dict(
    value: Any,
    nodes_by_key: Mapping[str, ResolvedEngineeringNode],
    *,
    config_version: int,
) -> EngineeringInventoryRow:
    data = _require_dict(value, "rows item", required=_ROW_FIELDS)
    node_key = _required_identifier(data["node_key"], "row node_key")
    try:
        node = nodes_by_key[node_key]
    except KeyError as err:
        raise EngineeringSnapshotError("rows item references a missing node") from err
    try:
        state = CapabilityState(data["capability_state"])
    except (TypeError, ValueError) as err:
        raise EngineeringSnapshotError("row capability state is invalid") from err
    try:
        exposure = ExposureStatus(data["exposure"])
    except (TypeError, ValueError) as err:
        raise EngineeringSnapshotError("row exposure is invalid") from err
    platform = _optional_platform(data["capability_platform"], "row capability platform")
    semantic = _optional_platform(data["semantic_platform"], "row semantic platform")
    capability = EngineeringCapability(
        state,
        platform,
        exposure,
        _required_reason(data["capability_reason"], "row capability reason"),
    )
    has_binding = _require_bool(data["has_binding"], "row has_binding")
    event_proven = _require_bool(
        data["event_binding_proven"],
        "row event_binding_proven",
    )
    binding_method = _optional_identifier(data["binding_method"], "row binding_method")
    value_kind = _optional_identifier(data["value_kind"], "row value_kind")
    state_uuid = _optional_identifier(data["state_uuid"], "row state_uuid")
    safe_unit = _safe_unit(data["safe_unit"], node)
    binding: SafeRuntimeBindingDescriptor | None = None
    if has_binding:
        if binding_method is None or value_kind not in _VALUE_KINDS:
            raise EngineeringSnapshotError("row binding is incomplete")
        if event_proven != (state_uuid is not None and binding_method == "uuid_all"):
            raise EngineeringSnapshotError("row binding proof is inconsistent")
        binding = SafeRuntimeBindingDescriptor(
            binding_method=binding_method,
            value_kind=value_kind,
            safe_unit=safe_unit,
            state_uuid=state_uuid,
            event_binding_proven=event_proven,
        )
    elif any(item is not None for item in (binding_method, value_kind, safe_unit, state_uuid)) or event_proven:
        raise EngineeringSnapshotError("row binding metadata exists without a binding")
    if exposure is ExposureStatus.PREPARED_DISABLED and (
        binding is None or not binding.event_binding_proven or platform is None or semantic != platform
    ):
        raise EngineeringSnapshotError("row prepared exposure lacks a proven binding")
    if node.sensitive and (binding is not None or state is not CapabilityState.SENSITIVE):
        raise EngineeringSnapshotError("sensitive row contains an exposed binding")
    owner = nodes_by_key.get(node.owner_key or "")
    return EngineeringInventoryRow(
        node=node,
        capability=capability,
        semantic_platform=semantic,
        binding=binding,
        owner_name=owner.element.title if owner else None,
        owner_model=owner.element.loxone_type if owner else None,
        config_version=config_version,
    )


def _validate_source(source: EngineeringSourceContext) -> None:
    _required_identifier(source.entry_id, "source entry_id")
    _optional_identifier(source.serial_number, "source serial_number")
    if (
        isinstance(source.config_version, bool)
        or not isinstance(source.config_version, int)
        or source.config_version < 0
    ):
        raise EngineeringSnapshotError("source config_version must be a non-negative integer")
    _normalized_timestamp(source.config_timestamp, "config_timestamp")
    _normalized_revision(source)


def _validate_parent_graph(nodes: tuple[ResolvedEngineeringNode, ...]) -> None:
    nodes_by_key = {node.element.key: node for node in nodes}
    for node in nodes:
        seen = {node.element.key}
        parent_key = node.element.parent_key
        while parent_key is not None:
            if parent_key in seen:
                raise EngineeringSnapshotError("node parent cycle detected")
            parent = nodes_by_key.get(parent_key)
            if parent is None:
                raise EngineeringSnapshotError("node parent is missing")
            seen.add(parent_key)
            parent_key = parent.element.parent_key


def _validate_scope(identifier: str | None, provider: str) -> None:
    if identifier is not None and identifier != provider and not identifier.startswith(f"{provider}:"):
        raise EngineeringSnapshotError("node identifier is outside source scope")


def validate_engineering_snapshot(  # noqa: PLR0912, PLR0915
    snapshot: EngineeringSnapshot,
) -> None:
    """Reject incomplete, mutable-shaped, or internally inconsistent snapshots."""
    if not isinstance(snapshot, EngineeringSnapshot):
        raise EngineeringSnapshotError("snapshot has an invalid type")
    _validate_source(snapshot.source)
    if not isinstance(snapshot.nodes, tuple) or not isinstance(
        snapshot.rows,
        tuple,
    ):
        raise EngineeringSnapshotError("snapshot nodes and rows must be immutable tuples")
    if not snapshot.nodes:
        raise EngineeringSnapshotError("snapshot nodes are empty")
    if len(snapshot.nodes) != len(snapshot.rows):
        raise EngineeringSnapshotError("snapshot rows are incomplete")
    keys = [node.element.key for node in snapshot.nodes]
    if len(keys) != len(set(keys)):
        raise EngineeringSnapshotError("duplicate node key")
    uuids = [node.element.uuid for node in snapshot.nodes if node.element.uuid is not None]
    if len(uuids) != len(set(uuids)):
        raise EngineeringSnapshotError("duplicate stable uuid")
    if not any(node.kind is NodeKind.MINISERVER for node in snapshot.nodes):
        raise EngineeringSnapshotError("snapshot is missing a Miniserver anchor")
    _validate_parent_graph(snapshot.nodes)
    nodes_by_key = {node.element.key: node for node in snapshot.nodes}
    provider = snapshot.source.provider_identifier
    for node in snapshot.nodes:
        # Exercise the exact storage decoder over candidate objects too.
        checked = _node_from_dict(_node_to_dict(node))
        if node.owner_key is not None and node.owner_key not in nodes_by_key:
            raise EngineeringSnapshotError("node owner key is missing")
        _validate_scope(checked.device_identifier, provider)
        _validate_scope(checked.via_device_identifier, provider)
    seen_rows: set[str] = set()
    for node, row in zip(snapshot.nodes, snapshot.rows, strict=True):
        if row.node != node:
            raise EngineeringSnapshotError("snapshot rows do not match nodes")
        if row.config_version != snapshot.source.config_version:
            raise EngineeringSnapshotError("snapshot row config_version does not match source")
        key = row.node.element.key
        if key in seen_rows:
            raise EngineeringSnapshotError("duplicate snapshot row")
        seen_rows.add(key)
        _row_from_dict(
            _row_to_dict(row),
            nodes_by_key,
            config_version=snapshot.source.config_version,
        )
    if seen_rows != set(keys):
        raise EngineeringSnapshotError("snapshot rows are incomplete")
    if (
        isinstance(snapshot.read_sequence, bool)
        or not isinstance(snapshot.read_sequence, int)
        or snapshot.read_sequence < 1
    ):
        raise EngineeringSnapshotError("read_sequence must be a positive integer")
    _normalized_timestamp(snapshot.captured_at, "captured_at")
    _required_digest(
        snapshot.configuration_revision_id,
        "rev",
        "configuration revision",
    )
    _required_digest(snapshot.safe_content_digest, "safe", "safe content digest")
    _required_digest(snapshot.generation_id, "gen", "generation")
    expected_revision = engineering_configuration_revision_id(snapshot.source)
    if snapshot.configuration_revision_id != expected_revision:
        raise EngineeringSnapshotError("configuration revision is inconsistent")
    expected_digest = engineering_safe_content_digest(
        snapshot.source,
        snapshot.nodes,
        snapshot.rows,
    )
    if snapshot.safe_content_digest != expected_digest:
        raise EngineeringSnapshotError("safe content digest is inconsistent")
    expected_generation = engineering_generation_id(
        snapshot.source,
        snapshot.nodes,
        snapshot.rows,
        read_sequence=snapshot.read_sequence,
    )
    if snapshot.generation_id != expected_generation:
        raise EngineeringSnapshotError("generation token is inconsistent")


_SNAPSHOT_FIELDS = frozenset(
    {
        "source",
        "nodes",
        "rows",
        "configuration_revision_id",
        "safe_content_digest",
        "read_sequence",
        "generation_id",
        "captured_at",
    }
)


def snapshot_to_dict(snapshot: EngineeringSnapshot) -> dict[str, Any]:
    """Serialize exactly the private safe reconstruction allowlist."""
    validate_engineering_snapshot(snapshot)
    return {
        "source": _source_to_dict(snapshot.source),
        "nodes": [_node_to_dict(node) for node in snapshot.nodes],
        "rows": [_row_to_dict(row) for row in snapshot.rows],
        "configuration_revision_id": snapshot.configuration_revision_id,
        "safe_content_digest": snapshot.safe_content_digest,
        "read_sequence": snapshot.read_sequence,
        "generation_id": snapshot.generation_id,
        "captured_at": _normalized_timestamp(snapshot.captured_at, "captured_at"),
    }


def snapshot_from_dict(value: Any) -> EngineeringSnapshot:
    """Restore an immutable snapshot only after validating all persisted fields."""
    data = _require_dict(value, "snapshot", required=_SNAPSHOT_FIELDS)
    source = _source_from_dict(data["source"])
    raw_nodes = _require_list(data["nodes"], "snapshot nodes")
    raw_rows = _require_list(data["rows"], "snapshot rows")
    nodes = tuple(_node_from_dict(item) for item in raw_nodes)
    nodes_by_key = {node.element.key: node for node in nodes}
    if len(nodes_by_key) != len(nodes):
        raise EngineeringSnapshotError("duplicate node key")
    rows = tuple(
        _row_from_dict(
            item,
            nodes_by_key,
            config_version=source.config_version,
        )
        for item in raw_rows
    )
    read_sequence = data["read_sequence"]
    if isinstance(read_sequence, bool) or not isinstance(read_sequence, int):
        raise EngineeringSnapshotError("read_sequence must be a positive integer")
    snapshot = EngineeringSnapshot(
        source=source,
        nodes=nodes,
        rows=rows,
        configuration_revision_id=_required_digest(
            data["configuration_revision_id"],
            "rev",
            "configuration revision",
        ),
        safe_content_digest=_required_digest(
            data["safe_content_digest"],
            "safe",
            "safe content digest",
        ),
        read_sequence=read_sequence,
        generation_id=_required_digest(data["generation_id"], "gen", "generation"),
        captured_at=_datetime_from_string(data["captured_at"], "captured_at"),
    )
    validate_engineering_snapshot(snapshot)
    return snapshot


def _impact_to_dict(impact: EngineeringEntityImpact) -> dict[str, Any]:
    return {
        "unique_id": impact.unique_id,
        "entity_ids": list(impact.entity_ids),
        "change_kind": impact.change_kind,
        "references": {key: list(values) for key, values in sorted(impact.references.items())},
    }


def _impact_from_dict(value: Any) -> EngineeringEntityImpact:
    fields = frozenset({"unique_id", "entity_ids", "change_kind", "references"})
    data = _require_dict(value, "impact", required=fields)
    unique_id = _required_identifier(data["unique_id"], "impact unique_id")
    entity_ids = tuple(
        _required_entity_id(item, "impact entity_id") for item in _require_list(data["entity_ids"], "impact entity_ids")
    )
    if len(entity_ids) != len(set(entity_ids)):
        raise EngineeringSnapshotError("impact entity_ids contain duplicates")
    change_kind = data["change_kind"]
    if not isinstance(change_kind, str) or change_kind not in {
        "removed",
        "platform_changed",
    }:
        raise EngineeringSnapshotError("impact change_kind is invalid")
    references_data = _require_dict(
        data["references"],
        "impact references",
        required=frozenset(),
        allowed=frozenset(data["references"]) if isinstance(data["references"], dict) else frozenset(),
    )
    references: dict[str, tuple[str, ...]] = {}
    for key, values in references_data.items():
        reason = _required_reason(key, "impact reference kind")
        references[reason] = tuple(
            _required_entity_id(item, "impact reference") for item in _require_list(values, "impact reference values")
        )
    return EngineeringEntityImpact(unique_id, entity_ids, change_kind, references)


def _required_entity_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _HA_ENTITY_ID_PATTERN.fullmatch(value):
        raise EngineeringSnapshotError(f"{field_name} has an unsafe identifier shape")
    return value


def _impact_plan_to_dict(plan: EngineeringImpactPlan) -> dict[str, Any]:
    return {
        "generation_id": _required_digest(
            plan.generation_id,
            "gen",
            "impact generation",
        ),
        "impacts": [_impact_to_dict(_impact_from_dict(_impact_to_dict(item))) for item in plan.impacts],
    }


def _impact_plan_from_dict(value: Any) -> EngineeringImpactPlan:
    fields = frozenset({"generation_id", "impacts"})
    data = _require_dict(value, "impact plan", required=fields)
    impacts = tuple(_impact_from_dict(item) for item in _require_list(data["impacts"], "impact plan impacts"))
    unique_ids = [item.unique_id for item in impacts]
    if len(unique_ids) != len(set(unique_ids)):
        raise EngineeringSnapshotError("impact plan contains duplicate unique IDs")
    return EngineeringImpactPlan(
        _required_digest(data["generation_id"], "gen", "impact generation"),
        impacts,
    )


_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "snapshot",
        "registry_applied_generation",
        "pending_impact_plan",
        "impact_published_generation",
        "managed_area_ids",
    }
)


def _validate_state(state: StoredEngineeringState, entry_id: str | None = None) -> None:
    snapshot = state.snapshot
    if snapshot is None:
        if (
            state.registry_applied_generation is not None
            or state.pending_impact_plan is not None
            or state.impact_published_generation is not None
            or state.managed_area_ids
        ):
            raise EngineeringSnapshotError("empty state contains recovery metadata")
        return
    validate_engineering_snapshot(snapshot)
    if entry_id is not None and snapshot.source.entry_id != entry_id:
        raise EngineeringSnapshotError("stored snapshot entry does not match store key")
    for token, label in (
        (state.registry_applied_generation, "registry applied generation"),
        (state.impact_published_generation, "impact published generation"),
    ):
        if token is not None:
            _required_digest(token, "gen", label)
    if state.pending_impact_plan is not None:
        _impact_plan_from_dict(_impact_plan_to_dict(state.pending_impact_plan))
        if state.pending_impact_plan.generation_id != snapshot.generation_id:
            raise EngineeringSnapshotError("pending impact generation does not match snapshot")
    provider = snapshot.source.provider_identifier
    for identifier, area_id in state.managed_area_ids.items():
        checked_identifier = _required_identifier(identifier, "managed area owner")
        _validate_scope(checked_identifier, provider)
        _required_identifier(area_id, "managed area id")


def stored_state_to_dict(state: StoredEngineeringState) -> dict[str, Any]:
    """Encode one atomic v2 private state envelope."""
    _validate_state(state)
    return {
        "schema_version": ENGINEERING_SNAPSHOT_STORAGE_VERSION,
        "snapshot": snapshot_to_dict(state.snapshot) if state.snapshot else None,
        "registry_applied_generation": state.registry_applied_generation,
        "pending_impact_plan": (_impact_plan_to_dict(state.pending_impact_plan) if state.pending_impact_plan else None),
        "impact_published_generation": state.impact_published_generation,
        "managed_area_ids": dict(state.managed_area_ids),
    }


def stored_state_from_dict(value: Any, entry_id: str) -> StoredEngineeringState:
    """Decode current or prior direct-private snapshot payloads without publishing."""
    _required_identifier(entry_id, "store entry_id")
    if isinstance(value, dict) and set(value) >= _SNAPSHOT_FIELDS:
        state = StoredEngineeringState(snapshot=snapshot_from_dict(value))
        _validate_state(state, entry_id)
        return state
    data = _require_dict(value, "stored state", required=_STATE_FIELDS)
    if data["schema_version"] != ENGINEERING_SNAPSHOT_STORAGE_VERSION:
        raise EngineeringSnapshotError("stored state schema_version is invalid")
    raw_snapshot = data["snapshot"]
    snapshot = None if raw_snapshot is None else snapshot_from_dict(raw_snapshot)
    raw_areas = _require_dict(
        data["managed_area_ids"],
        "managed_area_ids",
        required=frozenset(),
        allowed=frozenset(data["managed_area_ids"]) if isinstance(data["managed_area_ids"], dict) else frozenset(),
    )
    areas = {
        _required_identifier(key, "managed area owner"): _required_identifier(
            area_id,
            "managed area id",
        )
        for key, area_id in raw_areas.items()
    }
    state = StoredEngineeringState(
        snapshot=snapshot,
        registry_applied_generation=(
            None
            if data["registry_applied_generation"] is None
            else _required_digest(
                data["registry_applied_generation"],
                "gen",
                "registry applied generation",
            )
        ),
        pending_impact_plan=(
            None if data["pending_impact_plan"] is None else _impact_plan_from_dict(data["pending_impact_plan"])
        ),
        impact_published_generation=(
            None
            if data["impact_published_generation"] is None
            else _required_digest(
                data["impact_published_generation"],
                "gen",
                "impact published generation",
            )
        ),
        managed_area_ids=areas,
    )
    _validate_state(state, entry_id)
    return state


class EngineeringStateStore(Store[dict[str, Any]]):
    """Versioned Home Assistant store with a validating v1-to-v2 migration."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: Any,
    ) -> dict[str, Any]:
        del old_minor_version
        if old_major_version != 1:
            raise NotImplementedError
        entry_id = self.key.removeprefix(f"{ENGINEERING_SNAPSHOT_STORAGE_KEY}.")
        return stored_state_to_dict(stored_state_from_dict(old_data, entry_id))


async def async_load_engineering_state(
    hass: HomeAssistant,
    entry_id: str,
) -> StoredEngineeringState:
    """Load and validate one entry's private engineering recovery envelope."""
    _required_identifier(entry_id, "store entry_id")
    store: EngineeringStateStore = EngineeringStateStore(
        hass,
        ENGINEERING_SNAPSHOT_STORAGE_VERSION,
        f"{ENGINEERING_SNAPSHOT_STORAGE_KEY}.{entry_id}",
        private=True,
    )
    stored = await store.async_load()
    if stored is None:
        return StoredEngineeringState(snapshot=None)
    return stored_state_from_dict(stored, entry_id)


async def async_store_engineering_state(
    hass: HomeAssistant,
    state: StoredEngineeringState,
) -> None:
    """Atomically save a validated state under its source config entry."""
    if state.snapshot is None:
        raise EngineeringSnapshotError("cannot derive store key from an empty state")
    _validate_state(state, state.snapshot.source.entry_id)
    store: EngineeringStateStore = EngineeringStateStore(
        hass,
        ENGINEERING_SNAPSHOT_STORAGE_VERSION,
        f"{ENGINEERING_SNAPSHOT_STORAGE_KEY}.{state.snapshot.source.entry_id}",
        private=True,
    )
    await store.async_save(stored_state_to_dict(state))
