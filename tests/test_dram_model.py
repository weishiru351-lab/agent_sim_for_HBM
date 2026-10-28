from itertools import permutations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.spaces.dram_dsl import Policy, permutation_policy, verify_mapping
from experiments.analyze_archives import phase_feedback
from experiments.legacy_cases import simulate_mapping as simulate
from experiments.legacy_cases import Config, Request, workload
from agent_design_search.verification.dram_rtl import export_rtl


class MappingTests(unittest.TestCase):
    def test_exhaustive_space_is_720_distinct_reversible_permutations(self):
        cfg = Config(channel_bits=2, bank_bits=1, column_bits=1, row_bits=2)
        matrices = set()
        for order in permutations(range(6)):
            policy = permutation_policy(order, cfg)
            matrices.add(policy.masks(cfg))
            for block in range(64):
                expected = int("".join(str((block >> bit) & 1) for bit in reversed(order)), 2)
                self.assertEqual(policy.apply(block), expected)
                self.assertEqual(policy.inverse(expected), block)
        self.assertEqual(len(matrices), 720)

    def test_documented_example_and_offset(self):
        cfg = Config(channel_bits=2, bank_bits=2, column_bits=2, row_bits=2)
        policy = Policy.parse({"version": 0, "operations": [
            {"op": "xor_bit", "dst": 0, "src": 6},
            {"op": "xor_bit", "dst": 1, "src": 7}]}, cfg)
        self.assertEqual([policy.decode(b * 64 + 13, cfg)[0] for b in (0, 64, 128, 192)], [0, 1, 2, 3])
        self.assertEqual(policy.decode(64 * 64 + 13, cfg)[-1], 13)
        self.assertEqual(len({policy.apply(b) for b in range(256)}), 256)
        self.assertTrue(all(policy.inverse(policy.apply(b)) == b for b in range(256)))

    def test_sequential_dependency_and_cancellation(self):
        cfg = Config()
        policy = Policy((("xor_bit", 0, 1), ("xor_bit", 2, 0)))
        self.assertEqual(policy.apply(2), 7)
        self.assertEqual(verify_mapping(policy, cfg)["status"], "pass")
        cancelled = Policy((("swap_bits", 0, 7), ("swap_bits", 0, 7)))
        self.assertEqual(cancelled.masks(cfg), Policy().masks(cfg))

    def test_invalid_dsl_and_address(self):
        cfg = Config()
        invalid = [{"version": True, "operations": []}, {"version": 0, "operations": [{"op": []}]},
                   {"version": 0, "operations": [{"op": "xor_bit", "dst": 1, "src": 1}]},
                   {"version": 0, "operations": [{"op": "xor_bit", "dst": 0, "src": 99}]},
                   {"version": 0, "operations": [], "hardware": {}}]
        for obj in invalid:
            with self.assertRaises(ValueError):
                Policy.parse(obj, cfg)
        with self.assertRaises(ValueError):
            Policy().decode(cfg.blocks * cfg.granule_bytes, cfg)


class TimingTests(unittest.TestCase):
    def test_hand_calculated_row_timing_and_drain(self):
        cfg = Config(channel_bits=0, bank_bits=0, column_bits=1, row_bits=1, queue_depth=1)
        requests = [Request(i, address, 0) for i, address in enumerate((0, 64, 128))]
        result = simulate(requests, Policy(), cfg, True)
        self.assertEqual([e["finish"] for e in result["events"]], [16, 24, 48])
        self.assertEqual(result["completion_cycles"], 48)
        self.assertEqual(result["completed_requests"], 3)
        self.assertEqual(result["row_hit_rate"], 1/3)
        self.assertGreater(result["avg_backpressure_wait"], 0)

    def test_bank_overlap_but_serial_channel_bus(self):
        cfg = Config(channel_bits=0, bank_bits=1, column_bits=0, row_bits=1)
        result = simulate([Request(0, 0, 0), Request(1, 64, 0)], Policy(), cfg, True)
        self.assertEqual([e["issue"] for e in result["events"]], [0, 1])
        self.assertEqual([e["finish"] for e in result["events"]], [16, 20])
        self.assertGreaterEqual(result["events"][1]["transfer"], result["events"][0]["finish"])

    def test_channels_operate_in_parallel(self):
        cfg = Config(channel_bits=1, bank_bits=0, column_bits=0, row_bits=1)
        result = simulate([Request(0, 0, 0), Request(1, 64, 0)], Policy(), cfg)
        self.assertEqual(result["completion_cycles"], 16)

    def test_long_idle_and_many_requests_no_loss(self):
        cfg = Config(queue_depth=1)
        requests = workload("random", cfg, 200, 5)
        requests.append(Request(200, 0, 100000))
        result = simulate(requests, Policy(), cfg, True)
        self.assertEqual(len({e["request_id"] for e in result["events"]}), 201)
        for event in result["events"]:
            self.assertLessEqual(event["arrival"], event["admitted"])
            self.assertLessEqual(event["admitted"], event["issue"])
            self.assertLess(event["issue"], event["finish"])
        for channel in range(4):
            transfers = sorted((e["transfer"], e["finish"]) for e in result["events"] if e["channel"] == channel)
            self.assertTrue(all(a[1] <= b[0] for a, b in zip(transfers, transfers[1:])))
        self.assertGreaterEqual(result["completion_cycles"], 100000)

    def test_invalid_requests_rejected(self):
        for requests in ([], [Request(0, 1, 0)], [Request(0, 0, -1)], [Request(0, 0, 0), Request(0, 64, 0)]):
            with self.assertRaises(ValueError):
                simulate(requests, Policy(), Config())


class RTLTests(unittest.TestCase):

    def test_rtl_missing_tool_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp, patch("agent_design_search.verification.dram_rtl.shutil.which", return_value=None):
            result = export_rtl(Policy((("xor_bit", 0, 6),)), Config(), tmp)
            self.assertEqual(result["status"], "not_run")
            self.assertIn("address[12]", (Path(tmp) / "address_mapper.sv").read_text())
            self.assertTrue((Path(tmp) / "tb.sv").exists())


class PhaseFeedbackTests(unittest.TestCase):
    def test_cohort_feedback_retains_queue_and_open_row_state(self):
        cfg = Config(channel_bits=0, bank_bits=0, column_bits=1, row_bits=1)
        requests = [Request(10, 0, 0), Request(11, 0, 1), Request(12, 128, 2)]
        result = simulate(requests, Policy(), cfg, include_events=True)
        feedback = phase_feedback(requests, result['events'], cfg, 2)
        first, second = feedback['first_cohort'], feedback['second_cohort']
        self.assertEqual([e['finish'] for e in result['events']], [16, 24, 48])
        self.assertEqual(feedback['first_cohort_outstanding_at_boundary'], 2)
        self.assertEqual(first['row_hit_rate'], .5)
        self.assertEqual(second['row_conflict_rate'], 1)
        self.assertEqual(second['avg_latency'], 46)
        for cohort in (first, second):
            self.assertAlmostEqual(cohort['avg_latency'], cohort['avg_backpressure_wait']
                                   + cohort['avg_controller_wait'] + cohort['avg_issue_to_finish'])
        self.assertAlmostEqual((2 * first['avg_latency'] + second['avg_latency']) / 3,
                               result['avg_latency'])
        cold = simulate([Request(12, 128, 0)], Policy(), cfg)
        self.assertEqual(cold['completion_cycles'], 16)
        self.assertNotEqual(first['last_finish'] + cold['completion_cycles'], result['completion_cycles'])

    def test_missing_duplicate_events_and_invalid_boundary_rejected(self):
        cfg = Config(channel_bits=0, bank_bits=0, column_bits=1, row_bits=1)
        requests = [Request(4, 0, 0), Request(7, 64, 1)]
        events = simulate(requests, Policy(), cfg, include_events=True)['events']
        for invalid in (events[:1], events + events[:1]):
            with self.assertRaises(ValueError):
                phase_feedback(requests, invalid, cfg, 1)
        for boundary in (0, 2, True):
            with self.assertRaises(ValueError):
                phase_feedback(requests, events, cfg, boundary)


if __name__ == '__main__':
    unittest.main()
