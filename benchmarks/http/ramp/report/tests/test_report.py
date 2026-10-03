import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.http.ramp.report.report import (
    CAMPAIGN_VARIANTS,
    aggregate_results,
    csv_rows,
    document,
    sanitize,
)


def fixture():
    return {
        "experiment": "sqlite-ramp-v2",
        "generatedAt": "2026-10-03T00:00:00Z",
        "limitations": ["single trial", "accepted Wi-Fi route"],
        "schedule": {
            "hash": "abcdef0",
            "stages": [
                {
                    "targetRps": 300,
                    "stableStartSeconds": 20,
                    "stableEndSeconds": 100,
                    "transitionStartSeconds": 0,
                    "transitionEndSeconds": 20,
                }
            ],
        },
        "runs": [
            {
                "id": "node-express",
                "runtime": "node",
                "framework": "express",
                "validity": {"status": "valid", "reasons": []},
                "build": {
                    "imageDigest": "sha256:" + "a" * 64,
                    "sourceRevision": "abcdef0",
                    "loadHash": "1234567",
                    "scheduleHash": "7654321",
                    "harnessSourceRevision": "fedcba9",
                },
                "metadata": {
                    "runtimeVersion": "v24",
                    "frameworkVersion": "5",
                    "driver": "node:sqlite",
                    "workers": 1,
                    "pragmas": {},
                },
                "generator": {"coverage": 1},
                "resource": {"coverage": 1},
                "windows": [
                    {
                        "targetRps": 300,
                        "stable": True,
                        "startSeconds": 20,
                        "endSeconds": 100,
                        "goodputRps": 299.8,
                        "dropped": 1,
                        "httpFailures": 0,
                        "validationFailures": 0,
                        "checksFailed": 0,
                        "client": {"p95Ms": 10},
                        "serviceP95Ms": 5,
                        "dbP95Ms": 3,
                        "slo": {"status": "fail", "reasons": ["dropped iterations"]},
                        "resource": {"cpuMillicores": 100},
                    }
                ],
                "history": [
                    {
                        "seconds": 5,
                        "targetRps": 300,
                        "phase": "stable",
                        "completed": 300,
                        "successful": 300,
                        "dropped": 1,
                        "statuses": {"200": 300},
                        "clientP95Ms": 10,
                        "serviceP95Ms": 5,
                        "dbP95Ms": 3,
                    }
                ],
            }
        ],
    }


class ReportTests(unittest.TestCase):
    def campaign_result(self, runtime, framework, attempt, *, invalid=False, load_hash="1234567"):
        result = fixture()["runs"][0]
        result.update(
            {
                "id": f"{runtime}-{framework}",
                "runtime": runtime,
                "framework": framework,
                "attemptId": attempt,
            }
        )
        result["metadata"].update({"runtime": runtime, "framework": framework})
        result["build"].update(
            {
                "loadHash": load_hash,
                "scheduleHash": "abcdef0",
                "harnessSourceRevision": "fedcba9",
                "sourceRevision": "abcdef0",
                "imageDigest": "sha256:" + ("a" if runtime == "go" else "b") * 64,
            }
        )
        result["validity"] = {
            "status": "invalid" if invalid else "valid",
            "reasons": ["collector_failure"] if invalid else [],
        }
        if invalid:
            result["windows"] = []
        return result

    def campaign_directory(self, *, mixed=False, invalid_index=14):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        runs = []
        for index, (runtime, framework) in enumerate(CAMPAIGN_VARIANTS):
            attempt = f"attempt-{index}"
            result = self.campaign_result(
                runtime,
                framework,
                attempt,
                invalid=index == invalid_index,
                load_hash="7654321" if mixed and index == 1 else "1234567",
            )
            result_path = root / attempt / "result.json"
            result_path.parent.mkdir()
            result_path.write_text(json.dumps(result))
            runs.append(
                {
                    "runtime": runtime,
                    "framework": framework,
                    "attemptId": attempt,
                    "status": "invalid" if index == invalid_index else "complete",
                    "resultPath": str(result_path),
                }
            )
        (root / "campaign-journal.json").write_text(json.dumps({"runs": runs}))
        return directory

    def test_aggregate_selects_all_latest_results_and_retains_invalid_partial(self):
        directory = self.campaign_directory()
        self.addCleanup(directory.cleanup)
        aggregate = aggregate_results(Path(directory.name))
        self.assertEqual(len(aggregate["runs"]), 15)
        self.assertEqual(aggregate["runs"][-1]["validity"]["status"], "invalid")
        self.assertEqual(len(aggregate["schedule"]["stages"]), 1)

    def test_windowless_invalid_run_is_retained_without_fabricated_csv_rows(self):
        directory = self.campaign_directory(invalid_index=10)
        self.addCleanup(directory.cleanup)
        aggregate = aggregate_results(Path(directory.name))
        phoenix = next(
            run
            for run in aggregate["runs"]
            if run["runtime"] == "elixir" and run["framework"] == "phoenix"
        )
        self.assertEqual(phoenix["validity"]["status"], "invalid")
        self.assertEqual(phoenix["windows"], [])
        self.assertEqual(phoenix["metadata"]["runtime"], "elixir")

        data = sanitize(aggregate)
        retained = next(
            run
            for run in data["runs"]
            if run["runtime"] == "elixir" and run["framework"] == "phoenix"
        )
        self.assertEqual(retained["windows"], [])
        rows = list(csv_rows(data))
        self.assertEqual(len(rows), 14)
        self.assertFalse(
            any(row["runtime"] == "elixir" and row["framework"] == "phoenix" for row in rows)
        )

    def test_aggregate_rejects_mixed_campaign_identity(self):
        directory = self.campaign_directory(mixed=True)
        self.addCleanup(directory.cleanup)
        with self.assertRaisesRegex(ValueError, "mismatched load"):
            aggregate_results(Path(directory.name))

    def test_aggregate_uses_latest_attempt_for_a_variant(self):
        directory = self.campaign_directory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        journal = json.loads((root / "campaign-journal.json").read_text())
        old = self.campaign_result("go", "nethttp", "old-attempt", load_hash="7654321")
        old_path = root / "old-attempt" / "result.json"
        old_path.parent.mkdir()
        old_path.write_text(json.dumps(old))
        journal["runs"].insert(
            0,
            {
                "runtime": "go",
                "framework": "nethttp",
                "attemptId": "old-attempt",
                "status": "complete",
                "resultPath": str(old_path),
            },
        )
        (root / "campaign-journal.json").write_text(json.dumps(journal))
        self.assertEqual(aggregate_results(root)["runs"][0]["attemptId"], "attempt-0")

    def test_aggregate_ignores_campaign_relative_result_path(self):
        directory = self.campaign_directory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        journal_path = root / "campaign-journal.json"
        journal = json.loads(journal_path.read_text())
        journal["runs"][0]["resultPath"] = "results/http/sqlite-ramp/attempt-0/result.json"
        journal_path.write_text(json.dumps(journal))
        self.assertEqual(aggregate_results(root)["runs"][0]["attemptId"], "attempt-0")

    def test_sanitizes_allowlisted_public_fields_and_keeps_slo_distinct(self):
        source = fixture()
        source["runs"][0]["podName"] = "private"
        data = sanitize(source)
        self.assertNotIn("podName", data["runs"][0])
        self.assertEqual(data["runs"][0]["validity"]["status"], "valid")
        self.assertEqual(data["runs"][0]["windows"][0]["slo"]["status"], "fail")
        row = next(csv_rows(data))
        self.assertEqual(row["dropped"], 1)
        self.assertEqual(row["slo_status"], "fail")

    def test_drops_private_unallowlisted_field(self):
        source = fixture()
        source["runs"][0]["generator"] = {"sshHost": "192.168.1.122", "coverage": 1}
        source["runs"][0]["resource"]["samples"] = [
            {"seconds": 1, "cpuMillicores": 2, "podName": "private", "path": "/private/tmp"}
        ]
        source["runs"][0]["validity"]["reasons"] = ["pod replacement at 192.168.1.122"]
        data = sanitize(source)
        self.assertEqual(
            data["runs"][0]["generator"],
            {"coverage": 1, "samplingCoverage": None, "headroomFlag": False, "warnings": []},
        )
        sample = data["runs"][0]["resource"]["samples"][0]
        self.assertEqual(sample["seconds"], 1)
        self.assertEqual(sample["cpuMillicores"], 2)
        self.assertNotIn("podName", sample)
        self.assertNotIn("path", sample)
        self.assertEqual(data["runs"][0]["validity"]["reasons"], ["local_artifact"])

    def test_removes_private_text_at_every_allowed_nesting_level(self):
        source = fixture()
        run = source["runs"][0]
        run["id"] = "node /private/tmp"
        run["framework"] = "express pod-123"
        run["build"]["imageDigest"] = "ssh://192.168.1.122/image"
        run["metadata"]["driver"] = "sqlite at /Users/james.owen"
        run["metadata"]["pragmas"] = {"journal_mode": "WAL", "path": "/private/tmp"}
        run["metadata"]["workerSettings"] = {"workers": 1, "host": "192.168.1.122"}
        run["metadata"]["compileOptions"] = ["THREADSAFE=1", "POD=/private/tmp"]
        run["generator"] = {"coverage": 1, "interface": "ssh://eth0"}
        rendered = json.dumps(sanitize(source))
        self.assertNotIn("192.168.1.122", rendered)
        self.assertNotIn("/private/tmp", rendered)
        self.assertNotIn("/Users/james.owen", rendered)
        self.assertNotIn("pod-123", rendered)

    def test_unknown_schedule_and_status_fields_are_removed(self):
        source = fixture()
        source["schedule"]["path"] = "/private/tmp"
        source["schedule"]["stages"][0]["podName"] = "pod-123"
        source["runs"][0]["history"][0]["statuses"]["oops"] = 1
        data = sanitize(source)
        self.assertNotIn("path", data["schedule"])
        self.assertNotIn("podName", data["schedule"]["stages"][0])
        self.assertEqual(data["runs"][0]["history"][0]["statuses"], {"200": 300})

    def test_known_numeric_fields_reject_private_or_nonfinite_values(self):
        source = fixture()
        run = source["runs"][0]
        source["generatedAt"] = "ssh://192.168.1.122"
        source["schedule"]["hash"] = "/private/tmp"
        run["history"][0]["clientP95Ms"] = "192.168.1.122"
        run["resource"]["samples"] = [
            {
                "seconds": 1,
                "cpuMillicores": {"sshHost": "192.168.1.122"},
                "cpuPressure": {"some": {"avg10": "bad", "total": 10, "path": "/private"}},
            }
        ]
        run["windows"][0]["resource"] = {
            "cpuMillicores": {"sshHost": "192.168.1.122"},
            "pressure": {"some": {"avg10": "bad", "total": 10, "path": "/private"}},
        }
        rendered = json.dumps(sanitize(source))
        self.assertNotIn("192.168.1.122", rendered)
        self.assertNotIn("/private", rendered)
        self.assertNotIn("sshHost", rendered)
        data = sanitize(source)
        self.assertIsNone(data["runs"][0]["history"][0]["clientP95Ms"])
        self.assertIsNone(data["runs"][0]["resource"]["samples"][0]["cpuMillicores"])

    def test_invalid_run_without_windows_is_retained(self):
        source = fixture()
        source["runs"][0]["validity"] = {"status": "invalid", "reasons": ["restart"]}
        source["runs"][0]["windows"] = []
        data = sanitize(source)
        self.assertEqual(data["runs"][0]["validity"], {"status": "invalid", "reasons": ["restart"]})
        self.assertEqual(data["runs"][0]["windows"], [])

    def test_csv_carries_comparison_provenance_and_window_bounds(self):
        source = fixture()
        source["runs"][0]["generator"] = {"coverage": 0.98, "headroomFlag": True}
        row = next(csv_rows(sanitize(source)))
        self.assertEqual(row["source_revision"], "abcdef0")
        self.assertEqual(row["schedule_hash"], "7654321")
        self.assertEqual(row["harness_source_revision"], "fedcba9")
        self.assertEqual(row["window_start_seconds"], 20)
        self.assertEqual(row["window_end_seconds"], 100)
        self.assertEqual(row["generator_coverage"], 0.98)
        self.assertIs(row["generator_headroom_flag"], True)

    def test_csv_preserves_nested_window_coverage_and_zero_window_coverage(self):
        source = fixture()
        window = source["runs"][0]["windows"][0]
        window["resource"]["coverage"] = 0.97
        data = sanitize(source)
        self.assertEqual(data["runs"][0]["windows"][0]["resource"]["coverage"], 0.97)
        self.assertEqual(next(csv_rows(data))["window_coverage"], 0.97)

        window["coverage"] = 0
        self.assertEqual(next(csv_rows(sanitize(source)))["window_coverage"], 0)

    def test_document_has_bounded_filter_and_resource_controls(self):
        rendered = document(sanitize(fixture()), "window.Plotly = {}")
        self.assertIn("Target RPS", rendered)
        self.assertIn("workingSetBytes", rendered)
        self.assertIn("Restore all filters", rendered)
        self.assertIn("CFS throttled seconds / second", rendered)
        self.assertIn("Outcome", rendered)
        self.assertIn("Show graph legends", rendered)
        self.assertIn("outcomeReaders", rendered)
        self.assertIn("seconds >= lastStage.stableEndSeconds", rendered)
        self.assertIn('point.phase === "drain"', rendered)
        self.assertIn('warnings.includes("generator_headroom")', rendered)
        self.assertIn('warning !== "generator_headroom"', rendered)
        self.assertNotIn("sshHost", rendered)


if __name__ == "__main__":
    unittest.main()
