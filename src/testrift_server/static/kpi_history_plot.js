(function () {
  "use strict";

  const targetKey = window.KPI_TARGET;
  const metricSelect = document.getElementById("kpi-metric");
  const testcaseSelect = document.getElementById("kpi-testcase");
  const rangeElement = document.getElementById("kpi-range");
  const statusElement = document.getElementById("kpi-status");
  const statsElement = document.getElementById("kpi-history-stats");
  const runCountElement = document.getElementById("kpi-run-count");
  const sampleCountElement = document.getElementById("kpi-sample-count");
  const firstRunElement = document.getElementById("kpi-first-run");
  const lastRunElement = document.getElementById("kpi-last-run");
  const chartTitle = document.getElementById("kpi-chart-title");
  const chartSummary = document.getElementById("kpi-chart-summary");
  const chartElement = document.getElementById("kpi-chart");
  const chartEmpty = document.getElementById("kpi-chart-empty");
  const chartTooltip = document.getElementById("kpi-chart-tooltip");
  const metricLabels = {
    "throughput.tx_throughput": "TX throughput",
    "throughput.rx_throughput": "RX throughput",
  };
  let metricCatalog = new Map();
  let requestSequence = 0;
  let activeController = null;

  function apiUrl(path, params) {
    const url = new URL(path, window.location.origin);
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    });
    return url;
  }

  async function fetchJson(url, signal) {
    const response = await fetch(url, { headers: { Accept: "application/json" }, signal });
    const body = await response.json();
    if (!response.ok || !body.success) {
      throw new Error(body.error || `Request failed (${response.status})`);
    }
    return body;
  }

  function setStatus(message, kind = "info") {
    statusElement.textContent = message || "";
    statusElement.classList.toggle("visible", Boolean(message));
    statusElement.classList.toggle("error", kind === "error");
  }

  function metricLabel(metric) {
    return metricLabels[metric] || metric.replaceAll(".", " / ").replaceAll("_", " ");
  }

  function populateMetrics(metrics) {
    const choices = new Map(metrics.map(item => [`${item.metric_key}\t${item.unit}`, item]));
    metricCatalog = choices;
    const options = [...choices.entries()].sort(([left], [right]) => left.localeCompare(right));
    metricSelect.replaceChildren();
    options.forEach(([value, item]) => {
      const option = new Option(`${metricLabel(item.metric_key)} · ${item.unit}`, value);
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
    return { metricKey, unit, metadata: metricCatalog.get(metricSelect.value) };
  }

  function shortTestName(fullName, depth = 3) {
    return fullName.split(".").slice(-depth).join(".");
  }

  function populateTestcases(testcases, selectedName) {
    const previous = testcaseSelect.value;
    testcaseSelect.replaceChildren();
    const names = testcases.map(item => item.test_name);
    const labels = new Map();
    for (let depth = 2; depth <= 5; depth += 1) {
      names.forEach(name => labels.set(name, shortTestName(name, depth)));
      if (new Set(labels.values()).size === names.length) break;
    }
    testcases.forEach(item => {
      const option = new Option(labels.get(item.test_name) || item.test_name, item.test_name);
      option.title = `${item.test_name} | ${Number(item.run_count).toLocaleString()} runs | ${Number(item.sample_count).toLocaleString()} samples`;
      testcaseSelect.appendChild(option);
    });
    testcaseSelect.disabled = testcases.length === 0;
    const nextName = names.includes(selectedName) ? selectedName
      : names.includes(previous) ? previous : names[0];
    testcaseSelect.value = nextName || "";
    testcaseSelect.title = nextName || "No test cases in this range";
  }

  function activeRangeDays() {
    return rangeElement.querySelector('button[aria-pressed="true"]')?.dataset.days || "30";
  }

  function requestRange() {
    const days = activeRangeDays();
    if (days === "all") return {};
    const latestSample = selectedMetric().metadata?.last_sample;
    const parsedLatest = latestSample ? new Date(latestSample) : null;
    const end = parsedLatest && Number.isFinite(parsedLatest.getTime()) ? parsedLatest : new Date();
    const start = new Date(end.getTime() - Number(days) * 24 * 60 * 60 * 1000);
    return { from: start.toISOString(), to: end.toISOString() };
  }

  function formatDate(value, includeTime = false) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "Date unavailable";
    return date.toLocaleString(undefined, includeTime
      ? { dateStyle: "medium", timeStyle: "short" }
      : { dateStyle: "medium" });
  }

  function formatAxisDate(value, spanDays) {
    const options = spanDays > 180
      ? { month: "short", year: "2-digit" }
      : { month: "short", day: "numeric" };
    return new Date(value).toLocaleDateString(undefined, options);
  }

  function valueScale(values, unit) {
    const maximum = Math.max(...values.map(Math.abs), 0);
    if (unit === "bps" && maximum >= 1000000) return { divisor: 1000000, unit: "Mb/s" };
    if (unit === "bps" && maximum >= 1000) return { divisor: 1000, unit: "kb/s" };
    return { divisor: 1, unit };
  }

  function formatValue(value, scale) {
    return `${(Number(value) / scale.divisor).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${scale.unit}`;
  }

  function svgElement(name, attributes = {}) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
  }

  function updateStats(points, sampleCount) {
    runCountElement.textContent = points.length.toLocaleString();
    sampleCountElement.textContent = sampleCount.toLocaleString();
    firstRunElement.textContent = points.length ? formatDate(points[0].run_start_time) : "-";
    lastRunElement.textContent = points.length ? formatDate(points[points.length - 1].run_start_time) : "-";
    statsElement.hidden = points.length === 0;
  }

  function showTooltip(point, circle) {
    const chartBox = chartElement.getBoundingClientRect();
    const pointBox = circle.getBoundingClientRect();
    chartTooltip.replaceChildren();
    [
      point.run_name || point.run_id,
      formatDate(point.run_start_time, true),
      `Mean: ${formatValue(point.value, point.scale)}`,
      `Range: ${formatValue(point.minimum, point.scale)} to ${formatValue(point.maximum, point.scale)}`,
      `${Number(point.sample_count).toLocaleString()} sample${point.sample_count === 1 ? "" : "s"}`,
    ].forEach((text, index) => {
      const line = document.createElement(index === 0 ? "strong" : "span");
      line.textContent = text;
      chartTooltip.appendChild(line);
    });
    chartTooltip.hidden = false;
    const left = pointBox.left - chartBox.left + pointBox.width / 2;
    const top = pointBox.top - chartBox.top;
    chartTooltip.style.left = `${Math.max(8, Math.min(chartBox.width - 240, left + 12))}px`;
    chartTooltip.style.top = `${Math.max(8, top - chartTooltip.offsetHeight - 10)}px`;
  }

  function renderChart(points, metricKey, unit, testName, requestedRange) {
    chartElement.querySelector("svg")?.remove();
    chartTooltip.hidden = true;
    chartEmpty.hidden = true;
    const testLabel = testName ? shortTestName(testName, 3) : "No test case";
    chartTitle.textContent = `${metricLabel(metricKey)} · ${testLabel}`;
    if (!points.length) {
      chartEmpty.hidden = false;
      chartEmpty.textContent = testName
        ? "No runs contain this test case in the selected date range."
        : "No test cases have KPI measurements in this date range.";
      chartSummary.textContent = "Choose another date range or test case.";
      return;
    }

    const values = points.flatMap(point => [Number(point.minimum), Number(point.value), Number(point.maximum)])
      .filter(Number.isFinite);
    const scale = valueScale(values, unit);
    const compact = chartElement.getBoundingClientRect().width < 720;
    const width = compact ? 480 : 1100;
    const height = compact ? 340 : 390;
    const margin = compact
      ? { top: 20, right: 16, bottom: 55, left: 76 }
      : { top: 24, right: 28, bottom: 58, left: 92 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const pointTimes = points.map(point => new Date(point.run_start_time).getTime());
    const firstTime = pointTimes[0];
    const lastTime = pointTimes[pointTimes.length - 1];
    const requestedStart = requestedRange.from ? new Date(requestedRange.from).getTime() : firstTime;
    const requestedEnd = requestedRange.to ? new Date(requestedRange.to).getTime() : lastTime;
    const timeStart = Math.min(firstTime, requestedStart);
    const timeEnd = Math.max(lastTime, requestedEnd);
    const timeSpan = timeEnd - timeStart || 1;
    const minimum = Math.min(0, ...values);
    const maximum = Math.max(...values);
    const rawStep = (maximum - minimum || Math.abs(maximum) || 1) / 4;
    const magnitude = 10 ** Math.floor(Math.log10(rawStep));
    const normalizedStep = rawStep / magnitude;
    const stepFactor = normalizedStep <= 1 ? 1 : normalizedStep <= 2 ? 2 : normalizedStep <= 5 ? 5 : 10;
    const step = stepFactor * magnitude;
    const yMin = minimum < 0 ? Math.floor(minimum / step) * step : 0;
    const yMax = Math.max(step, Math.ceil(maximum / step) * step);
    const xPosition = time => margin.left + (time - timeStart) / timeSpan * plotWidth;
    const yPosition = value => margin.top + (yMax - value) / (yMax - yMin || 1) * plotHeight;
    const svg = svgElement("svg", {
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": `${metricLabel(metricKey)} for ${testName}, ${points.length} runs`,
      focusable: "false",
    });
    const spanDays = timeSpan / (24 * 60 * 60 * 1000);

    for (let tick = 0; tick <= 4; tick += 1) {
      const value = yMin + (yMax - yMin) * tick / 4;
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

    const tickCount = Math.min(compact ? 4 : 6, Math.max(2, points.length));
    for (let tick = 0; tick < tickCount; tick += 1) {
      const time = timeStart + timeSpan * tick / (tickCount - 1);
      const label = svgElement("text", {
        x: xPosition(time), y: height - margin.bottom + 20,
        "text-anchor": tick === 0 ? "start" : tick === tickCount - 1 ? "end" : "middle",
        class: "kpi-chart-axis-label",
      });
      label.textContent = formatAxisDate(time, spanDays);
      svg.appendChild(label);
    }

    const rangeLines = points.map((point, index) => {
      const x = xPosition(pointTimes[index]);
      return `${x},${yPosition(point.minimum)} ${x},${yPosition(point.maximum)}`;
    });
    rangeLines.forEach(coords => {
      const [minimumPoint, maximumPoint] = coords.split(" ");
      const [x1, y1] = minimumPoint.split(",");
      const [, y2] = maximumPoint.split(",");
      svg.appendChild(svgElement("line", {
        x1, x2: x1, y1, y2, class: "kpi-chart-range",
      }));
    });

    if (points.length > 1) {
      svg.appendChild(svgElement("polyline", {
        points: points.map((point, index) => `${xPosition(pointTimes[index])},${yPosition(point.value)}`).join(" "),
        class: "kpi-chart-line",
      }));
    }
    points.forEach((point, index) => {
      point.scale = scale;
      const circle = svgElement("circle", {
        cx: xPosition(pointTimes[index]),
        cy: yPosition(point.value),
        r: compact ? 3.5 : 4,
        class: "kpi-chart-point",
        tabindex: "0",
        "aria-label": `${formatDate(point.run_start_time, true)}: ${formatValue(point.value, scale)}`,
      });
      circle.appendChild(svgElement("title")).textContent =
        `${formatDate(point.run_start_time, true)}: ${formatValue(point.value, scale)}`;
      circle.addEventListener("pointerenter", () => showTooltip(point, circle));
      circle.addEventListener("pointerleave", () => { chartTooltip.hidden = true; });
      circle.addEventListener("focus", () => showTooltip(point, circle));
      circle.addEventListener("blur", () => { chartTooltip.hidden = true; });
      svg.appendChild(circle);
    });

    chartElement.insertBefore(svg, chartTooltip);
    const rangeLabel = activeRangeDays() === "all"
      ? "all available history"
      : `the last ${activeRangeDays()} days of available history`;
    chartSummary.textContent = `Mean per run with min–max range; ${points.length.toLocaleString()} runs for this test case in ${rangeLabel}.`;
  }

  async function loadHistory() {
    if (!metricSelect.value) return;
    if (activeController) activeController.abort();
    activeController = new AbortController();
    const sequence = ++requestSequence;
    const { metricKey, unit } = selectedMetric();
    const requestedRange = requestRange();
    const params = { target: targetKey, metric_key: metricKey, unit, ...requestedRange };
    if (testcaseSelect.value) params.test_name = testcaseSelect.value;
    setStatus("Loading selected test history...");
    chartEmpty.hidden = true;

    try {
      const result = await fetchJson(apiUrl("/api/kpis/history", params), activeController.signal);
      if (sequence !== requestSequence) return;
      populateTestcases(result.testcases, result.selected_test_name);
      updateStats(result.data, result.summary.sample_count);
      renderChart(result.data, metricKey, unit, result.selected_test_name, requestedRange);
      setStatus(result.data.length
        ? `Showing ${result.summary.sample_count.toLocaleString()} measurements across ${result.summary.run_count.toLocaleString()} runs for ${result.selected_test_name}.`
        : "No KPI measurements match this metric, test case, and date range.");
    } catch (error) {
      if (error.name === "AbortError" || sequence !== requestSequence) return;
      statsElement.hidden = true;
      chartEmpty.hidden = false;
      chartEmpty.textContent = "Unable to load this KPI history.";
      setStatus(error.message || "Unable to load KPI history.", "error");
    }
  }

  async function initialize() {
    try {
      const result = await fetchJson(apiUrl("/api/kpis/metrics", { target: targetKey }));
      if (!populateMetrics(result.data)) {
        metricSelect.replaceChildren(new Option("No metrics available", ""));
        testcaseSelect.replaceChildren(new Option("No test cases available", ""));
        testcaseSelect.disabled = true;
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
  testcaseSelect.addEventListener("change", loadHistory);
  rangeElement.addEventListener("click", event => {
    const button = event.target.closest("button[data-days]");
    if (!button) return;
    rangeElement.querySelectorAll("button[data-days]").forEach(option => {
      option.setAttribute("aria-pressed", String(option === button));
    });
    loadHistory();
  });

  initialize();
})();