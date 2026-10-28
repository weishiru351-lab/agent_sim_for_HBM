"""DRAM/HBM address-mapping DSL: primitives, compilation and mathematical validity."""

from dataclasses import dataclass
import random

from agent_design_search.contracts import CandidateError


@dataclass(frozen=True)
class Policy:
    # (op, first bit, second bit); operations read the previous operation's result.
    operations: tuple = ()

    @classmethod
    def parse(cls, obj, cfg, max_operations=8):
        if not isinstance(obj, dict) or set(obj) != {"version", "operations"}:
            raise CandidateError("invalid_fields", "$.dsl", "DSL must contain exactly version and operations")
        if type(obj["version"]) is not int or obj["version"] != 0:
            raise CandidateError("invalid_version", "$.dsl.version", "Only DSL version 0 is supported")
        ops = obj["operations"]
        if not isinstance(ops, list) or len(ops) > max_operations:
            raise CandidateError("operation_limit", "$.dsl.operations", "operations must be a list within the configured limit",
                                 maximum=max_operations)
        result = []
        for index, op in enumerate(ops):
            path = f"$.dsl.operations[{index}]"
            if not isinstance(op, dict):
                raise CandidateError("invalid_operation", path, "Each operation must be an object")
            kind = op.get("op")
            if not isinstance(kind, str):
                raise CandidateError("invalid_operation", path + ".op", "Operation name must be a string")
            fields = {"xor_bit": ("dst", "src"), "swap_bits": ("a", "b")}.get(kind)
            if fields is None or set(op) != {"op", *fields}:
                raise CandidateError("invalid_operation", path, "Unknown operation or unexpected fields")
            a, b = (op[field] for field in fields)
            if any(type(bit) is not int or not 0 <= bit < cfg.block_bits for bit in (a, b)) or a == b:
                raise CandidateError("invalid_bits", path, "Bits must be distinct integers within the block address",
                                     actual=[a, b], minimum=0, maximum=cfg.block_bits - 1)
            result.append((kind, a, b))
        return cls(tuple(result))

    def to_dict(self):
        return {"version": 0, "operations": [
            {"op": kind, **dict(zip(("dst", "src") if kind == "xor_bit" else ("a", "b"), (a, b)))}
            for kind, a, b in self.operations
        ]}

    def apply(self, block):
        for kind, a, b in self.operations:
            if kind == "xor_bit":
                block ^= ((block >> b) & 1) << a
            else:
                change = ((block >> a) ^ (block >> b)) & 1
                block ^= (change << a) | (change << b)
        return block

    def inverse(self, block):
        return Policy(tuple(reversed(self.operations))).apply(block)

    def masks(self, cfg):
        """Independent linear compilation; also canonicalizes equivalent DSLs."""
        masks = [1 << i for i in range(cfg.block_bits)]
        for kind, a, b in self.operations:
            if kind == "xor_bit":
                masks[a] ^= masks[b]
            else:
                masks[a], masks[b] = masks[b], masks[a]
        return tuple(masks)

    def decode(self, address, cfg):
        if type(address) is not int or not 0 <= address < cfg.blocks * cfg.granule_bytes:
            raise ValueError("Address out of range")
        block, offset = divmod(address, cfg.granule_bytes)
        mapped = self.apply(block)
        channel = mapped & ((1 << cfg.channel_bits) - 1)
        mapped >>= cfg.channel_bits
        bank = mapped & ((1 << cfg.bank_bits) - 1)
        mapped >>= cfg.bank_bits
        column = mapped & ((1 << cfg.column_bits) - 1)
        row = mapped >> cfg.column_bits
        return channel, bank, row, column, offset


def permutation_policy(order, cfg, max_operations=8):
    """Compile output-bit -> input-bit order into at most width-1 swaps."""
    if sorted(order) != list(range(cfg.block_bits)):
        raise ValueError("order must be a permutation of all block bits")
    labels, ops = list(range(cfg.block_bits)), []
    for dst, bit in enumerate(order):
        src = labels.index(bit)
        if src != dst:
            ops.append(("swap_bits", dst, src))
            labels[dst], labels[src] = labels[src], labels[dst]
    return Policy.parse(Policy(tuple(ops)).to_dict(), cfg, max_operations)


def verify_mapping(policy, cfg):
    """Full-domain rank argument for this linear DSL, plus sampled implementation checks."""
    Policy.parse(policy.to_dict(), cfg, len(policy.operations))
    basis = {}
    for vector in policy.masks(cfg):
        while vector:
            pivot = vector.bit_length() - 1
            if pivot in basis:
                vector ^= basis[pivot]
            else:
                basis[pivot] = vector
                break
    rng = random.Random(0)
    points = {0, cfg.blocks - 1, *(1 << i for i in range(cfg.block_bits))}
    points.update(range(cfg.blocks) if cfg.blocks <= 1024 else (rng.randrange(cfg.blocks) for _ in range(1024)))
    masks = policy.masks(cfg)
    for block in points:
        mapped = sum(((block & mask).bit_count() & 1) << i for i, mask in enumerate(masks))
        if mapped != policy.apply(block) or policy.inverse(mapped) != block:
            return {"status": "fail", "counterexample_block": block}
    return {"status": "pass" if len(basis) == cfg.block_bits else "fail",
            "method": "GF(2) full-rank check plus interpreter/compiler and inverse checks",
            "rank": len(basis), "block_bits": cfg.block_bits,
            "implementation_samples": len(points), "rtl_status": "not_run"}
