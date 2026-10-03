"""The SQLite ramp API served through FastAPI with one serialized connection."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import fastapi
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_BODY_BYTES = 65_536
CATEGORIES = ("books", "electronics", "home", "outdoors", "clothing")
INTEGER_RE = re.compile(r"^[0-9]+$")
POSITIVE_INTEGER_RE = re.compile(r"^[0-9]+$")


class RequestError(Exception):
    """A contract error that can be returned to an API client."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def compact_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def duration_header(service_started: float, db_started: float, db_finished: float) -> str:
    service_ms = (time.perf_counter() - service_started) * 1_000
    db_ms = (db_finished - db_started) * 1_000
    return f"service;dur={service_ms:.3f}, db;dur={db_ms:.3f}"


def parse_identifier(raw: str) -> int:
    if not POSITIVE_INTEGER_RE.fullmatch(raw):
        raise RequestError(400, "id must be a positive integer")
    value = int(raw)
    if value == 0 or value > MAX_SAFE_INTEGER:
        raise RequestError(400, "id must be a positive integer")
    return value


def parse_non_negative_integer(raw: str, name: str, maximum: int) -> int:
    if not INTEGER_RE.fullmatch(raw):
        raise RequestError(400, f"{name} must be an integer")
    value = int(raw)
    if value > maximum:
        raise RequestError(400, f"{name} is out of range")
    return value


def product_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "name": row[1],
        "category": row[2],
        "priceCents": row[3],
        "stock": row[4],
        "revision": row[5],
    }


@dataclass(frozen=True)
class ServiceResponse:
    status: int
    body: bytes
    timing: str


class Store:
    """Owns the one SQLite connection and serializes every database operation."""

    def __init__(self, path: str, seed_count: int) -> None:
        if not 100 <= seed_count <= 100_000:
            raise ValueError("SEED_COUNT must be between 100 and 100000")
        self.seed_count = seed_count
        self.connection = sqlite3.connect(
            path,
            check_same_thread=False,
            cached_statements=128,
            isolation_level=None,
        )
        self.lock = threading.Lock()
        self._initialize()

    def _initialize(self) -> None:
        with self.lock:
            cursor = self.connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA cache_size=-2000")
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
            cursor.execute("PRAGMA temp_store=MEMORY")
            exists = cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'products'"
            ).fetchone()
            if exists is None:
                cursor.execute(
                    "CREATE TABLE products ("
                    "id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, "
                    "price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, "
                    "revision INTEGER NOT NULL DEFAULT 0)"
                )
                cursor.execute("BEGIN")
                try:
                    cursor.executemany(
                        "INSERT INTO products(id, name, category, price_cents, stock, revision) "
                        "VALUES (?, ?, ?, ?, ?, 0)",
                        (
                            (
                                identifier,
                                f"Product{identifier:05d}",
                                CATEGORIES[(identifier - 1) % len(CATEGORIES)],
                                500 + (identifier * 7919) % 50_000,
                                (identifier * 37) % 201,
                            )
                            for identifier in range(1, self.seed_count + 1)
                        ),
                    )
                    cursor.execute("COMMIT")
                except BaseException:
                    cursor.execute("ROLLBACK")
                    raise
            rows = cursor.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            if rows != self.seed_count:
                raise RuntimeError(
                    f"existing products row count {rows} does not match SEED_COUNT {self.seed_count}"
                )
            cursor.close()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def detail(self, identifier: int) -> tuple[Any, ...] | None:
        with self.lock:
            return self.connection.execute(
                "SELECT id, name, category, price_cents, stock, revision FROM products WHERE id = ?",
                (identifier,),
            ).fetchone()

    def list(self, offset: int, limit: int) -> list[tuple[Any, ...]]:
        with self.lock:
            return self.connection.execute(
                "SELECT id, name, category, price_cents, stock, revision FROM products "
                "ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

    def update_stock(self, identifier: int, delta: int) -> tuple[Any, ...] | None:
        with self.lock:
            return self.connection.execute(
                "UPDATE products SET stock = stock + ?, revision = revision + 1 "
                "WHERE id = ? RETURNING id, stock, revision",
                (delta, identifier),
            ).fetchone()

    def integrity(self) -> tuple[int, int, int]:
        with self.lock:
            return self.connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(stock), 0), COALESCE(SUM(revision), 0) FROM products"
            ).fetchone()

    def pragmas(self) -> dict[str, Any]:
        with self.lock:
            return {
                "journal_mode": self.connection.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": self.connection.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": self.connection.execute("PRAGMA foreign_keys").fetchone()[0],
                "busy_timeout": self.connection.execute("PRAGMA busy_timeout").fetchone()[0],
                "cache_size": self.connection.execute("PRAGMA cache_size").fetchone()[0],
                "wal_autocheckpoint": self.connection.execute(
                    "PRAGMA wal_autocheckpoint"
                ).fetchone()[0],
                "temp_store": self.connection.execute("PRAGMA temp_store").fetchone()[0],
            }

    def compile_options(self) -> list[str]:
        with self.lock:
            return [row[0] for row in self.connection.execute("PRAGMA compile_options").fetchall()]


class Service:
    def __init__(self, store: Store) -> None:
        self.store = store

    def detail(self, identifier: int) -> ServiceResponse:
        service_started = time.perf_counter()
        db_started = time.perf_counter()
        row = self.store.detail(identifier)
        db_finished = time.perf_counter()
        if row is None:
            raise RequestError(404, "product not found")
        return ServiceResponse(
            200,
            compact_json(product_from_row(row)),
            duration_header(service_started, db_started, db_finished),
        )

    def list(self, offset: int, limit: int) -> ServiceResponse:
        service_started = time.perf_counter()
        db_started = time.perf_counter()
        rows = self.store.list(offset, limit)
        db_finished = time.perf_counter()
        value = {
            "products": [product_from_row(row) for row in rows],
            "total": self.store.seed_count,
            "offset": offset,
            "limit": limit,
        }
        return ServiceResponse(
            200, compact_json(value), duration_header(service_started, db_started, db_finished)
        )

    def update_stock(self, identifier: int, delta: int) -> ServiceResponse:
        service_started = time.perf_counter()
        db_started = time.perf_counter()
        row = self.store.update_stock(identifier, delta)
        db_finished = time.perf_counter()
        if row is None:
            raise RequestError(404, "product not found")
        return ServiceResponse(
            200,
            compact_json({"id": row[0], "stock": row[1], "revision": row[2]}),
            duration_header(service_started, db_started, db_finished),
        )


def error_response(error: RequestError) -> Response:
    return Response(
        compact_json({"error": error.message}),
        status_code=error.status,
        media_type="application/json",
    )


def timed_response(result: ServiceResponse) -> Response:
    return Response(
        result.body,
        status_code=result.status,
        media_type="application/json",
        headers={"Server-Timing": result.timing},
    )


async def read_limited_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise RequestError(413, "request body is too large")
    return bytes(body)


def parse_delta(content_type: str | None, body: bytes) -> int:
    if content_type is None or content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise RequestError(415, "content type must be application/json")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestError(400, "body must be valid JSON") from error
    if not isinstance(value, dict) or set(value) != {"delta"}:
        raise RequestError(400, "body must contain only integer delta")
    delta = value["delta"]
    if isinstance(delta, bool) or not isinstance(delta, int) or not -100 <= delta <= 100:
        raise RequestError(400, "delta must be an integer from -100 to 100")
    return delta


def parse_pagination(request: Request, seed_count: int) -> tuple[int, int]:
    items = list(request.query_params.multi_items())
    names = [name for name, _ in items]
    if any(name not in {"offset", "limit"} for name in names) or len(names) != len(set(names)):
        raise RequestError(400, "query must contain only one offset and one limit")
    values = dict(items)
    offset = parse_non_negative_integer(values.get("offset", "0"), "offset", seed_count)
    limit = parse_non_negative_integer(values.get("limit", "20"), "limit", 100)
    if limit == 0:
        raise RequestError(400, "limit is out of range")
    return offset, limit


def create_app() -> FastAPI:
    framework = os.environ.get("FRAMEWORK", "fastapi")
    if framework != "fastapi":
        raise RuntimeError("FRAMEWORK must be fastapi for the Python ramp image")
    path = os.environ.get("SQLITE_PATH", "/data/benchmark.sqlite")
    seed_count = int(os.environ.get("SEED_COUNT", "5000"))
    store = Store(path, seed_count)
    service = Service(store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        store.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> Response:
        return Response(compact_json({"status": "ok"}), media_type="application/json")

    @app.get("/benchmark/info")
    async def info() -> Response:
        return Response(
            compact_json(
                {
                    "experiment": "sqlite-ramp-v2",
                    "runtime": "python",
                    "framework": framework,
                    "runtimeVersion": sys.version.split()[0],
                    "frameworkVersion": fastapi.__version__,
                    "driver": "sqlite3",
                    "driverVersion": sys.version.split()[0],
                    "sqliteVersion": sqlite3.sqlite_version,
                    "compileOptions": store.compile_options(),
                    "seedCount": store.seed_count,
                    "workers": 1,
                    "httpProtocol": "h11",
                    "eventLoop": "asyncio",
                    "pragmas": store.pragmas(),
                }
            ),
            media_type="application/json",
        )

    @app.get("/benchmark/integrity")
    async def integrity() -> Response:
        rows, total_stock, total_revisions = await run_in_threadpool(store.integrity)
        return Response(
            compact_json(
                {"rows": rows, "totalStock": total_stock, "totalRevisions": total_revisions}
            ),
            media_type="application/json",
        )

    @app.get("/products")
    async def list_products(request: Request) -> Response:
        try:
            offset, limit = parse_pagination(request, store.seed_count)
            return timed_response(await run_in_threadpool(service.list, offset, limit))
        except RequestError as error:
            return error_response(error)

    @app.get("/products/{identifier}")
    async def product(identifier: str) -> Response:
        try:
            return timed_response(
                await run_in_threadpool(service.detail, parse_identifier(identifier))
            )
        except RequestError as error:
            return error_response(error)

    @app.post("/products/{identifier}/stock")
    async def stock(identifier: str, request: Request) -> Response:
        try:
            parsed_identifier = parse_identifier(identifier)
            body = await read_limited_body(request)
            delta = parse_delta(request.headers.get("content-type"), body)
            return timed_response(
                await run_in_threadpool(service.update_stock, parsed_identifier, delta)
            )
        except RequestError as error:
            return error_response(error)

    return app
