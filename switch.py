"""Switch entities for Heat Exchanger Controller."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import HeatExchangerRuntimeData
from .climate import _device_info, _room_subentries
from .const import CONF_DRY_RUN, DEFAULT_DRY_RUN, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HeatExchangerRuntimeData = entry.runtime_data
    entities: list[SwitchEntity] = [DryRunSwitch(entry), HourProfileSwitch(entry, runtime)]
    if _room_subentries(entry):
        entities.append(ScoringPausedSwitch(entry, runtime))
    async_add_entities(entities)


class DryRunSwitch(RestoreEntity, SwitchEntity):
    """Switch that enables/disables writing to the potentiometer."""

    _attr_has_entity_name = True
    _attr_translation_key = "dry_run"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_dry_run"
        self._attr_device_info = _device_info(entry)
        self._is_on: bool = bool(entry.options.get(CONF_DRY_RUN, DEFAULT_DRY_RUN))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == "on"

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def icon(self) -> str:
        return "mdi:flask" if self._is_on else "mdi:home-thermometer"

    async def async_turn_on(self, **kwargs) -> None:
        self._is_on = True
        await self._persist()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        await self._persist()
        self.async_write_ha_state()

    async def _persist(self) -> None:
        new_options = dict(self._entry.options)
        new_options[CONF_DRY_RUN] = self._is_on
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)


class ScoringPausedSwitch(RestoreEntity, SwitchEntity):
    """Pause room prediction scoring (open windows etc.); predictions keep running."""

    _attr_has_entity_name = True
    _attr_translation_key = "scoring_paused"
    _attr_icon = "mdi:pause-circle-outline"

    def __init__(self, entry: ConfigEntry, runtime: HeatExchangerRuntimeData) -> None:
        self._outer = runtime.outer
        self._attr_unique_id = f"{entry.entry_id}_scoring_paused"
        self._attr_device_info = _device_info(entry)
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"
        self._outer.set_scoring_paused(self._attr_is_on)

    async def async_turn_on(self, **kwargs) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._set(False)

    def _set(self, paused: bool) -> None:
        self._attr_is_on = paused
        self._outer.set_scoring_paused(paused)
        self.async_write_ha_state()


class HourProfileSwitch(RestoreEntity, SwitchEntity):
    """Apply the learned hour-of-day correction to the MPC. Learning continues when off."""

    _attr_has_entity_name = True
    _attr_translation_key = "hour_profile"
    _attr_icon = "mdi:clock-time-eight-outline"

    def __init__(self, entry: ConfigEntry, runtime: HeatExchangerRuntimeData) -> None:
        self._outer = runtime.outer
        self._attr_unique_id = f"{entry.entry_id}_hour_profile"
        self._attr_device_info = _device_info(entry)
        self._attr_is_on = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"
        self._outer.set_profile_enabled(self._attr_is_on)

    async def async_turn_on(self, **kwargs) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._set(False)

    def _set(self, enabled: bool) -> None:
        # Takes effect on the next outer tick; a refresh would also step the observer
        self._attr_is_on = enabled
        self._outer.set_profile_enabled(enabled)
        self.async_write_ha_state()
