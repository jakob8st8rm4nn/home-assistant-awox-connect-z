"""Entity publication and exported source regressions (F02/F04 only)."""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch
import test_optimization as opt
from awox112 import client as cm
from awox112 import diagnostics as dm


class SmallDiagnosticFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.
        self.clock = patch.object(cm, 'time', types.SimpleNamespace(monotonic=lambda: self.now))
        self.clock.start()
        hass = opt.fx.Hass()
        hass.async_create_background_task = lambda coro, *a, **kw: asyncio.create_task(coro)
        self.c = opt.fx.AwoxConnectZClient(hass, opt.fx.MAC, 'mesh', 'password',
            default_transition=.2, idle_disconnect=20, availability_timeout=10,
            command_semaphore=asyncio.Semaphore(1), max_concurrent_commands=1,
            configured_mesh_id=opt.fx.MID)
        self.info = None
        self.c._latest_service_info = lambda: self.info
        self.c.bluetooth_source_name = lambda source: None if source is None else 'Name:'+source
        self.e = opt.fx.AwoxConnectZLight(self.c, {'mesh_id':opt.fx.MID}, minimum_command_interval_ms=250)
        self.e.async_get_last_state = AsyncMock(return_value=None)
        self.removers = []
        self.e.async_on_remove = self.removers.append
        self.writes = []
        self.e.async_write_ha_state = lambda: self.writes.append((self.e.available, self.e.extra_state_attributes['ble_connected']))
        with patch.object(opt.fx.Entity, 'async_added_to_hass', AsyncMock(), create=True):
            await self.e.async_added_to_hass()

    async def asyncTearDown(self):
        for remove in self.removers: remove()
        self.c._client = None
        await self.c.async_close()
        self.clock.stop()

    def connect(self):
        class Backend:
            is_connected = True
            _connected_scanner = types.SimpleNamespace(source='link-proxy')
            async def disconnect(inner): inner.is_connected = False
        self.c._client = Backend()
        self.c._async_note_connection_liveness()

    async def test_export_remains_consistent_across_timeout_boundary(self):
        self.c.availability_timeout = 30
        self.c._availability.advertisement_time = 70.003
        clock = types.SimpleNamespace(monotonic=unittest.mock.Mock(
            side_effect=(100., 100.004, 100.008)))
        with patch.object(cm, 'time', clock), patch.object(dm, 'time', clock):
            data = self.c.diagnostics()
        self.assertTrue(data['available'])
        self.assertEqual(data['bluetooth_status'], 'visible')

    async def test_export_preserves_publication_snapshot_and_closed_guard(self):
        self.c._availability.advertisement_time = 1.
        for status in (None, 'visible', 'recently_connected', 'unreachable'):
            for closed in (False, True):
                with self.subTest(status=status, closed=closed):
                    self.c._availability.snapshot_active = True
                    self.c._availability.snapshot_status = status
                    self.c._closed = closed
                    data = self.c.diagnostics()
                    self.assertEqual(data['bluetooth_status'], status)
                    self.assertEqual(data['available'],
                        not closed and status in ('visible', 'recently_connected'))
        self.c._closed = False
        self.c._availability.snapshot_active = False

    async def test_export_refreshes_history_once_and_retains_fields(self):
        self.info = types.SimpleNamespace(time=100., source='scan-proxy', rssi=-65)
        with patch.object(self.c, '_refresh_last_seen_from_ha',
                          wraps=self.c._refresh_last_seen_from_ha) as refresh:
            with patch.object(dm, 'time', types.SimpleNamespace(monotonic=lambda: self.now)):
                data = self.c.diagnostics()
        refresh.assert_called_once_with()
        self.assertEqual(data['last_bluetooth_source_id'], 'scan-proxy')
        self.assertEqual(data['signal_strength'], -65)
        self.assertEqual(data['last_seen'], self.c._availability.last_seen.isoformat())
        self.assertTrue(data['available'])
        self.assertEqual(data['bluetooth_status'], 'visible')

    async def test_empty_waiters_do_not_read_pending_generations(self):
        with patch.object(type(self.e._manager), 'pending_generations',
                          new_callable=unittest.mock.PropertyMock) as pending:
            self.e._resolve_completed_waiters()
        pending.assert_not_called()

    async def test_waiter_resolution_keeps_active_and_completes_removed(self):
        active, removed, done, cancelled = (
            asyncio.get_running_loop().create_future() for _ in range(4))
        done.set_result(None)
        cancelled.cancel()
        self.e._request_waiters = {1: active, 2: removed, 3: done, 4: cancelled}
        with patch.object(type(self.e._manager), 'pending_generations',
                          new_callable=unittest.mock.PropertyMock,
                          return_value=frozenset((1, 3, 4))):
            self.e._resolve_completed_waiters()
        self.assertEqual(self.e._request_waiters, {1: active})
        self.assertFalse(active.done())
        self.assertIsNone(removed.result())
        self.assertTrue(cancelled.cancelled())
        active.cancel()

    async def test_regular_disconnect_publishes_false_while_available(self):
        self.connect(); self.writes.clear()
        await self.c._async_disconnect(mark_liveness=True)
        self.assertEqual(self.writes, [(True,False)])
        self.assertEqual(self.c.bluetooth_status, 'recently_connected')

    async def test_connection_publishes_true_while_already_available(self):
        self.c.async_note_bluetooth_liveness(100); self.writes.clear()
        self.connect()
        self.assertEqual(self.writes, [(True,True)])

    async def test_unexpected_loss_detected_by_worker_updates_attribute(self):
        self.connect(); self.writes.clear()
        changed = asyncio.Event()
        def write():
            self.writes.append((self.e.available,self.e.extra_state_attributes['ble_connected']))
            if not self.c.connected: changed.set()
        self.e.async_write_ha_state = write
        with patch.object(cm, 'AVAILABILITY_RECOVERY_POLL_SECONDS', .01):
            self.c.async_start_availability_tracking()
            await asyncio.sleep(0)
            self.c._client.is_connected = False
            await asyncio.wait_for(changed.wait(), 1)
        self.assertEqual(self.writes, [(True,False)])

    async def test_unchanged_diagnostics_do_not_rewrite_light(self):
        self.connect(); self.writes.clear()
        for _ in range(20): self.c._async_notify_diagnostic_listeners()
        self.assertEqual(self.writes, [])

    async def test_busy_light_suppresses_available_true_publication(self):
        self.e._executor.busy = True
        self.writes.clear()
        self.c.async_note_bluetooth_liveness(100)
        self.assertEqual(self.writes, [])

    async def test_busy_light_still_publishes_unavailable(self):
        self.c.async_note_bluetooth_liveness(100)
        self.writes.clear()
        self.e._executor.busy = True
        self.now = 111
        self.c._async_notify_diagnostic_listeners()
        self.assertEqual(self.writes, [(False, False)])

    async def test_removal_unsubscribes_connection_listener(self):
        for remove in self.removers: remove()
        self.removers.clear(); self.writes.clear()
        self.connect()
        self.assertEqual(self.writes, [])

    async def test_export_last_advertisement_source_matches_sensor_during_connection(self):
        self.info = types.SimpleNamespace(time=100., source='scan-proxy', rssi=-65)
        self.connect()
        self.c._async_set_last_connection_source('link-proxy')
        description = next(d for d in opt.sensor.SENSOR_DESCRIPTIONS if d.key=='current_bluetooth_source')
        sensor = opt.sensor.AwoxConnectZDiagnosticSensor(self.c, {'mesh_id':opt.fx.MID}, description)
        data = self.c.diagnostics()
        self.assertEqual(data['last_bluetooth_source'], sensor.native_value)
        self.assertEqual(data['last_bluetooth_source_id'], 'scan-proxy')
        self.assertEqual(data['signal_strength'], -65)
        self.assertEqual(self.c.last_connection_source_id, 'link-proxy')

    async def test_export_last_advertisement_source_ignores_unknown_link_source(self):
        self.info = types.SimpleNamespace(time=100., source='scan-proxy', rssi=-65)
        self.c._client = types.SimpleNamespace(is_connected=True)
        data = self.c.diagnostics()
        self.assertEqual(data['last_bluetooth_source_id'], 'scan-proxy')
        self.assertEqual(data['last_bluetooth_source'], 'Name:scan-proxy')
        self.assertEqual(data['signal_strength'], -65)

    async def test_export_source_and_rssi_remain_after_reachability_expiry(self):
        self.info = types.SimpleNamespace(time=100., source='scan-proxy', rssi=-65)
        self.assertEqual(self.c.diagnostics()['last_bluetooth_source_id'], 'scan-proxy')
        self.now = 105; self.c._async_note_connection_liveness()
        for now,status in ((110,'recently_connected'), (115,'unreachable')):
            self.now = now
            self.assertEqual(self.c.bluetooth_status, status)
            data = self.c.diagnostics()
            self.assertEqual(data['last_bluetooth_source_id'], 'scan-proxy')
            self.assertEqual(data['signal_strength'], -65)


if __name__ == '__main__': unittest.main(verbosity=2)
