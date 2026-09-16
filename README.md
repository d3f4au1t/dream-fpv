# Dream FPV

Dream FPV is a Webots-based FPV simulator for developing and testing predictive-flight displays. It includes an acro quad model, a technical practice course, synchronized RGB/depth capture, controller input, recovery logic, and timestamped telemetry.

The Phase 1 baseline is frozen as `apparatus-v1.0.0`. The tagged build passed the startup smoke test and all 72 flight-dynamics acceptance scenarios. See [the apparatus record](docs/APPARATUS_V1.md) and [validation summary](validation/apparatus-v1.0.0.json) for the exact configuration and test results.

This project is a research simulator, not a validated digital twin of a physical aircraft.

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

The main view is mounted to the aircraft and matches the research camera: 120° horizontal field of view with 22° uptilt. Flight control runs at 125 Hz. The RGB and depth sensors save one synchronized startup pair, then disable themselves to reduce rendering load. Set `DREAM_MODE_CONTINUOUS_SENSORS=1` before launch when a run needs continuous sampling.

## Input

Manual input is available through the keyboard or a supported HID controller adapter.

### Keyboard

| Action | Key |
|---|---|
| Pitch forward/back | Up / Down |
| Roll left/right | Left / Right |
| Yaw left/right | A / D |
| Increase/decrease throttle | W / S |
| Set throttle to the hover reference | Space |
| Emergency motor stop | Q |
| Arm after an emergency stop | E with throttle low |

Set `DREAM_MODE_DISABLE_JOYSTICK=1` before launch to force keyboard input.

### External controllers

Controller support has two parts:

1. Device discovery and HID report decoding in `dream_mode_controller.py`.
2. Axis indexes, inversion, range, and deadzone in `config/controller.json`.

Do not assume two USB radios or gamepads use the same HID report layout. A new controller may need a device matcher and report decoder before its axes can be configured. The adapter intentionally ignores unknown HID devices instead of guessing and sending unsafe commands to the flight model.

For a Mode 2 controller, verify these channels before flying:

| Stick movement | Command |
|---|---|
| Left vertical | Throttle |
| Left horizontal | Yaw |
| Right vertical | Pitch |
| Right horizontal | Roll |

The default Phase 1 mapping is roll, pitch, throttle, and yaw on axes 0–3. Roll, pitch, and yaw are inverted; throttle is not. Treat that as a reference profile, not a universal controller layout.

The currently shipped HID matcher recognizes the controller used to validate apparatus v1.0.0. Its calibration record remains under `config/` for reproducibility. Supporting another controller means validating its identity, report format, endpoints, center positions, mapping, and polarity before collecting research data.

When a supported external controller is active, keyboard flight axes and throttle are ignored. Disconnect the device or launch with `DREAM_MODE_DISABLE_JOYSTICK=1` to use the keyboard.

## Flight behavior

The aircraft uses acro/rate control. Centering roll, pitch, or yaw stops rotation; it does not level the aircraft.

The frozen rate profile is:

| Axis | RC Rate | Super Rate | Expo | Maximum rate |
|---|---:|---:|---:|---:|
| Roll | 1.25 | 0.68 | 0.22 | 781.25°/s |
| Pitch | 1.25 | 0.68 | 0.22 | 781.25°/s |
| Yaw | 1.25 | 0.55 | 0.28 | 555.56°/s |

Throttle commands motor thrust directly; there is no altitude hold. The reference model is a 198 g, 2.5-inch, 4S quad with 5000 KV motors and an approximately 32% hover command.

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

Run the full 72-scenario flight-dynamics suite:

```bash
./scripts/flight_dynamics_test.py
```

The simulator tests launch hidden Webots instances on macOS. They do not minimize or activate the user's current window.

## Repository layout

```text
config/                         frozen apparatus, controls, zones, calibration
controllers/dream_mode_controller/
                                Webots controller and input mapping
docs/APPARATUS_V1.md            Phase 1 apparatus specification
protos/FpvResearchQuad.proto    aircraft model
scripts/                        launcher and validation tools
tests/                          non-GUI unit and layout tests
validation/                     signed-off acceptance summaries
worlds/dream_mode_research.wbt  course and simulator world
```

## Known limitations

- The physical coefficients have not been identified from measured airframe data.
- Continuous image recording, outage injection, predictive display rendering, and uncertainty cues are not part of Phase 1.
- The current direct-HID adapter does not automatically support arbitrary controllers.
- The launcher and end-to-end validation scripts are macOS-specific.
- External Webots mesh assets are pinned to R2025a URLs but are not vendored in this repository.

Do not use simulator results to claim a safe real-world prediction horizon without separate physical validation.
