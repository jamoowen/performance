import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";

export interface Product {
  id: number;
  name: string;
  category: string;
  priceCents: number;
  stock: number;
  revision: number;
}

export interface StockResult {
  id: number;
  stock: number;
  revision: number;
}

interface ProductRow {
  id: number;
  name: string;
  category: string;
  price_cents: number;
  stock: number;
  revision: number;
}

const categories = ["books", "electronics", "home", "outdoors", "clothing"];

function productFrom(row: ProductRow): Product {
  return {
    id: row.id,
    name: row.name,
    category: row.category,
    priceCents: row.price_cents,
    stock: row.stock,
    revision: row.revision,
  };
}

function requiredRow(row: Record<string, unknown> | undefined): Record<string, unknown> {
  if (!row) {
    throw new Error("SQLite pragma query returned no row");
  }
  return row;
}

export class SQLiteStore {
  readonly pragmas: Record<string, string | number>;
  readonly compileOptions: string[];
  readonly sqliteVersion: string;
  private readonly detailStatement;
  private readonly listStatement;
  private readonly stockStatement;
  private readonly integrityStatement;

  constructor(
    readonly path: string,
    readonly seedCount: number,
  ) {
    mkdirSync(dirname(path), { recursive: true });
    const database = new DatabaseSync(path, { enableForeignKeyConstraints: true });
    for (const setting of [
      "journal_mode=WAL",
      "synchronous=NORMAL",
      "foreign_keys=ON",
      "busy_timeout=5000",
      "cache_size=-2000",
      "wal_autocheckpoint=1000",
      "temp_store=MEMORY",
    ]) {
      database.exec(`PRAGMA ${setting}`);
    }
    this.initialize(database);
    this.pragmas = {
      journal_mode: String(requiredRow(database.prepare("PRAGMA journal_mode").get()).journal_mode),
      synchronous: Number(requiredRow(database.prepare("PRAGMA synchronous").get()).synchronous),
      foreign_keys: Number(requiredRow(database.prepare("PRAGMA foreign_keys").get()).foreign_keys),
      busy_timeout: Number(requiredRow(database.prepare("PRAGMA busy_timeout").get()).timeout),
      cache_size: Number(requiredRow(database.prepare("PRAGMA cache_size").get()).cache_size),
      wal_autocheckpoint: Number(
        requiredRow(database.prepare("PRAGMA wal_autocheckpoint").get()).wal_autocheckpoint,
      ),
      temp_store: Number(requiredRow(database.prepare("PRAGMA temp_store").get()).temp_store),
    };
    this.compileOptions = (
      database.prepare("PRAGMA compile_options").all() as Array<{ compile_options: unknown }>
    ).map((row) => String(row.compile_options));
    this.sqliteVersion = String(
      requiredRow(database.prepare("SELECT sqlite_version() AS version").get()).version,
    );
    this.detailStatement = database.prepare(`SELECT id, name, category, price_cents, stock, revision
      FROM products WHERE id = ?`);
    this.listStatement = database.prepare(`SELECT id, name, category, price_cents, stock, revision
      FROM products ORDER BY id LIMIT ? OFFSET ?`);
    this.stockStatement =
      database.prepare(`UPDATE products SET stock = stock + ?, revision = revision + 1
      WHERE id = ? RETURNING id, stock, revision`);
    this.integrityStatement =
      database.prepare(`SELECT COUNT(*) AS rows, COALESCE(SUM(stock), 0) AS totalStock,
      COALESCE(SUM(revision), 0) AS totalRevisions FROM products`);
  }

  product(id: number): Product | null {
    const row = this.detailStatement.get(id) as ProductRow | undefined;
    return row ? productFrom(row) : null;
  }

  list(limit: number, offset: number): Product[] {
    return (this.listStatement.all(limit, offset) as unknown as ProductRow[]).map(productFrom);
  }

  stock(id: number, delta: number): StockResult | null {
    return (this.stockStatement.get(delta, id) as StockResult | undefined) ?? null;
  }

  integrity(): { rows: number; totalStock: number; totalRevisions: number } {
    return this.integrityStatement.get() as {
      rows: number;
      totalStock: number;
      totalRevisions: number;
    };
  }

  private initialize(database: DatabaseSync): void {
    const table = database
      .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'products'")
      .get();
    if (table) {
      const count = Number(
        requiredRow(database.prepare("SELECT COUNT(*) AS count FROM products").get()).count,
      );
      if (count !== this.seedCount) {
        throw new Error(`existing database has ${count} products, expected ${this.seedCount}`);
      }
      return;
    }
    database.exec(`CREATE TABLE products (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      category TEXT NOT NULL,
      price_cents INTEGER NOT NULL,
      stock INTEGER NOT NULL,
      revision INTEGER NOT NULL DEFAULT 0
    )`);
    const insert = database.prepare(
      "INSERT INTO products(id, name, category, price_cents, stock, revision) VALUES (?, ?, ?, ?, ?, 0)",
    );
    database.exec("BEGIN IMMEDIATE");
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
      database.exec("COMMIT");
    } catch (error) {
      database.exec("ROLLBACK");
      throw error;
    }
  }
}
