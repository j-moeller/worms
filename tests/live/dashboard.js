// The dashboard steps of the live journey (tests/live/test_devbox_journey.py), in a real
// Chromium against the deployed dashboard, as an administrator or a team member would do them.
//
// One request per run. The request is JSON on stdin, because it can hold passwords, and the
// answer is JSON on stdout. On failure: {"error": "..."} and exit code 1. Nothing here prints
// a password, and the screenshots mask every place that shows one.
//
// Request: {action, public_name, dashboard_ip, out, login_spacing_ms, ...the action's fields}.
const { chromium } = require("playwright");
const path = require("path");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const squash = (text) => text.replace(/\s+/g, " ").trim();

class Session {
  constructor(request) {
    this.request = request;
    this.base = `https://${request.public_name}`;
  }

  async open() {
    this.browser = await chromium.launch({
      // The port forward only matches traffic arriving from outside, so a browser on the node
      // has to go to the dashboard directly, under its public name.
      args: [`--host-resolver-rules=MAP ${this.request.public_name} ${this.request.dashboard_ip}`],
    });
    const context = await this.browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 900 } });
    this.page = await context.newPage();
    this.page.setDefaultTimeout(150000);
    this.page.setDefaultNavigationTimeout(150000);
    this.jsErrors = [];
    this.page.on("pageerror", (error) => this.jsErrors.push(String(error)));
  }

  close() {
    return this.browser.close();
  }

  path() {
    return new URL(this.page.url()).pathname;
  }

  async shot(name, options = {}) {
    if (name) await this.page.screenshot({ path: path.join(this.request.out, `${name}.png`), fullPage: true, ...options });
  }

  // A page that shows one-time passwords shows them twice: in the table and in the CSV block.
  passwordMask() {
    return { mask: [this.page.locator("main table tbody td"), this.page.locator("main pre")] };
  }

  async messages() {
    const texts = await this.page.$$eval(".alert, .message, .messages li, .errorlist li, .notice, [role=alert], .flash", (nodes) => nodes.map((n) => n.innerText));
    return [...new Set(texts.map(squash).filter(Boolean))];
  }

  async submit(selector) {
    const [response] = await Promise.all([this.page.waitForNavigation({ waitUntil: "load" }), this.page.click(selector)]);
    return response.status();
  }

  // Logins are rate limited per address, so space them out.
  async login(user, password) {
    await sleep(this.request.login_spacing_ms);
    await this.page.goto(`${this.base}/login/`, { waitUntil: "load" });
    await this.page.fill('input[name="username"]', user);
    await this.page.fill('input[name="password"]', password);
    return this.submit('main button[type="submit"]');
  }

  // The toggle switches with fetch(), not a page load: wait for the server's answer.
  async devView() {
    if ((await this.page.getAttribute("button[data-env-toggle]", "aria-pressed")) !== "true") {
      await Promise.all([
        this.page.waitForResponse((response) => new URL(response.url()).pathname === "/environment/"),
        this.page.click("button[data-env-toggle]"),
      ]);
    }
    await this.page.goto(`${this.base}/`, { waitUntil: "load" });
  }

  async devState() {
    const page = this.page;
    const command = page.locator("section[aria-labelledby=connect-title] pre code");
    return {
      version: squash(await page.innerText("section[aria-labelledby=score-title] .panel__meta")),
      score: squash(await page.innerText("section[aria-labelledby=score-title] .stat__value")),
      logs: await page.$$eval("details.log-entry", (nodes) => nodes.map((n) => n.textContent)),
      ssh_command: (await command.count()) ? squash(await command.first().innerText()) : null,
      keys: squash(await page.innerText("section[aria-labelledby=keys-title] .panel__meta")),
      vms: await page.$$eval("section[aria-labelledby=vms-title] tbody tr", (rows) => rows.map((row) => [...row.children].map((cell) => cell.innerText.trim()))),
    };
  }

  async setPassword(oldPassword, newPassword) {
    const names = await this.page.$$eval('main input[type="password"]', (inputs) => inputs.map((i) => i.name));
    for (const name of names) {
      await this.page.fill(`input[name="${name}"]`, /old|current/i.test(name) ? oldPassword : newPassword);
    }
    return this.submit('main button[type="submit"]');
  }

  // [[username, one-time password], ...] of the page shown once after creating accounts.
  passwordRows() {
    return this.page.$$eval("main table tbody tr", (rows) => rows.map((row) => [row.querySelector("th")?.innerText.trim(), row.querySelector("td")?.innerText.trim()]));
  }
}

const actions = {
  async admin_first_login(s, r) {
    const wrong = await s.login("admin", "definitely-not-the-password");
    const wrongPath = s.path();
    const wrongMessages = await s.messages();
    const status = await s.login("admin", r.otp);
    const firstPath = s.path();
    await s.shot("01-admin-forced-password-change", { fullPage: false });
    await s.page.goto(`${s.base}/staff/users/`);
    const beforeChangePath = s.path();
    await s.setPassword(r.otp, r.password);
    const afterPath = s.path();
    const pages = {};
    for (const p of ["/staff/", "/staff/users/", "/staff/teams/", "/staff/rounds/", "/topology/"]) {
      pages[p] = (await s.page.goto(s.base + p)).status();
    }
    return { wrong, wrongPath, wrongMessages, status, firstPath, beforeChangePath, afterPath, pages };
  },

  async admin_create_competition_and_teams(s, r) {
    await s.login("admin", r.password);
    await s.page.goto(`${s.base}/staff/`);
    await s.submit('main button:has-text("Create it")');
    const competition = await s.messages();
    await s.shot("02-dev-competition-created");
    const teams = {};
    for (const team of r.teams) {
      await s.page.goto(`${s.base}/staff/teams/`);
      await s.page.fill('input[name="name"]', team);
      await s.page.fill('input[name="max_members"]', String(r.max_members));
      await s.submit('main button:has-text("Create team")');
      teams[team] = await s.messages();
    }
    await s.page.goto(`${s.base}/staff/teams/`);
    await s.shot("03-teams-created");
    return { competition, teams };
  },

  // The first user through the form, the rest through the CSV import. Returns the one-time passwords.
  async admin_create_users(s, r) {
    await s.login("admin", r.password);
    const passwords = {};
    const [first, ...others] = r.users;
    await s.page.goto(`${s.base}/staff/users/`);
    await s.page.fill('input[name="email"]', first.email);
    await s.page.fill('input[name="first_name"]', first.first);
    await s.page.fill('input[name="last_name"]', first.last);
    const options = await s.page.$$eval('select[name="team"] option', (nodes) => nodes.map((n) => n.innerText.trim()));
    await s.page.selectOption('select[name="team"]', { label: options.find((o) => o.startsWith(first.team)) });
    await s.submit('main button:has-text("Create account")');
    for (const [user, password] of await s.passwordRows()) passwords[user] = password;
    await s.shot("04-user-created-passwords-hidden", s.passwordMask());

    const csv = ["Vorname,Nachname,E-Mail,Team", ...others.map((u) => `${u.first},${u.last},${u.email},${u.team}`)].join("\n") + "\n";
    await s.page.goto(`${s.base}/staff/users/`);
    await s.page.fill('textarea[name="csv_text"]', csv);
    await s.submit('main button:has-text("Import")');
    for (const [user, password] of await s.passwordRows()) passwords[user] = password;
    const problems = await s.page.$$eval("main ul.checklist li, main .errorlist li", (nodes) => nodes.map((n) => n.innerText.trim()));
    await s.shot("05-csv-import-passwords-hidden", s.passwordMask());
    return { passwords, problems };
  },

  async user_first_login_and_key(s, r) {
    const status = await s.login(r.user, r.otp);
    const firstPath = s.path();
    const beforeChange = s.path() === "/change-password/";
    if (r.shots) await s.shot("06-user-forced-password-change", { fullPage: false });
    if (beforeChange) await s.setPassword(r.otp, r.password);
    const afterPath = s.path();
    await s.devView();
    if (r.shots) await s.shot("07-dev-view-before-key");
    await s.page.fill("#key-name", r.key_name);
    await s.page.fill("#key-value", r.key);
    const keyStatus = await s.submit('form[action$="/devbox/ssh-keys/add/"] button[type="submit"]');
    const messages = await s.messages();
    const state = await s.devState();
    if (r.shots) await s.shot("08-ssh-key-added");
    return { status, firstPath, afterPath, keyStatus, messages, keys: state.keys, ssh_command: state.ssh_command };
  },

  async try_login(s, r) {
    const status = await s.login(r.user, r.password);
    return { status, path: s.path() };
  },

  async dev_view(s, r) {
    await s.login(r.user, r.password);
    await s.devView();
    if (r.shot) {
      for (const entry of await s.page.locator("details.log-entry").all()) await entry.evaluate((e) => { e.open = true; });
      await s.shot(r.shot);
    }
    return s.devState();
  },

  async reset_devbox(s, r) {
    await s.login(r.user, r.password);
    await s.devView();
    const before = await s.devState();
    await s.page.click("details.confirm > summary");
    await s.shot("12-reset-confirmation", { fullPage: false });
    const status = await s.submit('form[action$="/devbox/reset/"] button[type="submit"]');
    const messages = await s.messages();
    const after = await s.devState();
    await s.shot("13-after-reset-pressed");
    return { status, messages, before, after };
  },
};

(async () => {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const request = JSON.parse(input);
  const session = new Session(request);
  await session.open();
  try {
    const answer = await actions[request.action](session, request);
    console.log(JSON.stringify({ ...answer, jsErrors: session.jsErrors }));
  } finally {
    await session.close();
  }
})().catch((error) => {
  console.log(JSON.stringify({ error: String(error).split("\n")[0].slice(0, 400) }));
  process.exit(1);
});
