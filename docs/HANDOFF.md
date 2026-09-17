# Dream FPV project handoff

Last updated: 2026-09-17

This document is the starting point for a new Codex chat. Read it before
changing or running the project. It records the research objective, the user's
environment and preferences, the implementation state, validation evidence,
and the exact remaining work.

## 1. Project objective

Working title:

> Predictive FPV: Uncertainty-Aware Visual Continuation During Temporary
> Video-Link Failure

The primary research question is whether an uncertainty-aware predictive FPV
display can improve pilot recovery during short video outages without creating
dangerous overconfidence. The intended final result is a measured **safe
prediction horizon**: the outage duration at which synthetic continuation
changes from helpful to harmful.

The four eventual experimental display conditions are:

1. black screen;
2. frozen last-valid frame;
3. predicted view without uncertainty cues;
4. predicted view with uncertainty cues.

The project must keep simulator ground truth for evaluation while preventing a
predictor from using information that would not be available during a real
outage. Unsupported image regions must be visibly marked. Do not jump directly
to generative video or silently fill unknown areas with plausible imagery.

The original two-year plan is summarized as:

- Phase 1, months 4–6: programmable, manually piloted simulator with
  synchronized sensor/control logging;
- Phase 2, months 7–9: reproducible black/frozen video-outage baselines;
- Phase 3, months 10–12: oracle RGB-D reprojection using simulator-perfect
  pose;
- Phase 4, months 13–15: estimated pose, uncertainty scoring and automatic
  prediction termination;
- later phases: pilot study, formal human study, offline physical-flight
  validation and thesis.

Do not start Phase 3 unless the user asks. The current task was to complete
Phase 2.

## 2. User environment and preferences

- Main computer: Mac.
- A Windows laptop is also available, but the current repository and simulator
  are on the Mac.
- Coding experience: comfortable with Python; little experience with the other
  technologies.
- Simulator: Webots R2025a at `/Applications/Webots.app`.
- Python: 3.12.4 in the repository virtual environment.
- Controller: APEX T19H connected by USB cable, operated in Mode 2.
- The documentation must describe controller support generally. The checked-in
  APEX profile is a reference calibration, not a claim that only that radio is
  supported.
- The user prefers direct action and short explanations.
- Background checks must not minimize windows, activate hidden windows, or
  steal keyboard focus.
- Most importantly, the user explicitly requested **one Webots window left
  open**, rather than repeatedly opening and closing Webots windows. Before
  launching anything, check whether Webots is already running. Do not start a
  second instance unless the user asks.

Repository-specific instruction from `AGENTS.md`:

- before editing, fetch/check whether the local branch matches GitHub and pull
  if needed;
- commit and push every completed edit when a repository already exists.

Never discard unrelated user changes. Avoid destructive Git commands.

## 3. Repository and release state

- Local repository: `/Users/bitdev/Documents/ChatGPT/Research`
- GitHub: `https://github.com/d3f4au1t/dream-fpv`
- Branch: `main`
- Phase 1 tag: `apparatus-v1.0.0`
- Phase 1 validation record: `validation/apparatus-v1.0.0.json`
- Phase 2 source commit: `02237d1cda893bd5bd408e6b174a146e9a2a4277`
- The Phase 2 source commit has been pushed to `origin/main`.
- Phase 2 apparatus manifest digest:
  `31485b919cbb9391b83bd3182bf20c8bba5f39a0f85bafd5b9c302ae56db334a`
- Phase 2 controller-configuration digest:
  `3697b431b1683c903b06ce4358c55fb9a0faf1fcca47f90cc72f7ea273f67342`

At the time this handoff was written, one normal interactive Webots process was
left open with the deterministic Phase 2 schedule and the project file had all
rendering-device overlay panes hidden. Process IDs are transient; recheck with:

```bash
pgrep -fal '^/Applications/Webots.app/Contents/MacOS/webots'
```

The Phase 2 source is complete, but release bookkeeping is intentionally still
listed as pending so the next chat does not invent evidence:

- `validation/apparatus-v2.0.0.json` has not yet been committed;
- annotated tag `apparatus-v2.0.0` has not yet been created or pushed;
- the final 72-scenario Phase 2 dynamics rerun was stopped after 33 successful
  scenarios when the user requested that Webots stop opening and closing.

The Phase 1 validation record already contains a complete passing 72-scenario
dynamics suite. Phase 2 declares `flight_model_changed: false` and
`course_changed: false`; the Phase 2 spot check completed 33 scenarios without
a failure before it was stopped. A new chat must report this honestly rather
than claiming that all 72 scenarios were rerun on Phase 2.

## 4. Frozen Phase 1 apparatus

Phase 1 established the simulator baseline:

- acro/rate flight control at an 8 ms control step (125 Hz);
- mounted first-person camera at 480 × 270, 120° horizontal field of view and
  22° uptilt;
- depth at 320 × 180 with a 0.05–20 m range;
- synchronized RGB, depth, pose, velocity, IMU, controller, collision,
  recovery and course-zone logging;
- boundary/crash recovery with explicit safe rearming;
- a roughly 81 m technical course in a 50 × 30 m arena;
- conventional/angled gates, slalom, over-under bar, raised gate, tilted
  gravity gate, reverse-L, stacked double gate, orbit pylon, return turn,
  finish gate and landing pad.

The reference drone profile was based on the user's simulator screenshots:

- 198 g;
- 2.5-inch props, pitch 2.0;
- 4S, 5000 KV;
- 8% minimum throttle;
- gravity 9.82 m/s²;
- instant power 0.5, air friction 0.55, air grip 0.48;
- linear throttle with approximately 32.3% hover command.

Betaflight-style rates are frozen as:

| Axis | RC rate | Super rate | Expo | Maximum |
|---|---:|---:|---:|---:|
| Roll | 1.25 | 0.68 | 0.22 | 781.25°/s |
| Pitch | 1.25 | 0.68 | 0.22 | 781.25°/s |
| Yaw | 1.25 | 0.55 | 0.28 | 555.56°/s |

The controller is Mode 2. Roll, pitch and yaw commands center to zero rotational
rate; they do not auto-level the aircraft. Throttle does not spring back in the
simulator. The reference axis mapping and polarity live in
`config/controller.json` and `config/apex_t19_calibration_v1.json`.

This is a simulator-tuned reference, **not a validated digital twin**. Do not
claim that it exactly predicts the physical drone until physical measurements
support that claim.

## 5. Completed Phase 2 behavior

Phase 2 implements a reproducible link-loss experiment without prediction.

### Pilot display path

The research camera is attached to a 480 × 270 Webots `Display`. A textured
physical screen immediately in front of the mounted FPV `Viewpoint` fills the
main view. The display surface is a direct `Shape` child with a
`PBRAppearance`, matching Webots' supported camera-display pattern.

The project file keeps the depth, research-camera and duplicate pilot-display
overlay panes hidden. The physical display is hidden from the RGB and depth
sensors with `setVisibility`, preventing feedback while preserving hidden
ground truth.

### Conditions

- `black`: the pilot display becomes opaque black;
- `frozen`: the pilot display holds an immutable copy of the last valid frame.

Physics, controller input and hidden RGB-D capture continue through an outage.
Recovery aborts an active outage and restores live video.

### Timing

- controller step: 8 ms;
- pilot/RGB period: 16 ms;
- RGB-D anchor period: 64 ms;
- interval rule: half-open `[start_step, end_step)`;
- a display command issued on a control step is visible from the next step.

Requested durations are quantized upward to complete display frames:

| Requested | Effective | Display frames |
|---:|---:|---:|
| 100 ms | 112 ms | 7 |
| 250 ms | 256 ms | 16 |
| 500 ms | 512 ms | 32 |
| 750 ms | 752 ms | 47 |
| 1000 ms | 1008 ms | 63 |

### Schedules

- `deterministic`: ten balanced zone-triggered events, every condition-duration
  pair exactly once;
- `randomized`: seeded, repeatable selection of six unique course zones;
- default seed: 2907;
- optional condition override forces all selected events to black or frozen;
- `scripted` mode is reserved for acceptance tests.

Interactive zone-triggered events happen only if the pilot reaches the relevant
course section. They are not fabricated on a timer.

### Recorded evidence

Every Phase 2 run writes:

- `outage_schedule.json` with the fully materialized schedule and digest;
- `outage_events.jsonl` with run/start/end/abort/error records;
- `display_frames.csv` with every 16 ms pilot-frame mode and source digest;
- outage state fields in `telemetry.csv`;
- per-event onset RGB/depth, hidden midpoint, return RGB, and pilot
  onset/mid/last/return images.

Experimental buffers are copied in memory at the correct instant. Routine PNG
compression and disk I/O are deferred until after flight so they do not add a
timing cue at outage onset. Acceptance-only physical-display readback is
available through `DREAM_MODE_VALIDATE_DISPLAY_READBACK=1`.

## 6. Important files

Read these before modifying Phase 2:

- `README.md` — setup, controls, Phase 2 commands, outputs and limitations;
- `docs/APPARATUS_V1.md` — frozen Phase 1 apparatus;
- `docs/APPARATUS_V2.md` — complete Phase 2 specification and validity bounds;
- `config/apparatus_v2.json` — frozen Phase 2 claims and locked-file hashes;
- `config/outage_baselines_v1.json` — conditions, durations, deterministic and
  randomized schedules;
- `controllers/dream_mode_controller/dream_mode_controller.py` — Webots
  controller and outage integration;
- `controllers/dream_mode_controller/outage_baselines.py` — validation,
  quantization, scheduling and frame selection;
- `controllers/dream_mode_controller/outage_artifacts.py` — deferred RGB/depth
  artifact encoding;
- `worlds/dream_mode_research.wbt` — course, research camera, depth sensor and
  physical pilot display;
- `worlds/.dream_mode_research.wbproj` — overlay visibility; all three
  rendering-device panes should remain `;0;`;
- `scripts/run_phase2.sh` — interactive Phase 2 launcher;
- `scripts/outage_baseline_test.py` — two-replay end-to-end acceptance test;
- `scripts/verify_apparatus.py` — static manifest and semantic verifier;
- `tests/test_outage_baselines.py` and `tests/test_outage_artifacts.py` — Phase 2
  unit coverage.

The apparatus manifest locks 13 authoritative files. If a locked file changes,
update the apparatus version and validation evidence deliberately. Do not
replace a hash merely to silence the integrity check.

## 7. Commands

First check GitHub and local state:

```bash
cd /Users/bitdev/Documents/ChatGPT/Research
git fetch origin
git status --short
git rev-parse HEAD
git rev-parse origin/main
```

Before launching Webots, check for the existing process. If a window is already
open, leave it alone.

Normal simulator, outage emulator off:

```bash
./scripts/run_webots.sh
```

Phase 2 interactive modes:

```bash
./scripts/run_phase2.sh deterministic
./scripts/run_phase2.sh randomized
./scripts/run_phase2.sh deterministic black
./scripts/run_phase2.sh randomized frozen
```

Non-GUI validation:

```bash
./scripts/verify_apparatus.py
python3 -m unittest discover -s tests -v
```

Simulator checks, only when compatible with the user's one-window request:

```bash
./scripts/smoke_test.sh
./scripts/outage_baseline_test.py
./scripts/flight_dynamics_test.py
```

`flight_dynamics_test.py` launches and closes a Webots instance for each of 72
scenarios. Do not run it again without first explaining that behavior and
getting the user's direction, because the user specifically objected to the
repeated window lifecycle.

## 8. Verified results

The following checks were completed against the Phase 2 locked files:

- static verifier: passed; 13 locked files and 10 course zones;
- unit tests: 94 passed, zero failures;
- startup smoke: passed with 161 telemetry rows;
- outage acceptance: two isolated replays, ten events per replay, every required
  duration in black and frozen conditions, no aborts, exact frame intervals,
  valid artifacts, and return to live video;
- best final non-rendered onset work: 2.517 ms, below the 16 ms display period;
- rendered acceptance: two replays of ten events, physical display readback and
  main-Viewpoint images inspected; black covered the pilot view and frozen held
  the onset scene; onset work was 2.944 ms in that rendered run;
- inherited Phase 1 flight dynamics: 72/72 passed in
  `validation/apparatus-v1.0.0.json`;
- Phase 2 flight spot check: 33/72 completed without failure before the user
  requested that the repeated Webots launches stop.

Do not quote the interrupted Phase 2 flight run as 72/72.

## 9. Remaining Phase 2 release work

1. Decide how to record the inherited flight-dynamics evidence. The honest
   choices are:
   - record the complete Phase 1 72-scenario result plus the Phase 2 33-scenario
     spot check, explaining that the Phase 2 rerun was stopped at the user's
     request; or
   - design a true one-window regression runner before rerunning all 72.
2. Create `validation/apparatus-v2.0.0.json` with the actual evidence above and
   `validated_source.git_commit` set to the Phase 2 source commit.
3. Commit and push the validation record.
4. Create and push an annotated `apparatus-v2.0.0` tag pointing to the
   validation-record commit.
5. Leave the existing Webots window open unless the user asks to close it.

Do not mark a check as passed if it was not completed. Do not restart the
multi-window dynamics runner merely to make the summary look complete.

## 10. Known limits and research boundaries

- Phase 2 is black/frozen video interruption only. It contains no predicted
  imagery or uncertainty model.
- The emulator does not model RF propagation, packet loss, codec buffering,
  decoder concealment, compression damage or hardware display latency.
- Spatial-zone entry labels trigger locations but does not prove a correct gate
  crossing direction.
- Direct HID support is decoder- and calibration-specific. Unknown controllers
  must not be guessed into a mapping.
- End-to-end launch/test scripts are currently macOS-specific.
- The flight model is not physically identified and cannot support claims about
  a safe real-world prediction horizon.
- Formal participant work requires a fixed protocol, controller calibration,
  pilot training, a pilot study, power analysis and the relevant human-subject
  approval.

## 11. Next research phase, when requested

Phase 3 should implement a transparent oracle predictor before any learned
model:

1. capture the last valid RGB image and aligned depth map;
2. unproject pixels into a colored 3D point cloud using camera intrinsics;
3. use simulator-perfect camera pose during the outage;
4. transform and reproject the point cloud into the current camera pose;
5. use a z-buffer for visibility;
6. explicitly mark holes, newly revealed surfaces and unsupported pixels;
7. compare the predicted image with the hidden real simulator frame;
8. measure latency, support percentage and error by duration/maneuver;
9. terminate prediction conservatively after a configured limit.

Keep oracle pose and later estimated pose as separate experimental conditions.
The research contribution is not merely generated video; it is the calibrated,
uncertainty-aware boundary between useful continuation and unsafe confidence.

## 12. First actions for a new chat

1. Read `README.md`, `docs/APPARATUS_V2.md` and this handoff.
2. Fetch GitHub and confirm local `main` has not diverged.
3. Check `git status` and preserve any user changes.
4. Check whether Webots is already open; do not create another window.
5. Ask only if a real decision is required. Otherwise continue the pending
   validation-record/tag work directly and push each completed edit.
