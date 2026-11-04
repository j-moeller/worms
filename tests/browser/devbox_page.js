// Measures the devbox page (templates/devbox.html, rendered by Django) in Chromium and prints
// the observations as JSON; the assertions are in tests/test_devbox_page.py.
//
//   node devbox_page.js <page.html> <static dir> <screenshots dir or "">
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [pagePath, staticDir, shots] = process.argv.slice(2);
const ORIGIN = "https://worms.test"; // https: the clipboard needs a secure page
const TYPES = { ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".woff2": "font/woff2" };

function measure() {
    const overlap = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    const blocks = [...document.querySelectorAll("section[aria-labelledby=connect-title] pre[data-copy]")].map((pre) => {
        const code = pre.querySelector("code");
        const button = pre.querySelector("[data-copy-button]");
        return {
            text: code.textContent,
            hasButton: !!button,
            overlaps: !!button && overlap(code.getBoundingClientRect(), button.getBoundingClientRect()),
            clipped: code.scrollWidth > code.clientWidth + 1 || pre.scrollWidth > pre.clientWidth + 1,
        };
    });
    const pips = document.querySelector(".pips").getBoundingClientRect();
    const reset = document.querySelector(".confirm summary").getBoundingClientRect();
    return {
        blocks,
        resetGap: reset.top - pips.bottom,
        pageScrolls: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    };
}

(async () => {
    const errors = [];
    const browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: ORIGIN });
    await context.route("**/*", (route) => {
        const url = new URL(route.request().url());
        if (url.origin !== ORIGIN) return route.abort();
        if (url.pathname === "/page") return route.fulfill({ contentType: "text/html", body: fs.readFileSync(pagePath, "utf8") });
        const file = path.join(staticDir, url.pathname.replace(/^\/static\//, ""));
        if (url.pathname.startsWith("/static/") && fs.existsSync(file)) {
            return route.fulfill({ contentType: TYPES[path.extname(file)] || "application/octet-stream", body: fs.readFileSync(file) });
        }
        return route.fulfill({ status: 404, body: "" });
    });
    const page = await context.newPage();
    page.on("console", (msg) => { if (msg.type() === "error") errors.push(`console: ${msg.text()}`); });
    page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));

    const out = { errors, widths: {} };
    await page.goto(`${ORIGIN}/page`);
    for (const width of [1280, 900, 390]) {
        await page.setViewportSize({ width, height: 1000 });
        out.widths[width] = await page.evaluate(measure);
        if (shots) await page.screenshot({ path: path.join(shots, `devbox-${width}.png`), fullPage: true });
    }

    await page.setViewportSize({ width: 1280, height: 1000 });
    await page.locator("[data-ssh-login] [data-copy-button]").click();
    out.copied = await page.evaluate(() => navigator.clipboard.readText());

    await browser.close();
    console.log(JSON.stringify(out));
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
