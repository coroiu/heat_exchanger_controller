# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Install-specific notes (hardware, entity IDs, calibration history, incidents) live in `CLAUDE.local.md`, which is gitignored. Logs and other local data go in `private/`, also gitignored. Keep household, location and network details out of tracked files and commit messages.

## What this is

A Home Assistant custom component that controls district heating by replacing the heat exchanger's outdoor temperature probe with a WiFi-controlled software potentiometer. It uses a cascade control architecture: an outer RC-MPC loop predicts the required supply temperature using a thermal model of the house plus weather forecasts, and an inner PID loop drives the potentiometer to hit that supply temperature.

Full design rationale is in `docs/plans/2026-05-04-heat-exchanger-controller-design.md`.

The repository root is the integration directory (`hacs.json` has `content_in_root: true`), so a git checkout can sit directly in `<config>/custom_components/heat_exchanger_controller/`.

## Syntax checking

There is no build system or test runner. Verify Python files with:

```bash
python3 -c "import ast; ast.parse(open('filename.py').read()); print('OK')"
```

To check all files at once:
```bash
python3 -c "
import ast
for f in ['const.py','rc_mpc.py','room_model.py','hour_profile.py','path_mpc.py','pid.py','config_flow.py','coordinator.py','__init__.py','climate.py','sensor.py','number.py','switch.py','binary_sensor.py']:
    ast.parse(open(f).read()); print('OK:', f)
"
```

After changes, restart HA and check the logs.

## Strings / translations

`strings.json` and `translations/en.json` must always be identical. After editing `strings.json`:
```bash
cp strings.json translations/en.json
```

## Architecture

### Two pure-Python modules (no HA imports)

- **`rc_mpc.py`** — `compute_supply_setpoint()`: analytical RC thermal model solver. Takes current indoor temp, effective target (already corrected for disturbance), N forecast values for outdoor temp and solar irradiance, four RC parameters (R, C, UA, g), and optional `dt_hours` (model step size, default 1.0) and `prediction_supply` (override for the prediction simulation only — used when pump is off). Returns `(supply_setpoint, predicted_indoor_at_horizon)`. Uses a closed-form linear recurrence — no iterative solver.
- **`pid.py`** — `SimplePID`: discrete PID with back-calculation anti-windup. Takes `(measurement, dt_seconds)`.

These are independently testable without a running HA instance.

### RC model step size and horizon

The model equation uses `dt_hours` as the per-step duration. `a = (UA + 1/R)/C * dt_hours`, `b = UA/C * dt_hours`, disturbance `d` also scaled by `dt_hours`.

- Horizon **< 1 h**: single step with `dt_hours = horizon_h`. More aggressive — requests higher supply for the same deficit.
- Horizon **≥ 1 h**: integer steps of `dt_hours = 1.0`, N = round(horizon_h). Current weather forecasts used per step.

The prediction horizon is a float (step 0.5, min 0.5 h). SMHI and Open-Meteo always fetch `ceil(horizon)` hourly values regardless of step size.

### Two coordinators

- **`OuterCoordinator`** (default 5 min): Fetches the hourly weather forecast via `weather.get_forecasts` service, fetches Open-Meteo solar irradiance via REST (raw arrays cached 45 min, slice recomputed each tick), resolves outdoor temp from a prioritised fallback chain, runs the disturbance observer update, runs the RC-MPC solver. Produces `OuterData`. Holds `self._inner` reference (set by `__init__.py` after both coordinators are constructed) to read pump state for prediction.
- **`InnerCoordinator`** (default 30 sec): Reads `OuterData.supply_setpoint`, runs `SimplePID` against the actual supply line sensor, writes the result to the potentiometer via a configured service call. Produces `InnerData`.

**Critical HA pattern**: The override method in `DataUpdateCoordinator` is `_async_update_data` (leading underscore). Using `async_update_data` (no underscore) silently falls through to the base class which raises `NotImplementedError`.

### Disturbance observer (offset-free MPC)

The RC model is feedforward-only, so any model mismatch (wrong parameters, imperfect supply tracking, unmodelled heat sources) causes a persistent indoor temperature offset. The disturbance observer corrects this without changing the model parameters.

**Algorithm** (runs in `OuterCoordinator._async_update_data` every outer tick, frozen when `hvac_mode=off` or indoor unavailable):

```
indoor_error = actual_indoor - target
d_hat *= exp(−elapsed_h / 3)          # leak toward 0, τ = DISTURBANCE_LEAK_TAU_H
d_hat += (1 − α) × (−indoor_error)   # integrator; (1 − α) = gain per outer tick
d_hat = clamp(d_hat, −cap, +cap)       # cap = disturbance_max option, default 0.3
effective_target = target + d_hat
```

The MPC solver receives `effective_target` instead of the raw target. The prediction is then corrected back: `predicted_corrected = predicted_raw − d_hat`, so the displayed predicted indoor temperature represents the best estimate of actual future indoor temperature.

**Interpretation of `d_hat`:**
- Positive: model over-predicts warmth (house runs cold) — supply setpoint raised by `d_hat`
- Negative: model under-predicts (house runs warm) — supply setpoint lowered
- Converges to zero when RC parameters are well-calibrated

It is an integrator (since 2026-10-11). Before that it was a low-pass filter (`d_hat = α·d_hat + (1−α)·(−error)`), which settled at `d_hat = −error` and left a residual offset; that became visible when the horizon went 1 h → 3 h, which cut the MPC's correction strength about 3×. `disturbance_alpha` is limited to 0.8–1.0. Below 0.8 the integrator gain is too high. **α = 1.0 freezes d_hat** at its current value; it's used to switch the observer off without code changes (e.g. hold d_hat = 0 for parameter-evaluation nights, so the indoor offset directly shows model error). d_hat is clamped to ±`disturbance_max` (number entity, default 0.3 °C, limit 1.5). The clamp applies on the next observer update.

**Leak (2026-10-18):** d_hat decays toward 0 with τ = 3 h (`DISTURBANCE_LEAK_TAU_H`, by elapsed time, also while the update is frozen; not at α = 1.0). A pure integrator could build up ~0.5 on a cold morning, carry it into a sunny afternoon and unwind only ~0.2/h once the room ran warm. With the leak it's no longer offset-free: at α 0.95 it settles at d_hat ≈ 1.8 × (−error), removing ~65 % of a steady offset (a 0.45 P-only offset leaves −0.16). Persistent bias is for the hour profile and refits; the observer handles the day's deviations.

**Anti-windup (2026-10-18):** the update is skipped when the actuator can't move further in the direction d_hat would push. That's decided by the inner PID's potentiometer *request* (`InnerData.potentiometer_request`, the output before the pump-protection cap; lower = more heat):

- **No less heat possible:** request ≥ `pump_off_threshold` − 0.5. The pump stops, or will once its 20 min minimum run time is up, so a too-warm room can't be cooled further.
- **No more heat possible:** request ≤ `output_min` + 0.5.
- **No heat (supply margin):** the last supply setpoint is less than `observer_no_heat_margin` (default 5 °C) above indoor. This counts as "no less heat possible" alongside the request rule. Without it, a setpoint a few degrees above indoor (no meaningful heat) with the pot just below the pump-off line let d_hat wind down for no physical effect, and then held the MPC low when the room cooled. A margin in supply terms doesn't drift with the heat curve the way a pot level would.

When the PID isn't running (awaiting setpoint, supply sensor fault), it falls back to the supply setpoint being within 0.5 of `supply_min` / `supply_max`. Before this change only that fallback existed, and with a low `supply_min` it never triggered: every setpoint below ~26 already means no heat, so d_hat could wind up during warm spells.

**Planned moves aren't bias (2026-10-27):** the observer handles surprises, not known changes. The hour profile moves the room on purpose: pre-heat puts it above target before a known dip, and a heat-reducing slot lets it sit below target before known warming. So the error is measured from where the profile meant the room to be: `indoor_error = indoor − target − expected_effect`. `expected_effect` (°C, `advance_expected_effect()` in `hour_profile.py`) is a first-order state that relaxes like the room model, (UA + 1/R)/C per hour. It's driven by two inputs: the current slot as the MPC applies it, and the supply the profile added. That supply is the setpoint minus a second solve without the profile (0 while the pump is off). It's persisted with d_hat and shown as the `expected_effect` attribute on the hour-of-day sensor. It decays to 0 when the profile is switched off. Without it, d_hat wound negative during pre-heat and took the heat back just before the dip. A slot that's wrong in size or timing still reaches the observer as a surprise.

**Transient freeze (2026-10-12):** the update is skipped while indoor is already heading toward target fast enough to reach it within the prediction horizon (indoor slope over the last 30 min × horizon ≥ |error|). Otherwise the estimate would build up during a recovery and cause overshoot. A flat or diverging error still integrates, so persistent offsets are still corrected. The freeze can't catch the house giving back stored heat (indoor flat above target), because a single-mass model can't tell that apart from model bias.

`d_hat` is persisted via an HA `Store` (`.storage/heat_exchanger_controller.<entry_id>.observer`): saved (debounced 60 s) on every observer update, flushed on entry unload, and restored in `async_setup_entry` before the first refresh, so it survives reloads (e.g. structural option changes) and restarts. `entry.options[disturbance_estimate]` is only a fallback seed when no stored value exists. The `DisturbanceEstimateNumber` entity reads from `coordinator.data.disturbance_estimate` (auto-updates) and on write calls `coordinator.set_disturbance_estimate()`, which persists to the store.

### Prediction supply

`_run_mpc` passes `prediction_supply` to `compute_supply_setpoint`:

- **Pump off** (`inner.data.pump_state == "off"`, potentiometer > `pump_off_threshold`): `t_in`. The radiators sit at room temperature, so no heat is added.
- **Pump on / boosting**: `None`, so the prediction simulates the supply setpoint. While the setpoint is reachable, the prediction lands on target by construction.

A variant that simulated the measured supply line while the pump ran was tried (2026-10-16/17) and reverted as messy to read, so don't reintroduce it. The hour profile's learning still uses the measured supply, because it scores what actually happened.

### Hour-of-day profile (live)

`hour_profile.py` (pure Python): `HourProfile` learns one correction per local clock hour, q[h] in °C/h. It captures heat that follows the daily schedule and that the single-mass model misses, like occupied bedrooms at night and afternoon activity. If the indoor sensor is a time-of-day blend of rooms, it also captures the blend's ramps, which would go away if a room model steered.

- **Learning:** `OuterCoordinator._learn_profile` runs on every outer tick, after the shadow block, and feeds one sample per tick:
  - indoor;
  - effective supply (measured, or indoor when the pump is off);
  - outdoor;
  - current-hour solar.

  When the clock hour changes, the hour is scored. The model starts from the indoor reading at the start of the hour and is stepped with the hour's mean inputs; residual r = (actual − model) / elapsed h, clipped to ±0.4. Then `q[h] += 0.2·(r − q[h])`, so β 0.2 ≈ 5 days of memory.
- **When an hour doesn't count:** under 8 samples, elapsed outside 50–75 min (restarts, sensor gaps), or learning paused at any point in the hour. Learning pauses while `room_scoring_paused` or `window_suspected` is on.
- **Applying:** `_run_mpc` passes `extra = profile.corrections(now, n_steps, dt, reduce_only)` to `compute_supply_setpoint`, which adds it to each step's disturbance d[k]. Each step uses the slot of its midpoint's clock hour, so the whole horizon sees its own hours. The solve and the prediction both include it. The path MPC and the rooms don't use it yet.
- **No-pre-heat window:** from `profile_no_preheat_start` to `profile_no_preheat_end` (number entities, local hours, end exclusive, default 22 → 7, wrapping midnight) a negative slot is ignored, so the profile can only take heat away there. That rules out night/morning pre-heat of sleeping rooms, and the edge slots mostly learn the steering-sensor blend anyway. Added 2026-10-17 after negative early-morning slots pushed supply to ~40 and overheated the bedroom. Set it to cover the hours people sleep. Start = end disables it. The window is computed by `reduce_only_hours()` in `hour_profile.py` and read each tick via `OuterCoordinator._profile_reduce_only()`.
- **Kill switch:** `switch.…_hour_of_day_correction` (`HourProfileSwitch`, RestoreEntity). Off means the MPC ignores the profile; learning continues. Takes effect on the next outer tick.
- **Sensor:** `sensor.…_hour_of_day_correction` shows the current hour's q. Attributes: `profile` (24 values), `enabled`, `updates`, `last_hour`, `last_residual`.
- **Store:** `.storage/heat_exchanger_controller.<entry_id>.profile` (`q`, `updates`). Saved debounced after each hourly update, flushed on unload.
- **Backtest** (online, 14 d): house 3 h-ahead RMS 0.285 → 0.230, night bias +0.10 → +0.01. A single adaptive constant (observer-like) made it worse.
- **Scale:** 0.1 °C/h ≈ 50 W ≈ 9 °C of supply (at UA ≈ 6, C 500). Expect setpoint shifts of several °C at the hours where the profile is large.
- **Interaction:** the profile is learned against the current RC params, and it re-adapts within days if they change. Running it together with a fast observer makes two learners chase the same error; the observer's leak keeps it to short-term deviations.

### PID integral management

- **Dry-run toggle**: `InnerCoordinator` tracks `_prev_dry_run`. When dry-run transitions `True → False`, `self._pid.reset()` is called immediately. This prevents the large negative integral accumulated during dry-run testing from causing an overshoot spike when going live.
- **Awaiting first setpoint**: `OuterData.setpoint_valid` is `False` until `_run_mpc` has succeeded once (right after a restart the indoor/outdoor sensors are often not loaded yet, so the MPC is skipped and `supply_setpoint` is only the `supply_min` placeholder). A missing indoor reading skips the MPC too (`mpc_skipped_no_indoor`) until the first valid setpoint; before 2026-10-13 it fell back to indoor = target, which gave a wrong first setpoint after restarts (≈ 33 instead of ≈ 43 °C) for one outer interval. Later dropouts still use the target fallback. While it is `False` the inner loop runs in `awaiting_setpoint` mode: no PID step, no write, the potentiometer stays where it is, and it calls `outer.async_request_refresh()` each tick so the hold lasts ~30 s instead of a full outer interval. The pending preload runs on the first real PID cycle. Before this, every restart spent the first outer interval driving the potentiometer toward minimum heat.
- **Startup guard**: `_has_valid_output: bool` starts `False`. The potentiometer is not written until at least one full PID cycle has completed with a valid supply sensor reading. `_last_potentiometer` initialises to `OUTPUT_MAX` (safest value — minimum heat) rather than `OUTPUT_MIN`.
- **Bumpless start**: on the first PID cycle after setup, and on the first cycle after dry run is turned off, `_preload_pid()` calls `SimplePID.preload()` so the output continues from the current potentiometer value instead of jumping to the P-term only. Source priority: the optional readback sensor (`output_readback_entity` in `entry.data`) → the last value written (persisted via HA `Store` at `.storage/heat_exchanger_controller.<entry_id>.inner`, debounced 60 s, flushed on unload; startup only, since it's stale after dry run). With neither, it's a plain reset. If the seed value is above `pump_off_threshold`, `_pump_state` is set to `off` so pump protection doesn't force the pump back on.

### Open-Meteo solar irradiance

Latitude/longitude come from the config entry (pre-filled from HA's location). The URL uses `timezone=UTC` so response timestamps match `datetime.now(timezone.utc)`. The raw `(times, radiation)` arrays are cached for 15 minutes; the slice is recomputed fresh on every outer tick. This avoids both the UTC/local timezone mismatch (which caused 0 W/m² readings mid-day) and stale slices within the cache window.

`shortwave_radiation` is the **mean over the preceding hour** (the `10:00` slot covers 09:00–10:00). The slice therefore starts at the slot labelled `now + 1 h` (floored to the hour), so model step k gets the mean irradiance for hour k of the horizon and the `solar_irradiance` sensor shows the expected mean for the current hour. Before 2026-10-11 the slice started at the current-hour slot, which fed the model solar one hour late.

### Options storage split

Options are divided into two categories, both stored in `entry.options`:

| Category | Keys | On change |
|---|---|---|
| Structural | RC params, PID params, limits, intervals — see `RELOAD_ON_CHANGE_KEYS` in `const.py` | Full entry reload |
| State | `target_temperature`, `hvac_mode`, `dry_run`, `disturbance_estimate`, `disturbance_alpha`, and the other tuning numbers | Coordinator refresh only (no reload) |

The options listener in `__init__.py` compares a snapshot of structural keys taken at setup time against the current values to decide which path to take.

### Fault handling

Every data source in `OuterCoordinator._async_update_data` is wrapped in try/except. Faults are accumulated in `OuterData.faults: list[str]` and surface in the `status` sensor (`ok` / `degraded` / `fault`). The system never raises `UpdateFailed` — it always returns data, possibly with degraded values (assumed setpoint for indoor temp, zeros for solar, last-known for outdoor temp, etc.).

### Dry run

`DryRunSwitch` (`switch.py`) defaults to `on` after initial setup. While enabled, all calculations run normally and all sensors update, but the potentiometer write in `InnerCoordinator._write_output` is skipped. Turn it off to go live. The PID integral is reset to zero when dry run is turned off.

### Reconfiguring sensors and services

The sensors, weather entity, and output service are stored in `entry.data` (set at initial setup). To change them: go to **Settings → Devices & Services → Heat Exchanger Controller → Reconfigure**. This re-shows the setup form pre-filled with current values and reloads the integration on save.

### Rooms (shadow mode)

Design: `docs/plans/2026-10-12-multi-room-shadow-design.md`. Rooms are config subentries (type `room`, "Add room" on the integration), each with a name, its own temperature sensor and its own R / UA / g / C in `subentry.data`. **Shadow only: the live path (`_run_mpc` → `InnerCoordinator`) reads no room data.** The controller keeps steering on `indoor_temp_entity`.

- **`room_model.py`** (pure Python, like `rc_mpc.py`): `RoomModel.step()` per outer tick does the importance ramp (linear by elapsed time, `importance_ramp_time` = full 0→1), the room MPC against the room target on the house forecasts (`prediction_supply = t_room` when the pump is off, otherwise the live supply setpoint), a 3 h-ahead prediction (fixed `SCORE_HORIZON_H`, supply held at the live setpoint) that is scored against the reading when due (within 10 min, else dropped; skipped while `scoring_paused`), a rolling 24 h RMS/bias, and the anomaly check (30 min slope more than 0.3 °C/h below the zero-heat model slope for ≥ 15 min). `combine()` gives the importance-weighted mean and the max-rule value; `None` when all weights are 0.
- `rc_mpc.model_inputs()` turns a horizon + forecasts into the model's step inputs; `_run_mpc` and the rooms share it.
- **`OuterCoordinator._run_rooms`** runs after the live MPC, inside try/except. Faults go to `OuterData.shadow_faults` (shown as `shadow_faults` on the status sensor), never to `faults`, so the status level is unaffected. An unavailable room sensor makes that room's entities unavailable and gives it weight 0. With rooms configured, the forecasts fetch at least 3 hourly values (the live MPC still slices to its own horizon).
- **Reloads:** the structural snapshot in `__init__.py` includes each room's title + data, so adding/removing/editing a room (or writing a room RC number, which updates the subentry) reloads. Room `target`, `importance` and `importance_ramp_time` are `RestoreNumber` entities pushed into `outer.set_room_input()`. They never reload or refresh: the next outer tick picks them up, because a refresh would also step the live observer.
- **Store:** `.storage/heat_exchanger_controller.<entry_id>.rooms` holds each room's ramped importance, pending predictions and errors. It's saved debounced every tick and flushed on unload.
- **Entities:** per room device (`via_device` the house): numbers `target`, `importance`, `importance_ramp_time`, `rc_r/ua/g/c`; sensors `importance_ramped`, `predicted_temp` (control horizon, live supply), `supply_setpoint` (the room's own MPC), `prediction_error` (24 h RMS of 3 h-ahead; attrs `bias`, `samples`, `anomaly`). House (only created when rooms exist): `shadow_supply_setpoint` (attrs `max_rule`, `weights`, `live_supply_setpoint`), switch `room_scoring_paused`, binary_sensor `window_suspected` (diagnostic; doesn't pause scoring).
- **Tools:** `tools/fit_rc_multistep.py [days] [H] [indoor_entity]` fits one room. `tools/replay_rooms.py [days]` runs synthetic checks and replays history through `RoomModel`; with per-hour supply it must equal the fit's RMS. In-component scores run higher than the fit's, because the live prediction holds supply constant for 3 h.

### Offline tools

`tools/` holds analysis scripts (numpy/scipy, run from `tools/.venv`). They read the recorder DB, location and sensor entities from the local HA config via `tools/ha_local.py` (config dir = `$HA_CONFIG`, else three levels up; overrides `$HA_DB`, `$HA_LAT`, `$HA_LON`). Periods to drop from fits (open windows etc.) go in `tools/exclude.local.json` (gitignored). Never hardcode install values in the tools.

### Path MPC (shadow mode)

Design and replay results: `docs/plans/2026-10-13-path-mpc-design.md`. **Shadow only**, like the rooms.

- **`path_mpc.py`** (pure Python): `plan_supply()` picks an hourly supply plan over `path_horizon` (default 6 h). It minimises the comfort error along the whole path: a dead band ±`path_comfort_band` (default 0.2), a weak pull towards target inside the band (`CENTER_WEIGHT` 0.1; without it the plan drifts to the band edge), and a small supply-change penalty (`SMOOTH_WEIGHT` 1e-4).
  - Lower bound = max(supply_min, warmest weighted room) = "no heat". This replaces the pump-state rule, because the single-mass model would otherwise let a low supply cool the room.
  - The problem is convex (every room temperature is linear in the plan), and projected gradient with backtracking solves it exactly.
  - There's no energy term: at a flat kWh price it bought ≤ 1.4 % heat for clearly worse comfort. The target is the energy knob.
- **`OuterCoordinator._run_paths`** runs after `_run_rooms` and runs two plans:
  - rooms, weighted by ramped importance; weight-0 rooms still get a predicted path;
  - house, on the house RC options with `indoor_temp_entity` and target + d_hat.

  Warm start: the previous plan, shifted one hour once ≥ 30 min have passed (not persisted). Faults go to `shadow_faults`. The forecasts always fetch at least `path_horizon` hours.
- **Entities:**
  - house: numbers `path_comfort_band` and `path_horizon` (`RestoreNumber`s pushed via `set_path_input`, never reload);
  - house sensor `planned_indoor_temp` (house plan; attrs `path`, `plan`);
  - house sensor `path_supply_setpoint` (rooms plan[0]; attrs `plan`, `cost`, `converged`, `house_plan`, `house_first_hour`), created only when rooms exist;
  - per room: `planned_temp` (attr `path`).

  Plan entry k applies from `path_start` + k h; path entry k is the temperature at `path_start` + (k+1) h.
- **Replay** (`tools/replay_path.py [days] [--sweep]`): unit checks, then a 14-day closed loop against a coupled two-room plant with perfect forecasts. The 3 h shadow combine was outside ±0.2 °C 14.1 % of the time, with worst undershoot −0.68; path MPC was 10.5 % and −0.37, at the same heat.

### Entities

**Sensors (read-only):**
- `status` — ok / degraded / fault, with fault list and availability flags in attributes
- `controller_action` — human-readable description of what the outer loop is doing
- `supply_temp_setpoint` — RC-MPC output (°C)
- `supply_temp_actual` — supply line temperature read by inner loop (°C)
- `predicted_indoor_temp` — disturbance-corrected prediction of indoor temp at horizon (°C)
- `outdoor_temp_used` — outdoor temp after fallback resolution (°C)
- `outdoor_temp_source` — which entity/source was used
- `indoor_temp_used` — indoor temp used by MPC (°C)
- `solar_irradiance` — current-hour irradiance from Open-Meteo (W/m²)
- `smhi_forecast` — next-hour outdoor forecast (°C); full list in `forecast` attribute
- `potentiometer_output` — value written to the potentiometer (fake outdoor °C)
- `pump_state` — on / off (inferred from potentiometer vs pump_off_threshold)
- `pid_error`, `pid_p_term`, `pid_i_term`, `pid_d_term` — inner PID diagnostics (°C)
- `indoor_rate_actual`, `indoor_rate_model`, `model_mismatch_rate` — °C/h over the last hour (`_RATE_WINDOW`; unknown for the first ~40 min after a restart). Actual is the slope of `indoor_temp_entity`. Model is the RC rate evaluated each tick from current indoor, effective supply (indoor when the pump is off, else measured), outdoor and current-hour solar, averaged over the window, plus the profile slot as the MPC applies it (attrs `physics`, `profile`). Mismatch = actual − model, i.e. unmodelled heat (+ = warming faster than the model expects). It excludes d_hat, which is a target shift, not a rate.

**Number entities (tunable parameters):**
- RC model: `rc_r`, `rc_c`, `rc_ua`, `rc_g`
- PID: `pid_kp`, `pid_ki`, `pid_kd`
- Limits: `supply_min`, `supply_max`, `output_min` (default 0 °C), `output_max`
- Prediction: `prediction_horizon` (0.5–12 h, step 0.5)
- Intervals: `outer_interval`, `inner_interval`
- Climate bounds: `climate_min_temp`, `climate_max_temp`
- Pump cycling: `pump_off_threshold`, `min_pump_on_duration`
- Disturbance observer: `disturbance_alpha` (0.8–1.0; integrator gain is 1 − α per outer tick, so lower = faster adaptation, 1.0 = frozen), `disturbance_estimate` (live read/write, clamped to ±`disturbance_max`; auto-updated by observer, writable to seed or override), `disturbance_max` (cap on d_hat, default 0.3), `observer_no_heat_margin` (supply − indoor below which d_hat stops going negative, default 5 °C)
- Hour profile: `profile_no_preheat_start`, `profile_no_preheat_end` (no-pre-heat window, local hours, default 22 → 7)

**Climate entity:** `climate.heat_exchanger` — sets target temperature and hvac_mode (heat / off).

**Switch:** `dry_run` — disables potentiometer writes while keeping all sensors live.
