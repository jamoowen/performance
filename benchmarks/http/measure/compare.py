import argparse
import csv
import html
import json
import math
import os
import statistics
from collections import defaultdict
from pathlib import Path

from .results import compatible_key, healthy, load_records

CSV_FIELDS = (
    "run_id",
    "path",
    "date",
    "implementation",
    "target_rps",
    "profile",
    "workload",
    "duration",
    "warmup_duration",
    "seed_count",
    "preallocated_vus",
    "max_vus",
    "p95_threshold_ms",
    "max_error_rate_threshold",
    "sample_interval",
    "k6_version",
    "node_name",
    "node_uid",
    "resource_requests",
    "resource_limits",
    "image",
    "image_id",
    "actual_rps",
    "requests",
    "error_rate",
    "check_failure_rate",
    "dropped_iterations",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "operation_p95_ms",
    "mean_cpu_millicores",
    "max_cpu_millicores",
    "max_sampled_working_set_mib",
    "max_sampled_rss_mib",
    "throttled_periods_percent",
    "status",
    "failure_reasons",
    "restart_count",
    "node_pressure",
    "resource_coverage",
    "collector_errors",
    "cluster_after_pressure",
    "restart_delta",
)
RUNTIME_COLORS = {"go": "#3977af", "bun": "#d56a27"}
ESSENTIAL_RESOURCE_WARNINGS = {
    "insufficient distinct CPU samples",
    "CPU counter reset",
    "CPU counter identity changed",
    "essential metric unavailable: memory_working_set",
}


def nested(record, *keys):
    current = record
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def mapping(value):
    return value if isinstance(value, dict) else {}


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def mib(value):
    return value / 1024**2 if number(value) is not None else None


def display(value, digits=3):
    if value is None or value == "":
        return "NA"
    if isinstance(value, float):
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def record_path(record):
    return str(record.get("path", "")) if isinstance(record, dict) else ""


def run_id(record):
    value = record.get("run_id") if isinstance(record, dict) else None
    if isinstance(value, str) and value:
        return value
    path = Path(record_path(record))
    return path.parent.name if path.name == "result.json" else "unknown"


def validation_errors(record):
    if not isinstance(record, dict):
        return ["record is not an object"]
    errors = []
    if record.get("status") not in {"complete", "invalid"}:
        errors.append(f"status is {record.get('status', 'missing')}")
    if record.get("status") != "invalid":
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            errors.append("metadata is missing")
        elif metadata.get("implementation") not in {"go", "bun"}:
            errors.append("implementation is missing or unsupported")
        elif not isinstance(metadata.get("settings"), dict):
            errors.append("settings is missing")
        if not isinstance(record.get("result"), dict):
            errors.append("result is missing")
        if not isinstance(record.get("resource"), dict):
            errors.append("resource is missing")
        if record.get("schema_version", 1) != 1:
            errors.append("unsupported schema version")
    if record.get("error"):
        errors.append(str(record["error"]))
    return errors


def failure_reasons(record):
    reasons = validation_errors(record)
    thresholds = nested(record, "result", "thresholds_failed")
    if isinstance(thresholds, list) and thresholds:
        reasons.append("thresholds: " + ", ".join(map(str, thresholds)))
    warnings = nested(record, "resource", "warnings")
    if isinstance(warnings, list) and warnings:
        reasons.append("resources: " + ", ".join(map(str, warnings)))
    return "; ".join(reasons)


def record_row(record):
    if not isinstance(record, dict):
        record = {"status": "invalid", "error": "record is not an object"}
    settings = mapping(nested(record, "metadata", "settings"))
    resource = mapping(record.get("resource"))
    pod = mapping(nested(record, "metadata", "cluster", "pod"))
    pressure = nested(record, "metadata", "cluster", "pressure")
    after = mapping(nested(record, "metadata", "cluster_after"))
    node = mapping(nested(record, "metadata", "cluster", "node"))
    workload = mapping(nested(record, "metadata", "cluster", "workload"))
    resources = workload.get("resources") if isinstance(workload, dict) else None
    operation = nested(record, "result", "operation_p95_ms")
    return {
        "run_id": run_id(record),
        "path": record_path(record),
        "date": nested(record, "metadata", "measured_started_at_unix"),
        "implementation": nested(record, "metadata", "implementation") or "unknown",
        "target_rps": settings.get("rate"),
        "profile": settings.get("profile"),
        "workload": settings.get("workload"),
        "duration": settings.get("duration"),
        "warmup_duration": settings.get("warmup_duration"),
        "seed_count": settings.get("seed_count"),
        "preallocated_vus": settings.get("preallocated_vus"),
        "max_vus": settings.get("max_vus"),
        "p95_threshold_ms": settings.get("p95_ms"),
        "max_error_rate_threshold": settings.get("max_error_rate"),
        "sample_interval": settings.get("sample_interval"),
        "k6_version": nested(record, "metadata", "k6_version"),
        "node_name": node.get("name"),
        "node_uid": node.get("uid"),
        "resource_requests": json.dumps(resources.get("requests"), sort_keys=True)
        if isinstance(resources, dict) and isinstance(resources.get("requests"), dict)
        else None,
        "resource_limits": json.dumps(resources.get("limits"), sort_keys=True)
        if isinstance(resources, dict) and isinstance(resources.get("limits"), dict)
        else None,
        "image": pod.get("image"),
        "image_id": pod.get("image_id"),
        "actual_rps": nested(record, "result", "achieved_rps"),
        "requests": nested(record, "result", "requests"),
        "error_rate": nested(record, "result", "error_rate"),
        "check_failure_rate": nested(record, "result", "check_failure_rate"),
        "dropped_iterations": nested(record, "result", "dropped_iterations"),
        "p50_ms": nested(record, "result", "latency_ms", "med"),
        "p95_ms": nested(record, "result", "latency_ms", "p(95)"),
        "p99_ms": nested(record, "result", "latency_ms", "p(99)"),
        "operation_p95_ms": json.dumps(operation, sort_keys=True)
        if isinstance(operation, dict)
        else None,
        "mean_cpu_millicores": resource.get("mean_cpu_millicores"),
        "max_cpu_millicores": resource.get("max_cpu_millicores"),
        "max_sampled_working_set_mib": mib(resource.get("max_sampled_working_set_bytes")),
        "max_sampled_rss_mib": mib(resource.get("max_sampled_rss_bytes")),
        "throttled_periods_percent": resource.get("throttled_periods_percent"),
        "status": record.get("status", "invalid"),
        "failure_reasons": failure_reasons(record),
        "restart_count": pod.get("restart_count"),
        "node_pressure": json.dumps(pressure, sort_keys=True)
        if isinstance(pressure, dict)
        else None,
        "resource_coverage": resource.get("coverage") or resource.get("sample_count"),
        "collector_errors": "; ".join(map(str, record.get("collector_errors", [])))
        if isinstance(record.get("collector_errors"), list)
        else None,
        "cluster_after_pressure": json.dumps(after.get("pressure"), sort_keys=True)
        if isinstance(after.get("pressure"), dict)
        else None,
        "restart_delta": resource.get("restart_delta"),
    }


def is_healthy(record):
    try:
        return not validation_errors(record) and healthy(record)
    except (KeyError, TypeError, ValueError):
        return False


def group_key(record):
    try:
        return compatible_key(record)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def group_label(record):
    settings = mapping(nested(record, "metadata", "settings"))
    resources = mapping(nested(record, "metadata", "cluster", "workload", "resources"))
    limits = mapping(resources.get("limits"))
    diagnostics = (
        "diagnostic/instrumented"
        if settings.get("diagnostics", False)
        or str(
            mapping(nested(record, "metadata", "cluster", "workload", "configuration")).get(
                "DIAGNOSTICS", ""
            )
        )
        == "1"
        else "baseline"
    )
    return (
        f"{display(settings.get('rate'))} RPS · {display(settings.get('profile'))} · "
        f"{display(settings.get('workload'))} · {display(settings.get('duration'))} · "
        f"CPU {display(limits.get('cpu'))} · memory {display(limits.get('memory'))} · {diagnostics}"
    )


def diagnostics_detail(record, output):
    """A compact, escaped offline viewer for optional diagnostic artifacts."""
    diagnostic = mapping(nested(record, "metadata", "diagnostics"))
    if not diagnostic or diagnostic.get("status") == "not-recorded":
        return "<details class='diagnostics'><summary>Diagnostics: not recorded</summary><p>Profiler data was not collected for this baseline run.</p></details>"
    status = display(diagnostic.get("status"))
    run_directory = Path(record_path(record)).parent
    summary = {}
    if diagnostic.get("summary_file") == "diagnostics/diagnostics-summary.json":
        try:
            summary = mapping(
                json.loads((run_directory / "diagnostics" / "diagnostics-summary.json").read_text())
            )
        except (OSError, ValueError, json.JSONDecodeError):
            summary = {}
    capture = mapping(summary.get("capture_window"))
    capture_seconds = (
        number(capture.get("end")) - number(capture.get("start"))
        if number(capture.get("end")) is not None and number(capture.get("start")) is not None
        else None
    )
    measurement_window = mapping(summary.get("measurement_window"))
    fact_values = (
        ("CPU profile window", f"{display(capture_seconds, 2)} s"),
        ("Capture overlap", f"{display(number(measurement_window.get('overlap_seconds')), 2)} s"),
        ("Runtime sample coverage", f"{display(number(summary.get('coverage_seconds')), 2)} s"),
        ("Actual GOMAXPROCS", display(summary.get("actual_gomaxprocs"))),
        (
            "Heap peak",
            f"{display(mib(summary.get('go_heap_alloc_peak_bytes') or summary.get('bun_heap_peak_bytes')), 2)} MiB",
        ),
        (
            "Allocation rate",
            f"{display(mib(summary.get('go_allocation_bytes_per_second')), 2)} MiB/s",
        ),
        ("GC count", display(summary.get("go_gc_cycles_delta"))),
        (
            "GC pause",
            f"{display(number(summary.get('go_gc_pause_ns_delta')) / 1e6 if number(summary.get('go_gc_pause_ns_delta')) is not None else None, 2)} ms",
        ),
        ("DB wait count", display(summary.get("database_wait_count_delta"))),
        ("DB wait", f"{display(number(summary.get('database_wait_seconds_delta')), 3)} s"),
        (
            "Bun loop lag",
            f"{display(number(summary.get('bun_event_loop_delay_p95_max_ms')), 2)} ms",
        ),
    )
    facts = "".join(
        f"<div><dt>{html.escape(name)}</dt><dd>{html.escape(value)}</dd></div>"
        for name, value in fact_values
    )
    links = []
    for profile in (
        diagnostic.get("profiles", []) if isinstance(diagnostic.get("profiles"), list) else []
    ):
        allowed = {"cpu.pprof", "jsc-cpu.json", "cpu-top.txt"} | {
            f"{kind}-{phase}.pprof"
            for kind in ("heap", "allocs", "goroutine", "block", "mutex")
            for phase in ("before", "after")
        }
        if (
            not isinstance(profile, str)
            or profile != f"diagnostics/{Path(profile).name}"
            or Path(profile).name not in allowed
        ):
            continue
        candidate = run_directory / profile
        if candidate.is_file():
            relative = os.path.relpath(candidate, output)
            links.append(
                f"<a href='{html.escape(relative, quote=True)}' download>{html.escape(Path(profile).name)}</a>"
            )
    warnings = (
        list(diagnostic.get("warnings", [])) if isinstance(diagnostic.get("warnings"), list) else []
    )
    warnings += summary.get("warnings", []) if isinstance(summary.get("warnings"), list) else []
    top = ""
    top_path = run_directory / "diagnostics" / "cpu-top.txt"
    try:
        if top_path.is_file() and top_path.parent == run_directory / "diagnostics":
            top = "<pre>" + html.escape(top_path.read_text()[:10000]) + "</pre>"
    except (OSError, UnicodeError):
        pass
    notes = "".join(
        f"<p class='failure'>{html.escape(str(item))}</p>"
        for item in warnings
        if isinstance(item, str)
    )
    if status == "partial":
        notes = (
            "<p class='failure'>Diagnostics incomplete; benchmark outcome is preserved.</p>" + notes
        )
    return f"<details class='diagnostics'><summary>Diagnostics: {html.escape(status)}</summary><dl class='details'>{facts}</dl><p>{' · '.join(links) or 'No profile artifact saved.'}</p>{top}{notes}</details>"


def aggregate_metric(records, metric):
    values = [number(nested(record, *metric)) for record in records if is_healthy(record)]
    values = [value for value in values if value is not None]
    if not values:
        return {"median": None, "minimum": None, "maximum": None}
    return {"median": statistics.median(values), "minimum": min(values), "maximum": max(values)}


def summarize(records):
    groups, invalid = defaultdict(list), []
    for record in records:
        key = group_key(record)
        (invalid if key is None else groups[key]).append(record)
    summaries = []
    for key, runs in groups.items():
        values = {}
        for implementation in ("go", "bun"):
            own = [
                run for run in runs if nested(run, "metadata", "implementation") == implementation
            ]
            if own:
                values[implementation] = {
                    "runs": own,
                    "healthy_runs": [run for run in own if is_healthy(run)],
                    "failed_runs": [run for run in own if not is_healthy(run)],
                    "p95_ms": aggregate_metric(own, ("result", "latency_ms", "p(95)")),
                    "mean_cpu_millicores": aggregate_metric(
                        own, ("resource", "mean_cpu_millicores")
                    ),
                    "working_set_mib": {
                        name: mib(value)
                        for name, value in aggregate_metric(
                            own, ("resource", "max_sampled_working_set_bytes")
                        ).items()
                    },
                }
        summaries.append(
            {
                "key": key,
                "label": group_label(runs[0]),
                "runtimes": values,
                "awaiting": sorted({"go", "bun"} - set(values)),
                "all_failed": [
                    runtime for runtime, value in values.items() if not value["healthy_runs"]
                ],
            }
        )
    return summaries, invalid


def write_csv(records, output):
    with (output / "comparison.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record_row(record))


def history_samples(record):
    path = Path(record_path(record)).parent / "resources.jsonl"
    if not path.is_file():
        return []
    samples = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "sample":
            samples.append(event)
    return samples


def measurement_bounds(record):
    start = number(nested(record, "metadata", "measured_started_at_unix"))
    end = number(nested(record, "metadata", "measured_ended_at_unix"))
    return (
        int(start * 1000) if start is not None else None,
        int(end * 1000) if end is not None else None,
    )


def in_bounds(timestamp, start, end):
    return (
        timestamp is not None
        and (start is None or timestamp >= start)
        and (end is None or timestamp <= end)
    )


def cpu_history(samples, start=None, end=None):
    values, prior, seen = [], None, set()
    for sample in sorted(samples, key=lambda item: number(item.get("cpu_timestamp_ms")) or -1):
        timestamp, seconds, identity = (
            number(sample.get("cpu_timestamp_ms")),
            number(sample.get("cpu_seconds")),
            (sample.get("pod_uid"), sample.get("container_id")),
        )
        if not in_bounds(timestamp, start, end) or seconds is None or timestamp in seen:
            continue
        seen.add(timestamp)
        if prior:
            elapsed, delta = (timestamp - prior[0]) / 1000, seconds - prior[1]
            if elapsed > 0 and delta >= 0 and identity == prior[2]:
                values.append(((timestamp - (start or 0)) / 1000, 1000 * delta / elapsed))
        prior = (timestamp, seconds, identity)
    return values


def memory_history(samples, key, timestamp_key, start=None, end=None):
    values, seen = [], set()
    for sample in samples:
        timestamp, amount = number(sample.get(timestamp_key)), number(sample.get(key))
        if in_bounds(timestamp, start, end) and amount is not None and timestamp not in seen:
            seen.add(timestamp)
            values.append(((timestamp - (start or 0)) / 1000, amount / 1024**2))
    return sorted(values)


def plotly_javascript():
    from plotly.offline import get_plotlyjs

    return get_plotlyjs()


def json_for_script(value):
    return (
        json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def http_history(record):
    """Read the compact, optional timeline emitted beside a benchmark result."""
    path = Path(record_path(record)).parent / "http-history.json"
    unavailable = "HTTP timeline was not recorded; whole-run totals are available."
    if not path.is_file():
        capture = mapping(nested(record, "metadata", "http_capture"))
        if capture:
            raw_warnings = capture.get("warnings", [])
            warnings = (
                [f"HTTP capture: {item}" for item in raw_warnings if isinstance(item, str)]
                if isinstance(raw_warnings, list)
                else ["HTTP capture warnings are malformed."]
            )
            status = capture.get("status")
            if status in {"partial", "unavailable"}:
                return None, [
                    "HTTP timeline is unavailable; whole-run totals are available."
                ] + warnings
            return None, [
                "HTTP timeline is not available; whole-run totals are available."
            ] + warnings
        return None, [unavailable]
    try:
        history = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None, ["HTTP timeline could not be read; whole-run totals are available."]
    if not isinstance(history, dict) or history.get("schema_version") != 1:
        return None, ["HTTP timeline has an unsupported format; whole-run totals are available."]
    if history.get("status") not in {"complete", "partial"}:
        return None, ["HTTP timeline has an invalid status; whole-run totals are available."]
    if not isinstance(history.get("buckets"), list) or not isinstance(history.get("totals"), dict):
        return None, ["HTTP timeline is malformed; whole-run totals are available."]
    raw_warnings = history.get("warnings", [])
    if not isinstance(raw_warnings, list):
        return None, ["HTTP timeline warnings are malformed; whole-run totals are available."]
    warnings = [item for item in raw_warnings if isinstance(item, str)]
    if history["status"] == "partial":
        warnings.insert(0, "HTTP timeline is partial; it does not represent the full run.")
    return history, warnings


def http_history_figures(record, index):
    history, warnings = http_history(record)
    if history is None:
        return [], warnings
    buckets = [bucket for bucket in history["buckets"] if isinstance(bucket, dict)]
    if not buckets:
        return [], warnings + ["HTTP timeline contains no completed windows."]
    elapsed = [number(bucket.get("start_seconds")) for bucket in buckets]
    ends = [number(bucket.get("end_seconds")) for bucket in buckets]
    requests = [number(bucket.get("requests")) or 0 for bucket in buckets]
    hover_windows = [[end, count] for end, count in zip(ends, requests, strict=True)]
    latency = [mapping(bucket.get("latency_ms")) for bucket in buckets]
    latency_traces = []
    for name, key, color in (
        ("Median", "med", "#3977af"),
        ("p95", "p(95)", "#d56a27"),
        ("p99", "p(99)", "#8a4fb5"),
    ):
        values = [number(item.get(key)) for item in latency]
        if any(value is not None for value in values):
            latency_traces.append(
                {
                    "type": "scatter",
                    "mode": "lines+markers",
                    "name": name,
                    "x": elapsed,
                    "y": values,
                    "customdata": hover_windows,
                    "connectgaps": False,
                    "line": {"color": color},
                    "hovertemplate": "%{x:.3g}–%{customdata[0]:.3g} s: %{y:.3g} ms · %{customdata[1]} requests<extra></extra>",
                }
            )
    figures = []
    if latency_traces:
        figures.append(
            {
                "id": f"run-{index}-http-latency",
                "title": "HTTP latency by 5-second window",
                "data": latency_traces,
                "layout": {
                    "margin": {"l": 58, "r": 20, "t": 35, "b": 50},
                    "legend": {"orientation": "h", "y": 1.18},
                    "xaxis": {"title": {"text": "Elapsed time (s)"}},
                    "yaxis": {"title": {"text": "Latency (ms)"}, "rangemode": "tozero"},
                },
            }
        )
    statuses = sorted(
        {str(status) for bucket in buckets for status in mapping(bucket.get("statuses"))}
    )
    count_traces = []
    for status in statuses:
        values = [number(mapping(bucket.get("statuses")).get(status)) or 0 for bucket in buckets]
        label = (
            "No HTTP response"
            if status == "0"
            else "Unknown status"
            if status == "unknown"
            else f"HTTP {status}"
        )
        color = (
            "#2b8a3e"
            if status == "200"
            else "#c92a2a"
            if status == "0" or status.startswith(("4", "5"))
            else "#555"
        )
        count_traces.append(
            {
                "type": "bar",
                "name": label,
                "x": elapsed,
                "y": values,
                "customdata": hover_windows,
                "marker": {"color": color},
                "hovertemplate": "%{x:.3g}–%{customdata[0]:.3g} s: %{y} responses · %{customdata[1]} requests<extra>%{fullData.name}</extra>",
            }
        )
    dropped = [number(bucket.get("dropped_iterations")) or 0 for bucket in buckets]
    has_dropped = any(dropped)
    if has_dropped:
        count_traces.append(
            {
                "type": "scatter",
                "mode": "lines+markers",
                "name": "Not sent",
                "x": elapsed,
                "y": dropped,
                "customdata": hover_windows,
                "yaxis": "y2",
                "line": {"color": "#c92a2a"},
                "hovertemplate": "%{x:.3g}–%{customdata[0]:.3g} s: %{y} not sent · %{customdata[1]} requests<extra>Scheduled requests not sent</extra>",
            }
        )
    validation = [number(bucket.get("validation_failures")) or 0 for bucket in buckets]
    if any(validation):
        count_traces.append(
            {
                "type": "bar",
                "name": "Invalid response",
                "x": elapsed,
                "y": validation,
                "customdata": hover_windows,
                "marker": {"color": "#8a4fb5"},
                "hovertemplate": "%{x:.3g}–%{customdata[0]:.3g} s: %{y} validation failures · %{customdata[1]} requests<extra>Responses failing validation</extra>",
            }
        )
    if count_traces:
        top_margin = 50 if len(count_traces) <= 2 else 100
        figures.append(
            {
                "id": f"run-{index}-http-counts",
                "title": "HTTP responses and unsent requests by 5-second window",
                "data": count_traces,
                "layout": {
                    "barmode": "group",
                    "margin": {
                        "l": 58,
                        "r": 80 if has_dropped else 20,
                        "t": top_margin,
                        "b": 50,
                    },
                    "showlegend": True,
                    "legend": {
                        "orientation": "h",
                        "x": 0,
                        "xanchor": "left",
                        "y": 1.02,
                        "yanchor": "bottom",
                    },
                    "xaxis": {"title": {"text": "Elapsed time (s)"}},
                    "yaxis": {"title": {"text": "Responses / window"}, "rangemode": "tozero"},
                    **(
                        {
                            "yaxis2": {
                                "title": {"text": "Not sent / window"},
                                "overlaying": "y",
                                "side": "right",
                                "rangemode": "tozero",
                                "automargin": True,
                            }
                        }
                        if has_dropped
                        else {}
                    ),
                },
            }
        )
    return figures, warnings


def history_figures(record, index):
    samples = history_samples(record)
    if not samples:
        return []
    start, end = measurement_bounds(record)
    cpu = cpu_history(samples, start, end)
    working = memory_history(
        samples, "memory_working_set_bytes", "memory_working_set_timestamp_ms", start, end
    )
    rss = memory_history(samples, "memory_rss_bytes", "memory_rss_timestamp_ms", start, end)
    figures = []
    if cpu:
        figures.append(
            {
                "id": f"run-{index}-cpu",
                "title": "CPU",
                "data": [
                    {
                        "type": "scatter",
                        "mode": "lines+markers",
                        "name": "CPU",
                        "x": [v[0] for v in cpu],
                        "y": [v[1] for v in cpu],
                        "line": {
                            "color": RUNTIME_COLORS.get(
                                nested(record, "metadata", "implementation"), "#555"
                            )
                        },
                        "hovertemplate": "%{x:.3g} s: %{y:.3g} millicores<extra></extra>",
                    }
                ],
                "layout": {
                    "margin": {"l": 58, "r": 20, "t": 25, "b": 50},
                    "xaxis": {"title": {"text": "Elapsed time (s)"}},
                    "yaxis": {"title": {"text": "millicores"}, "rangemode": "tozero"},
                },
            }
        )
    if working or rss:
        traces = []
        for name, values, color in (("Working set", working, "#3977af"), ("RSS", rss, "#d56a27")):
            if values:
                traces.append(
                    {
                        "type": "scatter",
                        "mode": "lines+markers",
                        "name": name,
                        "x": [v[0] for v in values],
                        "y": [v[1] for v in values],
                        "line": {"color": color},
                        "hovertemplate": "%{x:.3g} s: %{y:.3g} MiB<extra></extra>",
                    }
                )
        figures.append(
            {
                "id": f"run-{index}-memory",
                "title": "Memory",
                "data": traces,
                "layout": {
                    "margin": {"l": 58, "r": 20, "t": 50, "b": 50},
                    "legend": {
                        "orientation": "h",
                        "x": 0,
                        "xanchor": "left",
                        "y": 1.02,
                        "yanchor": "bottom",
                    },
                    "xaxis": {"title": {"text": "Elapsed time (s)"}},
                    "yaxis": {"title": {"text": "MiB"}, "rangemode": "tozero"},
                },
            }
        )
    return figures


def label(field):
    return field.replace("_", " ").replace("rps", "RPS").capitalize()


def field_value(row, field, suffix=""):
    value = row.get(field)
    if field in {"error_rate", "check_failure_rate"} and number(value) is not None:
        return html.escape(f"{display(value * 100)}%")
    return html.escape(f"{display(value)}{suffix}")


def human_failure_reasons(record):
    settings = mapping(nested(record, "metadata", "settings"))
    result = mapping(record.get("result")) if isinstance(record, dict) else {}
    failed = result.get("thresholds_failed")
    failed = failed if isinstance(failed, list) else []
    operation_p95 = mapping(result.get("operation_p95_ms"))
    limit = number(settings.get("p95_ms"))
    reasons = []
    for threshold in failed:
        text = str(threshold)
        prefix = "http_req_duration{operation:"
        if text.startswith(prefix) and text.endswith("}"):
            operation = text[len(prefix) : -1]
            actual = number(operation_p95.get(operation))
            if actual is not None and limit is not None:
                reasons.append(
                    f"{operation} p95 {display(actual)} ms exceeded configured {display(limit)} ms"
                )
            else:
                reasons.append(f"{operation} latency target failed")
        elif text == "dropped_iterations":
            dropped = number(result.get("dropped_iterations"))
            if dropped is not None:
                reasons.append(f"{display(dropped)} scheduled requests were not sent")
            else:
                reasons.append("scheduled requests were not sent")
        elif text == "http_req_failed":
            reasons.append("HTTP failure-rate target failed")
        elif text == "checks":
            reasons.append("validation-check target failed")
        elif text == "http_req_duration":
            reasons.append("HTTP latency target failed")
        else:
            reasons.append(text)
    return reasons


def run_status(record):
    target_reasons = human_failure_reasons(record)
    infrastructure = validation_errors(record)
    warnings = nested(record, "resource", "warnings")
    if isinstance(warnings, list) and warnings:
        infrastructure.extend(
            f"resources: {item}" for item in warnings if item in ESSENTIAL_RESOURCE_WARNINGS
        )
    if infrastructure:
        return "Recording incomplete", infrastructure + target_reasons, "failed"
    if target_reasons:
        return "Failed targets", target_reasons, "failed"
    if not is_healthy(record):
        raw_status = record.get("status") if isinstance(record, dict) else "missing"
        return "Recording incomplete", [f"record status is {display(raw_status)}"], "failed"
    return "Passed targets", [], "healthy"


def run_card(record, index, output=None):
    row = record_row(record)
    coverage = mapping(nested(record, "resource", "coverage"))
    if coverage:
        row["resource_coverage"] = (
            f"{display(coverage.get('cpu_span_seconds'))}s of {display(coverage.get('measurement_seconds'))}s"
        )
    status_label, reasons, status = run_status(record)
    overview = (
        ("Actual RPS", "actual_rps", ""),
        ("Requests", "requests", ""),
        ("p50", "p50_ms", " ms"),
        ("p95", "p95_ms", " ms"),
        ("p99", "p99_ms", " ms"),
        ("HTTP failure rate", "error_rate", ""),
        ("Validation checks failed", "check_failure_rate", ""),
        ("CPU coverage", "resource_coverage", ""),
        ("Dropped (not sent)", "dropped_iterations", ""),
        ("Mean CPU", "mean_cpu_millicores", " millicores"),
        ("Peak working set", "max_sampled_working_set_mib", " MiB"),
        ("Throttled periods", "throttled_periods_percent", "%"),
    )
    facts = "".join(
        f"<div><dt>{name}</dt><dd>{field_value(row, field, suffix)}</dd></div>"
        for name, field, suffix in overview
    )
    details = "".join(
        f"<div><dt>{html.escape(label(field))}</dt><dd>{field_value(row, field)}</dd></div>"
        for field in CSV_FIELDS
    )
    history = history_figures(record, index)
    resource_charts = "".join(
        f"<figure><figcaption>{html.escape(item['title'])}</figcaption><div class='chart' id='{item['id']}'></div></figure>"
        for item in history
    )
    history_heading = (
        "<h3>Resource history</h3><div class='history-grid'>" + resource_charts + "</div>"
        if resource_charts
        else "<p class='history-unavailable'>Resource histories are unavailable for this run.</p>"
    )
    http_figures, http_warnings = http_history_figures(record, index)
    http_charts = "".join(
        f"<figure><figcaption>{html.escape(item['title'])}</figcaption><div class='chart' id='{item['id']}'></div></figure>"
        for item in http_figures
    )
    http_detail = (
        "<details class='http-history'><summary>HTTP timeline</summary>"
        + "<p class='history-note'>Each point is a 5-second completion window sampled by completion timestamp; the final window can be shorter. Its latency p95 combines completed requests in that window, so it differs from the per-endpoint target. Each window counts responses. Validation failures can overlap HTTP failures; check failures use a different whole-run denominator.</p>"
        + "".join(f"<p class='failure'>{html.escape(warning)}</p>" for warning in http_warnings)
        + (f"<div class='history-grid'>{http_charts}</div>" if http_charts else "")
        + "</details>"
    )
    failure = "".join(f"<p class='failure'>{html.escape(reason)}</p>" for reason in reasons)
    diagnostic_detail = diagnostics_detail(record, output or Path(record_path(record)).parent)
    card = (
        f"<article class='run {status}'><header><h3>{html.escape(row['implementation'])}</h3>"
        f"<p>{html.escape(group_label(record))}</p>"
        f"<p>{html.escape(row['run_id'])} · <strong>{html.escape(status_label)}</strong> · warmup {html.escape(display(row['warmup_duration']))} · {html.escape(display(row['preallocated_vus']))} preallocated VUs</p></header>{failure}"
        f"<dl class='facts'>{facts}</dl>{history_heading}{http_detail}{diagnostic_detail}<details><summary>All recorded fields</summary><dl class='details'>{details}</dl></details></article>"
    )
    return card, history + http_figures


def aggregate_value(stats, suffix=""):
    if stats["median"] is None:
        return "NA"
    return (
        f"{display(stats['median'])}{suffix} median · {display(stats['minimum'])}–"
        f"{display(stats['maximum'])}{suffix}"
    )


def comparison_card(summary):
    runtime_cards = []
    for runtime, value in summary["runtimes"].items():
        metrics = (
            ("p95", value["p95_ms"], " ms"),
            ("Mean CPU", value["mean_cpu_millicores"], " millicores"),
            ("Working set", value["working_set_mib"], " MiB"),
        )
        facts = "".join(
            f"<div><dt>{name}</dt><dd>{aggregate_value(stats, suffix)}</dd></div>"
            for name, stats, suffix in metrics
        )
        runtime_cards.append(
            f"<section class='aggregate-runtime'><h3>{html.escape(runtime)}</h3>"
            f"<p>{len(value['healthy_runs'])} healthy / {len(value['runs'])} total</p>"
            f"<dl class='aggregate-facts'>{facts}</dl></section>"
        )
    return (
        "<details class='aggregate'><summary>"
        + html.escape(summary["label"])
        + "</summary><div class='aggregate-grid'>"
        + "".join(runtime_cards)
        + "</div></details>"
    )


def render(records, output):
    output.mkdir(parents=True, exist_ok=True)
    write_csv(records, output)
    summaries, _ = summarize(records)
    charts = []
    cards = []
    for index, record in enumerate(records, 1):
        card, histories = run_card(record, index, output)
        cards.append(card)
        charts.extend(histories)
    style = (Path(__file__).parent / "report.css").read_text()
    document = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>HTTP benchmark comparison</title><style>"
        + style
        + "</style></head><body><main>"
    )
    implementations = {
        nested(record, "metadata", "implementation")
        for record in records
        if nested(record, "metadata", "implementation") in {"go", "bun"}
    }
    awaiting = sorted({"go", "bun"} - implementations)
    awaiting_text = " Awaiting " + ", ".join(awaiting) + " runs." if awaiting else ""
    has_http_capture = any(
        bool(mapping(nested(record, "metadata", "http_capture"))) for record in records
    )
    capture_note = (
        " Some records use K6 JSON capture for the HTTP timeline, which adds local Mac I/O; it is client-side telemetry, not a server workload change. Legacy Go runs did not export this capture."
        if has_http_capture
        else ""
    )
    document += (
        "<h1>HTTP benchmark comparison</h1><p>Whole-run latency and throughput summaries are shown for every run. Newer captures may include an optional HTTP timeline; legacy runs do not. CPU and memory charts show sampled usage during each measured run. HTTP failures, validation failures, and unsent scheduled requests are separate measures. Missing data is shown as NA. Failed and malformed runs remain visible and are excluded from healthy aggregates."
        + capture_note
        + awaiting_text
        + " <a href='comparison.csv'>Download full CSV</a>.</p>"
    )
    document += (
        "<h2>Per-run results</h2><p>CPU values are averages between source readings. Memory samples show working set and RSS; histories use elapsed time from the measured run.</p>"
        + "".join(cards)
    )
    comparisons = [
        summary
        for summary in summaries
        if sum(len(value["runs"]) for value in summary["runtimes"].values()) >= 2
    ]
    if comparisons:
        document += (
            "<h2>Matching-run aggregates</h2><p>Values are medians across healthy runs; ranges are those runs’ min–max values, not pooled request percentiles.</p>"
            + "".join(comparison_card(summary) for summary in comparisons)
        )
    document += (
        "</main><script>"
        + plotly_javascript()
        + "</script><script>window.reportCharts="
        + json_for_script(charts)
        + ";</script><script>"
        + (Path(__file__).parent / "report.js").read_text()
        + "</script></body></html>"
    )
    (output / "comparison.html").write_text(document)


def arguments():
    parser = argparse.ArgumentParser(
        description="Create an offline HTTP benchmark comparison report"
    )
    parser.add_argument("--results-dir", default="results/http")
    parser.add_argument("--output-dir", default="results/http/report")
    return parser.parse_args()


def main():
    args = arguments()
    records = load_records(args.results_dir)
    if not records:
        print(f"no result.json files under {args.results_dir}")
        return 1
    render(records, Path(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
