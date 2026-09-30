# HTTP SQLite baseline

This compares Go `net/http` `ServeMux` and Bun `Bun.serve` route tables with the same deterministic catalog, JSON contract, and filesystem SQLite workload. Both support `HEAD` on read routes, use strict single-segment product parameters, and retain their routers’ native canonical-path redirects and normalization. Give each server its own database file or volume.

| Route | Operation |
| --- | --- |
| `GET /healthz` | readiness |
| `GET /products/{id}` | product detail |
| `GET /products?category=&q=&offset=&limit=` | filtered catalog |
| `GET /reports/catalog`, `/reports/events` | read reports |
| `POST /cart/quote` | pricing and JSON serialization |
| `POST /events/batch` | atomic counter writes |

| Setting | Default and constraints |
| --- | --- |
| `PORT` | `8080`; 1–65535 |
| `SEED_COUNT` | `5000`; 1–100000; must match an existing DB |
| `DB_PATH` | `./data/benchmark.sqlite` relative to process cwd; containers use `/data/benchmark.sqlite` |
| `MAX_OPEN_CONNS` | Go only: `1` (1–32); Bun uses one synchronous connection |
| `PROFILE` | `smoke`, `steady`, or `stress`; default `smoke` |
| `WORKLOAD` | `mixed`, `list`, `detail`, `report`, `quote`, `batch`, or `events-report` |
| `RATE`, `DURATION` | 50 requests/s and `30s` for arrival-rate profiles |
| `PREALLOCATED_VUS`, `MAX_VUS` | 10 and 100 |
| `P95_MS`, `MAX_ERROR_RATE` | 1000 ms and 0.01, inclusive |

## Run and verify

From the repository root:

```sh
(cd benchmarks/http/go && DB_PATH=/tmp/http-go.sqlite go run .)
PORT=8081 DB_PATH=/tmp/http-bun.sqlite bun run benchmarks/http/bun/server.js
make test
```

The Go server listens on 8080 and the Bun server example listens on 8081, so they can run together. Use a separate terminal for either example:

```sh
curl http://127.0.0.1:8080/products/3
curl -H 'Content-Type: application/json' -d '{"items":[{"productId":1,"quantity":2}],"coupon":"SAVE10"}' http://127.0.0.1:8080/cart/quote
curl -H 'Content-Type: application/json' -d '{"events":[{"userId":1,"type":"view","value":1}]}' http://127.0.0.1:8080/events/batch
```

The shared suite starts native servers with separate temporary file databases:

```sh
python3 benchmarks/http/tests/contract_test.py
```

Set both `GO_BASE_URL` and `BUN_BASE_URL` to run HTTP parity checks against deployed containers. Restart and direct SQLite file checks are skipped in that mode.

## Images and k6

```sh
make build IMAGE_PREFIX=ghcr.io/OWNER/performance TAG=http-sqlite-v1 PLATFORM=linux/amd64
docker login ghcr.io
make push IMAGE_PREFIX=ghcr.io/OWNER/performance TAG=http-sqlite-v1 PLATFORM=linux/amd64,linux/arm64
docker run --rm -e BASE_URL=http://host.docker.internal:8080 -e PROFILE=steady -e RATE=100 -e DURATION=30s ghcr.io/OWNER/performance-http-load:http-sqlite-v1
```

The images are `ghcr.io/OWNER/performance-http-go`, `ghcr.io/OWNER/performance-http-bun`, and `ghcr.io/OWNER/performance-http-load`. In a cluster, give the load container `BASE_URL=http://SERVICE.NAMESPACE.svc.cluster.local:8080`. Mount a writable `/data` volume: Go runs as UID 65534 and Bun as UID 1000. A new volume/path resets data; an existing database rejects a different seed count.

Smoke performs 20 iterations. Mixed traffic is 35% list, 25% detail, 15% catalog report, 15% quote, and 10% event batches: 90% reads and 10% writes. Steady uses a constant arrival rate. Stress holds RATE for 20 seconds, then RATE×2 and RATE×3 for `DURATION`, before a 10-second ramp-down. Console output includes p50/p95/p99, errors, dropped iterations for arrival profiles, and per-operation p95 thresholds. Size VUs high enough that dropped iterations measure generator capacity rather than application capacity.

Products and users are immutable. Events are bounded counters (at most `3 * SEED_COUNT` keys), not an event log. JSON errors have an `error` field; malformed input is 400, missing rows 404, wrong methods 405 with `Allow`, non-JSON POSTs 415, and bodies over 1 MiB 413. Quotes use integer cents: `SAVE10` floors `subtotal * 10 / 100`, then tax floors `(subtotal - discount) * 20 / 100`. Ordinary integer JSON inputs are shared; lexical forms such as `1.0` are outside the shared contract.

Go is pinned to 1.27.1, Bun to 1.4.0, and k6 to 1.3.0. Go alone uses native-C `mattn/go-sqlite3` 1.14.52. Initial container checks reported SQLite 3.53.4 for Go and 3.53.2 for Bun; record runtime versions with each result. Both use WAL and `synchronous=NORMAL`: WAL allows readers while one writer operates, while NORMAL does not guarantee the latest committed transactions after power loss. See [SQLite WAL](https://www.sqlite.org/wal.html), [SQLite pragmas](https://www.sqlite.org/pragma.html), [Bun SQLite](https://bun.com/docs/runtime/sqlite), and [k6 constant arrival rate](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/constant-arrival-rate/).

Warm up separately. Compare process/container memory as well as latency; filesystem page cache, storage class, CPU limits, TLS, logging, and connection reuse all affect results. Start with one CPU and Go `MAX_OPEN_CONNS=1`; record multicore Go pool tests separately.

## Development tooling

Tooling is pinned in the repository: Biome 2.5.15 formats and lints JavaScript/JSON, golangci-lint 2.14.0 checks Go, and Ruff 0.16.4 formats and lints the Python contract suite. Run `make tools` once, then use `make format`, `make format-check`, `make lint`, and `make check`. Tools install or cache locally. See [Biome](https://biomejs.dev/), [golangci-lint](https://golangci-lint.run/), and [Ruff](https://docs.astral.sh/ruff/).

`make tools` requires Go with a C compiler, Bun, Python 3.10 or later, uv, curl, and network access for the pinned downloads. `make check` runs non-mutating format checks, all three linters, Go race tests, and the shared local HTTP/SQLite contract suite.

## NodePort workflow

Deploy to a single-node Flux cluster through the separate private cluster repository. Edit and commit its `apps/performance-http` manifests rather than using `kubectl apply` or manual scaling. Publish real images as `ghcr.io/OWNER/performance-http-go` and `ghcr.io/OWNER/performance-http-bun`, then pin their immutable linux/amd64 digests there.

The separate cluster repository’s `apps/performance-http` directory remains unregistered and inactive while image digests are pending. Replace placeholders and explicitly include it in that repository’s Flux root Kustomization only after both images are available.

Use one active implementation at a time. The services below are documentation snippets for the separate Flux repository, not resources applied by this repository. They assume the Deployment labels match `app.kubernetes.io/name`, the container exposes a named `http` port on 8080, and both apps use `/healthz` probes. Create the `performance` namespace there if it does not already exist.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: http-go
  namespace: performance
spec:
  type: NodePort
  externalTrafficPolicy: Local
  selector:
    app.kubernetes.io/name: http-go
  ports:
    - name: http
      protocol: TCP
      port: 8080
      targetPort: http
      nodePort: 30080
```

For Bun, use `metadata.name: http-bun`, selector `app.kubernetes.io/name: http-bun`, and `nodePort: 30081`. Override `GO_NODE_PORT` or `BUN_NODE_PORT` if the cluster uses different ports. Give each replica its own writable `/data` directory, keep `SEED_COUNT` equal, and use equivalent CPU and memory resources. Go runs as UID 65534 and Bun as UID 1000. Select the active application by committing replicas `1` or `0` through Flux, never with `kubectl scale`.

Install native k6 on the Mac with `brew install k6`, then record `k6 version`; Homebrew may provide a newer release. To match the pinned generator, use the official [k6 v1.3.0 release](https://github.com/grafana/k6/releases/tag/v1.3.0), or use the Docker load image pinned to k6 1.3.0. Use the same generator version for both app runs. After activating one implementation, verify and smoke-test that implementation only:

```sh
cp .local.mk.example .local.mk
# edit NODE_IP in .local.mk
make load-go PROFILE=smoke
```

For Bun, use `make load-bun PROFILE=smoke` after switching activation through Git and Flux. `.local.mk` affects Make targets only; use `NODE_IP=192.0.2.10` as a shell example for direct commands. Run only the active app. Seed startup belongs outside the measurement. Warm up, then run a measured steady workload:

```sh
make load-go PROFILE=steady DURATION=60s RATE=100 WORKLOAD=mixed
make load-go PROFILE=steady DURATION=5m RATE=100 WORKLOAD=mixed
```

`make load` requires an explicit `BASE_URL`; all scenario settings can be overridden, including `K6`, `NODE_IP`, ports, `SEED_COUNT`, VUs, latency threshold, and error threshold. k6 reports latency and schema/check errors, not server CPU or memory. For a quick read-only snapshot use `ssh USER@NODE_IP 'kubectl top pods -n performance'`; record sustained resource observations with the same time window as each run.

Save local output without committing it:

```sh
mkdir -p results/http
make load-go PROFILE=steady DURATION=5m RATE=100 >results/http/go-steady.log 2>&1
make load-bun PROFILE=steady DURATION=5m RATE=100 >results/http/bun-steady.log 2>&1
```

## GitHub Actions publication

The `HTTP images` workflow checks pull requests and publishes the Go and Bun linux/amd64 images on `main` or manual dispatch. It uses GitHub Actions’ ephemeral `GITHUB_TOKEN`; no personal access token is configured or required by this workflow. Each publish job writes an immutable `image@digest` reference to the workflow summary. Copy those digest references into the separate private Flux repository before activating either workload.

Repository visibility and package visibility are separate. A public source repository does not make GHCR packages public automatically. If images are made public for anonymous pulls, remove the cluster workload’s `imagePullSecrets` reference; private pulls still require the namespace-scoped pull Secret documented in that private cluster repository.
