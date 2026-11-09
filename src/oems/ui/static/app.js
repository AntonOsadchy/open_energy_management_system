/* Main controller: palette, drag & drop, settings, project save/load, and run. */
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

const START_HINT = "Drag a Bus onto the canvas to start, or press Tutorial (bottom left) for a guided tour.";

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
    ed.on("nodeSelected", id => { Layout.open("side"); Panel.show(id); });
    ed.on("nodeUnselected", () => Panel.clear());
    ed.on("nodeRemoved", () => { Panel.clear(); this.dirty(); this.renderDatasets(); });
    ed.on("nodeCreated", () => this.renderDatasets());
    ed.on("nodeDataChanged", id => { if (ed.getNodeFromId(id)?.data.kind === "note") this.dirty(); });   // typing in a note
    // pressing in a note's text (typing, selecting, its resize corner) selects the note but does not move it
    document.getElementById("canvas-wrap").addEventListener("mousedown", e => { if (e.target.closest?.(".note-node textarea")) ed.drag = false; });
    document.getElementById("drawflow").addEventListener("mouseup", e => {   // a resized note keeps its size
      const t = e.target.closest?.(".note-node textarea"), id = t?.closest(".drawflow-node").id.slice(5);
      if (!t) return;
      const node = ed.getNodeFromId(id), size = [t.offsetWidth, t.offsetHeight];
      if (String(node.data.size) === String(size)) return;
      ed.updateNodeDataFromId(id, { ...node.data, size });
      this.dirty();
    });
    ed.on("nodeMoved", () => Results.summary && Results.overlay());

    Wiring.init();
    Layout.init();
    this.buildPalette();
    for (const ds of await api("/api/datasets")) this.datasets[ds.id] = await api(`/api/datasets/${ds.id}`);
    this.renderDatasets();
    this.loadDbDefault();
    this.bindTopbar();
    RunSettings.init();
    this.bindPeriod();
    this.bindDrop();
    document.querySelectorAll("#zoom button").forEach(b => b.addEventListener("click", () =>
      ({ in: () => ed.zoom_in(), out: () => ed.zoom_out(), reset: () => ed.zoom_reset() })[b.dataset.z]()));
    document.querySelectorAll("#res-tabs button").forEach(b => b.addEventListener("click", () => Results.showTab(b.dataset.tab)));
    document.getElementById("res-toggle").addEventListener("click", () => {
      const r = document.getElementById("results");
      r.classList.toggle("collapsed");
      document.body.classList.toggle("results-open", !r.classList.contains("collapsed"));
    });
    this.status(START_HINT);   // until a project is opened or the first block is placed
  },

  /* a BESS IQ zone was chosen: its time zone becomes the project's (local time of the model and of naive CSVs) */
  zoneTimezone(tz) {
    const el = document.getElementById("set-tz");
    if (!tz || el.value === tz) return;
    el.value = tz;
    this.dirty();
    toast(`Time zone set to ${tz} (the zone's local time) - change it in ⚙️ Run settings`);
  },

  status(msg) { document.getElementById("status").textContent = msg; },
  edits: 0,             // counts model changes, so a run can tell whether the canvas changed while it solved
  savedEdits: 0,        // edits when the project was last saved or opened (more since: unsaved changes)
  variantOf: null,      // the project the open one is a variant of (see ProjectTree)
  dirty() {
    if (document.getElementById("status").textContent === START_HINT) this.status("");
    this.edits++;
    this.showSaved();
    Blocks.updateInvest();
    if (!Results.summary) return;
    Results.summary = null; Results.clearOverlay(); Results.updateBar();
    this.status("Model changed - run again to update results.");
  },

  /* ok to replace the canvas: no unsaved changes, or the user agrees to drop them */
  discardOk() {
    return this.edits === this.savedEdits ||
      confirm(`${this.settings().name ? `'${this.settings().name}'` : "The canvas"} has unsaved changes. Discard them?`);
  },

  /* the note after the project name when it has unsaved changes or was never saved (and the dot and note on it in the tree) */
  showSaved() {
    const name = this.settings().name, changed = this.edits !== this.savedEdits;
    const never = !!name && !ProjectTree.list.some(m => m.name === name), dot = document.getElementById("save-dot");
    dot.hidden = !changed && !never;
    dot.textContent = never ? "Not saved yet" : "Unsaved changes";
    document.body.classList.toggle("unsaved", changed);
  },

  /* Blocks panel: the bus, every component with its presets in its catalog group, and the run settings, as tiles */
  buildPalette() {
    const groups = { Nodes: [`<div class="pal-item tile bus" draggable="true" data-kind="bus" title="A node with its own energy balance: every block of one carrier connects to it">${tileIcon("bus", "el")}Bus</div>`] };
    for (const c of Blocks.catalog.components) {
      const g = (groups[GROUP_LABELS[c.group] || c.group] ||= []);
      for (const p of Blocks.catalog.presets.filter(p => p.type === c.type))
        g.push(`<div class="pal-item tile" draggable="true" data-kind="preset" data-preset="${p.id}" title="${esc(`${p.label}: ${c.label} with typical values`)}">${tileIcon(p.icon, TILE_KIND[p.id])}${esc(p.label)}</div>`);
      g.push(`<div class="pal-item tile" draggable="true" data-kind="component" data-type="${c.type}" title="${esc(c.doc)}">${tileIcon(c.icon, TILE_KIND[c.type])}${esc(c.label)}</div>`);
    }
    groups.Settings = [`<div class="pal-item tile invest" draggable="true" data-kind="invest"
      title="Drag onto the canvas (not wired, one per project): the CO₂ cap, and which PV and storages a lifetime optimisation sizes, with their CAPEX, OPEX and the discount rate">${tileIcon("gear", "any")}Run settings</div>`];
    groups.Notes = [`<div class="pal-item tile note" draggable="true" data-kind="note">${tileIcon("note", "any")}Note</div>`];
    const rank = g => { const i = GROUP_ORDER.indexOf(g); return i < 0 ? GROUP_ORDER.indexOf("Settings") - 0.5 : i; };
    document.getElementById("palette-blocks").innerHTML = Object.entries(groups).sort(([a], [b]) => rank(a) - rank(b)).map(([g, tiles]) =>
      `<h3>${esc(g)}</h3><div class="tiles">${tiles.join("")}</div>`).join("");
    // every block tile (also the input tiles in the page) explains its block, the same text as on the canvas
    for (const it of document.querySelectorAll("#palette .pal-item.tile[data-kind]")) {
      const d = it.dataset;
      const hint = d.kind === "preset" ? BLOCK_HINTS[`preset:${d.preset}`] : d.kind === "component" ? BLOCK_HINTS[d.type] :
        d.kind === "db" ? BLOCK_HINTS[d.preset ? `db:${d.preset}` : "db"] : BLOCK_HINTS[d.kind];
      if (hint) it.title = `${hint}\n\nDrag onto the canvas.`;
    }
  },

  /* Inputs section: every column of every uploaded dataset; each one becomes its own block on the canvas */
  renderDatasets() {
    const used = new Set();
    if (Blocks.editor) for (const n of allNodes()) if (n.data.kind === "data") for (const o of n.data.outs) used.add(`${n.data.dataset}:${o.name}`);
    // the tutorial's prepared data is only listed while the tutorial is open
    const tutOpen = typeof Tutorial !== "undefined" && Tutorial.isOpen();
    const list = Object.values(this.datasets).filter(ds => ds.name !== "tutorial_data" || tutOpen);
    document.getElementById("dataset-list").innerHTML = list.length ? list.map(ds =>
      `<div class="ds-group"><div class="ds-head" title="${esc(`${ds.rows} rows · ${ds.start} → ${ds.end}`)}">${esc(ds.name)}
         <span class="hint">${esc(ds.resolution || "")}</span></div>` +
      ds.columns.map(c => `<div class="pal-item ds-col${used.has(`${ds.id}:${c}`) ? " used" : ""}" draggable="true"
         data-kind="data" data-ds="${ds.id}" data-col="${esc(c)}" title="Drag onto the canvas (or click) to add '${esc(c)}' as an input block">
         <span class="ds-name">${esc(c)}</span>${sparkline(ds.preview?.[c])}</div>`).join("") + "</div>").join("") :
      `<div class="hint">No inputs yet. Upload a CSV with a time column; each other column becomes an input.</div>`;
  },

  /* BESS IQ database: the connection fields of a DB connection block, and the catalogue (zones, series) per connection */
  dbConn(d) { return { host: d.host, port: d.port, user: d.user, database: d.database, sslmode: d.sslmode }; },
  dbDefault: null,   // login from the project's .env / environment, used when the canvas has no DB connection block
  async loadDbDefault() {
    try {
      const d = await api("/api/db/defaults");
      this.dbDefault = d.host ? d : null;
    } catch (_) { this.dbDefault = null; }
  },
  /* the first modelled PV system of the connection a new PV profile block will use ("" if unknown yet) */
  async firstSolarScenario() {
    const conns = Blocks.dbConnections(), conn = conns.length ? conns[0].data : this.dbDefault;
    try { return conn ? (await this.dbOptions(conn)).solar_scenarios?.[0]?.code || "" : ""; } catch (_) { return ""; }
  },
  /* the first degradation profile of the connection a new Degradation curve block will use */
  async firstDegradationProfile() {
    const conns = Blocks.dbConnections(), conn = conns.length ? conns[0].data : this.dbDefault;
    try { return conn ? (await this.dbOptions(conn)).degradation_profiles?.[0]?.code || "" : ""; } catch (_) { return ""; }
  },
  dbOptionsCache: {},
  async dbOptions(connData) {
    const conn = this.dbConn(connData), key = JSON.stringify(conn);
    if (!this.dbOptionsCache[key]) this.dbOptionsCache[key] = await postJSON("/api/db/options", { conn });
    return this.dbOptionsCache[key];
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
      } else if (d.kind === "profile") {
        id = Blocks.addProfile(x, y);
      } else if (d.kind === "note") {
        id = Blocks.addNote(x, y);
      } else if (d.kind === "invest") {
        const inv = Blocks.investNode();
        if (inv) { toast("A project has one ⚙️ Run settings block - it is selected", true); Panel.show(inv.id); return; }
        id = Blocks.addInvest(x, y);
      } else if (d.kind === "dbconn") {
        const { origin, has_password, ...defaults } = await api("/api/db/defaults");
        id = Blocks.addDbConn(x, y, defaults.host ? defaults : {});
        if (has_password) Panel.dbAutoConnect = id;   // login known from .env: connect right away
      } else if (d.kind === "db" && d.preset === "degradation") {
        id = Blocks.addDb(x, y, { source: "degradation", series: "", profile: await this.firstDegradationProfile() });
      } else if (d.kind === "db" && d.preset === "solar") {
        id = Blocks.addDb(x, y, { source: "solar", series: "", scenario: await this.firstSolarScenario() });
      } else if (d.kind === "db") {
        id = Blocks.addDb(x, y);
      }
      this.dirty();
      if (id) Panel.show(id);
    });
  },

  settings() {
    const v = id => document.getElementById(id).value;
    const mode = RunSettings.mode(), years = Math.max(1, parseInt(v("set-years")) || 1), inv = Blocks.editor && Blocks.investNode()?.data;
    return { name: document.getElementById("set-name").textContent, base_year: v("set-base"), start: v("set-start"), days: parseFloat(v("set-days")) || 1,
             run_mode: mode, compute: RunSettings.compute(), years, sim_years: mode === "single" ? 1 : years,
             discount_rate: (inv ? inv.discount_rate : v("set-discount")) || 0, inflation: (inv ? inv.inflation : v("set-inflation")) || 0,
             lifetime_model: v("set-lifemodel"),
             freq: v("set-freq"), tz: v("set-tz") || null,
             time_limit: parseFloat(v("set-timelimit")) || null, tie_break: 0.01 };
  },

  applySettings(s) {
    const set = (id, val) => { if (val !== undefined && val !== null) document.getElementById(id).value = val; };
    if (s.name) document.getElementById("set-name").textContent = s.name;
    set("set-start", s.start); set("set-days", s.days); set("set-freq", s.freq);
    set("set-tz", s.tz);
    document.getElementById("set-base").value = s.base_year || "";
    document.getElementById("set-years").value = s.years || s.sim_years || 1;
    document.getElementById("set-discount").value = s.discount_rate || 0;
    document.getElementById("set-inflation").value = s.inflation || 0;
    document.getElementById("set-lifemodel").value = s.lifetime_model || "weighted";
    document.getElementById("set-timelimit").value = s.time_limit || "";
    RunSettings.setMode(s.run_mode || ((s.sim_years || 1) > 1 ? "multi_year" : "single"));   // older projects: by years
    RunSettings.setCompute(s.compute || "local");
    RunSettings.picked = !!s.run_mode;
    this.periodChanged();
  },

  /* Base period (a calendar year or custom start / days) and simulation length */
  bindPeriod() {
    const sel = document.getElementById("set-base");
    for (let y = new Date().getFullYear(); y >= 2015; y--) sel.insertAdjacentHTML("beforeend", `<option>${y}</option>`);
    sel.addEventListener("change", () => { this.periodChanged(); this.dirty(); });
    document.getElementById("set-years").addEventListener("change", () => { this.periodChanged(); this.dirty(); });
    this.periodChanged();
  },
  periodChanged() {
    const year = document.getElementById("set-base").value;
    if (year) {   // a calendar year: start / days follow it (shown for reference)
      const y = parseInt(year), leap = (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
      document.getElementById("set-start").value = `${y}-01-01`;
      document.getElementById("set-days").value = leap ? 366 : 365;
    }
    document.querySelectorAll(".custom-period").forEach(l => (l.hidden = !!year));   // a calendar year fixes them
    RunSettings.refresh();
  },

  /* a lifetime optimisation compared with another variant: its saved canvas goes with the run (graph.baseline) */
  async addBaseline(graph) {
    const name = Blocks.investNode()?.data.baseline;
    if (!name || graph.settings.run_mode !== "optimise") return true;
    const meta = ProjectTree.list.find(m => m.name === name);
    if (!meta) { toast(`The baseline '${name}' is not a saved variant (any more) - choose another in ⚙️ Run settings`, true); return false; }
    const p = await api(`/api/projects/${encodeURIComponent(meta.id)}`);
    const exp = migrateDrawflow(p.drawflow);
    graph.baseline = { name, nodes: Object.values(exp.drawflow.Home.data).map(n => ({ id: String(n.id), ...n.data })),
                       edges: edgesOf(exp) };
    return true;
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

  project() {
    const p = { settings: this.settings(), drawflow: Blocks.editor.export() };
    if (this.variantOf && this.variantOf !== p.settings.name) p.variant_of = this.variantOf;
    return p;
  },

  async run() {
    this.showErrors();
    const project = this.project();   // the canvas as run, stored with the results when the simulation is saved
    const edits = this.edits;           // the canvas stays editable while the run solves
    const graph = this.graph();
    if (!await this.addBaseline(graph)) return;
    const r = await postJSON("/api/runs", graph);
    if (!r.run_id) { this.showErrors(r.errors); return toast("The graph has errors - see highlighted blocks", true); }
    const btn = document.getElementById("btn-run"), label = btn.innerHTML;
    btn.disabled = true;
    const box = document.getElementById("run-progress");
    const t0 = Date.now(), clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
    let last = null;
    const show = () => {   // updated every second, between the status polls
      const sec = (Date.now() - t0) / 1000;
      btn.innerHTML = `⏳ ${clock(sec)}`;
      document.getElementById("rp-time").textContent = clock(sec);
      // lifetime optimisation solves all years in one model (then a reference case): no per-year progress to show
      const sizing = !!last?.stages, multi = !sizing && !last?.cloud && last && last.years > 1;
      const where = last?.cloud ? " · ☁ cloud" : "";
      document.getElementById("rp-text").textContent = (!last ? "Starting…" :
        last.cloud && !sizing ? (last.message?.startsWith("starting") ? "Starting a cloud machine…" : "Solving in the cloud…") :
        last.parallel ? `Optimal sizes and reference case in parallel (all ${last.years} years each)` : sizing ?
        `Solve ${last.stage} of ${last.stages}: ` + (last.stage === 1 ? `optimal sizes (all ${last.years} years in one model)` :
          "reference case without the new assets (for NPV, IRR, payback)") : multi ?
        `Year ${last.year} of ${last.years}${last.message?.includes("SoH") ? " · " + last.message.split("(")[1]?.replace(")", "") : ""}` :
        last.status === "building" ? "Building the model…" : "Solving…") + (sizing ? where : "");
      const done = multi ? (last.year - 1) / last.years : null;
      document.getElementById("rp-fill").style.width = done === null ? "" : `${Math.max(3, 100 * done)}%`;
      box.classList.toggle("indeterminate", done === null);
    };
    box.hidden = false;
    this.status("");   // progress is shown in the run box, not repeated in the status bar
    const stop = document.getElementById("rp-stop");
    stop.disabled = false; stop.textContent = "■ Stop";
    stop.onclick = async () => {
      stop.disabled = true; stop.textContent = "Stopping…";
      await fetch(`/api/runs/${r.run_id}/cancel`, { method: "POST" }).catch(() => {});
    };
    show();
    const tick = setInterval(show, 1000);
    try {
      for (;;) {
        await new Promise(res => setTimeout(res, 700));
        const s = await api(`/api/runs/${r.run_id}`);
        last = s;
        show();
        if (s.status === "done") {
          Results.project = project;
          await Results.load(r.run_id);
          await Simulations.keep().catch(err => toast(`The results were not saved: ${err.message}`, true));
          const changed = this.edits !== edits;
          this.status(changed ? "The model changed since you pressed Run - run again to include it" : "");
          toast(changed ? `${s.message}\nThese results are for the model as it was when you pressed Run.` : s.message);
          break;
        }
        if (s.status === "cancelled") { this.status("Run stopped."); toast("Run stopped"); break; }
        if (s.status === "failed") {
          this.showErrors(s.errors);
          this.status(s.message);
          toast(s.message + (s.errors?._global ? "\n" + s.errors._global.join("\n") : ""), true);
          break;
        }
      }
    } finally {
      clearInterval(tick);
      box.hidden = true;
      btn.innerHTML = label;
      btn.disabled = false;
    }
  },

  /* save the canvas and settings, and with them the results on show (the project's results, reopened with it) */
  async save() {
    await Account.ensure();
    let name = this.settings().name;
    if (!name) {   // a new project: name it first
      name = prompt("Name of the new project:")?.trim();
      if (!name) return;
      document.getElementById("set-name").textContent = name;
    }
    const project = this.project();
    let note = "";
    if (Results.summary) {   // results of this canvas (any edit clears them)
      if (Results.project && !Results.project.settings.name) Results.project.settings.name = name;   // run before it had a name
      await Simulations.keep();
      // only its own results: a new variant starts without (its parent's stay with the parent)
      if (Results.saved && (Results.saved.project_name ?? name) === name) { project.results = Results.saved.id; note = " with its results"; }
    }
    await postJSON(`/api/projects/${encodeURIComponent(name)}`, project);
    this.savedEdits = this.edits;
    await ProjectTree.refresh();   // also updates the note after the name
    toast(`Saved project '${name}'${note}`);
  },

  load(p, quiet = false) {
    if (document.getElementById("status").textContent === START_HINT) this.status("");
    Blocks.editor.clear();
    this.variantOf = p.variant_of || null;
    Results.summary = null; Results.updateBar();
    Results.clearOverlay();
    // projects saved before the CO₂ price moved from the top bar onto grid blocks
    const co2 = p.settings?.co2_price;
    if (co2 !== undefined && co2 !== null && co2 !== "")
      for (const n of Object.values(p.drawflow?.drawflow?.Home?.data || {}))
        if (n.data?.type === "GridConnection" && (n.data.params ??= {}).co2_price == null) n.data.params.co2_price = co2;
    Blocks.editor.import(migrateDrawflow(p.drawflow));
    for (const n of allNodes()) Blocks.decorate(n.id);
    // restyle data wires
    for (const e of edgesOf(Blocks.editor.export())) {
      const src = Blocks.editor.getNodeFromId(e.from[0]);
      if (!["data", "db", "profile"].includes(src.data.kind)) continue;
      document.querySelectorAll(`.connection.node_out_node-${src.id}`).forEach(c => c.classList.add("c-data"));
    }
    this.applySettings(p.settings || {});
    // projects saved before the CO₂ cap moved from the top bar into the Run settings block
    const cap = p.settings?.co2_cap;
    if (cap !== undefined && cap !== null && cap !== "") {
      let inv = Blocks.investNode();
      if (!inv) inv = Blocks.editor.getNodeFromId(Blocks.addInvest(40, 40));
      if (inv.data.co2_cap == null) { inv.data.co2_cap = parseFloat(cap); Blocks.editor.updateNodeDataFromId(inv.id, inv.data); }
    }
    Blocks.migrateSizing();
    Blocks.updateInvest();
    this.renderDatasets();
    Panel.clear();
    this.savedEdits = this.edits;
    this.showSaved();
    ProjectTree.render();
    if (p.results) this.loadResults(p.results, quiet);
    else if (!quiet) toast(`Loaded '${p.settings?.name || "project"}'`);
  },

  /* reopen the results saved with a project (a saved simulation) */
  async loadResults(simId, quiet) {
    try {
      const sim = await api(`/api/simulations/${simId}`);
      await Results.load(null, sim.meta);
      if (!quiet) toast(`Loaded '${this.settings().name || "project"}' with its results`);
    } catch (_) {
      if (!quiet) toast(`Loaded '${this.settings().name || "project"}' - its saved results are no longer available`);
    }
  },

  async upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("tz", this.settings().tz || "");
    try {
      const ds = await api("/api/datasets", { method: "POST", body: fd });
      this.datasets[ds.id] = ds;
      this.renderDatasets();
      Palette.show("inputs");
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
    document.getElementById("res-year").addEventListener("change", e => Results.setYear(e.target.value));
    document.getElementById("res-notes-text").addEventListener("input", e => Results.editNotes(e.target.value));
    const on = (id, fn) => document.getElementById(id).addEventListener("click", () => fn().catch?.(e => toast(e.message, true)));
    on("btn-run", () => RunSettings.start());
    on("btn-save", () => this.save());
    on("save-dot", () => this.save());
    document.addEventListener("keydown", e => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); this.save().catch(err => toast(err.message, true)); }
    });
    on("btn-config", () => this.exportConfig());
    on("res-dl-csv", () => Results.download("csv"));
    on("res-dl-xlsx", () => Results.download("xlsx"));
    on("btn-tutorial", async () => Tutorial.open());
    on("res-inspect", async () => Inspect.open());
    document.getElementById("file-upload").addEventListener("change", e => { if (e.target.files[0]) this.upload(e.target.files[0]); e.target.value = ""; });
  },
};

window.addEventListener("DOMContentLoaded", () => App.init().catch(e => toast("Failed to start: " + e.message, true)));
