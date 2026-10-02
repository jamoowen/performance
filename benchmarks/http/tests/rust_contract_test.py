"""Full Go-to-Rust HTTP contract parity tests, including the memory backend."""

import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack

from contract_test import ROOT, HTTPContractTests, Server

RUST_DIR = ROOT / "benchmarks/http/rust"


def build_binaries(directory):
    go_binary = directory / "http-go-contract"
    subprocess.run(
        ["go", "build", "-o", str(go_binary), "."], cwd=ROOT / "benchmarks/http/go", check=True
    )
    subprocess.run(["cargo", "build", "--locked"], cwd=RUST_DIR, check=True)
    return go_binary, RUST_DIR / "target/debug/performance-http-rust"


class CanonicalHeaderServer(Server):
    """Make the standard-library test helper's header lookups case-insensitive."""

    def request(self, *args, **kwargs):
        status, body, headers = super().request(*args, **kwargs)
        return status, body, {key.title(): value for key, value in headers.items()}


class RustSqliteContractTests(HTTPContractTests):
    """Run every established Go/Bun contract assertion with Go and Rust SQLite."""

    @classmethod
    def setUpClass(cls):
        cls.external = False
        cls.resources = ExitStack()
        cls.addClassCleanup(cls.resources.close)
        directory = pathlib.Path(
            cls.resources.enter_context(tempfile.TemporaryDirectory(prefix="rust-contract-"))
        )
        go_binary, rust_binary = build_binaries(directory)
        cls.go = Server([str(go_binary)], ROOT / "benchmarks/http/go", directory / "go.sqlite")
        cls.bun = CanonicalHeaderServer([str(rust_binary)], RUST_DIR, directory / "rust.sqlite")
        cls.resources.callback(cls.bun.close)
        cls.resources.callback(cls.go.close)

    def both(self, method, path, body=None, headers=None):
        go, rust = (
            self.go.request(method, path, body, headers),
            self.bun.request(method, path, body, headers),
        )
        self.assertEqual(go[:2], rust[:2], f"{method} {path} differs")
        for header in ("content-type", "allow"):
            self.assertEqual(
                go[2].get(header.title()),
                rust[2].get(header.title()),
                f"{method} {path} {header} differs",
            )
        return go

    def test_corrupt_metadata_is_rejected_at_startup(self):
        directory = pathlib.Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        database = directory / "corrupt.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        with self.assertRaisesRegex(RuntimeError, "Query returned no rows|metadata"):
            Server(self.bun.command, RUST_DIR, database)


class RustMemoryContractTests(HTTPContractTests):
    """Run request-level contract cases against two independent memory instances."""

    @classmethod
    def setUpClass(cls):
        cls.external = False
        cls.resources = ExitStack()
        cls.addClassCleanup(cls.resources.close)
        directory = pathlib.Path(
            cls.resources.enter_context(tempfile.TemporaryDirectory(prefix="rust-memory-contract-"))
        )
        _, rust_binary = build_binaries(directory)
        cls.go = CanonicalHeaderServer(
            ["env", "BACKEND=memory", str(rust_binary)], RUST_DIR, directory / "memory-a.sqlite"
        )
        cls.bun = CanonicalHeaderServer(
            ["env", "BACKEND=memory", str(rust_binary)], RUST_DIR, directory / "memory-b.sqlite"
        )
        cls.resources.callback(cls.bun.close)
        cls.resources.callback(cls.go.close)

    @unittest.skip("the in-memory backend intentionally has no SQLite file")
    def test_database_trigger_failures_are_json_and_atomic(self):
        pass

    @unittest.skip("the in-memory backend intentionally has no persistent SQLite file")
    def test_restart_seed_guard_and_sqlite(self):
        pass


# Avoid unittest discovering the imported baseline suite a second time.
del HTTPContractTests


if __name__ == "__main__":
    unittest.main(verbosity=2)
