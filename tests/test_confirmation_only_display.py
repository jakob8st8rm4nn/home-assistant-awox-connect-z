"""1911 is consumed by confirmation/bootstrap, not passive entity synchronization."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock
import test_small_diagnostic_fixes as fixtures
from awox112.command_sequences import PlannerState, TargetState
opt=fixtures.opt

class ConfirmationOnlyDisplay(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.SmallDiagnosticFixes.asyncSetUp
    asyncTearDown=fixtures.SmallDiagnosticFixes.asyncTearDown

    def feed(self, **values):
        self.c.status_1911.feed(opt.fx.payload(values),opt.fx.KEY,1)

    def begin(self):
        self.c.status_1911.begin(1)
        self.writes.clear()

    async def test_passive_reply_after_confirmation_does_not_overwrite_state(self):
        self.begin();self.feed(power=True,mired=250,brightness=200)
        self.e._async_apply_status_1911(self.c.status_1911.latest,confirmed_group=True)
        expected=(self.e._attr_is_on,self.e._attr_brightness,self.e._attr_color_temp_kelvin,self.e._manager.state)
        self.writes.clear()
        self.feed(power=False,mired=370,brightness=10)
        self.assertEqual(self.writes,[])
        self.assertEqual(expected,(self.e._attr_is_on,self.e._attr_brightness,self.e._attr_color_temp_kelvin,self.e._manager.state))
        self.assertFalse(self.c.status_1911.latest.power)
        self.assertEqual(self.c.status_1911.sequence,2)

    async def test_unknown_not_silently_recovered_by_unsolicited_reply(self):
        self.begin();self.feed(power=True,mired=250)
        self.assertFalse(self.e._manager.state.known)
        self.assertEqual(self.writes,[])

    async def test_unknown_bootstrap_still_initializes_planner_and_display(self):
        self.begin();self.feed(power=True,mired=250)
        self.e._executor.async_initial_status=AsyncMock(return_value=self.c.status_1911.latest)
        await asyncio.wait_for(self.e._async_submit_target(TargetState(power=True)),1)
        self.e._executor.async_initial_status.assert_awaited_once()
        self.assertEqual(self.e._manager.state,PlannerState(True,True,None))
        self.assertEqual(self.e._attr_color_temp_kelvin,4000)

    async def test_next_confirmation_can_still_consume_received_reply(self):
        self.begin();self.e._executor.busy=True
        self.feed(power=True,brightness=100,mired=250)
        self.assertEqual(self.writes,[])
        state=await self.c.status_1911.wait({'power':True,'brightness':100},1,0,time.monotonic()+.2,lambda:True)
        self.assertIsNotNone(state)
        self.e._executor.busy=False
        self.e._async_apply_status_1911(state,confirmed_group=True)
        self.assertEqual(self.e._attr_brightness,100)
        self.assertEqual(len(self.writes),1)

    async def test_advertisement_remains_normal_state_source(self):
        self.begin();self.feed(power=True,mired=250)
        self.e._async_apply_advertisement_state(self.c.status_1911.latest.as_lamp_state())
        self.assertEqual(self.e._manager.state,PlannerState(True,True,None))
        self.assertEqual(self.e._attr_color_temp_kelvin,4000)
        self.assertEqual(len(self.writes),1)

if __name__=='__main__':unittest.main(verbosity=2)
