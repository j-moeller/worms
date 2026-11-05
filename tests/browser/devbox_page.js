// Measures the devbox page (templates/devbox.html, rendered by Django) in Chromium and prints
// the observations as JSON; the assertions are in tests/test_devbox_page.py.
//
//   node devbox_page.js <page.html> <profile.html> <static dir> <screenshots dir or "">
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [pagePath, profilePath, staticDir, shots] = process.argv.slice(2);
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
    const nodes = [...document.querySelectorAll(".vm-node")].map((node) => {
        const box = node.getBoundingClientRect();
        const ports = [...node.querySelectorAll(".vm-port")].map((port) => port.getBoundingClientRect());
        const inner = [...node.querySelectorAll(".vm-node__head, .vm-port__ip, .vm-node__state")].map((e) => e.getBoundingClientRect());
        const style = getComputedStyle(node);
        return {
            text: node.innerText.replace(/\s+/g, " ").trim(),
            left: Math.round(box.left), top: Math.round(box.top), middle: box.top + box.height / 2,
            portsMiddle: ports.length ? (Math.min(...ports.map((p) => p.top)) + Math.max(...ports.map((p) => p.bottom))) / 2 : null,
            portsOverlap: ports.length === 2 && overlap(ports[0], ports[1]),
            spills: inner.some((r) => r.left < box.left || r.right > box.right || r.top < box.top || r.bottom > box.bottom),
            ipClipped: [...node.querySelectorAll(".vm-port__ip")].some((ip) => ip.scrollWidth > ip.clientWidth + 1),
            border: style.borderTopColor, background: style.backgroundColor,
        };
    });
    const chain = document.querySelector(".vm-chain").getBoundingClientRect();
    const reset = document.querySelector(".confirm summary").getBoundingClientRect();
    return {
        blocks,
        nodes,
        resetGap: reset.top - chain.bottom,
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
        if (url.pathname === "/profile-page") return route.fulfill({ contentType: "text/html", body: fs.readFileSync(profilePath, "utf8") });
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
    for (const width of [1280, 1101, 1100, 900, 390]) {
        await page.setViewportSize({ width, height: 1000 });
        out.widths[width] = await page.evaluate(measure);
        if (shots) await page.screenshot({ path: path.join(shots, `devbox-${width}.png`), fullPage: true });
    }

    await page.setViewportSize({ width: 1280, height: 1000 });
    await page.locator("[data-ssh-login] [data-copy-button]").click();
    out.copied = await page.evaluate(() => navigator.clipboard.readText());

    await page.goto(`${ORIGIN}/profile-page`);
    out.profile = {};
    for (const width of [1280, 390]) {
        await page.setViewportSize({ width, height: 1000 });
        out.profile[width] = await page.evaluate(() => ({
            pageScrolls: document.documentElement.scrollWidth > document.documentElement.clientWidth,
            keysPanel: !!document.querySelector("section[aria-labelledby=keys-title]"),
            createTokenInset: document.querySelector(".disclosure summary").getBoundingClientRect().left - document.querySelector(".disclosure").closest(".panel").getBoundingClientRect().left,
            issueFormShown: document.querySelector(".disclosure form").checkVisibility(),
        }));
        if (shots) await page.screenshot({ path: path.join(shots, `profile-${width}.png`), fullPage: true });
    }

    await browser.close();
    console.log(JSON.stringify(out));
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
