"""Pending-target and command-execution manager for AwoX / EGLO Connect.Z.

The manager owns latest-wins pending intent, per-lamp write spacing, retries,
request generations and the manager-level brightness-finalizer obligation. The
pure sequencer decides semantic command order and stored-effect recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from typing import Callable

from .command_sequences import (
    CommandKind,
    PlannedCommand,
    PlannerState,
    TargetState,
    UnknownPlannerStateError,
    normalize_request,
    plan_commands,
)
from .const import (
    DEFAULT_COMMAND_INTERVAL_MS,
    MAX_COMMAND_INTERVAL_MS,
    MAX_COMMAND_WRITE_ATTEMPTS,
    MIN_COMMAND_INTERVAL_MS,
)

Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ManagerSnapshot:
    """Read-only view of the manager's current logical state."""

    state: PlannerState
    pending: TargetState
    planned: tuple[PlannedCommand, ...]
    in_flight: PlannedCommand | None
    delay_remaining_s: float


@dataclass(slots=True)
class _PendingVersions:
    """Request generation that last wrote each logical pending slot."""

    power: int | None = None
    brightness: int | None = None
    appearance: int | None = None
    effect: int | None = None


class WriteFailureAction(str, Enum):
    """Manager-owned decision after one failed physical write attempt."""

    RETRY = "retry"
    REPLAN = "replan"
    ABORTED = "aborted"
    ABORTED_UNCERTAIN = "aborted_uncertain"


@dataclass(frozen=True, slots=True)
class WriteFailureResult:
    """Result of manager-owned retry/abort processing."""

    action: WriteFailureAction
    failed_generations: frozenset[int] = frozenset()


class CommandManagerError(RuntimeError):
    """Base class for command-manager errors."""


class WriteInProgressError(CommandManagerError):
    """Raised when a second write is started while one is already in flight."""


class NoWriteInProgressError(CommandManagerError):
    """Raised when a write result is reported without an in-flight command."""


class CommandNotReadyError(CommandManagerError):
    """Raised when the caller tries to start a command before spacing expires."""


class CommandManager:
    """Manage pending targets and unsent logical commands for one lamp."""

    def __init__(
        self,
        state: PlannerState,
        *,
        minimum_interval_ms: int = DEFAULT_COMMAND_INTERVAL_MS,
        max_write_attempts: int = MAX_COMMAND_WRITE_ATTEMPTS,
        clock: Clock = time.monotonic,
    ) -> None:
        minimum_interval_ms = int(minimum_interval_ms)
        if not MIN_COMMAND_INTERVAL_MS <= minimum_interval_ms <= MAX_COMMAND_INTERVAL_MS:
            raise ValueError(
                f"minimum_interval_ms must be between {MIN_COMMAND_INTERVAL_MS} "
                f"and {MAX_COMMAND_INTERVAL_MS}"
            )
        if int(max_write_attempts) < 1:
            raise ValueError("max_write_attempts must be at least 1")

        self._clock = clock
        self._minimum_interval_s = minimum_interval_ms / 1000.0
        self._max_write_attempts = int(max_write_attempts)

        self._state = state
        self._pending = TargetState()
        self._pending_versions = _PendingVersions()
        self._planned: list[PlannedCommand] = []
        self._planned_is_finalizer = False

        self._in_flight: PlannedCommand | None = None
        self._in_flight_generation: int | None = None
        self._in_flight_generation_cutoff: int | None = None
        self._in_flight_retry_cancelled = False
        self._in_flight_attempt: int | None = None
        self._in_flight_attempt_key: tuple[object, int] | None = None
        self._in_flight_is_finalizer = False

        self._last_write_finished_at: float | None = None
        self._replan_after_in_flight = False

        self._request_generation = 0
        self._write_attempts: dict[tuple[object, int], int] = {}

        # Manager-owned obligation created only by a confirmed successful
        # BRIGHTNESS_ON. While present, no non-power command may be selected.
        self._brightness_finalizer_generation: int | None = None

        # EFFECT START while OFF intentionally creates OFF+stored until its
        # already-planned POWER ON tail. Only that same uninterrupted sequence
        # may suppress stored-effect recovery.
        self._expected_stored_effect_generation: int | None = None
        self._expected_stored_effect: str | None = None

        # Explicit cancellation/abort must prevent a successful already-in-flight
        # BRIGHTNESS_ON from recreating automatic finalization afterwards.
        self._suppress_followups: set[int] = set()

        self._replan()

    @property
    def state(self) -> PlannerState:
        return self._state

    @property
    def pending(self) -> TargetState:
        return self._pending

    @property
    def planned_commands(self) -> tuple[PlannedCommand, ...]:
        return tuple(self._planned)

    @property
    def in_flight(self) -> PlannedCommand | None:
        return self._in_flight

    @property
    def minimum_interval_ms(self) -> int:
        return round(self._minimum_interval_s * 1000.0)

    @property
    def in_flight_attempt(self) -> int | None:
        return self._in_flight_attempt

    @property
    def max_write_attempts(self) -> int:
        return self._max_write_attempts

    @property
    def request_generation(self) -> int:
        return self._request_generation

    @property
    def pending_generations(self) -> frozenset[int]:
        """Return generations whose accepted request is not yet complete."""

        generations = {
            version
            for version in (
                self._pending_versions.power,
                self._pending_versions.brightness,
                self._pending_versions.appearance,
                self._pending_versions.effect,
                self._brightness_finalizer_generation,
                # A request whose logical command is already being written is
                # still unfinished even if a newer request has replaced all of
                # its pending target slots. Keep its waiter alive until the
                # physical write result is known.
                self._in_flight_generation,
            )
            if version is not None
        }
        return frozenset(generations)

    def set_minimum_interval_ms(self, value: int) -> None:
        value = int(value)
        if not MIN_COMMAND_INTERVAL_MS <= value <= MAX_COMMAND_INTERVAL_MS:
            raise ValueError(
                f"minimum interval must be between {MIN_COMMAND_INTERVAL_MS} "
                f"and {MAX_COMMAND_INTERVAL_MS} ms"
            )
        self._minimum_interval_s = value / 1000.0

    def snapshot(self, *, now: float | None = None) -> ManagerSnapshot:
        return ManagerSnapshot(
            state=self._state,
            pending=self._pending,
            planned=tuple(self._planned),
            in_flight=self._in_flight,
            delay_remaining_s=self.delay_remaining_s(now=now),
        )

    def set_state(self, state: PlannerState) -> frozenset[int]:
        """Reconcile authoritative state; UNKNOWN aborts all pending work."""

        self._state = state
        if not state.known or state.is_on is None:
            failed = self.pending_generations
            self._suppress_followups.update(failed)
            if self._in_flight_generation is not None:
                self._suppress_followups.add(self._in_flight_generation)
            self.clear_pending(_record_suppression=False)
            return failed

        if not self._matches_expected_stored_intermediate(state):
            if state.is_on or state.effect != self._expected_stored_effect:
                self._clear_expected_stored_intermediate()

        if self._in_flight is None:
            self._replan()
        else:
            self._replan_after_in_flight = True
        return frozenset()

    def clear_pending(self, *, _record_suppression: bool = True) -> None:
        """Discard all unsent intent and stop automatic follow-up work."""

        if _record_suppression:
            self._suppress_followups.update(self.pending_generations)
            if self._in_flight_generation is not None:
                self._suppress_followups.add(self._in_flight_generation)

        self._clear_pending_targets()
        self._brightness_finalizer_generation = None
        self._clear_expected_stored_intermediate()
        self._planned.clear()
        self._planned_is_finalizer = False

        if self._in_flight is not None:
            self._in_flight_retry_cancelled = True
        self._replan_after_in_flight = False
        self._prune_suppressed_followups()

    def cancel_generation(self, generation: int) -> None:
        """Cancel unsatisfied intent owned by exactly one request generation."""

        generation = int(generation)
        self._suppress_followups.add(generation)
        self._remove_generation(generation)

        if self._expected_stored_effect_generation == generation:
            self._clear_expected_stored_intermediate()
        if self._brightness_finalizer_generation == generation:
            self._brightness_finalizer_generation = None

        if self._in_flight is None:
            self._prune_suppressed_followups()
            self._replan()
            return

        self._replan_after_in_flight = True
        if self._in_flight_generation == generation:
            self._in_flight_retry_cancelled = True
        self._prune_suppressed_followups()

    def abort_pending_through(self, cutoff: int) -> frozenset[int]:
        """Abort pending request intent at or before ``cutoff``."""

        cutoff = int(cutoff)
        failed = self._pending_generations_at_or_before(cutoff)
        self._suppress_followups.update(failed)
        self._discard_pending_at_or_before(cutoff)
        self._planned.clear()
        self._planned_is_finalizer = False

        if self._in_flight is None:
            self._replan()
        else:
            self._replan_after_in_flight = True
            if (
                self._in_flight_generation is not None
                and self._in_flight_generation <= cutoff
            ):
                self._in_flight_retry_cancelled = True
        self._prune_suppressed_followups()
        return frozenset(failed)

    def submit(self, target: TargetState) -> tuple[PlannedCommand, ...]:
        """Normalize and merge one new partial request."""

        if not self._state.known or self._state.is_on is None:
            raise UnknownPlannerStateError(
                "Control request rejected because the planner state is UNKNOWN"
            )

        normalized = normalize_request(self._state, target)

        # A Stop Effect request must still supersede an older pending/in-flight
        # effect start even if the currently assumed lamp state has no active
        # effect yet. The older start may become physical before it can be
        # withdrawn; retaining Stop Effect guarantees the newer request wins.
        preserve_stop_for_older_start = (
            target.stop_effect
            and target.power is not False
            and target.brightness != 0
            and target.color is None
            and target.cct is None
            and (
                self._pending.effect is not None
                or (
                    self._in_flight is not None
                    and self._in_flight.kind is CommandKind.EFFECT_START
                )
            )
        )
        if preserve_stop_for_older_start and not normalized.stop_effect:
            normalized = TargetState(
                power=normalized.power,
                brightness=normalized.brightness,
                brightness_transition=normalized.brightness_transition,
                color=normalized.color,
                cct=normalized.cct,
                appearance_transition=normalized.appearance_transition,
                effect=normalized.effect,
                stop_effect=True,
            )

        self._request_generation += 1
        generation = self._request_generation

        # Any new accepted request ends the special exemption for an older
        # expected OFF+stored intermediate. If the old EFFECT START later
        # succeeds in flight, its generation no longer matches and recovery will
        # be used before executing the newer request.
        self._clear_expected_stored_intermediate()

        self._pending, self._pending_versions = self._merge_pending(
            self._pending,
            self._pending_versions,
            normalized,
            generation,
        )
        self._after_pending_change()
        return tuple(self._planned)

    def _after_pending_change(self) -> None:
        if self._in_flight is None:
            self._replan()
        else:
            self._replan_after_in_flight = True

    def delay_remaining_s(self, *, now: float | None = None) -> float:
        if self._last_write_finished_at is None:
            return 0.0
        current = self._clock() if now is None else float(now)
        elapsed = max(0.0, current - self._last_write_finished_at)
        return max(0.0, self._minimum_interval_s - elapsed)

    def is_ready(self, *, now: float | None = None) -> bool:
        return (
            self._in_flight is None
            and bool(self._planned)
            and self.delay_remaining_s(now=now) <= 0.0
        )

    def peek_next_command(self) -> PlannedCommand | None:
        if self._in_flight is not None or not self._planned:
            return None
        return self._planned[0]

    def take_next_command(
        self,
        *,
        now: float | None = None,
        require_ready: bool = True,
    ) -> PlannedCommand | None:
        """Move the next command into the in-flight slot."""

        if self._in_flight is not None:
            raise WriteInProgressError("a logical command is already in flight")
        if not self._planned:
            return None

        remaining = self.delay_remaining_s(now=now)
        if require_ready and remaining > 0.0:
            raise CommandNotReadyError(
                f"next command is not ready for {remaining:.6f} seconds"
            )

        command = self._planned.pop(0)
        is_finalizer = self._planned_is_finalizer
        generation = self._command_generation(command, is_finalizer=is_finalizer)

        attempt_identity: object = (
            "brightness_finalizer" if is_finalizer else command
        )
        key = (attempt_identity, generation)

        self._in_flight = command
        self._in_flight_generation = generation
        self._in_flight_generation_cutoff = self._request_generation
        self._in_flight_retry_cancelled = False
        self._in_flight_is_finalizer = is_finalizer
        self._in_flight_attempt_key = key
        self._in_flight_attempt = self._write_attempts.get(key, 0) + 1
        self._write_attempts[key] = self._in_flight_attempt

        # A finalizer plan contains exactly one command. Normal plans may have
        # more, but every successful write is replanned from the new state.
        self._planned_is_finalizer = False
        return command

    def mark_write_succeeded(self, *, finished_at: float | None = None) -> None:
        """Commit the in-flight logical command after a successful GATT write."""

        command = self._require_in_flight()
        timestamp = self._clock() if finished_at is None else float(finished_at)
        cutoff = self._in_flight_generation_cutoff
        generation = self._in_flight_generation
        is_finalizer = self._in_flight_is_finalizer
        assert cutoff is not None
        assert generation is not None

        state_before = self._state
        self._last_write_finished_at = timestamp
        if self._in_flight_attempt_key is not None:
            self._write_attempts.pop(self._in_flight_attempt_key, None)

        self._state = self._state_after_success(self._state, command)
        self._pending, self._pending_versions = self._pending_after_success(
            self._pending,
            self._pending_versions,
            command,
            cutoff,
        )

        # A successful BRIGHTNESS_ON creates the physical power-finalizer
        # obligation unless this generation was explicitly cancelled/aborted.
        if command.kind is CommandKind.BRIGHTNESS_ON:
            if generation not in self._suppress_followups:
                self._brightness_finalizer_generation = generation

        # A manager-selected power finalizer closes the obligation only after
        # the write itself succeeds.
        if is_finalizer and self._brightness_finalizer_generation == generation:
            self._brightness_finalizer_generation = None

        # Preserve the intended EFFECT START -> POWER ON intermediate only for
        # the exact uninterrupted generation that still owns the ON tail.
        if (
            command.kind is CommandKind.EFFECT_START
            and state_before.known
            and state_before.is_on is False
            and generation not in self._suppress_followups
            and generation == self._request_generation
            and self._pending.power is True
            and self._pending_versions.power == generation
        ):
            self._expected_stored_effect_generation = generation
            self._expected_stored_effect = str(command.value)

        if command.kind in {CommandKind.POWER_ON, CommandKind.POWER_OFF}:
            self._clear_expected_stored_intermediate()

        self._finish_in_flight()
        self._suppress_followups.discard(generation)
        self._prune_suppressed_followups()
        self._replan_after_in_flight = False
        self._prune_write_attempts()
        self._replan()

    def mark_write_failed(
        self,
        *,
        finished_at: float | None = None,
        uncertain: bool = False,
        retry_allowed: bool = True,
    ) -> WriteFailureResult:
        """Process one failed physical write using manager-owned retry policy."""

        command = self._require_in_flight()
        timestamp = self._clock() if finished_at is None else float(finished_at)
        cutoff = self._in_flight_generation_cutoff
        generation = self._in_flight_generation
        attempt = self._in_flight_attempt
        is_finalizer = self._in_flight_is_finalizer
        retry_cancelled = self._in_flight_retry_cancelled
        assert cutoff is not None
        assert generation is not None
        assert attempt is not None

        self._last_write_finished_at = timestamp
        self._finish_in_flight(keep_attempt_key=True)

        if uncertain:
            # _finish_in_flight() intentionally clears the active slot before
            # this branch. Explicitly retain the generation whose write outcome
            # became ambiguous so it cannot be reported as successfully
            # completed merely because a newer request superseded its targets.
            failed = frozenset((*self.pending_generations, generation))
            self._suppress_followups.update(failed)
            self._brightness_finalizer_generation = None
            self._clear_expected_stored_intermediate()
            self._clear_pending_targets()
            self._planned.clear()
            self._planned_is_finalizer = False
            self._state = PlannerState(known=False, is_on=None, effect=None)
            self._suppress_followups.discard(generation)
            self._prune_suppressed_followups()
            return WriteFailureResult(
                WriteFailureAction.ABORTED_UNCERTAIN,
                failed,
            )

        # A terminal finalizer failure belongs to its originating brightness
        # generation, not to independent newer requests accepted meanwhile.
        failure_cutoff = generation if is_finalizer else cutoff

        if not retry_allowed or (
            not retry_cancelled and attempt >= self._max_write_attempts
        ):
            failed = self._pending_generations_at_or_before(failure_cutoff)
            # The physical command's owning generation may already have been
            # superseded out of every pending slot while the write was in
            # flight. A terminal failure must still fail that transaction.
            failed.add(generation)
            self._suppress_followups.update(failed)
            self._discard_pending_at_or_before(failure_cutoff)
            self._planned.clear()
            self._planned_is_finalizer = False
            self._replan_after_in_flight = False
            self._suppress_followups.discard(generation)
            self._prune_suppressed_followups()
            self._replan()
            return WriteFailureResult(
                WriteFailureAction.ABORTED,
                frozenset(failed),
            )

        if self._replan_after_in_flight or retry_cancelled:
            self._replan_after_in_flight = False
            self._prune_suppressed_followups()
            self._replan()
            return WriteFailureResult(WriteFailureAction.REPLAN)

        # Retry the same semantic step. For a brightness finalizer, replanning
        # is preferable because the latest target may have changed ON -> OFF.
        if is_finalizer:
            self._replan()
            return WriteFailureResult(WriteFailureAction.REPLAN)

        self._planned.insert(0, command)
        self._planned_is_finalizer = False
        return WriteFailureResult(WriteFailureAction.RETRY)

    def force_replan(self) -> tuple[PlannedCommand, ...]:
        if self._in_flight is not None:
            self._replan_after_in_flight = True
            return tuple(self._planned)
        self._replan()
        return tuple(self._planned)

    def _finish_in_flight(self, *, keep_attempt_key: bool = False) -> None:
        if not keep_attempt_key:
            self._in_flight_attempt_key = None
        else:
            # Keep the accumulated attempt count in _write_attempts, but there is
            # no active in-flight key after the result was processed.
            self._in_flight_attempt_key = None
        self._in_flight = None
        self._in_flight_generation = None
        self._in_flight_generation_cutoff = None
        self._in_flight_retry_cancelled = False
        self._in_flight_attempt = None
        self._in_flight_is_finalizer = False

    def _command_generation(
        self,
        command: PlannedCommand,
        *,
        is_finalizer: bool,
    ) -> int:
        if is_finalizer:
            generation = self._brightness_finalizer_generation
            assert generation is not None
            return generation

        versions = self._pending_versions
        if command.kind in {CommandKind.POWER_ON, CommandKind.POWER_OFF}:
            generation = versions.power
        elif command.kind is CommandKind.BRIGHTNESS_ON:
            generation = versions.brightness
        elif command.kind in {CommandKind.COLOR, CommandKind.CCT}:
            generation = versions.appearance
        elif command.kind is CommandKind.EFFECT_START:
            generation = versions.effect
        else:
            # Explicit Stop Effect owns the effect slot. A recovery stop has no
            # effect target of its own and is charged to the newest pending
            # generation that triggered recovery.
            generation = versions.effect if self._pending.stop_effect else None
            if generation is None:
                active = [
                    value
                    for value in (
                        versions.power,
                        versions.brightness,
                        versions.appearance,
                        versions.effect,
                    )
                    if value is not None
                ]
                generation = max(active) if active else self._request_generation

        assert generation is not None, "planned command must have an owning target"
        return generation

    def _prune_suppressed_followups(self) -> None:
        """Keep suppression only for a write that can still finish later.

        Pending-only generations cannot create automatic follow-up work once
        their intent has been removed. The only generation that still needs
        suppression is therefore the currently in-flight write, whose eventual
        success could otherwise recreate a brightness finalizer or expected
        stored-effect continuation after cancellation.
        """
        if self._in_flight_generation is None:
            self._suppress_followups.clear()
            return

        self._suppress_followups.intersection_update(
            {self._in_flight_generation}
        )

    def _prune_write_attempts(self) -> None:
        active = self.pending_generations
        if self._in_flight_generation is not None:
            active = frozenset((*active, self._in_flight_generation))
        self._write_attempts = {
            key: attempts
            for key, attempts in self._write_attempts.items()
            if key[1] in active
        }

    def _replan(self) -> None:
        self._prune_write_attempts()
        self._planned_is_finalizer = False

        if not self._state.known or self._state.is_on is None:
            self._planned.clear()
            return

        # The finalizer is a physical invariant, not an ordinary pending slot.
        # It always blocks every non-power command until a power write succeeds.
        if self._brightness_finalizer_generation is not None:
            kind = (
                CommandKind.POWER_OFF
                if self._pending.power is False
                else CommandKind.POWER_ON
            )
            self._planned = [PlannedCommand(kind)]
            self._planned_is_finalizer = True
            return

        if not self._has_pending_targets(self._pending):
            self._planned.clear()
            return

        recover_stored_effect = not self._is_expected_stored_intermediate()
        planned = list(
            plan_commands(
                self._state,
                self._pending,
                recover_stored_effect=recover_stored_effect,
            )
        )

        # A normalized no-op completes immediately. This is safe only after the
        # finalizer check above; an outstanding brightness request cannot vanish
        # merely because state already matches its power target.
        if not planned:
            self._clear_pending_targets()
            self._clear_expected_stored_intermediate()

        self._planned = planned

    def _is_expected_stored_intermediate(self) -> bool:
        return (
            self._expected_stored_effect_generation is not None
            and self._state.known
            and self._state.is_on is False
            and self._state.effect == self._expected_stored_effect
            and self._pending.power is True
            and self._pending_versions.power
            == self._expected_stored_effect_generation
            and self._request_generation
            == self._expected_stored_effect_generation
        )

    def _matches_expected_stored_intermediate(self, state: PlannerState) -> bool:
        return (
            self._expected_stored_effect_generation is not None
            and state.known
            and state.is_on is False
            and state.effect == self._expected_stored_effect
        )

    def _clear_expected_stored_intermediate(self) -> None:
        self._expected_stored_effect_generation = None
        self._expected_stored_effect = None

    @staticmethod
    def _has_pending_targets(target: TargetState) -> bool:
        return any(
            (
                target.power is not None,
                target.brightness is not None,
                target.color is not None,
                target.cct is not None,
                target.effect is not None,
                target.stop_effect,
            )
        )

    @staticmethod
    def _merge_pending(
        current: TargetState,
        versions: _PendingVersions,
        incoming: TargetState,
        generation: int,
    ) -> tuple[TargetState, _PendingVersions]:
        """Merge one normalized partial request into latest-wins target slots."""

        # OFF is a complete normalized target and replaces every older slot.
        if incoming.power is False:
            return (
                TargetState(power=False),
                _PendingVersions(power=generation),
            )

        power = current.power
        brightness = current.brightness
        brightness_transition = current.brightness_transition
        color = current.color
        cct = current.cct
        appearance_transition = current.appearance_transition
        effect = current.effect
        stop_effect = current.stop_effect

        new_versions = _PendingVersions(
            power=versions.power,
            brightness=versions.brightness,
            appearance=versions.appearance,
            effect=versions.effect,
        )

        incoming_appearance = incoming.color is not None or incoming.cct is not None
        incoming_requires_on = (
            incoming.power is True
            or (incoming.brightness is not None and incoming.brightness > 0)
            or incoming_appearance
            or incoming.effect is not None
        )

        # Any newer ON-requiring request supersedes an older OFF target. Keep an
        # internal Power-ON tail so preparatory writes cannot lose the eventual
        # ON requirement during advertisement-driven replanning.
        if incoming_requires_on:
            power = True
            new_versions.power = generation

        if incoming.power is True:
            power = True
            new_versions.power = generation

        if incoming.brightness is not None:
            brightness = incoming.brightness
            brightness_transition = incoming.brightness_transition
            new_versions.brightness = generation

        # A newer static appearance supersedes any older effect goal. A newer
        # effect start, conversely, may intentionally coexist with an older
        # appearance so the combined sequence can establish the underlying mode.
        if incoming_appearance:
            effect = None
            stop_effect = False
            new_versions.effect = None
            if incoming.color is not None:
                color = incoming.color
                cct = None
            else:
                color = None
                cct = incoming.cct
            appearance_transition = incoming.appearance_transition
            new_versions.appearance = generation

        if incoming.effect is not None:
            effect = incoming.effect
            stop_effect = False
            new_versions.effect = generation
        elif incoming.stop_effect:
            effect = None
            stop_effect = True
            new_versions.effect = generation

        return (
            TargetState(
                power=power,
                brightness=brightness,
                brightness_transition=brightness_transition,
                color=color,
                cct=cct,
                appearance_transition=appearance_transition,
                effect=effect,
                stop_effect=stop_effect,
            ),
            new_versions,
        )

    def _pending_generations_at_or_before(self, cutoff: int) -> set[int]:
        return {
            version
            for version in (
                self._pending_versions.power,
                self._pending_versions.brightness,
                self._pending_versions.appearance,
                self._pending_versions.effect,
                self._brightness_finalizer_generation,
            )
            if version is not None and version <= cutoff
        }

    def _remove_generation(self, generation: int) -> None:
        pending = self._pending
        versions = self._pending_versions

        power = pending.power
        brightness = pending.brightness
        brightness_transition = pending.brightness_transition
        color = pending.color
        cct = pending.cct
        appearance_transition = pending.appearance_transition
        effect = pending.effect
        stop_effect = pending.stop_effect

        if versions.power == generation:
            power = None
            versions.power = None
        if versions.brightness == generation:
            brightness = None
            brightness_transition = None
            versions.brightness = None
        if versions.appearance == generation:
            color = None
            cct = None
            appearance_transition = None
            versions.appearance = None
        if versions.effect == generation:
            effect = None
            stop_effect = False
            versions.effect = None

        self._pending = TargetState(
            power=power,
            brightness=brightness,
            brightness_transition=brightness_transition,
            color=color,
            cct=cct,
            appearance_transition=appearance_transition,
            effect=effect,
            stop_effect=stop_effect,
        )

    def _discard_pending_at_or_before(self, cutoff: int) -> None:
        versions = [
            value
            for value in (
                self._pending_versions.power,
                self._pending_versions.brightness,
                self._pending_versions.appearance,
                self._pending_versions.effect,
            )
            if value is not None and value <= cutoff
        ]
        for generation in sorted(set(versions)):
            self._remove_generation(generation)

        if (
            self._brightness_finalizer_generation is not None
            and self._brightness_finalizer_generation <= cutoff
        ):
            self._brightness_finalizer_generation = None

        if (
            self._expected_stored_effect_generation is not None
            and self._expected_stored_effect_generation <= cutoff
        ):
            self._clear_expected_stored_intermediate()

    def _clear_pending_targets(self) -> None:
        self._pending = TargetState()
        self._pending_versions = _PendingVersions()

    @staticmethod
    def _state_after_success(
        state: PlannerState,
        command: PlannedCommand,
    ) -> PlannerState:
        if not state.known or state.is_on is None:
            return state

        is_on = state.is_on
        effect = state.effect

        if command.kind is CommandKind.POWER_ON:
            is_on = True
        elif command.kind is CommandKind.POWER_OFF:
            is_on = False
            effect = None
        elif command.kind is CommandKind.BRIGHTNESS_ON:
            is_on = True
        elif command.kind in {CommandKind.COLOR, CommandKind.CCT}:
            effect = None
        elif command.kind is CommandKind.EFFECT_START:
            effect = str(command.value)
        elif command.kind is CommandKind.EFFECT_STOP:
            effect = None

        return PlannerState(known=True, is_on=is_on, effect=effect)

    @staticmethod
    def _pending_after_success(
        pending: TargetState,
        versions: _PendingVersions,
        command: PlannedCommand,
        generation_cutoff: int,
    ) -> tuple[TargetState, _PendingVersions]:
        """Remove pending slots satisfied by a confirmed successful command."""

        power = pending.power
        brightness = pending.brightness
        brightness_transition = pending.brightness_transition
        color = pending.color
        cct = pending.cct
        appearance_transition = pending.appearance_transition
        effect = pending.effect
        stop_effect = pending.stop_effect

        def completed(version: int | None) -> bool:
            return version is not None and version <= generation_cutoff

        if command.kind is CommandKind.POWER_ON:
            if power is True and completed(versions.power):
                power = None
                versions.power = None

        elif command.kind is CommandKind.POWER_OFF:
            if power is False and completed(versions.power):
                power = None
                versions.power = None

        elif command.kind is CommandKind.BRIGHTNESS_ON:
            if brightness == command.value and completed(versions.brightness):
                brightness = None
                brightness_transition = None
                versions.brightness = None
            # Do not clear the internal Power-ON tail here. It is the physical
            # finalizer target and remains until a successful power write.

        elif command.kind is CommandKind.COLOR:
            if color == command.value and completed(versions.appearance):
                color = None
                appearance_transition = None
                versions.appearance = None

        elif command.kind is CommandKind.CCT:
            if cct == command.value and completed(versions.appearance):
                cct = None
                appearance_transition = None
                versions.appearance = None

        elif command.kind is CommandKind.EFFECT_START:
            if effect == command.value and completed(versions.effect):
                effect = None
                versions.effect = None

        elif command.kind is CommandKind.EFFECT_STOP:
            if stop_effect and completed(versions.effect):
                stop_effect = False
                versions.effect = None

        return (
            TargetState(
                power=power,
                brightness=brightness,
                brightness_transition=brightness_transition,
                color=color,
                cct=cct,
                appearance_transition=appearance_transition,
                effect=effect,
                stop_effect=stop_effect,
            ),
            versions,
        )

    def _require_in_flight(self) -> PlannedCommand:
        if self._in_flight is None:
            raise NoWriteInProgressError("no logical command is currently in flight")
        return self._in_flight


__all__ = [
    "CommandManager",
    "CommandManagerError",
    "CommandNotReadyError",
    "ManagerSnapshot",
    "NoWriteInProgressError",
    "WriteFailureAction",
    "WriteFailureResult",
    "WriteInProgressError",
]
