"""Listener registration lifecycle with production notification methods."""
import asyncio
import unittest
import test_status_confirmation as fx
from awox112 import client as cm

class ListenerRegistration(unittest.TestCase):
    kinds=('diagnostic','availability','advertisement')

    def client(self):
        c=cm.AwoxConnectZClient(fx.Hass(),fx.MAC,'mesh','password',
            default_transition=.2,idle_disconnect=10,
            command_semaphore=asyncio.Semaphore(1),max_concurrent_commands=1,
            configured_mesh_id=fx.MID)
        c._latest_service_info=lambda:None
        return c

    def notify(self,c,kind):
        if kind=='diagnostic':c._async_notify_diagnostic_listeners();return ()
        if kind=='availability':
            value=not c._available;c._async_set_available(value);return (value,)
        marker=object();c.async_set_advertisement_state(marker);return (marker,)

    def test_arguments_registration_and_repeat_removal(self):
        for kind in self.kinds:
            c=self.client();events=[]
            listener=lambda *args:events.append(args)
            remove=getattr(c,'async_add_'+kind+'_listener')(listener)
            self.assertEqual(events,[])
            expected=self.notify(c,kind)
            self.assertEqual(events,[expected])
            remove();remove()
            self.notify(c,kind)
            self.assertEqual(events,[expected])

    def test_duplicate_registration_remains_set_based(self):
        for kind in self.kinds:
            c=self.client();events=[]
            listener=lambda *args:events.append(args)
            add=getattr(c,'async_add_'+kind+'_listener')
            remove1=add(listener);remove2=add(listener)
            self.notify(c,kind);self.assertEqual(len(events),1)
            remove1();self.notify(c,kind);self.assertEqual(len(events),1)
            remove2()

    def test_self_removal_during_notification_keeps_other_listener(self):
        for kind in self.kinds:
            c=self.client();events=[];add=getattr(c,'async_add_'+kind+'_listener')
            def first(*args):
                events.append('self');remove()
            remove=add(first);add(lambda *args:events.append('other'))
            self.notify(c,kind)
            self.assertCountEqual(events,['self','other'])
            events.clear();self.notify(c,kind)
            self.assertEqual(events,['other'])

    def test_error_policy_remains_specific_to_notification_kind(self):
        for kind in self.kinds:
            c=self.client()
            def bad(*args):raise ValueError('listener failure')
            getattr(c,'async_add_'+kind+'_listener')(bad)
            if kind=='advertisement':
                with self.assertRaisesRegex(ValueError,'listener failure'):self.notify(c,kind)
            else:
                with self.assertLogs(cm._LOGGER,level='ERROR'):self.notify(c,kind)

if __name__=='__main__':unittest.main(verbosity=2)
