"""Administrator-only WebSocket and panel boundary for engineering hierarchy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .engineering_hierarchy import build_engineering_hierarchy, hierarchy_to_dict
from .engineering_topology import NodeKind

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .engineering_snapshot import EngineeringSnapshot

_DATA_REGISTRATION = f"{DOMAIN}_engineering_view_registration"
_PANEL_PATH = "pyloxone-hierarchy"
_STATIC_URL = "/pyloxone_static"
_MODULE_URL = f"{_STATIC_URL}/pyloxone-hierarchy.js"
_FRONTEND_PATH = Path(__file__).parent / "frontend"
_MAX_ENTRY_TITLE_LENGTH = 80


@dataclass(slots=True)
class _RegistrationState:
    """Process-lifetime registration and reversible panel state."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    server_registered: bool = False
    panel_registered: bool = False
    active_entry_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _EntityLink:
    """Provider-checked HA navigation and current status."""

    entity_id: str
    status: str


def _registration_state(hass: HomeAssistant) -> _RegistrationState:
    state = hass.data.get(_DATA_REGISTRATION)
    if isinstance(state, _RegistrationState):
        return state
    state = _RegistrationState()
    hass.data[_DATA_REGISTRATION] = state
    return state


def _bounded_entry_title(entry: ConfigEntry) -> str:
    """Return one bounded single-line display label."""
    title = getattr(entry, "title", None)
    if not isinstance(title, str):
        return "PyLoxone"
    normalized = " ".join(title.split())
    return (normalized or "PyLoxone")[:_MAX_ENTRY_TITLE_LENGTH]


def _loaded_entries(hass: HomeAssistant) -> tuple[ConfigEntry, ...]:
    """Return successfully loaded entries with their current coordinators."""
    coordinators = hass.data.get(DOMAIN, {})
    if not isinstance(coordinators, dict):
        return ()
    entries = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        coordinator = coordinators.get(entry.entry_id)
        if getattr(getattr(coordinator, "config_entry", None), "entry_id", None) == entry.entry_id:
            entries.append(entry)
    return tuple(sorted(entries, key=lambda item: (_bounded_entry_title(item).casefold(), item.entry_id)))


def _current_coordinator(
    hass: HomeAssistant,
    entry_id: str,
) -> tuple[ConfigEntry, Any] | None:
    """Resolve an exact currently loaded entry/coordinator pair."""
    entry = next((item for item in _loaded_entries(hass) if item.entry_id == entry_id), None)
    if entry is None:
        return None
    coordinator = hass.data[DOMAIN].get(entry_id)
    if getattr(getattr(coordinator, "config_entry", None), "entry_id", None) != entry_id:
        return None
    return entry, coordinator


def _snapshot_matches_current_provider(
    snapshot: EngineeringSnapshot,
    entry_id: str,
    coordinator: Any,
) -> bool:
    """Recheck both config-entry and currently connected provider identity."""
    miniserver = getattr(coordinator, "miniserver", None)
    serial = getattr(miniserver, "serial", None)
    current_provider = serial or entry_id
    return snapshot.source.entry_id == entry_id and snapshot.source.provider_identifier == current_provider


def _snapshot_owner_identifiers(snapshot: EngineeringSnapshot) -> dict[str, str]:
    """Map visible stable function UUIDs to resolved owner identifiers."""
    return {
        row.node.element.uuid: row.node.device_identifier
        for row in snapshot.rows
        if row.node.kind is NodeKind.CHANNEL
        and not row.node.sensitive
        and row.node.element.uuid is not None
        and row.node.device_identifier is not None
    }


def _provider_checked_devices(
    hass: HomeAssistant,
    entry_id: str,
    snapshot: EngineeringSnapshot,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return only devices whose exact identifier and current parent match."""
    expected_via_by_identifier = {
        node.device_identifier: node.via_device_identifier
        for node in snapshot.nodes
        if node.kind is not NodeKind.CHANNEL and not node.sensitive and node.device_identifier is not None
    }
    registry = dr.async_get(hass)
    owned_devices = tuple(
        device
        for device in dr.async_entries_for_config_entry(registry, entry_id)
        if getattr(device, "config_entry_id", None) == entry_id
    )
    owned_device_by_id = {
        device_id: device
        for device in owned_devices
        if isinstance(device_id := getattr(device, "id", None), str) and device_id
    }
    device_id_by_identifier: dict[str, str] = {}
    device_by_id: dict[str, Any] = {}
    for device in owned_devices:
        identifiers = getattr(device, "identifiers", ())
        matches = sorted(
            identifier
            for domain, identifier in identifiers
            if domain == DOMAIN and identifier in expected_via_by_identifier
        )
        if len(matches) != 1:
            continue
        device_id = getattr(device, "id", None)
        if not isinstance(device_id, str) or not device_id:
            continue
        identifier = matches[0]
        expected_via_identifier = expected_via_by_identifier[identifier]
        current_via_id = getattr(device, "via_device_id", None)
        if expected_via_identifier is None:
            if current_via_id is not None:
                continue
        else:
            current_parent = owned_device_by_id.get(current_via_id)
            if current_parent is None or (DOMAIN, expected_via_identifier) not in getattr(
                current_parent, "identifiers", ()
            ):
                continue
        device_id_by_identifier[identifier] = device_id
        device_by_id[device_id] = device
    return device_id_by_identifier, device_by_id


def _provider_checked_entity_links(
    hass: HomeAssistant,
    entry_id: str,
    snapshot: EngineeringSnapshot,
    devices_by_id: dict[str, Any],
) -> dict[str, _EntityLink]:
    """Read current entity registry/state without trusting names or capabilities."""
    owner_by_unique_id = _snapshot_owner_identifiers(snapshot)
    registry = er.async_get(hass)
    links: dict[str, _EntityLink] = {}
    entries = sorted(
        er.async_entries_for_config_entry(registry, entry_id),
        key=lambda item: getattr(item, "entity_id", ""),
    )
    for entity in entries:
        unique_id = getattr(entity, "unique_id", None)
        owner_identifier = owner_by_unique_id.get(unique_id)
        device = devices_by_id.get(getattr(entity, "device_id", None))
        if (
            owner_identifier is None
            or getattr(entity, "platform", None) != DOMAIN
            or getattr(entity, "config_entry_id", None) != entry_id
            or device is None
            or (DOMAIN, owner_identifier) not in getattr(device, "identifiers", ())
        ):
            continue
        entity_id = getattr(entity, "entity_id", None)
        if not isinstance(entity_id, str) or "." not in entity_id:
            continue
        if getattr(entity, "disabled_by", None) is not None:
            status = "disabled"
        else:
            state = hass.states.get(entity_id)
            status = (
                "unavailable"
                if state is None or getattr(state, "state", None) in {STATE_UNKNOWN, STATE_UNAVAILABLE}
                else "active_entity"
            )
        links.setdefault(unique_id, _EntityLink(entity_id, status))
    return links


def _enrich_node(
    node: dict[str, Any],
    device_ids: dict[str, str],
    entity_links: dict[str, _EntityLink],
    counts: dict[str, int],
) -> None:
    """Attach provider-checked navigation and replace only HA-derived statuses."""
    node["device_id"] = device_ids.get(node["identifier"])
    for function in node["functions"]:
        if link := entity_links.get(function["key"]):
            function["entity_id"] = link.entity_id
            function["status"] = link.status
        counts[function["status"]] += 1
    for child in (*node.get("sections", ()), *node["children"]):
        _enrich_node(child, device_ids, entity_links, counts)


def _hierarchy_payload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    snapshot: EngineeringSnapshot,
) -> dict[str, object]:
    """Build the Task-1 projection plus bounded HA navigation/freshness fields."""
    device_ids, devices_by_id = _provider_checked_devices(hass, entry.entry_id, snapshot)
    entity_links = _provider_checked_entity_links(
        hass,
        entry.entry_id,
        snapshot,
        devices_by_id,
    )
    hierarchy = build_engineering_hierarchy(
        snapshot,
        entity_ids_by_unique_id={key: link.entity_id for key, link in entity_links.items()},
    )
    payload = hierarchy_to_dict(hierarchy)
    counts = {
        "active_entity": 0,
        "disabled": 0,
        "unavailable": 0,
        "prepared": 0,
        "inventory_only": 0,
        "unsupported": 0,
    }
    root = cast("dict[str, Any]", payload["root"])
    _enrich_node(root, device_ids, entity_links, counts)
    summary = cast("dict[str, int]", payload["summary"])
    payload["summary"] = {**counts, "protected": summary["protected"]}
    age = max(0, int((dt_util.utcnow() - snapshot.captured_at).total_seconds()))
    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": _bounded_entry_title(entry),
        },
        "snapshot": {
            "generation_id": snapshot.generation_id,
            "captured_at": snapshot.captured_at.isoformat(),
            "age_seconds": age,
        },
        **payload,
    }


@websocket_api.websocket_command({"type": "loxone/engineering_entries"})
@websocket_api.require_admin
@callback
def websocket_engineering_entries(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return only loaded Loxone entries with bounded selection metadata."""
    entries = [{"entry_id": entry.entry_id, "title": _bounded_entry_title(entry)} for entry in _loaded_entries(hass)]
    connection.send_result(
        msg["id"],
        {"entries": entries, "selection_required": len(entries) > 1},
    )


@websocket_api.websocket_command(
    {
        "type": "loxone/engineering_hierarchy",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.require_admin
@callback
def websocket_engineering_hierarchy(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return one current provider-scoped privacy-safe hierarchy."""
    current = _current_coordinator(hass, msg["entry_id"])
    if current is None:
        connection.send_error(msg["id"], "entry_not_loaded", "The selected PyLoxone entry is not loaded.")
        return
    entry, coordinator = current
    snapshot = getattr(coordinator, "engineering_snapshot", None)
    if snapshot is None:
        connection.send_error(msg["id"], "snapshot_unavailable", "No engineering snapshot is available.")
        return
    if not _snapshot_matches_current_provider(snapshot, entry.entry_id, coordinator):
        connection.send_error(
            msg["id"],
            "provider_mismatch",
            "The engineering snapshot does not match the current provider.",
        )
        return
    connection.send_result(msg["id"], _hierarchy_payload(hass, entry, snapshot))


async def async_prepare_engineering_view(hass: HomeAssistant) -> None:
    """Register permanent server resources exactly once for this HA process."""
    state = _registration_state(hass)
    async with state.lock:
        if state.server_registered:
            return
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_STATIC_URL, str(_FRONTEND_PATH), cache_headers=False)]
        )
        websocket_api.async_register_command(hass, websocket_engineering_entries)
        websocket_api.async_register_command(hass, websocket_engineering_hierarchy)
        state.server_registered = True


async def async_activate_engineering_view(hass: HomeAssistant, entry_id: str) -> None:
    """Expose the admin panel after one entry finishes setup."""
    await async_prepare_engineering_view(hass)
    state = _registration_state(hass)
    async with state.lock:
        if entry_id in state.active_entry_ids:
            return
        if not state.panel_registered:
            await panel_custom.async_register_panel(
                hass=hass,
                frontend_url_path=_PANEL_PATH,
                webcomponent_name="pyloxone-hierarchy",
                sidebar_title="PyLoxone hierarchy",
                sidebar_icon="mdi:file-tree-outline",
                module_url=_MODULE_URL,
                require_admin=True,
            )
            state.panel_registered = True
        state.active_entry_ids.add(entry_id)


async def async_deactivate_engineering_view(hass: HomeAssistant, entry_id: str) -> None:
    """Remove only the panel after the last successfully set-up entry unloads."""
    state = _registration_state(hass)
    async with state.lock:
        state.active_entry_ids.discard(entry_id)
        if state.active_entry_ids or not state.panel_registered:
            return
        frontend.async_remove_panel(hass, _PANEL_PATH, warn_if_unknown=False)
        state.panel_registered = False
