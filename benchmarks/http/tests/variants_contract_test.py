"""Contract matrix for optional HTTP backends and routers."""

import concurrent.futures
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import time
import unittest

from contract_test import ROOT, Server, free_port


class VariantServer(Server):
    def __init__(self, command, cwd, database, backend, router):
        self.backend, self.router = backend, router
        super().__init__(command, cwd, database)

    def start(self):
        self.port = free_port()
        env = os.environ | {
            "PORT": str(self.port),
            "SEED_COUNT": str(self.seed_count),
            "DB_PATH": self.database,
            "BACKEND": self.backend,
            "ROUTER": self.router,
        }
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(self.process.stderr.read())
            try:
                if self.request("GET", "/healthz")[0] == 200:
                    return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("server did not start")


class VariantContractTests(unittest.TestCase):
    def test_matrix(self):
        with tempfile.TemporaryDirectory(prefix="http-variants-") as directory:
            root = pathlib.Path(directory)
            binary = root / "go"
            subprocess.run(
                ["go", "build", "-o", str(binary), "."], cwd=ROOT / "benchmarks/http/go", check=True
            )
            variants = [
                ("go", "sqlite", "stdlib", [str(binary)], ROOT / "benchmarks/http/go"),
                ("go", "sqlite", "chi", [str(binary)], ROOT / "benchmarks/http/go"),
                ("go", "memory", "stdlib", [str(binary)], ROOT / "benchmarks/http/go"),
                ("go", "memory", "chi", [str(binary)], ROOT / "benchmarks/http/go"),
                (
                    "bun",
                    "sqlite",
                    "stdlib",
                    ["bun", "run", "server.js"],
                    ROOT / "benchmarks/http/bun",
                ),
                (
                    "bun",
                    "sqlite",
                    "elysia",
                    ["bun", "run", "server.js"],
                    ROOT / "benchmarks/http/bun",
                ),
                (
                    "bun",
                    "memory",
                    "stdlib",
                    ["bun", "run", "server.js"],
                    ROOT / "benchmarks/http/bun",
                ),
                (
                    "bun",
                    "memory",
                    "elysia",
                    ["bun", "run", "server.js"],
                    ROOT / "benchmarks/http/bun",
                ),
            ]
            for name, backend, router, command, cwd in variants:
                with self.subTest(name=name, backend=backend, router=router):
                    server = VariantServer(
                        command, cwd, root / f"{name}-{backend}-{router}.sqlite", backend, router
                    )
                    try:
                        self.assertEqual(server.request("GET", "/products/3")[1]["id"], 3)
                        self.assertEqual(server.request("GET", "/products/%33")[1]["id"], 3)
                        self.assertEqual(
                            server.request(
                                "GET", "/products?category=books&q=PRODUCT&offset=1&limit=2"
                            )[1]["total"],
                            4,
                        )
                        self.assertEqual(server.request("POST", "/products")[0], 405)
                        self.assertEqual(
                            server.request(
                                "POST", "/cart/quote", b"{}", {"Content-Type": "application/json"}
                            )[0],
                            400,
                        )
                        oversized = (
                            b'{"items":[{"productId":1,"quantity":1}],"x":"'
                            + b"x" * (1024 * 1024)
                            + b'"}'
                        )
                        self.assertEqual(
                            server.request(
                                "POST",
                                "/cart/quote",
                                oversized,
                                {"Content-Type": "application/json"},
                            )[0],
                            413,
                        )
                        bad = b'{"events":[{"userId":1,"type":"view","value":7},{"userId":1,"type":"bad","value":2}]}'
                        self.assertEqual(
                            server.request(
                                "POST", "/events/batch", bad, {"Content-Type": "application/json"}
                            )[0],
                            400,
                        )
                        event = b'{"events":[{"userId":2,"type":"click","value":3}]}'
                        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                            statuses = list(
                                pool.map(
                                    lambda _, current_server=server, current_event=event: (
                                        current_server.request(
                                            "POST",
                                            "/events/batch",
                                            current_event,
                                            {"Content-Type": "application/json"},
                                        )[0]
                                    ),
                                    range(16),
                                )
                            )
                        self.assertEqual(statuses, [200] * 16)
                        self.assertEqual(
                            server.request("GET", "/reports/events")[1]["counts"]["click"], 16
                        )
                        if backend == "sqlite" and router == "elysia":
                            sqlite3.connect(server.database).execute(
                                "CREATE TRIGGER reject_events BEFORE INSERT ON event_totals BEGIN SELECT RAISE(ABORT, 'forced'); END"
                            ).connection.commit()
                            before = server.request("GET", "/reports/events")[1]
                            status, value, _ = server.request(
                                "POST", "/events/batch", event, {"Content-Type": "application/json"}
                            )
                            self.assertEqual((status, value), (500, {"error": "database error"}))
                            self.assertEqual(server.request("GET", "/reports/events")[1], before)
                    finally:
                        server.close()


if __name__ == "__main__":
    unittest.main()
