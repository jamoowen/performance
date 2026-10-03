# SQLite framework ramp findings

[Open the interactive report](https://jamoowen.github.io/performance/reports/http/sqlite-ramp/comparison.html).
[Download the comparison CSV](https://jamoowen.github.io/performance/reports/http/sqlite-ramp/comparison.csv).

## Scope

The campaign retains one 15-minute, open-arrival-rate trial per adapter. It has
five stable windows at 300, 600, 900, 1200, and 1500 RPS, with one shared-node
CPU quota, 512 MiB memory, and the current Wi-Fi route. These are descriptive
single trials: they have no confidence intervals and do not establish a universal
language or framework ranking.

The retained cohort contains 15 captures: 13 valid captures and two invalid OOM
captures. The journal has 16 entries because one earlier Phoenix generator attempt
was truncated and excluded; its full 15-minute replacement is retained but invalid
because the container was OOM-killed. The excluded attempt, its logs, and private
operational evidence are not part of the public report.

## Stable-window evidence

All p95, CPU, and working-set columns below refer to the 1500-RPS stable window,
including rows whose 1500-RPS SLO failed. An em dash is missing capture data, not
zero. The generator headroom flag is a conservative warning: true neither proves
saturation nor invalidates a capture, and false does not rule out other generator
limits.

| Adapter | Capture status | Highest passing target | Client / service / DB p95 at 1500 (ms) | Avg CPU at 1500 (mCPU) | Avg working set at 1500 (MiB) | Generator headroom flag |
| --- | --- | --- | ---: | ---: | ---: | --- |
| Go `net/http` | Valid | 1500 | 111.585 / 0.601 / 0.540 | 488.9 | 164.6 | true |
| Node Express | Valid | 1500 | 91.166 / 0.309 / 0.280 | 451.6 | 97.2 | true |
| Bun native | Valid | 1500 | 82.599 / 0.323 / 0.306 | 420.8 | 27.7 | true |
| Rust Axum | Valid | 1500 | 79.810 / 5.763 / 5.753 | 447.4 | 94.2 | false |
| Python FastAPI | Valid | 900 | 2000.183 / 81.958 / 81.878 | 285.3 | 296.7 | false |
| Elixir Plug | Invalid: OOM and failed resource coverage | 300 observed; invalid | 1996.063 / 1720.161 / 1720.147 | — | — | true |
| Go Chi | Valid | 1500 | 10.755 / 0.639 / 0.575 | 706.3 | 165.4 | true |
| Node Fastify | Valid | 1500 | 8.539 / 0.322 / 0.294 | 512.7 | 97.4 | false |
| Bun Hono | Valid | 1500 | 7.830 / 0.335 / 0.317 | 507.7 | 30.3 | false |
| Rust Actix | Valid | 1500 | 8.131 / 0.758 / 0.742 | 517.9 | 78.4 | false |
| Elixir Phoenix replacement | Invalid: OOM and failed resource coverage | 300 observed; invalid | 1995.298 / 1564.637 / 1564.474 | — | — | false |
| Go Fiber | Valid | 1500 | 9.950 / 0.647 / 0.583 | 691.2 | 172.4 | false |
| Node Nest | Valid | 1500 | 8.150 / 0.277 / 0.254 | 534.7 | 102.4 | false |
| Bun Elysia | Valid | 1500 | 8.366 / 0.339 / 0.320 | 504.5 | 30.5 | true |
| Rust Rocket | Valid | 1500 | 8.473 / 0.761 / 0.746 | 620.3 | 77.9 | false |

The completed valid Go, Node, Bun, and Rust adapters currently meet their 1500-RPS
SLOs. That is the tested ceiling, not a demonstrated maximum. Python passes
through 900 RPS and fails 1200 and 1500 RPS with timeouts, drops, throttling, and
backlog; declining CPU during collapse does not show spare useful capacity.

The Plug and Phoenix retained captures are invalid because their containers were
OOM-killed at 512 MiB and resource coverage failed. Their observed 300-RPS stages
passed, but they do not rank as valid capacity trials. Continued committed but
unacknowledged writes indicate queued work can survive client timeouts.

The full histories contain minor transport failures: Hono recorded 176, including
147 in its first stable 300-RPS window; Actix recorded 17 in the 900-RPS transition;
and Nest recorded 5 in the 600-RPS transition. Their configured stable-window SLOs
still passed. These counts do not show that every request was successful, and their
exact cause is unproven.

The exact hot function or actor responsible for either Elixir outcome is unproven
without profiles. The invalid captures remain visible in the report with their
recorded history and metadata, but are excluded from capacity rankings.

## Interpretation limits

The earlier Go `net/http` run recorded client p95 of about 111.6 ms at 1500 RPS,
while the later Chi run recorded about 10.8 ms. Their domain service timers were
about 0.60 ms and 0.64 ms. The order and network variation are strong confounders,
so this is not a router-only attribution.

These are full API-stack comparisons. SQLite driver engines, worker scheduling,
default HTTP connection policies, and server-timer boundaries differ. Server timings
measure wall-clock domain work only on successful responses; they are neither CPU
time nor a pure network subtraction. Python starts timing inside its thread-pool
worker, while Rust includes the SQLite-worker round trip.

Bun's sampled working set of about 28–31 MiB is below Go's roughly 165–172 MiB in
this configured campaign. Working set measures container residency, not managed
heap allocation, so it does not identify an allocation cause. Go's modernc pure-Go
SQLite and `encoding/json`, compared with native SQLite and optimized JSON paths in
the JavaScript runtimes, are plausible candidates for CPU and service-time
differences. They are hypotheses, not profile-proven causes isolated by this matrix.

Python here is the configured asyncio+h11 stack with threaded synchronous SQLite
behind a lock; it is not a generic FastAPI maximum. Plug and Phoenix both use
Bandit and the DB GenServer, so their similar overload points suggest investigating
shared dispatch, database, or scheduling behavior rather than blaming Phoenix
alone. CPU profiles are still needed. More cores cannot be extrapolated from this
one-CPU trial, and one serialized SQLite connection limits database parallelism.

## Provenance

The API source revision is `5204a2af2364cd5f8cf8c567f5782ec896fef3d4` and the
measurement harness revision is `1f5486aeec5c4159dd175bd1b87e6d33269f2f14`.
Each retained capture records its immutable image digest in report metadata.
Controller-only recovery commits are later operational changes and are not source
revisions for the measured API or harness.

The report contains 75 actual stable-window rows. The two OOM captures remain
invalid, and the one excluded truncated generator attempt is omitted. The saved
baseline deployment files were restored byte-for-byte through Flux; the original
Bun deployment is running and the other benchmark deployments are at their saved
zero-replica state.
