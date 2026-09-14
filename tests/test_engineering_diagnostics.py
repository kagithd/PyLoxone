"""Sanitized engineering inventory diagnostics."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from custom_components.loxone.const import DOMAIN
from custom_components.loxone.diagnostics import async_get_config_entry_diagnostics
from custom_components.loxone.engineering_runtime import (
    EngineeringRuntimeBinding,
    EngineeringRuntimeInventory,
)
from tests.engineering_fixtures import (
    element,
    inventory_of,
    make_snapshot,
    numeric_binding,
)


def test_diagnostics_exposes_complete_safe_inventory_and_runtime_summary():
    """Diagnostics retain statuses while omitting raw, sensitive, and live values."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07"),
            element(
                "analog",
                "VoltageIn",
                parent_uuid="device",
                io_name="AI1",
            ),
            element(
                "secret-code",
                "NfcCode",
                parent_uuid="device",
                title="Sensitive label",
            ),
        ),
        runtime=EngineeringRuntimeInventory((numeric_binding("analog", 7.0),)),
    )
    runtime = EngineeringRuntimeInventory(
        (
            EngineeringRuntimeBinding(
                engineering_uuid="analog",
                io_name="AI1",
                loxone_type="VoltageIn",
                title="Hidden runtime title",
                room="Office",
                suggested_platform="sensor",
                status="transport_error",
                endpoint="https://example.invalid/hidden",
                numeric_value=12345.6789,
                error="arbitrary private exception",
            ),
        )
    )
    coordinator = SimpleNamespace(
        engineering_snapshot=snapshot,
        engineering_runtime=runtime,
        miniserver=SimpleNamespace(
            lox_config=SimpleNamespace(json={"credentials": "must-not-appear", "LoxAPP3.json": {}})
        ),
    )
    entry = SimpleNamespace(entry_id="entry-a")
    hass = SimpleNamespace(data={DOMAIN: {"entry-a": coordinator}})

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(hass, entry))

    assert set(diagnostics) == {
        "integration",
        "engineering_inventory",
        "engineering_runtime",
    }
    rows = diagnostics["engineering_inventory"]["rows"]
    assert {row["exposure_status"] for row in rows} >= {
        "prepared_disabled",
        "inventory_only",
        "suppressed",
    }
    suppressed = next(row for row in rows if row["exposure_status"] == "suppressed")
    assert suppressed == {
        "kind": "structural",
        "capability_status": "sensitive",
        "exposure_status": "suppressed",
        "reason": "sensitive_metadata_suppressed",
    }
    assert {row["source_miniserver"] for row in rows if row["exposure_status"] != "suppressed"} == {
        snapshot.source.provider_identifier
    }
    serialized = json.dumps(diagnostics)
    for forbidden in (
        "LoxAPP3.json",
        "credentials",
        "must-not-appear",
        "secret-code",
        "Sensitive label",
        "Hidden runtime title",
        "example.invalid",
        "12345.6789",
        "arbitrary private exception",
        "attributes",
        "source_archive",
    ):
        assert forbidden not in serialized
    assert diagnostics["engineering_runtime"] == {
        "status": "live",
        "probed_count": 1,
        "bound_count": 0,
        "bindings_by_status": {"transport_error": 1},
        "bindings_by_method": {},
    }


def test_restored_diagnostics_reports_no_live_probe_without_raw_config():
    """A cached snapshot remains useful when no runtime probe is available."""
    snapshot = make_snapshot()
    coordinator = SimpleNamespace(
        engineering_snapshot=snapshot,
        engineering_runtime=None,
        miniserver=SimpleNamespace(lox_config=SimpleNamespace(json={"raw": "private"})),
    )
    entry = SimpleNamespace(entry_id="entry-a")
    hass = SimpleNamespace(data={DOMAIN: {"entry-a": coordinator}})

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(hass, entry))

    assert diagnostics["engineering_runtime"] == {"status": "restored_without_live_probe"}
    assert "private" not in json.dumps(diagnostics)
