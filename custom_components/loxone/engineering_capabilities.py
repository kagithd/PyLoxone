"""Conservative capability classification for engineering runtime reads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .engineering_runtime import EngineeringRuntimeBinding, EngineeringRuntimeInventory
    from .engineering_topology import ResolvedEngineeringInventory, ResolvedEngineeringNode

SENSOR_TYPES = frozenset({"voltagein", "analoginput", "weatherdata", "sysvar"})
BINARY_TYPES = frozenset({"digitalin", "digitalinput", "online", "status", "devicestatus", "deviceonline"})
OUTPUT_TYPES = frozenset({"actor", "analogout"})
PROBE_TYPES = SENSOR_TYPES | BINARY_TYPES | OUTPUT_TYPES
SENSITIVE_TYPES = frozenset(
    {"accesscode", "credential", "keycode", "nfccode", "nfctag", "password", "permission", "user"}
)


class CapabilityState(StrEnum):
    """Read/write evidence states; this module never grants write access."""

    READABLE = "readable"
    WRITABLE = "writable"
    READ_WRITE = "read_write"
    CONFIGURED_ONLY = "configured_only"
    UNSUPPORTED = "unsupported"
    SENSITIVE = "sensitive"


class ExposureStatus(StrEnum):
    """Safe exposure decision independent from semantic platform."""

    PREPARED_DISABLED = "prepared_disabled"
    INVENTORY_ONLY = "inventory_only"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class EngineeringCapability:
    """Capability and exposure result for one engineering node."""

    state: CapabilityState
    platform: Literal["sensor", "binary_sensor"] | None
    exposure: ExposureStatus
    reason: str


@dataclass(frozen=True, slots=True)
class SafeRuntimeBindingDescriptor:
    """Safe, reconstructable metadata for an explicitly proven binding."""

    binding_method: str
    value_kind: Literal["number", "boolean"]
    safe_unit: str | None
    state_uuid: str | None
    event_binding_proven: bool


@dataclass(frozen=True, slots=True)
class EngineeringInventoryRow:
    """One topology row joined with a safe capability result."""

    node: ResolvedEngineeringNode
    capability: EngineeringCapability
    semantic_platform: Literal["sensor", "binary_sensor"] | None
    binding: SafeRuntimeBindingDescriptor | None


def _type(node: ResolvedEngineeringNode) -> str:
    return (node.element.loxone_type or "").casefold()


def _is_sensitive(node: ResolvedEngineeringNode) -> bool:
    type_value = _type(node)
    return (
        node.sensitive
        or type_value in SENSITIVE_TYPES
        or type_value.startswith(("access", "keycode", "nfccode", "nfctag", "permission", "user"))
    )


def select_runtime_probe_candidates(resolved: ResolvedEngineeringInventory) -> tuple[ResolvedEngineeringNode, ...]:
    """Return only UUID-addressable, non-sensitive types with a read contract."""
    return tuple(
        node
        for node in resolved.nodes
        if node.element.uuid and node.element.io_name and not _is_sensitive(node) and _type(node) in PROBE_TYPES
    )


def _descriptor(binding: EngineeringRuntimeBinding | None) -> SafeRuntimeBindingDescriptor | None:
    if binding is None or binding.status != "bound" or binding.value_kind not in {"number", "boolean"}:
        return None
    if binding.numeric_value is None or not math.isfinite(binding.numeric_value):
        return None
    return SafeRuntimeBindingDescriptor(
        binding_method=binding.binding_method or "read_only",
        value_kind="boolean" if binding.value_kind == "boolean" else "number",
        safe_unit=binding.unit,
        state_uuid=binding.state_uuid,
        event_binding_proven=bool(binding.state_uuid and binding.binding_method == "uuid_all"),
    )


def resolve_capability(  # noqa: PLR0911
    node: ResolvedEngineeringNode, binding: EngineeringRuntimeBinding | None
) -> EngineeringCapability:
    """Classify a read result without granting any write capability."""
    type_value = _type(node)
    if _is_sensitive(node):
        return EngineeringCapability(
            CapabilityState.SENSITIVE, None, ExposureStatus.SUPPRESSED, "sensitive_metadata_suppressed"
        )
    if binding is not None and binding.status == "auth_error":
        return EngineeringCapability(
            CapabilityState.CONFIGURED_ONLY, None, ExposureStatus.INVENTORY_ONLY, "runtime_auth_failure"
        )
    if binding is not None and binding.status in {"transport_error", "error"}:
        return EngineeringCapability(
            CapabilityState.CONFIGURED_ONLY, None, ExposureStatus.INVENTORY_ONLY, "runtime_transport_failure"
        )
    if type_value not in PROBE_TYPES:
        return EngineeringCapability(
            CapabilityState.UNSUPPORTED, None, ExposureStatus.INVENTORY_ONLY, "unsupported_technical_type"
        )
    if binding is None or binding.status != "bound":
        return EngineeringCapability(
            CapabilityState.CONFIGURED_ONLY, None, ExposureStatus.INVENTORY_ONLY, "runtime_binding_not_proven"
        )
    if (
        binding.value_kind not in {"number", "boolean"}
        or binding.numeric_value is None
        or not math.isfinite(binding.numeric_value)
    ):
        return EngineeringCapability(
            CapabilityState.UNSUPPORTED, None, ExposureStatus.SUPPRESSED, "non_numeric_runtime_value"
        )
    if type_value in OUTPUT_TYPES:
        return EngineeringCapability(
            CapabilityState.READABLE, None, ExposureStatus.INVENTORY_ONLY, "output_write_contract_not_enabled"
        )
    platform: Literal["sensor", "binary_sensor"]
    if type_value in BINARY_TYPES:
        if binding.numeric_value not in {0.0, 1.0}:
            return EngineeringCapability(
                CapabilityState.UNSUPPORTED, None, ExposureStatus.INVENTORY_ONLY, "binary_semantics_not_proven"
            )
        platform = "binary_sensor"
    else:
        platform = "sensor"
    descriptor = _descriptor(binding)
    if descriptor is None or not descriptor.event_binding_proven:
        return EngineeringCapability(
            CapabilityState.READABLE, platform, ExposureStatus.INVENTORY_ONLY, "readable_rebind_only"
        )
    return EngineeringCapability(
        CapabilityState.READABLE, platform, ExposureStatus.PREPARED_DISABLED, "read_only_event_binding_proven"
    )


def resolve_engineering_capabilities(
    resolved: ResolvedEngineeringInventory, runtime: EngineeringRuntimeInventory | None
) -> tuple[EngineeringInventoryRow, ...]:
    """Join immutable topology and runtime results without endpoint data."""
    bindings = {} if runtime is None else {item.engineering_uuid: item for item in runtime.bindings}
    rows: list[EngineeringInventoryRow] = []
    for node in resolved.nodes:
        binding = bindings.get(node.element.uuid or "")
        capability = resolve_capability(node, binding)
        semantic = capability.platform
        rows.append(EngineeringInventoryRow(node, capability, semantic, _descriptor(binding)))
    return tuple(rows)
