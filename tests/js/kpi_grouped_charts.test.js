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

function installApi() {
  const points = testNames.map((test_name, index) => ({
    run_id: `run-${index}`,
    target_key: "nora-b27x",
    run_name: `Run ${index}`,
    run_start_time: `2026-09-0${index + 1}T00:00:00Z`,
    test_name,
    value: 10 + index,
    minimum: 9 + index,
    maximum: 11 + index,
    sample_count: 1,
  }));
  const peerPoints = points.map((point, index) => ({
    ...point,
    run_id: `peer-run-${index}`,
    target_key: "nora-b26x",
    value: point.value + 5,
  }));
  window.fetch = jest.fn(async input => {
    const url = new URL(input, window.location.origin);
    if (url.pathname.endsWith("/api/targets")) {
      return response({ data: [
        { key: "nora-b27x", display_name: "NORA-B27X" },
        { key: "nora-b26x", display_name: "NORA-B26X" },
      ] });
    }
    if (url.pathname.endsWith("/metrics")) {
      return response({ data: url.searchParams.getAll("target").map(target_key => ({
        metric_key: "latency.average", unit: "ms", target_key,
        last_sample: "2026-09-02T00:00:00Z", sample_count: 2,
      })) });
    }
    if (url.pathname.endsWith("/dimension-options")) return response({ data: [] });
    if (url.pathname.endsWith("/source-options")) {
      return response({ data: [], pagination: { count: 0 } });
    }
    if (url.pathname.endsWith("/history") && url.searchParams.has("catalog_only")) {
      return response({
        testcases: testNames.map(test_name => ({
          test_name,
          metric_key: "latency.average",
          unit: "ms",
          sample_count: 1,
          max_abs_value: 11,
        })),
      });
    }
    if (url.pathname.endsWith("/history")) {
      const allData = url.searchParams.getAll("target").includes("nora-b26x")
        ? [...points, ...peerPoints]
        : points;
      const selectedTestName = url.searchParams.get("test_name");
      const data = allData.filter(point => !selectedTestName || point.test_name === selectedTestName);
      return response({
        selected_test_name: selectedTestName,
        series_test_names: selectedTestName ? [selectedTestName] : testNames,
        data,
      });
    }
    throw new Error(`Unexpected API request: ${url}`);
  });
}

async function loadChart() {
  document.body.innerHTML = `
    <select id="kpi-metric"></select>
    <select id="kpi-testcase"></select>
    <details id="kpi-filter-disclosure"><div id="kpi-dimension-filters"></div></details>
    <div id="kpi-range"><button data-days="30" aria-pressed="true"></button></div>
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
  window.KPI_TARGET = "nora-b27x";
  window.HTMLElement.prototype.scrollIntoView = function (options) {
    scrollRequests.push({ element: this, options });
  };
  installObservers();
  const charts = installChartMocks();
  installApi();
  window.eval(SOURCE);

  for (let attempt = 0; attempt < 20 && !charts[0]?.options; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  if (!charts[0]?.options) throw new Error("KPI chart did not finish loading");
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
    document.body.innerHTML = "";
    scrollRequests = [];
    simulatedScrollY = 500;
  });

  afterEach(() => jest.restoreAllMocks());

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

  test("comparing a focused sidebar series stages targets and clears back to the browse view", async () => {
    const { charts, legend } = await loadChart();
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
      .some(input => input.value === "nora-b26x" && input.checked));
    expect(charts).toHaveLength(initialChartCount);

    document.getElementById("kpi-compare-apply").click();
    await waitFor(() => !dialog.open && charts.length > initialChartCount
      && charts.at(-1).options?.series?.length === 2);
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
      && url.searchParams.getAll("target").includes("nora-b26x"))).toBe(true);

    document.querySelector(".kpi-clear-comparison").click();
    await waitFor(() => !document.querySelector(".kpi-clear-comparison")
      && document.getElementById("kpi-metric").value === ""
      && document.getElementById("kpi-testcase").value === ""
      && charts.at(-1).options?.series?.length === 2);
    await waitFor(() => simulatedScrollY === 500);
    expect([...document.querySelectorAll(".kpi-path-group")].every(group => !group.open)).toBe(true);
    expect(window.scrollTo).toHaveBeenCalledTimes(2);
  });

  test("comparison dialog applies selected products without reloading on checkbox changes", async () => {
    const { charts, legend } = await loadChart();
    const initialChartCount = charts.length;
    legend.querySelector(`[data-test-name="${testNames[0]}"]`).click();
    await waitFor(() => legend.querySelector(".kpi-series-compare"));
    legend.querySelector(".kpi-series-compare").click();
    const peer = [...document.querySelectorAll(".kpi-comparison-target input")]
      .find(input => input.value === "nora-b26x");
    peer.checked = true;
    peer.dispatchEvent(new Event("change"));
    expect(charts).toHaveLength(initialChartCount);

    document.getElementById("kpi-compare-apply").click();
    await waitFor(() => charts.length > initialChartCount
      && charts.at(-1).options.series.length === 2);

    expect(charts.at(-1).options.series.map(series => series.name)).toEqual([
      "NORA-B27X · TestAlpha",
      "NORA-B26X · TestAlpha",
    ]);
  });
});