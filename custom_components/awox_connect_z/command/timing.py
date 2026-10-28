"""Shared fade allowance for command spacing and confirmation deadlines."""

from ..const import FADE_WAIT_FACTOR


def with_fade_wait(base_seconds: float, encoded_transition_s: float) -> float:
    """Add the fade allowance to an existing duration or absolute deadline.

    Callers retain their time origin, base wait, elapsed-time calculation and
    clamping. The transition is the duration already encoded in the command.
    """
    return base_seconds + FADE_WAIT_FACTOR * encoded_transition_s
