# Owner Resolver and Service Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only, privacy-safe engineering inventory that reconstructs Loxone hardware ownership, registers entity-less devices and Miniserver service modules in Home Assistant, prepares verified read-only entities, persists the last good topology, and warns about configuration changes that affect Home Assistant consumers.

**Architecture:** The existing engineering XML parser and runtime probe remain side-effect free. New topology, capability, snapshot, registry, and change modules transform a complete engineering download into one immutable, source-scoped model before Home Assistant registries are changed; the coordinator persists that model and publishes one config-entry-scoped refresh signal. Existing LoxAPP3 discovery remains authoritative for already-supported entities, while the engineering model fills hardware/topology/service gaps without issuing any write probe.

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

- [ ] **Step 1: Write failing model, classification, completeness, and allowlist tests**

Create the shared synthetic constructors in `tests/engineering_fixtures.py`; later tasks extend this file only with synthetic values:

```python
from datetime import UTC, datetime

from custom_components.loxone.engineering_config import EngineeringElement, EngineeringInventory
from custom_components.loxone.engineering_topology import EngineeringSourceContext, ResolvedEngineeringInventory


def element(
    uuid: str,
    element_type: str,
    *,
    parent_uuid: str | None = None,
    title: str | None = None,
    io_name: str | None = None,
    room: str | None = "Office",
    platform: str | None = None,
) -> EngineeringElement:
    return EngineeringElement(
        key=uuid,
        xml_element="C",
        loxone_type=element_type,
        title=title or element_type,
        uuid=uuid,
        io_name=io_name,
        parent_uuid=parent_uuid,
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

from custom_components.loxone.engineering_config import EngineeringElement, EngineeringInventory
from custom_components.loxone.engineering_topology import (
    EngineeringSourceContext,
    NodeKind,
    classify_node_kind,
    scoped_engineering_identifier,
)
from tests.engineering_fixtures import element


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


@dataclass(frozen=True, slots=True)
class ResolvedEngineeringInventory:
    source: EngineeringSourceContext
    nodes: tuple[ResolvedEngineeringNode, ...]

    @property
    def nodes_by_key(self) -> dict[str, ResolvedEngineeringNode]:
        return {node.element.key: node for node in self.nodes}
```

Use case-folded exact type sets for `LoxLIVE`, `LoxLink`, `LoxTree`, Air/1-Wire extensions, typed physical endpoints, provider service containers, channels, and structural nodes. Permit conservative suffix checks only for `*Device`, `*Dev`, and `*Extension`, after excluding service and structural types. `scoped_engineering_identifier()` must return `f"{source.provider_identifier}:{element.uuid}"` for UUID-backed graph devices and return `None` for non-device nodes without a UUID. Add the `is_complete` property without changing parser limits or XML declaration rejection.

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
- Guarantees: every parsed node gets one result; physical and service ownership is UUID/type driven; cycles and missing ancestors produce `UNRESOLVED` rows rather than guesses.

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
        elements_by_uuid = {item.uuid: item for item in inventory.elements if item.uuid}
        kinds = {item.key: classify_node_kind(item) for item in inventory.elements}
        nodes = tuple(
            self._resolve_one(item, elements_by_uuid, kinds, source)
            for item in inventory.elements
        )
        return ResolvedEngineeringInventory(source=source, nodes=nodes)


def resolve_engineering_topology(
    inventory: EngineeringInventory,
    source: EngineeringSourceContext,
) -> ResolvedEngineeringInventory:
    return OwnerResolver().resolve(inventory, source)
```

For each node, walk no more than 128 UUID ancestors and retain structural titles only in `topology_path`. A physical endpoint selects the nearest registered bridge/bus as `via_device_identifier`. A channel selects the nearest physical device or service module as `device_identifier`; an internal channel beneath `LoxLIVE` selects the Miniserver identifier. A document-level service module uses the source Miniserver as its provider even when `LoxLIVE` is a sibling. WeatherData and SysVar nodes resolve to their typed WeatherServer/GlobalStates module. Missing parents use `missing_parent`; cycles use `parent_cycle`; exceeded depth uses `parent_depth_exceeded`.

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
- Produces: `CapabilityState`, `ExposureStatus`, `EngineeringCapability`, `EngineeringInventoryRow`, `resolve_engineering_capabilities(resolved, runtime)`, `EngineeringEntitySpec`, and `build_engineering_entity_specs(rows, runtime | None)`.
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
```

Use exact type sets for WeatherData, SysVar, DigitalIn, VoltageIn, Online/status, Actor/relay outputs, and analog outputs. Treat a bound numeric value as readable, a missing binding as configured-only, a non-numeric arbitrary text response as suppressed, and an unknown typed channel as unsupported. Define `SENSITIVE_TYPES` for access-code, NFC-tag, credential, user, and permission child records without marking the physical `TreeDevice` container sensitive. Do not define a writable whitelist in this change.

- [ ] **Step 4: Replace sensor-only specs with a platform-neutral entity spec**

In `engineering_entities.py`, define:

```python
@dataclass(frozen=True, slots=True)
class EngineeringEntitySpec:
    unique_id: str
    platform: Literal["sensor", "binary_sensor"]
    name: str
    native_value: float | bool | None
    unit: str | None
    available: bool
    owner_identifier: str
    owner_name: str
    owner_model: str
    via_device_identifier: str | None
    room: str | None
    loxone_type: str | None
    io_name: str | None
    config_version: int
    runtime_binding: str | None
    enabled_by_default: bool = False
```

`build_engineering_entity_specs()` must emit only `PREPARED_DISABLED` rows with stable engineering UUIDs. When runtime is `None` during cache restore, emit the remembered safe spec with `available=False`, `native_value=None`, and no runtime endpoint. Keep `normalize_engineering_unit()` and delete `_nearest_device()` after its callers move to the resolver.

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
- Produces: `EngineeringSnapshot`, `EngineeringChangeSet`, `async_load_engineering_snapshot(hass, entry_id)`, `async_store_engineering_snapshot(hass, snapshot)`, `snapshot_to_dict(snapshot)`, `snapshot_from_dict(data)`, and `diff_engineering_snapshots(previous, current)`.
- Storage key: `loxone.engineering_snapshot.<entry_id>`, version `1`, `private=True`.

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
    rows = resolve_engineering_capabilities(
        resolved,
        runtime or EngineeringRuntimeInventory(bindings=()),
    )
    return EngineeringSnapshot(
        source=context,
        nodes=resolved.nodes,
        rows=rows,
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

    assert changes.added == ("new-device",)
    assert changes.removed == ("removed-channel",)
    assert changes.renamed == ("renamed-device",)
    assert changes.room_moved == ("moved-device",)
    assert changes.reparented == ("reparented-device",)
    assert changes.platform_changed == ("changed-channel",)
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
    captured_at: datetime


class EngineeringSnapshotError(ValueError):
    """Raised when a candidate or stored snapshot is unsafe or incomplete."""
```

Serialize only the inventory columns named in the spec: node key, name, technical type, UUID, parent UUID, room, node kind, owner key, source provider identifier, bus kind, topology path, suggested platform, capability state, exposure status, resolution status, and reason. Do not serialize raw XML attributes, category values, runtime values, endpoint URLs, error strings, credentials, host data, or Miniserver titles. `snapshot_from_dict()` must validate enum values, list/string shapes, source entry ID, serial scope, duplicate stable UUIDs, parent cycles, and completeness before returning an immutable snapshot.

- [ ] **Step 4: Implement source-scoped UUID diffing**

Define:

```python
@dataclass(frozen=True, slots=True)
class EngineeringChangeSet:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    renamed: tuple[str, ...] = ()
    room_moved: tuple[str, ...] = ()
    reparented: tuple[str, ...] = ()
    platform_changed: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(astuple(self))
```

Compare only nodes with a stable engineering UUID and include the source provider in each lookup key. A name, room, owner/via identifier, or platform change must never be represented as remove-plus-add.

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
- Consumes: `EngineeringSnapshot`, Home Assistant registries, and previous integration-managed area metadata.
- Produces: `EngineeringRegistryMetadata`, `EngineeringRegistrySyncResult`, `async_sync_engineering_devices(hass, entry_id, snapshot, previous_metadata)`, and updated `async_store_engineering_registry_metadata()` / `async_load_engineering_registry_metadata()`.
- Guarantees: physical devices exist without entities; `via_device` uses the nearest registered owner; user-overridden areas remain untouched; ambiguous legacy devices are not merged.

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


def test_registry_creates_entityless_tree_device_with_full_via_chain(registries):
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("tree", "LoxTree", parent_uuid="ms", title="Tree", room=None),
            element("branch", "TreeCaption", parent_uuid="tree", title="Branch A"),
            element("nfc", "TreeDevice", parent_uuid="branch", title="NFC Code Touch"),
        )
    )
    result = async_sync_engineering_devices(
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


def test_registry_builds_link_bridge_endpoint_chains(registries):
    async_sync_engineering_devices(
        registries.hass,
        "entry-a",
        make_snapshot(inventory=reference_link_inventory()),
        EngineeringRegistryMetadata.empty(),
    )

    assert registries.device("serial-a:air-extension").via_device_id == registries.device("serial-a:link").id
    assert registries.device("serial-a:air-device").via_device_id == registries.device("serial-a:air-extension").id
    assert registries.device("serial-a:wire-sensor").via_device_id == registries.device("serial-a:wire-extension").id


def test_service_module_is_created_only_when_it_has_supported_channels(registries):
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
    async_sync_engineering_devices(
        registries.hass,
        "entry-a",
        snapshot,
        EngineeringRegistryMetadata.empty(),
    )

    assert registries.device("serial-a:weather-server") is not None
    assert registries.device("serial-a:empty-service") is None


def test_loxone_area_move_updates_only_integration_managed_assignment(registries):
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
    async_sync_engineering_devices(registries.hass, "entry-a", moved, previous)
    assert registries.device("serial-a:device").area_id == "work-area"

    registries.device("serial-a:device").area_id = "user-selected-area"
    async_sync_engineering_devices(registries.hass, "entry-a", moved, previous)
    assert registries.device("serial-a:device").area_id == "user-selected-area"


def test_legacy_unscoped_device_is_not_merged_when_multiple_entries_claim_uuid(registries):
    registries.set_loaded_claims("shared-uuid", {"entry-a", "entry-b"})
    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("shared-uuid", "TreeDevice", parent_uuid="ms", title="ST-F01"),
        )
    )
    result = async_sync_engineering_devices(
        registries.hass,
        "entry-a",
        snapshot,
        EngineeringRegistryMetadata.empty(),
    )

    assert result.migrated_entities == 0
    assert registries.device("shared-uuid") is not None
```

The `registries` fixture must provide in-memory fakes for exactly the Home Assistant methods used by the production function: device `async_get_or_create`, `async_get_device_by_identifier`, `async_update_device`; area `async_get_area_by_name`, `async_get_or_create`; entity `async_get_entity_id`, `async_get`, `async_update_entity`; and loaded coordinator claims through `hass.data[DOMAIN]`. Its `device(identifier)` helper returns a fake entry by `(DOMAIN, identifier)`, `miniserver` is pre-created as `(DOMAIN, "serial-a")`, and `set_loaded_claims()` installs two synthetic coordinator snapshots without any network or user data.

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

    @classmethod
    def empty(cls) -> EngineeringRegistryMetadata:
        return cls(frozenset(), frozenset(), {})


@dataclass(frozen=True, slots=True)
class EngineeringRegistrySyncResult:
    created_identifiers: tuple[str, ...]
    updated_identifiers: tuple[str, ...]
    migrated_entities: int
    metadata: EngineeringRegistryMetadata
```

Create/update nodes in this order: Miniserver reference, bus, bridge, physical device, service module. Register `(DOMAIN, scoped_identifier)` and use `(DOMAIN, via_identifier)` for `via_device`; keep the Miniserver identifier unscoped as specified. Use `suggested_area` on creation. Update `area_id` on a later room move only when the current area is empty or equals the previously stored managed area. Do not create devices for structural/channel nodes or empty/unsupported service modules.

- [ ] **Step 4: Implement conservative legacy entity reassociation**

For each entity spec, look up the existing entity registry entry by unchanged `(platform, DOMAIN, engineering_uuid)` and update only `device_id`, `original_name`, and integration-owned area association. Reassociate an entity from a legacy `(DOMAIN, engineering_uuid)` device only when exactly one loaded PyLoxone config entry claims that UUID. Leave ambiguous legacy devices and entity associations unchanged and include the ambiguity in the sync result count/log.

- [ ] **Step 5: Change stale metadata consumers to use scoped identifiers**

Update `async_load_engineering_registry_metadata()` to return `EngineeringRegistryMetadata`. In `async_run_registry_maintenance()`, merge `metadata.active_device_identifiers` into the public LoxAPP3 identifiers, merge `metadata.room_names` into current rooms, and leave observation counters unchanged on absent or incomplete engineering snapshots. Preserve the existing default-off cleanup option and all three grace modes.

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
- Test: `tests/test_engineering_entities.py`
- Test: `tests/test_engineering_platforms.py`

**Interfaces:**
- Consumes: `build_engineering_entity_specs(snapshot.rows, runtime)` and the config-entry-scoped `engineering_inventory_updated_signal(entry_id)`.
- Produces: `filter_existing_loxapp_entities(specs, existing_uuids)`, updated `LoxoneEngineeringSensor`, and new `LoxoneEngineeringBinarySensor`, both preserving `unique_id == engineering UUID` and attaching to `(DOMAIN, owner_identifier)`.
- Guarantees: existing LoxAPP3 entities win UUID deduplication; cached entities start unavailable; removed prepared entities become unavailable rather than being deleted immediately.

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
        platform=platform,
        name="Outdoor temperature" if platform == "sensor" else "Input I1",
        native_value=value,
        unit="°C" if platform == "sensor" else None,
        available=available,
        owner_identifier="serial-a:weather-server" if platform == "sensor" else "serial-a",
        owner_name="Weather Server" if platform == "sensor" else "Miniserver",
        owner_model="WeatherServer" if platform == "sensor" else "Miniserver",
        via_device_identifier="serial-a" if platform == "sensor" else None,
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
    assert entity.device_info["via_device"] == ("loxone", "serial-a")
    assert entity.entity_registry_enabled_default is False


def test_engineering_binary_sensor_is_read_only_and_uses_boolean_value():
    entity = LoxoneEngineeringBinarySensor(entity_spec(platform="binary_sensor", value=True))

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
```

- [ ] **Step 2: Run platform tests and confirm binary engineering support is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_platforms.py tests\test_engineering_entities.py -v`

Expected: tests fail because `LoxoneEngineeringBinarySensor` and platform-neutral setup helpers are missing.

- [ ] **Step 3: Refactor sensor setup to consume platform-neutral specs**

Keep one `prepared_entities: dict[str, LoxoneEngineeringSensor]` per config entry. Filter every spec whose UUID is already exposed by normal LoxAPP3 discovery. Construct device info only from the resolved spec:

```python
device_info = {
    "identifiers": {(DOMAIN, spec.owner_identifier)},
    "name": spec.owner_name,
    "manufacturer": "Loxone",
    "model": spec.owner_model,
    "suggested_area": spec.room,
}
if spec.via_device_identifier is not None:
    device_info["via_device"] = (DOMAIN, spec.via_device_identifier)
self._attr_device_info = DeviceInfo(**device_info)
```

Set `_attr_entity_registry_enabled_default = spec.enabled_by_default`, `_attr_available = spec.available`, and expose only technical fields (`uuid`, `io_name`, `loxone_type`, config version, binding method) as extra attributes. Do not expose raw XML attributes, arbitrary runtime text, full paths containing access labels, or endpoint URLs.

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

- [ ] **Step 5: Run platform and existing sensor tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_platforms.py tests\test_engineering_entities.py tests\test_sensor_matching.py -v`

Expected: all selected tests pass and no writable method exists on engineering entities.

- [ ] **Step 6: Commit read-only platform exposure**

```powershell
git add custom_components/loxone/sensor.py custom_components/loxone/binary_sensor.py custom_components/loxone/engineering_entities.py tests/test_engineering_entities.py tests/test_engineering_platforms.py
git commit -m "feat: expose resolved read-only channels"
```

### Task 7: Restore, debounce, and automatically refresh engineering data

**Files:**
- Modify: `custom_components/loxone/coordinator.py:32-141`
- Modify: `custom_components/loxone/__init__.py:288-430`
- Modify: `custom_components/loxone/button.py:59-110`
- Test: `tests/test_engineering_coordinator.py`
- Test: `tests/test_engineering_snapshot.py`

**Interfaces:**
- Consumes: snapshot store, topology resolver, capability resolver, registry sync, runtime probe, and LoxAPP3 `lastModified`.
- Produces: `extract_loxapp_last_modified(lox_config)`, `LoxoneCoordinator._async_download_engineering_inventory()`, `async_restore_engineering_snapshot()`, `async_schedule_engineering_refresh()`, `async_refresh_engineering_inventory(force=False)`, and `engineering_snapshot` state.
- Guarantees: unchanged revisions do not download FTPS; changed revisions queue one debounced refresh; manual refresh bypasses comparison; failed refresh leaves memory/storage/registries on the last good snapshot.

- [ ] **Step 1: Write failing restore, debounce, unchanged, manual, and failure tests**

```python
from unittest.mock import AsyncMock

from tests.engineering_fixtures import inventory_of, make_snapshot, provider_inventory


def test_last_modified_accepts_only_scalar_revision_values():
    assert extract_loxapp_last_modified({"lastModified": "revision-7"}) == "revision-7"
    assert extract_loxapp_last_modified({"lastModified": 7}) == "7"
    assert extract_loxapp_last_modified({"lastModified": {"name": "unsafe"}}) is None


async def test_restore_registers_cached_devices_before_network_refresh(coordinator, stores, registries):
    stores.snapshot = make_snapshot()
    await coordinator.async_restore_engineering_snapshot()

    assert coordinator.engineering_snapshot == stores.snapshot
    assert registries.device("serial-a:nfc") is not None
    assert coordinator.engineering_runtime is None


async def test_unchanged_revision_does_not_download(coordinator):
    coordinator.engineering_snapshot = make_snapshot(last_modified="revision-7")
    coordinator.miniserver.lox_config.json["lastModified"] = "revision-7"
    coordinator._async_download_engineering_inventory = AsyncMock()

    assert await coordinator.async_refresh_engineering_inventory(force=False) is None
    coordinator._async_download_engineering_inventory.assert_not_awaited()


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
```

- [ ] **Step 2: Run coordinator tests and confirm automatic orchestration is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_coordinator.py tests\test_engineering_snapshot.py -v`

Expected: tests fail on missing restore/scheduling/snapshot behavior.

- [ ] **Step 3: Implement last-modified comparison and one refresh transaction**

`async_refresh_engineering_inventory(force=False)` must execute in this order:

1. Read and normalize current LoxAPP3 `lastModified`.
2. Return `None` without FTPS when `force` is false and the revision equals the stored revision.
3. Download and parse into a local candidate without replacing coordinator fields.
4. Reject an incomplete candidate.
5. Probe runtime using existing bounded GET-only probes.
6. Build source context, topology, capability rows, and candidate snapshot.
7. Validate and store the sanitized candidate snapshot.
8. Synchronize devices and registry metadata.
9. Swap `engineering_inventory`, `engineering_runtime`, and `engineering_snapshot` together.
10. Send the config-entry-scoped dispatcher signal.
11. Compute warnings and run stale audit only after the successful swap.

Do not put passwords, hosts, URLs, arbitrary exception messages, or downloaded XML into notification text. Use a fixed notification ID per config entry so repeat failures replace one notification rather than accumulating.

- [ ] **Step 4: Implement cache restore and a cancellable debounce task**

Add coordinator fields:

```python
self.engineering_snapshot: EngineeringSnapshot | None = None
self._engineering_refresh_task: asyncio.Task[None] | None = None
self._engineering_refresh_lock = asyncio.Lock()
```

`async_restore_engineering_snapshot()` loads the config-entry store, validates that its source entry ID and provider identifier match the connected Miniserver, registers cached devices, and publishes cached unavailable entity specs. `async_schedule_engineering_refresh(delay=5.0)` cancels only the coordinator's previous pending debounce task and schedules one background refresh. `async_cleanup()` cancels and awaits that task before closing the API.

- [ ] **Step 5: Wire startup/reload and manual refresh**

After `MiniServer` is constructed and stored under `hass.data[DOMAIN][entry_id]`, restore the cached snapshot before platform forwarding. After all platforms have subscribed, schedule the automatic revision check. Existing reconnect handling reloads the config entry, so the same path covers reconnects. Change the button to call `async_refresh_engineering_inventory(force=True)` and show only counts/status in attributes and its bounded notification.

- [ ] **Step 6: Run coordinator, button-adjacent, and setup tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_coordinator.py tests\test_engineering_snapshot.py tests\test_engineering_entities.py -v`

Expected: all selected tests pass; unchanged auto checks make zero download calls; manual refresh makes one.

- [ ] **Step 7: Commit automatic refresh orchestration**

```powershell
git add custom_components/loxone/coordinator.py custom_components/loxone/__init__.py custom_components/loxone/button.py tests/test_engineering_coordinator.py tests/test_engineering_snapshot.py
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
- Consumes: `EngineeringChangeSet`, entity/device registries, Home Assistant Searcher, and `EngineeringSnapshot`.
- Produces: `EngineeringEntityImpact`, `find_engineering_change_impacts()`, `async_warn_about_engineering_impacts()`, and a diagnostics-safe `engineering_inventory` tree/table payload.
- Guarantees: affected automations/scripts/scenes/groups are reported but never modified; removed nodes enter existing grace handling; sensitive rows are omitted from public diagnostics.

- [ ] **Step 1: Write failing warning and diagnostics tests**

```python
from custom_components.loxone.engineering_changes import EngineeringChangeSet
from tests.engineering_fixtures import make_snapshot


def test_removed_referenced_entity_creates_one_scoped_warning(impact_fakes):
    count = async_warn_about_engineering_impacts(
        impact_fakes.hass,
        impact_fakes.config_entry,
        EngineeringChangeSet(removed=("removed-channel",)),
        make_snapshot(),
    )

    assert count == 1
    assert impact_fakes.notifications[0].notification_id == "loxone_engineering_impact_entry-a"
    assert "automation.office_button" in impact_fakes.notifications[0].message
    assert impact_fakes.automation_updates == []


def test_unreferenced_metadata_rename_does_not_warn(impact_fakes):
    count = async_warn_about_engineering_impacts(
        impact_fakes.hass,
        impact_fakes.config_entry,
        EngineeringChangeSet(renamed=("renamed-device",)),
        make_snapshot(),
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

Build `impact_fakes` with the same in-memory registry/Searcher pattern already used in `tests/test_config_impact.py`: one entity registry entry with unique ID `removed-channel`, one automation search result `automation.office_button`, a notification recorder, and an `automation_updates` list that remains empty. Patch only `dr.async_get`, `er.async_get`, `entity_sources`, `Searcher`, `persistent_notification.async_create`, and `persistent_notification.async_dismiss`; no Home Assistant service call is permitted in this fixture.

- [ ] **Step 2: Run impact and diagnostics tests and confirm they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_engineering_impacts.py tests\test_config_impact.py -v`

Expected: tests fail because engineering change impacts and sanitized snapshot diagnostics are not wired.

- [ ] **Step 3: Extend impact analysis without changing consumers**

Define:

```python
@dataclass(frozen=True, slots=True)
class EngineeringEntityImpact:
    unique_id: str
    entity_ids: tuple[str, ...]
    change_kind: Literal["removed", "platform_changed"]
    references: Mapping[ItemType, tuple[str, ...]]
```

Look up affected entity registry entries by unchanged engineering unique ID and use `Searcher` for the existing relevant item types. Warn only for removed or incompatible-platform changes with actual references. Names and room moves update presentation/areas but do not create an impact warning unless an existing area-targeted consumer is detected by the current area-impact code. Use one config-entry-scoped fixed notification ID; dismiss it when the latest successful diff has no impacts.

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

Add a test that invokes the coordinator failure path followed by `async_run_registry_maintenance()` and asserts `missing_observations` is unchanged. Add a successful-removal test asserting the observation increments once and respects observation/time/combined mode exactly as configured.

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
rg -n -i "(latitude|longitude|gps|currentuser|username|password|token|remoteurl|localurl|hostaddress|accesscode|private[_ -]?key)" custom_components tests docs
rg -n "(192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)" custom_components tests docs
```

Expected: `diff --check` is empty; every commit uses the approved GitHub identity; source hits are limited to generic field-deny/allowlist logic and synthetic safety assertions; private-address search returns no committed installation data. Inspect every remaining match manually before the final commit.

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

- [ ] **Step 8: Install locally in Home Assistant and validate only read-only behavior**

Use the Home Assistant API/operations path, not browser automation, to back up the currently installed integration, copy the verified `custom_components/loxone` directory, restart/reload Home Assistant, and confirm the reported integration version is `0.9.22.13`. Trigger the manual engineering refresh once and verify:

- the Miniserver has Link and Tree children when present;
- the Tree NFC endpoint exists as a device without exposing tag/code data;
- Air and 1-Wire endpoints use their extension as `via_device`;
- central digital/analog input groups appear only when present;
- Weather Server and System Variables group their prepared entities;
- no relay or analog output changed state during download, probing, registration, or refresh;
- the automatic follow-up with unchanged `lastModified` performs no second FTPS download;
- integration logs contain no new error and no raw sensitive/access value.

Record the actual Home Assistant version and Loxone Miniserver firmware used for this validation in the local test report. Do not add host addresses, serial numbers, user names, project names, coordinates, credentials, or access labels to a commit.

- [ ] **Step 9: Stop at the local delivery boundary**

Report the branch name, commit list, test counts, lint result, Home Assistant version, Loxone firmware version, and any remaining review risk. Do not push or communicate upstream until the user explicitly authorizes the next step.
