"""Safe, synthetic data helpers for engineering configuration tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from custom_components.loxone.engineering_config import EngineeringElement, EngineeringInventory
from custom_components.loxone.engineering_topology import (
    EngineeringSourceContext,
    ResolvedEngineeringInventory,
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


def _raise_fixture_error() -> None:
    raise ValueError(_FIXTURE_ERROR)


def _validate_fixture_scalar(value: Any) -> None:
    """Allow only safe presentation or technical scalar values."""
    if value is None:
        return
    if not isinstance(value, str):
        _raise_fixture_error()
    if urlsplit(value).scheme:
        _raise_fixture_error()
    try:
        ip_address(value.strip("[]"))
    except ValueError:
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
                _validate_fixture_scalar(nested_item)
            _raise_fixture_error()
        _validate_fixture_scalar(item)
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
