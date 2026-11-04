/* Main controller: palette, drag & drop, settings, project save/load, validate and run. */
async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}
const postJSON = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function toast(msg, err = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = ""), err ? 4500 : 2500);
}

const App = {
  datasets: {},

  async init() {
    Blocks.catalog = await api("/api/catalog");
    const ed = new Drawflow(document.getElementById("drawflow"));
    ed.reroute = false;
    ed.start();
    Blocks.editor = ed;
    Panel.el = document.getElementById("panel");

    ed.on("connectionCreated", info => { Rules.onCreated(info); this.dirty(); });
    ed.on("connectionRemoved", () => this.dirty());
    ed.on("nodeSelected", id => Panel.show(id));
    ed.on("nodeUnselected", () => Panel.clear());
    ed.on("nodeRemoved", () => { Panel.clear(); this.dirty(); this.renderDatasets(); });
    ed.on("nodeCreated", () => this.renderDatasets());
    ed.on("nodeMoved", () => Results.summary && Results.overlay());

    Wiring.init();
    this.buildPalette();
    for (const ds of await api("/api/datasets")) this.datasets[ds.id] = await api(`/api/datasets/${ds.id}`);
    this.renderDatasets();
    this.bindTopbar();
    this.bindDrop();
    document.querySelectorAll("#zoom button").forEach(b => b.addEventListener("click", () =>
      ({ in: () => ed.zoom_in(), out: () => ed.zoom_out(), reset: () => ed.zoom_reset() })[b.dataset.z]()));
    document.querySelectorAll("#res-tabs button").forEach(b => b.addEventListener("click", () => Results.showTab(b.dataset.tab)));
    document.getElementById("sel-bus").addEventListener("change", () => Results.plotSeries("sel-bus", "plot-balance", true));
    document.getElementById("sel-comp").addEventListener("change", () => Results.plotSeries("sel-comp", "plot-comp", false));
    document.getElementById("res-toggle").addEventListener("click", () => {
      const r = document.getElementById("results");
      r.classList.toggle("collapsed");
      document.body.classList.toggle("results-open", !r.classList.contains("collapsed"));
    });
    this.status("Drag a Bus onto the canvas to start, or press 🎓 Tutorial for a guided tour.");
  },

  status(msg) { document.getElementById("status").textContent = msg; },
  dirty() { if (Results.summary) { Results.summary = null; Results.clearOverlay(); this.status("Model changed - run again to update results."); } },

  buildPalette() {
    const groups = {};
    for (const c of Blocks.catalog.components) (groups[c.group] ||= []).push(c);
    document.getElementById("palette-components").innerHTML = Object.entries(groups).map(([g, list]) =>
      `<section><h3>${esc(g)}</h3>${list.map(c =>
        `<div class="pal-item" draggable="true" data-kind="component" data-type="${c.type}" title="${esc(c.doc)}">${ICONS[c.icon] || "▫️"} ${esc(c.label)}</div>`).join("")}</section>`).join("");
    document.getElementById("palette-presets").innerHTML = Blocks.catalog.presets.map(p =>
      `<div class="pal-item" draggable="true" data-kind="preset" data-preset="${p.id}">${ICONS[p.icon] || "▫️"} ${esc(p.label)}</div>`).join("");
  },

  /* Inputs section: every column of every uploaded dataset; each one becomes its own block on the canvas */
  renderDatasets() {
    const used = new Set();
    if (Blocks.editor) for (const n of allNodes()) if (n.data.kind === "data") for (const o of n.data.outs) used.add(`${n.data.dataset}:${o.name}`);
    const list = Object.values(this.datasets);
    document.getElementById("dataset-list").innerHTML = list.length ? list.map(ds =>
      `<div class="ds-group"><div class="ds-head" title="${esc(`${ds.rows} rows · ${ds.start} → ${ds.end}`)}">${esc(ds.name)}
         <span class="hint">${esc(ds.resolution || "")}</span></div>` +
      ds.columns.map(c => `<div class="pal-item ds-col${used.has(`${ds.id}:${c}`) ? " used" : ""}" draggable="true"
         data-kind="data" data-ds="${ds.id}" data-col="${esc(c)}" title="Drag onto the canvas (or click) to add '${esc(c)}' as an input block">
         <span class="ds-name">${esc(c)}</span>${sparkline(ds.preview?.[c])}</div>`).join("") + "</div>").join("") :
      `<div class="hint">No inputs yet. Upload a CSV with a time column; each other column becomes an input.</div>`;
  },

  /* place an input block for dataset column (next to the other inputs when no position is given) */
  addInput(dsId, col, x, y) {
    const ds = this.datasets[dsId];
    if (x === undefined) {
      const n = allNodes().filter(n => n.data.kind === "data").length;
      x = 20; y = 20 + 70 * n;
    }
    const id = Blocks.addData(x, y, ds, col);
    this.renderDatasets();
    return id;
  },

  bindDrop() {
    document.getElementById("dataset-list").addEventListener("click", e => {
      const it = e.target.closest(".ds-col");
      if (it) Panel.show(this.addInput(it.dataset.ds, it.dataset.col));
    });
    document.getElementById("palette").addEventListener("dragstart", e => {
      const it = e.target.closest(".pal-item");
      if (!it) return;
      e.dataTransfer.setData("text/plain", JSON.stringify(it.dataset));
    });
    const canvas = document.getElementById("drawflow");
    canvas.addEventListener("dragover", e => e.preventDefault());
    canvas.addEventListener("drop", async e => {
      e.preventDefault();
      let d;
      try { d = JSON.parse(e.dataTransfer.getData("text/plain")); } catch (_) { return; }
      const ed = Blocks.editor, pc = ed.precanvas;
      const f = pc.clientWidth / (pc.clientWidth * ed.zoom);
      const x = e.clientX * f - pc.getBoundingClientRect().x * f;
      const y = e.clientY * f - pc.getBoundingClientRect().y * f;
      let id;
      if (d.kind === "bus") id = Blocks.addBus(x, y);
      else if (d.kind === "component") id = Blocks.addComponent(x, y, d.type);
      else if (d.kind === "preset") {
        const p = Blocks.catalog.presets.find(p => p.id === d.preset);
        id = Blocks.addComponent(x, y, p.type, p);
      } else if (d.kind === "data") {
        id = this.addInput(d.ds, d.col, x, y);
      }
      this.dirty();
      if (id) Panel.show(id);
    });
  },

  settings() {
    const v = id => document.getElementById(id).value;
    return { name: v("set-name"), start: v("set-start"), days: parseFloat(v("set-days")) || 1, freq: v("set-freq"),
             tz: v("set-tz") || null, co2_price: v("set-co2"), co2_cap: v("set-co2cap"), tie_break: 0.01 };
  },

  applySettings(s) {
    const set = (id, val) => { if (val !== undefined && val !== null) document.getElementById(id).value = val; };
    set("set-name", s.name); set("set-start", s.start); set("set-days", s.days); set("set-freq", s.freq);
    set("set-tz", s.tz); set("set-co2", s.co2_price); set("set-co2cap", s.co2_cap);
  },

  graph() {
    const exp = Blocks.editor.export();
    const nodes = Object.values(exp.drawflow.Home.data).map(n => ({ id: String(n.id), ...n.data }));
    return { settings: this.settings(), nodes, edges: edgesOf(exp) };
  },

  showErrors(errors = {}, warnings = {}) {
    document.querySelectorAll(".drawflow-node").forEach(n => n.classList.remove("has-error", "has-warn"));
    for (const id of Object.keys(errors)) document.getElementById("node-" + id)?.classList.add("has-error");
    for (const id of Object.keys(warnings)) document.getElementById("node-" + id)?.classList.add("has-warn");
    Panel.messages = { errors, warnings };
    if (Panel.nodeId) Panel.show(Panel.nodeId); else Panel.clear();
  },

  async validate() {
    const r = await postJSON("/api/validate", this.graph());
    this.showErrors(r.errors, r.warnings);
    const ne = Object.values(r.errors || {}).flat().length, nw = Object.values(r.warnings || {}).flat().length;
    if (r.ok) toast(`Valid ✓ ${nw ? `(${nw} warnings)` : ""}`);
    else toast(`${ne} error(s) - see highlighted blocks`, true);
    if (r.errors?._global) toast(r.errors._global.join("\n"), true);
    return r.ok;
  },

  async run() {
    this.showErrors();
    const r = await postJSON("/api/runs", this.graph());
    if (!r.run_id) { this.showErrors(r.errors); return toast("The graph has errors - see highlighted blocks", true); }
    const btn = document.getElementById("btn-run");
    btn.disabled = true;
    try {
      for (;;) {
        await new Promise(res => setTimeout(res, 700));
        const s = await api(`/api/runs/${r.run_id}`);
        this.status(`${s.status}… ${s.elapsed.toFixed(1)} s`);
        if (s.status === "done") { await Results.load(r.run_id); this.status(s.message); toast(s.message); break; }
        if (s.status === "failed") {
          this.showErrors(s.errors);
          this.status(s.message);
          toast(s.message + (s.errors?._global ? "\n" + s.errors._global.join("\n") : ""), true);
          break;
        }
      }
    } finally { btn.disabled = false; }
  },

  async save() {
    const name = this.settings().name || "site";
    await postJSON(`/api/projects/${encodeURIComponent(name)}`, { settings: this.settings(), drawflow: Blocks.editor.export() });
    toast(`Saved project '${name}'`);
  },

  async open() {
    const list = await api("/api/projects");
    if (!list.length) return toast("No saved projects yet");
    const name = prompt(`Open project:\n${list.join("\n")}`, list[0]);
    if (!name) return;
    const p = await api(`/api/projects/${encodeURIComponent(name)}`);
    this.load(p);
  },

  load(p) {
    Blocks.editor.clear();
    Results.summary = null;
    Blocks.editor.import(p.drawflow);
    for (const n of allNodes()) Blocks.decorate(n.id);
    // restyle data wires
    for (const e of edgesOf(Blocks.editor.export())) {
      const src = Blocks.editor.getNodeFromId(e.from[0]);
      if (src.data.kind !== "data") continue;
      document.querySelectorAll(`.connection.node_out_node-${src.id}`).forEach(c => c.classList.add("c-data"));
    }
    this.applySettings(p.settings || {});
    this.renderDatasets();
    Panel.clear();
    toast(`Loaded '${p.settings?.name || "project"}'`);
  },

  async upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("tz", this.settings().tz || "");
    try {
      const ds = await api("/api/datasets", { method: "POST", body: fd });
      this.datasets[ds.id] = ds;
      this.renderDatasets();
      toast(`Loaded ${ds.name}: ${ds.columns.length} inputs, ${ds.rows} rows (${ds.resolution || "?"}) - drag them from Inputs onto the canvas`);
    } catch (e) { toast(e.message, true); }
  },

  async exportConfig() {
    try {
      const cfg = await postJSON("/api/config", this.graph());
      const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${this.settings().name || "system"}.json`;
      a.click();
      toast("Config downloaded - run it with: python -m oems run <file>");
    } catch (e) { toast(e.message, true); }
  },

  bindTopbar() {
    const on = (id, fn) => document.getElementById(id).addEventListener("click", () => fn().catch?.(e => toast(e.message, true)));
    on("btn-validate", () => this.validate());
    on("btn-run", () => this.run());
    on("btn-save", () => this.save());
    on("btn-open", () => this.open());
    on("btn-config", () => this.exportConfig());
    on("btn-tutorial", async () => Tutorial.open());
    on("btn-new", async () => { if (confirm("Clear the canvas?")) { Blocks.editor.clear(); Panel.clear(); Results.summary = null; } });
    document.getElementById("file-upload").addEventListener("change", e => { if (e.target.files[0]) this.upload(e.target.files[0]); e.target.value = ""; });
    document.querySelectorAll("#topbar .settings input, #topbar .settings select").forEach(i => i.addEventListener("change", () => this.dirty()));
  },
};

window.addEventListener("DOMContentLoaded", () => App.init().catch(e => toast("Failed to start: " + e.message, true)));
