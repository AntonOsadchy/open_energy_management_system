/* The settings of every run: run mode (single period, multi-year, lifetime optimisation), base period, years,
   resolution, time zone, time limit and compute. They are edited in the Properties panel of the ⚙️ Run settings block
   (#run-settings is moved there while the block is selected, see Panel.investForm) and stored with the project; the
   inputs keep their ids, so App.settings() / applySettings() read and write them. ▶ Run only starts the run. */
const RunSettings = {
  picked: false,          // the user chose a mode in this session: do not preselect the lifetime optimisation again
  ok: true,               // the settings allow a run (a lifetime optimisation needs CAPEX and OPEX of every sized asset)

  mode() { return document.querySelector('input[name="run-mode"]:checked')?.value || "single"; },
  setMode(m) { const r = document.querySelector(`input[name="run-mode"][value="${m}"]`); if (r) r.checked = true; },
  compute() { return document.querySelector('input[name="run-compute"]:checked')?.value || "local"; },
  setCompute(c) { const r = document.querySelector(`input[name="run-compute"][value="${c}"]`); if (r) r.checked = true; },
  cloud: null,            // /api/cloud: {available, reason, local_gb, cloud_gb}

  init() {
    const form = document.getElementById("run-settings");
    document.querySelectorAll('input[name="run-mode"]').forEach(r => r.addEventListener("change", () => {
      this.picked = true; this.refresh(); App.dirty();
    }));
    form.querySelectorAll("input, select").forEach(i => i.addEventListener("change", () => { this.refresh(); App.dirty(); }));
    api("/api/cloud").then(c => { this.cloud = c; this.refresh(); }).catch(() => {});   // Modal set up? RAM here and there
  },

  /* back to its hidden home before the Properties panel is redrawn (which would drop it). An edited input of the
     panel is blurred first: its change event (fired when it is removed) may redraw the panel itself. */
  park() {
    if (document.getElementById("panel").contains(document.activeElement)) document.activeElement.blur();
    document.getElementById("run-settings-home").append(document.getElementById("run-settings"));
  },

  /* ▶ Run: run with the settings of the Run settings block. Without one, the block is added and shown first. */
  async start() {
    let inv = Blocks.investNode();
    if (!inv) {
      inv = Blocks.editor.getNodeFromId(Blocks.addInvest(40, 40));
      App.dirty();
      Layout.open("side");
      Panel.show(inv.id);
      return toast("Check the run in the ⚙️ Run settings block, then press ▶ Run again");
    }
    this.refresh();
    if (!this.ok) {
      Layout.open("side");
      Panel.show(inv.id);
      return toast("The run settings are not complete - see the ⚙️ Run settings block", true);
    }
    await App.run();
  },

  /* show what the chosen mode needs and what the run will be: steps, optimised assets, missing CAPEX / OPEX */
  refresh() {
    const sized = Blocks.optimisedAssets();
    if (sized.length && !this.picked && this.mode() === "single") this.setMode("optimise");
    if (!sized.length && this.mode() === "optimise") this.setMode("single");
    const mode = this.mode(), inv = Blocks.investNode();
    const set = v => v !== null && v !== undefined && v !== "";
    const hurdles = [set(inv?.data.min_irr) ? `IRR ≥ ${fmt(Number(inv.data.min_irr))} %` : "",
      set(inv?.data.max_payback) ? `${inv.data.payback_kind === "discounted" ? "discounted " : ""}payback ≤ ${
        fmt(Number(inv.data.max_payback))} years` : ""].filter(Boolean);
    const selfSuff = set(inv?.data.min_self_sufficiency) ? `, self-sufficiency ≥ ${fmt(Number(inv.data.min_self_sufficiency))} %` : "";
    const optRadio = document.querySelector('input[name="run-mode"][value="optimise"]');
    optRadio.disabled = !sized.length;
    document.getElementById("run-mode-opt").title = sized.length ? "" :
      "Switch a PV or storage block to Variable size first";
    document.getElementById("lbl-years").hidden = mode === "single";
    document.getElementById("lbl-years-text").textContent = mode === "optimise" ? "Lifetime (years)" : "Years";
    document.querySelectorAll("#panel .rs-econ").forEach(el => (el.hidden = mode === "single"));
    document.querySelectorAll("#panel .rs-opt").forEach(el => (el.hidden = mode !== "optimise"));
    document.getElementById("lbl-lifemodel").hidden = mode !== "optimise";
    const s = App.settings();
    const perDay = { "15min": 96, "30min": 48, "1h": 24 }[s.freq] || 24;
    const weighted = mode === "optimise" && s.lifetime_model !== "full";
    const years = mode === "single" ? 1 : s.sim_years, copies = weighted ? 1 : years;
    const steps = Math.round(s.days * perDay) * copies;
    let html = `<div class="hint">${steps.toLocaleString()} time steps${copies > 1 ? ` (${years} × ${Math.round(s.days * perDay).toLocaleString()})` : ""}${
      weighted && years > 1 ? `, weighted for ${years} years` : ""}${
      mode === "optimise" ? " in one model, solved twice (with the optimal sizes and without the investment" +
        (hurdles.length ? ", one after the other: the hurdles are measured against the case without" : "") + ")" : ""}.</div>`;
    // compute: the cloud takes single-period runs and lifetime optimisations (multi-year runs stay here)
    const c = this.cloud, cloudOk = !!c?.available && mode !== "multi_year";
    const cloudRadio = document.querySelector('input[name="run-compute"][value="cloud"]');
    cloudRadio.disabled = !cloudOk;
    if (!cloudOk && this.compute() === "cloud") this.setCompute("local");
    document.getElementById("run-compute-cloud").title = cloudOk ? "" : mode === "multi_year" ?
      "Multi-year runs (fixed sizes, degradation) run on this computer" : (c?.reason || "cloud status unknown");
    // memory: about 30 kB per time step and solve (Pyomo model + HiGHS; measured), one solve at a time
    const gb = (steps * 30 / 1024 + 150) / 1024, cloud = this.compute() === "cloud";
    const room = cloud ? c?.cloud_gb : c?.local_gb;
    html += `<div class="hint">Memory needed ≈ ${gb.toFixed(1)} GB${room ? ` · ${cloud ? "cloud machine" : "this computer"} has ${fmt(room)} GB` : ""}${
      cloud ? " · billed per second while it runs" : ""}.</div>`;
    if (!cloud && c?.local_gb && gb > 0.6 * c.local_gb)
      html += `<div class="msg warn">This run needs a large part of this computer's memory and may slow to a crawl (swapping).
        ${cloudOk ? "Choose <b>Cloud</b> above, or" : "Use"} a shorter base period (its costs are scaled to a year).</div>`;
    if (cloud && c?.cloud_gb && gb > 0.8 * c.cloud_gb)
      html += `<div class="msg warn">Close to the cloud machine's memory: use a shorter base period.</div>`;
    let ok = true;
    if (mode === "optimise") {
      html += `<table class="kpi"><tr><th>Optimised</th><th>Size</th><th>CAPEX / OPEX</th></tr>${sized.map(({ node, asset: a, missing }) => {
        const st = node.data.type === "Storage";
        const lo = Number((st ? a.e_size_min : a.capacity_min) || 0), hi = st ? a.e_size_max : a.capacity_max;
        const range = `${fmt(lo)}–${hi === undefined || hi === null || hi === "" ? "∞" : fmt(Number(hi))} ${st ? "MWh" : "MW"}`;
        return `<tr><td>${esc(node.data.name)}</td><td>${range}</td><td class="${missing.length ? "err-text" : ""}">${
          missing.length ? "missing " + missing.map(esc).join(", ") : "✓"}</td></tr>`;
      }).join("")}</table><div class="hint">Discount rate ${fmt(Number(inv ? inv.data.discount_rate || 0 : 7))} %${
        Number(inv?.data.inflation) ? `, inflation ${fmt(Number(inv.data.inflation))} %/yr` : ""}${
        hurdles.length ? `, hurdles ${hurdles.join(" and ")}` : ""}${selfSuff}.
        Lifetime = the years above; the base period is repeated every year.</div>`;
      ok = sized.length > 0 && sized.every(o => !o.missing.length);
      if (!ok) html += `<div class="msg err">Enter CAPEX and OPEX for every variable-size block (in its own settings).</div>`;
    }
    document.getElementById("run-info").innerHTML = html;
    this.ok = ok;
    if (inv) Blocks.updateInvest(inv.id);
  },
};
