# Generic Hierarchy and Bulk Area Repairs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic, privacy-safe Loxone hierarchy view and one native batch flow for HA area conflicts.

**Architecture:** A pure projection converts the validated engineering snapshot into a typed tree. A config-entry-scoped WebSocket command serves that projection to a minimal read-only panel. Stable Loxone room mappings and batch decisions extend the existing durable Task-5 boundary; Repairs remains a UI adapter and never mutates registries directly.

**Tech Stack:** Python 3.13, APIs compatible with the branch environment Home Assistant 2026.8.1 and live validation target 2026.9.2, voluptuous selectors, native WebSocket API, dependency-free JavaScript web component, pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-generic-hierarchy-bulk-area-repairs-design.md`

## Global Constraints

- Build relationships only from resolved node kind, owner, via-device, and bus metadata; never from device names.
- Show every safe function with a bounded capability status; sensitive descendants expose only an aggregate count.
- Room identity is the opaque Loxone room UUID; names are presentation only.
- Existing HA areas are never deleted by PyLoxone and new areas require explicit confirmation.
- No raw XML attributes, users, access/NFC identifiers, credentials, geographic data, or private endpoints. Real placement values are restricted to the authenticated admin hierarchy response and never appear in tests, diagnostics, issues, logs, notifications, or commits.
- Tests cover high-risk contracts only; no tests for trivial getters, static translations, or framework-owned rendering.
- Use `kagithd <42038442+kagithd@users.noreply.github.com>` for every commit. Do not push or deploy live.

## Binding execution rulings

- Version 1 changes hierarchy presentation only. Existing service-module devices and entity ownership remain untouched; registry migration is out of scope.
- `active_entity` is supplied only by optional caller-provided, provider-checked HA registry enrichment. Snapshot-only code must not infer active, disabled, or unavailable state.
- WebSocket commands and static assets register once per HA process. The server stays fail-closed after entry unload; only the panel is removed after the last entry and restored for the first. Panel and commands require administrators.
- Multi-entry views require explicit entry selection. Snapshot age is displayed, not used alone as a rejection reason. Client caches clear on entry or user change.
- Stored state adds versioned room mappings and pending batch intent/progress. Every coordinator construction and recovery path preserves them. Missing legacy room UUIDs remain unknown until a full read and are never reconstructed from names.
- Batch groups require exact nonempty tokens. Missing room UUIDs are resolved per device and are never mapping keys. One lock owner performs currentness checks after every await; a room mapping becomes effective only after its whole selected group succeeds.
- Aggregate issue identity includes a conflict-set fingerprint. A flow finishes successfully only after its own current conflict set is empty; stale or partial flows cannot remove a newer issue.
- Native Repairs uses two steps: group decisions first, then per-device keep overrides and no-room keep/clear decisions. Object rows carry immutable group/device keys; the backend rejects omitted, duplicate, unknown, and stale keys while preserving entered values on validation errors.
- Placement accepts only direct `Installation`, `SwitchBoard`, `SwitchBoardRow`, and `SwitchBoardPos` fields on non-sensitive provider, bus, bridge, or physical-device nodes. It is never inherited. Row and position accept ASCII decimal integers from 0 through 999. A dedicated validator exposes bounded placement only to admin hierarchy/repair responses, never issue data, diagnostics, logs, notifications, identity, or committed fixtures.

---

### Task 1: Privacy-safe generic hierarchy projection

**Files:**
- Create: `custom_components/loxone/engineering_hierarchy.py`
- Create: `tests/test_engineering_hierarchy.py`

**Interfaces:**
- Consumes: `EngineeringSnapshot`, `ResolvedEngineeringNode`, `EngineeringInventoryRow`.
- Produces: `build_engineering_hierarchy(snapshot: EngineeringSnapshot, *, entity_ids_by_unique_id: Mapping[str, str] | None = None) -> EngineeringHierarchy` and `hierarchy_to_dict(hierarchy: EngineeringHierarchy) -> dict[str, object]`.

- [ ] **Step 1: Write focused failing contract tests**

Use one synthetic graph covering: Miniserver-local service/channel grouping, nested bus/bridge/device ownership, an unknown physical type, all exposure states, and a sensitive subtree. Assert topology, counts, and absence of sensitive names/IDs.

```python
tree = build_engineering_hierarchy(snapshot)
payload = hierarchy_to_dict(tree)
assert payload["root"]["role"] == "provider"
assert payload["root"]["sections"][0]["role"] == "internal_service"
assert payload["summary"]["protected"] == 1
assert "synthetic-secret" not in json.dumps(payload)
```

- [ ] **Step 2: Run RED**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_hierarchy.py`
Expected: import failure because the projection module does not exist.

- [ ] **Step 3: Implement immutable projection types and serializer**

```python
@dataclass(frozen=True, slots=True)
class HierarchyFunction:
    key: str
    label: str | None
    technical_type: str | None
    status: str
    reason: str

@dataclass(frozen=True, slots=True)
class HierarchyNode:
    identifier: str
    role: str
    label: str | None
    technical_type: str | None
    bus_kind: str | None
    functions: tuple[HierarchyFunction, ...]
    children: tuple["HierarchyNode", ...]
    protected_count: int = 0

HierarchyBuilder = Callable[[EngineeringSnapshot], EngineeringHierarchy]
HierarchySerializer = Callable[[EngineeringHierarchy], dict[str, object]]
```

Use stable identifiers internally, but replace sensitive subtree identity with fixed protected aggregates. Service modules owned by the provider become root sections; channels attach to their resolved owner; physical nodes follow `via_device_identifier`/`device_identifier`. Sort by safe label, technical type, then opaque identifier for deterministic output.

Mark a function `active_entity` only when its stable UUID occurs in the optional caller-provided mapping, and include that mapped entity ID. Without enrichment, derive only prepared, inventory-only, unsupported, or protected states from snapshot contracts.

- [ ] **Step 4: Run GREEN and focused existing topology/privacy tests**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_hierarchy.py tests/test_engineering_topology.py tests/test_engineering_snapshot.py`
Expected: all pass.

- [ ] **Step 5: Run the full suite once and commit**

Run: `.venv/Scripts/python.exe -m pytest -q`

```powershell
git add custom_components/loxone/engineering_hierarchy.py tests/test_engineering_hierarchy.py
git commit -m "feat: project generic engineering hierarchy"
```

### Task 2: Read-only hierarchy API and minimal panel

**Files:**
- Create: `custom_components/loxone/engineering_websocket.py`
- Create: `custom_components/loxone/frontend/pyloxone-hierarchy.js`
- Modify: `custom_components/loxone/__init__.py`
- Modify: `custom_components/loxone/manifest.json`
- Create: `tests/test_engineering_websocket.py`

**Interfaces:**
- Consumes: Task 1 `hierarchy_to_dict`.
- Produces: admin-only commands `loxone/engineering_entries` and `loxone/engineering_hierarchy` with `{entry_id}`, plus a panel at `/pyloxone-hierarchy`.

- [ ] **Step 1: Write failing API boundary tests**

Test one success plus rejection of non-admin, unknown entry, unloaded entry, provider mismatch, and absent snapshot. Assert explicit multi-entry selection, freshness metadata, provider-checked entity enrichment, and a response containing only the Task-1 schema plus bounded navigation metadata.

```python
await websocket_handler(hass, connection, {"id": 1, "entry_id": "entry-a"})
assert connection.results[0]["result"]["root"]["role"] == "provider"
```

- [ ] **Step 2: Run RED**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_websocket.py`
Expected: import failure because the API module does not exist.

- [ ] **Step 3: Implement the scoped command and registration**

```python
ENGINEERING_HIERARCHY_SCHEMA = websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
    {
        vol.Required("type"): "loxone/engineering_hierarchy",
        vol.Required("entry_id"): str,
    }
)
```

Register commands and static assets once per HA process and require admin at both WebSocket and panel boundaries. Resolve the current loaded coordinator from `hass.data[DOMAIN]`, validate its snapshot provider against the current Miniserver, enrich links/states only from registry entries rechecked to belong to that provider, and return the safe projection with snapshot age. Keep commands fail-closed after unload. Remove only the panel when the last entry unloads and restore it for the first loaded entry; cover simultaneous/failed setup and unload/reload lifecycle.

- [ ] **Step 4: Implement the dependency-free tree component**

The web component first loads entries, requires an explicit choice when more than one is present, then calls `hass.callWS`. It renders nested semantic `<details>/<summary>` nodes, snapshot freshness, a local text filter, status badges, and links only to known HA device/entity IDs supplied by the backend. It distinguishes active, prepared, disabled, and unavailable enrichment states. It must use `textContent`, never interpolate data into `innerHTML`, contain no mutation controls, and clear cached entry/user state when either changes.

- [ ] **Step 5: Verify and commit**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_websocket.py tests/test_engineering_hierarchy.py tests/test_engineering_coordinator.py`
Run: `.venv/Scripts/python.exe -m py_compile custom_components/loxone/engineering_websocket.py`
Run the full suite once.

```powershell
git add custom_components/loxone/engineering_websocket.py custom_components/loxone/frontend/pyloxone-hierarchy.js custom_components/loxone/__init__.py custom_components/loxone/manifest.json tests/test_engineering_websocket.py
git commit -m "feat: add engineering hierarchy view"
```

### Task 3: Stable room mappings and batch resolution boundary

**Files:**
- Modify: `custom_components/loxone/engineering_config.py`
- Modify: `custom_components/loxone/engineering_snapshot.py`
- Modify: `custom_components/loxone/engineering_registry.py`
- Modify: `custom_components/loxone/coordinator.py`
- Modify: `tests/test_engineering_config.py`
- Modify: `tests/test_engineering_snapshot.py`
- Modify: `tests/test_engineering_registry.py`
- Modify: `tests/test_engineering_coordinator.py`

**Interfaces:**
- Adds opaque `room_uuid` to the safe node projection.
- Adds durable `room_area_mappings: Mapping[str, str]` and `pending_area_batch` to `StoredEngineeringState` with versioned codec/digest handling and a decoder accepting the prior schema.
- Produces `async_resolve_engineering_area_conflicts(hass, entry_id, decisions, *, is_current) -> EngineeringBatchAreaResolutionResult`.

- [ ] **Step 1: Write focused failing tests**

Cover opaque room identity surviving a rename, cold storage migration, every coordinator state-construction path, room mapping applied to a new device, keep-current device override, exact nonempty membership, full preflight before mutation, deleted selected area, stale token/provider/lifecycle rejection after awaits, partial/cancelled execution, and idempotent cold replay.

```python
result = await async_resolve_engineering_area_conflicts(
    hass,
    "entry-a",
    (EngineeringAreaDecision(room_uuid="room-1", action="use_existing", area_id="office"),),
    is_current=lambda: True,
)
assert result.resolved_groups == 1
```

- [ ] **Step 2: Run RED**

Run the exact new test nodes from the three files; expect missing fields/interfaces.

- [ ] **Step 3: Implement storage and resolver**

```python
@dataclass(frozen=True, slots=True)
class EngineeringAreaDecision:
    room_uuid: str | None
    action: str
    area_id: str | None = None
    area_name: str | None = None
    conflict_tokens: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class EngineeringBatchAreaResolutionResult:
    resolved_groups: int
    unresolved_groups: int
    reason: str
```

Validate all decisions, exact nonempty token sets, and selected area IDs before any mutation. Never combine or persist `room_uuid=None`; those conflicts are per-device keep/clear decisions. Create areas only for explicit `create` decisions after preflight. Use one lock owner and an internal exact-token primitive rather than recursively entering the existing public locking boundary. Persist batch intent and per-device progress before mutation, recheck currentness after every await, and make replay idempotent. Persist a room mapping only after all selected devices in its group succeed; preserve per-device keep/release semantics. Never delete created or pre-existing areas. Legacy snapshots retain last-known-good state/generation/cursor, leave absent room UUIDs unknown until a full read, and never reconstruct identity from names.

- [ ] **Step 4: Verify and commit**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_config.py tests/test_engineering_snapshot.py tests/test_engineering_registry.py`
Run the full suite once.

```powershell
git add custom_components/loxone/engineering_config.py custom_components/loxone/engineering_snapshot.py custom_components/loxone/engineering_registry.py custom_components/loxone/coordinator.py tests/test_engineering_config.py tests/test_engineering_snapshot.py tests/test_engineering_registry.py tests/test_engineering_coordinator.py
git commit -m "feat: persist stable room area mappings"
```

### Task 4: One native aggregate Repairs flow

**Files:**
- Modify: `custom_components/loxone/repairs.py`
- Modify: `custom_components/loxone/strings.json`
- Modify: `custom_components/loxone/translations/en.json`
- Modify: `custom_components/loxone/translations/de.json`
- Modify: `tests/test_engineering_repairs.py`
- Modify: `custom_components/loxone/manifest.json`
- Modify: `docs/engineering-owner-resolution.md`

**Interfaces:**
- Consumes: Task 3 batch resolver and HA `AreaSelector`.
- Replaces per-conflict issues with one entry-scoped issue per immutable conflict-set fingerprint whose data contains only kind, version, entry ID, and fingerprint.

- [ ] **Step 1: Write failing high-risk flow tests**

Use one table-driven backend boundary test plus focused cases for: one issue for twenty conflicts, grouping by opaque room identity, mixed per-device overrides, per-device no-room keep/clear, existing-area selection, explicit create, name-collision recovery, stale flow retry, and unresolved groups remaining after a partial operational failure. Exercise the real `RepairsFlowManager` for old/new fingerprints and ignored issues. Do not duplicate the existing provider/token/lifecycle matrix or test static wording and framework selector rendering.

```python
await async_sync_engineering_area_conflict_issues(hass, "entry-a")
assert len(owned_issue_ids(hass, "entry-a")) == 1
```

- [ ] **Step 2: Run RED**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_repairs.py`
Expected: aggregate issue and batch-flow assertions fail against the per-conflict implementation.

- [ ] **Step 3: Implement aggregate issue and native repeated-row form**

Reload and group conflicts when the flow opens. Step 1 uses native object rows with immutable group keys, safe device summaries, action, existing-area selector, and new-area name fields; fields remain visible because native selectors do not provide conditional field rendering. Omitted, duplicate, unknown, or stale keys return field errors while preserving entered values. Step 2 collects per-device keep overrides for mixed room groups and per-device keep/clear decisions for conflicts without a room UUID. A normalized create-name collision returns to Step 1 and points to the existing area choice without discarding input. Treat every submission as untrusted and validate through Task 3. Display bounded placement read-only beside the device reference, never in issue data. After resolution, resynchronize the fingerprinted issue; call `async_create_entry` only when that flow's current conflict set is empty so partial/stale flows cannot delete a newer or unresolved issue.

- [ ] **Step 4: Update documentation and version**

Document the room-level mapping, device override, explicit area creation, no-room behavior, hierarchy view, unsupported/protected function statuses, and the absence of Loxone writes. Increment the build version by one final patch component.

- [ ] **Step 5: Verify and commit**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_repairs.py tests/test_engineering_registry.py tests/test_engineering_hierarchy.py tests/test_engineering_websocket.py`
Run: `.venv/Scripts/python.exe -m pytest -q`
Run high-signal Ruff, compile, JSON parse, `git diff --check`, and privacy scan.

```powershell
git add custom_components/loxone/repairs.py custom_components/loxone/strings.json custom_components/loxone/translations/en.json custom_components/loxone/translations/de.json custom_components/loxone/manifest.json docs/engineering-owner-resolution.md tests/test_engineering_repairs.py
git commit -m "feat: resolve area conflicts in bulk"
```

### Task 5: Allowlisted physical placement

**Files:**
- Modify: `custom_components/loxone/engineering_config.py`
- Modify: `custom_components/loxone/engineering_topology.py`
- Modify: `custom_components/loxone/engineering_snapshot.py`
- Modify: `custom_components/loxone/engineering_hierarchy.py`
- Modify: `custom_components/loxone/repairs.py`
- Modify: `tests/test_engineering_config.py`
- Modify: `tests/test_engineering_snapshot.py`
- Modify: `tests/test_engineering_hierarchy.py`
- Modify: `tests/test_engineering_repairs.py`

**Interfaces:**
- Produces immutable `InstallationPlacement(installation, switchboard, row, position)`.
- Reads only exact XML attributes `Installation`, `SwitchBoard`, `SwitchBoardRow`, and `SwitchBoardPos`.

- [ ] **Step 1: Write three contract tests**

Use synthetic values only. Prove valid direct bounded strings/integers reach the admin hierarchy and repair projections, unrelated or inherited raw attributes do not, sensitive placement cannot cross ownership boundaries, and malformed/oversized placement is omitted and absent from diagnostics/issue projections.

```python
placement = InstallationPlacement(
    installation="Synthetic installation",
    switchboard="Cabinet A",
    row=2,
    position=4,
)
assert hierarchy_to_dict(tree)["root"]["placement"] == {
    "installation": "Synthetic installation",
    "switchboard": "Cabinet A",
    "row": 2,
    "position": 4,
}
```

- [ ] **Step 2: Run RED**

Run the three new test nodes; expect the placement type and projection fields to be absent.

- [ ] **Step 3: Implement the exact allowlist**

```python
@dataclass(frozen=True, slots=True)
class InstallationPlacement:
    installation: str | None = None
    switchboard: str | None = None
    row: int | None = None
    position: int | None = None
```

Use a dedicated placement validator rather than relaxing the general presentation/privacy policy. Accept strings only from direct exact allowlisted attributes on non-sensitive provider, bus, bridge, and physical-device nodes. Never inherit placement. Accept row/position only as ASCII base-10 integers from `0` through `999`, without bool/float coercion. Attach placement to its owning node, include it in the private versioned snapshot codec and safe digest, and serialize it only through admin hierarchy and repair-flow responses. Deterministically omit conflicting values. Do not add raw placement to issue data, diagnostics, notifications, logs, device names, areas, or HA registry identifiers.

- [ ] **Step 4: Verify and commit**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_config.py tests/test_engineering_snapshot.py tests/test_engineering_hierarchy.py tests/test_engineering_diagnostics.py tests/test_engineering_repairs.py`
Run the full suite once plus high-signal lint, compile, diff, JSON, and privacy gates.

```powershell
git add custom_components/loxone/engineering_config.py custom_components/loxone/engineering_topology.py custom_components/loxone/engineering_snapshot.py custom_components/loxone/engineering_hierarchy.py custom_components/loxone/repairs.py tests/test_engineering_config.py tests/test_engineering_snapshot.py tests/test_engineering_hierarchy.py tests/test_engineering_repairs.py
git commit -m "feat: expose bounded installation placement"
```
