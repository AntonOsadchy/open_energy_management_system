/* Results drawer and the result overlay on the canvas (wire labels, block badges). */
const Results = {
  runId: null,
  summary: null,

  async load(runId) {
    this.runId = runId;
    this.summary = await api(`/api/runs/${runId}/summary`);
    const s = this.summary;
    document.getElementById("res-meta").textContent =
      `${s.status} · total cost ${fmt(s.total_cost)} € · ${s.steps} steps (${s.resolution}) · build ${s.build_time.toFixed(1)} s, solve ${s.solve_time.toFixed(1)} s`;
    document.getElementById("res-download").href = `/api/runs/${runId}/download`;
    document.getElementById("results").classList.remove("collapsed");
    document.body.classList.add("results-open");
    this.fillSelect("sel-bus", s.buses);
    this.fillSelect("sel-comp", s.components);
    this.renderSummary();
    this.overlay();
    this.showTab(document.querySelector("#res-tabs .active").dataset.tab);
  },

  fillSelect(id, items) {
    document.getElementById(id).innerHTML = items.map(x => `<option>${esc(x)}</option>`).join("");
  },

  renderSummary() {
    const s = this.summary;
    const cats = Object.entries(s.by_category).map(([k, v]) => `<tr><td>${k}</td><td class="num">${fmt(v)} €</td></tr>`).join("");
    const kpis = s.kpis.map(k => `<tr><td>${esc(k.kpi)}</td><td>${esc(k.scope)}</td><td class="num">${fmt(k.value)}</td><td>${esc(k.unit)}</td></tr>`).join("");
    const warn = s.warnings.map(w => `<div class="msg warn">${esc(w)}</div>`).join("");
    document.getElementById("res-summary").innerHTML =
      `<div class="cols"><table class="kpi"><tr><th>Category</th><th>Cost</th></tr>${cats}</table>
       <table class="kpi"><tr><th>KPI</th><th>Scope</th><th>Value</th><th></th></tr>${kpis}</table><div>${warn}</div></div>`;
  },

  showTab(tab) {
    document.querySelectorAll("#res-tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    document.querySelectorAll(".res-body .tab").forEach(t => t.classList.toggle("hidden", t.dataset.tab !== tab));
    if (!this.summary) return;
    if (tab === "costs") this.plotCosts();
    if (tab === "balance") this.plotSeries("sel-bus", "plot-balance", true);
    if (tab === "component") this.plotSeries("sel-comp", "plot-comp", false);
  },

  plotCosts() {
    const rows = this.summary.costs.filter(r => r.category !== "TIEBREAK");
    const cats = [...new Set(rows.map(r => r.category))];
    const comps = [...new Set(rows.map(r => r.component))];
    const traces = cats.map(c => ({ type: "bar", name: c, x: comps,
      y: comps.map(k => rows.filter(r => r.component === k && r.category === c).reduce((a, r) => a + r.EUR, 0)) }));
    Plotly.newPlot("plot-costs", traces, { barmode: "relative", margin: { t: 10, r: 10, l: 60, b: 40 }, yaxis: { title: "€" } },
      { responsive: true, displaylogo: false });
  },

  async plotSeries(selId, plotId, isBus) {
    const name = document.getElementById(selId).value;
    if (!name) return;
    const d = await api(`/api/runs/${this.runId}/series?name=${encodeURIComponent(name)}`);
    const traces = [];
    for (const [k, v] of Object.entries(d.series)) {
      if (isBus && k === "shadow_price") {
        traces.push({ x: d.time, y: v, name: "marginal price €/MWh", yaxis: "y2", line: { color: "#111", width: 1, dash: "dot" } });
      } else if (isBus) {
        traces.push({ x: d.time, y: v, name: k, stackgroup: v.some(x => x < 0) && v.every(x => x <= 1e-9) ? "neg" : "pos", line: { width: 0.5 } });
      } else {
        traces.push({ x: d.time, y: v, name: k, line: { width: 1.2 }, yaxis: k.includes("[MW]") ? "y" : "y2" });
      }
    }
    Plotly.newPlot(plotId, traces, {
      margin: { t: 10, r: 60, l: 50, b: 30 }, legend: { orientation: "h", y: -0.18 },
      yaxis: { title: "MW" }, yaxis2: { overlaying: "y", side: "right", title: isBus ? "€/MWh" : "state" },
    }, { responsive: true, displaylogo: false });
  },

  /* ------------------------------------------------------------ canvas overlay */
  overlay() {
    this.clearOverlay();
    if (!this.summary) return;
    const ed = Blocks.editor;
    // group model edges by canvas wire: a ⇄ link wire carries two ports (import + export, charge + discharge)
    const wires = new Map();
    for (const e of this.summary.edges) {
      if (!e.node || !e.bus_node) continue;
      const comp = ed.getNodeFromId(e.node), bus = ed.getNodeFromId(e.bus_node);
      if (!comp || !bus) continue;
      let side = e.direction, conn = portToConnector(comp.data, e.port, e.direction);
      const link = [...comp.data.ins, ...comp.data.outs].find(c => c.ports?.includes(e.port));
      if (link) { conn = link.name; side = comp.data.ins.includes(link) ? "in" : "out"; }
      const sel = side === "out" ?
        `.connection.node_out_node-${comp.id}.node_in_node-${bus.id}.output_${connectorIndex(comp.data, "out", conn) + 1}.input_1` :
        `.connection.node_out_node-${bus.id}.node_in_node-${comp.id}.output_1.input_${connectorIndex(comp.data, "in", conn) + 1}`;
      if (!wires.has(sel)) wires.set(sel, { side, link: !!link, parts: [] });
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
      const p = path.getPointAtLength(len * (w.link ? 0.5 : w.side === "out" ? 0.62 : 0.38));  // separate paired wires
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
      if (b.cost !== undefined) parts.push(`${fmt(b.cost)} €`);
      if (b.full_load_hours !== undefined) parts.push(`${fmt(b.full_load_hours)} FLH`);
      if (b.equivalent_cycles !== undefined) parts.push(`${fmt(b.equivalent_cycles)} cycles`);
      if (b.production !== undefined) parts.push(`${fmt(b.production)} MWh`);
      if (b.max_billed_peak !== undefined) parts.push(`peak ${fmt(b.max_billed_peak)} MW`);
      el.innerHTML = parts.map(x => `<span>${x}</span>`).join("");
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
  for (const e of edgesOf(exp)) {
    if (side === "out" && e.from[0] === String(findNodeIdByName(data.name)) && nodes[e.to[0]]?.data.name === busName) return e.from[1];
    if (side === "in" && e.to[0] === String(findNodeIdByName(data.name)) && nodes[e.from[0]]?.data.name === busName) return e.to[1];
  }
  return port;
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
