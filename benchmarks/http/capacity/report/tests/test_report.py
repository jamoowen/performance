import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.http.capacity.report.report import VARIANTS, aggregate_results, csv_rows, sanitize

SOURCE = "a" * 40
HARNESS = "b" * 40
LOAD = "c" * 64
PROTOCOL = "d" * 64
IMAGE = "ghcr.io/test@sha256:" + "e" * 64


def run(runtime="go", framework="nethttp", attempt="go-nethttp"):
    return {
        "experiment": "sqlite-capacity-v1",
        "attemptId": attempt,
        "metadata": {
            "runtime": runtime,
            "framework": framework,
            "image": IMAGE,
            "sourceRevision": SOURCE,
            "harnessSourceRevision": HARNESS,
            "loadHash": LOAD,
            "protocolHash": PROTOCOL,
            "runtimeVersion": "1",
            "frameworkVersion": "1",
            "driver": "sqlite",
            "sqliteVersion": "3",
            "workers": 1,
            "measuredVus": 1024,
            "maxVus": 1024,
            "warmupVus": 256,
            "collectorDurationSeconds": 3600,
        },
        "validity": {"status": "valid", "reasons": []},
        "capacity": {
            "highestPassingRps": 300,
            "highestNoOverloadRps": 300,
            "firstOverloadRps": 600,
            "firstLatencyFailureRps": None,
            "stopReason": "errors",
            "generatorLimited": False,
        },
        "stages": [
            {
                "stageIndex": 0,
                "targetRps": 300,
                "offsetSeconds": 0,
                "completed": True,
                "overload": {"status": False, "reasons": []},
                "normalized": {
                    "windows": [
                        {
                            "targetRps": 300,
                            "stable": True,
                            "startSeconds": 15,
                            "endSeconds": 90,
                            "goodputRps": 300,
                            "completed": 22500,
                            "successful": 22500,
                            "dropped": 0,
                            "httpFailures": 0,
                            "validationFailures": 0,
                            "client": {"p95Ms": 10},
                            "serviceP95Ms": 3,
                            "dbP95Ms": 1,
                            "resource": {
                                "cpuMillicores": 400,
                                "workingSetBytes": 10485760,
                                "cfsPeriodRatio": 0.1,
                            },
                        }
                    ],
                    "history": [
                        {
                            "seconds": 20,
                            "targetRps": 300,
                            "phase": "stable",
                            "bucketSeconds": 5,
                            "completed": 1500,
                            "successful": 1500,
                            "dropped": 0,
                            "httpFailures": 0,
                            "clientP95Ms": 10,
                        }
                    ],
                },
            }
        ],
        "resource": {
            "scope": "pod",
            "coverage": 1,
            "samples": [
                {
                    "seconds": 20,
                    "cpuMillicores": 400,
                    "workingSetBytes": 10485760,
                    "memoryPeakBytes": 12582912,
                }
            ],
            "containerSamples": [{"seconds": 20, "cfsPeriodRatio": 0.1, "containerSegment": 3}],
            "events": [{"type": "oom", "seconds": 95, "reason": "oom", "pod": "private"}],
        },
        "generator": {"coverage": 1, "headroomFlag": False, "warnings": [], "peakThreads": 10},
        "integrity": {"status": "verified", "acknowledged": 10, "failed": 1},
        "schedule": {
            "hash": PROTOCOL,
            "stages": [
                {
                    "targetRps": 300,
                    "offsetSeconds": 0,
                    "stableStartSeconds": 15,
                    "stableEndSeconds": 90,
                }
            ],
        },
    }


class CapacityReportTests(unittest.TestCase):
    def test_preserves_oom_event_and_never_turns_missing_data_into_zero(self):
        data = sanitize({"experiment": "sqlite-capacity-v1", "runs": [run()]})
        result = data["runs"][0]
        self.assertEqual(
            result["resource"]["events"], [{"seconds": 95, "type": "oom", "reason": "oom"}]
        )
        self.assertIsNone(result["resource"]["samples"][0]["memoryCurrentBytes"])
        self.assertEqual(result["resource"]["samples"][0]["memoryPeakBytes"], 12582912)
        self.assertEqual(result["resource"]["containerSamples"][0]["segment"], 3)
        # The event is after the final stable window (90s), so the dashboard
        # must keep it for the selected final stage's drain/normalization tail.
        self.assertGreater(result["resource"]["events"][0]["seconds"], 90)
        script = (Path(__file__).parents[1] / "report.js").read_text()
        self.assertIn("selectedEvent", script)
        self.assertIn(".filter((e) => selectedEvent(run, e.seconds))", script)

    def test_preserves_recorder_stop_and_validity_codes(self):
        source = run()
        source["capacity"]["stopReason"] = "generator_limit_threads"
        source["validity"] = {"status": "invalid", "reasons": ["resource_coverage_missing"]}
        data = sanitize({"experiment": "sqlite-capacity-v1", "runs": [source]})
        self.assertEqual(data["runs"][0]["capacity"]["stopReason"], "generator_limit_threads")
        self.assertEqual(data["runs"][0]["validity"]["reasons"], ["resource_coverage_missing"])
        source["validity"] = {
            "status": "invalid",
            "reasons": ["collector_infrastructure", "generator_coverage_missing"],
        }
        data = sanitize({"experiment": "sqlite-capacity-v1", "runs": [source]})
        self.assertEqual(
            data["runs"][0]["validity"]["reasons"],
            ["collector_infrastructure", "generator_coverage_missing"],
        )

    def test_retains_unverified_final_write_qualifier_without_hiding_capture_data(self):
        source = run()
        source["integrity"] = {
            "qualifier": "unavailable_after_workload_boundary",
            "acknowledged": 300,
            "failed": 4,
            "committedUnacknowledged": 2,
            "privateError": "/private/tmp/hidden",
        }
        data = sanitize({"experiment": "sqlite-capacity-v1", "runs": [source]})
        integrity = data["runs"][0]["integrity"]
        self.assertEqual(integrity["status"], "unverified")
        self.assertEqual(integrity["qualifier"], "unavailable_after_workload_boundary")
        self.assertEqual(integrity["committedUnacknowledged"], 2)
        self.assertEqual(len(data["runs"][0]["stages"][0]["windows"]), 1)

    def test_keeps_unequal_step_gaps_and_only_complete_stages_have_csv_rows(self):
        source = run()
        source["stages"].append(
            {
                "stageIndex": 1,
                "targetRps": 600,
                "offsetSeconds": 131,
                "completed": False,
                "overload": {"status": True, "reasons": ["errors"]},
                "normalized": {
                    "windows": [
                        {"targetRps": 600, "stable": True, "startSeconds": 146, "endSeconds": 151}
                    ]
                },
            }
        )
        data = sanitize({"experiment": "sqlite-capacity-v1", "runs": [source]})
        self.assertEqual(data["runs"][0]["stages"][1]["offsetSeconds"], 131)
        self.assertEqual(len(list(csv_rows(data))), 2)

    def test_private_values_are_not_public(self):
        source = run()
        source["generator"] = {"warnings": ["generator_headroom"], "ssh": "192.168.1.1"}
        source["metadata"]["driver"] = "/private/tmp/sqlite"
        data = sanitize({"experiment": "sqlite-capacity-v1", "runs": [source]})
        self.assertEqual(data["runs"][0]["metadata"]["driver"], "local_artifact")
        self.assertNotIn("ssh", data["runs"][0]["generator"])

    def test_allowlists_fixed_vu_and_collector_metadata_as_safe_integers(self):
        source = run()
        source["metadata"].update(
            {
                "measuredVus": 1024,
                "maxVus": 1024,
                "warmupVus": 256,
                "collectorDurationSeconds": 3600,
                "privateHost": "192.168.1.122",
                "badVus": 3.5,
            }
        )
        data = sanitize({"experiment": "sqlite-capacity-v1", "runs": [source]})
        metadata = data["runs"][0]["metadata"]
        self.assertEqual(
            {
                key: metadata[key]
                for key in ("measuredVus", "maxVus", "warmupVus", "collectorDurationSeconds")
            },
            {
                "measuredVus": 1024,
                "maxVus": 1024,
                "warmupVus": 256,
                "collectorDurationSeconds": 3600,
            },
        )
        self.assertNotIn("privateHost", metadata)
        self.assertNotIn("badVus", metadata)

    def test_rejects_non_integer_or_nonfinite_allowlisted_vu_metadata(self):
        source = run()
        source["metadata"].update(
            {
                "measuredVus": 1.5,
                "maxVus": True,
                "warmupVus": "192.168.1.122",
                "collectorDurationSeconds": float("inf"),
            }
        )
        metadata = sanitize({"experiment": "sqlite-capacity-v1", "runs": [source]})["runs"][0][
            "metadata"
        ]
        self.assertEqual(
            {
                key: metadata[key]
                for key in ("measuredVus", "maxVus", "warmupVus", "collectorDurationSeconds")
            },
            {
                "measuredVus": None,
                "maxVus": None,
                "warmupVus": None,
                "collectorDurationSeconds": None,
            },
        )

    def test_aggregate_requires_the_excluded_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for index, (runtime, framework) in enumerate(VARIANTS):
                attempt = f"a{index}"
                payload = run(runtime, framework, attempt)
                path = root / attempt / "result.json"
                path.parent.mkdir()
                path.write_text(json.dumps(payload))
                entries.append(
                    {
                        "runtime": runtime,
                        "framework": framework,
                        "attemptId": attempt,
                        "status": "complete",
                        "image": IMAGE,
                    }
                )
            (root / "campaign-journal.json").write_text(json.dumps({"runs": entries}))
            self.assertEqual(len(aggregate_results(root)["runs"]), 13)

    def test_partial_preview_is_explicit_and_final_aggregate_remains_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = run()
            path = root / "go-nethttp" / "result.json"
            path.parent.mkdir()
            path.write_text(json.dumps(payload))
            (root / "campaign-journal.json").write_text(
                json.dumps(
                    {
                        "runs": [
                            {
                                "runtime": "go",
                                "framework": "nethttp",
                                "attemptId": "go-nethttp",
                                "status": "complete",
                                "image": IMAGE,
                            },
                            {
                                "runtime": "node",
                                "framework": "express",
                                "attemptId": "currently-running",
                                "status": "in_progress",
                                "image": IMAGE,
                            },
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "lacks"):
                aggregate_results(root)
            self.assertEqual(len(aggregate_results(root, allow_partial=True)["runs"]), 1)

    def test_aggregate_rejects_changed_image_or_short_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = run()
            payload["metadata"]["sourceRevision"] = "short"
            path = root / "go-nethttp" / "result.json"
            path.parent.mkdir()
            path.write_text(json.dumps(payload))
            (root / "campaign-journal.json").write_text(
                json.dumps(
                    {
                        "runs": [
                            {
                                "runtime": "go",
                                "framework": "nethttp",
                                "attemptId": "go-nethttp",
                                "status": "complete",
                                "image": "ghcr.io/other@sha256:" + "f" * 64,
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "image"):
                aggregate_results(root, allow_partial=True)
            journal = json.loads((root / "campaign-journal.json").read_text())
            journal["runs"][0]["image"] = IMAGE
            (root / "campaign-journal.json").write_text(json.dumps(journal))
            with self.assertRaisesRegex(ValueError, "immutable"):
                aggregate_results(root, allow_partial=True)
