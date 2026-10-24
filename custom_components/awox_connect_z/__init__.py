"""AwoX Connect.Z integration."""

from __future__ import annotations

import asyncio
import logging

from bluetooth_data_tools import parse_advertisement_data_bytes

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .advertisement import parse_awox_advertisement
from .client import AwoxConnectZClient
from .const import (
    AWOX_COMPANY_ID,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEFAULT_TRANSITION,
    CONF_DEVICES,
    CONF_IDLE_DISCONNECT,
    CONF_MAX_CONCURRENT_COMMANDS,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    CONF_SETUP_METHOD,
    SETUP_METHOD_CLOUD,
    SETUP_METHOD_LOCAL,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_MAX_CONCURRENT_COMMANDS,
    DEFAULT_TRANSITION,
    DATA_RUNTIME_CONNECT_LOCK,
    DATA_AVAILABILITY_EVIDENCE,
    MAX_AVAILABILITY_TIMEOUT,
    MAX_CONCURRENT_COMMANDS,
    MIN_AVAILABILITY_TIMEOUT,
    MIN_CONCURRENT_COMMANDS,
    DOMAIN,
)
from .protocol import is_valid_device_mesh_id

PLATFORMS = [Platform.LIGHT, Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Set up every lamp imported from the AwoX account."""
    if entry.version != 2 or entry.data.get(CONF_SETUP_METHOD) not in {
        SETUP_METHOD_CLOUD, SETUP_METHOD_LOCAL,
    }:
        _LOGGER.error("Unsupported AwoX configuration; set up the integration again")
        return False
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

    # Per-packet delivery requires the Bluetooth API introduced in Core 2026.10.
    if not hasattr(bluetooth, "async_register_advertisement_callback"):
        raise ConfigEntryNotReady(
            "AwoX Connect.Z requires the per-packet Bluetooth API "
            "available in Home Assistant Core 2026.10"
        )

    # ESPHome Bluetooth Proxy can accept multiple client requests while only
    # promoting one new BLE connection at a time. Serialize only the actual
    # runtime connection-establishment phase across all AwoX config entries so
    # a queued client does not burn its own connect timeout inside the proxy.
    domain_data = hass.data.setdefault(DOMAIN, {})
    runtime_connect_lock = domain_data.get(DATA_RUNTIME_CONNECT_LOCK)
    if runtime_connect_lock is None:
        runtime_connect_lock = asyncio.Lock()
        domain_data[DATA_RUNTIME_CONNECT_LOCK] = runtime_connect_lock

    availability_timeout = max(
        MIN_AVAILABILITY_TIMEOUT,
        min(
            MAX_AVAILABILITY_TIMEOUT,
            float(
                entry.options.get(
                    CONF_AVAILABILITY_TIMEOUT,
                    DEFAULT_AVAILABILITY_TIMEOUT,
                )
            ),
        ),
    )

    clients: list[tuple[AwoxConnectZClient, dict]] = []

    cleanups = []

    def register_cleanup(callback):
        active = True
        def once():
            nonlocal active
            if active:
                active = False
                callback()
        cleanups.append(once)
        entry.async_on_unload(once)

    try:
        for device in raw_devices:
            mac = str(device.get("mac") or "").upper()
            if not mac:
                continue

            try:
                mesh_id = int(device.get("mesh_id") or 0)
            except (TypeError, ValueError):
                mesh_id = 0

            if not is_valid_device_mesh_id(mesh_id):
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
                runtime_connect_lock=runtime_connect_lock,
                availability_timeout=availability_timeout,
                configured_mesh_id=mesh_id,
            )
            evidence = hass.data.get(DATA_AVAILABILITY_EVIDENCE, {}).get(entry.entry_id, {}).get(mac)
            if evidence is not None:
                client.restore_availability_evidence(evidence)
            clients.append((client, dict(device)))

            packet_tracking = {"active": True, "warned_no_raw": False, "state_time": -1.0}

            @callback
            def _async_handle_advertisement(
                service_info: bluetooth.BluetoothServiceInfoBleak,
                *,
                _client: AwoxConnectZClient = client,
                _expected_mesh_id: int = mesh_id,
                _tracking: dict = packet_tracking,
            ) -> None:
                if not _tracking["active"]:
                    return
                # The callback represents a received packet, not a history replay.
                _client.async_note_bluetooth_liveness(
                    service_info.time,
                    source=str(service_info.source) if service_info.source is not None else None,
                    rssi=int(service_info.rssi) if service_info.rssi is not None else None,
                )
                packet = getattr(service_info, "raw", None)
                if packet is None:
                    # Merged manufacturer data can retain an old scan response.
                    # Do not turn it into a fresh hardware-state confirmation.
                    if not _tracking["warned_no_raw"]:
                        _tracking["warned_no_raw"] = True
                        _LOGGER.warning(
                            "AwoX %s source=%s supplies no raw packet; "
                            "liveness is updated but state decoding is skipped. "
                            "Live state updates require a Bluetooth source that supplies raw advertisements",
                            _client.mac, service_info.source,
                        )
                    return
                try:
                    manufacturer_data = parse_advertisement_data_bytes(bytes(packet))[3]
                except (ValueError, IndexError, TypeError):
                    _LOGGER.debug("Ignoring invalid raw advertisement for AwoX %s", _client.mac)
                    return
                raw = manufacturer_data.get(AWOX_COMPANY_ID)
                state = parse_awox_advertisement(bytes(raw)) if raw is not None else None
                if state is None:
                    return
                # Multiple proxies may deliver packets out of order. Equal timestamps
                # remain eligible because short packets and scan responses can coincide.
                if service_info.time < _tracking["state_time"]:
                    return
                _tracking["state_time"] = service_info.time
                _client.async_note_advertised_mesh_id(
                    state.mesh_id, seen_time=service_info.time
                )
                if state.mesh_id != _expected_mesh_id:
                    return
                _client.async_set_advertisement_state(state)

            @callback
            def _async_receive_packet(
                service_info: bluetooth.BluetoothServiceInfoBleak,
                *,
                _handler=_async_handle_advertisement,
            ) -> None:
                # Packet callbacks run before HA updates its manager history. Defer
                # entity/diagnostic reads until the current dispatch has completed.
                hass.loop.call_soon(_handler, service_info)

            @callback
            def _async_active_scan_request(
                service_info: bluetooth.BluetoothServiceInfoBleak,
                change: bluetooth.BluetoothChange,
            ) -> None:
                # Keep the existing active-scan subscription, but decode state only
                # through the per-packet callback above (no duplicate state path).
                return

            @callback
            def _async_stop_packet_delivery(*, _tracking=packet_tracking) -> None:
                # Also invalidate any call_soon callback already queued at unload.
                _tracking["active"] = False

            register_cleanup(_async_stop_packet_delivery)
            register_cleanup(bluetooth.async_register_advertisement_callback(
                hass, _async_receive_packet, mac
            ))

            register_kwargs = {}
            replay_type = getattr(bluetooth, "BluetoothCallbackReplay", None)
            if replay_type is not None:
                # RestoreEntity already handles startup state. Avoid treating an old
                # cached advertisement as a fresh hardware confirmation.
                register_kwargs["replay"] = replay_type.DISABLED

            unregister_advertisement = bluetooth.async_register_callback(
                hass,
                _async_active_scan_request,
                {
                    "address": mac,
                    "manufacturer_id": AWOX_COMPANY_ID,
                    "connectable": False,
                },
                # The long Connect.Z status payload is carried in scan-response
                # manufacturer data on the tested lamps, so request active scan.
                bluetooth.BluetoothScanningMode.ACTIVE,
                **register_kwargs,
            )
            register_cleanup(unregister_advertisement)

            # Per-packet delivery includes unchanged advertisements; retain history
            # so device lookup, source and RSSI are not erased during setup/reload.

            client.async_start_availability_tracking()

        if not clients:
            _LOGGER.error(
                "AwoX config entry contains no usable lamp records with MAC and mesh ID"
            )
            if not any(
                key != DATA_RUNTIME_CONNECT_LOCK for key in domain_data
            ):
                hass.data.pop(DOMAIN)
            return False

        domain_data[entry.entry_id] = clients
        await hass.config_entries.async_forward_entry_setups(
            entry, PLATFORMS
        )
        return True

    except BaseException:
        for cleanup in reversed(cleanups):
            try:
                cleanup()
            except Exception:
                _LOGGER.exception("AwoX setup callback cleanup failed")
        await asyncio.gather(*(client.async_close() for client, _ in clients),
                             return_exceptions=True)
        domain_data.pop(entry.entry_id, None)
        if not any(key != DATA_RUNTIME_CONNECT_LOCK for key in domain_data):
            hass.data.pop(DOMAIN, None)
        raise


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload an AwoX Connect.Z account entry."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unloaded:
        domain_data = hass.data[DOMAIN]
        clients = domain_data.pop(entry.entry_id)
        # Capture before closing: active connections provide a real final proof.
        # Keep this outside domain_data, whose runtime locks are discarded below.
        hass.data.setdefault(DATA_AVAILABILITY_EVIDENCE, {})[entry.entry_id] = {
            client.mac: client.export_availability_evidence()
            for client, _device in clients
        }
        for client, _device in clients:
            await client.async_close()

        # Keep the shared runtime-connect lock only while at least one AwoX
        # config entry is loaded.
        if not any(
            key != DATA_RUNTIME_CONNECT_LOCK for key in domain_data
        ):
            hass.data.pop(DOMAIN)

    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Discard process-local history when an entry is permanently removed."""
    histories = hass.data.get(DATA_AVAILABILITY_EVIDENCE)
    if histories is not None:
        histories.pop(entry.entry_id, None)
        if not histories:
            hass.data.pop(DATA_AVAILABILITY_EVIDENCE, None)
