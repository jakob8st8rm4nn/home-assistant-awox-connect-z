"""Pure command-sequence planner for AwoX / EGLO Connect.Z lamps.

The sequencer owns request normalization, state-dependent command ordering and
stored-effect recovery. It deliberately does not implement pending-target
merging, latest-request handling, write spacing, BLE transport or retries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


ColorValue: TypeAlias = tuple[float, float]
EffectId: TypeAlias = str


class CommandKind(str, Enum):
    """Logical commands understood by the sequence planner."""

    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    BRIGHTNESS_ON = "brightness_on"
    COLOR = "color"
    CCT = "cct"
    EFFECT_START = "effect_start"
    EFFECT_STOP = "effect_stop"


@dataclass(frozen=True, slots=True)
class PlannedCommand:
    """One symbolic command produced by the planner."""

    kind: CommandKind
    value: object | None = None
    transition: float | None = None


@dataclass(frozen=True, slots=True)
class PlannerState:
    """Known state required to select a command sequence.

    ``effect`` has one meaning depending on power state:

    - ON + effect != None: that effect is currently active.
    - OFF + effect != None: an exceptional stored-effect recovery state.
    - effect == None: no active/stored effect is known.

    If information required for planning cannot be interpreted, set
    ``known=False``. New requests are rejected while the planner is UNKNOWN.
    """

    known: bool
    is_on: bool | None = None
    effect: EffectId | None = None


@dataclass(frozen=True, slots=True)
class TargetState:
    """Requested/pending logical target values.

    ``None`` means that the corresponding target is absent. Color and CCT stay
    separate so contradictory requests can be rejected before sequence
    selection. The manager may carry an internal ``power=True`` tail while a
    sequence still needs to establish ON; the planner treats it as redundant
    whenever another requested command already defines the sequence.
    """

    power: bool | None = None
    brightness: int | None = None
    brightness_transition: float | None = None
    color: ColorValue | None = None
    cct: int | None = None
    appearance_transition: float | None = None
    effect: EffectId | None = None
    stop_effect: bool = False


class SequencePlannerError(RuntimeError):
    """Base class for sequence-planner failures."""


class UnknownPlannerStateError(SequencePlannerError):
    """Raised when control is attempted while planner state is UNKNOWN."""


class SequenceConflictError(SequencePlannerError):
    """Raised for contradictory targets rejected by the specification."""


def _cmd(
    kind: CommandKind,
    value: object | None = None,
    transition: float | None = None,
) -> PlannedCommand:
    return PlannedCommand(kind=kind, value=value, transition=transition)


def _validate_state(state: PlannerState) -> None:
    if not state.known:
        raise UnknownPlannerStateError(
            "Control request rejected because the planner state is UNKNOWN"
        )
    if state.is_on is None:
        raise SequencePlannerError(
            "Known planner state must include a definite power state"
        )


def _validate_numeric_targets(target: TargetState) -> None:
    if target.brightness is not None and target.brightness < 0:
        raise SequenceConflictError("Brightness must not be negative")
    if target.brightness_transition is not None and target.brightness_transition < 0:
        raise SequenceConflictError("Brightness transition must not be negative")
    if target.appearance_transition is not None and target.appearance_transition < 0:
        raise SequenceConflictError("Appearance transition must not be negative")


def normalize_request(state: PlannerState, target: TargetState) -> TargetState:
    """Normalize one newly submitted request before pending-target merging.

    This implements the request-level precedence rules. It intentionally does
    not merge with older pending intent; that remains a command-manager task.
    """

    _validate_state(state)
    assert state.is_on is not None

    # OFF has highest request-level priority. Brightness zero is exactly OFF;
    # every other field in that request is discarded before further validation.
    if target.power is False or target.brightness == 0:
        return TargetState(power=False)

    _validate_numeric_targets(target)

    if target.color is not None and target.cct is not None:
        raise SequenceConflictError("Color and CCT cannot both be requested")
    if target.effect is not None and target.stop_effect:
        raise SequenceConflictError(
            "Effect start and explicit effect stop cannot be requested together"
        )

    has_appearance = target.color is not None or target.cct is not None
    has_positive_brightness = target.brightness is not None and target.brightness > 0
    has_effect_start = target.effect is not None

    power = target.power
    stop_effect = target.stop_effect

    # Power ON is redundant when an appearance/effect/brightness sequence
    # already establishes or preserves ON. It remains meaningful with Stop only.
    if power is True and (
        has_positive_brightness or has_appearance or has_effect_start
    ):
        power = None

    # Static appearance itself stops an active effect.
    if has_appearance and stop_effect:
        stop_effect = False

    # In normal states, stopping a non-existent effect is a no-op. A stored
    # effect is deliberately not removed here; recovery handles it later.
    if stop_effect and state.effect is None:
        stop_effect = False

    return TargetState(
        power=power,
        brightness=target.brightness,
        brightness_transition=(
            target.brightness_transition if target.brightness is not None else None
        ),
        color=target.color,
        cct=target.cct,
        appearance_transition=(
            target.appearance_transition if has_appearance else None
        ),
        effect=target.effect,
        stop_effect=stop_effect,
    )


def _appearance_command(target: TargetState) -> PlannedCommand:
    if target.color is not None and target.cct is not None:
        raise SequenceConflictError("Color and CCT cannot both be planned")
    if target.color is not None:
        return _cmd(CommandKind.COLOR, target.color, target.appearance_transition)
    if target.cct is not None:
        return _cmd(CommandKind.CCT, target.cct, target.appearance_transition)
    raise SequencePlannerError("Appearance command requested without Color/CCT")


def _plan_power(state: PlannerState, on: bool) -> tuple[PlannedCommand, ...]:
    assert state.is_on is not None
    if on:
        return () if state.is_on else (_cmd(CommandKind.POWER_ON),)
    return (_cmd(CommandKind.POWER_OFF),) if state.is_on else ()


def _plan_normal(
    state: PlannerState,
    target: TargetState,
) -> tuple[PlannedCommand, ...]:
    """Plan a normalized target from a normal (non-recovery) state."""

    assert state.is_on is not None

    # Keep request-level precedence for callers that invoke the pure planner
    # directly instead of going through CommandManager.submit().
    if target.power is False or target.brightness == 0:
        return _plan_power(state, False)
    if target.color is not None and target.cct is not None:
        raise SequenceConflictError("Color and CCT cannot both be requested")
    if target.effect is not None and target.stop_effect:
        raise SequenceConflictError(
            "Effect start and explicit effect stop cannot be requested together"
        )

    has_appearance = target.color is not None or target.cct is not None
    has_brightness = target.brightness is not None and target.brightness > 0
    has_effect = target.effect is not None

    # Color/CCT makes an explicit effect stop redundant.
    stop_effect = target.stop_effect and not has_appearance

    appearance = _appearance_command(target) if has_appearance else None
    brightness = (
        _cmd(
            CommandKind.BRIGHTNESS_ON,
            target.brightness,
            target.brightness_transition,
        )
        if has_brightness
        else None
    )
    effect_start = (
        _cmd(CommandKind.EFFECT_START, target.effect) if has_effect else None
    )

    # Color/CCT + Brightness + Start Effect.
    if appearance is not None and brightness is not None and effect_start is not None:
        return (
            appearance,
            brightness,
            _cmd(CommandKind.POWER_ON),
            effect_start,
        )

    # Color/CCT + Start Effect.
    if appearance is not None and effect_start is not None:
        if state.is_on:
            return (appearance, effect_start)
        return (appearance, effect_start, _cmd(CommandKind.POWER_ON))

    # Color/CCT + Brightness.
    if appearance is not None and brightness is not None:
        return (appearance, brightness, _cmd(CommandKind.POWER_ON))

    # Stop Effect + Brightness.
    if stop_effect and brightness is not None:
        if state.effect is not None:
            return (
                _cmd(CommandKind.EFFECT_STOP, state.effect),
                brightness,
                _cmd(CommandKind.POWER_ON),
            )
        return (brightness, _cmd(CommandKind.POWER_ON))

    # Brightness + Start Effect. Brightness always finalizes before a later
    # non-power effect command. Same active effect needs no redundant start.
    if brightness is not None and effect_start is not None:
        if state.is_on and state.effect == target.effect:
            return (brightness, _cmd(CommandKind.POWER_ON))
        return (brightness, _cmd(CommandKind.POWER_ON), effect_start)

    if appearance is not None:
        if state.is_on:
            return (appearance,)
        return (appearance, _cmd(CommandKind.POWER_ON))

    if effect_start is not None:
        if state.is_on:
            if state.effect == target.effect:
                return ()
            return (effect_start,)
        return (effect_start, _cmd(CommandKind.POWER_ON))

    if brightness is not None:
        return (brightness, _cmd(CommandKind.POWER_ON))

    if stop_effect:
        if state.effect is None:
            return _plan_power(state, True) if target.power is True else ()
        if target.power is True and state.is_on:
            return (_cmd(CommandKind.EFFECT_STOP, state.effect),)
        if target.power is True and not state.is_on:
            return (
                _cmd(CommandKind.EFFECT_STOP, state.effect),
                _cmd(CommandKind.POWER_ON),
            )
        return (_cmd(CommandKind.EFFECT_STOP, state.effect),)

    if target.power is not None:
        return _plan_power(state, target.power)

    return ()


def plan_commands(
    state: PlannerState,
    target: TargetState,
    *,
    recover_stored_effect: bool = True,
) -> tuple[PlannedCommand, ...]:
    """Return the logical command sequence for ``state`` -> ``target``.

    ``recover_stored_effect`` is normally true. The manager sets it false only
    for the expected OFF+stored intermediate state inside an already-running
    ``EFFECT START -> POWER ON`` sequence; that state must continue to the
    planned power-on rather than trigger recovery.

    No delay commands are returned. The manager enforces spacing and owns the
    brightness-finalizer obligation after a successful ``BRIGHTNESS_ON``.
    """

    _validate_state(state)
    assert state.is_on is not None

    # Request-level OFF normalization takes precedence over validation/conflicts.
    if target.power is False or target.brightness == 0:
        normalized = TargetState(power=False)
    else:
        _validate_numeric_targets(target)
        if target.color is not None and target.cct is not None:
            raise SequenceConflictError("Color and CCT cannot both be requested")
        if target.effect is not None and target.stop_effect:
            raise SequenceConflictError(
                "Effect start and explicit effect stop cannot be requested together"
            )
        normalized = target

    # OFF + stored effect is an exceptional recovery state. Recovery is a fixed
    # sequencer prefix; after it succeeds the remaining target is planned from
    # normal OFF. The manager controls when this is an expected intermediate.
    if (
        recover_stored_effect
        and not state.is_on
        and state.effect is not None
    ):
        normal_off = PlannerState(known=True, is_on=False, effect=None)
        return (
            _cmd(CommandKind.EFFECT_STOP, state.effect),
            *_plan_normal(normal_off, normalized),
        )

    return _plan_normal(state, normalized)


__all__ = [
    "ColorValue",
    "CommandKind",
    "EffectId",
    "PlannedCommand",
    "PlannerState",
    "SequenceConflictError",
    "SequencePlannerError",
    "TargetState",
    "UnknownPlannerStateError",
    "normalize_request",
    "plan_commands",
]
