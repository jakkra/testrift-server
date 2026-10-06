"use strict";

const fs = require("fs");
const path = require("path");
const source = fs.readFileSync(path.join(__dirname, "../../src/testrift_server/static/kpi_testcase_picker.js"), "utf8");

let picker;
let select;

function search(text) {
  picker.input.focus();
  picker.input.value = text;
  picker.input.dispatchEvent(new Event("input", { bubbles: true }));
  return [...picker.list.children].map(option => option.textContent);
}

function key(name) {
  picker.input.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true }));
}

beforeEach(() => {
  document.body.innerHTML = '<label for="testcases">Test case</label><select id="testcases"></select>';
  select = document.getElementById("testcases");
  select.append(
    new Option("All test families", ""),
    new Option("Station TCP", "NUnitTest.DirectAtMode.StationTcpSimplex"),
    new Option("Station UDP", "NUnitTest.DirectAtMode.StationUdpSimplex"),
    new Option("AP TCP", "NUnitTest.DirectAtMode.ApTcpSimplex"),
    new Option("Statoin TCP", "NUnitTest.DirectAtMode.StatoinTcpSimplex"),
  );
  window.Fuse = require("fuse.js");
  window.eval(source);
  picker = new window.KpiTestcasePicker(select);
});

test("matches case-insensitive tokens in any order and ranks direct matches before typos", () => {
  expect(search("TCP STATION")).toEqual(["Station TCP", "Statoin TCP"]);
  expect(search("statoin tcp")[0]).toBe("Statoin TCP");
  expect(search("simpelx station")).toContain("Station TCP");
});

test("joined words and numbers require both fragments without fuzzy numeric matches", () => {
  select.replaceChildren(
    new Option("All test families", ""),
    new Option("Buffered 244", "NUnitTest.BufferedAtMode.Peripheral244bytes"),
    new Option("Buffered 20", "NUnitTest.BufferedAtMode.Peripheral20bytes"),
    new Option("Buffered 144", "NUnitTest.BufferedAtMode.Peripheral144bytes"),
    new Option("Buffered 245", "NUnitTest.BufferedAtMode.Peripheral245bytes"),
    new Option("Direct 244", "NUnitTest.DirectAtMode.Peripheral244bytes"),
  );
  picker.refresh();
  expect(search("buffered244")).toEqual(["Buffered 244"]);
  expect(search("buffered 244")).toEqual(["Buffered 244"]);
  expect(search("244BUFFERED")).toEqual(["Buffered 244"]);
  expect(search("bufered244")).toEqual(["Buffered 244"]);
  expect(search("buffered243")).toEqual([]);
});

test("typing changes suggestions only; keyboard selection changes the underlying value once", () => {
  const changed = jest.fn();
  select.addEventListener("change", changed);
  search("station udp");
  expect(select.value).toBe("");
  expect(changed).not.toHaveBeenCalled();
  key("ArrowDown");
  expect(picker.input.getAttribute("aria-activedescendant")).toBe(picker.list.firstChild.id);
  key("Enter");
  expect(select.value).toBe("NUnitTest.DirectAtMode.StationUdpSimplex");
  expect(changed).toHaveBeenCalledTimes(1);
  expect(picker.input.value).toBe("Station UDP");
  expect(picker.input.getAttribute("aria-expanded")).toBe("false");
});

test("pointer selection and clear reset preserve the existing select change contract", () => {
  search("ap tcp");
  picker.list.firstChild.click();
  expect(select.value).toBe("NUnitTest.DirectAtMode.ApTcpSimplex");
  picker.clear.click();
  expect(select.value).toBe("");
  expect(picker.input.value).toBe("All test families");
  expect(picker.clear.disabled).toBe(true);
});

test("escape and blur discard uncommitted search without changing the selected test", () => {
  picker.choose("NUnitTest.DirectAtMode.ApTcpSimplex");
  search("station");
  key("Escape");
  expect(picker.input.value).toBe("AP TCP");
  search("anything else");
  picker.input.blur();
  expect(picker.input.value).toBe("AP TCP");
  expect(select.value).toBe("NUnitTest.DirectAtMode.ApTcpSimplex");
});

test("unmatched search shows an empty state and Enter does not select anything", () => {
  expect(search("zzzzzzzzzzzz")).toEqual([]);
  expect(picker.empty.hidden).toBe(false);
  key("Enter");
  expect(select.value).toBe("");
  expect(picker.input.hasAttribute("aria-activedescendant")).toBe(false);
});

test("refresh tracks programmatic selections, new options, and disabled state", () => {
  select.value = "NUnitTest.DirectAtMode.ApTcpSimplex";
  picker.refresh();
  expect(picker.input.value).toBe("AP TCP");
  select.replaceChildren(new Option("All test families", ""), new Option("Bluetooth", "NUnitTest.Bluetooth"));
  picker.refresh();
  expect(search("bluetooth")).toEqual(["Bluetooth"]);
  select.disabled = true;
  picker.refresh();
  expect(picker.input.disabled).toBe(true);
  expect(picker.toggle.disabled).toBe(true);
  expect(picker.popup.hidden).toBe(true);
});

test("Home, End, and arrow keys keep a valid active option", () => {
  search("");
  key("ArrowUp");
  expect(picker.active).toBe(4);
  key("Home");
  expect(picker.active).toBe(0);
  key("End");
  expect(picker.active).toBe(4);
  key("Enter");
  expect(picker.input.value).toBe("Statoin TCP");
});