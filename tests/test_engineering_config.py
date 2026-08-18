"""Tests for the read-only engineering configuration inventory."""

from datetime import UTC, datetime
import struct
import zlib

import pytest

from custom_components.loxone.engineering_config import (
    EngineeringConfigError,
    LOXCC_MAGIC,
    _decompress_loxcc,
    parse_engineering_xml,
)


def _literal_loxcc(payload: bytes) -> bytes:
    """Create one standards-compliant literal-only LoxCC test block."""
    length = len(payload)
    token_length = min(length, 15)
    compressed = bytearray([token_length << 4])
    if length >= 15:
        remaining = length - 15
        while remaining >= 255:
            compressed.append(255)
            remaining -= 255
        compressed.append(remaining)
    compressed.extend(payload)
    return struct.pack(
        "<IIII",
        LOXCC_MAGIC,
        len(compressed),
        len(payload),
        zlib.crc32(payload),
    ) + bytes(compressed)


def test_decompresses_literal_loxcc_and_validates_checksum():
    """A valid literal-only block is decompressed and checksum-checked."""
    xml = b'<ControlList Version="1" />'
    assert _decompress_loxcc(_literal_loxcc(xml)) == xml


def test_rejects_invalid_loxcc_magic():
    """An unrelated binary must never be parsed as a config."""
    with pytest.raises(EngineeringConfigError, match="magic"):
        _decompress_loxcc(bytes(16))


def test_generic_inventory_keeps_unknown_onewire_hardware_and_topology():
    """Unknown hardware remains available with inherited room/category data."""
    xml = b"""<?xml version="1.0"?>
<ControlList Version="42">
  <C Type="Place" U="room-office" Title="Office" />
  <C Type="Category" U="cat-temp" Title="Temperature" />
  <C Type="OneWireDevice" U="ow-device" Title="OneWire module">
    <IoData Pr="room-office" Cr="cat-temp" />
    <C Type="OneWireTemperatureSensor" U="ow-sensor" IName="AI1" Title="Pipe sensor" />
  </C>
</ControlList>"""
    inventory = parse_engineering_xml(
        xml,
        source_archive="sps_42_20260818120000.zip",
        config_version=42,
        config_timestamp=datetime(2026, 8, 18, 12, tzinfo=UTC),
    )

    sensor = next(item for item in inventory.candidates if item.uuid == "ow-sensor")
    assert sensor.parent_uuid == "ow-device"
    assert sensor.room == "Office"
    assert sensor.category == "Temperature"
    assert sensor.suggested_platform == "sensor"
    assert inventory.summary()["candidate_count"] == 2
