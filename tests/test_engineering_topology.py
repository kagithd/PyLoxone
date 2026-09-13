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
    ResolutionStatus,
    classify_node_kind,
    resolve_engineering_topology,
    scoped_engineering_identifier,
)
from tests.engineering_fixtures import (
    DUPLICATE_UUID_XML,
    SYNTHETIC_PARSE_CONTEXT,
    UUIDLESS_CONTAINER_XML,
    cyclic_inventory,
    element,
    inventory_of,
    node,
    provider_inventory,
    reference_link_inventory,
    source,
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


def test_tree_caption_stays_in_path_but_is_not_a_device():
    """Structural captions form a diagnostic path but never an owner identity."""
    resolved = resolve_engineering_topology(
        inventory_of(
            element("ms", "LoxLIVE"),
            element("tree", "LoxTree", parent_uuid="ms"),
            element("branch", "TreeCaption", parent_uuid="tree", title="Branch A"),
            element("nfc", "TreeDevice", parent_uuid="branch", title="ST-F03"),
        ),
        source(),
    )

    nfc = node(resolved, "nfc")
    assert nfc.device_identifier == "serial-a:nfc"
    assert nfc.via_device_identifier == "serial-a:tree"
    assert nfc.bus_kind == "tree"
    assert nfc.topology_path == ("LoxLIVE", "LoxTree", "Branch A", "ST-F03")
    assert node(resolved, "branch").kind is NodeKind.STRUCTURAL


def test_air_and_onewire_endpoints_use_the_nearest_extension():
    """Link endpoints attach to their nearest proven bridge or extension."""
    resolved = resolve_engineering_topology(reference_link_inventory(), source())

    assert node(resolved, "air-device").via_device_identifier == "serial-a:air-extension"
    assert node(resolved, "air-extension").via_device_identifier == "serial-a:link"
    assert node(resolved, "wire-sensor").via_device_identifier == "serial-a:wire-extension"
    assert node(resolved, "wire-extension").via_device_identifier == "serial-a:link"


def test_internal_io_and_document_services_belong_to_source_miniserver():
    """Internal channels and document service modules have source-scoped owners."""
    resolved = resolve_engineering_topology(provider_inventory(), source())

    assert node(resolved, "digital-i1").device_identifier == "serial-a"
    assert node(resolved, "analog-ai1").device_identifier == "serial-a"
    assert node(resolved, "relay-q1").device_identifier == "serial-a"
    assert node(resolved, "weather-server").via_device_identifier == "serial-a"
    assert node(resolved, "weather-value").device_identifier == "serial-a:weather-server"
    assert node(resolved, "global-states").via_device_identifier == "serial-a"
    assert node(resolved, "system-variable").device_identifier == "serial-a:global-states"


def test_cycle_and_missing_or_overdeep_parents_are_unresolved_without_guesses():
    """Broken ancestry never produces a fabricated owner or transport path."""
    cyclic = node(resolve_engineering_topology(cyclic_inventory(), source()), "cycle-a")
    missing = node(
        resolve_engineering_topology(inventory_of(element("missing", "TreeDevice", parent_key="absent")), source()),
        "missing",
    )
    deep = node(
        resolve_engineering_topology(
            inventory_of(
                element("first", "TreeCaption", parent_key="second"),
                element("second", "TreeCaption", parent_key="third"),
                element("third", "TreeDevice"),
            ),
            source(),
        ),
        "first",
    )

    assert cyclic.resolution_reason == "parent_cycle"
    assert missing.resolution_reason == "missing_parent"
    assert missing.via_device_identifier is None
    assert deep.resolution_status is ResolutionStatus.RESOLVED
    assert deep.device_identifier is None


def test_depth_limit_is_reported_without_owner_guess():
    """A custom bounded resolver reports chains that exceed its traversal limit."""
    from custom_components.loxone.engineering_topology import OwnerResolver

    resolved = OwnerResolver(max_depth=1).resolve(
        inventory_of(
            element("a", "TreeDevice", parent_key="b"),
            element("b", "TreeCaption", parent_key="c"),
            element("c", "LoxTree"),
        ),
        source(),
    )

    item = node(resolved, "a")
    assert item.resolution_status is ResolutionStatus.UNRESOLVED
    assert item.resolution_reason == "parent_depth_exceeded"
    assert item.via_device_identifier is None


def test_same_uuid_on_two_entries_produces_distinct_registry_identifiers():
    """Provider identity scopes otherwise identical engineering UUIDs."""
    inventory = inventory_of(element("shared", "TreeDevice"))

    assert (
        node(resolve_engineering_topology(inventory, source("entry-a", "serial-a")), "shared").device_identifier
        == "serial-a:shared"
    )
    assert (
        node(resolve_engineering_topology(inventory, source("entry-b", "serial-b")), "shared").device_identifier
        == "serial-b:shared"
    )


def test_singleton_and_duplicate_uuidless_services_have_safe_identity_rules():
    """Only a singleton type gets a typed UUID-less provider service identifier."""
    singleton = resolve_engineering_topology(
        inventory_of(element("ms", "LoxLIVE", room=None), element(None, "WeatherServer", key="xml:000002", room=None)),
        source(),
    )
    duplicate = resolve_engineering_topology(
        inventory_of(
            element("ms", "LoxLIVE", room=None),
            element(None, "WeatherServer", key="xml:000002", room=None),
            element(None, "WeatherServer", key="xml:000003", room=None),
        ),
        source(),
    )

    assert singleton.nodes[1].device_identifier == "serial-a:service:weatherserver"
    assert {item.resolution_reason for item in duplicate.nodes[1:]} == {"ambiguous_uuidless_service"}


def test_uuidless_singleton_service_owns_its_typed_channel():
    """Typed child ownership follows the same UUID-less singleton rule."""
    resolved = resolve_engineering_topology(
        inventory_of(
            element("ms", "LoxLIVE", room=None),
            element(None, "WeatherServer", key="xml:000002", room=None),
            element("weather", "WeatherData", parent_key="xml:000002"),
        ),
        source(),
    )

    assert node(resolved, "weather").device_identifier == "serial-a:service:weatherserver"


def test_parser_to_resolver_keeps_uuidless_sensitive_ancestry_opaque_and_sanitized():
    """Sensitivity crosses UUID-less parents before name/path projection."""
    xml = b"""<?xml version="1.0"?>
<ControlList>
  <C Type="LoxLIVE" U="ms" Title="Miniserver" />
  <C Type="NfcCode" Title="Private caption">
    <C Type="TreeDevice" U="child" Title="ST-F04" />
  </C>
</ControlList>"""

    resolved = resolve_engineering_topology(parse_engineering_xml(xml, **SYNTHETIC_PARSE_CONTEXT), source())
    child = node(resolved, "child")

    assert child.sensitive is True
    assert child.element.title is None
    assert child.topology_path == ()
