"""Boundary and lifecycle tests for the read-only engineering hierarchy API."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import Unauthorized

from custom_components.loxone.const import DOMAIN
from custom_components.loxone.engineering_config import InstallationPlacement
from custom_components.loxone.engineering_runtime import EngineeringRuntimeInventory
from custom_components.loxone.engineering_websocket import (
    async_activate_engineering_view,
    async_deactivate_engineering_view,
    async_prepare_engineering_view,
    websocket_engineering_entries,
    websocket_engineering_hierarchy,
)
from tests.engineering_fixtures import element, inventory_of, make_snapshot, numeric_binding


class _Connection:
    def __init__(self, *, admin: bool = True) -> None:
        self.user = SimpleNamespace(is_admin=admin)
        self.results: list[tuple[int, object]] = []
        self.errors: list[tuple[int, str, str]] = []

    def send_result(self, msg_id: int, result: object = None) -> None:
        self.results.append((msg_id, result))

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors.append((msg_id, code, message))


class _ConfigEntries:
    def __init__(self, entries=()) -> None:
        self.loaded = list(entries)

    def async_loaded_entries(self, domain: str):
        assert domain == DOMAIN
        return list(self.loaded)


class _States:
    def __init__(self, values=None) -> None:
        self.values = values or {}

    def get(self, entity_id: str):
        return self.values.get(entity_id)


def _entry(entry_id: str, title: str = "Synthetic Controller"):
    return SimpleNamespace(entry_id=entry_id, domain=DOMAIN, title=title)


def _coordinator(entry, *, snapshot=None, provider: str = "serial-a"):
    return SimpleNamespace(
        config_entry=entry,
        engineering_snapshot=snapshot,
        miniserver=SimpleNamespace(serial=provider),
    )


def _hass(*entries, coordinators=None, states=None):
    return SimpleNamespace(
        data={DOMAIN: coordinators or {}},
        config_entries=_ConfigEntries(entries),
        states=_States(states),
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("unknown", "entry_not_loaded"),
        ("unloaded", "entry_not_loaded"),
        ("absent_snapshot", "snapshot_unavailable"),
        ("provider_mismatch", "provider_mismatch"),
    ],
)
def test_hierarchy_rejection_matrix_is_bounded_and_fail_closed(case, expected):
    """A missing current lifecycle/provider boundary must never expose a snapshot."""
    entry = _entry("entry-a")
    snapshot = make_snapshot()
    coordinator = _coordinator(entry, snapshot=snapshot)
    entries = [entry]
    coordinators = {entry.entry_id: coordinator}
    requested = entry.entry_id
    if case == "unknown":
        requested = "entry-unknown"
    elif case == "unloaded":
        entries = []
    elif case == "absent_snapshot":
        coordinator.engineering_snapshot = None
    elif case == "provider_mismatch":
        coordinator.miniserver.serial = "serial-current"
    hass = _hass(*entries, coordinators=coordinators)
    connection = _Connection()

    websocket_engineering_hierarchy(
        hass,
        connection,
        {"id": 1, "type": "loxone/engineering_hierarchy", "entry_id": requested},
    )

    assert connection.results == []
    assert connection.errors == [(1, expected, connection.errors[0][2])]
    assert "serial" not in connection.errors[0][2].casefold()


@pytest.mark.parametrize("handler", [websocket_engineering_entries, websocket_engineering_hierarchy])
def test_websocket_boundaries_reject_non_admin(handler):
    """Removing either administrator decorator must fail this boundary test."""
    with pytest.raises(Unauthorized):
        handler(
            _hass(),
            _Connection(admin=False),
            {"id": 1, "type": "ignored", "entry_id": "entry-a"},
        )


@pytest.mark.parametrize("with_placement", [False, True])
def test_entries_and_hierarchy_return_scoped_enrichment_and_bounded_schema(monkeypatch, with_placement):
    """Wrong owner/status logic or an added disclosure field must fail together."""
    from custom_components.loxone import engineering_websocket as module

    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Provider", room=None),
            replace(
                element("device", "TreeDevice", parent_uuid="ms", title="Endpoint"),
                placement=InstallationPlacement(switchboard="Cabinet A", row=2) if with_placement else None,
            ),
            element("active", "VoltageIn", parent_uuid="device", title="Active", io_name="AI1"),
            element("disabled", "VoltageIn", parent_uuid="device", title="Disabled", io_name="AI2"),
            element("unavailable", "VoltageIn", parent_uuid="device", title="Unavailable", io_name="AI3"),
            element("foreign", "VoltageIn", parent_uuid="device", title="Foreign", io_name="AI4"),
        ),
        runtime=EngineeringRuntimeInventory(
            (
                numeric_binding("active", 1.0),
                numeric_binding("disabled", 2.0),
                numeric_binding("unavailable", 3.0),
                numeric_binding("foreign", 4.0),
            )
        ),
    )
    first = _entry("entry-a", "Primary synthetic controller")
    second = _entry("entry-b", "Secondary synthetic controller")
    coordinator = _coordinator(first, snapshot=snapshot)
    hass = _hass(
        first,
        second,
        coordinators={first.entry_id: coordinator},
        states={
            "sensor.active": SimpleNamespace(state="1"),
            "sensor.unavailable": SimpleNamespace(state=STATE_UNAVAILABLE),
        },
    )
    devices = [
        SimpleNamespace(
            id="device-provider",
            config_entry_id="entry-a",
            identifiers={(DOMAIN, "serial-a")},
            via_device_id=None,
        ),
        SimpleNamespace(
            id="device-endpoint",
            config_entry_id="entry-a",
            identifiers={(DOMAIN, "serial-a:device")},
            via_device_id="device-provider",
        ),
        SimpleNamespace(
            id="device-foreign",
            config_entry_id="entry-a",
            identifiers={(DOMAIN, "serial-other:device")},
            via_device_id=None,
        ),
    ]
    entities = [
        SimpleNamespace(
            entity_id="sensor.active",
            unique_id="active",
            platform=DOMAIN,
            config_entry_id="entry-a",
            device_id="device-endpoint",
            disabled_by=None,
        ),
        SimpleNamespace(
            entity_id="sensor.disabled",
            unique_id="disabled",
            platform=DOMAIN,
            config_entry_id="entry-a",
            device_id="device-endpoint",
            disabled_by="user",
        ),
        SimpleNamespace(
            entity_id="sensor.unavailable",
            unique_id="unavailable",
            platform=DOMAIN,
            config_entry_id="entry-a",
            device_id="device-endpoint",
            disabled_by=None,
        ),
        SimpleNamespace(
            entity_id="sensor.foreign",
            unique_id="foreign",
            platform=DOMAIN,
            config_entry_id="entry-a",
            device_id="device-foreign",
            disabled_by=None,
        ),
    ]
    monkeypatch.setattr(module.dr, "async_get", lambda _hass: object())
    monkeypatch.setattr(module.dr, "async_entries_for_config_entry", lambda _registry, _entry_id: devices)
    monkeypatch.setattr(module.er, "async_get", lambda _hass: object())
    monkeypatch.setattr(module.er, "async_entries_for_config_entry", lambda _registry, _entry_id: entities)
    monkeypatch.setattr(module.dt_util, "utcnow", lambda: datetime(2026, 9, 14, 12, tzinfo=UTC))

    entries_connection = _Connection()
    websocket_engineering_entries(
        hass,
        entries_connection,
        {"id": 1, "type": "loxone/engineering_entries"},
    )
    assert entries_connection.results == [
        (
            1,
            {
                "entries": [
                    {"entry_id": "entry-a", "title": "Primary synthetic controller"},
                ],
                "selection_required": False,
            },
        )
    ]

    # A loaded config entry without a current coordinator is omitted, not advertised.
    hass.data[DOMAIN][second.entry_id] = _coordinator(second, snapshot=make_snapshot(), provider="serial-a")
    websocket_engineering_entries(
        hass,
        entries_connection,
        {"id": 2, "type": "loxone/engineering_entries"},
    )
    assert entries_connection.results[1][1] == {
        "entries": [
            {"entry_id": "entry-a", "title": "Primary synthetic controller"},
            {"entry_id": "entry-b", "title": "Secondary synthetic controller"},
        ],
        "selection_required": True,
    }

    connection = _Connection()
    websocket_engineering_hierarchy(
        hass,
        connection,
        {"id": 3, "type": "loxone/engineering_hierarchy", "entry_id": "entry-a"},
    )
    payload = connection.results[0][1]
    assert set(payload) == {"entry", "snapshot", "root", "summary"}
    assert payload["entry"] == {"entry_id": "entry-a", "title": "Primary synthetic controller"}
    assert payload["snapshot"] == {
        "generation_id": snapshot.generation_id,
        "captured_at": "2026-09-13T12:00:00+00:00",
        "age_seconds": 86400,
    }
    assert set(payload["summary"]) == {
        "active_entity",
        "disabled",
        "unavailable",
        "prepared",
        "inventory_only",
        "unsupported",
        "protected",
    }
    assert payload["summary"] == {
        "active_entity": 1,
        "disabled": 1,
        "unavailable": 1,
        "prepared": 1,
        "inventory_only": 0,
        "unsupported": 0,
        "protected": 0,
    }
    assert payload["root"]["device_id"] == "device-provider"
    endpoint = payload["root"]["children"][0]
    assert endpoint["device_id"] == "device-endpoint"
    functions = {item["key"]: item for item in endpoint["functions"]}
    assert {key: (item["status"], item["entity_id"]) for key, item in functions.items()} == {
        "active": ("active_entity", "sensor.active"),
        "disabled": ("disabled", "sensor.disabled"),
        "foreign": ("prepared", None),
        "unavailable": ("unavailable", "sensor.unavailable"),
    }
    if with_placement:
        assert endpoint["placement"] == {"switchboard": "Cabinet A", "row": 2}
    assert set(endpoint) == {
        "identifier",
        "role",
        "label",
        "technical_type",
        "bus_kind",
        "functions",
        "children",
        "protected_count",
        "device_id",
    } | ({"placement"} if with_placement else set())
    assert all(
        set(item) == {"key", "label", "technical_type", "status", "reason", "entity_id"} for item in functions.values()
    )


def test_registry_via_mismatch_removes_only_navigation_enrichment(monkeypatch):
    """A moved registry device must stay visible without device/entity navigation."""
    from custom_components.loxone import engineering_websocket as module

    snapshot = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Provider", room=None),
            element("device", "TreeDevice", parent_uuid="ms", title="Endpoint"),
            element("channel", "VoltageIn", parent_uuid="device", title="Channel", io_name="AI1"),
        ),
        runtime=EngineeringRuntimeInventory((numeric_binding("channel", 1.0),)),
    )
    entry = _entry("entry-a")
    hass = _hass(
        entry,
        coordinators={entry.entry_id: _coordinator(entry, snapshot=snapshot)},
        states={"sensor.channel": SimpleNamespace(state="1")},
    )
    devices = [
        SimpleNamespace(
            id="device-provider",
            config_entry_id="entry-a",
            identifiers={(DOMAIN, "serial-a")},
            via_device_id=None,
        ),
        SimpleNamespace(
            id="device-wrong-parent",
            config_entry_id="entry-a",
            identifiers={(DOMAIN, "serial-a:unrelated")},
            via_device_id=None,
        ),
        SimpleNamespace(
            id="device-moved-endpoint",
            config_entry_id="entry-a",
            identifiers={(DOMAIN, "serial-a:device")},
            via_device_id="device-wrong-parent",
        ),
    ]
    entities = [
        SimpleNamespace(
            entity_id="sensor.channel",
            unique_id="channel",
            platform=DOMAIN,
            config_entry_id="entry-a",
            device_id="device-moved-endpoint",
            disabled_by=None,
        )
    ]
    monkeypatch.setattr(module.dr, "async_get", lambda _hass: object())
    monkeypatch.setattr(module.dr, "async_entries_for_config_entry", lambda _registry, _entry_id: devices)
    monkeypatch.setattr(module.er, "async_get", lambda _hass: object())
    monkeypatch.setattr(module.er, "async_entries_for_config_entry", lambda _registry, _entry_id: entities)

    connection = _Connection()
    websocket_engineering_hierarchy(
        hass,
        connection,
        {"id": 1, "type": "loxone/engineering_hierarchy", "entry_id": "entry-a"},
    )

    payload = connection.results[0][1]
    assert payload["root"]["device_id"] == "device-provider"
    endpoint = payload["root"]["children"][0]
    assert endpoint["identifier"] == "serial-a:device"
    assert endpoint["device_id"] is None
    assert [(item["status"], item["entity_id"]) for item in endpoint["functions"]] == [("prepared", None)]
    assert payload["summary"]["active_entity"] == 0
    assert payload["summary"]["prepared"] == 1


def test_registration_survives_failure_and_panel_tracks_first_last_entry(monkeypatch):
    """Registration races, a failed attempt, last unload, and reload stay idempotent."""
    from custom_components.loxone import engineering_websocket as module

    class Http:
        def __init__(self) -> None:
            self.calls = 0

        async def async_register_static_paths(self, configs):
            self.calls += 1
            assert len(configs) == 1
            assert configs[0].url_path == "/pyloxone_static"
            if self.calls == 1:
                raise RuntimeError("injected setup failure")

    http = Http()
    hass = SimpleNamespace(data={}, http=http)
    commands = []
    panels = []
    removals = []
    monkeypatch.setattr(module.websocket_api, "async_register_command", lambda _hass, handler: commands.append(handler))

    async def register_panel(**kwargs):
        panels.append(kwargs)

    monkeypatch.setattr(module.panel_custom, "async_register_panel", register_panel)
    monkeypatch.setattr(
        module.frontend,
        "async_remove_panel",
        lambda _hass, path, **kwargs: removals.append((path, kwargs)),
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="injected setup failure"):
            await async_prepare_engineering_view(hass)
        await asyncio.gather(
            async_prepare_engineering_view(hass),
            async_prepare_engineering_view(hass),
        )
        await async_activate_engineering_view(hass, "entry-a")
        await async_activate_engineering_view(hass, "entry-b")
        await async_deactivate_engineering_view(hass, "entry-a")
        assert removals == []
        await async_deactivate_engineering_view(hass, "entry-b")
        await async_activate_engineering_view(hass, "entry-a")

    asyncio.run(scenario())
    assert http.calls == 2
    assert commands == [websocket_engineering_entries, websocket_engineering_hierarchy]
    assert len(panels) == 2
    assert all(panel["require_admin"] is True for panel in panels)
    assert all(panel["frontend_url_path"] == "pyloxone-hierarchy" for panel in panels)
    assert removals == [("pyloxone-hierarchy", {"warn_if_unknown": False})]


def test_failed_entry_setup_prepares_server_without_activating_panel(monkeypatch):
    """A rejected provider setup must leave the permanent server fail-closed."""
    import custom_components.loxone as integration
    from custom_components.loxone.pyloxone_api.exceptions import LoxoneUnauthorisedError

    events = []

    async def prepare(_hass):
        events.append("prepare")

    async def activate(_hass, _entry_id):
        events.append("activate")

    async def reject():
        raise LoxoneUnauthorisedError

    coordinator = SimpleNamespace(async_config_entry_first_refresh=reject)
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="entry-a", options={"host": "synthetic-host", "port": 80})
    monkeypatch.setattr(integration, "async_prepare_engineering_view", prepare)
    monkeypatch.setattr(integration, "async_activate_engineering_view", activate)
    monkeypatch.setattr(integration, "LoxoneCoordinator", lambda *_args: coordinator)

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is False
    assert events == ["prepare"]
    assert hass.data[DOMAIN] == {}
