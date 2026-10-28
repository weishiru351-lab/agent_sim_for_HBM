"""Topology placement, target isolation and shared-link timing contracts."""

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.contracts import CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.__main__ import load_task
from agent_design_search.__main__ import main
from agent_design_search.json_io import save
from agent_design_search.inputs.hardware import MemorySystem
from agent_design_search.execution.memory_system import kv_transfers
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.spaces.kv_layout_dsl import Layout


def system_spec(count=2, shared=True):
    geometry = dict(channels=1, banks_per_channel=1, rows=2, columns=2, granule_bytes=8)
    link = dict(bytes_per_beat=8, beat_ps=10, latency_ps=0)
    return {"version": 3, "hardware": {
        "version": 1, "modules": [f"m{i}" for i in range(count)],
        "targets": {f"d{i}": dict(module=f"m{i}", medium="dram", geometry=geometry.copy(),
                                  clock_period_ps=10, mapping_group="shared", links=["bus" if shared else f"bus{i}"])
                    for i in range(count)},
        "regions": {f"r{i}": dict(target=f"d{i}", base_address=0, size_bytes=32) for i in range(count)},
        "pools": {"memory": [f"r{i}" for i in range(count)]},
        "links": {"bus": link} if shared else {f"bus{i}": link.copy() for i in range(count)}},
        "workload": {"kind": "kv_events", "data": {"version": 2,
            "objects": {"V": dict(tokens=1, dimensions=8, element_bytes=2)},
            "events": [{"op": "append", "object": "V", "count": 1},
                       {"op": "read", "object": "V", "tokens": [0, 1], "dimensions": [0, 8], "group_by": "token"},
                       {"op": "end"}]}},
        "execution": {"kind": "serial_groups"},
        "design_space": {"vary": ["kv_layout", "dram_address_mapping"],
                         "layout_options": {"pool": "memory", "alignment_bytes": [8, 16], "chunk_bytes": [8, 16]}},
        "evaluation": {"backend": "fcfs", "objective": "completion_ps"}, "budget": 3}


def group(names, regions, stripe=8, kind="concat"):
    return {"composition": {"kind": kind, "order": names,
                            "alignment_bytes" if kind == "concat" else "chunk_bytes": 8},
            "placement": {"regions": regions, "stripe_bytes": stripe}}


class MemorySystemTests(unittest.TestCase):
    def test_two_targets_and_shared_link_hand_timing(self):
        for shared, expected in ((False, 260), (True, 280)):
            task = MemorySearchTask(system_spec(shared=shared))
            self.assertEqual(task.check(task.default_design)["status"], "pass")
            result = task.measure(task.default_design, True)
            self.assertEqual((result["completion_ps"], result["transferred_bytes"]), (expected, 32))
            self.assertNotIn("completion_cycles", result)
            sources = result["request_sources"]
            self.assertEqual([(s["target"], s["address"]) for s in sources[:2]], [("d0", 0), ("d1", 0)])
            # Striping changes target addresses, but each byte retains its object-local offset.
            self.assertEqual([s["byte_origins"][0]["object_offset"] for s in sources[:2]], [0, 8])
            self.assertEqual([s["byte_origins"][0]["target_address"] for s in sources[:2]], [0, 0])
            self.assertEqual([s["group"] for s in sources], [0, 0, 1, 1])
            self.assertEqual(result["allocated_bytes_by_target"], {"d0": 8, "d1": 8})
            events = result["group_events"]
            self.assertEqual(events[0]["finish_ps"], events[1]["start_ps"])
            if shared:
                self.assertEqual([e["start_ps"] for e in result["link_events"]], [0, 10, 260, 270])
                self.assertEqual(result["per_link"]["bus"]["queue_wait_ps"], 20)

    def test_different_clocks_and_store_forward_link_paths(self):
        spec = system_spec()
        spec["hardware"]["targets"]["d1"]["clock_period_ps"] = 20
        task = MemorySearchTask(spec)
        self.assertEqual(task.measure(task.default_design)["completion_ps"], 510)
        for i in range(2):
            spec["hardware"]["links"][f"leaf{i}"] = dict(bytes_per_beat=8, beat_ps=4, latency_ps=1)
            spec["hardware"]["targets"][f"d{i}"]["links"].append(f"leaf{i}")
        task = MemorySearchTask(spec)
        result = task.measure(task.default_design, True)
        self.assertEqual(result["completion_ps"], 520)
        read0 = [e["link"] for e in result["link_events"] if e["request_id"] == 2]
        self.assertEqual(read0, ["leaf0", "bus"])

    def test_groups_isolation_padding_and_unequal_geometries(self):
        spec = system_spec()
        data = spec["workload"]["data"]
        data["objects"]["K"] = dict(tokens=3, dimensions=3, element_bytes=1)
        data["events"].insert(1, {"op": "append", "object": "K", "count": 2})
        data["events"].insert(-1, {"op": "read", "object": "K", "tokens": [0, 2], "dimensions": [0, 3], "group_by": "dimension"})
        target = spec["hardware"]["targets"]["d1"]
        target["mapping_group"] = "other"
        target["geometry"] = dict(channels=1, banks_per_channel=1, rows=2, columns=4, granule_bytes=4)
        task = MemorySearchTask(spec)
        design = task.default_design
        design["kv_layout"]["objects"]["K"] = Layout(2, 2).to_dict()
        design["kv_layout"]["groups"] = [group(["K"], ["r0"]), group(["V"], ["r1"])]
        self.assertEqual(task.check(task.normalize(design))["status"], "pass")
        result = task.measure(design, True)
        self.assertEqual(result["allocated_bytes_by_target"], {"d0": 16, "d1": 16})
        # Padding is reserved but never emitted as a useful byte.
        self.assertEqual(result["write_useful_bytes"], 22)
        self.assertTrue(any(s["target"] == "d1" and s["address"] == 0 for s in result["request_sources"]))
        for kind in ("concat", "interleave"):
            design["kv_layout"]["groups"] = [group(["V", "K"], ["r1", "r0"], 16, kind)]
            self.assertEqual(task.check(task.normalize(design))["status"], "pass")

    def test_region_boundaries_and_no_double_counted_pools(self):
        base = system_spec()["hardware"]
        mutations = []
        b = deepcopy(base); b["regions"]["alias"] = b["regions"]["r0"].copy(); mutations.append(b)
        b = deepcopy(base); b["pools"]["memory"].append("r0"); mutations.append(b)
        b = deepcopy(base); b["regions"]["r0"]["base_address"] = 1; mutations.append(b)
        b = deepcopy(base); b["regions"]["r0"]["size_bytes"] = 40; mutations.append(b)
        b = deepcopy(base); b["targets"]["d0"]["module"] = "missing"; mutations.append(b)
        b = deepcopy(base); b["targets"]["d0"]["links"] = ["missing"]; mutations.append(b)
        b = deepcopy(base); b["targets"]["d0"]["clock_period_ps"] = True; mutations.append(b)
        b = deepcopy(base); b["targets"]["d0"]["medium"] = "nand"; mutations.append(b)
        b = deepcopy(base); b["targets"]["d1"]["geometry"]["rows"] = 4; mutations.append(b)
        for hardware in mutations:
            with self.subTest(hardware=hardware), self.assertRaises(ValueError):
                MemorySystem(hardware)
        spec = system_spec()
        spec["hardware"]["regions"] = {"lo": dict(target="d0", base_address=0, size_bytes=16),
                                         "hi": dict(target="d0", base_address=16, size_bytes=16)}
        spec["hardware"]["pools"] = {"memory": ["lo", "hi"], "other_view": ["lo"]}
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        self.assertEqual(task.measure(task.default_design)["allocated_bytes_by_target"], {"d0": 16, "d1": 0})

    def test_local_overflow_and_explicit_seed_for_unequal_capacity(self):
        spec = system_spec()
        spec["hardware"]["regions"]["r0"]["size_bytes"] = 8
        task = MemorySearchTask(spec)
        invalid = task.default_design
        invalid["kv_layout"]["groups"][0]["placement"]["regions"] = ["r0"]
        evaluator = Evaluator(task)
        with self.assertRaises(CandidateError) as error:
            evaluator.evaluate(invalid)
        self.assertEqual(evaluator.backend_calls, 0)
        self.assertEqual(error.exception.details["target"], "d0")
        seed = task.default_design
        seed["kv_layout"]["groups"][0]["placement"]["regions"] = ["r1"]
        spec["workload"]["data"]["objects"]["V"]["dimensions"] = 16
        spec["workload"]["data"]["events"][1]["dimensions"] = [0, 16]
        with self.assertRaises(CandidateError):
            MemorySearchTask(spec)  # Equal striping overflows r0, although r1 alone can fit.
        seed["kv_layout"]["objects"]["V"] = Layout(1, 16).to_dict()
        spec["initial_design"] = seed
        task = MemorySearchTask(spec)
        self.assertEqual(task.check(task.default_design)["status"], "pass")
        self.assertEqual(MemorySearchTask(task.snapshot()).default_design, seed)

    def test_mapping_scope_candidate_identity_and_fixed_pool(self):
        spec = system_spec()
        task = MemorySearchTask(spec)
        invalid = task.default_design
        invalid["dram_address_mapping"]["groups"]["unapproved"] = {"version": 0, "operations": []}
        with self.assertRaises(CandidateError):
            task.normalize(invalid)
        moved = task.default_design
        moved["kv_layout"]["groups"][0]["placement"]["regions"].reverse()
        self.assertNotEqual(task.candidate_id(moved), task.candidate_id(task.default_design))
        spec["hardware"]["targets"]["d1"]["mapping_group"] = "independent"
        task = MemorySearchTask(spec)
        candidate = task.default_design
        candidate["dram_address_mapping"]["groups"]["independent"]["operations"] = [{"op": "xor_bit", "dst": 0, "src": 1}]
        self.assertEqual(task.check(task.normalize(candidate))["status"], "pass")
        spec["hardware"]["pools"]["memory"] = ["r0"]
        task = MemorySearchTask(spec)
        invalid = task.default_design
        invalid["kv_layout"]["groups"][0]["placement"]["regions"] = ["r1"]
        with self.assertRaises(CandidateError):
            task.normalize(invalid)

    def test_multiple_groups_in_one_region_keep_alignment_gaps_reserved(self):
        spec = system_spec()
        spec["workload"]["data"]["objects"]["K"] = dict(tokens=1, dimensions=3, element_bytes=2)
        spec["workload"]["data"]["events"].insert(0, {"op": "append", "object": "K", "count": 1})
        task = MemorySearchTask(spec)
        design = task.default_design
        design["kv_layout"]["groups"] = [group(["K"], ["r0"], 8), group(["V"], ["r0"], 16)]
        self.assertEqual(task.check(task.normalize(design))["status"], "pass")
        self.assertEqual(task.parse_layout(design["kv_layout"]).offsets(task.bounds)["V"][0], ("r0", 16))
        self.assertEqual(task.measure(design)["allocated_bytes_by_region"], {"r0": 32})
        spec["hardware"]["regions"]["r0"]["size_bytes"] = 24
        small = MemorySearchTask(spec)
        with self.assertRaises(CandidateError):
            small.normalize(design)

    def test_four_kib_object_interleave_and_resource_stripes_are_distinct(self):
        task_spec = MemorySearchTask.from_file("examples/module_search.json").snapshot()
        for shape in task_spec["workload"]["data"]["objects"].values():
            shape["tokens"] = 32  # Each object reserves 8 KiB, although only its prefix is written.
        task = MemorySearchTask(task_spec)
        design = task.default_design
        c = {"kind": "concat", "order": ["K", "V"], "alignment_bytes": 64}
        design["kv_layout"]["groups"] = [{"composition": c, "placement": {"regions": ["r0", "r1"], "stripe_bytes": 4096}}]
        for kind, expected in (("concat", [("r0", 0), ("r1", 0), ("r0", 4096), ("r1", 4096)]),
                               ("interleave", [("r0", 0), ("r0", 4096), ("r1", 0), ("r1", 4096)])):
            if kind == "interleave":
                design["kv_layout"]["groups"][0]["composition"] = dict(kind=kind, order=["K", "V"], chunk_bytes=4096)
            offsets = task.parse_layout(design["kv_layout"]).offsets(task.bounds)
            self.assertEqual([offsets[n][i] for n in ("K", "V") for i in (0, 2048)], expected)
            self.assertEqual(task.check(task.normalize(design))["status"], "pass")

    def test_invalid_topology_layouts_and_backend_capabilities(self):
        task = MemorySearchTask(system_spec())
        variants = []
        d = task.default_design; d["kv_layout"]["groups"].append(deepcopy(d["kv_layout"]["groups"][0])); variants.append(d)
        d = task.default_design; d["kv_layout"]["groups"][0]["placement"]["regions"] = ["r0", "r0"]; variants.append(d)
        d = task.default_design; d["kv_layout"]["groups"][0]["placement"]["stripe_bytes"] = True; variants.append(d)
        d = task.default_design; d["kv_layout"]["groups"][0]["composition"]["alignment_bytes"] = 16; variants.append(d)
        for design in variants:
            with self.subTest(design=design), self.assertRaises(CandidateError):
                task.normalize(design)
        for backend, metric in (("line-traffic-v2", "transferred_bytes"), ("fcfs-serial-kv-v2", "completion_cycles"),
                                ("ramulator", "completion_ps"), ("fcfs", "completion_cycles")):
            spec = system_spec(); spec["evaluation"] = dict(backend=backend, objective=metric)
            with self.subTest(backend=backend), self.assertRaises(ValueError):
                MemorySearchTask(spec)
        invalid = task.default_design
        invalid["kv_layout"]["objects"]["V"]["tile"]["token"] = 0
        with self.assertRaises(CandidateError) as error:
            task.normalize(invalid)
        self.assertEqual(error.exception.path, "$.dsl.kv_layout.objects.V.tile.token")
        spec = system_spec()
        spec["design_space"]["vary"] = ["kv_layout"]
        spec["fixed_design"] = {"dram_address_mapping": task.default_design["dram_address_mapping"]}
        spec["initial_design"] = task.default_design
        spec["initial_design"]["dram_address_mapping"]["groups"]["shared"]["operations"] = [{"op": "swap_bits", "a": 0, "b": 1}]
        with self.assertRaisesRegex(ValueError, "respect fixed_design"):
            MemorySearchTask(spec)

    def test_large_reserved_padding_is_checked_without_expanding_all_stripes(self):
        spec = system_spec()
        for target in spec["hardware"]["targets"].values():
            target["geometry"]["rows"] = 1 << 23
        for region in spec["hardware"]["regions"].values():
            region["size_bytes"] = 1 << 27
        spec["design_space"]["layout_options"]["chunk_bytes"].append(1 << 27)
        task = MemorySearchTask(spec)
        design = task.default_design
        design["kv_layout"]["groups"][0]["composition"] = dict(kind="interleave", order=["V"], chunk_bytes=1 << 27)
        self.assertEqual(task.check(task.normalize(design))["status"], "pass")
        self.assertEqual(task.measure(design)["allocated_bytes"], 1 << 27)

    def test_rtl_export_selects_a_declared_mapping_group(self):
        spec = system_spec()
        spec["hardware"]["targets"]["d1"]["mapping_group"] = "other"
        task = MemorySearchTask(spec)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save(root / "task.json", task.snapshot())
            save(root / "design.json", task.default_design)
            argv = ["memory-search", "export-rtl", str(root / "design.json"), "--task", str(root / "task.json"),
                    "--output", str(root / "rtl")]
            with patch("sys.argv", argv), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            with patch("sys.argv", argv + ["--mapping-group", "other"]), redirect_stdout(StringIO()) as output, \
                    patch("agent_design_search.__main__.export_rtl", return_value={"status": "not_run"}) as exporter:
                main()
            self.assertEqual(exporter.call_args.args[1], task.system.targets["d1"].geometry)
            self.assertIn('"mapping_group": "other"', output.getvalue())

    def test_corrupted_target_or_mask_fails_before_measurement(self):
        task = MemorySearchTask(system_spec())
        for corruption in ("target", "mask"):
            def broken(*args):
                for event, name, transfers in kv_transfers(*args):
                    first = transfers[0]
                    transfers[0] = (replace(first, target="d1", region="r1") if corruption == "target" else
                                    replace(first, access=replace(first.access, valid_bytes=(0,)), byte_tags=first.byte_tags[:1]))
                    yield event, name, transfers
            with patch("agent_design_search.execution.memory_system.kv_transfers", side_effect=broken):
                evaluator = Evaluator(task)
                with self.assertRaises(CandidateError) as error:
                    evaluator.evaluate(task.default_design)
            self.assertEqual(evaluator.backend_calls, 0)
            self.assertEqual(error.exception.code, "verification_failed")

    def test_eight_modules_round_trip_and_one_budget_per_complete_candidate(self):
        spec = system_spec(count=8)
        spec["workload"]["data"]["objects"]["V"]["dimensions"] = 32
        spec["workload"]["data"]["events"][1]["dimensions"] = [0, 32]
        task = MemorySearchTask(spec)
        self.assertEqual(MemorySearchTask(task.snapshot()).definition, task.definition)
        result = task.measure(task.default_design)
        self.assertEqual([v["transactions"] for v in result["per_target"].values()], [2] * 8)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "session"
            session_init(folder, task, task.budget, [("initial", task.default_design)])
            resumed = load_task(folder)
            design = resumed.default_design
            design["kv_layout"]["groups"][0]["placement"]["regions"] = ["r0", "r1", "r2", "r3"]
            entry = submit(folder, resumed, {"hypothesis": "fewer targets", "expected": "content preserved", "dsl": design})
            self.assertEqual(entry["backend_calls"], 2)
            self.assertEqual(submit(folder, resumed, {"hypothesis": "cached", "expected": "same", "dsl": design})["backend_calls"], 2)
            self.assertIn("best", session_finish(folder, resumed))


if __name__ == "__main__":
    unittest.main()
