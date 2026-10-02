import base64
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from measure import diagnostics, diagnostics_remote
from measure.results import compatible_key


class DiagnosticsTest(unittest.TestCase):
    def test_profile_is_bounded_and_filename_is_fixed(self):
        with tempfile.TemporaryDirectory() as temporary:
            saved = diagnostics.save_profile(
                Path(temporary),
                "go",
                "cpu.pprof",
                base64.b64encode(gzip.compress(b"profile")).decode(),
            )
            self.assertEqual(saved.read_bytes()[:2], b"\x1f\x8b")
            with self.assertRaises(ValueError):
                diagnostics.save_profile(Path(temporary), "go", "../../bad", "")
            with self.assertRaises(ValueError):
                diagnostics.save_profile(Path(temporary), "go", "cpu.pprof", "not base64")

    def test_summary_does_not_make_deltas_across_missing_or_reset_counters(self):
        events = [
            {
                "type": "snapshot",
                "runtime": {
                    "schema_version": 1,
                    "runtime": "go",
                    "process_id": 1,
                    "time_unix": 10,
                    "go": {
                        "gomaxprocs": 1,
                        "heap_alloc_bytes": 3,
                        "gc_cycles": 4,
                        "total_alloc_bytes": 10,
                    },
                    "database": {"wait_count": 3},
                },
            },
            {
                "type": "snapshot",
                "runtime": {
                    "schema_version": 1,
                    "runtime": "go",
                    "process_id": 1,
                    "time_unix": 20,
                    "go": {
                        "gomaxprocs": 1,
                        "heap_alloc_bytes": 5,
                        "gc_cycles": 2,
                        "total_alloc_bytes": 8,
                    },
                    "database": {"wait_count": 1},
                },
            },
        ]
        summary = diagnostics.diagnostics_summary(events, "go")
        self.assertEqual(summary["actual_gomaxprocs"], 1)
        self.assertEqual(summary["go_heap_alloc_peak_bytes"], 5)
        self.assertNotIn("go_gc_cycles_delta", summary)
        self.assertNotIn("database_wait_count_delta", summary)

    def test_summary_rejects_identity_timestamp_and_null_cpu_deltas(self):
        events = [
            {
                "type": "snapshot",
                "runtime": {
                    "schema_version": 1,
                    "runtime": "go",
                    "process_id": 1,
                    "time_unix": 2,
                    "process_cpu": None,
                    "go": {"heap_alloc_bytes": 4},
                },
            },
            {
                "type": "snapshot",
                "runtime": {
                    "schema_version": 1,
                    "runtime": "go",
                    "process_id": 2,
                    "time_unix": 1,
                    "process_cpu": None,
                    "go": {"heap_alloc_bytes": 8},
                },
            },
        ]
        summary = diagnostics.diagnostics_summary(events, "go")
        self.assertEqual(summary["status"], "partial")
        self.assertNotIn("process_cpu_seconds_per_second", summary)
        self.assertEqual(summary["go_heap_alloc_peak_bytes"], 8)

    def test_bun_database_wait_is_not_a_reset(self):
        events = [
            {
                "type": "snapshot",
                "runtime": {
                    "schema_version": 1,
                    "runtime": "bun",
                    "process_id": 1,
                    "time_unix": 1,
                    "database": {"wait_count": None},
                },
            },
            {
                "type": "snapshot",
                "runtime": {
                    "schema_version": 1,
                    "runtime": "bun",
                    "process_id": 1,
                    "time_unix": 2,
                    "database": {"wait_count": None},
                },
            },
        ]
        summary = diagnostics.diagnostics_summary(events, "bun")
        self.assertNotIn("reset database", " ".join(summary["warnings"]))

    def test_profile_validation_rejects_invalid_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaises(ValueError):
                diagnostics.save_profile(
                    directory, "go", "cpu.pprof", base64.b64encode(b"bad").decode()
                )
            with self.assertRaises(ValueError):
                diagnostics.save_profile(
                    directory, "bun", "jsc-cpu.json", base64.b64encode(b"{}").decode()
                )
            with self.assertRaises(ValueError):
                diagnostics.save_profile(
                    directory, "bun", "jsc-cpu.json", base64.b64encode(b"[]").decode()
                )

    def test_remote_uses_loopback_port_forward_and_no_exec(self):
        source = diagnostics_remote.remote_program()
        self.assertIn('"port-forward", "pod/" + pod', source)
        self.assertIn('"--address", "127.0.0.1"', source)
        self.assertNotIn('"exec"', source)
        self.assertNotIn('"get", "secret"', source)

    def test_old_baseline_key_matches_new_default_and_diagnostics_separates(self):
        def record(settings, configuration):
            return {
                "metadata": {
                    "base_url": "http://node",
                    "settings": settings,
                    "cluster": {
                        "node": {},
                        "workload": {"resources": {}, "configuration": configuration},
                    },
                }
            }

        old = record({}, {})
        new_default = record({"diagnostics": False, "diagnostics_seconds": 30}, {"DIAGNOSTICS": ""})
        enabled = record({"diagnostics": True, "diagnostics_seconds": 30}, {"DIAGNOSTICS": "1"})
        self.assertEqual(compatible_key(old), compatible_key(new_default))
        self.assertNotEqual(compatible_key(old), compatible_key(enabled))

    def test_summary_file_is_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics-summary.json"
            diagnostics.write_summary(path, [], "go")
            self.assertEqual(json.loads(path.read_text())["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
