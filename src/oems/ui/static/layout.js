/* Pane layout: drag-resize and close/reopen the palette (left), the properties panel (right) and the results drawer
   (bottom). Sizes and open/closed state are kept per browser. */
const Layout = {
  KEY: "oems.layout",
  size: { palette: 250, side: 320, results: 340 },
  limits: { palette: [180, 480], side: [240, 720], results: [120, 900] },
  closed: { palette: false, side: true, results: true },

  init() {
    try { Object.assign(this, JSON.parse(localStorage.getItem(this.KEY)) || {}); } catch (_) { /* defaults */ }
    // Properties and results always start closed: selecting a block or showing results opens them.
    Object.assign(this.closed, { side: true, results: true });
    for (const p of Object.keys(this.size)) this.apply(p);
    document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => this.close(b.dataset.close)));
    document.querySelectorAll("[data-open]").forEach(b => b.addEventListener("click", () => this.open(b.dataset.open)));
    document.querySelectorAll(".resizer").forEach(r => r.addEventListener("pointerdown", e => this.drag(e, r.dataset.pane)));
    window.addEventListener("resize", () => { this.apply("results"); this.resizePlots(); });
    Palette.init();
  },

  save() {
    try { localStorage.setItem(this.KEY, JSON.stringify({ size: this.size, closed: this.closed })); } catch (_) { /* ignore */ }
  },

  apply(pane) {
    const [lo, hi] = this.limits[pane];
    const max = pane === "results" ? Math.min(hi, window.innerHeight - 48 - 120) : hi;
    const v = Math.max(lo, Math.min(max, this.size[pane]));
    document.body.style.setProperty(`--${pane}-size`, `${v}px`);
    document.body.classList.toggle(`${pane}-closed`, this.closed[pane]);
  },

  open(pane) {
    if (!this.closed[pane]) return;
    this.closed[pane] = false;
    this.apply(pane); this.save(); this.resizePlots();
    if (pane === "palette") Palette.render();
  },

  close(pane) {
    this.closed[pane] = true;
    this.apply(pane); this.save();
    if (pane === "palette") Palette.render();
  },

  drag(e, pane) {
    e.preventDefault();
    const start = pane === "results" ? e.clientY : e.clientX;
    const from = this.size[pane];
    const sign = pane === "palette" ? 1 : -1;   // palette grows to the right, side to the left, results upwards
    document.body.classList.add("resizing", pane === "results" ? "resizing-row" : "resizing-col");
    const move = ev => {
      this.size[pane] = from + sign * ((pane === "results" ? ev.clientY : ev.clientX) - start);
      this.apply(pane);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.classList.remove("resizing", "resizing-row", "resizing-col");
      this.size[pane] = parseFloat(getComputedStyle(document.body).getPropertyValue(`--${pane}-size`));
      this.save(); this.resizePlots();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  },

  resizePlots() {
    document.querySelectorAll("#results .js-plotly-plot").forEach(p => { if (p.offsetParent) Plotly.Plots.resize(p); });
  },
};

/* Palette: an icon rail (Project, Inputs, Blocks) that switches the panel beside it; clicking the open tab collapses
   the panel to the rail. The chosen tab is kept per browser. */
const Palette = {
  KEY: "oems.palette",
  TITLES: { project: "Project", inputs: "Inputs", blocks: "Building blocks" },
  tab: "blocks",

  init() {
    try { this.tab = this.TITLES[localStorage.getItem(this.KEY)] ? localStorage.getItem(this.KEY) : "blocks"; } catch (_) { /* default */ }
    document.querySelectorAll("#palette .rail [data-tab]").forEach(b => b.addEventListener("click", () => {
      if (b.dataset.tab === this.tab && !Layout.closed.palette) Layout.close("palette");
      else this.show(b.dataset.tab);
    }));
    this.render();
  },

  show(tab) {
    this.tab = tab;
    try { localStorage.setItem(this.KEY, tab); } catch (_) { /* ignore */ }
    Layout.open("palette");
    this.render();
  },

  render() {
    document.querySelectorAll("#palette .rail [data-tab]").forEach(b =>
      b.setAttribute("aria-selected", b.dataset.tab === this.tab && !Layout.closed.palette));
    document.querySelectorAll("#palette .pal-panel").forEach(p => { p.hidden = p.dataset.panel !== this.tab; });
    document.getElementById("pal-title").textContent = this.TITLES[this.tab];
  },
};
