"""Compile fixed KV groups into tagged DRAM/page/block accesses, then execute them."""

from dataclasses import asdict, dataclass, replace

from agent_design_search.contracts import Request, DRAMRequest, PageRequest, BlockRequest, AllocationEvent, MigrationEvent, CandidateError
from agent_design_search.inputs.hardware import MemorySystem
from agent_design_search.performance.dram import FCFS
from agent_design_search.performance.memory_system import SystemFCFS
from agent_design_search.execution.nand import NANDExecutor
from agent_design_search.execution.ssd import BlockExecutor, NANDBackedSSD
from agent_design_search.execution.allocation import GroupAllocator


def route_requests(requests, mapping, hardware):
    routed = []
    for request in requests:
        channel, bank, row, column, offset = mapping.decode(request.address, hardware)
        if offset:
            raise ValueError("Address traces require aligned, full-granule requests")
        routed.append(DRAMRequest(request, channel, bank, row, column))
    return routed


@dataclass(frozen=True)
class KVTransfer:
    access: DRAMRequest | PageRequest | BlockRequest
    byte_tags: tuple  # (object, element, byte); lifecycle inputs add generation after object.
    target: str = "dram0"
    region: str = "main"


def describe_transfer(transfer, bounds, object_offsets, hardware, base_address):
    """Explain the emitted transfer, without inventing a virtual-memory mapping."""
    access = transfer.access
    system = hardware if isinstance(hardware, MemorySystem) else None
    geometry = system.targets[transfer.target].geometry if system else hardware
    region_base = system.regions[transfer.region].base_address if system else base_address
    space = ("dram_target_bytes" if isinstance(access, DRAMRequest) else
             "nand_logical_bytes" if isinstance(access, PageRequest) else "ssd_logical_bytes")
    origins = []
    for block_offset, tag in zip(access.valid_bytes, transfer.byte_tags):
        name, (index, byte) = tag[0], tag[-2:]
        address = access.request.address + block_offset
        origins.append({"index": list(divmod(index, bounds[name].dimensions)), "element_byte": byte,
                        "object_offset": object_offsets[name][index] + byte,
                        "region_offset": address - region_base, "target_address": address,
                        "block_offset": block_offset})
    result = {"target": transfer.target, "region": transfer.region, "kind": access.request.kind,
              "address_space": space, "granule_bytes": geometry.granule_bytes, "byte_origins": origins}
    if isinstance(access, DRAMRequest):
        result["dram_location"] = {key: getattr(access, key) for key in ("channel", "bank", "row", "column")}
    return result


def prepare_layout(workload, layout, bounds, hardware, reserve_on=None):
    if workload.version != 3:
        return layout.offsets(bounds), None
    bases = [{r: 0 for r in regions} for _, regions, _ in layout.groups]
    groups = [(group.order, sizes, stripe) for (group, _, stripe), sizes in
              zip(layout.groups, layout.group_requirements(bounds))]
    allocator = GroupAllocator({r: v.size_bytes for r, v in hardware.regions.items()}, groups, reserve_on)
    return layout.offsets(bounds, bases), allocator


def kv_transfers(workload, offsets, hardware, mapping, base_address=0, allocator=None, migration=None):
    """Generate the actual masked transfers; no coalescing across logical groups."""
    request_id, shapes = 0, workload.shapes
    system = hardware if isinstance(hardware, MemorySystem) else None
    rules = migration["rules"] if migration else {}
    full, moved = set(), set()

    def build(source_event, name, kind, indices, extent=None):
        nonlocal request_id
        lines = {}
        for index in indices:
            if system:
                region, offset = offsets[name][index]
                if extent:
                    region, base, _ = extent
                    offset += base
                elif allocator:
                    region = allocator.relocations.get(name, region)
                    offset = allocator.address(name, region, offset)
                resource = system.regions[region]
                target, address = resource.target, resource.base_address + offset
                geometry = system.targets[target].geometry
            else:
                target, region, geometry = "dram0", "main", hardware
                address = base_address + offsets[name][index]
            for byte in range(shapes[name].element_bytes):
                line, offset = divmod(address + byte, geometry.granule_bytes)
                tag = (name, allocator.generations[name], index, byte) if allocator else (name, index, byte)
                lines.setdefault((target, region, line), {})[offset] = tag
        transfers = []
        for (target, region, line), tags in sorted(lines.items()):
            geometry = system.targets[target].geometry if system else hardware
            request = Request(request_id, line * geometry.granule_bytes, 0, kind, source_event)
            mask = tuple(sorted(tags))
            if system and system.targets[target].medium == "nand":
                access = PageRequest(request, mask)
            elif system and system.targets[target].medium == "ssd":
                access = BlockRequest(request, mask)
            else:
                policy = mapping[system.targets[target].mapping_group] if system else mapping
                channel, bank, row, column, _ = policy.decode(request.address, geometry)
                access = DRAMRequest(request, channel, bank, row, column, mask)
            transfers.append(KVTransfer(access, tuple(tags[b] for b in mask), target, region))
            request_id += 1
        return source_event, name, transfers

    def move_object(source_event, name):
        destination = rules[name]["destination"]
        try:
            event = allocator.move("reserve", name, destination, source_event)
            yield event
            chunks = {}
            for index, (_, offset) in enumerate(offsets[name]):
                chunks.setdefault(offset // migration["buffer_bytes"], []).append(index)
            for _, chunk in sorted(chunks.items()):
                yield build(source_event, name, "read", chunk, event.source)
                yield build(source_event, name, "write", chunk, event.destination)
            yield allocator.move("publish", name, destination, source_event)
            yield allocator.move("release", name, destination, source_event)
            moved.add(name)
        finally:
            allocator.abort_move(name)

    for source_event, name, kind, indices in workload.groups():
        try:
            lifecycle = kind in ("create", "invalidate")
            needs_reservation = allocator is not None and not lifecycle and allocator.owners[name] not in allocator.leases
            if lifecycle or needs_reservation:
                if allocator is None:
                    raise ValueError("Lifecycle workloads require a group allocator")
                yield allocator.apply(kind if lifecycle else "reserve", name, source_event)
                if lifecycle:
                    full.discard(name)
                    moved.discard(name)
                    continue
            trigger = rules.get(name, {}).get("trigger")
            if kind == "read" and trigger == "before_read" and name in full and name not in moved:
                yield from move_object(source_event, name)
            yield build(source_event, name, kind, indices)
            if kind == "write" and indices[-1] == shapes[name].tokens * shapes[name].dimensions - 1:
                full.add(name)
                if trigger == "on_full" and name not in moved:
                    yield from move_object(source_event, name)
        except CandidateError as error:
            region = error.details.get("region")
            context = {"target": system.regions[region].target} if system and region in system.regions else {}
            raise CandidateError(error.code, error.path, str(error), **{**error.details, **context}) from error


def execute_kv(workload, bounds, layout, mapping, hardware, base_address, backend, parameters,
               include_events=False, nand_parameters=None, ssd_parameters=None, allocation_parameters=None, reserve_on=None,
               migration=None, migration_parameters=None, nand_mappings=None, nand_execution=None, nand_gc_policies=None):
    offsets, allocator = prepare_layout(workload, layout, bounds, hardware, reserve_on)
    traffic = {f"{kind}_{suffix}": 0 for kind in ("read", "write") for suffix in ("transactions", "useful_bytes")}
    system = hardware if isinstance(hardware, MemorySystem) else None
    object_offsets = {}
    if include_events:
        groups = [group for group, _, _ in layout.groups] if system else [layout]
        object_offsets = {name: item.offsets(bounds[name]) for group in groups for name, item in group.objects}
    if system:
        engine = SystemFCFS(system, parameters, nand_parameters, ssd_parameters, nand_execution)
    elif backend == "fcfs-serial-kv-v2":
        engine = FCFS(hardware, parameters)
    else:
        engine = None  # The single-resource traffic profile has no timing engine.
    controllers = {}
    for name, target in system.targets.items() if system else ():
        if target.medium == "dram":
            continue
        options = ((nand_mappings or {}).get(name), nand_execution["buffer_pages"] if nand_execution else 1,
                   nand_execution.get("gc") if nand_execution else None, (nand_gc_policies or {}).get(name))
        if target.medium == "nand":
            controllers[name] = NANDExecutor(target, *options)
        elif target.nand:
            controllers[name] = NANDBackedSSD(target, *options)
        else:
            controllers[name] = BlockExecutor(target)
    ready, sources, transferred_bytes, group_id = 0, [], 0, 0
    lifetime_events, management_ps = [], 0
    peak_by_target = dict.fromkeys(system.targets, 0) if allocator else {}
    moving, migration_events, current_move = False, [], None
    copy_bytes = copy_traffic = copy_management = buffer_peak = 0

    def record_allocation_peak():
        usage = allocator.usage()
        for target in peak_by_target:
            used = sum(size for region, size in usage.items() if system.regions[region].target == target)
            peak_by_target[target] = max(peak_by_target[target], used)

    def release_nand(extents, source_event):
        released = {}
        for region, base, size in extents:
            resource = system.regions[region]
            if system.targets[resource.target].medium == "nand":
                pages = controllers[resource.target].release_range(resource.base_address + base, size)
                released.setdefault(resource.target, []).extend(pages)
        engine.invalidate_nand(released, source_event)

    for item in kv_transfers(workload, offsets, hardware, mapping, base_address, allocator, migration):
        if isinstance(item, MigrationEvent):
            current_move = item
            start = engine.ready
            cost = (migration_parameters["publish_ps"] if item.kind == "publish"
                    else allocation_parameters[item.kind + "_ps"])
            engine.ready += cost
            copy_management += cost
            moving = item.kind != "release"
            if item.kind == "reserve":
                buffer_peak = max(buffer_peak, min(migration["buffer_bytes"], item.destination[2]))
            if item.kind == "release":
                release_nand((item.source,), item.source_event)
            migration_events.append({**asdict(item), "start_ps": start, "finish_ps": engine.ready})
            record_allocation_peak()
            continue
        if isinstance(item, AllocationEvent):
            start = engine.ready
            cost = allocation_parameters[item.kind + "_ps"]
            if item.changed and item.kind != "reserve":
                cost += allocation_parameters.get("reserve_ps" if item.kind == "create" else "release_ps", 0)
            engine.ready += cost
            management_ps += cost
            if item.kind == "invalidate" and item.changed:
                release_nand(item.extents, item.source_event)
            lifetime_events.append({**asdict(item), "start_ps": start, "finish_ps": engine.ready})
            record_allocation_peak()
            continue
        source_event, name, transfers = item
        if moving and transfers[0].access.request.kind == "write":
            start = engine.ready
            cost = sum(len(t.access.valid_bytes) for t in transfers) * migration_parameters["copy_ps_per_byte"]
            engine.ready += cost
            copy_management += cost
            migration_events.append({**asdict(current_move), "kind": "buffer_copy", "start_ps": start, "finish_ps": engine.ready})
        batch, nand_actions, block_actions = [], {}, {}
        for transfer in transfers:
            access = transfer.access
            kind = access.request.kind
            if moving:
                copy_traffic += system.targets[transfer.target].geometry.granule_bytes
                copy_bytes += len(access.valid_bytes) if kind == "write" else 0
            traffic[kind + "_transactions"] += 1
            traffic[kind + "_useful_bytes"] += len(access.valid_bytes)
            transferred_bytes += system.targets[transfer.target].geometry.granule_bytes if system else hardware.granule_bytes
            batch.append((transfer.target, access) if system else replace(access, request=replace(access.request, arrival=ready)))
            if transfer.target in controllers:
                try:
                    actions = nand_actions if system.targets[transfer.target].medium == "nand" else block_actions
                    actions[access.request.request_id], _ = controllers[transfer.target].execute(access, transfer.byte_tags)
                except CandidateError as error:
                    raise CandidateError(error.code, error.path, str(error), **{**error.details, "object": name,
                                         "workload_event": source_event, "target": transfer.target,
                                         "region": transfer.region, "request_id": access.request.request_id}) from error
            if include_events:
                sources.append({"request_id": access.request.request_id, "workload_event": source_event,
                                "group": group_id, "object": name, "address": access.request.address,
                                "valid_bytes": list(access.valid_bytes),
                                **describe_transfer(transfer, bounds, object_offsets, hardware, base_address)})
                if allocator:
                    sources[-1]["generation"] = allocator.generations[name]
                if migration:
                    sources[-1]["purpose"] = "migration" if moving else "workload"
        if engine:
            ready = engine.submit_batch(batch, nand_actions, block_actions) if controllers else engine.submit_batch(batch)
        group_id += 1
    result = {**traffic, "allocated_bytes": allocator.metrics()["allocated_bytes"] if allocator else layout.allocated_bytes(bounds), "backend": backend,
              "transferred_bytes": transferred_bytes}
    if system:
        allocated = allocator.usage() if allocator else layout.reservations(bounds)[1]
        result["allocated_bytes_by_region"] = allocated
        result["allocated_bytes_by_target"] = {target: sum(v for r, v in allocated.items() if system.regions[r].target == target)
                                                for target in system.targets}
    if engine:
        result.update(engine.metrics(include_events))
        result["backend"] = backend
    if controllers:
        result["frontend_transferred_bytes"] = transferred_bytes
        result["transferred_bytes"] = sum(v["transferred_bytes"] for v in result["per_target"].values())
        for name, controller in controllers.items():
            result["per_target"][name].update(controller.metrics())
            if system.targets[name].medium == "ssd" and system.targets[name].nand:
                timing = engine.engines[name].nand
                page_bytes = system.targets[name].nand.geometry.page_bytes
                result["per_target"][name]["nand"] = {**timing.counts, **controller.nand.metrics(),
                    "completion_ps": timing.completed, "scratch_peak_bytes": timing.counts["peak_inflight_pages"] * page_bytes,
                    "peak_live_bytes": timing.peak_live_pages * page_bytes}
            if nand_execution and system.targets[name].medium == "nand":
                nand, page_bytes = engine.engines[name], system.targets[name].geometry.page_bytes
                result["per_target"][name].update(scratch_peak_bytes=nand.counts["peak_inflight_pages"] * page_bytes,
                                                 peak_live_bytes=nand.peak_live_pages * page_bytes)
    if include_events:
        result["request_sources"] = sources
        result["virtual_memory"] = "not_modeled"
    if allocator:
        result.update(allocator.metrics(), allocation_management_ps=management_ps,
                      lifetime_events=sum(e["kind"] != "reserve" for e in lifetime_events),
                      peak_allocated_bytes_by_target=peak_by_target)
        if include_events:
            result["allocation_events"] = lifetime_events
    if migration:
        result.update(migrations=sum(e["kind"] == "release" for e in migration_events), copied_bytes=copy_bytes,
                      migration_frontend_bytes=copy_traffic, migration_management_ps=copy_management,
                      migration_buffer_peak_bytes=buffer_peak)
        if include_events:
            result["migration_events"] = migration_events
    return result
