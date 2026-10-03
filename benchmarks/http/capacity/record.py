"""Durably record one adaptive capacity attempt.

Each k6 invocation is intentionally bounded to a single 90 second step.  A
checkpoint is written before and after every invocation, so an interruption
does not turn already completed stages into a synthetic failed run.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import resource
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.http.ramp.measure.collector import GeneratorCollector, weighted_window
from benchmarks.http.ramp.measure.normalize import normalize_k6
from benchmarks.http.ramp.measure.record import (
    _align_samples,
    _drain_integrity,
    _k6_version,
    _metadata,
    _seed_total,
    _stock_outcomes,
    _validate_integrity,
    _warmup_valid,
    clock_alignment,
    load_pod,
    request,
    route_interface,
)
from benchmarks.http.ramp.measure.schedule import Stage

from .protocol import (
    COLLECTOR_DURATION_SECONDS,
    MEASURED_VUS,
    SAFETY_CEILING_RPS,
    STABLE_SECONDS,
    TRANSITION_SECONDS,
    WARMUP_RPS,
    WARMUP_SECONDS,
    WARMUP_VUS,
    Step,
    initial_steps,
    latency_failed,
    next_target,
    overload_from_history,
    overload_from_window,
    protocol_hash,
)

LOAD_SCRIPT = Path(__file__).with_name("load.js")
GIB = 1024**3


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_hash() -> str:
    import hashlib

    return hashlib.sha256(LOAD_SCRIPT.read_bytes()).hexdigest()


def required_available_memory(vus: int) -> int:
    return GIB + int(vus * 0.45 * 1024 * 1024)


def nofile_budget(vus: int) -> int:
    """Budget k6 descriptors for sockets plus transient per-VU overhead.

    The 90% emergency guard needs spare descriptors itself, so the requested
    soft limit is deliberately larger than the expected two-per-VU footprint.
    """
    return max(8192, math.ceil((2 * vus + 512) / 0.8))


def _kernel_nofile_limit(inherited_soft: int) -> int | None:
    if platform.system() != "Darwin":
        return None
    try:
        reply = subprocess.run(
            ["sysctl", "-n", "kern.maxfilesperproc"],
            text=True,
            capture_output=True,
            check=True,
            timeout=3,
        )
        return int(reply.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        # Do not probe or raise a process limit when the OS ceiling is
        # unavailable; inherited soft is the conservative safe ceiling.
        return inherited_soft


def preflight(results_dir: Path, vus: int, *, campaign_start: bool = False) -> dict[str, int]:
    try:
        import psutil
    except ImportError as error:  # pragma: no cover - dependency supplied by command
        raise RuntimeError("psutil is required for capacity preflight") from error
    disk = shutil.disk_usage(results_dir)
    available = psutil.virtual_memory().available
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    required_fds = nofile_budget(vus)
    kernel_limit = _kernel_nofile_limit(soft)
    if disk.free < (20 * GIB if campaign_start else 2 * GIB):
        raise RuntimeError(
            "generator_limit_disk_start" if campaign_start else "generator_limit_disk"
        )
    if available < required_available_memory(vus):
        raise RuntimeError("generator_limit_memory_step")
    if hard != resource.RLIM_INFINITY and hard < required_fds:
        raise RuntimeError("generator_limit_nofile")
    if kernel_limit is not None and kernel_limit < required_fds:
        raise RuntimeError("generator_limit_nofile")
    return {
        "diskFreeBytes": disk.free,
        "availableMemoryBytes": available,
        "nofileSoft": soft,
        "nofileHard": hard,
        "nofileRequired": required_fds,
        "kernelNofileLimit": kernel_limit,
    }


def _k6_preexec(vus: int):
    """Set only the k6 child's soft FD limit; never modify the host limit."""
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = nofile_budget(vus)
    kernel_limit = _kernel_nofile_limit(_soft)
    if kernel_limit is not None and limit > kernel_limit:
        raise RuntimeError("generator_limit_nofile")
    if hard != resource.RLIM_INFINITY and hard < limit:
        raise RuntimeError("generator_limit_nofile")

    def set_limit() -> None:
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))

    return set_limit, limit, kernel_limit


def _k6_command(a: Any, mode: str, step: Step | None, output: Path, summary: Path) -> list[str]:
    command = [
        a.k6,
        "run",
        "--out",
        f"json={output}",
        "--summary-export",
        str(summary),
        str(LOAD_SCRIPT),
    ]
    return command


def _k6_env(a: Any, mode: str, step: Step | None) -> dict[str, str]:
    value = {"BASE_URL": a.base_url, "MODE": mode, "SEED_COUNT": "5000"}
    if step:
        value.update(
            {
                "TARGET_RPS": str(step.target_rps),
                "START_RPS": str(getattr(a, "start_rps", step.target_rps)),
                "TRANSITION_SECONDS": str(step.transition_seconds),
                "STABLE_SECONDS": str(step.stable_seconds),
                "SETTLING_SECONDS": str(step.settling_seconds),
                "PREALLOCATED_VUS": str(step.vus),
            }
        )
    return value


def _generator_limit(samples: list[dict[str, Any]], disk_free: int, fd_limit: int) -> str | None:
    """Check the five consecutive sample emergency guard without host mutation."""
    run_threads = run_memory = run_cpu = run_fds = 0
    for sample in samples:
        if sample.get("numThreads", 0) >= 2500:
            run_threads += 1
        else:
            run_threads = 0
        if sample.get("availableMemoryBytes", float("inf")) < 512 * 1024 * 1024:
            run_memory += 1
        else:
            run_memory = 0
        host_cpu = sample.get("hostCpuPercent", 0)
        if isinstance(host_cpu, list):
            host_cpu = sum(host_cpu) / max(1, len(host_cpu))
        if host_cpu >= 95:
            run_cpu += 1
        else:
            run_cpu = 0
        num_fds = sample.get("numFds")
        if isinstance(num_fds, (int, float)) and num_fds >= fd_limit * 0.9:
            run_fds += 1
        else:
            run_fds = 0
        if run_threads >= 1:
            return "generator_limit_threads"
        if run_memory >= 5:
            return "generator_limit_memory"
        if run_cpu >= 5:
            return "generator_limit_cpu"
        if run_fds >= 5:
            return "generator_limit_nofile"
    return "generator_limit_disk" if disk_free < 2 * GIB else None


def _stop_owned_k6(process: Any) -> bool:
    """Stop only this recorder's child and prove it is gone before continuing."""
    if process.poll() is not None:
        return True
    for signal_value, timeout in ((signal.SIGINT, 8), (signal.SIGTERM, 5), (signal.SIGKILL, 30)):
        try:
            process.send_signal(signal_value)
        except ProcessLookupError:
            return True
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            continue
    return process.poll() is not None


def _write_cleanup_failure(directory: Path, mode: str, reason: str | None = None) -> None:
    """Durably identify the one condition that must halt the whole campaign."""
    value: dict[str, Any] = {"code": "owned_process_cleanup_failure", "mode": mode}
    if reason:
        value["reason"] = reason
    _write_json(directory / "owned-process-cleanup-failure.json", value)


def _write_recorder_diagnostic(directory: Path, mode: str, error: BaseException) -> None:
    """Keep a safe, private exception classification beside the raw k6 logs."""
    try:
        _write_json(
            directory / "recorder-diagnostic.json",
            {"code": "run_k6_exception", "mode": mode, "exceptionClass": type(error).__name__},
        )
    except OSError:
        # Diagnostics must never prevent owned-child cleanup.
        pass


def run_k6(a: Any, mode: str, step: Step | None, directory: Path) -> dict[str, Any]:
    name = "warmup" if mode == "warmup" else f"step-{step.target_rps}"
    output, summary, log = (
        directory / f"{name}.json.gz",
        directory / f"{name}-summary.json",
        directory / f"{name}.log",
    )
    child_preexec, fd_limit, kernel_fd_limit = _k6_preexec(
        WARMUP_VUS if mode == "warmup" else step.vus
    )
    generator_dir = directory / f"{name}-generator"
    generator_dir.mkdir(mode=0o700)
    interface = route_interface(a.base_url)
    with log.open("w") as destination:
        os.chmod(log, 0o600)
        process = subprocess.Popen(
            _k6_command(a, mode, step, output, summary),
            env={**os.environ, **_k6_env(a, mode, step)},
            stdout=destination,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=child_preexec,
        )
        start_ns = time.time_ns()
        try:
            collector = GeneratorCollector(process.pid, interface, log_dir=generator_dir).start()
        except BaseException as error:
            if not _stop_owned_k6(process):
                _write_cleanup_failure(directory, mode, "collector_initialization")
            _write_recorder_diagnostic(directory, mode, error)
            raise
        stopped = None
        cleanup_attempted = False
        try:
            while process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    samples = getattr(collector, "samples", [])
                    stopped = _generator_limit(samples, shutil.disk_usage(directory).free, fd_limit)
                    if stopped:
                        cleanup_attempted = True
                        if not _stop_owned_k6(process):
                            _write_cleanup_failure(directory, mode, stopped)
                            raise RuntimeError("owned_process_cleanup_failure") from None
                        break
        except BaseException as error:
            if process.poll() is None and not cleanup_attempted:
                cleanup_attempted = True
                if not _stop_owned_k6(process):
                    _write_cleanup_failure(directory, mode)
            _write_recorder_diagnostic(directory, mode, error)
            raise
        finally:
            collector.stop()
            generator = collector.join(timeout=15)
        return {
            "returncode": process.returncode,
            "generator": generator,
            "generatorLimit": stopped,
            "childNofileLimit": fd_limit,
            "kernelNofileLimit": kernel_fd_limit,
            "generatorLog": str(generator_dir / "generator-telemetry.jsonl"),
            "startNs": start_ns,
            "endNs": time.time_ns(),
            "output": output.name,
            "summary": summary.name,
            "log": log.name,
        }


def _safe_normalize(directory: Path, capture: dict[str, Any], step: Step) -> dict[str, Any]:
    output, summary = directory / capture["output"], directory / capture["summary"]
    try:
        return normalize_k6(
            str(output),
            json.loads(summary.read_text()),
            [
                Stage(
                    step.target_rps,
                    step.transition_seconds,
                    step.stable_seconds,
                    step.settling_seconds,
                )
            ],
        )
    except (OSError, json.JSONDecodeError, EOFError):
        return {
            "validity": {"status": "invalid", "reasons": ["partial_k6_capture"]},
            "windows": [],
            "history": [],
            "counts": {},
            "scenarioOrigin": None,
        }


def _shift(records: list[dict[str, Any]], offset: float) -> list[dict[str, Any]]:
    shifted = []
    for record in records:
        copy = dict(record)
        for key in ("seconds", "startSeconds", "endSeconds"):
            if isinstance(copy.get(key), (int, float)):
                copy[key] += offset
        shifted.append(copy)
    return shifted


def _capacity(stages: list[dict[str, Any]], stop_reason: str | None) -> dict[str, Any]:
    passing = no_overload = None
    first_overload = first_latency = None
    for position, entry in enumerate(stages):
        rate = entry["targetRps"]
        normalized, overload = entry.get("normalized", {}), entry.get("overload", {})
        window = next(iter(normalized.get("windows", [])), {})
        if latency_failed(window) and first_latency is None:
            first_latency = rate
        if overload.get("status") or (
            stop_reason == "pod_restart_or_oom" and position == len(stages) - 1
        ):
            first_overload = rate if first_overload is None else first_overload
        elif entry.get("completed") and normalized.get("validity", {}).get("status") == "valid":
            no_overload = rate
            if window.get("slo", {}).get("status") == "pass":
                passing = rate
    return {
        "highestPassingRps": passing,
        "highestNoOverloadRps": no_overload,
        "firstOverloadRps": first_overload,
        "firstLatencyFailureRps": first_latency,
        "stopReason": stop_reason,
        "generatorLimited": bool(stop_reason and stop_reason.startswith("generator_limit")),
    }


def run(a: Any) -> dict[str, Any]:
    """Run warmup then bounded steps.  ``a`` is argparse-compatible for tests."""
    out = a.results_dir / a.attempt_id
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    _write_json(
        out / "manifest.json",
        {
            **vars(a),
            "protocol": {
                "measuredVus": MEASURED_VUS,
                "warmupVus": WARMUP_VUS,
                "collectorDurationSeconds": COLLECTOR_DURATION_SECONDS,
            },
        },
    )
    before = request(a.base_url, "/benchmark/integrity")
    if (
        before.get("rows") != 5000
        or before.get("totalRevisions") != 0
        or before.get("totalStock") != _seed_total()
    ):
        raise RuntimeError("database is not fresh")
    # The shared metadata validator also records a schedule hash. Here that
    # identity is a one-step schedule; protocolHash identifies the full search.
    a.schedule_json = json.dumps([Step(300, 0, STABLE_SECONDS, TRANSITION_SECONDS).as_k6_stage()])
    a.local_only = False
    info = request(a.base_url, "/benchmark/info")
    metadata = _metadata(a, info)
    metadata.update(
        {
            "loadHash": load_hash(),
            "protocolHash": protocol_hash(a.safety_ceiling),
            "generator": {"k6Version": _k6_version(a.k6)},
            "measuredVus": MEASURED_VUS,
            "warmupVus": WARMUP_VUS,
            "maxVus": MEASURED_VUS,
            "collectorDurationSeconds": COLLECTOR_DURATION_SECONDS,
        }
    )
    checkpoint: dict[str, Any] = {"attemptId": a.attempt_id, "status": "in_progress", "stages": []}
    _write_json(out / "checkpoint.json", checkpoint)
    # PodCollector is supplied by the telemetry worker.  Keep this import late so
    # protocol-only tests don't need remote collection dependencies.
    from .telemetry import PodCollector

    clock = clock_alignment(a.ssh_host)
    identity = load_pod(a)
    remote = PodCollector(
        a.ssh_host,
        a.namespace,
        identity["pod"],
        "http-ramp",
        identity["uid"],
        identity["containerId"],
        COLLECTOR_DURATION_SECONDS,
        log_dir=out,
    ).start()
    if not remote._metadata_event.wait(10) or remote.errors:
        remote.stop()
        remote.join(timeout=15)
        raise RuntimeError("remote_collector_initialization_failed")
    stages: list[dict[str, Any]] = []
    all_windows: list[dict[str, Any]] = []
    all_history: list[dict[str, Any]] = []
    offset = 0.0
    measured_origin: float | None = None
    stop_reason = None
    recorder_errors: list[str] = []
    warm = {"generator": {"coverage": 0, "errors": [], "samples": []}}
    acknowledged = failed = 0
    try:
        preflight(a.results_dir, WARMUP_VUS, campaign_start=True)
        checkpoint["current"] = {"kind": "warmup", "status": "running"}
        _write_json(out / "checkpoint.json", checkpoint)
        warm = run_k6(a, "warmup", None, out)
        warm_normalized = _safe_normalize(out, warm, Step(WARMUP_RPS, 0, WARMUP_SECONDS))
        _write_json(out / "warmup-normalized.json", warm_normalized)
        if warm["returncode"] or warm["generatorLimit"] or not _warmup_valid(warm_normalized):
            raise RuntimeError("warmup_invalid")
        warm_stock = _stock_outcomes(out / warm["output"])
        acknowledged += warm_stock["acknowledged"]
        failed += warm_stock["failed"]
        planned = initial_steps()
        index = 0
        while index < len(planned):
            step = planned[index]
            try:
                step_preflight = preflight(a.results_dir, step.vus)
            except RuntimeError as error:
                stop_reason = str(error)
                break
            # A new k6 process still starts at the prior offered rate. The
            # first measured process is intentionally flat at 300 RPS.
            a.start_rps = stages[-1]["targetRps"] if stages else step.target_rps
            checkpoint["current"] = {
                "kind": "step",
                "stageIndex": index,
                "targetRps": step.target_rps,
                "vus": step.vus,
                "status": "running",
            }
            _write_json(out / "checkpoint.json", checkpoint)
            print(
                json.dumps({"event": "capacity_step_started", **checkpoint["current"]}), flush=True
            )
            capture = run_k6(a, "step", step, out)
            normalized = _safe_normalize(out, capture, step)
            scenario_origin = normalized.get("scenarioOrigin")
            if isinstance(scenario_origin, (int, float)):
                if measured_origin is None:
                    measured_origin = float(scenario_origin)
                offset = max(0.0, float(scenario_origin) - measured_origin)
            histories = _shift(normalized.get("history", []), offset)
            windows = _shift(normalized.get("windows", []), offset)
            overload = (
                overload_from_history(normalized.get("history", []), step.target_rps)
                if normalized.get("validity", {}).get("status") == "valid"
                else {"status": False, "reasons": ["incomplete_capture"], "buckets": 0}
            )
            window = next(iter(normalized.get("windows", [])), {})
            if overload["status"] and not overload_from_window(window):
                overload = {"status": False, "reasons": [], "buckets": overload["buckets"]}
            entry = {
                "stageIndex": index,
                "targetRps": step.target_rps,
                "vus": step.vus,
                "transitionSeconds": step.transition_seconds,
                "settlingSeconds": step.settling_seconds,
                "stableSeconds": step.stable_seconds,
                "offsetSeconds": offset,
                "completed": capture["returncode"] == 0
                and not capture["generatorLimit"]
                and normalized.get("validity", {}).get("status") == "valid",
                "normalized": normalized,
                "generator": capture["generator"],
                "overload": overload,
                "files": {key: capture[key] for key in ("output", "summary", "log")},
                "generatorPreflight": step_preflight,
                "childNofileLimit": capture.get("childNofileLimit"),
            }
            stages.append(entry)
            all_windows.extend(windows)
            all_history.extend(histories)
            stock = (
                _stock_outcomes(out / capture["output"])
                if (out / capture["output"]).exists()
                else {"acknowledged": 0, "failed": 0}
            )
            acknowledged += stock["acknowledged"]
            failed += stock["failed"]
            checkpoint["stages"] = stages
            checkpoint.pop("current", None)
            _write_json(out / "checkpoint.json", checkpoint)
            print(
                json.dumps(
                    {
                        "event": "capacity_step_finished",
                        "targetRps": step.target_rps,
                        "completed": entry["completed"],
                        "overload": overload["status"],
                    }
                ),
                flush=True,
            )
            # If a partial capture lacks scenario_origin, retain its observed
            # wall duration as the next offset rather than inventing continuity.
            if not isinstance(scenario_origin, (int, float)):
                offset += max(0.0, (capture["endNs"] - capture["startNs"]) / 1e9)
            if capture["generatorLimit"]:
                stop_reason = capture["generatorLimit"]
                break
            if capture["returncode"]:
                stop_reason = "k6_failed"
                break
            if normalized.get("validity", {}).get("status") != "valid":
                stop_reason = "capture_invalid"
                break
            if remote.errors:
                stop_reason = "collector_infrastructure"
                break
            if overload["status"]:
                stop_reason = "sustained_overload"
                break
            event_types = {event.get("type") for event in getattr(remote, "events", [])}
            if {"oom", "restart"} & event_types:
                stop_reason = "pod_restart_or_oom"
                break
            candidate = next_target(step.target_rps, a.safety_ceiling)
            if index == len(planned) - 1 and candidate:
                planned.append(Step(candidate))
            if step.target_rps >= a.safety_ceiling:
                stop_reason = "tested_safety_ceiling"
                break
            index += 1
    except Exception as error:
        # Preserve every already-durable bounded step and let finalization
        # attach its telemetry rather than falling into the empty main fallback.
        stop_reason = (
            str(error) if str(error).startswith("generator_limit_") else "recorder_failure"
        )
        recorder_errors.append(str(error))
    finally:
        remote.stop()
        remote_data = remote.join(timeout=30)
    integrity_unavailable = False
    try:
        after = _drain_integrity(a, before)
        integrity_errors, excess = _validate_integrity(before, after, acknowledged, failed)
        integrity: dict[str, Any] = {
            "before": before,
            "after": after,
            "acknowledged": acknowledged,
            "failed": failed,
        }
        if excess is not None:
            integrity["committedUnacknowledged"] = excess
    except Exception as error:  # preserve load history when an OOM makes endpoint unreachable
        integrity_unavailable = True
        integrity_errors, integrity = [], {"before": before, "error": str(error)}
    # Use the first measured scenario epoch as the common run origin.  The
    # per-step offsets above retain real k6 process/normalization gaps.
    samples = _align_samples(remote_data, clock["offsetNs"], measured_origin or 0)
    container_samples = _align_samples(
        {"samples": remote_data.get("containerSamples", [])},
        clock["offsetNs"],
        measured_origin or 0,
    )
    aligned_events = []
    for event in remote_data.get("events", []):
        copy = dict(event)
        if isinstance(copy.get("finishedAt"), str):
            try:
                copy["seconds"] = (
                    datetime.fromisoformat(copy["finishedAt"].replace("Z", "+00:00")).timestamp()
                    - clock["offsetNs"] / 1e9
                    - (measured_origin or 0)
                )
            except ValueError:
                if isinstance(copy.get("realtime_ns"), (int, float)):
                    copy["seconds"] = (
                        copy["realtime_ns"] / 1e9 - clock["offsetNs"] / 1e9 - (measured_origin or 0)
                    )
        elif isinstance(copy.get("realtime_ns"), (int, float)):
            copy["seconds"] = (
                copy["realtime_ns"] / 1e9 - clock["offsetNs"] / 1e9 - (measured_origin or 0)
            )
        aligned_events.append(copy)
    coverage_values = []
    for stage in stages:
        local_windows = stage["normalized"].get("windows", [])
        for local_window in local_windows:
            start = local_window.get("startSeconds")
            end = local_window.get("endSeconds")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                resource_window = weighted_window(
                    samples, start + stage["offsetSeconds"], end + stage["offsetSeconds"]
                )
                local_window["resource"] = resource_window
                coverage_values.append(resource_window.get("coverage", 0))
    for window in all_windows:
        if isinstance(window.get("startSeconds"), (int, float)) and isinstance(
            window.get("endSeconds"), (int, float)
        ):
            window["resource"] = weighted_window(
                samples, window["startSeconds"], window["endSeconds"]
            )
    generator_values = [warm["generator"], *(stage["generator"] for stage in stages)]
    generator_warnings = sorted(
        {
            warning
            for item in generator_values
            for warning in item.get("errors", [])
            if isinstance(warning, str)
        }
    )
    generator_coverage = min((item.get("coverage", 0) for item in generator_values), default=0)
    peak_threads = max(
        (
            sample.get("numThreads", 0)
            for item in generator_values
            for sample in item.get("samples", [])
            if isinstance(sample.get("numThreads", 0), (int, float))
        ),
        default=0,
    )
    resource_coverage = min(coverage_values, default=0)
    reasons = list(integrity_errors) + list(remote_data.get("errors", []))
    if recorder_errors:
        reasons.append("recorder_failure")
    if generator_coverage < 0.95:
        reasons.append("generator_coverage_missing")
    if generator_warnings:
        reasons.append("generator_collector_failure")
    if stop_reason in {"k6_failed", "capture_invalid", "collector_infrastructure"}:
        reasons.append(stop_reason)
    if resource_coverage < 0.95:
        reasons.append("resource_coverage_missing")
    event_types = {event.get("type") for event in aligned_events}
    if {"oom", "restart"} & event_types:
        if not stop_reason:
            stop_reason = "pod_restart_or_oom"
        if stages:
            stages[-1]["overload"] = {
                "status": True,
                "reasons": ["oom_or_restart"],
                "buckets": 0,
            }
    status = (
        "valid"
        if not reasons and not (stop_reason and stop_reason.startswith("generator_limit"))
        else "invalid"
    )
    result = {
        "attemptId": a.attempt_id,
        "id": a.attempt_id,
        "runtime": a.runtime,
        "framework": a.framework,
        "metadata": metadata,
        "validity": {"status": status, "reasons": sorted(set(reasons))},
        "capacity": _capacity(stages, stop_reason),
        "stages": stages,
        "windows": all_windows,
        "history": all_history,
        "resource": {
            "scope": "pod",
            "coverage": resource_coverage,
            "samples": samples,
            "containerSamples": container_samples,
            "events": aligned_events,
        },
        "generator": {
            "coverage": generator_coverage,
            "headroomFlag": any(item.get("headroomFlag", False) for item in generator_values),
            "warnings": generator_warnings,
            "peakThreads": peak_threads,
        },
        "integrity": integrity,
        "schedule": {
            "hash": protocol_hash(a.safety_ceiling),
            "stages": [
                {
                    "targetRps": item["targetRps"],
                    "vus": item["vus"],
                    "transitionSeconds": item["transitionSeconds"],
                    "stableSeconds": item["stableSeconds"],
                    "settlingSeconds": item["settlingSeconds"],
                    "offsetSeconds": item["offsetSeconds"],
                }
                for item in stages
            ],
        },
    }
    if integrity_unavailable:
        result["integrity"]["qualifier"] = "unavailable_after_workload_boundary"
    _write_json(out / "result.json", result)
    checkpoint["status"] = "complete"
    _write_json(out / "checkpoint.json", checkpoint)
    return result


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "runtime",
        "framework",
        "image",
        "source-revision",
        "harness-source-revision",
        "flux-revision",
        "attempt-id",
        "base-url",
        "namespace",
        "ssh-host",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--safety-ceiling", type=int, default=SAFETY_CEILING_RPS)
    values = parser.parse_args(argv)
    if values.safety_ceiling < 1500:
        parser.error("--safety-ceiling must be at least 1500")
    return values


def main(argv: list[str] | None = None) -> int:
    values = arguments(argv)
    try:
        run(values)
    except Exception:
        # A failed warmup must still be a durable, explicitly invalid attempt;
        # the campaign/report can then account for all selected adapters.
        out = values.results_dir / values.attempt_id
        if out.is_dir() and not (out / "result.json").exists():
            try:
                checkpoint = json.loads((out / "checkpoint.json").read_text())
            except (OSError, json.JSONDecodeError):
                checkpoint = {"stages": []}
            stages = checkpoint.get("stages", [])
            windows = [
                shifted
                for stage in stages
                for shifted in _shift(
                    stage.get("normalized", {}).get("windows", []),
                    stage.get("offsetSeconds", 0),
                )
            ]
            history = [
                shifted
                for stage in stages
                for shifted in _shift(
                    stage.get("normalized", {}).get("history", []),
                    stage.get("offsetSeconds", 0),
                )
            ]
            _write_json(
                out / "result.json",
                {
                    "attemptId": values.attempt_id,
                    "id": values.attempt_id,
                    "runtime": values.runtime,
                    "framework": values.framework,
                    "metadata": {
                        "attemptId": values.attempt_id,
                        "runtime": values.runtime,
                        "framework": values.framework,
                        "image": values.image,
                        "sourceRevision": values.source_revision,
                        "harnessSourceRevision": values.harness_source_revision,
                        "loadHash": load_hash(),
                        "protocolHash": protocol_hash(values.safety_ceiling),
                        "measuredVus": MEASURED_VUS,
                        "warmupVus": WARMUP_VUS,
                        "maxVus": MEASURED_VUS,
                        "collectorDurationSeconds": COLLECTOR_DURATION_SECONDS,
                    },
                    "validity": {"status": "invalid", "reasons": ["recorder_failure"]},
                    "capacity": {
                        "highestPassingRps": None,
                        "highestNoOverloadRps": None,
                        "firstOverloadRps": None,
                        "firstLatencyFailureRps": None,
                        "stopReason": "recorder_failure",
                        "generatorLimited": False,
                    },
                    "stages": stages,
                    "windows": windows,
                    "history": history,
                    "resource": {
                        "scope": "pod",
                        "coverage": 0,
                        "samples": [],
                        "containerSamples": [],
                        "events": [],
                    },
                    "generator": {
                        "coverage": 0,
                        "headroomFlag": False,
                        "warnings": ["recorder_failure"],
                        "peakThreads": 0,
                    },
                    "schedule": {
                        "hash": protocol_hash(values.safety_ceiling),
                        "stages": [
                            {
                                "targetRps": stage.get("targetRps"),
                                "vus": stage.get("vus"),
                                "transitionSeconds": stage.get("transitionSeconds"),
                                "settlingSeconds": stage.get("settlingSeconds"),
                                "stableSeconds": stage.get("stableSeconds"),
                                "offsetSeconds": stage.get("offsetSeconds", 0),
                            }
                            for stage in stages
                        ],
                    },
                },
            )
        raise
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
