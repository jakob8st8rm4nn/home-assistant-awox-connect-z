"""Contracts for local preparation, generation sets and optional status goals."""
import types
import unittest
from dataclasses import replace

import test_optimization as opt
from awox112.command.executor import (
    CommandExecutor, CommandExecutorError, EffectIdentityRequiredError,
    UnsupportedEffectError,
)
from awox112.command.manager import CommandManager
from awox112.command.sequences import CommandKind as K, PlannedCommand, PlannerState, TargetState
from awox112.status_1911 import Status1911State


class AdditionalCleanupTests(unittest.TestCase):
    def setUp(self):
        self.manager = CommandManager(PlannerState(True, True), minimum_interval_ms=0)
        self.executor = CommandExecutor(
            types.SimpleNamespace(default_transition=.2), self.manager, mesh_id=0x1234,
        )

    def test_power_and_native_effect_wire_contracts(self):
        cases = (
            (K.POWER_ON, None, '03061234010600010000000000000000', 'power:on'),
            (K.POWER_OFF, None, '64061234010600000000000000000000', 'power:off'),
            (K.EFFECT_START, 'Candle', '3a081234010800100101000000000000', 'effect:candle:start'),
            (K.EFFECT_STOP, 'Candle', '49081234010800100001000000000000', 'effect:candle:stop'),
            (K.EFFECT_START, 'Color cycle', '530912340100034101000c0000000000', 'effect:color_cycle:start'),
            (K.EFFECT_STOP, 'Color cycle', '11061234010003470000000000000000', 'effect:color_cycle:stop'),
        )
        for kind, value, frame, label in cases:
            command = PlannedCommand(kind, value)
            result = self.executor._prepare_command(command)
            self.assertIs(result.command, command)
            self.assertEqual((result.frame.hex(), result.label), (frame, label))

    def test_effect_aliases_and_error_types_remain_distinct(self):
        for kind in (K.EFFECT_START, K.EFFECT_STOP):
            for canonical, aliases in (
                ('Color cycle', ('color_cycle', 'COLOR CYCLE', ' color-cycle ')),
                ('Candle', ('candle', 'CANDLE', ' candle ')),
            ):
                expected = self.executor._prepare_command(PlannedCommand(kind, canonical))
                for alias in aliases:
                    actual = self.executor._prepare_command(PlannedCommand(kind, alias))
                    self.assertEqual((actual.frame, actual.label), (expected.frame, expected.label))
            with self.assertRaisesRegex(UnsupportedEffectError, 'Unsupported AwoX native effect'):
                self.executor._prepare_command(PlannedCommand(kind, 'invalid'))
        with self.assertRaises(EffectIdentityRequiredError):
            self.executor._prepare_command(PlannedCommand(K.EFFECT_STOP))
        with self.assertRaises(UnsupportedEffectError):
            self.executor._prepare_command(PlannedCommand(K.EFFECT_START))

    def test_raw_kind_strings_do_not_become_valid_commands(self):
        for kind in ('power_on', 'power_off', 'effect_start', 'effect_stop'):
            with self.assertRaisesRegex(CommandExecutorError, 'Unsupported command kind'):
                self.executor._prepare_command(PlannedCommand(kind, 'Candle'))

    def test_pending_generations_keep_queue_inflight_and_confirmation(self):
        manager = self.manager
        manager.submit(TargetState(brightness=80))
        manager.begin_group()
        manager.take_next_command()
        manager.submit(TargetState(cct=2700))
        pending = manager.pending_generations
        self.assertIsInstance(pending, frozenset)
        self.assertEqual(pending, frozenset((1, 2)))
        manager.mark_write_succeeded()
        self.assertEqual(manager.pending_generations, pending)
        self.assertIn(1, manager.last_write_generations)
        manager.cancel_generation(2)
        self.assertEqual(pending, frozenset((1, 2)))
        self.assertNotIn(2, manager.pending_generations)
        self.assertIn(1, manager.pending_generations)

    def test_optional_goal_fields_preserve_none_and_false(self):
        state = Status1911State(1, 10., 1, 0x1234, 'aabbccdd', 1, True, 80, False, None, 250, 0, 128)
        self.assertTrue(state.matches({'effect': None, 'color_mode': False, 'brightness': 81}))
        self.assertFalse(state.matches({'power': None}))
        self.assertFalse(state.matches({'brightness': None}))
        self.assertFalse(state.matches({'effect': 'Candle'}))
        self.assertTrue(replace(state, brightness=None).matches({'brightness': None}))
        self.assertFalse(replace(state, brightness=None).matches({'brightness': 0}))
        self.assertTrue(state.matches({'missing': None}))
        self.assertFalse(state.matches({'missing': False}))

    def test_hue_wrap_and_tolerance_stay_independent_of_optional_fields(self):
        state = Status1911State(1, 10., 1, 0x1234, 'aabbccdd', 3, True, 80, True, None, None, 254, 128)
        self.assertTrue(state.matches({'hue': 0, 'mired': None, 'effect': None}))
        self.assertTrue(state.matches({'hue': 253}))
        self.assertFalse(state.matches({'hue': 2}))
        self.assertFalse(replace(state, hue=255).matches({'hue': 255}))


class ConvergenceCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_unbounded_command_budget_stays_unbounded(self):
        import asyncio
        client = opt.fx.AwoxConnectZClient(
            opt.fx.Hass(), opt.fx.MAC, 'mesh', 'password', default_transition=0,
            idle_disconnect=10, command_semaphore=asyncio.Semaphore(1),
            max_concurrent_commands=1, configured_mesh_id=opt.fx.MID,
        )
        async with asyncio.timeout(None) as budget:
            await client._async_acquire_runtime_connect_lock(budget)
            self.assertTrue(client._runtime_connect_lock.locked())
            self.assertIsNone(budget.when())
            client._runtime_connect_lock.release()
        await client.async_close()

    async def test_queue_timeout_restores_active_command_deadline(self):
        import asyncio
        from unittest.mock import patch
        from awox112 import client as cm
        client = opt.fx.AwoxConnectZClient(
            opt.fx.Hass(), opt.fx.MAC, 'mesh', 'password', default_transition=0,
            idle_disconnect=10, command_semaphore=asyncio.Semaphore(1),
            max_concurrent_commands=1, configured_mesh_id=opt.fx.MID,
        )
        await client._runtime_connect_lock.acquire()
        async with asyncio.timeout(2) as budget:
            before = budget.when()
            with patch.object(cm, 'RUNTIME_CONNECT_QUEUE_TIMEOUT_SECONDS', .001):
                with self.assertRaisesRegex(cm.AwoxConnectZError, 'runtime BLE connection queue'):
                    await client._async_acquire_runtime_connect_lock(budget)
            self.assertGreater(budget.when(), before)
            self.assertFalse(budget.expired())
        client._runtime_connect_lock.release()
        await client.async_close()

    async def test_worker_handles_empty_failure_and_unload_boundaries(self):
        import asyncio
        from awox112.command.executor import CommandExecutionFailed
        for mode in ('empty', 'failure', 'unload'):
            entity = opt.fx.AwoxConnectZLight(
                opt.fx.FakeClient(None), {'mesh_id': opt.fx.MID}, minimum_command_interval_ms=0,
            )
            manager = entity._manager
            manager.set_state(PlannerState(True, True))
            manager.submit(TargetState(power=False))
            first = asyncio.get_running_loop().create_future()
            entity._request_waiters[1] = first
            writes = []; calls = []; later = []
            entity.async_write_ha_state = lambda: writes.append('write')
            async def execute():
                calls.append('group')
                self.assertLessEqual(len(calls), 2)
                if len(calls) == 1:
                    if mode == 'empty':
                        return ()
                    manager.cancel_generation(1)
                    if mode == 'failure':
                        manager.submit(TargetState(brightness=80))
                        later.append(asyncio.get_running_loop().create_future())
                        entity._request_waiters[2] = later[0]
                    else:
                        entity._entity_unloading = True
                    raise CommandExecutionFailed('failed', failed_generations=frozenset((1,)), uncertain=True)
                if mode == 'failure':
                    self.assertTrue(first.done())
                    self.assertFalse(later[0].done())
                    manager.cancel_generation(2)
                else:
                    self.assertFalse(first.done())
                    manager.cancel_generation(1)
                return ()
            entity._executor = types.SimpleNamespace(async_execute_group=execute, last_confirmed_state=None)
            await entity._async_command_worker()
            self.assertTrue(first.done())
            if mode == 'empty':
                self.assertIsNone(first.result())
                self.assertEqual(writes, [])
            else:
                self.assertIsInstance(first.exception(), CommandExecutionFailed)
                self.assertEqual(writes, ['write'])
            if later:
                self.assertTrue(later[0].done())
                self.assertIsNone(later[0].result())
            self.assertEqual(len(calls), 1 if mode == 'unload' else 2)

    async def test_verification_cancellation_finishes_protected_cleanup(self):
        import asyncio
        for cancel_twice in (False, True):
            client = opt.fx.AwoxConnectZClient(
                opt.fx.Hass(), opt.fx.MAC, 'mesh', 'password', default_transition=0,
                idle_disconnect=10, command_semaphore=asyncio.Semaphore(1),
                max_concurrent_commands=1, configured_mesh_id=opt.fx.MID,
            )
            authenticating = asyncio.Event()
            disconnecting = asyncio.Event()
            finish = asyncio.Event()
            disconnects = []
            class Backend:
                is_connected = True
                async def disconnect(inner):
                    disconnects.append('disconnect')
                    disconnecting.set()
                    await finish.wait()
                    inner.is_connected = False
            async def ensure(**kwargs):
                client._client = Backend()
                client._session_key = opt.fx.KEY
                client.status_1911.begin(1)
                authenticating.set()
                await asyncio.Event().wait()
            client._async_ensure_connected = ensure
            task = asyncio.create_task(client.async_verify_mesh_credentials())
            await asyncio.wait_for(authenticating.wait(), 1)
            task.cancel()
            await asyncio.wait_for(disconnecting.wait(), 1)
            if cancel_twice:
                task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(disconnects, ['disconnect'])
            self.assertIsNone(client._client)
            self.assertIsNone(client._session_key)
            self.assertFalse(client.status_1911.active)
            self.assertFalse(client._command_lock.locked())
            self.assertFalse(client._command_semaphore.locked())
            self.assertIsNone(client.last_error)
            await client.async_close()

    def test_flow_read_helpers_keep_record_ownership(self):
        flow = opt.config_flow.AwoxConnectZConfigFlow()
        original = {'mac': 'AA', 'mesh_id': 1, 'name': 'Desk'}
        flow._pending_cloud_data = {opt.config_flow.CONF_DEVICES: [original]}
        flow._configured_addresses = lambda **kwargs: set()
        usable, conflicts = flow._cloud_review_devices()
        self.assertEqual(conflicts, [])
        usable[0]['name'] = 'Changed'
        self.assertEqual(original['name'], 'Desk')
        self.assertEqual(flow._pending_cloud_data[opt.config_flow.CONF_DEVICES][0]['name'], 'Desk')


class TargetedCleanupTests(unittest.TestCase):
    def test_reconfigure_candidate_stays_independent_of_saved_device(self):
        flow = opt.config_flow.AwoxConnectZConfigFlow()
        original = {'mac': 'AA', 'mesh_id': 1, 'name': 'Desk', 'device_type': 'saved'}
        flow._local_reconfigure_mode = True
        flow._get_reconfigure_entry = lambda: types.SimpleNamespace(
            data={opt.config_flow.CONF_DEVICES: [original]})
        copied = flow._current_reconfigure_device('AA')
        self.assertIsNot(copied, original)
        flow._remember_local_candidate('AA', {'mesh_id': 2, 'name': 'Discovered'})
        candidate = flow._local_candidates['AA']
        self.assertIsNot(candidate, original)
        self.assertEqual(candidate['name'], 'Desk')
        self.assertEqual(candidate['mesh_id'], 1)
        self.assertEqual(candidate['device_type'], 'local_existing_mesh_id_conflict')
        self.assertEqual(flow._local_mesh_id_conflicts['AA'], (1, 2))
        candidate['name'] = 'Changed'
        self.assertEqual(original, {'mac': 'AA', 'mesh_id': 1, 'name': 'Desk', 'device_type': 'saved'})

    def test_empty_discovery_addresses_do_not_query_flows(self):
        from unittest.mock import Mock
        flow = opt.config_flow.AwoxConnectZConfigFlow()
        flow._async_in_progress = Mock(side_effect=AssertionError('No lookup needed'))
        flow._async_abort_bluetooth_discovery_flows(set())
        flow._async_in_progress.assert_not_called()

    def test_discovery_abort_keeps_source_self_and_address_filters(self):
        from unittest.mock import Mock
        flow = opt.config_flow.AwoxConnectZConfigFlow()
        flow.flow_id = 'self'
        rows = [
            {'flow_id': 'self', 'context': {'source': 'bluetooth', 'unique_id': 'AA'}},
            {'flow_id': 'matching', 'context': {'source': 'bluetooth', 'unique_id': 'aa'}},
            {'flow_id': 'other_address', 'context': {'source': 'bluetooth', 'unique_id': 'BB'}},
            {'flow_id': 'other_source', 'context': {'source': 'user', 'unique_id': 'AA'}},
            {'flow_id': 'no_context'},
        ]
        flow._async_in_progress = Mock(return_value=rows)
        abort = Mock()
        flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
            flow=types.SimpleNamespace(async_abort=abort)))
        flow._async_abort_bluetooth_discovery_flows({'aA'})
        flow._async_in_progress.assert_called_once_with(include_uninitialized=True)
        abort.assert_called_once_with('matching')

    def test_pending_target_presence_and_boolean_result(self):
        import itertools
        fields = ('power', 'brightness', 'color', 'cct', 'effect', 'stop_effect')
        for values in itertools.product((None, False, 1), repeat=len(fields)):
            target = TargetState(**dict(zip(fields, values)))
            expected = any(value is not None for value in values[:-1]) or bool(values[-1])
            actual = CommandManager._has_pending_targets(target)
            self.assertIs(type(actual), bool)
            self.assertIs(actual, expected)


class SensorEvidenceCleanupTests(unittest.TestCase):
    def test_rssi_conversion_and_history_semantics(self):
        from datetime import datetime, timezone
        from awox112.availability import AvailabilityState
        class RSSI:
            calls = 0
            def __int__(self):
                self.calls += 1
                return -60
        clock = datetime(2026, 10, 26, tzinfo=timezone.utc)
        state = AvailabilityState(advertisement_time=100, rssi=-70)
        rssi = RSSI()
        self.assertTrue(state.note_advertisement(100, source=None, rssi=rssi,
            clock=lambda: 100, utcnow=lambda: clock))
        self.assertEqual((rssi.calls, state.rssi), (1, -60))
        self.assertFalse(state.note_advertisement(100, source=None, rssi=-60.,
            clock=lambda: 100, utcnow=lambda: clock))
        self.assertFalse(state.note_advertisement(99, source=None, rssi=-40,
            clock=lambda: 100, utcnow=lambda: clock))
        self.assertEqual(state.rssi, -60)
        self.assertFalse(state.note_advertisement(100, source=None, rssi=None,
            clock=lambda: 100, utcnow=lambda: clock))

    def test_mesh_signature_preserves_change_decisions(self):
        import itertools
        description = next(d for d in opt.sensor.SENSOR_DESCRIPTIONS if d.key == 'mesh_id')
        signatures = []
        for configured, advertised, status in itertools.product(
                (1, 2, 3), (None, 1, 2), ('unknown', 'match', 'mismatch')):
            client = types.SimpleNamespace(mac=opt.fx.MAC, advertised_mesh_id=advertised,
                                           mesh_id_status=status, closed=False)
            entity = opt.sensor.AwoxConnectZDiagnosticSensor(
                client, {'mesh_id': configured}, description)
            previous = entity.native_value, tuple(sorted(entity.extra_state_attributes.items()))
            signatures.append((previous, entity._publish_signature()))
        for left, right in itertools.product(signatures, repeat=2):
            self.assertEqual(left[0] == right[0], left[1] == right[1])
        writes = []
        entity.async_write_ha_state = lambda: writes.append(entity._publish_signature())
        entity._async_diagnostics_changed()
        entity._async_diagnostics_changed()
        self.assertEqual(len(writes), 1)
        client.advertised_mesh_id = 100
        entity._async_diagnostics_changed()
        client.mesh_id_status = 'match'
        entity._async_diagnostics_changed()
        self.assertEqual(len(writes), 3)

    def test_uncertain_failure_keeps_all_generation_owners(self):
        from awox112.command.manager import WriteFailureAction
        manager = CommandManager(PlannerState(True, True), minimum_interval_ms=0)
        manager.submit(TargetState(brightness=80))
        manager.take_next_command()
        manager.submit(TargetState(cct=2700))
        result = manager.mark_write_failed(uncertain=True)
        self.assertIs(type(result.failed_generations), frozenset)
        self.assertEqual(result.failed_generations, frozenset((1, 2)))
        self.assertIs(result.action, WriteFailureAction.ABORTED_UNCERTAIN)
        self.assertFalse(manager.state.known)
        self.assertFalse(manager.pending_generations)

    def test_cloud_duplicates_preserve_first_usable_record(self):
        def row(address, name):
            return {'type': '.ble.light', 'macAddress': 'AA:BB:CC:DD:EE:FF',
                    'address': address, 'displayName': name}
        for invalid in (0, None, 65535, 'bad'):
            result = opt.cloud._convert_devices([
                row(invalid, 'invalid'), row(100, 'first'), row(200, 'second')])
            self.assertEqual([(d['mesh_id'], d['name']) for d in result], [(100, 'first')])
        result = opt.cloud._convert_devices([row(100, 'first'), row('bad', 'invalid')])
        self.assertEqual([(d['mesh_id'], d['name']) for d in result], [(100, 'first')])


class PowerCompletionCleanupTests(unittest.TestCase):
    def test_power_completion_preserves_exact_booleans_and_generation_boundary(self):
        from awox112.command.manager import _PendingVersions
        for kind in (K.POWER_ON, K.POWER_OFF):
            for power in (None, False, True, 0, 1):
                for version in (None, 1, 2, 3):
                    for cutoff in (0, 1, 2, 3):
                        with self.subTest(kind=kind, power=power, version=version, cutoff=cutoff):
                            pending = TargetState(power=power, brightness=80,
                                brightness_transition=.2, color=(30., 70.),
                                appearance_transition=.5, effect='Candle')
                            versions = _PendingVersions(power=version, brightness=4,
                                                        appearance=5, effect=6)
                            result, returned_versions = CommandManager._pending_after_success(
                                pending, versions, PlannedCommand(kind), cutoff)
                            complete = (power is (kind is K.POWER_ON)
                                        and version is not None and version <= cutoff)
                            self.assertIs(result.power, None if complete else power)
                            self.assertEqual(returned_versions.power, None if complete else version)
                            self.assertIs(returned_versions, versions)
                            self.assertEqual(replace(result, power=pending.power), pending)
                            self.assertEqual((versions.brightness, versions.appearance, versions.effect),
                                             (4, 5, 6))


if __name__ == '__main__':
    unittest.main()
