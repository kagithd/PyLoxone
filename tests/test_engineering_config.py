"""Tests for the read-only engineering configuration inventory."""

import io
import struct
import zipfile
import zlib
from datetime import UTC, datetime

import pytest

from custom_components.loxone import engineering_config
from custom_components.loxone.engineering_config import (
    LOXCC_MAGIC,
    EngineeringConfigError,
    _decompress_loxcc,
    _extract_xml,
    parse_engineering_xml,
)
from custom_components.loxone.engineering_entities import build_engineering_sensor_specs
from custom_components.loxone.engineering_runtime import EngineeringRuntimeBinding, EngineeringRuntimeInventory

_TEST_XML_LIMIT = 32
_DECODE_LIMIT_ERROR = "Decompressed XML exceeds the safety limit"


def test_direct_placement_parser_uses_exact_fields_and_omits_invalid_siblings():
    """Aliases, coercions, and malformed siblings must not supply placement."""
    from dataclasses import asdict
    from tests.engineering_fixtures import SYNTHETIC_PARSE_CONTEXT

    parsed = parse_engineering_xml(
        (
            '<C Type="LoxLIVE" U="ms" Installation=" Synthetic installation " '
            'SwitchBoard="Cabinet A" SwitchBoardRow="002" SwitchBoardPos="999">'
            '<C Type="FutureDevice" U="device" Installation="' + "x" * 81 + '" '
            'SwitchBoard="Cabinet B" SwitchBoardRow=" 2 " SwitchBoardPos="4" />'
            '<C Type="FutureDevice" U="alias" installation="Wrong" Switchboard="Wrong" '
            'Row="2" Position="4" />'
            '<C Type="FutureDevice" U="invalid" Installation="Bad&#10;text" '
            'SwitchBoardRow="２" SwitchBoardPos="+1" />'
            "</C>"
        ).encode(),
        **SYNTHETIC_PARSE_CONTEXT,
    )
    by_id = {item.uuid: item for item in parsed.elements}
    assert asdict(by_id["ms"].placement) == {
        "installation": "Synthetic installation",
        "switchboard": "Cabinet A",
        "row": 2,
        "position": 999,
    }
    assert asdict(by_id["device"].placement) == {
        "installation": None,
        "switchboard": "Cabinet B",
        "row": None,
        "position": 4,
    }
    assert by_id["alias"].placement is None
    assert by_id["invalid"].placement is None
    assert "placement" not in by_id["ms"].as_public_dict()


def _literal_loxcc(payload: bytes, *, declared_size: int | None = None) -> bytes:
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
        len(payload) if declared_size is None else declared_size,
        zlib.crc32(payload),
    ) + bytes(compressed)


def _loxcc_sequence(
    token: int,
    literals: bytes,
    offset: int,
    match_length_extension: bytes = b"",
) -> bytes:
    """Build one synthetic LoxCC sequence containing a back-reference."""
    return bytes((token,)) + literals + struct.pack("<H", offset) + match_length_extension


def _loxcc_block(compressed: bytes, *, declared_size: int, checksum: int = 0) -> bytes:
    """Wrap synthetic compressed bytes in a LoxCC header."""
    return (
        struct.pack(
            "<IIII",
            LOXCC_MAGIC,
            len(compressed),
            declared_size,
            checksum,
        )
        + compressed
    )


def _track_decoder_output(monkeypatch: pytest.MonkeyPatch) -> list[bytearray]:
    """Track monotonic decoder output without imposing a second size limit."""
    instances: list[bytearray] = []

    class TrackingBytearray(bytearray):
        """Record each decoder output buffer for post-call inspection."""

        def __init__(self) -> None:
            super().__init__()
            instances.append(self)

    monkeypatch.setattr(
        engineering_config,
        "bytearray",
        TrackingBytearray,
        raising=False,
    )
    return instances


def test_decompresses_literal_loxcc_and_validates_checksum():
    """A valid literal-only block is decompressed and checksum-checked."""
    xml = b'<ControlList Version="1" />'
    assert _decompress_loxcc(_literal_loxcc(xml)) == xml


def test_literal_output_exactly_at_limit_decodes(monkeypatch: pytest.MonkeyPatch):
    """A literal run may consume the complete configured output budget."""
    monkeypatch.setattr(engineering_config, "MAX_XML_BYTES", _TEST_XML_LIMIT)
    payload = b"A" * _TEST_XML_LIMIT

    assert _decompress_loxcc(_literal_loxcc(payload)) == payload  # noqa: S101


def test_literal_output_over_limit_is_rejected_before_copy(
    monkeypatch: pytest.MonkeyPatch,
):
    """A literal run must not be copied when it exceeds the remaining budget."""
    payload = b"A" * (_TEST_XML_LIMIT + 1)
    data = _literal_loxcc(payload, declared_size=0)
    monkeypatch.setattr(engineering_config, "MAX_XML_BYTES", _TEST_XML_LIMIT)
    outputs = _track_decoder_output(monkeypatch)

    with pytest.raises(EngineeringConfigError) as error:
        _decompress_loxcc(data)

    assert str(error.value) == _DECODE_LIMIT_ERROR  # noqa: S101
    assert [bytes(output) for output in outputs] == [b""]  # noqa: S101


def test_overlapping_match_exactly_at_limit_decodes(
    monkeypatch: pytest.MonkeyPatch,
):
    """An offset-one match retains overlap semantics at the exact limit."""
    compressed = _loxcc_sequence(0x1F, b"A", 1, bytes((0x0C,)))
    data = _loxcc_block(compressed, declared_size=_TEST_XML_LIMIT)
    monkeypatch.setattr(engineering_config, "MAX_XML_BYTES", _TEST_XML_LIMIT)

    assert _decompress_loxcc(data) == b"A" * _TEST_XML_LIMIT  # noqa: S101


def test_overlapping_match_over_limit_is_rejected_before_copy(
    monkeypatch: pytest.MonkeyPatch,
):
    """An over-budget match must be rejected before its append loop starts."""
    compressed = _loxcc_sequence(0x1F, b"A", 1, bytes((0x0D,)))
    data = _loxcc_block(compressed, declared_size=0)
    monkeypatch.setattr(engineering_config, "MAX_XML_BYTES", _TEST_XML_LIMIT)
    outputs = _track_decoder_output(monkeypatch)

    with pytest.raises(EngineeringConfigError) as error:
        _decompress_loxcc(data)

    assert str(error.value) == _DECODE_LIMIT_ERROR  # noqa: S101
    assert [bytes(output) for output in outputs] == [b"A"]  # noqa: S101


@pytest.mark.parametrize("declared_size", [0, _TEST_XML_LIMIT])
def test_extended_match_is_rejected_atomically_before_expansion(
    monkeypatch: pytest.MonkeyPatch,
    declared_size: int,
):
    """A 25-byte block requesting 1040 bytes never expands past its prefix."""
    compressed = bytes((0x1F, ord("A"), 0x01, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00))
    data = _loxcc_block(compressed, declared_size=declared_size)
    monkeypatch.setattr(engineering_config, "MAX_XML_BYTES", _TEST_XML_LIMIT)
    outputs = _track_decoder_output(monkeypatch)

    with pytest.raises(EngineeringConfigError) as error:
        _decompress_loxcc(data)

    assert len(data) == 25  # noqa: PLR2004, S101
    assert str(error.value) == _DECODE_LIMIT_ERROR  # noqa: S101
    assert [bytes(output) for output in outputs] == [b"A"]  # noqa: S101


def test_repeated_overlapping_back_references_preserve_output():
    """More than one valid overlapping match keeps byte-for-byte semantics."""
    compressed = _loxcc_sequence(0x10, b"A", 1) + _loxcc_sequence(0x00, b"", 1)
    expected = b"AAAAAAAAA"
    data = _loxcc_block(
        compressed,
        declared_size=len(expected),
        checksum=zlib.crc32(expected),
    )

    assert _decompress_loxcc(data) == expected  # noqa: S101


def test_rejects_incomplete_back_reference():
    """A truncated offset retains its bounded malformed-input rejection."""
    data = _loxcc_block(bytes((0x10, ord("A"), 0x01)), declared_size=0)

    with pytest.raises(EngineeringConfigError, match="back-reference is incomplete"):
        _decompress_loxcc(data)


@pytest.mark.parametrize("offset", [0, 2])
def test_rejects_invalid_back_reference_offsets(offset: int):
    """Zero and out-of-range offsets retain their bounded rejection."""
    data = _loxcc_block(_loxcc_sequence(0x10, b"A", offset), declared_size=0)

    with pytest.raises(EngineeringConfigError, match="invalid back-reference"):
        _decompress_loxcc(data)


def test_rejects_declared_size_mismatch_within_budget():
    """A safe decoded result must still agree with a nonzero declared size."""
    with pytest.raises(EngineeringConfigError, match="size mismatch"):
        _decompress_loxcc(_literal_loxcc(b"A", declared_size=2))


def test_rejects_declared_size_over_limit_before_output_construction(
    monkeypatch: pytest.MonkeyPatch,
):
    """An oversized header is rejected before allocating the output buffer."""
    compressed = bytes((0x10, ord("A")))
    data = _loxcc_block(compressed, declared_size=_TEST_XML_LIMIT + 1)
    monkeypatch.setattr(engineering_config, "MAX_XML_BYTES", _TEST_XML_LIMIT)
    outputs = _track_decoder_output(monkeypatch)

    with pytest.raises(EngineeringConfigError, match="configured safety limit"):
        _decompress_loxcc(data)

    assert outputs == []  # noqa: S101


def test_extracts_valid_loxcc_from_in_memory_archive():
    """The archive path keeps returning the exact validated decoder output."""
    expected = b'<ControlList Version="2" />'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as backup:
        backup.writestr("sps0.LoxCC", _literal_loxcc(expected))

    assert _extract_xml(archive.getvalue()) == expected  # noqa: S101


def test_rejects_invalid_loxcc_magic():
    """An unrelated binary must never be parsed as a config."""
    with pytest.raises(EngineeringConfigError, match="magic"):
        _decompress_loxcc(bytes(16))


@pytest.mark.parametrize("room_uuid", ["room-office", ""])
def test_generic_inventory_keeps_unknown_onewire_hardware_and_topology(room_uuid):
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
    xml = xml.replace(b'Pr="room-office"', f'Pr="{room_uuid}"'.encode())
    inventory = parse_engineering_xml(
        xml,
        source_archive="sps_42_20260818120000.zip",
        config_version=42,
        config_timestamp=datetime(2026, 8, 18, 12, tzinfo=UTC),
    )

    sensor = next(item for item in inventory.candidates if item.uuid == "ow-sensor")
    assert sensor.parent_uuid == "ow-device"
    assert sensor.room == ("Office" if room_uuid else None)
    assert sensor.room_uuid == (room_uuid or None)
    assert sensor.category == "Temperature"
    assert sensor.suggested_platform == "sensor"
    assert inventory.summary()["candidate_count"] == 2


def test_uuidless_containers_preserve_legacy_uuid_ancestry_for_sensor_grouping():
    """Opaque immediate keys must not break the legacy UUID ownership consumer."""
    xml = b"""<ControlList>
  <C Type="LoxLIVE" U="miniserver-uuid" />
  <C Type="TreeDevice" U="device-uuid">
    <C Type="TreeCaption"><C Type="TreeCaption">
      <C Type="WeatherData" U="channel-uuid" IName="AI1" />
    </C></C>
  </C>
</ControlList>"""
    inventory = parse_engineering_xml(
        xml,
        source_archive="sps_7_20260913120000.zip",
        config_version=7,
        config_timestamp=datetime(2026, 9, 13, 12, tzinfo=UTC),
    )
    captions = [item for item in inventory.elements if item.loxone_type == "TreeCaption"]
    channel = next(item for item in inventory.elements if item.uuid == "channel-uuid")
    runtime = EngineeringRuntimeInventory(
        bindings=(
            EngineeringRuntimeBinding(
                "channel-uuid", "AI1", "WeatherData", None, None, "sensor", "bound", numeric_value=1.0
            ),
        )
    )

    assert channel.parent_uuid == "device-uuid"
    assert channel.parent_key == captions[-1].key
    assert build_engineering_sensor_specs(inventory, runtime)[0].device.uuid == "device-uuid"
