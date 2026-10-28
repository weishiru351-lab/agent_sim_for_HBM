"""Scripted KV lifecycle acceptance, not an Agent search or a measured workload.

Keep events fixed; compare eight declared designs with 256 B / 384 B DRAM regions.
Full tasks, proposals, failures and charged evaluations use the normal file sessions.
Interpretation lives in docs/experiments.md.
"""

import argparse
from copy import deepcopy
import hashlib
from pathlib import Path

from agent_design_search.exploration.session import session_init, submit_file, session_finish
from agent_design_search.json_io import read, save
from agent_design_search.exploration.memory_search_task import MemorySearchTask


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/kv_lifecycle_search.json"


def proposals(task):
    """Deliberate single changes, plus the explicit static-placement alternative."""
    specs = [
        ("on_full_nand", "Reserve at first access, move K0/V0 to NAND when full.", "Free DRAM before K1/V1 need it; charge two copies."),
        ("reserve_at_create", "Reserve all groups at creation.", "256 B cannot hold 384 B of declared slots; 384 B can."),
        ("never", "Keep every object in DRAM throughout its lifetime.", "Fail with 256 B; complete with 384 B without copy traffic."),
        ("before_read_nand", "Delay K0/V0 moves until their first full-object reads.", "K1/V1 need DRAM before that read; delaying can fail with 256 B."),
        ("on_full_ssd", "Use the exposed SSD block region as the migration destination.", "Preserve contents; charge host block traffic rather than raw NAND actions."),
        ("column_layout_k0", "Store K0 by dimension while keeping its migration rule.", "Change append/read transactions, but not logical work, lifetime or copied bytes."),
        ("static_nand", "Initially place K0/V0 on NAND and disable their moves.", "Avoid copies and fit in 256 B DRAM; all original K0/V0 writes now hit NAND."),
        ("shared_group_move", "Combine K0/V0 into one group while retaining their move rules.", "Reject: whole-object migration currently requires singleton groups."),
    ]
    candidates = {name: task.default_design for name, _, _ in specs}
    candidates["reserve_at_create"]["allocation_policy"]["reserve_on"] = dict.fromkeys(task.bounds, "create")
    for rule in candidates["never"]["migration_policy"]["objects"].values():
        rule.clear(); rule["trigger"] = "never"
    for name in ("K0", "V0"):
        candidates["before_read_nand"]["migration_policy"]["objects"][name]["trigger"] = "before_read"
        candidates["on_full_ssd"]["migration_policy"]["objects"][name]["destination"] = "ssd"
        candidates["static_nand"]["migration_policy"]["objects"][name] = {"trigger": "never"}
    for group in candidates["static_nand"]["kv_layout"]["groups"]:
        if group["composition"]["order"][0] in ("K0", "V0"):
            group["placement"]["regions"] = ["nand"]
    candidates["column_layout_k0"]["kv_layout"]["objects"]["K0"]["tile"] = {"token": 2, "dimension": 1}
    layout = candidates["shared_group_move"]["kv_layout"]
    group = deepcopy(layout["groups"][0]); group["composition"]["order"] = ["K0", "V0"]
    layout["groups"] = [group, *[g for g in layout["groups"] if g["composition"]["order"][0] in ("K1", "V1")]]
    return {name: {"hypothesis": hypothesis, "expected": expected, "dsl": candidates[name]}
            for name, hypothesis, expected in specs}


def run(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    cases = {}
    for label, capacity in (("tight", 256), ("roomy", 384)):
        spec = read(EXAMPLE)
        spec["hardware"]["regions"]["dram"]["size_bytes"] = capacity
        task, folder = MemorySearchTask(spec), output / label
        session_init(folder, task, actor="scripted_acceptance")
        rows = {}
        for name, proposal in proposals(task).items():
            path = folder / "proposals" / (name + ".json")
            save(path, proposal)
            result = submit_file(folder, task, path)
            rows[name] = {k: result[k] for k in ("status", "backend_calls", "error_detail") if k in result}
            if result["status"] == "ok":
                metrics = result["metrics"]
                rows[name]["metrics"] = {k: metrics[k] for k in (
                    "completion_ps", "transferred_bytes", "peak_allocated_bytes", "peak_allocated_bytes_by_region",
                    "allocated_bytes", "migrations", "copied_bytes")}
            detail = (f"{result['metrics']['completion_ps']} ps" if result["status"] == "ok"
                      else result["error_detail"]["code"])
            print(f"{label}/{name}: {result['status']}, {detail}, calls={result['backend_calls']}", flush=True)
        closed = session_finish(folder, task)
        cases[label] = {"task": task.definition, "dram_bytes": capacity,
                        "backend_calls": closed["backend_calls"], "rows": rows}
    sources = [*sorted((ROOT / "agent_design_search").rglob("*.py")), Path(__file__).resolve(), EXAMPLE]
    result = {"version": 1, "kind": "lifecycle_acceptance", "is_agent_experiment": False,
              "source_hashes": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
              "cases": cases,
              "limits": ["synthetic fixed access groups, not a captured model trace or numerical attention",
                         "uncalibrated serial execution; full-object moves only, no layout conversion or GC",
                         "scripted candidates, no optimizer, no claim of optimality or Agent advantage"]}
    save(output / "study.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New output directory; never overwrite an existing run")
    args = parser.parse_args()
    run(args.output)


if __name__ == "__main__":
    main()
