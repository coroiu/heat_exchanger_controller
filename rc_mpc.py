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
    prediction_supply: float | None = None,
    dt_hours: float = 1.0,
    extra: list[float] | None = None,
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
        extra: Optional per-step temperature change (°C) added to the disturbance,
            e.g. the hour-of-day profile. Used by both the solve and the prediction.
    """
    N = len(outdoor_temps)
    if N == 0:
        return _clamp(t_setpoint, supply_min, supply_max), t_in

    # Per-step coefficients scaled by dt
    a = (UA + 1.0 / R) / C * dt_hours
    b = UA / C * dt_hours

    # Disturbance per step: contribution from solar gain + outdoor temp, scaled by dt
    d = [(g * solar_irradiance[k] + outdoor_temps[k] / R) / C * dt_hours for k in range(N)]
    if extra is not None:
        d = [d[k] + extra[k] for k in range(N)]

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

    # prediction_supply lets the caller override the simulated supply (e.g. t_in when pump is off)
    effective_supply = prediction_supply if prediction_supply is not None else t_supply_clamped
    predicted = _simulate_forward(t_in, outdoor_temps, solar_irradiance, effective_supply, a, b, d)

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


def model_inputs(
    horizon_h: float,
    outdoor_forecast: list[float],
    solar_forecast: list[float],
    fallback_outdoor: float,
) -> tuple[list[float], list[float], float]:
    """Return (outdoor_temps, solar_irradiance, dt_hours) for a horizon.

    Sub-hourly horizons use a single step of dt = horizon; ≥1 h uses integer hourly
    steps, padded with the last forecast value when the forecast is short.
    """
    if horizon_h < 1.0:
        return outdoor_forecast[:1] or [fallback_outdoor], solar_forecast[:1] or [0.0], horizon_h
    n_steps = max(1, round(horizon_h))
    fallback_t = outdoor_forecast[-1] if outdoor_forecast else fallback_outdoor
    fallback_s = solar_forecast[-1] if solar_forecast else 0.0
    return (
        (outdoor_forecast + [fallback_t] * n_steps)[:n_steps],
        (solar_forecast + [fallback_s] * n_steps)[:n_steps],
        1.0,
    )
