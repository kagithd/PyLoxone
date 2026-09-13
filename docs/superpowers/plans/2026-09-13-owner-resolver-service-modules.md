# Owner Resolver and Service Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only, privacy-safe engineering inventory that reconstructs Loxone hardware ownership, registers entity-less devices and Miniserver service modules in Home Assistant, prepares verified read-only entities, persists the last good topology, and warns about configuration changes that affect Home Assistant consumers.

**Architecture:** The existing engineering XML parser and runtime probe remain side-effect free. New topology, capability, snapshot, registry, and change modules transform a complete engineering download into one immutable, source-scoped candidate before Home Assistant registries are changed. A validated candidate is committed as a deterministic last-known-good generation, then applied idempotently to Home Assistant registries; signals, warnings, and stale observations are published only after that generation is fully applied. Existing LoxAPP3 discovery remains authoritative for already-supported entities, while the engineering model fills hardware/topology/service gaps without issuing any write probe.

**Tech Stack:** Python 3.14.7, Home Assistant 2026.8.1 test dependency, pytest 9.1.1, stdlib `dataclasses`, `enum.StrEnum`, `xml.etree.ElementTree`, Home Assistant device/entity/area registries, dispatcher, `Store`, Searcher, and persistent notifications.

**Spec:** `docs/superpowers/specs/2026-09-13-owner-resolver-service-modules-design.md`

## Global Constraints

- Base all work on official `upstream/master` commit `75612473e1d6882c44514e61edc12ed2bf4ab5b9`; the local fork build starts at `0.9.22.12` after the rebase.
- Keep normal LoxAPP3 discovery and every existing entity unique ID unchanged.
- Scope graph identifiers, storage, signals, notifications, and registry mutations by config entry and Miniserver serial.
- Register hardware without enabled entities, but never fabricate a physical device from a caption, page, reference, or layout container.
- Use UUID ancestry and explicit technical type maps for ownership; never use a translated title or editable display name as identity evidence.
- Never issue exploratory writes. A successful read proves only readability.
- Keep relay and analog outputs inventory-visible and without a new writable entity handler.
- Keep new engineering entities disabled by default; users can enable verified read-only channels from the entity registry.
- Omit NFC identifiers, keycodes, credentials, arbitrary text states, and values below sensitive containers from entities, persistence, diagnostics, and notifications.
- Persist only explicitly allowlisted fields; never serialize `EngineeringElement.attributes` or a complete live Loxone export.
- Repository tests and documentation may use numbered device labels such as `ST-F01` and ordinary room names, but must not contain personal names, account names, real installation or geographic names, GPS coordinates, private network addresses, private URLs, or access-control labels.
- Preserve the last known-good snapshot after network, authentication, parsing, empty-input, cycle, or runtime-probe failure.
- Keep stale-device cleanup audit-only by default and honor the existing observation, time, and combined grace policies.
- Do not push the branch, update a GitHub discussion, or open an upstream pull request during implementation or live validation.
- Commit metadata must remain `kagithd <42038442+kagithd@users.noreply.github.com>`.

#### Binding preflight rulings

These rulings take precedence over any older example or step below that is not
yet worded consistently. Each task must keep compatibility adapters until its
callers have migrated, and must run its focused tests plus the complete suite
before commit so intermediate commits remain usable.

1. **Opaque parser identity and complete ancestry.** `EngineeringElement`
   carries both `key` and `parent_key`. A UUID is used as `key` when present;
   otherwise the parser assigns an opaque, deterministic-in-document key such
   as `xml:000123`. Neither key may contain a title, value, address, access
   label, or any other source text. The parser preserves `parent_uuid` for
   compatibility but resolution walks `parent_key`. Duplicate non-empty UUIDs
   invalidate the candidate before any UUID-indexed dictionary is built.
2. **Sensitivity before projection.** Sensitivity propagates over the complete
   `parent_key` chain, including UUID-less containers. Sensitive descendants
   are excluded before runtime probing and sanitized before persistence,
   diagnostics, notification text, or public conversion.
3. **UUID-less provider services.** A UUID-less service receives
   `{provider}:service:{normalized_type}` only when its normalized technical
   type occurs once in the project. Duplicate UUID-less services of the same
   type stay inventory-only with `ambiguous_uuidless_service`; titles never
   disambiguate identity.
4. **Shared candidate and value policy.** Runtime probe candidates come from
   the capability resolver's exact technical-type allowlist, not legacy
   `suggested_platform`. It includes verified digital inputs, analog inputs,
   status/online channels, WeatherData, and SysVar. Runtime numbers must be
   finite. Units pass only through an explicit safe-unit map; a numeric prefix
   followed by arbitrary text is text, not a numeric value. Authentication and
   transport failures are reported separately and never replace the last good
   capability state.
5. **Global Home Assistant entity identity.** Existing engineering entity
   unique IDs stay unchanged. Before exposing a spec, query the entity registry
   for `(domain, platform, unique_id)`. If it belongs to another config entry,
   leave that entry untouched and keep the new row inventory-only with
   `entity_unique_id_owned_by_other_entry`. Registry/persisted ownership must be
   considered even if the owning config entry is unloaded. Tests cover both
   load orders, restart, and an unloaded owner.
6. **Safe reconstructable snapshot.** The private snapshot persists the scalar
   source revision plus only safe fields required to rebuild cached unavailable
   entities and topology: opaque key/parent key, UUID, technical type, safe
   presentation name/room, kind, owner/via identifiers, sanitized path,
   resolution/capability/exposure reasons, semantic platform, safe unit,
   `io_name`, optional proven `state_uuid`, and binding method. A safe binding
   descriptor distinguishes event-capable bindings from bounded rebind-only
   scalar reads. It never persists runtime
   values, endpoints, raw attributes, host/URL data, credentials, provider
   titles, project titles, user/location fields, or access metadata.
7. **Exact registry APIs and two passes.** Use the installed Home Assistant API:
   entry-scoped `async_get_device_by_identifier(identifier, config_entry_id)`,
   global `async_get_entity_id(domain, platform, unique_id)`, and
   `async_update_device(..., via_device_id=...)`. First create/update all
   eligible devices; then resolve their concrete registry IDs and update
   `via_device_id`. Engineering entity `DeviceInfo` contains only the owner
   identifier so platform setup cannot overwrite centrally managed topology.
8. **Committed generation and idempotent replay.** Candidate construction,
   validation, diffing, and impact discovery are pure and use the previous
   snapshot plus pre-mutation registry state. Store the validated snapshot as
   the new committed application generation before registry application. The
   same private envelope stores its sanitized pending impact plan plus separate
   registry-applied and impact-published cursors. Then swap
   coordinator memory and apply a deterministic registry plan idempotently. If
   snapshot storage fails, old memory and registries remain untouched. If
   registry application fails or is cancelled after a partial mutation, retain
   the committed candidate, mark its registry generation pending, publish no
   entity signal/impact warning/stale observation, and replay it at startup or
   retry. This is a recovery boundary, not a transactional rollback claim.
   Home Assistant registries persist on their own delayed schedule, so an
   integration cursor is not proof of external durability. Every process
   startup reconciles the committed desired topology against the freshly loaded
   registries even when the cursor matches; private HA save methods are never
   called. Any still-applicable warning is likewise recreated from the sanitized
   pending plan because persistent notifications are process-local. A manual
   dismissal lasts for the current process and the warning returns after restart
   only if the recorded problem still applies.
9. **Topology freshness and runtime liveness are separate.** An unchanged
   `lastModified` causes zero FTPS downloads but still rebinds cached safe
   channels through their safe binding descriptors. Prepared entities subscribe
   only when an explicit state mapping is proven, through an internal signal
   keyed by `(config_entry_id, state_uuid)`, and accept only verified finite
   numeric or boolean values. A scalar response does not prove that its
   engineering UUID is an event UUID.
   A transport/authentication failure keeps cached topology and marks live
   bindings unavailable.
10. **Exactly-once maintenance and post-apply publication.** Each committed
    complete read allocates a persisted monotonic read sequence. Its application
    token combines provider scope, normalized configuration revision, canonical
    safe-content digest, and that sequence. A new forced complete read is a new
    observation even if the reported revision is unchanged; replay of the same
    committed read is not. Missing revisions fall back to archive configuration
    version and configuration timestamp, never capture time. Registry metadata
    stores committed, registry-applied, impact-published, and last-counted
    tokens plus integration-managed areas. Warnings are published/dismissed and
    stale grace counters advance only after successful registry application, at
    most once per token. Reprocessing the same token may reevaluate elapsed-time
    eligibility without incrementing counters, preserving time and combined
    grace modes. Runtime-only rebind, restart, or retry cannot double-count.
    Under the per-entry refresh lock, all pending phases of the current
    committed generation are drained before the unchanged-revision path and
    before a newer complete read may commit. A drain failure retains the old
    pending envelope and blocks replacement. Maintenance state is consulted even
    when registry and publication cursors match.
11. **Privacy fixture gate.** Synthetic fixtures are created by hand or through
    an explicit input validator that rejects forbidden identity, location,
    coordinate, URL, address, credential, and access-control fields before
    writing anything. Permitted numbered device labels and ordinary room names
    remain available for behavior tests. The final audit evaluates every added
    line and commit intended for transmission; it does not rely on an
    impossible repository-wide zero-hit rule for generic security code.
12. **Live boundary.** Task 9 may prepare a sanitized, git-ignored validation
    report, but backup, installation, integration reload, or Home Assistant
    restart requires a fresh explicit authorization from the user at that gate.

---

### Task 1: Add source-scoped topology types and safe classification

**Files:**
- Create: `custom_components/loxone/engineering_topology.py`
- Create: `tests/engineering_fixtures.py`
- Modify: `custom_components/loxone/engineering_config.py:42-124`
- Test: `tests/test_engineering_topology.py`
- Test: `tests/test_engineering_config.py`

**Interfaces:**
- Consumes: `EngineeringElement` and `EngineeringInventory` from `engineering_config.py`.
- Produces: `EngineeringSourceContext`, `NodeKind`, `ResolutionStatus`, `ResolvedEngineeringNode`, `ResolvedEngineeringInventory`, `classify_node_kind(element)`, and `scoped_engineering_identifier(source, node)`.
- Produces: `EngineeringInventory.is_complete`, which is true only for a non-empty parsed tree containing at least one stable UUID and one `LoxLIVE` node.
- Extends `EngineeringElement` with opaque `key` and `parent_key`; `parent_uuid` remains a compatibility field only.
- Produces early validation that rejects duplicate stable UUIDs before topology resolution.

- [ ] **Step 1: Write failing model, classification, completeness, and allowlist tests**

Create the shared synthetic constructors in `tests/engineering_fixtures.py`; later tasks extend this file only with synthetic values:

```python
from datetime import UTC, datetime

from custom_components.loxone.engineering_config import (
    EngineeringConfigError,
    EngineeringElement,
    EngineeringInventory,
    parse_engineering_xml,
)
from custom_components.loxone.engineering_topology import EngineeringSourceContext, ResolvedEngineeringInventory


def element(
    uuid: str | None,
    element_type: str,
    *,
    parent_uuid: str | None = None,
    key: str | None = None,
    parent_key: str | None = None,
    title: str | None = None,
    io_name: str | None = None,
    room: str | None = "Office",
    platform: str | None = None,
) -> EngineeringElement:
    return EngineeringElement(
        key=key or uuid or "xml:fixture",
        xml_element="C",
        loxone_type=element_type,
        title=title or element_type,
        uuid=uuid,
        io_name=io_name,
        parent_uuid=parent_uuid,
        parent_key=parent_key or parent_uuid,
        room_uuid="room-uuid" if room else None,
        room=room,
        category_uuid=None,
        category=None,
        suggested_platform=platform,
        attributes={},
    )


def inventory_of(*elements: EngineeringElement) -> EngineeringInventory:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    return EngineeringInventory("sps_7_20260913120000.zip", 7, now, now, 10, elements)


def source(entry_id: str = "entry-a", serial: str = "serial-a") -> EngineeringSourceContext:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    return EngineeringSourceContext(
        entry_id=entry_id,
        serial_number=serial,
        title="Miniserver",
        model="Miniserver",
        source_archive="sps_7_20260913120000.zip",
        config_version=7,
        config_timestamp=now,
        loxapp_last_modified="revision-7",
    )


def node(resolved: ResolvedEngineeringInventory, uuid: str):
    return next(item for item in resolved.nodes if item.element.uuid == uuid)
```

Then add the behavior tests:

```python
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.loxone.engineering_config import (
    EngineeringConfigError,
    EngineeringElement,
    EngineeringInventory,
    parse_engineering_xml,
)
from custom_components.loxone.engineering_topology import (
    EngineeringSourceContext,
    NodeKind,
    classify_node_kind,
    scoped_engineering_identifier,
)
from tests.engineering_fixtures import (
    DUPLICATE_UUID_XML,
    SYNTHETIC_PARSE_CONTEXT,
    UUIDLESS_CONTAINER_XML,
    element,
    validate_fixture_input,
)


def test_fixture_input_gate_rejects_identity_and_location_fields():
    with pytest.raises(ValueError, match="forbidden fixture field"):
        validate_fixture_input(
            {"device": "ST-F01", "room": "Office", "CurrentUser": "person"}
        )

    assert validate_fixture_input({"device": "ST-F01", "room": "Office"}) == {
        "device": "ST-F01",
        "room": "Office",
    }


def test_type_driven_classification_does_not_trust_titles():
    link = element("link-uuid", "LoxLink", title="Editable caption")
    tree = element("tree-uuid", "LoxTree", title="Another caption")
    endpoint = element("endpoint-uuid", "TreeDevice", title="ST-F07")
    caption = element("caption-uuid", "TreeCaption", title="Branch A")

    assert classify_node_kind(link) is NodeKind.BUS
    assert classify_node_kind(tree) is NodeKind.BUS
    assert classify_node_kind(endpoint) is NodeKind.PHYSICAL_DEVICE
    assert classify_node_kind(caption) is NodeKind.STRUCTURAL


@pytest.mark.parametrize(
    "element_type",
    (
        "WeatherServer",
        "GlobalStates",
        "OperatingModes",
        "TimeFunctions",
        "SystemStatus",
        "DeviceMonitor",
        "NetworkPlugin",
        "VirtualInputs",
        "VirtualOutputs",
        "Tasks",
        "Messages",
        "Intercom",
        "LightingGroups",
    ),
)
def test_known_provider_containers_are_service_modules(element_type):
    assert classify_node_kind(element("service", element_type)) is NodeKind.SERVICE_MODULE


def test_scoped_identifier_uses_serial_and_uuid_only():
    source = EngineeringSourceContext(
        entry_id="entry-a",
        serial_number="serial-a",
        title="Miniserver",
        model="Miniserver",
        source_archive="sps_7_20260913120000.zip",
        config_version=7,
        config_timestamp=datetime(2026, 9, 13, 12, tzinfo=UTC),
        loxapp_last_modified="revision-7",
    )
    node = element("device-uuid", "TreeDevice", title="ST-F07")

    assert scoped_engineering_identifier(source, node) == "serial-a:device-uuid"


def test_public_element_dict_is_an_explicit_allowlist():
    item = element("device-uuid", "TreeDevice", title="ST-F07")
    item = replace(
        item,
        attributes={
            "U": "device-uuid",
            "Title": "ST-F07",
            "CurrentUser": "Synthetic User",
            "Latitude": "12.345",
            "RemoteUrl": "https://example.invalid/private",
            "AccessCode": "synthetic-secret",
        },
    )

    public = item.as_public_dict()

    assert public["uuid"] == "device-uuid"
    assert public["title"] == "ST-F07"
    assert not ({"attributes", "CurrentUser", "Latitude", "RemoteUrl", "AccessCode"} & public.keys())


def test_inventory_requires_uuid_and_miniserver_to_be_complete():
    now = datetime.now(UTC)
    complete = EngineeringInventory("test.zip", 1, now, now, 10, (element("ms-uuid", "LoxLIVE"),))
    empty = EngineeringInventory("test.zip", 1, now, now, 10, ())

    assert complete.is_complete is True
    assert empty.is_complete is False


def test_uuidless_parser_keys_are_opaque_and_preserve_parent_chain():
    inventory = parse_engineering_xml(UUIDLESS_CONTAINER_XML, **SYNTHETIC_PARSE_CONTEXT)
    container, child = inventory.elements[-2:]

    assert container.uuid is None
    assert container.key.startswith("xml:")
    assert "Private caption" not in container.key
    assert child.parent_key == container.key


def test_duplicate_stable_uuid_invalidates_inventory():
    with pytest.raises(EngineeringConfigError, match="duplicate_engineering_uuid"):
        parse_engineering_xml(DUPLICATE_UUID_XML, **SYNTHETIC_PARSE_CONTEXT)
```

- [ ] **Step 2: Run the focused tests and confirm the new imports/properties fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_topology.py tests\test_engineering_config.py -v`

Expected: collection fails because `engineering_topology` and `EngineeringInventory.is_complete` do not exist.

- [ ] **Step 3: Implement the immutable source and topology model**

Create these exact public types in `engineering_topology.py`:

```python
class NodeKind(StrEnum):
    MINISERVER = "miniserver"
    BUS = "bus"
    BRIDGE = "bridge"
    PHYSICAL_DEVICE = "physical_device"
    SERVICE_MODULE = "service_module"
    CHANNEL = "channel"
    STRUCTURAL = "structural"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class EngineeringSourceContext:
    entry_id: str
    serial_number: str | None
    title: str | None
    model: str | None
    source_archive: str
    config_version: int
    config_timestamp: datetime
    loxapp_last_modified: str | None

    @property
    def provider_identifier(self) -> str:
        return self.serial_number or self.entry_id


@dataclass(frozen=True, slots=True)
class ResolvedEngineeringNode:
    element: EngineeringElement
    kind: NodeKind
    owner_key: str | None
    device_identifier: str | None
    via_device_identifier: str | None
    bus_kind: str | None
    topology_path: tuple[str, ...]
    resolution_status: ResolutionStatus
    resolution_reason: str
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedEngineeringInventory:
    source: EngineeringSourceContext
    nodes: tuple[ResolvedEngineeringNode, ...]

    @property
    def nodes_by_key(self) -> dict[str, ResolvedEngineeringNode]:
        return {node.element.key: node for node in self.nodes}
```

Use case-folded exact type sets for `LoxLIVE`, `LoxLink`, `LoxTree`, Air/1-Wire extensions, typed physical endpoints, provider service containers, channels, and structural nodes. Permit conservative suffix checks only for `*Device`, `*Dev`, and `*Extension`, after excluding service and structural types. `scoped_engineering_identifier()` must return `f"{source.provider_identifier}:{element.uuid}"` for UUID-backed graph devices and return `None` for non-device nodes without a UUID. Parse elements in document order, assign UUID-less nodes an opaque `xml:{index:06d}` key, and retain the immediate opaque `parent_key` even when `parent_uuid` is absent. Reject duplicate non-empty UUIDs before returning the immutable inventory. Add the `is_complete` property without changing parser limits or XML declaration rejection. Task 1 tests parser key/parent retention and type classification; Task 2 adds the parser-through-resolver ownership and sensitivity propagation assertions. Define the synthetic XML constants and parse context in `tests/engineering_fixtures.py` before using them, so red tests fail on absent production behavior rather than fixture errors. Add a test-fixture input gate in that helper that recursively rejects forbidden key names and URL/address/coordinate/credential/access values before a helper can construct or serialize a fixture, while permitting synthetic numbered device labels and ordinary room names. Hand-authored negative security payloads remain test inputs and are not rejected before reaching the production parser being tested.

- [ ] **Step 4: Run the focused tests and confirm they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_topology.py tests\test_engineering_config.py -v`

Expected: all focused tests pass.

- [ ] **Step 5: Commit the topology model**

```powershell
git add custom_components/loxone/engineering_topology.py custom_components/loxone/engineering_config.py tests/engineering_fixtures.py tests/test_engineering_topology.py tests/test_engineering_config.py
git commit -m "feat: model engineering topology"
```

### Task 2: Resolve physical owners, buses, bridges, and provider services

**Files:**
- Modify: `custom_components/loxone/engineering_topology.py`
- Modify: `tests/engineering_fixtures.py`
- Test: `tests/test_engineering_topology.py`

**Interfaces:**
- Consumes: Task 1 topology types and `EngineeringInventory.elements`.
- Produces: `OwnerResolver(max_depth: int = 128)` and `resolve_engineering_topology(inventory, source) -> ResolvedEngineeringInventory`.
- Guarantees: every parsed node gets one result; physical and service ownership is opaque-key/type driven; cycles and missing ancestors produce `UNRESOLVED` rows rather than guesses; sensitivity is inherited before projection.

- [ ] **Step 1: Add failing tests for Tree, Link/Air, Link/1-Wire, internal I/O, services, cycles, and multi-entry isolation**

Extend `tests/engineering_fixtures.py` with these complete inventory factories:

```python
def reference_link_inventory() -> EngineeringInventory:
    return inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("link", "LoxLink", parent_uuid="ms", title="Link", room=None),
        element("air-extension", "AirBaseExtension", parent_uuid="link", title="Wireless bridge A"),
        element("air-device", "AirDevice", parent_uuid="air-extension", title="ST-F01"),
        element("wire-extension", "Lox1WireExtension", parent_uuid="link", title="Wired-bus extension A"),
        element("wire-sensor", "Lox1wireDevice", parent_uuid="wire-extension", title="Wired sensor A"),
    )


def provider_inventory() -> EngineeringInventory:
    return inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("io", "IoData", parent_uuid="ms", room=None),
        element("digital-i1", "DigitalIn", parent_uuid="io", io_name="I1"),
        element("analog-ai1", "VoltageIn", parent_uuid="io", io_name="AI1"),
        element("relay-q1", "Actor", parent_uuid="io", io_name="Q1"),
        element("weather-server", "WeatherServer", room=None),
        element("weather-value", "WeatherData", parent_uuid="weather-server", io_name="WDC1"),
        element("global-states", "GlobalStates", room=None),
        element("system-variable", "SysVar", parent_uuid="global-states", io_name="SYS1"),
    )


def cyclic_inventory() -> EngineeringInventory:
    return inventory_of(
        element("ms", "LoxLIVE", title="Miniserver", room=None),
        element("cycle-a", "TreeDevice", parent_uuid="cycle-b"),
        element("cycle-b", "TreeCaption", parent_uuid="cycle-a"),
    )
```

Add these imports and tests to `tests/test_engineering_topology.py`:

```python
from tests.engineering_fixtures import (
    cyclic_inventory,
    element,
    inventory_of,
    node,
    provider_inventory,
    reference_link_inventory,
    source,
)


def test_tree_caption_stays_in_path_but_is_not_a_device():
    inventory = inventory_of(
        element("ms", "LoxLIVE"),
        element("tree", "LoxTree", parent_uuid="ms"),
        element("branch", "TreeCaption", parent_uuid="tree", title="Branch A"),
        element("nfc", "TreeDevice", parent_uuid="branch", title="NFC Code Touch"),
    )
    resolved = resolve_engineering_topology(inventory, source("entry-a", "serial-a"))
    nfc = node(resolved, "nfc")

    assert nfc.device_identifier == "serial-a:nfc"
    assert nfc.via_device_identifier == "serial-a:tree"
    assert nfc.bus_kind == "tree"
    assert nfc.topology_path == ("Miniserver", "Tree", "Branch A", "NFC Code Touch")
    assert node(resolved, "branch").kind is NodeKind.STRUCTURAL


def test_air_and_onewire_endpoints_use_the_nearest_extension():
    resolved = resolve_engineering_topology(reference_link_inventory(), source("entry-a", "serial-a"))

    assert node(resolved, "air-device").via_device_identifier == "serial-a:air-extension"
    assert node(resolved, "air-extension").via_device_identifier == "serial-a:link"
    assert node(resolved, "wire-sensor").via_device_identifier == "serial-a:wire-extension"
    assert node(resolved, "wire-extension").via_device_identifier == "serial-a:link"


def test_internal_io_and_document_services_belong_to_source_miniserver():
    resolved = resolve_engineering_topology(provider_inventory(), source("entry-a", "serial-a"))

    assert node(resolved, "digital-i1").device_identifier == "serial-a"
    assert node(resolved, "analog-ai1").device_identifier == "serial-a"
    assert node(resolved, "relay-q1").device_identifier == "serial-a"
    assert node(resolved, "weather-server").via_device_identifier == "serial-a"
    assert node(resolved, "weather-value").device_identifier == "serial-a:weather-server"
    assert node(resolved, "global-states").via_device_identifier == "serial-a"
    assert node(resolved, "system-variable").device_identifier == "serial-a:global-states"


def test_cycle_is_bounded_and_reported_without_owner_guess():
    resolved = resolve_engineering_topology(cyclic_inventory(), source("entry-a", "serial-a"))
    item = node(resolved, "cycle-a")

    assert item.resolution_status is ResolutionStatus.UNRESOLVED
    assert item.resolution_reason == "parent_cycle"


def test_same_uuid_on_two_entries_produces_distinct_registry_identifiers():
    inventory = inventory_of(element("shared", "TreeDevice"))
    first = resolve_engineering_topology(inventory, source("entry-a", "serial-a"))
    second = resolve_engineering_topology(inventory, source("entry-b", "serial-b"))

    assert node(first, "shared").device_identifier == "serial-a:shared"
    assert node(second, "shared").device_identifier == "serial-b:shared"


def test_singleton_uuidless_service_gets_stable_provider_identifier():
    inventory = inventory_of(
        element("ms", "LoxLIVE", room=None),
        element(None, "WeatherServer", key="xml:000002", room=None),
    )
    resolved = resolve_engineering_topology(inventory, source())

    assert resolved.nodes[1].device_identifier == "serial-a:service:weatherserver"


def test_duplicate_uuidless_service_types_are_inventory_only():
    inventory = inventory_of(
        element("ms", "LoxLIVE", room=None),
        element(None, "WeatherServer", key="xml:000002", room=None),
        element(None, "WeatherServer", key="xml:000003", room=None),
    )
    resolved = resolve_engineering_topology(inventory, source())

    assert {item.resolution_reason for item in resolved.nodes[1:]} == {
        "ambiguous_uuidless_service"
    }
```

- [ ] **Step 2: Run the resolver tests and confirm they fail on the missing resolver**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_topology.py -v`

Expected: resolver tests fail because `resolve_engineering_topology` is not defined.

- [ ] **Step 3: Implement bounded two-pass owner resolution**

Implement `OwnerResolver.resolve()` as two passes:

```python
class OwnerResolver:
    def __init__(self, max_depth: int = 128) -> None:
        self._max_depth = max_depth

    def resolve(
        self,
        inventory: EngineeringInventory,
        source: EngineeringSourceContext,
    ) -> ResolvedEngineeringInventory:
        elements_by_key = {item.key: item for item in inventory.elements}
        kinds = {item.key: classify_node_kind(item) for item in inventory.elements}
        nodes = tuple(
            self._resolve_one(item, elements_by_key, kinds, source)
            for item in inventory.elements
        )
        return ResolvedEngineeringInventory(source=source, nodes=nodes)


def resolve_engineering_topology(
    inventory: EngineeringInventory,
    source: EngineeringSourceContext,
) -> ResolvedEngineeringInventory:
    return OwnerResolver().resolve(inventory, source)
```

For each node, walk no more than 128 `parent_key` ancestors and retain only non-sensitive structural titles in `topology_path`. A physical endpoint selects the nearest registrable bridge/bus as `via_device_identifier`; skip an UUID-less/unregistrable recognized ancestor and continue to the nearest valid upstream identifier rather than silently truncating the path. A channel selects the nearest physical endpoint, physical bridge/extension, or service module as `device_identifier`; only a genuinely internal channel beneath `LoxLIVE` selects the Miniserver identifier. A document-level service module uses the source Miniserver as its provider even when `LoxLIVE` is a sibling. WeatherData and SysVar nodes resolve to their typed WeatherServer/GlobalStates module. A singleton UUID-less provider service uses the typed fallback from ruling 3 only when its normalized type occurs exactly once across UUID-backed and UUID-less modules; conflicts and dependent channels remain unresolved with `ambiguous_uuidless_service`. Missing parents use `missing_parent`; cycles use `parent_cycle`; exceeded depth uses `parent_depth_exceeded`. A physical node with an unknown parent must not receive a guessed `via_device_identifier`. Mark `sensitive=True` when the node or any ancestor matches the centralized exact sensitive technical `Type`/XML-tag policy and clear its public name, room, I/O, category, raw attributes, and path before it leaves the resolver boundary. Physical NFC reader hardware is not sensitive, but its tag/code/user/permission/credential children are. Because missing, cyclic, or depth-truncated ancestry cannot prove the absence of a hidden sensitive ancestor, such unresolved rows fail closed with the same sanitization while retaining their explicit failure reason.

- [ ] **Step 4: Run the topology tests and the existing engineering tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_topology.py tests\test_engineering_config.py tests\test_engineering_entities.py -v`

Expected: all selected tests pass and existing conservative onboarding remains unchanged.

- [ ] **Step 5: Commit the owner resolver**

```powershell
git add custom_components/loxone/engineering_topology.py tests/engineering_fixtures.py tests/test_engineering_topology.py
git commit -m "feat: resolve engineering device owners"
```

### Task 3: Classify read-only capabilities and safe exposure

**Files:**
- Create: `custom_components/loxone/engineering_capabilities.py`
- Modify: `custom_components/loxone/engineering_entities.py:27-108`
- Modify: `custom_components/loxone/engineering_runtime.py:78-120`
- Modify: `tests/engineering_fixtures.py`
- Test: `tests/test_engineering_capabilities.py`
- Test: `tests/test_engineering_entities.py`

**Interfaces:**
- Consumes: `ResolvedEngineeringInventory` and `EngineeringRuntimeInventory`.
- Produces: `CapabilityState`, `ExposureStatus`, `EngineeringCapability`, `SafeRuntimeBindingDescriptor`, `EngineeringInventoryRow`, `select_runtime_probe_candidates(resolved)`, `resolve_engineering_capabilities(resolved, runtime)`, `EngineeringEntitySpec`, and `build_engineering_entity_specs(rows, runtime | None)`.
- Guarantees: readable does not imply writable; sensitive and arbitrary text channels never become entity specs; outputs remain inventory-only.

- [ ] **Step 1: Write failing capability and entity-policy tests**

Add `numeric_binding()`, `text_binding()`, and `resolved_node()` from the following block to `tests/engineering_fixtures.py`, then import them into `tests/test_engineering_capabilities.py` with `provider_inventory` and `source`:

```python
from custom_components.loxone.engineering_runtime import (
    EngineeringRuntimeBinding,
    EngineeringRuntimeInventory,
)
from custom_components.loxone.engineering_topology import (
    NodeKind,
    ResolvedEngineeringNode,
    ResolutionStatus,
    resolve_engineering_topology,
)
from tests.engineering_fixtures import element


def numeric_binding(uuid: str, value: float, element_type: str = "VoltageIn") -> EngineeringRuntimeBinding:
    return EngineeringRuntimeBinding(
        engineering_uuid=uuid,
        io_name="AI1",
        loxone_type=element_type,
        title=element_type,
        room="Office",
        suggested_platform=None,
        status="bound",
        binding_method="uuid_state",
        value_kind="number",
        numeric_value=value,
        state_uuid=f"{uuid}-state",
    )


def text_binding(uuid: str = "text", element_type: str = "SysVar") -> EngineeringRuntimeBinding:
    return EngineeringRuntimeBinding(
        engineering_uuid=uuid,
        io_name="SYS1",
        loxone_type=element_type,
        title=element_type,
        room="Office",
        suggested_platform=None,
        status="bound",
        binding_method="uuid_state",
        value_kind="text",
    )


def resolved_node(uuid: str, element_type: str, *, io_name: str = "AI1") -> ResolvedEngineeringNode:
    return ResolvedEngineeringNode(
        element=element(uuid, element_type, io_name=io_name),
        kind=NodeKind.CHANNEL,
        owner_key="ms",
        device_identifier="serial-a",
        via_device_identifier=None,
        bus_kind=None,
        topology_path=("Miniserver", element_type),
        resolution_status=ResolutionStatus.RESOLVED,
        resolution_reason="internal_miniserver_channel",
    )
```

The test module itself imports the shared helpers and contains these assertions:

```python
from tests.engineering_fixtures import (
    numeric_binding,
    provider_inventory,
    resolved_node,
    source,
    text_binding,
)


def test_successful_read_never_implies_write_capability():
    capability = resolve_capability(
        resolved_node("analog-input", "VoltageIn"),
        numeric_binding("analog-input", 2.4),
    )

    assert capability.state is CapabilityState.READABLE
    assert capability.platform == "sensor"
    assert capability.exposure is ExposureStatus.PREPARED_DISABLED


def test_binary_input_requires_zero_or_one_semantics():
    binary = resolve_capability(resolved_node("i1", "DigitalIn"), numeric_binding("i1", 1.0, "DigitalIn"))
    non_binary = resolve_capability(resolved_node("i2", "DigitalIn"), numeric_binding("i2", 2.0, "DigitalIn"))

    assert binary.platform == "binary_sensor"
    assert non_binary.platform is None
    assert non_binary.state is CapabilityState.UNSUPPORTED


def test_outputs_are_inventory_only_even_when_readable():
    output = resolve_capability(
        resolved_node("q1", "Actor", io_name="Q1"),
        numeric_binding("q1", 1.0, "Actor"),
    )

    assert output.state is CapabilityState.READABLE
    assert output.platform is None
    assert output.exposure is ExposureStatus.INVENTORY_ONLY
    assert output.reason == "output_write_contract_not_enabled"


def test_sensitive_access_children_and_text_payloads_are_suppressed():
    access = resolve_capability(
        resolved_node("code", "NfcCode"),
        numeric_binding("code", 1234.0, "NfcCode"),
    )
    text = resolve_capability(resolved_node("text", "SysVar"), text_binding())

    assert access.state is CapabilityState.SENSITIVE
    assert access.exposure is ExposureStatus.SUPPRESSED
    assert text.exposure is ExposureStatus.SUPPRESSED


def test_weather_and_system_variables_share_their_module_owner():
    runtime = EngineeringRuntimeInventory(
        bindings=(
            numeric_binding("weather-value", 18.5, "WeatherData"),
            numeric_binding("system-variable", 1.0, "SysVar"),
        )
    )
    resolved = resolve_engineering_topology(provider_inventory(), source())
    rows = resolve_engineering_capabilities(resolved, runtime)
    specs = build_engineering_entity_specs(rows, runtime)

    weather = next(item for item in specs if item.unique_id == "weather-value")
    system = next(item for item in specs if item.unique_id == "system-variable")
    assert weather.owner_identifier == "serial-a:weather-server"
    assert system.owner_identifier == "serial-a:global-states"
    assert weather.enabled_by_default is False
    assert system.enabled_by_default is False


def test_probe_candidates_do_not_depend_on_legacy_platform_hint():
    candidates = select_runtime_probe_candidates(
        ResolvedEngineeringInventory(
            source=source(),
            nodes=(
                resolved_node("i1", "DigitalIn"),
                resolved_node("ai1", "VoltageIn"),
                resolved_node("online", "Online"),
            ),
        )
    )

    assert {item.element.uuid for item in candidates} == {"i1", "ai1", "online"}


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_non_finite_values_are_not_exposed(value):
    capability = resolve_capability(
        resolved_node("ai1", "VoltageIn"), numeric_binding("ai1", value)
    )
    assert capability.exposure is ExposureStatus.SUPPRESSED


def test_numeric_prefix_with_unknown_suffix_is_text_not_a_unit():
    binding = binding_from_response("ai1", "12.4 private-label")
    assert binding.value_kind == "text"
    assert binding.numeric_value is None


def test_explicit_runtime_state_uuid_may_differ_from_engineering_uuid():
    binding = numeric_binding("engineering-ai1", 2.4)
    binding = replace(binding, state_uuid="event-state-ai1")
    row = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(
            source=source(),
            nodes=(resolved_node("engineering-ai1", "VoltageIn"),),
        ),
        EngineeringRuntimeInventory((binding,)),
    )[0]
    spec = build_engineering_entity_specs((row,), EngineeringRuntimeInventory((binding,)))[0]
    assert spec.unique_id == "engineering-ai1"
    assert spec.state_uuid == "event-state-ai1"


def test_scalar_response_without_explicit_state_mapping_is_rebind_only():
    binding = replace(numeric_binding("ai1", 2.4), state_uuid=None)
    row = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(
            source=source(), nodes=(resolved_node("ai1", "VoltageIn"),)
        ),
        EngineeringRuntimeInventory((binding,)),
    )[0]
    assert row.binding.event_binding_proven is False
    assert row.capability.exposure is ExposureStatus.INVENTORY_ONLY
    assert row.capability.reason == "readable_rebind_only"


def test_status_boolean_and_unknown_channel_are_explicitly_classified():
    runtime = EngineeringRuntimeInventory(
        (
            replace(
                numeric_binding("online", 1.0, "Online"),
                state_uuid="online-state",
            ),
        )
    )
    rows = resolve_engineering_capabilities(
        ResolvedEngineeringInventory(
            source=source(),
            nodes=(
                resolved_node("online", "Online"),
                resolved_node("unknown", "FutureChannel"),
            ),
        ),
        runtime,
    )
    online, unknown = rows
    assert online.semantic_platform == "binary_sensor"
    assert online.capability.exposure is ExposureStatus.PREPARED_DISABLED
    assert unknown.capability.state in {
        CapabilityState.CONFIGURED_ONLY,
        CapabilityState.UNSUPPORTED,
    }
    assert unknown.capability.exposure is ExposureStatus.INVENTORY_ONLY
```

- [ ] **Step 2: Run the focused tests and confirm the capability API is missing**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_capabilities.py tests\test_engineering_entities.py -v`

Expected: collection or assertions fail because capability types and generalized entity specs are absent.

- [ ] **Step 3: Implement explicit capability and exposure enums**

Create these exact contracts:

```python
class CapabilityState(StrEnum):
    READABLE = "readable"
    WRITABLE = "writable"
    READ_WRITE = "read_write"
    CONFIGURED_ONLY = "configured_only"
    UNSUPPORTED = "unsupported"
    SENSITIVE = "sensitive"


class ExposureStatus(StrEnum):
    PREPARED_DISABLED = "prepared_disabled"
    INVENTORY_ONLY = "inventory_only"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class EngineeringCapability:
    state: CapabilityState
    platform: Literal["sensor", "binary_sensor"] | None
    exposure: ExposureStatus
    reason: str


@dataclass(frozen=True, slots=True)
class EngineeringInventoryRow:
    node: ResolvedEngineeringNode
    capability: EngineeringCapability
    semantic_platform: Literal["sensor", "binary_sensor"] | None
    binding: SafeRuntimeBindingDescriptor | None


@dataclass(frozen=True, slots=True)
class SafeRuntimeBindingDescriptor:
    binding_method: str
    value_kind: Literal["number", "boolean"]
    safe_unit: str | None
    state_uuid: str | None
    event_binding_proven: bool
```

Use exact type sets for WeatherData, SysVar, DigitalIn, VoltageIn, Online/status, Actor/relay outputs, and analog outputs. Treat a bound finite numeric value as readable, a missing binding as configured-only, a non-numeric arbitrary text response as suppressed, and an unknown typed channel as unsupported. `select_runtime_probe_candidates()` must use these same exact type sets, ignore the legacy `suggested_platform` hint, and omit sensitive descendants before an endpoint is constructed. Define `SENSITIVE_TYPES` for access-code, NFC-tag, credential, user, and permission child records without marking the physical `TreeDevice` container sensitive. Do not define a writable whitelist in this change. Keep authentication and transport failures distinct from an ordinary unbound value so they cannot downgrade the stored last-good capability. Extend runtime bindings with `state_uuid: str | None`; set it only from an explicit matching `uN`/child UUID in `/all` or a proven LoxAPP3 mapping. A scalar `/state` result alone creates a `SafeRuntimeBindingDescriptor(event_binding_proven=False)` and remains inventory-only with `readable_rebind_only`; never copy `engineering_uuid` into `state_uuid` by assumption.

Replace permissive unit parsing with an explicit map of safe engineering units
to Home Assistant units. Accept a bare finite number or a finite number followed
by an allowlisted unit only. Reject `nan`, infinities, arbitrary suffixes,
control characters, and numeric-prefix strings with unknown text. Add adversarial
tests for all of these cases.

- [ ] **Step 4: Replace sensor-only specs with a platform-neutral entity spec**

In `engineering_entities.py`, define:

```python
@dataclass(frozen=True, slots=True)
class EngineeringEntitySpec:
    unique_id: str
    state_uuid: str
    platform: Literal["sensor", "binary_sensor"]
    name: str
    native_value: float | bool | None
    unit: str | None
    available: bool
    owner_identifier: str
    owner_name: str
    owner_model: str
    room: str | None
    loxone_type: str | None
    io_name: str | None
    config_version: int
    runtime_binding: str | None
    enabled_by_default: bool = False
```

`build_engineering_entity_specs()` must emit only `PREPARED_DISABLED` rows with stable engineering UUIDs and a proven non-empty event `state_uuid`. When runtime is `None` during cache restore, emit the remembered safe spec with `available=False`, `native_value=None`, and no runtime endpoint. A rebind-only scalar row remains visible in inventory but does not become a prepared entity. `owner_name` and `owner_model` are presentation data for registry planning only; the platform entity will later put only `owner_identifier` in `DeviceInfo`. Keep a compatibility wrapper for the previous sensor-only builder until all callers move to this model. Keep `normalize_engineering_unit()` as the explicit safe-unit map and delete `_nearest_device()` only after its callers move to the resolver.

- [ ] **Step 5: Run capability, runtime, and entity tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_capabilities.py tests\test_engineering_runtime.py tests\test_engineering_entities.py -v`

Expected: all selected tests pass; no test invokes a Miniserver write endpoint.

- [ ] **Step 6: Commit capability resolution**

```powershell
git add custom_components/loxone/engineering_capabilities.py custom_components/loxone/engineering_entities.py custom_components/loxone/engineering_runtime.py tests/engineering_fixtures.py tests/test_engineering_capabilities.py tests/test_engineering_entities.py
git commit -m "feat: classify engineering capabilities"
```

### Task 4: Persist and diff the last known-good sanitized snapshot

**Files:**
- Create: `custom_components/loxone/engineering_snapshot.py`
- Create: `custom_components/loxone/engineering_changes.py`
- Modify: `tests/engineering_fixtures.py`
- Test: `tests/test_engineering_snapshot.py`
- Test: `tests/test_engineering_changes.py`

**Interfaces:**
- Consumes: `ResolvedEngineeringInventory` and `EngineeringInventoryRow`.
- Produces: `EngineeringSnapshot`, `StoredEngineeringState`, `EngineeringNodeChange`, `EngineeringChangeSet`, `EngineeringEntityImpact`, `EngineeringImpactPlan`, `async_load_engineering_state(hass, entry_id)`, `async_store_engineering_state(hass, state)`, `snapshot_to_dict(snapshot)`, `snapshot_from_dict(data)`, and `diff_engineering_snapshots(previous, current)`.
- Storage key: `loxone.engineering_snapshot.<entry_id>`, version `2`, `private=True`; the loader migrates the previous private snapshot shape without publishing it.

- [ ] **Step 1: Write failing round-trip, rejection, and UUID-diff tests**

Add this constructor to `tests/engineering_fixtures.py` after Task 4 production imports are available:

```python
from dataclasses import replace
from datetime import UTC, datetime

from custom_components.loxone.engineering_capabilities import resolve_engineering_capabilities
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from custom_components.loxone.engineering_snapshot import EngineeringSnapshot
from custom_components.loxone.engineering_topology import resolve_engineering_topology


def make_snapshot(
    *,
    last_modified: str = "revision-7",
    inventory=None,
    runtime: EngineeringRuntimeInventory | None = None,
) -> EngineeringSnapshot:
    raw = inventory or provider_inventory()
    context = replace(source(), loxapp_last_modified=last_modified)
    resolved = resolve_engineering_topology(raw, context)
    default_runtime = EngineeringRuntimeInventory(
        bindings=(
            numeric_binding("weather-value", 18.5, "WeatherData"),
            numeric_binding("system-variable", 1.0, "SysVar"),
        )
    )
    rows = resolve_engineering_capabilities(resolved, runtime or default_runtime)
    return EngineeringSnapshot(
        source=context,
        nodes=resolved.nodes,
        rows=rows,
        configuration_revision_id=engineering_configuration_revision_id(context),
        safe_content_digest=engineering_safe_content_digest(context, resolved.nodes, rows),
        read_sequence=1,
        generation_id=engineering_generation_id(
            context, resolved.nodes, rows, read_sequence=1
        ),
        captured_at=datetime(2026, 9, 13, 12, tzinfo=UTC),
    )
```

Then add these tests to the snapshot/change test modules:

```python
import json

import pytest

from tests.engineering_fixtures import (
    cyclic_inventory,
    element,
    inventory_of,
    make_snapshot,
)


def test_snapshot_round_trip_contains_only_public_allowlisted_fields():
    original = make_snapshot()
    encoded = snapshot_to_dict(original)
    restored = snapshot_from_dict(encoded)

    assert snapshot_to_dict(restored) == encoded
    assert restored.source.title is None
    assert restored.generation_id == original.generation_id
    assert any(
        row.binding and row.binding.state_uuid
        for row in restored.rows
        if row.semantic_platform
    )
    rendered = json.dumps(encoded)
    for forbidden_key in (
        "attributes",
        "CurrentUser",
        "Latitude",
        "Longitude",
        "LocalUrl",
        "RemoteUrl",
        "HostAddress",
        "AccessCode",
    ):
        assert forbidden_key not in rendered


@pytest.mark.parametrize(
    "candidate",
    [
        make_snapshot(inventory=inventory_of()),
        make_snapshot(inventory=inventory_of(element("device", "TreeDevice"))),
        make_snapshot(inventory=cyclic_inventory()),
    ],
)
def test_invalid_candidate_snapshot_is_rejected_before_storage(candidate):
    with pytest.raises(EngineeringSnapshotError):
        validate_engineering_snapshot(candidate)


def test_uuid_diff_separates_add_remove_metadata_move_and_platform_change():
    previous = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("removed-channel", "VoltageIn", parent_uuid="ms", io_name="AI1"),
            element("renamed-device", "TreeDevice", parent_uuid="ms", title="ST-F01"),
            element("moved-device", "TreeDevice", parent_uuid="ms", title="ST-F02", room="Office"),
            element("old-parent", "LoxTree", parent_uuid="ms", title="Tree A"),
            element("new-parent", "LoxTree", parent_uuid="ms", title="Tree B"),
            element("reparented-device", "TreeDevice", parent_uuid="old-parent", title="ST-F03"),
            element("changed-channel", "VoltageIn", parent_uuid="ms", io_name="AI2"),
        )
    )
    current = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("new-device", "TreeDevice", parent_uuid="ms", title="ST-F04"),
            element("renamed-device", "TreeDevice", parent_uuid="ms", title="ST-F01 renamed"),
            element("moved-device", "TreeDevice", parent_uuid="ms", title="ST-F02", room="Workshop"),
            element("old-parent", "LoxTree", parent_uuid="ms", title="Tree A"),
            element("new-parent", "LoxTree", parent_uuid="ms", title="Tree B"),
            element("reparented-device", "TreeDevice", parent_uuid="new-parent", title="ST-F03"),
            element("changed-channel", "DigitalIn", parent_uuid="ms", io_name="I1"),
        )
    )
    changes = diff_engineering_snapshots(previous, current)

    assert tuple(item.unique_id for item in changes.added) == ("new-device",)
    assert tuple(item.unique_id for item in changes.removed) == ("removed-channel",)
    changed = {item.unique_id: item for item in changes.metadata_changed}
    assert set(changed) == {
        "renamed-device",
        "moved-device",
        "reparented-device",
        "changed-channel",
    }
    assert changed["changed-channel"].old_semantic_platform == "sensor"
    assert changed["changed-channel"].new_semantic_platform == "binary_sensor"
```

- [ ] **Step 2: Run the focused tests and confirm snapshot APIs are missing**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_snapshot.py tests\test_engineering_changes.py -v`

Expected: collection fails because the snapshot and change modules do not exist.

- [ ] **Step 3: Implement strict JSON serialization and validation**

Define:

```python
@dataclass(frozen=True, slots=True)
class EngineeringSnapshot:
    source: EngineeringSourceContext
    nodes: tuple[ResolvedEngineeringNode, ...]
    rows: tuple[EngineeringInventoryRow, ...]
    configuration_revision_id: str
    safe_content_digest: str
    read_sequence: int
    generation_id: str
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class StoredEngineeringState:
    snapshot: EngineeringSnapshot | None
    registry_applied_generation: str | None = None
    pending_impact_plan: EngineeringImpactPlan | None = None
    impact_published_generation: str | None = None
    managed_area_ids: Mapping[str, str] = field(default_factory=dict)


class EngineeringSnapshotError(ValueError):
    """Raised when a candidate or stored snapshot is unsafe or incomplete."""
```

Compute `configuration_revision_id` from provider identity plus normalized scalar `lastModified`, falling back to archive configuration version and configuration timestamp when the scalar is missing; capture/download time must not affect it. Compute `safe_content_digest` from canonical serialization of the sanitized topology/capability/binding fields. Allocate `read_sequence = previous.read_sequence + 1` only when a new complete download is committed. `generation_id` combines provider scope, configuration revision, content digest, and read sequence; replay retains it while a later forced complete read receives a new sequence even if revision/content are unchanged. Serialize only the safe reconstruction fields named in binding ruling 6: opaque node key and parent key, technical type, UUID, safe name and room, node kind, owner/via identifiers, bus kind, sanitized topology path, resolution/capability/exposure reasons, semantic platform, safe unit, `io_name`, optional proven `state_uuid`, event-binding proof flag, and binding method. Persist the committed snapshot, registry-applied token, sanitized pending impact plan, impact-published token, and integration-managed area IDs in one versioned private envelope. Do not serialize `parent_uuid` when `parent_key` is sufficient, raw XML attributes, category values, runtime values, endpoint URLs, arbitrary error strings, credentials, host data, or Miniserver/provider/project/user/location titles. `snapshot_from_dict()` must validate enum values, list/string shapes, source entry ID, serial scope, duplicate stable UUIDs, opaque-key uniqueness, parent cycles, allowed units, finite-safe metadata, monotonic sequence shape, digest/token consistency, and completeness before returning an immutable snapshot.

- [ ] **Step 4: Implement source-scoped UUID diffing**

Define:

```python
@dataclass(frozen=True, slots=True)
class EngineeringNodeChange:
    unique_id: str
    old_name: str | None = None
    new_name: str | None = None
    old_room: str | None = None
    new_room: str | None = None
    old_owner_identifier: str | None = None
    new_owner_identifier: str | None = None
    old_semantic_platform: str | None = None
    new_semantic_platform: str | None = None


@dataclass(frozen=True, slots=True)
class EngineeringChangeSet:
    added: tuple[EngineeringNodeChange, ...] = ()
    removed: tuple[EngineeringNodeChange, ...] = ()
    metadata_changed: tuple[EngineeringNodeChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(astuple(self))


@dataclass(frozen=True, slots=True)
class EngineeringEntityImpact:
    unique_id: str
    entity_ids: tuple[str, ...]
    change_kind: Literal["removed", "platform_changed"]
    references: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class EngineeringImpactPlan:
    generation_id: str
    impacts: tuple[EngineeringEntityImpact, ...]
```

Compare only nodes with a stable engineering UUID and include the source provider in each lookup key. A name, room, owner/via identifier, or semantic-platform change must never be represented as remove-plus-add. Each change contains enough previous and candidate metadata for Task 8 to discover impacts before any Home Assistant registry mutation. `semantic_platform` records the resolver's meaning independently from whether an entity was suppressed by a cross-entry collision, so collision/load order cannot manufacture a false platform change.

- [ ] **Step 5: Run the snapshot and change tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_snapshot.py tests\test_engineering_changes.py -v`

Expected: all selected tests pass.

- [ ] **Step 6: Commit persistence and diffing**

```powershell
git add custom_components/loxone/engineering_snapshot.py custom_components/loxone/engineering_changes.py tests/engineering_fixtures.py tests/test_engineering_snapshot.py tests/test_engineering_changes.py
git commit -m "feat: persist engineering snapshots"
```

### Task 5: Synchronize the Home Assistant device topology

**Files:**
- Create: `custom_components/loxone/engineering_registry.py`
- Modify: `custom_components/loxone/engineering_entities.py:110-210`
- Modify: `custom_components/loxone/registry_maintenance.py:278-412`
- Test: `tests/test_engineering_registry.py`
- Test: `tests/test_registry_maintenance.py`

**Interfaces:**
- Consumes: `EngineeringSnapshot`, Home Assistant registries, and `StoredEngineeringState` integration-managed area metadata.
- Produces: `EngineeringRegistryPlan`, `EngineeringRegistryMetadata`, `EngineeringRegistrySyncResult`, `async_plan_engineering_registry_sync(...)`, `async_apply_engineering_registry_plan(...)`, `async_filter_entity_identity_conflicts(...)`, and a compatibility `async_sync_engineering_devices(...)` wrapper that plans then applies.
- Guarantees: planning is mutation-free; application is idempotent; physical devices exist without entities; `via_device_id` uses the nearest registered owner; user-overridden areas remain untouched; ambiguous legacy devices are not merged; global entity identities owned by another entry are never stolen.

- [ ] **Step 1: Write failing registry topology and migration tests**

```python
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from tests.engineering_fixtures import (
    element,
    inventory_of,
    make_snapshot,
    numeric_binding,
    reference_link_inventory,
)


async def test_registry_creates_entityless_tree_device_with_full_via_chain(registries):
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("tree", "LoxTree", parent_uuid="ms", title="Tree", room=None),
            element("branch", "TreeCaption", parent_uuid="tree", title="Branch A"),
            element("nfc", "TreeDevice", parent_uuid="branch", title="NFC Code Touch"),
        )
    )
    result = await async_sync_engineering_devices(
        registries.hass,
        "entry-a",
        snapshot,
        EngineeringRegistryMetadata.empty(),
    )

    tree = registries.device("serial-a:tree")
    endpoint = registries.device("serial-a:nfc")
    assert tree.via_device_id == registries.miniserver.id
    assert endpoint.via_device_id == tree.id
    assert result.created_identifiers == ("serial-a:tree", "serial-a:nfc")


async def test_registry_builds_link_bridge_endpoint_chains(registries):
    await async_sync_engineering_devices(
        registries.hass,
        "entry-a",
        make_snapshot(inventory=reference_link_inventory()),
        EngineeringRegistryMetadata.empty(),
    )

    assert registries.device("serial-a:air-extension").via_device_id == registries.device("serial-a:link").id
    assert registries.device("serial-a:air-device").via_device_id == registries.device("serial-a:air-extension").id
    assert registries.device("serial-a:wire-sensor").via_device_id == registries.device("serial-a:wire-extension").id


async def test_service_module_is_created_only_when_it_has_supported_channels(registries):
    runtime = EngineeringRuntimeInventory(
        bindings=(numeric_binding("weather-value", 18.5, "WeatherData"),)
    )
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("weather-server", "WeatherServer", room=None),
            element("weather-value", "WeatherData", parent_uuid="weather-server", io_name="WDC1"),
            element("empty-service", "GlobalStates", room=None),
        ),
        runtime=runtime,
    )
    await async_sync_engineering_devices(
        registries.hass,
        "entry-a",
        snapshot,
        EngineeringRegistryMetadata.empty(),
    )

    assert registries.device("serial-a:weather-server") is not None
    assert registries.device("serial-a:empty-service") is None


async def test_loxone_area_move_updates_only_integration_managed_assignment(registries):
    previous = EngineeringRegistryMetadata(
        active_device_identifiers=frozenset({"serial-a:device"}),
        room_names=frozenset({"Office"}),
        managed_area_ids={"serial-a:device": "office-area"},
    )
    moved = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="ST-F07", room="Workshop"),
        )
    )
    registries.device("serial-a:device").area_id = "office-area"
    await async_sync_engineering_devices(registries.hass, "entry-a", moved, previous)
    assert registries.device("serial-a:device").area_id == "work-area"

    registries.device("serial-a:device").area_id = "user-selected-area"
    await async_sync_engineering_devices(registries.hass, "entry-a", moved, previous)
    assert registries.device("serial-a:device").area_id == "user-selected-area"


async def test_legacy_unscoped_device_is_not_merged_when_multiple_entries_claim_uuid(registries):
    registries.set_loaded_claims("shared-uuid", {"entry-a", "entry-b"})
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("shared-uuid", "TreeDevice", parent_uuid="ms", title="ST-F01"),
        )
    )
    result = await async_sync_engineering_devices(
        registries.hass,
        "entry-a",
        snapshot,
        EngineeringRegistryMetadata.empty(),
    )

    assert result.migrated_entities == 0
    assert registries.device("shared-uuid") is not None


@pytest.mark.parametrize("load_order", (("entry-a", "entry-b"), ("entry-b", "entry-a")))
async def test_global_entity_collision_never_rewires_existing_owner(registries, load_order):
    registries.persisted_entity(
        domain="sensor", platform=DOMAIN, unique_id="shared-channel",
        config_entry_id=load_order[0], device_id="owner-device",
    )
    specs = (engineering_spec("shared-channel"),)

    accepted, rejected = await async_filter_entity_identity_conflicts(
        registries.hass, load_order[1], specs
    )

    assert accepted == ()
    assert rejected[0].reason == "entity_unique_id_owned_by_other_entry"
    assert registries.entity("sensor", DOMAIN, "shared-channel").device_id == "owner-device"


async def test_registry_plan_replays_after_partial_application(registries):
    plan = await async_plan_engineering_registry_sync(
        registries.hass, "entry-a", make_snapshot(), EngineeringRegistryMetadata.empty()
    )
    registries.fail_update_number = 2
    with pytest.raises(RuntimeError):
        await async_apply_engineering_registry_plan(registries.hass, plan)

    registries.fail_update_number = None
    first = await async_apply_engineering_registry_plan(registries.hass, plan)
    second = await async_apply_engineering_registry_plan(registries.hass, plan)
    assert first.metadata == second.metadata
    assert registries.has_duplicate_devices is False
```

The `registries` fixture must provide in-memory fakes with the exact installed Home Assistant signatures used by production: device `async_get_or_create`, `async_get_device_by_identifier(identifier, config_entry_id)`, `async_update_device(..., via_device_id=...)`; area `async_get_area_by_name`, `async_get_or_create`; entity `async_get_entity_id(domain, platform, unique_id)`, `async_get`, `async_update_entity(..., config_entry_id=..., device_id=...)`; and persisted ownership independent of `hass.data[DOMAIN]`. Its `device(identifier)` helper returns a fake entry by `(DOMAIN, identifier)` within `entry-a`, `miniserver` is pre-created as `(DOMAIN, "serial-a")`, and fault injection can fail any numbered registry mutation. Tests must cover an unloaded owner and restart, not only two loaded coordinators.

- [ ] **Step 2: Run registry tests and confirm the new synchronization layer is missing**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_registry.py tests\test_registry_maintenance.py -v`

Expected: collection fails because `engineering_registry` does not exist.

- [ ] **Step 3: Implement device creation and ordered `via_device` synchronization**

Define:

```python
@dataclass(frozen=True, slots=True)
class EngineeringRegistryMetadata:
    active_device_identifiers: frozenset[str]
    room_names: frozenset[str]
    managed_area_ids: Mapping[str, str]
    applied_generation: str | None

    @classmethod
    def empty(cls) -> EngineeringRegistryMetadata:
        return cls(frozenset(), frozenset(), {}, None)


@dataclass(frozen=True, slots=True)
class EngineeringRegistryPlan:
    generation_id: str
    device_operations: tuple[EngineeringDeviceOperation, ...]
    entity_operations: tuple[EngineeringEntityOperation, ...]
    metadata: EngineeringRegistryMetadata


@dataclass(frozen=True, slots=True)
class EngineeringRegistrySyncResult:
    created_identifiers: tuple[str, ...]
    updated_identifiers: tuple[str, ...]
    migrated_entities: int
    metadata: EngineeringRegistryMetadata
```

The planner reads the current registries but performs no mutation. The applier runs two device passes: first create/update every eligible Miniserver reference, bus, bridge, physical device, and service module; then resolve entry-scoped registry IDs and call `async_update_device(..., via_device_id=<concrete id>)`. Register `(DOMAIN, scoped_identifier)` and keep the Miniserver identifier unscoped as specified. Use `suggested_area` on creation. Update `area_id` on a later room move only when the current area is empty or equals the previously stored managed area. Do not create devices for structural/channel nodes or empty/unsupported service modules. Every operation has a stable target and desired end state so applying the same plan again is harmless after partial failure or restart.

- [ ] **Step 4: Implement conservative legacy entity reassociation**

For each entity spec, use global `async_get_entity_id(platform, DOMAIN, engineering_uuid)`. If the entry belongs to another config entry, do not mutate it and return an inventory-only rejection with the fixed reason from ruling 5. This lookup must work from the persisted registry even when the owner entry is unloaded. Otherwise update only `device_id`, `original_name`, and integration-owned area association. Reassociate an entity from a legacy `(DOMAIN, engineering_uuid)` device only when the persisted registry and all available snapshots establish one claimant; loaded-coordinator state alone is insufficient. Leave ambiguous legacy devices and entity associations unchanged and include the ambiguity in the sync result.

- [ ] **Step 5: Change stale metadata consumers to use scoped identifiers**

Store registry metadata through the Task 4 private `StoredEngineeringState` envelope. After a plan is fully applied, persist its `managed_area_ids` and `applied_generation`. In `async_run_registry_maintenance()`, merge `metadata.active_device_identifiers` into the public LoxAPP3 identifiers and `metadata.room_names` into current rooms. Its own maintenance store writes the last counted engineering generation atomically with updated missing counters before any optional idempotent deletion attempt. An absent, incomplete, pending, or already-counted generation never increments observations; an already-counted complete snapshot may still run a read-only elapsed-time eligibility audit so time and combined grace modes can mature. Preserve the existing default-off cleanup option and all three grace modes. Add tests for same-generation time passage, repeated audit, failure around the counter-store write, replay after the store write, and default-off cleanup.

- [ ] **Step 6: Run registry and maintenance tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_registry.py tests\test_registry_maintenance.py tests\test_device_sync.py -v`

Expected: all selected tests pass.

- [ ] **Step 7: Commit registry synchronization**

```powershell
git add custom_components/loxone/engineering_registry.py custom_components/loxone/engineering_entities.py custom_components/loxone/registry_maintenance.py tests/test_engineering_registry.py tests/test_registry_maintenance.py
git commit -m "feat: register engineering device topology"
```

### Task 6: Prepare read-only sensor and binary-sensor entities under resolved owners

**Files:**
- Modify: `custom_components/loxone/sensor.py:315-463`
- Modify: `custom_components/loxone/binary_sensor.py:49-97`
- Modify: `custom_components/loxone/engineering_entities.py`
- Modify: `custom_components/loxone/__init__.py:460-470`
- Modify: `custom_components/loxone/const.py`
- Test: `tests/test_engineering_entities.py`
- Test: `tests/test_engineering_platforms.py`

**Interfaces:**
- Consumes: `build_engineering_entity_specs(snapshot.rows, runtime)` and the config-entry-scoped `engineering_inventory_updated_signal(entry_id)`.
- Produces: `filter_existing_loxapp_entities(specs, existing_uuids)`, updated `LoxoneEngineeringSensor`, and new `LoxoneEngineeringBinarySensor`, both preserving `unique_id == engineering UUID`, attaching to `(DOMAIN, owner_identifier)`, and subscribing to verified `state_uuid` events.
- Guarantees: existing LoxAPP3 entities and cross-entry registry owners win UUID deduplication; cached entities start unavailable but can rebind without an FTPS refresh; events are source-scoped; removed prepared entities become unavailable rather than being deleted immediately.

- [ ] **Step 1: Write failing platform tests**

```python
from custom_components.loxone.engineering_entities import EngineeringEntitySpec


def entity_spec(
    *,
    unique_id: str = "weather-value",
    platform: str = "sensor",
    value: float | bool | None = 18.5,
    available: bool = True,
) -> EngineeringEntitySpec:
    return EngineeringEntitySpec(
        unique_id=unique_id,
        state_uuid=unique_id,
        platform=platform,
        name="Outdoor temperature" if platform == "sensor" else "Input I1",
        native_value=value,
        unit="°C" if platform == "sensor" else None,
        available=available,
        owner_identifier="serial-a:weather-server" if platform == "sensor" else "serial-a",
        owner_name="Weather Server" if platform == "sensor" else "Miniserver",
        owner_model="WeatherServer" if platform == "sensor" else "Miniserver",
        room="Office",
        loxone_type="WeatherData" if platform == "sensor" else "DigitalIn",
        io_name="WDC1" if platform == "sensor" else "I1",
        config_version=7,
        runtime_binding="uuid_state" if available else None,
        enabled_by_default=False,
    )


def test_engineering_sensor_attaches_to_service_module_and_keeps_uuid():
    entity = LoxoneEngineeringSensor(entity_spec())

    assert entity.unique_id == "weather-value"
    assert entity.device_info["identifiers"] == {("loxone", "serial-a:weather-server")}
    assert "via_device" not in entity.device_info
    assert "name" not in entity.device_info
    assert entity.entity_registry_enabled_default is False


def test_engineering_binary_sensor_is_read_only_and_uses_boolean_value():
    entity = LoxoneEngineeringBinarySensor(
        entity_spec(unique_id="digital-i1", platform="binary_sensor", value=True)
    )

    assert entity.unique_id == "digital-i1"
    assert entity.is_on is True
    assert entity.entity_registry_enabled_default is False
    assert not hasattr(entity, "turn_on")
    assert not hasattr(entity, "turn_off")


def test_cached_entity_spec_is_created_unavailable():
    entity = LoxoneEngineeringSensor(entity_spec(value=None, available=False))

    assert entity.available is False
    assert entity.native_value is None


def test_public_loxapp_uuid_suppresses_duplicate_engineering_entity():
    specs = filter_existing_loxapp_entities(
        (entity_spec(unique_id="existing-uuid"),),
        {"existing-uuid"},
    )
    assert specs == ()


async def test_cached_entity_rebinds_and_updates_from_state_uuid_event(entity_platform):
    entity = entity_platform.add(entity_spec(value=None, available=False))
    await entity_platform.rebind({"weather-value": 18.5})
    entity_platform.fire_loxone_event("weather-value", 19.0)

    assert entity.available is True
    assert entity.native_value == 19.0


async def test_non_finite_or_text_events_do_not_replace_last_good_value(entity_platform):
    entity = entity_platform.add(entity_spec(value=18.5, available=True))
    entity_platform.fire_loxone_event("weather-value", float("nan"))
    entity_platform.fire_loxone_event("weather-value", "18.5 private-label")

    assert entity.native_value == 18.5


async def test_same_state_uuid_from_other_config_entry_is_ignored(entity_platform):
    entity = entity_platform.add(entity_spec(value=18.5, available=True))
    entity_platform.fire_engineering_event("entry-b", "weather-value", 21.0)
    entity_platform.fire_engineering_event("entry-a", "weather-value", 19.0)
    assert entity.native_value == 19.0


async def test_entity_ownership_is_rechecked_immediately_before_add(entity_platform):
    entity_platform.plan_specs(entity_spec(unique_id="raced-uuid"))
    entity_platform.persist_other_entry_owner("sensor", "raced-uuid")
    await entity_platform.add_planned_specs()
    assert "raced-uuid" not in entity_platform.entities
```

- [ ] **Step 2: Run platform tests and confirm binary engineering support is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_platforms.py tests\test_engineering_entities.py -v`

Expected: tests fail because `LoxoneEngineeringBinarySensor` and platform-neutral setup helpers are missing.

- [ ] **Step 3: Refactor sensor setup to consume platform-neutral specs**

Keep one `prepared_entities: dict[str, LoxoneEngineeringSensor]` per config entry. Filter every spec whose UUID is already exposed by normal LoxAPP3 discovery or rejected by Task 5's persisted entity-identity check. Construct device info only from the resolved owner identifier:

```python
device_info = {
    "identifiers": {(DOMAIN, spec.owner_identifier)},
}
self._attr_device_info = DeviceInfo(**device_info)
```

Device names, models, rooms, and `via_device_id` are owned exclusively by Task 5 registry synchronization. Set `_attr_entity_registry_enabled_default = spec.enabled_by_default`, `_attr_available = spec.available`, and expose only technical fields (`uuid`, `io_name`, `loxone_type`, config version, binding method) as extra attributes. Do not expose raw XML attributes, arbitrary runtime text, full paths containing access labels, or endpoint URLs.

- [ ] **Step 4: Add binary-sensor subscription and read-only entity class**

In `binary_sensor.async_setup_entry()`, retain current LoxAPP3 entities and subscribe to the same config-entry signal. Add only specs with `platform == "binary_sensor"`. Implement:

```python
class LoxoneEngineeringBinarySensor(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, spec: EngineeringEntitySpec) -> None:
        self._spec = spec
        self._attr_unique_id = spec.unique_id
        self._attr_name = spec.name
        self._attr_is_on = bool(spec.native_value) if spec.native_value is not None else None
        self._attr_available = spec.available
        self._attr_entity_registry_enabled_default = spec.enabled_by_default
        self._set_device_info(spec)

    @callback
    def update_spec(self, spec: EngineeringEntitySpec) -> None:
        self._spec = spec
        self._attr_is_on = bool(spec.native_value) if spec.native_value is not None else None
        self._attr_available = spec.available
        if self.hass is not None:
            self.async_write_ha_state()

    @callback
    def mark_unavailable(self) -> None:
        self._attr_available = False
        if self.hass is not None:
            self.async_write_ha_state()
```

Both engineering entity classes subscribe through a new internal dispatcher
path keyed by `(config_entry_id, spec.state_uuid)`. The existing public
`loxone_event` bus event remains unchanged for compatibility, but the websocket
callback also forwards the value through the scoped internal path. A bounded reconnect rebind
reads the currently known value without an engineering download. Accept only
finite values already classified for the platform (`0/1` for binary sensors;
finite number with the stored allowlisted unit for sensors). Transport or auth
failure marks the binding unavailable while retaining the cached topology and
last safe value. Unsubscribe callbacks are registered with the entity lifecycle.
Immediately before `async_add_entities()` and before any reassociation, repeat
Task 5's global entity-registry ownership check to close the planning/setup race.
Test two config entries with the same state UUID, including a source whose entity
was suppressed by the collision rule; its event must not affect the owner.

- [ ] **Step 5: Run platform and existing sensor tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_platforms.py tests\test_engineering_entities.py tests\test_sensor_matching.py -v`

Expected: all selected tests pass and no writable method exists on engineering entities.

- [ ] **Step 6: Commit read-only platform exposure**

```powershell
git add custom_components/loxone/sensor.py custom_components/loxone/binary_sensor.py custom_components/loxone/engineering_entities.py custom_components/loxone/__init__.py custom_components/loxone/const.py tests/test_engineering_entities.py tests/test_engineering_platforms.py
git commit -m "feat: expose resolved read-only channels"
```

### Task 7: Restore, debounce, and automatically refresh engineering data

**Files:**
- Modify: `custom_components/loxone/coordinator.py:32-141`
- Modify: `custom_components/loxone/__init__.py:288-430`
- Modify: `custom_components/loxone/button.py:59-110`
- Modify: `custom_components/loxone/config_impact.py:22-216`
- Test: `tests/test_engineering_coordinator.py`
- Test: `tests/test_engineering_snapshot.py`
- Test: `tests/test_engineering_impacts.py`

**Interfaces:**
- Consumes: private stored state, topology resolver, capability resolver, mutation-free registry planning, idempotent registry application, runtime probe/rebind, and LoxAPP3 `lastModified`.
- Produces: `extract_loxapp_last_modified(lox_config)`, `LoxoneCoordinator._async_download_engineering_inventory()`, `async_restore_engineering_snapshot()`, `async_schedule_engineering_refresh()`, `async_refresh_engineering_inventory(force=False)`, `async_rebind_engineering_runtime()`, `async_drain_committed_engineering_state(startup=False)`, mutation-free `async_find_engineering_change_impacts(...)`, the minimal real idempotent `async_publish_engineering_impact_plan(...)`, and committed/applied/published generation state.
- Guarantees: unchanged revisions do not download FTPS but do rebind cached channels; changed revisions queue one debounced refresh; manual refresh bypasses comparison; pre-commit failure leaves old state untouched; post-commit registry failure retains a replayable pending generation and publishes no signals, impacts, or stale observation.

- [ ] **Step 1: Write failing restore, debounce, unchanged, manual, and failure tests**

```python
from unittest.mock import AsyncMock

from tests.engineering_fixtures import inventory_of, make_snapshot, provider_inventory


def test_last_modified_accepts_only_scalar_revision_values():
    assert extract_loxapp_last_modified({"lastModified": "revision-7"}) == "revision-7"
    assert extract_loxapp_last_modified({"lastModified": 7}) == "7"
    assert extract_loxapp_last_modified({"lastModified": {"name": "unsafe"}}) is None


async def test_restore_registers_cached_devices_before_network_refresh(coordinator, stores, registries):
    stores.snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", room=None),
            element("tree", "LoxTree", parent_uuid="ms", room=None),
            element("nfc", "TreeDevice", parent_uuid="tree", title="NFC endpoint"),
        )
    )
    coordinator.async_rebind_engineering_runtime = AsyncMock(return_value=None)
    await coordinator.async_restore_engineering_snapshot()

    assert coordinator.engineering_snapshot == stores.snapshot
    assert registries.device("serial-a:nfc") is not None
    coordinator.async_rebind_engineering_runtime.assert_awaited_once()


async def test_unchanged_revision_does_not_download(coordinator):
    coordinator.engineering_snapshot = make_snapshot(last_modified="revision-7")
    coordinator.miniserver.lox_config.json["lastModified"] = "revision-7"
    coordinator._async_download_engineering_inventory = AsyncMock()
    coordinator.async_rebind_engineering_runtime = AsyncMock()

    assert await coordinator.async_refresh_engineering_inventory(force=False) is None
    coordinator._async_download_engineering_inventory.assert_not_awaited()
    coordinator.async_rebind_engineering_runtime.assert_awaited_once()


async def test_manual_refresh_downloads_even_when_revision_is_unchanged(coordinator):
    coordinator.engineering_snapshot = make_snapshot(last_modified="revision-7")
    coordinator.miniserver.lox_config.json["lastModified"] = "revision-7"
    coordinator._async_download_engineering_inventory = AsyncMock(
        return_value=provider_inventory()
    )

    result = await coordinator.async_refresh_engineering_inventory(force=True)

    assert result is coordinator.engineering_snapshot
    coordinator._async_download_engineering_inventory.assert_awaited_once()


async def test_failed_or_incomplete_refresh_preserves_last_good_snapshot(coordinator, stores, registries):
    original = make_snapshot(last_modified="revision-7")
    coordinator.engineering_snapshot = original
    stores.snapshot = original
    coordinator._async_download_engineering_inventory = AsyncMock(
        return_value=inventory_of()
    )

    with pytest.raises(EngineeringConfigError):
        await coordinator.async_refresh_engineering_inventory(force=True)

    assert coordinator.engineering_snapshot is original
    assert stores.snapshot is original
    assert registries.mutations == []


async def test_snapshot_store_failure_leaves_memory_and_registries_untouched(
    coordinator, stores, registries
):
    original = make_snapshot(last_modified="revision-7")
    coordinator.engineering_snapshot = original
    stores.fail_next_write = True
    coordinator._async_download_engineering_inventory = AsyncMock(
        return_value=provider_inventory()
    )

    with pytest.raises(RuntimeError):
        await coordinator.async_refresh_engineering_inventory(force=True)

    assert coordinator.engineering_snapshot is original
    assert registries.mutations == []
    assert coordinator.published_generations == []


async def test_partial_registry_failure_is_committed_pending_and_replayed(
    coordinator, stores, registries
):
    registries.fail_update_number = 2
    coordinator._async_download_engineering_inventory = AsyncMock(
        return_value=provider_inventory()
    )

    with pytest.raises(RuntimeError):
        await coordinator.async_refresh_engineering_inventory(force=True)

    pending = stores.state.snapshot.generation_id
    assert stores.state.registry_applied_generation != pending
    assert coordinator.published_generations == []
    assert coordinator.stale_observations == []

    registries.fail_update_number = None
    await coordinator.async_restore_engineering_snapshot()
    assert stores.state.registry_applied_generation == pending
    assert coordinator.published_generations == [pending]


async def test_cold_restart_finishes_post_apply_publication(coordinator_factory, stores):
    first = coordinator_factory()
    first.fail_after_applied_token = True
    with pytest.raises(RuntimeError):
        await first.async_refresh_engineering_inventory(force=True)

    generation = stores.state.snapshot.generation_id
    assert stores.state.registry_applied_generation == generation
    assert stores.state.impact_published_generation != generation
    assert stores.state.pending_impact_plan.generation_id == generation

    restarted = coordinator_factory()
    await restarted.async_restore_engineering_snapshot()
    assert stores.state.impact_published_generation == generation
    assert restarted.notifications_were_replayed_once is True


async def test_cold_restart_reconciles_even_when_applied_cursor_matches(
    coordinator_factory, stores, registries
):
    committed = stores.complete_applied_state()
    registries.restore_older_persisted_registry()
    registries.notifications.clear()

    restarted = coordinator_factory()
    await restarted.async_restore_engineering_snapshot()

    assert registries.matches(committed.snapshot)
    assert registries.notification_matches(committed.pending_impact_plan)


async def test_pending_generation_must_drain_before_fast_path_or_replacement(
    coordinator, stores
):
    stores.state = stores.pending_registry_state()
    coordinator.fail_drain = True
    coordinator._async_download_engineering_inventory = AsyncMock()

    with pytest.raises(RuntimeError):
        await coordinator.async_refresh_engineering_inventory(force=True)

    coordinator._async_download_engineering_inventory.assert_not_awaited()
    assert stores.state.snapshot.generation_id == stores.pending_generation


async def test_forced_same_revision_allocates_new_observation_but_retry_does_not(
    coordinator, stores
):
    await coordinator.async_refresh_engineering_inventory(force=True)
    first = stores.state.snapshot
    await coordinator.async_refresh_engineering_inventory(force=True)
    second = stores.state.snapshot
    assert second.configuration_revision_id == first.configuration_revision_id
    assert second.read_sequence == first.read_sequence + 1
    assert second.generation_id != first.generation_id

    await coordinator.async_restore_engineering_snapshot()
    assert stores.state.snapshot.generation_id == second.generation_id
    assert coordinator.stale_observations.count(second.generation_id) == 1
```

- [ ] **Step 2: Run coordinator tests and confirm automatic orchestration is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_coordinator.py tests\test_engineering_snapshot.py -v`

Expected: tests fail on missing restore/scheduling/snapshot behavior.

- [ ] **Step 3: Implement last-modified comparison and one refresh transaction**

`async_refresh_engineering_inventory(force=False)` must execute in this order:

1. Acquire the per-entry refresh lock and call
   `async_drain_committed_engineering_state()`. This completes pending registry,
   impact-publication, and maintenance phases. If it fails, keep that single
   pending envelope and stop without reading or committing a new candidate.
2. Read and normalize current LoxAPP3 `lastModified`.
3. When `force` is false and the revision equals the stored revision, perform
   only `async_rebind_engineering_runtime()` and return without FTPS.
4. Download and parse into local candidate values without replacing coordinator
   fields.
5. Reject an incomplete candidate, duplicate UUIDs, unsafe snapshot fields, or
   a failed auth/transport probe without mutating old state.
6. Select safe probe candidates by technical capability, run bounded GET-only
   probes, and build source, topology, capability rows, and entity specs.
7. Build a candidate snapshot with the next persisted read sequence and a
   deterministic safe-content digest, plus a registry plan. Compute its diff
   and consumer impacts through `async_find_engineering_change_impacts()`
   against the previous snapshot and pre-mutation Home
   Assistant registry. This entire candidate phase is mutation-free.
8. Persist the validated candidate, sanitized `EngineeringImpactPlan`, and
   pending registry/impact cursors together as the committed last-known-good generation.
   A failure here leaves old memory and registries untouched.
9. Swap coordinator memory to the committed generation, then apply the registry
   plan idempotently. If application fails or is cancelled, retain the committed
   generation with an older `registry_applied_generation`, create one bounded
   degraded-state notification, and stop without dispatcher signals, impact
   publication/dismissal, or stale observation.
10. After full registry success, persist the matching applied token and managed
   area metadata. If this token write fails, treat the generation as pending and
   replay safely; do not publish it yet.
11. Send the config-entry-scoped entity signal, publish/dismiss the persisted
    impact plan idempotently, save its publication cursor, and submit the
    generation token to stale maintenance exactly once. A crash between any two
    phases is completed from the stored cursors on cold restart.

Do not put passwords, hosts, URLs, arbitrary exception messages, or downloaded XML into notification text. Use a fixed notification ID per config entry so repeat failures replace one notification rather than accumulating.

- [ ] **Step 4: Implement cache restore and a cancellable debounce task**

Add coordinator fields:

```python
self.engineering_snapshot: EngineeringSnapshot | None = None
self._engineering_refresh_task: asyncio.Task[None] | None = None
self._engineering_refresh_lock = asyncio.Lock()
```

`async_restore_engineering_snapshot()` loads the config-entry store, validates that its source entry ID and provider identifier match the connected Miniserver, swaps in cached unavailable entity specs, and calls `async_drain_committed_engineering_state(startup=True)`. Startup mode always rebuilds a fresh plan and reconciles desired topology against the loaded Home Assistant registries even when the applied cursor matches, because HA registry writes are delayed. It also recreates a still-applicable fixed-ID notification from the stored plan even when the publication cursor matches; a user dismissal therefore lasts for the current process but not across restart while the problem remains. No private registry storage method is called. The drain then consults maintenance state and completes any lagging observation. Only after this succeeds may entities be signaled and runtime rebound. `async_schedule_engineering_refresh(delay=5.0)` cancels only the coordinator's previous pending debounce task and schedules one background refresh. `async_cleanup()` cancels and awaits that task before closing the API. Cancellation at each await boundary follows the same pre-commit/post-commit recovery rule and is covered by true cold-restart fault-injection tests at candidate save, partial registry mutation, applied-token save, impact publication, and stale-observation save. Cold-restart fakes retain the integration store while restoring an older HA registry and empty notification dictionary.

- [ ] **Step 5: Wire startup/reload and manual refresh**

After `MiniServer` is constructed and stored under `hass.data[DOMAIN][entry_id]`, restore the cached snapshot before platform forwarding. After all platforms have subscribed, schedule the automatic revision check. Existing reconnect handling reloads the config entry, so the same path covers reconnects. Change the button to call `async_refresh_engineering_inventory(force=True)` and show only counts/status in attributes and its bounded notification.

- [ ] **Step 6: Run coordinator, button-adjacent, and setup tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_coordinator.py tests\test_engineering_snapshot.py tests\test_engineering_entities.py -v`

Expected: all selected tests pass; unchanged auto checks make zero download calls; manual refresh makes one.

- [ ] **Step 7: Commit automatic refresh orchestration**

```powershell
git add custom_components/loxone/coordinator.py custom_components/loxone/__init__.py custom_components/loxone/button.py custom_components/loxone/config_impact.py tests/test_engineering_coordinator.py tests/test_engineering_snapshot.py tests/test_engineering_impacts.py
git commit -m "feat: refresh engineering inventory safely"
```

### Task 8: Surface sanitized inventory and warn about affected consumers

**Files:**
- Modify: `custom_components/loxone/config_impact.py:22-216`
- Modify: `custom_components/loxone/diagnostics.py:13-33`
- Modify: `custom_components/loxone/registry_maintenance.py`
- Modify: `custom_components/loxone/button.py`
- Test: `tests/test_engineering_impacts.py`
- Test: `tests/test_config_impact.py`
- Test: `tests/test_registry_maintenance.py`

**Interfaces:**
- Consumes: Task 7's persisted immutable `EngineeringImpactPlan`, a successfully applied generation token, maintenance state, and `EngineeringSnapshot`.
- Produces: richer presentation/coverage around Task 7's real idempotent publisher, exactly-once counter advancement plus same-token elapsed-time audit, and a diagnostics-safe `engineering_inventory` tree/table payload.
- Guarantees: impact discovery precedes registry mutation; publication follows successful registry application; affected automations/scripts/scenes/groups are reported but never modified; removed nodes enter existing grace handling once per generation; sensitive rows are omitted from public diagnostics.

- [ ] **Step 1: Write failing warning and diagnostics tests**

```python
from custom_components.loxone.engineering_changes import EngineeringChangeSet
from tests.engineering_fixtures import make_snapshot


async def test_removed_referenced_entity_creates_one_scoped_warning(impact_fakes):
    plan = await async_find_engineering_change_impacts(
        impact_fakes.hass,
        impact_fakes.config_entry,
        PREVIOUS_WITH_REMOVED_CHANNEL,
        make_snapshot(),
    )
    count = await async_publish_engineering_impact_plan(
        impact_fakes.hass, impact_fakes.config_entry, plan
    )

    assert count == 1
    assert impact_fakes.notifications[0].notification_id == "loxone_engineering_impact_entry-a"
    assert "automation.office_button" in impact_fakes.notifications[0].message
    assert impact_fakes.automation_updates == []


async def test_unreferenced_metadata_rename_does_not_warn(impact_fakes):
    plan = await async_find_engineering_change_impacts(
        impact_fakes.hass,
        impact_fakes.config_entry,
        PREVIOUS_WITH_OLD_NAME,
        make_snapshot(),
    )
    count = await async_publish_engineering_impact_plan(
        impact_fakes.hass, impact_fakes.config_entry, plan
    )
    assert count == 0


async def test_diagnostics_contains_complete_safe_rows_without_raw_exports(hass, entry):
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert "LoxAPP3.json" not in diagnostics
    rows = diagnostics["engineering_inventory"]["rows"]
    assert {row["exposure_status"] for row in rows} >= {
        "prepared_disabled",
        "inventory_only",
        "suppressed",
    }
    serialized = json.dumps(diagnostics)
    assert "attributes" not in serialized
    assert "runtime_endpoint" not in serialized
    assert "sensitive-payload" not in serialized
```

Build `impact_fakes` with the same in-memory registry/Searcher pattern already used in `tests/test_config_impact.py`: one entity registry entry with unique ID `removed-channel`, one automation search result `automation.office_button`, a notification recorder, and an `automation_updates` list that remains empty. Patch only `dr.async_get`, `er.async_get`, `entity_sources`, `Searcher`, `persistent_notification.async_create`, and `persistent_notification.async_dismiss`; no Home Assistant service call is permitted in this fixture. Capture the immutable impact plan before applying a registry plan, mutate the fake registry, and prove publication still uses the captured previous entity identity.

- [ ] **Step 2: Run impact and diagnostics tests and confirm they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_impacts.py tests\test_config_impact.py -v`

Expected: tests fail because engineering change impacts and sanitized snapshot diagnostics are not wired.

- [ ] **Step 3: Complete idempotent impact publication without changing consumers**

Task 7 already implements mutation-free discovery, persists its sanitized
`EngineeringImpactPlan` before registry mutation, and implements the minimal
real publisher. In this task, extend its presentation and integration coverage:
the publisher uses one config-entry-scoped fixed notification ID and dismisses
it only when that exact generation has applied successfully with no impacts.
Replaying the same plan after a crash is harmless.
Names and room moves do not warn unless the persisted plan recorded an existing
area-targeted consumer from the pre-mutation registry/Searcher state. No Home
Assistant consumer configuration or service is modified.

- [ ] **Step 4: Replace raw diagnostics with the sanitized tree/table model**

Return these top-level keys:

```python
{
    "integration": {
        "entry_id": config_entry.entry_id,
        "provider_identifier": snapshot.source.provider_identifier,
        "config_version": snapshot.source.config_version,
        "config_timestamp": snapshot.source.config_timestamp.isoformat(),
        "captured_at": snapshot.captured_at.isoformat(),
    },
    "engineering_inventory": {
        "summary": snapshot.summary(),
        "rows": [row.as_public_dict() for row in snapshot.rows],
    },
    "engineering_runtime": coordinator.engineering_runtime.summary()
        if coordinator.engineering_runtime is not None
        else {"status": "restored_without_live_probe"},
}
```

Each row contains the exact inventory columns from the spec and no raw `attributes`, runtime numeric/text values, HTTP/FTPS endpoints, host information, access material, or arbitrary exception message. Keep suppressed rows as structural records but omit their names and paths, using `name=None`, `topology_path=[]`, and reason `sensitive_metadata_suppressed`.

- [ ] **Step 5: Verify stale observations advance only after a successful complete refresh**

Add a test that invokes the coordinator failure path followed by `async_run_registry_maintenance()` and asserts `missing_observations` is unchanged. Add a successful-removal test asserting the observation increments once and respects observation/time/combined mode exactly as configured. Pass `observation_token=snapshot.generation_id`; persist the last counted token and counters together before optional deletion. Replay of the identical committed token never increments, but may reevaluate elapsed-time eligibility. A later successful forced complete read has a new read sequence and increments once even if the Loxone revision is unchanged. Runtime-only rebinds, failed candidates, and retry of the same committed generation do not advance the counter. Test default-off cleanup, same-token time passage, and idempotent recovery around the maintenance-store write.

- [ ] **Step 6: Run impacts, diagnostics, and maintenance tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_impacts.py tests\test_config_impact.py tests\test_registry_maintenance.py -v`

Expected: all selected tests pass; no test records a consumer mutation.

- [ ] **Step 7: Commit diagnostics and warnings**

```powershell
git add custom_components/loxone/config_impact.py custom_components/loxone/diagnostics.py custom_components/loxone/registry_maintenance.py custom_components/loxone/button.py tests/test_engineering_impacts.py tests/test_config_impact.py tests/test_registry_maintenance.py
git commit -m "feat: report engineering inventory impacts"
```

### Task 9: Document, version, audit, and validate the complete feature

**Files:**
- Modify: `docs/engineering-config-spike.md`
- Create: `docs/engineering-owner-resolution.md`
- Modify: `custom_components/loxone/manifest.json`
- Review: all files changed from `upstream/master`

**Interfaces:**
- Consumes: the completed feature from Tasks 1-8.
- Produces: fork build `0.9.22.13`, user-facing operating documentation, a clean privacy audit, full automated verification, and a local HA test deployment report.
- Delivery remains local; this task creates no remote branch, discussion edit, or pull request.

- [ ] **Step 1: Write concise operating and safety documentation**

Document these use cases in `docs/engineering-owner-resolution.md`:

1. A newly connected Tree endpoint appears as a device under `Miniserver → Tree` even with all entities disabled.
2. Air and 1-Wire endpoints show their Link extension as the immediate parent.
3. Internal digital/analog inputs and status values attach to the Miniserver.
4. Weather and system-variable values group below service-module devices.
5. Manual refresh always reads; automatic refresh downloads only after `lastModified` changes.
   An unchanged revision still performs a read-only runtime rebind so cached
   entities recover without another engineering archive download.
6. Names, rooms, and topology moves preserve UUID identity; explicit user area overrides are respected.
7. Removed or platform-incompatible referenced entities create a warning; automations are never edited.
8. Outputs, NFC access data, arbitrary text, and unknown channels remain non-writable and are explained in inventory status.
9. Audit-only stale cleanup is the default; observation, time, and combined grace policies remain selectable.

State explicitly that engineering access is local, read-only, GET/FTPS based, and preserves the last good cache on failure.

- [ ] **Step 2: Bump the local fork version once**

Change only:

```json
"version": "0.9.22.13"
```

- [ ] **Step 3: Run formatter/linter checks available in the repository**

Run: `.\.venv\Scripts\python.exe -m ruff check custom_components tests`

Expected: exit code 0.

Run: `.\.venv\Scripts\python.exe -m ruff format --check custom_components tests`

Expected: exit code 0. If formatting is required, run `.\.venv\Scripts\python.exe -m ruff format custom_components tests`, inspect the diff, and rerun both checks.

- [ ] **Step 4: Run the complete automated suite**

Run: `.\.venv\Scripts\python.exe -m pytest`

Expected: all collected tests pass; only the already known Home Assistant/aiohttp deprecation warnings may remain.

- [ ] **Step 5: Audit the complete upstream diff and every transmissible commit for privacy and identity**

Run:

```powershell
git diff --check upstream/master...HEAD
git log --format='%h %an <%ae> %s' upstream/master..HEAD
git diff --name-only upstream/master...HEAD
git diff -U0 upstream/master...HEAD | rg -n -i "^\+.*(latitude|longitude|gps|currentuser|username|password|token|remoteurl|localurl|hostaddress|accesscode|private[_ -]?key)"
git diff -U0 upstream/master...HEAD | rg -n "^\+.*(192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)"
```

Expected: `diff --check` is empty; every commit uses the approved GitHub identity; added-line hits are limited to generic field-deny/allowlist logic and synthetic safety assertions; private-address search returns no committed installation data. Inspect every added line and every remaining match manually before the final commit. Also inspect each transmissible commit rather than relying only on the aggregate diff.

- [ ] **Step 6: Commit the version and documentation**

```powershell
git add docs/engineering-config-spike.md docs/engineering-owner-resolution.md custom_components/loxone/manifest.json
git commit -m "docs: explain engineering owner resolution"
```

- [ ] **Step 7: Re-run final verification after the commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check custom_components tests
.\.venv\Scripts\python.exe -m ruff format --check custom_components tests
.\.venv\Scripts\python.exe -m pytest
git status --short --branch
git rev-list --left-right --count upstream/master...HEAD
```

Expected: lint/format/tests exit 0, the working tree is clean, and the branch reports `0` upstream-only commits.

- [ ] **Step 8: Stop for explicit live-deployment authorization, then validate read-only behavior**

Before any backup, copy, install, integration reload, or Home Assistant restart,
present the completed automated verification and request a fresh explicit user
authorization. Do not treat approval of this implementation plan as approval
for the live mutation. After authorization, use the Home Assistant
API/operations path, not browser automation, to back up the currently installed
integration, copy the verified `custom_components/loxone` directory,
restart/reload Home Assistant, and confirm the reported integration version is
`0.9.22.13`. Trigger the manual engineering refresh once and verify:

- the Miniserver has Link and Tree children when present;
- the Tree NFC endpoint exists as a device without exposing tag/code data;
- Air and 1-Wire endpoints use their extension as `via_device`;
- central digital/analog input groups appear only when present;
- Weather Server and System Variables group their prepared entities;
- no relay or analog output changed state during download, probing, registration, or refresh;
- the automatic follow-up with unchanged `lastModified` performs no second FTPS download;
- integration logs contain no new error and no raw sensitive/access value.

Record the actual Home Assistant version and Loxone Miniserver firmware used for this validation in `.superpowers/sdd/2026-09-13-owner-resolver-service-modules/local-ha-validation.md`. The file remains git-ignored and contains only sanitized versions, counts, outcomes, and risks. Do not add host addresses, serial numbers, user names, project names, coordinates, credentials, or access labels to a commit.

- [ ] **Step 9: Stop at the local delivery boundary**

Report the branch name, commit list, test counts, lint result, Home Assistant version, Loxone firmware version, and any remaining review risk. Do not push or communicate upstream until the user explicitly authorizes the next step.
