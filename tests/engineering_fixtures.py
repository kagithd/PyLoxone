"""Safe, synthetic data helpers for engineering configuration tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

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

_FORBIDDEN_KEY_PARTS = (
    "access",
    "address",
    "credential",
    "latitude",
    "longitude",
    "password",
    "privatekey",
    "remoteurl",
    "localurl",
    "token",
    "user",
)
_URL_OR_ADDRESS = re.compile(r"(?:https?://|\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:latitude|longitude|gps)\b)", re.IGNORECASE)


def validate_fixture_input(value: Any) -> Any:
    """Reject personal, location, endpoint, credential, and access fixture data."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if any(part in normalized_key for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError("forbidden fixture field")
            validate_fixture_input(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            validate_fixture_input(item)
    elif isinstance(value, str) and _URL_OR_ADDRESS.search(value):
        raise ValueError("forbidden fixture field")
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
