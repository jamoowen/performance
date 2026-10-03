import "reflect-metadata";

import { performance } from "node:perf_hooks";
import { Controller, Get, Module, Param, Post, Query, Req, Res } from "@nestjs/common";
import { NestFactory } from "@nestjs/core";
import express, { type NextFunction, type Request, type Response } from "express";
import Fastify, { type FastifyReply, type FastifyRequest } from "fastify";

import {
  Domain,
  type EncodedResponse,
  error,
  MAX_BODY_BYTES,
  parseDeltaBody,
  parseId,
  parseListQuery,
} from "./domain.js";
import { SQLiteStore } from "./store.js";

const framework = process.env.FRAMEWORK;
const validFrameworks = ["express", "nest", "fastify"];
if (!framework || !validFrameworks.includes(framework)) {
  throw new Error(`FRAMEWORK must be one of ${validFrameworks.join(", ")}`);
}

const port = integerEnvironment("PORT", 8080, 1, 65_535);
const seedCount = integerEnvironment("SEED_COUNT", 5000, 100, 100_000);
const sqlitePath = process.env.SQLITE_PATH ?? "/data/benchmark.sqlite";
const store = new SQLiteStore(sqlitePath, seedCount);
const domain = new Domain(store);
const frameworkVersions: Record<string, string> = {
  express: "5.2.1",
  nest: "12.1.2 (Express adapter 5.2.1)",
  fastify: "5.12.5",
};
const metadata = {
  experiment: "sqlite-ramp-v2",
  runtime: "node",
  framework,
  runtimeVersion: process.version,
  frameworkVersion: frameworkVersions[framework],
  driver: "node:sqlite DatabaseSync (release candidate)",
  driverVersion: process.version,
  sqliteVersion: store.sqliteVersion,
  seedCount,
  workers: 1,
  pragmas: store.pragmas,
  compileOptions: store.compileOptions,
};

declare global {
  namespace Express {
    interface Request {
      rawBody?: Buffer;
    }
  }
}

function integerEnvironment(
  name: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
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

function sendExpress(response: Response, encoded: EncodedResponse): void {
  response.status(encoded.status).type("application/json");
  if (encoded.timing) {
    response.setHeader("Server-Timing", encoded.timing);
  }
  response.send(encoded.body);
}

function sendFastify(response: FastifyReply, encoded: EncodedResponse): void {
  response.code(encoded.status).type("application/json");
  if (encoded.timing) {
    response.header("Server-Timing", encoded.timing);
  }
  response.send(encoded.body);
}

function contentTypeIsJson(value: string | undefined): boolean {
  return value?.split(";", 1)[0].trim().toLowerCase() === "application/json";
}

function jsonBodyParser(): ReturnType<typeof express.json> {
  return express.json({
    limit: MAX_BODY_BYTES,
    strict: true,
    type: "application/json",
    verify: (request, _response, buffer) => {
      (request as Request).rawBody = Buffer.from(buffer);
    },
  });
}

function expressBodyError(
  errorValue: unknown,
  _request: Request,
  response: Response,
  next: NextFunction,
): void {
  const candidate = errorValue as { type?: string; status?: number; statusCode?: number };
  if (
    candidate.type === "entity.too.large" ||
    candidate.status === 413 ||
    candidate.statusCode === 413
  ) {
    sendExpress(response, error(413, "request body too large"));
    return;
  }
  if (
    candidate.type === "entity.parse.failed" ||
    candidate.status === 400 ||
    candidate.statusCode === 400
  ) {
    sendExpress(response, error(400, "invalid JSON body"));
    return;
  }
  next(errorValue);
}

function configureExpress(app: express.Express): void {
  app.disable("x-powered-by");
  app.set("etag", false);
  app.use(jsonBodyParser());
  app.get("/healthz", (_request, response) => sendExpress(response, domain.health()));
  app.get("/benchmark/info", (_request, response) => sendExpress(response, domain.info(metadata)));
  app.get("/benchmark/integrity", (_request, response) =>
    sendExpress(response, domain.integrity()),
  );
  app.get("/products", (request, response) => {
    const query = parseListQuery(request.query as Record<string, unknown>, seedCount);
    sendExpress(
      response,
      query ? domain.list(query.offset, query.limit) : error(400, "invalid query parameters"),
    );
  });
  app.get("/products/:id", (request, response) => {
    const id = parseId(request.params.id);
    sendExpress(
      response,
      id === null ? error(400, "id must be a positive integer") : domain.detail(id),
    );
  });
  app.post("/products/:id/stock", (request, response) => {
    if (!contentTypeIsJson(request.header("content-type"))) {
      sendExpress(response, error(415, "content-type must be application/json"));
      return;
    }
    const id = parseId(request.params.id);
    const delta = parseDeltaBody(request.rawBody, request.body);
    sendExpress(
      response,
      id === null
        ? error(400, "id must be a positive integer")
        : delta === null
          ? error(400, "body must be exactly an object with integer delta")
          : domain.stock(id, delta),
    );
  });
  app.use(expressBodyError);
}

@Controller()
class NestController {
  @Get("healthz")
  health(@Res() response: Response): void {
    sendExpress(response, domain.health());
  }

  @Get("benchmark/info")
  info(@Res() response: Response): void {
    sendExpress(response, domain.info(metadata));
  }

  @Get("benchmark/integrity")
  integrity(@Res() response: Response): void {
    sendExpress(response, domain.integrity());
  }

  @Get("products")
  list(@Query() query: Record<string, unknown>, @Res() response: Response): void {
    const parsed = parseListQuery(query, seedCount);
    sendExpress(
      response,
      parsed ? domain.list(parsed.offset, parsed.limit) : error(400, "invalid query parameters"),
    );
  }

  @Get("products/:id")
  detail(@Param("id") rawId: string, @Res() response: Response): void {
    const id = parseId(rawId);
    sendExpress(
      response,
      id === null ? error(400, "id must be a positive integer") : domain.detail(id),
    );
  }

  @Post("products/:id/stock")
  stock(@Param("id") rawId: string, @Req() request: Request, @Res() response: Response): void {
    if (!contentTypeIsJson(request.header("content-type"))) {
      sendExpress(response, error(415, "content-type must be application/json"));
      return;
    }
    const id = parseId(rawId);
    const delta = parseDeltaBody(request.rawBody, request.body);
    sendExpress(
      response,
      id === null
        ? error(400, "id must be a positive integer")
        : delta === null
          ? error(400, "body must be exactly an object with integer delta")
          : domain.stock(id, delta),
    );
  }
}

@Module({ controllers: [NestController] })
class NestModule {}

async function startNest(): Promise<void> {
  const app = await NestFactory.create(NestModule, { bodyParser: false, logger: false });
  const expressApp = app.getHttpAdapter().getInstance() as express.Express;
  expressApp.disable("x-powered-by");
  expressApp.set("etag", false);
  expressApp.use(jsonBodyParser());
  expressApp.use(expressBodyError);
  await app.init();
  await app.listen(port, "0.0.0.0");
}

async function startFastify(): Promise<void> {
  const app = Fastify({ logger: false, bodyLimit: MAX_BODY_BYTES });
  app.removeContentTypeParser("application/json");
  app.addContentTypeParser(
    /^application\/json(?:\s*;.*)?$/i,
    { parseAs: "buffer" },
    (_request, body, done) => done(null, body),
  );
  app.setErrorHandler((errorValue, _request, response) => {
    const sourceStatus = (errorValue as { statusCode?: number }).statusCode;
    const status =
      sourceStatus === 413 || sourceStatus === 415 || sourceStatus === 500 ? sourceStatus : 400;
    sendFastify(
      response,
      error(
        status,
        status === 413
          ? "request body too large"
          : status === 415
            ? "content-type must be application/json"
            : status === 500
              ? "internal server error"
              : "invalid JSON body",
      ),
    );
  });
  app.get("/healthz", (_request, response) => sendFastify(response, domain.health()));
  app.get("/benchmark/info", (_request, response) => sendFastify(response, domain.info(metadata)));
  app.get("/benchmark/integrity", (_request, response) =>
    sendFastify(response, domain.integrity()),
  );
  app.get(
    "/products",
    (request: FastifyRequest<{ Querystring: Record<string, unknown> }>, response) => {
      const query = parseListQuery(request.query, seedCount);
      sendFastify(
        response,
        query ? domain.list(query.offset, query.limit) : error(400, "invalid query parameters"),
      );
    },
  );
  app.get("/products/:id", (request: FastifyRequest<{ Params: { id: string } }>, response) => {
    const id = parseId(request.params.id);
    sendFastify(
      response,
      id === null ? error(400, "id must be a positive integer") : domain.detail(id),
    );
  });
  app.post(
    "/products/:id/stock",
    (request: FastifyRequest<{ Params: { id: string }; Body: Buffer }>, response) => {
      if (!contentTypeIsJson(request.headers["content-type"])) {
        sendFastify(response, error(415, "content-type must be application/json"));
        return;
      }
      const id = parseId(request.params.id);
      let parsedBody: unknown;
      try {
        parsedBody = JSON.parse(request.body.toString("utf8"));
      } catch {
        sendFastify(response, error(400, "invalid JSON body"));
        return;
      }
      const delta = parseDeltaBody(request.body, parsedBody);
      sendFastify(
        response,
        id === null
          ? error(400, "id must be a positive integer")
          : delta === null
            ? error(400, "body must be exactly an object with integer delta")
            : domain.stock(id, delta),
      );
    },
  );
  await app.listen({ port, host: "0.0.0.0" });
}

const startedAt = performance.now();
if (framework === "express") {
  const app = express();
  configureExpress(app);
  app.listen(port, "0.0.0.0");
} else if (framework === "nest") {
  await startNest();
} else {
  await startFastify();
}
console.error(
  `sqlite-ramp node ${framework} started in ${(performance.now() - startedAt).toFixed(1)}ms`,
);
