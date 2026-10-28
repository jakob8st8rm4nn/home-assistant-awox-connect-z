"""Independent request validation, immutable targets and callback boundaries."""
import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from dataclasses import FrozenInstanceError

if "--source" in sys.argv:
    index = sys.argv.index("--source")
    sys.path.insert(0, sys.argv[index + 1])
    del sys.argv[index:index + 2]
import test_optimization as opt
from awox112.command import sequences as seq
from awox112.command.manager import CommandManager, _PendingVersions
fx = opt.fx


class TwentyMinuteCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_both_public_apis_keep_off_precedence_and_error_order(self):
        on = seq.PlannerState(True, True, "Candle")
        contradictory = dict(color=(120, 75), cct=2700, effect="Candle", stop_effect=True)
        for api in (seq.normalize_request, seq.plan_commands):
            for off in (seq.TargetState(power=False, brightness=-1, **contradictory), seq.TargetState(brightness=0, brightness_transition=-1, **contradictory)):
                result = api(on, off)
                if api is seq.normalize_request:
                    self.assertEqual(result, seq.TargetState(power=False))
                else:
                    self.assertEqual([c.kind for c in result], [seq.CommandKind.POWER_OFF])
            for target, text in ((seq.TargetState(brightness=-1, **contradictory), "Brightness must not be negative"), (seq.TargetState(**contradictory), "Color and CCT cannot both be requested"), (seq.TargetState(effect="Candle", stop_effect=True), "Effect start and explicit effect stop cannot be requested together")):
                with self.assertRaisesRegex(seq.SequenceConflictError, text):
                    api(on, target)
            with self.assertRaises(seq.UnknownPlannerStateError):
                api(seq.PlannerState(False), seq.TargetState(power=False))

    def test_generation_removal_keeps_unrelated_slots_and_frozen_target(self):
        target = seq.TargetState(power=True, brightness=80, brightness_transition=.2, color=(120, 75), appearance_transition=3, effect="Candle")
        versions = _PendingVersions(power=1, brightness=2, appearance=3, effect=4)
        unchanged = CommandManager._without_generation(target, versions, 9)
        self.assertEqual(unchanged, target)
        self.assertEqual((versions.power, versions.brightness, versions.appearance, versions.effect), (1, 2, 3, 4))
        with self.assertRaises(FrozenInstanceError):
            unchanged.power = False
        removed = CommandManager._without_generation(target, versions, 2)
        self.assertIsNone(removed.brightness)
        self.assertIsNone(removed.brightness_transition)
        self.assertEqual(removed.color, (120, 75))
        self.assertEqual(removed.appearance_transition, 3)
        self.assertIsNone(versions.brightness)
        self.assertEqual(target.brightness, 80)

    async def test_worker_completion_starts_only_needed_work_and_resolves_waiters(self):
        for pending, unloading, cancelled in ((False, False, False), (True, False, False), (False, False, True), (True, True, False)):
            client = fx.FakeClient(None)
            entity = fx.AwoxConnectZLight(client, {"mesh_id": fx.MID}, minimum_command_interval_ms=0)
            entity._manager.set_state(fx.S(True, True))
            if pending:
                entity._manager.submit(fx.T(power=False))
            waiter = asyncio.get_running_loop().create_future()
            entity._request_waiters[1] = waiter
            callbacks = []; created = []
            replacement = types.SimpleNamespace(add_done_callback=callbacks.append)
            def create(coro, *args):
                coro.close(); created.append(args); return replacement
            entity.hass = types.SimpleNamespace(async_create_task=create)
            completed = types.SimpleNamespace(cancelled=lambda: cancelled, result=lambda: None)
            entity._command_worker = completed
            entity._entity_unloading = unloading
            entity._command_worker_done(completed)
            self.assertEqual(bool(created), pending and not unloading)
            self.assertEqual(waiter.done(), not pending and not unloading)
            if created:
                self.assertIs(entity._command_worker, replacement)
                self.assertEqual(len(callbacks), 1)
            waiter.cancel()

    def test_retry_limits_keep_normalization_and_validation_priority(self):
        for value, expected in ((1, 1), (2, 2), ("3", 3), (2.9, 2), (True, 1)):
            manager = CommandManager(seq.PlannerState(True, True), max_write_attempts=value)
            self.assertEqual(manager._max_write_attempts, expected)
        for value in (0, -1, "0"):
            with self.assertRaisesRegex(ValueError, "max_write_attempts must be at least 1"):
                CommandManager(seq.PlannerState(True, True), max_write_attempts=value)
        with self.assertRaisesRegex(ValueError, "minimum_interval_ms must be between"):
            CommandManager(seq.PlannerState(True, True), minimum_interval_ms=-1, max_write_attempts="invalid")

    async def test_cloud_error_labels_match_in_setup_and_reconfigure(self):
        cf = opt.config_flow
        cases = ((cf.AwoxInvalidAuth, "invalid_auth"), (cf.AwoxNoDevices, "no_devices"), (cf.AwoxCloudError, "cannot_connect"), (RuntimeError, "unknown"), (type("CustomAuthError", (cf.AwoxInvalidAuth,), {}), "invalid_auth"))
        for reconfigure in (False, True):
            for error_type, expected in cases:
                flow = cf.AwoxConnectZConfigFlow()
                flow.hass = object()
                flow._get_reconfigure_entry = lambda: types.SimpleNamespace(data={cf.CONF_SETUP_METHOD: cf.SETUP_METHOD_CLOUD})
                step = flow.async_step_reconfigure if reconfigure else flow.async_step_cloud
                with patch.object(cf, "async_import_account", AsyncMock(side_effect=error_type("failure"))):
                    result = await step({cf.CONF_EMAIL: "user@example.org", cf.CONF_PASSWORD: "secret"})
                self.assertEqual(result["errors"], {"base": expected})
            flow = cf.AwoxConnectZConfigFlow()
            flow.hass = object()
            flow._get_reconfigure_entry = lambda: types.SimpleNamespace(data={cf.CONF_SETUP_METHOD: cf.SETUP_METHOD_CLOUD})
            step = flow.async_step_reconfigure if reconfigure else flow.async_step_cloud
            with patch.object(cf, "async_import_account", AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await step({cf.CONF_EMAIL: "user@example.org", cf.CONF_PASSWORD: "secret"})

    def test_finalization_preserves_wire_bytes_and_input_mutation(self):
        import random
        rng = random.Random(1912)
        for length in range(15):
            block = bytearray(rng.randbytes(16)); block[1] = length
            before = bytearray(block)
            expected = fx.p.crc8_awox(bytes(before[1:2 + length]))
            result = fx.p.finalize_block(block)
            self.assertEqual(block[0], expected)
            self.assertEqual(block[1:], before[1:])
            self.assertEqual(result, bytes(block))
        with self.assertRaisesRegex(ValueError, "Invalid Connect.Z length marker"):
            fx.p.finalize_block(bytearray([0, 15] + [0] * 14))
        with self.assertRaisesRegex(ValueError, "Connect.Z plaintext must be 16 bytes"):
            fx.p.finalize_block(bytearray(15))


if __name__ == "__main__":
    unittest.main()
