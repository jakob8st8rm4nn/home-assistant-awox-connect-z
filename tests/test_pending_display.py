"""Suppress old confirmed display states, preserving real manager/executor work."""
import asyncio
import unittest

import test_status_confirmation as fx


class PendingDisplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fx.Tests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def entity(self):
        manager, client, _ = self.fixture.setup_group('correct', on=True)
        entity = fx.AwoxConnectZLight(client, {'mesh_id': fx.MID},
                                      minimum_command_interval_ms=0)
        entity.hass = fx.Hass()
        client.manager = entity._manager
        entity._manager.set_state(fx.S(True, True))
        published = []
        entity.async_write_ha_state = lambda: published.append({
            'on': getattr(entity, '_attr_is_on', None),
            'hs': getattr(entity, '_attr_hs_color', None),
            'brightness': getattr(entity, '_attr_brightness', None),
            'known': entity._manager.state.known,
        })
        return entity, client, published

    async def drain(self, entity):
        await asyncio.wait_for(entity._async_command_worker(), 2)
        # Applying a final state can schedule the existing worker; join it too.
        if entity._command_worker is not None:
            await asyncio.wait_for(entity._command_worker, 2)

    async def test_single_group_still_publishes(self):
        entity, client, published = self.entity()
        entity._manager.submit(fx.T(color=(120, 80)))
        await self.drain(entity)
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[-1]['hs'][0], 120, delta=1)
        self.assertEqual(client.confirmation.diagnostics['confirmed'], 1)

    async def test_two_colors_publish_only_final_but_confirm_both(self):
        entity, client, published = self.entity()
        queued = []
        def hook(stage):
            if stage == 'write' and not queued:
                entity._manager.submit(fx.T(color=(240, 90)))
                queued.append(entity._manager.request_generation)
        client.hook = hook
        entity._manager.submit(fx.T(color=(0, 100)))
        await self.drain(entity)
        self.assertEqual(client.controls, [fx.K.COLOR, fx.K.COLOR])
        self.assertEqual(client.confirmation.diagnostics['confirmed'], 2)
        self.assertEqual(client.polls, 0)
        self.assertEqual(client.replays, [])
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[0]['hs'][0], 240, delta=1)

    async def test_color_then_off_never_publishes_old_on(self):
        entity, client, published = self.entity()
        def hook(stage):
            if stage == 'write' and len(client.controls) == 1:
                entity._manager.submit(fx.T(power=False))
        client.hook = hook
        entity._manager.submit(fx.T(color=(0, 100)))
        await self.drain(entity)
        self.assertEqual(client.controls, [fx.K.COLOR, fx.K.POWER_OFF])
        self.assertEqual(client.confirmation.diagnostics['confirmed'], 2)
        self.assertEqual([s['on'] for s in published], [False])

    async def test_brightness_followup_keeps_color_and_power_finalizer(self):
        entity, client, published = self.entity()
        def hook(stage):
            if stage == 'write' and len(client.controls) == 1:
                entity._manager.submit(fx.T(brightness=80))
        client.hook = hook
        entity._manager.submit(fx.T(color=(120, 80)))
        await self.drain(entity)
        self.assertEqual(client.controls, [fx.K.COLOR, fx.K.BRIGHTNESS_ON, fx.K.POWER_ON])
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]['brightness'], 80)
        self.assertAlmostEqual(published[0]['hs'][0], 120, delta=1)
        self.assertTrue(entity._manager.state.known)

    async def test_pending_work_is_promoted_before_display_guard(self):
        entity, client, published = self.entity()
        states = []
        execute = entity._executor.async_execute_group
        async def observe():
            result = await execute()
            states.append((entity._manager.pending_generations,
                           entity._manager.next_command_generation,
                           entity._manager.state.known))
            return result
        entity._executor.async_execute_group = observe
        def hook(stage):
            if stage == 'write' and len(client.controls) == 1:
                entity._manager.submit(fx.T(cct=3500))
        client.hook = hook
        entity._manager.submit(fx.T(color=(0, 100)))
        await self.drain(entity)
        self.assertTrue(states[0][0])
        self.assertTrue(states[0][2])
        self.assertFalse(states[-1][0])
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(entity._attr_color_temp_kelvin, 3500, delta=20)

    async def test_cancelled_followup_does_not_hide_successful_group(self):
        entity, client, published = self.entity()
        def hook(stage):
            if stage == 'write' and len(client.controls) == 1:
                entity._manager.submit(fx.T(color=(240, 90)))
                entity._manager.cancel_generation(entity._manager.request_generation)
        client.hook = hook
        entity._manager.submit(fx.T(color=(0, 100)))
        await self.drain(entity)
        self.assertEqual(client.controls, [fx.K.COLOR])
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[0]['hs'][0], 0, delta=1)

    async def test_latest_waiting_color_wins_and_all_real_groups_confirm(self):
        entity, client, published = self.entity()
        def hook(stage):
            if stage == 'write' and len(client.controls) == 1:
                for hue in (60, 120, 240):
                    entity._manager.submit(fx.T(color=(hue, 80)))
        client.hook = hook
        entity._manager.submit(fx.T(color=(0, 100)))
        await self.drain(entity)
        self.assertEqual(client.controls, [fx.K.COLOR, fx.K.COLOR])
        self.assertEqual(client.confirmation.diagnostics['confirmed'], 2)
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[0]['hs'][0], 240, delta=1)

    async def test_failed_followup_keeps_existing_unknown_error_policy(self):
        entity, client, published = self.entity()
        def hook(stage):
            if stage == 'write' and len(client.controls) == 1:
                entity._manager.submit(fx.T(color=(240, 90)))
            elif stage == 'prepare' and len(client.controls) == 1:
                client.behavior = 'wrong'
        client.hook = hook
        entity._manager.submit(fx.T(color=(0, 100)))
        await self.drain(entity)
        self.assertFalse(entity._manager.state.known)
        self.assertFalse(entity._manager.pending_generations)
        self.assertEqual(client.confirmation.diagnostics['confirmed'], 1)
        self.assertEqual(client.confirmation.diagnostics['unconfirmed'], 1)
        self.assertEqual(client.polls, 14)
        self.assertEqual(len(client.replays), 1)
        self.assertTrue(published)
        self.assertTrue(all(not state['known'] for state in published))

    async def test_real_request_waiters_complete_for_both_colors(self):
        entity, client, published = self.entity()
        second = []
        def hook(stage):
            if stage == 'write' and not second:
                second.append(asyncio.create_task(
                    entity._async_submit_target(fx.T(color=(240, 90)))))
        client.hook = hook
        await asyncio.wait_for(entity._async_submit_target(fx.T(color=(0, 100))), 2)
        self.assertEqual(len(second), 1)
        await asyncio.wait_for(second[0], 2)
        if entity._command_worker is not None:
            await asyncio.wait_for(entity._command_worker, 2)
        self.assertEqual(entity._request_waiters, {})
        self.assertEqual(client.confirmation.diagnostics['confirmed'], 2)
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[-1]['hs'][0], 240, delta=1)

    async def test_already_satisfied_followup_does_not_leave_display_stale(self):
        entity, client, published = self.entity()
        def hook(stage):
            if stage == 'write' and len(client.controls) == 1:
                entity._manager.submit(fx.T(power=True))
        client.hook = hook
        entity._manager.submit(fx.T(color=(120, 80)))
        await self.drain(entity)
        self.assertEqual(client.controls, [fx.K.COLOR])
        self.assertFalse(entity._manager.pending_generations)
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[0]['hs'][0], 120, delta=1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
