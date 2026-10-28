"""Atomic first-fit group reservations with fixed or supplied lifecycle triggers."""

from agent_design_search.contracts import AllocationEvent, MigrationEvent, CandidateError


class GroupAllocator:
    def __init__(self, capacities, groups, reserve_on=None):
        # Each descriptor contains object names, per-region sizes and alignment.
        self.capacities, self.groups = capacities, groups
        self.owners = {name: i for i, (names, _, _) in enumerate(groups) for name in names}
        self.reserve_on = reserve_on if reserve_on is not None else dict.fromkeys(self.owners, "create")
        self.active, self.leases, self.generations = set(), {}, dict.fromkeys(self.owners, 0)
        self.allocations = self.releases = self.peak_bytes = 0
        self.peak_by_region = dict.fromkeys(capacities, 0)
        self.moves, self.relocations = {}, {}

    def occupied(self):
        return [extent for lease in self.leases.values() for extent in lease] + [
            source if published else destination for source, destination, published in self.moves.values()]

    def usage(self):
        usage = dict.fromkeys(self.capacities, 0)
        for region, _, size in self.occupied():
            usage[region] += size
        return usage

    def reserve(self, sizes, alignment, name, source_event):
        pending = []
        for region, size in sorted(sizes.items()):
            occupied = sorted((base, base + length) for r, base, length in self.occupied() if r == region)
            cursor = 0
            for low, high in occupied:
                cursor += (-cursor) % alignment
                if cursor + size <= low:
                    break
                cursor = high
            cursor += (-cursor) % alignment
            if cursor + size > self.capacities[region]:
                raise CandidateError("allocation_exhausted", f"$.dsl.kv_layout.groups[{self.owners[name]}]",
                                     "No aligned contiguous extent fits this live group",
                                     object=name, workload_event=source_event, region=region,
                                     required_bytes=size, capacity_bytes=self.capacities[region])
            pending.append((region, cursor, size))
        return tuple(pending)

    def record_peak(self):
        usage = self.usage()
        self.peak_bytes = max(self.peak_bytes, sum(usage.values()))
        for region, size in usage.items():
            self.peak_by_region[region] = max(self.peak_by_region[region], size)

    def apply(self, kind, name, source_event):
        group = self.owners[name]
        if (kind not in ("create", "invalidate", "reserve")
                or (kind == "create") == (name in self.active)
                or (kind == "reserve" and group in self.leases) or group in self.moves):
            raise CandidateError("invalid_lifetime", "$.workload.events", "Invalid allocator lifetime transition",
                                 object=name, workload_event=source_event)
        if kind in ("create", "reserve"):
            changed = group not in self.leases and (kind == "reserve" or self.reserve_on[name] == "create")
            if changed:
                _, sizes, alignment = self.groups[group]
                # Commit only after every selected region fits.
                self.leases[group] = self.reserve(sizes, alignment, name, source_event)
                self.allocations += 1
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
                self.releases += 1
            self.relocations.pop(name, None)
        self.record_peak()
        return AllocationEvent(source_event, name, kind, self.generations[name], group, extents, changed)

    def move(self, kind, name, region, source_event):
        group = self.owners[name]
        if name not in self.active or group not in self.leases or len(self.groups[group][0]) != 1:
            raise CandidateError("invalid_migration", "$.execution.migration", "Migration requires a reserved singleton group", object=name)
        if kind == "reserve":
            if group in self.moves or len(self.leases[group]) != 1 or region == self.leases[group][0][0]:
                raise CandidateError("invalid_migration", "$.execution.migration", "Need one source and a distinct destination", object=name)
            source = self.leases[group][0]
            destination, = self.reserve({region: source[2]}, self.groups[group][2], name, source_event)
            self.moves[group] = source, destination, False
            self.allocations += 1
        else:
            if group not in self.moves or kind not in ("publish", "release"):
                raise CandidateError("invalid_migration", "$.execution.migration", "No matching migration transition", object=name)
            source, destination, published = self.moves[group]
            if published != (kind == "release"):
                raise CandidateError("invalid_migration", "$.execution.migration", "Invalid publication/release order", object=name)
            if kind == "publish":
                self.leases[group] = (destination,)
                self.relocations[name] = destination[0]
                self.moves[group] = source, destination, True
            else:
                del self.moves[group]
                self.releases += 1
        self.record_peak()
        return MigrationEvent(source_event, name, self.generations[name], kind, source, destination)

    def abort_move(self, name):
        """Discard an unpublished reservation when a candidate stops; keep its source."""
        group = self.owners[name]
        if group in self.moves and not self.moves[group][2]:
            del self.moves[group]
            self.releases += 1

    def address(self, name, region, offset):
        if name not in self.active:
            raise CandidateError("inactive_object", "$.workload.events", "Access outside object lifetime", object=name)
        if self.owners[name] not in self.leases:
            raise CandidateError("unreserved_object", "$.workload.events", "Access requires a group reservation", object=name)
        extent = next(((base, size) for r, base, size in self.leases[self.owners[name]] if r == region), None)
        if extent is None:
            raise CandidateError("allocation_region", "$.dsl.kv_layout", "Access uses an obsolete or unowned region", object=name, region=region)
        base, size = extent
        if not 0 <= offset < size:
            raise CandidateError("allocation_range", "$.dsl.kv_layout", "Relative address outside group extent", object=name)
        return base + offset

    def metrics(self):
        usage = self.usage()
        return {"allocated_bytes": sum(usage.values()), "allocated_bytes_by_region": usage,
                "peak_allocated_bytes": self.peak_bytes, "peak_allocated_bytes_by_region": self.peak_by_region.copy(),
                "group_allocations": self.allocations, "group_releases": self.releases}
