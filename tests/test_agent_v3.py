from itertools import permutations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hbm_toy import agent_v3 as v3
from hbm_toy.experiment import read, save
from hbm_toy.model import Request
from hbm_toy.rule_study import permutation_policy


class FactorialProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / 'study'
        # Test-only traces: never evaluate the actual held-back experiment tasks.
        fixture = {name: [Request(i, (i % 2) * 64, i // 8) for i in range(192)] for name in v3.TASKS}
        with patch.object(v3, 'tasks', return_value=fixture), patch.object(v3, 'simulate', side_effect=AssertionError('prepare must not simulate')):
            self.assertEqual(v3.prepare(self.folder)['backend_calls'], 0)
        protocol = read(self.folder / 'protocol.json')
        protocol['budget'] = 3
        save(self.folder / 'protocol.json', protocol)
        orders = [o for o in permutations(range(6)) if o not in (tuple(range(6)), tuple(reversed(range(6))))][:18]
        batch = [{'hypothesis': 'test', 'expected': 'test', 'dsl': permutation_policy(o, v3.CFG).to_dict()} for o in orders]
        for task in v3.TASKS:
            save(self.folder / 'one_shot' / (task + '.json'), batch)

    def test_same_scores_different_feedback_and_no_extra_execution(self):
        actual = v3.simulate
        with patch.object(v3, 'simulate', wraps=actual) as mock:
            v3.start(self.folder)
            self.assertEqual(mock.call_count, 24)
        base = self.folder / 'sessions'
        summary = read(base / 'old_summary' / v3.TASKS[0] / 'feedback.json')
        rich = read(base / 'old_phase' / v3.TASKS[0] / 'feedback.json')
        for left, right in zip(summary['history'], rich['history']):
            self.assertEqual(left['metrics'], right['metrics'])
            self.assertNotIn('phase_feedback', left)
            self.assertIn('phase_feedback', right)
        with patch.object(v3, 'exact_permutations', side_effect=AssertionError('no early oracle')):
            with self.assertRaisesRegex(ValueError, 'Finish ALL'):
                v3.compare(self.folder)

    def test_cache_invalid_and_failed_backend_budget(self):
        v3.start(self.folder)
        session = self.folder / 'sessions' / 'new_phase' / v3.TASKS[0]
        seed = read(session / 'state.json')['history'][0]['proposal']
        self.assertEqual(v3.submit(session, seed)['backend_calls'], 2)
        bad = {'hypothesis': 'test', 'expected': 'test', 'dsl': {'version': 0, 'operations': [{'op': 'xor_bit', 'src': 0, 'dst': 1}]}}
        self.assertEqual(v3.submit(session, bad)['status'], 'invalid')
        proposal = {'hypothesis': 'test', 'expected': 'test', 'dsl': permutation_policy([1, 0, 2, 3, 4, 5], v3.CFG).to_dict()}
        with patch.object(v3, 'simulate', side_effect=RuntimeError('injected failure')):
            entry = v3.submit(session, proposal)
        self.assertEqual((entry['status'], entry['backend_calls']), ('backend_error', 3))
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            v3.submit(session, proposal)
        self.assertEqual(v3.finish(session, 'budget spent')['backend_calls'], 3)

    def test_commitments_guard_prompt_and_one_shot_changes(self):
        v3.start(self.folder)
        path = self.folder / 'protocol.json'
        original = read(path)
        changed = read(path)
        changed['prompts']['old'] = 'changed'
        save(path, changed)
        with self.assertRaisesRegex(ValueError, 'protocol changed'):
            v3.check(self.folder)
        save(path, original)
        path = self.folder / 'one_shot' / (v3.TASKS[0] + '.json')
        batch = read(path)
        batch[0]['expected'] = 'changed'
        save(path, batch)
        with self.assertRaisesRegex(ValueError, 'one-shot proposals changed'):
            v3.check(self.folder)


if __name__ == '__main__':
    unittest.main()
