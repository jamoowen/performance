import concurrent.futures
import hashlib
import http.client
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[3]
SEED = 20
GO_BASE_URL, BUN_BASE_URL = os.getenv("GO_BASE_URL"), os.getenv("BUN_BASE_URL")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Server:
    def __init__(self, command, cwd, database):
        self.command, self.cwd, self.database, self.seed_count = (
            command,
            cwd,
            str(database),
            SEED,
        )
        self.process = None
        self.stderr = ""
        self.start()

    def start(self):
        self.port = free_port()
        env = os.environ | {
            "PORT": str(self.port),
            "SEED_COUNT": str(self.seed_count),
            "DB_PATH": self.database,
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
                self.stderr = self.process.stderr.read()
                self.process.stderr.close()
                raise RuntimeError(f"server exited during startup {self.command}:\n{self.stderr}")
            try:
                if self.request("GET", "/healthz")[0] == 200:
                    return
            except (OSError, http.client.HTTPException):
                time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"server did not start {self.command}:\n{self.stderr}")

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, json.loads(raw) if raw else None, response_headers

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)
        if self.process and self.process.stderr:
            self.stderr = self.process.stderr.read()
            self.process.stderr.close()

    def restart(self):
        self.stop()
        self.start()

    def close(self):
        self.stop()


class ExternalServer:
    def __init__(self, url):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base URL must be http(s)")
        self.host, self.port, self.secure = (
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            parsed.scheme == "https",
        )

    def request(self, method, path, body=None, headers=None):
        cls = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        connection = cls(self.host, self.port, timeout=10)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        raw = response.read()
        result = (
            response.status,
            json.loads(raw) if raw else None,
            dict(response.getheaders()),
        )
        connection.close()
        return result


class HTTPContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if bool(GO_BASE_URL) != bool(BUN_BASE_URL):
            raise RuntimeError("set both GO_BASE_URL and BUN_BASE_URL, or neither")
        cls.external = bool(GO_BASE_URL)
        cls.resources = ExitStack()
        cls.addClassCleanup(cls.resources.close)
        if cls.external:
            cls.go, cls.bun = ExternalServer(GO_BASE_URL), ExternalServer(BUN_BASE_URL)
            return
        directory = pathlib.Path(
            cls.resources.enter_context(tempfile.TemporaryDirectory(prefix="http-contract-"))
        )
        binary = directory / "http-go-test"
        subprocess.run(
            ["go", "build", "-o", str(binary), "."],
            cwd=ROOT / "benchmarks/http/go",
            check=True,
        )
        cls.go = Server([str(binary)], ROOT / "benchmarks/http/go", directory / "go.sqlite")
        cls.resources.callback(cls.go.close)
        cls.bun = Server(
            ["bun", "run", "server.js"],
            ROOT / "benchmarks/http/bun",
            directory / "bun.sqlite",
        )
        cls.resources.callback(cls.bun.close)

    def both(self, method, path, body=None, headers=None):
        go, bun = (
            self.go.request(method, path, body, headers),
            self.bun.request(method, path, body, headers),
        )
        self.assertEqual(go[:2], bun[:2], f"{method} {path} differs")
        return go

    def report(self, server):
        status, value, _ = server.request("GET", "/reports/events")
        self.assertEqual(status, 200)
        return value

    def test_seeded_reads(self):
        status, product, _ = self.both("GET", "/products/3")
        self.assertEqual(status, 200)
        self.assertEqual(
            product,
            {
                "id": 3,
                "name": "Product 00003",
                "category": "home",
                "priceCents": 24257,
                "stock": 111,
                "tags": ["home", "featured", "odd"],
            },
        )
        status, listing, _ = self.both("GET", "/products?category=books&q=PRODUCT&offset=1&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(
            (
                listing["total"],
                listing["offset"],
                listing["limit"],
                [x["id"] for x in listing["products"]],
            ),
            (4, 1, 2, [6, 11]),
        )
        status, report, _ = self.both("GET", "/reports/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(report["totalStock"], sum((i * 37) % 201 for i in range(1, SEED + 1)))

    def test_quote_and_hash_oracles(self):
        json_headers = {"Content-Type": "application/json"}
        status, value, _ = self.both(
            "POST",
            "/cart/quote",
            b'{"items":[{"productId":1,"quantity":2}],"coupon":"SAVE10"}',
            json_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            {x: value[x] for x in ("subtotalCents", "discountCents", "taxCents", "totalCents")},
            {
                "subtotalCents": 16838,
                "discountCents": 1683,
                "taxCents": 3031,
                "totalCents": 18186,
            },
        )
        event_body = b'{"events":[{"userId":7,"type":"view","value":2},{"userId":7,"type":"purchase","value":9}]}'
        status, value, _ = self.both("POST", "/events/batch", event_body, json_headers)
        self.assertEqual(status, 200)
        self.assertEqual(value["sha256"], hashlib.sha256(b"7:view:2\n7:purchase:9\n").hexdigest())

    def test_errors_and_methods(self):
        for path in (
            "/products/nope",
            "/products/",
            "/products/+1",
            "/products/-1",
            "/products/1.0",
        ):
            self.assertEqual(self.both("GET", path)[0], 400)
        self.assertEqual(self.both("GET", "/products/0003")[1]["id"], 3)
        for name in ("offset", "limit"):
            self.assertEqual(self.both("GET", f"/products?{name}=")[0], 200)
            for raw in ("+1", "-1", "1.0"):
                self.assertEqual(self.both("GET", f"/products?{name}={raw}")[0], 400)
        self.assertEqual(self.both("GET", "/products?offset=00")[1]["offset"], 0)
        self.assertEqual(self.both("GET", "/products?limit=00")[0], 400)
        self.assertEqual(self.both("GET", "/products?limit=020")[1]["limit"], 20)
        self.assertEqual(self.both("GET", "/products/999")[0], 404)
        for method, path, allow in (
            ("POST", "/products", "GET, HEAD"),
            ("GET", "/cart/quote", "POST"),
            ("GET", "/events/batch", "POST"),
        ):
            status, body, headers = self.both(method, path)
            self.assertEqual(
                (status, body, headers.get("Allow")),
                (405, {"error": "method not allowed"}, allow),
            )
        self.assertEqual(self.both("GET", "/missing")[0], 404)

    def test_inputs_and_atomicity(self):
        json_headers = {"Content-Type": "application/json"}
        for body in (
            b"{}",
            b"{",
            b"{} {}",
            b"[]",
            b'{"items":[{"productId":1}]}',
            b'{"items":[{"productId":1,"quantity":1}],"coupon":""}',
        ):
            self.assertEqual(self.both("POST", "/cart/quote", body, json_headers)[0], 400)
        self.assertEqual(
            self.both(
                "POST",
                "/cart/quote",
                b'{"items":[{"productId":1,"quantity":1}],"coupon":null}',
                json_headers,
            )[0],
            200,
        )
        self.assertEqual(
            self.both(
                "POST",
                "/cart/quote",
                b'{"items":[{"productId":1,"quantity":1,"ignored":true}],"ignored":true}',
                json_headers,
            )[0],
            200,
        )
        self.assertEqual(
            self.both(
                "POST",
                "/cart/quote",
                b'{"items":[{"productId":1,"quantity":20},{"productId":1,"quantity":20}]}',
                json_headers,
            )[0],
            400,
        )
        for body in (
            b"{}",
            b"[]",
            b'{"events":[{"userId":1,"type":"view"}]}',
            b'{"events":[{"userId":1,"type":"view","value":null}]}',
        ):
            self.assertEqual(self.both("POST", "/events/batch", body, json_headers)[0], 400)
        for body in (
            b'{"items":[{"productId":1,"quantity":1}],"x":"' + b"x" * 1048576 + b'"}',
            b'{"items":[{"productId":1,"quantity":1}],"x":"' + "£".encode() * 524288 + b'"}',
        ):
            self.assertEqual(self.both("POST", "/cart/quote", body, json_headers)[0], 413)
        before = [self.report(server) for server in (self.go, self.bun)]
        self.assertEqual(
            self.both(
                "POST",
                "/events/batch",
                b'{"events":[{"userId":1,"type":"view","value":7},{"userId":1,"type":"bad","value":2}]}',
                json_headers,
            )[0],
            400,
        )
        self.assertEqual([self.report(server) for server in (self.go, self.bun)], before)

    def test_concurrent_writes_and_reads(self):
        json_headers, event_body = (
            {"Content-Type": "application/json"},
            b'{"events":[{"userId":2,"type":"click","value":3}]}',
        )
        for server in (self.go, self.bun):
            before = self.report(server)
            with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
                statuses = list(
                    pool.map(
                        lambda _, current_server=server: current_server.request(
                            "POST", "/events/batch", event_body, json_headers
                        )[0],
                        range(64),
                    )
                ) + list(
                    pool.map(
                        lambda _, current_server=server: current_server.request(
                            "GET", "/products/1"
                        )[0],
                        range(64),
                    )
                )
            self.assertTrue(all(x == 200 for x in statuses))
            after = self.report(server)
            self.assertEqual(after["counts"]["click"] - before["counts"]["click"], 64)
            self.assertEqual(after["values"]["click"] - before["values"]["click"], 192)

    def test_native_router_contract(self):
        status, body, headers = self.both("HEAD", "/products/3")
        self.assertEqual((status, body), (200, None))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(self.both("GET", "/products/%33")[1]["id"], 3)
        self.assertEqual(self.both("GET", "/products/3/extra")[0], 404)
        status, body, headers = self.both("POST", "/products/3")
        self.assertEqual(
            (status, body, headers["Allow"]), (405, {"error": "method not allowed"}, "GET, HEAD")
        )
        self.assertEqual(self.both("POST", "/unknown")[0], 404)
        self.assertEqual(self.both("GET", "/products/")[0], 400)

    @unittest.skipIf(bool(GO_BASE_URL or BUN_BASE_URL), "external endpoints cannot be modified")
    def test_database_trigger_failures_are_json_and_atomic(self):
        headers = {"Content-Type": "application/json"}
        body = b'{"events":[{"userId":4,"type":"view","value":8}]}'
        for server in (self.go, self.bun):
            before = self.report(server)
            with sqlite3.connect(server.database) as database:
                database.execute(
                    "CREATE TRIGGER reject_event BEFORE INSERT ON event_totals "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
                )
            try:
                status, response, _ = server.request("POST", "/events/batch", body, headers)
                self.assertEqual((status, response), (500, {"error": "database error"}))
                self.assertEqual(self.report(server), before)
            finally:
                with sqlite3.connect(server.database) as database:
                    database.execute("DROP TRIGGER IF EXISTS reject_event")

    @unittest.skipIf(bool(GO_BASE_URL or BUN_BASE_URL), "external containers are not restartable")
    def test_restart_seed_guard_and_sqlite(self):
        json_headers, event_body = (
            {"Content-Type": "application/json"},
            b'{"events":[{"userId":3,"type":"view","value":4}]}',
        )
        for server in (self.go, self.bun):
            self.assertEqual(
                server.request("POST", "/events/batch", event_body, json_headers)[0],
                200,
            )
            expected = self.report(server)
            server.restart()
            self.assertEqual(self.report(server), expected)
            self.assertEqual(server.request("GET", "/products/20")[1]["name"], "Product 00020")
            server.stop()
            with sqlite3.connect(server.database) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(db.execute("SELECT count(*) FROM products").fetchone()[0], SEED)
                self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], SEED)
                self.assertEqual(
                    [x[1] for x in db.execute("PRAGMA table_info(event_totals)")],
                    ["user_id", "type", "count", "value_total"],
                )
                self.assertIn(
                    "products_category_id",
                    [x[1] for x in db.execute("PRAGMA index_list(products)")],
                )
                self.assertGreaterEqual(
                    db.execute("SELECT COALESCE(SUM(count),0) FROM event_totals").fetchone()[0],
                    1,
                )
            server.seed_count = 21
            with self.assertRaisesRegex(RuntimeError, "metadata does not match"):
                server.start()
            server.seed_count = SEED
            server.start()


if __name__ == "__main__":
    unittest.main(verbosity=2)
