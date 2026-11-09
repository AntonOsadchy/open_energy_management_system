/* Properties panel: forms generated from the catalog field specs. */
const Panel = {
  el: null,
  nodeId: null,
  messages: { errors: {}, warnings: {} },

  show(id) {
    RunSettings.park();
    this.nodeId = id;
    const node = Blocks.editor.getNodeFromId(id);
    if (!node) return this.clear();
    const d = node.data;
    let html = "";
    if (d.kind === "bus") html = this.busForm(d);
    else if (d.kind === "data") html = this.dataForm(d);
    else if (d.kind === "db") { this.dbForm(id, d); return; }
    else if (d.kind === "dbconn") { this.dbConnForm(id, d); return; }
    else if (d.kind === "invest") { this.investForm(id, d); return; }
    else if (d.kind === "note") html = `<h2>Note</h2><div class="doc">Your notes on the analysis: type into the block on the
      canvas. Saved with the project; runs ignore it. Move it by its header; drag the corner of the text box to resize it.</div>`;
    else if (d.kind === "profile") { this.profileForm(id, d); return; }
    else html = this.componentForm(id, d);
    html += this.messageHtml(id);
    this.el.innerHTML = html;
    this.bind(id);
  },

  clear() { RunSettings.park(); this.nodeId = null; this.el.innerHTML = '<div class="empty">Select a block to edit its properties.</div>' + this.messageHtml("_global"); },

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
    if (Blocks.catalog.sizing[d.type]) html += this.sizingForm(id, d);
    const groups = {};
    for (const f of spec.fields) (groups[f.group] ||= []).push(f);
    for (const [g, fields] of Object.entries(groups)) {
      let body = "";
      for (const f of fields) body += this.fieldHtml(f, d.params[f.name], wired[f.name], d);
      html += `<fieldset><legend>${esc(g)}</legend>${body}</fieldset>`;
    }
    return html;
  },

  /* PV and storages: fixed size (their own size fields) or variable size (chosen by the lifetime optimisation
     within min/max, which then needs CAPEX and OPEX) */
  sizingForm(id, d) {
    const a = d.sizing || {}, variable = !!a.optimise, st = d.type === "Storage";
    const mode = (v, label) => `<label><input type="radio" name="size-mode" data-size-mode="${v}" ${
      (v === "variable") === variable ? "checked" : ""}> ${label}</label>`;
    let html = `<fieldset><legend>Size</legend><div class="seg">${mode("fixed", "Fixed size")}${mode("variable", "Variable size")}</div>`;
    if (!variable) return html + `<div class="hint">${st ? "Energy and power are" : "The capacity is"} set below. Variable
      size lets a lifetime optimisation choose ${st ? "them" : "it"}.</div></fieldset>`;
    const missing = Blocks.optimisedAssets().find(o => String(o.node.id) === String(id))?.missing || [];
    const body = Blocks.catalog.sizing[d.type].filter(f => f.name !== "optimise_size").map(f => {
      const cost = f.name.startsWith("capex") || f.name === "opex";
      const empty = [undefined, null, ""].includes(a[f.name]);
      return `<div class="field"><label class="${cost ? "req" : ""}" title="${esc(f.help)}">${esc(f.label)}</label><span>
        <input type="number" step="any" min="0" data-sz="${f.name}" value="${empty ? "" : a[f.name]}"
          class="${cost && empty ? "invalid" : ""}" placeholder="${cost ? "required" : f.name.endsWith("_min") ? "0" : f.name === "duration" ? "free" : "no limit"}">
        <span class="unit">${esc(f.unit || "")}</span></span></div>`;
    }).join("");
    return html + `<div class="hint">▶ Run → <b>Lifetime optimisation</b> chooses ${st ? "energy and power" : "the capacity"}
      within these limits. Min sizes are what exists already: the investment is the size above them. CAPEX is paid in
      year 0, OPEX every year. Without a max size a profitable asset may grow without limit. Discount rate and hurdles:
      ⚙️ Run settings block.</div>${body}${missing.length ? `<div class="msg err">Missing ${missing.map(esc).join(", ")}</div>` : ""}</fieldset>`;
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
    if (f.name === "soh" && d.type === "Storage" && wiredParams(this.nodeId).degradation)
      return `<div class="field">${label}<span class="wired" title="Remove the SoH curves wire to use a fixed SoH">not used: SoH curves are wired</span></div>`;
    if ((SIZE_FIELDS[d.type] || []).includes(f.name) && Blocks.isSized(this.nodeId))
      return `<div class="field">${label}<span class="wired" title="Variable size: the lifetime optimisation chooses this
        size (switch to Fixed size to set it here)">🔍 variable size</span></div>`;
    let input;
    switch (f.kind) {
      case "scalar": case "series":
        input = `<span><input type="number" step="any" data-p="${f.name}" value="${val ?? ""}" placeholder="${f.unlimited ? "no limit" : f.optional ? "off" : ""}">${unit}</span>`; break;
      case "enum":
        input = `<select data-p="${f.name}">${f.choices.map(c => `<option ${c === val ? "selected" : ""}>${c}</option>`).join("")}</select>`; break;
      case "curves":
        input = `<span class="hint">not connected - the fixed SoH below, or 100 % (wire a 📉 Degradation curve for SoH by age and cycling)</span>`; break;
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
        d.ins[0].kind = d.carrier_kind;
        const div = document.getElementById("node-" + id);
        div.className = div.className.replace(/bus-\w+/g, "") + " bus-" + d.carrier_kind;
        Blocks.decorate(id);
      }
      if (k === "carrier") document.querySelector(`#node-${id} .bus-carrier`).textContent = d.carrier;
      save(d);
      if (k === "carrier_kind") this.show(id);
    }));
    // size mode (PV, storages)
    el.querySelectorAll("[data-size-mode]").forEach(r => r.addEventListener("change", () => {
      const d = node().data;
      (d.sizing ||= {}).optimise = r.dataset.sizeMode === "variable";
      save(d); App.dirty(); this.show(id);
    }));
    el.querySelectorAll("[data-sz]").forEach(inp => inp.addEventListener("change", () => {
      const d = node().data;
      d.sizing[inp.dataset.sz] = inp.value === "" ? null : parseFloat(inp.value);
      save(d); App.dirty(); this.show(id);
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

/* ------------------------------------------------------------ DB connection + DB input forms */
Object.assign(Panel, {
  dbConnStatus: {},   // node id -> last /api/db/connect answer (databases list, error)

  /* Run settings block: every setting of a run (#run-settings, see RunSettings: mode, period, resolution, time zone,
     compute), the CO₂ cap, the discount rate and inflation, and the lifetime optimisation's targets (the sizes to optimise
     are switched to variable size on the PV and storage blocks themselves). ▶ Run then only starts the run. */
  investForm(id, d) {
    RunSettings.park();
    const rows = Blocks.optimisedAssets().map(({ node, missing }) => `<div class="${missing.length ? "inv-missing" : ""}">
      <a href="#" data-goto="${node.id}">${esc(node.data.name)}</a>${missing.length ? ` - missing ${missing.map(esc).join(", ")}` : ""}</div>`).join("");
    this.el.innerHTML = `<h2>⚙️ Run settings</h2><div class="doc">Everything a run uses, set before ▶ Run (which only starts
      it). Not wired, one per project; saved with the project.</div>
      <fieldset class="rs-opt"><legend>Lifetime optimisation</legend><div class="doc">Switch a PV or storage block to
      <b>Variable size</b> in its own settings (with its size limits, CAPEX and OPEX). The run maximises the net present
      value of the investment over the lifetime and reports NPV, IRR and payback.</div>
      ${rows ? `<div class="hint">Variable size:</div>${rows}` : `<div class="hint">No PV or storage has a variable size.</div>`}
      ${row("Baseline", `<select data-k="baseline"><option value="">this model without the new assets</option>${
        ProjectTree.family(App.settings().name).map(m => `<option ${m.name === d.baseline ? "selected" : ""}>${esc(m.name)}</option>`).join("")}${
        d.baseline && !ProjectTree.family(App.settings().name).some(m => m.name === d.baseline) ?
          `<option selected>${esc(d.baseline)}</option>` : ""}</select>`)}
      <div class="hint">The case the investment is measured against (savings, NPV, IRR, payback): this model with every
      variable size at its minimum, or another saved variant of the project (e.g. the site as it is), run with these
      settings.${Account.user ? "" : " Log in to choose a variant."}</div>
      </fieldset>
      <div id="rs-slot"></div>
      <fieldset class="rs-econ"><legend>Economics</legend>
      ${row("Discount rate", `<span><input type="number" step="0.1" min="0" data-k="discount_rate" value="${d.discount_rate ?? ""}"><span class="unit">%/yr</span></span>`)}
      ${row("Inflation", `<span><input type="number" step="0.1" data-k="inflation" value="${d.inflation ?? ""}" placeholder="0"><span class="unit">%/yr</span></span>`)}
      <div class="hint">For the net present value of a multi-year run or a lifetime optimisation. Energy prices, tariffs
      and OPEX rise by the inflation every year (year 1 at the base period's prices; CAPEX is paid up front). Cash flows,
      NPV and IRR are then nominal, so use a nominal discount rate.</div></fieldset>
      <fieldset><legend>Targets</legend>
      ${row("CO₂ cap", `<span><input type="number" step="any" min="0" data-k="co2_cap" value="${d.co2_cap ?? ""}" placeholder="no cap"><span class="unit">t</span></span>`)}
      <div class="hint">Limit on the total CO₂ of the run, for every run mode (the CO₂ price is an input of the grid block).</div>
      <div class="rs-opt"><div class="doc">Optional targets the investment must meet. The optimiser then
      picks the sizes with the highest NPV among those that meet them.<br><b>Self-sufficiency</b>: the share of the site's
      electricity demand not bought from the grid (1 − import / demand), supplied by on-site generation directly or via
      storage; exported power does not count. Reaching it may cost NPV: the Sizing tab shows the result.<br><b>IRR /
      payback</b> hurdles usually make the investment smaller; if none meets them it invests nothing. With a hurdle the
      reference case is solved first, then the sizes (one after the other).</div>
      ${row("Min self-sufficiency", `<span><input type="number" step="1" min="0" max="100" data-k="min_self_sufficiency" value="${d.min_self_sufficiency ?? ""}" placeholder="none"><span class="unit">%</span></span>`)}
      ${row("Minimum IRR", `<span><input type="number" step="0.1" data-k="min_irr" value="${d.min_irr ?? ""}" placeholder="none"><span class="unit">%</span></span>`)}
      ${row("Maximum payback", `<span><input type="number" step="0.1" min="0" data-k="max_payback" value="${d.max_payback ?? ""}" placeholder="none"><span class="unit">years</span></span>`)}
      ${row("Payback", `<select data-k="payback_kind">${["simple", "discounted"].map(k =>
        `<option ${k === (d.payback_kind || "simple") ? "selected" : ""}>${k}</option>`).join("")}</select>`)}
      </div></fieldset>` + this.messageHtml(id);
    this.el.querySelector("#rs-slot").replaceWith(document.getElementById("run-settings"));
    RunSettings.refresh();
    const save = mutate => {
      const nd = Blocks.editor.getNodeFromId(id).data;
      mutate(nd);
      Blocks.editor.updateNodeDataFromId(id, nd);
      App.dirty();
      this.investForm(id, nd);
    };
    for (const k of ["discount_rate", "inflation"])
      this.el.querySelector(`[data-k="${k}"]`).addEventListener("change", e =>
        save(nd => { nd[k] = e.target.value === "" ? 0 : parseFloat(e.target.value); }));
    this.el.querySelector('[data-k="co2_cap"]').addEventListener("change", e =>
      save(nd => { nd.co2_cap = e.target.value === "" ? null : parseFloat(e.target.value); }));
    for (const k of ["min_irr", "max_payback", "min_self_sufficiency"])
      this.el.querySelector(`[data-k="${k}"]`).addEventListener("change", e =>
        save(nd => { nd[k] = e.target.value === "" ? null : parseFloat(e.target.value); }));
    this.el.querySelector('[data-k="payback_kind"]').addEventListener("change", e =>
      save(nd => { nd.payback_kind = e.target.value; }));
    this.el.querySelector('[data-k="baseline"]').addEventListener("change", e =>
      save(nd => { nd.baseline = e.target.value || null; }));
    this.el.querySelectorAll("[data-goto]").forEach(a => a.addEventListener("click", e => {
      e.preventDefault(); this.show(a.dataset.goto);
    }));
  },

  /* daily profile: one day of values drawn on the chart or typed in the table, repeated on every day of the run */
  profileForm(id, d) {
    const v = d.values, n = v.length, per = n / 24, stepMin = 1440 / n;
    const time = i => `${String(Math.floor(i * stepMin / 60)).padStart(2, "0")}:${String(i * stepMin % 60).padStart(2, "0")}`;
    const rows = Array.from({ length: 24 }, (_, h) => `<tr><th>${time(h * per)}</th>${Array.from({ length: per }, (_, j) =>
      `<td><input type="number" step="any" data-i="${h * per + j}" value="${v[h * per + j]}" title="${time(h * per + j)}"></td>`).join("")}</tr>`).join("");
    this.el.innerHTML = `<h2>🕒 Daily profile</h2><div class="doc">One day of values from midnight, repeated on every day
      of the run by local time of day (whatever period you run). Wire it to one or more grey parameter connectors, like a
      data column. Drag across the chart to draw the profile, or type the values below.</div>
      ${row("Name", `<input type="text" data-k="name" value="${esc(d.name)}">`)}
      ${row("Unit", `<input type="text" data-k="unit" value="${esc(d.unit || "")}" placeholder="e.g. MW, EUR/MWh">`)}
      ${row("Values per day", `<select data-k="steps">${[[24, "24 (hourly)"], [48, "48 (30 min)"], [96, "96 (15 min)"]].map(([k, l]) =>
        `<option value="${k}" ${k === n ? "selected" : ""}>${l}</option>`).join("")}</select>`)}
      <svg class="prof-chart" viewBox="0 0 ${PROF_W} ${PROF_H + 14}"></svg>
      <div class="prof-tools">
        <span><button class="small" data-act="fill">Set all to</button><input type="number" step="any" data-tool="fill" value="0"></span>
        <span><button class="small" data-act="scale">Scale ×</button><input type="number" step="any" data-tool="scale" value="1"></span>
      </div>
      <details class="prof-paste"><summary>Paste values</summary>
        <textarea data-tool="paste" rows="3" placeholder="24, 48 or 96 numbers (one per line, or separated by commas, tabs or spaces), e.g. a column copied from Excel"></textarea>
        <button class="small" data-act="paste">Apply</button></details>
      <table class="prof-table">${rows}</table>` + this.messageHtml(id);
    const save = (values, rerender = true) => {
      const nd = Blocks.editor.getNodeFromId(id).data;
      nd.values = values.map(x => (Number.isFinite(x) ? x : 0));
      Blocks.editor.updateNodeDataFromId(id, nd);
      Blocks.updateProfile(id);
      App.dirty();
      if (rerender) this.profileForm(id, nd);
    };
    const cur = () => Blocks.editor.getNodeFromId(id).data.values;    // the table edits values without a re-render
    this.drawProfile(id, v, save);
    this.el.querySelectorAll('[data-k="name"], [data-k="unit"]').forEach(inp => inp.addEventListener("change", () => {
      const nd = Blocks.editor.getNodeFromId(id).data;
      nd[inp.dataset.k] = inp.value;
      Blocks.editor.updateNodeDataFromId(id, nd);
      Blocks.updateProfile(id);
      App.dirty();
    }));
    this.el.querySelector('[data-k="steps"]').addEventListener("change", e => save(resampleDay(cur(), parseInt(e.target.value))));
    this.el.querySelectorAll("[data-i]").forEach(inp => inp.addEventListener("change", () => {
      const nv = [...cur()];
      nv[+inp.dataset.i] = inp.value === "" ? 0 : parseFloat(inp.value);
      save(nv, false);
      this.drawProfile(id, nv, save);
    }));
    const tool = k => parseFloat(this.el.querySelector(`[data-tool="${k}"]`).value);
    this.el.querySelector('[data-act="fill"]').addEventListener("click", () => save(cur().map(() => tool("fill") || 0)));
    this.el.querySelector('[data-act="scale"]').addEventListener("click", () => { const k = tool("scale"); if (Number.isFinite(k)) save(cur().map(x => x * k)); });
    this.el.querySelector('[data-act="paste"]').addEventListener("click", () => {
      const nums = this.el.querySelector('[data-tool="paste"]').value.trim().split(/[\s,;]+/).filter(Boolean).map(t => parseFloat(t));
      if (![24, 48, 96].includes(nums.length) || nums.some(x => !Number.isFinite(x)))
        return toast(`Paste 24, 48 or 96 numbers (got ${nums.length}${nums.some(x => !Number.isFinite(x)) ? ", some not numbers" : ""})`, true);
      save(nums);
    });
  },

  /* bars of the profile; dragging across the chart sets the values under the pointer */
  drawProfile(id, values, save) {
    const svg = this.el.querySelector(".prof-chart");
    if (!svg) return;
    const n = values.length, bw = PROF_W / n;
    const lo = Math.min(0, ...values);
    const hi = Math.max(...values) > 0 ? niceCeil(Math.max(...values) * 1.25) : 1;
    const y = val => PROF_H - (val - lo) / (hi - lo) * PROF_H;
    const render = vals => {
      svg.innerHTML = `<line x1="0" x2="${PROF_W}" y1="${y(0)}" y2="${y(0)}" class="prof-axis"/>` +
        vals.map((val, i) => `<rect x="${i * bw + 0.5}" width="${Math.max(bw - 1, 0.5)}" y="${Math.min(y(val), y(0))}"
          height="${Math.abs(y(0) - y(val))}" class="prof-bar"/>`).join("") +
        [0, 6, 12, 18].map(h => `<text x="${h / 24 * PROF_W + 1}" y="${PROF_H + 12}" class="prof-tick">${String(h).padStart(2, "0")}h</text>`).join("") +
        `<text x="${PROF_W - 2}" y="9" class="prof-tick" text-anchor="end">${fmt(hi)}</text>`;
    };
    render(values);
    let drawing = null;
    const at = e => {
      const r = svg.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width * PROF_W, py = (e.clientY - r.top) / r.height * (PROF_H + 14);
      const i = Math.min(n - 1, Math.max(0, Math.floor(px / bw)));
      const val = lo + (PROF_H - Math.min(PROF_H, Math.max(0, py))) / PROF_H * (hi - lo);
      return [i, Number(val.toPrecision(3))];
    };
    svg.onpointerdown = e => {
      drawing = { values: [...values], last: null };
      svg.setPointerCapture(e.pointerId);
      svg.onpointermove(e);
    };
    svg.onpointermove = e => {
      if (!drawing) return;
      const [i, val] = at(e);
      const from = drawing.last ?? i;      // fill the bars skipped by a fast drag
      for (let k = Math.min(from, i); k <= Math.max(from, i); k++) drawing.values[k] = val;
      drawing.last = i;
      render(drawing.values);
    };
    svg.onpointerup = () => {
      if (!drawing) return;
      const nv = drawing.values;
      drawing = null;
      save(nv);
    };
  },

  dbConnForm(id, d) {
    const st = this.dbConnStatus[id];
    const dbs = st?.databases?.includes(d.database) ? st.databases : [...new Set([d.database, ...(st?.databases || [])])];
    const ssl = ["require", "prefer", "disable"].map(m => `<option ${m === d.sslmode ? "selected" : ""}>${m}</option>`).join("");
    this.el.innerHTML = `<h2>DB connection</h2><div class="doc">Login for a BESS IQ (Postgres / Supabase) database, read-only.
      The password is stored on this computer for this login, never in the project file.</div>
      ${row("Name", `<input type="text" data-c="name" value="${esc(d.name)}">`)}
      ${row("Host", `<input type="text" data-c="host" value="${esc(d.host)}" placeholder="aws-0-eu-central-1.pooler.supabase.com">`)}
      ${row("Port", `<input type="number" data-c="port" value="${esc(d.port)}">`)}
      ${row("User", `<input type="text" data-c="user" value="${esc(d.user)}" placeholder="postgres.&lt;project ref&gt;">`)}
      ${row("Password", `<input type="password" id="dbc-pw" autocomplete="new-password" placeholder="${st?.has_password ? "•••••• stored" : "required"}">`)}
      ${row("SSL", `<select data-c="sslmode">${ssl}</select>`)}
      <button id="dbc-connect" class="primary">Connect</button>
      ${st ? `<div class="msg ${st.connected ? "ok" : "err"}">${st.connected ? `Connected · ${st.databases.length} database(s)` : esc(st.error)}</div>` : ""}
      ${row("Database", `<select data-c="database" ${st?.connected ? "" : "disabled"}>${dbs.map(x => `<option ${x === d.database ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>`)}
      <div class="hint">${st?.connected ? "Choose the database to pull from; DB input blocks then list its zones and series." : "Connect to list the databases on this server."}</div>`
      + this.messageHtml(id);
    const autoConnect = this.dbAutoConnect === id;
    this.dbAutoConnect = null;
    this.el.querySelectorAll("[data-c]").forEach(inp => inp.addEventListener("change", () => {
      const nd = Blocks.editor.getNodeFromId(id).data;
      nd[inp.dataset.c] = inp.type === "number" ? parseInt(inp.value) || 5432 : inp.value.trim();
      if (inp.dataset.c !== "name" && inp.dataset.c !== "database") delete this.dbConnStatus[id];   // login changed
      Blocks.editor.updateNodeDataFromId(id, nd);
      Blocks.updateDbConn(id);
      App.dirty();
      this.dbConnForm(id, nd);
    }));
    document.getElementById("dbc-connect").onclick = async () => {
      const nd = Blocks.editor.getNodeFromId(id).data;
      const btn = document.getElementById("dbc-connect");
      btn.disabled = true; btn.textContent = "Connecting…";
      const r = await postJSON("/api/db/connect", { conn: App.dbConn(nd), password: document.getElementById("dbc-pw").value });
      this.dbConnStatus[id] = r;
      App.dbOptionsCache = {};
      if (r.connected && !r.databases.includes(nd.database) && r.databases.length) {
        nd.database = r.databases.includes("postgres") ? "postgres" : r.databases[0];
        Blocks.editor.updateNodeDataFromId(id, nd);
        Blocks.updateDbConn(id);
      }
      if (this.nodeId === id) this.dbConnForm(id, nd);
    };
    if (autoConnect) document.getElementById("dbc-connect").click();
  },

  async dbForm(id, d) {
    const conns = Blocks.dbConnections();
    const def = !conns.length && App.dbDefault;   // no connection block: the .env login is used
    const connSel = `<select data-db="connection"><option value="">${def ? esc(`default · ${def.database} @ ${def.host.split(".")[0]} (.env)`) : "choose…"}</option>${conns.map(n =>
      `<option value="${n.id}" ${String(n.id) === String(d.connection) ? "selected" : ""}>${esc(n.data.name)} · ${esc(n.data.database)}</option>`).join("")}</select>`;
    const head = `<h2>DB input</h2><div class="doc">One series pulled through a DB connection, fetched for the project's
      start, days and resolution when you validate or run (cached afterwards). Wire it to a grey parameter connector.</div>
      ${row("Connection", connSel)}`;
    const connNode = conns.find(n => String(n.id) === String(d.connection)) || (conns.length === 1 ? conns[0] : null);
    const connData = connNode ? connNode.data : def || null;
    const bindConn = () => this.el.querySelector('[data-db="connection"]').addEventListener("change", e => {
      const nd = Blocks.editor.getNodeFromId(id).data;
      nd.connection = e.target.value;
      Blocks.editor.updateNodeDataFromId(id, nd);
      App.dirty();
      this.dbForm(id, nd);
    });
    if (!connData) {
      this.el.innerHTML = head + `<div class="msg warn">${conns.length ? "Choose a connection." :
        "No database login: add a 🔐 DB connection block, or put SUPABASE_DB_URL in the project's .env."}</div>` + this.messageHtml(id);
      return bindConn();
    }
    this.el.innerHTML = head + `<div class="hint">Loading zones and series…</div>`;
    let o;
    try { o = await App.dbOptions(connData); } catch (e) {
      if (this.nodeId !== id) return;
      this.el.innerHTML = head + `<div class="msg err">${esc(e.message)}</div>
        <div class="hint">Select the DB connection block and press Connect.</div>` + this.messageHtml(id);
      return bindConn();
    }
    if (this.nodeId !== id) return;   // another block was selected meanwhile
    const opt = (list, cur) => list.map(([v, t]) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(t)}</option>`).join("");
    const zones = opt(o.zones.map(z => [z.code, `${z.code} · ${z.name}`]), d.zone);
    const groups = {};
    for (const s of o.series) (groups[s.category] ||= []).push(s);
    const series = Object.entries(groups).map(([g, list]) => `<optgroup label="${esc(g)}">${
      opt(list.map(s => [s.code, `${s.code} (${s.unit})`]), d.series)}</optgroup>`).join("");
    const info = o.series.find(s => s.code === d.series);
    let html = head + row("Source", `<select data-db="source">${opt(Object.entries(o.sources), d.source)}</select>`);
    if (d.source !== "solar" && d.source !== "degradation") html += row("Zone", `<select data-db="zone">${zones}</select>`);
    if (d.source === "degradation") {
      const ps = o.degradation_profiles || [];
      const p = ps.find(x => x.code === d.profile);
      html += row("Profile", `<select data-db="profile"><option value="">choose…</option>${
        opt(ps.map(x => [x.code, `${x.name} (${x.code})`]), d.profile)}</select>`);
      if (p) html += `<div class="hint">${p.rates.length > 1 ? `Curves for ${p.rates.map(r => r + "").join(" / ")} cycles/day` :
        `One curve (${p.rates[0]} cycles/day)`}, up to ${p.years} years.</div>`;
      else if (!ps.length) html += `<div class="msg warn">This database has no battery degradation curves (bess_degradation_curves).</div>`;
      html += `<div class="hint">Wire it to a battery's <b>SoH curves</b> and set <b>Years</b> in the top bar above 1: each year
        the battery's capacity is read from the curves at its age and average cycles/day so far (interpolated between the
        curves); below the curves' end-of-life SoH it retires. Not used by a 1-year run.</div>
        <button id="db-fetch">Fetch &amp; preview</button><div id="db-preview"></div>`;
      this.el.innerHTML = html + this.messageHtml(id);
      bindConn();
      this.el.querySelectorAll("[data-db]:not([data-db=connection])").forEach(inp => inp.addEventListener("change", () => {
        const nd = Blocks.editor.getNodeFromId(id).data;
        nd[inp.dataset.db] = inp.value;
        if (inp.dataset.db === "source") this.dbSourceChanged(id, nd, o);
        Blocks.editor.updateNodeDataFromId(id, nd);
        Blocks.updateDb(id);
        App.dirty();
        this.dbForm(id, nd);
      }));
      document.getElementById("db-fetch").onclick = () => this.dbPreview(id, connData);
      return;
    }
    if (d.source === "solar") {
      const scs = o.solar_scenarios || [];
      const sc = scs.find(x => x.code === d.scenario);
      html += row("PV system", `<select data-db="scenario"><option value="">choose…</option>${
        opt(scs.map(x => [x.code, `${x.name} · ${x.zone}`]), d.scenario)}</select>`);
      if (sc) html += `<div class="hint">${esc(sc.capacity_mwp)} MWp, tilt ${esc(sc.slope_deg)}°, azimuth ${esc(sc.azimuth_deg)}°${
        sc.tracking ? ", " + esc(sc.tracking) + " tracking" : ", fixed"}${sc.bifacial ? ", bifacial" : ""} at ${esc(sc.lat)}, ${esc(sc.lon)}.
        ${esc(sc.description || "")}<br>Output is divided by the installed MWp, so the values are a 0…1 capacity factor:
        wire it to a PV block's <b>cf</b> and set the PV's capacity (MW) to size it.</div>`;
      else if (!scs.length) html += `<div class="msg warn">This database has no modelled PV systems (solar_pv_scenarios).</div>`;
    } else if (d.source === "tariff") {
      html += row("Grid connection", `<select data-db="connection_type"><option value="">choose…</option>${
        opt(o.connection_types.map(c => [c.code, c.name]), d.connection_type)}</select>`);
      html += row("Tariff", `<select data-db="series">${opt([["consumption", "consumption (import) €/MWh"], ["feedin", "feed-in (export) €/MWh"]], d.series)}</select>`);
    } else {
      html += row("Series", `<select data-db="series">${series}</select>`);
      if (info) html += `<div class="hint">${esc(info.description)} · ${esc(info.unit)}</div>`;
    }
    html += `<fieldset><legend>Transform</legend>
      ${row("Transform", `<select data-db="transform">${opt(Object.entries(o.transforms), d.transform || "none")}</select>`)}
      ${row("Scale ×", `<input type="number" step="any" data-db="scale" value="${esc(d.scale ?? "")}" placeholder="1">`)}
      <div class="hint">E.g. normalise national solar output to a 0…1 capacity factor, or K → °C for temperature.</div></fieldset>
      <button id="db-fetch">Fetch &amp; preview</button><div id="db-preview"></div>`;
    this.el.innerHTML = html + this.messageHtml(id);
    bindConn();
    this.el.querySelectorAll("[data-db]:not([data-db=connection])").forEach(inp => inp.addEventListener("change", () => {
      const nd = Blocks.editor.getNodeFromId(id).data;
      const k = inp.dataset.db;
      nd[k] = inp.value;
      if (k === "source") this.dbSourceChanged(id, nd, o);
      if (k === "zone") App.zoneTimezone(o.zones.find(z => z.code === inp.value)?.tz);
      if (k === "series" && inp.value === "temperature_2m_k" && (nd.transform || "none") === "none") nd.transform = "kelvin_to_celsius";
      Blocks.editor.updateNodeDataFromId(id, nd);
      Blocks.updateDb(id);
      App.dirty();
      this.dbForm(id, nd);
    }));
    document.getElementById("db-fetch").onclick = () => this.dbPreview(id, connData);
  },

  dbSourceChanged(id, nd, o) {
    nd.series = { tariff: "consumption", solar: "", degradation: "" }[nd.source] ?? "da_price";
    nd.connection_type = "";
    nd.scenario = nd.source === "solar" ? o.solar_scenarios?.[0]?.code || "" : "";
    nd.profile = nd.source === "degradation" ? o.degradation_profiles?.[0]?.code || "" : "";
    const icon = document.querySelector(`#node-${id} .blk-head .icon`);
    if (icon) icon.textContent = dbIcon(nd.source);
  },

  async dbPreview(id, connData) {
    const box = document.getElementById("db-preview");
    box.innerHTML = `<div class="hint">Fetching…</div>`;
    const d = Blocks.editor.getNodeFromId(id).data;
    try {
      const r = await postJSON("/api/db/preview", { spec: d, conn: App.dbConn(connData), settings: App.settings(), refresh: true });
      if (r.curves) {
        const years = Math.max(...Object.values(r.curves).map(v => v.length));
        const head = Object.keys(r.curves).map(k => `<th>${esc(k)}/day</th>`).join("");
        const body = Array.from({ length: years }, (_, y) => `<tr><td>${y + 1}</td>${Object.values(r.curves).map(v =>
          `<td class="num">${v[y] === undefined ? "" : (100 * v[y]).toFixed(1)}</td>`).join("")}</tr>`).join("");
        box.innerHTML = `<div class="hint">SoH (%) after each year · end of life ${(100 * r.eol).toFixed(1)} %</div>
          ${(r.warnings || []).map(w => `<div class="msg warn">${esc(w)}</div>`).join("")}
          <table class="kpi"><tr><th>year</th>${head}</tr>${body}</table>`;
        return;
      }
      const f = x => (Math.abs(x) >= 100 ? x.toFixed(0) : x.toPrecision(3));
      box.innerHTML = `<div class="data-preview">${sparkline(r.preview)}</div>
        <div class="hint">${r.steps} steps · min ${f(r.min)} · mean ${f(r.mean)} · max ${f(r.max)}<br>
        database resolution ${esc(r.source_resolution || "?")}, ${r.points} points${r.held_steps ? `, ${r.held_steps} steps held from the previous value` : ""}</div>`;
    } catch (e) { box.innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
  },
});

/* size fields of a component that the lifetime optimisation chooses when the block is switched to variable size */
const SIZE_FIELDS = { Producer: ["capacity"], Storage: ["e_max", "p_ch_max", "p_dis_max"] };

function row(label, input) { return `<div class="field"><label>${label}</label>${input}</div>`; }

const PROF_W = 288, PROF_H = 110;

/* a day of values at another number of steps: repeated when finer, averaged when coarser */
function resampleDay(values, m) {
  const n = values.length;
  if (m === n) return [...values];
  if (m > n) return Array.from({ length: m }, (_, i) => values[Math.floor(i * n / m)]);
  const k = n / m;
  return Array.from({ length: m }, (_, i) => values.slice(i * k, (i + 1) * k).reduce((a, b) => a + b, 0) / k);
}

/* 1, 2, 2.5, 5 × 10^k at or above x */
function niceCeil(x) {
  const p = 10 ** Math.floor(Math.log10(x));
  return [1, 2, 2.5, 5, 10].map(f => f * p).find(c => c >= x - 1e-12);
}

/* {param: "dataset:column"} for every data wire into node id */
function wiredParams(id) {
  const exp = Blocks.editor.export();
  const nodes = exp.drawflow.Home.data;
  const out = {};
  for (const e of edgesOf(exp)) {
    if (e.to[0] !== String(id)) continue;
    const src = nodes[e.from[0]];
    if (src?.data.kind === "data") out[e.to[1]] = `${src.data.label}:${e.from[1]}`;
    else if (src?.data.kind === "profile") out[e.to[1]] = `🕒 ${src.data.name} (daily profile)`;
    else if (src?.data.kind === "db") out[e.to[1]] = `DB ${["solar", "degradation"].includes(src.data.source) ? "" :
      src.data.zone + " "}${Blocks.dbTitle(src.data)}`;
  }
  return out;
}
