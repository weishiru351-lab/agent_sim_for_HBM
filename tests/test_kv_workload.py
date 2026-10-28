import unittest

from agent_design_search.contracts import CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.spaces.kv_layout_dsl import Layout
from experiments.legacy_cases import KVLayoutTask, LayoutConfig, regular_workload
from agent_design_search.inputs.workload import KVWorkload
from experiments.legacy_cases import verify_contents
from agent_design_search.exploration.memory_search_task import MemorySearchTask


def layout_task(workload, cfg):
    return MemorySearchTask({"version": 1, "workload": {"kind": "kv_events", "data": workload},
        "hardware": {"channels": 1, "banks_per_channel": 1, "rows": 1,
                     "columns": cfg.capacity_bytes // cfg.line_bytes, "granule_bytes": cfg.line_bytes},
        "execution": {"kind": "serial_groups"}, "design_space": {"vary": ["kv_layout"]},
        "evaluation": {"backend": "line-traffic-v1", "objective": "transferred_bytes"}})


def design(task, layout):
    return {**task.default_design, "kv_layout": layout.to_dict()}


def fixture(group_by="dimension"):
    return {"version": 1, "tensor": {"tokens": 4, "dimensions": 4, "element_bytes": 2}, "events": [
        {"op": "append", "count": 2},
        {"op": "read", "tokens": [0, 2], "dimensions": [0, 4], "group_by": group_by},
        {"op": "end"}]}


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        self.cfg = LayoutConfig(tokens=4, dimensions=4, line_bytes=8, capacity_bytes=32)

    def test_grouping_changes_transfers_without_changing_logical_elements(self):
        row_task = layout_task(fixture("token"), self.cfg)
        column_task = layout_task(fixture("dimension"), self.cfg)
        for task in (row_task, column_task):
            actual_reads = [i for _, _, kind, indices in task.workload.groups() if kind == "read" for i in indices]
            self.assertEqual(sorted(actual_reads), list(range(8)))
        policies = [design(row_task, Layout(1, 4)), design(row_task, Layout(4, 1))]
        self.assertEqual([Evaluator(row_task).evaluate(p)["metrics"]["transferred_bytes"] for p in policies], [32, 128])
        self.assertEqual([Evaluator(column_task).evaluate(p)["metrics"]["transferred_bytes"] for p in policies], [80, 96])
        self.assertNotEqual(row_task.candidate_id(policies[0]), column_task.candidate_id(policies[0]))

    def test_ranges_and_early_end_do_not_manufacture_missing_tokens(self):
        obj = fixture()
        obj["events"][1].update(tokens=[1, 2], dimensions=[1, 3])
        workload = KVWorkload.parse(obj)
        groups = list(workload.groups())
        self.assertEqual(groups, [(0, "V", "write", (0, 1, 2, 3)), (0, "V", "write", (4, 5, 6, 7)),
                                  (1, "V", "read", (5,)), (1, "V", "read", (6,))])
        task = layout_task(workload.to_dict(), self.cfg)
        result = Evaluator(task).evaluate(design(task, Layout(2, 2)))
        self.assertEqual(result["verification"]["status"], "pass")
        self.assertEqual(result["metrics"]["write_useful_bytes"], 16)
        self.assertEqual(result["metrics"]["read_useful_bytes"], 4)
        self.assertEqual(result["metrics"]["allocated_bytes"], 32)  # Full static reservation, even on early end.
        self.assertEqual(verify_contents(Layout(2, 2), self.cfg, [(k, i) for _, _, k, i in groups[1:]], 2)["status"], "fail")

    def test_states_are_derived_and_inputs_cannot_mutate_the_workload(self):
        obj = fixture()
        obj["events"].insert(2, {"op": "append", "count": 2})
        workload = KVWorkload.parse(obj)
        self.assertEqual([e["state_after"] for e in workload.summary()["event_states"]], ["partial", "partial", "full", "ended"])
        obj["events"][0]["count"] = 99
        exported = workload.to_dict()
        exported["events"][1]["tokens"][1] = 99
        self.assertEqual(workload.written_tokens, {"V": 4})
        self.assertEqual(workload.to_dict()["events"][1]["tokens"], [0, 2])

    def test_rejects_illegal_events_with_precise_paths(self):
        cases = [
            ([{"op": "read", "tokens": [0, 1], "dimensions": [0, 4], "group_by": "dimension"}, {"op": "end"}], "invalid_read_range", "events[0].tokens"),
            ([{"op": "append", "count": 5}, {"op": "end"}], "append_overflow", "events[0].count"),
            ([{"op": "append", "count": True}, {"op": "end"}], "append_overflow", "events[0].count"),
            ([{"op": "append", "count": 1}], "incomplete_workload", "events"),
            ([{"op": "append", "count": 1}, {"op": "end"}, {"op": "append", "count": 1}], "event_after_end", "events[2]"),
            ([{"op": "append", "count": 1, "phase": "full"}, {"op": "end"}], "invalid_event", "events[0]"),
        ]
        for events, code, path in cases:
            with self.subTest(code=code, events=events), self.assertRaises(CandidateError) as error:
                KVWorkload.parse({**fixture(), "events": events})
            self.assertEqual((error.exception.code, error.exception.path), (code, "$.workload." + path))
        for field, value in (("tokens", [0, 3]), ("tokens", [False, 2]), ("dimensions", [3, 3]), ("dimensions", [0, 5]), ("group_by", [])):
            obj = fixture()
            obj["events"][1][field] = value
            with self.assertRaises(CandidateError):
                KVWorkload.parse(obj)

    def test_input_schema_and_reference_resource_limits(self):
        for obj in (None, [], {**fixture(), "version": True}, {**fixture(), "phase": "decode"}):
            with self.assertRaises(CandidateError):
                KVWorkload.parse(obj)
        for key, value in (("tokens", 65), ("dimensions", 0), ("element_bytes", 3)):
            obj = fixture(); obj["tensor"][key] = value
            with self.assertRaises(CandidateError):
                KVWorkload.parse(obj)
        obj = fixture()
        obj["tensor"].update(tokens=64, dimensions=256)
        obj["events"] = [{"op": "append", "count": 64}, *[
            {"op": "read", "tokens": [0, 64], "dimensions": [0, 256], "group_by": "dimension"} for _ in range(64)], {"op": "end"}]
        with self.assertRaises(CandidateError) as error:
            KVWorkload.parse(obj)
        self.assertEqual(error.exception.code, "access_limit")

    def test_legacy_generator_and_explicit_events_have_identical_execution(self):
        for every in (0, 1, 3, 16, 99):
            cfg = LayoutConfig(read_every=every)
            old, expected = KVLayoutTask(cfg), []
            for t in range(cfg.tokens):
                expected.append(("write", tuple(t * cfg.dimensions + d for d in range(cfg.dimensions))))
                if every and ((t + 1) % every == 0 or t + 1 == cfg.tokens):
                    expected.extend(("read", tuple(i * cfg.dimensions + d for i in range(t + 1))) for d in range(cfg.dimensions))
            explicit = layout_task(regular_workload(cfg.tokens, cfg.dimensions, cfg.element_bytes, every).to_dict(), cfg)
            self.assertEqual(tuple(expected), old.groups)
            self.assertEqual(old.groups, tuple((k, i) for _, _, k, i in explicit.workload.groups()))
            layout = Layout(4, 8)
            self.assertEqual({k: v for k, v in old.measure(layout.to_dict()).items() if k != "backend"},
                             {k: v for k, v in explicit.measure(design(explicit, layout)).items() if k != "backend"})
            self.assertEqual(old.check(layout.to_dict())["status"], explicit.check(design(explicit, layout))["status"])

    def test_objects_have_independent_prefixes_and_round_trip(self):
        shape = {"tokens": 4, "dimensions": 4, "element_bytes": 2}
        obj = {"version": 2, "objects": {"V": shape, "K": shape}, "events": [
            {"op": "append", "object": "K", "count": 2},
            {"op": "read", "object": "V", "tokens": [0, 2], "dimensions": [0, 4], "group_by": "token"},
            {"op": "end"}]}
        with self.assertRaises(CandidateError) as caught: KVWorkload.parse(obj)
        self.assertEqual(caught.exception.details["object"], "V")
        obj["events"].insert(1, {"op": "append", "object": "V", "count": 2})
        parsed = KVWorkload.parse(obj)
        self.assertEqual(parsed.written_tokens, {"K": 2, "V": 2})
        self.assertEqual(KVWorkload.parse(parsed.to_dict()), parsed)
        obj["events"][0]["object"] = "unknown"
        with self.assertRaises(CandidateError) as caught: KVWorkload.parse(obj)
        self.assertEqual(caught.exception.path, "$.workload.events[0].object")




if __name__ == "__main__":
    unittest.main()
