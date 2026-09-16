# Predictive FPV research simulator

This is the first programmable simulator for the **Dream Mode** project. It combines a symmetric micro quad, a purpose-built turning FPV practice course, synchronized sensors, configurable manual control, and timestamped telemetry.

## Installed software

- Webots R2025a: `/Applications/Webots.app`
- Python 3.12: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`
- Project Python environment: `.venv/`

## Start the simulator

From Terminal:

```bash
cd "/Users/bitdev/Documents/ChatGPT/Research"
./scripts/run_webots.sh
```

Or open `worlds/dream_mode_research.wbt` from Webots.

The main Webots view is the drone's mounted FPV camera: 120° field of view, 22° uptilt, and no tracking-camera lag. A matching 480×270 RGB sensor and 320×180 depth sensor capture one synchronized startup pair and then switch off to preserve frame rate. Flight physics and pilot input run every 8 ms. Set `DREAM_MODE_CONTINUOUS_SENSORS=1` before launch only when an experiment needs live sensor sampling.

## Course

The route is about 81 metres long across a 50-by-30-metre field. Launch down the six-metre opening straight through the large orange gate, break right around the first blue block, and line up with the angled yellow checkpoint. A broad three-pylon S-section leads into the cyan over-under bar. From there, take the long diagonal to the raised green gate and climb into the high, tilted orange gravity gate. Drop through its horizontal opening, set up for the lower-left opening of the magenta three-opening reverse-L, then take the long diagonal to the blue double gate tower. Fly its upper opening, orbit the separate blue pylon, return through the lower opening, and turn back through the angled purple finish gate. The purple landing pad sits two metres beyond it. The transfer sections alternate between open 6–12 metre runs and compact technical combinations, and every wall, frame, pylon, bar, and obstacle has matching collision geometry.

## Controls

The controller automatically prefers a connected joystick. Without one, use:

| Action | Keyboard |
|---|---|
| Pitch forward/back | Up / Down |
| Roll left/right | Left / Right |
| Yaw left/right | A / D |
| Increase/decrease throttle | W / S |
| Reset throttle to hover | Space |
| Emergency motor stop | Q |
| Arm again after emergency stop | E, with throttle fully low |

Roll, pitch, and yaw use **acro/rate behavior** with the exact legacy Betaflight rate settings shown in the reference: roll/pitch 1.25 RC Rate, 0.68 Super Rate, 0.22 Expo (781.25 degrees/second); yaw 1.25 RC Rate, 0.55 Super Rate, 0.28 Expo (555.56 degrees/second). Returning a stick to center stops rotation without leveling the aircraft.

Throttle directly commands motor thrust, as it does in an FPV quad. There is no altitude hold: raising throttle adds thrust and lowering it removes thrust. The model is 198 g with 2.5-inch props, 5000 KV motors, 4S power, about 32% hover stick, and a 5.15:1 static thrust-to-weight anchor. After every simulator reset, move throttle fully down once to arm, then raise it gradually. Motor corrections are mixed as thrust instead of rotor speed, preserving attitude authority at high and low throttle in an Airmode-like way. Once the craft has settled on the floor, zero-throttle attitude commands are suppressed so it cannot thrash or launch from a crash.

Crossing the course boundary, entering an invalid physics state, or remaining stuck on the ground beyond 75° of tilt returns the craft upright to its start and disarms it. Center roll, pitch, and yaw and hold throttle fully low for 0.15 seconds to rearm after that reset; this prevents a maneuver held during the teleport from launching the craft sideways. An input fault still requires the stronger above-10%-then-low gesture from the same reconnected source. Keyboard flight keys are ignored while the Apex is connected, so an accidental key press cannot replace radio throttle.

## Apex T19 setup

1. Put the T19 in joystick/simulator mode.
2. Connect it by USB before starting Webots.
3. Start this world and read the Webots console. It reports the controller name and number of axes.
4. If the channels are wrong, edit `config/controller.json`.

The tested Mode 2 mapping is axes 0–3 for roll, pitch, throttle, and yaw. Roll, pitch, and yaw are inverted in the configuration; throttle is not. This is the mapping confirmed with this Apex T19, but re-check it after radio firmware changes or recalibration and before collecting experiment data.

### If one stick direction does not respond

The T19 manual gives this recalibration sequence:

1. Connect the controller to the computer with USB-C.
2. Put both sticks at their centers. For Mode 2, manually move the left throttle stick to the middle first.
3. Hold **FN + MODE** until the indicator flashes quickly. This records the stick centers.
4. Wait for the indicator to flash slowly, then move **both sticks** gently through their full up, down, left, and right limits.
5. Stop when the indicator changes to its breathing-light pattern, then reset the Webots simulation.

For Mode 2, the expected channels are left vertical = throttle, left horizontal = yaw, right vertical = pitch, and right horizontal = roll. If yaw remains fixed after calibration, use **A / D** as the temporary keyboard yaw control while the T19 USB channel is diagnosed.

On macOS 26, Webots R2025a's built-in joystick enumerator crashes inside its OIS HID library, while SDL only captures the T19's startup state in a background controller process. This project therefore polls the T19 directly through HIDAPI and does not enable either faulty path. This workaround affects only pilot input; simulation and sensor timestamps still come from Webots.

## Generated data

Each run creates `logs/<UTC timestamp>/` containing:

- `telemetry.csv` (then `telemetry_001.csv`, and so on): control-step and host timestamps, pose, velocity, IMU, pilot commands, requested rates, motor targets, input/failsafe state, raw joystick axes, recovery count, and collision state. New runs are capped at four 64 MiB segments so a forgotten session cannot fill the disk.
- `rgb_initial.png`: the first RGB observation.
- `depth_initial.png`: a viewable depth preview.
- `depth_initial.f32`: the same first depth observation as native-endian float32 metres.
- `depth_initial.json`: width, height, units, encoding, and camera parameters for the raw depth file.

The first RGB and depth frames share the timestamp recorded in `depth_initial.json`; both sensors then stop unless continuous sampling was explicitly enabled. Continuous image recording and outage emulation are the next milestones; they are intentionally not mixed into installation validation.

## Verify the setup

Run the Python unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Run a short headless simulator smoke test:

```bash
./scripts/smoke_test.sh
```

The smoke test runs invisibly in the background, uses a free network port, times out safely, and exits after 6.4 simulated seconds. It checks the controller, pose, IMU, takeoff thrust, rate damping, telemetry schema and timing, PNG validity and dimensions, and raw depth metadata and byte count.

Run the full flight-dynamics acceptance suite:

```bash
./scripts/flight_dynamics_test.py
```

It launches isolated Webots instances without taking macOS focus and checks direction, rates, response and braking time, reversals, Acro attitude retention, mixed inputs, throttle range, arming, landing, recovery, collision survival, signal-loss and emergency-stop latches, invalid re-arm timing, and deterministic stress behavior.

## Project structure

```text
config/controller.json
controllers/dream_mode_controller/
  dream_mode_controller.py
  input_mapping.py
  runtime.ini
protos/FpvResearchQuad.proto
requirements.txt
scripts/
  run_webots.sh
  flight_dynamics_test.py
  smoke_test.sh
tests/test_controller_config.py
tests/test_controller_failsafe.py
tests/test_input_mapping.py
tests/test_world_layout.py
worlds/dream_mode_research.wbt
```

## Current research boundary

This setup is suitable for engineering validation of the control and data path. It is not yet a physically validated real-drone model or ready for a human-subject experiment. The airframe still inherits approximate Crazyflie coefficients; its fast Webots rotor setting is a simulator response parameter, not a measured motor torque. Airframe system identification, continuous image recording, outage conditions, predictive display, uncertainty cues, randomized protocol, and the pilot-safety termination rule remain future milestones.
