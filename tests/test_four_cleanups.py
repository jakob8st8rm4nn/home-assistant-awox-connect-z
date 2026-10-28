"""Sequence, discovery and setup contracts shared with the predecessor."""
import asyncio
import hashlib
import itertools
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

if "--source" in sys.argv:
    index = sys.argv.index("--source")
    sys.path.insert(0, sys.argv[index + 1])
    del sys.argv[index:index + 2]
import test_optimization as opt
from awox112.command import sequences as seq
cf = opt.config_flow


def sequence_digest():
    digest = hashlib.sha256()
    count = 0
    for known, on, stored, power, bright, appearance, effect, stop, fade, recover in itertools.product(
        (False, True), (None, False, True), (None, "Candle", "Color cycle"),
        (None, False, True), (None, 0, 80, 255), ("none", "color", "cct", "conflict"),
        (None, "Candle", "Color cycle"), (False, True), (0, .2, -1), (False, True),
    ):
        state = seq.PlannerState(known, on, stored)
        target = seq.TargetState(power=power, brightness=bright,
            color=(120, 75) if appearance in ("color", "conflict") else None,
            cct=2700 if appearance in ("cct", "conflict") else None,
            effect=effect, stop_effect=stop, brightness_transition=fade, appearance_transition=fade)
        try:
            result = ("ok", [(c.kind.value, c.value, c.transition) for c in seq.plan_commands(state, target, recover_stored_effect=recover)])
        except Exception as error:
            result = ("error", type(error).__name__, str(error))
        digest.update(json.dumps(result, sort_keys=True).encode())
        count += 1
    return count, digest.hexdigest()


def service_info(data):
    return types.SimpleNamespace(manufacturer_data={} if data is None else {cf.AWOX_COMPANY_ID: data})


def complete_packet():
    data = bytearray(17)
    data[:2] = bytes.fromhex("9620")
    data[2:6] = bytes.fromhex("CCDDEEFF")[::-1]
    data[6] = 3
    data[9:11] = (100).to_bytes(2, "little")
    data[11:13] = bytes((1, 255))
    data[13:15] = (370).to_bytes(2, "little")
    return bytes(data)


async def discovery_trace():
    valid = service_info(complete_packet())
    wrong_identity = bytearray(complete_packet()); wrong_identity[2] ^= 1
    wrong_marker = bytearray(complete_packet()); wrong_marker[6] = 0
    outcomes = []
    for initial in (valid, service_info(None), service_info(b"short"), service_info(bytes(wrong_identity)), service_info(bytes(wrong_marker))):
        flow = cf.AwoxConnectZConfigFlow(); flow.hass = object()
        async def wait(hass, predicate, matcher, mode, timeout):
            outcomes.append((predicate(initial), predicate(valid), matcher, mode, timeout))
            return valid
        with patch.object(cf.bluetooth, "async_process_advertisements", wait, create=True), patch.object(cf.bluetooth, "BluetoothScanningMode", types.SimpleNamespace(ACTIVE="active"), create=True):
            assert await flow._async_full_advertisement(initial, "AA:BB:CC:DD:EE:FF") is valid
    assert len(outcomes) == 4
    for error in (TimeoutError, asyncio.CancelledError):
        flow = cf.AwoxConnectZConfigFlow(); flow.hass = object()
        with patch.object(cf.bluetooth, "async_process_advertisements", AsyncMock(side_effect=error), create=True), patch.object(cf.bluetooth, "BluetoothScanningMode", types.SimpleNamespace(ACTIVE="active"), create=True):
            try:
                await flow._async_full_advertisement(service_info(None), "AA:BB:CC:DD:EE:FF")
            except BaseException as caught:
                assert type(caught) is error
            else:
                raise AssertionError("Expected discovery failure")
    return outcomes


async def bluetooth_record_trace():
    fixture = opt.FlowTests(); flow = fixture.flow(); target = fixture.entry("target")
    flow._configured_entries = lambda: [target]
    flow._async_matching_account = AsyncMock(return_value=(target, False))
    updates = []
    flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_update_entry=lambda *args, **kwargs: updates.append(kwargs), async_reload=AsyncMock()))
    result = await flow.async_step_bluetooth_confirm({})
    record = updates[0]["data"][cf.CONF_DEVICES][0]
    assert record == {"name": "Test", "mac": "AA:BB:CC:DD:EE:FF", "mesh_id": 100,
        "model": "Connect.Z", "manufacturer": "EGLO / AwoX", "firmware": None,
        "hardware": None, "device_type": "bluetooth_discovered", "cloud_object_id": ""}
    assert target.data[cf.CONF_DEVICES] == []
    return result, record


class FourCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_effect_start_finalizer_and_same_effect_noop(self):
        for on, current, requested in itertools.product((False, True), (None, "Candle", "Color cycle"), ("Candle", "Color cycle")):
            commands = seq.plan_commands(seq.PlannerState(True, on, current), seq.TargetState(effect=requested))
            if on and current == requested:
                self.assertEqual(commands, ())
            else:
                self.assertEqual(commands[-1].kind, seq.CommandKind.POWER_ON)
                self.assertIn(seq.CommandKind.EFFECT_START, [c.kind for c in commands])

    async def test_complete_discovery_filter_and_failure_propagation(self):
        self.assertEqual(len(await discovery_trace()), 4)

    async def test_bluetooth_record_keeps_all_fields_and_original_entry(self):
        result, _ = await bluetooth_record_trace()
        self.assertEqual(result["reason"], "device_added")

    async def test_setup_metadata_keeps_normalization_and_entry_isolation(self):
        fixture = opt.SetupTests()
        await fixture.asyncSetUp()
        try:
            original = {"mac": opt.fx.MAC.lower(), "mesh_id": str(opt.fx.MID), "name": "Desk"}
            fixture.entry.data[cf.CONF_DEVICES] = [original, {"mac": "AA:BB:CC:DD:EE:FF", "mesh_id": "100"}]
            self.assertTrue(await opt.setup.async_setup_entry(fixture.hass, fixture.entry))
            clients = fixture.hass.data[cf.DOMAIN][fixture.entry.entry_id]
            self.assertEqual(len(clients), 2)
            self.assertEqual(clients[0][1]["mesh_id"], opt.fx.MID)
            self.assertEqual(clients[1][1]["mesh_id"], 100)
            self.assertIsNot(clients[0][1], original)
            clients[0][1]["name"] = "Changed"
            self.assertEqual(original["name"], "Desk")
            self.assertIsInstance(original["mesh_id"], str)
            self.assertTrue(await opt.setup.async_unload_entry(fixture.hass, fixture.entry))
            self.assertTrue(all(client.closed for client in fixture.clients))
        finally:
            await fixture.asyncTearDown()


if __name__ == "__main__":
    if "--compare" in sys.argv:
        async def compare():
            print(json.dumps({"sequences": sequence_digest(), "discovery": await discovery_trace(), "record": await bluetooth_record_trace()}, sort_keys=True))
        asyncio.run(compare())
    else:
        unittest.main()
