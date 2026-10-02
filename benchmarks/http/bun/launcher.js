const workers = Number(process.env.WORKERS || "1");
if (![1, 2].includes(workers)) {
  throw new Error("WORKERS must be 1 or 2");
}

if (workers === 1) {
  // Preserve the baseline: one worker means the server itself is PID 1.
  await import("./server.js");
} else {
  const children = Array.from({ length: workers }, () =>
    Bun.spawn([process.execPath, "server.js"], {
      cwd: import.meta.dir,
      env: { ...process.env, REUSE_PORT: "1" },
      stdout: "inherit",
      stderr: "inherit",
    }),
  );
  let stopping = false;
  function stop(signal) {
    if (stopping) {
      return;
    }
    stopping = true;
    for (const child of children) {
      child.kill(signal);
    }
  }
  process.on("SIGINT", () => stop("SIGINT"));
  process.on("SIGTERM", () => stop("SIGTERM"));
  const firstExit = await Promise.race(children.map((child) => child.exited));
  if (!stopping) {
    // A two-worker container must never keep serving with one child after a failed startup or crash.
    stop("SIGTERM");
  }
  const exits = await Promise.all(children.map((child) => child.exited));
  process.exitCode = firstExit === 0 && exits.every((code) => code === 0) ? 0 : 1;
}
