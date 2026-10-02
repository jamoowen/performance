import { afterEach, expect, test } from "bun:test";
import { diagnosticsEnabled, startDiagnostics } from "../bun/diagnostics.js";

const running = [];

afterEach(() => {
  while (running.length > 0) {
    running.pop().stop();
  }
});

function start(options) {
  const diagnostics = startDiagnostics(options);
  running.push(diagnostics);
  return diagnostics;
}

async function response(url, options) {
  const result = await fetch(url, options);
  return { status: result.status, headers: result.headers, body: await result.json() };
}

test("DIAGNOSTICS accepts only disabled and enabled values", () => {
  for (const value of [undefined, "", "0"]) {
    expect(diagnosticsEnabled(value)).toBe(false);
  }
  expect(diagnosticsEnabled("1")).toBe(true);
  for (const value of ["2", "true", "-1", " 1"]) {
    expect(() => diagnosticsEnabled(value)).toThrow("DIAGNOSTICS must be 0 or 1");
  }
});

test("disabled diagnostics do not create a listener", () => {
  expect(diagnosticsEnabled("0")).toBe(false);
  expect(running).toHaveLength(0);
});

test("runtime reports finite Bun heap and cumulative CPU measurements", async () => {
  const diagnostics = start({ port: 0 });
  const result = await response(`http://127.0.0.1:${diagnostics.server.port}/runtime`);

  expect(result.status).toBe(200);
  expect(result.body).toMatchObject({
    schema_version: 1,
    runtime: "bun",
    process_id: process.pid,
    capabilities: ["cpu"],
    bun: { version: Bun.version, js_execution_threads: 1 },
    database: {
      mode: "synchronous-single-connection",
      wait_count: null,
      wait_duration_ns: null,
    },
  });
  for (const value of [
    result.body.time_unix,
    result.body.process_cpu.user_us,
    result.body.process_cpu.system_us,
    result.body.bun.heap_size_bytes,
    result.body.bun.heap_capacity_bytes,
    result.body.bun.extra_memory_bytes,
    result.body.bun.heap_objects,
  ]) {
    expect(Number.isFinite(value)).toBe(true);
    expect(value).toBeGreaterThanOrEqual(0);
  }
  for (const value of [
    result.body.bun.event_loop_delay_mean_ms,
    result.body.bun.event_loop_delay_p95_ms,
    result.body.bun.event_loop_delay_max_ms,
  ]) {
    expect(value === null || (Number.isFinite(value) && value >= 0)).toBe(true);
  }
  expect(result.body.observations.event_loop_delay_window).toBe(
    "since diagnostics start or the previous GET /runtime",
  );
  expect(result.body.observations.unix_poll_started).toBeLessThanOrEqual(result.body.time_unix);
});

test("diagnostic routes reject unsupported methods, paths, and CPU bounds", async () => {
  const diagnostics = start({ port: 0 });
  const base = `http://127.0.0.1:${diagnostics.server.port}`;

  const method = await response(`${base}/runtime`, { method: "POST" });
  expect(method).toMatchObject({ status: 405, body: { error: "method not allowed" } });
  expect(method.headers.get("allow")).toBe("GET");
  expect((await response(`${base}/cpu?seconds=0`)).status).toBe(400);
  expect((await response(`${base}/cpu?seconds=121`)).status).toBe(400);
  expect((await response(`${base}/cpu?seconds=1.5`)).status).toBe(400);
  expect((await response(`${base}/missing`)).status).toBe(404);
});

test("native profiler returns a Bun JSC artifact and permits other requests", async () => {
  const diagnostics = start({ port: 0 });
  const base = `http://127.0.0.1:${diagnostics.server.port}`;
  const capture = response(`${base}/cpu?seconds=1`);
  await Bun.sleep(50);
  const concurrent = await response(`${base}/cpu?seconds=1`);
  expect(concurrent).toMatchObject({
    status: 409,
    body: { error: "CPU profile capture already in progress" },
  });
  for (let count = 0; count < 20; count += 1) {
    expect((await response(`${base}/runtime`)).status).toBe(200);
  }
  const result = await capture;
  expect(result.status).toBe(200);
  expect(result.body.format).toBe("bun:jsc.profile");
  expect(typeof result.body.functions).toBe("string");
  expect(result.body.functions.length).toBeGreaterThan(0);
  expect(result.body.stackTraces.traces.length).toBeGreaterThan(0);
  expect(result.body.stackTraces.interval).toBeGreaterThan(0);
});

test("client cancellation releases a capture for a follow-up profile", async () => {
  const diagnostics = start({ port: 0 });
  const base = `http://127.0.0.1:${diagnostics.server.port}`;
  const controller = new AbortController();
  const cancelled = fetch(`${base}/cpu?seconds=120`, { signal: controller.signal }).then(
    () => true,
    () => true,
  );
  await Bun.sleep(50);
  controller.abort();
  expect(await Promise.race([cancelled, Bun.sleep(1000).then(() => false)])).toBe(true);
  const result = await response(`${base}/cpu?seconds=1`);
  expect(result.status).toBe(200);
  expect(result.body.format).toBe("bun:jsc.profile");
});

test("stopping diagnostics interrupts captures and frees its listener", async () => {
  const first = start({ port: 0 });
  const port = first.server.port;
  const capture = fetch(`http://127.0.0.1:${port}/cpu?seconds=120`).then(
    () => true,
    () => true,
  );
  await Bun.sleep(50);
  first.stop();
  running.pop();
  expect(await Promise.race([capture, Bun.sleep(1000).then(() => false)])).toBe(true);
  const second = start({ port });
  expect(second.server.port).toBe(port);
  const result = await response(`http://127.0.0.1:${port}/cpu?seconds=1`);
  expect(result.status).toBe(200);
});
