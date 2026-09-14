# Engineering inventory and owner resolution

## Purpose and scope

The engineering inventory complements normal `LoxAPP3.json` discovery with a
sanitized view of devices, buses, service modules, and channels from the complete
engineering configuration. Existing PyLoxone entities discovered through
`LoxAPP3.json` remain authoritative. The feature does not replace or duplicate
them.

Engineering access is local and read-only. The integration uses only HTTP GET
requests and encrypted FTPS reads. It never uploads, activates, or writes an
engineering configuration and never tests an output by changing it.

## Using the inventory

1. Open the PyLoxone Miniserver device in Home Assistant.
2. Press the **Refresh engineering inventory** configuration button. A manual
   refresh forces a complete read after pending committed work has been
   reconciled (see [Refresh and recovery](#refresh-and-recovery)).
3. Review the bounded Home Assistant notification for the number of discovered
   nodes, prepared read-only channels, and reachable runtime bindings.
4. Inspect the device registry for the resolved physical and logical hierarchy.
   Download the integration diagnostics when the complete sanitized inventory
   and its exposure reasons are needed.

Authentication, transport, download, parse, or runtime-probe failures can cause
a refresh error, but the Home Assistant UI deliberately shows only a generic
error and retry message. Correct the likely cause and press the button again.
Do not delete the integration entry or its stored state as a recovery step.
Every such failure preserves the last good cache.

## Device topology

Owner resolution uses stable technical identities and the engineering ancestry,
not display names. Typical results are:

- A newly connected Tree endpoint appears below `Miniserver → Tree`, even
  when all of its entities are disabled.
- An Air endpoint such as `ST-F01` and a 1-Wire endpoint use their Link-connected
  extension as the immediate parent.
- Internal digital inputs, analog inputs, and status values attach to the
  Miniserver.
- Weather values and system-variable values are grouped below their respective
  service-module devices, which in turn belong to the source Miniserver.

Structural captions may remain visible in the sanitized inventory path, but
they do not become fabricated devices. Unsupported or empty modules remain
inventory rows unless there is enough evidence to register a useful device.

## Entity identity and availability

Engineering UUIDs are entity identities. A safe change to a name, room, or
topology path therefore does not create a new entity. PyLoxone may update device
presentation and association, but it does not forcibly rename a user-customized
Home Assistant entity ID.

Verified numeric and boolean channels may be prepared as read-only sensor or
binary-sensor entities. They are disabled by default and can be enabled from the
Home Assistant entity registry when wanted. Cached entities start unavailable;
they become available only after a bounded runtime rebind proves the expected
state UUID and a compatible finite value. Bad or unverified runtime values never
replace the last safe value.

## Refresh and recovery

Before starting a new read, PyLoxone drains any pending work from an earlier
committed generation. After that succeeds, manual refresh forces a complete
engineering archive read. Automatic refresh skips the archive download only
when a cached snapshot exists and a usable scalar `lastModified` revision is
unchanged. Without that cache or revision, it performs a complete read. When
the unchanged-revision optimization applies, PyLoxone still attempts a bounded,
read-only runtime rebind so cached entities can recover.

A validated inventory is committed before registry, publication, and maintenance
phases are completed. Those phases are idempotent and are replayed in order after
a retry or restart. A failed phase does not authorize a new configuration to
replace the pending committed generation. Retry the refresh after correcting
the bounded status rather than deleting integration state.

## Rooms and Home Assistant Repairs

Loxone supplies the desired room, but Home Assistant user assignments are
preserved whenever PyLoxone cannot prove that the current assignment is still
managed by this running integration process. Area equality, timestamps, and
history are not treated as ownership evidence. Automatic room synchronization
pauses only for the affected device; other safe registry updates can continue.

Open **Settings → System → Repairs**. One aggregate issue per loaded config
entry covers all current conflicts; notifications contain only their bounded
count. Names and device summaries are shown inside the native repair form.

1. Review room groups. Group membership uses exact opaque room UUIDs, never
   matching labels. Select an existing Home Assistant area by ID, explicitly
   create a new area with a normalized name, or keep Home Assistant assignments.
   A name collision requires explicit selection of the existing area; it never
   authorizes adoption by name.
2. Review each device. Room-group members can apply the group decision or keep
   their current Home Assistant assignment as an individual override. Devices
   without a room UUID can only keep their assignment or explicitly clear it.
   All decisions are checked again when submitted.

Keys identify the original rows and must not be edited, duplicated, or omitted.
Unused target fields stay empty. Ordinary validation errors retain entered
values. Changed conflicts or lifecycle/provider bindings invalidate the open
flow: reopen the synchronized current repair instead of submitting old choices.
A successful repair completes only after a fresh read proves no conflicts
remain for that entry. Dismissing the notification is not consent.

The batch boundary records bounded write-ahead progress. A partial result or
restart leaves current conflicts available for retry. If creation succeeded but
its exact returned area identity was not durably acknowledged, or the created
area was removed or renamed, retry can require manual selection of an existing
area. It does not silently create another area, adopt by name, delete a global
Home Assistant area, or roll one back. Refreshing or waiting is not consent to
clear an area. The flow performs no Loxone writes.

The generic hierarchy view supports review of controller roots, supported
branches and devices without product-specific filtering. Unsupported and
protected/suppressed inventory statuses remain explicit and non-writable;
hierarchy presentation does not grant registry ownership. The native Repairs
flow is the only area-decision write interface; the hierarchy is a read-only
view, not a second custom write panel.

## Consumer-impact warnings

A warning can be created when a referenced entity is removed, becomes
platform-incompatible, or has a proven applicable area change. The warning may
report affected automations, scripts, scenes, and groups. PyLoxone never edits,
disables, or rewrites those consumers; review them manually after resolving the
underlying inventory change.

## Inventory and privacy boundaries

The complete sanitized inventory explains whether a channel is readable,
configured-only, unsupported, or suppressed. Outputs, unknown channels, and
unsupported channels remain non-writable and are inventory-only when safe to
describe. NFC access, tag, and code data, arbitrary text, sensitive descendants,
and unsafe presentation data are suppressed.

Diagnostics use an explicit safe allowlist, but they are not fully anonymized.
They can contain technical UUIDs, config-entry and provider identifiers
(including a Miniserver serial), owner identities, topology paths, and permitted
device names and rooms. Review and redact diagnostics before sharing them. They
omit raw engineering configuration, runtime values, network endpoints,
credentials, arbitrary exception text, and sensitive row metadata; suppressed
sensitive rows are reduced to fixed structural status. The archive itself is
not persisted as diagnostics or as an inventory snapshot.

## Stale-device cleanup

Stale-device handling is audit-only by default. In the PyLoxone integration
options, automatic cleanup can be enabled explicitly and its grace rule selected:

- **Successful observations** requires a configured number of complete,
  committed and applied engineering generations.
- **Elapsed time** requires the configured missing duration.
- **Both** requires both thresholds.

The same committed observation token never increments the observation count
twice. Failed or incomplete reads and runtime-only rebinds do not increment
that counter. Wall-clock time still passes, and a later safe maintenance audit
may satisfy elapsed-time eligibility without counting another observation.

## Multiple Miniservers

Device identities and operational, runtime-event, warning, consumer-impact,
Repairs, maintenance, and cache state are scoped by Home Assistant config entry
and source provider. Engineering entity unique IDs remain the globally owned,
unprefixed engineering UUID. PyLoxone never steals an existing entity owner; a
competing entry with the same UUID is suppressed. Runtime state and Repairs
decisions still cannot be applied across Miniservers.
