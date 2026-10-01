(function () {
  "use strict";

  const targetKey = window.KPI_TARGET;
  const metricSelect = document.getElementById("kpi-metric");
  const testcaseSelect = document.getElementById("kpi-testcase");
  const rangeElement = document.getElementById("kpi-range");
  const statusElement = document.getElementById("kpi-status");
  const statsElement = document.getElementById("kpi-history-stats");
  const chartElement = document.getElementById("kpi-chart");
  const chartEmpty = document.getElementById("kpi-chart-empty");
  const legendElement = document.getElementById("kpi-series-legend");
  const resetZoom = document.getElementById("kpi-reset-zoom");
  const chart = echarts.init(chartElement, null, { renderer: "svg" });
  const colors = ["#0b806a", "#d39415", "#3e80b4", "#bf5b49", "#8464a0", "#788a39", "#2c9396", "#bd7283"];
  const metricLabels = {
    "throughput.tx_throughput": "TX throughput",
    "throughput.rx_throughput": "RX throughput",
  };
  let catalog = new Map();
  let history = null;
  let activeController = null;
  let requestSequence = 0;
  let focusedTest = "";

  function metricLabel(key) {
    return metricLabels[key] || key.replaceAll(".", " / ").replaceAll("_", " ");
  }

  function seriesLabels(names) {
    const terms = name => name.split(".").at(-1)
      .replace(/3000000/g, "")
      .replace(/DutPeripheralToTester/g, "DutToTester")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
      .split(/[^A-Za-z0-9]+/)
      .filter(Boolean);
    const compact = name => {
      const parts = name.split(".");
      const mode = parts.at(-3)?.replace(/AtMode$/, "");
      const protocol = parts.at(-2) === "BtSpsThroughput" ? "BT" : "";
      const ignored = new Set(["binary", "data", "station", "simplex", "interop", "odin", "mode", "as", "to", "3000000"]);
      const words = terms(name).filter(word => !ignored.has(word.toLowerCase()));
      const label = words.join(" ").replace(/\bTcp\b/gi, "TCP").replace(/\bUdp\b/gi, "UDP")
        .replace(/\bAp\b/gi, "AP").replace(/\bDut\b/gi, "DUT").replace(/\bBin\b/gi, "Binary");
      return { label: `${protocol ? `${protocol} ` : ""}${label}`, mode };
    };
    const candidates = new Map(names.map(name => [name, compact(name)]));
    const counts = new Map();
    candidates.forEach(({ label }) => counts.set(label, (counts.get(label) || 0) + 1));
    const labels = new Map();
    candidates.forEach(({ label, mode }, name) => labels.set(
      name,
      counts.get(label) > 1 ? `${mode} · ${label}` : label,
    ));
    return labels;
  }

  function shortName(name) {
    return name.split(".").slice(-2).join(".");
  }

  function dateLabel(value, withTime = false) {
    const date = new Date(value);
    return date.toLocaleString(undefined, withTime
      ? { dateStyle: "medium", timeStyle: "short" }
      : { dateStyle: "medium" });
  }

  function scaleFor(points, unit) {
    const maximum = Math.max(0, ...points.map(point => Number(point.maximum)));
    if (unit === "bps" && maximum >= 1000000) return { divisor: 1000000, label: "MB/s" };
    if (unit === "bps" && maximum >= 1000) return { divisor: 1000, label: "kB/s" };
    return { divisor: 1, label: unit };
  }

  function formatValue(value, scale) {
    return `${(Number(value) / scale.divisor).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${scale.label}`;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
  }

  function setStatus(message, error = false) {
    statusElement.textContent = message;
    statusElement.classList.toggle("visible", Boolean(message));
    statusElement.classList.toggle("error", error);
  }

  function selectedMetric() {
    const [key, unit] = metricSelect.value.split("\t");
    return { key, unit, latest: catalog.get(metricSelect.value)?.last_sample };
  }

  function rangeParams() {
    const days = rangeElement.querySelector('button[aria-pressed="true"]')?.dataset.days || "30";
    if (days === "all") return {};
    const latest = selectedMetric().latest;
    const end = latest && Number.isFinite(Date.parse(latest)) ? new Date(latest) : new Date();
    return {
      from: new Date(end.getTime() - Number(days) * 86400000).toISOString(),
      to: end.toISOString(),
    };
  }

  function populateTestcases(testcases, selection) {
    testcaseSelect.replaceChildren(new Option("Overview (most measured tests)", ""));
    const names = testcases.map(item => item.test_name);
    const labels = new Map();
    for (let depth = 2; depth <= 6; depth += 1) {
      names.forEach(name => labels.set(name, name.split(".").slice(-depth).join(".")));
      if (new Set(labels.values()).size === names.length) break;
    }
    testcases.forEach(item => {
      const option = new Option(labels.get(item.test_name), item.test_name);
      option.title = item.test_name;
      testcaseSelect.appendChild(option);
    });
    testcaseSelect.value = names.includes(selection) ? selection : "";
    testcaseSelect.disabled = testcases.length === 0;
  }

  function render() {
    const { key, unit } = selectedMetric();
    const points = (history?.data || []).filter(point => !focusedTest || point.test_name === focusedTest);
    const names = (history?.series_test_names || []).filter(name => !focusedTest || name === focusedTest);
    const scale = scaleFor(points, unit);
    const uniqueRuns = new Set(points.map(point => point.run_id));
    const samples = points.reduce((sum, point) => sum + point.sample_count, 0);
    const labels = seriesLabels(history?.series_test_names || []);
    const first = points[0]?.run_start_time;
    const last = points[points.length - 1]?.run_start_time;
    document.getElementById("kpi-run-count").textContent = uniqueRuns.size.toLocaleString();
    document.getElementById("kpi-sample-count").textContent = samples.toLocaleString();
    document.getElementById("kpi-first-run").textContent = first ? dateLabel(first) : "-";
    document.getElementById("kpi-last-run").textContent = last ? dateLabel(last) : "-";
    statsElement.hidden = !points.length;
    chartEmpty.hidden = Boolean(points.length);
    chartEmpty.textContent = "No measurements in this date range. Choose another test or range.";
    chartElement.hidden = !points.length;
    document.getElementById("kpi-chart-title").textContent = focusedTest
      ? `${metricLabel(key)} · ${testcaseSelect.selectedOptions[0]?.textContent || shortName(focusedTest)}` : metricLabel(key);
    document.getElementById("kpi-chart-summary").textContent = focusedTest
      ? `${uniqueRuns.size.toLocaleString()} runs · ${focusedTest}`
      : `${names.length} tests · ${uniqueRuns.size.toLocaleString()} runs · mean per run.`;
    setStatus(points.length ? "" : "No KPI measurements match this selection.");

    legendElement.replaceChildren();
    const overview = document.createElement("button");
    overview.type = "button";
    overview.className = "kpi-overview-button";
    overview.textContent = "All shown tests";
    overview.disabled = !focusedTest;
    overview.addEventListener("click", () => selectTest(""));
    legendElement.appendChild(overview);
    (history?.series_test_names || []).forEach((name, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "kpi-series-button";
      button.dataset.testName = name;
      button.setAttribute("aria-pressed", String(focusedTest === name));
      button.title = name;
      const swatch = document.createElement("i");
      swatch.style.background = colors[index % colors.length];
      const label = document.createElement("span");
      label.textContent = labels.get(name) || name;
      button.append(swatch, label);
      button.addEventListener("click", () => selectTest(focusedTest === name ? "" : name));
      legendElement.appendChild(button);
    });

    chart.clear();
    if (!points.length) return;
    chart.setOption({
      animation: false,
      color: colors,
      grid: { top: 28, left: 10, right: 24, bottom: 74, containLabel: true },
      xAxis: { type: "time", axisLine: { lineStyle: { color: "#acb9b7" } }, axisLabel: { color: "#54656a" },
        splitLine: { show: true, lineStyle: { color: "#e7eeec" } } },
      yAxis: { type: "value", min: 0, scale: true, name: scale.label,
        nameTextStyle: { color: "#54656a" }, axisLabel: { color: "#54656a", formatter: value => (value / scale.divisor).toLocaleString() },
        splitLine: { lineStyle: { color: "#e2e9e6" } } },
      tooltip: { trigger: "axis", confine: true, backgroundColor: "#1a3034", borderWidth: 0,
        axisPointer: { type: "cross", snap: true, lineStyle: { color: "#748583", width: 1, type: "dashed" } },
        textStyle: { color: "#fff", fontSize: 12 }, formatter: items => {
          const entries = (Array.isArray(items) ? items : [items]).filter(item => item.data?.point);
          if (!entries.length) return "";
          const firstPoint = entries[0].data.point;
          const heading = `<strong>${escapeHtml(dateLabel(firstPoint.run_start_time, true))}</strong>`;
          return `${heading}<br>${entries.map(item => {
            const point = item.data.point;
            return `${item.marker}<strong>${escapeHtml(labels.get(point.test_name) || point.test_name)}</strong>`
              + `: ${escapeHtml(formatValue(point.value, scale))}`
              + ` <span>(${escapeHtml(formatValue(point.minimum, scale))}–${escapeHtml(formatValue(point.maximum, scale))})</span>`;
          }).join("<br>")}`;
        } },
      dataZoom: [
        { type: "inside", xAxisIndex: 0, filterMode: "none", zoomOnMouseWheel: "shift", moveOnMouseWheel: true },
        { type: "slider", xAxisIndex: 0, filterMode: "none", height: 18, bottom: 18, brushSelect: true,
          borderColor: "#cbd9d4", fillerColor: "#0b806a24", handleStyle: { color: "#0b806a" } },
      ],
      series: names.map(name => ({
        name: labels.get(name) || name, type: "line", showSymbol: false, symbol: "circle", symbolSize: 8,
        lineStyle: { width: 2 }, emphasis: { focus: "series" },
        data: points.filter(point => point.test_name === name).map(point => ({
          value: [point.run_start_time, point.value], point,
        })),
      })),
    });
    chart.resize();
  }

  function selectTest(name) {
    if (!history) return;
    if (name && !history.series_test_names.includes(name)) {
      testcaseSelect.value = name;
      loadHistory();
      return;
    }
    if (!name && history.selected_test_name) {
      testcaseSelect.value = "";
      loadHistory();
      return;
    }
    focusedTest = name;
    testcaseSelect.value = name;
    render();
  }

  async function loadHistory() {
    if (!metricSelect.value) return;
    activeController?.abort();
    activeController = new AbortController();
    const sequence = ++requestSequence;
    const { key, unit } = selectedMetric();
    const requestedTest = testcaseSelect.value;
    const params = new URLSearchParams({ target: targetKey, metric_key: key, unit, ...rangeParams() });
    if (requestedTest) params.set("test_name", requestedTest);
    setStatus("Loading KPI history...");
    try {
      const response = await fetch(`/api/kpis/history?${params}`, { signal: activeController.signal });
      const result = await response.json();
      if (!response.ok || !result.success) throw new Error(result.error || `Request failed (${response.status})`);
      if (sequence !== requestSequence) return;
      history = result;
      focusedTest = result.selected_test_name || "";
      populateTestcases(result.testcases, focusedTest);
      render();
    } catch (error) {
      if (sequence !== requestSequence || error.name === "AbortError") return;
      setStatus(error.message || "Unable to load KPI history.", true);
    }
  }

  metricSelect.addEventListener("change", loadHistory);
  testcaseSelect.addEventListener("change", () => selectTest(testcaseSelect.value));
  rangeElement.addEventListener("click", event => {
    const button = event.target.closest("button[data-days]");
    if (!button) return;
    rangeElement.querySelectorAll("button[data-days]").forEach(option => {
      option.setAttribute("aria-pressed", String(option === button));
    });
    loadHistory();
  });
  legendElement.addEventListener("keydown", event => {
    if (event.key === "Escape" && focusedTest) selectTest("");
  });
  chart.on("click", params => {
    if (params.seriesType === "line" && !focusedTest) {
      const labels = seriesLabels(history?.series_test_names || []);
      selectTest([...labels].find(([, label]) => label === params.seriesName)?.[0] || "");
    }
  });
  resetZoom.addEventListener("click", () => chart.dispatchAction({ type: "dataZoom", start: 0, end: 100 }));
  new ResizeObserver(() => chart.resize()).observe(chartElement);
  window.addEventListener("resize", () => chart.resize());

  (async function initialize() {
    try {
      const response = await fetch(`/api/kpis/metrics?${new URLSearchParams({ target: targetKey })}`);
      const result = await response.json();
      if (!response.ok || !result.success) throw new Error(result.error || `Request failed (${response.status})`);
      catalog = new Map(result.data.map(item => [`${item.metric_key}\t${item.unit}`, item]));
      metricSelect.replaceChildren();
      [...catalog].sort(([left], [right]) => left.localeCompare(right)).forEach(([value, item]) => {
        metricSelect.appendChild(new Option(`${metricLabel(item.metric_key)} · ${item.unit}`, value));
      });
      metricSelect.disabled = !catalog.size;
      if (!catalog.size) {
        setStatus("No KPI measurements are available for this target.");
        return;
      }
      metricSelect.value = [...catalog.keys()].find(key => key.startsWith("throughput.tx_throughput\t")) || metricSelect.value;
      await loadHistory();
    } catch (error) {
      setStatus(error.message || "Unable to load KPI metrics.", true);
    }
  })();
})();