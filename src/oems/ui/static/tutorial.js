/* Guided tutorial: builds the README tutorial site (grid, PV, battery, heat pump, heat storage) step by step.
 *
 * Every step has a checklist that is re-evaluated against the canvas twice a second, so the user can do the
 * step by hand; "Do it for me" performs it. Blocks are found by their role (bus carrier kind, block type and
 * wiring), not by name, so renaming blocks does not break the checks.
 */
const Tut = {
  /* ---------------------------------------------------------- canvas queries */
  bus: kind => allNodes().find(n => n.data.kind === "bus" && n.data.carrier_kind === kind),
  dataset: () => Object.values(App.datasets).find(d => d.name === "tutorial_data"),
  /* input block of a tutorial column on the canvas */
  input: col => allNodes().find(n => n.data.kind === "data" && n.data.dataset === Tut.dataset()?.id && n.data.outs.some(o => o.name === col)),
  blocks: (type, preset) => allNodes().filter(n => n.data.type === type && (preset === undefined || n.data.preset === preset)),
  edge(from, fc, to, tc) {
    if (!from || !to) return false;
    return edgesOf(Blocks.editor.export()).some(e =>
      e.from[0] === String(from.id) && e.from[1] === fc && e.to[0] === String(to.id) && e.to[1] === tc);
  },
  touches: (n, bus) => !!n && !!bus && edgesOf(Blocks.editor.export()).some(e =>
    (e.from[0] === String(n.id) && e.to[0] === String(bus.id)) || (e.to[0] === String(n.id) && e.from[0] === String(bus.id))),
  /* the block of a type wired to a bus, else the first unwired one */
  find(type, preset, kind) {
    const list = this.blocks(type, preset), bus = this.bus(kind);
    return list.find(n => this.touches(n, bus)) || list.find(n => !edgesOf(Blocks.editor.export()).some(e =>
      e.from[0] === String(n.id) && Blocks.editor.getNodeFromId(e.to[0])?.data.kind === "bus" ||
      e.to[0] === String(n.id) && Blocks.editor.getNodeFromId(e.from[0])?.data.kind === "bus"));
  },
  param: (n, k, v) => !!n && n.data.params[k] !== null && n.data.params[k] !== undefined && Math.abs(n.data.params[k] - v) < 1e-9,
  dataWire(n, param, col) {
    return allNodes().some(d => d.data.kind === "data" && d.data.dataset === this.dataset()?.id && this.edge(d, col, n, param));
  },

  /* ---------------------------------------------------------- canvas actions */
  setParams(n, params) {
    const node = Blocks.editor.getNodeFromId(n.id);
    Object.assign(node.data.params, params);
    Blocks.editor.updateNodeDataFromId(n.id, node.data);
    if (String(Panel.nodeId) === String(n.id)) Panel.show(n.id);
    App.dirty();
  },
  wire(from, fc, to, tc) { if (!this.edge(from, fc, to, tc)) connectByName([String(from.id), fc], [String(to.id), tc]); },
  node: id => Blocks.editor.getNodeFromId(id),
  ensure(found, create) { return found || this.node(create()); },
  addPreset(id, x, y) { return Blocks.addComponent(x, y, Blocks.catalog.presets.find(p => p.id === id).type, Blocks.catalog.presets.find(p => p.id === id)); },
  async loadData() {
    if (this.dataset()) return this.dataset();
    const ds = await api("/api/tutorial/data", { method: "POST" });
    App.datasets[ds.id] = ds;
    App.renderDatasets();
    return ds;
  },
  /* the input block of a column, placed at (x, y) if it is not on the canvas yet */
  async inputBlock(col, x, y) {
    const ds = await this.loadData();
    return this.input(col) || this.node(App.addInput(ds.id, col, x, y));
  },
  /* highlight the input in the palette, or its block's connector once it is on the canvas */
  inputTarget(col) { const b = this.input(col); return b ? this.conn(b, "out", col) : `.ds-col[data-col="${col}"]`; },
  settingsOk() {
    const s = App.settings();
    return s.start === "2025-06-02" && s.days === 2 && s.freq === "1h";
  },
  palette: sel => `#palette ${sel}`,
  conn(n, side, name) {
    if (!n) return null;
    const i = connectorIndex(n.data, side, name);
    return i < 0 ? null : `#node-${n.id} .${side === "in" ? "input" : "output"}_${i + 1}`;
  },
  async runAndWait() { await App.run(); },
  cost: () => Results.summary ? eur(Results.summary.total_cost) : "–",
};

/* expected total costs (2 days) after each run - checked by tests/integration/test_ui_browser.py */
const TUT_EXPECTED = { run1: 3240, run2: 515, run3: 1479 };
const eur = v => `${Math.round(v).toLocaleString()} €`;

const TUT_STEPS = [
  {
    title: "Welcome",
    html: `<p>You will build a small industrial site and let the optimiser find the cheapest way to run it:</p>
      <ol><li>an electricity <b>bus</b> with a <b>grid connection</b> and a fixed demand</li>
      <li>rooftop <b>PV</b> and a <b>battery</b></li><li>a second carrier, <b>heat</b>, with a heat pump, a heat
      storage and a process-heat demand</li></ol>
      <p>Each step has a checklist that ticks itself when you have done it on the canvas, and what to use next
      pulses blue. Stuck? Press <b>Do it for me</b>.</p><p class="hint">The same site is built in code in the README tutorial.</p>`,
    checks: [],
  },
  {
    title: "Set the time horizon",
    html: `<p>Every run covers a time horizon at a fixed resolution. In the top bar set <b>Start</b> to
      <code>2025-06-02</code>, <b>Days</b> to <code>2</code> and <b>Resolution</b> to <code>1h</code>.</p>
      <p class="hint">48 hourly steps. The same model also runs at 15-min resolution without changing any
      parameter.</p>`,
    target: () => ["#topbar .settings"],
    checks: [["Start 2025-06-02, 2 days, 1h", () => Tut.settingsOk()]],
    auto() {
      App.applySettings({ start: "2025-06-02", days: 2, freq: "1h", tz: "Europe/Copenhagen" });
      App.dirty();
    },
  },
  {
    title: "Load the input data",
    html: `<p>Time series come from CSV files. With <b>Upload CSV data</b> every column of a file appears as an
      input in the <b>Inputs</b> section of the palette. Each input becomes its own block when you drag it onto
      the canvas.</p>
      <p>For this tutorial, load the prepared data: hourly <code>price</code> (60 €/MWh at night, 120 €/MWh from
      7 to 21 h), <code>pv_cf</code> (PV output per MW installed) and <code>heat_load</code> (MW).</p>
      <button class="primary" data-tut="load">Load tutorial data</button>`,
    target: () => ['[data-tut="load"]'],
    checks: [["price, pv_cf and heat_load listed under Inputs", () => !!Tut.dataset()]],
    auto: () => Tut.loadData(),
  },
  {
    title: "Add an electricity bus",
    html: `<p>A <b>bus</b> is a central node with its own energy balance: in every hour, energy in = energy out.
      All blocks of one carrier connect to it.</p>
      <p>Drag <b>Bus</b> from the palette onto the canvas. A new bus is an electricity bus named <code>el</code>.</p>`,
    target: () => [Tut.palette(".pal-item.bus")],
    checks: [["Electricity bus on the canvas", () => !!Tut.bus("electricity")]],
    auto: () => Blocks.addBus(460, 170),
  },
  {
    title: "Connect the grid",
    html: `<p>Drag <b>Grid connection</b> onto the canvas. Then drag a wire from its <code>⇄ import / export</code>
      connector (right side) to the bus's <code>supply</code> connector (left side).</p>
      <p>Grids and storages have a single <b>⇄</b> connector: the one wire carries energy both ways, and after a
      run it shows both directions.</p>`,
    target: () => {
      const g = Tut.find("GridConnection", undefined, "electricity");
      return g ? [Tut.conn(g, "out", "link"), Tut.conn(Tut.bus("electricity"), "in", "supply")] :
        [Tut.palette('.pal-item[data-type="GridConnection"]')];
    },
    checks: [
      ["Grid connection block", () => Tut.blocks("GridConnection").length > 0],
      ["grid ⇄ → el supply", () => Tut.edge(Tut.find("GridConnection", undefined, "electricity"), "link", Tut.bus("electricity"), "supply")],
    ],
    auto() {
      const el = Tut.ensure(Tut.bus("electricity"), () => Blocks.addBus(460, 170));
      const g = Tut.ensure(Tut.find("GridConnection", undefined, "electricity"), () => Blocks.addComponent(220, 20, "GridConnection"));
      Tut.wire(g, "link", el, "supply");
    },
  },
  {
    title: "Set the grid tariff",
    html: `<p>Grey square connectors are <b>parameters</b> that take a time series. Drag <code>price</code> from
      <b>Inputs</b> onto the canvas and wire it to the grid's <code>import_price</code> and also to
      <code>export_price</code>: one input can feed several parameters.</p>
      <p>Then click the grid block and, in the properties panel on the right, set <b>import fee</b> to
      <code>40</code> €/MWh, <b>max import</b> to <code>2</code> MW and <b>max export</b> to <code>1</code> MW.</p>
      <p class="hint">Unwired parameters use the constant typed in the panel. Max export defaults to 0, so a grid
      connection is import-only unless you allow export.</p>`,
    target: () => {
      const g = Tut.find("GridConnection", undefined, "electricity");
      return [Tut.inputTarget("price"), Tut.conn(g, "in", "import_price"), Tut.conn(g, "in", "export_price"), "#panel"];
    },
    checks: [
      ["price → import_price", () => Tut.dataWire(Tut.find("GridConnection", undefined, "electricity"), "import_price", "price")],
      ["price → export_price", () => Tut.dataWire(Tut.find("GridConnection", undefined, "electricity"), "export_price", "price")],
      ["import fee 40 €/MWh", () => Tut.param(Tut.find("GridConnection", undefined, "electricity"), "import_fee", 40)],
      ["max import 2 MW, max export 1 MW", () => {
        const g = Tut.find("GridConnection", undefined, "electricity");
        return Tut.param(g, "max_import", 2) && Tut.param(g, "max_export", 1);
      }],
    ],
    async auto() {
      const d = await Tut.inputBlock("price", 10, 50);
      const g = Tut.find("GridConnection", undefined, "electricity");
      Tut.wire(d, "price", g, "import_price");
      Tut.wire(d, "price", g, "export_price");
      Tut.setParams(g, { import_fee: 40, max_import: 2, max_export: 1 });
      Panel.show(g.id);
    },
  },
  {
    title: "Add a demand",
    html: `<p>Drag <b>Demand</b> onto the canvas and wire the bus's <code>withdraw</code> to the demand's
      <code>in</code>. In the panel set <b>profile</b> to <code>0.5</code> MW: a constant load.</p>`,
    target: () => {
      const dm = Tut.find("Demand", undefined, "electricity");
      return dm ? [Tut.conn(Tut.bus("electricity"), "out", "withdraw"), Tut.conn(dm, "in", "in"), "#panel"] :
        [Tut.palette('.pal-item[data-type="Demand"]')];
    },
    checks: [
      ["Demand wired to the el bus", () => Tut.edge(Tut.bus("electricity"), "withdraw", Tut.find("Demand", undefined, "electricity"), "in")],
      ["profile 0.5 MW", () => Tut.param(Tut.find("Demand", undefined, "electricity"), "profile", 0.5)],
    ],
    auto() {
      const el = Tut.bus("electricity");
      const dm = Tut.ensure(Tut.find("Demand", undefined, "electricity"), () => Blocks.addComponent(670, 20, "Demand"));
      Tut.wire(el, "withdraw", dm, "in");
      Tut.setParams(dm, { profile: 0.5 });
    },
  },
  {
    title: "Run the model",
    html: `<p>Press <b>▶ Run</b>. The model is checked, built and solved; then every wire shows the energy it carried
      and the results drawer opens at the bottom.</p>
      <p>Check by hand: 28 day hours × 0.5 MW × (120 + 40) + 20 night hours × 0.5 MW × (60 + 40) =
      <b>${eur(TUT_EXPECTED.run1)}</b>.</p>`,
    keepResults: true,
    target: () => ["#btn-run"],
    checks: [[() => `Run solved: total cost ${Tut.cost()} (expected ${eur(TUT_EXPECTED.run1)})`, () => !!Results.summary]],
    auto: () => Tut.runAndWait(),
  },
  {
    title: "Add rooftop PV",
    html: `<p>From <b>Presets</b> drag <b>PV</b> onto the canvas. Wire its <code>out</code> to the el bus
      <code>supply</code>. Then drag the input <code>pv_cf</code> onto the canvas and wire it to the PV's
      <code>cf</code> connector.</p>
      <p class="hint">Output = capacity (1 MW in the panel) × cf. PV is curtailable: surplus the site cannot use
      is exported (up to 1 MW) or curtailed.</p>`,
    target: () => {
      const pv = Tut.find("Producer", "pv", "electricity");
      return pv ? [Tut.conn(pv, "out", "out"), Tut.conn(Tut.bus("electricity"), "in", "supply"), Tut.inputTarget("pv_cf"), Tut.conn(pv, "in", "cf")] :
        [Tut.palette('.pal-item[data-preset="pv"]')];
    },
    checks: [
      ["PV out → el supply", () => Tut.edge(Tut.find("Producer", "pv", "electricity"), "out", Tut.bus("electricity"), "supply")],
      ["pv_cf → cf", () => Tut.dataWire(Tut.find("Producer", "pv", "electricity"), "cf", "pv_cf")],
    ],
    async auto() {
      const pv = Tut.ensure(Tut.find("Producer", "pv", "electricity"), () => Tut.addPreset("pv", 220, 250));
      const d = await Tut.inputBlock("pv_cf", 10, 280);
      Tut.wire(pv, "out", Tut.bus("electricity"), "supply");
      Tut.wire(d, "pv_cf", pv, "cf");
    },
  },
  {
    title: "Add a battery",
    html: `<p>From <b>Presets</b> drag <b>Battery</b> and wire the el bus <code>withdraw</code> to the battery's
      <code>⇄ charge / discharge</code> connector (left side).</p>
      <p class="hint">The preset is 2 MWh / 1 MW with 95 % charge and discharge efficiency. Click it to see or
      change the parameters.</p>`,
    target: () => {
      const b = Tut.find("Storage", "battery", "electricity");
      return b ? [Tut.conn(Tut.bus("electricity"), "out", "withdraw"), Tut.conn(b, "in", "link")] :
        [Tut.palette('.pal-item[data-preset="battery"]')];
    },
    checks: [
      ["el withdraw → battery ⇄", () => Tut.edge(Tut.bus("electricity"), "withdraw", Tut.find("Storage", "battery", "electricity"), "link")],
    ],
    auto() {
      const b = Tut.ensure(Tut.find("Storage", "battery", "electricity"), () => Tut.addPreset("battery", 670, 170));
      Tut.wire(Tut.bus("electricity"), "withdraw", b, "link");
    },
  },
  {
    title: "Run again",
    html: `<p>Press <b>▶ Run</b>. PV now covers most of the daytime load and the battery shifts midday PV and cheap
      night energy into the expensive hours. Look at the MWh labels on the wires and the badges under the blocks
      (cost, full-load hours, cycles).</p>`,
    keepResults: true,
    target: () => ["#btn-run"],
    checks: [[() => `Run solved: total cost ${Tut.cost()} (expected about ${eur(TUT_EXPECTED.run2)})`, () => !!Results.summary]],
    auto: () => Tut.runAndWait(),
  },
  {
    title: "Add a heat bus",
    html: `<p>Now a second carrier. Drag another <b>Bus</b> onto the canvas, click it and set <b>Carrier kind</b>
      to <code>heat</code> in the panel (you may also rename it to <code>heat</code>).</p>
      <p class="hint">Each carrier, and each temperature level, gets its own bus. The colour shows the kind.</p>`,
    target: () => [Tut.palette(".pal-item.bus"), "#panel"],
    checks: [["Bus with carrier kind heat", () => !!Tut.bus("heat")]],
    auto() {
      if (Tut.bus("heat")) return;
      const id = Blocks.addBus(460, 450, { carrier_kind: "heat", name: "heat" });
      Panel.show(id);
    },
  },
  {
    title: "Add a heat pump",
    html: `<p>From <b>Presets</b> drag <b>Heat pump</b>. Wire el <code>withdraw</code> → heat pump <code>in1</code>
      (electricity) and heat pump <code>out1</code> → heat bus <code>supply</code>.</p>
      <p>In the panel set <b>p_max</b> to <code>1.5</code> MW. The capacity applies to out1, the heat output.</p>
      <p class="hint">A heat pump is a <b>Converter</b>: out1 = 3 × in1 (COP 3). Wire a data column to
      <code>coef out1</code> for a time-varying COP.</p>`,
    target: () => {
      const hp = Tut.find("Converter", "heat_pump", "heat");
      return hp ? [Tut.conn(Tut.bus("electricity"), "out", "withdraw"), Tut.conn(hp, "in", "in1"), Tut.conn(hp, "out", "out1"),
        Tut.conn(Tut.bus("heat"), "in", "supply"), "#panel"] : [Tut.palette('.pal-item[data-preset="heat_pump"]')];
    },
    checks: [
      ["el withdraw → heat pump in1", () => Tut.edge(Tut.bus("electricity"), "withdraw", Tut.find("Converter", "heat_pump", "heat"), "in1")],
      ["heat pump out1 → heat supply", () => Tut.edge(Tut.find("Converter", "heat_pump", "heat"), "out1", Tut.bus("heat"), "supply")],
      ["p_max 1.5 MW", () => Tut.param(Tut.find("Converter", "heat_pump", "heat"), "p_max", 1.5)],
    ],
    auto() {
      const hp = Tut.ensure(Tut.find("Converter", "heat_pump", "heat"), () => Tut.addPreset("heat_pump", 220, 440));
      Tut.wire(Tut.bus("electricity"), "withdraw", hp, "in1");
      Tut.wire(hp, "out1", Tut.bus("heat"), "supply");
      Tut.setParams(hp, { p_max: 1.5 });
    },
  },
  {
    title: "Add heat storage",
    html: `<p>From <b>Presets</b> drag <b>Heat storage</b> and wire the heat bus <code>withdraw</code> to its
      <code>⇄</code> connector. In the panel set <b>e_max</b> <code>4</code> MWh, <b>p_ch_max</b> and <b>p_dis_max</b>
      <code>1</code> MW and <b>self_discharge</b> <code>0.005</code> per hour.</p>
      <p class="hint">The tank lets the heat pump run when electricity is cheap or PV is available.</p>`,
    target: () => {
      const t = Tut.find("Storage", "tes", "heat");
      return t ? [Tut.conn(Tut.bus("heat"), "out", "withdraw"), Tut.conn(t, "in", "link"), "#panel"] :
        [Tut.palette('.pal-item[data-preset="tes"]')];
    },
    checks: [
      ["heat withdraw → storage ⇄", () => Tut.edge(Tut.bus("heat"), "withdraw", Tut.find("Storage", "tes", "heat"), "link")],
      ["e_max 4 MWh, 1 MW charge/discharge", () => {
        const t = Tut.find("Storage", "tes", "heat");
        return Tut.param(t, "e_max", 4) && Tut.param(t, "p_ch_max", 1) && Tut.param(t, "p_dis_max", 1);
      }],
      ["self_discharge 0.005 /h", () => Tut.param(Tut.find("Storage", "tes", "heat"), "self_discharge", 0.005)],
    ],
    auto() {
      const t = Tut.ensure(Tut.find("Storage", "tes", "heat"), () => Tut.addPreset("tes", 670, 400));
      Tut.wire(Tut.bus("heat"), "withdraw", t, "link");
      Tut.setParams(t, { e_max: 4, p_ch_max: 1, p_dis_max: 1, self_discharge: 0.005 });
    },
  },
  {
    title: "Add the heat demand",
    html: `<p>Drag another <b>Demand</b> and wire heat <code>withdraw</code> → its <code>in</code>. Then drag the
      input <code>heat_load</code> onto the canvas and wire it to the demand's <code>profile</code>: 0.8 MW from 6
      to 18 h, 0.3 MW otherwise.</p>`,
    target: () => {
      const dm = Tut.find("Demand", undefined, "heat");
      return dm ? [Tut.conn(Tut.bus("heat"), "out", "withdraw"), Tut.conn(dm, "in", "in"), Tut.inputTarget("heat_load"), Tut.conn(dm, "in", "profile")] :
        [Tut.palette('.pal-item[data-type="Demand"]')];
    },
    checks: [
      ["heat withdraw → demand in", () => Tut.edge(Tut.bus("heat"), "withdraw", Tut.find("Demand", undefined, "heat"), "in")],
      ["heat_load → profile", () => Tut.dataWire(Tut.find("Demand", undefined, "heat"), "profile", "heat_load")],
    ],
    async auto() {
      const dm = Tut.ensure(Tut.find("Demand", undefined, "heat"), () => Blocks.addComponent(670, 560, "Demand"));
      const d = await Tut.inputBlock("heat_load", 460, 640);
      Tut.wire(Tut.bus("heat"), "withdraw", dm, "in");
      Tut.wire(d, "heat_load", dm, "profile");
    },
  },
  {
    title: "Run the full site",
    html: `<p>Press <b>▶ Run</b>. The optimiser now also decides when the heat pump runs and when the tank is
      charged.</p>`,
    keepResults: true,
    target: () => ["#btn-run"],
    checks: [[() => `Run solved: total cost ${Tut.cost()} (expected about ${eur(TUT_EXPECTED.run3)})`, () => !!Results.summary]],
    auto: () => Tut.runAndWait(),
  },
  {
    title: "Explore the results",
    html: `<ul><li><b>Summary</b>: cost per category and KPIs (self-consumption, cycles, full-load hours,
      average marginal price per bus).</li>
      <li><b>Costs</b>: cost per block. REVENUE is negative: exported PV.</li>
      <li><b>Bus balance</b>: pick a bus to see every flow in and out per hour and its <b>marginal price</b>.
      On the heat bus it is about the electricity price ÷ COP.</li>
      <li><b>Component</b>: flows and storage level of one block.</li>
      <li><b>Download CSV</b> for all tables, <b>Save</b> to keep the project, <b>Export config</b> to run it
      from the command line.</li></ul>
      <p>Next, try a CO₂ price in the top bar, a peak charge on the grid, or Resolution 15min.</p>`,
    keepResults: true,
    target: () => ["#res-tabs", "#res-download"],
    checks: [],
  },
];

const Tutorial = {
  i: 0,
  el: null,
  timer: null,

  open() {
    if (!this.el) this.build();
    if (this.i === 0 && allNodes().length && confirm("The tutorial starts from an empty canvas. Clear the canvas?")) {
      Blocks.editor.clear(); Panel.clear(); Results.summary = null;
    }
    this.el.classList.remove("hidden");
    this.render();
    clearInterval(this.timer);
    this.timer = setInterval(() => this.refresh(), 500);
  },

  close() {
    this.el.classList.add("hidden");
    clearInterval(this.timer);
    this.highlight([]);
  },

  build() {
    const el = document.getElementById("tutorial");
    el.innerHTML = `<div class="tut-head"><span id="tut-count"></span><b id="tut-title"></b>
        <button class="small" id="tut-min" title="Minimise">–</button><button class="small" id="tut-close" title="Close">✕</button></div>
      <div class="tut-main"><div id="tut-body"></div><ul id="tut-checks"></ul></div>
      <div class="tut-foot"><button id="tut-back">‹ Back</button><button id="tut-auto">Do it for me</button>
        <button id="tut-next">Next ›</button></div>`;
    this.el = el;
    el.querySelector("#tut-close").onclick = () => this.close();
    el.querySelector("#tut-min").onclick = () => el.classList.toggle("min");
    el.querySelector("#tut-back").onclick = () => this.go(this.i - 1);
    el.querySelector("#tut-next").onclick = () => this.i === TUT_STEPS.length - 1 ? this.close() : this.go(this.i + 1);
    el.querySelector("#tut-auto").onclick = async () => {
      const b = el.querySelector("#tut-auto");
      b.disabled = true;
      try { await TUT_STEPS[this.i].auto?.(); } catch (e) { toast(e.message, true); } finally { b.disabled = false; this.refresh(); }
    };
    el.querySelector("#tut-body").addEventListener("click", e => {
      if (e.target.dataset.tut === "load") Tut.loadData().then(() => this.refresh()).catch(err => toast(err.message, true));
    });
  },

  go(i) {
    this.i = Math.max(0, Math.min(TUT_STEPS.length - 1, i));
    this.el.classList.remove("min");
    if (!TUT_STEPS[this.i].keepResults) {
      document.getElementById("results").classList.add("collapsed");
      document.body.classList.remove("results-open");
    }
    this.render();
  },

  render() {
    const s = TUT_STEPS[this.i];
    this.el.querySelector("#tut-count").textContent = `${this.i + 1}/${TUT_STEPS.length}`;
    this.el.querySelector("#tut-title").textContent = s.title;
    this.el.querySelector("#tut-body").innerHTML = s.html;
    this.el.querySelector("#tut-back").disabled = this.i === 0;
    this.el.querySelector("#tut-auto").style.display = s.auto ? "" : "none";
    this.refresh();
  },

  refresh() {
    const s = TUT_STEPS[this.i];
    const results = s.checks.map(([label, fn]) => {
      let ok = false;
      try { ok = !!fn(); } catch (_) { /* block not there yet */ }
      return { ok, label: typeof label === "function" ? label() : label };
    });
    this.el.querySelector("#tut-checks").innerHTML = results.map(r =>
      `<li class="${r.ok ? "ok" : ""}">${r.ok ? "✓" : "○"} ${esc(r.label)}</li>`).join("");
    const done = results.every(r => r.ok);
    const next = this.el.querySelector("#tut-next");
    next.textContent = this.i === TUT_STEPS.length - 1 ? "Finish" : done ? "Next ›" : "Skip ›";
    next.classList.toggle("primary", done);
    this.el.classList.toggle("done", done && results.length > 0);
    let targets = [];
    try { targets = done ? [] : (s.target?.() || []); } catch (_) { /* ignore */ }
    this.highlight(targets.filter(Boolean));
  },

  highlight(selectors) {
    document.querySelectorAll(".tut-hl").forEach(x => x.classList.remove("tut-hl"));
    for (const sel of selectors) document.querySelectorAll(sel).forEach(x => x.classList.add("tut-hl"));
  },
};
