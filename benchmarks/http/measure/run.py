import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .cluster import collector_source, ssh_command
from .diagnostics import PROFILE_NAMES, save_profile, write_cpu_top, write_event, write_summary
from .diagnostics_remote import remote_program
from .diagnostics_remote import ssh_command as diagnostics_ssh_command
from .http_history import write_history
from .results import SCHEMA_VERSION, normalize_k6, resource_statistics

ROOT = Path(__file__).resolve().parents[3]
LOAD_SCRIPT = ROOT / "benchmarks/http/load.js"


def _duration_seconds(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", str(value))
    if not match:
        return None
    return float(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Record one HTTP benchmark")
    parser.add_argument("implementation", choices=("go", "bun"))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--namespace", default="my-api")
    parser.add_argument("--profile", default="steady", choices=("smoke", "steady", "stress"))
    parser.add_argument("--workload", default="mixed")
    parser.add_argument("--rate", type=int, default=100)
    parser.add_argument("--duration", default="5m")
    parser.add_argument("--seed-count", type=int, default=5000)
    parser.add_argument("--preallocated-vus", type=int, default=10)
    parser.add_argument("--max-vus", type=int, default=100)
    parser.add_argument("--p95-ms", type=float, default=1000)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--warmup-duration", default="60s")
    parser.add_argument("--sample-interval", type=float, default=5)
    parser.add_argument("--results-dir", default="results/http")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--diagnostics-seconds", type=int, default=30)
    args = parser.parse_args(argv)
    if (
        not args.base_url.startswith(("http://", "https://"))
        or args.rate < 1
        or args.seed_count < 1
        or args.preallocated_vus < 1
        or args.max_vus < args.preallocated_vus
        or args.p95_ms < 0
        or not 0 <= args.max_error_rate <= 1
        or args.sample_interval <= 0
        or not 1 <= args.diagnostics_seconds <= 120
        or (
            args.diagnostics
            and (
                _duration_seconds(args.duration) is None
                or args.diagnostics_seconds > _duration_seconds(args.duration)
            )
        )
    ):
        parser.error("invalid benchmark setting")
    if args.diagnostics and args.profile != "steady":
        parser.error("--diagnostics currently requires --profile steady")
    return args


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_lines(stream, destination, events, errors):
    try:
        for line in stream:
            destination.write(line)
            destination.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append(f"collector JSON: {error}")
                continue
            events.append(event)
            if event.get("type") == "error":
                errors.append(str(event.get("error", "collector error")))
    except Exception as error:
        errors.append(f"collector output: {error}")


def _read_stderr(stream, destination, errors):
    for line in stream:
        destination.write(line)
        destination.flush()
        errors.append(f"collector stderr: {line.rstrip()}")


def start_collector(args, deployment, resource_file, log_file):
    process = subprocess.Popen(
        ssh_command(args.ssh_host, args.namespace, deployment, args.sample_interval, "stream"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    process.stdin.write(collector_source())
    process.stdin.close()
    events, errors = [], []
    threads = [
        threading.Thread(
            target=_read_lines, args=(process.stdout, resource_file, events, errors), daemon=True
        ),
        threading.Thread(target=_read_stderr, args=(process.stderr, log_file, errors), daemon=True),
    ]
    for thread in threads:
        thread.start()
    return process, threads, events, errors


def start_diagnostics(args, pod_name, directory):
    destination = directory / "diagnostics"
    stream, log, process = None, None, None
    try:
        destination.mkdir(exist_ok=True)
        stream = (destination / "runtime-samples.jsonl").open("w")
        log = (destination / "diagnostics.log").open("w")
        process = subprocess.Popen(
            diagnostics_ssh_command(
                args.ssh_host,
                args.namespace,
                pod_name,
                args.implementation,
                args.sample_interval,
                args.diagnostics_seconds,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            bufsize=1,
        )
        process.stdin.write(remote_program())
        process.stdin.close()
    except Exception:
        _stop(process)
        if stream:
            stream.close()
        if log:
            log.close()
        raise
    events, errors = [], []

    def reader():
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("diagnostics event must be an object")
                    if event.get("type") == "profile":
                        path = save_profile(
                            destination, args.implementation, event.get("name"), event.get("data")
                        )
                        event = {key: value for key, value in event.items() if key != "data"}
                        event["saved"] = path.is_file()
                    write_event(stream, event)
                    events.append(event)
                    if event.get("type") == "error":
                        errors.append(str(event.get("error", "diagnostic error")))
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                    errors.append(f"diagnostics event: {error}")
        except (OSError, TypeError) as error:
            errors.append(f"diagnostics output: {error}")
        finally:
            stream.close()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    return process, thread, events, errors, log, destination


def collect_once(args, deployment, mode="metadata"):
    completed = subprocess.run(
        ssh_command(args.ssh_host, args.namespace, deployment, args.sample_interval, mode),
        input=collector_source(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "error":
                raise RuntimeError(str(event.get("error")))
        raise RuntimeError(completed.stderr.strip() or "collector command failed")
    events = []
    for line in completed.stdout.splitlines():
        events.append(json.loads(line))
    metadata = next(
        (event.get("metadata") for event in events if event.get("type") == "metadata"), None
    )
    sample = next((event for event in events if event.get("type") == "sample"), None)
    return metadata, sample


def _wrapper(directory):
    wrapper = directory / "load-wrapper.js"
    wrapper.write_text(
        f"import run, {{ options }} from {json.dumps(str(LOAD_SCRIPT))}; export {{ options }}; export default run; export function handleSummary(data) {{ return {{ [__ENV.SUMMARY_PATH]: JSON.stringify(data) }}; }}\n"
    )
    return wrapper


def run_k6(args, directory, duration, label, profile=None):
    summary, wrapper = directory / f"{label}-k6-summary.json", _wrapper(directory)
    values = {
        "BASE_URL": args.base_url,
        "PROFILE": profile or args.profile,
        "WORKLOAD": args.workload,
        "RATE": args.rate,
        "DURATION": duration,
        "SEED_COUNT": args.seed_count,
        "PREALLOCATED_VUS": args.preallocated_vus,
        "MAX_VUS": args.max_vus,
        "P95_MS": args.p95_ms,
        "MAX_ERROR_RATE": args.max_error_rate,
        "SUMMARY_PATH": summary,
    }
    output = (
        ["--out", f"json={directory / 'http-metrics.json.gz'}"] if label == "measurement" else []
    )
    command = (
        [args.k6, "run"]
        + output
        + [part for key, value in values.items() for part in ("-e", f"{key}={value}")]
        + [str(wrapper)]
    )
    log_path = directory / f"{label}-k6.log"
    environment = os.environ.copy()
    environment.pop("K6_OUT", None)
    environment["K6_NEW_MACHINE_READABLE_SUMMARY"] = "false"
    process = None
    with log_path.open("w") as log:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
            )
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end="")
            code = process.wait()
        finally:
            _stop(process)
    return code, summary


def _settings(args):
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
        "diagnostics",
        "diagnostics_seconds",
    )
    return {name: getattr(args, name) for name in names}


def _validate_preflight(metadata, sample, args):
    if (
        not metadata
        or not sample
        or sample.get("cpu_seconds") is None
        or sample.get("memory_working_set_bytes") is None
        or not isinstance(sample.get("cpu_timestamp_ms"), (int, float))
        or not isinstance(sample.get("memory_working_set_timestamp_ms"), (int, float))
    ):
        raise RuntimeError("collector preflight lacks essential CPU or memory sample")
    configuration = metadata.get("workload", {}).get("configuration", {})
    if str(configuration.get("SEED_COUNT")) != str(args.seed_count):
        raise RuntimeError("SEED_COUNT differs from requested benchmark")
    if args.implementation == "go" and str(configuration.get("MAX_OPEN_CONNS", "1")) != "1":
        raise RuntimeError("Go MAX_OPEN_CONNS must be 1")
    if args.diagnostics and str(configuration.get("DIAGNOSTICS")) != "1":
        raise RuntimeError("--diagnostics requires app DIAGNOSTICS=1")
    if sample.get("pod_uid") != metadata.get("pod", {}).get("uid") or sample.get(
        "container_id"
    ) != metadata.get("pod", {}).get("container_id"):
        raise RuntimeError("collector preflight sample identity differs from metadata")


def _same_identity(before, after):
    for key in ("uid", "container_id", "image_id"):
        if before.get("pod", {}).get(key) != after.get("pod", {}).get(key):
            return False, key
    if after.get("pod", {}).get("restart_count", 0) != before.get("pod", {}).get(
        "restart_count", 0
    ):
        return False, "restart_count"
    for key in ("configuration", "resources"):
        if before.get("workload", {}).get(key) != after.get("workload", {}).get(key):
            return False, key
    if before.get("node", {}).get("uid") != after.get("node", {}).get("uid"):
        return False, "node"
    return True, None


def _write_csv(path, samples):
    keys = sorted({key for sample in samples for key in sample})
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=keys)
        writer.writeheader()
        writer.writerows(samples)


def _stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _print_summary(directory, status, result, resource):
    latency = result.get("latency_ms", {}) if isinstance(result, dict) else {}
    values = {
        "status": status,
        "requests": result.get("requests") if isinstance(result, dict) else None,
        "achieved_rps": result.get("achieved_rps") if isinstance(result, dict) else None,
        "p95_ms": latency.get("p(95)"),
        "error_rate": result.get("error_rate") if isinstance(result, dict) else None,
        "mean_cpu_millicores": resource.get("mean_cpu_millicores"),
        "max_sampled_working_set_bytes": resource.get("max_sampled_working_set_bytes"),
        "directory": str(directory),
    }
    print("recording summary: " + json.dumps(values, sort_keys=True))


def main(argv=None):
    args = arguments(argv)
    started = datetime.now(UTC)
    run_id = f"{started:%Y%m%dT%H%M%S}.{started.microsecond:06d}Z-{args.implementation}-{args.profile}-{args.rate}rps"
    directory = Path(args.results_dir) / run_id
    directory.mkdir(parents=True, exist_ok=False)
    deployment = f"http-{args.implementation}"
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "implementation": args.implementation,
        "base_url": args.base_url,
        "started_at": started.isoformat(),
        "settings": _settings(args),
        "http_capture": {
            "status": "pending",
            "warnings": [],
            "raw_file": "http-metrics.json.gz",
            "history_file": "http-history.json",
            "format": "k6-json-gzip",
            "phase": "measurement",
            "source": "--out",
        },
        "diagnostics": {
            "status": "pending" if args.diagnostics else "not-recorded",
            "warnings": [],
            "seconds": args.diagnostics_seconds if args.diagnostics else None,
            "profiles": [],
        },
    }
    collector, collector_threads = None, []
    diagnostic_process, diagnostic_thread, diagnostic_events = None, None, []
    diagnostic_errors, diagnostic_log, diagnostic_directory = [], None, None
    errors, events, result, resource, code = [], [], None, None, None
    measurement_summary = directory / "measurement-k6-summary.json"
    status, error = "failed", None
    try:
        metadata.update(
            {
                "load_script_sha256": hashlib.sha256(LOAD_SCRIPT.read_bytes()).hexdigest(),
                "k6_version": subprocess.check_output([args.k6, "version"], text=True).strip(),
                "source_git_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "source_git_dirty": bool(
                    subprocess.check_output(
                        ["git", "status", "--porcelain"], cwd=ROOT, text=True
                    ).strip()
                ),
            }
        )
        before, preflight_sample = collect_once(args, deployment, "once")
        _validate_preflight(before, preflight_sample, args)
        metadata["cluster"] = before
        warmup_code, warmup_summary = run_k6(
            args, directory, args.warmup_duration, "warmup", "steady"
        )
        metadata["warmup_exit_code"] = warmup_code
        if warmup_summary.exists():
            metadata["warmup_summary"] = json.loads(warmup_summary.read_text())
        if warmup_code not in (0, 99):
            raise RuntimeError(f"warmup k6 exited {warmup_code}")
        resource_file = (directory / "resources.jsonl").open("w")
        collector_log = (directory / "collector.log").open("w")
        try:
            collector, collector_threads, events, errors = start_collector(
                args, deployment, resource_file, collector_log
            )
            deadline = time.monotonic() + max(10, args.sample_interval * 3)
            while time.monotonic() < deadline and not (
                any(event.get("type") == "sample" for event in events)
                and any(event.get("type") == "metadata" for event in events)
            ):
                if collector.poll() is not None:
                    raise RuntimeError("collector exited before first sample")
                if errors:
                    raise RuntimeError(errors[0])
                time.sleep(0.05)
            stream_metadata = next(
                (event.get("metadata") for event in events if event.get("type") == "metadata"), None
            )
            stream_sample = next((event for event in events if event.get("type") == "sample"), None)
            if not stream_metadata or not stream_sample:
                raise RuntimeError("collector did not provide source identity and first sample")
            _validate_preflight(stream_metadata, stream_sample, args)
            same, changed = _same_identity(before, stream_metadata)
            if not same:
                raise RuntimeError(f"collector source identity changed: {changed}")
            if args.diagnostics:
                try:
                    (
                        diagnostic_process,
                        diagnostic_thread,
                        diagnostic_events,
                        diagnostic_errors,
                        diagnostic_log,
                        diagnostic_directory,
                    ) = start_diagnostics(args, before["pod"]["name"], directory)
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline and not any(
                        event.get("type") == "ready" for event in diagnostic_events
                    ):
                        if diagnostic_process.poll() is not None:
                            break
                        time.sleep(0.05)
                    if not any(event.get("type") == "ready" for event in diagnostic_events):
                        diagnostic_errors.append("diagnostics did not become ready")
                        metadata["diagnostics"]["status"] = "partial"
                        _stop(diagnostic_process)
                    else:
                        metadata["diagnostics"]["status"] = "recording"
                except Exception as caught:
                    diagnostic_errors.append(f"diagnostics startup: {caught}")
                    metadata["diagnostics"]["status"] = "partial"
            metadata["measured_started_at_unix"] = time.time()
            code, summary_path = run_k6(args, directory, args.duration, "measurement")
            metadata["measured_ended_at_unix"] = time.time()
            _stop(diagnostic_process)
            if summary_path.exists():
                raw = json.loads(summary_path.read_text())
                write_json(directory / "k6-summary.json", raw)
                result = normalize_k6(raw)
            elif code == 0:
                raise RuntimeError("k6 exited successfully without a summary")
            if errors or collector.poll() not in (None, 0):
                raise RuntimeError(errors[0] if errors else "collector failed during measurement")
        finally:
            _stop(collector)
            for thread in collector_threads:
                thread.join(timeout=5)
            if collector and getattr(collector, "stdout", None):
                collector.stdout.close()
            resource_file.close()
            collector_log.close()
            collector = None
        after, _ = collect_once(args, deployment)
        metadata["cluster_after"] = after
        same, changed = _same_identity(before, after)
        if not same:
            raise RuntimeError(f"cluster identity changed: {changed}")
        samples = [event for event in events if event.get("type") == "sample"]
        _write_csv(directory / "resources.csv", samples)
        resource = resource_statistics(
            samples, metadata["measured_started_at_unix"], metadata["measured_ended_at_unix"]
        )
        resource["restart_delta"] = after.get("pod", {}).get("restart_count", 0) - before.get(
            "pod", {}
        ).get("restart_count", 0)
        essential = {
            "insufficient distinct CPU samples",
            "CPU counter reset",
            "CPU counter identity changed",
            "essential metric unavailable: memory_working_set",
        }
        status = (
            "complete"
            if code == 0 and not essential.intersection(resource["warnings"])
            else "invalid"
        )
    except KeyboardInterrupt:
        error, status = "interrupted", "interrupted"
        code = 130
    except Exception as caught:
        error = str(caught)
        if code is not None:
            status = "invalid"
    finally:
        _stop(collector)
        _stop(diagnostic_process)
        for thread in collector_threads:
            thread.join(timeout=5)
        if collector and getattr(collector, "stdout", None):
            collector.stdout.close()
        samples = [event for event in events if event.get("type") == "sample"]
        if "measured_started_at_unix" in metadata and "measured_ended_at_unix" not in metadata:
            metadata["measured_ended_at_unix"] = time.time()
        if result is None and measurement_summary.exists():
            try:
                raw = json.loads(measurement_summary.read_text())
                write_json(directory / "k6-summary.json", raw)
                result = normalize_k6(raw)
            except (OSError, ValueError, json.JSONDecodeError) as caught:
                errors.append(f"k6 summary: {caught}")
        if result is not None and "measured_started_at_unix" in metadata:
            try:
                _, capture = write_history(directory, result, metadata)
                metadata["http_capture"] = {
                    **metadata["http_capture"],
                    **capture,
                }
            except Exception as caught:
                metadata["http_capture"] = {
                    **metadata["http_capture"],
                    "status": "partial",
                    "warnings": [f"HTTP history: {caught}"],
                }
        if samples:
            _write_csv(directory / "resources.csv", samples)
        if resource is None:
            resource = resource_statistics(
                samples,
                metadata.get("measured_started_at_unix"),
                metadata.get("measured_ended_at_unix"),
            )
        if args.diagnostics:
            if diagnostic_thread:
                diagnostic_thread.join(timeout=5)
                if diagnostic_thread.is_alive():
                    diagnostic_errors.append("diagnostics reader did not stop")
            if diagnostic_process and getattr(diagnostic_process, "stdout", None):
                diagnostic_process.stdout.close()
            if diagnostic_log:
                diagnostic_log.close()
            metadata["diagnostics"]["warnings"] = list(dict.fromkeys(diagnostic_errors))
            if diagnostic_directory:
                try:
                    summary = write_summary(
                        diagnostic_directory / "diagnostics-summary.json",
                        diagnostic_events,
                        args.implementation,
                    )
                    measured_start = metadata.get("measured_started_at_unix")
                    measured_end = metadata.get("measured_ended_at_unix")
                    capture = summary.get("capture_window", {})
                    capture_start = capture.get("start")
                    capture_end = capture.get("end")
                    values = (measured_start, measured_end, capture_start, capture_end)
                    window = {
                        "measured_start_unix": measured_start,
                        "measured_end_unix": measured_end,
                        "capture_start_unix": capture_start,
                        "capture_end_unix": capture_end,
                        "overlap_seconds": None,
                    }
                    if all(
                        isinstance(value, (int, float)) and math.isfinite(value) for value in values
                    ):
                        window["overlap_seconds"] = max(
                            0, min(capture_end, measured_end) - max(capture_start, measured_start)
                        )
                        if window["overlap_seconds"] == 0:
                            summary["warnings"].append(
                                "diagnostic capture did not overlap measurement"
                            )
                    else:
                        summary["warnings"].append("diagnostic capture overlap unavailable")
                    summary["measurement_window"] = window
                    metadata["diagnostics"]["measurement_window"] = window
                    write_json(diagnostic_directory / "diagnostics-summary.json", summary)
                    metadata["diagnostics"]["summary_file"] = "diagnostics/diagnostics-summary.json"
                    metadata["diagnostics"]["profiles"] = [
                        f"diagnostics/{event['name']}"
                        for event in diagnostic_events
                        if event.get("type") == "profile"
                        and event.get("saved")
                        and event.get("name") in PROFILE_NAMES[args.implementation]
                    ]
                    top, top_error = write_cpu_top(diagnostic_directory, args.implementation)
                    if top:
                        metadata["diagnostics"]["profiles"].append(f"diagnostics/{top}")
                    if top_error:
                        metadata["diagnostics"]["warnings"].append(
                            f"cpu top unavailable: {top_error}"
                        )
                    required = {"ready", "end"}
                    present = {event.get("type") for event in diagnostic_events}
                    expected_cpu = "cpu.pprof" if args.implementation == "go" else "jsc-cpu.json"
                    if expected_cpu not in {
                        event.get("name")
                        for event in diagnostic_events
                        if event.get("type") == "profile" and event.get("saved")
                    }:
                        metadata["diagnostics"]["warnings"].append("CPU profile was not saved")
                    if not required.issubset(present):
                        metadata["diagnostics"]["warnings"].append("diagnostics did not complete")
                    if diagnostic_process and diagnostic_process.poll() not in (None, 0):
                        metadata["diagnostics"]["warnings"].append(
                            f"diagnostics helper exited {diagnostic_process.poll()}"
                        )
                    metadata["diagnostics"]["warnings"] = list(
                        dict.fromkeys(metadata["diagnostics"]["warnings"] + summary["warnings"])
                    )
                    if metadata["diagnostics"]["status"] == "recording":
                        metadata["diagnostics"]["status"] = (
                            "partial"
                            if metadata["diagnostics"]["warnings"]
                            or summary["status"] != "complete"
                            else "complete"
                        )
                except Exception as caught:
                    metadata["diagnostics"].update(
                        {
                            "status": "partial",
                            "warnings": metadata["diagnostics"]["warnings"] + [str(caught)],
                        }
                    )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "metadata": metadata,
            "result": result,
            "resource": resource,
            "k6_exit_code": code,
            "error": error,
            "collector_errors": errors,
        }
        write_json(directory / "metadata.json", metadata)
        write_json(directory / "result.json", payload)
        _print_summary(directory, status, result, resource)
        if error:
            print(f"recording error: {error}")
    return code if code in (99, 130) or status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
