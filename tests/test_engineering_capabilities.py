"""Tests for explicit, read-only engineering capability policy."""

from dataclasses import replace
import math

import pytest

from custom_components.loxone.engineering_capabilities import (
    CapabilityState,
    ExposureStatus,
    resolve_capability,
    resolve_engineering_capabilities,
    select_runtime_probe_candidates,
)
from custom_components.loxone.engineering_entities import build_engineering_entity_specs
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory, binding_from_response
from custom_components.loxone.engineering_topology import ResolvedEngineeringInventory, resolve_engineering_topology
from tests.engineering_fixtures import numeric_binding, provider_inventory, resolved_node, source, text_binding


def test_successful_read_never_implies_write_capability():
    capability = resolve_capability(resolved_node("analog-input", "VoltageIn"), numeric_binding("analog-input", 2.4))
    assert capability.state is CapabilityState.READABLE
    assert capability.platform == "sensor"
    assert capability.exposure is ExposureStatus.PREPARED_DISABLED


def test_binary_input_requires_zero_or_one_semantics():
    binary = resolve_capability(resolved_node("i1", "DigitalIn"), numeric_binding("i1", 1.0, "DigitalIn"))
    non_binary = resolve_capability(resolved_node("i2", "DigitalIn"), numeric_binding("i2", 2.0, "DigitalIn"))
    assert binary.platform == "binary_sensor"
    assert non_binary.platform is None
    assert non_binary.state is CapabilityState.UNSUPPORTED


def test_outputs_are_inventory_only_even_when_readable():
    capability = resolve_capability(resolved_node("q1", "Actor", io_name="Q1"), numeric_binding("q1", 1.0, "Actor"))
    assert capability.state is CapabilityState.READABLE
    assert capability.platform is None
    assert capability.exposure is ExposureStatus.INVENTORY_ONLY
    assert capability.reason == "output_write_contract_not_enabled"


def test_sensitive_and_text_payloads_are_suppressed():
    access = resolve_capability(resolved_node("code", "NfcCode"), numeric_binding("code", 1234.0, "NfcCode"))
    text = resolve_capability(resolved_node("text", "SysVar"), text_binding())
    assert access.state is CapabilityState.SENSITIVE
    assert access.exposure is ExposureStatus.SUPPRESSED
    assert text.exposure is ExposureStatus.SUPPRESSED


def test_weather_and_system_variables_share_module_owners():
    runtime = EngineeringRuntimeInventory(
        (numeric_binding("weather-value", 18.5, "WeatherData"), numeric_binding("system-variable", 1.0, "SysVar"))
    )
    rows = resolve_engineering_capabilities(resolve_engineering_topology(provider_inventory(), source()), runtime)
    specs = build_engineering_entity_specs(rows, runtime)
    assert {spec.unique_id: spec.owner_identifier for spec in specs} == {
        "weather-value": "serial-a:weather-server",
        "system-variable": "serial-a:global-states",
    }
    assert all(not spec.enabled_by_default for spec in specs)


def test_probe_candidates_are_type_driven_and_sensitive_rows_are_not_targets():
    sensitive = replace(resolved_node("code", "NfcCode"), sensitive=True)
    candidates = select_runtime_probe_candidates(
        ResolvedEngineeringInventory(
            source(),
            (
                resolved_node("i1", "DigitalIn"),
                resolved_node("ai1", "VoltageIn"),
                resolved_node("online", "Online"),
                sensitive,
            ),
        )
    )
    assert {item.element.uuid for item in candidates} == {"i1", "ai1", "online"}


@pytest.mark.parametrize("value", (math.nan, math.inf, -math.inf))
def test_nonfinite_values_are_not_exposed(value):
    assert (
        resolve_capability(resolved_node("ai1", "VoltageIn"), numeric_binding("ai1", value)).exposure
        is ExposureStatus.SUPPRESSED
    )


@pytest.mark.parametrize("value", ("12.4 private-label", "12 C\x01", "nan", "inf", "12 xyz"))
def test_unsafe_numeric_strings_are_not_runtime_numbers(value):
    binding = binding_from_response("ai1", value)
    assert binding.value_kind == "text"
    assert binding.numeric_value is None


def test_only_explicit_state_uuid_creates_event_entity():
    mapped = replace(numeric_binding("engineering-ai1", 2.4), state_uuid="event-state-ai1")
    mapped_rows = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(source(), (resolved_node("engineering-ai1", "VoltageIn"),)),
        EngineeringRuntimeInventory((mapped,)),
    )
    assert (
        build_engineering_entity_specs(mapped_rows, EngineeringRuntimeInventory((mapped,)))[0].state_uuid
        == "event-state-ai1"
    )
    scalar = replace(numeric_binding("ai1", 2.4), binding_method="uuid_state", state_uuid=None)
    scalar_row = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(source(), (resolved_node("ai1", "VoltageIn"),)),
        EngineeringRuntimeInventory((scalar,)),
    )[0]
    assert scalar_row.binding is not None and not scalar_row.binding.event_binding_proven
    assert scalar_row.capability.reason == "readable_rebind_only"


def test_status_boolean_and_unknown_channel_have_explicit_safe_results():
    online = replace(numeric_binding("online", 1.0, "Online"), state_uuid="online-state")
    rows = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(
            source(), (resolved_node("online", "Online"), resolved_node("unknown", "FutureChannel"))
        ),
        EngineeringRuntimeInventory((online,)),
    )
    assert rows[0].semantic_platform == "binary_sensor"
    assert rows[0].capability.exposure is ExposureStatus.PREPARED_DISABLED
    assert rows[1].capability.exposure is ExposureStatus.INVENTORY_ONLY


def test_transport_and_auth_failures_remain_distinct_from_unbound():
    transport = replace(numeric_binding("ai1", 2.4), status="transport_error")
    auth = replace(numeric_binding("ai2", 2.4), status="auth_error")
    assert resolve_capability(resolved_node("ai1", "VoltageIn"), transport).reason == "runtime_transport_failure"
    assert resolve_capability(resolved_node("ai2", "VoltageIn"), auth).reason == "runtime_auth_failure"


def test_semantic_platform_is_stable_without_a_runtime_result():
    rows = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(source(), (resolved_node("ai1", "VoltageIn"),)), None
    )
    assert rows[0].semantic_platform == "sensor"
    assert rows[0].capability.exposure is ExposureStatus.INVENTORY_ONLY


def test_sensitive_rows_never_keep_a_safe_binding_descriptor():
    row = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(source(), (resolved_node("code", "NfcCode"),)),
        EngineeringRuntimeInventory((numeric_binding("code", 1.0, "NfcCode"),)),
    )[0]
    assert row.binding is None


def test_descriptor_drops_unknown_units_and_cached_invalid_rebind_is_unavailable():
    binding = replace(numeric_binding("ai1", 2.4), unit="unknown-unit")
    rows = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(source(), (resolved_node("ai1", "VoltageIn"),)),
        EngineeringRuntimeInventory((binding,)),
    )
    assert rows[0].binding is None
    assert rows[0].capability.reason == "invalid_runtime_unit"


@pytest.mark.parametrize("unit", ["invalid", "V"])
def test_cached_unitless_binding_rejects_invalid_or_changed_unit(unit):
    saved = numeric_binding("ai1", 2.4)
    rows = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(source(), (resolved_node("ai1", "VoltageIn"),)),
        EngineeringRuntimeInventory((saved,)),
    )
    live = replace(saved, unit=unit)
    spec = build_engineering_entity_specs(rows, EngineeringRuntimeInventory((live,)))[0]
    assert spec.available is False
    assert spec.native_value is None


def test_malformed_probe_remains_distinguishable_in_capability_row():
    binding = replace(numeric_binding("ai1", 2.4), status="malformed_response")
    row = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(source(), (resolved_node("ai1", "VoltageIn"),)),
        EngineeringRuntimeInventory((binding,)),
    )[0]
    assert row.capability.reason == "runtime_malformed_failure"


def test_physical_nfc_hardware_is_not_reclassified_sensitive():
    capability = resolve_capability(resolved_node("nfc", "NfcCodeTouch"), None)
    assert capability.state is not CapabilityState.SENSITIVE


def test_invalid_initial_unit_is_not_prepared_and_degree_is_canonical():
    invalid = replace(numeric_binding("ai1", 2.4), unit="invalid")
    assert resolve_capability(resolved_node("ai1", "VoltageIn"), invalid).reason == "invalid_runtime_unit"
    degree = replace(numeric_binding("ai2", 2.4), unit="°", title="Temperature")
    rows = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(source(), (resolved_node("ai2", "VoltageIn"),)),
        EngineeringRuntimeInventory((degree,)),
    )
    assert rows[0].binding is not None
    assert rows[0].binding.safe_unit == "°C"
    initial = build_engineering_entity_specs(rows, EngineeringRuntimeInventory((degree,)))[0]
    assert initial.available is True
    assert initial.native_value == 2.4
    rebind = build_engineering_entity_specs(rows, EngineeringRuntimeInventory((replace(degree),)))[0]
    assert rebind.available is True
    canonical = replace(degree, unit="°C")
    assert build_engineering_entity_specs(rows, EngineeringRuntimeInventory((canonical,)))[0].available is True
