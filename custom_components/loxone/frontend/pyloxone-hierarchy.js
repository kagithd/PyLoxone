const STATUS_LABELS = {
  active_entity: "Active",
  disabled: "Disabled",
  unavailable: "Unavailable",
  prepared: "Prepared",
  inventory_only: "Inventory only",
  unsupported: "Unsupported",
  protected: "Protected",
};

class PyLoxoneHierarchy extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._entries = null;
    this._selectedEntryId = null;
    this._hierarchy = null;
    this._error = null;
    this._filter = "";
    this._loadingEntries = false;
    this._request = 0;
  }

  set hass(value) {
    const userId = value?.user?.id ?? null;
    const userChanged = this._userId !== undefined && this._userId !== userId;
    this._hass = value;
    this._userId = userId;
    if (userChanged) {
      this._clearCachedSelection();
    }
    if (this.isConnected && (userChanged || this._entries === null)) {
      void this._loadEntries();
    }
  }

  set panel(value) {
    this._panel = value;
  }

  connectedCallback() {
    this._render();
    void this._loadEntries();
  }

  _clearCachedSelection() {
    this._request += 1;
    this._entries = null;
    this._selectedEntryId = null;
    this._hierarchy = null;
    this._error = null;
    this._filter = "";
    this._render();
  }

  async _loadEntries() {
    if (!this._hass || this._loadingEntries) return;
    this._loadingEntries = true;
    const request = ++this._request;
    try {
      const response = await this._hass.callWS({ type: "loxone/engineering_entries" });
      if (request !== this._request) return;
      this._entries = Array.isArray(response.entries) ? response.entries : [];
      this._error = null;
      this._selectedEntryId = this._entries.length === 1 ? this._entries[0].entry_id : null;
      this._hierarchy = null;
      this._render();
      if (this._selectedEntryId) await this._loadHierarchy();
    } catch (_error) {
      if (request === this._request) {
        this._error = "The hierarchy entry list could not be loaded.";
        this._render();
      }
    } finally {
      this._loadingEntries = false;
      if (request !== this._request && this._entries === null && this.isConnected) {
        void this._loadEntries();
      }
    }
  }

  async _loadHierarchy() {
    if (!this._hass || !this._selectedEntryId) return;
    const entryId = this._selectedEntryId;
    const request = ++this._request;
    this._hierarchy = null;
    this._error = null;
    this._render();
    try {
      const hierarchy = await this._hass.callWS({
        type: "loxone/engineering_hierarchy",
        entry_id: entryId,
      });
      if (request !== this._request || entryId !== this._selectedEntryId) return;
      this._hierarchy = hierarchy;
      this._render();
    } catch (_error) {
      if (request === this._request) {
        this._error = "The selected hierarchy is not currently available.";
        this._render();
      }
    }
  }

  _element(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }

  _render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.replaceChildren();
    const style = this._element("style");
    style.textContent = `
      :host { display:block; padding:24px; color:var(--primary-text-color); }
      main { max-width:1100px; margin:0 auto; }
      h1 { font-size:24px; margin:0 0 20px; }
      .toolbar { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:16px; }
      select,input { min-height:40px; padding:0 12px; border:1px solid var(--divider-color); border-radius:8px; background:var(--card-background-color); color:inherit; }
      input { flex:1 1 260px; }
      .badges { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0 18px; }
      .badge { padding:4px 9px; border-radius:999px; background:var(--secondary-background-color); font-size:13px; }
      details { margin:8px 0 8px 16px; border-left:2px solid var(--divider-color); padding-left:12px; }
      summary { cursor:pointer; font-weight:600; }
      .meta,.function { color:var(--secondary-text-color); font-size:14px; }
      .function { display:flex; gap:8px; align-items:baseline; margin:6px 0 6px 20px; }
      a { color:var(--primary-color); }
      .message { padding:16px; border-radius:10px; background:var(--card-background-color); }
    `;
    const main = this._element("main");
    main.append(this._element("h1", "PyLoxone engineering hierarchy"));
    if (this._entries) main.append(this._renderToolbar());
    if (this._error) {
      main.append(this._element("p", this._error, "message"));
    } else if (this._entries === null) {
      main.append(this._element("p", "Loading entries…", "message"));
    } else if (this._entries.length === 0) {
      main.append(this._element("p", "No loaded PyLoxone entries are available.", "message"));
    } else if (!this._selectedEntryId) {
      main.append(this._element("p", "Select a PyLoxone entry to view its hierarchy.", "message"));
    } else if (!this._hierarchy) {
      main.append(this._element("p", "Loading hierarchy…", "message"));
    } else {
      main.append(this._renderSnapshot(), this._renderBadges());
      const tree = this._element("section");
      tree.id = "hierarchy-tree";
      tree.append(this._renderNode(this._hierarchy.root, true));
      main.append(tree);
    }
    this.shadowRoot.append(style, main);
    this._applyFilter();
  }

  _renderToolbar() {
    const toolbar = this._element("div", null, "toolbar");
    const select = this._element("select");
    select.setAttribute("aria-label", "PyLoxone entry");
    const prompt = this._element("option", "Select entry…");
    prompt.value = "";
    prompt.selected = !this._selectedEntryId;
    select.append(prompt);
    for (const entry of this._entries) {
      const option = this._element("option", entry.title);
      option.value = entry.entry_id;
      option.selected = entry.entry_id === this._selectedEntryId;
      select.append(option);
    }
    select.addEventListener("change", () => {
      this._request += 1;
      this._selectedEntryId = select.value || null;
      this._hierarchy = null;
      this._error = null;
      this._filter = "";
      this._render();
      if (this._selectedEntryId) void this._loadHierarchy();
    });
    toolbar.append(select);
    if (this._selectedEntryId) {
      const filter = this._element("input");
      filter.type = "search";
      filter.placeholder = "Filter hierarchy";
      filter.setAttribute("aria-label", "Filter hierarchy");
      filter.value = this._filter;
      filter.addEventListener("input", () => {
        this._filter = filter.value;
        this._applyFilter();
      });
      toolbar.append(filter);
    }
    return toolbar;
  }

  _renderSnapshot() {
    const snapshot = this._hierarchy.snapshot;
    const seconds = Number(snapshot.age_seconds) || 0;
    const capturedDate = new Date(snapshot.captured_at);
    const captured = Number.isNaN(capturedDate.getTime()) ? "an unknown time" : capturedDate.toLocaleString();
    let age = `${seconds} seconds ago`;
    if (seconds >= 86400) age = `${Math.floor(seconds / 86400)} days ago`;
    else if (seconds >= 3600) age = `${Math.floor(seconds / 3600)} hours ago`;
    else if (seconds >= 60) age = `${Math.floor(seconds / 60)} minutes ago`;
    const text = `Snapshot captured ${captured} (${age})`;
    return this._element("p", text, "meta");
  }

  _renderBadges() {
    const badges = this._element("div", null, "badges");
    for (const key of Object.keys(STATUS_LABELS)) {
      const count = this._hierarchy.summary[key] ?? 0;
      badges.append(this._element("span", `${STATUS_LABELS[key]}: ${count}`, "badge"));
    }
    return badges;
  }

  _renderNode(node, root = false) {
    const details = this._element("details");
    details.open = root;
    details.dataset.search = this._searchText(node);
    const summary = this._element("summary");
    const label = node.label || node.technical_type || node.role;
    summary.append(document.createTextNode(label));
    if (node.device_id) {
      const link = this._element("a", "Open device");
      link.href = `/config/devices/device/${encodeURIComponent(node.device_id)}`;
      link.style.marginInlineStart = "10px";
      link.addEventListener("click", (event) => event.stopPropagation());
      summary.append(link);
    }
    details.append(summary);
    if (node.technical_type) details.append(this._element("div", node.technical_type, "meta"));
    const placement = this._placementText(node.placement);
    if (placement) details.append(this._element("div", placement, "meta"));
    for (const item of node.functions || []) details.append(this._renderFunction(item));
    for (const section of node.sections || []) details.append(this._renderNode(section));
    for (const child of node.children || []) details.append(this._renderNode(child));
    return details;
  }

  _placementText(placement) {
    if (!placement || typeof placement !== "object") return "";
    const values = [];
    for (const [key, label] of [["installation", "Installation"], ["switchboard", "Switchboard"]]) {
      const value = placement[key];
      if (typeof value === "string" && value.trim() && [...value.trim()].length <= 80 && !/\p{C}/u.test(value)) {
        values.push(`${label}: ${value.trim()}`);
      }
    }
    for (const [key, label] of [["row", "Row"], ["position", "Position"]]) {
      const value = placement[key];
      if (Number.isInteger(value) && value >= 0 && value <= 999) values.push(`${label}: ${value}`);
    }
    return values.join(" · ");
  }

  _renderFunction(item) {
    const row = this._element("div", null, "function");
    row.append(this._element("span", item.label || item.technical_type || item.key));
    row.append(this._element("span", STATUS_LABELS[item.status] || item.status, "badge"));
    if (item.entity_id) {
      const link = this._element("a", item.entity_id);
      link.href = `/config/entities/entity/${encodeURIComponent(item.entity_id)}`;
      row.append(link);
    }
    return row;
  }

  _searchText(node) {
    const values = [node.label, node.technical_type, node.role];
    for (const item of node.functions || []) {
      values.push(item.label, item.technical_type, item.status);
    }
    for (const child of [...(node.sections || []), ...(node.children || [])]) {
      values.push(this._searchText(child));
    }
    return values.filter(Boolean).join(" ").toLocaleLowerCase();
  }

  _applyFilter() {
    const query = this._filter.trim().toLocaleLowerCase();
    for (const node of this.shadowRoot?.querySelectorAll("details[data-search]") || []) {
      node.hidden = Boolean(query) && !node.dataset.search.includes(query);
    }
  }
}

if (!customElements.get("pyloxone-hierarchy")) {
  customElements.define("pyloxone-hierarchy", PyLoxoneHierarchy);
}
