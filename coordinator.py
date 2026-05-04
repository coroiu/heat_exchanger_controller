"""Coordinators for Heat Exchanger Controller."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HVAC_MODE,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INNER_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_OUTDOOR_TEMP_ENTITIES,
    CONF_OUTPUT_MIN,
    CONF_OUTPUT_MAX,
    CONF_OUTPUT_SERVICE,
    CONF_OUTPUT_VALUE_KEY,
    CONF_PID_KD,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PREDICTION_HORIZON,
    CONF_RC_C,
    CONF_RC_G,
    CONF_RC_R,
    CONF_RC_UA,
    CONF_SUPPLY_MAX,
    CONF_SUPPLY_MIN,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TARGET_TEMPERATURE,
    CONF_WEATHER_ENTITY,
    DEFAULT_HVAC_MODE,
    DEFAULT_OUTPUT_MAX,
    DEFAULT_OUTPUT_MIN,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PREDICTION_HORIZON,
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

# Cache Open-Meteo data for 45 minutes to avoid hammering the free API
_OPEN_METEO_CACHE_SECONDS = 2700


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


@dataclass
class InnerData:
    potentiometer_value: float
    supply_fault: bool
    inner_mode: str  # "pid" or "open_loop"
    current_supply_temp: float | None
    dry_run: bool


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
        self._last_outdoor_temp: float | None = None
        self._open_meteo_cache: list[float] | None = None
        self._open_meteo_cache_time: datetime | None = None

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

        # --- RC-MPC ---
        supply_setpoint, predicted_indoor = self._run_mpc(
            t_in, target_temp, outdoor_temp, smhi_outdoor_forecast, solar_forecast, faults
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

        hvac_mode = self._entry.options.get(CONF_HVAC_MODE, DEFAULT_HVAC_MODE)
        controller_action = self._compute_controller_action(
            hvac_mode, status, supply_setpoint, predicted_indoor, target_temp, indoor_temp, faults
        )

        return OuterData(
            supply_setpoint=supply_setpoint,
            predicted_indoor_temp=predicted_indoor,
            outdoor_temp_source=outdoor_source,
            solar_irradiance=solar_now,
            indoor_temp=indoor_temp,
            current_outdoor_temp=outdoor_temp,
            faults=faults,
            status=status,
            smhi_available=smhi_available,
            open_meteo_available=open_meteo_available,
            controller_action=controller_action,
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
        horizon = int(self._opt(CONF_PREDICTION_HORIZON, DEFAULT_PREDICTION_HORIZON))

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
        horizon = int(self._opt(CONF_PREDICTION_HORIZON, DEFAULT_PREDICTION_HORIZON))
        now = datetime.now(timezone.utc)

        # Return cached data if still fresh
        if (
            self._open_meteo_cache is not None
            and self._open_meteo_cache_time is not None
            and (now - self._open_meteo_cache_time).total_seconds() < _OPEN_METEO_CACHE_SECONDS
        ):
            return self._open_meteo_cache[:horizon], True

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

            # Find current hour index
            current_hour = now.strftime("%Y-%m-%dT%H:00")
            try:
                idx = times.index(current_hour)
            except ValueError:
                # Use closest future time
                idx = 0
                for i, t in enumerate(times):
                    if t >= current_hour:
                        idx = i
                        break

            solar_slice = radiation[idx: idx + horizon]
            while len(solar_slice) < horizon:
                solar_slice.append(0.0)

            self._open_meteo_cache = solar_slice
            self._open_meteo_cache_time = now
            return solar_slice, True

        except Exception as err:
            _LOGGER.debug("Open-Meteo unavailable: %s", err)
            faults.append("open_meteo_unavailable")
            return [0.0] * horizon, False

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

        try:
            setpoint, predicted = compute_supply_setpoint(
                t_in=t_in,
                t_setpoint=target_temp,
                outdoor_temps=smhi_forecast,
                solar_irradiance=solar_forecast,
                R=R,
                C=C,
                UA=UA,
                g=g,
                supply_min=supply_min,
                supply_max=supply_max,
            )
            self._last_supply_setpoint = setpoint
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
        self._last_potentiometer: float = entry.options.get(CONF_OUTPUT_MIN, DEFAULT_OUTPUT_MIN)
        self._last_update: datetime | None = None
        self._pid = self._build_pid()

    def _opt(self, key: str, default: Any) -> Any:
        return self._entry.options.get(key, default)

    def _build_pid(self) -> SimplePID:
        supply_setpoint = (
            self._outer.data.supply_setpoint
            if self._outer.data is not None
            else float(self._opt(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN))
        )
        return SimplePID(
            kp=float(self._opt(CONF_PID_KP, DEFAULT_PID_KP)),
            ki=float(self._opt(CONF_PID_KI, DEFAULT_PID_KI)),
            kd=float(self._opt(CONF_PID_KD, DEFAULT_PID_KD)),
            setpoint=supply_setpoint,
            output_limits=(
                float(self._opt(CONF_OUTPUT_MIN, DEFAULT_OUTPUT_MIN)),
                float(self._opt(CONF_OUTPUT_MAX, DEFAULT_OUTPUT_MAX)),
            ),
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

        # Get supply setpoint from outer coordinator
        supply_setpoint = (
            self._outer.data.supply_setpoint
            if self._outer.data is not None
            else float(self._opt(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN))
        )
        self._pid.setpoint = supply_setpoint

        # Read supply line temp
        supply_entity = self._entry.data.get(CONF_SUPPLY_TEMP_ENTITY)
        supply_temp = self._get_sensor_value(supply_entity) if supply_entity else None
        supply_fault = supply_temp is None

        if supply_fault:
            potentiometer_value = self._last_potentiometer
            inner_mode = "open_loop"
        else:
            potentiometer_value = self._pid(supply_temp, dt)
            self._last_potentiometer = potentiometer_value
            inner_mode = "pid"

        # Write to potentiometer (skip if off or dry run)
        if not self._hvac_is_off() and not self._dry_run():
            await self._write_output(potentiometer_value)

        return InnerData(
            potentiometer_value=potentiometer_value,
            supply_fault=supply_fault,
            inner_mode=inner_mode,
            current_supply_temp=supply_temp,
            dry_run=self._dry_run(),
        )

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
