/* Wiring from either end, and dropping wires on a whole block.
 *
 * Drawflow only starts wires at outputs and only accepts drops exactly on an input dot. This adds:
 *  - dragging from an INPUT connector (grey parameter, bus port) to a block or one of its output connectors;
 *  - dropping on a block body instead of a connector dot, in both directions: the matching connector is chosen
 *    (the bus port, the input's single output, or the first free compatible component energy port).
 * Wires are created with editor.addConnection, so Rules.onCreated validates them as usual.
 *
 * A bus is connectable all around: its one connector is a ring along the whole outline. While a wire is pulled over a
 * bus, snap points show on its outline and the wire snaps to the one nearest the pointer; the chosen point is kept
 * in the bus data (anchors, by source connector), so the user picks the side. A wire without one (made in code, or
 * in an older project) ends where it meets the outline nearest to the block it comes from (see busAnchor).
 */
const Wiring = {
  drag: null,    // reverse drag in progress: {node, input, conn, path, hover}
  hover: null,
  pick: null,    // snap point aimed at on a bus during a forward drag: {bus, anchor}
  pending: null, // snap point for the wire being created: {bus, anchor}
  dots: null,    // the shown snap points: {busEl, el}

  init() {
    const box = document.getElementById("drawflow");
    box.addEventListener("mousedown", e => this.start(e), true);
    document.addEventListener("mousemove", e => this.move(e));
    document.addEventListener("mouseup", e => this.end(e));
    box.addEventListener("mouseup", e => this.forwardDrop(e), true);
    box.addEventListener("mousemove", e => this.aim(e));
    // Drawflow redraws the wires of a node on every move, import and new wire; reshape the bus wires after it
    const ed = Blocks.editor, redraw = ed.updateConnectionNodes.bind(ed);
    ed.updateConnectionNodes = id => { redraw(id); this.reshapeBusWires(id); };
    ed.on("connectionCreated", info => this.keepAnchor(info));
    ed.on("connectionRemoved", info => this.dropAnchor(info));
  },

  /* ------------------------------------------------------------ geometry */
  toCanvas(cx, cy) {
    const ed = Blocks.editor, r = ed.precanvas.getBoundingClientRect();
    return { x: (cx - r.x) / ed.zoom, y: (cy - r.y) / ed.zoom };
  },
  center(el) {
    const r = el.getBoundingClientRect();
    return this.toCanvas(r.x + r.width / 2, r.y + r.height / 2);
  },
  nodeOf: el => el?.closest?.(".drawflow-node"),
  idOf: nodeEl => nodeEl.id.replace("node-", ""),
  /* the block under the pointer, other than the one the wire starts from */
  nodeAt(target, fromEl) {
    const n = this.nodeOf(target);
    return n && n !== this.nodeOf(fromEl) ? n : null;
  },
  setHover(nodeEl) {
    if (this.hover === nodeEl) return;
    this.hover?.classList.remove("drop-target");
    this.hover = nodeEl;
    nodeEl?.classList.add("drop-target");
  },

  /* ------------------------------------------------------------ bus outline */
  /* the point of a bus outline (a rounded rectangle) nearest to canvas point p, with the outward normal there */
  busAnchor(busEl, p) {
    const r = busEl.getBoundingClientRect(), a = this.toCanvas(r.x, r.y), z = Blocks.editor.zoom;
    const w = r.width / z, h = r.height / z, rad = Math.min(28, w / 2, h / 2);
    // clamp to the rectangle shrunk by the corner radius, then step out by the radius towards p
    const qx = Math.min(Math.max(p.x, a.x + rad), a.x + w - rad), qy = Math.min(Math.max(p.y, a.y + rad), a.y + h - rad);
    const dx = p.x - qx, dy = p.y - qy, d = Math.hypot(dx, dy);
    if (d > 1e-6) return { x: qx + dx / d * rad, y: qy + dy / d * rad, nx: dx / d, ny: dy / d };
    // p inside the bus: leave by the nearest side
    const [, nx, ny] = [[p.x - a.x, -1, 0], [a.x + w - p.x, 1, 0], [p.y - a.y, 0, -1], [a.y + h - p.y, 0, 1]]
      .reduce((m, s) => (s[0] < m[0] ? s : m));
    return { x: nx ? (nx < 0 ? a.x : a.x + w) : p.x, y: ny ? (ny < 0 ? a.y : a.y + h) : p.y, nx, ny };
  },

  /* the snap points on a bus outline: thirds of the top and bottom, the middle of each end, the corners */
  busAnchors(busEl) {
    const r = busEl.getBoundingClientRect(), a = this.toCanvas(r.x, r.y), z = Blocks.editor.zoom;
    const w = r.width / z, h = r.height / z, rad = Math.min(28, w / 2, h / 2), c = rad * (1 - Math.SQRT1_2), d = Math.SQRT1_2;
    return [["t1", w / 4, 0, 0, -1], ["t2", w / 2, 0, 0, -1], ["t3", 3 * w / 4, 0, 0, -1],
      ["b1", w / 4, h, 0, 1], ["b2", w / 2, h, 0, 1], ["b3", 3 * w / 4, h, 0, 1],
      ["l", 0, h / 2, -1, 0], ["r", w, h / 2, 1, 0],
      ["tl", c, c, -d, -d], ["tr", w - c, c, d, -d], ["bl", c, h - c, -d, d], ["br", w - c, h - c, d, d]]
      .map(([id, x, y, nx, ny]) => ({ id, x: a.x + x, y: a.y + y, nx, ny }));
  },
  nearestAnchor(busEl, p) {
    return this.busAnchors(busEl).reduce((m, q) => (Math.hypot(q.x - p.x, q.y - p.y) < Math.hypot(m.x - p.x, m.y - p.y) ? q : m));
  },
  showAnchors(busEl, active) {
    if (this.dots?.busEl !== busEl) {
      this.hideAnchors();
      const el = document.createElement("div");
      el.className = "bus-anchors";
      Blocks.editor.precanvas.appendChild(el);
      this.dots = { busEl, el };
    }
    this.dots.el.innerHTML = this.busAnchors(busEl).map(q =>
      `<i class="${q.id === active ? "on" : ""}" style="left:${q.x}px;top:${q.y}px"></i>`).join("");
  },
  hideAnchors() { this.dots?.el.remove(); this.dots = null; },

  /* forward drag (Drawflow's) over a block: show a bus's snap points and run the wire into the nearest one */
  aim(e) {
    const ed = Blocks.editor;
    if (!ed.connection || !ed.ele_selected) return;
    const nodeEl = this.nodeAt(e.target, ed.ele_selected);
    this.setHover(nodeEl);
    this.pick = null;
    if (nodeEl?.classList.contains("bus-node")) {
      const src = ed.getNodeFromId(this.idOf(this.nodeOf(ed.ele_selected))), bus = ed.getNodeFromId(this.idOf(nodeEl));
      const oi = parseInt([...ed.ele_selected.classList].find(c => c.startsWith("output_")).split("_")[1]) - 1;
      if (this.pickInput(bus, src.data.outs[oi], src) === 0) {
        const q = this.nearestAnchor(nodeEl, this.toCanvas(e.clientX, e.clientY));
        this.pick = { bus: String(bus.id), anchor: q.id };
        this.showAnchors(nodeEl, q.id);
        ed.connection_ele?.querySelector(".main-path")?.setAttribute("d", this.busPath(this.center(ed.ele_selected), q));
        return;
      }
    }
    this.hideAnchors();
  },

  /* a new wire into a bus keeps the snap point it was dropped on; a removed one forgets it */
  anchorKey: info => `${info.output_id}_${info.output_class}`,
  keepAnchor(info) {
    const p = this.pending, ed = Blocks.editor;
    if (!p || p.bus !== String(info.input_id)) return;
    this.pending = null;
    const bus = ed.getNodeFromId(info.input_id);
    const live = bus.inputs[info.input_class]?.connections.some(c => String(c.node) === String(info.output_id) && c.input === info.output_class);
    if (!live) return;   // rejected by the wiring rules
    ed.updateNodeDataFromId(bus.id, { ...bus.data, anchors: { ...bus.data.anchors, [this.anchorKey(info)]: p.anchor } });
    this.reshapeBusWires("node-" + bus.id);
  },
  dropAnchor(info) {
    const ed = Blocks.editor, data = ed.drawflow.drawflow[ed.module].data[info.input_id]?.data;
    if (!data?.anchors?.[this.anchorKey(info)]) return;
    const { [this.anchorKey(info)]: _, ...anchors } = data.anchors;
    ed.updateNodeDataFromId(info.input_id, { ...data, anchors });
  },

  /* wire from an output dot s (leaving to the right) into a bus anchor e (arriving square to the outline) */
  busPath(s, e) {
    const k = Math.max(30, 0.4 * Math.hypot(e.x - s.x, e.y - s.y));
    return `M ${s.x} ${s.y} C ${s.x + k} ${s.y} ${e.x + e.nx * k} ${e.y + e.ny * k} ${e.x} ${e.y}`;
  },

  /* redraw the wires into buses: of one node ("node-<id>"), or all */
  reshapeBusWires(nodeElId) {
    const sel = nodeElId ? `.connection.node_in_${nodeElId}, .connection.node_out_${nodeElId}` : ".connection";
    for (const svg of Blocks.editor.precanvas.querySelectorAll(sel)) {
      const cls = [...svg.classList], get = pre => cls.find(c => c.startsWith(pre))?.slice(pre.length);
      const busId = get("node_in_node-"), busEl = document.getElementById("node-" + busId);
      if (!busEl?.classList.contains("bus-node")) continue;
      const dot = document.querySelector(`#node-${get("node_out_node-")} .outputs .output_${get("output_")}`);
      if (!dot) continue;
      const s = this.center(dot), ed = Blocks.editor;
      const id = ed.drawflow.drawflow[ed.module].data[busId]?.data.anchors?.[`${get("node_out_node-")}_output_${get("output_")}`];
      const e = (id && this.busAnchors(busEl).find(q => q.id === id)) || this.busAnchor(busEl, s);
      svg.querySelector(".main-path")?.setAttribute("d", this.busPath(s, e));
    }
  },

  /* ------------------------------------------------------------ reverse drag (from an input) */
  start(e) {
    const inp = e.target.closest?.(".drawflow-node .input");
    if (!inp || e.button !== 0) return;
    e.stopPropagation(); e.preventDefault();   // keep Drawflow from moving the block
    const nodeEl = this.nodeOf(inp);
    const node = Blocks.editor.getNodeFromId(this.idOf(nodeEl));
    const idx = parseInt([...inp.classList].find(c => c.startsWith("input_")).split("_")[1]) - 1;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "connection wiring-temp" + (node.data.ins[idx]?.type === "data" ? " c-data" : ""));
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", "main-path");
    svg.appendChild(path);
    Blocks.editor.precanvas.appendChild(svg);
    this.drag = { node, nodeEl, idx, conn: node.data.ins[idx], el: inp, svg, path };
    if (node.data.kind === "bus") {   // from the bus snap point nearest where it was grabbed
      this.drag.anchor = this.nearestAnchor(nodeEl, this.toCanvas(e.clientX, e.clientY));
      this.showAnchors(nodeEl, this.drag.anchor.id);
    }
    this.move(e);
  },

  move(e) {
    if (!this.drag) return;
    const m = this.toCanvas(e.clientX, e.clientY);
    if (this.drag.anchor) {
      this.drag.path.setAttribute("d", this.busPath(m, this.drag.anchor));
    } else {
      const a = this.center(this.drag.el);
      this.drag.path.setAttribute("d", Blocks.editor.createCurvature(m.x, m.y, a.x, a.y, Blocks.editor.curvature, "openclose"));
    }
    this.setHover(this.nodeAt(e.target, this.drag.el));
  },

  end(e) {
    this.hideAnchors();
    if (!this.drag) return;
    const d = this.drag;
    this.drag = null;
    d.svg.remove();
    this.setHover(null);
    const out = e.target.closest?.(".drawflow-node .output");
    const targetEl = this.nodeAt(e.target, d.el);
    if (!targetEl) return;
    const target = Blocks.editor.getNodeFromId(this.idOf(targetEl));
    let oi;
    if (out && this.nodeOf(out) === targetEl) oi = parseInt([...out.classList].find(c => c.startsWith("output_")).split("_")[1]) - 1;
    else oi = this.pickOutput(target, d.conn, d.node);
    if (oi < 0) return toast(`no connector on '${target.data.name || target.data.column}' fits '${d.conn.label || d.conn.name}'`, true);
    if (d.anchor) this.pending = { bus: String(d.node.id), anchor: d.anchor.id };
    Blocks.editor.addConnection(target.id, d.node.id, `output_${oi + 1}`, `input_${d.idx + 1}`);
    this.pending = null;
  },

  /* ------------------------------------------------------------ forward drag dropped on a block body */
  forwardDrop(e) {
    const ed = Blocks.editor;
    this.setHover(null);
    this.hideAnchors();
    // the snap point aimed at goes to the wire Drawflow (dot drop) or the timeout below (body drop) creates
    this.pending = this.pick;
    this.pick = null;
    setTimeout(() => { this.pending = null; }, 0);
    if (!ed.connection || !ed.ele_selected || e.target.closest?.(".input")) return;  // Drawflow handles dot drops
    const outEl = ed.ele_selected, targetEl = this.nodeAt(e.target, outEl);
    if (!targetEl) return;
    const src = ed.getNodeFromId(this.idOf(this.nodeOf(outEl)));
    const oi = parseInt([...outEl.classList].find(c => c.startsWith("output_")).split("_")[1]) - 1;
    const target = ed.getNodeFromId(this.idOf(targetEl));
    const ii = this.pickInput(target, src.data.outs[oi], src);
    // Drawflow removes its temporary wire on this mouseup; add the real one right after
    const pick = this.pending;
    if (ii >= 0) setTimeout(() => {
      this.pending = pick;
      ed.addConnection(src.id, target.id, `output_${oi + 1}`, `input_${ii + 1}`);
      this.pending = null;
    }, 0);
    else setTimeout(() => toast(`drop on a connector of '${target.data.name || target.data.column}': no single match`, true), 0);
  },

  /* ------------------------------------------------------------ connector choice */
  wired(node, side, idx) {
    const key = `${side === "in" ? "input" : "output"}_${idx + 1}`;
    return (side === "in" ? node.inputs : node.outputs)[key]?.connections.length > 0;
  },
  kindOk: (c, kind) => !c.kind || !kind || c.kind === kind,

  /* output on `target` for a wire into input connector `conn` of `from` */
  pickOutput(target, conn, from) {
    if (target.data.kind === "bus") return conn.type === "energy" && this.kindOk(conn, target.data.carrier_kind) ? 0 : -1;
    if (["data", "db", "profile"].includes(target.data.kind)) return conn.type === "data" ? 0 : -1;
    if (conn.type === "bus") {   // from a bus port: a free energy port of the component
      return this.firstFree(target, "out", c => c.type === "energy" && this.kindOk(c, from.data.carrier_kind));
    }
    return -1;
  },

  /* input on `target` for a wire from output connector `conn` of `src` */
  pickInput(target, conn, src) {
    if (target.data.kind === "bus") return conn.type === "energy" && this.kindOk(conn, target.data.carrier_kind) ? 0 : -1;
    // input -> component: only when exactly one parameter connector is free (else drop on the one you mean)
    if (conn.type === "data") {
      const curves = isCurves(src.data);
      const free = target.data.ins.map((c, i) => (c.type === "data" && (c.name === "degradation") === curves &&
        !this.wired(target, "in", i) ? i : -1)).filter(i => i >= 0);
      return free.length === 1 ? free[0] : -1;
    }
    return -1;
  },

  /* the first matching connector that has no wire yet */
  firstFree(node, side, match) {
    const list = side === "in" ? node.data.ins : node.data.outs;
    return list.findIndex((c, i) => match(c) && !this.wired(node, side, i));
  },
};
