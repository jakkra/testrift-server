(function () {
  "use strict";

  const metricSelect = document.getElementById("kpi-metric");
  const testcaseSelect = document.getElementById("kpi-testcase");
  const testcasePicker = new window.KpiTestcasePicker(testcaseSelect);
  const dimensionFiltersDisclosure = document.getElementById("kpi-filter-disclosure");
  const dimensionFiltersElement = document.getElementById("kpi-dimension-filters");
  const rangeElement = document.getElementById("kpi-range");
  const statusElement = document.getElementById("kpi-status");
  const plotsElement = document.getElementById("kpi-plots");
  const toolbarElement = document.getElementById("kpi-group-toolbar");
  const jumpSelect = document.getElementById("kpi-group-jump");
  const expandGroupsButton = document.getElementById("kpi-groups-expand");
  const collapseGroupsButton = document.getElementById("kpi-groups-collapse");
  const comparisonDialog = document.getElementById("kpi-compare-dialog");
  const comparisonSelectionLabel = document.getElementById("kpi-compare-selection");
  const comparisonTargetsElement = document.getElementById("kpi-comparison-targets");
  const comparisonNote = document.getElementById("kpi-comparison-note");
  const compareAllButton = document.getElementById("kpi-compare-all");
  const compareClearButton = document.getElementById("kpi-compare-clear");
  const compareApplyButton = document.getElementById("kpi-compare-apply");
  const sharedStyles = getComputedStyle(document.documentElement);
  const themeColor = (name, fallback) => sharedStyles.getPropertyValue(name).trim() || fallback;
  const chartTheme = {
    accent: themeColor("--tr-accent", "#667eea"),
    muted: themeColor("--tr-muted", "#6c757d"),
    border: themeColor("--tr-border", "#e9ecef"),
    tooltip: themeColor("--tr-sidebar-bg", "#1f1e2e"),
    onAccent: themeColor("--tr-on-accent", "#fff"),
    focus: themeColor("--tr-focus-ring", "rgba(102, 126, 234, 0.2)"),
  };
  const colors = [chartTheme.accent, "#d39415", "#3e80b4", "#bf5b49", "#8464a0", "#788a39", "#2c9396", "#bd7283"];
  const metricLabels = {
    "throughput.tx_throughput": "TX throughput",
    "throughput.rx_throughput": "RX throughput",
  };
  let metrics = new Map();
  let catalog = [];
  let targetCatalog = [];
  let comparedTargetKeys = new Set();
  let comparisonSelection = null;
  let comparisonDraft = null;
  let comparisonBrowseState = null;
  let targetDisplayNames = new Map([[window.KPI_TARGET, window.KPI_TARGET]]);
  let metricRequest = 0;
  let controller = null;
  let observer = null;
  let generation = 0;
  let loading = 0;
  let queue = [];
  const panels = new Map();
  let pathGroups = [];

  function setStatus(text, error = false) {
    statusElement.textContent = text;
    statusElement.classList.toggle("visible", Boolean(text));
    statusElement.classList.toggle("error", error);
  }

  function latestTimestamp(values) {
    return values.filter(Boolean).reduce((latest, value) => {
      if (!latest) return value;
      const latestTime = Date.parse(latest);
      const valueTime = Date.parse(value);
      return Number.isFinite(valueTime) && (!Number.isFinite(latestTime) || valueTime > latestTime)
        ? value
        : latest;
    }, "");
  }

  function selectedMetric() {
    if (!metricSelect.value) {
      const last = latestTimestamp([...metrics.values()].map(item => item.last_sample));
      return { last };
    }
    const [key, unit] = metricSelect.value.split("\t");
    return { key, unit, last: metrics.get(metricSelect.value)?.last_sample };
  }

  function metricLabel(key, unit) {
    return `${metricLabels[key] || key} · ${unit}`;
  }

  function dateRange() {
    const days = rangeElement.querySelector('button[aria-pressed="true"]')?.dataset.days || "30";
    if (days === "all") return {};
    const last = selectedMetric().last;
    const end = last && Number.isFinite(Date.parse(last)) ? new Date(last) : new Date();
    return { from: new Date(end.getTime() - Number(days) * 86400000).toISOString(), to: end.toISOString() };
  }

  async function fetchJson(path, params, signal) {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (Array.isArray(value)) value.forEach(item => query.append(key, item));
      else query.set(key, value);
    });
    const response = await fetch(`${path}?${query}`, { signal });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.error || `Request failed (${response.status})`);
    return result;
  }

  async function fetchHistory(params, signal) {
    const field = params.catalog_only === "1" ? "testcases" : "data";
    const items = [];
    let offset = 0;
    let result;
    do {
      const page = await fetchJson("/api/kpis/history", { ...params, limit: "500", offset: String(offset) }, signal);
      if (!result) result = page;
      items.push(...page[field]);
      offset += page[field].length;
      if (!page.pagination || offset >= page.pagination.count) break;
      if (!page[field].length || offset > 1000000) throw new Error("History is too large; select a shorter date range.");
    } while (true);
    return { ...result, [field]: items };
  }

  function selectedKpiFilters() {
    const params = {};
    dimensionFiltersElement.querySelectorAll("select[data-filter-name]").forEach(select => {
      if (select.value) params[select.dataset.filterName] = select.value;
    });
    return params;
  }

  function metricFilterParams() {
    if (!metricSelect.value) return {};
    const [metric_key, unit] = metricSelect.value.split("\t");
    return { metric_key, unit };
  }

  function selectedTargetKeys() {
    return [window.KPI_TARGET, ...comparedTargetKeys];
  }

  function targetLabel(targetKey) {
    return targetDisplayNames.get(targetKey) || targetKey;
  }

  function updateComparisonNote(text) {
    const selectedCount = comparisonDraft?.targetKeys.size || 0;
    comparisonNote.textContent = text || `${selectedCount} comparison target${selectedCount === 1 ? "" : "s"} selected. ${targetLabel(window.KPI_TARGET)} is included as the baseline.`;
  }

  function renderComparisonTargets() {
    comparisonTargetsElement.replaceChildren();
    targetCatalog.filter(target => target.key !== window.KPI_TARGET)
      .forEach(target => {
        const label = document.createElement("label");
        label.className = "kpi-comparison-target";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = target.key;
        checkbox.checked = Boolean(comparisonDraft?.targetKeys.has(target.key));
        checkbox.setAttribute("aria-label", `Compare with ${target.display_name}`);
        checkbox.addEventListener("change", () => {
          if (!comparisonDraft) return;
          if (checkbox.checked) comparisonDraft.targetKeys.add(target.key);
          else comparisonDraft.targetKeys.delete(target.key);
          updateComparisonNote();
        });
        const name = document.createElement("span");
        name.textContent = target.display_name;
        label.append(checkbox, name);
        comparisonTargetsElement.appendChild(label);
      });
  }

  async function selectAllCompatibleTargets() {
    if (!comparisonDraft) return;
    const targetKeys = [window.KPI_TARGET, ...targetCatalog.map(target => target.key)
      .filter(key => key !== window.KPI_TARGET)];
    if (targetKeys.length > 50) {
      updateComparisonNote("Select targets individually; a maximum of 50 can be compared at once.");
      return;
    }
    const draft = comparisonDraft;
    const [metric_key, unit] = draft.metricValue.split("\t");
    compareAllButton.disabled = true;
    try {
      const result = await fetchHistory({
        target: targetKeys,
        metric_key,
        unit,
        test_name: draft.testName,
        ...dateRange(),
        ...selectedKpiFilters(),
      });
      if (comparisonDraft !== draft) return;
      draft.targetKeys = new Set(result.data.map(point => point.target_key)
        .filter(key => key && key !== window.KPI_TARGET));
      renderComparisonTargets();
      updateComparisonNote(draft.targetKeys.size
        ? `${draft.targetKeys.size} compatible target${draft.targetKeys.size === 1 ? "" : "s"} selected.`
        : "No other targets have this metric and test in the selected range.");
    } catch (error) {
      if (comparisonDraft === draft) updateComparisonNote(error.message || "Unable to find compatible targets.");
    } finally {
      if (comparisonDraft === draft) compareAllButton.disabled = false;
    }
  }

  async function fetchDimensionOptions(signal) {
    const params = { target: selectedTargetKeys(), ...metricFilterParams(), ...dateRange() };
    const result = await fetchJson("/api/kpis/dimension-options", params, signal);
    return result.data;
  }

  async function fetchSourceCatalog(signal) {
    const params = { target: selectedTargetKeys(), ...metricFilterParams(), ...dateRange() };
    const items = [];
    let offset = 0;
    let total = 0;
    do {
      const page = await fetchJson("/api/kpis/source-options", { ...params, limit: "500", offset: String(offset) }, signal);
      items.push(...page.data);
      total = page.pagination.count;
      offset += page.data.length;
      if (!page.data.length) break;
    } while (offset < total && offset < 1000000);
    return items;
  }

  function dimensionLabel(key) {
    const labels = {
      baudrate: "Baud rate",
      dut_firmware: "DUT firmware",
      dut_model: "DUT model",
      file_size_bytes: "File size (bytes)",
      frame_size_bytes: "Frame size (bytes)",
      tls: "TLS",
      transfer_time_ms: "Transfer time (ms)",
    };
    if (labels[key]) return labels[key];
    return key.split("_").map(part => part.toLowerCase() === "dut" ? "DUT" : `${part[0].toUpperCase()}${part.slice(1)}`).join(" ");
  }

  function renderDimensionFilters(dimensionOptions, sourceRows) {
    const previous = new Map([...dimensionFiltersElement.querySelectorAll("select[data-filter-name]")]
      .map(select => [select.dataset.filterName, select.value]));
    const valuesByDimension = new Map(dimensionOptions.map(({ dimension_key, values }) => [
      dimension_key,
      new Map(values.map(value => [JSON.stringify(value), value])),
    ]));

    dimensionFiltersElement.replaceChildren();
    const addFilter = (labelText, filterName, options) => {
      if (!options.length) return;
      const label = document.createElement("label");
      label.appendChild(document.createTextNode(labelText));
      const select = document.createElement("select");
      select.dataset.filterName = filterName;
      select.setAttribute("aria-label", `Filter by ${labelText}`);
      select.appendChild(new Option("All values", ""));
      options.forEach(([value, text]) => select.appendChild(new Option(text, value)));
      select.value = previous.get(filterName) || "";
      select.addEventListener("change", loadCatalog);
      label.appendChild(select);
      dimensionFiltersElement.appendChild(label);
    };

    [...valuesByDimension].sort(([left], [right]) => left.localeCompare(right)).forEach(([key, values]) => {
      const options = [...values]
        .sort(([, left], [, right]) => String(left).localeCompare(String(right), undefined, { numeric: true }))
        .map(([encoded, value]) => [encoded, String(value)]);
      addFilter(dimensionLabel(key), `dimension.${key}`, options);
    });

    [
      ["source_role", "source_role", "Source role"],
      ["branch", "source_branch", "Build branch"],
      ["revision", "source_revision", "Build revision"],
    ].forEach(([field, filterName, label]) => {
      const options = [...new Set(sourceRows.map(row => row[field]).filter(Boolean))]
        .sort((left, right) => left.localeCompare(right, undefined, { numeric: true }))
        .map(value => [value, value]);
      addFilter(label, filterName, options);
    });
    dimensionFiltersElement.hidden = dimensionFiltersElement.childElementCount === 0;
    dimensionFiltersDisclosure.hidden = dimensionFiltersElement.hidden;
  }

  function groupName(testName) {
    const separator = testName.lastIndexOf(".");
    return separator < 0 ? "" : testName.slice(0, separator);
  }

  function groupLabel(group) {
    if (!group) return "Ungrouped";
    const parts = group.split(".");
    const family = parts[parts.length - 1];
    const parent = parts[parts.length - 2];
    return !parent || parent === family ? family : `${parent} · ${family}`;
  }

  function buildPathTree(items) {
    const root = { children: new Map(), panels: [] };
    items.forEach(item => {
      const parts = item.group.split(".");
      const category = parts.length > 1 ? parts[parts.length - 2] : groupLabel(item.group);
      if (!root.children.has(category)) root.children.set(category, { label: category, children: new Map(), panels: [] });
      root.children.get(category).panels.push(item);
    });
    return root;
  }

  function pathNodeStats(node) {
    const panelsInNode = [...node.panels];
    node.children.forEach(child => panelsInNode.push(...pathNodeStats(child).items));
    return {
      items: panelsInNode,
      plots: panelsInNode.length,
      tests: new Set(panelsInNode.flatMap(item => item.tests.map(test => test.test_name))).size,
    };
  }

  function testLabels(names) {
    const labels = new Map();
    for (let depth = 1; depth <= 8; depth += 1) {
      const values = names.map(name => name.split(".").slice(-depth).join("."));
      values.forEach((value, index) => labels.set(names[index], value));
      if (new Set(values).size === names.length) break;
    }
    return labels;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
  }

  function scaleFor(points, unit) {
    const maximum = Math.max(0, ...points.map(point => Number(point.maximum)));
    if (unit === "bps" && maximum >= 1000000) return { divisor: 1000000, label: "Mb/s" };
    if (unit === "bps" && maximum >= 1000) return { divisor: 1000, label: "kb/s" };
    return { divisor: 1, label: unit };
  }

  function formatValue(value, scale) {
    return `${(Number(value) / scale.divisor).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${scale.label}`;
  }

  function renderPointDetails(panel, point, label, scale) {
    panel.details.replaceChildren();
    if (!point) {
      panel.details.textContent = "Select a point to view its run and test log.";
      return;
    }
    const date = new Date(point.run_start_time).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    const summary = document.createElement("span");
    summary.textContent = `${date} · ${label} · ${formatValue(point.value, scale)}`;
    const links = document.createElement("span");
    links.className = "kpi-point-links";
    const run = document.createElement("a");
    run.href = `/testRun/${encodeURIComponent(point.run_id)}/index.html`;
    run.textContent = `Run ${point.run_name || point.run_id}`;
    links.appendChild(run);
    if (point.test_case_id) {
      const log = document.createElement("a");
      log.href = `/testRun/${encodeURIComponent(point.run_id)}/log/${encodeURIComponent(point.test_case_id)}.html`;
      log.textContent = "Test log";
      links.appendChild(log);
    }
    panel.details.append(summary, links);
  }

  function discardPanels() {
    observer?.disconnect();
    observer = null;
    for (const panel of panels.values()) {
      panel.resizeObserver?.disconnect();
      panel.chart?.dispose();
    }
    panels.clear();
    pathGroups = [];
    queue = [];
    loading = 0;
    plotsElement.replaceChildren();
  }

  function focusSeries(panel, series, revealInLegend = false) {
    const clearingFocus = panel.focusedSeriesKey === series.key;
    const focusedSeriesKey = clearingFocus ? "" : series.key;
    renderPlot(panel, { ...panel.result, focused_series_key: focusedSeriesKey });
    if (revealInLegend && !clearingFocus) {
      [...panel.legend.querySelectorAll(".kpi-series-button")]
        .find(button => button.dataset.seriesKey === series.key)
        ?.scrollIntoView({ block: "nearest" });
    }
  }

  function groupPath(details) {
    const path = [];
    let current = details;
    while (current && current !== plotsElement) {
      if (current.matches?.("details.kpi-path-group")) {
        path.unshift(current.querySelector(":scope > summary .kpi-path-label")?.textContent || "");
      }
      current = current.parentElement;
    }
    return JSON.stringify(path);
  }

  function expandedGroupPaths() {
    return new Set(pathGroups.filter(group => group.open).map(groupPath));
  }

  function restorePlotPosition(selection, anchorTop) {
    const panel = panels.get(`${selection.metricValue}\t${selection.group}`);
    if (!panel) return;
    const offset = panel.section.getBoundingClientRect().top - anchorTop;
    if (Math.abs(offset) > 1) window.scrollTo(0, window.scrollY + offset);
  }

  function startComparison(panel, series) {
    if (series.targetKey !== window.KPI_TARGET) return;
    const metricValue = `${panel.key}\t${panel.unit}`;
    comparisonDraft = {
      metricValue,
      testName: series.testName,
      group: panel.group,
      anchorTop: panel.section.getBoundingClientRect().top,
      targetKeys: new Set(comparedTargetKeys),
    };
    const [metricKey, unit] = metricValue.split("\t");
    comparisonSelectionLabel.textContent = `${metricLabel(metricKey, unit)} · ${series.testName}`;
    renderComparisonTargets();
    updateComparisonNote();
    comparisonDialog.showModal();
  }

  async function applyComparison() {
    if (!comparisonDraft) return;
    const draft = comparisonDraft;
    const selection = {
      metricValue: draft.metricValue,
      testName: draft.testName,
      group: draft.group,
    };
    if (!comparisonBrowseState) {
      comparisonBrowseState = {
        metricValue: metricSelect.value,
        testName: testcaseSelect.value,
        targetKeys: new Set(comparedTargetKeys),
        expandedGroupPaths: expandedGroupPaths(),
      };
    }
    metricSelect.value = draft.metricValue;
    testcaseSelect.value = draft.testName;
    comparedTargetKeys = new Set(draft.targetKeys);
    comparisonSelection = selection;
    comparisonDraft = null;
    compareApplyButton.disabled = true;
    try {
      await loadCatalog();
    } finally {
      if (comparisonDialog.open) comparisonDialog.close();
      compareApplyButton.disabled = false;
      requestAnimationFrame(() => restorePlotPosition(selection, draft.anchorTop));
    }
  }

  async function clearComparison(panel) {
    if (!comparisonBrowseState) return;
    const browseState = comparisonBrowseState;
    const selection = { metricValue: `${panel.key}\t${panel.unit}`, group: panel.group };
    const anchorTop = panel.section.getBoundingClientRect().top;
    comparedTargetKeys = new Set(browseState.targetKeys);
    metricSelect.value = browseState.metricValue;
    testcaseSelect.value = browseState.testName;
    comparisonSelection = null;
    await loadCatalog();
    comparisonBrowseState = null;
    requestAnimationFrame(() => restorePlotPosition(selection, anchorTop));
  }

  function renderPlot(panel, result) {
    panel.result = result;
    renderPointDetails(panel);
    const selectedTestName = result.selected_test_name || "";
    panel.focusedSeriesKey = result.focused_series_key || "";
    const names = result.series_test_names;
    const labels = testLabels(names);
    const { unit } = panel;
    const points = result.data.filter(point => !selectedTestName || point.test_name === selectedTestName);
    const targetKeys = [...new Set(points.map(point => point.target_key || window.KPI_TARGET))];
    const seriesDefinitions = targetKeys.flatMap(targetKey => names
      .filter(name => points.some(point => point.test_name === name && (point.target_key || window.KPI_TARGET) === targetKey))
      .map(name => ({
        key: `${targetKey}\t${name}`,
        targetKey,
        testName: name,
        label: comparedTargetKeys.size ? `${targetLabel(targetKey)} · ${labels.get(name)}` : labels.get(name),
        points: points.filter(point => point.test_name === name && (point.target_key || window.KPI_TARGET) === targetKey),
      })));
    panel.seriesDefinitions = seriesDefinitions;
    const visibleSeries = seriesDefinitions.filter(series => !panel.focusedSeriesKey || series.key === panel.focusedSeriesKey);
    const visiblePoints = points.filter(point => !panel.focusedSeriesKey
      || `${point.target_key || window.KPI_TARGET}\t${point.test_name}` === panel.focusedSeriesKey);
    const visibleTestCount = new Set(visibleSeries.map(series => series.testName)).size;
    const visibleTargetCount = new Set(visibleSeries.map(series => series.targetKey)).size;
    const runCount = new Set(visiblePoints.map(point => point.run_id)).size;
    const measurementCount = visiblePoints.reduce((count, point) => count + point.sample_count, 0);
    panel.summary.textContent = comparedTargetKeys.size
      ? `${visibleTestCount} test${visibleTestCount === 1 ? "" : "s"} across ${visibleTargetCount} target${visibleTargetCount === 1 ? "" : "s"} · ${runCount.toLocaleString()} runs · ${measurementCount.toLocaleString()} measurements`
      : `${visibleTestCount} test${visibleTestCount === 1 ? "" : "s"} · ${runCount.toLocaleString()} runs · ${measurementCount.toLocaleString()} measurements`;
    const legendScrollTop = panel.legend.scrollTop;
    panel.legend.replaceChildren();
    if (panel.focusedSeriesKey || (!comparedTargetKeys.size && selectedTestName)) {
      const back = document.createElement("button");
      back.type = "button";
      back.textContent = comparedTargetKeys.size ? "All selected targets for this test" : "All tests in this family";
      back.addEventListener("click", () => {
        if (panel.focusedSeriesKey) {
          renderPlot(panel, { ...panel.result, focused_series_key: "" });
        } else if (testcaseSelect.value) {
          testcaseSelect.value = "";
          loadCatalog();
        } else {
          renderPlot(panel, { ...panel.result, focused_series_key: "" });
        }
      });
      panel.legend.appendChild(back);
    }
    visibleSeries.forEach(series => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "kpi-series-button";
      button.dataset.testName = series.testName;
      button.dataset.targetKey = series.targetKey;
      button.dataset.seriesKey = series.key;
      button.title = comparedTargetKeys.size ? `${targetLabel(series.targetKey)} · ${series.testName}` : series.testName;
      button.setAttribute("aria-pressed", String(panel.focusedSeriesKey === series.key
        || (!panel.focusedSeriesKey && selectedTestName === series.testName)));
      const swatch = document.createElement("i");
      const colorIndex = comparedTargetKeys.size
        ? Math.max(0, selectedTargetKeys().indexOf(series.targetKey))
        : names.indexOf(series.testName);
      swatch.style.background = colors[colorIndex % colors.length];
      const label = document.createElement("span");
      label.textContent = series.label;
      button.append(swatch, label);
      button.addEventListener("click", () => focusSeries(panel, series));
      const entry = document.createElement("div");
      entry.className = "kpi-series-entry";
      entry.appendChild(button);
      const isSelected = panel.focusedSeriesKey === series.key
        || (!panel.focusedSeriesKey && selectedTestName === series.testName);
      if (series.targetKey === window.KPI_TARGET && isSelected) {
        const compare = document.createElement("button");
        compare.type = "button";
        compare.className = "kpi-series-compare";
        compare.textContent = "Compare";
        compare.title = "Compare this test across targets";
        compare.setAttribute("aria-label", `Compare ${series.testName} across targets`);
        compare.addEventListener("click", () => startComparison(panel, series));
        entry.appendChild(compare);
      }
      panel.legend.appendChild(entry);
    });
    panel.legend.scrollTop = legendScrollTop;

    if (!panel.chart) {
      panel.chart = echarts.init(panel.plot, null, { renderer: "canvas" });
      panel.resizeObserver = new ResizeObserver(() => panel.chart.resize());
      panel.resizeObserver.observe(panel.plot);
      panel.chart.on("click", event => {
        if (event.seriesType !== "line") return;
        const series = panel.seriesDefinitions.find(item => item.label === event.seriesName);
        if (!event.data?.point) {
          if (series) focusSeries(panel, series, true);
          return;
        }
        const point = event.data.point;
        renderPointDetails(panel, point, series?.label || testLabels(panel.result.series_test_names).get(point.test_name), panel.scale);
        panel.chart.dispatchAction({ type: "hideTip" });
      });
    }
    const scale = scaleFor(points, unit);
    panel.scale = scale;
    panel.chart.clear();
    panel.chart.setOption({
      animation: false,
      color: visibleSeries.map(series => {
        const colorIndex = comparedTargetKeys.size
          ? Math.max(0, selectedTargetKeys().indexOf(series.targetKey))
          : names.indexOf(series.testName);
        return colors[colorIndex % colors.length];
      }),
      grid: { top: 28, left: 10, right: 20, bottom: 74, containLabel: true },
      xAxis: { type: "time", axisLabel: { color: chartTheme.muted }, splitLine: { show: true, lineStyle: { color: chartTheme.border } } },
      yAxis: { type: "value", name: scale.label, nameTextStyle: { color: chartTheme.muted },
        axisLabel: { color: chartTheme.muted, formatter: value => (value / scale.divisor).toLocaleString() },
        splitLine: { lineStyle: { color: chartTheme.border } } },
      tooltip: { trigger: "axis", confine: true,
        extraCssText: "width:min(340px,calc(100% - 16px));max-height:220px;overflow-y:auto;overflow-x:hidden;box-sizing:border-box;white-space:normal;",
        backgroundColor: chartTheme.tooltip, borderWidth: 0, textStyle: { color: chartTheme.onAccent, fontSize: 12 },
        axisPointer: { type: "cross", snap: true }, formatter: items => {
          const entries = items.filter(item => item.data?.point);
          if (!entries.length) return "";
          const date = new Date(entries[0].data.point.run_start_time).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
          return `<strong class="kpi-tooltip-date">${escapeHtml(date)}</strong>${entries.map(item => {
            const point = item.data.point;
            const label = escapeHtml(labels.get(point.test_name));
            return `<div class="kpi-tooltip-row">${item.marker}<span class="kpi-tooltip-name" title="${label}">${label}</span><span class="kpi-tooltip-value">${escapeHtml(formatValue(point.value, scale))}</span></div>`;
          }).join("")}`;
        } },
      dataZoom: [
        { type: "inside", xAxisIndex: 0, filterMode: "none", zoomOnMouseWheel: "shift" },
        { type: "slider", xAxisIndex: 0, filterMode: "none", height: 18, bottom: 18,
          borderColor: chartTheme.border, fillerColor: chartTheme.focus, handleStyle: { color: chartTheme.accent } },
      ],
      series: visibleSeries.map(series => ({
        name: series.label, type: "line", triggerLineEvent: true,
        showSymbol: series.points.length === 1,
        symbol: "circle", symbolSize: 7,
        lineStyle: { width: 1.7 }, emphasis: { focus: "series" },
        data: series.points.map(point => ({
          value: [point.run_start_time, point.value], point,
        })),
      })),
    });
    panel.chart.resize();
  }

  function drainQueue() {
    while (loading < 2 && queue.length) {
      const panel = queue.shift();
      const sequence = generation;
      loading += 1;
      const params = { target: selectedTargetKeys(), metric_key: panel.key, unit: panel.unit, ...dateRange(), ...selectedKpiFilters() };
      if (testcaseSelect.value) params.test_name = testcaseSelect.value;
      else params.test_group = panel.group;
      fetchHistory(params, controller.signal).then(result => {
        if (sequence !== generation) return;
        panel.loading.textContent = "";
        renderPlot(panel, result);
      }).catch(error => {
        if (sequence !== generation || error.name === "AbortError") return;
        panel.loading.textContent = `Unable to load this test family: ${error.message}`;
        panel.queued = false;
      }).finally(() => {
        if (sequence !== generation) return;
        loading -= 1;
        drainQueue();
      });
    }
  }

  function queuePanel(panel) {
    if (panel.queued) return;
    panel.queued = true;
    panel.loading.textContent = "Loading test family...";
    queue.push(panel);
    drainQueue();
  }

  function loadNearbyPanels() {
    const edge = window.innerHeight + 350;
    for (const panel of panels.values()) {
      if (panel.queued || panel.result) continue;
      const rect = panel.section.getBoundingClientRect();
      if (!panel.section.getClientRects().length) continue;
      if (rect.top <= edge && rect.bottom >= -350) queuePanel(panel);
    }
  }

  function addPanel(group, tests, metricValue, index) {
    const [key, unit] = metricValue.split("\t");
    const titleText = metricSelect.value ? groupLabel(group) : `${groupLabel(group)} · ${metricLabel(key, unit)}`;
    const section = document.createElement("section");
    section.className = "kpi-chart-section kpi-group-section";
    section.id = `kpi-group-${index}`;
    const header = document.createElement("header");
    header.className = "kpi-chart-header";
    const text = document.createElement("div");
    const title = document.createElement("h2");
    title.textContent = titleText;
    const summary = document.createElement("p");
    summary.textContent = `${tests.length} test${tests.length === 1 ? "" : "s"} · ${tests.reduce((count, test) => count + test.sample_count, 0).toLocaleString()} measurements`;
    text.append(title, summary);
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "kpi-chart-reset";
    reset.title = "Reset chart zoom";
    reset.setAttribute("aria-label", `Reset zoom for ${titleText}`);
    reset.innerHTML = '<i class="fas fa-expand-arrows-alt" aria-hidden="true"></i>';
    const actions = document.createElement("div");
    actions.className = "kpi-chart-actions";
    actions.appendChild(reset);
    if (comparisonBrowseState && (comparisonSelection || comparedTargetKeys.size)) {
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "kpi-clear-comparison";
      clear.textContent = "Clear comparison";
      clear.addEventListener("click", () => clearComparison(panel));
      actions.appendChild(clear);
    }
    header.append(text, actions);
    const layout = document.createElement("div");
    layout.className = "kpi-plot-layout";
    const plot = document.createElement("div");
    plot.className = "kpi-chart";
    plot.setAttribute("role", "img");
    plot.setAttribute("aria-label", `KPI history for ${titleText}`);
    const legend = document.createElement("nav");
    legend.className = "kpi-series-legend";
    legend.setAttribute("aria-label", `Test cases in ${titleText}`);
    layout.append(plot, legend);
    const details = document.createElement("div");
    details.className = "kpi-point-details";
    details.setAttribute("aria-label", "Selected KPI sample");
    details.setAttribute("aria-live", "polite");
    const loadingElement = document.createElement("p");
    loadingElement.className = "kpi-group-loading";
    section.append(header, layout, details, loadingElement);
    plotsElement.appendChild(section);
    const panel = { group, key, unit, section, summary, plot, legend, details, loading: loadingElement, chart: null, queued: false };
    const panelId = `${metricValue}\t${group}`;
    section.dataset.panel = panelId;
    panels.set(panelId, panel);
    reset.addEventListener("click", () => panel.chart?.dispatchAction({ type: "dataZoom", start: 0, end: 100 }));
    loadingElement.addEventListener("click", () => { if (!panel.queued) queuePanel(panel); });
    observer.observe(section);
    return panel;
  }

  async function loadCatalog() {
    generation += 1;
    controller?.abort();
    controller = new AbortController();
    discardPanels();
    const sequence = generation;
    toolbarElement.hidden = true;
    setStatus("Loading test families...");
    try {
      const [dimensionOptions, sourceRows] = await Promise.all([
        fetchDimensionOptions(controller.signal),
        fetchSourceCatalog(controller.signal),
      ]);
      if (sequence !== generation) return;
      renderDimensionFilters(dimensionOptions, sourceRows);
      const result = await fetchHistory({
        target: selectedTargetKeys(), ...dateRange(), ...selectedKpiFilters(), catalog_only: "1",
      }, controller.signal);
      if (sequence !== generation) return;
      catalog = result.testcases;
      const selected = testcaseSelect.value;
      const metricCatalog = catalog.filter(test =>
        !metricSelect.value || `${test.metric_key}\t${test.unit}` === metricSelect.value
      );
      testcaseSelect.replaceChildren(new Option("All test families", ""));
      const testNames = [...new Set(metricCatalog.map(test => test.test_name))];
      const labels = testLabels(testNames);
      testNames.forEach(name => {
        const option = new Option(`${groupLabel(groupName(name))} · ${labels.get(name)}`, name);
        option.title = name;
        testcaseSelect.appendChild(option);
      });
      testcaseSelect.value = testNames.includes(selected) ? selected : "";
      testcaseSelect.disabled = !testNames.length;
      testcasePicker.refresh();
      if (comparedTargetKeys.size && (!metricSelect.value || !testcaseSelect.value)) {
        setStatus("Choose one metric and one test case to compare targets.");
        return;
      }
      const groups = new Map();
      metricCatalog.filter(test => !testcaseSelect.value || test.test_name === testcaseSelect.value).forEach(test => {
        const group = groupName(test.test_name);
        const metricValue = `${test.metric_key}\t${test.unit}`;
        const panelId = `${metricValue}\t${group}`;
        if (!groups.has(panelId)) groups.set(panelId, { group, metricValue, tests: [] });
        groups.get(panelId).tests.push(test);
      });
      const ordered = [...groups.values()]
        .sort((left, right) => left.group.localeCompare(right.group) || left.metricValue.localeCompare(right.metricValue));
      jumpSelect.replaceChildren();
      observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (!entry.isIntersecting) return;
          const panel = panels.get(entry.target.dataset.panel);
          if (panel) {
            observer.unobserve(entry.target);
            queuePanel(panel);
          }
        });
      }, { rootMargin: "350px 0px" });
      let panelIndex = 0;
      function renderPathGroups(root, parent) {
        root.children.forEach(group => {
          const details = document.createElement("details");
          details.className = "kpi-path-group";
          const summary = document.createElement("summary");
          summary.className = "kpi-path-summary";
          const label = document.createElement("span");
          label.className = "kpi-path-label";
          label.textContent = group.label;
          const stats = pathNodeStats(group);
          const count = document.createElement("span");
          count.className = "kpi-path-count";
          count.textContent = `${stats.plots} plot${stats.plots === 1 ? "" : "s"} · ${stats.tests} test${stats.tests === 1 ? "" : "s"}`;
          summary.append(label, count);
          const content = document.createElement("div");
          content.className = "kpi-path-contents";
          details.append(summary, content);
          details.addEventListener("toggle", () => {
            if (!details.open) return;
            requestAnimationFrame(() => {
              stats.items.forEach(item => panels.get(`${item.metricValue}\t${item.group}`)?.chart?.resize());
              loadNearbyPanels();
            });
          });
          pathGroups.push(details);
          parent.appendChild(details);
          stats.items.forEach(item => {
            const panel = addPanel(item.group, item.tests, item.metricValue, panelIndex++);
            panel.section.dataset.group = item.group;
            content.appendChild(panel.section);
            jumpSelect.appendChild(new Option(metricSelect.value ? groupLabel(item.group) : `${groupLabel(item.group)} · ${metricLabel(panel.key, panel.unit)}`, panel.section.id));
          });
        });
      }
      renderPathGroups(buildPathTree(ordered), plotsElement);
      const comparisonActive = Boolean(comparisonSelection || comparedTargetKeys.size);
      if (comparisonActive) pathGroups.forEach(group => { group.open = true; });
      else if (comparisonBrowseState) {
        pathGroups.forEach(group => {
          group.open = comparisonBrowseState.expandedGroupPaths.has(groupPath(group));
        });
      }
      loadNearbyPanels();
      if (comparisonActive) {
        requestAnimationFrame(() => {
          panels.forEach(panel => panel.chart?.resize());
          loadNearbyPanels();
        });
      }
      toolbarElement.hidden = !ordered.length;
      const familyCount = new Set(ordered.map(panel => panel.group)).size;
      document.getElementById("kpi-group-count").textContent = `${familyCount} test famil${familyCount === 1 ? "y" : "ies"} · ${ordered.length} plots · ${testNames.length} tests`;
      setStatus(ordered.length ? "" : "No KPI history is available in this date range.");
    } catch (error) {
      if (sequence !== generation || error.name === "AbortError") return;
      setStatus(error.message || "Unable to load test families.", true);
    }
  }

  metricSelect.addEventListener("change", () => {
    comparisonSelection = null;
    testcaseSelect.value = "";
    loadCatalog();
  });
  testcaseSelect.addEventListener("change", () => {
    comparisonSelection = null;
    loadCatalog();
  });
  rangeElement.addEventListener("click", event => {
    const button = event.target.closest("button[data-days]");
    if (!button) return;
    rangeElement.querySelectorAll("button[data-days]").forEach(item => {
      item.setAttribute("aria-pressed", String(item === button));
    });
    loadCatalog();
  });
  jumpSelect.addEventListener("change", () => {
    const section = document.getElementById(jumpSelect.value);
    if (!section) return;
    const panel = panels.get(section.dataset.panel);
    if (panel) queuePanel(panel);
    let ancestor = section.parentElement;
    while (ancestor && ancestor !== plotsElement) {
      if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
      ancestor = ancestor.parentElement;
    }
    requestAnimationFrame(() => {
      panel?.chart?.resize();
      loadNearbyPanels();
      section.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  });
  expandGroupsButton.addEventListener("click", () => {
    pathGroups.forEach(group => { group.open = true; });
    requestAnimationFrame(() => {
      panels.forEach(panel => panel.chart?.resize());
      loadNearbyPanels();
    });
  });
  collapseGroupsButton.addEventListener("click", () => pathGroups.forEach(group => { group.open = false; }));
  compareAllButton.addEventListener("click", selectAllCompatibleTargets);
  compareClearButton.addEventListener("click", () => {
    if (!comparisonDraft) return;
    comparisonDraft.targetKeys.clear();
    renderComparisonTargets();
    updateComparisonNote();
  });
  compareApplyButton.addEventListener("click", applyComparison);
  document.getElementById("kpi-compare-close").addEventListener("click", () => comparisonDialog.close());
  document.getElementById("kpi-compare-cancel").addEventListener("click", () => comparisonDialog.close());
  comparisonDialog.addEventListener("close", () => {
    comparisonDraft = null;
    compareAllButton.disabled = false;
    compareApplyButton.disabled = false;
  });
  comparisonDialog.addEventListener("click", event => {
    if (event.target === comparisonDialog) comparisonDialog.close();
  });
  window.addEventListener("scroll", loadNearbyPanels, { passive: true });
  window.addEventListener("resize", () => {
    panels.forEach(panel => panel.chart?.resize());
    loadNearbyPanels();
  });

  (async function initialize() {
    try {
      const result = await fetchJson("/api/targets", {});
      targetCatalog = result.data || [];
      targetCatalog.forEach(target => targetDisplayNames.set(target.key, target.display_name || target.key));
      await loadMetricOptions();
    } catch (error) {
      setStatus(error.message || "Unable to load KPI metrics.", true);
    }
  })();

  async function loadMetricOptions() {
    const request = ++metricRequest;
    const previous = metricSelect.value;
    const result = await fetchJson("/api/kpis/metrics", { target: selectedTargetKeys() });
    if (request !== metricRequest) return;
    const merged = new Map();
    result.data.forEach(item => {
      const value = `${item.metric_key}\t${item.unit}`;
      const existing = merged.get(value);
      if (!existing) merged.set(value, { ...item });
      else {
        existing.last_sample = latestTimestamp([existing.last_sample, item.last_sample]);
        existing.sample_count += item.sample_count;
      }
    });
    metrics = merged;
    metricSelect.replaceChildren(new Option("All metrics", ""));
    [...metrics].sort(([left], [right]) => left.localeCompare(right)).forEach(([value, item]) => {
      metricSelect.appendChild(new Option(metricLabel(item.metric_key, item.unit), value));
    });
    metricSelect.value = metrics.has(previous) ? previous : "";
    metricSelect.disabled = !metrics.size;
    if (!metrics.size) {
      discardPanels();
      setStatus("No KPI history is available for the selected targets.");
      return;
    }
    await loadCatalog();
  }
})();