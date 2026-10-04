# Performance experiments

### what is this?
This is me sating my curiosity - I was interested in seeing how a bunch of different langs and frameworks compared against eachother.

I was particularly interested in performance - How many requests per second before each api broke? How did the request latencies compare? How much memory and cpu got used during this?

### how i did it
- I chose a few different languages and frameworks, t
- I told GPT-6.1-Sol high to write a basic http api in each service. 
- I chose sqlite as the db and seeded it with test data.
- I deployed each api to my small home server with a set amount of compute each time
- I used K6 to generate a mixture of different load and captured the result

### what this tells us
Firstly, many people will point out that this is ai slop. 

It is kind of. 

And perhaps a good Elixir developer would be able to write a far more performant api...

But this in itself is a great test and tells us a lot - If an average developer or vibecoder were to write these services, this is probably a similar result to what they would get. Performance tuning is irrelevant here and is not the goal of these tests.

These tests give us a rough indication of what these langs can output given a very small single core machine to run on with sqlite.

The results are not indicative of the langs/frameworks performance across all deployment architectures - only this limited case I have set it up with.

The results definitely surprised me and upset me a little as a fan of Go (I was hoping for better Go performance)


Real HTTP, JSON, and SQLite experiments running on local Kubernetes.

## Latest results — SQLite capacity search

Start with the [interactive SQLite capacity report](https://jamoowen.github.io/performance/reports/http/sqlite-capacity/comparison.html), the [findings](docs/reports/http/sqlite-capacity/findings.md), and the [live CSV](https://jamoowen.github.io/performance/reports/http/sqlite-capacity/comparison.csv).

| Adapter | Highest fully passing RPS | First sustained overload RPS |
| --- | ---: | ---: |
| Go net/http | 5724 | 8944 |
| Go Chi | 5724 | 7155 |
| Go Fiber | 7155 | 8944 |
| Node Express | 5724 | 7155 |
| Node Nest | 4579 | 5724 |
| Node Fastify | 5724 | 7155 |
| Bun native | 8944 | 11180 |
| Bun Hono | 7155 | 11180 |
| Bun Elysia | 8944 | 11180 |
| Rust Axum | 11180 | 13975 |
| Rust Actix | 8944 | 13975 |
| Python FastAPI | 900 | 1200 |
| Elixir Phoenix | 300 | 600 |

Fully passing means the whole SLO passed. Sustained overload means at least 1% errors or dropped arrivals over the full stable window, persisting in four consecutive five-second buckets. The highest fully passing tier is an observed step, not a monotonic guarantee; see the [full report](https://jamoowen.github.io/performance/reports/http/sqlite-capacity/comparison.html) for the last tier below overload and detailed metrics.

The 13 valid captures used one CPU quota, 512 MiB, SQLite with 5,000 products, and a 50% detail / 30% list / 20% write mix. They ran on a shared node and Wi-Fi route with 1,024 fixed measured VUs, a two-second deadline, and 90-second tiers (15 seconds settling and 75 seconds stable), increasing by 25% above 1,500 RPS. These are whole-stack exploratory request-path and concurrency results from one trial, not universal language rankings.

Rust reached the highest below-overload tier at 11,180 RPS, versus Bun at 8,944 RPS. Every overload window except Phoenix had zero HTTP and validation errors among completed requests; their overload appeared as dropped, unissued arrivals. Phoenix showed CPU throttling and timeouts at 600 RPS. There was no OOM in this cohort, and Phoenix committed 7,554 writes without an acknowledgement.

## Interactive dashboards

Latest results:

| Experiment | Dashboard |
| --- | --- |
| SQLite capacity search | [interactive report](https://jamoowen.github.io/performance/reports/http/sqlite-capacity/comparison.html) |

Historical separate experiments:

| Experiment | Dashboard |
| --- | --- |
| 15-adapter SQLite framework ramp | [interactive report](https://jamoowen.github.io/performance/reports/http/sqlite-ramp/comparison.html) |
| Go scheduling | [interactive report](https://jamoowen.github.io/performance/reports/http/scheduling/comparison.html) |
| SQLite storage | [interactive report](https://jamoowen.github.io/performance/reports/http/sqlite/comparison.html) |
| Memory workload | [interactive report](https://jamoowen.github.io/performance/reports/http/memory/comparison.html) |
| Frameworks | [interactive report](https://jamoowen.github.io/performance/reports/http/frameworks/comparison.html) |
| Multicore scaling | [interactive report](https://jamoowen.github.io/performance/reports/http/scaling/comparison.html) |
| Original diagnostic comparison | [interactive report](https://jamoowen.github.io/performance/reports/http/comparison.html) |

Browse the [report index](https://jamoowen.github.io/performance/). Older campaigns also retain [historical findings](docs/reports/http/findings.md), [ramp findings](docs/reports/http/sqlite-ramp/findings.md), [follow-up findings](docs/reports/http/followup-findings.md), and a [methodology audit](docs/reports/http/methodology-review.md); the audit covers its historical experiment rather than certifying the newer capacity campaign.

## Generate and reproduce reports

These commands require local, ignored recordings. Committed `docs/` artifacts are published by the configured `main`/`docs` GitHub Pages workflow; historical findings are maintained manually.

```sh
# Latest SQLite capacity search
make capacity-report

# 15-adapter SQLite framework ramp
make ramp-report RAMP_RESULTS_DIR=results/http/sqlite-ramp

# Historical follow-up suite
make report-suite

# Original diagnostic comparison; local results are written under results/http/report/
make publish-report
make compare
```

The [HTTP benchmark README](benchmarks/http/README.md), [capacity harness README](benchmarks/http/capacity/README.md), [capacity experiment plan](docs/experiments/sqlite-capacity-plan.md), [ramp README](benchmarks/http/ramp/README.md), and [ramp experiment plan](docs/experiments/sqlite-ramp-plan.md) describe the available harnesses and protocols.

## Future ideas

- Compare SQLite and Postgres.
- Add mixed database workloads.
- Compare agent frameworks.
