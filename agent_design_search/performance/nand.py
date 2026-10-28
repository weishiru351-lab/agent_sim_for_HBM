"""Deterministic NAND timing: legacy serial or bounded channel/die concurrency."""

from dataclasses import asdict, dataclass
import heapq


@dataclass(frozen=True)
class NANDParameters:
    read_ps: int
    program_ps: int
    erase_ps: int
    transfer_ps: int
    lookup_ps: int
    merge_ps: int
    bind_ps: int

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in asdict(self).values()):
            raise ValueError("All NAND timing parameters must be positive integer ps")


class NANDTiming:
    def __init__(self, geometry, parameters):
        self.geometry, self.parameters = geometry, parameters
        self.completed, self.events, self.operations = 0, [], []
        self.ids = set()
        self.counts = dict(read_pages=0, program_pages=0, erases=0, management_steps=0,
                           transferred_bytes=0, management_ps=0, array_ps=0, bus_ps=0)

    def invalidate(self, pages, arrival, source_event):
        """A drained metadata operation; unbind uses the fixed bind update cost."""
        if (type(arrival) is not int or arrival < self.completed or len(set(pages)) != len(pages)
                or any(type(p) is not int or not 0 <= p < self.geometry.pages for p in pages)):
            raise ValueError("NAND invalidation needs distinct logical pages and a drained controller")
        now = arrival
        for page in sorted(pages):
            for kind, duration in (("lookup", self.parameters.lookup_ps), ("unbind", self.parameters.bind_ps)):
                self.operations.append({"workload_event": source_event, "kind": kind, "logical_page": page,
                                        "start_ps": now, "finish_ps": now + duration})
                now += duration
                self.counts["management_steps"] += 1
                self.counts["management_ps"] += duration
        if pages:
            self.completed = now
        return now

    def submit_batch(self, requests, actions):
        ids = [a.request.request_id for a in requests]
        if (not ids or any(type(i) is not int or i < 0 for i in ids)
                or len(set(ids)) != len(ids) or self.ids.intersection(ids)
                or any(type(a.request.arrival) is not int or a.request.arrival < self.completed
                       or a.request.kind not in ("read", "write") for a in requests)
                or not isinstance(actions, dict) or any(not actions.get(i) for i in ids)):
            raise ValueError("NAND timing requires unique requests, drained batches and explicit nonempty actions")
        self.ids.update(ids)
        for access in sorted(requests, key=lambda a: a.request.request_id):
            req = access.request
            start = now = max(self.completed, req.arrival)
            for action in actions[req.request_id]:
                kind = action.kind
                event = {"request_id": req.request_id, "kind": kind, "start_ps": now}
                if kind in ("read", "program", "erase"):
                    coords = self.geometry.decode(action.page)
                    if kind == "erase" and coords[-1] != 0:
                        raise ValueError("Erase timing requires an aligned block address")
                    event.update(physical_page=action.page, coordinates=list(coords))
                    array = getattr(self.parameters, kind + "_ps")
                    bus = 0 if kind == "erase" else self.parameters.transfer_ps
                    # Program data crosses the internal bus first; read data crosses it last.
                    event["array_start_ps"] = now + (bus if kind == "program" else 0)
                    event["array_finish_ps"] = event["array_start_ps"] + array
                    event["bus_start_ps"] = now if kind == "program" else now + array
                    self.counts[{"read": "read_pages", "program": "program_pages", "erase": "erases"}[kind]] += 1
                    self.counts["transferred_bytes"] += 0 if kind == "erase" else self.geometry.page_bytes
                    self.counts["array_ps"] += array
                    self.counts["bus_ps"] += bus
                    now += array + bus
                elif kind in ("lookup", "merge", "bind"):
                    duration = getattr(self.parameters, kind + "_ps")
                    self.counts["management_steps"] += 1
                    self.counts["management_ps"] += duration
                    now += duration
                else:
                    raise ValueError("Unknown NAND timing action")
                event["finish_ps"] = now
                self.operations.append(event)
            self.events.append({"request_id": req.request_id, "arrival": req.arrival, "issue": start, "finish": now})
            self.completed = now
        return self.completed


class NANDParallelTiming(NANDTiming):
    """Drained batches, bounded page buffers, one bus/channel and one engine/die.

    Planes share their die; no multi-plane commands or independent plane engines.
    Ready phases use FCFS (request ID breaks ties). Lookup/merge/bind share a port.
    A read holds its die until data-out ends; a program holds it from data-in to
    array completion. Same-LPN requests stay ordered through bind/read completion.
    Writers to the same erase block are admitted and programmed in page order.
    A request containing GC drains earlier requests and blocks later requests
    on this target; copy/bind/erase use the same resources and one page buffer.
    """

    def __init__(self, geometry, parameters, buffer_pages, gc=None):
        super().__init__(geometry, parameters)
        if type(buffer_pages) is not int or not 1 <= buffer_pages <= 64:
            raise ValueError("NAND buffer_pages must be 1..64")
        self.buffer_pages = buffer_pages
        self.gc = gc
        if gc:
            self.counts.update(gc_blocking_ps=0)
        self.bound_pages, self.live_pages, self.peak_live_pages = set(), 0, 0
        self.counts.update(peak_inflight_pages=0, channel_busy_ps={str(c): 0 for c in range(geometry.channels)},
                           die_busy_ps={f"{c}:{d}": 0 for c in range(geometry.channels) for d in range(geometry.dies_per_channel)})

    def invalidate(self, pages, arrival, source_event):
        result = super().invalidate(pages, arrival, source_event)
        self.live_pages -= len(self.bound_pages.intersection(pages))
        self.bound_pages.difference_update(pages)
        return result

    def _job(self, access, actions):
        stages, records = [], []
        for i, action in enumerate(actions):
            kind = action.kind
            record = {"request_id": access.request.request_id, "kind": kind}
            if action.purpose == "gc":
                if not self.gc:
                    raise ValueError("GC actions require the foreground GC timing profile")
                record.update(purpose="gc", logical_page=action.logical_page)
            if kind in ("read", "program", "erase"):
                coords = self.geometry.decode(action.page)
                if kind == "erase" and coords[-1] != 0:
                    raise ValueError("Erase timing requires an aligned block")
                record.update(physical_page=action.page, coordinates=list(coords))
                channel, die = ("channel", coords[0]), ("die", coords[0], coords[1])
                array, bus = getattr(self.parameters, kind + "_ps"), self.parameters.transfer_ps
                if kind == "read":
                    stages += [(i, "array", array, (die,), ()), (i, "bus", bus, (channel,), (channel, die))]
                elif kind == "program":
                    stages += [(i, "bus", bus, (channel, die), (channel,)), (i, "array", array, (), (die,))]
                else:
                    stages.append((i, "array", array, (die,), (die,)))
            elif kind in ("lookup", "merge", "bind"):
                stages.append((i, "management", getattr(self.parameters, kind + "_ps"), ("controller",), ("controller",)))
            else:
                raise ValueError("Unknown NAND timing action")
            records.append(record)
        return {"access": access, "stages": stages, "records": records, "step": 0, "running": False, "predecessors": []}

    def submit_batch(self, requests, actions):
        ids = [a.request.request_id for a in requests]
        if (not ids or any(type(i) is not int or i < 0 for i in ids) or len(set(ids)) != len(ids)
                or self.ids.intersection(ids) or not isinstance(actions, dict)
                or any(type(a.request.arrival) is not int or a.request.arrival < self.completed
                       or a.request.kind not in ("read", "write") or not actions.get(a.request.request_id)
                       for a in requests)):
            raise ValueError("Parallel NAND requires unique requests, drained arrivals and explicit actions")
        # Functional allocation may reuse an erased page later in this batch.
        # Fence the complete GC request, so no earlier read sees erased data.
        batch = []
        for access in sorted(requests, key=lambda a: a.request.request_id):
            maintenance = any(a.purpose == "gc" for a in actions[access.request.request_id])
            if maintenance:
                if batch:
                    self._run_batch(batch, actions)
                    batch = []
                self._run_batch([access], actions)
            else:
                batch.append(access)
        if batch:
            self._run_batch(batch, actions)
        return self.completed

    def _run_batch(self, requests, actions):
        waiting = [self._job(a, actions[a.request.request_id]) for a in sorted(requests, key=lambda a: (a.request.arrival, a.request.request_id))]
        # Allocation was compiled before timing. Preserve physical program order
        # even when RMW reads or arrival times make a later page ready earlier.
        programs, previous_page = {}, {}
        for job in sorted(waiting, key=lambda j: j["access"].request.request_id):
            logical = job["access"].request.address // self.geometry.page_bytes
            job["previous_page"] = previous_page.get(logical)
            previous_page[logical] = job
            for record in job["records"]:
                if record["kind"] == "program":
                    block = record["physical_page"] // self.geometry.pages_per_block
                    programs.setdefault(block, []).append((record["physical_page"], job, record))
        for items in programs.values():
            items.sort(key=lambda item: item[0])
            if len({p for p, _, _ in items}) != len(items):
                raise ValueError("A drained NAND batch cannot program one physical page twice")
            for (_, previous, done), (_, job, record) in zip(items, items[1:]):
                if previous is not job:
                    job["predecessors"].append((previous, done, record))
        self.ids.update(a.request.request_id for a in requests)
        active, held, pending = {}, set(), []
        now = self.completed
        while waiting or active:
            while pending and pending[0][0] == now:
                _, request_id, release = heapq.heappop(pending)
                held.difference_update(release)
                job = active[request_id]
                i = job["stages"][job["step"]][0]
                job["step"] += 1
                job.update(running=False, ready=now)
                if job["step"] == len(job["stages"]) or job["stages"][job["step"]][0] != i:
                    record = job["records"][i]
                    record["finish_ps"] = now
                    if "physical_page" in record:
                        c, d = record["coordinates"][:2]
                        self.counts["die_busy_ps"][f"{c}:{d}"] += now - record["start_ps"]
                    if record["kind"] == "bind":
                        logical = (record["logical_page"] if record.get("purpose") == "gc"
                                   else job["access"].request.address // self.geometry.page_bytes)
                        self.live_pages -= logical in self.bound_pages
                        self.bound_pages.add(logical)
                    if record.get("purpose") == "gc":
                        self.counts["gc_blocking_ps"] += now - record["start_ps"]
                if job["step"] == len(job["stages"]):
                    job["event"]["finish"] = now
                    del active[request_id]
            for job in list(waiting):
                req = job["access"].request
                previous = job["previous_page"]
                if (req.arrival > now or len(active) == self.buffer_pages
                        or (previous is not None and "finish" not in previous.get("event", {}))
                        or any("event" not in previous for previous, _, _ in job["predecessors"])):
                    continue
                waiting.remove(job)
                job.update(ready=now, event={"request_id": req.request_id, "arrival": req.arrival, "admitted": now})
                self.events.append(job["event"])
                active[req.request_id] = job
            self.counts["peak_inflight_pages"] = max(self.counts["peak_inflight_pages"], len(active))
            for request_id, job in sorted(active.items(), key=lambda item: (item[1]["ready"], item[0])):
                if job["running"]:
                    continue
                i, phase, duration, acquire, release = job["stages"][job["step"]]
                record = job["records"][i]
                if (any(resource in held for resource in acquire)
                        or any(record is dependent and "finish_ps" not in done
                               for _, done, dependent in job["predecessors"])):
                    continue
                held.update(acquire)
                job["running"] = True
                job["event"].setdefault("issue", now)
                if "start_ps" not in record:
                    record["start_ps"] = now
                    self.operations.append(record)
                    if record["kind"] == "program":
                        self.live_pages += 1
                        self.peak_live_pages = max(self.peak_live_pages, self.live_pages)
                record[phase + "_start_ps"], record[phase + "_finish_ps"] = now, now + duration
                if phase == "array":
                    self.counts[{"read": "read_pages", "program": "program_pages", "erase": "erases"}[record["kind"]]] += 1
                    self.counts["array_ps"] += duration
                elif phase == "bus":
                    self.counts["bus_ps"] += duration
                    self.counts["transferred_bytes"] += self.geometry.page_bytes
                    self.counts["channel_busy_ps"][str(record["coordinates"][0])] += duration
                else:
                    self.counts["management_steps"] += 1
                    self.counts["management_ps"] += duration
                heapq.heappush(pending, (now + duration, request_id, release))
            future = [t for t, _, _ in pending]
            future += [j["access"].request.arrival for j in waiting if j["access"].request.arrival > now]
            if not future:
                if waiting or active or held:
                    raise RuntimeError("NAND phase scheduler deadlocked")
                break
            now = min(future)
        self.completed = now
        return now
