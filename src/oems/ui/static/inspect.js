/* Results inspector: a pop-up window with every per-time-step series of the shown run (flows, states, marginal
 * prices, inputs). Pick series on the left; they are plotted (zoom / range slider) and listed in a table below.
 * Clicking the chart jumps the table to that time step. */
const isSoc = c => c.startsWith("state ") && /^soc( |$)/.test(c.split(".").pop());

const Inspect = {
  columns: [],
  selected: [],
  rows: 0,
  offset: 0,
  page: 200,
  plotted: null,     // {points, rows} of the last chart, to map a clicked point to a table row
  seq: { plot: 0, table: 0 },   // only the latest request draws (ticks can arrive faster than answers)

  async open() {
    if (!Results.summary) return toast("Run the model first - then inspect its results", true);
    let t;
    try { t = await api(`${Results.base}/timeseries?limit=1${Results.yq}`); } catch (e) { return toast(e.message, true); }
    this.columns = t.columns.filter(c => c !== "step_h");
    this.rows = t.rows;
    this.selected = this.defaultSelection();
    this.offset = 0;
    this.build();
    this.renderList();
    await this.refresh();
  },

  close() {
    document.getElementById("inspect")?.remove();
    document.removeEventListener("keydown", this._esc);
  },

  /* the selected block's series, else the flows of the first bus plus its price; storage SOC always */
  defaultSelection() {
    const node = Panel.nodeId && Blocks.editor.getNodeFromId(Panel.nodeId);
    const name = node?.data?.name;
    if (name) {
      const mine = this.columns.filter(c => c.includes(` ${name}.`) || c.includes(`→ ${name}`) ||
        c.startsWith(`price ${name} `) || c.includes(`.${name} `) || c.includes(`→ ${name} `) ||
        c.startsWith(`check ${name} `));
      if (mine.length) return mine.slice(0, 12);
    }
    const bus = Results.summary.buses[0];
    const soc = this.columns.filter(isSoc);
    return [...this.columns.filter(c => tsGroup(c) === "flow" && c.includes(` ${bus} `) || c === `price ${bus} [EUR/MWh]` ||
      c.startsWith(`flow ${bus} →`) || c.endsWith(`→ ${bus} [MW]`)).slice(0, 12), ...soc,
      ...this.columns.filter(c => c === "econ profit [EUR]" || tsGroup(c) === "check")];
  },

  build() {
    this.close();
    const s = Results.summary, sim = Results.saved;
    const div = document.createElement("div");
    div.id = "inspect";
    div.innerHTML = `<div class="ins-win" role="dialog" aria-label="Inspect results">
      <header class="ins-head"><strong>🔍 Inspect results</strong>
        ${Results.byYear ? `<label class="ins-year">Year <select id="ins-year">${Array.from({ length: s.lifetime.years }, (_, i) =>
          `<option ${i + 1 === Results.year ? "selected" : ""}>${i + 1}</option>`).join("")}</select> of ${s.lifetime.years}</label>` : ""}
        <span class="hint">${sim ? `💾 ${esc(sim.name)} · ` : ""}${s.steps} steps (${esc(s.resolution)}) · total cost ${fmt(s.total_cost)} €</span>
        <span class="grow"></span>
        <span class="ins-dl">⤓ Selected: <a id="ins-csv-sel" href="#" download>CSV</a> · <a id="ins-xlsx-sel" href="#" download>Excel</a></span>
        <span class="ins-dl">⤓ All: <a href="${Results.base}/timeseries?format=csv${Results.yq}" download>CSV</a> ·
          <a href="${Results.base}/timeseries?format=xlsx${Results.yq}" download>Excel</a></span>
        ${Results.byYear ? `<span class="ins-dl" title="Every year of the run, one block per year with a 'year' column">⤓ All years:
          <a id="ins-all-years" href="${Results.base}/timeseries?format=csv&year=all" download>CSV</a></span>` : ""}
        <span class="ins-dl"><a id="ins-zip" href="${Results.base}/download" download title="All result tables (flows, states, costs, KPIs …) as CSV files in a zip">⤓ All tables (zip)</a></span>
        <button id="ins-report" class="small" title="Download a report: system picture, key inputs, key outputs and charts">📄 Report</button>
        <button id="ins-close" title="Close (Esc)">✕</button></header>
      <div class="ins-body">
        <aside class="ins-cols">
          <input id="ins-filter" type="search" placeholder="Filter series… (e.g. battery, price, el →)">
          <div class="ins-quick"><button data-q="flow">flows</button><button data-q="state">states</button>
            <button data-q="price">prices</button><button data-q="econ">profit</button><button data-q="input">inputs</button>
            <button data-q="none">clear</button></div>
          <div id="ins-list"></div>
        </aside>
        <section class="ins-main">
          <div class="ins-plotbox"><div id="ins-plot"></div><div id="ins-empty" class="empty">Tick series on the left to plot them.</div></div>
          <div class="ts-bar"><button data-p="first">⏮</button><button data-p="prev">◀</button><span id="ins-range"></span>
            <button data-p="next">▶</button><button data-p="last">⏭</button>
            <span class="hint">click the chart to jump to a time step</span></div>
          <div id="ins-table"></div>
        </section>
      </div></div>`;
    document.body.appendChild(div);
    div.addEventListener("mousedown", e => { if (e.target === div) this.close(); });
    document.getElementById("ins-close").onclick = () => this.close();
    document.getElementById("ins-year")?.addEventListener("change", e => Results.setYear(e.target.value));
    document.getElementById("ins-report").onclick = () => Results.downloadReport();
    this._esc = e => { if (e.key === "Escape") this.close(); };
    document.addEventListener("keydown", this._esc);
    document.getElementById("ins-filter").addEventListener("input", () => this.renderList());
    div.querySelectorAll("[data-q]").forEach(b => b.onclick = () => {
      const q = b.dataset.q, shown = this.visible();
      this.selected = q === "none" ? [] : [...new Set([...this.selected, ...shown.filter(c => tsGroup(c) === q)])].slice(0, 40);
      this.renderList();
      this.refresh();
    });
    div.querySelectorAll("[data-p]").forEach(b => b.onclick = () => {
      const last = Math.max(0, Math.floor((this.rows - 1) / this.page) * this.page);
      const to = { first: 0, prev: this.offset - this.page, next: this.offset + this.page, last }[b.dataset.p];
      this.loadTable(Math.min(Math.max(0, to), last));
    });
  },

  visible() {
    const f = document.getElementById("ins-filter").value.trim().toLowerCase();
    return this.columns.filter(c => !f || c.toLowerCase().includes(f));
  },

  renderList() {
    const groups = { check: "⚠ Checks: both ways at once (MW)", econ: "Profit & costs (€ per step)", flow: "Flows (MW)", state: "States", price: "Marginal prices",
      input: "Inputs used" };
    const shown = this.visible();
    document.getElementById("ins-list").innerHTML = Object.entries(groups).map(([g, title]) => {
      const cols = shown.filter(c => tsGroup(c) === g);
      return cols.length ? `<div class="ins-group g-${g}"><div class="ins-gtitle">${title}</div>${cols.map(c =>
        `<label title="${esc(c)}"><input type="checkbox" value="${esc(c)}" ${this.selected.includes(c) ? "checked" : ""}>
         <span>${esc(tsLabel(c))}</span></label>`).join("")}</div>` : "";
    }).join("") || `<div class="hint">No series match.</div>`;
    document.querySelectorAll("#ins-list input").forEach(cb => cb.onchange = () => {
      this.selected = cb.checked ? [...this.selected, cb.value] : this.selected.filter(c => c !== cb.value);
      this.refresh();
    });
  },

  async refresh() {
    const sel = encodeURIComponent(JSON.stringify(this.selected));
    document.getElementById("ins-csv-sel").href = `${Results.base}/timeseries?format=csv&cols=${sel}${Results.yq}`;
    document.getElementById("ins-xlsx-sel").href = `${Results.base}/timeseries?format=xlsx&cols=${sel}${Results.yq}`;
    await Promise.all([this.plot(), this.loadTable(this.offset)]);
  },

  async plot() {
    const my = ++this.seq.plot;
    const el = document.getElementById("ins-plot");
    document.getElementById("ins-empty").hidden = this.selected.length > 0;
    if (!this.selected.length) return Plotly.purge(el);
    const d = await api(`${Results.base}/timeseries?format=series&cols=${encodeURIComponent(JSON.stringify(this.selected))}${Results.yq}`);
    if (my !== this.seq.plot || !document.getElementById("inspect")) return;   // a newer selection is on its way
    this.plotted = { points: d.points, rows: d.rows };
    const money = c => /\[EUR/.test(c);              // prices and €-inputs on the right axis
    const traces = this.selected.map(c => isSoc(c) ? {   // state of charge in % on its own 0-100 axis
      x: d.time, y: d.series[c].map(v => (v === null ? null : v * 100)), name: tsLabel(c).replace("[0-1]", "[%]"),
      type: "scatter", mode: "lines", yaxis: "y3", fill: "tozeroy", fillcolor: "rgba(124,58,237,.07)",
      line: { shape: d.points === d.rows ? "hv" : "linear", width: 2, dash: "dot" },
      hovertemplate: "%{y:.1f} %<extra>" + esc(tsLabel(c)) + "</extra>" } : {
      x: d.time, y: d.series[c], name: tsLabel(c), type: "scatter", mode: "lines",
      line: { shape: d.points === d.rows ? "hv" : "linear", width: 1.5 }, yaxis: money(c) ? "y2" : "y",
      hovertemplate: "%{y:.4g}<extra>" + esc(tsLabel(c)) + "</extra>" });
    const hasSoc = this.selected.some(isSoc);
    Plotly.react(el, traces, {
      margin: { l: 55, r: 60, t: 30, b: 10 }, hovermode: "x unified", showlegend: true,
      legend: { orientation: "h", x: 0, y: 1.02, yanchor: "bottom", font: { size: 11 } },
      xaxis: { rangeslider: { thickness: 0.08 }, type: "date", domain: [0, hasSoc ? 0.94 : 1] },
      yaxis: { title: { text: "MW / other", font: { size: 11 } }, zeroline: true, automargin: true },
      yaxis2: { title: { text: this.selected.some(c => tsGroup(c) === "econ") ? "€ · €/MWh" : "€/MWh", font: { size: 11 } },
        overlaying: "y", side: "right", showgrid: false,
        visible: this.selected.some(money) },
      yaxis3: { title: { text: "SOC %", font: { size: 11 } }, overlaying: "y", side: "right", anchor: "free",
        position: 1, range: [0, 100], showgrid: false, visible: hasSoc },
    }, { responsive: true, displaylogo: false });
    el.removeAllListeners?.("plotly_click");
    el.on("plotly_click", ev => {
      const p = ev.points?.[0];
      if (!p) return;
      const row = Math.min(this.rows - 1, Math.round(p.pointIndex * this.plotted.rows / this.plotted.points));
      this.loadTable(Math.floor(row / this.page) * this.page, row % this.page);
    });
  },

  async loadTable(offset, mark = -1) {
    const my = ++this.seq.table;
    const cols = this.selected.length ? `&cols=${encodeURIComponent(JSON.stringify(this.selected))}` : "";
    const t = await api(`${Results.base}/timeseries?offset=${offset}&limit=${this.page}${cols}${Results.yq}`);
    if (my !== this.seq.table || !document.getElementById("inspect")) return;
    this.offset = t.offset;
    document.getElementById("ins-range").textContent = `steps ${t.offset + 1}–${t.offset + t.time.length} of ${t.rows}`;
    const box = document.getElementById("ins-table");
    box.innerHTML = tsTableHtml(t, mark);
    box.querySelector("tr.mark")?.scrollIntoView({ block: "center" });
  },
};
