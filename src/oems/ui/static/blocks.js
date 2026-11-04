/* Block creation, connector metadata and wiring rules for the Drawflow canvas.
 *
 * Every node stores its connector meaning in node.data:
 *   data.ins[i]  describes Drawflow connector input_{i+1}
 *   data.outs[i] describes output_{i+1}
 * with {name, type: "energy" | "data" | "bus", label, kind (carrier kind or null)}.
 */
const ICONS = { grid: "🔌", sun: "☀️", plug: "🏭", sink: "🌬️", shift: "⏱️", battery: "🔋", convert: "🔁",
  thermometer: "🌡️", hp: "♨️", boiler: "🔥", chiller: "❄️", flame: "🔥", chp: "⚙️", link: "🔗", tank: "🛢️", box: "▫️" };
const SHORT_NAMES = { GridConnection: "grid", Producer: "prod", Demand: "demand", FlexibleDemand: "flex", Sink: "sink",
  Storage: "store", Converter: "conv", ThermalZone: "zone" };
const KIND_ICON = { electricity: "⚡", heat: "🔥", cold: "❄️", fuel: "🛢️", other: "◆" };

const Blocks = {
  catalog: null,
  editor: null,

  compSpec(type) { return this.catalog.components.find(c => c.type === type); },

  /* connectors of a component from its catalog entry (and converter definition) */
  componentConnectors(spec, conv) {
    const ins = [], outs = [];
    if (spec.dynamic) {
      for (const it of conv.inputs) ins.push({ name: it.key, type: "energy", label: it.key, kind: it.kind || null });
      for (const it of conv.outputs) {
        outs.push({ name: it.key, type: "energy", label: it.key, kind: it.kind || null });
      }
      for (const it of conv.outputs) ins.push({ name: "coef:" + it.key, type: "data", label: "coef " + it.key });
    } else {
      const link = spec.link;  // one connector for both directions (grid import/export, storage charge/discharge)
      if (link) (link.direction === "in" ? ins : outs).push({ name: link.connector, type: "energy", label: link.label, kind: null, ports: link.ports });
      for (const e of spec.energy) {
        if (link?.ports.includes(e.port)) continue;
        const c = { name: e.port, type: "energy", label: e.port, kind: e.carrier_kind || null };
        (e.direction === "in" ? ins : outs).push(c);
      }
      for (const f of spec.data_connectors) ins.push({ name: f, type: "data", label: f });
    }
    return { ins, outs };
  },

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
      emission_factor: opts.emission_factor ?? "", allow_slack: !!opts.allow_slack,
      ins: [{ name: "supply", type: "bus", label: "supply", kind }],
      outs: [{ name: "withdraw", type: "bus", label: "withdraw", kind }] };
    const id = this.editor.addNode("bus", 1, 1, x, y, `bus-node bus-${kind}`, data,
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

  decorate(id) {
    const node = this.editor.getNodeFromId(id);
    const el = document.getElementById("node-" + id);
    if (!el || !node) return;
    node.data.ins.forEach((c, i) => styleConn(el.querySelector(`.inputs .input_${i + 1}`), c, node.data));
    node.data.outs.forEach((c, i) => styleConn(el.querySelector(`.outputs .output_${i + 1}`), c, node.data));
  },

  setTitle(id, title) {
    const el = document.querySelector(`#node-${id} .blk-title`);
    if (el) el.textContent = title;
  },
};

function styleConn(el, c, data) {
  if (!el) return;
  el.dataset.label = c.label;
  el.className = el.className.split(" ").filter(x => !x.startsWith("k-") && x !== "conn-data").join(" ");
  if (c.type === "data") el.classList.add("conn-data");
  else el.classList.add("k-" + (c.kind || data.carrier_kind || "any"));
  el.title = c.type === "data" ? `parameter ${c.label} (wire a data column)` : `${c.label}${c.kind ? " · " + c.kind : ""}`;
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
      if (inn.data.kind !== "bus") return "energy outputs must go to a bus 'supply' connector";
      if (oc.kind && oc.kind !== inn.data.carrier_kind) return `${oc.label} needs a ${oc.kind} bus, '${inn.data.name}' is ${inn.data.carrier_kind}`;
      if (out.outputs[info.output_class].connections.length > 1) return `'${oc.label}' already has a wire`;
    } else if (oc.type === "bus") {
      if (ic.type !== "energy") return "a bus 'withdraw' connector feeds component energy inputs only";
      if (ic.kind && ic.kind !== out.data.carrier_kind) return `${ic.label} needs a ${ic.kind} bus, '${out.data.name}' is ${out.data.carrier_kind}`;
    } else if (oc.type === "data") {
      if (ic.type !== "data") return "data columns connect to grey parameter connectors only";
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
    const out = ed.getNodeFromId(info.output_id), inn = ed.getNodeFromId(info.input_id);
    const oc = out.data.outs[parseInt(info.output_class.split("_")[1]) - 1];
    // style data wires
    const sel = `.connection.node_in_node-${info.input_id}.node_out_node-${info.output_id}.${info.output_class}.${info.input_class}`;
    if (oc.type === "data") document.querySelector(sel)?.classList.add("c-data");
    if (Rules.silent) return;
    // auto-wire the partner port of bidirectional components (grid import/export, storage charge/discharge)
    const comp = oc.type === "energy" ? out : (oc.type === "bus" ? inn : null);
    const bus = oc.type === "energy" ? inn : (oc.type === "bus" ? out : null);
    if (!comp || comp.data.kind !== "component") return;
    const spec = Blocks.compSpec(comp.data.type);
    const port = oc.type === "energy" ? oc.name : inn.data.ins[parseInt(info.input_class.split("_")[1]) - 1].name;
    const partner = spec.partners?.[port];
    if (!partner) return;
    const exp = ed.export();
    const already = edgesOf(exp).some(e => (e.from[0] === String(comp.id) && e.from[1] === partner) ||
                                          (e.to[0] === String(comp.id) && e.to[1] === partner));
    if (already) return;
    const pIsIn = connectorIndex(comp.data, "in", partner) >= 0;
    if (pIsIn) connectByName([String(bus.id), "withdraw"], [String(comp.id), partner], true);
    else connectByName([String(comp.id), partner], [String(bus.id), "supply"], true);
  },
};
