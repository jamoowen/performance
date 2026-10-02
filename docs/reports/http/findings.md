# Go and Bun: six diagnostic recordings

Run on 2026-10-02 over the local network with native k6 on the workstation. Exactly one measured run per API at 300, 600 and 900 RPS; Go ran first, then Bun, each in ascending order. Old HTTP runs and the previous report were removed.

[Open the interactive report](comparison.html). [Download the metric table](comparison.csv). Use **Compare runs** to overlay elapsed-time lines and filter by runtime, target RPS or individual recording. Choose latency, CPU, memory or completed throughput. Expand each run’s **HTTP history** for response-status timelines and **Runtime diagnostics** for its counters, top functions and profile downloads.

## Measured results

Each measurement is two minutes after a 60-second warmup. Both pods request and limit 1 CPU / 512 MiB, use the same 5,000-row seed and mixed workload, and have one SQLite connection. Every test uses 1,000 preallocated / 2,000 maximum VUs, a 1,000 ms p95 target for each operation, zero permitted dropped iterations, and a 1% HTTP/check failure threshold. CPU profiling runs for approximately the first 30 seconds; CPU/memory and HTTP timelines cover the full measurement. These are instrumented measurements; different profilers add different overhead.

| API | Target RPS | Achieved RPS | p95 ms | p99 ms | Mean CPU (m) | Peak sampled working set MiB | Throttled periods | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Go | 300 | 299.99 | 144.27 | 193.77 | 645 | 42.52 | 13.46% | Passed measured targets |
| Bun | 300 | 299.99 | 12.68 | 19.41 | 503 | 33.95 | 0.10% | Passed measured targets |
| Go | 600 | 599.77 | 342.44 | 537.63 | 874 | 46.15 | 44.37% | Passed measured targets |
| Bun | 600 | 599.98 | 96.72 | 117.58 | 492 | 32.39 | 0.00% | Passed measured targets |
| Go | 900 | 898.73 | 792.04 | 1248.49 | 956 | 50.79 | 60.56% | Passed measured targets |
| Bun | 900 | 899.77 | 108.72 | 123.89 | 603 | 48.77 | 5.20% | Passed measured targets |

All measured runs have zero HTTP failures, zero validation-check failures and zero dropped iterations. The achieved-RPS calculation includes completion time, so small differences from the scheduled target are expected. A passing p95 target does not mean every request completed within one second.

**Go’s 900 RPS warmup failed its targets:** 205 scheduled iterations were not sent, warmup overall p95 was 1,174.70 ms, and the list and quote p95 targets failed. The later measured window passed with zero drops and p95 792.04 ms. Warmup and measurement summaries are stored separately. This is evidence of startup/transient sensitivity; it is not a demonstrated sustained capacity ceiling.

“Throttled periods” is the fraction of sampled CPU scheduling periods that encountered throttling. It is not the percentage of CPU time wasted. CPU and memory values come from container counters; working set is broader than a managed heap and is not the same as node memory pressure.

## What the profiles tell us

Go reports `GOMAXPROCS=2` on the four-core node while the pod has a total one-CPU quota. It is therefore not configured as a literal one-thread process. Across the three CPU profiles, approximately 74–76% of sampled CPU time falls in the native SQLite stepping call (`_Cfunc__sqlite3_step_internal`); approximately 76–78% is attributed to `runtime.cgocall` overall. List and catalog-report handlers account for most of this work. This attribution includes native execution reached through cgo; it does not prove that crossing the cgo boundary itself is the expense.

Go’s connection-pool waits also increase strongly with load. In the 900 RPS block-profile difference, about 98.8% of sampled blocking delay is on the `database/sql.(*DB).conn` path. These waits are distinct from CPU work.

| Go target RPS | Runtime coverage seconds | Connection waits | Summed connection wait seconds | Allocation MiB/s | GC cycles | Total GC pause ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 300 | 30.07 | 11107 | 235.07 | 3.37 | 8 | 0.864 |
| 600 | 30.06 | 25817 | 1620.84 | 6.50 | 16 | 1.017 |
| 900 | 30.07 | 39873 | 4499.40 | 9.63 | 22 | 2.058 |

Summed waiting time can exceed wall time because many requests wait concurrently. GC pauses are small in these windows; the profiles point first to SQLite execution, connection queuing and CPU quota pressure rather than GC pauses. The counters alone do not exclude every possible GC cost.

Bun’s largest JSC sampling entries are the SQLite statement methods `all` and `get`. This likewise points toward database work. Its event-loop delay and managed-heap measurements are recorded below. Bun’s synchronous single-connection model does not expose Go-style pool wait counters; their absence is not proof of zero queueing.

| Bun target RPS | Runtime coverage seconds | Peak sampled JSC heap MiB | Largest window p95 event-loop delay ms |
| ---: | ---: | ---: | ---: |
| 300 | 35.16 | 13.39 | 11.706 |
| 600 | 35.15 | 6.69 | 4.461 |
| 900 | 35.22 | 10.91 | 51.053 |

The JSC function percentages and Go pprof percentages cover different sampling domains and must not be compared as equivalent CPU percentages. Neither managed-heap reading attributes native SQLite allocations. Per-operation latency and container CPU/memory remain the directly comparable measurements.

## Interpretation and next experiment

Bun has lower measured p95 latency and container CPU at each tested rate in this six-run set. Go approaches its one-CPU allowance at 900 RPS and shows increased database waiting and throttling, even though the measured targets pass. Both implementations spend much of their observed execution in database work. This does not isolate the cause to the programming language, JSON serializer, HTTP router, SQLite build or driver.

A focused `GOMAXPROCS=1` comparison under the same one-CPU quota would test whether reducing simultaneous execution helps quota throttling. A separate database-only experiment should control SQLite builds and queries before increasing machine size. A later CPU-scaling experiment would need to address the single database connection and choose comparable Bun worker/process settings. No additional runs were executed for these hypotheses.

One attempt per rate provides no repeatability estimate or confidence interval. These two-minute runs do not establish a five-minute or indefinite capacity ceiling, and a sequential Go-then-Bun order cannot rule out changes in node, network or thermal conditions. All six recordings kept the same benchmark pod identity within each run, reported no restarts, no node pressure, complete HTTP capture, and complete runtime/profile collection.

## Working hypothesis for Go’s higher latency

The leading hypothesis is that SQLite execution and coordination around the single connection consume more CPU in this Go stack, while its one-CPU quota amplifies waiting and tail latency. This is an inference from the profiles and counters, not a demonstrated causal split.

At 900 RPS, Go averaged 956 millicores and had throttling in 60.56% of sampled CPU periods; Bun averaged 603 millicores and had throttling in 5.20%. Go’s approximately 30-second diagnostic window accumulated 4,499 seconds of connection waiting, equivalent to roughly 150 concurrent connection waiters on average during that window. These are summed waits across concurrent work, not 4,499 seconds of CPU work.

Go reports `GOMAXPROCS=2` even with a one-CPU quota. This is consistent with the documented default minimum of two when CPU affinity permits it. A CPU quota controls CPU time over a period, rather than enforcing a single running thread; bursts may exhaust that time early and pause the container until the next period. This could amplify request latency, but these six runs do not isolate its contribution. See [Go runtime defaults](https://pkg.go.dev/runtime#GOMAXPROCS) and [Go’s explanation of CPU quota throttling](https://go.dev/blog/container-aware-gomaxprocs).

Bun’s SQLite API is synchronous and implemented natively, whereas the Go API uses `database/sql` pooling and a cgo-backed driver. Their coordination and result-conversion paths differ. Both implementations prepare their statements, and Bun caches prepared statements rather than query results; a cached response is not the explanation here. See [Bun’s SQLite driver and statement caching](https://bun.com/docs/runtime/sqlite). The deployed SQLite versions also differ: Go 3.53.4 versus Bun 3.53.2. Native library build differences remain a candidate, not a proven cause.

The approximately 75% Go CPU attribution to SQLite stepping includes time executing native SQLite through cgo. It must not be described as 75% spent merely crossing the Go/C boundary. Go’s observed GC pauses were small; the evidence points first to the database path and quota/queue interaction.

A focused follow-up would keep the current image, one-CPU quota, single database connection and workload constant, changing only `GOMAXPROCS` from 2 to 1. A separate database-only comparison with aligned SQLite builds would test the driver/build hypothesis. Simply adding cores would change several conditions without isolating this explanation. Neither follow-up has been run.

## Reproducibility and files

Recording source and tested image build: [`3bce230`](https://github.com/jamoowen/performance/commit/3bce230c55372b8c019f9de564fd02527c9c0c4b). [Image publishing checks passed](https://github.com/jamoowen/performance/actions/runs/36994684209). The existing namespace-scoped `ghcr-pull` Secret successfully pulled both new private images. Credentials were not changed.

Go startup: `go_version=go1.27.1 sqlite_version=3.53.4 seed_count=5000 db_path=/data/benchmark.sqlite max_open_conns=1 journal_mode=wal synchronous=1 foreign_keys=on busy_timeout=5000 cache_size=-2000 wal_autocheckpoint=1000 temp_store=MEMORY`

Bun startup: `runtime=bun-1.4.0 sqlite_version=3.53.2 seed_count=5000 db_path=/data/benchmark.sqlite max_open_conns=1`

Tested Go image: `ghcr.io/jamoowen/performance-http-go@sha256:9aa545ff07b355e103a9043c928520431709ddeb0db9ce77d058784b83ddbea3`

Tested Bun image: `ghcr.io/jamoowen/performance-http-bun@sha256:32ffb00a4a139460aaf8ebf5059b7050deb09a1b640f6594c2f02e3b118bc9af`

Flux Go activation: `2e4de6e10c0ec433ca05445d31ce1a4ba336dada`; Bun activation: `6fb72a9e9f75f1869049aee0133cf291f819d704`. Final desired state is Bun active / Go stopped, with diagnostics enabled.

Each run directory contains `result.json` and `metadata.json`, separate warmup and measurement k6 summaries/logs, container samples (`resources.jsonl` / `resources.csv`), compressed raw HTTP points (`http-metrics.json.gz`) and five-second completion-time bins (`http-history.json`). `diagnostics/` contains the runtime samples, derived summary, CPU profile and top-functions text; Go additionally includes heap, allocations, goroutine, block and mutex profiles before and after capture. The derived runtime statistics describe approximately 30–35 seconds around the CPU capture, not the complete two-minute run.

To inspect Go profiles interactively:

```sh
go tool pprof -http=127.0.0.1:0 results/http/20261002T103406.538435Z-go-steady-900rps/diagnostics/cpu.pprof
```

Bun’s `jsc-cpu.json` is native `bun:jsc.profile` output, not a Chrome `.cpuprofile`. Its top-functions view is already included in the HTML report. The report renders charts in HTML and does not rely on PNG graphs.
