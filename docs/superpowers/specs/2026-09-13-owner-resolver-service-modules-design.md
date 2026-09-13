# Owner Resolver and Service Modules Design

## Status

Approved in conversation on 2026-09-13. This document defines the behavior to implement before any upstream communication or pull request is created.

## Problem

PyLoxone currently discovers useful values from the public Loxone structure and, on explicit request, from the complete engineering configuration. The engineering onboarding is deliberately conservative: it prepares only verified numeric sensors, disables them by default, and creates a Home Assistant device only as a side effect of an entity. Its device association also flattens the topology by pointing prepared devices directly at the Miniserver.

This produces three user-visible gaps:

1. Physical hardware without an enabled entity is missing from the Home Assistant device registry. A Tree device such as an NFC Code Touch can therefore be present and readable in the engineering configuration without appearing as a traceable device.
2. The physical path is lost. A user cannot see that an Air device is reached through an Air Base Extension on Loxone Link, that a 1-Wire sensor belongs to a 1-Wire Extension on Link, or that a Tree device is connected through the Miniserver Tree interface.
3. Miniserver-owned functions are fragmented or hidden. Weather data, system variables, internal I/O, status channels, and other service modules are not consistently grouped under the Miniserver that provides them.

A sanitized reference topology covers all three cases: a Miniserver, Link and Tree branches, an Air bridge, a wired-bus extension, a Tree endpoint, internal input and output groups, WeatherData channels, and SysVar channels. Runtime fixtures model readable WeatherData and SysVar channels through non-mutating UUID `/all` responses. Device labels such as numbered socket names and room names may be used when useful for behavior tests, but personal or location-identifying data may not be copied from a real installation.

## Goals

- Preserve the source Miniserver for every engineering element, including installations with more than one PyLoxone config entry.
- Register physical hardware even when it has no enabled Home Assistant entity.
- Reconstruct and expose the physical path from Miniserver through bus and bridge nodes to an end device.
- Group Miniserver-provided services and their channels into stable logical modules.
- Show the complete sanitized engineering tree, including configured-only and unsupported items, without turning every structural caption into a Home Assistant device.
- Classify read and write capabilities without issuing exploratory write commands.
- Keep existing entity unique IDs stable and avoid unnecessary entity-ID changes.
- Keep NFC identifiers, access codes, credentials, and arbitrary string payloads out of entities, diagnostics, notifications, and stored public metadata.
- Keep personal names, user-account names, geographic or installation names, GPS coordinates, private network addresses, and other person- or location-identifying artifacts out of committed tests, documentation, logs, and examples. The approved GitHub author name and GitHub-provided noreply address are the only identity exception.
- Continue to make destructive stale-device cleanup opt-in and subject to the configured persistent grace policy.

## Non-goals

- Editing the Loxone engineering configuration from Home Assistant.
- Automatically exposing every engineering output as a writable Home Assistant entity.
- Treating page layout, captions, references, or program blocks as physical hardware.
- Publishing changes or opening an upstream pull request before local review and live validation.
- Replacing the normal LoxAPP3-based entity discovery used by existing PyLoxone entities.

## Architecture

The feature is split into four independent layers. Parsing remains side-effect free; registry and entity mutations occur only after a complete snapshot has been resolved.

### 1. Source context

Every inventory is wrapped in an immutable source context containing:

- Home Assistant config-entry ID
- Miniserver serial number
- Miniserver title and model when known
- source archive name, configuration version, and configuration timestamp

The config entry and serial number are the authoritative provider identity. This is essential because document-level service containers such as `WeatherServer` and `GlobalStates` are siblings of the `LoxLIVE` hardware node rather than its XML descendants. They still belong to the Miniserver from which that single project archive was downloaded.

No ownership decision may be based on a translated title or user-editable name.

### 2. Topology graph and owner resolver

The parsed engineering elements form a directed graph keyed by their stable engineering UUIDs. The graph classifies nodes as:

- `miniserver`: the `LoxLIVE` physical controller
- `bus`: a transport interface such as Link or Tree
- `bridge`: a physical extension that owns a downstream transport, such as an Air Base Extension or 1-Wire Extension
- `physical_device`: a hardware endpoint such as an Air socket or Tree NFC Code Touch
- `service_module`: a logical provider-owned group such as WeatherServer or GlobalStates
- `channel`: a readable or controllable runtime endpoint
- `structural`: captions, references, pages, and other organizational nodes

For each channel or device, the owner resolver returns an immutable resolution with:

- source Miniserver identity
- owning physical or logical node
- immediate `via_device` node when one exists
- normalized bus kind
- full sanitized topology path
- room inherited from the engineering configuration
- resolution status and reason

Resolution follows these rules in order:

1. Walk the UUID parent chain and select the nearest recognized physical device or service module.
2. Preserve recognized bus and bridge nodes in the transport path.
3. Skip structural captions as owners, but retain useful branch labels such as `Tree Ast` in the diagnostic path.
4. Assign document-level provider services to the source Miniserver from the source context.
5. Assign internal channels whose physical ancestor is `LoxLIVE` directly to the Miniserver.
6. Leave a node unresolved when neither its type nor ancestry proves ownership. Report it in diagnostics instead of guessing.

The expected synthetic reference topology is:

```text
Miniserver A
├── Link
│   ├── Wireless bridge A
│   │   └── Wireless endpoint A
│   └── Wired-bus extension A
│       └── Wired sensor A
└── Tree
    └── Tree endpoint A
```

Link and Tree are visible logical bridge devices. A branch caption remains topology metadata rather than a fabricated physical device. Home Assistant `via_device` associations use the nearest registered node, not the Miniserver for every descendant.

### 3. Service graph

Provider-owned functions use a logical graph parallel to the physical graph. A configured service module with useful channels is registered as a logical Home Assistant child device via the source Miniserver. Its entities belong to that module rather than being scattered across one device per value.

Initial recognized modules are:

- Weather server (`WeatherServer` and `WeatherData`)
- System variables (`GlobalStates` and `SysVar`)
- Operating modes
- Time functions
- System status and device monitoring
- Network services and configured plugins, including their availability channels
- Virtual inputs and virtual outputs
- Recorded tasks and messages
- Intercom, lighting groups, and other explicitly typed provider containers

The module registry is type-driven. Titles are presentation only. Unknown module types remain visible in the complete inventory with `unsupported` status and are not silently discarded.

A structural or empty module is not created as a Home Assistant device. It remains visible in the integration inventory, with its configuration and capability status. This prevents device-registry clutter while preserving complete engineering visibility.

### 4. Capability resolver

Ownership and capability are separate decisions. Each channel receives one capability state:

- `readable`: a non-mutating runtime read succeeded
- `writable`: a documented write command and an explicit PyLoxone handler exist
- `read_write`: both conditions are satisfied
- `configured_only`: present in engineering configuration but no runtime endpoint is proven
- `unsupported`: recognized but not safely mapped to a Home Assistant platform
- `sensitive`: intentionally suppressed from normal exposure

I/O prefixes such as `I`, `AI`, `Q`, `AQ`, `SYS`, or `WDC` are hints, not proof of write access. Runtime reads may use the existing UUID-first `/all` probe. Write capability is never tested by changing a real value. It is granted only from a whitelisted type and known command contract already supported by a dedicated handler.

Initial exposure policy:

- WeatherData and SysVar values: readable sensors when their runtime binding succeeds
- Digital inputs: read-only binary sensors when their value semantics are binary
- Analog inputs: read-only sensors with verified units
- Online/status channels: diagnostic binary sensors after boolean semantics are verified
- Relay and analog outputs: visible in the inventory; entity creation remains disabled by default and no write handler is added without separate review
- NFC tag definitions, keycodes, credentials, arbitrary text states, and personal labels below sensitive containers: suppressed

## Home Assistant representation

### Device registry

The device registry contains:

- one device for each Miniserver config entry
- recognized Link and Tree bus nodes
- physical bridges and extensions
- physical endpoint devices, even if no entity is enabled
- logical service modules that contain useful supported channels

Device identifiers use stable technical identities scoped to the source Miniserver:

- Miniserver: `(DOMAIN, serial_number)` with the config-entry ID only as a fallback when no serial is available
- engineering graph node: `(DOMAIN, "{serial_number}:{engineering_uuid}")`
- typed service without an engineering UUID: `(DOMAIN, "{serial_number}:service:{normalized_type}")`

User-facing names and rooms may change without changing these identifiers. Existing engineering entity unique IDs remain their engineering UUIDs. When an existing single-source pseudo-device uses the legacy unscoped engineering UUID, registry synchronization migrates its entity associations to the scoped owner and leaves the old device to the normal grace-based cleanup path. It must not merge an unscoped device when more than one config entry claims the same engineering UUID.

### Entity registry

Entities attach to the resolved owner device. Moving an entity from an old pseudo-device to its resolved owner changes the device association but not its unique ID. The integration must not forcibly rename a user-customized entity ID.

New engineering entities start disabled unless the exposure policy explicitly marks a read-only diagnostic/input class safe and useful by default. Writable engineering entities are never enabled automatically.

### Complete inventory

The integration exposes a sanitized tree/table model containing every parsed node and these columns:

- name and technical type
- stable UUID when present
- source Miniserver
- owner
- bus and topology path
- room
- suggested Home Assistant platform
- capability status
- exposure status and reason

The inventory includes empty, configured-only, unsupported, and suppressed nodes. Sensitive values and credentials are omitted rather than merely masked after storage.

## Refresh, persistence, and change handling

The sanitized resolved inventory is persisted per config entry. On Home Assistant startup it is restored before the first successful engineering download so registered hardware does not disappear during a temporary Miniserver or FTPS outage.

An explicit refresh button remains available. The integration also evaluates an automatic refresh trigger after the initial successful LoxAPP3 load and after every successful Miniserver reconnect/reload. It compares the current LoxAPP3 `lastModified` value with the value stored alongside the last resolved inventory. A changed value queues one debounced engineering refresh; an unchanged value causes no FTPS archive download. A missing or unparsable `lastModified` value does not discard the cache and leaves the manual refresh available. Failed refreshes preserve the last known-good snapshot and create a bounded repair notification; they never replace valid state with an empty inventory.

After a successful refresh, the integration computes a UUID-based diff:

- new nodes are registered or prepared according to policy
- renamed nodes update presentation metadata without changing identity
- room moves update suggested or integration-owned area metadata without overwriting an explicit user override
- moved hardware updates its resolved `via_device` path
- removed nodes enter the existing persistent stale-device grace process
- automations referencing entities affected by removal or an incompatible platform change are reported through Home Assistant repair/notification output

Automatic deletion remains disabled by default. A failed, empty, or incomplete structure load never increments the missing-observation count.

## Error handling and safety boundaries

- Parse and resolve a complete immutable snapshot before mutating registries.
- Enforce the existing archive, compressed-data, and XML size limits.
- Reject XML entity declarations and malformed parent cycles.
- Bound parent traversal and report cycles or missing ancestors as unresolved.
- Never downgrade a proven physical owner to a title-based guess.
- Never perform write probes against the Miniserver.
- Never expose NFC IDs, keycodes, passwords, tokens, or arbitrary string payloads.
- Never copy personal names, user-account names, geographic or installation names, GPS coordinates, private network addresses, or other person- or location-identifying artifacts into repository fixtures or documentation. Numbered device labels and room names are permitted. Examples otherwise use synthetic labels and invented identifiers that cannot be traced back to a person or deployment location.
- Apply privacy filtering through an explicit field allowlist rather than a list of known personal values. Live exports must never be converted wholesale into repository fixtures. Source fields such as project or installation name, current user, location, latitude, longitude, local URL, remote URL, host address, and access-control labels are excluded before any fixture, log excerpt, or document is produced.
- Keep the last known-good inventory on network, authentication, parsing, or runtime-probe failure.
- Scope storage keys, dispatcher signals, identifiers, and notifications by config entry to support multiple Miniservers.

## Testing strategy

Unit tests use synthetic, sanitized engineering XML and runtime responses. No production credentials or personal identifiers are committed.

Required cases:

1. Resolve `Miniserver -> Tree -> Tree branch caption -> TreeDevice` while keeping the caption only in the diagnostic path.
2. Resolve `Miniserver -> Link -> Air Base Extension -> Air device` with correct `via_device` links.
3. Resolve `Miniserver -> Link -> 1-Wire Extension -> 1-Wire sensor` and attach the sensor to the extension-owned device.
4. Register a physical device that has no supported or enabled entity.
5. Assign internal digital, analog, and relay channels to the Miniserver.
6. Assign document-level WeatherServer and GlobalStates modules to the correct source Miniserver.
7. Keep two config entries with identical display names isolated by their technical source identities.
8. Prove that a successful read does not imply write capability.
9. Suppress NFC and keycode definitions from public inventory and diagnostics.
10. Preserve entity unique IDs while changing device association, room, name, or transport path.
11. Preserve the last good graph after failed, empty, cyclic, or malformed refresh input.
12. Keep automatic cleanup disabled by default and enforce observation, time, and combined grace modes.
13. Produce change warnings for removed entities referenced by automations without modifying those automations.
14. Reject a fixture-generation input containing forbidden identity, location, coordinate, URL, address, or access-control fields while retaining permitted numbered device labels and room names.

Integration-level tests verify that Home Assistant device-registry entries form the expected `via_device` chain, service-module entities share one logical device, and disabled-by-default outputs cannot be invoked.

## Acceptance criteria

- The NFC Code Touch appears as a device even when all of its entities are disabled.
- Its Home Assistant device path identifies the source Miniserver and Tree interface.
- Air and 1-Wire devices show their Link-connected bridge or extension.
- Miniserver internal I/O is visible under the Miniserver; only groups present in the parsed inventory are created.
- Weather values appear together under one Weather Server service module owned by the correct Miniserver.
- System variables appear together under one System Variables service module owned by the correct Miniserver.
- The full inventory shows all discovered modules and explains why unsupported or configured-only items have no entity.
- No real output changes while refreshing, probing, resolving, or registering the inventory.
- Existing unique IDs remain unchanged.
- Existing tests and all new topology, capability, privacy, and registry tests pass on the repository's supported Python and Home Assistant versions.

## Delivery boundaries

Implementation remains local on a dedicated feature branch until tests, privacy review, and live Home Assistant validation are complete. The work must be split into reviewable commits by responsibility. No fork push, discussion update, upstream pull request, or other external publication is part of this implementation phase.

Before any later push, review both the final upstream diff and every commit that would be transmitted. If an ancestor contains personal names, user-account names, geographic or installation names, GPS coordinates, private network addresses, or comparable identifying artifacts, rebuild the deliverable branch from the official upstream base with sanitized patches; adding a later cleanup commit is insufficient because the earlier commit remains visible in history. Commit metadata may contain only the approved GitHub username and GitHub-provided noreply address.
