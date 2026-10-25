// Drives the PROGRESSION page (the round player for a team: templates/topology/
// player.html + topology.js with data-audience="team") in Chromium with
// fixture data and prints the observations as JSON; the assertions are in
// tests/test_topology_page.py.
//
//   node progression_page.js <page.html> <fixtures dir> <screenshots dir>
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [pagePath, fixtures, shots] = process.argv.slice(2);
const read = (name) => fs.readFileSync(path.join(fixtures, name), "utf8");
const ORIGIN = "http://worms.test";

const errors = [];
const out = { errors, checks: {} };
const requests = [];

(async () => {
    const browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    const page = await context.newPage();
    page.on("console", (msg) => { if (msg.type() === "error") errors.push(`console: ${msg.text()}`); });
    page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
    page.on("dialog", async (dialog) => { errors.push(`dialog: ${dialog.message()}`); await dialog.dismiss(); });

    await context.route(`${ORIGIN}/**`, async (route) => {
        const url = new URL(route.request().url());
        const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body });
        if (url.pathname === "/page") return route.fulfill({ status: 200, contentType: "text/html", body: fs.readFileSync(pagePath, "utf8") });
        if (url.pathname === "/page-fast") {
            const html = fs.readFileSync(pagePath, "utf8").replace("data-refresh-ms='600000'", "data-refresh-ms='600'");
            return route.fulfill({ status: 200, contentType: "text/html", body: html });
        }
        requests.push(url.pathname);
        if (url.pathname === "/progression/rounds/") return json(read("team_rounds.json"));
        if (url.pathname === "/progression/rounds/1/") return json(read("team1.json"));
        if (url.pathname === "/progression/rounds/1/flows/") return json(read("team_flows.json"));
        if (url.pathname === "/progression/rounds/3/") return json(read("team3.json"));
        if (/^\/progression\/rounds\/[13]\/logs\/[0-9a-f-]+\/$/.test(url.pathname)) return json(read("log.json"));
        return json(JSON.stringify({ error: "No such round." }), 404);
    });
    await context.route(/^https?:\/\/(?!worms\.test)/, (route) => route.abort());

    const S = () => page.evaluate(() => {
        const s = document.getElementById("topo").topoState;
        return {
            mode: s.mode, round: s.roundId, t: s.t, playing: s.playing, dots: s.dots.length, timeline: !!s.timeline,
            selected: s.selected ? s.selected.type : null, flowsLoaded: !!(s.timeline && s.timeline.flowsLoaded),
            error: s.timelineError ? s.timelineError.message : null,
        };
    });
    const sceneInfo = () => page.evaluate(() => {
        const sc = document.getElementById("topo").topoState.scene;
        return {
            team: sc.team,
            roles: [...new Set(sc.order.map((n) => n.role))].sort(),
            groups: sc.groups.map((g) => g.id),
            vms: sc.order.filter((n) => n.info && n.info.run).map((n) => n.info.name).sort(),
            devboxMembers: sc.order.filter((n) => n.group && n.group.startsWith("group:devbox")).map((n) => n.info.name).sort(),
            edgeKinds: [...new Set(sc.edges.map((e) => e.kind))].sort(),
            unverified: sc.edges.filter((e) => !e.verified).length,
        };
    });
    // Go to time t: set it 10 s before and step forward with the player's button.
    const settle = async (t) => {
        await page.evaluate((x) => { document.getElementById("topo").topoState.t = x - 10; }, t);
        await page.locator("#topo-fwd").click();
    };
    const nodeId = (label) => page.evaluate((name) => {
        const sc = document.getElementById("topo").topoState.scene;
        const n = sc.order.find((x) => x.info && x.info.name === name);
        return n ? n.id : null;
    }, label);
    const waitLoaded = () => page.waitForFunction(() => { const s = document.getElementById("topo").topoState; return s.timeline && (s.timeline.flowsLoaded || !s.doc.network.url); });

    // --- the live round (team-a's run) ----------------------------------
    await page.goto(`${ORIGIN}/page`);
    await waitLoaded();
    out.checks.serverPartsHidden = await page.evaluate(() =>
        [...document.querySelectorAll("[data-progression-tabs], [data-progression-summary]")].every((n) => n.hidden));
    out.checks.playerShown = await page.locator("#topo").isVisible();
    out.checks.title = await page.locator(".topo-title").innerText();
    out.checks.auxHidden = !(await page.locator("#topo-aux").isVisible());
    out.checks.tabs = await page.locator("#topo-rounds .topo-tab").allInnerTexts();
    out.checks.live = await sceneInfo();
    out.checks.liveState = await S();
    out.checks.requestsAtLoad = requests.slice();
    out.checks.svgText = await page.locator("#topo-svg").textContent();
    out.checks.legend = await page.locator("#topo-legend").innerText();
    out.checks.summary = await page.locator("#topo-side").innerText();
    out.checks.meta = await page.locator("#topo-meta").innerText();

    // Hover cards and details: no firewall, no NICs, no gateway.
    const vm0 = await nodeId("sandbox-vm0");
    await page.locator(`[data-node="${vm0}"]`).hover();
    await page.waitForSelector("#topo-tip:not([hidden])");
    out.checks.vmTip = await page.locator("#topo-tip").innerText();
    const scoring = await nodeId("scoring");
    await page.locator(`[data-node="${scoring}"]`).hover();
    out.checks.scoringTip = await page.locator("#topo-tip").innerText();
    await page.locator(".anchor").first().hover();
    out.checks.anchorTip = await page.locator("#topo-tip").innerText();
    await page.locator(`[data-node="${vm0}"]`).click();
    out.checks.vmSide = await page.locator("#topo-side").innerText();
    await page.locator(`[data-node="${scoring}"]`).click();
    out.checks.scoringSide = await page.locator("#topo-side").innerText();
    await page.locator("#topo-side button", { hasText: "Back" }).click();

    // A VM is faint until its capture.
    const vm1 = await nodeId("sandbox-vm1");
    await settle(30);
    out.checks.at30 = { vm0: await page.locator(`[data-node="${vm0}"]`).getAttribute("class"), vm1: await page.locator(`[data-node="${vm1}"]`).getAttribute("class") };
    await settle(100);
    out.checks.at100 = { vm0: await page.locator(`[data-node="${vm0}"]`).getAttribute("class"), vm1: await page.locator(`[data-node="${vm1}"]`).getAttribute("class") };
    await page.screenshot({ path: path.join(shots, "progression-live.png") });

    // Playing draws the run's connections.
    await settle(0);
    await page.locator("#topo-play").click();
    await page.waitForFunction(() => document.getElementById("topo").topoState.dots.length > 0, null, { timeout: 15000 });
    await page.locator("#topo-play").click();
    out.checks.played = await S();

    // A log record: fetched when opened (once), shown as text.
    await page.locator("#topo-feed li", { hasText: "log received" }).first().click();
    await page.waitForFunction(() => { const pre = document.querySelector("#topo-side pre.log-record"); return pre && !pre.textContent.startsWith("Loading"); });
    out.checks.logSide = await page.locator("#topo-side pre.log-record").innerText();
    out.checks.xss = await page.evaluate(() => window.__xss === 1);
    out.checks.imgs = await page.locator("img").count();
    out.checks.logRequests = requests.filter((p) => p.includes("/logs/"));

    // The Download menu: the round's zip.
    await page.locator("#topo-download-menu > summary").click();
    const zipHref = () => page.evaluate(() => { const a = document.getElementById("topo-dl-zip"); return a.hidden ? null : a.getAttribute("href"); });
    out.checks.zip = { full: await zipHref() };
    await page.locator("#topo-dl-network").uncheck();
    out.checks.zip.noNetwork = await zipHref();
    await page.locator("#topo-dl-network").check();
    await page.locator("#topo-download-menu > summary").click();
    out.checks.hash = await page.evaluate(() => location.hash);

    // --- an upcoming round: 404, a message ------------------------------
    await page.locator("#topo-rounds .topo-tab", { hasText: "R2" }).click();
    await page.waitForFunction(() => document.getElementById("topo").topoState.timelineError);
    out.checks.errorText = await page.locator("#topo-empty").innerText();

    // --- a Dev round: the team's devbox, no network data ------------------
    const before = requests.length;
    await page.locator("#topo-rounds .topo-tab", { hasText: "Dev week" }).click();
    await waitLoaded();
    await page.waitForTimeout(300);
    out.checks.dev = await sceneInfo();
    out.checks.devState = await S();
    out.checks.devRequests = requests.slice(before);
    out.checks.devSummary = await page.locator("#topo-side").innerText();
    out.checks.devMarks = await page.evaluate(() => [...document.querySelectorAll("#topo-marks .mark")].map((m) => m.className).filter((c) => /edge|now/.test(c)));
    await page.locator("#topo-download-menu > summary").click();
    out.checks.devZip = await zipHref();
    out.checks.devNetworkDisabled = await page.locator("#topo-dl-network").isDisabled();
    await page.locator("#topo-download-menu > summary").click();
    await settle(5000);
    out.checks.devAt5000 = await page.evaluate(() => {
        const sc = document.getElementById("topo").topoState.scene;
        return sc.order.filter((n) => n.group && n.group.startsWith("group:devbox")).map((n) => [n.info.name, n.el.getAttribute("class")]);
    });
    await page.screenshot({ path: path.join(shots, "progression-dev.png") });

    // --- a running round refreshes, keeping the time and the selection -----
    const fast = await context.newPage();
    fast.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
    const fetched = [];
    fast.on("request", (r) => fetched.push(new URL(r.url()).pathname));
    await fast.goto(`${ORIGIN}/page-fast#round=3&t=6000`);
    await fast.waitForFunction(() => { const s = document.getElementById("topo").topoState; return s.timeline && s.roundId === 3; });
    const restored = await fast.evaluate(() => { const s = document.getElementById("topo").topoState; return { round: s.roundId, t: s.t }; });
    const devA = await fast.evaluate(() => document.getElementById("topo").topoState.scene.order.find((n) => n.info && n.info.name === "devbox-t1-a").id);
    await fast.locator(`[data-node="${devA}"]`).click();
    const fetchesBefore = fetched.filter((p) => p === "/progression/rounds/3/").length;
    await fast.waitForTimeout(1600);
    out.checks.refresh = await fast.evaluate(() => { const s = document.getElementById("topo").topoState; return { t: s.t, round: s.roundId, selected: s.selected && s.selected.type }; });
    Object.assign(out.checks.refresh, { restored, fetchesBefore, fetchesAfter: fetched.filter((p) => p === "/progression/rounds/3/").length });

    console.log(JSON.stringify(out));
    await browser.close();
})().catch((err) => {
    console.log(JSON.stringify({ fatal: String((err && err.stack) || err), errors, checks: out.checks }));
    process.exit(1);
});
