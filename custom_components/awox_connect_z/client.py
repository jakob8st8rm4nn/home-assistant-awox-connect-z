"""Resilient BLE client for AwoX Connect.Z."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from contextlib import suppress
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

from .advertisement import AwoxAdvertisementState
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
    RUNTIME_WRITE_ATTEMPTS,
    RUNTIME_WRITE_TIMEOUT_SECONDS,
    SETUP_CONNECT_ATTEMPTS,
)
from .protocol import encrypt_command, make_pair_packet, make_session_key

_LOGGER = logging.getLogger(__name__)


class AwoxConnectZError(HomeAssistantError):
    """Base AwoX Connect.Z error."""


class AwoxDeviceNotFound(AwoxConnectZError):
    """Lamp was not visible through any HA Bluetooth adapter/proxy."""


class AwoxAuthenticationError(AwoxConnectZError):
    """Lamp rejected the local mesh credential."""


class AwoxConnectZClient:
    """Persistent-on-demand BLE session with retry and idle disconnect."""

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
        self.advertised_mesh_id: int | None = None
        self.advertised_mesh_id_time: datetime | None = None
        self.mesh_id_status = "unknown"

        self._advertisement_state: AwoxAdvertisementState | None = None
        self._advertisement_listeners: set[
            Callable[[AwoxAdvertisementState], None]
        ] = set()

        self._available = True
        self._availability_started = time.monotonic()
        self._last_liveness = self._availability_started
        self._availability_task: asyncio.Task[None] | None = None
        self._availability_wakeup = asyncio.Event()
        self._availability_listeners: set[Callable[[bool], None]] = set()

        self._diagnostic_listeners: set[Callable[[], None]] = set()
        self._last_connection_source: str | None = None
        self._last_connection_observed = False
        self._last_seen_service_time: float | None = None
        self._last_seen_wallclock: datetime | None = None
        # Availability starts with a grace period so the light entity does not
        # immediately flap unavailable after a reload. Diagnostics must not
        # mistake that grace period for confirmed Bluetooth visibility.
        self._bluetooth_liveness_confirmed = False

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
        return not self._closed and self._available

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
        """Return RSSI from the current best Bluetooth advertisement."""
        if not self.available:
            return None
        service_info = self._latest_service_info()
        return None if service_info is None else int(service_info.rssi)

    @callback
    def _async_update_last_seen(self, seen_time: float) -> bool:
        """Cache one monotonic advertisement timestamp as a stable UTC value."""
        seen_time = float(seen_time)
        if (
            self._last_seen_service_time is not None
            and seen_time <= self._last_seen_service_time
        ):
            return False

        # BluetoothServiceInfoBleak.time uses the event loop's monotonic clock.
        # Convert it once when HA reports a newer packet instead of recalculating
        # it on every entity read, which can otherwise introduce tiny jitter.
        age = max(0.0, time.monotonic() - seen_time)
        self._last_seen_service_time = seen_time
        self._last_seen_wallclock = dt_util.utcnow() - timedelta(seconds=age)
        return True

    def _refresh_last_seen_from_ha(self) -> bool:
        """Refresh cached Bluetooth evidence from HA's advertisement history."""
        service_info = self._latest_service_info()
        if service_info is None:
            return False
        self._bluetooth_liveness_confirmed = True
        self._async_update_last_seen(float(service_info.time))
        return True

    @property
    def last_seen(self) -> datetime | None:
        """Return the stable wall-clock time of HA's newest advertisement."""
        self._refresh_last_seen_from_ha()
        return self._last_seen_wallclock

    @property
    def bluetooth_status(self) -> str | None:
        """Return the confirmed Bluetooth path state for diagnostics.

        Availability deliberately starts with a grace period after setup/reload.
        Until an advertisement or successful GATT connection confirms actual
        Bluetooth liveness, reporting ``visible`` would be misleading. Home
        Assistant renders ``None`` as Unknown for the enum sensor.
        """
        if self.connected:
            return "connected"

        if not self._bluetooth_liveness_confirmed:
            # HA can learn an advertisement without dispatching our integration
            # callback (for example when an identical packet is deduplicated).
            # Treat an advertisement present in HA's Bluetooth history as real
            # visibility evidence as well.
            self._refresh_last_seen_from_ha()

        if self.available:
            if self._bluetooth_liveness_confirmed:
                return "visible"
            return None
        return "unreachable"

    @property
    def current_bluetooth_source(self) -> str | None:
        """Return the scanner/proxy supplying HA's current best advertisement."""
        if not self.available:
            return None
        service_info = self._latest_service_info()
        if service_info is None:
            return None
        return self.bluetooth_source_name(str(service_info.source))

    @property
    def current_bluetooth_source_id(self) -> str | None:
        """Return the raw source ID for HA's current best advertisement."""
        if not self.available:
            return None
        service_info = self._latest_service_info()
        if service_info is None:
            return None
        return str(service_info.source)

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

    @callback
    def async_add_diagnostic_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Subscribe an entity to Bluetooth diagnostic changes."""
        self._diagnostic_listeners.add(listener)

        @callback
        def _remove_listener() -> None:
            self._diagnostic_listeners.discard(listener)

        return _remove_listener

    @callback
    def _async_notify_diagnostic_listeners(self) -> None:
        """Notify diagnostic entities without affecting BLE command handling."""
        for listener in tuple(self._diagnostic_listeners):
            try:
                listener()
            except Exception:  # diagnostics must never break a BLE operation
                _LOGGER.exception(
                    "AwoX Connect.Z diagnostic listener failed for %s", self.mac
                )

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
        self._availability_listeners.add(listener)

        @callback
        def _remove_listener() -> None:
            self._availability_listeners.discard(listener)

        return _remove_listener

    @callback
    def _async_set_available(self, available: bool) -> None:
        """Update availability and notify listeners only on a change."""
        if self._available == available:
            return

        self._available = available
        for listener in tuple(self._availability_listeners):
            listener(available)
        self._async_notify_diagnostic_listeners()

    @callback
    def async_note_bluetooth_liveness(self, seen_time: float | None = None) -> None:
        """Record a Bluetooth packet and make the lamp available immediately."""
        seen = seen_time if seen_time is not None else time.monotonic()
        self._bluetooth_liveness_confirmed = True
        self._last_liveness = max(self._last_liveness, seen)
        self._async_update_last_seen(seen)
        self._async_set_available(True)
        self._availability_wakeup.set()
        self._async_notify_diagnostic_listeners()

    @callback
    def _async_note_connection_liveness(self) -> None:
        """Record a known-good local GATT session as a liveness signal."""
        self._bluetooth_liveness_confirmed = True
        self._last_liveness = time.monotonic()
        self._async_set_available(True)
        self._availability_wakeup.set()
        self._async_notify_diagnostic_listeners()

    def _latest_liveness_time(self) -> float:
        """Return the newest packet/session liveness timestamp known to HA."""
        latest = self._last_liveness
        service_info = bluetooth.async_last_service_info(
            self.hass, self.mac, connectable=False
        )
        if service_info is not None:
            self._bluetooth_liveness_confirmed = True
            seen_time = float(service_info.time)
            self._async_update_last_seen(seen_time)
            latest = max(latest, seen_time)
        return latest

    @callback
    def async_start_availability_tracking(self) -> None:
        """Start availability tracking for this lamp."""
        if self._availability_task is not None or self._closed:
            return

        async def _worker() -> None:
            try:
                while not self._closed:
                    self._availability_wakeup.clear()

                    if self.connected:
                        self._async_set_available(True)
                        wait_seconds = self.availability_timeout
                    else:
                        age = max(
                            0.0,
                            time.monotonic() - self._latest_liveness_time(),
                        )
                        if age < self.availability_timeout:
                            self._async_set_available(True)
                            wait_seconds = max(
                                AVAILABILITY_MIN_WAIT_SECONDS,
                                self.availability_timeout - age,
                            )
                        else:
                            self._async_set_available(False)
                            # Home Assistant updates BluetoothServiceInfoBleak.time
                            # even when an identical advertisement is deduplicated
                            # before integration callbacks. While unavailable, poll
                            # that timestamp briefly so unchanged packets restore
                            # availability as soon as practical.
                            wait_seconds = AVAILABILITY_RECOVERY_POLL_SECONDS

                    try:
                        await asyncio.wait_for(
                            self._availability_wakeup.wait(),
                            timeout=wait_seconds,
                        )
                    except TimeoutError:
                        # HA may refresh BluetoothServiceInfoBleak.time for an
                        # identical advertisement without dispatching the normal
                        # integration callback. Re-evaluate diagnostic sensors on
                        # this bounded availability cadence as well.
                        self._async_notify_diagnostic_listeners()
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
        self._advertisement_listeners.add(listener)

        @callback
        def _remove_listener() -> None:
            self._advertisement_listeners.discard(listener)

        return _remove_listener

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
        characteristic_missing = (
            "characteristic" in message
            and (
                "not found" in message
                or "could not be found" in message
                or "does not exist" in message
            )
        )
        service_missing = (
            "service" in message
            and (
                "not found" in message
                or "could not be found" in message
                or "does not exist" in message
            )
        )
        stale_handle = (
            "invalid handle" in message
            or "attribute not found" in message
        )
        return characteristic_missing or service_missing or stale_handle

    def _async_find_device_runtime(self) -> Any:
        """Return the currently known HA Bluetooth device without retry sleeps."""
        device = async_ble_device_from_address(
            self.hass, self.mac, connectable=True
        )
        if device is None:
            device = async_ble_device_from_address(
                self.hass, self.mac, connectable=False
            )
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
            device = async_ble_device_from_address(
                self.hass, self.mac, connectable=True
            )
            if device is None:
                device = async_ble_device_from_address(
                    self.hass, self.mac, connectable=False
                )
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
        command_budget_suspended = False
        if command_timeout is not None and not command_timeout.expired():
            previous_deadline = command_timeout.when()
            if previous_deadline is not None:
                command_timeout.reschedule(None)
                command_budget_suspended = True

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
                command_budget_suspended
                and command_timeout is not None
                and not command_timeout.expired()
                and previous_deadline is not None
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
                    except TimeoutError as err:
                        await self._async_disconnect(cancel_idle=False)
                        raise AwoxConnectZError(
                            f"Runtime BLE connect timed out after "
                            f"{RUNTIME_CONNECT_TIMEOUT_SECONDS:.1f}s"
                        ) from err
                    except Exception as err:
                        await self._async_disconnect(cancel_idle=False)
                        raise AwoxConnectZError(
                            f"Runtime BLE connect failed: {err}"
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
            except asyncio.CancelledError:
                raise
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
                except asyncio.CancelledError:
                    raise
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
        self._session_key = None

        if client is None:
            return

        was_connected = bool(client.is_connected)

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
            except asyncio.CancelledError:
                raise
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

        if was_connected and mark_liveness:
            self._async_note_connection_liveness()

        # Per-packet callbacks deliver unchanged advertisements.
        # Preserve manager/scanner history across a normal GATT disconnect.

        if cancelled:
            raise asyncio.CancelledError

    def _schedule_idle_disconnect(self) -> None:
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
                except asyncio.CancelledError:
                    await self._async_disconnect(cancel_idle=True)
                    raise
                except Exception as err:
                    self._record_error(
                        err,
                        category=self._error_category(err, "verification"),
                        stage="credential_verification",
                    )
                    raise
                finally:
                    await self._async_disconnect(cancel_idle=True)

    async def async_send_selected(
        self,
        selector: Callable[[], tuple[bytes, str] | None],
    ) -> bool:
        """Send one runtime command selected immediately before the GATT write.

        Waiting for the command semaphore, per-lamp command lock, runtime connect
        gate, BLE connection, or authentication does not freeze a pending target.
        The selector is called only after a usable authenticated session exists.
        Once selected, that command remains fixed across the bounded write retry.
        """
        selected_label: str | None = None
        command_started_monotonic = time.monotonic()

        def _select() -> tuple[bytes, str] | None:
            nonlocal selected_label
            selected = selector()
            if selected is not None:
                selected_label = selected[1]
            return selected

        async with self._command_semaphore:
            async with self._command_lock:
                try:
                    async with asyncio.timeout(
                        RUNTIME_COMMAND_TIMEOUT_SECONDS
                    ) as command_timeout:
                        return await self._async_send_selected_locked(
                            _select,
                            runtime_command_timeout=command_timeout,
                            command_started_monotonic=command_started_monotonic,
                        )
                except asyncio.CancelledError:
                    # Keep the integration slot and per-lamp lock until cleanup
                    # has been attempted, then propagate the external cancel.
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
                    else:
                        message = (
                            f"Command '{selected_label}' exceeded the "
                            f"{RUNTIME_COMMAND_TIMEOUT_SECONDS:.1f}s runtime budget"
                        )
                        stage = "command_runtime"
                    self._record_error(
                        message,
                        category="timeout",
                        stage=stage,
                        command_label=selected_label,
                    )
                    raise AwoxConnectZError(message) from err

    async def _async_send_selected_locked(
        self,
        selector: Callable[[], tuple[bytes, str] | None],
        *,
        runtime_command_timeout: asyncio.Timeout,
        command_started_monotonic: float,
    ) -> bool:
        """Run one late-selected command while runtime locks are already held."""
        try:
            await self._async_ensure_connected(
                fast_runtime=True,
                runtime_command_timeout=runtime_command_timeout,
            )
        except AwoxDeviceNotFound as err:
            self._record_error(
                err, category="device_not_found", stage="device_lookup"
            )
            raise AwoxConnectZError(
                f"AwoX command preparation failed during device lookup: {err}"
            ) from err
        except AwoxAuthenticationError as err:
            self._record_error(
                err, category="authentication", stage="authentication"
            )
            raise AwoxConnectZError(
                f"AwoX command preparation failed during authentication: {err}"
            ) from err
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._record_error(
                err,
                category=self._error_category(err, "connection"),
                stage="connect",
            )
            raise AwoxConnectZError(
                f"AwoX command preparation failed during BLE connect: {err}"
            ) from err

        # This is the write boundary for same-lamp coalescing. The entity may
        # replace waiting requests right up to this synchronous callback. No
        # await occurs between selection and construction of the first packet.
        selected = selector()
        if selected is None:
            self._clear_current_error()
            self._schedule_idle_disconnect()
            return False

        plain16, label = selected
        last_exception: Exception | None = None

        for attempt in range(1, RUNTIME_WRITE_ATTEMPTS + 1):
            try:
                if self._client is None or self._session_key is None:
                    raise AwoxConnectZError(
                        "AwoX session was not created"
                    )

                packet = encrypt_command(self._session_key, plain16)
                async with asyncio.timeout(RUNTIME_WRITE_TIMEOUT_SECONDS):
                    await self._client.write_gatt_char(
                        COMMAND_CHAR_UUID, packet, response=True
                    )

                self._record_command_success(
                    label,
                    started_monotonic=command_started_monotonic,
                    attempts=attempt,
                )
                self._async_note_connection_liveness()
                self._schedule_idle_disconnect()
                return True
            except asyncio.CancelledError:
                raise
            except TimeoutError as err:
                last_exception = AwoxConnectZError(
                    f"GATT write timed out after "
                    f"{RUNTIME_WRITE_TIMEOUT_SECONDS:.1f}s"
                )
                last_exception.__cause__ = err
            except Exception as err:
                last_exception = err

            self._record_error(
                last_exception,
                category=self._error_category(last_exception, "write"),
                stage="gatt_write",
                command_label=label,
                write_attempt=attempt,
            )
            _LOGGER.debug(
                "AwoX command write %s failed on attempt %s/%s: %s",
                label,
                attempt,
                RUNTIME_WRITE_ATTEMPTS,
                last_exception,
            )
            await self._async_disconnect(cancel_idle=True)

            if attempt >= RUNTIME_WRITE_ATTEMPTS:
                break

            # The selected command is now genuinely in flight and remains fixed
            # across the one allowed reconnect/write retry. The recovery connect
            # still uses the shared runtime connection gate.
            try:
                await self._async_ensure_connected(
                    fast_runtime=True,
                    runtime_command_timeout=runtime_command_timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception as reconnect_err:
                self._record_error(
                    reconnect_err,
                    category=self._error_category(reconnect_err, "connection"),
                    stage="retry_connect",
                    command_label=label,
                    write_attempt=attempt,
                )
                raise AwoxConnectZError(
                    f"Command '{label}' write failed; "
                    f"reconnect failed: {reconnect_err}"
                ) from reconnect_err

        raise AwoxConnectZError(
            f"Command '{label}' failed after "
            f"{RUNTIME_WRITE_ATTEMPTS} write attempt(s): "
            f"{last_exception}"
        ) from last_exception

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
        """Safe diagnostics without credentials."""
        last_seen = self.last_seen
        return {
            "mac": self.mac,
            "connected": self.connected,
            "available": self.available,
            "bluetooth_status": self.bluetooth_status,
            "signal_strength": self.signal_strength,
            "last_seen": last_seen.isoformat() if last_seen is not None else None,
            "current_bluetooth_source": self.current_bluetooth_source,
            "current_bluetooth_source_id": self.current_bluetooth_source_id,
            "last_connection": self.last_connection_source,
            "last_connection_source_id": self.last_connection_source_id,
            "last_connection_time": (
                self.last_connection_time.isoformat()
                if self.last_connection_time is not None
                else None
            ),
            "closed": self.closed,
            "availability_timeout": self.availability_timeout,
            "default_transition": self.default_transition,
            "idle_disconnect": self.idle_disconnect,
            "max_concurrent_commands": self.max_concurrent_commands,
            # Keep the existing flat values for compatibility and add grouped
            # detail below for easier bug-report reading.
            "last_command": self.last_command,
            "last_error": self.last_error,
            "command": {
                "last_successful": self.last_command,
                "time": (
                    self.last_command_time.isoformat()
                    if self.last_command_time is not None
                    else None
                ),
                "client_operation_duration_ms": (
                    self.last_command_client_operation_duration_ms
                ),
                "attempts": self.last_command_attempts,
                "retries": self.last_command_retries,
            },
            "error": {
                "last_message": self.last_error_message,
                "time": (
                    self.last_error_time.isoformat()
                    if self.last_error_time is not None
                    else None
                ),
                "category": self.last_error_category,
                "stage": self.last_error_stage,
                "command": self.last_error_command,
                "write_attempt": self.last_error_write_attempt,
                "current_message": self.last_error,
            },
            "mesh_id": {
                "configured": (
                    f"0x{self.configured_mesh_id:04X}"
                    if self.configured_mesh_id is not None
                    else None
                ),
                "advertised": (
                    f"0x{self.advertised_mesh_id:04X}"
                    if self.advertised_mesh_id is not None
                    else None
                ),
                "advertised_time": (
                    self.advertised_mesh_id_time.isoformat()
                    if self.advertised_mesh_id_time is not None
                    else None
                ),
                "status": self.mesh_id_status,
            },
        }
