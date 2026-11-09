/* Compare two variants of the open project: both models side by side (read-only, the same zoom and position), blocks
   that differ highlighted (changed / only in A / only in B), and the fields of a block with their A and B values, the
   changed ones highlighted. Blocks are matched by kind and name (a data block by its column, the Run settings block
   is one per project); the open project is compared as it is on the canvas (unsaved edits included). Drag a canvas
   to move it, scroll to zoom (each on its own; both together while "move together" is ticked). Clicking a block also shows its Properties (the
   panel moves into this window while it is open): the open variant's blocks are edited in place and the comparison
   follows the edits. */
const Compare = {
  eds: {},            // side ("a" | "b") -> read-only Drawflow editor
  diff: null,         // key -> {a: node | undefined, b: node | undefined, fields: [{field, a, b, changed}], changed}
  selected: null,     // key of the block shown below
  work: {},           // variant name -> its edited, unsaved project, while another variant is on the canvas
  timer: null,

  dlg: () => document.getElementById("cmp-dialog"),

  async open() {
    await Account.ensure();
    await ProjectTree.refresh();
    const current = App.settings().name;
    const names = [current, ...ProjectTree.family(current).map(m => m.name)].filter(Boolean);
    if (names.length < 2) return toast("Compare needs two saved variants of the open project: save one with ⑂ first", true);
    const meta = ProjectTree.list.find(m => m.name === current);
    const other = names.find(n => n === meta?.parent) || names.find(n => n !== current);
    for (const [side, pick] of [["a", current], ["b", other]])
      document.getElementById(`cmp-${side}`).innerHTML = names.map(n => `<option ${n === pick ? "selected" : ""}>${esc(n)}</option>`).join("");
    if (!this.hooked) {                   // edits of the open variant (Properties, wires) update the comparison
      for (const ev of ["nodeDataChanged", "nodeCreated", "nodeRemoved", "connectionCreated", "connectionRemoved"])
        Blocks.editor.on(ev, () => this.changed());
      this.hooked = true;
    }
    if (!this.dlg().open) {
      document.getElementById("cmp-props").append(Panel.el);   // the Properties panel, for editing while comparing
      Panel.clear();
      this.dlg().showModal();
    }
    this.selected = null;
    this.work = {};
    await this.render();
  },

  /* back in the side pane when the window closes */
  closed() {
    document.getElementById("side").append(Panel.el);
    Panel.clear();
  },

  /* the open variant was edited: compare again (keeping the view and the chosen block) */
  changed() {
    if (!this.dlg().open) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.render(true).catch(err => toast(err.message, true)), 250);
  },

  /* a variant's project: the open one from the canvas, others as saved */
  async project(name) {
    if (name === App.settings().name) return structuredClone(App.project());
    if (this.work[name]) return structuredClone(this.work[name]);
    const meta = ProjectTree.list.find(m => m.name === name);
    return api(`/api/projects/${encodeURIComponent(meta.id)}`);
  },

  async render(keep = false) {
    const [na, nb] = ["a", "b"].map(s => document.getElementById(`cmp-${s}`).value);
    const [pa, pb] = await Promise.all([this.project(na), this.project(nb)]);
    for (const [side, p] of [["a", pa], ["b", pb]]) p.drawflow = migrateDrawflow(structuredClone(p.drawflow));
    const view = keep && Object.fromEntries(Object.entries(this.eds).map(([s, ed]) => [s, [ed.canvas_x, ed.canvas_y, ed.zoom]]));
    this.pa = pa; this.pb = pb;
    this.diff = this.compare(pa, pb);
    for (const [side, p] of [["a", pa], ["b", pb]]) this.draw(side, p);
    if (view) for (const [s, v] of Object.entries(view)) this.view(s, ...v, false);
    else this.fit();
    this.mark();
    this.summary(na, nb);
    if (keep && this.selected && this.diff[this.selected]) { this.select(this.selected, false); this.markFields(); }
    else { this.selected = null; this.detail(null); }
    this.editable();
  },

  /* ------------------------------------------------------------- the difference */
  nodes: p => Object.values(p.drawflow?.drawflow?.Home?.data || {}),

  key(d) {
    if (d.kind === "invest") return "invest";
    if (d.kind === "data") return `data:${d.column || d.outs?.map(o => o.name).join(",")}`;
    if (d.kind === "db") return `db:${Blocks.dbTitle(d)}`;
    return `${d.kind}:${d.type || ""}:${d.name || ""}`;
  },

  /* a node's fields as "path" -> value (nested objects flattened, lists as one value); connectors and markup left out */
  fields(d, prefix = "", out = {}) {
    for (const [k, v] of Object.entries(d || {})) {
      if (!prefix && ["ins", "outs", "kind", "size"].includes(k)) continue;
      const path = prefix ? `${prefix}.${k}` : k;
      if (v && typeof v === "object" && !Array.isArray(v)) this.fields(v, path, out);
      else if (v !== null && v !== undefined && v !== "") out[path] = v;
    }
    return out;
  },

  /* each block's wires, by the names of the blocks at both ends: "port → block.port" */
  wires(p) {
    const byId = Object.fromEntries(this.nodes(p).map(n => [String(n.id), n]));
    const out = {};
    for (const e of edgesOf(p.drawflow)) {
      const a = byId[e.from[0]], b = byId[e.to[0]];
      if (!a || !b) continue;
      const port = (n, conn, side) => n.data[side]?.[parseInt(conn.split("_")[1]) - 1]?.name || conn;
      const ka = this.key(a.data), kb = this.key(b.data);
      const pa = port(a, e.from[1], "outs"), pb = port(b, e.to[1], "ins");
      (out[ka] ??= new Set()).add(`${pa} → ${Blocks.titleOf(b.data)}.${pb}`);
      (out[kb] ??= new Set()).add(`${pb} ← ${Blocks.titleOf(a.data)}.${pa}`);
    }
    return out;
  },

  /* the run settings of a project (they belong to its Run settings block) */
  settings(p) {
    const s = p.settings || {}, keep = ["run_mode", "base_year", "start", "days", "years", "freq", "tz", "lifetime_model",
      "time_limit", "compute"];
    return Object.fromEntries(keep.filter(k => s[k] !== undefined && s[k] !== null && s[k] !== "")
      .filter(k => !(s.base_year && (k === "start" || k === "days")) && !(s.run_mode === "single" && k === "years"))
      .map(k => [`run.${k}`, s[k]]));
  },

  compare(pa, pb) {
    const out = {}, wa = this.wires(pa), wb = this.wires(pb);
    for (const [side, p] of [["a", pa], ["b", pb]])
      for (const n of this.nodes(p)) {
        const k = this.key(n.data);
        if (!(out[k] ??= {})[side]) out[k][side] = n;
      }
    const sa = this.settings(pa), sb = this.settings(pb);
    (out.invest ??= {}).settings = true;
    for (const [k, e] of Object.entries(out)) {
      const fa = e.a ? this.fields(e.a.data) : {}, fb = e.b ? this.fields(e.b.data) : {};
      if (k === "invest") { Object.assign(fa, sa); Object.assign(fb, sb); }
      const ja = [...(wa[k] || [])].sort().join("\n"), jb = [...(wb[k] || [])].sort().join("\n");
      if (ja || jb) { fa.wires = ja; fb.wires = jb; }
      e.fields = [...new Set([...Object.keys(fa), ...Object.keys(fb)])].sort((x, y) =>
        (x === "name") ? -1 : (y === "name") ? 1 : x.localeCompare(y)).map(f => ({ field: f, a: fa[f], b: fb[f],
          changed: JSON.stringify(fa[f] ?? null) !== JSON.stringify(fb[f] ?? null) }));
      e.fields.sort((x, y) => (x.field === "name") - (y.field === "name") ? (y.field === "name") - (x.field === "name") :
        y.changed - x.changed);                    // the name, then the changed fields first
      e.changed = e.fields.some(f => f.changed);
      if (k === "invest" && !e.a && !e.b && !e.changed) delete out[k];
    }
    return out;
  },

  state: e => !e.a ? "only-b" : !e.b ? "only-a" : e.changed ? "changed" : "same",

  /* ------------------------------------------------------------- drawing */
  draw(side, p) {
    const el = document.getElementById(`cmp-canvas-${side}`);
    el.innerHTML = "";
    const ed = new Drawflow(el);
    ed.reroute = false;
    ed.start();
    ed.editor_mode = "view";              // nothing can be moved or wired here; pan and zoom are ours (bindView)
    ed.zoom_enter = () => {};
    ed.import(p.drawflow);
    for (const n of this.nodes(p))      // data wires as on the canvas
      if (["data", "db", "profile"].includes(n.data.kind))
        el.querySelectorAll(`.connection.node_out_node-${n.id}`).forEach(c => c.classList.add("c-data"));
    el.querySelectorAll(".note-node textarea").forEach(t => (t.readOnly = true));
    this.eds[side] = ed;
  },

  /* both canvases at the same zoom and position, fitting the blocks of both */
  fit() {
    const all = [...this.nodes(this.pa), ...this.nodes(this.pb)];
    const el = document.getElementById("cmp-canvas-a");
    if (!all.length) return;
    const x0 = Math.min(...all.map(n => n.pos_x)) - 30, y0 = Math.min(...all.map(n => n.pos_y)) - 30;
    const x1 = Math.max(...all.map(n => n.pos_x)) + 300, y1 = Math.max(...all.map(n => n.pos_y)) + 160;
    const zoom = Math.min(1, el.clientWidth / (x1 - x0), el.clientHeight / (y1 - y0));
    for (const side of Object.keys(this.eds)) this.view(side, -x0 * zoom, -y0 * zoom, zoom, false);
  },

  linked: () => document.getElementById("cmp-link").checked,

  /* move / zoom one canvas (and the other one too while linked) */
  view(side, x, y, zoom, follow = true) {
    for (const s of follow && this.linked() ? ["a", "b"] : [side]) {
      const ed = this.eds[s];
      if (!ed) continue;
      ed.zoom = ed.zoom_last_value = zoom;
      ed.canvas_x = x; ed.canvas_y = y;
      ed.precanvas.style.transform = `translate(${x}px, ${y}px) scale(${zoom})`;
    }
  },

  /* drag the background to move a canvas, scroll (or the buttons) to zoom around the pointer */
  bindView(side) {
    const el = document.getElementById(`cmp-canvas-${side}`);
    let drag = null;
    el.addEventListener("mousedown", e => {
      if (e.button !== 0 || e.target.closest(".drawflow-node") || !this.eds[side]) return;
      const ed = this.eds[side];
      drag = { x: e.clientX, y: e.clientY, cx: ed.canvas_x, cy: ed.canvas_y };
      el.classList.add("panning");
      e.preventDefault(); e.stopPropagation();
    }, true);
    window.addEventListener("mousemove", e => {
      if (drag) this.view(side, drag.cx + e.clientX - drag.x, drag.cy + e.clientY - drag.y, this.eds[side].zoom);
    });
    window.addEventListener("mouseup", () => { drag = null; el.classList.remove("panning"); });
    el.addEventListener("wheel", e => {
      if (!this.eds[side]) return;
      e.preventDefault();
      const r = el.getBoundingClientRect();
      this.zoomAt(side, Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
  },

  zoomAt(side, factor, mx, my) {
    const ed = this.eds[side], z = Math.min(2, Math.max(0.15, ed.zoom * factor));
    this.view(side, mx - (mx - ed.canvas_x) * z / ed.zoom, my - (my - ed.canvas_y) * z / ed.zoom, z);
  },

  /* colour every block by its difference, with the number of changed fields */
  mark() {
    for (const [k, e] of Object.entries(this.diff))
      for (const side of ["a", "b"]) {
        const n = e[side], el = n && document.querySelector(`#cmp-canvas-${side} #node-${n.id}`);
        if (!el) continue;
        const st = this.state(e), count = e.fields.filter(f => f.changed).length;
        el.classList.add(`cmp-${st}`);
        el.dataset.cmpKey = k;
        if (st === "changed") el.insertAdjacentHTML("beforeend", `<span class="cmp-badge" title="${count} field${
          count > 1 ? "s" : ""} differ">${count}</span>`);
      }
  },

  summary(na, nb) {
    const es = Object.values(this.diff), n = st => es.filter(e => this.state(e) === st).length;
    const parts = [[n("changed"), "changed", "changed"], [n("only-a"), `only in ${na}`, "only-a"], [n("only-b"), `only in ${nb}`, "only-b"]];
    document.getElementById("cmp-summary").innerHTML = es.every(e => this.state(e) === "same") ?
      `<span class="hint">No differences.</span>` :
      parts.filter(([c]) => c).map(([c, label, st]) => `<span class="cmp-chip cmp-${st}">${c} ${esc(label)}</span>`).join("");
  },

  /* ------------------------------------------------------------- one block */
  /* a block chosen (``side``: the view it was clicked in), with its Properties unless ``props`` is false */
  select(key, props = true, side = null) {
    this.selected = key;
    for (const el of document.querySelectorAll("#cmp-dialog .drawflow-node"))
      el.classList.toggle("cmp-on", el.dataset.cmpKey === key);
    this.detail(key);
    if (props) this.props(key, side).catch(err => toast(err.message, true));
  },

  /* which side is the open variant (editable), if either */
  liveSide() {
    const current = App.settings().name;
    return ["a", "b"].find(s => document.getElementById(`cmp-${s}`).value === current) || null;
  },

  /* the Properties of the chosen block in the view it was clicked in: the open variant's block is edited in place;
     a block of the other view opens that variant first (offering to save the open one's changes) */
  async props(key, side = null) {
    const e = this.diff[key], live = this.liveSide();
    side ||= live || (e?.a ? "a" : "b");
    if (side !== live && e?.[side]) return this.switchTo(document.getElementById(`cmp-${side}`).value, side);
    if (e?.[side]) { Panel.show(e[side].id); return this.markFields(); }
    if (key === "invest" && live) {      // run settings differ, the open variant has no block for them yet
      Panel.show(Blocks.addInvest(40, 40));
      return App.dirty();
    }
    const other = ["a", "b"].find(s => e?.[s]), name = other && document.getElementById(`cmp-${other}`).value;
    Panel.clear();
    Panel.el.innerHTML = `<div class="empty">${live ? `This block is not in the open variant
      <b>${esc(App.settings().name)}</b>.` : "Neither variant is open."}${name ? `<p><button data-open="${esc(name)}"
      data-side="${other}">Edit it in '${esc(name)}'</button></p>` : ""}</div>`;
  },

  /* the changed fields of the chosen block highlighted in its Properties too */
  markFields() {
    const e = this.diff[this.selected];
    Panel.el.querySelectorAll(".cmp-field-diff").forEach(x => x.classList.remove("cmp-field-diff"));
    for (const f of e?.fields.filter(x => x.changed) || []) {
      const name = f.field.split(".").pop(), at = `[data-p="${name}"], [data-k="${name}"], [data-sz="${name}"]`;
      for (const inp of Panel.el.querySelectorAll(at)) (inp.closest(".field, .row, label") || inp).classList.add("cmp-field-diff");
    }
  },

  /* the header of the Properties pane: which variant its edits go to */
  editable() {
    const side = this.liveSide();
    for (const s of ["a", "b"]) document.getElementById(`cmp-canvas-${s}`).classList.toggle("cmp-live", s === side);
    document.getElementById("cmp-props-head").innerHTML = side ?
      `Properties · <b>${esc(App.settings().name)}</b> <span class="hint">(${side.toUpperCase()}, edited in place)</span>` :
      `Properties <span class="hint">(click a block to edit its variant)</span>`;
  },

  /* open the variant of view ``side`` on the canvas to edit it here, and show the chosen block's Properties there.
     The variant left keeps its unsaved changes (in ``work``) until the comparison is closed. */
  async switchTo(name, side) {
    const meta = ProjectTree.list.find(m => m.name === name), current = App.settings().name;
    if (!meta) return;
    if (App.edits !== App.savedEdits && current) this.work[current] = App.project();
    const kept = this.work[name];
    delete this.work[name];
    App.load(kept ? structuredClone(kept) : await api(`/api/projects/${encodeURIComponent(meta.id)}`), true);
    if (kept) App.dirty(false);           // its changes are still unsaved
    const key = this.selected;
    await this.render(true);
    if (key && this.diff[key]) this.select(key, true, side);
  },

  /* the variants changed in this comparison and not saved (the open one first) */
  pending() {
    const live = App.settings().name;
    return [...(live && App.edits !== App.savedEdits ? [live] : []), ...Object.keys(this.work)];
  },

  /* leaving the comparison: save or discard the changes made in it (or keep comparing) */
  async leave() {
    const names = this.pending();
    if (!names.length) return this.dlg().close();
    const dlg = document.getElementById("cmp-leave");
    document.getElementById("cmp-leave-list").innerHTML = names.map(n => `<li>${esc(n)}</li>`).join("");
    const choice = await new Promise(resolve => {
      const done = v => { dlg.close(); resolve(v); };
      dlg.querySelectorAll("[data-choice]").forEach(b => (b.onclick = () => done(b.dataset.choice)));
      dlg.oncancel = e => { e.preventDefault(); done("stay"); };
      dlg.showModal();
    });
    if (choice === "stay") return;
    const live = App.settings().name;
    if (choice === "save") {
      for (const [name, p] of Object.entries(this.work))
        await postJSON(`/api/projects/${encodeURIComponent(name)}`, p);
      if (names.includes(live)) await App.save();
    } else if (names.includes(live)) {    // discard: the open variant as saved
      const meta = ProjectTree.list.find(m => m.name === live);
      if (meta) App.load(await api(`/api/projects/${encodeURIComponent(meta.id)}`), true);
    }
    this.work = {};
    await ProjectTree.refresh();
    this.dlg().close();
    toast(choice === "save" ? `Saved ${names.map(n => `'${n}'`).join(", ")}` : "Changes discarded");
  },

  detail(key) {
    const el = document.getElementById("cmp-detail"), e = key && this.diff[key];
    if (!e) {
      const changed = Object.entries(this.diff).filter(([, x]) => this.state(x) !== "same");
      el.innerHTML = changed.length ? `<div class="hint">Click a block to see its fields. Differences:</div><ul class="cmp-list">${
        changed.map(([k, x]) => `<li><a href="#" data-key="${esc(k)}" class="cmp-${this.state(x)}">${esc(this.title(x))}</a></li>`).join("")}</ul>` : "";
      return;
    }
    const [na, nb] = ["a", "b"].map(s => document.getElementById(`cmp-${s}`).value);
    const show = v => v === undefined ? `<span class="hint">–</span>` :
      `<span class="cmp-val">${esc(typeof v === "object" ? JSON.stringify(v) : String(v))}</span>`;
    const st = this.state(e);
    el.innerHTML = `<h4>${esc(this.title(e))} <span class="cmp-chip cmp-${st}">${{ changed: "changed", same: "no difference",
      "only-a": `only in ${esc(na)}`, "only-b": `only in ${esc(nb)}` }[st]}</span></h4>
      <table class="kpi cmp-table"><tr><th>Field</th><th>${esc(na)}</th><th>${esc(nb)}</th></tr>${
      e.fields.map(f => `<tr class="${f.changed ? "cmp-diff" : ""}"><td>${esc(this.label(e, f.field))}</td><td>${show(f.a)}</td><td>${
        show(f.b)}</td></tr>`).join("")}</table>`;
  },

  title(e) {
    const d = (e.a || e.b)?.data;
    return d ? (Blocks.titleOf(d) || d.kind) : "Run settings";
  },

  /* a field's label from the catalog where there is one */
  label(e, path) {
    const d = (e.a || e.b)?.data, last = path.split(".").pop();
    const f = d?.type && Blocks.compSpec(d.type)?.fields.find(x => x.name === last);
    return f ? `${f.label}${f.unit ? ` (${f.unit})` : ""}${path.startsWith("sizing.") ? " · sizing" : ""}` : path;
  },
};

window.addEventListener("DOMContentLoaded", () => {
  document.getElementById("pt-compare").addEventListener("click", () => Compare.open().catch(err => toast(err.message, true)));
  for (const s of ["a", "b"])
    document.getElementById(`cmp-${s}`).addEventListener("change", () => Compare.render().catch(err => toast(err.message, true)));
  document.getElementById("cmp-swap").addEventListener("click", () => {
    const a = document.getElementById("cmp-a"), b = document.getElementById("cmp-b");
    [a.value, b.value] = [b.value, a.value];
    Compare.render().catch(err => toast(err.message, true));
  });
  for (const s of ["a", "b"])            // a block clicked (view mode does not select nodes)
    document.getElementById(`cmp-canvas-${s}`).addEventListener("click", e => {
      const n = e.target.closest(".drawflow-node[data-cmp-key]");
      if (n) Compare.select(n.dataset.cmpKey, true, s);
    });
  document.getElementById("cmp-close").addEventListener("click", () => Compare.leave().catch(err => toast(err.message, true)));
  Compare.dlg().addEventListener("cancel", e => { e.preventDefault(); Compare.leave().catch(err => toast(err.message, true)); });
  Compare.dlg().addEventListener("close", () => Compare.closed());
  for (const s of ["a", "b"]) Compare.bindView(s);
  document.getElementById("cmp-fit").addEventListener("click", () => Compare.fit());
  document.querySelectorAll("#cmp-dialog [data-zoom]").forEach(b => b.addEventListener("click", () => {
    const el = document.getElementById("cmp-canvas-a"), f = b.dataset.zoom === "in" ? 1.2 : 1 / 1.2;
    for (const s of Compare.linked() ? ["a"] : ["a", "b"])   // both canvases, each around its centre
      Compare.zoomAt(s, f, el.clientWidth / 2, el.clientHeight / 2);
  }));
  document.getElementById("cmp-props").addEventListener("change", () => Compare.changed());   // a field edited
  document.getElementById("cmp-props").addEventListener("click", e => {
    const b = e.target.closest("[data-open]");
    if (b) Compare.switchTo(b.dataset.open, b.dataset.side).catch(err => toast(err.message, true));
  });
  document.getElementById("cmp-detail").addEventListener("click", e => {
    const a = e.target.closest("[data-key]");
    if (a) { e.preventDefault(); Compare.select(a.dataset.key); }
  });
});
