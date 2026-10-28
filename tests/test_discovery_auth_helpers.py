"""Discovery history reset boundaries and authentication protocol regressions."""
import asyncio,types,unittest
from unittest.mock import AsyncMock,Mock,patch
import test_optimization as opt
from awox112 import protocol
cf=opt.config_flow

class DiscoveryAuthHelpers(unittest.IsolatedAsyncioTestCase):
    def flow(self,entries):
        f=cf.AwoxConnectZConfigFlow();f.hass=object()
        f.async_set_unique_id=AsyncMock();f._abort_if_unique_id_configured=Mock()
        f._configured_entries=lambda:entries
        f._async_full_advertisement=AsyncMock(side_effect=TimeoutError)
        return f

    async def test_optional_history_api_at_both_abort_points(self):
        for has_entries in (False,True):
            for api_present in (False,True):
                f=self.flow([object()] if has_entries else [])
                f._entry_contains_address=Mock(return_value=False)
                clear=Mock()
                with patch.object(cf.bluetooth,'async_clear_address_from_match_history',clear if api_present else None,create=True):
                    result=await f.async_step_bluetooth(types.SimpleNamespace(address='aa:bb:cc:dd:ee:ff'))
                self.assertEqual(result['reason'],'incomplete_advertisement' if has_entries else 'account_required')
                if api_present:clear.assert_called_once_with(f.hass,'AA:BB:CC:DD:EE:FF')
                else:clear.assert_not_called()
                self.assertEqual(f._async_full_advertisement.await_count,int(has_entries))

    async def test_already_configured_and_cancellation_do_not_clear_history(self):
        for cancelled in (False,True):
            f=self.flow([object()]);f._entry_contains_address=Mock(return_value=not cancelled)
            f._async_full_advertisement=AsyncMock(side_effect=asyncio.CancelledError)
            with patch.object(cf.bluetooth,'async_clear_address_from_match_history',Mock(),create=True) as clear:
                if cancelled:
                    with self.assertRaises(asyncio.CancelledError):await f.async_step_bluetooth(types.SimpleNamespace(address='AA'))
                else:self.assertEqual((await f.async_step_bluetooth(types.SimpleNamespace(address='AA')))['reason'],'device_already_configured')
                clear.assert_not_called()

    async def test_auth_credentials_and_random_boundaries(self):
        for name,password in ((b'',b''),(b'mesh',b'password'),(bytes(range(16)),bytes(range(15,-1,-1)))):
            login=protocol.make_pair_packet(name,password,b'12345678')
            key=protocol.make_session_key(name,password,b'12345678',b'ABCDEFGH')
            self.assertEqual(len(login),17);self.assertEqual(login[:9],b'\x0c12345678')
            self.assertEqual(len(key),16)
        for name,password in ((b'x'*17,b'ok'),(b'ok',b'x'*17)):
            for fn,args in ((protocol.make_pair_packet,(name,password,b'12345678')),(protocol.make_session_key,(name,password,b'12345678',b'ABCDEFGH'))):
                with self.assertRaisesRegex(ValueError,'credential exceeds 16 bytes'):fn(*args)

if __name__=='__main__':unittest.main(verbosity=2)
