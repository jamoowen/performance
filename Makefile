IMAGE_PREFIX ?= ghcr.io/owner/performance
TAG ?= latest
PLATFORM ?= linux/amd64
K6 ?= k6
NODE_IP ?=
GO_NODE_PORT ?= 30080
BUN_NODE_PORT ?= 30081
PROFILE ?= smoke
WORKLOAD ?= mixed
RATE ?= 50
DURATION ?= 30s
SEED_COUNT ?= 5000
PREALLOCATED_VUS ?= 10
MAX_VUS ?= 100
P95_MS ?= 1000
MAX_ERROR_RATE ?= 0.01
SSH_HOST ?=
NAMESPACE ?= my-api
WARMUP_DURATION ?= 60s
SAMPLE_INTERVAL ?= 5
DIAGNOSTICS ?= 0
PROFILE_SECONDS ?= 30
RESULTS_DIR ?= results/http

-include .local.mk

.PHONY: tools format format-check lint check test build build-go build-bun build-load push load load-go load-bun record-go record-bun compare

GOLANGCI_LINT := GOCACHE=$(CURDIR)/.cache/go-build GOMODCACHE=$(CURDIR)/.cache/go-mod GOLANGCI_LINT_CACHE=$(CURDIR)/.cache/golangci-lint $(CURDIR)/.tools/bin/golangci-lint
RUFF := UV_CACHE_DIR=$(CURDIR)/.cache/uv UV_TOOL_DIR=$(CURDIR)/.cache/uv-tools uvx --from ruff==0.16.4 ruff

tools:
	bun install --frozen-lockfile
	mkdir -p .tools/bin
	curl --fail --silent --show-error --location https://golangci-lint.run/install.sh -o .tools/install-golangci-lint.sh
	sh .tools/install-golangci-lint.sh -b .tools/bin v2.14.0
	cd benchmarks/http/go && GOMODCACHE=$(CURDIR)/.cache/go-mod go mod download
	$(RUFF) --version

format:
	gofmt -w benchmarks/http/go/*.go
	./node_modules/.bin/biome format --write benchmarks/http package.json biome.json
	$(RUFF) format benchmarks/http/measure benchmarks/http/tests

format-check:
	@unformatted="$$(gofmt -l benchmarks/http/go/*.go)" && { test -z "$$unformatted" || { printf '%s\n' "$$unformatted"; exit 1; }; }
	./node_modules/.bin/biome check --formatter-enabled=true --linter-enabled=false --assist-enabled=false benchmarks/http package.json biome.json
	$(RUFF) format --check benchmarks/http/measure benchmarks/http/tests

lint:
	cd benchmarks/http/go && $(GOLANGCI_LINT) run --config ../../../.golangci.yml ./...
	./node_modules/.bin/biome lint --error-on-warnings benchmarks/http package.json biome.json
	$(RUFF) check benchmarks/http/measure benchmarks/http/tests

check: format-check lint test

test:
	cd benchmarks/http/go && go vet ./... && go test -race ./...
	bun test ./benchmarks/http/tests/diagnostics_bun_test.js
	python3 benchmarks/http/tests/contract_test.py
	PYTHONPATH=benchmarks/http python3 -m unittest discover -s benchmarks/http/tests -p 'measurement*_test.py'

build: build-go build-bun build-load

build-go:
	docker buildx build --load --platform $(PLATFORM) -f benchmarks/http/go/Dockerfile -t $(IMAGE_PREFIX)-http-go:$(TAG) .

build-bun:
	docker buildx build --load --platform $(PLATFORM) -f benchmarks/http/bun/Dockerfile -t $(IMAGE_PREFIX)-http-bun:$(TAG) .

build-load:
	docker buildx build --load --platform $(PLATFORM) -f benchmarks/http/load/Dockerfile -t $(IMAGE_PREFIX)-http-load:$(TAG) .

push:
	docker buildx build --push --platform $(PLATFORM) -f benchmarks/http/go/Dockerfile -t $(IMAGE_PREFIX)-http-go:$(TAG) .
	docker buildx build --push --platform $(PLATFORM) -f benchmarks/http/bun/Dockerfile -t $(IMAGE_PREFIX)-http-bun:$(TAG) .
	docker buildx build --push --platform $(PLATFORM) -f benchmarks/http/load/Dockerfile -t $(IMAGE_PREFIX)-http-load:$(TAG) .

load:
	@test -n "$(BASE_URL)" || { echo "BASE_URL is required, for example: make load BASE_URL=http://NODE_IP:30080"; exit 2; }
	"$(K6)" run \
		-e BASE_URL="$(BASE_URL)" \
		-e PROFILE="$(PROFILE)" \
		-e WORKLOAD="$(WORKLOAD)" \
		-e RATE="$(RATE)" \
		-e DURATION="$(DURATION)" \
		-e SEED_COUNT="$(SEED_COUNT)" \
		-e PREALLOCATED_VUS="$(PREALLOCATED_VUS)" \
		-e MAX_VUS="$(MAX_VUS)" \
		-e P95_MS="$(P95_MS)" \
		-e MAX_ERROR_RATE="$(MAX_ERROR_RATE)" \
		benchmarks/http/load.js

load-go:
	@test -n "$(NODE_IP)" || { echo "NODE_IP is required; set NODE_IP=... or copy .local.mk.example to .local.mk"; exit 2; }
	$(MAKE) load BASE_URL="http://$(NODE_IP):$(GO_NODE_PORT)"

load-bun:
	@test -n "$(NODE_IP)" || { echo "NODE_IP is required; set NODE_IP=... or copy .local.mk.example to .local.mk"; exit 2; }
	$(MAKE) load BASE_URL="http://$(NODE_IP):$(BUN_NODE_PORT)"

record-go:
	@test -n "$(NODE_IP)" || { echo "NODE_IP is required; set NODE_IP=... or copy .local.mk.example to .local.mk"; exit 2; }
	@test -n "$(SSH_HOST)" || { echo "SSH_HOST is required; set SSH_HOST=USER@NODE_IP in .local.mk"; exit 2; }
	PYTHONPATH=benchmarks/http python3 -m measure.run go --base-url "http://$(NODE_IP):$(GO_NODE_PORT)" --ssh-host "$(SSH_HOST)" --namespace "$(NAMESPACE)" --profile "$(PROFILE)" --workload "$(WORKLOAD)" --rate "$(RATE)" --duration "$(DURATION)" --seed-count "$(SEED_COUNT)" --preallocated-vus "$(PREALLOCATED_VUS)" --max-vus "$(MAX_VUS)" --p95-ms "$(P95_MS)" --max-error-rate "$(MAX_ERROR_RATE)" --k6 "$(K6)" --warmup-duration "$(WARMUP_DURATION)" --sample-interval "$(SAMPLE_INTERVAL)" --results-dir "$(RESULTS_DIR)" $(if $(filter 1,$(DIAGNOSTICS)),--diagnostics --diagnostics-seconds "$(PROFILE_SECONDS)")

record-go: PROFILE = steady
record-go: RATE = 100
record-go: DURATION = 5m

record-bun:
	@test -n "$(NODE_IP)" || { echo "NODE_IP is required; set NODE_IP=... or copy .local.mk.example to .local.mk"; exit 2; }
	@test -n "$(SSH_HOST)" || { echo "SSH_HOST is required; set SSH_HOST=USER@NODE_IP in .local.mk"; exit 2; }
	PYTHONPATH=benchmarks/http python3 -m measure.run bun --base-url "http://$(NODE_IP):$(BUN_NODE_PORT)" --ssh-host "$(SSH_HOST)" --namespace "$(NAMESPACE)" --profile "$(PROFILE)" --workload "$(WORKLOAD)" --rate "$(RATE)" --duration "$(DURATION)" --seed-count "$(SEED_COUNT)" --preallocated-vus "$(PREALLOCATED_VUS)" --max-vus "$(MAX_VUS)" --p95-ms "$(P95_MS)" --max-error-rate "$(MAX_ERROR_RATE)" --k6 "$(K6)" --warmup-duration "$(WARMUP_DURATION)" --sample-interval "$(SAMPLE_INTERVAL)" --results-dir "$(RESULTS_DIR)" $(if $(filter 1,$(DIAGNOSTICS)),--diagnostics --diagnostics-seconds "$(PROFILE_SECONDS)")

record-bun: PROFILE = steady
record-bun: RATE = 100
record-bun: DURATION = 5m

compare:
	UV_CACHE_DIR=$(CURDIR)/.cache/uv PYTHONPATH=benchmarks/http uv run --no-project --with plotly==7.1.0 python3 -m measure.compare --results-dir "$(RESULTS_DIR)" --output-dir "$(RESULTS_DIR)/report"
