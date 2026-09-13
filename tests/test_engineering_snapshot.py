"""Private, privacy-safe engineering snapshot persistence tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from math import nan
from types import MappingProxyType

import pytest

from custom_components.loxone.engineering_changes import (
    EngineeringEntityImpact,
    EngineeringImpactPlan,
)
from custom_components.loxone.engineering_snapshot import (
    ENGINEERING_SNAPSHOT_STORAGE_VERSION,
    EngineeringSnapshotError,
    StoredEngineeringState,
    async_load_engineering_state,
    async_store_engineering_state,
    engineering_configuration_revision_id,
    engineering_generation_id,
    engineering_safe_content_digest,
    next_engineering_read_sequence,
    snapshot_from_dict,
    snapshot_to_dict,
    stored_state_from_dict,
    stored_state_to_dict,
    validate_engineering_snapshot,
)
from tests.engineering_fixtures import (
    cyclic_inventory,
    element,
    inventory_of,
    make_snapshot,
    source,
)


def test_snapshot_round_trip_contains_only_safe_allowlisted_fields():
    """A persisted cache must reconstruct bindings without raw/live metadata."""
    original = make_snapshot()
    encoded = snapshot_to_dict(original)
    restored = snapshot_from_dict(encoded)

    assert snapshot_to_dict(restored) == encoded
    assert restored.source.title is None
    assert restored.source.model is None
    assert restored.source.source_archive == ""
    assert restored.generation_id == original.generation_id
    assert any(row.binding and row.binding.state_uuid for row in restored.rows if row.semantic_platform)
    assert all(not row.node.element.attributes for row in restored.rows)
    assert all(isinstance(row.node.element.attributes, MappingProxyType) for row in restored.rows)
    assert {row.config_version for row in restored.rows} == {7}
    weather = next(row for row in restored.rows if row.node.element.uuid == "weather-value")
    assert weather.owner_name == "WeatherServer"
    assert weather.owner_model == "WeatherServer"

    rendered = json.dumps(encoded)
    for forbidden_key in (
        "parent_uuid",
        "attributes",
        "category",
        "numeric_value",
        "endpoint",
        "error",
        "source_archive",
        "downloaded_at",
        "CurrentUser",
        "Latitude",
        "Longitude",
        "LocalUrl",
        "RemoteUrl",
        "HostAddress",
        "AccessCode",
        "title",
        "model",
    ):
        assert forbidden_key not in rendered


@pytest.mark.parametrize(
    "candidate",
    [
        make_snapshot(inventory=inventory_of()),
        make_snapshot(inventory=inventory_of(element("device", "TreeDevice"))),
        make_snapshot(inventory=cyclic_inventory()),
    ],
)
def test_invalid_candidate_snapshot_is_rejected_before_storage(candidate):
    """Empty, anchorless, and cyclic candidates must not replace last-good state."""
    with pytest.raises(EngineeringSnapshotError):
        validate_engineering_snapshot(candidate)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda data: data.update(nodes={}), "nodes"),
        (lambda data: data["nodes"][0].update(kind="invented"), "kind"),
        (lambda data: data["nodes"][1].update(parent_key="missing"), "parent"),
        (lambda data: data["nodes"][0].update(parent_key=data["nodes"][1]["key"]), "cycle"),
        (lambda data: data["nodes"][1].update(uuid=data["nodes"][0]["uuid"]), "uuid"),
        (lambda data: data["nodes"][1].update(key=data["nodes"][0]["key"]), "key"),
        (lambda data: data["rows"][0].update(semantic_platform={}), "platform"),
        (lambda data: data["rows"][1].update(safe_unit="unsafe-unit"), "unit"),
        (lambda data: data.update(read_sequence=nan), "read_sequence"),
        (lambda data: data["nodes"][1].update(owner_identifier="foreign:device"), "scope"),
        (lambda data: data["nodes"].pop(), "rows"),
        (lambda data: data["nodes"][4].update(name="changed"), "digest"),
        (lambda data: data.update(generation_id="gen:" + "0" * 64), "generation"),
    ],
)
def test_snapshot_decoder_fails_closed_on_malformed_or_inconsistent_data(mutation, match):
    """Each persisted invariant must be checked before an object is restored."""
    encoded = snapshot_to_dict(make_snapshot())
    mutation(encoded)

    with pytest.raises(EngineeringSnapshotError, match=match):
        snapshot_from_dict(encoded)


def test_snapshot_decoder_rejects_inconsistent_binding_proof():
    """A state UUID is valid only with an explicit event-capable proof."""
    encoded = snapshot_to_dict(make_snapshot())
    bound = next(item for item in encoded["rows"] if item["state_uuid"])
    bound["event_binding_proven"] = False

    with pytest.raises(EngineeringSnapshotError, match="binding"):
        snapshot_from_dict(encoded)


def test_snapshot_decoder_rejects_network_material_in_presentation():
    """Safe display fields must not persist a network address."""
    encoded = snapshot_to_dict(make_snapshot())
    encoded["nodes"][0]["name"] = "203.0.113.42"

    with pytest.raises(EngineeringSnapshotError, match="network"):
        snapshot_from_dict(encoded)


def test_revision_digest_and_generation_have_separate_stable_inputs():
    """Capture/presentation changes must not masquerade as config observations."""
    snapshot = make_snapshot()
    later_source = replace(
        snapshot.source,
        title="Different presentation",
        model="Different model",
        source_archive="sps_999_20270101000000.zip",
    )

    assert engineering_configuration_revision_id(later_source) == snapshot.configuration_revision_id
    assert engineering_safe_content_digest(later_source, snapshot.nodes, snapshot.rows) == snapshot.safe_content_digest
    assert next_engineering_read_sequence(None) == 1
    assert next_engineering_read_sequence(snapshot) == 2
    second = engineering_generation_id(
        snapshot.source,
        snapshot.nodes,
        snapshot.rows,
        read_sequence=2,
    )
    assert second != snapshot.generation_id


def test_revision_falls_back_to_archive_version_and_timestamp_without_capture_time():
    """A missing scalar revision uses deterministic archive metadata only."""
    context = replace(source(), loxapp_last_modified=None)
    changed_capture = replace(make_snapshot(last_modified=None), captured_at=datetime.now(UTC))

    assert engineering_configuration_revision_id(context) == changed_capture.configuration_revision_id
    assert snapshot_to_dict(changed_capture)["configuration_revision_id"] == (
        make_snapshot(last_modified=None).configuration_revision_id
    )


def test_state_envelope_round_trip_is_immutable_and_generation_consistent():
    """Recovery cursors and impact intent must persist atomically with the snapshot."""
    snapshot = make_snapshot()
    plan = EngineeringImpactPlan(
        snapshot.generation_id,
        (
            EngineeringEntityImpact(
                "weather-value",
                ("sensor.weather_value",),
                "platform_changed",
                {"automation": ("automation.sample",)},
            ),
        ),
    )
    state = StoredEngineeringState(
        snapshot=snapshot,
        registry_applied_generation=snapshot.generation_id,
        pending_impact_plan=plan,
        impact_published_generation=snapshot.generation_id,
        managed_area_ids={"serial-a:weather-server": "area-1"},
    )

    restored = stored_state_from_dict(stored_state_to_dict(state), "entry-a")

    assert stored_state_to_dict(restored) == stored_state_to_dict(state)
    assert isinstance(restored.managed_area_ids, MappingProxyType)
    assert isinstance(restored.pending_impact_plan.impacts[0].references, MappingProxyType)
    with pytest.raises(TypeError):
        restored.managed_area_ids["new"] = "area-2"


def test_impact_plan_detaches_all_caller_owned_collections():
    """A pending recovery plan must not change when caller lists are mutated."""
    entity_ids = ["sensor.weather_value"]
    referenced = ["automation.sample"]
    impacts = [
        EngineeringEntityImpact(
            "weather-value",
            entity_ids,
            "removed",
            {"automation": referenced},
        )
    ]

    plan = EngineeringImpactPlan(make_snapshot().generation_id, impacts)
    entity_ids.append("sensor.changed")
    referenced.append("automation.changed")
    impacts.clear()

    assert plan.impacts[0].entity_ids == ("sensor.weather_value",)
    assert plan.impacts[0].references == {"automation": ("automation.sample",)}


def test_state_envelope_rejects_cross_entry_or_inconsistent_pending_plan():
    """A store key may not load another source or an unrelated recovery intent."""
    snapshot = make_snapshot()
    encoded = stored_state_to_dict(StoredEngineeringState(snapshot=snapshot))
    encoded["pending_impact_plan"] = {
        "generation_id": "gen:" + "0" * 64,
        "impacts": [],
    }

    with pytest.raises(EngineeringSnapshotError, match="impact"):
        stored_state_from_dict(encoded, "entry-a")
    with pytest.raises(EngineeringSnapshotError, match="entry"):
        stored_state_from_dict(
            stored_state_to_dict(StoredEngineeringState(snapshot=snapshot)),
            "entry-b",
        )


def test_state_envelope_rejects_non_scalar_impact_enum():
    """Unhashable JSON shapes must fail with the bounded snapshot error."""
    snapshot = make_snapshot()
    state = StoredEngineeringState(
        snapshot=snapshot,
        pending_impact_plan=EngineeringImpactPlan(
            snapshot.generation_id,
            (
                EngineeringEntityImpact(
                    "weather-value",
                    ("sensor.weather_value",),
                    "removed",
                    {},
                ),
            ),
        ),
    )
    encoded = stored_state_to_dict(state)
    encoded["pending_impact_plan"]["impacts"][0]["change_kind"] = []

    with pytest.raises(EngineeringSnapshotError, match="change_kind"):
        stored_state_from_dict(encoded, "entry-a")


def test_previous_direct_private_snapshot_shape_is_migrated_without_publication():
    """The former direct private payload must load into the v2 recovery envelope."""
    snapshot = make_snapshot()

    restored = stored_state_from_dict(snapshot_to_dict(snapshot), "entry-a")

    assert restored.snapshot is not None
    assert restored.snapshot.generation_id == snapshot.generation_id
    assert restored.registry_applied_generation is None
    assert restored.pending_impact_plan is None


@pytest.mark.anyio
async def test_store_uses_private_v2_entry_scoped_envelope(monkeypatch):
    """Persistence must use the exact private per-entry Home Assistant key."""
    calls: list[tuple[object, int, str, bool]] = []
    payloads: list[dict] = []

    class FakeStore:
        def __init__(self, hass, version, key, private=False, **kwargs):
            del kwargs
            calls.append((hass, version, key, private))

        async def async_load(self):
            return payloads[-1] if payloads else None

        async def async_save(self, payload):
            payloads.append(payload)

    monkeypatch.setattr(
        "custom_components.loxone.engineering_snapshot.EngineeringStateStore",
        FakeStore,
    )
    hass = object()
    state = StoredEngineeringState(snapshot=make_snapshot())

    await async_store_engineering_state(hass, state)
    restored = await async_load_engineering_state(hass, "entry-a")

    assert calls == [
        (
            hass,
            ENGINEERING_SNAPSHOT_STORAGE_VERSION,
            "loxone.engineering_snapshot.entry-a",
            True,
        ),
        (
            hass,
            ENGINEERING_SNAPSHOT_STORAGE_VERSION,
            "loxone.engineering_snapshot.entry-a",
            True,
        ),
    ]
    assert restored.snapshot.generation_id == state.snapshot.generation_id


def test_invalid_state_metadata_is_rejected_before_storage_encoding():
    """Area IDs and cursor tokens accept only bounded technical values."""
    snapshot = make_snapshot()
    state = StoredEngineeringState(
        snapshot=snapshot,
        registry_applied_generation="not a generation",
        managed_area_ids={"serial-a:device": "https://invalid.example"},
    )

    with pytest.raises(EngineeringSnapshotError):
        stored_state_to_dict(state)


def test_snapshot_capture_timestamp_must_be_aware_and_finite_shape():
    """A cache cannot accept a timezone-ambiguous capture timestamp."""
    snapshot = make_snapshot()
    with pytest.raises(EngineeringSnapshotError, match="captured_at"):
        validate_engineering_snapshot(replace(snapshot, captured_at=datetime(2026, 9, 13, 12)))


def test_candidate_row_version_must_match_the_source_version():
    """Cached entity metadata must derive from the committed source revision."""
    snapshot = make_snapshot()
    changed = replace(snapshot.rows[0], config_version=0)

    with pytest.raises(EngineeringSnapshotError, match="config_version"):
        validate_engineering_snapshot(replace(snapshot, rows=(changed, *snapshot.rows[1:])))
