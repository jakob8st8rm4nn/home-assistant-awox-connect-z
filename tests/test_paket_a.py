"""Frames, encoded timing and unchanged dictionary/tuple contracts."""
import asyncio
import json
import sys
import unittest
from datetime import datetime, timezone
import test_status_confirmation as fx
from awox112 import protocol
from awox112.availability import AvailabilityState
from awox112.command.executor import CommandExecutor
from awox112.command.sequences import PlannedCommand
from awox112.command.confirmation import ConfirmationBatch, ExecutedCommand

TRANSITIONS=(0,.04,.05,.19,.2,1,1.05,30,-1,6553.5,99999.)


def executor():
    manager=fx.CommandManager(fx.S(True,True))
    client=fx.FakeClient(manager)
    return manager,client,CommandExecutor(client,manager,mesh_id=fx.MID)


def prepared_trace(value,transition=.2):
    _,_,e=executor()
    try:
        prepared=e._prepare_command(PlannedCommand(fx.K.BRIGHTNESS_ON,value,transition))
        return prepared.frame.hex(),prepared.label
    except Exception as error:return type(error).__name__,str(error)


async def execution_trace(kind,transition):
    manager,client,e=executor()
    targets={
        'brightness':fx.T(brightness=140,brightness_transition=transition),
        'color':fx.T(color=(120,80),appearance_transition=transition),
        'cct':fx.T(cct=2700,appearance_transition=transition),
        'off':fx.T(power=False),'effect':fx.T(effect='Candle'),
    }
    try:manager.submit(targets[kind])
    except Exception as error:return 'error',type(error).__name__,str(error)
    result=await e.async_execute_next()
    return result.kind.value,e._last_executed.plaintext.hex(),e._last_executed.transition


class PaketATests(unittest.IsolatedAsyncioTestCase):
    def test_executor_brightness_frames_labels_and_errors(self):
        for value in (-1000,-1,0,1,2,254,255,256,1000,.9,1.9,254.9,'255',True,False):
            frame,label=prepared_trace(value)
            expected=protocol.make_brightness(value,.2,mesh_id=fx.MID)
            self.assertEqual(frame,expected.hex())
            self.assertEqual(label,f'brightness:{int(value)}')
        for value,error in ((None,'CommandExecutorError'),('invalid','ValueError'),(float('nan'),'ValueError'),(float('inf'),'OverflowError')):
            self.assertEqual(prepared_trace(value)[0],error)

    def test_encoded_transition_rounding_and_bounds(self):
        for requested in TRANSITIONS:
            for layout,frame,offset in (
                ('brightness',protocol.make_brightness(140,requested,mesh_id=fx.MID),9),
                ('appearance',protocol.make_hs_color(120,80,requested,mesh_id=fx.MID),10),
                ('appearance',protocol.make_color_temp_kelvin(2700,requested,mesh_id=fx.MID),10),
            ):
                expected=int.from_bytes(frame[offset:offset+2],'little')/10
                if hasattr(protocol,'encoded_transition_seconds'):
                    self.assertEqual(protocol.encoded_transition_seconds(frame,layout=layout),expected)
                self.assertEqual(expected,max(0,min(65535,round(requested*10)))/10)

    async def test_executor_uses_encoded_duration_only_for_fading_commands(self):
        for requested in TRANSITIONS:
            for kind in ('brightness','color','cct','off','effect'):
                with self.subTest(kind=kind,requested=requested):
                    result,frame,actual=await execution_trace(kind,requested)
                    if requested < 0 and kind in ('brightness','color','cct'):
                        self.assertEqual((result,frame),('error','SequenceConflictError'))
                        continue
                    frame=bytes.fromhex(frame)
                    expected=int.from_bytes(frame[9:11] if kind=='brightness' else frame[10:12],'little')/10 if kind in ('brightness','color','cct') else 0
                    self.assertEqual(actual,expected)

    def test_optional_goal_fields_and_explicit_none_keep_their_meaning(self):
        store=fx.Status1911Store(fx.MAC,fx.MID);store.begin(1)
        store.feed(fx.payload({'effect':'Candle'}),fx.KEY,1)
        state=store.latest
        self.assertTrue(state.matches({}))
        self.assertFalse(state.matches({'effect':None}))
        self.assertTrue(state.matches({'effect':'Candle'}))
        item=ExecutedCommand(PlannedCommand(fx.K.POWER_OFF),protocol.make_power(False,mesh_id=fx.MID),0,1,0)
        goal=ConfirmationBatch(1,(item,)).goal
        self.assertIs(type(goal),dict)
        self.assertEqual(goal,{'power':False,'effect':None})
        store.feed(fx.payload({'color_mode':True,'hue':254}),fx.KEY,1)
        self.assertTrue(store.latest.matches({'hue':0}))
        self.assertFalse(store.latest.matches({'hue':127}))

    def test_evidence_export_stays_tuple_and_restore_formats_unchanged(self):
        stamp=datetime(2026,10,25,tzinfo=timezone.utc)
        evidence=(100.,99.,stamp,'proxy',-50)
        state=AvailabilityState();state.restore(evidence)
        self.assertIs(type(state.export()),tuple)
        self.assertEqual(state.export(),evidence)
        for length in (3,4,5,6):
            restored=AvailabilityState(source='existing',rssi=-80)
            restored.restore((evidence+('ignored',))[:length])
            self.assertEqual(restored.export(),evidence if length>=5 else evidence[:3]+('existing',-80))


if __name__=='__main__':
    if '--traces' in sys.argv:
        async def traces():
            output={}
            values=(-1000,-1,0,1,2,254,255,256,1000,.9,1.9,254.9,'255',True,False,None,'invalid',float('nan'),float('inf'))
            for index,value in enumerate(values):
                for t in TRANSITIONS:output[f'prepared_{index}_{t}']=prepared_trace(value,t)
            for kind in ('brightness','color','cct','off','effect'):
                for t in TRANSITIONS:output[f'executed_{kind}_{t}']=await execution_trace(kind,t)
            print(json.dumps(output,sort_keys=True))
        asyncio.run(traces())
    else:unittest.main()
