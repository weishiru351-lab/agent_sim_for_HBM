"""Stateful, simplified FCFS timing for routed requests; no DSL interpretation.

submit_batch drains a batch before accepting the next one, preserving row and
bus state. It supports fixed arrivals and serial groups, not arbitrary DAGs.
"""

from collections import Counter, deque
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class FCFSParameters:
    queue_depth: int = 16
    issue_interval: int = 1
    t_hit: int = 4
    t_activate: int = 8
    t_precharge: int = 8
    t_transfer: int = 4

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in asdict(self).values()):
            raise ValueError("FCFS parameters must be positive integer cycle counts/capacities")


class FCFS:
    def __init__(self, hardware, parameters=None):
        self.hardware, self.parameters = hardware, parameters or FCFSParameters()
        channels, banks = 1 << hardware.channel_bits, 1 << hardware.bank_bits
        self.channel_state = [(0, 0)] * channels
        self.bank_free = [[0] * banks for _ in range(channels)]
        self.open_rows = [[None] * banks for _ in range(channels)]
        self.events, self.requests, self.ids, self.completed = [], [], set(), 0

    def submit_batch(self, transfers):
        if not transfers:
            raise ValueError("Workload must not be empty")
        cfg = self.parameters
        channels, banks = len(self.channel_state), len(self.bank_free[0])
        routed, batch_ids = [[] for _ in range(channels)], set()
        for transfer in transfers:
            req = transfer.request
            if (type(req.request_id) is not int or req.request_id < 0
                    or req.request_id in self.ids or req.request_id in batch_ids):
                raise ValueError("Duplicate or invalid request ID")
            if type(req.arrival) is not int or req.arrival < self.completed or req.kind not in {"read", "write"}:
                raise ValueError("Invalid arrival/kind, or batch overlaps a previously drained batch")
            for value, maximum in ((transfer.channel, channels), (transfer.bank, banks),
                                   (transfer.row, 1 << self.hardware.row_bits),
                                   (transfer.column, 1 << self.hardware.column_bits)):
                if type(value) is not int or not 0 <= value < maximum:
                    raise ValueError("DRAM coordinate outside hardware")
            if (len(set(transfer.valid_bytes)) != len(transfer.valid_bytes)
                    or any(type(b) is not int or not 0 <= b < self.hardware.granule_bytes for b in transfer.valid_bytes)):
                raise ValueError("Invalid transfer byte mask")
            routed[transfer.channel].append((req, transfer.bank, transfer.row))
            batch_ids.add(req.request_id)
        self.ids.update(batch_ids)
        self.requests.extend(t.request for t in transfers)
        events = self.events
        first_new_event = len(events)
        for channel, items in enumerate(routed):
            if not items:
                continue
            items.sort(key=lambda item: (item[0].arrival, item[0].request_id))
            queue = deque()
            cursor, now = 0, items[0][0].arrival
            next_issue, bus_free = self.channel_state[channel]
            bank_free, open_rows = self.bank_free[channel], self.open_rows[channel]
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
            self.channel_state[channel] = (next_issue, bus_free)

        self.completed = max(e["finish"] for e in events[first_new_event:])
        return self.completed

    def metrics(self, include_events=False):
        events = self.events
        if not events:
            raise ValueError("No completed requests")
        channels, banks = len(self.channel_state), len(self.bank_free[0])
        stats = Counter(e["row_state"] for e in events)
        channel_counts = [0] * channels
        bank_counts = [[0] * banks for _ in range(channels)]
        for event in events:
            channel_counts[event["channel"]] += 1
            bank_counts[event["channel"]][event["bank"]] += 1
        latencies = sorted(event["finish"] - event["arrival"] for event in events)
        cycles = max(e["finish"] for e in events) - min(e["arrival"] for e in events)
        n = len(events)
        result = {"backend": "toy-fcfs-v1", "completed_requests": n, "completion_cycles": cycles,
                  "avg_latency": sum(latencies) / n, "p95_latency": latencies[math.ceil(n * .95) - 1],
                  "row_hit_rate": stats["row_hits"] / n, "row_conflict_rate": stats["row_conflicts"] / n,
                  "row_empty_rate": stats["row_empty"] / n,
                  "channel_requests": channel_counts, "bank_requests": bank_counts,
                  "channel_transfer_utilization": [c * self.parameters.t_transfer / cycles for c in channel_counts],
                  "avg_backpressure_wait": sum(e["admitted"] - e["arrival"] for e in events) / n,
                  "avg_controller_wait": sum(e["issue"] - e["admitted"] for e in events) / n,
                  "read_requests": sum(r.kind == "read" for r in self.requests),
                  "write_requests": sum(r.kind == "write" for r in self.requests)}
        if include_events:
            result["events"] = sorted(events, key=lambda e: e["request_id"])
        return result


def simulate(transfers, hardware, parameters=None, include_events=False):
    engine = FCFS(hardware, parameters)
    engine.submit_batch(transfers)
    return engine.metrics(include_events)
