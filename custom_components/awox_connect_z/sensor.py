"""Diagnostic sensor entities for AwoX Connect.Z."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import AwoxConnectZClient
from .const import (
    DOMAIN,
    RSSI_MIN_PUBLISH_INTERVAL_SECONDS,
    RSSI_SIGNIFICANT_CHANGE_DBM,
)


_UNSET = object()


SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="signal_strength",
        translation_key="signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="bluetooth_status",
        translation_key="bluetooth_status",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="mesh_id",
        translation_key="mesh_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:identifier",
    ),
    SensorEntityDescription(
        key="current_bluetooth_source",
        translation_key="current_bluetooth_source",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:bluetooth-transfer",
    ),
    SensorEntityDescription(
        key="last_connection",
        translation_key="last_connection",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:bluetooth-connect",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create diagnostic sensors for every imported lamp."""
    clients: list[tuple[AwoxConnectZClient, dict[str, Any]]] = hass.data[DOMAIN][
        entry.entry_id
    ]
    async_add_entities(
        AwoxConnectZDiagnosticSensor(client, device, description)
        for client, device in clients
        for description in SENSOR_DESCRIPTIONS
    )


class AwoxConnectZDiagnosticSensor(RestoreSensor):
    """One diagnostic value attached to an AwoX Connect.Z lamp."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        client: AwoxConnectZClient,
        device: dict[str, Any],
        description: SensorEntityDescription,
    ) -> None:
        self.entity_description = description
        self._client = client
        self._device = device
        self._mesh_id = int(device["mesh_id"])
        self._restored_last_connection: str | None = None
        self._last_published_value: Any = _UNSET
        self._last_published_at = 0.0
        mac = client.mac
        self._attr_unique_id = (
            f"{mac.replace(':', '').lower()}_{description.key}"
        )

        if description.key == "bluetooth_status":
            self._attr_options = ["connected", "visible", "unreachable"]

        device_name = str(device.get("name") or f"AwoX {mac[-8:]}")
        model = str(device.get("model") or "Connect.Z RGB/TW")
        manufacturer = str(device.get("manufacturer") or "AwoX / EGLO")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name=device_name,
            manufacturer=manufacturer,
            model=model,
            sw_version=device.get("firmware"),
            hw_version=device.get("hardware"),
            connections={("bluetooth", mac)},
        )

    @property
    def available(self) -> bool:
        """Keep diagnostics visible even while the lamp itself is unreachable."""
        return not self._client.closed

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose mesh-ID comparison details without another sensor."""
        if self.entity_description.key != "mesh_id":
            return None

        advertised = self._client.advertised_mesh_id
        return {
            "configured_mesh_id": f"0x{self._mesh_id:04X}",
            "advertised_mesh_id": (
                f"0x{advertised:04X}" if advertised is not None else None
            ),
            "mesh_id_status": self._client.mesh_id_status,
        }

    def _publish_signature(self) -> Any:
        """Return state plus attributes that matter for HA publication."""
        value = self.native_value
        if self.entity_description.key != "mesh_id":
            return value
        attributes = self.extra_state_attributes or {}
        return value, tuple(sorted(attributes.items()))

    @property
    def native_value(self):
        """Return the current diagnostic value."""
        key = self.entity_description.key
        if key == "signal_strength":
            return self._client.signal_strength
        if key == "last_seen":
            return self._client.last_seen
        if key == "bluetooth_status":
            return self._client.bluetooth_status
        if key == "mesh_id":
            return f"0x{self._mesh_id:04X}"
        if key == "current_bluetooth_source":
            return self._client.current_bluetooth_source
        if key == "last_connection":
            if self._client.has_live_last_connection_result:
                return self._client.last_connection_source
            return self._restored_last_connection
        return None

    async def async_added_to_hass(self) -> None:
        """Restore historical diagnostics and subscribe to live changes."""
        await super().async_added_to_hass()
        if (
            self.entity_description.key == "last_connection"
            and not self._client.has_live_last_connection_result
        ):
            restored = await self.async_get_last_sensor_data()
            if restored is not None and isinstance(restored.native_value, str):
                self._restored_last_connection = restored.native_value

        # Cache the value Home Assistant receives on initial add. Subsequent
        # diagnostic callbacks only write a state when this entity's own value
        # has actually changed.
        self._last_published_value = self._publish_signature()
        self._last_published_at = time.monotonic()

        self.async_on_remove(
            self._client.async_add_diagnostic_listener(
                self._async_diagnostics_changed
            )
        )

    @callback
    def _async_diagnostics_changed(self) -> None:
        """Publish only meaningful changes for this diagnostic entity."""
        value = self.native_value
        signature = self._publish_signature()
        previous = self._last_published_value
        if previous is not _UNSET and signature == previous:
            return

        now = time.monotonic()
        key = self.entity_description.key

        # RSSI naturally jitters with nearly every advertisement. Keep it useful
        # for diagnostics without creating a recorder entry every second: large
        # jumps are immediate, small changes are sampled at most every 5 seconds.
        if (
            key == "signal_strength"
            and previous is not _UNSET
            and isinstance(previous, (int, float))
            and isinstance(value, (int, float))
            and now - self._last_published_at < RSSI_MIN_PUBLISH_INTERVAL_SECONDS
            and abs(value - previous) < RSSI_SIGNIFICANT_CHANGE_DBM
        ):
            return

        self._last_published_value = signature
        self._last_published_at = now
        self.async_write_ha_state()
