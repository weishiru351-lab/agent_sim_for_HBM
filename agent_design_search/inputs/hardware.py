"""Storage geometry in counts; columns are transfer-sized slots in each row."""

from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class Hardware:
    channels: int = 4
    banks_per_channel: int = 4
    rows: int = 256
    columns: int = 16
    granule_bytes: int = 64

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < 1 or value & (value - 1):
                raise ValueError(f"hardware.{name} must be a positive power of two for the current bit mapper")
        if self.block_bits > 24 or self.granule_bytes > 4096:
            raise ValueError("Current executor supports at most 24 block bits and 4096 bytes per transfer")

    @classmethod
    def parse(cls, obj):
        if not isinstance(obj, dict) or set(obj) != {"channels", "banks_per_channel", "rows", "columns", "granule_bytes"}:
            raise ValueError("Hardware needs channels, banks_per_channel, rows, columns, granule_bytes")
        return cls(**obj)

    @property
    def channel_bits(self):
        return self.channels.bit_length() - 1

    @property
    def bank_bits(self):
        return self.banks_per_channel.bit_length() - 1

    @property
    def row_bits(self):
        return self.rows.bit_length() - 1

    @property
    def column_bits(self):
        return self.columns.bit_length() - 1

    @property
    def block_bits(self):
        return self.channel_bits + self.bank_bits + self.row_bits + self.column_bits

    @property
    def blocks(self):
        return 1 << self.block_bits

    @property
    def capacity_bytes(self):
        return self.blocks * self.granule_bytes

    def to_dict(self):
        return asdict(self)


def _named(obj, label):
    if (not isinstance(obj, dict) or not 1 <= len(obj) <= 64
            or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", k) for k in obj)):
        raise ValueError(f"{label} needs 1..64 uniquely named entries")
    return obj


def _fields(obj, fields, label):
    if not isinstance(obj, dict) or set(obj) != set(fields.split()):
        raise ValueError(f"{label} requires exactly: {fields}")


def _integer(value, minimum, label):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")


@dataclass(frozen=True)
class Target:
    module: str
    geometry: Hardware
    clock_period_ps: int
    mapping_group: str
    links: tuple

    @property
    def medium(self):
        return "dram"

    @property
    def capacity_bytes(self):
        return self.geometry.capacity_bytes


@dataclass(frozen=True)
class NANDGeometry:
    channels: int
    dies_per_channel: int
    planes_per_die: int
    blocks_per_plane: int
    pages_per_block: int
    page_bytes: int

    def __post_init__(self):
        for name, value in asdict(self).items():
            _integer(value, 1, "nand.geometry." + name)
        if self.pages > 1 << 24 or self.page_bytes > 65536:
            raise ValueError("NAND model supports at most 2^24 physical pages and 65536 bytes/page")

    @classmethod
    def parse(cls, obj):
        _fields(obj, "channels dies_per_channel planes_per_die blocks_per_plane pages_per_block page_bytes", "NAND geometry")
        return cls(**obj)

    @property
    def blocks(self):
        return self.channels * self.dies_per_channel * self.planes_per_die * self.blocks_per_plane

    @property
    def pages(self):
        return self.blocks * self.pages_per_block

    @property
    def capacity_bytes(self):
        return self.pages * self.page_bytes

    @property
    def granule_bytes(self):
        return self.page_bytes

    def decode(self, physical_page):
        _integer(physical_page, 0, "physical_page")
        if physical_page >= self.pages:
            raise ValueError("Physical NAND page outside geometry")
        coords = []
        for count in (self.pages_per_block, self.blocks_per_plane, self.planes_per_die, self.dies_per_channel):
            physical_page, coordinate = divmod(physical_page, count)
            coords.append(coordinate)
        return (physical_page, *reversed(coords))  # channel, die, plane, block, page

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class NANDTarget:
    module: str
    geometry: NANDGeometry
    logical_pages: int
    management_bytes: int
    links: tuple

    def __post_init__(self):
        _integer(self.logical_pages, 1, "NAND logical_pages")
        _integer(self.management_bytes, 1, "NAND management_bytes")
        if self.logical_pages >= self.geometry.pages:
            raise ValueError("NAND requires at least one spare physical page beyond logical_pages")
        if self.management_bytes < self.metadata_bytes + self.geometry.page_bytes:
            raise ValueError("NAND management_bytes must cover metadata and one page scratch buffer")

    @property
    def medium(self):
        return "nand"

    @property
    def capacity_bytes(self):
        return self.logical_pages * self.geometry.page_bytes

    @property
    def metadata_bytes(self):
        # Explicit abstract accounting, not Python object size or a real firmware ABI.
        return self.logical_pages * 8 + self.geometry.pages + self.geometry.blocks * 4


@dataclass(frozen=True)
class BlockGeometry:
    logical_blocks: int
    block_bytes: int

    def __post_init__(self):
        _integer(self.logical_blocks, 1, "SSD logical_blocks")
        _integer(self.block_bytes, 1, "SSD block_bytes")
        if self.logical_blocks > 1 << 24 or self.block_bytes > 65536:
            raise ValueError("Block model supports at most 2^24 logical blocks and 65536 bytes/block")

    @classmethod
    def parse(cls, obj):
        _fields(obj, "logical_blocks block_bytes", "SSD host geometry")
        return cls(**obj)

    @property
    def granule_bytes(self):
        return self.block_bytes

    @property
    def capacity_bytes(self):
        return self.logical_blocks * self.block_bytes

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SSDTarget:
    module: str
    geometry: BlockGeometry
    max_transfer_blocks: int
    host_buffer_bytes: int
    links: tuple
    nand: NANDTarget | None = None
    controller_access: str = "closed"

    def __post_init__(self):
        _integer(self.max_transfer_blocks, 1, "SSD max_transfer_blocks")
        _integer(self.host_buffer_bytes, 1, "SSD host_buffer_bytes")
        if self.max_transfer_blocks > self.geometry.logical_blocks:
            raise ValueError("SSD transfer limit exceeds logical capacity")
        if self.host_buffer_bytes < self.geometry.block_bytes:
            raise ValueError("SSD host_buffer_bytes must cover one logical block for fixed RMW")
        if self.controller_access not in ("open", "closed") or self.controller_access == "open" and self.nand is None:
            raise ValueError("Only a detailed SSD may expose its controller")
        if self.nand and self.capacity_bytes > self.nand.capacity_bytes:
            raise ValueError("SSD host capacity exceeds internal NAND logical capacity")

    @property
    def medium(self):
        return "ssd"

    @property
    def capacity_bytes(self):
        return self.geometry.capacity_bytes


@dataclass(frozen=True)
class Region:
    target: str
    base_address: int
    size_bytes: int


@dataclass(frozen=True)
class Link:
    bytes_per_beat: int
    beat_ps: int
    latency_ps: int

    def duration(self, size):
        # Exclusive, bidirectional link: latency also occupies the link.
        return self.latency_ps + ((size + self.bytes_per_beat - 1) // self.bytes_per_beat) * self.beat_ps


class MemorySystem:
    """Fixed physical resources. Pools reference disjoint regions, never add capacity."""

    def __init__(self, obj):
        _fields(obj, "version modules targets regions pools links", "memory system")
        if type(obj["version"]) is not int or obj["version"] != 1:
            raise ValueError("Memory system requires version=1")
        modules = obj["modules"]
        if (not isinstance(modules, list) or any(not isinstance(x, str) for x in modules)
                or len(set(modules)) != len(modules)):
            raise ValueError("modules must list distinct identifiers")
        _named(dict.fromkeys(modules), "modules")
        self.modules = tuple(modules)
        self.links = {}
        for name, link in _named(obj["links"], "links").items():
            _fields(link, "bytes_per_beat beat_ps latency_ps", f"links.{name}")
            for field in link:
                _integer(link[field], 0 if field == "latency_ps" else 1, f"links.{name}.{field}")
            self.links[name] = Link(**link)
        self.targets, self.mapping_geometries = {}, {}
        for name, target in _named(obj["targets"], "targets").items():
            medium = target.get("medium") if isinstance(target, dict) else None
            if medium not in ("dram", "nand", "ssd"):
                raise ValueError(f"targets.{name}: medium must be dram, nand or ssd")
            extra = {"dram": "clock_period_ps mapping_group", "nand": "logical_pages management_bytes",
                     "ssd": "max_transfer_blocks host_buffer_bytes"}[medium]
            detailed = medium == "ssd" and "nand" in target
            _fields(target, "module medium geometry links " + extra + (" nand controller_access" if detailed else ""), f"targets.{name}")
            if not isinstance(target["module"], str) or target["module"] not in modules:
                raise ValueError(f"targets.{name}: unknown module")
            if medium == "dram":
                geometry = Hardware.parse(target["geometry"])
                _integer(target["clock_period_ps"], 1, f"targets.{name}.clock_period_ps")
                group = target["mapping_group"]
                if not isinstance(group, str):
                    raise ValueError(f"targets.{name}: invalid mapping_group")
                _named({group: None}, "mapping_group")
                if group in self.mapping_geometries and self.mapping_geometries[group] != geometry:
                    raise ValueError(f"Mapping group {group} requires identical DRAM geometries")
                self.mapping_geometries[group] = geometry
            elif medium == "nand":
                geometry = NANDGeometry.parse(target["geometry"])
            else:
                geometry = BlockGeometry.parse(target["geometry"])
            path = target["links"]
            if (not isinstance(path, list) or not 1 <= len(path) <= 8
                    or any(not isinstance(x, str) or x not in self.links for x in path)
                    or len(set(path)) != len(path)):
                raise ValueError(f"targets.{name}: links must be a path of 1..8 distinct declared links")
            cls = {"dram": Target, "nand": NANDTarget, "ssd": SSDTarget}[medium]
            internal = {}
            if detailed:
                _fields(target["nand"], "geometry logical_pages management_bytes", f"targets.{name}.nand")
                n = target["nand"]
                internal = {"nand": NANDTarget(target["module"], NANDGeometry.parse(n["geometry"]),
                                               n["logical_pages"], n["management_bytes"], ()),
                            "controller_access": target["controller_access"]}
            self.targets[name] = cls(target["module"], geometry, *(target[k] for k in extra.split()), tuple(path), **internal)
        self.has_nand = any(t.medium == "nand" for t in self.targets.values())
        self.has_ssd = any(t.medium == "ssd" for t in self.targets.values())
        self.has_ssd_nand = any(t.medium == "ssd" and t.nand for t in self.targets.values())
        self.nand_targets = {n: t if t.medium == "nand" else t.nand for n, t in self.targets.items()
                             if t.medium == "nand" or t.medium == "ssd" and t.nand}
        self.nand_design_targets = {n: t for n, t in self.nand_targets.items()
                                    if self.targets[n].medium == "nand" or self.targets[n].controller_access == "open"}
        self.regions = {}
        for name, region in _named(obj["regions"], "regions").items():
            _fields(region, "target base_address size_bytes", f"regions.{name}")
            if not isinstance(region["target"], str) or region["target"] not in self.targets:
                raise ValueError(f"regions.{name}: unknown target")
            geometry = self.targets[region["target"]].geometry
            _integer(region["base_address"], 0, f"regions.{name}.base_address")
            _integer(region["size_bytes"], 1, f"regions.{name}.size_bytes")
            if (region["base_address"] % geometry.granule_bytes or region["size_bytes"] % geometry.granule_bytes
                    or region["base_address"] + region["size_bytes"] > self.targets[region["target"]].capacity_bytes):
                raise ValueError(f"regions.{name}: unaligned or outside target capacity")
            for other in self.regions.values():
                if (other.target == region["target"] and region["base_address"] < other.base_address + other.size_bytes
                        and other.base_address < region["base_address"] + region["size_bytes"]):
                    raise ValueError(f"regions.{name}: overlapping regions would duplicate physical capacity")
            self.regions[name] = Region(**region)
        self.pools = {}
        for name, regions in _named(obj["pools"], "pools").items():
            if (not isinstance(regions, list) or not regions
                    or any(not isinstance(x, str) or x not in self.regions for x in regions)
                    or len(set(regions)) != len(regions)):
                raise ValueError(f"pools.{name}: list distinct declared regions")
            self.pools[name] = tuple(regions)

    def to_dict(self):
        return {"version": 1, "modules": list(self.modules),
                "targets": {n: {"module": t.module, "medium": t.medium, "geometry": t.geometry.to_dict(),
                                 **({"clock_period_ps": t.clock_period_ps, "mapping_group": t.mapping_group} if t.medium == "dram"
                                    else {"logical_pages": t.logical_pages, "management_bytes": t.management_bytes} if t.medium == "nand"
                                    else {"max_transfer_blocks": t.max_transfer_blocks, "host_buffer_bytes": t.host_buffer_bytes}),
                                 **({"nand": {"geometry": t.nand.geometry.to_dict(), "logical_pages": t.nand.logical_pages,
                                              "management_bytes": t.nand.management_bytes},
                                     "controller_access": t.controller_access} if t.medium == "ssd" and t.nand else {}),
                                 "links": list(t.links)} for n, t in self.targets.items()},
                "regions": {n: asdict(r) for n, r in self.regions.items()},
                "pools": {n: list(rs) for n, rs in self.pools.items()},
                "links": {n: asdict(link) for n, link in self.links.items()}}
