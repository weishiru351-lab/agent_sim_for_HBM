"""Independent placement enumeration and byte-tag replay of compiled transactions."""

from itertools import product, groupby

from agent_design_search.inputs.hardware import MemorySystem
from agent_design_search.contracts import CandidateError, DRAMRequest, PageRequest, BlockRequest, AllocationEvent, MigrationEvent
from agent_design_search.execution.nand import NANDExecutor
from agent_design_search.execution.ssd import BlockExecutor, NANDBackedSSD
from agent_design_search.verification.nand import ReferenceNAND
from agent_design_search.verification.ssd import ReferenceSSD


def reference_object_offsets(layout, cfg):
    extents = {"token_outer": (cfg.tokens + layout.token - 1) // layout.token,
               "dimension_outer": (cfg.dimensions + layout.dimension - 1) // layout.dimension,
               "token_inner": layout.token, "dimension_inner": layout.dimension}
    expected = {}
    for slot, coords in enumerate(product(*(range(extents[a]) for a in layout.order))):
        c = dict(zip(layout.order, coords))
        t = c["token_outer"] * layout.token + c["token_inner"]
        d = c["dimension_outer"] * layout.dimension + c["dimension_inner"]
        if t < cfg.tokens and d < cfg.dimensions:
            expected[t * cfg.dimensions + d] = slot * cfg.element_bytes
    return expected


def reference_placement(layout, bounds):
    if hasattr(layout, "groups"):
        placed, reserved = reference_regions(layout, bounds)
        return placed, sum(reserved.values())
    local, sizes = {}, {}
    for name, item in layout.objects:
        b = bounds[name]
        local[name] = reference_object_offsets(item, b)
        size = (((b.tokens + item.token - 1) // item.token) * item.token
                * ((b.dimensions + item.dimension - 1) // item.dimension) * item.dimension * b.element_bytes)
        sizes[name] = ((size + b.line_bytes - 1) // b.line_bytes) * b.line_bytes
    result, cursor = {}, 0
    if layout.kind == "concat":
        for name in layout.order:
            cursor += (-cursor) % layout.alignment_bytes
            result[name] = {i: cursor + v for i, v in local[name].items()}
            cursor += sizes[name]
    else:
        counts = {n: (v + layout.chunk_bytes - 1) // layout.chunk_bytes for n, v in sizes.items()}
        for pos, name in enumerate(layout.order):
            result[name] = {}
            for i, v in local[name].items():
                turn, remainder = divmod(v, layout.chunk_bytes)
                # Rank the chunk directly, independently of the compiler's round-robin loop.
                before = sum(min(turn, count) for count in counts.values())
                before += sum(counts[n] > turn for n in layout.order[:pos])
                result[name][i] = before * layout.chunk_bytes + remainder
        cursor = sum(counts.values()) * layout.chunk_bytes
    return result, cursor


def reference_regions(layout, bounds):
    """Describe each region's stripe sequence without materializing empty padding."""
    result, used = {}, {}
    for group, regions, stripe in layout.groups:
        local, size = reference_placement(group, bounds)
        stripes, bases = {}, {}
        total = len(range(0, size, stripe))
        for i, region in enumerate(regions):
            stripes[region] = range(i, total, len(regions))
            bases[region] = used.get(region, 0) + (-used.get(region, 0)) % stripe
            if stripes[region]:
                used[region] = bases[region] + len(stripes[region]) * stripe
        for name, offsets in local.items():
            result[name] = {}
            for i, offset in offsets.items():
                chunk = offset // stripe
                region = next(r for r in regions if chunk in stripes[r])
                result[name][i] = (region, bases[region] + stripes[region].index(chunk) * stripe + offset % stripe)
    return result, used


def reference_groups(workload):
    """Read source events directly, without the executor's groups() expansion."""
    written, shapes = dict.fromkeys(workload.shapes, 0), workload.shapes
    for index, event in enumerate(workload.events):
        if event.op == "end":
            return
        name = event.object_id
        if event.op in ("create", "invalidate"):
            written[name] = 0
            yield index, name, event.op, ()
            continue
        width = shapes[name].dimensions
        if event.op == "append":
            start = written[name]
            written[name] += event.count
            groups = [list(range(t * width, (t + 1) * width)) for t in range(start, written[name])]
            kind = "write"
        else:
            rows = [[t * width + d for d in range(*event.dimensions)] for t in range(*event.tokens)]
            groups = rows if event.group_by == "token" else list(zip(*rows))
            kind = "read"
        for indices in groups:
            yield index, name, kind, indices


class ReferenceAllocation:
    """Independent stripe positions and first-fit from possible gap endpoints."""
    def __init__(self, layout, bounds, system, reserve_on=None):
        self.system, self.local, self.groups, self.owners = system, {}, [], {}
        self.active, self.leases, self.generations = set(), {}, dict.fromkeys(bounds, 0)
        self.reserve_on = reserve_on if reserve_on is not None else dict.fromkeys(bounds, "create")
        self.moves, self.relocations = {}, {}
        for index, (group, regions, stripe) in enumerate(layout.groups):
            offsets, size = reference_placement(group, bounds)
            total = len(range(0, size, stripe))
            sizes = {r: len(range(i, total, len(regions))) * stripe for i, r in enumerate(regions) if i < total}
            self.groups.append((sizes, stripe))
            for name, positions in offsets.items():
                self.owners[name] = index
                self.local[name] = {i: (regions[(v // stripe) % len(regions)],
                                       (v // (stripe * len(regions))) * stripe + v % stripe) for i, v in positions.items()}

    def reserve(self, sizes, alignment, name, event):
        planned = []
        extents = [e for lease in self.leases.values() for e in lease]
        extents += [old if published else new for old, new, published in self.moves.values()]
        for region, size in sorted(sizes.items()):
            occupied = [(b, b + s) for r, b, s in extents if r == region]
            starts = {((p + alignment - 1) // alignment) * alignment for p in [0, *(hi for _, hi in occupied)]}
            valid = [p for p in starts if p + size <= self.system.regions[region].size_bytes
                     and all(p + size <= lo or p >= hi for lo, hi in occupied)]
            if not valid:
                raise CandidateError("allocation_exhausted", "$.dsl.kv_layout.groups", "Reference allocation cannot fit",
                                     object=name, workload_event=event, region=region)
            planned.append((region, min(valid), size))
        return tuple(planned)

    def apply(self, event, name, kind):
        group = self.owners[name]
        if kind in ("create", "reserve"):
            changed = group not in self.leases and (kind == "reserve" or self.reserve_on[name] == "create")
            if changed:
                sizes, alignment = self.groups[group]
                self.leases[group] = self.reserve(sizes, alignment, name, event)
            if kind == "create":
                self.active.add(name)
                self.generations[name] += 1
            extents = self.leases.get(group, ())
        else:
            extents = self.leases.get(group, ())
            self.active.remove(name)
            changed = group in self.leases and not any(self.owners[n] == group for n in self.active)
            if changed:
                del self.leases[group]
            self.relocations.pop(name, None)
        return AllocationEvent(event, name, kind, self.generations[name], group, extents, changed)

    def positions(self, name):
        bases = {r: b for r, b, _ in self.leases[self.owners[name]]}
        return {i: (self.relocations.get(name, r), bases[self.relocations.get(name, r)] + offset)
                for i, (r, offset) in self.local[name].items()}

    def move(self, event, name, kind, region):
        group = self.owners[name]
        if kind == "reserve":
            source, = self.leases[group]
            destination, = self.reserve({region: source[2]}, self.groups[group][1], name, event)
            self.moves[group] = source, destination, False
        else:
            source, destination, _ = self.moves[group]
            if kind == "publish":
                self.leases[group] = (destination,)
                self.relocations[name] = destination[0]
                self.moves[group] = source, destination, True
            else:
                del self.moves[group]
        return MigrationEvent(event, name, self.generations[name], kind, source, destination)


def reference_execution(workload, lifetime, migration):
    """Expected ordered copy windows from independently computed element positions."""
    rules = migration["rules"] if migration else {}
    written, migrated = {}, set()

    def copy_object(event, name):
        yield event, name, "reserve_move", (), None
        source = lifetime.positions(name)
        region, base, _ = lifetime.moves[lifetime.owners[name]][1]
        destination = {i: (region, base + p) for i, (_, p) in lifetime.local[name].items()}
        ordered = sorted(lifetime.local[name], key=lambda i: lifetime.local[name][i][1])
        for _, members in groupby(ordered, key=lambda i: lifetime.local[name][i][1] // migration["buffer_bytes"]):
            chunk = tuple(members)
            yield event, name, "read", chunk, source
            yield event, name, "write", chunk, destination
        yield event, name, "publish_move", (), None
        yield event, name, "release_move", (), None
        migrated.add(name)

    for event, name, kind, indices in reference_groups(workload):
        if kind in ("create", "invalidate"):
            written[name] = 0
            migrated.discard(name)
        rule = rules.get(name, {})
        shape = workload.shapes[name]
        size = shape.tokens * shape.dimensions
        if (kind == "read" and rule.get("trigger") == "before_read"
                and written.get(name, 0) == size and name not in migrated):
            yield from copy_object(event, name)
        yield event, name, kind, indices, None
        if kind == "write":
            written[name] = written.get(name, 0) + len(indices)
            if written[name] == size and rule.get("trigger") == "on_full" and name not in migrated:
                yield from copy_object(event, name)


def verify_contents(workload, layout, bounds, hardware, mapping, base_address, transfers, reserve_on=None, migration=None,
                    nand_mappings=None, nand_buffer_pages=1, nand_gc=None, nand_gc_policies=None):
    """Check placement, routing, group coverage, masks and actual read/write contents.

    Tags model bytes without claiming numerical attention or concurrent execution.
    The reference never uses compiled offsets, decode(), or the workload group generator.
    """
    lifetime = ReferenceAllocation(layout, bounds, hardware, reserve_on) if workload.version == 3 else None
    if lifetime:
        expected = lifetime.local
        actual = layout.offsets(bounds, [{r: 0 for r in regions} for _, regions, _ in layout.groups])
        allocation_matches = layout.group_requirements(bounds) == [sizes for sizes, _ in lifetime.groups]
    else:
        expected, allocated = reference_placement(layout, bounds)
        actual = layout.offsets(bounds)
        allocation_matches = layout.allocated_bytes(bounds) == allocated
    if set(actual) != set(expected) or not allocation_matches:
        return {"status": "fail", "reason": "allocation_mismatch"}
    for name, offsets in expected.items():
        if len(actual[name]) != len(offsets):
            return {"status": "fail", "reason": "element_count_mismatch", "object": name}
        for index, offset in offsets.items():
            if actual[name][index] != offset:
                return {"status": "fail", "reason": "placement_mismatch", "object": name,
                        "element": list(divmod(index, bounds[name].dimensions)),
                        "expected_address": offset, "actual_address": actual[name][index]}
    system = hardware if isinstance(hardware, MemorySystem) else None
    if system:
        if not lifetime and layout.reservations(bounds)[1] != reference_regions(layout, bounds)[1]:
            return {"status": "fail", "reason": "region_allocation_mismatch"}
        geometries = {n: t.geometry for n, t in system.targets.items()}
        masks = {n: mapping[t.mapping_group].masks(t.geometry) for n, t in system.targets.items() if t.medium == "dram"}
    else:
        geometries, masks = {"dram0": hardware}, {"dram0": mapping.masks(hardware)}
    nand_mappings = nand_mappings or {}
    controllers = {n: NANDExecutor(t, nand_mappings.get(n), nand_buffer_pages, nand_gc, (nand_gc_policies or {}).get(n))
                   for n, t in system.targets.items() if t.medium == "nand"} if system else {}
    block_controllers, block_references = {}, {}
    for n, t in system.targets.items() if system else ():
        if t.medium != "ssd":
            continue
        rule = (nand_gc_policies or {}).get(n)
        block_controllers[n] = (NANDBackedSSD(t, nand_mappings.get(n), nand_buffer_pages, nand_gc, rule)
                                if t.nand else BlockExecutor(t))
        block_references[n] = ReferenceSSD(t, nand_mappings.get(n), nand_gc, rule)
    nand_references = {n: ReferenceNAND(system.targets[n], nand_mappings.get(n), nand_gc, (nand_gc_policies or {}).get(n))
                       for n in controllers}
    nand_bindings = {n: reference.bindings for n, reference in nand_references.items()}

    def physical_key(target, address):
        block, byte = divmod(address, geometries[target].granule_bytes)
        if target in controllers or target in block_controllers:
            return target, block, byte  # Logical key; physical bindings are independently replayed below.
        return target, sum(((block & mask).bit_count() & 1) << i for i, mask in enumerate(masks[target])), byte

    def expected_key(offset, byte):
        if system:
            region, relative = offset
            resource = system.regions[region]
            return physical_key(resource.target, resource.base_address + relative + byte)
        return physical_key("dram0", base_address + offset + byte)

    memory, groups, transactions, checked_bytes, read_elements, lifecycle_events = {}, 0, 0, 0, 0, 0
    transfers = iter(transfers)
    completed_moves, copied_bytes, copy_buffer = 0, 0, {}
    for reference in reference_execution(workload, lifetime, migration):
        batch = next(transfers, None)
        if batch is None:
            return {"status": "fail", "reason": "group_count_mismatch", "group": groups,
                    "workload_event": reference[0]}
        event, name, kind, indices, copy_positions = reference
        transfer_context = {}

        def fail(reason, **details):
            return {"status": "fail", "reason": reason, "workload_event": event,
                    "object": name, "group": groups, **transfer_context, **details}

        def release_nand(extents):
            for region, base, size in extents:
                resource = system.regions[region]
                target = resource.target
                if target not in controllers:
                    continue  # DRAM/SSD storage remains untouched; only allocator ownership changes.
                controller = controllers[target]
                width = controller.state.geometry.page_bytes
                first, end = (resource.base_address + base) // width, (resource.base_address + base + size) // width
                wanted_pages = tuple(sorted(p for p in nand_bindings[target] if first <= p < end))
                try:
                    actual_pages = controller.release_range(resource.base_address + base, size)
                except CandidateError as error:
                    return fail(error.code, target=target, region=region, **error.details)
                if actual_pages != wanted_pages:
                    return fail("nand_release_mismatch", target=target)
                for page in wanted_pages:
                    nand_references[target].release(page)
                    for byte in range(width):
                        memory.pop((target, page, byte), None)
                if controller.state.bindings != nand_bindings[target]:
                    return fail("nand_release_mismatch", target=target)

        if kind.endswith("_move"):
            phase = kind.removesuffix("_move")
            wanted_event = lifetime.move(event, name, phase, migration["rules"][name]["destination"])
            if batch != wanted_event:
                return fail("migration_event_mismatch")
            if phase == "release":
                error = release_nand((wanted_event.source,))
                if error:
                    return error
                completed_moves += 1
            continue

        if kind in ("create", "invalidate"):
            wanted_event = lifetime.apply(event, name, kind)
            if batch != wanted_event:
                return fail("allocation_event_mismatch")
            if kind == "invalidate" and wanted_event.changed:
                error = release_nand(wanted_event.extents)
                if error:
                    return error
            lifecycle_events += 1
            continue
        if lifetime and lifetime.owners[name] not in lifetime.leases:
            if batch != lifetime.apply(event, name, "reserve"):
                return fail("allocation_event_mismatch")
            batch = next(transfers, None)
        if not isinstance(batch, tuple) or batch[:2] != (event, name):
            return fail("group_source_mismatch")
        positions = copy_positions if copy_positions is not None else lifetime.positions(name) if lifetime else expected[name]
        wanted = {expected_key(positions[i], byte): ((name, lifetime.generations[name], i, byte) if lifetime else (name, i, byte))
                  for i in indices for byte in range(bounds[name].element_bytes)}
        if copy_positions is not None and kind == "write":
            if set(copy_buffer) != set(wanted.values()):
                return fail("migration_buffer_mismatch")
            wanted = {key: copy_buffer[tag] for key, tag in wanted.items()}
        seen = set()
        for transfer in batch[2]:
            access, tags = transfer.access, transfer.byte_tags
            req = access.request
            target = transfer.target
            if system:
                transfer_context = {"target": target, "region": transfer.region}
            if target not in geometries:
                return fail("unknown_target", target=target, request_id=req.request_id)
            geometry = geometries[target]
            if system:
                region = system.regions.get(transfer.region)
                if (region is None or region.target != target or req.address < region.base_address
                        or req.address + geometry.granule_bytes > region.base_address + region.size_bytes):
                    return fail("region_mismatch", target=target, region=transfer.region, request_id=req.request_id)
            elif transfer.region != "main":
                return fail("region_mismatch", region=transfer.region, request_id=req.request_id)
            if (req.kind != kind or req.stream != event or req.request_id != transactions or req.arrival != 0
                    or not 0 <= req.address < geometry.capacity_bytes or req.address % geometry.granule_bytes):
                return fail("request_mismatch", request_id=req.request_id)
            if target in controllers:
                if not isinstance(access, PageRequest):
                    return fail("nand_access_type", request_id=req.request_id)
                routed = req.address // geometry.page_bytes
            elif target in block_controllers:
                if not isinstance(access, BlockRequest):
                    return fail("block_access_type", request_id=req.request_id)
                routed = req.address // geometry.block_bytes
            else:
                if not isinstance(access, DRAMRequest):
                    return fail("dram_access_type", request_id=req.request_id)
                coords = (access.channel, access.bank, access.row, access.column)
                limits = (geometry.channels, geometry.banks_per_channel, geometry.rows, geometry.columns)
                if any(type(v) is not int or not 0 <= v < limit for v, limit in zip(coords, limits)):
                    return fail("invalid_coordinate", request_id=req.request_id)
                routed = (access.channel | (access.bank << geometry.channel_bits)
                          | (access.column << (geometry.channel_bits + geometry.bank_bits))
                          | (access.row << (geometry.channel_bits + geometry.bank_bits + geometry.column_bits)))
                if routed != physical_key(target, req.address)[1]:
                    return fail("routing_mismatch", request_id=req.request_id)
            mask = access.valid_bytes or tuple(range(geometry.granule_bytes))
            if (any(type(b) is not int or not 0 <= b < geometry.granule_bytes for b in mask)
                    or len(set(mask)) != len(mask) or len(tags) != len(mask) or not tags):
                return fail("invalid_mask", request_id=req.request_id)
            for byte, tag in zip(mask, tags):
                key = target, routed, byte
                if key in seen or key not in wanted or wanted[key] != tag:
                    return fail("masked_byte_mismatch", request_id=req.request_id, byte=byte)
                seen.add(key)
                if kind == "write":
                    memory[key] = tag
                elif memory.get(key) != tag:
                    return fail("content_mismatch", request_id=req.request_id, byte=byte,
                                expected_value=tag, actual_value=memory.get(key))
                if copy_positions is not None and kind == "read":
                    copy_buffer[tag] = memory[key]
            if target in controllers:
                problem = nand_references[target].replay(transfer, controllers[target], memory)
                if problem:
                    return fail(problem.pop("reason"), request_id=req.request_id, **problem)
            if target in block_controllers:
                problem = block_references[target].replay(transfer, block_controllers[target], memory)
                if problem:
                    return fail(problem.pop("reason"), request_id=req.request_id, **problem)
            transactions += 1
            checked_bytes += len(mask)
        if seen != set(wanted):
            return fail("missing_bytes", expected_bytes=len(wanted), actual_bytes=len(seen))
        groups += 1
        read_elements += len(indices) if kind == "read" else 0
        if copy_positions is not None and kind == "write":
            copied_bytes += len(wanted)
            copy_buffer.clear()
    extra = next(transfers, None)
    if extra is not None:
        return {"status": "fail", "reason": "group_count_mismatch", "group": groups,
                "workload_event": extra.source_event if isinstance(extra, (AllocationEvent, MigrationEvent)) else extra[0]}
    for target, controller in controllers.items():
        for logical, page in nand_bindings[target].items():
            width = controller.state.geometry.page_bytes
            if controller.state.pages[page] != tuple(memory.get((target, logical, b)) for b in range(width)):
                return {"status": "fail", "reason": "nand_final_content_mismatch", "target": target, "logical_page": logical}
    for target, controller in block_controllers.items():
        width = controller.target.geometry.block_bytes
        expected_blocks = {lba for t, lba, _ in memory if t == target}
        if set(controller.blocks) != expected_blocks or any(
                data != tuple(memory.get((target, lba, b)) for b in range(width)) for lba, data in controller.blocks.items()):
            return {"status": "fail", "reason": "block_final_content_mismatch", "target": target}
    return {"status": "pass", "method": "independent placement and byte-tag transaction replay",
            "objects": len(bounds), "elements": sum(len(v) for v in expected.values()), "groups": groups,
            "read_elements": read_elements, "transactions": transactions, "checked_bytes": checked_bytes,
            **({"lifetime_events": lifecycle_events} if lifetime else {}),
            **({"migrations": completed_moves, "copied_bytes": copied_bytes} if migration else {})}
