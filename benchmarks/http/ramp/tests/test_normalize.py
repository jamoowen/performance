import gzip
import json
import tempfile
import unittest

from benchmarks.http.ramp.measure.normalize import normalize_k6, percentile
from benchmarks.http.ramp.measure.schedule import Stage

STAGES = [Stage(2, 0, 5, 2), Stage(4, 2, 5)]


def point(metric, second, value, tags=None):
    return {
        "type": "Point",
        "metric": metric,
        "data": {
            "time": f"1970-01-01T00:00:{second:02d}Z",
            "value": value,
            "tags": tags or {},
        },
    }


def tags(level, phase, **extra):
    return {"level": str(level), "phase": phase, **extra}


def summary(requests, passed, failed, stock=0, drops=0, *, include_optional=True):
    metrics = {
        "http_reqs": {"values": {"count": requests}},
        "request_outcomes": {"values": {"count": requests}},
        "checks": {"values": {"passes": passed, "fails": failed}},
    }
    if include_optional:
        metrics["stock_successes"] = {"values": {"count": stock}}
        metrics["dropped_iterations"] = {"values": {"count": drops}}
    return {"metrics": metrics}


def valid_records():
    records = [point("scenario_origin", 0, 0), {"type": "Metric", "metric": "vus"}]
    # First stage: settling at 0-2, stable at 2-7. Second: transition 7-9, stable 9-14.
    samples = [
        (3, 2, "stable", "success", 1, 200, 10, 4),
        (4, 2, "stable", "http_error", 0, 500, None, None),
        (8, 4, "transition", "success", 1, 200, 20, 8),
        (10, 4, "stable", "validation_error", 0, 200, None, None),
        (11, 4, "stable", "success", 1, 200, 30, 12),
    ]
    for second, level, phase, outcome, checked, status, service, database in samples:
        base = tags(level, phase)
        records.extend(
            [
                point(
                    "http_req_duration", second, second * 10, tags(level, phase, status=str(status))
                ),
                point("request_outcomes", second, 1, tags(level, phase, outcome=outcome)),
                point("checks", second, checked, base),
            ]
        )
        if service is not None:
            records.extend(
                [
                    point("service_duration", second, service, base),
                    point("db_duration", second, database, base),
                ]
            )
    records.append(point("stock_successes", 11, 1, tags(4, "stable")))
    records.append(point("dropped_iterations", 8, 2))
    return records


class NormalizeTests(unittest.TestCase):
    def normalize(self, records=None, result_summary=None, origin=None):
        with tempfile.NamedTemporaryFile(suffix=".gz") as file:
            with gzip.open(file.name, "wt") as output:
                for record in records or valid_records():
                    output.write(json.dumps(record) + "\n")
            return normalize_k6(file.name, result_summary or summary(5, 3, 2, 1, 2), STAGES, origin)

    def test_normalizes_two_stages_and_actual_check_failures(self):
        result = self.normalize(origin=0)
        self.assertEqual(result["validity"], {"status": "valid", "reasons": []})
        first, second = result["windows"]
        self.assertEqual((first["startSeconds"], first["endSeconds"]), (2, 7))
        self.assertEqual((second["startSeconds"], second["endSeconds"]), (9, 14))
        self.assertEqual(
            (first["completed"], first["successful"], first["checksFailed"]), (2, 1, 1)
        )
        self.assertEqual(
            (second["completed"], second["successful"], second["checksFailed"]), (2, 1, 1)
        )
        self.assertEqual(second["client"]["p95Ms"], 109.5)
        self.assertEqual(second["serviceP95Ms"], 30.0)
        self.assertEqual(result["history"][1]["statuses"], {"200": 1})
        self.assertEqual(result["history"][1]["dropped"], 2)
        self.assertEqual(first["slo"]["reasons"], ["schedule_delivery", "goodput", "http_errors"])

    def test_cross_request_summary_invariant(self):
        result = self.normalize(result_summary=summary(4, 3, 2, 1, 2))
        self.assertIn("summary_request_mismatch", result["validity"]["reasons"])

    def test_stock_attempts_and_failures_follow_outcome_tags(self):
        records = valid_records()
        outcomes = [record for record in records if record.get("metric") == "request_outcomes"]
        outcomes[0]["data"]["tags"]["operation"] = "stock"
        outcomes[1]["data"]["tags"]["operation"] = "stock"
        result = self.normalize(records)
        self.assertEqual(result["counts"]["stockAttempts"], 2)
        self.assertEqual(result["counts"]["stockFailures"], 1)

    def test_summary_missing_and_optional_zero_rules(self):
        result = self.normalize(result_summary={"metrics": {}})
        self.assertIn("missing_required_summary", result["validity"]["reasons"])
        records = valid_records()
        records = [
            record
            for record in records
            if record.get("metric") not in {"stock_successes", "dropped_iterations"}
        ]
        result = self.normalize(records, summary(5, 3, 2, include_optional=False))
        self.assertEqual(result["validity"]["status"], "valid")
        result = self.normalize(records, summary(5, 3, 2, stock=1, include_optional=True))
        self.assertIn("summary_stock_mismatch", result["validity"]["reasons"])
        result = self.normalize(records, summary(5, 3, 2, drops=1, include_optional=True))
        self.assertIn("summary_drops_mismatch", result["validity"]["reasons"])

    def test_checks_and_timing_invariants(self):
        result = self.normalize(result_summary=summary(5, 4, 1, 1, 2))
        self.assertIn("summary_checks_mismatch", result["validity"]["reasons"])
        records = [record for record in valid_records() if record.get("metric") != "db_duration"]
        result = self.normalize(records)
        self.assertIn("timing_outcome_mismatch", result["validity"]["reasons"])

    def test_invalid_capture_values_and_shapes(self):
        cases = [
            ([[]], "missing_scenario_origin"),
            (
                [
                    point("scenario_origin", 0, 0),
                    point("http_req_duration", 3, float("nan"), tags(2, "stable", status="200")),
                ],
                "invalid_duration",
            ),
            (
                [
                    point("scenario_origin", 0, 0),
                    point(
                        "http_req_duration",
                        3,
                        1,
                        tags(2, "stable", status="200"),
                    ),
                ],
                "required k6 metrics missing",
            ),
        ]
        for records, reason in cases:
            with self.subTest(reason=reason):
                result = self.normalize(records)
                self.assertEqual(result["validity"]["status"], "invalid")
                self.assertIn(reason, result["validity"]["reasons"])
        records = valid_records()
        records[2]["data"]["time"] = "1970-01-01T00:00:03"
        self.assertIn("invalid_metric_time", self.normalize(records)["validity"]["reasons"])

    def test_origin_required_even_when_supplied_and_outcomes_are_strict(self):
        records = valid_records()[1:]
        self.assertIn(
            "missing_scenario_origin", self.normalize(records, origin=0)["validity"]["reasons"]
        )
        self.assertIn(
            "inconsistent_scenario_origin", self.normalize(origin=1)["validity"]["reasons"]
        )
        records = valid_records()
        next(record for record in records if record.get("metric") == "request_outcomes")["data"][
            "tags"
        ]["outcome"] = "unknown"
        self.assertIn("invalid_metric_tags", self.normalize(records)["validity"]["reasons"])
        records = valid_records()
        next(record for record in records if record.get("metric") == "checks")["data"]["tags"][
            "level"
        ] = "999"
        self.assertIn("invalid_metric_tags", self.normalize(records)["validity"]["reasons"])

    def test_truncated_gzip_is_invalid(self):
        with tempfile.NamedTemporaryFile(suffix=".gz") as file:
            file.write(b"not gzip")
            file.flush()
            result = normalize_k6(file.name, summary(0, 0, 0), STAGES)
        self.assertEqual(result["validity"]["status"], "invalid")
        self.assertIn("truncated or malformed k6 capture", result["validity"]["reasons"])

    def test_exact_percentile(self):
        self.assertEqual(percentile([1, 2, 100], 95), 90.19999999999999)
