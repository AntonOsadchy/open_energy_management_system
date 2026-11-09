/* Saved results ("runs"): each project or variant keeps the results of its last run (canvas + results). They are
   saved when a run finishes, replacing the ones before (the server drops them and points the saved project at the new
   ones); reopened with the project, from the Project tab or from the saved-cases window (projects.js). */
const Simulations = {
  list: [],

  /* keep the shown results as the project's results (logged in and the project named; otherwise on its first save) */
  async keep() {
    const name = Results.project?.settings?.name;
    if (!Results.summary || !Results.runId || Results.saved || !Account.user || !name) return;
    Results.saved = await postJSON("/api/simulations", { run_id: Results.runId, name, notes: Results.notes,
      project: Results.project });
    Results.updateBar();
    await ProjectTree.refresh();
  },

  async refresh() {
    this.list = await api("/api/simulations");
    Projects.render();
  },

  /* a row of the Open window, indented under its project */
  row(m) {
    return `<tr class="run" data-kind="run" data-id="${esc(m.id)}">
        <td>▸ <b>${esc(m.name)}</b>${m.notes ? `<div class="hint">${esc(m.notes)}</div>` : ""}</td>
        <td>${esc(m.created.slice(0, 16).replace("T", " "))}</td>
        <td>${esc(m.start || "")} · ${m.steps} × ${esc(m.resolution)}</td>
        <td class="num">${fmt(m.total_cost)} €</td>
        <td class="acts"><button class="small primary" data-act="open">Open</button>
          <button class="small" data-act="delete" title="Delete">🗑</button></td></tr>`;
  },

  /* renaming a project takes its saved runs along */
  async moveProject(from, to) {
    for (const m of this.list.filter(m => m.project_name === from))
      Object.assign(m, await api(`/api/simulations/${m.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_name: to }) }));
  },

  async act(id, what) {
    const meta = this.list.find(m => m.id === id);
    if (what === "open") {
      const sim = await api(`/api/simulations/${id}`);
      App.load(sim.project, true);
      await Results.load(null, sim.meta);
      App.status(`Saved simulation '${sim.meta.name}' - change the model and run again to compare.`);
      Projects.close();
      ProjectTree.render();
      toast(`Opened simulation '${sim.meta.name}'`);
    } else if (what === "rename") {
      const name = prompt("Rename simulation:", meta.name);
      if (!name) return;
      const notes = prompt("Notes (optional):", meta.notes || "");
      Object.assign(meta, await api(`/api/simulations/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, notes: notes ?? meta.notes }) }));
      if (Results.saved?.id === id) {   // the shown run: its name and comments box follow
        Object.assign(Results.saved, meta);
        Results.notes = document.getElementById("res-notes-text").value = meta.notes || "";
        Results.updateBar();
      }
      Projects.render();
      ProjectTree.render();
    } else if (what === "delete") {
      if (!confirm(`Delete the results of '${meta.project_name || meta.name}'? This cannot be undone.`)) return;
      await api(`/api/simulations/${id}`, { method: "DELETE" });
      this.list = this.list.filter(m => m.id !== id);
      Projects.render();
      ProjectTree.render();
    }
  },
};
