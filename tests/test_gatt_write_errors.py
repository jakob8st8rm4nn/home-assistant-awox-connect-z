"""Preserve uncertain-write classification, timestamps and cancellation."""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import test_optimization as opt
from awox112 import client as cm

fx = opt.fx


class GattWriteErrors(unittest.IsolatedAsyncioTestCase):
    def fixture(self, failure=None, *, missing_session=False):
        clock = [100.]
        events = []
        c = cm.AwoxConnectZClient(fx.Hass(), fx.MAC, 'mesh', 'password',
            default_transition=.2, idle_disconnect=10,
            command_semaphore=asyncio.Semaphore(1), max_concurrent_commands=1,
            configured_mesh_id=fx.MID)

        class BLE:
            is_connected = True

            async def write_gatt_char(self, uuid, packet, **kw):
                clock[0] = 101.
                events.append(('write', kw))
                if failure == 'wait':
                    await asyncio.Event().wait()
                elif failure is not None:
                    raise failure

        c._client = BLE()
        c._session_key = None if missing_session else fx.KEY
        c._latest_service_info = lambda: None
        c._async_ensure_connected = AsyncMock()
        c._async_enable_status = AsyncMock()
        c._schedule_idle_disconnect = Mock()
        record = c._record_error

        def record_error(error, **kw):
            events.append(('error', clock[0], str(error), kw))
            record(error, **kw)

        async def cleanup(**kw):
            events.append(('cleanup', clock[0], kw))
            clock[0] = 150.

        c._record_error = record_error
        c._async_disconnect = AsyncMock(side_effect=cleanup)
        return c, clock, events

    async def send(self, c, clock, frame=bytes(16), attempt=2):
        with patch.object(cm, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])):
            return await c.async_send_selected_once(
                lambda: cm.RuntimeSelectedCommand(frame, 'test', attempt))

    async def test_timeout_keeps_uncertain_result_and_pre_cleanup_timestamp(self):
        cause = TimeoutError('backend timeout')
        c, clock, events = self.fixture(cause)
        result = await self.send(c, clock)
        self.assertIs(result.status, cm.RuntimeCommandStatus.WRITE_UNCERTAIN)
        self.assertEqual(result.write_finished_monotonic, 101.)
        self.assertEqual(str(result.error), 'GATT write timed out after 4.0s')
        self.assertIs(result.error.__cause__, cause)
        self.assertEqual((c.last_error_category, c.last_error_stage,
            c.last_error_command, c.last_error_write_attempt),
            ('timeout', 'gatt_write', 'test', 2))
        self.assertEqual([e[0] for e in events], ['write', 'error', 'cleanup'])
        self.assertEqual(events[-1][1], 101.)
        self.assertEqual(clock[0], 150.)
        c._async_disconnect.assert_awaited_once_with(cancel_idle=True)

    async def test_other_errors_keep_message_and_classification(self):
        for cause, category in ((RuntimeError('failed'), 'write'),
                (RuntimeError('transport timed out'), 'timeout'),
                (ValueError('bad transport'), 'write')):
            with self.subTest(cause=cause):
                c, clock, events = self.fixture(cause)
                result = await self.send(c, clock)
                self.assertIs(result.status, cm.RuntimeCommandStatus.WRITE_UNCERTAIN)
                self.assertEqual(str(result.error), f"Command 'test' write failed: {cause}")
                self.assertIs(result.error.__cause__, cause)
                self.assertEqual(c.last_error_category, category)
                self.assertEqual(result.write_finished_monotonic, 101.)
                self.assertEqual([e[0] for e in events], ['write', 'error', 'cleanup'])

    async def test_real_asyncio_write_timeout_uses_same_path(self):
        c, clock, events = self.fixture('wait')
        with patch.object(cm, 'RUNTIME_WRITE_TIMEOUT_SECONDS', .01):
            result = await self.send(c, clock)
        self.assertIs(result.status, cm.RuntimeCommandStatus.WRITE_UNCERTAIN)
        self.assertIsInstance(result.error.__cause__, TimeoutError)
        self.assertEqual(c.last_error_category, 'timeout')
        self.assertEqual(result.write_finished_monotonic, 101.)
        self.assertEqual([e[0] for e in events], ['write', 'error', 'cleanup'])

    async def test_task_cancellation_propagates_and_cleans_up(self):
        c, clock, events = self.fixture('wait')
        with patch.object(cm, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])):
            task = asyncio.create_task(c.async_send_selected_once(
                lambda: cm.RuntimeSelectedCommand(bytes(16), 'test')))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([e[0] for e in events], ['write', 'cleanup'])
        self.assertIsNone(c.last_error)
        c._async_disconnect.assert_awaited_once_with(cancel_idle=True)

    async def test_missing_session_remains_confirmed_write_failure(self):
        c, clock, events = self.fixture(missing_session=True)
        result = await self.send(c, clock)
        self.assertIs(result.status, cm.RuntimeCommandStatus.WRITE_FAILED)
        self.assertEqual(c.last_error_stage, 'gatt_write_preparation')
        self.assertNotIn('write', [e[0] for e in events])

    async def test_packet_error_remains_final_without_disconnect(self):
        c, clock, events = self.fixture()
        result = await self.send(c, clock, frame=b'short')
        self.assertIs(result.status, cm.RuntimeCommandStatus.WRITE_FAILED_FINAL)
        self.assertEqual(c.last_error_category, 'protocol')
        c._async_disconnect.assert_not_awaited()
        c._schedule_idle_disconnect.assert_called_once()
        self.assertNotIn('write', [e[0] for e in events])

    async def test_preparation_failure_never_selects_or_writes(self):
        c, clock, events = self.fixture()
        c._async_ensure_connected.side_effect = cm.AwoxAuthenticationError('bad credentials')
        selector = Mock(return_value=cm.RuntimeSelectedCommand(bytes(16), 'test'))
        with patch.object(cm, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])):
            result = await c.async_send_selected_once(selector)
        self.assertIs(result.status, cm.RuntimeCommandStatus.PREPARATION_FAILED)
        self.assertEqual(c.last_error_category, 'authentication')
        selector.assert_not_called()
        self.assertNotIn('write', [e[0] for e in events])

    async def test_success_and_unselected_paths_stay_separate(self):
        c, clock, events = self.fixture()
        result = await self.send(c, clock)
        self.assertIs(result.status, cm.RuntimeCommandStatus.SUCCESS)
        c._async_disconnect.assert_not_awaited()
        c, clock, events = self.fixture()
        with patch.object(cm, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])):
            result = await c.async_send_selected_once(lambda: None)
        self.assertIs(result.status, cm.RuntimeCommandStatus.NOT_SELECTED)
        self.assertEqual(events, [])
        c._async_disconnect.assert_not_awaited()


if __name__ == '__main__':
    unittest.main(verbosity=2)
