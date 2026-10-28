"""Resilient BLE client for AwoX Connect.Z."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from contextlib import suppress, asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from bleak_retry_connector import (
    BleakClientWithServiceCache,
    device_source,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .diagnostics import build_client_diagnostics
from .availability import AvailabilityState, AvailabilityEvidence
from .advertisement import AwoxAdvertisementState
from .status_1911 import Status1911Store
from .command.confirmation import CommandConfirmation, ConfirmationSuperseded
from .const import (
    AUTH_RESPONSE_SETTLE_SECONDS,
    AVAILABILITY_MIN_WAIT_SECONDS,
    AVAILABILITY_RECOVERY_POLL_SECONDS,
    COMMAND_CHAR_UUID,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DISCONNECT_TIMEOUT_SECONDS,
    DISCOVERY_ATTEMPTS,
    DISCOVERY_RETRY_BASE_DELAY_SECONDS,
    DISCOVERY_RETRY_MAX_DELAY_SECONDS,
    MIN_CONCURRENT_COMMANDS,
    MIN_IDLE_DISCONNECT,
    MIN_TRANSITION,
    PAIR_CHAR_UUID,
    RUNTIME_AUTH_TIMEOUT_SECONDS,
    RUNTIME_COMMAND_TIMEOUT_SECONDS,
    RUNTIME_CONNECT_QUEUE_LOG_THRESHOLD_SECONDS,
    RUNTIME_CONNECT_QUEUE_TIMEOUT_SECONDS,
    RUNTIME_CONNECT_TIMEOUT_SECONDS,
    RUNTIME_WRITE_TIMEOUT_SECONDS,
    SETUP_CONNECT_ATTEMPTS,
    STATUS_CHAR_UUID,
    CONFIRMATION_MAX_POLLS,
    CONFIRMATION_POLL_WINDOW_MS,
)
from .protocol import encrypt_command, make_pair_packet, make_session_key, make_status_query

_LOGGER = logging.getLogger(__name__)


class AwoxConnectZError(HomeAssistantError):
    """Base AwoX Connect.Z error."""


class AwoxDeviceNotFound(AwoxConnectZError):
    """Lamp was not visible through any HA Bluetooth adapter/proxy."""


class AwoxAuthenticationError(AwoxConnectZError):
    """Lamp rejected the local mesh credential."""


class RuntimeCommandStatus(str, Enum):
    """Outcome of one runtime command operation."""

    NOT_SELECTED = "not_selected"
    SUCCESS = "success"
    PREPARATION_FAILED = "preparation_failed"
    WRITE_FAILED = "write_failed"
    WRITE_FAILED_FINAL = "write_failed_final"
    WRITE_UNCERTAIN = "write_uncertain"


@dataclass(frozen=True, slots=True)
class RuntimeSelectedCommand:
    """One late-selected encrypted-command source plus manager attempt number."""

    frame: bytes
    label: str
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class RuntimeCommandResult:
    """Structured result of one late-selected physical write attempt."""

    status: RuntimeCommandStatus
    label: str | None = None
    error: AwoxConnectZError | None = None
    write_finished_monotonic: float | None = None
    attempt: int | None = None



class AwoxConnectZClient:
    """Persistent-on-demand BLE session, authentication and GATT transport."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        mesh_name: str,
        mesh_password: str,
        *,
        default_transition: float,
        idle_disconnect: float,
        command_semaphore: asyncio.Semaphore,
        max_concurrent_commands: int,
        runtime_connect_lock: asyncio.Lock | None = None,
        availability_timeout: float = DEFAULT_AVAILABILITY_TIMEOUT,
        configured_mesh_id: int | None = None,
    ) -> None:
        self.hass = hass
        self.mac = mac
        self.mesh_name = mesh_name
        self.mesh_password = mesh_password
        self.default_transition = max(MIN_TRANSITION, float(default_transition))
        self.idle_disconnect = max(MIN_IDLE_DISCONNECT, float(idle_disconnect))
        self.availability_timeout = max(0.0, float(availability_timeout))
        self.max_concurrent_commands = max(
            MIN_CONCURRENT_COMMANDS, int(max_concurrent_commands)
        )
        self._command_semaphore = command_semaphore
        # Runtime clients created by async_setup_entry share one lock across all
        # AwoX config entries. Config-flow verifier clients may omit it because
        # they keep the separate robust setup/reconfigure connection path.
        self._runtime_connect_lock = runtime_connect_lock or asyncio.Lock()

        self._client: Any | None = None
        self._session_key: bytes | None = None
        self._connect_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._closed = False

        # ``last_error`` remains the current operational error and is cleared
        # after a successful operation. The diagnostic error fields below keep
        # the most recent historical failure so a recovered retry is still
        # visible in downloaded diagnostics.
        self.last_error: str | None = None
        self.last_command: str | None = None
        self.last_command_time: datetime | None = None
        self.last_command_client_operation_duration_ms: int | None = None
        self.last_command_attempts: int | None = None
        self.last_command_retries: int | None = None
        self.last_error_message: str | None = None
        self.last_error_time: datetime | None = None
        self.last_error_category: str | None = None
        self.last_error_stage: str | None = None
        self.last_error_command: str | None = None
        self.last_error_write_attempt: int | None = None
        self.last_connection_time: datetime | None = None

        self.configured_mesh_id = (
            int(configured_mesh_id) if configured_mesh_id is not None else None
        )
        self.status_1911 = Status1911Store(self.mac, self.configured_mesh_id)
        self.confirmation = CommandConfirmation(self)
        self._session_generation = 0
        self._status_ready_generation: int | None = None
        self._status_notify_method: str | None = None
        self._status_pins = 0
        self._initial_status_polls = 0
        self.advertised_mesh_id: int | None = None
        self.advertised_mesh_id_time: datetime | None = None
        self.mesh_id_status = "unknown"

        self._advertisement_state: AwoxAdvertisementState | None = None
        self._advertisement_listeners: set[
            Callable[[AwoxAdvertisementState], None]
        ] = set()

        self._available = False
        self._availability = AvailabilityState()
        self._availability_task: asyncio.Task[None] | None = None
        self._availability_wakeup = asyncio.Event()
        self._availability_listeners: set[Callable[[bool], None]] = set()

        self._diagnostic_listeners: set[Callable[[], None]] = set()
        self._last_connection_source: str | None = None
        self._last_connection_observed = False

    @callback
    def _record_error(
        self,
        error: Exception | str,
        *,
        category: str,
        stage: str,
        command_label: str | None = None,
        write_attempt: int | None = None,
    ) -> None:
        """Record current and historical diagnostic error information."""
        message = str(error)
        self.last_error = message
        self.last_error_message = message
        self.last_error_time = dt_util.utcnow()
        self.last_error_category = category
        self.last_error_stage = stage
        self.last_error_command = command_label
        self.last_error_write_attempt = write_attempt
        self._async_notify_diagnostic_listeners()

    @callback
    def _clear_current_error(self) -> None:
        """Clear the current error without erasing historical diagnostics."""
        self.last_error = None

    @staticmethod
    def _error_category(error: Exception, default: str) -> str:
        """Return a compact diagnostic category for one exception."""
        if isinstance(error, AwoxDeviceNotFound):
            return "device_not_found"
        if isinstance(error, AwoxAuthenticationError):
            return "authentication"
        if isinstance(error, TimeoutError) or "timed out" in str(error).lower():
            return "timeout"
        return default

    @callback
    def _record_command_success(
        self,
        label: str,
        *,
        started_monotonic: float,
        attempts: int,
    ) -> None:
        """Store diagnostics for the last successfully written command."""
        self.last_command = label
        self.last_command_time = dt_util.utcnow()
        self.last_command_client_operation_duration_ms = max(
            0, round((time.monotonic() - started_monotonic) * 1000)
        )
        self.last_command_attempts = attempts
        self.last_command_retries = max(0, attempts - 1)
        self._clear_current_error()
        self._async_notify_diagnostic_listeners()

    @callback
    def async_note_advertised_mesh_id(
        self, mesh_id: int, *, seen_time: float | None = None
    ) -> None:
        """Compare one decoded advertisement mesh ID with configuration."""
        mesh_id = int(mesh_id)
        previous_id = self.advertised_mesh_id
        previous_status = self.mesh_id_status
        self.advertised_mesh_id = mesh_id

        # Timestamp the complete status packet that actually carried the mesh ID.
        # General Bluetooth Last Seen can also be refreshed by other advertisement
        # forms and is therefore intentionally separate. HA's service-info time is
        # monotonic, so convert it once to a stable UTC wall-clock value.
        if seen_time is None:
            self.advertised_mesh_id_time = dt_util.utcnow()
        else:
            age = max(0.0, time.monotonic() - float(seen_time))
            self.advertised_mesh_id_time = dt_util.utcnow() - timedelta(seconds=age)

        if self.configured_mesh_id is None:
            status = "unknown"
        elif mesh_id == self.configured_mesh_id:
            status = "match"
        else:
            status = "mismatch"

        self.mesh_id_status = status
        if status == "mismatch" and previous_status != "mismatch":
            _LOGGER.warning(
                "AwoX Connect.Z mesh ID mismatch for %s: configured 0x%04X, "
                "advertised 0x%04X; advertisement state is ignored",
                self.mac,
                self.configured_mesh_id,
                mesh_id,
            )
        elif status == "match" and previous_status == "mismatch":
            _LOGGER.info(
                "AwoX Connect.Z mesh ID for %s matches configuration again "
                "(0x%04X)",
                self.mac,
                mesh_id,
            )

        if previous_id != mesh_id or previous_status != status:
            self._async_notify_diagnostic_listeners()

    @property
    def connected(self) -> bool:
        """Return current BLE connection state."""
        return bool(self._client is not None and self._client.is_connected)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def advertisement_state(self) -> AwoxAdvertisementState | None:
        """Return the latest decoded long advertisement state, if any."""
        return self._advertisement_state

    @property
    def available(self) -> bool:
        """Return whether the lamp has recent Bluetooth liveness."""
        return False if self._closed else self._is_available_status(self.bluetooth_status)

    def _is_available_status(self, status: str | None) -> bool:
        return not self._closed and AvailabilityState.is_available(status)

    def _latest_service_info(self) -> bluetooth.BluetoothServiceInfoBleak | None:
        """Return Home Assistant's current best advertisement for this lamp."""
        return bluetooth.async_last_service_info(
            self.hass, self.mac, connectable=False
        )

    def bluetooth_source_name(self, source: str | None) -> str | None:
        """Resolve a Bluetooth source MAC/ID to the scanner's friendly name."""
        if not source:
            return None

        scanner = bluetooth.async_scanner_by_source(self.hass, source)
        if scanner is not None:
            name = getattr(scanner, "name", None)
            if name:
                return str(name)
        return source

    @property
    def signal_strength(self) -> int | None:
        """Return RSSI from the last known Bluetooth advertisement."""
        self._refresh_last_seen_from_ha()
        return self._availability.rssi

    @callback
    def _async_update_last_seen(
        self, seen_time: float, *, source: str | None = None, rssi: int | None = None
    ) -> bool:
        return self._availability.note_advertisement(
            seen_time, source=source, rssi=rssi,
            clock=time.monotonic, utcnow=dt_util.utcnow,
        )

    def _refresh_last_seen_from_ha(self) -> bool:
        """Refresh cached advertisement history from HA's Bluetooth manager."""
        service_info = self._latest_service_info()
        if service_info is None:
            return False

        seen_time = float(service_info.time)
        same_packet_time = self._availability.advertisement_time == seen_time
        # A live per-packet callback is the authoritative source for which proxy
        # actually delivered an equal-timestamp packet. HA's cached "best" service
        # info may choose a different proxy for that same timestamp, so use it only
        # to fill missing history or when it is genuinely newer.
        source = (
            str(service_info.source)
            if service_info.source is not None
            and (not same_packet_time or self._availability.source is None)
            else None
        )
        rssi = (
            int(service_info.rssi)
            if service_info.rssi is not None
            and (not same_packet_time or self._availability.rssi is None)
            else None
        )
        return self._async_update_last_seen(
            seen_time,
            source=source,
            rssi=rssi,
        )

    @property
    def last_seen(self) -> datetime | None:
        """Return the stable wall-clock time of HA's newest advertisement."""
        self._refresh_last_seen_from_ha()
        return self._availability.last_seen

    def _derive_bluetooth_status(self) -> str | None:
        connected = self.connected
        if not connected:
            self._refresh_last_seen_from_ha()
        return self._availability.status(
            time.monotonic(), self.availability_timeout, connected=connected
        )

    @property
    def bluetooth_status(self) -> str | None:
        """Return current reachability, or the snapshot for an active publication."""
        if self._availability.snapshot_active:
            return self._availability.snapshot_status
        return self._derive_bluetooth_status()

    def export_availability_evidence(self) -> AvailabilityEvidence:
        """Preserve history, including the existing final proof of an active link."""
        self._refresh_last_seen_from_ha()
        if self.connected:
            self._availability.connection_time = time.monotonic()
        return self._availability.export()

    def restore_availability_evidence(self, evidence: tuple) -> None:
        self._availability.restore(evidence)

    @property
    def last_bluetooth_source(self) -> str | None:
        """Return the source of the last known Bluetooth advertisement."""
        return self.bluetooth_source_name(self.last_bluetooth_source_id)

    @property
    def last_bluetooth_source_id(self) -> str | None:
        """Return the raw source ID of the last known advertisement."""
        self._refresh_last_seen_from_ha()
        return self._availability.source

    @property
    def last_connection_source(self) -> str | None:
        """Return the scanner/proxy used by the last successful GATT session."""
        return self.bluetooth_source_name(self._last_connection_source)

    @property
    def last_connection_source_id(self) -> str | None:
        """Return the raw source ID of the last successful GATT session."""
        return self._last_connection_source

    @property
    def has_live_last_connection_result(self) -> bool:
        """Return whether this runtime has completed a successful GATT session."""
        return self._last_connection_observed

    @staticmethod
    def _register_listener[Listener](
        listeners: set[Listener], listener: Listener
    ) -> Callable[[], None]:
        """Register in a listener set and return an idempotent removal callback."""
        listeners.add(listener)

        @callback
        def _remove_listener() -> None:
            listeners.discard(listener)

        return _remove_listener

    @callback
    def async_add_diagnostic_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Subscribe an entity to Bluetooth diagnostic changes."""
        return self._register_listener(self._diagnostic_listeners, listener)

    @callback
    def _async_notify_diagnostic_listeners(self) -> None:
        """Publish availability and diagnostics from one reachability snapshot."""
        status = self._derive_bluetooth_status()
        available = self._is_available_status(status)

        previous_active = self._availability.snapshot_active
        previous_status = self._availability.snapshot_status
        self._availability.snapshot_active = True
        self._availability.snapshot_status = status
        try:
            self._async_set_available(available)
            for listener in tuple(self._diagnostic_listeners):
                try:
                    listener()
                except Exception:  # diagnostics must never break a BLE operation
                    _LOGGER.exception(
                        "AwoX Connect.Z diagnostic listener failed for %s", self.mac
                    )
        finally:
            self._availability.snapshot_active = previous_active
            self._availability.snapshot_status = previous_status

    def _actual_connection_source(self, client: Any | None) -> str | None:
        """Best-effort source of the scanner that actually established the link."""
        if client is None:
            return None

        # Home Assistant's habluetooth wrapper tracks the scanner selected at
        # connect time. It may differ from the advertisement source that looked
        # preferable before the connection (for example because a proxy had no
        # free connection slot). Keep this access defensive because the wrapper
        # implementation is not part of HA's stable integration API.
        scanner = getattr(client, "_connected_scanner", None)
        source = getattr(scanner, "source", None)
        if source:
            return str(source)

        connected_device = getattr(client, "_connected_device", None)
        if connected_device is not None:
            try:
                source = device_source(connected_device)
            except Exception:
                source = None
            if source:
                return str(source)

        # Public-API fallback: one connectable path is unambiguous. If several
        # scanners can reach the lamp we deliberately do not guess.
        try:
            paths = bluetooth.async_scanner_devices_by_address(
                self.hass, self.mac, connectable=True
            )
        except Exception:
            return None
        if len(paths) == 1:
            source = getattr(paths[0].scanner, "source", None)
            if source:
                return str(source)
        return None

    @callback
    def _async_set_last_connection_source(self, source: str | None) -> None:
        """Remember source and time of a successful authenticated session."""
        self._last_connection_observed = True
        self._last_connection_source = source
        self.last_connection_time = dt_util.utcnow()
        # Notify even when the source is unchanged: the downloadable diagnostic
        # timestamp has still advanced. Visible sensor entities suppress writes
        # when their own state did not change.
        self._async_notify_diagnostic_listeners()

    @callback
    def async_add_availability_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Subscribe an entity to availability changes."""
        return self._register_listener(self._availability_listeners, listener)

    @callback
    def _async_set_available(self, available: bool) -> None:
        """Update availability and notify listeners only on a change."""
        if self._available == available:
            return

        self._available = available
        for listener in tuple(self._availability_listeners):
            try:
                listener(available)
            except Exception:
                _LOGGER.exception(
                    "AwoX Connect.Z availability listener failed for %s", self.mac
                )

    @callback
    def async_note_bluetooth_liveness(
        self,
        seen_time: float | None = None,
        *,
        source: str | None = None,
        rssi: int | None = None,
    ) -> None:
        """Record one received Bluetooth advertisement and its diagnostics."""
        if self._closed:
            return
        seen = seen_time if seen_time is not None else time.monotonic()
        self._async_update_last_seen(seen, source=source, rssi=rssi)
        self._availability_wakeup.set()
        self._async_notify_diagnostic_listeners()

    @callback
    def _async_note_connection_liveness(self) -> None:
        """Record a known-good local GATT session as a liveness signal."""
        if self._closed:
            return
        self._availability.connection_time = time.monotonic()
        self._availability_wakeup.set()
        self._async_notify_diagnostic_listeners()

    def _availability_wait_seconds(self) -> float:
        connected = self.connected
        self._refresh_last_seen_from_ha()
        return self._availability.wait_seconds(
            time.monotonic(), self.availability_timeout, connected=connected,
            poll=AVAILABILITY_RECOVERY_POLL_SECONDS,
            minimum=AVAILABILITY_MIN_WAIT_SECONDS,
        )

    @callback
    def async_start_availability_tracking(self) -> None:
        """Start availability tracking for this lamp."""
        if self._availability_task is not None or self._closed:
            return

        # Publish immediately, including known stale history on reload.
        self._async_notify_diagnostic_listeners()

        async def _worker() -> None:
            try:
                while not self._closed:
                    self._availability_wakeup.clear()
                    self._async_notify_diagnostic_listeners()

                    wait_seconds = self._availability_wait_seconds()

                    try:
                        await asyncio.wait_for(
                            self._availability_wakeup.wait(),
                            timeout=wait_seconds,
                        )
                    except TimeoutError:
                        # Recompute availability and diagnostics together on the
                        # next iteration, including HA cache-only packet updates.
                        pass
            except asyncio.CancelledError:
                return
            finally:
                if asyncio.current_task() is self._availability_task:
                    self._availability_task = None

        self._availability_task = self.hass.async_create_background_task(
            _worker(), f"AwoX Connect.Z availability {self.mac}"
        )

    @callback
    def async_add_advertisement_listener(
        self, listener: Callable[[AwoxAdvertisementState], None]
    ) -> Callable[[], None]:
        """Subscribe an entity to decoded advertisement state changes."""
        return self._register_listener(self._advertisement_listeners, listener)

    @callback
    def async_set_advertisement_state(
        self, state: AwoxAdvertisementState
    ) -> None:
        """Store one decoded advertisement and notify entity listeners."""
        self._advertisement_state = state

        for listener in tuple(self._advertisement_listeners):
            listener(state)

    @staticmethod
    def _is_cache_service_error(err: Exception) -> bool:
        """Return whether an error specifically points to stale GATT services."""
        class_name = err.__class__.__name__
        if class_name in {
            "BleakCharacteristicNotFoundError",
            "BleakServiceNotFoundError",
        }:
            return True

        message = str(err).lower()
        missing_service_or_characteristic = (
            ("characteristic" in message or "service" in message)
            and any(part in message for part in (
                "not found", "could not be found", "does not exist"
            ))
        )
        stale_handle = (
            "invalid handle" in message
            or "attribute not found" in message
        )
        return missing_service_or_characteristic or stale_handle

    def _async_lookup_device(self) -> Any:
        """Look up connectable history first, then any known Bluetooth entry."""
        device = async_ble_device_from_address(
            self.hass, self.mac, connectable=True
        )
        if device is None:
            device = async_ble_device_from_address(
                self.hass, self.mac, connectable=False
            )
        return device

    def _async_find_device_runtime(self) -> Any:
        """Return the currently known HA Bluetooth device without retry sleeps."""
        device = self._async_lookup_device()
        if device is None:
            scanner_devices = bluetooth.async_scanner_devices_by_address(
                self.hass, self.mac, connectable=True
            )
            if scanner_devices:
                device = scanner_devices[0].ble_device
                _LOGGER.debug(
                    "Using retained Bluetooth scanner device entry for AwoX %s",
                    self.mac,
                )
        if device is None:
            raise AwoxDeviceNotFound(
                f"AwoX Connect.Z {self.mac} has no current Bluetooth device entry"
            )
        return device

    async def _async_find_device(self) -> Any:
        for attempt in range(1, DISCOVERY_ATTEMPTS + 1):
            device = self._async_lookup_device()
            if device is not None:
                return device

            if attempt < DISCOVERY_ATTEMPTS:
                _LOGGER.debug(
                    "%s not currently visible; discovery retry %s/%s",
                    self.mac,
                    attempt,
                    DISCOVERY_ATTEMPTS,
                )
                await asyncio.sleep(
                    min(
                        DISCOVERY_RETRY_BASE_DELAY_SECONDS * attempt,
                        DISCOVERY_RETRY_MAX_DELAY_SECONDS,
                    )
                )

        raise AwoxDeviceNotFound(
            f"AwoX Connect.Z {self.mac} is not currently visible via Bluetooth"
        )

    async def _async_authenticate(self) -> None:
        if self._client is None:
            raise AwoxConnectZError("BLE client missing during authentication")

        mesh_name = self.mesh_name.encode("utf-8")
        mesh_password = self.mesh_password.encode("utf-8")
        session_random = os.urandom(8)

        pair_packet = make_pair_packet(mesh_name, mesh_password, session_random)
        await self._client.write_gatt_char(
            PAIR_CHAR_UUID, pair_packet, response=True
        )
        await asyncio.sleep(AUTH_RESPONSE_SETTLE_SECONDS)
        reply = bytes(await self._client.read_gatt_char(PAIR_CHAR_UUID))

        if len(reply) < 9:
            raise AwoxAuthenticationError(
                f"Short AwoX login response ({len(reply)} bytes)"
            )
        if reply[0] == 0x0E:
            raise AwoxAuthenticationError("AwoX mesh credential rejected (0x0E)")
        if reply[0] != 0x0D:
            raise AwoxAuthenticationError(
                f"Unexpected AwoX login response 0x{reply[0]:02X}"
            )

        self._session_key = make_session_key(
            mesh_name, mesh_password, session_random, reply[1:9]
        )
        self._session_generation += 1
        self._status_ready_generation = None
        self.status_1911.end()

    async def _async_acquire_runtime_connect_lock(
        self,
        command_timeout: asyncio.Timeout | None,
    ) -> None:
        """Acquire the shared runtime-connect slot outside command budget."""
        loop = asyncio.get_running_loop()
        wait_started = loop.time()

        # The 30-second command budget is intended for actual runtime work.
        # Suspend it while this command merely waits behind another AwoX BLE
        # connection attempt; the queue has its own bounded timeout instead.
        previous_deadline: float | None = None
        if command_timeout is not None and not command_timeout.expired():
            previous_deadline = command_timeout.when()
            if previous_deadline is not None:
                command_timeout.reschedule(None)

        try:
            try:
                async with asyncio.timeout(RUNTIME_CONNECT_QUEUE_TIMEOUT_SECONDS):
                    await self._runtime_connect_lock.acquire()
            except TimeoutError as err:
                raise AwoxConnectZError(
                    "Timed out after "
                    f"{RUNTIME_CONNECT_QUEUE_TIMEOUT_SECONDS:.1f}s waiting for the "
                    "AwoX runtime BLE connection queue"
                ) from err
        finally:
            if (
                previous_deadline is not None
                and command_timeout is not None
                and not command_timeout.expired()
            ):
                waited = loop.time() - wait_started
                command_timeout.reschedule(previous_deadline + waited)

        waited = loop.time() - wait_started
        if waited >= RUNTIME_CONNECT_QUEUE_LOG_THRESHOLD_SECONDS:
            _LOGGER.debug(
                "AwoX %s waited %.3fs for shared runtime BLE connect slot",
                self.mac,
                waited,
            )

    async def _async_ensure_connected(
        self,
        *,
        fast_runtime: bool = False,
        runtime_command_timeout: asyncio.Timeout | None = None,
    ) -> None:
        if self._closed:
            raise AwoxConnectZError("AwoX client is closed")
        if self.connected and self._session_key is not None:
            return

        async with self._connect_lock:
            if self.connected and self._session_key is not None:
                return

            await self._async_disconnect(cancel_idle=False)

            if not fast_runtime:
                # Setup/reconfigure credential verification keeps the existing
                # robust connector behavior. The config flow has its own
                # 20-second overall verification timeout.
                device = await self._async_find_device()
                try:
                    client = await establish_connection(
                        BleakClientWithServiceCache,
                        device,
                        device.name or f"AwoX Connect.Z {self.mac}",
                        max_attempts=SETUP_CONNECT_ATTEMPTS,
                    )
                    self._client = client
                    await self._async_authenticate()
                    self._clear_current_error()
                    self._async_set_last_connection_source(
                        self._actual_connection_source(client)
                    )
                    self._async_note_connection_liveness()
                    _LOGGER.debug(
                        "Authenticated AwoX Connect.Z %s", self.mac
                    )
                    return
                except Exception:
                    await self._async_disconnect(cancel_idle=False)
                    raise

            # Runtime light commands deliberately bypass the retry loop in
            # bleak-retry-connector. Home Assistant still chooses the BLEDevice
            # (and therefore the adapter/proxy), while this path performs one
            # direct BleakClient.connect() call per requested runtime session.
            #
            # Use the service cache by default for fast cold starts. If the BLE
            # link succeeds but authentication fails for a non-credential
            # reason, retry once without cache to recover from stale services.
            device = self._async_find_device_runtime()
            async def _connect_once(*, use_cache: bool) -> None:
                await self._async_acquire_runtime_connect_lock(
                    runtime_command_timeout
                )
                try:
                    client = BleakClientWithServiceCache(
                        device,
                        _is_retry_client=True,
                    )
                    self._client = client

                    try:
                        async with asyncio.timeout(RUNTIME_CONNECT_TIMEOUT_SECONDS):
                            await client.connect(
                                timeout=RUNTIME_CONNECT_TIMEOUT_SECONDS,
                                dangerous_use_bleak_cache=use_cache,
                            )
                    except asyncio.CancelledError:
                        # Keep the shared connection-establishment slot until
                        # bounded proxy cleanup has completed.
                        await self._async_disconnect(cancel_idle=False)
                        raise
                    except Exception as err:
                        await self._async_disconnect(cancel_idle=False)
                        raise AwoxConnectZError(
                            f"Runtime BLE connect timed out after {RUNTIME_CONNECT_TIMEOUT_SECONDS:.1f}s"
                            if isinstance(err, TimeoutError) else f"Runtime BLE connect failed: {err}"
                        ) from err
                finally:
                    self._runtime_connect_lock.release()

            async def _authenticate_once() -> None:
                try:
                    async with asyncio.timeout(RUNTIME_AUTH_TIMEOUT_SECONDS):
                        await self._async_authenticate()
                except asyncio.CancelledError:
                    await self._async_disconnect(cancel_idle=False)
                    raise
                except TimeoutError as err:
                    raise AwoxConnectZError(
                        f"Runtime AwoX authentication timed out after "
                        f"{RUNTIME_AUTH_TIMEOUT_SECONDS:.1f}s"
                    ) from err

            await _connect_once(use_cache=True)

            try:
                await _authenticate_once()
            except AwoxAuthenticationError:
                # Wrong mesh credentials are not a cache problem.
                await self._async_disconnect(cancel_idle=False)
                raise
            except Exception as cached_auth_error:
                if not self._is_cache_service_error(cached_auth_error):
                    # Timeouts, transport failures and other generic GATT
                    # errors are not evidence of stale cached services.
                    await self._async_disconnect(cancel_idle=False)
                    raise

                # Only an identifiable missing/invalid service or characteristic
                # gets one recovery connection without the local Bleak cache.
                # This is a recovery attempt, not a guarantee that a remote
                # ESPHome service cache has been fully erased.
                _LOGGER.debug(
                    "Runtime GATT service cache looks stale for %s; "
                    "retrying once without local BLE service cache: %s",
                    self.mac,
                    cached_auth_error,
                )
                await self._async_disconnect(cancel_idle=False)
                await _connect_once(use_cache=False)
                try:
                    await _authenticate_once()
                except Exception:
                    await self._async_disconnect(cancel_idle=False)
                    raise

            self._clear_current_error()
            self._async_set_last_connection_source(
                self._actual_connection_source(self._client)
            )
            self._async_note_connection_liveness()
            _LOGGER.debug(
                "Authenticated AwoX Connect.Z %s via one-shot runtime connect",
                self.mac,
            )

    async def _async_disconnect(
        self,
        *,
        cancel_idle: bool = True,
        mark_liveness: bool = False,
    ) -> None:
        if cancel_idle and self._idle_task is not None:
            current = asyncio.current_task()
            if self._idle_task is not current:
                self._idle_task.cancel()
            self._idle_task = None

        client = self._client
        self.status_1911.end()
        self._status_ready_generation = None
        self._session_key = None

        if client is None:
            return

        was_connected = bool(client.is_connected)
        if was_connected and mark_liveness:
            # The connection is known live now. Establish a bounded idle grace
            # before awaiting backend cleanup; its completion is not new evidence.
            self._async_note_connection_liveness()

        async def _cleanup_client() -> None:
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT_SECONDS):
                    await client.disconnect()
            except TimeoutError:
                _LOGGER.warning(
                    "Timed out after %.1fs while disconnecting AwoX %s",
                    DISCONNECT_TIMEOUT_SECONDS,
                    self.mac,
                )
            except Exception as err:
                _LOGGER.debug(
                    "Ignoring AwoX disconnect error for %s: %s",
                    self.mac,
                    err,
                )

        cleanup_task = self.hass.async_create_task(
            _cleanup_client(),
            f"AwoX Connect.Z disconnect cleanup {self.mac}",
        )

        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # The command budget or Home Assistant may cancel the caller,
                # but the proxy cleanup must finish before we release the
                # command semaphore / per-lamp lock.
                cancelled = True
                continue

        # Consume any task exception. Ordinary disconnect errors are already
        # handled inside _cleanup_client; a cancelled cleanup is treated like
        # cancellation of the caller.
        if cleanup_task.cancelled():
            cancelled = True
        else:
            with suppress(Exception):
                cleanup_task.result()

        if self._client is client:
            self._client = None
            self._async_notify_diagnostic_listeners()

        # Per-packet callbacks deliver unchanged advertisements.
        # Preserve manager/scanner history across a normal GATT disconnect.

        if cancelled:
            raise asyncio.CancelledError

    def _schedule_idle_disconnect(self) -> None:
        if self._status_pins:
            return
        if self._idle_task is not None:
            self._idle_task.cancel()

        async def _worker() -> None:
            try:
                await asyncio.sleep(self.idle_disconnect)
                async with self._command_lock:
                    await self._async_disconnect(
                        cancel_idle=False,
                        mark_liveness=True,
                    )
                    _LOGGER.debug(
                        "Idle-disconnected AwoX Connect.Z %s", self.mac
                    )
            except asyncio.CancelledError:
                return
            finally:
                if asyncio.current_task() is self._idle_task:
                    self._idle_task = None

        self._idle_task = self.hass.async_create_task(
            _worker(), f"AwoX Connect.Z idle disconnect {self.mac}"
        )

    async def async_verify_mesh_credentials(self) -> None:
        """Connect and authenticate without sending a lamp command."""
        async with self._command_semaphore:
            async with self._command_lock:
                try:
                    await self._async_ensure_connected()
                    self._clear_current_error()
                except Exception as err:
                    self._record_error(
                        err,
                        category=self._error_category(err, "verification"),
                        stage="credential_verification",
                    )
                    raise
                finally:
                    await self._async_disconnect(cancel_idle=True)

    async def async_send_selected_once(
        self,
        selector: Callable[[], RuntimeSelectedCommand | None],
        *, expected_session: int | None = None,
    ) -> RuntimeCommandResult:
        """Prepare a session, late-select, and attempt exactly one GATT write.

        The selector is called only after connection and authentication.  There
        is no lamp-command retry in this client method: retry eligibility,
        spacing and newer-request handling belong to CommandManager/Executor.

        Connection/authentication failures are reported as PREPARATION_FAILED
        because no lamp command was selected.  Once ``write_gatt_char`` has been
        entered, an exception is conservatively WRITE_UNCERTAIN because the
        transport cannot prove whether the lamp processed the packet. Packet
        construction errors are final: retrying identical input cannot fix them.
        """
        selected_label: str | None = None
        selected_attempt: int | None = None
        command_started_monotonic = time.monotonic()

        def _select() -> RuntimeSelectedCommand | None:
            nonlocal selected_label, selected_attempt
            selected = selector()
            if selected is not None:
                selected_label = selected.label
                selected_attempt = selected.attempt
            return selected

        async with self._command_semaphore:
            async with self._command_lock:
                try:
                    async with asyncio.timeout(
                        RUNTIME_COMMAND_TIMEOUT_SECONDS
                    ) as command_timeout:
                        return await self._async_send_selected_once_locked(
                            _select,
                            runtime_command_timeout=command_timeout,
                            command_started_monotonic=command_started_monotonic,
                            expected_session=expected_session,
                        )
                except asyncio.CancelledError:
                    await self._async_disconnect(cancel_idle=True)
                    raise
                except TimeoutError as err:
                    await self._async_disconnect(cancel_idle=True)
                    if selected_label is None:
                        message = (
                            "AwoX command preparation exceeded the "
                            f"{RUNTIME_COMMAND_TIMEOUT_SECONDS:.1f}s runtime budget"
                        )
                        stage = "command_preparation"
                        status = RuntimeCommandStatus.PREPARATION_FAILED
                    else:
                        message = (
                            f"Command '{selected_label}' exceeded the "
                            f"{RUNTIME_COMMAND_TIMEOUT_SECONDS:.1f}s runtime budget"
                        )
                        stage = "command_runtime"
                        status = RuntimeCommandStatus.WRITE_UNCERTAIN
                    error = AwoxConnectZError(message)
                    error.__cause__ = err
                    self._record_error(
                        error,
                        category="timeout",
                        stage=stage,
                        command_label=selected_label,
                        write_attempt=selected_attempt,
                    )
                    return RuntimeCommandResult(
                        status=status,
                        label=selected_label,
                        error=error,
                        write_finished_monotonic=(
                            time.monotonic()
                            if status is RuntimeCommandStatus.WRITE_UNCERTAIN
                            else None
                        ),
                        attempt=selected_attempt,
                    )

    async def _async_send_selected_once_locked(
        self,
        selector: Callable[[], RuntimeSelectedCommand | None],
        *,
        runtime_command_timeout: asyncio.Timeout,
        command_started_monotonic: float,
        expected_session: int | None = None,
    ) -> RuntimeCommandResult:
        """Run one late-selected physical write while runtime locks are held."""
        try:
            if expected_session is None:
                await self._async_ensure_connected(
                    fast_runtime=True,
                    runtime_command_timeout=runtime_command_timeout,
                )
                await self._async_enable_status()
            else:
                # Continuing groups must never reopen a session after loss.
                self._require_status_session(expected_session)
        except Exception as err:
            if isinstance(err, AwoxDeviceNotFound):
                category, stage, description = "device_not_found", "device_lookup", "device lookup"
            elif isinstance(err, AwoxAuthenticationError):
                category, stage, description = "authentication", "authentication", "authentication"
            else:
                category = self._error_category(err, "connection")
                stage, description = "connect", "BLE connect"
            self._record_error(err, category=category, stage=stage)
            error = AwoxConnectZError(
                f"AwoX command preparation failed during {description}: {err}"
            )
            error.__cause__ = err
            await self._async_disconnect(cancel_idle=True)
            return RuntimeCommandResult(RuntimeCommandStatus.PREPARATION_FAILED, error=error)

        # Final late-selection boundary. No await occurs between selection and
        # the checks/encryption that precede the physical GATT call.
        try:
            selected = selector()
        except Exception:
            # Frame construction can fail before selection. Keep the prepared
            # session subject to normal idle cleanup even without a write.
            self._schedule_idle_disconnect()
            raise
        if selected is None:
            self._clear_current_error()
            self._schedule_idle_disconnect()
            return RuntimeCommandResult(RuntimeCommandStatus.NOT_SELECTED)

        plain16 = selected.frame
        label = selected.label
        attempt = max(1, int(selected.attempt))

        if self._client is None or self._session_key is None:
            error = AwoxConnectZError("AwoX session was not created")
            self._record_error(
                error,
                category="write",
                stage="gatt_write_preparation",
                command_label=label,
                write_attempt=attempt,
            )
            await self._async_disconnect(cancel_idle=True)
            return RuntimeCommandResult(
                RuntimeCommandStatus.WRITE_FAILED,
                label=label,
                error=error,
                write_finished_monotonic=time.monotonic(),
                attempt=attempt,
            )

        try:
            packet = encrypt_command(self._session_key, plain16)
        except Exception as err:
            error = AwoxConnectZError(
                f"Command '{label}' packet construction failed: {err}"
            )
            error.__cause__ = err
            self._record_error(
                error,
                category="protocol",
                stage="command_encryption",
                command_label=label,
                write_attempt=attempt,
            )
            self._schedule_idle_disconnect()
            return RuntimeCommandResult(
                RuntimeCommandStatus.WRITE_FAILED_FINAL,
                label=label,
                error=error,
                write_finished_monotonic=time.monotonic(),
                attempt=attempt,
            )

        try:
            async with asyncio.timeout(RUNTIME_WRITE_TIMEOUT_SECONDS):
                await self._client.write_gatt_char(
                    # State success is checked separately through 1911.
                    COMMAND_CHAR_UUID, packet, response=False
                )
        except Exception as err:
            write_finished = time.monotonic()
            is_timeout = isinstance(err, TimeoutError)
            error = AwoxConnectZError(
                f"GATT write timed out after {RUNTIME_WRITE_TIMEOUT_SECONDS:.1f}s"
                if is_timeout else f"Command '{label}' write failed: {err}"
            )
            error.__cause__ = err
            self._record_error(
                error,
                category="timeout" if is_timeout else self._error_category(err, "write"),
                stage="gatt_write",
                command_label=label,
                write_attempt=attempt,
            )
            await self._async_disconnect(cancel_idle=True)
            return RuntimeCommandResult(
                RuntimeCommandStatus.WRITE_UNCERTAIN,
                label=label,
                error=error,
                write_finished_monotonic=write_finished,
                attempt=attempt,
            )

        write_finished = time.monotonic()
        self._record_command_success(
            label,
            started_monotonic=command_started_monotonic,
            attempts=attempt,
        )
        # A write without response is not a new received liveness proof.
        self._schedule_idle_disconnect()
        return RuntimeCommandResult(
            RuntimeCommandStatus.SUCCESS,
            label=label,
            write_finished_monotonic=write_finished,
            attempt=attempt,
        )

    async def _async_start_status_notify(self, notification) -> None:
        """Handle the measured Connect.Z no-CCCD quirk on ESPHome only.

        These lamps expose 1911 with a 2901 description but no 2902 CCCD.
        Vendor 01 enables their reports; never invent or write a descriptor.
        Keep the real ESPHome backend's subscription registry for cleanup.
        """
        client = self._client
        backend = getattr(client, "_backend", None)
        if backend is not None:
            try:
                from bleak_esphome.backend.client import ESPHomeClient
            except ImportError:
                ESPHomeClient = None
            if ESPHomeClient is not None and isinstance(backend, ESPHomeClient):
                characteristic = client.services.get_characteristic(STATUS_CHAR_UUID)
                cccd_uuid = "00002902-0000-1000-8000-00805f9b34fb"
                if (characteristic is not None and
                        "notify" in characteristic.properties and
                        characteristic.get_descriptor(cccd_uuid) is None):
                    # Private compatibility boundary, verified against the actual
                    # backend. Other characteristics/backends keep normal Bleak.
                    subscriptions = backend._notify_cancels
                    handle = characteristic.handle
                    if handle in subscriptions:
                        raise AwoxConnectZError("1911 notifications already registered")
                    subscription = await backend._client.bluetooth_gatt_start_notify(
                        backend._address_as_int, handle,
                        lambda _handle, data: notification(characteristic, data),
                        timeout=RUNTIME_WRITE_TIMEOUT_SECONDS,
                    )
                    subscriptions[handle] = subscription
                    self._status_notify_method = "esphome_vendor_no_cccd"
                    return
        await client.start_notify(STATUS_CHAR_UUID, notification)
        self._status_notify_method = "standard_bleak"

    async def _async_enable_status(self) -> None:
        if self._status_ready_generation == self._session_generation:
            return
        if not self.connected or self._session_key is None:
            raise AwoxConnectZError("Cannot initialize 1911 without authentication")
        generation, key = self._session_generation, self._session_key
        self.status_1911.begin(generation)
        def notification(_sender, payload):
            if self.connected:
                before = self.status_1911.sequence
                self.status_1911.feed(bytes(payload), key, generation)
                if self.status_1911.sequence != before:
                    self._async_note_connection_liveness()
        try:
            async with asyncio.timeout(RUNTIME_WRITE_TIMEOUT_SECONDS):
                await self._async_start_status_notify(notification)
                await self._client.write_gatt_char(STATUS_CHAR_UUID, bytes((1,)), response=False)
        except BaseException:
            await self._async_disconnect(cancel_idle=True)
            raise
        self._status_ready_generation = generation

    @asynccontextmanager
    async def status_transaction(self):
        """Hold idle disconnect off across the group and its confirmation."""
        self._status_pins += 1
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        try:
            yield
        finally:
            self._status_pins -= 1
            if not self._status_pins and self.connected and not self._closed:
                self._schedule_idle_disconnect()

    async def async_status_delay(self, seconds: float, valid) -> None:
        deadline = time.monotonic() + seconds
        while valid() and (remaining := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(remaining, .05))

    def _require_status_session(self, generation: int) -> None:
        if (not self.connected or self._session_key is None or
                self._status_ready_generation != generation or
                self.status_1911.session_generation != generation):
            raise AwoxConnectZError("Status session lost; automatic batch reconnect is disabled")

    async def _async_status_write(
        self, packet: Callable[[bytes], bytes], generation: int,
        valid: Callable[[], bool] | None = None,
    ) -> tuple[float, float]:
        """Write in an existing session; queue time never counts as reply freshness."""
        async with asyncio.timeout(RUNTIME_COMMAND_TIMEOUT_SECONDS):
            async with self._command_semaphore:
                async with self._command_lock:
                    self._require_status_session(generation)
                    if valid is not None and not valid():
                        raise ConfirmationSuperseded()
                    encoded = packet(self._session_key)
                    async with asyncio.timeout(RUNTIME_WRITE_TIMEOUT_SECONDS):
                        started = time.monotonic()
                        await self._client.write_gatt_char(
                            COMMAND_CHAR_UUID, encoded, response=False,
                        )
                    return started, time.monotonic()

    async def async_poll_status(self, generation: int) -> None:
        await self._async_status_write(
            lambda key: make_status_query(key, self.configured_mesh_id), generation,
        )

    async def async_repeat_plaintext(
        self, frame: bytes, generation: int, valid: Callable[[], bool],
    ) -> tuple[float, float]:
        return await self._async_status_write(
            lambda key: encrypt_command(key, frame), generation, valid,
        )

    async def async_initial_status(self):
        """Bounded UNKNOWN bootstrap; never called before an ordinary known-state request."""
        started = time.monotonic()
        async with self.status_transaction():
            async with self._command_semaphore:
                async with self._command_lock:
                    async with asyncio.timeout(RUNTIME_COMMAND_TIMEOUT_SECONDS) as budget:
                        await self._async_ensure_connected(
                            fast_runtime=True, runtime_command_timeout=budget,
                        )
                        await self._async_enable_status()
            generation = self.status_1911.session_generation
            valid = lambda: self.connected and not self._closed
            latest = self.status_1911.latest
            if latest is not None and latest.received_monotonic >= started:
                return latest
            for _ in range(CONFIRMATION_MAX_POLLS):
                self._require_status_session(generation)
                await self.async_poll_status(generation)
                self._initial_status_polls += 1
                state = await self.status_1911.wait(
                    {}, generation, started,
                    time.monotonic() + CONFIRMATION_POLL_WINDOW_MS / 1000, valid,
                )
                if state:
                    return state
            raise AwoxConnectZError("No valid 1911 initial state within the poll budget")

    async def async_close(self) -> None:
        """Stop timers and release the BLE connection."""
        self._closed = True
        self._availability_wakeup.set()
        self._async_notify_diagnostic_listeners()

        if self._availability_task is not None:
            self._availability_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._availability_task
            self._availability_task = None

        if self._idle_task is not None:
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None

        async with self._command_lock:
            await self._async_disconnect(cancel_idle=False)

    def diagnostics(self) -> dict[str, Any]:
        """Return the existing HA diagnostic download representation."""
        return build_client_diagnostics(self)
