"""RC thermal model + analytical MPC solver.

No Home Assistant imports — pure Python, independently testable.

Model (first-order RC, dt = 1 hour):
    T_in[k+1] = T_in[k] + (1/C) * (UA*(T_s - T_in[k]) + g*I[k] - (T_in[k] - T_out[k])/R)

Solving analytically for the constant T_supply that brings T_in[N] to T_setpoint.
"""

from __future__ import annotations


def compute_supply_setpoint(
    t_in: float,
    t_setpoint: float,
    outdoor_temps: list[float],
    solar_irradiance: list[float],
    R: float,
    C: float,
    UA: float,
    g: float,
    supply_min: float,
    supply_max: float,
) -> tuple[float, float]:
    """Return (supply_setpoint, predicted_t_in_at_horizon).

    Args:
        t_in: Current indoor temperature (°C).
        t_setpoint: Target indoor temperature (°C).
        outdoor_temps: Hourly outdoor temperatures, length N (°C).
        solar_irradiance: Hourly solar irradiance, length N (W/m²).
        R: Thermal resistance of envelope (°C/W).
        C: Thermal capacitance (Wh/°C).
        UA: Radiator-to-room heat transfer coefficient (W/°C).
        g: Solar aperture (m²).
        supply_min: Minimum allowed supply temperature (°C).
        supply_max: Maximum allowed supply temperature (°C).
    """
    N = len(outdoor_temps)
    if N == 0:
        return _clamp(t_setpoint, supply_min, supply_max), t_in

    # Per-step coefficients (dt = 1 hour)
    a = (UA + 1.0 / R) / C
    b = UA / C

    # Disturbance per step: contribution from solar gain + outdoor temp
    d = [(g * solar_irradiance[k] + outdoor_temps[k] / R) / C for k in range(N)]

    # Protect against degenerate parameters
    if abs(b) < 1e-9 or abs(a) < 1e-9:
        return _clamp(t_setpoint, supply_min, supply_max), t_in

    decay = 1.0 - a

    # A = decay^N (how much of the initial error persists after N steps)
    A = decay ** N

    # B = b * (1 - decay^N) / a  (cumulative supply gain over N steps)
    B = b * (1.0 - A) / a

    # D = sum_{k=0}^{N-1} decay^(N-1-k) * d[k]  (cumulative disturbance)
    D = sum(decay ** (N - 1 - k) * d[k] for k in range(N))

    if abs(B) < 1e-9:
        # Can't compute setpoint; return conservative bound
        return (supply_min if t_in < t_setpoint else supply_max), t_in

    t_supply = (t_setpoint - A * t_in - D) / B
    t_supply_clamped = _clamp(t_supply, supply_min, supply_max)

    predicted = _simulate_forward(t_in, outdoor_temps, solar_irradiance, t_supply_clamped, a, b, d)

    return t_supply_clamped, predicted


def _simulate_forward(
    t_in: float,
    outdoor_temps: list[float],
    solar_irradiance: list[float],
    t_supply: float,
    a: float,
    b: float,
    d: list[float],
) -> float:
    """Simulate the RC model forward N steps and return T_in[N]."""
    t = t_in
    for k in range(len(outdoor_temps)):
        t = (1.0 - a) * t + b * t_supply + d[k]
    return t


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
