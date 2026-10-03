import { check } from "k6";
import exec from "k6/execution";
import http from "k6/http";
import { Counter, Gauge, Trend } from "k6/metrics";

const base = __ENV.BASE_URL || "http://127.0.0.1:30083";
const mode = __ENV.MODE || "measurement";
const seed = Number(__ENV.SEED_COUNT || 5000);
const levels = JSON.parse(
  __ENV.SCHEDULE_JSON ||
    '[{"targetRps":300,"transitionSeconds":0,"stableSeconds":160,"settlingSeconds":20},{"targetRps":600,"transitionSeconds":20,"stableSeconds":160},{"targetRps":900,"transitionSeconds":20,"stableSeconds":160},{"targetRps":1200,"transitionSeconds":20,"stableSeconds":160},{"targetRps":1500,"transitionSeconds":20,"stableSeconds":160}]',
);
const origin = new Gauge("scenario_origin"),
  outcomes = new Counter("request_outcomes"),
  stocks = new Counter("stock_successes"),
  service = new Trend("service_duration", true),
  database = new Trend("db_duration", true);
function stages() {
  const result = [];
  for (const level of levels) {
    const ramp = Number(level.transitionSeconds || 0),
      hold = Number(level.settlingSeconds || 0) + Number(level.stableSeconds || 0);
    if (ramp) {
      result.push({ target: Number(level.targetRps), duration: `${ramp}s` });
    }
    result.push({ target: Number(level.targetRps), duration: `${hold}s` });
  }
  return result;
}
export const options =
  mode === "warmup"
    ? {
        systemTags: ["status", "method", "name"],
        scenarios: {
          warmup: {
            executor: "constant-arrival-rate",
            rate: Number(__ENV.WARMUP_RPS || 100),
            timeUnit: "1s",
            duration: `${Number(__ENV.WARMUP_SECONDS || 60)}s`,
            preAllocatedVUs: Number(__ENV.WARMUP_VUS || 256),
            maxVUs: Number(__ENV.WARMUP_VUS || 256),
            gracefulStop: "3s",
            exec: "request",
          },
        },
      }
    : {
        systemTags: ["status", "method", "name"],
        scenarios: {
          measured: {
            executor: "ramping-arrival-rate",
            timeUnit: "1s",
            startRate: Number(levels[0].targetRps),
            stages: stages(),
            preAllocatedVUs: Number(__ENV.PREALLOCATED_VUS || 3200),
            maxVUs: Number(__ENV.PREALLOCATED_VUS || 3200),
            gracefulStop: "3s",
            exec: "request",
          },
        },
      };
function hash(v, s) {
  let x = (v ^ s) >>> 0;
  x = Math.imul(x ^ (x >>> 16), 0x45d9f3b);
  x = Math.imul(x ^ (x >>> 16), 0x45d9f3b);
  return (x ^ (x >>> 16)) >>> 0;
}
function phase() {
  if (mode === "warmup") {
    return [Number(__ENV.WARMUP_RPS || 100), "stable"];
  }
  let t = 0,
    e = Date.now() / 1000 - exec.scenario.startTime / 1000;
  for (const x of levels) {
    t += Number(x.transitionSeconds || 0);
    if (e < t) {
      return [x.targetRps, "transition"];
    }
    t += Number(x.settlingSeconds || 0);
    if (e < t) {
      return [x.targetRps, "settling"];
    }
    t += Number(x.stableSeconds || 0);
    if (e < t) {
      return [x.targetRps, "stable"];
    }
  }
  return [0, "drain"];
}
function timed(r, t) {
  const raw = r.headers["Server-Timing"];
  if (!raw) {
    return false;
  }
  const p = Object.fromEntries(
    raw.split(",").map((x) => {
      const a = x.trim().split(";dur=");
      return [a[0], Number(a[1])];
    }),
  );
  if (
    !Number.isFinite(p.service) ||
    !Number.isFinite(p.db) ||
    p.service < 0 ||
    p.db < 0 ||
    p.db > p.service + 0.001
  ) {
    return false;
  }
  service.add(p.service, t);
  database.add(p.db, t);
  return true;
}
function shaped(r, op, id) {
  if (r.status !== 200) {
    return false;
  }
  try {
    const b = r.json();
    if (op === "detail") {
      return (
        b.id === id &&
        b.name === `Product${String(id).padStart(5, "0")}` &&
        Number.isInteger(b.stock) &&
        Number.isInteger(b.revision)
      );
    }
    if (op === "list") {
      return b.total === seed && b.products.length === 20 && Number.isInteger(b.offset);
    }
    return b.id === id && Number.isInteger(b.stock) && Number.isInteger(b.revision);
  } catch (_) {
    return false;
  }
}
export function request() {
  if (exec.vu.iterationInScenario === 0) {
    origin.add(exec.scenario.startTime / 1000);
  }
  const iteration = exec.scenario.iterationInTest,
    [level, phaseName] = phase(),
    id = (hash(iteration, 0x2468ace0) % seed) + 1,
    tags = { level: String(level), phase: phaseName };
  let op, r;
  if (hash(iteration, 0x13579bdf) % 10 < 5) {
    op = "detail";
    r = http.get(`${base}/products/${id}`, {
      tags: { ...tags, operation: op, name: "GET /products/:id" },
      timeout: "2s",
    });
  } else if (hash(iteration, 0x13579bdf) % 10 < 8) {
    op = "list";
    r = http.get(`${base}/products?offset=${hash(iteration, 0xa5a5a5a5) % (seed - 19)}&limit=20`, {
      tags: { ...tags, operation: op, name: "GET /products" },
      timeout: "2s",
    });
  } else {
    op = "stock";
    r = http.post(`${base}/products/${id}/stock`, '{"delta":1}', {
      headers: { "content-type": "application/json" },
      tags: { ...tags, operation: op, name: "POST /products/:id/stock" },
      timeout: "2s",
    });
  }
  const ok = shaped(r, op, id) && timed(r, { ...tags, operation: op }),
    checked = check(r, { "status shape and timing valid": () => ok }, { ...tags, operation: op }),
    outcome =
      r.status === 0 || r.status !== 200 ? "http_error" : checked ? "success" : "validation_error";
  outcomes.add(1, { ...tags, operation: op, status: String(r.status), outcome });
  if (op === "stock" && checked) {
    stocks.add(1, { ...tags, operation: op });
  }
}
