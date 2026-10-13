"use strict";

const fs = require("fs");
const path = require("path");

const sourceRoot = path.join(__dirname, "..", "..", "src", "testrift_server");
const script = fs.readFileSync(path.join(sourceRoot, "static", "collection_admin.js"), "utf8");
const template = fs.readFileSync(path.join(sourceRoot, "templates", "collection_summary.html"), "utf8");
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

describe("Collection editor", () => {
  let collection;

  beforeEach(async () => {
    const markup = new DOMParser().parseFromString(template, "text/html");
    document.body.replaceChildren(
      document.importNode(markup.getElementById("open-edit"), true),
      document.importNode(markup.getElementById("edit-collection-modal"), true)
    );
    collection = {
      key: "history",
      display_name: "History",
      description: "Completed runs",
      targets: [{ id: 1, key: "device", display_name: "Device" }],
      profiles: [],
    };
    window.__COLLECTION_KEY__ = collection.key;
    window.__editCollectionModal = { show: jest.fn(), hide: jest.fn() };
    window.__onCollectionUpdated = jest.fn();
    window.fetch = jest.fn(async (url, options) => {
      let data;
      if (url.endsWith("/profile-filter-options")) {
        data = { purposes: [{ value: "historical-import", run_count: 1 }], source_pairs: [] };
      } else if (url === "/api/targets") {
        data = collection.targets;
      } else if (!options) {
        data = collection;
      }
      return { ok: true, json: async () => ({ success: true, data }) };
    });
    window.eval(script);
    await settle();
  });

  test("opens the actual template editor and populates its readonly URL key", () => {
    expect(document.getElementById("collection-admin-status").textContent).toBe("");
    document.getElementById("open-edit").click();

    expect(window.__editCollectionModal.show).toHaveBeenCalledTimes(1);
    expect(document.getElementById("collection-url-key").value).toBe("history");
    expect(document.getElementById("collection-url-key").readOnly).toBe(true);
    expect(document.getElementById("collection-name").value).toBe("History");
    expect(document.querySelectorAll("#collection-members input:checked")).toHaveLength(1);
  });

  test("saves membership and refreshes without a missing label error", async () => {
    document.getElementById("collection-editor").dispatchEvent(new Event("submit", { cancelable: true }));
    await settle();

    expect(window.fetch).toHaveBeenCalledWith("/api/collections/history/members", expect.objectContaining({
      method: "PUT",
      body: JSON.stringify({ target_ids: [1] }),
    }));
    expect(document.getElementById("collection-admin-status").textContent).toBe("Collection saved.");
    expect(window.__editCollectionModal.hide).toHaveBeenCalledTimes(1);
  });
});