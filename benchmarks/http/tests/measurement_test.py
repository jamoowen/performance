import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from measure import run
from measure.results import (
    compatible_key,
    exact_container_metric,
    normalize_k6,
    parse_quantity,
    resource_statistics,
)


class MeasurementTests(unittest.TestCase):
    def test_rust_diagnostics_is_rejected_before_recording(self):
        with self.assertRaises(SystemExit):
            run.arguments(
                [
                    "rust",
                    "--base-url",
                    "http://localhost",
                    "--ssh-host",
                    "host",
                    "--diagnostics",
                ]
            )

    def runner_fixture(self, temporary):
        args = run.arguments(
            [
                "go",
                "--base-url",
                "http://localhost",
                "--ssh-host",
                "host",
                "--results-dir",
                temporary,
                "--duration",
                "1s",
                "--warmup-duration",
                "1s",
            ]
        )
        metadata = {
            "pod": {
                "uid": "pod",
                "container_id": "container",
                "image_id": "image",
                "restart_count": 0,
            },
            "node": {"uid": "node"},
            "workload": {
                "configuration": {"SEED_COUNT": "5000", "MAX_OPEN_CONNS": "1"},
                "resources": {},
            },
        }
        now = 1_000_000_000_000
        sample = {
            "type": "sample",
            "pod_uid": "pod",
            "container_id": "container",
            "cpu_seconds": 1,
            "cpu_timestamp_ms": now,
            "memory_working_set_bytes": 2,
            "memory_working_set_timestamp_ms": now,
        }
        return args, metadata, sample

    def runner_summary(self, path):
        path.write_text(
            json.dumps(
                {
                    "state": {"testRunDurationMs": 1000},
                    "metrics": {
                        "http_reqs": {"values": {"count": 1}},
                        "http_req_failed": {"values": {"rate": 0}},
                        "http_req_duration": {"values": {}},
                        "checks": {"values": {"rate": 1}},
                    },
                }
            )
        )

    def test_quantities(self):
        self.assertEqual(parse_quantity("100m"), 0.1)
        self.assertEqual(parse_quantity("10Mi"), 10 * 1024**2)
        self.assertEqual(parse_quantity("1n"), 1e-9)

    def test_exact_metric(self):
        line = 'container_cpu_usage_seconds_total{container="http-go",namespace="my-api",pod="pod"} 4 5000'
        self.assertEqual(
            exact_container_metric(
                line, "container_cpu_usage_seconds_total", "my-api", "pod", "http-go"
            )["value"],
            4,
        )
        self.assertIsNone(
            exact_container_metric(
                line, "container_cpu_usage_seconds_total", "my-api", "other", "http-go"
            )
        )

    def test_weighted_cpu_deduplicates_timestamps(self):
        samples = [
            {
                "cpu_seconds": 0,
                "cpu_timestamp_ms": 1000,
                "pod_uid": "p",
                "container_id": "a",
                "memory_working_set_bytes": 2,
                "memory_working_set_timestamp_ms": 1000,
            },
            {"cpu_seconds": 1, "cpu_timestamp_ms": 2000, "pod_uid": "p", "container_id": "a"},
            {
                "cpu_seconds": 2,
                "cpu_timestamp_ms": 3000,
                "pod_uid": "p",
                "container_id": "a",
                "memory_working_set_bytes": 4,
                "memory_working_set_timestamp_ms": 3000,
            },
        ]
        result = resource_statistics(samples)
        self.assertEqual(result["mean_cpu_millicores"], 1000)
        self.assertEqual(result["max_sampled_working_set_bytes"], 4)

    def test_reset_and_no_cpu_are_invalid(self):
        reset = resource_statistics(
            [
                {"cpu_seconds": 2, "cpu_timestamp_ms": 1000, "pod_uid": "p", "container_id": "a"},
                {"cpu_seconds": 1, "cpu_timestamp_ms": 2000, "pod_uid": "p", "container_id": "a"},
            ]
        )
        self.assertIsNone(reset["mean_cpu_millicores"])
        self.assertIn("CPU counter reset", reset["warnings"])
        self.assertIsNone(resource_statistics([])["mean_cpu_millicores"])

    def test_boundaries_and_optional_metrics(self):
        samples = [
            {
                "cpu_seconds": 0,
                "cpu_timestamp_ms": 0,
                "pod_uid": "p",
                "container_id": "a",
                "memory_working_set_bytes": 999,
                "memory_working_set_timestamp_ms": 0,
            },
            {
                "cpu_seconds": 1,
                "cpu_timestamp_ms": 1000,
                "pod_uid": "p",
                "container_id": "a",
                "memory_working_set_bytes": 5,
                "memory_working_set_timestamp_ms": 1000,
            },
            {
                "cpu_seconds": 2,
                "cpu_timestamp_ms": 2000,
                "pod_uid": "p",
                "container_id": "a",
                "memory_working_set_bytes": 7,
                "memory_working_set_timestamp_ms": 2000,
            },
            {
                "cpu_seconds": 3,
                "cpu_timestamp_ms": 3000,
                "pod_uid": "p",
                "container_id": "a",
                "memory_working_set_bytes": 999,
                "memory_working_set_timestamp_ms": 3000,
            },
        ]
        result = resource_statistics(samples, 1, 2)
        self.assertEqual(result["max_sampled_working_set_bytes"], 7)
        self.assertIsNone(result["throttled_periods_percent"])

    def test_summary(self):
        result = normalize_k6(
            {
                "state": {"testRunDurationMs": 1000},
                "metrics": {
                    "http_reqs": {"values": {"count": 10}},
                    "checks": {"values": {"rate": 1}},
                    "http_req_failed": {"values": {"rate": 0}},
                    "http_req_duration": {
                        "values": {"med": 2, "p(95)": 5, "p(99)": 9},
                        "thresholds": {"p(95)<=3": {"ok": False}},
                    },
                    "http_req_duration{operation:list}": {"values": {"p(95)": 4}},
                },
            }
        )
        self.assertEqual(result["achieved_rps"], 10)
        self.assertEqual(result["thresholds_failed"], ["http_req_duration"])
        self.assertEqual(result["operation_p95_ms"], {"list": 4})

    def test_malformed_summary_fails(self):
        with self.assertRaises(ValueError):
            normalize_k6({"metrics": {}})

    def test_compatible_key_normalizes_resources_and_pool(self):
        common = {
            "schema_version": 1,
            "metadata": {
                "base_url": "http://node:30080/products",
                "settings": {},
                "cluster": {
                    "node": {},
                    "workload": {
                        "resources": {"requests": {"cpu": "1000m"}, "limits": {}},
                        "configuration": {"SEED_COUNT": 5000, "MAX_OPEN_CONNS": None},
                    },
                },
            },
        }
        equivalent = {
            **common,
            "metadata": {
                **common["metadata"],
                "cluster": {
                    **common["metadata"]["cluster"],
                    "workload": {
                        "resources": {"requests": {"cpu": "1"}, "limits": {}},
                        "configuration": {"SEED_COUNT": "5000"},
                    },
                },
            },
        }
        self.assertEqual(compatible_key(common), compatible_key(equivalent))
        with_capture = {
            **common,
            "metadata": {
                **common["metadata"],
                "http_capture": {"format": "k6-json-gzip", "phase": "measurement"},
            },
        }
        self.assertEqual(compatible_key(common), compatible_key(with_capture))

    def test_preflight_failure_happens_before_load(self):
        with TemporaryDirectory() as temporary:
            args = run.arguments(
                [
                    "go",
                    "--base-url",
                    "http://localhost",
                    "--ssh-host",
                    "host",
                    "--results-dir",
                    temporary,
                ]
            )
            with (
                patch.object(run, "arguments", return_value=args),
                patch.object(run, "collect_once", return_value=({}, None)),
                patch.object(run.subprocess, "check_output", return_value="value"),
                patch.object(run, "run_k6") as load,
            ):
                self.assertEqual(run.main(), 1)
                load.assert_not_called()
            self.assertEqual(len(list(Path(temporary).rglob("result.json"))), 1)

    def test_collector_failure_preserves_k6_result(self):
        with TemporaryDirectory() as temporary:
            args, metadata, sample = self.runner_fixture(temporary)
            errors, process = (
                [],
                type(
                    "Process",
                    (),
                    {
                        "poll": lambda self: None,
                        "terminate": lambda self: None,
                        "wait": lambda self, timeout=None: 0,
                    },
                )(),
            )
            events = [{"type": "metadata", "metadata": metadata}, sample]

            def k6(_, directory, __, label, ___=None):
                path = directory / f"{label}-k6-summary.json"
                self.runner_summary(path)
                if label == "measurement":
                    errors.append("collector failed")
                return (0, path)

            with (
                patch.object(run, "arguments", return_value=args),
                patch.object(
                    run, "collect_once", side_effect=[(metadata, sample), (metadata, None)]
                ),
                patch.object(run, "start_collector", return_value=(process, [], events, errors)),
                patch.object(run, "run_k6", side_effect=k6),
                patch.object(run.subprocess, "check_output", return_value="value"),
            ):
                self.assertEqual(run.main(), 1)
            record = json.loads(next(Path(temporary).rglob("result.json")).read_text())
            self.assertEqual(record["result"]["requests"], 1)
            self.assertEqual(record["status"], "invalid")

    def test_missing_measurement_summary_fails(self):
        with TemporaryDirectory() as temporary:
            args, metadata, sample = self.runner_fixture(temporary)
            process = type(
                "Process",
                (),
                {
                    "poll": lambda self: None,
                    "terminate": lambda self: None,
                    "wait": lambda self, timeout=None: 0,
                },
            )()
            events = [{"type": "metadata", "metadata": metadata}, sample]

            def k6(_, directory, __, label, ___=None):
                path = directory / f"{label}-k6-summary.json"
                if label == "warmup":
                    self.runner_summary(path)
                return (0, path)

            with (
                patch.object(run, "arguments", return_value=args),
                patch.object(run, "collect_once", return_value=(metadata, sample)),
                patch.object(run, "start_collector", return_value=(process, [], events, [])),
                patch.object(run, "run_k6", side_effect=k6),
                patch.object(run.subprocess, "check_output", return_value="value"),
            ):
                self.assertEqual(run.main(), 1)
            self.assertIn(
                "without a summary",
                json.loads(next(Path(temporary).rglob("result.json")).read_text())["error"],
            )

    def test_post_restart_is_invalid(self):
        with TemporaryDirectory() as temporary:
            args, metadata, sample = self.runner_fixture(temporary)
            after = {**metadata, "pod": {**metadata["pod"], "restart_count": 1}}
            process = type(
                "Process",
                (),
                {
                    "poll": lambda self: None,
                    "terminate": lambda self: None,
                    "wait": lambda self, timeout=None: 0,
                },
            )()
            events = [{"type": "metadata", "metadata": metadata}, sample]

            def k6(_, directory, __, label, ___=None):
                path = directory / f"{label}-k6-summary.json"
                self.runner_summary(path)
                return (0, path)

            with (
                patch.object(run, "arguments", return_value=args),
                patch.object(run, "collect_once", side_effect=[(metadata, sample), (after, None)]),
                patch.object(run, "start_collector", return_value=(process, [], events, [])),
                patch.object(run, "run_k6", side_effect=k6),
                patch.object(run.subprocess, "check_output", return_value="value"),
            ):
                self.assertEqual(run.main(), 1)
            record = json.loads(next(Path(temporary).rglob("result.json")).read_text())
            self.assertEqual(record["status"], "invalid")
            self.assertIn("restart_count", record["error"])

    def test_keyboard_interrupt_persists_partial_directory(self):
        with TemporaryDirectory() as temporary:
            args, metadata, sample = self.runner_fixture(temporary)
            with (
                patch.object(run, "arguments", return_value=args),
                patch.object(run, "collect_once", return_value=(metadata, sample)),
                patch.object(run, "run_k6", side_effect=KeyboardInterrupt),
                patch.object(run.subprocess, "check_output", return_value="value"),
            ):
                self.assertEqual(run.main(), 130)
            record = json.loads(next(Path(temporary).rglob("result.json")).read_text())
            self.assertEqual(record["status"], "interrupted")
            self.assertEqual(record["k6_exit_code"], 130)

    def test_run_k6_interrupt_terminates_process(self):
        class Process:
            def __init__(self):
                self.stdout = self
                self.terminated = False

            def __iter__(self):
                raise KeyboardInterrupt

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        process = Process()
        args = SimpleNamespace(
            base_url="http://localhost",
            profile="steady",
            workload="list",
            rate=1,
            seed_count=1,
            preallocated_vus=1,
            max_vus=1,
            p95_ms=1,
            max_error_rate=0,
            k6="k6",
        )
        with (
            TemporaryDirectory() as temporary,
            patch.object(run.subprocess, "Popen", return_value=process),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run.run_k6(args, Path(temporary), "1s", "measurement")
        self.assertTrue(process.terminated)

    def test_run_k6_captures_only_the_measured_workload(self):
        class Process:
            stdout = []

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        args = SimpleNamespace(
            base_url="http://localhost",
            profile="steady",
            workload="list",
            rate=1,
            seed_count=1,
            preallocated_vus=1,
            max_vus=1,
            p95_ms=1,
            max_error_rate=0,
            k6="k6",
        )
        with (
            TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"K6_OUT": "json=inherited.gz"}),
            patch.object(run.subprocess, "Popen", return_value=Process()) as popen,
        ):
            run.run_k6(args, Path(temporary), "1s", "warmup")
            run.run_k6(args, Path(temporary), "1s", "measurement")
        warmup_command = popen.call_args_list[0].args[0]
        measurement_command = popen.call_args_list[1].args[0]
        self.assertNotIn("--out", warmup_command)
        self.assertEqual(
            measurement_command[2:4], ["--out", f"json={Path(temporary) / 'http-metrics.json.gz'}"]
        )
        self.assertNotIn("K6_OUT", popen.call_args_list[0].kwargs["env"])
        self.assertNotIn("K6_OUT", popen.call_args_list[1].kwargs["env"])

    def test_history_error_preserves_measurement_summary(self):
        with TemporaryDirectory() as temporary:
            args, metadata, sample = self.runner_fixture(temporary)
            process = type(
                "Process",
                (),
                {
                    "poll": lambda self: None,
                    "terminate": lambda self: None,
                    "wait": lambda self, timeout=None: 0,
                },
            )()
            measured_sample = {
                **sample,
                "cpu_seconds": 2,
                "cpu_timestamp_ms": sample["cpu_timestamp_ms"] + 1000,
                "memory_working_set_timestamp_ms": sample["memory_working_set_timestamp_ms"] + 1000,
            }
            events = [{"type": "metadata", "metadata": metadata}, sample, measured_sample]

            def k6(_, directory, __, label, ___=None):
                path = directory / f"{label}-k6-summary.json"
                self.runner_summary(path)
                return 0, path

            with (
                patch.object(run, "arguments", return_value=args),
                patch.object(run, "collect_once", return_value=(metadata, sample)),
                patch.object(run, "start_collector", return_value=(process, [], events, [])),
                patch.object(run, "run_k6", side_effect=k6),
                patch.object(run, "write_history", side_effect=RuntimeError("bad raw capture")),
                patch.object(run, "resource_statistics", return_value={"warnings": []}),
                patch.object(run.subprocess, "check_output", return_value="value"),
            ):
                self.assertEqual(run.main(), 0)
            record = json.loads(next(Path(temporary).rglob("result.json")).read_text())
            self.assertEqual(record["status"], "complete")
            self.assertEqual(record["result"]["requests"], 1)
            self.assertEqual(record["metadata"]["http_capture"]["status"], "partial")
            self.assertIn("bad raw capture", record["metadata"]["http_capture"]["warnings"][0])
            self.assertEqual(record["metadata"]["http_capture"]["format"], "k6-json-gzip")


if __name__ == "__main__":
    unittest.main()
