"""End-to-end browser test of the drag-and-drop UI (headless Chromium via Playwright).

Skipped when Playwright or its browser is not installed:
    pip install playwright && python -m playwright install chromium
"""

import socket
import threading
import time

import numpy as np
import pandas as pd
import pytest

playwright = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from oems.ui.server import create_app  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    app = create_app(tmp_path_factory.mktemp("ws"))
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
        pg.goto(server)
        try:
            pg.wait_for_function("typeof Blocks !== 'undefined' && Blocks.catalog !== null && Blocks.editor !== null", timeout=15000)
        except playwright.Error:
            browser.close()
            pytest.skip("UI libraries could not be loaded from the CDN (offline?)")
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
        connectByName([id('grid'), 'link'], [id('el'), 'supply']);          // one wire: import + export
        connectByName([id('el'), 'withdraw'], [id('battery'), 'link']);     // one wire: charge + discharge
        connectByName([id('el'), 'withdraw'], [id('demand'), 'in']);
        connectByName([id('price'), 'price'], [id('grid'), 'import_price']);
        connectByName([id('load'), 'load'], [id('demand'), 'profile']);
    }""")
    edges = page.evaluate("edgesOf(Blocks.editor.export())")
    assert len(edges) == 5
    assert sum(1 for e in edges if "link" in (e["from"][1], e["to"][1])) == 2

    # an invalid wire (input -> bus) is rejected
    page.evaluate("connectByName([String(allNodes().find(x => x.data.kind === 'data').id), 'price'],"
                  " [String(allNodes().find(x => x.data.name === 'el').id), 'supply'])")
    assert len(page.evaluate("edgesOf(Blocks.editor.export())")) == len(edges)

    # settings and run
    page.fill("#set-days", "2")
    page.click("#btn-validate")
    page.wait_for_selector("#toast.show")
    page.click("#btn-run")
    page.wait_for_function("Results.summary !== null", timeout=60000)
    labels = page.locator(".edge-label").all_inner_texts()
    assert len(labels) == 3                                   # one label per energy wire
    assert any(t.startswith("import") and "export" in t for t in labels)   # both directions on the grid wire
    assert "€" in page.inner_text("#res-summary")
    page.click('#res-tabs button[data-tab="balance"]')
    page.wait_for_selector("#plot-balance .main-svg")
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
    assert outs == ["out1", "out2"]
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
    """Real mouse drag from the centre of element `src` to the centre of element `dst` (CSS selectors)."""
    a, b = page.locator(src).bounding_box(), page.locator(dst).bounding_box()
    page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
    page.mouse.down()
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=8)
    page.mouse.up()


def test_wires_can_start_at_either_end_and_drop_on_blocks(page, tmp_path):
    """Wires can be pulled from a component's own input connectors (energy and parameter) to a bus or input block,
    and can be dropped anywhere on a block: the matching connector is chosen (bus supply/withdraw, the free energy
    input, the only free parameter). Invalid drops still go through the wiring rules."""
    for typ, x, y in (("GridConnection", 80, 120), ("Demand", 760, 120), ("Demand", 760, 360)):
        page.drag_and_drop(f'.pal-item[data-type="{typ}"]', "#drawflow", target_position={"x": x, "y": y})
    page.drag_and_drop('.pal-item[data-preset="battery"]', "#drawflow", target_position={"x": 760, "y": 560})
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

    _drag(page, conn("demand", "in", "in"), head("el"))                 # component input -> bus body
    _drag(page, conn("grid", "in", "import_price"), head("price"))      # parameter -> input block body
    _drag(page, conn("grid", "out", "link"), head("el"))                # forward drag dropped on the bus body
    _drag(page, conn("battery", "in", "link"), conn("el", "out", "withdraw"))  # input -> exact output dot
    _drag(page, conn("el", "out", "withdraw"), head("demand2"))         # bus -> component body: picks 'in'
    _drag(page, conn("load", "out", "load"), head("demand2"))           # input -> only free parameter (profile)
    _drag(page, conn("demand", "in", "profile"), head("el"))            # invalid: parameter <- bus, rejected

    edges = {(e["from"][1], e["to"][1]) for e in page.evaluate("edgesOf(Blocks.editor.export())")}
    assert edges == {("withdraw", "in"), ("price", "import_price"), ("link", "supply"), ("withdraw", "link"),
                     ("load", "profile")}
    assert len(page.evaluate("edgesOf(Blocks.editor.export())")) == 6   # both demands wired to el
    assert page.locator(".wiring-temp").count() == 0 and page.locator(".drop-target").count() == 0
    assert not page.errors, page.errors
