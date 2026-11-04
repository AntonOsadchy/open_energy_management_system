/* Wiring from either end, and dropping wires on a whole block.
 *
 * Drawflow only starts wires at outputs and only accepts drops exactly on an input dot. This adds:
 *  - dragging from an INPUT connector (component energy input, grey parameter, bus supply) to a block or one of its
 *    output connectors;
 *  - dropping on a block body instead of a connector dot, in both directions: the matching connector is chosen
 *    (bus supply/withdraw, the input's single output, or the first free compatible component connector).
 * Wires are created with editor.addConnection, so Rules.onCreated validates them as usual.
 */
const Wiring = {
  drag: null,    // reverse drag in progress: {node, input, conn, path, hover}
  hover: null,

  init() {
    const box = document.getElementById("drawflow");
    box.addEventListener("mousedown", e => this.start(e), true);
    document.addEventListener("mousemove", e => this.move(e));
    document.addEventListener("mouseup", e => this.end(e));
    box.addEventListener("mouseup", e => this.forwardDrop(e), true);
    box.addEventListener("mousemove", e => {
      if (Blocks.editor.connection) this.setHover(this.nodeAt(e.target, Blocks.editor.ele_selected));
    });
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
    this.move(e);
  },

  move(e) {
    if (!this.drag) return;
    const m = this.toCanvas(e.clientX, e.clientY), a = this.center(this.drag.el);
    this.drag.path.setAttribute("d", Blocks.editor.createCurvature(m.x, m.y, a.x, a.y, Blocks.editor.curvature, "openclose"));
    this.setHover(this.nodeAt(e.target, this.drag.el));
  },

  end(e) {
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
    Blocks.editor.addConnection(target.id, d.node.id, `output_${oi + 1}`, `input_${d.idx + 1}`);
  },

  /* ------------------------------------------------------------ forward drag dropped on a block body */
  forwardDrop(e) {
    const ed = Blocks.editor;
    this.setHover(null);
    if (!ed.connection || !ed.ele_selected || e.target.closest?.(".input")) return;  // Drawflow handles dot drops
    const outEl = ed.ele_selected, targetEl = this.nodeAt(e.target, outEl);
    if (!targetEl) return;
    const src = ed.getNodeFromId(this.idOf(this.nodeOf(outEl)));
    const oi = parseInt([...outEl.classList].find(c => c.startsWith("output_")).split("_")[1]) - 1;
    const target = ed.getNodeFromId(this.idOf(targetEl));
    const ii = this.pickInput(target, src.data.outs[oi], src);
    // Drawflow removes its temporary wire on this mouseup; add the real one right after
    if (ii >= 0) setTimeout(() => ed.addConnection(src.id, target.id, `output_${oi + 1}`, `input_${ii + 1}`), 0);
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
    if (target.data.kind === "data") return conn.type === "data" ? 0 : -1;
    if (conn.type === "bus") {   // from a bus supply: a free energy output of the component
      return this.firstFree(target, "out", c => c.type === "energy" && this.kindOk(c, from.data.carrier_kind));
    }
    return -1;
  },

  /* input on `target` for a wire from output connector `conn` of `src` */
  pickInput(target, conn, src) {
    if (target.data.kind === "bus") return conn.type === "energy" && this.kindOk(conn, target.data.carrier_kind) ? 0 : -1;
    if (conn.type === "bus") return this.firstFree(target, "in", c => c.type === "energy" && this.kindOk(c, src.data.carrier_kind));
    // input -> component: only when exactly one parameter connector is free (else drop on the one you mean)
    if (conn.type === "data") {
      const free = target.data.ins.map((c, i) => (c.type === "data" && !this.wired(target, "in", i) ? i : -1)).filter(i => i >= 0);
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
