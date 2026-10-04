const data = JSON.parse(document.getElementById("report-data").textContent);
const colors = {
  go: "#0072b2",
  node: "#d55e00",
  bun: "#009e73",
  rust: "#cc79a7",
  python: "#e69f00",
  elixir: "#56b4e9",
};
const dashes = {
  nethttp: "solid",
  express: "solid",
  native: "solid",
  axum: "solid",
  fastapi: "solid",
  phoenix: "dash",
  chi: "dash",
  fastify: "dash",
  hono: "dash",
  actix: "dash",
  fiber: "dot",
  nest: "dot",
  elysia: "dot",
};
const runSet = new Set(data.runs.map((r) => r.id)),
  runtimeSet = new Set(data.runs.map((r) => r.runtime));
const rateSet = new Set(
  data.runs.flatMap((r) => r.stages.map((s) => String(s.targetRps))).filter(Boolean),
);
const fmt = (v, n = 1) =>
  Number.isFinite(v) ? v.toLocaleString(undefined, { maximumFractionDigits: n }) : "—";
const get = (obj, path) => path.split(".").reduce((v, key) => v?.[key], obj);
const active = () => data.runs.filter((r) => runSet.has(r.id) && runtimeSet.has(r.runtime));
const line = (r) => ({ color: colors[r.runtime] || "#333", dash: dashes[r.framework] || "solid" });
const selectedStage = (s) => rateSet.has(String(s.targetRps));
const selectedHistory = (p) => rateSet.has(String(p.targetRps));
const selectedTime = (run, seconds) =>
  run.schedule.stages.some((stage) => {
    const start = stage.offsetSeconds ?? 0;
    const end =
      start +
      (stage.transitionSeconds ?? 0) +
      (stage.settlingSeconds ?? 0) +
      (stage.stableSeconds ?? 0);
    return selectedStage(stage) && seconds >= start && seconds <= end;
  });
const selectedEvent = (run, seconds) => {
  const priorStages = run.schedule.stages.filter((stage) => seconds >= (stage.offsetSeconds ?? 0));
  const stage = priorStages.at(-1);
  return Boolean(stage && selectedStage(stage));
};
const layout = (title) => ({
  showlegend: document.getElementById("show-legends").checked,
  xaxis: { title: { text: "Elapsed seconds" }, automargin: true },
  yaxis: { title: { text: title }, automargin: true },
  margin: { t: 25, b: 55, l: 65, r: 25 },
});
const opts = { responsive: true };

function group(title, values, set) {
  const fs = document.createElement("fieldset");
  fs.className = "control-group";
  fs.innerHTML = `<legend>${title}</legend><button type="button">All</button><button type="button">None</button>`;
  const refresh = () => {
    fs.querySelectorAll("input").forEach((i) => {
      i.checked = set.has(i.value);
    });
    render();
  };
  fs.querySelector("button").onclick = () => {
    values.forEach((v) => {
      set.add(v.id);
    });
    refresh();
  };
  fs.querySelectorAll("button")[1].onclick = () => {
    values.forEach((v) => {
      set.delete(v.id);
    });
    refresh();
  };
  values.forEach((v) => {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" value="${v.id}"> ${v.color ? `<span class="key" style="background:${v.color}"></span>` : ""}${v.label}`;
    const input = label.querySelector("input");
    input.checked = set.has(v.id);
    input.onchange = () => {
      input.checked ? set.add(v.id) : set.delete(v.id);
      render();
    };
    fs.append(label);
  });
  document.getElementById("controls").append(fs);
}
function controls() {
  group(
    "Runtime",
    [...runtimeSet].map((id) => ({ id, label: id })),
    runtimeSet,
  );
  group(
    "Framework",
    data.runs.map((r) => ({
      id: r.id,
      label: `${r.runtime} · ${r.framework}`,
      color: colors[r.runtime],
    })),
    runSet,
  );
  group(
    "Target RPS",
    [...rateSet].sort((a, b) => a - b).map((id) => ({ id, label: id })),
    rateSet,
  );
  document.getElementById("restore").onclick = () => {
    [runSet, runtimeSet, rateSet].forEach((set, index) => {
      set.clear();
      [
        data.runs.map((r) => r.id),
        data.runs.map((r) => r.runtime),
        data.runs.flatMap((r) => r.stages.map((s) => String(s.targetRps))),
      ][index].forEach((v) => {
        set.add(v);
      });
    });
    document.getElementById("controls").replaceChildren();
    controls();
    render();
  };
}
function annotations(run) {
  return (
    run.resource.events
      // An OOM can occur in the observer's drain/normalization tail. Attribute it
      // to the most recently offered stage instead of hiding it after stable end.
      .filter((e) => selectedEvent(run, e.seconds))
      .map((e) => ({
        x: e.seconds,
        y: 1,
        text: `${run.runtime} · ${run.framework}: ${e.type}`,
        showarrow: true,
        arrowhead: 2,
        yref: "paper",
      }))
  );
}
function historyTrace(run, metric, transform = (v) => v) {
  const x = [],
    y = [];
  let previous = null;
  run.history.forEach((point) => {
    if (!selectedHistory(point)) {
      return;
    }
    const gap =
      previous &&
      (point.seconds - previous.seconds >
        Math.max(point.bucketSeconds || 5, previous.bucketSeconds || 5) * 1.5 ||
        point.targetRps !== previous.targetRps);
    if (gap) {
      x.push(null);
      y.push(null);
    }
    x.push(point.seconds);
    y.push(transform(point[metric], point));
    previous = point;
  });
  return {
    name: `${run.runtime} · ${run.framework}`,
    mode: "lines",
    connectgaps: false,
    line: line(run),
    x,
    y,
  };
}
function sampleTrace(run, samples, metric, container = false) {
  const x = [],
    y = [];
  let previous = null;
  samples.forEach((point) => {
    if (!selectedTime(run, point.seconds)) {
      return;
    }
    const missingInterval =
      previous &&
      Number.isFinite(point.intervalStartSeconds) &&
      Number.isFinite(previous.intervalEndSeconds) &&
      point.intervalStartSeconds - previous.intervalEndSeconds > 2;
    const changedContainer = container && previous && point.segment !== previous.segment;
    if (missingInterval || changedContainer) {
      x.push(null);
      y.push(null);
    }
    x.push(point.seconds);
    y.push(point[metric]);
    previous = point;
  });
  return { x, y };
}
function stageWindows(run) {
  return run.stages
    .filter((stage) => selectedStage(stage) && stage.completed)
    .flatMap((s) => s.windows.filter((w) => w.stable).map((w) => ({ ...w, stage: s })));
}
function causeLabel(run) {
  const cause = run.capacity.stopReason;
  if (cause === "sustained_overload") {
    const overload = run.stages.find((stage) => stage.overload.status);
    const kinds = overload?.overload.reasons || [];
    const detail = [
      kinds.includes("errors") ? "HTTP/validation failures" : null,
      kinds.includes("drops") ? "dropped arrivals" : null,
    ].filter(Boolean);
    return `Sustained overload${detail.length ? ` (${detail.join(" and ")})` : ""}`;
  }
  return (
    {
      pod_restart_or_oom: "Pod restart or OOM",
      tested_safety_ceiling: "Safety ceiling tested",
      k6_failed: "k6 capture failed",
      generator_limit_disk_start: "Generator disk guard before step",
      generator_limit_memory_step: "Generator memory guard before step",
      generator_limit_nofile: "Generator file-descriptor guard",
      generator_limit_threads: "Generator thread guard",
      generator_limit_memory: "Generator memory guard",
      generator_limit_cpu: "Generator CPU guard",
      generator_limit_disk: "Generator disk guard",
    }[cause] || "—"
  );
}
function metadataCard(run) {
  const card = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `${run.runtime} · ${run.framework}`;
  const value = document.createElement("pre");
  value.textContent = JSON.stringify(
    {
      runtime: run.metadata.runtimeVersion,
      framework: run.metadata.frameworkVersion,
      driver: run.metadata.driver,
      driverVersion: run.metadata.driverVersion,
      sqlite: run.metadata.sqliteVersion,
      workers: run.metadata.workers,
      workerSettings: run.metadata.workerSettings,
      pragmas: run.metadata.pragmas,
      measuredVus: run.metadata.measuredVus,
      maxVus: run.metadata.maxVus,
      warmupVus: run.metadata.warmupVus,
      collectorDurationSeconds: run.metadata.collectorDurationSeconds,
      imageDigest: run.build.imageDigest,
      sourceRevision: run.build.sourceRevision,
      harnessSourceRevision: run.build.harnessSourceRevision,
      loadHash: run.build.loadHash,
      protocolHash: run.build.protocolHash,
    },
    null,
    2,
  );
  card.append(summary, value);
  return card;
}
function render() {
  const runs = active();
  document.getElementById("empty").hidden = Boolean(runs.length && rateSet.size);
  const notes = [];
  const tbody = document.querySelector("#summary tbody");
  tbody.replaceChildren();
  runs.forEach((r) => {
    const c = r.capacity;
    const qualification =
      r.validity.status !== "valid"
        ? "capture invalid"
        : c.generatorLimited
          ? "inconclusive: generator guard"
          : r.generator.headroomFlag
            ? "caution: generator headroom"
            : "qualified";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.runtime} · ${r.framework}</td><td>${fmt(c.highestPassingRps, 0)}</td><td>${fmt(c.highestNoOverloadRps, 0)}</td><td>${fmt(c.firstOverloadRps, 0)}</td><td>${causeLabel(r)}</td><td class="${qualification !== "qualified" ? "warn" : ""}">${qualification}</td>`;
    tbody.append(tr);
    if (r.validity.status !== "valid") {
      notes.push(
        `${r.runtime} · ${r.framework}: invalid capture (${r.validity.reasons.join(", ") || "unspecified"}); do not use it for capacity.`,
      );
    }
    if (c.generatorLimited) {
      notes.push(
        `${r.runtime} · ${r.framework}: a generator guard stopped the run; the boundary is inconclusive.`,
      );
    } else if (r.generator.headroomFlag) {
      notes.push(
        `${r.runtime} · ${r.framework}: generator headroom warning; this is caution, not proof of saturation.`,
      );
    }
    if (r.integrity.status !== "verified") {
      notes.push(
        `${r.runtime} · ${r.framework}: final write integrity is ${r.integrity.status || "unverified"}${r.integrity.qualifier ? ` (${r.integrity.qualifier})` : ""}; HTTP and resource traces remain available.`,
      );
    }
    r.resource.events.forEach((e) => {
      notes.push(`${r.runtime} · ${r.framework}: ${e.type} at ${fmt(e.seconds, 1)} s.`);
    });
  });
  const metadata = document.getElementById("metadata");
  metadata.replaceChildren(...runs.map(metadataCard));
  document.getElementById("notes").innerHTML =
    notes.map((v) => `<li>${v}</li>`).join("") || "<li>No warnings in the selected runs.</li>";
  const wm = document.getElementById("window-metric").value;
  Plotly.react(
    "windows",
    runs.map((r) => {
      const w = stageWindows(r);
      return {
        name: `${r.runtime} · ${r.framework}`,
        mode: "lines+markers",
        line: line(r),
        marker: { color: colors[r.runtime] },
        x: w.map((x) => x.targetRps),
        y: w.map((x) => get(x, wm)),
      };
    }),
    {
      ...layout(document.querySelector("#window-metric option:checked").text),
      xaxis: { title: { text: "Target RPS" }, automargin: true },
    },
    opts,
  );
  const lm = document.getElementById("latency-metric").value;
  Plotly.react(
    "latency",
    runs.map((r) => historyTrace(r, lm)),
    {
      ...layout(document.querySelector("#latency-metric option:checked").text),
      annotations: runs.flatMap(annotations),
    },
    opts,
  );
  const om = document.getElementById("outcome-metric").value;
  Plotly.react(
    "outcomes",
    runs.map((r) =>
      historyTrace(r, om, (v, p) => {
        const value = om === "allErrors" ? (p.httpFailures ?? 0) + (p.validationFailures ?? 0) : v;
        return Number.isFinite(value) ? value / (p.bucketSeconds || 5) : null;
      }),
    ),
    {
      ...layout(document.querySelector("#outcome-metric option:checked").text),
      annotations: runs.flatMap(annotations),
    },
    opts,
  );
  const rm = document.getElementById("resource-metric").value;
  Plotly.react(
    "resources",
    runs.map((r) => ({
      name: `${r.runtime} · ${r.framework}`,
      mode: "lines",
      connectgaps: false,
      line: line(r),
      ...sampleTrace(r, r.resource.samples, rm),
    })),
    {
      ...layout(document.querySelector("#resource-metric option:checked").text),
      annotations: runs.flatMap(annotations),
    },
    opts,
  );
  const cm = document.getElementById("cfs-metric").value;
  Plotly.react(
    "cfs",
    runs.map((r) => ({
      name: `${r.runtime} · ${r.framework}`,
      mode: "lines",
      connectgaps: false,
      line: line(r),
      ...sampleTrace(r, r.resource.containerSamples, cm, true),
    })),
    {
      ...layout(document.querySelector("#cfs-metric option:checked").text),
      annotations: runs.flatMap(annotations),
    },
    opts,
  );
}
[
  "window-metric",
  "latency-metric",
  "outcome-metric",
  "resource-metric",
  "cfs-metric",
  "show-legends",
].forEach((id) => {
  document.getElementById(id).onchange = render;
});
document.getElementById("show-legends").checked = innerWidth >= 600;
controls();
render();
