"""Render the public SQLite-ramp report from normalized, measured artifacts only."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parent
CAMPAIGN_VARIANTS = (
    ("go", "nethttp"),
    ("node", "express"),
    ("bun", "native"),
    ("rust", "axum"),
    ("python", "fastapi"),
    ("elixir", "plug"),
    ("go", "chi"),
    ("node", "fastify"),
    ("bun", "hono"),
    ("rust", "actix"),
    ("elixir", "phoenix"),
    ("go", "fiber"),
    ("node", "nest"),
    ("bun", "elysia"),
    ("rust", "rocket"),
)
SAFE_REASON_CODES = {
    "schedule_delivery",
    "goodput",
    "http_errors",
    "checks",
    "latency",
    "dropped_iterations",
    "collector_failure",
    "identity_drift",
    "capture_truncated",
    "restart",
    "oom",
    "write_integrity",
    "summary_request_mismatch",
    "summary_checks_mismatch",
    "summary_stock_mismatch",
    "summary_drops_mismatch",
    "timing_outcome_mismatch",
    "missing_required_summary",
    "invalid_scenario_origin",
    "missing_scenario_origin",
    "invalid_metric_tags",
    "integrity_mismatch",
    "integrity_excess_revisions",
    "local_telemetry_missing",
    "recorder_failure",
    "coverage_missing",
    "collector_generator_failure",
    "pod_identity_drift",
}
RUN_FIELDS = {"id", "runtime", "framework"}
BUILD_FIELDS = {
    "imageDigest",
    "sourceRevision",
    "loadHash",
    "scheduleHash",
    "harnessSourceRevision",
}
METADATA_FIELDS = {
    "runtimeVersion",
    "frameworkVersion",
    "driver",
    "driverVersion",
    "sqliteVersion",
    "workers",
    "pragmas",
    "compileOptions",
    "workerSettings",
}
WINDOW_FIELDS = {
    "targetRps",
    "startSeconds",
    "endSeconds",
    "stable",
    "expectedArrivals",
    "started",
    "completed",
    "successful",
    "goodputRps",
    "dropped",
    "httpFailures",
    "validationFailures",
    "checksFailed",
    "client",
    "serviceP95Ms",
    "dbP95Ms",
    "slo",
    "resource",
    "coverage",
}
HISTORY_FIELDS = {
    "seconds",
    "bucketSeconds",
    "targetRps",
    "phase",
    "completed",
    "successful",
    "dropped",
    "statuses",
    "httpFailures",
    "validationFailures",
    "checksFailed",
    "clientP50Ms",
    "clientP95Ms",
    "clientP99Ms",
    "serviceP95Ms",
    "dbP95Ms",
}
RESOURCE_FIELDS = {
    "seconds",
    "intervalStartSeconds",
    "intervalEndSeconds",
    "realtimeStartSeconds",
    "realtimeEndSeconds",
    "monotonicIntervalSeconds",
    "cpuMillicores",
    "workingSetBytes",
    "memoryCurrentBytes",
    "throttledSeconds",
    "cfsPeriods",
    "cfsThrottledPeriods",
    "cfsPeriodRatio",
    "cpuPressure",
    "memoryPressure",
    "cpuPressureTotalMicroseconds",
    "memoryPressureTotalMicroseconds",
    "nodeCpuUtilization",
}
PRIVATE_TEXT = re.compile(
    r"(?:\b(?:\d{1,3}\.){3}\d{1,3}\b|/users/|/private/|/tmp/|\\\\|ssh://|\bpod[-_/ ])",
    re.IGNORECASE,
)
SAFE_TEXT = re.compile(r"^[A-Za-z0-9 .,:_+@/=-]{1,160}$")
SAFE_INTERFACE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
SAFE_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
SAFE_REVISION = re.compile(r"^[a-f0-9]{7,64}$")
SAFE_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_SETTINGS = {
    "workers",
    "goMaxProcs",
    "uvicornWorkers",
    "beamSchedulers",
    "beamDirtyCpuSchedulers",
    "beamDirtyIoSchedulers",
    "nodeClusterWorkers",
    "bunWorkers",
}
SAFE_PRAGMAS = {
    "journal_mode",
    "synchronous",
    "foreign_keys",
    "busy_timeout",
    "cache_size",
    "wal_autocheckpoint",
    "temp_store",
}
SAFE_WARNINGS = {
    "host_cpu_headroom",
    "host_memory_headroom",
    "swap_growth",
    "collector_gap",
    "generator_headroom",
}
SAFE_PHASES = {"stable", "transition", "settling", "drain"}
ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
PRESSURE_FIELDS = {"avg10", "avg60", "avg300", "total"}


def mapping(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def pick(value, fields, name):
    value = mapping(value or {}, name)
    return {field: value[field] for field in fields if field in value}


def reasons(value):
    return (
        [item if item in SAFE_REASON_CODES else "local_artifact" for item in value]
        if isinstance(value, list)
        else []
    )


def warnings(value):
    return (
        [item if item in SAFE_WARNINGS else "local_artifact" for item in value]
        if isinstance(value, list)
        else []
    )


def number(value):
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def numeric_fields(value, fields):
    return {field: number(value.get(field)) for field in fields}


def clean_pressure(value):
    if not isinstance(value, dict):
        return {}
    return {
        category: numeric_fields(item, PRESSURE_FIELDS)
        for category, item in value.items()
        if category in {"some", "full"} and isinstance(item, dict)
    }


def safe_text(value):
    if not isinstance(value, str) or not SAFE_TEXT.fullmatch(value) or PRIVATE_TEXT.search(value):
        return "local_artifact"
    return value


def safe_mapping(value, allowed):
    if not isinstance(value, dict):
        return {}
    return {
        key: safe_text(item) if isinstance(item, str) else item
        for key, item in value.items()
        if key in allowed and isinstance(item, (str, int, float, bool))
    }


def clean_metadata(value):
    source = pick(value, METADATA_FIELDS, "metadata")
    result = {
        key: safe_text(item) if isinstance(item, str) else item
        for key, item in source.items()
        if key not in {"pragmas", "compileOptions", "workerSettings"}
        and isinstance(item, (str, int, float, bool))
    }
    result["pragmas"] = safe_mapping(source.get("pragmas"), SAFE_PRAGMAS)
    result["workerSettings"] = safe_mapping(source.get("workerSettings"), SAFE_SETTINGS)
    result["compileOptions"] = [
        safe_text(item) for item in source.get("compileOptions", []) if isinstance(item, str)
    ]
    return result


def clean_build(value):
    source = pick(value, BUILD_FIELDS, "build")
    return {
        "imageDigest": source.get("imageDigest")
        if isinstance(source.get("imageDigest"), str)
        and SAFE_DIGEST.fullmatch(source["imageDigest"])
        else "local_artifact",
        "sourceRevision": source.get("sourceRevision")
        if isinstance(source.get("sourceRevision"), str)
        and SAFE_REVISION.fullmatch(source["sourceRevision"])
        else "local_artifact",
        "loadHash": source.get("loadHash")
        if isinstance(source.get("loadHash"), str) and SAFE_REVISION.fullmatch(source["loadHash"])
        else "local_artifact",
        "scheduleHash": source.get("scheduleHash")
        if isinstance(source.get("scheduleHash"), str)
        and SAFE_REVISION.fullmatch(source["scheduleHash"])
        else "local_artifact",
        "harnessSourceRevision": source.get("harnessSourceRevision")
        if isinstance(source.get("harnessSourceRevision"), str)
        and SAFE_REVISION.fullmatch(source["harnessSourceRevision"])
        else "local_artifact",
    }


def clean_window(value):
    source = pick(value, WINDOW_FIELDS, "window")
    if number(source.get("targetRps")) is None:
        raise ValueError("window targetRps is required")
    result = numeric_fields(
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
    result["client"] = numeric_fields(
        mapping(source.get("client"), "client"), {"p50Ms", "p95Ms", "p99Ms"}
    )
    result["resource"] = pick(
        source.get("resource"),
        {
            "cpuMillicores",
            "workingSetBytes",
            "memoryCurrentBytes",
            "throttledSeconds",
            "cfsPeriodRatio",
            "coverage",
            "pressure",
        },
        "window resource",
    )
    result["resource"] = numeric_fields(
        result["resource"],
        {
            "cpuMillicores",
            "workingSetBytes",
            "memoryCurrentBytes",
            "throttledSeconds",
            "cfsPeriodRatio",
            "coverage",
        },
    ) | {
        "pressure": clean_pressure(
            mapping(source.get("resource") or {}, "window resource").get("pressure")
        )
    }
    source_slo = mapping(source.get("slo"), "window slo")
    result["slo"] = {
        "scheduleDelivery": source_slo.get("scheduleDelivery"),
        "goodput": source_slo.get("goodput"),
        "errors": source_slo.get("errors"),
        "latency": source_slo.get("latency"),
        "status": source_slo.get("status")
        if source_slo.get("status") in {"pass", "fail"}
        else "fail",
        "reasons": reasons(source_slo.get("reasons")),
    }
    return result


def clean_run(value, index):
    value = mapping(value, f"runs[{index}]")
    source_identity = pick(value, RUN_FIELDS, f"runs[{index}]")
    if not all(
        isinstance(source_identity.get(key), str) and source_identity[key] for key in RUN_FIELDS
    ):
        raise ValueError(f"runs[{index}] needs id, runtime, framework")
    result = {key: safe_text(item) for key, item in source_identity.items()}
    source_validity = mapping(value.get("validity"), "validity")
    if source_validity.get("status") not in {"valid", "invalid"}:
        raise ValueError("validity.status must be valid or invalid")
    result["validity"] = {
        "status": source_validity["status"],
        "reasons": reasons(source_validity.get("reasons")),
    }
    result["build"] = clean_build(value.get("build"))
    result["metadata"] = clean_metadata(value.get("metadata"))
    source_generator = pick(
        value.get("generator"),
        {"coverage", "samplingCoverage", "interface", "headroomFlag", "warnings"},
        "generator",
    )
    result["generator"] = {
        "coverage": number(source_generator.get("coverage")),
        "samplingCoverage": number(source_generator.get("samplingCoverage")),
        "headroomFlag": source_generator.get("headroomFlag") is True,
        "warnings": warnings(source_generator.get("warnings")),
    }
    if SAFE_INTERFACE.fullmatch(str(source_generator.get("interface", ""))):
        result["generator"]["interface"] = source_generator["interface"]
    source_resource = pick(value.get("resource"), {"coverage", "samples"}, "resource")
    result["resource"] = {
        "coverage": number(source_resource.get("coverage")),
        "samples": [
            clean_resource_sample(sample)
            for sample in source_resource.get("samples", [])
            if isinstance(sample, dict)
        ],
    }
    result["windows"] = [clean_window(item) for item in value.get("windows", [])]
    result["history"] = []
    for item in value.get("history", []):
        source_history = pick(item, HISTORY_FIELDS, "history")
        cleaned = numeric_fields(
            source_history,
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
        cleaned["phase"] = (
            source_history.get("phase") if source_history.get("phase") in SAFE_PHASES else None
        )
        statuses = source_history.get("statuses", {})
        cleaned["statuses"] = (
            {
                str(code): count
                for code, count in statuses.items()
                if str(code).isdigit() and number(count) is not None
            }
            if isinstance(statuses, dict)
            else {}
        )
        result["history"].append(cleaned)
    return result


def clean_resource_sample(value):
    source = pick(value, RESOURCE_FIELDS, "resource sample")
    result = numeric_fields(source, RESOURCE_FIELDS - {"cpuPressure", "memoryPressure"})
    result["cpuPressure"] = clean_pressure(source.get("cpuPressure"))
    result["memoryPressure"] = clean_pressure(source.get("memoryPressure"))
    return result


def sanitize(source):
    source = mapping(source, "result data")
    if source.get("experiment") != "sqlite-ramp-v2" or not isinstance(source.get("runs"), list):
        raise ValueError("expected sqlite-ramp-v2 runs")
    source_schedule = pick(source.get("schedule"), {"hash", "stages"}, "schedule")
    schedule = {
        "hash": source_schedule.get("hash")
        if isinstance(source_schedule.get("hash"), str)
        and SAFE_REVISION.fullmatch(source_schedule["hash"])
        else "local_artifact",
        "stages": [],
    }
    schedule["stages"] = [
        numeric_fields(
            pick(
                stage,
                {
                    "targetRps",
                    "stableStartSeconds",
                    "stableEndSeconds",
                    "transitionStartSeconds",
                    "transitionEndSeconds",
                },
                "stage",
            ),
            {
                "targetRps",
                "stableStartSeconds",
                "stableEndSeconds",
                "transitionStartSeconds",
                "transitionEndSeconds",
            },
        )
        for stage in source_schedule.get("stages", [])
        if isinstance(stage, dict)
    ]
    return {
        "experiment": "sqlite-ramp-v2",
        "generatedAt": source.get("generatedAt")
        if isinstance(source.get("generatedAt"), str)
        and ISO_TIMESTAMP.fullmatch(source["generatedAt"])
        else None,
        "limitations": ["single trial", "accepted Wi-Fi route", "shared CPU quota"]
        if source.get("limitations")
        else [],
        "schedule": schedule,
        "runs": [clean_run(item, index) for index, item in enumerate(source["runs"])],
    }


def schedule_from_windows(windows):
    stable = [item for item in windows if item.get("stable") is True]
    if not stable:
        raise ValueError("result has no stable windows")
    schedule, previous_end = [], 0
    for index, window in enumerate(stable):
        start, end, target = (
            window.get("startSeconds"),
            window.get("endSeconds"),
            window.get("targetRps"),
        )
        if any(number(value) is None for value in (start, end, target)) or end <= start:
            raise ValueError("result has invalid stable window bounds")
        transition = start - previous_end
        schedule.append(
            {
                "targetRps": target,
                "transitionSeconds": transition if index else 0,
                "stableSeconds": end - start,
                "settlingSeconds": start if index == 0 else 0,
            }
        )
        previous_end = end
    return schedule


def aggregate_results(results_dir: Path):
    try:
        journal = json.loads((results_dir / "campaign-journal.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("results directory requires a readable campaign-journal.json") from error
    runs = journal.get("runs")
    if not isinstance(runs, list):
        raise ValueError("campaign journal runs must be an array")
    latest = {}
    for entry in runs:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("runtime"), str)
            and isinstance(entry.get("framework"), str)
        ):
            latest[(entry["runtime"], entry["framework"])] = entry
    if set(latest) != set(CAMPAIGN_VARIANTS):
        raise ValueError("campaign journal must contain the latest result for all 15 variants")
    collected, common, runtime_images = [], None, {}
    for variant in CAMPAIGN_VARIANTS:
        entry = latest[variant]
        if entry.get("status") not in {"complete", "invalid"}:
            raise ValueError(f"latest {variant[0]}/{variant[1]} attempt is incomplete")
        attempt_id = entry.get("attemptId")
        if not isinstance(attempt_id, str) or not SAFE_ATTEMPT_ID.fullmatch(attempt_id):
            raise ValueError("campaign journal has an unsafe attempt ID")
        result_path = results_dir / attempt_id / "result.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"latest {variant[0]}/{variant[1]} result is unreadable") from error
        metadata, build = (
            mapping(result.get("metadata"), "result metadata"),
            mapping(result.get("build"), "result build"),
        )
        identity = (
            build.get("loadHash"),
            build.get("scheduleHash"),
            build.get("sourceRevision"),
            build.get("harnessSourceRevision"),
        )
        if common is None:
            common = identity
            schedule = schedule_from_windows(result.get("windows", []))
        elif identity != common:
            raise ValueError("campaign results have mismatched load, schedule, or source identity")
        if (
            metadata.get("runtime") != variant[0]
            or metadata.get("framework") != variant[1]
            or result.get("attemptId") != entry.get("attemptId")
        ):
            raise ValueError("result metadata does not match its latest journal attempt")
        image = build.get("imageDigest")
        if runtime_images.setdefault(variant[0], image) != image:
            raise ValueError("campaign results have inconsistent runtime image digests")
        collected.append(result)
    return {
        "experiment": "sqlite-ramp-v2",
        "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limitations": ["campaign aggregate"],
        "schedule": {"hash": common[1], "stages": schedule_to_report(schedule)},
        "runs": collected,
    }


def schedule_to_report(schedule):
    elapsed, result = 0, []
    for stage in schedule:
        transition_start = elapsed
        transition_end = elapsed + stage["transitionSeconds"]
        stable_start = transition_end + stage.get("settlingSeconds", 0)
        stable_end = stable_start + stage["stableSeconds"]
        result.append(
            {
                "targetRps": stage["targetRps"],
                "transitionStartSeconds": transition_start,
                "transitionEndSeconds": transition_end,
                "stableStartSeconds": stable_start,
                "stableEndSeconds": stable_end,
            }
        )
        elapsed = stable_end
    return result


CSV_FIELDS = [
    "run_id",
    "runtime",
    "framework",
    "validity",
    "image_digest",
    "source_revision",
    "load_hash",
    "schedule_hash",
    "harness_source_revision",
    "runtime_version",
    "framework_version",
    "driver",
    "driver_version",
    "sqlite_version",
    "workers",
    "target_rps",
    "window_start_seconds",
    "window_end_seconds",
    "expected_arrivals",
    "started",
    "completed",
    "successful",
    "goodput_rps",
    "dropped",
    "http_failures",
    "validation_failures",
    "checks_failed",
    "client_p95_ms",
    "service_p95_ms",
    "db_p95_ms",
    "cpu_millicores",
    "working_set_bytes",
    "memory_current_bytes",
    "throttled_seconds",
    "resource_coverage",
    "window_coverage",
    "generator_coverage",
    "generator_headroom_flag",
    "slo_status",
    "slo_reasons",
]


def csv_rows(data):
    for run in data["runs"]:
        for item in run["windows"]:
            metadata, build, client, resource, slo = (
                run["metadata"],
                run["build"],
                item["client"],
                item["resource"],
                item["slo"],
            )
            yield {
                "run_id": run["id"],
                "runtime": run["runtime"],
                "framework": run["framework"],
                "validity": run["validity"]["status"],
                "image_digest": build.get("imageDigest"),
                "source_revision": build.get("sourceRevision"),
                "load_hash": build.get("loadHash"),
                "schedule_hash": build.get("scheduleHash"),
                "harness_source_revision": build.get("harnessSourceRevision"),
                "runtime_version": metadata.get("runtimeVersion"),
                "framework_version": metadata.get("frameworkVersion"),
                "driver": metadata.get("driver"),
                "driver_version": metadata.get("driverVersion"),
                "sqlite_version": metadata.get("sqliteVersion"),
                "workers": metadata.get("workers"),
                "target_rps": item.get("targetRps"),
                "window_start_seconds": item.get("startSeconds"),
                "window_end_seconds": item.get("endSeconds"),
                "expected_arrivals": item.get("expectedArrivals"),
                "started": item.get("started"),
                "completed": item.get("completed"),
                "successful": item.get("successful"),
                "goodput_rps": item.get("goodputRps"),
                "dropped": item.get("dropped"),
                "http_failures": item.get("httpFailures"),
                "validation_failures": item.get("validationFailures"),
                "checks_failed": item.get("checksFailed"),
                "client_p95_ms": client.get("p95Ms"),
                "service_p95_ms": item.get("serviceP95Ms"),
                "db_p95_ms": item.get("dbP95Ms"),
                "cpu_millicores": resource.get("cpuMillicores"),
                "working_set_bytes": resource.get("workingSetBytes"),
                "memory_current_bytes": resource.get("memoryCurrentBytes"),
                "throttled_seconds": resource.get("throttledSeconds"),
                "resource_coverage": run["resource"].get("coverage"),
                "window_coverage": (
                    item.get("coverage")
                    if item.get("coverage") is not None
                    else resource.get("coverage")
                ),
                "generator_coverage": run["generator"].get("coverage"),
                "generator_headroom_flag": run["generator"].get("headroomFlag"),
                "slo_status": slo.get("status"),
                "slo_reasons": ";".join(slo["reasons"]),
            }


def document(data, plotly):
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    return (
        template.replace("/*__CSS__*/", (ROOT / "report.css").read_text(encoding="utf-8"))
        .replace("/*__PLOTLY__*/", plotly)
        .replace(
            '"__DATA__"',
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c"),
        )
        .replace("/*__JS__*/", (ROOT / "report.js").read_text(encoding="utf-8"))
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--results-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.input) == bool(args.results_dir):
        parser.error("provide exactly one aggregate input or --results-dir")
    source = (
        aggregate_results(args.results_dir)
        if args.results_dir
        else json.loads(args.input.read_text(encoding="utf-8"))
    )
    data = sanitize(source)
    try:
        from plotly.offline import get_plotlyjs
    except ImportError as error:
        raise SystemExit("install report/requirements.txt before rendering") from error
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "data.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    with (args.output_dir / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(csv_rows(data))
    (args.output_dir / "comparison.html").write_text(
        document(data, get_plotlyjs()), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
