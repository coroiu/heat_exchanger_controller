#!/usr/bin/env python3
"""
Estimate RC thermal model parameters from Home Assistant recorder history.

The model (first-order RC, dt = 1 hour):
    ΔT_in = (UA/C)*(T_supply - T_in) + (g/C)*I_solar + (1/RC)*(T_out - T_in)

Three ratios are identifiable by OLS regression: UA/C, g/C, and 1/(RC).
C itself cannot be determined from temperature data alone. Specify --c to
assume a value (Wh/°C) and the script back-calculates R, UA, g.

Solar irradiance is fetched from the Open-Meteo free archive API (no key
required) since the component may not have been running long enough to have
recorder history for it.

Usage:
    python3 estimate_rc.py [options]

Examples:
    # Run with all defaults (DB, location and entities from the local HA config):
    python3 estimate_rc.py

    # Use a different C assumption and a shorter window:
    python3 estimate_rc.py --c 800 --days 30

    # Override entity IDs:
    python3 estimate_rc.py --indoor sensor.my_indoor --outdoor sensor.my_outdoor
"""

import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

try:
    import numpy as np
except ImportError:
    print("numpy is required: pip3 install numpy")
    sys.exit(1)


# --- Defaults from the local HA config (see ha_local.py) ---
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ha_local
_DB      = ha_local.db_path()
_INDOOR  = ha_local.indoor_entity()
_OUTDOOR = ha_local.outdoor_entity()
_SUPPLY  = ha_local.supply_entity()
_LAT, _LON = ha_local.location()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db",      default=_DB,      metavar="PATH", help=f"Recorder DB path (default: {_DB})")
    p.add_argument("--indoor",  default=_INDOOR,  metavar="ENTITY", help=f"Indoor temp entity (default: {_INDOOR})")
    p.add_argument("--outdoor", default=_OUTDOOR, metavar="ENTITY", help=f"Outdoor temp entity (default: {_OUTDOOR})")
    p.add_argument("--supply",  default=_SUPPLY,  metavar="ENTITY", help=f"Supply line temp entity (default: {_SUPPLY})")
    p.add_argument("--lat",     default=_LAT,     type=float, help=f"Latitude for solar fetch (default: {_LAT})")
    p.add_argument("--lon",     default=_LON,     type=float, help=f"Longitude for solar fetch (default: {_LON})")
    p.add_argument("--days",    default=60,       type=int,   help="Days of history to use (default: 60)")
    p.add_argument("--c",       default=500.0,    type=float, metavar="WHR_PER_C",
                   help="Assumed thermal capacitance C in Wh/°C (default: 500)")
    p.add_argument("--min-supply", default=25.0,  type=float, dest="min_supply", metavar="DEGC",
                   help="Exclude hours where supply temp < this to skip off/idle periods (default: 25)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# HA recorder access
# ---------------------------------------------------------------------------

def _get_metadata_id(conn: sqlite3.Connection, entity_id: str) -> int | None:
    row = conn.execute(
        "SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return row[0] if row else None


def fetch_history(
    conn: sqlite3.Connection, entity_id: str, start_ts: float, end_ts: float
) -> list[tuple[float, float]]:
    """Return (unix_ts, float_value) pairs, sorted by time, for one entity."""
    mid = _get_metadata_id(conn, entity_id)
    if mid is None:
        return []
    rows = conn.execute(
        "SELECT last_updated_ts, state FROM states "
        "WHERE metadata_id = ? AND last_updated_ts BETWEEN ? AND ? "
        "ORDER BY last_updated_ts",
        (mid, start_ts, end_ts),
    ).fetchall()
    result: list[tuple[float, float]] = []
    for ts, state in rows:
        try:
            result.append((float(ts), float(state)))
        except (ValueError, TypeError):
            pass
    return result


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def resample_hourly(
    data: list[tuple[float, float]], start_ts: float, end_ts: float
) -> dict[float, float]:
    """Forward-fill sensor readings to whole-hour UTC timestamps.

    Returns {timestamp: value} where timestamp is on the hour boundary.
    """
    # Snap to the first whole hour after start_ts
    t = (int(start_ts / 3600) + 1) * 3600.0
    out: dict[float, float] = {}
    last_val: float | None = None
    i = 0
    while t <= end_ts:
        while i < len(data) and data[i][0] <= t:
            last_val = data[i][1]
            i += 1
        if last_val is not None:
            out[t] = last_val
        t += 3600.0
    return out


# ---------------------------------------------------------------------------
# Solar irradiance from Open-Meteo archive
# ---------------------------------------------------------------------------

def fetch_solar(lat: float, lon: float, start_date: str, end_date: str) -> dict[float, float]:
    """Fetch hourly shortwave radiation (W/m²) from Open-Meteo archive.

    Returns {unix_ts: W/m²}. Falls back to empty dict on failure so the
    regression still runs (p2/g will be unreliable without solar data).
    """
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        "&hourly=shortwave_radiation&timezone=UTC"
    )
    print("  Fetching solar irradiance from Open-Meteo archive ... ", end="", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            payload = json.loads(resp.read())
        times     = payload["hourly"]["time"]
        radiation = payload["hourly"]["shortwave_radiation"]
        result: dict[float, float] = {}
        for t_str, w in zip(times, radiation):
            try:
                dt = datetime.strptime(t_str, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
                result[dt.timestamp()] = float(w) if w is not None else 0.0
            except (ValueError, TypeError):
                pass
        print(f"OK ({len(result)} hours)")
        return result
    except Exception as exc:
        print(f"FAILED ({exc})")
        print("  WARNING: Proceeding with zero solar. g estimate will be unreliable.")
        return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    end_ts     = datetime.now(timezone.utc).timestamp()
    start_ts   = end_ts - args.days * 86400.0
    start_date = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    end_date   = datetime.fromtimestamp(end_ts,   tz=timezone.utc).strftime("%Y-%m-%d")

    print()
    print("RC Parameter Estimation")
    print("=======================")
    print(f"Database   : {args.db}")
    print(f"Period     : {start_date} → {end_date} ({args.days} days)")
    print(f"Indoor     : {args.indoor}")
    print(f"Outdoor    : {args.outdoor}")
    print(f"Supply     : {args.supply}")
    print(f"C assumed  : {args.c} Wh/°C")
    print(f"Min supply : {args.min_supply} °C (hours below this are skipped)")
    print()

    # --- Load recorder data ---
    print("Loading sensor history ...")
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    raw_in  = fetch_history(conn, args.indoor,  start_ts, end_ts)
    raw_out = fetch_history(conn, args.outdoor, start_ts, end_ts)
    raw_sup = fetch_history(conn, args.supply,  start_ts, end_ts)
    conn.close()

    ok = True
    for label, entity_id, raw in [
        ("indoor",  args.indoor,  raw_in),
        ("outdoor", args.outdoor, raw_out),
        ("supply",  args.supply,  raw_sup),
    ]:
        if raw:
            print(f"  {label:8s} : {len(raw):6d} readings")
        else:
            print(f"  {label:8s} : NO DATA — entity not found: {entity_id}")
            ok = False
    if not ok:
        print("\nERROR: One or more sensors returned no data. Check entity IDs.")
        sys.exit(1)
    print()

    # --- Resample to hourly ---
    in_h  = resample_hourly(raw_in,  start_ts, end_ts)
    out_h = resample_hourly(raw_out, start_ts, end_ts)
    sup_h = resample_hourly(raw_sup, start_ts, end_ts)

    # --- Solar from Open-Meteo ---
    solar_h = fetch_solar(args.lat, args.lon, start_date, end_date)
    print()

    # --- Build aligned regression dataset ---
    # Need consecutive hours (k, k+1) for ΔT_in = T_in[k+1] - T_in[k]
    skipped_missing = 0
    skipped_supply  = 0
    delta_t_list: list[float] = []
    sup_minus_in_list: list[float] = []
    solar_list: list[float] = []
    out_minus_in_list: list[float] = []

    for t in sorted(in_h.keys()):
        t_next  = t + 3600.0
        t_in_k  = in_h.get(t)
        t_in_k1 = in_h.get(t_next)
        t_out   = out_h.get(t)
        t_sup   = sup_h.get(t)
        solar   = solar_h.get(t, 0.0)

        if None in (t_in_k, t_in_k1, t_out, t_sup):
            skipped_missing += 1
            continue
        if t_sup < args.min_supply:
            skipped_supply += 1
            continue

        delta_t_list.append(t_in_k1 - t_in_k)
        sup_minus_in_list.append(t_sup - t_in_k)
        solar_list.append(solar)
        out_minus_in_list.append(t_out - t_in_k)

    n = len(delta_t_list)
    print(
        f"Usable hourly intervals : {n}"
        f"  (skipped: {skipped_missing} missing data, {skipped_supply} below supply threshold)"
    )

    if n < 48:
        print(f"\nERROR: Too few samples ({n}). Increase --days or lower --min-supply.")
        sys.exit(1)
    if n < 200:
        print(f"WARNING: Only {n} samples — results may be noisy. More history is better.")
    print()

    # --- OLS regression ---
    X = np.column_stack([sup_minus_in_list, solar_list, out_minus_in_list])
    y = np.array(delta_t_list)
    params, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    p1, p2, p3 = float(params[0]), float(params[1]), float(params[2])

    y_hat  = X @ params
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - float(np.mean(y))) ** 2))
    r2     = 1.0 - ss_res / ss_tot if ss_tot > 1e-10 else 0.0

    print("Fitted ratios  (all per °C·h, dt = 1 h)")
    print("----------------------------------------")
    print(f"  UA/C  = {p1:+.6f}   supply → room heat transfer per unit thermal mass")
    print(f"  g/C   = {p2:+.6f}   solar aperture per unit thermal mass (W/m² → °C/h)")
    print(f"  1/RC  = {p3:+.6f}   envelope heat loss per unit thermal mass")
    print(f"  R²    = {r2:.3f}")
    print()

    # --- Warnings ---
    warnings: list[str] = []
    if p1 <= 0:
        warnings.append("UA/C ≤ 0: supply heating term has wrong sign. Check supply sensor ID.")
    if p2 < -0.001:
        warnings.append("g/C < 0: solar term is negative. Will be clamped to 0 in output.")
    if p3 <= 0:
        warnings.append("1/RC ≤ 0: envelope loss term has wrong sign. Check outdoor sensor ID.")
    if r2 < 0.40:
        warnings.append(
            f"R² = {r2:.2f} is low. Try a longer window or different entity IDs. "
            "Low variation in supply temp also reduces identifiability."
        )
    for w in warnings:
        print(f"WARNING: {w}")
    if warnings:
        print()

    # --- Suggested physical parameters ---
    if p1 > 1e-9 and p3 > 1e-9:
        C  = args.c
        UA = p1 * C
        g  = max(0.0, p2 * C)
        R  = 1.0 / (p3 * C)

        print(f"Suggested parameters  (with C = {C:.0f} Wh/°C)")
        print(f"----------------------------------------------")
        print(f"  R  = {R:.4f}   °C/W     (current default: 0.05)")
        print(f"  C  = {C:.1f}    Wh/°C   (assumed — see note below)")
        print(f"  UA = {UA:.3f}   W/°C    (current default: 20.0)")
        print(f"  g  = {g:.3f}   m²       (current default: 5.0)")
        print()
        print("Note: C is not identifiable from temperature data alone.")
        print(f"      At 2 Wh/(m³·°C), {C:.0f} Wh/°C ≈ {C/2:.0f} m³ internal volume")
        print( "      (floor area × ceiling height — not floor area alone).")
        print("      R, UA, and g all scale with C: if you change C, multiply UA and g")
        print(f"      by (new_C / {C:.0f}) and divide R by the same factor.")
    else:
        print("Cannot suggest physical parameters — see warnings above.")


if __name__ == "__main__":
    main()
