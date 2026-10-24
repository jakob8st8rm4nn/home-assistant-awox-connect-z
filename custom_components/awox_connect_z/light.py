"""Light entities for AwoX Connect.Z."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    EFFECT_OFF,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .advertisement import AwoxAdvertisementState
from .client import AwoxConnectZClient
from .command_executor import CommandExecutionFailed, CommandExecutor
from .command_manager import CommandManager
from .command_sequences import (
    PlannerState,
    SequenceConflictError,
    TargetState,
    UnknownPlannerStateError,
)
from .const import (
    CONF_COMMAND_INTERVAL_MS,
    DEFAULT_COMMAND_INTERVAL_MS,
    DOMAIN,
    MAX_COLOR_TEMP_KELVIN,
    MAX_COMMAND_INTERVAL_MS,
    MIN_COLOR_TEMP_KELVIN,
    MIN_COMMAND_INTERVAL_MS,
    NATIVE_EFFECTS,
)


def planner_state_from_advertisement(state: AwoxAdvertisementState) -> PlannerState:
    """Convert decoded hardware state into the planner's authoritative state.

    The sequencer requires both power and effect identity to be interpretable.
    Unknown advertisement modes therefore make the complete planner state
    UNKNOWN rather than pretending that no effect is active.
    """
    if not state.effect_state_known:
        return PlannerState(known=False, is_on=None, effect=None)
    return PlannerState(known=True, is_on=state.is_on, effect=state.effect)


# Home Assistant's platform semaphore is static. The integration uses the
# per-config-entry semaphore owned by AwoxConnectZClient instead.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one light entity per imported account device."""
    clients: list[tuple[AwoxConnectZClient, dict]] = hass.data[DOMAIN][entry.entry_id]
    interval_ms = max(
        MIN_COMMAND_INTERVAL_MS,
        min(
            MAX_COMMAND_INTERVAL_MS,
            int(entry.options.get(CONF_COMMAND_INTERVAL_MS, DEFAULT_COMMAND_INTERVAL_MS)),
        ),
    )
    async_add_entities(
        [
            AwoxConnectZLight(
                client,
                device,
                minimum_command_interval_ms=interval_ms,
            )
            for client, device in clients
        ]
    )


class AwoxConnectZLight(LightEntity, RestoreEntity):
    """AwoX Connect.Z light using the planner/manager/executor command pipeline."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_supported_features = (
        LightEntityFeature.TRANSITION | LightEntityFeature.EFFECT
    )
    _attr_effect_list = [EFFECT_OFF, *NATIVE_EFFECTS]
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
    _attr_should_poll = False

    def __init__(
        self,
        client: AwoxConnectZClient,
        device: dict[str, Any],
        *,
        minimum_command_interval_ms: int,
    ) -> None:
        self._client = client
        self._device = device
        self._mac = client.mac
        self._mesh_id = int(device["mesh_id"])
        self._device_name = str(device.get("name") or f"AwoX {self._mac[-8:]}")

        self._manager = CommandManager(
            PlannerState(known=False, is_on=None, effect=None),
            minimum_interval_ms=minimum_command_interval_ms,
        )
        self._executor = CommandExecutor(
            client,
            self._manager,
            mesh_id=self._mesh_id,
        )

        self._command_worker: asyncio.Task[None] | None = None
        self._entity_unloading = False
        self._last_ble_connected = client.connected
        self._request_waiters: dict[int, asyncio.Future[None]] = {}
        self._initial_status_lock = asyncio.Lock()

        self._attr_unique_id = self._mac.replace(":", "").lower()
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_color_temp_kelvin = 4000
        self._attr_hs_color = (0.0, 100.0)
        self._attr_effect = EFFECT_OFF
        self._effect_state_known = False

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
            "effect_state_known": self._effect_state_known,
            "planner_state_known": self._manager.state.known,
            "command_interval_ms": self._manager.minimum_interval_ms,
        }

    async def async_added_to_hass(self) -> None:
        """Restore display state, then prefer fresh hardware advertisements."""
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
        self._last_ble_connected = self._client.connected
        self.async_on_remove(
            self._client.async_add_diagnostic_listener(self._async_connection_changed)
        )
        # 1911 replies remain in the session store for confirmation/UNKNOWN lookup.
        # Only their explicit successful result is applied, never every notification.
        self._entity_unloading = False
        self.async_on_remove(self._async_cancel_command_worker)

        if self._client.advertisement_state is not None:
            self._async_apply_advertisement_state(self._client.advertisement_state)

    @callback
    def _async_availability_changed(self, available: bool) -> None:
        self._last_ble_connected = self._client.connected
        if self._executor.busy and available:
            return
        self.async_write_ha_state()

    @callback
    def _async_connection_changed(self) -> None:
        """Publish link changes even while light availability stays true."""
        connected = self._client.connected
        if connected != self._last_ble_connected:
            self._last_ble_connected = connected
            if self._executor.busy:
                return
            self.async_write_ha_state()

    @callback
    def _async_apply_advertisement_state(
        self, state: AwoxAdvertisementState
    ) -> None:
        self._async_apply_lamp_state(state)

    @callback
    def _async_apply_lamp_state(
        self, state: AwoxAdvertisementState, *, confirmed_group: bool = False
    ) -> None:
        """Apply a received lamp state outside a physical/confirmation batch."""
        if self._executor.busy:
            # Neither advertisements nor 1911 intermediate replies may replan an
            # executing group. A matching end status is applied after the barrier.
            return
        self._attr_is_on = state.is_on
        self._attr_brightness = state.brightness

        if state.effect_state_known:
            self._set_effect_state_known(state.effect)
        else:
            self._set_effect_state_unknown()

        # Only a confirmed group's final reply may update effect appearance.
        # Unsolicited effect samples continue to preserve HA's last color/CCT.
        if confirmed_group or state.effect not in NATIVE_EFFECTS:
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

        failed = self._manager.set_state(planner_state_from_advertisement(state))
        if failed:
            self._fail_waiters(
                failed,
                HomeAssistantError(
                    "AwoX lamp state became unknown; pending commands were aborted. "
                    "Wait for a valid state advertisement before sending a new request."
                ),
            )
        self._resolve_completed_waiters()
        self._ensure_command_worker()
        self.async_write_ha_state()

    @callback
    def _async_apply_status_1911(self, state, *, confirmed_group: bool = False) -> None:
        """Apply an explicit group-confirmation or UNKNOWN-bootstrap result."""
        if not self._executor.busy:
            self._async_apply_lamp_state(
                state.as_lamp_state(), confirmed_group=confirmed_group
            )

    def _set_effect_state_known(self, effect: str | None) -> None:
        self._effect_state_known = True
        self._attr_effect = str(effect) if effect in NATIVE_EFFECTS else EFFECT_OFF

    def _set_effect_state_unknown(self) -> None:
        self._effect_state_known = False

    @callback
    def _resolve_completed_waiters(self) -> None:
        active = self._manager.pending_generations
        for generation, waiter in tuple(self._request_waiters.items()):
            if waiter.done():
                self._request_waiters.pop(generation, None)
                continue
            if generation not in active:
                waiter.set_result(None)
                self._request_waiters.pop(generation, None)

    @callback
    def _fail_waiters(
        self,
        generations: frozenset[int] | set[int],
        error: Exception,
    ) -> None:
        for generation in generations:
            waiter = self._request_waiters.pop(generation, None)
            if waiter is not None and not waiter.done():
                waiter.set_exception(error)

    async def _async_wait_for_request(
        self,
        generation: int,
        waiter: asyncio.Future[None],
    ) -> None:
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            self._request_waiters.pop(generation, None)
            self._manager.cancel_generation(generation)
            self._resolve_completed_waiters()
            self._ensure_command_worker()
            raise

    @callback
    def _ensure_command_worker(self) -> None:
        if self._entity_unloading:
            return
        if self._command_worker is not None:
            return
        if self._manager.peek_next_command() is None:
            return

        task = self.hass.async_create_task(
            self._async_command_worker(),
            f"AwoX Connect.Z command worker {self._mac}",
        )
        self._command_worker = task
        task.add_done_callback(self._command_worker_done)

    @callback
    def _command_worker_done(self, task: asyncio.Task[None]) -> None:
        if task is not self._command_worker:
            return
        self._command_worker = None

        if not task.cancelled():
            with suppress(Exception):
                task.result()

        if self._entity_unloading:
            return

        self._resolve_completed_waiters()
        if self._manager.peek_next_command() is not None:
            self._ensure_command_worker()

    @callback
    def _async_cancel_command_worker(self) -> None:
        self._entity_unloading = True
        self._manager.clear_pending()

        task = self._command_worker
        if task is not None and not task.done():
            task.cancel()

        for waiter in self._request_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._request_waiters.clear()

    async def _async_command_worker(self) -> None:
        """Drain manager-planned work through the executor."""
        while not self._entity_unloading:
            self._resolve_completed_waiters()
            if self._manager.peek_next_command() is None:
                return

            try:
                commands = await self._executor.async_execute_group()
            except asyncio.CancelledError:
                raise
            except CommandExecutionFailed as err:
                if err.uncertain:
                    self._set_effect_state_unknown()
                    self.async_write_ha_state()
                self._fail_waiters(err.failed_generations, err)
                self._resolve_completed_waiters()
                # Newer requests may have survived the failed generation.
                if self._manager.peek_next_command() is None:
                    return
                continue
            except Exception as err:
                # Programming/protocol errors are not safe to leave queued.
                failed = self._manager.pending_generations
                self._manager.clear_pending()
                wrapped = HomeAssistantError(
                    f"AwoX command execution failed: {err}"
                )
                self._fail_waiters(failed, wrapped)
                return

            if not commands:
                self._resolve_completed_waiters()
                if self._manager.peek_next_command() is None:
                    return
                continue

            if self._executor.last_confirmed_state is not None:
                self._async_apply_status_1911(
                    self._executor.last_confirmed_state, confirmed_group=True
                )
            self._resolve_completed_waiters()

    async def _async_submit_target(self, target: TargetState) -> None:
        """Submit one HA request and wait until its generation completes."""
        if self._entity_unloading:
            raise HomeAssistantError("AwoX entity is unloading")
        if not self._manager.state.known:
            async with self._initial_status_lock:
                if not self._manager.state.known:
                    state = await self._executor.async_initial_status()
                    self._async_apply_status_1911(state)
        if self._entity_unloading:
            raise HomeAssistantError("AwoX entity unloaded during initial-state lookup")
        previous_generation = self._manager.request_generation
        try:
            self._manager.submit(target)
        except UnknownPlannerStateError as err:
            raise HomeAssistantError(
                "AwoX lamp state is not known yet; wait for a valid state advertisement"
            ) from err
        except SequenceConflictError as err:
            raise HomeAssistantError(str(err)) from err

        generation = self._manager.request_generation
        if generation == previous_generation:
            # Reserved for callers that complete without accepting a generation.
            return

        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._request_waiters[generation] = waiter
        self._resolve_completed_waiters()
        self._ensure_command_worker()

        # A no-op can have completed during submit/replan before the waiter was
        # inserted; resolve once more after insertion.
        self._resolve_completed_waiters()
        if waiter.done():
            await waiter
            return

        await self._async_wait_for_request(generation, waiter)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Submit one combined Home Assistant ON target."""
        transition = float(
            kwargs.get(ATTR_TRANSITION, self._client.default_transition)
        )
        brightness_raw = kwargs.get(ATTR_BRIGHTNESS)
        brightness = int(brightness_raw) if brightness_raw is not None else None
        hs_color = kwargs.get(ATTR_HS_COLOR)
        color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        effect_requested = ATTR_EFFECT in kwargs
        effect_value = str(kwargs[ATTR_EFFECT]) if effect_requested else None

        if effect_requested and effect_value not in self._attr_effect_list:
            raise HomeAssistantError(
                f"Unsupported AwoX Connect.Z effect: {effect_value}"
            )

        color: tuple[float, float] | None = None
        if hs_color is not None:
            color = (float(hs_color[0]), float(hs_color[1]))

        cct: int | None = None
        if color_temp_kelvin is not None:
            cct = max(
                MIN_COLOR_TEMP_KELVIN,
                min(MAX_COLOR_TEMP_KELVIN, int(color_temp_kelvin)),
            )

        effect: str | None = None
        stop_effect = False
        if effect_requested:
            if effect_value == EFFECT_OFF:
                stop_effect = True
            else:
                effect = effect_value

        positive_requires_on = (
            (brightness is not None and brightness > 0)
            or color is not None
            or cct is not None
            or effect is not None
        )
        power = None if positive_requires_on else True

        await self._async_submit_target(
            TargetState(
                power=power,
                brightness=brightness,
                brightness_transition=(
                    transition if brightness is not None else None
                ),
                color=color,
                cct=cct,
                appearance_transition=(
                    transition if color is not None or cct is not None else None
                ),
                effect=effect,
                stop_effect=stop_effect,
            )
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Submit Power OFF; POWER OFF itself clears an active effect."""
        await self._async_submit_target(TargetState(power=False))
