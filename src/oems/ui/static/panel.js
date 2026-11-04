/* Properties panel: forms generated from the catalog field specs. */
const Panel = {
  el: null,
  nodeId: null,
  messages: { errors: {}, warnings: {} },

  show(id) {
    this.nodeId = id;
    const node = Blocks.editor.getNodeFromId(id);
    if (!node) return this.clear();
    const d = node.data;
    let html = "";
    if (d.kind === "bus") html = this.busForm(d);
    else if (d.kind === "data") html = this.dataForm(d);
    else html = this.componentForm(id, d);
    html += this.messageHtml(id);
    this.el.innerHTML = html;
    this.bind(id);
  },

  clear() { this.nodeId = null; this.el.innerHTML = '<div class="empty">Select a block to edit its properties.</div>' + this.messageHtml("_global"); },

  messageHtml(id) {
    const e = this.messages.errors[id] || [], w = this.messages.warnings[id] || [];
    return e.map(m => `<div class="msg err">${esc(m)}</div>`).join("") + w.map(m => `<div class="msg warn">${esc(m)}</div>`).join("");
  },

  busForm(d) {
    const kinds = Blocks.catalog.carrier_kinds.map(k => `<option ${k === d.carrier_kind ? "selected" : ""}>${k}</option>`).join("");
    return `<h2>Bus</h2><div class="doc">Central node with its own energy balance. All energy wires of a carrier meet here.</div>
      ${row("Name", `<input type="text" data-k="name" value="${esc(d.name)}">`)}
      ${row("Carrier", `<input type="text" data-k="carrier" value="${esc(d.carrier)}">`)}
      ${row("Carrier kind", `<select data-k="carrier_kind">${kinds}</select>`)}
      ${row("CO₂ factor", `<input type="number" step="any" data-k="emission_factor" value="${d.emission_factor ?? ""}" placeholder="t/MWh on grid imports">`)}
      ${row("Allow slack", `<input type="checkbox" data-k="allow_slack" ${d.allow_slack ? "checked" : ""}>`)}`;
  },

  dataForm(d) {
    const preview = App.datasets[d.dataset]?.preview || {};
    return `<h2>Input: ${esc(d.column || d.outs.map(o => o.name).join(", "))}</h2>
      <div class="doc">From ${esc(d.label)} · resolution ${esc(d.resolution || "?")}. Wire it to one or more grey
      parameter connectors. Series at another resolution are aligned automatically.</div>
      ${d.outs.map(o => `<div class="data-preview">${sparkline(preview[o.name])}<span class="hint">${esc(o.name)}</span></div>`).join("")}`;
  },

  componentForm(id, d) {
    const spec = Blocks.compSpec(d.type);
    const wired = wiredParams(id);
    let html = `<h2>${esc(d.preset ? d.preset.replace("_", " ") : spec.label)}</h2><div class="doc">${esc(spec.doc)}</div>`;
    html += row("Name", `<input type="text" data-k="name" value="${esc(d.name)}">`);
    if (spec.dynamic) html += this.converterForm(d, wired);
    const groups = {};
    for (const f of spec.fields) (groups[f.group] ||= []).push(f);
    for (const [g, fields] of Object.entries(groups)) {
      let body = "";
      for (const f of fields) body += this.fieldHtml(f, d.params[f.name], wired[f.name], d);
      html += `<fieldset><legend>${esc(g)}</legend>${body}</fieldset>`;
    }
    return html;
  },

  converterForm(d, wired) {
    const c = d.converter;
    const side = (list, label) => list.map(it => `<div class="row"><b>${it.key}</b>
        ${wired["coef:" + it.key] ? `<span class="wired">← ${esc(wired["coef:" + it.key])}</span>` :
          `<input type="number" step="any" data-conv="${it.key}" value="${it.coef}" title="${label} coefficient">`}
        <label><input type="radio" name="capport" data-cap="${it.key}" ${c.capacity === it.key ? "checked" : ""}> capacity</label>
        <button class="small" data-del="${it.key}">✕</button></div>`).join("");
    return `<fieldset><legend>Conversion</legend>
      <div class="doc">Flows are proportional: each connector carries coefficient × activity. Capacity (p_max) applies to the selected connector.</div>
      <div>Inputs</div>${side(c.inputs, "input")}<button class="small" data-add="in">+ input</button>
      <div style="margin-top:6px">Outputs</div>${side(c.outputs, "output")}<button class="small" data-add="out">+ output</button>
    </fieldset>`;
  },

  fieldHtml(f, val, wiredRef, d) {
    const label = `<label class="${f.required ? "req" : ""}" title="${esc(f.help)}">${esc(f.label)}</label>`;
    const unit = f.unit ? `<span class="unit">${esc(f.unit)}</span>` : "";
    if (wiredRef) return `<div class="field">${label}<span class="wired">← ${esc(wiredRef)}</span></div>`;
    let input;
    switch (f.kind) {
      case "scalar": case "series":
        input = `<span><input type="number" step="any" data-p="${f.name}" value="${val ?? ""}" placeholder="${f.optional ? "off" : ""}">${unit}</span>`; break;
      case "enum":
        input = `<select data-p="${f.name}">${f.choices.map(c => `<option ${c === val ? "selected" : ""}>${c}</option>`).join("")}</select>`; break;
      case "bool":
        input = `<input type="checkbox" data-p="${f.name}" ${val ? "checked" : ""}>`; break;
      case "period":
        input = `<select data-p="${f.name}">${["D", "W", "M", "Q", "Y", ""].map(c => `<option value="${c}" ${c === (val ?? "") ? "selected" : ""}>${c || "horizon"}</option>`).join("")}</select>`; break;
      case "str":
        input = `<input type="text" data-p="${f.name}" value="${esc(val ?? "")}">`; break;
      case "tariffs":
        return this.tariffsHtml(val || []);
      case "port_map":
        return this.portMapHtml(f.name, val || {}, d);
      default:
        input = `<input type="text" data-p="${f.name}" value="${esc(val ?? "")}">`;
    }
    return `<div class="field">${label}${input}</div>`;
  },

  tariffsHtml(list) {
    const types = Blocks.catalog.tariffs;
    const items = list.map((t, i) => {
      const spec = types.find(x => x.type === t.type);
      const fields = spec.fields.map(f => {
        const v = t[f.name] ?? f.default ?? "";
        if (f.kind === "enum") return `<label>${f.name} <select data-t="${i}" data-tf="${f.name}">${f.choices.map(c => `<option ${c === v ? "selected" : ""}>${c}</option>`).join("")}</select></label>`;
        if (f.kind === "period") return `<label>${f.name} <select data-t="${i}" data-tf="${f.name}">${["D", "W", "M", "Q", "Y"].map(c => `<option ${c === v ? "selected" : ""}>${c}</option>`).join("")}</select></label>`;
        if (f.kind === "scalar" || f.kind === "series") return `<label>${f.name} <input type="number" step="any" style="width:70px" data-t="${i}" data-tf="${f.name}" value="${v}"></label>`;
        return "";
      }).join(" ");
      return `<div class="msg" style="background:#f6f7f9"><b>${t.type}</b> <button class="small" data-tdel="${i}">✕</button><br>${fields}</div>`;
    }).join("");
    return `<div>${items}<select id="tariff-type">${types.map(t => `<option>${t.type}</option>`).join("")}</select>
      <button class="small" id="tariff-add">+ tariff</button></div>`;
  },

  portMapHtml(name, map, d) {
    const ports = [...d.ins, ...d.outs].filter(c => c.type === "energy").flatMap(c => c.ports || [c.name]);
    if (name === "bounds" || !ports.length) return "";
    const rows = Object.entries(map).map(([port, v]) => {
      const val = name === "emission_factors" ? (v.co2 ?? "") : (typeof v === "object" ? v.rate : v);
      const cat = name === "flow_costs" && typeof v === "object" ? v.category : "OPERATION";
      const catSel = name === "flow_costs" ? `<select data-pmcat="${name}:${port}">${["OPERATION", "TAX", "NETWORK", "ENERGY"].map(c => `<option ${c === cat ? "selected" : ""}>${c}</option>`).join("")}</select>` : "";
      return `<div class="row">${port} <input type="number" step="any" data-pm="${name}:${port}" value="${val}">${catSel}<button class="small" data-pmdel="${name}:${port}">✕</button></div>`;
    }).join("");
    const unit = name === "emission_factors" ? "t CO₂/MWh" : "€/MWh";
    return `<div class="field"><label>${name.replace("_", " ")}</label><span class="unit">${unit}</span></div>${rows}
      <div class="row"><select data-pmnew="${name}">${ports.map(p => `<option>${p}</option>`).join("")}</select>
      <button class="small" data-pmadd="${name}">+ add</button></div>`;
  },

  /* ------------------------------------------------------------ events */
  bind(id) {
    const ed = Blocks.editor;
    const node = () => ed.getNodeFromId(id);
    const save = (d) => ed.updateNodeDataFromId(id, d);
    const el = this.el;
    el.querySelectorAll("[data-k]").forEach(inp => inp.addEventListener("change", () => {
      const d = node().data;
      const k = inp.dataset.k;
      const old = d[k];
      d[k] = inp.type === "checkbox" ? inp.checked : inp.value;
      if (k === "name") Blocks.setTitle(id, d.name);
      if (k === "carrier_kind") {
        // keep default carrier and bus names in step with the kind (el -> heat), custom names are kept
        const base = kind => (kind === "electricity" ? "el" : kind);
        if (d.carrier === old) d.carrier = d.carrier_kind;
        if (new RegExp(`^${base(old)}\\d*$`).test(d.name)) {
          d.name = uniqueName(base(d.carrier_kind));
          Blocks.setTitle(id, d.name);
        }
        document.querySelector(`#node-${id} .bus-carrier`).textContent = d.carrier;
        const kindIcon = document.querySelector(`#node-${id} .blk-head .icon`);
        if (kindIcon) kindIcon.textContent = KIND_ICON[d.carrier_kind] || "◆";
        d.ins[0].kind = d.outs[0].kind = d.carrier_kind;
        const div = document.getElementById("node-" + id);
        div.className = div.className.replace(/bus-\w+/g, "") + " bus-" + d.carrier_kind;
        Blocks.decorate(id);
      }
      if (k === "carrier") document.querySelector(`#node-${id} .bus-carrier`).textContent = d.carrier;
      save(d);
      if (k === "carrier_kind") this.show(id);
    }));
    el.querySelectorAll("[data-p]").forEach(inp => inp.addEventListener("change", () => {
      const d = node().data;
      const k = inp.dataset.p;
      if (inp.type === "checkbox") d.params[k] = inp.checked;
      else if (inp.type === "number") d.params[k] = inp.value === "" ? null : parseFloat(inp.value);
      else d.params[k] = inp.value === "" ? null : inp.value;
      save(d);
    }));
    // converter
    el.querySelectorAll("[data-conv]").forEach(inp => inp.addEventListener("change", () => {
      const d = node().data;
      for (const it of [...d.converter.inputs, ...d.converter.outputs]) if (it.key === inp.dataset.conv) it.coef = parseFloat(inp.value);
      save(d);
    }));
    el.querySelectorAll("[data-cap]").forEach(inp => inp.addEventListener("change", () => {
      const d = node().data; d.converter.capacity = inp.dataset.cap; save(d);
    }));
    el.querySelectorAll("[data-add]").forEach(b => b.addEventListener("click", () => {
      const d = node().data, side = b.dataset.add === "in" ? "inputs" : "outputs";
      const keys = new Set([...d.converter.inputs, ...d.converter.outputs].map(x => x.key));
      let i = 1; while (keys.has(b.dataset.add + i)) i++;
      d.converter[side].push({ key: b.dataset.add + i, coef: 1, kind: null });
      save(d);
      const nid = Blocks.rebuildConverter(id);
      this.show(nid);
    }));
    el.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () => {
      const d = node().data;
      for (const side of ["inputs", "outputs"]) {
        if (d.converter[side].length <= 1 && d.converter[side].some(x => x.key === b.dataset.del)) return toast("a converter needs at least one input and one output", true);
        d.converter[side] = d.converter[side].filter(x => x.key !== b.dataset.del);
      }
      if (d.converter.capacity === b.dataset.del) d.converter.capacity = d.converter.inputs[0].key;
      save(d);
      this.show(Blocks.rebuildConverter(id));
    }));
    // tariffs
    el.querySelector("#tariff-add")?.addEventListener("click", () => {
      const d = node().data, type = el.querySelector("#tariff-type").value;
      const spec = Blocks.catalog.tariffs.find(t => t.type === type);
      const t = { type };
      for (const f of spec.fields) if (f.default !== undefined && f.default !== null) t[f.name] = f.default;
      (d.params.tariffs ||= []).push(t); save(d); this.show(id);
    });
    el.querySelectorAll("[data-tdel]").forEach(b => b.addEventListener("click", () => {
      const d = node().data; d.params.tariffs.splice(+b.dataset.tdel, 1); save(d); this.show(id);
    }));
    el.querySelectorAll("[data-tf]").forEach(inp => inp.addEventListener("change", () => {
      const d = node().data, t = d.params.tariffs[+inp.dataset.t];
      t[inp.dataset.tf] = inp.type === "number" ? (inp.value === "" ? null : parseFloat(inp.value)) : inp.value;
      save(d);
    }));
    // port maps (flow costs, emission factors)
    el.querySelectorAll("[data-pmadd]").forEach(b => b.addEventListener("click", () => {
      const d = node().data, name = b.dataset.pmadd;
      const port = el.querySelector(`[data-pmnew="${name}"]`).value;
      d.params[name] ||= {};
      d.params[name][port] = name === "emission_factors" ? { co2: 0 } : { rate: 0, category: "OPERATION" };
      save(d); this.show(id);
    }));
    el.querySelectorAll("[data-pm]").forEach(inp => inp.addEventListener("change", () => {
      const d = node().data, [name, port] = inp.dataset.pm.split(":");
      const v = parseFloat(inp.value);
      if (name === "emission_factors") d.params[name][port] = { co2: v };
      else d.params[name][port] = { ...(d.params[name][port] || {}), rate: v };
      save(d);
    }));
    el.querySelectorAll("[data-pmcat]").forEach(inp => inp.addEventListener("change", () => {
      const d = node().data, [name, port] = inp.dataset.pmcat.split(":");
      d.params[name][port] = { ...(d.params[name][port] || {}), category: inp.value }; save(d);
    }));
    el.querySelectorAll("[data-pmdel]").forEach(b => b.addEventListener("click", () => {
      const d = node().data, [name, port] = b.dataset.pmdel.split(":");
      delete d.params[name][port]; save(d); this.show(id);
    }));
  },
};

function row(label, input) { return `<div class="field"><label>${label}</label>${input}</div>`; }

/* {param: "dataset:column"} for every data wire into node id */
function wiredParams(id) {
  const exp = Blocks.editor.export();
  const nodes = exp.drawflow.Home.data;
  const out = {};
  for (const e of edgesOf(exp)) {
    if (e.to[0] !== String(id)) continue;
    const src = nodes[e.from[0]];
    if (src?.data.kind === "data") out[e.to[1]] = `${src.data.label}:${e.from[1]}`;
  }
  return out;
}
