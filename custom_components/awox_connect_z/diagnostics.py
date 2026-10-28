"""Diagnostics for AwoX Connect.Z."""

from __future__ import annotations

from datetime import datetime
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from .client import AwoxConnectZClient

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


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def build_client_diagnostics(client: AwoxConnectZClient) -> dict:
    """Serialize existing client data without owning or recording runtime state."""
    client._refresh_last_seen_from_ha()
    evidence = client._availability
    connected = client.connected
    # Keep exported availability and status on the same side of an expiry.
    status = (
        evidence.snapshot_status
        if evidence.snapshot_active
        else evidence.status(
            time.monotonic(), client.availability_timeout, connected=connected
        )
    )
    source_id = evidence.source
    source_name = client.bluetooth_source_name(source_id)
    last_seen = evidence.last_seen
    return {
        "status_1911": client.status_1911.diagnostics(),
        "command_confirmation": dict(client.confirmation.diagnostics),
        "initial_status_polls": client._initial_status_polls,
        "status_notify_method": client._status_notify_method,
        "mac": client.mac,
        "connected": connected,
        "available": client._is_available_status(status),
        "bluetooth_status": status,
        "signal_strength": evidence.rssi,
        "last_seen": _isoformat(last_seen),
        "last_bluetooth_source": source_name,
        "last_bluetooth_source_id": source_id,
        "last_connection": client.last_connection_source,
        "last_connection_source_id": client.last_connection_source_id,
        "last_connection_time": _isoformat(client.last_connection_time),
        "closed": client.closed,
        "availability_timeout": client.availability_timeout,
        "default_transition": client.default_transition,
        "idle_disconnect": client.idle_disconnect,
        "max_concurrent_commands": client.max_concurrent_commands,
        "command": {
            "last_successful": client.last_command,
            "time": _isoformat(client.last_command_time),
            "client_operation_duration_ms": (
                client.last_command_client_operation_duration_ms
            ),
            "attempts": client.last_command_attempts,
            "retries": client.last_command_retries,
        },
        "error": {
            "last_message": client.last_error_message,
            "time": _isoformat(client.last_error_time),
            "category": client.last_error_category,
            "stage": client.last_error_stage,
            "command": client.last_error_command,
            "write_attempt": client.last_error_write_attempt,
            "current_message": client.last_error,
        },
        "mesh_id": {
            "configured": (
                f"0x{client.configured_mesh_id:04X}"
                if client.configured_mesh_id is not None
                else None
            ),
            "advertised": (
                f"0x{client.advertised_mesh_id:04X}"
                if client.advertised_mesh_id is not None
                else None
            ),
            "advertised_time": _isoformat(client.advertised_mesh_id_time),
            "status": client.mesh_id_status,
        },
    }
