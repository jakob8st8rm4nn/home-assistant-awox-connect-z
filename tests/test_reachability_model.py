"""Exercise the real setup/unload lifecycle and the agreed evidence model."""
import asyncio
import types
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import test_optimization as opt
from awox112 import client as cm
from awox112.const import DOMAIN, DATA_AVAILABILITY_EVIDENCE, CONF_AVAILABILITY_TIMEOUT


class ReachabilityModel(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def runtime(self):
        fixture = opt.SetupTests()
        await fixture.asyncSetUp()
        self.now = 100.
        self.info = None
        fixture.hass.async_create_task = lambda coro, *a, **kw: asyncio.create_task(coro)
        fixture.hass.async_create_background_task = fixture.hass.async_create_task
        fixture.entry.options[CONF_AVAILABILITY_TIMEOUT] = 10
        with patch.object(opt.setup, 'AwoxConnectZClient', cm.AwoxConnectZClient), \
             patch.object(cm, 'time', types.SimpleNamespace(monotonic=lambda: self.now)), \
             patch.object(cm.bluetooth, 'async_last_service_info', lambda *a, **kw: self.info):
            try:
                yield fixture
            finally:
                if fixture.entry.entry_id in fixture.hass.data.get(DOMAIN, {}):
                    await opt.setup.async_unload_entry(fixture.hass, fixture.entry)
                await fixture.asyncTearDown()

    async def start(self, f):
        self.assertTrue(await opt.setup.async_setup_entry(f.hass, f.entry))
        return f.hass.data[DOMAIN][f.entry.entry_id][0][0]

    async def reload(self, f):
        self.assertTrue(await opt.setup.async_unload_entry(f.hass, f.entry))
        return await self.start(f)

    async def test_reload_preserves_session_proof_with_stale_advertisement(self):
        async with self.runtime() as f:
            self.info = types.SimpleNamespace(time=80., source='scan', rssi=-60)
            c = await self.start(f)
            c._async_note_connection_liveness()
            seen = c.last_seen
            self.now = 105
            c = await self.reload(f)
            self.assertEqual(c.bluetooth_status, 'recently_connected')
            self.assertTrue(c.available); self.assertEqual(c.last_seen, seen)
            self.assertEqual(c._availability.connection_time, 100)
            self.now = 110
            self.assertEqual(c.bluetooth_status, 'unreachable'); self.assertFalse(c.available)

    async def test_repeated_reload_does_not_renew_session_proof(self):
        async with self.runtime() as f:
            c = await self.start(f); c._async_note_connection_liveness()
            for t in (102, 105, 109, 110, 115):
                self.now = t; c = await self.reload(f)
                self.assertEqual(c.available, t < 110)
                self.assertEqual(c._availability.connection_time, 100)

    async def test_reload_live_connection_captures_final_known_connection(self):
        async with self.runtime() as f:
            c = await self.start(f)
            class Backend:
                is_connected = True
                async def disconnect(inner): inner.is_connected = False
            c._client = Backend()
            self.now = 120
            c = await self.reload(f)
            self.assertFalse(c.connected)
            self.assertEqual(c.bluetooth_status, 'recently_connected')
            self.assertEqual(c._availability.connection_time, 120)
            self.now = 130; self.assertFalse(c.available)

    async def test_new_timeout_uses_original_age(self):
        async with self.runtime() as f:
            c = await self.start(f); c._async_note_connection_liveness()
            self.now = 115; f.entry.options[CONF_AVAILABILITY_TIMEOUT] = 30
            c = await self.reload(f); self.assertTrue(c.available)
            self.now = 130; self.assertFalse(c.available)
            f.entry.options[CONF_AVAILABILITY_TIMEOUT] = 10
            c = await self.reload(f); self.assertFalse(c.available)

    async def test_failed_setup_keeps_existing_evidence(self):
        async with self.runtime() as f:
            c = await self.start(f); c._async_note_connection_liveness()
            await opt.setup.async_unload_entry(f.hass, f.entry)
            f.fail_forward = True; self.now = 105
            with self.assertRaises(RuntimeError): await opt.setup.async_setup_entry(f.hass, f.entry)
            f.fail_forward = False
            c = await self.start(f)
            self.assertEqual(c._availability.connection_time, 100); self.assertTrue(c.available)

    async def test_failed_unload_keeps_live_instance(self):
        async with self.runtime() as f:
            c = await self.start(f)
            f.hass.config_entries.async_unload_platforms.return_value = False
            self.assertFalse(await opt.setup.async_unload_entry(f.hass, f.entry))
            self.assertFalse(c.closed)
            self.assertNotIn(DATA_AVAILABILITY_EVIDENCE, f.hass.data)
            f.hass.config_entries.async_unload_platforms.return_value = True

    async def test_entry_removal_clears_only_its_history(self):
        async with self.runtime() as f:
            c = await self.start(f); c._async_note_connection_liveness()
            await opt.setup.async_unload_entry(f.hass, f.entry)
            histories = f.hass.data[DATA_AVAILABILITY_EVIDENCE]
            histories['other'] = {'other-mac': (1, 2, None)}
            await opt.setup.async_remove_entry(f.hass, f.entry)
            self.assertEqual(list(histories), ['other'])
            c = await self.start(f)
            self.assertIsNone(c.bluetooth_status); self.assertFalse(c.available)

    async def test_start_without_history_has_no_artificial_grace(self):
        async with self.runtime() as f:
            c = await self.start(f)
            self.assertIsNone(c.bluetooth_status); self.assertFalse(c.available)
            self.now = 500
            self.assertIsNone(c.bluetooth_status); self.assertFalse(c.available)

    async def test_priority_and_independent_expirations(self):
        async with self.runtime() as f:
            c = await self.start(f)
            c.async_note_bluetooth_liveness(100)
            self.now = 105; c._async_note_connection_liveness()
            self.assertEqual(c.bluetooth_status, 'recently_connected')
            self.now = 110; self.assertEqual(c.bluetooth_status, 'recently_connected')
            self.now = 115; self.assertEqual(c.bluetooth_status, 'unreachable')
            c.async_note_bluetooth_liveness(115)
            self.assertEqual(c.bluetooth_status, 'visible')
            self.assertEqual(c._availability.connection_time, 105)

    async def test_last_advertisement_source_is_independent_of_connection_proxy(self):
        async with self.runtime() as f:
            self.info = types.SimpleNamespace(time=100, source='advertising-proxy', rssi=-60)
            c = await self.start(f)
            c._client = types.SimpleNamespace(is_connected=True,
                _connected_scanner=types.SimpleNamespace(source='connected-proxy'))
            self.assertEqual(c.last_bluetooth_source_id, 'advertising-proxy')
            self.assertEqual(c.signal_strength, -60)
            c._client = None
            self.assertEqual(c.last_bluetooth_source_id, 'advertising-proxy')
            self.now = 111; c._async_note_connection_liveness()
            self.assertEqual(c.bluetooth_status, 'recently_connected')
            self.assertEqual(c.last_bluetooth_source_id, 'advertising-proxy')
            self.assertEqual(c.signal_strength, -60)

    async def test_last_advertisement_source_does_not_depend_on_connection_source(self):
        async with self.runtime() as f:
            self.info = types.SimpleNamespace(time=100, source='advertising-proxy', rssi=-60)
            c = await self.start(f); c._client = types.SimpleNamespace(is_connected=True)
            self.assertEqual(c.last_bluetooth_source_id, 'advertising-proxy')
            c._client = None

    async def test_equal_timestamp_ha_cache_does_not_replace_last_received_source(self):
        async with self.runtime() as f:
            self.info = types.SimpleNamespace(time=100, source='cache-proxy', rssi=-70)
            c = await self.start(f)
            c.async_note_bluetooth_liveness(100, source='packet-proxy', rssi=-55)
            self.assertEqual(c.last_bluetooth_source_id, 'packet-proxy')
            self.assertEqual(c.signal_strength, -55)

    async def test_fresh_ha_cache_after_unload_wins_over_saved_packet(self):
        async with self.runtime() as f:
            self.info = types.SimpleNamespace(time=100, source='old-proxy', rssi=-60)
            c = await self.start(f)
            await opt.setup.async_unload_entry(f.hass, f.entry)
            self.now = 105
            self.info = types.SimpleNamespace(time=105, source='new-proxy', rssi=-50)
            c = await self.start(f)
            self.assertEqual(c._availability.advertisement_time, 105)
            self.assertEqual(c.last_bluetooth_source_id, 'new-proxy')
            self.now = 115; self.assertFalse(c.available)

    async def test_entry_reload_keeps_last_advertisement_source_and_rssi_without_cache(self):
        async with self.runtime() as f:
            self.info = types.SimpleNamespace(time=100, source='saved-proxy', rssi=-57)
            c = await self.start(f)
            seen = c.last_seen
            self.assertEqual(c.last_bluetooth_source_id, 'saved-proxy')
            self.assertEqual(c.signal_strength, -57)
            await opt.setup.async_unload_entry(f.hass, f.entry)
            self.info = None
            c = await self.start(f)
            self.assertEqual(c.last_bluetooth_source_id, 'saved-proxy')
            self.assertEqual(c.signal_strength, -57)
            self.assertEqual(c.last_seen, seen)

    async def test_new_ha_process_does_not_restore_monotonic_history(self):
        async with self.runtime() as f:
            c = await self.start(f); c._async_note_connection_liveness()
            await opt.setup.async_unload_entry(f.hass, f.entry)
            # A fresh HA process has fresh hass.data, not a persisted runtime cache.
            f.hass.data = {}; self.now = 1
            c = await self.start(f)
            self.assertIsNone(c.bluetooth_status); self.assertFalse(c.available)

    async def test_second_config_entry_cannot_receive_first_entries_proof(self):
        async with self.runtime() as f:
            c = await self.start(f); c._async_note_connection_liveness()
            await opt.setup.async_unload_entry(f.hass, f.entry)
            f.entry.entry_id = 'second'
            c = await self.start(f)
            self.assertIsNone(c.bluetooth_status); self.assertFalse(c.available)

    async def test_event_sequence_matrix_matches_independent_model(self):
        import random
        async with self.runtime() as f:
            c = await self.start(f)
            rng = random.Random(1911)
            packet = proof = None
            connected = False
            count = 0
            for _ in range(2000):
                self.now += rng.choice((0, .01, 1, 10, 11))
                event = rng.choice(('packet', 'connect', 'reply', 'loss', 'idle', 'reload', 'wait'))
                if event == 'packet':
                    packet = self.now; c.async_note_bluetooth_liveness(packet)
                elif event == 'connect':
                    connected = True; proof = self.now
                    c._client = types.SimpleNamespace(is_connected=True)
                    c._async_note_connection_liveness()
                elif event == 'reply' and connected:
                    proof = self.now; c._async_note_connection_liveness()
                elif event in ('loss', 'idle') and connected:
                    if event == 'idle':
                        proof = self.now; c._async_note_connection_liveness()
                    connected = False; c._client = None
                elif event == 'reload':
                    if connected: proof = self.now
                    saved = c.export_availability_evidence()
                    c._client = None; connected = False
                    # Exercise timestamp restoration independently of HA lifecycle tests.
                    c.restore_availability_evidence(saved)
                packet_fresh = packet is not None and self.now-packet < 10
                proof_fresh = proof is not None and self.now-proof < 10
                status = ('connected' if connected else
                          'recently_connected' if proof_fresh and (not packet_fresh or proof >= packet) else
                          'visible' if packet_fresh else
                          'unreachable' if packet is not None or proof is not None else None)
                self.assertEqual(c.bluetooth_status, status, (count, event))
                self.assertEqual(c.available, status in ('connected','visible','recently_connected'))
                count += 1
            c._client = None
            self.assertEqual(count, 2000)


if __name__ == '__main__': unittest.main(verbosity=2)
