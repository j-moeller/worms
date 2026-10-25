/*
 * Progressive enhancement for the WORMS dashboard.
 *
 * Every feature here only improves a page that already works without
 * JavaScript: forms submit and redirect on the server, times and countdowns
 * are rendered server-side, the theme and the environment are set by form
 * posts. This file makes those nicer (live ticking, local time, instant theme
 * switch, an animated environment switch that swaps the page in place, copy
 * buttons, drag-and-drop upload, a self-refreshing leaderboard, a staff menu
 * that closes on outside-click). If any of it throws or is unsupported, the
 * server behaviour remains.
 *
 * No third-party code, no innerHTML of server data: nodes are built with
 * textContent / createElement, pages are swapped by moving parsed nodes.
 *
 * init() is idempotent: per-node binders guard with a data-enhanced attribute
 * and the interval pollers guard with module flags, so it is safe to call
 * again after the environment switch swaps part of the page in.
 */
(function () {
    "use strict";
    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));
    // Bind a node at most once across any number of init() runs.
    const once = (el) => {
        if (!el || el.hasAttribute("data-enhanced")) return false;
        el.setAttribute("data-enhanced", "1");
        return true;
    };

    // Server clock: honour the (possibly frozen, in debug) time the page was
    // rendered at, so countdowns match what the server believes.
    let clockOffset = 0;
    try {
        const iso = document.documentElement.getAttribute("data-now");
        if (iso) clockOffset = Date.parse(iso) - Date.now();
    } catch (e) { /* keep 0 */ }
    const serverNow = () => Date.now() + clockOffset;

    // ---- local time ------------------------------------------------------
    // Re-render <time data-localtime datetime="ISO"> in the viewer's locale.
    function localTimes(root) {
        $$("time[data-localtime]", root).forEach((el) => {
            const iso = el.getAttribute("datetime");
            if (!iso) return;
            const d = new Date(iso);
            if (isNaN(d)) return;
            el.textContent = d.toLocaleString([], {
                year: "numeric", month: "short", day: "2-digit",
                hour: "2-digit", minute: "2-digit",
            });
        });
    }

    // ---- countdowns ------------------------------------------------------
    // One global ticker; tick() re-queries every second, so a countdown that
    // is swapped into the page later (on an environment switch) is picked up.
    function fmt(ms) {
        let s = Math.max(0, Math.floor(ms / 1000));
        const d = Math.floor(s / 86400); s -= d * 86400;
        const h = Math.floor(s / 3600); s -= h * 3600;
        const m = Math.floor(s / 60); s -= m * 60;
        const pad = (n) => String(n).padStart(2, "0");
        const clock = pad(h) + ":" + pad(m) + ":" + pad(s);
        return d > 0 ? d + "d " + clock : clock;
    }
    function tick() {
        $$("[data-countdown]").forEach((el) => {
            const iso = el.getAttribute("datetime");
            if (!iso) return;
            el.textContent = fmt(Date.parse(iso) - serverNow());
        });
    }
    let countdownStarted = false;
    function startCountdowns() {
        if (countdownStarted) return;
        countdownStarted = true;
        tick();
        setInterval(tick, 1000);
    }

    // ---- copy to clipboard ----------------------------------------------
    function flash(button, text) {
        const prev = button.textContent;
        button.textContent = text;
        setTimeout(() => { button.textContent = prev; }, 1500);
    }
    async function copy(text, button) {
        try {
            await navigator.clipboard.writeText(text);
            flash(button, "Copied");
        } catch (e) {
            flash(button, "Press ⌘/Ctrl-C");
        }
    }
    function copyButtons(root) {
        if (!navigator.clipboard) return;
        // Explicit copy rows (e.g. a freshly issued token).
        $$("[data-copy] [data-copy-button]", root).forEach((button) => {
            if (!once(button)) return;
            const row = button.closest("[data-copy]");
            const value = $("[data-copy-value]", row);
            if (value) button.addEventListener("click", () => copy(value.textContent, button));
        });
        // Code blocks marked data-copy get a corner button.
        $$("pre.code[data-copy]", root).forEach((pre) => {
            if ($("[data-copy-button]", pre)) return;
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn--ghost btn--sm code__copy";
            button.setAttribute("data-copy-button", "");
            button.textContent = "Copy";
            button.addEventListener("click", () => copy(pre.querySelector("code").textContent, button));
            pre.style.position = "relative";
            pre.appendChild(button);
        });
    }

    // ---- instant theme switch -------------------------------------------
    // Clicking a design swaps the stylesheet at once, then persists the choice
    // in the background (the form would otherwise reload the page).
    function themePicker() {
        const form = $("[data-theme-picker]");
        if (!form || !once(form)) return;
        const link = $("link[data-theme-stylesheet]");
        form.addEventListener("click", (event) => {
            const button = event.target.closest(".theme-picker__option");
            if (!button || !link) return;
            event.preventDefault();
            const href = button.getAttribute("data-stylesheet");
            if (href) link.setAttribute("href", href);
            document.documentElement.setAttribute("data-theme", button.value);
            $$(".theme-picker__option", form).forEach((o) =>
                o.setAttribute("aria-pressed", o === button ? "true" : "false"));
            const body = new FormData(form);
            body.set("theme", button.value);
            fetch(form.action, {
                method: "POST", body: body, credentials: "same-origin",
                headers: { "X-Requested-With": "fetch" },
            }).catch(() => { /* the visual switch already happened */ });
        });
    }

    // ---- environment switch (Dev / Sandbox) -----------------------------
    // Flip data-env on <html> at once — the knob slides and the whole page
    // recolours from CSS — then persist and swap the environment-dependent
    // regions (header, main) in place, so there is no page reload. If the
    // network swap fails, fall back to a full load so content stays correct.
    function regions() { return $$("[data-env-swap]"); }
    function reflectToggle(button, dev) {
        button.setAttribute("aria-pressed", dev ? "true" : "false");
        button.value = dev ? "sandbox" : "dev";  // a later no-JS submit toggles back
    }
    async function switchEnvironment(button) {
        const form = button.form;
        if (!form) return;
        const toDev = document.documentElement.getAttribute("data-env") !== "dev";
        const value = toDev ? "dev" : "sandbox";
        document.documentElement.setAttribute("data-env", value);  // instant restyle + slide
        reflectToggle(button, toDev);
        try {
            const token = form.querySelector("input[name=csrfmiddlewaretoken]");
            const body = new FormData();
            body.set("environment", value);
            if (token) body.set("csrfmiddlewaretoken", token.value);
            await fetch(form.action, {
                method: "POST", body: body, credentials: "same-origin",
                redirect: "manual", headers: { "X-Requested-With": "fetch" },
            });
            const response = await fetch(location.href, {
                credentials: "same-origin", headers: { "X-Requested-With": "fetch" },
            });
            if (!response.ok) throw new Error("reload");
            const doc = new DOMParser().parseFromString(await response.text(), "text/html");
            // Trust the server, not the optimistic flip. If the page did not come
            // back in the environment we asked for — the session expired and the
            // POST redirected to the login page, or the switch was refused — a
            // full reload keeps the content and the chrome in sync.
            if (doc.documentElement.getAttribute("data-env") !== value) {
                location.reload();
                return;
            }
            // A page with a script of its own (the round player) cannot be
            // swapped in: a parsed-in script never runs. Load it instead.
            if (doc.querySelector("[data-env-swap] script[src]") || $("[data-env-swap] script[src]")) {
                location.reload();
                return;
            }
            regions().forEach((current) => {
                const key = current.getAttribute("data-env-swap");
                const fresh = doc.querySelector('[data-env-swap="' + key + '"]');
                if (fresh) current.replaceWith(fresh);
            });
            if (doc.title) document.title = doc.title;
            init();  // bind the freshly swapped-in nodes (idempotent)
        } catch (e) {
            location.reload();  // correctness over cleverness
        }
    }
    function envToggle() {
        const button = $("[data-env-toggle]");
        if (!button || !once(button)) return;
        button.addEventListener("click", (event) => {
            if (!button.form) return;  // no form: let the browser do whatever it would
            event.preventDefault();
            switchEnvironment(button);
        });
    }

    // ---- staff menu ------------------------------------------------------
    // The <details> dropdown already works; close it on outside-click / Escape.
    function staffMenu(root) {
        $$("[data-staff-menu]", root).forEach((menu) => {
            if (!once(menu)) return;
            document.addEventListener("click", (event) => {
                if (menu.open && !menu.contains(event.target)) menu.open = false;
            });
            menu.addEventListener("keydown", (event) => {
                if (event.key === "Escape" && menu.open) {
                    menu.open = false;
                    const summary = $("summary", menu);
                    if (summary) summary.focus();
                }
            });
        });
    }

    // ---- upload: filename, drag and drop, busy state --------------------
    // The form still submits natively (multipart); the server stores the file
    // and redirects with a message. We only improve the picker.
    function uploadForm(root) {
        const form = $("[data-upload]", root);
        if (!form || !once(form)) return;
        const input = $("[data-file]", form);
        const text = $("[data-dropzone-text]", form);
        const fileOut = $("[data-dropzone-file]", form);
        const zone = $("[data-dropzone]", form);
        const status = $("[data-upload-status]", form);
        const max = parseInt(form.getAttribute("data-max") || "0", 10);

        function show() {
            const file = input.files && input.files[0];
            if (file && fileOut) {
                fileOut.textContent = file.name + " · " + (file.size / 1048576).toFixed(2) + " MiB";
                fileOut.hidden = false;
                if (text) text.hidden = true;
                if (status && max && file.size > max) {
                    status.textContent = "Larger than the limit"; status.className = "upload__status is-error";
                } else if (status) {
                    status.textContent = "Ready"; status.className = "upload__status";
                }
            }
        }
        if (input) input.addEventListener("change", show);
        if (zone) {
            ["dragover", "dragenter"].forEach((ev) => zone.addEventListener(ev, (e) => {
                e.preventDefault(); zone.classList.add("is-over");
            }));
            ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("is-over")));
            zone.addEventListener("drop", (e) => {
                e.preventDefault();
                if (e.dataTransfer && e.dataTransfer.files.length && input) {
                    input.files = e.dataTransfer.files;
                    show();
                }
            });
        }
        form.addEventListener("submit", () => {
            if (status) { status.textContent = "Uploading…"; status.className = "upload__status is-busy"; }
        });
    }

    // ---- leaderboard self-refresh ---------------------------------------
    // One global poller; it re-finds the section each cycle, so it survives an
    // environment swap and refreshes whichever ranking is on the page now.
    let refreshStarted = false;
    function autoRefresh() {
        if (refreshStarted) return;
        refreshStarted = true;
        setInterval(async () => {
            const section = $("[data-autorefresh]");
            if (!section || !section.id) return;
            try {
                const response = await fetch(location.href, {
                    credentials: "same-origin", headers: { "X-Requested-With": "fetch" },
                });
                if (!response.ok) return;
                const doc = new DOMParser().parseFromString(await response.text(), "text/html");
                const fresh = doc.getElementById(section.id);
                const current = document.getElementById(section.id);
                if (fresh && current) {
                    current.replaceWith(fresh);
                    localTimes(document.getElementById(section.id));
                }
            } catch (e) { /* keep the current table */ }
        }, 30000);
    }

    function init() {
        [
            () => localTimes(), startCountdowns, () => copyButtons(), themePicker,
            envToggle, () => staffMenu(), () => uploadForm(), autoRefresh,
        ].forEach((fn) => {
            try { fn(); } catch (e) { /* one broken enhancement must not stop the others */ }
        });
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
