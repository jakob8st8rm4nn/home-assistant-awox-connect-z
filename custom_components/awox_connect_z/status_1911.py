"""Session-scoped, bounded storage of validated direct-lamp status replies."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass
import time
from typing import Callable, TypedDict

from .advertisement import AwoxAdvertisementState, lamp_state_from_fields
from .const import STATUS_EFFECTS
from .protocol import parse_status_1911


class StatusGoal(TypedDict, total=False):
    """Absent fields are ignored; effect=None explicitly requires no effect."""

    power: bool
    brightness: int
    color_mode: bool
    effect: str | None
    mired: int | None
    hue: int | None
    saturation: int | None


@dataclass(frozen=True, slots=True)
class Status1911State:
    session_generation: int
    received_monotonic: float
    sequence: int
    mesh_id: int
    mac_suffix: str
    mode: int
    power: bool
    brightness: int
    color_mode: bool
    effect: str | None
    mired: int | None
    hue: int | None
    saturation: int | None
    outer_type: int = 0x80

    def matches(self, goal: StatusGoal) -> bool:
        for name, expected in goal.items():
            actual = getattr(self, name, None)
            if actual is None or expected is None:
                if actual != expected:
                    return False
                continue
            if name in {"brightness", "mired", "saturation", "hue"}:
                if name == "hue":
                    if actual == 255:
                        # An effect sample with FF cannot prove a fixed hue goal.
                        return False
                    # Tested firmware reports sent 0xFE as 0x00. Preserve raw
                    # bytes in state/commands and normalize only for comparison.
                    actual = 0 if actual == 254 else actual
                    expected = 0 if expected == 254 else expected
                delta = abs(actual - expected)
                if name == "hue" and 0 <= actual < 254 and 0 <= expected < 254:
                    delta = min(delta, 254 - delta)
                if delta > 1:
                    return False
            elif actual != expected:
                return False
        return True

    def as_lamp_state(self) -> AwoxAdvertisementState:
        """Reuse the existing normalized lamp fields, never fake a radio packet."""
        return lamp_state_from_fields(
            mesh_id=self.mesh_id, mode=self.mode, brightness=self.brightness,
            mired=self.mired, hue=self.hue, saturation=self.saturation,
        )


class Status1911Store:
    def __init__(self, mac: str, mesh_id: int | None):
        compact = mac.replace(":", "").replace("-", "")
        self.mac_suffix = bytes.fromhex(compact)[-4:][::-1]
        self.mesh_id = mesh_id
        self.session_generation = 0
        self.active = False
        self.sequence = 0
        self.frames: deque[Status1911State] = deque(maxlen=128)
        self.changed = asyncio.Event()
        self.listeners: set[Callable[[Status1911State], None]] = set()
        self.invalid_frames = 0
        self.ignored_mesh_frames = 0
        self.last_received = None
        self.last_rejected_status = None

    @property
    def latest(self) -> Status1911State | None:
        return self.frames[-1] if self.frames else None

    def begin(self, generation: int) -> None:
        self.session_generation = generation
        self.active = True
        self.frames.clear()
        self.changed.set()

    def end(self) -> None:
        self.active = False
        self.frames.clear()
        self.changed.set()

    def feed(self, payload: bytes, key: bytes, generation: int) -> None:
        if not self.active or generation != self.session_generation:
            return
        try:
            raw = parse_status_1911(key, payload)
        except ValueError as exc:
            self.invalid_frames += 1
            self.last_rejected_status = dict(
                reason=str(exc), raw=payload.hex(), session_generation=generation,
                received_monotonic=time.monotonic(),
            )
            return
        # 1.12 confirms only the directly connected lamp. Decode mesh replies
        # for classification, but never publish or confirm state from them.
        if raw["outer_type"] == 0x82:
            self.ignored_mesh_frames += 1
            return
        if raw["mesh_id"] != self.mesh_id or raw["mac_suffix"] != self.mac_suffix:
            self.invalid_frames += 1
            return
        mode = raw["mode"]
        self.sequence += 1
        state = Status1911State(
            generation, time.monotonic(), self.sequence, raw["mesh_id"],
            self.mac_suffix.hex(), mode, bool(mode & 1), raw["brightness"],
            bool(mode & 2), STATUS_EFFECTS[mode],
            raw["mired"] if not mode & 2 and raw["mired"] not in (0, 65535) else None,
            raw["hue"] if mode & 2 else None,
            raw["saturation"] if mode & 2 else None,
        )
        self.frames.append(state)
        self.last_received = state
        self.changed.set()
        for listener in tuple(self.listeners):
            listener(state)

    async def wait(self, goal: StatusGoal, generation: int, after: float,
                   deadline: float, valid: Callable[[], bool]) -> Status1911State | None:
        while self.active and self.session_generation == generation and valid():
            self.changed.clear()
            for state in reversed(self.frames):
                # feed() appends in monotonic receive order; older frames cannot match.
                if state.received_monotonic < after:
                    break
                if state.matches(goal):
                    return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                # Recheck confirmation ownership even without a new notification.
                await asyncio.wait_for(self.changed.wait(), min(remaining, .05))
            except TimeoutError:
                pass
        return None

    def diagnostics(self) -> dict:
        return dict(session_generation=self.session_generation, active=self.active,
                    invalid_frames=self.invalid_frames,
                    last_rejected_status=self.last_rejected_status,
                    ignored_mesh_frames=self.ignored_mesh_frames,
                    received_frames=self.sequence,
                    last_status=asdict(self.last_received) if self.last_received else None)
