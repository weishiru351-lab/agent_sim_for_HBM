"""Independent host-block coverage and internal NAND command reference."""

from agent_design_search.contracts import CandidateError, PageRequest, Request
from agent_design_search.verification.nand import ReferenceNAND


class ReferenceSSD:
    def __init__(self, target, mapping=None, gc=None, gc_policy=None):
        self.target, self.serial = target, 0
        if target.nand and target.controller_access == "closed":
            mapping, gc_policy = {"axis_order": ["channel", "die"], "stripe_pages": 1}, None
            gc = "foreground_greedy" if gc else None
        self.nand = ReferenceNAND(target.nand, mapping, gc, gc_policy) if target.nand else None

    def replay(self, transfer, controller, memory):
        access, target = transfer.access, transfer.target
        width = self.target.geometry.block_bytes
        lba = access.request.address // width
        expected = (["read", "merge", "write"] if access.request.kind == "write" and len(access.valid_bytes) < width
                    else [access.request.kind])
        try:
            actions, values = controller.execute(access, transfer.byte_tags)
            if [(a.kind, a.lba) for a in actions] != [(k, lba) for k in expected]:
                return {"reason": "block_action_mismatch", "lba": lba}
            if self.nand:
                for action in actions:
                    error = self.pages(action, access, target, memory)
                    if error:
                        return error
                state = controller.nand.state
                if (state.pages != self.nand.pages or state.bindings != self.nand.bindings or state.pending or state.unpublished):
                    return {"reason": "ssd_nand_state_mismatch", "lba": lba}
        except CandidateError as error:
            return {"reason": error.code, **error.details}
        block = tuple(memory.get((target, lba, byte)) for byte in range(width))
        if controller.blocks.get(lba) != block:
            return {"reason": "block_preservation_mismatch", "lba": lba}
        if values != tuple(block[b] for b in access.valid_bytes):
            return {"reason": "block_readback_mismatch", "lba": lba}
        return None

    def pages(self, action, host, target, memory):
        if action.kind == "merge":
            return {"reason": "ssd_merge_has_page_commands"} if action.pages else None
        width, size = self.target.geometry.block_bytes, self.nand.geometry.page_bytes
        # Group individual host bytes by internal page, independently of the adapter's range splitter.
        by_page = {}
        for byte in range(width):
            page, offset = divmod(action.lba * width + byte, size)
            by_page.setdefault(page, []).append((offset, byte))
        if len(action.pages) != len(by_page):
            return {"reason": "ssd_page_coverage"}
        for actual, (page, members) in zip(action.pages, sorted(by_page.items())):
            mask = tuple(b for b, _ in members)
            request = PageRequest(Request(self.serial, page * size, 0, action.kind, host.request.stream), mask)
            self.serial += 1
            if actual.access != request:
                return {"reason": "ssd_page_request_mismatch"}
            tags = tuple(memory.get((target, action.lba, byte)) for _, byte in members)
            expected, values = self.nand.plan(request, tags, allow_unbound=True)
            if actual.actions != tuple(expected) or actual.values != values:
                return {"reason": "ssd_page_action_mismatch"}
        return None
