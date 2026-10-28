"""Abstract block service or detailed SSD interface plus shared NAND timing.

BlockTiming caps concurrent commands with fixed block costs. NANDSSDTiming
serializes host commands and charges their internal NAND action chains.
External data transfer and host merging are charged by memory_system.py.
"""

from dataclasses import asdict, dataclass, replace
import heapq
from agent_design_search.performance.nand import NANDParallelTiming


@dataclass(frozen=True)
class BlockParameters:
    read_ps: int
    write_ps: int
    queue_depth: int
    merge_ps: int

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in asdict(self).values()) or self.queue_depth > 65536:
            raise ValueError("SSD timings must be positive integer ps; queue_depth must be 1..65536")


@dataclass(frozen=True)
class SSDInterfaceParameters:
    command_ps: int
    completion_ps: int
    merge_ps: int

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in asdict(self).values()):
            raise ValueError("Detailed SSD interface costs must be positive integer ps")


class BlockTiming:
    def __init__(self, target, parameters):
        self.target, self.parameters = target, parameters
        self.completed, self.events, self.ids = 0, [], set()
        self.counts = dict(read_commands=0, write_commands=0, read_blocks=0, write_blocks=0,
                           transferred_bytes=0, service_ps=0, queue_wait_ps=0, peak_outstanding=0)

    def submit_batch(self, commands):
        geometry = self.target.geometry
        keys = [(c.request.request_id, c.request.kind) for c in commands]
        if not commands or len(set(keys)) != len(keys) or self.ids.intersection(keys):
            raise ValueError("SSD commands need distinct request ID/kind pairs")
        for cmd in commands:
            r = cmd.request
            if (type(r.request_id) is not int or r.request_id < 0 or r.kind not in ("read", "write")
                    or type(r.arrival) is not int or r.arrival < self.completed
                    or type(cmd.lba) is not int or cmd.lba < 0 or type(cmd.blocks) is not int
                    or not 1 <= cmd.blocks <= self.target.max_transfer_blocks
                    or cmd.lba + cmd.blocks > geometry.logical_blocks
                    or type(r.address) is not int or r.address != cmd.lba * geometry.block_bytes):
                raise ValueError("SSD commands must be aligned, within capacity/transfer limits and in a drained batch")
        self.ids.update(keys)
        pending = []
        for cmd in sorted(commands, key=lambda c: (c.request.arrival, c.request.request_id)):
            r = cmd.request
            admitted = r.arrival
            while pending and pending[0] <= admitted:
                heapq.heappop(pending)
            if len(pending) == self.parameters.queue_depth:
                admitted = heapq.heappop(pending)
                while pending and pending[0] <= admitted:
                    heapq.heappop(pending)
            duration = cmd.blocks * getattr(self.parameters, r.kind + "_ps")
            finish = admitted + duration
            heapq.heappush(pending, finish)
            self.counts[r.kind + "_commands"] += 1
            self.counts[r.kind + "_blocks"] += cmd.blocks
            self.counts["transferred_bytes"] += cmd.blocks * geometry.block_bytes
            self.counts["service_ps"] += duration
            self.counts["queue_wait_ps"] += admitted - r.arrival
            self.counts["peak_outstanding"] = max(self.counts["peak_outstanding"], len(pending))
            self.events.append({"command_id": len(self.events), "request_id": r.request_id, "kind": r.kind,
                                "lba": cmd.lba, "blocks": cmd.blocks, "arrival": r.arrival,
                                "admitted": admitted, "issue": admitted, "finish": finish})
            self.completed = max(self.completed, finish)
        return self.completed


class NANDSSDTiming:
    """One host command at a time; pages within it use the shared NAND scheduler."""
    def __init__(self, target, parameters, nand_parameters, execution):
        self.target, self.parameters = target, parameters
        self.nand = NANDParallelTiming(target.nand.geometry, nand_parameters, execution["buffer_pages"], execution.get("gc"))
        self.completed, self.events, self.ids = 0, [], set()
        self.counts = dict(read_commands=0, write_commands=0, read_blocks=0, write_blocks=0,
                           transferred_bytes=0, interface_ps=0, queue_wait_ps=0, peak_outstanding=0)

    def submit_batch(self, commands, chains):
        keys = [(c.request.request_id, c.request.kind) for c in commands]
        if not keys or len(set(keys)) != len(keys) or self.ids.intersection(keys):
            raise ValueError("Detailed SSD requires distinct host request ID/kind pairs")
        for command in commands:
            req = command.request
            if (req.kind not in ("read", "write") or type(req.arrival) is not int or req.arrival < self.completed
                    or command.blocks != 1 or not 0 <= command.lba < self.target.geometry.logical_blocks
                    or req.address != command.lba * self.target.geometry.block_bytes
                    or not chains.get(req.request_id)):
                raise ValueError("Detailed SSD requires whole single-block commands with explicit NAND chains")
        self.ids.update(keys)
        for command in sorted(commands, key=lambda c: c.request.request_id):
            req = command.request
            start = max(req.arrival, self.completed)
            ready = start + self.parameters.command_ps
            pages = chains[req.request_id]
            before = len(self.nand.operations)
            requests = [replace(p.access, request=replace(p.access.request, arrival=ready)) for p in pages]
            self.nand.submit_batch(requests, {p.access.request.request_id: p.actions for p in pages})
            for op in self.nand.operations[before:]:
                op["host_request_id"] = req.request_id
                op["host_kind"] = req.kind
            self.completed = self.nand.completed + self.parameters.completion_ps
            self.counts[req.kind + "_commands"] += 1
            self.counts[req.kind + "_blocks"] += 1
            self.counts["transferred_bytes"] += self.target.geometry.block_bytes
            self.counts["interface_ps"] += self.parameters.command_ps + self.parameters.completion_ps
            self.counts["queue_wait_ps"] += start - req.arrival
            self.counts["peak_outstanding"] = 1
            self.events.append({"request_id": req.request_id, "kind": req.kind, "lba": command.lba,
                                "arrival": req.arrival, "admitted": start, "issue": ready, "finish": self.completed})
        return self.completed
