"""Bounded snapshot reuse must never cache mutable recovery authority."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest

import custom_components.loxone.engineering_snapshot as module
from tests.engineering_fixtures import make_snapshot


@pytest.fixture(autouse=True)
def isolated_codec_cache():
    """Keep cache warmth local; no executor work survives these tests."""
    getattr(module, "_SNAPSHOT_CODEC_CACHE", []).clear()
    yield
    getattr(module, "_SNAPSHOT_CODEC_CACHE", []).clear()


def test_repeated_decode_reuses_the_exact_validated_snapshot():
    """Unchanged checkpoints must not reconstruct the complete graph."""
    payload = module.snapshot_to_dict(make_snapshot())
    first = module.snapshot_from_dict(payload)
    assert module.snapshot_from_dict(deepcopy(payload)) is first


def test_warm_snapshot_validation_and_encoding_do_not_resolve_topology(monkeypatch):
    """Warm journal writes must not redo expensive authoritative resolution."""
    snapshot = module.snapshot_from_dict(module.snapshot_to_dict(make_snapshot()))
    expected = module.snapshot_to_dict(snapshot)
    resolutions = []
    actual = module._authoritative_topology

    def observed(*args):
        resolutions.append(True)
        return actual(*args)

    monkeypatch.setattr(module, "_authoritative_topology", observed)
    module.validate_engineering_snapshot(snapshot)
    assert module.snapshot_to_dict(snapshot) == expected
    assert resolutions == []


def test_encoding_returns_detached_mutable_payloads():
    """A caller cannot poison a cached snapshot by editing an earlier result."""
    snapshot = make_snapshot()
    original = module.snapshot_to_dict(snapshot)
    expected = deepcopy(original)
    original["source"]["entry_id"] = "foreign-entry"
    original["nodes"].clear()
    assert module.snapshot_to_dict(snapshot) == expected


def test_decode_cache_key_and_value_share_the_same_detached_observation(monkeypatch):
    """A caller mutation after fingerprinting must not poison the original key."""
    payload = module.snapshot_to_dict(make_snapshot())
    original = deepcopy(payload)
    actual_key = module._snapshot_json_key

    def caller_mutates_after_key(value):
        key = actual_key(value)
        # Deterministically model another holder of the input mutating it
        # between fingerprinting and the expensive uncached decode.
        payload["captured_at"] = "2026-09-14T12:00:00+00:00"
        return key

    with monkeypatch.context() as context:
        context.setattr(module, "_snapshot_json_key", caller_mutates_after_key)
        module.snapshot_from_dict(payload)
    restored = module.snapshot_from_dict(original)
    assert restored.captured_at.isoformat() == "2026-09-13T12:00:00+00:00"


@pytest.mark.parametrize(
    "mutation", ["boolean_sequence", "tuple_nodes", "foreign_source", "unknown_field", "node_owner"]
)
def test_warm_decode_never_accepts_same_generation_tampering(mutation):
    """Neither claimed generation nor normalized JSON shapes prove identity."""
    payload = module.snapshot_to_dict(make_snapshot())
    module.snapshot_from_dict(payload)
    changed = deepcopy(payload)
    if mutation == "boolean_sequence":
        changed["read_sequence"] = True
    elif mutation == "tuple_nodes":
        changed["nodes"] = tuple(changed["nodes"])
    elif mutation == "foreign_source":
        changed["source"]["entry_id"] = "foreign-entry"
    elif mutation == "unknown_field":
        changed["unexpected"] = "must-not-be-ignored"
    else:
        changed["nodes"][0]["device_identifier"] = "foreign-owner"
    with pytest.raises(module.EngineeringSnapshotError):
        module.snapshot_from_dict(changed)


def test_validation_cache_does_not_use_dataclass_equality():
    """Python's True == 1 must not validate a distinct malformed snapshot."""
    snapshot = make_snapshot()
    module.validate_engineering_snapshot(snapshot)
    malformed = replace(snapshot, read_sequence=True)
    assert malformed == snapshot
    with pytest.raises(module.EngineeringSnapshotError):
        module.validate_engineering_snapshot(malformed)


def test_recovery_metadata_and_journal_are_fresh_despite_snapshot_reuse():
    """Progress changes reuse the graph, not previous mapping or journal state."""
    snapshot = make_snapshot()
    token = "a" * 64
    member = module.EngineeringAreaBatchMember(token, "serial-a:weather-server", None)
    group = module.EngineeringAreaBatchGroup(
        module.EngineeringAreaDecision("room-uuid", "keep_ha", conflict_tokens=(token,)),
        (member,),
    )
    batch = module.EngineeringAreaBatchIntent("entry-a", "serial-a", snapshot.generation_id, (group,))
    state = module.StoredEngineeringState(snapshot, pending_area_batch=batch)
    first = module.stored_state_from_dict(module.stored_state_to_dict(state), "entry-a")
    progressed = replace(group, members=(replace(member, completed=True),), mapping_committed=True)
    changed = replace(
        first,
        pending_area_batch=replace(batch, groups=(progressed,)),
        room_area_mappings={"room-uuid": "office"},
    )
    second = module.stored_state_from_dict(module.stored_state_to_dict(changed), "entry-a")
    assert second.snapshot is first.snapshot
    assert second.room_area_mappings == {"room-uuid": "office"}
    assert not first.pending_area_batch.groups[0].mapping_committed
    assert second.pending_area_batch.groups[0].mapping_committed
    assert second.pending_area_batch.groups[0].members[0].completed


@pytest.mark.parametrize("mutation", ["store_scope", "mapping_digest", "publication_cursor", "mapping_scope"])
def test_warm_envelope_rechecks_recovery_authority(mutation):
    """A valid cached graph never authorizes changed envelope metadata."""
    state = module.StoredEngineeringState(make_snapshot(), room_area_mappings={"room-uuid": "office"})
    payload = module.stored_state_to_dict(state)
    module.stored_state_from_dict(payload, "entry-a")
    entry_id = "entry-a"
    changed = deepcopy(payload)
    if mutation == "store_scope":
        entry_id = "foreign-entry"
    elif mutation == "mapping_digest":
        changed["room_area_mappings"]["room-uuid"] = "kitchen"
    elif mutation == "publication_cursor":
        changed["impact_published_generation"] = state.snapshot.generation_id
    else:
        changed["room_area_mapping_scope"] = ["entry-a", "foreign-provider"]
    with pytest.raises(module.EngineeringSnapshotError):
        module.stored_state_from_dict(changed, entry_id)


def test_cache_is_bounded_and_evicted_snapshots_still_decode():
    """Changing generations must not retain an unbounded private inventory."""
    payloads = [module.snapshot_to_dict(make_snapshot(read_sequence=sequence)) for sequence in range(1, 5)]
    snapshots = [module.snapshot_from_dict(payload) for payload in payloads]
    assert len(module._SNAPSHOT_CODEC_CACHE) <= 2
    restored = module.snapshot_from_dict(payloads[0])
    assert restored is not snapshots[0]
    assert restored.generation_id == snapshots[0].generation_id


def test_concurrent_codec_calls_keep_outputs_detached_and_valid():
    """Executor workers may share immutable graphs, never returned dictionaries."""
    payload = module.snapshot_to_dict(make_snapshot())

    def roundtrip(_index):
        snapshot = module.snapshot_from_dict(deepcopy(payload))
        encoded = module.snapshot_to_dict(snapshot)
        encoded["nodes"].clear()
        return module.snapshot_to_dict(snapshot)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(roundtrip, range(12)))
    assert all(result == payload for result in results)
    assert len(module._SNAPSHOT_CODEC_CACHE) <= 2
