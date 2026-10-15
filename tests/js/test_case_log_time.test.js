"use strict";

const fs = require("fs");
const path = require("path");
const staticDir = path.join(__dirname, "../../src/testrift_server/static");

beforeAll(() => {
  window.eval(fs.readFileSync(path.join(staticDir, "comments.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticDir, "test_case_log.js"), "utf8") + `
    window.logTimeTest = {
      setDeltaMode(value) { showDeltaTime = value; },
      refreshLogTimeDisplay,
      refreshCommentThreadVisibility,
      toggleTimeDisplay,
      applyTimeDisplayPreference,
      convertTimesToLocal
    };
  `);
});

beforeEach(() => {
  document.body.innerHTML = '<button id="time-display-toggle"></button><table id="msg_table"><tbody></tbody></table>';
  window.logTimeTest.setDeltaMode(true);
  window.CommentsUI.Log.comments = [];
  window.CommentsUI.Log.commentedOnly = false;
  localStorage.clear();
});

function addRow(milliseconds, index) {
  const row = document.createElement("tr");
  row.className = "log-entry-row";
  row.dataset.logIndex = String(index);
  const cell = document.createElement("td");
  cell.className = "log-time";
  cell.dataset.originalTime = new Date(Date.UTC(2026, 9, 15) + milliseconds).toISOString();
  row.appendChild(cell);
  document.querySelector("#msg_table tbody").appendChild(row);
  return row;
}

function visibleDeltas() {
  return Array.from(document.querySelectorAll(".log-entry-row"))
    .filter(row => !row.hidden && getComputedStyle(row).display !== "none")
    .map(row => row.querySelector(".log-time").textContent);
}

test("uses the previous visible row and resets the first visible row to zero", () => {
  const first = addRow(0, 0);
  const middle = addRow(1000, 1);
  addRow(4000, 2);
  window.logTimeTest.refreshLogTimeDisplay();
  expect(visibleDeltas()).toEqual(["0ms", "+1.00s", "+3.00s"]);

  middle.style.display = "none";
  window.logTimeTest.refreshCommentThreadVisibility();
  expect(visibleDeltas()).toEqual(["0ms", "+4.00s"]);

  first.style.display = "none";
  window.logTimeTest.refreshCommentThreadVisibility();
  expect(visibleDeltas()).toEqual(["0ms"]);
});

test("restores deltas when rows become visible and when the time mode is toggled", () => {
  addRow(0, 0);
  const middle = addRow(1000, 1);
  addRow(4000, 2);
  middle.hidden = true;
  window.logTimeTest.refreshLogTimeDisplay();
  expect(visibleDeltas()).toEqual(["0ms", "+4.00s"]);

  window.logTimeTest.toggleTimeDisplay();
  window.logTimeTest.toggleTimeDisplay();
  expect(visibleDeltas()).toEqual(["0ms", "+4.00s"]);

  middle.hidden = false;
  window.logTimeTest.refreshLogTimeDisplay();
  expect(visibleDeltas()).toEqual(["0ms", "+1.00s", "+3.00s"]);
});

test("keeps DOM order instead of sorting timestamp values", () => {
  addRow(4000, 0);
  addRow(1000, 1);
  addRow(2000, 2);
  window.logTimeTest.applyTimeDisplayPreference();
  expect(visibleDeltas()).toEqual(["0ms", "0ms", "+1.00s"]);
});

test("repeated local-time conversion starts a fresh visible delta chain", () => {
  addRow(0, 0);
  addRow(4000, 1);
  window.logTimeTest.convertTimesToLocal();
  window.logTimeTest.convertTimesToLocal();
  expect(visibleDeltas()).toEqual(["0ms", "+4.00s"]);
});

test("comment-only visibility changes recalculate deltas without a mode toggle", () => {
  [0, 1000, 4000, 9000, 16000, 25000, 36000].forEach(addRow);
  window.CommentsUI.Log.comments = [{ line_start: 5, line_end: 5 }];
  window.CommentsUI.Log.commentedOnly = true;
  window.CommentsUI.Log.applyCommentedFilter();
  expect(visibleDeltas()).toEqual(["0ms", "+7.00s", "+9.00s", "+11.00s"]);

  window.CommentsUI.Log.commentedOnly = false;
  window.CommentsUI.Log.applyCommentedFilter();
  expect(visibleDeltas()).toEqual(["0ms", "+1.00s", "+3.00s", "+5.00s", "+7.00s", "+9.00s", "+11.00s"]);
});

test("an appended hidden row does not change the next visible delta", () => {
  addRow(0, 0);
  addRow(1000, 1).style.display = "none";
  window.logTimeTest.refreshLogTimeDisplay();
  addRow(4000, 2);
  window.logTimeTest.refreshCommentThreadVisibility();
  expect(visibleDeltas()).toEqual(["0ms", "+4.00s"]);
});

test("absolute mode restores original timestamps for hidden rows too", () => {
  const first = addRow(0, 0);
  addRow(1000, 1);
  window.logTimeTest.refreshLogTimeDisplay();
  first.style.display = "none";
  window.logTimeTest.toggleTimeDisplay();
  expect(first.querySelector(".log-time").textContent).not.toBe("0ms");
  expect(first.querySelector(".log-time").dataset.originalTime).toBe("2026-10-15T00:00:00.000Z");
  expect(first.querySelector(".log-time").style.fontFamily).toBe("");
});

test("empty and non-log rows do not advance the reference time", () => {
  addRow(0, 0).style.display = "none";
  window.logTimeTest.refreshLogTimeDisplay();
  const nonLog = addRow(1000, 1);
  nonLog.className = "comment-thread-row";
  addRow(4000, 2);
  window.logTimeTest.refreshLogTimeDisplay();
  expect(visibleDeltas()).toEqual(["0ms"]);
});