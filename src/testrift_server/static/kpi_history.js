(function () {
  "use strict";

  const targetKey = window.KPI_TARGET;
  const metricSelect = document.getElementById("kpi-metric");
  const statusElement = document.getElementById("kpi-status");
  const statsElement = document.getElementById("kpi-history-stats");
  const runCountElement = document.getElementById("kpi-run-count");
  const sampleCountElement = document.getElementById("kpi-sample-count");
  const firstRunElement = document.getElementById("kpi-first-run");
  const lastRunElement = document.getElementById("kpi-last-run");
  const chartTitle = document.getElementById("kpi-chart-title");
  const chartSummary = document.getElementById("kpi-chart-summary");
  const chartElement = document.getElementById("kpi-chart");
  const chartLegend = document.getElementById("kpi-chart-legend");
  const chartEmpty = document.getElementById("kpi-chart-empty");
  const pageSize = 500;
  const metricLabels = {
    "throughput.tx_throughput": "TX throughput",
    "throughput.rx_throughput": "RX throughput",
  };
  let requestSequence = 0;

  function apiUrl(path, params) {
    const url = new URL(path, window.location.origin);
    Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, String(value)));
    return url;
  }

  async function fetchJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const body = await response.json();
    if (!response.ok || !body.success) {
      throw new Error(body.error || `Request failed (${response.status})`);
    }
    return body;
  }

  async function fetchPages(path, params) {
    const first = await fetchJson(apiUrl(path, { ...params, limit: pageSize, offset: 0 }));
    const rows = [...first.data];
    const offsets = [];
    for (let offset = rows.length; offset < first.pagination.count; offset += pageSize) {
      offsets.push(offset);
    }
    for (let index = 0; index < offsets.length; index += 4) {
      const pages = await Promise.all(offsets.slice(index, index + 4).map(offset =>
        fetchJson(apiUrl(path, { ...params, limit: pageSize, offset }))
      ));
      pages.forEach(page => rows.push(...page.data));
    }
    return { rows, count: first.pagination.count };
  }

  function setStatus(message, kind = "info") {
    statusElement.textContent = message || "";
    statusElement.classList.toggle("visible", Boolean(message));
    statusElement.classList.toggle("error", kind === "error");
  }

  function replaceMetricOptions(catalog) {
    const selections = new Map();
    catalog.forEach(item => selections.set(`${item.metric_key}\t${item.unit}`, item));
    const options = [...selections.entries()].sort(([left], [right]) => left.localeCompare(right));
    metricSelect.replaceChildren();
    options.forEach(([value, item]) => {
      const option = document.createElement("option");
      const label = metricLabels[item.metric_key] || item.metric_key.replaceAll(".", " / ").replaceAll("_", " ");
      option.value = value;
      option.textContent = `${label} · ${item.unit}`;
      metricSelect.appendChild(option);
    });
    metricSelect.disabled = options.length === 0;
    if (options.length) {
      const preferred = options.find(([value]) => value.startsWith("throughput.tx_throughput\t"));
      metricSelect.value = preferred ? preferred[0] : options[0][0];
    }
    return options.length;
  }

  function selectedMetric() {
    const [metricKey, unit] = metricSelect.value.split("\t");
    return { metricKey, unit };
  }

  function median(values) {
    const ordered = values.slice().sort((left, right) => left - right);
    const middle = Math.floor(ordered.length / 2);
    return ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2;
  }

  function quantile(values, ratio) {
    const ordered = values.slice().sort((left, right) => left - right);
    const position = (ordered.length - 1) * ratio;
    const lower = Math.floor(position);
    const upper = Math.ceil(position);
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower);
  }

  function formatDate(value, withTime = false) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "Date unavailable";
    return date.toLocaleString(undefined, withTime
      ? { dateStyle: "medium", timeStyle: "short" }
      : { dateStyle: "medium" });
  }

  function valueScale(values, unit) {
    const maximum = Math.max(...values.map(Math.abs));
    if (unit === "bps" && maximum >= 1000000) return { divisor: 1000000, unit: "Mb/s" };
    if (unit === "bps" && maximum >= 1000) return { divisor: 1000, unit: "kb/s" };
    return { divisor: 1, unit };
  }

  function formatValue(value, scale) {
    return `${(value / scale.divisor).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${scale.unit}`;
  }

  function svgElement(name, attributes = {}) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
  }

  function summarizeRuns(samples) {
    const groups = new Map();
    samples.forEach(sample => {
      if (!Number.isFinite(Number(sample.value))) return;
      let run = groups.get(sample.run_id);
      if (!run) {
        run = {
          runId: sample.run_id,
          runName: sample.run_name || sample.run_id,
          startTime: sample.run_start_time,
          values: [],
        };
        groups.set(sample.run_id, run);
      }
      run.values.push(Number(sample.value));
    });
    return [...groups.values()].map(run => ({
      ...run,
      median: median(run.values),
      p95: quantile(run.values, .95),
    })).sort((left, right) => String(left.startTime).localeCompare(String(right.startTime)));
  }

  function addLegend(label, color) {
    const item = document.createElement("span");
    item.className = "kpi-chart-legend-item";
    const swatch = document.createElement("i");
    swatch.style.setProperty("--series-color", color);
    const text = document.createElement("span");
    text.textContent = label;
    item.append(swatch, text);
    chartLegend.appendChild(item);
  }

  function renderChart(runs, metricKey, unit) {
    chartElement.replaceChildren();
    chartLegend.replaceChildren();
    chartEmpty.hidden = true;
    chartTitle.textContent = `${metricLabels[metricKey] || metricKey} across all runs`;
    if (!runs.length) {
      chartEmpty.hidden = false;
      chartEmpty.textContent = "No measurements are available for this metric.";
      chartSummary.textContent = "No run history found.";
      return;
    }

    const values = runs.flatMap(run => [run.median, run.p95]);
    const scale = valueScale(values, unit);
    const compact = chartElement.getBoundingClientRect().width < 720;
    const width = compact ? 400 : 1100;
    const height = 390;
    const margin = compact
      ? { top: 18, right: 14, bottom: 72, left: 75 }
      : { top: 24, right: 28, bottom: 62, left: 92 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const minimumTime = new Date(runs[0].startTime).getTime();
    const maximumTime = new Date(runs[runs.length - 1].startTime).getTime();
    const timeSpan = maximumTime - minimumTime || 1;
    const minimumValue = Math.min(0, ...values);
    const maximumValue = Math.max(...values);
    const yMax = maximumValue === minimumValue ? maximumValue + 1 : maximumValue * 1.08;
    const xPosition = run => margin.left + (new Date(run.startTime).getTime() - minimumTime) / timeSpan * plotWidth;
    const yPosition = value => margin.top + (yMax - value) / (yMax - minimumValue || 1) * plotHeight;
    const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, role: "presentation", focusable: "false" });
    const title = svgElement("title");
    title.textContent = `${metricLabels[metricKey] || metricKey} history`;
    svg.appendChild(title);

    for (let tick = 0; tick <= 4; tick += 1) {
      const value = minimumValue + (yMax - minimumValue) * tick / 4;
      const y = yPosition(value);
      svg.appendChild(svgElement("line", {
        x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: "kpi-chart-grid",
      }));
      const label = svgElement("text", {
        x: margin.left - 10, y: y + 4, "text-anchor": "end", class: "kpi-chart-axis-label",
      });
      label.textContent = formatValue(value, scale);
      svg.appendChild(label);
    }
    svg.appendChild(svgElement("line", {
      x1: margin.left, x2: margin.left, y1: margin.top, y2: height - margin.bottom, class: "kpi-chart-axis",
    }));
    svg.appendChild(svgElement("line", {
      x1: margin.left, x2: width - margin.right,
      y1: height - margin.bottom, y2: height - margin.bottom, class: "kpi-chart-axis",
    }));

    const labelCount = Math.min(compact ? 5 : 7, runs.length);
    for (let index = 0; index < labelCount; index += 1) {
      const runIndex = labelCount === 1 ? 0 : Math.round(index * (runs.length - 1) / (labelCount - 1));
      const run = runs[runIndex];
      const label = svgElement("text", {
        x: xPosition(run), y: height - margin.bottom + 22,
        "text-anchor": "middle", class: "kpi-chart-axis-label",
      });
      label.textContent = compact
        ? new Date(run.startTime).toLocaleDateString(undefined, { month: "short", day: "numeric" })
        : formatDate(run.startTime);
      label.appendChild(svgElement("title")).textContent = `${run.runName} · ${formatDate(run.startTime, true)}`;
      svg.appendChild(label);
    }

    [
      { key: "median", label: "Median per run", color: "#087b69" },
      { key: "p95", label: "95th percentile per run", color: "#bd432f" },
    ].forEach(series => {
      addLegend(series.label, series.color);
      const points = runs.map(run => `${xPosition(run)},${yPosition(run[series.key])}`).join(" ");
      if (runs.length > 1) {
        svg.appendChild(svgElement("polyline", {
          points, class: "kpi-chart-line", stroke: series.color,
        }));
      }
      runs.forEach(run => {
        const point = svgElement("circle", {
          cx: xPosition(run), cy: yPosition(run[series.key]), r: 2.8,
          fill: series.color, class: "kpi-chart-point",
        });
        point.appendChild(svgElement("title")).textContent =
          `${run.runName}\n${formatDate(run.startTime, true)}\n${series.label}: ${formatValue(run[series.key], scale)}`;
        svg.appendChild(point);
      });
    });

    chartSummary.textContent = `Median and 95th percentile across all test measurements; ${runs.length.toLocaleString()} points, one per run.`;
    chartElement.appendChild(svg);
  }

  async function loadHistory() {
    if (!metricSelect.value) return;
    const sequence = ++requestSequence;
    const { metricKey, unit } = selectedMetric();
    metricSelect.disabled = true;
    setStatus("Loading all-run history...");
    chartElement.replaceChildren();
    chartEmpty.hidden = true;
    try {
      const result = await fetchPages("/api/kpis/series", {
        target: targetKey,
        metric_key: metricKey,
        unit,
      });
      if (sequence !== requestSequence) return;
      const runs = summarizeRuns(result.rows);
      runCountElement.textContent = runs.length.toLocaleString();
      sampleCountElement.textContent = result.count.toLocaleString();
      firstRunElement.textContent = runs.length ? formatDate(runs[0].startTime) : "-";
      lastRunElement.textContent = runs.length ? formatDate(runs[runs.length - 1].startTime) : "-";
      statsElement.hidden = !runs.length;
      renderChart(runs, metricKey, unit);
      setStatus(runs.length
        ? `Showing ${result.count.toLocaleString()} measurements from ${runs.length.toLocaleString()} runs.`
        : "No KPI samples exist for this metric.");
    } catch (error) {
      if (sequence !== requestSequence) return;
      statsElement.hidden = true;
      chartEmpty.hidden = false;
      chartEmpty.textContent = "Unable to load this KPI history.";
      setStatus(error.message || "Unable to load KPI history.", "error");
    } finally {
      if (sequence === requestSequence) metricSelect.disabled = false;
    }
  }

  async function initialize() {
    try {
      const result = await fetchPages("/api/kpis/catalog", { target: targetKey });
      if (!replaceMetricOptions(result.rows)) {
        metricSelect.replaceChildren(new Option("No metrics available", ""));
        setStatus("This Target has no KPI history yet.");
        chartEmpty.hidden = false;
        chartEmpty.textContent = "No historical KPI measurements are available for this Target.";
        return;
      }
      await loadHistory();
    } catch (error) {
      metricSelect.replaceChildren(new Option("Metric catalog unavailable", ""));
      metricSelect.disabled = true;
      chartEmpty.hidden = false;
      chartEmpty.textContent = "Unable to load the KPI catalog.";
      setStatus(error.message || "Unable to load KPI metrics.", "error");
    }
  }

  metricSelect.addEventListener("change", loadHistory);
  initialize();
})();