"""Private, privacy-safe engineering snapshot persistence tests."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from math import nan
from pathlib import Path
from types import MappingProxyType

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.loxone.engineering_capabilities import (
    CapabilityState,
    EngineeringCapability,
    ExposureStatus,
    SafeRuntimeBindingDescriptor,
    resolve_engineering_capabilities,
    select_runtime_probe_candidates,
)
from custom_components.loxone.engineering_changes import (
    EngineeringEntityImpact,
    EngineeringImpactPlan,
)
from custom_components.loxone.engineering_config import parse_engineering_xml
from custom_components.loxone.engineering_entities import build_engineering_entity_specs
from custom_components.loxone.engineering_runtime import (
    EngineeringRuntimeBinding,
    EngineeringRuntimeInventory,
    binding_from_response,
)
from custom_components.loxone.engineering_snapshot import (
    ENGINEERING_SNAPSHOT_STORAGE_VERSION,
    EngineeringSnapshot,
    EngineeringSnapshotError,
    EngineeringStateStore,
    EngineeringStoreCommitError,
    EngineeringStoreCommitOutcome,
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
from custom_components.loxone.engineering_topology import resolve_engineering_topology
from tests.engineering_fixtures import (
    cyclic_inventory,
    element,
    inventory_of,
    make_snapshot,
    numeric_binding,
    reference_link_inventory,
    source,
)


def test_placement_roundtrip_digest_and_operational_privacy(monkeypatch):
    """Placement must change private integrity, never automation semantics."""
    from custom_components.loxone.engineering_changes import diff_engineering_snapshots
    from custom_components.loxone import config_impact
    from custom_components.loxone.diagnostics import async_get_config_entry_diagnostics
    from types import SimpleNamespace
    from tests.engineering_fixtures import SYNTHETIC_PARSE_CONTEXT

    snapshots = []
    for cabinet in ("Cabinet A", "Cabinet B"):
        parsed = parse_engineering_xml(
            (
                f'<C Type="LoxLIVE" U="ms" Installation="Synthetic installation">'
                f'<C Type="FutureDevice" U="device" SwitchBoard="{cabinet}" '
                'SwitchBoardRow="0" SwitchBoardPos="4" /></C>'
            ).encode(),
            **SYNTHETIC_PARSE_CONTEXT,
        )
        candidate = make_snapshot(inventory=parsed)
        payload = snapshot_to_dict(candidate)
        assert payload["nodes"][0]["placement"] == {"installation": "Synthetic installation"}
        assert payload["nodes"][1]["placement"] == {"switchboard": cabinet, "row": 0, "position": 4}
        restored = snapshot_from_dict(payload)
        assert snapshot_to_dict(restored) == payload
        assert all(not node.element.attributes for node in restored.nodes)
        assert "placement" not in json.dumps([node.element.as_public_dict() for node in restored.nodes])
        snapshots.append(restored)
    assert snapshots[0].safe_content_digest != snapshots[1].safe_content_digest
    assert snapshots[0].generation_id != snapshots[1].generation_id
    assert diff_engineering_snapshots(*snapshots).is_empty
    entry = SimpleNamespace(entry_id="entry-a")
    coordinator = SimpleNamespace(engineering_snapshot=snapshots[1], engineering_runtime=None)
    hass = SimpleNamespace(data={"loxone": {"entry-a": coordinator}})
    diagnostics = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    assert "Cabinet" not in json.dumps(diagnostics)
    assert "placement" not in json.dumps(diagnostics)
    monkeypatch.setattr(config_impact.er, "async_get", lambda _hass: object())
    impact = asyncio.run(config_impact.async_find_engineering_change_impacts(hass, entry, *snapshots))
    assert impact.impacts == ()
    legacy = snapshot_to_dict(make_snapshot())
    assert all("placement" not in item for item in legacy["nodes"])
    assert snapshot_to_dict(snapshot_from_dict(legacy)) == legacy
    for malformed in (
        {"row": True},
        {"position": 2.5},
        {"row": "2"},
        {"row": 1000},
        {"switchboard": "x" * 81},
        {"cabinet": "Cabinet C"},
    ):
        tampered = json.loads(json.dumps(payload))
        tampered["nodes"][1]["placement"] = malformed
        with pytest.raises(EngineeringSnapshotError):
            snapshot_from_dict(tampered)


def test_room_identity_survives_safe_projection_and_rename():
    """Dropping opaque room identity would turn a rename into a new mapping."""
    for name in ("Workshop", "Studio"):
        snapshot = make_snapshot(
            inventory=inventory_of(
                element("ms", "LoxLIVE", room=None),
                replace(element("device", "TreeDevice", parent_uuid="ms", room=name), room_uuid="room-a"),
            )
        )
        restored = snapshot_from_dict(snapshot_to_dict(snapshot))
        device = next(node for node in restored.nodes if node.element.uuid == "device")
        assert device.element.room_uuid == "room-a"
        assert device.element.room == name


def test_v2_state_migration_preserves_recovery_cursors_and_unknown_rooms():
    """A schema migration must not invent room IDs or invalidate old cursors."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            replace(element("ms", "LoxLIVE", room=None), room_uuid=None),
            replace(element("device", "TreeDevice", parent_uuid="ms", room="Workshop"), room_uuid=None),
        )
    )
    encoded = stored_state_to_dict(
        StoredEngineeringState(
            snapshot=snapshot,
            registry_applied_generation=snapshot.generation_id,
            impact_published_generation=snapshot.generation_id,
            managed_area_ids={"serial-a:device": "area-a"},
        )
    )
    legacy = {
        key: encoded[key]
        for key in (
            "snapshot",
            "registry_applied_generation",
            "impact_published_generation",
            "pending_impact_plan",
            "managed_area_ids",
        )
    }
    legacy["schema_version"] = 2
    restored = stored_state_from_dict(legacy, "entry-a")
    assert getattr(restored, "room_area_mappings", None) == {}
    assert restored.registry_applied_generation == snapshot.generation_id
    assert restored.impact_published_generation == snapshot.generation_id
    assert restored.managed_area_ids == {"serial-a:device": "area-a"}
    assert all(node.element.room_uuid is None for node in restored.snapshot.nodes)
    wire = stored_state_to_dict(restored)
    assert stored_state_to_dict(stored_state_from_dict(wire, "entry-a")) == wire


@pytest.mark.parametrize("field,value", [("room_area_mappings", None), ("room_area_mapping_scope", 7)])
def test_room_state_decoder_rejects_invalid_shapes(field, value):
    """Malformed persisted containers must fail at the snapshot error boundary."""
    wire = stored_state_to_dict(StoredEngineeringState(make_snapshot()))
    wire[field] = value
    with pytest.raises(EngineeringSnapshotError):
        stored_state_from_dict(wire, "entry-a")


def test_room_state_is_detached_scoped_and_integrity_checked():
    """Caller mutations and provider-changing replacements cannot redirect durable authority."""
    from custom_components.loxone.engineering_snapshot import (
        EngineeringAreaBatchGroup,
        EngineeringAreaBatchIntent,
        EngineeringAreaBatchMember,
        EngineeringAreaDecision,
    )

    snapshot = make_snapshot()
    mappings = {"room-a": "area-a"}
    tokens = ["b" * 64, "a" * 64]
    members = [
        EngineeringAreaBatchMember("b" * 64, "serial-a:second", None),
        EngineeringAreaBatchMember("a" * 64, "serial-a:device", None, True),
    ]
    groups = [
        EngineeringAreaBatchGroup(
            EngineeringAreaDecision("room-a", "use_existing", area_id="area-a", conflict_tokens=tokens), members
        )
    ]
    pending = EngineeringAreaBatchIntent("entry-a", "serial-a", snapshot.generation_id, groups)
    state = StoredEngineeringState(snapshot, room_area_mappings=mappings, pending_area_batch=pending)
    mappings.clear()
    tokens.clear()
    members.clear()
    groups.clear()
    wire = stored_state_to_dict(state)
    assert stored_state_to_dict(stored_state_from_dict(wire, "entry-a")) == wire
    assert state.room_area_mappings == {"room-a": "area-a"}
    assert len(state.pending_area_batch.groups[0].members) == 2
    foreign = _rehash(replace(snapshot, source=replace(snapshot.source, entry_id="entry-b")))
    with pytest.raises(EngineeringSnapshotError):
        stored_state_to_dict(replace(state, snapshot=foreign))
    wire["room_area_mappings"]["room-a"] = "area-b"
    with pytest.raises(EngineeringSnapshotError):
        stored_state_from_dict(wire, "entry-a")


def _rehash(snapshot):
    """Recompute integrity tokens after an intentional semantic mutation."""
    return replace(
        snapshot,
        safe_content_digest=engineering_safe_content_digest(
            snapshot.source,
            snapshot.nodes,
            snapshot.rows,
        ),
        generation_id=engineering_generation_id(
            snapshot.source,
            snapshot.nodes,
            snapshot.rows,
            read_sequence=snapshot.read_sequence,
        ),
    )


def _replace_node(snapshot, node_key, transform):
    """Replace a graph node and keep its row reference internally coherent."""
    old_node = next(node for node in snapshot.nodes if node.element.key == node_key)
    new_node = transform(old_node)
    nodes = tuple(new_node if node is old_node else node for node in snapshot.nodes)
    rows = tuple(replace(row, node=new_node) if row.node is old_node else row for row in snapshot.rows)
    return _rehash(replace(snapshot, nodes=nodes, rows=rows))


def _replace_row(snapshot, node_key, transform):
    """Replace one capability row and recompute the safe integrity tokens."""
    rows = tuple(transform(row) if row.node.element.key == node_key else row for row in snapshot.rows)
    return _rehash(replace(snapshot, rows=rows))


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
                ("area-1", "area-2"),
                "area-1",
                "area-2",
                "Workshop",
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
    assert restored.pending_impact_plan.impacts[0].target_area_ids == (
        "area-1",
        "area-2",
    )
    assert restored.pending_impact_plan.impacts[0].area_from_id == "area-1"
    assert restored.pending_impact_plan.impacts[0].area_to_id == "area-2"
    assert restored.pending_impact_plan.impacts[0].area_to_name == "Workshop"
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


def test_prior_impact_payload_without_area_targets_remains_loadable():
    """The Task 8 field is optional when restoring a Task 7 state envelope."""
    snapshot = make_snapshot()
    state = StoredEngineeringState(
        snapshot=snapshot,
        registry_applied_generation=snapshot.generation_id,
        pending_impact_plan=EngineeringImpactPlan(
            snapshot.generation_id,
            (
                EngineeringEntityImpact(
                    "weather-value",
                    ("sensor.weather_value",),
                    "removed",
                    {"automation": ("automation.sample",)},
                ),
            ),
        ),
    )
    encoded = stored_state_to_dict(state)
    encoded["pending_impact_plan"]["impacts"][0].pop("target_area_ids")

    restored = stored_state_from_dict(encoded, "entry-a")

    assert restored.snapshot.generation_id == snapshot.generation_id
    assert restored.pending_impact_plan.impacts[0].target_area_ids == ()
    assert restored.pending_impact_plan.impacts[0].area_from_id is None
    assert restored.pending_impact_plan.impacts[0].area_to_id is None
    assert restored.pending_impact_plan.impacts[0].area_to_name is None


def test_prior_area_impact_payload_without_owner_replay_evidence_is_dropped():
    """Legacy room metadata evidence cannot be replayed as an owner transition."""
    snapshot = make_snapshot()
    state = StoredEngineeringState(
        snapshot=snapshot,
        pending_impact_plan=EngineeringImpactPlan(
            snapshot.generation_id,
            (
                EngineeringEntityImpact(
                    "weather-value",
                    (),
                    "area_changed",
                    {"automation": ("automation.sample",)},
                    ("area-1",),
                ),
            ),
        ),
    )
    encoded = stored_state_to_dict(state)
    impact = encoded["pending_impact_plan"]["impacts"][0]
    impact.pop("area_from_id", None)
    impact.pop("area_to_id", None)
    impact.pop("area_to_name", None)

    restored = stored_state_from_dict(encoded, "entry-a")

    assert restored.pending_impact_plan.impacts == ()


def test_impact_replay_fields_do_not_change_snapshot_generation_hash():
    """Private recovery evidence is outside immutable snapshot generation identity."""
    snapshot = make_snapshot()
    generation = snapshot.generation_id
    plan = EngineeringImpactPlan(
        generation,
        (
            EngineeringEntityImpact(
                "serial-a:device",
                (),
                "area_changed",
                {"automation": ("automation.sample",)},
                ("area-1",),
                "area-1",
                None,
                "Workshop",
            ),
        ),
    )

    restored = stored_state_from_dict(
        stored_state_to_dict(StoredEngineeringState(snapshot=snapshot, pending_impact_plan=plan)),
        "entry-a",
    )

    assert restored.snapshot.generation_id == generation


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

        async def async_save_acknowledged(self, payload):
            payloads.append(payload)
            return EngineeringStoreCommitOutcome.COMMITTED

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


def test_snapshot_strips_forbidden_role_presentation_but_retains_operational_labels():
    """Project/provider/user/location labels must not enter the safe projection."""
    inventory = inventory_of(
        element("project", "Document", title="PROJECT-MARKER", room=None),
        element(
            "ms",
            "LoxLIVE",
            parent_uuid="project",
            title="PROVIDER-MARKER",
            room=None,
        ),
        element("place", "Place", parent_uuid="ms", title="LOCATION-MARKER"),
        element(
            None,
            "TreeCaption",
            key="xml:000001",
            parent_key="ms",
            title="Branch A",
            room=None,
        ),
        element(
            "device",
            "TreeDevice",
            parent_key="xml:000001",
            title="ST-F07",
            room="Office",
        ),
        element("user", "User", parent_uuid="ms", title="USER-MARKER"),
        element("service", "WeatherServer", title="Weather service", room=None),
    )

    payload = snapshot_to_dict(make_snapshot(inventory=inventory))
    rendered = json.dumps(payload)

    assert all(
        marker not in rendered
        for marker in (
            "PROJECT-MARKER",
            "PROVIDER-MARKER",
            "LOCATION-MARKER",
            "USER-MARKER",
        )
    )
    assert "Branch A" in rendered
    assert "ST-F07" in rendered
    assert "Office" in rendered
    assert "Weather service" in rendered
    restored = snapshot_from_dict(payload)
    assert all(
        marker not in json.dumps(snapshot_to_dict(restored))
        for marker in (
            "PROJECT-MARKER",
            "PROVIDER-MARKER",
            "LOCATION-MARKER",
            "USER-MARKER",
        )
    )


def test_decoder_rejects_checksum_consistent_forbidden_role_presentation():
    """A locally edited payload cannot restore project titles through safe fields."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("project", "Document", title="PROJECT-ORIGINAL", room=None),
            element("ms", "LoxLIVE", parent_uuid="project", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07"),
        )
    )
    payload = snapshot_to_dict(snapshot)
    document = next(node for node in payload["nodes"] if node["key"] == "project")
    document["name"] = "FORBIDDEN-PROJECT-MARKER"
    for node in payload["nodes"]:
        node["topology_path"] = [
            "FORBIDDEN-PROJECT-MARKER" if item == "Document" else item for item in node["topology_path"]
        ]

    with pytest.raises(EngineeringSnapshotError, match=r"presentation|privacy"):
        snapshot_from_dict(payload)


def test_safe_digest_ignores_forbidden_role_titles_but_tracks_device_labels():
    """Only the role-aware safe projection may influence content identity."""

    def projected(project_title: str, provider_title: str, device_title: str):
        return make_snapshot(
            inventory=inventory_of(
                element("project", "Document", title=project_title, room=None),
                element(
                    "ms",
                    "LoxLIVE",
                    parent_uuid="project",
                    title=provider_title,
                    room=None,
                ),
                element(
                    "device",
                    "TreeDevice",
                    parent_uuid="ms",
                    title=device_title,
                ),
            )
        )

    first = projected("PROJECT-A", "PROVIDER-A", "ST-F07")
    forbidden_changed = projected("PROJECT-B", "PROVIDER-B", "ST-F07")
    device_changed = projected("PROJECT-B", "PROVIDER-B", "ST-F07 renamed")

    assert first.safe_content_digest == forbidden_changed.safe_content_digest
    assert first.safe_content_digest != device_changed.safe_content_digest


def test_uuidless_branch_and_provider_service_round_trip_authoritatively():
    """Safe structural ancestry and a unique UUID-less service remain reconstructable."""
    inventory = inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element(
            None,
            "TreeCaption",
            key="xml:000001",
            parent_key="ms",
            title="Branch A",
            room=None,
        ),
        element(
            "device",
            "TreeDevice",
            parent_key="xml:000001",
            title="ST-F07",
        ),
        element(
            None,
            "WeatherServer",
            key="xml:000002",
            title="Weather service",
            room=None,
        ),
        element(
            "weather",
            "WeatherData",
            parent_key="xml:000002",
            io_name="WDC1",
        ),
    )
    snapshot = make_snapshot(
        inventory=inventory,
        runtime=EngineeringRuntimeInventory(bindings=(numeric_binding("weather", 18.5, "WeatherData"),)),
    )

    restored = snapshot_from_dict(snapshot_to_dict(snapshot))
    weather = next(row for row in restored.rows if row.node.element.key == "weather")

    assert weather.node.device_identifier == "serial-a:service:weatherserver"
    assert weather.node.owner_key == "xml:000002"
    assert next(node for node in restored.nodes if node.element.key == "device").topology_path[-2:] == (
        "Branch A",
        "ST-F07",
    )


def test_snapshot_rejects_uuidless_miniserver_without_stable_anchor():
    """A type assertion alone cannot establish the source Miniserver anchor."""
    candidate = make_snapshot(
        inventory=inventory_of(
            element(None, "LoxLIVE", key="xml:000001", title="Miniserver", room=None),
        )
    )

    with pytest.raises(EngineeringSnapshotError, match="anchor|complete"):
        validate_engineering_snapshot(candidate)


def test_snapshot_rejects_checksum_consistent_owner_identifier_forgery():
    """An in-scope-looking identifier must still match the resolved owner key."""
    snapshot = make_snapshot()
    candidate = _replace_node(
        snapshot,
        "weather-value",
        lambda node: replace(node, device_identifier="serial-a:missing-owner"),
    )

    with pytest.raises(EngineeringSnapshotError, match="topology|owner"):
        validate_engineering_snapshot(candidate)


def test_snapshot_rejects_checksum_consistent_via_identifier_forgery():
    """A scoped existing via identifier must still be the nearest eligible ancestor."""
    snapshot = make_snapshot(inventory=reference_link_inventory())
    candidate = _replace_node(
        snapshot,
        "air-device",
        lambda node: replace(
            node,
            via_device_identifier="serial-a:wire-extension",
        ),
    )

    with pytest.raises(EngineeringSnapshotError, match=r"topology|via"):
        validate_engineering_snapshot(candidate)


def test_snapshot_rejects_checksum_consistent_sensitive_type_forgery():
    """Type-derived sensitivity and kind cannot be overridden by cached booleans."""
    snapshot = make_snapshot()
    candidate = _replace_node(
        snapshot,
        "weather-value",
        lambda node: replace(
            node,
            element=replace(node.element, loxone_type="NfcCode"),
            sensitive=False,
        ),
    )

    with pytest.raises(EngineeringSnapshotError, match="sensitive|topology|kind"):
        validate_engineering_snapshot(candidate)


def test_snapshot_rejects_unsupported_binding_method_with_valid_checksums():
    """Only the proven all/state read contracts may survive persistence."""
    snapshot = make_snapshot()
    candidate = _replace_row(
        snapshot,
        "weather-value",
        lambda row: replace(
            row,
            capability=EngineeringCapability(
                CapabilityState.READABLE,
                "sensor",
                ExposureStatus.INVENTORY_ONLY,
                "readable_rebind_only",
            ),
            binding=SafeRuntimeBindingDescriptor(
                "unsupported_read",
                "number",
                None,
                None,
                False,
            ),
        ),
    )

    with pytest.raises(EngineeringSnapshotError, match="method|binding"):
        validate_engineering_snapshot(candidate)


def test_snapshot_rejects_unproven_state_uuid_with_valid_checksums():
    """A scalar rebind hint must never smuggle an unproven event-state UUID."""
    snapshot = make_snapshot()
    candidate = _replace_row(
        snapshot,
        "weather-value",
        lambda row: replace(
            row,
            capability=EngineeringCapability(
                CapabilityState.READABLE,
                "sensor",
                ExposureStatus.INVENTORY_ONLY,
                "readable_rebind_only",
            ),
            binding=SafeRuntimeBindingDescriptor(
                "uuid_state",
                "number",
                None,
                "unproven-state",
                False,
            ),
        ),
    )

    with pytest.raises(EngineeringSnapshotError, match="proof|state_uuid|binding"):
        validate_engineering_snapshot(candidate)


def test_snapshot_rejects_boolean_binding_for_numeric_sensor_semantics():
    """A numeric sensor platform cannot be reconstructed from a boolean descriptor."""
    snapshot = make_snapshot()
    candidate = _replace_row(
        snapshot,
        "weather-value",
        lambda row: replace(
            row,
            binding=replace(row.binding, value_kind="boolean"),
        ),
    )

    with pytest.raises(EngineeringSnapshotError, match=r"binding|semantic"):
        validate_engineering_snapshot(candidate)


@pytest.mark.parametrize("technical_type", ["Actor", "UnknownType"])
def test_snapshot_rejects_prepared_exposure_for_output_or_unknown_type(technical_type):
    """Prepared entities are limited to safe, read-only sensor semantics."""
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("target", technical_type, parent_uuid="ms", io_name="IO1"),
        )
    )
    candidate = _replace_row(
        snapshot,
        "target",
        lambda row: replace(
            row,
            capability=EngineeringCapability(
                CapabilityState.READABLE,
                "sensor",
                ExposureStatus.PREPARED_DISABLED,
                "read_only_event_binding_proven",
            ),
            semantic_platform="sensor",
            binding=SafeRuntimeBindingDescriptor(
                "uuid_all",
                "number",
                None,
                "target-state",
                True,
            ),
        ),
    )

    with pytest.raises(EngineeringSnapshotError, match="prepared|semantic|topology"):
        validate_engineering_snapshot(candidate)


def test_snapshot_rejects_writable_prepared_assertion_with_valid_checksums():
    """Persistence may never upgrade the read-only feature to writable."""
    snapshot = make_snapshot()
    candidate = _replace_row(
        snapshot,
        "weather-value",
        lambda row: replace(
            row,
            capability=replace(row.capability, state=CapabilityState.WRITABLE),
        ),
    )

    with pytest.raises(EngineeringSnapshotError, match="writable|capability"):
        validate_engineering_snapshot(candidate)


def test_valid_numeric_and_binary_bindings_restore_as_unavailable_specs():
    """Safe read bindings remain reconstructable without persisting runtime values."""
    from custom_components.loxone.engineering_runtime import (
        EngineeringRuntimeBinding,
        EngineeringRuntimeInventory,
    )

    inventory = inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("io", "IoData", parent_uuid="ms", room=None),
        element("analog", "VoltageIn", parent_uuid="io", io_name="AI1"),
        element("binary", "DigitalIn", parent_uuid="io", io_name="I1"),
    )
    runtime = EngineeringRuntimeInventory(
        bindings=(
            EngineeringRuntimeBinding(
                engineering_uuid="analog",
                io_name="AI1",
                loxone_type="VoltageIn",
                title="Analog",
                room="Office",
                suggested_platform=None,
                status="bound",
                binding_method="uuid_all",
                value_kind="number",
                numeric_value=1.5,
                state_uuid="analog-state",
            ),
            EngineeringRuntimeBinding(
                engineering_uuid="binary",
                io_name="I1",
                loxone_type="DigitalIn",
                title="Binary",
                room="Office",
                suggested_platform=None,
                status="bound",
                binding_method="uuid_all",
                value_kind="boolean",
                numeric_value=1.0,
                state_uuid="binary-state",
            ),
        )
    )
    restored = snapshot_from_dict(snapshot_to_dict(make_snapshot(inventory=inventory, runtime=runtime)))

    specs = build_engineering_entity_specs(restored.rows, None)

    assert {(spec.platform, spec.available) for spec in specs} == {
        ("sensor", False),
        ("binary_sensor", False),
    }


def test_state_rejects_publication_ahead_of_registry_application():
    """A published current generation requires the same registry application proof."""
    snapshot = make_snapshot()
    impossible = StoredEngineeringState(
        snapshot=snapshot,
        registry_applied_generation=None,
        impact_published_generation=snapshot.generation_id,
    )

    with pytest.raises(EngineeringSnapshotError, match="publication|registry"):
        stored_state_to_dict(impossible)


def test_state_round_trips_each_supported_recovery_phase():
    """Pending, registry-applied, fully-applied, and migrated phases are durable."""
    snapshot = make_snapshot()
    previous = "gen:" + "1" * 64
    plan = EngineeringImpactPlan(snapshot.generation_id, ())
    phases = (
        StoredEngineeringState(snapshot=snapshot, pending_impact_plan=plan),
        StoredEngineeringState(
            snapshot=snapshot,
            registry_applied_generation=previous,
            pending_impact_plan=plan,
            impact_published_generation=previous,
        ),
        StoredEngineeringState(
            snapshot=snapshot,
            registry_applied_generation=snapshot.generation_id,
            pending_impact_plan=plan,
            impact_published_generation=previous,
        ),
        StoredEngineeringState(
            snapshot=snapshot,
            registry_applied_generation=snapshot.generation_id,
            pending_impact_plan=plan,
            impact_published_generation=snapshot.generation_id,
        ),
        StoredEngineeringState(snapshot=snapshot),
    )

    for phase in phases:
        assert stored_state_to_dict(
            stored_state_from_dict(stored_state_to_dict(phase), "entry-a")
        ) == stored_state_to_dict(phase)


def test_snapshot_detaches_nested_path_and_attribute_aliases():
    """Caller mutation cannot alter committed candidate content after construction."""
    snapshot = make_snapshot()
    path = ["Miniserver", "Weather"]
    attributes = {"technical": "value"}
    node = snapshot.nodes[0]
    mutable_node = replace(
        node,
        element=replace(node.element, attributes=attributes),
        topology_path=path,
    )
    rows = tuple(replace(row, node=mutable_node) if row.node is node else row for row in snapshot.rows)
    candidate = EngineeringSnapshot(
        snapshot.source,
        (mutable_node, *snapshot.nodes[1:]),
        rows,
        snapshot.configuration_revision_id,
        snapshot.safe_content_digest,
        snapshot.read_sequence,
        snapshot.generation_id,
        snapshot.captured_at,
    )

    path.append("MUTATED")
    attributes["technical"] = "MUTATED"

    assert candidate.nodes[0].topology_path == ("Miniserver", "Weather")
    assert candidate.nodes[0].element.attributes == {"technical": "value"}
    assert isinstance(candidate.nodes[0].element.attributes, MappingProxyType)


def test_snapshot_rejects_nested_mutable_attribute_values():
    """Arbitrary nested raw-attribute aliases cannot enter a frozen candidate."""
    snapshot = make_snapshot()
    node = replace(
        snapshot.nodes[0],
        element=replace(snapshot.nodes[0].element, attributes={"unsafe": []}),
    )

    with pytest.raises(EngineeringSnapshotError, match="mutable"):
        EngineeringSnapshot(
            snapshot.source,
            (node, *snapshot.nodes[1:]),
            snapshot.rows,
            snapshot.configuration_revision_id,
            snapshot.safe_content_digest,
            snapshot.read_sequence,
            snapshot.generation_id,
            snapshot.captured_at,
        )


def _real_engineering_store(hass, entry_id="entry-a", **kwargs):
    return EngineeringStateStore(
        hass,
        ENGINEERING_SNAPSHOT_STORAGE_VERSION,
        f"loxone.engineering_snapshot.{entry_id}",
        private=True,
        **kwargs,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("old_version", [1, 2])
async def test_real_store_is_atomic_acknowledged_and_migrates_legacy(tmp_path, old_version):
    """Cold HA Store migration preserves old safe hashes and recovery cursors."""
    hass = HomeAssistant(str(tmp_path))
    snapshot = make_snapshot(
        inventory=inventory_of(
            replace(element("ms", "LoxLIVE", room=None), room_uuid=None),
            replace(element("device", "TreeDevice", parent_uuid="ms", room="Workshop"), room_uuid=None),
        )
    )
    legacy = snapshot_to_dict(snapshot)
    if old_version == 2:
        legacy = {
            "schema_version": 2,
            "snapshot": legacy,
            "registry_applied_generation": snapshot.generation_id,
            "impact_published_generation": snapshot.generation_id,
            "pending_impact_plan": {"generation_id": snapshot.generation_id, "impacts": []},
            "managed_area_ids": {"serial-a:device": "area-a"},
        }
    legacy_store = Store(
        hass,
        old_version,
        "loxone.engineering_snapshot.entry-a",
        private=True,
        atomic_writes=True,
    )
    await legacy_store.async_save(legacy)

    migrated = _real_engineering_store(hass)
    restored = stored_state_from_dict(await migrated.async_load(), "entry-a")
    raw_current = await Store(
        hass,
        ENGINEERING_SNAPSHOT_STORAGE_VERSION,
        "loxone.engineering_snapshot.entry-a",
        private=True,
    ).async_load()

    assert restored.snapshot.generation_id == snapshot.generation_id
    assert migrated._atomic_writes is True
    assert raw_current["schema_version"] == ENGINEERING_SNAPSHOT_STORAGE_VERSION
    assert all(node.element.room_uuid is None for node in restored.snapshot.nodes)
    if old_version == 2:
        assert restored.registry_applied_generation == snapshot.generation_id
        assert restored.impact_published_generation == snapshot.generation_id
        assert restored.pending_impact_plan.generation_id == snapshot.generation_id
        assert restored.managed_area_ids == {"serial-a:device": "area-a"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure_kind", "outcome"),
    [
        ("serialization", EngineeringStoreCommitOutcome.SERIALIZATION_FAILED),
        ("write", EngineeringStoreCommitOutcome.WRITE_FAILED),
        ("cancel", EngineeringStoreCommitOutcome.CANCELLED),
        ("unexpected", EngineeringStoreCommitOutcome.FAILED),
    ],
)
async def test_real_store_surfaces_write_failures_and_preserves_last_good(
    tmp_path,
    monkeypatch,
    failure_kind,
    outcome,
):
    """No failed/cancelled candidate may silently authorize downstream work."""
    hass = HomeAssistant(str(tmp_path))
    snapshot = make_snapshot()
    store = _real_engineering_store(hass)
    last_good = StoredEngineeringState(snapshot=snapshot)
    await store.async_save_acknowledged(stored_state_to_dict(last_good))
    changed = StoredEngineeringState(snapshot=make_snapshot(read_sequence=2))
    payload = stored_state_to_dict(changed)

    if failure_kind == "serialization":
        payload["invalid"] = object()
    elif failure_kind == "write":

        def fail_write(*args):
            del args
            raise WriteError("synthetic")

        monkeypatch.setattr(store, "_write_prepared_data", fail_write)
    else:
        failure = asyncio.CancelledError() if failure_kind == "cancel" else RuntimeError("synthetic")

        async def fail_async_write(data):
            del data
            raise failure

        monkeypatch.setattr(store, "_async_write_data", fail_async_write)
    with pytest.raises(EngineeringStoreCommitError) as raised:
        await store.async_save_acknowledged(payload)

    assert raised.value.outcome is outcome
    loaded = await _real_engineering_store(hass).async_load()
    assert stored_state_from_dict(loaded, "entry-a").snapshot.generation_id == snapshot.generation_id


@pytest.mark.anyio
async def test_real_store_surfaces_deferred_pending_write(tmp_path):
    """An acknowledged save never replaces a previously queued Store write."""
    hass = HomeAssistant(str(tmp_path))
    store = _real_engineering_store(hass)
    store.async_delay_save(lambda: {"pending": True}, 60)

    with pytest.raises(EngineeringStoreCommitError) as raised:
        await store.async_save_acknowledged(stored_state_to_dict(StoredEngineeringState(snapshot=make_snapshot())))

    assert raised.value.outcome is EngineeringStoreCommitOutcome.DEFERRED
    store._async_cleanup_delay_listener()
    store._async_cleanup_final_write_listener()


@pytest.mark.anyio
async def test_real_store_runs_commit_guard_after_waiting_for_serialization(
    tmp_path,
):
    """A synchronous admission guard runs inside the shared Store locks."""
    hass = HomeAssistant(str(tmp_path))
    store = _real_engineering_store(hass)
    checks = []

    def veto() -> None:
        checks.append("checked")
        raise RuntimeError("synthetic lifecycle veto")

    await store._commit_lock.acquire()
    task = asyncio.create_task(
        store.async_save_acknowledged(
            stored_state_to_dict(StoredEngineeringState(snapshot=make_snapshot())),
            before_commit=veto,
        )
    )
    await asyncio.sleep(0)
    assert checks == []
    store._commit_lock.release()
    with pytest.raises(RuntimeError, match="lifecycle veto"):
        await task
    assert checks == ["checked"]
    assert not Path(store.path).exists()

    assert (
        await store.async_save_acknowledged(stored_state_to_dict(StoredEngineeringState(snapshot=make_snapshot())))
        is EngineeringStoreCommitOutcome.COMMITTED
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("read_only", "core_state", "outcome"),
    [
        (True, CoreState.running, EngineeringStoreCommitOutcome.READ_ONLY),
        (False, CoreState.stopping, EngineeringStoreCommitOutcome.STOPPING),
    ],
)
async def test_real_store_surfaces_non_committing_modes(
    tmp_path,
    read_only,
    core_state,
    outcome,
):
    """Stopping and read-only stores are explicit non-commit outcomes."""
    hass = HomeAssistant(str(tmp_path))
    hass.set_state(core_state)
    store = _real_engineering_store(hass, read_only=read_only)

    with pytest.raises(EngineeringStoreCommitError) as raised:
        await store.async_save_acknowledged(stored_state_to_dict(StoredEngineeringState(snapshot=make_snapshot())))

    assert raised.value.outcome is outcome
    assert not Path(store.path).exists()


async def _wait_thread_event(event: threading.Event) -> None:
    """Wait without occupying a Home Assistant executor worker."""
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    pytest.fail("controlled Store writer did not reach the expected phase")


@pytest.mark.anyio
async def test_cancelled_same_store_save_keeps_serialization_until_write_settles(tmp_path, monkeypatch):
    """Caller cancellation before the executor starts cannot release write ownership."""
    hass = HomeAssistant(str(tmp_path))
    store = _real_engineering_store(hass)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = store._async_write_data

    async def blocked_write(data):
        entered.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(store, "_async_write_data", blocked_write)
    first = asyncio.create_task(
        store.async_save_acknowledged(stored_state_to_dict(StoredEngineeringState(make_snapshot())))
    )
    await entered.wait()
    first.cancel()
    retry = asyncio.create_task(
        store.async_save_acknowledged(stored_state_to_dict(StoredEngineeringState(make_snapshot(read_sequence=2))))
    )
    await asyncio.sleep(0)

    assert not first.done()
    assert not retry.done()
    release.set()
    with pytest.raises(EngineeringStoreCommitError) as raised:
        await first
    assert raised.value.outcome is EngineeringStoreCommitOutcome.CANCELLED
    assert raised.value.settled_outcome is EngineeringStoreCommitOutcome.COMMITTED
    assert await retry is EngineeringStoreCommitOutcome.COMMITTED
    loaded = stored_state_from_dict(await _real_engineering_store(hass).async_load(), "entry-a")
    assert loaded.snapshot.read_sequence == 2


@pytest.mark.anyio
@pytest.mark.parametrize("block_after_replace", [False, True])
async def test_cancelled_wrapper_save_blocks_new_store_until_executor_settles(
    tmp_path,
    monkeypatch,
    block_after_replace,
):
    """New Store wrappers cannot let a retry overtake an in-flight cancelled writer."""
    hass = HomeAssistant(str(tmp_path))
    started = threading.Event()
    release = threading.Event()
    completion_order = []
    original = EngineeringStateStore._write_prepared_data

    def controlled_write(store, mode, json_data):
        raw = json.loads(json_data)
        sequence = raw["data"]["snapshot"]["read_sequence"]
        if sequence == 1:
            if block_after_replace:
                original(store, mode, json_data)
            started.set()
            release.wait(5)
            if not block_after_replace:
                original(store, mode, json_data)
        else:
            original(store, mode, json_data)
        completion_order.append(sequence)

    monkeypatch.setattr(EngineeringStateStore, "_write_prepared_data", controlled_write)
    first = asyncio.create_task(async_store_engineering_state(hass, StoredEngineeringState(make_snapshot())))
    await _wait_thread_event(started)
    first.cancel()
    retry = asyncio.create_task(
        async_store_engineering_state(hass, StoredEngineeringState(make_snapshot(read_sequence=2)))
    )
    await asyncio.sleep(0.05)

    assert not first.done()
    assert not retry.done()
    release.set()
    with pytest.raises(EngineeringStoreCommitError) as raised:
        await first
    assert raised.value.outcome is EngineeringStoreCommitOutcome.CANCELLED
    assert raised.value.settled_outcome is EngineeringStoreCommitOutcome.COMMITTED
    await retry
    assert completion_order == [1, 2]
    loaded = await async_load_engineering_state(hass, "entry-a")
    assert loaded.snapshot.read_sequence == 2


def test_nonbinary_digital_input_producer_snapshot_round_trip_is_inventory_only():
    """A safely bound non-binary DigitalIn remains inventory data, never prepared."""
    inventory = inventory_of(
        element("ms", "LoxLIVE", room=None),
        element("digital", "DigitalIn", parent_uuid="ms", io_name="I1"),
    )
    runtime = EngineeringRuntimeInventory(bindings=(numeric_binding("digital", 2.0, "DigitalIn"),))

    restored = snapshot_from_dict(snapshot_to_dict(make_snapshot(inventory=inventory, runtime=runtime)))
    row = next(item for item in restored.rows if item.node.element.uuid == "digital")

    assert row.capability.reason == "binary_semantics_not_proven"
    assert row.capability.exposure is ExposureStatus.INVENTORY_ONLY
    assert row.binding is not None
    assert build_engineering_entity_specs(restored.rows, None) == ()


def test_io_name_scalar_binding_producer_snapshot_round_trip_is_rebind_only():
    """The supported name fallback remains a scalar hint without event proof."""
    inventory = inventory_of(
        element("ms", "LoxLIVE", room=None),
        element("analog", "VoltageIn", parent_uuid="ms", io_name="AI1"),
    )
    runtime = EngineeringRuntimeInventory(
        bindings=(
            EngineeringRuntimeBinding(
                engineering_uuid="analog",
                io_name="AI1",
                loxone_type="VoltageIn",
                title="Analog",
                room="Office",
                suggested_platform=None,
                status="bound",
                binding_method="io_name_state",
                value_kind="number",
                numeric_value=2.5,
            ),
        )
    )

    restored = snapshot_from_dict(snapshot_to_dict(make_snapshot(inventory=inventory, runtime=runtime)))
    row = next(item for item in restored.rows if item.node.element.uuid == "analog")

    assert row.binding.binding_method == "io_name_state"
    assert row.binding.state_uuid is None
    assert row.binding.event_binding_proven is False
    assert row.capability.reason == "readable_rebind_only"
    assert build_engineering_entity_specs(restored.rows, None) == ()


def test_collision_suppressed_producer_row_round_trips_with_semantics_and_binding():
    """A source-scoped global collision suppresses exposure, not technical semantics."""
    produced = make_snapshot()
    collision = _replace_row(
        produced,
        "weather-value",
        lambda row: replace(
            row,
            capability=replace(
                row.capability,
                exposure=ExposureStatus.INVENTORY_ONLY,
                reason="entity_unique_id_owned_by_other_entry",
            ),
        ),
    )

    restored = snapshot_from_dict(snapshot_to_dict(collision))
    row = next(item for item in restored.rows if item.node.element.key == "weather-value")

    assert row.semantic_platform == "sensor"
    assert row.binding.event_binding_proven is True
    assert row.capability.exposure is ExposureStatus.INVENTORY_ONLY
    assert "weather-value" not in {spec.unique_id for spec in build_engineering_entity_specs(restored.rows, None)}


@pytest.mark.parametrize(
    "role",
    ["AccessCode", "Credential", "KeyCode", "NfcCode", "NfcTag", "Password", "Permission", "User"],
)
@pytest.mark.parametrize("with_type", [False, True])
def test_sensitive_xml_role_projection_is_private_and_reloadable(role, with_type):
    """Every authoritative XML sensitivity family survives its safe projection."""
    type_attribute = ' Type="Page"' if with_type else ""
    xml = (
        '<ControlList><C Type="LoxLIVE" U="ms" />'
        f'<{role} U="secret-{role}" Title="PRIVATE-MARKER" IName="PRIVATE-IO"{type_attribute} />'
        "</ControlList>"
    ).encode()
    inventory = parse_engineering_xml(
        xml,
        source_archive="sps_7_20260913120000.zip",
        config_version=7,
        config_timestamp=datetime(2026, 9, 13, 12, tzinfo=UTC),
    )
    context = source()
    resolved = resolve_engineering_topology(inventory, context)
    rows = resolve_engineering_capabilities(resolved, None)
    snapshot = EngineeringSnapshot(
        context,
        resolved.nodes,
        rows,
        engineering_configuration_revision_id(context),
        engineering_safe_content_digest(context, resolved.nodes, rows),
        1,
        engineering_generation_id(context, resolved.nodes, rows, read_sequence=1),
        datetime(2026, 9, 13, 12, tzinfo=UTC),
    )

    encoded = snapshot_to_dict(snapshot)
    restored = snapshot_from_dict(encoded)
    rendered = json.dumps(encoded)
    sensitive = next(item for item in restored.rows if item.node.element.uuid == f"secret-{role}")

    assert sensitive.capability.state is CapabilityState.SENSITIVE
    assert sensitive.node.sensitive is True
    assert "PRIVATE-MARKER" not in rendered
    assert "PRIVATE-IO" not in rendered


def test_uuidless_loxlive_with_uuid_backed_device_is_complete_and_reloadable():
    """Completeness needs LoxLIVE and a stable UUID, not the UUID on LoxLIVE itself."""
    inventory = inventory_of(
        element(None, "LoxLIVE", key="xml:fixture", room=None),
        element("device", "TreeDevice", parent_key="xml:fixture"),
    )

    restored = snapshot_from_dict(snapshot_to_dict(make_snapshot(inventory=inventory)))

    assert any(node.kind.value == "miniserver" for node in restored.nodes)
    assert any(node.element.uuid == "device" for node in restored.nodes)


@pytest.mark.parametrize("technical_type", ["SysVar", "VoltageIn", "WeatherData"])
@pytest.mark.parametrize("event_mapped", [False, True])
@pytest.mark.parametrize("boolean_value", [False, True])
def test_boolean_sensor_response_producer_round_trip_stays_conservative(
    technical_type,
    event_mapped,
    boolean_value,
):
    """A real boolean parse cannot abort a numeric sensor snapshot or prepare it unsafely."""
    target_uuid = f"target-{technical_type}"
    inventory = inventory_of(
        element("ms", "LoxLIVE", room=None),
        element(target_uuid, technical_type, parent_uuid="ms", io_name="AI1"),
    )
    parsed = binding_from_response(target_uuid, boolean_value)
    binding = replace(
        parsed,
        io_name="AI1",
        loxone_type=technical_type,
        binding_method="uuid_all" if event_mapped else "uuid_state",
        state_uuid=f"{target_uuid}-state" if event_mapped else None,
    )
    runtime = EngineeringRuntimeInventory(bindings=(binding,))

    restored = snapshot_from_dict(snapshot_to_dict(make_snapshot(inventory=inventory, runtime=runtime)))
    row = next(item for item in restored.rows if item.node.element.uuid == target_uuid)

    assert parsed.value_kind == "boolean"
    assert parsed.numeric_value == float(boolean_value)
    assert row.binding.value_kind == "boolean"
    assert row.capability.reason == "boolean_sensor_semantics_not_proven"
    assert row.capability.exposure is ExposureStatus.INVENTORY_ONLY
    assert build_engineering_entity_specs(restored.rows, runtime) == ()


@pytest.mark.parametrize(
    "technical_type",
    ["Page", "VoltageIn", "TreeDevice", "WeatherServer"],
)
def test_sensitive_xml_role_precedes_unrelated_type_during_full_round_trip(technical_type):
    """Sensitive XML roles and descendants resolve identically before and after projection."""
    xml = (
        '<ControlList><C Type="LoxLIVE" U="ms" />'
        f'<Credential U="secret" Type="{technical_type}" Title="PRIVATE-PARENT" IName="PRIVATE-PARENT-IO">'
        '<C Type="VoltageIn" U="secret-child" Title="PRIVATE-CHILD" IName="PRIVATE-CHILD-IO" />'
        "</Credential></ControlList>"
    ).encode()
    inventory = parse_engineering_xml(
        xml,
        source_archive="sps_7_20260913120000.zip",
        config_version=7,
        config_timestamp=datetime(2026, 9, 13, 12, tzinfo=UTC),
    )
    context = source()
    resolved = resolve_engineering_topology(inventory, context)
    rows = resolve_engineering_capabilities(resolved, None)
    snapshot = EngineeringSnapshot(
        context,
        resolved.nodes,
        rows,
        engineering_configuration_revision_id(context),
        engineering_safe_content_digest(context, resolved.nodes, rows),
        1,
        engineering_generation_id(context, resolved.nodes, rows, read_sequence=1),
        datetime(2026, 9, 13, 12, tzinfo=UTC),
    )

    assert not {"secret", "secret-child"} & {node.element.uuid for node in select_runtime_probe_candidates(resolved)}
    encoded = snapshot_to_dict(snapshot)
    restored = snapshot_from_dict(encoded)
    rendered = json.dumps(encoded)
    restored_rows = {
        row.node.element.uuid: row for row in restored.rows if row.node.element.uuid in {"secret", "secret-child"}
    }

    assert set(restored_rows) == {"secret", "secret-child"}
    assert all(row.node.sensitive for row in restored_rows.values())
    assert all(row.capability.state is CapabilityState.SENSITIVE for row in restored_rows.values())
    assert all(row.capability.exposure is ExposureStatus.SUPPRESSED for row in restored_rows.values())
    assert all(row.binding is None for row in restored_rows.values())
    assert restored_rows["secret"].semantic_platform is None
    assert restored_rows["secret-child"].semantic_platform == "sensor"
    assert "PRIVATE-PARENT" not in rendered
    assert "PRIVATE-CHILD" not in rendered
