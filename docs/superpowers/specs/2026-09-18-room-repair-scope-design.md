# Room Repair Scope Design

## Goal

Keep Loxone topology visible without treating every discovered technical node as
a Home Assistant room assignment. Room synchronization must be automatic for
unambiguous end devices and must ask for input only when a safe target cannot
be determined.

## Problem

The existing repair flow groups every `EngineeringAreaConflict` that has a
Loxone `room_uuid`. A bus, extension, or provider-owned infrastructure node can
also carry that metadata. It is therefore presented as though it were an
end-device that belongs in a Home Assistant area. The flow exposes its opaque
group key and backend action values because the generic repeated-row editor is
being used as an operator-facing decision table.

## Decision policy

The registry plan will assign each resolved node one of these internal area
policies, derived solely from validated node role, resolved ownership/topology,
and its safe entity/function contract. Product names and display labels are
never inputs to the policy.

- `end_device`: a device whose exposed functions have a meaningful room
  context. It may be synchronized to an HA area.
- `topology_only`: a provider, bus, extension, structural owner, or technical
  container. It remains in the hierarchy and keeps placement metadata, but
  never creates a room conflict or changes an HA area.
- `unassigned`: a safe inventory node without enough evidence for either
  policy. It remains visible but receives no area mutation.

The classification is deliberately conservative: an uncertain node is not
silently assigned to an area. Its hierarchy location is still retained.

## Synchronization behavior

For an `end_device` with a Loxone room:

1. Use a previously stored mapping keyed by the stable Loxone room UUID.
2. Otherwise use exactly one existing HA area whose normalized name equals the
   current Loxone room name.
3. If neither exists, keep the device unassigned and emit one aggregate repair
   for the unresolved room group. The repair may select an existing HA area or
   explicitly create one.
4. If matching is ambiguous, do not guess; emit the same aggregate repair.

An exact match is a convenience lookup, not persistent identity. Once a
mapping exists, later Loxone room renames preserve the stable UUID mapping.
Manual HA assignments are not overwritten without an established mapping or an
explicit repair decision.

## Repairs presentation

The repair backend receives only unresolved `end_device` conflicts. It shows
the Loxone room label and device labels, never UUIDs. Internal action values
are translated to operator language. Technical topology is available from the
read-only hierarchy instead of the room repair.

The current native object editor remains the implementation mechanism for this
increment. A dedicated custom table is out of scope: it would introduce a
separate frontend and persistence boundary without improving room decisions.

## Safety and recovery

- The existing stable-room mapping and batch journal remain authoritative.
- Classification happens before conflict persistence, so filtered technical
  nodes cannot revive a stale repair after restart.
- Existing unresolved end-device work stays resumable; no completed mapping or
  area assignment is cleared by this change.
- An area is created only through the existing explicit `create` repair action.
- No names, raw engineering attributes, identifiers, or placement values enter
  issue data, logs, tests, diagnostics, or commits.

## Test strategy

Three behavior tests cover the regression boundary:

1. A topology-only node with a room is excluded from conflicts while remaining
   in the hierarchy input.
2. An end device with a unique name match resolves without a repair and stores
   the stable room mapping.
3. An ambiguous or missing target remains an aggregate repair candidate.

Existing lifecycle, journal, token preflight, and slow-resolution tests cover
the mutation/recovery boundary and will be run as the relevant regression set.
No tests are added for translation wording or framework rendering.

## Acceptance criteria

- Bus and module nodes never appear in a room repair solely because they carry
  a Loxone room.
- A room-assigned end device remains eligible for automatic synchronization.
- No classification depends on a device or room name.
- The hierarchy remains the source for topology and placement; Repairs contains
  only actionable room decisions.
- Existing automation-facing unique IDs and entity ownership do not change.
