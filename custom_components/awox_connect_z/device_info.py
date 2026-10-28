"""Shared device registry metadata for lamp entities."""

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def build_device_info(mac: str, device: dict[str, Any]) -> DeviceInfo:
    """Preserve imported metadata and existing defaults for every entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, mac)},
        name=str(device.get("name") or f"AwoX {mac[-8:]}"),
        manufacturer=str(device.get("manufacturer") or "AwoX / EGLO"),
        model=str(device.get("model") or "Connect.Z RGB/TW"),
        sw_version=device.get("firmware"),
        hw_version=device.get("hardware"),
        connections={("bluetooth", mac)},
    )
