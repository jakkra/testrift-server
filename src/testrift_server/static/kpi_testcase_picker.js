(function () {
  "use strict";

  const normalize = value => value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

  class KpiTestcasePicker {
    constructor(select) {
      this.select = select;
      this.entries = [];
      this.results = [];
      this.active = -1;
      this.root = document.createElement("div");
      this.root.className = "kpi-testcase-picker";
      this.input = document.createElement("input");
      this.input.id = `${select.id}-search`;
      this.input.type = "text";
      this.input.autocomplete = "off";
      this.input.spellcheck = false;
      this.input.setAttribute("role", "combobox");
      this.input.setAttribute("aria-label", "Test case");
      this.input.setAttribute("aria-autocomplete", "list");
      this.input.setAttribute("aria-expanded", "false");
      this.input.setAttribute("aria-controls", `${select.id}-options`);
      this.clear = this.iconButton("Clear test case", "times");
      this.toggle = this.iconButton("Show test cases", "chevron-down");
      this.popup = document.createElement("div");
      this.popup.className = "kpi-testcase-popup";
      this.popup.hidden = true;
      this.list = document.createElement("div");
      this.list.id = `${select.id}-options`;
      this.list.setAttribute("role", "listbox");
      this.list.setAttribute("aria-label", "Test cases");
      this.empty = document.createElement("div");
      this.empty.className = "kpi-testcase-empty";
      this.empty.setAttribute("role", "status");
      this.empty.textContent = "No matching test cases";
      this.empty.hidden = true;
      this.popup.append(this.list, this.empty);
      this.root.append(this.input, this.clear, this.toggle, this.popup);
      select.after(this.root);
      select.hidden = true;
      select.tabIndex = -1;
      const label = document.querySelector(`label[for="${select.id}"]`);
      if (label) label.htmlFor = this.input.id;

      this.input.addEventListener("focus", () => { this.open(""); this.input.select(); });
      this.input.addEventListener("click", () => { if (this.popup.hidden) this.open(""); });
      this.input.addEventListener("input", () => this.open(this.input.value));
      this.input.addEventListener("keydown", event => this.keydown(event));
      this.input.addEventListener("blur", () => this.close());
      this.clear.addEventListener("click", () => { this.choose(""); this.input.focus(); });
      this.toggle.addEventListener("mousedown", event => event.preventDefault());
      this.toggle.addEventListener("click", () => {
        if (!this.popup.hidden) this.close();
        else { this.input.focus(); this.open(""); }
      });
      select.addEventListener("change", () => this.refresh());
      this.refresh();
    }

    iconButton(label, icon) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "kpi-testcase-icon";
      button.title = label;
      button.setAttribute("aria-label", label);
      const symbol = document.createElement("i");
      symbol.className = `fas fa-${icon}`;
      symbol.setAttribute("aria-hidden", "true");
      button.append(symbol);
      return button;
    }

    refresh() {
      this.entries = [...this.select.options].map(option => ({
        value: option.value, label: option.textContent, title: option.title || option.textContent,
        search: normalize(`${option.textContent} ${option.value}`),
      }));
      this.fuse = new window.Fuse(this.entries.filter(entry => entry.value), {
        keys: ["search"], threshold: 0.32, ignoreLocation: true, ignoreFieldNorm: true,
        includeScore: true, minMatchCharLength: 2,
      });
      this.input.disabled = this.select.disabled;
      this.toggle.disabled = this.select.disabled;
      this.close();
    }

    matches(query) {
      const text = normalize(query);
      if (!text) return this.entries;
      const tokens = text.match(/[a-z]+|[0-9]+/g);
      const tokenMatches = tokens.map(token => new Map(
        token.length < 3 || !/^[a-z]+$/.test(token)
          ? [] : this.fuse.search(token).map(result => [result.item.value, result.score])
      ));
      return this.entries.map(entry => {
        let score;
        if (normalize(entry.label) === text || normalize(entry.value) === text) score = 0;
        else if (entry.search.startsWith(text)) score = 0.05;
        else if (entry.search.includes(text)) score = 0.1;
        else if (tokens.every(token => entry.search.includes(token))) score = 0.2;
        else if (entry.value && tokens.every((token, index) =>
          entry.search.includes(token) || tokenMatches[index].has(entry.value))) {
          score = 1 + tokens.reduce((total, token, index) =>
            total + (entry.search.includes(token) ? 0 : tokenMatches[index].get(entry.value)), 0);
        }
        return { entry, score };
      }).filter(result => result.score !== undefined)
        .sort((left, right) => left.score - right.score || left.entry.label.localeCompare(right.entry.label))
        .map(result => result.entry);
    }

    open(query) {
      if (this.input.disabled) return;
      this.results = this.matches(query);
      this.list.replaceChildren();
      this.results.forEach((entry, index) => {
        const option = document.createElement("div");
        option.id = `${this.list.id}-${index}`;
        option.className = "kpi-testcase-option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(entry.value === this.select.value));
        option.textContent = entry.label;
        option.title = entry.title;
        option.addEventListener("mousedown", event => event.preventDefault());
        option.addEventListener("click", () => this.choose(entry.value));
        option.addEventListener("mousemove", () => this.activate(index));
        this.list.append(option);
      });
      this.empty.hidden = Boolean(this.results.length);
      this.popup.hidden = false;
      this.input.setAttribute("aria-expanded", "true");
      this.active = -1;
      this.input.removeAttribute("aria-activedescendant");
    }

    activate(index) {
      this.active = index;
      [...this.list.children].forEach((option, optionIndex) => {
        option.classList.toggle("active", index === optionIndex);
      });
      const option = this.list.children[index];
      if (option) {
        this.input.setAttribute("aria-activedescendant", option.id);
        option.scrollIntoView?.({ block: "nearest" });
      }
    }

    keydown(event) {
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        if ((event.key === "Home" || event.key === "End") && this.popup.hidden) return;
        event.preventDefault();
        if (this.popup.hidden) this.open("");
        if (!this.results.length) return;
        let next = this.active + (event.key === "ArrowUp" ? -1 : 1);
        if (event.key === "Home") next = 0;
        if (event.key === "End" || (event.key === "ArrowUp" && this.active < 0)) next = this.results.length - 1;
        this.activate(Math.max(0, Math.min(this.results.length - 1, next)));
      } else if (event.key === "Enter" && !this.popup.hidden) {
        event.preventDefault();
        const entry = this.results[this.active < 0 ? 0 : this.active];
        if (entry) this.choose(entry.value);
      } else if (event.key === "Escape" && !this.popup.hidden) {
        event.preventDefault();
        this.close();
      }
    }

    choose(value) {
      const changed = this.select.value !== value;
      this.select.value = value;
      this.close();
      if (changed) this.select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    close() {
      this.popup.hidden = true;
      this.input.setAttribute("aria-expanded", "false");
      this.input.removeAttribute("aria-activedescendant");
      const selected = this.select.selectedOptions[0];
      this.input.value = selected?.textContent || "All test families";
      this.input.title = selected?.title || this.input.value;
      this.clear.disabled = this.select.disabled || !this.select.value;
      this.active = -1;
    }
  }

  window.KpiTestcasePicker = KpiTestcasePicker;
}());