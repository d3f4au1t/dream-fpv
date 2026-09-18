# Dream FPV Experimental Apparatus v2.0.1

Status: **frozen** on 2026-09-18.

Version 2 adds reproducible pilot-video interruption baselines to the Phase 1
flight apparatus. It is intended for controlled simulator experiments that
compare uninterrupted flight with black-screen and frozen-frame conditions.
It does not implement a predictive display and does not claim to reproduce a
real video or radio link.

## Scope and inheritance

The machine-readable definition is
[`config/apparatus_v2.json`](../config/apparatus_v2.json). Version 2 inherits
the flight model, control rates, course, camera geometry, recovery behavior,
and simulator seed from the tagged `apparatus-v1.0.0` baseline. The course and
flight-model files were not retuned for this phase.

The Phase 1 specification remains in
[`docs/APPARATUS_V1.md`](APPARATUS_V1.md). Version 2 adds and locks the pilot
display, outage configuration, scheduler, artifact writer, and validation
paths needed for video-interruption experiments. The controller verifies the
manifest and all locked-file SHA-256 digests before starting a run.

Any intentional change to a locked file requires a new apparatus version,
fresh digests, a complete validation run, a new validation summary, and a new
Git tag. A digest must not be updated only to silence a failed integrity check.

## Pilot-view path

The research camera renders at 480 × 270 with a 16 ms sampling period, 120°
horizontal field of view, and 22° uptilt. Its live output is attached to a
Webots `Display` that fills the mounted pilot viewpoint. Outage commands affect
that display, not the aircraft dynamics.

During an outage:

- flight physics, controller input, collision handling, and recovery continue;
- the hidden RGB and depth sensors continue sampling ground truth;
- the raw research-camera and depth overlays must remain hidden from the pilot;
- a vehicle recovery aborts the active outage and returns the display to live
  video.

The run manifest records the display dimensions and period, the overlay-safety
check, and whether acceptance-only display readback was enabled.

## Baseline conditions and timing

The outage definitions are frozen in
[`config/outage_baselines_v1.json`](../config/outage_baselines_v1.json).

- `black` presents an opaque black image for the complete interruption.
- `frozen` holds an immutable copy of the last live frame at onset.

The controller runs at 8 ms and the pilot display updates every 16 ms. Requested
durations are rounded upward to a whole display frame:

| Requested duration | Effective duration | Display frames | Quantization error |
|---:|---:|---:|---:|
| 100 ms | 112 ms | 7 | 12 ms |
| 250 ms | 256 ms | 16 | 6 ms |
| 500 ms | 512 ms | 32 | 12 ms |
| 750 ms | 752 ms | 47 | 2 ms |
| 1000 ms | 1008 ms | 63 | 8 ms |

Intervals use the half-open rule `[start_step, end_step)`. The display command
is issued on the scheduled control step and is visible from the next 8 ms
control step. On recovery, the live-camera command follows the same one-step
visibility rule. Requested duration, effective duration, command step, visible
step, and realized duration are all recorded.

Outage onset is aligned to the 64 ms RGB/depth anchor period. This provides a
synchronized RGB frame and depth sample at the experimental instant without
moving simulator time or pausing the physics engine.

## Schedules

### Deterministic

The deterministic mode contains ten events, covering each requested duration
once in each condition and placing one event in every named course section.

| Event | Course zone | Condition | Requested | Effective |
|---|---|---|---:|---:|
| `baseline_001` | `start_straight` | black | 100 ms | 112 ms |
| `baseline_002` | `occlusion_turn` | frozen | 100 ms | 112 ms |
| `baseline_003` | `checkpoint_turn` | black | 250 ms | 256 ms |
| `baseline_004` | `slalom` | frozen | 250 ms | 256 ms |
| `baseline_005` | `over_under` | black | 500 ms | 512 ms |
| `baseline_006` | `raised_gate` | frozen | 500 ms | 512 ms |
| `baseline_007` | `gravity_gate` | black | 750 ms | 752 ms |
| `baseline_008` | `reverse_l` | frozen | 750 ms | 752 ms |
| `baseline_009` | `stacked_tower` | black | 1000 ms | 1008 ms |
| `baseline_010` | `finish` | frozen | 1000 ms | 1008 ms |

Each event triggers on the first entry into its zone. The start-straight event
uses a 2000 ms delay; the other configured events begin at the first aligned
RGB/depth anchor after entry. A zone event is not fabricated if the pilot never
reaches that section.

### Randomized

Randomized mode uses seed `2907` by default. It chooses six unique trigger zones
from the same ten eligible course sections, assigns condition-duration pairs
with a local seeded generator, and then restores course order before the run.
The seed can be overridden for a planned experiment, and the effective seed is
stored in the schedule, run manifest, telemetry, and event log.

Every run writes the fully materialized schedule to `outage_schedule.json`.
The schedule includes quantized timing and a canonical SHA-256 digest, so the
exact event plan is attributable even when randomized mode is used.

The condition override is intended for single-condition baselines. It keeps the
same event timing and forces all selected events to either `black` or `frozen`.
`scripted` mode exists for acceptance testing and requires an explicit event
list; it is not the normal interactive workflow.

## Running Phase 2

Normal simulator launches leave the outage emulator disabled:

```bash
./scripts/run_webots.sh
```

The Phase 2 launcher accepts a schedule mode followed by an optional condition
override:

```bash
./scripts/run_phase2.sh deterministic
./scripts/run_phase2.sh randomized
./scripts/run_phase2.sh deterministic black
./scripts/run_phase2.sh deterministic frozen
```

The same experiment can be configured directly with
`DREAM_MODE_OUTAGE_MODE`, `DREAM_MODE_OUTAGE_CONDITION`, and
`DREAM_MODE_OUTAGE_SEED`. These overrides are preserved in `run_manifest.json`.
Formal runs should record the chosen values before data collection rather than
changing them after inspecting results.

The outage system is independent of controller brand. Any USB HID controller
used for a trial still needs a verified device decoder and a validated Mode 2
axis, polarity, endpoint, center, and deadzone calibration.

## Recorded evidence

Every run retains the Phase 1 manifest, input history, telemetry, and startup
sensor artifacts. Version 2 also writes:

- `outage_schedule.json`: materialized events, seed, quantized timing, trigger
  parameters, and schedule digest;
- `outage_events.jsonl`: ordered run-start, onset, end, abort, error, and run-end
  records with control-step, simulator-time, and host-time fields;
- `display_frames.csv`: every 16 ms pilot frame with visible mode, event and
  source identity, source age, rendered digest, hidden-ground-truth digest, and
  schedule digest;
- outage columns in `telemetry.csv`: active flag, condition code, ordinal, seed,
  requested and effective duration, elapsed time, and anchor age;
- `outages/<ordinal>_<event-id>/`: per-event visual and depth evidence.

Each completed per-event directory contains:

- `anchor_rgb.png` and `anchor_depth.{png,f32,json}` at onset;
- `hidden_mid_rgb.png`, showing what the hidden live camera saw during the
  interruption;
- `return_rgb.png`, captured as the display returns to live video;
- `pilot_anchor.png`, `pilot_mid.png`, `pilot_last.png`, and
  `pilot_return.png`, representing the pilot output across the transition.

Sensor buffers are copied in memory at their experimental timestamps. Routine
PNG compression and disk writes are deferred until the flight has finished so
artifact encoding does not add an onset cue or block the controller path. The
acceptance runner can additionally enable physical `Display` readback and
rendered-viewpoint captures; those checks are validation aids rather than
normal experiment artifacts.

Keep the complete run directory together. The CSV files depend on their run
manifest and materialized schedule for interpretation and provenance.

## Validation contract

The non-GUI verifier checks the apparatus schema, locked-file hashes, simulator
timing, display geometry, sensor geometry, schedule semantics, course-zone
references, duration quantization, and hidden-overlay policy. Unit tests cover
the scheduler, overlap rejection, seeded randomization, frame selection,
artifact encoding, and world layout.

The end-to-end acceptance test runs two isolated replays containing all ten
condition-duration pairs:

```bash
./scripts/outage_baseline_test.py
```

It requires both replays to agree on their transition and frame signatures. It
also checks exact half-open frame intervals, onset and return timing, black and
frozen display contents, changing hidden ground truth, RGB/depth artifacts,
telemetry transitions, schedule digests, clean run-end counts, and onset work
within one 16 ms display period. Hidden Webots instances use unique ports and do
not minimize or activate the user's current window.

Phase 1 regression checks remain mandatory:

```bash
./scripts/verify_apparatus.py
python3 -m unittest discover -s tests -v
./scripts/smoke_test.sh
./scripts/flight_dynamics_test.py
```

The signed-off result is stored in
`validation/apparatus-v2.0.1.json` and the validated source is tagged
`apparatus-v2.0.1`.

The flight-dynamics runner uses one hidden Webots process for the complete
72-scenario suite. It reloads the frozen world between isolated scenarios,
which resets simulation time, fields, physics, devices and the controller
without repeatedly creating and closing application windows. The former
multi-process behavior remains available only for diagnosis with
`--legacy-multi-session`.

## Validity boundary

- The outage emulator changes only the rendered pilot view. It does not model
  packet loss, RF propagation, encoding, buffering, decoder concealment,
  compression artifacts, link recovery, or display hardware latency.
- Phase 2 contains no predicted imagery, optical-flow extrapolation, learned
  model, uncertainty estimate, or safety cue.
- The physical coefficients remain simulator-tuned rather than identified from
  a measured airframe. Results support controlled comparisons inside this
  apparatus, not claims about a safe real-world prediction horizon.
- Spatial zone entry is a trigger label, not proof that a gate was crossed in
  the intended direction.
- Controller support is calibration-specific. A working mapping for one radio
  or gamepad does not validate another device.
- Formal participant trials still require a documented controller calibration,
  physical-airframe validation appropriate to the research claim, a fixed
  experimental protocol, and the relevant human-subject review.

## Phase 2 exit checks

- The Version 2 manifest and every locked-file digest pass verification.
- Deterministic and randomized schedules validate against the frozen course
  zones and produce canonical schedule digests.
- Local unit tests, startup smoke testing, and all 72 inherited flight-dynamics
  scenarios pass.
- Two end-to-end outage replays pass every duration in both conditions with
  exact timing, valid artifacts, and no aborted or pending events.
- The validation summary and `apparatus-v2.0.1` Git tag identify the frozen
  source used for the apparatus.
