import unittest

from agent_design_search.exploration.dram_mapping_candidates import activity_policy, joint_policy
from experiments.legacy_cases import simulate_mapping as simulate
from experiments.legacy_cases import Config, Request, workload


SMALL_CONFIG = Config(channel_bits=2, bank_bits=1, column_bits=1, row_bits=2)


def correlated_requests(count=256):
    # Bits 0 and 1 always match: marginal activity overstates channel diversity.
    blocks = [(i & 1) | ((i & 1) << 1) | (((i % 32) >> 1) << 2) for i in range(count)]
    return [Request(i, block * 64, i // 8) for i, block in enumerate(blocks)]


class MappingCandidateTests(unittest.TestCase):
    def test_correlated_bits_do_not_supply_independent_channel_capacity(self):
        cfg = SMALL_CONFIG
        requests = correlated_requests()
        packed = simulate(requests, activity_policy(requests, cfg, True), cfg)
        joint = simulate(requests, joint_policy(requests, cfg, True), cfg)
        self.assertEqual(sum(n > 0 for n in packed["channel_requests"]), 2)
        self.assertEqual(joint["channel_requests"], [64, 64, 64, 64])

    def test_default_width_stays_within_operation_budget(self):
        cfg = Config()
        policy = joint_policy(workload("kv_interleaved", cfg), cfg, True)
        self.assertLessEqual(len(policy.operations), cfg.max_operations)

if __name__ == "__main__":
    unittest.main()
