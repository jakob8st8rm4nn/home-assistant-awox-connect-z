"""Focused regressions against production modules with simulated HA/BLE/HTTP."""
import asyncio
import importlib
import sys
import types
import unittest
from contextlib import suppress
from unittest.mock import AsyncMock, patch

import test_status_confirmation as fx

# These stubs exercise our control flow, not real Home Assistant API compatibility.
class Flow:
 def __init_subclass__(cls, **kwargs):super().__init_subclass__()
 def async_abort(self, **kw):return dict(type='abort',**kw)
 def async_show_form(self, **kw):return dict(type='form',**kw)
 def _set_confirm_only(self):pass

vol=fx.module('voluptuous',Schema=lambda value:value,Required=lambda key,**kw:key,
 Optional=lambda key,**kw:key,All=lambda *a:a,Coerce=lambda x:x,Range=lambda **kw:kw)
config=sys.modules['homeassistant.config_entries']
config.ConfigFlow=Flow;config.OptionsFlowWithReload=Flow;config.ConfigFlowResult=dict;config.SOURCE_BLUETOOTH='bluetooth'
sys.modules['homeassistant.const'].CONF_PASSWORD='password'
sys.modules['homeassistant.const'].Platform=types.SimpleNamespace(LIGHT='light',SENSOR='sensor')
sys.modules['homeassistant.const'].SIGNAL_STRENGTH_DECIBELS_MILLIWATT='dBm'
sys.modules['homeassistant.const'].EntityCategory=types.SimpleNamespace(DIAGNOSTIC='diagnostic')
sys.modules['homeassistant.exceptions'].ConfigEntryNotReady=type('ConfigEntryNotReady',(Exception,),{})
fx.module('homeassistant.helpers.aiohttp_client',async_get_clientsession=lambda h:None)
select_names=('BooleanSelector','NumberSelector','NumberSelectorConfig','SelectSelector','SelectSelectorConfig',
 'TextSelector','TextSelectorConfig')
selectors=fx.module('homeassistant.helpers.selector',SelectOptionDict=lambda **kw:kw,
 NumberSelectorMode=types.SimpleNamespace(BOX='box'),TextSelectorType=types.SimpleNamespace(PASSWORD='password'))
for name in select_names:setattr(selectors,name,lambda *a,**kw:kw)
fx.module('homeassistant.components.sensor',RestoreSensor=fx.Entity,SensorEntityDescription=lambda **kw:types.SimpleNamespace(**kw),
 SensorDeviceClass=types.SimpleNamespace(SIGNAL_STRENGTH='rssi',TIMESTAMP='timestamp',ENUM='enum'),
 SensorStateClass=types.SimpleNamespace(MEASUREMENT='measurement'))
fx.module('bluetooth_data_tools',parse_advertisement_data_bytes=lambda raw:(None,None,None,{}))
from awox112 import cloud,config_flow,diagnostics,sensor
from awox112.const import *
source=fx.SOURCE/'__init__.py'
spec=importlib.util.spec_from_file_location('awox112.setup_audit',source)
setup=importlib.util.module_from_spec(spec);sys.modules[spec.name]=setup;spec.loader.exec_module(setup)

class PipelineTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.fixture=fx.Tests();self.fixture.setUp()
 def tearDown(self):self.fixture.tearDown()
 def entity(self,on=False):
  m,c,e=self.fixture.setup_group('silent',on=on);c.state['power']=on
  entity=fx.AwoxConnectZLight(c,{'mesh_id':fx.MID},minimum_command_interval_ms=0)
  entity.hass=fx.Hass();c.manager=entity._manager;entity._manager.set_state(fx.S(True,on))
  return entity,c
 async def test_cancel_first_brightness_invalidates_state_and_following_on_bootstraps(self):
  entity,c=self.entity();task=None
  def hook(stage):
   if stage=='write':task.cancel()
  c.hook=hook;task=asyncio.create_task(entity._async_submit_target(fx.T(brightness=200)))
  with self.assertRaises(asyncio.CancelledError):await task
  worker=entity._command_worker
  if worker:await worker
  self.assertFalse(entity._manager.state.known)
  self.assertEqual(c.controls,[fx.K.BRIGHTNESS_ON]);self.assertFalse(entity._request_waiters)
  c.hook=None;c.behavior='correct'
  await entity._async_submit_target(fx.T(power=True))
  self.assertEqual(c.initial_queries,1)
 async def test_cancel_selected_request_aborts_queued_request(self):
  entity,c=self.entity();task=None;queued=[]
  def hook(stage):
   if stage=='write' and not queued:
    c.manager.submit(fx.T(power=False));queued.append(c.manager.request_generation);task.cancel()
  c.hook=hook;task=asyncio.create_task(entity._async_submit_target(fx.T(brightness=200)))
  with self.assertRaises(asyncio.CancelledError):await task
  worker=entity._command_worker
  if worker:await worker
  self.assertFalse(c.manager.state.known);self.assertFalse(c.manager.pending_generations)
  self.assertEqual(c.controls,[fx.K.BRIGHTNESS_ON])
 async def test_frozen_group_keeps_old_brightness(self):
  m,c,e=self.fixture.setup_group();m.submit(fx.T(color=(10,80),brightness=210));seen=[]
  def hook(stage):
   if stage=='write' and not seen:seen.append(True);m.submit(fx.T(brightness=150))
  c.hook=hook;await e.async_execute_group()
  self.assertEqual(e.last_confirmed_state.brightness,210)
  c.hook=None;await e.async_execute_group();self.assertEqual(e.last_confirmed_state.brightness,150)
 async def test_group_goal_only_derived_once(self):
  m,c,e=self.fixture.setup_group('silent');m.submit(fx.T(color=(10,80),brightness=100));reads=[]
  prop=fx.cc.ConfirmationBatch.goal
  def read(batch):reads.append(True);return prop.fget(batch)
  with patch.object(fx.cc.ConfirmationBatch,'goal',property(read)):
   with self.assertRaises(fx.CommandExecutionFailed):await e.async_execute_group()
  self.assertEqual(len(reads),1);self.assertEqual(c.polls,14);self.assertEqual(len(c.replays),3)
 async def test_unknown_passes_reschedulable_command_budget(self):
  client=fx.AwoxConnectZClient(fx.Hass(),fx.MAC,'mesh','password',default_transition=0,
   idle_disconnect=10,command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,configured_mesh_id=fx.MID)
  class BLE:
   is_connected=True
   async def disconnect(self):self.is_connected=False
  client._client=BLE();client._session_key=fx.KEY;client._session_generation=1
  budgets=[]
  async def ensure(**kw):budgets.append(kw['runtime_command_timeout'])
  async def enable():client.status_1911.begin(1);client.status_1911.feed(fx.payload({}),fx.KEY,1)
  client._async_ensure_connected=ensure;client._async_enable_status=enable
  await client.async_initial_status()
  self.assertEqual(len(budgets),1);self.assertIsInstance(budgets[0],asyncio.Timeout)
  if client._idle_task:
   client._idle_task.cancel()
   with suppress(asyncio.CancelledError):await client._idle_task
 def test_remove_generation_keeps_unrelated_slots_and_transitions(self):
  m=fx.CommandManager(fx.S(True,True),minimum_interval_ms=0)
  m.submit(fx.T(color=(10,80),appearance_transition=2));old=m.request_generation
  m.submit(fx.T(brightness=100,brightness_transition=3));new=m.request_generation
  m.cancel_generation(old)
  self.assertIsNone(m.pending.color);self.assertIsNone(m.pending.appearance_transition)
  self.assertEqual(m.pending.brightness,100);self.assertEqual(m.pending.brightness_transition,3)
  self.assertIn(new,m.pending_generations)

class CloudTests(unittest.IsolatedAsyncioTestCase):
 def row(self,**kw):return dict(type='.ble.light',macAddress='AA:BB:CC:DD:EE:FF',**kw)
 def test_missing_and_reserved_destinations_are_not_imported(self):
  for value in (None,0,65535):
   row=self.row() if value is None else self.row(address=value)
   self.assertEqual(cloud._convert_devices([row]),[])
 def test_invalid_duplicate_does_not_hide_valid_destination(self):
  rows=cloud._convert_devices([self.row(),self.row(address=40157)])
  self.assertEqual([r['mesh_id'] for r in rows],[40157])
 def test_signed_destination_is_preserved(self):
  self.assertEqual(cloud._convert_devices([self.row(address=-25379)])[0]['mesh_id'],40157)
 def test_first_valid_duplicate_is_deterministic(self):
  rows=cloud._convert_devices([self.row(address=10),self.row(address=20)])
  self.assertEqual([r['mesh_id'] for r in rows],[10])
 async def test_first_server_transport_failure_second_succeeds(self):
  calls=[]
  async def post(hass,url,**kw):
   calls.append(url)
   if len(calls)==1:raise cloud.AwoxCloudError('simulated transport failure')
   return 200,dict(objectId='owner',sessionToken='token')
  async def fetch(*a,**kw):
   if a[5]=='Credential':return [dict(service='zigbee',client_id='mesh',access_token='password')]
   return [self.row(address=100)]
  with patch.object(cloud,'_post_json',post),patch.object(cloud,'_fetch_class',fetch):
   result=await cloud.async_import_account(None,'user@example.invalid','password')
  self.assertEqual(len(calls),2);self.assertEqual(result.devices[0]['mesh_id'],100)
 async def test_auth_rejection_not_hidden_by_later_transport_failure(self):
  calls=[]
  async def post(*a,**kw):
   calls.append(True)
   if len(calls)==1:return 404,dict(code=101,error='invalid login')
   raise cloud.AwoxCloudError('later transport failure')
  with patch.object(cloud,'_post_json',post):
   with self.assertRaises(cloud.AwoxInvalidAuth):await cloud.async_import_account(None,'test','password')
 async def test_results_structure_validated(self):
  for value in (None,{},['not a row']):
   with self.subTest(value=value),patch.object(cloud,'_post_json',AsyncMock(return_value=(200,dict(results=value)))):
    with self.assertRaises(cloud.AwoxCloudError):
     await cloud._fetch_class(None,'url','id','token','owner','Device',owner_filtered=True)
 async def test_no_usable_targets_fails_before_setup(self):
  async def fetch(*a,**kw):
   if a[5]=='Credential':return [dict(service='zigbee',client_id='mesh',access_token='password')]
   return [self.row()]
  with patch.object(cloud,'_post_json',AsyncMock(return_value=(200,dict(objectId='owner',sessionToken='token')))),patch.object(cloud,'_fetch_class',fetch):
   with self.assertRaises(cloud.AwoxNoDevices):await cloud.async_import_account(None,'test','password')
 async def test_http_boundary_wraps_connection_error(self):
  from aiohttp import ClientConnectionError
  class Session:
   def post(self,*a,**kw):raise ClientConnectionError('failed')
  with patch.object(cloud,'async_get_clientsession',lambda h:Session()):
   with self.assertRaises(cloud.AwoxCloudError):await cloud._post_json(None,'url',headers={},payload={})
 async def test_http_boundary_rejects_non_object_json(self):
  class Response:
   status=200
   async def __aenter__(self):return self
   async def __aexit__(self,*a):pass
   async def json(self,**kw):return []
  session=types.SimpleNamespace(post=lambda *a,**kw:Response())
  with patch.object(cloud,'async_get_clientsession',lambda h:session):
   with self.assertRaises(cloud.AwoxCloudError):await cloud._post_json(None,'url',headers={},payload={})
 async def test_http_cancellation_propagates(self):
  class Session:
   def post(self,*a,**kw):raise asyncio.CancelledError()
  with patch.object(cloud,'async_get_clientsession',lambda h:Session()):
   with self.assertRaises(asyncio.CancelledError):await cloud._post_json(None,'url',headers={},payload={})

class FlowTests(unittest.IsolatedAsyncioTestCase):
 def flow(self):
  f=config_flow.AwoxConnectZConfigFlow();f._discovered_address='AA:BB:CC:DD:EE:FF'
  f._discovered_mesh_id=100;f._discovered_name='Test';return f
 def entry(self,id,devices=None):
  return types.SimpleNamespace(entry_id=id,data={CONF_SETUP_METHOD:SETUP_METHOD_LOCAL,
   CONF_MESH_NAME:'mesh',CONF_MESH_PASSWORD:'password',CONF_DEVICES:devices or []})
 async def test_bluetooth_final_check_rejects_new_owner(self):
  f=self.flow();target=self.entry('target');entries=[target];updates=[]
  f._configured_entries=lambda:entries
  async def match(address):
   entries.append(self.entry('other',[dict(mac=address)]));return target,False
  f._async_matching_account=match
  f.hass=types.SimpleNamespace(config_entries=types.SimpleNamespace(async_update_entry=lambda *a,**kw:updates.append(kw)))
  result=await f.async_step_bluetooth_confirm({})
  self.assertEqual(result['reason'],'device_already_configured');self.assertFalse(updates)
 async def test_bluetooth_final_check_rejects_duplicate_in_target(self):
  f=self.flow();target=self.entry('target');f._configured_entries=lambda:[target]
  async def match(address):target.data[CONF_DEVICES].append(dict(mac=address));return target,False
  f._async_matching_account=match
  result=await f.async_step_bluetooth_confirm({})
  self.assertEqual(result['reason'],'device_already_configured')
 async def test_bluetooth_removed_target_not_updated(self):
  f=self.flow();target=self.entry('target');entries=[target];f._configured_entries=lambda:entries
  async def match(address):entries.clear();return target,False
  f._async_matching_account=match
  result=await f.async_step_bluetooth_confirm({})
  self.assertEqual(result['reason'],'local_setup_incomplete')
 async def test_bluetooth_success_updates_current_target(self):
  f=self.flow();target=self.entry('target');f._configured_entries=lambda:[target]
  f._async_matching_account=AsyncMock(return_value=(target,False));updates=[]
  f.hass=types.SimpleNamespace(config_entries=types.SimpleNamespace(
   async_update_entry=lambda *a,**kw:updates.append(kw),async_reload=AsyncMock()))
  result=await f.async_step_bluetooth_confirm({})
  self.assertEqual(result['reason'],'device_added');self.assertEqual(len(updates[0]['data'][CONF_DEVICES]),1)
 async def test_scan_preserves_selected_survivors_and_selects_new_only(self):
  f=self.flow();f._local_candidates={'a':{},'b':{},'gone':{}};f._local_selected_addresses={'a','gone'}
  async def collect(**kw):f._local_candidates={'a':{},'b':{},'new':{}}
  f._async_collect_local_candidates=collect;await f._async_refresh_local_selection()
  self.assertEqual(f._local_selected_addresses,{'a','new'})
 async def test_changed_credentials_do_not_select_stale_account(self):
  f=self.flow();entry=self.entry('target');f._configured_entries=lambda:[entry]
  class Verifier:
   async def async_verify_mesh_credentials(self):entry.data[CONF_MESH_PASSWORD]='new'
   async def async_close(self):pass
  f._verification_client=lambda *a:Verifier()
  match,transient=await f._async_matching_account(f._discovered_address)
  self.assertIsNone(match);self.assertTrue(transient)
 def test_setup_type_is_explicit(self):
  with self.assertRaises(KeyError):self.flow()._entry_setup_method(types.SimpleNamespace(data={CONF_OWNER_ID:'old'}))
 def test_reset_error_fields(self):
  f=self.flow();f._local_failed_device='failed';f._local_unavailable_devices='missing';f._reset_local_errors()
  self.assertEqual(f._local_failed_device,'');self.assertEqual(f._local_unavailable_devices,'')

class SetupTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.clients=[];self.callbacks=[];self.cleanups=[];self.fail_forward=False;self.cancel_forward=False;self.registrations=0;self.fail_registration=0
  owner=self
  class Client:
   def __init__(self,*a,**kw):self.mac=a[1];self.task=None;self.closed=False;self.evidence=(None,None,None);owner.clients.append(self)
   def export_availability_evidence(self):return self.evidence
   def restore_availability_evidence(self,evidence):self.evidence=evidence
   def async_start_availability_tracking(self):self.task=asyncio.create_task(asyncio.Event().wait())
   async def async_close(self):
    self.closed=True
    if self.task:
     self.task.cancel()
     with suppress(asyncio.CancelledError):await self.task
  def register(*a,**kw):
   owner.registrations+=1
   if owner.registrations==owner.fail_registration:raise RuntimeError('registration failed')
   token=dict(active=True);owner.callbacks.append(token)
   return lambda:token.update(active=False)
  async def forward(*a):
   if self.cancel_forward:raise asyncio.CancelledError()
   if self.fail_forward:raise RuntimeError('forward failed')
  self.entry=types.SimpleNamespace(entry_id='test',version=2,options={},data={
   CONF_SETUP_METHOD:SETUP_METHOD_LOCAL,CONF_MESH_NAME:'mesh',CONF_MESH_PASSWORD:'password',
   CONF_DEVICES:[dict(mac=fx.MAC,mesh_id=fx.MID)]},async_on_unload=self.cleanups.append)
  self.hass=types.SimpleNamespace(data={},loop=asyncio.get_running_loop(),config_entries=types.SimpleNamespace(
   async_forward_entry_setups=forward,async_unload_platforms=AsyncMock(return_value=True)))
  self.patches=[patch.object(setup,'AwoxConnectZClient',Client),
   patch.object(setup.bluetooth,'async_register_advertisement_callback',register,create=True),
   patch.object(setup.bluetooth,'async_register_callback',register,create=True),
   patch.object(setup.bluetooth,'BluetoothScanningMode',types.SimpleNamespace(ACTIVE='active'),create=True)]
  for p in self.patches:p.start()
 async def asyncTearDown(self):
  for c in self.clients:await c.async_close()
  for p in reversed(self.patches):p.stop()
 async def test_forward_failure_cleans_tasks_callbacks_and_data(self):
  self.fail_forward=True
  with self.assertRaises(RuntimeError):await setup.async_setup_entry(self.hass,self.entry)
  self.assertTrue(all(c.closed and c.task.done() for c in self.clients));self.assertTrue(all(not c['active'] for c in self.callbacks))
  self.assertNotIn(DOMAIN,self.hass.data)
  for cleanup in self.cleanups:cleanup() # HA may call them again: idempotent.
 async def test_failure_in_registration_closes_partial_client(self):
  self.fail_registration=2
  with self.assertRaises(RuntimeError):await setup.async_setup_entry(self.hass,self.entry)
  self.assertTrue(all(c.closed for c in self.clients));self.assertTrue(all(not c['active'] for c in self.callbacks))
 async def test_cancellation_cleans_partial_setup(self):
  self.cancel_forward=True
  with self.assertRaises(asyncio.CancelledError):await setup.async_setup_entry(self.hass,self.entry)
  self.assertTrue(all(c.closed for c in self.clients));self.assertNotIn(DOMAIN,self.hass.data)
 async def test_other_entry_keeps_shared_connect_lock(self):
  lock=asyncio.Lock();self.hass.data[DOMAIN]={DATA_RUNTIME_CONNECT_LOCK:lock,'other':[]};self.fail_forward=True
  with self.assertRaises(RuntimeError):await setup.async_setup_entry(self.hass,self.entry)
  self.assertIs(self.hass.data[DOMAIN][DATA_RUNTIME_CONNECT_LOCK],lock)
  self.assertIn('other',self.hass.data[DOMAIN])
 async def test_success_then_unload_closes_client(self):
  self.assertTrue(await setup.async_setup_entry(self.hass,self.entry))
  self.assertFalse(self.clients[0].closed)
  self.assertTrue(await setup.async_unload_entry(self.hass,self.entry));self.assertTrue(self.clients[0].closed)
 async def test_old_schema_rejected_without_conversion(self):
  self.entry.version=1
  self.assertFalse(await setup.async_setup_entry(self.hass,self.entry));self.assertFalse(self.clients)
 async def test_missing_setup_type_rejected(self):
  self.entry.data.pop(CONF_SETUP_METHOD)
  self.assertFalse(await setup.async_setup_entry(self.hass,self.entry));self.assertFalse(self.clients)
 async def test_fresh_entry_without_options_uses_defaults(self):
  self.assertTrue(await setup.async_setup_entry(self.hass,self.entry));self.assertEqual(len(self.clients),1)

class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
 def test_sensor_reads_value_once_per_callback(self):
  class Client:
   mac=fx.MAC;reads=0
   @property
   def signal_strength(self):self.reads+=1;return -60
  c=Client();s=sensor.AwoxConnectZDiagnosticSensor(c,{'mesh_id':fx.MID},sensor.SENSOR_DESCRIPTIONS[0])
  s._async_diagnostics_changed();self.assertEqual(c.reads,1)
 def test_mesh_attribute_change_publishes_even_if_value_unchanged(self):
  c=types.SimpleNamespace(mac=fx.MAC,advertised_mesh_id=1,mesh_id_status='mismatch')
  s=sensor.AwoxConnectZDiagnosticSensor(c,{'mesh_id':fx.MID},sensor.SENSOR_DESCRIPTIONS[3]);writes=[]
  s.async_write_ha_state=lambda:writes.append(True)
  s._async_diagnostics_changed();c.advertised_mesh_id=fx.MID;c.mesh_id_status='match';s._async_diagnostics_changed()
  self.assertEqual(len(writes),2)
 async def test_export_omits_account_email_and_legacy_type_inference(self):
  entry=types.SimpleNamespace(entry_id='id',options={},data={CONF_SETUP_METHOD:SETUP_METHOD_LOCAL,CONF_EMAIL:'private@example.invalid',CONF_DEVICES:[]})
  hass=types.SimpleNamespace(data={DOMAIN:{'id':[]}})
  data=await diagnostics.async_get_config_entry_diagnostics(hass,entry)
  self.assertNotIn('account',data['entry']);self.assertEqual(data['entry']['setup_method'],SETUP_METHOD_LOCAL)

class TransportBoundaryTests(unittest.IsolatedAsyncioTestCase):
 def client(self,lock=None,semaphore=None):
  c=fx.AwoxConnectZClient(fx.Hass(),fx.MAC,'mesh','password',default_transition=0,
   idle_disconnect=10,command_semaphore=semaphore or asyncio.Semaphore(2),max_concurrent_commands=2,
   runtime_connect_lock=lock,configured_mesh_id=fx.MID)
  return c
 async def test_replay_timestamp_excludes_time_waiting_for_command_lock(self):
  c=self.client();writes=[]
  class BLE:
   is_connected=True
   async def write_gatt_char(self,*a,**kw):writes.append((fx.time.monotonic(),kw))
  c._client=BLE();c._session_key=fx.KEY;c._session_generation=1;c._status_ready_generation=1;c.status_1911.begin(1)
  await c._command_lock.acquire()
  task=asyncio.create_task(c.async_repeat_plaintext(bytes(16),1,lambda:True))
  await asyncio.sleep(.002);c.status_1911.feed(fx.payload({}),fx.KEY,1);stale=c.status_1911.latest.received_monotonic
  c._command_lock.release();started,finished=await task
  self.assertGreater(started,stale);self.assertLessEqual(started,finished)
  self.assertEqual(len(writes),1);self.assertIs(writes[0][1]['response'],False)
 async def test_replay_rechecks_validity_after_waiting_for_lock(self):
  c=self.client();writes=[]
  class BLE:
   is_connected=True
   async def write_gatt_char(self,*a,**kw):writes.append(a)
  c._client=BLE();c._session_key=fx.KEY;c._session_generation=1;c._status_ready_generation=1;c.status_1911.begin(1)
  valid=True;await c._command_lock.acquire()
  task=asyncio.create_task(c.async_repeat_plaintext(bytes(16),1,lambda:valid))
  await asyncio.sleep(0);valid=False;c._command_lock.release()
  with self.assertRaises(fx.cc.ConfirmationSuperseded):await task
  self.assertFalse(writes)
 async def test_two_cold_clients_serialize_only_connection_setup(self):
  lock=asyncio.Lock();a=self.client(lock);b=self.client(lock);active=0;maximum=0
  class BLE:
   def __init__(self,*args,**kw):self.is_connected=False
   async def connect(self,**kw):
    nonlocal active,maximum
    active+=1;maximum=max(maximum,active)
    await asyncio.sleep(.005);self.is_connected=True;active-=1
   async def disconnect(self):self.is_connected=False
  for c in (a,b):
   c._async_find_device_runtime=lambda:object()
   c._actual_connection_source=lambda ble:None
   async def auth(c=c):c._session_key=fx.KEY;c._session_generation+=1
   c._async_authenticate=auth
  with patch('awox112.client.BleakClientWithServiceCache',BLE):
   await asyncio.gather(a._async_ensure_connected(fast_runtime=True),b._async_ensure_connected(fast_runtime=True))
  self.assertEqual(maximum,1);self.assertTrue(a.connected and b.connected)
  await a.async_close();await b.async_close()
 async def test_warm_client_can_write_while_another_waits_for_connect_lock(self):
  lock=asyncio.Lock();await lock.acquire();cold=self.client(lock);warm=self.client(lock)
  class BLE:
   is_connected=True
   async def write_gatt_char(self,*a,**kw):pass
   async def disconnect(self):self.is_connected=False
  warm._client=BLE();warm._session_key=fx.KEY;warm._async_enable_status=AsyncMock()
  cold_budget=asyncio.timeout(1)
  async def wait_cold():
   async with cold_budget:await cold._async_acquire_runtime_connect_lock(cold_budget)
   lock.release()
  task=asyncio.create_task(wait_cold());await asyncio.sleep(0)
  from awox112.client import RuntimeSelectedCommand
  result=await warm.async_send_selected_once(lambda:RuntimeSelectedCommand(bytes(16),'test'))
  self.assertEqual(result.status,fx.Status.SUCCESS);self.assertFalse(task.done())
  lock.release();await task;await warm.async_close()
 async def test_connect_queue_wait_does_not_consume_active_budget(self):
  lock=asyncio.Lock();await lock.acquire();c=self.client(lock)
  async def unlock():await asyncio.sleep(.06);lock.release()
  release=asyncio.create_task(unlock())
  async with asyncio.timeout(.03) as budget:
   await c._async_acquire_runtime_connect_lock(budget);lock.release()
   await asyncio.sleep(.005)
  await release
 def test_diagnostic_snapshot_exports_sticky_advertisement_history(self):
  c=self.client();info=types.SimpleNamespace(time=100.,source='proxy',rssi=-60)
  c._latest_service_info=lambda:info
  c.bluetooth_source_name=lambda source:source
  data=c.diagnostics()
  self.assertEqual(data['last_bluetooth_source_id'],'proxy');self.assertEqual(data['signal_strength'],-60)
  self.assertNotIn('last_command',data);self.assertNotIn('last_error',data)
  self.assertIn('command',data);self.assertIn('error',data)

class SharedStateTests(unittest.TestCase):
 def test_sources_share_field_conversion_across_modes_and_boundaries(self):
  from awox112.advertisement import lamp_state_from_fields,parse_awox_advertisement
  for mode in SUPPORTED_STATUS_MODES:
   for hue in (0,1,253,254,255):
    if mode in (2,3) and hue==255:continue
    with self.subTest(mode=mode,hue=hue):
     expected=lamp_state_from_fields(mesh_id=fx.MID,mode=mode,brightness=255,mired=250,hue=hue,saturation=255)
     raw=bytearray(17);raw[:2]=bytes.fromhex('9620');raw[6:8]=bytes.fromhex('0300')
     raw[9:11]=fx.MID.to_bytes(2,'little');raw[11]=mode;raw[12]=255;raw[13:15]=(250).to_bytes(2,'little');raw[15]=hue;raw[16]=255
     self.assertEqual(parse_awox_advertisement(raw),expected)
     store=fx.Status1911Store(fx.MAC,fx.MID);store.begin(1)
     store.feed(fx.payload(dict(mode=mode,brightness=255,mired=250,hue=hue,saturation=255)),fx.KEY,1)
     self.assertEqual(store.latest.as_lamp_state(),expected)
 def test_unknown_advertisement_mode_remains_unknown(self):
  from awox112.advertisement import lamp_state_from_fields
  state=lamp_state_from_fields(mesh_id=1,mode=8,brightness=1,mired=250,hue=None,saturation=None)
  self.assertFalse(state.effect_state_known)
 def test_white_sentinel_is_not_a_real_cct(self):
  from awox112.advertisement import lamp_state_from_fields
  for mired in (None,0,65535):
   state=lamp_state_from_fields(mesh_id=1,mode=1,brightness=1,mired=mired,hue=255,saturation=255)
   self.assertIsNone(state.color_temp_kelvin);self.assertIsNone(state.hue_raw)

if __name__=='__main__':unittest.main(verbosity=2)
