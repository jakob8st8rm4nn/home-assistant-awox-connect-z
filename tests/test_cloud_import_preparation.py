"""Cloud setup and reconfigure keep their own ownership and completion rules."""
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import test_optimization as opt
from awox112.cloud import AwoxAccountImport
cf=opt.config_flow

class CloudImportPreparation(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        flow=cf.AwoxConnectZConfigFlow();flow.hass=object()
        flow.async_set_unique_id=AsyncMock()
        flow._abort_if_unique_id_configured=Mock()
        flow._abort_if_unique_id_mismatch=Mock()
        entry=types.SimpleNamespace(data={cf.CONF_SETUP_METHOD:cf.SETUP_METHOD_CLOUD})
        flow._get_reconfigure_entry=lambda:entry
        imported=AwoxAccountImport('owner','mesh','mesh-secret',[{'mac':'AA','mesh_id':1}],1)
        return flow,imported

    async def test_review_retains_identical_data_without_login_password(self):
        for reconfigure in (False,True):
            f,imported=self.fixture()
            f._cloud_review_devices=Mock(return_value=(imported.devices,[{'mac':'BB'}]))
            f.async_step_cloud_review=AsyncMock(return_value={'type':'review'})
            f.async_step_reconfigure_cloud_review=AsyncMock(return_value={'type':'review'})
            step=f.async_step_reconfigure if reconfigure else f.async_step_cloud
            with patch.object(cf,'async_import_account',AsyncMock(return_value=imported)):
                result=await step({cf.CONF_EMAIL:' User@EXAMPLE.org ',cf.CONF_PASSWORD:'login-secret'})
            self.assertEqual(result,{'type':'review'})
            self.assertEqual(f._pending_cloud_data,{
                cf.CONF_SETUP_METHOD:cf.SETUP_METHOD_CLOUD,cf.CONF_EMAIL:'user@example.org',
                cf.CONF_OWNER_ID:'owner',cf.CONF_MESH_NAME:'mesh',cf.CONF_MESH_PASSWORD:'mesh-secret',
                cf.CONF_DEVICES:imported.devices})
            self.assertIsNot(f._pending_cloud_data[cf.CONF_DEVICES],imported.devices)
            self.assertEqual(f._pending_cloud_review_counts,(1,1))
            f.async_set_unique_id.assert_awaited_once_with('awox-cloud-owner')
            self.assertEqual(f._abort_if_unique_id_mismatch.call_count,int(reconfigure))
            self.assertEqual(f._abort_if_unique_id_configured.call_count,int(not reconfigure))

    async def test_no_conflict_keeps_separate_save_actions(self):
        for reconfigure in (False,True):
            f,imported=self.fixture()
            f._cloud_review_devices=Mock(return_value=(imported.devices,[]))
            f.async_create_entry=Mock(return_value={'type':'create'})
            f.async_update_reload_and_abort=Mock(return_value={'type':'update'})
            step=f.async_step_reconfigure if reconfigure else f.async_step_cloud
            with patch.object(cf,'async_import_account',AsyncMock(return_value=imported)):
                result=await step({cf.CONF_EMAIL:'user@example.org',cf.CONF_PASSWORD:'login-secret'})
            self.assertEqual(result,{'type':'update' if reconfigure else 'create'})
            self.assertIsNone(f._pending_cloud_data)
            self.assertIsNone(f._pending_cloud_review_counts)
            method=f.async_update_reload_and_abort if reconfigure else f.async_create_entry
            data=method.call_args.kwargs['data_updates' if reconfigure else 'data']
            self.assertEqual(data[cf.CONF_DEVICES],imported.devices)
            self.assertEqual(data[cf.CONF_MESH_PASSWORD],'mesh-secret')
            self.assertNotIn(cf.CONF_PASSWORD,data)

if __name__=='__main__':unittest.main(verbosity=2)
