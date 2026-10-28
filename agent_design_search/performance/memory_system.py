"""Serial groups, medium/device targets and shared bidirectional links, in ps.

Writes traverse host-to-target links before device service; reads return through
the reverse path after service. Link occupancy includes its latency.
No command packets, clock-edge rounding or overlap between logical groups.
"""

from dataclasses import asdict, replace
import heapq
import time

from agent_design_search.performance.dram import FCFS, FCFSParameters
from agent_design_search.performance.nand import NANDTiming, NANDParallelTiming
from agent_design_search.performance.ssd import BlockTiming, NANDSSDTiming
from agent_design_search.contracts import BlockIO


class SystemFCFS:
    def __init__(self, system, parameters, nand_parameters=None, ssd_parameters=None, nand_execution=None):
        self.system = system
        self.engines = {}
        for name, target in system.targets.items():
            if target.medium == "nand":
                self.engines[name] = (NANDParallelTiming(target.geometry, nand_parameters[name], nand_execution["buffer_pages"],
                                                        nand_execution.get("gc"))
                                      if nand_execution else NANDTiming(target.geometry, nand_parameters[name]))
                continue
            if target.medium == "ssd":
                self.engines[name] = (NANDSSDTiming(target, ssd_parameters[name], nand_parameters[name], nand_execution)
                                      if target.nand else BlockTiming(target, ssd_parameters[name]))
                continue
            params = {k: v if k == "queue_depth" else v * target.clock_period_ps
                      for k, v in asdict(parameters).items()}
            self.engines[name] = FCFS(target.geometry, FCFSParameters(**params))
        self.link_free = dict.fromkeys(system.links, 0)
        self.link_stats = {n: dict(transfers=0, transferred_bytes=0, busy_ps=0, queue_wait_ps=0) for n in system.links}
        self.target_stats = {n: dict(transactions=0, transferred_bytes=0, backend_batches=0, backend_seconds=0.0)
                             for n in system.targets}
        self.ready, self.link_events, self.group_events = 0, [], []
        self.host_events = []

    def _transport(self, transfers, arrivals, reverse, phase=""):
        pending, completed = [], {}
        paths = {}
        for target, access in transfers:
            request_id = access.request.request_id
            path = self.system.targets[target].links
            paths[request_id] = tuple(reversed(path)) if reverse else path
            size = self.system.targets[target].geometry.granule_bytes
            heapq.heappush(pending, (arrivals[request_id], request_id, 0, target, size))
        while pending:
            arrival, request_id, hop, target, size = heapq.heappop(pending)
            path = paths[request_id]
            if hop == len(path):
                completed[request_id] = arrival
                continue
            name = path[hop]
            duration = self.system.links[name].duration(size)
            start = max(arrival, self.link_free[name])
            finish = start + duration
            self.link_free[name] = finish
            stats = self.link_stats[name]
            stats["transfers"] += 1
            stats["transferred_bytes"] += size
            stats["busy_ps"] += duration
            stats["queue_wait_ps"] += start - arrival
            self.link_events.append({"request_id": request_id, "target": target, "link": name,
                                     "direction": "read_return" if reverse else "write_data",
                                     "arrival_ps": arrival, "start_ps": start, "finish_ps": finish})
            if phase:
                self.link_events[-1]["phase"] = phase
            heapq.heappush(pending, (finish, request_id, hop + 1, target, size))
        return completed

    def submit_batch(self, transfers, nand_actions=None, block_actions=None):
        """Detailed SSD commands retain functional order, including host RMWs.

        Then serialize abstract SSD RMWs before other transfers (legacy policy).
        These global barriers are model assumptions, not hardware requirements.
        """
        kinds = {a.request.kind for _, a in transfers}
        if len(kinds) != 1 or not kinds <= {"read", "write"}:
            raise ValueError("A system batch must be one nonempty logical read or write group")
        start, remaining, partial, detailed = self.ready, [], [], []
        for target, access in transfers:
            if self.system.targets[target].medium != "ssd":
                remaining.append((target, access))
                continue
            width = self.system.targets[target].geometry.block_bytes
            lba = access.request.address // width
            rmw = access.request.kind == "write" and len(access.valid_bytes) < width
            expected = ["read", "merge", "write"] if rmw else [access.request.kind]
            actual = (block_actions or {}).get(access.request.request_id, [])
            if [(a.kind, a.lba) for a in actual] != [(k, lba) for k in expected]:
                raise ValueError("SSD timing requires the complete host command/merge chain")
            if self.system.targets[target].nand:
                detailed.append((target, access))
            else:
                (partial if rmw else remaining).append((target, access))
        ordered = sorted(detailed, key=lambda x: x[1].request.request_id) + sorted(partial, key=lambda x: x[1].request.request_id)
        for target, access in ordered:
            width = self.system.targets[target].geometry.block_bytes
            if access.request.kind != "write" or len(access.valid_bytes) == width:
                self._run_batch([(target, access)], block_actions=block_actions)
                continue
            read = replace(access, request=replace(access.request, kind="read"))
            self._run_batch([(target, read)], phase="rmw_read", block_actions=block_actions)
            finish = self.ready + self.engines[target].parameters.merge_ps
            self.host_events.append({"target": target, "request_id": access.request.request_id,
                                     "kind": "merge", "start_ps": self.ready, "finish_ps": finish})
            self.ready = finish
            self._run_batch([(target, access)], phase="rmw_write", block_actions=block_actions)
        if remaining:
            self._run_batch(remaining, nand_actions, block_actions=block_actions)
        self.group_events.append({"group": len(self.group_events), "kind": next(iter(kinds)),
                                  "start_ps": start, "finish_ps": self.ready})
        return self.ready

    def _run_batch(self, transfers, nand_actions=None, phase="", block_actions=None):
        kinds = {access.request.kind for _, access in transfers}
        if len(kinds) != 1:
            raise ValueError("A system batch must be one nonempty logical read or write group")
        kind = next(iter(kinds))
        start = self.ready
        arrivals = {d.request.request_id: start for _, d in transfers}
        if kind == "write":
            arrivals = self._transport(transfers, arrivals, reverse=False, phase=phase)
        media_done = {}
        for target, engine in self.engines.items():
            batch = [replace(access, request=replace(access.request, arrival=arrivals[access.request.request_id]))
                     for name, access in transfers if name == target]
            if not batch:
                continue
            before = len(engine.events)
            started = time.perf_counter()
            if self.system.targets[target].medium == "nand":
                engine.submit_batch(batch, nand_actions)
            elif self.system.targets[target].medium == "ssd":
                width = self.system.targets[target].geometry.block_bytes
                commands = [BlockIO(a.request, a.request.address // width) for a in batch]
                if self.system.targets[target].nand:
                    chains = {a.request.request_id: next(op.pages for op in block_actions[a.request.request_id]
                                                         if op.kind == a.request.kind) for a in batch}
                    engine.submit_batch(commands, chains)
                else:
                    engine.submit_batch(commands)
            else:
                engine.submit_batch(batch)
            if phase:
                for event in engine.events[before:]:
                    event["phase"] = phase
            self.target_stats[target]["backend_seconds"] += time.perf_counter() - started
            media_done.update({e["request_id"]: e["finish"] for e in engine.events[before:]})
            stats = self.target_stats[target]
            stats["transactions"] += len(batch)
            stats["transferred_bytes"] += len(batch) * self.system.targets[target].geometry.granule_bytes
            stats["backend_batches"] += 1
        done = self._transport(transfers, media_done, reverse=True, phase=phase) if kind == "read" else media_done
        self.ready = max(done.values())
        return self.ready

    def metrics(self, include_events=False):
        targets = {}
        for name, engine in self.engines.items():
            details = (engine.counts if self.system.targets[name].medium != "dram" else
                       {state: sum(e["row_state"] == state for e in engine.events)
                        for state in ("row_hits", "row_empty", "row_conflicts")})
            targets[name] = {**self.target_stats[name], "media_completion_ps": engine.completed, **details}
            if self.system.targets[name].medium == "ssd":
                host = [e for e in self.host_events if e["target"] == name]
                targets[name].update(host_merge_steps=len(host), host_merge_ps=sum(e["finish_ps"] - e["start_ps"] for e in host))
        result = {"completion_ps": self.ready, "per_target": targets, "per_link": self.link_stats}
        if include_events:
            result["media_events"] = [{"target": name, **{(k + "_ps" if k in {
                "arrival", "admitted", "issue", "transfer", "finish"} else k): v for k, v in e.items()}}
                for name, engine in self.engines.items() for e in engine.events]
            result["link_events"] = self.link_events
            result["group_events"] = self.group_events
            if self.system.has_nand:
                result["nand_operations"] = [{"target": n, **op} for n, engine in self.engines.items()
                                              if self.system.targets[n].medium == "nand" for op in engine.operations]
            if self.system.has_ssd:
                result["ssd_host_events"] = self.host_events
            if self.system.has_ssd_nand:
                result["ssd_nand_operations"] = [{"target": n, **op} for n, engine in self.engines.items()
                                                  if self.system.targets[n].medium == "ssd" and self.system.targets[n].nand
                                                  for op in engine.nand.operations]
        return result

    def invalidate_nand(self, pages, source_event):
        """Distinct controllers may unbind in parallel after the host transition."""
        finishes = [self.ready]
        for target, logical in pages.items():
            if not logical:
                continue
            started = time.perf_counter()
            finishes.append(self.engines[target].invalidate(logical, self.ready, source_event))
            self.target_stats[target]["backend_seconds"] += time.perf_counter() - started
            self.target_stats[target]["backend_batches"] += 1
        self.ready = max(finishes)
