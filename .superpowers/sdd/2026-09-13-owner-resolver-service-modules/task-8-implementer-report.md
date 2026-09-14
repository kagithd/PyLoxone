# Task 8 implementer report

Base: `6f1e25f578d16eef76ea7bf7a1412b88099e80bb`.
Branch: `codex/owner-resolver-service-modules`.
Implementation commit: recorded in the controller handoff after this report is committed.

## Outcome and scope

Task 8 now exposes only the accepted sanitized engineering snapshot in diagnostics, reports still-applicable Home Assistant consumers without editing them, and projects every durable Task 5 area conflict into a native Home Assistant Repair. The implementation targets the locally installed Home Assistant **2026.8.1** Repairs API. It does not contact a Miniserver, use a browser or network, install or reload anything, or publish upstream.

Diagnostics have exactly the top-level `integration`, `engineering_inventory`, and `engineering_runtime` sections. Every non-sensitive row contains the allowlisted inventory columns and its source Miniserver identifier. Sensitive rows collapse to a fixed structural record without names, UUIDs, paths, rooms, owner identifiers, or runtime content. Runtime output contains counts/status only; raw LoxAPP data, scalar/text values, endpoints, credentials, access/NFC data, coordinates, exception text, and raw attributes are not read into the response.

Consumer discovery still runs before registry mutation and publication still runs only after a successful registry application. Removed and platform-incompatible entity references are retained from Task 7. Task 8 additionally captures references that explicitly target the old or desired Home Assistant area before a room mutation. Unreferenced name/room metadata stays silent. Notifications list only bounded `automation.*`, `script.*`, `scene.*`, and `group.*` entity IDs; channel UUIDs, person entities, arbitrary URLs, and unsafe strings are excluded. No consumer registry update or Home Assistant service call is made.

## Native area Repairs

Each issue ID is scoped by an opaque config-entry hash and the complete immutable conflict token. This exact-token identity is required because Home Assistant deletes the original issue ID after a successful fix-flow entry; an independently retokenized conflict therefore remains a distinct issue. Issue data contains only a fixed kind/version, entry ID, and exact token. Friendly values are freshly loaded from Task 5 and validated before display.

The form shows the current Home Assistant room and requested Loxone room, renders an absent room as `No room`, and offers exactly:

- `Apply Loxone room`
- `Keep HA room`

Both actions call only `async_resolve_engineering_area_conflict(...)` with the exact current token. The flow never mutates area/device/entity registries directly and never calls Task 5 private storage or locking helpers. Malformed, foreign, missing, stale, concurrently changed, invalid-desired, and failed-resolution paths abort with bounded translated reasons. Issue synchronization then recreates the current actionable token where appropriate. Entry unload uses Home Assistant's native config-entry unload callback and removes only that entry's hashed issue namespace.

The one authorized Task 5 correction is narrow: when `Keep HA room` observes that HA changed after the conflict was captured, it reuses Task 5's existing locked race-persistence path. It preserves the current HA assignment and returns a freshly persisted conflict/token instead of allowing retries to loop forever on stale evidence. No room-authority rule was broadened.

English source strings and English/German translations cover the issue, form, exact choices, and all bounded abort outcomes. Existing Czech translation policy is unchanged and may fall back to English.

## Immutable area-impact evidence and compatibility

`EngineeringEntityImpact` gained only the `area_changed` kind and an immutable `target_area_ids` tuple. This is necessary to persist the pre-mutation area evidence across a crash/cold startup and to recheck whether the captured area-targeted consumer still applies. `engineering_snapshot.py` encodes the bounded identifiers in the existing private impact plan; its decoder treats the field as optional, so Task 7 envelopes restore with an empty tuple. Duplicate or unsafe target identifiers are rejected by the existing bounded identifier policy.

Snapshot nodes, rows, configuration revision, safe-content digest, read sequence, and generation hash are unchanged. Impact plans remain outside the snapshot content/generation hash. Round-trip tests prove the new field is immutable and restored, while an explicit prior-payload test removes the field and proves the old envelope and original generation remain loadable.

## Maintenance acceptance retained from Task 7

No `registry_maintenance.py` production change was necessary. The required semantics are already enforced at the committed-drain boundary and remain covered by real tests:

- `test_stale_observations_count_new_reads_not_replay_and_notifications_are_bounded` proves one successful committed token counts once, same-token replay does not increment, and the next forced complete read advances once.
- `test_cancelled_phase_cold_restart_reconciles_older_registry`, `test_partial_registry_failure_blocks_new_read_and_publication`, and the candidate-failure coordinator tests prove failed/pending candidates do not reach maintenance.
- `test_cleanup_defaults_to_audit_only_when_option_is_absent`, `test_cleanup_requires_two_consecutive_successful_structure_loads`, `test_time_mode_waits_for_elapsed_time_and_a_new_structure_load`, and `test_combined_mode_requires_observations_and_elapsed_time` retain default-off, observation, time, and combined behavior.
- `test_pending_engineering_generation_does_not_advance_grace`, `test_counter_store_failure_prevents_cleanup`, `test_cleanup_replays_after_counted_token_was_saved`, and the restored/partial-deletion replay tests retain acknowledged-storage cancellation and idempotency.

All **22** maintenance tests pass unchanged.

## Strict RED/GREEN evidence

The initial RED run produced **16 failures**: 11 native Repairs/unload cases, 2 diagnostics cases, 1 safe consumer-presentation case, 1 stale `Keep HA room` retokenization regression, and 1 post-registry Repairs hook/order case. Three later RED failures covered area-target capture, source-Miniserver completeness, and impact-plan persistence. One final RED failure proved a resolver/storage exception escaped the Repairs flow. Total recorded RED: **20 failing cases**. The unreferenced-room test was already green as the silent control.

After the minimal implementations:

- Focused engineering/Task 8 gate: **266 passed**, 43 pre-existing dependency deprecation warnings.
- Complete repository suite: **512 passed**, 133 pre-existing dependency deprecation warnings.

## Verification and privacy

- Configured Ruff on all seven changed production Python modules: **PASS**.
- High-signal Ruff `E4,E7,E9,F,I,UP` on every changed Python file: **PASS**.
- Ruff format check with `--target-version py313`: **PASS**.
- JSON parse for `strings.json`, `translations/en.json`, and `translations/de.json`: **PASS**.
- Compile check for every changed production Python module: **PASS**.
- Diff whitespace check: **PASS**.
- Added-line privacy audit: **PASS**. Additions contain only synthetic `ST-F07`, generic room/consumer labels, fixed reason codes, opaque hashes/tokens, and test-only reserved examples. No personal/user name, private installation/project/place name, GPS coordinate, private address/URL, credential, access code/label, or raw engineering configuration fragment is present.
- Required author and committer identity: `kagithd <42038442+kagithd@users.noreply.github.com>`.

## Files changed

Production:

- `custom_components/loxone/config_impact.py`
- `custom_components/loxone/coordinator.py`
- `custom_components/loxone/diagnostics.py`
- `custom_components/loxone/engineering_changes.py`
- `custom_components/loxone/engineering_registry.py`
- `custom_components/loxone/engineering_snapshot.py`
- `custom_components/loxone/repairs.py`
- `custom_components/loxone/strings.json`
- `custom_components/loxone/translations/en.json`
- `custom_components/loxone/translations/de.json`

Tests:

- `tests/test_engineering_coordinator.py`
- `tests/test_engineering_diagnostics.py`
- `tests/test_engineering_impacts.py`
- `tests/test_engineering_registry.py`
- `tests/test_engineering_repairs.py`
- `tests/test_engineering_snapshot.py`

## Residual boundaries

Live deployment validation is intentionally deferred because Task 8 forbids live/network/reload actions. The tests exercise the installed HA 2026.8.1 flow/result, selector, issue-registry signatures, registry fakes, durable codecs, cold recovery order, and failure/cancellation paths. Existing Home Assistant/aiohttp `BasicAuth` deprecation warnings are unrelated to this task. Process-local impact-notification dismissal still intentionally lasts only for the process, as accepted in Task 7; native Repairs preserve HA's own dismissal state for an unchanged exact issue ID.
