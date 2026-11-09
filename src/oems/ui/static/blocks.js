/* Block creation, connector metadata and wiring rules for the Drawflow canvas.
 *
 * Every node stores its connector meaning in node.data:
 *   data.ins[i]  describes Drawflow connector input_{i+1}
 *   data.outs[i] describes output_{i+1}
 * with {name, type: "energy" | "data" | "bus", label, kind (carrier kind or null), flow ("in" | "out" | "both")}.
 *
 * Energy ports carry no direction on the canvas: every energy port of a component is an output connector on its right
 * side and every bus has ONE "port" input; the backend reads each port's direction (inflow / outflow) from the
 * component type. Parameters (grey) are the component's inputs on the left.
 */
const ICONS = { grid: "🔌", sun: "☀️", plug: "🏭", sink: "🌬️", shift: "⏱️", battery: "🔋", convert: "🔁",
  thermometer: "🌡️", hp: "♨️", boiler: "🔥", chiller: "❄️", flame: "🔥", chp: "⚙️", link: "🔗", tank: "🛢️", box: "▫️" };
/* palette tiles: line icon per catalog icon, tinted by the carrier the block mostly serves (by preset id or type) */
const TILE_SVG = { grid: "grid", sun: "sun", plug: "plug", sink: "sink", shift: "shift", battery: "battery", convert: "convert",
  thermometer: "thermo", hp: "hp", boiler: "flame", chiller: "snow", flame: "flame", chp: "chp", link: "link", tank: "tank",
  box: "box", bus: "bus", gear: "gear", note: "note" };
const TILE_KIND = { GridConnection: "el", pv: "el", battery: "el", chp: "el", tes: "heat", heat_pump: "heat", e_boiler: "heat",
  gas_boiler: "heat", ThermalZone: "heat", chiller: "cold" };
const GROUP_LABELS = { Producer: "Production", Consumer: "Demand", Converter: "Conversion" };
/* palette group order, in the order a site is usually built (unknown groups go before Settings) */
const GROUP_ORDER = ["Nodes", "Grid", "Production", "Storage", "Conversion", "Demand", "Thermal", "Settings", "Notes"];
const tileIcon = (icon, kind) => `<span class="ico t-${kind || "any"}"><svg><use href="#i-${TILE_SVG[icon] || "box"}"/></svg></span>`;
const SHORT_NAMES = { GridConnection: "grid", Producer: "prod", Demand: "demand", FlexibleDemand: "flex", Sink: "sink",
  Storage: "store", Converter: "conv", ThermalZone: "zone" };
const KIND_ICON = { electricity: "⚡", heat: "🔥", cold: "❄️", fuel: "🛢️", other: "◆" };

/* what a block does and how to wire it: the tooltip of the block on the canvas and of its palette item */
const BLOCK_HINTS = {
  bus: "Bus: the meeting point of one energy carrier. Energy in = energy out in every time step. Wire the energy " +
    "ports of components (right side) to it; the model knows which way each port flows.",
  data: "Input series: one column of an uploaded CSV. Wire it to grey parameter connectors (prices, profiles, " +
    "limits …). Data at another resolution is aligned to the run automatically.",
  profile: "Daily profile: one day of values (hourly, 30 or 15 min) that you draw or type in its panel. Every day " +
    "of the run repeats it by local time of day. Wire it to grey parameter connectors like a data column.",
  dbconn: "DB connection: login details for the BESS IQ database (the password stays on the server). DB input " +
    "blocks pull their series through it. Not wired.",
  db: "DB input: one series pulled from the database for the run period (market prices, tariffs, generation, " +
    "weather …). Wire it to grey parameter connectors.",
  "db:solar": "PV profile: modelled PV output per MWp (0…1) from the database. Wire it to a PV block's cf.",
  "db:degradation": "Degradation curve: battery state-of-health curves by cycling rate from the database. Wire it " +
    "to a battery's SoH curves for multi-year runs.",
  note: "Note: free text for your notes on the analysis. Type into it on the canvas; runs ignore it.",
  invest: "Run settings: the CO₂ cap, the discount rate and the targets of a lifetime optimisation (IRR, payback, " +
    "self-sufficiency). Not wired, one per project.",
  GridConnection: "Grid connection: buys (import) and sells (export) energy at the wired prices and fees, within " +
    "its import / export limits; peak and fixed charges are optional. One ⇄ wire to a bus covers both directions.",
  Producer: "Producer: generation limited by an availability profile (capacity × cf per step), e.g. PV or wind. " +
    "Can be curtailed if allowed. Wire its output port to a bus.",
  Demand: "Demand: a fixed load that must be met in every step (the wired or typed profile, MW). Wire its port to a bus.",
  FlexibleDemand: "Flexible demand: a load that can be shifted in time within its limits (a backlog that must be " +
    "served later). Wire its port to a bus.",
  Sink: "Sink: an optional energy dump (dry cooler, dump load) with a cost per MWh, so surplus energy has somewhere to go.",
  Storage: "Storage: charges and discharges energy with efficiency losses and self-discharge, within its energy " +
    "and power limits. One ⇄ wire to a bus covers charge and discharge.",
  Converter: "Converter: turns one carrier into others at fixed ratios (inputs → outputs), up to its capacity. Wire " +
    "each input and output port to the bus of its carrier.",
  ThermalZone: "Thermal zone: a building's thermal mass, kept inside a temperature band; heat in and losses to the " +
    "outside set its temperature.",
  "preset:heat_pump": "Heat pump: electricity in, heat out (COP 3 by default; wire a COP series to vary it). Wire " +
    "the inputs and outputs to an electricity bus and a heat bus.",
  "preset:e_boiler": "E-boiler: electricity in, heat out at 99 % efficiency. Wire it to an electricity bus and a heat bus.",
  "preset:chiller": "Chiller: electricity in, cold out (COP 4 by default). Wire it to an electricity bus and a cold bus.",
  "preset:gas_boiler": "Gas boiler: fuel in, heat out at 92 % efficiency. Wire it to a fuel bus and a heat bus.",
  "preset:chp": "CHP: fuel in, electricity (38 %) and heat (45 %) out at the same time. Wire it to a fuel, an " +
    "electricity and a heat bus.",
  "preset:link": "Link / cascade: moves energy from one bus to another at a fixed ratio, e.g. between two heat " +
    "temperature levels.",
  "preset:battery": "Battery: stores electricity (2 MWh / 1 MW, 95 % efficiency each way by default). Can be sized " +
    "by a lifetime optimisation. One ⇄ wire to an electricity bus.",
  "preset:tes": "Heat storage: a hot-water tank (10 MWh / 3 MW, 0.3 % loss per hour by default). One ⇄ wire to a heat bus.",
  "preset:pv": "PV: solar generation, capacity (MWp) × the wired capacity factor (0…1) per step. Can be sized by a " +
    "lifetime optimisation. Wire its output to an electricity bus.",
};

const Blocks = {
  catalog: null,
  editor: null,

  compSpec(type) { return this.catalog.components.find(c => c.type === type); },

  /* connectors of a component from its catalog entry (and converter definition): energy ports on the right, parameters
     on the left */
  componentConnectors(spec, conv) {
    const ins = [], outs = [];
    if (spec.dynamic) {
      for (const it of conv.inputs) outs.push({ name: it.key, type: "energy", label: it.key, kind: it.kind || null, flow: "in" });
      for (const it of conv.outputs) outs.push({ name: it.key, type: "energy", label: it.key, kind: it.kind || null, flow: "out" });
      for (const it of conv.outputs) ins.push({ name: "coef:" + it.key, type: "data", label: "coef " + it.key });
    } else {
      const link = spec.link;  // one port for both directions (grid import/export, storage charge/discharge)
      if (link) outs.push({ name: link.connector, type: "energy", label: link.label, kind: null, flow: "both", ports: link.ports });
      for (const e of spec.energy) {
        if (link?.ports.includes(e.port)) continue;
        outs.push({ name: e.port, type: "energy", label: e.port, kind: e.carrier_kind || null, flow: e.direction });
      }
      for (const f of spec.data_connectors) ins.push({ name: f, type: "data", label: f });
    }
    return { ins, outs };
  },

  /* a bus: one connector that takes every component port, whatever its direction */
  busConnectors: kind => ({ ins: [{ name: "port", type: "bus", label: "", kind }], outs: [] }),

  html(title, typeLabel, icon, nConn, extra = "") {
    const h = Math.max(1, nConn) * 20 + 8;
    return `<div class="blk-head"><span class="icon">${icon}</span><span class="blk-title">${esc(title)}</span>` +
      `<span class="blk-type">${esc(typeLabel)}</span></div>` +
      `<div class="blk-body" style="min-height:${h}px">${extra}</div><div class="blk-badges"></div>`;
  },

  addBus(x, y, opts = {}) {
    const kind = opts.carrier_kind || "electricity";
    const name = opts.name || uniqueName(kind === "electricity" ? "el" : kind);
    const data = { kind: "bus", name, carrier: opts.carrier || kind, carrier_kind: kind,
      emission_factor: opts.emission_factor ?? "", allow_slack: !!opts.allow_slack, ...this.busConnectors(kind) };
    const id = this.editor.addNode("bus", 1, 0, x, y, `bus-node bus-${kind}`, data,
      this.html(name, "", KIND_ICON[kind] || "◆", 1, `<div class="bus-carrier">${esc(data.carrier)}</div>`));
    this.decorate(id);
    return id;
  },

  /* one input series (a column of an uploaded dataset) = one block with a single output */
  addData(x, y, ds, column) {
    const outs = [{ name: column, type: "data", label: "" }];
    const data = { kind: "data", dataset: ds.id, column, label: ds.name, resolution: ds.resolution, ins: [], outs };
    const id = this.editor.addNode("data", 0, 1, x, y, "data-node", data,
      this.html(column, ds.resolution || "", "📈", 1, `<div class="data-src">${esc(ds.name)}</div>`));
    this.decorate(id);
    return id;
  },

  /* one day of values typed in the panel (24 hourly, 48 or 96), repeated on every day of the run by local time */
  addProfile(x, y, opts = {}) {
    const outs = [{ name: "value", type: "data", label: "" }];
    const data = { kind: "profile", name: uniqueName("profile"), unit: "MW", values: Array(24).fill(0), ...opts, ins: [], outs };
    const id = this.editor.addNode("profile", 0, 1, x, y, "data-node profile-node", data,
      this.html(data.name, "daily", "🕒", 1, `<div class="data-src"></div>`));
    this.decorate(id);
    this.updateProfile(id);
    return id;
  },

  updateProfile(id) {
    const d = this.editor.getNodeFromId(id).data;
    this.setTitle(id, d.name);
    const src = document.querySelector(`#node-${id} .data-src`);
    if (src) src.innerHTML = `${sparkline(d.values)}${esc(fmt(Math.min(...d.values)))}–${esc(fmt(Math.max(...d.values)))} ${esc(d.unit || "")}`;
  },

  /* connection details of a database (the password stays on the server, never in the project) */
  addDbConn(x, y, opts = {}) {
    const data = { kind: "dbconn", name: uniqueName("bess_iq"), host: "", port: 5432, user: "", database: "postgres",
      sslmode: "require", ...opts, ins: [], outs: [] };
    const id = this.editor.addNode("dbconn", 0, 0, x, y, "dbconn-node", data,
      this.html(data.name, "DB", "🔐", 1, `<div class="data-src"></div>`));
    this.decorate(id);
    this.updateDbConn(id);
    return id;
  },

  updateDbConn(id) {
    const d = this.editor.getNodeFromId(id).data;
    this.setTitle(id, d.name);
    const src = document.querySelector(`#node-${id} .data-src`);
    if (src) src.textContent = d.host ? `${d.database} @ ${d.host.split(".")[0]}` : "not set up";
  },

  dbConnections() { return allNodes().filter(n => n.data.kind === "dbconn"); },

  /* Run settings block (kind "invest"): the CO₂ cap and the lifetime optimisation's discount rate and hurdles; not
     wired, at most one per project (the sizes to optimise are set on the PV and storage blocks themselves) */
  addInvest(x, y, opts = {}) {
    // a multi-year project from before the block held the discount rate and inflation keeps its own
    const v = id => parseFloat(document.getElementById(id).value) || 0;
    const kept = RunSettings.mode() === "multi_year" ? { discount_rate: v("set-discount"), inflation: v("set-inflation") } : {};
    const data = { kind: "invest", co2_cap: null, discount_rate: 7, inflation: 0, min_irr: null, max_payback: null, min_self_sufficiency: null,
      payback_kind: "simple", ...kept, ...opts, ins: [], outs: [] };
    const id = this.editor.addNode("invest", 0, 0, x, y, "invest-node", data,
      this.html("Run settings", "", "⚙️", 1, `<div class="invest-list"></div>`));
    this.decorate(id);
    this.updateInvest(id);
    return id;
  },

  /* Note block (kind "note"): free text typed on the canvas, saved with the project; not wired, runs ignore it */
  addNote(x, y, opts = {}) {
    const data = { kind: "note", text: "", ...opts, ins: [], outs: [] };
    const id = this.editor.addNode("note", 0, 0, x, y, "note-node", data,
      this.html("Note", "", "📝", 1, `<textarea df-text placeholder="Notes on the analysis…" spellcheck="true"></textarea>`));
    this.decorate(id);
    return id;
  },

  investNode() { return allNodes().find(n => n.data.kind === "invest"); },

  /* components that can be sized (PV and storages) */
  sizeable() { return allNodes().filter(n => n.data.kind === "component" && this.catalog.sizing[n.data.type]); },

  /* the CAPEX / OPEX fields of a component type (required when it is optimised) */
  costFields(type) { return (this.catalog.sizing[type] || []).filter(f => f.name.startsWith("capex") || f.name === "opex"); },

  /* PV and storages switched to variable size: [{node, asset: its sizing, missing: [labels of empty CAPEX/OPEX]}] */
  optimisedAssets() {
    return this.sizeable().filter(n => n.data.sizing?.optimise).map(n => ({
      node: n, asset: n.data.sizing,
      missing: this.costFields(n.data.type).filter(f => [undefined, null, ""].includes(n.data.sizing[f.name])).map(f => f.label) }));
  },

  /* projects saved before the sizing moved onto the components: move the Run settings block's assets onto them */
  migrateSizing() {
    const inv = this.investNode();
    if (!inv?.data.assets) return;
    for (const n of this.sizeable()) {
      const a = inv.data.assets[n.id];
      if (!a || n.data.sizing) continue;
      n.data.sizing = { ...a, optimise: !!a.optimise };
      this.editor.updateNodeDataFromId(n.id, n.data);
    }
    delete inv.data.assets;
    this.editor.updateNodeDataFromId(inv.id, inv.data);
  },

  /* is this component's size variable (its own size fields are then ignored by a lifetime optimisation)? */
  isSized(id) { return this.optimisedAssets().some(o => String(o.node.id) === String(id)); },

  /* the block body lists the optimised assets and their size range; variable-size blocks get 🔍 */
  updateInvest(id = this.investNode()?.id) {
    document.querySelectorAll(".drawflow-node").forEach(n => n.classList.remove("is-sized"));
    for (const o of this.optimisedAssets()) document.getElementById("node-" + o.node.id)?.classList.add("is-sized");
    if (!id) return;
    const d = this.editor.getNodeFromId(id).data;
    const range = (lo, hi, unit) => `${fmt(lo || 0)}–${hi === undefined || hi === null || hi === "" ? "∞" : fmt(hi)} ${unit}`;
    const lines = this.optimisedAssets().map(({ node, asset: a, missing }) => `<div class="${missing.length ? "inv-missing" : ""}">${
      esc(node.data.name)}: ${node.data.type === "Storage" ? range(a.e_size_min, a.e_size_max, "MWh") : range(a.capacity_min, a.capacity_max, "MW")}${
      missing.length ? " ⚠" : ""}</div>`);
    const el = document.querySelector(`#node-${id} .invest-list`);
    const cap = d.co2_cap === null || d.co2_cap === undefined || d.co2_cap === "" ? "" : `<div>CO₂ cap ${fmt(Number(d.co2_cap))} t</div>`;
    const set = v => v !== null && v !== undefined && v !== "";
    const goals = [set(d.min_self_sufficiency) ? `self-sufficiency ≥ ${fmt(Number(d.min_self_sufficiency))} %` : "",
      set(d.min_irr) ? `IRR ≥ ${fmt(Number(d.min_irr))} %` : "",
      set(d.max_payback) ? `${d.payback_kind === "discounted" ? "disc. " : ""}payback ≤ ${fmt(Number(d.max_payback))} yr` : ""]
      .filter(Boolean).join(" · ");
    const s = App.settings(), mode = s.run_mode;
    const run = `<div><b>${{ single: "Single period", multi_year: "Multi-year", optimise: "Lifetime optimisation" }[mode]}</b></div>
      <div>${s.base_year ? esc(s.base_year) : `${esc(s.start)}, ${s.days} days`} · ${esc(s.freq)}${
      mode !== "single" ? ` · ${s.years} years` : ""}${s.compute === "cloud" ? " · cloud" : ""}</div>`;
    if (el) el.innerHTML = run + cap + (mode !== "optimise" ? (mode === "multi_year" ? `<div class="hint">discount ${
      Number(d.discount_rate || 0)} %${Number(d.inflation) ? ` · inflation ${Number(d.inflation)} %` : ""}</div>` : "") :
      (lines.join("") || `<div class="hint">no size optimised</div>`) +
      (lines.length ? `<div class="hint">discount ${fmt(Number(d.discount_rate || 0))} %${
        Number(d.inflation) ? ` · inflation ${fmt(Number(d.inflation))} %` : ""}</div>` : "") +
      (lines.length && goals ? `<div class="hint">${goals}</div>` : "") +
      (d.baseline ? `<div class="hint">baseline: ${esc(d.baseline)}</div>` : ""));
  },

  /* one series pulled through a DB connection; fetched for the project horizon when validating or running */
  addDb(x, y, spec = {}) {
    const outs = [{ name: "value", type: "data", label: "" }];
    const conns = this.dbConnections();
    const data = { kind: "db", connection: conns.length ? String(conns[0].id) : "", source: "market", zone: "DK_1",
      series: "da_price", connection_type: "", transform: "none", scale: "", ...spec, ins: [], outs };
    const id = this.editor.addNode("db", 0, 1, x, y, "data-node db-node", data,
      this.html(this.dbTitle(data), "DB", dbIcon(data.source), 1, `<div class="data-src"></div>`));
    this.decorate(id);
    this.updateDb(id);
    return id;
  },

  dbTitle(d) {
    if (d.source === "solar") return `PV ${d.scenario || "?"}`;
    if (d.source === "degradation") return `SoH ${d.profile || "?"}`;
    return d.source === "tariff" ? `${d.connection_type || "?"} ${d.series === "feedin" ? "feed-in" : "consumption"}` : (d.series || "?");
  },

  /* refresh title and subtitle after the connector's settings changed */
  updateDb(id) {
    const d = this.editor.getNodeFromId(id).data;
    this.setTitle(id, this.dbTitle(d));
    const src = document.querySelector(`#node-${id} .data-src`);
    if (src) src.textContent = d.source === "solar" ? "BESS IQ · capacity factor" : d.source === "degradation" ?
      "BESS IQ · SoH curves" : `BESS IQ · ${d.zone}${d.transform && d.transform !== "none" ? " · " + d.transform.replace(/_/g, " ") : ""}`;
  },

  addComponent(x, y, type, preset = null) {
    const spec = this.compSpec(type);
    const params = {};
    for (const f of spec.fields) if (f.default !== undefined && f.default !== null && f.kind !== "port_map") params[f.name] = f.default;
    Object.assign(params, preset?.params || {});
    let conv = null;
    if (spec.dynamic) {
      const tpl = preset?.converter || { inputs: [{ kind: null, coef: 1 }], outputs: [{ kind: null, coef: 1 }], capacity: "in1" };
      conv = { inputs: tpl.inputs.map((it, i) => ({ key: "in" + (i + 1), coef: it.coef, kind: it.kind })),
               outputs: tpl.outputs.map((it, i) => ({ key: "out" + (i + 1), coef: it.coef, kind: it.kind })),
               capacity: tpl.capacity || "in1" };
    }
    const { ins, outs } = this.componentConnectors(spec, conv);
    const name = uniqueName(preset ? preset.id : (SHORT_NAMES[type] || type.toLowerCase()));
    const data = { kind: "component", type, name, params, converter: conv, ins, outs, preset: preset?.id || null };
    const icon = ICONS[preset?.icon || spec.icon] || "▫️";
    const id = this.editor.addNode(type, ins.length, outs.length, x, y, "comp-node", data,
      this.html(name, preset ? preset.label : spec.label, icon, Math.max(ins.length, outs.length)));
    this.decorate(id);
    return id;
  },

  /* rebuild a converter's connectors after inputs/outputs were added or removed */
  rebuildConverter(id) {
    const node = this.editor.getNodeFromId(id);
    const exported = this.editor.export();
    const spec = this.compSpec(node.data.type);
    const { ins, outs } = this.componentConnectors(spec, node.data.converter);
    // remember wires by connector name, then recreate the node with the same id-independent wiring
    const wires = edgesOf(exported).filter(e => e.from[0] === String(id) || e.to[0] === String(id));
    const pos = [node.pos_x, node.pos_y];
    const data = { ...node.data, ins, outs };
    this.editor.removeNodeId("node-" + id);
    const nid = this.editor.addNode(node.name, ins.length, outs.length, pos[0], pos[1], "comp-node", data,
      this.html(data.name, data.preset ? data.preset : spec.label, ICONS[spec.icon], Math.max(ins.length, outs.length)));
    this.decorate(nid);
    for (const w of wires) {
      const from = w.from[0] === String(id) ? [String(nid), w.from[1]] : w.from;
      const to = w.to[0] === String(id) ? [String(nid), w.to[1]] : w.to;
      connectByName(from, to, true);
    }
    return nid;
  },

  /* the tooltip text of a block (see BLOCK_HINTS) */
  hint(d) {
    if (d.kind === "component")
      return BLOCK_HINTS[`preset:${d.preset}`] || BLOCK_HINTS[d.type] || this.compSpec(d.type)?.doc.replace(/``/g, "") || "";
    if (d.kind === "db") return BLOCK_HINTS[`db:${d.source}`] || BLOCK_HINTS.db;
    return BLOCK_HINTS[d.kind] || "";
  },

  decorate(id) {
    const node = this.editor.getNodeFromId(id);
    const el = document.getElementById("node-" + id);
    if (!el || !node) return;
    el.title = this.hint(node.data);
    const note = node.data.kind === "note" && el.querySelector("textarea");
    if (note && node.data.size) [note.style.width, note.style.height] = node.data.size.map(v => v + "px");
    const title = this.titleOf(node.data);    // projects saved before renames were kept show the old title
    if (title && el.querySelector(".blk-title")?.textContent !== title) this.setTitle(id, title);
    node.data.ins.forEach((c, i) => styleConn(el.querySelector(`.inputs .input_${i + 1}`), c, node.data));
    node.data.outs.forEach((c, i) => styleConn(el.querySelector(`.outputs .output_${i + 1}`), c, node.data));
  },

  /* the title a block shows: its name (a data block its column, a DB input its series) */
  titleOf(d) {
    if (d.kind === "invest") return "Run settings";
    if (d.kind === "note") return "Note";
    if (d.kind === "db") return this.dbTitle(d);
    if (d.kind === "data") return d.column || d.outs.map(o => o.name).join(", ");
    return d.name;
  },

  /* also in the block's stored markup, so a saved project reopens with the new title (not the name it was made with) */
  setTitle(id, title) {
    const el = document.querySelector(`#node-${id} .blk-title`);
    if (el) el.textContent = title;
    const stored = this.editor.drawflow.drawflow[this.editor.module]?.data[id];
    if (stored?.html) stored.html = stored.html.replace(/(<span class="blk-title">)[^<]*(<\/span>)/, `$1${esc(title)}$2`);
    if (this.editor.getNodeFromId(id)?.data.kind === "component") this.updateInvest();   // it lists components by name
  },
};

function styleConn(el, c, data) {
  if (!el) return;
  el.dataset.label = c.label;
  el.className = el.className.split(" ").filter(x => !x.startsWith("k-") && x !== "conn-data").join(" ");
  if (c.type === "data") el.classList.add("conn-data");
  else el.classList.add("k-" + (c.kind || data.carrier_kind || "any"));
  const FLOW = { in: "inflow", out: "outflow", both: "inflow + outflow" };
  el.title = c.type === "data" ? `parameter ${c.label} (wire a data column)` :
    c.type === "bus" ? "port: wire any component port of this carrier here" :
    `${c.name}${c.flow ? " · " + FLOW[c.flow] : ""}${c.kind ? " · " + c.kind : ""} (wire to a bus)`;
}

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch])); }

function sparkline(vals) {
  if (!vals || !vals.length) return "";
  const v = vals.filter(x => x !== null);
  const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  const pts = vals.map((x, i) => `${(i / (vals.length - 1 || 1)) * 70},${14 - ((x ?? lo) - lo) / span * 13}`).join(" ");
  return `<svg class="spark" viewBox="0 0 70 14"><polyline fill="none" stroke="#6b7280" stroke-width="1" points="${pts}"/></svg>`;
}

function allNodes() { return Object.values(Blocks.editor.export().drawflow.Home.data); }

function uniqueName(base) {
  const names = new Set(allNodes().map(n => n.data.name).filter(Boolean));
  if (!names.has(base)) return base;
  let i = 2;
  while (names.has(`${base}${i}`)) i++;
  return `${base}${i}`;
}

/* list of {from:[nodeId, connectorName], to:[nodeId, connectorName]} from a Drawflow export */
function edgesOf(exported) {
  const nodes = exported.drawflow.Home.data;
  const out = [];
  for (const n of Object.values(nodes)) {
    Object.entries(n.outputs || {}).forEach(([oc, o]) => {
      const oi = parseInt(oc.split("_")[1]) - 1;
      for (const c of o.connections) {
        const target = nodes[c.node];
        const ii = parseInt(c.output.split("_")[1]) - 1;
        out.push({ from: [String(n.id), n.data.outs[oi]?.name], to: [String(c.node), target?.data.ins[ii]?.name] });
      }
    });
  }
  return out;
}

function connectorIndex(nodeData, side, name) {
  const list = side === "in" ? nodeData.ins : nodeData.outs;
  return list.findIndex(c => c.name === name);
}

function connectByName(from, to, silent = false) {
  const a = Blocks.editor.getNodeFromId(from[0]), b = Blocks.editor.getNodeFromId(to[0]);
  if (!a || !b) return;
  const oi = connectorIndex(a.data, "out", from[1]), ii = connectorIndex(b.data, "in", to[1]);
  if (oi < 0 || ii < 0) return;
  Rules.silent = silent;
  Blocks.editor.addConnection(a.id, b.id, `output_${oi + 1}`, `input_${ii + 1}`);
  Rules.silent = false;
}

const dbIcon = source => ({ solar: "☀️", degradation: "📉" })[source] || "🗄";
/* a block whose output is a battery's SoH curves (not a time series) */
const isCurves = d => d.kind === "db" && d.source === "degradation";

/* ------------------------------------------------------------ wiring rules */
const Rules = {
  silent: false,
  check(info) {
    const ed = Blocks.editor;
    const out = ed.getNodeFromId(info.output_id), inn = ed.getNodeFromId(info.input_id);
    const oc = out.data.outs[parseInt(info.output_class.split("_")[1]) - 1];
    const ic = inn.data.ins[parseInt(info.input_class.split("_")[1]) - 1];
    if (!oc || !ic) return "unknown connector";
    if (out.id === inn.id) return "cannot wire a block to itself";
    if (oc.type === "energy") {
      if (inn.data.kind !== "bus") return "energy ports connect to a bus";
      if (oc.kind && oc.kind !== inn.data.carrier_kind) return `${oc.label} needs a ${oc.kind} bus, '${inn.data.name}' is ${inn.data.carrier_kind}`;
      if (out.outputs[info.output_class].connections.length > 1) return `'${oc.label}' already has a wire`;
    } else if (oc.type === "data") {
      if (ic.type !== "data") return "data columns connect to grey parameter connectors only";
      const curves = isCurves(out.data);
      if (curves && ic.name !== "degradation") return "degradation curves connect to a battery's 'SoH curves' only";
      if (!curves && ic.name === "degradation") return "a battery's SoH curves take a 📉 Degradation curve block";
    }
    if (ic.type !== "bus" && inn.inputs[info.input_class].connections.length > 1) return `'${ic.label}' already has a wire`;
    return null;
  },

  onCreated(info) {
    const err = Rules.check(info);
    const ed = Blocks.editor;
    if (err) {
      ed.removeSingleConnection(info.output_id, info.input_id, info.output_class, info.input_class);
      toast(err, true);
      return;
    }
    const out = ed.getNodeFromId(info.output_id);
    const oc = out.data.outs[parseInt(info.output_class.split("_")[1]) - 1];
    // style data wires
    const sel = `.connection.node_in_node-${info.input_id}.node_out_node-${info.output_id}.${info.output_class}.${info.input_class}`;
    if (oc.type === "data") document.querySelector(sel)?.classList.add("c-data");
  },
};

/* Projects saved before energy ports lost their direction: buses had a 'supply' input and a 'withdraw' output, and
   component inflow ports were inputs. Rebuilds every block's connectors in the current layout and rewires by name
   (bus withdraw -> component port becomes component port -> bus port; split grid / storage ports become one link). */
function migrateDrawflow(df) {
  const data = df?.drawflow?.Home?.data;
  if (!data || !Object.values(data).some(n => n.data?.kind === "bus" && n.data.outs?.length)) return df;
  const edges = edgesOf(df);
  for (const n of Object.values(data)) {
    const d = n.data;
    if (d.kind === "bus") Object.assign(d, Blocks.busConnectors(d.carrier_kind));
    else if (d.kind === "component") Object.assign(d, Blocks.componentConnectors(Blocks.compSpec(d.type), d.converter));
    else continue;
    n.inputs = Object.fromEntries(d.ins.map((_, i) => [`input_${i + 1}`, { connections: [] }]));
    n.outputs = Object.fromEntries(d.outs.map((_, i) => [`output_${i + 1}`, { connections: [] }]));
  }
  for (const n of Object.values(data))   // data blocks keep their outputs, wires are re-added below
    if (n.data.kind !== "bus" && n.data.kind !== "component") for (const o of Object.values(n.outputs || {})) o.connections = [];
  const seen = new Set();
  for (let { from, to } of edges) {
    const a = data[from[0]], b = data[to[0]];
    if (!a || !b) continue;
    if (a.data.kind === "bus") [from, to] = [to, from];               // withdraw -> component: now component -> bus
    const src = data[from[0]], dst = data[to[0]];
    let port = from[1];
    if (dst.data.kind === "bus") {
      const link = src.data.outs.find(c => c.ports?.includes(port));
      port = link ? link.name : port;
      to = [to[0], "port"];
    }
    const oi = connectorIndex(src.data, "out", port), ii = connectorIndex(dst.data, "in", to[1]);
    const key = `${from[0]}:${oi}>${to[0]}:${ii}`;
    if (oi < 0 || ii < 0 || seen.has(key)) continue;
    seen.add(key);
    src.outputs[`output_${oi + 1}`].connections.push({ node: String(to[0]), output: `input_${ii + 1}` });
    dst.inputs[`input_${ii + 1}`].connections.push({ node: String(from[0]), input: `output_${oi + 1}` });
  }
  return df;
}
