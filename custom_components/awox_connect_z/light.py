"""Light entities for AwoX Connect.Z."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .advertisement import AwoxAdvertisementState
from .client import AwoxConnectZClient
from .const import DOMAIN, MAX_COLOR_TEMP_KELVIN, MIN_COLOR_TEMP_KELVIN
from .protocol import (
    ha_brightness_to_device,
    make_brightness,
    make_color_temp_kelvin,
    make_hs_color,
    make_power,
)

@dataclass(slots=True)
class _PendingPower:
    """Newest waiting power request."""

    value: bool
    sequence: int


@dataclass(slots=True)
class _PendingBrightness:
    """Newest waiting brightness request."""

    value: int
    transition: float
    sequence: int


@dataclass(slots=True)
class _PendingAppearance:
    """Newest waiting color or color-temperature request."""

    color_mode: ColorMode
    value: tuple[float, float] | int
    transition: float
    sequence: int


@dataclass(slots=True)
class _InFlightCommand:
    """One command selected immediately before its GATT write."""

    kind: str
    sequences: set[int]
    value: Any = None


# Home Assistant's platform semaphore is static. The integration uses a
# per-config-entry semaphore so the limit can be changed in Options.
PARALLEL_UPDATES = 0

# Hardware settling guard: Connect.Z lamps can apply a very recent power-off
# after a newer turn-on-style command when both writes are too close together.
# Only commands that can turn the lamp back on are delayed, and only after an
# off command was actually written successfully.
POWER_OFF_SETTLE_SECONDS = 0.5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one light entity per imported account device."""
    clients: list[tuple[AwoxConnectZClient, dict]] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AwoxConnectZLight(client, device) for client, device in clients])


class AwoxConnectZLight(LightEntity, RestoreEntity):
    """AwoX Connect.Z light with optimistic commands and advertisement state sync."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.TRANSITION
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
    _attr_should_poll = False

    def __init__(self, client: AwoxConnectZClient, device: dict[str, Any]) -> None:
        self._client = client
        self._command_worker: asyncio.Task[None] | None = None
        self._command_worker_generation = 0
        self._command_worker_stopping = False
        self._command_worker_cancel_cutoff: int | None = None
        self._entity_unloading = False
        self._request_sequence = 0
        self._pending_power: _PendingPower | None = None
        self._pending_brightness: _PendingBrightness | None = None
        self._pending_appearance: _PendingAppearance | None = None
        self._request_waiters: dict[int, asyncio.Future[None]] = {}
        self._inflight_sequences: set[int] = set()
        self._inflight_command: _InFlightCommand | None = None
        self._power_off_settle_until = 0.0
        self._device = device
        self._mac = client.mac
        self._mesh_id = int(device["mesh_id"])
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
        """Return availability from Bluetooth liveness tracking."""
        return self._client.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Useful diagnostics without exposing credentials."""
        return {
            "ble_connected": self._client.connected,
            "last_ble_error": self._client.last_error,
            "last_command": self._client.last_command,
            "cloud_mesh_id": self._device.get("mesh_id"),
            "mesh_destination": f"0x{self._mesh_id:04X}",
            "cloud_device_type": self._device.get("device_type"),
        }

    async def async_added_to_hass(self) -> None:
        """Restore state, then prefer the newest real advertisement state."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
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

        self.async_on_remove(
            self._client.async_add_advertisement_listener(
                self._async_apply_advertisement_state
            )
        )
        self.async_on_remove(
            self._client.async_add_availability_listener(
                self._async_availability_changed
            )
        )
        self._entity_unloading = False
        self.async_on_remove(self._async_cancel_command_worker)

        if self._client.advertisement_state is not None:
            self._async_apply_advertisement_state(
                self._client.advertisement_state
            )

    @callback
    def _async_availability_changed(self, _available: bool) -> None:
        """Write entity state when Bluetooth availability changes."""
        self.async_write_ha_state()

    @callback
    def _async_apply_advertisement_state(
        self, state: AwoxAdvertisementState
    ) -> None:
        """Apply a hardware-reported Connect.Z advertisement state."""
        self._attr_is_on = state.is_on
        self._attr_brightness = state.brightness

        if state.is_color_mode:
            if (
                state.hue_degrees is not None
                and state.saturation_percent is not None
            ):
                self._attr_hs_color = (
                    float(state.hue_degrees),
                    float(state.saturation_percent),
                )
            self._attr_color_mode = ColorMode.HS
        else:
            if state.color_temp_kelvin is not None:
                self._attr_color_temp_kelvin = max(
                    MIN_COLOR_TEMP_KELVIN,
                    min(MAX_COLOR_TEMP_KELVIN, state.color_temp_kelvin),
                )
            self._attr_color_mode = ColorMode.COLOR_TEMP

        self.async_write_ha_state()

    @callback
    def _cancel_request_sequences(self, sequences: set[int]) -> None:
        """Cancel only the supplied request generations and their owned state."""
        if not sequences:
            return

        for sequence in sequences:
            waiter = self._request_waiters.pop(sequence, None)
            if waiter is not None and not waiter.done():
                waiter.cancel()

        self._drop_pending_sequences(sequences)

        if self._inflight_sequences.intersection(sequences):
            self._inflight_sequences.difference_update(sequences)
            if not self._inflight_sequences:
                self._inflight_command = None

    @callback
    def _cancel_outstanding_requests(self) -> None:
        """Cancel every outstanding request, used only for full entity unload."""
        self._cancel_request_sequences(set(self._request_waiters))
        self._inflight_sequences.clear()
        self._inflight_command = None
        self._pending_power = None
        self._pending_brightness = None
        self._pending_appearance = None

    @callback
    def _mark_worker_stopping(self, task: asyncio.Task[None]) -> None:
        """Freeze ownership of a worker before or during its shutdown."""
        if task is not self._command_worker:
            return
        if not self._command_worker_stopping:
            self._command_worker_stopping = True
            # Requests allocated after this point belong to the successor. The old
            # worker may still be inside protected client disconnect cleanup.
            self._command_worker_cancel_cutoff = self._request_sequence

    @callback
    def _async_cancel_command_worker(self) -> None:
        """Cancel all same-lamp commands during entity unload."""
        self._entity_unloading = True
        task = self._command_worker
        if task is not None and not task.done():
            self._mark_worker_stopping(task)
            task.cancel()
        self._cancel_outstanding_requests()

    @callback
    def _command_worker_done(
        self, task: asyncio.Task[None], generation: int
    ) -> None:
        """Finish one worker generation and start a successor if required."""
        # A stale generation must never clear or otherwise mutate a successor.
        if (
            task is not self._command_worker
            or generation != self._command_worker_generation
        ):
            return

        # A task cancelled before its coroutine ever starts never reaches the
        # worker's CancelledError handler. The done callback is therefore the
        # final, idempotent cleanup guard for this generation. Use the frozen
        # cutoff so requests accepted after cancellation belong to a successor.
        if task.cancelled():
            cutoff = self._command_worker_cancel_cutoff
            if cutoff is None:
                cutoff = self._request_sequence
            owned = {
                sequence
                for sequence in self._request_waiters
                if sequence <= cutoff
            }
            self._cancel_request_sequences(owned)

        self._command_worker = None
        self._command_worker_stopping = False
        self._command_worker_cancel_cutoff = None

        if self._entity_unloading:
            return

        if self._has_pending_target() or self._request_waiters:
            self._ensure_command_worker()

    @callback
    def _ensure_command_worker(self) -> None:
        """Start the per-lamp latest-wins worker if necessary."""
        if self._entity_unloading:
            return

        task = self._command_worker
        if task is not None:
            # The current generation owns the slot until its done callback has
            # completed cleanup, even if asyncio already reports the task done.
            # This prevents a successor from starting between task completion and
            # generation handoff.
            if (
                (task.cancelling() or task.cancelled())
                and not self._command_worker_stopping
            ):
                self._mark_worker_stopping(task)
            return

        self._command_worker_generation += 1
        generation = self._command_worker_generation
        self._command_worker_stopping = False
        self._command_worker_cancel_cutoff = None
        task = self.hass.async_create_task(
            self._async_command_worker(generation),
            f"AwoX Connect.Z command worker {self._mac}",
        )
        self._command_worker = task
        task.add_done_callback(
            lambda completed, generation=generation: self._command_worker_done(
                completed, generation
            )
        )

    def _new_request(self) -> tuple[int, asyncio.Future[None]]:
        """Allocate one request generation and completion future."""
        task = self._command_worker
        if (
            task is not None
            and (task.cancelling() or task.cancelled())
            and not self._command_worker_stopping
        ):
            # Detect even a direct task.cancel() not initiated by our own helpers.
            # The cutoff is recorded before the new sequence is allocated.
            self._mark_worker_stopping(task)

        self._request_sequence += 1
        sequence = self._request_sequence
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._request_waiters[sequence] = waiter
        return sequence, waiter

    def _pending_sequences(self) -> set[int]:
        """Return request generations still represented by pending state."""
        sequences: set[int] = set()
        if self._pending_power is not None:
            sequences.add(self._pending_power.sequence)
        if self._pending_brightness is not None:
            sequences.add(self._pending_brightness.sequence)
        if self._pending_appearance is not None:
            sequences.add(self._pending_appearance.sequence)
        return sequences

    @callback
    def _resolve_completed_waiters(self) -> None:
        """Resolve requests that are neither pending nor currently in flight."""
        active = self._pending_sequences() | self._inflight_sequences
        for sequence, waiter in tuple(self._request_waiters.items()):
            if waiter.done():
                self._request_waiters.pop(sequence, None)
                continue
            if sequence not in active:
                waiter.set_result(None)
                self._request_waiters.pop(sequence, None)

    @callback
    def _fail_waiters(
        self, sequences: set[int], err: Exception
    ) -> None:
        """Fail only callers belonging to the failed BLE command."""
        for sequence in sequences:
            waiter = self._request_waiters.pop(sequence, None)
            if waiter is not None and not waiter.done():
                waiter.set_exception(err)

    @callback
    def _drop_pending_sequences(self, sequences: set[int]) -> None:
        """Drop only pending state that belongs to the supplied requests."""
        if (
            self._pending_power is not None
            and self._pending_power.sequence in sequences
        ):
            self._pending_power = None
        if (
            self._pending_brightness is not None
            and self._pending_brightness.sequence in sequences
        ):
            self._pending_brightness = None
        if (
            self._pending_appearance is not None
            and self._pending_appearance.sequence in sequences
        ):
            self._pending_appearance = None

    @callback
    def _cancel_request(self, sequence: int) -> None:
        """Remove only unsent state owned by one cancelled caller."""
        self._request_waiters.pop(sequence, None)
        self._drop_pending_sequences({sequence})
        self._resolve_completed_waiters()

        # If that caller owned the final not-yet-selected target, stop a worker
        # that may still be waiting for command capacity, the connect gate, BLE
        # connection or authentication. Never cancel an already-selected command:
        # once _inflight_sequences is populated its bounded cleanup/retry remains
        # responsible for the physical operation.
        task = self._command_worker
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
            and not self._has_pending_target()
            and not self._inflight_sequences
        ):
            self._mark_worker_stopping(task)
            task.cancel()

    async def _async_wait_for_request(
        self, sequence: int, waiter: asyncio.Future[None]
    ) -> None:
        """Wait for one request and remove its unsent targets on cancellation."""
        try:
            await waiter
        except asyncio.CancelledError:
            self._cancel_request(sequence)
            raise

    def _has_pending_target(self) -> bool:
        """Return whether any not-yet-started target remains."""
        return (
            self._pending_power is not None
            or self._pending_brightness is not None
            or self._pending_appearance is not None
        )

    def _pending_requires_power_on_settle(self) -> bool:
        """Return whether the newest waiting target can turn the lamp on."""
        power = self._pending_power
        if power is not None and power.value is False:
            # A newer explicit off remains safe to send immediately.
            return False
        return (
            (power is not None and power.value is True)
            or self._pending_brightness is not None
            or self._pending_appearance is not None
        )

    def _power_on_settle_remaining(self) -> float:
        """Return remaining post-off guard time for the current pending target."""
        if not self._pending_requires_power_on_settle():
            return 0.0

        remaining = (
            self._power_off_settle_until - asyncio.get_running_loop().time()
        )
        if remaining <= 0:
            self._power_off_settle_until = 0.0
            return 0.0
        return remaining

    async def _async_wait_for_power_off_settle(self) -> None:
        """Let a successfully written off command settle before turning on again."""
        remaining = self._power_on_settle_remaining()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _async_command_worker(self, generation: int) -> None:
        """Execute one BLE command at a time, selecting only when ready to write."""
        try:
            while self._request_waiters or self._has_pending_target():
                self._resolve_completed_waiters()

                if not self._has_pending_target():
                    self._resolve_completed_waiters()
                    break

                self._inflight_sequences.clear()
                self._inflight_command = None

                # A hardware-confirmed compatibility guard for rapid OFF -> ON
                # transitions. It does not debounce ordinary commands and does
                # not apply when a waiting OFF was coalesced away before write.
                await self._async_wait_for_power_off_settle()
                if not self._has_pending_target():
                    self._resolve_completed_waiters()
                    break

                try:
                    sent_command = await self._client.async_send_selected(
                        self._select_next_pending_command
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    # If a command was selected, fail only the request
                    # generation(s) that actually reached the write stage.
                    # If connection preparation itself failed before selection,
                    # fail the currently pending target so the worker cannot loop
                    # forever on an unreachable lamp.
                    failed_sequences = (
                        set(self._inflight_sequences)
                        if self._inflight_sequences
                        else self._pending_sequences()
                    )
                    self._inflight_sequences.clear()
                    self._inflight_command = None
                    self._drop_pending_sequences(failed_sequences)
                    self._fail_waiters(failed_sequences, err)
                    self._resolve_completed_waiters()
                    continue

                if sent_command:
                    self._apply_inflight_success()

                self._inflight_sequences.clear()
                self._inflight_command = None
                self._resolve_completed_waiters()

                if not sent_command and not self._has_pending_target():
                    break
        except asyncio.CancelledError:
            # Cancellation is asynchronous: while the client finishes protected
            # disconnect cleanup, newer requests may already arrive. Cancel only
            # the generations owned by this worker at the moment stopping began.
            if generation == self._command_worker_generation:
                cutoff = self._command_worker_cancel_cutoff
                if cutoff is None:
                    # Covers a direct task.cancel(). If a newer request arrived
                    # during cleanup, _new_request recorded the cutoff beforehand.
                    cutoff = self._request_sequence
                owned = {
                    sequence
                    for sequence in self._request_waiters
                    if sequence <= cutoff
                }
                self._cancel_request_sequences(owned)
            raise
        except Exception as err:
            # No unexpected worker failure may orphan requests. The normal BLE
            # failures above are handled per generation; this is a final guard for
            # errors in the worker itself.
            remaining = set(self._request_waiters)
            self._fail_waiters(remaining, err)
            self._pending_power = None
            self._pending_brightness = None
            self._pending_appearance = None
            self._inflight_sequences.clear()
            self._inflight_command = None
            raise
        finally:
            # Lifecycle/reference handoff happens in _command_worker_done(), after
            # this task is fully complete. This worker only clears its own in-flight
            # command; pending targets for a successor remain intact.
            if generation == self._command_worker_generation:
                self._inflight_sequences.clear()
                self._inflight_command = None

    def _select_next_pending_command(self) -> tuple[bytes, str] | None:
        """Select and consume exactly one command at the write boundary.

        This callback is invoked by the client only after the account semaphore,
        same-lamp lock and authenticated BLE connection are ready. No await occurs
        after this selection before the client starts the GATT write.
        """
        self._inflight_sequences.clear()
        self._inflight_command = None

        # Re-check the post-off hardware guard at the actual write boundary.
        # The pending target may have changed while async_send_selected() waited
        # for command capacity, the per-lamp lock, connection or authentication.
        # Returning None consumes nothing; the worker loops, waits the remaining
        # guard time, then re-enters late selection with the newest target.
        if self._power_on_settle_remaining() > 0:
            return None

        # Off is an explicit barrier and wins over older waiting state.
        power = self._pending_power
        if power is not None and power.value is False:
            self._pending_power = None
            command = _InFlightCommand(
                kind="power_off",
                sequences={power.sequence},
            )
            self._inflight_sequences = set(command.sequences)
            self._inflight_command = command
            return (
                make_power(False, mesh_id=self._mesh_id),
                "power:off",
            )

        # Brightness is its own turn-on path. A waiting plain "on" can be
        # satisfied by this same brightness write.
        brightness = self._pending_brightness
        if brightness is not None:
            self._pending_brightness = None
            sequences = {brightness.sequence}

            power = self._pending_power
            if power is not None and power.value is True:
                self._pending_power = None
                sequences.add(power.sequence)

            command = _InFlightCommand(
                kind="brightness",
                sequences=sequences,
                value=brightness.value,
            )
            self._inflight_sequences = set(command.sequences)
            self._inflight_command = command
            return (
                make_brightness(
                    ha_brightness_to_device(brightness.value),
                    brightness.transition,
                    mesh_id=self._mesh_id,
                ),
                f"brightness:{brightness.value}",
            )

        appearance = self._pending_appearance
        if appearance is not None and not self._attr_is_on:
            # The appearance request owns this prerequisite. Do not consume the
            # appearance itself; after power-on succeeds the pending target is
            # re-evaluated before any color/CT write starts.
            command = _InFlightCommand(
                kind="power_on_for_appearance",
                sequences={appearance.sequence},
            )
            self._inflight_sequences = set(command.sequences)
            self._inflight_command = command
            return (
                make_power(True, mesh_id=self._mesh_id),
                "power:on-for-appearance",
            )

        # A plain on request remains independent from an appearance request.
        power = self._pending_power
        if power is not None and power.value is True:
            self._pending_power = None
            if not self._attr_is_on:
                command = _InFlightCommand(
                    kind="power_on",
                    sequences={power.sequence},
                )
                self._inflight_sequences = set(command.sequences)
                self._inflight_command = command
                return (
                    make_power(True, mesh_id=self._mesh_id),
                    "power:on",
                )
            # Already on: the request is satisfied without a BLE write.

        appearance = self._pending_appearance
        if appearance is not None:
            self._pending_appearance = None

            if appearance.color_mode is ColorMode.HS:
                hue, saturation = appearance.value
                value = (float(hue), float(saturation))
                command = _InFlightCommand(
                    kind="hs",
                    sequences={appearance.sequence},
                    value=value,
                )
                self._inflight_sequences = set(command.sequences)
                self._inflight_command = command
                return (
                    make_hs_color(
                        value[0],
                        value[1],
                        appearance.transition,
                        mesh_id=self._mesh_id,
                    ),
                    f"hs:{value[0]:.1f},{value[1]:.1f}",
                )

            kelvin = int(appearance.value)
            command = _InFlightCommand(
                kind="color_temp",
                sequences={appearance.sequence},
                value=kelvin,
            )
            self._inflight_sequences = set(command.sequences)
            self._inflight_command = command
            return (
                make_color_temp_kelvin(
                    kelvin,
                    appearance.transition,
                    mesh_id=self._mesh_id,
                ),
                f"color_temp:{kelvin}K",
            )

        return None

    @callback
    def _apply_inflight_success(self) -> None:
        """Apply optimistic state only after the selected GATT command succeeds."""
        command = self._inflight_command
        if command is None:
            return

        if command.kind == "power_off":
            self._attr_is_on = False
            self._power_off_settle_until = (
                asyncio.get_running_loop().time() + POWER_OFF_SETTLE_SECONDS
            )
        elif command.kind in {"power_on", "power_on_for_appearance"}:
            self._attr_is_on = True
        elif command.kind == "brightness":
            self._attr_brightness = int(command.value)
            self._attr_is_on = True
        elif command.kind == "hs":
            hue, saturation = command.value
            self._attr_hs_color = (float(hue), float(saturation))
            self._attr_color_mode = ColorMode.HS
            self._attr_is_on = True
        elif command.kind == "color_temp":
            self._attr_color_temp_kelvin = int(command.value)
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_is_on = True

        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Queue the newest on/brightness/appearance target for this lamp."""
        transition = float(
            kwargs.get(ATTR_TRANSITION, self._client.default_transition)
        )
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        hs_color = kwargs.get(ATTR_HS_COLOR)
        color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)

        sequence, waiter = self._new_request()

        has_appearance = hs_color is not None or color_temp_kelvin is not None

        # Any later on/brightness/appearance request supersedes a still-waiting off.
        # Only a plain "on" needs its own pending power slot. Brightness is already
        # a turn-on path, and appearance carries its own explicit power prerequisite.
        if (
            self._pending_power is not None
            and self._pending_power.value is False
        ):
            self._pending_power = None

        if brightness is None and not has_appearance:
            self._pending_power = _PendingPower(True, sequence)

        if brightness is not None:
            self._pending_brightness = _PendingBrightness(
                value=int(brightness),
                transition=transition,
                sequence=sequence,
            )

        if hs_color is not None:
            hue, saturation = hs_color
            self._pending_appearance = _PendingAppearance(
                color_mode=ColorMode.HS,
                value=(float(hue), float(saturation)),
                transition=transition,
                sequence=sequence,
            )
        elif color_temp_kelvin is not None:
            kelvin = max(
                MIN_COLOR_TEMP_KELVIN,
                min(MAX_COLOR_TEMP_KELVIN, int(color_temp_kelvin)),
            )
            self._pending_appearance = _PendingAppearance(
                color_mode=ColorMode.COLOR_TEMP,
                value=kelvin,
                transition=transition,
                sequence=sequence,
            )

        # Superseded requests that are not currently in flight can complete now.
        self._resolve_completed_waiters()
        self._ensure_command_worker()
        await self._async_wait_for_request(sequence, waiter)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Queue off and discard older waiting brightness/appearance changes."""
        sequence, waiter = self._new_request()

        self._pending_power = _PendingPower(False, sequence)
        self._pending_brightness = None
        self._pending_appearance = None

        # Older requests cleared by this off can complete, unless one of their
        # BLE commands is already in flight.
        self._resolve_completed_waiters()
        self._ensure_command_worker()
        await self._async_wait_for_request(sequence, waiter)
