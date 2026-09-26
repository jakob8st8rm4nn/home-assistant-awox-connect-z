"""Light entities for AwoX Connect.Z."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .client import AwoxConnectZClient
from .const import DOMAIN, MAX_COLOR_TEMP_KELVIN, MIN_COLOR_TEMP_KELVIN
from .protocol import (
    ha_brightness_to_device,
    make_brightness,
    make_color_temp_kelvin,
    make_hs_color,
    make_power,
)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one light entity per imported account device."""
    clients: list[tuple[AwoxConnectZClient, dict]] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AwoxConnectZLight(client, device) for client, device in clients])


class AwoxConnectZLight(LightEntity, RestoreEntity):
    """Optimistic AwoX Connect.Z RGB/TW light."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.TRANSITION
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
    _attr_should_poll = False

    def __init__(self, client: AwoxConnectZClient, device: dict[str, Any]) -> None:
        self._client = client
        self._device = device
        self._mac = client.mac
        self._device_name = str(device.get("name") or f"AwoX {self._mac[-8:]}")
        self._attr_unique_id = self._mac.replace(":", "").lower()
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_color_temp_kelvin = 4000
        self._attr_hs_color = (0.0, 100.0)
        model = str(device.get("model") or "Connect.Z RGB/TW")
        manufacturer = str(device.get("manufacturer") or "AwoX / EGLO")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=self._device_name,
            manufacturer=manufacturer,
            model=model,
            sw_version=device.get("firmware"),
            hw_version=device.get("hardware"),
            connections={("bluetooth", self._mac)},
        )

    @property
    def available(self) -> bool:
        """Keep entity loaded across temporary BLE visibility gaps."""
        return not self._client.closed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Useful diagnostics without exposing credentials."""
        return {
            "ble_connected": self._client.connected,
            "last_ble_error": self._client.last_error,
            "last_command": self._client.last_command,
            "cloud_mesh_id": self._device.get("mesh_id"),
            "cloud_device_type": self._device.get("device_type"),
        }

    async def async_added_to_hass(self) -> None:
        """Restore optimistic state after a Home Assistant restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        self._attr_is_on = last.state == STATE_ON
        brightness = last.attributes.get(ATTR_BRIGHTNESS)
        if brightness is not None:
            self._attr_brightness = int(brightness)
        hs_color = last.attributes.get(ATTR_HS_COLOR)
        if hs_color is not None:
            self._attr_hs_color = (float(hs_color[0]), float(hs_color[1]))
        color_temp_kelvin = last.attributes.get(ATTR_COLOR_TEMP_KELVIN)
        if color_temp_kelvin is not None:
            self._attr_color_temp_kelvin = int(color_temp_kelvin)
        mode = last.attributes.get(ATTR_COLOR_MODE)
        if mode in (ColorMode.HS, ColorMode.HS.value):
            self._attr_color_mode = ColorMode.HS
        elif mode in (ColorMode.COLOR_TEMP, ColorMode.COLOR_TEMP.value):
            self._attr_color_mode = ColorMode.COLOR_TEMP

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on or alter the lamp."""
        transition = float(kwargs.get(ATTR_TRANSITION, self._client.default_transition))
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        hs_color = kwargs.get(ATTR_HS_COLOR)
        color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        sent_something = False
        if brightness is not None:
            device_level = ha_brightness_to_device(int(brightness))
            await self._client.async_send_plain(
                make_brightness(device_level, transition), label=f"brightness:{brightness}"
            )
            self._attr_brightness = int(brightness)
            self._attr_is_on = True
            sent_something = True
        elif not self._attr_is_on:
            await self._client.async_send_plain(make_power(True), label="power:on")
            self._attr_is_on = True
            sent_something = True
        if hs_color is not None:
            hue, saturation = hs_color
            await self._client.async_send_plain(
                make_hs_color(hue, saturation, transition),
                label=f"hs:{hue:.1f},{saturation:.1f}",
            )
            self._attr_hs_color = (float(hue), float(saturation))
            self._attr_color_mode = ColorMode.HS
            self._attr_is_on = True
            sent_something = True
        elif color_temp_kelvin is not None:
            kelvin = max(MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, int(color_temp_kelvin)))
            await self._client.async_send_plain(
                make_color_temp_kelvin(kelvin, transition), label=f"color_temp:{kelvin}K"
            )
            self._attr_color_temp_kelvin = kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_is_on = True
            sent_something = True
        if not sent_something and not self._attr_is_on:
            await self._client.async_send_plain(make_power(True), label="power:on")
            self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the lamp."""
        await self._client.async_send_plain(make_power(False), label="power:off")
        self._attr_is_on = False
        self.async_write_ha_state()
