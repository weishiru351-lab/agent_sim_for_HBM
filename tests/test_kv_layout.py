from dataclasses import replace
from itertools import permutations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.contracts import CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_finish, session_init, submit, submit_file
from agent_design_search.json_io import save
from agent_design_search.spaces.kv_layout_dsl import AXES, Layout
from experiments.legacy_cases import KVLayoutTask, LayoutConfig
from experiments.legacy_cases import verify_contents
from experiments.layout_study import analytic_choice, legal_layouts


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.cfg = LayoutConfig(tokens=4, dimensions=4, line_bytes=8, capacity_bytes=32, read_every=4)

    def test_hand_calculated_addresses_and_read_write_tradeoff(self):
        task = KVLayoutTask(self.cfg)
        layouts = [Layout(1, 4), Layout(4, 1), Layout(2, 2)]
        self.assertEqual(layouts[2].offsets(self.cfg), [0, 2, 8, 10, 4, 6, 12, 14, 16, 18, 24, 26, 20, 22, 28, 30])
        results = [Evaluator(task).evaluate(p.to_dict())["metrics"] for p in layouts]
        self.assertEqual([(m["write_transactions"], m["read_transactions"]) for m in results], [(4, 16), (16, 4), (8, 8)])
        self.assertEqual([m["transferred_bytes"] for m in results], [160, 160, 128])
        write_only = KVLayoutTask(replace(self.cfg, read_every=0))
        self.assertEqual([write_only.measure(p.to_dict())["transferred_bytes"] for p in layouts], [32, 128, 64])
        every_append = KVLayoutTask(replace(self.cfg, read_every=1))
        self.assertEqual([every_append.measure(p.to_dict())["transferred_bytes"] for p in layouts], [352, 256, 256])

    def test_all_small_axis_orders_partial_tiles_and_content(self):
        cfg = LayoutConfig(tokens=3, dimensions=5, line_bytes=8, capacity_bytes=128, read_every=2)
        task = KVLayoutTask(cfg)
        for token in range(1, 4):
            for dimension in range(1, 6):
                for order in permutations(AXES):
                    policy = Layout(token, dimension, order).to_dict()
                    self.assertEqual(task.check(task.normalize(policy))["status"], "pass")
        metrics = task.measure(Layout(2, 3).to_dict())
        self.assertEqual(metrics["allocated_bytes"], 48)  # 4 padded tokens x 6 dimensions x 2 bytes.
        self.assertEqual(metrics["write_useful_bytes"], 30)  # Padding is never logical payload.
        self.assertEqual(metrics["read_useful_bytes"], 50)  # Prefixes of length 2 and 3.

    def test_malformed_dsl_resource_limits_and_config_types(self):
        for key, value in (("token", True), ("token", 0), ("dimension", 5)):
            policy = Layout(2, 2).to_dict()
            policy["tile"][key] = value
            with self.assertRaises(CandidateError) as error:
                Layout.parse(policy, self.cfg)
            self.assertEqual(error.exception.path, f"$.dsl.tile.{key}")
        for order in (["token_inner"] * 4, [[], *AXES[:3]], None):
            with self.assertRaises(CandidateError):
                Layout.parse({**Layout(2, 2).to_dict(), "order": order}, self.cfg)
        with self.assertRaises(CandidateError) as error:
            Layout.parse(Layout(3, 3).to_dict(), self.cfg)
        self.assertEqual(error.exception.to_dict()["details"], {"required_bytes": 72, "capacity_bytes": 32})
        for override in ({"read_every": True}, {"tokens": 65}, {"element_bytes": 16}, {"capacity_bytes": 31}):
            with self.assertRaises(ValueError):
                replace(self.cfg, **override)

    def test_semantic_cache_does_not_merge_merely_equal_scores(self):
        evaluator = Evaluator(KVLayoutTask(self.cfg))
        evaluator.evaluate(Layout(1, 4).to_dict())
        self.assertTrue(evaluator.evaluate(Layout(4, 4).to_dict())["cached"])
        self.assertFalse(evaluator.evaluate(Layout(4, 1).to_dict())["cached"])
        self.assertEqual(evaluator.backend_calls, 2)

    def test_analytic_choice_matches_independent_full_small_space(self):
        for every in (0, 1, 4):
            cfg = replace(self.cfg, read_every=every)
            task, evaluator = KVLayoutTask(cfg), Evaluator(KVLayoutTask(cfg))
            for candidate in legal_layouts(cfg):
                evaluator.evaluate(candidate)
            choice, _ = analytic_choice(cfg)
            self.assertEqual(task.measure(choice["dsl"])["transferred_bytes"], choice["predicted_bytes"])
            self.assertEqual(choice["predicted_bytes"], min(e["metrics"]["transferred_bytes"] for e in evaluator.cache.values()))

    def test_independent_reference_catches_address_bug_and_read_before_write(self):
        task, layout = KVLayoutTask(self.cfg), Layout(2, 2)
        with patch.object(Layout, "offsets", return_value=[0] * 16):
            with self.assertRaises(CandidateError) as error:
                Evaluator(task).evaluate(layout.to_dict())
        failure = error.exception.details["verification"]
        self.assertEqual((failure["reason"], failure["element"], failure["actual_address"]), ("placement_mismatch", [0, 1], 0))
        failure = verify_contents(layout, self.cfg, (("read", (0,)), *task.groups))
        self.assertEqual((failure["reason"], failure["event"], failure["actual_value"]), ("content_mismatch", 0, None))


class LayoutSessionTests(unittest.TestCase):
    def test_invalid_files_counterexamples_repair_and_budget(self):
        task = KVLayoutTask(LayoutConfig(tokens=4, dimensions=4, line_bytes=8, capacity_bytes=32, read_every=4))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            session_init(folder, task, budget=2)
            path = folder / "proposal.json"
            path.write_text('{"dsl":', encoding="utf-8")
            failed = submit_file(folder, task, path)
            self.assertEqual((failed["status"], failed["backend_calls"], failed["error_detail"]["code"]), ("invalid", 0, "invalid_json"))
            self.assertEqual(failed["proposal"], '{"dsl":')
            proposal = {"hypothesis": "balance reads/writes", "expected": "128 bytes", "dsl": Layout(2, 2).to_dict()}
            with patch.object(task, "check", return_value={"status": "fail", "event": 7, "expected": 1, "actual": 0}):
                failed = submit(folder, task, proposal)
            self.assertEqual(failed["error_detail"]["details"]["verification"]["event"], 7)
            self.assertEqual(failed["backend_calls"], 0)
            save(path, proposal)
            self.assertEqual(submit_file(folder, task, path)["backend_calls"], 1)
            self.assertTrue(submit_file(folder, task, path)["cached"])
            with patch.object(task, "measure", side_effect=RuntimeError("failed backend")):
                failed = submit(folder, task, {**proposal, "dsl": Layout(1, 4).to_dict()})
            self.assertEqual((failed["status"], failed["backend_calls"]), ("backend_error", 2))
            result = session_finish(folder, task)
            self.assertEqual(result["best"]["metrics"]["transferred_bytes"], 128)
            self.assertEqual(result["validation_seconds"], sum(e["validation_seconds"] for e in result["history"]))
            changed = KVLayoutTask(replace(task.cfg, read_every=1))
            with self.assertRaisesRegex(ValueError, "Frozen task changed"):
                session_finish(folder, changed)

    def test_malformed_files_cannot_bypass_proposal_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = KVLayoutTask(LayoutConfig())
            session_init(tmp, task, budget=1)
            path = Path(tmp) / "bad.json"
            path.write_text("{", encoding="utf-8")
            for _ in range(10):
                submit_file(tmp, task, path)
            with self.assertRaisesRegex(ValueError, "Proposal limit"):
                submit_file(tmp, task, path)



if __name__ == "__main__":
    unittest.main()
