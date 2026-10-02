import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from measure import run


class Process:
    def __init__(self, output="", returncode=None):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(output)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class DiagnosticsLifecycleTests(unittest.TestCase):
    def fixture(self, temporary):
        args = run.arguments(
            [
                "bun",
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
                "--diagnostics",
                "--diagnostics-seconds",
                "1",
            ]
        )
        metadata = {
            "pod": {
                "name": "pod",
                "uid": "pod",
                "container_id": "container",
                "image_id": "image",
                "restart_count": 0,
            },
            "node": {"uid": "node"},
            "workload": {
                "configuration": {"SEED_COUNT": "5000", "DIAGNOSTICS": "1"},
                "resources": {},
            },
        }
        sample = {
            "type": "sample",
            "pod_uid": "pod",
            "container_id": "container",
            "cpu_seconds": 1,
            "cpu_timestamp_ms": 1000,
            "memory_working_set_bytes": 2,
            "memory_working_set_timestamp_ms": 1000,
        }
        return args, metadata, sample

    def summary(self, path):
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

    def test_startup_failure_preserves_valid_http_result(self):
        with TemporaryDirectory() as temporary:
            args, metadata, sample = self.fixture(temporary)
            events = [{"type": "metadata", "metadata": metadata}, sample]

            def k6(_, directory, __, label, ___=None):
                path = directory / f"{label}-k6-summary.json"
                self.summary(path)
                return 0, path

            with (
                patch.object(run, "arguments", return_value=args),
                patch.object(
                    run, "collect_once", side_effect=[(metadata, sample), (metadata, None)]
                ),
                patch.object(run, "start_collector", return_value=(Process(), [], events, [])),
                patch.object(run, "start_diagnostics", side_effect=OSError("no ssh")),
                patch.object(run, "run_k6", side_effect=k6),
                patch.object(run, "resource_statistics", return_value={"warnings": []}),
                patch.object(run.subprocess, "check_output", return_value="value"),
            ):
                self.assertEqual(run.main(), 0)
            result = json.loads(next(Path(temporary).rglob("result.json")).read_text())
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["metadata"]["diagnostics"]["status"], "partial")
            self.assertIn(
                "diagnostics startup: no ssh", result["metadata"]["diagnostics"]["warnings"]
            )

    def test_profile_validation_and_write_failures_never_retain_payload(self):
        event = json.dumps({"type": "profile", "name": "invalid", "data": "YWJj"}) + "\n"
        with (
            TemporaryDirectory() as temporary,
            patch.object(run.subprocess, "Popen", return_value=Process(event)),
        ):
            process, thread, events, errors, log, _ = run.start_diagnostics(
                self.fixture(temporary)[0], "pod", Path(temporary)
            )
            thread.join(1)
            log.close()
            self.assertEqual(events, [])
            self.assertTrue(errors)
            self.assertNotIn("YWJj", " ".join(errors))
            run._stop(process)
        event = json.dumps({"type": "profile", "name": "jsc-cpu.json", "data": "YWJj"}) + "\n"
        with (
            TemporaryDirectory() as temporary,
            patch.object(run.subprocess, "Popen", return_value=Process(event)),
            patch.object(run, "save_profile", side_effect=OSError("disk full")),
        ):
            process, thread, events, errors, log, _ = run.start_diagnostics(
                self.fixture(temporary)[0], "pod", Path(temporary)
            )
            thread.join(1)
            log.close()
            self.assertEqual(events, [])
            self.assertIn("disk full", errors[0])
            run._stop(process)

    def test_ready_precedes_diagnostics_and_missing_cpu_is_partial(self):
        with TemporaryDirectory() as temporary:
            args, metadata, sample = self.fixture(temporary)
            order, events = [], [{"type": "metadata", "metadata": metadata}, sample]
            diagnostics = [{"type": "ready"}, {"type": "end"}]

            def k6(_, directory, __, label, ___=None):
                order.append(label)
                path = directory / f"{label}-k6-summary.json"
                self.summary(path)
                return 0, path

            def start_diagnostics(*_):
                order.append("diagnostics")
                directory = Path(temporary) / "diagnostics"
                directory.mkdir(exist_ok=True)
                return Process(returncode=3), None, diagnostics, [], io.StringIO(), directory

            with (
                patch.object(run, "arguments", return_value=args),
                patch.object(
                    run, "collect_once", side_effect=[(metadata, sample), (metadata, None)]
                ),
                patch.object(run, "start_collector", return_value=(Process(), [], events, [])),
                patch.object(run, "start_diagnostics", side_effect=start_diagnostics),
                patch.object(run, "run_k6", side_effect=k6),
                patch.object(run, "resource_statistics", return_value={"warnings": []}),
                patch.object(run.subprocess, "check_output", return_value="value"),
            ):
                self.assertEqual(run.main(), 0)
            result = json.loads(next(Path(temporary).rglob("result.json")).read_text())
            self.assertEqual(order, ["warmup", "diagnostics", "measurement"])
            self.assertEqual(result["metadata"]["diagnostics"]["status"], "partial")
            self.assertIn(
                "CPU profile was not saved", result["metadata"]["diagnostics"]["warnings"]
            )
            self.assertIn(
                "diagnostics helper exited 3", result["metadata"]["diagnostics"]["warnings"]
            )


if __name__ == "__main__":
    unittest.main()
