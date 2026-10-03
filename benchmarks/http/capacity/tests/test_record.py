import json
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from benchmarks.http.capacity.record import (
    GIB,
    _capacity,
    _generator_limit,
    nofile_budget,
    required_available_memory,
)


class RecordTests(unittest.TestCase):
    def test_vu_memory_guard(self):
        self.assertEqual(required_available_memory(0), GIB)
        self.assertGreater(required_available_memory(3150), GIB)

    def test_generator_guard_accepts_collector_cpu_list_and_stops_on_fd_ceiling(self):
        samples = [
            {
                "numThreads": 1,
                "numFds": 900,
                "availableMemoryBytes": GIB,
                "hostCpuPercent": [10.0, 12.0],
            }
            for _ in range(5)
        ]
        self.assertEqual(_generator_limit(samples, 3 * GIB, 1000), "generator_limit_nofile")

    def test_fd_budget_leaves_headroom_for_observed_3663_rps_generator(self):
        vus = 7693
        budget = nofile_budget(vus)
        self.assertEqual(budget, 19873)
        sample = {
            "numThreads": 354,
            "numFds": 9550,
            "availableMemoryBytes": GIB,
            "hostCpuPercent": [10.0],
        }
        self.assertIsNone(_generator_limit([sample], 3 * GIB, budget))
        self.assertEqual(
            _generator_limit([dict(sample, numFds=math.ceil(budget * 0.9))] * 5, 3 * GIB, budget),
            "generator_limit_nofile",
        )

    def test_final_oom_stage_is_not_ranked_as_no_overload(self):
        stage = {
            "targetRps": 900,
            "completed": True,
            "normalized": {
                "validity": {"status": "valid"},
                "windows": [{"slo": {"status": "pass"}}],
            },
            "overload": {"status": True, "reasons": ["oom_or_restart"]},
        }
        capacity = _capacity([stage], "pod_restart_or_oom")
        self.assertEqual(capacity["firstOverloadRps"], 900)
        self.assertIsNone(capacity["highestNoOverloadRps"])

    def _args(self, directory):
        return SimpleNamespace(
            results_dir=Path(directory),
            attempt_id="11111111-1111-1111-1111-111111111111",
            runtime="go",
            framework="nethttp",
            image="ghcr.io/jamoowen/performance-http-ramp-go@sha256:" + "a" * 64,
            source_revision="b" * 40,
            harness_source_revision="c" * 40,
            flux_revision="d" * 40,
            base_url="http://127.0.0.1:30083",
            namespace="my-api",
            ssh_host="user@host",
            k6="k6",
            safety_ceiling=1500,
        )

    def _normalized(self, *, overload=False):
        history = [
            {
                "seconds": second,
                "bucketSeconds": 5,
                "phase": "stable",
                "completed": 500,
                "successful": 495 if overload else 500,
                "httpFailures": 5 if overload else 0,
                "validationFailures": 0,
                "checksFailed": 5 if overload else 0,
                "dropped": 0,
            }
            for second in (15, 20, 25, 30)
        ]
        return {
            "validity": {"status": "valid", "reasons": []},
            "scenarioOrigin": 100.0,
            "counts": {
                "request_outcomes": 1,
                "outcome:success": 1,
                "checks": 1,
                "drops": 0,
            },
            "history": history,
            "windows": [
                {
                    "targetRps": 300,
                    "startSeconds": 15,
                    "endSeconds": 90,
                    "expectedArrivals": 22500,
                    "completed": 7500,
                    "httpFailures": 75 if overload else 0,
                    "validationFailures": 0,
                    "dropped": 0,
                    "client": {"p95Ms": 10},
                    "slo": {"status": "pass"},
                }
            ],
        }

    def _remote_module(self, *, events=None, errors=None):
        class Collector:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self._metadata_event = threading.Event()
                self._metadata_event.set()
                self.events = list(events or [])
                self.errors = list(errors or [])
                self.stopped = False
                Collector.instances.append(self)

            def start(self):
                return self

            def stop(self):
                self.stopped = True

            def join(self, timeout=30):
                samples = [
                    {
                        "realtimeStartSeconds": 115 + second,
                        "realtimeEndSeconds": 116 + second,
                        "cpuMillicores": 100,
                        "workingSetBytes": 10,
                        "memoryCurrentBytes": 10,
                        "memoryPeakBytes": 10,
                        "throttledSeconds": 0,
                        "cfsPeriods": 1,
                        "cfsThrottledPeriods": 0,
                    }
                    for second in range(75)
                ]
                return {
                    "samples": samples,
                    "containerSamples": samples,
                    "events": self.events,
                    "errors": self.errors,
                    "coverage": 1,
                }

        module = ModuleType("benchmarks.http.capacity.telemetry")
        module.PodCollector = Collector
        return module, Collector

    def _run(
        self,
        *,
        overload=False,
        generator_limit=None,
        remote_events=None,
        remote_errors=None,
        stock_failure=False,
    ):
        with tempfile.TemporaryDirectory() as directory:
            args = self._args(directory)
            module, collector = self._remote_module(events=remote_events)
            calls = []

            def fake_run_k6(_args, mode, step, _out):
                calls.append(mode)
                if mode == "step" and remote_errors:
                    collector.instances[0].errors.extend(remote_errors)
                output = f"{mode}.json.gz"
                (_out / output).touch()
                return {
                    "returncode": 0,
                    "generator": {"coverage": 1, "errors": [], "samples": []},
                    "generatorLimit": generator_limit if mode == "step" else None,
                    "startNs": 1,
                    "endNs": 2,
                    "output": output,
                    "summary": f"{mode}.json",
                    "log": f"{mode}.log",
                }

            def fake_normalize(_out, _capture, step):
                if step.target_rps == 100:
                    return self._normalized()
                return self._normalized(overload=overload)

            metadata = {
                "attemptId": args.attempt_id,
                "runtime": args.runtime,
                "framework": args.framework,
                "image": args.image,
                "sourceRevision": args.source_revision,
                "harnessSourceRevision": args.harness_source_revision,
                "generatorHardware": {},
            }
            stock_calls = 0

            def stock_outcomes(_path):
                nonlocal stock_calls
                stock_calls += 1
                if stock_failure and stock_calls == 2:
                    raise RuntimeError("stock parse failed")
                return {"acknowledged": 0, "failed": 0}

            with (
                patch.dict(sys.modules, {"benchmarks.http.capacity.telemetry": module}),
                patch(
                    "benchmarks.http.capacity.record.request",
                    return_value={"rows": 5000, "totalRevisions": 0, "totalStock": 0},
                ),
                patch("benchmarks.http.capacity.record._seed_total", return_value=0),
                patch("benchmarks.http.capacity.record._metadata", return_value=metadata),
                patch("benchmarks.http.capacity.record._k6_version", return_value="k6 test"),
                patch(
                    "benchmarks.http.capacity.record.clock_alignment", return_value={"offsetNs": 0}
                ),
                patch(
                    "benchmarks.http.capacity.record.load_pod",
                    return_value={"pod": "pod", "uid": "u", "containerId": "c"},
                ),
                patch("benchmarks.http.capacity.record.preflight", return_value={}),
                patch("benchmarks.http.capacity.record.run_k6", side_effect=fake_run_k6),
                patch(
                    "benchmarks.http.capacity.record._safe_normalize", side_effect=fake_normalize
                ),
                patch(
                    "benchmarks.http.capacity.record._stock_outcomes",
                    side_effect=stock_outcomes,
                ),
                patch(
                    "benchmarks.http.capacity.record._drain_integrity",
                    return_value={"rows": 5000, "totalRevisions": 0, "totalStock": 0},
                ),
                patch("benchmarks.http.capacity.record._validate_integrity", return_value=([], 0)),
            ):
                result = __import__("benchmarks.http.capacity.record", fromlist=["run"]).run(args)
            saved = json.loads((Path(directory) / args.attempt_id / "result.json").read_text())
            checkpoint = json.loads(
                (Path(directory) / args.attempt_id / "checkpoint.json").read_text()
            )
            return result, saved, checkpoint, calls, collector.instances[0]

    def test_recorder_persists_a_healthy_overload_boundary(self):
        result, saved, checkpoint, calls, collector = self._run(overload=True)
        self.assertEqual(calls, ["warmup", "step"])
        self.assertEqual(result["capacity"]["firstOverloadRps"], 300)
        self.assertEqual(saved["validity"]["status"], "valid")
        self.assertEqual(checkpoint["status"], "complete")
        self.assertTrue(collector.stopped)

    def test_recorder_keeps_oom_as_valid_workload_boundary(self):
        result, _saved, _checkpoint, _calls, _collector = self._run(
            remote_events=[{"type": "oom", "finishedAt": "1970-01-01T00:01:50Z"}]
        )
        self.assertEqual(result["capacity"]["stopReason"], "pod_restart_or_oom")
        self.assertEqual(result["capacity"]["firstOverloadRps"], 300)
        self.assertEqual(result["validity"]["status"], "valid")

    def test_recorder_marks_generator_limited_step_invalid_and_stops(self):
        result, _saved, _checkpoint, calls, collector = self._run(
            generator_limit="generator_limit_threads"
        )
        self.assertEqual(calls, ["warmup", "step"])
        self.assertTrue(result["capacity"]["generatorLimited"])
        self.assertEqual(result["validity"]["status"], "invalid")
        self.assertTrue(collector.stopped)

    def test_recorder_stops_after_collector_error_and_cleans_up(self):
        result, _saved, _checkpoint, calls, collector = self._run(remote_errors=["remote_failure"])
        self.assertEqual(calls, ["warmup", "step"])
        self.assertEqual(result["capacity"]["stopReason"], "collector_infrastructure")
        self.assertEqual(result["validity"]["status"], "invalid")
        self.assertTrue(collector.stopped)

    def test_late_recorder_failure_retains_prior_stage_and_telemetry(self):
        result, saved, _checkpoint, calls, collector = self._run(stock_failure=True)
        self.assertEqual(calls, ["warmup", "step"])
        self.assertEqual(result["capacity"]["stopReason"], "recorder_failure")
        self.assertEqual(result["validity"]["status"], "invalid")
        self.assertEqual(len(saved["stages"]), 1)
        self.assertTrue(saved["history"])
        self.assertTrue(saved["resource"]["samples"])
        self.assertTrue(collector.stopped)
