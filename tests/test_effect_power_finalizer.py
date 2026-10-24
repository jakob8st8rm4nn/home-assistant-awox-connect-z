"""Offline checks; no Home Assistant runtime, network or hardware required."""
import importlib.util,itertools,sys,types,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'custom_components/awox_connect_z'
pkg=types.ModuleType('awox_release_review');pkg.__path__=[str(SOURCE)];sys.modules[pkg.__name__]=pkg
from awox_release_review.command_sequences import CommandKind as K,PlannerState as S,TargetState as T,plan_commands,SequenceConflictError,UnknownPlannerStateError
from awox_release_review.command_manager import CommandManager as M,WriteFailureAction as F,CommandNotReadyError

class FinalizerTests(unittest.TestCase):
 def test_current_sequence_contract(self):
  cases=[
   (S(True,False),T(power=True),[K.POWER_ON]),
   (S(True,True),T(power=False),[K.POWER_OFF]),
   (S(True,True),T(brightness=200),[K.BRIGHTNESS_ON,K.POWER_ON]),
   (S(True,False),T(brightness=200),[K.BRIGHTNESS_ON,K.POWER_ON]),
   (S(True,True),T(color=(120,70)),[K.COLOR]),
   (S(True,False),T(cct=3500),[K.CCT,K.POWER_ON]),
   (S(True,True),T(effect='Candle'),[K.EFFECT_START,K.POWER_ON]),
   (S(True,True),T(color=(120,70),brightness=180,effect='Candle'),
    [K.COLOR,K.BRIGHTNESS_ON,K.POWER_ON,K.EFFECT_START,K.POWER_ON]),
   (S(True,False,'Candle'),T(power=True),[K.EFFECT_STOP,K.POWER_ON]),
   (S(True,True,'Candle'),T(brightness=180,stop_effect=True),
    [K.EFFECT_STOP,K.BRIGHTNESS_ON,K.POWER_ON]),
   (S(True,True,'Color cycle'),T(brightness=180,effect='Color cycle'),
    [K.BRIGHTNESS_ON,K.POWER_ON]),
  ]
  for state,target,kinds in cases:
   with self.subTest(state=state,target=target):
    self.assertEqual([c.kind for c in plan_commands(state,target)],kinds)
 def manager(self,on=True,effect=None,interval=0):return M(S(True,on,effect),minimum_interval_ms=interval,clock=lambda:0)
 def write(self,m,kind=None):
  command=m.take_next_command(require_ready=False)
  self.assertIsNotNone(command)
  if kind is not None:self.assertEqual(command.kind,kind)
  m.mark_write_succeeded(finished_at=0)
  m.release_confirmation(m.last_write_generations)
  return command.kind
 def drain(self,m):
  result=[]
  for _ in range(30):
   if m.peek_next_command() is None:break
   result.append(self.write(m))
  self.assertLess(len(result),30)
  self.assertFalse(m.pending_generations)
  return result
 def test_all_planner_start_paths_have_immediate_power(self):
  for on,active,effect,appearance,brightness in itertools.product([False,True],[None,'Candle','Color cycle'],['Candle','Color cycle'],[None,'color','cct'],[None,180]):
   kw=dict(effect=effect,brightness=brightness)
   if appearance=='color':kw['color']=(120,70)
   if appearance=='cct':kw['cct']=3500
   commands=plan_commands(S(True,on,active),T(**kw))
   for i,c in enumerate(commands):
    if c.kind in (K.EFFECT_START,K.BRIGHTNESS_ON):
     self.assertLess(i+1,len(commands));self.assertEqual(commands[i+1].kind,K.POWER_ON)
 def test_on_off_both_effects_complete_only_after_power(self):
  for on,effect in itertools.product([False,True],['Candle','Color cycle']):
   m=self.manager(on);m.submit(T(effect=effect));generation=m.request_generation
   self.write(m,K.EFFECT_START);self.assertIn(generation,m.pending_generations)
   self.write(m,K.POWER_ON);self.assertFalse(m.pending_generations)
   self.assertEqual(m.state,S(True,True,effect))
 def test_combined_targets_keep_both_power_steps(self):
  for on,appearance,effect in itertools.product([False,True],['color','cct'],['Candle','Color cycle']):
   m=self.manager(on);kwargs={appearance:(120,70) if appearance=='color' else 3500}
   m.submit(T(brightness=180,effect=effect,**kwargs))
   self.assertEqual(self.drain(m),[K.COLOR if appearance=='color' else K.CCT,K.BRIGHTNESS_ON,K.POWER_ON,K.EFFECT_START,K.POWER_ON])
 def test_same_effect_noop_and_stop_no_extra_power(self):
  for effect in ['Candle','Color cycle']:
   m=self.manager(effect=effect);m.submit(T(effect=effect));self.assertEqual(self.drain(m),[])
   m.submit(T(stop_effect=True));self.assertEqual(self.drain(m),[K.EFFECT_STOP])
   m=self.manager(effect=effect);m.submit(T(effect=effect,brightness=180));self.assertEqual(self.drain(m),[K.BRIGHTNESS_ON,K.POWER_ON])
 def test_later_off_replaces_effect_finalizer_even_if_off_advertised(self):
  for initial_on in [False,True]:
   m=self.manager(initial_on);m.submit(T(effect='Candle'));self.write(m,K.EFFECT_START)
   m.submit(T(power=False));m.set_state(S(True,False,'Candle'))
   self.assertEqual(self.drain(m),[K.POWER_OFF])
 def test_latest_nonpower_request_cannot_skip_finalizer(self):
  for incoming in [T(color=(200,70)),T(cct=4000),T(brightness=120),T(effect='Color cycle'),T(stop_effect=True)]:
   m=self.manager();m.submit(T(effect='Candle'));self.write(m,K.EFFECT_START)
   m.submit(incoming);m.force_replan();m.set_state(S(True,True,'Candle'))
   self.assertEqual(m.peek_next_command().kind,K.POWER_ON)
   self.write(m,K.POWER_ON);self.drain(m)
 def test_new_off_during_effect_write_finalizes_off(self):
  m=self.manager();m.submit(T(effect='Candle'));m.take_next_command()
  m.submit(T(power=False));m.mark_write_succeeded(finished_at=0)
  self.assertEqual(self.drain(m),[K.POWER_OFF])
 def test_retry_budget_not_reset_by_replan_or_on_to_off(self):
  m=self.manager();m.submit(T(effect='Candle'));self.write(m,K.EFFECT_START)
  m.take_next_command();self.assertEqual(m.in_flight_attempt,1)
  self.assertEqual(m.mark_write_failed().action,F.REPLAN)
  m.force_replan();m.set_state(S(True,True,'Candle'));m.submit(T(power=False))
  self.assertEqual(m.take_next_command().kind,K.POWER_OFF);self.assertEqual(m.in_flight_attempt,2)
  result=m.mark_write_failed();self.assertEqual(result.action,F.ABORTED)
  self.assertEqual(m.peek_next_command().kind,K.POWER_OFF)
  self.write(m,K.POWER_OFF);self.assertFalse(m.pending_generations)
 def test_terminal_old_finalizer_preserves_newer_request(self):
  m=self.manager();m.submit(T(effect='Candle'));old=m.request_generation;self.write(m,K.EFFECT_START)
  m.submit(T(cct=3500));new=m.request_generation;m.take_next_command()
  result=m.mark_write_failed(retry_allowed=False)
  self.assertIn(old,result.failed_generations);self.assertNotIn(new,result.failed_generations)
  self.assertIn(new,m.pending_generations);self.drain(m)
 def test_uncertain_start_aborts_and_does_not_create_power(self):
  m=self.manager();m.submit(T(effect='Candle'));m.take_next_command()
  self.assertEqual(m.mark_write_failed(uncertain=True).action,F.ABORTED_UNCERTAIN)
  self.assertFalse(m.state.known);self.assertIsNone(m.peek_next_command())
  m.set_state(S(True,True,'Candle'));self.assertIsNone(m.peek_next_command())
 def test_failed_undelivered_start_has_no_finalizer(self):
  m=self.manager();m.submit(T(effect='Candle'));m.take_next_command()
  m.mark_write_failed(retry_allowed=False);self.assertIsNone(m.peek_next_command())
 def test_cancel_and_unknown_during_start_suppress_followup(self):
  for action in ['cancel','unknown','clear']:
   m=self.manager();m.submit(T(effect='Candle'));g=m.request_generation;m.take_next_command()
   if action=='cancel':m.cancel_generation(g)
   elif action=='unknown':m.set_state(S(False))
   else:m.clear_pending()
   m.mark_write_succeeded(finished_at=0);self.assertIsNone(m.peek_next_command());self.assertFalse(m.pending_generations)
 def test_cancel_after_effect_start_discards_finalizer(self):
  m=self.manager();m.submit(T(effect='Candle'));g=m.request_generation;self.write(m,K.EFFECT_START)
  m.cancel_generation(g);self.assertIsNone(m.peek_next_command())
 def test_power_finalizer_respects_250ms_interval(self):
  m=self.manager(interval=250);m.submit(T(effect='Candle'));self.write(m,K.EFFECT_START)
  with self.assertRaises(CommandNotReadyError):m.take_next_command(now=.249)
  self.assertEqual(m.take_next_command(now=.250).kind,K.POWER_ON)
 def test_off_priority_conflicts_unknown_and_stored_recovery(self):
  self.assertEqual([c.kind for c in plan_commands(S(True,True),T(power=False,effect='Candle',stop_effect=True))],[K.POWER_OFF])
  with self.assertRaises(SequenceConflictError):plan_commands(S(True,True),T(effect='Candle',stop_effect=True))
  with self.assertRaises(UnknownPlannerStateError):plan_commands(S(False),T(effect='Candle'))
  m=self.manager(False,'Color cycle');m.submit(T(effect='Candle'))
  self.assertEqual(self.drain(m),[K.EFFECT_STOP,K.EFFECT_START,K.POWER_ON])

if __name__=='__main__':unittest.main(verbosity=2)
