"""Tests for detecting Loxone changes that affect Home Assistant."""

from types import SimpleNamespace

from homeassistant.components.search import ItemType

from custom_components.loxone.config_impact import (
    AreaChangeImpact,
    RemovedControlImpact,
    async_warn_about_config_impacts,
    control_identifiers_from_lox_config,
    find_area_change_impacts,
    find_removed_control_impacts,
    format_config_impact_message,
)


def test_control_identifiers_include_nested_controls():
    """Nested Loxone controls participate in removal detection."""
    assert control_identifiers_from_lox_config(
        {
            "controls": {
                "parent-key": {
                    "uuidAction": "parent-action",
                    "subControls": {
                        "child-key": {"uuidAction": "child-action"},
                        "fallback-child": {},
                    },
                }
            }
        }
    ) == {"parent-action", "child-action", "fallback-child"}


def test_format_message_lists_removed_and_area_impacts():
    """Warning text contains entities and affected consumers."""
    message = format_config_impact_message(
        [
            RemovedControlImpact(
                name="ST-F01",
                identifier="old-uuid",
                entity_ids=("switch.st_f01",),
                references={
                    ItemType.AUTOMATION: ("automation.use_st_f01",),
                },
            )
        ],
        [
            AreaChangeImpact(
                name="ST-F02",
                old_area="Wohnzimmer",
                new_area="Büro",
                references={ItemType.SCRIPT: ("script.area_off",)},
            )
        ],
    )

    assert "old-uuid" in message
    assert "switch.st_f01" in message
    assert "automation.use_st_f01" in message
    assert "Wohnzimmer → Büro" in message
    assert "script.area_off" in message


def test_removed_control_with_automation_reference_is_reported(monkeypatch):
    """A stale device UUID is actionable when an automation references it."""
    device = SimpleNamespace(
        id="device-id",
        identifiers={("loxone", "old-uuid")},
        model="Switch",
        name="ST-F01",
        name_by_user=None,
    )
    entity = SimpleNamespace(entity_id="switch.st_f01")
    monkeypatch.setattr("custom_components.loxone.config_impact.dr.async_get", lambda hass: object())
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.dr.async_entries_for_config_entry",
        lambda registry, entry_id: [device],
    )
    monkeypatch.setattr("custom_components.loxone.config_impact.er.async_get", lambda hass: object())
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.er.async_entries_for_device",
        lambda registry, device_id: [entity],
    )
    monkeypatch.setattr("custom_components.loxone.config_impact.entity_sources", lambda hass: {})

    class FakeSearcher:
        def __init__(self, hass, sources):
            pass

        def async_search(self, item_type, item_id):
            return {ItemType.AUTOMATION: {"automation.use_st_f01"}}

    monkeypatch.setattr("custom_components.loxone.config_impact.Searcher", FakeSearcher)

    impacts = find_removed_control_impacts(object(), SimpleNamespace(entry_id="entry-id"), {"controls": {}})

    assert impacts == [
        RemovedControlImpact(
            name="ST-F01",
            identifier="old-uuid",
            entity_ids=("switch.st_f01",),
            references={
                ItemType.AUTOMATION: ("automation.use_st_f01",),
            },
        )
    ]


def test_room_move_reports_area_targeted_automation(monkeypatch):
    """Moving a device warns when an automation targets either affected area."""
    old_area = SimpleNamespace(id="living", name="Wohnzimmer")
    new_area = SimpleNamespace(id="office", name="B\u00fcro")
    area_registry = SimpleNamespace(
        async_get_area=lambda area_id: old_area,
        async_get_area_by_name=lambda name: new_area,
    )
    device = SimpleNamespace(
        id="device-id",
        area_id="living",
        name="ST-F01",
        name_by_user=None,
    )
    device_registry = SimpleNamespace(async_get_device_by_identifier=lambda identifier, entry_id: device)
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.ar.async_get",
        lambda hass: area_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.dr.async_get",
        lambda hass: device_registry,
    )
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.automation.automations_with_area",
        lambda hass, area_id: {"automation.area_off"} if area_id == "living" else set(),
    )
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.script.scripts_with_area",
        lambda hass, area_id: set(),
    )

    impacts = find_area_change_impacts(
        object(),
        SimpleNamespace(entry_id="entry-id"),
        {"controls": {"socket-uuid": {"room": "B\u00fcro"}}},
    )

    assert impacts == [
        AreaChangeImpact(
            name="ST-F01",
            old_area="Wohnzimmer",
            new_area="B\u00fcro",
            references={ItemType.AUTOMATION: ("automation.area_off",)},
        )
    ]


def test_warning_is_created_only_for_detected_impacts(monkeypatch):
    """Persistent notifications are emitted only for actionable changes."""
    removed = [
        RemovedControlImpact(
            name="ST-F01",
            identifier="old-uuid",
            entity_ids=("switch.st_f01",),
            references={ItemType.AUTOMATION: ("automation.use_st_f01",)},
        )
    ]
    notifications = []
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.find_removed_control_impacts",
        lambda hass, entry, config: removed,
    )
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.find_area_change_impacts",
        lambda hass, entry, config: [],
    )
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.persistent_notification.async_create",
        lambda hass, message, title, notification_id: notifications.append((message, title, notification_id)),
    )

    count = async_warn_about_config_impacts(object(), SimpleNamespace(entry_id="entry-id"), {"controls": {}})

    assert count == 1
    assert len(notifications) == 1
    assert notifications[0][2] == "loxone_config_impact_entry-id"


def test_warning_is_not_created_for_safe_changes(monkeypatch):
    """Renames and other safe changes do not create notifications."""
    notifications = []
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.find_removed_control_impacts",
        lambda hass, entry, config: [],
    )
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.find_area_change_impacts",
        lambda hass, entry, config: [],
    )
    monkeypatch.setattr(
        "custom_components.loxone.config_impact.persistent_notification.async_create",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    count = async_warn_about_config_impacts(object(), SimpleNamespace(entry_id="entry-id"), {"controls": {}})

    assert count == 0
    assert notifications == []
