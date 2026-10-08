import unittest

from hbm_toy.agent_v2_diagnosis import phase_feedback
from hbm_toy.model import Config, Policy, Request, simulate


class PhaseFeedbackTests(unittest.TestCase):
    def test_cohort_feedback_retains_queue_and_open_row_state(self):
        cfg = Config(channel_bits=0, bank_bits=0, column_bits=1, row_bits=1)
        requests = [Request(10, 0, 0), Request(11, 0, 1), Request(12, 128, 2)]
        result = simulate(requests, Policy(), cfg, include_events=True)
        feedback = phase_feedback(requests, result['events'], cfg, 2)
        first, second = feedback['first_cohort'], feedback['second_cohort']
        self.assertEqual([e['finish'] for e in result['events']], [16, 24, 48])
        self.assertEqual(feedback['first_cohort_outstanding_at_boundary'], 2)
        self.assertEqual(first['row_hit_rate'], .5)
        self.assertEqual(second['row_conflict_rate'], 1)
        self.assertEqual(second['avg_latency'], 46)
        for cohort in (first, second):
            self.assertAlmostEqual(cohort['avg_latency'], cohort['avg_backpressure_wait']
                                   + cohort['avg_controller_wait'] + cohort['avg_issue_to_finish'])
        self.assertAlmostEqual((2 * first['avg_latency'] + second['avg_latency']) / 3,
                               result['avg_latency'])
        cold = simulate([Request(12, 128, 0)], Policy(), cfg)
        self.assertEqual(cold['completion_cycles'], 16)
        self.assertNotEqual(first['last_finish'] + cold['completion_cycles'], result['completion_cycles'])

    def test_missing_duplicate_events_and_invalid_boundary_rejected(self):
        cfg = Config(channel_bits=0, bank_bits=0, column_bits=1, row_bits=1)
        requests = [Request(4, 0, 0), Request(7, 64, 1)]
        events = simulate(requests, Policy(), cfg, include_events=True)['events']
        for invalid in (events[:1], events + events[:1]):
            with self.assertRaises(ValueError):
                phase_feedback(requests, invalid, cfg, 1)
        for boundary in (0, 2, True):
            with self.assertRaises(ValueError):
                phase_feedback(requests, events, cfg, boundary)


if __name__ == '__main__':
    unittest.main()
