"""Cross-layer engineering acceptance; fixed synthetic work, no Agent experiment."""

import argparse
from copy import deepcopy
import hashlib
from pathlib import Path

from agent_design_search.exploration.memory_search_task import MemorySearchTask
from agent_design_search.exploration.session import session_init, submit, session_finish
from agent_design_search.json_io import read, save
from experiments.kv_lifecycle_study import proposals as lifecycle_proposals


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples/kv_lifecycle_search.json"


def task_spec(capacity=256, repetitions=8):
    """Different lifetimes, prefix reads, early termination and repeated reuse."""
    spec = read(SOURCE)
    workload = spec["workload"]["data"]
    workload["events"] = workload["events"][:-1] * repetitions + [{"op": "end"}]
    workload["provenance"] = dict(kind="synthetic", source="memory_system_study.task_spec",
        source_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(), execution="serial_groups",
        note=f"{repetitions} repetitions of fixed lifecycle events; shapes scaled for independent byte checking; no model capture")
    hw = spec["hardware"]
    hw["regions"]["dram"]["size_bytes"] = capacity
    n = hw["targets"]["n0"]
    n["geometry"].update(channels=2, dies_per_channel=2, blocks_per_plane=3)
    n["management_bytes"] = 512
    hw["targets"]["s0"].update(nand={k: deepcopy(n[k]) for k in ("geometry", "logical_pages", "management_bytes")},
                               controller_access="open")
    spec["execution"]["nand"] = dict(kind="channel_die", buffer_pages=2, gc="policy")
    space = spec["design_space"]
    space["vary"] += ["nand_page_mapping", "nand_gc"]
    space["nand_options"] = dict(stripe_pages=[1, 2, 16])
    space["gc_options"] = dict(trigger_free_blocks=[1, 2], victims=["most_invalid", "oldest_reclaimable"])
    spec["evaluation"]["nand_parameters"]["s0"] = deepcopy(spec["evaluation"]["nand_parameters"]["n0"])
    spec["evaluation"]["ssd_parameters"]["s0"] = dict(command_ps=7, completion_ps=3, merge_ps=3)
    initial = spec["initial_design"]
    initial["nand_page_mapping"] = dict(version=1, targets={
        n: dict(axis_order=["channel", "die"], stripe_pages=1) for n in ("n0", "s0")})
    initial["nand_gc"] = dict(version=1, targets={
        n: dict(trigger_free_blocks=1, victim="most_invalid") for n in ("n0", "s0")})
    return spec


def proposals(task):
    result = lifecycle_proposals(task)
    for name, threshold, victim, stripe in (
            ("early_gc", 2, "most_invalid", 1),
            ("oldest_gc", 1, "oldest_reclaimable", 1),
            ("stripe_two", 1, "most_invalid", 2)):
        candidate = task.default_design
        for n in ("n0", "s0"):
            candidate["nand_gc"]["targets"][n].update(trigger_free_blocks=threshold, victim=victim)
            candidate["nand_page_mapping"]["targets"][n]["stripe_pages"] = stripe
        result[name] = dict(hypothesis="Change one declared NAND rule", expected="Charge changed physical operations", dsl=candidate)
    candidate = deepcopy(result["on_full_ssd"]["dsl"])
    candidate["nand_gc"]["targets"]["s0"]["trigger_free_blocks"] = 2
    result["ssd_early_gc"] = dict(hypothesis="Earlier SSD internal GC", expected="Separate host and NAND traffic", dsl=candidate)
    candidate = task.default_design
    candidate["nand_gc"]["targets"]["n0"]["trigger_free_blocks"] = 0
    result["invalid_gc"] = dict(hypothesis="Illegal threshold", expected="Reject before timing", dsl=candidate)
    return result


def run(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    cases = {}
    for label, capacity in (("tight", 256), ("roomy", 384)):
        task = MemorySearchTask(task_spec(capacity))
        folder = output / label
        session_init(folder, task, actor="scripted_acceptance")
        rows = {}
        for name, proposal in proposals(task).items():
            save(folder / "proposals" / (name + ".json"), proposal)
            result = submit(folder, task, proposal)
            rows[name] = {k: result[k] for k in ("status", "backend_calls", "metrics", "error_detail") if k in result}
            print(label, name, result["status"], result.get("metrics", {}).get("completion_ps", result.get("error_detail")), flush=True)
        calls = result["backend_calls"]
        repeated = submit(folder, task, proposals(task)["on_full_nand"])
        if not repeated.get("cached") or repeated["backend_calls"] != calls:
            raise AssertionError("Repeated complete candidate must hit the session cache")
        closed = session_finish(folder, task)
        cases[label] = dict(task=task.definition, backend_calls=closed["backend_calls"], rows=rows)
    sources = [*sorted((ROOT / "agent_design_search").rglob("*.py")), Path(__file__).resolve(),
               ROOT / "experiments/kv_lifecycle_study.py", SOURCE]
    result = dict(version=1, kind="cross_layer_acceptance", is_agent_experiment=False, cases=cases,
        source_hashes={p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        limits=["synthetic serial events, no real inference capture", "uncalibrated costs; foreground GC, no wear/TRIM",
                "detailed SSD globally serialized; host/internal traffic separately counted", "scripted designs, no search-quality claim"])
    save(output / "study.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args().output)
