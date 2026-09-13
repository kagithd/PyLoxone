"""Source-scoped engineering topology diff tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.loxone.engineering_changes import diff_engineering_snapshots
from custom_components.loxone.engineering_snapshot import EngineeringSnapshotError
from tests.engineering_fixtures import element, inventory_of, make_snapshot


def test_uuid_diff_separates_add_remove_metadata_move_and_platform_change():
    """Stable UUID metadata changes remain changes rather than false replacement."""
    previous = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("removed-channel", "VoltageIn", parent_uuid="ms", io_name="AI1"),
            element("renamed-device", "TreeDevice", parent_uuid="ms", title="ST-F01"),
            element(
                "moved-device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F02",
                room="Office",
            ),
            element("old-parent", "LoxTree", parent_uuid="ms", title="Tree A"),
            element("new-parent", "LoxTree", parent_uuid="ms", title="Tree B"),
            element(
                "reparented-device",
                "TreeDevice",
                parent_uuid="old-parent",
                title="ST-F03",
            ),
            element("changed-channel", "VoltageIn", parent_uuid="ms", io_name="AI2"),
        )
    )
    current = make_snapshot(
        inventory=inventory_of(
            element("ms", "LoxLIVE", title="Miniserver", room=None),
            element("new-device", "TreeDevice", parent_uuid="ms", title="ST-F04"),
            element(
                "renamed-device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F01 renamed",
            ),
            element(
                "moved-device",
                "TreeDevice",
                parent_uuid="ms",
                title="ST-F02",
                room="Workshop",
            ),
            element("old-parent", "LoxTree", parent_uuid="ms", title="Tree A"),
            element("new-parent", "LoxTree", parent_uuid="ms", title="Tree B"),
            element(
                "reparented-device",
                "TreeDevice",
                parent_uuid="new-parent",
                title="ST-F03",
            ),
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
    assert changed["reparented-device"].old_via_identifier == "serial-a:old-parent"
    assert changed["reparented-device"].new_via_identifier == "serial-a:new-parent"
    assert changes.added[0].new_name == "ST-F04"
    assert changes.removed[0].old_semantic_platform == "sensor"


def test_uuidless_nodes_are_not_added_or_removed_and_unchanged_diff_is_empty():
    """Only stable engineering UUIDs participate in change identity."""
    previous = make_snapshot()
    current = make_snapshot(read_sequence=2)

    changes = diff_engineering_snapshots(previous, current)

    assert changes.is_empty


def test_diff_rejects_provider_scope_changes_instead_of_cross_matching_uuids():
    """Equal UUIDs from different entries or Miniservers must never cross-match."""
    previous = make_snapshot()
    other_source = replace(previous.source, entry_id="entry-b", serial_number="serial-b")
    current = replace(previous, source=other_source)

    with pytest.raises(EngineeringSnapshotError, match="source"):
        diff_engineering_snapshots(previous, current)


def test_exposure_suppression_does_not_manufacture_platform_change():
    """Diff semantics come from technical meaning, independent of exposure state."""
    previous = make_snapshot()
    target = next(row for row in previous.rows if row.node.element.uuid == "weather-value")
    suppressed = replace(
        target,
        capability=replace(
            target.capability,
            platform=None,
            reason="entity_unique_id_owned_by_other_entry",
        ),
    )
    current = replace(
        previous,
        rows=tuple(suppressed if row is target else row for row in previous.rows),
    )

    assert diff_engineering_snapshots(previous, current).is_empty
