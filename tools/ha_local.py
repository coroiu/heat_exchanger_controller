"""Install-specific defaults for the offline tools, read from the local HA config.

Nothing here is hardcoded: the recorder DB, location and sensor entities come from
the HA config directory and this integration's config entry, so the tools work on
any install without editing.

HA config directory: $HA_CONFIG, else three levels up from this file
(<config>/custom_components/heat_exchanger_controller/tools/).
Overrides: $HA_DB, $HA_LAT, $HA_LON.
"""
import json
import os
from pathlib import Path

DOMAIN = "heat_exchanger_controller"


def config_dir() -> Path:
    env = os.environ.get("HA_CONFIG")
    return Path(env) if env else Path(__file__).resolve().parents[3]


def db_path() -> str:
    return os.environ.get("HA_DB") or str(config_dir() / "home-assistant_v2.db")


def entry_data() -> dict:
    """The integration's config entry data ({} if not found)."""
    try:
        entries = json.loads((config_dir() / ".storage" / "core.config_entries").read_text())
    except (OSError, ValueError):
        return {}
    for e in entries.get("data", {}).get("entries", []):
        if e.get("domain") == DOMAIN:
            return e.get("data", {})
    return {}


def location() -> tuple[float | None, float | None]:
    """(lat, lon) from env, else the config entry, else HA's core config."""
    if os.environ.get("HA_LAT") and os.environ.get("HA_LON"):
        return float(os.environ["HA_LAT"]), float(os.environ["HA_LON"])
    d = entry_data()
    if "latitude" in d and "longitude" in d:
        return float(d["latitude"]), float(d["longitude"])
    try:
        core = json.loads((config_dir() / ".storage" / "core.config").read_text())["data"]
        return float(core["latitude"]), float(core["longitude"])
    except (OSError, ValueError, KeyError):
        return None, None


def indoor_entity() -> str | None:
    return entry_data().get("indoor_temp_entity")


def outdoor_entity() -> str | None:
    ents = entry_data().get("outdoor_temp_entities") or []
    return ents[0] if ents else None


def supply_entity() -> str | None:
    return entry_data().get("supply_temp_entity")


def rooms() -> list[tuple[str, dict]]:
    """[(title, data)] of the integration's room subentries (sensor in data["room_sensor"])."""
    try:
        entries = json.loads((config_dir() / ".storage" / "core.config_entries").read_text())
    except (OSError, ValueError):
        return []
    for e in entries.get("data", {}).get("entries", []):
        if e.get("domain") == DOMAIN:
            return [
                (s["title"], s["data"]) for s in e.get("subentries", [])
                if s.get("subentry_type") == "room"
            ]
    return []
