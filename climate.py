"""Climate entity for Heat Exchanger Controller."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HeatExchangerRuntimeData
from .const import (
    CONF_CLIMATE_MAX_TEMP,
    CONF_CLIMATE_MIN_TEMP,
    CONF_HVAC_MODE,
    CONF_SUPPLY_MIN,
    CONF_SUPPLY_MAX,
    CONF_TARGET_TEMPERATURE,
    DEFAULT_CLIMATE_MAX_TEMP,
    DEFAULT_CLIMATE_MIN_TEMP,
    DEFAULT_HVAC_MODE,
    DEFAULT_SUPPLY_MAX,
    DEFAULT_SUPPLY_MIN,
    DEFAULT_TARGET_TEMPERATURE,
    DOMAIN,
)
from .coordinator import OuterCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HeatExchangerRuntimeData = entry.runtime_data
    async_add_entities([HeatExchangerClimate(entry, runtime)])


class HeatExchangerClimate(
    CoordinatorEntity[OuterCoordinator], ClimateEntity, RestoreEntity
):
    _attr_has_entity_name = True
    _attr_translation_key = "heat_exchanger_climate"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.1

    def __init__(self, entry: ConfigEntry, runtime: HeatExchangerRuntimeData) -> None:
        super().__init__(runtime.outer)
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self._attr_device_info = _device_info(entry)

        self._target_temp: float = entry.options.get(
            CONF_TARGET_TEMPERATURE, DEFAULT_TARGET_TEMPERATURE
        )
        self._hvac_mode: HVACMode = _parse_hvac_mode(
            entry.options.get(CONF_HVAC_MODE, DEFAULT_HVAC_MODE)
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            if last_state.attributes.get("temperature") is not None:
                try:
                    self._target_temp = float(last_state.attributes["temperature"])
                except (ValueError, TypeError):
                    pass
            if last_state.state in (HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL):
                self._hvac_mode = HVACMode(last_state.state)

    @property
    def min_temp(self) -> float:
        return float(self._entry.options.get(CONF_CLIMATE_MIN_TEMP, DEFAULT_CLIMATE_MIN_TEMP))

    @property
    def max_temp(self) -> float:
        return float(self._entry.options.get(CONF_CLIMATE_MAX_TEMP, DEFAULT_CLIMATE_MAX_TEMP))

    @property
    def current_temperature(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.indoor_temp

    @property
    def target_temperature(self) -> float:
        return self._target_temp

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF

        data = self.coordinator.data
        inner_data = self._runtime.inner.data
        if data is None or inner_data is None:
            return HVACAction.IDLE

        supply_setpoint = data.supply_setpoint
        current_supply = inner_data.current_supply_temp

        if current_supply is None:
            return HVACAction.IDLE

        diff = supply_setpoint - current_supply
        if diff > 0.5:
            return HVACAction.HEATING
        if diff < -0.5:
            return HVACAction.COOLING
        return HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        self._target_temp = float(temp)
        await self._persist_climate_state()
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._hvac_mode = hvac_mode
        await self._persist_climate_state()
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def _persist_climate_state(self) -> None:
        """Save target_temperature and hvac_mode to options without triggering reload."""
        new_options = dict(self._entry.options)
        new_options[CONF_TARGET_TEMPERATURE] = self._target_temp
        new_options[CONF_HVAC_MODE] = self._hvac_mode.value
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)


def _parse_hvac_mode(value: str) -> HVACMode:
    try:
        return HVACMode(value)
    except ValueError:
        return HVACMode.HEAT


def _device_info(entry: ConfigEntry) -> dict:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": entry.title,
        "manufacturer": "Custom",
        "model": "Heat Exchanger Controller",
    }
