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
