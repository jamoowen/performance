import { check } from "k6";
import exec from "k6/execution";
import http from "k6/http";
import { Counter, Gauge, Trend } from "k6/metrics";

const base = __ENV.BASE_URL || "http://127.0.0.1:30083";
const mode = __ENV.MODE || "step";
const seed = Number(__ENV.SEED_COUNT || 5000);
const target = Number(__ENV.TARGET_RPS || 300);
const transition = Number(__ENV.TRANSITION_SECONDS || 15);
const settling = Number(__ENV.SETTLING_SECONDS || 0);
const stable = Number(__ENV.STABLE_SECONDS || 75);
const vus = Number(__ENV.PREALLOCATED_VUS || 1024);
const origin = new Gauge("scenario_origin");
const outcomes = new Counter("request_outcomes");
const stocks = new Counter("stock_successes");
const service = new Trend("service_duration", true);
const database = new Trend("db_duration", true);
export const options =
  mode === "warmup"
    ? {
        systemTags: ["status", "method", "name"],
        scenarios: {
          warmup: {
            executor: "constant-arrival-rate",
            rate: 100,
            timeUnit: "1s",
            duration: "30s",
            preAllocatedVUs: 256,
            maxVUs: 256,
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
            startRate: Number(__ENV.START_RPS || target),
            timeUnit: "1s",
            stages: [
              { target, duration: `${transition || settling}s` },
              { target, duration: `${stable}s` },
            ],
            preAllocatedVUs: vus,
            maxVUs: vus,
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
  if (mode === "warmup") return [100, "stable"];
  const elapsed = Date.now() / 1000 - exec.scenario.startTime / 1000;
  if (elapsed < transition) return [target, "transition"];
  if (elapsed < transition + settling) return [target, "settling"];
  return elapsed < transition + settling + stable ? [target, "stable"] : [0, "drain"];
}
function timed(r, tags) {
  const raw = r.headers["Server-Timing"];
  if (!raw) return false;
  const timings = Object.fromEntries(
    raw.split(",").map((part) => {
      const pair = part.trim().split(";dur=");
      return [pair[0], Number(pair[1])];
    }),
  );
  if (
    !Number.isFinite(timings.service) ||
    !Number.isFinite(timings.db) ||
    timings.service < 0 ||
    timings.db < 0 ||
    timings.db > timings.service + 0.001
  )
    return false;
  service.add(timings.service, tags);
  database.add(timings.db, tags);
  return true;
}
function shaped(r, operation, id) {
  if (r.status !== 200) return false;
  try {
    const body = r.json();
    if (operation === "detail")
      return (
        body.id === id &&
        body.name === `Product${String(id).padStart(5, "0")}` &&
        Number.isInteger(body.stock) &&
        Number.isInteger(body.revision)
      );
    if (operation === "list")
      return body.total === seed && body.products.length === 20 && Number.isInteger(body.offset);
    return body.id === id && Number.isInteger(body.stock) && Number.isInteger(body.revision);
  } catch (_) {
    return false;
  }
}
export function request() {
  if (exec.vu.iterationInScenario === 0) origin.add(exec.scenario.startTime / 1000);
  const iteration = exec.scenario.iterationInTest;
  const [level, phaseName] = phase();
  const id = (hash(iteration, 0x2468ace0) % seed) + 1;
  const tags = { level: String(level), phase: phaseName };
  let operation, response;
  const pick = hash(iteration, 0x13579bdf) % 10;
  if (pick < 5) {
    operation = "detail";
    response = http.get(`${base}/products/${id}`, {
      tags: { ...tags, operation, name: "GET /products/:id" },
      timeout: "2s",
    });
  } else if (pick < 8) {
    operation = "list";
    response = http.get(
      `${base}/products?offset=${hash(iteration, 0xa5a5a5a5) % (seed - 19)}&limit=20`,
      { tags: { ...tags, operation, name: "GET /products" }, timeout: "2s" },
    );
  } else {
    operation = "stock";
    response = http.post(`${base}/products/${id}/stock`, '{"delta":1}', {
      headers: { "content-type": "application/json" },
      tags: { ...tags, operation, name: "POST /products/:id/stock" },
      timeout: "2s",
    });
  }
  const valid = shaped(response, operation, id) && timed(response, { ...tags, operation });
  const passed = check(
    response,
    { "status shape and timing valid": () => valid },
    { ...tags, operation },
  );
  const outcome =
    response.status === 0 || response.status !== 200
      ? "http_error"
      : passed
        ? "success"
        : "validation_error";
  outcomes.add(1, { ...tags, operation, status: String(response.status), outcome });
  if (operation === "stock" && passed) stocks.add(1, { ...tags, operation });
}
