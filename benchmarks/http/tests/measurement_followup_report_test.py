import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from measure import compare


def record(runtime, variant, experiment="scheduling"):
    return {
        "schema_version": 1,
        "status": "complete",
        "metadata": {
            "implementation": runtime,
            "experiment": experiment,
            "variant": variant,
            "repetition": 1,
            "base_url": "http://host",
            "settings": {
                "rate": 600,
                "profile": "steady",
                "workload": "mixed",
                "duration": "2m",
                "warmup_duration": "60s",
                "seed_count": 5000,
                "preallocated_vus": 1,
                "max_vus": 1,
                "p95_ms": 1000,
                "max_error_rate": 0.01,
                "sample_interval": 5,
                "diagnostics": False,
            },
            "cluster": {
                "workload": {
                    "configuration": {
                        "BACKEND": "sqlite",
                        "ROUTER": "axum" if runtime == "rust" else "stdlib",
                        "WORKERS": "1",
                    },
                    "resources": {
                        "requests": {"cpu": "1", "memory": "512Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                },
                "node": {},
                "pod": {},
            },
        },
        "result": {"thresholds_failed": [], "latency_ms": {"p(95)": 1}},
        "resource": {"warnings": []},
    }


class FollowupReportTest(unittest.TestCase):
    def test_rust_is_valid_and_variant_styles_are_distinct(self):
        rust = record("rust", "Axum")
        self.assertEqual(compare.validation_errors(rust), [])
        overlay = compare.run_overlay(
            [record("go", "GOMAXPROCS=1"), record("go", "GOMAXPROCS=2"), rust]
        )
        self.assertEqual(
            {run["variant"] for run in overlay["runs"]}, {"GOMAXPROCS=1", "GOMAXPROCS=2", "Axum"}
        )
        self.assertEqual(
            {run["color"] for run in overlay["runs"] if run["runtime"] == "rust"}, {"#3c9d68"}
        )
        go = [run for run in overlay["runs"] if run["runtime"] == "go"]
        self.assertEqual({run["dash"] for run in go}, {"solid"})
        self.assertEqual(len({run["color"] for run in go}), 2)

    def test_scheduling_report_does_not_await_bun(self):
        with (
            TemporaryDirectory() as temporary,
            patch.object(compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"),
        ):
            compare.render(
                [record("go", "GOMAXPROCS=1"), record("go", "GOMAXPROCS=2")], Path(temporary)
            )
            output = (Path(temporary) / "comparison.html").read_text()
        self.assertNotIn("Awaiting bun", output)
        self.assertIn("data-overlay-variant", output)

    def test_variant_changes_group_label_and_keeps_repetitions_separate_from_variants(self):
        one, two = record("go", "GOMAXPROCS=1"), record("go", "GOMAXPROCS=2")
        one["metadata"]["cluster"]["workload"]["configuration"]["GOMAXPROCS"] = "1"
        two["metadata"]["cluster"]["workload"]["configuration"]["GOMAXPROCS"] = "2"
        self.assertNotEqual(compare.group_label(one), compare.group_label(two))
        self.assertNotEqual(compare.group_key(one), compare.group_key(two))


if __name__ == "__main__":
    unittest.main()
