"""Focused recorder ownership tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from benchmarks.http.ramp.measure import record

IMAGE = "ghcr.io/jamoowen/performance-http-ramp-go@sha256:" + "a" * 64


def pod(image_id: str | None = IMAGE) -> dict:
    status = {
        "name": "http-ramp",
        "ready": True,
        "restartCount": 0,
        "containerID": "containerd://" + "c" * 64,
        "image": "sha256:" + "b" * 64,
    }
    if image_id is not None:
        status["imageID"] = image_id
    return {
        "metadata": {
            "name": "http-ramp",
            "uid": "01234567-89ab-cdef-0123-456789abcdef",
            "annotations": {"benchmark.jamoowen.dev/attempt-id": "attempt"},
        },
        "spec": {"containers": [{"name": "http-ramp", "image": IMAGE}]},
        "status": {"containerStatuses": [status]},
    }


class RecordTests(unittest.TestCase):
    def test_load_pod_accepts_resolved_manifest_identity(self):
        args = SimpleNamespace(
            ssh_host="host", namespace="my-api", attempt_id="attempt", image=IMAGE
        )
        response = SimpleNamespace(stdout=json.dumps({"items": [pod()]}))
        with patch.object(record, "_ssh", return_value=response):
            self.assertEqual(
                record.load_pod(args),
                {
                    "pod": "http-ramp",
                    "uid": "01234567-89ab-cdef-0123-456789abcdef",
                    "containerId": "c" * 64,
                },
            )

    def test_load_pod_rejects_missing_or_wrong_resolved_image_id(self):
        args = SimpleNamespace(
            ssh_host="host", namespace="my-api", attempt_id="attempt", image=IMAGE
        )
        for image_id in (None, "wrong"):
            response = SimpleNamespace(stdout=json.dumps({"items": [pod(image_id)]}))
            with patch.object(record, "_ssh", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "pod identity"):
                    record.load_pod(args)

    def test_clock_alignment_uses_lowest_rtt_and_midpoint_offset(self):
        with (
            patch.object(record, "_server_clock_ns", side_effect=[110, 220, 330]),
            patch.object(record.time, "time_ns", side_effect=[0, 20, 100, 104, 200, 240]),
        ):
            value = record.clock_alignment("bench.example")
        self.assertEqual(value["selected"]["rttNs"], 4)
        self.assertEqual(value["offsetNs"], 118)
        self.assertEqual(value["uncertaintyNs"], 2)

    def test_alignment_applies_server_offset_and_origin_to_intervals(self):
        samples = {"samples": [{"realtimeStartSeconds": 15.0, "realtimeEndSeconds": 16.0}]}
        value = record._align_samples(samples, 2_000_000_000, 10.0)
        self.assertEqual(value[0]["intervalStartSeconds"], 3.0)
        self.assertEqual(value[0]["intervalEndSeconds"], 4.0)

    def test_metadata_rejects_wrong_runtime_and_keeps_flat_api_fields(self):
        args = SimpleNamespace(
            attempt_id="a",
            runtime="go",
            framework="chi",
            image="image",
            source_revision="src",
            harness_source_revision="harness",
            flux_revision="flux",
            schedule_json="[]",
            local_only=False,
        )
        info = {
            "experiment": "sqlite-ramp-v2",
            "runtime": "go",
            "framework": "chi",
            "runtimeVersion": "1",
            "frameworkVersion": "2",
            "driver": "sqlite",
            "driverVersion": "3",
            "sqliteVersion": "4",
            "seedCount": 5000,
            "compileOptions": ["THREADSAFE=1"],
            "pragmas": {
                "journal_mode": "wal",
                "synchronous": 1,
                "foreign_keys": 1,
                "busy_timeout": 5000,
                "cache_size": -2000,
                "wal_autocheckpoint": 1000,
                "temp_store": 2,
            },
            "workers": 1,
            "cgoEnabled": False,
            "gomaxprocs": 1,
        }
        with patch.object(record, "load_hash", return_value="load"):
            metadata = record._metadata(args, info)
        self.assertEqual(metadata["runtimeVersion"], "1")
        self.assertEqual(metadata["compileOptions"], ["THREADSAFE=1"])
        info["runtime"] = "node"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            record._metadata(args, info)

    def test_route_uses_parsed_host_without_shell(self):
        reply = SimpleNamespace(stdout="1.2.3.4 dev en0 src 1.2.3.5")
        with (
            patch.object(record.platform, "system", return_value="Linux"),
            patch.object(record.subprocess, "run", return_value=reply) as run,
        ):
            self.assertEqual(record.route_interface("http://1.2.3.4:30083/path"), "en0")
        self.assertEqual(run.call_args.args[0], ["ip", "route", "get", "1.2.3.4"])

    def test_k6_timeout_terminates_then_collects_generator(self):
        process = MagicMock(pid=12, returncode=-15)
        process.poll.side_effect = [None, None, 0]
        process.wait.side_effect = [subprocess.TimeoutExpired("k6", 1200), None]
        collector = SimpleNamespace(
            start=lambda: collector, stop=MagicMock(), join=lambda timeout: {"coverage": 1}
        )
        args = SimpleNamespace(
            base_url="http://127.0.0.1",
            schedule_json="[]",
            preallocated_vus=1,
            warmup_rps=1,
            warmup_seconds=1,
            k6="k6",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(record.subprocess, "Popen", return_value=process),
            patch.object(record, "GeneratorCollector", return_value=collector),
            patch.object(record, "route_interface", return_value="lo"),
            patch.object(record.time, "monotonic", side_effect=[0, 32]),
        ):
            result = record.k6_run(
                args,
                "measurement",
                record.Path(directory) / "raw",
                record.Path(directory) / "summary",
                record.Path(directory) / "k6.log",
                1,
            )
        self.assertTrue(result.timed_out)
        process.terminate.assert_called_once()
        collector.stop.assert_called_once()

    def test_k6_generator_start_failure_kills_child(self):
        process = MagicMock(pid=12, returncode=-9)
        process.poll.return_value = None
        args = SimpleNamespace(
            base_url="http://127.0.0.1",
            schedule_json="[]",
            preallocated_vus=1,
            warmup_rps=1,
            warmup_seconds=1,
            k6="k6",
            local_only=True,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(record.subprocess, "Popen", return_value=process),
            patch.object(record, "GeneratorCollector", side_effect=RuntimeError("boom")),
            patch.object(record, "route_interface", return_value="lo"),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                record.k6_run(
                    args,
                    "warmup",
                    record.Path(directory) / "raw",
                    record.Path(directory) / "summary",
                    record.Path(directory) / "log",
                    1,
                )
        process.kill.assert_called_once()

    def test_drain_requires_two_consecutive_results(self):
        values = [{"rows": 1}, {"rows": 2}, {"rows": 2}]
        args = SimpleNamespace(base_url="http://example")
        with (
            patch.object(record, "request", side_effect=values),
            patch.object(record.time, "monotonic", side_effect=[0, 1, 2, 3, 4]),
            patch.object(record.time, "sleep"),
        ):
            self.assertEqual(record._drain_integrity(args, {}), {"rows": 2})

    def test_integrity_reconciliation_bounds_excess(self):
        before = {"rows": 5000, "totalStock": 100, "totalRevisions": 0}
        self.assertEqual(
            record._validate_integrity(
                before, {"rows": 5000, "totalStock": 105, "totalRevisions": 5}, 5, 0
            ),
            ([], 0),
        )
        self.assertEqual(
            record._validate_integrity(
                before, {"rows": 5000, "totalStock": 106, "totalRevisions": 6}, 5, 1
            ),
            ([], 1),
        )
        self.assertEqual(
            record._validate_integrity(
                before, {"rows": 5000, "totalStock": 107, "totalRevisions": 7}, 5, 1
            )[0],
            ["integrity_excess_revisions"],
        )
        self.assertEqual(
            record._validate_integrity(
                before, {"rows": 4999, "totalStock": 105, "totalRevisions": 5}, 5, 0
            )[0],
            ["integrity_mismatch"],
        )


if __name__ == "__main__":
    unittest.main()
