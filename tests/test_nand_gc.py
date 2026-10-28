"""Foreground GC: persistent data, finite space, charged work and safe timing."""

from dataclasses import replace
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.contracts import CandidateError
from agent_design_search.execution.nand import NANDExecutor, NANDState
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.__main__ import load_task
from agent_design_search.inputs.hardware import NANDGeometry, NANDTarget
from agent_design_search.performance.nand import NANDParameters, NANDParallelTiming
from test_nand import access, timing
from test_memory_migration import migration_spec
from test_memory_allocation import epoch


def controller(channels=1, dies=1, blocks=2, pages=2):
    target = NANDTarget("m0", NANDGeometry(channels, dies, 1, blocks, pages, 8),
                        channels * dies * (blocks - 1) * pages, 4096, ())
    return NANDExecutor(target, {"axis_order": ["channel", "die"], "stripe_pages": 1}, 4, "foreground_greedy")


def gc_task():
    return MemorySearchTask.from_file("examples/nand_gc_search.json")


class ForegroundGCTests(unittest.TestCase):
    def test_hand_copy_publish_erase_cost_and_capacity(self):
        c = controller()
        engine = NANDParallelTiming(c.state.geometry, NANDParameters(**timing()), 4, c.gc)
        for request_id, logical in enumerate((0, 1, 0, 1)):
            req = access(request_id, "write", range(8), logical * 8)
            actions, _ = c.execute(req, (request_id,) * 8)
            req = replace(req, request=replace(req.request, arrival=engine.completed))
            engine.submit_batch([req], {request_id: actions})
        self.assertEqual(c.state.logical_contents(0), (2,) * 8)
        self.assertEqual(c.state.logical_contents(1), (3,) * 8)
        gc_actions = [(a.kind, a.page, a.logical_page) for a in actions if a.purpose == "gc"]
        self.assertEqual(gc_actions, [("lookup", -1, -1), ("lookup", -1, -1),
                                     ("read", 1, 1), ("program", 3, 1), ("bind", 3, 1), ("erase", 0, -1)])
        self.assertEqual(engine.completed, 843)
        self.assertEqual(engine.counts["gc_blocking_ps"], 363)  # Two scans plus one copy/erase.
        self.assertEqual((engine.counts["program_pages"], engine.counts["erases"]), (5, 1))
        metrics = c.metrics()
        self.assertEqual((metrics["gc_cycles"], metrics["gc_copied_pages"], metrics["gc_reclaimed_pages"]), (1, 1, 1))
        self.assertEqual(metrics["write_amplification"], 1.25)
        self.assertEqual(metrics["free_pages"] + metrics["invalid_pages"] + metrics["logical_bound_pages"], 4)
        self.assertEqual(engine.peak_live_pages, 3)

    def test_repeated_partial_updates_and_release_reuse_over_many_erasures(self):
        c = controller(channels=3, dies=2, blocks=3, pages=3)
        rng, contents = random.Random(7), {}
        for i in range(500):
            logical = rng.randrange(30)
            if i % 11 == 0 and logical in contents:
                c.release_range(logical * 8, 8)
                del contents[logical]
            mask = rng.sample(range(8), rng.randrange(1, 9))
            tags = tuple((i, b) for b in mask)
            c.execute(access(i, "write", mask, logical * 8), tags)
            data = contents.setdefault(logical, [None] * 8)
            for b, v in zip(mask, tags):
                data[b] = v
            for l, expected in contents.items():
                self.assertEqual(c.state.logical_contents(l), tuple(expected))
                self.assertEqual(c.state.geometry.decode(c.state.bindings[l])[:2], (l % 3, (l // 3) % 2))
            m = c.metrics()
            self.assertEqual(m["free_pages"] + m["invalid_pages"] + m["logical_bound_pages"], c.state.geometry.pages)
            self.assertFalse(c.state.pending or c.state.unpublished)
        self.assertGreater(m["gc_cycles"], 20)
        self.assertGreater(m["gc_copied_pages"], 0)

    def test_gc_reserve_is_per_die_and_does_not_borrow_another_die(self):
        c = controller(channels=2)
        for i, logical in enumerate((0, 2)):
            c.execute(access(i, "write", range(8), logical * 8), (logical,) * 8)
        before = c.state.bindings.copy(), c.state.pages.copy()
        # This direct fixture exposes an extra logical page to test per-die admission.
        c.state.target = replace(c.state.target, logical_pages=5)
        with self.assertRaises(CandidateError) as error:
            c.execute(access(2, "write", range(8), 32), (4,) * 8)
        self.assertEqual(error.exception.code, "nand_gc_reserve")
        self.assertEqual((c.state.bindings, c.state.pages), before)
        self.assertEqual(c.metrics()["free_pages"], 6)

    def test_copy_failure_never_erases_or_publishes_unfinished_data(self):
        c = controller()
        for i, logical in enumerate((0, 1, 0)):
            c.execute(access(i, "write", range(8), logical * 8), (i,) * 8)
        before = c.state.bindings.copy(), c.state.pages.copy()
        begin = NANDState.begin
        def fail_program(state, kind, page, data=()):
            if kind == "program":
                raise CandidateError("injected_program_failure", "$.execution.nand", "test")
            return begin(state, kind, page, data)
        with patch.object(NANDState, "begin", fail_program), self.assertRaises(CandidateError):
            c.execute(access(3, "write", range(8), 8), (3,) * 8)
        self.assertEqual((c.state.bindings, c.state.pages), before)
        self.assertFalse(c.state.pending or c.state.unpublished)

    def test_same_batch_reuse_waits_for_reads_and_erase_and_then_reopens_parallelism(self):
        c = controller(channels=2)
        engine = NANDParallelTiming(c.state.geometry, NANDParameters(**timing()), 4, c.gc)
        requests, chains = [], {}
        # Repeated physical pages within one batch are legal only across GC barriers.
        for i, logical in enumerate((0, 2, 0, 2, 0, 1, 3)):
            req = access(i, "write", range(8), logical * 8)
            chains[i], _ = c.execute(req, (i,) * 8)
            requests.append(req)
        engine.submit_batch(requests, chains)
        events = {e["request_id"]: e for e in engine.events}
        for i in (2, 3, 4):
            self.assertGreaterEqual(events[i]["admitted"], max(events[j]["finish"] for j in range(i)))
        self.assertLess(events[6]["admitted"], events[5]["finish"])  # Post-GC concurrency is retained.
        operations = engine.operations
        erased = [o for o in operations if o["kind"] == "erase"]
        self.assertTrue(erased)
        for erase in erased:
            reuse = [o for o in operations if o["kind"] == "program"
                     and o["physical_page"] == erase["physical_page"] and o["request_id"] >= erase["request_id"]]
            self.assertTrue(reuse)
            self.assertGreaterEqual(min(o["start_ps"] for o in reuse), erase["finish_ps"])
        self.assertEqual(engine.live_pages, len(c.state.bindings))

    def test_gc_drains_an_earlier_read_before_relocating_its_page(self):
        c = controller()
        engine = NANDParallelTiming(c.state.geometry, NANDParameters(**timing()), 4, c.gc)
        for i, logical in enumerate((0, 1, 0)):
            req = access(i, "write", range(8), logical * 8)
            actions, _ = c.execute(req, (i,) * 8)
            engine.submit_batch([replace(req, request=replace(req.request, arrival=engine.completed))], {i: actions})
        read, write = access(3, "read", range(8), 8), access(4, "write", range(8), 8)
        reads, values = c.execute(read, (1,) * 8)
        writes, _ = c.execute(write, (4,) * 8)
        reqs = [replace(a, request=replace(a.request, arrival=engine.completed)) for a in (read, write)]
        engine.submit_batch(reqs, {3: reads, 4: writes})
        self.assertEqual(values, (1,) * 8)
        read_done = next(e["finish"] for e in engine.events if e["request_id"] == 3)
        self.assertGreaterEqual(min(o["start_ps"] for o in engine.operations if o.get("purpose") == "gc"
                                    and o["request_id"] == 4), read_done)


class GCIntegrationTests(unittest.TestCase):
    def test_searchable_rules_identity_cost_and_file_budget(self):
        task = MemorySearchTask.from_file("examples/nand_gc_policy_search.json")
        measurements, identities = {}, set()
        for threshold in (1, 2):
            for victim in ("most_invalid", "oldest_reclaimable"):
                candidate = task.default_design
                candidate["nand_gc"]["targets"]["n0"].update(trigger_free_blocks=threshold, victim=victim)
                self.assertEqual(task.check(candidate)["status"], "pass")
                identities.add(task.candidate_id(candidate))
                measurements[threshold, victim] = task.measure(candidate, True)
        self.assertEqual(len(identities), 4)
        self.assertNotEqual(measurements[1, "most_invalid"]["completion_ps"],
                            measurements[2, "most_invalid"]["completion_ps"])
        self.assertEqual(measurements[1, "most_invalid"]["per_target"]["n0"]["metadata_bytes"],
                         task.nand_targets["n0"].metadata_bytes + 8 * task.nand_targets["n0"].geometry.blocks)
        with tempfile.TemporaryDirectory() as tmp:
            session_init(tmp, task, seeds=[("initial", task.default_design)])
            candidate = task.default_design
            candidate["nand_gc"]["targets"]["n0"]["victim"] = "oldest_reclaimable"
            proposal = dict(hypothesis="Change victim", expected="Explicit charged rule", dsl=candidate)
            self.assertEqual(submit(tmp, task, proposal)["backend_calls"], 2)
            self.assertTrue(submit(tmp, task, proposal)["cached"])
            candidate["nand_gc"]["targets"]["n0"]["trigger_free_blocks"] = 0
            bad = submit(tmp, task, proposal)
            self.assertEqual((bad["status"], bad["backend_calls"]), ("invalid", 2))
            self.assertEqual(session_finish(tmp, task)["backend_calls"], 2)

    def test_victim_rules_choose_different_safe_blocks(self):
        # First stale block has one invalid page, second has two. Both fit the spare block.
        victims = {}
        for rule in ("most_invalid", "oldest_reclaimable"):
            target = NANDTarget("m0", NANDGeometry(1, 1, 1, 4, 3, 8), 9, 4096, ())
            c = NANDExecutor(target, dict(axis_order=["channel", "die"], stripe_pages=1), 1,
                             "policy", dict(trigger_free_blocks=1, victim=rule))
            for i, logical in enumerate((0, 1, 2, 3, 4, 5, 0, 3, 4, 5)):
                actions, _ = c.execute(access(i, "write", range(8), logical * 8), (i,) * 8)
            victims[rule] = next(a.page for a in actions if a.kind == "erase")
            for logical, latest in {0: 6, 1: 1, 2: 2, 3: 7, 4: 8, 5: 9}.items():
                self.assertEqual(c.state.logical_contents(logical), (latest,) * 8)
        self.assertEqual(victims, {"most_invalid": 3, "oldest_reclaimable": 0})

    def test_lifecycle_example_verification_costs_and_disabled_gc_failure(self):
        task = gc_task()
        result = Evaluator(task).evaluate(task.default_design)
        self.assertEqual(result["verification"]["status"], "pass")
        self.assertEqual(task.backend, "fcfs-nand-gc-v1")
        metrics = task.measure(task.default_design, True)
        n = metrics["per_target"]["n0"]
        self.assertEqual(n["frontend_program_pages"], 36)
        self.assertGreater(n["program_pages"], task.system.targets["n0"].geometry.pages)
        self.assertGreater(n["gc_copied_pages"], 0)
        self.assertEqual(n["erases"], n["gc_cycles"])
        self.assertEqual(n["program_pages"], n["frontend_program_pages"] + n["gc_copied_pages"])
        self.assertEqual(n["write_amplification"], n["program_pages"] * 8 / metrics["write_useful_bytes"])
        self.assertEqual(n["gc_blocking_ps"], sum(o["finish_ps"] - o["start_ps"]
                         for o in metrics["nand_operations"] if o.get("purpose") == "gc"))
        self.assertEqual(metrics["frontend_transferred_bytes"], 72 * 8)  # GC never uses host links.
        self.assertEqual(metrics["per_link"]["host"]["transferred_bytes"], 72 * 8)
        slower = task.snapshot()
        slower["evaluation"]["nand_parameters"]["n0"]["erase_ps"] += 10
        slow_task = MemorySearchTask(slower)
        self.assertEqual(slow_task.measure(slow_task.default_design)["completion_ps"],
                         metrics["completion_ps"] + n["erases"] * 10)
        spec = task.snapshot()
        del spec["execution"]["nand"]["gc"]
        spec["evaluation"]["backend"] = "fcfs"
        no_gc = MemorySearchTask(spec)
        with self.assertRaises(CandidateError) as error:
            Evaluator(no_gc).evaluate(no_gc.default_design)
        self.assertEqual(error.exception.details["verification"]["reason"], "nand_lane_exhausted")

    def test_repeated_migration_and_release_across_mixed_media(self):
        for pair in (("dram", "nand"), ("nand", "nand"), ("nand", "ssd")):
            with self.subTest(pair=pair):
                spec = migration_spec(*pair)
                spec["workload"]["data"]["events"] = epoch("A") * 12 + [{"op": "end"}]
                spec["execution"]["nand"] = dict(kind="channel_die", buffer_pages=1, gc="foreground_greedy")
                spec["design_space"]["nand_options"] = {"stripe_pages": [1]}
                rules = {n: dict(axis_order=["channel", "die"], stripe_pages=1)
                         for n, t in spec["hardware"]["targets"].items() if t["medium"] == "nand"}
                spec["initial_design"]["nand_page_mapping"] = dict(version=1, targets=rules)
                task = MemorySearchTask(spec)
                result = Evaluator(task).evaluate(task.default_design)
                self.assertEqual(result["verification"]["migrations"], 12)
                self.assertEqual(result["metrics"]["allocated_bytes"], 0)
                for name in rules:
                    self.assertGreater(result["metrics"]["per_target"][name]["gc_cycles"], 0)
                    self.assertEqual(result["metrics"]["per_target"][name]["logical_bound_pages"], 0)

    def test_independent_reference_rejects_omitted_gc_operations_and_corrupt_copy(self):
        task = gc_task()
        execute = NANDExecutor.execute
        for omitted in ("read", "program", "bind", "erase"):
            def omit(self, *args):
                actions, values = execute(self, *args)
                return [a for a in actions if not (a.purpose == "gc" and a.kind == omitted)], values
            evaluator = Evaluator(task)
            with self.subTest(omitted=omitted), patch.object(NANDExecutor, "execute", omit), self.assertRaises(CandidateError) as error:
                evaluator.evaluate(task.default_design)
            self.assertEqual(error.exception.details["verification"]["reason"], "nand_action_mismatch")
            self.assertEqual(evaluator.backend_calls, 0)
        def corrupt(self, *args):
            actions, values = execute(self, *args)
            for action in actions:
                if action.purpose == "gc" and action.kind == "program":
                    self.state.pages[action.page] = ("corrupt",) * 8
            return actions, values
        with patch.object(NANDExecutor, "execute", corrupt), self.assertRaises(CandidateError) as error:
            Evaluator(task).evaluate(task.default_design)
        self.assertEqual(error.exception.details["verification"]["reason"], "nand_preservation_mismatch")

    def test_fixed_configuration_version_and_complete_file_session(self):
        task = gc_task()
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)
        for value in (None, True, {}, "background"):
            spec = task.snapshot()
            spec["execution"]["nand"]["gc"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                MemorySearchTask(spec)
        spec = task.snapshot(); spec["evaluation"]["backend"] = "fcfs-nand-policy-v1"
        with self.assertRaises(ValueError): MemorySearchTask(spec)
        spec = task.snapshot()
        spec["hardware"]["targets"]["n0"]["geometry"].update(blocks_per_plane=1, pages_per_block=8)
        with self.assertRaisesRegex(ValueError, "two blocks per die"): MemorySearchTask(spec)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "session"
            session_init(folder, task, seeds=[("initial", task.default_design)])
            resumed = load_task(folder)
            design = resumed.default_design
            design["nand_page_mapping"]["targets"]["n0"]["stripe_pages"] = 2
            proposal = {"hypothesis": "test fixed GC with different striping", "expected": "charged copying", "dsl": design}
            self.assertEqual(submit(folder, resumed, proposal)["backend_calls"], 2)
            self.assertEqual(submit(folder, resumed, proposal)["backend_calls"], 2)
            design["nand_page_mapping"]["targets"]["n0"]["stripe_pages"] = 8
            failure = submit(folder, resumed, proposal)
            self.assertEqual((failure["status"], failure["backend_calls"]), ("invalid", 2))
            self.assertIn("best", session_finish(folder, resumed))


if __name__ == "__main__":
    unittest.main()
