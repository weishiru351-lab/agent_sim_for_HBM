"""Reservation timing is searchable; workload lifetimes and capacity are not."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.__main__ import load_task
from agent_design_search.execution import memory_system as memory_execution
from agent_design_search.contracts import AllocationEvent, CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.json_io import read
from agent_design_search.execution.allocation import GroupAllocator
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from test_memory_allocation import lifecycle_spec, epoch


def policy_spec(medium="dram", capacity=16):
    spec = lifecycle_spec(medium, capacity)
    spec["execution"]["allocation"] = "policy_first_fit"
    spec["design_space"]["vary"].append("allocation_policy")
    spec["evaluation"]["allocation_parameters"].update(reserve_ps=7, release_ps=11)
    return spec


def delayed(task):
    candidate = task.default_design
    candidate["allocation_policy"]["reserve_on"] = dict.fromkeys(task.bounds, "first_access")
    return candidate


def overlapping_events():
    # A is live but unused while B is used; the last B generation is never accessed.
    return [epoch("A")[0], *epoch("B"), *epoch("A")[1:], epoch("B")[0], epoch("B")[3], {"op": "end"}]


class AllocationPolicyTests(unittest.TestCase):
    def test_schema_permissions_snapshot_and_cache_identity(self):
        task = MemorySearchTask(policy_spec())
        self.assertEqual(task.backend, "fcfs-allocation-v1")
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)
        self.assertNotEqual(task.candidate_id(task.default_design), task.candidate_id(delayed(task)))
        for value in ({}, {"version": True, "reserve_on": {"A": "create", "B": "create"}},
                      {"version": 1, "reserve_on": {"A": "create"}},
                      {"version": 1, "reserve_on": {"A": "future_event", "B": "create"}},
                      {"version": 1, "reserve_on": {"A": [], "B": "create"}},
                      {"version": 1, "reserve_on": {"A": "create", "B": "create"}, "release": "early"}):
            candidate = task.default_design; candidate["allocation_policy"] = value
            with self.subTest(value=value), self.assertRaises(CandidateError) as caught: task.normalize(candidate)
            self.assertTrue(caught.exception.path.startswith("$.dsl.allocation_policy"))
        fixed = policy_spec()
        fixed["design_space"]["vary"].remove("allocation_policy")
        fixed["fixed_design"] = {"allocation_policy": delayed(task)["allocation_policy"]}
        frozen = MemorySearchTask(fixed)
        self.assertEqual(frozen.default_design["allocation_policy"], delayed(task)["allocation_policy"])
        candidate = frozen.default_design; candidate["allocation_policy"]["reserve_on"]["A"] = "create"
        with self.assertRaises(CandidateError) as caught: frozen.normalize(candidate)
        self.assertEqual(caught.exception.code, "fixed_strategy")
        legacy = MemorySearchTask(lifecycle_spec())
        with self.assertRaises(CandidateError): legacy.normalize(task.default_design)

    def test_new_profile_requires_explicit_costs_and_lifetimes(self):
        for change in (lambda s: s["evaluation"]["allocation_parameters"].pop("reserve_ps"),
                       lambda s: s["evaluation"]["allocation_parameters"].update(release_ps=0),
                       lambda s: s["evaluation"]["allocation_parameters"].update(reserve_ps=True),
                       lambda s: s["evaluation"].update(backend="fcfs-lifecycle-v1")):
            spec = policy_spec(); change(spec)
            with self.assertRaises(ValueError): MemorySearchTask(spec)
        static = MemorySearchTask.from_file("examples/module_search.json").snapshot()
        static["execution"]["allocation"] = "policy_first_fit"
        with self.assertRaises(ValueError): MemorySearchTask(static)

    def test_delayed_reservation_failure_is_atomic_and_access_requires_lease(self):
        groups = [(('A',), {"r1": 8}, 8), (('B',), {"r0": 8, "r1": 8}, 8)]
        allocator = GroupAllocator({"r0": 8, "r1": 8}, groups, {"A": "create", "B": "first_access"})
        allocator.apply("create", "A", 0)
        deferred = allocator.apply("create", "B", 1)
        self.assertEqual((deferred.changed, deferred.extents, deferred.generation), (False, (), 1))
        before = deepcopy(vars(allocator))
        with self.assertRaises(CandidateError): allocator.apply("reserve", "B", 2)
        self.assertEqual(vars(allocator), before)
        with self.assertRaises(CandidateError) as caught: allocator.address("B", "r0", 0)
        self.assertEqual(caught.exception.code, "unreserved_object")
        allocator.apply("invalidate", "A", 3)
        self.assertEqual(allocator.apply("reserve", "B", 4).extents, (("r0", 0, 8), ("r1", 0, 8)))
        self.assertEqual(allocator.generations["B"], 1)
        with self.assertRaises(CandidateError): allocator.apply("reserve", "B", 5)

    def test_same_used_epochs_charge_allocation_once_per_group_on_each_medium(self):
        for medium, old_time in (("dram", 140), ("nand", 591), ("ssd", 474)):
            task = MemorySearchTask(policy_spec(medium))
            for candidate in (task.default_design, delayed(task)):
                with self.subTest(medium=medium, candidate=candidate["allocation_policy"]):
                    self.assertEqual(task.check(candidate)["status"], "pass")
                    m = task.measure(candidate, True)
                    self.assertEqual(m["completion_ps"], old_time + 3 * (7 + 11))
                    self.assertEqual(m["allocation_management_ps"], 24 + 3 * (7 + 11))
                    self.assertEqual((m["lifetime_events"], m["group_allocations"], m["group_releases"]), (6, 3, 3))
                    for event in m["allocation_events"]:
                        if event["kind"] == "reserve": self.assertEqual(event["finish_ps"] - event["start_ps"], 7)
                    if medium == "nand":
                        self.assertEqual((m["per_target"]["t0"]["invalid_pages"], m["per_target"]["t0"]["erases"]), (3, 0))

    def test_delayed_policy_reduces_peak_and_skips_unused_generation(self):
        for medium in ("dram", "nand", "ssd"):
            spec = policy_spec(medium)
            spec["workload"]["data"]["events"] = overlapping_events()
            task = MemorySearchTask(spec)
            early, late = task.default_design, delayed(task)
            for candidate in (early, late): self.assertEqual(task.check(candidate)["status"], "pass")
            a, b = task.measure(early, True), task.measure(late, True)
            self.assertEqual((a["peak_allocated_bytes"], b["peak_allocated_bytes"]), (16, 8))
            self.assertEqual((a["group_allocations"], b["group_allocations"]), (3, 2))
            self.assertEqual((a["allocation_management_ps"], b["allocation_management_ps"]), (78, 60))
            self.assertEqual(a["transferred_bytes"], b["transferred_bytes"])
            # DRAM row state can depend on placement; use SSD's address-independent model for the exact delta.
            if medium == "ssd": self.assertEqual(a["completion_ps"] - b["completion_ps"], 18)
            self.assertEqual(b["allocation_events"][-1]["extents"], ())
            self.assertEqual(b["allocation_events"][-1]["generation"], 2)

    def test_deferred_capacity_is_checked_at_access_before_budget_charge(self):
        spec = policy_spec(capacity=8)
        spec["workload"]["data"]["events"] = overlapping_events()
        task = MemorySearchTask(spec); evaluator = Evaluator(task)
        with self.assertRaises(CandidateError) as caught: evaluator.evaluate(task.default_design)
        self.assertEqual((caught.exception.code, evaluator.backend_calls), ("allocation_exhausted", 0))
        self.assertEqual(evaluator.evaluate(delayed(task))["verification"]["status"], "pass")
        spec["workload"]["data"]["events"] = [*epoch("A")[:2], *epoch("B"), *epoch("A")[2:], {"op": "end"}]
        task = MemorySearchTask(spec); evaluator = Evaluator(task)
        with self.assertRaises(CandidateError) as caught: evaluator.evaluate(delayed(task))
        self.assertEqual((caught.exception.code, evaluator.backend_calls), ("allocation_exhausted", 0))
        self.assertEqual(caught.exception.details["workload_event"], 3)
        self.assertEqual(caught.exception.details["target"], "t0")

    def test_mixed_triggers_reserve_whole_group_and_wait_for_last_live_member(self):
        for medium in ("dram", "nand", "ssd"):
            spec = policy_spec(medium)
            spec["workload"]["data"]["events"] = overlapping_events()
            task = MemorySearchTask(spec); candidate = delayed(task)
            candidate["allocation_policy"]["reserve_on"]["B"] = "create"
            group = candidate["kv_layout"]["groups"][0]
            group["composition"]["order"] = ["A", "B"]
            candidate["kv_layout"]["groups"] = [group]
            self.assertEqual(task.check(candidate)["status"], "pass")
            m = task.measure(candidate, True)
            events = m["allocation_events"]
            self.assertEqual((events[0]["changed"], events[1]["changed"], events[2]["changed"]), (False, True, False))
            self.assertEqual((m["group_allocations"], m["group_releases"], m["peak_allocated_bytes"]), (2, 2, 16))
            self.assertNotIn("reserve", [e["kind"] for e in events])

    def test_missing_premature_or_corrupt_reservations_fail_independent_replay(self):
        task = MemorySearchTask(policy_spec()); candidate = delayed(task)
        original = memory_execution.kv_transfers
        def missing(*args, **kwargs):
            for item in original(*args, **kwargs):
                if not isinstance(item, AllocationEvent) or item.kind != "reserve": yield item
        def corrupt(*args, **kwargs):
            for item in original(*args, **kwargs):
                yield replace(item, changed=False) if isinstance(item, AllocationEvent) and item.kind == "reserve" else item
        def premature(*args, **kwargs):
            allocator = args[5]
            allocator.reserve_on = dict.fromkeys(allocator.owners, "create")
            yield from original(*args, **kwargs)
        for substitute in (missing, corrupt, premature):
            evaluator = Evaluator(task)
            with patch("agent_design_search.execution.memory_system.kv_transfers", substitute), self.assertRaises(CandidateError) as caught:
                evaluator.evaluate(candidate)
            self.assertEqual(caught.exception.details["verification"]["reason"], "allocation_event_mismatch")
            self.assertEqual(evaluator.backend_calls, 0)

    def test_unreserved_live_member_pins_group_until_invalidation(self):
        allocator = GroupAllocator({"r": 16}, [(('A', 'B'), {"r": 16}, 8)], {"A": "first_access", "B": "first_access"})
        allocator.apply("create", "A", 0); allocator.apply("create", "B", 1)
        allocator.apply("reserve", "B", 2)
        self.assertFalse(allocator.apply("invalidate", "B", 3).changed)
        self.assertEqual(allocator.metrics()["allocated_bytes"], 16)
        self.assertTrue(allocator.apply("invalidate", "A", 4).changed)

    def test_mixed_example_and_file_session_restart(self):
        spec = MemorySearchTask.from_file("examples/lifecycle_search.json").snapshot()
        spec["execution"]["allocation"] = "policy_first_fit"
        spec["design_space"]["vary"].append("allocation_policy")
        spec["evaluation"].update(backend="fcfs")
        spec["evaluation"]["allocation_parameters"].update(reserve_ps=7, release_ps=11)
        task = MemorySearchTask(spec)
        candidate = read("examples/lifecycle_layout_proposal.json")["dsl"]
        candidate["allocation_policy"] = delayed(task)["allocation_policy"]
        self.assertEqual(task.check(candidate)["status"], "pass")
        self.assertEqual(task.measure(candidate)["completion_ps"], 1898180 + 54)
        task = MemorySearchTask.from_file("examples/allocation_search.json")
        proposal = read("examples/allocation_proposal.json")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "run"
            session_init(folder, task, task.budget, [("initial", task.default_design)])
            restored = load_task(folder)
            self.assertEqual(restored.definition, task.definition)
            self.assertEqual(submit(folder, restored, proposal)["backend_calls"], 2)
            self.assertEqual(submit(folder, restored, proposal)["backend_calls"], 2)
            self.assertEqual(len(session_finish(folder, restored)["history"]), 3)


if __name__ == "__main__":
    unittest.main()
