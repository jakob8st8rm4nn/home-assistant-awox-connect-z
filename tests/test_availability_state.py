"""Direct model boundaries, history retention and snapshot independence."""
import math
import unittest
from datetime import datetime, timezone

from test_status_confirmation import AwoxConnectZClient  # Install the existing HA/BLE stubs.
from awox112.availability import AvailabilityState


class AvailabilityStateTests(unittest.TestCase):
    def test_state_priority_and_exact_deadlines(self):
        state = AvailabilityState(connection_time=105, advertisement_time=100)
        self.assertEqual(state.status(109, 10), 'recently_connected')
        state.advertisement_time = 106
        self.assertEqual(state.status(109, 10), 'visible')
        self.assertEqual(state.status(116, 10), 'unreachable')
        self.assertEqual(state.status(116, 10, connected=True), 'connected')
        self.assertIsNone(AvailabilityState().status(116, 10))

    def test_floating_point_boundary_matches_original_age_comparison(self):
        for timestamp in (0., .1, 100., 10000000.123):
            deadline = timestamp + 10
            for now in (math.nextafter(deadline, -math.inf), deadline,
                        math.nextafter(deadline, math.inf)):
                state = AvailabilityState(advertisement_time=timestamp)
                expected = 'visible' if now - timestamp < 10 else 'unreachable'
                self.assertEqual(state.status(now, 10), expected)

    def test_same_time_refines_history_without_redating_last_seen(self):
        state = AvailabilityState()
        wallclock = datetime(2026, 10, 24, tzinfo=timezone.utc)
        def unexpected():
            raise AssertionError('Duplicate or old evidence must not read a new clock')
        state.note_advertisement(100, source='A', rssi=-60,
                                 clock=lambda: 101, utcnow=lambda: wallclock)
        seen = state.last_seen
        self.assertTrue(state.note_advertisement(100, source='B', rssi=-50,
                                                clock=unexpected, utcnow=unexpected))
        self.assertFalse(state.note_advertisement(99, source='C', rssi=-40,
                                                 clock=unexpected, utcnow=unexpected))
        self.assertEqual((state.source, state.rssi, state.last_seen), ('B', -50, seen))

    def test_export_restore_preserves_all_history_and_existing_tuple_forms(self):
        original = AvailabilityState(connection_time=102, advertisement_time=100,
                                     source='B', rssi=-50)
        restored = AvailabilityState()
        restored.restore(original.export())
        self.assertEqual(restored.export(), original.export())
        restored.restore((90, 91, None))
        self.assertEqual(restored.export(), (90, 91, None, 'B', -50))

    def test_wait_for_packet_expiry_even_with_newer_connection_evidence(self):
        state = AvailabilityState(connection_time=105, advertisement_time=100)
        self.assertEqual(state.wait_seconds(109, 10, connected=False, poll=1, minimum=.05), 1)
        self.assertEqual(state.wait_seconds(110, 10, connected=False, poll=1, minimum=.05), 5)
        self.assertEqual(state.wait_seconds(115, 10, connected=False, poll=1, minimum=.05), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
