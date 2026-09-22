# Dream FPV

Dream FPV is a Webots-based FPV research simulator for controlled video-loss experiments. It includes an acro quad model, a technical practice course, a camera-backed pilot view, synchronized RGB/depth capture, external-controller input, recovery logic, and timestamped telemetry.

The frozen Phase 1 flight baseline is tagged `apparatus-v1.0.0`. Phase 2 keeps that flight model and course unchanged and adds reproducible black-screen and frozen-frame outage baselines. The current Phase 2 apparatus version is `2.4.0`, adding manual outage shortcuts. See the [Phase 1 apparatus record](docs/APPARATUS_V1.md), [Phase 2 apparatus record](docs/APPARATUS_V2.md), [project handoff](docs/HANDOFF.md), and the corresponding validation summaries under [`validation/`](validation/).

This is an experimental software apparatus, not a validated digital twin of a physical aircraft.

## Requirements

- Webots R2025a
- Python 3.12
- A Python environment with the packages in `requirements.txt`

The current launch and validation scripts target macOS and expect Webots at `/Applications/Webots.app`. The tested environment is macOS 26.3, Webots R2025a, and Python 3.12.4.

Other platforms can open `worlds/dream_mode_research.wbt` directly in Webots. Update the Python command in `controllers/dream_mode_controller/runtime.ini` to point to the platform's virtual-environment interpreter. The macOS test runners have not yet been ported to Windows or Linux.

## Quick start

Clone the repository and install the controller dependency:

```bash
git clone https://github.com/d3f4au1t/dream-fpv.git
cd dream-fpv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set `COMMAND` in `controllers/dream_mode_controller/runtime.ini` to the absolute path of the environment's Python executable. On macOS, it will normally be:

```ini
[python]
COMMAND = /absolute/path/to/dream-fpv/.venv/bin/python3
```

Start the simulator:

```bash
./scripts/run_webots.sh
```

You can also open `worlds/dream_mode_research.wbt` from Webots.

Digital is the default pilot mode. It references the standard DJI O4 Air Unit with DJI Goggles 3 in Racing Mode: a clean full-frame view, 117.6° lens geometry, Normal color, and no inset camera pane. The simulator uses the mounted Webots viewport for healthy digital video and exposes the physical display only during black or frozen outages. The proprietary H.265 encoder, adaptive bitrate and RF link are not reproduced.

Analog references a good-signal Foxeer Nano Predator 5 NTSC/CVBS camera in 16:9 through a modern deinterlaced receiver. Its 125° view is softer and lower-bandwidth than digital, with restrained sensor noise, four-millisecond camera motion response and mild lens character. It deliberately has no permanent scanlines, colored sync bars or exaggerated VHS damage; those are not present on a healthy modern analog link. Neither pilot path contaminates hidden RGB-D ground truth.

Select the optional analog view explicitly:

```bash
./scripts/run_webots.sh analog
```

Flight control runs at 125 Hz. The RGB and depth sensors save one synchronized startup pair, then disable themselves to reduce rendering load. Set `DREAM_MODE_CONTINUOUS_SENSORS=1` before launch when a run needs continuous sampling.

## Input

Manual input is available through the keyboard or a supported USB HID controller. Keyboard input is intended for development and automated checks, not normal flying or participant trials.

Set `DREAM_MODE_DISABLE_JOYSTICK=1` before launch to force keyboard input.

### External controllers

Controller support has two parts:

1. Device discovery and HID report decoding in `dream_mode_controller.py`.
2. Axis indexes, inversion, range, and deadzone in `config/controller.json`.

USB radios and gamepads do not share one report layout. Each controller model must be identified and calibrated before use; some devices also need a small report decoder before their axes can be configured. Unknown HID devices are ignored rather than mapped by guesswork.

For a Mode 2 controller, verify these channels before flying:

| Stick movement | Command |
|---|---|
| Left vertical | Throttle |
| Left horizontal | Yaw |
| Right vertical | Pitch |
| Right horizontal | Roll |

The checked-in reference mapping uses axes 0–3 for roll, pitch, throttle, and yaw. Roll, pitch, and yaw are inverted; throttle is not. This is a calibration profile, not a universal controller layout. Before collecting research data with another device, verify its identity, report format, endpoints, center positions, channel mapping, polarity, and deadzone. Calibration records remain under `config/` so completed runs can be reproduced.

When a supported external controller is active, keyboard flight axes and throttle are ignored. Disconnect the device or launch with `DREAM_MODE_DISABLE_JOYSTICK=1` to use the keyboard.

### Manual video outages

Click the flight viewport while the simulation is running, then tap a number key:

| Key | Video cut | Actual frame-aligned duration |
|---|---|---|
| `1` | ¼ second | 256 ms |
| `2` | ½ second | 512 ms |
| `3` | 1 second | 1008 ms |

These shortcuts are enabled by default in both digital and analog modes, including
when flying with a USB controller. Only the video goes black: physics and controls
continue, and live video returns automatically. Durations use simulation time;
use Webots real-time mode for real-time practice. Holding a key triggers once;
presses while an outage is pending or active are ignored, not queued or extended.
Release and press again for another outage. Simultaneous presses choose the
lowest-numbered key. Crash recovery cancels pending or active outages.

The requested cut starts at the next synchronized RGB-D frame (up to 64 ms
including display-command latency). Edit `manual.key_durations_ms` in the outage
configuration to change the three presets, following the apparatus versioning
procedure. For frozen-frame practice use `./scripts/run_phase2.sh manual frozen`.
Set `DREAM_MODE_OUTAGE_MODE=off` to disable all outage shortcuts. Formal
deterministic/randomized runs ignore them so keyboard input cannot alter a trial.

## Flight behavior

The aircraft uses acro/rate control. Centering roll, pitch, or yaw stops rotation; it does not level the aircraft.

The frozen rate profile is:

| Axis | RC Rate | Super Rate | Expo | Maximum rate |
|---|---:|---:|---:|---:|
| Roll | 1.25 | 0.68 | 0.22 | 781.25°/s |
| Pitch | 1.25 | 0.68 | 0.22 | 781.25°/s |
| Yaw | 1.25 | 0.55 | 0.28 | 555.56°/s |

Throttle commands motor thrust directly; there is no altitude hold. The reference physics profile is a 198 g, 2.5-inch, 4S quad with 5000 KV motors and an approximately 32% hover command. Its coefficients were tuned as a simulator reference and have not been identified from physical-airframe measurements.

Move throttle fully low after a simulator reset to arm. Boundary exits, invalid physics, and a settled inverted crash return the aircraft to the start and disarm it. After recovery, center the attitude sticks and hold throttle low for 0.15 seconds. Input loss requires a deliberate above-10%-then-low throttle cycle from the reconnected source.

## Course

The course covers roughly 81 metres inside a 50 × 30 metre arena. It mixes open transfer sections with tighter combinations:

- conventional and angled gates;
- slalom pylons;
- over-under and raised gates;
- a tilted gravity gate;
- a three-opening reverse-L;
- a stacked double-gate tower and orbit pylon;
- a return turn, finish gate, and landing pad.

Visible course structures have matching collision geometry. Spatial zone IDs label the major sections for telemetry, but they do not by themselves prove that a gate was crossed in the correct direction.

## Phase 2 video-outage baselines

Phase 2 interrupts the selected pilot view while flight physics and control input continue normally. Digital live operation uses the original mounted viewport and reveals the physical display only for the interruption; analog operation continuously uses the 480 × 270 camera-backed display. The hidden research RGB/depth sensors continue sampling so the interrupted view can be compared with ground truth after the run.

Two baseline conditions are implemented:

- `black` replaces the pilot image with black;
- `frozen` holds the last live frame captured at outage onset.

The requested durations are quantized upward to the 16 ms display period. Both the requested and realized values are recorded.

| Requested | Realized | Display frames |
|---:|---:|---:|
| 100 ms | 112 ms | 7 |
| 250 ms | 256 ms | 16 |
| 500 ms | 512 ms | 32 |
| 750 ms | 752 ms | 47 |
| 1000 ms | 1008 ms | 63 |

Normal launches enable manual `1`/`2`/`3` outages, with no automatic schedule:

```bash
./scripts/run_webots.sh
```

Use the Phase 2 launcher to run an outage schedule:

```bash
# Fixed ten-event schedule: every duration in both conditions
./scripts/run_phase2.sh deterministic

# Seeded selection of six course zones, durations, and conditions
./scripts/run_phase2.sh randomized

# Keep the schedule and force every event to one condition
./scripts/run_phase2.sh deterministic black
./scripts/run_phase2.sh randomized frozen

# Optional third argument selects the analog pilot treatment
./scripts/run_phase2.sh deterministic configured analog
```

The deterministic schedule places one condition-duration pair in each named course section. The randomized schedule makes a repeatable seeded selection from those sections. Events are triggered on zone entry and aligned to a synchronized 64 ms RGB/depth anchor, so an event only occurs if the aircraft reaches its trigger zone. The materialized schedule and its SHA-256 digest are saved with the run.

Phase 2 is a video-interruption baseline. It does not predict the missing view, estimate uncertainty, or model an RF/video link.

## Run data

Each run creates `logs/<UTC timestamp>/` with:

- `run_manifest.json` — apparatus version, file hashes, controller-config hash, run fingerprint, seed, runtime versions, active overrides, Git state, course zones, and the effective configuration;
- `input_device.json` — the first non-sensitive HID identity detected during the run;
- `input_device_events.jsonl` — append-only controller connection history;
- `telemetry.csv` and rotated segments — timestamps, pose, velocity, IMU, commands, rate targets, motor outputs, input/failsafe state, raw axes, recovery count, collision state, and course zone;
- `rgb_initial.png` — initial RGB observation;
- `depth_initial.png` — viewable initial depth image;
- `depth_initial.f32` — initial depth values as native-endian float32 metres;
- `depth_initial.json` — dimensions, range, encoding, camera parameters, and provenance for the depth data.

Phase 2 runs also include:

- `outage_schedule.json` — the fully materialized schedule, seed, quantized timing, trigger details, and schedule digest;
- `outage_events.jsonl` — ordered run, onset, recovery, abort, scheduler-error, and completion records with simulator and host timestamps;
- `display_frames.csv` — one row per 16 ms pilot frame, including its visible mode, source frame, source age, rendered digest, and hidden-ground-truth digest;
- outage fields in `telemetry.csv` — active state, condition, event ordinal, seed, requested/effective duration, elapsed time, and anchor age;
- `outages/<event>/` — onset RGB and depth, hidden midpoint and return RGB, plus pilot images at onset, midpoint, final interrupted frame, and return to live video.

Image buffers are captured at the experimental instant and encoded after the flight, keeping PNG compression and routine artifact writes out of the control path.

Keep the complete run directory together. The CSV relies on its manifest for zone names and cryptographic provenance.

## Verification

Run the non-GUI checks:

```bash
./scripts/verify_apparatus.py
python3 -m unittest discover -s tests -v
```

Run the startup smoke test:

```bash
./scripts/smoke_test.sh
```

Run the end-to-end Phase 2 outage acceptance test:

```bash
./scripts/outage_baseline_test.py
DREAM_MODE_VIDEO_STYLE=analog ./scripts/outage_baseline_test.py
./scripts/outage_baseline_test.py --manual
DREAM_MODE_VIDEO_STYLE=analog ./scripts/outage_baseline_test.py --manual
```

This runs two isolated replays of all ten condition-duration pairs and checks schedule reproducibility, exact display-frame intervals, pilot-display output, hidden ground truth, telemetry, artifacts, and return to live video.

Run the full 72-scenario flight-dynamics suite:

```bash
./scripts/flight_dynamics_test.py
```

The dynamics runner executes all 72 scenarios in one hidden Webots session, reloading the world between scenarios without repeatedly opening and closing the application. The other simulator tests also launch hidden Webots instances on macOS. They do not minimize or activate the user's current window.

## Repository layout

```text
config/                         frozen apparatus, controls, zones, calibration
controllers/dream_mode_controller/
                                Webots controller and input mapping
docs/APPARATUS_V1.md            Phase 1 apparatus specification
docs/APPARATUS_V2.md            Phase 2 outage-baseline specification
protos/FpvResearchQuad.proto    aircraft model
scripts/                        launcher and validation tools
tests/                          non-GUI unit and layout tests
validation/                     signed-off acceptance summaries
worlds/dream_mode_research.wbt  course and simulator world
```

## Known limitations

- The physical coefficients have not been identified from measured airframe data.
- Phase 2 supports controlled black and frozen frames, not continuous encoded-video capture, packet loss, RF propagation, decoder behavior, or latency/jitter emulation. The visual profiles are grounded in public DJI O4, Foxeer Predator 5 and modern analog-receiver specifications, but have not been calibrated against captured goggle DVR or optical measurements.
- Predictive display rendering and uncertainty cues are not implemented.
- The direct-HID adapter requires a validated identity, decoder, and calibration profile for each controller model; arbitrary devices are not mapped automatically.
- The launcher and end-to-end validation scripts are macOS-specific.
- External Webots mesh assets are pinned to R2025a URLs but are not vendored in this repository.

Do not use simulator results to claim a safe real-world prediction horizon without separate physical validation.
