"""State-boundary and metadata contracts shared with the predecessor."""
import itertools
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock

if "--source" in sys.argv:
    index = sys.argv.index("--source")
    sys.path.insert(0, sys.argv[index + 1])
    del sys.argv[index:index + 2]
import test_optimization as opt
fx = opt.fx
cf = opt.config_flow


class TenMinuteCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_late_write_success_cannot_restore_cancelled_finalizer(self):
        for unknown, frozen, newer in itertools.product((False, True), repeat=3):
            manager = fx.CommandManager(fx.S(True, True), minimum_interval_ms=0)
            manager.submit(fx.T(brightness=80))
            if frozen:
                manager.begin_group()
            manager.take_next_command()
            if newer:
                manager.submit(fx.T(color=(120, 70)))
            if unknown:
                manager.set_state(fx.S(False))
            else:
                manager.clear_pending()
            manager.mark_write_succeeded()
            self.assertFalse(manager.power_finalizer_pending)
            self.assertFalse(manager.pending_generations)
            self.assertIsNone(manager.peek_next_command())

    def test_matching_stored_reply_keeps_same_generation_continuation(self):
        manager = fx.CommandManager(fx.S(True, False), minimum_interval_ms=0)
        manager.submit(fx.T(effect="Candle"))
        manager.begin_group()
        manager.take_next_command()
        manager.mark_write_succeeded()
        generation = manager._expected_stored_effect_generation
        self.assertIsNotNone(generation)
        manager.set_state(fx.S(True, False, "Candle"))
        self.assertEqual(manager._expected_stored_effect_generation, generation)
        self.assertEqual(manager.peek_next_command().kind, fx.K.POWER_ON)
        manager.set_state(fx.S(True, True, "Candle"))
        self.assertIsNone(manager._expected_stored_effect_generation)

    async def test_local_extension_keeps_saved_records_separate(self):
        for reconfigure in (False, True):
            old = {"mac": "AA", "mesh_id": 1, "name": "Desk"}
            added = {"mac": "BB", "mesh_id": 2, "name": "New"}
            entry = types.SimpleNamespace(entry_id="entry", data={cf.CONF_DEVICES: [old]})
            flow = cf.AwoxConnectZConfigFlow()
            flow._local_mesh_name = "mesh"; flow._local_mesh_password = "password"
            flow._local_scan_done = True
            flow._first_device_conflict = Mock(return_value=None)
            flow._async_abort_bluetooth_discovery_flows = Mock()
            updates = []
            if reconfigure:
                flow._get_reconfigure_entry = lambda: entry
                flow._local_selected_addresses = {"BB"}
                flow._local_candidates = {"BB": added}
                flow._async_verify_local_selection = AsyncMock(return_value=([added], None, ""))
                flow.async_update_reload_and_abort = lambda item, **kw: updates.append(kw["data_updates"]) or {"type": "saved"}
                await flow.async_step_reconfigure_local_devices({cf.CONF_DEVICES: ["BB"]})
            else:
                flow._matching_local_mesh_entry = lambda: entry
                flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_update_entry=lambda item, **kw: updates.append(kw["data"]), async_reload=AsyncMock()))
                await flow._async_create_local_entry([added])
            merged = updates[0][cf.CONF_DEVICES]
            self.assertEqual(merged, [old, added])
            self.assertIsNot(merged[0], old)
            self.assertIsNot(merged[1], added)
            merged[0]["name"] = "Changed"
            self.assertEqual(old["name"], "Desk")
            self.assertEqual(entry.data[cf.CONF_DEVICES], [old])

    def test_default_none_and_explicit_transition_encode_same_frames(self):
        from awox112.const import DEFAULT_TRANSITION
        builders = ((fx.p.make_brightness, (127,)), (fx.p.make_hs_color, (120, 75)), (fx.p.make_color_temp_kelvin, (2700,)))
        for builder, args in builders:
            default = builder(*args, mesh_id=fx.MID)
            self.assertEqual(default, builder(*args, DEFAULT_TRANSITION, mesh_id=fx.MID))
            self.assertEqual(default, builder(*args, None, mesh_id=fx.MID))


if __name__ == "__main__":
    unittest.main()
