"""Lookup ordering, classification and local ownership boundaries."""
import asyncio,itertools,types,unittest
from unittest.mock import AsyncMock,Mock,patch
import test_optimization as opt
from awox112 import client as cm
cf=opt.config_flow

class StructureCleanup(unittest.IsolatedAsyncioTestCase):
    def client(self):
        return cm.AwoxConnectZClient(opt.fx.Hass(),opt.fx.MAC,'mesh','password',default_transition=.2,
            idle_disconnect=10,command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,
            configured_mesh_id=opt.fx.MID)

    async def test_lookup_order_and_runtime_scanner_fallback(self):
        for answers,expected_calls,scanner_needed in [([object()],[True],False),([None,object()],[True,False],False),([None,None],[True,False],True)]:
            c=self.client();scan_device=object()
            with patch.object(cm,'async_ble_device_from_address',side_effect=answers) as lookup,patch.object(cm.bluetooth,'async_scanner_devices_by_address',return_value=[types.SimpleNamespace(ble_device=scan_device)],create=True) as scan:
                result=c._async_find_device_runtime()
                self.assertIs(result,scan_device if scanner_needed else answers[-1])
                self.assertEqual([call.kwargs['connectable'] for call in lookup.call_args_list],expected_calls)
                self.assertEqual(scan.call_count,int(scanner_needed))
        c=self.client()
        with patch.object(cm,'async_ble_device_from_address',return_value=None),patch.object(cm.bluetooth,'async_scanner_devices_by_address',return_value=[],create=True):
            with self.assertRaisesRegex(cm.AwoxDeviceNotFound,'no current Bluetooth device entry'):c._async_find_device_runtime()

    async def test_setup_retries_spacing_and_cancellation(self):
        c=self.client();attempts=cm.DISCOVERY_ATTEMPTS
        with patch.object(cm,'async_ble_device_from_address',return_value=None) as lookup,patch.object(cm.asyncio,'sleep',new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(cm.AwoxDeviceNotFound,'not currently visible'):await c._async_find_device()
            self.assertEqual([call.kwargs['connectable'] for call in lookup.call_args_list],[True,False]*attempts)
            self.assertEqual([call.args[0] for call in sleep.call_args_list],[min(cm.DISCOVERY_RETRY_BASE_DELAY_SECONDS*i,cm.DISCOVERY_RETRY_MAX_DELAY_SECONDS) for i in range(1,attempts)])
        with patch.object(cm,'async_ble_device_from_address',return_value=None),patch.object(cm.asyncio,'sleep',new_callable=AsyncMock,side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):await c._async_find_device()

    async def test_cache_message_matrix_keeps_hits_and_nonhits(self):
        for noun,missing,tail in itertools.product(['service','characteristic','device',''],['not found','could not be found','does not exist','unavailable',''],['',' INVALID HANDLE',' ATTRIBUTE NOT FOUND']):
            message=f'{noun} {missing}{tail}'
            expected=(noun in ('service','characteristic') and missing in ('not found','could not be found','does not exist')) or bool(tail)
            self.assertEqual(cm.AwoxConnectZClient._is_cache_service_error(RuntimeError(message)),expected,message)
        for name in ('BleakCharacteristicNotFoundError','BleakServiceNotFoundError'):
            self.assertTrue(cm.AwoxConnectZClient._is_cache_service_error(type(name,(Exception,),{})('unrelated')))

    async def test_record_addresses_keep_merge_and_exclusion_semantics(self):
        f=cf.AwoxConnectZConfigFlow();first=types.SimpleNamespace(entry_id='first',data={cf.CONF_DEVICES:[{'mac':'aa'},{'mac':None},{'mac':''}]});second=types.SimpleNamespace(entry_id='second',data={cf.CONF_DEVICES:[{'mac':'bb'}]})
        f._configured_entries=lambda:[first,second]
        self.assertEqual(f._configured_addresses(),{'AA','BB'})
        self.assertEqual(f._configured_addresses(exclude_entry=first),{'BB'})
        self.assertTrue(f._entry_contains_address(first,'AA'));self.assertFalse(f._entry_contains_address(first,'BB'))
        self.assertEqual(f._merge_devices([{'mac':'aa','name':'old'},{'mac':None}],[{'mac':'AA','name':'new'},{'mac':'bb'},{'mac':''}]),[{'mac':'AA','name':'new'},{'mac':'bb'}])

    async def test_both_final_conflict_checks_keep_abort_label(self):
        for conflicts in (['AA'],[None,'AA']):
            f=cf.AwoxConnectZConfigFlow();f._local_mesh_name='mesh';f._local_mesh_password='password'
            f._local_candidates={'AA':{'name':'Desk','mesh_id':1}}
            f._matching_local_mesh_entry=Mock(return_value=None)
            f.async_set_unique_id=AsyncMock();f._abort_if_unique_id_configured=Mock()
            f._first_device_conflict=Mock(side_effect=conflicts)
            result=await f._async_create_local_entry([{'mac':'AA','mesh_id':1}])
            self.assertEqual(result,{'type':'abort','reason':'device_configured_during_setup','description_placeholders':{'device':'Desk (AA)'}})
            self.assertEqual(f._first_device_conflict.call_count,len(conflicts))

if __name__=='__main__':unittest.main(verbosity=2)

