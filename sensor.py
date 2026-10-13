"""Sensor entities for Heat Exchanger Controller."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import UnitOfIrradiance, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HeatExchangerRuntimeData
from .climate import _device_info, _room_device_info, _room_subentries
from .const import DOMAIN
from .coordinator import InnerCoordinator, OuterCoordinator
from .path_mpc import PathResult
from .room_model import RoomResult


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HeatExchangerRuntimeData = entry.runtime_data
    async_add_entities(
        [
            StatusSensor(entry, runtime),
            ControllerActionSensor(entry, runtime.outer, runtime.inner),
            SupplySetpointSensor(entry, runtime.outer),
            PredictedIndoorTempSensor(entry, runtime.outer),
            OutdoorTempSourceSensor(entry, runtime.outer),
            SolarIrradianceSensor(entry, runtime.outer),
            IndoorTempUsedSensor(entry, runtime.outer),
            OutdoorTempUsedSensor(entry, runtime.outer),
            SupplyTempActualSensor(entry, runtime.inner),
            PotentiometerOutputSensor(entry, runtime.inner),
            PidErrorSensor(entry, runtime.inner),
            PidPTermSensor(entry, runtime.inner),
            PidITermSensor(entry, runtime.inner),
            PidDTermSensor(entry, runtime.inner),
            PumpStateSensor(entry, runtime.inner),
            SmhiForecastSensor(entry, runtime.outer),
            PlannedIndoorTempSensor(entry, runtime.outer),
        ]
    )
    rooms = _room_subentries(entry)
    if rooms:
        async_add_entities([
            ShadowSupplySetpointSensor(entry, runtime.outer),
            PathSupplySetpointSensor(entry, runtime.outer),
        ])
    for sub in rooms:
        async_add_entities(
            [
                RoomImportanceRampedSensor(entry, sub, runtime.outer),
                RoomPredictedTempSensor(entry, sub, runtime.outer),
                RoomSupplySetpointSensor(entry, sub, runtime.outer),
                RoomPredictionErrorSensor(entry, sub, runtime.outer),
                RoomPlannedTempSensor(entry, sub, runtime.outer),
            ],
            config_subentry_id=sub.subentry_id,
        )


class _OuterSensor(CoordinatorEntity[OuterCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _device_info(entry)


class _InnerSensor(CoordinatorEntity[InnerCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: InnerCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _device_info(entry)


class StatusSensor(_OuterSensor):
    _attr_translation_key = "status"

    def __init__(self, entry: ConfigEntry, runtime: HeatExchangerRuntimeData) -> None:
        super().__init__(entry, runtime.outer)
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.status

    @property
    def extra_state_attributes(self) -> dict:
        outer = self.coordinator.data
        inner = self._runtime.inner.data
        attrs: dict = {}

        if outer is not None:
            attrs["active_faults"] = outer.faults
            attrs["smhi_available"] = outer.smhi_available
            attrs["open_meteo_available"] = outer.open_meteo_available
            attrs["outdoor_temp_source"] = outer.outdoor_temp_source
            attrs["indoor_temp_assumed"] = outer.indoor_temp is None
            if outer.shadow_faults:
                attrs["shadow_faults"] = outer.shadow_faults

        if inner is not None:
            attrs["inner_loop_mode"] = inner.inner_mode
            attrs["supply_temp_available"] = not inner.supply_fault
            attrs["dry_run"] = inner.dry_run

        return attrs


class ControllerActionSensor(_OuterSensor):
    _attr_translation_key = "controller_action"

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator, inner: InnerCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._inner = inner
        self._attr_unique_id = f"{entry.entry_id}_controller_action"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        action = self.coordinator.data.controller_action
        inner = self._inner.data
        if inner is not None:
            if inner.pump_protection_active:
                action += " (pump held on)"
            elif inner.pump_state == "off":
                action += " (pump off)"
        return action


class SupplySetpointSensor(_OuterSensor):
    _attr_translation_key = "supply_temp_setpoint"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_supply_temp_setpoint"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.supply_setpoint, 2)


class PredictedIndoorTempSensor(_OuterSensor):
    _attr_translation_key = "predicted_indoor_temp"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_predicted_indoor_temp"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.predicted_indoor_temp
        return round(v, 2) if v is not None else None


class OutdoorTempSourceSensor(_OuterSensor):
    _attr_translation_key = "outdoor_temp_source"

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_outdoor_temp_source"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.outdoor_temp_source


class SolarIrradianceSensor(_OuterSensor):
    _attr_translation_key = "solar_irradiance"
    _attr_device_class = SensorDeviceClass.IRRADIANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfIrradiance.WATTS_PER_SQUARE_METER

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_solar_irradiance"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.solar_irradiance
        return round(v, 1) if v is not None else None


class IndoorTempUsedSensor(_OuterSensor):
    """Indoor temperature actually used by the MPC (None → assumed = setpoint)."""

    _attr_translation_key = "indoor_temp_used"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_indoor_temp_used"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.indoor_temp
        return round(v, 2) if v is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {"assumed": self.coordinator.data.indoor_temp is None}


class OutdoorTempUsedSensor(_OuterSensor):
    """Outdoor temperature actually used by the MPC (after fallback resolution)."""

    _attr_translation_key = "outdoor_temp_used"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_outdoor_temp_used"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.current_outdoor_temp
        return round(v, 2) if v is not None else None


class SupplyTempActualSensor(_InnerSensor):
    """Actual supply line temperature read by the inner loop."""

    _attr_translation_key = "supply_temp_actual"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: InnerCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_supply_temp_actual"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.current_supply_temp
        return round(v, 2) if v is not None else None


class PotentiometerOutputSensor(_InnerSensor):
    """Current value being written to the potentiometer (fake outdoor temp)."""

    _attr_translation_key = "potentiometer_output"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: InnerCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_potentiometer_output"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.potentiometer_value, 2)


class PidErrorSensor(_InnerSensor):
    """Supply temperature tracking error (setpoint − actual)."""

    _attr_translation_key = "pid_error"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: InnerCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pid_error"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.pid_error
        return round(v, 3) if v is not None else None


class PidPTermSensor(_InnerSensor):
    """PID proportional term (Kp × error)."""

    _attr_translation_key = "pid_p_term"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: InnerCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pid_p_term"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.pid_p_term
        return round(v, 3) if v is not None else None


class PidITermSensor(_InnerSensor):
    """PID integral term (Ki × accumulated error)."""

    _attr_translation_key = "pid_i_term"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: InnerCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pid_i_term"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.pid_i_term
        return round(v, 3) if v is not None else None


class PidDTermSensor(_InnerSensor):
    """PID derivative term (Kd × rate of error change)."""

    _attr_translation_key = "pid_d_term"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: InnerCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pid_d_term"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        v = self.coordinator.data.pid_d_term
        return round(v, 3) if v is not None else None


class SmhiForecastSensor(_OuterSensor):
    """Hourly outdoor temperature forecast fed to the RC-MPC."""

    _attr_translation_key = "smhi_forecast"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_smhi_forecast"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        fc = self.coordinator.data.smhi_forecast
        return round(fc[0], 1) if fc else None

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        fc = self.coordinator.data.smhi_forecast
        return {
            "forecast": [round(t, 1) for t in fc],
            "smhi_available": self.coordinator.data.smhi_available,
        }


class PumpStateSensor(_InnerSensor):
    """Inferred circulation pump state based on potentiometer output vs threshold."""

    _attr_translation_key = "pump_state"

    def __init__(self, entry: ConfigEntry, coordinator: InnerCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pump_state"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.pump_state

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {"protection_active": self.coordinator.data.pump_protection_active}


class ShadowSupplySetpointSensor(_OuterSensor):
    """Importance-weighted mean of the room setpoints (shadow mode, not applied)."""

    _attr_translation_key = "shadow_supply_setpoint"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_shadow_supply_setpoint"

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and data.shadow_supply_setpoint is not None

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None or self.coordinator.data.shadow_supply_setpoint is None:
            return None
        return round(self.coordinator.data.shadow_supply_setpoint, 2)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}
        names = {sid: sub.title for sid, sub in self._entry.subentries.items()}
        return {
            "max_rule": round(data.shadow_supply_max, 2) if data.shadow_supply_max is not None else None,
            "weights": {names.get(sid, sid): round(w, 3) for sid, w in data.shadow_weights.items()},
            "live_supply_setpoint": round(data.supply_setpoint, 2),
        }


class _RoomSensor(CoordinatorEntity[OuterCoordinator], SensorEntity):
    """A room's shadow-mode value; unavailable while the room's sensor is."""

    _attr_has_entity_name = True
    _key = ""

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry, coordinator: OuterCoordinator) -> None:
        super().__init__(coordinator)
        self._subentry_id = subentry.subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{subentry.subentry_id}_{self._key}"
        self._attr_device_info = _room_device_info(entry, subentry)

    @property
    def _room(self) -> RoomResult | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.rooms.get(self._subentry_id)

    @property
    def available(self) -> bool:
        return super().available and self._room is not None


class _RoomTempSensor(_RoomSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS


class RoomImportanceRampedSensor(_RoomSensor):
    _attr_translation_key = "room_importance_ramped"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "importance_ramped"

    @property
    def native_value(self) -> float | None:
        room = self._room
        return round(room.importance_ramped, 3) if room else None


class RoomPredictedTempSensor(_RoomTempSensor):
    """Room temperature at the control horizon under the live supply setpoint."""

    _attr_translation_key = "room_predicted_temp"
    _key = "predicted_temp"

    @property
    def native_value(self) -> float | None:
        room = self._room
        return round(room.predicted_temp, 2) if room else None


class RoomSupplySetpointSensor(_RoomTempSensor):
    """Supply this room's own MPC would request to reach its target."""

    _attr_translation_key = "room_supply_setpoint"
    _key = "supply_setpoint"

    @property
    def native_value(self) -> float | None:
        room = self._room
        return round(room.supply_setpoint, 2) if room else None

    @property
    def extra_state_attributes(self) -> dict:
        room = self._room
        return {"target": room.target, "temperature": room.temperature} if room else {}


class RoomPredictionErrorSensor(_RoomSensor):
    """Rolling 24 h RMS of the 3 h-ahead prediction error."""

    _attr_translation_key = "room_prediction_error"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _key = "prediction_error"

    @property
    def native_value(self) -> float | None:
        room = self._room
        return round(room.error_rms, 3) if room and room.error_rms is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        room = self._room
        if room is None:
            return {}
        return {
            "bias": round(room.error_bias, 3) if room.error_bias is not None else None,
            "samples": room.error_samples,
            "anomaly": room.anomaly,
        }


def _plan_attr(start: datetime, result: PathResult) -> list[dict]:
    """Hourly supply plan; entry k applies from start + k h."""
    return [
        {"t": (start + timedelta(hours=k)).isoformat(), "supply": round(v, 2)}
        for k, v in enumerate(result.plan)
    ]


def _path_attr(start: datetime, temps: list[float]) -> list[dict]:
    """Planned temperatures at start + 1 h … start + N h."""
    return [
        {"t": (start + timedelta(hours=k + 1)).isoformat(), "temp": round(v, 2)}
        for k, v in enumerate(temps)
    ]


class PathSupplySetpointSensor(_OuterSensor):
    """First hour of the path MPC plan for the rooms (shadow mode, not applied)."""

    _attr_translation_key = "path_supply_setpoint"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_path_supply_setpoint"

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and data.path is not None

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None or data.path is None:
            return None
        return round(data.path.plan[0], 2)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None or data.path is None or data.path_start is None:
            return {}
        attrs = {
            "plan": _plan_attr(data.path_start, data.path),
            "cost": {"comfort": round(data.path.cost_comfort, 4), "smooth": round(data.path.cost_smooth, 4)},
            "converged": data.path.converged,
            "iterations": data.path.iterations,
            "live_supply_setpoint": round(data.supply_setpoint, 2),
        }
        if data.house_path is not None:
            attrs["house_plan"] = _plan_attr(data.path_start, data.house_path)
            attrs["house_first_hour"] = round(data.house_path.plan[0], 2)
        return attrs


class PlannedIndoorTempSensor(_OuterSensor):
    """House model's planned indoor temperature in 1 h, with the whole planned path."""

    _attr_translation_key = "planned_indoor_temp"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_planned_indoor_temp"

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and data.house_path is not None

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None or data.house_path is None:
            return None
        return round(data.house_path.paths[0][0], 2)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None or data.house_path is None or data.path_start is None:
            return {}
        return {
            "path": _path_attr(data.path_start, data.house_path.paths[0]),
            "plan": _plan_attr(data.path_start, data.house_path),
            "converged": data.house_path.converged,
        }


class RoomPlannedTempSensor(_RoomTempSensor):
    """Room temperature in 1 h under the rooms plan, with the whole planned path."""

    _attr_translation_key = "room_planned_temp"
    _key = "planned_temp"

    def _temps(self) -> list[float] | None:
        data = self.coordinator.data
        if data is None or data.path is None or self._subentry_id not in data.path_rooms:
            return None
        return data.path.paths[data.path_rooms.index(self._subentry_id)]

    @property
    def available(self) -> bool:
        return super().available and self._temps() is not None

    @property
    def native_value(self) -> float | None:
        temps = self._temps()
        return round(temps[0], 2) if temps else None

    @property
    def extra_state_attributes(self) -> dict:
        temps = self._temps()
        start = self.coordinator.data.path_start if self.coordinator.data else None
        if temps is None or start is None:
            return {}
        return {"path": _path_attr(start, temps)}
