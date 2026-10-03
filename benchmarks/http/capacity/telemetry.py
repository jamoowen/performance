"""Local durable wrapper for the restart-aware remote pod observer."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from benchmarks.http.ramp.measure.collector import (
    resource_delta,
    validated_positive_pid,
    validated_ssh_host,
)
from benchmarks.http.ramp.measure.collector_remote import SAFE_ID, SAFE_NAME, SAFE_UID


@dataclass
class PodCollector:
    """Collect durable pod-scoped samples across same-UID container restarts."""

    ssh_host: str
    namespace: str
    pod: str
    container: str
    pod_uid: str
    container_id: str
    duration: float
    log_dir: Path | str
    interval: float = 1.0
    raw_samples: list = field(default_factory=list)
    raw_container_samples: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    identities: list = field(default_factory=list)
    events: list = field(default_factory=list)
    end: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    _process: object = None
    _reader: object = None
    _stderr_reader: object = None
    _raw_log: object = None
    _stderr_log: object = None
    _remote_pid: int | None = None
    _metadata_event: object = field(default_factory=threading.Event)
    _lock: object = field(default_factory=threading.Lock)
    _closed: bool = False

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
        log_dir = Path(self.log_dir)
        if not log_dir.is_dir():
            raise ValueError("log_dir must be an existing directory")

    def start(self):
        self._validate()
        source = Path(__file__).with_name("telemetry_remote.py").read_text()
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
        output = Path(self.log_dir)
        self._raw_log = (output / "pod-telemetry.jsonl").open("x", encoding="utf-8")
        self._stderr_log = (output / "pod-telemetry.stderr").open("x", encoding="utf-8")
        os.chmod(self._raw_log.name, 0o600)
        os.chmod(self._stderr_log.name, 0o600)
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

    def _append_error(self, value):
        with self._lock:
            if value not in self.errors:
                self.errors.append(value)

    def _read_stdout(self):
        for line in self._process.stdout:
            try:
                self._raw_log.write(line)
                self._raw_log.flush()
                os.fsync(self._raw_log.fileno())
            except OSError:
                self._append_error("raw_log_failure")
                return
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                self._append_error("invalid_remote_json")
                continue
            if not isinstance(value, dict):
                self._append_error("invalid_remote_record")
                continue
            with self._lock:
                kind = value.get("kind")
                if kind == "sample" and value.get("scope") == "pod":
                    self.raw_samples.append(value)
                elif kind == "container_sample":
                    self.raw_container_samples.append(value)
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
                elif kind == "event" and value.get("type") in {
                    "oom",
                    "restart",
                    "container_missing",
                    "pod_replaced",
                    "collector_gap",
                }:
                    self.events.append(value)
                elif kind == "error":
                    self.errors.append(value.get("code", "remote_error"))

    def _read_stderr(self):
        for line in self._process.stderr:
            try:
                self._stderr_log.write(line)
                self._stderr_log.flush()
                os.fsync(self._stderr_log.fileno())
            except OSError:
                self._append_error("stderr_log_failure")
                return

    def stop(self):
        if not self._process or self._process.poll() is not None:
            return
        self._metadata_event.wait(timeout=3)
        if self._remote_pid is None:
            self._append_error("clean_stop_unavailable")
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
            self._append_error("clean_stop_timeout")
            return
        if completed.returncode:
            self._append_error("clean_stop_failed")

    def join(self, timeout=20):
        if not self._process:
            return self.result()
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._append_error("collector_timeout")
            self._process.kill()
            self._process.wait(timeout=5)
        if self._reader:
            self._reader.join(timeout=5)
        if self._stderr_reader:
            self._stderr_reader.join(timeout=5)
        if not self._closed:
            self._raw_log.close()
            self._stderr_log.close()
            self._closed = True
        if self._process.returncode and not self.end and not self.errors:
            self._append_error("remote_failure")
        if not self.end and self._process.returncode == 0:
            self._append_error("missing_remote_end")
        return self.result()

    @staticmethod
    def _derived(samples, boundary=None):
        result = []
        errors = []
        segment = 0
        for previous, current in zip(samples, samples[1:], strict=False):
            if boundary and boundary(previous) != boundary(current):
                segment += 1
                continue
            try:
                derived = resource_delta(previous, current)
            except (KeyError, TypeError, ValueError) as error:
                errors.append(f"collector_gap:{error}")
                continue
            if boundary:
                derived["containerSegment"] = segment
            result.append(derived)
        return result, errors

    def result(self):
        with self._lock:
            raw_samples = list(self.raw_samples)
            raw_container_samples = list(self.raw_container_samples)
            errors = list(dict.fromkeys(self.errors))
            metadata = dict(self.metadata)
            end = dict(self.end)
            identities = list(self.identities)
            events = list(self.events)
        samples, sample_errors = self._derived(raw_samples)
        container_samples, container_errors = self._derived(
            raw_container_samples, lambda item: item.get("containerId")
        )
        observed = sum(item["elapsedSeconds"] for item in samples if item["elapsedSeconds"] <= 2.5)
        return {
            "metadata": metadata,
            "end": end,
            "identities": identities,
            "rawSamples": raw_samples,
            "samples": samples,
            "containerSamples": container_samples,
            "events": events,
            "coverage": min(1.0, observed / self.duration),
            "errors": list(dict.fromkeys([*errors, *sample_errors, *container_errors])),
        }
