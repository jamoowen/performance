import { Elysia } from "elysia";
import { Hono } from "hono";

import { SQLiteStore } from "./storage.js";

const MAX_BODY_BYTES = 65_536;
const framework = process.env.FRAMEWORK;
const validFrameworks = ["native", "hono", "elysia"];
if (!framework || !validFrameworks.includes(framework)) {
  throw new Error(`FRAMEWORK must be one of ${validFrameworks.join(", ")}`);
}
const port = integerEnvironment("PORT", 8080, 1, 65_535);
const seedCount = integerEnvironment("SEED_COUNT", 5000, 100, 100_000);
const store = new SQLiteStore(process.env.SQLITE_PATH ?? "/data/benchmark.sqlite", seedCount);
const frameworkVersions = { native: Bun.version, hono: "4.13.12", elysia: "1.4.30" };
const metadata = {
  experiment: "sqlite-ramp-v2",
  runtime: "bun",
  framework,
  runtimeVersion: Bun.version,
  frameworkVersion: frameworkVersions[framework],
  driver: "bun:sqlite",
  driverVersion: Bun.version,
  sqliteVersion: store.sqliteVersion,
  seedCount,
  workers: 1,
  pragmas: store.pragmas,
  compileOptions: store.compileOptions,
};

function integerEnvironment(name, fallback, minimum, maximum) {
  const raw = process.env[name];
  if (!raw) {
    return fallback;
  }
  if (!/^[0-9]+$/.test(raw)) {
    throw new Error(`${name} must be an integer`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  }
  return value;
}

function response(status, value, timing) {
  const headers = { "content-type": "application/json" };
  if (timing) {
    headers["server-timing"] = timing;
  }
  return new Response(JSON.stringify(value), { status, headers });
}

function error(status, message) {
  return response(status, { error: message });
}

function timed(operation) {
  const serviceStart = performance.now();
  const dbStart = performance.now();
  const value = operation();
  const dbEnd = performance.now();
  const serialized = JSON.stringify(value);
  const service = performance.now() - serviceStart;
  const db = dbEnd - dbStart;
  return new Response(serialized, {
    status: 200,
    headers: {
      "content-type": "application/json",
      "server-timing": `service;dur=${service.toFixed(3)}, db;dur=${db.toFixed(3)}`,
    },
  });
}

function parseId(raw) {
  if (!/^[0-9]+$/.test(raw)) {
    return null;
  }
  const id = Number(raw);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

function parseList(url) {
  const keys = [...url.searchParams.keys()];
  if (keys.some((key) => key !== "offset" && key !== "limit")) {
    return null;
  }
  if (new Set(keys).size !== keys.length) {
    return null;
  }
  const offset = parseUnsigned(url.searchParams.get("offset"), 0, 0, seedCount);
  const limit = parseUnsigned(url.searchParams.get("limit"), 20, 1, 100);
  return offset === null || limit === null ? null : { offset, limit };
}

function parseUnsigned(raw, fallback, minimum, maximum) {
  if (raw === null) {
    return fallback;
  }
  if (!/^[0-9]+$/.test(raw)) {
    return null;
  }
  const value = Number(raw);
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum ? value : null;
}

function parseDelta(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const keys = Object.keys(value);
  if (keys.length !== 1 || keys[0] !== "delta") {
    return null;
  }
  const delta = value.delta;
  return typeof delta === "number" && Number.isSafeInteger(delta) && delta >= -100 && delta <= 100
    ? delta
    : null;
}

const integerDeltaJson = /^\s*\{\s*"delta"\s*:\s*-?(?:0|[1-9][0-9]*)\s*\}\s*$/;

function parseDeltaBody(raw, value) {
  return integerDeltaJson.test(raw) ? parseDelta(value) : null;
}

function contentTypeIsJson(request) {
  return (
    request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() ===
    "application/json"
  );
}

async function parseJson(request) {
  if (!contentTypeIsJson(request)) {
    return { error: error(415, "content-type must be application/json") };
  }
  const contentLength = request.headers.get("content-length");
  if (contentLength && /^[0-9]+$/.test(contentLength) && Number(contentLength) > MAX_BODY_BYTES) {
    return { error: error(413, "request body too large") };
  }
  const bytes = await boundedBody(request);
  if (!bytes) {
    return { error: error(413, "request body too large") };
  }
  const raw = new TextDecoder().decode(bytes);
  try {
    return { raw, value: JSON.parse(raw) };
  } catch {
    return { error: error(400, "invalid JSON body") };
  }
}

async function boundedBody(request) {
  const reader = request.body?.getReader();
  if (!reader) {
    return new Uint8Array();
  }
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      length += value.byteLength;
      if (length > MAX_BODY_BYTES) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function detail(rawId) {
  const id = parseId(rawId);
  if (id === null) {
    return error(400, "id must be a positive integer");
  }
  return timed(() => {
    const product = store.product(id);
    if (!product) {
      throw new NotFoundError();
    }
    return product;
  });
}

class NotFoundError extends Error {}

function detailResponse(rawId) {
  try {
    return detail(rawId);
  } catch (exception) {
    if (exception instanceof NotFoundError) {
      return error(404, "product not found");
    }
    throw exception;
  }
}

function listResponse(request) {
  const parameters = parseList(new URL(request.url));
  if (!parameters) {
    return error(400, "invalid query parameters");
  }
  return timed(() => ({
    products: store.list(parameters.limit, parameters.offset),
    total: seedCount,
    offset: parameters.offset,
    limit: parameters.limit,
  }));
}

async function stockResponse(rawId, request) {
  const id = parseId(rawId);
  if (id === null) {
    return error(400, "id must be a positive integer");
  }
  const parsed = await parseJson(request);
  if (parsed.error) {
    return parsed.error;
  }
  const delta = parseDeltaBody(parsed.raw, parsed.value);
  if (delta === null) {
    return error(400, "body must be exactly an object with integer delta");
  }
  try {
    return timed(() => {
      const result = store.stock(id, delta);
      if (!result) {
        throw new NotFoundError();
      }
      return result;
    });
  } catch (exception) {
    if (exception instanceof NotFoundError) {
      return error(404, "product not found");
    }
    throw exception;
  }
}

function staticResponse(pathname) {
  if (pathname === "/healthz") {
    return response(200, { status: "ok" });
  }
  if (pathname === "/benchmark/info") {
    return response(200, metadata);
  }
  if (pathname === "/benchmark/integrity") {
    return response(200, store.integrity());
  }
  return null;
}

async function nativeFetch(request) {
  const url = new URL(request.url);
  const staticValue = staticResponse(url.pathname);
  if (request.method === "GET" && staticValue) {
    return staticValue;
  }
  if (request.method === "GET" && url.pathname === "/products") {
    return listResponse(request);
  }
  const productMatch = url.pathname.match(/^\/products\/([^/]+)$/);
  if (request.method === "GET" && productMatch) {
    return detailResponse(productMatch[1]);
  }
  const stockMatch = url.pathname.match(/^\/products\/([^/]+)\/stock$/);
  if (request.method === "POST" && stockMatch) {
    return stockResponse(stockMatch[1], request);
  }
  return error(404, "not found");
}

function startHono() {
  const app = new Hono();
  app.get("/healthz", () => response(200, { status: "ok" }));
  app.get("/benchmark/info", () => response(200, metadata));
  app.get("/benchmark/integrity", () => response(200, store.integrity()));
  app.get("/products", (context) => listResponse(context.req.raw));
  app.get("/products/:id", (context) => detailResponse(context.req.param("id")));
  app.post("/products/:id/stock", (context) =>
    stockResponse(context.req.param("id"), context.req.raw),
  );
  Bun.serve({ hostname: "0.0.0.0", port, fetch: app.fetch });
}

function startElysia() {
  const app = new Elysia({ serve: { hostname: "0.0.0.0", port } });
  app.get("/healthz", () => response(200, { status: "ok" }));
  app.get("/benchmark/info", () => response(200, metadata));
  app.get("/benchmark/integrity", () => response(200, store.integrity()));
  app.get("/products", ({ request }) => listResponse(request));
  app.get("/products/:id", ({ params }) => detailResponse(params.id));
  app.post("/products/:id/stock", ({ params, request }) => stockResponse(params.id, request), {
    parse: "none",
  });
  app.listen(port);
}

if (framework === "native") {
  Bun.serve({ hostname: "0.0.0.0", port, fetch: nativeFetch });
} else if (framework === "hono") {
  startHono();
} else {
  startElysia();
}

console.error(`sqlite-ramp bun ${framework} listening on ${port}`);
