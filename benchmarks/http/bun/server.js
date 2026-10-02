import { createHash } from "node:crypto";
import { diagnosticsEnabled, startDiagnostics } from "./diagnostics.js";
import { createStorage } from "./storage.js";

const categories = ["books", "electronics", "home", "sports", "toys"];
const maxBodyBytes = 1024 * 1024;
const seedCount = envNumber("SEED_COUNT", 5000, 100000);
const port = envNumber("PORT", 8080, 65535);
const dbPath = process.env.DB_PATH || "./data/benchmark.sqlite";
const backend = process.env.BACKEND || "sqlite";
const router = process.env.ROUTER || "stdlib";
if (!["sqlite", "memory"].includes(backend)) {
  throw new Error("BACKEND must be sqlite or memory");
}
if (!["stdlib", "elysia"].includes(router)) {
  throw new Error("ROUTER must be stdlib or elysia");
}
const { Elysia } = router === "elysia" ? await import("elysia") : {};

function envNumber(name, fallback, maximum) {
  const value = process.env[name];
  if (!value) {
    return fallback;
  }
  if (!/^[1-9][0-9]*$/.test(value) || Number(value) > maximum) {
    throw new Error(`${name} must be an integer between 1 and ${maximum}`);
  }
  return Number(value);
}

function productFor(id) {
  const category = categories[(id - 1) % categories.length];
  return {
    id,
    name: `Product ${String(id).padStart(5, "0")}`,
    category,
    priceCents: 500 + ((id * 7919) % 50000),
    stock: (id * 37) % 201,
    tags: [category, id % 3 === 0 ? "featured" : "standard", id % 2 === 0 ? "even" : "odd"],
  };
}
const store = await createStorage({ backend, dbPath, seedCount, categories, productFor });
const diagnostics = diagnosticsEnabled() ? startDiagnostics() : null;
console.error(
  backend === "sqlite"
    ? `runtime=bun-${Bun.version} backend=sqlite sqlite_version=${store.sqliteVersion} seed_count=${seedCount} db_path=${dbPath} max_open_conns=1`
    : `runtime=bun-${Bun.version} backend=memory seed_count=${seedCount}`,
);
function json(body, status = 200, headers = {}) {
  return Response.json(body, { status, headers });
}
function fail(status, message, headers) {
  return json({ error: message }, status, headers);
}
function integer(value, minimum, maximum) {
  return (
    typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum
  );
}
function decimalParameter(value, fallback, maximum) {
  if (value === null || value === "") {
    return fallback;
  }
  if (!/^[0-9]+$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed <= maximum ? parsed : null;
}

async function readJsonBody(request) {
  if (
    request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !==
    "application/json"
  ) {
    return { error: 415 };
  }
  const contentLength = request.headers.get("content-length");
  if (contentLength && /^[0-9]+$/.test(contentLength) && Number(contentLength) > maxBodyBytes) {
    return { error: 413 };
  }
  const reader = request.body?.getReader();
  if (!reader) {
    return { error: 400 };
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
      if (length > maxBodyBytes) {
        await reader.cancel();
        return { error: 413 };
      }
      chunks.push(value);
    }
  } catch {
    return { error: length > maxBodyBytes ? 413 : 400 };
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return { value: JSON.parse(new TextDecoder().decode(bytes)) };
  } catch {
    return { error: 400 };
  }
}

function inputError(status, message) {
  if (status === 415) {
    return fail(415, "content-type must be application/json");
  }
  if (status === 413) {
    return fail(413, "request body too large");
  }
  return fail(400, message);
}

function listProducts(url) {
  const category = url.searchParams.get("category") ?? "";
  const query = url.searchParams.get("q") ?? "";
  let offset = decimalParameter(url.searchParams.get("offset"), 0, 1_000_000_000);
  const limit = decimalParameter(url.searchParams.get("limit"), 20, 100);
  if (category && !categories.includes(category)) {
    return fail(400, "category is invalid");
  }
  if (offset === null) {
    return fail(400, "offset is invalid");
  }
  if (limit === null || limit < 1) {
    return fail(400, "limit is invalid");
  }
  const listed = store.list(category, query, limit, offset);
  const total = listed.total;
  offset = Math.min(offset, total);
  return json({
    products: listed.products,
    total,
    offset,
    limit,
  });
}

function getProduct(rawId) {
  if (!/^[0-9]+$/.test(rawId)) {
    return fail(400, "id must be a positive integer");
  }
  const id = Number(rawId);
  if (!Number.isSafeInteger(id) || id < 1 || id > 1_000_000_000) {
    return fail(400, "id must be a positive integer");
  }
  const product = store.product(id);
  return product ? json(product) : fail(404, "product not found");
}

function catalogReport() {
  const byCategory = new Map(store.catalog().map((row) => [row.category, row]));
  const reports = categories.map(
    (category) =>
      byCategory.get(category) || { category, count: 0, stock: 0, inventoryValueCents: 0 },
  );
  return json({
    categories: reports,
    totalStock: reports.reduce((total, report) => total + report.stock, 0),
    totalInventoryValueCents: reports.reduce(
      (total, report) => total + report.inventoryValueCents,
      0,
    ),
  });
}

function eventReport() {
  const counts = { view: 0, click: 0, purchase: 0 };
  const values = { view: 0, click: 0, purchase: 0 };
  for (const row of store.events()) {
    counts[row.type] = row.count;
    values[row.type] = row.value;
  }
  return json({ counts, values });
}

async function quote(request) {
  const parsed = await readJsonBody(request);
  if (parsed.error) {
    return inputError(parsed.error, "invalid quote request");
  }
  const value = parsed.value;
  if (!value || !Array.isArray(value.items) || value.items.length < 1 || value.items.length > 100) {
    return fail(400, "invalid quote request");
  }
  if ("coupon" in value && value.coupon !== null && typeof value.coupon !== "string") {
    return fail(400, "invalid quote request");
  }
  if (value.coupon !== undefined && value.coupon !== null && value.coupon !== "SAVE10") {
    return fail(400, "coupon is invalid");
  }
  const requested = new Map();
  const items = [];
  let subtotalCents = 0;
  for (const line of value.items) {
    if (!line || !integer(line.productId, 1, 1_000_000_000) || !integer(line.quantity, 1, 100)) {
      return fail(400, "quantity is invalid or unavailable");
    }
    const product = store.product(line.productId);
    if (!product) {
      return fail(404, "product not found");
    }
    const totalQuantity = (requested.get(line.productId) || 0) + line.quantity;
    if (totalQuantity > product.stock) {
      return fail(400, "quantity is invalid or unavailable");
    }
    requested.set(line.productId, totalQuantity);
    const lineTotalCents = product.priceCents * line.quantity;
    subtotalCents += lineTotalCents;
    items.push({
      productId: line.productId,
      quantity: line.quantity,
      unitPriceCents: product.priceCents,
      lineTotalCents,
    });
  }
  const discountCents = value.coupon === "SAVE10" ? Math.floor(subtotalCents / 10) : 0;
  const taxCents = Math.floor((subtotalCents - discountCents) / 5);
  return json({
    items,
    subtotalCents,
    discountCents,
    taxCents,
    totalCents: subtotalCents - discountCents + taxCents,
  });
}

async function recordEvents(request) {
  const parsed = await readJsonBody(request);
  if (parsed.error) {
    return inputError(parsed.error, "invalid events request");
  }
  const value = parsed.value;
  if (
    !value ||
    !Array.isArray(value.events) ||
    value.events.length < 1 ||
    value.events.length > 100
  ) {
    return fail(400, "invalid events request");
  }
  const counts = { view: 0, click: 0, purchase: 0 };
  const values = { view: 0, click: 0, purchase: 0 };
  let canonical = "";
  for (const event of value.events) {
    if (
      !event ||
      !integer(event.userId, 1, seedCount) ||
      !["view", "click", "purchase"].includes(event.type) ||
      !integer(event.value, 0, 1_000_000)
    ) {
      return fail(400, "event is invalid");
    }
    counts[event.type] += 1;
    values[event.type] += event.value;
    canonical += `${event.userId}:${event.type}:${event.value}\n`;
  }
  try {
    store.recordEvents(value.events);
  } catch (error) {
    return databaseFailure(error);
  }
  return json({ counts, values, sha256: createHash("sha256").update(canonical).digest("hex") });
}

function wrongMethod(method) {
  return fail(405, "method not allowed", { Allow: method });
}
const getRoutes = new Set([
  "/healthz",
  "/products",
  "/products/",
  "/reports/catalog",
  "/reports/events",
]);
const postRoutes = new Set(["/cart/quote", "/events/batch"]);
function fallback(request) {
  const path = new URL(request.url).pathname;
  if (getRoutes.has(path) || /^\/products\/[^/]+$/.test(path)) {
    return wrongMethod("GET, HEAD");
  }
  if (postRoutes.has(path)) {
    return wrongMethod("POST");
  }
  return fail(404, "not found");
}

function databaseFailure(error) {
  console.error(error);
  const message = String(error?.message || error);
  if (
    error?.code === "SQLITE_BUSY" ||
    error?.code === "SQLITE_LOCKED" ||
    /SQLITE_(BUSY|LOCKED)/.test(message)
  ) {
    return fail(503, "database busy");
  }
  return fail(500, "database error");
}

const stdlibServer =
  router === "stdlib"
    ? Bun.serve({
        hostname: "0.0.0.0",
        port,
        reusePort: process.env.REUSE_PORT === "1",
        idleTimeout: 60,
        maxRequestBodySize: 16 * 1024 * 1024,
        development: false,
        routes: {
          "/healthz": { GET: () => json({ status: "ok" }), HEAD: () => json({ status: "ok" }) },
          "/products": {
            GET: (request) => listProducts(new URL(request.url)),
            HEAD: (request) => listProducts(new URL(request.url)),
          },
          "/products/:id": {
            GET: (request) => getProduct(request.params.id),
            HEAD: (request) => getProduct(request.params.id),
          },
          "/products/": {
            GET: () => fail(400, "id must be a positive integer"),
            HEAD: () => fail(400, "id must be a positive integer"),
          },
          "/reports/catalog": { GET: catalogReport, HEAD: catalogReport },
          "/reports/events": { GET: eventReport, HEAD: eventReport },
          "/cart/quote": { POST: quote },
          "/events/batch": { POST: recordEvents },
        },
        error(error) {
          return databaseFailure(error);
        },
        fetch(request) {
          return fallback(request);
        },
      })
    : null;

function buildElysia() {
  const health = () => json({ status: "ok" });
  const route = new Elysia()
    .get("/healthz", health)
    .head("/healthz", health)
    .get("/products", ({ request }) => listProducts(new URL(request.url)))
    .head("/products", ({ request }) => listProducts(new URL(request.url)))
    .get("/products/:id", ({ params }) => getProduct(params.id))
    .head("/products/:id", ({ params }) => getProduct(params.id))
    .get("/products/", () => fail(400, "id must be a positive integer"))
    .head("/products/", () => fail(400, "id must be a positive integer"))
    .get("/reports/catalog", catalogReport)
    .head("/reports/catalog", catalogReport)
    .get("/reports/events", eventReport)
    .head("/reports/events", eventReport)
    // No body schema is registered: quote and event handlers retain the same bounded manual reader.
    .post("/cart/quote", ({ request }) => quote(request), { parse: "none" })
    .post("/events/batch", ({ request }) => recordEvents(request), { parse: "none" })
    .all("/*", ({ request }) => fallback(request))
    .onError(({ error }) => databaseFailure(error));
  route.listen({
    hostname: "0.0.0.0",
    port,
    reusePort: process.env.REUSE_PORT === "1",
    idleTimeout: 60,
    maxRequestBodySize: 16 * 1024 * 1024,
    development: false,
  });
  return route;
}

const elysiaApp = router === "elysia" ? buildElysia() : null;
const server = elysiaApp?.server || stdlibServer;

let shuttingDown = false;
async function stop() {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  const forceStop = setTimeout(() => server.stop(true), 10_000);
  try {
    if (elysiaApp) {
      await elysiaApp.stop(false);
    } else {
      await server.stop(false);
    }
  } finally {
    clearTimeout(forceStop);
    diagnostics?.stop();
    store.close();
  }
}
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
