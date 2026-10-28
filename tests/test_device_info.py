"""Registry metadata parity for real light and sensor constructors with HA stubs."""
import unittest
import test_optimization as opt
from awox112.light import AwoxConnectZLight
from awox112.sensor import AwoxConnectZDiagnosticSensor, SENSOR_DESCRIPTIONS
from awox112.const import DOMAIN

class DeviceInfoParity(unittest.TestCase):
    def test_light_and_all_sensors_preserve_metadata_and_defaults(self):
        for fields in ({}, {'name':None,'model':'','manufacturer':None},
                {'name':'Desk','model':'RGB','manufacturer':'EGLO','firmware':'1.2','hardware':'A'},
                {'name':42,'model':23,'manufacturer':17,'firmware':0,'hardware':''}):
            client=opt.fx.FakeClient(None)
            device=dict(fields,mesh_id=opt.fx.MID)
            expected=dict(identifiers={(DOMAIN,client.mac)},
                name=str(fields.get('name') or f'AwoX {client.mac[-8:]}'),
                manufacturer=str(fields.get('manufacturer') or 'AwoX / EGLO'),
                model=str(fields.get('model') or 'Connect.Z RGB/TW'),
                sw_version=fields.get('firmware'),hw_version=fields.get('hardware'),
                connections={('bluetooth',client.mac)})
            light=AwoxConnectZLight(client,device,minimum_command_interval_ms=250)
            self.assertEqual(light._attr_device_info,expected)
            for description in SENSOR_DESCRIPTIONS:
                sensor=AwoxConnectZDiagnosticSensor(client,device,description)
                self.assertEqual(sensor._attr_device_info,expected)
                self.assertIsNot(light._attr_device_info,sensor._attr_device_info)

if __name__=='__main__':unittest.main(verbosity=2)

