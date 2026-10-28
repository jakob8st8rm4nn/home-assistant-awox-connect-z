"""Behavioral regressions for in-file simplifications, also runnable on Paket A."""
import asyncio
import hashlib
import itertools
import json
import sys
import unittest
from dataclasses import asdict
from unittest.mock import patch
import test_optimization as opt
from awox112.command import manager as mm
from awox112 import protocol, cloud

fx=opt.fx


def merge_cases():
    targets=(fx.T(),fx.T(power=True),fx.T(power=False),fx.T(brightness=80),fx.T(color=(120,70)),fx.T(cct=4000),fx.T(effect='Candle'),fx.T(stop_effect=True))
    result=[]
    for current,incoming,versions,generation in itertools.product(targets,targets,((None,None,None,None),(1,2,3,4),(4,4,4,4)),(5,10)):
        before=mm._PendingVersions(*versions)
        target,after=mm.CommandManager._merge_pending(current,before,incoming,generation)
        result.append((asdict(target),asdict(after),asdict(before)))
    return result


def cct_frames():
    digest=hashlib.sha256();count=0
    values=list(range(2600,6601))+[0,-1000,1e9,2700.9,6499.9,'4000',b'6500',True,False,None,'invalid',float('nan'),float('inf')]
    for value,transition in itertools.product(values,(0,.04,.19,.2,30)):
        try:output=('frame',protocol.make_color_temp_kelvin(value,transition,mesh_id=fx.MID).hex())
        except Exception as error:output=('error',type(error).__name__,str(error))
        digest.update(json.dumps(output,separators=(',',':')).encode());count+=1
    return count,digest.hexdigest()


async def cloud_trace(scenario):
    events=[]
    responses={
        'first_success':('success',),
        'network_success':('network','success'),
        'timeout_success':('timeout','success'),
        'network_network':('network','network'),
        'credential_network':('credential','network'),
        'network_credential':('network','credential'),
        'generic_network':('generic','network'),
        'credential_success':('credential','success'),
        'cancel':('cancel',),
    }[scenario]
    async def post(hass,url,**kwargs):
        outcome=responses[len([e for e in events if e[0]=='post'])]
        events.append(('post',url,kwargs['payload']))
        if outcome=='network':raise cloud.AwoxCloudError('offline')
        if outcome=='timeout':raise TimeoutError('timeout')
        if outcome=='cancel':raise asyncio.CancelledError()
        if outcome=='credential':return 400,{'code':101,'error':'invalid login'}
        if outcome=='generic':return 503,{'error':'temporarily unavailable'}
        return 200,{'objectId':'owner','sessionToken':'token'}
    async def fetch(*args,**kwargs):
        events.append(('fetch',args[1],args[5],kwargs['owner_filtered']))
        if args[5]=='Credential':return [{'service':'zigbee','client_id':'mesh','access_token':'mesh-secret'}]
        return [{'objectId':'lamp','type':'.ble.light','macAddress':fx.MAC,'address':fx.MID}]
    with patch.object(cloud,'PARSE_URLS',('first/','second/')),patch.object(cloud,'_post_json',post),patch.object(cloud,'_fetch_class',fetch):
        try:
            result=await cloud.async_import_account(None,' User@Example.org ','login-secret')
            events.append(('result',asdict(result)))
        except BaseException as error:events.append(('error',type(error).__name__,str(error)))
    return events


async def request_trace(target):
    client=fx.FakeClient(None)
    light=fx.AwoxConnectZLight(client,{'mesh_id':fx.MID},minimum_command_interval_ms=0)
    light.hass=fx.Hass();client.manager=light._manager
    light._manager.set_state(fx.S(True,True));light._attr_is_on=True
    try:
        await light._async_submit_target(target)
        return {'controls':[c.value for c in client.controls],'generation':light._manager.request_generation,'pending':sorted(light._manager.pending_generations),'waiters':len(light._request_waiters),'state':asdict(light._manager.state)}
    except Exception as error:return {'error':type(error).__name__,'message':str(error)}


class SimplificationTests(unittest.IsolatedAsyncioTestCase):
    def test_cct_golden_frames_and_conversion_errors(self):
        for value,expected in ((2600,'780a9cdd0100030a7201020000000000'),(2700,'780a9cdd0100030a7201020000000000'),(4000,'1a0a9cdd0100030afa00020000000000'),('4000','1a0a9cdd0100030afa00020000000000'),(6500,'7e0a9cdd0100030a9a00020000000000'),(6600,'7e0a9cdd0100030a9a00020000000000')):
            self.assertEqual(protocol.make_color_temp_kelvin(value,.2,mesh_id=fx.MID).hex(),expected)
        for value,error in ((None,TypeError),('invalid',ValueError),(float('nan'),ValueError),(float('inf'),OverflowError)):
            with self.assertRaises(error):protocol.make_color_temp_kelvin(value,.2,mesh_id=fx.MID)

    def test_on_obligation_and_generation_survive_pending_merge(self):
        for incoming in (fx.T(power=True),fx.T(brightness=80),fx.T(color=(120,70)),fx.T(cct=4000),fx.T(effect='Candle')):
            versions=mm._PendingVersions(power=1)
            target,updated=mm.CommandManager._merge_pending(fx.T(power=False),versions,incoming,5)
            self.assertIs(target.power,True)
            self.assertEqual(updated.power,5)
            self.assertEqual(versions.power,1)
            self.assertIsNot(versions,updated)

    def test_pruning_retains_running_retry_and_discards_stale_attempt(self):
        manager=mm.CommandManager(fx.S(True,True),minimum_interval_ms=0)
        manager.submit(fx.T(brightness=80));manager.begin_group();manager.take_next_command()
        key=manager._in_flight_attempt_key
        manager.submit(fx.T(color=(120,70)))
        manager._write_attempts[('stale',0)]=99
        manager._prune_write_attempts()
        self.assertEqual(manager._write_attempts[key],1)
        self.assertNotIn(('stale',0),manager._write_attempts)
        self.assertIn(key[1],manager.pending_generations)

    def test_each_successful_submit_advances_generation_even_for_noop_or_frozen_group(self):
        for on,frozen,target in itertools.product((False,True),(False,True),(fx.T(),fx.T(power=True),fx.T(power=False),fx.T(brightness=80),fx.T(color=(120,70)),fx.T(effect='Candle'),fx.T(stop_effect=True))):
            manager=mm.CommandManager(fx.S(True,on),minimum_interval_ms=0)
            if frozen:manager.begin_group()
            before=manager.request_generation
            manager.submit(target)
            self.assertEqual(manager.request_generation,before+1)

    async def test_cloud_endpoint_order_auth_priority_and_cancel(self):
        for scenario,expected in (('first_success',None),('network_success',None),('timeout_success',None),('network_network','AwoxCloudError'),('credential_network','AwoxInvalidAuth'),('network_credential','AwoxInvalidAuth'),('generic_network','AwoxCloudError'),('credential_success',None),('cancel','CancelledError')):
            events=await cloud_trace(scenario)
            self.assertEqual([e[1] for e in events if e[0]=='post'],['first/login'] if scenario in ('first_success','cancel') else ['first/login','second/login'])
            if expected:self.assertEqual(events[-1][1],expected)
            else:
                self.assertEqual(events[-1][0],'result')
                self.assertEqual(events[-1][1]['raw_device_count'],1)
                self.assertEqual(events[-1][1]['devices'][0]['mesh_id'],fx.MID)

    async def test_ha_noop_and_written_requests_complete_without_lost_waiters(self):
        for target in (fx.T(power=True),fx.T(power=False),fx.T(brightness=80),fx.T(color=(120,70))):
            result=await request_trace(target)
            self.assertNotIn('error',result)
            self.assertEqual(result['generation'],1)
            self.assertEqual(result['pending'],[])
            self.assertEqual(result['waiters'],0)
        self.assertEqual((await request_trace(fx.T(power=True)))['controls'],[])


if __name__=='__main__':
    if '--compare' in sys.argv:
        async def compare():
            output={'merges':merge_cases(),'cct':cct_frames(),'cloud':{},'requests':[]}
            for scenario in ('first_success','network_success','timeout_success','network_network','credential_network','network_credential','generic_network','credential_success','cancel'):output['cloud'][scenario]=await cloud_trace(scenario)
            for target in (fx.T(power=True),fx.T(power=False),fx.T(brightness=80),fx.T(color=(120,70)),fx.T(cct=4000),fx.T(effect='Candle'),fx.T(brightness=-1)):
                output['requests'].append(await request_trace(target))
            print(json.dumps(output,sort_keys=True))
        asyncio.run(compare())
    else:unittest.main()
