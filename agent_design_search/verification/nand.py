"""Independent NAND allocation/GC reference and audit of actual controller actions."""

from agent_design_search.contracts import CandidateError, NANDAction


class ReferenceNAND:
    """Enumerate physical pages directly; do not use executor cursors or GC helpers."""

    def __init__(self, target, page_mapping=None, gc=None, gc_policy=None):
        self.geometry, self.mapping, self.gc = target.geometry, page_mapping, gc
        self.bindings, self.pages = {}, {}
        self.gc_policy = gc_policy or {"trigger_free_blocks": 1, "victim": "most_invalid"}
        self.retirement_order, self.invalid_since = 0, {}

    def release(self, logical):
        page = self.bindings.pop(logical)
        self.retirement_order += 1
        self.invalid_since.setdefault(page // self.geometry.pages_per_block, self.retirement_order)

    def bind(self, logical, page):
        if logical in self.bindings:
            self.release(logical)
        self.bindings[logical] = page

    def lane(self, logical):
        g = self.geometry
        if self.mapping is None:
            return range(g.pages)
        stride, coords = self.mapping["stripe_pages"], {}
        counts = {"channel": g.channels, "die": g.dies_per_channel}
        for axis in self.mapping["axis_order"]:
            coords[axis] = (logical // stride) % counts[axis]
            stride *= counts[axis]
        size = g.planes_per_die * g.blocks_per_plane * g.pages_per_block
        base = (coords["channel"] * g.dies_per_channel + coords["die"]) * size
        return range(base, base + size)

    def free(self, lane, excluded=-1):
        page = next((p for p in lane if p not in self.pages and p // self.geometry.pages_per_block != excluded), None)
        if page is None:
            code = "nand_lane_exhausted" if self.mapping else "nand_exhausted"
            raise CandidateError(code, "$.execution.nand", "Reference has no erased page")
        return page

    def collect(self, logical, expected):
        lane, width = self.lane(logical), self.geometry.pages_per_block
        live = set(self.bindings.values())
        live_count = sum(p in lane for p in live)
        if logical not in self.bindings and live_count + 1 > len(lane) - width:
            raise CandidateError("nand_gc_reserve", "$.execution.nand.gc", "Reference GC reserve exceeded")
        erased = set(lane) - self.pages.keys()
        if len(erased) > width * self.gc_policy["trigger_free_blocks"]:
            return
        choices = []
        for first in range(lane.start, lane.stop, width):
            expected.append(NANDAction("lookup", purpose="gc"))
            members = set(range(first, first + width))
            stale = members.intersection(self.pages) - live
            if stale and len(members & live) <= len(erased - members):
                score = len(stale) if self.gc_policy["victim"] == "most_invalid" else -self.invalid_since[first // width]
                choices.append((score, -first, members))
        if not choices:
            if not erased:
                raise CandidateError("nand_gc_no_victim", "$.execution.nand.gc", "Reference cannot reclaim safely")
            return
        _, negative_first, members = max(choices, key=lambda c: c[:2])
        first = -negative_first
        for old, owner in sorted((p, l) for l, p in self.bindings.items() if p in members):
            new = self.free(lane, first // width)
            expected.extend((NANDAction("read", old, owner, "gc"), NANDAction("program", new, owner, "gc"),
                             NANDAction("bind", new, owner, "gc")))
            self.pages[new] = self.pages[old]
            self.bind(owner, new)
        expected.append(NANDAction("erase", first, purpose="gc"))
        for page in members:
            self.pages.pop(page, None)
        self.invalid_since.pop(first // width, None)

    def plan(self, access, tags, allow_unbound=False):
        """Expected operations/bytes; used for raw NAND and internal SSD pages."""
        width = self.geometry.page_bytes
        logical = access.request.address // width
        expected = [NANDAction("lookup")]
        if access.request.kind == "write" and self.gc:
            self.collect(logical, expected)
        old = self.bindings.get(logical)
        if access.request.kind == "read":
            if old is None:
                if allow_unbound:
                    return expected, (None,) * len(access.valid_bytes)
                raise CandidateError("nand_reference_unbound", "$.execution.nand", "Reference read has no binding")
            expected.append(NANDAction("read", old))
            data = self.pages[old]
        else:
            partial = old is not None and len(access.valid_bytes) != width
            data = list(self.pages[old]) if partial else [None] * width
            if partial:
                expected.append(NANDAction("read", old))
            for byte, value in zip(access.valid_bytes, tags):
                data[byte] = value
            new = self.free(self.lane(logical))
            expected.extend((NANDAction("merge"), NANDAction("program", new), NANDAction("bind")))
            self.pages[new] = tuple(data)
            self.bind(logical, new)
        return expected, tuple(data[b] for b in access.valid_bytes)

    def replay(self, transfer, controller, memory):
        access, width = transfer.access, self.geometry.page_bytes
        logical = access.request.address // width
        try:
            expected, _ = self.plan(access, transfer.byte_tags)
            actions, values = controller.execute(access, transfer.byte_tags)
        except CandidateError as error:
            return {"reason": error.code, **error.details}
        if actions != expected:
            fields = lambda a: (a.kind, a.page, a.logical_page, a.purpose)
            return {"reason": "nand_action_mismatch", "expected_actions": [fields(a) for a in expected],
                    "actual_actions": [fields(a) for a in actions]}
        state = controller.state
        if (state.bindings != self.bindings or set(state.pages) != set(self.pages) or state.pending or state.unpublished):
            return {"reason": "nand_binding_mismatch"}
        expected_page = tuple(memory.get((transfer.target, logical, byte)) for byte in range(width))
        if state.pages != self.pages or state.pages[self.bindings[logical]] != expected_page:
            return {"reason": "nand_preservation_mismatch", "logical_page": logical}
        if values != tuple(expected_page[byte] for byte in access.valid_bytes):
            return {"reason": "nand_readback_mismatch", "logical_page": logical}
        return None
