"""Raw NAND state, fixed binding, independent replay and hand-calculated costs."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.contracts import CandidateError, Request, PageRequest, NANDAction
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.__main__ import load_task
from agent_design_search.inputs.hardware import NANDGeometry, NANDTarget, MemorySystem
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.execution.nand import NANDExecutor, merge_page
from agent_design_search.performance.nand import NANDParameters, NANDTiming


def nand_target():
    return {"module": "m0", "medium": "nand", "geometry": {
        "channels": 1, "dies_per_channel": 1, "planes_per_die": 1, "blocks_per_plane": 3,
        "pages_per_block": 2, "page_bytes": 8}, "logical_pages": 2, "management_bytes": 64, "links": ["bus"]}


def timing():
    return dict(read_ps=30, program_ps=100, erase_ps=200, transfer_ps=10, lookup_ps=2, merge_ps=3, bind_ps=5)


def nand_spec(mixed=False):
    spec = {"version": 3, "hardware": {"version": 1, "modules": ["m0"], "targets": {"n0": nand_target()},
        "regions": {"nand": {"target": "n0", "base_address": 0, "size_bytes": 16}},
        "pools": {"memory": ["nand"]}, "links": {"bus": {"bytes_per_beat": 8, "beat_ps": 10, "latency_ps": 0}}},
        "workload": {"kind": "kv_events", "data": {"version": 2, "objects": {
            "V": {"tokens": 2, "dimensions": 2, "element_bytes": 2}}, "events": [
            {"op": "append", "object": "V", "count": 2},
            {"op": "read", "object": "V", "tokens": [0, 2], "dimensions": [0, 2], "group_by": "token"},
            {"op": "end"}]}},
        "execution": {"kind": "serial_groups"},
        "design_space": {"vary": ["kv_layout"], "layout_options": {"pool": "memory", "alignment_bytes": [8], "chunk_bytes": [8, 16]}},
        "evaluation": {"backend": "fcfs", "objective": "completion_ps", "nand_parameters": {"n0": timing()}}, "budget": 3}
    if mixed:
        spec["hardware"]["targets"]["d0"] = {"module": "m0", "medium": "dram", "geometry": {
            "channels": 1, "banks_per_channel": 1, "rows": 2, "columns": 2, "granule_bytes": 8},
            "clock_period_ps": 10, "mapping_group": "dram", "links": ["bus"]}
        spec["hardware"]["regions"]["dram"] = {"target": "d0", "base_address": 0, "size_bytes": 32}
        spec["hardware"]["pools"]["memory"] = ["dram", "nand"]
        spec["design_space"]["vary"].append("dram_address_mapping")
    return spec


def access(request_id, kind, mask, address=0):
    return PageRequest(Request(request_id, address, 0, kind), tuple(mask))


class NANDStateTests(unittest.TestCase):
    def controller(self):
        return NANDExecutor(MemorySystem(nand_spec()["hardware"]).targets["n0"])

    def test_non_power_of_two_geometry_and_exported_capacity(self):
        geometry = NANDGeometry(3, 2, 3, 5, 3, 12)
        self.assertEqual(geometry.decode(geometry.pages - 1), (2, 1, 2, 4, 2))
        for page in range(geometry.pages):
            ch, die, plane, block, local = geometry.decode(page)
            self.assertEqual(((((ch * 2 + die) * 3 + plane) * 5 + block) * 3 + local), page)
        for page in (True, -1, geometry.pages):
            with self.assertRaises(ValueError):
                geometry.decode(page)
        target = NANDTarget("m0", geometry, 100, 4096, ("bus",))
        self.assertEqual(target.capacity_bytes, 1200)
        self.assertGreater(geometry.capacity_bytes, target.capacity_bytes)

    def test_program_publish_inflight_and_erase_rules(self):
        controller = self.controller(); state = controller.state
        with self.assertRaises(CandidateError):
            state.begin("program", 1, ("x",) * 8)  # Cannot skip page zero in a block.
        with self.assertRaises(CandidateError):
            state.begin("program", 0, ("x",))
        token = state.begin("program", 0, ("x",) * 8)
        for operation in (lambda: state.bind(0, 0), lambda: state.begin("erase", 0)):
            with self.assertRaises(CandidateError): operation()
        state.complete(token)
        with self.assertRaises(CandidateError): state.begin("erase", 0)  # Unpublished page is protected too.
        state.bind(0, 0)
        with self.assertRaises(CandidateError): state.begin("program", 0, ("y",) * 8)
        with self.assertRaises(CandidateError): state.begin("erase", 0)
        for page in (1, 2):
            state.complete(state.begin("program", page, (str(page),) * 8))
            state.bind(0, page)
        read = state.begin("read", 0)
        with self.assertRaises(CandidateError): state.begin("erase", 0)
        state.complete(read)
        actions = controller.erase(0)
        self.assertEqual(actions, [NANDAction("erase", 0)])
        self.assertEqual(state.free_page(), 0)
        self.assertEqual(state.logical_contents(0), ("2",) * 8)
        state.complete(state.begin("program", 0, ("z",) * 8))
        state.bind(1, 0)
        engine = NANDTiming(state.geometry, NANDParameters(**timing()))
        engine.submit_batch([access(0, "write", range(8))], {0: actions})
        self.assertEqual((engine.completed, engine.counts["erases"], engine.counts["transferred_bytes"]), (200, 1, 0))

    def test_partial_updates_keep_other_objects_and_full_overwrite_skips_old_read(self):
        controller = self.controller()
        a, b = tuple(("K", 0, i) for i in range(4)), tuple(("V", 0, i) for i in range(4))
        controller.execute(access(0, "write", range(4)), a)
        actions, _ = controller.execute(access(1, "write", range(4, 8)), b)
        self.assertEqual([x.kind for x in actions], ["lookup", "read", "merge", "program", "bind"])
        _, values = controller.execute(access(2, "read", range(8)), a + b)
        self.assertEqual(values, a + b)
        self.assertEqual(controller.metrics()["preserved_bytes"], 4)
        actions, _ = controller.execute(access(3, "write", range(8)), b + a)
        self.assertNotIn("read", [x.kind for x in actions])
        self.assertEqual(controller.state.logical_contents(0), b + a)
        self.assertEqual(controller.metrics()["peak_live_bytes"], 16)

    def test_exhaustion_keeps_old_binding_and_never_erases_implicitly(self):
        controller = self.controller()
        for i in range(6):
            controller.execute(access(i, "write", range(8)), (i,) * 8)
        before = dict(controller.state.bindings)
        with self.assertRaises(CandidateError) as error:
            controller.execute(access(6, "write", range(8)), (6,) * 8)
        self.assertEqual(error.exception.code, "nand_exhausted")
        self.assertEqual(controller.state.bindings, before)
        self.assertEqual(controller.state.logical_contents(0), (5,) * 8)
        self.assertFalse(controller.state.pending)


class NANDIntegrationTests(unittest.TestCase):
    def test_pure_nand_rmw_timing_and_accounting_hand_case(self):
        task = MemorySearchTask(nand_spec())
        self.assertEqual(set(task.default_design), {"kv_layout"})
        self.assertEqual(task.backend, "fcfs-hybrid-v1")
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        result = task.measure(task.default_design, True)
        self.assertEqual((result["completion_ps"], result["frontend_transferred_bytes"], result["transferred_bytes"]), (404, 32, 40))
        n = result["per_target"]["n0"]
        self.assertEqual((n["read_pages"], n["program_pages"], n["erases"], n["invalid_pages"]), (3, 2, 0, 1))
        self.assertEqual((n["management_ps"], n["array_ps"], n["bus_ps"]), (24, 290, 50))
        self.assertEqual((n["metadata_bytes"], n["management_reserved_bytes"], n["scratch_peak_bytes"]), (34, 42, 8))
        self.assertEqual((n["physical_peak_bytes"], n["live_bytes"], n["preserved_bytes"]), (16, 8, 4))
        self.assertEqual([e["finish_ps"] for e in result["group_events"]], [130, 300, 352, 404])
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)

    def test_same_workload_on_dram_nand_and_mixed_targets(self):
        spec = nand_spec(mixed=True)
        data = spec["workload"]["data"]
        data["objects"]["V"] = dict(tokens=1, dimensions=8, element_bytes=2)
        data["events"][0]["count"] = 1
        data["events"][1].update(tokens=[0, 1], dimensions=[0, 8])
        task = MemorySearchTask(spec)
        self.assertEqual(task.measure(task.default_design)["completion_ps"], 260)
        for regions in (["dram"], ["nand"], ["dram", "nand"], ["nand", "dram"]):
            design = task.default_design
            design["kv_layout"]["groups"][0]["placement"]["regions"] = regions
            evaluated = Evaluator(task).evaluate(design)
            self.assertEqual(evaluated["verification"]["status"], "pass")
            self.assertEqual(evaluated["metrics"]["read_useful_bytes"], 16)
        multi = MemorySearchTask.from_file("examples/hybrid_search.json")
        for regions in (["dram0", "dram1"], ["nand0", "nand1"], ["dram0", "nand0", "dram1", "nand1"]):
            design = multi.default_design
            design["kv_layout"]["groups"][0]["placement"]["regions"] = regions
            result = Evaluator(multi).evaluate(design)
            self.assertEqual(result["verification"]["objects"], 2)
            self.assertEqual(result["metrics"]["read_useful_bytes"], 6144)

    def test_lcm_layout_with_non_power_of_two_nand_page_size(self):
        spec = nand_spec(mixed=True)
        spec["hardware"]["targets"]["n0"]["geometry"]["page_bytes"] = 12
        spec["hardware"]["regions"]["nand"]["size_bytes"] = 24
        spec["design_space"]["layout_options"] = dict(pool="memory", alignment_bytes=[24], chunk_bytes=[24])
        task = MemorySearchTask(spec)
        self.assertEqual(task.bounds["V"].line_bytes, 24)
        design = task.default_design
        design["kv_layout"]["groups"][0]["placement"]["regions"] = ["nand"]
        self.assertEqual(task.check(task.normalize(design))["status"], "pass")
        self.assertEqual(task.measure(design)["per_target"]["n0"]["program_pages"], 2)

    def test_action_omission_and_lost_preserved_data_fail_before_budget(self):
        task = MemorySearchTask(nand_spec())
        execute = NANDExecutor.execute
        def omit_program(self, *args):
            actions, values = execute(self, *args)
            return [a for a in actions if a.kind != "program"], values
        def lose_old(previous, mask, values):
            return merge_page((None,) * len(previous), mask, values)
        for target, substitute in (("agent_design_search.execution.nand.NANDExecutor.execute", omit_program),
                                   ("agent_design_search.execution.nand.merge_page", lose_old)):
            evaluator = Evaluator(task)
            with patch(target, substitute), self.assertRaises(CandidateError) as error:
                evaluator.evaluate(task.default_design)
            self.assertEqual(evaluator.backend_calls, 0)
            verification = error.exception.details["verification"]
            self.assertEqual(verification["target"], "n0")
            self.assertIn(verification["reason"], ("nand_action_mismatch", "nand_preservation_mismatch"))

    def test_elements_can_cross_non_power_of_two_nand_page_boundaries(self):
        spec = nand_spec()
        spec["hardware"]["targets"]["n0"]["geometry"]["page_bytes"] = 12
        spec["hardware"]["regions"]["nand"]["size_bytes"] = 24
        spec["design_space"]["layout_options"].update(alignment_bytes=[24], chunk_bytes=[24])
        data = spec["workload"]["data"]
        data["objects"]["V"] = dict(tokens=1, dimensions=3, element_bytes=8)
        data["events"][0]["count"] = 1
        data["events"][1].update(tokens=[0, 1], dimensions=[0, 3])
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        metrics = task.measure(task.default_design, True)
        self.assertEqual((metrics["write_useful_bytes"], metrics["transferred_bytes"]), (24, 48))
        split = [(r["address"], b["element_byte"], b["block_offset"], b["object_offset"])
                 for r in metrics["request_sources"] if r["kind"] == "read"
                 for b in r["byte_origins"] if b["index"] == [0, 1]]
        self.assertEqual(split, [(0, b, 8+b, 8+b) for b in range(4)]
                               + [(12, b, b-4, 8+b) for b in range(4, 8)])
        spec["design_space"]["layout_options"].update(alignment_bytes=[12], chunk_bytes=[12])
        with self.assertRaises(ValueError): MemorySearchTask(spec)  # A resource stripe must not split the 8-byte element.

    def test_timing_refuses_missing_operations_and_duplicate_requests(self):
        geometry = NANDGeometry.parse(nand_target()["geometry"])
        engine = NANDTiming(geometry, NANDParameters(**timing()))
        req = access(0, "read", range(8))
        with self.assertRaises(ValueError): engine.submit_batch([req], {})
        engine.submit_batch([req], {0: [NANDAction("read", 0)]})
        with self.assertRaises(ValueError): engine.submit_batch([req], {0: [NANDAction("read", 0)]})

    def test_resource_limits_versions_and_search_permissions(self):
        bad = []
        s = nand_spec(); s["hardware"]["targets"]["n0"]["management_bytes"] = 41; bad.append(s)
        s = nand_spec(); s["hardware"]["targets"]["n0"]["logical_pages"] = 6; bad.append(s)
        s = nand_spec(); s["hardware"]["regions"]["nand"]["size_bytes"] = 24; bad.append(s)
        s = nand_spec(); s["hardware"]["targets"]["n0"]["mapping_group"] = "illegal"; bad.append(s)
        s = nand_spec(); s["evaluation"].pop("nand_parameters"); bad.append(s)
        s = nand_spec(); s["evaluation"]["nand_parameters"]["n0"]["program_ps"] = 0; bad.append(s)
        s = nand_spec(); s["evaluation"]["backend"] = "fcfs-system-v1"; bad.append(s)
        s = nand_spec(); s["design_space"]["vary"].append("dram_address_mapping"); bad.append(s)
        for spec in bad:
            with self.subTest(spec=spec), self.assertRaises(ValueError): MemorySearchTask(spec)
        spec = nand_spec()
        spec["hardware"]["targets"]["n0"]["geometry"].update(blocks_per_plane=1, pages_per_block=3)
        data = spec["workload"]["data"]
        data["objects"]["V"].update(tokens=4, dimensions=1)
        data["events"][0]["count"] = 4
        data["events"][1].update(tokens=[0, 4], dimensions=[0, 1])
        task = MemorySearchTask(spec)
        evaluator = Evaluator(task)
        with self.assertRaises(CandidateError) as error: evaluator.evaluate(task.default_design)
        self.assertEqual(error.exception.details["verification"]["reason"], "nand_exhausted")
        self.assertEqual(evaluator.backend_calls, 0)

    def test_hybrid_file_session_budget_and_cache(self):
        task = MemorySearchTask(nand_spec(mixed=True))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "session"
            session_init(folder, task, task.budget, [("initial", task.default_design)])
            resumed = load_task(folder)
            design = resumed.default_design
            design["kv_layout"]["groups"][0]["placement"]["regions"] = ["nand"]
            proposal = {"hypothesis": "place the object on NAND", "expected": "charged RMW", "dsl": design}
            result = submit(folder, resumed, proposal)
            self.assertEqual(result["backend_calls"], 2)
            self.assertEqual(submit(folder, resumed, proposal)["backend_calls"], 2)
            self.assertIn("best", session_finish(folder, resumed))


if __name__ == "__main__":
    unittest.main()
