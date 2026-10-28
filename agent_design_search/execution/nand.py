"""Abstract SLC state and out-of-place logical-page execution.

Legacy ascending allocation or DSL-selected channel/die and foreground GC rules.
Functional execution produces ordered actions; timing preserves their dependencies.
"""

from agent_design_search.contracts import CandidateError, NANDAction


def reject(code, message, **details):
    raise CandidateError(code, "$.dsl.kv_layout", message, **details)


class NANDState:
    """Sparse erased array with explicit start/completion and publish boundaries."""
    def __init__(self, target, page_mapping=None):
        self.target, self.geometry = target, target.geometry
        self.page_mapping, self.lane_cursors = page_mapping, {}
        self.pages, self.bindings, self.next_page = {}, {}, {}
        self.pending, self.unpublished = {}, set()
        self.cursor, self.serial = 0, 0
        self.retirements, self.invalid_since = 0, {}
        self.peak_physical_pages, self.peak_live_pages = 0, 0

    def _peaks(self):
        programs = sum(kind == "program" for kind, _, _ in self.pending.values())
        self.peak_physical_pages = max(self.peak_physical_pages, len(self.pages) + programs)
        self.peak_live_pages = max(self.peak_live_pages, len(self.bindings) + len(self.unpublished) + programs)

    def lane_pages(self, logical_page):
        counts = {"channel": self.geometry.channels, "die": self.geometry.dies_per_channel}
        rest, coords = logical_page // self.page_mapping["stripe_pages"], {}
        for axis in self.page_mapping["axis_order"]:
            rest, coords[axis] = divmod(rest, counts[axis])
        lane = coords["channel"] * self.geometry.dies_per_channel + coords["die"]
        size = self.geometry.planes_per_die * self.geometry.blocks_per_plane * self.geometry.pages_per_block
        return range(lane * size, (lane + 1) * size)

    def free_page(self, logical_page=None, exclude_block=None):
        """Lowest available block, then ascending page order; no implicit erase."""
        if self.page_mapping is not None:
            pages = self.lane_pages(logical_page)
            first, limit = pages.start, pages.stop
            lane = first // len(pages)
            cursor = self.lane_cursors.get(lane, first)
            while cursor < limit:
                block = cursor // self.geometry.pages_per_block
                page = block * self.geometry.pages_per_block + self.next_page.get(block, 0)
                if block != exclude_block and page < (block + 1) * self.geometry.pages_per_block:
                    if exclude_block is None:
                        self.lane_cursors[lane] = page
                    return page
                cursor = (block + 1) * self.geometry.pages_per_block
            raise CandidateError("nand_lane_exhausted", "$.dsl.nand_page_mapping",
                                 "Selected channel/die has no available erased page; no cross-die fallback",
                                 logical_page=logical_page, channel=lane // self.geometry.dies_per_channel,
                                 die=lane % self.geometry.dies_per_channel, lane_pages=len(pages))
        width = self.geometry.pages_per_block
        while self.cursor < self.geometry.pages:
            block = self.cursor // width
            page = block * width + self.next_page.get(block, 0)
            if page < (block + 1) * width:
                return page
            self.cursor = (block + 1) * width
        reject("nand_exhausted", "No erased NAND page remains; automatic GC is disabled",
               physical_pages=self.geometry.pages, programmed_pages=len(self.pages))

    def begin(self, kind, page, data=()):
        self.geometry.decode(page)  # Also validates non-power-of-two coordinates.
        block, within = divmod(page, self.geometry.pages_per_block)
        if any(p // self.geometry.pages_per_block == block for _, p, _ in self.pending.values()):
            reject("nand_busy", "Block has an unfinished operation", block=block)
        if kind == "read":
            if page not in self.pages:
                reject("nand_unprogrammed_read", "Raw read requires a programmed page", physical_page=page)
        elif kind == "program":
            if page in self.pages or within != self.next_page.get(block, 0):
                reject("nand_program_order", "Program each erased page once, in ascending block order", physical_page=page)
            if not isinstance(data, tuple) or len(data) != self.geometry.page_bytes:
                reject("nand_partial_program", "Raw NAND programming requires one complete page", physical_page=page)
        elif kind == "erase":
            if within:
                reject("nand_erase_alignment", "Erase must name the first page of a block", physical_page=page)
            protected = set(self.bindings.values()) | self.unpublished
            if any(p // self.geometry.pages_per_block == block for p in protected):
                reject("nand_live_erase", "Cannot erase live data or an unpublished program", block=block)
        else:
            reject("nand_operation", "Unknown raw NAND operation", operation=kind)
        token = self.serial
        self.serial += 1
        self.pending[token] = kind, page, data
        self._peaks()
        return token

    def complete(self, token):
        if type(token) is not int or token not in self.pending:
            reject("nand_completion", "Unknown or already completed operation")
        kind, page, data = self.pending.pop(token)
        block = page // self.geometry.pages_per_block
        if kind == "read":
            return self.pages[page]
        if kind == "program":
            self.pages[page] = data
            self.unpublished.add(page)
            self.next_page[block] = self.next_page.get(block, 0) + 1
        else:
            for p in range(page, page + self.geometry.pages_per_block):
                self.pages.pop(p, None)
            self.next_page.pop(block, None)
            self.invalid_since.pop(block, None)
            self.cursor = min(self.cursor, page)
            lane_size = self.geometry.planes_per_die * self.geometry.blocks_per_plane * self.geometry.pages_per_block
            lane = page // lane_size
            self.lane_cursors[lane] = min(self.lane_cursors.get(lane, page), page)
        self._peaks()

    def bind(self, logical_page, physical_page):
        if type(logical_page) is not int or not 0 <= logical_page < self.target.logical_pages:
            reject("nand_logical_range", "Logical page outside the exported address space", logical_page=logical_page)
        if type(physical_page) is not int or physical_page not in self.unpublished:
            reject("nand_early_bind", "Binding requires a completed, unpublished program", physical_page=physical_page)
        if logical_page in self.bindings:
            self.unbind(logical_page)
        self.bindings[logical_page] = physical_page  # Old page becomes stale, never erased here.
        self.unpublished.remove(physical_page)

    def unbind(self, logical_page):
        old = self.bindings.pop(logical_page)
        self.retirements += 1
        self.invalid_since.setdefault(old // self.geometry.pages_per_block, self.retirements)

    def logical_contents(self, logical_page):
        page = self.bindings.get(logical_page)
        if page is None:
            reject("nand_unbound_read", "Logical page has no completed binding", logical_page=logical_page)
        return self.pages[page]


def merge_page(previous, mask, values):
    """One page scratch buffer; None marks erased/unwritten bytes in this model."""
    data = list(previous)
    for byte, value in zip(mask, values):
        data[byte] = value
    return tuple(data)


class NANDExecutor:
    def __init__(self, target, page_mapping=None, buffer_pages=1, gc=None, gc_policy=None):
        self.age_metadata_bytes = 8 * target.geometry.blocks if gc == "policy" else 0
        if (type(buffer_pages) is not int or buffer_pages < 1
                or target.management_bytes < target.metadata_bytes + self.age_metadata_bytes + buffer_pages * target.geometry.page_bytes):
            raise ValueError("NAND management budget must cover metadata and declared page buffers")
        self.state = NANDState(target, page_mapping)
        if gc not in (None, "foreground_greedy", "policy") or gc and (
                page_mapping is None or target.geometry.planes_per_die * target.geometry.blocks_per_plane < 2):
            raise ValueError("Foreground GC requires page mapping and at least two blocks per die")
        self.gc = gc
        if (gc == "policy") != (gc_policy is not None):
            raise ValueError("Searchable GC requires a supplied rule; fixed GC takes no candidate")
        self.gc_policy = gc_policy if gc_policy is not None else {"trigger_free_blocks": 1, "victim": "most_invalid"}
        if (not isinstance(self.gc_policy, dict) or set(self.gc_policy) != {"trigger_free_blocks", "victim"}
                or type(self.gc_policy["trigger_free_blocks"]) is not int
                or not 1 <= self.gc_policy["trigger_free_blocks"] < target.geometry.planes_per_die * target.geometry.blocks_per_plane
                or self.gc_policy["victim"] not in ("most_invalid", "oldest_reclaimable")) and gc:
            raise ValueError("Invalid foreground GC rule")
        self.gc_cycles = self.gc_copied_pages = self.gc_reclaimed_pages = 0
        self.frontend_program_pages = self.frontend_write_bytes = 0
        self.buffer_pages = buffer_pages
        self.scratch_used, self.preserved_bytes = False, 0

    def collect(self, logical, raw, actions):
        """Apply threshold/victim choice with one fixed block of capacity reserve.

        Only reclaim safe stale blocks, tie-breaking by physical block ID. Copy
        within the die, outside the victim; publish before erase. No background GC.
        """
        state, width = self.state, self.state.geometry.pages_per_block
        lane = state.lane_pages(logical)
        live = {p: l for l, p in state.bindings.items() if p in lane}
        if logical not in state.bindings and len(live) >= len(lane) - width:
            reject("nand_gc_reserve", "Binding would consume the die's GC reserve",
                   logical_page=logical, live_pages=len(live), usable_pages=len(lane) - width)
        free = len(lane) - sum(p in lane for p in state.pages)
        if free > width * self.gc_policy["trigger_free_blocks"]:
            return
        victims = []
        for first in range(lane.start, lane.stop, width):
            actions.append(NANDAction("lookup", purpose="gc"))
            block = first // width
            programmed = state.next_page.get(block, 0)
            valid = sorted((p, l) for p, l in live.items() if p // width == block)
            invalid = programmed - len(valid)
            outside_free = free - (width - programmed)
            if invalid and len(valid) <= outside_free:
                rank = -invalid if self.gc_policy["victim"] == "most_invalid" else state.invalid_since[block]
                victims.append((rank, block, valid, invalid))
        if not victims:
            if not free:
                reject("nand_gc_no_victim", "No safely reclaimable block in selected die", logical_page=logical)
            return
        _, block, valid, invalid = min(victims)
        for old, owner in valid:
            data = raw("read", old, logical_page=owner, purpose="gc")
            new = state.free_page(owner, exclude_block=block)
            raw("program", new, data, logical_page=owner, purpose="gc")
            state.bind(owner, new)
            actions.append(NANDAction("bind", new, owner, "gc"))
        raw("erase", block * width, purpose="gc")
        self.gc_cycles += 1
        self.gc_copied_pages += len(valid)
        self.gc_reclaimed_pages += invalid

    def execute(self, access, values):
        """Compile and functionally replay one page access into a serial action chain."""
        state, geometry = self.state, self.state.geometry
        req, mask = access.request, access.valid_bytes
        if type(req.address) is not int:
            reject("nand_page_access", "Logical page address must be an integer", request_id=req.request_id)
        logical, offset = divmod(req.address, geometry.page_bytes)
        if (offset or not 0 <= logical < state.target.logical_pages or req.kind not in ("read", "write")
                or not mask or len(set(mask)) != len(mask) or len(values) != len(mask)
                or any(type(b) is not int or not 0 <= b < geometry.page_bytes for b in mask)):
            reject("nand_page_access", "Invalid logical page request", request_id=req.request_id)
        actions = [NANDAction("lookup")]
        def raw(kind, page, data=(), logical_page=-1, purpose="access"):
            token = state.begin(kind, page, data)
            actions.append(NANDAction(kind, page, logical_page, purpose))
            return state.complete(token)

        self.scratch_used = True
        if req.kind == "write" and self.gc:
            self.collect(logical, raw, actions)
        old = state.bindings.get(logical)
        if req.kind == "read":
            if old is None:
                reject("nand_unbound_read", "Logical read has no binding", logical_page=logical)
            page_data = raw("read", old)
        else:
            page_data = (None,) * geometry.page_bytes
            # A full overwrite needs no old read. Partial updates preserve the whole old page.
            if old is not None and len(mask) != geometry.page_bytes:
                page_data = raw("read", old)
                written = set(mask)
                self.preserved_bytes += sum(v is not None and i not in written for i, v in enumerate(page_data))
            actions.append(NANDAction("merge"))
            page_data = merge_page(page_data, mask, values)
            new = state.free_page(logical)
            raw("program", new, page_data)
            actions.append(NANDAction("bind"))
            state.bind(logical, new)
            self.frontend_program_pages += 1
            self.frontend_write_bytes += len(mask)
        return actions, tuple(page_data[b] for b in mask)

    def erase(self, block):
        """Explicit maintenance primitive; refuses blocks with live/unpublished pages."""
        if type(block) is not int or not 0 <= block < self.state.geometry.blocks:
            reject("nand_block_range", "Erase block outside geometry", block=block)
        page = block * self.state.geometry.pages_per_block
        token = self.state.begin("erase", page)
        self.state.complete(token)
        return [NANDAction("erase", page)]

    def release_range(self, address, size):
        """Drop completed logical bindings; programmed pages remain occupied."""
        state, width = self.state, self.state.geometry.page_bytes
        if (type(address) is not int or type(size) is not int or address < 0 or size < 1
                or address % width or size % width or address + size > state.target.capacity_bytes):
            reject("nand_release_range", "Release must cover aligned exported logical pages")
        if state.pending or state.unpublished:
            reject("nand_busy_release", "Cannot unbind while operations or publication are pending")
        logical = tuple(sorted(p for p in state.bindings if address // width <= p < (address + size) // width))
        for page in logical:
            state.unbind(page)
        return logical

    def metrics(self):
        state, target = self.state, self.state.target
        page = state.geometry.page_bytes
        return {"logical_bound_pages": len(state.bindings), "programmed_pages": len(state.pages),
                "invalid_pages": len(state.pages) - len(state.bindings) - len(state.unpublished),
                "free_pages": state.geometry.pages - len(state.pages),
                "spare_pages": state.geometry.pages - target.logical_pages,
                "physical_used_bytes": len(state.pages) * page, "physical_peak_bytes": state.peak_physical_pages * page,
                "live_bytes": len(state.bindings) * page, "peak_live_bytes": state.peak_live_pages * page,
                "metadata_bytes": target.metadata_bytes + self.age_metadata_bytes, "management_budget_bytes": target.management_bytes,
                "management_reserved_bytes": target.metadata_bytes + self.age_metadata_bytes + self.buffer_pages * page,
                "scratch_peak_bytes": page if self.scratch_used else 0, "preserved_bytes": self.preserved_bytes,
                **({"gc_cycles": self.gc_cycles, "gc_copied_pages": self.gc_copied_pages,
                    "gc_reclaimed_pages": self.gc_reclaimed_pages, "gc_reserve_pages_per_die": state.geometry.pages_per_block,
                    "frontend_program_pages": self.frontend_program_pages, "frontend_write_bytes": self.frontend_write_bytes,
                    "write_amplification": ((self.frontend_program_pages + self.gc_copied_pages) * page / self.frontend_write_bytes
                                            if self.frontend_write_bytes else None)} if self.gc else {})}
