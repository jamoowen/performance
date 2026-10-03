#!/usr/bin/env python3
"""External SQLite-ramp API contract tests; no runtime internals required."""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import math
import unittest
import urllib.error
import urllib.parse
import urllib.request


class Contract(unittest.TestCase):
    base_url: str
    runtime: str
    framework: str
    seed_count: int

    def request(self, path: str, method="GET", body: bytes | None = None, headers=None):
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers.items()), error.read()

    def json(self, path, method="GET", body=None, headers=None):
        status, response_headers, raw = self.request(path, method, body, headers)
        return status, response_headers, json.loads(raw)

    def assert_error(self, path, expected, **kwargs):
        status, _, body = self.json(path, **kwargs)
        self.assertEqual(status, expected)
        self.assertIsInstance(body.get("error"), str)

    def test_health_metadata_and_pragmas(self):
        status, _, health = self.json("/healthz")
        self.assertEqual((status, health), (200, {"status": "ok"}))
        status, _, info = self.json("/benchmark/info")
        self.assertEqual(status, 200)
        self.assertEqual(info["experiment"], "sqlite-ramp-v2")
        self.assertEqual(info["runtime"], self.runtime)
        self.assertEqual(info["framework"], self.framework)
        self.assertEqual(info["seedCount"], self.seed_count)
        self.assertEqual(info["workers"], 1)
        for key in (
            "runtimeVersion",
            "frameworkVersion",
            "driver",
            "driverVersion",
            "sqliteVersion",
        ):
            self.assertIsInstance(info[key], str)
        self.assertEqual(
            info["pragmas"],
            {
                "journal_mode": "wal",
                "synchronous": 1,
                "foreign_keys": 1,
                "busy_timeout": 5000,
                "cache_size": -2000,
                "wal_autocheckpoint": 1000,
                "temp_store": 2,
            },
        )
        self.assertIsInstance(info.get("compileOptions"), list)
        self.assertTrue(info["compileOptions"])
        self.assertTrue(
            all(isinstance(option, str) and option for option in info["compileOptions"])
        )

    def test_seed_detail_list_and_bounds(self):
        status, headers, product = self.json("/products/1")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(product), {"id", "name", "category", "priceCents", "stock", "revision"}
        )
        self.assertEqual(
            product,
            {
                "id": 1,
                "name": "Product00001",
                "category": "books",
                "priceCents": 8419,
                "stock": 37,
                "revision": 0,
            },
        )
        self.assert_timing(headers)
        status, headers, listing = self.json("/products")
        self.assertEqual(status, 200)
        self.assertEqual(set(listing), {"products", "total", "offset", "limit"})
        self.assertEqual(
            (listing["total"], listing["offset"], listing["limit"], len(listing["products"])),
            (self.seed_count, 0, 20, 20),
        )
        self.assert_timing(headers)
        status, headers, last = self.json(f"/products?limit=1&offset={self.seed_count - 1}")
        self.assertEqual(status, 200)
        self.assertEqual(
            (last["total"], last["offset"], last["limit"]),
            (self.seed_count, self.seed_count - 1, 1),
        )
        self.assertEqual(last["products"][0]["id"], self.seed_count)
        self.assert_timing(headers)
        for path in (
            "/products/0",
            "/products/-1",
            "/products/+1",
            "/products/1.0",
            "/products/9007199254740992",
            "/products?limit=0",
            "/products?limit=101",
            "/products?offset=-1",
            f"/products?offset={self.seed_count + 1}",
            "/products?wat=1",
            "/products?limit=1&limit=2",
        ):
            self.assert_error(path, 400)
        self.assert_error(f"/products/{self.seed_count + 1}", 404)

    def test_stock_validation_and_body_limit(self):
        endpoint = "/products/2/stock"
        for value in (
            b"{}",
            b'{"delta":true}',
            b'{"delta":1.0}',
            b'{"delta":1e0}',
            b'{"delta":101}',
            b'{"delta":1,"extra":1}',
        ):
            self.assert_error(
                endpoint,
                400,
                method="POST",
                body=value,
                headers={"content-type": "application/json"},
            )
        self.assert_error(
            endpoint,
            415,
            method="POST",
            body=b'{"delta":1}',
            headers={"content-type": "text/plain"},
        )
        self.assert_error(
            endpoint,
            413,
            method="POST",
            body=b"x" * 65537,
            headers={"content-type": "application/json"},
        )
        self.assert_error(
            "/products/999999/stock",
            404,
            method="POST",
            body=b'{"delta":1}',
            headers={"content-type": "application/json"},
        )
        self.assert_chunked_too_large(endpoint)

    def test_concurrent_atomic_updates_and_integrity(self):
        _, _, before = self.json("/benchmark/integrity")
        endpoint = "/products/3/stock"

        def update(_):
            return self.json(endpoint, "POST", b'{"delta":1}', {"content-type": "application/json"})

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(update, range(40)))
        self.assertTrue(all(status == 200 for status, _, _ in results), results)
        self.assertTrue(all(set(body) == {"id", "stock", "revision"} for _, _, body in results))
        self.assertTrue(all(body["id"] == 3 for _, _, body in results))
        self.assertEqual(sorted(body["revision"] for _, _, body in results), list(range(1, 41)))
        self.assertTrue(all(body["stock"] == 111 + body["revision"] for _, _, body in results))
        for _, headers, _ in results:
            self.assert_json_content_type(headers)
            self.assert_timing(headers)
        _, _, after = self.json("/benchmark/integrity")
        self.assertEqual(after["rows"], before["rows"])
        self.assertEqual(after["totalRevisions"], before["totalRevisions"] + 40)
        self.assertEqual(after["totalStock"], before["totalStock"] + 40)

    def assert_timing(self, headers):
        raw = headers.get("Server-Timing") or headers.get("server-timing")
        self.assertIsNotNone(raw)
        values = {
            part.split(";")[0].strip(): float(part.split("dur=")[1]) for part in raw.split(",")
        }
        self.assertTrue(math.isfinite(values["service"]))
        self.assertTrue(math.isfinite(values["db"]))
        self.assertGreaterEqual(values["service"], 0)
        self.assertGreaterEqual(values["db"], 0)
        self.assertLessEqual(values["db"], values["service"] + 0.001)

    def assert_json_content_type(self, headers):
        self.assertEqual(
            (headers.get("Content-Type") or headers.get("content-type", "")).split(";", 1)[0],
            "application/json",
        )

    def assert_chunked_too_large(self, endpoint):
        parsed = urllib.parse.urlparse(self.base_url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        connection.putrequest("POST", endpoint)
        connection.putheader("content-type", "application/json")
        connection.putheader("transfer-encoding", "chunked")
        connection.endheaders()
        payload = b"x" * 65537
        connection.send(f"{len(payload):X}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 413)
        self.assertIsInstance(body.get("error"), str)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--framework", required=True)
    parser.add_argument("--seed-count", type=int, default=100)
    arguments, rest = parser.parse_known_args()
    Contract.base_url = arguments.base_url.rstrip("/")
    Contract.runtime = arguments.runtime
    Contract.framework = arguments.framework
    Contract.seed_count = arguments.seed_count
    unittest.main(argv=["contract_test.py", *rest])


if __name__ == "__main__":
    main()
