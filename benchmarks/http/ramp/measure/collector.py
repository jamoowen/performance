"""Fresh remote cgroup and local generator resource collectors."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from .collector_remote import SAFE_ID, SAFE_NAME, SAFE_UID

SAFE_SSH_HOST = re.compile(r"^(?:[a-z_][a-z0-9_-]*@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")


def validated_ssh_host(value):
    if not SAFE_SSH_HOST.fullmatch(value):
        raise ValueError("invalid SSH host")
    host = value.rsplit("@", 1)[-1]
    if host.startswith("-"):
        raise ValueError("invalid SSH host")
    return value


def validated_positive_pid(value):
    if not isinstance(value, int) or value <= 0:
        raise ValueError("invalid remote PID")
    return value


def pressure(value):
    result = {}
    for line in (value or "").splitlines():
        parts = line.split()
        if parts:
            result[parts[0]] = {
                key: float(number) for key, number in (entry.split("=") for entry in parts[1:])
            }
    return result


def pressure_total(value, category="some"):
    return pressure(value).get(category, {}).get("total", 0.0)


def node_cpu_utilization(previous, current):
    fields = {"user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"}
    before = previous.get("node_cpu", {})
    after = current.get("node_cpu", {})
    total = sum(after.get(field, 0) - before.get(field, 0) for field in fields)
    idle = (after.get("idle", 0) - before.get("idle", 0)) + (
        after.get("iowait", 0) - before.get("iowait", 0)
    )
    if total <= 0 or idle < 0:
        raise ValueError("node_cpu_counter_reset")
    return max(0, min(1, (total - idle) / total))


def resource_delta(previous, current):
    elapsed = (current["monotonic_ns"] - previous["monotonic_ns"]) / 1e9
    if elapsed <= 0:
        raise ValueError("non_monotonic_clock")
    cpu_before, cpu_after = previous["cpu_stat"], current["cpu_stat"]
    usage = cpu_after.get("usage_usec", 0) - cpu_before.get("usage_usec", 0)
    throttled = cpu_after.get("throttled_usec", 0) - cpu_before.get("throttled_usec", 0)
    periods = cpu_after.get("nr_periods", 0) - cpu_before.get("nr_periods", 0)
    throttled_periods = cpu_after.get("nr_throttled", 0) - cpu_before.get("nr_throttled", 0)
    oom = current["memory_events"].get("oom_kill", 0) - previous["memory_events"].get("oom_kill", 0)
    cpu_pressure_total = pressure_total(current.get("cpu_pressure")) - pressure_total(
        previous.get("cpu_pressure")
    )
    memory_pressure_total = pressure_total(current.get("memory_pressure")) - pressure_total(
        previous.get("memory_pressure")
    )
    if (
        min(
            usage,
            throttled,
            periods,
            throttled_periods,
            oom,
            cpu_pressure_total,
            memory_pressure_total,
        )
        < 0
    ):
        raise ValueError("counter_reset")
    return {
        "seconds": current["monotonic_ns"] / 1e9,
        "intervalStartSeconds": previous["monotonic_ns"] / 1e9,
        "intervalEndSeconds": current["monotonic_ns"] / 1e9,
        "realtimeStartSeconds": previous["realtime_ns"] / 1e9,
        "realtimeEndSeconds": current["realtime_ns"] / 1e9,
        "monotonicIntervalSeconds": elapsed,
        "elapsedSeconds": elapsed,
        "cpuMillicores": usage / elapsed / 1000,
        "throttledSeconds": throttled / 1e6,
        "cfsPeriods": periods,
        "cfsThrottledPeriods": throttled_periods,
        "cfsPeriodRatio": throttled_periods / periods if periods else 0,
        "workingSetBytes": max(0, current["memory_current"] - current.get("inactive_file", 0)),
        "memoryCurrentBytes": current["memory_current"],
        "oomKills": oom,
        "cpuPressureTotalMicroseconds": cpu_pressure_total,
        "memoryPressureTotalMicroseconds": memory_pressure_total,
        "nodeCpuUtilization": node_cpu_utilization(previous, current),
    }


def weighted_window(samples, start, end):
    if end <= start:
        return {"coverage": 0, "error": "invalid_window"}
    covered = 0.0
    weighted = {
        "cpuMillicores": 0.0,
        "workingSetBytes": 0.0,
        "memoryCurrentBytes": 0.0,
        "nodeCpuUtilization": 0.0,
    }
    throttled = 0.0
    cfs_periods = 0.0
    cfs_throttled = 0.0
    cpu_pressure_total = 0.0
    memory_pressure_total = 0.0
    for item in samples:
        interval_start = item.get("intervalStartSeconds", item.get("seconds"))
        interval_end = item.get("intervalEndSeconds", item.get("seconds"))
        if not isinstance(interval_start, (int, float)) or not isinstance(
            interval_end, (int, float)
        ):
            continue
        interval = interval_end - interval_start
        overlap = max(0.0, min(end, interval_end) - max(start, interval_start))
        if not overlap or interval <= 0 or interval > 2.5:
            continue
        covered += overlap
        for key in weighted:
            if isinstance(item.get(key), (int, float)):
                weighted[key] += item[key] * overlap
        fraction = overlap / interval
        throttled += item.get("throttledSeconds", 0) * fraction
        cfs_periods += item.get("cfsPeriods", 0) * fraction
        cfs_throttled += item.get("cfsThrottledPeriods", 0) * fraction
        cpu_pressure_total += item.get("cpuPressureTotalMicroseconds", 0) * fraction
        memory_pressure_total += item.get("memoryPressureTotalMicroseconds", 0) * fraction
    duration = end - start
    if not covered:
        return {"coverage": 0, "error": "insufficient_samples"}
    result = {
        "coverage": covered / duration,
        **{key: value / covered for key, value in weighted.items()},
        "throttledSeconds": throttled,
        "cfsPeriodRatio": cfs_throttled / cfs_periods if cfs_periods else 0,
        "cpuPressureTotalMicroseconds": cpu_pressure_total,
        "memoryPressureTotalMicroseconds": memory_pressure_total,
    }
    if covered < duration:
        result["error"] = "sample_gap"
    return result


def private_log(prefix, suffix):
    handle = tempfile.NamedTemporaryFile(
        prefix=prefix, suffix=suffix, delete=False, mode="w", encoding="utf-8"
    )
    os.chmod(handle.name, 0o600)
    return handle


@dataclass
class RemoteCollector:
    ssh_host: str
    namespace: str
    pod: str
    container: str
    pod_uid: str
    container_id: str
    duration: float
    interval: float = 1.0
    samples: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    identities: list = field(default_factory=list)
    end: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    _process: object = None
    _reader: object = None
    _stderr_reader: object = None
    _raw_log: object = None
    _stderr_log: object = None
    _remote_pid: int | None = None
    _metadata_event: object = field(default_factory=threading.Event)

    def _validate(self):
        validated_ssh_host(self.ssh_host)
        for value, pattern, name in (
            (self.namespace, SAFE_NAME, "namespace"),
            (self.pod, SAFE_NAME, "pod"),
            (self.container, SAFE_NAME, "container"),
            (self.pod_uid, SAFE_UID, "pod UID"),
            (self.container_id, SAFE_ID, "container ID"),
        ):
            if not pattern.fullmatch(value):
                raise ValueError(f"invalid {name}")
        if self.duration <= 0 or self.interval < 1:
            raise ValueError("invalid duration or interval")

    def start(self):
        self._validate()
        source = Path(__file__).with_name("collector_remote.py").read_text()
        remote_command = shlex.join(
            [
                "python3",
                "-",
                self.namespace,
                self.pod,
                self.container,
                self.pod_uid,
                self.container_id,
                str(self.duration),
                str(self.interval),
            ]
        )
        self._raw_log = private_log("performance-ramp-remote-", ".jsonl")
        self._stderr_log = private_log("performance-ramp-remote-", ".stderr")
        self._process = subprocess.Popen(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                self.ssh_host,
                remote_command,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._process.stdin.write(source)
        self._process.stdin.close()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        return self

    def _read_stdout(self):
        for line in self._process.stdout:
            self._raw_log.write(line)
            self._raw_log.flush()
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                self.errors.append("invalid_remote_json")
                continue
            kind = value.get("kind")
            if kind == "sample":
                self.samples.append(value)
            elif kind == "metadata":
                self.metadata = value
                try:
                    self._remote_pid = validated_positive_pid(value.get("pid"))
                except ValueError:
                    self.errors.append("invalid_remote_pid")
                self._metadata_event.set()
            elif kind == "identity":
                self.identities.append(value.get("identity", {}))
            elif kind == "end":
                self.end = value
            elif kind == "error":
                self.errors.append(value.get("code", "remote_error"))

    def _read_stderr(self):
        for line in self._process.stderr:
            self._stderr_log.write(line)
            self._stderr_log.flush()

    def stop(self):
        if not self._process or self._process.poll() is not None:
            return
        self._metadata_event.wait(timeout=3)
        if self._remote_pid is None:
            self.errors.append("clean_stop_unavailable")
            return
        command = shlex.join(["kill", "-TERM", str(validated_positive_pid(self._remote_pid))])
        try:
            completed = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.ssh_host, command],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            self.errors.append("clean_stop_timeout")
            return
        if completed.returncode:
            self.errors.append("clean_stop_failed")

    def join(self, timeout=15):
        if not self._process:
            return self.result()
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.errors.append("collector_timeout")
            self._process.kill()
            self._process.wait(timeout=5)
        if self._reader:
            self._reader.join(timeout=5)
        if self._stderr_reader:
            self._stderr_reader.join(timeout=5)
        self._raw_log.close()
        self._stderr_log.close()
        if self._process.returncode and not self.end and not self.errors:
            self.errors.append("remote_failure")
        if not self.end and self._process.returncode == 0:
            self.errors.append("missing_remote_end")
        return self.result()

    def result(self):
        derived = []
        for previous, current in zip(self.samples, self.samples[1:], strict=False):
            try:
                derived.append(resource_delta(previous, current))
            except ValueError as error:
                self.errors.append(str(error))
        if any(item.get("memory_events", {}).get("oom_kill", 0) for item in self.samples):
            self.errors.append("oom_kill")
        observed = sum(item["elapsedSeconds"] for item in derived if item["elapsedSeconds"] <= 2.5)
        return {
            "metadata": self.metadata,
            "end": self.end,
            "identities": self.identities,
            "rawSamples": self.samples,
            "samples": derived,
            "coverage": min(1.0, observed / self.duration),
            "errors": sorted(set(self.errors)),
        }


@dataclass
class GeneratorCollector:
    pid: int
    interface: str | None = None
    interval: float = 1.0
    samples: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    _stop: object = field(default_factory=threading.Event)
    _thread: object = None
    _started_monotonic: float | None = None
    _ended_monotonic: float | None = None

    def start(self):
        self._started_monotonic = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sample()
            except psutil.NoSuchProcess:
                break
            except Exception:
                self.errors.append("generator_sample_failure")
                break
            self._stop.wait(self.interval)
        self._ended_monotonic = time.monotonic()

    def stop(self):
        self._stop.set()

    def join(self, timeout=5):
        if self._thread:
            self._thread.join(timeout)
        return self.result()

    def sample(self):
        if psutil is None:
            raise RuntimeError("psutil_7_2_2_required")
        process = psutil.Process(self.pid)
        monotonic = time.monotonic()
        realtime = time.time()
        cpu = process.cpu_times()
        memory = process.memory_info()
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        net = psutil.net_io_counters(pernic=True).get(self.interface) if self.interface else None
        item = {
            "seconds": monotonic,
            "monotonicSeconds": monotonic,
            "realtimeSeconds": realtime,
            "cpuSeconds": cpu.user + cpu.system,
            "rssBytes": memory.rss,
            "availableMemoryBytes": virtual.available,
            "swapUsedBytes": swap.used,
            "hostCpuPercent": psutil.cpu_percent(interval=None, percpu=True),
            "interfaceBytesSent": net.bytes_sent if net else None,
            "interfaceBytesRecv": net.bytes_recv if net else None,
        }
        if self.samples:
            elapsed = monotonic - self.samples[-1]["monotonicSeconds"]
            item["processCpuMillicores"] = (
                (item["cpuSeconds"] - self.samples[-1]["cpuSeconds"]) / elapsed * 1000
                if elapsed > 0
                else None
            )
        self.samples.append(item)
        return item

    def result(self):
        ended = self._ended_monotonic or time.monotonic()
        elapsed = max(0.0, ended - (self._started_monotonic or ended))
        observed = (
            max(0.0, self.samples[-1]["monotonicSeconds"] - self.samples[0]["monotonicSeconds"])
            if len(self.samples) > 1
            else 0.0
        )
        sustained = False
        segment = []
        for item in self.samples:
            if segment and item["seconds"] - segment[-1]["seconds"] > self.interval * 2.5:
                segment = []
            segment.append(item)
            if len(segment) < 2 or item["seconds"] - segment[0]["seconds"] < 5:
                continue
            host_average = sum(
                sum(entry["hostCpuPercent"]) / max(1, len(entry["hostCpuPercent"]))
                for entry in segment
            ) / len(segment)
            low_memory = all(entry["availableMemoryBytes"] < 1024**3 for entry in segment)
            swap_growth = item["swapUsedBytes"] > segment[0]["swapUsedBytes"]
            if host_average > 90 or low_memory or swap_growth:
                sustained = True
        return {
            "samples": self.samples,
            "coverage": observed / elapsed if elapsed else 0,
            "interface": self.interface,
            "errors": self.errors,
            "headroomFlag": sustained,
        }
