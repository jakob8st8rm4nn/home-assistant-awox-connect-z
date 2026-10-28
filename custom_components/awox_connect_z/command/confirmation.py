"""Immutable executed batches and bounded, session-local confirmation policy."""
from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Callable, Literal

from .sequences import CommandKind as K, PlannedCommand
from ..const import (CONFIRMATION_AUTO_WAIT_MS, CONFIRMATION_POLL_WINDOW_MS,
                    CONFIRMATION_MAX_POLLS, CONFIRMATION_MAX_GROUP_REPEATS)
from ..status_1911 import Status1911State, StatusGoal
from .timing import with_fade_wait


ConfirmationOutcome = Literal["confirmed", "unconfirmed", "superseded"]


@dataclass(frozen=True, slots=True)
class ExecutedCommand:
    command: PlannedCommand
    plaintext: bytes
    started: float
    finished: float
    transition: float
    affected_generations: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class ConfirmationBatch:
    session_generation: int
    commands: tuple[ExecutedCommand, ...]

    @property
    def goal(self) -> StatusGoal:
        goal: StatusGoal = {}
        for item in self.commands:
            c = item.command
            if c.kind is K.POWER_OFF:
                goal = dict(power=False, effect=None)
            elif c.kind is K.POWER_ON:
                goal["power"] = True
            elif c.kind is K.BRIGHTNESS_ON:
                goal.update(power=True, brightness=item.plaintext[8])
            elif c.kind in (K.COLOR, K.CCT):
                for name in ("hue", "saturation", "mired"):
                    goal.pop(name, None)
                goal.update(power=True, effect=None, color_mode=c.kind is K.COLOR)
                if c.kind is K.COLOR:
                    goal.update(hue=item.plaintext[8], saturation=item.plaintext[9])
                else:
                    goal["mired"] = int.from_bytes(item.plaintext[8:10], "little")
            elif c.kind is K.EFFECT_STOP:
                goal["effect"] = None
            elif c.kind is K.EFFECT_START:
                normalized = str(c.value).strip().casefold().replace("-", "_").replace(" ", "_")
                goal["effect"] = "Color cycle" if normalized == "color_cycle" else "Candle"
                if goal["effect"] == "Color cycle":
                    for name in ("color_mode", "hue", "saturation", "mired"):
                        goal.pop(name, None)
        return goal

    @property
    def transition_deadline(self) -> float:
        # Earlier writes have already consumed their post-fade spacing.
        item = self.commands[-1]
        if item.command.kind in (K.BRIGHTNESS_ON, K.COLOR, K.CCT):
            return with_fade_wait(item.finished, item.transition) + CONFIRMATION_AUTO_WAIT_MS / 1000
        return 0.0



class ConfirmationSuperseded(Exception):
    """The confirmation owner was invalidated at the actual repeat boundary."""


class CommandConfirmation:
    def __init__(self, client):
        self.client = client
        self.diagnostics = dict(batches=0, confirmed=0, passive_confirmed=0,
                                polls=0, repeats=0, unconfirmed=0, superseded=0,
                                last_outcome=None)

    async def confirm(self, batch: ConfirmationBatch, valid: Callable[[], bool],
                      spacing: float, note_repeat_write: Callable[[float, float], None],
                      goal_provider: Callable[[], StatusGoal] | None = None) -> tuple[ConfirmationOutcome, Status1911State | None]:
        if goal_provider is None:
            base_goal = batch.goal
            goal_provider = lambda: base_goal
        self.diagnostics["batches"] += 1
        try:
            after = batch.commands[-1].started
            finished = batch.commands[-1].finished
            previous_transition = batch.commands[-1].transition
            transition_deadline = batch.transition_deadline
            wait_valid = lambda: (
                valid() and self.client.connected and self.client.status_1911.active and
                self.client.status_1911.session_generation == batch.session_generation
            )
            for attempt in range(CONFIRMATION_MAX_GROUP_REPEATS + 1):
                if not valid():
                    return self._result("superseded", None)
                self._require_session(batch)
                if attempt:
                    for index, item in enumerate(batch.commands):
                        await self.client.async_status_delay(max(0, with_fade_wait(finished + spacing, previous_transition) - time.monotonic()), valid)
                        if not valid():
                            return self._result("superseded", None)
                        self._require_session(batch)
                        try:
                            after, finished = await self.client.async_repeat_plaintext(item.plaintext, batch.session_generation, valid)
                        finally:
                            # An uncertain failed attempt also consumes the command interval.
                            note_repeat_write(time.monotonic(), item.transition)
                        previous_transition = item.transition
                        if index == 0:
                            self.diagnostics["repeats"] += 1
                    transition_deadline = ConfirmationBatch(
                        batch.session_generation,
                        (replace(batch.commands[-1], started=after, finished=finished),),
                    ).transition_deadline
                deadline = max(finished + CONFIRMATION_AUTO_WAIT_MS / 1000, transition_deadline)
                state = await self.client.status_1911.wait(goal_provider(), batch.session_generation, after, deadline, wait_valid)
                if state and state.matches(goal_provider()):
                    self.diagnostics["passive_confirmed"] += 1
                    return self._result("confirmed", state)
                for _ in range(CONFIRMATION_MAX_POLLS):
                    if not valid():
                        return self._result("superseded", None)
                    self._require_session(batch)
                    await self.client.async_poll_status(batch.session_generation)
                    self.diagnostics["polls"] += 1
                    deadline = time.monotonic() + CONFIRMATION_POLL_WINDOW_MS / 1000
                    state = await self.client.status_1911.wait(goal_provider(), batch.session_generation, after, deadline, wait_valid)
                    if state and state.matches(goal_provider()):
                        return self._result("confirmed", state)
            if not valid():
                return self._result("superseded", None)
            return self._result("unconfirmed", None)
        except ConfirmationSuperseded:
            return self._result("superseded", None)
        except Exception:
            self.diagnostics["unconfirmed"] += 1
            self.diagnostics["last_outcome"] = "session_or_transport_error"
            raise

    def _require_session(self, batch: ConfirmationBatch) -> None:
        if (not self.client.connected or not self.client.status_1911.active or
                self.client.status_1911.session_generation != batch.session_generation):
            raise ConnectionError("1911 session ended; old batches are never replayed after reconnect")

    def _result(self, outcome: ConfirmationOutcome, state: Status1911State | None):
        self.diagnostics[outcome] += 1
        self.diagnostics["last_outcome"] = outcome
        return outcome, state
