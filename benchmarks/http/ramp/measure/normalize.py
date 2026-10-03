"""Streaming k6 JSON normalization; normalized values form the public report schema."""

from __future__ import annotations

import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterator

from .schedule import Stage, classify

RELEVANT_METRICS = {
    "http_req_duration",
    "request_outcomes",
    "checks",
    "stock_successes",
    "service_duration",
    "db_duration",
    "dropped_iterations",
}
OUTCOMES = {"success", "http_error", "validation_error"}
PHASES = {"stable", "transition", "settling", "drain"}
CAPTURE_ERROR = "truncated or malformed k6 capture"


def percentile(values: list[float], p: int) -> float | None:
    if not values:
        return None
    values.sort()
    index = (len(values) - 1) * p / 100
    low = int(index)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (index - low)


def epoch(value: str) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    result = parsed.timestamp()
    if not math.isfinite(result):
        raise ValueError("timestamp is not finite")
    return result


def points(path: str) -> Iterator[Any]:
    with gzip.open(path, "rt") as source:
        for line in source:
            yield json.loads(line)


def _integer(value: Any, *, positive: bool = False) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
        and value == int(value)
        and (not positive or value > 0)
    )


def _duration(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _summary_count(metrics: dict, name: str, field: str) -> Any:
    metric = metrics.get(name)
    values = metric.get("values") if isinstance(metric, dict) else None
    if isinstance(values, dict):
        return values.get(field)
    return metric.get(field) if isinstance(metric, dict) else None


def _valid_request_tags(tags: dict, metric: str, levels: set[int]) -> bool:
    if (
        not isinstance(tags.get("level"), str)
        or not isinstance(tags.get("phase"), str)
        or tags["phase"] not in PHASES
    ):
        return False
    try:
        level = int(tags["level"])
    except ValueError:
        return False
    if level < 0 or str(level) != tags["level"]:
        return False
    if tags["phase"] == "drain":
        if level != 0:
            return False
    elif level not in levels:
        return False
    if metric == "request_outcomes" and tags.get("outcome") not in OUTCOMES:
        return False
    return metric != "http_req_duration" or isinstance(tags.get("status"), str)


def _result(
    errors: list[str], windows: list, history: list, counts: Counter, origin: float | None
) -> dict:
    return {
        "validity": {"status": "valid" if not errors else "invalid", "reasons": errors},
        "windows": windows,
        "history": history,
        "counts": dict(counts),
        "scenarioOrigin": origin,
    }


def _invalid_capture() -> dict:
    return _result([CAPTURE_ERROR], [], [], Counter(), None)


def _validate_optional_counter(
    errors: list[str], metrics: dict, name: str, raw: int, error: str
) -> None:
    summary_count = _summary_count(metrics, name, "count")
    if summary_count is None:
        if raw:
            errors.append(error)
    elif not _integer(summary_count) or int(summary_count) != raw:
        errors.append(error)


def _window_status(window: dict) -> dict:
    expected = window["expectedArrivals"]
    reasons = []
    if not expected or window["started"] / expected < 0.999:
        reasons.append("schedule_delivery")
    if not expected or window["successful"] / expected < 0.99:
        reasons.append("goodput")
    if window["checksFailed"] / max(1, window["completed"]) > 0.01:
        reasons.append("http_errors")
    if window["client"]["p95Ms"] is None or window["client"]["p95Ms"] > 250:
        reasons.append("latency")
    return {"status": "pass" if not reasons else "fail", "reasons": reasons}


def normalize_k6(
    path: str, summary: dict, stages: list[Stage], origin_seconds: float | None = None
) -> dict:
    errors: list[str] = []
    origins: list[float] = []
    try:
        for record in points(path):
            if not isinstance(record, dict):
                errors.append(CAPTURE_ERROR)
            elif record.get("type") == "Point" and record.get("metric") == "scenario_origin":
                data = record.get("data")
                if not isinstance(data, dict) or not _duration(data.get("value")):
                    errors.append("invalid_scenario_origin")
                else:
                    origins.append(float(data["value"]))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError):
        return _invalid_capture()
    if not origins:
        errors.append("missing_scenario_origin")
    elif max(origins) - min(origins) > 0.001:
        errors.append("inconsistent_scenario_origin")
    capture_origin = origins[0] if origins else None
    if origin_seconds is not None:
        if not _duration(origin_seconds):
            errors.append("invalid_scenario_origin")
        elif capture_origin is not None and abs(float(origin_seconds) - capture_origin) > 0.001:
            errors.append("inconsistent_scenario_origin")
    if capture_origin is None:
        return _result(errors, [], [], Counter(), None)
    origin = (
        float(origin_seconds)
        if origin_seconds is not None and _duration(origin_seconds)
        else capture_origin
    )
    stage_levels = {stage.target_rps for stage in stages}

    windows = {
        str(stage.target_rps): {
            "stage": stage,
            "client": [],
            "service": [],
            "db": [],
            "outcomes": Counter(),
            "checksFailed": 0,
            "drops": 0,
        }
        for stage in stages
    }
    history = defaultdict(
        lambda: {
            "client": [],
            "service": [],
            "db": [],
            "statuses": Counter(),
            "outcomes": Counter(),
            "checksFailed": 0,
            "drops": 0,
        }
    )
    counts: Counter[str] = Counter()
    check_passes = check_fails = 0
    seen: set[str] = set()
    try:
        for record in points(path):
            if not isinstance(record, dict):
                errors.append(CAPTURE_ERROR)
                continue
            if record.get("type") != "Point":
                continue
            metric = record.get("metric")
            # Ignore k6 metadata/system points before allocating a history bucket.
            if metric not in RELEVANT_METRICS:
                continue
            seen.add(metric)
            data = record.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("tags"), dict):
                errors.append("invalid_metric_data")
                continue
            tags, value = data["tags"], data.get("value")
            try:
                seconds = epoch(data.get("time")) - origin
            except (OverflowError, TypeError, ValueError):
                errors.append("invalid_metric_time")
                continue
            if metric == "dropped_iterations":
                if not _integer(value):
                    errors.append("invalid_counter")
                    continue
                bucket = history[int(max(0, seconds) // 5) * 5]
                bucket["drops"] += int(value)
                level, phase = classify(seconds, stages)
                if phase == "stable" and str(level) in windows:
                    windows[str(level)]["drops"] += int(value)
                counts["drops"] += int(value)
                continue
            if not _valid_request_tags(tags, metric, stage_levels):
                errors.append("invalid_metric_tags")
                continue
            bucket = history[int(max(0, seconds) // 5) * 5]
            if metric == "request_outcomes":
                if not _integer(value, positive=True) or int(value) != 1:
                    errors.append("invalid_outcome")
                    continue
                outcome = tags["outcome"]
                counts["request_outcomes"] += 1
                counts[f"outcome:{outcome}"] += 1
                if tags.get("operation") == "stock":
                    counts["stockAttempts"] += 1
                    if outcome != "success":
                        counts["stockFailures"] += 1
                bucket["outcomes"][outcome] += 1
                if tags["phase"] == "stable" and tags["level"] in windows:
                    windows[tags["level"]]["outcomes"][outcome] += 1
            elif metric == "checks":
                if isinstance(value, bool) or value not in {0, 1}:
                    errors.append("invalid_check")
                    continue
                counts["checks"] += 1
                check_passes += int(value)
                check_fails += 1 - int(value)
                bucket["checksFailed"] += 1 - int(value)
                if tags["phase"] == "stable" and tags["level"] in windows:
                    windows[tags["level"]]["checksFailed"] += 1 - int(value)
            elif metric == "stock_successes":
                if not _integer(value):
                    errors.append("invalid_counter")
                else:
                    counts["stockSuccesses"] += int(value)
            else:
                if not _duration(value):
                    errors.append("invalid_duration")
                    continue
                counts[metric] += 1
                key = {
                    "http_req_duration": "client",
                    "service_duration": "service",
                    "db_duration": "db",
                }[metric]
                bucket[key].append(float(value))
                if metric == "http_req_duration":
                    bucket["statuses"][tags["status"]] += 1
                if tags["phase"] == "stable" and tags["level"] in windows:
                    windows[tags["level"]][key].append(float(value))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError):
        errors.append(CAPTURE_ERROR)

    if not {"http_req_duration", "request_outcomes", "checks"}.issubset(seen):
        errors.append("required k6 metrics missing")
    metrics = (
        summary.get("metrics")
        if isinstance(summary, dict) and isinstance(summary.get("metrics"), dict)
        else {}
    )
    if not metrics:
        errors.append("missing_required_summary")
    http_count, outcome_count = (
        _summary_count(metrics, "http_reqs", "count"),
        _summary_count(metrics, "request_outcomes", "count"),
    )
    summary_passes, summary_fails = (
        _summary_count(metrics, "checks", "passes"),
        _summary_count(metrics, "checks", "fails"),
    )
    if not all(
        _integer(value) for value in (http_count, outcome_count, summary_passes, summary_fails)
    ):
        errors.append("missing_required_summary")
    else:
        if (
            len(
                {
                    counts["request_outcomes"],
                    counts["http_req_duration"],
                    int(http_count),
                    int(outcome_count),
                }
            )
            != 1
        ):
            errors.append("summary_request_mismatch")
        if (
            counts["checks"] != counts["request_outcomes"]
            or int(summary_passes) != check_passes
            or int(summary_fails) != check_fails
        ):
            errors.append("summary_checks_mismatch")
    _validate_optional_counter(
        errors, metrics, "stock_successes", counts["stockSuccesses"], "summary_stock_mismatch"
    )
    _validate_optional_counter(
        errors, metrics, "dropped_iterations", counts["drops"], "summary_drops_mismatch"
    )
    if (
        counts["service_duration"] != counts["db_duration"]
        or counts["service_duration"] != counts["outcome:success"]
    ):
        errors.append("timing_outcome_mismatch")

    result_windows = []
    elapsed = 0
    for data in windows.values():
        stage = data["stage"]
        elapsed += stage.transition_seconds + stage.settling_seconds
        start, end = elapsed, elapsed + stage.stable_seconds
        elapsed = end
        client = data["client"]
        window = {
            "targetRps": stage.target_rps,
            "startSeconds": start,
            "endSeconds": end,
            "stable": True,
            "expectedArrivals": stage.expected_arrivals,
            "started": sum(data["outcomes"].values()),
            "completed": len(client),
            "successful": data["outcomes"]["success"],
            "goodputRps": data["outcomes"]["success"] / stage.stable_seconds
            if stage.stable_seconds
            else 0,
            "dropped": data["drops"],
            "httpFailures": data["outcomes"]["http_error"],
            "validationFailures": data["outcomes"]["validation_error"],
            "checksFailed": data["checksFailed"],
            "client": {
                "p50Ms": percentile(client, 50),
                "p95Ms": percentile(client, 95),
                "p99Ms": percentile(client, 99),
            },
            "serviceP95Ms": percentile(data["service"], 95),
            "dbP95Ms": percentile(data["db"], 95),
        }
        status = _window_status(window)
        window["slo"] = {
            "scheduleDelivery": bool(stage.expected_arrivals)
            and window["started"] / stage.expected_arrivals >= 0.999,
            "goodput": bool(stage.expected_arrivals)
            and window["successful"] / stage.expected_arrivals >= 0.99,
            "errors": data["checksFailed"] / max(1, len(client)) <= 0.01,
            "latency": window["client"]["p95Ms"] is not None and window["client"]["p95Ms"] <= 250,
            **status,
        }
        result_windows.append(window)
    result_history = []
    for seconds, data in sorted(history.items()):
        result_history.append(
            {
                "seconds": seconds,
                "bucketSeconds": 5,
                "targetRps": classify(seconds, stages)[0],
                "phase": classify(seconds, stages)[1],
                "completed": len(data["client"]),
                "successful": data["outcomes"]["success"],
                "httpFailures": data["outcomes"]["http_error"],
                "validationFailures": data["outcomes"]["validation_error"],
                "checksFailed": data["checksFailed"],
                "dropped": data["drops"],
                "statuses": dict(data["statuses"]),
                "outcomes": dict(data["outcomes"]),
                "clientP50Ms": percentile(data["client"], 50),
                "clientP95Ms": percentile(data["client"], 95),
                "clientP99Ms": percentile(data["client"], 99),
                "serviceP95Ms": percentile(data["service"], 95),
                "dbP95Ms": percentile(data["db"], 95),
            }
        )
    return _result(errors, result_windows, result_history, counts, origin)
