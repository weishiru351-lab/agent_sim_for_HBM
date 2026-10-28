"""Host block/RMW adapter, with an optional shared NAND controller underneath.

Unwritten bytes have unknown tags (None), not an assumed device zero-fill promise.
BlockExecutor tracks host contents; NANDBackedSSD also executes physical pages.
"""

from agent_design_search.contracts import BlockAction, CandidateError, PageCommand, PageRequest, Request, NANDAction
from agent_design_search.execution.nand import NANDExecutor


def merge_block(previous, mask, values):
    result = list(previous)
    for byte, value in zip(mask, values):
        result[byte] = value
    return tuple(result)


class BlockExecutor:
    def __init__(self, target):
        self.target, self.blocks = target, {}
        self.rmw_updates = self.preserved_bytes = 0

    def execute(self, access, tags):
        req, mask = access.request, access.valid_bytes
        width = self.target.geometry.block_bytes
        if (type(req.address) is not int or req.address < 0 or req.address % width
                or req.address + width > self.target.capacity_bytes or req.kind not in ("read", "write")
                or not mask or len(mask) != len(tags) or len(set(mask)) != len(mask)
                or any(type(b) is not int or not 0 <= b < width for b in mask)):
            raise CandidateError("invalid_block_access", "$.dsl.kv_layout", "Invalid masked SSD block access")
        lba = req.address // width
        previous = self.blocks.get(lba, (None,) * width)
        if req.kind == "read":
            return [BlockAction("read", lba)], tuple(previous[b] for b in mask)
        partial = len(mask) < width
        actions = [BlockAction("read", lba), BlockAction("merge", lba)] if partial else []
        # Even the first partial write reads the block: unwritten device bytes are opaque.
        self.blocks[lba] = merge_block(previous, mask, tags)
        if partial:
            self.rmw_updates += 1
            self.preserved_bytes += sum(v is not None and b not in mask for b, v in enumerate(previous))
        return actions + [BlockAction("write", lba)], tuple(self.blocks[lba][b] for b in mask)

    def metrics(self):
        width = self.target.geometry.block_bytes
        return {"written_logical_blocks": len(self.blocks), "rmw_updates": self.rmw_updates,
                "preserved_bytes": self.preserved_bytes, "host_buffer_budget_bytes": self.target.host_buffer_bytes,
                "host_buffer_reserved_bytes": width, "scratch_peak_bytes": width if self.rmw_updates else 0}


class NANDBackedSSD(BlockExecutor):
    """Translate the same host block contract into the shared NAND executor.

    Unbound page reads return unknown tags after a charged mapping lookup. There
    is no device data cache or TRIM; host release does not retire NAND bindings.
    """
    def __init__(self, target, page_mapping, buffer_pages, gc, gc_policy=None):
        super().__init__(target)
        if target.controller_access == "closed":
            page_mapping = {"axis_order": ["channel", "die"], "stripe_pages": 1}
            gc, gc_policy = ("foreground_greedy" if gc else None), None
        self.nand = NANDExecutor(target.nand, page_mapping, buffer_pages, gc, gc_policy)
        self.page_serial = 0

    def execute(self, access, tags):
        width, page_bytes = self.target.geometry.block_bytes, self.target.nand.geometry.page_bytes
        lba = access.request.address // width
        previous = self.blocks.get(lba, (None,) * width)
        host_actions, returned = super().execute(access, tags)
        actions = []
        for host in host_actions:
            pages = []
            if host.kind != "merge":
                contents = previous if host.kind == "read" else self.blocks[lba]
                first, end = lba * width, (lba + 1) * width
                for base in range(first // page_bytes * page_bytes, end, page_bytes):
                    mask = tuple(range(max(first, base) - base, min(end, base + page_bytes) - base))
                    values = tuple(contents[base + b - first] for b in mask)
                    req = PageRequest(Request(self.page_serial, base, 0, host.kind, access.request.stream), mask)
                    self.page_serial += 1
                    if host.kind == "read" and base // page_bytes not in self.nand.state.bindings:
                        raw, actual = [NANDAction("lookup")], (None,) * len(mask)
                    else:
                        raw, actual = self.nand.execute(req, values)
                    if actual != values:
                        raise CandidateError("ssd_nand_content", "$.dsl.kv_layout", "SSD internal page lost host data", lba=lba)
                    pages.append(PageCommand(req, tuple(raw), actual))
            actions.append(BlockAction(host.kind, host.lba, tuple(pages)))
        return actions, returned
