"""Subprocess coverage for the streamed diagnostics transport helper."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from measure import diagnostics_remote

FAKE_KUBECTL = r"""#!/usr/bin/env python3
import gzip
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

mode = os.environ.get("FAKE_KUBECTL_MODE", "success")
pid_file = os.environ.get("FAKE_KUBECTL_PID")
if pid_file:
    open(pid_file, "w", encoding="utf-8").write(str(os.getpid()))

if mode == "never":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(1)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/runtime":
            body = json.dumps({
                "schema_version": 1,
                "runtime": "go",
                "process_id": 123,
                "time_unix": time.time(),
            }).encode()
            self.send_response(200)
        elif self.path.startswith("/cpu"):
            cpu_started = os.environ.get("FAKE_KUBECTL_CPU_STARTED")
            if cpu_started:
                open(cpu_started, "w", encoding="utf-8").write("started")
            if mode == "cpu-failure":
                time.sleep(0.15)
                body = b"cpu failed"
                self.send_response(500)
            else:
                time.sleep(5 if mode == "long-cpu" else 1)
                body = gzip.compress(b"profile")
                self.send_response(200)
        elif self.path in {"/heap", "/allocs", "/goroutine", "/block", "/mutex"}:
            body = gzip.compress(self.path.encode())
            self.send_response(200)
        elif self.path == "/oversized":
            body = b"x" * (20 * 1024 * 1024 + 2)
            self.send_response(200)
        else:
            body = b"missing"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
stopped = threading.Event()
def stop(*_):
    stopped.set()
    server.shutdown()
signal.signal(signal.SIGTERM, stop)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"Forwarding from 127.0.0.1:{server.server_address[1]} -> 6060", flush=True)
while not stopped.wait(1):
    pass
"""


class DiagnosticsTransportTest(unittest.TestCase):
    def start_helper(self, directory, mode="success"):
        fake_kubectl = directory / "kubectl"
        fake_kubectl.write_text(FAKE_KUBECTL)
        fake_kubectl.chmod(0o755)
        pid_file = directory / "kubectl.pid"
        cpu_started_file = directory / "cpu-started"
        environment = {
            **os.environ,
            "PATH": f"{directory}:{os.environ['PATH']}",
            "FAKE_KUBECTL_MODE": mode,
            "FAKE_KUBECTL_PID": str(pid_file),
            "FAKE_KUBECTL_CPU_STARTED": str(cpu_started_file),
        }
        process = subprocess.Popen(
            [sys.executable, "-u", "-", "namespace", "pod", "go", "0.05", "1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        process.stdin.write(diagnostics_remote.remote_program())
        process.stdin.close()
        process.stdin = None
        return process, pid_file, cpu_started_file

    def collect(self, process, timeout=10):
        stdout, stderr = process.communicate(timeout=timeout)
        return [json.loads(line) for line in stdout.splitlines()], stderr

    def assert_child_reaped(self, pid_file):
        for _ in range(30):
            if pid_file.exists():
                process_id = int(pid_file.read_text())
                try:
                    os.kill(process_id, 0)
                except ProcessLookupError:
                    return
            time.sleep(0.05)
        self.fail("fake kubectl was not reaped")

    def wait_for_pid_file(self, pid_file):
        for _ in range(30):
            if pid_file.exists():
                return
            time.sleep(0.05)
        self.fail("fake kubectl did not start")

    def test_success_captures_all_go_profiles_and_ends_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            process, _, _ = self.start_helper(Path(temporary))
            events, stderr = self.collect(process)

        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(events[-1], {"incomplete": False, "type": "end"})
        self.assertEqual(sum(event["type"] == "ready" for event in events), 1)
        profiles = [event for event in events if event["type"] == "profile"]
        self.assertEqual(len(profiles), 11)
        self.assertEqual(profiles[5]["name"], "cpu.pprof")
        self.assertLess(profiles[5]["wallclock_start"], profiles[5]["wallclock_end"])
        snapshots = [event for event in events if event["type"] == "snapshot"]
        self.assertGreaterEqual(len(snapshots), 3)
        self.assertTrue(all("time_unix" in event["runtime"] for event in snapshots))

    def test_cpu_failure_emits_error_but_runtime_polling_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            process, _, _ = self.start_helper(Path(temporary), mode="cpu-failure")
            events, _ = self.collect(process)

        self.assertNotEqual(process.returncode, 0, events)
        self.assertTrue(any("CPU profile:" in event.get("error", "") for event in events), events)
        self.assertTrue(any(event.get("phase") == "sample" for event in events))
        self.assertTrue(events[-1]["incomplete"])
        self.assertEqual(events[-1]["type"], "end")

    def test_sigterm_during_readiness_reaps_child_and_ends_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            process, pid_file, _ = self.start_helper(Path(temporary), mode="never")
            self.wait_for_pid_file(pid_file)
            process.send_signal(signal.SIGTERM)
            events, _ = self.collect(process)
            self.assert_child_reaped(pid_file)

        self.assertNotEqual(process.returncode, 0, events)
        self.assertEqual(events[-1]["type"], "end")
        self.assertTrue(events[-1]["incomplete"])

    def test_sigterm_during_cpu_reaps_child_and_ends_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            process, pid_file, cpu_started_file = self.start_helper(
                Path(temporary), mode="long-cpu"
            )
            self.wait_for_pid_file(cpu_started_file)
            process.send_signal(signal.SIGTERM)
            events, _ = self.collect(process)
            self.assert_child_reaped(pid_file)

        self.assertNotEqual(process.returncode, 0, events)
        self.assertEqual(events[-1]["type"], "end")
        self.assertTrue(events[-1]["incomplete"])

    def test_readiness_deadline_is_fifteen_seconds(self):
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            process, _, _ = self.start_helper(Path(temporary), mode="never")
            events, _ = self.collect(process, timeout=20)
            elapsed = time.monotonic() - started

        self.assertNotEqual(process.returncode, 0)
        self.assertGreaterEqual(elapsed, 14.5)
        self.assertLess(elapsed, 18)
        self.assertEqual(events[-1]["type"], "end")

    def test_response_size_limit(self):
        class OversizedResponse(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"x" * (20 * 1024 * 1024 + 1)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), OversizedResponse)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "exceeds 20 MiB"):
                diagnostics_remote.read_response(server.server_address[1], "/oversized", timeout=5)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
