"""Record one SQLite ramp attempt; local-only captures are never production-valid."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import platform
import shlex
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None
from .campaign import generator_preflight, load_hash, pod_container_image_matches, schedule_hash
from .collector import GeneratorCollector, RemoteCollector, validated_ssh_host, weighted_window
from .collector_remote import SAFE_ID, SAFE_UID
from .normalize import normalize_k6
from .schedule import Stage


def request(base, path):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=10) as response:
        return json.loads(response.read())


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, default=str, indent=2) + "\n")
    os.chmod(path, 0o600)


def _copy_private(source, target: Path) -> None:
    if source and Path(source).exists():
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)


def _ssh(host: str, command: list[str], timeout=15):
    validated_ssh_host(host)
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, shlex.join(command)],
        check=True,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def load_pod(a):
    reply = _ssh(
        a.ssh_host,
        [
            "kubectl",
            "-n",
            a.namespace,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=http-ramp",
            "-o",
            "json",
        ],
        timeout=20,
    )
    pods = json.loads(reply.stdout).get("items", [])
    if len(pods) != 1:
        raise RuntimeError("expected exactly one http-ramp pod")
    pod = pods[0]
    meta, status = pod.get("metadata", {}), pod.get("status", {})
    matching_statuses = [
        item for item in status.get("containerStatuses", []) if item.get("name") == "http-ramp"
    ]
    container = matching_statuses[0] if len(matching_statuses) == 1 else {}
    if (
        meta.get("annotations", {}).get("benchmark.jamoowen.dev/attempt-id") != a.attempt_id
        or meta.get("deletionTimestamp")
        or not container.get("ready")
        or container.get("restartCount") != 0
        or not pod_container_image_matches(pod, "http-ramp", a.image)
    ):
        raise RuntimeError("pod identity does not match attempt/image/readiness")
    uid = meta.get("uid", "")
    container_id = container.get("containerID", "").removeprefix("containerd://")
    if not SAFE_UID.fullmatch(uid) or not SAFE_ID.fullmatch(container_id):
        raise RuntimeError("pod has invalid immutable identity")
    return {"pod": meta.get("name"), "uid": uid, "containerId": container_id}


def _server_clock_ns(host):
    value = _ssh(host, ["date", "+%s%N"]).stdout.strip()
    if not value.isdigit() or len(value) < 16:
        raise RuntimeError("invalid remote clock response")
    return int(value)


def clock_alignment(host):
    """Choose the lowest-RTT of three NTP-style timestamp brackets."""
    probes = []
    for _ in range(3):
        before = time.time_ns()
        server = _server_clock_ns(host)
        after = time.time_ns()
        rtt = after - before
        probes.append(
            {
                "clientBeforeNs": before,
                "serverNs": server,
                "clientAfterNs": after,
                "rttNs": rtt,
                "offsetNs": server - (before + after) // 2,
                "uncertaintyNs": rtt // 2,
            }
        )
    best = min(probes, key=lambda item: item["rttNs"])
    return {
        "probes": probes,
        "selected": best,
        "offsetNs": best["offsetNs"],
        "uncertaintyNs": best["uncertaintyNs"],
    }


def route_interface(base_url):
    host = urllib.parse.urlparse(base_url).hostname
    if not host:
        return None
    try:
        command = (
            ["route", "-n", "get", host]
            if platform.system() == "Darwin"
            else ["ip", "route", "get", host]
        )
        reply = subprocess.run(command, text=True, capture_output=True, check=True, timeout=15)
        if platform.system() == "Darwin":
            return next(
                (
                    x.split("interface:", 1)[1].strip()
                    for x in reply.stdout.splitlines()
                    if "interface:" in x
                ),
                None,
            )
        words = reply.stdout.split()
        return words[words.index("dev") + 1] if "dev" in words else None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class K6Result:
    returncode: int
    timed_out: bool
    generator: dict
    start_ns: int
    end_ns: int
    disk_headroom: bool = True


def k6_run(a, mode, output: Path, summary: Path, log: Path, duration: float):
    env = {
        "BASE_URL": a.base_url,
        "MODE": mode,
        "SEED_COUNT": "5000",
        "SCHEDULE_JSON": a.schedule_json,
        "PREALLOCATED_VUS": str(a.preallocated_vus),
        "WARMUP_RPS": str(a.warmup_rps),
        "WARMUP_SECONDS": str(a.warmup_seconds),
    }
    with log.open("w", encoding="utf-8") as output_log:
        os.chmod(log, 0o600)
        process = subprocess.Popen(
            [
                a.k6,
                "run",
                "--out",
                f"json={output}",
                "--summary-export",
                str(summary),
                str(Path(__file__).resolve().parents[1] / "load.js"),
            ],
            env={**os.environ, **env},
            stdout=output_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        start_ns = time.time_ns()
        timed_out = False
        generator = None
        generator_result = {"errors": ["generator_not_started"], "coverage": 0}
        deadline = time.monotonic() + duration + 30
        disk_headroom = True
        try:
            generator = GeneratorCollector(process.pid, route_interface(a.base_url)).start()
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(a.k6, duration + 30)
                if not a.local_only and shutil.disk_usage(output.parent).free < 2 * 1024**3:
                    disk_headroom = False
                    process.kill()
                    process.wait(timeout=5)
                    break
                try:
                    process.wait(timeout=min(5, remaining))
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise
        finally:
            if generator:
                generator.stop()
                generator_result = generator.join(timeout=10)
        end_ns = time.time_ns()
    return K6Result(
        process.returncode, timed_out, generator_result, start_ns, end_ns, disk_headroom
    )


def stages(raw):
    return [
        Stage(
            x["targetRps"], x["transitionSeconds"], x["stableSeconds"], x.get("settlingSeconds", 0)
        )
        for x in raw
    ]


def args(argv=None):
    p = argparse.ArgumentParser()
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
        "schedule-json",
    ):
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--results-dir", type=Path, required=True)
    p.add_argument("--k6", default="k6")
    p.add_argument("--warmup-rps", type=int, default=100)
    p.add_argument("--warmup-seconds", type=int, default=60)
    p.add_argument("--preallocated-vus", type=int, default=3200)
    p.add_argument("--max-vus", type=int, default=3200)
    p.add_argument("--ssh-host")
    p.add_argument("--local-only", action="store_true")
    return p.parse_args(argv)


def _duration(schedule):
    return sum(x.transition_seconds + x.settling_seconds + x.stable_seconds for x in schedule)


def _align_samples(collector, offset_ns, origin):
    aligned = []
    for item in collector.get("samples", []):
        start, end = item.get("realtimeStartSeconds"), item.get("realtimeEndSeconds")
        if not isinstance(start, (float, int)) or not isinstance(end, (float, int)):
            continue
        value = dict(item)
        value["intervalStartSeconds"] = start - offset_ns / 1e9 - origin
        value["intervalEndSeconds"] = end - offset_ns / 1e9 - origin
        value["seconds"] = value["intervalEndSeconds"]
        aligned.append(value)
    return aligned


def _hardware():
    result = {"platform": platform.platform(), "machine": platform.machine()}
    if psutil:
        result.update(
            {"logicalCpus": psutil.cpu_count(), "memoryBytes": psutil.virtual_memory().total}
        )
    return result


def _k6_version(k6):
    try:
        return subprocess.run(
            [k6, "version"], text=True, capture_output=True, check=True, timeout=15
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _metadata(a, info):
    required = (
        "runtimeVersion",
        "frameworkVersion",
        "driver",
        "driverVersion",
        "sqliteVersion",
        "pragmas",
        "workers",
    )
    expected_pragmas = {
        "journal_mode": "wal",
        "synchronous": 1,
        "foreign_keys": 1,
        "busy_timeout": 5000,
        "cache_size": -2000,
        "wal_autocheckpoint": 1000,
        "temp_store": 2,
    }
    if (
        any(x not in info for x in required)
        or info.get("runtime") != a.runtime
        or info.get("framework") != a.framework
        or info.get("experiment") != "sqlite-ramp-v2"
        or info.get("seedCount") != 5000
        or info.get("workers") != 1
        or info.get("pragmas") != expected_pragmas
        or (
            a.runtime == "go"
            and (info.get("cgoEnabled") is not False or info.get("gomaxprocs") != 1)
        )
        or not isinstance(info.get("compileOptions"), list)
        or not info["compileOptions"]
        or not all(isinstance(value, str) for value in info["compileOptions"])
    ):
        raise RuntimeError("benchmark info identity/metadata mismatch")
    return {
        "attemptId": a.attempt_id,
        "runtime": a.runtime,
        "framework": a.framework,
        "image": a.image,
        "sourceRevision": a.source_revision,
        "harnessSourceRevision": a.harness_source_revision,
        "fluxRevision": a.flux_revision,
        "loadHash": load_hash(),
        "scheduleHash": schedule_hash(json.loads(a.schedule_json)),
        **{x: info[x] for x in required},
        "compileOptions": info["compileOptions"],
        "workerSettings": _worker_settings(a.runtime, info),
        "localOnly": a.local_only,
        "generatorHardware": _hardware(),
    }


def _worker_settings(runtime, info):
    settings = {"workers": info["workers"]}
    for source, target in {
        "gomaxprocs": "goMaxProcs",
        "schedulersOnline": "beamSchedulers",
        "dirtyCpuSchedulersOnline": "beamDirtyCpuSchedulers",
        "dirtyIoSchedulers": "beamDirtyIoSchedulers",
        "tokioWorkers": "rustExecutorWorkers",
        "actixWorkers": "rustExecutorWorkers",
    }.items():
        if source in info:
            settings[target] = info[source]
    if runtime == "python":
        settings["uvicornWorkers"] = info["workers"]
    elif runtime == "node":
        settings["nodeClusterWorkers"] = info["workers"]
    elif runtime == "bun":
        settings["bunWorkers"] = info["workers"]
    return settings


def _seed_total(seed_count=5000):
    return sum((item * 37) % 201 for item in range(1, seed_count + 1))


def _add_invalid(result, reason):
    reasons = result["validity"].setdefault("reasons", [])
    if reason not in reasons:
        reasons.append(reason)
    result["validity"]["status"] = "invalid"


def _warmup_valid(normalized):
    counts = normalized.get("counts", {})
    return (
        normalized.get("validity", {}).get("status") == "valid"
        and counts.get("outcome:success", 0) == counts.get("request_outcomes", 0)
        and counts.get("request_outcomes", 0) > 0
        and counts.get("checks", 0) == counts.get("request_outcomes", 0)
        and counts.get("drops", 0) == 0
    )


def _stock_outcomes(path):
    """Stream only stock outcome counters for integrity reconciliation."""
    result = {"acknowledged": 0, "failed": 0}
    with gzip.open(path, "rt") as source:
        for line in source:
            point = json.loads(line)
            data = point.get("data", {})
            tags = data.get("tags", {})
            if point.get("type") != "Point" or point.get("metric") != "request_outcomes":
                continue
            if tags.get("operation") != "stock":
                continue
            if tags.get("outcome") == "success":
                result["acknowledged"] += 1
            elif tags.get("outcome") in {"http_error", "validation_error"}:
                result["failed"] += 1
    return result


def _drain_integrity(a, before):
    deadline = time.monotonic() + 30
    previous = None
    while time.monotonic() < deadline:
        current = request(a.base_url, "/benchmark/integrity")
        if current == previous:
            return current
        previous = current
        time.sleep(1)
    raise RuntimeError("integrity did not stabilize")


def _validate_integrity(before, after, acknowledged, failed):
    """Return public validity codes and the bounded unacknowledged-write count."""
    revisions = after.get("totalRevisions", -1)
    stock_delta = after.get("totalStock", 0) - before.get("totalStock", 0)
    if after.get("rows") != 5000 or stock_delta != revisions or revisions < acknowledged:
        return ["integrity_mismatch"], None
    excess = revisions - acknowledged
    if excess > failed:
        return ["integrity_excess_revisions"], excess
    return [], excess


def run(a):
    old_umask = os.umask(0o077)
    try:
        out = a.results_dir / a.attempt_id
        out.mkdir(parents=True, exist_ok=False, mode=0o700)
    finally:
        os.umask(old_umask)
    _write_json(out / "manifest.json", vars(a))
    result = {
        "attemptId": a.attempt_id,
        "id": a.attempt_id,
        "runtime": a.runtime,
        "framework": a.framework,
        "metadata": {},
        "build": {
            "imageDigest": a.image.rsplit("@", 1)[-1] if "@sha256:" in a.image else a.image,
            "sourceRevision": a.source_revision,
            "harnessSourceRevision": a.harness_source_revision,
            "loadHash": load_hash(),
            "scheduleHash": schedule_hash(json.loads(a.schedule_json)),
        },
        "validity": {"status": "invalid", "reasons": []},
        "counts": {},
        "resource": {"samples": [], "coverage": 0},
    }
    remote = collector = None
    try:
        if a.preallocated_vus <= 0 or a.preallocated_vus != a.max_vus:
            raise RuntimeError("invalid VU allocation")
        if not a.local_only and a.preallocated_vus != 3200:
            raise RuntimeError("production VU allocation must be 3200")
        schedule = stages(json.loads(a.schedule_json))
        before = request(a.base_url, "/benchmark/integrity")
        info = request(a.base_url, "/benchmark/info")
        result["metadata"] = _metadata(a, info)
        _write_json(out / "remote.json", {})
        _write_json(out / "generator.json", {})
        _write_json(out / "clock.json", {})
        if (
            before.get("rows") != 5000
            or before.get("totalRevisions") != 0
            or before.get("totalStock") != _seed_total()
        ):
            raise RuntimeError("database is not fresh")
        if not a.local_only:
            generator_preflight(a.results_dir)
        clock = (
            {"mode": "local-only", "offsetNs": 0, "uncertaintyNs": None}
            if a.local_only
            else clock_alignment(a.ssh_host)
        )
        _write_json(out / "clock.json", clock)
        if not a.local_only:
            identity = load_pod(a)
            remote = RemoteCollector(
                a.ssh_host,
                a.namespace,
                identity["pod"],
                "http-ramp",
                identity["uid"],
                identity["containerId"],
                a.warmup_seconds + _duration(schedule) + 60,
            ).start()
            if not remote._metadata_event.wait(15):
                raise RuntimeError("remote collector did not initialize")
        warm = k6_run(
            a,
            "warmup",
            out / "warmup.json.gz",
            out / "warmup-summary.json",
            out / "warmup.log",
            a.warmup_seconds,
        )
        _write_json(out / "generator.json", {"warmup": warm.generator})
        if warm.returncode or warm.timed_out:
            raise RuntimeError("warmup failed")
        warm_normalized = normalize_k6(
            str(out / "warmup.json.gz"),
            json.loads((out / "warmup-summary.json").read_text()),
            [Stage(a.warmup_rps, 0, a.warmup_seconds)],
        )
        _write_json(out / "warmup-normalized.json", warm_normalized)
        if not _warmup_valid(warm_normalized):
            raise RuntimeError("warmup capture/outcomes invalid")
        measured = k6_run(
            a,
            "measurement",
            out / "k6.json.gz",
            out / "summary.json",
            out / "measurement.log",
            _duration(schedule),
        )
        _write_json(
            out / "generator.json",
            {
                "warmup": warm.generator,
                "measurement": measured.generator,
                "k6Version": _k6_version(a.k6),
            },
        )
        result["metadata"]["generator"] = {
            "hardware": result["metadata"]["generatorHardware"],
            "k6Version": _k6_version(a.k6),
            "coverage": measured.generator.get("coverage", 0),
            "headroomFlag": measured.generator.get("headroomFlag", False),
            "warnings": measured.generator.get("errors", []),
            "interface": measured.generator.get("interface"),
        }
        result["generator"] = result["metadata"]["generator"]
        if result["generator"]["headroomFlag"]:
            result["generator"]["warnings"].append("generator_headroom")
        if not a.local_only and result["generator"]["warnings"]:
            _add_invalid(result, "collector_generator_failure")
        if not a.local_only and result["generator"]["coverage"] < 0.95:
            _add_invalid(result, "coverage_missing")
        if not warm.disk_headroom or not measured.disk_headroom:
            _add_invalid(result, "disk_headroom")
            raise RuntimeError("disk_headroom")
        if measured.returncode or measured.timed_out:
            raise RuntimeError("measurement k6 failed")
        normalized = normalize_k6(
            str(out / "k6.json.gz"), json.loads((out / "summary.json").read_text()), schedule
        )
        capture_validity = dict(normalized["validity"])
        result.update(normalized)
        result["counts"]["requests"] = result["counts"].get("http_req_duration", 0)
        warm_stocks = _stock_outcomes(out / "warmup.json.gz")
        measured_stocks = _stock_outcomes(out / "k6.json.gz")
        result["stockAcknowledgements"] = {"warmup": warm_stocks, "measurement": measured_stocks}
        after = _drain_integrity(a, before)
        result["integrity"] = {"before": before, "after": after}
        acknowledged = warm_stocks["acknowledged"] + measured_stocks["acknowledged"]
        failed = warm_stocks["failed"] + measured_stocks["failed"]
        integrity_errors, excess = _validate_integrity(before, after, acknowledged, failed)
        for error in integrity_errors:
            _add_invalid(result, error)
        if excess:
            result["integrity"]["committedUnacknowledged"] = excess
        if remote:
            remote.stop()
            collector = remote.join(timeout=20)
            _copy_private(remote._raw_log.name if remote._raw_log else None, out / "remote.jsonl")
            _copy_private(
                remote._stderr_log.name if remote._stderr_log else None, out / "remote.stderr"
            )
            _write_json(out / "remote.json", collector)
            aligned = _align_samples(
                collector, clock["offsetNs"], normalized.get("scenarioOrigin", 0)
            )
            coverage = weighted_window(aligned, 0, _duration(schedule))
            result["resource"] = {
                "samples": aligned,
                "coverage": coverage.get("coverage", 0),
                "overall": coverage,
                "generator": measured.generator,
            }
            for window in result.get("windows", []):
                window["resource"] = weighted_window(
                    aligned, window["startSeconds"], window["endSeconds"]
                )
            errors = collector["errors"] + (
                ["remote collector coverage below 95%"]
                if coverage.get("coverage", 0) < 0.95
                else []
            )
            for error in sorted(set(errors)):
                _add_invalid(result, error)
            final_identity = load_pod(a)
            if final_identity != identity:
                _add_invalid(result, "pod_identity_drift")
        else:
            result["resource"] = {
                "samples": [],
                "coverage": 0,
                "generator": measured.generator,
                "locality": "local-only: cluster telemetry unavailable",
            }
        if not result["validity"]["reasons"]:
            result["validity"] = normalized["validity"]
        if a.local_only:
            _add_invalid(result, "local_telemetry_missing")
            result["captureValidity"] = capture_validity
    except Exception as error:
        _add_invalid(result, "recorder_failure")
        with (out / "errors.log").open("a", encoding="utf-8") as errors:
            errors.write(f"{type(error).__name__}: {error}\n")
        os.chmod(out / "errors.log", 0o600)
    finally:
        if remote and collector is None:
            try:
                remote.stop()
                collector = remote.join(timeout=20)
                _copy_private(
                    remote._raw_log.name if remote._raw_log else None, out / "remote.jsonl"
                )
                _copy_private(
                    remote._stderr_log.name if remote._stderr_log else None, out / "remote.stderr"
                )
                _write_json(out / "remote.json", collector)
            except Exception as error:
                result["validity"]["reasons"].append(f"collector cleanup: {error}")
        _write_json(out / "result.json", result)
    return 0 if result["validity"]["status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(run(args()))
