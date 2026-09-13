"""Type-driven, source-scoped engineering topology primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .engineering_config import EngineeringElement


class NodeKind(StrEnum):
    """Safe categories used when resolving engineering topology."""

    MINISERVER = "miniserver"
    BUS = "bus"
    BRIDGE = "bridge"
    PHYSICAL_DEVICE = "physical_device"
    SERVICE_MODULE = "service_module"
    CHANNEL = "channel"
    STRUCTURAL = "structural"


class ResolutionStatus(StrEnum):
    """Whether an ownership decision was proven from technical metadata."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class EngineeringSourceContext:
    """Immutable identity and revision metadata for one engineering source."""

    entry_id: str
    serial_number: str | None
    title: str | None
    model: str | None
    source_archive: str
    config_version: int
    config_timestamp: datetime
    loxapp_last_modified: str | None

    @property
    def provider_identifier(self) -> str:
        """Return the source identity without relying on presentation data."""
        return self.serial_number or self.entry_id


@dataclass(frozen=True, slots=True)
class ResolvedEngineeringNode:
    """One classified engineering node with its later resolver output."""

    element: EngineeringElement
    kind: NodeKind
    owner_key: str | None
    device_identifier: str | None
    via_device_identifier: str | None
    bus_kind: str | None
    topology_path: tuple[str, ...]
    resolution_status: ResolutionStatus
    resolution_reason: str
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedEngineeringInventory:
    """Immutable resolved graph for a single engineering source."""

    source: EngineeringSourceContext
    nodes: tuple[ResolvedEngineeringNode, ...]

    @property
    def nodes_by_key(self) -> dict[str, ResolvedEngineeringNode]:
        """Index nodes by parser-assigned opaque or stable graph key."""
        return {node.element.key: node for node in self.nodes}


_MINISERVER_TYPES = frozenset({"loxlive"})
_BUS_TYPES = frozenset({"loxlink", "loxtree"})
_BRIDGE_TYPES = frozenset(
    {
        "loxair",
        "airbaseextension",
        "onewireextension",
        "loxonewireextension",
        "lox1wireextension",
    }
)
_SERVICE_TYPES = frozenset(
    {
        "weatherserver",
        "globalstates",
        "operatingmodes",
        "timefunctions",
        "systemstatus",
        "devicemonitor",
        "networkplugin",
        "virtualinputs",
        "virtualoutputs",
        "tasks",
        "messages",
        "intercom",
        "lightinggroups",
    }
)
_CHANNEL_TYPES = frozenset(
    {
        "weatherdata",
        "sysvar",
        "digitalinput",
        "analoginput",
        "status",
        "online",
    }
)
_STRUCTURAL_TYPES = frozenset(
    {
        "document",
        "page",
        "place",
        "category",
        "user",
        "iodata",
        "co",
        "inputref",
        "outputref",
        "permission",
        "reference",
        "caption",
    }
)


def classify_node_kind(element: EngineeringElement) -> NodeKind:
    """Classify only from the technical type, never editable presentation text."""
    normalized_type = (element.loxone_type or "").casefold()
    if normalized_type in _MINISERVER_TYPES:
        return NodeKind.MINISERVER
    if normalized_type in _BUS_TYPES:
        return NodeKind.BUS
    if normalized_type in _BRIDGE_TYPES:
        return NodeKind.BRIDGE
    if normalized_type in _SERVICE_TYPES:
        return NodeKind.SERVICE_MODULE
    if normalized_type in _CHANNEL_TYPES:
        return NodeKind.CHANNEL
    if normalized_type in _STRUCTURAL_TYPES or normalized_type.endswith(("caption", "ref")):
        return NodeKind.STRUCTURAL
    if normalized_type.endswith(("device", "dev", "extension")):
        return NodeKind.PHYSICAL_DEVICE
    return NodeKind.STRUCTURAL


def scoped_engineering_identifier(source: EngineeringSourceContext, element: EngineeringElement) -> str | None:
    """Return a source-scoped identity only for UUID-backed graph nodes."""
    if element.uuid is None:
        return None
    if classify_node_kind(element) is NodeKind.STRUCTURAL:
        return None
    return f"{source.provider_identifier}:{element.uuid}"
