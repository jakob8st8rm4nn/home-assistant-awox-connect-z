"""AwoX Connect.Z integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .client import AwoxConnectZClient
from .const import (
    CONF_DEFAULT_TRANSITION,
    CONF_DEVICES,
    CONF_IDLE_DISCONNECT,
    CONF_MAC,
    CONF_MAX_CONCURRENT_COMMANDS,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    CONF_NAME,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_MAX_CONCURRENT_COMMANDS,
    DEFAULT_NAME,
    DEFAULT_TRANSITION,
    MAX_CONCURRENT_COMMANDS,
    MIN_CONCURRENT_COMMANDS,
    DOMAIN,
)
from .protocol import protocol_self_test

PLATFORMS = [Platform.LIGHT]
_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Migrate v1.0 single-lamp entries into the multi-device schema."""
    if entry.version > 2:
        _LOGGER.error(
            "Cannot migrate AwoX entry from future version %s",
            entry.version,
        )
        return False

    if entry.version == 1:
        data = dict(entry.data)

        if not data.get(CONF_DEVICES):
            mac = data.get(CONF_MAC)
            if mac:
                data[CONF_DEVICES] = [
                    {
                        "name": data.get(
                            CONF_NAME, DEFAULT_NAME
                        ),
                        "mac": mac,
                        "mesh_id": 0,
                        "model": "Connect.Z RGB/TW",
                        "manufacturer": "AwoX / EGLO",
                        "firmware": None,
                        "hardware": None,
                        "device_type": "migrated_v1_single_device",
                        "cloud_object_id": "",
                    }
                ]

        data.pop(CONF_MAC, None)
        data.pop(CONF_NAME, None)

        hass.config_entries.async_update_entry(
            entry, data=data, version=2
        )

    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Set up every lamp imported from the AwoX account."""
    protocol_self_test()

    raw_devices = list(entry.data.get(CONF_DEVICES) or [])
    if not raw_devices:
        _LOGGER.error(
            "AwoX config entry contains no device records"
        )
        return False

    max_concurrent_commands = max(
        MIN_CONCURRENT_COMMANDS,
        min(
            MAX_CONCURRENT_COMMANDS,
            int(
                entry.options.get(
                    CONF_MAX_CONCURRENT_COMMANDS,
                    DEFAULT_MAX_CONCURRENT_COMMANDS,
                )
            ),
        ),
    )
    command_semaphore = asyncio.Semaphore(max_concurrent_commands)

    clients: list[tuple[AwoxConnectZClient, dict]] = []

    for device in raw_devices:
        mac = str(device.get("mac") or "").upper()
        if not mac:
            continue

        try:
            mesh_id = int(device.get("mesh_id") or 0)
        except (TypeError, ValueError):
            mesh_id = 0

        if not 1 <= mesh_id <= 0xFFFE:
            _LOGGER.error(
                "Skipping AwoX Connect.Z %s because no valid per-device "
                "mesh destination is available; re-add the integration to "
                "refresh device metadata",
                mac,
            )
            continue

        device = dict(device)
        device["mesh_id"] = mesh_id

        client = AwoxConnectZClient(
            hass,
            mac,
            entry.data[CONF_MESH_NAME],
            entry.data[CONF_MESH_PASSWORD],
            default_transition=entry.options.get(
                CONF_DEFAULT_TRANSITION,
                DEFAULT_TRANSITION,
            ),
            idle_disconnect=entry.options.get(
                CONF_IDLE_DISCONNECT,
                DEFAULT_IDLE_DISCONNECT,
            ),
            command_semaphore=command_semaphore,
            max_concurrent_commands=max_concurrent_commands,
        )
        clients.append((client, dict(device)))

    if not clients:
        _LOGGER.error(
            "AwoX config entry contains no usable lamp records with MAC and mesh ID"
        )
        return False

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = clients
    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload an AwoX Connect.Z account entry."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unloaded:
        clients = hass.data[DOMAIN].pop(entry.entry_id)
        for client, _device in clients:
            await client.async_close()

        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)

    return unloaded
