// Drives the TOPOLOGY page (templates/topology/player.html + app.js) in
// Chromium with fixture data and prints the observations as JSON; the
// assertions are in tests/test_topology_page.py.
//
//   node topology_player.js <page.html> <fixtures dir> <screenshots dir>
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [pagePath, fixtures, shots] = process.argv.slice(2);
const read = (name) => fs.readFileSync(path.join(fixtures, name), "utf8");
const ORIGIN = "http://worms.test";

const errors = [];
const out = { errors, checks: {} };
let modelFetches = 0;

(async () => {
    const browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1680, height: 1050 } });
    const page = await context.newPage();
    page.on("console", (msg) => { if (msg.type() === "error") errors.push(`console: ${msg.text()}`); });
    page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
    page.on("dialog", async (dialog) => { errors.push(`dialog: ${dialog.message()}`); await dialog.dismiss(); });

    await page.route(`${ORIGIN}/**`, async (route) => {
        const url = new URL(route.request().url());
        const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body });
        if (url.pathname === "/page") return route.fulfill({ status: 200, contentType: "text/html", body: fs.readFileSync(pagePath, "utf8") });
        if (url.pathname === "/model") { modelFetches++; return json(read("model.json")); }
        if (url.pathname === "/runs") return json(read("runs.json"));
        if (url.pathname === `/rounds/${JSON.parse(read("runs.json")).rounds[0].runs[0].uuid}/timeline/`) return json(read("timeline.json"));
        if (url.pathname.startsWith("/rounds/")) return json(JSON.stringify({ error: "Not found" }), 404);
        return route.fulfill({ status: 404, body: "" });
    });
    // Fonts etc. from the internet are not needed (and not reachable).
    await page.route(/^https?:\/\/(?!worms\.test)/, (route) => route.abort());

    const S = () => page.evaluate(() => {
        const s = document.getElementById("topo").topoState;
        return { mode: s.mode, t: s.t, playing: s.playing, dots: s.dots.length, timeline: !!s.timeline, speed: s.speed, selected: s.selected ? s.selected.type : null };
    });
    const sceneInfo = () => page.evaluate(() => {
        const sc = document.getElementById("topo").topoState.scene;
        const byRole = (role) => { const id = (sc.byRole.get(role) || [])[0]; const n = sc.nodes.get(id); return n ? { x: n.x, y: n.y, id: n.id } : null; };
        const group = (id) => { const g = sc.groups.find((x) => x.id === id); return g ? { x: g.x, y: g.y, w: g.w, h: g.h } : null; };
        return {
            proxmox: byRole("proxmox"), orchestrator: byRole("orchestrator"), dashboard: byRole("dashboard"), database: byRole("database"),
            scoring: byRole("scoring"), logging: byRole("logging"), monitor: byRole("monitor"),
            sandbox: group("group:sandbox"), pi: group("group:pi"),
            devboxes: sc.groups.filter((g) => g.kind === "devbox").map((g) => g.id),
            sandboxVMs: sc.order.filter((n) => n.group === "group:sandbox").length,
            edgeKinds: [...new Set(sc.edges.map((e) => e.kind))].sort(),
            dbSources: sc.edges.filter((e) => e.kind === "db").map((e) => sc.nodes.get(e.from).info.name).sort(),
            unverified: sc.edges.filter((e) => !e.verified).length,
        };
    });
    const seekUI = async (t) => {
        const box = await page.locator("#topo-track").boundingBox();
        const [lo, hi] = await page.evaluate(() => { const r = document.getElementById("topo").topoState.timeline.run; return [Math.min(r.t_min, 0), r.t_max]; });
        await page.mouse.click(box.x + ((t - lo) / (hi - lo)) * box.width, box.y + box.height / 2);
    };

    // --- live infrastructure --------------------------------------------
    await page.goto(`${ORIGIN}/page`);
    await page.waitForSelector("[data-node]");
    out.checks.live = await sceneInfo();
    out.checks.liveNodeCount = await page.locator("[data-node]").count();
    out.checks.inactive = await page.locator("[data-node].inactive").count();
    await page.locator(`[data-node="${out.checks.live.database.id}"]`).hover();
    await page.waitForSelector("#topo-tip:not([hidden])");
    out.checks.dbTip = await page.locator("#topo-tip").innerText();
    await page.screenshot({ path: path.join(shots, "topology-live.png") });
    await page.locator(`[data-node="${out.checks.live.database.id}"]`).click();
    out.checks.dbSide = await page.locator("#topo-side").innerText();
    await page.locator("#topo-side button", { hasText: "Back" }).click();

    // --- round -> team -> player ---------------------------------------------
    await page.locator("#topo-rounds .topo-tab", { hasText: "R1" }).click();
    await page.waitForFunction(() => document.getElementById("topo").topoState.timeline);
    out.checks.teamTabs = await page.locator("#topo-teams .topo-tab").allInnerTexts();
    out.checks.round = await sceneInfo();
    out.checks.playerVisible = await page.locator("#topo-player").isVisible();
    out.checks.marks = await page.locator("#topo-marks .mark").count();

    await page.locator("#topo-play").click();
    await page.waitForFunction(() => document.getElementById("topo").topoState.dots.length > 0, null, { timeout: 15000 });
    out.checks.afterPlay = await S();
    await page.locator("#topo-play").click();
    out.checks.afterPause = await S();

    // A capture: badge on the VM, flag marker; clicking the badge pauses and shows details.
    await seekUI(95.4);
    out.checks.at95 = await S();
    await page.waitForSelector(".badge");
    out.checks.badges95 = await page.locator(".badge").count();
    const capturedId = await page.evaluate(() => document.getElementById("topo").topoState.timeline.nodes.find((n) => n.captured).id);
    out.checks.capturedClass = await page.locator(`[data-node="${capturedId}"]`).getAttribute("class");
    await page.locator("#topo-play").click(); // playing again: a badge click must pause
    await page.locator(".badge").first().click({ force: true });
    out.checks.afterBadge = await S();
    out.checks.badgeSide = await page.locator("#topo-side").innerText();
    await page.screenshot({ path: path.join(shots, "topology-round.png") });

    // The log with the XSS payload: shown as text.
    await seekUI(60.6);
    await page.locator("#topo-side button", { hasText: "Back" }).click();
    await page.waitForFunction(() => [...document.querySelectorAll("#topo-feed li")].length > 0);
    await page.locator("#topo-feed li", { hasText: "log received" }).first().click();
    out.checks.logSide = await page.locator("#topo-side pre").innerText();
    out.checks.xssFired = await page.evaluate(() => window.__xss === 1);
    out.checks.imgInSide = await page.locator("#topo-side img").count();

    // VM power state over time.
    const stoppedId = await page.evaluate(() => document.getElementById("topo").topoState.timeline.nodes[3].id);
    await seekUI(150);
    out.checks.stoppedClass = await page.locator(`[data-node="${stoppedId}"]`).getAttribute("class");
    await seekUI(320);
    out.checks.restartedClass = await page.locator(`[data-node="${stoppedId}"]`).getAttribute("class");

    // Click a dot: pauses, shows the connection.
    await seekUI(20);
    await page.locator("#topo-play").click();
    await page.waitForFunction(() => document.getElementById("topo").topoState.dots.length > 0, null, { timeout: 15000 });
    await page.locator("#topo-play").click();
    const dot = await page.evaluate(() => { const d = document.getElementById("topo").topoState.dots[0]; return { x: d.x, y: d.y }; });
    const stage = await page.locator("#topo-stage").boundingBox();
    await page.mouse.click(stage.x + dot.x, stage.y + dot.y);
    out.checks.afterDot = await S();
    out.checks.dotSide = await page.locator("#topo-side").innerText();

    // The live snapshot refreshes (every 700 ms here): the chosen speed and
    // the user's zoom survive it.
    await page.locator("#topo-side button", { hasText: "Back" }).click();
    await page.locator("#topo-speed").selectOption("60");
    await page.locator("#topo-zoom-in").click();
    const k0 = await page.evaluate(() => document.getElementById("topo").topoState.view.k);
    const fetchesBefore = modelFetches;
    await page.waitForTimeout(1800);
    out.checks.refresh = await page.evaluate(() => { const s = document.getElementById("topo").topoState; return { speed: s.speed, k: s.view.k }; });
    Object.assign(out.checks.refresh, { k0, fetchesBefore, modelFetches });
    await page.locator("#topo-fit").click();

    // Next-event key and the URL hash.
    await page.keyboard.press(".");
    out.checks.afterNext = await S();
    out.checks.hash = await page.evaluate(() => location.hash);

    await page.emulateMedia({ colorScheme: "dark" });
    await seekUI(96);
    await page.screenshot({ path: path.join(shots, "topology-round-dark.png") });

    // The other team's run fails to load: a message, not a crash.
    await page.locator("#topo-teams .topo-tab", { hasText: "team-b" }).click();
    await page.waitForFunction(() => document.getElementById("topo").topoState.timelineError);
    out.checks.errorText = await page.locator("#topo-empty").innerText();

    // Back to live.
    await page.locator("#topo-rounds .topo-tab", { hasText: "Infrastructure" }).click();
    out.checks.backLive = await S();
    out.checks.playerHiddenLive = !(await page.locator("#topo-player").isVisible());

    console.log(JSON.stringify(out));
    await browser.close();
})().catch((err) => {
    console.log(JSON.stringify({ fatal: String(err && err.stack || err), errors, checks: out.checks }));
    process.exit(1);
});
