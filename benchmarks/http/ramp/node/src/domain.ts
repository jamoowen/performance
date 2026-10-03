import { performance } from "node:perf_hooks";

import type { Product, SQLiteStore, StockResult } from "./store.js";

export const MAX_BODY_BYTES = 65_536;

export interface EncodedResponse {
  status: number;
  body: Buffer;
  timing?: string;
}

function encode(value: unknown): Buffer {
  return Buffer.from(JSON.stringify(value), "utf8");
}

export function error(status: number, message: string): EncodedResponse {
  return { status, body: encode({ error: message }) };
}

function successful(
  value: unknown,
  serviceStart: number,
  dbStart: number,
  dbEnd: number,
): EncodedResponse {
  const body = encode(value);
  const service = performance.now() - serviceStart;
  const db = dbEnd - dbStart;
  return {
    status: 200,
    body,
    timing: `service;dur=${service.toFixed(3)}, db;dur=${db.toFixed(3)}`,
  };
}

export function parseId(raw: string): number | null {
  if (!/^[0-9]+$/.test(raw)) {
    return null;
  }
  const id = Number(raw);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

function parseInteger(
  raw: string | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number | null {
  if (raw === undefined) {
    return fallback;
  }
  if (!/^[0-9]+$/.test(raw)) {
    return null;
  }
  const value = Number(raw);
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum ? value : null;
}

export function parseListQuery(
  query: Record<string, unknown>,
  seedCount: number,
): { offset: number; limit: number } | null {
  const keys = Object.keys(query);
  if (keys.some((key) => key !== "offset" && key !== "limit")) {
    return null;
  }
  if (keys.some((key) => Array.isArray(query[key]))) {
    return null;
  }
  const offset = parseInteger(
    typeof query.offset === "string" ? query.offset : undefined,
    0,
    0,
    seedCount,
  );
  const limit = parseInteger(typeof query.limit === "string" ? query.limit : undefined, 20, 1, 100);
  return offset === null || limit === null ? null : { offset, limit };
}

export function parseDelta(value: unknown): number | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length !== 1 || !("delta" in record)) {
    return null;
  }
  const delta = record.delta;
  return typeof delta === "number" && Number.isSafeInteger(delta) && delta >= -100 && delta <= 100
    ? delta
    : null;
}

const integerDeltaJson = /^\s*\{\s*"delta"\s*:\s*-?(?:0|[1-9][0-9]*)\s*\}\s*$/;

export function parseDeltaBody(raw: Buffer | undefined, value: unknown): number | null {
  if (!raw || !integerDeltaJson.test(raw.toString("utf8"))) {
    return null;
  }
  return parseDelta(value);
}

export class Domain {
  constructor(private readonly store: SQLiteStore) {}

  detail(id: number): EncodedResponse {
    const serviceStart = performance.now();
    const dbStart = performance.now();
    const product = this.store.product(id);
    const dbEnd = performance.now();
    return product
      ? successful(product, serviceStart, dbStart, dbEnd)
      : error(404, "product not found");
  }

  list(offset: number, limit: number): EncodedResponse {
    const serviceStart = performance.now();
    const dbStart = performance.now();
    const products = this.store.list(limit, offset);
    const dbEnd = performance.now();
    return successful(
      { products, total: this.store.seedCount, offset, limit },
      serviceStart,
      dbStart,
      dbEnd,
    );
  }

  stock(id: number, delta: number): EncodedResponse {
    const serviceStart = performance.now();
    const dbStart = performance.now();
    const result = this.store.stock(id, delta);
    const dbEnd = performance.now();
    return result
      ? successful(result, serviceStart, dbStart, dbEnd)
      : error(404, "product not found");
  }

  health(): EncodedResponse {
    return { status: 200, body: encode({ status: "ok" }) };
  }

  info(metadata: Record<string, unknown>): EncodedResponse {
    return { status: 200, body: encode(metadata) };
  }

  integrity(): EncodedResponse {
    return { status: 200, body: encode(this.store.integrity()) };
  }
}

export type { Product, StockResult };
