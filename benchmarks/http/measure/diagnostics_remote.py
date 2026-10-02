"""Remote helper for a loopback-only Kubernetes admin diagnostic session.

The complete module is sent to ``python3 -`` over SSH. Keep its imports in
the standard library so the helper works on the benchmark host without local
package installation.
"""

import base64
import json
import math
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
PORT_FORWARD_READY_TIMEOUT = 15
PORT_FORWARD_OUTPUT_LINES = 64


class StopRequested(RuntimeError):
    """Raised when the remote helper is asked to stop."""


def ssh_command(host, namespace, pod, runtime, interval, seconds):
    if not host or host.startswith("-") or any(char.isspace() for char in host):
        raise ValueError("invalid SSH host")
    if not pod or any(char.isspace() for char in pod) or "/" in pod:
        raise ValueError("invalid pod name")
    remote = shlex.join(
        ["python3", "-u", "-", namespace, pod, runtime, str(interval), str(seconds)]
    )
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote]


def remote_program():
    """Return the executable remote helper."""
    return Path(__file__).read_text(encoding="utf-8")


def emit_event(lock, event_type, **values):
    """Write one JSONL event without interleaving CPU-thread output."""
    gate = values.pop("_gate", None)
    with lock:
        if gate is not None and not gate["open"]:
            return False
        print(json.dumps({"type": event_type, **values}, sort_keys=True), flush=True)
        return True


def validate_runtime(runtime, expected_runtime):
    """Reject malformed or cross-runtime diagnostics responses."""
    if not isinstance(runtime, dict):
        raise RuntimeError("runtime diagnostics response is not an object")
    if runtime.get("schema_version") != 1:
        raise RuntimeError("runtime diagnostics schema version is invalid")
    if runtime.get("runtime") != expected_runtime:
        raise RuntimeError("runtime diagnostics runtime does not match request")
    process_id = runtime.get("process_id")
    if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
        raise RuntimeError("runtime diagnostics process ID is invalid")
    return runtime


def read_response(port, path, timeout):
    """Fetch a bounded local diagnostics response."""
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("diagnostic response exceeds 20 MiB")
    return body


def read_runtime(port, runtime):
    """Fetch and validate one runtime snapshot."""
    try:
        value = json.loads(read_response(port, "/runtime", timeout=5))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid runtime diagnostics JSON: {error}") from error
    return validate_runtime(value, runtime)


def drain_port_forward_output(stream, lines, finished):
    """Drain kubectl output without accumulating unbounded output in memory."""
    try:
        for line in stream:
            try:
                lines.put(line, timeout=0.1)
            except queue.Full:
                pass
    finally:
        finished.set()


def wait_for_port_forward(process, lines, stop):
    """Return kubectl's selected loopback port once forwarding is ready."""
    deadline = time.monotonic() + PORT_FORWARD_READY_TIMEOUT
    while True:
        if stop.is_set():
            raise StopRequested("diagnostics stopped while waiting for port-forward")
        if process.poll() is not None:
            raise RuntimeError("port-forward exited")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("port-forward deadline exceeded")
        try:
            line = lines.get(timeout=min(0.2, remaining))
        except queue.Empty:
            continue
        match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
        if match:
            return int(match.group(1))


def close_port_forward(process):
    """Close the forwarding connection and reap kubectl promptly."""
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()


def emit_optional_profiles(port, runtime, phase, event_lock, stop, event_gate):
    """Capture Go's optional instantaneous profiles without aborting CPU capture."""
    if runtime != "go":
        return
    for kind in ("heap", "allocs", "goroutine", "block", "mutex"):
        if stop.is_set():
            raise StopRequested("diagnostics stopped while collecting profiles")
        try:
            body = read_response(port, f"/{kind}", timeout=5)
            if stop.is_set():
                raise StopRequested("diagnostics stopped while collecting profiles")
            emit_event(
                event_lock,
                "profile",
                name=f"{kind}-{phase}.pprof",
                data=base64.b64encode(body).decode("ascii"),
                _gate=event_gate,
            )
        except StopRequested:
            raise
        except Exception as error:  # Optional diagnostic data must not stop capture.
            emit_event(event_lock, "error", error=f"{kind} profile: {error}", _gate=event_gate)


def start_cpu_capture(port, runtime, seconds, event_lock, event_gate, result):
    """Fetch the CPU profile and record its true request wall-clock interval."""
    try:
        started = time.time()
        body = read_response(port, f"/cpu?seconds={seconds}", timeout=seconds + 10)
        ended = time.time()
        emit_event(
            event_lock,
            "profile",
            name="cpu.pprof" if runtime == "go" else "jsc-cpu.json",
            data=base64.b64encode(body).decode("ascii"),
            wallclock_start=started,
            wallclock_end=ended,
            _gate=event_gate,
        )
        result["captured"] = True
    except Exception as error:  # Report from this thread under the event lock.
        result["error"] = error
        emit_event(event_lock, "error", error=f"CPU profile: {error}", _gate=event_gate)


def parse_arguments(argv):
    """Parse and validate the SSH helper invocation arguments."""
    if len(argv) != 6:
        raise ValueError("expected namespace, pod, runtime, sample interval, and seconds")
    namespace, pod, runtime, interval_value, seconds_value = argv[1:]
    if not namespace or namespace.startswith("-") or any(char.isspace() for char in namespace):
        raise ValueError("invalid namespace")
    if not pod or pod.startswith("-") or "/" in pod or any(char.isspace() for char in pod):
        raise ValueError("invalid pod")
    if runtime not in {"go", "bun"}:
        raise ValueError("runtime must be go or bun")
    interval = float(interval_value)
    seconds = int(seconds_value)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("sample interval must be positive")
    if not 1 <= seconds <= 120:
        raise ValueError("diagnostics seconds must be between 1 and 120")
    return namespace, pod, runtime, interval, seconds


def remote_main():
    """Run the remote diagnostic lifecycle and return a process exit status."""
    event_lock = threading.Lock()
    stop = threading.Event()
    process = None
    cpu_thread = None
    cpu_result = {"captured": False}
    event_gate = {"open": True}
    incomplete = False
    failed = False

    def request_stop(*_):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGHUP, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        namespace, pod, runtime, interval, seconds = parse_arguments(sys.argv)
        port_forward = ["port-forward", "pod/" + pod]
        loopback_address = ["--address", "127.0.0.1"]
        process = subprocess.Popen(
            ["kubectl", "-n", namespace, *port_forward, ":6060", *loopback_address],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines = queue.Queue(maxsize=PORT_FORWARD_OUTPUT_LINES)
        reader_finished = threading.Event()
        reader = threading.Thread(
            target=drain_port_forward_output,
            args=(process.stdout, lines, reader_finished),
            daemon=True,
        )
        reader.start()
        port = wait_for_port_forward(process, lines, stop)
        if stop.is_set():
            raise StopRequested("diagnostics stopped before admin requests")

        before = read_runtime(port, runtime)
        emit_event(event_lock, "snapshot", runtime=before, phase="before")
        emit_optional_profiles(port, runtime, "before", event_lock, stop, event_gate)
        if stop.is_set():
            raise StopRequested("diagnostics stopped before CPU capture")

        # This must precede CPU launch so the parent can begin measurement promptly.
        emit_event(event_lock, "ready", runtime=before)
        cpu_thread = threading.Thread(
            target=start_cpu_capture,
            args=(port, runtime, seconds, event_lock, event_gate, cpu_result),
            daemon=True,
        )
        cpu_thread.start()

        while cpu_thread.is_alive():
            if stop.wait(interval):
                incomplete = True
                raise StopRequested("diagnostics stopped during CPU capture")
            try:
                if stop.is_set():
                    raise StopRequested("diagnostics stopped before runtime sample")
                sample = read_runtime(port, runtime)
                if stop.is_set():
                    raise StopRequested("diagnostics stopped after runtime sample")
                emit_event(event_lock, "snapshot", runtime=sample, phase="sample")
            except StopRequested:
                raise
            except Exception as error:
                emit_event(event_lock, "error", error=f"runtime sample: {error}")

        cpu_thread.join()
        if "error" in cpu_result:
            incomplete = True
            raise RuntimeError(f"CPU profile: {cpu_result['error']}")
        if stop.is_set():
            incomplete = True
            raise StopRequested("diagnostics stopped after CPU capture")

        after = read_runtime(port, runtime)
        emit_event(event_lock, "snapshot", runtime=after, phase="after")
        emit_optional_profiles(port, runtime, "after", event_lock, stop, event_gate)
    except Exception as error:
        failed = True
        if not cpu_result["captured"]:
            incomplete = True
        if "error" not in cpu_result:
            emit_event(event_lock, "error", error=str(error))
    finally:
        # Closing kubectl first closes the HTTP connection and interrupts a running CPU request.
        close_port_forward(process)
        if cpu_thread is not None:
            cpu_thread.join(timeout=5)
            if cpu_thread.is_alive():
                failed = True
                incomplete = True
                emit_event(event_lock, "error", error="CPU profile thread did not stop")
        if not cpu_result["captured"]:
            incomplete = True
        with event_lock:
            print(json.dumps({"incomplete": incomplete, "type": "end"}, sort_keys=True), flush=True)
            event_gate["open"] = False
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(remote_main())
