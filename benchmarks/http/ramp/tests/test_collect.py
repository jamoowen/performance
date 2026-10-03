import unittest
from unittest.mock import Mock, patch

from benchmarks.http.ramp.measure.collector import (
    GeneratorCollector,
    RemoteCollector,
    resource_delta,
    validated_ssh_host,
    weighted_window,
)
from benchmarks.http.ramp.measure.collector_remote import SAFE_ID, cgroup_for, identity_matches


def sample(monotonic, usage, throttled=0, current=1000, inactive=100, oom=0, periods=0):
    return {
        "monotonic_ns": monotonic,
        "realtime_ns": monotonic + 1_700_000_000_000_000_000,
        "cpu_stat": {
            "usage_usec": usage,
            "throttled_usec": throttled,
            "nr_periods": periods,
            "nr_throttled": periods // 2,
        },
        "memory_current": current,
        "inactive_file": inactive,
        "memory_events": {"oom_kill": oom},
        "cpu_pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0",
        "memory_pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0",
        "node_cpu": {"user": monotonic, "system": monotonic, "idle": monotonic, "iowait": 0},
    }


def collector():
    return RemoteCollector(
        "jamoowen@192.168.1.122",
        "my-api",
        "http-bun-123",
        "http-bun",
        "db22d766-97c7-4e5c-ae16-ecf736230b4b",
        "a" * 64,
        5,
    )


class CollectorTests(unittest.TestCase):
    def test_delta_includes_backward_interval_and_working_set(self):
        result = resource_delta(
            sample(0, 0), sample(1_000_000_000, 250000, 50000, 1200, 300, periods=10)
        )
        self.assertEqual(result["cpuMillicores"], 250)
        self.assertEqual(result["workingSetBytes"], 900)
        self.assertEqual(result["throttledSeconds"], 0.05)
        self.assertEqual(result["intervalStartSeconds"], 0)
        self.assertEqual(result["intervalEndSeconds"], 1)
        self.assertEqual(result["realtimeStartSeconds"], 1_700_000_000)
        self.assertEqual(result["cfsPeriodRatio"], 0.5)

    def test_counter_reset_and_clock_rejected(self):
        with self.assertRaises(ValueError):
            resource_delta(sample(2, 1), sample(1, 2))
        with self.assertRaises(ValueError):
            resource_delta(sample(1, 2), sample(2, 1))

    def test_weighted_window_clips_backward_intervals(self):
        samples = [
            {
                "intervalStartSeconds": 0,
                "intervalEndSeconds": 1,
                "cpuMillicores": 100,
                "workingSetBytes": 10,
                "memoryCurrentBytes": 20,
                "throttledSeconds": 0.1,
                "cfsPeriods": 10,
                "cfsThrottledPeriods": 1,
                "nodeCpuUtilization": 0.2,
                "cpuPressureTotalMicroseconds": 100,
                "memoryPressureTotalMicroseconds": 20,
            },
            {
                "intervalStartSeconds": 1,
                "intervalEndSeconds": 2,
                "cpuMillicores": 300,
                "workingSetBytes": 30,
                "memoryCurrentBytes": 40,
                "throttledSeconds": 0.2,
                "cfsPeriods": 10,
                "cfsThrottledPeriods": 3,
                "nodeCpuUtilization": 0.4,
                "cpuPressureTotalMicroseconds": 300,
                "memoryPressureTotalMicroseconds": 60,
            },
        ]
        result = weighted_window(samples, 0.5, 1.5)
        self.assertEqual(result["coverage"], 1)
        self.assertEqual(result["cpuMillicores"], 200)
        self.assertAlmostEqual(result["throttledSeconds"], 0.15)
        self.assertEqual(result["cfsPeriodRatio"], 0.2)
        self.assertAlmostEqual(result["nodeCpuUtilization"], 0.3)
        self.assertEqual(result["cpuPressureTotalMicroseconds"], 200)
        self.assertEqual(result["memoryPressureTotalMicroseconds"], 40)

    def test_weighted_window_reports_coverage_hole(self):
        result = weighted_window(
            [{"intervalStartSeconds": 0, "intervalEndSeconds": 1, "cpuMillicores": 100}], 0, 3
        )
        self.assertAlmostEqual(result["coverage"], 1 / 3)
        self.assertEqual(result["error"], "sample_gap")

    def test_invalid_cgroup_id_and_ssh_host_rejected_before_start(self):
        with self.assertRaises(ValueError):
            cgroup_for("../not-an-id")
        with self.assertRaises(ValueError):
            validated_ssh_host("host; rm -rf /")
        with self.assertRaises(ValueError):
            collector().__class__("bad host", "ns", "pod", "container", "x", "a" * 64, 5).start()
        self.assertTrue(SAFE_ID.fullmatch("a" * 64))

    def test_identity_restart_change_is_drift(self):
        args = Mock(uid="db22d766-97c7-4e5c-ae16-ecf736230b4b", container_id="a" * 64)
        identity = {
            "uid": args.uid,
            "container_id": args.container_id,
            "restarts": 2,
            "deleting": False,
            "ready": True,
        }
        self.assertTrue(identity_matches(identity, args, 2))
        self.assertFalse(identity_matches({**identity, "restarts": 3}, args, 2))

    @patch("benchmarks.http.ramp.measure.collector.subprocess.run")
    def test_clean_stop_signals_only_known_positive_remote_pid(self, remote_run):
        instance = collector()
        instance._process = Mock()
        instance._process.poll.return_value = None
        instance._remote_pid = 17
        instance._metadata_event.set()
        remote_run.return_value = Mock(returncode=0)
        instance.stop()
        self.assertEqual(remote_run.call_args.args[0][0], "ssh")
        self.assertIn("kill -TERM 17", remote_run.call_args.args[0][-1])
        self.assertNotIn("clean_stop_failed", instance.errors)

    def test_generator_requires_sustained_host_pressure_not_one_hot_core(self):
        samples = [
            {
                "seconds": index * 1.01,
                "monotonicSeconds": index * 1.01,
                "hostCpuPercent": [91, 91],
                "availableMemoryBytes": 2 * 1024**3,
                "swapUsedBytes": 0,
            }
            for index in range(7)
        ]
        collector = GeneratorCollector(1)
        collector.samples = samples
        collector._started_monotonic = 0
        collector._ended_monotonic = 6.06
        self.assertTrue(collector.result()["headroomFlag"])
        collector.samples = [{**item, "hostCpuPercent": [100, 0]} for item in samples]
        self.assertFalse(collector.result()["headroomFlag"])

    @patch(
        "benchmarks.http.ramp.measure.collector.subprocess.run",
        side_effect=__import__("subprocess").TimeoutExpired("ssh", 10),
    )
    def test_clean_stop_timeout_is_bounded(self, _remote_run):
        instance = collector()
        instance._process = Mock()
        instance._process.poll.return_value = None
        instance._remote_pid = 17
        instance._metadata_event.set()
        instance.stop()
        self.assertIn("clean_stop_timeout", instance.errors)


if __name__ == "__main__":
    unittest.main()
