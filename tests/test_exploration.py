from pathlib import Path
import ast
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.__main__ import load_task
from agent_design_search.contracts import Objective
from agent_design_search.exploration.session import session_finish, session_init, submit
from agent_design_search.exploration.search_algorithms import search, random_local_search
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.json_io import digest, read, save
from experiments.legacy_cases import AddressMappingTask, Config, workload
from agent_design_search.inputs.hardware import Hardware
from agent_design_search.inputs.workload import load_trace
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.spaces.dram_dsl import Policy
from tools.replay_history import verify_sources
from experiments.benchmark import run_mapping_search


class DependencyTests(unittest.TestCase):
    def test_core_is_domain_independent_and_never_imports_experiments(self):
        allowed = {'contracts': (), 'json_io': (), 'inputs': ('contracts', 'json_io', 'inputs'),
                   'exploration.evaluation': ('contracts',),
                   'exploration': ('json_io', 'contracts', 'exploration'),
                   'exploration.dram_mapping_candidates': ('inputs', 'spaces'),
                   'exploration.memory_search_task': ('contracts', 'inputs', 'json_io', 'execution', 'verification', 'performance', 'spaces'),
                   'spaces': ('contracts', 'json_io', 'inputs', 'spaces'),
                   'performance': ('contracts', 'inputs', 'json_io', 'performance'),
                   'execution': ('contracts',),
                   'execution.ssd': ('contracts', 'execution.nand'),
                   'execution.memory_system': ('contracts', 'inputs', 'performance', 'execution'),
                   'verification': ('contracts', 'inputs', 'execution', 'spaces', 'json_io', 'verification')}
        package = Path(__file__).resolve().parents[1] / 'agent_design_search'
        for path in package.rglob('*.py'):
            relative = path.relative_to(package).with_suffix('')
            local = '.'.join(relative.parts)
            layer = local if local in allowed else relative.parts[0]
            module = 'agent_design_search.' + local
            context = module.rpartition('.')[0]
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    name = importlib.util.resolve_name('.' * node.level + (node.module or ''), context) if node.level else node.module
                    imports = [name + '.' + alias.name for alias in node.names]
                else:
                    continue
                for name in imports:
                    self.assertNotIn(name.split('.')[0], {'experiments', 'results', 'tools', 'hbm_toy'})
                    if name.startswith('agent_design_search.') and layer in allowed:
                        target = name.removeprefix('agent_design_search.')
                        self.assertTrue(any(target == p or target.startswith(p.rstrip('.') + '.') for p in allowed[layer]),
                                        f'{relative} must not import {name}')


class WorkerTask:
    """Test substitute: no addresses, Policy class or completion_cycles metric."""

    definition = {"domain": "test_workers", "backend": "formula-v1"}
    objective = Objective("throughput", "maximize")

    def feedback(self):
        return {"workers": "integer in [1, 5]"}

    def normalize(self, candidate):
        if (not isinstance(candidate, dict) or set(candidate) != {"workers"}
                or type(candidate["workers"]) is not int or not 1 <= candidate["workers"] <= 5):
            raise ValueError("Invalid workers")
        return dict(candidate)

    def candidate_id(self, candidate):
        return digest([self.definition, candidate])

    def check(self, candidate):
        return {"status": "fail" if candidate["workers"] == 5 else "pass"}

    def measure(self, candidate):
        if candidate["workers"] == 3:
            raise RuntimeError("Backend failed")
        return {"throughput": float("nan") if candidate["workers"] == 4 else candidate["workers"] * 10}


class GenericSessionTests(unittest.TestCase):
    def test_semantic_cache_and_budget(self):
        cfg = Config()
        requests = workload("stride", cfg, 64)
        evaluator = Evaluator(AddressMappingTask(cfg, requests))
        first = evaluator.evaluate(Policy().to_dict())
        second = evaluator.evaluate(Policy((("xor_bit", 0, 6), ("xor_bit", 0, 6))).to_dict())
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(evaluator.backend_calls, 1)
        for method in ("random", "local", "activity_local", "packed_local"):
            a = run_mapping_search(method, cfg, requests, 6, 42)
            b = run_mapping_search(method, cfg, requests, 6, 42)
            self.assertEqual(a["backend_calls"], 6)
            self.assertEqual(a["best"]["candidate_id"], b["best"]["candidate_id"])
            self.assertEqual(a["best"]["metrics"], b["best"]["metrics"])

    def test_restart_cache_failures_and_maximizing_a_non_address_objective(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_init(tmp, WorkerTask(), budget=4)
            def attempt(workers):
                return submit(tmp, WorkerTask(), {"hypothesis": "test capacity", "expected": "more throughput",
                                                     "dsl": {"workers": workers}})
            self.assertEqual(attempt(True)["status"], "invalid")
            self.assertEqual(attempt(5)["backend_calls"], 0)  # Correctness failure, no execution.
            self.assertEqual(attempt(1)["backend_calls"], 1)
            self.assertTrue(attempt(1)["cached"])
            self.assertEqual(attempt(2)["best_value"], 20)
            self.assertEqual(attempt(3)["status"], "backend_error")
            self.assertEqual(attempt(4)["status"], "backend_error")  # Invalid metric also consumes a call.
            with self.assertRaisesRegex(ValueError, "exhausted"):
                attempt(2)
            result = session_finish(tmp, WorkerTask())
            self.assertEqual((result["backend_calls"], result["best"]["dsl"]), (4, {"workers": 2}))
            self.assertEqual(result, session_finish(tmp, WorkerTask()))
            changed = WorkerTask()
            changed.objective = Objective("throughput", "minimize")
            with self.assertRaisesRegex(ValueError, "Frozen task changed"):
                session_finish(tmp, changed)

    def test_search_honors_both_objective_directions(self):
        for direction, best in (("maximize", 2), ("minimize", 1)):
            task = WorkerTask()
            task.objective = Objective("throughput", direction)
            result = search(task, lambda i, _: {"workers": i + 1}, budget=2)
            self.assertEqual(result["best"]["dsl"], {"workers": best})

    def test_shared_random_and_local_algorithms_use_supplied_operators(self):
        for method in ("random", "local"):
            with self.subTest(method=method):
                mutate = lambda candidate, rng: {"workers": candidate["workers"] + 1}
                result = random_local_search(WorkerTask(), method, [{"workers": 1}],
                                             lambda rng: {"workers": 2}, mutate, budget=2, seed=9)
                self.assertEqual(result["best"]["metrics"], {"throughput": 20})
                self.assertEqual((result["backend_calls"], result["proposals"]), (2, 2))

    def test_proposal_cap_and_existing_session_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_init(tmp, WorkerTask(), budget=1)
            with self.assertRaisesRegex(ValueError, "already exists"):
                session_init(tmp, WorkerTask(), budget=1)
            for _ in range(10):
                submit(tmp, WorkerTask(), {})
            with self.assertRaisesRegex(ValueError, "Proposal limit"):
                submit(tmp, WorkerTask(), {})
            self.assertEqual(read(Path(tmp) / "state.json")["backend_calls"], 0)


class TraceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Hardware(channels=2, banks_per_channel=2, columns=2, rows=2)
        self.trace = self.root / "input.json"
        save(self.trace, {"version": 1, "requests": [
            {"request_id": i, "address": (i % 16) * 64, "arrival": i // 8} for i in range(32)]})

    def start(self, budget=4):
        folder = self.root / "session"
        spec = {"version": 1, "hardware": self.cfg.to_dict(),
                "workload": {"kind": "address_trace", "file": self.trace.name},
                "execution": {"kind": "fixed_arrivals"},
                "design_space": {"vary": ["dram_address_mapping"], "mapping_family": "permutation"},
                "evaluation": {"backend": "fcfs", "objective": "completion_cycles"}, "budget": budget}
        save(self.root / "search.json", spec)
        task = MemorySearchTask.from_file(self.root / "search.json")
        session_init(folder, task, seeds=[("common_initial_design", task.default_design)])
        return folder

    def submit(self, folder, proposal):
        return submit(folder, load_task(folder), proposal)

    def finish(self, folder):
        return session_finish(folder, load_task(folder))

    def proposal(self, ops):
        return {"hypothesis": "test channel selection", "expected": "different occupancy",
                "dsl": {"dram_address_mapping": {"version": 0, "operations": ops}}}

    def test_trace_validation_and_frozen_copy(self):
        bad = read(self.trace)
        bad["requests"][0]["address"] = True
        save(self.root / "bad.json", bad)
        with self.assertRaises(ValueError):
            load_trace(self.root / "bad.json", self.cfg)
        folder = self.start()
        save(self.trace, bad)  # Changing the original does not change the frozen session.
        self.assertEqual(read(folder / "feedback.json")["task_feedback"]["workload"]["requests"], 32)
        self.assertNotIn("kv_layout", load_task(folder).feedback()["dsl"])
        self.finish(folder)
        frozen = read(folder / "task.json")
        frozen["workload"]["data"]["requests"][0]["address"] = 64
        save(folder / "task.json", frozen)
        with self.assertRaisesRegex(ValueError, "Frozen task changed"):
            self.finish(folder)

    def test_cache_illegal_space_and_actual_execution_budget(self):
        folder = self.start(budget=2)
        self.assertEqual(self.submit(folder, self.proposal([]))["backend_calls"], 1)
        bad = self.submit(folder, self.proposal([{"op": "xor_bit", "dst": 0, "src": 1}]))
        self.assertEqual((bad["status"], bad["backend_calls"]), ("invalid", 1))
        bad_bits = self.submit(folder, self.proposal([{"op": "swap_bits", "a": 0, "b": 99}]))
        self.assertEqual(bad_bits["error_detail"]["code"], "invalid_bits")
        self.assertEqual(bad_bits["error_detail"]["path"], "$.dsl.dram_address_mapping.operations[0]")
        self.assertEqual(bad_bits["error_detail"]["details"]["maximum"], 3)
        proposal = self.root / "proposal.json"
        save(proposal, self.proposal([{"op": "swap_bits", "a": 0, "b": 1}]))
        self.assertEqual(self.submit(folder, read(proposal))["backend_calls"], 2)
        with self.assertRaises(ValueError):
            self.submit(folder, read(proposal))
        result = self.finish(folder)
        self.assertNotIn("holdout", result)
        self.assertEqual(result, self.finish(folder))
        self.assertNotIn("exact", read(folder / "feedback.json"))

    def test_backend_failure_consumes_budget_but_does_not_become_best(self):
        folder = self.start(budget=2)
        with patch("agent_design_search.exploration.memory_search_task.simulate", side_effect=RuntimeError("backend failed")), \
                patch("agent_design_search.exploration.evaluation.time.perf_counter", side_effect=[0., .25, 1., 3.]):
            entry = self.submit(folder, self.proposal([{"op": "swap_bits", "a": 0, "b": 1}]))
        self.assertEqual((entry["status"], entry["backend_calls"]), ("backend_error", 2))
        self.assertEqual((entry["validation_seconds"], entry["backend_seconds"]), (.25, 2.))
        self.assertIn("metrics", self.finish(folder)["best"])

    def test_historical_source_hashes_keep_original_newline_semantics(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            source = tree / "hbm_toy" / "model.py"
            source.parent.mkdir()
            source.write_bytes(b"first\r\nsecond\r\n")
            protocol = tree / "results" / "sample" / "protocol.json"
            raw_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            text_hash = hashlib.sha256(b"first\nsecond\n").hexdigest()
            save(protocol, {"version": 2, "source_hashes": {"model.py": raw_hash}})
            verify_sources(tree, "sample")
            source.write_bytes(b"first\nsecond\n")
            with self.assertRaisesRegex(ValueError, "source mismatch"):
                verify_sources(tree, "sample")
            save(protocol, {"version": 3, "source_hashes": {"hbm_toy/model.py": text_hash}})
            source.write_bytes(b"first\r\nsecond\r\n")
            verify_sources(tree, "sample")
            source.write_bytes(b"changed\r\nsecond\r\n")
            with self.assertRaisesRegex(ValueError, "source mismatch"):
                verify_sources(tree, "sample")


if __name__ == "__main__":
    unittest.main()
