# SQLite adaptive capacity search

This follow-up measures where an adapter first reaches sustained overload on the
same realistic SQLite workload. It is an exploratory boundary search, not a
long-duration burn-in or a universal language ranking.

The cohort excludes Elixir/Plug and Rust/Rocket. The remaining adapters are Go
(`net/http`, Chi, Fiber), Node (Express, Nest, Fastify), Bun (native, Hono,
Elysia), Rust (Axum, Actix), Python (FastAPI), and Elixir (Phoenix).

Every adapter runs in a fresh pod with a fresh SQLite database. The immutable API
image revision is `5204a2af2364cd5f8cf8c567f5782ec896fef3d4`. Go keeps
`CGO_ENABLED=0` and modernc SQLite. Each database has 5,000 seeded products and
uses the same 50% detail, 30% list, and 20% atomic stock-update request mix.

The pod receives one CPU quota and 512 MiB. The node is shared and k6 reaches it
over the accepted Wi-Fi route. Requests use a two-second deadline. These are
recorded constraints, so results should not be generalized to a dedicated wired
machine or multi-core deployment.

Each adapter gets one 30-second, 100 RPS warm-up, then separate k6 processes for
each 90-second measured step: 15 seconds transition/settling and 75 seconds
stable. The first measured scenario epoch is the elapsed-time origin; process
startup, normalization, and step gaps remain visible rather than being presented
as continuous traffic. The initial targets are 300, 600, 900, 1200, and 1500 RPS.
Later targets are the previous target multiplied by 1.25, rounded up, up to a
20,000 RPS safety ceiling. This ceiling is a guard, not a claimed capacity.

Each step uses `ceil(target RPS × 2.1)` preallocated and maximum VUs. Starting a
new process for each step releases generator resources between tiers. A stage is
overloaded when its stable-window error/drop rate reaches 1% and that condition
also persists for four consecutive five-second stable buckets. The next step does
not begin after that sustained overload or an OOM/restart. A client p95 above
250 ms is recorded as a latency SLO miss, but does not stop the search on its
own.

Before a step, the generator requires at least `1 GiB + VUs × 0.45 MiB` free
memory and sufficient file descriptors. While it runs, it stops on the stated
guard conditions: at least 2,500 threads, less than 512 MiB available memory for
five seconds, at least 95% host CPU for five seconds, near its file-descriptor
limit, or less than 2 GiB free disk. These stops are generator limits and make a
boundary inconclusive. A headroom warning alone is caution, not proof of API
saturation.

The report records three different boundary values: highest passing RPS, highest
RPS with no overload, and first overload RPS. Only complete, valid stages may
establish a boundary. Generator or capture guards make a result inconclusive;
they are never reported as API saturation.

Pod telemetry follows the persistent parent cgroup and records Kubernetes OOM
and termination events across container restarts. Pod CPU, memory, and pod CFS
counters are restart-durable. A second CFS trace is per container identity and
is never joined across reset counters. The report preserves elapsed gaps between
processes and missing resource data rather than inventing zeros. It also records
cumulative acknowledged and unacknowledged write outcomes: an OOM can leave a
write committed after the client deadline, so an OOM is never simplified into
zero errors or a passing result.

An interrupted adapter is retried from a fresh pod and database. Resuming checks
the exact runtime/framework, image, source revision, harness revision, workload
hash, and protocol hash before accepting a prior result.
