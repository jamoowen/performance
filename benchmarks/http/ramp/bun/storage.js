import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

const categories = ["books", "electronics", "home", "outdoors", "clothing"];

function productFrom(row) {
  return {
    id: row.id,
    name: row.name,
    category: row.category,
    priceCents: row.price_cents,
    stock: row.stock,
    revision: row.revision,
  };
}

export class SQLiteStore {
  constructor(path, seedCount) {
    mkdirSync(dirname(path), { recursive: true });
    this.seedCount = seedCount;
    this.database = new Database(path, { create: true, strict: true });
    for (const setting of [
      "journal_mode=WAL",
      "synchronous=NORMAL",
      "foreign_keys=ON",
      "busy_timeout=5000",
      "cache_size=-2000",
      "wal_autocheckpoint=1000",
      "temp_store=MEMORY",
    ]) {
      this.database.exec(`PRAGMA ${setting}`);
    }
    this.initialize();
    this.pragmas = {
      journal_mode: String(this.database.query("PRAGMA journal_mode").get().journal_mode),
      synchronous: Number(this.database.query("PRAGMA synchronous").get().synchronous),
      foreign_keys: Number(this.database.query("PRAGMA foreign_keys").get().foreign_keys),
      busy_timeout: Number(this.database.query("PRAGMA busy_timeout").get().timeout),
      cache_size: Number(this.database.query("PRAGMA cache_size").get().cache_size),
      wal_autocheckpoint: Number(
        this.database.query("PRAGMA wal_autocheckpoint").get().wal_autocheckpoint,
      ),
      temp_store: Number(this.database.query("PRAGMA temp_store").get().temp_store),
    };
    this.compileOptions = this.database
      .query("PRAGMA compile_options")
      .all()
      .map((row) => String(row.compile_options));
    this.sqliteVersion = String(
      this.database.query("SELECT sqlite_version() AS version").get().version,
    );
    this.detailStatement =
      this.database.query(`SELECT id, name, category, price_cents, stock, revision
      FROM products WHERE id = ?`);
    this.listStatement =
      this.database.query(`SELECT id, name, category, price_cents, stock, revision
      FROM products ORDER BY id LIMIT ? OFFSET ?`);
    this.stockStatement =
      this.database.query(`UPDATE products SET stock = stock + ?, revision = revision + 1
      WHERE id = ? RETURNING id, stock, revision`);
    this.integrityStatement =
      this.database.query(`SELECT COUNT(*) AS rows, COALESCE(SUM(stock), 0) AS totalStock,
      COALESCE(SUM(revision), 0) AS totalRevisions FROM products`);
  }

  product(id) {
    const row = this.detailStatement.get(id);
    return row ? productFrom(row) : null;
  }

  list(limit, offset) {
    return this.listStatement.all(limit, offset).map(productFrom);
  }

  stock(id, delta) {
    return this.stockStatement.get(delta, id) ?? null;
  }

  integrity() {
    return this.integrityStatement.get();
  }

  initialize() {
    const existing = this.database
      .query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'products'")
      .get();
    if (existing) {
      const count = Number(
        this.database.query("SELECT COUNT(*) AS count FROM products").get().count,
      );
      if (count !== this.seedCount) {
        throw new Error(`existing database has ${count} products, expected ${this.seedCount}`);
      }
      return;
    }
    this.database.exec(`CREATE TABLE products (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      category TEXT NOT NULL,
      price_cents INTEGER NOT NULL,
      stock INTEGER NOT NULL,
      revision INTEGER NOT NULL DEFAULT 0
    )`);
    const insert = this.database.query(
      "INSERT INTO products(id, name, category, price_cents, stock, revision) VALUES (?, ?, ?, ?, ?, 0)",
    );
    this.database.exec("BEGIN IMMEDIATE");
    try {
      for (let id = 1; id <= this.seedCount; id += 1) {
        insert.run(
          id,
          `Product${String(id).padStart(5, "0")}`,
          categories[(id - 1) % categories.length],
          500 + ((id * 7919) % 50000),
          (id * 37) % 201,
        );
      }
      this.database.exec("COMMIT");
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }
}
