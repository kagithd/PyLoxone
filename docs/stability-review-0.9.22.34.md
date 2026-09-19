# Stability review: 0.9.22.34–0.9.22.36

This fork-only stabilization follows a review of the engineering branch from
`ab0ab92` through `55f085b`, with the earlier metadata patch and upstream code
used for comparison. A previous Git revision is not presumed to be a known-good
live installation. No upstream publication is part of this release.

## Confirmed defects and corrections

| Area | Defect | Correction |
| --- | --- | --- |
| Room repair performance | Eligible-device discovery was recomputed for every node at each batch checkpoint. | Compute the eligible set once per checkpoint. |
| HA responsiveness | Complete snapshot validation/encoding/decoding repeatedly ran on the HA event loop. | Run CPU-heavy store codecs, migration, candidate roundtrip, sequence validation, metadata derivation and registry-plan validation in the executor; preserve mutation and commit fences. |
| Repair latency | Each small journal checkpoint rebuilt and revalidated the unchanged inventory. | Retain at most two successfully validated frozen snapshots and their immutable encoded payloads; compare complete input on decode and return fresh mutable output. Envelope, journal, lifecycle and live registry checks remain uncached. |
| Device identity | Reassigning an entity could delete its previous physical device even when that hardware remained present. | Retain all snapshot-backed device identifiers during logical-device cleanup. |
| Startup | Engineering recovery and runtime probes gated ordinary Loxone setup. | Start the normal listener first; restore engineering state in the owned background task. |
| Transport ownership | The socket returned during initial connection setup was discarded, allowing a second open. This also exists in the compared upstream revision. | Prepare configuration without a socket; open and retain transport immediately before listener authentication. |
| Reload | Cleanup cancellation/timeout could abandon its ownership; a retry created another cleanup operation. | Retain and rejoin the cleanup task. Close transport before waiting for optional metadata work; reject replacement while cleanup is incomplete. |
| Shutdown subscriptions | Old instances retained HA stop/start listeners across reloads. Old stop callbacks could write stale tokens. | Unsubscribe those callbacks with the owning coordinator. |
| Channel availability | One failed engineering runtime probe invalidated all successful bindings. | Preserve independently successful bindings. |

## Deliberately retained boundaries

- Complete validation and atomic acknowledged writes remain; responsiveness is
  improved by scheduling, not by bypassing integrity checks.
- No device/entity identity renaming, no automatic deletion policy expansion,
  no commands to physical actuators, no Loxone engineering writes.
- Existing area decisions, room identity mappings, privacy filtering and
  lifecycle/replay fences remain enforced.
- Storage remains schema version 4. This release does not introduce another
  migration. A rollback to pre-v4 code still requires compatible private state.
- A genuinely stalled cleanup reports failure rather than starting a second
  connection. Reload can rejoin it after it settles; a permanent external I/O
  failure is not reported as successful recovery.

## Verification

Live startup of 0.9.22.34 exposed a timing defect in its socket-ownership fix:
the retained socket could expire while HA loaded entity platforms. That build
was not accepted as stable. Version 0.9.22.35 separates configuration preparation
from transport opening, preserving the standalone API while avoiding an idle
unauthenticated socket. Two targeted regressions cover both startup phases.

Version 0.9.22.35 restored normal entity states, but final room persistence did
not complete within the 60-second acceptance deadline. Profiling identified
repeated snapshot codec work. Version 0.9.22.36 adds the bounded snapshot-only
cache; warmed local encode/decode fell from approximately 2.5 seconds to
40–60 milliseconds for a representative multi-thousand-node private inventory.
No private inventory is included in the repository. Cache tests cover complete
input tampering, invalid Python shapes, mutable output isolation, stale envelope
metadata, bounded eviction and concurrent callers. The full local suite passes
751 tests; live persistence acceptance is still a separate required check.

Regression tests first reproduced the blocking startup, discarded socket,
cross-channel invalidation, quadratic checkpoints, event-loop snapshot codec,
active-device deletion, duplicate cleanup and stale HA stop subscriptions.
The complete local suite passes; targeted independent review found no unresolved
release blocker in the corrective diff. The tests include actual HA event-bus
subscription setup/unload and real snapshot codecs; network and disk boundaries
are controlled where needed.

Deployment acceptance must separately confirm configuration validation, loaded
integration, preserved device/entity identities and areas, bounded repair
responses, successful final persistence, and unchanged actuator states. Unit
tests are not a substitute for this check. Production logs/configuration are
private and are not included here.

## Live acceptance of 0.9.22.36

Tested with Home Assistant **2026.9.3** and Miniserver **17.2.8.28**.
Configuration validation passed; the deployed archive was checksum-verified.
A full HA backup and the previous integration source were retained privately.

- Native Repairs API: opening the room form took 135 ms; invalid input returned
  a field validation result in 102 ms instead of hanging.
- Confirming two existing room groups for three devices completed successfully
  in 13.3 seconds. Existing device areas were unchanged.
- During final persistence, 28 parallel HA API probes had no timeout; the
  slowest response was 951 ms.
- The durable journal was empty after completion. Room mappings and managed
  assignments survived an integration reload; no Loxone repair remained.
- No original device, entity identity, parent link, room assignment or actuator
  state changed relative to the captured baseline. No test command switched a
  physical output.

Remaining limitations: cold platform setup can still emit a ten-second startup
warning, and final persistence is not instantaneous. The native form's wait was
measured through its API, not by a browser UI test. This acceptance does not
establish long-duration reliability or certify unrelated HA integrations.

AI-assisted changes were critically re-reviewed with independent code review and
targeted failing regressions. This release makes no claim of exhaustive proof
against every possible HA or Miniserver failure.
