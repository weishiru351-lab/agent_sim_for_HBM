from itertools import permutations
import unittest

from hbm_toy.experiment import activity_policy
from hbm_toy.model import Config, simulate, workload
from hbm_toy.rule_study import (SMALL_CONFIG, joint_policy, make_trace,
                                permutation_policy, permutation_search)


class RuleStudyTests(unittest.TestCase):
    def test_exhaustive_space_is_720_distinct_reversible_permutations(self):
        cfg = SMALL_CONFIG
        matrices = set()
        for order in permutations(range(6)):
            policy = permutation_policy(order, cfg)
            matrices.add(policy.masks(cfg))
            for block in range(64):
                expected = int("".join(str((block >> bit) & 1) for bit in reversed(order)), 2)
                self.assertEqual(policy.apply(block), expected)
                self.assertEqual(policy.inverse(expected), block)
        self.assertEqual(len(matrices), 720)

    def test_correlated_bits_do_not_supply_independent_channel_capacity(self):
        cfg = SMALL_CONFIG
        requests = make_trace({"family": "duplicate", "order": "ordered", "seed": 0})
        packed = simulate(requests, activity_policy(requests, cfg, True), cfg)
        joint = simulate(requests, joint_policy(requests, cfg, True), cfg)
        self.assertEqual(sum(n > 0 for n in packed["channel_requests"]), 2)
        self.assertEqual(joint["channel_requests"], [64, 64, 64, 64])

    def test_default_width_stays_within_operation_budget(self):
        cfg = Config()
        policy = joint_policy(workload("kv_interleaved", cfg), cfg, True)
        self.assertLessEqual(len(policy.operations), cfg.max_operations)

    def test_search_counts_unique_evaluations_and_replays(self):
        requests = make_trace({"family": "duplicate", "order": "ordered", "seed": 0}, count=32)
        for method in ("random", "local"):
            first = permutation_search(requests, SMALL_CONFIG, method, 7, budget=8)
            second = permutation_search(requests, SMALL_CONFIG, method, 7, budget=8)
            self.assertEqual(first["backend_calls"], 8)
            self.assertEqual(len({tuple(e["order"]) for e in first["history"]}), 8)
            self.assertEqual(first["history"], second["history"])


if __name__ == "__main__":
    unittest.main()
