import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";

const categories = ["books", "electronics", "home", "sports", "toys"];
const maxBodyBytes = 1024 * 1024;
const seedCount = envNumber("SEED_COUNT", 5000, 100000);
const port = envNumber("PORT", 8080, 65535);
const dbPath = process.env.DB_PATH || "./data/benchmark.sqlite";
await mkdir(dirname(dbPath), { recursive: true });
const db = new Database(dbPath, { create: true, strict: true });

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

function configureDatabase() {
  for (const setting of [
    "journal_mode=WAL",
    "synchronous=NORMAL",
    "foreign_keys=ON",
    "busy_timeout=5000",
    "cache_size=-2000",
    "wal_autocheckpoint=1000",
    "temp_store=MEMORY",
  ]) {
    db.exec(`PRAGMA ${setting}`);
  }
  if (db.query("PRAGMA journal_mode").get().journal_mode !== "wal") {
    throw new Error("WAL mode was not enabled");
  }
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

function verifyDatabaseMetadata() {
  const version = db.query("SELECT value FROM metadata WHERE key='schema_version'").get();
  const seed = db.query("SELECT value FROM metadata WHERE key='seed_count'").get();
  if (!version || !seed || version.value !== "1" || seed.value !== String(seedCount)) {
    throw new Error(
      `database metadata does not match schema version 1 and SEED_COUNT ${seedCount}`,
    );
  }
}

function seedDatabase() {
  db.exec(`
    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE products(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
      price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, tags TEXT NOT NULL
    );
    CREATE INDEX products_category_id ON products(category, id);
    CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE event_totals(
      user_id INTEGER NOT NULL REFERENCES users(id), type TEXT NOT NULL,
      count INTEGER NOT NULL CHECK(count >= 0), value_total INTEGER NOT NULL CHECK(value_total >= 0),
      PRIMARY KEY(user_id, type)
    );
  `);
  const insertProduct = db.query("INSERT INTO products VALUES(?,?,?,?,?,?)");
  const insertUser = db.query("INSERT INTO users VALUES(?,?)");
  for (let id = 1; id <= seedCount; id += 1) {
    const product = productFor(id);
    insertProduct.run(
      product.id,
      product.name,
      product.category,
      product.priceCents,
      product.stock,
      JSON.stringify(product.tags),
    );
    insertUser.run(id, `User ${String(id).padStart(5, "0")}`);
  }
  db.query("INSERT INTO metadata VALUES('schema_version','1'),('seed_count',?)").run(
    String(seedCount),
  );
}

function initializeDatabase() {
  if (db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'").get()) {
    return verifyDatabaseMetadata();
  }
  db.transaction(seedDatabase).immediate();
}

configureDatabase();
initializeDatabase();
console.error(
  `runtime=bun-${Bun.version} sqlite_version=${db.query("SELECT sqlite_version() AS version").get().version} seed_count=${seedCount} db_path=${dbPath} max_open_conns=1`,
);

const statements = {
  productById: db.query(`SELECT id, name, category, price_cents, stock, tags
    FROM products WHERE id = ?`),
  productCount: db.query(`SELECT COUNT(*) AS total FROM products
    WHERE (? = '' OR category = ?)
      AND (? = '' OR instr(lower(name), lower(?)) > 0)`),
  productList: db.query(`SELECT id, name, category, price_cents, stock, tags
    FROM products
    WHERE (? = '' OR category = ?)
      AND (? = '' OR instr(lower(name), lower(?)) > 0)
    ORDER BY id LIMIT ? OFFSET ?`),
  catalogReport: db.query(`SELECT category, COUNT(*) AS count, COALESCE(SUM(stock), 0) AS stock,
    COALESCE(SUM(stock * price_cents), 0) AS inventoryValueCents
    FROM products GROUP BY category`),
  eventReport: db.query(`SELECT type, COALESCE(SUM(count), 0) AS count,
    COALESCE(SUM(value_total), 0) AS value FROM event_totals GROUP BY type`),
  eventUpsert: db.query(`INSERT INTO event_totals(user_id, type, count, value_total)
    VALUES (?, ?, 1, ?)
    ON CONFLICT(user_id, type) DO UPDATE SET
      count = count + 1,
      value_total = value_total + excluded.value_total`),
};

function apiProduct(row) {
  return (
    row && {
      id: row.id,
      name: row.name,
      category: row.category,
      priceCents: row.price_cents,
      stock: row.stock,
      tags: JSON.parse(row.tags),
    }
  );
}
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
  const total = statements.productCount.get(category, category, query, query).total;
  offset = Math.min(offset, total);
  return json({
    products: statements.productList
      .all(category, category, query, query, limit, offset)
      .map(apiProduct),
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
  const product = apiProduct(statements.productById.get(id));
  return product ? json(product) : fail(404, "product not found");
}

function catalogReport() {
  const byCategory = new Map(statements.catalogReport.all().map((row) => [row.category, row]));
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
  for (const row of statements.eventReport.all()) {
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
    const product = apiProduct(statements.productById.get(line.productId));
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
  db.transaction(() => {
    for (const event of value.events) {
      statements.eventUpsert.run(event.userId, event.type, event.value);
    }
  }).immediate();
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

const server = Bun.serve({
  hostname: "0.0.0.0",
  port,
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
});

let shuttingDown = false;
async function stop() {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  const forceStop = setTimeout(() => server.stop(true), 10_000);
  try {
    await server.stop(false);
  } finally {
    clearTimeout(forceStop);
    db.close();
  }
}
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
