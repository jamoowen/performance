# Adaptive SQLite capacity search

This harness runs a short, adaptive capacity search for 13 API adapters. It
keeps the ramp workload and immutable API images from the SQLite framework ramp,
but stops an adapter after sustained overload instead of spending more time at
known-losing rates.

Read the [experiment plan](../../../docs/experiments/sqlite-capacity-plan.md)
before running it. The controller records one result per adapter with its actual
step schedule, elapsed gaps, pod telemetry, container CFS telemetry, restart/OOM
events, and generator headroom qualification.

The public report is generated only after the full 13-adapter campaign has real
captures. It intentionally does not publish a placeholder report or invent
untested rates.

From the repository root, inspect the immutable plan before mutating Flux:

```sh
make capacity-campaign CAPACITY_CAMPAIGN_ARGS='--cluster-repo <cluster-repo> --ssh-host <ssh-host> --node-ip <node-ip> --image-map <image-map.json> --source-revision <api-sha>'
```

Run or resume the campaign only with the immutable API source revision and the
current 40-character harness revision supplied by the controller. The API image
source remains `5204a2af2364cd5f8cf8c567f5782ec896fef3d4`; the harness revision
identifies the controller, recorder, and telemetry code that collected a run.

```sh
make capacity-campaign CAPACITY_CAMPAIGN_ARGS='--execute --cluster-repo <cluster-repo> --ssh-host <ssh-host> --node-ip <node-ip> --image-map <image-map.json> --source-revision <api-sha> --harness-source-revision <harness-sha>'
make capacity-campaign CAPACITY_CAMPAIGN_ARGS='--execute --resume --cluster-repo <cluster-repo> --ssh-host <ssh-host> --node-ip <node-ip> --image-map <image-map.json> --source-revision <api-sha> --harness-source-revision <harness-sha>'
make capacity-report CAPACITY_RESULTS_DIR=results/http/sqlite-capacity
```

The final command should only run after the complete campaign; use the report
renderer's explicit `--allow-partial` option for a private progress preview.
