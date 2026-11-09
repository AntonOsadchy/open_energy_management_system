/* Logins and saved projects ("cases"): each user sees, opens, renames and deletes only their own.
   The saved-cases window lists the projects, each with its saved runs (simulations.js) under it; the project tree in the
   Blocks sidebar shows them with their variants. */
const Account = {
  user: null,
  pending: null,   // {resolve, reject} of an ensure() waiting for the login dialog

  async init() {
    this.user = (await api("/api/auth/me")).user;
    this.renderChip();
    ProjectTree.refresh();
  },

  /* logged in: an initials avatar opening a menu (log out); logged out: Log in */
  renderChip() {
    const u = this.user;
    if (!u) { document.getElementById("user-chip").innerHTML = `<button id="btn-login" class="signin">Log in</button>`; return; }
    const initials = (u.match(/[A-Za-zÀ-ÿ0-9]+/g) || [u]).slice(0, 2).map(w => w[0].toUpperCase()).join("");
    let h = 0;
    for (const c of u) h = (h * 31 + c.charCodeAt(0)) % 360;
    const av = `style="background:hsl(${h} 50% 42%)"`;
    document.getElementById("user-chip").innerHTML =
      `<button id="btn-account" class="avatar" ${av} title="${esc(u)}" aria-haspopup="menu" aria-expanded="false">${esc(initials)}</button>
      <div id="account-menu" class="menu" role="menu" hidden>
        <div class="who"><span class="avatar" ${av}>${esc(initials)}</span><div><b>${esc(u)}</b><small>Signed in</small></div></div>
        <hr><button id="btn-logout" role="menuitem">Log out</button></div>`;
  },

  menu(open) {
    const m = document.getElementById("account-menu");
    if (!m) return;
    m.hidden = !open;
    document.getElementById("btn-account").setAttribute("aria-expanded", open);
  },

  /* resolves with the user name, asking for a login first if needed; rejects when the dialog is closed */
  ensure() {
    if (this.user) return Promise.resolve(this.user);
    return new Promise((resolve, reject) => { this.pending = { resolve, reject }; this.show(); });
  },

  show() {
    document.getElementById("login-msg").textContent = "";
    document.getElementById("login-dialog").showModal();
    document.getElementById("login-user").focus();
  },

  async submit(register) {
    const user = document.getElementById("login-user").value, password = document.getElementById("login-pw").value;
    try {
      this.user = (await postJSON(`/api/auth/${register ? "register" : "login"}`, { user, password })).user;
    } catch (e) {
      document.getElementById("login-msg").textContent = e.message;
      return;
    }
    document.getElementById("login-pw").value = "";
    this.renderChip();
    const p = this.pending;
    this.pending = null;
    document.getElementById("login-dialog").close();
    ProjectTree.refresh();
    toast(register ? `Account '${this.user}' created` : `Logged in as ${this.user}`);
    p?.resolve(this.user);
  },

  async logout() {
    this.menu(false);
    await postJSON("/api/auth/logout", {});
    this.user = null;
    this.renderChip();
    Projects.close();
    ProjectTree.refresh();
    toast("Logged out");
  },
};

const Projects = {
  list: [],

  async open() {
    await Account.ensure();
    this.list = await api("/api/projects");
    await Simulations.refresh();   // renders both
    const dlg = document.getElementById("proj-dialog");
    if (!dlg.open) dlg.showModal();
  },

  close() { const d = document.getElementById("proj-dialog"); if (d.open) d.close(); },

  /* one table: each project (its variants are in the Project tab), then its results; results of projects that are not saved come last */
  render() {
    document.getElementById("proj-title").textContent = `Cases of ${Account.user}`;
    const runs = Simulations.list, names = new Set(this.list.map(m => m.name));
    const rows = ProjectTree.projects(this.list).map(m => `<tr data-kind="project" data-id="${esc(m.id)}">
        <td><b>${esc(m.name)}</b></td>
        <td>${esc(new Date(m.latest * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }))}${
          m.variants ? `<div class="hint">${m.variants} variant${m.variants > 1 ? "s" : ""}</div>` : ""}</td>
        <td>${esc(m.start || "")}${m.days ? ` · ${m.days} d` : ""}${m.freq ? ` ${esc(m.freq)}` : ""}</td>
        <td class="num">${m.blocks} blocks</td>
        <td class="acts"><button class="small primary" data-act="open">Open</button>
          <button class="small" data-act="rename">Rename</button>
          <button class="small" data-act="delete" title="Delete">🗑</button></td></tr>` +
      runs.filter(r => r.project_name === m.name).map(r => Simulations.row(r)).join("")).join("");
    const other = runs.filter(r => !names.has(r.project_name));
    const otherRows = other.length ? `<tr class="group"><td colspan="5">Runs of projects that are not saved</td></tr>` +
      other.map(r => Simulations.row(r)).join("") : "";
    const hint = this.list.length ? "" :
      `<p class="hint">No saved cases yet. Build a model and save it with <b>⌘S</b> (Ctrl+S) or the save button in the Project tab.</p>`;
    document.getElementById("proj-list").innerHTML = hint + (rows || otherRows ?
      `<table class="kpi sims"><tr><th>Name</th><th>Saved</th><th>Horizon</th><th>Total cost</th><th></th></tr>${rows}${otherRows}</table>` : "");
  },

  async act(id, what) {
    const meta = this.list.find(m => m.id === id), url = `/api/projects/${encodeURIComponent(id)}`;
    if (what === "open") {
      App.load(await api(url));
      this.close();
    } else if (what === "rename") {
      const name = prompt("Rename case:", meta.name);
      if (!name || name === meta.name) return;
      await api(url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
      await Simulations.moveProject(meta.name, name);
      if (App.settings().name === meta.name) App.applySettings({ name });
      await this.open();
    } else if (what === "delete") {
      if (!confirm(`Delete case '${meta.name}'? This cannot be undone.`)) return;
      await api(url, { method: "DELETE" });
      this.list = this.list.filter(m => m.id !== id);
      this.render();
    }
    if (what !== "open") ProjectTree.refresh();
  },
};

/* The Project tab: the open project with its variants nested under it (a variant is a project saved with
   "variant_of" naming the project it was branched from), from the project they all stem from down, each with the
   runs saved from it. Empty while no project is open. Click a project or run to open it; ⑂ Variant saves the canvas as
   a new variant of the open project and switches to it; + starts an empty canvas as a new variant. */
const ProjectTree = {
  list: [],
  collapsed: new Set(),   // names of projects whose variants and runs are hidden

  async refresh() {
    [this.list, Simulations.list] = Account.user ?
      await Promise.all([api("/api/projects").catch(() => []), api("/api/simulations").catch(() => [])]) : [[], []];
    this.render();
  },

  render() {
    App.showSaved();
    const el = document.getElementById("proj-tree"), current = App.settings().name;
    document.getElementById("pt-title").textContent = current ? "Variants & runs" : "Project";
    if (!current) { el.innerHTML = ""; return; }   // no project open
    if (!Account.user) {
      el.innerHTML = `<p class="hint"><a href="#" data-act="login">Log in</a> to see the variants and saved runs of this project.</p>`;
      return;
    }
    const byName = Object.fromEntries(this.list.map(m => [m.name, m])), kids = {}, runs = {};
    for (const m of this.list) if (m.parent && m.parent !== m.name && byName[m.parent]) (kids[m.parent] ??= []).push(m);
    for (const k of Object.values(kids)) k.sort((a, b) => a.name.localeCompare(b.name));
    for (const r of Simulations.list) (runs[r.project_name] ??= []).push(r);
    const pad = depth => `style="padding-left:${4 + 14 * depth}px"`;
    const edit = `<span class="pt-edit"><button class="icon-btn" data-act="rename" title="Rename"><svg><use href="#i-pencil"/></svg></button><button
          class="icon-btn" data-act="delete" title="Delete"><svg><use href="#i-trash"/></svg></button></span>`;
    // the results of a project's last run (older saves may have kept several: the newest is shown)
    const runRows = (name, depth) => (runs[name] || []).slice(0, 1).map(r => `<div class="pt-run${Results.saved?.id === r.id ? " on" : ""}"
        data-run="${esc(r.id)}" ${pad(depth)} title="Results of the last run · ${esc(r.created.slice(0, 16).replace("T", " "))} · total cost ${
        fmt(r.total_cost)} €"><span class="pt-tog"></span><svg class="pt-ico"><use href="#i-wave"/></svg>
        <span class="pt-name">Results <small>${esc(r.created.slice(5, 16).replace("T", " "))}</small></span><span class="pt-edit"><button
          class="icon-btn" data-act="delete" title="Delete the results"><svg><use href="#i-trash"/></svg></button></span></div>`).join("");
    const seen = new Set();
    const row = (m, depth) => {
      seen.add(m.name);
      const own = (runs[m.name] || []).slice(0, 1), shut = this.collapsed.has(m.name);
      const sub = (kids[m.name] || []).filter(k => !seen.has(k.name)), fold = sub.length || own.length;
      return `<div class="pt-row${m.name === current ? " on" : ""}" data-id="${esc(m.id)}" data-name="${esc(m.name)}"
          ${pad(depth)} title="${esc(m.parent ? `Variant of '${m.parent}'` : "Project")} · saved ${
          esc(new Date(m.saved * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }))}">
        <span class="pt-tog" data-act="toggle">${fold ? (shut ? "▸" : "▾") : ""}</span>
        <span class="pt-name">${depth ? "⑂ " : ""}${esc(m.name)}</span>${m.name === current ? `<span class="pt-unsaved">unsaved</span>` : ""}${sub.length ? `<span class="pt-n">${sub.length}</span>` : ""}${edit}</div>` +
        (shut ? "" : (own.length ? runRows(m.name, depth + 1) : "") + sub.map(k => row(k, depth + 1)).join(""));
    };
    if (!byName[current]) {   // a new project, not saved yet: only the runs made from it
      el.innerHTML = `<div class="pt-row on unsaved" title="Not saved yet: press ⌘S (Ctrl+S)"><span class="pt-tog"></span>
        <span class="pt-name">${esc(current)}</span><span class="pt-n">not saved</span></div>` + runRows(current, 1);
      return;
    }
    let root = byName[current];   // up to the project the open one stems from: its whole family is listed
    const up = new Set([root.name]);
    while (byName[root.parent] && !up.has(root.parent)) { root = byName[root.parent]; up.add(root.name); }
    el.innerHTML = row(root, 0);
  },

  async click(e) {
    if (e.target.closest("[data-act=login]")) { e.preventDefault(); return Account.ensure(); }
    const run = e.target.closest(".pt-run");
    if (run) {
      const act = e.target.closest("[data-act=rename], [data-act=delete]")?.dataset.act;
      if (act) return Simulations.act(run.dataset.run, act);
      if (run.classList.contains("on") || !App.discardOk()) return;
      return Simulations.act(run.dataset.run, "open");
    }
    const r = e.target.closest(".pt-row[data-id]");
    if (!r) return;
    const act = e.target.closest("[data-act=rename], [data-act=delete]")?.dataset.act;
    if (act) return this[act](this.list.find(m => m.id === r.dataset.id));
    if (e.target.closest("[data-act=toggle]")?.textContent.trim()) {
      const n = r.dataset.name;
      this.collapsed.has(n) ? this.collapsed.delete(n) : this.collapsed.add(n);
      return this.render();
    }
    if (r.dataset.name === App.settings().name || !App.discardOk()) return;
    App.load(await api(`/api/projects/${encodeURIComponent(r.dataset.id)}`));
  },

  /* rename a project or variant: its variants and saved runs follow it (the server relinks the variants) */
  async rename(m) {
    const name = prompt(`Rename '${m.name}':`, m.name)?.trim();
    if (!name || name === m.name) return;
    if (this.list.some(x => x.name === name)) return toast(`A project named '${name}' already exists`, true);
    await api(`/api/projects/${encodeURIComponent(m.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    await Simulations.moveProject(m.name, name);
    if (App.settings().name === m.name) App.applySettings({ name });
    if (App.variantOf === m.name) App.variantOf = name;
    if (this.collapsed.delete(m.name)) this.collapsed.add(name);
    await this.refresh();
    toast(`Renamed '${m.name}' to '${name}'`);
  },

  /* delete a project or variant: its variants move up to its parent; its saved runs are kept. If it is open,
     the canvas stays as it is, now not saved. */
  async delete(m) {
    const kids = this.list.filter(x => x.parent === m.name).length;
    if (!confirm(`Delete '${m.name}'? This cannot be undone.${kids ? ` Its ${kids} variant(s) move up to ${
        m.parent ? `'${m.parent}'` : "the top level"}.` : ""}`)) return;
    await api(`/api/projects/${encodeURIComponent(m.id)}`, { method: "DELETE" });
    if (App.variantOf === m.name) App.variantOf = m.parent || null;
    await this.refresh();
    toast(`Deleted '${m.name}'`);
  },

  /* the project a saved project or variant stems from (itself for a project) */
  rootOf(name, list = this.list) {
    const by = Object.fromEntries(list.map(m => [m.name, m])), seen = new Set([name]);
    let m = by[name];
    while (m && by[m.parent] && !seen.has(m.parent)) { seen.add(m.parent); m = by[m.parent]; }
    return m;
  },

  /* the other saved projects and variants of the family of project ``name`` (the baselines it can be compared with) */
  family(name) {
    const root = this.rootOf(name)?.name;
    return root ? this.list.filter(m => m.name !== name && this.rootOf(m.name)?.name === root)
      .sort((a, b) => a.name.localeCompare(b.name)) : [];
  },

  /* separate projects only (variants are listed in the Project tab), most recently worked on first, each with
     its number of variants and the time any of them was last saved */
  projects(list = this.list) {
    const fam = {};
    for (const m of list) (fam[this.rootOf(m.name, list).name] ??= []).push(m);
    return list.filter(m => fam[m.name]).map(m => ({ ...m, variants: fam[m.name].length - 1,
      latest: Math.max(...fam[m.name].map(x => x.saved)) })).sort((a, b) => b.latest - a.latest);
  },

  /* save the canvas as a new variant of the open project (saving that project first if it never was); ``change``
     is applied to the canvas once the open project is saved and the variant named (an edit that makes the variant).
     Returns whether the variant was saved. */
  async branch(change = null) {
    await Account.ensure();
    await this.refresh();
    const names = new Set(this.list.map(m => m.name));
    if (!names.has(App.settings().name)) await App.save();
    const base = App.settings().name;
    if (!base) return false;   // naming the new project was cancelled
    let i = 1;
    while (names.has(`${base} - variant ${i}`)) i++;
    const name = prompt(`Name of the new variant of '${base}':`, `${base} - variant ${i}`)?.trim();
    if (!name) return false;
    if (name === base || names.has(name)) { toast(`A project named '${name}' already exists`, true); return false; }
    change?.();
    document.getElementById("set-name").textContent = name;
    App.variantOf = base;
    await App.save();
    return true;
  },

  /* start an empty canvas as a new variant in the open project's family, under the project they all stem from,
     keeping the run settings (the open project is saved first if it never was) */
  async blank() {
    await Account.ensure();
    await this.refresh();
    const names = new Set(this.list.map(m => m.name));
    if (!names.has(App.settings().name)) await App.save();
    else if (!App.discardOk()) return;
    const byName = Object.fromEntries(this.list.map(m => [m.name, m]));
    let root = App.settings().name;
    if (!root) return;   // naming the new project was cancelled
    const up = new Set([root]);
    while (byName[root]?.parent && byName[byName[root].parent] && !up.has(byName[root].parent)) up.add(root = byName[root].parent);
    let i = 1;
    while (names.has(`${root} - variant ${i}`)) i++;
    const name = prompt(`Name of the new empty variant of '${root}':`, `${root} - variant ${i}`)?.trim();
    if (!name) return;
    if (names.has(name) || name === root) return toast(`A project named '${name}' already exists`, true);
    App.load({ settings: { ...App.settings(), name }, variant_of: root, drawflow: { drawflow: { Home: { data: {} } } } }, true);
    await App.save();
  },
};

/* The project switcher: the open project's name in the top bar opens a menu of the saved projects, most recently
   saved first (type to find one), with rename, new project and the window of all projects and runs. */
const Switcher = {
  shown: 8,   // projects listed while the search box is empty

  menu: () => document.getElementById("proj-menu"),

  async toggle(open = this.menu().hidden) {
    this.menu().hidden = !open;
    document.getElementById("proj-switch").setAttribute("aria-expanded", open);
    if (!open) return;
    const q = document.getElementById("pm-search");
    q.value = "";
    q.hidden = !Account.user;
    this.render();
    q.focus();
    if (Account.user) { await ProjectTree.refresh(); this.render(); }
  },

  matches() {
    const q = document.getElementById("pm-search").value.trim().toLowerCase();
    const list = ProjectTree.projects();
    return q ? list.filter(m => m.name.toLowerCase().includes(q)) : list.slice(0, this.shown);
  },

  render() {
    const el = document.getElementById("pm-list"), current = ProjectTree.rootOf(App.settings().name)?.name;
    document.getElementById("pm-rename").lastChild.textContent = current ? "Rename…" : "Name project…";
    if (!Account.user) {
      el.innerHTML = `<button data-act="login" role="menuitem">Log in to see your projects</button>`;
      return;
    }
    const list = this.matches(), q = document.getElementById("pm-search").value.trim();
    el.innerHTML = list.length ? `<div class="pm-head">${q ? "Matching" : "Recent"}</div>` + list.map(m => `<button
        class="pm-proj${m.name === current ? " on" : ""}" data-id="${esc(m.id)}" role="menuitem"><b>${esc(m.name)}</b><small>${
        m.variants ? `${m.variants} variant${m.variants > 1 ? "s" : ""} · ` : ""}${esc(new Date(m.latest * 1000).toLocaleString([],
        { dateStyle: "medium", timeStyle: "short" }))}</small></button>`).join("") :
      `<p class="hint">${q ? "No project matches." : "No saved projects yet."}</p>`;
  },

  async open(id) {
    this.toggle(false);
    if (ProjectTree.list.find(m => m.id === id)?.name === App.settings().name || !App.discardOk()) return;
    App.load(await api(`/api/projects/${encodeURIComponent(id)}`));
  },

  /* a saved project is renamed where it is stored (its variants and runs follow); a new one just takes the name */
  async rename() {
    this.toggle(false);
    const el = document.getElementById("set-name"), meta = ProjectTree.list.find(m => m.name === el.textContent);
    if (meta) return ProjectTree.rename(meta);
    const name = prompt(el.textContent ? "Project name" : "Name of the new project:", el.textContent)?.trim();
    if (name) el.textContent = name;
    ProjectTree.render();
  },

  async click(e) {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.act === "login") { await Account.ensure(); return this.toggle(true); }
    if (b.dataset.id) return this.open(b.dataset.id);
    if (b.id === "pm-rename") return this.rename();
    this.toggle(false);
    if (b.id === "pm-new") return Start.show();
    if (b.id === "btn-open") return Projects.open();
  },
};

/* The start window, shown when the app opens (and from New project in the switcher): name a new project, open a
   saved one, or follow the tutorial. Closing it leaves an empty canvas with no project open. */
const Start = {
  dlg: () => document.getElementById("start-dialog"),

  async show() {
    document.getElementById("start-name").value = "";
    if (!this.dlg().open) this.dlg().showModal();
    document.getElementById("start-name").focus();
    await this.renderList();
  },

  close() { if (this.dlg().open) this.dlg().close(); },

  async renderList() {
    const el = document.getElementById("start-list");
    if (!Account.user) {
      el.innerHTML = `<p class="hint">Your saved projects are listed here once you are logged in.</p>
        <button type="button" data-act="login">Log in</button>`;
      return;
    }
    await ProjectTree.refresh();
    const list = ProjectTree.projects();
    el.innerHTML = list.length ? list.map(m => `<button type="button" class="start-proj" data-id="${esc(m.id)}">
        <b>${esc(m.name)}</b><small>${m.variants ? `${m.variants} variant${m.variants > 1 ? "s" : ""} · ` : ""}${
        esc(new Date(m.latest * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }))}</small></button>`).join("") :
      `<p class="hint">No saved projects yet.</p>`;
  },

  create() {
    const name = document.getElementById("start-name").value.trim();
    if (!name || !App.discardOk()) return;
    App.load({ settings: { name }, drawflow: { drawflow: { Home: { data: {} } } } }, true);
    this.close();
  },

  async click(e) {
    if (e.target.closest("[data-act=login]")) { await Account.ensure(); return this.renderList(); }
    const b = e.target.closest(".start-proj");
    if (!b || !App.discardOk()) return;
    App.load(await api(`/api/projects/${encodeURIComponent(b.dataset.id)}`));
    this.close();
  },
};

window.addEventListener("DOMContentLoaded", () => {
  Account.init().then(() => { if (!new URLSearchParams(location.search).has("nostart")) Start.show(); }).catch(e => toast(e.message, true));
  document.getElementById("user-chip").addEventListener("click", e => {
    const id = e.target.closest("button")?.id;
    if (id === "btn-login") Account.ensure().catch(() => {});
    if (id === "btn-account") Account.menu(document.getElementById("account-menu").hidden);
    if (id === "btn-logout") Account.logout().catch(err => toast(err.message, true));
  });
  document.getElementById("proj-switch").addEventListener("click", () => Switcher.toggle().catch(err => toast(err.message, true)));
  document.getElementById("proj-menu").addEventListener("click", e => Switcher.click(e).catch(err => toast(err.message, true)));
  document.getElementById("pm-search").addEventListener("input", () => Switcher.render());
  document.getElementById("pm-search").addEventListener("keydown", e => {   // Enter opens the first match
    const first = document.querySelector("#pm-list .pm-proj");
    if (e.key === "Enter" && first) Switcher.open(first.dataset.id).catch(err => toast(err.message, true));
  });
  document.addEventListener("click", e => {
    if (!e.target.closest("#user-chip")) Account.menu(false);
    if (!e.target.closest("#proj-switcher")) Switcher.toggle(false);
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") { Account.menu(false); Switcher.toggle(false); } });
  document.getElementById("login-form").addEventListener("submit", e => { e.preventDefault(); Account.submit(false); });
  document.getElementById("login-register").addEventListener("click", () => Account.submit(true));
  document.getElementById("login-dialog").addEventListener("close", () => {
    const p = Account.pending;
    Account.pending = null;
    p?.reject(new Error("Log in to see your saved cases"));
  });
  document.getElementById("proj-close").addEventListener("click", () => Projects.close());
  document.getElementById("start-new").addEventListener("submit", e => { e.preventDefault(); Start.create(); });
  document.getElementById("start-list").addEventListener("click", e => Start.click(e).catch(err => toast(err.message, true)));
  document.getElementById("start-close").addEventListener("click", () => Start.close());
  document.getElementById("start-tutorial").addEventListener("click", e => { e.preventDefault(); Start.close(); Tutorial.open(); });
  document.getElementById("proj-tree").addEventListener("click", e => ProjectTree.click(e).catch(err => toast(err.message, true)));
  document.getElementById("pt-variant").addEventListener("click", () => ProjectTree.branch().catch(err => toast(err.message, true)));
  document.getElementById("pt-blank").addEventListener("click", () => ProjectTree.blank().catch(err => toast(err.message, true)));
  document.getElementById("proj-list").addEventListener("click", e => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const tr = b.closest("tr");
    (tr.dataset.kind === "run" ? Simulations : Projects).act(tr.dataset.id, b.dataset.act).catch(err => toast(err.message, true));
  });
});
