"""Tests for source-scoped engineering topology models."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.loxone.engineering_config import (
    EngineeringConfigError,
    EngineeringInventory,
    parse_engineering_xml,
)
from custom_components.loxone.engineering_topology import (
    EngineeringSourceContext,
    NodeKind,
    classify_node_kind,
    scoped_engineering_identifier,
)
from tests.engineering_fixtures import (
    DUPLICATE_UUID_XML,
    SYNTHETIC_PARSE_CONTEXT,
    UUIDLESS_CONTAINER_XML,
    element,
    validate_fixture_input,
)


def test_fixture_input_gate_rejects_identity_and_location_fields():
    """Synthetic helpers must reject personal and installation data."""
    with pytest.raises(ValueError, match="forbidden fixture field"):
        validate_fixture_input({"device": "ST-F01", "room": "Office", "CurrentUser": "person"})

    assert validate_fixture_input({"device": "ST-F01", "room": "Office"}) == {
        "device": "ST-F01",
        "room": "Office",
    }


def test_fixture_input_accepts_opaque_xml_keys_for_uuidless_ancestry():
    """Synthetic UUID-less nodes can retain their parser-compatible opaque key."""
    container = element(None, "WeatherServer", key="xml:000002")
    child = element("weather-uuid", "WeatherData", parent_key=container.key)

    assert container.key == "xml:000002"
    assert child.parent_key == "xml:000002"


@pytest.mark.parametrize(
    "title",
    (
        "Device at https://example.invalid",
        "Sensor 198.51.100.1",
        "Sensor [2001:db8::1]",
    ),
)
def test_fixture_input_rejects_embedded_network_material_in_allowed_title(title):
    """Presentation fields cannot conceal URL or address material."""
    with pytest.raises(ValueError, match="forbidden fixture field"):
        validate_fixture_input({"title": title})


@pytest.mark.parametrize(
    "fixture_input",
    (
        {"ProjectName": "Synthetic project"},
        {"Installation": "Synthetic installation"},
        {"CurrentUser": "person"},
        {"Location": "Office"},
        {"coordinates": [12.0, 34.0]},
        {"url": "https://example.invalid"},
        {"host": "2001:db8::1"},
        {"credential": "synthetic-secret"},
        {"accessCode": "synthetic-secret"},
        {"device": {"host": "198.51.100.1"}},
    ),
)
def test_fixture_input_schema_rejects_forbidden_nested_and_network_fields(fixture_input):
    """Only the fixture constructor's safe scalar fields are accepted."""
    with pytest.raises(ValueError, match="forbidden fixture field"):
        validate_fixture_input(fixture_input)


def test_type_driven_classification_does_not_trust_titles():
    """Only technical types can establish topology identity."""
    link = element("link-uuid", "LoxLink", title="Editable caption")
    tree = element("tree-uuid", "LoxTree", title="Another caption")
    endpoint = element("endpoint-uuid", "TreeDevice", title="ST-F07")
    caption = element("caption-uuid", "TreeCaption", title="Branch A")

    assert classify_node_kind(link) is NodeKind.BUS
    assert classify_node_kind(tree) is NodeKind.BUS
    assert classify_node_kind(endpoint) is NodeKind.PHYSICAL_DEVICE
    assert classify_node_kind(caption) is NodeKind.STRUCTURAL


@pytest.mark.parametrize(
    "element_type",
    (
        "WeatherServer",
        "GlobalStates",
        "OperatingModes",
        "TimeFunctions",
        "SystemStatus",
        "DeviceMonitor",
        "NetworkPlugin",
        "VirtualInputs",
        "VirtualOutputs",
        "Tasks",
        "Messages",
        "Intercom",
        "LightingGroups",
    ),
)
def test_known_provider_containers_are_service_modules(element_type):
    """Known provider services are recognized from their exact types."""
    assert classify_node_kind(element("service", element_type)) is NodeKind.SERVICE_MODULE


@pytest.mark.parametrize(
    "element_type",
    (
        "DigitalIn",
        "digitalin",
        "VoltageIn",
        "VOLTAGEIN",
        "Actor",
        "actor",
        "AnalogOut",
        "ANALOGOUT",
        "Status",
        "Online",
        "DeviceStatus",
        "DeviceOnline",
    ),
)
def test_known_io_and_status_types_are_channels_without_capability(element_type):
    """Exact technical types identify channels without implying write access."""
    assert classify_node_kind(element("channel", element_type)) is NodeKind.CHANNEL


@pytest.mark.parametrize(
    ("element_type", "expected_kind"),
    (
        ("Page", NodeKind.STRUCTURAL),
        ("TreeCaption", NodeKind.STRUCTURAL),
        ("WeatherServer", NodeKind.SERVICE_MODULE),
        ("SystemStatus", NodeKind.SERVICE_MODULE),
    ),
)
def test_structural_and_service_types_are_not_channels(element_type, expected_kind):
    """Channel aliases cannot override exact service or structural classifications."""
    assert classify_node_kind(element("item", element_type)) is expected_kind


def test_scoped_identifier_uses_serial_and_uuid_only():
    """Presentation metadata cannot affect a graph-device identity."""
    source = EngineeringSourceContext(
        entry_id="entry-a",
        serial_number="serial-a",
        title="Miniserver",
        model="Miniserver",
        source_archive="sps_7_20260913120000.zip",
        config_version=7,
        config_timestamp=datetime(2026, 9, 13, 12, tzinfo=UTC),
        loxapp_last_modified="revision-7",
    )
    item = element("device-uuid", "TreeDevice", title="ST-F07")

    assert scoped_engineering_identifier(source, item) == "serial-a:device-uuid"


def test_public_element_dict_is_an_explicit_allowlist():
    """Raw parsed attributes must never reach the public inventory."""
    item = element("device-uuid", "TreeDevice", title="ST-F07")
    item = replace(
        item,
        attributes={
            "U": "device-uuid",
            "Title": "ST-F07",
            "CurrentUser": "Synthetic User",
            "Latitude": "12.345",
            "RemoteUrl": "https://example.invalid/private",
            "AccessCode": "synthetic-secret",
        },
    )

    public = item.as_public_dict()

    assert public["uuid"] == "device-uuid"
    assert public["title"] == "ST-F07"
    assert not ({"attributes", "CurrentUser", "Latitude", "RemoteUrl", "AccessCode"} & public.keys())


def test_inventory_requires_uuid_and_miniserver_to_be_complete():
    """Only a recognizable, UUID-backed parsed tree is complete."""
    now = datetime.now(UTC)
    complete = EngineeringInventory("test.zip", 1, now, now, 10, (element("ms-uuid", "LoxLIVE"),))
    separate_anchor = EngineeringInventory(
        "test.zip",
        1,
        now,
        now,
        10,
        (element(None, "LoxLIVE"), element("device-uuid", "TreeDevice")),
    )
    empty = EngineeringInventory("test.zip", 1, now, now, 10, ())

    assert complete.is_complete is True
    assert separate_anchor.is_complete is True
    assert empty.is_complete is False


def test_uuidless_parser_keys_are_opaque_and_preserve_parent_chain():
    """UUID-less containers retain opaque ancestry without source text in keys."""
    inventory = parse_engineering_xml(UUIDLESS_CONTAINER_XML, **SYNTHETIC_PARSE_CONTEXT)
    container, child = inventory.elements[-2:]

    assert container.uuid is None
    assert container.key.startswith("xml:")
    assert "Private caption" not in container.key
    assert child.parent_key == container.key


def test_duplicate_stable_uuid_invalidates_inventory():
    """A duplicate stable UUID makes a candidate unsafe to resolve."""
    with pytest.raises(EngineeringConfigError, match="duplicate_engineering_uuid"):
        parse_engineering_xml(DUPLICATE_UUID_XML, **SYNTHETIC_PARSE_CONTEXT)
