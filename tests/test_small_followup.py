"""Public flow and manager contracts shared with the preceding draft."""
import itertools
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock

if "--source" in sys.argv:
    index = sys.argv.index("--source")
    sys.path.insert(0, sys.argv[index + 1])
    del sys.argv[index:index + 2]
import test_optimization as opt
cf = opt.config_flow
mm = opt.fx.CommandManager


def generation_trace():
    result = []
    for values in itertools.product((None, 1, 3), repeat=6):
        manager = mm(opt.fx.S(True, True), minimum_interval_ms=0)
        for field, value in zip(("power", "brightness", "appearance", "effect"), values):
            setattr(manager._pending_versions, field, value)
        manager._power_finalizer_generation = values[4]
        manager._in_flight_generation = values[5]
        manager._confirmation_generations = {2}
        manager._queued_versions.power = 4
        expected = {v for v in values if v is not None}
        assert manager.write_generations == expected
        assert manager.pending_generations == expected | {2, 4}
        result.append(sorted(manager.pending_generations))
    return result


async def credentials_trace():
    result = []
    for reconfigure, name, password in itertools.product(
        (False, True), (" mesh ", "", "x" * 17, "ä" * 9, "\ud800"),
        (" password ", "", "x" * 17, "ä" * 8),
    ):
        flow = cf.AwoxConnectZConfigFlow()
        flow._get_reconfigure_entry = lambda: types.SimpleNamespace(data={cf.CONF_DEVICES: [{"mac": "AA"}]})
        flow._async_verify_credentials = AsyncMock(return_value=None)
        target = AsyncMock(return_value={"type": "next"})
        if reconfigure:
            flow.async_step_reconfigure_local_devices = target
            step = flow.async_step_reconfigure_local
        else:
            flow.async_step_local_devices = target
            step = flow.async_step_local
        response = await step({cf.CONF_MESH_NAME: name, cf.CONF_MESH_PASSWORD: password})
        expected = {}
        for field, value in ((cf.CONF_MESH_NAME, name.strip()), (cf.CONF_MESH_PASSWORD, password)):
            error = flow._mesh_credential_error(value)
            if error:
                expected[field] = error
        assert response.get("errors", {}) == expected
        if expected:
            assert target.await_count == 0
            assert flow._async_verify_credentials.await_count == 0
        else:
            assert target.await_count == 1
            assert flow._local_mesh_name == name.strip()
            assert flow._local_mesh_password == password
        result.append((response.get("errors", {}), flow._local_mesh_name, flow._local_mesh_password))
    return result


async def scan_trace():
    result = []
    for reconfigure in (False, True):
        flow = cf.AwoxConnectZConfigFlow()
        flow._local_mesh_name = "mesh"
        flow._local_mesh_password = "password"
        flow._local_scan_done = True
        flow._local_scan_error = True
        flow._get_reconfigure_entry = lambda: types.SimpleNamespace(data={cf.CONF_DEVICES: []})
        for field in ("_local_failed_device", "_local_conflict_entered_id", "_local_conflict_advertised_id", "_local_unavailable_devices", "_local_rejected_devices"):
            setattr(flow, field, "old")
        step = flow.async_step_reconfigure_local_devices if reconfigure else flow.async_step_local_devices
        first = await step()
        assert first["errors"] == {"base": "active_scan_failed"}
        assert not flow._local_scan_error
        assert first["description_placeholders"]["failed_device"] == ""
        second = await step()
        assert second["errors"] == {}
        result.append((first, second))
    return result


class SmallFollowupTests(unittest.IsolatedAsyncioTestCase):
    def test_pending_generations_preserve_all_owners(self):
        self.assertEqual(len(generation_trace()), 729)

    async def test_local_credentials_keep_errors_and_password_spaces(self):
        self.assertEqual(len(await credentials_trace()), 40)

    async def test_scan_error_is_consumed_once_in_both_flows(self):
        self.assertEqual(len(await scan_trace()), 2)

    def test_generation_discard_preserves_newer_and_sorted_callbacks(self):
        for values in itertools.product((None, 1, 3), repeat=4):
            for cutoff in (0, 1, 2, 3):
                manager = mm(opt.fx.S(True, True), minimum_interval_ms=0)
                for field, value in zip(("power", "brightness", "appearance", "effect"), values):
                    setattr(manager._pending_versions, field, value)
                seen = []
                manager._remove_generation = seen.append
                manager._discard_pending_at_or_before(cutoff)
                self.assertEqual(seen, sorted({v for v in values if v is not None and v <= cutoff}))


if __name__ == "__main__":
    if "--compare" in sys.argv:
        import asyncio
        async def compare():
            print(json.dumps({"generations": generation_trace(), "credentials": await credentials_trace(), "scan": await scan_trace()}, sort_keys=True, default=str))
        asyncio.run(compare())
    else:
        unittest.main()
