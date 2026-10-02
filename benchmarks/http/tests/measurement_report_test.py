import csv
import json
import math
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from measure import compare


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.articles = 0

    def handle_starttag(self, tag, attrs):
        if tag == "article" and ("class", "run healthy") in attrs:
            self.articles += 1


def record(implementation="go", p95=10, status="complete", thresholds=None, rate=50):
    return {
        "schema_version": 1,
        "status": status,
        "metadata": {
            "implementation": implementation,
            "base_url": f"http://127.0.0.1:{30080 if implementation == 'go' else 30081}",
            "settings": {
                "profile": "steady",
                "workload": "mixed",
                "rate": rate,
                "duration": "30s",
                "warmup_duration": "5s",
                "seed_count": 5000,
                "preallocated_vus": 10,
                "max_vus": 100,
                "p95_ms": 1000,
                "max_error_rate": 0.01,
                "sample_interval": "1s",
            },
            "k6_version": "1.3.0",
            "load_script_sha256": "abc",
            "cluster": {
                "node": {"uid": "node", "kubelet_version": "v1"},
                "workload": {
                    "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                    "configuration": {"MAX_OPEN_CONNS": "1"},
                },
            },
        },
        "result": {
            "latency_ms": {"med": 5, "p(95)": p95, "p(99)": 20},
            "thresholds_failed": thresholds or [],
        },
        "resource": {
            "mean_cpu_millicores": 50,
            "max_sampled_working_set_bytes": 20 * 1024**2,
            "warnings": [],
        },
    }


class ReportTest(unittest.TestCase):
    def test_groups_and_repeat_statistics(self):
        groups, invalid = compare.summarize(
            [record("go", 10), record("go", 30), record("bun", 20), record("go", 100, rate=100)]
        )
        self.assertEqual(len(groups), 2)
        self.assertFalse(invalid)
        go = groups[0]["runtimes"]["go"]["p95_ms"]
        self.assertEqual(go, {"median": 20, "minimum": 10, "maximum": 30})

    def test_missing_values_remain_blank(self):
        item = record()
        del item["resource"]["mean_cpu_millicores"]
        self.assertIsNone(compare.record_row(item)["mean_cpu_millicores"])
        self.assertEqual(compare.display(None), "NA")
        self.assertIsNone(compare.number(math.nan))
        self.assertIsNone(compare.number(math.inf))

    def test_csv_uses_actual_collector_and_after_shape(self):
        item = record()
        item["collector_errors"] = ["collector unavailable"]
        item["resource"]["restart_delta"] = 2
        item["metadata"]["cluster_after"] = {"pressure": {"MemoryPressure": "False"}}
        row = compare.record_row(item)
        self.assertEqual(row["collector_errors"], "collector unavailable")
        self.assertEqual(row["restart_delta"], 2)
        self.assertEqual(row["cluster_after_pressure"], '{"MemoryPressure": "False"}')

    def test_resource_history_skips_cached_cpu_timestamps_and_resets(self):
        samples = [
            {"cpu_timestamp_ms": 1000, "cpu_seconds": 1, "container_id": "a"},
            {"cpu_timestamp_ms": 1000, "cpu_seconds": 2, "container_id": "a"},
            {"cpu_timestamp_ms": 2000, "cpu_seconds": 1.1, "container_id": "a"},
            {"cpu_timestamp_ms": 3000, "cpu_seconds": 0.1, "container_id": "a"},
        ]
        history = compare.cpu_history(samples)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0][0], 2.0)
        self.assertAlmostEqual(history[0][1], 100)

    def test_resource_history_uses_source_bounds_and_actual_memory_keys(self):
        samples = [
            {
                "cpu_timestamp_ms": 99000,
                "cpu_seconds": 1,
                "container_id": "a",
                "pod_uid": "pod",
                "memory_working_set_bytes": 1,
                "memory_working_set_timestamp_ms": 99000,
            },
            {
                "cpu_timestamp_ms": 101000,
                "cpu_seconds": 1.1,
                "container_id": "a",
                "pod_uid": "pod",
                "memory_working_set_bytes": 2 * 1024**2,
                "memory_working_set_timestamp_ms": 101000,
                "memory_rss_bytes": 1024**2,
                "memory_rss_timestamp_ms": 101000,
            },
            {
                "cpu_timestamp_ms": 111000,
                "cpu_seconds": 1.2,
                "container_id": "a",
                "pod_uid": "pod",
                "memory_working_set_bytes": 3 * 1024**2,
                "memory_working_set_timestamp_ms": 111000,
            },
        ]
        self.assertEqual(compare.cpu_history(samples, 100000, 110000), [])
        self.assertEqual(
            compare.memory_history(
                samples,
                "memory_working_set_bytes",
                "memory_working_set_timestamp_ms",
                100000,
                110000,
            ),
            [(1.0, 2.0)],
        )

    def test_failed_runs_visible_but_excluded(self):
        groups, _ = compare.summarize(
            [
                record("go"),
                record("bun", status="invalid"),
                record("bun", thresholds=["http_req_duration"]),
            ]
        )
        summary = groups[0]
        self.assertEqual(summary["awaiting"], [])
        self.assertEqual(summary["all_failed"], ["bun"])
        self.assertIsNone(summary["runtimes"]["bun"]["p95_ms"]["median"])

    def test_malformed_record_and_single_runtime_render(self):
        valid = record()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "go").mkdir()
            (root / "go" / "result.json").write_text(json.dumps(valid))
            (root / "bad").mkdir()
            (root / "bad" / "result.json").write_text("{")
            records = compare.load_records(root)
            records[0]["path"] = str(root / "go" / "result.json")
            output = root / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render(records, output)
            self.assertTrue((output / "comparison.csv").is_file())
            self.assertTrue((output / "comparison.html").is_file())
            self.assertIn("Awaiting bun runs", (output / "comparison.html").read_text())

    def test_null_nested_objects_and_history_data_are_robust(self):
        malformed = {"status": "complete", "metadata": None, "result": None, "resource": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "synthetic"
            run.mkdir()
            valid = record()
            valid["metadata"]["measured_started_at_unix"] = 100
            valid["metadata"]["measured_ended_at_unix"] = 110
            valid["path"] = str(run / "result.json")
            (run / "resources.jsonl").write_text(
                "\n".join(
                    json.dumps(sample)
                    for sample in (
                        {
                            "type": "sample",
                            "cpu_timestamp_ms": 101000,
                            "cpu_seconds": 1,
                            "container_id": "a",
                            "pod_uid": "pod",
                            "memory_working_set_bytes": 2 * 1024**2,
                            "memory_working_set_timestamp_ms": 101000,
                            "memory_rss_bytes": 1024**2,
                            "memory_rss_timestamp_ms": 101000,
                        },
                        {
                            "type": "sample",
                            "cpu_timestamp_ms": 102000,
                            "cpu_seconds": 1.1,
                            "container_id": "a",
                            "pod_uid": "pod",
                            "memory_working_set_bytes": 3 * 1024**2,
                            "memory_working_set_timestamp_ms": 102000,
                            "memory_rss_bytes": 2 * 1024**2,
                            "memory_rss_timestamp_ms": 102000,
                        },
                    )
                )
            )
            samples = compare.history_samples(valid)
            self.assertEqual(len(samples), 2)
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                output = root / "report"
                compare.render([valid, malformed], output)
            self.assertIn("class='run failed'", (output / "comparison.html").read_text())

    def test_report_embeds_safe_chart_data_and_no_image_references(self):
        item = record()
        item["run_id"] = "unsafe</script><img src=x>"
        item["metadata"]["settings"]["profile"] = "<unsafe>"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            document = (output / "comparison.html").read_text()
            self.assertIn("Plotly.newPlot", document)
            self.assertNotIn(".png", document)
            self.assertNotIn(".svg", document)
            self.assertNotIn("</script><img", document)
            self.assertEqual(
                compare.json_for_script("</script><unsafe>&"),
                '"\\u003c/script\\u003e\\u003cunsafe\\u003e\\u0026"',
            )
            self.assertEqual(list(output.glob("*.png")), [])
            self.assertEqual(list(output.glob("*.svg")), [])

    def test_csv_columns_and_na_values_are_preserved(self):
        item = record()
        del item["resource"]["mean_cpu_millicores"]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            with (output / "comparison.csv").open() as file:
                row = next(csv.DictReader(file))
            self.assertEqual(set(row), set(compare.CSV_FIELDS))
            self.assertEqual(row["mean_cpu_millicores"], "")

    def test_visible_error_rate_is_percentage_while_csv_is_fractional(self):
        item = record()
        item["result"]["error_rate"] = 0.025
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            document = (output / "comparison.html").read_text()
            with (output / "comparison.csv").open() as file:
                row = next(csv.DictReader(file))
        self.assertIn("2.5%", document)
        self.assertEqual(row["error_rate"], "0.025")

    def test_visible_cpu_coverage_and_history_figure_units(self):
        item = record()
        item["resource"]["coverage"] = {
            "cpu_span_seconds": 3,
            "measurement_seconds": 10,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "go"
            run.mkdir()
            item["path"] = str(run / "result.json")
            item["metadata"]["measured_started_at_unix"] = 100
            item["metadata"]["measured_ended_at_unix"] = 110
            (run / "resources.jsonl").write_text(
                "\n".join(
                    json.dumps(sample)
                    for sample in (
                        {
                            "type": "sample",
                            "cpu_timestamp_ms": 101000,
                            "cpu_seconds": 1,
                            "container_id": "a",
                            "pod_uid": "pod",
                            "memory_working_set_bytes": 2 * 1024**2,
                            "memory_working_set_timestamp_ms": 101000,
                        },
                        {
                            "type": "sample",
                            "cpu_timestamp_ms": 102000,
                            "cpu_seconds": 1.1,
                            "container_id": "a",
                            "pod_uid": "pod",
                            "memory_working_set_bytes": 3 * 1024**2,
                            "memory_working_set_timestamp_ms": 102000,
                        },
                    )
                )
            )
            figures = compare.history_figures(item, 1)
            output = root / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            document = (output / "comparison.html").read_text()
        by_title = {figure["title"]: figure for figure in figures}
        self.assertIn("3s of 10s", document)
        self.assertEqual(by_title["CPU"]["layout"]["yaxis"]["title"]["text"], "millicores")
        self.assertEqual(by_title["CPU"]["layout"]["yaxis"]["rangemode"], "tozero")
        self.assertIn("millicores", by_title["CPU"]["data"][0]["hovertemplate"])
        self.assertEqual(by_title["Memory"]["layout"]["yaxis"]["title"]["text"], "MiB")
        self.assertEqual(by_title["Memory"]["layout"]["yaxis"]["rangemode"], "tozero")
        self.assertIn("MiB", by_title["Memory"]["data"][0]["hovertemplate"])

    def test_single_run_has_only_source_bound_resource_timelines_in_its_card(self):
        item = record()
        item["run_id"] = "run-one"
        item["metadata"]["measured_started_at_unix"] = 100
        item["metadata"]["measured_ended_at_unix"] = 110
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-one"
            run.mkdir()
            item["path"] = str(run / "result.json")
            (run / "resources.jsonl").write_text(
                "\n".join(
                    json.dumps(sample)
                    for sample in (
                        {
                            "type": "sample",
                            "cpu_timestamp_ms": 101000,
                            "cpu_seconds": 1,
                            "container_id": "a",
                            "pod_uid": "pod",
                            "memory_working_set_bytes": 2 * 1024**2,
                            "memory_working_set_timestamp_ms": 101000,
                            "memory_rss_bytes": 1024**2,
                            "memory_rss_timestamp_ms": 101000,
                        },
                        {
                            "type": "sample",
                            "cpu_timestamp_ms": 102000,
                            "cpu_seconds": 1.1,
                            "container_id": "a",
                            "pod_uid": "pod",
                            "memory_working_set_bytes": 3 * 1024**2,
                            "memory_working_set_timestamp_ms": 102000,
                            "memory_rss_bytes": 2 * 1024**2,
                            "memory_rss_timestamp_ms": 102000,
                        },
                    )
                )
            )
            output = root / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            document = (output / "comparison.html").read_text()
        parser = ArticleParser()
        parser.feed(document)
        chart_json = document.split("window.reportCharts=", 1)[1].split(";</script>", 1)[0]
        charts = json.loads(chart_json)
        article = document.split("<article", 1)[1].split("</article>", 1)[0]
        self.assertEqual(parser.articles, 1)
        self.assertNotIn("group-", document)
        self.assertNotIn('"type":"bar"', chart_json)
        self.assertEqual({chart["id"] for chart in charts}, {"run-1-cpu", "run-1-memory"})
        self.assertIn("run-1-cpu", article)
        self.assertIn("run-1-memory", article)
        self.assertEqual(
            next(chart for chart in charts if chart["id"] == "run-1-cpu")["data"][0]["x"], [2.0]
        )
        self.assertEqual(
            next(chart for chart in charts if chart["id"] == "run-1-memory")["data"][0]["x"],
            [1.0, 2.0],
        )

    def test_matching_aggregates_exclude_failed_runs_and_keep_settings_separate(self):
        go_first = record("go", 10)
        go_second = record("go", 30)
        bun = record("bun", 20)
        failed_bun = record("bun", 40, thresholds=["http_req_duration"])
        different_settings = record("go", 100, rate=100)
        for item in (go_first, go_second, bun, failed_bun, different_settings):
            item["resource"]["mean_cpu_millicores"] = item["result"]["latency_ms"]["p(95)"]
            item["resource"]["max_sampled_working_set_bytes"] = (
                item["result"]["latency_ms"]["p(95)"] * 1024**2
            )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([go_first, go_second, bun, failed_bun, different_settings], output)
            document = (output / "comparison.html").read_text()
        aggregate = document.split("<h2>Matching-run aggregates</h2>", 1)[1]
        self.assertIn("2 healthy / 2 total", aggregate)
        self.assertIn("1 healthy / 2 total", aggregate)
        self.assertIn("20 ms median · 10–30 ms", aggregate)
        self.assertIn("20 millicores median · 10–30 millicores", aggregate)
        self.assertIn("20 MiB median · 10–30 MiB", aggregate)
        self.assertNotIn("100 ms median", aggregate)
        self.assertEqual(document.count("<details class='aggregate'>"), 1)
        self.assertNotIn("Awaiting go runs", document)
        self.assertNotIn("Awaiting bun runs", document)

    def test_list_nested_objects_are_invalid_without_crashing(self):
        item = record()
        item["metadata"]["settings"] = []
        item["metadata"]["cluster"]["pod"] = []
        item["metadata"]["cluster"]["node"] = []
        item["metadata"]["cluster"]["workload"] = []
        row = compare.record_row(item)
        self.assertEqual(row["profile"], None)
        self.assertIn("settings is missing", compare.failure_reasons(item))
        self.assertFalse(compare.is_healthy(item))

    def test_human_failure_reasons_distinguish_http_validation_and_dropped_requests(self):
        item = record("go", p95=900)
        item["result"].update(
            {
                "error_rate": 0,
                "check_failure_rate": 0.02,
                "dropped_iterations": 107,
                "operation_p95_ms": {"list": 1104, "quote": 1015},
                "thresholds_failed": [
                    "http_req_duration{operation:list}",
                    "http_req_duration{operation:quote}",
                    "dropped_iterations",
                ],
            }
        )
        status, reasons, _ = compare.run_status(item)
        self.assertEqual(status, "Failed targets")
        self.assertIn("list p95 1104 ms exceeded configured 1000 ms", reasons)
        self.assertIn("quote p95 1015 ms exceeded configured 1000 ms", reasons)
        self.assertIn("107 scheduled requests were not sent", reasons)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            document = (output / "comparison.html").read_text()
        self.assertIn("HTTP failure rate</dt><dd>0%", document)
        self.assertIn("Validation checks failed</dt><dd>2%", document)
        self.assertIn("Dropped (not sent)</dt><dd>107", document)

    def test_incomplete_or_malformed_records_never_pass_targets(self):
        invalid = {"status": "invalid", "result": {"thresholds_failed": []}, "resource": {}}
        null_result = record()
        null_result["result"] = None
        for item in (invalid, null_result, []):
            status, reasons, css_class = compare.run_status(item)
            self.assertEqual(status, "Recording incomplete")
            self.assertEqual(css_class, "failed")
            self.assertTrue(reasons)
            compare.run_card(item, 1)

    def test_nonessential_capture_warning_does_not_fail_targets(self):
        item = record()
        item["resource"]["warnings"] = ["memory RSS unavailable"]
        self.assertEqual(compare.run_status(item)[0], "Passed targets")

    def test_http_timeline_figures_have_null_gaps_counts_and_secondary_drop_axis(self):
        item = record()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            run.mkdir()
            item["path"] = str(run / "result.json")
            (run / "http-history.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "bucket_seconds": 5,
                        "origin_unix": 100,
                        "status": "complete",
                        "warnings": [],
                        "totals": {
                            "requests": 2000,
                            "http_failures": 2,
                            "validation_failures": 3,
                            "dropped_iterations": 107,
                        },
                        "buckets": [
                            {
                                "start_seconds": 0,
                                "end_seconds": 5,
                                "requests": 1000,
                                "latency_ms": {"med": 4, "p(95)": 9, "p(99)": 12, "max": 20},
                                "statuses": {"200": 998, "500": 1, "0": 1},
                                "http_failures": 2,
                                "validation_failures": 3,
                                "dropped_iterations": 7,
                            },
                            {
                                "start_seconds": 5,
                                "end_seconds": 10,
                                "requests": 0,
                                "latency_ms": {
                                    "med": None,
                                    "p(95)": None,
                                    "p(99)": None,
                                    "max": None,
                                },
                                "statuses": {"unknown": 0},
                                "http_failures": 0,
                                "validation_failures": 0,
                                "dropped_iterations": 100,
                            },
                        ],
                    }
                )
            )
            figures, warnings = compare.http_history_figures(item, 1)
        self.assertFalse(warnings)
        by_title = {figure["title"]: figure for figure in figures}
        latency = by_title["HTTP latency by 5-second window"]
        self.assertEqual(latency["data"][1]["y"], [9, None])
        self.assertFalse(latency["data"][1]["connectgaps"])
        self.assertEqual(latency["data"][1]["customdata"], [[5, 1000], [10, 0]])
        counts = by_title["HTTP responses and unsent requests by 5-second window"]
        self.assertIn("yaxis2", counts["layout"])
        self.assertTrue(counts["layout"]["showlegend"])
        self.assertEqual(counts["layout"]["legend"]["yanchor"], "bottom")
        self.assertGreaterEqual(counts["layout"]["margin"]["t"], 100)
        self.assertTrue(counts["layout"]["yaxis2"]["automargin"])
        dropped = next(trace for trace in counts["data"] if trace["name"] == "Not sent")
        self.assertEqual(dropped["type"], "scatter")
        self.assertEqual(dropped["yaxis"], "y2")
        self.assertEqual(dropped["y"], [7, 100])
        self.assertIn("HTTP 200", {trace["name"] for trace in counts["data"]})
        self.assertIn("No HTTP response", {trace["name"] for trace in counts["data"]})
        invalid = next(trace for trace in counts["data"] if trace["name"] == "Invalid response")
        self.assertIn("Responses failing validation", invalid["hovertemplate"])
        self.assertIn("Scheduled requests not sent", dropped["hovertemplate"])

    def test_http_timeline_partial_missing_and_untrusted_status_are_safe(self):
        item = record()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            run.mkdir()
            item["path"] = str(run / "result.json")
            self.assertEqual(
                compare.http_history(item)[1],
                ["HTTP timeline was not recorded; whole-run totals are available."],
            )
            (run / "http-history.json").write_text("{")
            self.assertIn("could not be read", compare.http_history(item)[1][0])
            (run / "http-history.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "complete",
                        "warnings": None,
                        "totals": {},
                        "buckets": [],
                    }
                )
            )
            self.assertIn("warnings are malformed", compare.http_history(item)[1][0])
            (run / "http-history.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "partial",
                        "warnings": ["tail unavailable"],
                        "totals": {},
                        "buckets": [
                            {
                                "start_seconds": 0,
                                "requests": 1,
                                "latency_ms": {"med": 1},
                                "statuses": {"</script><img src=x>": 1},
                            }
                        ],
                    }
                )
            )
            output = Path(directory) / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            document = (output / "comparison.html").read_text()
        self.assertIn("HTTP timeline is partial", document)
        self.assertIn("tail unavailable", document)
        self.assertNotIn("</script><img", document)
        self.assertIn("\\u003c/script\\u003e", document)

    def test_http_count_chart_keeps_normal_single_status_plot_area(self):
        item = record()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            run.mkdir()
            item["path"] = str(run / "result.json")
            (run / "http-history.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "complete",
                        "warnings": [],
                        "totals": {},
                        "buckets": [
                            {
                                "start_seconds": 0,
                                "end_seconds": 5,
                                "requests": 10,
                                "latency_ms": {"med": 1},
                                "statuses": {"200": 10},
                            }
                        ],
                    }
                )
            )
            figures, _ = compare.http_history_figures(item, 1)
        counts = next(
            figure
            for figure in figures
            if figure["title"] == "HTTP responses and unsent requests by 5-second window"
        )
        self.assertEqual(counts["layout"]["margin"]["t"], 50)
        self.assertEqual(counts["layout"]["margin"]["r"], 20)
        self.assertNotIn("yaxis2", counts["layout"])

    def test_capture_note_is_only_shown_when_capture_metadata_exists(self):
        item = record()
        item["metadata"]["http_capture"] = {
            "format": "k6-json-gzip",
            "phase": "measurement",
            "source": "K6_OUT",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            document = (output / "comparison.html").read_text()
        self.assertIn("adds local Mac I/O", document)
        self.assertIn("Legacy Go runs did not export", document)

    def test_partial_capture_metadata_warns_without_failing_the_benchmark(self):
        item = record()
        item["metadata"]["http_capture"] = {
            "status": "partial",
            "warnings": ["writer stopped before final flush"],
        }
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            run.mkdir()
            item["path"] = str(run / "result.json")
            self.assertEqual(compare.run_status(item)[0], "Passed targets")
            output = Path(directory) / "report"
            with patch.object(
                compare, "plotly_javascript", return_value="window.Plotly={newPlot(){}}"
            ):
                compare.render([item], output)
            document = (output / "comparison.html").read_text()
        self.assertIn("HTTP timeline is unavailable", document)
        self.assertIn("HTTP capture: writer stopped before final flush", document)
        self.assertIn("Passed targets", document)


if __name__ == "__main__":
    unittest.main()
