# SQLite framework ramp

This experiment compares 15 framework adapters against the same file-backed SQLite
API contract, seed data, resource envelope, and open-arrival-rate schedule. It is
separate from the earlier HTTP reports and does not replace their source, images,
measurements, or dashboards. The methodology is specified in the [SQLite ramp
plan](../../../docs/experiments/sqlite-ramp-plan.md); the report source is in
[`report/`](report/).

The measured deployment uses one benchmark pod at a time, one process, one CPU and
512 MiB, a fresh `emptyDir` database, NodePort 30083, and the current shared
Wi-Fi/LAN route. Each of the 15 adapters gets one trial: a 15-minute
open-arrival-rate run with five three-minute stages at 300, 600, 900, 1200, and
1500 RPS. Go builds with `CGO_ENABLED=0` and modernc SQLite. The network route,
one-trial design, and shared-node CPU quota are reported as limitations; results
will not be treated as universal framework rankings.

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
make ramp-campaign RAMP_CAMPAIGN_ARGS='--cluster-repo ephemeral/ramp-cluster --ssh-host user@node --node-ip 192.168.1.2 --source-revision <app-commit> --results-dir results/http/sqlite-ramp'
```

For execution, use a clean isolated cluster clone at `ephemeral/ramp-cluster`, a
JSON image map containing exactly one immutable GHCR `sha256` digest for each of
the six runtime children (`go`, `node`, `bun`, `rust`, `python`, and `elixir`), and
separate 40-character application and harness source revisions. The controller
deploys only one benchmark pod at a time. It saves the exact baseline desired-state
bytes before the campaign and restores them through Flux after the campaign, even
when a run fails.

```sh
make ramp-campaign RAMP_CAMPAIGN_ARGS='--execute --cluster-repo ephemeral/ramp-cluster --ssh-host user@node --node-ip 192.168.1.2 --image-map path/to/images.json --source-revision <40-char-app-sha> --harness-source-revision <40-char-harness-sha> --results-dir results/http/sqlite-ramp'
```

To resume while explicitly retaining an already-reviewed invalid trial, add
`--resume --retain-invalid-attempt <UUID>` to the execution arguments. The flag is
repeatable, keeps each retained attempt invalid, and cannot prove capacity; without
it, a resumed campaign retries invalid attempts.

When actual recordings exist, render their sanitized aggregate report with:

```sh
make ramp-report RAMP_RESULTS_DIR=results/http/sqlite-ramp RAMP_OUTPUT_DIR=docs/reports/http/sqlite-ramp
```

This writes the report and `comparison.csv` to `docs/reports/http/sqlite-ramp/`.
Review generated artifacts before committing them.
