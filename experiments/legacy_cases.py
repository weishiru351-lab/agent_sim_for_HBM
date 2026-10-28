"""Historical fixture parameters and score identities, outside the production API.

These adapters replay existing experiments through the current execution tools.
New tasks use MemorySearchTask and explicit JSON inputs.
"""

from dataclasses import asdict, dataclass, fields
import random

from agent_design_search.contracts import Request, Objective
from agent_design_search.inputs.workload import KVWorkload, statistics
from agent_design_search.json_io import digest
from agent_design_search.execution.memory_system import route_requests
from agent_design_search.verification.memory_contents import reference_object_offsets
from agent_design_search.performance.dram import FCFSParameters, simulate
from agent_design_search.spaces.dram_dsl import Policy, verify_mapping
from agent_design_search.spaces.kv_layout_dsl import Layout


LAYOUT_BACKEND = "line-traffic-v1"


def simulate_mapping(requests, mapping, cfg, include_events=False):
    """Historical experiment bridge; the production task passes separated inputs."""
    Policy.parse(mapping.to_dict(), cfg, cfg.max_operations)
    parameters = FCFSParameters(**{f.name: getattr(cfg, f.name) for f in fields(FCFSParameters)})
    return simulate(route_requests(requests, mapping, cfg), cfg, parameters, include_events)


@dataclass(frozen=True)
class Config:
    granule_bytes: int = 64
    channel_bits: int = 2
    bank_bits: int = 2
    column_bits: int = 4
    row_bits: int = 8
    queue_depth: int = 16
    issue_interval: int = 1
    t_hit: int = 4
    t_activate: int = 8
    t_precharge: int = 8
    t_transfer: int = 4
    max_operations: int = 8

    def __post_init__(self):
        for key, value in asdict(self).items():
            minimum = 0 if key in {"channel_bits", "bank_bits", "column_bits", "max_operations"} else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"Invalid configuration: {key}")
        if self.granule_bytes & (self.granule_bytes - 1):
            raise ValueError("granule_bytes must be a power of two")
        if self.block_bits > 24:
            raise ValueError("Address width is currently limited to 24 block bits")

    @property
    def block_bits(self):
        return self.channel_bits + self.bank_bits + self.column_bits + self.row_bits

    @property
    def blocks(self):
        return 1 << self.block_bits

    def to_dict(self):
        return asdict(self)


WORKLOADS = ("sequential", "stride", "kv_interleaved", "random")


def workload(name, cfg, count=768, seed=0, holdout=False):
    if name not in WORKLOADS or count < 1:
        raise ValueError("Unknown workload or non-positive request count")
    rng = random.Random(seed + (100003 if holdout else 0))
    result = []
    for i in range(count):
        stream = 0
        if name == "sequential":
            block = i + (32 if holdout else 0)
        elif name == "stride":
            block = i * (96 if holdout else 64) + (16 if holdout else 0)
        elif name == "kv_interleaved":
            streams = 6 if holdout else 8
            stream, token = i % streams, i // streams
            # Synthetic lane/page spacing, not a trace from an actual LLM kernel.
            block = stream * (3072 if holdout else 4096) + (token % 256) * (24 if holdout else 16)
        else:
            block = rng.randrange(cfg.blocks)
        result.append(Request(i, (block % cfg.blocks) * cfg.granule_bytes,
                              i // 8, "write" if i % 16 == 15 else "read", stream))
    return result


@dataclass(frozen=True)
class LayoutConfig:
    tokens: int = 16
    dimensions: int = 128
    element_bytes: int = 2
    line_bytes: int = 64
    capacity_bytes: int = 4096
    read_every: int = 16  # 0: writes only; otherwise also read at the final append.

    def __post_init__(self):
        for key, value in asdict(self).items():
            if type(value) is not int or value < (0 if key == "read_every" else 1):
                raise ValueError(f"Invalid layout configuration: {key}")
        if any(v & (v - 1) for v in (self.element_bytes, self.line_bytes)) or self.element_bytes > self.line_bytes:
            raise ValueError("Element and line sizes must be powers of two, with element <= line")
        if self.tokens > 64 or self.dimensions > 256:
            raise ValueError("Reference executor supports at most 64 tokens and 256 dimensions")
        minimum = ((self.tokens * self.dimensions * self.element_bytes + self.line_bytes - 1)
                   // self.line_bytes) * self.line_bytes
        if self.capacity_bytes < minimum:
            raise ValueError("Capacity cannot hold even the unpadded, line-aligned block")


def regular_workload(tokens, dimensions, element_bytes, read_every):
    """Expand the historical append/prefix-read fixture into the same event semantics."""
    events = []
    for token in range(tokens):
        events.append({"op": "append", "count": 1})
        if read_every and ((token + 1) % read_every == 0 or token + 1 == tokens):
            events.append({"op": "read", "tokens": [0, token + 1], "dimensions": [0, dimensions], "group_by": "dimension"})
    return KVWorkload.parse({"version": 1, "tensor": {"tokens": tokens, "dimensions": dimensions, "element_bytes": element_bytes},
                             "events": [*events, {"op": "end"}]})


class AddressMappingTask:
    """Old score identity and single-component envelope, for archived comparisons only."""

    objective = Objective("completion_cycles")

    def __init__(self, cfg, requests, space="linear"):
        if space not in {"linear", "permutation"} or not requests:
            raise ValueError("A mapping task requires requests and a known search space")
        self.cfg, self.requests, self.space = cfg, tuple(requests), space
        self.trace_id = digest([asdict(r) for r in self.requests])

    @property
    def definition(self):
        return {"domain": "dram_address_mapping", "config": self.cfg.to_dict(), "space": self.space,
                "trace_id": self.trace_id, "backend": "toy-fcfs-v1"}

    def feedback(self):
        return {"features": statistics(self.requests, self.cfg),
                "decode_order_low_to_high": ["channel", "bank", "column", "row"],
                "dsl_schema": {"version": 0, "operations": []},
                "rules": "Sequential swap_bits(a,b) and xor_bit(dst,src); distinct block bits; "
                         "byte offset unchanged. Respect max_operations; permutation permits swaps only.",
                "limits": "Simplified FCFS timing, fixed trace, static bijection; no held-out claim."}

    def normalize(self, candidate):
        policy = Policy.parse(candidate, self.cfg, self.cfg.max_operations)
        if self.space == "permutation" and any(op[0] != "swap_bits" for op in policy.operations):
            raise ValueError("This task permits swap_bits only; XOR is outside its search space")
        return policy.to_dict()

    def candidate_id(self, candidate):
        # Keep original measurement identity: unchanged inputs/backend, unchanged scores.
        return digest({"masks": Policy.parse(candidate, self.cfg, self.cfg.max_operations).masks(self.cfg),
                       "config": self.cfg.to_dict(), "trace": self.trace_id, "backend": "toy-fcfs-v1"})

    def check(self, candidate):
        return verify_mapping(Policy.parse(candidate, self.cfg, self.cfg.max_operations), self.cfg)

    def measure(self, candidate):
        return simulate_mapping(self.requests, Policy.parse(candidate, self.cfg, self.cfg.max_operations), self.cfg)


def verify_contents(layout, cfg, groups, written_tokens=None):
    """Independent nested-loop reference for placement; tagged writes/reads check content.

    Exhaustive for this finite task, including padding. No numerical attention,
    concurrent execution, migration or RTL correctness is implied.
    """
    offsets, expected = layout.offsets(cfg), reference_object_offsets(layout, cfg)
    if len(offsets) != len(expected):
        return {"status": "fail", "reason": "element_count_mismatch", "expected": len(expected), "actual": len(offsets)}
    for index, actual in enumerate(offsets):
        if actual != expected[index]:
            return {"status": "fail", "reason": "placement_mismatch", "element": list(divmod(index, cfg.dimensions)),
                    "expected_address": expected[index], "actual_address": actual}
    memory, read_elements = {}, 0
    for event, (kind, indices) in enumerate(groups):
        for index in indices:
            address = offsets[index]
            tag = index + 1  # Symbolic content, not numeric BF16 arithmetic.
            if kind == "write":
                memory[address] = tag
            elif memory.get(address) != tag:
                return {"status": "fail", "reason": "content_mismatch", "event": event,
                        "element": list(divmod(index, cfg.dimensions)), "address": address,
                        "expected_value": tag, "actual_value": memory.get(address)}
            else:
                read_elements += 1
    # Early end leaves reserved slots unwritten. Never silently manufacture data.
    written = (cfg.tokens if written_tokens is None else written_tokens) * cfg.dimensions
    expected_writes = {i: address for i, address in expected.items() if i < written}
    if len(memory) != len(expected_writes) or any(memory.get(address) != i + 1 for i, address in expected_writes.items()):
        return {"status": "fail", "reason": "final_content_mismatch"}
    return {"status": "pass", "method": "exhaustive nested-loop placement and tagged logical execution",
            "elements": len(expected), "read_elements": read_elements, "groups": len(groups)}


class KVLayoutTask:
    objective = Objective("transferred_bytes")

    def __init__(self, cfg):
        self.cfg = cfg
        self.workload = regular_workload(cfg.tokens, cfg.dimensions, cfg.element_bytes, cfg.read_every)
        expanded = tuple(self.workload.groups())
        self.groups = tuple((kind, indices) for _, _, kind, indices in expanded)
        self.group_events = tuple(event for event, _, _, _ in expanded)
        self.definition = {"domain": "kv_layout", "version": 1, "config": asdict(cfg), "backend": LAYOUT_BACKEND,
                           "executor": "append-vector_then_dimension-prefix-v1"}

    def feedback(self):
        return {"shape": [self.cfg.tokens, self.cfg.dimensions], "groups": len(self.groups),
                "read_groups": sum(kind == "read" for kind, _ in self.groups),
                "objective": "Minimize full-line transferred bytes; no cache or timing model",
                "dsl": {"version": 1, "tile": "token/dimension positive sizes within shape",
                        "order": "permutation of token_outer, dimension_outer, token_inner, dimension_inner; slow to fast"},
                "constraints": "Static, padded allocation at byte 0; masked writes; fixed serial logical groups",
                "workload": self.workload.summary()}

    def normalize(self, candidate):
        return Layout.parse(candidate, self.cfg).to_dict()

    def candidate_id(self, candidate):
        layout = Layout.parse(candidate, self.cfg)
        # Equal scores or line footprints alone do not establish semantic equivalence.
        return digest([self.definition, layout.offsets(self.cfg), layout.allocated_bytes(self.cfg)])

    def check(self, candidate):
        result = verify_contents(Layout.parse(candidate, self.cfg), self.cfg, self.groups, self.workload.written_tokens["V"])
        if result["status"] == "fail" and "event" in result:
            result["workload_event"] = self.group_events[result["event"]]
        return result

    def measure(self, candidate):
        layout = Layout.parse(candidate, self.cfg)
        offsets = layout.offsets(self.cfg)
        # Historical traffic-only profile: coalesce within each group, without a cache.
        counts = {f"{kind}_{suffix}": 0 for kind in ("read", "write") for suffix in ("transactions", "useful_bytes")}
        for kind, indices in self.groups:
            counts[f"{kind}_transactions"] += len({offsets[index] // self.cfg.line_bytes for index in indices})
            counts[f"{kind}_useful_bytes"] += len(indices) * self.cfg.element_bytes
        transfers = counts["read_transactions"] + counts["write_transactions"]
        return {"backend": LAYOUT_BACKEND, **counts, "transferred_bytes": transfers * self.cfg.line_bytes,
                "allocated_bytes": layout.allocated_bytes(self.cfg)}
