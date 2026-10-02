import { heapStats, profile } from "bun:jsc";
import { monitorEventLoopDelay } from "node:perf_hooks";

const diagnosticsHost = "127.0.0.1";
const diagnosticsPort = 6060;

function response(body, status = 200, headers = {}) {
  return Response.json(body, { status, headers });
}

function error(status, message, headers) {
  return response({ error: message }, status, headers);
}

function eventLoopMilliseconds(value) {
  return Number.isFinite(value) && value >= 0 ? value / 1_000_000 : null;
}

function requestedSeconds(url) {
  const value = url.searchParams.get("seconds");
  if (value === null) {
    return 30;
  }
  if (!/^[0-9]+$/.test(value)) {
    return null;
  }
  const seconds = Number(value);
  return Number.isSafeInteger(seconds) && seconds >= 1 && seconds <= 120 ? seconds : null;
}

export function diagnosticsEnabled(value = process.env.DIAGNOSTICS) {
  if (value === undefined || value === "" || value === "0") {
    return false;
  }
  if (value === "1") {
    return true;
  }
  throw new Error("DIAGNOSTICS must be 0 or 1");
}

export function startDiagnostics({ hostname = diagnosticsHost, port = diagnosticsPort } = {}) {
  const eventLoop = monitorEventLoopDelay({ resolution: 10 });
  let eventLoopWindowStarted = Date.now() / 1000;
  let capturing = false;
  let cancelCapture;
  let stopped = false;
  eventLoop.enable();

  function waitForCapture(seconds, signal) {
    return new Promise((resolve) => {
      let finished = false;
      const finish = (cancelled) => {
        if (finished) {
          return;
        }
        finished = true;
        clearTimeout(timer);
        signal.removeEventListener("abort", cancel);
        if (cancelCapture === cancel) {
          cancelCapture = undefined;
        }
        resolve(cancelled);
      };
      const cancel = () => finish(true);
      const timer = setTimeout(() => finish(false), seconds * 1000);
      cancelCapture = cancel;
      signal.addEventListener("abort", cancel, { once: true });
      if (signal.aborted) {
        cancel();
      }
    });
  }

  function runtime() {
    const heap = heapStats();
    const cpu = process.cpuUsage();
    const timeUnix = Date.now() / 1000;
    const body = {
      schema_version: 1,
      runtime: "bun",
      process_id: process.pid,
      time_unix: timeUnix,
      capabilities: ["cpu"],
      process_cpu: { user_us: cpu.user, system_us: cpu.system },
      bun: {
        version: Bun.version,
        js_execution_threads: 1,
        heap_size_bytes: heap.heapSize,
        heap_capacity_bytes: heap.heapCapacity,
        extra_memory_bytes: heap.extraMemorySize,
        heap_objects: heap.objectCount,
        event_loop_delay_mean_ms: eventLoopMilliseconds(eventLoop.mean),
        event_loop_delay_p95_ms: eventLoopMilliseconds(eventLoop.percentile(95)),
        event_loop_delay_max_ms: eventLoopMilliseconds(eventLoop.max),
      },
      database: {
        mode: "synchronous-single-connection",
        wait_count: null,
        wait_duration_ns: null,
      },
      observations: {
        event_loop_delay_window: "since diagnostics start or the previous GET /runtime",
        unix_poll_started: eventLoopWindowStarted,
      },
    };
    eventLoop.reset();
    eventLoopWindowStarted = timeUnix;
    return response(body);
  }

  async function cpu(request, url) {
    const seconds = requestedSeconds(url);
    if (seconds === null) {
      return error(400, "seconds must be an integer between 1 and 120");
    }
    if (capturing) {
      return error(409, "CPU profile capture already in progress");
    }
    capturing = true;
    try {
      let cancelled = false;
      const artifact = await profile(async () => {
        cancelled = await waitForCapture(seconds, request.signal);
      }, 1000);
      if (cancelled) {
        return error(499, "CPU profile capture cancelled");
      }
      return response({ format: "bun:jsc.profile", ...artifact });
    } catch (caught) {
      console.error(caught);
      return error(500, "CPU profile capture failed");
    } finally {
      capturing = false;
    }
  }

  let server;
  try {
    server = Bun.serve({
      hostname,
      port,
      idleTimeout: 0,
      development: false,
      async fetch(request) {
        const url = new URL(request.url);
        if (url.pathname === "/runtime") {
          if (request.method !== "GET") {
            return error(405, "method not allowed", { Allow: "GET" });
          }
          try {
            return runtime();
          } catch (caught) {
            console.error(caught);
            return error(500, "runtime diagnostics unavailable");
          }
        }
        if (url.pathname === "/cpu") {
          if (request.method !== "GET") {
            return error(405, "method not allowed", { Allow: "GET" });
          }
          return cpu(request, url);
        }
        return error(404, "not found");
      },
    });
  } catch (caught) {
    eventLoop.disable();
    throw caught;
  }

  return {
    server,
    stop() {
      if (stopped) {
        return;
      }
      stopped = true;
      eventLoop.disable();
      cancelCapture?.();
      server.stop(true);
    },
  };
}
