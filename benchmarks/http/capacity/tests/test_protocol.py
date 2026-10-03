import unittest

from benchmarks.http.capacity.protocol import (
    Step,
    initial_steps,
    next_target,
    overload_from_history,
)


class ProtocolTests(unittest.TestCase):
    def test_initial_steps_and_vus(self):
        self.assertEqual([step.target_rps for step in initial_steps()], [300, 600, 900, 1200, 1500])
        self.assertEqual(Step(300).vus, 1024)
        self.assertEqual(Step(2930).vus, 1024)
        self.assertEqual(Step(20_000).vus, 1024)

    def test_next_target_rounds_up_and_honors_ceiling(self):
        self.assertEqual(next_target(1500), 1875)
        self.assertEqual(next_target(19999, 20000), 20000)
        self.assertIsNone(next_target(20000, 20000))

    def test_overload_requires_four_consecutive_stable_buckets(self):
        bad = {
            "phase": "stable",
            "bucketSeconds": 5,
            "completed": 500,
            "httpFailures": 5,
            "validationFailures": 0,
            "dropped": 0,
        }
        rows = [dict(bad, seconds=index * 5) for index in range(4)]
        self.assertFalse(overload_from_history(rows[:3], 300)["status"])
        self.assertTrue(overload_from_history(rows, 300)["status"])

    def test_non_stable_bucket_breaks_overload_run(self):
        bad = {
            "phase": "stable",
            "bucketSeconds": 5,
            "completed": 500,
            "httpFailures": 5,
            "validationFailures": 0,
            "dropped": 0,
        }
        self.assertFalse(
            overload_from_history(
                [
                    dict(bad, seconds=0),
                    dict(bad, seconds=5),
                    {"phase": "transition", "bucketSeconds": 5, "seconds": 10},
                    dict(bad, seconds=15),
                    dict(bad, seconds=20),
                ],
                300,
            )["status"]
        )
