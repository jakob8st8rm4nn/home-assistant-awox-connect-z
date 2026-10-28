"""Behavior contracts for the final local cleanup; also runnable on its predecessor."""
import itertools
import json
import sys
import unittest
from dataclasses import asdict

if "--source" in sys.argv:
    index = sys.argv.index("--source")
    sys.path.insert(0, sys.argv[index + 1])
    del sys.argv[index:index + 2]
import test_optimization as opt
from awox112.command import sequences as seq
from awox112.command.manager import CommandManager, _PendingVersions
cf = opt.config_flow


def state_trace():
    results = []
    for known, on, effect, kind, value in itertools.product(
        (False, True), (None, False, True), (None, "Candle", "Color cycle"),
        list(seq.CommandKind), (None, "Candle", "Color cycle", 99),
    ):
        state = seq.PlannerState(known, on, effect)
        results.append(asdict(CommandManager._state_after_success(state, seq.PlannedCommand(kind, value))))
    return results


def generation_trace():
    results = []
    for values, default in itertools.product(itertools.product((None, 0, 1, 3, 100), repeat=4), (-1, 0, 5)):
        manager = CommandManager(seq.PlannerState(True, False, "Candle"), minimum_interval_ms=0)
        manager._pending_versions = _PendingVersions(*values)
        manager._request_generation = default
        actual = manager._command_generation(seq.PlannedCommand(seq.CommandKind.EFFECT_STOP, "Candle"), is_finalizer=False)
        active = [v for v in values if v is not None]
        assert actual == (max(active) if active else default)
        results.append(actual)
    return results


def selection_trace():
    results = []
    for selection in (None, [], "aa", ["aa", "BB", "Aa", "missing"], [1, "1", None, False]):
        flow = cf.AwoxConnectZConfigFlow()
        flow._local_candidates = {"AA": {}, "BB": {}, "1": {}}
        flow._set_local_selection(selection)
        results.append(sorted(flow._local_selected_addresses))
    assert results == [[], [], ["AA"], ["AA", "BB"], ["1"]]
    return results


def cloud_trace():
    rows = [{"objectId": "a", "name": "first"}, {"objectId": "a", "name": "second"},
            {"objectId": "b"}, {"name": "no id", "details": {"x": 1}}]
    results = []
    for primary, secondary in itertools.product(([], rows[:2], rows[2:], rows), repeat=2):
        before = json.dumps((primary, secondary), sort_keys=True)
        merged = opt.cloud._merge_rows(primary, secondary)
        assert json.dumps((primary, secondary), sort_keys=True) == before
        seen = set(); expected = []
        for row in [*primary, *secondary]:
            key = "id:" + str(row["objectId"]) if row.get("objectId") else repr(sorted(row.items(), key=lambda item: item[0]))
            if key not in seen:
                seen.add(key); expected.append(row)
        assert merged == expected
        assert all(any(row is original for original in primary + secondary) for row in merged)
        results.append(merged)
    return results


class FinalCleanupTests(unittest.TestCase):
    def test_successful_state_derivation_keeps_effect_semantics(self):
        self.assertEqual(len(state_trace()), 504)
        for kind in (seq.CommandKind.POWER_ON, seq.CommandKind.BRIGHTNESS_ON):
            actual = CommandManager._state_after_success(seq.PlannerState(True, False, "Candle"), seq.PlannedCommand(kind, 80))
            self.assertEqual(actual, seq.PlannerState(True, True, "Candle"))
        self.assertEqual(CommandManager._state_after_success(seq.PlannerState(True, True, "Candle"), seq.PlannedCommand(seq.CommandKind.POWER_OFF)), seq.PlannerState(True, False))

    def test_recovery_owner_maximum_falls_back_only_for_empty_slots(self):
        self.assertEqual(len(generation_trace()), 1875)

    def test_selection_accepts_single_or_multiple_addresses(self):
        selection_trace()

    def test_cloud_merge_preserves_precedence_order_identity_and_inputs(self):
        self.assertEqual(len(cloud_trace()), 16)


if __name__ == "__main__":
    if "--compare" in sys.argv:
        print(json.dumps({"states": state_trace(), "generations": generation_trace(), "selection": selection_trace(), "cloud": cloud_trace()}, sort_keys=True))
    else:
        unittest.main()
