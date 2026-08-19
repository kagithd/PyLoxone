"""Read-only runtime binding probes for engineering configuration elements."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import aiohttp

if TYPE_CHECKING:
    from .engineering_config import EngineeringElement, EngineeringInventory

HTTP_OK = 200
MAX_RUNTIME_RESPONSE_BYTES = 64 * 1024
RUNTIME_PROBE_CONCURRENCY = 4
RUNTIME_PROBE_TIMEOUT = 10.0
ERR_RESPONSE_TOO_LARGE = "Runtime response exceeds the safety limit"
ERR_RESPONSE_EMPTY = "Runtime response is empty"
ERR_JSON_LL_MISSING = "Runtime JSON does not contain an LL response"
ERR_XML_DECLARATIONS = "Runtime XML contains forbidden declarations"
ERR_XML_ROOT = "Runtime XML root is not LL"
_NUMBER_WITH_UNIT = re.compile(r"^\s*(?P<number>[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?)\s*(?P<unit>.*?)\s*$")


class EngineeringRuntimeError(ValueError):
    """Raised when a read-only runtime response cannot be handled safely."""


@dataclass(frozen=True, slots=True)
class RuntimeProbeClient:
    """Connection details used for bounded read-only runtime probes."""

    session: aiohttp.ClientSession = field(repr=False)
    base_url: str
    auth: aiohttp.BasicAuth = field(repr=False)
    verify_ssl: bool
    semaphore: asyncio.Semaphore = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeNumericState:
    """One diagnostics-safe numeric state returned by an ``/all`` endpoint."""

    index: int
    state_uuid: str | None
    numeric_value: float
    unit: str | None

    def as_public_dict(self) -> dict[str, Any]:
        """Return the numeric state without its potentially sensitive display name."""
        return {
            "index": self.index,
            "state_uuid": self.state_uuid,
            "numeric_value": self.numeric_value,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    """Parsed response from one read-only Miniserver runtime endpoint."""

    code: int
    control: str | None
    value_kind: str
    numeric_value: float | None
    unit: str | None
    substate_count: int
    numeric_states: tuple[RuntimeNumericState, ...]


@dataclass(frozen=True, slots=True)
class EngineeringRuntimeBinding:
    """Runtime reachability result for one engineering configuration channel."""

    engineering_uuid: str
    io_name: str
    loxone_type: str | None
    title: str | None
    room: str | None
    suggested_platform: str | None
    status: str
    binding_method: str | None = None
    endpoint: str | None = None
    response_code: int | None = None
    response_control: str | None = None
    value_kind: str | None = None
    numeric_value: float | None = None
    unit: str | None = None
    substate_count: int = 0
    numeric_states: tuple[RuntimeNumericState, ...] = ()
    error: str | None = None

    def as_public_dict(self) -> dict[str, Any]:
        """Return diagnostics-safe binding information."""
        return {
            "engineering_uuid": self.engineering_uuid,
            "io_name": self.io_name,
            "loxone_type": self.loxone_type,
            "title": self.title,
            "room": self.room,
            "suggested_platform": self.suggested_platform,
            "status": self.status,
            "binding_method": self.binding_method,
            "endpoint": self.endpoint,
            "response_code": self.response_code,
            "response_control": self.response_control,
            "value_kind": self.value_kind,
            "numeric_value": self.numeric_value,
            "unit": self.unit,
            "substate_count": self.substate_count,
            "numeric_states": [state.as_public_dict() for state in self.numeric_states],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class EngineeringRuntimeInventory:
    """Read-only runtime binding results for one engineering inventory."""

    bindings: tuple[EngineeringRuntimeBinding, ...]

    def summary(self) -> dict[str, Any]:
        """Return compact binding counts for entity attributes and diagnostics."""
        by_status: dict[str, int] = {}
        by_method: dict[str, int] = {}
        for binding in self.bindings:
            by_status[binding.status] = by_status.get(binding.status, 0) + 1
            if binding.binding_method:
                by_method[binding.binding_method] = by_method.get(binding.binding_method, 0) + 1
        return {
            "probed_count": len(self.bindings),
            "bound_count": by_status.get("bound", 0),
            "bindings_by_status": dict(sorted(by_status.items())),
            "bindings_by_method": dict(sorted(by_method.items())),
        }


def _classify_value(value: Any) -> tuple[str, float | None, str | None]:
    """Classify a value without exposing arbitrary text in diagnostics."""
    if value is None or value == "":
        kind, numeric_value, unit = "empty", None, None
    elif isinstance(value, bool):
        kind, numeric_value, unit = "boolean", float(value), None
    elif isinstance(value, (int, float)):
        kind, numeric_value, unit = "number", float(value), None
    elif isinstance(value, str):
        match = _NUMBER_WITH_UNIT.fullmatch(value)
        if match:
            kind = "number"
            numeric_value = float(match.group("number").replace(",", "."))
            unit = match.group("unit") or None
        else:
            kind, numeric_value, unit = "text", None, None
    elif isinstance(value, (dict, list)):
        kind, numeric_value, unit = "structured", None, None
    else:
        kind, numeric_value, unit = "unknown", None, None
    return kind, numeric_value, unit


def _numeric_state(*, index: int, state_uuid: str | None, value: Any) -> RuntimeNumericState | None:
    """Return a safe numeric state or omit nonnumeric content."""
    _kind, numeric_value, unit = _classify_value(value)
    if numeric_value is None:
        return None
    return RuntimeNumericState(
        index=index,
        state_uuid=state_uuid,
        numeric_value=numeric_value,
        unit=unit,
    )


def _xml_numeric_states(root: ET.Element) -> tuple[RuntimeNumericState, ...]:
    """Extract numeric ``vN`` values and numeric child states from an LL response."""
    states: list[RuntimeNumericState] = []
    seen: set[tuple[int, str | None, float]] = set()

    for key, value in root.attrib.items():
        match = re.fullmatch(r"v(?P<index>\d+)", key, flags=re.IGNORECASE)
        if not match:
            continue
        index = int(match.group("index"))
        state = _numeric_state(index=index, state_uuid=root.attrib.get(f"u{index}"), value=value)
        if state is not None:
            marker = (state.index, state.state_uuid, state.numeric_value)
            seen.add(marker)
            states.append(state)

    for fallback_index, child in enumerate(root, start=1):
        raw_index = child.attrib.get("nr") or child.attrib.get("index")
        try:
            index = int(raw_index) if raw_index is not None else fallback_index
        except ValueError:
            index = fallback_index
        state = _numeric_state(
            index=index,
            state_uuid=child.attrib.get("uuid") or child.attrib.get("u") or child.attrib.get("U"),
            value=child.attrib.get("value", child.attrib.get("v")),
        )
        if state is not None:
            marker = (state.index, state.state_uuid, state.numeric_value)
            if marker not in seen:
                seen.add(marker)
                states.append(state)
    return tuple(states)


def _parse_runtime_response(payload: bytes) -> RuntimeResponse:
    """Parse the JSON or XML response returned by a Loxone read endpoint."""
    if len(payload) > MAX_RUNTIME_RESPONSE_BYTES:
        raise EngineeringRuntimeError(ERR_RESPONSE_TOO_LARGE)
    stripped = payload.lstrip()
    if not stripped:
        raise EngineeringRuntimeError(ERR_RESPONSE_EMPTY)

    if stripped.startswith(b"{"):
        parsed = json.loads(payload)
        ll = parsed.get("LL")
        if not isinstance(ll, dict):
            raise EngineeringRuntimeError(ERR_JSON_LL_MISSING)
        code = int(ll.get("Code", ll.get("code", 0)))
        value = ll.get("value")
        value_kind, numeric_value, unit = _classify_value(value)
        substates = value if isinstance(value, dict) else ll.get("data")
        return RuntimeResponse(
            code=code,
            control=ll.get("control"),
            value_kind=value_kind,
            numeric_value=numeric_value,
            unit=unit,
            substate_count=len(substates) if isinstance(substates, dict) else 0,
            numeric_states=(),
        )

    lowered_prefix = stripped[:4096].lower()
    if b"<!doctype" in lowered_prefix or b"<!entity" in lowered_prefix:
        raise EngineeringRuntimeError(ERR_XML_DECLARATIONS)
    root = ET.fromstring(payload)  # noqa: S314
    if root.tag != "LL":
        raise EngineeringRuntimeError(ERR_XML_ROOT)
    code = int(root.attrib.get("Code", root.attrib.get("code", "0")))
    value_kind, numeric_value, unit = _classify_value(root.attrib.get("value"))
    numeric_states = _xml_numeric_states(root)
    indexed_substates = {
        key[1:] for key in root.attrib if len(key) > 1 and key[0].lower() in {"n", "u", "v"} and key[1:].isdigit()
    }
    return RuntimeResponse(
        code=code,
        control=root.attrib.get("control"),
        value_kind=value_kind,
        numeric_value=numeric_value,
        unit=unit,
        substate_count=max(len(indexed_substates), sum(1 for _ in root.iter()) - 1),
        numeric_states=numeric_states,
    )


def _probe_targets(element: EngineeringElement, *, unique_io_name: bool) -> tuple[tuple[str, str], ...]:
    """Build ordered read-only probe targets, preferring stable UUID addressing."""
    targets: list[tuple[str, str]] = []
    if element.uuid:
        identifier = quote(element.uuid, safe="")
        targets.extend(
            (
                ("uuid_all", f"/dev/sps/io/{identifier}/all"),
                ("uuid_state", f"/dev/sps/io/{identifier}/state"),
            )
        )
    if element.io_name and unique_io_name:
        targets.append(("io_name_state", f"/dev/sps/io/{quote(element.io_name, safe='')}/state"))
    return tuple(targets)


async def _probe_element(
    client: RuntimeProbeClient,
    element: EngineeringElement,
    *,
    unique_io_name: bool,
) -> EngineeringRuntimeBinding:
    """Probe one engineering channel without issuing a state-changing command."""
    last_code: int | None = None
    last_error: str | None = None
    targets = _probe_targets(element, unique_io_name=unique_io_name)
    if not targets:
        return EngineeringRuntimeBinding(
            engineering_uuid=element.uuid or "",
            io_name=element.io_name or "",
            loxone_type=element.loxone_type,
            title=element.title,
            room=element.room,
            suggested_platform=element.suggested_platform,
            status="ambiguous_io_name",
            error="No safe unique runtime address is available",
        )

    async with client.semaphore:
        for method, endpoint in targets:
            try:
                async with client.session.get(
                    f"{client.base_url.rstrip('/')}{endpoint}",
                    auth=client.auth,
                    ssl=client.verify_ssl,
                    timeout=aiohttp.ClientTimeout(total=RUNTIME_PROBE_TIMEOUT),
                ) as response:
                    if response.status != HTTP_OK:
                        last_code = response.status
                        continue
                    payload = await response.content.read(MAX_RUNTIME_RESPONSE_BYTES + 1)
                parsed = _parse_runtime_response(payload)
                last_code = parsed.code
                if parsed.code != HTTP_OK:
                    continue
                return EngineeringRuntimeBinding(
                    engineering_uuid=element.uuid or "",
                    io_name=element.io_name or "",
                    loxone_type=element.loxone_type,
                    title=element.title,
                    room=element.room,
                    suggested_platform=element.suggested_platform,
                    status="bound",
                    binding_method=method,
                    endpoint=endpoint,
                    response_code=parsed.code,
                    response_control=parsed.control,
                    value_kind=parsed.value_kind,
                    numeric_value=parsed.numeric_value,
                    unit=parsed.unit,
                    substate_count=parsed.substate_count,
                    numeric_states=parsed.numeric_states,
                )
            except (aiohttp.ClientError, TimeoutError) as err:
                last_error = type(err).__name__
            except (ET.ParseError, json.JSONDecodeError, TypeError, ValueError) as err:
                last_error = str(err)

    status = "not_found" if last_code is not None else "error"
    if not unique_io_name and element.io_name:
        status = "ambiguous_io_name"
        last_error = "UUID endpoints failed and the IO name is not globally unique"
    return EngineeringRuntimeBinding(
        engineering_uuid=element.uuid or "",
        io_name=element.io_name or "",
        loxone_type=element.loxone_type,
        title=element.title,
        room=element.room,
        suggested_platform=element.suggested_platform,
        status=status,
        response_code=last_code,
        error=last_error,
    )


async def async_probe_engineering_runtime(
    inventory: EngineeringInventory,
    *,
    client: RuntimeProbeClient,
) -> EngineeringRuntimeInventory:
    """Probe all direct engineering channels through read-only Miniserver endpoints."""
    elements = tuple(
        element
        for element in inventory.candidates
        if element.uuid and element.io_name and element.suggested_platform is not None
    )
    io_name_counts: dict[str, int] = {}
    for element in elements:
        key = element.io_name.casefold()
        io_name_counts[key] = io_name_counts.get(key, 0) + 1

    bindings = await asyncio.gather(
        *(
            _probe_element(
                client,
                element,
                unique_io_name=io_name_counts[element.io_name.casefold()] == 1,
            )
            for element in elements
        )
    )
    return EngineeringRuntimeInventory(bindings=tuple(bindings))
