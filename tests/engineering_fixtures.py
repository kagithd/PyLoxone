"""Safe, synthetic data helpers for engineering configuration tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any

from custom_components.loxone.engineering_capabilities import resolve_engineering_capabilities
from custom_components.loxone.engineering_config import EngineeringElement, EngineeringInventory
from custom_components.loxone.engineering_runtime import (
    EngineeringRuntimeBinding,
    EngineeringRuntimeInventory,
)
from custom_components.loxone.engineering_snapshot import (
    EngineeringSnapshot,
    engineering_configuration_revision_id,
    engineering_generation_id,
    engineering_safe_content_digest,
)
from custom_components.loxone.engineering_topology import (
    EngineeringSourceContext,
    ResolvedEngineeringInventory,
    resolve_engineering_topology,
)

SYNTHETIC_PARSE_CONTEXT = {
    "source_archive": "sps_7_20260913120000.zip",
    "config_version": 7,
    "config_timestamp": datetime(2026, 9, 13, 12, tzinfo=UTC),
}

UUIDLESS_CONTAINER_XML = b"""<?xml version="1.0"?>
<ControlList>
  <C Type="LoxLIVE" U="miniserver-uuid" Title="Miniserver" />
  <C Type="Page" Title="Private caption">
    <C Type="TreeDevice" U="tree-device-uuid" Title="ST-F07" />
  </C>
</ControlList>"""

DUPLICATE_UUID_XML = b"""<?xml version="1.0"?>
<ControlList>
  <C Type="LoxLIVE" U="duplicate-uuid" Title="Miniserver" />
  <C Type="TreeDevice" U="duplicate-uuid" Title="ST-F01" />
</ControlList>"""

_ALLOWED_FIXTURE_FIELDS = frozenset(
    {
        "device",
        "element_type",
        "io_name",
        "key",
        "parent_key",
        "parent_uuid",
        "platform",
        "room",
        "title",
        "uuid",
    }
)
_FIXTURE_ERROR = "forbidden fixture field"
_PRESENTATION_FIELDS = frozenset({"room", "title"})
_OPAQUE_KEY = re.compile(r"^xml:(?:\d{6}|fixture)$")
_TECHNICAL_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_URL_CONTENT = re.compile(r"\b(?:https?|ftp)://", re.IGNORECASE)
_ADDRESS_CONTENT = re.compile(r"[0-9A-Fa-f:.]+")


def _raise_fixture_error() -> None:
    raise ValueError(_FIXTURE_ERROR)


def _contains_network_material(value: str) -> bool:
    """Return whether a presentation string embeds an IP address or URL."""
    if _URL_CONTENT.search(value):
        return True
    for candidate in _ADDRESS_CONTENT.findall(value):
        try:
            ip_address(candidate)
        except ValueError:
            continue
        return True
    return False


def _validate_fixture_scalar(field: str, value: Any) -> None:
    """Validate a scalar according to its presentation or technical field role."""
    if value is None:
        return
    if not isinstance(value, str):
        _raise_fixture_error()
    if field not in _PRESENTATION_FIELDS:
        if _OPAQUE_KEY.fullmatch(value) or _TECHNICAL_TOKEN.fullmatch(value):
            return
        _raise_fixture_error()
    if not _contains_network_material(value):
        return
    _raise_fixture_error()


def validate_fixture_input(value: Any) -> Any:
    """Accept only explicitly allowlisted safe scalar fixture fields."""
    if not isinstance(value, Mapping):
        _raise_fixture_error()
    for key, item in value.items():
        if not isinstance(key, str) or key not in _ALLOWED_FIXTURE_FIELDS:
            _raise_fixture_error()
        if isinstance(item, Mapping):
            validate_fixture_input(item)
            _raise_fixture_error()
        if isinstance(item, (list, tuple, set, frozenset)):
            for nested_item in item:
                _validate_fixture_scalar(key, nested_item)
            _raise_fixture_error()
        _validate_fixture_scalar(key, item)
    return value


def element(
    uuid: str | None,
    element_type: str,
    *,
    parent_uuid: str | None = None,
    key: str | None = None,
    parent_key: str | None = None,
    title: str | None = None,
    io_name: str | None = None,
    room: str | None = "Office",
    platform: str | None = None,
) -> EngineeringElement:
    """Create one validated, synthetic engineering element."""
    validate_fixture_input(
        {
            "uuid": uuid,
            "element_type": element_type,
            "parent_uuid": parent_uuid,
            "key": key,
            "parent_key": parent_key,
            "title": title,
            "io_name": io_name,
            "room": room,
            "platform": platform,
        }
    )
    return EngineeringElement(
        key=key or uuid or "xml:fixture",
        xml_element="C",
        loxone_type=element_type,
        title=title or element_type,
        uuid=uuid,
        io_name=io_name,
        parent_uuid=parent_uuid,
        parent_key=parent_key or parent_uuid,
        room_uuid="room-uuid" if room else None,
        room=room,
        category_uuid=None,
        category=None,
        suggested_platform=platform,
        attributes={},
    )


def inventory_of(*elements: EngineeringElement) -> EngineeringInventory:
    """Build an inventory with a deterministic synthetic source revision."""
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    return EngineeringInventory("sps_7_20260913120000.zip", 7, now, now, 10, elements)


def source(entry_id: str = "entry-a", serial: str = "serial-a") -> EngineeringSourceContext:
    """Build an immutable synthetic source context."""
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    return EngineeringSourceContext(
        entry_id=entry_id,
        serial_number=serial,
        title="Miniserver",
        model="Miniserver",
        source_archive="sps_7_20260913120000.zip",
        config_version=7,
        config_timestamp=now,
        loxapp_last_modified="revision-7",
    )


def node(resolved: ResolvedEngineeringInventory, uuid: str):
    """Return the resolved node with a stable engineering UUID."""
    return next(item for item in resolved.nodes if item.element.uuid == uuid)


def reference_link_inventory() -> EngineeringInventory:
    """Build a Link graph with Air and 1-Wire endpoint chains."""
    return inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("link", "LoxLink", parent_uuid="ms", title="Link", room=None),
        element("air-extension", "AirBaseExtension", parent_uuid="link", title="Air bridge"),
        element("air-device", "AirDevice", parent_uuid="air-extension", title="ST-F01"),
        element("wire-extension", "Lox1WireExtension", parent_uuid="link", title="Wire extension"),
        element("wire-sensor", "Lox1wireDevice", parent_uuid="wire-extension", title="ST-F02"),
    )


def provider_inventory() -> EngineeringInventory:
    """Build internal I/O and document-level provider services."""
    return inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("io", "IoData", parent_uuid="ms", room=None),
        element("digital-i1", "DigitalIn", parent_uuid="io", io_name="I1"),
        element("analog-ai1", "VoltageIn", parent_uuid="io", io_name="AI1"),
        element("relay-q1", "Actor", parent_uuid="io", io_name="Q1"),
        element("weather-server", "WeatherServer", room=None),
        element("weather-value", "WeatherData", parent_uuid="weather-server", io_name="WDC1"),
        element("global-states", "GlobalStates", room=None),
        element("system-variable", "SysVar", parent_uuid="global-states", io_name="SYS1"),
    )


def make_snapshot(
    *,
    last_modified: str | None = "revision-7",
    inventory: EngineeringInventory | None = None,
    runtime: EngineeringRuntimeInventory | None = None,
    read_sequence: int = 1,
) -> EngineeringSnapshot:
    """Build a complete deterministic sanitized engineering snapshot."""
    raw = inventory or provider_inventory()
    context = replace(source(), loxapp_last_modified=last_modified)
    resolved = resolve_engineering_topology(raw, context)
    default_runtime = EngineeringRuntimeInventory(
        bindings=(
            numeric_binding("weather-value", 18.5, "WeatherData"),
            numeric_binding("system-variable", 1.0, "SysVar"),
        )
    )
    rows = resolve_engineering_capabilities(resolved, runtime or default_runtime)
    return EngineeringSnapshot(
        source=context,
        nodes=resolved.nodes,
        rows=rows,
        configuration_revision_id=engineering_configuration_revision_id(context),
        safe_content_digest=engineering_safe_content_digest(context, resolved.nodes, rows),
        read_sequence=read_sequence,
        generation_id=engineering_generation_id(
            context,
            resolved.nodes,
            rows,
            read_sequence=read_sequence,
        ),
        captured_at=datetime(2026, 9, 13, 12, tzinfo=UTC),
    )


def cyclic_inventory() -> EngineeringInventory:
    """Build a cycle that cannot be resolved to a physical owner."""
    return inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("cycle-a", "TreeDevice", parent_uuid="cycle-b"),
        element("cycle-b", "TreeCaption", parent_uuid="cycle-a"),
    )


def numeric_binding(uuid: str, value: float, element_type: str = "VoltageIn") -> EngineeringRuntimeBinding:
    """Create a synthetic explicitly event-mapped numeric runtime binding."""
    return EngineeringRuntimeBinding(
        engineering_uuid=uuid,
        io_name="AI1",
        loxone_type=element_type,
        title=element_type,
        room="Office",
        suggested_platform=None,
        status="bound",
        binding_method="uuid_all",
        value_kind="number",
        numeric_value=value,
        state_uuid=f"{uuid}-state",
    )


def text_binding(uuid: str = "text", element_type: str = "SysVar") -> EngineeringRuntimeBinding:
    """Create a synthetic arbitrary-text binding which must never be exposed."""
    return EngineeringRuntimeBinding(
        engineering_uuid=uuid,
        io_name="SYS1",
        loxone_type=element_type,
        title=element_type,
        room="Office",
        suggested_platform=None,
        status="bound",
        binding_method="uuid_state",
        value_kind="text",
    )


def resolved_node(uuid: str, element_type: str, *, io_name: str = "AI1"):
    """Create a resolved channel without relying on a legacy platform hint."""
    from custom_components.loxone.engineering_topology import (
        NodeKind,
        ResolutionStatus,
        ResolvedEngineeringNode,
    )

    return ResolvedEngineeringNode(
        element=element(uuid, element_type, io_name=io_name),
        kind=NodeKind.CHANNEL,
        owner_key="ms",
        device_identifier="serial-a",
        via_device_identifier=None,
        bus_kind=None,
        topology_path=("Miniserver", element_type),
        resolution_status=ResolutionStatus.RESOLVED,
        resolution_reason="internal_miniserver_channel",
    )
