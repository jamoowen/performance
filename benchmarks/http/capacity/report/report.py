"""Render the public, adaptive SQLite capacity-search report.

Raw result directories intentionally remain private.  This module accepts only
the measured campaign schema and emits a small allowlisted data model for the
standalone HTML report and CSV export.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parent
VARIANTS = (
    ("go", "nethttp"),
    ("go", "chi"),
    ("go", "fiber"),
    ("node", "express"),
    ("node", "nest"),
    ("node", "fastify"),
    ("bun", "native"),
    ("bun", "hono"),
    ("bun", "elysia"),
    ("rust", "axum"),
    ("rust", "actix"),
    ("python", "fastapi"),
    ("elixir", "phoenix"),
)
SAFE_REASONS = {
    "errors",
    "drops",
    "sustained_overload",
    "pod_restart_or_oom",
    "tested_safety_ceiling",
    "k6_failed",
    "partial_k6_capture",
    "incomplete_capture",
    "integrity_unavailable",
    "warmup_invalid",
    "capture_invalid",
    "warmup_or_recorder_failure",
    "database_not_fresh",
    "oom",
    "restart",
    "latency",
    "collector_failure",
    "recorder_failure",
    "generator_limited",
    "generator_limit_disk_start",
    "generator_limit_memory_step",
    "generator_limit_nofile",
    "generator_limit_threads",
    "generator_limit_memory",
    "generator_limit_cpu",
    "generator_limit_disk",
    "capture_truncated",
    "identity_drift",
    "disk_guard",
    "memory_guard",
    "thread_guard",
    "host_cpu_guard",
    "pod_restart",
    "pod_identity_changed",
    "pod_cgroup_uid_mismatch",
    "node_cpu_missing",
    "collector_timeout",
    "remote_failure",
    "missing_remote_end",
    "invalid_remote_json",
    "invalid_remote_pid",
    "integrity_mismatch",
    "resource_coverage_missing",
    "generator_coverage_missing",
    "generator_collector_failure",
    "collector_infrastructure",
    "oom_or_restart",
    "remote_collector_initialization_failed",
}
SAFE_EVENTS = {"oom", "restart", "container_missing", "collector_gap"}
SAFE_INTEGRITY_STATUS = {"verified", "unverified", "mismatch"}
SAFE_INTEGRITY_QUALIFIERS = {"unavailable_after_workload_boundary"}
SAFE_WARNINGS = {
    "host_cpu_headroom",
    "host_memory_headroom",
    "swap_growth",
    "generator_headroom",
    "thread_guard",
    "memory_guard",
    "disk_guard",
}
SAFE_PHASES = {"transition", "settling", "stable", "drain"}
SAFE_TEXT = re.compile(r"^[A-Za-z0-9 .,:_+@/=()\-]{1,160}$")
PRIVATE = re.compile(
    r"(?:\b(?:\d{1,3}\.){3}\d{1,3}\b|/users/|/private/|/tmp/|\\\\|ssh://|\bpod[-_/ ])", re.I
)
SHA = re.compile(r"^(?:sha256:)?[a-f0-9]{7,64}$")
SHA40 = re.compile(r"^[a-f0-9]{40}$")
SHA64 = re.compile(r"^[a-f0-9]{64}$")


def _num(value):
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _bounded_integer(value):
    value = _integer(value)
    return value if value is not None and value <= 1_000_000 else None


def _text(value):
    return (
        value
        if isinstance(value, str) and SAFE_TEXT.fullmatch(value) and not PRIVATE.search(value)
        else "local_artifact"
    )


def _reasons(values):
    return [_reason(value) for value in values] if isinstance(values, list) else []


def _reason(value):
    if value in SAFE_REASONS:
        return value
    return (
        "collector_gap"
        if isinstance(value, str) and value.startswith("collector_gap:")
        else "local_artifact"
    )


def _numbers(source, fields):
    source = source if isinstance(source, dict) else {}
    return {field: _num(source.get(field)) for field in fields}


def _clean_build(metadata):
    source = metadata if isinstance(metadata, dict) else {}
    aliases = {"imageDigest": "image", "scheduleHash": "protocolHash"}
    result = {}
    for output, input_name in {
        **{
            key: key
            for key in ("sourceRevision", "harnessSourceRevision", "loadHash", "protocolHash")
        },
        **aliases,
    }.items():
        value = source.get(input_name, source.get(output))
        if output == "imageDigest" and isinstance(value, str) and "@sha256:" in value:
            value = value.rsplit("@", 1)[1]
        result[output] = (
            value
            if isinstance(value, str) and SHA.fullmatch(value.removeprefix("sha256:"))
            else "local_artifact"
        )
    return result


def _clean_metadata(source):
    source = source if isinstance(source, dict) else {}
    simple = (
        "runtimeVersion",
        "frameworkVersion",
        "driver",
        "driverVersion",
        "sqliteVersion",
        "workers",
    )
    result = {
        key: _text(source[key]) if isinstance(source.get(key), str) else _num(source.get(key))
        for key in simple
        if key in source
    }
    for key in ("measuredVus", "maxVus", "warmupVus", "collectorDurationSeconds"):
        result[key] = _bounded_integer(source.get(key))
    result["pragmas"] = {
        key: _text(value) if isinstance(value, str) else _num(value)
        for key, value in source.get("pragmas", {}).items()
        if key
        in {
            "journal_mode",
            "synchronous",
            "foreign_keys",
            "busy_timeout",
            "cache_size",
            "wal_autocheckpoint",
            "temp_store",
        }
        and isinstance(value, (str, int, float))
    }
    result["workerSettings"] = {
        key: _text(value) if isinstance(value, str) else _num(value)
        for key, value in source.get("workerSettings", {}).items()
        if key
        in {
            "workers",
            "goMaxProcs",
            "uvicornWorkers",
            "beamSchedulers",
            "beamDirtyCpuSchedulers",
            "beamDirtyIoSchedulers",
            "nodeClusterWorkers",
            "bunWorkers",
            "rustExecutorWorkers",
        }
        and isinstance(value, (str, int, float))
    }
    return result


def _clean_window(source):
    source = source if isinstance(source, dict) else {}
    result = _numbers(
        source,
        {
            "targetRps",
            "startSeconds",
            "endSeconds",
            "expectedArrivals",
            "started",
            "completed",
            "successful",
            "goodputRps",
            "dropped",
            "httpFailures",
            "validationFailures",
            "checksFailed",
            "serviceP95Ms",
            "dbP95Ms",
            "coverage",
        },
    )
    result["stable"] = source.get("stable") is True
    result["client"] = _numbers(source.get("client"), {"p50Ms", "p95Ms", "p99Ms"})
    result["resource"] = _numbers(
        source.get("resource"),
        {
            "cpuMillicores",
            "workingSetBytes",
            "memoryCurrentBytes",
            "memoryPeakBytes",
            "cfsPeriodRatio",
            "throttledSeconds",
            "coverage",
        },
    )
    return result


def _clean_history(source):
    source = source if isinstance(source, dict) else {}
    result = _numbers(
        source,
        {
            "seconds",
            "bucketSeconds",
            "targetRps",
            "completed",
            "successful",
            "dropped",
            "httpFailures",
            "validationFailures",
            "checksFailed",
            "clientP50Ms",
            "clientP95Ms",
            "clientP99Ms",
            "serviceP95Ms",
            "dbP95Ms",
        },
    )
    result["phase"] = source.get("phase") if source.get("phase") in SAFE_PHASES else None
    statuses = source.get("statuses", {})
    result["statuses"] = (
        {
            str(key): _num(value)
            for key, value in statuses.items()
            if str(key).isdigit() and _num(value) is not None
        }
        if isinstance(statuses, dict)
        else {}
    )
    return result


def _clean_samples(source, *, container=False):
    fields = {
        "seconds",
        "intervalStartSeconds",
        "intervalEndSeconds",
        "cpuMillicores",
        "workingSetBytes",
        "memoryCurrentBytes",
        "memoryPeakBytes",
        "throttledSeconds",
        "cfsPeriods",
        "cfsThrottledPeriods",
        "cfsPeriodRatio",
    }
    if not isinstance(source, list):
        return []
    result = []
    for item in source:
        if not isinstance(item, dict):
            continue
        cleaned = _numbers(item, fields)
        if container:
            # Telemetry derives this before sanitization, without exposing an ID.
            cleaned["segment"] = _num(item.get("containerSegment"))
        result.append(cleaned)
    return result


def _clean_events(source):
    output = []
    for event in source if isinstance(source, list) else []:
        if not isinstance(event, dict) or event.get("type") not in SAFE_EVENTS:
            continue
        item = _numbers(event, {"seconds"})
        item["type"] = event["type"]
        item["reason"] = _reason(event.get("reason"))
        if item["reason"] == "local_artifact":
            item["reason"] = event["type"]
        output.append(item)
    return output


def _clean_stage(source):
    source = source if isinstance(source, dict) else {}
    normalized = source.get("normalized", {}) if isinstance(source.get("normalized"), dict) else {}
    overload = source.get("overload", {}) if isinstance(source.get("overload"), dict) else {}
    return {
        "stageIndex": _num(source.get("stageIndex")),
        "targetRps": _num(source.get("targetRps")),
        "offsetSeconds": _num(source.get("offsetSeconds")),
        "completed": source.get("completed") is True,
        "overload": {
            "status": overload.get("status") is True,
            "reasons": _reasons(overload.get("reasons")),
        },
        "windows": [
            _clean_window(item)
            for item in normalized.get("windows", source.get("windows", []))
            if isinstance(item, dict)
        ],
        "history": [
            _clean_history(item)
            for item in normalized.get("history", source.get("history", []))
            if isinstance(item, dict)
        ],
    }


def _clean_integrity(source):
    source = source if isinstance(source, dict) else {}
    qualifier = source.get("qualifier")
    before, after = source.get("before"), source.get("after")
    counts = {
        key: _integer(source.get(key))
        for key in ("acknowledged", "failed", "committedUnacknowledged")
    }
    has_snapshots = "before" in source or "after" in source
    if qualifier in SAFE_INTEGRITY_QUALIFIERS:
        status = "unverified"
    elif has_snapshots:
        status = _snapshot_integrity_status(
            before,
            after,
            counts,
            committed_count_present="committedUnacknowledged" in source,
        )
    else:
        # Older, already-sanitized fixtures can carry a status without snapshots.
        status = source.get("status") if source.get("status") in SAFE_INTEGRITY_STATUS else None
    return {
        "status": status,
        "qualifier": qualifier if qualifier in SAFE_INTEGRITY_QUALIFIERS else None,
        **counts,
    }


def _snapshot_integrity_status(before, after, counts, *, committed_count_present):
    """Translate the capacity recorder's before/after proof to public status."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "unverified"
    fields = ("rows", "totalRevisions", "totalStock")
    clean_before = {field: _integer(before.get(field)) for field in fields}
    clean_after = {field: _integer(after.get(field)) for field in fields}
    # The recorder omits the derived count when its snapshot validation has
    # already found a mismatch.  A supplied malformed derived count leaves
    # that proof incomplete; an omitted one is computed below.
    if any(value is None for value in (*clean_before.values(), *clean_after.values())):
        return "unverified"
    if counts["acknowledged"] is None or counts["failed"] is None:
        return "unverified"
    if committed_count_present and counts["committedUnacknowledged"] is None:
        return "unverified"
    if clean_before["rows"] != 5000 or clean_before["totalRevisions"] != 0:
        return "mismatch"
    revisions = clean_after["totalRevisions"]
    stock_delta = clean_after["totalStock"] - clean_before["totalStock"]
    excess = revisions - counts["acknowledged"]
    if (
        clean_after["rows"] != 5000
        or stock_delta != revisions
        or revisions < counts["acknowledged"]
        or excess > counts["failed"]
        or (committed_count_present and counts["committedUnacknowledged"] != excess)
    ):
        return "mismatch"
    return "verified"


def _clean_run(source, index):
    source = source if isinstance(source, dict) else {}
    metadata = source.get("metadata", {}) if isinstance(source.get("metadata"), dict) else {}
    runtime, framework = (
        metadata.get("runtime", source.get("runtime")),
        metadata.get("framework", source.get("framework")),
    )
    if not isinstance(runtime, str) or not isinstance(framework, str):
        raise ValueError(f"run {index} lacks runtime/framework")
    validity = source.get("validity", {}) if isinstance(source.get("validity"), dict) else {}
    status = validity.get("status")
    if status not in {"valid", "invalid"}:
        raise ValueError(f"run {index} has invalid capture status")
    stages = [_clean_stage(item) for item in source.get("stages", []) if isinstance(item, dict)]
    capacity = source.get("capacity", {}) if isinstance(source.get("capacity"), dict) else {}
    generator = source.get("generator", {}) if isinstance(source.get("generator"), dict) else {}
    resource = source.get("resource", {}) if isinstance(source.get("resource"), dict) else {}
    return {
        "id": _text(source.get("attemptId", f"{runtime}-{framework}")),
        "runtime": _text(runtime),
        "framework": _text(framework),
        "validity": {"status": status, "reasons": _reasons(validity.get("reasons"))},
        "build": _clean_build(metadata),
        "metadata": _clean_metadata(metadata),
        "capacity": {
            key: _num(capacity.get(key))
            for key in (
                "highestPassingRps",
                "highestNoOverloadRps",
                "firstOverloadRps",
                "firstLatencyFailureRps",
            )
        }
        | {
            "stopReason": capacity.get("stopReason")
            if capacity.get("stopReason") in SAFE_REASONS
            else None,
            "generatorLimited": capacity.get("generatorLimited") is True,
        },
        "generator": {
            "coverage": _num(generator.get("coverage")),
            "headroomFlag": generator.get("headroomFlag") is True,
            "peakThreads": _num(generator.get("peakThreads")),
            "warnings": [item for item in generator.get("warnings", []) if item in SAFE_WARNINGS]
            if isinstance(generator.get("warnings"), list)
            else [],
        },
        "integrity": _clean_integrity(source.get("integrity")),
        "resource": {
            "scope": "pod",
            "coverage": _num(resource.get("coverage")),
            "samples": _clean_samples(resource.get("samples")),
            "containerSamples": _clean_samples(resource.get("containerSamples"), container=True),
            "events": _clean_events(resource.get("events")),
        },
        "schedule": {
            "hash": _clean_build(metadata)["protocolHash"],
            "stages": [
                _numbers(
                    item,
                    {
                        "targetRps",
                        "transitionSeconds",
                        "stableSeconds",
                        "settlingSeconds",
                        "offsetSeconds",
                        "stableStartSeconds",
                        "stableEndSeconds",
                    },
                )
                for item in source.get("schedule", {}).get("stages", [])
                if isinstance(item, dict)
            ]
            if isinstance(source.get("schedule"), dict)
            else [],
        },
        "stages": stages,
        "windows": [
            _clean_window(item) for item in source.get("windows", []) if isinstance(item, dict)
        ],
        "history": [
            _clean_history(item) for item in source.get("history", []) if isinstance(item, dict)
        ],
    }


def sanitize(source):
    if (
        not isinstance(source, dict)
        or source.get("experiment") != "sqlite-capacity-v1"
        or not isinstance(source.get("runs"), list)
    ):
        raise ValueError("expected sqlite-capacity-v1 results")
    return {
        "experiment": "sqlite-capacity-v1",
        "generatedAt": source.get("generatedAt")
        if isinstance(source.get("generatedAt"), str)
        else None,
        "limitations": [
            "single exploratory boundary search",
            "accepted Wi-Fi route",
            "shared one-CPU quota",
        ],
        "runs": [_clean_run(run, index) for index, run in enumerate(source["runs"])],
    }


def aggregate_results(results_dir: Path, *, allow_partial=False):
    try:
        journal = json.loads((results_dir / "campaign-journal.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("results directory requires campaign-journal.json") from error
    latest = {}
    for entry in journal.get("runs", []):
        if isinstance(entry, dict) and (entry.get("runtime"), entry.get("framework")) in VARIANTS:
            latest[(entry["runtime"], entry["framework"])] = entry
    if not allow_partial and set(latest) != set(VARIANTS):
        raise ValueError("campaign journal lacks a latest result for one or more capacity variants")
    results = []
    shared_identity = None
    runtime_images = {}
    for runtime, framework in VARIANTS:
        if (runtime, framework) not in latest:
            continue
        entry = latest[(runtime, framework)]
        if entry.get("status") not in {"complete", "failed", "invalid"}:
            if allow_partial:
                continue
            raise ValueError(f"{runtime}/{framework} is incomplete")
        attempt = entry.get("attemptId")
        if not isinstance(attempt, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", attempt):
            raise ValueError("unsafe attempt ID")
        path = results_dir / attempt / "result.json"
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"unreadable result for {runtime}/{framework}") from error
        if (
            result.get("attemptId") != attempt
            or result.get("metadata", {}).get("runtime") != runtime
            or result.get("metadata", {}).get("framework") != framework
        ):
            raise ValueError("journal/result identity mismatch")
        metadata = result.get("metadata", {})
        if metadata.get("image") != entry.get("image"):
            raise ValueError("journal/result image identity mismatch")
        if runtime_images.setdefault(runtime, metadata.get("image")) != metadata.get("image"):
            raise ValueError("campaign results have inconsistent runtime image pins")
        identity = tuple(
            metadata.get(key)
            for key in ("sourceRevision", "harnessSourceRevision", "loadHash", "protocolHash")
        )
        if not (
            isinstance(identity[0], str)
            and SHA40.fullmatch(identity[0])
            and isinstance(identity[1], str)
            and SHA40.fullmatch(identity[1])
            and isinstance(identity[2], str)
            and SHA64.fullmatch(identity[2])
            and isinstance(identity[3], str)
            and SHA64.fullmatch(identity[3])
        ):
            raise ValueError("result has an invalid immutable campaign identity")
        if shared_identity is None:
            shared_identity = identity
        elif identity != shared_identity:
            raise ValueError(
                "campaign results have mismatched source, harness, load, or protocol identity"
            )
        results.append(result)
    if not results:
        raise ValueError("campaign journal has no completed capacity results")
    return {
        "experiment": "sqlite-capacity-v1",
        "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": results,
    }


CSV_FIELDS = [
    "runtime",
    "framework",
    "capture_validity",
    "target_rps",
    "complete",
    "overload",
    "client_p95_ms",
    "service_p95_ms",
    "db_p95_ms",
    "goodput_rps",
    "completed",
    "successful",
    "http_errors",
    "validation_errors",
    "dropped",
    "pod_cpu_millicores",
    "pod_working_set_mib",
    "pod_cfs_period_ratio",
]


def csv_rows(data):
    for run in data["runs"]:
        for stage in run["stages"]:
            for window in stage["windows"]:
                if not window["stable"]:
                    continue
                resource, client = window["resource"], window["client"]
                yield {
                    "runtime": run["runtime"],
                    "framework": run["framework"],
                    "capture_validity": run["validity"]["status"],
                    "target_rps": window["targetRps"],
                    "complete": stage["completed"],
                    "overload": stage["overload"]["status"],
                    "client_p95_ms": client["p95Ms"],
                    "service_p95_ms": window["serviceP95Ms"],
                    "db_p95_ms": window["dbP95Ms"],
                    "goodput_rps": window["goodputRps"],
                    "completed": window["completed"],
                    "successful": window["successful"],
                    "http_errors": window["httpFailures"],
                    "validation_errors": window["validationFailures"],
                    "dropped": window["dropped"],
                    "pod_cpu_millicores": resource["cpuMillicores"],
                    "pod_working_set_mib": None
                    if resource["workingSetBytes"] is None
                    else resource["workingSetBytes"] / 1048576,
                    "pod_cfs_period_ratio": resource["cfsPeriodRatio"],
                }


def document(data, plotly):
    template = (ROOT / "template.html").read_text()
    return (
        template.replace("/*__CSS__*/", (ROOT / "report.css").read_text())
        .replace("/*__JS__*/", (ROOT / "report.js").read_text())
        .replace("/*__PLOTLY__*/", plotly)
        .replace(
            '"__DATA__"',
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c"),
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args(argv)
    data = sanitize(aggregate_results(args.results_dir, allow_partial=args.allow_partial))
    try:
        from plotly.offline import get_plotlyjs
    except ImportError as error:
        raise SystemExit("install Plotly 7.1.0 before rendering") from error
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "data.json").write_text(json.dumps(data, indent=2) + "\n")
    (args.output_dir / "comparison.html").write_text(document(data, get_plotlyjs()))
    with (args.output_dir / "comparison.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(csv_rows(data))


if __name__ == "__main__":
    main()
