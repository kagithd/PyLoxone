# Generic Hierarchy and Bulk Area Repairs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic, privacy-safe Loxone hierarchy view and one native batch flow for HA area conflicts.

**Architecture:** A pure projection converts the validated engineering snapshot into a typed tree. A config-entry-scoped WebSocket command serves that projection to a minimal read-only panel. Stable Loxone room mappings and batch decisions extend the existing durable Task-5 boundary; Repairs remains a UI adapter and never mutates registries directly.

**Tech Stack:** Python 3.13, Home Assistant 2026.9 APIs, voluptuous selectors, native WebSocket API, dependency-free JavaScript web component, pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-generic-hierarchy-bulk-area-repairs-design.md`

## Global Constraints

- Build relationships only from resolved node kind, owner, via-device, and bus metadata; never from device names.
- Show every safe function with a bounded capability status; sensitive descendants expose only an aggregate count.
- Room identity is the opaque Loxone room UUID; names are presentation only.
- Existing HA areas are never deleted by PyLoxone and new areas require explicit confirmation.
- No raw XML attributes, users, access/NFC identifiers, credentials, geographic data, or private endpoints. Real placement values are restricted to the authenticated admin hierarchy response and never appear in tests, diagnostics, issues, logs, notifications, or commits.
- Tests cover high-risk contracts only; no tests for trivial getters, static translations, or framework-owned rendering.
- Use `kagithd <42038442+kagithd@users.noreply.github.com>` for every commit. Do not push or deploy live.

---

### Task 1: Privacy-safe generic hierarchy projection

**Files:**
- Create: `custom_components/loxone/engineering_hierarchy.py`
- Create: `tests/test_engineering_hierarchy.py`

**Interfaces:**
- Consumes: `EngineeringSnapshot`, `ResolvedEngineeringNode`, `EngineeringInventoryRow`.
- Produces: `build_engineering_hierarchy(snapshot: EngineeringSnapshot) -> EngineeringHierarchy` and `hierarchy_to_dict(hierarchy: EngineeringHierarchy) -> dict[str, object]`.

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
- Produces: admin-only command `loxone/engineering_hierarchy` with `{entry_id}` and a panel at `/pyloxone-hierarchy`.

- [ ] **Step 1: Write failing API boundary tests**

Test one success plus rejection of non-admin, unknown entry, unloaded entry, provider mismatch, and absent/stale snapshot. Assert the response contains only the Task-1 schema.

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

Register once, require admin, resolve the current loaded coordinator from `hass.data[DOMAIN]`, validate its snapshot provider against the current Miniserver, and return the safe projection. Register the static module and custom panel once; unregister only when the last Loxone entry unloads.

- [ ] **Step 4: Implement the dependency-free tree component**

The web component calls `hass.callWS`, renders nested semantic `<details>/<summary>` nodes, provides a local text filter, status badges, and links only to known HA device/entity IDs supplied by the backend. It must use `textContent`, never interpolate data into `innerHTML`, and contain no mutation controls.

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
- Modify: `tests/test_engineering_config.py`
- Modify: `tests/test_engineering_snapshot.py`
- Modify: `tests/test_engineering_registry.py`

**Interfaces:**
- Adds opaque `room_uuid` to the safe node projection.
- Adds durable `room_area_mappings: Mapping[str, str]` to `StoredEngineeringState` with a versioned decoder accepting the prior schema.
- Produces `async_resolve_engineering_area_conflicts(hass, entry_id, decisions, *, is_current) -> EngineeringBatchAreaResolutionResult`.

- [ ] **Step 1: Write focused failing tests**

Cover opaque room identity surviving a rename, storage migration, room mapping applied to a new device, keep-current device override, full preflight before mutation, deleted selected area, stale token/provider/lifecycle rejection, and idempotent retry.

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

Validate all decisions and selected area IDs before any mutation. Create areas only for explicit `create` decisions after preflight. Apply each group through the existing exact-token resolution machinery. Persist room mappings only after successful device updates; preserve per-device keep/release semantics. Never delete created or pre-existing areas.

- [ ] **Step 4: Verify and commit**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_config.py tests/test_engineering_snapshot.py tests/test_engineering_registry.py`
Run the full suite once.

```powershell
git add custom_components/loxone/engineering_config.py custom_components/loxone/engineering_snapshot.py custom_components/loxone/engineering_registry.py tests/test_engineering_config.py tests/test_engineering_snapshot.py tests/test_engineering_registry.py
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
- Replaces per-conflict issues with one entry-scoped issue whose data contains only kind, version, and entry ID.

- [ ] **Step 1: Write failing high-risk flow tests**

Cover one issue for twenty conflicts, grouping by opaque room identity, existing-area selection, explicit create, keep, valid clear, invalid-target keep-only, stale flow retry, and unresolved groups remaining after a partial operational failure. Do not test static wording or selector rendering.

```python
await async_sync_engineering_area_conflict_issues(hass, "entry-a")
assert len(owned_issue_ids(hass, "entry-a")) == 1
```

- [ ] **Step 2: Run RED**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_repairs.py`
Expected: aggregate issue and batch-flow assertions fail against the per-conflict implementation.

- [ ] **Step 3: Implement aggregate issue and native repeated-row form**

Reload and group conflicts when the flow opens. Build native object rows containing safe device summaries, action, optional `AreaSelector`, and optional new-area name. Treat submitted rows as untrusted: validate exact current token membership and all target fields through Task 3. Show a bounded completion summary; resynchronize the one issue afterward.

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
- Modify: `tests/test_engineering_config.py`
- Modify: `tests/test_engineering_snapshot.py`
- Modify: `tests/test_engineering_hierarchy.py`

**Interfaces:**
- Produces immutable `InstallationPlacement(installation, switchboard, row, position)`.
- Reads only exact XML attributes `Installation`, `SwitchBoard`, `SwitchBoardRow`, and `SwitchBoardPos`.

- [ ] **Step 1: Write three contract tests**

Use synthetic values only. Prove valid bounded strings/integers reach the admin hierarchy projection, unrelated raw attributes do not, and malformed/oversized/sensitive placement is omitted and absent from diagnostics/issue projections.

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

Normalize strings with the existing bounded presentation validator. Accept row/position only as base-10 integers from `0` through `999`. Attach placement to its owning physical/provider node, include it in the private snapshot codec and safe digest, and serialize it only through the admin hierarchy command. Do not add it to diagnostics, Repairs data, notifications, device names, areas, or HA registry identifiers.

- [ ] **Step 4: Verify and commit**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_engineering_config.py tests/test_engineering_snapshot.py tests/test_engineering_hierarchy.py tests/test_engineering_diagnostics.py tests/test_engineering_repairs.py`
Run the full suite once plus high-signal lint, compile, diff, JSON, and privacy gates.

```powershell
git add custom_components/loxone/engineering_config.py custom_components/loxone/engineering_topology.py custom_components/loxone/engineering_snapshot.py custom_components/loxone/engineering_hierarchy.py tests/test_engineering_config.py tests/test_engineering_snapshot.py tests/test_engineering_hierarchy.py
git commit -m "feat: expose bounded installation placement"
```
