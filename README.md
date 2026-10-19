# Heat Exchanger Controller

A Home Assistant integration that runs a district-heating substation with model-predictive control instead of a fixed heat curve.

Most district-heating heat exchangers pick their supply temperature from an outdoor-temperature probe and a heat curve. This integration replaces that probe with a WiFi-controlled digital potentiometer. Home Assistant then decides what "outdoor temperature" the heat exchanger sees, and through that what supply temperature it produces.

> [!WARNING]
> This controls real heating. It is a personal project, tuned on one house, and still experimental. It starts in **dry run** mode (it computes everything but writes nothing). Only turn dry run off when you can watch it, and keep a way to hand control back to the original probe.

## How it works

It's a cascade of two loops:

```
weather forecast ─┐
solar forecast ───┼─► Outer loop: RC model + MPC ──► supply setpoint ──► Inner loop: PID ──► potentiometer ──► heat exchanger
indoor / outdoor ─┘      (every 5 min)                    (°C)            (every 30 s)      (fake outdoor °C)
                                                                              ▲
                                                           supply line temp ──┘
```

- **Outer loop (RC-MPC).** The house is modelled as a single thermal mass with four parameters: radiator conductance `UA`, envelope resistance `R`, capacity `C` and solar aperture `g`. Each tick the model is stepped forward over the prediction horizon using the hourly outdoor forecast (from a `weather` entity) and solar irradiance (from [Open-Meteo](https://open-meteo.com/), no API key). The integration then solves in closed form for the supply temperature that brings the room to the target at the end of the horizon.
- **Inner loop (PID).** Drives the potentiometer until the measured supply-line temperature matches that setpoint. It has anti-windup, a bumpless start, pump minimum run time, and a startup guard that holds the output until the first valid setpoint.
- **Model correction.**
  - A leaky **disturbance observer** shifts the effective target to remove short-term model error.
  - An **hour-of-day profile** learns heat that follows the daily schedule, like people, cooking or afternoon sun on one side. You can set a no-pre-heat window, by default 22:00–07:00, during which the profile may only reduce heat, so sleeping rooms aren't pre-heated.
- **Shadow mode.** Per-room models (config subentries) and a path-optimising MPC run alongside the live controller and publish what they *would* do, so you can evaluate them before trusting them.

Every data source has a fallback. A missing forecast, sensor or solar fetch degrades the controller and shows up on the `status` sensor; the controller itself keeps running.

## Requirements

- A heat exchanger or substation controlled by an outdoor-temperature probe that you can replace with a controllable resistance. For example, a WiFi digital potentiometer exposed through a `rest_command`, which receives the fake outdoor temperature as a parameter.
- Temperature sensors for:
  - **indoor** temperature (the controlled variable);
  - the **supply line** (clamp a sensor to the hot pipe);
  - **outdoor** temperature (one or more, in priority order).
- A `weather` entity with hourly forecasts. It was developed with SMHI.
- Home Assistant with a location set (used for the solar forecast). Tested on 2026.4.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/coroiu/heat_exchanger_controller` as type **Integration**.
2. Install **Heat Exchanger Controller** and restart Home Assistant.

### Manual

Copy this repository into `<config>/custom_components/heat_exchanger_controller/` (or clone it there) and restart Home Assistant.

## Setup

**Settings → Devices & Services → Add integration → Heat Exchanger Controller**, then pick:

| Field | Meaning |
|---|---|
| Indoor temperature sensor | The temperature to control. A template that averages rooms works. |
| Outdoor temperature sensors | In priority order; the first available one is used. The weather entity's current observation is the final fallback. |
| Supply line temperature sensor | The measured supply temperature the inner loop tracks. |
| Output service / value parameter | E.g. `rest_command.set_heat_exchanger_temp` with parameter `temp`. Called with the fake outdoor temperature. |
| Potentiometer readback (optional) | A sensor reporting the value the potentiometer currently outputs; used for a bumpless start. |
| Weather entity | Hourly outdoor forecast. |

You can change these later via **Reconfigure**. Rooms for shadow mode are added with **Add room** on the integration.

## Going live

1. Leave **dry run** on for a day or so. Check that `supply_temp_setpoint`, `predicted_indoor_temp` and `status` look sane.
2. **Calibrate the RC model** from your own history (see below). The defaults are only placeholders.
3. Turn dry run off while you can watch it. The PID integral is reset when you do, and the output continues from the current potentiometer value.
4. Tune from the number entities. RC parameters, PID gains, limits and intervals reload the integration. Target, observer and profile settings apply on the next tick.

## Entities (overview)

- **Climate:** target temperature and heat/off.
- **Sensors:**
  - outer loop: status, controller action, supply setpoint;
  - inner loop: actual supply, potentiometer output, pump state, PID terms;
  - inputs: indoor, outdoor (and its source), solar, forecast;
  - predicted indoor temperature;
  - model diagnostics: indoor rate actual vs model and their mismatch, current hour-profile correction.
- **Numbers:** RC parameters, PID gains, supply/output limits, horizon, intervals, pump cycling, observer settings, hour-profile no-pre-heat window.
- **Switches:** dry run, hour-of-day correction on/off, room scoring pause.
- **Per room (shadow):** target, importance, RC parameters, predicted and planned temperature, own supply setpoint, prediction error.

`docs/dashboard.yaml` is an example dashboard. Replace its entity IDs with your own.

## Calibrating with the offline tools

`tools/` contains numpy/scipy scripts that fit the RC parameters from the Home Assistant recorder database. They find the database, your location and your sensors from the integration's config entry, so no editing is needed when the repository sits in `custom_components/`. Otherwise set `HA_CONFIG` to your config directory (overrides: `HA_DB`, `HA_LAT`, `HA_LON`).

```bash
cd tools
python3 -m venv .venv && .venv/bin/pip install numpy scipy
.venv/bin/python fit_rc_multistep.py 14 3          # 14 days of history, fit on 3 h-ahead error
.venv/bin/python fit_rc_multistep.py 14 3 sensor.bedroom_temperature   # one room
```

The fit prints UA, R and g for an assumed C of 500 Wh/K. Only the ratios are identifiable from temperature data. To drop periods that don't reflect normal heating (open windows, a fireplace), list them in `tools/exclude.local.json`:

```json
[["2026-01-10T19:30:00+01:00", "2026-01-10T22:30:00+01:00", "window open"]]
```

`replay_rooms.py` and `replay_path.py` replay history through the room and path models.

## Design notes

The design documents are in [`docs/plans/`](docs/plans/). [`CLAUDE.md`](CLAUDE.md) has a detailed walkthrough of the architecture and the reasoning behind each mechanism.

## License

[MIT](LICENSE)
