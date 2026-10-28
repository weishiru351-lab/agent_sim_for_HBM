"""Whole-object migration: fixed plans and searchable triggers/destinations."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.execution import memory_system as memory_execution
from agent_design_search.contracts import MigrationEvent, CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.__main__ import load_task
from agent_design_search.json_io import read
from agent_design_search.execution.allocation import GroupAllocator
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.spaces.kv_layout_dsl import Layout
from test_allocation_policy import policy_spec, delayed
from test_memory_allocation import lifecycle_spec, epoch


def migration_spec(source="dram", destination="dram"):
    spec = policy_spec(source)
    design = MemorySearchTask(spec).default_design
    other = lifecycle_spec(destination)
    spec["hardware"]["targets"]["t1"] = other["hardware"]["targets"]["t0"]
    spec["hardware"]["regions"]["cold"] = dict(target="t1", base_address=0, size_bytes=16)
    spec["hardware"]["pools"]["memory"].append("cold")
    for key in ("nand_parameters", "ssd_parameters"):
        if key in other["evaluation"]:
            spec["evaluation"].setdefault(key, {})["t1"] = other["evaluation"][key]["t0"]
    if destination == "dram" and source != "dram":
        spec["design_space"]["vary"].append("dram_address_mapping")
        design["dram_address_mapping"] = {"version": 1, "groups": {"dram": {"version": 0, "operations": []}}}
    spec["workload"]["data"]["events"] = [*epoch("A"), {"op": "end"}]
    spec["execution"]["migration"] = {"on_full": {"A": "cold"}, "buffer_bytes": 8}
    spec["evaluation"]["migration_parameters"] = {"publish_ps": 13, "copy_ps_per_byte": 1}
    spec["initial_design"] = design
    return spec


def migration_policy_spec(source="dram", destination="dram"):
    spec = migration_spec(source, destination)
    spec["execution"]["migration"] = {"kind": "policy", "buffer_bytes": 8}
    spec["design_space"]["vary"].append("migration_policy")
    spec["design_space"]["migration_options"] = {"triggers": ["on_full", "before_read"], "destinations": ["cold"]}
    spec["initial_design"]["migration_policy"] = {"version": 1, "objects": {
        "A": {"trigger": "never"}, "B": {"trigger": "never"}}}
    return spec


def moving(task, trigger, destination="cold"):
    candidate = task.default_design
    candidate["migration_policy"]["objects"]["A"] = {"trigger": trigger, "destination": destination}
    return candidate


class MigrationTests(unittest.TestCase):
    def test_all_medium_pairs_have_hand_costs_and_data_after_move(self):
        costs = {("dram", "dram"): 153, ("dram", "nand"): 298, ("dram", "ssd"): 259,
                 ("nand", "dram"): 298, ("nand", "nand"): 443, ("nand", "ssd"): 404,
                 ("ssd", "dram"): 259, ("ssd", "nand"): 404, ("ssd", "ssd"): 365}
        for pair, cost in costs.items():
            with self.subTest(pair=pair):
                task = MemorySearchTask(migration_spec(*pair)); candidate = task.default_design
                check = task.check(candidate)
                self.assertEqual((check["status"], check["migrations"], check["copied_bytes"]), ("pass", 1, 8))
                m = task.measure(candidate, True)
                self.assertEqual(m["completion_ps"], cost)
                self.assertEqual((m["peak_allocated_bytes"], m["allocated_bytes"], m["migration_buffer_peak_bytes"]), (16, 0, 8))
                self.assertEqual((m["allocation_management_ps"], m["migration_management_ps"]), (26, 39))
                self.assertEqual((m["copied_bytes"], m["migration_frontend_bytes"], m["transferred_bytes"]), (8, 16, 32))
                self.assertEqual([s["target"] for s in m["request_sources"]], ["t0", "t0", "t1", "t1"])
                self.assertEqual([s["purpose"] for s in m["request_sources"]], ["workload", "migration", "migration", "workload"])
                for source in m["request_sources"]:
                    medium = pair[0 if source["target"] == "t0" else 1]
                    self.assertEqual(source["address_space"], f"{medium}_target_bytes" if medium == "dram" else f"{medium}_logical_bytes")
                    self.assertEqual("dram_location" in source, medium == "dram")
                    self.assertEqual(source["byte_origins"][0]["index"], [0, 0])
                    self.assertEqual(source["byte_origins"][0]["object_offset"], 0)
                events = m["migration_events"]
                self.assertEqual([e["kind"] for e in events], ["reserve", "buffer_copy", "publish", "release"])
                copy_finish = next(e["finish_ps"] for e in m["group_events"] if e["group"] == 2)
                self.assertEqual(events[2]["start_ps"], copy_finish)
                self.assertEqual(events[3]["start_ps"], events[2]["finish_ps"])
                for target, medium in zip(("t0", "t1"), pair):
                    if medium == "nand":
                        self.assertEqual((m["per_target"][target]["invalid_pages"], m["per_target"][target]["erases"]), (1, 0))

    def test_reserve_publish_release_and_abort_keep_source_owned(self):
        allocator = GroupAllocator({"hot": 8, "cold": 8}, [(('A',), {"hot": 8}, 8)])
        allocator.apply("create", "A", 0)
        allocator.move("reserve", "A", "cold", 1)
        self.assertEqual(allocator.leases[0], (("hot", 0, 8),))
        self.assertEqual(allocator.usage(), {"hot": 8, "cold": 8})
        with self.assertRaises(CandidateError): allocator.move("release", "A", "cold", 1)
        with self.assertRaises(CandidateError): allocator.apply("invalidate", "A", 2)
        allocator.abort_move("A")
        self.assertEqual(allocator.usage(), {"hot": 8, "cold": 0})
        allocator.move("reserve", "A", "cold", 1)
        allocator.move("publish", "A", "cold", 1)
        self.assertEqual(allocator.leases[0], (("cold", 0, 8),))
        self.assertEqual(allocator.usage(), {"hot": 8, "cold": 8})
        allocator.move("release", "A", "cold", 1)
        self.assertEqual(allocator.usage(), {"hot": 0, "cold": 8})
        allocator.apply("invalidate", "A", 2)
        self.assertFalse(allocator.relocations)
        allocator.apply("create", "A", 3)
        self.assertEqual(allocator.leases[0], (("hot", 0, 8),))

    def test_full_destination_rejected_before_budget_and_without_half_reservation(self):
        spec = migration_spec()
        spec["hardware"]["regions"]["cold"]["size_bytes"] = 8
        spec["initial_design"]["kv_layout"]["groups"][1]["placement"]["regions"] = ["cold"]
        spec["workload"]["data"]["events"].insert(0, epoch("B")[0])
        task = MemorySearchTask(spec); evaluator = Evaluator(task)
        with self.assertRaises(CandidateError) as caught: evaluator.evaluate(task.default_design)
        self.assertEqual((caught.exception.code, evaluator.backend_calls), ("allocation_exhausted", 0))
        self.assertEqual(caught.exception.details["target"], "t1")
        allocator = GroupAllocator({"hot": 8, "cold": 8}, [(('A',), {"hot": 8}, 8), (('B',), {"cold": 8}, 8)])
        allocator.apply("create", "A", 0); allocator.apply("create", "B", 1)
        before = deepcopy(vars(allocator))
        with self.assertRaises(CandidateError): allocator.move("reserve", "A", "cold", 2)
        self.assertEqual(vars(allocator), before)

    def test_generation_recreation_and_delayed_allocation_migrate_again(self):
        spec = migration_spec("nand", "ssd")
        spec["workload"]["data"]["events"] = [*epoch("A"), *epoch("A"), {"op": "end"}]
        task = MemorySearchTask(spec); candidate = delayed(task)
        self.assertEqual(task.check(candidate)["status"], "pass")
        m = task.measure(candidate, True)
        self.assertEqual((m["migrations"], m["copied_bytes"], m["peak_allocated_bytes"]), (2, 16, 16))
        self.assertEqual([e["generation"] for e in m["migration_events"] if e["kind"] == "publish"], [1, 2])
        self.assertEqual([s["target"] for s in m["request_sources"] if s["purpose"] == "workload"], ["t0", "t1", "t0", "t1"])

    def test_partial_object_does_not_migrate_and_end_keeps_destination(self):
        spec = migration_spec()
        spec["workload"]["data"]["objects"]["A"]["tokens"] = 2
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["migrations"], 0)
        self.assertEqual(task.measure(task.default_design)["migrations"], 0)
        spec = migration_spec(); spec["workload"]["data"]["events"].pop(-2)
        task = MemorySearchTask(spec)
        m = task.measure(task.default_design)
        self.assertEqual(m["allocated_bytes_by_region"], {"main": 0, "cold": 8})

    def test_released_source_reuse_preserves_another_migrated_object(self):
        spec = migration_spec("dram", "nand")
        spec["execution"]["migration"]["on_full"]["B"] = "cold"
        spec["workload"]["data"]["events"] = [*epoch("A")[:2], *epoch("B")[:2],
            epoch("A")[2], epoch("B")[2], epoch("A")[3], epoch("B")[3], {"op": "end"}]
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        m = task.measure(task.default_design, True)
        self.assertEqual((m["migrations"], m["peak_allocated_bytes"]), (2, 24))
        moves = [e for e in m["migration_events"] if e["kind"] == "reserve"]
        self.assertEqual([e["source"][1] for e in moves], [0, 0])
        self.assertEqual([e["destination"][1] for e in moves], [0, 8])

    def test_nand_copy_exhaustion_cannot_publish_or_erase_live_source(self):
        spec = migration_spec("dram", "nand")
        spec["hardware"]["targets"]["t1"]["geometry"]["blocks_per_plane"] = 1
        spec["hardware"]["targets"]["t1"]["logical_pages"] = 1
        spec["hardware"]["regions"]["cold"]["size_bytes"] = 8
        spec["workload"]["data"]["events"] = [*epoch("A"), *epoch("A"), *epoch("A"), {"op": "end"}]
        task = MemorySearchTask(spec); evaluator = Evaluator(task)
        prepare, states = memory_execution.prepare_layout, []
        def capture(*args, **kwargs):
            offsets, allocator = prepare(*args, **kwargs)
            states.append(allocator)
            return offsets, allocator
        with patch("agent_design_search.execution.memory_system.prepare_layout", capture), self.assertRaises(CandidateError) as caught:
            evaluator.evaluate(task.default_design)
        self.assertEqual(caught.exception.details["verification"]["reason"], "nand_exhausted")
        self.assertEqual(evaluator.backend_calls, 0)
        self.assertEqual(states[0].leases[0], (("main", 0, 8),))
        self.assertFalse(states[0].moves)
        self.assertFalse(states[0].relocations)

    def test_buffer_chunks_padding_and_different_transfer_granules(self):
        for source, destination in (("dram", "nand"), ("nand", "ssd"), ("ssd", "dram")):
            spec = migration_spec(source, destination)
            for r in spec["hardware"]["regions"].values(): r["size_bytes"] = 32
            spec["workload"]["data"]["objects"]["A"] = dict(tokens=3, dimensions=3, element_bytes=2)
            events = spec["workload"]["data"]["events"]
            events[1]["count"] = 3; events[2].update(tokens=[0, 3], dimensions=[0, 3])
            spec["initial_design"]["kv_layout"]["objects"]["A"] = Layout(2, 2).to_dict()
            task = MemorySearchTask(spec)
            self.assertEqual(task.check(task.default_design)["copied_bytes"], 18)
            m = task.measure(task.default_design, True)
            self.assertEqual((m["copied_bytes"], m["peak_allocated_bytes"], m["migration_buffer_peak_bytes"]), (18, 64, 8))
            self.assertEqual(sum(e["kind"] == "buffer_copy" for e in m["migration_events"]), 4)
        spec = migration_spec("dram", "ssd")
        spec["hardware"]["targets"]["t1"]["geometry"]["block_bytes"] = 16
        spec["hardware"]["targets"]["t1"]["host_buffer_bytes"] = 16
        spec["workload"]["data"]["objects"]["A"]["tokens"] = 2
        spec["workload"]["data"]["events"][1]["count"] = 2
        spec["execution"]["migration"]["buffer_bytes"] = 16
        spec["design_space"]["layout_options"].update(alignment_bytes=[16], chunk_bytes=[16])
        for g in spec["initial_design"]["kv_layout"]["groups"]:
            g["composition"]["alignment_bytes"] = 16; g["placement"]["stripe_bytes"] = 16
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["copied_bytes"], 16)
        self.assertEqual(task.measure(task.default_design)["migration_frontend_bytes"], 32)

    def test_corrupt_copy_missing_copy_early_publish_and_old_binding_are_detected(self):
        task = MemorySearchTask(migration_spec()); candidate = task.default_design
        original = memory_execution.kv_transfers
        def corrupt(*args, **kwargs):
            for item in original(*args, **kwargs):
                if isinstance(item, tuple) and item[2][0].access.request.request_id == 2:
                    e, n, transfers = item
                    transfers = [replace(t, byte_tags=(('wrong',),) * len(t.byte_tags)) for t in transfers]
                    item = e, n, transfers
                yield item
        def missing(*args, **kwargs):
            for item in original(*args, **kwargs):
                if not isinstance(item, tuple) or item[2][0].access.request.request_id != 2: yield item
        def early(*args, **kwargs):
            for item in original(*args, **kwargs):
                if isinstance(item, MigrationEvent) and item.kind == "reserve": yield replace(item, kind="publish")
                yield item
        def old_binding(*args, **kwargs):
            for item in original(*args, **kwargs):
                if isinstance(item, MigrationEvent) and item.kind == "publish": args[5].relocations.clear()
                yield item
        for substitute in (corrupt, missing, early, old_binding):
            evaluator = Evaluator(task)
            with patch("agent_design_search.execution.memory_system.kv_transfers", substitute), self.assertRaises(CandidateError):
                evaluator.evaluate(candidate)
            self.assertEqual(evaluator.backend_calls, 0)

    def test_plan_permissions_and_backend_identity(self):
        task = MemorySearchTask(migration_spec())
        self.assertEqual(task.backend, "fcfs-migration-v1")
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)
        for change in (lambda s: s["execution"]["migration"].update(buffer_bytes=4),
                       lambda s: s["execution"]["migration"]["on_full"].update(A="unknown"),
                       lambda s: s["execution"]["migration"]["on_full"].update(C="cold"),
                       lambda s: s["execution"]["migration"].update(after_event=2),
                       lambda s: s["evaluation"].pop("migration_parameters"),
                       lambda s: s["evaluation"]["migration_parameters"].update(copy_ps_per_byte=0),
                       lambda s: s["evaluation"].update(backend="fcfs-allocation-v1")):
            spec = migration_spec(); change(spec)
            with self.assertRaises(ValueError): MemorySearchTask(spec)
        for regions, members in ((["main", "cold"], ["A"]), (["cold"], ["A"]), (["main"], ["A", "B"])):
            candidate = task.default_design
            candidate["kv_layout"]["groups"][0]["placement"]["regions"] = regions
            candidate["kv_layout"]["groups"][0]["composition"]["order"] = members
            if len(members) > 1: candidate["kv_layout"]["groups"].pop()
            with self.assertRaises(CandidateError) as caught: task.normalize(candidate)
            self.assertEqual(caught.exception.code, "unsupported_migration_layout")
        other = task.snapshot(); other["execution"]["migration"]["buffer_bytes"] = 16
        self.assertNotEqual(MemorySearchTask(other).definition, task.definition)

    def test_example_file_session_and_restart(self):
        task = MemorySearchTask.from_file("examples/migration_search.json")
        proposal = read("examples/migration_proposal.json")
        self.assertEqual(task.measure(task.default_design)["completion_ps"], 114)
        self.assertEqual(task.measure(proposal["dsl"])["completion_ps"], 342)
        early = {**proposal, "dsl": moving(task, "on_full")}
        self.assertEqual(task.measure(early["dsl"])["completion_ps"], 536)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "run"
            session_init(folder, task, task.budget, [("initial", task.default_design)])
            restored = load_task(folder)
            self.assertEqual(restored.definition, task.definition)
            self.assertEqual(submit(folder, restored, proposal)["backend_calls"], 2)
            self.assertEqual(submit(folder, restored, proposal)["backend_calls"], 2)
            self.assertEqual(submit(folder, restored, early)["backend_calls"], 3)
            self.assertTrue(session_finish(folder, restored)["closed"])


class MigrationPolicyTests(unittest.TestCase):
    def test_policy_schema_permissions_and_frozen_identity(self):
        task = MemorySearchTask(migration_policy_spec())
        self.assertEqual(task.backend, "fcfs-migration-policy-v1")
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)
        ids = {task.candidate_id(c) for c in (task.default_design, moving(task, "on_full"), moving(task, "before_read"))}
        self.assertEqual(len(ids), 3)
        bad_rules = [None, [], {"trigger": []}, {"trigger": "never", "destination": "cold"},
                     {"trigger": "on_full"}, {"trigger": "before_read", "destination": []},
                     {"trigger": "on_full", "destination": "unknown"},
                     {"trigger": "event_2", "destination": "cold"},
                     {"trigger": "before_read", "destination": "cold", "repeat": True}]
        for rule in bad_rules:
            candidate = task.default_design; candidate["migration_policy"]["objects"]["A"] = rule
            evaluator = Evaluator(task)
            with self.subTest(rule=rule), self.assertRaises(CandidateError) as caught: evaluator.evaluate(candidate)
            self.assertEqual(caught.exception.code, "invalid_migration_policy")
            self.assertEqual(caught.exception.path, "$.dsl.migration_policy.objects.A")
            self.assertEqual(evaluator.backend_calls, 0)
        for policy in ({"version": True, "objects": {}}, {"version": 1, "objects": {"A": {"trigger": "never"}}}):
            candidate = task.default_design; candidate["migration_policy"] = policy
            with self.assertRaises(CandidateError): task.normalize(candidate)
        fixed = task.snapshot()
        fixed["design_space"]["vary"].remove("migration_policy")
        fixed["fixed_design"]["migration_policy"] = task.default_design["migration_policy"]
        frozen = MemorySearchTask(fixed)
        with self.assertRaises(CandidateError) as caught: frozen.normalize(moving(task, "on_full"))
        self.assertEqual(caught.exception.code, "fixed_strategy")
        with self.assertRaises(CandidateError): MemorySearchTask(migration_spec()).normalize(task.default_design)
        changed = task.snapshot(); changed["design_space"]["migration_options"]["triggers"] = ["on_full"]
        restricted = MemorySearchTask(changed)
        self.assertNotEqual(restricted.definition, task.definition)
        with self.assertRaises(CandidateError): restricted.normalize(moving(task, "before_read"))

    def test_task_rejects_unsupported_options_profiles_and_missing_costs(self):
        changes = [lambda s: s["design_space"].pop("migration_options"),
                   lambda s: s["design_space"]["migration_options"].update(destinations=["outside_pool"]),
                   lambda s: s["design_space"]["migration_options"].update(destinations=["cold", "cold"]),
                   lambda s: s["design_space"]["migration_options"].update(triggers=["never"]),
                   lambda s: s["design_space"]["migration_options"].update(triggers=[[]]),
                   lambda s: s["execution"]["migration"].update(on_full={"A": "cold"}),
                   lambda s: s["execution"]["migration"].update(buffer_bytes=True),
                   lambda s: s["evaluation"].pop("migration_parameters"),
                   lambda s: s["evaluation"].update(backend="fcfs-migration-v1")]
        for change in changes:
            spec = migration_policy_spec(); change(spec)
            with self.assertRaises(ValueError): MemorySearchTask(spec)
        spec = migration_spec(); spec["design_space"]["migration_options"] = {"destinations": ["cold"], "triggers": ["on_full"]}
        with self.assertRaises(ValueError): MemorySearchTask(spec)

    def test_never_has_zero_migration_cost_and_all_media_share_execution(self):
        for source in ("dram", "nand", "ssd"):
            for destination in ("dram", "nand", "ssd"):
                with self.subTest(source=source, destination=destination):
                    spec = migration_policy_spec(source, destination); task = MemorySearchTask(spec)
                    base = deepcopy(spec)
                    del base["execution"]["migration"], base["evaluation"]["migration_parameters"]
                    del base["design_space"]["migration_options"], base["initial_design"]["migration_policy"]
                    base["design_space"]["vary"].remove("migration_policy")
                    static = MemorySearchTask(base)
                    zero = task.measure(task.default_design)
                    self.assertEqual(task.check(task.default_design)["migrations"], 0)
                    self.assertTrue(all(zero[k] == 0 for k in ("migrations", "copied_bytes", "migration_frontend_bytes",
                                                              "migration_management_ps", "migration_buffer_peak_bytes")))
                    old = static.measure(static.default_design)
                    for key in ("completion_ps", "transferred_bytes", "peak_allocated_bytes", "allocated_bytes"):
                        self.assertEqual(zero[key], old[key])
                    fixed = MemorySearchTask(migration_spec(source, destination))
                    expected_time = fixed.measure(fixed.default_design)["completion_ps"]
                    for trigger, event in (("on_full", 1), ("before_read", 2)):
                        candidate = moving(task, trigger)
                        self.assertEqual(task.check(candidate)["status"], "pass")
                        m = task.measure(candidate, True)
                        self.assertEqual((m["completion_ps"], m["migrations"], m["copied_bytes"]), (expected_time, 1, 8))
                        self.assertTrue(all(e["source_event"] == event for e in m["migration_events"]))

    def test_never_allows_shared_and_striped_layouts_but_active_moves_do_not(self):
        spec = migration_policy_spec(); spec.pop("initial_design")
        default = MemorySearchTask(spec)
        self.assertEqual(default.check(default.default_design)["status"], "pass")
        task = MemorySearchTask(migration_policy_spec())
        candidate = task.default_design
        group = candidate["kv_layout"]["groups"][0]
        group["composition"]["order"] = ["A", "B"]
        group["placement"]["regions"] = ["main", "cold"]
        candidate["kv_layout"]["groups"] = [group]
        self.assertEqual(task.check(task.normalize(candidate))["status"], "pass")
        candidate["migration_policy"] = moving(task, "before_read")["migration_policy"]
        with self.assertRaises(CandidateError) as caught: task.normalize(candidate)
        self.assertEqual((caught.exception.code, caught.exception.path),
                         ("unsupported_migration_layout", "$.dsl.kv_layout.groups[0]"))

    def test_partial_reads_precede_move_and_generation_resets_once(self):
        spec = migration_policy_spec("nand", "ssd")
        spec["workload"]["data"]["objects"]["A"]["tokens"] = 2
        create, append, read, invalidate = epoch("A")
        full_read = {**read, "tokens": [0, 2]}  # Two groups must trigger only one move.
        generation = [create, append, read, append, full_read, full_read, invalidate]
        spec["workload"]["data"]["events"] = [*generation, *generation, {"op": "end"}]
        task = MemorySearchTask(spec); candidate = moving(task, "before_read")
        candidate["allocation_policy"]["reserve_on"]["A"] = "first_access"
        self.assertEqual(task.check(candidate)["migrations"], 2)
        m = task.measure(candidate, True)
        self.assertEqual((m["migrations"], m["copied_bytes"], m["allocated_bytes"]), (2, 32, 0))
        moves = [e for e in m["migration_events"] if e["kind"] == "reserve"]
        self.assertEqual([e["source_event"] for e in moves], [4, 11])
        self.assertEqual([e["generation"] for e in moves], [1, 2])
        reads = [s["target"] for s in m["request_sources"] if s["purpose"] == "workload"]
        self.assertEqual(reads, ["t0", "t0", "t0", "t1", "t1", "t1", "t1"] * 2)

    def test_before_read_skips_full_unread_objects_and_respects_end(self):
        for ends_live in (True, False):
            spec = migration_policy_spec()
            events = epoch("A"); events.pop(2)
            if ends_live: events.pop()
            spec["workload"]["data"]["events"] = [*events, {"op": "end"}]
            task = MemorySearchTask(spec)
            for trigger, count in (("on_full", 1), ("before_read", 0)):
                candidate = moving(task, trigger)
                self.assertEqual(task.check(candidate)["migrations"], count)
                m = task.measure(candidate)
                self.assertEqual((m["migrations"], m["copied_bytes"]), (count, count * 8))
                self.assertEqual(m["allocated_bytes_by_region"],
                                 {"main": 8 * (1 - count) if ends_live else 0, "cold": 8 * count if ends_live else 0})

    def test_trigger_changes_capacity_feasibility_without_changing_lifetimes(self):
        spec = migration_policy_spec()
        spec["hardware"]["regions"]["cold"]["size_bytes"] = 8
        spec["initial_design"]["kv_layout"]["groups"][1]["placement"]["regions"] = ["cold"]
        spec["workload"]["data"]["events"] = [*epoch("B")[:2], *epoch("A")[:2], epoch("B")[3], *epoch("A")[2:], {"op": "end"}]
        task = MemorySearchTask(spec); evaluator = Evaluator(task)
        with self.assertRaises(CandidateError) as caught: evaluator.evaluate(moving(task, "on_full"))
        self.assertEqual((caught.exception.code, evaluator.backend_calls), ("allocation_exhausted", 0))
        self.assertEqual(caught.exception.details["workload_event"], 3)
        result = evaluator.evaluate(moving(task, "before_read"))
        self.assertEqual((result["verification"]["status"], result["metrics"]["migrations"]), ("pass", 1))
        # Delaying can also hurt: on_full frees the source before B needs it.
        spec["hardware"]["regions"]["main"]["size_bytes"] = 8
        spec["initial_design"]["kv_layout"]["groups"][1]["placement"]["regions"] = ["main"]
        spec["workload"]["data"]["events"] = [*epoch("A")[:2], *epoch("B"), *epoch("A")[2:], {"op": "end"}]
        task = MemorySearchTask(spec); evaluator = Evaluator(task)
        with self.assertRaises(CandidateError): evaluator.evaluate(moving(task, "before_read"))
        self.assertEqual(evaluator.backend_calls, 0)
        self.assertEqual(evaluator.evaluate(moving(task, "on_full"))["verification"]["status"], "pass")

    def test_destination_is_candidate_controlled_and_in_cache_identity(self):
        spec = migration_policy_spec("dram", "nand")
        spec["hardware"]["regions"]["warm"] = dict(target="t0", base_address=16, size_bytes=16)
        spec["hardware"]["pools"]["memory"].append("warm")
        spec["design_space"]["migration_options"]["destinations"].append("warm")
        task = MemorySearchTask(spec); evaluator = Evaluator(task)
        candidates = [moving(task, "before_read", r) for r in ("cold", "warm")]
        self.assertNotEqual(*(task.candidate_id(c) for c in candidates))
        for candidate, target in zip(candidates, ("t1", "t0")):
            result = evaluator.evaluate(candidate)
            self.assertEqual(result["verification"]["status"], "pass")
            self.assertEqual(task.measure(candidate, True)["request_sources"][-1]["target"], target)
        self.assertEqual(evaluator.backend_calls, 2)
        self.assertTrue(evaluator.evaluate(candidates[0])["cached"])

    def test_independent_reference_detects_earlier_or_missing_trigger(self):
        task = MemorySearchTask(migration_policy_spec()); candidate = moving(task, "before_read")
        original = memory_execution.kv_transfers
        for trigger in ("on_full", "never"):
            def wrong_timing(*args, **kwargs):
                changed = list(args); plan = deepcopy(changed[6])
                if trigger == "never": plan["rules"] = {}
                else: plan["rules"]["A"]["trigger"] = trigger
                changed[6] = plan
                yield from original(*changed, **kwargs)
            evaluator = Evaluator(task)
            with patch("agent_design_search.execution.memory_system.kv_transfers", wrong_timing), self.assertRaises(CandidateError) as caught:
                evaluator.evaluate(candidate)
            self.assertEqual(caught.exception.code, "verification_failed")
            self.assertEqual(evaluator.backend_calls, 0)


if __name__ == "__main__":
    unittest.main()
