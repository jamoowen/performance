"""Local handling for optional, non-ranking runtime diagnostics."""

import base64
import json
import math
import subprocess

MAX_PROFILE_BYTES = 20 * 1024 * 1024
MAX_TOP_BYTES = 10 * 1024
PROFILE_NAMES = {
    "go": {"cpu.pprof"}
    | {
        f"{kind}-{phase}.pprof"
        for kind in ("heap", "allocs", "goroutine", "block", "mutex")
        for phase in ("before", "after")
    },
    "bun": {"jsc-cpu.json"},
}


def write_event(file, event):
    file.write(json.dumps(event, sort_keys=True) + "\n")
    file.flush()


def save_profile(directory, runtime, name, encoded):
    if name not in PROFILE_NAMES.get(runtime, set()):
        raise ValueError("unsupported diagnostic profile filename")
    if not isinstance(encoded, str) or len(encoded) > MAX_PROFILE_BYTES * 2:
        raise ValueError("diagnostic profile encoding exceeds limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid diagnostic profile encoding") from error
    if not data or len(data) > MAX_PROFILE_BYTES:
        raise ValueError("diagnostic profile exceeds 20 MiB or is empty")
    if runtime == "go" and (len(data) < 2 or data[:2] != b"\x1f\x8b"):
        raise ValueError("Go diagnostic profile is not gzip data")
    if runtime == "bun":
        try:
            profile = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Bun diagnostic profile is not JSON") from error
        if not isinstance(profile, dict):
            raise ValueError("Bun diagnostic profile is not an object")
        traces = profile.get("stackTraces")
        if (
            profile.get("format") != "bun:jsc.profile"
            or not isinstance(profile.get("functions"), str)
            or not isinstance(traces, dict)
            or not isinstance(traces.get("traces"), list)
        ):
            raise ValueError("Bun diagnostic profile is missing functions or traces")
    path = directory / name
    path.write_bytes(data)
    return path


def write_cpu_top(directory, runtime):
    profile = directory / ("cpu.pprof" if runtime == "go" else "jsc-cpu.json")
    if not profile.is_file() or profile.parent != directory:
        return None, "CPU profile is unavailable"
    destination = directory / "cpu-top.txt"
    try:
        if runtime == "go":
            completed = subprocess.run(
                ["go", "tool", "pprof", "-top", "-nodecount=15", str(profile)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=True,
            )
            data = completed.stdout.encode()[:MAX_TOP_BYTES]
        elif runtime == "bun":
            functions = json.loads(profile.read_text()).get("functions")
            if not isinstance(functions, str):
                return None, "Bun CPU profile has no functions"
            data = functions[:MAX_TOP_BYTES]
        else:
            return None, "unsupported runtime"
        if not data:
            return None, "CPU top output is empty"
        if isinstance(data, bytes):
            destination.write_bytes(data)
        else:
            destination.write_text(data)
        return destination.name, None
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError) as error:
        return None, str(error)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _counter_delta(samples, section, source, target, result):
    values = [
        sample.get(section, {}).get(source) if isinstance(sample.get(section), dict) else None
        for sample in samples
    ]
    if not all(_number(value) and value >= 0 for value in values):
        result["warnings"].append(f"invalid {source}; delta omitted")
    elif any(current < previous for previous, current in zip(values, values[1:], strict=False)):
        result["warnings"].append(f"reset {source}; delta omitted")
    else:
        result[target] = values[-1] - values[0]


def diagnostics_summary(events, runtime=None):
    samples = [
        event.get("runtime")
        for event in events
        if event.get("type") == "snapshot" and isinstance(event.get("runtime"), dict)
    ]
    selected = runtime or (samples[0].get("runtime") if samples else None)
    warnings, valid, invalid_sample = [], [], False
    for sample in samples:
        if sample.get("schema_version") != 1 or sample.get("runtime") != selected:
            warnings.append("runtime sample schema or runtime does not match")
            invalid_sample = True
        elif (
            not isinstance(sample.get("process_id"), int)
            or isinstance(sample.get("process_id"), bool)
            or sample["process_id"] <= 0
        ):
            warnings.append("runtime sample has missing or invalid process identity")
            invalid_sample = True
        elif not _number(sample.get("time_unix")):
            warnings.append("runtime sample has invalid timestamp")
            invalid_sample = True
        else:
            valid.append(sample)
    if not valid:
        return {
            "status": "unavailable",
            "warnings": list(dict.fromkeys(warnings or ["no valid runtime samples"])),
        }
    result = {
        "status": "complete",
        "sample_count": len(valid),
        "warnings": warnings,
        "process_id": valid[0]["process_id"],
    }
    identity_ok = len({sample["process_id"] for sample in valid}) == 1
    time_ok = all(
        current["time_unix"] > previous["time_unix"]
        for previous, current in zip(valid, valid[1:], strict=False)
    )
    if not identity_ok:
        warnings.append("runtime process identity changed; identity-dependent deltas omitted")
    if not time_ok:
        warnings.append("runtime sample timestamps are not increasing; deltas omitted")
    if len(valid) < 2:
        warnings.append("at least two runtime samples are required for rates and deltas")
    deltas_ok = identity_ok and time_ok and len(valid) >= 2 and not invalid_sample
    if not deltas_ok:
        result["status"] = "partial"
    if selected == "go":
        go = [sample.get("go") for sample in valid if isinstance(sample.get("go"), dict)]
        result["actual_gomaxprocs"] = next(
            (
                item.get("gomaxprocs")
                for item in go
                if _number(item.get("gomaxprocs")) and item["gomaxprocs"] > 0
            ),
            None,
        )
        result["go_version"] = next(
            (item.get("version") for item in go if isinstance(item.get("version"), str)), None
        )
        result["go_heap_alloc_peak_bytes"] = max(
            (
                item.get("heap_alloc_bytes")
                for item in go
                if _number(item.get("heap_alloc_bytes")) and item["heap_alloc_bytes"] >= 0
            ),
            default=None,
        )
        if deltas_ok:
            for source, target in (
                ("gc_cycles", "go_gc_cycles_delta"),
                ("gc_pause_total_ns", "go_gc_pause_ns_delta"),
                ("total_alloc_bytes", "go_total_alloc_bytes_delta"),
            ):
                _counter_delta(valid, "go", source, target, result)
            duration = valid[-1]["time_unix"] - valid[0]["time_unix"]
            if _number(result.get("go_total_alloc_bytes_delta")) and duration > 0:
                result["go_allocation_bytes_per_second"] = (
                    result["go_total_alloc_bytes_delta"] / duration
                )
    elif selected == "bun":
        bun = [sample.get("bun") for sample in valid if isinstance(sample.get("bun"), dict)]
        result["bun_heap_peak_bytes"] = max(
            (
                item.get("heap_size_bytes")
                for item in bun
                if _number(item.get("heap_size_bytes")) and item["heap_size_bytes"] >= 0
            ),
            default=None,
        )
        result["bun_event_loop_delay_p95_max_ms"] = max(
            (
                item.get("event_loop_delay_p95_ms")
                for item in bun
                if _number(item.get("event_loop_delay_p95_ms"))
                and item["event_loop_delay_p95_ms"] >= 0
            ),
            default=None,
        )
    if deltas_ok:
        duration = valid[-1]["time_unix"] - valid[0]["time_unix"]
        result["coverage_seconds"] = duration
        process = [sample.get("process_cpu") for sample in valid]
        if any(item is None for item in process):
            warnings.append("process CPU is unavailable on this platform")
        elif all(
            isinstance(item, dict)
            and _number(item.get("user_us"))
            and _number(item.get("system_us"))
            and item["user_us"] >= 0
            and item["system_us"] >= 0
            for item in process
        ):
            totals = [item["user_us"] + item["system_us"] for item in process]
            users = [item["user_us"] for item in process]
            systems = [item["system_us"] for item in process]
            if (
                all(
                    current >= previous for previous, current in zip(users, users[1:], strict=False)
                )
                and all(
                    current >= previous
                    for previous, current in zip(systems, systems[1:], strict=False)
                )
                and duration > 0
            ):
                result["process_cpu_seconds_per_second"] = (totals[-1] - totals[0]) / 1e6 / duration
            else:
                warnings.append("reset process CPU; delta omitted")
        else:
            warnings.append("invalid process CPU; delta omitted")
        if selected != "bun":
            _counter_delta(valid, "database", "wait_count", "database_wait_count_delta", result)
            _counter_delta(
                valid, "database", "wait_duration_ns", "database_wait_seconds_delta", result
            )
            if _number(result.get("database_wait_seconds_delta")):
                result["database_wait_seconds_delta"] /= 1e9
    cpu_name = "cpu.pprof" if selected == "go" else "jsc-cpu.json"
    profiles = [
        event
        for event in events
        if event.get("type") == "profile"
        and event.get("name") == cpu_name
        and event.get("saved")
        and _number(event.get("wallclock_start"))
        and _number(event.get("wallclock_end"))
        and event["wallclock_end"] >= event["wallclock_start"]
    ]
    if profiles:
        result["capture_window"] = {
            "start": min(event["wallclock_start"] for event in profiles),
            "end": max(event["wallclock_end"] for event in profiles),
        }
    result["warnings"] = list(dict.fromkeys(warnings))
    if result["warnings"] and result["status"] == "complete":
        result["status"] = "partial"
    return result


def write_summary(path, events, runtime=None):
    summary = diagnostics_summary(events, runtime)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary
