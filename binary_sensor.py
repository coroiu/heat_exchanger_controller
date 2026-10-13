"""Binary sensor entities for Heat Exchanger Controller."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HeatExchangerRuntimeData
from .climate import _device_info, _room_subentries
from .coordinator import OuterCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: HeatExchangerRuntimeData = entry.runtime_data
    if _room_subentries(entry):
        async_add_entities([WindowSuspectedBinarySensor(entry, runtime.outer)])


class WindowSuspectedBinarySensor(CoordinatorEntity[OuterCoordinator], BinarySensorEntity):
    """On when a room cools clearly faster than with no heat at all. Diagnostic only."""

    _attr_has_entity_name = True
    _attr_translation_key = "window_suspected"
    _attr_device_class = BinarySensorDeviceClass.WINDOW

    def __init__(self, entry: ConfigEntry, coordinator: OuterCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_window_suspected"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.window_suspected

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}
        return {
            "rooms": [
                self._entry.subentries[sid].title
                for sid, r in data.rooms.items()
                if r is not None and r.anomaly and sid in self._entry.subentries
            ]
        }
