# Room Repair Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Synchronize Home Assistant areas only for external Loxone hardware, retain extension room support, and provide a durable HA-only fallback when Loxone supplies no room.

**Architecture:** Device topology and area synchronization become independent. A node-kind policy enables area work only for external bridges and physical devices; provider-owned, bus, and structural nodes remain visible without area mutations. A missing source room permits a device-scoped fallback in validated engineering state. A later Loxone room releases that fallback and resumes source-room handling.

**Tech Stack:** Home Assistant custom integration, Python 3.13, pytest, ruff, Home Assistant device and area registries, private validated Store state.

**Spec:** docs/superpowers/specs/2026-09-18-room-repair-scope-design.md

## Global Constraints

- Stay on codex/owner-resolver-service-modules; use kagithd <42038442+kagithd@users.noreply.github.com>; do not push or create an upstream PR.
- Derive policy only from resolved topology metadata. Never use device, room, bus, or display names.
- NodeKind.BRIDGE and NodeKind.PHYSICAL_DEVICE are room-capable. NodeKind.BUS, Miniserver, service, and structural nodes are not.
- Preserve entity unique IDs, entity ownership, via_device_id, existing room mappings, and completed batch checkpoints.
- A missing Loxone room permits an explicit HA-only device fallback. It never writes to Loxone or creates a fictitious room mapping.
- Remove a fallback only after a verified later Loxone room UUID for the same source-scoped device.
- Do not expose persistent identifiers or private installation values in Repairs, logs, tests, diagnostics, or commits.
- Use one meaningful failing behavior test before each production change. Skip framework-rendering and trivial-getter tests.
- Do not install in Home Assistant, restart it, or push the fork without a separate explicit request.

---

### Task 1: Add an explicit area-synchronization policy

**Files:**
- Modify: custom_components/loxone/engineering_registry.py:112-131,1357-1386,1477-1581
- Test: tests/test_engineering_registry.py

**Interfaces:**
- Consumes: ResolvedEngineeringNode.kind, ResolutionStatus.RESOLVED, NodeKind.BRIDGE, and NodeKind.PHYSICAL_DEVICE.
- Produces: EngineeringDeviceOperation.area_sync_enabled: bool and _area_sync_enabled(node: ResolvedEngineeringNode) -> bool.
- Contract: topology device creation remains unchanged; only area synchronization is disabled for provider, bus, service, and structural operations.

- [ ] **Step 1: Write the failing planning-policy test**

~~~python
def test_area_policy_keeps_external_bridge_and_device_room_capable(registries):
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("tree", "LoxTree", parent_uuid="ms", room="Room A"),
            element("extension", "OneWireExtension", parent_uuid="tree", room="Room B"),
            element("endpoint", "TreeDevice", parent_uuid="tree", room="Room C"),
        )
    )
    plan = asyncio.run(
        async_plan_engineering_registry_sync(
            registries.hass, "entry-a", snapshot, EngineeringRegistryMetadata.empty()
        )
    )
    operations = {item.identifier: item for item in plan.device_operations}
    assert operations["serial-a:tree"].area_sync_enabled is False
    assert operations["serial-a:extension"].area_sync_enabled is True
    assert operations["serial-a:endpoint"].area_sync_enabled is True
~~~

- [ ] **Step 2: Verify RED**

Run: pytest tests/test_engineering_registry.py::test_area_policy_keeps_external_bridge_and_device_room_capable -q

Expected: failure because area_sync_enabled does not exist.

- [ ] **Step 3: Implement the minimal policy seam**

~~~python
def _area_sync_enabled(node: ResolvedEngineeringNode) -> bool:
    return (
        not node.sensitive
        and node.resolution_status is ResolutionStatus.RESOLVED
        and node.kind in {NodeKind.BRIDGE, NodeKind.PHYSICAL_DEVICE}
    )
~~~

Add area_sync_enabled: bool = True as the final EngineeringDeviceOperation field, and pass the helper result from async_plan_engineering_registry_sync. Do not change _eligible_device_keys: it is the topology policy.

- [ ] **Step 4: Verify GREEN**

Run: pytest tests/test_engineering_registry.py::test_area_policy_keeps_external_bridge_and_device_room_capable -q

Expected: PASS.

- [ ] **Step 5: Commit**

Run: git add custom_components/loxone/engineering_registry.py tests/test_engineering_registry.py

Run: git commit -m "feat: scope area sync to external hardware"

### Task 2: Isolate structural nodes from area mutation and Repairs

**Files:**
- Modify: custom_components/loxone/engineering_registry.py:1330-1480,1830-1980
- Test: tests/test_engineering_registry.py

**Interfaces:**
- Consumes: EngineeringDeviceOperation.area_sync_enabled.
- Produces: intent, mutation, reconciliation, and conflict publication that ignore non-room-capable operations.
- Contract: an old bus conflict is retired without clearing its HA area; a bridge in the same topology remains actionable.

- [ ] **Step 1: Write the failing reconciliation regression test**

Add test_bus_area_is_preserved_and_its_stale_conflict_is_retired. Construct the
synthetic LoxLIVE, LoxTree, and OneWireExtension inventory shown in Task 1.
Create existing_area with the FakeAreaRegistry and assign it to serial-a:tree.
Persist a valid bus conflict using the existing _area_conflict constructor and
FakeIntentStore.data format. Monkeypatch async_load_engineering_state and
async_store_engineering_state with the same copied-wire pattern used by
_batch_case. Apply the real planner and applier. Assert that the bus still has
existing_area, that it has no conflict, and that the bridge conflict remains.

- [ ] **Step 2: Verify RED**

Run: pytest tests/test_engineering_registry.py::test_bus_area_is_preserved_and_its_stale_conflict_is_retired -q

Expected: failure because the current reconciliation treats all device operations as area candidates.

- [ ] **Step 3: Gate every area side effect**

In _candidate_area_intents, _recheck_area_intents, _apply_device_properties, and _reconcile_area_state_after_apply, guard area work with operation.area_sync_enabled. For a disabled operation, preserve its current HA area and topology, then drop only integration-owned area bookkeeping:

~~~python
if not operation.area_sync_enabled:
    baselines.pop(identifier, None)
    conflicts.pop(identifier, None)
    remaining_intents.pop(identifier, None)
    continue
~~~

Use the exact local map names in each function. Do not set released_identifiers: classification is not a permanent user override.

Before replaying a pending batch, reject an ineligible member. Retire only its stale Repair bookkeeping and preserve its current HA area. If its group has eligible members, use the existing stale-batch path to rebuild a fresh group instead of silently changing token membership.

- [ ] **Step 4: Verify GREEN**

Run: pytest tests/test_engineering_registry.py -q -k "area_policy_keeps_external_bridge_and_device_room_capable or bus_area_is_preserved_and_its_stale_conflict_is_retired or batch or area_conflict"

Expected: PASS.

- [ ] **Step 5: Commit**

Run: git add custom_components/loxone/engineering_registry.py tests/test_engineering_registry.py

Run: git commit -m "fix: exclude structural nodes from area repairs"

### Task 3: Add a durable HA-only fallback for devices without a Loxone room

**Files:**
- Modify: custom_components/loxone/engineering_snapshot.py:60-64,210-226,1338-1655
- Modify: custom_components/loxone/engineering_registry.py:808-964,1002-1304,1477-1581,1830-1980
- Test: tests/test_engineering_snapshot.py
- Test: tests/test_engineering_registry.py

**Interfaces:**
- Consumes: a one-token EngineeringAreaDecision with room_uuid=None.
- Produces: StoredEngineeringState.device_area_fallbacks: Mapping[str, str], keyed by source-scoped device identifier.
- Contract: use_existing and create persist a fallback; keep_ha persists none; clear removes it. A later Loxone room removes the fallback before source-room planning.

- [ ] **Step 1: Write the failing state-codec test**

~~~python
def test_device_area_fallbacks_round_trip_and_require_source_scope():
    snapshot = make_snapshot()
    state = StoredEngineeringState(
        snapshot=snapshot,
        device_area_fallbacks={"serial-a:device": "area-a"},
    )
    wire = stored_state_to_dict(state)
    restored = stored_state_from_dict(wire, "entry-a")
    assert restored.device_area_fallbacks == {"serial-a:device": "area-a"}
    wire["device_area_fallbacks"] = {"foreign:device": "area-a"}
    with pytest.raises(EngineeringSnapshotError):
        stored_state_from_dict(wire, "entry-a")
~~~

- [ ] **Step 2: Verify RED**

Run: pytest tests/test_engineering_snapshot.py::test_device_area_fallbacks_round_trip_and_require_source_scope -q

Expected: failure because the state has no device_area_fallbacks field.

- [ ] **Step 3: Extend the validated storage envelope**

Add an immutable empty device_area_fallbacks default to StoredEngineeringState. Include it in _AREA_STATE_FIELDS, _area_state_payload, validation, and both codec directions. Validate each key with _validate_scope(identifier, provider) and each area with _required_identifier(area_id, "fallback area").

Bump ENGINEERING_SNAPSHOT_STORAGE_VERSION from 3 to 4. Decode versions 2 and 3 with an empty fallback map before calculating the v4 area-state digest. Do not reinterpret an older room mapping as a fallback.

- [ ] **Step 4: Verify GREEN**

Run: pytest tests/test_engineering_snapshot.py -q -k "fallback or room_state or state_envelope or migration"

Expected: PASS.

- [ ] **Step 5: Write the failing fallback lifecycle test**

Add test_no_room_device_fallback_is_released_when_loxone_room_appears. Build a
source-scoped OneWireExtension with room=None, persist it through the copied
wire-state harness used by _batch_case, and resolve its sole conflict with a
use_existing decision pointing to fallback_area. Assert the saved state stores
only {"serial-a:extension": fallback_area.id} in device_area_fallbacks and has
no room_area_mappings entry. Refresh the exact same bridge with room="Source
room" and a stable room UUID; create exactly one HA area named Source room.
Replan and apply. Assert the fallback map is empty and the bridge area equals
the source-room area.

- [ ] **Step 6: Verify RED**

Run: pytest tests/test_engineering_registry.py::test_no_room_device_fallback_is_released_when_loxone_room_appears -q

Expected: failure because no-room decisions only allow keep or clear and no fallback state exists.

- [ ] **Step 7: Implement the one-token fallback lifecycle**

Allow use_existing and create for room_uuid=None only with one conflict token. On a successfully checkpointed batch group, store the selected area under its device identifier. Remove the fallback for clear and keep_ha. Never write a None room into room_area_mappings.

During planning, use a valid fallback only for an area-sync-enabled operation with no room UUID. If that same node later has a verified room UUID, remove its fallback inside the acknowledged state transition before calculating the source area. Existing source-room mappings keep their current behavior.

- [ ] **Step 8: Verify GREEN**

Run: pytest tests/test_engineering_registry.py -q -k "no_room_device_fallback_is_released_when_loxone_room_appears or batch or room_mapping"

Expected: PASS.

- [ ] **Step 9: Commit**

Run: git add custom_components/loxone/engineering_snapshot.py custom_components/loxone/engineering_registry.py tests/test_engineering_snapshot.py tests/test_engineering_registry.py

Run: git commit -m "feat: add area fallback for unassigned devices"

### Task 4: Make native Repairs usable without persistent identifiers

**Files:**
- Modify: custom_components/loxone/repairs.py:500-697
- Modify: custom_components/loxone/strings.json:39-69
- Modify: custom_components/loxone/translations/en.json:87-137
- Modify: custom_components/loxone/translations/de.json:87-137
- Test: tests/test_engineering_repairs.py

**Interfaces:**
- Consumes: room-group conflicts and one-token no-room decisions from Task 3.
- Produces: flow-local room-1 and device-1 references mapped in memory to verified room UUIDs and tokens.
- Contract: Repairs exposes no persistent room UUID or device identifier. No-room rows allow selecting or creating an HA area, retaining HA, or clearing it.

- [ ] **Step 1: Write the failing Repair-flow test**

~~~python
def test_no_room_device_rows_allow_target_selection_without_exposing_ids(monkeypatch):
    conflict = _conflict(room_uuid=None)
    harness = _repairs_harness(monkeypatch, {"entry-a": [conflict]})

    async def scenario():
        form = await (await _open(harness)).async_step_init()
        row = _rows(form, "no_room_devices")[0]
        assert row["row_id"] == "device-1"
        assert conflict.device_identifier not in repr(form)
        result = await (await _open(harness)).async_step_devices(
            {"no_room_devices": [{**row, "action": "use_existing", "area_id": "office"}]}
        )
        assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY

    asyncio.run(scenario())
~~~

- [ ] **Step 2: Verify RED**

Run: pytest tests/test_engineering_repairs.py::test_no_room_device_rows_allow_target_selection_without_exposing_ids -q

Expected: failure because the form exposes device_key and rejects use_existing for no-room devices.

- [ ] **Step 3: Use flow-local row references and target validation**

After _load verifies the exact fingerprint, create deterministic per-flow maps from room-1 and device-1 to current room UUIDs or conflict tokens. Accept only those references for the current step and resolve them back to trusted tokens before creating EngineeringAreaDecision objects.

For no-room rows, add area_id and area_name; allow use_existing, create, keep_ha, and clear. Reuse area existence and normalized-name validation from _validate_rooms, including collision checks across room and device creates. Keep the default keep_ha with empty targets.

Use labelled select options rather than internal action values. Keep English and German strings synchronized; explain that the no-room choice is an HA-only fallback.

- [ ] **Step 4: Verify GREEN**

Run: pytest tests/test_engineering_repairs.py -q -k "no_room_device_rows_allow_target_selection_without_exposing_ids or mixed_room_overrides_and_no_room_build_exact_batch or device_membership_and_action_validation"

Expected: PASS.

- [ ] **Step 5: Validate translations and commit**

Run: python -m json.tool custom_components/loxone/strings.json

Run: python -m json.tool custom_components/loxone/translations/en.json

Run: python -m json.tool custom_components/loxone/translations/de.json

Run: git add custom_components/loxone/repairs.py custom_components/loxone/strings.json custom_components/loxone/translations/en.json custom_components/loxone/translations/de.json tests/test_engineering_repairs.py

Run: git commit -m "feat: support device area fallback repairs"

### Task 5: Run high-signal verification and wait for deployment direction

**Files:**
- Modify: none unless a verification failure proves a defect.
- Test: tests/test_engineering_registry.py
- Test: tests/test_engineering_repairs.py
- Test: tests/test_engineering_snapshot.py

**Interfaces:**
- Consumes: all preceding contracts.
- Produces: evidence for policy, migration, recovery, and Repair decision behavior.

- [ ] **Step 1: Run the targeted integration suite**

Run: pytest tests/test_engineering_registry.py tests/test_engineering_repairs.py tests/test_engineering_snapshot.py -q

Expected: PASS with no failures.

- [ ] **Step 2: Run narrow static checks**

Run: ruff check custom_components/loxone/engineering_registry.py custom_components/loxone/engineering_snapshot.py custom_components/loxone/repairs.py

Run: `$base = git merge-base upstream/master HEAD`, then `git diff --check "$base..HEAD"`.

Run: git status --short

Expected: ruff reports no errors, diff check is empty, and status is clean.

- [ ] **Step 3: Inspect the privacy boundary**

Run: rg -n -i "rotensol|gps|latitude|longitude|user@" custom_components/loxone tests docs/superpowers/plans/2026-09-19-room-repair-scope.md

Run: git log -4 --format="%h %an <%ae> %s"

Expected: no installation-sensitive data; each new commit uses the GitHub noreply identity.

- [ ] **Step 4: Report evidence and wait for deployment direction**

Report only commands, pass/fail counts, changed files, commit IDs, and the worktree state. Do not claim live Home Assistant behavior until installation is explicitly requested.
