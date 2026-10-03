const data = JSON.parse(document.getElementById("report-data").textContent);
const allRuns = new Set(data.runs.map((run) => run.id));
const allRuntimes = new Set(data.runs.map((run) => run.runtime));
const allRates = new Set(data.schedule.stages.map((stage) => String(stage.targetRps)));
const selectedRuns = new Set(allRuns);
const selectedRuntimes = new Set(allRuntimes);
const selectedRates = new Set(allRates);
const controls = document.getElementById("controls");
const runtimeColors = {
  go: "#0072b2",
  node: "#d55e00",
  bun: "#009e73",
  rust: "#cc79a7",
  python: "#e69f00",
  elixir: "#56b4e9",
};
const frameworkDashes = {
  nethttp: "solid",
  express: "solid",
  native: "solid",
  axum: "solid",
  fastapi: "solid",
  plug: "solid",
  chi: "dash",
  fastify: "dash",
  hono: "dash",
  actix: "dash",
  phoenix: "dash",
  fiber: "dot",
  nest: "dot",
  elysia: "dot",
  rocket: "dot",
};
const runColor = new Map(data.runs.map((run) => [run.id, runtimeColors[run.runtime] ?? "#000000"]));
const runLine = (run) => ({
  color: runColor.get(run.id),
  dash: frameworkDashes[run.framework] ?? "solid",
});
const htmlEscape = (value) =>
  String(value ?? "").replace(
    /[&<>]/g,
    (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[character],
  );
const formatNumber = (value, maximumFractionDigits) =>
  Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits }) : "—";
const formatMiB = (value) => formatNumber(Number.isFinite(value) ? value / 1048576 : null, 1);
const formatPercent = (value) => formatNumber(Number.isFinite(value) ? value * 100 : null, 2);
const get = (value, path) => path.split(".").reduce((current, key) => current?.[key], value);
const activeRuns = () =>
  data.runs.filter((run) => selectedRuns.has(run.id) && selectedRuntimes.has(run.runtime));
const selectedLabel = (id) => {
  const select = document.getElementById(id);
  return select.options[select.selectedIndex].text;
};
const plotOptions = () => ({ responsive: true });
const plotLayout = (yTitle) => ({
  showlegend: document.getElementById("show-legends").checked,
  xaxis: { title: { text: "Elapsed seconds" }, automargin: true },
  yaxis: { title: { text: yTitle }, automargin: true },
  margin: { t: 25, b: 55, l: 60, r: 25 },
});

function rateAt(seconds) {
  const stages = data.schedule.stages || [];
  const lastStage = stages.at(-1);
  if (lastStage && seconds >= lastStage.stableEndSeconds) {
    return 0;
  }
  for (let index = 0; index < stages.length; index += 1) {
    const stage = stages[index];
    if (index === 0 && seconds <= stage.stableStartSeconds) {
      return stage.targetRps;
    }
    if (seconds >= stage.stableStartSeconds && seconds <= stage.stableEndSeconds) {
      return stage.targetRps;
    }
    if (seconds >= stage.transitionStartSeconds && seconds <= stage.transitionEndSeconds) {
      const previous = stages[index - 1]?.targetRps ?? stage.targetRps;
      const fraction =
        (seconds - stage.transitionStartSeconds) /
        (stage.transitionEndSeconds - stage.transitionStartSeconds);
      return previous + (stage.targetRps - previous) * fraction;
    }
  }
  return stages.at(-1)?.targetRps ?? null;
}

function selectedPoint(point) {
  const stages = data.schedule.stages || [];
  const lastStage = stages.at(-1);
  if (lastStage && (point.phase === "drain" || point.seconds >= lastStage.stableEndSeconds)) {
    return selectedRates.has(String(lastStage.targetRps));
  }
  for (let index = 0; index < stages.length; index += 1) {
    const stage = stages[index];
    if (index === 0 && point.seconds <= stage.stableStartSeconds) {
      return selectedRates.has(String(stage.targetRps));
    }
    if (point.seconds >= stage.stableStartSeconds && point.seconds <= stage.stableEndSeconds) {
      return selectedRates.has(String(stage.targetRps));
    }
    if (
      point.seconds >= stage.transitionStartSeconds &&
      point.seconds <= stage.transitionEndSeconds
    ) {
      const previous = stages[index - 1]?.targetRps ?? stage.targetRps;
      return selectedRates.has(String(previous)) || selectedRates.has(String(stage.targetRps));
    }
  }
  return point.targetRps != null && selectedRates.has(String(point.targetRps));
}

function makeGroup(name, values, selected) {
  const fieldset = document.createElement("fieldset");
  fieldset.className = "control-group";
  fieldset.innerHTML = `<legend>${htmlEscape(name)}</legend><button type="button">All</button><button type="button">None</button>`;
  const refresh = () => {
    fieldset.querySelectorAll("input").forEach((input) => {
      input.checked = selected.has(input.value);
    });
    render();
  };
  fieldset.querySelector("button").onclick = () => {
    values.forEach((value) => {
      selected.add(String(value.id));
    });
    refresh();
  };
  fieldset.querySelectorAll("button")[1].onclick = () => {
    values.forEach((value) => {
      selected.delete(String(value.id));
    });
    refresh();
  };
  values.forEach((value) => {
    const label = document.createElement("label");
    const key = value.color ? `<span class="key" style="background:${value.color}"></span>` : "";
    label.innerHTML = `<input type="checkbox" value="${htmlEscape(value.id)}"> ${key}${htmlEscape(value.label)}`;
    const input = label.querySelector("input");
    input.checked = selected.has(String(value.id));
    input.onchange = () => {
      input.checked ? selected.add(String(value.id)) : selected.delete(String(value.id));
      render();
    };
    fieldset.append(label);
  });
  controls.append(fieldset);
}

function setupControls() {
  makeGroup(
    "Runtime",
    [...allRuntimes].map((id) => ({ id, label: id })),
    selectedRuntimes,
  );
  makeGroup(
    "Framework",
    data.runs.map((run) => ({
      id: run.id,
      label: `${run.runtime} · ${run.framework}`,
      color: runColor.get(run.id),
    })),
    selectedRuns,
  );
  makeGroup(
    "Target RPS",
    [...allRates].sort((left, right) => left - right).map((id) => ({ id, label: id })),
    selectedRates,
  );
  document.getElementById("restore").onclick = () => {
    [allRuns, allRuntimes, allRates].forEach((source, index) => {
      const target = [selectedRuns, selectedRuntimes, selectedRates][index];
      target.clear();
      source.forEach((value) => {
        target.add(String(value));
      });
    });
    controls.replaceChildren();
    setupControls();
    render();
  };
}

function tracesForHistory(run, metric, transform = (value) => value) {
  return {
    name: `${run.runtime} · ${run.framework}`,
    mode: "lines",
    connectgaps: false,
    line: runLine(run),
    marker: { color: runColor.get(run.id) },
    x: run.history.map((point) => point.seconds),
    y: run.history.map((point) => (selectedPoint(point) ? transform(point[metric], point) : null)),
  };
}

function render() {
  const runs = activeRuns();
  const limitations = [
    ...data.limitations,
    ...runs.flatMap((run) => {
      const generator = run.generator || {};
      const warnings = generator.warnings || [];
      return [
        ...(run.validity?.status === "invalid"
          ? [
              `${run.runtime} · ${run.framework}: incomplete or invalid capture; exclude from capacity rankings.`,
            ]
          : []),
        ...(generator.headroomFlag || warnings.includes("generator_headroom")
          ? [`${run.runtime} · ${run.framework}: generator headroom flag`]
          : []),
        ...warnings
          .filter((warning) => warning !== "generator_headroom")
          .map((warning) => `${run.runtime} · ${run.framework}: generator ${warning}`),
      ];
    }),
  ];
  document.getElementById("limitations").innerHTML = limitations
    .map((item) => `<li>${htmlEscape(item)}</li>`)
    .join("");
  document.getElementById("empty").hidden = Boolean(runs.length && selectedRates.size);
  const windowMetric = document.getElementById("window-metric").value;
  Plotly.react(
    "windows",
    runs.map((run) => ({
      name: `${run.runtime} · ${run.framework}`,
      mode: "lines+markers",
      line: runLine(run),
      marker: { color: runColor.get(run.id) },
      x: run.windows
        .filter((window) => window.stable && selectedRates.has(String(window.targetRps)))
        .map((window) => window.targetRps),
      y: run.windows
        .filter((window) => window.stable && selectedRates.has(String(window.targetRps)))
        .map((window) => get(window, windowMetric)),
    })),
    {
      ...plotLayout(selectedLabel("window-metric")),
      xaxis: { title: { text: "Target RPS" }, automargin: true },
    },
    plotOptions(),
  );
  const latencyMetric = document.getElementById("latency-metric").value;
  const latency = runs.map((run) => tracesForHistory(run, latencyMetric));
  if (runs[0]) {
    latency.push({
      name: "Target RPS",
      mode: "lines",
      line: { dash: "dot", color: "#607080" },
      yaxis: "y2",
      x: runs[0].history.map((point) => point.seconds),
      y: runs[0].history.map((point) => (selectedPoint(point) ? rateAt(point.seconds) : null)),
      connectgaps: false,
    });
  }
  Plotly.react(
    "latency",
    latency,
    {
      xaxis: { title: { text: "Elapsed seconds" } },
      ...plotLayout(selectedLabel("latency-metric")),
      yaxis2: { title: { text: "Target RPS" }, overlaying: "y", side: "right" },
      margin: { t: 25 },
    },
    plotOptions(),
  );
  const outcomeReaders = {
    successful: (point) => point.successful,
    completed: (point) => point.completed,
    http200: (point) => point.statuses?.["200"],
    dropped: (point) => point.dropped,
    httpFailures: (point) => point.httpFailures,
    validationFailures: (point) => point.validationFailures,
  };
  const outcomeMetric = document.getElementById("outcome-metric").value;
  const outcomes = runs.map((run) => ({
    ...tracesForHistory(
      run,
      "seconds",
      (_unused, point) => (outcomeReaders[outcomeMetric](point) ?? 0) / (point.bucketSeconds || 5),
    ),
    name: `${run.runtime} · ${run.framework}`,
  }));
  if (runs[0]) {
    outcomes.push({
      name: "Target RPS",
      mode: "lines",
      line: { dash: "dot", color: "#607080" },
      x: runs[0].history.map((point) => point.seconds),
      y: runs[0].history.map((point) => (selectedPoint(point) ? rateAt(point.seconds) : null)),
      connectgaps: false,
    });
  }
  Plotly.react("throughput", outcomes, plotLayout(selectedLabel("outcome-metric")), plotOptions());
  const resourceMetric = document.getElementById("resource-metric").value;
  const resource = runs.map((run) => {
    const points = run.resource?.samples || [];
    return {
      name: `${run.runtime} · ${run.framework}`,
      mode: "lines",
      connectgaps: false,
      line: runLine(run),
      x: points.map((point) => point.seconds),
      y: points.map((point, index) => {
        if (!selectedPoint(point)) {
          return null;
        }
        if (resourceMetric === "workingSetMiB") {
          return point.workingSetBytes == null ? null : point.workingSetBytes / 1048576;
        }
        if (resourceMetric === "throttledSecondsPerSecond") {
          const previous = points[index - 1];
          const elapsed = previous ? point.seconds - previous.seconds : null;
          return elapsed && elapsed <= 2.5 ? point.throttledSeconds / elapsed : null;
        }
        return point[resourceMetric] ?? null;
      }),
    };
  });
  Plotly.react("resources", resource, plotLayout(selectedLabel("resource-metric")), plotOptions());
  const details = document.getElementById("details");
  details.replaceChildren();
  runs.forEach((run) => {
    const card = document.createElement("details");
    const validity = run.validity || { status: "invalid", reasons: ["local_artifact"] };
    const windows = (run.windows || []).filter(
      (window) => window.stable && selectedRates.has(String(window.targetRps)),
    );
    const hasNoStableWindowStatistics = validity.status === "invalid" && run.windows.length === 0;
    card.innerHTML = `<summary>${htmlEscape(run.runtime)} · ${htmlEscape(run.framework)} <span class="${validity.status === "invalid" ? "invalid" : ""}">${htmlEscape(validity.status)}</span></summary><p class="meta">${htmlEscape(validity.reasons.join(", "))}<br>image ${htmlEscape(run.build.imageDigest)} · source ${htmlEscape(run.build.sourceRevision)} · load ${htmlEscape(run.build.loadHash)}<br>${htmlEscape(run.metadata.runtimeVersion)} · ${htmlEscape(run.metadata.frameworkVersion)} · ${htmlEscape(run.metadata.driver)} ${htmlEscape(run.metadata.driverVersion)} · SQLite ${htmlEscape(run.metadata.sqliteVersion)} · workers ${htmlEscape(run.metadata.workers)} · ${htmlEscape(JSON.stringify(run.metadata.workerSettings || run.metadata.compileOptions || {}))} · coverage ${htmlEscape(run.resource.coverage)}</p>${hasNoStableWindowStatistics ? "<p>No stable-window statistics are available for this invalid capture.</p>" : ""}<div class="stages"><table><thead><tr><th>RPS</th><th>Goodput (req/s)</th><th>p95 (ms): client / service / DB</th><th>CPU (mCPU) / working set (MiB) / CFS periods (%)</th><th>Drops/errors</th><th>SLO</th></tr></thead><tbody>${windows.map((window) => `<tr><td>${formatNumber(window.targetRps, 0)}</td><td>${formatNumber(window.goodputRps, 1)}</td><td>${formatNumber(window.client.p95Ms, 2)} / ${formatNumber(window.serviceP95Ms, 3)} / ${formatNumber(window.dbP95Ms, 3)}</td><td>${formatNumber(window.resource.cpuMillicores, 1)} / ${formatMiB(window.resource.workingSetBytes)} / ${formatPercent(window.resource.cfsPeriodRatio)}</td><td class="${window.dropped ? "bad" : ""}">${htmlEscape(window.dropped)} / ${htmlEscape(window.httpFailures)} / ${htmlEscape(window.validationFailures)} / ${htmlEscape(window.checksFailed)}</td><td class="${window.slo.status === "fail" ? "bad" : ""}">${htmlEscape(window.slo.status)} ${htmlEscape(window.slo.reasons.join(", "))}</td></tr>`).join("")}</tbody></table></div>`;
    details.append(card);
  });
}

["window-metric", "latency-metric", "outcome-metric", "resource-metric", "show-legends"].forEach(
  (id) => {
    document.getElementById(id).onchange = render;
  },
);
document.getElementById("show-legends").checked = window.innerWidth >= 600;
setupControls();
render();
