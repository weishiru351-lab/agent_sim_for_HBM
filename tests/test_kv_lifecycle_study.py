"""Combined K/V workload contracts: capacity, layout, migration and generations."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from agent_design_search.json_io import read
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from experiments.kv_lifecycle_study import EXAMPLE, proposals, run


class KVLifecycleTests(unittest.TestCase):
    def test_partial_slots_and_recreated_generations_keep_content_and_capacity(self):
        task = MemorySearchTask.from_file(EXAMPLE)
        candidate = task.default_design
        check = task.check(candidate)
        self.assertEqual((check["status"], check["objects"], check["lifetime_events"]), ("pass", 4, 12))
        metrics = task.measure(candidate, True)
        self.assertEqual((metrics["migrations"], metrics["copied_bytes"]), (2, 128))
        # Workload: ten 32-byte token writes and twelve 32-byte token-equivalent reads.
        self.assertEqual(metrics["write_useful_bytes"] - metrics["copied_bytes"], 320)
        self.assertEqual(metrics["read_useful_bytes"] - metrics["copied_bytes"], 384)
        moves = [(e["object_id"], e["generation"], e["source_event"]) for e in metrics["migration_events"] if e["kind"] == "publish"]
        self.assertEqual(moves, [("K0", 1, 8), ("V0", 1, 9)])
        self.assertEqual(metrics["allocated_bytes"], 0)
        self.assertEqual(metrics["peak_allocated_bytes_by_region"], {"dram": 256, "nand": 128, "ssd": 0})
        leases = {(e["object_id"], e["generation"]): e["extents"] for e in metrics["allocation_events"] if e["kind"] == "reserve"}
        self.assertEqual(leases[("K0", 2)], leases[("K0", 1)])
        self.assertEqual(leases[("V0", 2)], leases[("V0", 1)])
        self.assertEqual(metrics["per_target"]["n0"]["invalid_pages"], 4)  # Release is not erase.
        self.assertEqual(metrics["per_target"]["n0"]["erases"], 0)
        # Requesting on_full for the open slots cannot move their unwritten tails.
        for name in ("K1", "V1"):
            candidate["migration_policy"]["objects"][name] = {"trigger": "on_full", "destination": "nand"}
        self.assertEqual(task.check(candidate)["migrations"], 2)
        self.assertEqual(task.measure(candidate)["copied_bytes"], 128)

    def test_column_layout_trades_more_dram_write_traffic_for_fewer_nand_reads(self):
        task = MemorySearchTask.from_file(EXAMPLE)
        candidate = proposals(task)["column_layout_k0"]["dsl"]
        self.assertEqual(task.check(candidate)["status"], "pass")
        row, column = task.measure(task.default_design), task.measure(candidate)
        for key in ("read_useful_bytes", "write_useful_bytes", "copied_bytes", "migrations", "peak_allocated_bytes"):
            self.assertEqual(row[key], column[key])
        # K0 full reads touch two NAND pages per dimension in row layout, one in column layout.
        self.assertEqual((row["per_target"]["n0"]["read_pages"], column["per_target"]["n0"]["read_pages"]), (34, 18))
        self.assertEqual(column["per_target"]["d0"]["transferred_bytes"] - row["per_target"]["d0"]["transferred_bytes"], 96)
        self.assertEqual(row["transferred_bytes"] - column["transferred_bytes"], 16 * 32 - 96)

    def test_static_placement_avoids_moves_but_writes_unfilled_tail_to_nand(self):
        task = MemorySearchTask.from_file(EXAMPLE)
        candidate = proposals(task)["static_nand"]["dsl"]
        self.assertEqual(task.check(candidate)["status"], "pass")
        metrics = task.measure(candidate)
        self.assertEqual((metrics["migrations"], metrics["copied_bytes"]), (0, 0))
        self.assertEqual((metrics["write_useful_bytes"], metrics["read_useful_bytes"]), (320, 384))
        self.assertEqual(metrics["per_target"]["n0"]["program_pages"], 6)
        self.assertEqual(metrics["peak_allocated_bytes_by_region"]["dram"], 256)

    def test_saved_sessions_freeze_same_work_and_reject_without_charging(self):
        original = EXAMPLE.read_bytes()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            output = Path(tmp) / "study"
            result = run(output)
            self.assertFalse(result["is_agent_experiment"])
            self.assertEqual(read(output / "study.json"), result)
            self.assertEqual(result["cases"]["tight"]["backend_calls"], 4)
            self.assertEqual(result["cases"]["roomy"]["backend_calls"], 7)
            tight_task = read(output / "tight/task.json")
            roomy_task = read(output / "roomy/task.json")
            self.assertEqual(tight_task["workload"], roomy_task["workload"])
            roomy_task["hardware"]["regions"]["dram"]["size_bytes"] = 256
            self.assertEqual(tight_task, roomy_task)
            for label in ("tight", "roomy"):
                feedback = read(output / label / "feedback.json")
                self.assertTrue(feedback["closed"])
                task = MemorySearchTask(read(output / label / "task.json"))
                self.assertEqual(task.definition, result["cases"][label]["task"])
                calls = 0
                for entry in feedback["history"]:
                    self.assertEqual(entry["actor"], "scripted_acceptance")
                    if entry["status"] == "ok":
                        self.assertEqual(entry["verification"]["status"], "pass")
                        self.assertEqual(task.candidate_id(task.normalize(entry["dsl"])), entry["candidate_id"])
                        calls += 1
                    else:
                        self.assertEqual(entry["status"], "invalid")
                    self.assertEqual(entry["backend_calls"], calls)
            rows = result["cases"]["tight"]["rows"]
            for name, event in (("reserve_at_create", 3), ("never", 11), ("before_read_nand", 11)):
                error = rows[name]["error_detail"]
                self.assertEqual(error["code"], "allocation_exhausted")
                self.assertEqual((error["details"]["object"], error["details"]["workload_event"]), ("V1", event))
            self.assertEqual(rows["shared_group_move"]["error_detail"]["code"], "unsupported_migration_layout")
            with self.assertRaises(FileExistsError): run(output)
        self.assertEqual(EXAMPLE.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
