"""Bridge contract tests. Synthetic callbacks below are not simulator results."""

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from hbm_toy import ramulator_backend as bridge
from hbm_toy.experiment import save, read
from hbm_toy.model import Policy, Request


class RamulatorBridgeTests(unittest.TestCase):
    def test_hand_calculated_coordinates_and_single_dsl_application(self):
        cfg = bridge.DEFAULT_CONFIG
        requests = [Request(7, 64, 3), Request(9, 4032, 4)]
        tx = bridge.compile_transactions(requests, Policy(), cfg)
        self.assertEqual([t['addr_vec'] for t in tx], [
            [1, 0, 0, 0, 0, 0, 0], [1, 0, 0, 0, 0, 0, 1],
            [3, 0, 0, 0, 1, 3, 2], [3, 0, 0, 0, 1, 3, 3]])
        self.assertEqual([t['physical_address'] for t in tx[:2]], [32, 160])
        policy = Policy((('swap_bits', 0, 4), ('xor_bit', 1, 4)))
        # Block 1 -> block 16 -> block 18: channel 2, bank 0, row 1, column 0.
        tx = bridge.compile_transactions(requests[:1], policy, cfg)
        self.assertEqual(tx[0]['addr_vec'], [2, 0, 0, 0, 0, 1, 0])
        self.assertEqual((tx[0]['request_id'], tx[0]['arrival']), (7, 3))

    def test_no_aliases_and_roundtrip_over_full_small_domain(self):
        cfg = replace(bridge.DEFAULT_CONFIG, bank_bits=5)
        requests = [Request(i, i * 64, i // 8) for i in range(cfg.blocks)]
        policy = Policy((('swap_bits', 0, 8), ('xor_bit', 2, 7)))
        txs = bridge.compile_transactions(requests, policy, cfg)
        self.assertEqual(len({t['physical_address'] for t in txs}), cfg.blocks * 2)
        self.assertEqual(sum(t['size_bytes'] for t in txs), cfg.blocks * 64)
        self.assertEqual({t['addr_vec'][1] for t in txs}, {0, 1})
        for tx in txs:
            self.assertEqual(bridge.decode_native_address(tx['physical_address'], 4), tx['addr_vec'])

    def test_reject_unsupported_requests_and_geometry(self):
        cfg = bridge.DEFAULT_CONFIG
        for requests in ([], [Request(0, 0, 0, 'write')], [Request(0, 1, 0)]):
            with self.assertRaises(ValueError):
                bridge.compile_transactions(requests, Policy(), cfg)
        for key, value in [('channel_bits', 4), ('bank_bits', 6), ('column_bits', 5), ('row_bits', 15), ('granule_bytes', 32)]:
            with self.assertRaises(ValueError):
                bridge.compile_transactions([Request(0, 0, 0)], Policy(), replace(cfg, **{key: value}))

    def fixture(self):
        requests = [Request(0, 0, 0), Request(1, 64, 5)]
        bundle = {'upstream_commit': bridge.REVISION, 'requests': [asdict(r) for r in requests],
                  'transactions': bridge.compile_transactions(requests, Policy(), bridge.DEFAULT_CONFIG)}
        bundle['bundle_id'] = bridge.content_hash(bundle)
        result = {'backend': 'ramulator-2.1-hbm2-read-v1', 'upstream_commit': bridge.REVISION,
                  'bundle_id': bundle['bundle_id'], 'accepted_transactions': 4, 'completed_transactions': 4,
                  'final_tick': 30, 'tCK_ns': 1.0, 'events': []}
        for tx, finish in zip(bundle['transactions'], [10, 20, 30, 15]):
            result['events'].append({'transaction_id': tx['transaction_id'], 'request_id': tx['request_id'],
                                     'arrival': tx['arrival'], 'accepted': tx['arrival'] + 1, 'finish': finish})
        result['events'].reverse()
        return bundle, result

    def test_completion_requires_both_halves_despite_out_of_order_events(self):
        bundle, result = self.fixture()
        metrics = bridge.validate_result(bundle, result)
        self.assertEqual(metrics['completed_requests'], 2)
        self.assertEqual(metrics['completion_cycles'], 30)
        self.assertEqual(metrics['avg_latency'], 22.5)
        self.assertEqual(metrics['p95_latency'], 25)
        self.assertEqual(metrics['request_finishes'], [{'request_id': 0, 'finish': 20}, {'request_id': 1, 'finish': 30}])

    def test_bad_backend_results_do_not_become_scores(self):
        bundle, result = self.fixture()
        bad = []
        item = deepcopy(result); item['events'].pop(); bad.append(item)
        item = deepcopy(result); item['events'][0] = item['events'][1]; bad.append(item)
        item = deepcopy(result); item['events'][0]['finish'] = 0; bad.append(item)
        item = deepcopy(result); item['events'][0]['request_id'] = 99; bad.append(item)
        for key, value in [('accepted_transactions', 3), ('completed_transactions', 3), ('final_tick', 9),
                           ('bundle_id', 'changed'), ('tCK_ns', float('nan'))]:
            item = deepcopy(result); item[key] = value; bad.append(item)
        for item in bad:
            with self.assertRaises(ValueError):
                bridge.validate_result(bundle, item)

    def test_missing_binary_and_timeout_count_actual_starts(self):
        bundle, _ = self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save(root / 'bundle.json', bundle)
            result = bridge.run(root / 'bundle.json', root / 'missing', root / 'missing_run')
            self.assertEqual((result['status'], result['backend_calls']), ('not_run', 0))
            driver = root / 'fixture_driver'
            driver.write_bytes(b'test fixture, never executed')
            process = Mock()
            process.communicate.side_effect = [subprocess.TimeoutExpired('fixture', 1), (b'partial output', b'timeout log')]
            with patch.object(bridge.subprocess, 'Popen', return_value=process):
                result = bridge.run(root / 'bundle.json', driver, root / 'timeout_run', timeout=1)
            self.assertEqual((result['status'], result['backend_calls']), ('backend_error', 1))
            process.kill.assert_called_once()
            self.assertEqual((root / 'timeout_run' / 'stderr.log').read_bytes(), b'timeout log')
            self.assertNotIn('metrics', read(root / 'timeout_run' / 'result.json'))
            bundle['requests'][0]['address'] = 64
            save(root / 'bundle.json', bundle)
            with self.assertRaisesRegex(ValueError, 'bundle changed'):
                bridge.run(root / 'bundle.json', driver, root / 'changed_run')


if __name__ == '__main__':
    unittest.main()
