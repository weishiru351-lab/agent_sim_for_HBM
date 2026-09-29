"""DSL semantics, synthetic requests, and a small event-driven memory model.

This is not a JEDEC timing model. Each channel dispatches FCFS, at most one
request per issue interval. A bank remains occupied through data completion;
bank preparation can overlap across banks, but transfers on a channel cannot.
Read and write service times are identical. No refresh, caches, bank groups,
pseudochannels, read/write turnaround, or compute dependencies are modeled.
"""

from collections import Counter, deque
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import random


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


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
            raise ValueError("Toy address width is limited to 24 block bits")

    @property
    def block_bits(self):
        return self.channel_bits + self.bank_bits + self.column_bits + self.row_bits

    @property
    def blocks(self):
        return 1 << self.block_bits

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Policy:
    # (op, first bit, second bit); operations read the previous operation's result.
    operations: tuple = ()

    @classmethod
    def parse(cls, obj, cfg):
        if not isinstance(obj, dict) or set(obj) != {"version", "operations"}:
            raise ValueError("DSL must contain exactly version and operations")
        if type(obj["version"]) is not int or obj["version"] != 0:
            raise ValueError("Only DSL version 0 is supported")
        ops = obj["operations"]
        if not isinstance(ops, list) or len(ops) > cfg.max_operations:
            raise ValueError("operations must be a list within the configured limit")
        result = []
        for op in ops:
            if not isinstance(op, dict):
                raise ValueError("Each operation must be an object")
            kind = op.get("op")
            if not isinstance(kind, str):
                raise ValueError("Operation name must be a string")
            fields = {"xor_bit": ("dst", "src"), "swap_bits": ("a", "b")}.get(kind)
            if fields is None or set(op) != {"op", *fields}:
                raise ValueError("Unknown operation or unexpected fields")
            a, b = (op[field] for field in fields)
            if any(type(bit) is not int or not 0 <= bit < cfg.block_bits for bit in (a, b)) or a == b:
                raise ValueError("Bits must be distinct integers within the block address")
            result.append((kind, a, b))
        return cls(tuple(result))

    def to_dict(self):
        return {"version": 0, "operations": [
            {"op": kind, **dict(zip(("dst", "src") if kind == "xor_bit" else ("a", "b"), (a, b)))}
            for kind, a, b in self.operations
        ]}

    def apply(self, block):
        for kind, a, b in self.operations:
            if kind == "xor_bit":
                block ^= ((block >> b) & 1) << a
            else:
                change = ((block >> a) ^ (block >> b)) & 1
                block ^= (change << a) | (change << b)
        return block

    def inverse(self, block):
        return Policy(tuple(reversed(self.operations))).apply(block)

    def masks(self, cfg):
        """Independent linear compilation; also canonicalizes equivalent DSLs."""
        masks = [1 << i for i in range(cfg.block_bits)]
        for kind, a, b in self.operations:
            if kind == "xor_bit":
                masks[a] ^= masks[b]
            else:
                masks[a], masks[b] = masks[b], masks[a]
        return tuple(masks)

    def decode(self, address, cfg):
        if type(address) is not int or not 0 <= address < cfg.blocks * cfg.granule_bytes:
            raise ValueError("Address out of range")
        block, offset = divmod(address, cfg.granule_bytes)
        mapped = self.apply(block)
        channel = mapped & ((1 << cfg.channel_bits) - 1)
        mapped >>= cfg.channel_bits
        bank = mapped & ((1 << cfg.bank_bits) - 1)
        mapped >>= cfg.bank_bits
        column = mapped & ((1 << cfg.column_bits) - 1)
        row = mapped >> cfg.column_bits
        return channel, bank, row, column, offset


def verify(policy, cfg):
    """Full-domain rank argument for this linear DSL, plus sampled implementation checks."""
    Policy.parse(policy.to_dict(), cfg)
    basis = {}
    for vector in policy.masks(cfg):
        while vector:
            pivot = vector.bit_length() - 1
            if pivot in basis:
                vector ^= basis[pivot]
            else:
                basis[pivot] = vector
                break
    rng = random.Random(0)
    points = {0, cfg.blocks - 1, *(1 << i for i in range(cfg.block_bits))}
    points.update(range(cfg.blocks) if cfg.blocks <= 1024 else (rng.randrange(cfg.blocks) for _ in range(1024)))
    masks = policy.masks(cfg)
    for block in points:
        mapped = sum(((block & mask).bit_count() & 1) << i for i, mask in enumerate(masks))
        if mapped != policy.apply(block) or policy.inverse(mapped) != block:
            return {"status": "fail", "counterexample_block": block}
    return {"status": "pass" if len(basis) == cfg.block_bits else "fail",
            "method": "GF(2) full-rank check plus interpreter/compiler and inverse checks",
            "rank": len(basis), "block_bits": cfg.block_bits,
            "implementation_samples": len(points), "rtl_status": "not_run"}


@dataclass(frozen=True)
class Request:
    request_id: int
    address: int
    arrival: int
    kind: str = "read"
    stream: int = 0


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


def features(requests, cfg):
    blocks = [req.address // cfg.granule_bytes for req in requests]
    transitions = [sum(((a ^ b) >> bit) & 1 for a, b in zip(blocks, blocks[1:])) / max(1, len(blocks) - 1)
                   for bit in range(cfg.block_bits)]
    return {"requests": len(requests), "block_bits": cfg.block_bits,
            "top_block_strides": Counter(b - a for a, b in zip(blocks, blocks[1:])).most_common(8),
            "bit_transition_rates": transitions,
            "bit_one_rates": [sum((b >> bit) & 1 for b in blocks) / len(blocks) for bit in range(cfg.block_bits)],
            "address_sample": [r.address for r in requests[:24]]}


def simulate(requests, policy, cfg, include_events=False):
    if not requests:
        raise ValueError("Workload must not be empty")
    Policy.parse(policy.to_dict(), cfg)
    if len({r.request_id for r in requests}) != len(requests):
        raise ValueError("Duplicate request IDs")
    channels, banks = 1 << cfg.channel_bits, 1 << cfg.bank_bits
    routed = [[] for _ in range(channels)]
    for req in requests:
        if type(req.arrival) is not int or req.arrival < 0 or req.kind not in {"read", "write"}:
            raise ValueError("Invalid request arrival or kind")
        channel, bank, row, column, offset = policy.decode(req.address, cfg)
        if offset:
            raise ValueError("Toy requests must be aligned, one-granule accesses")
        routed[channel].append((req, bank, row))
    stats = Counter()
    channel_counts = [len(items) for items in routed]
    bank_counts = [[0] * banks for _ in range(channels)]
    events = []
    for channel, items in enumerate(routed):
        if not items:
            continue
        items.sort(key=lambda item: (item[0].arrival, item[0].request_id))
        queue = deque()
        cursor, now, next_issue, bus_free = 0, items[0][0].arrival, 0, 0
        bank_free, open_rows = [0] * banks, [None] * banks
        while cursor < len(items) or queue:
            while cursor < len(items) and items[cursor][0].arrival <= now and len(queue) < cfg.queue_depth:
                queue.append((*items[cursor], now))
                cursor += 1
            if queue:
                req, bank, row, admitted = queue[0]
                ready = max(next_issue, bank_free[bank])
                if ready <= now:
                    queue.popleft()
                    state = "row_hits" if open_rows[bank] == row else ("row_empty" if open_rows[bank] is None else "row_conflicts")
                    setup = cfg.t_hit + (0 if state == "row_hits" else cfg.t_activate)
                    if state == "row_conflicts":
                        setup += cfg.t_precharge
                    transfer = max(now + setup, bus_free)
                    finish = transfer + cfg.t_transfer
                    open_rows[bank], bank_free[bank] = row, finish
                    bus_free, next_issue = finish, now + cfg.issue_interval
                    bank_counts[channel][bank] += 1
                    stats[state] += 1
                    events.append({"request_id": req.request_id, "channel": channel, "bank": bank,
                                   "arrival": req.arrival, "admitted": admitted, "issue": now,
                                   "transfer": transfer, "finish": finish, "row_state": state})
                    # Refill a just-freed queue slot at the same timestamp.
                    continue
                times = [ready]
            else:
                times = []
            if cursor < len(items) and len(queue) < cfg.queue_depth:
                times.append(max(now, items[cursor][0].arrival))
            later = [time for time in times if time > now]
            if not later:
                raise RuntimeError("Simulator failed to make forward progress")
            now = min(later)
    latencies = sorted(event["finish"] - event["arrival"] for event in events)
    cycles = max(e["finish"] for e in events) - min(r.arrival for r in requests)
    n = len(events)
    result = {"backend": "toy-fcfs-v1", "completed_requests": n, "completion_cycles": cycles,
              "avg_latency": sum(latencies) / n, "p95_latency": latencies[math.ceil(n * .95) - 1],
              "row_hit_rate": stats["row_hits"] / n, "row_conflict_rate": stats["row_conflicts"] / n,
              "row_empty_rate": stats["row_empty"] / n,
              "channel_requests": channel_counts, "bank_requests": bank_counts,
              "channel_transfer_utilization": [c * cfg.t_transfer / cycles for c in channel_counts],
              "avg_backpressure_wait": sum(e["admitted"] - e["arrival"] for e in events) / n,
              "avg_controller_wait": sum(e["issue"] - e["admitted"] for e in events) / n,
              "read_requests": sum(r.kind == "read" for r in requests),
              "write_requests": sum(r.kind == "write" for r in requests)}
    if include_events:
        result["events"] = sorted(events, key=lambda e: e["request_id"])
    return result
