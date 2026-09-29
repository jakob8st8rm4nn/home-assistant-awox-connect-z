"""Resilient BLE client for AwoX Connect.Z."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .advertisement import AwoxAdvertisementState
from .const import COMMAND_CHAR_UUID, PAIR_CHAR_UUID
from .protocol import encrypt_command, make_pair_packet, make_session_key

_LOGGER = logging.getLogger(__name__)

DISCOVERY_ATTEMPTS = 5
COMMAND_ATTEMPTS = 3


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
    ) -> None:
        self.hass = hass
        self.mac = mac
        self.mesh_name = mesh_name
        self.mesh_password = mesh_password
        self.default_transition = max(0.0, float(default_transition))
        self.idle_disconnect = max(5.0, float(idle_disconnect))
        self.max_concurrent_commands = max(1, int(max_concurrent_commands))
        self._command_semaphore = command_semaphore

        self._client: Any | None = None
        self._session_key: bytes | None = None
        self._connect_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._closed = False

        self.last_error: str | None = None
        self.last_command: str | None = None

        self._advertisement_state: AwoxAdvertisementState | None = None
        self._advertisement_listeners: set[
            Callable[[AwoxAdvertisementState], None]
        ] = set()

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
                await asyncio.sleep(min(1.5 * attempt, 4.0))

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
        await asyncio.sleep(0.05)
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

    async def _async_ensure_connected(self) -> None:
        if self._closed:
            raise AwoxConnectZError("AwoX client is closed")
        if self.connected and self._session_key is not None:
            return

        async with self._connect_lock:
            if self.connected and self._session_key is not None:
                return

            await self._async_disconnect(cancel_idle=False)
            device = await self._async_find_device()

            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    device.name or f"AwoX Connect.Z {self.mac}",
                    max_attempts=4,
                )
                self._client = client
                await self._async_authenticate()
                self.last_error = None
                _LOGGER.debug("Authenticated AwoX Connect.Z %s", self.mac)
            except Exception:
                await self._async_disconnect(cancel_idle=False)
                raise

    async def _async_disconnect(self, *, cancel_idle: bool = True) -> None:
        if cancel_idle and self._idle_task is not None:
            current = asyncio.current_task()
            if self._idle_task is not current:
                self._idle_task.cancel()
            self._idle_task = None

        client = self._client
        self._client = None
        self._session_key = None

        had_client = client is not None
        if client is not None and client.is_connected:
            with suppress(Exception):
                await client.disconnect()

        if had_client:
            # Home Assistant deduplicates identical advertisements. Clear the
            # per-address history after every known GATT session, including when
            # the BLE link already dropped unexpectedly before cleanup ran.
            clear_history = getattr(
                bluetooth, "async_clear_advertisement_history", None
            )
            if clear_history is not None:
                with suppress(Exception):
                    clear_history(self.hass, self.mac)

    def _schedule_idle_disconnect(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()

        async def _worker() -> None:
            try:
                await asyncio.sleep(self.idle_disconnect)
                async with self._command_lock:
                    await self._async_disconnect(cancel_idle=False)
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

    async def async_send_plain(self, plain16: bytes, *, label: str) -> None:
        """Send one command, reconnecting and re-authenticating when needed."""
        async with self._command_semaphore:
            async with self._command_lock:
                last_exception: Exception | None = None

                for attempt in range(1, COMMAND_ATTEMPTS + 1):
                    try:
                        await self._async_ensure_connected()
                        if self._client is None or self._session_key is None:
                            raise AwoxConnectZError(
                                "AwoX session was not created"
                            )

                        packet = encrypt_command(self._session_key, plain16)
                        await self._client.write_gatt_char(
                            COMMAND_CHAR_UUID, packet, response=True
                        )
                        self.last_error = None
                        self.last_command = label
                        self._schedule_idle_disconnect()
                        return
                    except Exception as err:
                        last_exception = err
                        self.last_error = str(err)
                        _LOGGER.debug(
                            "AwoX command %s failed on attempt %s/%s: %s",
                            label,
                            attempt,
                            COMMAND_ATTEMPTS,
                            err,
                        )
                        await self._async_disconnect(cancel_idle=True)
                        if attempt < COMMAND_ATTEMPTS:
                            await asyncio.sleep(0.5 * attempt)

                raise AwoxConnectZError(
                    f"Command '{label}' failed after "
                    f"{COMMAND_ATTEMPTS} attempts: {last_exception}"
                ) from last_exception

    async def async_close(self) -> None:
        """Stop timers and release the BLE connection."""
        self._closed = True
        if self._idle_task is not None:
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None

        async with self._command_lock:
            await self._async_disconnect(cancel_idle=False)

    def diagnostics(self) -> dict[str, Any]:
        """Safe diagnostics without credentials."""
        return {
            "mac": self.mac,
            "connected": self.connected,
            "closed": self.closed,
            "default_transition": self.default_transition,
            "idle_disconnect": self.idle_disconnect,
            "max_concurrent_commands": self.max_concurrent_commands,
            "last_command": self.last_command,
            "last_error": self.last_error,
        }
