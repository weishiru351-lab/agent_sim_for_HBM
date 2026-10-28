"""Explicit lifetimes, atomic group allocation and cross-medium reuse."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.contracts import AllocationEvent, CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.__main__ import load_task
from agent_design_search.inputs.workload import KVWorkload
from agent_design_search.execution.allocation import GroupAllocator
from agent_design_search.execution import memory_system as memory_execution
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.execution.nand import NANDExecutor


def epoch(name):
    return [{"op": "create", "object": name}, {"op": "append", "object": name, "count": 1},
            {"op": "read", "object": name, "tokens": [0, 1], "dimensions": [0, 4], "group_by": "token"},
            {"op": "invalidate", "object": name}]


def lifecycle_spec(medium="dram", capacity=8):
    target = {"module": "m0", "medium": medium, "links": ["bus"]}
    evaluation = {"backend": "fcfs", "objective": "completion_ps", "allocation_parameters": {"create_ps": 5, "invalidate_ps": 3}}
    vary = ["kv_layout"]
    if medium == "dram":
        target.update(geometry=dict(channels=1, banks_per_channel=1, rows=1, columns=8, granule_bytes=8),
                      clock_period_ps=1, mapping_group="dram")
        vary.append("dram_address_mapping")
    elif medium == "nand":
        target.update(geometry=dict(channels=1, dies_per_channel=1, planes_per_die=1, blocks_per_plane=4,
                                    pages_per_block=2, page_bytes=8), logical_pages=4, management_bytes=128)
        evaluation["nand_parameters"] = {"t0": dict(read_ps=30, program_ps=100, erase_ps=200, transfer_ps=10,
                                                    lookup_ps=2, merge_ps=3, bind_ps=5)}
    else:
        target.update(geometry=dict(logical_blocks=8, block_bytes=8), max_transfer_blocks=4, host_buffer_bytes=8)
        evaluation["ssd_parameters"] = {"t0": dict(read_ps=30, write_ps=100, queue_depth=2, merge_ps=3)}
    return {"version": 3, "workload": {"kind": "kv_events", "data": {"version": 3,
        "objects": {n: dict(tokens=1, dimensions=4, element_bytes=2) for n in ("A", "B")},
        "events": [*epoch("A"), *epoch("B"), *epoch("A"), {"op": "end"}]}},
        "hardware": {"version": 1, "modules": ["m0"], "targets": {"t0": target},
                     "regions": {"main": dict(target="t0", base_address=0, size_bytes=capacity)},
                     "pools": {"memory": ["main"]},
                     "links": {"bus": dict(bytes_per_beat=8, beat_ps=10, latency_ps=0)}},
        "execution": {"kind": "serial_groups", "allocation": "group_first_fit"},
        "design_space": {"vary": vary, "layout_options": {"pool": "memory", "alignment_bytes": [8], "chunk_bytes": [8]}},
        "evaluation": evaluation, "budget": 3}


class LifecycleInputTests(unittest.TestCase):
    def test_round_trip_generations_and_prefix_reset(self):
        data = lifecycle_spec()["workload"]["data"]
        workload = KVWorkload.parse(data)
        self.assertEqual(workload.to_dict(), data)
        self.assertEqual(workload.written_tokens, {"A": 0, "B": 0})
        states = workload.summary()["event_states"]
        self.assertEqual([s["generation"] for s in states if s["op"] == "create"], [1, 1, 2])
        self.assertEqual(workload.summary()["logical_groups"], 6)
        self.assertEqual([g[2] for g in workload.groups()].count("invalidate"), 3)

    def test_reject_use_after_free_duplicate_create_and_unwritten_new_generation(self):
        base = lifecycle_spec()["workload"]["data"]
        cases = [
            ([*epoch("A")[:1], {"op": "create", "object": "A"}, {"op": "end"}], "invalid_lifetime"),
            ([{"op": "invalidate", "object": "A"}, {"op": "end"}], "invalid_lifetime"),
            ([*epoch("A"), epoch("A")[2], {"op": "end"}], "inactive_object"),
            ([*epoch("A"), epoch("A")[1], {"op": "end"}], "inactive_object"),
            ([*epoch("A"), epoch("A")[0], epoch("A")[2], {"op": "end"}], "invalid_read_range"),
        ]
        for events, code in cases:
            with self.subTest(code=code), self.assertRaises(CandidateError) as caught:
                KVWorkload.parse({**base, "events": events})
            self.assertEqual(caught.exception.code, code)
            self.assertIn("$.workload.events[", caught.exception.path)
        with self.assertRaises(CandidateError): KVWorkload.parse({**base, "version": 2})

    def test_fixed_execution_and_management_cost_contract(self):
        mutations = [lambda s: s["execution"].pop("allocation"),
                     lambda s: s["execution"].update(allocation="search"),
                     lambda s: s["evaluation"].pop("allocation_parameters"),
                     lambda s: s["evaluation"]["allocation_parameters"].update(create_ps=0),
                     lambda s: s["evaluation"].update(backend="fcfs-system-v1"),
                     lambda s: s["design_space"]["vary"].append("allocation")]
        for change in mutations:
            spec = lifecycle_spec(); change(spec)
            with self.assertRaises(ValueError): MemorySearchTask(spec)
        task = MemorySearchTask(lifecycle_spec())
        self.assertEqual(task.backend, "fcfs-lifecycle-v1")
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)
        static = MemorySearchTask.from_file("examples/pool_search.json").snapshot()
        static["evaluation"]["allocation_parameters"] = {"create_ps": 5, "invalidate_ps": 3}
        with self.assertRaises(ValueError): MemorySearchTask(static)


class AllocatorStateTests(unittest.TestCase):
    def test_multiregion_failure_is_atomic(self):
        allocator = GroupAllocator({"r0": 16, "r1": 8}, [(('A',), {"r1": 8}, 8), (('B',), {"r0": 8, "r1": 8}, 8)])
        allocator.apply("create", "A", 0)
        before = deepcopy(vars(allocator))
        with self.assertRaises(CandidateError) as caught: allocator.apply("create", "B", 1)
        self.assertEqual(caught.exception.code, "allocation_exhausted")
        self.assertEqual(vars(allocator), before)
        allocator.apply("invalidate", "A", 2)
        event = allocator.apply("create", "B", 3)
        self.assertEqual(event.extents, (("r0", 0, 8), ("r1", 0, 8)))

    def test_fragmentation_and_reuse_choose_lowest_aligned_gap(self):
        groups = [((name,), {"r": size}, 8) for name, size in (("A", 8), ("B", 8), ("C", 8), ("D", 16))]
        allocator = GroupAllocator({"r": 24}, groups)
        for i, name in enumerate(("A", "B", "C")): allocator.apply("create", name, i)
        allocator.apply("invalidate", "A", 3); allocator.apply("invalidate", "C", 4)
        self.assertEqual(allocator.metrics()["allocated_bytes"], 8)
        with self.assertRaises(CandidateError): allocator.apply("create", "D", 5)
        allocator.apply("invalidate", "B", 6)
        self.assertEqual(allocator.apply("create", "D", 7).extents, (("r", 0, 16),))
        self.assertEqual(allocator.metrics()["peak_allocated_bytes"], 24)

    def test_group_lease_survives_one_members_invalidation(self):
        allocator = GroupAllocator({"r": 16}, [(('A', 'B'), {"r": 16}, 8)])
        self.assertTrue(allocator.apply("create", "A", 0).changed)
        self.assertFalse(allocator.apply("create", "B", 1).changed)
        self.assertFalse(allocator.apply("invalidate", "A", 2).changed)
        with self.assertRaises(CandidateError): allocator.address("A", "r", 0)
        self.assertEqual(allocator.address("B", "r", 8), 8)
        event = allocator.apply("create", "A", 3)
        self.assertEqual((event.changed, event.generation), (False, 2))
        allocator.apply("invalidate", "B", 4)
        self.assertTrue(allocator.apply("invalidate", "A", 5).changed)
        self.assertEqual((allocator.allocations, allocator.releases), (1, 1))


class LifecycleIntegrationTests(unittest.TestCase):
    def test_dram_nand_ssd_reuse_and_hand_calculated_costs(self):
        for medium, completion in (("dram", 140), ("nand", 591), ("ssd", 474)):
            with self.subTest(medium=medium):
                task = MemorySearchTask(lifecycle_spec(medium))
                self.assertEqual(task.check(task.default_design)["status"], "pass")
                m = task.measure(task.default_design, True)
                self.assertEqual((m["completion_ps"], m["allocated_bytes"], m["peak_allocated_bytes"]), (completion, 0, 8))
                self.assertEqual((m["group_allocations"], m["group_releases"], m["allocation_management_ps"]), (3, 3, 24))
                self.assertEqual(m["peak_allocated_bytes_by_target"], {"t0": 8})
                self.assertEqual({tuple(e["extents"]) for e in m["allocation_events"]}, {(("main", 0, 8),)})
                self.assertEqual([e["generation"] for e in m["allocation_events"] if e["kind"] == "create"], [1, 1, 2])
                if medium == "nand":
                    n = m["per_target"]["t0"]
                    self.assertEqual((n["program_pages"], n["invalid_pages"], n["logical_bound_pages"], n["erases"]), (3, 3, 0, 0))
                    self.assertEqual(n["physical_used_bytes"], 24)
                    self.assertEqual(sum(e["kind"] == "unbind" for e in m["nand_operations"]), 3)
                elif medium == "ssd":
                    self.assertEqual(m["per_target"]["t0"]["written_logical_blocks"], 1)

    def test_live_capacity_failure_spends_no_evaluation(self):
        spec = lifecycle_spec()
        events = spec["workload"]["data"]["events"]
        events.insert(1, {"op": "create", "object": "B"})
        events.insert(4, {"op": "invalidate", "object": "B"})
        task = MemorySearchTask(spec)
        evaluator = Evaluator(task)
        with self.assertRaises(CandidateError) as caught: evaluator.evaluate(task.default_design)
        self.assertEqual(caught.exception.code, "allocation_exhausted")
        self.assertEqual(caught.exception.details["region"], "main")
        self.assertEqual(evaluator.backend_calls, 0)

    def test_content_survives_shared_group_recreate_on_each_medium(self):
        for medium in ("dram", "nand", "ssd"):
            spec = lifecycle_spec(medium, 16)
            spec["workload"]["data"]["events"] = [*epoch("A")[:2], *epoch("B")[:2], epoch("A")[3],
                *epoch("A")[:2], epoch("B")[2], epoch("A")[2], epoch("A")[3], epoch("B")[3], {"op": "end"}]
            task = MemorySearchTask(spec)
            separate = Evaluator(task).evaluate(task.default_design)
            self.assertEqual(separate["metrics"]["group_allocations"], 3)
            design = task.default_design
            group = design["kv_layout"]["groups"][0]
            group["composition"]["order"] = ["A", "B"]
            design["kv_layout"]["groups"] = [group]
            result = Evaluator(task).evaluate(design)
            self.assertEqual(result["verification"]["status"], "pass")
            m = result["metrics"]
            self.assertEqual((m["peak_allocated_bytes"], m["group_allocations"], m["group_releases"]), (16, 1, 1))

    def test_nand_inflight_release_is_rejected_before_binding_changes(self):
        task = MemorySearchTask(lifecycle_spec("nand"))
        controller = NANDExecutor(task.system.targets["t0"])
        state = controller.state
        token = state.begin("program", 0, ("x",) * 8)
        with self.assertRaises(CandidateError) as caught: controller.release_range(0, 8)
        self.assertEqual(caught.exception.code, "nand_busy_release")
        state.complete(token)
        with self.assertRaises(CandidateError): controller.release_range(0, 8)
        state.bind(0, 0)
        with self.assertRaises(CandidateError): controller.release_range(1, 8)
        self.assertEqual(state.bindings, {0: 0})
        self.assertEqual(controller.release_range(0, 8), (0,))
        self.assertEqual(len(state.pages), 1)
        self.assertFalse(state.bindings)

    def test_mixed_resource_example_uses_all_backends_and_reuses_capacity(self):
        from agent_design_search.json_io import read
        task = MemorySearchTask.from_file("examples/lifecycle_search.json")
        proposal = read("examples/lifecycle_layout_proposal.json")["dsl"]
        result = Evaluator(task).evaluate(proposal)
        self.assertEqual(result["verification"]["status"], "pass")
        m = result["metrics"]
        self.assertEqual((m["peak_allocated_bytes"], m["allocated_bytes"]), (128, 0))
        self.assertEqual(m["per_target"]["n0"]["program_pages"], 2)
        self.assertGreater(m["per_target"]["s0"]["rmw_updates"], 0)
        self.assertGreater(m["per_target"]["d0"]["transactions"], 0)

    def test_generation_and_allocation_record_corruption_are_detected(self):
        original = memory_execution.kv_transfers
        def stale_tags(*args, **kwargs):
            for item in original(*args, **kwargs):
                if isinstance(item, AllocationEvent):
                    yield item
                else:
                    event, name, transfers = item
                    yield event, name, [replace(t, byte_tags=tuple((n, 1, i, b) for n, _, i, b in t.byte_tags)) for t in transfers]
        def wrong_record(*args, **kwargs):
            for item in original(*args, **kwargs):
                yield replace(item, changed=not item.changed) if isinstance(item, AllocationEvent) else item
        for substitute, reason in ((stale_tags, "masked_byte_mismatch"), (wrong_record, "allocation_event_mismatch")):
            task = MemorySearchTask(lifecycle_spec())
            evaluator = Evaluator(task)
            with patch("agent_design_search.execution.memory_system.kv_transfers", substitute), self.assertRaises(CandidateError) as caught:
                evaluator.evaluate(task.default_design)
            self.assertEqual(caught.exception.details["verification"]["reason"], reason)
            self.assertEqual(evaluator.backend_calls, 0)

    def test_nand_unbinding_is_verified_and_does_not_erase_or_prevent_exhaustion(self):
        task = MemorySearchTask(lifecycle_spec("nand"))
        release = NANDExecutor.release_range
        def forget(self, address, size):
            before = dict(self.state.bindings)
            pages = release(self, address, size)
            self.state.bindings = before
            return pages
        with patch("agent_design_search.execution.nand.NANDExecutor.release_range", forget), self.assertRaises(CandidateError) as caught:
            Evaluator(task).evaluate(task.default_design)
        self.assertEqual(caught.exception.details["verification"]["reason"], "nand_release_mismatch")
        spec = lifecycle_spec("nand")
        spec["hardware"]["targets"]["t0"].update(logical_pages=1)
        spec["hardware"]["targets"]["t0"]["geometry"].update(blocks_per_plane=1)
        evaluator = Evaluator(MemorySearchTask(spec))
        with self.assertRaises(CandidateError) as caught: evaluator.evaluate(evaluator.task.default_design)
        self.assertEqual(caught.exception.details["verification"]["reason"], "nand_exhausted")
        self.assertEqual(evaluator.backend_calls, 0)

    def test_end_is_not_implicit_release_and_lifetime_identity_is_cached_safely(self):
        spec = lifecycle_spec(capacity=16)
        task = MemorySearchTask(spec)
        separate = task.default_design
        combined = task.default_design
        combined["kv_layout"]["groups"] = combined["kv_layout"]["groups"][:1]
        combined["kv_layout"]["groups"][0]["composition"]["order"] = ["A", "B"]
        self.assertNotEqual(task.candidate_id(separate), task.candidate_id(combined))
        self.assertEqual(task.measure(separate)["peak_allocated_bytes"], 8)
        self.assertEqual(task.measure(combined)["peak_allocated_bytes"], 16)
        spec["workload"]["data"]["events"].pop(-2)
        live = MemorySearchTask(spec)
        self.assertEqual(live.measure(live.default_design)["allocated_bytes"], 8)

    def test_session_restart_budget_and_semantic_state_replay(self):
        task = MemorySearchTask(lifecycle_spec("ssd", 16))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "run"
            session_init(folder, task, task.budget, [("initial", task.default_design)])
            restored = load_task(folder)
            candidate = restored.default_design
            candidate["kv_layout"]["groups"] = candidate["kv_layout"]["groups"][:1]
            candidate["kv_layout"]["groups"][0]["composition"]["order"] = ["A", "B"]
            proposal = dict(hypothesis="share a group reservation", expected="larger peak", dsl=candidate)
            self.assertEqual(submit(folder, restored, proposal)["backend_calls"], 2)
            self.assertEqual(submit(folder, restored, proposal)["backend_calls"], 2)
            self.assertIn("best", session_finish(folder, restored))


if __name__ == "__main__":
    unittest.main()
