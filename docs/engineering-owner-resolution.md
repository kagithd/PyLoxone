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
   refresh always performs a complete read.
3. Review the bounded Home Assistant notification for the number of discovered
   nodes, prepared read-only channels, and reachable runtime bindings.
4. Inspect the device registry for the resolved physical and logical hierarchy.
   Download the integration diagnostics when the complete sanitized inventory
   and its exposure reasons are needed.

If a refresh reports an error, correct the displayed authentication, transport,
download, parse, or runtime-probe condition and press the button again. Do not
delete the integration entry or its stored state as a recovery step. Every such
failure preserves the last good cache.

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

Manual refresh always downloads and evaluates a complete engineering archive.
Automatic refresh compares the scalar `lastModified` revision with the stored
revision and downloads an archive only after it changes. When the revision is
unchanged, no FTPS archive download occurs, but PyLoxone still attempts a
bounded, read-only runtime rebind so cached entities can recover.

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

Open **Settings → System → Repairs** to resolve a reported room conflict.
The native repair offers exactly these choices:

- **Apply Loxone room** applies the desired room for that exact, still-current
  conflict.
- **Keep HA room** keeps the Home Assistant assignment and records it as
  user-owned.

An intentional Loxone no-room assignment can clear a Home Assistant area only
with matching current-process managed-ownership evidence or the explicit
exact-token repair decision. Restarting Home Assistant, refreshing the
inventory, dismissing a notification, or waiting is not consent. If a conflict
changes while its repair is open, reopen the current repair and decide again.

For example, moving `ST-F07` from `Office` to `Workshop` updates a proven
integration-managed assignment. If the current Home Assistant assignment is
unexplained or ambiguous, it remains unchanged until one of the two repair
choices is confirmed.

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

Diagnostics use an explicit safe allowlist. They omit raw engineering
configuration, runtime values, network endpoints, credentials, private
identifiers, arbitrary exception text, and sensitive presentation data. The
archive itself is not persisted as diagnostics or as an inventory snapshot.

## Stale-device cleanup

Stale-device handling is audit-only by default. In the PyLoxone integration
options, automatic cleanup can be enabled explicitly and its grace rule selected:

- **Successful observations** requires a configured number of complete,
  committed engineering reads.
- **Elapsed time** requires the configured missing duration.
- **Both** requires both thresholds.

The same committed observation token never increments the observation count
twice. Replaying it may still re-evaluate elapsed-time eligibility. Failed or
incomplete reads and runtime-only rebinds do not advance the grace period.

## Multiple Miniservers

Device and registry identities, runtime events, warnings, consumer impacts,
Repairs issues, maintenance state, and cached inventory are scoped by Home
Assistant config entry and source provider. A state or repair belonging to one
Miniserver cannot be applied to another, even when display names or engineering
UUID text happen to match.
