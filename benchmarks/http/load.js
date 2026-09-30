import { check } from "k6";
import exec from "k6/execution";
import http from "k6/http";
import { Counter } from "k6/metrics";

const operations = ["list", "detail", "report", "quote", "batch", "events-report"];
const baseURL = readURL(
  "BASE_URL",
  __ENV.BASE_URL === undefined ? "http://127.0.0.1:8080" : __ENV.BASE_URL,
);
const seedCount = readIntegerEnvironment("SEED_COUNT", 5000, 1, 100000);
const rate = readIntegerEnvironment("RATE", 50, 1, 100000);
const duration = readDurationEnvironment("DURATION", "30s");
const preAllocatedVUs = readIntegerEnvironment("PREALLOCATED_VUS", 10, 1, 10000);
const maxVUs = readIntegerEnvironment("MAX_VUS", 100, preAllocatedVUs, 100000);
const profile = readChoiceEnvironment("PROFILE", "smoke", ["smoke", "steady", "stress"]);
const workload = readChoiceEnvironment("WORKLOAD", "mixed", ["mixed", ...operations]);
const p95Milliseconds = readNumberEnvironment("P95_MS", 1000, 0, 600000);
const maximumErrorRate = readNumberEnvironment("MAX_ERROR_RATE", 0.01, 0, 1);
const operationFailures = new Counter("operation_failures");

function readNumberEnvironment(name, fallback, minimum, maximum) {
  if (__ENV[name] === undefined) {
    return fallback;
  }
  const raw = __ENV[name];
  const value = Number(raw);
  if (raw.trim() === "" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be a finite number from ${minimum} to ${maximum}`);
  }
  return value;
}

function readIntegerEnvironment(name, fallback, minimum, maximum) {
  const value = readNumberEnvironment(name, fallback, minimum, maximum);
  if (!Number.isSafeInteger(value)) {
    throw new Error(`${name} must be an integer`);
  }
  return value;
}

function readDurationEnvironment(name, fallback) {
  const value = __ENV[name] === undefined ? fallback : __ENV[name];
  if (!/^\d+(ms|s|m|h)$/.test(value)) {
    throw new Error(`${name} must be a nonblank duration such as 30s`);
  }
  return value;
}

function readURL(name, value) {
  if (!value || !/^https?:\/\/[^/]+/.test(value)) {
    throw new Error(`${name} must be a nonblank http(s) URL`);
  }
  return value.replace(/\/$/, "");
}

function readChoiceEnvironment(name, fallback, choices) {
  const value = __ENV[name] === undefined ? fallback : __ENV[name];
  if (!choices.includes(value)) {
    throw new Error(`${name} must be one of: ${choices.join(", ")}`);
  }
  return value;
}

function activeOperations() {
  return workload === "mixed" ? operations.slice(0, 5) : [workload];
}

function buildThresholds() {
  const thresholds = {
    http_req_failed: [`rate<=${maximumErrorRate}`],
    checks: [`rate>=${1 - maximumErrorRate}`],
  };
  if (profile !== "smoke") {
    thresholds.dropped_iterations = ["count<=0"];
  }
  for (const operation of activeOperations()) {
    thresholds[`http_req_duration{operation:${operation}}`] = [`p(95)<=${p95Milliseconds}`];
  }
  return thresholds;
}

function buildScenarioOptions() {
  const common = {
    thresholds: buildThresholds(),
    summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],
    systemTags: [
      "proto",
      "subproto",
      "status",
      "method",
      "name",
      "group",
      "check",
      "error",
      "error_code",
      "scenario",
    ],
  };
  if (profile === "smoke") {
    return { ...common, vus: 1, iterations: 20 };
  }
  if (profile === "steady") {
    return {
      ...common,
      scenarios: {
        requests: {
          executor: "constant-arrival-rate",
          rate,
          timeUnit: "1s",
          duration,
          preAllocatedVUs,
          maxVUs,
        },
      },
    };
  }
  return {
    ...common,
    scenarios: {
      requests: {
        executor: "ramping-arrival-rate",
        startRate: rate,
        timeUnit: "1s",
        preAllocatedVUs,
        maxVUs,
        stages: [
          { target: rate, duration: "20s" },
          { target: rate * 2, duration },
          { target: rate * 3, duration },
          { target: 0, duration: "10s" },
        ],
      },
    },
  };
}

export const options = buildScenarioOptions();

function operationFor(iteration) {
  if (workload !== "mixed") {
    return workload;
  }
  const position = iteration % 20;
  if (position < 7) {
    return "list";
  }
  if (position < 12) {
    return "detail";
  }
  if (position < 15) {
    return "report";
  }
  if (position < 18) {
    return "quote";
  }
  return "batch";
}

function routeName(operation) {
  return {
    list: "/products",
    detail: "/products/:id",
    report: "/reports/catalog",
    quote: "/cart/quote",
    batch: "/events/batch",
    "events-report": "/reports/events",
  }[operation];
}

function requestFor(operation, iteration) {
  const productID = (iteration % seedCount) + 1;
  const tags = { operation, name: routeName(operation) };
  if (operation === "list") {
    return http.get(
      `${baseURL}/products?category=books&q=product&offset=${iteration % 100}&limit=20`,
      { tags },
    );
  }
  if (operation === "detail") {
    return http.get(`${baseURL}/products/${productID}`, { tags });
  }
  if (operation === "report" || operation === "events-report") {
    return http.get(baseURL + tags.name, { tags });
  }
  const requestBody =
    operation === "quote"
      ? {
          items: [
            { productId: 1, quantity: 1 },
            { productId: Math.min(2, seedCount), quantity: 1 },
          ],
          coupon: "SAVE10",
        }
      : {
          events: [
            { userId: productID, type: "view", value: 1 },
            { userId: productID, type: "click", value: 2 },
            { userId: productID, type: "purchase", value: 1999 },
          ],
        };
  return http.post(baseURL + tags.name, JSON.stringify(requestBody), {
    headers: { "Content-Type": "application/json" },
    tags,
  });
}

function hasEventReportSchema(responseBody) {
  return ["view", "click", "purchase"].every(
    (eventType) =>
      Number.isInteger(responseBody.counts?.[eventType]) &&
      Number.isInteger(responseBody.values?.[eventType]),
  );
}

function hasExpectedSchema(operation, response) {
  if (response.status !== 200 || !response.headers["Content-Type"]?.includes("application/json")) {
    return false;
  }
  try {
    const responseBody = response.json();
    if (operation === "list") {
      return (
        Array.isArray(responseBody.products) &&
        ["total", "offset", "limit"].every((field) => Number.isInteger(responseBody[field]))
      );
    }
    if (operation === "detail") {
      return (
        Number.isInteger(responseBody.id) &&
        typeof responseBody.name === "string" &&
        Array.isArray(responseBody.tags)
      );
    }
    if (operation === "report") {
      return (
        Array.isArray(responseBody.categories) &&
        Number.isInteger(responseBody.totalStock) &&
        Number.isInteger(responseBody.totalInventoryValueCents)
      );
    }
    if (operation === "events-report") {
      return hasEventReportSchema(responseBody);
    }
    if (operation === "quote") {
      return (
        Array.isArray(responseBody.items) &&
        ["subtotalCents", "discountCents", "taxCents", "totalCents"].every((field) =>
          Number.isInteger(responseBody[field]),
        )
      );
    }
    return hasEventReportSchema(responseBody) && /^[a-f0-9]{64}$/.test(responseBody.sha256);
  } catch (_) {
    return false;
  }
}

export default function () {
  const iteration = exec.scenario.iterationInTest;
  const operation = operationFor(iteration);
  const response = requestFor(operation, iteration);
  const passed = check(
    response,
    { [`${operation} response schema`]: (result) => hasExpectedSchema(operation, result) },
    { operation },
  );
  operationFailures.add(passed ? 0 : 1, { operation });
}
