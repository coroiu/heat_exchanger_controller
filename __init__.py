"""Heat Exchanger Controller integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_HVAC_MODE,
    CONF_INNER_INTERVAL,
    CONF_OUTER_INTERVAL,
    CONF_TARGET_TEMPERATURE,
    DEFAULT_INNER_INTERVAL,
    DEFAULT_OUTER_INTERVAL,
    DOMAIN,
    PLATFORMS,
    RELOAD_ON_CHANGE_KEYS,
)
from .coordinator import InnerCoordinator, OuterCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class HeatExchangerRuntimeData:
    outer: OuterCoordinator
    inner: InnerCoordinator
    structural_opts_snapshot: dict


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Heat Exchanger Controller from a config entry."""
    outer_minutes = int(entry.options.get(CONF_OUTER_INTERVAL, DEFAULT_OUTER_INTERVAL))
    inner_seconds = int(entry.options.get(CONF_INNER_INTERVAL, DEFAULT_INNER_INTERVAL))

    outer = OuterCoordinator(
        hass, entry, update_interval=timedelta(minutes=outer_minutes)
    )
    inner = InnerCoordinator(
        hass, entry, outer, update_interval=timedelta(seconds=inner_seconds)
    )

    # First refresh — outer coordinator must succeed at least partially.
    # Fault-tolerant: will not raise unless the coordinator itself errors out.
    try:
        await outer.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Outer coordinator failed on first refresh: {err}") from err

    structural_snapshot = {k: entry.options.get(k) for k in RELOAD_ON_CHANGE_KEYS}
    entry.runtime_data = HeatExchangerRuntimeData(
        outer=outer, inner=inner, structural_opts_snapshot=structural_snapshot
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry.runtime_data = None
    return unload_ok


async def _async_options_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on structural option changes; refresh only for climate state changes."""
    # Determine which keys changed by comparing to defaults stored in runtime_data
    # We don't have old options easily, so we check if any reload-triggering key is present.
    # Since all options are always written together, we check the RELOAD_ON_CHANGE_KEYS subset.
    # If ANY of those keys exist in the new options dict (they always do), we reload.
    # To avoid reloading on pure climate state changes, we track the last structural hash.
    runtime: HeatExchangerRuntimeData | None = entry.runtime_data
    if runtime is None:
        return

    # If only hvac_mode or target_temperature changed, skip reload.
    # We detect this by checking if the inner coordinator's PID params need updating.
    # Simplest heuristic: always reload on options change EXCEPT when only climate keys differ.
    # Since options flow only changes structural params, and climate entity writes only
    # target_temperature/hvac_mode via async_update_entry, we check which keys changed.

    curr_structural = {k: entry.options.get(k) for k in RELOAD_ON_CHANGE_KEYS}

    if curr_structural == runtime.structural_opts_snapshot:
        # Only climate state keys changed (target_temperature / hvac_mode) — no reload needed
        await runtime.outer.async_request_refresh()
        return

    # Structural params changed — full reload
    await hass.config_entries.async_reload(entry.entry_id)
