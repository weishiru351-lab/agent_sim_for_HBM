"""Shared request records, objective/error types and the small evaluation interface."""

from dataclasses import dataclass
import math
from typing import Protocol


@dataclass(frozen=True)
class Request:
    """A byte address with an external arrival or executor-derived ready cycle."""
    request_id: int
    address: int
    arrival: int
    kind: str = "read"
    stream: int = 0


@dataclass(frozen=True)
class DRAMRequest:
    """One routed full-granule transfer; valid_bytes is a partial-access mask.

    An empty mask means the whole granule. The timing model never applies a DSL.
    """
    request: Request
    channel: int
    bank: int
    row: int
    column: int
    valid_bytes: tuple = ()


@dataclass(frozen=True)
class PageRequest:
    """Masked access to a target-local logical NAND page; no physical binding yet."""
    request: Request
    valid_bytes: tuple


@dataclass(frozen=True)
class BlockRequest:
    """Masked host access to one SSD logical block, before read-modify-write."""
    request: Request
    valid_bytes: tuple


@dataclass(frozen=True)
class BlockAction:
    """Host block read/write or merge; never a physical NAND operation."""
    kind: str
    lba: int
    pages: tuple = ()  # Detailed SSD only: ordered internal PageCommand records.


@dataclass(frozen=True)
class BlockIO:
    """An aligned, whole-block device command, without byte masks or FTL fields."""
    request: Request
    lba: int
    blocks: int = 1


@dataclass(frozen=True)
class AllocationEvent:
    """A lifetime transition or access-triggered reservation, not a DSL."""
    source_event: int
    object_id: str
    kind: str
    generation: int
    group: int
    extents: tuple  # (region, region-relative base, reserved size), sorted by region.
    changed: bool  # Acquired/released a lease; an unreserved group has empty extents.


@dataclass(frozen=True)
class MigrationEvent:
    """A drained reserve/publish/release transition for one complete object."""
    source_event: int
    object_id: str
    generation: int
    kind: str
    source: tuple  # (region, base, size)
    destination: tuple


@dataclass(frozen=True)
class NANDAction:
    """One ordered raw operation or management step, consumed by the timing tool."""
    kind: str  # read, program, erase, lookup, merge, bind
    page: int = -1  # Erase uses the first physical page of the block.
    logical_page: int = -1  # GC bind may update a different page than the host request.
    purpose: str = "access"  # "gc" actions run behind a target-wide foreground barrier.


@dataclass(frozen=True)
class PageCommand:
    """One internal SSD page request, its actual NAND chain and returned bytes."""
    access: PageRequest
    actions: tuple
    values: tuple


class CandidateError(ValueError):
    """Machine-readable rejection; the message remains useful at the CLI."""

    def __init__(self, code, path, message, **details):
        super().__init__(message)
        self.code, self.path, self.details = code, path, details

    def to_dict(self):
        return {"code": self.code, "path": self.path, "message": str(self), "details": self.details}


@dataclass(frozen=True)
class Objective:
    metric: str
    direction: str = "minimize"

    def __post_init__(self):
        if not self.metric or self.direction not in {"minimize", "maximize"}:
            raise ValueError("Objective needs a metric and minimize/maximize direction")

    def value(self, metrics):
        value = metrics.get(self.metric)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Missing or non-finite objective: {self.metric}")
        return value

    def rank(self, result):
        value = self.value(result["metrics"])
        return value if self.direction == "minimize" else -value


class DesignTask(Protocol):
    """normalize/check never launch performance evaluations. measure represents
    one backend execution; its failures consume budget. IDs include fixed inputs
    and backend identity, canonicalizing only semantic equivalence.
    """

    definition: dict
    objective: Objective

    def feedback(self) -> dict: ...
    def normalize(self, candidate: dict) -> dict: ...
    def candidate_id(self, candidate: dict) -> str: ...
    def check(self, candidate: dict) -> dict: ...
    def measure(self, candidate: dict) -> dict: ...
