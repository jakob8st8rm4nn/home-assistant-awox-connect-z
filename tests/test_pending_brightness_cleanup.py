"""Merge ownership and brightness boundary regressions."""
import unittest
from dataclasses import asdict
import test_status_confirmation as fx
from awox112.command import manager as manager
from awox112 import protocol

class PendingBrightnessCleanup(unittest.TestCase):
    def test_merge_retains_independent_request_versions(self):
        initial=manager._PendingVersions(power=1,brightness=2,appearance=3,effect=4)
        targets=(fx.T(),fx.T(power=False),fx.T(power=True),fx.T(brightness=80),fx.T(color=(210,70)),fx.T(cct=2700),fx.T(effect='Candle'),fx.T(stop_effect=True))
        for target in targets:
            with self.subTest(target=target):
                before=asdict(initial)
                _,merged=manager.CommandManager._merge_pending(fx.T(),initial,target,10)
                self.assertIsNot(merged,initial)
                self.assertEqual(asdict(initial),before)
                merged.power=999
                self.assertEqual(asdict(initial),before)
        _,unchanged=manager.CommandManager._merge_pending(fx.T(),initial,fx.T(),10)
        self.assertEqual(asdict(unchanged),asdict(initial))

    def test_brightness_boundaries_and_conversion_errors(self):
        for value in (-1000,-1,0,1,2,254,255,256,1000,.9,1.9,254.9,'255',True,False):
            with self.subTest(value=value):
                expected=max(1,min(255,int(value)))
                self.assertEqual(protocol.ha_brightness_to_device(value),expected)
                self.assertEqual(protocol.make_brightness(value,.2,mesh_id=fx.MID)[8],expected)
        for value,error in ((None,TypeError),('invalid',ValueError),(float('nan'),ValueError),(float('inf'),OverflowError)):
            with self.subTest(value=value):
                with self.assertRaises(error):protocol.make_brightness(value,.2,mesh_id=fx.MID)

if __name__=='__main__':unittest.main(verbosity=2)
