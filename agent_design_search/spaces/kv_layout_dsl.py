"""KV layout DSL: object tiling, axis order, composition and region placement."""

from dataclasses import dataclass
from math import prod

from agent_design_search.contracts import CandidateError


AXES = ("token_outer", "dimension_outer", "token_inner", "dimension_inner")


@dataclass(frozen=True)
class LayoutBounds:
    """The fixed object shape and placement limits supplied by a search task."""
    tokens: int
    dimensions: int
    element_bytes: int
    line_bytes: int
    capacity_bytes: int

    def __post_init__(self):
        if (self.element_bytes > self.line_bytes
                or self.tokens * self.dimensions * self.element_bytes > self.capacity_bytes):
            raise ValueError("Object does not fit, or an element exceeds the transfer granule")


@dataclass(frozen=True)
class Layout:
    token: int
    dimension: int
    order: tuple = AXES  # Slowest to fastest varying physical axis.

    @classmethod
    def parse(cls, obj, cfg):
        if not isinstance(obj, dict) or set(obj) != {"version", "tile", "order"}:
            raise CandidateError("invalid_fields", "$.dsl", "Expected version, tile and order")
        if type(obj["version"]) is not int or obj["version"] != 1:
            raise CandidateError("invalid_version", "$.dsl.version", "KV layout requires version 1")
        tile = obj["tile"]
        if not isinstance(tile, dict) or set(tile) != {"token", "dimension"}:
            raise CandidateError("invalid_tile", "$.dsl.tile", "Expected token and dimension tile sizes")
        for axis, maximum in (("token", cfg.tokens), ("dimension", cfg.dimensions)):
            value = tile[axis]
            if type(value) is not int or not 1 <= value <= maximum:
                raise CandidateError("invalid_tile", f"$.dsl.tile.{axis}", "Tile size must be an integer within the shape",
                                     actual=value, minimum=1, maximum=maximum)
        order = obj["order"]
        if (not isinstance(order, list) or len(order) != 4 or any(type(a) is not str for a in order)
                or set(order) != set(AXES)):
            raise CandidateError("invalid_order", "$.dsl.order", "Each outer/inner axis must occur exactly once",
                                 expected=list(AXES), actual=order)
        layout = cls(tile["token"], tile["dimension"], tuple(order))
        if layout.allocated_bytes(cfg) > cfg.capacity_bytes:
            raise CandidateError("capacity_exceeded", "$.dsl.tile", "Padded, line-aligned allocation exceeds capacity",
                                 required_bytes=layout.allocated_bytes(cfg), capacity_bytes=cfg.capacity_bytes)
        return layout

    def to_dict(self):
        return {"version": 1, "tile": {"token": self.token, "dimension": self.dimension}, "order": list(self.order)}

    def extents(self, cfg):
        return dict(zip(AXES, ((cfg.tokens + self.token - 1) // self.token,
                               (cfg.dimensions + self.dimension - 1) // self.dimension,
                               self.token, self.dimension)))

    def allocated_bytes(self, cfg):
        size = prod(self.extents(cfg).values()) * cfg.element_bytes
        return ((size + cfg.line_bytes - 1) // cfg.line_bytes) * cfg.line_bytes

    def offsets(self, cfg):
        """Byte offsets indexed by token * dimensions + dimension; base is fixed at 0."""
        extents, strides, stride = self.extents(cfg), {}, cfg.element_bytes
        for axis in reversed(self.order):
            strides[axis], stride = stride, stride * extents[axis]
        result = []
        for token in range(cfg.tokens):
            for dimension in range(cfg.dimensions):
                coords = (token // self.token, dimension // self.dimension,
                          token % self.token, dimension % self.dimension)
                result.append(sum(c * strides[a] for a, c in zip(AXES, coords)))
        return result


@dataclass(frozen=True)
class KVLayout:
    """Compose independent object layouts in one fixed DRAM region."""
    objects: tuple  # (name, Layout)
    order: tuple
    kind: str = "concat"
    alignment_bytes: int = 1
    chunk_bytes: int = 0

    @classmethod
    def parse(cls, obj, bounds, alignments, chunks):
        if not isinstance(obj, dict) or type(obj.get("version")) is not int:
            raise CandidateError("invalid_version", "$.dsl.version", "Layout requires version 1 or 2")
        # A single compatibility conversion; execution only sees a composed layout.
        if obj["version"] == 1:
            if len(bounds) != 1:
                raise CandidateError("invalid_version", "$.dsl.version", "Multi-object layouts require version 2")
            name = next(iter(bounds))
            local = Layout.parse(obj, bounds[name])
            obj = {"version": 2, "objects": {name: local.to_dict()}, "composition": {
                "kind": "concat", "order": [name], "alignment_bytes": bounds[name].line_bytes}}
        if obj["version"] != 2 or set(obj) != {"version", "objects", "composition"}:
            raise CandidateError("invalid_fields", "$.dsl", "Expected version=2, objects and composition")
        if not isinstance(obj["objects"], dict) or set(obj["objects"]) != set(bounds):
            raise CandidateError("invalid_objects", "$.dsl.objects", "Include every workload object exactly once",
                                 expected=sorted(bounds))
        layouts = []
        for name in sorted(bounds):
            try:
                layouts.append((name, Layout.parse(obj["objects"][name], bounds[name])))
            except CandidateError as error:
                raise CandidateError(error.code, error.path.replace("$.dsl", "$.dsl.objects." + name, 1),
                                     str(error), **error.details) from error
        c = obj["composition"]
        if not isinstance(c, dict) or c.get("kind") not in ("concat", "interleave"):
            raise CandidateError("invalid_composition", "$.dsl.composition", "Choose concat or interleave")
        field = "alignment_bytes" if c["kind"] == "concat" else "chunk_bytes"
        if set(c) != {"kind", "order", field}:
            raise CandidateError("invalid_fields", "$.dsl.composition", "Unexpected composition fields")
        order = c["order"]
        if (not isinstance(order, list) or len(order) != len(bounds) or any(type(n) is not str for n in order)
                or set(order) != set(bounds)):
            raise CandidateError("invalid_order", "$.dsl.composition.order", "List each object exactly once")
        allowed = alignments if c["kind"] == "concat" else chunks
        if type(c[field]) is not int or c[field] not in allowed:
            raise CandidateError("outside_space", "$.dsl.composition." + field, "Value is not allowed by this task",
                                 allowed=list(allowed))
        result = cls(tuple(layouts), tuple(order), c["kind"], **{field: c[field]})
        required = result.allocated_bytes(bounds)
        capacity = next(iter(bounds.values())).capacity_bytes
        if required > capacity:
            raise CandidateError("capacity_exceeded", "$.dsl.composition", "Combined padded allocation exceeds capacity",
                                 required_bytes=required, capacity_bytes=capacity)
        return result

    def to_dict(self):
        field = "alignment_bytes" if self.kind == "concat" else "chunk_bytes"
        return {"version": 2, "objects": {n: layout.to_dict() for n, layout in self.objects},
                "composition": {"kind": self.kind, "order": list(self.order), field: getattr(self, field)}}

    def placements(self, bounds):
        """Per-object chunk starts and total reserved span (including all padding)."""
        sizes = {n: layout.allocated_bytes(bounds[n]) for n, layout in self.objects}
        starts, cursor = {n: [] for n in self.order}, 0
        if self.kind == "concat":
            for name in self.order:
                cursor = ((cursor + self.alignment_bytes - 1) // self.alignment_bytes) * self.alignment_bytes
                starts[name].append(cursor)
                cursor += sizes[name]
        else:
            counts = {n: (size + self.chunk_bytes - 1) // self.chunk_bytes for n, size in sizes.items()}
            for index in range(max(counts.values())):
                for name in self.order:
                    if index < counts[name]:
                        starts[name].append(cursor)
                        cursor += self.chunk_bytes
        return starts, cursor

    def allocated_bytes(self, bounds):
        return self.placements(bounds)[1]

    def offsets(self, bounds):
        starts, _ = self.placements(bounds)
        result = {}
        for name, layout in self.objects:
            local = layout.offsets(bounds[name])
            result[name] = ([starts[name][0] + offset for offset in local] if self.kind == "concat" else
                            [starts[name][offset // self.chunk_bytes] + offset % self.chunk_bytes for offset in local])
        return result


@dataclass(frozen=True)
class PlacedLayout:
    """Object groups composed locally, then striped into fixed hardware regions."""
    groups: tuple  # (KVLayout, ordered region IDs, stripe_bytes)

    @classmethod
    def parse(cls, obj, bounds, alignments, chunks, system, pool, dynamic=False):
        if (not isinstance(obj, dict) or set(obj) != {"version", "objects", "groups"}
                or type(obj["version"]) is not int or obj["version"] != 3):
            raise CandidateError("invalid_fields", "$.dsl", "Topology layout requires version=3, objects and groups")
        if not isinstance(obj["objects"], dict) or set(obj["objects"]) != set(bounds):
            raise CandidateError("invalid_objects", "$.dsl.objects", "Include every workload object exactly once")
        if not isinstance(obj["groups"], list) or not 1 <= len(obj["groups"]) <= len(bounds):
            raise CandidateError("invalid_groups", "$.dsl.groups", "Provide one or more nonempty object groups")
        groups, seen = [], set()
        for i, group in enumerate(obj["groups"]):
            path = f"$.dsl.groups[{i}]"
            if not isinstance(group, dict) or set(group) != {"composition", "placement"}:
                raise CandidateError("invalid_fields", path, "Group requires composition and placement")
            composition = group["composition"]
            order = composition.get("order") if isinstance(composition, dict) else None
            if (not isinstance(order, list) or not order or any(not isinstance(n, str) or n not in bounds for n in order)
                    or len(set(order)) != len(order) or seen.intersection(order)):
                raise CandidateError("invalid_order", path + ".composition.order", "Each object must belong to exactly one group")
            try:
                local = KVLayout.parse({"version": 2, "objects": {n: obj["objects"][n] for n in order},
                                        "composition": composition}, {n: bounds[n] for n in order}, alignments, chunks)
            except CandidateError as error:
                location = error.path if error.path.startswith("$.dsl.objects.") else error.path.replace("$.dsl", path, 1)
                raise CandidateError(error.code, location, str(error), **error.details) from error
            placement = group["placement"]
            if not isinstance(placement, dict) or set(placement) != {"regions", "stripe_bytes"}:
                raise CandidateError("invalid_fields", path + ".placement", "Expected regions and stripe_bytes")
            regions, stripe = placement["regions"], placement["stripe_bytes"]
            if (not isinstance(regions, list) or not regions
                    or any(not isinstance(r, str) or r not in system.pools[pool] for r in regions)
                    or len(set(regions)) != len(regions)):
                raise CandidateError("invalid_regions", path + ".placement.regions", "List distinct regions from the fixed pool")
            if (type(stripe) is not int or stripe not in chunks
                    or (local.kind == "concat" and stripe % local.alignment_bytes)
                    or any(system.regions[r].base_address % stripe for r in regions)):
                raise CandidateError("invalid_alignment", path + ".placement.stripe_bytes",
                                     "Stripe must be allowed, divide region bases and preserve object alignment")
            groups.append((local, tuple(regions), stripe))
            seen.update(order)
        if seen != set(bounds):
            raise CandidateError("invalid_groups", "$.dsl.groups", "Every object needs a placement group")
        result = cls(tuple(groups))
        reservations = result.group_requirements(bounds) if dynamic else [result.reservations(bounds)[1]]
        for allocated in reservations:
            for region, size in allocated.items():
                if size > system.regions[region].size_bytes:
                    raise CandidateError("capacity_exceeded", "$.dsl.groups", "Region allocation exceeds its capacity",
                                         region=region, target=system.regions[region].target,
                                         required_bytes=size, capacity_bytes=system.regions[region].size_bytes)
        return result

    def to_dict(self):
        return {"version": 3, "objects": {n: item.to_dict() for group, _, _ in self.groups for n, item in group.objects},
                "groups": [{"composition": group.to_dict()["composition"],
                            "placement": {"regions": list(regions), "stripe_bytes": stripe}}
                           for group, regions, stripe in self.groups]}

    def reservations(self, bounds):
        used, bases = {}, []
        for group, regions, stripe in self.groups:
            total = (group.allocated_bytes(bounds) + stripe - 1) // stripe
            base = {}
            for i, region in enumerate(regions):
                count = max(0, (total + len(regions) - 1 - i) // len(regions))
                base[region] = ((used.get(region, 0) + stripe - 1) // stripe) * stripe
                if count:
                    used[region] = base[region] + count * stripe
            bases.append(base)
        return bases, used

    def allocated_bytes(self, bounds):
        return sum(self.reservations(bounds)[1].values())

    def group_requirements(self, bounds):
        """Standalone group footprints, before an allocator chooses their bases."""
        return [PlacedLayout((group,)).reservations(bounds)[1] for group in self.groups]

    def offsets(self, bounds, bases=None):
        """Each element maps to (region ID, byte offset relative to that region)."""
        result = {}
        if bases is None:
            bases, _ = self.reservations(bounds)
        for (group, regions, stripe), base in zip(self.groups, bases):
            for name, offsets in group.offsets(bounds).items():
                placed = []
                for offset in offsets:
                    chunk, within = divmod(offset, stripe)
                    region = regions[chunk % len(regions)]
                    placed.append((region, base[region] + (chunk // len(regions)) * stripe + within))
                result[name] = placed
        return result
