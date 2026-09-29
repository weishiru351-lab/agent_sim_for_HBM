import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from hbm_toy.experiment import (Evaluator, read, run_search, save, session_finish,
                                session_init, session_step)
from hbm_toy.model import Config, Policy, Request, simulate, verify, workload
from hbm_toy.rtl import export_rtl


class MappingTests(unittest.TestCase):
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
        self.assertEqual(verify(policy, cfg)["status"], "pass")
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


class ExperimentTests(unittest.TestCase):
    def test_semantic_cache_and_budget(self):
        cfg = Config()
        requests = workload("stride", cfg, 64)
        evaluator = Evaluator(cfg, requests)
        first = evaluator.evaluate(Policy())
        second = evaluator.evaluate(Policy((("xor_bit", 0, 6), ("xor_bit", 0, 6))))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(evaluator.backend_calls, 1)
        for method in ("random", "local", "activity_local", "packed_local"):
            a = run_search(method, cfg, requests, 6, 42)
            b = run_search(method, cfg, requests, 6, 42)
            self.assertEqual(a["backend_calls"], 6)
            self.assertEqual(a["best"]["candidate_id"], b["best"]["candidate_id"])
            self.assertEqual(a["best"]["metrics"], b["best"]["metrics"])

    def test_session_validation_budget_and_holdout_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "session"
            session_init(folder, Config(), "stride", 64, 0, 3)
            proposal = Path(tmp) / "proposal.json"
            save(proposal, {"hypothesis": "invalid", "expected": "reject", "dsl": {}})
            self.assertEqual(session_step(folder, proposal)["status"], "invalid")
            self.assertEqual(read(folder / "state.json")["backend_calls"], 2)
            save(proposal, {"hypothesis": "spread traffic", "expected": "less conflict",
                            "dsl": {"version": 0, "operations": [{"op": "xor_bit", "dst": 0, "src": 6}]}})
            session_step(folder, proposal)
            self.assertNotIn("holdout", read(folder / "feedback.json"))
            with self.assertRaises(ValueError):
                session_step(folder, proposal)
            result = session_finish(folder)
            self.assertIn("holdout", result)
            self.assertTrue(read(folder / "state.json")["closed"])

    def test_rtl_missing_tool_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp, patch("hbm_toy.rtl.shutil.which", return_value=None):
            result = export_rtl(Policy((("xor_bit", 0, 6),)), Config(), tmp)
            self.assertEqual(result["status"], "not_run")
            self.assertIn("address[12]", (Path(tmp) / "address_mapper.sv").read_text())
            self.assertTrue((Path(tmp) / "tb.sv").exists())


if __name__ == "__main__":
    unittest.main()
