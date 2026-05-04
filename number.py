"""Number entities for Heat Exchanger Controller tunable parameters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HeatExchangerRuntimeData
from .climate import _device_info
from .const import (
    CONF_INNER_INTERVAL,
    CONF_OUTER_INTERVAL,
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
    CONF_OUTPUT_MIN,
    CONF_OUTPUT_MAX,
    DEFAULT_INNER_INTERVAL,
    DEFAULT_OUTER_INTERVAL,
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
    DOMAIN,
    RELOAD_ON_CHANGE_KEYS,
)


@dataclass(frozen=True)
class HeatExchangerNumberDescription(NumberEntityDescription):
    conf_key: str = ""
    default: float = 0.0


_NUMBERS: tuple[HeatExchangerNumberDescription, ...] = (
    HeatExchangerNumberDescription(
        key="rc_r", translation_key="rc_r", conf_key=CONF_RC_R, default=DEFAULT_RC_R,
        native_min_value=0.001, native_max_value=1.0, native_step=0.001,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="rc_c", translation_key="rc_c", conf_key=CONF_RC_C, default=DEFAULT_RC_C,
        native_min_value=10.0, native_max_value=10000.0, native_step=10.0,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="rc_ua", translation_key="rc_ua", conf_key=CONF_RC_UA, default=DEFAULT_RC_UA,
        native_min_value=0.1, native_max_value=500.0, native_step=0.1,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="rc_g", translation_key="rc_g", conf_key=CONF_RC_G, default=DEFAULT_RC_G,
        native_min_value=0.0, native_max_value=100.0, native_step=0.1,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="pid_kp", translation_key="pid_kp", conf_key=CONF_PID_KP, default=DEFAULT_PID_KP,
        native_min_value=0.0, native_max_value=100.0, native_step=0.01,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="pid_ki", translation_key="pid_ki", conf_key=CONF_PID_KI, default=DEFAULT_PID_KI,
        native_min_value=0.0, native_max_value=10.0, native_step=0.001,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="pid_kd", translation_key="pid_kd", conf_key=CONF_PID_KD, default=DEFAULT_PID_KD,
        native_min_value=0.0, native_max_value=100.0, native_step=0.01,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="supply_min", translation_key="supply_min", conf_key=CONF_SUPPLY_MIN, default=DEFAULT_SUPPLY_MIN,
        native_min_value=-10.0, native_max_value=40.0, native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="supply_max", translation_key="supply_max", conf_key=CONF_SUPPLY_MAX, default=DEFAULT_SUPPLY_MAX,
        native_min_value=20.0, native_max_value=100.0, native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="output_min", translation_key="output_min", conf_key=CONF_OUTPUT_MIN, default=DEFAULT_OUTPUT_MIN,
        native_min_value=-50.0, native_max_value=30.0, native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="output_max", translation_key="output_max", conf_key=CONF_OUTPUT_MAX, default=DEFAULT_OUTPUT_MAX,
        native_min_value=0.0, native_max_value=50.0, native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="prediction_horizon", translation_key="prediction_horizon", conf_key=CONF_PREDICTION_HORIZON,
        default=DEFAULT_PREDICTION_HORIZON,
        native_min_value=1, native_max_value=12, native_step=1,
        native_unit_of_measurement=UnitOfTime.HOURS,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="outer_interval", translation_key="outer_interval", conf_key=CONF_OUTER_INTERVAL,
        default=DEFAULT_OUTER_INTERVAL,
        native_min_value=1, native_max_value=60, native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
    ),
    HeatExchangerNumberDescription(
        key="inner_interval", translation_key="inner_interval", conf_key=CONF_INNER_INTERVAL,
        default=DEFAULT_INNER_INTERVAL,
        native_min_value=10, native_max_value=300, native_step=10,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [HeatExchangerNumber(entry, desc) for desc in _NUMBERS]
    )


class HeatExchangerNumber(NumberEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        description: HeatExchangerNumberDescription,
    ) -> None:
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float:
        return float(self._entry.options.get(self.entity_description.conf_key, self.entity_description.default))

    async def async_set_native_value(self, value: float) -> None:
        new_options = dict(self._entry.options)
        new_options[self.entity_description.conf_key] = value
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()
