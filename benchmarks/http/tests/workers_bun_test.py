import http.client
import os
import signal
import socket
import subprocess
import time
import unittest

from contract_test import ROOT


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def descendants(root_pid):
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True)
    by_parent = {}
    for row in rows.splitlines():
        pid, parent = map(int, row.split())
        by_parent.setdefault(parent, []).append(pid)
    found, pending = set(), [root_pid]
    while pending:
        parent = pending.pop()
        for child in by_parent.get(parent, []):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


class BunWorkersTests(unittest.TestCase):
    def test_one_worker_has_no_supervisor_child(self):
        port = free_port()
        process = subprocess.Popen(
            ["bun", "run", "launcher.js"],
            cwd=ROOT / "benchmarks/http/bun",
            env=os.environ
            | {
                "PORT": str(port),
                "SEED_COUNT": "20",
                "BACKEND": "memory",
                "ROUTER": "stdlib",
                "WORKERS": "1",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    connection.request("GET", "/healthz")
                    if connection.getresponse().status == 200:
                        connection.close()
                        break
                    connection.close()
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("one-worker launcher did not serve health checks")
            self.assertEqual(descendants(process.pid), set())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(5)
            process.stderr.close()

    def test_two_workers_serve_and_fail_fast(self):
        port = free_port()
        process = subprocess.Popen(
            ["bun", "run", "launcher.js"],
            cwd=ROOT / "benchmarks/http/bun",
            env=os.environ
            | {
                "PORT": str(port),
                "SEED_COUNT": "20",
                "BACKEND": "memory",
                "ROUTER": "stdlib",
                "WORKERS": "2",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(f"launcher exited: {process.stderr.read()}")
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    connection.request("GET", "/healthz")
                    status = connection.getresponse().status
                    connection.close()
                    if status == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            else:
                self.fail("two-worker launcher did not serve health checks")
            children = descendants(process.pid)
            self.assertGreaterEqual(
                len(children), 2, f"expected two server children, got {children}"
            )
            time.sleep(1)
            self.assertIsNone(process.poll(), "launcher exited after becoming healthy")
            alive_before_kill = [
                pid
                for pid in children
                if subprocess.run(
                    ["ps", "-p", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                ).returncode
                == 0
            ]
            self.assertGreaterEqual(len(alive_before_kill), 2, "both workers must remain alive")
            for _ in range(20):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/healthz")
                self.assertEqual(connection.getresponse().status, 200)
                connection.close()
            # The two Bun server children are leaves; killing one simulates a real worker crash.
            parent_pids = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True)
            parents = {int(row.split()[1]) for row in parent_pids.splitlines()}
            victim = next(pid for pid in children if pid not in parents)
            os.kill(victim, signal.SIGKILL)
            process.wait(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            time.sleep(0.2)
            alive = [
                pid
                for pid in children
                if subprocess.run(
                    ["ps", "-p", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                ).returncode
                == 0
            ]
            self.assertEqual(alive, [], f"worker descendants still alive: {alive}")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(5)
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
