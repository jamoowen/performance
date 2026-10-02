"""Build compact HTTP completion histories from k6 JSON output."""

import argparse
import gzip
import json
import math
import zlib
from datetime import datetime
from pathlib import Path

BUCKET_SECONDS = 5
METRICS = {
    "http_req_duration",
    "http_req_failed",
    "operation_failures",
    "dropped_iterations",
}


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("point has no RFC3339 timestamp")
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    if "." in value:
        prefix, suffix = value.split(".", 1)
        timezone_index = max(suffix.rfind("+"), suffix.rfind("-"))
        fraction = suffix if timezone_index < 0 else suffix[:timezone_index]
        timezone = "" if timezone_index < 0 else suffix[timezone_index:]
        value = f"{prefix}.{fraction[:6]}{timezone}"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("point timestamp has no timezone")
    return parsed.timestamp()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _quantile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _bucket(start, end):
    return {
        "start_seconds": start,
        "end_seconds": end,
        "requests": 0,
        "latency_ms": {"med": None, "p(95)": None, "p(99)": None, "max": None},
        "statuses": {},
        "http_failures": 0,
        "validation_failures": 0,
        "dropped_iterations": 0,
        "_latencies": [],
    }


def _finalize(bucket):
    latencies = bucket.pop("_latencies")
    bucket["latency_ms"] = {
        "med": _quantile(latencies, 0.5),
        "p(95)": _quantile(latencies, 0.95),
        "p(99)": _quantile(latencies, 0.99),
        "max": max(latencies) if latencies else None,
    }
    return bucket


def _metric_point(record):
    if not isinstance(record, dict):
        raise ValueError("JSON record is not an object")
    if record.get("type") != "Point" or record.get("metric") not in METRICS:
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        raise ValueError("metric point has no data object")
    timestamp = _timestamp(data.get("time"))
    value = data.get("value")
    if not _number(value):
        raise ValueError("metric point has a non-finite value")
    return record["metric"], timestamp, value, data.get("tags", {})


def _validated_value(metric, value):
    if metric == "http_req_duration":
        if value < 0:
            raise ValueError("duration point is negative")
    elif metric == "http_req_failed":
        if value not in (0, 1):
            raise ValueError("HTTP failure point is not 0 or 1")
    elif metric == "operation_failures":
        if value not in (0, 1):
            raise ValueError("validation failure point is not 0 or 1")
    elif value < 0 or value != int(value):
        raise ValueError("dropped iteration point is not a nonnegative integer")
    return value


def _totals(buckets):
    return {
        "requests": sum(bucket["requests"] for bucket in buckets),
        "http_failures": sum(bucket["http_failures"] for bucket in buckets),
        "validation_failures": sum(bucket["validation_failures"] for bucket in buckets),
        "dropped_iterations": sum(bucket["dropped_iterations"] for bucket in buckets),
    }


def build_history(raw_path, measured_start, measured_end, result):
    """Return a compact history, or ``None`` when a legacy raw capture is absent."""
    raw_path = Path(raw_path)
    if not raw_path.exists():
        return None, {"status": "unavailable", "warnings": ["raw HTTP capture is unavailable"]}
    if not _number(measured_start) or not _number(measured_end) or measured_end < measured_start:
        raise ValueError("measurement bounds are invalid")

    warnings, buckets = [], {}
    observations = {"http_req_failed": 0, "operation_failures": 0}
    partial, last_observed = False, None
    bucket_count = max(1, math.ceil((measured_end - measured_start) / BUCKET_SECONDS))
    try:
        with gzip.open(raw_path, "rt", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                try:
                    record = json.loads(line)
                    point = _metric_point(record)
                    if point is None:
                        continue
                    metric, timestamp, value, tags = point
                    if timestamp < measured_start or timestamp > measured_end:
                        continue
                    _validated_value(metric, value)
                    index = min(
                        int((timestamp - measured_start) // BUCKET_SECONDS), bucket_count - 1
                    )
                    bucket = buckets.setdefault(
                        index,
                        _bucket(
                            index * BUCKET_SECONDS,
                            min((index + 1) * BUCKET_SECONDS, measured_end - measured_start),
                        ),
                    )
                    last_observed = (
                        timestamp if last_observed is None else max(last_observed, timestamp)
                    )
                    if metric == "http_req_duration":
                        bucket["requests"] += 1
                        bucket["_latencies"].append(value)
                        status = tags.get("status") if isinstance(tags, dict) else None
                        status = str(status) if status is not None else "unknown"
                        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
                    elif metric == "http_req_failed":
                        observations[metric] += 1
                        bucket["http_failures"] += int(value)
                    elif metric == "operation_failures":
                        observations[metric] += 1
                        bucket["validation_failures"] += int(value)
                    else:
                        bucket["dropped_iterations"] += int(value)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    partial = True
                    warnings.append(f"line {line_number}: {error}")
    except (EOFError, gzip.BadGzipFile, OSError, UnicodeDecodeError, zlib.error) as error:
        partial = True
        warnings.append(f"raw HTTP capture: {error}")

    if not buckets:
        partial = True
        warnings.append("raw HTTP capture contains no measured metric points")
    observed_totals = _totals(buckets.values())
    if isinstance(result, dict):
        expected_requests = result.get("requests")
        if _number(expected_requests):
            if observed_totals["requests"] != expected_requests:
                partial = True
                warnings.append(
                    f"requests total {observed_totals['requests']} differs from k6 summary {expected_requests}"
                )
            for metric, label in (
                ("http_req_failed", "HTTP failure"),
                ("operation_failures", "validation failure"),
            ):
                if observations[metric] != expected_requests:
                    partial = True
                    warnings.append(
                        f"{label} point count {observations[metric]} differs from request total {expected_requests}"
                    )
        expected_dropped = result.get("dropped_iterations")
        if _number(expected_dropped) and observed_totals["dropped_iterations"] != expected_dropped:
            partial = True
            warnings.append(
                f"dropped_iterations total {observed_totals['dropped_iterations']} differs from k6 summary {expected_dropped}"
            )
    if partial:
        if last_observed is None:
            retained = []
        else:
            last_index = min(
                int((last_observed - measured_start) // BUCKET_SECONDS), bucket_count - 1
            )
            retained = range(last_index + 1)
    else:
        retained = range(bucket_count)
    final_buckets = [
        _finalize(
            buckets.get(
                index,
                _bucket(
                    index * BUCKET_SECONDS,
                    min((index + 1) * BUCKET_SECONDS, measured_end - measured_start),
                ),
            )
        )
        for index in retained
    ]
    totals = _totals(final_buckets)
    return {
        "schema_version": 1,
        "bucket_seconds": BUCKET_SECONDS,
        "origin_unix": measured_start,
        "status": "partial" if partial else "complete",
        "warnings": warnings,
        "totals": totals,
        "buckets": final_buckets,
    }, {"status": "partial" if partial else "complete", "warnings": warnings}


def write_history(run_dir, result=None, metadata=None):
    run_dir = Path(run_dir)
    if metadata is None:
        metadata = json.loads((run_dir / "metadata.json").read_text())
    if result is None:
        payload = json.loads((run_dir / "result.json").read_text())
        result = payload.get("result")
    history, capture = build_history(
        run_dir / "http-metrics.json.gz",
        metadata.get("measured_started_at_unix"),
        metadata.get("measured_ended_at_unix"),
        result,
    )
    if history is not None:
        (run_dir / "http-history.json").write_text(
            json.dumps(history, indent=2, sort_keys=True) + "\n"
        )
    return history, capture


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build HTTP history from a recorded k6 run")
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        history, capture = write_history(args.run_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if history is None:
        parser.error("raw HTTP capture is unavailable for this legacy run")
    print(args.run_dir / "http-history.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
