"""Cross-layer outcomes and input provenance, not optimizer-quality tests."""

from pathlib import Path
import tempfile
import unittest

from agent_design_search.contracts import CandidateError
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.inputs.workload import import_kv, KVWorkload
from agent_design_search.json_io import save
from experiments.memory_system_study import task_spec, proposals


class CrossLayerTests(unittest.TestCase):
    def test_capacity_changes_feasibility_without_changing_work(self):
        tight, roomy = (MemorySearchTask(task_spec(capacity, repetitions=2)) for capacity in (256, 384))
        self.assertEqual(tight.workload.to_dict(), roomy.workload.to_dict())
        with self.assertRaises(CandidateError):
            Evaluator(tight).evaluate(proposals(tight)["never"]["dsl"])
        r = Evaluator(roomy).evaluate(proposals(roomy)["never"]["dsl"])
        self.assertEqual(r["metrics"]["migrations"], 0)
        for name in ("on_full_nand", "on_full_ssd", "column_layout_k0", "early_gc"):
            result = Evaluator(tight).evaluate(proposals(tight)[name]["dsl"])
            self.assertEqual(result["verification"]["status"], "pass")
            self.assertEqual(result["metrics"]["migrations"], 4)
            self.assertEqual(result["metrics"]["allocated_bytes"], 0)

    def test_provenance_survives_freeze_and_unsupported_capture_fields_are_rejected(self):
        spec = task_spec(repetitions=1)
        source = spec["workload"]["data"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.json"
            save(path, source)
            data = import_kv(path, "synthetic", "fixture", "Already serialized; no async timing retained")
            self.assertEqual(KVWorkload.parse(data).to_dict(), data)
            task = MemorySearchTask({**spec, "workload": dict(kind="kv_events", data=data)})
            self.assertEqual(task.snapshot()["workload"]["data"]["provenance"], data["provenance"])
            data["provenance"]["note"] = "changed conversion"
            changed = MemorySearchTask({**spec, "workload": dict(kind="kv_events", data=data)})
            self.assertNotEqual(task.definition, changed.definition)
            source["events"][0]["arrival_ps"] = 50
            save(path, source)
            with self.assertRaises(CandidateError):
                import_kv(path, "captured", "declared source", "Do not discard unknown timing")


if __name__ == "__main__":
    unittest.main()
