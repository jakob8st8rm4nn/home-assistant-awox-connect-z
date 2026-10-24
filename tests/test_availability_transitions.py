"""Observe every publication, including delayed BLE cleanup and timer boundaries."""
import asyncio
import itertools
import types
import unittest
from unittest.mock import patch

import test_status_confirmation as fx
from awox112 import client as cm


class AvailabilityTransitions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.clock = patch.object(cm, 'time', types.SimpleNamespace(monotonic=lambda: self.now))
        self.clock.start()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            client._client = None
            await client.async_close()
        self.clock.stop()

    def client(self, timeout=10, idle=20):
        hass = fx.Hass()
        hass.async_create_background_task = lambda coro, *a, **kw: asyncio.create_task(coro)
        c = fx.AwoxConnectZClient(hass, fx.MAC, 'mesh', 'password',
            default_transition=.2, idle_disconnect=idle, availability_timeout=timeout,
            command_semaphore=asyncio.Semaphore(1), max_concurrent_commands=1,
            configured_mesh_id=fx.MID)
        self.clients.append(c)
        c._latest_service_info = lambda: None
        return c

    def watch(self, c):
        events = []
        def observe(*args):
            events.append((c.bluetooth_status, c.available))
        c.async_add_availability_listener(observe)
        c.async_add_diagnostic_listener(observe)
        return events

    def coherent(self, events):
        self.assertTrue(events)
        for status, available in events:
            self.assertEqual(available, status in ('connected','visible','recently_connected'), events)

    async def test_boundary_reads_agree_before_worker_runs(self):
        c = self.client()
        c.async_note_bluetooth_liveness(100)
        self.now = 110
        self.assertEqual(c.bluetooth_status, 'unreachable')
        self.assertFalse(c.available)

    async def test_unrelated_diagnostic_publication_updates_expired_light(self):
        c = self.client(); events = self.watch(c)
        c.async_note_bluetooth_liveness(100)
        self.now = 110
        c._async_notify_diagnostic_listeners()
        self.coherent(events)

    async def test_publication_uses_one_reachability_snapshot_at_deadline(self):
        c = self.client(); c._last_seen_service_time = 100
        c._available = False
        observations = []

        ticks = iter((109.999, 110.001, 110.001, 110.001))
        cm.time.monotonic = lambda: next(ticks, 110.001)

        c.async_add_availability_listener(
            lambda value: observations.append(
                ("availability", value, c.bluetooth_status, c.available)
            )
        )
        c.async_add_diagnostic_listener(
            lambda: observations.append(
                ("diagnostic", c._available, c.bluetooth_status, c.available)
            )
        )

        c._async_notify_diagnostic_listeners()

        self.assertEqual(
            observations,
            [
                ("availability", True, "visible", True),
                ("diagnostic", True, "visible", True),
            ],
        )
        # Outside the publication snapshot the real clock has crossed the deadline.
        self.assertEqual(c.bluetooth_status, "unreachable")
        self.assertFalse(c.available)

    async def test_timer_expiry_publications_are_coherent(self):
        c = self.client(timeout=.02); events = self.watch(c)
        expired = asyncio.Event()
        c.async_add_availability_listener(lambda value: expired.set() if not value else None)
        c.async_note_bluetooth_liveness(100)
        c.async_start_availability_tracking()
        await asyncio.sleep(.005)
        self.now = 100.03
        await asyncio.wait_for(expired.wait(), 1)
        self.coherent(events)
        self.assertEqual(events[-1], ('unreachable', False))

    async def test_cache_only_recovery_publications_are_coherent(self):
        c = self.client(timeout=.02); c._last_liveness = 99
        c.async_start_availability_tracking()
        events = self.watch(c)
        c._latest_service_info = lambda: types.SimpleNamespace(time=100, source='proxy', rssi=-60)
        c._async_notify_diagnostic_listeners()
        self.coherent(events)
        self.assertEqual(events[-1], ('visible', True))

    async def test_backend_connect_before_auth_diagnostic_is_coherent(self):
        c = self.client(); c._last_liveness = 80
        c.async_start_availability_tracking()
        events = self.watch(c)
        c._client = types.SimpleNamespace(is_connected=True)
        c._async_set_last_connection_source('proxy')
        self.coherent(events)

    async def cleanup_case(self, *, delay=1, failure=False, cancel=False):
        c = self.client(); c._last_liveness = 80
        entered = asyncio.Event(); release = asyncio.Event()
        class Backend:
            is_connected = True
            async def disconnect(inner):
                inner.is_connected = False
                entered.set()
                await release.wait()
                if failure:
                    raise RuntimeError('backend cleanup failed')
        c._client = Backend(); events = self.watch(c)
        task = asyncio.create_task(c._async_disconnect(mark_liveness=True))
        await entered.wait()
        try:
            self.now += delay
            c._async_notify_diagnostic_listeners()
            during = events[-1]
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
        finally:
            release.set()
            if cancel:
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                await task
        return c, events, during

    async def test_delayed_idle_cleanup_does_not_publish_unreachable(self):
        c, events, during = await self.cleanup_case()
        self.assertEqual(during, ('recently_connected', True))
        self.coherent(events)

    async def test_failed_cleanup_does_not_renew_liveness_at_completion(self):
        c, events, during = await self.cleanup_case(delay=11, failure=True)
        self.assertEqual(c._last_liveness, 100)
        self.assertFalse(c.available)
        self.coherent(events)

    async def test_cancelled_cleanup_preserves_fixed_deadline(self):
        c, events, during = await self.cleanup_case(delay=2, cancel=True)
        self.assertEqual(c._last_liveness, 100)
        self.assertEqual(c._last_liveness, 100)
        self.coherent(events)

    async def test_packet_during_cleanup_owns_new_deadline(self):
        c = self.client(); c._last_liveness = 80
        test = self
        class Backend:
            is_connected = True
            async def disconnect(inner):
                inner.is_connected = False
                test.now = 102; c.async_note_bluetooth_liveness(102)
                test.now = 105
        c._client = Backend()
        await c._async_disconnect(mark_liveness=True)
        self.assertEqual(c._latest_liveness_time(), 102)
        self.now = 112
        self.assertFalse(c.available)

    async def test_bad_listener_does_not_block_other_entities(self):
        c = self.client(); c._last_liveness = 80; c._available = True
        def bad(*a): raise RuntimeError('entity callback failed')
        # Force a stable ordering to ensure the failure precedes the good listener.
        good = []
        c._availability_listeners = [bad, lambda value: good.append(value)]
        with self.assertLogs(cm._LOGGER, level='ERROR'):
            c.async_start_availability_tracking()
        self.assertEqual(good, [False])

    async def test_matrix_evidence_and_deadlines(self):
        count = 0
        for timeout, connected, packet_age, session_age in itertools.product(
                (10, 30, 60), (False, True), (None, 0, 9.999, 10, 30, 60, 80),
                (None, 0, 9.999, 10, 30, 60, 80)):
            with self.subTest(timeout=timeout, connected=connected, packet=packet_age, session=session_age):
                c = self.client(timeout=timeout)
                c._last_seen_service_time = None if packet_age is None else 100-packet_age
                c._last_liveness = None if session_age is None else 100-session_age
                c._client = types.SimpleNamespace(is_connected=connected)
                expected = connected or any(age is not None and age < timeout for age in (packet_age, session_age))
                packet_fresh = packet_age is not None and packet_age < timeout
                session_fresh = session_age is not None and session_age < timeout
                expected_status = (
                    'connected' if connected else
                    'recently_connected' if session_fresh and (not packet_fresh or session_age <= packet_age) else
                    'visible' if packet_fresh else
                    None if packet_age is None and session_age is None else
                    'unreachable'
                )
                self.assertEqual(c.bluetooth_status, expected_status)
                self.assertEqual(c.available, expected)
                count += 1
        self.assertEqual(count, 294)

    async def test_old_packet_cannot_shorten_or_extend_newer_evidence(self):
        c = self.client(); c.async_note_bluetooth_liveness(100)
        seen = c.last_seen
        self.now = 105; c.async_note_bluetooth_liveness(90)
        self.assertEqual(c.last_seen, seen)
        self.assertEqual(c._latest_liveness_time(), 100)
        self.now = 110; self.assertFalse(c.available)

    async def test_shutdown_cannot_be_revived_by_late_packet(self):
        c = self.client(); await c.async_close()
        c.async_note_bluetooth_liveness(100)
        self.assertFalse(c.available)
        self.assertIsNone(c._availability_task)

    async def test_idle_and_availability_ratio_matrix(self):
        for idle, timeout in itertools.product((10, 20, 60), (10, 30, 60)):
            with self.subTest(idle=idle, timeout=timeout):
                self.now = 100
                c = self.client(timeout=timeout, idle=idle)
                c.async_note_bluetooth_liveness(100)
                seen = c.last_seen
                class Backend:
                    is_connected = True
                    async def disconnect(inner): inner.is_connected = False
                c._client = Backend()
                self.now = 100 + idle
                events = self.watch(c)
                await c._async_disconnect(mark_liveness=True)
                self.coherent(events)
                self.assertEqual(events[-1], ('recently_connected', True))
                self.now += timeout - .001
                self.assertTrue(c.available)
                self.now += .001
                c._async_notify_diagnostic_listeners()
                self.assertEqual(events[-1], ('unreachable', False))
                self.assertEqual(c.last_seen, seen)

    async def test_cleanup_timeout_never_restarts_expired_deadline(self):
        c = self.client(); c._last_liveness = 80
        test = self
        class Backend:
            is_connected = True
            async def disconnect(inner):
                inner.is_connected = False
                test.now = 111
                await asyncio.Event().wait()
        c._client = Backend(); events = self.watch(c)
        with patch.object(cm, 'DISCONNECT_TIMEOUT_SECONDS', .01):
            with self.assertLogs(cm._LOGGER, level='WARNING'):
                await c._async_disconnect(mark_liveness=True)
        self.assertEqual(c._last_liveness, 100)
        self.assertEqual(events[-1], ('unreachable', False))
        self.coherent(events)

    async def test_idle_scheduler_replacement_only_disconnects_once(self):
        c = self.client(); c.idle_disconnect = .02
        calls = []
        class Backend:
            is_connected = True
            async def disconnect(inner):
                calls.append('disconnect'); inner.is_connected = False
        c._client = Backend()
        c._schedule_idle_disconnect()
        first = c._idle_task
        c._schedule_idle_disconnect()
        await asyncio.sleep(.04)
        self.assertTrue(first.cancelled())
        self.assertEqual(calls, ['disconnect'])
        self.assertEqual(c.bluetooth_status, 'recently_connected')

    async def test_shutdown_during_cleanup_cannot_republish_available(self):
        c = self.client(); entered = asyncio.Event(); release = asyncio.Event()
        class Backend:
            is_connected = True
            async def disconnect(inner):
                inner.is_connected = False; entered.set(); await release.wait()
        c._client = Backend()
        task = asyncio.create_task(c._async_disconnect(mark_liveness=True))
        await entered.wait()
        c._closed = True
        events = self.watch(c)
        release.set(); await task
        self.assertTrue(events)
        self.assertTrue(all(not available for _, available in events))

    async def test_advertisement_at_exact_expiry_recovers_without_extra_grace(self):
        c = self.client(); c.async_note_bluetooth_liveness(100)
        self.now = 110
        self.assertFalse(c.available)
        c.async_note_bluetooth_liveness(110)
        self.assertTrue(c.available)
        self.now = 120
        self.assertFalse(c.available)


if __name__ == '__main__':
    unittest.main(verbosity=2)
