# Heat Exchanger Controller — Design

**Date:** 2026-05-04
**Status:** Approved
**Audience:** Personal use (not intended for publication)

---

## Problem

The house has district heating with a heat exchanger whose outdoor temperature probe has been replaced with a WiFi-controlled software potentiometer. By setting the potentiometer value (in °C), the heat exchanger can be tricked into producing more or less heat. The potentiometer reacts almost immediately to changes.

The current control approach — a PID controller that nudges the fake outdoor temperature up or down based on indoor temperature error — always lags. Weather changes and solar gain cause indoor temperature to drift before the controller can react, because indoor temperature is slow to respond (thermal mass of the house).

---

## Solution Overview

Cascade control with two loops running at different speeds:

```
SMHI forecast (outdoor temp)   ──┐
Open-Meteo forecast (solar)    ──┤
Real outdoor temp (fallback)   ──┤──► RC-MPC outer loop ──► supply temp setpoint
Indoor temp (aggregated avg)   ──┤       (every 5 min)              │
                                  │                                   │
                                  └───────────────────────           │
                                                                      ▼
Supply line temp sensor ──────────────────────────────► PID inner loop ──► potentiometer value
                                                             (every 30 sec)
```

**Outer loop (RC-MPC):** Models the house as an RC thermal system. At each 5-minute tick, simulates the next N hours forward using forecast outdoor temperature and solar irradiance, then solves analytically for the supply temperature setpoint that keeps indoor temperature at the climate entity's target. Reacts to predicted changes before they affect indoor temperature.

**Inner loop (PID):** Tracks the supply temperature setpoint from the outer loop by adjusting the potentiometer value. Fast, simple, familiar pattern from `simple_pid_controller`.

---

## RC Thermal Model

The house is modelled as a first-order RC system, discretized at 5-minute steps:

```
T_in[k+1] = T_in[k] + (dt / C) × (UA × (T_supply − T_in[k]) + g × I_solar[k] − (T_in[k] − T_out[k]) / R)
```

| Symbol | Meaning | Unit |
|---|---|---|
| `R` | Thermal resistance of envelope (heat loss rate) | °C/W |
| `C` | Thermal capacitance (thermal mass of house) | Wh/°C |
| `UA` | Radiator-to-room heat transfer coefficient | W/°C |
| `g` | Solar aperture (fraction of irradiance becoming indoor heat) | m² |
| `I_solar` | Solar irradiance from Open-Meteo forecast | W/m² |

Since the model is linear in `T_supply`, the supply setpoint that brings `T_in[N]` to the target has an analytical solution — no iterative solver needed.

---

## Data Sources

| Source | Used for | Fallback |
|---|---|---|
| Z2M / WiFi sensors (ordered list) | Outdoor temperature (primary) | Next in list → SMHI current → last known |
| SMHI weather entity | Outdoor temp forecast + final temp fallback | Flat forecast = last known outdoor temp |
| Open-Meteo REST API | Hourly solar irradiance forecast (W/m²) | Assume zero solar gain |
| Indoor temp sensor | Current indoor temperature | Assume indoor = setpoint |
| Supply line temp sensor | Inner PID feedback | Disable inner PID, use outer loop output directly |

**Outdoor temp fallback chain:** An ordered list of entity IDs is configured. The component tries each in sequence, falls through to the SMHI entity current observation, then to the last known value. The active source is always reported in a sensor entity.

**Open-Meteo:** Fetched directly by the component (`api.open-meteo.com/v1/forecast?hourly=shortwave_radiation`). No API key required. Latitude/longitude auto-populated from HA config.

---

## Fault Handling & Degradation

Every data source has a defined fallback. The system never hard-fails — it degrades gracefully.

| Degradation tier | Condition | Behaviour |
|---|---|---|
| **Full** | All sources available | Full RC-MPC + inner PID |
| **Degraded — no solar** | Open-Meteo unavailable | RC-MPC with zero solar gain (conservative) |
| **Degraded — no forecast** | SMHI forecast unavailable | Flat forecast = current outdoor temp |
| **Degraded — no indoor temp** | Indoor sensor unavailable | Assume indoor = setpoint; outer loop runs feedforward-only |
| **Degraded — no supply temp** | Supply line sensor unavailable | Inner PID disabled; potentiometer set directly from outer loop output |
| **Degraded — no outdoor temp** | All outdoor sources unavailable | Outer loop holds last known supply setpoint |
| **Fault — nothing works** | All sources gone | Hold last known potentiometer value; status = FAULT |

**Status sensor** — a single `sensor` entity with state `ok` / `degraded` / `fault`. Attributes list every active fault and which fallback is in use. Wire one HA automation off this entity state to get phone notifications.

---

## Exposed Entities

### Climate entity
The primary user-facing control surface.
- `target_temperature` — indoor temperature setpoint
- `current_temperature` — live reading from configured indoor sensor
- `hvac_mode` — heat / cool / off
- `hvac_action` — heating / cooling / idle (what is actually happening)

### Sensors
| Entity | Description |
|---|---|
| `status` | ok / degraded / fault + fault detail attributes |
| `supply_temp_setpoint` | Supply temperature the outer loop is currently requesting |
| `predicted_indoor_temp` | Where the RC model predicts indoor temp will be in N hours |
| `outdoor_temp_source` | Which sensor in the fallback chain is currently active |
| `solar_irradiance` | Current Open-Meteo value in W/m² |

### Numbers (tunable parameters)
| Entity | Default |
|---|---|
| RC — thermal resistance `R` | — |
| RC — thermal capacitance `C` | — |
| RC — radiator coefficient `UA` | — |
| RC — solar aperture `g` | — |
| PID — Kp | — |
| PID — Ki | — |
| PID — Kd | — |
| Supply temp min | — |
| Supply temp max | — |
| Potentiometer output min | — |
| Potentiometer output max | — |
| Prediction horizon (hours) | 2 |
| Outer loop interval (minutes) | 5 |
| Inner loop interval (seconds) | 30 |

---

## Configuration Flow

### Initial setup
1. Instance name
2. Indoor temperature sensor entity
3. Outdoor temperature sensors — ordered list (primary first, e.g. Z2M → WiFi → SMHI)
4. Supply line temperature sensor entity
5. Potentiometer entity (WiFi-controlled device)
6. SMHI weather entity (forecast + final outdoor temp fallback)
7. Latitude / longitude (auto-populated from HA config)

### Options flow (reconfigurable without restart)
- All 14 number parameters
- Climate entity min/max temperature bounds

---

## Component Structure

Follows the `simple_pid_controller` pattern already present in the repo.

```
heat_exchanger_controller/
├── __init__.py          # setup_entry, unload_entry, coordinators
├── manifest.json
├── const.py
├── config_flow.py       # ConfigFlow + OptionsFlow
├── coordinator.py       # OuterLoopCoordinator (5 min) + InnerLoopCoordinator (30 sec)
├── climate.py           # Climate entity
├── sensor.py            # Status + diagnostic sensors
├── number.py            # Tunable parameters
├── strings.json
└── translations/
    └── en.json
```

Two coordinators:
- **OuterLoopCoordinator**: fetches SMHI forecast + Open-Meteo, runs RC-MPC, produces supply temp setpoint
- **InnerLoopCoordinator**: runs PID against actual supply temp, writes potentiometer entity
