# Room Repair Scope Design

## Goal

Keep Loxone topology visible without treating every discovered structural node
as a Home Assistant room assignment. Room synchronization must be automatic for
unambiguous external physical devices and must ask for input only when a safe
target cannot be determined.

## Problem

The existing repair flow groups every `EngineeringAreaConflict` that has a
Loxone `room_uuid`. It does not distinguish a physical device from a bus,
bridge, or provider-owned internal function. A physical extension may have its
own installation room even when it is connected through a bus. The flow
therefore must not use bus membership as a room-assignment rule. It also
exposes opaque group keys and backend action values because the generic
repeated-row editor is being used as an operator-facing decision table.

## Decision policy

The registry plan will assign each resolved node one of these internal area
policies, derived solely from validated node role and resolved
ownership/topology. Product names, display labels, bus names, and entity count
are never inputs to the policy.

- `room_capable_device`: every resolved external `physical_device`, including
  extensions and endpoints reached through any bus. It may be synchronized to
  an HA area even if it currently exposes no HA entity.
- `provider_owned`: the Miniserver and its integrated I/O and services. They
  remain attached to the Miniserver and do not receive an independent area.
- `topology_only`: a bus, bridge, or structural grouping. It remains in the
  hierarchy and keeps placement metadata, but never creates a room conflict or
  changes an HA area.
- `unassigned`: a safe inventory node without enough identity evidence. It
  remains visible but receives no area mutation.

The classification is deliberately conservative: an uncertain node is not
silently assigned to an area. Its hierarchy location is still retained.

## Synchronization behavior

For a `room_capable_device` with a Loxone room:

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

For a `room_capable_device` without a Loxone room, the repair offers an
explicit device-level fallback: retain its current HA area, select an existing
HA area, or explicitly create one. This creates a device-scoped HA fallback,
not a synthetic Loxone-room mapping and never writes to Loxone. If Loxone later
supplies a room UUID for that device, the fallback is released and normal
Loxone-room synchronization becomes authoritative again.

## Repairs presentation

The repair backend receives only unresolved `room_capable_device` conflicts.
Room groups show the Loxone room label and device labels; devices without a
Loxone room use a device-level fallback row. Neither view exposes UUIDs.
Internal action values are translated to operator language. Technical topology
is available from the read-only hierarchy instead of the room repair.

The current native object editor remains the implementation mechanism for this
increment. A dedicated custom table is out of scope: it would introduce a
separate frontend and persistence boundary without improving room decisions.

## Safety and recovery

- The existing stable-room mapping and batch journal remain authoritative.
- Classification happens before conflict persistence, so structural nodes
  cannot revive a stale repair after restart.
- Existing unresolved room-capable-device work stays resumable; no completed mapping or
  area assignment is cleared by this change.
- A device-level fallback is released only after a later verified Loxone room
  assignment; it is never converted into a fictitious Loxone-room mapping.
- An area is created only through the existing explicit `create` repair action.
- No names, raw engineering attributes, identifiers, or placement values enter
  issue data, logs, tests, diagnostics, or commits.

## Test strategy

Four behavior tests cover the regression boundary:

1. A topology-only node with a room is excluded from conflicts while a physical
   extension on the same bus remains eligible.
2. A physical device with a unique name match resolves without a repair and
   stores the stable room mapping.
3. A physical device without a Loxone room allows an explicit device-level HA
   fallback and releases it when a verified Loxone room later appears.
4. An ambiguous target remains an aggregate repair candidate.

Existing lifecycle, journal, token preflight, and slow-resolution tests cover
the mutation/recovery boundary and will be run as the relevant regression set.
No tests are added for translation wording or framework rendering.

## Acceptance criteria

- Bus and structural nodes never appear in a room repair solely because they
  carry a Loxone room.
- Every resolved external physical device remains room-capable, including an
  extension installed away from its parent Miniserver.
- A device without a Loxone room can receive an explicit HA-only fallback;
  a later Loxone room takes precedence.
- No classification depends on a device or room name.
- The hierarchy remains the source for topology and placement; Repairs contains
  only actionable room decisions.
- Existing automation-facing unique IDs and entity ownership do not change.
