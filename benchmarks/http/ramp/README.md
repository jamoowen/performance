# SQLite framework ramp

This is the second HTTP experiment. It compares the requested framework matrix
against the same file-backed SQLite API contract, seed data, resource envelope,
and open-arrival-rate schedule. It is separate from the earlier HTTP reports and
does not replace their source, images, measurements, or dashboards.

The measured deployment uses one pod, one process, one CPU and 512 MiB, a fresh
`emptyDir` database, NodePort 30083, and the current shared Wi-Fi/LAN route. The
network route, one-trial design, and shared-node CPU quota are reported as
limitations; results will not be treated as universal framework rankings.

## Local checks

Install the runtime dependencies with `make ramp-tools`, then run `make
ramp-check`. The Go build uses `CGO_ENABLED=0`; the Elixir check needs Elixir
1.20.4 and OTP 28.5.0.7. CI runs that check in the pinned container because the
host toolchain can differ.

Build all six Linux/amd64 images locally with:

```sh
make ramp-build RAMP_TAG=dev
```

Run every real adapter for one runtime against the shared HTTP contract with:

```sh
make ramp-contracts RAMP_RUNTIME=go RAMP_IMAGE=local/performance-http-ramp-go:dev
```

The contract runner starts each selected framework with an isolated seeded
database. It verifies the fixed product data, strict request validation,
response timing header, SQLite PRAGMAs, and concurrent atomic writes.

`make ramp-campaign` invokes the campaign planner. It is dry-run by default;
execution needs explicit `--execute`, an isolated ephemeral cluster clone, and
an exact six-runtime GHCR digest map. The production generator preflight
requires 20 GiB free disk, 4 GiB available memory for the fixed 3200-VU
allocation, and a child `RLIMIT_NOFILE` of 8192. These thresholds are
conservative configuration guards, not measurements of k6 memory use.

```sh
make ramp-campaign RAMP_CAMPAIGN_ARGS='--cluster-repo ../ephemeral/ramp-cluster --ssh-host user@node --node-ip 192.168.1.2 --source-revision <app-commit> --results-dir results/http/sqlite-ramp'
```

When recordings exist, render their sanitized aggregate input with:

```sh
make ramp-report RAMP_INPUT=path/to/runs.json
```

The report will be written to `docs/reports/http/sqlite-ramp/`; no report link
is published until a real campaign has produced measured artifacts.
