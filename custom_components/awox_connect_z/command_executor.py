"""Execute manager-selected AwoX commands through the BLE client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .client import (
    AwoxConnectZClient,
    AwoxConnectZError,
    RuntimeCommandResult,
    RuntimeCommandStatus,
    RuntimeSelectedCommand,
)
from .command_manager import (
    CommandManager,
    CommandNotReadyError,
    WriteFailureAction,
)
from .command_sequences import CommandKind, PlannedCommand
from .const import EFFECT_CANDLE, EFFECT_COLOR_CYCLE
from .protocol import (
    ha_brightness_to_device,
    make_brightness,
    make_candle_effect,
    make_color_cycle_start,
    make_color_cycle_stop,
    make_color_temp_kelvin,
    make_hs_color,
    make_power,
)


class CommandExecutorError(AwoxConnectZError):
    """Base class for command-execution failures."""


class CommandExecutionFailed(CommandExecutorError):
    """A command request was definitively aborted by the execution pipeline."""

    def __init__(
        self,
        message: str,
        *,
        failed_generations: frozenset[int] = frozenset(),
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.failed_generations = failed_generations
        self.uncertain = bool(uncertain)


class UnsupportedEffectError(CommandExecutorError):
    """Raised when an effect cannot be mapped to an AwoX protocol frame."""


class EffectIdentityRequiredError(CommandExecutorError):
    """Raised when EFFECT_STOP has no effect identity."""


class CommandSelectionChangedError(CommandExecutorError):
    """Raised if selection changes inside the synchronous write boundary."""


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    """Protocol representation of one logical command."""

    command: PlannedCommand
    frame: bytes
    label: str


class CommandExecutor:
    """Bridge CommandManager, protocol frame builders and BLE transport.

    Planning remains entirely in ``command_sequences.py`` / ``command_manager.py``.
    The executor waits for manager spacing, lets the client prepare connection and
    authentication, late-selects the still-current command immediately before the
    GATT write, and reports the structured result back to the manager.
    """

    def __init__(
        self,
        client: AwoxConnectZClient,
        manager: CommandManager,
        *,
        mesh_id: int,
        default_transition: float | None = None,
    ) -> None:
        self._client = client
        self._manager = manager
        self._mesh_id = int(mesh_id)
        self._default_transition = (
            float(client.default_transition)
            if default_transition is None
            else float(default_transition)
        )
        self._execution_lock = asyncio.Lock()

    @property
    def manager(self) -> CommandManager:
        return self._manager

    @property
    def client(self) -> AwoxConnectZClient:
        return self._client

    async def async_execute_next(self) -> PlannedCommand | None:
        """Execute the next logical command, including manager-approved retries.

        Returns the successfully written logical command, or ``None`` if the
        pending target disappeared before the final write boundary.
        """
        async with self._execution_lock:
            while True:
                candidate = self._manager.peek_next_command()
                if candidate is None:
                    return None

                wait_cutoff = self._manager.request_generation
                try:
                    await self._async_wait_for_manager_spacing()
                except asyncio.CancelledError:
                    self._manager.abort_pending_through(wait_cutoff)
                    raise

                # A newer request may have completed/superseded all work while
                # spacing elapsed. Avoid a needless BLE connection in that case.
                if self._manager.peek_next_command() is None:
                    return None

                preparation_cutoff = self._manager.request_generation
                selected: PreparedCommand | None = None

                def selector() -> RuntimeSelectedCommand | None:
                    nonlocal selected

                    current = self._manager.peek_next_command()
                    if current is None:
                        return None
                    if self._manager.delay_remaining_s() > 0.0:
                        return None

                    # Build before take_next_command(). A frame-construction error
                    # therefore never leaves a phantom in-flight manager command.
                    prepared = self._prepare_command(current)
                    try:
                        taken = self._manager.take_next_command(require_ready=True)
                    except CommandNotReadyError:
                        return None

                    if taken is None:
                        return None
                    if taken != current:
                        raise CommandSelectionChangedError(
                            "Manager selection changed inside the synchronous "
                            "write-boundary step"
                        )

                    selected = PreparedCommand(
                        command=taken,
                        frame=prepared.frame,
                        label=prepared.label,
                    )
                    attempt = self._manager.in_flight_attempt or 1
                    return RuntimeSelectedCommand(
                        frame=selected.frame,
                        label=selected.label,
                        attempt=attempt,
                    )

                try:
                    result = await self._client.async_send_selected_once(selector)
                except asyncio.CancelledError:
                    if selected is None:
                        self._manager.abort_pending_through(preparation_cutoff)
                    else:
                        self._manager.mark_write_failed(uncertain=True)
                    raise

                outcome = self._handle_result(
                    result,
                    selected=selected,
                    preparation_cutoff=preparation_cutoff,
                )
                if outcome is not None:
                    return outcome

                # RETRY/REPLAN/NOT_SELECTED: loop, re-check spacing and select the
                # current command again. No old frame is retained across attempts.

    async def async_execute_until_idle(self) -> tuple[PlannedCommand, ...]:
        """Execute commands until the manager has no immediately planned work."""
        completed: list[PlannedCommand] = []
        while self._manager.peek_next_command() is not None:
            command = await self.async_execute_next()
            if command is None:
                break
            completed.append(command)
        return tuple(completed)

    async def _async_wait_for_manager_spacing(self) -> None:
        while True:
            remaining = self._manager.delay_remaining_s()
            if remaining <= 0.0:
                return
            await asyncio.sleep(remaining)

    def _handle_result(
        self,
        result: RuntimeCommandResult,
        *,
        selected: PreparedCommand | None,
        preparation_cutoff: int,
    ) -> PlannedCommand | None:
        status = result.status

        if status is RuntimeCommandStatus.NOT_SELECTED:
            if selected is not None:
                raise CommandExecutorError(
                    "Client reported NOT_SELECTED after manager selection"
                )
            return None

        if status is RuntimeCommandStatus.PREPARATION_FAILED:
            if selected is not None:
                raise CommandExecutorError(
                    "Client reported preparation failure after manager selection"
                )
            failed = self._manager.abort_pending_through(preparation_cutoff)
            error = result.error or AwoxConnectZError(
                "AwoX command preparation failed"
            )
            raise CommandExecutionFailed(
                str(error),
                failed_generations=failed,
            ) from error

        if selected is None:
            raise CommandExecutorError(
                f"Client returned {status.value} without a selected command"
            )

        finished_at = result.write_finished_monotonic

        if status is RuntimeCommandStatus.SUCCESS:
            self._manager.mark_write_succeeded(finished_at=finished_at)
            return selected.command

        if status in {
            RuntimeCommandStatus.WRITE_FAILED,
            RuntimeCommandStatus.WRITE_FAILED_FINAL,
        }:
            decision = self._manager.mark_write_failed(
                finished_at=finished_at,
                uncertain=False,
                retry_allowed=status is RuntimeCommandStatus.WRITE_FAILED,
            )
            if decision.action in {
                WriteFailureAction.RETRY,
                WriteFailureAction.REPLAN,
            }:
                return None
            error = result.error or AwoxConnectZError(
                f"Command '{selected.label}' failed"
            )
            raise CommandExecutionFailed(
                str(error),
                failed_generations=decision.failed_generations,
            ) from error

        if status is RuntimeCommandStatus.WRITE_UNCERTAIN:
            decision = self._manager.mark_write_failed(
                finished_at=finished_at,
                uncertain=True,
            )
            error = result.error or AwoxConnectZError(
                f"Command '{selected.label}' has an uncertain write outcome"
            )
            raise CommandExecutionFailed(
                str(error),
                failed_generations=decision.failed_generations,
                uncertain=True,
            ) from error

        raise CommandExecutorError(f"Unsupported client result: {status!r}")

    def _prepare_command(self, command: PlannedCommand) -> PreparedCommand:
        kind = command.kind
        transition = (
            self._default_transition
            if command.transition is None
            else float(command.transition)
        )

        if kind is CommandKind.POWER_ON:
            return PreparedCommand(
                command,
                make_power(True, mesh_id=self._mesh_id),
                "power:on",
            )

        if kind is CommandKind.POWER_OFF:
            return PreparedCommand(
                command,
                make_power(False, mesh_id=self._mesh_id),
                "power:off",
            )

        if kind is CommandKind.BRIGHTNESS_ON:
            if command.value is None:
                raise CommandExecutorError("BRIGHTNESS_ON requires a value")
            brightness = int(command.value)
            return PreparedCommand(
                command,
                make_brightness(
                    ha_brightness_to_device(brightness),
                    transition,
                    mesh_id=self._mesh_id,
                ),
                f"brightness:{brightness}",
            )

        if kind is CommandKind.COLOR:
            if command.value is None:
                raise CommandExecutorError("COLOR requires an HS value")
            try:
                hue, saturation = command.value
            except (TypeError, ValueError) as err:
                raise CommandExecutorError(
                    f"Invalid COLOR value: {command.value!r}"
                ) from err
            hue = float(hue)
            saturation = float(saturation)
            return PreparedCommand(
                command,
                make_hs_color(
                    hue,
                    saturation,
                    transition,
                    mesh_id=self._mesh_id,
                ),
                f"hs:{hue:.1f},{saturation:.1f}",
            )

        if kind is CommandKind.CCT:
            if command.value is None:
                raise CommandExecutorError("CCT requires a Kelvin value")
            kelvin = int(command.value)
            return PreparedCommand(
                command,
                make_color_temp_kelvin(
                    kelvin,
                    transition,
                    mesh_id=self._mesh_id,
                ),
                f"color_temp:{kelvin}K",
            )

        if kind is CommandKind.EFFECT_START:
            effect = self._require_effect(command.value, for_stop=False)
            if effect == EFFECT_COLOR_CYCLE:
                frame = make_color_cycle_start(mesh_id=self._mesh_id)
                label = "effect:color_cycle:start"
            else:
                frame = make_candle_effect(True, mesh_id=self._mesh_id)
                label = "effect:candle:start"
            return PreparedCommand(command, frame, label)

        if kind is CommandKind.EFFECT_STOP:
            effect = self._require_effect(command.value, for_stop=True)
            if effect == EFFECT_COLOR_CYCLE:
                frame = make_color_cycle_stop(mesh_id=self._mesh_id)
                label = "effect:color_cycle:stop"
            else:
                frame = make_candle_effect(False, mesh_id=self._mesh_id)
                label = "effect:candle:stop"
            return PreparedCommand(command, frame, label)

        raise CommandExecutorError(f"Unsupported command kind: {kind!r}")

    @staticmethod
    def _require_effect(value: object | None, *, for_stop: bool) -> str:
        if value is None:
            if for_stop:
                raise EffectIdentityRequiredError(
                    "EFFECT_STOP requires an effect identity"
                )
            raise UnsupportedEffectError("EFFECT_START requires an effect identity")

        effect = str(value)
        normalized = effect.strip().casefold().replace("-", "_").replace(" ", "_")
        if effect == EFFECT_COLOR_CYCLE or normalized == "color_cycle":
            return EFFECT_COLOR_CYCLE
        if effect == EFFECT_CANDLE or normalized == "candle":
            return EFFECT_CANDLE
        raise UnsupportedEffectError(f"Unsupported AwoX native effect: {effect!r}")


__all__ = [
    "CommandExecutionFailed",
    "CommandExecutor",
    "CommandExecutorError",
    "CommandSelectionChangedError",
    "EffectIdentityRequiredError",
    "PreparedCommand",
    "UnsupportedEffectError",
]
