"""Cross-layer behavior and the new public input/session contract."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.__main__ import main, load_task
from agent_design_search.contracts import CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_init, submit, submit_file, session_finish
from agent_design_search.json_io import read, save
from agent_design_search.execution.memory_system import route_requests
from agent_design_search.execution import memory_system as memory_execution
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.performance.dram import FCFS
from agent_design_search.spaces.dram_dsl import Policy
from agent_design_search.spaces.kv_layout_dsl import AXES, Layout, KVLayout


def kv_spec(tokens=4, dimensions=4, backend="fcfs"):
    return {"version": 1,
            "workload": {"kind": "kv_events", "data": {
                "version": 1, "tensor": {"tokens": tokens, "dimensions": dimensions, "element_bytes": 2},
                "events": [{"op": "append", "count": tokens},
                           {"op": "read", "tokens": [0, tokens], "dimensions": [0, dimensions], "group_by": "token"},
                           {"op": "end"}]}},
            "hardware": {"channels": 2, "banks_per_channel": 1, "rows": 1, "columns": 2, "granule_bytes": 8},
            "execution": {"kind": "serial_groups", "base_address": 0},
            "design_space": {"vary": ["kv_layout"] if backend == "line-traffic-v1" else ["kv_layout", "dram_address_mapping"]},
            "evaluation": {"backend": backend, "objective": "transferred_bytes" if backend == "line-traffic-v1" else "completion_cycles"},
            "budget": 4}


def trace_spec():
    return {"version": 1, "workload": {"kind": "address_trace", "data": {"version": 1, "requests": [
        {"request_id": 0, "address": 0, "arrival": 0}, {"request_id": 1, "address": 8, "arrival": 0}]}},
        "hardware": kv_spec()["hardware"], "execution": {"kind": "fixed_arrivals"},
        "design_space": {"vary": ["dram_address_mapping"]},
        "evaluation": {"backend": "fcfs", "objective": "completion_cycles"}, "budget": 4}


def proposal(design):
    return {"hypothesis": "change the channel distribution", "expected": "different completion time", "dsl": design}


class MemoryExecutionTests(unittest.TestCase):
    def test_tensor_address_chain_uses_actual_mapped_transfer(self):
        task = MemorySearchTask.from_file(Path(__file__).resolve().parents[1] / "examples/tensor_addressing.json")
        design = task.default_design
        plain = task.measure(design)
        self.assertNotIn("request_sources", plain)
        for operations, expected in (((), (0, 4)), ((("swap_bits", 0, 4),), (1, 0)),
                                     ((("xor_bit", 0, 4),), (1, 4))):
            design["dram_address_mapping"] = Policy(operations).to_dict()
            self.assertEqual(task.check(task.normalize(design))["status"], "pass")
            metrics = task.measure(design, True)
            source = metrics["request_sources"][-1]
            self.assertEqual(metrics["virtual_memory"], "not_modeled")
            self.assertEqual((source["workload_event"], source["group"], source["kind"]), (1, 32, "read"))
            self.assertEqual((source["address"], source["granule_bytes"], source["valid_bytes"]), (1024, 64, [0, 1]))
            self.assertEqual(source["dram_location"], dict(channel=expected[0], bank=expected[1], row=0, column=0))
            self.assertEqual(source["byte_origins"], [
                dict(index=[0, 16], element_byte=b, object_offset=1024+b, region_offset=1024+b,
                     target_address=1024+b, block_offset=b) for b in range(2)])
            if not operations:
                self.assertEqual({k: metrics[k] for k in plain}, plain)

    def test_detailed_evaluation_uses_one_call_and_the_same_budget(self):
        task = MemorySearchTask(kv_spec())
        with patch.object(task, "measure", wraps=task.measure) as measure:
            evaluator = Evaluator(task, budget=1, measure=lambda d: task.measure(d, include_events=True))
            result = evaluator.evaluate(task.default_design)
            self.assertIn("byte_origins", result["metrics"]["request_sources"][0])
            self.assertTrue(evaluator.evaluate(task.default_design)["cached"])
            self.assertEqual((evaluator.backend_calls, measure.call_count), (1, 1))
            changed = task.default_design
            changed["dram_address_mapping"] = Policy((("swap_bits", 0, 1),)).to_dict()
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                evaluator.evaluate(changed)
            self.assertEqual(measure.call_count, 1)

    def test_joint_mapping_changes_parallelism_hand_calculated(self):
        task = MemorySearchTask(kv_spec(1, 8))
        first = Evaluator(task).evaluate(task.default_design)
        self.assertEqual((first["metrics"]["completion_cycles"], first["metrics"]["transferred_bytes"]), (24, 32))
        design = task.default_design
        design["dram_address_mapping"] = Policy((("swap_bits", 0, 1),)).to_dict()
        second = Evaluator(task).evaluate(design)
        self.assertEqual((second["metrics"]["completion_cycles"], second["metrics"]["transferred_bytes"]), (40, 32))
        self.assertEqual(second["metrics"]["channel_requests"], [4, 0])

    def test_layout_changes_downstream_requests_and_both_spaces_share_budget(self):
        spec = kv_spec()
        spec["workload"]["data"]["events"][1]["group_by"] = "dimension"
        task, evaluator = MemorySearchTask(spec), Evaluator(MemorySearchTask(spec), budget=3)
        first = evaluator.evaluate(task.default_design)
        design = task.default_design; design["kv_layout"] = Layout(2, 2).to_dict()
        second = evaluator.evaluate(design)
        design["dram_address_mapping"] = Policy((("swap_bits", 0, 1),)).to_dict()
        third = evaluator.evaluate(design)
        self.assertEqual([r["metrics"]["transferred_bytes"] for r in (first, second, third)], [160, 128, 128])
        self.assertNotEqual(second["metrics"]["completion_cycles"], third["metrics"]["completion_cycles"])
        self.assertEqual(evaluator.backend_calls, 3)

    def test_serial_dependencies_preserve_open_rows_and_completion_state(self):
        spec = kv_spec(2, 2); spec["hardware"]["channels"] = 1
        task = MemorySearchTask(spec)
        result = task.measure(task.default_design, include_events=True)
        self.assertEqual(result["completion_cycles"], 40)  # 16 cold, then three 8-cycle hits.
        self.assertEqual([e["arrival"] for e in result["events"]], [0, 16, 24, 32])
        self.assertEqual([e["row_state"] for e in result["events"]], ["row_empty", "row_hits", "row_hits", "row_hits"])
        self.assertEqual([r["workload_event"] for r in result["request_sources"]], [0, 0, 1, 1])

    def test_fixed_arrivals_use_same_routing_without_forcing_serial_dependencies(self):
        task = MemorySearchTask(trace_spec())
        metrics = task.measure(task.default_design, True)
        self.assertEqual(metrics["completion_cycles"], 16)
        self.assertEqual([e["arrival"] for e in metrics["events"]], [0, 0])
        engine = FCFS(task.hardware)
        transfers = route_requests(task.requests, Policy(), task.hardware)
        engine.submit_batch(transfers)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            engine.submit_batch([replace(transfers[0], request=replace(task.requests[0], request_id=2))])

    def test_padding_byte_masks_and_early_end_reserve_capacity(self):
        spec = kv_spec(3, 5); spec["hardware"]["rows"] = 4
        spec["execution"]["base_address"] = 8
        spec["workload"]["data"]["events"][0]["count"] = 1
        spec["workload"]["data"]["events"][1]["tokens"] = [0, 1]
        task = MemorySearchTask(spec); design = task.default_design
        design["kv_layout"] = Layout(2, 3).to_dict()
        self.assertEqual(task.check(design)["status"], "pass")
        metrics = task.measure(design, True)
        self.assertEqual((metrics["allocated_bytes"], metrics["write_useful_bytes"], metrics["read_useful_bytes"]), (48, 10, 10))
        self.assertEqual(sum(len(r["valid_bytes"]) for r in metrics["request_sources"]), 20)
        self.assertTrue(all(r["address"] >= 8 for r in metrics["request_sources"]))
        origins = [b for r in metrics["request_sources"] for b in r["byte_origins"]]
        self.assertEqual(len(origins), 20)  # Padding and unwritten rows have no byte origins.
        self.assertTrue(all(b["index"][0] == 0 and b["index"][1] < 5 for b in origins))
        self.assertTrue(all(b["target_address"] == b["region_offset"] + 8 for b in origins))
        spec["execution"]["base_address"] = 96
        small = MemorySearchTask(spec)
        with self.assertRaises(CandidateError):
            small.normalize(design)

    def test_line_traffic_stays_an_explicit_single_layer_profile(self):
        spec = kv_spec(backend="line-traffic-v1")
        spec["workload"]["data"]["events"][1]["group_by"] = "dimension"
        task = MemorySearchTask(spec)
        for layout, expected in ((Layout(1, 4), 160), (Layout(4, 1), 160), (Layout(2, 2), 128)):
            design = task.default_design; design["kv_layout"] = layout.to_dict()
            result = Evaluator(task).evaluate(design)
            self.assertEqual(result["metrics"]["transferred_bytes"], expected)
            self.assertNotIn("completion_cycles", result["metrics"])


class SearchInputTests(unittest.TestCase):
    def test_reject_unsupported_combinations_and_undeclared_freedom(self):
        bad = []
        spec = trace_spec(); spec["design_space"]["vary"].append("kv_layout"); bad.append(spec)
        spec = kv_spec(backend="line-traffic-v1"); spec["design_space"]["vary"].append("dram_address_mapping"); bad.append(spec)
        spec = kv_spec(); spec["evaluation"]["backend"] = "ramulator"; bad.append(spec)
        spec = kv_spec(); spec["evaluation"]["objective"] = "tokens_per_second"; bad.append(spec)
        spec = kv_spec(); spec["execution"]["kind"] = "fixed_arrivals"; bad.append(spec)
        spec = kv_spec(); spec["execution"]["migration"] = True; bad.append(spec)
        spec = kv_spec(); spec["fixed_design"] = {"kv_layout": Layout(1, 4).to_dict()}; bad.append(spec)
        spec = kv_spec(); spec["hardware"]["channels"] = True; bad.append(spec)
        spec = kv_spec(); spec["design_space"]["max_mapping_operations"] = True; bad.append(spec)
        spec = kv_spec(); spec["evaluation"]["parameters"] = {"typo": 10}; bad.append(spec)
        for spec in bad:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                MemorySearchTask(spec)

    def test_fixed_components_and_nested_error_paths(self):
        task = MemorySearchTask(kv_spec(backend="line-traffic-v1"))
        design = task.default_design
        design["dram_address_mapping"] = Policy((("swap_bits", 0, 1),)).to_dict()
        with self.assertRaises(CandidateError) as caught:
            task.normalize(design)
        self.assertEqual(caught.exception.code, "fixed_strategy")
        design = task.default_design; design["kv_layout"]["objects"]["V"]["tile"]["token"] = 0
        with self.assertRaises(CandidateError) as caught:
            task.normalize(design)
        self.assertEqual(caught.exception.path, "$.dsl.kv_layout.objects.V.tile.token")
        with self.assertRaises(CandidateError):
            task.normalize({**task.default_design, "workload": {}})

    def test_semantic_cache_and_all_fixed_inputs_affect_identity(self):
        spec = kv_spec(); task = MemorySearchTask(spec); evaluator = Evaluator(task)
        first = evaluator.evaluate(task.default_design)
        design = task.default_design
        design["dram_address_mapping"] = Policy((("xor_bit", 0, 1), ("xor_bit", 0, 1))).to_dict()
        self.assertTrue(evaluator.evaluate(design)["cached"])
        for section, key, value in (("evaluation", "parameters", {"t_hit": 5}),
                                    ("execution", "base_address", 32), ("hardware", "rows", 2)):
            changed = deepcopy(spec)
            if key == "base_address":
                changed["hardware"]["rows"] = 2
            changed[section][key] = value
            other = MemorySearchTask(changed)
            self.assertNotEqual(first["candidate_id"], other.candidate_id(other.default_design))
        snapshot = task.snapshot(); snapshot["hardware"]["channels"] = 8
        design = task.default_design; design["dram_address_mapping"]["operations"].append({})
        spec["workload"]["data"]["events"].clear()
        self.assertEqual(first["candidate_id"], task.candidate_id(task.default_design))

    def test_content_failure_keeps_source_event_and_does_not_run_backend(self):
        task = MemorySearchTask(kv_spec()); evaluator = Evaluator(task)
        with patch("agent_design_search.exploration.memory_search_task.verify_contents", return_value={"status": "fail", "workload_event": 1}):
            with self.assertRaises(CandidateError) as caught:
                evaluator.evaluate(task.default_design)
        self.assertEqual(caught.exception.details["verification"]["workload_event"], 1)
        self.assertEqual(evaluator.backend_calls, 0)


class PublicSessionTests(unittest.TestCase):
    def cli(self, *args):
        output = StringIO()
        with patch("sys.argv", ["agent_design_search", *map(str, args)]), redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_file_references_freeze_restart_and_detect_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); spec = kv_spec(); source = root / "search.json"; folder = root / "session"
            save(root / "workload.json", spec["workload"]["data"]); save(root / "hardware.json", spec["hardware"])
            spec["workload"] = {"kind": "kv_events", "file": "workload.json"}; spec["hardware"] = "hardware.json"
            save(source, spec)
            self.cli("inspect-task", source)
            self.assertEqual(self.cli("session-init", folder, "--task", source)["backend_calls"], 1)
            (root / "workload.json").write_text("{}", encoding="utf8")
            task = load_task(folder); design = task.default_design
            design["kv_layout"] = Layout(2, 2).to_dict()
            save(root / "proposal.json", proposal(design))
            self.assertEqual(self.cli("session-step", folder, "--proposal", root / "proposal.json")["backend_calls"], 2)
            self.assertTrue(self.cli("session-step", folder, "--proposal", root / "proposal.json")["cached"])
            self.assertEqual(self.cli("session-finish", folder)["backend_calls"], 2)
            frozen = read(folder / "task.json"); frozen["evaluation"]["parameters"]["t_hit"] += 1
            save(folder / "task.json", frozen)
            with self.assertRaisesRegex(ValueError, "Frozen task changed"):
                load_task(folder)

    def test_failures_and_invalid_proposals_share_the_single_budget(self):
        spec = kv_spec(); spec["budget"] = 2
        task = MemorySearchTask(spec)
        self.assertEqual(Evaluator(task).budget, 2)
        with self.assertRaises(ValueError):
            Evaluator(task, budget=3)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Budget differs"):
                session_init(tmp, task, budget=3)
            session_init(tmp, task, budget=2)
            path = Path(tmp) / "bad.json"; path.write_text("{", encoding="utf8")
            self.assertEqual(submit_file(tmp, task, path)["backend_calls"], 0)
            with patch.object(task, "measure", side_effect=RuntimeError("test failure")):
                self.assertEqual(submit(tmp, task, proposal(task.default_design))["status"], "backend_error")
            self.assertEqual(submit(tmp, task, proposal(task.default_design))["backend_calls"], 2)
            self.assertEqual(session_finish(tmp, load_task(tmp))["best"]["status"], "ok")

    def test_cli_evaluate_and_reject_bad_task_before_creating_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); spec = kv_spec(backend="line-traffic-v1")
            task = MemorySearchTask(spec); save(root / "search.json", spec); save(root / "design.json", task.default_design)
            result = self.cli("evaluate", root / "design.json", "--task", root / "search.json")
            self.assertEqual(result["metrics"]["transferred_bytes"], 64)
            detailed = self.cli("evaluate", root / "design.json", "--task", root / "search.json", "--include-events")
            self.assertEqual(detailed["metrics"]["transferred_bytes"], 64)
            self.assertEqual(detailed["metrics"]["request_sources"][0]["address_space"], "dram_target_bytes")
            spec["design_space"]["vary"].append("dram_address_mapping"); save(root / "search.json", spec)
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                self.cli("session-init", root / "bad", "--task", root / "search.json")
            self.assertFalse((root / "bad").exists())


def multi_spec(shapes=None):
    shapes = shapes or {n: {"tokens": 4, "dimensions": 4, "element_bytes": 2} for n in ("K", "V")}
    spec = kv_spec()
    spec["version"] = 2
    spec["hardware"] = {"channels": 2, "banks_per_channel": 1, "rows": 2, "columns": 4, "granule_bytes": 8}
    spec["design_space"]["layout_options"] = {"alignment_bytes": [8, 16, 32, 64], "chunk_bytes": [8, 16, 32, 64]}
    spec["workload"]["data"] = {"version": 2, "objects": shapes, "events": [
        *[{"op": "append", "object": n, "count": s["tokens"]} for n, s in shapes.items()],
        *[{"op": "read", "object": n, "tokens": [0, s["tokens"]], "dimensions": [0, s["dimensions"]],
           "group_by": "dimension"} for n, s in shapes.items()], {"op": "end"}]}
    return spec


class MultiObjectTests(unittest.TestCase):
    def test_four_kib_chunks_concat_and_interleave_hand_addresses(self):
        spec = multi_spec({n: {"tokens": 16, "dimensions": 256, "element_bytes": 2} for n in ("K", "V")})
        spec["hardware"] = {"channels": 4, "banks_per_channel": 1, "rows": 8, "columns": 8, "granule_bytes": 64}
        spec["design_space"]["layout_options"] = {"alignment_bytes": [64], "chunk_bytes": [4096]}
        task = MemorySearchTask(spec)
        design = task.default_design
        layout = task.parse_layout(design["kv_layout"])
        offsets = layout.offsets(task.bounds)
        self.assertEqual([offsets[n][i] for n, i in (("K", 0), ("K", 2048), ("V", 0), ("V", 2048))],
                         [0, 4096, 8192, 12288])
        design["kv_layout"]["composition"] = {"kind": "interleave", "order": ["K", "V"], "chunk_bytes": 4096}
        layout = task.parse_layout(design["kv_layout"])
        offsets = layout.offsets(task.bounds)
        self.assertEqual([offsets[n][i] for n, i in (("K", 0), ("V", 0), ("K", 2048), ("V", 2048))],
                         [0, 4096, 8192, 12288])
        result = Evaluator(task).evaluate(design)
        self.assertEqual(result["metrics"]["allocated_bytes"], 16384)
        self.assertEqual(result["verification"]["checked_bytes"], 32768)

    def test_unequal_objects_partial_tiles_tail_padding_and_early_end(self):
        spec = multi_spec({"K": {"tokens": 3, "dimensions": 5, "element_bytes": 2},
                           "V": {"tokens": 2, "dimensions": 3, "element_bytes": 1}})
        for e in spec["workload"]["data"]["events"]:
            if e["op"] == "append": e["count"] = 1
            if e["op"] == "read": e["tokens"] = [0, 1]
        task = MemorySearchTask(spec); design = task.default_design
        design["kv_layout"]["objects"]["K"] = Layout(2, 3).to_dict()
        design["kv_layout"]["composition"] = {"kind": "interleave", "order": ["K", "V"], "chunk_bytes": 16}
        layout = task.parse_layout(design["kv_layout"])
        self.assertEqual(layout.offsets(task.bounds)["K"], [0, 2, 4, 12, 14, 6, 8, 10, 34, 36, 40, 42, 44, 52, 54])
        self.assertEqual(layout.offsets(task.bounds)["V"], list(range(16, 22)))
        result = Evaluator(task).evaluate(design)
        self.assertEqual(result["metrics"]["allocated_bytes"], 64)
        self.assertEqual(result["metrics"]["write_useful_bytes"], 13)
        self.assertEqual(result["verification"]["checked_bytes"], 26)
        design["kv_layout"]["composition"] = {"kind": "concat", "order": ["K", "V"], "alignment_bytes": 32}
        self.assertEqual(task.parse_layout(design["kv_layout"]).allocated_bytes(task.bounds), 72)

    def test_all_axis_orders_with_both_compositions_match_independent_replay(self):
        from itertools import permutations
        spec = multi_spec({"K": {"tokens": 3, "dimensions": 5, "element_bytes": 2},
                           "V": {"tokens": 2, "dimensions": 3, "element_bytes": 1}})
        task = MemorySearchTask(spec)
        for order in permutations(AXES):
            for composition in ({"kind": "concat", "order": ["V", "K"], "alignment_bytes": 16},
                                {"kind": "interleave", "order": ["V", "K"], "chunk_bytes": 16}):
                design = task.default_design
                design["kv_layout"]["objects"]["K"] = Layout(2, 3, order).to_dict()
                design["kv_layout"]["composition"] = composition
                self.assertEqual(task.check(task.normalize(design))["status"], "pass")

    def test_joint_capacity_fixed_scope_and_declared_choices(self):
        task = MemorySearchTask(multi_spec())
        bad = []
        d = task.default_design; del d["kv_layout"]["objects"]["K"]; bad.append(d)
        d = task.default_design; d["kv_layout"]["composition"]["order"] = ["K", "K"]; bad.append(d)
        d = task.default_design; d["kv_layout"]["composition"]["alignment_bytes"] = 24; bad.append(d)
        d = task.default_design; d["kv_layout"] = Layout(2, 2).to_dict(); bad.append(d)
        d = task.default_design; d["kv_layout"]["composition"]["target"] = "HBM1"; bad.append(d)
        for d in bad:
            with self.assertRaises(CandidateError): task.normalize(d)
        spec = multi_spec(); spec["hardware"]["rows"] = 1  # 64 bytes total, each padded object uses 72.
        small = MemorySearchTask(spec); d = small.default_design
        d["kv_layout"]["objects"]["K"] = Layout(3, 3).to_dict()
        with self.assertRaises(CandidateError): small.normalize(d)
        d = small.default_design
        d["kv_layout"]["composition"] = {"kind": "interleave", "order": ["K", "V"], "chunk_bytes": 64}
        with self.assertRaises(CandidateError) as caught: small.normalize(d)
        self.assertEqual(caught.exception.details["required_bytes"], 128)
        spec = multi_spec(); spec["design_space"]["vary"] = ["dram_address_mapping"]
        fixed = MemorySearchTask(spec); d = fixed.default_design
        d["kv_layout"]["composition"]["order"].reverse()
        with self.assertRaises(CandidateError) as caught: fixed.normalize(d)
        self.assertEqual(caught.exception.code, "fixed_strategy")

    def test_semantic_cache_includes_object_ownership_padding_and_options(self):
        task = MemorySearchTask(multi_spec()); evaluator = Evaluator(task)
        first = evaluator.evaluate(task.default_design)
        d = task.default_design; d["kv_layout"]["objects"]["K"] = Layout(4, 4).to_dict()
        self.assertTrue(evaluator.evaluate(d)["cached"])
        d = task.default_design; d["kv_layout"]["composition"]["order"].reverse()
        self.assertNotEqual(first["candidate_id"], task.candidate_id(d))
        spec = multi_spec(); spec["design_space"]["layout_options"]["chunk_bytes"] = [8, 16]
        other = MemorySearchTask(spec)
        self.assertNotEqual(first["candidate_id"], other.candidate_id(other.default_design))

    def test_actual_transaction_corruption_fails_before_performance(self):
        task = MemorySearchTask(multi_spec())
        original = memory_execution.kv_transfers

        def corrupt(mode):
            def run(*args, **kwargs):
                batches = list(original(*args, **kwargs))
                event, name, items = batches[0]
                t = items[0]
                if mode == "missing":
                    items[0] = replace(t, access=replace(t.access, valid_bytes=t.access.valid_bytes[:-1]), byte_tags=t.byte_tags[:-1])
                elif mode == "object":
                    items[0] = replace(t, byte_tags=tuple(("V", i, b) for _, i, b in t.byte_tags))
                elif mode == "route":
                    items[0] = replace(t, access=replace(t.access, channel=1-t.access.channel))
                elif mode == "duplicate":
                    items.append(t)
                elif mode == "group":
                    batches.pop(0)
                yield from batches
            return run

        for mode in ("missing", "object", "route", "duplicate", "group"):
            evaluator = Evaluator(task)
            with self.subTest(mode=mode), patch.object(memory_execution, "kv_transfers", corrupt(mode)):
                with self.assertRaises(CandidateError) as caught: evaluator.evaluate(task.default_design)
            verification = caught.exception.details["verification"]
            self.assertEqual(verification["workload_event"], 0)
            self.assertEqual(evaluator.backend_calls, 0)
        offsets = task.parse_layout(task.default_design["kv_layout"]).offsets(task.bounds)
        offsets["V"] = offsets["K"]
        with patch.object(KVLayout, "offsets", return_value=offsets):
            self.assertEqual(task.check(task.default_design)["reason"], "placement_mismatch")

    def test_padding_overwrite_is_detected_even_without_subsequent_read(self):
        spec = multi_spec({"K": {"tokens": 1, "dimensions": 3, "element_bytes": 1}})
        spec["workload"]["data"]["events"] = [spec["workload"]["data"]["events"][0], {"op": "end"}]
        task = MemorySearchTask(spec); original = memory_execution.kv_transfers
        def corrupt(*args, **kwargs):
            for event, name, items in original(*args, **kwargs):
                t = items[0]
                items[0] = replace(t, access=replace(t.access, valid_bytes=(*t.access.valid_bytes, 3)),
                                   byte_tags=(*t.byte_tags, ("K", 3, 0)))
                yield event, name, items
        with patch.object(memory_execution, "kv_transfers", corrupt):
            self.assertEqual(task.check(task.default_design)["reason"], "masked_byte_mismatch")

    def test_input_versions_and_capabilities_are_explicit(self):
        for version in (0, 3, True):
            spec = multi_spec(); spec["version"] = version
            with self.assertRaises(ValueError): MemorySearchTask(spec)
        for options in ({"chunk_bytes": [8]}, {"alignment_bytes": [True], "chunk_bytes": [8]},
                        {"alignment_bytes": [8], "chunk_bytes": [12]}):
            spec = multi_spec(); spec["design_space"]["layout_options"] = options
            with self.assertRaises(ValueError): MemorySearchTask(spec)
        spec = multi_spec(); spec["design_space"]["vary"].append("nand_mapping")
        with self.assertRaises(ValueError): MemorySearchTask(spec)
        task = MemorySearchTask(kv_spec())
        restored = MemorySearchTask(task.snapshot())
        self.assertEqual(restored.definition, task.definition)
        self.assertEqual(restored.default_design, task.default_design)
        self.assertEqual(task.snapshot()["version"], 2)

    def test_multi_object_file_session_restart_budget_and_protocol(self):
        cli = PublicSessionTests().cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / "search.json"; folder = root / "session"
            save(source, multi_spec())
            initialized = cli("session-init", folder, "--task", source)
            self.assertEqual((initialized["protocol"], initialized["backend_calls"]), ("agent-memory-search-v3", 1))
            task = load_task(folder); d = task.default_design
            d["kv_layout"]["composition"] = {"kind": "interleave", "order": ["V", "K"], "chunk_bytes": 8}
            save(root / "proposal.json", proposal(d))
            entry = cli("session-step", folder, "--proposal", root / "proposal.json")
            self.assertEqual((entry["status"], entry["backend_calls"]), ("ok", 2))
            self.assertEqual(entry["verification"]["objects"], 2)
            self.assertTrue(cli("session-step", folder, "--proposal", root / "proposal.json")["cached"])
            (root / "proposal.json").write_text('{"dsl":{},"dsl":{}}', encoding="utf-8")
            invalid = cli("session-step", folder, "--proposal", root / "proposal.json")
            self.assertEqual((invalid["status"], invalid["backend_calls"]), ("invalid", 2))
            self.assertIn("Duplicate JSON field", invalid["error"])
            save(source, {})  # The session uses its own complete input snapshot.
            self.assertTrue(cli("session-finish", folder)["closed"])
            state = read(folder / "state.json"); state["protocol"] = "agent-memory-search-v2"
            save(folder / "state.json", state)
            with self.assertRaisesRegex(ValueError, "Historical session"): load_task(folder)


if __name__ == "__main__":
    unittest.main()
