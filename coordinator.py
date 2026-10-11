"""Coordinators for Heat Exchanger Controller."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DISTURBANCE_ALPHA,
    CONF_DISTURBANCE_ESTIMATE,
    CONF_HVAC_MODE,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INNER_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_BOOST_TRIGGER_SPAN,
    CONF_MIN_PUMP_ON_DURATION,
    CONF_OUTDOOR_TEMP_ENTITIES,
    CONF_OUTPUT_MIN,
    CONF_OUTPUT_READBACK_ENTITY,
    CONF_OUTPUT_MAX,
    CONF_OUTPUT_SERVICE,
    CONF_OUTPUT_VALUE_KEY,
    CONF_PID_KD,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PREDICTION_HORIZON,
    CONF_PUMP_OFF_THRESHOLD,
    CONF_RC_C,
    CONF_RC_G,
    CONF_RC_R,
    CONF_RC_UA,
    CONF_SUPPLY_MAX,
    CONF_SUPPLY_MIN,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TARGET_TEMPERATURE,
    CONF_WEATHER_ENTITY,
    DEFAULT_BOOST_TRIGGER_SPAN,
    DEFAULT_DISTURBANCE_ALPHA,
    DEFAULT_DISTURBANCE_ESTIMATE,
    DEFAULT_HVAC_MODE,
    DEFAULT_MIN_PUMP_ON_DURATION,
    DEFAULT_OUTPUT_MAX,
    DEFAULT_OUTPUT_MIN,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PREDICTION_HORIZON,
    DEFAULT_PUMP_OFF_THRESHOLD,
    DEFAULT_RC_C,
    DEFAULT_RC_G,
    DEFAULT_RC_R,
    DEFAULT_RC_UA,
    DEFAULT_SUPPLY_MAX,
    DEFAULT_SUPPLY_MIN,
    DEFAULT_TARGET_TEMPERATURE,
    DOMAIN,
    OPEN_METEO_URL,
    OUTDOOR_SOURCE_LAST_KNOWN,
    OUTDOOR_SOURCE_UNAVAILABLE,
    STATUS_DEGRADED,
    STATUS_FAULT,
    STATUS_OK,
    STATUS_STARTING,
)
from .pid import SimplePID
from .rc_mpc import compute_supply_setpoint

_LOGGER = logging.getLogger(__name__)

# Cache Open-Meteo data for 15 minutes
_OPEN_METEO_CACHE_SECONDS = 900

# Maximum disturbance correction (°C) — hard cap regardless of alpha setting
_DISTURBANCE_MAX = 5.0
# Observer state is written at most this often; HA flushes pending writes on shutdown
_STATE_SAVE_DELAY = 60


@dataclass
class OuterData:
    supply_setpoint: float
    predicted_indoor_temp: float | None
    outdoor_temp_source: str
    solar_irradiance: float | None
    indoor_temp: float | None
    current_outdoor_temp: float | None
    faults: list[str]
    status: str
    smhi_available: bool
    open_meteo_available: bool
    controller_action: str = "unknown"
    smhi_forecast: list[float] = field(default_factory=list)
    disturbance_estimate: float = 0.0
    # False until the MPC has produced at least one setpoint (e.g. sensors not yet
    # loaded right after a restart); supply_setpoint is then only a placeholder
    setpoint_valid: bool = True


@dataclass
class InnerData:
    potentiometer_value: float
    supply_fault: bool
    inner_mode: str  # "pid", "open_loop" or "awaiting_setpoint"
    current_supply_temp: float | None
    dry_run: bool
    pump_state: str  # "on" or "off"
    pump_protection_active: bool
    pid_error: float | None = None
    pid_p_term: float | None = None
    pid_i_term: float | None = None
    pid_d_term: float | None = None


class OuterCoordinator(DataUpdateCoordinator[OuterData]):
    """Outer loop: RC-MPC every N minutes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_outer",
            update_interval=update_interval,
        )
        self._entry = entry
        self._last_supply_setpoint: float = entry.options.get(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN)
        self._has_valid_setpoint: bool = False
        self._last_outdoor_temp: float | None = None
        self._open_meteo_cache: tuple[list[str], list[float]] | None = None
        self._open_meteo_cache_time: datetime | None = None
        self._d_hat: float = float(entry.options.get(CONF_DISTURBANCE_ESTIMATE, DEFAULT_DISTURBANCE_ESTIMATE))
        self._store: Store[dict[str, float]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.observer")
        self._inner: "InnerCoordinator | None" = None  # set by __init__.py after both are created

    async def async_load_state(self) -> None:
        """Restore d_hat persisted by the previous run (falls back to the options value)."""
        stored = await self._store.async_load()
        if stored and "d_hat" in stored:
            self._d_hat = float(stored["d_hat"])

    def set_disturbance_estimate(self, value: float) -> None:
        self._d_hat = max(-_DISTURBANCE_MAX, min(_DISTURBANCE_MAX, value))
        self._save_state()

    def _save_state(self) -> None:
        self._store.async_delay_save(lambda: {"d_hat": self._d_hat}, _STATE_SAVE_DELAY)

    async def async_flush_state(self) -> None:
        """Write d_hat immediately so a reload picks up the latest value."""
        await self._store.async_save({"d_hat": self._d_hat})

    def _opt(self, key: str, default: Any) -> Any:
        return self._entry.options.get(key, default)

    def _get_sensor_value(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    async def _async_update_data(self) -> OuterData:
        faults: list[str] = []

        # --- Indoor temperature ---
        indoor_entity = self._entry.data.get(CONF_INDOOR_TEMP_ENTITY)
        indoor_temp = self._get_sensor_value(indoor_entity) if indoor_entity else None
        if indoor_temp is None:
            faults.append("indoor_temp_unavailable")

        # Use setpoint as fallback for indoor temp
        target_temp = self._entry.options.get(CONF_TARGET_TEMPERATURE, DEFAULT_TARGET_TEMPERATURE)
        t_in = indoor_temp if indoor_temp is not None else target_temp

        # --- Outdoor temperature (fallback chain) ---
        outdoor_temp, outdoor_source = self._resolve_outdoor_temp(faults)

        # --- SMHI forecast ---
        smhi_outdoor_forecast, smhi_available = await self._fetch_smhi_forecast(faults, outdoor_temp)

        # --- Open-Meteo solar irradiance ---
        solar_forecast, open_meteo_available = await self._fetch_open_meteo(faults)

        # --- Current solar value (first item of forecast) ---
        solar_now = solar_forecast[0] if solar_forecast else None

        # --- Disturbance observer ---
        # Accumulate a slow-moving estimate of the gap between model predictions and
        # reality. Frozen when heating is off or indoor temp is unavailable.
        hvac_mode = self._entry.options.get(CONF_HVAC_MODE, DEFAULT_HVAC_MODE)
        if hvac_mode == "heat" and indoor_temp is not None:
            indoor_error = indoor_temp - target_temp  # positive = warm, negative = cold
            # Anti-windup: when supply setpoint is saturated and the error would push d_hat
            # further into saturation, freeze the update. Allows recovery once the error reverses.
            supply_min = float(self._opt(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN))
            supply_max = float(self._opt(CONF_SUPPLY_MAX, DEFAULT_SUPPLY_MAX))
            at_min = self._last_supply_setpoint <= supply_min + 0.5
            at_max = self._last_supply_setpoint >= supply_max - 0.5
            saturated = (at_min and indoor_error > 0) or (at_max and indoor_error < 0)
            if not saturated:
                alpha = float(self._opt(CONF_DISTURBANCE_ALPHA, DEFAULT_DISTURBANCE_ALPHA))
                # Integrate (not low-pass) so any persistent offset keeps growing d_hat until
                # indoor reaches target; (1 - alpha) is the gain per outer tick.
                self._d_hat += (1.0 - alpha) * (-indoor_error)
                self._d_hat = max(-_DISTURBANCE_MAX, min(_DISTURBANCE_MAX, self._d_hat))
                self._save_state()

        # Solver targets (target + d_hat) so the house actually lands on target
        effective_target = target_temp + self._d_hat

        # --- RC-MPC ---
        supply_setpoint, predicted_indoor = self._run_mpc(
            t_in, effective_target, outdoor_temp, smhi_outdoor_forecast, solar_forecast, faults
        )

        # Shift prediction back to "expected actual" space by removing the model bias
        predicted_corrected = (
            (predicted_indoor - self._d_hat) if predicted_indoor is not None else None
        )

        # --- Overall status ---
        if self.hass.state != CoreState.running:
            status = STATUS_STARTING
        elif outdoor_source == OUTDOOR_SOURCE_UNAVAILABLE:
            status = STATUS_FAULT
        elif faults:
            status = STATUS_DEGRADED
        else:
            status = STATUS_OK

        controller_action = self._compute_controller_action(
            hvac_mode, status, supply_setpoint, predicted_corrected, target_temp, indoor_temp, faults
        )

        return OuterData(
            supply_setpoint=supply_setpoint,
            predicted_indoor_temp=predicted_corrected,
            outdoor_temp_source=outdoor_source,
            solar_irradiance=solar_now,
            indoor_temp=indoor_temp,
            current_outdoor_temp=outdoor_temp,
            faults=faults,
            status=status,
            smhi_available=smhi_available,
            open_meteo_available=open_meteo_available,
            controller_action=controller_action,
            smhi_forecast=smhi_outdoor_forecast,
            disturbance_estimate=self._d_hat,
            setpoint_valid=self._has_valid_setpoint,
        )

    def _compute_controller_action(
        self,
        hvac_mode: str,
        status: str,
        supply_setpoint: float,
        predicted_indoor: float | None,
        target_temp: float,
        indoor_temp: float | None,
        faults: list[str],
    ) -> str:
        if hvac_mode == "off":
            return "Heating off"
        if status == STATUS_STARTING:
            return "Starting"
        if "mpc_skipped_no_outdoor" in faults:
            return "Fault: no outdoor temperature"
        if (
            self._inner is not None
            and self._inner.data is not None
            and self._inner.data.pump_state == "boosting"
        ):
            horizon = int(self._opt(CONF_PREDICTION_HORIZON, DEFAULT_PREDICTION_HORIZON))
            pred_str = f"{predicted_indoor:.1f}°C" if predicted_indoor is not None else "unknown"
            return f"Preventive boost — predicted {pred_str} in {horizon}h"

        supply_min = float(self._opt(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN))
        supply_max = float(self._opt(CONF_SUPPLY_MAX, DEFAULT_SUPPLY_MAX))
        horizon = int(self._opt(CONF_PREDICTION_HORIZON, DEFAULT_PREDICTION_HORIZON))
        at_min = supply_setpoint <= supply_min + 0.5
        at_max = supply_setpoint >= supply_max - 0.5

        if predicted_indoor is None:
            if at_max:
                return "Maximum heating"
            if at_min:
                return "Minimum heating"
            return "Active"

        pred_err = predicted_indoor - target_temp
        horizon_str = f"in {horizon}h"

        if at_max:
            if pred_err < -0.5:
                return f"Maximum heating — predicted {predicted_indoor:.1f}°C {horizon_str}"
            return "Maximum heating applied"

        if at_min:
            if pred_err > 0.5:
                return f"House predicted to overheat — heating turned off"
            return f"Maintaining {target_temp:.1f}°C — heating turned off"

        if pred_err < -0.5:
            return f"Heating to {target_temp:.1f}°C — predicted {predicted_indoor:.1f}°C {horizon_str}"
        if pred_err > 0.5:
            return f"Reducing heat — predicted {predicted_indoor:.1f}°C {horizon_str}"
        return f"Maintaining {target_temp:.1f}°C"

    def _resolve_outdoor_temp(self, faults: list[str]) -> tuple[float | None, str]:
        """Try each configured outdoor sensor in order, then SMHI, then last known."""
        outdoor_entities: list[str] = self._entry.data.get(CONF_OUTDOOR_TEMP_ENTITIES, [])

        for entity_id in outdoor_entities:
            val = self._get_sensor_value(entity_id)
            if val is not None:
                self._last_outdoor_temp = val
                return val, entity_id

        # Try SMHI current observation
        weather_entity = self._entry.data.get(CONF_WEATHER_ENTITY)
        if weather_entity:
            state = self.hass.states.get(weather_entity)
            if state and state.state not in ("unknown", "unavailable"):
                smhi_temp = state.attributes.get("temperature")
                if smhi_temp is not None:
                    try:
                        val = float(smhi_temp)
                        self._last_outdoor_temp = val
                        faults.append("outdoor_temp_using_smhi_current")
                        return val, f"{weather_entity}_current"
                    except (ValueError, TypeError):
                        pass

        # Use last known value
        if self._last_outdoor_temp is not None:
            faults.append("outdoor_temp_using_last_known")
            return self._last_outdoor_temp, OUTDOOR_SOURCE_LAST_KNOWN

        faults.append("outdoor_temp_unavailable")
        return None, OUTDOOR_SOURCE_UNAVAILABLE

    async def _fetch_smhi_forecast(
        self, faults: list[str], current_outdoor: float | None
    ) -> tuple[list[float], bool]:
        """Fetch hourly outdoor temp forecast from SMHI weather entity.

        Tries weather.get_forecasts service first (HA 2023.9+), falls back to
        reading the forecast attribute directly from the entity state.
        """
        weather_entity = self._entry.data.get(CONF_WEATHER_ENTITY)
        horizon = max(1, math.ceil(float(self._opt(CONF_PREDICTION_HORIZON, DEFAULT_PREDICTION_HORIZON))))

        if not weather_entity:
            faults.append("smhi_not_configured")
            return _flat_forecast(current_outdoor, horizon), False

        forecasts: list[dict] = []

        # Try the service-based API first
        try:
            result = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            forecasts = result.get(weather_entity, {}).get("forecast", [])
        except ServiceNotFound:
            # Older HA: read forecast attribute directly from entity state
            state = self.hass.states.get(weather_entity)
            if state:
                forecasts = state.attributes.get("forecast", [])
        except Exception as err:
            _LOGGER.warning("SMHI forecast service error (%s): %s", type(err).__name__, err)

        if not forecasts:
            _LOGGER.warning("SMHI forecast returned no data for %s", weather_entity)
            faults.append("smhi_forecast_unavailable")
            return _flat_forecast(current_outdoor, horizon), False

        try:
            temps = [float(f["temperature"]) for f in forecasts[:horizon]]
            while len(temps) < horizon:
                temps.append(temps[-1])
            return temps, True
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("SMHI forecast parse error: %s", err)
            faults.append("smhi_forecast_unavailable")
            return _flat_forecast(current_outdoor, horizon), False

    async def _fetch_open_meteo(
        self, faults: list[str]
    ) -> tuple[list[float], bool]:
        """Fetch hourly solar irradiance from Open-Meteo, with caching."""
        horizon = max(1, math.ceil(float(self._opt(CONF_PREDICTION_HORIZON, DEFAULT_PREDICTION_HORIZON))))
        now = datetime.now(timezone.utc)

        # Fetch fresh data if cache is stale; otherwise re-use raw arrays
        if (
            self._open_meteo_cache is None
            or self._open_meteo_cache_time is None
            or (now - self._open_meteo_cache_time).total_seconds() >= _OPEN_METEO_CACHE_SECONDS
        ):
            lat = self._entry.data.get(CONF_LATITUDE, self.hass.config.latitude)
            lon = self._entry.data.get(CONF_LONGITUDE, self.hass.config.longitude)
            url = OPEN_METEO_URL.format(lat=lat, lon=lon)

            try:
                session = async_get_clientsession(self.hass)
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                times: list[str] = data["hourly"]["time"]
                radiation: list[float] = data["hourly"]["shortwave_radiation"]
                self._open_meteo_cache = (times, radiation)
                self._open_meteo_cache_time = now

            except Exception as err:
                _LOGGER.debug("Open-Meteo unavailable: %s", err)
                faults.append("open_meteo_unavailable")
                return [0.0] * horizon, False

        times, radiation = self._open_meteo_cache

        # Open-Meteo's shortwave_radiation is the mean over the *preceding* hour, so the
        # slot labelled HH+1:00 covers the current hour HH–HH+1. Start the slice there so
        # model step k gets the mean for hour k. Times are UTC because the URL uses timezone=UTC.
        current_slot = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
        try:
            idx = times.index(current_slot)
        except ValueError:
            idx = 0
            for i, t in enumerate(times):
                if t >= current_slot:
                    idx = i
                    break

        solar_slice = radiation[idx: idx + horizon]
        while len(solar_slice) < horizon:
            solar_slice.append(0.0)

        return solar_slice, True

    def _run_mpc(
        self,
        t_in: float,
        target_temp: float,
        outdoor_temp: float | None,
        smhi_forecast: list[float],
        solar_forecast: list[float],
        faults: list[str],
    ) -> tuple[float, float | None]:
        """Run the RC-MPC solver. Returns (supply_setpoint, predicted_indoor)."""
        supply_min = float(self._opt(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN))
        supply_max = float(self._opt(CONF_SUPPLY_MAX, DEFAULT_SUPPLY_MAX))

        if outdoor_temp is None:
            faults.append("mpc_skipped_no_outdoor")
            return self._last_supply_setpoint, None

        R = float(self._opt(CONF_RC_R, DEFAULT_RC_R))
        C = float(self._opt(CONF_RC_C, DEFAULT_RC_C))
        UA = float(self._opt(CONF_RC_UA, DEFAULT_RC_UA))
        g = float(self._opt(CONF_RC_G, DEFAULT_RC_G))

        # Determine model step size and number of steps
        # Sub-hourly horizons use a single step of dt = horizon; ≥1 h uses integer hourly steps
        horizon_h = float(self._opt(CONF_PREDICTION_HORIZON, DEFAULT_PREDICTION_HORIZON))
        if horizon_h < 1.0:
            dt_hours = horizon_h
            model_outdoor = smhi_forecast[:1] or [outdoor_temp]
            model_solar = solar_forecast[:1] or [0.0]
        else:
            dt_hours = 1.0
            n_steps = max(1, round(horizon_h))
            fallback_t = smhi_forecast[-1] if smhi_forecast else outdoor_temp
            fallback_s = solar_forecast[-1] if solar_forecast else 0.0
            model_outdoor = (smhi_forecast + [fallback_t] * n_steps)[:n_steps]
            model_solar = (solar_forecast + [fallback_s] * n_steps)[:n_steps]

        # When pump is off, no heat is delivered — simulate with T_indoor as effective supply
        pump_off = (
            self._inner is not None
            and self._inner.data is not None
            and self._inner.data.pump_state == "off"
        )

        try:
            setpoint, predicted = compute_supply_setpoint(
                t_in=t_in,
                t_setpoint=target_temp,
                outdoor_temps=model_outdoor,
                solar_irradiance=model_solar,
                R=R,
                C=C,
                UA=UA,
                g=g,
                supply_min=supply_min,
                supply_max=supply_max,
                prediction_supply=t_in if pump_off else None,
                dt_hours=dt_hours,
            )
            self._last_supply_setpoint = setpoint
            self._has_valid_setpoint = True
            return setpoint, predicted
        except Exception as err:
            _LOGGER.warning("RC-MPC failed: %s", err)
            faults.append(f"mpc_error: {err}")
            return self._last_supply_setpoint, None


class InnerCoordinator(DataUpdateCoordinator[InnerData]):
    """Inner loop: PID supply temp control every N seconds."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        outer: OuterCoordinator,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_inner",
            update_interval=update_interval,
        )
        self._entry = entry
        self._outer = outer
        self._last_potentiometer: float = float(entry.options.get(CONF_OUTPUT_MAX, DEFAULT_OUTPUT_MAX))
        self._has_valid_output: bool = False
        self._last_update: datetime | None = None
        self._pid = self._build_pid()
        self._prev_dry_run: bool = self._dry_run()
        self._pump_state: str = "on"
        self._pump_on_since: datetime = datetime.now(timezone.utc)
        self._boost_active: bool = False
        self._store: Store[dict[str, float]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.inner")
        # Last written potentiometer value from the previous run (fallback when no
        # readback sensor is available)
        self._restored_potentiometer: float | None = None
        self._last_written: float | None = None
        # Preload the PID integrator on the next valid cycle (bumpless start).
        # Set at startup and when dry run is turned off.
        self._preload_pending: bool = True

    async def async_load_state(self) -> None:
        """Restore the last written potentiometer value so a restart doesn't jump."""
        stored = await self._store.async_load()
        if not stored or "potentiometer" not in stored:
            return
        value = float(stored["potentiometer"])
        self._restored_potentiometer = value
        self._last_potentiometer = value
        self._last_written = value

    def _read_potentiometer(self) -> float | None:
        """Actual potentiometer value from the readback sensor, if configured."""
        entity = self._entry.data.get(CONF_OUTPUT_READBACK_ENTITY)
        return self._get_sensor_value(entity) if entity else None

    def _preload_pid(self, supply_temp: float, allow_stored: bool) -> None:
        """Seed the PID so its output continues from the current potentiometer value."""
        value = self._read_potentiometer()
        source = "readback"
        if value is None and allow_stored:
            value, source = self._restored_potentiometer, "stored"
        self._restored_potentiometer = None
        if value is None:
            return
        # Negated to match the PID's sign convention (see _async_update_data)
        self._pid.preload(-value, supply_temp)
        threshold = float(self._opt(CONF_PUMP_OFF_THRESHOLD, DEFAULT_PUMP_OFF_THRESHOLD))
        if value > threshold:
            # Pump is already off — don't let pump protection force it back on
            self._pump_state = "off"
        _LOGGER.info("PID preloaded from %s potentiometer value %.2f", source, value)

    def _save_state(self) -> None:
        self._store.async_delay_save(lambda: {"potentiometer": self._last_written}, _STATE_SAVE_DELAY)

    async def async_flush_state(self) -> None:
        if self._last_written is not None:
            await self._store.async_save({"potentiometer": self._last_written})

    def _opt(self, key: str, default: Any) -> Any:
        return self._entry.options.get(key, default)

    def _build_pid(self) -> SimplePID:
        supply_setpoint = (
            self._outer.data.supply_setpoint
            if self._outer.data is not None
            else float(self._opt(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN))
        )
        # Limits are negated because the PID output is negated before writing
        # (the heat exchanger has an inverse transfer function: a warmer fake
        # outdoor temp produces a lower supply temperature).
        out_min = float(self._opt(CONF_OUTPUT_MIN, DEFAULT_OUTPUT_MIN))
        out_max = float(self._opt(CONF_OUTPUT_MAX, DEFAULT_OUTPUT_MAX))
        return SimplePID(
            kp=float(self._opt(CONF_PID_KP, DEFAULT_PID_KP)),
            ki=float(self._opt(CONF_PID_KI, DEFAULT_PID_KI)),
            kd=float(self._opt(CONF_PID_KD, DEFAULT_PID_KD)),
            setpoint=supply_setpoint,
            output_limits=(-out_max, -out_min),
        )

    def _get_sensor_value(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _hvac_is_off(self) -> bool:
        return self._entry.options.get("hvac_mode", "heat") == "off"

    def _dry_run(self) -> bool:
        return bool(self._entry.options.get("dry_run", True))

    async def _async_update_data(self) -> InnerData:
        now = datetime.now(timezone.utc)
        dt = (now - self._last_update).total_seconds() if self._last_update else float(self._opt(CONF_INNER_INTERVAL, 30))
        self._last_update = now

        dry_run_now = self._dry_run()
        if self._prev_dry_run and not dry_run_now:
            self._pid.reset()
            # Continue from the real potentiometer value if a readback sensor exists;
            # otherwise this stays a plain reset (stored value is stale after dry run)
            self._preload_pending = True
            self._restored_potentiometer = None
            _LOGGER.info("Dry run disabled — PID integral reset")
        self._prev_dry_run = dry_run_now

        # Get supply setpoint from outer coordinator; use SUPPLY_MIN when off so
        # the PID naturally drives the potentiometer to OUTPUT_MAX (pump off).
        # Until the MPC has produced a real setpoint (sensors still loading after a
        # restart), hold the potentiometer where it is instead of chasing SUPPLY_MIN.
        supply_min = float(self._opt(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN))
        outer_data = self._outer.data
        awaiting_setpoint = False
        if self._hvac_is_off():
            self._pid.setpoint = supply_min
        elif outer_data is None or not outer_data.setpoint_valid:
            awaiting_setpoint = True
        else:
            self._pid.setpoint = outer_data.supply_setpoint

        # Read supply line temp
        supply_entity = self._entry.data.get(CONF_SUPPLY_TEMP_ENTITY)
        supply_temp = self._get_sensor_value(supply_entity) if supply_entity else None
        supply_fault = supply_temp is None

        pid_error: float | None = None
        pid_p: float | None = None
        pid_i: float | None = None
        pid_d: float | None = None
        pump_protection_active = False

        if awaiting_setpoint:
            # No write and no PID step; the preload (if pending) runs on the first real cycle
            readback = self._read_potentiometer()
            potentiometer_value = readback if readback is not None else self._last_potentiometer
            inner_mode = "awaiting_setpoint"
            # Retry the outer loop now rather than waiting out its full interval
            await self._outer.async_request_refresh()
        elif supply_fault:
            potentiometer_value = self._last_potentiometer
            inner_mode = "open_loop"
        else:
            if self._preload_pending:
                self._preload_pid(supply_temp, allow_stored=True)
                self._preload_pending = False
            # Negate: supply_temp too low → error positive → raw PID positive
            # → negate → colder fake outdoor temp → more heat → supply rises.
            potentiometer_value = -self._pid(supply_temp, dt)
            inner_mode = "pid"
            pid_error = self._pid.last_error
            pid_p = self._pid.last_p
            pid_i = self._pid.last_i
            pid_d = self._pid.last_d
            self._maybe_trigger_boost()
            potentiometer_value, pump_protection_active = self._apply_pump_protection(potentiometer_value, now)
            self._last_potentiometer = potentiometer_value
            self._has_valid_output = True

        # Write to potentiometer (skip during dry run, before first valid reading, or
        # while waiting for the first MPC setpoint)
        if not self._dry_run() and self._has_valid_output and not awaiting_setpoint:
            await self._write_output(potentiometer_value)
            self._last_written = potentiometer_value
            self._save_state()

        return InnerData(
            potentiometer_value=potentiometer_value,
            supply_fault=supply_fault,
            inner_mode=inner_mode,
            current_supply_temp=supply_temp,
            dry_run=self._dry_run(),
            pump_state=self._pump_state,
            pump_protection_active=pump_protection_active,
            pid_error=pid_error,
            pid_p_term=pid_p,
            pid_i_term=pid_i,
            pid_d_term=pid_d,
        )

    def _maybe_trigger_boost(self) -> None:
        """Set _boost_active if the outer prediction shows the house cooling below target."""
        if self._boost_active or self._pump_state != "off" or self._hvac_is_off():
            return
        outer_data = self._outer.data
        if outer_data is None or outer_data.predicted_indoor_temp is None:
            return
        target = float(self._opt(CONF_TARGET_TEMPERATURE, DEFAULT_TARGET_TEMPERATURE))
        span = float(self._opt(CONF_BOOST_TRIGGER_SPAN, DEFAULT_BOOST_TRIGGER_SPAN))
        if outer_data.predicted_indoor_temp < target - span:
            self._boost_active = True
            _LOGGER.info(
                "Preventive boost triggered: predicted %.1f°C < %.1f°C",
                outer_data.predicted_indoor_temp,
                target - span,
            )

    def _apply_pump_protection(self, output: float, now: datetime) -> tuple[float, bool]:
        """Cap output to keep the pump on until it has run long enough.

        The heat exchanger turns off its circulation pump when the fake outdoor
        temp exceeds pump_off_threshold. Before allowing that, we require the
        pump to have run for at least min_pump_on_duration minutes so that
        trapped heat near the exchanger has time to radiate away (which would
        otherwise distort the supply sensor reading).
        """
        threshold = float(self._opt(CONF_PUMP_OFF_THRESHOLD, DEFAULT_PUMP_OFF_THRESHOLD))
        min_on_secs = float(self._opt(CONF_MIN_PUMP_ON_DURATION, DEFAULT_MIN_PUMP_ON_DURATION)) * 60

        if output <= threshold:
            # PID naturally wants pump on
            if self._pump_state == "off":
                self._pump_on_since = now
            self._pump_state = "on"
            self._boost_active = False
            return output, False

        # PID wants pump off
        if self._pump_state == "off":
            if self._boost_active:
                # Predictive boost: force pump on for min_pump_on_duration
                self._pump_state = "boosting"
                self._pump_on_since = now
            else:
                return output, False  # already off, no cap

        # Pump is on or boosting — check if minimum run time has elapsed
        run_secs = (now - self._pump_on_since).total_seconds()
        if run_secs >= min_on_secs:
            self._boost_active = False
            self._pump_state = "off"
            return output, False

        # Cap potentiometer to keep pump running
        # pump_protection_active only True for normal on→off protection, not during a boost
        return threshold, self._pump_state == "on"

    async def _write_output(self, value: float) -> None:
        output_service: str = self._entry.data.get(CONF_OUTPUT_SERVICE, "")
        output_key: str = self._entry.data.get(CONF_OUTPUT_VALUE_KEY, "temp")

        if not output_service or "." not in output_service:
            _LOGGER.error("Invalid output_service: %s", output_service)
            return

        domain, service = output_service.split(".", 1)
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {output_key: round(value, 2)},
                blocking=False,
            )
        except Exception as err:
            _LOGGER.warning("Failed to write potentiometer value %s: %s", value, err)


def _flat_forecast(value: float | None, length: int) -> list[float]:
    """Return a flat forecast array using the given value (or 0.0 if None)."""
    return [value if value is not None else 0.0] * length
