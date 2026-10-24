"""Offline integration checks for protocol, session correlation and executor barriers."""
import asyncio,importlib.util,sys,types,unittest,time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
from enum import Enum,IntFlag

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'custom_components/awox_connect_z'
pkg=types.ModuleType('awox112');pkg.__path__=[str(SOURCE)];sys.modules['awox112']=pkg
def module(name,**attrs):
 m=types.ModuleType(name);m.__dict__.update(attrs);m.__path__=[];sys.modules[name]=m;return m
module('homeassistant');module('homeassistant.components')
bt=module('homeassistant.components.bluetooth',async_ble_device_from_address=lambda *a,**k:None,async_last_service_info=lambda *a,**k:None)
module('homeassistant.core',HomeAssistant=object,callback=lambda f:f)
module('homeassistant.exceptions',HomeAssistantError=type('HomeAssistantError',(Exception,),{}))
module('homeassistant.util');module('homeassistant.util.dt',utcnow=lambda:datetime.now(timezone.utc))
module('bleak_retry_connector',BleakClientWithServiceCache=object,device_source=lambda *a:None,establish_connection=None)
from awox112 import protocol as p,command_confirmation as cc
from awox112.status_1911 import Status1911Store
from awox112.command_sequences import CommandKind as K,PlannerState as S,TargetState as T
from awox112.command_manager import CommandManager
from awox112.command_executor import CommandExecutor,CommandExecutionFailed
from awox112.client import RuntimeCommandResult as Result,RuntimeCommandStatus as Status,AwoxConnectZClient

class ColorMode(str,Enum):HS='hs';COLOR_TEMP='color_temp'
class Feature(IntFlag):TRANSITION=1;EFFECT=2
class Entity:
 def async_write_ha_state(self):pass
class Restore:pass
module('homeassistant.components.light',ATTR_BRIGHTNESS='brightness',ATTR_COLOR_MODE='color_mode',ATTR_COLOR_TEMP_KELVIN='color_temp_kelvin',ATTR_EFFECT='effect',ATTR_HS_COLOR='hs_color',ATTR_TRANSITION='transition',ColorMode=ColorMode,EFFECT_OFF='off',LightEntity=Entity,LightEntityFeature=Feature)
module('homeassistant.config_entries',ConfigEntry=object);module('homeassistant.const',STATE_ON='on')
module('homeassistant.helpers');module('homeassistant.helpers.device_registry',DeviceInfo=dict)
module('homeassistant.helpers.entity_platform',AddEntitiesCallback=object);module('homeassistant.helpers.restore_state',RestoreEntity=Restore)
from awox112.light import AwoxConnectZLight

class Hass:
 def async_create_task(self,coro,*args):return asyncio.create_task(coro)

MAC='02:00:00:00:00:02';MID=40157;KEY=bytes(range(16))
def payload(state,mid=MID,mac=MAC,outer_type=0x80):
 mode=(6 if state.get('effect')=='Color cycle' else 16 if state.get('effect')=='Candle' else 0)
 if state.get('power',True):mode|=1
 if state.get('color_mode',False):mode|=2
 block=bytearray(16);block[:2]=mid.to_bytes(2,'little');block[5:7]=bytes.fromhex('9620')
 block[7:11]=bytes.fromhex(mac.replace(':',''))[-4:][::-1]
 block[12]=state.get("mode",mode);block[13]=state.get('brightness',127)
 block[14:16]=state.get('mired',250).to_bytes(2,'little')
 return bytes((outer_type,))+p.aes_encrypt_reversed(KEY,bytes(block))+bytes((state.get('hue',0),state.get('saturation',64),0x60))

class FakeClient:
 def __init__(self,manager,behavior='correct'):
  self.manager=manager;self.behavior=behavior;self.default_transition=0;self.connected=True;self.closed=False
  self.status_1911=Status1911Store(MAC,MID);self.status_1911.begin(1)
  self.confirmation=cc.CommandConfirmation(self);self.controls=[];self.replays=[];self.polls=0;self.frames={};self.hook=None
  self.state=dict(power=True,effect=None,color_mode=False,brightness=127,mired=250,hue=0,saturation=64)
  self.mac=MAC;self.initial_queries=0
 async def async_initial_status(self):
  self.initial_queries+=1;self.feed();return self.status_1911.latest
 @asynccontextmanager
 async def status_transaction(self):yield
 def apply(self,c):
  if c.kind is K.POWER_ON:self.state['power']=True
  elif c.kind is K.POWER_OFF:self.state.update(power=False,effect=None)
  elif c.kind is K.BRIGHTNESS_ON:self.state.update(brightness=p.ha_brightness_to_device(c.value),power=True)
  elif c.kind is K.COLOR:
   self.state.update(color_mode=True,hue=round(c.value[0]%360*254/360)%254,saturation=round(c.value[1]*255/100),effect=None)
  elif c.kind is K.CCT:self.state.update(color_mode=False,mired=round(1_000_000/c.value),effect=None)
  elif c.kind is K.EFFECT_START:self.state['effect']=c.value
  elif c.kind is K.EFFECT_STOP:self.state['effect']=None
 def feed(self,correct=True):
  state=dict(self.state)
  if not correct:state['power']=not state['power']
  self.status_1911.feed(payload(state),KEY,self.status_1911.session_generation)
 async def async_send_selected_once(self,selector,expected_session=None):
  if expected_session is not None and (not self.connected or expected_session!=self.status_1911.session_generation):raise ConnectionError("session lost")
  if self.hook:self.hook("prepare")
  selected=selector()
  if selected is None:return Result(Status.NOT_SELECTED)
  command=self.manager.in_flight;self.frames[selected.frame]=command;self.controls.append(command.kind);self.apply(command)
  if self.behavior=='correct':self.feed()
  elif self.behavior=='wrong':self.feed(False)
  if self.hook:self.hook('write')
  await asyncio.sleep(0)
  return Result(Status.SUCCESS,write_finished_monotonic=time.monotonic())
 async def async_poll_status(self,generation):
  self.polls+=1
  if self.hook:self.hook('poll')
  if self.behavior=='poll4' and self.polls>=4:self.feed()
  elif self.behavior=='wrong':self.feed(False)
 async def async_repeat_plaintext(self,frame,generation,valid):
  if self.hook:self.hook('repeat')
  if not valid():raise cc.ConfirmationSuperseded()
  if not self.connected:raise ConnectionError('lost')
  started=time.monotonic()
  self.replays.append(frame);self.apply(self.frames[frame])
  if self.behavior=='repeat':self.feed()
  elif self.behavior=='wrong':self.feed(False)
  return started,time.monotonic()
 async def async_status_delay(self,seconds,valid):
  if seconds:await asyncio.sleep(seconds)

class Tests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.auto=patch.object(cc,'CONFIRMATION_AUTO_WAIT_MS',5);self.window=patch.object(cc,'CONFIRMATION_POLL_WINDOW_MS',5)
  self.auto.start();self.window.start()
 def tearDown(self):self.auto.stop();self.window.stop()
 def setup_group(self,behavior='correct',on=True):
  m=CommandManager(S(True,on),minimum_interval_ms=0);c=FakeClient(m,behavior);e=CommandExecutor(c,m,mesh_id=MID);return m,c,e
 def test_cycle_white_intermediate_modes_keep_effect_identity(self):
  from awox112.advertisement import parse_awox_advertisement
  for mode in (4,5):
   with self.subTest(mode=mode):
    store=Status1911Store(MAC,MID);store.begin(1)
    packet=payload(dict(mode=mode,hue=255,saturation=255,mired=250))
    decoded=p.parse_status_1911(KEY,packet)
    self.assertEqual(decoded['mode'],mode)
    store.feed(packet,KEY,1);state=store.latest
    self.assertEqual(state.effect,'Color cycle');self.assertEqual(state.power,bool(mode&1))
    self.assertFalse(state.color_mode);self.assertEqual(state.mired,250)
    self.assertIsNone(state.hue)
    self.assertTrue(state.matches(dict(power=bool(mode&1))))
    self.assertFalse(state.matches(dict(effect=None,color_mode=False,mired=250)))
    ad=bytearray(17);ad[:2]=bytes.fromhex('9620');ad[6:8]=bytes((3,0));ad[9:11]=MID.to_bytes(2,'little')
    ad[11]=mode;ad[12]=127;ad[13:15]=(250).to_bytes(2,'little');ad[15:17]=b'\xff\xff'
    parsed=parse_awox_advertisement(bytes(ad))
    self.assertEqual(parsed.effect,'Color cycle');self.assertTrue(parsed.effect_state_known)
    self.assertEqual(parsed.color_temp_mired,250)
 def test_unknown_effect_bits_are_not_promoted_to_supported_status(self):
  from awox112.advertisement import parse_awox_advertisement
  for mode in (8,9,20,21,31,128):
   with self.assertRaisesRegex(ValueError,'Unsupported 1911 state'):
    p.parse_status_1911(KEY,payload(dict(mode=mode)))
   ad=bytearray(17);ad[6:8]=bytes((3,0));ad[11]=mode
   self.assertFalse(parse_awox_advertisement(bytes(ad)).effect_state_known)
 def test_native_effect_hue_ff_preserves_packet_without_inventing_angle(self):
  from awox112.advertisement import parse_awox_advertisement
  for mode in (6,7,18,19):
   with self.subTest(mode=mode):
    store=Status1911Store(MAC,MID);store.begin(1)
    for outer in (128,130):
     parsed=p.parse_status_1911(KEY,payload(dict(mode=mode,hue=255,saturation=255),outer_type=outer))
     self.assertEqual(parsed['hue'],255)
    store.feed(payload(dict(mode=mode,hue=255,saturation=255)),KEY,1)
    state=store.latest;lamp=state.as_lamp_state()
    self.assertEqual(state.hue,255);self.assertEqual(lamp.hue_raw,255)
    self.assertEqual(state.saturation,255);self.assertIsNone(lamp.hue_degrees)
    self.assertEqual(store.invalid_frames,0)
    self.assertTrue(state.matches(dict(effect=state.effect,power=bool(mode&1))))
    for hue in (0,1,253,254,255):self.assertFalse(state.matches(dict(hue=hue)))
    ad=bytearray(17);ad[6:8]=bytes((3,0));ad[11]=mode;ad[15:17]=b'\xff\xff'
    parsed=parse_awox_advertisement(bytes(ad))
    self.assertEqual(parsed.hue_raw,255);self.assertIsNone(parsed.hue_degrees)
    self.assertEqual(parsed.effect,state.effect)
 async def test_cycle_ff_status_confirms_effect_without_poll_or_repeat(self):
  m,c,e=self.setup_group('silent')
  def response(stage):
   if stage=='write' and c.state['effect']=='Color cycle':
    c.status_1911.feed(payload(dict(c.state,color_mode=True,hue=255,saturation=255)),KEY,1)
  c.hook=response;m.submit(T(effect='Color cycle'))
  await e.async_execute_group()
  self.assertEqual(e.last_batch_outcome,'confirmed');self.assertEqual(e.last_confirmed_state.hue,255)
  self.assertEqual(c.polls,0);self.assertFalse(c.replays)
 async def test_intermediate_waits_for_automatic_final_cct_status(self):
  m,c,e=self.setup_group('silent');c.state.update(effect='Color cycle',color_mode=True)
  m.set_state(S(True,True,'Color cycle'))
  async def final():
   await asyncio.sleep(.003);c.feed()
  tasks=[]
  def response(stage):
   if stage=='write' and c.state['effect'] is None and not c.state['color_mode']:
    c.status_1911.feed(payload(dict(c.state,mode=5)),KEY,1)
    tasks.append(asyncio.create_task(final()))
  c.hook=response;m.submit(T(cct=4000))
  await e.async_execute_group();await asyncio.gather(*tasks)
  self.assertEqual(e.last_batch_outcome,'confirmed');self.assertEqual(e.last_confirmed_state.mode,1)
  self.assertEqual(c.polls,0);self.assertFalse(c.replays)
 async def test_intermediate_alone_cannot_confirm_effect_stopped(self):
  m,c,e=self.setup_group('silent')
  def response(stage):
   if stage in ('write','poll','repeat'):
    c.status_1911.feed(payload(dict(c.state,mode=5)),KEY,1)
  c.hook=response;m.submit(T(cct=4000))
  with self.assertRaises(CommandExecutionFailed):await e.async_execute_group()
  self.assertEqual(c.polls,14);self.assertTrue(c.replays)
 def test_effect_reports_preserve_ha_color_cct_and_color_mode_from_both_sources(self):
  from awox112.advertisement import parse_awox_advertisement
  for source in ('1911','advertisement'):
   for initial_mode in (ColorMode.HS,ColorMode.COLOR_TEMP):
    for mode in (4,5,6,7,16,17,18,19):
     with self.subTest(source=source,initial_mode=initial_mode,mode=mode):
      m,c,_=self.setup_group();entity=AwoxConnectZLight(c,{'mesh_id':MID},minimum_command_interval_ms=0)
      entity.hass=Hass();entity._attr_hs_color=(123.,45.);entity._attr_color_temp_kelvin=3500;entity._attr_color_mode=initial_mode
      store=Status1911Store(MAC,MID);store.begin(1);store.feed(payload(dict(mode=mode,hue=255,saturation=255,brightness=200,mired=250)),KEY,1)
      if source=='1911':entity._async_apply_status_1911(store.latest)
      else:
       raw=bytearray(17);raw[6:8]=bytes((3,0));raw[9:11]=MID.to_bytes(2,'little');raw[11]=mode;raw[12]=200;raw[13:15]=(250).to_bytes(2,'little');raw[15:17]=b'\xff\xff'
       entity._async_apply_advertisement_state(parse_awox_advertisement(bytes(raw)))
      self.assertEqual(entity._attr_hs_color,(123.,45.));self.assertEqual(entity._attr_color_temp_kelvin,3500)
      self.assertEqual(entity._attr_color_mode,initial_mode);self.assertEqual(entity._attr_brightness,200)
      self.assertEqual(entity._attr_is_on,bool(mode&1));self.assertEqual(entity._attr_effect,store.latest.effect)
 def test_static_reports_resume_appearance_updates_after_effect(self):
  for source in ('1911','advertisement'):
   for color_mode in (False,True):
    with self.subTest(source=source,color_mode=color_mode):
     m,c,_=self.setup_group();entity=AwoxConnectZLight(c,{'mesh_id':MID},minimum_command_interval_ms=0);entity.hass=Hass()
     entity._attr_effect='Candle';entity._attr_hs_color=(10.,20.);entity._attr_color_temp_kelvin=3500
     store=Status1911Store(MAC,MID);store.begin(1);store.feed(payload(dict(color_mode=color_mode,hue=127,saturation=255,mired=250)),KEY,1)
     if source=='1911':entity._async_apply_status_1911(store.latest)
     else:entity._async_apply_advertisement_state(store.latest.as_lamp_state())
     self.assertEqual(entity._attr_effect,'off')
     if color_mode:self.assertEqual(entity._attr_hs_color,(180.,100.));self.assertEqual(entity._attr_color_mode,ColorMode.HS)
     else:self.assertEqual(entity._attr_color_temp_kelvin,4000);self.assertEqual(entity._attr_color_mode,ColorMode.COLOR_TEMP)
 def test_query_matches_previously_tested_wire_format(self):
  self.assertEqual(p.make_status_query(KEY,MID).hex(),"028ee344b1b405f693bcfb5977ff02389b")
 def test_parser_preserves_direct_and_mesh_types(self):
  for kind in (0x80,0x82):
   decoded=p.parse_status_1911(KEY,payload({},outer_type=kind))
   self.assertEqual(decoded['outer_type'],kind)
   self.assertEqual(decoded['mesh_id'],MID)
  with self.assertRaises(ValueError):p.parse_status_1911(KEY,payload({},outer_type=0x81))
 def test_mesh_reply_cannot_publish_or_replace_direct_state(self):
  store=Status1911Store(MAC,MID);store.begin(1);published=[]
  store.listeners.add(published.append);store.changed.clear()
  store.feed(payload({},outer_type=0x82),KEY,1)
  self.assertIsNone(store.latest);self.assertIsNone(store.last_received)
  self.assertEqual(store.sequence,0);self.assertFalse(published)
  self.assertFalse(store.changed.is_set());self.assertEqual(store.ignored_mesh_frames,1)
  store.feed(payload(dict(power=False)),KEY,1);direct=store.latest
  self.assertEqual(direct.outer_type,0x80)
  store.feed(payload(dict(power=True),outer_type=0x82),KEY,1)
  self.assertIs(store.latest,direct);self.assertIs(store.last_received,direct)
  self.assertEqual(len(published),1);self.assertEqual(store.diagnostics()['ignored_mesh_frames'],2)
 async def test_mesh_only_feedback_cannot_confirm_group(self):
  m,c,e=self.setup_group(behavior='missing',on=False)
  def mesh_feedback(stage):
   if stage in ('write','poll','repeat'):
    c.status_1911.feed(payload(c.state,outer_type=0x82),KEY,1)
  c.hook=mesh_feedback;m.submit(T(power=True))
  with self.assertRaises(CommandExecutionFailed):await e.async_execute_until_idle()
  self.assertEqual(c.polls,14)
  self.assertTrue(c.replays)
  self.assertIsNone(c.status_1911.latest)
  self.assertGreater(c.status_1911.ignored_mesh_frames,0)
 def test_status_hue_uses_254_scale_and_preserves_raw(self):
  store=Status1911Store(MAC,MID);store.begin(1)
  for hue in (0,1,32,64,127,190,220,252,253,254):
   store.feed(payload(dict(color_mode=True,hue=hue)),KEY,1)
   state=store.latest;lamp=state.as_lamp_state()
   self.assertEqual(state.hue,hue);self.assertEqual(lamp.hue_raw,hue)
   self.assertAlmostEqual(lamp.hue_degrees,(hue*360/254)%360)
  store.feed(payload(dict(color_mode=True,hue=127)),KEY,1)
  self.assertEqual(store.latest.as_lamp_state().hue_degrees,180)
 def test_advertisement_hue_uses_same_scale_and_preserves_raw(self):
  from awox112.advertisement import parse_awox_advertisement
  for hue in (0,1,32,64,127,190,220,252,253,254):
   raw=bytearray(17);raw[:2]=bytes.fromhex('9620');raw[6:8]=bytes((3,0))
   raw[9:11]=MID.to_bytes(2,'little');raw[11]=3;raw[12]=127
   raw[15]=hue;raw[16]=254
   lamp=parse_awox_advertisement(bytes(raw))
   self.assertEqual(lamp.hue_raw,hue)
   self.assertAlmostEqual(lamp.hue_degrees,(hue*360/254)%360)
 def test_hue_wrap_matches_without_normalizing_other_fields(self):
  store=Status1911Store(MAC,MID);store.begin(1)
  for received,expected in ((0,254),(254,0),(252,252),(253,253),(0,0),(1,1)):
   store.feed(payload(dict(color_mode=True,hue=received,saturation=0,brightness=0)),KEY,1)
   self.assertTrue(store.latest.matches(dict(hue=expected)))
   self.assertFalse(store.latest.matches(dict(saturation=254)))
   self.assertFalse(store.latest.matches(dict(brightness=254)))
  store.feed(payload(dict(color_mode=True,hue=0)),KEY,1)
  self.assertFalse(store.latest.matches(dict(hue=252)))
  self.assertFalse(store.latest.matches(dict(hue=255)))
 async def test_hue_sender_normalizes_red_endpoint_before_confirmation(self):
  m,c,e=self.setup_group()
  original=c.feed
  def normalized_feed(correct=True):
   hue=c.state['hue']
   if hue==254:c.state['hue']=0
   try:original(correct)
   finally:c.state['hue']=hue
  c.feed=normalized_feed;m.submit(T(color=(359.9,100)))
  await e.async_execute_group()
  self.assertTrue(any(frame[8]==0 for frame in c.frames))
  self.assertEqual(e.last_confirmed_state.hue,0)
  self.assertFalse(m.pending_generations);self.assertEqual(c.polls,0);self.assertFalse(c.replays)
 def test_brightness_direct_mapping_all_nonzero_values(self):
  for value in range(1,256):
   self.assertEqual(p.ha_brightness_to_device(value),value)
   self.assertEqual(p.make_brightness(value,0,mesh_id=MID)[8],value)
 def test_hue_sender_never_sends_duplicate_endpoints(self):
  for degrees in [0,1,180,358,359,359.9,360,720]+[i/10 for i in range(3601)]:
   packet=p.make_hs_color(degrees,100,0,mesh_id=MID)
   self.assertEqual(packet[8],round((degrees%360)*254/360)%254)
   self.assertLess(packet[8],254);self.assertEqual(packet[9],255)
 def test_saturation_scale_and_full_brightness_both_sources(self):
  from awox112.advertisement import parse_awox_advertisement
  store=Status1911Store(MAC,MID);store.begin(1)
  for raw_sat in (0,1,127,128,254,255):
   store.feed(payload(dict(color_mode=True,hue=254,saturation=raw_sat,brightness=255)),KEY,1)
   lamp=store.latest.as_lamp_state()
   self.assertEqual(lamp.brightness,255);self.assertEqual(lamp.brightness_raw,255)
   self.assertEqual(lamp.hue_raw,254);self.assertAlmostEqual(lamp.saturation_percent,raw_sat*100/255)
   raw=bytearray(17);raw[:2]=bytes.fromhex('9620');raw[6:8]=bytes((3,0));raw[9:11]=MID.to_bytes(2,'little')
   raw[11]=3;raw[12]=255;raw[15]=254;raw[16]=raw_sat
   ad=parse_awox_advertisement(bytes(raw))
   self.assertEqual(ad.brightness,255);self.assertEqual(ad.hue_raw,254)
   self.assertEqual(ad.saturation_raw,raw_sat);self.assertAlmostEqual(ad.saturation_percent,raw_sat*100/255)
  for percent in (0,25,50,75,100):
   self.assertEqual(p.make_hs_color(180,percent,0,mesh_id=MID)[9],round(percent*255/100))
 async def test_brightness_zero_plans_power_off_without_brightness_write(self):
  m,c,e=self.setup_group();m.submit(T(brightness=0));await e.async_execute_group()
  self.assertEqual(c.controls,[K.POWER_OFF]);self.assertFalse(e.last_confirmed_state.power)
 async def test_full_brightness_and_saturation_confirm_from_actual_bytes(self):
  m,c,e=self.setup_group();m.submit(T(color=(359.9,100),brightness=255));await e.async_execute_group()
  self.assertEqual(e.last_confirmed_state.brightness,255)
  self.assertEqual(e.last_confirmed_state.hue,0);self.assertEqual(e.last_confirmed_state.saturation,255)
  self.assertEqual(c.polls,0);self.assertFalse(c.replays)
 def test_hue_ff_rejected_in_static_color_mode(self):
  for outer in (0x80,0x82):
   with self.assertRaisesRegex(ValueError,'hue FF'):
    p.parse_status_1911(KEY,payload(dict(color_mode=True,hue=255,saturation=255,brightness=255),outer_type=outer))
   raw=p.parse_status_1911(KEY,payload(dict(color_mode=True,hue=254,saturation=255,brightness=255),outer_type=outer))
   self.assertEqual((raw['hue'],raw['saturation'],raw['brightness']),(254,255,255))
   white=p.parse_status_1911(KEY,payload(dict(color_mode=False,hue=255,saturation=255,brightness=255),outer_type=outer))
   self.assertEqual(white['brightness'],255)
 def test_rejected_hue_raw_packet_retained_without_publishing_state(self):
  store=Status1911Store(MAC,MID);store.begin(1);published=[];store.listeners.add(published.append)
  raw=payload(dict(color_mode=True,hue=255,saturation=255,brightness=255))
  store.feed(raw,KEY,1)
  self.assertIsNone(store.latest);self.assertFalse(published);self.assertEqual(store.sequence,0)
  self.assertEqual(store.invalid_frames,1)
  self.assertEqual(store.diagnostics()['last_rejected_status']['raw'],raw.hex())
  self.assertIn('hue FF',store.diagnostics()['last_rejected_status']['reason'])
 def test_advertisement_static_hue_ff_rejected_but_white_placeholders_valid(self):
  from awox112.advertisement import parse_awox_advertisement
  raw=bytearray(17);raw[:2]=bytes.fromhex('9620');raw[6:8]=bytes((3,0));raw[9:11]=MID.to_bytes(2,'little')
  raw[11]=3;raw[12]=255;raw[15]=255;raw[16]=255
  self.assertIsNone(parse_awox_advertisement(bytes(raw)))
  raw[15]=254;ad=parse_awox_advertisement(bytes(raw))
  self.assertEqual((ad.hue_raw,ad.saturation_raw,ad.brightness_raw),(254,255,255))
  raw[11]=1;raw[15]=255;ad=parse_awox_advertisement(bytes(raw))
  self.assertIsNone(ad.hue_raw);self.assertIsNone(ad.saturation_raw);self.assertEqual(ad.brightness,255)
 async def test_unsupported_static_hue_ff_cannot_confirm_command_group(self):
  m,c,e=self.setup_group()
  def unsupported_feed(correct=True):
   state=dict(c.state,color_mode=True,hue=255)
   c.status_1911.feed(payload(state),KEY,1)
  c.feed=unsupported_feed;m.submit(T(color=(120,100)))
  with self.assertRaises(CommandExecutionFailed):await e.async_execute_group()
  self.assertEqual(c.polls,14);self.assertTrue(c.replays)
  self.assertIsNone(c.status_1911.latest);self.assertGreater(c.status_1911.invalid_frames,0)
 def notify_fixture(self,cccd=False,native=False,api_error=None,write_error=None,pause_init=False,standard_error=None):
  class API:
   def __init__(self):self.calls=0;self.removed=0;self.callback=None
   async def bluetooth_gatt_start_notify(self,address,handle,callback,timeout):
    self.calls+=1
    if api_error:raise api_error
    self.callback=callback
    async def stop():pass
    def remove():self.removed+=1;self.callback=None
    return stop,remove
  class ESPBackend:
   def __init__(self,api):self._client=api;self._address_as_int=2;self._notify_cancels={}
  api=API();backend=ESPBackend(api)
  characteristic=types.SimpleNamespace(handle=18,properties=['notify'],get_descriptor=lambda uuid:object() if cccd else None)
  class BLE:
   is_connected=True
   def __init__(self):
    self._backend=object() if native else backend
    self.services=types.SimpleNamespace(get_characteristic=lambda uuid:characteristic)
    self.standard=0;self.writes=[];self.init_started=asyncio.Event()
   async def start_notify(self,uuid,callback):
    self.standard+=1
    if standard_error:raise standard_error
   async def write_gatt_char(self,uuid,data,**kw):
    self.init_started.set()
    if write_error:raise write_error
    if pause_init:await asyncio.Event().wait()
    self.writes.append(bytes(data))
   async def disconnect(self):
    self.is_connected=False
    for stop,remove in list(backend._notify_cancels.values()):remove()
    backend._notify_cancels.clear()
  ble=BLE();client=AwoxConnectZClient(Hass(),MAC,'mesh','password',default_transition=0,idle_disconnect=10,command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,configured_mesh_id=MID)
  client._client=ble;client._session_key=KEY;client._session_generation=1
  fake_module=types.ModuleType('bleak_esphome.backend.client');fake_module.ESPHomeClient=ESPBackend
  return client,ble,api,backend,fake_module
 async def test_esphome_no_cccd_uses_vendor_subscription_once_and_receives(self):
  client,ble,api,backend,mod=self.notify_fixture()
  with patch.dict(sys.modules,{'bleak_esphome.backend.client':mod}):
   await client._async_enable_status();await client._async_enable_status()
  self.assertEqual(api.calls,1);self.assertEqual(ble.standard,0);self.assertEqual(ble.writes,[b'\x01'])
  self.assertIn(18,backend._notify_cancels);self.assertEqual(client._status_notify_method,'esphome_vendor_no_cccd')
  api.callback(18,bytearray(payload(dict(brightness=255))))
  self.assertEqual(client.status_1911.latest.brightness,255)
  await client.async_close();self.assertFalse(backend._notify_cancels);self.assertEqual(api.removed,1)
 async def test_esphome_with_cccd_keeps_standard_bleak(self):
  client,ble,api,backend,mod=self.notify_fixture(cccd=True)
  with patch.dict(sys.modules,{'bleak_esphome.backend.client':mod}):await client._async_enable_status()
  self.assertEqual(ble.standard,1);self.assertEqual(api.calls,0);self.assertEqual(client._status_notify_method,'standard_bleak')
  await client.async_close()
 async def test_non_esphome_keeps_standard_bleak(self):
  client,ble,api,backend,mod=self.notify_fixture(native=True)
  with patch.dict(sys.modules,{'bleak_esphome.backend.client':mod}):await client._async_enable_status()
  self.assertEqual(ble.standard,1);self.assertEqual(api.calls,0)
  await client.async_close()
 async def test_esphome_subscribe_failure_is_not_hidden(self):
  client,ble,api,backend,mod=self.notify_fixture(api_error=RuntimeError('subscribe failed'))
  with patch.dict(sys.modules,{'bleak_esphome.backend.client':mod}):
   with self.assertRaisesRegex(RuntimeError,'subscribe failed'):await client._async_enable_status()
  self.assertFalse(client.connected);self.assertFalse(client.status_1911.active);self.assertFalse(ble.writes)
 async def test_vendor_init_write_failure_cleans_subscription(self):
  client,ble,api,backend,mod=self.notify_fixture(write_error=RuntimeError('01 failed'))
  with patch.dict(sys.modules,{'bleak_esphome.backend.client':mod}):
   with self.assertRaisesRegex(RuntimeError,'01 failed'):await client._async_enable_status()
  self.assertFalse(client.connected);self.assertFalse(backend._notify_cancels);self.assertEqual(api.removed,1)
 async def test_cancelled_vendor_init_cleans_subscription(self):
  client,ble,api,backend,mod=self.notify_fixture(pause_init=True)
  with patch.dict(sys.modules,{'bleak_esphome.backend.client':mod}):
   task=asyncio.create_task(client._async_enable_status());await ble.init_started.wait();task.cancel()
   with self.assertRaises(asyncio.CancelledError):await task
  self.assertFalse(client.connected);self.assertFalse(backend._notify_cancels);self.assertEqual(api.removed,1)
 async def test_standard_notify_errors_do_not_trigger_vendor_bypass(self):
  client,ble,api,backend,mod=self.notify_fixture(cccd=True,standard_error=RuntimeError('normal notify failed'))
  with patch.dict(sys.modules,{'bleak_esphome.backend.client':mod}):
   with self.assertRaisesRegex(RuntimeError,'normal notify failed'):await client._async_enable_status()
  self.assertEqual(api.calls,0);self.assertFalse(client.connected)
 def test_status_reads_color_and_cct(self):
  store=Status1911Store(MAC,MID);store.begin(1)
  store.feed(payload(dict(color_mode=True,hue=169,saturation=190)),KEY,1)
  self.assertTrue(store.latest.matches(dict(hue=169,saturation=190,color_mode=True)))
  self.assertIsNone(store.latest.mired)
  store.feed(payload(dict(color_mode=False,mired=370)),KEY,1)
  self.assertEqual(store.latest.as_lamp_state().color_temp_kelvin,2703)
 def test_invalid_identity_old_session_and_bad_frames_rejected(self):
  s=Status1911Store(MAC,MID);s.begin(2)
  for packet,gen in [(payload({}),1),(payload({},mid=1234),2),(payload({},mac='00:11:22:33:44:55'),2),(payload({})[:19],2),(payload({})[:-1]+b'\x00',2)]:s.feed(packet,KEY,gen)
  self.assertIsNone(s.latest);s.end();s.feed(payload({}),KEY,2);self.assertIsNone(s.latest)
 async def test_combined_group_and_both_power_finalizers_confirm_at_end(self):
  m,c,e=self.setup_group(on=False);c.state['power']=False
  m.submit(T(color=(120,75),brightness=200,effect='Candle'))
  await e.async_execute_group()
  self.assertEqual(c.controls,[K.COLOR,K.BRIGHTNESS_ON,K.POWER_ON,K.EFFECT_START,K.POWER_ON])
  self.assertFalse(m.pending_generations);self.assertEqual(c.polls,0);self.assertIsNotNone(e.last_confirmed_state)
 async def test_written_generation_stays_pending_until_barrier(self):
  m,c,e=self.setup_group('poll4');m.submit(T(brightness=200));g=m.request_generation
  def hook(stage):
   if stage=='poll':self.assertIn(g,m.pending_generations)
  c.hook=hook;await e.async_execute_group();self.assertEqual(c.polls,4);self.assertNotIn(g,m.pending_generations)
 async def test_multiple_generations_in_same_batch(self):
  m,c,e=self.setup_group();m.submit(T(color=(120,75)));first=m.request_generation;m.submit(T(brightness=200));second=m.request_generation
  await e.async_execute_group();self.assertFalse(m.pending_generations);self.assertEqual(c.confirmation.diagnostics['batches'],1)
 async def test_wrong_or_missing_status_repeats_entire_identical_batch_once(self):
  for mode in ['wrong','silent']:
   m,c,e=self.setup_group(mode);m.submit(T(cct=3500,brightness=200))
   with self.assertRaises(CommandExecutionFailed):await e.async_execute_group()
   self.assertEqual(c.polls,14);self.assertEqual(len(c.replays),3)
   self.assertEqual([c.frames[f].kind for f in c.replays],c.controls)
   self.assertFalse(m.state.known);self.assertFalse(m.pending_generations)
 async def test_repeat_can_confirm_without_repeating_semantic_effect(self):
  m,c,e=self.setup_group('repeat');m.submit(T(effect='Candle'))
  await e.async_execute_group();self.assertEqual(c.polls,7);self.assertEqual(len(c.replays),2)
  self.assertEqual(c.controls,[K.EFFECT_START,K.POWER_ON]);self.assertFalse(m.pending_generations)
 async def test_new_off_waits_for_complete_group_repeat(self):
  m,c,e=self.setup_group('repeat');m.submit(T(brightness=200));old=m.request_generation
  def hook(stage):
   if stage=='poll' and c.polls==1:m.submit(T(power=False))
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(len(c.replays),2);self.assertEqual(e.last_batch_outcome,'confirmed')
  self.assertNotIn(old,m.pending_generations);self.assertTrue(c.state['power'])
  self.assertEqual(m.peek_next_command().kind,K.POWER_OFF)
  c.hook=None;c.behavior='correct';await e.async_execute_group()
  self.assertFalse(c.state['power']);self.assertFalse(m.pending_generations)
 async def test_new_request_at_repeat_boundary_does_not_cancel_frozen_replay(self):
  m,c,e=self.setup_group('repeat');m.submit(T(effect='Candle'));seen=[]
  def hook(stage):
   if stage=='repeat' and not seen:seen.append(True);m.submit(T(power=False))
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(len(c.replays),2);self.assertEqual(e.last_batch_outcome,'confirmed')
  self.assertEqual(m.peek_next_command().kind,K.POWER_OFF)
 async def test_session_loss_never_reconnects_or_replays_old_commands(self):
  m,c,e=self.setup_group('silent');m.submit(T(effect='Candle'))
  c.hook=lambda stage:setattr(c,'connected',False) if stage=='poll' else None
  with self.assertRaises(CommandExecutionFailed):await e.async_execute_group()
  self.assertFalse(c.replays);self.assertFalse(m.state.known)
 async def test_stale_previous_reply_cannot_confirm_new_group(self):
  m,c,e=self.setup_group('silent');c.feed();m.submit(T(power=False))
  with self.assertRaises(CommandExecutionFailed):await e.async_execute_group()
  self.assertEqual(c.polls,14)
 async def test_cancellation_releases_barrier_and_leaves_unknown(self):
  m,c,e=self.setup_group('silent');m.submit(T(effect='Candle'))
  task=asyncio.create_task(e.async_execute_group());await asyncio.sleep(.002);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertFalse(m.pending_generations);self.assertFalse(e.busy);self.assertFalse(m.state.known)
 async def test_listener_sees_complete_session_scoped_states(self):
  s=Status1911Store(MAC,MID);seen=[];s.listeners.add(seen.append);s.begin(3);s.feed(payload(dict(effect='Candle')),KEY,3)
  self.assertEqual(seen[0].session_generation,3);self.assertEqual(seen[0].effect,'Candle')
 async def test_real_client_notify_once_and_disconnect_invalidates(self):
  client=AwoxConnectZClient(types.SimpleNamespace(),MAC,'mesh','password',default_transition=0,idle_disconnect=10,command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,configured_mesh_id=MID)
  class BLE:
   is_connected=True
   def __init__(self):self.notifications=0;self.writes=[]
   async def start_notify(self,uuid,cb):self.notifications+=1;self.cb=cb
   async def write_gatt_char(self,uuid,data,**kw):self.writes.append((uuid,data))
  ble=BLE();client._client=ble;client._session_key=KEY;client._session_generation=1
  await client._async_enable_status();await client._async_enable_status()
  self.assertEqual(ble.notifications,1);self.assertEqual([v[1] for v in ble.writes],[b'\x01'])
  ble.cb(None,payload({}));self.assertIsNotNone(client.status_1911.latest)
  client.status_1911.end();ble.cb(None,payload({}));self.assertIsNone(client.status_1911.latest)

 async def test_ha_waiter_is_not_completed_by_successful_writes(self):
  m,c,_=self.setup_group('silent')
  entity=AwoxConnectZLight(c,{'mesh_id':MID},minimum_command_interval_ms=0)
  entity.hass=Hass();c.manager=entity._manager;entity._manager.set_state(S(True,True))
  c.status_1911.listeners.add(entity._async_apply_status_1911)
  task=asyncio.create_task(entity._async_submit_target(T(brightness=200)))
  await asyncio.sleep(.002)
  self.assertTrue(c.controls);self.assertFalse(task.done());self.assertTrue(entity._request_waiters)
  c.feed();await asyncio.wait_for(task,.3)
  self.assertFalse(entity._request_waiters);self.assertFalse(entity._manager.pending_generations)
 async def test_ha_unknown_bootstrap_only_when_needed(self):
  m,c,_=self.setup_group();entity=AwoxConnectZLight(c,{'mesh_id':MID},minimum_command_interval_ms=0)
  entity.hass=Hass();c.manager=entity._manager
  await entity._async_submit_target(T(brightness=200));self.assertEqual(c.initial_queries,1)
  await entity._async_submit_target(T(brightness=180));self.assertEqual(c.initial_queries,1)
 async def test_advertisement_and_status_intermediates_do_not_replan_busy_group(self):
  m,c,_=self.setup_group();entity=AwoxConnectZLight(c,{'mesh_id':MID},minimum_command_interval_ms=0)
  entity.hass=Hass();entity._manager.set_state(S(True,True));entity._executor.busy=True
  c.state['power']=False;c.feed();state=c.status_1911.latest
  entity._async_apply_status_1911(state);entity._async_apply_advertisement_state(state.as_lamp_state())
  self.assertTrue(entity._manager.state.is_on)
  entity._executor.busy=False;entity._async_apply_status_1911(state);self.assertFalse(entity._manager.state.is_on)
 async def test_link_and_availability_updates_do_not_publish_stale_light_state_while_busy(self):
  m,c,_=self.setup_group();entity=AwoxConnectZLight(c,{'mesh_id':MID},minimum_command_interval_ms=0)
  entity.hass=Hass();writes=[];entity.async_write_ha_state=lambda:writes.append(entity._attr_is_on)
  entity._attr_is_on=True;entity._executor.busy=True
  c.connected=False;entity._last_ble_connected=False
  c.connected=True;entity._async_connection_changed()
  self.assertTrue(entity._last_ble_connected);self.assertEqual(writes,[])
  entity._async_availability_changed(True)
  self.assertEqual(writes,[])
  entity._executor.busy=False;c.connected=False;entity._async_connection_changed()
  self.assertEqual(writes,[True])
  entity._async_availability_changed(False)
  self.assertEqual(writes,[True,True])
 async def test_generation_abort_discards_barrier_only_work(self):
  m,c,e=self.setup_group();m.submit(T(effect='Candle'));g=m.request_generation
  await e.async_execute_next();await e.async_execute_next()
  self.assertIn(g,m.pending_generations);m.abort_pending_through(g)
  self.assertFalse(m.pending_generations)
 async def test_initial_state_polls_are_bounded_and_no_hidden_control(self):
  client=AwoxConnectZClient(Hass(),MAC,'mesh','password',default_transition=0,idle_disconnect=10,command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,configured_mesh_id=MID)
  class BLE:
   is_connected=True
   def __init__(self):self.writes=[];self.responses=[]
   async def start_notify(self,uuid,cb):self.cb=cb
   async def write_gatt_char(self,uuid,data,**kw):self.writes.append(data);self.responses.append(kw.get("response"))
  ble=BLE();client._client=ble;client._session_key=KEY;client._session_generation=1
  async def connected(**kw):pass
  client._async_ensure_connected=connected
  with patch('awox112.client.CONFIRMATION_POLL_WINDOW_MS',1):
   with self.assertRaises(Exception):await client.async_initial_status()
  self.assertEqual(len(ble.writes),8);self.assertEqual(ble.writes[0],b'\x01')
  self.assertTrue(all(v[0]==2 for v in ble.writes[1:]))
  if client._idle_task:client._idle_task.cancel()
 async def test_real_client_checks_latest_request_inside_repeat_lock(self):
  client=AwoxConnectZClient(Hass(),MAC,'mesh','password',default_transition=0,idle_disconnect=10,command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,configured_mesh_id=MID)
  class BLE:
   is_connected=True
   async def write_gatt_char(self,*a,**k):raise AssertionError('Must not write obsolete command')
  client._client=BLE();client._session_key=KEY;client._session_generation=1;client._status_ready_generation=1;client.status_1911.begin(1)
  with self.assertRaises(cc.ConfirmationSuperseded):await client.async_repeat_plaintext(bytes(16),1,lambda:False)
 async def test_transition_target_can_match_before_fade_deadline(self):
  m,c,e=self.setup_group();m.submit(T(color=(120,80),appearance_transition=10))
  started=time.monotonic();await e.async_execute_group()
  self.assertLess(time.monotonic()-started,.2);self.assertEqual(c.polls,0)
 async def test_transition_window_waits_for_explicit_fade_before_polling(self):
  m,c,e=self.setup_group('silent');m.submit(T(color=(120,80),appearance_transition=.1))
  task=asyncio.create_task(e.async_execute_group());await asyncio.sleep(.015)
  self.assertEqual(c.polls,0);c.feed();await task
 async def test_cancel_one_owner_prevents_shared_batch_replay(self):
  m,c,e=self.setup_group('silent');m.submit(T(color=(120,75)));g=m.request_generation;m.submit(T(brightness=200))
  c.hook=lambda stage:m.cancel_generation(g) if stage=='poll' else None
  with self.assertRaises(CommandExecutionFailed):await e.async_execute_group()
  self.assertFalse(c.replays);self.assertFalse(m.pending_generations);self.assertFalse(m.state.known)
 async def test_new_off_cannot_replace_running_power_finalizer(self):
  m,c,e=self.setup_group();m.submit(T(brightness=200));new=[]
  def hook(stage):
   if stage=='write' and len(c.controls)==1:m.submit(T(power=False));new.append(m.request_generation)
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(c.controls,[K.BRIGHTNESS_ON,K.POWER_ON])
  self.assertIn(new[0],m.pending_generations)
  c.hook=None;await e.async_execute_group()
  self.assertEqual(c.controls[-1],K.POWER_OFF);self.assertFalse(m.pending_generations)
 async def test_new_on_consumed_by_older_finalizer_waits_for_confirmation(self):
  m,c,e=self.setup_group('silent');m.submit(T(brightness=200));new=[]
  def hook(stage):
   if stage=='write' and len(c.controls)==1:m.submit(T(power=True));new.append(m.request_generation)
   if stage=='poll':self.assertIn(new[0],m.pending_generations);c.feed()
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(e.last_batch_outcome,'confirmed');self.assertFalse(m.pending_generations)
 async def test_disconnect_interrupts_long_transition_wait(self):
  m,c,e=self.setup_group('silent');m.submit(T(brightness=200,brightness_transition=10))
  task=asyncio.create_task(e.async_execute_group());await asyncio.sleep(.01);c.connected=False
  with self.assertRaises(CommandExecutionFailed):await asyncio.wait_for(task,.3)
 async def test_new_color_waits_until_original_brightness_group_is_confirmed(self):
  m,c,e=self.setup_group();m.submit(T(color=(120,75),brightness=200));called=[]
  def hook(stage):
   if stage=='prepare' and len(c.controls)==1 and not called:
    called.append(True);m.submit(T(color=(210,60)))
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(c.controls,[K.COLOR,K.BRIGHTNESS_ON,K.POWER_ON]);self.assertEqual(e.last_confirmed_state.brightness,200)
  self.assertEqual(e.last_confirmed_state.hue,round(120*254/360))
  c.hook=None;await e.async_execute_group()
  self.assertEqual(c.controls,[K.COLOR,K.BRIGHTNESS_ON,K.POWER_ON,K.COLOR]);self.assertFalse(m.pending_generations)
 async def test_loss_during_inter_command_delay_never_reopens_old_session(self):
  m,c,e=self.setup_group();m.set_minimum_interval_ms(20);m.submit(T(brightness=200))
  def hook(stage):
   if stage=='write' and len(c.controls)==1:
    asyncio.get_running_loop().call_later(.005,setattr,c,'connected',False)
  c.hook=hook
  with self.assertRaises(CommandExecutionFailed):await e.async_execute_group()
  self.assertEqual(c.controls,[K.BRIGHTNESS_ON]);self.assertFalse(m.state.known)
 async def test_new_noop_on_cannot_complete_from_optimistic_state(self):
  m,c,_=self.setup_group('silent');entity=AwoxConnectZLight(c,{'mesh_id':MID},minimum_command_interval_ms=0)
  entity.hass=Hass();c.manager=entity._manager;entity._manager.set_state(S(True,True))
  first=asyncio.create_task(entity._async_submit_target(T(brightness=200)))
  await asyncio.sleep(.002)
  second=asyncio.create_task(entity._async_submit_target(T(power=True)))
  await asyncio.sleep(.001);self.assertFalse(first.done());self.assertFalse(second.done())
  c.feed();await asyncio.wait_for(asyncio.gather(first,second),.3)
  self.assertEqual(c.controls,[K.BRIGHTNESS_ON,K.POWER_ON]);self.assertFalse(entity._manager.pending_generations)
 async def test_cancelled_active_group_aborts_waiting_noop_without_false_success(self):
  m,c,e=self.setup_group('silent');m.submit(T(brightness=200));old=m.request_generation
  task=asyncio.create_task(e.async_execute_group());await asyncio.sleep(.002)
  m.submit(T(power=True));new=m.request_generation;m.cancel_generation(old)
  self.assertIn(new,m.pending_generations)
  with self.assertRaises(CommandExecutionFailed) as error:await task
  self.assertIn(new,error.exception.failed_generations);self.assertFalse(m.pending_generations);self.assertFalse(m.state.known)
 async def test_waiting_stop_is_planned_after_effect_start_group(self):
  m,c,e=self.setup_group(on=False);c.state['power']=False;m.submit(T(effect='Candle'));seen=[]
  def hook(stage):
   if stage=='write' and not seen:seen.append(True);m.submit(T(stop_effect=True))
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(c.controls,[K.EFFECT_START,K.POWER_ON]);self.assertEqual(m.peek_next_command().kind,K.EFFECT_STOP)
  c.hook=None;await e.async_execute_group()
  self.assertIsNone(c.state['effect']);self.assertFalse(m.pending_generations)
 async def test_real_runtime_transport_initializes_once_and_does_not_reconnect_group(self):
  from awox112.client import RuntimeSelectedCommand
  client=AwoxConnectZClient(Hass(),MAC,'mesh','password',default_transition=0,idle_disconnect=10,command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,configured_mesh_id=MID)
  class BLE:
   is_connected=True
   def __init__(self):self.writes=[];self.notify_count=0;self.responses=[]
   async def start_notify(self,uuid,cb):self.notify_count+=1
   async def write_gatt_char(self,uuid,data,**kw):self.writes.append(data);self.responses.append(kw.get("response"))
   async def disconnect(self):self.is_connected=False
  ble=BLE();client._client=ble;client._session_key=KEY;client._session_generation=1;preparations=[]
  async def connected(**kw):preparations.append(True)
  client._async_ensure_connected=connected
  selector=lambda:RuntimeSelectedCommand(p.make_power(True,mesh_id=MID),'power:on')
  async with client.status_transaction():
   self.assertEqual((await client.async_send_selected_once(selector)).status,Status.SUCCESS)
   self.assertEqual((await client.async_send_selected_once(selector,expected_session=1)).status,Status.SUCCESS)
   self.assertEqual(ble.notify_count,1);self.assertEqual(ble.writes[0],b'\x01');self.assertEqual(len(ble.writes),3);self.assertEqual(ble.responses,[False,False,False])
   ble.is_connected=False
   self.assertEqual((await client.async_send_selected_once(selector,expected_session=1)).status,Status.PREPARATION_FAILED)
   self.assertEqual(len(preparations),1);self.assertEqual(len(ble.writes),3)

 async def test_waiting_latest_slots_merge_without_changing_running_group(self):
  m,c,e=self.setup_group();m.submit(T(color=(10,80),brightness=210));queued=[]
  def hook(stage):
   if stage=='write' and len(c.controls)==1:
    for t in (T(color=(100,70)),T(color=(240,60)),T(brightness=150)):
     m.submit(t);queued.append(m.request_generation)
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(c.controls,[K.COLOR,K.BRIGHTNESS_ON,K.POWER_ON])
  self.assertNotIn(queued[0],m.pending_generations);self.assertIn(queued[1],m.pending_generations);self.assertIn(queued[2],m.pending_generations)
  self.assertEqual(e.last_confirmed_state.brightness,210)
  c.hook=None;await e.async_execute_group()
  self.assertEqual(c.controls,[K.COLOR,K.BRIGHTNESS_ON,K.POWER_ON,K.COLOR,K.BRIGHTNESS_ON,K.POWER_ON])
  self.assertEqual(e.last_confirmed_state.brightness,150);self.assertEqual(e.last_confirmed_state.hue,round(240*254/360));self.assertFalse(m.pending_generations)

 async def test_request_before_first_selection_still_uses_latest_wins(self):
  m,c,e=self.setup_group();m.submit(T(color=(10,80)));seen=[]
  def hook(stage):
   if stage=='prepare' and not seen:seen.append(True);m.submit(T(color=(240,60)))
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(c.controls,[K.COLOR]);self.assertEqual(e.last_confirmed_state.hue,round(240*254/360));self.assertFalse(m.pending_generations)

 async def test_queued_effect_waits_and_color_supersedes_it(self):
  m,c,e=self.setup_group();m.submit(T(brightness=200));seen=[]
  def hook(stage):
   if stage=='write' and not seen:
    seen.append(True);m.submit(T(effect='Candle'));m.submit(T(color=(220,80)))
  c.hook=hook;await e.async_execute_group();c.hook=None;await e.async_execute_group()
  self.assertNotIn(K.EFFECT_START,c.controls);self.assertIsNone(e.last_confirmed_state.effect)
  self.assertEqual(c.controls,[K.BRIGHTNESS_ON,K.POWER_ON,K.COLOR])

 async def test_queued_off_discards_older_waiting_appearance_and_brightness(self):
  m,c,e=self.setup_group();m.submit(T(effect='Candle'));seen=[]
  def hook(stage):
   if stage=='write' and not seen:
    seen.append(True);m.submit(T(color=(220,80),brightness=150));m.submit(T(power=False))
  c.hook=hook;await e.async_execute_group();c.hook=None;await e.async_execute_group()
  self.assertEqual(c.controls,[K.EFFECT_START,K.POWER_ON,K.POWER_OFF]);self.assertFalse(e.last_confirmed_state.power)

 async def test_cancel_waiting_generation_does_not_cancel_running_group(self):
  m,c,e=self.setup_group();m.submit(T(brightness=200));seen=[]
  def hook(stage):
   if stage=='write' and not seen:
    seen.append(True);m.submit(T(color=(220,80)));m.cancel_generation(m.request_generation)
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(e.last_batch_outcome,'confirmed');self.assertFalse(m.pending_generations);self.assertEqual(c.controls,[K.BRIGHTNESS_ON,K.POWER_ON])

 async def test_failed_active_group_aborts_queued_requests_without_auto_replay(self):
  m,c,e=self.setup_group('silent');m.submit(T(brightness=200));queued=[]
  def hook(stage):
   if stage=='poll' and not queued:
    m.submit(T(color=(220,80)));queued.append(m.request_generation)
  c.hook=hook
  with self.assertRaises(CommandExecutionFailed) as err:await e.async_execute_group()
  self.assertIn(queued[0],err.exception.failed_generations);self.assertFalse(m.state.known)
  self.assertFalse(m.pending_generations);self.assertEqual(c.controls,[K.BRIGHTNESS_ON,K.POWER_ON]);self.assertEqual(len(c.replays),2)
  self.assertIsNone(m.peek_next_command())

 async def test_effect_start_while_off_retains_its_finalizer_with_waiting_request(self):
  m,c,e=self.setup_group(on=False);c.state['power']=False;m.submit(T(effect='Candle'));seen=[]
  def hook(stage):
   if stage=='write' and not seen:seen.append(True);m.submit(T(color=(220,80)))
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(c.controls,[K.EFFECT_START,K.POWER_ON]);self.assertNotIn(K.EFFECT_STOP,c.controls)
  c.hook=None;await e.async_execute_group();self.assertIsNone(e.last_confirmed_state.effect)

 async def test_ha_pending_replacements_complete_without_superseded_errors(self):
  m,c,_=self.setup_group('silent');entity=AwoxConnectZLight(c,{'mesh_id':MID},minimum_command_interval_ms=0)
  entity.hass=Hass();c.manager=entity._manager;entity._manager.set_state(S(True,True))
  with patch.object(cc,'CONFIRMATION_AUTO_WAIT_MS',100):
   first=asyncio.create_task(entity._async_submit_target(T(brightness=200)))
   while len(c.controls)<2:await asyncio.sleep(0)
   second=asyncio.create_task(entity._async_submit_target(T(color=(100,70))));await asyncio.sleep(0)
   third=asyncio.create_task(entity._async_submit_target(T(color=(240,60))));await asyncio.sleep(0)
   fourth=asyncio.create_task(entity._async_submit_target(T(brightness=150)));await asyncio.sleep(0)
   self.assertFalse(first.done());self.assertFalse(third.done());self.assertFalse(fourth.done())
   self.assertEqual(c.controls,[K.BRIGHTNESS_ON,K.POWER_ON]);c.behavior='correct';c.feed()
   await asyncio.wait_for(asyncio.gather(first,second,third,fourth),.5)
  self.assertEqual(c.controls,[K.BRIGHTNESS_ON,K.POWER_ON,K.COLOR,K.BRIGHTNESS_ON,K.POWER_ON])
  self.assertFalse(entity._request_waiters);self.assertFalse(entity._manager.pending_generations)

 async def test_waiting_stop_uses_confirmed_effect_not_optimistic_state(self):
  m,c,e=self.setup_group('silent');m.submit(T(brightness=200));seen=[]
  def hook(stage):
   if stage=='write' and not seen:seen.append(True);m.submit(T(stop_effect=True))
   if stage=='poll':c.state['effect']='Candle';c.feed()
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(m.state.effect,'Candle');self.assertEqual(m.peek_next_command().kind,K.EFFECT_STOP)
  c.hook=None;c.behavior='correct';await e.async_execute_group()
  self.assertIsNone(e.last_confirmed_state.effect);self.assertFalse(m.pending_generations)

 async def test_group_and_waiting_group_keep_minimum_control_spacing(self):
  m,c,e=self.setup_group();m.set_minimum_interval_ms(10);m.submit(T(effect='Candle'));times=[]
  def hook(stage):
   if stage=='write':
    times.append(time.monotonic())
    if len(times)==1:m.submit(T(color=(220,80)))
  c.hook=hook;await e.async_execute_group();await e.async_execute_group()
  self.assertEqual(c.controls,[K.EFFECT_START,K.POWER_ON,K.COLOR])
  self.assertTrue(all(b-a>=.009 for a,b in zip(times,times[1:])))

class FadeDelayTests(unittest.IsolatedAsyncioTestCase):
 setUp=Tests.setUp
 tearDown=Tests.tearDown
 setup_group=Tests.setup_group
 async def test_fade_spacing_before_effect_even_when_status_already_matches(self):
  m,c,e=self.setup_group();m.set_minimum_interval_ms(20);times=[]
  c.hook=lambda stage:times.append(time.monotonic()) if stage=='write' else None
  m.submit(T(color=(120,80),effect='Candle',appearance_transition=.1))
  await e.async_execute_group()
  self.assertEqual(c.controls,[K.COLOR,K.EFFECT_START,K.POWER_ON])
  self.assertGreaterEqual(times[1]-times[0],.14)
  self.assertGreaterEqual(times[2]-times[1],.019)
 async def test_actual_encoded_transition_drives_spacing(self):
  m,c,e=self.setup_group();m.submit(T(color=(120,80),effect='Candle',appearance_transition=.19));times=[]
  c.hook=lambda stage:times.append(time.monotonic()) if stage=='write' else None
  await e.async_execute_group()
  frame=next(frame for frame,command in c.frames.items() if command.kind is K.COLOR)
  encoded=int.from_bytes(frame[10:12],'little')/10
  self.assertGreaterEqual(times[1]-times[0],1.25*encoded-.005)
 async def test_single_final_color_confirms_before_its_long_budget(self):
  m,c,e=self.setup_group();m.submit(T(color=(210,75),appearance_transition=30));start=time.monotonic()
  await e.async_execute_group()
  self.assertLess(time.monotonic()-start,.2);self.assertEqual(c.polls,0)
 async def test_early_confirmation_does_not_release_next_group_too_soon(self):
  m,c,e=self.setup_group();m.set_minimum_interval_ms(20);times=[]
  c.hook=lambda stage:times.append(time.monotonic()) if stage=='write' else None
  m.submit(T(color=(120,80),appearance_transition=.1));await e.async_execute_group()
  m.submit(T(power=False));await e.async_execute_group()
  self.assertEqual(c.controls,[K.COLOR,K.POWER_OFF]);self.assertGreaterEqual(times[1]-times[0],.14)
 async def test_group_repeat_observes_fade_spacing(self):
  m,c,e=self.setup_group('repeat');m.set_minimum_interval_ms(20);times=[]
  c.hook=lambda stage:times.append(time.monotonic()) if stage=='repeat' else None
  m.submit(T(color=(120,80),effect='Candle',appearance_transition=.1))
  await e.async_execute_group()
  self.assertEqual(len(c.replays),3);self.assertGreaterEqual(times[1]-times[0],.14)

if __name__=='__main__':unittest.main(verbosity=2)
