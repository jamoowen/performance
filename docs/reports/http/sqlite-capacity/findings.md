# SQLite adaptive capacity search findings

Open the [interactive report](comparison.html) or download its [stable-window CSV](comparison.csv). This is one sequential exploratory request-path and capped-concurrency search per adapter, not a production capacity certification or a universal language ranking.

The final cohort uses the same immutable API source `5204a2af2364cd5f8cf8c567f5782ec896fef3d4`, including pure-Go modernc SQLite for Go. The recorder and controller harness revision is `8e209f517046872c58049c4a69530dff2def294a`; the workload and protocol hashes are `e818993e19fe98b39a6dcd2c45ea4a5b375e14c68e1db7e95783a00cc17fb873` and `dd6c65ad9b11fe8420d6bbaa4eaf6dc6998fe3876b64378209b42810ee0f1c65`. Full runtime, framework, driver, SQLite, worker, PRAGMA, and image-digest metadata is available in the report's expandable records.

All 13 captures are valid. Pod-resource coverage is 100%; generator sampling coverage ranges from 96.65% to 96.76%. No final-cohort run triggered a generator guard or headroom warning, and no OOM or restart occurred. Plug and Rocket are excluded from this new search only; their earlier recordings remain in the historical reports.

## Observed boundary brackets

“Strict SLO pass” is the highest observed step that passed the complete SLO. “Below sustained overload” is the highest completed step before the four-bucket, 1% error/drop overload rule. “First overload” is the next observed overload step. These are coarse target brackets from one Wi-Fi-routed trial: the initial preset tiers are 300, 600, 900, 1200, and 1500 RPS, with 25% increments only above 1500 RPS. They are not monotonic guarantees or precise framework speedups. Actix, Fiber, and Elysia have lower-rate delivery blips followed by higher passing steps.

| Family | Adapter | Highest strict SLO pass | Highest below sustained overload | First overload |
| --- | --- | ---: | ---: | ---: |
| Go | net/http | 5,724 | 7,155 | 8,944 |
| Go | Chi | 5,724 | 5,724 | 7,155 |
| Go | Fiber | 7,155 | 7,155 | 8,944 |
| Node | Express | 5,724 | 5,724 | 7,155 |
| Node | Nest | 4,579 | 4,579 | 5,724 |
| Node | Fastify | 5,724 | 5,724 | 7,155 |
| Bun | native | 8,944 | 8,944 | 11,180 |
| Bun | Hono | 7,155 | 8,944 | 11,180 |
| Bun | Elysia | 8,944 | 8,944 | 11,180 |
| Rust | Axum | 11,180 | 11,180 | 13,975 |
| Rust | Actix | 8,944 | 11,180 | 13,975 |
| Python | FastAPI | 900 | 900 | 1,200 |
| Elixir | Phoenix | 300 | 300 | 600 |

Every final overload window except Phoenix had zero HTTP and validation errors: unissued dropped arrivals caused the overload decision. HTTP errors use completed requests as their denominator; drops use scheduled arrivals. The report plots them separately for that reason.

FastAPI at 1,200 RPS delivered about 1,055 good RPS, dropped 12.1% of scheduled arrivals, had client p95 1,173.5 ms, and used about 1,000 millicores. Its successful responses still passed validation. Phoenix at 600 RPS delivered 7.48 good RPS, had 37,879 HTTP failures from 38,440 completed requests (about 98.54%), and dropped 6,560 of 45,000 scheduled arrivals (about 14.58%). Its client p95 was about 1,997.4 ms, CPU about 992 millicores, and CFS throttling 96.7% of periods. Phoenix already used about 985.7 millicores at 300 RPS. This is a CPU/queueing boundary for this configuration, not a Phoenix-in-general maximum. It did not OOM: observed pod memory peak was 268,804,096 bytes (about 256.4 MiB), below the 512 MiB limit.

## Comparisons that are useful, with their limits

At the shared 7,155 RPS target, Go net/http delivered 7,106.35 good RPS and Chi 7,012.55, about a 1.3% difference, while their first overload tiers were 8,944 and 7,155. Fiber delivered 7,154.97 good RPS with about 845.7 millicores, versus net/http's 934.2 and Chi's 951.6. This records the offered-rate result; it does not isolate router cost or establish causation.

At 7,155 RPS, Fastify delivered 6,834 good RPS versus Express's 6,337.99, with client p95 175.8 ms versus 260.1 ms and CPU around 973–978 millicores for both. Nest uses the Express adapter and adds framework machinery. The result is consistent with overhead, but this capture does not prove the mechanism.

Rust reached a highest no-overload tier of 11,180 RPS while Bun reached 8,944, but that does not mean Rust used less CPU at every rate. At 4,579 RPS, Bun native, Hono, and Elysia used about 563, 589, and 539 millicores; Axum and Actix used about 818 and 766. Throughput headroom, latency, and CPU efficiency are separate observations. Bun and Rust used about 907–938 millicores in their overload windows rather than a full 1,000; the 1,024-VU cap, Wi-Fi route, and request path mean this does not prove a universal server CPU ceiling.

This is a whole-stack comparison. Go's modernc driver differs from the native SQLite bindings and versions in other adapters. Server-Timing is wall time on successful responses and includes instrumented waiting boundaries. It is not a CPU profile, and client and server p95 values cannot be subtracted to derive network time. These captures contain no causal CPU profile.

## Phoenix integrity and telemetry

Phoenix retains complete telemetry despite its failed workload: the collector uses the persistent parent pod cgroup, durable JSONL, and separate container segments to preserve observations across an OOM or restart. Unit and reproduction coverage exercised disappearing-container and restart handling. This final campaign did not OOM, so it does not demonstrate that recovery against a real OOM.

Final integrity is verified. Across Phoenix's warm-up and two measured steps, 7,433 writes were acknowledged, 14,987 revisions committed, and 7,554 writes committed without acknowledgement; that equals the 7,554 failed Phoenix POSTs. The Phoenix source uses a six-second `GenServer.call` while the client deadline is two seconds, so queued work can continue after the client times out. The other 12 adapters have exact acknowledged-write and committed-revision counts, with no unacknowledged writes.

## Time and scope

The 151 measured 90-second steps used 3 hours 46 minutes 30 seconds of traffic; 13 30-second warmups added 6 minutes 30 seconds, for 3 hours 53 minutes total traffic. The first-to-final collector interval was 17,578.406785 seconds, about 4 hours 53 minutes, including deployment, restore, and capture processing between adapters. The earlier 15-adapter ramp used roughly four hours of traffic before builds, deployment, and QA. This session also fixed an over-aggressive initial VU allocation; two earlier preflight cohorts are excluded. API images were unchanged, and only the final fixed-1,024-VU cohort appears here.

Results apply to one CPU quota, 512 MiB, a shared i3 node, an accepted local Wi-Fi generator route, one worker configuration, one trial, 90-second tiers, a two-second deadline, and a fixed 1,024 preallocated/max VU pool. The initial preset tiers and 25% increments above 1500 RPS provide an observed below-threshold/first-overload bracket for this route and protocol. They do not establish an exact production limit, multi-core scaling, or a universal ranking.
