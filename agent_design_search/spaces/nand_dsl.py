"""NAND page distribution and foreground garbage-collection strategy DSL."""

from agent_design_search.contracts import CandidateError


def parse_page_mapping(obj, targets, stripe_pages):
    if (not isinstance(obj, dict) or set(obj) != {"version", "targets"}
            or type(obj["version"]) is not int or obj["version"] != 1
            or not isinstance(obj["targets"], dict) or set(obj["targets"]) != set(targets)):
        raise CandidateError("invalid_nand_mapping", "$.dsl", "Use version=1 and exactly the task's NAND targets")
    result = {}
    for name, rule in sorted(obj["targets"].items()):
        path = f"$.dsl.targets.{name}"
        if (not isinstance(rule, dict) or set(rule) != {"axis_order", "stripe_pages"}
                or rule["axis_order"] not in (["channel", "die"], ["die", "channel"])
                or type(rule["stripe_pages"]) is not int or rule["stripe_pages"] not in stripe_pages):
            raise CandidateError("invalid_nand_mapping", path,
                                 "axis_order lists channel/die fastest first; stripe_pages must be allowed by the task")
        result[name] = {"axis_order": list(rule["axis_order"]), "stripe_pages": rule["stripe_pages"]}
    return {"version": 1, "targets": result}


def parse_gc_policy(obj, targets, options):
    if (not isinstance(obj, dict) or set(obj) != {"version", "targets"} or type(obj["version"]) is not int
            or obj["version"] != 1 or not isinstance(obj["targets"], dict) or set(obj["targets"]) != set(targets)):
        raise CandidateError("invalid_nand_gc", "$.dsl", "Use version=1 and exactly the controllable NAND targets")
    result = {}
    for name, rule in sorted(obj["targets"].items()):
        if (not isinstance(rule, dict) or set(rule) != {"trigger_free_blocks", "victim"}
                or type(rule["trigger_free_blocks"]) is not int
                or rule["trigger_free_blocks"] not in options["trigger_free_blocks"]
                or rule["victim"] not in options["victims"]):
            raise CandidateError("invalid_nand_gc", f"$.dsl.targets.{name}", "GC threshold and victim rule must be allowed")
        result[name] = dict(rule)
    return {"version": 1, "targets": result}
