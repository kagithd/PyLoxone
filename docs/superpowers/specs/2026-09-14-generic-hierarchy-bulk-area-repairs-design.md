# Generic Hierarchy and Bulk Area Repairs Design

## Goal

Present the complete safe Loxone engineering inventory as a generic hierarchy and let an operator resolve many Home Assistant area conflicts in one native Repairs flow.

## Scope

- One aggregate area-repair issue per loaded Loxone config entry.
- Existing HA areas can be selected; new areas require an explicit create action.
- Stable Loxone rooms map to HA area IDs. A device-specific "keep HA area" decision remains an override.
- Miniserver-local services and integrated channels are rendered inside the Miniserver node.
- Hardware reached through a physical bus is rendered below its actual owner or bus node.
- Every discovered function is visible with a bounded capability status, even when no entity can be created.
- A small read-only hierarchy view uses the same safe snapshot as registry synchronization.

Writing configuration back to Loxone and exposing credentials, users, access codes, NFC identifiers, geographic data, or arbitrary raw XML attributes are out of scope.

## Generic topology model

The view must not contain product-name branches such as hard-coded "NFC", "1-Wire", or "Air" sections. It consumes typed graph data:

- `provider`: the Miniserver root.
- `internal_service`: a logical service owned by the provider, rendered inside the root rather than as an independent HA device.
- `bus`: a physical transport node owned by the provider or another bus device.
- `physical_device`: hardware connected through the nearest proven bus or physical owner.
- `function`: an input, output, state, or service channel owned by a provider or physical device.
- `structural`: a non-device grouping node used only when it contributes a proven relationship.

Relationships come only from resolved `owner_identifier`, `via_identifier`, `bus_kind`, and node kind. Titles influence presentation only. Unknown technical types remain visible as unknown inventory nodes; names never establish ownership.

Example rendering:

```text
▼ Miniserver
  ├─ Internal services
  │  ├─ System variables
  │  ├─ Weather data
  │  └─ Integrated inputs and outputs
  ├─ Bus
  │  └─ Extension
  │     └─ Sensor
  └─ Bus
     └─ Access device
        ├─ Available function
        ├─ Prepared function
        ├─ Unsupported function — reason
        └─ Protected functions present
```

The labels above describe roles, not a fixed list of Loxone products.

## Function visibility

Every safe discovered function is listed under its owner with one status:

- `active entity`: a supported HA entity exists.
- `prepared`: an entity specification exists but is disabled by default.
- `inventory only`: readable or configured functionality exists without an enabled entity contract.
- `unsupported`: the technical function is known but no safe HA contract exists; show a translated bounded reason.
- `protected`: sensitive descendants exist; show only an aggregate presence/count and never their names, values, identifiers, or structure.

The hierarchy never invents a writable control. Entity creation still requires a proven stable UUID, supported semantics, and a safe runtime binding.

## Miniserver-local content

Provider-owned services, system variables, weather data, and integrated I/O are sections of the Miniserver node in this hierarchy view. Version 1 preserves existing service-module device-registry entries and entity ownership; it does not delete, migrate, or reassign them. A physical extension or endpoint becomes a hierarchy descendant only when the owner resolver proves a physical relationship. Its functions remain part of that device rather than becoming sibling hierarchy nodes.

HA `via_device_id` mirrors only proven physical relationships. Display grouping does not mutate entity identity, ownership, or automation references.

## Physical placement metadata

Physical placement is separate from HA area and bus topology. A bounded `InstallationPlacement` value may contain verified installation label, cabinet, row, and order/position fields.

Local schema inspection established the Loxone attributes `Installation`, `SwitchBoard`, `SwitchBoardRow`, and `SwitchBoardPos` without reading their values into development artifacts. They map to installation label, cabinet, row, and position. No other raw attribute is admitted. Each admitted field receives:

- an explicit parser mapping;
- length/type/range validation;
- sensitivity inheritance;
- snapshot codec and privacy tests.

Missing or invalid fields are omitted. XML document order, titles, parent keys, and arbitrary attributes must not be interpreted as cabinet order. Placement values may be returned only to an authenticated local HA administrator for this view. Diagnostics, Repairs issues, logs, notifications, tests, and committed fixtures expose only field presence or synthetic values.

## Bulk area repair

One active issue represents the current conflict set for a config entry. Its issue identity contains a fingerprint of that immutable conflict set, so an old flow cannot remove a newer issue. Opening it reloads the current provider-bound snapshot and displays native repeated rows grouped by stable Loxone room identity.

Each group shows safe device references, current HA area, and Loxone room. Its explicit target is one of:

- use an existing HA area selected by `AreaSelector`;
- create a named HA area;
- keep current HA assignments as user-owned overrides;
- remove the HA assignment when Loxone validly requests no room.

The default operation is a room-level mapping `(entry_id, provider_identifier, loxone_room_uuid) -> area_id`, so future devices in that Loxone room follow the same HA area. A second native step carries explicit per-device keep overrides for mixed decisions inside a room group. Conflicts without a Loxone room are never grouped or persisted under `None`; each device offers only keep or ownership-checked clear. Names are presentation only and never mapping identity.

Before mutation, the backend reloads every conflict, verifies exact nonempty token membership, provider, config-entry lifecycle, room identities, selected area IDs, and normalized new-area names. No mutation occurs when preflight fails. The resolver uses one lock owner and persists batch intent/progress before registry mutation. A room mapping becomes effective only after every selected device in that group succeeds; idempotent replay resumes partially applied work. Unresolved or stale work does not complete the Repairs flow, so its current issue remains active. A newly created global HA area is never deleted automatically after a later failure.

## Hierarchy view

A read-only integration view receives a privacy-safe tree projection from a config-entry-scoped backend command. With multiple loaded entries, the operator explicitly selects the entry. Snapshot generation time and freshness are displayed; a last-known-good snapshot is not rejected merely because of age. The initial UI is deliberately small:

- expand/collapse nodes;
- text filter;
- badges with active, prepared, inventory-only, unsupported, and protected counts;
- optional physical placement lines, also shown read-only beside affected devices in the repair flow;
- links to existing HA device/entity pages.

The response contains no raw XML and is generated from the validated snapshot. The command rejects an unloaded entry, provider mismatch, absent snapshot, or unauthorized config-entry scope. It distinguishes projected capability from read-only registry/state enrichment: active, prepared, disabled, and unavailable are never inferred from snapshot names.

## Change and warning behavior

- A room change updates the room mapping plan and produces one aggregate warning when human input is required.
- A physical owner/bus change updates `via_device_id` without changing entity unique IDs.
- A function gaining or losing a safe contract changes its capability status and raises the existing automation-impact warning when consumers may break.
- Placement-only changes update display metadata and do not move areas or trigger automation-impact warnings.
- Refresh, restart, timeout, dismissal, or opening a flow never counts as consent.

## Test strategy

Tests target contracts with meaningful regression risk:

1. Generic ownership projection for provider-local functions, nested physical buses, unknown node types, and function ownership.
2. Sensitive descendants collapse to bounded protected metadata and never leak presentation or identifiers.
3. Stable room mapping and device override behavior across rename, refresh, and restart.
4. Aggregate repair preflight rejects stale tokens, provider changes, deleted areas, name collisions, and lifecycle changes before mutation.
5. Partial operational failure leaves unresolved groups repairable and never deletes a global HA area.
6. Snapshot migration and codec round-trip for admitted placement fields.
7. One view-contract test proves the safe hierarchy schema and capability counts.

No dedicated tests are added for trivial getters, static translations, framework rendering, or permutations already covered by the contract tests. Existing full-suite, high-signal lint, compile, JSON, diff, and privacy gates remain mandatory.

## Acceptance criteria

- No supported or unsupported safe function disappears merely because it has no entity.
- Provider-local content is grouped under the Miniserver; only proven physically connected hardware forms descendant devices.
- Twenty room conflicts can be handled from one native flow without opening twenty issues.
- Existing-area selection and explicit area creation work without name-based identity.
- The hierarchy and repair paths remain generic across technical device types.
- No real installation-identifying data appears in diagnostics, tests, commits, issue data, logs, notifications, or public output; bounded placement values are visible only in the authenticated admin hierarchy view.
