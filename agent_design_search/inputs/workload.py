"""Fixed named KV objects and serial events; input compatibility ends at parsing."""

from dataclasses import asdict, dataclass
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from agent_design_search.contracts import Request, CandidateError
from agent_design_search.json_io import read


@dataclass(frozen=True)
class TensorShape:
    tokens: int
    dimensions: int
    element_bytes: int

    @classmethod
    def parse(cls, obj, path):
        if not isinstance(obj, dict) or set(obj) != {"tokens", "dimensions", "element_bytes"}:
            raise CandidateError("invalid_tensor", path, "Specify tokens, dimensions and element_bytes")
        for name, limit in (("tokens", 64), ("dimensions", 256), ("element_bytes", 4096)):
            if type(obj[name]) is not int or not 1 <= obj[name] <= limit:
                raise CandidateError("invalid_tensor", path + "." + name, "Invalid tensor extent or element size")
        if obj["element_bytes"] & (obj["element_bytes"] - 1):
            raise CandidateError("invalid_tensor", path + ".element_bytes", "Element size must be a power of two")
        return cls(**obj)


@dataclass(frozen=True)
class Event:
    op: str
    object_id: str = ""
    count: int = 0
    tokens: tuple = ()
    dimensions: tuple = ()
    group_by: str = ""

    def to_dict(self):
        if self.op in ("create", "invalidate"):
            return {"op": self.op, "object": self.object_id}
        if self.op == "append":
            return {"op": self.op, "object": self.object_id, "count": self.count}
        if self.op == "read":
            return {"op": self.op, "object": self.object_id, "tokens": list(self.tokens),
                    "dimensions": list(self.dimensions), "group_by": self.group_by}
        return {"op": self.op}


@dataclass(frozen=True)
class KVWorkload:
    objects: tuple  # (stable object ID, TensorShape), sorted independently of JSON key order.
    events: tuple
    version: int = 2
    provenance: dict | None = None

    @classmethod
    def parse(cls, obj):
        path = "$.workload"
        if not isinstance(obj, dict) or type(obj.get("version")) is not int or obj["version"] not in (1, 2, 3):
            raise CandidateError("invalid_version", path, "Workload version must be 1, 2 or 3")
        legacy = obj["version"] == 1
        lifecycle = obj["version"] == 3
        required = {"version", "tensor" if legacy else "objects", "events"}
        if not required <= set(obj) or set(obj) - required - {"provenance"}:
            raise CandidateError("invalid_fields", path, "Unexpected workload fields")
        provenance = obj.get("provenance")
        if "provenance" in obj and (
                not isinstance(provenance, dict)
                or set(provenance) != {"kind", "source", "source_sha256", "execution", "note"}
                or provenance["kind"] not in ("synthetic", "captured")
                or provenance["execution"] != "serial_groups"
                or any(type(provenance[k]) is not str or not provenance[k].strip() for k in ("source", "note"))
                or type(provenance["source_sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", provenance["source_sha256"])):
            raise CandidateError("invalid_provenance", path + ".provenance",
                                 "Declare kind, source, SHA256, execution=serial_groups and conversion note")
        raw_objects = {"V": obj["tensor"]} if legacy else obj["objects"]
        if (not isinstance(raw_objects, dict) or not 1 <= len(raw_objects) <= 32
                or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", n) for n in raw_objects)):
            raise CandidateError("invalid_objects", path + ".objects", "Specify 1..32 uniquely named objects (letters/digits/underscore)")
        shapes = {n: TensorShape.parse(raw_objects[n], path + (".tensor" if legacy else ".objects." + n))
                  for n in sorted(raw_objects)}
        if sum(s.tokens * s.dimensions * s.element_bytes for s in shapes.values()) > 1_048_576:
            raise CandidateError("capacity_limit", path + ".objects", "Reference checker supports at most 1 MiB of logical data")
        if not isinstance(obj["events"], list) or not 1 <= len(obj["events"]) <= 4096:
            raise CandidateError("event_limit", path + ".events", "Expected 1 to 4096 events")
        events, written, accesses, accessed_bytes, ended = [], dict.fromkeys(shapes, 0), 0, 0, False
        active = set() if lifecycle else set(shapes)
        appended = False
        for index, raw in enumerate(obj["events"]):
            at = f"{path}.events[{index}]"
            if ended:
                raise CandidateError("event_after_end", at, "No event may follow end")
            if not isinstance(raw, dict) or type(raw.get("op")) is not str:
                raise CandidateError("invalid_event", at, "Event needs an operation name")
            if raw == {"op": "end"}:
                events.append(Event("end")); ended = True
                continue
            if legacy:
                if "object" in raw:
                    raise CandidateError("invalid_event", at, "Version 1 has no object field")
                raw = {**raw, "object": "V"}
            name = raw.get("object")
            if not isinstance(name, str) or name not in shapes:
                raise CandidateError("unknown_object", at + ".object", "Event must name a declared object")
            shape, count = shapes[name], 0
            if lifecycle and raw["op"] in ("create", "invalidate"):
                if set(raw) != {"op", "object"}:
                    raise CandidateError("invalid_event", at, "Lifecycle events only name an object")
                if (raw["op"] == "create") == (name in active):
                    raise CandidateError("invalid_lifetime", at, "Create requires an inactive object; invalidate requires a live object", object=name)
                if raw["op"] == "create":
                    active.add(name)
                else:
                    active.remove(name)
                written[name] = 0
                events.append(Event(raw["op"], name))
                continue
            if lifecycle and name not in active:
                raise CandidateError("inactive_object", at, "Create the object before accessing it", object=name)
            if raw["op"] == "append" and set(raw) == {"op", "object", "count"}:
                count = raw["count"]
                if type(count) is not int or not 1 <= count <= shape.tokens - written[name]:
                    raise CandidateError("append_overflow", at + ".count", "Append must fit remaining object slots",
                                         object=name, written_tokens=written[name], remaining=shape.tokens-written[name])
                events.append(Event("append", name, count=count))
                written[name] += count
                appended = True
                count *= shape.dimensions
            elif raw["op"] == "read" and set(raw) == {"op", "object", "tokens", "dimensions", "group_by"}:
                for axis, maximum in (("tokens", written[name]), ("dimensions", shape.dimensions)):
                    interval = raw[axis]
                    if (not isinstance(interval, list) or len(interval) != 2
                            or any(type(v) is not int for v in interval)
                            or not 0 <= interval[0] < interval[1] <= maximum):
                        raise CandidateError("invalid_read_range", at + "." + axis,
                                             "Use a nonempty [start, stop) range of already written elements",
                                             object=name, actual=interval, available=maximum)
                if raw["group_by"] not in ("token", "dimension"):
                    raise CandidateError("invalid_grouping", at + ".group_by", "Choose token or dimension grouping")
                events.append(Event("read", name, tokens=tuple(raw["tokens"]), dimensions=tuple(raw["dimensions"]),
                                    group_by=raw["group_by"]))
                count = (raw["tokens"][1]-raw["tokens"][0]) * (raw["dimensions"][1]-raw["dimensions"][0])
            else:
                raise CandidateError("invalid_event", at, "Unknown operation or unexpected event fields")
            accesses += count; accessed_bytes += count * shape.element_bytes
            if accesses > 1_000_000 or accessed_bytes > 16_777_216:
                raise CandidateError("access_limit", at, "Reference workload exceeds 1M element or 16 MiB byte accesses")
        if not ended or not appended:
            raise CandidateError("incomplete_workload", path + ".events", "Append at least once and finish with end")
        return cls(tuple(shapes.items()), tuple(events), 3 if lifecycle else 2, deepcopy(provenance))

    @property
    def shapes(self):
        return dict(self.objects)

    @property
    def written_tokens(self):
        result = dict.fromkeys(self.shapes, 0)
        for e in self.events:
            if e.op == "append":
                result[e.object_id] += e.count
            elif e.op in ("create", "invalidate"):
                result[e.object_id] = 0
        return result

    def to_dict(self):
        return {"version": self.version, "objects": {n: asdict(s) for n, s in self.objects},
                "events": [e.to_dict() for e in self.events],
                **({"provenance": deepcopy(self.provenance)} if self.provenance else {})}

    def groups(self):
        """Yield (source event, object ID, operation, local element indices)."""
        written, shapes = dict.fromkeys(self.shapes, 0), self.shapes
        for index, event in enumerate(self.events):
            if event.op == "end":
                continue
            name = event.object_id
            if event.op in ("create", "invalidate"):
                written[name] = 0
                yield index, name, event.op, ()
                continue
            dimensions = shapes[name].dimensions
            if event.op == "append":
                for token in range(written[name], written[name] + event.count):
                    yield index, name, "write", tuple(token * dimensions + d for d in range(dimensions))
                written[name] += event.count
            else:
                tokens, dims = range(*event.tokens), range(*event.dimensions)
                if event.group_by == "token":
                    for token in tokens:
                        yield index, name, "read", tuple(token * dimensions + d for d in dims)
                else:
                    for dimension in dims:
                        yield index, name, "read", tuple(t * dimensions + dimension for t in tokens)

    def summary(self):
        written, states = dict.fromkeys(self.shapes, 0), []
        generations = dict.fromkeys(self.shapes, 0)
        for index, e in enumerate(self.events):
            if e.op == "end":
                states.append({"event": index, "op": "end", "state_after": "ended"})
                continue
            if e.op in ("create", "invalidate"):
                written[e.object_id] = 0
                if e.op == "create":
                    generations[e.object_id] += 1
                states.append({"event": index, "op": e.op, "object": e.object_id,
                               "generation": generations[e.object_id], "written_tokens": 0,
                               "state_after": "empty" if e.op == "create" else "inactive"})
                continue
            written[e.object_id] += e.count if e.op == "append" else 0
            states.append({"event": index, "op": e.op, "object": e.object_id,
                           "written_tokens": written[e.object_id],
                           "state_after": "full" if written[e.object_id] == self.shapes[e.object_id].tokens else "partial"})
            if self.version == 3:
                states[-1]["generation"] = generations[e.object_id]
        groups = [g for g in self.groups() if g[2] in ("read", "write")]
        return {"objects": {n: asdict(s) for n, s in self.objects}, "execution": "serial",
                "logical_groups": len(groups), "event_states": states,
                **({"provenance": deepcopy(self.provenance)} if self.provenance else {}),
                **{k + "_elements": sum(len(indices) for _, _, kind, indices in groups if kind == k)
                   for k in ("read", "write")}}


def import_kv(path, kind, source, note):
    """Import already normalized events; never discard timestamps/dependencies.

    Capture tools must explicitly convert to this serial contract first. Unknown
    event fields are rejected by parse, including unsupported async timestamps.
    'captured' is the caller's declaration, not authentication of its origin.
    """
    path = Path(path)
    data = KVWorkload.parse(read(path)).to_dict()
    data["provenance"] = dict(kind=kind, source=source, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              execution="serial_groups", note=note)
    return KVWorkload.parse(data).to_dict()


def features(requests, cfg):
    blocks = [req.address // cfg.granule_bytes for req in requests]
    transitions = [sum(((a ^ b) >> bit) & 1 for a, b in zip(blocks, blocks[1:])) / max(1, len(blocks) - 1)
                   for bit in range(cfg.block_bits)]
    return {"requests": len(requests), "block_bits": cfg.block_bits,
            "top_block_strides": Counter(b - a for a, b in zip(blocks, blocks[1:])).most_common(8),
            "bit_transition_rates": transitions,
            "bit_one_rates": [sum((b >> bit) & 1 for b in blocks) / len(blocks) for bit in range(cfg.block_bits)],
            "address_sample": [r.address for r in requests[:24]]}


def parse_trace(obj, cfg):
    if (not isinstance(obj, dict) or type(obj.get("version")) is not int
            or obj["version"] != 1 or not isinstance(obj.get("requests"), list)
            or not obj["requests"] or set(obj) - {"version", "requests", "provenance"}):
        raise ValueError("Trace requires version=1 and a nonempty requests list")
    result, ids = [], set()
    for item in obj["requests"]:
        if (not isinstance(item, dict) or not {"request_id", "address", "arrival"} <= set(item)
                or set(item) - {"request_id", "address", "arrival", "kind", "stream"}):
            raise ValueError("Invalid trace request fields")
        req = Request(**item)
        if any(type(v) is not int or v < 0 for v in
               (req.request_id, req.address, req.arrival, req.stream)):
            raise ValueError("Request integer fields must be nonnegative integers")
        if (req.request_id in ids or req.address >= cfg.blocks * cfg.granule_bytes
                or req.address % cfg.granule_bytes or req.kind not in {"read", "write"}):
            raise ValueError("Duplicate ID, invalid address/alignment, or invalid kind")
        ids.add(req.request_id)
        result.append(req)
    return sorted(result, key=lambda r: (r.arrival, r.request_id))


def statistics(requests, cfg):
    blocks = [r.address // cfg.granule_bytes for r in requests]
    pairs = []
    for a in range(cfg.block_bits):
        for b in range(a + 1, cfg.block_bits):
            counts = Counter(((x >> a) & 1) | (((x >> b) & 1) << 1) for x in blocks)
            pairs.append({"bits": [a, b], "counts_00_10_01_11": [counts[i] for i in range(4)]})
    width = max(1, (len(requests) + 3) // 4)
    return {**features(requests, cfg), "pair_joint_counts": pairs,
            "windows": [{"start_index": i, **features(requests[i:i + width], cfg)}
                        for i in range(0, len(requests), width)]}


def load_trace(path, cfg):
    return parse_trace(read(path), cfg)
