// Drives the PROGRESSION page in the Dev view: the devbox has no rounds, so the player gets one
// progression, which is never finished. Prints the observations as JSON; the assertions are in
// tests/test_topology_page.py.
//
//   node devbox_progression.js <page.html> <document.json> <log.json> <screenshots dir>
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [pagePath, docPath, logPath, shots] = process.argv.slice(2);
const ORIGIN = "http://worms.test";

const errors = [];
const requests = [];
const out = { errors, requests, checks: {} };

(async () => {
    const browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    const page = await context.newPage();
    page.on("console", (msg) => { if (msg.type() === "error") errors.push(`console: ${msg.text()}`); });
    page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));

    await context.route(`${ORIGIN}/**`, (route) => {
        const url = new URL(route.request().url());
        const json = (file) => route.fulfill({ contentType: "application/json", body: fs.readFileSync(file, "utf8") });
        if (url.pathname === "/page") return route.fulfill({ contentType: "text/html", body: fs.readFileSync(pagePath, "utf8") });
        requests.push(url.pathname);
        if (url.pathname === "/progression/devbox/") return json(docPath);
        if (url.pathname.startsWith("/progression/devbox/logs/")) return json(logPath);
        return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: "Not found." }) });
    });
    await context.route(/^https?:\/\/(?!worms\.test)/, (route) => route.abort());

    await page.goto(`${ORIGIN}/page`);
    await page.waitForFunction(() => document.getElementById("topo").topoState.timeline);
    const state = () => page.evaluate(() => {
        const s = document.getElementById("topo").topoState;
        return { mode: s.mode, t: s.t, tMin: s.timeline.run.t_min, tMax: s.timeline.run.t_max, error: s.timelineError ? s.timelineError.message : null };
    });

    out.checks.state = await state();
    out.checks.tabs = await page.locator("#topo-rounds .topo-tab").allInnerTexts();
    out.checks.roundsText = await page.locator("#topo-rounds").innerText();
    out.checks.meta = await page.locator("#topo-meta").innerText();
    out.checks.side = await page.locator("#topo-side").innerText();
    out.checks.groups = await page.evaluate(() => [...document.querySelectorAll("#topo-svg [data-group]")].map((g) => g.dataset.group));

    // Seeking to the end shows everything it has captured so far.
    await page.evaluate((t) => { const s = document.getElementById("topo"); s.topoState.t = t; }, out.checks.state.tMax);
    await page.locator("#topo-seek").evaluate((seek, t) => {
        seek.value = t;
        seek.dispatchEvent(new Event("input", { bubbles: true }));
    }, out.checks.state.tMax).catch(() => {});
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(shots, "progression-devbox.png") });

    // A log record: fetched from the devbox's own address, shown as text.
    await page.locator("#topo-feed li", { hasText: "log received" }).first().click();
    await page.waitForFunction(() => { const pre = document.querySelector("#topo-side pre.log-record"); return pre && pre.textContent.length > 0; });
    out.checks.logSide = await page.locator("#topo-side pre.log-record").innerText();
    out.checks.xss = await page.evaluate(() => window.__xss === 1);
    await page.locator("#topo-side button", { hasText: "Back" }).click();

    // The download goes to the devbox's zip; it has no network data to offer.
    await page.locator("#topo-download-menu > summary").click();
    out.checks.zip = await page.evaluate(() => { const a = document.getElementById("topo-dl-zip"); return a.hidden ? null : a.getAttribute("href"); });
    out.checks.networkDisabled = await page.locator("#topo-dl-network").isDisabled();

    // It is never finished, so it keeps reloading (data-refresh-ms in the page), keeping the time.
    const before = requests.filter((p) => p === "/progression/devbox/").length;
    const t = (await state()).t;
    await page.waitForTimeout(1500);
    out.checks.refresh = { before, after: requests.filter((p) => p === "/progression/devbox/").length, t, tAfter: (await state()).t };

    console.log(JSON.stringify(out));
    await browser.close();
})().catch((err) => {
    console.log(JSON.stringify({ fatal: String((err && err.stack) || err), errors, checks: out.checks }));
    process.exit(1);
});
