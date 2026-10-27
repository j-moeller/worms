// A real browser against the disposable local server; no mocked responses.
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const engines = require('playwright');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

(async () => {
    const browser = await engines[input.engine || "chromium"].launch();
    const errors = [];
    try {
        const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
            // Only the disposable fixture server uses a self-signed certificate.
            // The independent requests journey verifies that certificate.
            ignoreHTTPSErrors: Boolean(input.fixtureTLS) });
        // Keep all browser traffic inside this local test instance.
        await context.route('**/*', route => {
            const url = new URL(route.request().url());
            return url.origin === input.base ? route.continue() : route.abort();
        });
        const page = await context.newPage();
        page.on('pageerror', error => errors.push(error.message));
        page.on('response', response => {
            if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) {
                errors.push(`${response.status()} ${response.url()}`);
            }
        });
        page.on('dialog', async dialog => {
            errors.push(`Unexpected dialog: ${dialog.message()}`);
            await dialog.dismiss();
        });
        const login = async (username, password, next) => {
            await page.goto(`${input.base}/login/?next=${encodeURIComponent(next)}`);
            await page.getByLabel('Username', { exact: true }).fill(username);
            await page.getByLabel('Password', { exact: true }).fill(password);
            await page.getByRole('button', { name: 'Sign in', exact: true }).click();
            await page.waitForURL(input.base + next);
        };
        await login(input.student, input.password, '/progression/');
        await page.waitForFunction(() => document.getElementById('topo')?.topoState?.timeline);
        assert(await page.locator('#topo').isVisible());
        await page.locator('#topo-feed li', { hasText: 'log received' }).first().click();
        await page.waitForFunction(() => document.querySelector('#topo-side pre.log-record')?.textContent.includes('local log'));
        const record = await page.locator('#topo-side pre.log-record').innerText();
        assert(record.includes('local log ü 🐛'));
        assert(record.includes('<img src=x onerror=alert(1)>'));
        assert(!record.includes('RED-ONLY-MARKER'));
        assert.equal(await page.locator('#topo-side pre.log-record img').count(), 0);
        fs.mkdirSync(input.output, { recursive: true });
        await page.screenshot({ path: path.join(input.output, 'student-log.png'), fullPage: true });
        await page.locator('#topo-download-menu > summary').click();
        const pending = page.waitForEvent('download');
        await page.locator('#topo-dl-zip').click();
        const download = await pending;
        assert.equal(await download.failure(), null);
        const archive = path.join(input.output, 'student-round.zip');
        await download.saveAs(archive);
        await page.goto(input.base + '/staff/');
        assert.equal(new URL(page.url()).pathname, '/');
        await page.screenshot({ path: path.join(input.output, 'student-scores.png'), fullPage: true });
        await page.getByRole('button', { name: 'Sign out', exact: true }).click();
        await login(input.admin, input.adminPassword, '/staff/rounds/');
        await page.getByRole('link', { name: 'Journey Round', exact: true }).first().click();
        await page.getByRole('heading', { name: 'Runs', exact: true }).waitFor();
        await page.screenshot({ path: path.join(input.output, 'staff-round.png'), fullPage: true });
        assert.deepEqual(errors, []);
        process.stdout.write(JSON.stringify({ archive, record, errors }));
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
