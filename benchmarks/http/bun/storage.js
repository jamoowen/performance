import { Database } from "bun:sqlite";
import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";

const schemaVersion = "1";

export async function createStorage({ backend, dbPath, seedCount, categories, productFor }) {
  if (backend === "memory") {
    return createMemoryStorage({ seedCount, categories, productFor });
  }
  if (backend !== "sqlite") {
    throw new Error("BACKEND must be sqlite or memory");
  }
  await mkdir(dirname(dbPath), { recursive: true });
  const db = new Database(dbPath, { create: true, strict: true });
  configureDatabase(db);
  initializeDatabase(db, seedCount, productFor);
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
  return {
    backend: "sqlite",
    sqliteVersion: db.query("SELECT sqlite_version() AS version").get().version,
    product(id) {
      return apiProduct(statements.productById.get(id));
    },
    list(category, query, limit, offset) {
      return {
        total: statements.productCount.get(category, category, query, query).total,
        products: statements.productList
          .all(category, category, query, query, limit, offset)
          .map(apiProduct),
      };
    },
    catalog() {
      return statements.catalogReport.all();
    },
    events() {
      return statements.eventReport.all();
    },
    recordEvents(events) {
      db.transaction(() => {
        for (const event of events) {
          statements.eventUpsert.run(event.userId, event.type, event.value);
        }
      }).immediate();
    },
    close() {
      db.close();
    },
  };
}

function createMemoryStorage({ seedCount, categories, productFor }) {
  const totalsByUser = new Map();
  const seededProducts = Array.from({ length: seedCount }, (_, index) => productFor(index + 1));
  const matchingProducts = (category, query) => {
    const lowered = query.toLowerCase();
    const products = [];
    for (const product of seededProducts) {
      if (
        (!category || product.category === category) &&
        (!lowered || product.name.toLowerCase().includes(lowered))
      ) {
        products.push(product);
      }
    }
    return products;
  };
  return {
    backend: "memory",
    product(id) {
      return id >= 1 && id <= seedCount ? seededProducts[id - 1] : null;
    },
    list(category, query, limit, offset) {
      const products = matchingProducts(category, query);
      return { total: products.length, products: products.slice(offset, offset + limit) };
    },
    catalog() {
      const reports = categories.map((category) => ({
        category,
        count: 0,
        stock: 0,
        inventoryValueCents: 0,
      }));
      const indexByCategory = new Map(categories.map((category, index) => [category, index]));
      for (const product of seededProducts) {
        const report = reports[indexByCategory.get(product.category)];
        report.count += 1;
        report.stock += product.stock;
        report.inventoryValueCents += product.stock * product.priceCents;
      }
      return reports;
    },
    events() {
      const byType = new Map();
      for (const userTotals of totalsByUser.values()) {
        for (const [type, value] of userTotals) {
          const total = byType.get(type) || { count: 0, value: 0 };
          total.count += value.count;
          total.value += value.value;
          byType.set(type, total);
        }
      }
      return [...byType.entries()].map(([type, value]) => ({
        type,
        count: value.count,
        value: value.value,
      }));
    },
    // JavaScript executes this synchronous operation without interleaving handlers.
    recordEvents(events) {
      for (const event of events) {
        let userTotals = totalsByUser.get(event.userId);
        if (!userTotals) {
          userTotals = new Map();
          totalsByUser.set(event.userId, userTotals);
        }
        const total = userTotals.get(event.type) || { count: 0, value: 0 };
        total.count += 1;
        total.value += event.value;
        userTotals.set(event.type, total);
      }
    },
    close() {},
  };
}

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

function configureDatabase(db) {
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

function initializeDatabase(db, seedCount, productFor) {
  if (db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'").get()) {
    const version = db.query("SELECT value FROM metadata WHERE key='schema_version'").get();
    const seed = db.query("SELECT value FROM metadata WHERE key='seed_count'").get();
    if (!version || !seed || version.value !== schemaVersion || seed.value !== String(seedCount)) {
      throw new Error(
        `database metadata does not match schema version ${schemaVersion} and SEED_COUNT ${seedCount}`,
      );
    }
    return;
  }
  db.transaction(() => {
    db.exec(
      `CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, tags TEXT NOT NULL); CREATE INDEX products_category_id ON products(category, id); CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL); CREATE TABLE event_totals(user_id INTEGER NOT NULL REFERENCES users(id), type TEXT NOT NULL, count INTEGER NOT NULL CHECK(count >= 0), value_total INTEGER NOT NULL CHECK(value_total >= 0), PRIMARY KEY(user_id, type));`,
    );
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
  }).immediate();
}
