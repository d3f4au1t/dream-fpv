"""Pure input and motor-mixing helpers independent from the Webots runtime."""

from math import sqrt


def clamp(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def hid_byte_to_signed(raw_byte: int, axis_range: int = 32767) -> int:
    """Convert an unsigned eight-bit HID axis to a signed centered value."""
    if not 0 <= raw_byte <= 255:
        raise ValueError("raw_byte must be in [0, 255]")
    if axis_range <= 0:
        raise ValueError("axis_range must be positive")
    return round(((raw_byte / 255.0) * 2.0 - 1.0) * axis_range)


def normalize_axis(
    raw_value: int,
    axis_range: int = 32767,
    *,
    invert: bool = False,
    deadzone: float = 0.0,
) -> float:
    """Map a signed joystick axis to [-1, 1] and apply a scaled deadzone."""
    if axis_range <= 0:
        raise ValueError("axis_range must be positive")
    if not 0.0 <= deadzone < 1.0:
        raise ValueError("deadzone must be in [0, 1)")

    value = clamp(raw_value / axis_range, -1.0, 1.0)
    if invert:
        value = -value

    magnitude = abs(value)
    if magnitude <= deadzone:
        return 0.0
    scaled = (magnitude - deadzone) / (1.0 - deadzone)
    return scaled if value > 0 else -scaled


def normalize_throttle(
    raw_value: int,
    axis_range: int = 32767,
    *,
    invert: bool = False,
) -> float:
    """Map a signed joystick axis to the throttle interval [0, 1]."""
    centered = normalize_axis(raw_value, axis_range, invert=invert)
    return clamp((centered + 1.0) / 2.0, 0.0, 1.0)


def shape_throttle(throttle: float, exponent: float) -> float:
    """Apply a monotonic throttle curve while preserving both endpoints."""
    if exponent <= 0.0:
        raise ValueError("throttle exponent must be positive")
    return clamp(throttle, 0.0, 1.0) ** exponent


def actual_rate_setpoint(
    stick: float,
    center_rate_deg_s: float,
    max_rate_deg_s: float,
    expo: float,
) -> float:
    """Apply Betaflight's Actual Rates curve to a normalized stick value."""
    if center_rate_deg_s < 0.0:
        raise ValueError("center_rate_deg_s must be non-negative")
    if max_rate_deg_s < center_rate_deg_s:
        raise ValueError("max_rate_deg_s must be at least center_rate_deg_s")
    if not 0.0 <= expo <= 1.0:
        raise ValueError("expo must be in [0, 1]")

    stick = clamp(stick, -1.0, 1.0)
    expo_factor = abs(stick) * (
        stick**5 * expo + stick * (1.0 - expo)
    )
    return (
        stick * center_rate_deg_s
        + (max_rate_deg_s - center_rate_deg_s) * expo_factor
    )


def betaflight_rate_setpoint(
    stick: float,
    rc_rate: float,
    super_rate: float,
    expo: float,
) -> float:
    """Apply Betaflight's legacy RC Rate / Super Rate / Expo curve."""
    if rc_rate < 0.0:
        raise ValueError("rc_rate must be non-negative")
    if not 0.0 <= super_rate < 1.0:
        raise ValueError("super_rate must be in [0, 1)")
    if not 0.0 <= expo <= 1.0:
        raise ValueError("expo must be in [0, 1]")

    stick = clamp(stick, -1.0, 1.0)
    magnitude = abs(stick)
    curved_stick = stick * (expo * magnitude**3 + 1.0 - expo)
    effective_rc_rate = rc_rate
    if effective_rc_rate > 2.0:
        effective_rc_rate += 14.54 * (effective_rc_rate - 2.0)
    super_factor = 1.0 / clamp(
        1.0 - magnitude * super_rate,
        0.01,
        1.0,
    )
    return 200.0 * effective_rc_rate * curved_stick * super_factor


def throttle_to_motor_speed(
    throttle: float,
    idle_speed: float,
    max_speed: float,
) -> float:
    """Map linear commanded thrust to speed for Webots' quadratic propellers."""
    if idle_speed < 0.0:
        raise ValueError("idle_speed must be non-negative")
    if max_speed <= idle_speed:
        raise ValueError("max_speed must be greater than idle_speed")
    throttle = clamp(throttle, 0.0, 1.0)
    return sqrt(idle_speed**2 + throttle * (max_speed**2 - idle_speed**2))


def desaturate_motor_mix(
    base_speed: float,
    corrections: tuple[float, float, float, float],
    minimum_speed: float,
    maximum_speed: float,
) -> tuple[float, float, float, float]:
    """Shift and, if necessary, scale a quad mix while preserving authority."""
    if maximum_speed <= minimum_speed:
        raise ValueError("maximum_speed must be greater than minimum_speed")

    available = maximum_speed - minimum_speed
    low = min(corrections)
    high = max(corrections)
    span = high - low
    if span > available:
        scale = available / span
        corrections = tuple(value * scale for value in corrections)
        low *= scale
        high *= scale

    shifted_base = clamp(
        base_speed,
        minimum_speed - low,
        maximum_speed - high,
    )
    return tuple(
        clamp(shifted_base + value, minimum_speed, maximum_speed)
        for value in corrections
    )


def thrust_mix_to_motor_speeds(
    throttle: float,
    corrections: tuple[float, float, float, float],
    idle_speed: float,
    max_speed: float,
) -> tuple[float, float, float, float]:
    """Mix normalized thrust, then convert each motor command to rotor speed."""
    motor_thrusts = desaturate_motor_mix(
        clamp(throttle, 0.0, 1.0),
        corrections,
        0.0,
        1.0,
    )
    return tuple(
        throttle_to_motor_speed(value, idle_speed, max_speed)
        for value in motor_thrusts
    )
