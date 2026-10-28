"""DRAM-specific heuristic seeds, random candidates and neighborhood operations.

The shared algorithms in exploration/ decide which candidate to evaluate next.
Experiments decide which of these tools serve as comparison baselines.
"""

from collections import Counter

from agent_design_search.spaces.dram_dsl import Policy, permutation_policy
from agent_design_search.inputs.workload import features


def row_xor(cfg, max_operations=8):
    if not cfg.row_bits:
        return Policy()
    row_start = cfg.channel_bits + cfg.bank_bits + cfg.column_bits
    return Policy(tuple(("xor_bit", dst, row_start + dst % cfg.row_bits)
                        for dst in range(min(cfg.channel_bits + cfg.bank_bits, max_operations))))


def activity_policy(requests, cfg, pack_columns=False, max_operations=8):
    """A transparent hand-written bit-transition heuristic, NOT an AI agent or DReAM reproduction."""
    rates = features(requests, cfg)["bit_transition_rates"]
    selected = sorted(range(cfg.block_bits), key=lambda bit: (-rates[bit], bit))
    labels, ops = list(range(cfg.block_bits)), []
    targets = cfg.channel_bits + cfg.bank_bits + (cfg.column_bits if pack_columns else 0)
    for dst, original_bit in enumerate(selected[:targets]):
        src = labels.index(original_bit)
        if src != dst and len(ops) < max_operations:
            ops.append(("swap_bits", dst, src))
            labels[dst], labels[src] = labels[src], labels[dst]
    return Policy(tuple(ops))


def random_op(rng, cfg):
    a, b = rng.sample(range(cfg.block_bits), 2)
    return rng.choice(("xor_bit", "swap_bits")), a, b


def random_policy(rng, cfg, max_operations=8):
    if cfg.block_bits < 2 or max_operations < 1:
        return Policy()
    return Policy(tuple(random_op(rng, cfg) for _ in range(rng.randint(1, max_operations))))


def mutate(policy, rng, cfg, max_operations=8):
    if cfg.block_bits < 2 or max_operations < 1:
        return Policy()
    ops = list(policy.operations)
    choices = ["replace", "delete"] if ops else []
    if len(ops) < max_operations:
        choices.append("insert")
    action = rng.choice(choices)
    if action == "insert":
        ops.insert(rng.randrange(len(ops) + 1), random_op(rng, cfg))
    elif action == "delete":
        ops.pop(rng.randrange(len(ops)))
    else:
        ops[rng.randrange(len(ops))] = random_op(rng, cfg)
    return Policy(tuple(ops))


def selector(block, bits):
    return sum(((block >> bit) & 1) << dst for dst, bit in enumerate(bits))


def joint_policy(requests, cfg, conditional_columns, max_operations=8):
    """Greedy joint occupancy, then global or within-bank column activity.

    No backend evaluations: minimize the sum of squared joint bucket counts for
    each added channel/bank bit. Break ties by global transition count, then bit
    index. Column bits absorb frequent changes; the conditional version counts
    changes between consecutive accesses to the SAME selected channel and bank.
    This is a hand-written rule hypothesis, not a learned or proven-optimal rule.
    """
    if not requests:
        raise ValueError("requests must not be empty")
    ordered = sorted(requests, key=lambda r: (r.arrival, r.request_id))
    blocks = [r.address // cfg.granule_bytes for r in ordered]
    transitions = [sum(((a ^ b) >> bit) & 1 for a, b in zip(blocks, blocks[1:]))
                   for bit in range(cfg.block_bits)]
    remaining, selected = set(range(cfg.block_bits)), []
    for _ in range(cfg.channel_bits + cfg.bank_bits):
        def score(bit):
            counts = Counter(selector(block, selected + [bit]) for block in blocks)
            return sum(n * n for n in counts.values()), -transitions[bit], bit
        bit = min(remaining, key=score)
        selected.append(bit)
        remaining.remove(bit)
    column_changes = transitions
    if conditional_columns:
        previous, differences = {}, []
        for block in blocks:
            key = selector(block, selected)
            if key in previous:
                differences.append(block ^ previous[key])
            previous[key] = block
        column_changes = [sum((delta >> bit) & 1 for delta in differences)
                          for bit in range(cfg.block_bits)]
    columns = sorted(remaining, key=lambda b: (-column_changes[b], b))[:cfg.column_bits]
    remaining.difference_update(columns)
    # Leave the remaining row bits in their resulting order: sorting them would
    # waste DSL operations without changing row equality in this backend.
    order = list(range(cfg.block_bits))
    for dst, bit in enumerate(selected + columns):
        src = order.index(bit)
        order[dst], order[src] = order[src], order[dst]
    return permutation_policy(order, cfg, max_operations)
