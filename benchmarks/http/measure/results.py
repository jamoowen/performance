import json
import math
from pathlib import Path
from urllib.parse import urlparse

from . import cluster

parse_quantity = cluster.parse_quantity
exact_container_metric = cluster.exact_container_metric

SCHEMA_VERSION = 1


def _values(summary, name):
    metric = summary.get("metrics", {}).get(name, {})
    return metric.get("values", {}) if isinstance(metric, dict) else {}


def _trend(values):
    return {key: values.get(key) for key in ("med", "p(95)", "p(99)")}


def normalize_k6(summary):
    if not isinstance(summary, dict) or not isinstance(summary.get("metrics"), dict):
        raise ValueError("k6 summary has no metrics object")
    metrics = summary["metrics"]
    requests = _values(summary, "http_reqs").get("count")
    duration_ms = summary.get("state", {}).get("testRunDurationMs")
    if not _number(requests) or not _number(duration_ms) or duration_ms <= 0:
        raise ValueError("k6 summary is missing HTTP request count or duration")
    if "http_req_duration" not in metrics or "http_req_failed" not in metrics:
        raise ValueError("k6 summary is missing HTTP metrics")
    if not _number(_values(summary, "http_req_failed").get("rate")):
        raise ValueError("k6 summary has no HTTP failure rate")
    checks = _values(summary, "checks").get("rate")
    failures, operations = [], {}
    for name, metric in metrics.items():
        if not isinstance(metric, dict):
            raise ValueError(f"k6 metric {name} is malformed")
        for threshold in metric.get("thresholds", {}).values():
            if not threshold.get("ok", False):
                failures.append(name)
                break
        prefix = "http_req_duration{operation:"
        if name.startswith(prefix) and name.endswith("}"):
            operations[name[len(prefix) : -1]] = metric.get("values", {}).get("p(95)")
    return {
        "requests": requests,
        "achieved_rps": requests * 1000 / duration_ms,
        "error_rate": _values(summary, "http_req_failed").get("rate"),
        "check_failure_rate": 1 - checks if isinstance(checks, (int, float)) else None,
        "dropped_iterations": _values(summary, "dropped_iterations").get("count", 0),
        "latency_ms": _trend(_values(summary, "http_req_duration")),
        "operation_p95_ms": operations,
        "thresholds_failed": sorted(failures),
        "duration_ms": duration_ms,
    }


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def resource_statistics(samples, measurement_start=None, measurement_end=None):
    warnings, filtered = [], []
    for sample in samples:
        timestamp = sample.get("cpu_timestamp_ms")
        if (
            not isinstance(timestamp, (int, float))
            or (measurement_start is not None and timestamp / 1000 < measurement_start)
            or (measurement_end is not None and timestamp / 1000 > measurement_end)
        ):
            continue
        filtered.append(sample)
    points = {
        sample["cpu_timestamp_ms"]: sample
        for sample in filtered
        if sample.get("cpu_seconds") is not None
    }
    cpu, intervals = [points[key] for key in sorted(points)], []
    throttle_numerator = throttle_denominator = 0
    for previous, current in zip(cpu, cpu[1:], strict=False):
        elapsed = (current["cpu_timestamp_ms"] - previous["cpu_timestamp_ms"]) / 1000
        delta = current["cpu_seconds"] - previous["cpu_seconds"]
        if elapsed <= 0:
            continue
        if current.get("pod_uid") != previous.get("pod_uid") or current.get(
            "container_id"
        ) != previous.get("container_id"):
            warnings.append("CPU counter identity changed")
            continue
        if delta < 0:
            warnings.append("CPU counter reset")
            continue
        intervals.append((delta / elapsed, elapsed))
        periods = (current.get("cfs_periods"), previous.get("cfs_periods"))
        throttled = (current.get("cfs_throttled_periods"), previous.get("cfs_throttled_periods"))
        timestamps = (
            previous.get("cfs_periods_timestamp_ms"),
            current.get("cfs_periods_timestamp_ms"),
            previous.get("cfs_throttled_periods_timestamp_ms"),
            current.get("cfs_throttled_periods_timestamp_ms"),
        )
        in_window = (
            all(isinstance(timestamp, (int, float)) for timestamp in timestamps)
            and all(
                measurement_start is None or timestamp / 1000 >= measurement_start
                for timestamp in timestamps
            )
            and all(
                measurement_end is None or timestamp / 1000 <= measurement_end
                for timestamp in timestamps
            )
        )
        if None not in periods + throttled and in_window:
            period_delta, throttle_delta = periods[0] - periods[1], throttled[0] - throttled[1]
            if period_delta < 0 or throttle_delta < 0:
                warnings.append("negative CFS counter delta")
            elif period_delta:
                throttle_numerator += throttle_delta
                throttle_denominator += period_delta
    if len(cpu) < 2 or not intervals:
        warnings.append("insufficient distinct CPU samples")
    total_elapsed = sum(interval[1] for interval in intervals)
    mean_cpu = (
        sum(rate * seconds for rate, seconds in intervals) / total_elapsed
        if total_elapsed
        else None
    )
    memory, rss = [], []
    optional_seen = {"cfs_periods": False, "cfs_throttled_periods": False, "memory_rss": False}
    for sample in samples:
        for value_key, timestamp_key, destination, optional_key in (
            ("memory_working_set_bytes", "memory_working_set_timestamp_ms", memory, None),
            ("memory_rss_bytes", "memory_rss_timestamp_ms", rss, "memory_rss"),
        ):
            value, timestamp = sample.get(value_key), sample.get(timestamp_key)
            if value is None:
                continue
            if not isinstance(timestamp, (int, float)):
                warnings.append(f"missing {timestamp_key}")
                continue
            if (measurement_start is not None and timestamp / 1000 < measurement_start) or (
                measurement_end is not None and timestamp / 1000 > measurement_end
            ):
                continue
            destination.append(value)
            if optional_key:
                optional_seen[optional_key] = True
        for key in ("cfs_periods", "cfs_throttled_periods"):
            if sample.get(key) is not None and isinstance(
                sample.get(f"{key}_timestamp_ms"), (int, float)
            ):
                timestamp = sample[f"{key}_timestamp_ms"] / 1000
                optional_seen[key] |= (
                    measurement_start is None or timestamp >= measurement_start
                ) and (measurement_end is None or timestamp <= measurement_end)
    for key, seen in optional_seen.items():
        if not seen:
            warnings.append(f"optional metric unavailable: {key}")
    if not memory:
        warnings.append("essential metric unavailable: memory_working_set")
    duration = (
        measurement_end - measurement_start
        if measurement_start is not None and measurement_end is not None
        else None
    )
    coverage = (
        (cpu[-1]["cpu_timestamp_ms"] - cpu[0]["cpu_timestamp_ms"]) / 1000 if len(cpu) > 1 else 0
    )
    return {
        "mean_cpu_millicores": mean_cpu * 1000 if mean_cpu is not None else None,
        "max_cpu_millicores": max((rate * 1000 for rate, _ in intervals), default=None),
        "throttled_periods_percent": 100 * throttle_numerator / throttle_denominator
        if throttle_denominator
        else None,
        "max_sampled_working_set_bytes": max(memory, default=None),
        "max_sampled_rss_bytes": max(rss, default=None),
        "sample_count": len(filtered),
        "coverage": {"cpu_span_seconds": coverage, "measurement_seconds": duration},
        "source_window": {
            "first_cpu_timestamp_ms": cpu[0]["cpu_timestamp_ms"] if cpu else None,
            "last_cpu_timestamp_ms": cpu[-1]["cpu_timestamp_ms"] if cpu else None,
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def _normalized_resources(resources):
    return {
        category: tuple(
            sorted((key, _quantity(value)) for key, value in resources.get(category, {}).items())
        )
        for category in ("requests", "limits")
    }


def _quantity(value):
    try:
        return parse_quantity(value)
    except (TypeError, ValueError):
        return value


def compatible_key(record):
    if not isinstance(record, dict) or not isinstance(record.get("metadata"), dict):
        return None
    metadata = record["metadata"]
    settings, cluster = metadata.get("settings", {}), metadata.get("cluster", {})
    if not isinstance(settings, dict) or not isinstance(cluster, dict):
        return None
    workload, node, target = (
        cluster.get("workload", {}),
        cluster.get("node", {}),
        urlparse(metadata.get("base_url", "")),
    )
    names = (
        "profile",
        "workload",
        "rate",
        "duration",
        "warmup_duration",
        "seed_count",
        "preallocated_vus",
        "max_vus",
        "p95_ms",
        "max_error_rate",
        "sample_interval",
    )
    diagnostics_enabled = bool(settings.get("diagnostics", False))
    diagnostics_seconds = settings.get("diagnostics_seconds", 30) if diagnostics_enabled else None
    configuration = workload.get("configuration", {})
    if not isinstance(configuration, dict):
        configuration = {}
    return tuple(settings.get(name) for name in names) + (
        diagnostics_enabled,
        diagnostics_seconds,
        metadata.get("k6_version"),
        metadata.get("load_script_sha256"),
        node.get("uid"),
        node.get("kubelet_version"),
        repr(_normalized_resources(workload.get("resources", {}))),
        str(configuration.get("SEED_COUNT")),
        str(configuration.get("MAX_OPEN_CONNS") or "1"),
        # Empty defaults retain compatibility with records written before diagnostics.
        "1" if str(configuration.get("DIAGNOSTICS", "")).lower() in {"1", "true"} else "",
        str(configuration.get("GOMAXPROCS") or ""),
        str(configuration.get("GOMEMLIMIT") or ""),
        str(configuration.get("GODEBUG") or ""),
        str(configuration.get("BACKEND") or ""),
        str(configuration.get("ROUTER") or ""),
        str(configuration.get("WORKERS") or ""),
        (target.scheme, target.hostname, target.path),
    )


def load_records(results_dir):
    records = []
    for path in sorted(Path(results_dir).rglob("result.json")):
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported result schema")
            record["path"], record["run_id"] = str(path), path.parent.name
        except (OSError, ValueError, json.JSONDecodeError) as error:
            record = {"status": "invalid", "path": str(path), "error": str(error)}
        records.append(record)
    return records


def healthy(record):
    if not isinstance(record, dict) or not isinstance(record.get("result"), dict):
        return False
    resource = record.get("resource") if isinstance(record.get("resource"), dict) else {}
    warnings = (
        resource.get("warnings", []) if isinstance(resource.get("warnings", []), list) else []
    )
    essential = {
        "insufficient distinct CPU samples",
        "CPU counter reset",
        "CPU counter identity changed",
        "essential metric unavailable: memory_working_set",
    }
    return (
        record.get("status") == "complete"
        and not record.get("result", {}).get("thresholds_failed")
        and not essential.intersection(warnings)
    )
