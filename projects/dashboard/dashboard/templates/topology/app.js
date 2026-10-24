/* The TOPOLOGY view: the infrastructure (live) and the round player.
 *
 * Data (JSON, staff only; docs/visualization.md):
 *   data-model     the live infrastructure model (dashboard/infra_view.py)
 *   data-runs      rounds of the current competition and their runs (the tabs)
 *   data-timeline  one run's timeline (orchestrator/rounds/timeline.py); the
 *                  all-zero uuid in the URL is replaced by the run's
 *
 * The picture has a fixed layout: the Proxmox host, the orchestrator and the
 * dashboard on top, the database in the middle, scoring and logging below it,
 * then the devboxes, the sandbox and (to the right) the private internet. Only
 * operational connections are drawn, never a management network.
 *
 * Selecting a round and a team replays that run: connections as dots along
 * the edges, events as icons on the VMs (click: pause + details), VM power
 * state and captures as they happen, with a seekable timeline.
 *
 * Everything the view shows can be saved as one JSON file ("Download JSON":
 * the infrastructure model + the selected run's timeline) and viewed again
 * later ("Open JSON…", read locally, nothing is uploaded): the replay bundle,
 * docs/visualization.md.
 *
 * SECURITY: flows, logs and submitted flags come from the (malicious)
 * sandbox, and an opened file can come from anywhere. Every string is
 * rendered with textContent / text nodes; nothing from the data is ever
 * parsed as HTML. Server responses and files alike pass through the same
 * sanitizer (cleanModel / cleanTimeline): known fields only, coerced types,
 * capped sizes.
 */
(function () {
    "use strict";

    const root = document.getElementById("topo");
    if (!root) return;
    const $ = (id) => document.getElementById(id);
    const SVG_NS = "http://www.w3.org/2000/svg";
    const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
    const cfg = {
        model: root.dataset.model,
        runs: root.dataset.runs,
        timeline: root.dataset.timeline,
        refreshMs: Number(root.dataset.refreshMs || 60000),
    };

    // ------------------------------------------------------------------
    // DOM helpers (text only -- see SECURITY above)
    // ------------------------------------------------------------------

    function append(node, children) {
        for (const child of children) {
            if (child === null || child === undefined || child === false) continue;
            if (Array.isArray(child)) append(node, child);
            else if (child instanceof Node) node.appendChild(child);
            else node.appendChild(document.createTextNode(String(child)));
        }
        return node;
    }

    function setAttrs(node, attrs) {
        for (const [key, value] of Object.entries(attrs || {})) {
            if (value === null || value === undefined || value === false) continue;
            if (key === "class") node.setAttribute("class", value);
            else if (key === "text") node.textContent = String(value);
            else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? "" : String(value));
        }
        return node;
    }

    function el(tag, attrs, ...children) {
        return append(setAttrs(document.createElement(tag), attrs), children);
    }

    function sv(tag, attrs, ...children) {
        return append(setAttrs(document.createElementNS(SVG_NS, tag), attrs), children);
    }

    function clear(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
        return node;
    }

    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

    function fmtTime(t) {
        if (t === null || t === undefined || Number.isNaN(t)) return "--:--";
        const sign = t < 0 ? "−" : "";
        let s = Math.abs(t);
        const h = Math.floor(s / 3600);
        s -= h * 3600;
        const m = Math.floor(s / 60);
        const sec = Math.floor(s - m * 60);
        const mm = String(m).padStart(2, "0");
        const ss = String(sec).padStart(2, "0");
        return h ? `${sign}${h}:${mm}:${ss}` : `${sign}${mm}:${ss}`;
    }

    function fmtBytes(n) {
        if (!n) return "0 B";
        const units = ["B", "KiB", "MiB", "GiB"];
        let i = 0;
        while (n >= 1024 && i < units.length - 1) {
            n /= 1024;
            i++;
        }
        return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
    }

    // A date for display; a string that is not one is shown as it is.
    function fmtDate(iso) {
        const d = new Date(iso);
        return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
    }

    function shorten(text, max) {
        text = String(text || "");
        return text.length > max ? `${text.slice(0, max - 1)}…` : text;
    }

    function ago(iso) {
        if (!iso) return "never";
        const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
        if (s < 90) return `${Math.round(s)} s ago`;
        if (s < 5400) return `${Math.round(s / 60)} min ago`;
        return `${Math.round(s / 3600)} h ago`;
    }

    // Index of the first element with key(el) >= value (sorted arrays).
    function lowerBound(arr, value, key) {
        let lo = 0;
        let hi = arr.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (key(arr[mid]) < value) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }

    async function getJSON(url) {
        const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
        let body = null;
        try {
            body = await response.json();
        } catch (e) {
            body = null;
        }
        if (!response.ok) {
            const error = new Error((body && body.error) || `HTTP ${response.status}`);
            error.status = response.status;
            error.body = body;
            throw error;
        }
        return body;
    }

    // ------------------------------------------------------------------
    // The replay bundle: sanitizer, export, import
    // ------------------------------------------------------------------

    const BUNDLE_FORMAT = "worms-topology-replay";
    const BUNDLE_VERSION = 1;
    const MAX_FILE_BYTES = Number(root.dataset.maxFileBytes) || 64 * 1024 * 1024;
    const CAP = { nodes: 10000, list: 100000, events: 200000, flows: 500000, rules: 4000, small: 512, status: 10000 };
    const UNSAFE_KEYS = new Set(["__proto__", "constructor", "prototype"]);
    const KNOWN_ROLES = new Set(["proxmox", "orchestrator", "dashboard", "database", "scoring", "logging", "monitor", "wazuh",
        "dns", "private_internet", "sandbox", "sandbox_gw", "devbox", "devbox_gw", "ssh_gateway", "other"]);
    const T_LIMIT = 1e8; // seconds; anything beyond is not a round

    const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
    const bool = (v) => v === true || v === 1 || v === "1" || v === "true";

    function txt(v, max = 4096) {
        if (v === null || v === undefined) return "";
        const s = typeof v === "string" ? v : typeof v === "number" || typeof v === "boolean" ? String(v) : "";
        return s.length > max ? s.slice(0, max) : s;
    }

    function optTxt(v, max) {
        return v === null || v === undefined || v === "" ? null : txt(v, max);
    }

    function num(v, fallback = null) {
        const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
        return Number.isFinite(n) && Math.abs(n) <= T_LIMIT * 1000 ? n : fallback;
    }

    function time(v) {
        const t = num(v);
        return t !== null && Math.abs(t) <= T_LIMIT ? t : null;
    }

    function list(v, name, max, fn) {
        if (v === null || v === undefined) return [];
        if (!Array.isArray(v)) throw new Error(`${name} must be a list`);
        if (v.length > max) throw new Error(`${name} has more than ${max} entries`);
        const out = [];
        for (const item of v) {
            const x = fn(item);
            if (x !== null && x !== undefined) out.push(x);
        }
        return out;
    }

    function dict(v, max, fn) {
        const out = {};
        if (!isObj(v)) return out;
        let n = 0;
        for (const [key, value] of Object.entries(v)) {
            if (UNSAFE_KEYS.has(key)) continue;
            if (++n > max) break;
            const x = fn(value);
            if (x !== null && x !== undefined) out[key] = x;
        }
        return out;
    }

    const strings = (v, name, max = CAP.small) => list(v, name, max, (x) => (typeof x === "string" || typeof x === "number" ? txt(x, 256) : null));

    const cleanRule = (r) => (isObj(r) ? {
        direction: txt(r.direction, 16), action: txt(r.action, 16), proto: txt(r.proto, 16), dport: txt(r.dport, 256),
        sport: txt(r.sport, 256), source: txt(r.source, 1024), dest: txt(r.dest, 1024), macro: txt(r.macro, 64),
        iface: txt(r.iface, 32), group: optTxt(r.group, 128), comment: txt(r.comment, 512),
    } : null);

    function cleanFirewall(f) {
        f = isObj(f) ? f : {};
        return {
            enabled: bool(f.enabled), policy_in: txt(f.policy_in, 16), policy_out: txt(f.policy_out, 16), ipfilter: bool(f.ipfilter),
            groups: list(f.groups, "firewall.groups", CAP.small, (g) => (isObj(g) ? { name: txt(g.name, 128), iface: txt(g.iface, 32), comment: txt(g.comment, 512) } : null)),
            rules: list(f.rules, "firewall.rules", CAP.rules, cleanRule),
        };
    }

    // The infrastructure model (dashboard/infra_view.py + the view's metadata).
    function cleanModel(m) {
        if (!isObj(m)) throw new Error("model must be an object");
        if (!Array.isArray(m.nodes)) throw new Error("model.nodes must be a list");
        const host = isObj(m.host) ? m.host : {};
        return {
            host: {
                node: optTxt(host.node, 256), version: optTxt(host.version, 64),
                firewall_services: list(host.firewall_services, "host.firewall_services", CAP.small,
                    (x) => (isObj(x) ? { name: txt(x.name, 64), state: txt(x.state, 64), active_state: txt(x.active_state, 64) } : null)),
                errors: list(host.errors, "host.errors", CAP.list, (e) => (isObj(e) ? { path: txt(e.path, 512), error: txt(e.error, 1024) } : null)),
            },
            nodes: list(m.nodes, "model.nodes", CAP.nodes, (n) => (isObj(n) && n.id !== undefined && n.id !== null ? {
                id: txt(n.id, 256), name: txt(n.name, 256) || txt(n.id, 256), role: KNOWN_ROLES.has(n.role) ? n.role : "other",
                status: txt(n.status, 64) || "unknown", active: bool(n.active), vmid: num(n.vmid), type: optTxt(n.type, 16),
                pool: txt(n.pool, 128), tags: strings(n.tags, "tags"), aux: bool(n.aux), team: optTxt(n.team, 128),
                devbox_role: ["a", "b", "c"].includes(n.devbox_role) ? n.devbox_role : null,
                addresses: strings(n.addresses, "addresses"),
                nics: list(n.nics, "nics", CAP.small, (c) => (isObj(c) ? {
                    name: txt(c.name, 32), bridge: txt(c.bridge, 64), kind: txt(c.kind, 32) || "other", ip: txt(c.ip, 64),
                    cidr: txt(c.cidr, 64), network: txt(c.network, 64), firewall: bool(c.firewall), link_down: bool(c.link_down),
                } : null)),
                firewall: cleanFirewall(n.firewall),
            } : null)),
            groups: list(m.groups, "model.groups", CAP.nodes, (g) => (isObj(g) && (g.kind === "sandbox" || g.kind === "devbox") ? {
                id: txt(g.id, 256), kind: g.kind, team: optTxt(g.team, 128), team_name: optTxt(g.team_name, 256),
                label: txt(g.label, 256), gateway: optTxt(g.gateway, 256), members: strings(g.members, "members", CAP.nodes),
            } : null)),
            networks: list(m.networks, "model.networks", CAP.nodes, (n) => (isObj(n) && (n.kind === "sandbox" || n.kind === "devbox") ? {
                id: txt(n.id, 256), bridge: txt(n.bridge, 64), kind: n.kind, team: optTxt(n.team, 128), group: optTxt(n.group, 256),
                cidr: txt(n.cidr, 64), comment: txt(n.comment, 512), members: strings(n.members, "members", CAP.nodes),
            } : null)),
            edges: list(m.edges, "model.edges", CAP.list, (e) => (isObj(e) ? {
                id: txt(e.id, 600), source: txt(e.source, 256), target: txt(e.target, 256), kind: txt(e.kind, 32),
                verified: bool(e.verified), label: optTxt(e.label, 128), group: optTxt(e.group, 128), comment: txt(e.comment, 512),
                network: optTxt(e.network, 256), targets: strings(e.targets, "targets"),
            } : null)),
            security_groups: dict(m.security_groups, 2000, (g) => (isObj(g) ? { comment: txt(g.comment, 512), rules: list(g.rules, "security group rules", CAP.rules, cleanRule) } : null)),
            unresolved: { database_sources: strings(isObj(m.unresolved) ? m.unresolved.database_sources : [], "unresolved") },
            collected_at: optTxt(m.collected_at, 64), stale: bool(m.stale), collection_error: txt(m.collection_error, 2048),
        };
    }

    // One run's timeline (orchestrator/rounds/timeline.py), sorted, with
    // event ids and the time range recomputed.
    function cleanTimeline(tl) {
        if (!isObj(tl)) throw new Error("timeline must be an object");
        const r = isObj(tl.run) ? tl.run : {};
        const events = list(tl.events, "timeline.events", CAP.events, (e) => {
            const t = isObj(e) ? time(e.t) : null;
            return t === null ? null : { t, kind: txt(e.kind, 64), node: optTxt(e.node, 256), vm: optTxt(e.vm, 256), data: isObj(e.data) ? e.data : {} };
        }).sort((a, b) => a.t - b.t);
        events.forEach((e, i) => { e.id = i; });
        const flows = list(tl.flows, "timeline.flows", CAP.flows, (f) => {
            const t = isObj(f) ? time(f.t) : null;
            return t === null ? null : {
                t, d: clamp(num(f.d, 0), 0, T_LIMIT), src: txt(f.src, 256), dst: txt(f.dst, 256), src_role: txt(f.src_role, 64),
                dst_role: txt(f.dst_role, 64), src_name: txt(f.src_name, 256), dst_name: txt(f.dst_name, 256), kind: txt(f.kind, 32) || "other",
                src_port: num(f.src_port), dst_port: num(f.dst_port), transport: txt(f.transport, 32), service: optTxt(f.service, 128),
                conn_state: optTxt(f.conn_state, 16), bytes: num(f.bytes, 0), pkts: num(f.pkts, 0), uid: txt(f.uid, 128),
            };
        }).sort((a, b) => a.t - b.t);
        let lo = Math.min(0, time(r.t_min) ?? 0);
        let hi = Math.max(time(r.duration) ?? 0, time(r.t_max) ?? 0);
        for (const e of events) { lo = Math.min(lo, e.t); hi = Math.max(hi, e.t); }
        for (const f of flows) { lo = Math.min(lo, f.t); hi = Math.max(hi, f.t + f.d); }
        return {
            run: {
                uuid: txt(r.uuid, 64), team: txt(r.team, 256), team_uuid: txt(r.team_uuid, 64), team_slug: optTxt(r.team_slug, 128),
                round: txt(r.round, 256), state: txt(r.state, 32), position: num(r.position, 0),
                worm_start_at: optTxt(r.worm_start_at, 64), ends_at: optTxt(r.ends_at, 64), start_vm: optTxt(r.start_vm, 256),
                duration: time(r.duration), t_min: lo, t_max: hi,
            },
            nodes: list(tl.nodes, "timeline.nodes", CAP.nodes, (n) => (isObj(n) && n.id !== undefined && n.id !== null ? {
                id: txt(n.id, 256), label: txt(n.label, 256) || txt(n.id, 256), vmid: num(n.vmid), addresses: strings(n.addresses, "addresses"),
                networks: list(n.networks, "networks", CAP.small, (x) => (isObj(x) ? { name: txt(x.name, 64), cidr: optTxt(x.cidr, 64) } : null)),
                start_vm: bool(n.start_vm), captured: bool(n.captured), captured_at: time(n.captured_at),
            } : null)),
            networks: list(tl.networks, "timeline.networks", CAP.nodes, (n) => (isObj(n) ? { id: txt(n.id, 64), label: txt(n.label, 64), cidr: optTxt(n.cidr, 64) } : null)),
            edges: list(tl.edges, "timeline.edges", CAP.list, (e) => (isObj(e) ? { source: txt(e.source, 256), target: txt(e.target, 64), network: txt(e.network, 64) } : null)),
            vm_status: dict(tl.vm_status, CAP.nodes, (points) => (Array.isArray(points) ? points.slice(0, CAP.status)
                .map((x) => (Array.isArray(x) && time(x[0]) !== null ? [time(x[0]), txt(x[1], 32)] : null))
                .filter(Boolean).sort((a, b) => a[0] - b[0]) : null)),
            events,
            flows,
            captures: list(tl.captures, "timeline.captures", CAP.list, (c) => (isObj(c) && time(c.t) !== null ? { t: time(c.t), vm: optTxt(c.vm, 256), team: num(c.team) } : null)),
            truncated: isObj(tl.truncated) ? { flows: bool(tl.truncated.flows), flag_submissions: bool(tl.truncated.flag_submissions), logs: bool(tl.truncated.logs) } : {},
        };
    }

    // A replay bundle, as written by makeBundle (any source: validate it).
    function readBundle(raw) {
        if (!isObj(raw)) throw new Error("not a JSON object");
        if (raw.format !== BUNDLE_FORMAT) throw new Error(`not a WORMS topology file (format must be "${BUNDLE_FORMAT}")`);
        if (!Number.isInteger(raw.version) || raw.version < 1 || raw.version > BUNDLE_VERSION) {
            throw new Error(`unsupported version ${txt(raw.version, 16) || "?"} (this page reads version ${BUNDLE_VERSION})`);
        }
        const source = isObj(raw.source) ? raw.source : {};
        return {
            model: cleanModel(raw.model),
            timeline: raw.timeline === null || raw.timeline === undefined ? null : cleanTimeline(raw.timeline),
            exported_at: optTxt(raw.exported_at, 64),
            source: { mode: optTxt(source.mode, 16), run: optTxt(source.run, 64), from_file: optTxt(source.from_file, 256) },
        };
    }

    function makeBundle() {
        return {
            format: BUNDLE_FORMAT,
            version: BUNDLE_VERSION,
            exported_at: new Date().toISOString(),
            source: {
                mode: S.mode,
                run: S.mode === "round" ? S.runUuid : S.mode === "file" && S.file ? S.file.source.run : null,
                from_file: S.mode === "file" && S.file ? S.file.name : null,
            },
            model: S.model || cleanModel({ nodes: [] }),
            timeline: S.mode === "live" ? null : S.timeline,
        };
    }

    function bundleName() {
        const tl = S.mode === "live" ? null : S.timeline;
        const parts = tl ? ["worms", tl.run.round, tl.run.team] : ["worms", "infrastructure"];
        parts.push(new Date().toISOString().slice(0, 19));
        return `${parts.join("-").replace(/[^A-Za-z0-9._-]+/g, "_")}.json`;
    }

    function downloadBundle() {
        if (!S.model && !S.timeline) return notice("Nothing to download yet.", true);
        const blob = new Blob([JSON.stringify(makeBundle())], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = el("a", { href: url, download: bundleName(), style: "display: none" });
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
    }

    async function openFile(file) {
        if (!file) return;
        try {
            if (file.size > MAX_FILE_BYTES) throw new Error(`too large (${fmtBytes(file.size)}; at most ${fmtBytes(MAX_FILE_BYTES)})`);
            let raw;
            try {
                raw = JSON.parse(await file.text());
            } catch (e) {
                throw new Error("not valid JSON");
            }
            S.file = Object.assign({ name: txt(file.name, 200) || "replay.json" }, readBundle(raw));
            notice(null);
            selectFile();
        } catch (error) {
            notice(`Could not open ${txt(file.name, 200)}: ${error.message}`, true);
        }
    }

    function notice(text, isError) {
        const banner = $("topo-banner");
        banner.hidden = !text;
        banner.classList.toggle("error", !!isError);
        banner.textContent = text || "";
    }

    // ------------------------------------------------------------------
    // Vocabulary: roles, event kinds, flow kinds
    // ------------------------------------------------------------------

    const ROLE_LABEL = {
        proxmox: "Proxmox host", orchestrator: "orchestrator", dashboard: "dashboard", database: "database",
        scoring: "scoring", logging: "logging", monitor: "monitor", wazuh: "wazuh", dns: "DNS",
        private_internet: "private internet", sandbox: "sandbox VM", sandbox_gw: "sandbox gateway",
        devbox: "devbox VM", devbox_gw: "devbox gateway", ssh_gateway: "devbox SSH gateway", other: "other",
    };

    const KIND_COLOR = {
        lan: "--c-lan", scoring: "--c-scoring", logging: "--c-logging", private_internet: "--c-pi", dns: "--c-pi",
        proxmox: "--c-other", sandbox_gw: "--c-service", other: "--c-other",
    };

    // Icons: paths in a 16x16 box centred on 0,0 (stroked white on a coloured disc).
    const ICON = {
        power: { d: "M0,-4.6 L0,-0.6 M-3.1,-2.9 A4.2,4.2 0 1,0 3.1,-2.9" },
        key: { d: "M-4.2,0 a2,2 0 1,0 4,0 a2,2 0 1,0 -4,0 M-0.2,0 L4.6,0 M3,0 L3,2.2 M4.6,0 L4.6,2.2" },
        flag: { d: "M-3,4.5 L-3,-4.5", solid: "M-3,-4.5 L4.2,-2.4 L-3,-0.3 Z" },
        check: { d: "M-3.6,0.2 L-1,2.9 L3.9,-3" },
        cross: { d: "M-3,-3 L3,3 M3,-3 L-3,3" },
        doc: { d: "M-3.4,-3.2 L3.4,-3.2 M-3.4,0 L3.4,0 M-3.4,3.2 L1.6,3.2" },
        play: { solid: "M-2.6,-4.2 L4.2,0 L-2.6,4.2 Z" },
        down: { d: "M0,-4.2 L0,3.2 M-3.2,0.4 L0,3.6 L3.2,0.4" },
        up: { d: "M0,4.2 L0,-3.2 M-3.2,-0.4 L0,-3.6 L3.2,-0.4" },
        ring: { d: "M-3.4,0 a3.4,3.4 0 1,0 6.8,0 a3.4,3.4 0 1,0 -6.8,0 M0,-3.4 L0,-1.2" },
        bang: { d: "M0,-4.4 L0,1.2 M0,3.6 L0,3.8" },
    };

    // How each event is shown: on which node(s), with which icon and colour.
    function eventLook(e) {
        const d = e.data || {};
        switch (e.kind) {
            case "state":
                return { icon: "ring", color: "--c-state", label: `run state → ${d.state || "?"}` };
            case "vm_stopped":
                return { icon: "power", color: "--off", label: "VM stopped (game plan)" };
            case "vm_started":
                return { icon: "power", color: "--ok", label: "VM started (game plan)" };
            case "secret_released":
                return { icon: "key", color: "--warn", label: "secret released (a new flag in the VM)" };
            case "worm_deployed":
                return { icon: "down", color: "--c-lan", label: "worm deployed to the start VM" };
            case "worm_started":
                return { icon: "play", color: "--c-lan", label: "worm started" };
            case "captured":
                return { icon: "flag", color: "--c-flag", label: "captured — its flag was scored" };
            case "flag_submitted": {
                const ok = d.result === "captured";
                const labels = {
                    captured: "flag submitted — captured", duplicate: "flag submitted — already captured",
                    not_counted: "flag submitted — outside the live window", invalid: "flag submitted — invalid",
                    pending: "flag submitted — not processed yet",
                };
                return { icon: ok ? "check" : "cross", color: ok ? "--c-flag" : "--c-scoring", label: labels[d.result] || "flag submitted" };
            }
            case "log_received":
                return { icon: "doc", color: "--c-logging", label: d.stored ? "log received (stored)" : "log received" };
            case "error":
                return { icon: "bang", color: "--bad", label: "error" };
            default:
                return { icon: "ring", color: "--c-other", label: e.kind };
        }
    }

    // The nodes an event is shown on (scene ids are resolved later).
    function eventTargets(e) {
        const d = e.data || {};
        const targets = [];
        if (e.kind === "state" || e.kind === "error") {
            targets.push("role:orchestrator");
            if (e.kind === "state" && ["preparing", "checking", "deploying", "tearing_down"].includes(d.state)) targets.push("role:proxmox");
        } else if (e.node) {
            targets.push(e.node);
        }
        if ((e.kind === "flag_submitted" || e.kind === "log_received") && d.source_vm) targets.push(d.source_vm);
        return targets;
    }

    const FAILED_STATES = new Set(["S0", "REJ", "RSTOS0", "RSTRH", "SH", "SHR", "OTH"]);

    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------

    const S = {
        model: null, // the model shown (live, or from the opened file)
        liveModel: null, // the latest snapshot from the server
        modelError: null,
        file: null, // an opened replay bundle {name, model, timeline, exported_at, source}
        runs: null,
        runsError: null,
        mode: "live", // "live" | "round" | "file"
        roundId: null,
        runUuid: null,
        timeline: null,
        timelineError: null,
        loading: false,
        scene: null,
        showAux: false,
        view: { k: 1, x: 0, y: 0, user: false },
        needsFit: true,
        playerFor: null, // the run the player controls were set up for
        teamName: null, // kept when switching rounds
        t: 0,
        playing: false,
        speed: 10,
        lastFrame: 0,
        selected: null, // {type: "node"|"event"|"flow"|"edge"|"group", ...}
        colors: {},
        dots: [], // screen positions of the dots drawn in the last frame
        badges: new Map(),
        lastStatusKey: "",
        feedIndex: -1,
    };

    // The player is on: a run (from the server or a file) is shown.
    const replaying = () => S.mode !== "live" && !!S.timeline;

    function readColors() {
        const style = getComputedStyle(root);
        const names = ["--c-lan", "--c-scoring", "--c-logging", "--c-pi", "--c-other", "--c-service", "--c-flag", "--c-state",
            "--c-db", "--c-devbox", "--ok", "--off", "--warn", "--bad", "--panel", "--ink", "--muted", "--line", "--accent"];
        for (const name of names) S.colors[name] = style.getPropertyValue(name).trim() || "#888";
    }

    // ------------------------------------------------------------------
    // Scene: the fixed layout
    // ------------------------------------------------------------------

    const CARD = { w: 150, h: 46 };
    const VM_CARD = { w: 128, h: 40 };
    const PI_CARD = { w: 180, h: 32 };

    function placeholder(role) {
        return {
            id: `role:${role}`, name: role, role, status: "absent", active: false, aux: false,
            nics: [], addresses: [], firewall: { groups: [], rules: [] }, absent: true,
        };
    }

    function buildScene(model, tl, opts) {
        const scene = {
            nodes: new Map(), // scene id -> scene node
            order: [],
            groups: [],
            hubs: new Map(),
            edges: [],
            alias: new Map(),
            byName: new Map(),
            byRole: new Map(),
            pi: null,
            lane: 0,
            bounds: { x0: 0, y0: 0, x1: 0, y1: 0 },
        };
        const mnodes = (model && model.nodes) || [];
        const byRole = {};
        for (const n of mnodes) (byRole[n.role] = byRole[n.role] || []).push(n);
        const first = (role) => (byRole[role] && byRole[role][0]) || placeholder(role);

        function addNode(info, x, y, size, extra) {
            const node = Object.assign({
                id: info.id, info, role: info.role, x, y, w: size.w, h: size.h,
                title: info.name, kind: info.role === "proxmox" ? "host" : "vm",
            }, extra || {});
            scene.nodes.set(node.id, node);
            scene.order.push(node);
            if (info.name && !scene.byName.has(info.name)) scene.byName.set(info.name, node.id);
            if (!scene.byRole.has(node.role)) scene.byRole.set(node.role, []);
            scene.byRole.get(node.role).push(node.id);
            scene.alias.set(`role:${node.role}`, scene.byRole.get(node.role)[0]);
            return node;
        }

        // --- the untrusted area first: its width fixes the top rows' spread.
        const GROUPS_TOP = 500;
        const LEFT = 40;

        // Devboxes (left column): one box per team, A - B - C.
        const devGroups = ((model && model.groups) || []).filter((g) => g.kind === "devbox")
            .filter((g) => opts.mode !== "round" || g.team === opts.teamSlug);
        const DEV_W = 500;
        let devY = GROUPS_TOP;
        const devboxBoxes = [];
        for (const g of devGroups) {
            const box = { id: g.id, kind: "devbox", x: LEFT, y: devY, w: DEV_W, h: 132, label: g.label, info: g };
            const members = g.members.map((id) => mnodes.find((n) => n.id === id)).filter(Boolean);
            const gap = (DEV_W - members.length * VM_CARD.w) / (members.length + 1);
            members.forEach((m, i) => {
                const cx = LEFT + gap * (i + 1) + VM_CARD.w * i + VM_CARD.w / 2;
                addNode(m, cx, devY + 82, VM_CARD, { group: g.id, sub: devboxSub(m) });
            });
            devboxBoxes.push(box);
            scene.groups.push(box);
            devY += box.h + 26;
        }
        const devBottom = devboxBoxes.length ? devY - 26 : GROUPS_TOP;

        // Sandbox (centre): from the timeline in round mode, else the live VMs.
        const SB_X = LEFT + (devboxBoxes.length ? DEV_W + 50 : 0);
        let sbVMs;
        let sbNets;
        let sbMembership; // vm id -> [net id]
        if (opts.mode === "round" && tl) {
            sbVMs = tl.nodes.map((n) => ({
                id: n.id, name: n.label, role: "sandbox", vmid: n.vmid, run: n, status: "running", active: true,
                addresses: n.addresses || [], networks: n.networks || [], tags: [], pool: "sandbox",
            }));
            sbNets = (tl.networks || []).map((n) => ({ id: `net:${n.id}`, bridge: n.label || n.id, cidr: n.cidr, kind: "sandbox" }));
            sbMembership = {};
            for (const e of tl.edges || []) (sbMembership[e.source] = sbMembership[e.source] || []).push(`net:${e.target}`);
        } else {
            sbVMs = byRole.sandbox || [];
            sbNets = ((model && model.networks) || []).filter((n) => n.kind === "sandbox");
            sbMembership = {};
            for (const net of sbNets) for (const m of net.members) (sbMembership[m] = sbMembership[m] || []).push(net.id);
        }
        const perRow = clamp(Math.ceil(sbVMs.length / 2), 3, 7);
        const SB_W = Math.max(620, perRow * (VM_CARD.w + 16) + 40);
        const rowsEach = Math.max(1, Math.ceil(sbVMs.length / 2 / perRow));
        const SB_H = Math.max(240, 100 + (2 * rowsEach) * (VM_CARD.h + 30) + 40);
        const sbBox = {
            id: "group:sandbox", kind: "sandbox", x: SB_X, y: GROUPS_TOP, w: SB_W, h: SB_H,
            label: opts.mode === "round" && tl ? `SANDBOX · ${tl.run.team} · ${tl.run.round}` : "SANDBOX",
            info: ((model && model.groups) || []).find((g) => g.id === "group:sandbox") || { id: "group:sandbox", kind: "sandbox", members: [] },
            empty: sbVMs.length === 0,
        };
        scene.groups.push(sbBox);
        // Network hubs on the box's middle line, VMs above and below, each
        // placed near the barycentre of its networks.
        const hubY = sbBox.y + sbBox.h / 2 + 8;
        sbNets.forEach((net, i) => {
            const hx = sbBox.x + (sbBox.w / (sbNets.length + 1)) * (i + 1);
            const hub = { id: net.id, kind: "hub", net, x: hx, y: hubY, w: clamp(7 * String(net.bridge).length + 30, 92, 150), h: 24, group: sbBox.id };
            scene.hubs.set(hub.id, hub);
        });
        const bary = (vm) => {
            const nets = (sbMembership[vm.id] || []).map((id) => scene.hubs.get(id)).filter(Boolean);
            return nets.length ? nets.reduce((s, h) => s + h.x, 0) / nets.length : sbBox.x + sbBox.w / 2;
        };
        const sorted = sbVMs.slice().sort((a, b) => bary(a) - bary(b) || String(a.name).localeCompare(String(b.name)));
        const above = sorted.filter((_, i) => i % 2 === 0);
        const below = sorted.filter((_, i) => i % 2 === 1);
        const placeRows = (list, dir) => {
            const rows = Math.max(1, Math.ceil(list.length / perRow));
            for (let r = 0; r < rows; r++) {
                const row = list.slice(r * perRow, (r + 1) * perRow);
                const step = VM_CARD.w + 16;
                const x0 = sbBox.x + sbBox.w / 2 - ((row.length - 1) * step) / 2;
                row.forEach((vm, i) => {
                    const y = hubY + dir * (70 + r * (VM_CARD.h + 30));
                    const node = addNode(vm, x0 + i * step, y, VM_CARD, { group: sbBox.id, sub: sandboxSub(vm) });
                    if (vm.run) scene.alias.set(vm.id, node.id);
                });
            }
        };
        placeRows(above, -1);
        placeRows(below, 1);

        // Private internet (right): DNS first, then the caches.
        const piNodes = [...(byRole.dns || []), ...(byRole.private_internet || [])];
        const PI_X = sbBox.x + sbBox.w + 50;
        const PI_W = PI_CARD.w + 50;
        const piBox = {
            id: "group:pi", kind: "pi", x: PI_X, y: GROUPS_TOP, w: PI_W, h: Math.max(160, 60 + piNodes.length * (PI_CARD.h + 12)),
            label: "PRIVATE INTERNET", info: { kind: "pi", members: piNodes.map((n) => n.id) },
        };
        scene.groups.push(piBox);
        piNodes.forEach((n, i) => addNode(n, PI_X + PI_W / 2, GROUPS_TOP + 56 + i * (PI_CARD.h + 12), PI_CARD, { group: piBox.id, compact: true }));
        scene.pi = { x: PI_X + PI_W / 2, y: GROUPS_TOP, box: piBox };
        scene.alias.set("pi", "group:pi");

        // --- the top rows, spread over the total width.
        const RIGHT = PI_X + PI_W;
        const mid = (LEFT + RIGHT) / 2;
        addNode(first("proxmox"), LEFT + 120, 80, CARD, { sub: "hypervisor" });
        addNode(first("orchestrator"), mid, 80, CARD);
        addNode(first("dashboard"), RIGHT - 120, 80, CARD);
        addNode(first("database"), mid, 220, CARD);
        addNode(first("monitor"), mid - 380, 220, CARD, { sub: "traffic recorder" });
        addNode(first("scoring"), mid - 170, 360, CARD);
        addNode(first("logging"), mid + 170, 360, CARD);
        if (opts.showAux) {
            for (const n of byRole.wazuh || []) addNode(n, mid + 380, 220, CARD);
            for (const n of byRole.ssh_gateway || []) addNode(n, LEFT + 120, 360, CARD);
            (byRole.other || []).forEach((n, i) => addNode(n, LEFT + 80 + i * (CARD.w + 20), Math.max(devBottom, sbBox.y + sbBox.h, piBox.y + piBox.h) + 70, CARD));
        }
        // Extra VMs of a role that has a fixed slot (e.g. two scoring VMs) go next to it.
        for (const role of ["orchestrator", "dashboard", "database", "scoring", "logging", "monitor"]) {
            (byRole[role] || []).slice(1).forEach((n, i) => {
                const base = scene.nodes.get(scene.byRole.get(role)[0]);
                addNode(n, base.x, base.y + (i + 1) * (CARD.h + 12), CARD);
            });
        }

        // --- gateways: each group's anchor (a diamond on the box's top edge).
        const gateways = Object.fromEntries(mnodes.filter((n) => n.role === "sandbox_gw" || n.role === "devbox_gw").map((n) => [n.id, n]));
        for (const box of [...devboxBoxes, sbBox]) {
            const gwId = box.info && box.info.gateway;
            const gw = gwId ? gateways[gwId] : null;
            const onRight = box.kind === "devbox";
            box.anchor = {
                id: `anchor:${box.id}`, kind: "anchor", w: 18, h: 18, gw, group: box.id, labelLeft: onRight,
                x: onRight ? box.x + box.w : box.x + box.w / 2, y: onRight ? box.y + 24 : box.y,
            };
            scene.nodes.set(box.anchor.id, box.anchor);
            if (gw) scene.alias.set(gw.id, box.anchor.id);
        }
        scene.lane = GROUPS_TOP - 34;
        // Devbox traffic leaves through the gutter between the devbox column
        // and the sandbox (one trunk for all teams), then the shared lane.
        const trunkX = LEFT + DEV_W + 25;
        const lead = (box) => (box.kind === "devbox"
            ? [pt(box.anchor), { x: trunkX, y: box.anchor.y }, { x: trunkX, y: scene.lane }]
            : [pt(box.anchor)]);

        // --- edges (operational only; from the model, the timeline for the sandbox).
        const edges = (model && model.edges) || [];
        const nodeAt = (id) => scene.nodes.get(id) || scene.nodes.get(scene.alias.get(id));
        for (const e of edges) {
            if (e.kind === "db") {
                const a = nodeAt(e.source);
                const b = nodeAt(e.target);
                if (a && b) scene.edges.push({ id: e.id, kind: "db", from: a.id, to: b.id, points: [boundary(a, b), boundary(b, a)], info: e, verified: e.verified });
            } else if (e.kind === "lan") {
                const a = nodeAt(e.source);
                const b = nodeAt(e.target);
                if (a && b && a.group === b.group) scene.edges.push({ id: e.id, kind: "lan devbox", from: a.id, to: b.id, points: [pt(a), pt(b)], info: e, verified: true, label: e.label });
            } else if (e.kind === "lan_hub" && opts.mode !== "round") {
                const a = nodeAt(e.source);
                const hub = scene.hubs.get(e.target);
                if (a && hub) scene.edges.push({ id: e.id, kind: "lan_hub", from: a.id, to: hub.id, points: [pt(a), pt(hub)], info: e, verified: true });
            }
        }
        if (opts.mode === "round" && tl) {
            for (const e of tl.edges || []) {
                const a = scene.nodes.get(e.source);
                const hub = scene.hubs.get(`net:${e.target}`);
                if (a && hub) scene.edges.push({ id: `lan_hub:${e.source}->${e.target}`, kind: "lan_hub", from: a.id, to: hub.id, points: [pt(a), pt(hub)], info: e, verified: true });
            }
        }
        // Service edges: every group -> scoring, logging, the private internet.
        const serviceInfo = {};
        for (const e of edges) if (e.kind === "service") serviceInfo[`${e.source}>${e.target}`] = e;
        for (const box of [...devboxBoxes, sbBox]) {
            const a = box.anchor;
            for (const role of ["scoring", "logging"]) {
                for (const id of scene.byRole.get(role) || []) {
                    const target = scene.nodes.get(id);
                    const info = serviceInfo[`${box.id}>${target.info.id}`] || { verified: false, label: role, kind: "service" };
                    scene.edges.push({
                        id: `service:${box.id}->${id}`, kind: "service", from: a.id, to: id, group: box.id,
                        points: [...lead(box), pt(target)], info, verified: !!info.verified,
                    });
                }
            }
            if (piNodes.length) {
                const info = serviceInfo[`${box.id}>pi`] || { verified: false, label: "private internet", kind: "service" };
                const head = lead(box);
                const last = head[head.length - 1];
                scene.edges.push({
                    id: `service:${box.id}->pi`, kind: "service pi", from: a.id, to: "group:pi", group: box.id,
                    points: [...head, ...(last.y === scene.lane ? [] : [{ x: last.x, y: scene.lane }]), { x: scene.pi.x, y: scene.lane }, { x: scene.pi.x, y: scene.pi.y }],
                    info, verified: !!info.verified,
                });
            }
        }

        // Bounds.
        const xs = [];
        const ys = [];
        for (const n of scene.order) {
            xs.push(n.x - n.w / 2, n.x + n.w / 2);
            ys.push(n.y - n.h / 2, n.y + n.h / 2);
        }
        for (const g of scene.groups) {
            xs.push(g.x, g.x + g.w);
            ys.push(g.y, g.y + g.h);
        }
        scene.bounds = { x0: Math.min(...xs) - 30, y0: Math.min(...ys) - 40, x1: Math.max(...xs) + 30, y1: Math.max(...ys) + 30 };
        return scene;
    }

    const pt = (n) => ({ x: n.x, y: n.y });

    // The point where the line from n's centre towards `to` leaves n's card.
    function boundary(n, to) {
        const dx = to.x - n.x;
        const dy = to.y - n.y;
        if (!dx && !dy) return pt(n);
        const s = Math.min((n.w / 2) / Math.abs(dx || 1e-9), (n.h / 2) / Math.abs(dy || 1e-9));
        return { x: n.x + dx * s, y: n.y + dy * s };
    }

    function devboxSub(n) {
        const role = (n.devbox_role || "").toUpperCase();
        const ip = (n.addresses || [])[0] || "";
        return `${role}${n.devbox_role === "a" ? "·main" : ""} ${ip}`.trim();
    }

    // Shorten a label to the width it is drawn in (approximate glyph widths).
    function fitText(text, px, size, mono) {
        text = String(text || "");
        const max = Math.floor(px / (size * (mono ? 0.6 : 0.6)));
        return text.length > max ? `${text.slice(0, Math.max(1, max - 1))}…` : text;
    }

    function sandboxSub(n) {
        return (n.addresses || []).slice(0, 2).join(", ");
    }

    // ------------------------------------------------------------------
    // Routes for the dots: along the drawn edges
    // ------------------------------------------------------------------

    function resolveNode(id, role, name) {
        const sc = S.scene;
        if (!sc || !id) return null;
        if (sc.nodes.has(id)) return sc.nodes.get(id);
        if (sc.alias.has(id)) return sc.nodes.get(sc.alias.get(id));
        if (name && sc.byName.has(name)) return sc.nodes.get(sc.byName.get(name));
        if (role && sc.byRole.has(role)) return sc.nodes.get(sc.byRole.get(role)[0]);
        return null;
    }

    function groupOf(node) {
        return node && node.group ? S.scene.groups.find((g) => g.id === node.group) : null;
    }

    function hubsOf(node) {
        const sc = S.scene;
        const ids = [];
        for (const e of sc.edges) if (e.kind === "lan_hub" && e.from === node.id) ids.push(e.to);
        return ids.map((id) => sc.hubs.get(id)).filter(Boolean);
    }

    // Points from an untrusted VM to its group's gateway.
    function upToAnchor(node) {
        const g = groupOf(node);
        if (!g || !g.anchor) return [pt(node)];
        const hubs = hubsOf(node);
        if (g.kind === "devbox") {
            return [pt(node), { x: node.x, y: g.anchor.y }, pt(g.anchor)];
        }
        return hubs.length ? [pt(node), pt(hubs[0]), pt(g.anchor)] : [pt(node), pt(g.anchor)];
    }

    // Points from a group's gateway to an infrastructure node: along the
    // drawn service edge. null: not reachable from the group (the connection
    // is drawn up to the gateway and fades there).
    function anchorTo(g, node) {
        const sc = S.scene;
        const target = node.group === "group:pi" ? "group:pi" : node.id;
        const edge = sc.edges.find((e) => e.group === g.id && e.to === target && e.kind.startsWith("service"));
        if (!edge) return null;
        return node.group === "group:pi" ? [...edge.points, pt(node)] : edge.points;
    }

    const routeCache = new Map();

    function routeFor(f) {
        const key = `${f.src}>${f.dst}`;
        if (routeCache.has(key)) return routeCache.get(key);
        const a = resolveNode(f.src, f.src_role, f.src_name);
        const b = resolveNode(f.dst, f.dst_role, f.dst_name);
        let points = null;
        let ends = "dst"; // "dst" | "gateway" (the far end is not drawn / unknown)
        const ga = groupOf(a);
        const gb = groupOf(b);
        const untrusted = (g) => g && (g.kind === "sandbox" || g.kind === "devbox");
        if (a && b && untrusted(ga) && ga === gb) {
            const shared = hubsOf(a).find((h) => hubsOf(b).includes(h));
            const direct = S.scene.edges.find((e) => e.kind.startsWith("lan devbox") && ((e.from === a.id && e.to === b.id) || (e.from === b.id && e.to === a.id)));
            if (direct) points = [pt(a), pt(b)];
            else if (shared) points = [pt(a), pt(shared), pt(b)];
            else points = [...upToAnchor(a), ...upToAnchor(b).reverse().slice(1)];
        } else if (a && untrusted(ga)) {
            const tail = b ? anchorTo(ga, b) : null;
            points = tail ? [...upToAnchor(a), ...tail.slice(1)] : upToAnchor(a);
            if (!tail) ends = "gateway";
        } else if (b && untrusted(gb)) {
            const head = a ? anchorTo(gb, a) : null;
            points = head ? [...head.slice().reverse(), ...upToAnchor(b).reverse().slice(1)] : upToAnchor(b).reverse();
            if (!head) ends = "gateway";
        } else if (a && b) {
            points = [pt(a), pt(b)];
        }
        const route = points ? withLengths(points, ends) : null;
        routeCache.set(key, route);
        return route;
    }

    function withLengths(points, ends) {
        const cum = [0];
        for (let i = 1; i < points.length; i++) {
            cum.push(cum[i - 1] + Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y));
        }
        return { points, cum, length: cum[cum.length - 1], ends };
    }

    function pointAt(route, p) {
        const target = clamp(p, 0, 1) * route.length;
        const i = clamp(lowerBound(route.cum, target, (v) => v), 1, route.points.length - 1);
        const a = route.points[i - 1];
        const b = route.points[i];
        const seg = route.cum[i] - route.cum[i - 1] || 1;
        const u = (target - route.cum[i - 1]) / seg;
        return { x: a.x + (b.x - a.x) * u, y: a.y + (b.y - a.y) * u };
    }

    // ------------------------------------------------------------------
    // Drawing the scene (SVG)
    // ------------------------------------------------------------------

    const svg = $("topo-svg");
    let viewport;
    let layers = {};

    function drawScene() {
        clear(svg);
        S.badges.clear();
        routeCache.clear();
        S.lastStatusKey = "";
        const sc = S.scene;
        svg.appendChild(addDefs());
        viewport = sv("g", { class: "viewport" });
        svg.appendChild(viewport);
        layers = {
            groups: sv("g"), edges: sv("g"), hubs: sv("g"), nodes: sv("g"), markers: sv("g"), badges: sv("g"),
        };
        for (const layer of Object.values(layers)) viewport.appendChild(layer);

        for (const g of sc.groups) layers.groups.appendChild(drawGroup(g));
        // The shared lane to the private internet (one line, however many groups).
        for (const e of sc.edges) layers.edges.appendChild(drawEdge(e));
        for (const hub of sc.hubs.values()) layers.hubs.appendChild(drawHub(hub));
        for (const g of sc.groups) if (g.anchor) layers.hubs.appendChild(drawAnchor(g));
        for (const n of sc.order) {
            const node = drawNode(n);
            n.el = node;
            layers.nodes.appendChild(node);
        }
        applyView();
        updateSelectionClasses();
    }

    function drawGroup(g) {
        const node = sv("g", { "data-group": g.id });
        node.appendChild(sv("rect", { class: `group-box ${g.kind}`, x: g.x, y: g.y, width: g.w, height: g.h, rx: 10 }));
        let label = g.label;
        if (g.kind === "sandbox") {
            const vms = S.scene.order.filter((n) => n.group === g.id).length;
            label = `${g.label} · ${vms} VM${vms === 1 ? "" : "s"} · ${[...S.scene.hubs.values()].filter((h) => h.group === g.id).length} networks`;
        }
        // The PI box has its subtitle; other boxes have the gateway on their edge.
        node.appendChild(sv("text", { class: `group-label ${g.kind}`, x: g.x + 12, y: g.y + 20, text: fitText(label, g.w - (g.kind === "pi" ? 24 : 40), 12, true) }));
        if (g.kind === "sandbox" && g.empty) {
            node.appendChild(sv("text", {
                class: "muted", x: g.x + g.w / 2, y: g.y + g.h / 2 + 4, "text-anchor": "middle",
                text: replaying() ? "No VMs recorded for this run." : "No sandbox right now (no run in progress).",
            }));
        }
        if (g.kind === "pi") {
            node.appendChild(sv("text", { class: "muted", x: g.x + 12, y: g.y + 36, "font-size": 10, text: "dead end: ingress only" }));
        }
        node.addEventListener("mouseenter", (ev) => showTip(ev, groupTip(g)));
        node.addEventListener("mousemove", moveTip);
        node.addEventListener("mouseleave", hideTip);
        return node;
    }

    function drawEdge(e) {
        const d = e.points.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
        const node = sv("g", { "data-edge": e.id });
        const cls = `edge ${e.kind}${e.verified ? "" : " unverified"}`;
        node.appendChild(sv("path", { class: cls, d, "marker-end": e.kind === "db" ? "url(#topo-arrow)" : null }));
        node.appendChild(sv("path", { d, fill: "none", stroke: "transparent", "stroke-width": 12, style: "cursor: help" }));
        if (e.label && e.kind.startsWith("lan devbox")) {
            const a = e.points[0];
            const b = e.points[e.points.length - 1];
            // Below the card row: the gap between the cards is too narrow.
            node.appendChild(sv("text", { class: "edge-label muted mono halo", x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 + VM_CARD.h / 2 + 13, "text-anchor": "middle", text: e.label }));
        }
        node.addEventListener("mouseenter", (ev) => showTip(ev, edgeTip(e)));
        node.addEventListener("mousemove", moveTip);
        node.addEventListener("mouseleave", hideTip);
        return node;
    }

    function drawHub(hub) {
        const node = sv("g", { class: `hub ${hub.net.kind}`, transform: `translate(${hub.x},${hub.y})`, tabindex: 0 });
        node.appendChild(sv("rect", { x: -hub.w / 2, y: -hub.h / 2, width: hub.w, height: hub.h, rx: hub.h / 2 }));
        node.appendChild(sv("text", { class: "mono", y: -1, "text-anchor": "middle", "font-size": 11, text: fitText(hub.net.bridge, hub.w - 12, 11, true) }));
        node.appendChild(sv("text", { class: "muted mono", y: 9, "text-anchor": "middle", "font-size": 8.5, text: fitText(hub.net.cidr || "", hub.w - 12, 8.5, true) }));
        node.addEventListener("mouseenter", (ev) => showTip(ev, hubTip(hub)));
        node.addEventListener("mousemove", moveTip);
        node.addEventListener("mouseleave", hideTip);
        return node;
    }

    function drawAnchor(g) {
        const a = g.anchor;
        const node = sv("g", { class: `anchor${a.gw ? "" : " missing"}`, transform: `translate(${a.x},${a.y})`, tabindex: 0 });
        node.appendChild(sv("path", { d: "M0,-9 L9,0 L0,9 L-9,0 Z" }));
        node.appendChild(sv("text", {
            class: "muted mono halo", x: a.labelLeft ? -13 : 13, y: a.labelLeft ? -10 : -6, "font-size": 10,
            "text-anchor": a.labelLeft ? "end" : "start", text: fitText(a.gw ? a.gw.name : g.kind === "sandbox" ? "sandbox-gw" : "devbox-gw", 160, 10, true),
        }));
        node.addEventListener("mouseenter", (ev) => showTip(ev, anchorTip(g)));
        node.addEventListener("mousemove", moveTip);
        node.addEventListener("mouseleave", hideTip);
        node.addEventListener("click", (ev) => {
            ev.stopPropagation();
            if (a.gw) select({ type: "node", id: a.id });
        });
        return node;
    }

    function drawNode(n) {
        const info = n.info;
        const node = sv("g", { class: `vm ${n.kind}`, transform: `translate(${n.x},${n.y})`, tabindex: 0, "data-node": n.id, role: "button" });
        node.appendChild(sv("rect", { class: "card", x: -n.w / 2, y: -n.h / 2, width: n.w, height: n.h, rx: 7 }));
        const statusDot = sv("circle", { class: "status", cx: -n.w / 2 + 11, cy: n.compact ? 0 : -n.h / 2 + 13, r: 4.5 });
        node.appendChild(statusDot);
        n.statusEl = statusDot;
        if (n.compact) {
            node.appendChild(sv("text", { x: -n.w / 2 + 22, y: 4, "font-size": 12, "font-weight": 600, text: fitText(info.name, n.w - 100, 12) }));
            node.appendChild(sv("text", { class: "muted mono", x: n.w / 2 - 8, y: 4, "text-anchor": "end", "font-size": 10, text: (info.addresses || [])[0] || "" }));
        } else {
            node.appendChild(sv("text", { x: -n.w / 2 + 22, y: -n.h / 2 + 17, "font-size": 13, "font-weight": 700, text: fitText(info.name, n.w - 30, 13) }));
            const sub = n.sub !== undefined ? n.sub : ((info.addresses || [])[0] || ROLE_LABEL[n.role] || "");
            const subEl = sv("text", { class: "muted mono", x: -n.w / 2 + 10, y: n.h / 2 - 8, "font-size": 10, text: fitText(sub, n.w - 18, 10, true) });
            node.appendChild(subEl);
            n.subEl = subEl;
        }
        node.addEventListener("mouseenter", (ev) => showTip(ev, nodeTip(n)));
        node.addEventListener("mousemove", moveTip);
        node.addEventListener("mouseleave", hideTip);
        node.addEventListener("click", (ev) => {
            ev.stopPropagation();
            select({ type: "node", id: n.id });
        });
        node.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                select({ type: "node", id: n.id });
            }
        });
        return node;
    }

    function addDefs() {
        const defs = sv("defs");
        const marker = sv("marker", { id: "topo-arrow", viewBox: "0 0 10 10", refX: 10, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
        marker.appendChild(sv("path", { d: "M0,0 L10,5 L0,10 z", style: "fill: var(--c-db)" }));
        defs.appendChild(marker);
        return defs;
    }

    // Status of a node at the player's time (round mode: the run's VMs).
    function nodeStatus(n, t) {
        if (n.kind === "anchor" || n.kind === "hub") return null;
        const info = n.info;
        if (info.absent) return { on: false, label: "not in the snapshot" };
        if (info.run && S.timeline) {
            const points = (S.timeline.vm_status || {})[info.id] || [];
            let state = "not built";
            for (const [pt0, value] of points) {
                if (pt0 <= t) state = value;
                else break;
            }
            return { on: state === "running", label: state };
        }
        return { on: !!info.active, label: info.status || "unknown" };
    }

    function updateStatuses(t) {
        const sc = S.scene;
        let key = "";
        const states = [];
        for (const n of sc.order) {
            const st = nodeStatus(n, t);
            const captured = n.info.run && n.info.run.captured_at !== null && n.info.run.captured_at !== undefined && n.info.run.captured_at <= t;
            states.push([n, st, captured]);
            key += st ? (st.on ? "1" : "0") + st.label : "-";
            key += captured ? "c" : "";
        }
        if (key === S.lastStatusKey) return;
        S.lastStatusKey = key;
        clear(layers.markers);
        for (const [n, st, captured] of states) {
            if (!n.el || !st) continue;
            n.el.classList.toggle("inactive", !st.on);
            n.el.classList.toggle("absent", !!n.info.absent);
            n.el.classList.toggle("captured", !!captured);
            n.el.classList.toggle("start", !!(n.info.run && n.info.run.start_vm));
            n.statusEl.setAttribute("class", `status ${st.on ? "on" : "off"}`);
            if (n.info.run && n.subEl) {
                n.subEl.textContent = fitText(st.on ? sandboxSub(n.info) : `${st.label} · ${sandboxSub(n.info)}`, n.w - (captured ? 30 : 18), 10, true);
            }
            if (captured) {
                const flag = sv("g", { class: "marker-flag", transform: `translate(${n.x + n.w / 2 - 14},${n.y - 2})` });
                flag.appendChild(sv("path", { d: "M0,0 L0,14 M0,0 L9,3 L0,6 Z", style: "stroke: var(--c-flag); stroke-width: 1.6" }));
                layers.markers.appendChild(flag);
            }
        }
    }

    // ------------------------------------------------------------------
    // Pan / zoom
    // ------------------------------------------------------------------

    function applyView() {
        if (viewport) viewport.setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
        drawFrame();
    }

    function fit() {
        if (!S.scene) return;
        const rect = svg.getBoundingClientRect();
        const b = S.scene.bounds;
        const w = b.x1 - b.x0;
        const h = b.y1 - b.y0;
        if (!rect.width || !rect.height || !w || !h) return;
        const k = Math.min(rect.width / w, rect.height / h, 1.4);
        S.view = { k, x: (rect.width - w * k) / 2 - b.x0 * k, y: (rect.height - h * k) / 2 - b.y0 * k, user: false };
        S.needsFit = false;
        applyView();
    }

    function zoomAt(factor, cx, cy) {
        const k = clamp(S.view.k * factor, 0.15, 4);
        const f = k / S.view.k;
        S.view.x = cx - (cx - S.view.x) * f;
        S.view.y = cy - (cy - S.view.y) * f;
        S.view.k = k;
        S.view.user = true;
        applyView();
    }

    function wirePanZoom() {
        let drag = null;
        svg.addEventListener("wheel", (ev) => {
            ev.preventDefault();
            const rect = svg.getBoundingClientRect();
            zoomAt(Math.exp(-ev.deltaY * 0.0015), ev.clientX - rect.left, ev.clientY - rect.top);
        }, { passive: false });
        svg.addEventListener("pointerdown", (ev) => {
            if (ev.button !== 0) return;
            drag = { x: ev.clientX, y: ev.clientY, vx: S.view.x, vy: S.view.y, moved: false };
        });
        window.addEventListener("pointermove", (ev) => {
            if (!drag) return;
            const dx = ev.clientX - drag.x;
            const dy = ev.clientY - drag.y;
            if (!drag.moved && Math.hypot(dx, dy) < 4) return;
            drag.moved = true;
            svg.classList.add("panning");
            S.view.x = drag.vx + dx;
            S.view.y = drag.vy + dy;
            S.view.user = true;
            applyView();
        });
        window.addEventListener("pointerup", () => {
            if (drag && drag.moved) svg.dataset.justPanned = "1";
            drag = null;
            svg.classList.remove("panning");
        });
        $("topo-zoom-in").addEventListener("click", () => {
            const r = svg.getBoundingClientRect();
            zoomAt(1.25, r.width / 2, r.height / 2);
        });
        $("topo-zoom-out").addEventListener("click", () => {
            const r = svg.getBoundingClientRect();
            zoomAt(0.8, r.width / 2, r.height / 2);
        });
        $("topo-fit").addEventListener("click", fit);
        window.addEventListener("resize", () => {
            resizeCanvas();
            if (!S.view.user) fit();
            else drawFrame();
            drawDensity();
        });
    }

    // ------------------------------------------------------------------
    // Tooltips (hover) -- firewall groups, rules, bridges
    // ------------------------------------------------------------------

    const tip = $("topo-tip");
    const stage = $("topo-stage");

    function showTip(ev, content) {
        if (!content) return;
        clear(tip);
        append(tip, [content]);
        tip.hidden = false;
        moveTip(ev);
    }

    function moveTip(ev) {
        if (tip.hidden) return;
        const r = stage.getBoundingClientRect();
        let x = ev.clientX - r.left + 14;
        let y = ev.clientY - r.top + 14;
        const w = tip.offsetWidth;
        const h = tip.offsetHeight;
        if (x + w > r.width - 6) x = Math.max(6, ev.clientX - r.left - w - 14);
        if (y + h > r.height - 6) y = Math.max(6, r.height - h - 6);
        tip.style.left = `${x}px`;
        tip.style.top = `${y}px`;
    }

    function hideTip() {
        tip.hidden = true;
    }

    function ruleText(r) {
        const dir = r.direction === "in" ? "IN" : r.direction === "out" ? "OUT" : r.direction;
        const proto = r.macro ? r.macro : `${r.proto || "any"}${r.dport ? "/" + r.dport : ""}`;
        const peer = r.direction === "in" ? (r.source ? `from ${r.source}` : "from any") : (r.dest ? `to ${r.dest}` : "to any");
        return `${dir} ${r.action} ${proto} ${peer}${r.iface ? " @" + r.iface : ""}`;
    }

    function nicText(nic) {
        const mgmt = nic.kind === "management" || nic.kind === "mirror" ? " — not drawn" : "";
        return `${nic.name} ${nic.bridge} (${nic.kind}${mgmt}) ${nic.cidr || ""}${nic.firewall ? "" : " · NO FIREWALL"}`;
    }

    // Hover data for a timeline VM: its networks; groups/rules from the
    // cluster security groups (sandbox on every NIC, ssh on the management NIC).
    function sandboxFirewall() {
        const sg = (S.model && S.model.security_groups) || {};
        const groups = [
            { name: "sandbox", iface: "", comment: "Sandbox networks + services bridge" },
            { name: "ssh", iface: "management NIC", comment: "SSH from the Proxmox host" },
        ];
        const rules = [];
        for (const g of groups) for (const r of (sg[g.name] || { rules: [] }).rules) rules.push(Object.assign({}, r, { iface: g.iface === "management NIC" ? "mgmt" : r.iface }));
        return { enabled: true, policy_in: "DROP", policy_out: "DROP", ipfilter: true, groups, rules };
    }

    function nodeTip(n) {
        const info = n.info;
        const st = nodeStatus(n, S.t);
        const parts = [el("div", { class: "tt" }, info.name)];
        const bits = [ROLE_LABEL[n.role] || n.role];
        if (info.vmid) bits.push(`VMID ${info.vmid}`);
        if (st) bits.push(st.label);
        parts.push(el("div", { class: "ts" }, bits.join(" · ")));
        if (info.absent) {
            parts.push(el("div", { class: "tn" }, "Not in the Proxmox snapshot (not built, or not collected yet)."));
            return el("div", null, parts);
        }
        if (info.run) {
            parts.push(el("div", { class: "th" }, "Networks (vmbr)"));
            for (const net of info.networks || []) parts.push(el("div", { class: "tl mono" }, `${net.name} ${net.cidr || ""}`));
            parts.push(el("div", { class: "tl mono" }, `addresses: ${(info.addresses || []).join(", ")}`));
            if (info.run.start_vm) parts.push(el("div", { class: "tn" }, "Start VM: the worm is deployed here."));
            if (info.run.captured_at !== null && info.run.captured_at !== undefined) {
                parts.push(el("div", { class: "tn" }, `Captured at ${fmtTime(info.run.captured_at)}.`));
            }
            appendFirewall(parts, sandboxFirewall(), 6);
            parts.push(el("div", { class: "tn" }, "Groups as attached to every sandbox VM (opentofu/stacks/sandbox). Click for details."));
            return el("div", null, parts);
        }
        if (info.nics && info.nics.length) {
            parts.push(el("div", { class: "th" }, "NICs / bridges"));
            for (const nic of info.nics) parts.push(el("div", { class: "tl mono" }, nicText(nic)));
        }
        appendFirewall(parts, info.firewall, 8);
        parts.push(el("div", { class: "tn" }, "Click for all rules."));
        return el("div", null, parts);
    }

    function appendFirewall(parts, fw, limit) {
        if (!fw) return;
        parts.push(el("div", { class: "th" }, "Firewall"));
        const opts = [fw.enabled ? "enabled" : "DISABLED"];
        if (fw.policy_in) opts.push(`in ${fw.policy_in}`);
        if (fw.policy_out) opts.push(`out ${fw.policy_out}`);
        if (fw.ipfilter) opts.push("ipfilter");
        parts.push(el("div", { class: `tl${fw.enabled ? "" : " is-bad"}` }, opts.join(" · ")));
        if (fw.groups && fw.groups.length) {
            parts.push(el("div", { class: "th" }, "Security groups"));
            parts.push(el("div", { class: "tl mono" }, fw.groups.map((g) => `${g.name}${g.iface ? "@" + g.iface : ""}`).join(", ")));
        }
        const rules = fw.rules || [];
        if (rules.length) {
            parts.push(el("div", { class: "th" }, `Rules (${rules.length})`));
            for (const r of rules.slice(0, limit)) parts.push(el("div", { class: "tl mono" }, ruleText(r)));
            if (rules.length > limit) parts.push(el("div", { class: "tn" }, `+${rules.length - limit} more`));
        }
    }

    function anchorTip(g) {
        const a = g.anchor;
        const parts = [el("div", { class: "tt" }, a.gw ? a.gw.name : g.kind === "sandbox" ? "sandbox-gw" : "devbox-gw")];
        parts.push(el("div", { class: "ts" }, `${ROLE_LABEL[g.kind === "sandbox" ? "sandbox_gw" : "devbox_gw"]} · routes the group's networks to the services (SNAT)`));
        if (!a.gw) {
            parts.push(el("div", { class: "tn" }, S.mode !== "live" ? "Built per run; not in the snapshot." : "Not in the live snapshot."));
            return el("div", null, parts);
        }
        const st = a.gw.active ? "running" : a.gw.status;
        parts.push(el("div", { class: "ts" }, `VMID ${a.gw.vmid} · ${st}`));
        parts.push(el("div", { class: "th" }, "NICs / bridges"));
        for (const nic of a.gw.nics || []) parts.push(el("div", { class: "tl mono" }, nicText(nic)));
        appendFirewall(parts, a.gw.firewall, 6);
        return el("div", null, parts);
    }

    function hubTip(hub) {
        const members = S.scene.edges.filter((e) => e.to === hub.id).map((e) => S.scene.nodes.get(e.from)).filter(Boolean);
        return el("div", null,
            el("div", { class: "tt mono" }, hub.net.bridge),
            el("div", { class: "ts" }, `${hub.net.kind} network · ${hub.net.cidr || "no CIDR"}`),
            hub.net.comment ? el("div", { class: "tl" }, hub.net.comment) : null,
            el("div", { class: "th" }, `Members (${members.length})`),
            el("div", { class: "tl" }, members.map((m) => m.info.name).join(", ") || "—"),
            el("div", { class: "tn" }, "Plus the group's gateway (its default route)."));
    }

    function edgeTip(e) {
        const sc = S.scene;
        const from = sc.nodes.get(e.from);
        const to = sc.nodes.get(e.to);
        const name = (n, fallback) => (n ? (n.info ? n.info.name : n.gw ? n.gw.name : fallback) : fallback);
        const parts = [];
        if (e.kind === "db") {
            parts.push(el("div", { class: "tt" }, `${name(from)} → database`));
            parts.push(el("div", { class: "ts" }, "tcp/5432 — allowed by the database's firewall"));
            if (e.info.group) parts.push(el("div", { class: "tl mono" }, `group ${e.info.group}${e.info.comment ? " — " + e.info.comment : ""}`));
        } else if (e.kind.startsWith("service")) {
            const g = sc.groups.find((x) => x.id === e.group);
            parts.push(el("div", { class: "tt" }, `${g ? g.label : "group"} → ${e.to === "group:pi" ? "private internet" : name(to)}`));
            parts.push(el("div", { class: "ts" }, e.verified ? "allowed by the target's firewall (via the gateway)" : "by design — not verified (no live gateway / no matching rule)"));
            if (e.info.targets && e.info.targets.length) parts.push(el("div", { class: "tl" }, e.info.targets.join(", ")));
        } else {
            parts.push(el("div", { class: "tt" }, `${name(from)} — ${name(to, e.to)}`));
            parts.push(el("div", { class: "ts" }, `${e.label || (e.info && e.info.network) || "network"} (operational)`));
        }
        return el("div", null, parts);
    }

    function groupTip(g) {
        if (g.kind === "pi") {
            return el("div", null, el("div", { class: "tt" }, "Private internet"),
                el("div", { class: "ts" }, "Caches and DNS on the services bridge: reachable by the sandbox and the devboxes, no egress."));
        }
        if (g.kind === "devbox") {
            return el("div", null, el("div", { class: "tt" }, g.label),
                el("div", { class: "ts" }, "Three VMs, A–B and B–C; reaches scoring, logging and the private internet only (never the sandbox or another team)."));
        }
        return el("div", null, el("div", { class: "tt" }, "Sandbox"),
            el("div", { class: "ts" }, "The running round's VMs; reach each other, scoring, logging and the private internet only."));
    }

    // ------------------------------------------------------------------
    // Canvas overlay: dots
    // ------------------------------------------------------------------

    const canvas = $("topo-dots");
    const ctx = canvas.getContext("2d");
    const MAX_DOTS = 500;

    function resizeCanvas() {
        const r = stage.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.round(r.width * dpr));
        canvas.height = Math.max(1, Math.round(r.height * dpr));
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    // Wall-clock seconds a dot takes to travel: its connection's duration at
    // the current speed, kept between 0.8 s and 3 s so every dot is visible.
    function travel(f) {
        const wall = clamp((f.d || 0) / S.speed, 0.8, 3);
        return wall * S.speed;
    }

    function activeFlows(t) {
        const flows = (S.timeline && S.timeline.flows) || [];
        if (!flows.length) return [];
        const maxTravel = 3 * S.speed;
        const lo = lowerBound(flows, t - maxTravel, (f) => f.t);
        const hi = lowerBound(flows, t + 1e-9, (f) => f.t);
        const out = [];
        for (let i = hi - 1; i >= lo && out.length < MAX_DOTS; i--) {
            const f = flows[i];
            if (t - f.t <= travel(f)) out.push(f);
        }
        return out;
    }

    function drawFrame() {
        const r = stage.getBoundingClientRect();
        ctx.clearRect(0, 0, r.width, r.height);
        S.dots = [];
        if (!replaying() || !S.scene) return;
        const { k, x: vx, y: vy } = S.view;
        const selectedUid = S.selected && S.selected.type === "flow" ? S.selected.flow.uid : null;
        for (const f of activeFlows(S.t)) {
            const route = routeFor(f);
            if (!route) continue;
            const p = (S.t - f.t) / travel(f);
            const pos = pointAt(route, p);
            const x = pos.x * k + vx;
            const y = pos.y * k + vy;
            const color = S.colors[KIND_COLOR[f.kind] || "--c-other"];
            const failed = FAILED_STATES.has(f.conn_state);
            const radius = (f.uid === selectedUid ? 6.5 : 4.8) * clamp(Math.sqrt(k), 0.75, 1.4);
            const fade = route.ends === "gateway" && p > 0.8 ? 1 - (p - 0.8) / 0.2 : 1;
            ctx.globalAlpha = clamp(fade, 0.15, 1);
            ctx.beginPath();
            ctx.arc(x, y, radius, 0, Math.PI * 2);
            if (failed) {
                ctx.lineWidth = 2;
                ctx.strokeStyle = S.colors["--bad"];
                ctx.fillStyle = S.colors["--panel"];
                ctx.fill();
                ctx.stroke();
            } else {
                ctx.fillStyle = color;
                ctx.fill();
                ctx.lineWidth = 1;
                ctx.strokeStyle = S.colors["--panel"];
                ctx.stroke();
            }
            if (f.uid === selectedUid) {
                ctx.beginPath();
                ctx.arc(x, y, radius + 4, 0, Math.PI * 2);
                ctx.lineWidth = 2;
                ctx.strokeStyle = S.colors["--ink"];
                ctx.stroke();
            }
            S.dots.push({ x, y, f });
        }
        ctx.globalAlpha = 1;
    }

    function dotAt(clientX, clientY) {
        const r = stage.getBoundingClientRect();
        const x = clientX - r.left;
        const y = clientY - r.top;
        let best = null;
        let bestD = 9;
        for (const dot of S.dots) {
            const d = Math.hypot(dot.x - x, dot.y - y);
            if (d < bestD) {
                best = dot;
                bestD = d;
            }
        }
        return best;
    }

    function flowTip(f) {
        return el("div", null,
            el("div", { class: "tt" }, `${f.src_name || f.src} → ${f.dst_name || f.dst}`),
            el("div", { class: "ts mono" }, `${f.transport}/${f.dst_port ?? "?"}${f.service ? " · " + f.service : ""} · ${f.conn_state || "?"}`),
            el("div", { class: "tl" }, `at ${fmtTime(f.t)} · ${f.d ? f.d.toFixed(2) + " s" : "<0.01 s"} · ${fmtBytes(f.bytes)}`),
            el("div", { class: "tn" }, "Click to pause and inspect."));
    }

    // ------------------------------------------------------------------
    // Event badges (SVG icons on the nodes)
    // ------------------------------------------------------------------

    const HOLD = 3.2; // wall-clock seconds an event's icon stays up

    function visibleEvents(t) {
        const events = (S.timeline && S.timeline.events) || [];
        const hold = HOLD * S.speed;
        const lo = lowerBound(events, t - hold, (e) => e.t);
        const hi = lowerBound(events, t + 1e-9, (e) => e.t);
        return events.slice(lo, hi);
    }

    function badgeElement(e, look) {
        const g = sv("g", { class: "badge", tabindex: 0, role: "button", "aria-label": look.label });
        g.appendChild(sv("circle", { r: 8.5, style: `fill: var(${look.color})` }));
        const icon = ICON[look.icon] || ICON.ring;
        if (icon.solid) g.appendChild(sv("path", { class: "solid", d: icon.solid }));
        if (icon.d) g.appendChild(sv("path", { d: icon.d }));
        g.addEventListener("click", (ev) => {
            ev.stopPropagation();
            pause();
            select({ type: "event", event: e });
        });
        g.addEventListener("mouseenter", (ev) => showTip(ev, eventTip(e)));
        g.addEventListener("mousemove", moveTip);
        g.addEventListener("mouseleave", hideTip);
        return g;
    }

    function updateBadges(t) {
        if (!replaying()) {
            if (layers.badges) clear(layers.badges);
            S.badges.clear();
            return;
        }
        const hold = HOLD * S.speed;
        const perNode = new Map();
        const wanted = new Set();
        const events = visibleEvents(t);
        for (let i = events.length - 1; i >= 0; i--) {
            const e = events[i];
            for (const target of eventTargets(e)) {
                const node = resolveNode(target, target.startsWith("role:") ? target.slice(5) : null, null);
                if (!node || node.kind === "hub") continue;
                const list = perNode.get(node.id) || [];
                if (list.length >= 4) continue;
                list.push(e);
                perNode.set(node.id, list);
                const key = `${e.id}@${node.id}`;
                wanted.add(key);
                let badge = S.badges.get(key);
                if (!badge) {
                    badge = { el: badgeElement(e, eventLook(e)), event: e };
                    S.badges.set(key, badge);
                    layers.badges.appendChild(badge.el);
                }
                const slot = list.length - 1;
                const bx = node.x + (node.w || 18) / 2 - 10 - slot * 19;
                const by = node.y - (node.h || 18) / 2 - 6;
                const age = (t - e.t) / hold; // 0..1
                const pop = clamp((t - e.t) / (0.25 * S.speed), 0, 1);
                const scale = 0.6 + 0.4 * pop + (pop < 1 ? 0.25 * Math.sin(pop * Math.PI) : 0);
                badge.el.setAttribute("transform", `translate(${bx},${by}) scale(${scale.toFixed(3)})`);
                badge.el.style.opacity = String(clamp(1.15 - age, 0.25, 1));
                badge.el.classList.toggle("selected", !!(S.selected && S.selected.type === "event" && S.selected.event.id === e.id));
            }
        }
        for (const [key, badge] of S.badges) {
            if (!wanted.has(key)) {
                badge.el.remove();
                S.badges.delete(key);
            }
        }
    }

    function eventTip(e) {
        const look = eventLook(e);
        const d = e.data || {};
        const parts = [el("div", { class: "tt" }, look.label), el("div", { class: "ts" }, `at ${fmtTime(e.t)}`)];
        const vm = e.vm ? resolveNode(e.vm) : null;
        if (vm && vm.info) parts.push(el("div", { class: "tl" }, `VM ${vm.info.name}`));
        if (d.source_label) parts.push(el("div", { class: "tl" }, `from ${d.source_label} (inferred from the monitor)`));
        if (d.flag_of_label) parts.push(el("div", { class: "tl" }, `flag of ${d.flag_of_label}`));
        parts.push(el("div", { class: "tn" }, "Click to pause and see all details."));
        return el("div", null, parts);
    }

    // ------------------------------------------------------------------
    // Side panel: summary, selection details, event feed
    // ------------------------------------------------------------------

    const side = $("topo-side");

    function kvTable(rows) {
        return el("table", { class: "kv" }, el("tbody", null,
            rows.filter(Boolean).map(([k, v]) => el("tr", null, el("td", null, k), el("td", null, v)))));
    }

    function select(sel) {
        S.selected = sel;
        renderSide();
        updateSelectionClasses();
        drawFrame();
        updateBadges(S.t);
    }

    function updateSelectionClasses() {
        if (!S.scene) return;
        for (const n of S.scene.order) {
            if (n.el) n.el.classList.toggle("selected", !!(S.selected && S.selected.type === "node" && S.selected.id === n.id));
        }
    }

    function renderSide() {
        clear(side);
        const sel = S.selected;
        if (sel && sel.type === "node") return renderNodeDetails(sel.id);
        if (sel && sel.type === "event") return renderEventDetails(sel.event);
        if (sel && sel.type === "flow") return renderFlowDetails(sel.flow);
        if (replaying() || S.mode === "round") return renderRunSummary();
        return renderLiveSummary();
    }

    function backButton() {
        return el("div", { class: "actions" }, el("button", { type: "button", onclick: () => select(null) }, "← Back"));
    }

    function renderLiveSummary() {
        const m = S.model;
        if (!m) {
            append(side, [el("h3", null, "Infrastructure"), el("p", { class: "hint" }, S.modelError ? S.modelError.message : "Loading…")]);
            return;
        }
        const vms = m.nodes.filter((n) => n.role !== "proxmox");
        const off = vms.filter((n) => !n.active);
        append(side, [
            el("h3", null, S.mode === "file" ? "Infrastructure (from the file)" : "Infrastructure"),
            el("div", { class: "sub" }, `${m.host.node || "?"} · Proxmox ${m.host.version || "?"}`),
            kvTable([
                ["VMs", `${vms.length} (${vms.length - off.length} running)`],
                ["sandbox", `${m.nodes.filter((n) => n.role === "sandbox").length} VMs`],
                ["devboxes", `${m.groups.filter((g) => g.kind === "devbox").length} teams`],
                ["firewall daemon", (m.host.firewall_services || []).map((s) => `${s.name}: ${s.state || s.active_state}`).join(", ") || "unknown"],
            ]),
        ]);
        if (off.length) {
            append(side, [el("h4", null, "Not running"), el("ul", null, off.map((n) => el("li", null,
                el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); select({ type: "node", id: n.id }); } }, n.name), ` — ${n.status}`)))]);
        }
        const unresolved = (m.unresolved && m.unresolved.database_sources) || [];
        if (unresolved.length) {
            append(side, [el("h4", null, "Database rules with unknown sources"), el("p", { class: "is-warn" }, unresolved.join(", "))]);
        }
        if ((m.host.errors || []).length) {
            append(side, [el("h4", null, "Collection errors"), el("ul", null, m.host.errors.slice(0, 20).map((e) => el("li", { class: "mono" }, `${e.path}: ${e.error}`)))]);
        }
        append(side, [el("h4", null, "How to read it"), el("p", { class: "hint" },
            "Hover a VM for its bridges, security groups and rules; click it for all of them. Solid lines are allowed by the " +
            "firewall; dashed lines are by design but not verified against a live rule. Management networks are never drawn. " +
            "Pick a round and a team above to replay a run.")]);
    }

    function renderRunSummary() {
        const tl = S.timeline;
        if (!tl) {
            const msg = S.timelineError ? S.timelineError.message : S.runUuid ? "Loading the run…" : "Pick a team.";
            append(side, [el("h3", null, "Round"), el("p", { class: "hint" }, msg)]);
            return;
        }
        const r = tl.run;
        const kinds = {};
        for (const e of tl.events) kinds[e.kind] = (kinds[e.kind] || 0) + 1;
        const captured = tl.nodes.filter((n) => n.captured).length;
        append(side, [
            el("h3", null, `${r.round} · ${r.team}`),
            el("div", { class: "sub" }, `run #${r.position} · ${r.state}`),
            kvTable([
                ["worm start", r.worm_start_at ? new Date(r.worm_start_at).toLocaleString() : "—"],
                ["duration", r.duration !== null ? fmtTime(r.duration) : "—"],
                ["VMs", `${tl.nodes.length} (${captured} captured)`],
                ["connections", `${tl.flows.length}${tl.truncated && tl.truncated.flows ? " (truncated)" : ""}`],
                ["flags submitted", String(kinds.flag_submitted || 0)],
                ["logs sent", String(kinds.log_received || 0)],
            ]),
        ]);
        const feed = el("ul", { class: "feed", id: "topo-feed" });
        const notable = tl.events.filter((e) => e.kind !== "flag_submitted" || e.data.result !== "invalid").slice(0, 600);
        for (const e of notable) {
            const look = eventLook(e);
            const vm = e.vm ? resolveNode(e.vm) : e.node ? resolveNode(e.node) : null;
            const who = vm && vm.info ? ` — ${vm.info.name}` : "";
            const li = el("li", { "data-t": e.t, onclick: () => { seek(e.t); pause(); select({ type: "event", event: e }); } },
                el("span", { class: "t" }, fmtTime(e.t)),
                el("span", { class: "sw", style: `background: var(${look.color})` }),
                el("span", null, `${look.label}${who}`));
            feed.appendChild(li);
        }
        append(side, [el("h4", null, `Events (${tl.events.length})`), feed]);
        S.feedIndex = -1;
        updateFeed(S.t);
    }

    function updateFeed(t) {
        const feed = document.getElementById("topo-feed");
        if (!feed) return;
        const items = feed.children;
        const idx = lowerBound(Array.from(items), t + 1e-9, (li) => Number(li.dataset.t));
        if (idx === S.feedIndex) return;
        S.feedIndex = idx;
        for (let i = 0; i < items.length; i++) items[i].classList.toggle("past", i < idx);
    }

    function renderNodeDetails(id) {
        const n = S.scene && S.scene.nodes.get(id);
        if (!n) return select(null);
        const info = n.kind === "anchor" ? n.gw : n.info;
        if (!info) return select(null);
        const st = n.kind === "anchor" ? { on: info.active, label: info.status } : nodeStatus(n, S.t);
        append(side, [
            backButton(),
            el("h3", null, info.name),
            el("div", { class: "sub" }, [ROLE_LABEL[info.role] || info.role, info.vmid ? `VMID ${info.vmid}` : null, info.pool ? `pool ${info.pool}` : null].filter(Boolean).join(" · ")),
            kvTable([
                ["status", el("span", { class: st && st.on ? "is-ok" : "is-off" }, st ? st.label : "—")],
                info.tags && info.tags.length ? ["tags", info.tags.join(", ")] : null,
                info.team ? ["team", info.team] : null,
                info.addresses && info.addresses.length ? ["addresses", info.addresses.join(", ")] : null,
            ]),
        ]);
        if (info.run) {
            append(side, [el("h4", null, "Networks"), el("table", null, el("tbody", null,
                (info.networks || []).map((net) => el("tr", null, el("td", { class: "mono" }, net.name), el("td", { class: "mono" }, net.cidr || "")))))]);
            if (info.run.captured_at !== null && info.run.captured_at !== undefined) append(side, [el("p", { class: "is-bad" }, `Captured at ${fmtTime(info.run.captured_at)}.`)]);
            renderFirewall(sandboxFirewall());
            const mine = S.timeline.events.filter((e) => e.vm === info.id || e.node === info.id || (e.data && e.data.source_vm === info.id));
            if (mine.length) {
                const feed = el("ul", { class: "feed" });
                for (const e of mine.slice(0, 300)) {
                    feed.appendChild(el("li", { onclick: () => { seek(e.t); pause(); select({ type: "event", event: e }); } },
                        el("span", { class: "t" }, fmtTime(e.t)), el("span", { class: "sw", style: `background: var(${eventLook(e).color})` }), eventLook(e).label));
                }
                append(side, [el("h4", null, `Events of this VM (${mine.length})`), feed]);
            }
            return;
        }
        if (info.nics && info.nics.length) {
            append(side, [el("h4", null, "NICs"), el("table", null,
                el("thead", null, el("tr", null, el("th", null, "nic"), el("th", null, "bridge"), el("th", null, "kind"), el("th", null, "address"))),
                el("tbody", null, info.nics.map((nic) => el("tr", null,
                    el("td", { class: "mono" }, nic.name), el("td", { class: "mono" }, nic.bridge),
                    el("td", { class: nic.kind === "management" ? "is-off" : "" }, nic.kind + (nic.firewall ? "" : " · no fw")),
                    el("td", { class: "mono" }, nic.cidr || "")))))]);
        }
        renderFirewall(info.firewall);
    }

    function renderFirewall(fw) {
        if (!fw) return;
        append(side, [el("h4", null, "Firewall"), el("p", { class: fw.enabled ? "" : "is-bad" },
            [fw.enabled ? "enabled" : "DISABLED", fw.policy_in ? `in ${fw.policy_in}` : null, fw.policy_out ? `out ${fw.policy_out}` : null, fw.ipfilter ? "ipfilter" : null].filter(Boolean).join(" · "))]);
        if (fw.groups && fw.groups.length) {
            append(side, [el("h4", null, "Security groups"), el("table", null, el("tbody", null,
                fw.groups.map((g) => el("tr", null, el("td", { class: "mono" }, g.name), el("td", { class: "mono" }, g.iface || "all NICs"), el("td", null, g.comment || "")))))]);
        }
        const rules = fw.rules || [];
        append(side, [el("h4", null, `Rules (${rules.length})`), rules.length ? el("table", null,
            el("thead", null, el("tr", null, el("th", null, "dir"), el("th", null, "action"), el("th", null, "proto/port"), el("th", null, "peer"), el("th", null, "via"))),
            el("tbody", null, rules.map((r) => el("tr", null,
                el("td", null, r.direction), el("td", { class: r.action === "ACCEPT" ? "is-ok" : "is-bad" }, r.action),
                el("td", { class: "mono" }, r.macro || `${r.proto || "any"}${r.dport ? "/" + r.dport : ""}`),
                el("td", { class: "mono" }, r.direction === "in" ? (r.source || "any") : (r.dest || "any")),
                el("td", { class: "mono" }, [r.group, r.iface].filter(Boolean).join("@")))))) : el("p", { class: "hint" }, "none")]);
    }

    function renderEventDetails(e) {
        const look = eventLook(e);
        const d = e.data || {};
        const vm = e.vm ? resolveNode(e.vm) : null;
        const rows = [["time", fmtTime(e.t)], ["kind", e.kind]];
        if (vm && vm.info) rows.push(["VM", vm.info.name]);
        if (e.kind === "flag_submitted") {
            rows.push(["result", el("span", { class: d.result === "captured" ? "is-ok" : "is-warn" }, d.result)]);
            if (d.flag_of_label) rows.push(["flag of", d.flag_of_label]);
            rows.push(["hash", el("span", { class: "mono" }, `${d.hash_prefix}…`)]);
            rows.push(["submitted by", d.source_label ? `${d.source_label} (inferred)` : "unknown (no connection to scoring at the time)"]);
        }
        if (e.kind === "log_received") {
            rows.push(["stored", d.stored ? "yes (in the round's logs)" : "no"]);
            rows.push(["sent by", d.source_label ? `${d.source_label} (inferred)` : "unknown"]);
        }
        append(side, [backButton(), el("h3", null, look.label), el("div", { class: "sub" }, `at ${fmtTime(e.t)}`), kvTable(rows)]);
        if (e.kind === "log_received" && d.log) {
            append(side, [el("h4", null, d.log.truncated ? `Log (first ${String(d.log.data).length} of ${d.log.size} characters)` : "Log"),
                el("pre", null, typeof d.log.data === "string" ? d.log.data : JSON.stringify(d.log.data, null, 2))]);
        } else {
            append(side, [el("h4", null, "Data"), el("pre", null, JSON.stringify(d, null, 2))]);
        }
        const actions = el("div", { class: "actions" });
        actions.appendChild(el("button", { type: "button", onclick: () => { select(null); play(); } }, "▶ Continue"));
        if (vm && vm.info) actions.appendChild(el("button", { type: "button", onclick: () => select({ type: "node", id: vm.id }) }, `Show ${vm.info.name}`));
        side.appendChild(actions);
    }

    function renderFlowDetails(f) {
        append(side, [backButton(), el("h3", null, `${f.src_name || f.src} → ${f.dst_name || f.dst}`),
            el("div", { class: "sub" }, `${f.kind} connection at ${fmtTime(f.t)}`),
            kvTable([
                ["protocol", `${f.transport}${f.service ? " · " + f.service : ""}`],
                ["ports", `${f.src_port ?? "?"} → ${f.dst_port ?? "?"}`],
                ["state", `${f.conn_state || "?"}${FAILED_STATES.has(f.conn_state) ? " (not established)" : ""}`],
                ["duration", f.d ? `${f.d.toFixed(3)} s` : "< 1 ms"],
                ["traffic", `${fmtBytes(f.bytes)} · ${f.pkts || 0} packets`],
                ["Zeek uid", el("span", { class: "mono" }, f.uid)],
            ]),
            el("p", { class: "hint" }, "Recorded by the monitor before the gateway's SNAT, so the sandbox end is the VM's own address.")]);
        side.appendChild(el("div", { class: "actions" }, el("button", { type: "button", onclick: () => { select(null); play(); } }, "▶ Continue")));
    }

    // ------------------------------------------------------------------
    // Player: clock, slider, density strip
    // ------------------------------------------------------------------

    const SPEEDS = [1, 2, 5, 10, 30, 60, 120, 300, 600];
    const player = $("topo-player");
    const track = $("topo-track");

    function range() {
        const r = S.timeline ? S.timeline.run : null;
        if (!r) return [0, 1];
        const lo = Math.min(r.t_min, 0);
        const hi = Math.max(r.t_max, lo + 1);
        return [lo, hi];
    }

    function seek(t) {
        const [lo, hi] = range();
        S.t = clamp(t, lo, hi);
        render();
    }

    function play() {
        if (!S.timeline) return;
        const [, hi] = range();
        if (S.t >= hi) S.t = range()[0];
        S.playing = true;
        S.lastFrame = performance.now();
        $("topo-play").textContent = "❚❚";
        $("topo-play").setAttribute("aria-label", "Pause");
        requestAnimationFrame(tick);
    }

    function pause() {
        S.playing = false;
        $("topo-play").textContent = "▶";
        $("topo-play").setAttribute("aria-label", "Play");
        writeHash();
    }

    function tick(now) {
        if (!S.playing) return;
        const dt = Math.min(0.25, (now - S.lastFrame) / 1000);
        S.lastFrame = now;
        const [, hi] = range();
        S.t += dt * S.speed;
        if (S.t >= hi) {
            S.t = hi;
            render();
            pause();
            return;
        }
        render();
        requestAnimationFrame(tick);
    }

    function phaseAt(t) {
        const states = S.stateEvents || [];
        const i = lowerBound(states, t + 1e-9, (e) => e.t) - 1;
        return i >= 0 ? states[i].data.state : null;
    }

    function render() {
        if (!S.scene) return;
        updateStatuses(S.t);
        updateBadges(S.t);
        drawFrame();
        if (replaying()) {
            const [lo, hi] = range();
            const frac = (S.t - lo) / (hi - lo);
            $("topo-knob").style.left = `${(frac * 100).toFixed(3)}%`;
            $("topo-fill").style.width = `${(frac * 100).toFixed(3)}%`;
            track.setAttribute("aria-valuenow", S.t.toFixed(1));
            track.setAttribute("aria-valuetext", fmtTime(S.t));
            $("topo-time").textContent = `${fmtTime(S.t)} / ${fmtTime(S.timeline.run.duration ?? hi)}`;
            const phase = phaseAt(S.t);
            $("topo-phase").textContent = S.t < 0 ? `before the worm's start · ${phase || "…"}` : phase ? `state: ${phase}` : "";
            updateFeed(S.t);
        }
    }

    function buildSpeeds() {
        const speed = $("topo-speed");
        clear(speed);
        for (const s of SPEEDS) speed.appendChild(el("option", { value: s }, `${s}×`));
        speed.addEventListener("change", () => {
            S.speed = Number(speed.value);
            render();
        });
    }

    function defaultSpeed(duration) {
        // The whole run in about 90 s of playback.
        const target = (duration || 600) / 90;
        let best = SPEEDS[0];
        for (const s of SPEEDS) if (Math.abs(Math.log(s / target)) < Math.abs(Math.log(best / target))) best = s;
        return best;
    }

    function setupPlayer() {
        const tl = S.timeline;
        player.hidden = !tl;
        if (!tl) return;
        const [lo, hi] = range();
        track.setAttribute("aria-valuemin", lo.toFixed(1));
        track.setAttribute("aria-valuemax", hi.toFixed(1));
        if (S.playerFor !== tl.run.uuid) {
            S.playerFor = tl.run.uuid;
            S.speed = defaultSpeed(tl.run.duration);
        }
        S.stateEvents = tl.events.filter((e) => e.kind === "state");
        $("topo-speed").value = String(S.speed);

        // Marks for the notable events (DOM: they have titles); submissions
        // and logs go into the density strip.
        const marks = clear($("topo-marks"));
        const pct = (t) => `${(((t - lo) / (hi - lo)) * 100).toFixed(3)}%`;
        const big = new Set(["captured", "worm_started"]);
        for (const e of tl.events) {
            if (!["captured", "worm_started", "worm_deployed", "vm_stopped", "vm_started", "secret_released", "state", "error"].includes(e.kind)) continue;
            const look = eventLook(e);
            marks.appendChild(el("div", {
                class: `mark${big.has(e.kind) ? " big" : ""}`, title: `${fmtTime(e.t)} ${look.label}`,
                style: `left: ${pct(e.t)}; background: var(${look.color})`,
            }));
        }
        // Axis labels.
        const axis = clear($("topo-axis"));
        const span = hi - lo;
        const step = [10, 30, 60, 120, 300, 600, 900, 1800, 3600].find((s) => span / s <= 10) || 7200;
        for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) axis.appendChild(el("span", { style: `left: ${pct(t)}` }, fmtTime(t)));
        drawDensity();
    }

    function drawDensity() {
        const c = $("topo-density");
        if (!S.timeline || player.hidden) return;
        const rect = c.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        c.width = Math.max(1, Math.round(rect.width * dpr));
        c.height = Math.max(1, Math.round(rect.height * dpr));
        const g = c.getContext("2d");
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, rect.width, rect.height);
        const [lo, hi] = range();
        const bins = Math.max(20, Math.floor(rect.width / 4));
        const counts = new Array(bins).fill(0);
        for (const f of S.timeline.flows) counts[clamp(Math.floor(((f.t - lo) / (hi - lo)) * bins), 0, bins - 1)]++;
        const max = Math.max(1, ...counts);
        const bw = rect.width / bins;
        g.fillStyle = S.colors["--muted"];
        g.globalAlpha = 0.35;
        for (let i = 0; i < bins; i++) {
            if (!counts[i]) continue;
            const h = Math.max(1.5, (Math.sqrt(counts[i]) / Math.sqrt(max)) * 16);
            g.fillRect(i * bw + 0.5, 18 - h, Math.max(1, bw - 1), h);
        }
        g.globalAlpha = 0.9;
        for (const e of S.timeline.events) {
            if (e.kind !== "flag_submitted" && e.kind !== "log_received") continue;
            const x = ((e.t - lo) / (hi - lo)) * rect.width;
            g.fillStyle = S.colors[e.kind === "log_received" ? "--c-logging" : e.data.result === "captured" ? "--c-flag" : "--c-scoring"];
            g.fillRect(x - 0.75, e.kind === "log_received" ? 0 : 3, 1.5, 4);
        }
        g.globalAlpha = 1;
    }

    function wirePlayer() {
        buildSpeeds();
        $("topo-play").addEventListener("click", () => (S.playing ? pause() : play()));
        $("topo-back").addEventListener("click", () => seek(S.t - 10));
        $("topo-fwd").addEventListener("click", () => seek(S.t + 10));
        $("topo-prev").addEventListener("click", () => jumpEvent(-1));
        $("topo-next").addEventListener("click", () => jumpEvent(1));
        const hover = $("topo-hover");
        const tAt = (clientX) => {
            const r = track.getBoundingClientRect();
            const [lo, hi] = range();
            return lo + clamp((clientX - r.left) / r.width, 0, 1) * (hi - lo);
        };
        let dragging = false;
        let resume = false;
        track.addEventListener("pointerdown", (ev) => {
            dragging = true;
            resume = S.playing;
            S.playing = false;
            track.setPointerCapture(ev.pointerId);
            seek(tAt(ev.clientX));
        });
        track.addEventListener("pointermove", (ev) => {
            const r = track.getBoundingClientRect();
            hover.hidden = false;
            hover.style.left = `${clamp(ev.clientX - r.left, 0, r.width)}px`;
            hover.textContent = fmtTime(tAt(ev.clientX));
            if (dragging) seek(tAt(ev.clientX));
        });
        track.addEventListener("pointerleave", () => (hover.hidden = true));
        const release = () => {
            if (!dragging) return;
            dragging = false;
            if (resume) play();
            else pause();
        };
        track.addEventListener("pointerup", release);
        track.addEventListener("pointercancel", release);
        track.addEventListener("keydown", (ev) => {
            if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
                ev.preventDefault();
                seek(S.t + (ev.key === "ArrowLeft" ? -1 : 1) * (ev.shiftKey ? 60 : 10));
            }
        });
        document.addEventListener("keydown", (ev) => {
            if (!replaying()) return;
            const tag = (ev.target && ev.target.tagName) || "";
            if (["INPUT", "SELECT", "TEXTAREA"].includes(tag)) return;
            // A focused button handles space/enter itself; the other keys still work.
            if (tag === "BUTTON" && (ev.key === " " || ev.key === "Enter")) return;
            if (ev.key === " ") {
                ev.preventDefault();
                S.playing ? pause() : play();
            } else if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
                if (ev.target === track) return;
                ev.preventDefault();
                seek(S.t + (ev.key === "ArrowLeft" ? -1 : 1) * (ev.shiftKey ? 60 : 10));
            } else if (ev.key === "," || ev.key === ".") {
                jumpEvent(ev.key === "," ? -1 : 1);
            } else if (ev.key === "f" || ev.key === "F") {
                fit();
            } else if (ev.key === "Escape") {
                select(null);
            }
        });
    }

    function jumpEvent(dir) {
        const events = ((S.timeline && S.timeline.events) || []).filter((e) => e.kind !== "flag_submitted" || e.data.result !== "invalid");
        if (!events.length) return;
        let target;
        if (dir > 0) target = events.find((e) => e.t > S.t + 0.05);
        else target = [...events].reverse().find((e) => e.t < S.t - 0.05);
        if (!target) return;
        pause();
        seek(target.t);
        select({ type: "event", event: target });
    }

    // Clicks on the stage: a dot first (the canvas does not take events),
    // then whatever SVG element is underneath.
    function wireStage() {
        stage.addEventListener("click", (ev) => {
            if (svg.dataset.justPanned) {
                delete svg.dataset.justPanned;
                ev.stopPropagation();
                return;
            }
            const dot = dotAt(ev.clientX, ev.clientY);
            if (dot) {
                ev.stopPropagation();
                pause();
                select({ type: "flow", flow: dot.f });
            }
        }, true);
        svg.addEventListener("click", () => {
            if (S.selected) select(null);
        });
        stage.addEventListener("mousemove", (ev) => {
            if (!replaying()) return;
            const dot = dotAt(ev.clientX, ev.clientY);
            stage.style.cursor = dot ? "pointer" : "";
            if (dot) showTip(ev, flowTip(dot.f));
            else if (tip.dataset.flow) hideTip();
            tip.dataset.flow = dot ? "1" : "";
        });
    }

    // ------------------------------------------------------------------
    // Tabs: [Infrastructure] [round ...] then the round's teams
    // ------------------------------------------------------------------

    function tabButton(label, selected, onclick, extra) {
        return el("button", { type: "button", class: "topo-tab", role: "tab", "aria-selected": selected ? "true" : "false", onclick }, label, extra || null);
    }

    function renderTabs() {
        const rounds = clear($("topo-rounds"));
        rounds.appendChild(tabButton("Infrastructure (live)", S.mode === "live", () => selectLive()));
        const list = (S.runs && S.runs.rounds) || [];
        for (const r of list) rounds.appendChild(tabButton(shorten(r.name, 40), S.mode === "round" && S.roundId === r.id, () => selectRound(r.id)));
        if (S.file) {
            const what = S.file.timeline ? `${S.file.timeline.run.round} · ${S.file.timeline.run.team}` : "infrastructure";
            rounds.appendChild(tabButton(`File: ${shorten(S.file.name, 40)}`, S.mode === "file", () => selectFile(), el("span", { class: "state" }, shorten(what, 48))));
        }
        if (S.runsError) rounds.appendChild(el("span", { class: "topo-tabs-empty is-warn" }, `rounds: ${S.runsError.message}`));
        else if (S.runs && !list.length) rounds.appendChild(el("span", { class: "topo-tabs-empty" }, "No round has been evaluated yet."));

        const teams = clear($("topo-teams"));
        if (S.mode !== "round") return;
        const round = list.find((r) => r.id === S.roundId);
        if (!round) return;
        for (const run of round.runs) {
            teams.appendChild(tabButton(shorten(run.team, 40), S.runUuid === run.uuid, () => selectRun(run.uuid), el("span", { class: "state" }, run.state)));
        }
    }

    function selectLive() {
        pause();
        S.needsFit = true;
        S.mode = "live";
        S.model = S.liveModel;
        S.roundId = null;
        S.runUuid = null;
        S.timeline = null;
        S.timelineError = null;
        S.selected = null;
        renderTabs();
        rebuild();
        writeHash();
    }

    function selectRound(roundId) {
        const round = ((S.runs && S.runs.rounds) || []).find((r) => r.id === roundId);
        if (!round) return;
        S.mode = "round";
        S.roundId = roundId;
        renderTabs();
        const run = round.runs.find((x) => x.team === S.teamName) || round.runs[0];
        if (run) selectRun(run.uuid);
        else {
            S.timeline = null;
            rebuild();
        }
    }

    async function selectRun(uuid, t) {
        pause();
        S.needsFit = true;
        S.mode = "round";
        S.model = S.liveModel;
        S.runUuid = uuid;
        S.timeline = null;
        S.timelineError = null;
        S.selected = null;
        renderTabs();
        rebuild();
        try {
            const tl = cleanTimeline(await getJSON(cfg.timeline.replace(ZERO_UUID, uuid)));
            if (S.runUuid !== uuid || S.mode !== "round") return; // another tab was picked meanwhile
            S.timeline = tl;
            S.teamName = tl.run.team;
            S.needsFit = true;
            S.t = t !== undefined && !Number.isNaN(t) ? t : Math.min(0, tl.run.t_min);
        } catch (error) {
            S.timelineError = error;
        }
        rebuild();
        writeHash();
    }

    // The opened file: its own infrastructure and timeline; nothing is fetched.
    function selectFile() {
        if (!S.file) return;
        pause();
        S.needsFit = true;
        S.mode = "file";
        S.roundId = null;
        S.runUuid = null;
        S.model = S.file.model;
        S.timeline = S.file.timeline;
        S.timelineError = null;
        S.selected = null;
        S.playerFor = null;
        if (S.timeline) S.t = Math.min(0, S.timeline.run.t_min);
        renderTabs();
        rebuild();
        writeHash();
    }

    // ------------------------------------------------------------------
    // Building / refreshing
    // ------------------------------------------------------------------

    function rebuild() {
        const tl = S.mode !== "live" ? S.timeline : null;
        S.scene = buildScene(S.model, tl, {
            mode: tl ? "round" : "live", showAux: S.showAux, teamSlug: tl ? tl.run.team_slug : null,
        });
        drawScene();
        if (S.needsFit) fit();
        else applyView();
        setupPlayer();
        renderSide();
        renderMeta();
        renderLegend();
        const empty = $("topo-empty");
        if (S.mode === "round" && S.timelineError) {
            empty.hidden = false;
            empty.textContent = `Could not load the run: ${S.timelineError.message}`;
        } else if (S.mode !== "file" && !S.model && S.modelError) {
            empty.hidden = false;
            empty.textContent = S.modelError.message;
        } else {
            empty.hidden = true;
        }
        render();
    }

    function renderMeta() {
        const meta = clear($("topo-meta"));
        const m = S.model;
        if (S.mode === "file" && S.file) {
            meta.appendChild(document.createTextNode(`File ${shorten(S.file.name, 80)}`));
            if (S.file.exported_at) meta.appendChild(document.createTextNode(`, exported ${shorten(fmtDate(S.file.exported_at), 40)}`));
            if (m && m.collected_at) meta.appendChild(document.createTextNode(` · snapshot of ${shorten(m.host.node || "?", 60)} from ${shorten(fmtDate(m.collected_at), 40)}`));
            meta.appendChild(document.createTextNode(" · viewed locally, nothing is fetched"));
            return;
        }
        if (!m) {
            meta.appendChild(el("span", { class: "err" }, S.modelError ? S.modelError.message : "Loading the snapshot…"));
            return;
        }
        meta.appendChild(document.createTextNode(`Proxmox snapshot of ${m.host.node || "?"}, collected ${ago(m.collected_at)}`));
        if (m.stale) meta.appendChild(el("span", { class: "stale" }, " · stale"));
        if (m.collection_error) meta.appendChild(el("span", { class: "err" }, ` · last collection failed: ${m.collection_error}`));
        if (S.mode === "round") meta.appendChild(document.createTextNode(" · infrastructure status is live; the sandbox is the run's"));
    }

    function renderLegend() {
        const legend = clear($("topo-legend"));
        const item = (cls, color, label) => el("span", { style: `color: var(${color})` }, el("i", { class: cls }), el("span", { style: "color: var(--muted)" }, label));
        append(legend, [
            item("", "--c-db", "database ingress (firewall)"),
            item("", "--c-service", "→ scoring / logging"),
            item("", "--c-pi", "→ private internet"),
            item("", "--c-lan", "sandbox network"),
            item("", "--c-devbox", "devbox link"),
            item("dash", "--muted", "by design, not verified"),
        ]);
        if (replaying()) {
            append(legend, [
                item("dot", "--c-lan", "VM ↔ VM"),
                item("dot", "--c-scoring", "→ scoring"),
                item("dot", "--c-logging", "→ logging"),
                item("dot", "--c-pi", "→ private internet / DNS"),
                item("dot", "--bad", "not established (ring)"),
            ]);
        }
    }

    function writeHash() {
        const hash = S.mode === "round" && S.runUuid ? `#run=${S.runUuid}&t=${S.t.toFixed(1)}` : S.mode === "file" ? "#file" : "#live";
        if (location.hash !== hash) history.replaceState(null, "", hash);
    }

    function readHash() {
        const params = new URLSearchParams(location.hash.replace(/^#/, ""));
        return { run: params.get("run"), t: params.has("t") ? Number(params.get("t")) : undefined };
    }

    async function loadModel() {
        try {
            S.liveModel = cleanModel(await getJSON(cfg.model));
            S.modelError = null;
        } catch (error) {
            S.modelError = error;
        }
        if (S.mode !== "file") S.model = S.liveModel;
    }

    async function loadRuns() {
        try {
            S.runs = await getJSON(cfg.runs);
            S.runsError = null;
        } catch (error) {
            S.runsError = error;
        }
    }

    async function reload() {
        await Promise.all([loadModel(), loadRuns()]);
        renderTabs();
        if (S.mode !== "file") rebuild();
    }

    async function init() {
        readColors();
        resizeCanvas();
        wirePanZoom();
        wirePlayer();
        wireStage();
        $("topo-aux").addEventListener("change", (ev) => {
            S.showAux = ev.target.checked;
            S.needsFit = !S.view.user;
            rebuild();
        });
        $("topo-reload").addEventListener("click", reload);
        $("topo-download").addEventListener("click", downloadBundle);
        $("topo-open").addEventListener("click", () => $("topo-file").click());
        $("topo-file").addEventListener("change", (ev) => {
            openFile(ev.target.files && ev.target.files[0]);
            ev.target.value = ""; // the same file can be opened again
        });
        // Drop a file anywhere on the view.
        root.addEventListener("dragover", (ev) => {
            if (!ev.dataTransfer || !Array.from(ev.dataTransfer.types || []).includes("Files")) return;
            ev.preventDefault();
            root.classList.add("dragging");
        });
        root.addEventListener("dragleave", (ev) => {
            if (ev.target === root || !root.contains(ev.relatedTarget)) root.classList.remove("dragging");
        });
        root.addEventListener("drop", (ev) => {
            if (!ev.dataTransfer || !ev.dataTransfer.files.length) return;
            ev.preventDefault();
            root.classList.remove("dragging");
            openFile(ev.dataTransfer.files[0]);
        });
        if (window.matchMedia) {
            window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
                readColors();
                drawDensity();
                drawFrame();
            });
        }
        await Promise.all([loadModel(), loadRuns()]);
        renderTabs();
        const wanted = readHash();
        const round = wanted.run && ((S.runs && S.runs.rounds) || []).find((r) => r.runs.some((x) => x.uuid === wanted.run));
        if (round) {
            S.mode = "round";
            S.roundId = round.id;
            await selectRun(wanted.run, wanted.t);
        } else {
            rebuild();
        }
        // The snapshot is refreshed every few minutes on the orchestrator.
        setInterval(async () => {
            if (document.hidden || S.mode === "file") return;
            await loadModel();
            rebuild(); // keeps the view: needsFit is only set when the picture changes
        }, cfg.refreshMs);
    }

    // For the browser test: read-only access to the player's state.
    root.topoState = S;
    init();
})();
