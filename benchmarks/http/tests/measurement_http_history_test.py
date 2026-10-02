import gzip
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from measure.http_history import build_history, write_history


class HttpHistoryTests(unittest.TestCase):
    def write_points(self, directory, points, suffix=""):
        path = Path(directory) / f"http-metrics.json.gz{suffix}"
        with gzip.open(path, "wt") as output:
            for point in points:
                output.write(json.dumps(point) + "\n")
        return path

    def point(self, metric, time, value, tags=None):
        return {
            "type": "Point",
            "metric": metric,
            "data": {"time": time, "value": value, "tags": tags or {}},
        }

    def test_quantiles_statuses_and_failure_counters(self):
        with TemporaryDirectory() as temporary:
            path = self.write_points(
                temporary,
                [
                    self.point("http_req_duration", "1970-01-01T00:16:40Z", 0, {"status": "200"}),
                    self.point("http_req_duration", "1970-01-01T00:16:41Z", 10, {"status": "500"}),
                    self.point("http_req_duration", "1970-01-01T00:16:42Z", 20, {"status": "0"}),
                    self.point("http_req_duration", "1970-01-01T00:16:43Z", 30),
                    self.point("http_req_failed", "1970-01-01T00:16:40Z", 0),
                    self.point("http_req_failed", "1970-01-01T00:16:41Z", 0),
                    self.point("http_req_failed", "1970-01-01T00:16:42Z", 0),
                    self.point("http_req_failed", "1970-01-01T00:16:43Z", 1),
                    self.point("operation_failures", "1970-01-01T00:16:40Z", 0),
                    self.point("operation_failures", "1970-01-01T00:16:41Z", 0),
                    self.point("operation_failures", "1970-01-01T00:16:42Z", 0),
                    self.point("operation_failures", "1970-01-01T00:16:43Z", 1),
                    self.point("dropped_iterations", "1970-01-01T00:16:43Z", 3),
                    self.point("http_req_duration{operation:list}", "1970-01-01T00:16:43Z", 999),
                ],
            )
            history, capture = build_history(
                path, 1000, 1005, {"requests": 4, "dropped_iterations": 3}
            )
        self.assertEqual(capture["status"], "complete")
        self.assertEqual(
            history["totals"],
            {
                "requests": 4,
                "http_failures": 1,
                "validation_failures": 1,
                "dropped_iterations": 3,
            },
        )
        bucket = history["buckets"][0]
        self.assertEqual(bucket["statuses"], {"0": 1, "200": 1, "500": 1, "unknown": 1})
        self.assertEqual(bucket["latency_ms"]["med"], 15)
        self.assertAlmostEqual(bucket["latency_ms"]["p(95)"], 28.5)
        self.assertAlmostEqual(bucket["latency_ms"]["p(99)"], 29.7)
        self.assertEqual(bucket["latency_ms"]["max"], 30)

    def test_time_bounds_timezone_and_empty_bucket_latency(self):
        with TemporaryDirectory() as temporary:
            path = self.write_points(
                temporary,
                [
                    self.point("http_req_duration", "1970-01-01T01:16:40.123456789+01:00", 4),
                    self.point("http_req_failed", "1970-01-01T01:16:40.123456789+01:00", 0),
                    self.point("operation_failures", "1970-01-01T01:16:40.123456789+01:00", 0),
                    self.point("dropped_iterations", "1970-01-01T00:16:46Z", 2),
                    self.point("http_req_duration", "1970-01-01T00:16:51Z", 8),
                ],
            )
            history, _ = build_history(path, 1000, 1010, {"requests": 1, "dropped_iterations": 2})
        self.assertEqual(history["status"], "complete")
        self.assertEqual(len(history["buckets"]), 2)
        self.assertEqual(history["buckets"][0]["requests"], 1)
        self.assertEqual(
            history["buckets"][1]["latency_ms"],
            {
                "med": None,
                "p(95)": None,
                "p(99)": None,
                "max": None,
            },
        )
        self.assertEqual(history["buckets"][1]["dropped_iterations"], 2)

    def test_malformed_and_truncated_capture_is_partial_without_tail_buckets(self):
        with TemporaryDirectory() as temporary:
            path = self.write_points(
                temporary,
                [self.point("http_req_duration", "1970-01-01T00:16:41Z", 1)],
            )
            with path.open("ab") as output:
                output.write(b'{"type":')
            history, _ = build_history(path, 1000, 1020, {"requests": 1, "dropped_iterations": 0})
        self.assertEqual(history["status"], "partial")
        self.assertEqual(len(history["buckets"]), 1)
        self.assertTrue(history["warnings"])

    def test_totals_mismatch_and_missing_legacy_capture(self):
        with TemporaryDirectory() as temporary:
            path = self.write_points(
                temporary,
                [self.point("http_req_duration", "1970-01-01T00:16:41Z", 1)],
            )
            history, _ = build_history(path, 1000, 1005, {"requests": 2, "dropped_iterations": 0})
            missing, capture = build_history(Path(temporary) / "missing.gz", 1000, 1005, {})
        self.assertEqual(history["status"], "partial")
        self.assertIn("requests total 1 differs", history["warnings"][0])
        self.assertIsNone(missing)
        self.assertEqual(capture["status"], "unavailable")

    def test_mismatch_marks_partial_before_empty_tail_buckets(self):
        with TemporaryDirectory() as temporary:
            path = self.write_points(
                temporary,
                [
                    self.point("http_req_duration", "1970-01-01T00:16:41Z", 1),
                    self.point("http_req_failed", "1970-01-01T00:16:41Z", 0),
                    self.point("operation_failures", "1970-01-01T00:16:41Z", 0),
                ],
            )
            history, _ = build_history(path, 1000, 1020, {"requests": 2, "dropped_iterations": 0})
        self.assertEqual(history["status"], "partial")
        self.assertEqual(len(history["buckets"]), 1)

    def test_malformed_records_and_truncated_footer_preserve_prefix(self):
        with TemporaryDirectory() as temporary:
            path = self.write_points(
                temporary,
                [
                    self.point("http_req_duration", "1970-01-01T00:16:41Z", 1),
                    self.point("http_req_failed", "1970-01-01T00:16:41Z", 0),
                    self.point("operation_failures", "1970-01-01T00:16:41Z", 0),
                    [],
                    self.point("http_req_duration", "1970-01-01T00:16:42Z", float("nan")),
                    self.point("http_req_duration", "1970-01-01T00:16:43Z", -1),
                ],
            )
            with gzip.open(path, "at") as output:
                output.write("{not JSON}\n")
            path.write_bytes(path.read_bytes()[:-8])
            history, _ = build_history(path, 1000, 1020, {"requests": 1, "dropped_iterations": 0})
        self.assertEqual(history["status"], "partial")
        self.assertEqual(history["totals"]["requests"], 1)
        self.assertEqual(len(history["buckets"]), 1)
        self.assertGreaterEqual(len(history["warnings"]), 3)

    def test_missing_counter_points_marks_history_partial(self):
        with TemporaryDirectory() as temporary:
            path = self.write_points(
                temporary,
                [self.point("http_req_duration", "1970-01-01T00:16:41Z", 1)],
            )
            history, _ = build_history(path, 1000, 1005, {"requests": 1, "dropped_iterations": 0})
        self.assertEqual(history["status"], "partial")
        self.assertTrue(
            any("HTTP failure point count 0" in warning for warning in history["warnings"])
        )

    def test_write_history_uses_run_metadata(self):
        with TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_points(run, [self.point("http_req_duration", "1970-01-01T00:16:41Z", 1)])
            (run / "metadata.json").write_text(
                json.dumps({"measured_started_at_unix": 1000, "measured_ended_at_unix": 1005})
            )
            (run / "result.json").write_text(
                json.dumps({"result": {"requests": 1, "dropped_iterations": 0}})
            )
            history, _ = write_history(run)
            written = json.loads((run / "http-history.json").read_text())
        self.assertEqual(written, history)


if __name__ == "__main__":
    unittest.main()
