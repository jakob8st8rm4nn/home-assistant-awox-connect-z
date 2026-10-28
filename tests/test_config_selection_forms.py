"""Selection form regressions; HA selectors are simulated, not real HA validation."""
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import test_optimization as opt
cf = opt.config_flow

class DeviceSelectionForms(unittest.IsolatedAsyncioTestCase):
    def flow(self, count=2):
        f=cf.AwoxConnectZConfigFlow()
        f._local_mesh_name='mesh'; f._local_mesh_password='password'
        f._local_scan_done=True
        f._configured_entries=lambda:[]
        f._local_candidates={a:{'name':a,'mesh_id':i+1} for i,a in enumerate(['BB','AA'][:count])}
        f._local_selected_addresses={'BB','AA','STALE'}
        f._get_reconfigure_entry=lambda:types.SimpleNamespace(data={cf.CONF_DEVICES:[]})
        return f

    async def show(self,f,reconfigure=False,input=None):
        step=f.async_step_reconfigure_local_devices if reconfigure else f.async_step_local_devices
        # Retain defaults/options in the fixture rather than discarding them.
        with patch.object(cf.vol,'Optional',lambda key,**kw:(key,repr(kw))), patch.object(cf,'SelectSelector',lambda config:config):
            return await step(input)

    async def test_fields_defaults_order_and_step_ids(self):
        for reconfigure in (False,True):
            for count in (0,1,2):
                with self.subTest(reconfigure=reconfigure,count=count):
                    f=self.flow(count)
                    result=await self.show(f,reconfigure)
                    prefix='reconfigure_local_devices' if reconfigure else 'local_devices'
                    self.assertEqual(result['step_id'],prefix if count else prefix+'_empty')
                    fields=result['data_schema']
                    self.assertEqual([k[0] for k in fields],([cf.CONF_DEVICES] if count else [])+['rescan','manual_add'])
                    if count:
                        key=next(iter(fields))
                        self.assertEqual(key[1],repr({'default':sorted(f._local_candidates)}))
                        self.assertEqual(fields[key]['multiple'],True)
                        self.assertEqual([o['value'] for o in fields[key]['options']],sorted(f._local_candidates))
                    self.assertEqual(result['description_placeholders']['count'],str(count))
                    self.assertEqual('existing_count' in result['description_placeholders'],reconfigure)

    async def test_empty_selection_keeps_different_completion_rules(self):
        f=self.flow(0)
        result=await self.show(f,input={})
        self.assertEqual(result['errors'],{'base':'no_selection'})
        f=self.flow(0)
        f._async_abort_bluetooth_discovery_flows=Mock()
        f.async_update_reload_and_abort=Mock(return_value={'type':'saved'})
        result=await self.show(f,True,{})
        self.assertEqual(result,{'type':'saved'})
        f.async_update_reload_and_abort.assert_called_once()

    async def test_rescan_uses_correct_entry_scope(self):
        for reconfigure in (False,True):
            f=self.flow()
            f._async_refresh_local_selection=AsyncMock()
            result=await self.show(f,reconfigure,{'rescan':True,cf.CONF_DEVICES:['AA']})
            f._async_refresh_local_selection.assert_awaited_once()
            self.assertEqual(bool(f._async_refresh_local_selection.call_args.kwargs),reconfigure)
            self.assertEqual(f._local_selected_addresses,{'AA'})
            self.assertEqual(result['errors'],{})

    async def test_manual_add_routes_without_verifying_selection(self):
        for reconfigure in (False,True):
            f=self.flow()
            f.async_step_local_manual=AsyncMock(return_value={'type':'manual'})
            f._async_verify_local_selection=AsyncMock()
            result=await self.show(f,reconfigure,{'manual_add':True})
            self.assertEqual(result,{'type':'manual'})
            f._async_verify_local_selection.assert_not_awaited()

class SelectionPreparation(DeviceSelectionForms):
    # Reuse fixture utilities without rerunning the inherited test methods.
    test_fields_defaults_order_and_step_ids = None
    test_empty_selection_keeps_different_completion_rules = None
    test_rescan_uses_correct_entry_scope = None
    test_manual_add_routes_without_verifying_selection = None

    async def test_selection_normalization_and_failure_details(self):
        for reconfigure in (False,True):
            for selection,expected in [('aa',{'AA'}),(['bb','aa','BB','missing'],{'AA','BB'}),(None,set()),([],set())]:
                f=self.flow()
                f._async_verify_local_selection=AsyncMock(return_value=([], 'device_unavailable', 'Lamp AA'))
                f.async_step_local_manual=AsyncMock(return_value={'type':'manual'})
                result=await self.show(f,reconfigure,{cf.CONF_DEVICES:selection,'manual_add':True})
                self.assertEqual(f._local_selected_addresses,expected)
                self.assertEqual(result,{'type':'manual'})

    async def test_start_reset_preserves_credential_cache_rules(self):
        for reconfigure in (False,True):
            for changed in (False,True):
                f=self.flow()
                f._local_verified_addresses={'AA'}
                f._local_mesh_id_conflicts={'AA':(1,2)}
                f._local_observed_mesh_ids={'AA':2}
                f._local_scan_error=True;f._local_failed_device='old'
                f._local_conflict_entered_id='sticky'
                entry=types.SimpleNamespace(data={cf.CONF_DEVICES:[{'mac':'AA'}]})
                f._get_reconfigure_entry=lambda:entry
                f._async_verify_credentials=AsyncMock(return_value=None)
                destination=AsyncMock(return_value={'type':'next'})
                if reconfigure:f.async_step_reconfigure_local_devices=destination
                else:f.async_step_local_devices=destination
                step=f.async_step_reconfigure_local if reconfigure else f.async_step_local
                result=await step({cf.CONF_MESH_NAME:'new' if changed else 'mesh',cf.CONF_MESH_PASSWORD:'password'})
                self.assertEqual(result,{'type':'next'})
                self.assertEqual(f._local_candidates,{})
                self.assertEqual(f._local_selected_addresses,set())
                self.assertEqual(f._local_mesh_id_conflicts,{})
                self.assertEqual(f._local_observed_mesh_ids,{})
                self.assertFalse(f._local_scan_error);self.assertFalse(f._local_scan_done)
                self.assertEqual(f._local_reconfigure_mode,reconfigure)
                self.assertEqual(f._local_failed_device,'')
                self.assertEqual(f._local_verified_addresses,set() if changed else {'AA'})
                self.assertEqual(f._local_conflict_entered_id,'sticky')

    async def test_verification_error_placeholders_and_scan_failure(self):
        for reconfigure in (False,True):
            f=self.flow()
            async def verify(*args,**kwargs):
                f._local_conflict_entered_id='0x0001'
                f._local_conflict_advertised_id='0x0002'
                f._local_unavailable_devices='Lamp B'
                f._local_rejected_devices='Lamp C'
                return [],'mesh_id_conflict','Lamp A'
            f._async_verify_local_selection=AsyncMock(side_effect=verify)
            result=await self.show(f,reconfigure,{cf.CONF_DEVICES:['aa']})
            self.assertEqual(result['errors'],{'base':'mesh_id_conflict'})
            details=result['description_placeholders']
            self.assertEqual(details['failed_device'],'Lamp A')
            self.assertEqual(details['entered_mesh_id'],'0x0001')
            self.assertEqual(details['advertised_mesh_id'],'0x0002')
            self.assertEqual(details['unavailable_devices'],'Lamp B')
            self.assertEqual(details['rejected_devices'],'Lamp C')
            f._local_scan_error=True
            result=await self.show(f,reconfigure)
            self.assertEqual(result['errors'],{'base':'active_scan_failed'})
            self.assertEqual(result['description_placeholders']['failed_device'],'')

if __name__=='__main__':
    unittest.main(verbosity=2)

