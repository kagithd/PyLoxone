"""Tests for read-only engineering runtime binding helpers."""

import pytest

from custom_components.loxone.engineering_config import EngineeringElement
from custom_components.loxone.engineering_runtime import (
    _parse_runtime_response,
    _probe_targets,
)


def _element(*, uuid: str = "sensor-uuid", io_name: str = "AWI1") -> EngineeringElement:
    return EngineeringElement(
        key=uuid,
        xml_element="C",
        loxone_type="Lox1wireAsensor",
        title="Temperature",
        uuid=uuid,
        io_name=io_name,
        parent_uuid="device-uuid",
        room_uuid="room-uuid",
        room="Cellar",
        category_uuid="category-uuid",
        category="Sensors",
        suggested_platform="sensor",
        attributes={},
    )


def test_runtime_probe_prefers_uuid_and_uses_unique_name_as_fallback():
    """Stable UUID reads must be attempted before a unique engineering IO name."""
    assert _probe_targets(_element(), unique_io_name=True) == (
        ("uuid_all", "/dev/sps/io/sensor-uuid/all"),
        ("uuid_state", "/dev/sps/io/sensor-uuid/state"),
        ("io_name_state", "/dev/sps/io/AWI1/state"),
    )


def test_runtime_probe_does_not_fallback_to_ambiguous_io_name():
    """Repeated names such as AQ1 must never bind channels across devices."""
    assert _probe_targets(_element(io_name="AQ1"), unique_io_name=False) == (
        ("uuid_all", "/dev/sps/io/sensor-uuid/all"),
        ("uuid_state", "/dev/sps/io/sensor-uuid/state"),
    )


@pytest.mark.parametrize(
    ("payload", "numeric_value", "unit", "substate_count"),
    [
        (b'<LL control="dev/sps/io/AWI1/state" value="21.75" Code="200"/>', 21.75, None, 0),
        (b'{"LL":{"control":"dev/sps/io/AWI1/state","value":"21,75","Code":"200"}}', 21.75, None, 0),
        (b'<LL control="dev/sps/io/x/all" value="0" Code="200"><S value="1"/></LL>', 0.0, None, 1),
        (
            '<LL control="dev/sps/io/AWI1/state" value="21,75 \\N{DEGREE SIGN}C" Code="200"/>'.encode(),
            21.75,
            "\\N{DEGREE SIGN}C",
            0,
        ),
    ],
)
def test_runtime_response_parses_xml_and_json(payload, numeric_value, unit, substate_count):
    """Both documented XML and JSON LL response forms are accepted."""
    parsed = _parse_runtime_response(payload)
    assert parsed.code == 200
    assert parsed.numeric_value == numeric_value
    assert parsed.unit == unit
    assert parsed.substate_count == substate_count


def test_runtime_response_does_not_expose_text_as_numeric_value():
    """Arbitrary string states are classified but not copied into diagnostics."""
    parsed = _parse_runtime_response(b'<LL control="dev/sps/io/name/state" value="private text" Code="200"/>')
    assert parsed.value_kind == "text"
    assert parsed.numeric_value is None


def test_runtime_response_extracts_numeric_all_states_without_names():
    """Numeric output states retain stable UUIDs but omit arbitrary display names."""
    parsed = _parse_runtime_response(
        b'<LL control="dev/sps/io/x/all" value="2" Code="200" '
        b'n1="Temperature" u1="state-temp" v1="20.5 C" '
        b'n2="Private text" u2="state-text" v2="not numeric"/>'
    )
    assert parsed.substate_count == 2
    assert len(parsed.numeric_states) == 1
    assert parsed.numeric_states[0].state_uuid == "state-temp"
    assert parsed.numeric_states[0].numeric_value == 20.5
    assert parsed.numeric_states[0].unit == "C"


def test_runtime_response_rejects_entity_declarations():
    """Untrusted endpoint XML must not enable entity expansion."""
    with pytest.raises(ValueError, match="forbidden"):
        _parse_runtime_response(b'<!DOCTYPE LL [<!ENTITY x "boom">]><LL Code="200" value="&x;"/>')
