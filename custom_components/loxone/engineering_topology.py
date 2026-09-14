"""Type-driven, source-scoped engineering topology primitives."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .engineering_config import EngineeringElement, EngineeringInventory


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


PLACEMENT_NODE_KINDS = frozenset({NodeKind.MINISERVER, NodeKind.BUS, NodeKind.BRIDGE, NodeKind.PHYSICAL_DEVICE})


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
        "digitalin",
        "digitalinput",
        "voltagein",
        "analoginput",
        "actor",
        "analogout",
        "status",
        "online",
        "devicestatus",
        "deviceonline",
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
_SENSITIVE_TYPES = frozenset(
    {
        "accesscode",
        "credential",
        "keycode",
        "nfccode",
        "password",
        "permission",
        "user",
    }
)
_SENSITIVE_PREFIXES = ("access", "keycode", "nfccode", "nfctag", "permission", "user")
_PHYSICAL_TYPES = frozenset({"nfccodetouch"})
_TYPED_SERVICE_OWNERS = {"weatherdata": "weatherserver", "sysvar": "globalstates"}


def is_sensitive_engineering_role(value: str | None, *, technical_type: bool = False) -> bool:
    """Apply the one authoritative policy for privacy-sensitive XML roles."""
    normalized = (value or "").casefold()
    if technical_type and normalized in _PHYSICAL_TYPES:
        return False
    return normalized in _SENSITIVE_TYPES or normalized.startswith(_SENSITIVE_PREFIXES)


def is_sensitive_engineering_element(element: EngineeringElement) -> bool:
    """Return whether either the XML role or technical type is sensitive."""
    return is_sensitive_engineering_role(element.xml_element) or is_sensitive_engineering_role(
        element.loxone_type,
        technical_type=True,
    )


def effective_engineering_technical_type(element: EngineeringElement) -> str | None:
    """Preserve an XML sensitivity role ahead of any unrelated Type attribute."""
    if is_sensitive_engineering_role(element.xml_element):
        return element.xml_element
    if element.loxone_type:
        return element.loxone_type
    return element.xml_element if element.xml_element.casefold() != "c" else None


def classify_node_kind(element: EngineeringElement) -> NodeKind:
    """Classify only from the effective technical role, never presentation text."""
    normalized_type = (effective_engineering_technical_type(element) or "").casefold()
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
    if normalized_type in _PHYSICAL_TYPES:
        return NodeKind.PHYSICAL_DEVICE
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


class OwnerResolver:
    """Resolve technical ownership from opaque parser keys and type maps."""

    def __init__(self, max_depth: int = 128) -> None:
        self._max_depth = max_depth

    def resolve(
        self,
        inventory: EngineeringInventory,
        source: EngineeringSourceContext,
    ) -> ResolvedEngineeringInventory:
        """Resolve every parsed node without using presentation data as identity."""
        elements_by_key = {item.key: item for item in inventory.elements}
        kinds = {item.key: classify_node_kind(item) for item in inventory.elements}
        service_types = {
            (item.loxone_type or "").casefold()
            for item in inventory.elements
            if kinds[item.key] is NodeKind.SERVICE_MODULE
        }
        uuidless_service_counts = {
            item_type: sum(
                1
                for item in inventory.elements
                if kinds[item.key] is NodeKind.SERVICE_MODULE and (item.loxone_type or "").casefold() == item_type
            )
            for item_type in service_types
        }
        nodes = tuple(
            self._resolve_one(
                item,
                elements_by_key,
                kinds,
                source,
                uuidless_service_counts,
            )
            for item in inventory.elements
        )
        return ResolvedEngineeringInventory(source=source, nodes=nodes)

    def _resolve_one(
        self,
        item: EngineeringElement,
        elements_by_key: dict[str, EngineeringElement],
        kinds: dict[str, NodeKind],
        source: EngineeringSourceContext,
        uuidless_service_counts: dict[str, int],
    ) -> ResolvedEngineeringNode:
        chain, failure = self._ancestry(item, elements_by_key)
        kind = kinds[item.key]
        sensitive = failure is not None or any(self._is_sensitive(ancestor) for ancestor in chain)
        public_item = self._sanitize(item) if sensitive else item
        if kind not in PLACEMENT_NODE_KINDS:
            public_item = replace(public_item, placement=None)
        path = () if sensitive else tuple(self._presentation_name(ancestor) for ancestor in reversed(chain))
        if failure is not None:
            return self._unresolved(public_item, kind, path, failure, sensitive)

        if kind is NodeKind.MINISERVER:
            return self._resolved(
                public_item, kind, item.key, source.provider_identifier, None, None, path, "miniserver", sensitive
            )
        if kind is NodeKind.STRUCTURAL:
            return self._resolved(
                public_item, kind, None, None, None, self._nearest_bus(chain, kinds), path, "structural", sensitive
            )
        if kind is NodeKind.SERVICE_MODULE:
            return self._resolve_service(item, public_item, path, source, uuidless_service_counts, sensitive)

        identifier = scoped_engineering_identifier(source, item)
        if kind in {NodeKind.BUS, NodeKind.BRIDGE, NodeKind.PHYSICAL_DEVICE}:
            if identifier is None:
                return self._unresolved(public_item, kind, path, "missing_stable_identifier", sensitive)
            via = self._nearest_registered_ancestor(chain[1:], kinds, source)
            return self._resolved(
                public_item,
                kind,
                item.key,
                identifier,
                via,
                self._nearest_bus(chain, kinds),
                path,
                "physical_owner",
                sensitive,
            )

        return self._resolve_channel(
            item,
            public_item,
            chain,
            kinds,
            source,
            path,
            sensitive,
            uuidless_service_counts,
        )

    def _resolve_service(
        self,
        item: EngineeringElement,
        public_item: EngineeringElement,
        path: tuple[str, ...],
        source: EngineeringSourceContext,
        uuidless_service_counts: dict[str, int],
        sensitive: bool,
    ) -> ResolvedEngineeringNode:
        normalized_type = (item.loxone_type or "").casefold()
        if item.uuid is None:
            if uuidless_service_counts[normalized_type] != 1:
                return self._unresolved(
                    public_item, NodeKind.SERVICE_MODULE, path, "ambiguous_uuidless_service", sensitive
                )
            identifier = f"{source.provider_identifier}:service:{normalized_type}"
        else:
            identifier = f"{source.provider_identifier}:{item.uuid}"
        return self._resolved(
            public_item,
            NodeKind.SERVICE_MODULE,
            item.key,
            identifier,
            source.provider_identifier,
            None,
            path,
            "provider_service",
            sensitive,
        )

    def _resolve_channel(
        self,
        item: EngineeringElement,
        public_item: EngineeringElement,
        chain: tuple[EngineeringElement, ...],
        kinds: dict[str, NodeKind],
        source: EngineeringSourceContext,
        path: tuple[str, ...],
        sensitive: bool,
        uuidless_service_counts: dict[str, int],
    ) -> ResolvedEngineeringNode:
        expected_service = _TYPED_SERVICE_OWNERS.get((item.loxone_type or "").casefold())
        for ancestor in chain[1:]:
            ancestor_kind = kinds[ancestor.key]
            ancestor_type = (ancestor.loxone_type or "").casefold()
            if expected_service is not None:
                if ancestor_kind is NodeKind.SERVICE_MODULE and ancestor_type == expected_service:
                    identifier = self._service_identifier(ancestor, source, uuidless_service_counts)
                    if identifier is not None:
                        return self._resolved(
                            public_item,
                            NodeKind.CHANNEL,
                            ancestor.key,
                            identifier,
                            None,
                            self._nearest_bus(chain, kinds),
                            path,
                            "service_channel",
                            sensitive,
                        )
                    return self._unresolved(
                        public_item,
                        NodeKind.CHANNEL,
                        path,
                        "ambiguous_uuidless_service",
                        sensitive,
                    )
                continue
            if ancestor_kind in {NodeKind.PHYSICAL_DEVICE, NodeKind.BRIDGE}:
                identifier = scoped_engineering_identifier(source, ancestor)
                if identifier is not None:
                    return self._resolved(
                        public_item,
                        NodeKind.CHANNEL,
                        ancestor.key,
                        identifier,
                        None,
                        self._nearest_bus(chain, kinds),
                        path,
                        "physical_channel",
                        sensitive,
                    )
            if ancestor_kind is NodeKind.SERVICE_MODULE:
                identifier = self._service_identifier(ancestor, source, uuidless_service_counts)
                if identifier is not None:
                    return self._resolved(
                        public_item,
                        NodeKind.CHANNEL,
                        ancestor.key,
                        identifier,
                        None,
                        self._nearest_bus(chain, kinds),
                        path,
                        "service_channel",
                        sensitive,
                    )
                if ancestor.uuid is None:
                    return self._unresolved(
                        public_item,
                        NodeKind.CHANNEL,
                        path,
                        "ambiguous_uuidless_service",
                        sensitive,
                    )
            if ancestor_kind is NodeKind.MINISERVER:
                return self._resolved(
                    public_item,
                    NodeKind.CHANNEL,
                    ancestor.key,
                    source.provider_identifier,
                    None,
                    None,
                    path,
                    "internal_miniserver_channel",
                    sensitive,
                )
        return self._unresolved(public_item, NodeKind.CHANNEL, path, "missing_owner", sensitive)

    def _ancestry(
        self, item: EngineeringElement, elements_by_key: dict[str, EngineeringElement]
    ) -> tuple[tuple[EngineeringElement, ...], str | None]:
        chain = [item]
        seen = {item.key}
        current = item
        for _ in range(self._max_depth):
            parent_key = current.parent_key
            if parent_key is None:
                return tuple(chain), None
            if parent_key in seen:
                return tuple(chain), "parent_cycle"
            parent = elements_by_key.get(parent_key)
            if parent is None:
                return tuple(chain), "missing_parent"
            chain.append(parent)
            seen.add(parent_key)
            current = parent
        if current.parent_key is not None:
            return tuple(chain), "parent_depth_exceeded"
        return tuple(chain), None

    @staticmethod
    def _nearest_bus(chain: tuple[EngineeringElement, ...], kinds: dict[str, NodeKind]) -> str | None:
        for ancestor in chain:
            if kinds[ancestor.key] is NodeKind.BUS:
                return (ancestor.loxone_type or "").casefold().removeprefix("lox")
        return None

    @staticmethod
    def _nearest_registered_ancestor(
        ancestors: tuple[EngineeringElement, ...],
        kinds: dict[str, NodeKind],
        source: EngineeringSourceContext,
    ) -> str | None:
        for ancestor in ancestors:
            if kinds[ancestor.key] in {NodeKind.BUS, NodeKind.BRIDGE, NodeKind.PHYSICAL_DEVICE, NodeKind.MINISERVER}:
                if kinds[ancestor.key] is NodeKind.MINISERVER:
                    return source.provider_identifier
                if identifier := scoped_engineering_identifier(source, ancestor):
                    return identifier
        return None

    @staticmethod
    def _service_identifier(
        item: EngineeringElement,
        source: EngineeringSourceContext,
        uuidless_service_counts: dict[str, int],
    ) -> str | None:
        if item.uuid is not None:
            return f"{source.provider_identifier}:{item.uuid}"
        normalized_type = (item.loxone_type or "").casefold()
        if uuidless_service_counts.get(normalized_type) == 1:
            return f"{source.provider_identifier}:service:{normalized_type}"
        return None

    @staticmethod
    def _presentation_name(item: EngineeringElement) -> str:
        return item.title or item.loxone_type or item.xml_element

    @staticmethod
    def _is_sensitive(item: EngineeringElement) -> bool:
        return is_sensitive_engineering_element(item)

    @staticmethod
    def _sanitize(item: EngineeringElement) -> EngineeringElement:
        return replace(
            item,
            loxone_type=effective_engineering_technical_type(item),
            title=None,
            io_name=None,
            room=None,
            category=None,
            attributes={},
            placement=None,
        )

    @staticmethod
    def _resolved(
        item: EngineeringElement,
        kind: NodeKind,
        owner_key: str | None,
        identifier: str | None,
        via: str | None,
        bus_kind: str | None,
        path: tuple[str, ...],
        reason: str,
        sensitive: bool,
    ) -> ResolvedEngineeringNode:
        return ResolvedEngineeringNode(
            item, kind, owner_key, identifier, via, bus_kind, path, ResolutionStatus.RESOLVED, reason, sensitive
        )

    @staticmethod
    def _unresolved(
        item: EngineeringElement,
        kind: NodeKind,
        path: tuple[str, ...],
        reason: str,
        sensitive: bool,
    ) -> ResolvedEngineeringNode:
        return ResolvedEngineeringNode(
            item, kind, None, None, None, None, path, ResolutionStatus.UNRESOLVED, reason, sensitive
        )


def resolve_engineering_topology(
    inventory: EngineeringInventory, source: EngineeringSourceContext
) -> ResolvedEngineeringInventory:
    """Resolve a parsed inventory through the bounded opaque-key owner graph."""
    return OwnerResolver().resolve(inventory, source)
