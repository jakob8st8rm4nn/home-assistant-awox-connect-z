"""Bluetooth evidence and reachability rules, independent of HA and BLE I/O."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Callable
from typing import TypeAlias


# Advertisement time, connection time, last seen UTC, source, RSSI.
AvailabilityEvidence: TypeAlias = tuple[
    float | None, float | None, datetime | None, str | None, int | None
]


@dataclass(slots=True)
class AvailabilityState:
    """One lamp's evidence; historical advertisement data never expires."""

    connection_time: float | None = None
    advertisement_time: float | None = None
    last_seen: datetime | None = None
    source: str | None = None
    rssi: int | None = None
    snapshot_active: bool = False
    snapshot_status: str | None = None

    @staticmethod
    def is_available(status: str | None) -> bool:
        return status in ("connected", "visible", "recently_connected")

    def note_advertisement(
        self, seen_time: float, *, source: str | None, rssi: int | None,
        clock: Callable[[], float], utcnow: Callable[[], datetime],
    ) -> bool:
        seen_time = float(seen_time)
        if self.advertisement_time is not None and seen_time < self.advertisement_time:
            return False
        changed = self.advertisement_time != seen_time
        if changed:
            # Convert once: equal-time refinements must not move the displayed UTC time.
            age = max(0.0, clock() - seen_time)
            self.advertisement_time = seen_time
            self.last_seen = utcnow() - timedelta(seconds=age)
        if source is not None and source != self.source:
            self.source = str(source)
            changed = True
        if rssi is not None:
            rssi = int(rssi)
            if rssi != self.rssi:
                self.rssi = rssi
                changed = True
        return changed

    @property
    def latest_time(self) -> float | None:
        times = [t for t in (self.connection_time, self.advertisement_time) if t is not None]
        return max(times) if times else None

    def remaining(self, timestamp: float | None, now: float, timeout: float) -> float:
        return 0.0 if timestamp is None else timeout - (now - timestamp)

    def status(self, now: float, timeout: float, *, connected: bool = False) -> str | None:
        if connected:
            return "connected"
        advertisement_fresh = self.remaining(self.advertisement_time, now, timeout) > 0
        connection_fresh = self.remaining(self.connection_time, now, timeout) > 0
        if connection_fresh and (
            not advertisement_fresh or self.connection_time >= self.advertisement_time
        ):
            return "recently_connected"
        if advertisement_fresh:
            return "visible"
        if self.connection_time is None and self.advertisement_time is None:
            return None
        return "unreachable"

    def wait_seconds(
        self, now: float, timeout: float, *, connected: bool, poll: float, minimum: float
    ) -> float:
        remaining = 0.0 if connected else self.remaining(self.latest_time, now, timeout)
        wait = poll if not remaining > 0 else max(minimum, remaining)
        packet_remaining = (
            0.0 if self.advertisement_time is None else self.advertisement_time + timeout - now
        )
        return min(wait, max(minimum, packet_remaining)) if packet_remaining > 0 else wait

    def export(self) -> AvailabilityEvidence:
        return (self.advertisement_time, self.connection_time, self.last_seen, self.source, self.rssi)

    def restore(self, evidence: tuple) -> None:
        self.advertisement_time, self.connection_time, self.last_seen = evidence[:3]
        if len(evidence) >= 5:
            self.source, self.rssi = evidence[3:5]
