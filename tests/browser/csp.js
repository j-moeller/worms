// The dashboard's Content-Security-Policy (config/settings/dashboard.py: SECURE_CSP), for the
// pages the drivers serve themselves; tests/test_dashboard_frontend.py checks that it is the
// one Django sends. watch(): whatever the policy blocks is an error on the console, which the
// drivers count as they count page errors (an init script is not subject to the policy).
// staticFile(): /static/<path> from the checkout, as nginx serves it on the dashboard's origin
// (the policy allows no inline script or style, so the pages reference these).
const path = require("path");
const POLICY = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    + "form-action 'self'; frame-ancestors 'none'; base-uri 'none'";
const HEADERS = { "Content-Security-Policy": POLICY };

async function watch(context) {
    await context.addInitScript(() => document.addEventListener("securitypolicyviolation", (event) => console.error(
        `CSP violation: ${event.violatedDirective} ${event.blockedURI || "inline"} at ${event.sourceFile}:${event.lineNumber}`)));
}

const STATIC = path.join(__dirname, "..", "..", "projects", "dashboard", "dashboard", "static");

function staticFile(pathname) {
    const file = path.resolve(STATIC, `.${pathname.slice("/static".length)}`);
    return file.startsWith(STATIC + path.sep) ? file : null;
}

module.exports = { POLICY, HEADERS, watch, staticFile };
