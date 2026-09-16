# Dream FPV Experimental Apparatus v1.0.0

Status: **frozen** on 2026-09-16.

This document defines the first reproducible software apparatus for the Dream
Mode predictive-FPV project. Its purpose is to support controlled simulator
experiments; it is not a claim that the simulator is a physically exact copy of
the real drone.

## Authoritative definition

The machine-readable definition is
[`config/apparatus_v1.json`](../config/apparatus_v1.json). It contains the
apparatus identity, fixed random seed, timing, known limitations, and SHA-256
digests for every file that can alter the course, camera, controller mapping, or
flight behavior. The controller refuses to start when one of those files no
longer matches the frozen digest.

The course-zone definitions are in
[`config/course_zones.json`](../config/course_zones.json). Zone bounds use the
Webots world frame in metres: +x points forward from the launch pad, +y points
left, and +z points upward. Bounds include their lower edge and exclude their
upper edge. Position code `0` means the vehicle is on an open transfer between
named zones.

Any intentional change to a locked file requires all of the following:

1. Review the effect on experimental validity.
2. Increment the apparatus semantic version.
3. Recompute every locked-file digest.
4. Repeat unit, startup, and flight-dynamics acceptance testing.
5. Preserve a new validation summary and Git tag.

Do not silently update a digest to make a failed integrity check disappear.

## Frozen simulator and sensor geometry

- Simulator: Webots R2025a on the current macOS development machine
- Physics step: 8 ms (125 Hz)
- Deterministic Webots seed: 1907
- Course: 50 × 30 m, approximately 81.15 m nominal route
- FPV view and RGB camera: 120° horizontal field of view, 22° uptilt
- RGB device: 480 × 270, 16 ms sampling period when enabled
- Depth device: 320 × 180, 64 ms sampling period when enabled
- Default telemetry period: 40 ms
- Recovery boundary: 0.4 m inside each wall face

The RGB and depth sensors share the same translation and rotation. The initial
capture waits for the slower depth sampling period so the saved startup pair has
one simulator timestamp.

## Flight and controller calibration record

The accepted Mode 2 mapping is:

| Control | Apex T19 axis | Inverted in software |
|---|---:|---|
| Roll | 0 | Yes |
| Pitch | 1 | Yes |
| Throttle | 2 | No |
| Yaw | 3 | Yes |

The mapping and direction were confirmed interactively by the user and recorded
in [`config/apex_t19_calibration_v1.json`](../config/apex_t19_calibration_v1.json). Roll and
pitch use Betaflight legacy rates `1.25 / 0.68 / 0.22`; yaw uses
`1.25 / 0.55 / 0.28`. The reference airframe entry records 198 g, 2.5-inch
propellers, 5000 KV motors, 4S power, and the available bench-thrust reference.

The controller was not connected when v1.0.0 was frozen, so its USB VID/PID,
firmware revision, and measured raw minimum/center/maximum values are not part
of this version. Before formal participant data collection, record those values
and verify neutral positions fall inside the configured 2% deadzone. A connected
device's non-sensitive HID identity is saved as `input_device.json` in its run
directory.

## Experimental course zones

| Code | Zone | Intended research context |
|---:|---|---|
| 0 | `transfer` | Open flight between defined sections |
| 1 | `start_straight` | Straight flight and first gate |
| 2 | `occlusion_turn` | Newly revealed surface after the blue block |
| 3 | `checkpoint_turn` | Turn exit and angled gate approach |
| 4 | `slalom` | Repeated high angular motion |
| 5 | `over_under` | Narrow obstacle approach |
| 6 | `raised_gate` | Gate approach with height change |
| 7 | `gravity_gate` | Vertical maneuver and horizontal opening |
| 8 | `reverse_l` | Occlusion reveal and narrow opening choice |
| 9 | `stacked_tower` | High-angular-rate upper/orbit/lower sequence |
| 10 | `finish` | Recovery after the return turn |

These are spatial labels, not proof that a gate was successfully crossed. The
later trial manager must use directional gate-plane crossings and route state to
distinguish the stacked tower's upper and lower passes.

## Per-run provenance

Every controller run creates `run_manifest.json` before telemetry begins. It
records:

- apparatus identity, version, manifest digest, and locked-file digests;
- one deterministic run fingerprint;
- fixed seed and effective sensor/telemetry periods;
- home pose and recovery/sensor modes;
- all active `DREAM_MODE_*` overrides except the output path;
- controller and course-zone configuration snapshots;
- Git commit and dirty-state information when available.

Telemetry repeats the numeric apparatus version, seed, and current course-zone
code in every row. Depth metadata and smoke-test markers carry the same run
fingerprint. The complete run directory must remain together; a CSV separated
from its manifest is not a fully attributable research record.

Configured log directories may contain a test runner's console file, but the
controller refuses to overwrite any prior controller-owned artifact.

## Safety and known validity boundary

- Boundary, invalid-state, and inverted-crash recovery return the vehicle to
  the launch pose and disarm it.
- Rearming after recovery requires neutral attitude sticks and fully low
  throttle; controller loss requires a deliberate throttle cycle.
- Every visible course structure has corresponding collision geometry.
- The physical coefficients remain derived from a Crazyflie-style model and
  subjective simulator matching. Motor dynamics, inertia, aerodynamic drag,
  propwash, and collision response have not been identified from the physical
  airframe.
- Consequently, this apparatus may support controlled comparisons within the
  simulator, but it must not be represented as an exact physical-drone model or
  used to infer a real-world safe prediction duration without separate physical
  validation.
- Mesh and propeller assets are pinned to Cyberbotics' Webots R2025a URLs but
  remain external dependencies.

## Phase 1 exit checks

- Frozen hashes and apparatus schema pass verification.
- Zone codes are unique, non-overlapping, inside recovery bounds, and correctly
  classify representative course anchors.
- All local unit tests pass without opening Webots.
- A startup smoke test and all 72 flight-dynamics scenarios pass against the
  final frozen commit.
- The validation summary is stored under `validation/` and the commit is tagged
  `apparatus-v1.0.0`.

Long Webots validation runs can claim macOS focus. They must not be launched
unattended while the computer is in use; unit and integrity tests are safe to run
without opening simulator windows.
