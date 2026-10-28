"""SSD host contract, independent replay, charged RMW and queue/link hand cases."""

from copy import deepcopy
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.execution.ssd import BlockExecutor, merge_block
from agent_design_search.contracts import BlockRequest, BlockAction, BlockIO, Request, CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.__main__ import load_task, main
from agent_design_search.inputs.hardware import MemorySystem
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.performance.ssd import BlockParameters, BlockTiming
from agent_design_search.performance.memory_system import SystemFCFS


def ssd_spec():
    return {"version": 3, "hardware": {"version": 1, "modules": ["m0"], "targets": {"s0": {
        "module": "m0", "medium": "ssd", "geometry": {"logical_blocks": 8, "block_bytes": 8},
        "max_transfer_blocks": 4, "host_buffer_bytes": 8, "links": ["bus"]}},
        "regions": {"ssd": {"target": "s0", "base_address": 0, "size_bytes": 64}},
        "pools": {"memory": ["ssd"]}, "links": {"bus": {"bytes_per_beat": 8, "beat_ps": 10, "latency_ps": 0}}},
        "workload": {"kind": "kv_events", "data": {"version": 2, "objects": {
            "V": {"tokens": 2, "dimensions": 2, "element_bytes": 2}}, "events": [
            {"op": "append", "object": "V", "count": 2},
            {"op": "read", "object": "V", "tokens": [0, 2], "dimensions": [0, 2], "group_by": "token"},
            {"op": "end"}]}},
        "execution": {"kind": "serial_groups"},
        "design_space": {"vary": ["kv_layout"], "layout_options": {"pool": "memory", "alignment_bytes": [8], "chunk_bytes": [8, 16]}},
        "evaluation": {"backend": "fcfs", "objective": "completion_ps",
                       "ssd_parameters": {"s0": {"read_ps": 30, "write_ps": 100, "queue_depth": 2, "merge_ps": 3}}}, "budget": 3}


def access(request_id, kind, mask, address=0):
    return BlockRequest(Request(request_id, address, 0, kind), tuple(mask))


class BlockExecutionTests(unittest.TestCase):
    def test_first_partial_write_reads_and_updates_preserve_other_objects(self):
        controller = BlockExecutor(MemorySystem(ssd_spec()["hardware"]).targets["s0"])
        a, b = tuple(("K", 0, i) for i in range(4)), tuple(("V", 0, i) for i in range(4))
        for rid, mask, tags in ((0, range(4), a), (1, range(4, 8), b)):
            actions, _ = controller.execute(access(rid, "write", mask), tags)
            self.assertEqual([x.kind for x in actions], ["read", "merge", "write"])
        _, values = controller.execute(access(2, "read", range(8)), a + b)
        self.assertEqual(values, a + b)
        self.assertEqual(controller.metrics()["preserved_bytes"], 4)
        actions, _ = controller.execute(access(3, "write", range(8)), b + a)
        self.assertEqual(actions, [BlockAction("write", 0)])
        self.assertEqual(controller.blocks[0], b + a)
        before = deepcopy(controller.blocks)
        for bad in (access(4, "write", range(8), 1), access(4, "write", range(8), 64),
                    access(4, "erase", range(8)), access(4, "write", (0, 0)), access(4, "write", ())):
            with self.assertRaises(CandidateError): controller.execute(bad, (1,) * len(bad.valid_bytes))
            self.assertEqual(controller.blocks, before)

    def test_hand_calculated_rmw_links_merge_and_readback(self):
        task = MemorySearchTask(ssd_spec())
        self.assertEqual(set(task.default_design), {"kv_layout"})
        self.assertEqual(task.backend, "fcfs-block-system-v1")
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        metrics = task.measure(task.default_design, True)
        self.assertEqual((metrics["completion_ps"], metrics["frontend_transferred_bytes"], metrics["transferred_bytes"]), (386, 32, 48))
        self.assertEqual([g["finish_ps"] for g in metrics["group_events"]], [153, 306, 346, 386])
        s = metrics["per_target"]["s0"]
        self.assertEqual((s["read_blocks"], s["write_blocks"], s["rmw_updates"]), (4, 2, 2))
        self.assertEqual((s["service_ps"], s["host_merge_ps"], metrics["per_link"]["bus"]["busy_ps"]), (320, 6, 60))
        self.assertEqual((s["scratch_peak_bytes"], s["host_buffer_reserved_bytes"], s["preserved_bytes"]), (8, 8, 4))
        self.assertEqual(metrics["per_link"]["bus"]["transferred_bytes"], 48)
        self.assertEqual([e["phase"] for e in metrics["link_events"][:4]], ["rmw_read", "rmw_write"] * 2)
        self.assertEqual(len(metrics["media_events"]), 6)
        self.assertEqual(len(metrics["ssd_host_events"]), 2)
        self.assertNotIn("nand_operations", metrics)
        self.assertNotIn("physical_used_bytes", s)
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)

    def test_whole_block_traffic_and_configured_queue_depth(self):
        for depth, expected in ((1, 280), (2, 170)):
            spec = ssd_spec()
            spec["evaluation"]["ssd_parameters"]["s0"]["queue_depth"] = depth
            data = spec["workload"]["data"]
            data["objects"]["V"] = dict(tokens=1, dimensions=8, element_bytes=2)
            data["events"][0]["count"] = 1
            data["events"][1].update(tokens=[0, 1], dimensions=[0, 8])
            task = MemorySearchTask(spec)
            result = Evaluator(task).evaluate(task.default_design)
            m = result["metrics"]
            self.assertEqual(m["completion_ps"], expected)
            self.assertEqual(m["transferred_bytes"], 32)
            self.assertEqual(m["per_target"]["s0"]["rmw_updates"], 0)
            self.assertEqual(m["per_target"]["s0"]["peak_outstanding"], depth)

    def test_lba_base_offsets_capacity_and_same_target_regions(self):
        spec = ssd_spec()
        spec["hardware"]["regions"]["ssd"].update(base_address=16, size_bytes=8)
        spec["hardware"]["regions"]["other"] = dict(target="s0", base_address=40, size_bytes=8)
        spec["hardware"]["pools"]["memory"].append("other")
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        m = task.measure(task.default_design, True)
        self.assertEqual({e["lba"] for e in m["media_events"]}, {2})
        self.assertEqual(m["allocated_bytes_by_region"], {"ssd": 8})
        spec["workload"]["data"]["objects"]["V"]["tokens"] = 10
        with self.assertRaises(ValueError): MemorySearchTask(spec)

    def test_shared_link_and_fixed_rmw_barrier_with_dram(self):
        spec = ssd_spec()
        spec["hardware"]["targets"]["d0"] = {"module": "m0", "medium": "dram", "geometry": {
            "channels": 1, "banks_per_channel": 1, "rows": 1, "columns": 1, "granule_bytes": 8},
            "clock_period_ps": 1, "mapping_group": "dram", "links": ["bus"]}
        spec["hardware"]["regions"]["dram"] = dict(target="d0", base_address=0, size_bytes=8)
        spec["hardware"]["pools"]["memory"] = ["dram", "ssd"]
        spec["design_space"]["vary"].append("dram_address_mapping")
        data = spec["workload"]["data"]
        data["objects"]["V"] = dict(tokens=1, dimensions=6, element_bytes=2)
        data["events"][0]["count"] = 1
        data["events"][1].update(tokens=[0, 1], dimensions=[0, 6])
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        m = task.measure(task.default_design, True)
        self.assertEqual([g["finish_ps"] for g in m["group_events"]], [179, 219])
        self.assertEqual(m["per_link"]["bus"]["transferred_bytes"], 40)
        self.assertEqual([(e["target"], e["direction"]) for e in m["link_events"][:3]],
                         [("s0", "read_return"), ("s0", "write_data"), ("d0", "write_data")])

    def test_cross_block_elements_and_non_power_of_two_geometry(self):
        spec = ssd_spec()
        spec["hardware"]["targets"]["s0"]["geometry"].update(block_bytes=12)
        spec["hardware"]["targets"]["s0"]["host_buffer_bytes"] = 12
        spec["hardware"]["regions"]["ssd"]["size_bytes"] = 96
        spec["design_space"]["layout_options"].update(alignment_bytes=[24], chunk_bytes=[24])
        data = spec["workload"]["data"]
        data["objects"]["V"] = dict(tokens=1, dimensions=3, element_bytes=8)
        data["events"][0]["count"] = 1
        data["events"][1].update(tokens=[0, 1], dimensions=[0, 3])
        task = MemorySearchTask(spec)
        result = Evaluator(task).evaluate(task.default_design)
        self.assertEqual(result["metrics"]["transferred_bytes"], 48)

    def test_omitted_rmw_read_and_lost_preserved_bytes_fail_before_budget(self):
        task = MemorySearchTask(ssd_spec())
        execute = BlockExecutor.execute
        def omit_read(self, *args):
            actions, values = execute(self, *args)
            return [a for a in actions if a.kind != "read"], values
        def lose_old(previous, mask, values):
            return merge_block((None,) * len(previous), mask, values)
        for target, substitute, reason in (
                ("agent_design_search.execution.ssd.BlockExecutor.execute", omit_read, "block_action_mismatch"),
                ("agent_design_search.execution.ssd.merge_block", lose_old, "block_preservation_mismatch")):
            evaluator = Evaluator(task)
            with patch(target, substitute), self.assertRaises(CandidateError) as error:
                evaluator.evaluate(task.default_design)
            self.assertEqual(evaluator.backend_calls, 0)
            self.assertEqual(error.exception.details["verification"]["reason"], reason)

    def test_hardware_permissions_parameters_and_backend_identity(self):
        mutations = [
            lambda s: s["hardware"]["targets"]["s0"].update(mapping_group="ftl"),
            lambda s: s["hardware"]["targets"]["s0"]["geometry"].update(planes_per_die=2),
            lambda s: s["hardware"]["targets"]["s0"].update(host_buffer_bytes=7),
            lambda s: s["hardware"]["targets"]["s0"].update(max_transfer_blocks=9),
            lambda s: s["hardware"]["regions"]["ssd"].update(size_bytes=72),
            lambda s: s["hardware"]["regions"]["ssd"].update(base_address=1),
            lambda s: s["evaluation"].pop("ssd_parameters"),
            lambda s: s["evaluation"]["ssd_parameters"]["s0"].update(queue_depth=0),
            lambda s: s["evaluation"]["ssd_parameters"]["s0"].update(read_ps=True),
            lambda s: s["evaluation"].update(nand_parameters={}),
            lambda s: s["evaluation"].update(parameters={"queue_depth": 2}),
            lambda s: s["evaluation"].update(backend="fcfs-hybrid-v1"),
            lambda s: s["design_space"]["vary"].append("ssd_mapping"),
        ]
        for mutate in mutations:
            spec = ssd_spec(); mutate(spec)
            with self.subTest(spec=spec), self.assertRaises(ValueError): MemorySearchTask(spec)
        task = MemorySearchTask(ssd_spec())
        with self.assertRaises(CandidateError):
            task.normalize({**task.default_design, "ssd_mapping": {}})
        changed = task.snapshot()
        changed["evaluation"]["ssd_parameters"]["s0"]["read_ps"] += 1
        self.assertNotEqual(MemorySearchTask(changed).candidate_id(task.default_design), task.candidate_id(task.default_design))

    def test_same_workload_on_dram_raw_nand_and_ssd(self):
        task = MemorySearchTask.from_file("examples/hybrid_search.json")
        spec = task.snapshot()
        spec["hardware"]["targets"]["s0"] = {"module": "m0", "medium": "ssd", "geometry": {
            "logical_blocks": 64, "block_bytes": 512}, "max_transfer_blocks": 8, "host_buffer_bytes": 512, "links": ["host", "bus0"]}
        spec["hardware"]["regions"]["ssd"] = dict(target="s0", base_address=0, size_bytes=32768)
        spec["hardware"]["pools"]["memory"].append("ssd")
        spec["design_space"]["layout_options"].update(alignment_bytes=[512], chunk_bytes=[512, 1024])
        spec["evaluation"].update(backend="fcfs", ssd_parameters=ssd_spec()["evaluation"]["ssd_parameters"])
        task = MemorySearchTask(spec)
        for regions in (["dram0", "dram1"], ["nand0", "nand1"], ["ssd"], ["dram0", "nand0", "ssd"]):
            design = task.default_design
            design["kv_layout"]["groups"][0]["placement"]["regions"] = regions
            result = Evaluator(task).evaluate(design)
            self.assertEqual(result["verification"]["objects"], 2)
            self.assertEqual(result["metrics"]["read_useful_bytes"], 6144)
            self.assertEqual(result["metrics"]["write_useful_bytes"], 4096)

    def test_file_session_budget_cache_and_restart(self):
        task = MemorySearchTask(ssd_spec())
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "session"
            session_init(folder, task, task.budget, [("initial", task.default_design)])
            resumed = load_task(folder)
            design = resumed.default_design
            design["kv_layout"]["objects"]["V"]["tile"].update(token=2, dimension=1)
            proposal = dict(hypothesis="change block utilization", expected="charged RMW", dsl=design)
            result = submit(folder, resumed, proposal)
            self.assertEqual(result["backend_calls"], 2)
            self.assertEqual(submit(folder, resumed, proposal)["backend_calls"], 2)
            self.assertIn("best", session_finish(folder, resumed))

    def test_rtl_export_selects_only_dram_targets_in_a_pool(self):
        task = MemorySearchTask.from_file("examples/pool_search.json")
        with tempfile.TemporaryDirectory() as tmp:
            design = Path(tmp) / "design.json"
            design.write_text(json.dumps(task.default_design), encoding="utf-8")
            stdout = io.StringIO()
            args = ["agent_design_search", "export-rtl", str(design), "--task", "examples/pool_search.json", "--output", tmp]
            with patch("sys.argv", args), patch("agent_design_search.__main__.export_rtl", return_value={}), redirect_stdout(stdout):
                main()
            self.assertEqual(json.loads(stdout.getvalue()), {"mapping_group": "dram", "targets": ["d0"]})


class BlockTimingTests(unittest.TestCase):
    def engine(self, depth=2):
        target = MemorySystem(ssd_spec()["hardware"]).targets["s0"]
        return BlockTiming(target, BlockParameters(30, 100, depth, 3))

    def test_queue_depth_and_multiblock_command_limit(self):
        commands = [BlockIO(Request(i, i * 8, 0, "read"), i) for i in range(3)]
        for depth, expected, wait in ((1, 90, 90), (2, 60, 30)):
            engine = self.engine(depth)
            self.assertEqual(engine.submit_batch(commands), expected)
            self.assertEqual(engine.counts["queue_wait_ps"], wait)
            self.assertEqual(engine.counts["peak_outstanding"], depth)
        engine = self.engine()
        self.assertEqual(engine.submit_batch([BlockIO(Request(0, 0, 0, "read"), 0, 4)]), 120)
        self.assertEqual(engine.counts["transferred_bytes"], 32)
        self.assertEqual(engine.counts["read_commands"], 1)

    def test_invalid_commands_do_not_mutate_timing_state(self):
        command = BlockIO(Request(0, 0, 0, "read"), 0)
        bad = [[], [command, command], [replace(command, blocks=5)], [replace(command, lba=8)],
               [replace(command, request=replace(command.request, address=1))],
               [replace(command, request=replace(command.request, kind="erase"))],
               [replace(command, request=replace(command.request, arrival=-1))]]
        for batch in bad:
            engine = self.engine()
            with self.assertRaises(ValueError): engine.submit_batch(batch)
            self.assertFalse(engine.ids)
            self.assertFalse(engine.events)
            self.assertEqual(engine.completed, 0)

    def test_system_rejects_missing_rmw_actions_before_charging(self):
        task = MemorySearchTask(ssd_spec())
        engine = SystemFCFS(task.system, None, ssd_parameters=task.ssd_parameters)
        with self.assertRaises(ValueError):
            engine.submit_batch([("s0", access(0, "write", range(4)))], block_actions={0: [BlockAction("write", 0)]})
        self.assertEqual(engine.ready, 0)
        self.assertFalse(engine.link_events)


if __name__ == "__main__":
    unittest.main()
