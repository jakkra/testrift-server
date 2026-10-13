"use strict";

const fs = require("fs");
const path = require("path");

const SOURCE_PATH = path.join(
  __dirname,
  "..",
  "..",
  "src",
  "testrift_server",
  "static",
  "kpi_grouped_charts.js"
);
const SOURCE = fs.readFileSync(SOURCE_PATH, "utf8");
const PICKER_SOURCE = fs.readFileSync(path.join(path.dirname(SOURCE_PATH), "kpi_testcase_picker.js"), "utf8");
const testNames = [
  "NUnitTest.Family.TestAlpha",
  "NUnitTest.Family.TestBeta",
];
let scrollRequests = [];
let simulatedScrollY = 500;

function response(data) {
  return { ok: true, json: async () => ({ success: true, ...data }) };
}

function installChartMocks() {
  const charts = [];
  window.echarts = {
    init: () => {
      const listeners = new Map();
      const chart = {
        listeners,
        options: null,
        on: (name, callback) => listeners.set(name, callback),
        setOption: option => { chart.options = option; },
        clear: jest.fn(),
        resize: jest.fn(),
        dispatchAction: jest.fn(),
        dispose: jest.fn(),
      };
      charts.push(chart);
      return chart;
    },
  };
  return charts;
}

function installObservers() {
  window.ResizeObserver = class {
    observe() {}
    disconnect() {}
  };
  window.IntersectionObserver = class {
    constructor(callback) { this.callback = callback; }
    observe(target) { this.callback([{ target, isIntersecting: true }]); }
    unobserve() {}
    disconnect() {}
  };
}

function installApi({ names = testNames, value = 10, maxAbsValue = 11, pageSize = 500, dimensionOptions = [], sourceRows = [], historyNames = () => names, targets = [
  { key: "device-a", display_name: "Device A" },
  { key: "device-b", display_name: "Device B" },
] } = {}) {
  const points = names.map((test_name, index) => ({
    run_id: `run-${index}`,
    target_key: "device-a",
    run_name: `Run ${index}`,
    run_start_time: `2026-09-0${index + 1}T00:00:00Z`,
    test_name,
    value: value + index,
    minimum: value - 1 + index,
    maximum: value + 1 + index,
    sample_count: 1,
  }));
  const peerPoints = points.map((point, index) => ({
    ...point,
    run_id: `peer-run-${index}`,
    target_key: "device-b",
    value: point.value + 5,
  }));
  window.fetch = jest.fn(async input => {
    const url = new URL(input, window.location.origin);
    if (url.pathname.endsWith("/api/targets")) {
      return response({ data: targets });
    }
    if (url.pathname.endsWith("/metrics")) {
      return response({ data: url.searchParams.getAll("target").map(target_key => ({
        metric_key: "latency.average", unit: "ms", target_key,
        last_sample: "2026-09-02T00:00:00Z", sample_count: 2,
      })) });
    }
    if (url.pathname.endsWith("/dimension-options")) return response({ data: dimensionOptions });
    if (url.pathname.endsWith("/source-options")) {
      return response({ data: sourceRows, pagination: { count: sourceRows.length } });
    }
    if (url.pathname.endsWith("/history") && url.searchParams.has("catalog_only")) {
      const testcases = historyNames(url.searchParams).map(test_name => ({
          test_name,
          metric_key: "latency.average",
          unit: "ms",
          sample_count: 1,
          max_abs_value: maxAbsValue,
        }));
      const offset = Number(url.searchParams.get("offset") || 0);
      return response({
        testcases: testcases.slice(offset, offset + pageSize),
        pagination: { count: testcases.length },
      });
    }
    if (url.pathname.endsWith("/history")) {
      const allData = url.searchParams.getAll("target").includes("device-b")
        ? [...points, ...peerPoints]
        : points;
      const selectedTestName = url.searchParams.get("test_name");
      const availableNames = historyNames(url.searchParams);
      const data = allData.filter(point => availableNames.includes(point.test_name)
        && (!selectedTestName || point.test_name === selectedTestName));
      const offset = Number(url.searchParams.get("offset") || 0);
      return response({
        selected_test_name: selectedTestName,
        series_test_names: selectedTestName ? [selectedTestName] : names,
        data: data.slice(offset, offset + pageSize),
        pagination: { count: data.length },
      });
    }
    throw new Error(`Unexpected API request: ${url}`);
  });
}

async function loadChart(apiOptions) {
  document.body.innerHTML = `
    <select id="kpi-metric"></select>
    <select id="kpi-testcase" disabled><option value="">Loading test cases...</option></select>
    <details id="kpi-filter-disclosure"><div id="kpi-dimension-filters"></div></details>
    <div id="kpi-range">
      <button data-days="7" aria-pressed="false"></button>
      <button data-days="30" aria-pressed="true"></button>
    </div>
    <div id="kpi-status"></div>
    <div id="kpi-plots"></div>
    <div id="kpi-group-toolbar"><span id="kpi-group-count"></span></div>
    <select id="kpi-group-jump"></select>
    <button id="kpi-groups-expand"></button>
    <button id="kpi-groups-collapse"></button>
    <dialog id="kpi-compare-dialog">
      <p id="kpi-compare-selection"></p>
      <div id="kpi-comparison-targets"></div>
      <p id="kpi-comparison-note"></p>
      <button id="kpi-compare-all"></button>
      <button id="kpi-compare-clear"></button>
      <button id="kpi-compare-apply"></button>
      <button id="kpi-compare-close"></button>
      <button id="kpi-compare-cancel"></button>
    </dialog>
  `;
  const comparisonDialog = document.getElementById("kpi-compare-dialog");
  comparisonDialog.showModal = () => { comparisonDialog.open = true; };
  comparisonDialog.close = () => {
    comparisonDialog.open = false;
    comparisonDialog.dispatchEvent(new Event("close"));
  };
  Object.defineProperty(window, "scrollY", { configurable: true, get: () => simulatedScrollY });
  window.scrollTo = jest.fn((x, y) => { simulatedScrollY = y; });
  window.KPI_TARGET = "device-a";
  window.HTMLElement.prototype.scrollIntoView = function (options) {
    scrollRequests.push({ element: this, options });
  };
  installObservers();
  const charts = installChartMocks();
  installApi(apiOptions);
  window.Fuse = require("fuse.js");
  window.eval(PICKER_SOURCE);
  window.eval(SOURCE);

  for (let attempt = 0; attempt < 20 && !charts[0]?.options; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  if (!charts[0]?.options) throw new Error(`KPI chart did not finish loading: ${document.getElementById("kpi-status").textContent}`);
  return { chart: charts[0], charts, legend: document.querySelector(".kpi-series-legend") };
}

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  throw new Error("Timed out waiting for KPI UI update");
}

describe("KPI grouped chart interactions", () => {
  beforeEach(() => {
    jest.resetModules();
    document.documentElement.removeAttribute("style");
    document.body.innerHTML = "";
    scrollRequests = [];
    simulatedScrollY = 500;
  });

  afterEach(() => jest.restoreAllMocks());

  test("catalog and history requests collect every page", async () => {
    const { chart } = await loadChart({ pageSize: 1 });
    expect(document.getElementById("kpi-testcase").value).toBe("");
    expect(document.getElementById("kpi-testcase-search").value).toBe("All test families");
    expect(chart.options.series).toHaveLength(2);
    expect(chart.options.series.flatMap(series => series.data)).toHaveLength(2);
    expect(window.fetch.mock.calls.filter(([url]) => url.includes("offset=1"))).toHaveLength(2);
  });

  test("dimension options retain exact scalar tokens through labels and outgoing filters", async () => {
    const dimensionOptions = JSON.parse(`[{"dimension_key":"frame_size_bytes","values":[9007199254740992,9007199254740993,-9223372036854775808,9223372036854775807,true,false,"9007199254740993"],"options":[
      {"value":9007199254740992,"filter_token":"9007199254740992"},
      {"value":9007199254740993,"filter_token":"9007199254740993"},
      {"value":-9223372036854775808,"filter_token":"-9223372036854775808"},
      {"value":9223372036854775807,"filter_token":"9223372036854775807"},
      {"value":true,"filter_token":"true"},
      {"value":false,"filter_token":"false"},
      {"value":"9007199254740993","filter_token":"\\\"9007199254740993\\\""}
    ]}]`);
    expect(dimensionOptions[0].values[0]).toBe(dimensionOptions[0].values[1]);
    const { charts } = await loadChart({ dimensionOptions });
    for (const option of dimensionOptions[0].options) {
      const select = document.querySelector('[data-filter-name="dimension.frame_size_bytes"]');
      const rendered = [...select.options].find(item => item.value === option.filter_token);
      expect(rendered).toBeDefined();
      expect(rendered.textContent).toBe(typeof option.value === "string" ? option.value : option.filter_token);
      const chartCount = charts.length;
      window.fetch.mockClear();
      select.value = option.filter_token;
      select.dispatchEvent(new Event("change"));
      await waitFor(() => charts.length > chartCount && charts[charts.length - 1].options);
      const requests = window.fetch.mock.calls.map(([input]) => new URL(input, window.location.origin))
        .filter(url => url.pathname.endsWith("/history"));
      expect(requests).toHaveLength(2);
      requests.forEach(url => expect(url.searchParams.get("dimension.frame_size_bytes")).toBe(option.filter_token));
      expect(document.querySelector('[data-filter-name="dimension.frame_size_bytes"]').value).toBe(option.filter_token);
    }
  });

  test("legacy scalar dimension options retain their natural labels and typed filters", async () => {
    await loadChart({ dimensionOptions: [{ dimension_key: "tls", values: [false, true, 244, "244", { value: 115200 }, { value: "serial" }] }] });
    const select = document.querySelector('[data-filter-name="dimension.tls"]');
    expect([...select.options].map(option => [option.value, option.textContent])).toEqual(expect.arrayContaining([
      ["false", "false"], ["true", "true"], ["244", "244"], ['"244"', "244"],
      ["115200", "115200"], ['"serial"', "serial"],
    ]));
  });

  test("chart chrome inherits the shared site theme", async () => {
    document.documentElement.style.setProperty("--tr-accent", "#123456");
    document.documentElement.style.setProperty("--tr-muted", "#654321");
    document.documentElement.style.setProperty("--tr-border", "#abcdef");
    document.documentElement.style.setProperty("--tr-sidebar-bg", "#112233");
    const { chart } = await loadChart();
    expect(chart.options.color[0]).toBe("#123456");
    expect(chart.options.xAxis.axisLabel.color).toBe("#654321");
    expect(chart.options.yAxis.splitLine.lineStyle.color).toBe("#abcdef");
    expect(chart.options.tooltip.backgroundColor).toBe("#112233");
    expect(chart.options.dataZoom[1].handleStyle.color).toBe("#123456");
  });

  test("zero-only metrics remain visible in the all-metrics overview", async () => {
    const { chart } = await loadChart({ names: ["Checks.Errors"], value: 0, maxAbsValue: 0 });
    expect(chart.options.series[0].data[0].point.value).toBe(0);
    expect(document.querySelectorAll(".kpi-chart-section")).toHaveLength(1);
  });

  test("signed metrics are not clipped at zero", async () => {
    const { chart } = await loadChart({ value: -70 });
    expect(chart.options.series[0].data[0].point.value).toBe(-70);
    expect(chart.options.yAxis.min).toBeUndefined();
  });

  test("unqualified and single-level test groups have readable labels", async () => {
    await loadChart({ names: ["login_latency", "Checks.Errors"] });
    expect(document.getElementById("kpi-plots").textContent).not.toContain("undefined");
    expect(document.getElementById("kpi-testcase").textContent).toContain("Ungrouped");
    expect(document.getElementById("kpi-testcase").textContent).toContain("login_latency");
    expect(window.fetch.mock.calls.some(([url]) => new URL(url, window.location.origin)
      .searchParams.get("test_group") === "")).toBe(true);
  });

  test("seven-day range changes request bounds without changing the default", async () => {
    await loadChart();
    expect(document.querySelector('[data-days="30"]').getAttribute("aria-pressed")).toBe("true");
    window.fetch.mockClear();
    document.querySelector('[data-days="7"]').click();
    await waitFor(() => window.fetch.mock.calls.some(([input]) => input.includes("catalog_only")));
    const [input] = window.fetch.mock.calls.find(([url]) => url.includes("catalog_only"));
    const params = new URL(input, window.location.origin).searchParams;
    expect(Date.parse(params.get("to")) - Date.parse(params.get("from"))).toBe(7 * 86400000);
    expect(document.querySelector('[data-days="7"]').getAttribute("aria-pressed")).toBe("true");
  });

  test("test-case search only reloads charts when a suggestion is selected", async () => {
    await loadChart();
    window.fetch.mockClear();
    const input = document.getElementById("kpi-testcase-search");
    input.focus();
    input.value = "beta family";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    expect(window.fetch).not.toHaveBeenCalled();
    expect(document.querySelectorAll("#kpi-testcase-options [role=option]")).toHaveLength(1);
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await waitFor(() => window.fetch.mock.calls.some(([url]) => url.includes("test_name=")));
    const [url] = window.fetch.mock.calls.find(([request]) => request.includes("test_name="));
    expect(new URL(url, window.location.origin).searchParams.get("test_name")).toBe(testNames[1]);
    expect(input.value).toContain("TestBeta");
  });

  test.each([
    ["range", false, false], ["range", true, false],
    ["dimension", false, false], ["dimension", true, false],
    ["source", false, false], ["source", true, false],
    ["range", false, true],
  ])("%s refresh retains a missing selected test (other tests: %s, explicit metric: %s)", async (filter, hasOtherTests, explicitMetric) => {
    const filteredNames = hasOtherTests ? [testNames[1]] : [];
    const isFiltered = params => filter === "range"
      ? Date.parse(params.get("to")) - Date.parse(params.get("from")) === 7 * 86400000
      : params.has(filter === "dimension" ? "dimension.tls" : "source_role");
    const { charts } = await loadChart({
      dimensionOptions: [{ dimension_key: "tls", values: [true] }],
      sourceRows: [{ source_role: "dut" }],
      historyNames: params => isFiltered(params) ? filteredNames : testNames,
    });
    const changeSelection = async (select, value) => {
      const chartCount = charts.length;
      select.value = value;
      select.dispatchEvent(new Event("change"));
      await waitFor(() => charts.length > chartCount && charts[charts.length - 1].options);
    };
    if (explicitMetric) await changeSelection(document.getElementById("kpi-metric"), "latency.average\tms");
    const testcase = document.getElementById("kpi-testcase");
    await changeSelection(testcase, testNames[0]);
    const selectedLabel = testcase.selectedOptions[0].textContent;
    const changeFilter = async enabled => {
      const chartCount = charts.length;
      if (filter === "range") document.querySelector(`[data-days="${enabled ? 7 : 30}"]`).click();
      else {
        const select = document.querySelector(`[data-filter-name="${filter === "dimension" ? "dimension.tls" : "source_role"}"]`);
        select.value = enabled ? (filter === "dimension" ? "true" : "dut") : "";
        select.dispatchEvent(new Event("change"));
      }
      await waitFor(() => charts.length > chartCount && charts[charts.length - 1].options);
    };
    window.fetch.mockClear();
    await changeFilter(true);
    expect(testcase.value).toBe(testNames[0]);
    expect(testcase.selectedOptions[0].textContent).toBe(selectedLabel);
    expect(document.getElementById("kpi-testcase-search").value).toBe(selectedLabel);
    expect(testcase.disabled).toBe(false);
    expect(document.querySelectorAll(".kpi-chart-section")).toHaveLength(1);
    expect(charts[charts.length - 1].options.series).toHaveLength(0);
    expect(document.querySelector(".kpi-group-loading").textContent).toContain("No KPI history");
    expect(document.querySelector(".kpi-chart-header p").textContent).toContain("0 measurements");
    expect(document.getElementById("kpi-group-count").textContent).toContain(`${filteredNames.length} tests`);
    const historyRequests = () => window.fetch.mock.calls.map(([input]) => new URL(input, window.location.origin))
      .filter(url => url.pathname.endsWith("/history") && !url.searchParams.has("catalog_only"));
    expect(historyRequests()).toHaveLength(1);
    expect(historyRequests()[0].searchParams.get("test_name")).toBe(testNames[0]);
    expect(historyRequests()[0].searchParams.has("test_group")).toBe(false);
    expect(historyRequests()[0].searchParams.get("metric_key")).toBe("latency.average");
    await changeFilter(true);
    expect(testcase.value).toBe(testNames[0]);
    expect(charts[charts.length - 1].options.series).toHaveLength(0);
    await changeFilter(false);
    expect(testcase.value).toBe(testNames[0]);
    expect(charts[charts.length - 1].options.series).toHaveLength(1);
    expect(charts[charts.length - 1].options.series[0].data[0].point.test_name).toBe(testNames[0]);
    expect(document.querySelector(".kpi-group-loading").textContent).toBe("");
    await changeFilter(true);
    const clear = document.querySelector('[aria-label="Clear test case"]');
    expect(clear.disabled).toBe(false);
    const chartCount = charts.length;
    clear.click();
    await waitFor(() => testcase.value === "" && (hasOtherTests
      ? charts.length > chartCount && charts[charts.length - 1].options
      : document.getElementById("kpi-status").textContent.includes("No KPI history")));
    if (hasOtherTests) {
      expect(charts[charts.length - 1].options.series).toHaveLength(1);
      expect(charts[charts.length - 1].options.series[0].data[0].point.test_name).toBe(testNames[1]);
    } else expect(document.querySelectorAll(".kpi-chart-section")).toHaveLength(0);
  });

  test("metric changes explicitly reset a retained missing test selection", async () => {
    const { charts } = await loadChart({
      historyNames: params => Date.parse(params.get("to")) - Date.parse(params.get("from")) === 7 * 86400000
        ? [testNames[1]] : testNames,
    });
    const testcase = document.getElementById("kpi-testcase");
    testcase.value = testNames[0];
    testcase.dispatchEvent(new Event("change"));
    await waitFor(() => charts.length === 2 && charts[1].options);
    document.querySelector('[data-days="7"]').click();
    await waitFor(() => charts.length === 3 && charts[2].options);
    expect(testcase.value).toBe(testNames[0]);
    const metric = document.getElementById("kpi-metric");
    metric.value = "latency.average\tms";
    metric.dispatchEvent(new Event("change"));
    await waitFor(() => charts.length === 4 && charts[3].options);
    expect(testcase.value).toBe("");
    expect([...testcase.options].map(option => option.value)).not.toContain(testNames[0]);
    expect(charts[3].options.series[0].data[0].point.test_name).toBe(testNames[1]);
  });

  test("clicking a data line focuses its matching test series", async () => {
    const { chart, legend } = await loadChart();

    chart.listeners.get("click")({ seriesType: "line", seriesName: "TestAlpha" });

    expect(chart.options.series).toHaveLength(1);
    expect(legend.querySelector('[aria-pressed="true"]').title).toBe(testNames[0]);
    expect(scrollRequests).toContainEqual({
      element: legend.querySelector(`[data-test-name="${testNames[0]}"]`),
      options: { block: "nearest" },
    });

    chart.listeners.get("click")({ seriesType: "line", seriesName: "TestAlpha" });

    expect(chart.options.series).toHaveLength(2);
    expect(scrollRequests).toHaveLength(1);
  });

  test("selecting a sidebar test preserves the legend scroll position", async () => {
    const { legend } = await loadChart();
    legend.scrollTop = 96;

    legend.querySelector(`[title="${testNames[1]}"]`).click();

    expect(legend.scrollTop).toBe(96);
    expect(legend.querySelector('[aria-pressed="true"]').title).toBe(testNames[1]);
  });

  test("comparison rejects 50 peers without changing charts or state and accepts 49", async () => {
    const targets = [
      { key: "device-a", display_name: "Device A" },
      ...Array.from({ length: 50 }, (_, index) => ({ key: `peer-${index}`, display_name: `Peer ${index}` })),
    ];
    const { chart, charts, legend } = await loadChart({ targets });
    legend.querySelector(`[data-test-name="${testNames[0]}"]`).click();
    await waitFor(() => legend.querySelector(".kpi-series-compare"));
    legend.querySelector(".kpi-series-compare").click();
    const dialog = document.getElementById("kpi-compare-dialog");
    const checkboxes = [...dialog.querySelectorAll(".kpi-comparison-target input")];
    expect(checkboxes).toHaveLength(50);
    checkboxes.forEach(checkbox => {
      checkbox.checked = true;
      checkbox.dispatchEvent(new Event("change"));
    });
    const originalOptions = chart.options;
    const originalSection = document.querySelector(".kpi-chart-section");
    window.fetch.mockClear();
    document.getElementById("kpi-compare-apply").click();
    expect(document.getElementById("kpi-comparison-note").textContent).toContain("at most 49");
    expect(document.getElementById("kpi-comparison-note").textContent).toContain("baseline");
    expect(dialog.open).toBe(true);
    expect(window.fetch).not.toHaveBeenCalled();
    expect(chart.dispose).not.toHaveBeenCalled();
    expect(chart.options).toBe(originalOptions);
    expect(charts).toHaveLength(1);
    expect(document.querySelector(".kpi-chart-section")).toBe(originalSection);
    expect(document.getElementById("kpi-metric").value).toBe("");
    expect(document.getElementById("kpi-testcase").value).toBe("");
    expect(document.querySelector(".kpi-clear-comparison")).toBeNull();
    document.getElementById("kpi-compare-all").click();
    expect(document.getElementById("kpi-comparison-note").textContent).toContain("maximum of 50");
    expect(window.fetch).not.toHaveBeenCalled();
    checkboxes[49].checked = false;
    checkboxes[49].dispatchEvent(new Event("change"));
    document.getElementById("kpi-compare-apply").click();
    await waitFor(() => !dialog.open && charts.length > 1 && charts[charts.length - 1].options);
    expect(chart.dispose).toHaveBeenCalledTimes(1);
    const requests = window.fetch.mock.calls.map(([input]) => new URL(input, window.location.origin));
    requests.forEach(url => {
      expect(url.searchParams.getAll("target")).toEqual(targets.slice(0, 50).map(target => target.key));
    });
    expect(document.getElementById("kpi-metric").value).toBe("latency.average\tms");
    expect(document.getElementById("kpi-testcase").value).toBe(testNames[0]);
    document.querySelector(".kpi-clear-comparison").click();
    await waitFor(() => document.getElementById("kpi-metric").value === "" && charts.length > 2
      && charts[charts.length - 1].options);
    expect(document.getElementById("kpi-testcase").value).toBe("");
    const restoredRequests = window.fetch.mock.calls.slice(requests.length)
      .map(([input]) => new URL(input, window.location.origin));
    restoredRequests.forEach(url => expect(url.searchParams.getAll("target")).toEqual(["device-a"]));
  });

  test("comparison finds targets on later pages and clears back to the browse view", async () => {
    const { charts, legend } = await loadChart({ pageSize: 1 });
    const initialChartCount = charts.length;
    const originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;
    jest.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      const rect = originalGetBoundingClientRect.call(this);
      if (!this.classList?.contains("kpi-chart-section")) return rect;
      return { ...rect, top: document.getElementById("kpi-testcase").value ? 80 : 220 };
    });

    legend.querySelector(`[data-test-name="${testNames[0]}"]`).click();
    await waitFor(() => legend.querySelector(".kpi-series-compare"));
    legend.querySelector(".kpi-series-compare").click();

    const dialog = document.getElementById("kpi-compare-dialog");
    expect(dialog.open).toBe(true);
    expect(document.getElementById("kpi-compare-selection").textContent).toContain(testNames[0]);
    expect(document.getElementById("kpi-metric").value).toBe("");
    const compatible = document.getElementById("kpi-compare-all");
    compatible.click();
    await waitFor(() => [...dialog.querySelectorAll(".kpi-comparison-target input")]
      .some(input => input.value === "device-b" && input.checked));
    expect(charts).toHaveLength(initialChartCount);

    document.getElementById("kpi-compare-apply").click();
    await waitFor(() => !dialog.open && charts.length > initialChartCount
      && charts[charts.length - 1].options?.series?.length === 2);
    await waitFor(() => simulatedScrollY === 360);
    expect(document.getElementById("kpi-metric").value).toBe("latency.average\tms");
    expect(document.getElementById("kpi-testcase").value).toBe(testNames[0]);
    expect(document.querySelectorAll(".kpi-chart-section")).toHaveLength(1);
    expect([...document.querySelectorAll(".kpi-path-group")].every(group => group.open)).toBe(true);
    expect(document.querySelector(".kpi-clear-comparison")).not.toBeNull();

    const historyRequests = window.fetch.mock.calls
      .map(([input]) => new URL(input, window.location.origin))
      .filter(url => url.pathname.endsWith("/history"));
    expect(historyRequests.some(url => url.searchParams.get("metric_key") === "latency.average"
      && url.searchParams.get("unit") === "ms"
      && url.searchParams.get("test_name") === testNames[0]
      && url.searchParams.get("offset") === "1"
      && url.searchParams.getAll("target").includes("device-b"))).toBe(true);

    document.querySelector(".kpi-clear-comparison").click();
    await waitFor(() => !document.querySelector(".kpi-clear-comparison")
      && document.getElementById("kpi-metric").value === ""
      && document.getElementById("kpi-testcase").value === ""
      && charts[charts.length - 1].options?.series?.length === 2);
    await waitFor(() => simulatedScrollY === 500);
    expect([...document.querySelectorAll(".kpi-path-group")].every(group => !group.open)).toBe(true);
    expect(window.scrollTo).toHaveBeenCalledTimes(2);
  });

  test("comparison dialog applies selected targets without reloading on checkbox changes", async () => {
    const { charts, legend } = await loadChart();
    const initialChartCount = charts.length;
    legend.querySelector(`[data-test-name="${testNames[0]}"]`).click();
    await waitFor(() => legend.querySelector(".kpi-series-compare"));
    legend.querySelector(".kpi-series-compare").click();
    const peer = [...document.querySelectorAll(".kpi-comparison-target input")]
      .find(input => input.value === "device-b");
    peer.checked = true;
    peer.dispatchEvent(new Event("change"));
    expect(charts).toHaveLength(initialChartCount);

    document.getElementById("kpi-compare-apply").click();
    await waitFor(() => charts.length > initialChartCount
      && charts[charts.length - 1].options.series.length === 2);

    expect(charts[charts.length - 1].options.series.map(series => series.name)).toEqual([
      "Device A · TestAlpha",
      "Device B · TestAlpha",
    ]);
  });
});