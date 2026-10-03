# Harness implementation handoff

This supplements `sqlite-ramp-plan.md`; read both. The API/runtime workers own
their runtime directories. The harness worker owns `benchmarks/http/ramp/measure/`,
`benchmarks/http/ramp/tests/`, `benchmarks/http/ramp/load.js`, root Makefile,
`.gitignore`, dedicated CI workflow, and ramp README. Report UI may be assigned
separately under `benchmarks/http/ramp/report/`. Do not change old experiments.
Python harness requirements: pinned PyYAML 6.0.3 and psutil 7.2.2; use stdlib for
everything else except report Plotly 7.1.0. Root Ruff/Biome/golangci conventions.

## Shared contract runner

External HTTP tests take --base-url and expected --runtime/--framework and a
known seed count. They run against all genuine adapters, using a fresh database.
Use stdlib unittest, urllib and ThreadPoolExecutor. Test deterministic seeding,
required exact successful response fields, pagination/defaults/bounds/duplicate
fields, invalid path ids and 404, strict stock JSON (boolean/float/unknown/missing),
unsupported media and chunked/declared bodies over 65536 bytes, Server-Timing
parseable nonnegative service/db and db<=service with rounding tolerance,
PRAGMAs, worker metadata, and 40 concurrent delta=1 updates with exact revision
and stock totals. Integrity measured before/after test writes. Timing values
and JSON property order are not equality requirements. Test startup selection
failure in native launch/orchestration checks. Provide a matrix runner which
builds/starts each runtime adapter locally or using six built containers, uses
fresh temporary directories/volumes, cleanly terminates services, waits health,
and invokes the shared suite. Allow runtime/framework subsets for development.

## k6 schedule and data

Implement configurable schedule JSON (production default five 180s levels
300/600/900/1200/1500, 20s transition, first level 20s settling); short schedules
are allowed for harness tests, not silently mixed with production results.
Warmup separate constant-arrival-rate 100 RPS/60s. Explicit scenario startTime
and absolute clock origin are essential; emit a scenario_origin Gauge with
`exec.scenario.startTime/1000` on each VU's first iteration. This gives recorder
an exact epoch origin without guessing from process launch or setup time.
Metric tags include operation/detail|list|stock, load level, stable|transition,
and route template name. Bound system tags to prevent dynamic URL cardinality.
Determine tags from request START relative to scenario origin. All requests
timeout 2s. Preallocated=maxVUs=3200 before measurement; no dynamic VU creation.
Warmup may use fewer VUs, but measurement allocation/source hash is recorded.

Use deterministic independent 32-bit hashes of scenario iteration+fixed salt
for route bucket (50/30/20), product id and list offset. No Math.random or
correlated id/operation modulo. Minimal checks cover status, shape/value and
timing presence for every request; treat failure categories distinctly.
One `request_outcomes` Counter per request with status and success/http_error/
validation_error outcome; custom `service_duration` and `db_duration` Trends
for successful responses, `stock_successes` Counter for acknowledged valid
updates. Retain built-in durations/failures/drops/checks and summary JSON.
Use k6 compressed JSON output, restrict tags and monitor disk. Keep full capture
ignored, normalize without loading every metric/request into RAM at once.

Build 5s completion-time histories (client med/p95/p99, service/db p95, counts,
goodput, statuses, validation/HTTP errors, dropped iterations) and stable-window
cohorts based on request START tags. Dropped iterations have no request-start
tag: classify using their point timestamp minus exact scenario origin. Stable
window expected arrivals = target * stable seconds (160s each production level).
Count completions from requests started in that window, including up to 2s drain;
label cohort goodput accordingly, rather than implying completion-time throughput.
Use exact quantiles over per-window numeric arrays or arrays on disk; do not
derive p95 by averaging 5s p95s. Reconcile raw point counts/totals with k6 summary,
including stock_successes and drops. Missing/truncated capture is invalid.
Time histories include transitions and drain; windows exclude them. No whole
ramp threshold abort, as high-load failures are expected scientific outcomes.

## Fresh cgroup collector

The primary validated this without sudo. Get selected pod metadata through
kubectl JSON, extract exact container ID, then resolve only
`/sys/fs/cgroup/kubepods.slice/**/cri-containerd-<container_id>.scope` with glob.
Require exactly one match; record pod UID/container ID, reject replacements.
Read cpu.stat, cpu.max, memory.current, memory.stat, memory.events, cpu.pressure,
memory.pressure each second. Capture realtime_ns and monotonic_ns; derive CPU
millicores and throttling deltas using monotonic interval, never cached metrics.
Working set = max(0,memory.current-inactive_file), label it as that estimate;
retain memory.current separately. Read /proc/stat for node CPU deltas,
/proc/loadavg, /proc/meminfo and /proc/pressure/cpu; optional cpu frequency read.
No secrets, no root/crictl requirement, no additional deployment or privileged pod.

Remote Python program streamed through SSH stdin. Safe validated host/namespace/
pod arguments and shlex quoting; never interpolate untrusted shell strings.
Sample no slower than 1s. Check identity/readiness periodically (e.g every 5s)
and at end, record source timestamp and coverage. Missing cgroup, replacement,
counter regression, OOM, restart or collector failure invalidates measurement.
Use bracket samples/counter deltas at stable-window bounds where possible;
resource averages use observed elapsed durations, not arithmetic average of
polls. Record coverage/gaps; do not interpolate large gaps silently.
Estimate server/client clock offset using 3 short bracketed remote timestamps,
keep smallest RTT and its uncertainty, record that exact cross-host alignment
is approximate. CPU counters use remote monotonic timestamps regardless.

Continuous local generator samples use psutil Process(k6.pid).cpu_times(),
memory_info().rss, host cpu_percent(percpu=True), virtual_memory, swap counters,
and network stats on actual selected route interface (Mac `route -n get`, Linux
`ip route get`, captured metadata only). Derive process CPU from deltas; retain
one-core percentages/millicores and host/core utilization. Capture before/after,
sample every second. Flag sustained >90% host CPU or <1GiB available memory/
swap growth and incomplete scheduling as possible generator bottlenecks;
flag explicit VU cap rather than claiming proven server capacity. Record actual
k6 version, Mac/node CPU/memory and interface/network accepted limitation.

## Recorder and validity

Record.py CLI accepts base URL, SSH host, expected runtime/framework/image/
attempt/Flux revision, output root, schedule, k6 path; provide local-only mode
for fast harness checks clearly lacking cluster telemetry. Preflight disk>=20GiB
for production campaign, and sufficient local memory to allocate VUs. Fetch
health/info/integrity, verify fresh 5000 rows and zero revisions before warmup.
Start collectors, run warmup, retain its summary, run measured k6, drain, collect
final integrity and exact pod metadata, stop/join collectors even on exception.
Write manifest/metadata/result/history/summary and logs in a unique attempt dir.
Any setup/collector/protocol/capture/identity failure is `invalid`; a good
measurement missing an SLO is still `valid` with failed windows. Never convert
failed windows into an overall 0%-error pass. Preserve partial artifacts.

Window SLO: scheduling >=99.9%, cohort successful goodput >=99% target,
HTTP/check failures <=1%, client p95<=250ms. Separate unsent drops from HTTP
failures and failed response validation. Include counts/reasons in every window.
Integrity: rows unchanged; totalStock-initialStock == totalRevisions; revisions
>= warmup+measurement acknowledged update count. An excess no greater than
failed stock requests can be committed-but-unacknowledged work and is labelled;
less acknowledged or impossible excess invalidates. Warmup failure prevents
measurement rather than silently seeding a compromised comparison.

## Campaign and GitOps

15 variants, interleaved order: go/nethttp, node/express, bun/native, rust/axum,
python/fastapi, elixir/plug, go/chi, node/fastify, bun/hono, rust/actix,
elixir/phoenix, go/fiber, node/nest, bun/elysia, rust/rocket. Retain exact order;
one trial each means time/order effects are not statistically controlled.
Default CLI dry-run with finite matrix/duration; execute needs explicit digest
map for six runtimes. New app template parameterized actual digest, deployment
http-ramp/NodePort30083. Initial add is reviewable before push. Retain new app
replicas=0 at rest. Existing old http-go/bun/rust desired files are snapshotted;
set them replicas0 during campaign and restore exact bytes in finally, set ramp0.
Do not delete user namespaces/data/secrets or touch unrelated apps. New database
emptyDir port8080, `/healthz` probes and startupProbe, Recreate strategy avoids
overlap, CPU requests=limits1 memory requests=limits512Mi, FRAMEWORK, SEED_COUNT,
SQLITE_PATH, GOMAXPROCS=1, NODE_ENV=production, ERL_FLAGS as plan, ghcr-pull.

Work in isolated ephemeral cluster clone; only narrow benchmark app changes.
Render clusters/optiplex, git diff --check, allowlisted path check before commits.
Push normal Git; if main advances rebase once, abort conflicts, never force.
Flux reconcile requests allowed; no kubectl apply/scale. Wait for expected
applied revision (or verified descendant), deployment observedGeneration, exact
image/env/attempt annotation and exactly one ready pod; count terminating old
benchmark pods as active until gone. Final restoration must also wait Flux
revision and baseline readiness; if interrupted persist recovery instructions.
Journal every attempt immediately; resume never silently reruns successful
existing entries or pools different hashes/images. Stop on infrastructure
failures; SLO failures continue remaining variants.

## Checks and CI

Unit tests focus on nontrivial schedule boundaries/integrals, independent hash
domain coverage, malformed/truncated capture reconciliation, exact p95 versus
bucket medians, CPU-counter/working-set deltas and sample gaps, stage/transition
and drain classification, measurement validity vs missed SLO, write accounting,
strict Flux readiness including terminating pod/old revision, narrow path guard
and restoration after exceptions. Use fixtures/fakes; no synthetic passing
metrics in real reports. Short local end-to-end k6 run checks raw->normalized
totals and real timing metadata before any production campaign.

Dedicated image workflow uses GitHub Actions built-in GITHUB_TOKEN package
write permission, six-image matrix, linux/amd64 SHA tags/OCI provenance; capture
linux/amd64 child digests. Add ramp-native CI checks plus shared container matrix
contracts, preserving old workflow behavior. Make targets document checks,
builds, dry-run/run campaign and report; dependencies pinned and caches ignored.

## Report

Separate docs/reports/http/sqlite-ramp/comparison.html plus CSV and sanitized
data.json. Use inline self-contained Plotly (existing project convention), no
PNG or external CDN. Toggle runtimes, individual frameworks and target levels;
compare per-window metrics versus load and individual histories versus elapsed
time; show target RPS, success/drops/errors, latency/service/DB, CPU/memory/CFS.
Details grouped by implementation, stage rows clearly show why criteria failed;
measurement validity is separate. All failures remain visible by default.
Comparison includes immutable image/source/load hash, runtime/framework/SQLite
versions, effective worker/driver config, sample coverage and caveats. Omit SSH
hosts, private IPs, filesystem paths, pod names and arbitrary environment values
from public export. Show useful explanatory labels for server timing exclusions,
single trial, accepted Wi-Fi route and shared CPU quotas. Tables/cards must fit
320/390px mobile without body horizontal scroll; scrolling graph/table only in
contained regions when appropriate. README and docs/index link to new report
without overwriting old dashboards. Primary verifies desktop/mobile controls
using user-authorized headless Chrome fallback, then verifies published URLs.
