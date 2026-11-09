/* Results drawer and the result overlay on the canvas (wire labels, block badges). */
const Results = {
  runId: null,
  base: null,       // API prefix of the shown results: /api/runs/<id> (live run) or /api/simulations/<id> (saved)
  summary: null,
  project: null,    // canvas + settings the live run was started from (stored with it on save)
  saved: null,      // meta of the saved simulation that is shown, or that the live run was saved as
  notes: "",        // comments on the analysis (stored with the saved simulation, shown in the report)
  year: 1,          // year of a multi-year run shown in the Summary, on the canvas and in Inspect

  /* show a live run (runId) or a saved simulation (sim = its meta) */
  async load(runId, sim = null) {
    this.runId = runId;
    this.saved = sim;
    this.notes = sim?.notes || "";
    document.getElementById("res-notes-text").value = this.notes;
    document.getElementById("res-notes-state").textContent = "";
    this.base = sim ? `/api/simulations/${sim.id}` : `/api/runs/${runId}`;
    this.year = 1;
    this.summary = await api(`${this.base}/summary`);
    const s = this.summary;
    const life = s.lifetime;
    document.getElementById("res-year-box").hidden = !this.byYear;
    if (life) document.getElementById("res-year").innerHTML =
      Array.from({ length: life.years }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join("");
    const sz = s.sizing;
    document.querySelector('#res-tabs [data-tab="sizing"]').hidden = !sz;
    if (!sz && document.querySelector("#res-tabs .active")?.dataset.tab === "sizing") this.showTab("summary");
    this.updateBar();
    Layout.open("results");
    document.getElementById("results").classList.remove("collapsed");
    document.body.classList.add("results-open");
    this.renderSummary();
    this.overlay();
    this.showTab(sz && !sim ? "sizing" : document.querySelector("#res-tabs .active").dataset.tab);
  },

  /* query parameter of the shown year ("" for an ordinary run) */
  get yq() { return this.byYear ? `&year=${this.year}` : ""; },
  /* every year can be shown: a live multi-year run, or a saved one that kept every year */
  get byYear() { const l = this.summary?.lifetime; return !!l && (!this.saved || !!l.saved_years); },

  /* show another year of a multi-year run: Summary, canvas badges, plots and Inspect follow */
  async setYear(year) {
    if (!this.byYear) return toast("This saved simulation keeps year 1 in detail", true);
    this.year = parseInt(year) || 1;
    document.getElementById("res-year").value = String(this.year);
    this.summary = await api(`${this.base}/summary?year=${this.year}`);
    this.renderSummary();
    this.overlay();
    this.showTab(document.querySelector("#res-tabs .active").dataset.tab);
    if (document.getElementById("inspect")) Inspect.open();
  },

  /* investment of a lifetime optimisation: headline KPIs, optimal sizes, yearly cash flows (chart + table) */
  renderSizing() {
    const sz = this.summary.sizing;
    if (!sz) return;
    const k = sz.kpis;
    const yrs = v => (v === null || v === undefined ? "not within lifetime" : `${v.toFixed(1)} years`);
    const tile = (label, value, hint = "", extra = "") =>
      `<div class="sz-tile" title="${esc(hint)}"><span>${esc(label)}</span><b>${value}</b>${extra}</div>`;
    const hurdle = kind => (k.hurdles || []).find(h => h.kind === kind);
    const goal = (h, text) => !h ? "" : `<small class="sz-goal ${h.binding ? "binding" : h.met ? "met" : "unmet"}">target ${text}${
      h.binding ? " · binding" : h.met ? " ✓" : " ✗"}</small>`;
    const irrGoal = goal(hurdle("irr"), `≥ ${(100 * (hurdle("irr")?.target || 0)).toFixed(1)} %`);
    const pbGoal = kind => goal(hurdle(kind), `≤ ${(hurdle(kind)?.target || 0).toFixed(1)} years`);
    const sizes = sz.sizes.map(s => `<tr><td>${esc(s.component)}</td><td>${esc(s.quantity)}</td><td class="num"><b>${fmt(s.value)}</b> ${esc(s.unit)}</td>
      <td class="num">${fmt(s.min)}–${s.max === null || s.max === undefined ? "∞" : fmt(s.max)}</td></tr>`).join("");
    document.getElementById("sz-kpis").innerHTML = `<div class="sz-tiles">
        ${tile("NPV of the investment", `${fmt(k.npv)} €`, `present value at ${(100 * k.discount_rate).toFixed(1)} % of savings − OPEX − CAPEX${
          k.inflation ? ` (prices +${(100 * k.inflation).toFixed(1)} %/yr)` : ""}`)}
        ${tile("IRR", k.irr === null || k.irr === undefined ? "n/a" : `${(100 * k.irr).toFixed(1)} %`, "discount rate at which the NPV is 0", irrGoal)}
        ${tile("Payback", yrs(k.payback), "years until the cumulative net cash flow turns positive", pbGoal("payback"))}
        ${tile("Discounted payback", yrs(k.payback_discounted), "the same with discounted cash flows", pbGoal("payback_discounted"))}
        ${tile("CAPEX", `${fmt(k.capex)} €`)}
        ${k.self_sufficiency === null || k.self_sufficiency === undefined ? "" : tile("Self-sufficiency",
          `${(100 * k.self_sufficiency).toFixed(1)} %`, "share of the electricity demand not bought from the grid",
          `<small class="sz-goal">without the investment ${(100 * (k.self_sufficiency_reference || 0)).toFixed(1)} %</small>${
            k.self_sufficiency_target === undefined ? "" : `<small class="sz-goal ${k.self_sufficiency_binding ? "binding" : "met"}">target ≥ ${
              (100 * k.self_sufficiency_target).toFixed(0)} %${k.self_sufficiency_binding ? " · binding" : " ✓"}</small>`}`)}
        ${tile("Savings, year 1", `${fmt(k.savings_year1)} €`, k.reference ? `operating cost of '${k.reference}' − with the investment` :
          "operating cost without the investment − with it")}
      </div>${k.reference ? `<div class="hint">Compared with the baseline <b>${esc(k.reference)}</b> (run with the same settings).</div>` : ""}
      <div class="cols"><table class="kpi"><tr><th>Asset</th><th></th><th>Optimal size</th><th>Range</th></tr>${sizes}</table>
      <div>${sz.warnings.map(w => `<div class="msg warn">${esc(w)}</div>`).join("")}</div></div>`;
    const rows = sz.rows.map(r => Object.fromEntries(sz.columns.map((c, i) => [c, r[i]])));
    const years = rows.map(r => r.year);
    Plotly.newPlot("sz-plot", [
      { type: "bar", name: "net cash flow €", x: years, y: rows.map(r => r.net_cash_flow),
        marker: { color: rows.map(r => (r.net_cash_flow < 0 ? "#fca5a5" : "#86efac")) } },
      { name: "cumulative €", x: years, y: rows.map(r => r.net_cash_flow_cumulative), line: { color: "#15803d", dash: "dot" } },
      { name: "cumulative discounted €", x: years, y: rows.map(r => r.discounted_cumulative), line: { color: "#1d4ed8" } },
    ], { margin: { t: 10, r: 20, l: 70, b: 30 }, legend: { orientation: "h", y: -0.2 }, xaxis: { title: "year", dtick: 1 },
      yaxis: { title: "€", zeroline: true } }, { responsive: true, displaylogo: false });
    const label = { price_index: "price index", discount_factor: "discount factor", operating_cost: "operating cost €", operating_cost_reference: k.reference ? `${k.reference} €` : "without investment €",
      savings: "savings €", opex: "OPEX €", capex: "CAPEX €", net_cash_flow: "net cash flow €", net_cash_flow_cumulative: "cumulative €",
      discounted: "discounted €", discounted_cumulative: "cum. discounted €" };
    document.getElementById("sz-table").innerHTML = `<table class="kpi life"><tr>${sz.columns.map(c => `<th>${esc(label[c] || c)}</th>`).join("")}</tr>${
      rows.map(r => `<tr>${sz.columns.map(c => `<td class="num">${c === "year" ? r[c] : c === "discount_factor" || c === "price_index" ? r[c].toFixed(3) : fmt(r[c])}</td>`).join("")}</tr>`).join("")}</table>
      <div class="hint">Year 0 is the investment. Operating cost includes peak and fixed charges, spread evenly over the years${
        k.inflation ? `; all yearly amounts are nominal: the base period's times the price index (inflation ${(100 * k.inflation).toFixed(1)} %/yr)` : ""}.</div>`;
  },

  /* HTML report of the shown results (system picture, key inputs and outputs, charts): a saved simulation is
     rendered from what is stored with it, a live run from the canvas it was started from */
  async downloadReport() {
    if (!this.summary) return toast("Run the model first", true);
    let blob, name;
    try {
      if (this.saved) {
        const r = await fetch(`${this.base}/report`);
        if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
        blob = await r.blob(); name = this.saved.name;
      } else {
        name = this.project?.settings?.name || App.settings().name || "simulation";
        const r = await fetch(`${this.base}/report`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project: this.project, name, year: this.year, notes: this.notes }) });
        if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
        blob = await r.blob();
      }
    } catch (e) { return toast("Report failed: " + e.message, true); }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `oems_report_${name.replace(/[^\w\-]+/g, "_")}.html`;
    a.click();
    toast("Report downloaded - open it in a browser; print to PDF from there");
  },

  /* ⤓ Download the data of the selected tab as CSV or Excel (format = "csv" | "xlsx"): the Summary of the shown
     year, or the Sizing cash flows */
  async download(format) {
    if (!this.summary) return toast("Run the model first", true);
    const tab = document.querySelector("#res-tabs .active").dataset.tab;
    const name = (this.saved?.name || this.project?.settings?.name || App.settings().name || "simulation");
    const url = `${this.base}/export?view=${tab}&format=${format}${tab === "sizing" ? "" : this.yq}`;
    const r = await fetch(url);
    if (!r.ok) return toast("Download failed: " + ((await r.json().catch(() => ({}))).detail || r.statusText), true);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    const year = this.byYear && tab !== "sizing" ? `_year${this.year}` : "";
    a.download = `oems_${tab}_${name}${year}.${format}`.replace(/[^\w\-.]+/g, "_");
    a.click();
  },

  /* comments typed in the results drawer: kept with a saved simulation right away (debounced), with a live run
     once it is saved */
  editNotes(text) {
    this.notes = text;
    const state = document.getElementById("res-notes-state");
    clearTimeout(this.notesTimer);
    if (!this.saved) { state.textContent = this.summary ? "· saved with the run" : ""; return; }
    state.textContent = "· saving…";
    const sim = this.saved;
    this.notesTimer = setTimeout(async () => {
      try {
        Object.assign(sim, await api(`/api/simulations/${sim.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ notes: text }) }));
        if (this.saved === sim) state.textContent = "· saved";
      } catch (e) { state.textContent = ""; toast("Saving the comments failed: " + e.message, true); }
    }, 600);
  },

  /* the download buttons are offered once results are shown */
  updateBar() {
    document.getElementById("res-download").hidden = !this.summary;
    document.getElementById("res-inspect").disabled = !this.summary;
    const name = document.getElementById("res-name");
    name.textContent = name.title = this.summary && this.saved ? this.saved.name : "";
  },

  renderSummary() {
    const s = this.summary;
    const cats = Object.entries(s.by_category).map(([k, v]) => `<tr><td>${k}</td><td class="num">${fmt(v)} €</td></tr>`).join("");
    const kpis = s.kpis.map(k => `<tr><td>${esc(k.kpi)}</td><td>${esc(k.scope)}</td><td class="num">${fmt(k.value)}</td><td>${esc(k.unit)}</td></tr>`).join("");
    const warn = [...(s.notices || []), ...s.warnings].map(w => `<div class="msg warn">${esc(w)}</div>`).join("");
    const p = s.profit;
    const t = x => esc(String(x).slice(0, 16).replace("T", " "));
    const profit = p ? `<table class="kpi"><tr><th>System profit</th><th></th></tr>
      <tr><td><b>Profit</b> (revenue − costs)</td><td class="num"><b>${fmt(p.total)} €</b></td></tr>
      <tr><td>of which per time step</td><td class="num">${fmt(p.steps)} €</td></tr>
      ${p.period_charges.map(c => `<tr><td>− ${esc(c.category.toLowerCase())} charge (${esc(c.component)}, per period)</td>
        <td class="num">${fmt(-c.EUR)} €</td></tr>`).join("")}
      <tr><td>revenue</td><td class="num">${fmt(p.revenue)} €</td></tr>
      <tr><td>best step ${t(p.best_step)}</td><td class="num">${fmt(p.best)} €</td></tr>
      <tr><td>worst step ${t(p.worst_step)}</td><td class="num">${fmt(p.worst)} €</td></tr>
      <tr><td colspan="2" class="hint">Per-step profit: 🔍 Inspect → Profit &amp; costs</td></tr></table>` : "";
    document.getElementById("res-summary").innerHTML =
      `<div class="cols">${profit}<table class="kpi"><tr><th>Category</th><th>Cost</th></tr>${cats}</table>
       <table class="kpi"><tr><th>KPI</th><th>Scope</th><th>Value</th><th></th></tr>${kpis}</table><div>${warn}</div></div>`;
  },

  showTab(tab) {
    document.querySelectorAll("#res-tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    document.querySelectorAll(".res-body .tab").forEach(t => t.classList.toggle("hidden", t.dataset.tab !== tab));
    if (!this.summary) return;
    if (tab === "sizing") this.renderSizing();
  },

  /* ------------------------------------------------------------ canvas overlay */
  overlay() {
    this.clearOverlay();
    if (!this.summary) return;
    const ed = Blocks.editor;
    // group model edges by canvas wire (component port -> bus port): a ⇄ link wire carries two ports (import +
    // export, charge + discharge)
    const wires = new Map();
    for (const e of this.summary.edges) {
      if (!e.node || !e.bus_node) continue;
      const comp = ed.getNodeFromId(e.node), bus = ed.getNodeFromId(e.bus_node);
      if (!comp || !bus) continue;
      const link = comp.data.outs.find(c => c.ports?.includes(e.port));
      const conn = link ? link.name : portToConnector(comp.data, e.port, e.direction);
      const sel = `.connection.node_out_node-${comp.id}.node_in_node-${bus.id}.output_${connectorIndex(comp.data, "out", conn) + 1}.input_1`;
      if (!wires.has(sel)) wires.set(sel, { link: !!link, parts: [] });
      wires.get(sel).parts.push({ port: e.port, MWh: Math.abs(e.MWh || 0) });
    }
    const total = w => w.parts.reduce((a, p) => a + p.MWh, 0);
    const max = Math.max(1e-9, ...[...wires.values()].map(total));
    for (const [sel, w] of wires) {
      const svg = document.querySelector(sel);
      const path = svg?.querySelector(".main-path");
      if (!path) continue;
      path.style.strokeWidth = `${2 + 6 * total(w) / max}px`;
      const len = path.getTotalLength();
      const p = path.getPointAtLength(len * 0.5);
      // HTML label above the blocks (an SVG label would be hidden behind blocks on short wires)
      const t = document.createElement("div");
      t.className = "edge-label";
      t.style.left = `${p.x}px`; t.style.top = `${p.y}px`;
      t.textContent = w.link ? w.parts.map(x => `${x.port} ${fmt(x.MWh)}`).join(" · ") + " MWh" : `${fmt(w.parts[0].MWh)} MWh`;
      ed.precanvas.appendChild(t);
    }
    for (const [nid, b] of Object.entries(this.summary.badges)) {
      const el = document.querySelector(`#node-${nid} .blk-badges`);
      if (!el) continue;
      const parts = [];
      if (b.size) parts.push(`🔍 ${b.size.join(" / ")}`);
      if (b.cost !== undefined) parts.push(`${fmt(b.cost)} €`);
      if (b.full_load_hours !== undefined) parts.push(`${fmt(b.full_load_hours)} FLH`);
      if (b.equivalent_cycles !== undefined) parts.push(`${fmt(b.equivalent_cycles)} cycles`);
      if (b.soc_min !== undefined) parts.push(`SOC ${b.soc_min.toFixed(0)}–${b.soc_max.toFixed(0)} %`);
      if (b.soh !== undefined && b.soh !== null) parts.push(`SoH ${b.soh.toFixed(0)} %`);
      if (b.production !== undefined) parts.push(`${fmt(b.production)} MWh`);
      if (b.max_billed_peak !== undefined) parts.push(`peak ${fmt(b.max_billed_peak)} MW`);
      el.innerHTML = parts.map(x => `<span>${x}</span>`).join("") + (b.simultaneous ? `<span class="warn"
        title="Flows in both directions at once in ${b.simultaneous} steps (see the Summary tab and 🔍 Inspect → Checks)">⚠ ${
        b.simultaneous} steps both ways</span>` : "");
    }
  },

  clearOverlay() {
    document.querySelectorAll(".edge-label").forEach(x => x.remove());
    document.querySelectorAll(".blk-badges").forEach(x => (x.innerHTML = ""));
    document.querySelectorAll(".connection .main-path").forEach(p => (p.style.strokeWidth = ""));
  },
};

/* model port name -> UI connector name (converters use in1/out1 keys, ports are in_<bus>/out_<bus>) */
function portToConnector(data, port, side) {
  if (data.type !== "Converter") return port;
  const busName = port.replace(/^(in|out)_/, "");
  const exp = Blocks.editor.export();
  const nodes = exp.drawflow.Home.data;
  const keys = new Set((data.converter?.[side === "in" ? "inputs" : "outputs"] || []).map(it => it.key));
  const id = String(findNodeIdByName(data.name));
  const e = edgesOf(exp).find(e => e.from[0] === id && keys.has(e.from[1]) && nodes[e.to[0]]?.data.name === busName);
  return e ? e.from[1] : port;
}

function findNodeIdByName(name) {
  const n = allNodes().find(n => n.data.name === name);
  return n ? n.id : null;
}

function fmt(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  return a >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) :
    a >= 10 ? v.toFixed(1) : a >= 0.01 || a === 0 ? v.toFixed(2) : v.toExponential(2);
}

/* ------------------------------------------------------------ per-time-step table helpers (drawer + inspector) */
const tsGroup = c => c.split(" ")[0];                       // flow | state | price | check | econ | input | step_h
const tsLabel = c => (tsGroup(c) === "step_h" ? "step [h]" : c === "year" ? "year" : c.slice(tsGroup(c).length + 1));
const tsNum = v => (v === null ? "" : Math.abs(v) >= 1000 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) :
  v.toPrecision(3).replace(/\.?0+$/, ""));

/* HTML table of a /timeseries page; `mark` = row index (within the page) to highlight */
function tsTableHtml(t, mark = -1) {
  const head = `<tr><th class="ts-time">time</th>${t.columns.map(c =>
    `<th class="g-${tsGroup(c)}" title="${esc(c)}">${esc(tsLabel(c))}</th>`).join("")}</tr>`;
  const body = t.data.map((row, i) => `<tr${i === mark ? ' class="mark"' : ""}><td class="ts-time">${
    esc(t.time[i].slice(0, 16).replace("T", " "))}</td>${
    row.map((v, j) => {
      const g = tsGroup(t.columns[j]);
      return `<td class="num g-${g}${g === "check" && v > 0 ? " flag" : ""}">${tsNum(v)}</td>`;
    }).join("")}</tr>`).join("");
  return `<table class="ts">${head}${body}</table>`;
}
