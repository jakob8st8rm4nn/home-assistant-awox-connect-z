"""Diagnostics for AwoX Connect.Z."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICES,
    CONF_OWNER_ID,
    CONF_SETUP_METHOD,
    DOMAIN,
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return account/device diagnostics without credentials."""
    clients = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": {
            "setup_method": entry.data[CONF_SETUP_METHOD],
            "owner_id_present": bool(entry.data.get(CONF_OWNER_ID)),
            "device_count": len(entry.data.get(CONF_DEVICES) or []),
            "options": dict(entry.options),
        },
        "devices": [
            {
                "metadata": {
                    key: value
                    for key, value in device.items()
                    if key not in {"cloud_object_id"}
                },
                "client": client.diagnostics(),
            }
            for client, device in clients
        ],
    }
