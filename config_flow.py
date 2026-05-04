"""Config flow for Heat Exchanger Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CLIMATE_MAX_TEMP,
    CONF_CLIMATE_MIN_TEMP,
    CONF_DRY_RUN,
    CONF_HVAC_MODE,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INNER_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_OUTDOOR_TEMP_ENTITIES,
    CONF_OUTPUT_SERVICE,
    CONF_OUTPUT_VALUE_KEY,
    CONF_OUTPUT_MIN,
    CONF_OUTPUT_MAX,
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
    CONF_OUTER_INTERVAL,
    CONF_WEATHER_ENTITY,
    DEFAULT_CLIMATE_MAX_TEMP,
    DEFAULT_CLIMATE_MIN_TEMP,
    DEFAULT_DRY_RUN,
    DEFAULT_HVAC_MODE,
    DEFAULT_INNER_INTERVAL,
    DEFAULT_OUTPUT_MAX,
    DEFAULT_OUTPUT_MIN,
    DEFAULT_OUTER_INTERVAL,
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
)


class HeatExchangerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_OUTDOOR_TEMP_ENTITIES):
                errors[CONF_OUTDOOR_TEMP_ENTITIES] = "no_outdoor_sensors"
            else:
                data = dict(user_input)
                data[CONF_LATITUDE] = self.hass.config.latitude
                data[CONF_LONGITUDE] = self.hass.config.longitude

                options = _default_options()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=data,
                    options=options,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Heat Exchanger"): selector.TextSelector(),
                vol.Required(CONF_INDOOR_TEMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_OUTDOOR_TEMP_ENTITIES): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", multiple=True)
                ),
                vol.Required(CONF_SUPPLY_TEMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_OUTPUT_SERVICE,
                    default="rest_command.set_heat_exchanger_temp",
                ): selector.TextSelector(),
                vol.Required(
                    CONF_OUTPUT_VALUE_KEY,
                    default="temp",
                ): selector.TextSelector(),
                vol.Required(CONF_WEATHER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HeatExchangerOptionsFlow()


class HeatExchangerOptionsFlow(OptionsFlow):
    """Handle options updates."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Preserve climate state keys that aren't in this form
            merged = dict(self.config_entry.options)
            merged.update(user_input)
            return self.async_create_entry(data=merged)

        opts = self.config_entry.options

        schema = vol.Schema(
            {
                vol.Required(CONF_RC_R, default=opts.get(CONF_RC_R, DEFAULT_RC_R)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.001, max=1.0, step=0.001, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_RC_C, default=opts.get(CONF_RC_C, DEFAULT_RC_C)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10.0, max=10000.0, step=10.0, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_RC_UA, default=opts.get(CONF_RC_UA, DEFAULT_RC_UA)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.1, max=500.0, step=0.1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_RC_G, default=opts.get(CONF_RC_G, DEFAULT_RC_G)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.0, max=100.0, step=0.1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_PID_KP, default=opts.get(CONF_PID_KP, DEFAULT_PID_KP)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.0, max=100.0, step=0.01, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_PID_KI, default=opts.get(CONF_PID_KI, DEFAULT_PID_KI)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.0, max=10.0, step=0.001, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_PID_KD, default=opts.get(CONF_PID_KD, DEFAULT_PID_KD)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.0, max=100.0, step=0.01, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_SUPPLY_MIN, default=opts.get(CONF_SUPPLY_MIN, DEFAULT_SUPPLY_MIN)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=-10.0, max=40.0, step=0.5, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_SUPPLY_MAX, default=opts.get(CONF_SUPPLY_MAX, DEFAULT_SUPPLY_MAX)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=20.0, max=100.0, step=0.5, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_OUTPUT_MIN, default=opts.get(CONF_OUTPUT_MIN, DEFAULT_OUTPUT_MIN)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=-50.0, max=30.0, step=0.5, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_OUTPUT_MAX, default=opts.get(CONF_OUTPUT_MAX, DEFAULT_OUTPUT_MAX)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.0, max=50.0, step=0.5, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_PREDICTION_HORIZON, default=opts.get(CONF_PREDICTION_HORIZON, DEFAULT_PREDICTION_HORIZON)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=12, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_OUTER_INTERVAL, default=opts.get(CONF_OUTER_INTERVAL, DEFAULT_OUTER_INTERVAL)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_INNER_INTERVAL, default=opts.get(CONF_INNER_INTERVAL, DEFAULT_INNER_INTERVAL)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=300, step=10, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_CLIMATE_MIN_TEMP, default=opts.get(CONF_CLIMATE_MIN_TEMP, DEFAULT_CLIMATE_MIN_TEMP)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5.0, max=20.0, step=0.5, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_CLIMATE_MAX_TEMP, default=opts.get(CONF_CLIMATE_MAX_TEMP, DEFAULT_CLIMATE_MAX_TEMP)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=18.0, max=35.0, step=0.5, mode=selector.NumberSelectorMode.BOX)
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)


def _default_options() -> dict[str, Any]:
    return {
        CONF_RC_R: DEFAULT_RC_R,
        CONF_RC_C: DEFAULT_RC_C,
        CONF_RC_UA: DEFAULT_RC_UA,
        CONF_RC_G: DEFAULT_RC_G,
        CONF_PID_KP: DEFAULT_PID_KP,
        CONF_PID_KI: DEFAULT_PID_KI,
        CONF_PID_KD: DEFAULT_PID_KD,
        CONF_SUPPLY_MIN: DEFAULT_SUPPLY_MIN,
        CONF_SUPPLY_MAX: DEFAULT_SUPPLY_MAX,
        CONF_OUTPUT_MIN: DEFAULT_OUTPUT_MIN,
        CONF_OUTPUT_MAX: DEFAULT_OUTPUT_MAX,
        CONF_PREDICTION_HORIZON: DEFAULT_PREDICTION_HORIZON,
        CONF_OUTER_INTERVAL: DEFAULT_OUTER_INTERVAL,
        CONF_INNER_INTERVAL: DEFAULT_INNER_INTERVAL,
        CONF_CLIMATE_MIN_TEMP: DEFAULT_CLIMATE_MIN_TEMP,
        CONF_CLIMATE_MAX_TEMP: DEFAULT_CLIMATE_MAX_TEMP,
        CONF_TARGET_TEMPERATURE: DEFAULT_TARGET_TEMPERATURE,
        CONF_HVAC_MODE: DEFAULT_HVAC_MODE,
        CONF_DRY_RUN: DEFAULT_DRY_RUN,
    }
