// The round player's files: download what is shown (a progression JSON), then
// open files in a page whose server endpoints all fail (an offline viewer):
// that JSON, the server's zip (log records read from it), a file of the
// earlier format, broken, crafted and hostile files. Prints observations as
// JSON; the assertions are in tests/test_topology_page.py.
//
//   node topology_replay.js <page.html> <fixtures dir> <work dir>
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [pagePath, fixtures, work] = process.argv.slice(2);
const read = (name) => fs.readFileSync(path.join(fixtures, name), "utf8");
const ORIGIN = "http://worms.test";
const errors = [];
const out = { errors, checks: {} };

async function openPage(browser, offline, counters) {
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, acceptDownloads: true });
    const page = await context.newPage();
    page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
    page.on("dialog", async (dialog) => { errors.push(`dialog: ${dialog.message()}`); await dialog.dismiss(); });
    await page.route(`${ORIGIN}/**`, async (route) => {
        const url = new URL(route.request().url());
        const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body });
        if (url.pathname === "/page") return route.fulfill({ status: 200, contentType: "text/html", body: fs.readFileSync(pagePath, "utf8") });
        counters.requests.push(url.pathname);
        if (offline) return json(JSON.stringify({ error: "server unavailable" }), 503);
        if (url.pathname === "/model") return json(read("model.json"));
        if (url.pathname === "/runs") return json(read("runs.json"));
        const run = `/rounds/${JSON.parse(read("runs.json")).rounds[0].runs[0].uuid}/progression/`;
        if (url.pathname === run) return json(read("progression.json"));
        if (url.pathname === `${run}flows/`) return json(read("flows.json"));
        if (url.pathname.startsWith(`${run}logs/`)) return json(read("log.json"));
        return json(JSON.stringify({ error: "Not found" }), 404);
    });
    page.on("request", (request) => { if (!request.url().startsWith(ORIGIN)) counters.foreign.push(request.url()); });
    await page.route(/^https?:\/\/(?!worms\.test)/, (route) => route.abort());
    await page.goto(`${ORIGIN}/page`);
    return page;
}

const state = (page) => page.evaluate(() => {
    const s = document.getElementById("topo").topoState;
    return {
        mode: s.mode, t: s.t, playing: s.playing, dots: s.dots.length, timeline: !!s.timeline, selected: s.selected ? s.selected.type : null,
        sandboxVMs: s.scene ? s.scene.order.filter((n) => n.group === "group:sandbox").length : 0,
        nodes: s.scene ? s.scene.order.length : 0, file: s.file ? s.file.name : null,
    };
});

async function download(page, file) {
    await page.evaluate(() => { document.getElementById("topo-download-menu").open = true; });
    const [dl] = await Promise.all([page.waitForEvent("download"), page.locator("#topo-download").click()]);
    const target = path.join(work, file);
    await dl.saveAs(target);
    return { path: target, suggested: dl.suggestedFilename(), json: JSON.parse(fs.readFileSync(target, "utf8")) };
}

async function openFile(page, file) {
    await page.locator("#topo-file").setInputFiles(file);
}

// Open the first log event of what is shown; its record as the side panel shows it.
async function openLog(page) {
    await page.evaluate(() => {
        const s = document.getElementById("topo").topoState;
        const e = s.timeline.events.find((x) => x.kind === "log_received");
        s.selected = null;
        document.querySelectorAll("#topo-side button").forEach((b) => { if (b.textContent.includes("Back")) b.click(); });
        [...document.querySelectorAll("#topo-feed li")].find((li) => Number(li.dataset.t) === e.t).click();
    });
    await page.waitForFunction(() => { const pre = document.querySelector("#topo-side pre.log-record"); return pre && !pre.textContent.startsWith("Loading"); });
    return page.locator("#topo-side pre.log-record").innerText();
}

async function seekUI(page, t) {
    const box = await page.locator("#topo-track").boundingBox();
    const [lo, hi] = await page.evaluate(() => { const r = document.getElementById("topo").topoState.timeline.run; return [Math.min(r.t_min, 0), r.t_max]; });
    await page.mouse.click(box.x + ((t - lo) / (hi - lo)) * box.width, box.y + box.height / 2);
}

(async () => {
    const browser = await chromium.launch();

    // --- download from the live page -------------------------------------
    const online = { requests: [], foreign: [] };
    const page = await openPage(browser, false, online);
    await page.waitForSelector("[data-node]");
    const infra = await download(page, "infrastructure.json");
    await page.locator("#topo-rounds .topo-tab", { hasText: "R1" }).click();
    await page.waitForFunction(() => { const s = document.getElementById("topo").topoState; return s.timeline && s.timeline.flowsLoaded; });
    const replay = await download(page, "replay.json");
    out.checks.download = {
        infraName: infra.suggested, replayName: replay.suggested,
        format: replay.json.format, version: replay.json.version, audience: replay.json.audience,
        sceneNodes: replay.json.scene.nodes.length, flows: replay.json.timeline.flows.length,
        events: replay.json.timeline.events.length, sandboxVMs: replay.json.timeline.nodes.length,
        infraTimeline: infra.json.timeline, infraNodes: infra.json.scene.nodes.length,
        keys: Object.keys(replay.json).sort(), sceneKeys: Object.keys(replay.json.scene).sort(),
        logs: replay.json.logs, network: replay.json.network,
    };

    // --- an offline viewer: every endpoint fails ------------------------------
    const offline = { requests: [], foreign: [] };
    const viewer = await openPage(browser, true, offline);
    await viewer.waitForFunction(() => document.getElementById("topo").topoState.modelError);
    out.checks.offlineBefore = await state(viewer);
    const before = offline.requests.length;
    await openFile(viewer, replay.path);
    await viewer.waitForFunction(() => document.getElementById("topo").topoState.mode === "file");
    out.checks.opened = await state(viewer);
    out.checks.fileTab = await viewer.locator("#topo-rounds .topo-tab[aria-selected=true]").innerText();
    out.checks.meta = await viewer.locator("#topo-meta").innerText();
    out.checks.playerVisible = await viewer.locator("#topo-player").isVisible();
    await viewer.locator("#topo-play").click();
    await viewer.waitForFunction(() => document.getElementById("topo").topoState.dots.length > 0, null, { timeout: 15000 });
    out.checks.playing = await state(viewer);
    await viewer.locator("#topo-play").click();
    await seekUI(viewer, 95.4);
    await viewer.waitForSelector(".badge");
    await viewer.locator(".badge").first().click({ force: true });
    out.checks.badge = await state(viewer);
    out.checks.badgeSide = await viewer.locator("#topo-side").innerText();
    await viewer.screenshot({ path: path.join(work, "topology-file.png") });
    out.checks.requestsWhileViewing = offline.requests.slice(before).filter((p) => p !== "/model");

    // Round trip: downloading the opened file gives the same data back.
    const again = await download(viewer, "again.json");
    const same = (key) => JSON.stringify(again.json[key]) === JSON.stringify(replay.json[key]);
    out.checks.roundTrip = { scene: same("scene"), timeline: same("timeline"), clock: same("clock"), round: same("round"), team: same("team") };

    // The server's zip: flows from network/flows.jsonl, log records from logs/.
    const beforeZip = offline.requests.length;
    await openFile(viewer, path.join(fixtures, "round.zip"));
    await viewer.waitForFunction(() => { const s = document.getElementById("topo").topoState; return s.mode === "file" && s.file && s.file.name === "round.zip"; });
    out.checks.zip = {
        state: await state(viewer),
        flows: await viewer.evaluate(() => document.getElementById("topo").topoState.timeline.flows.length),
        log: await openLog(viewer),
        xss: await viewer.evaluate(() => window.__xss === 1),
        imgs: await viewer.locator("img").count(),
    };
    out.checks.zip.requests = offline.requests.slice(beforeZip).filter((p) => p !== "/model");
    await viewer.screenshot({ path: path.join(work, "topology-zip.png") });

    // A file of the earlier format: model + timeline, inline logs.
    await openFile(viewer, path.join(fixtures, "legacy.json"));
    await viewer.waitForFunction(() => { const s = document.getElementById("topo").topoState; return s.mode === "file" && s.file && s.file.name === "legacy.json"; });
    out.checks.legacy = {
        state: await state(viewer),
        flows: await viewer.evaluate(() => document.getElementById("topo").topoState.timeline.flows.length),
        log: await openLog(viewer),
    };

    // A progression pointing at other servers: nothing is fetched from there.
    await openFile(viewer, path.join(fixtures, "elsewhere.json"));
    await viewer.waitForFunction(() => { const s = document.getElementById("topo").topoState; return s.mode === "file" && s.file && s.file.name === "elsewhere.json"; });
    out.checks.elsewhere = { state: await state(viewer), log: await openLog(viewer) };
    await viewer.waitForTimeout(300);
    out.checks.elsewhere.evilRequests = offline.foreign.filter((u) => u.includes("evil"));
    out.checks.elsewhere.xss = await viewer.evaluate(() => window.__xss === 1);

    // The infrastructure-only file: a static picture, no player.
    await openFile(viewer, infra.path);
    await viewer.waitForFunction(() => { const s = document.getElementById("topo").topoState; return s.mode === "file" && !s.timeline; });
    out.checks.infraOpened = await state(viewer);
    out.checks.infraPlayerHidden = !(await viewer.locator("#topo-player").isVisible());

    // Broken files: an error, and the view stays as it was.
    out.checks.broken = {};
    for (const name of ["notjson.json", "wrongformat.json", "version99.json", "nodesnotlist.json", "big.json", "nodoc.zip", "liar.zip", "huge.zip"]) {
        await viewer.evaluate(() => { document.getElementById("topo-banner").hidden = true; });
        await openFile(viewer, path.join(fixtures, name));
        await viewer.waitForSelector("#topo-banner.error:not([hidden])");
        out.checks.broken[name] = { banner: await viewer.locator("#topo-banner").innerText(), state: await state(viewer) };
    }

    // A hostile file: opens, nothing executes, odd values are coerced.
    await openFile(viewer, path.join(fixtures, "hostile.json"));
    await viewer.waitForFunction(() => { const s = document.getElementById("topo").topoState; return s.mode === "file" && s.file && s.file.name === "hostile.json"; });
    await viewer.locator("#topo-aux").check();
    out.checks.hostile = {
        state: await state(viewer),
        xss: await viewer.evaluate(() => window.__xss === 1),
        imgs: await viewer.locator("img").count(),
        protoPolluted: await viewer.evaluate(() => ({}).polluted === true),
        svgText: await viewer.locator("#topo-svg").textContent(),
        events: await viewer.evaluate(() => document.getElementById("topo").topoState.timeline.events.map((e) => e.t)),
        roles: await viewer.evaluate(() => document.getElementById("topo").topoState.model.nodes.map((n) => n.role)),
        banner: await viewer.locator("#topo-banner").isHidden(),
    };
    await viewer.locator("#topo-play").click();
    await viewer.waitForTimeout(600);
    await viewer.locator("#topo-play").click();
    out.checks.hostilePlayed = await state(viewer);
    await viewer.screenshot({ path: path.join(work, "topology-hostile.png") });

    console.log(JSON.stringify(out));
    await browser.close();
})().catch((err) => {
    console.log(JSON.stringify({ fatal: String((err && err.stack) || err), errors, checks: out.checks }));
    process.exit(1);
});
