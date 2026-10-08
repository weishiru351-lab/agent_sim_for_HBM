from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hbm_toy.agent_v2 import compare, prepare
from hbm_toy.experiment import read, save, session_finish, session_init, session_step
from hbm_toy.model import Config
from hbm_toy.trace_bridge import load_trace, submit


class TraceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Config(channel_bits=1, bank_bits=1, column_bits=1, row_bits=1)
        self.trace = self.root / "input.json"
        save(self.trace, {"version": 1, "requests": [
            {"request_id": i, "address": (i % 16) * 64, "arrival": i // 8} for i in range(32)]})

    def start(self, budget=4):
        folder = self.root / "session"
        session_init(folder, self.cfg, "unused", 0, 0, budget, trace_path=self.trace, space="permutation")
        return folder

    def proposal(self, ops):
        return {"hypothesis": "test channel selection", "expected": "different occupancy",
                "dsl": {"version": 0, "operations": ops}}

    def test_trace_validation_and_frozen_copy(self):
        bad = read(self.trace)
        bad["requests"][0]["address"] = True
        save(self.root / "bad.json", bad)
        with self.assertRaises(ValueError):
            load_trace(self.root / "bad.json", self.cfg)
        folder = self.start()
        save(self.trace, bad)  # Changing the original does not change the frozen session.
        self.assertEqual(session_finish(folder)["features"]["requests"], 32)
        frozen = read(folder / "trace.json")
        frozen["requests"][0]["address"] = 64
        save(folder / "trace.json", frozen)
        with self.assertRaisesRegex(ValueError, "Frozen trace changed"):
            session_finish(folder)

    def test_cache_illegal_space_and_actual_execution_budget(self):
        folder = self.start(budget=3)
        self.assertEqual(submit(folder, self.proposal([]))["backend_calls"], 2)
        bad = submit(folder, self.proposal([{"op": "xor_bit", "dst": 0, "src": 1}]))
        self.assertEqual((bad["status"], bad["backend_calls"]), ("invalid", 2))
        proposal = self.root / "proposal.json"
        save(proposal, self.proposal([{"op": "swap_bits", "a": 0, "b": 1}]))
        self.assertEqual(session_step(folder, proposal)["backend_calls"], 3)
        with self.assertRaises(ValueError):
            session_step(folder, proposal)
        result = session_finish(folder)
        self.assertNotIn("holdout", result)
        self.assertNotIn("exact", read(folder / "feedback.json"))

    def test_backend_failure_consumes_budget_but_does_not_become_best(self):
        folder = self.start(budget=3)
        with patch("hbm_toy.experiment.simulate", side_effect=RuntimeError("backend failed")):
            entry = submit(folder, self.proposal([{"op": "swap_bits", "a": 0, "b": 1}]))
        self.assertEqual((entry["status"], entry["backend_calls"]), ("backend_error", 3))
        self.assertIn("metrics", session_finish(folder)["best"])

    def test_oracle_rejected_before_search_and_no_prepare_evaluations(self):
        folder = self.root / "study"
        with patch("hbm_toy.experiment.simulate", side_effect=AssertionError("must not evaluate")):
            self.assertEqual(prepare(folder)["performance_evaluations"], 0)
            with self.assertRaises(ValueError):
                compare(folder)
        self.assertFalse((folder / "comparison.json").exists())


if __name__ == "__main__":
    unittest.main()
