"""Deterministic cached-history/reload availability regressions."""
import asyncio,types,unittest
from unittest.mock import patch
import test_status_confirmation as fx
from awox112 import client as cm

class ReloadAvailabilityTests(unittest.IsolatedAsyncioTestCase):
 def make_client(self):
  hass=fx.Hass();hass.async_create_background_task=lambda coro,*args,**kwargs:asyncio.create_task(coro)
  return fx.AwoxConnectZClient(hass,fx.MAC,'mesh','password',default_transition=.2,idle_disconnect=10,command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,configured_mesh_id=fx.MID)
 async def test_reload_with_20_second_old_packet_expires_at_original_30(self):
  now=[100.0];info=types.SimpleNamespace(time=80.0,source='test',rssi=-60)
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:info
   c.async_start_availability_tracking();await asyncio.sleep(.01)
   self.assertTrue(c.available);self.assertEqual(c.bluetooth_status,'visible');seen=c.last_seen
   now[0]=109.9;c._availability_wakeup.set();await asyncio.sleep(.01);self.assertTrue(c.available)
   now[0]=110.01;c._availability_wakeup.set();await asyncio.sleep(.01)
   self.assertFalse(c.available);self.assertEqual(c.bluetooth_status,'unreachable');self.assertEqual(c.last_seen,seen)
   await c.async_close()
 async def test_exact_reported_reload_at_25_seconds_expires_at_30_not_55(self):
  now=[100.0];info=types.SimpleNamespace(time=75.0,source='test',rssi=-60)
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:info;c.bluetooth_source_name=lambda source:source
   c.async_start_availability_tracking();seen=c.last_seen
   self.assertTrue(c.available);self.assertEqual(c.bluetooth_status,'visible')
   self.assertEqual(c.signal_strength,-60);self.assertEqual(c.last_bluetooth_source,'test')
   now[0]=104.99;c._availability_wakeup.set();await asyncio.sleep(.01);self.assertTrue(c.available)
   now[0]=105.01;c._availability_wakeup.set();await asyncio.sleep(.01)
   self.assertFalse(c.available);self.assertEqual(c.bluetooth_status,'unreachable')
   self.assertEqual(c.signal_strength,-60);self.assertEqual(c.last_bluetooth_source,'test');self.assertEqual(c.last_seen,seen)
   await c.async_close()
 async def test_real_idle_disconnect_grace_remains_separate_from_last_seen(self):
  now=[100.0];info=types.SimpleNamespace(time=50.0,source='test',rssi=-60)
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:info;c._async_note_connection_liveness()
   c.async_start_availability_tracking();seen=c.last_seen
   now[0]=129.9;c._availability_wakeup.set();await asyncio.sleep(.01)
   self.assertTrue(c.available);self.assertEqual(c.bluetooth_status,'recently_connected');self.assertEqual(c.signal_strength,-60)
   now[0]=130.01;c._availability_wakeup.set();await asyncio.sleep(.01)
   self.assertFalse(c.available);self.assertEqual(c.bluetooth_status,'unreachable');self.assertEqual(c.signal_strength,-60);self.assertEqual(c.last_seen,seen)
   await c.async_close()
 async def test_stale_history_is_unavailable_immediately_after_reload(self):
  now=[100.0];info=types.SimpleNamespace(time=50.0,source='test',rssi=-60)
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:info;c.async_start_availability_tracking()
   self.assertFalse(c.available);self.assertEqual(c.bluetooth_status,'unreachable');await c.async_close()
 async def test_repeated_reload_does_not_extend_known_history(self):
  now=[100.0];info=types.SimpleNamespace(time=80.0,source='test',rssi=-60)
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   for t,expected in ((100,True),(109,True),(111,False),(130,False)):
    now[0]=t;c=self.make_client();c._latest_service_info=lambda:info;c.async_start_availability_tracking()
    self.assertEqual(c.available,expected);await c.async_close()
 async def test_no_history_stays_unknown_and_unavailable(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:None;c.async_start_availability_tracking()
   self.assertFalse(c.available);self.assertIsNone(c.bluetooth_status)
   now[0]=130.01;c._availability_wakeup.set();await asyncio.sleep(.01)
   self.assertFalse(c.available);self.assertIsNone(c.bluetooth_status);await c.async_close()
 async def test_real_new_packet_recovers_and_starts_its_own_deadline(self):
  now=[100.0];info=[types.SimpleNamespace(time=50.0,source='test',rssi=-60)]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:info[0];c.async_start_availability_tracking();self.assertFalse(c.available)
   info[0]=types.SimpleNamespace(time=101.0,source='test',rssi=-60);now[0]=101;c.async_note_bluetooth_liveness(101);self.assertTrue(c.available)
   now[0]=131.01;c._availability_wakeup.set();await asyncio.sleep(.01);self.assertFalse(c.available);await c.async_close()
 async def test_real_connection_evidence_is_not_limited_by_older_advertisement(self):
  now=[100.0];info=types.SimpleNamespace(time=50.0,source='test',rssi=-60)
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:info;c._async_note_connection_liveness();c.async_start_availability_tracking()
   self.assertTrue(c.available);self.assertEqual(c.bluetooth_status,'recently_connected')
   now[0]=130.01;c._availability_wakeup.set();await asyncio.sleep(.01);self.assertFalse(c.available);await c.async_close()
 async def test_live_connection_stays_available_even_with_old_history(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:None;c._client=types.SimpleNamespace(is_connected=True);c.async_start_availability_tracking()
   now[0]=200;c._availability_wakeup.set();await asyncio.sleep(.01);self.assertTrue(c.available);self.assertEqual(c.bluetooth_status,'connected');c._client=None;await c.async_close()
 async def test_unexpected_backend_disconnect_wakes_before_full_timeout(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])),patch.object(cm,'AVAILABILITY_RECOVERY_POLL_SECONDS',.01):
   c=self.make_client();c._latest_service_info=lambda:None;c._availability.connection_time=100.0
   backend=types.SimpleNamespace(is_connected=True);c._client=backend;c.async_start_availability_tracking();await asyncio.sleep(.003)
   now[0]=140.0;backend.is_connected=False;await asyncio.sleep(.035)
   self.assertFalse(c.available);self.assertEqual(c.bluetooth_status,'unreachable');c._client=None;await c.async_close()
 async def test_unexpected_disconnect_keeps_only_unexpired_real_evidence(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])),patch.object(cm,'AVAILABILITY_RECOVERY_POLL_SECONDS',.01):
   c=self.make_client();c._latest_service_info=lambda:None;c._availability.connection_time=100.0
   backend=types.SimpleNamespace(is_connected=True);c._client=backend;c.async_start_availability_tracking();await asyncio.sleep(.003)
   now[0]=105.0;backend.is_connected=False;await asyncio.sleep(.035)
   self.assertTrue(c.available);self.assertEqual(c.bluetooth_status,'recently_connected')
   now[0]=130.01;c._availability_wakeup.set();await asyncio.sleep(.02)
   self.assertFalse(c.available);c._client=None;await c.async_close()

 async def test_slightly_later_connection_deadline_has_no_unknown_gap(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:None
   c.async_note_bluetooth_liveness(100.0);seen=c.last_seen
   now[0]=100.15;c._async_note_connection_liveness()
   observed=[]
   for t in (129.99,130.01,130.14,130.151):
    now[0]=t;observed.append(c.bluetooth_status)
   self.assertEqual(observed,['recently_connected','recently_connected','recently_connected','unreachable'])
   self.assertEqual(c.last_seen,seen);self.assertEqual(c._availability.advertisement_time,100.0);self.assertEqual(c._availability.connection_time,100.15)
   await c.async_close()
 async def test_idle_disconnect_stays_recently_connected_until_newer_advertisement(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c.availability_timeout=30;c._latest_service_info=lambda:None
   c.async_note_bluetooth_liveness(90.0,source='proxy-a',rssi=-60)
   c._availability.connection_time=100.0
   self.assertEqual(c.bluetooth_status,'recently_connected')
   now[0]=105.0
   self.assertEqual(c.bluetooth_status,'recently_connected')
   c.async_note_bluetooth_liveness(105.0,source='proxy-b',rssi=-65)
   self.assertEqual(c.bluetooth_status,'visible')
   self.assertEqual(c.last_bluetooth_source_id,'proxy-b')
   await c.async_close()

 async def test_known_expired_evidence_never_falls_back_to_unknown_startup(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:None;c._availability.connection_time=60.0
   self.assertEqual(c.bluetooth_status,'unreachable');await c.async_close()


 async def test_idle_20_availability_10_publishes_only_visible_then_expiry(self):
  now=[100.0];info=types.SimpleNamespace(time=80.0,source='test',rssi=-60)
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c.idle_disconnect=20;c.availability_timeout=10
   c._latest_service_info=lambda:info;c.bluetooth_source_name=lambda source:source
   class Backend:
    is_connected=True
    async def disconnect(self):self.is_connected=False
   c._client=Backend();c._availability.connection_time=80.0
   seen=c.last_seen
   observed=[]
   c.async_add_diagnostic_listener(lambda:observed.append((c.bluetooth_status,c.last_bluetooth_source,c.available)))
   await c._async_disconnect(mark_liveness=True)
   self.assertTrue(observed)
   self.assertTrue(all(status in ('connected','recently_connected') and source == 'test' and available for status,source,available in observed),observed)
   self.assertEqual(c.last_seen,seen)
   c.async_start_availability_tracking();await asyncio.sleep(.01)
   now[0]=109.99;c._availability_wakeup.set();await asyncio.sleep(.01)
   self.assertTrue(c.available);self.assertEqual(c.bluetooth_status,'recently_connected')
   now[0]=110.01;c._availability_wakeup.set();await asyncio.sleep(.01)
   self.assertFalse(c.available);self.assertEqual(c.bluetooth_status,'unreachable');await c.async_close()


 async def test_already_lost_connection_does_not_receive_idle_grace(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c.availability_timeout=10;c._latest_service_info=lambda:None;c._availability.connection_time=80.0
   class Backend:
    is_connected=False
    async def disconnect(self):pass
   c._client=Backend();observed=[]
   c.async_add_diagnostic_listener(lambda:observed.append(c.bluetooth_status))
   await c._async_disconnect(mark_liveness=True)
   self.assertEqual(observed,['unreachable'])
   self.assertEqual(c._availability.connection_time,80.0);await c.async_close()

 async def test_old_cleanup_does_not_add_grace_to_replacement_client(self):
  now=[100.0]
  with patch.object(cm,'time',types.SimpleNamespace(monotonic=lambda:now[0])):
   c=self.make_client();c._latest_service_info=lambda:None
   replacement=types.SimpleNamespace(is_connected=True)
   class Backend:
    is_connected=True
    async def disconnect(self):
     self.is_connected=False;c._client=replacement
     now[0]=102;c._availability.connection_time=102
   c._client=Backend();observed=[]
   c.async_add_diagnostic_listener(lambda:observed.append(c.bluetooth_status))
   await c._async_disconnect(mark_liveness=True)
   self.assertIs(c._client,replacement);self.assertEqual(observed,['connected'])
   self.assertEqual(c._availability.connection_time,102)
   c._client=None;await c.async_close()

if __name__=='__main__':unittest.main(verbosity=2)
