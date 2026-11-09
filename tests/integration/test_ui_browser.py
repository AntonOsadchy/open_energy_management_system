"""End-to-end browser test of the drag-and-drop UI (headless Chromium via Playwright).

Skipped when Playwright or its browser is not installed:
    pip install playwright && python -m playwright install chromium
"""

import os
import socket
import threading
import time

import numpy as np
import pandas as pd
import pytest

playwright = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from helpers import use_fake_bess_db  # noqa: E402

from oems.ui.server import create_app  # noqa: E402

APPS = {}   # the server's FastAPI app, to swap in a fake database


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _set(page, sel, value):
    """Set a run setting (its input sits in the Run settings block's panel, parked while the block is not selected)."""
    page.evaluate("([s, v]) => { const el = document.querySelector(s); el.value = v; "
                  "el.dispatchEvent(new Event('change')); }", [sel, value])


def _tab(page, tab):
    """Show a palette panel: "project", "inputs" or "blocks" (the default)."""
    page.evaluate("t => Palette.show(t)", tab)


def _start_run(page, mode=None):
    """Pick the run mode (optional) in the ⚙️ Run settings block (added if the project has none) and press ▶ Run."""
    page.evaluate("m => { if (!Blocks.investNode()) Blocks.addInvest(40, 40); "
                  "if (m) { RunSettings.setMode(m); RunSettings.picked = true; RunSettings.refresh(); } }", mode)
    page.click("#btn-run")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    app = create_app(tmp_path_factory.mktemp("ws"))
    APPS["app"] = app
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


@pytest.fixture()
def page(server):
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary missing
            pytest.skip(f"chromium not available: {e}")
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(server.rstrip("/") + "/?nostart")                            # no start window: tests begin on the canvas
        try:
            pg.wait_for_function("typeof Blocks !== 'undefined' && Blocks.catalog !== null && Blocks.editor !== null", timeout=15000)
        except playwright.Error:
            browser.close()
            pytest.skip("UI libraries could not be loaded from the CDN (offline?)")
        pg.evaluate("Layout.open('side'); Layout.open('results')")   # both start closed; most tests use them
        pg.errors = errors
        yield pg
        browser.close()


def test_drag_wire_run(page, tmp_path):
    """Build grid + battery + demand on a bus (one ⇄ wire each for grid and battery), add uploaded CSV columns as
    separate input blocks, wire them, run, and check the results overlay."""
    # drag a bus and two palette components onto the canvas (real HTML5 drag & drop)
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 500, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 150, "y": 150})
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 150, "y": 420})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 820, "y": 300})
    names = page.evaluate("allNodes().map(n => n.data.name)")
    assert set(names) == {"el", "grid", "battery", "demand"}

    # upload a CSV through the file input -> its columns are listed under Inputs (one item per column)
    idx = pd.date_range("2025-01-01", periods=48, freq="1h")
    df = pd.DataFrame({"price": 50 + 30 * np.sin(np.arange(48) / 3), "load": 0.5}, index=idx).rename_axis("time")
    csv = tmp_path / "site.csv"
    df.to_csv(csv)
    page.set_input_files("#file-upload", str(csv))
    page.wait_for_selector('.ds-col[data-col="load"]')
    assert page.locator(".ds-col").count() == 2
    # each input becomes its own single-connector block (drag, or click)
    page.drag_and_drop('.ds-col[data-col="price"]', "#drawflow", target_position={"x": 60, "y": 120})
    page.click('.ds-col[data-col="load"]')
    inputs = page.evaluate("allNodes().filter(n => n.data.kind === 'data').map(n => n.data.outs.map(o => o.name))")
    assert sorted(inputs) == [["load"], ["price"]]
    assert page.locator(".ds-col.used").count() == 2       # marked as in use

    # wire through the same function the canvas uses (validates rules)
    page.evaluate("""() => {
        const id = n => String(allNodes().find(x => x.data.name === n || x.data.column === n).id);
        connectByName([id('grid'), 'link'], [id('el'), 'port']);            // one wire: import + export
        connectByName([id('battery'), 'link'], [id('el'), 'port']);         // one wire: charge + discharge
        connectByName([id('demand'), 'in'], [id('el'), 'port']);            // no direction: the port knows it
        connectByName([id('price'), 'price'], [id('grid'), 'import_price']);
        connectByName([id('load'), 'load'], [id('demand'), 'profile']);
    }""")
    edges = page.evaluate("edgesOf(Blocks.editor.export())")
    assert len(edges) == 5
    assert sum(1 for e in edges if "link" in (e["from"][1], e["to"][1])) == 2

    # an invalid wire (input -> bus) is rejected
    page.evaluate("connectByName([String(allNodes().find(x => x.data.kind === 'data').id), 'price'],"
                  " [String(allNodes().find(x => x.data.name === 'el').id), 'port'])")
    assert len(page.evaluate("edgesOf(Blocks.editor.export())")) == len(edges)

    # settings and run
    _set(page, "#set-days", "2")
    _start_run(page)
    page.wait_for_function("Results.summary !== null", timeout=60000)
    labels = page.locator(".edge-label").all_inner_texts()
    assert len(labels) == 3                                   # one label per energy wire
    assert any(t.startswith("import") and "export" in t for t in labels)   # both directions on the grid wire
    assert "€" in page.inner_text("#res-summary")
    assert "soc_mean" in page.inner_text("#res-summary")                 # SOC statistics of the battery
    battery_node = page.evaluate("allNodes().find(n => n.data.name === 'battery').id")
    badges = page.inner_text(f"#node-{battery_node} .blk-badges")
    assert "SOC" in badges and "%" in badges

    # ⤓ Download in the results bar: the data of the selected tab (here the Summary)
    with page.expect_download() as dl:
        page.click("#res-dl-xlsx")
    assert dl.value.suggested_filename.startswith("oems_summary_") and dl.value.suggested_filename.endswith(".xlsx")

    # results inspector: pop-up with the selected block's series, chart + table; filter, quick picks, Esc closes
    battery_id = page.evaluate("allNodes().find(n => n.data.name === 'battery').id")
    page.click(f"#node-{battery_id} .blk-head")
    page.click("#res-inspect")
    page.wait_for_selector("#ins-plot .main-svg")
    picked = page.eval_on_selector_all("#ins-list input:checked", "els => els.map(e => e.value)")
    assert picked and all("battery" in c for c in picked)            # focused on the selected block
    assert page.locator("#ins-table th").count() == len(picked) + 1
    page.fill("#ins-filter", "price")
    page.click('.ins-quick button[data-q="price"]')
    page.wait_for_function("Inspect.selected.some(c => c.startsWith('price'))")
    page.wait_for_function("document.querySelectorAll('#ins-table th').length === Inspect.selected.length + 1")
    page.click('.ins-quick button[data-q="none"]')
    page.wait_for_selector("#ins-empty:not([hidden])")
    page.fill("#ins-filter", "")
    page.check('#ins-list input[value="flow el → demand.in [MW]"]')
    page.check('#ins-list input[value="flow grid.import → el [MW]"]')
    page.check('#ins-list input[value="price el [EUR/MWh]"]')
    page.wait_for_function("document.querySelectorAll('#ins-table th').length === 4")
    page.wait_for_function("document.getElementById('ins-plot').data?.length === 3")    # 3 series plotted
    # downloads: selected / all as CSV and Excel, and the report (system picture, inputs, outputs, charts)
    assert "format=xlsx&cols=" in page.get_attribute("#ins-xlsx-sel", "href")
    assert page.locator('#inspect a[href$="timeseries?format=xlsx"]').count() == 1
    assert page.get_attribute("#ins-zip", "href").endswith("/download")
    assert page.locator("#res-report, #ts-download").count() == 0     # not on the main screen
    with page.expect_download() as dl:
        page.click("#ins-report")
    assert dl.value.suggested_filename.startswith("oems_report_") and dl.value.suggested_filename.endswith(".html")
    page.keyboard.press("Escape")
    assert page.locator("#inspect").count() == 0
    # without a selected block: the bus flows and price plus every storage's SOC (in % on its own axis)
    page.evaluate("Panel.clear()")
    page.click("#res-inspect")
    page.wait_for_function("document.getElementById('ins-plot')?.data?.some(t => t.yaxis === 'y3')")
    assert page.is_checked('#ins-list input[value="state battery.soc [0-1]"]')
    soc = page.evaluate("document.getElementById('ins-plot').data.find(t => t.yaxis === 'y3')")
    assert soc["name"] == "battery.soc [%]" and 0 <= min(v for v in soc["y"] if v is not None) <= max(soc["y"]) <= 100
    if os.environ.get("OEMS_SHOT"):
        page.screenshot(path=os.environ["OEMS_SHOT"])
    page.keyboard.press("Escape")

    # save the project: the results of its last run are kept with it; clear the canvas, reopen them without a solve
    page.evaluate("document.getElementById('set-name').textContent = 'base case'")
    page.keyboard.press("Control+s")
    page.wait_for_selector("#login-dialog[open]")
    page.fill("#login-user", "runner")
    page.fill("#login-pw", "secret1")
    page.click("#login-register")
    page.wait_for_function("Results.saved !== null")
    _switch(page, "#btn-open")
    page.wait_for_selector('#proj-list tr.run:has-text("base case")')
    page.click("#proj-close")
    page.evaluate("Blocks.editor.clear(); Panel.clear(); Results.summary = null; Results.updateBar()")
    assert page.evaluate("allNodes().length") == 0
    _switch(page, "#btn-open")
    page.click('#proj-list tr.run:has-text("base case") button[data-act="open"]')
    page.wait_for_function("Results.summary !== null && Results.saved !== null")
    assert len(page.evaluate("allNodes()")) == 7   # with the ⚙️ Run settings block
    assert len(page.locator(".edge-label").all_inner_texts()) == 3
    assert page.inner_text("#res-name") == "base case"
    page.click("#res-inspect")                                          # the table also comes from the saved zip
    page.wait_for_selector("#ins-table td")
    assert page.get_attribute("#ins-zip", "href").startswith("/api/simulations/")
    page.keyboard.press("Escape")
    assert not page.errors, page.errors


def test_properties_panel_edits_parameters(page):
    """Editing a field in the properties panel updates the block's parameters, and renaming updates the block title."""
    page.drag_and_drop('.pal-item[data-type="Storage"]', "#drawflow", target_position={"x": 300, "y": 300})
    node_id = page.evaluate("allNodes()[0].id")
    page.evaluate(f"Panel.show({node_id})")
    page.fill('#panel input[data-p="e_max"]', "3.5")
    page.dispatch_event('#panel input[data-p="e_max"]', "change")
    page.fill('#panel input[data-k="name"]', "tank")
    page.dispatch_event('#panel input[data-k="name"]', "change")
    data = page.evaluate(f"Blocks.editor.getNodeFromId({node_id}).data")
    assert data["params"]["e_max"] == 3.5 and data["name"] == "tank"
    assert page.inner_text(f"#node-{node_id} .blk-title") == "tank"
    assert not page.errors, page.errors


def test_converter_add_output(page):
    """'+ output' on a converter adds a new energy connector (out2) to the block."""
    page.drag_and_drop('.pal-item[data-preset="heat_pump"]', "#drawflow", target_position={"x": 300, "y": 300})
    node_id = page.evaluate("allNodes()[0].id")
    page.evaluate(f"Panel.show({node_id})")
    page.click('#panel button[data-add="out"]')
    outs = page.evaluate("allNodes()[0].data.outs.map(o => o.name)")
    assert outs == ["in1", "out1", "out2"]           # energy ports, inflows and outflows alike
    assert not page.errors, page.errors


def test_panes_resize_and_close(page):
    """The palette, properties panel and results drawer can be drag-resized, closed and reopened (and selecting a
    block reopens the properties panel); properties and results start closed on every load."""
    def width(sel):
        return page.evaluate(f"document.querySelector('{sel}').getBoundingClientRect().width")

    page.reload()                                          # properties and results start closed, even after use
    page.wait_for_function("typeof Blocks !== 'undefined' && Blocks.editor !== null")
    assert not page.is_visible("#side") and not page.is_visible("#results")
    assert page.is_visible('[data-open="side"]') and page.is_visible('[data-open="results"]')
    page.click('[data-open="side"]')
    page.click('[data-open="results"]')

    box = page.locator('.resizer[data-pane="side"]').bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + 200
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 100, y, steps=5)
    page.mouse.up()
    assert abs(width("#side") - 420) < 3

    page.click('[data-close="palette"]')                  # collapses the panel to the icon rail
    assert not page.is_visible("#palette .pal-body") and page.is_visible("#palette .rail")
    page.click('#palette .rail [data-tab="blocks"]')
    assert page.is_visible("#palette .pal-body")
    page.click('#palette .rail [data-tab="blocks"]')         # the open tab collapses again
    assert not page.is_visible("#palette .pal-body")
    page.click('#palette .rail [data-tab="inputs"]')
    assert page.is_visible(".pal-upload") and not page.is_visible('.pal-item[data-type="Storage"]')
    _tab(page, "blocks")

    page.click('[data-close="side"]')
    assert not page.is_visible("#side")
    page.drag_and_drop('.pal-item[data-type="Storage"]', "#drawflow", target_position={"x": 300, "y": 300})
    page.click(f"#node-{page.evaluate('allNodes()[0].id')} .blk-title")
    assert page.is_visible("#side")

    page.click('[data-close="results"]')
    assert not page.is_visible("#results") and page.is_visible('[data-open="results"]')
    page.click('[data-open="results"]')
    assert page.is_visible("#results")
    assert page.evaluate("JSON.parse(localStorage.getItem('oems.layout')).size.side") == 420
    assert not page.errors, page.errors


def test_tutorial_walkthrough(page):
    """The in-app tutorial: its checklist ticks for a block placed by hand (real drag of a Bus), "Do it for me"
    completes every other step, and the three runs reproduce the expected costs shown in the card
    (3240 EUR grid only, then with PV + battery and with the heat section)."""
    page.click("#btn-tutorial")
    n = page.evaluate("TUT_STEPS.length")
    costs = []
    for _ in range(n):
        title = page.inner_text("#tut-title")
        if title == "Add an electricity bus":  # done by hand: the checklist must notice it
            page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 300, "y": 260})
        elif page.is_visible("#tut-auto"):
            page.click("#tut-auto")
            page.wait_for_function("!document.querySelector('#tut-auto').disabled", timeout=60000)
        page.wait_for_function("Tutorial.refresh() || true")
        if page.evaluate("TUT_STEPS[Tutorial.i].checks.length"):
            page.wait_for_selector("#tutorial.done", timeout=5000)  # every check ticked
        if title.startswith("Run"):
            costs.append(page.evaluate("Results.summary.total_cost"))
        page.click("#tut-next")
    expected = page.evaluate("TUT_EXPECTED")
    assert costs == pytest.approx([expected["run1"], expected["run2"], expected["run3"]], abs=0.5)
    assert costs[0] == pytest.approx(3240)
    assert page.is_hidden("#tutorial")  # "Finish" closes the card
    kinds = sorted(n["data"].get("carrier_kind") for n in page.evaluate("allNodes()") if n["data"]["kind"] == "bus")
    assert kinds == ["electricity", "heat"]
    assert not page.errors, page.errors


def test_bus_kind_change_renames_default_carrier(page):
    """Switching a new bus from electricity to heat in the panel also changes its default carrier and name
    (el -> heat), so two buses never share a carrier name with different kinds; custom names are kept."""
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 300, "y": 300})
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 300, "y": 500})
    second = page.evaluate("allNodes()[1].id")
    page.evaluate(f"Panel.show({second})")
    page.select_option('#panel select[data-k="carrier_kind"]', "heat")
    d = page.evaluate(f"Blocks.editor.getNodeFromId({second}).data")
    assert (d["carrier"], d["carrier_kind"], d["name"]) == ("heat", "heat", "heat")
    page.fill('#panel input[data-k="name"]', "hot_loop")
    page.dispatch_event('#panel input[data-k="name"]', "change")
    page.select_option('#panel select[data-k="carrier_kind"]', "cold")
    assert page.evaluate(f"Blocks.editor.getNodeFromId({second}).data.name") == "hot_loop"
    assert not page.errors, page.errors


def _drag(page, src, dst):
    """Real mouse drag from the centre of element `src` to the centre of element `dst` (CSS selectors, or (x, y)
    page points)."""
    def point(t):
        if isinstance(t, tuple):
            return t
        b = page.locator(t).bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    page.mouse.move(*point(src))
    page.mouse.down()
    page.mouse.move(*point(dst), steps=8)
    page.mouse.up()


def _edge(page, sel, side):
    """A page point just inside the `side` edge of element `sel` (a bus outline ring)."""
    b = page.locator(sel).bounding_box()
    cx, cy = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    return {"top": (cx, b["y"] + 4), "bottom": (cx, b["y"] + b["height"] - 4),
            "left": (b["x"] + 4, cy), "right": (b["x"] + b["width"] - 4, cy)}[side]


def test_wires_can_start_at_either_end_and_drop_on_blocks(page, tmp_path):
    """Wires can be pulled from a component's energy port to a bus, from a bus port or a parameter back to a block,
    and can be dropped anywhere on a block: the matching connector is chosen (the bus port, the free energy port, the
    only free parameter). Invalid drops still go through the wiring rules."""
    for typ, x, y in (("GridConnection", 80, 120), ("Demand", 640, 120), ("Demand", 640, 360)):
        page.drag_and_drop(f'.pal-item[data-type="{typ}"]', "#drawflow", target_position={"x": x, "y": y})
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 640, "y": 560})
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 450, "y": 330})
    idx = pd.date_range("2025-01-01", periods=48, freq="1h")
    csv = tmp_path / "in.csv"
    pd.DataFrame({"price": 50.0, "load": 0.5}, index=idx).rename_axis("time").to_csv(csv)
    page.set_input_files("#file-upload", str(csv))
    page.wait_for_selector('.ds-col[data-col="load"]')
    page.drag_and_drop('.ds-col[data-col="price"]', "#drawflow", target_position={"x": 80, "y": 420})
    page.drag_and_drop('.ds-col[data-col="load"]', "#drawflow", target_position={"x": 450, "y": 600})
    nid = page.evaluate("""() => { const o = {}; for (const n of allNodes())
        o[n.data.name || n.data.column] = n.id; return o; }""")
    node = lambda name: f"#node-{nid[name]}"            # noqa: E731
    head = lambda name: f"#node-{nid[name]} .blk-head"  # noqa: E731

    def conn(name, side, cname):
        i = page.evaluate(f"connectorIndex(Blocks.editor.getNodeFromId({nid[name]}).data, '{side}', '{cname}')")
        return f"{node(name)} .{'input' if side == 'in' else 'output'}_{i + 1}"

    _drag(page, conn("demand", "out", "in"), head("el"))                # component port -> bus body
    _drag(page, conn("grid", "in", "import_price"), head("price"))      # parameter -> input block body
    _drag(page, conn("grid", "out", "link"), _edge(page, conn("el", "in", "port"), "left"))  # port -> bus edge
    _drag(page, _edge(page, conn("el", "in", "port"), "bottom"), head("battery"))  # bus edge -> body: picks '⇄'
    _drag(page, _edge(page, conn("el", "in", "port"), "right"), conn("demand2", "out", "in"))  # bus edge -> port
    _drag(page, conn("load", "out", "load"), head("demand2"))           # input -> only free parameter (profile)
    _drag(page, conn("demand", "in", "profile"), head("el"))            # invalid: parameter <- bus, rejected

    edges = {(e["from"][1], e["to"][1]) for e in page.evaluate("edgesOf(Blocks.editor.export())")}
    assert edges == {("in", "port"), ("price", "import_price"), ("link", "port"), ("load", "profile")}
    assert len(page.evaluate("edgesOf(Blocks.editor.export())")) == 6   # both demands and grid + battery on el
    assert page.locator(".wiring-temp").count() == 0 and page.locator(".drop-target").count() == 0
    assert not page.errors, page.errors


def test_bus_takes_wires_all_around_its_outline(page):
    """A bus is connectable on its whole outline: each wire ends on the bus edge facing the block it comes from (top,
    bottom, left, right), dragging the bus's middle still moves it, and moving it keeps the wires on its outline."""
    page.evaluate("""() => {
      const el = Blocks.addBus(500, 300);
      const at = [["Producer", 520, 40], ["Demand", 520, 560], ["Demand", 120, 300], ["Demand", 900, 300]];
      for (const [t, x, y] of at) { const n = Blocks.addComponent(x, y, t);
        connectByName([String(n), t === "Producer" ? "out" : "in"], [String(el), "port"]); }
    }""")

    def ends():
        """(x, y) of each wire end relative to the bus box, in canvas units, by source node name."""
        return page.evaluate("""() => {
          const bus = allNodes().find(n => n.data.kind === "bus"), el = document.getElementById("node-" + bus.id);
          const r = el.getBoundingClientRect(), a = Wiring.toCanvas(r.x, r.y), z = Blocks.editor.zoom;
          const out = {};
          for (const svg of document.querySelectorAll(`.connection.node_in_node-${bus.id}`)) {
            const src = [...svg.classList].find(c => c.startsWith("node_out_node-")).slice(14);
            const d = svg.querySelector(".main-path").getAttribute("d").trim().split(/[ ,]+/);
            out[Blocks.editor.getNodeFromId(src).data.name] = [(+d.at(-2) - a.x) / (r.width / z), (+d.at(-1) - a.y) / (r.height / z)];
          }
          return out; }""")

    def side(x, y):
        return min((x, "left"), (1 - x, "right"), (y, "top"), (1 - y, "bottom"))

    def check(e):
        assert all(side(*p)[0] < 0.1 for p in e.values()), e         # every wire ends on the outline
        assert {n: side(*p)[1] for n, p in e.items()} == {"prod": "top", "demand": "bottom", "demand2": "left",
                                                          "demand3": "right"}, e
    check(ends())
    bus = page.locator(".bus-node .blk-title")
    before = page.evaluate("allNodes().find(n => n.data.kind === 'bus').pos_y")
    _drag(page, ".bus-node .blk-title", (bus.bounding_box()["x"] + 10, bus.bounding_box()["y"] + 40))
    assert page.evaluate("allNodes().find(n => n.data.kind === 'bus').pos_y") > before + 20   # the middle moves it
    check(ends())
    assert len(page.evaluate("edgesOf(Blocks.editor.export())")) == 4
    assert not page.errors, page.errors


def test_wire_snaps_to_the_bus_point_it_is_dropped_on(page):
    """Pulling a wire over a bus shows snap points around its outline; the wire ends on the one it is dropped on (here
    the far side of the bus, not the side facing the block), keeps it when the bus moves, and saves it."""
    ids = page.evaluate("""() => { const bus = Blocks.addBus(500, 300), dem = Blocks.addComponent(120, 300, "Demand");
        return [String(bus), String(dem)]; }""")
    port = page.evaluate(f"connectorIndex(Blocks.editor.getNodeFromId({ids[1]}).data, 'out', 'in')")
    b = page.locator(f"#node-{ids[0]}").bounding_box()
    o = page.locator(f"#node-{ids[1]} .output_{port + 1}").bounding_box()
    page.mouse.move(o["x"] + o["width"] / 2, o["y"] + o["height"] / 2)
    page.mouse.down()
    page.mouse.move(b["x"] + b["width"] - 3, b["y"] + b["height"] / 2, steps=10)   # the right end, away from demand
    assert page.locator(".bus-anchors i").count() == 12 and page.locator(".bus-anchors i.on").count() == 1
    page.mouse.up()
    assert page.locator(".bus-anchors").count() == 0
    assert page.evaluate(f"Blocks.editor.getNodeFromId({ids[0]}).data.anchors") == {f"{ids[1]}_output_{port + 1}": "r"}

    def end_x():
        d = page.locator(f".connection.node_in_node-{ids[0]} .main-path").get_attribute("d").split()
        r = page.locator(f"#node-{ids[0]}").bounding_box()
        z = page.evaluate("Blocks.editor.zoom")
        a = page.evaluate(f"Wiring.toCanvas({r['x']}, {r['y']})")
        return (float(d[-2]) - a["x"]) / (r["width"] / z)
    assert end_x() > 0.95
    _drag(page, f"#node-{ids[0]} .blk-title", (b["x"] + 100, b["y"] + 60))
    assert end_x() > 0.95
    page.evaluate(f"Blocks.editor.removeSingleConnection({ids[1]}, {ids[0]}, 'output_{port + 1}', 'input_1')")
    assert page.evaluate(f"Blocks.editor.getNodeFromId({ids[0]}).data.anchors") == {}
    assert not page.errors, page.errors


def test_old_supply_withdraw_project_is_converted_on_load(page):
    """A project saved with bus supply/withdraw connectors (and split grid import/export wires) opens with one bus
    port: every energy wire runs component port -> bus port, split ports become the one ⇄ link wire."""
    bus = {"kind": "bus", "name": "el", "carrier": "electricity", "carrier_kind": "electricity",
           "ins": [{"name": "supply", "type": "bus", "label": "supply"}],
           "outs": [{"name": "withdraw", "type": "bus", "label": "withdraw"}]}
    grid = {"kind": "component", "type": "GridConnection", "name": "dso", "params": {},
            "ins": [{"name": "export", "type": "energy", "label": "export"}],
            "outs": [{"name": "import", "type": "energy", "label": "import"}]}
    dem = {"kind": "component", "type": "Demand", "name": "load", "params": {"profile": 1},
           "ins": [{"name": "in", "type": "energy", "label": "in"}, {"name": "profile", "type": "data", "label": "profile"}],
           "outs": []}
    node = lambda i, d, ins, outs, x: {"id": i, "name": d.get("type", "bus"), "data": d, "class": "", "html": "",  # noqa: E731
                                       "typenode": False, "pos_x": x, "pos_y": 100, "inputs": ins, "outputs": outs}
    data = {
        "1": node(1, bus, {"input_1": {"connections": [{"node": "2", "input": "output_1"}]}},
                  {"output_1": {"connections": [{"node": "2", "output": "input_1"}, {"node": "3", "output": "input_1"}]}}, 400),
        "2": node(2, grid, {"input_1": {"connections": [{"node": "1", "input": "output_1"}]}},
                  {"output_1": {"connections": [{"node": "1", "output": "input_1"}]}}, 100),
        "3": node(3, dem, {"input_1": {"connections": [{"node": "1", "input": "output_1"}]}, "input_2": {"connections": []}},
                  {}, 700)}
    page.evaluate("p => App.load(p)", {"settings": {"name": "old"}, "drawflow": {"drawflow": {"Home": {"data": data}}}})
    edges = page.evaluate("edgesOf(Blocks.editor.export())")
    assert sorted((e["from"][0], e["from"][1], e["to"][0], e["to"][1]) for e in edges) == [
        ("2", "link", "1", "port"), ("3", "in", "1", "port")]
    assert page.evaluate("Blocks.editor.getNodeFromId(1).data.outs.length") == 0
    assert page.locator("#node-1 .output").count() == 0 and page.locator("#node-3 .outputs .output").count() == 1
    assert not page.errors, page.errors


def test_db_connection_and_db_input(page):
    """DB connection block: enter host/user/password, Connect, pick the database from the dropdown. DB input block:
    choose zone and series from the database catalogue, preview, wire to the grid price and run. The password is
    never part of the saved project."""
    use_fake_bess_db(APPS["app"])
    _tab(page, "inputs")
    page.drag_and_drop(".pal-item.dbconn", "#drawflow", target_position={"x": 80, "y": 40})
    page.wait_for_selector('#panel input[data-c="host"]')
    page.fill('#panel input[data-c="host"]', "db.example.com")
    page.dispatch_event('#panel input[data-c="host"]', "change")
    page.fill('#panel input[data-c="user"]', "tester")
    page.dispatch_event('#panel input[data-c="user"]', "change")
    page.fill("#dbc-pw", "hunter2")
    page.click("#dbc-connect")
    page.wait_for_selector('#panel select[data-c="database"]:not([disabled])')
    assert page.locator('#panel select[data-c="database"] option').all_inner_texts() == ["postgres", "staging"]
    page.select_option('#panel select[data-c="database"]', "staging")
    assert page.evaluate("Blocks.dbConnections()[0].data.database") == "staging"

    _tab(page, "blocks")
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 500, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 260, "y": 200})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 760, "y": 300})
    _tab(page, "inputs")
    page.drag_and_drop(".pal-item.db", "#drawflow", target_position={"x": 80, "y": 220})
    page.wait_for_selector('#panel select[data-db="zone"]')                 # connection chosen automatically
    page.select_option('#panel select[data-db="zone"]', "DK_1")
    page.wait_for_selector('#panel select[data-db="series"]')
    page.select_option('#panel select[data-db="series"]', "da_price")
    _set(page, "#set-days", "2")
    page.click("#db-fetch")
    page.wait_for_selector("#db-preview .data-preview")
    assert "48 steps" in page.inner_text("#db-preview")

    page.evaluate("""() => {
        const id = k => String(allNodes().find(n => k(n.data)).id);
        const bus = id(d => d.kind === "bus"), grid = id(d => d.type === "GridConnection"),
              dem = id(d => d.type === "Demand"), db = id(d => d.kind === "db");
        connectByName([grid, "link"], [bus, "port"]);
        connectByName([dem, "in"], [bus, "port"]);
        connectByName([db, "value"], [grid, "import_price"]);
        const n = Blocks.editor.getNodeFromId(dem); n.data.params.profile = 0.5; Blocks.editor.updateNodeDataFromId(dem, n.data);
    }""")
    _start_run(page)
    page.wait_for_function("Results.summary !== null", timeout=60000)
    assert "hunter2" not in page.evaluate("JSON.stringify(Blocks.editor.export())")
    assert not page.errors, page.errors


def test_pv_profile_from_db_feeds_pv_cf(page):
    """The palette's '☀️ PV profile' item places a DB input on the database's first modelled PV system (using the
    default .env login); wired to a PV block's cf it shows up as that block's capacity-factor input."""
    use_fake_bess_db(APPS["app"])
    page.evaluate("App.dbOptionsCache = {}; App.dbDefault = {host: 'db.example.com', port: 5432, user: 'tester',"
                  " database: 'postgres', sslmode: 'require'}")
    _tab(page, "inputs")
    page.drag_and_drop('.pal-item[data-preset="solar"]', "#drawflow", target_position={"x": 80, "y": 200})
    page.wait_for_selector('#panel select[data-db="scenario"]')
    d = page.evaluate("allNodes().find(n => n.data.kind === 'db').data")
    assert (d["source"], d["scenario"]) == ("solar", "SITE")
    assert page.inner_text(".db-node .blk-title") == "PV SITE"
    assert "0…1 capacity factor" in page.inner_text("#panel")
    _tab(page, "blocks")
    page.drag_and_drop('.pal-item[data-preset="pv"]', "#drawflow", target_position={"x": 400, "y": 200})
    page.evaluate("""() => { const id = k => String(allNodes().find(n => k(n.data)).id);
        connectByName([id(d => d.kind === "db"), "value"], [id(d => d.preset === "pv"), "cf"]); }""")
    assert page.evaluate("Object.values(wiredParams(allNodes().find(n => n.data.preset === 'pv').id))") == ["DB PV SITE"]
    assert not page.errors, page.errors


def test_daily_profile_block_is_drawn_typed_and_feeds_a_demand(page):
    """The palette's '🕒 Daily profile' places a 24-value block; dragging across its chart sets values, a table cell
    types one, switching to 96 values per day repeats each hour four times, and pasted values replace the day. Wired
    to a demand it shows as that demand's profile input and the run uses it."""
    _tab(page, "inputs")
    page.evaluate("Palette.show('inputs')")
    page.drag_and_drop('.pal-item[data-kind="profile"]', "#drawflow", target_position={"x": 80, "y": 200})
    page.evaluate("Palette.show('blocks')")
    page.click(".profile-node .blk-head")                      # selecting a block opens its properties
    page.wait_for_selector("#panel .prof-chart")
    prof = "allNodes().find(n => n.data.kind === 'profile').data"
    assert page.evaluate(f"{prof}.values") == [0] * 24
    box = page.locator("#panel .prof-chart").bounding_box()
    y = box["y"] + box["height"] * 0.3
    page.mouse.move(box["x"] + 2, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, y, steps=8)
    page.mouse.up()
    drawn = page.evaluate(f"{prof}.values")
    assert all(v > 0 for v in drawn[:11]) and all(v == 0 for v in drawn[13:])
    page.fill('#panel .prof-table input[data-i="18"]', "2.5")
    page.press('#panel .prof-table input[data-i="18"]', "Tab")
    assert page.evaluate(f"{prof}.values[18]") == 2.5
    page.select_option('#panel select[data-k="steps"]', "96")
    v96 = page.evaluate(f"{prof}.values")
    assert len(v96) == 96 and v96[72:76] == [2.5] * 4
    day = [0.5] * 8 + [1.5] * 10 + [0.5] * 6
    page.click("#panel .prof-paste summary")
    page.fill('#panel textarea[data-tool="paste"]', "\n".join(map(str, day)))
    page.click('#panel [data-act="paste"]')
    assert page.evaluate(f"{prof}.values") == day
    assert "0.50–1.50 MW" in page.inner_text(".profile-node .data-src")

    _tab(page, "blocks")
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 500, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 300, "y": 120})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 300, "y": 420})
    page.evaluate("""() => { const id = k => String(allNodes().find(n => k(n.data)).id);
        connectByName([id(d => d.type === "GridConnection"), "link"], [id(d => d.kind === "bus"), "port"]);
        connectByName([id(d => d.type === "Demand"), "in"], [id(d => d.kind === "bus"), "port"]);
        connectByName([id(d => d.kind === "profile"), "value"], [id(d => d.type === "Demand"), "profile"]); }""")
    assert page.evaluate("Object.values(wiredParams(allNodes().find(n => n.data.type === 'Demand').id))") == \
        ["🕒 profile (daily profile)"]
    page.evaluate("""() => { const g = allNodes().find(n => n.data.type === "GridConnection");
        g.data.params.import_price = 50; Blocks.editor.updateNodeDataFromId(g.id, g.data); }""")
    _set(page, "#set-days", "2")
    _start_run(page)
    page.wait_for_function("Results.summary !== null", timeout=60000)
    demand = next(e for e in page.evaluate("Results.summary.edges") if e["component"] == "demand")
    assert demand["MWh"] == pytest.approx(2 * sum(day))
    assert not page.errors, page.errors


def test_blocks_and_palette_tiles_explain_themselves_on_hover(page):
    """Every block tile in the side bar and every block on the canvas has a tooltip saying what it does: presets
    their own text, plain components their type's, inputs and settings blocks theirs; a placed block shows the same
    text as its tile."""
    tiles = page.evaluate("[...document.querySelectorAll('#palette .pal-item.tile[data-kind]')].map(t => [t.dataset.kind, t.title])")
    assert tiles and all(title for _, title in tiles), [k for k, t in tiles if not t]
    assert page.get_attribute('.pal-item[data-preset="battery"]', "title").startswith("Battery: stores electricity")
    page.evaluate("Palette.show('inputs')")
    page.drag_and_drop('.pal-item[data-kind="profile"]', "#drawflow", target_position={"x": 150, "y": 420})
    page.evaluate("Palette.show('blocks')")
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 150, "y": 200})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 450, "y": 200})
    page.drag_and_drop('.pal-item[data-kind="invest"]', "#drawflow", target_position={"x": 450, "y": 420})
    hints = page.evaluate("allNodes().map(n => [n.data.kind, n.data.preset || n.data.type || '', "
                          "document.getElementById('node-' + n.id).title])")
    by = {(k, t): h for k, t, h in hints}
    assert by[("component", "battery")].startswith("Battery: stores electricity")
    assert by[("component", "Demand")].startswith("Demand: a fixed load")
    assert by[("profile", "")].startswith("Daily profile:") and by[("invest", "")].startswith("Run settings:")
    tile = page.get_attribute('.pal-item[data-kind="profile"]', "title")
    assert tile.startswith(by[("profile", "")])
    assert not page.errors, page.errors


def test_renamed_block_keeps_its_name_after_reopening(page):
    """Renaming a block in its panel changes its title on the canvas, and the title survives export / import (saving
    and reopening a project); a project saved while renames were lost shows each block's name again."""
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 200, "y": 200})
    page.click(".drawflow-node .blk-head")
    page.fill('#panel input[data-k="name"]', "BESS")
    page.press('#panel input[data-k="name"]', "Tab")
    assert page.inner_text(".drawflow-node .blk-title") == "BESS"
    page.evaluate("const e = Blocks.editor.export(); Blocks.editor.clear(); Blocks.editor.import(e); "
                  "for (const n of allNodes()) Blocks.decorate(n.id);")
    assert page.inner_text(".drawflow-node .blk-title") == "BESS"
    page.evaluate("""() => { const e = Blocks.editor.export(); const n = Object.values(e.drawflow.Home.data)[0];
        n.html = n.html.replace('>BESS<', '>store2<'); Blocks.editor.clear(); Blocks.editor.import(e);
        for (const x of allNodes()) Blocks.decorate(x.id); }""")
    assert page.inner_text(".drawflow-node .blk-title") == "BESS"
    assert not page.errors, page.errors


def test_note_block_holds_text(page):
    """A Note block (Blocks tab, Notes) holds free text typed on the canvas: typing marks the project unsaved and
    does not delete the block, and the text and the note's size come back when the project is reopened."""
    page.drag_and_drop(".pal-item.note", "#drawflow", target_position={"x": 300, "y": 200})
    note = ".drawflow-node.note-node textarea"
    page.click(note)
    page.keyboard.type("Tariff from 2025 - check")
    page.keyboard.press("Backspace")
    page.keyboard.press("Delete")
    assert page.input_value(note) == "Tariff from 2025 - chec"
    assert page.evaluate("allNodes()[0].data.text") == "Tariff from 2025 - chec"
    assert page.evaluate("App.edits !== App.savedEdits")
    page.evaluate("""() => { const t = document.querySelector('.note-node textarea'); t.style.width = '300px';
        t.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })); }""")
    assert page.evaluate("allNodes()[0].data.size[0]") == 300
    page.evaluate("App.load(App.project(), true)")
    assert page.input_value(note) == "Tariff from 2025 - chec"
    assert page.evaluate("document.querySelector('.note-node textarea').offsetWidth") == 300
    node = page.locator(".drawflow-node.note-node")
    box = node.bounding_box()
    t = page.locator(note).bounding_box()                                  # dragging the text box's corner resizes it
    page.mouse.move(t["x"] + t["width"] - 3, t["y"] + t["height"] - 3)
    page.mouse.down()
    page.mouse.move(t["x"] + t["width"] + 60, t["y"] + t["height"] + 40, steps=5)
    page.mouse.up()
    assert node.bounding_box()["x"] == box["x"] and node.bounding_box()["y"] == box["y"]   # the note stays put
    head = page.locator(".note-node .blk-head").bounding_box()            # moved by its header
    page.mouse.move(head["x"] + 40, head["y"] + head["height"] / 2)
    page.mouse.down()
    page.mouse.move(head["x"] + 140, head["y"] + 80, steps=5)
    page.mouse.up()
    assert node.bounding_box()["x"] > box["x"] + 50
    assert "Note" in page.inner_text("#panel")
    assert not page.errors, page.errors


def test_start_hint_shows_on_an_empty_canvas_until_a_project_is_opened(page):
    """The empty canvas shows how to start; opening a project or placing a first block removes the hint."""
    assert "Drag a Bus onto the canvas" in page.inner_text("#status")
    page.evaluate("App.load({settings: {}, drawflow: {drawflow: {Home: {data: {}}}}})")
    assert page.inner_text("#status") == ""
    page.evaluate("App.status(START_HINT)")
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 400, "y": 300})
    assert page.inner_text("#status") == ""
    assert not page.errors, page.errors


def _switch(page, item):
    """Open the project switcher (the project name in the top bar) and click one of its items."""
    page.click("#proj-switch")
    page.click(item)


def test_saved_cases_window_with_login(page):
    """All projects (in the project switcher) asks for a login, then lists only that user's cases
    in a window: open, rename and delete them. A dot after the project name marks unsaved changes."""
    _tab(page, "project")
    _switch(page, "#btn-open")
    page.wait_for_selector("#login-dialog[open]")
    page.fill("#login-user", "casey")
    page.fill("#login-pw", "secret1")
    page.click("#login-register")
    page.wait_for_selector("#proj-dialog[open]")
    assert page.get_attribute("#btn-account", "title") == "casey"
    assert page.inner_text("#btn-account") == "C"
    assert "No saved cases yet" in page.inner_text("#proj-list")
    page.click("#proj-close")

    assert page.locator("#save-dot").is_hidden()
    page.evaluate("Blocks.addBus(100, 100); App.dirty()")
    assert page.inner_text("#save-dot") == "Unsaved changes"
    page.once("dialog", lambda d: d.accept("plant"))
    _switch(page, "#pm-rename")
    page.click("#btn-save")
    page.wait_for_function("document.getElementById('toast').textContent.includes(\"Saved project 'plant'\")")
    assert page.locator("#save-dot").is_hidden()
    page.evaluate("Blocks.editor.clear(); Panel.clear(); Results.summary = null; Results.updateBar()")
    assert page.evaluate("allNodes().length") == 0

    _switch(page, "#btn-open")
    page.wait_for_selector('#proj-list tr:has-text("plant")')
    page.once("dialog", lambda d: d.accept("plant v2"))
    page.click('#proj-list tr:has-text("plant") button[data-act="rename"]')
    page.wait_for_selector('#proj-list tr:has-text("plant v2")')
    page.click('#proj-list tr:has-text("plant v2") button[data-act="open"]')
    page.wait_for_selector("#proj-dialog:not([open])", state="attached")
    assert page.evaluate("allNodes().length") == 1
    assert page.inner_text("#set-name") == "plant v2"

    _switch(page, "#btn-open")
    page.once("dialog", lambda d: d.accept())
    page.click('#proj-list tr:has-text("plant v2") button[data-act="delete"]')
    page.wait_for_selector("#proj-list >> text=No saved cases yet")
    page.click("#proj-close")

    page.click("#btn-account")
    page.click("#btn-logout")
    page.wait_for_selector("#btn-login")
    _switch(page, "#btn-open")                 # logged out: the login dialog again; closing it cancels
    page.wait_for_selector("#login-dialog[open]")
    page.keyboard.press("Escape")
    assert not page.evaluate("document.getElementById('proj-dialog').open")
    assert page.errors == []


def test_battery_cycle_limit_in_panel(page):
    """The battery block's panel has a Cycling section: Max cycles (empty = no limit) per day/week/month/year."""
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 300, "y": 300})
    page.click(".drawflow-node .blk-head")
    box = page.locator("#panel fieldset:has(legend:text-is('Cycling'))")
    assert box.count() == 1
    assert box.locator('input[data-p="max_cycles"]').get_attribute("placeholder") == "off"
    box.locator('input[data-p="max_cycles"]').fill("1.5")
    box.locator('input[data-p="max_cycles"]').dispatch_event("change")
    box.locator('select[data-p="cycle_period"]').select_option("W")
    params = page.evaluate("allNodes()[0].data.params")
    assert params["max_cycles"] == 1.5 and params["cycle_period"] == "W"
    if os.environ.get("OEMS_SHOT"):
        page.screenshot(path=os.environ["OEMS_SHOT"])
    assert not page.errors, page.errors


def test_pv_curtailment_tick_box(page):
    """The PV block has an 'Allow curtailment' tick box, ticked by default; unticking it sets curtailable=false."""
    page.drag_and_drop('.pal-item[data-preset="pv"]', "#drawflow", target_position={"x": 300, "y": 300})
    page.click(".drawflow-node .blk-head")
    box = page.locator('#panel input[data-p="curtailable"]')
    assert box.count() == 1 and box.is_checked()
    assert page.locator("#panel label", has_text="Allow curtailment").count() == 1
    box.uncheck()
    box.dispatch_event("change")
    assert page.evaluate("allNodes()[0].data.params.curtailable") is False
    assert not page.errors, page.errors


def test_base_period_years_and_year_selector(page):
    """Run settings live in the ⚙️ Run settings block: ▶ Run without one adds it and shows its settings instead of
    running; a calendar base year fixes start / days (366 for 2024) and hides them; the multi-year mode shows years
    and the discount rate, and the block on the canvas sums up the run. A '📉 Degradation curve' block (the database's first profile) wires only to a battery's SoH curves; a
    3-year run gets a year selector that switches the summary, the battery's SoH badge and Inspect."""
    use_fake_bess_db(APPS["app"])
    page.click("#btn-run")
    page.wait_for_selector("#panel #run-settings")                      # no block yet: added and shown, no run
    assert page.evaluate("!!Blocks.investNode()") and page.is_hidden("#run-progress")
    assert page.is_hidden("#lbl-years")                                  # single period: no years
    page.select_option("#set-base", "2024")
    assert page.input_value("#set-start") == "2024-01-01" and page.input_value("#set-days") == "366"
    assert page.is_hidden("#set-days") and page.is_hidden('#panel [data-k="discount_rate"]')
    assert "2024 · 1h" in page.inner_text(".invest-node .invest-list")
    page.select_option("#set-base", "")
    assert page.is_visible("#set-days")
    page.fill("#set-days", "2")
    page.check('input[name="run-mode"][value="multi_year"]')
    page.fill("#set-years", "3")
    page.dispatch_event("#set-years", "change")
    assert page.is_visible('#panel [data-k="discount_rate"]')
    page.fill('#panel [data-k="discount_rate"]', "4")
    page.dispatch_event('#panel [data-k="discount_rate"]', "change")
    assert page.evaluate("App.settings().sim_years") == 3 and page.evaluate("App.settings().discount_rate") == 4
    assert "Multi-year" in page.inner_text(".invest-node .invest-list")
    assert page.is_disabled('input[name="run-mode"][value="optimise"]')  # no variable-size PV or storage

    page.evaluate("App.dbOptionsCache = {}; Blocks.addDbConn(40, 40, {host: 'db.example.com', port: 5432, "
                  "user: 'tester', database: 'postgres', sslmode: 'require'})")
    _tab(page, "inputs")
    page.drag_and_drop('.pal-item[data-preset="degradation"]', "#drawflow", target_position={"x": 80, "y": 260})
    page.wait_for_selector('#panel select[data-db="profile"]')
    assert page.evaluate("allNodes().find(n => n.data.source === 'degradation').data.profile") == "short"
    assert page.inner_text(".db-node .blk-title") == "SoH short"
    page.click("#db-fetch")
    page.wait_for_selector("#db-preview table")
    assert "end of life 80.0 %" in page.inner_text("#db-preview")
    _tab(page, "blocks")
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 500, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 260, "y": 120})
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 760, "y": 160})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 760, "y": 420})
    page.evaluate("""() => {
        const id = k => String(allNodes().find(n => k(n.data)).id);
        const bus = id(d => d.kind === "bus"), grid = id(d => d.type === "GridConnection"),
              bat = id(d => d.type === "Storage"), dem = id(d => d.type === "Demand"), deg = id(d => d.source === "degradation");
        connectByName([grid, "link"], [bus, "port"]);
        connectByName([bat, "link"], [bus, "port"]);
        connectByName([dem, "in"], [bus, "port"]);
        connectByName([deg, "value"], [grid, "import_price"], true);       // refused: curves are not a time series
        connectByName([deg, "value"], [bat, "degradation"]);
        const n = Blocks.editor.getNodeFromId(dem); n.data.params.profile = 0.5; Blocks.editor.updateNodeDataFromId(dem, n.data);
    }""")
    wires = page.evaluate("edgesOf(Blocks.editor.export()).map(e => e.to[1])")
    assert "degradation" in wires and "import_price" not in wires
    polls = []

    def slow_status(route):            # the first status polls report year 3 of 20, so the banner can be seen
        polls.append(1)
        if len(polls) <= 2:
            route.fulfill(json={"status": "solving", "year": 3, "years": 20, "elapsed": 12.0, "errors": {},
                                "message": "year 3 of 20 (battery SoH 90.2%)"})
        else:
            route.continue_()
    page.route("**/api/runs/*", slow_status)
    _start_run(page)                                                    # the multi-year mode is kept
    page.wait_for_selector("#run-progress:not([hidden])")
    page.wait_for_function("document.getElementById('rp-text').textContent.startsWith('Year 3 of 20')")
    assert "battery SoH 90.2%" in page.inner_text("#rp-text") and page.inner_text("#btn-run").startswith("⏳")
    if os.environ.get("OEMS_SHOT"):
        page.screenshot(path=os.environ["OEMS_SHOT"].replace(".png", "_running.png"))
    page.wait_for_function("Results.summary !== null", timeout=60000)
    page.unroute("**/api/runs/*")
    assert page.is_hidden("#run-progress") and page.inner_text("#btn-run").strip().endswith("Run")
    assert page.is_visible("#res-year-box") and page.locator("#res-year option").count() == 3
    page.select_option("#res-year", "2")
    page.wait_for_function("Results.year === 2 && Results.summary.year === 2")
    assert page.input_value("#res-year") == "2"
    bat = page.evaluate("allNodes().find(n => n.data.type === 'Storage').id")
    soh = page.evaluate("Results.summary.lifetime.rows[1][Results.summary.lifetime.columns.indexOf('soh_start')]")
    assert f"SoH {100 * soh:.0f} %" in page.inner_text(f"#node-{bat} .blk-badges")
    page.click("#res-inspect")
    page.wait_for_selector("#ins-year")
    assert page.input_value("#ins-year") == "2"
    assert page.get_attribute("#ins-all-years", "href").endswith("year=all")
    if os.environ.get("OEMS_SHOT"):
        page.screenshot(path=os.environ["OEMS_SHOT"].replace(".png", "_inspect.png"))
        page.click("#ins-close")
        page.screenshot(path=os.environ["OEMS_SHOT"])
    assert not page.errors, page.errors


def test_project_save_keeps_the_results(page):
    """Save stores the results on show with the project (as a saved simulation); opening the project shows them
    again without running."""
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 500, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 260, "y": 200})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 760, "y": 300})
    page.evaluate("""() => {
        const id = k => String(allNodes().find(n => k(n.data)).id);
        const bus = id(d => d.kind === "bus"), grid = id(d => d.type === "GridConnection"), dem = id(d => d.type === "Demand");
        connectByName([grid, "link"], [bus, "port"]);
        connectByName([dem, "in"], [bus, "port"]);
        const g = Blocks.editor.getNodeFromId(grid); g.data.params.import_price = 50; Blocks.editor.updateNodeDataFromId(grid, g.data);
        const n = Blocks.editor.getNodeFromId(dem); n.data.params.profile = 0.5; Blocks.editor.updateNodeDataFromId(dem, n.data);
    }""")
    _set(page, "#set-days", "1")
    page.once("dialog", lambda d: d.accept("kept"))
    _switch(page, "#pm-rename")
    _start_run(page)
    page.wait_for_function("Results.summary !== null", timeout=60000)
    cost = page.evaluate("Results.summary.total_cost")
    page.fill("#res-notes-text", "peak shaving looks worth it")         # comments on the analysis go with the save
    page.keyboard.press("Control+s")                                    # ⌘S / Ctrl+S saves
    page.wait_for_selector("#login-dialog[open]")
    page.fill("#login-user", "keeper")
    page.fill("#login-pw", "secret1")
    page.click("#login-register")
    page.wait_for_function("document.getElementById('toast').textContent.includes(\"Saved project 'kept' with its results\")")
    page.evaluate("Blocks.editor.clear(); Panel.clear(); Results.summary = null; Results.updateBar()")
    assert page.evaluate("Results.summary") is None

    _tab(page, "project")
    _switch(page, "#btn-open")
    page.wait_for_selector('#proj-list tr:has-text("kept")')
    page.click('#proj-list tr:has-text("kept") button[data-act="open"]')
    page.wait_for_function("Results.summary !== null && Results.saved !== null")
    assert page.evaluate("Results.summary.total_cost") == cost == 0.5 * 24 * 50
    assert page.inner_text("#res-name") == "kept"                       # named after the project
    assert page.input_value("#res-notes-text") == "peak shaving looks worth it"
    page.fill("#res-notes-text", "edited")                              # comments on a saved run are kept as typed
    page.wait_for_function("document.getElementById('res-notes-state').textContent.includes('saved')")
    assert page.evaluate("Results.saved.notes") == "edited"
    _tab(page, "project")                                               # listed under its project
    page.wait_for_selector('#proj-tree .pt-row[data-name="kept"] + .pt-run.on:has-text("Results")')
    first = page.evaluate("Results.saved.id")
    _start_run(page)                                                    # every run replaces the project's results
    page.wait_for_function(f"Results.saved && Results.saved.id !== '{first}'", timeout=60000)
    page.wait_for_function("Simulations.list.filter(r => r.project_name === 'kept').length === 1")
    assert page.evaluate("ProjectTree.list.length") == 1
    assert page.evaluate("api('/api/projects/kept').then(p => p.results === Results.saved.id)")   # reopened with it
    page.once("dialog", lambda d: d.accept())                           # 🗑 next to the results deletes them
    page.click('#proj-tree .pt-run:has-text("Results") [data-act=delete]')
    page.wait_for_selector("#proj-tree .pt-run", state="detached")
    assert not page.errors, page.errors


def test_investment_block_and_lifetime_optimisation(page):
    """A ⚙️ Run settings block (not wired) holds the CO₂ cap; switching the battery to Variable size (in its own panel)
    shows its size limits and CAPEX / OPEX there, marks its size fields and lists it in the Run settings block.
    ▶ Run preselects Lifetime optimisation, and Start stays disabled until CAPEX and OPEX are entered. The run shows
    a Sizing tab with the NPV, IRR (against its minimum-IRR hurdle) and payback, and the optimal size on the battery
    block."""
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 500, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 260, "y": 120})
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 760, "y": 160})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 760, "y": 420})
    page.evaluate("""() => {
        const id = k => String(allNodes().find(n => k(n.data)).id);
        const bus = id(d => d.kind === "bus"), grid = id(d => d.type === "GridConnection"),
              bat = id(d => d.type === "Storage"), dem = id(d => d.type === "Demand");
        connectByName([grid, "link"], [bus, "port"]);
        connectByName([bat, "link"], [bus, "port"]);
        connectByName([dem, "in"], [bus, "port"]);
        const g = Blocks.editor.getNodeFromId(grid); g.data.params.import_price = 50; Blocks.editor.updateNodeDataFromId(grid, g.data);
        const n = Blocks.editor.getNodeFromId(dem); n.data.params.profile = 0.5; Blocks.editor.updateNodeDataFromId(dem, n.data);
    }""")
    _set(page, "#set-days", "1")
    page.drag_and_drop(".pal-item.invest", "#drawflow", target_position={"x": 80, "y": 420})
    page.wait_for_selector('#panel [data-k="co2_cap"]')
    page.fill('#panel [data-k="co2_cap"]', "1000")
    page.dispatch_event('#panel [data-k="co2_cap"]', "change")
    assert "CO₂ cap 1,000 t" in page.inner_text(".invest-node .invest-list")
    assert page.is_visible('#panel fieldset:has(legend:text("Targets")) [data-k="co2_cap"]')   # a target of every run
    assert page.is_hidden('#panel [data-k="min_irr"]')                  # the investment targets: lifetime runs only
    assert page.is_visible("#panel #set-tz") and page.query_selector("#topbar #set-co2cap") is None   # moved off the top bar
    assert page.is_disabled('#panel input[name="run-mode"][value="optimise"]')  # no variable-size PV or storage yet
    bat = page.evaluate("String(allNodes().find(n => n.data.type === 'Storage').id)")
    page.evaluate(f"Panel.show('{bat}')")
    assert page.is_checked('#panel [data-size-mode="fixed"]') and page.query_selector("#panel [data-sz]") is None
    page.click('#panel label:has([data-size-mode="variable"])')
    page.wait_for_selector('#panel [data-sz="capex_energy"]')
    assert "invalid" in page.get_attribute('#panel [data-sz="capex_energy"]', "class")
    assert "variable size" in page.inner_text("#panel")                # its own size fields are not used
    assert "no size optimised" not in page.inner_text(".invest-node .invest-list")
    page.drag_and_drop(".pal-item.invest", "#drawflow", target_position={"x": 80, "y": 80})   # only one per project
    assert page.evaluate("allNodes().filter(n => n.data.kind === 'invest').length") == 1

    page.click("#btn-run")                                              # incomplete: shows the block, no run
    page.wait_for_selector("#panel #run-settings")
    assert page.is_checked('input[name="run-mode"][value="optimise"]') and page.is_hidden("#run-progress")
    assert "missing CAPEX energy" in page.inner_text("#run-info")
    assert page.is_visible("#set-tz")                                    # the time zone is a run setting
    assert page.input_value("#set-lifemodel") == "weighted" and page.is_visible("#lbl-lifemodel")
    page.fill("#set-years", "2")
    page.dispatch_event("#set-years", "change")
    page.evaluate(f"Panel.show('{bat}')")
    for f, v in (("capex_energy", "100000"), ("capex_power", "50000"), ("opex", "1000"), ("e_size_max", "2")):
        page.fill(f'#panel [data-sz="{f}"]', v)
        page.dispatch_event(f'#panel [data-sz="{f}"]', "change")
    inv = page.evaluate("String(Blocks.investNode().id)")
    page.evaluate(f"Panel.show('{inv}')")
    name = page.evaluate(f"Blocks.editor.getNodeFromId('{bat}').data.name")
    assert f"{name}\n" in page.inner_text("#panel")                     # listed as variable size
    page.fill('#panel [data-k="min_irr"]', "5")                         # an investment hurdle
    page.dispatch_event('#panel [data-k="min_irr"]', "change")
    assert "IRR ≥ 5" in page.inner_text(".invest-node .invest-list")

    _start_run(page)
    page.wait_for_function("Results.summary !== null", timeout=60000)
    assert page.is_visible('#res-tabs [data-tab="sizing"]')
    page.wait_for_selector("#sz-kpis .sz-tile")
    assert "NPV of the investment" in page.inner_text("#sz-kpis") and "IRR" in page.inner_text("#sz-kpis")
    assert page.evaluate("Results.summary.sizing.kpis.years") == 2 and page.evaluate("Results.summary.steps") == 24
    assert page.evaluate("Results.summary.sizing.kpis.hurdles[0].kind") == "irr"
    assert "target ≥ 5.0 %" in page.inner_text("#sz-kpis")
    assert "🔍" in page.inner_text(f"#node-{bat} .blk-badges")
    assert not page.errors, page.errors


def test_stop_a_running_run(page):
    """The run shows in the top bar while the canvas stays usable; ■ Stop ends a long run (a year at 15 min): the
    progress closes, the Run button comes back and no results are shown."""
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 500, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 260, "y": 120})
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 760, "y": 160})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 760, "y": 420})
    page.evaluate("""() => {
        const id = k => String(allNodes().find(n => k(n.data)).id);
        const bus = id(d => d.kind === "bus"), grid = id(d => d.type === "GridConnection"),
              bat = id(d => d.type === "Storage"), dem = id(d => d.type === "Demand");
        connectByName([grid, "link"], [bus, "port"]);
        connectByName([bat, "link"], [bus, "port"]);
        connectByName([dem, "in"], [bus, "port"]);
        const g = Blocks.editor.getNodeFromId(grid); g.data.params.import_price = 50; Blocks.editor.updateNodeDataFromId(grid, g.data);
        const n = Blocks.editor.getNodeFromId(dem); n.data.params.profile = 0.5; Blocks.editor.updateNodeDataFromId(dem, n.data);
    }""")
    _set(page, "#set-days", "365")
    _set(page, "#set-freq", "15min")
    _start_run(page, mode="single")
    page.wait_for_selector("#topbar #run-progress:not([hidden])")
    page.click("#drawflow", position={"x": 50, "y": 50})                      # the canvas stays usable
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 300, "y": 500})
    assert page.evaluate("allNodes().filter(n => n.data.kind === 'bus').length") == 2
    page.click("#rp-stop")
    page.wait_for_function("document.getElementById('toast').textContent.includes('Run stopped')", timeout=10000)
    assert page.is_hidden("#run-progress") and page.inner_text("#btn-run").strip().endswith("Run")
    assert page.evaluate("Results.summary") is None
    assert not page.errors, page.errors


def test_project_tree_shows_variants_under_their_project(page):
    """The Project tab shows the open project as a tree: ⑂ Variant saves the canvas as a variant of the open
    project (saving that first) and switches to it; variants nest under their project, the open one is highlighted,
    clicking one opens it, asking first when there are unsaved changes; ▾ folds a project's variants, and ✎ / 🗑 next
    to a row rename / delete it. Only the
    open project's whole family is listed, each with its saved runs (other projects are not); with no project open
    the tab is empty."""
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 400, "y": 300})
    _tab(page, "project")
    assert page.inner_text("#proj-tree") == ""
    page.evaluate("document.getElementById('set-name').textContent = 'plant'; ProjectTree.render()")
    assert "Log in" in page.inner_text("#proj-tree")
    page.click("#pt-variant")
    page.wait_for_selector("#login-dialog[open]")
    page.fill("#login-user", "brancher")
    page.fill("#login-pw", "secret1")
    page.once("dialog", lambda d: d.accept("plant + pv"))
    page.click("#login-register")
    page.wait_for_function("document.getElementById('set-name').textContent === 'plant + pv'")
    page.wait_for_selector('#proj-tree .pt-row[data-name="plant + pv"]')
    rows = lambda: page.eval_on_selector_all(  # noqa: E731
        "#proj-tree .pt-row", "rs => rs.map(r => [r.dataset.name, parseInt(r.style.paddingLeft), r.classList.contains('on')])")
    assert rows() == [["plant", 4, False], ["plant + pv", 18, True]]     # a variant: its parent stays above it
    _switch(page, "#pm-search")                                         # the project list: projects only, no variants
    page.wait_for_selector('#pm-list .pm-proj:has-text("plant")')
    assert page.eval_on_selector_all("#pm-list .pm-proj", "bs => bs.map(b => [b.querySelector('b').textContent, "
                                     "b.classList.contains('on'), b.textContent.includes('1 variant')])") == [["plant", True, True]]
    page.keyboard.press("Escape")
    inv = page.evaluate("Blocks.addInvest(40, 40)")                     # the other variants can be the baseline
    page.evaluate(f"Panel.show({inv})")
    assert page.eval_on_selector_all('#panel [data-k="baseline"] option', "os => os.map(o => o.value)") == ["", "plant"]
    _set(page, '#panel [data-k="baseline"]', "plant")                    # (shown in Lifetime optimisation mode)
    assert page.evaluate("Blocks.investNode().data.baseline") == "plant"
    page.evaluate(f"Blocks.editor.removeNodeId('node-{inv}')")

    _tab(page, "blocks")
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 600, "y": 300})   # an unsaved change
    page.once("dialog", lambda d: d.dismiss())
    _tab(page, "project")
    page.click('#proj-tree .pt-row[data-name="plant"] .pt-name')
    assert page.inner_text("#set-name") == "plant + pv"   # declined: stays
    page.once("dialog", lambda d: d.accept())
    page.click('#proj-tree .pt-row[data-name="plant"] .pt-name')
    page.wait_for_function("document.getElementById('set-name').textContent === 'plant'")
    assert rows() == [["plant", 4, True], ["plant + pv", 18, False]]
    assert page.evaluate("allNodes().length") == 1

    page.click('#proj-tree .pt-row[data-name="plant"] .pt-tog')
    assert [r[0] for r in rows()] == ["plant"]
    page.click('#proj-tree .pt-row[data-name="plant"] .pt-tog')
    assert [r[0] for r in rows()] == ["plant", "plant + pv"]
    assert page.text_content("#pt-title") == "Variants & runs"

    variant = '#proj-tree .pt-row[data-name="plant + pv"]'               # ✎ and 🗑 next to a variant (on hover)
    page.once("dialog", lambda d: d.accept("plant + wind"))
    page.hover(variant)
    page.click(f"{variant} [data-act=rename]")
    page.wait_for_selector('#proj-tree .pt-row[data-name="plant + wind"]')
    assert [r[0] for r in rows()] == ["plant", "plant + wind"]
    page.once("dialog", lambda d: d.accept())
    page.hover('#proj-tree .pt-row[data-name="plant + wind"]')
    page.click('#proj-tree .pt-row[data-name="plant + wind"] [data-act=delete]')
    page.wait_for_function("document.querySelectorAll('#proj-tree .pt-row').length === 1")
    assert rows() == [["plant", 4, True]]

    page.once("dialog", lambda d: d.accept("plant - blank"))             # + : an empty canvas in the same project
    page.click("#pt-blank")
    page.wait_for_selector('#proj-tree .pt-row.on[data-name="plant - blank"]')
    assert rows() == [["plant", 4, False], ["plant - blank", 18, True]]
    assert page.evaluate("allNodes().filter(n => n.data.kind !== 'invest').length") == 0
    assert page.is_hidden("#save-dot")

    page.on("dialog", lambda d: d.accept())
    _switch(page, "#pm-new")                                            # another project, not saved yet: just it
    page.fill("#start-name", "depot")
    page.click("#start-new button[type=submit]")
    assert [r[0] for r in rows()] == [None]
    page.keyboard.press("Control+s")                                    # saved: only its own (variant-less) family
    page.wait_for_selector('#proj-tree .pt-row[data-name="depot"]')
    assert rows() == [["depot", 4, True]]
    assert not page.errors, page.errors


def test_project_switcher(page):
    """The project name in the top bar is a switcher: its menu lists the saved projects, newest first, with the open
    one ticked; typing filters them and Enter opens the first match. Rename… renames the saved project."""
    page.click("#proj-switch")
    assert "Log in" in page.inner_text("#pm-list") and page.is_hidden("#pm-search")
    page.keyboard.press("Escape")
    assert page.is_hidden("#proj-menu")
    for name in ("depot", "yard"):
        page.once("dialog", lambda d: d.accept(name))
        _switch(page, "#pm-rename")                                     # no project open: names the canvas
        page.evaluate("Blocks.addBus(100, 100); App.dirty()")
        page.keyboard.press("Control+s")
        if name == "depot":
            page.wait_for_selector("#login-dialog[open]")
            page.fill("#login-user", "switcher")
            page.fill("#login-pw", "secret1")
            page.click("#login-register")
        page.wait_for_function(f"document.getElementById('toast').textContent.includes(\"Saved project '{name}'\")")
        if name == "depot":
            page.evaluate("document.getElementById('set-name').textContent = ''; Blocks.editor.clear(); App.savedEdits = App.edits")

    page.click("#proj-switch")
    page.wait_for_selector('#pm-list .pm-proj:has-text("depot")')
    assert page.eval_on_selector_all("#pm-list .pm-proj", "bs => bs.map(b => [b.querySelector('b').textContent, b.classList.contains('on')])") \
        == [["yard", True], ["depot", False]]
    page.click('#pm-list .pm-proj:has-text("depot")')
    page.wait_for_function("document.getElementById('set-name').textContent === 'depot'")
    assert page.is_hidden("#proj-menu")

    page.click("#proj-switch")
    page.wait_for_selector('#pm-list .pm-proj:has-text("yard")')
    page.fill("#pm-search", "ya")
    assert page.locator("#pm-list .pm-proj").count() == 1
    page.keyboard.press("Enter")
    page.wait_for_function("document.getElementById('set-name').textContent === 'yard'")

    page.once("dialog", lambda d: d.accept("yard 2"))
    _switch(page, "#pm-rename")                                         # a saved project: renamed where it is stored
    page.wait_for_function("document.getElementById('set-name').textContent === 'yard 2'")
    page.click("#proj-switch")
    page.wait_for_selector('#pm-list .pm-proj:has-text("yard 2")')
    assert page.locator("#pm-list .pm-proj").count() == 2
    assert not page.errors, page.errors


def test_untitled_project_is_named_on_first_save(page):
    """With no project open the switcher says so; saving the canvas asks for a project name first."""
    assert page.inner_text("#set-name") == ""
    assert "No project open" in page.evaluate("getComputedStyle(document.getElementById('set-name'), '::before').content")
    page.evaluate("Blocks.addBus(100, 100); App.dirty()")
    page.keyboard.press("Control+s")
    page.wait_for_selector("#login-dialog[open]")
    page.fill("#login-user", "namer")
    page.fill("#login-pw", "secret1")
    page.once("dialog", lambda d: d.accept("depot"))
    page.click("#login-register")
    page.wait_for_function("document.getElementById('toast').textContent.includes(\"Saved project 'depot'\")")
    assert page.inner_text("#set-name") == "depot"
    assert page.locator("#save-dot").is_hidden()
    assert not page.errors, page.errors


def test_older_projects_move_their_sizing_onto_the_components(page):
    """Projects saved with the sizes to optimise in the ⚙️ Run settings block get them on the PV / storage blocks."""
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 500, "y": 200})
    page.drag_and_drop(".pal-item.invest", "#drawflow", target_position={"x": 80, "y": 420})
    sizing = page.evaluate("""() => {
        const bat = allNodes().find(n => n.data.type === "Storage"), inv = Blocks.investNode();
        inv.data.assets = {[bat.id]: {optimise: true, opex: 10}, 999: {optimise: true}};
        Blocks.editor.updateNodeDataFromId(inv.id, inv.data);
        Blocks.migrateSizing(); Blocks.updateInvest();
        return [Blocks.editor.getNodeFromId(bat.id).data.sizing, "assets" in Blocks.investNode().data,
                document.getElementById("node-" + bat.id).classList.contains("is-sized")];
    }""")
    assert sizing == [{"optimise": True, "opex": 10}, False, True]
    assert not page.errors, page.errors


def test_start_window_new_or_open_project(page, server):
    """On startup a window offers a new project or a saved one; closing it leaves the canvas with no project."""
    page.goto(server)
    page.wait_for_selector("#start-dialog[open]")
    assert "Log in" in page.inner_text("#start-list")
    page.fill("#start-name", "depot")
    page.click("#start-new button[type=submit]")
    page.wait_for_selector("#start-dialog:not([open])", state="attached")
    assert page.inner_text("#set-name") == "depot"
    assert page.inner_text("#save-dot") == "Not saved yet"                # a new project: a note after its name
    page.evaluate("Blocks.addBus(100, 100); App.dirty()")
    page.keyboard.press("Control+s")
    page.wait_for_selector("#login-dialog[open]")
    page.fill("#login-user", "starter")
    page.fill("#login-pw", "secret1")
    page.click("#login-register")
    page.wait_for_function("document.getElementById('toast').textContent.includes(\"Saved project 'depot'\")")
    assert page.locator("#save-dot").is_hidden()

    _tab(page, "project")
    _switch(page, "#pm-new")                                               # New project: the start window again, now with the list
    page.wait_for_selector('#start-list .start-proj:has-text("depot")')
    page.fill("#start-name", "yard")
    page.click("#start-new button[type=submit]")
    assert page.inner_text("#set-name") == "yard" and page.evaluate("allNodes().length") == 0
    _switch(page, "#pm-new")
    page.click('#start-list .start-proj:has-text("depot")')
    page.wait_for_function("document.getElementById('set-name').textContent === 'depot'")
    assert page.evaluate("allNodes().length") == 1
    _switch(page, "#pm-new")
    page.click("#start-close")                                          # closing keeps what is open
    assert page.inner_text("#set-name") == "depot"
    assert not page.errors, page.errors


def test_compare_two_variants_side_by_side(page):
    """Compare (Project tab) shows two variants side by side: a changed block is amber with the number of changed
    fields, a block only in one variant red (A) or green (B), the rest dimmed; clicking a block lists its fields
    with both values, the changed ones highlighted. The open variant is compared as it is on the canvas."""
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 400, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 150, "y": 200})
    page.evaluate("document.getElementById('set-name').textContent = 'site'")
    page.keyboard.press("Control+s")
    page.wait_for_selector("#login-dialog[open]")
    page.fill("#login-user", "comparer")
    page.fill("#login-pw", "secret1")
    page.click("#login-register")
    page.wait_for_function("ProjectTree.list.some(m => m.name === 'site')")
    _tab(page, "project")
    page.once("dialog", lambda d: d.accept("site + load"))
    page.click("#pt-variant")
    page.wait_for_function("ProjectTree.list.some(m => m.name === 'site + load')")
    page.evaluate("""() => { const g = allNodes().find(n => n.data.type === 'GridConnection');
        g.data.params.max_import = 7; Blocks.editor.updateNodeDataFromId(g.id, g.data); App.dirty(); }""")
    _tab(page, "blocks")
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 650, "y": 300})

    _tab(page, "project")
    page.click("#pt-compare")
    page.wait_for_selector("#cmp-dialog[open] #cmp-canvas-b .drawflow-node")
    assert page.input_value("#cmp-a") == "site + load" and page.input_value("#cmp-b") == "site"   # vs its parent
    cls = lambda side, sel: page.get_attribute(f"#cmp-canvas-{side} {sel}", "class")  # noqa: E731
    assert "cmp-changed" in cls("a", ".drawflow-node:has-text('grid')") and "cmp-changed" in cls("b", ".drawflow-node:has-text('grid')")
    assert page.inner_text("#cmp-canvas-a .drawflow-node.cmp-changed .cmp-badge") == "1"
    assert "cmp-only-a" in cls("a", ".drawflow-node:has-text('demand')")
    assert page.locator("#cmp-canvas-b .drawflow-node:has-text('demand')").count() == 0
    assert "cmp-same" in cls("a", ".drawflow-node.bus-node") or "cmp-changed" in cls("a", ".drawflow-node.bus-node")
    assert "1 changed" in page.inner_text("#cmp-summary") and "1 only in site + load" in page.inner_text("#cmp-summary")
    page.click("#cmp-canvas-a .drawflow-node.cmp-changed .blk-head")
    row = page.locator("#cmp-detail tr.cmp-diff")
    assert row.count() == 1 and "7" in row.inner_text() and "import" in row.inner_text().lower()
    assert page.locator("#cmp-canvas-b .drawflow-node.cmp-on").count() == 1   # the same block on both sides
    assert page.is_visible("#cmp-props #panel") and page.input_value('#cmp-props #panel [data-p="max_import"]') == "7"
    assert page.locator('#cmp-props .cmp-field-diff [data-p="max_import"]').count() == 1   # highlighted there too
    page.fill('#cmp-props #panel [data-p="max_import"]', "")                # edited in place: no difference left
    page.press('#cmp-props #panel [data-p="max_import"]', "Tab")
    page.wait_for_function("!document.querySelector('#cmp-canvas-a .drawflow-node.cmp-changed')")
    assert page.locator("#cmp-detail tr.cmp-diff").count() == 0       # the chosen block stays shown

    box = page.locator("#cmp-canvas-a").bounding_box()                 # drag to move: each canvas on its own
    t = lambda s: page.evaluate(f"[Compare.eds.{s}.canvas_x, Compare.eds.{s}.canvas_y]")  # noqa: E731

    def drag():
        page.mouse.move(box["x"] + box["width"] - 20, box["y"] + box["height"] - 20)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] - 120, box["y"] + box["height"] - 70, steps=5)
        page.mouse.up()
    a0, b0 = t("a"), t("b")
    drag()
    assert t("a") == pytest.approx([a0[0] - 100, a0[1] - 50]) and t("b") == b0
    page.check("#cmp-link")                                             # moved together while ticked
    a1, b1 = t("a"), t("b")
    drag()
    assert t("a") == pytest.approx([a1[0] - 100, a1[1] - 50]) and t("b") == t("a")
    z0 = page.evaluate("Compare.eds.a.zoom")
    page.click('#cmp-dialog [data-zoom="in"]')
    assert page.evaluate("Compare.eds.a.zoom") > z0 and page.evaluate("Compare.eds.b.zoom == Compare.eds.a.zoom")
    page.click("#cmp-fit")                                              # a block of the other view: edit that variant
    assert "cmp-live" in page.get_attribute("#cmp-canvas-a", "class")
    page.click("#cmp-canvas-b .drawflow-node:has-text('grid') .blk-head")   # no question: changes kept until leaving
    page.wait_for_function("App.settings().name === 'site'")
    page.wait_for_selector('#cmp-props #panel [data-p="max_import"]')
    assert "site" in page.inner_text("#cmp-props-head") and "(B," in page.inner_text("#cmp-props-head")
    assert "cmp-live" in page.get_attribute("#cmp-canvas-b", "class")
    assert not page.evaluate("api('/api/projects/site + load').then(p => Object.values(p.drawflow.drawflow.Home.data)"
                             ".some(n => n.data.type === 'Demand'))")          # not saved yet...
    assert page.locator("#cmp-canvas-a .drawflow-node:has-text('demand')").count() == 1   # ...but still compared
    page.fill('#cmp-props #panel [data-p="max_import"]', "3")              # site edited too
    page.press('#cmp-props #panel [data-p="max_import"]', "Tab")
    page.click("#cmp-swap")                                             # B and A swapped: the demand is only in B
    page.wait_for_selector("#cmp-canvas-b .drawflow-node.cmp-only-b")
    page.click("#cmp-close")                                            # leaving: save the changes of both
    page.wait_for_selector("#cmp-leave[open]")
    assert page.eval_on_selector_all("#cmp-leave-list li", "ls => ls.map(l => l.textContent)") == ["site", "site + load"]
    page.click('#cmp-leave [data-choice="stay"]')
    assert page.is_visible("#cmp-dialog")
    page.click("#cmp-close")
    page.click('#cmp-leave [data-choice="save"]')
    page.wait_for_selector("#side #panel", state="attached")            # the Properties panel is back
    assert page.evaluate("api('/api/projects/site + load').then(p => Object.values(p.drawflow.drawflow.Home.data)"
                         ".some(n => n.data.type === 'Demand'))")
    assert page.evaluate("api('/api/projects/site').then(p => Object.values(p.drawflow.drawflow.Home.data)"
                         ".find(n => n.data.type === 'GridConnection').data.params.max_import === 3)")
    assert page.inner_text("#set-name") == "site" and page.is_hidden("#save-dot")
    assert not page.errors, page.errors


def test_editing_properties_after_a_run_asks_first(page):
    """Every change of a block's properties marks the project unsaved and clears the results (they no longer match
    the model); with results on show it asks first: Cancel puts the field back and keeps the results, 'Save as a
    new variant' saves the change as a variant (the open project keeps its results), 'Change this variant' clears
    them. A note is not the model: typing in one keeps the results."""
    page.drag_and_drop(".pal-item.bus", "#drawflow", target_position={"x": 500, "y": 300})
    page.drag_and_drop('.pal-item[data-type="GridConnection"]', "#drawflow", target_position={"x": 260, "y": 200})
    page.drag_and_drop('.pal-item[data-type="Demand"]', "#drawflow", target_position={"x": 760, "y": 300})
    page.evaluate("""() => {
        const id = k => String(allNodes().find(n => k(n.data)).id);
        const bus = id(d => d.kind === "bus"), grid = id(d => d.type === "GridConnection"), dem = id(d => d.type === "Demand");
        connectByName([grid, "link"], [bus, "port"]);
        connectByName([dem, "in"], [bus, "port"]);
        const g = Blocks.editor.getNodeFromId(grid); g.data.params.import_price = 50; Blocks.editor.updateNodeDataFromId(grid, g.data);
        const n = Blocks.editor.getNodeFromId(dem); n.data.params.profile = 0.5; Blocks.editor.updateNodeDataFromId(dem, n.data);
    }""")
    _set(page, "#set-days", "1")
    page.evaluate("document.getElementById('set-name').textContent = 'base'")
    page.keyboard.press("Control+s")
    page.wait_for_selector("#login-dialog[open]")
    page.fill("#login-user", "editor")
    page.fill("#login-pw", "secret1")
    page.click("#login-register")
    page.wait_for_function("ProjectTree.list.some(m => m.name === 'base')")
    _start_run(page)
    page.wait_for_function("Results.summary !== null && Results.saved !== null", timeout=60000)
    page.drag_and_drop(".pal-item.note", "#drawflow", target_position={"x": 300, "y": 450})
    assert page.evaluate("Results.summary !== null")                    # a note does not clear them
    dem = page.evaluate("String(allNodes().find(n => n.data.type === 'Demand').id)")
    page.evaluate(f"Panel.show('{dem}')")
    field = '#panel [data-p="profile"]'

    page.fill(field, "0.8")
    page.press(field, "Tab")
    page.wait_for_selector("#edit-dialog[open]")
    page.click('#edit-dialog [data-choice="cancel"]')
    assert page.input_value(field) == "0.5" and page.evaluate("Results.summary !== null")
    assert page.evaluate(f"Blocks.editor.getNodeFromId('{dem}').data.params.profile") == 0.5

    page.fill(field, "0.8")
    page.once("dialog", lambda d: d.accept("base - more load"))
    page.press(field, "Tab")
    page.wait_for_selector("#edit-dialog[open]")
    page.click('#edit-dialog [data-choice="variant"]')
    page.wait_for_function("ProjectTree.list.some(m => m.name === 'base - more load')")
    assert page.inner_text("#set-name") == "base - more load" and page.evaluate("Results.summary") is None
    assert page.evaluate("api('/api/projects/base').then(p => !!p.results && Object.values(p.drawflow.drawflow.Home.data)"
                         ".find(n => n.data.type === 'Demand').data.params.profile === 0.5)")
    assert page.evaluate("api('/api/projects/base - more load').then(p => Object.values(p.drawflow.drawflow.Home.data)"
                         ".find(n => n.data.type === 'Demand').data.params.profile === 0.8)")

    _start_run(page)                                                    # "Change this variant": results cleared
    page.wait_for_function("Results.summary !== null", timeout=60000)
    page.evaluate(f"Panel.show('{dem}')")
    page.fill(field, "0.9")
    page.press(field, "Tab")
    page.wait_for_selector("#edit-dialog[open]")
    page.click('#edit-dialog [data-choice="edit"]')
    page.wait_for_function("Results.summary === null")
    assert page.evaluate(f"Blocks.editor.getNodeFromId('{dem}').data.params.profile") == 0.9
    assert page.inner_text("#save-dot") == "Unsaved changes"
    page.fill(field, "1")                                               # no results: no question, still unsaved
    page.press(field, "Tab")
    assert page.is_hidden("#edit-dialog") and page.evaluate(f"Blocks.editor.getNodeFromId('{dem}').data.params.profile") == 1
    assert not page.errors, page.errors
