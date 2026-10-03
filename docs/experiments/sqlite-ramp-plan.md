# SQLite framework ramp experiment (v2)

Approved scope: implement and publish the framework matrix requested on 2026-10-03,
deploy sequentially through the OptiPlex GitOps repository, run one longer ramp per
implementation, and publish an interactive report. The user explicitly accepts
the current network, including Wi-Fi, provided that limitation is labelled.
Preserve all earlier implementations, measurements and reports.

## Implementations and ownership

New code lives under `benchmarks/http/ramp/`. Six separate images select real
framework adapters using `FRAMEWORK`: Go (`nethttp`, `chi`, `fiber`), Node
(`express`, `nest`, `fastify`), Bun (`native`, `hono`, `elysia`), Rust (`axum`,
`actix`, `rocket`), Python (`fastapi`), Elixir (`phoenix`, `plug`). Nest uses its
Express adapter; both Elixir adapters use Bandit. Identify those relationships
in metadata and reports. Plug is a router baseline, rather than a second full
application framework. Do not use compatibility wrappers that bypass the
framework's actual routing lifecycle.

Implementation workers own their assigned runtime directories only. Primary
owns the protocol, experimental design, integration and final review. Shared
harness, CI and report work will be assigned separately after runtime work.

## Common API contract

Listen on `0.0.0.0:8080`; `PORT` may override it. `FRAMEWORK` must name a supported
adapter or startup fails. `SQLITE_PATH` defaults to `/data/benchmark.sqlite` and
`SEED_COUNT` defaults to 5000 (allow 100..100000 for contract tests). No HTTP
access logging, compression, authentication, profiling or response caching in
measured runs. Record runtime, framework, driver and SQLite versions.

One file-backed SQLite connection per process, WAL journal, NORMAL synchronous,
foreign_keys=ON, busy_timeout=5000, cache_size=-2000, wal_autocheckpoint=1000,
temp_store=MEMORY. Use prepared/cached SQL statements and safely serialize use
of a shared connection. Each deployment receives a fresh emptyDir database.
Schema and seed are fixed:

```sql
CREATE TABLE products (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  stock INTEGER NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0
);
```

Categories, in order: books, electronics, home, outdoors, clothing. For id 1..N:
name=`Product` + five-digit zero-padded id; category=categories[(id-1)%5];
price_cents=500+(id*7919)%50000; stock=(id*37)%201; revision=0. Seed in one
transaction, never re-seed an existing table or silently change its row count.

Product JSON has exactly `id`, `name`, `category`, `priceCents`, `stock`,
`revision` (JSON key order is irrelevant). All integer fields remain integers.

* GET `/products/:id`: primary-key lookup, return product, 404 if missing.
  Id must be a positive integer within JavaScript's safe integer range; invalid
  syntax/range is 400.
* GET `/products?offset=0&limit=20`: return `{products,total,offset,limit}`.
  Defaults offset=0, limit=20; offset integer 0..N, limit integer 1..100;
  unknown/duplicate query fields are 400. Query is ORDER BY id LIMIT ? OFFSET ?.
  `total` is the verified immutable seeded row count, not an extra COUNT query.
* POST `/products/:id/stock`: application/json (optional charset parameters),
  body exactly `{delta: integer}` with -100..100 inclusive. Reject booleans,
  floats, missing or unknown fields with 400; unsupported content type 415;
  maximum body size 65536 bytes, larger is 413. Atomically execute:
  `UPDATE products SET stock=stock+?, revision=revision+1 WHERE id=? RETURNING id,stock,revision`.
  Return `{id,stock,revision}` with 200, missing id 404. No growing event table,
  timestamp generation, application read/modify/write transaction or extra SELECT.
* GET `/healthz`: 200 JSON `{status:"ok"}` once database initialization completes.
* GET `/benchmark/info`: JSON metadata: `experiment:"sqlite-ramp-v2"`,
  `runtime`, `framework`, `runtimeVersion`, `frameworkVersion`, `driver`,
  `driverVersion`, `sqliteVersion`, `seedCount`, `workers:1`, and effective
  `pragmas` object read from the live connection using these SQLite names:
  `{journal_mode:"wal",synchronous:1,foreign_keys:1,busy_timeout:5000,
  cache_size:-2000,wal_autocheckpoint:1000,temp_store:2}`. Additional build/
  scheduling information is permitted; include SQLite compile options where available.
* GET `/benchmark/integrity`: `{rows,totalStock,totalRevisions}` from aggregate
  SQL outside measured traffic. Enables write-integrity checks after the run.

Errors return JSON with a string `error`. Router-native behavior for unsupported
methods and unrelated paths is allowed and excluded from benchmark traffic.
Body-size enforcement must actually inspect bytes, not just Content-Length.
Prepared statements used for measured endpoints must be reused across requests.

Successful measured responses include
`Server-Timing: service;dur=<milliseconds>, db;dur=<milliseconds>`.
Service timer starts at the domain operation after request routing, body parsing
and validation, and includes SQL and explicit JSON serialization to bytes/text.
DB timer starts immediately before requesting serialized database access, thus
includes connection lock/worker queue wait and SQLite execution. It ends when
rows are materialized. These timers exclude socket transfer, event-loop wait
before dispatch, framework routing/body parsing and kernel scheduling before
domain entry. Never present client latency minus this timer as pure network time.
Serialize successful JSON once in shared domain code; adapters send those bytes
with application/json and the timing header. Metadata/integrity need no timing.

## Runtime choices

Go uses database/sql with modernc.org/sqlite v1.60.1, CGO_ENABLED=0, Chi v5.3.2,
Fiber v3.5.0. Pin Go 1.27.1 and set GOMAXPROCS=1 in measured manifests.
Fiber is built on fasthttp and must be identified as such. One DB connection.

Node 24.21.0 uses built-in node:sqlite DatabaseSync (release-candidate API, record
that caveat), Express 5.2.1, Fastify 5.12.5, Nest packages 12.1.2. One process,
NODE_ENV=production. Bun 1.4.2 uses bun:sqlite, Hono 4.13.12, Elysia 1.4.30.
Single process for every JS implementation. Pin exact dependencies and lockfiles.

Rust 1.93.0 uses rusqlite 0.40.2 with bundled and cache features, Axum 0.8.9,
Actix Web 4.15.0, Rocket 0.5.1, Tokio 1.53.2. One application worker/executor
plus one dedicated SQLite worker, cached statements inside the owning thread.
Do not perform blocking SQLite work on an async executor. Record native threads;
one process/one CPU budget does not imply exactly one operating-system thread.

Python 3.14.8 uses FastAPI 0.142.2, Uvicorn 0.54.0, Pydantic 2.13.5, stdlib sqlite3.
One Uvicorn worker, synchronous SQLite work offloaded appropriately, connection
statement cache and explicit lock protecting use, access logging disabled.

Elixir 1.20.4 / OTP 28.5.0.7 uses compatible build and runtime images, Phoenix
1.8.15, Plug 1.20.3, Bandit 1.12.5, Exqlite 0.42.0, Jason 1.4.5. One DB owning
GenServer with retained prepared statement handles. Use production release;
ERL_FLAGS='+S 1:1 +SDcpu 1 +SDio 1' for the one-CPU experiment. Phoenix is a JSON
API without browser/session/static/LiveView plugs. Exqlite is a native NIF;
record its build/SQLite differences. Handle statement reset/release correctly.
Local amd64 emulation fails during Erlang terminal initialization on this Mac,
including clean public base images; native arm64 startup succeeds. Run local
Elixir release checks natively and require native Linux amd64 CI container
contract checks before publishing/deploying. This is not an application result.

## Verification before publishing

Each runtime gets formatter/linter configuration and focused native checks.
One shared external contract suite will verify all 15 adapters: deterministic
seed JSON, pagination and invalid bounds, strict JSON validation and byte body
limit, missing product, repeated and concurrent atomic stock updates (no lost
writes), metadata/PRAGMAs, response timings, unchanged cardinality, and startup
selection failure. Format, lint and compile must pass. Container build and
smoke tests validate linux/amd64 listening/probes and effective worker settings.
Do not duplicate the old complex workload or add unrelated framework defaults.

## Run design

One active benchmark pod at a time, one process, CPU requests/limits 1 and memory
512Mi. CPU quota is not a dedicated core. Restart between variants; fresh DB;
verify immutable linux/amd64 image digest, current Flux revision, attempt annotation,
pod UID and container readiness. Preserve/restore prior benchmark desired state.

Separate 60-second warmup at 100 RPS. Measured 15-minute open-arrival-rate run:
five three-minute stages targeting 300, 600, 900, 1200, 1500 RPS. First stage is
flat; each later stage transitions over 20 seconds and holds for 160 seconds.
Exclude transitions and first 20 seconds of the initial stage from capacity
assessment; show them on time charts. Drain is separately labelled. Preallocate
3200 VUs, maxVUs=3200, HTTP timeout=2s, so the maximum 1500 RPS times timeout
fits the VU budget. Preflight generator memory/CPU and retain continuous samples.
If generator limits dominate, flag the run as inconclusive rather than silently
claiming a server limit. Invalid HTTP and unscheduled requests are distinct.

Mix: 50% detail, 30% list, 20% stock updates (delta=1). Independently hash the
iteration to choose the route, product id and pagination offset; fixed seed
across implementations, avoid the correlated modulo behavior of v1. Checks
verify response status and minimal expected shape/invariants for every request.
Capture counts, goodput, client latency, service/DB duration, statuses, dropped
iterations and checks over time and by stable load window. Success criterion
per stable window: schedule delivery >=99.9%, successful goodput >=99% of target,
HTTP/check failure <=1%, client p95 <=250ms. These are explicit experiment SLOs,
not universal production requirements; one trial provides descriptive evidence.
Continue the full ramp through failed criteria, retain every stage.

Fresh CPU/cgroup memory/throttling/pressure samples each second using read-only
SSH on the exact container cgroup, with monotonic/source timestamps. Do not
mislabel repeatedly cached cAdvisor readings as fresh observations. Bracket
measurement with counters; record sample coverage, memory.events and restarts.
Capture node contention and local generator CPU/RSS/interface continuously.
All SQLite write acknowledgments must reconcile with final totalRevisions
(warmup included; connection resets after committed writes may create an excess,
never silently discard an inconsistency). Stop on pod replacement or identity drift.

## Artifacts and deployment

Publish six images named performance-http-ramp-{runtime}, pin digest after CI.
Add dedicated CI paths without changing historical image identities. Cluster
app apps/performance-http-ramp uses existing namespace my-api and ghcr-pull,
one deployment http-ramp and NodePort 30083 (verify availability first), no
Ingress/tunnel. All desired-state changes through Git and Flux; do not apply
or scale workloads manually. Review/render root kustomization before push.

Add separate interactive SQLite ramp dashboard, README link and documented
results. Toggle runtime/framework/load windows; plot target RPS, completed
goodput/drops/statuses, client p95 and service/DB durations, CPU/memory/throttling.
Tables describe stable windows, thresholds, failures, build/driver versions,
sampling coverage and the accepted Wi-Fi/shared-node/single-trial limitations.
Do not rank using a pooled fifteen-minute p95. Preserve old reports. Publish
through existing GitHub Pages pipeline and verify live interactive behavior.
