"""NAND placement legality, independent replay, bounded concurrency and sessions."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.__main__ import load_task
from agent_design_search.contracts import CandidateError, NANDAction
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.inputs.hardware import NANDGeometry, NANDTarget
from agent_design_search.json_io import read
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.execution.nand import NANDExecutor, NANDState
from agent_design_search.performance.nand import NANDParameters, NANDParallelTiming, NANDTiming
from test_nand import access, timing
from test_memory_migration import migration_spec


def specification():
    return read("examples/nand_mapping_search.json")


def mapped(task, stripe=1, order=("channel", "die")):
    design = task.default_design
    design["nand_page_mapping"]["targets"]["n0"] = dict(axis_order=list(order), stripe_pages=stripe)
    return design


class PageMappingTests(unittest.TestCase):
    def test_mixed_radix_selection_and_updates_preserve_data(self):
        geometry = NANDGeometry(3, 2, 3, 2, 2, 8)
        target = NANDTarget("m0", geometry, 12, 4096, ("bus",))
        for order in (["channel", "die"], ["die", "channel"]):
            controller = NANDExecutor(target, dict(axis_order=order, stripe_pages=2), 4)
            for logical in range(12):
                controller.execute(access(logical, "write", range(8), logical * 8), (logical,) * 8)
                physical = controller.state.bindings[logical]
                expected = ((logical // 2) % 3, (logical // 6) % 2) if order[0] == "channel" else ((logical // 4) % 3, (logical // 2) % 2)
                self.assertEqual(geometry.decode(physical)[:2], expected)
            old = controller.state.bindings[0]
            controller.execute(access(20, "write", [0], 0), ("new",))
            self.assertNotEqual(controller.state.bindings[0], old)
            self.assertEqual(controller.state.logical_contents(0), ("new",) + (0,) * 7)
            self.assertEqual(controller.metrics()["preserved_bytes"], 7)

    def test_selected_die_exhaustion_does_not_steal_free_pages(self):
        task = MemorySearchTask(specification())
        controller = NANDExecutor(task.system.targets["n0"], dict(axis_order=["channel", "die"], stripe_pages=4))
        for i in range(8):
            controller.execute(access(i, "write", range(8)), (i,) * 8)
        with self.assertRaises(CandidateError) as error:
            controller.execute(access(8, "write", range(8)), (8,) * 8)
        self.assertEqual(error.exception.code, "nand_lane_exhausted")
        self.assertEqual(controller.metrics()["free_pages"], 24)
        self.assertEqual(controller.state.logical_contents(0), (7,) * 8)
        controller.erase(0)  # All four pages of this block are stale.
        self.assertEqual(controller.state.free_page(0), 0)
        spec = specification(); data = spec["workload"]["data"]
        data["objects"]["V"].update(tokens=9, dimensions=1)
        data["events"][0]["count"] = 9
        data["events"][1].update(tokens=[0, 9], dimensions=[0, 1])
        task = MemorySearchTask(spec); evaluator = Evaluator(task)
        with self.assertRaises(CandidateError) as error:
            evaluator.evaluate(mapped(task, 4))
        self.assertEqual(error.exception.details["verification"]["reason"], "nand_lane_exhausted")
        self.assertEqual(evaluator.backend_calls, 0)

    def test_rule_uses_target_logical_page_including_region_base(self):
        spec = specification()
        spec["hardware"]["regions"]["nand"].update(base_address=8, size_bytes=56)
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        operations = task.measure(task.default_design, True)["nand_operations"]
        programs = sorted((r for r in operations if r["kind"] == "program"), key=lambda r: r["request_id"])
        self.assertEqual([r["coordinates"][:2] for r in programs], [[1, 0], [0, 1], [1, 1], [0, 0]])

    def test_invalid_schema_fixed_components_and_budget(self):
        task = MemorySearchTask(specification())
        bad_rules = [None, {}, {"axis_order": ["channel", "plane"], "stripe_pages": 1},
                     {"axis_order": ["channel", "die"], "stripe_pages": True},
                     {"axis_order": ["die", "channel"], "stripe_pages": 3}]
        evaluator = Evaluator(task)
        for rule in bad_rules:
            candidate = task.default_design
            candidate["nand_page_mapping"]["targets"]["n0"] = rule
            with self.assertRaises(CandidateError) as error:
                evaluator.evaluate(candidate)
            self.assertEqual(error.exception.code, "invalid_nand_mapping")
            self.assertIn("nand_page_mapping", error.exception.path)
        self.assertEqual(evaluator.backend_calls, 0)
        spec = specification(); spec["design_space"]["vary"] = ["kv_layout"]
        fixed = MemorySearchTask(spec)
        with self.assertRaises(CandidateError) as error:
            fixed.normalize(mapped(fixed, 2))
        self.assertEqual(error.exception.code, "fixed_strategy")
        for mutate in (lambda s: s["execution"]["nand"].update(buffer_pages=5),
                       lambda s: s["execution"]["nand"].update(buffer_pages=True),
                       lambda s: s["execution"]["nand"].update(buffer_pages=65),
                       lambda s: s["design_space"].pop("nand_options"),
                       lambda s: s["design_space"]["nand_options"].update(stripe_pages=[1, 1])):
            spec = specification(); mutate(spec)
            with self.assertRaises(ValueError): MemorySearchTask(spec)

    def test_independent_replay_rejects_wrong_lane_before_performance(self):
        task = MemorySearchTask(specification())
        original = NANDState.free_page
        def wrong_lane(state, logical):
            return original(state, logical + 1)
        evaluator = Evaluator(task)
        with patch.object(NANDState, "free_page", wrong_lane), self.assertRaises(CandidateError) as error:
            evaluator.evaluate(task.default_design)
        self.assertEqual(error.exception.details["verification"]["reason"], "nand_action_mismatch")
        self.assertEqual(evaluator.backend_calls, 0)

    def test_hand_costs_same_traffic_and_real_parallel_resources(self):
        task = MemorySearchTask(specification())
        for stripe, completion in ((1, 242), (2, 362), (4, 632)):
            result = Evaluator(task).evaluate(mapped(task, stripe))
            self.assertEqual(result["verification"]["status"], "pass")
            metrics = result["metrics"]
            self.assertEqual(metrics["completion_ps"], completion)
            n = metrics["per_target"]["n0"]
            self.assertEqual((n["read_pages"], n["program_pages"], n["transferred_bytes"]), (4, 4, 64))
            self.assertEqual((n["scratch_peak_bytes"], n["management_reserved_bytes"]), (32, 160))
            self.assertEqual(n["channel_busy_ps"], {"0": 80, "1": 0} if stripe == 4 else {"0": 40, "1": 40})
            self.assertEqual(sum(v > 0 for v in n["die_busy_ps"].values()), 4 // stripe)
        serial = specification(); serial["execution"]["nand"]["buffer_pages"] = 1
        task = MemorySearchTask(serial)
        self.assertEqual(task.measure(mapped(task, 1))["completion_ps"], task.measure(mapped(task, 4))["completion_ps"])

    def test_migration_and_ssd_host_mode_compose_without_nand_fields_on_ssd(self):
        for pair in (("dram", "nand"), ("nand", "nand"), ("nand", "ssd"), ("ssd", "nand")):
            spec = migration_spec(*pair)
            legacy = MemorySearchTask(spec)
            expected = legacy.measure(legacy.default_design)
            spec["execution"]["nand"] = dict(kind="channel_die", buffer_pages=1)
            spec["design_space"].update(nand_options={"stripe_pages": [1]})
            spec["design_space"]["vary"].append("nand_page_mapping")
            rules = {n: {"axis_order": ["channel", "die"], "stripe_pages": 1}
                     for n, t in spec["hardware"]["targets"].items() if t["medium"] == "nand"}
            spec["initial_design"]["nand_page_mapping"] = dict(version=1, targets=rules)
            task = MemorySearchTask(spec)
            result = Evaluator(task).evaluate(task.default_design)
            self.assertEqual(result["metrics"]["completion_ps"], expected["completion_ps"])
            self.assertEqual(result["verification"]["migrations"], 1)
            self.assertEqual(task.backend, "fcfs-nand-policy-v1")
            bad = task.default_design; bad["nand_page_mapping"]["targets"]["ssd"] = deepcopy(next(iter(rules.values())))
            with self.assertRaises(CandidateError): task.normalize(bad)

    def test_file_session_roundtrip_identity_cache_and_proposal(self):
        task = MemorySearchTask(specification())
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)
        self.assertNotEqual(task.candidate_id(mapped(task, 1)), task.candidate_id(mapped(task, 4)))
        self.assertNotEqual(task.candidate_id(mapped(task, 1)), task.candidate_id(mapped(task, 1, ["die", "channel"])))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "session"
            session_init(folder, task, seeds=[("striped", task.default_design)])
            resumed = load_task(folder)
            proposal = read("examples/nand_mapping_proposal.json")
            self.assertEqual(submit(folder, resumed, proposal)["backend_calls"], 2)
            self.assertEqual(submit(folder, resumed, proposal)["backend_calls"], 2)
            self.assertIn("best", session_finish(folder, resumed))


class ParallelTimingTests(unittest.TestCase):
    def engine(self, channels=1, dies=1, planes=1, buffers=4):
        return NANDParallelTiming(NANDGeometry(channels, dies, planes, 2, 2, 8), NANDParameters(**timing()), buffers)

    def test_distinct_channels_shared_bus_and_planes_within_die(self):
        for channels, dies, planes, cost in ((2, 1, 1, 110), (1, 2, 1, 120), (1, 1, 2, 220)):
            engine = self.engine(channels, dies, planes)
            engine.submit_batch([access(0, "write", range(8), 0), access(1, "write", range(8), 8)],
                                {0: [NANDAction("program", 0)], 1: [NANDAction("program", 4)]})
            self.assertEqual(engine.completed, cost)
            self.assertEqual(engine.counts["array_ps"], 200)
            self.assertEqual(engine.counts["bus_ps"], 20)

    def test_read_register_stays_busy_through_data_out(self):
        engine = self.engine(dies=2)
        engine.submit_batch([access(0, "read", range(8), 0), access(1, "read", range(8), 8), access(2, "write", range(8), 16)],
                            {0: [NANDAction("read", 0)], 1: [NANDAction("read", 4)], 2: [NANDAction("program", 5)]})
        by_id = {r["request_id"]: r for r in engine.operations}
        self.assertEqual((by_id[0]["finish_ps"], by_id[1]["finish_ps"], by_id[2]["start_ps"]), (40, 50, 50))
        self.assertEqual(engine.completed, 160)

    def test_block_program_order_survives_rmw_and_reversed_arrivals(self):
        for delayed in (False, True):
            engine = NANDParallelTiming(NANDGeometry(1, 1, 1, 2, 4, 8), NANDParameters(**timing()), 4)
            a, b = access(0, "write", [0]), access(1, "write", range(8), 8)
            if delayed: a = replace(a, request=replace(a.request, arrival=100))
            actions = {0: [NANDAction("lookup"), NANDAction("read", 0), NANDAction("merge"), NANDAction("program", 1), NANDAction("bind")],
                       1: [NANDAction("lookup"), NANDAction("merge"), NANDAction("program", 2), NANDAction("bind")]}
            engine.submit_batch([a, b], actions)
            programs = [r for r in engine.operations if r["kind"] == "program"]
            self.assertEqual([r["physical_page"] for r in programs], [1, 2])
            self.assertGreaterEqual(programs[1]["start_ps"], programs[0]["finish_ps"])
            self.assertGreaterEqual(min(e["admitted"] for e in engine.events), 100 if delayed else 0)

    def test_same_logical_page_keeps_request_order_and_one_buffer_serializes(self):
        for same_page, buffers in ((True, 4), (False, 1)):
            engine = self.engine(channels=2, buffers=buffers)
            reqs = [access(0, "write", range(8), 0), access(1, "write", range(8), 0 if same_page else 8)]
            actions = {0: [NANDAction("program", 0), NANDAction("bind")], 1: [NANDAction("program", 4), NANDAction("bind")]}
            engine.submit_batch(reqs, actions)
            self.assertEqual(engine.completed, 230)
            self.assertEqual(engine.counts["peak_inflight_pages"], 1)
            self.assertEqual(engine.events[1]["admitted"], engine.events[0]["finish"])
            serial = NANDTiming(engine.geometry, engine.parameters); serial.submit_batch(reqs, actions)
            self.assertEqual(engine.completed, serial.completed)

    def test_overlapping_updates_charge_live_pages_until_bind_then_release(self):
        engine = self.engine(channels=2)
        for batch in (0, 1):
            reqs = [access(batch * 2 + i, "write", range(8), i * 8) for i in range(2)]
            reqs = [replace(a, request=replace(a.request, arrival=engine.completed)) for a in reqs]
            actions = {batch * 2 + i: [NANDAction("program", i * 4 + batch), NANDAction("bind")] for i in range(2)}
            engine.submit_batch(reqs, actions)
        self.assertEqual((engine.live_pages, engine.peak_live_pages), (2, 4))
        engine.invalidate([0, 1], engine.completed, 10)
        self.assertEqual(engine.live_pages, 0)
        self.assertFalse(engine.bound_pages)

    def test_rejects_missing_actions_duplicate_pages_and_undrained_batches(self):
        engine = self.engine()
        a, b = access(0, "write", range(8)), access(1, "write", range(8), 8)
        for actions in ({}, {0: [NANDAction("program", 0)], 1: [NANDAction("program", 0)]}):
            with self.assertRaises(ValueError): engine.submit_batch([a, b], actions)
        engine.submit_batch([a], {0: [NANDAction("program", 0)]})
        with self.assertRaises(ValueError): engine.submit_batch([b], {1: [NANDAction("program", 1)]})


if __name__ == "__main__":
    unittest.main()
