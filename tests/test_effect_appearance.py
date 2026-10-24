"""Final confirmed status supplies native-effect appearance; commands do not."""
import types
import unittest
from unittest.mock import AsyncMock, Mock
import test_small_diagnostic_fixes as fixtures
opt = fixtures.opt
from awox112.command_sequences import PlannedCommand, CommandKind as K

class EffectAppearance(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SmallDiagnosticFixes.asyncSetUp
    asyncTearDown = fixtures.SmallDiagnosticFixes.asyncTearDown

    async def run_group(self, effect, commands, confirmed=True, **fields):
        self.c.status_1911.begin(1)
        self.c.status_1911.feed(opt.fx.payload(dict(power=True,effect=effect,**dict({'mired':250},**fields))),opt.fx.KEY,1)
        state=self.c.status_1911.latest
        self.e._executor=types.SimpleNamespace(busy=False,
            last_confirmed_state=state if confirmed else None,
            async_execute_group=AsyncMock(return_value=tuple(commands)))
        self.e._manager=types.SimpleNamespace(
            peek_next_command=Mock(side_effect=[object(),None]),set_state=lambda state:frozenset(),clear_pending=lambda:None)
        self.e.async_write_ha_state=lambda:None
        self.e._resolve_completed_waiters=lambda:None
        self.e._ensure_command_worker=lambda:None
        await self.e._async_command_worker()

    async def test_candle_explicit_cct_updates_ha(self):
        await self.run_group('Candle',[PlannedCommand(K.CCT,2700),PlannedCommand(K.BRIGHTNESS_ON,255),PlannedCommand(K.EFFECT_START,'Candle'),PlannedCommand(K.POWER_ON)])
        self.assertEqual(self.e._attr_color_temp_kelvin,4000)
        self.assertEqual(self.e._attr_color_mode,opt.fx.ColorMode.COLOR_TEMP)
        self.assertEqual(self.e._attr_effect,'Candle')

    async def test_both_effects_keep_explicit_color_choice(self):
        for effect in ('Candle','Color cycle'):
            await self.run_group(effect,[PlannedCommand(K.COLOR,(120.,75.)),PlannedCommand(K.EFFECT_START,effect)],color_mode=True,hue=85,saturation=191)
            self.assertAlmostEqual(self.e._attr_hs_color[0],85*360/254)
            self.assertAlmostEqual(self.e._attr_hs_color[1],191*100/255)
            self.assertEqual(self.e._attr_color_mode,opt.fx.ColorMode.HS)

    async def test_color_cycle_explicit_cct(self):
        await self.run_group('Color cycle',[PlannedCommand(K.CCT,2700),PlannedCommand(K.EFFECT_START,'Color cycle')])
        self.assertEqual(self.e._attr_color_temp_kelvin,4000)

    async def test_effect_only_uses_final_reply_but_unsolicited_keeps_it(self):
        self.e._attr_color_temp_kelvin=3200
        await self.run_group('Candle',[PlannedCommand(K.EFFECT_START,'Candle')])
        self.assertEqual(self.e._attr_color_temp_kelvin,4000)
        self.c.status_1911.feed(opt.fx.payload(dict(power=True,effect='Candle',mired=200)),opt.fx.KEY,1)
        self.e._async_apply_status_1911(self.c.status_1911.latest)
        self.assertEqual(self.e._attr_color_temp_kelvin,4000)

    async def test_effect_hue_ff_does_not_invent_color(self):
        self.e._attr_hs_color=(120.,75.)
        await self.run_group('Color cycle',[PlannedCommand(K.EFFECT_START,'Color cycle')],color_mode=True,hue=255,saturation=255)
        self.assertEqual(self.e._attr_hs_color,(120.,75.))

    async def test_without_confirmation_no_appearance_commit(self):
        self.e._attr_color_temp_kelvin=3200
        await self.run_group('Candle',[PlannedCommand(K.CCT,2700)],confirmed=False)
        self.assertEqual(self.e._attr_color_temp_kelvin,3200)

if __name__=='__main__':unittest.main(verbosity=2)
