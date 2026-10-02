# HTTP follow-up campaign

This report covers the completed 54-run follow-up suite; its five isolated interactive pages are available from the [follow-up report index](https://jamoowen.github.io/performance/). It is separate from the [historical diagnostics findings](findings.md), which describe the earlier Go/Bun SQLite campaign and its instrumented profiles.

## Scope and method

The suite compares these specific API implementations, container limits, workload shapes, and runtime settings. It does not establish a general language or framework ranking.

All 54 measured runs use a fresh single pod and data directory, 5,000 seeded products and a matching user-ID domain, 60 seconds of warm-up, 120 seconds of measurement, `DIAGNOSTICS=0`, 512 MiB memory, and one CPU except for the two-CPU scaling points. k6 2.3.0 runs locally over the LAN to the NodePort, with 1,000 preallocated and 2,000 maximum VUs. The node is an i3-8100T with four physical/logical cores and no dedicated-core pinning.

The six scheduler records use the earlier immutable Go API image built from `3bce230c55372b8c019f9de564fd02527c9c0c4b`. The remaining 48 runs use the new API image build from `0e2599681028cf500d198c5afbcc058856d7bfab`. Each record retains its immutable image metadata.

| Stage | Runs | Configuration |
| --- | ---: | --- |
| Scheduling | 6 | Go `GOMAXPROCS=1` and `2`, SQLite mixed workload at 600 RPS, three runs each |
| SQLite | 15 | Go, Bun, Rust at 300 RPS once, 600 RPS three times, 900 RPS once |
| Memory | 15 | The same runtime/rate/repetition plan with the seeded in-memory catalog |
| Frameworks | 6 | Chi and Elysia, memory mixed workload at 600 RPS, three runs each; compared with earlier memory stdlib reference records |
| Scaling | 12 | Go, Bun, Rust immutable-memory list workload at 600 and 3,000 RPS, one and two CPUs, once each |

Go uses `GOMAXPROCS=1` for the one-CPU SQLite, memory, and framework stages after the scheduling experiment. For scaling, Go uses `GOMAXPROCS` equal to the CPU limit. Bun uses one process per CPU; its two-process mode has separate immutable catalogs and is read-only here, so it has no shared mutable counters. Rust uses Tokio executor threads equal to the CPU setting in one process.

The SQLite binaries differ: Go reports 3.53.4, Bun 3.53.2, and Rust 3.50.2. Rust uses one dedicated SQLite thread and a queue of 256. This is an implementation/workload comparison, not an isolation of a common SQLite build.

## Definitions

- A reported latency is the median of whole-run p95 values across repetitions; a range is the minimum to maximum of those run-level p95 values. Percentiles are never pooled across runs.
- k6 [`http_req_duration`](https://grafana.com/docs/k6/latest/using-k6/metrics/reference/) covers sending, waiting, and receiving; DNS and connection timing are tracked separately. The p95 values here are therefore client-observed request time, not pure handler CPU or service-execution time.
- CPU is mean sampled cgroup millicores. Working set is the maximum sampled container working-set bytes; it is neither startup peak memory nor managed-heap size.
- CFS throttling is the fraction of scheduling periods with some throttling. It is not a wall-time percentage.
- Measurement duration and sample coverage can differ between records and must remain visible when comparing them.
- Each run requires overall and per-operation p95 at or below 1,000 ms, HTTP failure rate at or below 1%, checks at or above 99%, and zero dropped iterations.

## Suite quality

The retained record set has 55 records: 54 measured runs and one pre-traffic failed activation. Of the measured runs, 52 completed/passed and two were invalid solely because they crossed the zero-dropped-iteration threshold. Across the measured suite, k6 issued 5,615,056 HTTP requests and recorded 992 unsent iterations: 451 in Go one-CPU scaling and 541 in Rust two-CPU scaling. The only HTTP failures were 129 Rust two-CPU, 3,000-RPS requests (0.036%), below the configured 1% limit; there were no other HTTP errors or check failures.

All 54 warm-ups exited successfully. There were no resource warnings or restarts, and the 54 fresh pod UIDs were distinct and unchanged within their runs. CPU counter coverage spans 69.9–95.4% of the measurement windows.

## Confirmed scheduler result

The completed Go scheduling comparison supports a quota/scheduling contribution in this one-CPU configuration. The median p95 was 53.7 ms with `GOMAXPROCS=1` and 341.3 ms with `GOMAXPROCS=2`; median mean CPU was 896 and 960 millicores, respectively. The median fraction of CFS periods touched by throttling was 33.7% versus 74.2%. All six runs had zero HTTP errors and zero dropped iterations.

This is not general advice to set `GOMAXPROCS=1`. Go has a [container-aware default](https://go.dev/blog/container-aware-gomaxprocs) that accounts for CPU quotas; prefer that default and measure deliberate overrides for the deployment at hand. This result is controlled for this container quota, workload, and host. The historical Go profile’s roughly 75% cumulative SQLite/cgo stepping includes native SQLite execution; it does not measure cgo boundary-crossing overhead alone.

## Completed SQLite and memory stages

All 30 completed SQLite and memory measurements have status `complete`, zero HTTP errors, zero dropped iterations, zero restarts, zero collector warnings, and successful warm-ups. Their CPU counter coverage falls within the suite-wide 69.9–95.4% range, rather than representing a full 120-second trace.

At 600 RPS, the SQLite-to-memory change reduced median mean sampled CPU for all three implementations: Go 838 to 277 millicores (about 67%), Bun 493 to 265 (about 46%), and Rust 611 to 244 (about 60%). This supports a database-path CPU contribution. It does not identify cgo boundary-crossing overhead as a share of Go's database-path cost.

### 600 RPS repeated measurements

| Backend | Runtime | Runs | p95 median ms | p95 range ms |
| --- | --- | ---: | ---: | --- |
| SQLite | Go | 3 | 50.7 | 21.2–146.8 |
| SQLite | Bun | 3 | 94.1 | 15.2–98.0 |
| SQLite | Rust | 3 | 20.1 | 11.9–97.8 |
| Memory | Go | 3 | 93.6 | 83.3–93.8 |
| Memory | Bun | 3 | 86.3 | 77.3–88.0 |
| Memory | Rust | 3 | 78.6 | 77.6–82.8 |

SQLite p95 ranges overlap substantially. Memory did not consistently reduce client-observed tail latency despite the CPU reductions. Waiting, network, client, host, and I/O variation remain possible contributors, but this data does not isolate their source or support a definitive language winner.

| Backend | Runtime | Median mean CPU m | Maximum sampled working set MiB |
| --- | --- | ---: | ---: |
| SQLite | Go | 838 | 42.4 |
| SQLite | Bun | 493 | 18.5 |
| SQLite | Rust | 611 | 37.8 |
| Memory | Go | 277 | 41.6 |
| Memory | Bun | 265 | 21.2 |
| Memory | Rust | 244 | 37.6 |

The working-set column is each group’s maximum per-run sampled working set, kept separate from CPU medians and p95 summaries.

### 300 and 900 RPS exploratory single attempts

These are one attempt per runtime/backend, so they are exploratory rather than repeat-based comparisons.

| Backend | Runtime | 300 RPS p95 ms | 900 RPS p95 ms |
| --- | --- | ---: | ---: |
| SQLite | Go | 14.3 | 216.5 |
| SQLite | Bun | 12.7 | 101.5 |
| SQLite | Rust | 13.2 | 106.0 |
| Memory | Go | 81.4 | 89.6 |
| Memory | Bun | 84.3 | 119.9 |
| Memory | Rust | 6.9 | 79.5 |

## Completed framework stage

The framework records use the memory mixed workload at 600 RPS. Each router has three complete measurements with zero HTTP errors, drops, restarts, and CFS-throttled periods. The stdlib values below are earlier memory-stage reference records, so the comparisons are unpaired. The frameworks dashboard includes those six earlier stdlib reference runs, which means dashboard rows across suites exceed the 54 unique measured runs in this campaign.

| Runtime | Router | Runs | p95 median ms | p95 range ms | Median mean CPU m | Maximum sampled working set MiB |
| --- | --- | ---: | ---: | --- | ---: | ---: |
| Go | stdlib reference | 3 | 93.6 | 83.3–93.8 | 277 | 41.6 |
| Go | Chi | 3 | 83.4 | 60.4–88.9 | 302 | 41.2 |
| Bun | stdlib reference | 3 | 86.3 | 77.3–88.0 | 265 | 21.2 |
| Bun | Elysia | 3 | 77.8 | 7.6–78.0 | 289 | 28.0 |

Both alternate routers had about 9% higher median sampled CPU than their earlier stdlib references. That modest difference, the unpaired three-run samples, and sample-coverage/time variation do not support a causal router-overhead estimate or a latency winner. Elysia’s third p95 of 7.6 ms, compared with roughly 78 ms in its first two runs, is notable run variation with the same code.

The client route was observed to use Wi-Fi during this campaign. That observation does not prove every historical record used the same interface and does not establish Wi-Fi as the cause of the variation. A controlled wired repeat with server-side timing would be a useful next diagnostic, but it was not run and is outside this 54-run suite.

## Operational note

There are 54 measured attempts plus one retained pre-traffic failed activation. After 38 valid measurements, framework Go repetition two failed before warm-up or measured requests because the orchestrator saw more than one active benchmark pod (`expected exactly one active benchmark pod`). It is not an HTTP or API-load failure, and it has no request error rate or latency result.

Strict resume retained the valid framework records while completing the suite. The temporary readiness gate waited for the old terminating pod to disappear and for the intended pod’s run annotation and readiness to match. Its ignored audit is at `results/http/followups/readiness-resume-audit.json` and records the readiness-gate SHA-256 `76a37b36a39fb68313c1e4e542cf59c3f0dc03c95620a665190bc5f24387687d`. Server images, load script, and collector sampling were unchanged. The baseline is restored to the original Bun deployment: Bun 1 Ready at the old digest beginning `32ff`, Go and Rust at zero replicas, and Flux `main` at `9a729eabf7c02c861f038b8e9cb9f16c0aa9fb2a`; the new Rust manifest remains at zero replicas with a valid digest.

## Read-only scaling stage

Scaling uses a single immutable-memory list workload attempt for each runtime, CPU limit, and rate. It is exploratory: it does not establish maximum throughput or a runtime ceiling.

| Runtime | CPU | 600 RPS p95 ms | CPU m | Working set MiB | Drops | Status |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Go | 1 | 8.5 | 429 | 40.9 | 0 | pass |
| Go | 2 | 15.3 | 514 | 41.8 | 0 | pass |
| Bun | 1 | 8.7 | 410 | 27.3 | 0 | pass |
| Bun | 2 | 10.6 | 453 | 44.1 | 0 | pass |
| Rust | 1 | 9.3 | 395 | 39.5 | 0 | pass |
| Rust | 2 | 8.3 | 411 | 40.4 | 0 | pass |

| Runtime | CPU | 3,000 RPS p95 ms | CPU m | Working set MiB | Achieved RPS | Drops | HTTP failure rate | Status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Go | 1 | 22.4 | 633 | 57.7 | 2,996.1 | 451 | 0% | invalid: drops |
| Go | 2 | 39.6 | 1,299 | 44.0 | 2,999.9 | 0 | 0% | pass |
| Bun | 1 | 10.1 | 584 | 27.5 | 2,999.8 | 0 | 0% | pass |
| Bun | 2 | 9.2 | 954 | 53.6 | 2,999.8 | 0 | 0% | pass |
| Rust | 1 | 9.8 | 560 | 40.0 | 2,999.8 | 0 | 0% | pass |
| Rust | 2 | 142.4 | 854 | 55.6 | 2,995.3 | 541 | 0.036% | invalid: drops |

Rust two-CPU client logs reported `connection reset by peer`; this record does not establish handler bugs, HTTP 500 responses, or a causal network explanation. Go one-CPU reached a sampled peak of 721.5 millicores with 0.1105% CFS touched periods; Rust two-CPU reached a sampled peak of 921.8 millicores with zero CFS touched periods. Sampled means and peaks do not show sustained CPU quota saturation.

Moving from one to two CPUs produced no consistent latency improvement in this configuration. Go two-CPU used more than one core at 3,000 RPS, but that does not prove better tail latency, that Go outscales Bun or Rust, or a large-machine capacity result. The two dropped-iteration cases do not establish runtime ceilings. These 5.6 million LAN client requests remain single high-load attempts with Wi-Fi in the route, uncontrolled CPU frequency, and background host activity; CPU quotas are not dedicated physical cores. A controlled wired repeat with server-side timing is the next useful diagnostic, and has not been started.
