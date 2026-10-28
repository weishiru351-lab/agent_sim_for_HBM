"""Detailed SSD: host/NAND boundaries, independent replay and shared timing."""

from dataclasses import replace
import random
import unittest
from unittest.mock import patch

from agent_design_search.contracts import BlockIO, CandidateError
from agent_design_search.inputs.hardware import SSDTarget, BlockGeometry, NANDTarget, NANDGeometry
from agent_design_search.execution.ssd import NANDBackedSSD
from agent_design_search.execution.nand import NANDExecutor
from agent_design_search.performance.ssd import NANDSSDTiming, SSDInterfaceParameters
from agent_design_search.performance.nand import NANDParallelTiming, NANDParameters
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.exploration.evaluation import Evaluator
from test_nand import access, timing


def device(block_bytes=8, page_bytes=8):
    nand = NANDTarget("m0", NANDGeometry(2, 1, 1, 8, 4, page_bytes), 24, 4096, ())
    return SSDTarget("m0", BlockGeometry(8, block_bytes), 1, block_bytes, (), nand, "open")


def detailed_task():
    return MemorySearchTask.from_file("examples/ssd_nand_search.json")


class DetailedSSDTests(unittest.TestCase):
    def test_same_nand_commands_plus_hand_counted_interface_cost(self):
        target, mapping = device(), dict(axis_order=["channel", "die"], stripe_pages=1)
        ssd = NANDBackedSSD(target, mapping, 2, "foreground_greedy")
        raw = NANDExecutor(target.nand, mapping, 2, "foreground_greedy")
        engine = NANDSSDTiming(target, SSDInterfaceParameters(7, 3, 5), NANDParameters(**timing()),
                               dict(buffer_pages=2, gc="foreground_greedy"))
        raw_engine = NANDParallelTiming(target.nand.geometry, NANDParameters(**timing()), 2, "foreground_greedy")
        for i, kind in enumerate(("write", "read")):
            req = access(i, kind, range(8))
            host, _ = ssd.execute(req, ("a",) * 8)
            actions, _ = raw.execute(req, ("a",) * 8)
            self.assertEqual(host[0].pages[0].actions, tuple(actions))
            engine.submit_batch([BlockIO(replace(req.request, arrival=engine.completed), 0)],
                                {i: host[0].pages})
            raw_engine.submit_batch([replace(req, request=replace(req.request, arrival=raw_engine.completed))],
                                    {i: actions})
        self.assertEqual(engine.completed, raw_engine.completed + 20)
        self.assertEqual(engine.counts["transferred_bytes"], 16)
        self.assertEqual(engine.nand.counts["program_pages"], 1)

    def test_smaller_larger_and_crossing_host_blocks_preserve_neighbor_bytes(self):
        for width, page in ((4, 8), (8, 8), (16, 8), (12, 8), (8, 12)):
            with self.subTest(width=width, page=page):
                c = NANDBackedSSD(device(width, page), dict(axis_order=["channel", "die"], stripe_pages=1),
                                  2, "foreground_greedy")
                expected, rng = {}, random.Random(13)
                for i in range(140):
                    lba = rng.randrange(8)
                    mask = rng.sample(range(width), rng.randrange(1, width + 1))
                    values = tuple((i, b) for b in mask)
                    c.execute(access(i, "write", mask, lba * width), values)
                    data = expected.setdefault(lba, [None] * width)
                    for b, value in zip(mask, values):
                        data[b] = value
                    for block, contents in expected.items():
                        for b, value in enumerate(contents):
                            p, byte = divmod(block * width + b, page)
                            self.assertEqual(c.nand.state.logical_contents(p)[byte], value)
                self.assertGreater(c.nand.metrics()["gc_cycles"], 0)

    def test_integrated_gc_costs_and_no_double_counted_links(self):
        task = detailed_task()
        result = Evaluator(task).evaluate(task.default_design)
        self.assertEqual(result["verification"]["status"], "pass")
        m = task.measure(task.default_design, True)
        s = m["per_target"]["s0"]
        self.assertEqual(m["transferred_bytes"], s["transferred_bytes"])
        self.assertEqual(m["per_link"]["host"]["transferred_bytes"], s["transferred_bytes"])
        self.assertGreater(s["nand"]["gc_copied_pages"], 0)
        self.assertEqual(s["nand"]["program_pages"], s["nand"]["frontend_program_pages"] + s["nand"]["gc_copied_pages"])
        # Host release is not TRIM: final bound pages persist and later writes preserve opaque bytes.
        self.assertEqual(s["nand"]["logical_bound_pages"], 6)
        self.assertTrue(any(op.get("purpose") == "gc" for op in m["ssd_nand_operations"]))
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)

    def test_closed_device_hides_controller_design_and_keeps_fixed_internal_rule(self):
        spec = detailed_task().snapshot()
        spec.pop("fixed_design")
        spec["hardware"]["targets"]["s0"]["controller_access"] = "closed"
        spec["execution"]["nand"]["gc"] = "foreground_greedy"
        spec["design_space"]["vary"] = ["kv_layout"]
        for key in ("gc_options", "nand_options"):
            del spec["design_space"][key]
        spec["evaluation"]["backend"] = "fcfs"
        task = MemorySearchTask(spec)
        self.assertNotIn("nand_page_mapping", task.default_design)
        self.assertNotIn("nand_gc", task.default_design)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        candidate = task.default_design
        candidate["nand_gc"] = detailed_task().default_design["nand_gc"]
        with self.assertRaises(ValueError):
            task.normalize(candidate)
        spec["execution"]["nand"]["gc"] = "policy"
        with self.assertRaisesRegex(ValueError, "open NAND controller"):
            MemorySearchTask(spec)

    def test_full_and_partial_host_blocks_sharing_a_page_keep_program_order(self):
        spec = detailed_task().snapshot()
        spec.pop("fixed_design")
        spec["hardware"]["targets"]["s0"]["geometry"] = dict(logical_blocks=16, block_bytes=4)
        workload = spec["workload"]["data"]
        workload["objects"]["V"].update(tokens=4, dimensions=3)
        for event in workload["events"]:
            if event["op"] == "append":
                event["count"] = 4
            elif event["op"] == "read":
                event.update(tokens=[0, 4], dimensions=[0, 3])
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        metrics = task.measure(task.default_design, True)
        operations = metrics["ssd_nand_operations"]
        programmed = {}
        for operation in sorted(operations, key=lambda op: op["start_ps"]):
            if "physical_page" not in operation:
                continue
            page = operation["physical_page"]
            if operation["kind"] == "program":
                programmed[page] = operation["finish_ps"]
            elif operation["kind"] == "read":
                self.assertIn(page, programmed)
                self.assertGreaterEqual(operation["start_ps"], programmed[page])
            elif operation["kind"] == "erase":
                for p in range(page, page + 4):
                    programmed.pop(p, None)

    def test_independent_checker_rejects_missing_internal_commands_before_performance(self):
        task, execute = detailed_task(), NANDBackedSSD.execute
        def omit(self, *args):
            actions, values = execute(self, *args)
            actions[0] = replace(actions[0], pages=())
            return actions, values
        evaluator = Evaluator(task)
        with patch.object(NANDBackedSSD, "execute", omit), self.assertRaises(CandidateError) as caught:
            evaluator.evaluate(task.default_design)
        self.assertEqual(caught.exception.details["verification"]["reason"], "ssd_page_coverage")
        self.assertEqual(evaluator.backend_calls, 0)


if __name__ == "__main__":
    unittest.main()
