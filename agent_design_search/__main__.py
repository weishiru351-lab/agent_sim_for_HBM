"""Run an explicit memory search task: python -m agent_design_search --help."""

import argparse
import json
from pathlib import Path

from agent_design_search.contracts import CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.session import PROTOCOL, load_state, session_init, submit_file, session_finish
from agent_design_search.inputs.workload import KVWorkload, import_kv
from agent_design_search.json_io import read, save
from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.verification.dram_rtl import export_rtl


def load_task(folder):
    state = read(Path(folder) / "state.json")
    if state["protocol"] != PROTOCOL:
        raise ValueError("Historical session: replay with its original implementation")
    task = MemorySearchTask(read(Path(folder) / "task.json"))
    load_state(folder, task)
    return task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("session-init", help="Freeze search.json inputs and evaluate the common initial design")
    init.add_argument("folder", type=Path)
    init.add_argument("--task", required=True, type=Path)
    step = commands.add_parser("session-step", help="Evaluate one complete Agent proposal")
    step.add_argument("folder", type=Path)
    step.add_argument("--proposal", required=True, type=Path)
    finish = commands.add_parser("session-finish", help="Close the session and save its best design")
    finish.add_argument("folder", type=Path)
    evaluate = commands.add_parser("evaluate", help="Evaluate a complete design without creating a search session")
    evaluate.add_argument("design", type=Path)
    evaluate.add_argument("--task", required=True, type=Path)
    evaluate.add_argument("--include-events", action="store_true", help="Include actual transfers, byte origins and timing events")
    inspect = commands.add_parser("inspect-task", help="Validate inputs, allowed spaces and backend compatibility")
    inspect.add_argument("task", type=Path)
    workload = commands.add_parser("inspect-workload", help="Inspect the fixed serial KV event format")
    workload.add_argument("file", type=Path)
    capture = commands.add_parser("import-kv", help="Validate normalized serial KV events and freeze source provenance")
    capture.add_argument("file", type=Path)
    capture.add_argument("--kind", required=True, choices=["synthetic", "captured"])
    capture.add_argument("--source", required=True, help="Capture/generator source, model/version if applicable")
    capture.add_argument("--note", required=True, help="Conversion and serial execution assumptions")
    capture.add_argument("--output", required=True, type=Path)
    rtl = commands.add_parser("export-rtl", help="Export the mapping component of a complete design")
    rtl.add_argument("design", type=Path)
    rtl.add_argument("--task", required=True, type=Path)
    rtl.add_argument("--output", required=True, type=Path)
    rtl.add_argument("--mapping-group", help="Required when the hardware declares multiple mapping groups")
    args = parser.parse_args()
    try:
        if args.command == "import-kv":
            result = import_kv(args.file, args.kind, args.source, args.note)
            if args.output.exists():
                raise ValueError("Do not overwrite an imported workload")
            save(args.output, result)
        elif args.command == "inspect-workload":
            result = KVWorkload.parse(read(args.file)).summary()
        elif args.command in {"session-step", "session-finish"}:
            task = load_task(args.folder)
            result = (submit_file(args.folder, task, args.proposal) if args.command == "session-step"
                      else session_finish(args.folder, task))
        else:
            task = MemorySearchTask.from_file(args.task)
            if args.command == "session-init":
                result = session_init(args.folder, task, task.budget,
                                      [("common_initial_design", task.default_design)])
            elif args.command == "inspect-task":
                result = task.feedback()
            else:
                design = task.normalize(read(args.design))
                if args.command == "evaluate":
                    result = Evaluator(task, task.budget, measure=lambda d: task.measure(d, args.include_events)).evaluate(design)
                else:
                    mappings = task.parse_mappings(design.get("dram_address_mapping"))
                    if not mappings:
                        raise ValueError("This task has no DRAM mapping to export")
                    group = args.mapping_group or (next(iter(mappings)) if len(mappings) == 1 else None)
                    if group not in mappings:
                        raise ValueError(f"Select --mapping-group from {sorted(mappings)}")
                    geometry = task.system.mapping_geometries[group] if task.system else task.hardware
                    result = export_rtl(mappings[group], geometry, args.output)
                    if task.system:
                        result["mapping_group"] = group
                        result["targets"] = [n for n, t in task.system.targets.items()
                                             if t.medium == "dram" and t.mapping_group == group]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except CandidateError as error:
        parser.exit(2, json.dumps({"error": error.to_dict()}, ensure_ascii=False) + "\n")
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
