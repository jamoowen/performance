import unittest

from benchmarks.http.ramp.measure.normalize import percentile
from benchmarks.http.ramp.measure.schedule import classify, production_schedule, schedule_hash


class ScheduleTests(unittest.TestCase):
    def test_production_integral_and_boundaries(self):
        stages = production_schedule()
        self.assertEqual(sum(s.expected_arrivals for s in stages), 720000)
        self.assertEqual(classify(0, stages), (300, "settling"))
        self.assertEqual(classify(20, stages), (300, "stable"))
        self.assertEqual(classify(180, stages), (600, "transition"))
        self.assertEqual(len(schedule_hash(stages)), 64)

    def test_exact_percentile(self):
        self.assertEqual(percentile([1, 2, 100], 95), 90.19999999999999)
