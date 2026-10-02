const renderedCharts = new Set();

function renderChart(chart) {
  const element = document.getElementById(chart.id);
  if (!element || element.closest("details:not([open])") || element.clientWidth === 0) {
    return;
  }
  if (renderedCharts.has(chart.id)) {
    if (window.Plotly?.Plots) {
      window.Plotly.Plots.resize(element);
    }
    return;
  }
  window.Plotly.newPlot(chart.id, chart.data, chart.layout, {
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ["select2d", "lasso2d"],
  });
  renderedCharts.add(chart.id);
}

function renderVisibleCharts(scope = document) {
  for (const chart of window.reportCharts) {
    const element = document.getElementById(chart.id);
    if (element && scope.contains(element)) {
      renderChart(chart);
    }
  }
}

renderVisibleCharts();
for (const details of document.querySelectorAll("details.http-history")) {
  details.addEventListener("toggle", () => {
    if (details.open) {
      requestAnimationFrame(() => renderVisibleCharts(details));
    }
  });
}

function renderRunOverlay() {
  const overlay = window.runOverlay;
  const element = document.getElementById("run-overlay");
  const message = document.getElementById("run-overlay-message");
  const selectionNote = document.getElementById("run-overlay-selection-note");
  const metricSelect = document.getElementById("run-overlay-metric");
  if (!overlay || !element || !message || !selectionNote || !metricSelect || !window.Plotly) {
    return;
  }
  const metric = overlay.metrics.find((item) => item.id === metricSelect.value);
  if (!metric) {
    message.textContent = "Choose a metric to display recorded samples.";
    window.Plotly.purge?.(element);
    return;
  }
  const checked = (selector, dataName, value) =>
    Array.from(document.querySelectorAll(selector)).some(
      (input) => input.checked && input.dataset[dataName] === value,
    );
  const selectedRuns = overlay.runs.filter(
    (run) =>
      checked("[data-overlay-runtime]", "overlayRuntime", run.runtime) &&
      checked("[data-overlay-rate]", "overlayRate", String(run.rate)) &&
      checked("[data-overlay-run]", "overlayRun", run.id),
  );
  if (!selectedRuns.length) {
    message.textContent = "No runs are selected. Enable a runtime, target RPS, and individual run.";
    selectionNote.textContent = "";
    window.Plotly.purge?.(element);
    return;
  }
  const selectedCompatibility = new Set(
    selectedRuns
      .filter((run) => run.compatibility !== null)
      .map((run) => JSON.stringify(run.compatibility)),
  );
  const selectionWarnings = [];
  if (selectedRuns.length === 1) {
    selectionWarnings.push("Only one run is selected.");
  }
  if (selectedCompatibility.size > 1) {
    selectionWarnings.push("Selected runs use nonmatching settings beyond target RPS.");
  }
  if (selectedRuns.some((run) => run.compatibility === null)) {
    selectionWarnings.push("A selected run has unknown compatibility.");
  }
  if (selectedRuns.some((run) => run.status !== "Passed targets")) {
    selectionWarnings.push("A selected run is incomplete or failed.");
  }
  if (
    ["p50_latency", "p95_latency", "p99_latency", "completed_rps"].includes(metric.id) &&
    selectedRuns.some((run) => run.http_warnings.length)
  ) {
    selectionWarnings.push(
      "A selected HTTP timeline is partial or unavailable; see its run card for details.",
    );
  }
  if (
    selectedRuns.some(
      (run) => !(run.series[metric.id] || []).some((sample) => Number.isFinite(sample.y)),
    )
  ) {
    selectionWarnings.push("A selected run has no recorded samples for this metric.");
  }
  selectionNote.textContent = selectionWarnings.join(" ");
  const traces = selectedRuns.flatMap((run) => {
    const samples = run.series[metric.id] || [];
    if (!samples.some((sample) => Number.isFinite(sample.y))) {
      return [];
    }
    const hasWindow = samples.some((sample) => sample.start !== undefined);
    return [
      {
        type: "scatter",
        mode: "lines+markers",
        name: run.label,
        x: samples.map((sample) => sample.x),
        y: samples.map((sample) => sample.y),
        customdata: samples.map((sample) => [
          run.label,
          run.runtime,
          run.rate,
          run.status,
          sample.start,
          sample.end,
          sample.requests,
        ]),
        connectgaps: false,
        line: { color: run.color, dash: run.dash },
        marker: { color: run.color },
        hovertemplate: hasWindow
          ? `%{customdata[1]} · %{customdata[2]} RPS<br>%{customdata[0]} · %{customdata[3]}<br>Elapsed: %{x:.3g} s<br>%{y:.3g} ${metric.unit}<br>Window: %{customdata[4]:.3g}–%{customdata[5]:.3g} s · %{customdata[6]} completed requests<extra></extra>`
          : `%{customdata[1]} · %{customdata[2]} RPS<br>%{customdata[0]} · %{customdata[3]}<br>Elapsed: %{x:.3g} s<br>%{y:.3g} ${metric.unit}<extra></extra>`,
      },
    ];
  });
  if (!traces.length) {
    message.textContent = `The selected runs have no recorded samples for ${metric.label}.`;
    window.Plotly.purge?.(element);
    return;
  }
  message.textContent = "";
  window.Plotly.newPlot(
    element,
    traces,
    {
      margin: { l: 62, r: 20, t: 18, b: 52 },
      showlegend: false,
      xaxis: { title: { text: "Elapsed time (s)" }, rangemode: "tozero" },
      yaxis: { title: { text: metric.unit }, rangemode: "tozero" },
    },
    { responsive: true, displaylogo: false, modeBarButtonsToRemove: ["select2d", "lasso2d"] },
  );
}

for (const control of document.querySelectorAll(
  "[data-overlay-runtime], [data-overlay-rate], [data-overlay-run], #run-overlay-metric",
)) {
  control.addEventListener("change", renderRunOverlay);
}
renderRunOverlay();
