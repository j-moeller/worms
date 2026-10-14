{% verbatim %}
// Network topology view. Data: dashboard/views/topology.py (analysis of the
// snapshot the orchestrator collects from Proxmox with a read-only token).
// Everything from the snapshot is inserted with textContent, never as HTML.
(() => {
    "use strict";

    const root = document.getElementById("topo");
    const svg = document.getElementById("topo-svg");
    const NS = "http://www.w3.org/2000/svg";
    const NO_GROUP = "(guest rule / policy)";
    const PALETTE = [
        "#2a78d6", "#d9622b", "#1a9e77", "#c43c8d", "#7a5fd3",
        "#b8860b", "#0f8fa6", "#c0392b", "#5d7f1f", "#8a5a44",
    ];
    const CARD = { w: 150, h: 50 };
    const HOST_CARD = { w: 190, h: 56 };
    const BRIDGE = { w: 120, h: 36 };

    const state = {
        data: null,
        pos: new Map(),
        hidden: { bridges: new Set(), pools: new Set(), services: new Set(), groups: new Set() },
        opts: { flows: true, unknown: true, hostFlows: true, nicLabels: true, portLabels: true, focus: false },
        selected: null, // {type: "ep"|"br"|"flow", id}
        search: "",
        view: { x: 0, y: 0, k: 1 },
        colors: new Map(),
    };

    // ---------------------------------------------------------------- helpers

    function el(tag, attrs = {}, children = []) {
        const node = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs)) {
            if (k === "text") node.textContent = v;
            else if (k === "class") node.className = v;
            else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
            else node.setAttribute(k, v);
        }
        for (const child of [].concat(children)) {
            if (child == null) continue;
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
        return node;
    }

    function sv(tag, attrs = {}, parent = null) {
        const node = document.createElementNS(NS, tag);
        for (const [k, v] of Object.entries(attrs)) {
            if (k === "text") node.textContent = v;
            else node.setAttribute(k, v);
        }
        if (parent) parent.append(node);
        return node;
    }

    function hash(str) {
        let h = 2166136261;
        for (let i = 0; i < str.length; i++) h = Math.imul(h ^ str.charCodeAt(i), 16777619);
        return (h >>> 0) / 4294967295;
    }

    const byId = () => new Map(state.data.endpoints.map((ep) => [ep.id, ep]));
    const poolOf = (ep) => (ep.kind === "host" ? "proxmox host" : ep.pool || "no pool");
    const nicEnforced = (ep, nic) => Boolean(ep.firewall_enabled && nic.firewall);
    const nicBridge = (ep, name) => (ep.nics.find((n) => n.name === name) || {}).bridge;
    const epLabel = (ep) => (ep.kind === "host" ? `Proxmox host (${ep.name})` : ep.name);

    function ruleText(r) {
        const service = r.macro ? `${r.macro} (${r.proto || "any"}/${r.dport || "*"})` : `${r.proto || "any"}${r.dport ? "/" + r.dport : ""}`;
        const peer = r.direction === "in" ? `from ${r.source || "any"}` : `to ${r.dest || "any"}`;
        return `${r.action} ${r.direction.toUpperCase()} ${service} ${peer}${r.iface ? " on " + r.iface : ""}`;
    }

    function decisionText(d) {
        if (d.rule) {
            const where = d.rule.group ? `security group "${d.rule.group}"` : `${d.rule.origin} rule`;
            return `${ruleText(d.rule)} — ${where}${d.rule.comment ? ` (${d.rule.comment})` : ""}`;
        }
        if ((d.verdict === "allow") !== (d.policy === "ACCEPT")) return "not filtered (firewall off for this NIC/guest)";
        return `no rule matched → policy_${d.direction} ${d.policy}`;
    }

    function verdictPill(v) {
        const text = { allow: "ALLOWED", deny: "BLOCKED", unknown: "DEPENDS" }[v] || v;
        return el("span", { class: `topo-pill v-${v}`, text });
    }

    function fwClass(ep) {
        if (!ep.nics.length) return "fw-warn";
        if (!ep.nics.every((n) => nicEnforced(ep, n))) return "fw-bad";
        if (ep.policy_in !== "DROP" && ep.policy_in !== "REJECT") return "fw-warn";
        if (ep.kind !== "host" && ep.policy_out !== "DROP" && ep.policy_out !== "REJECT") return "fw-warn";
        return "fw-ok";
    }

    // --------------------------------------------------------- visibility

    function bridgeVisible(id) {
        return !state.hidden.bridges.has(id);
    }

    function endpointVisible(ep) {
        if (state.hidden.pools.has(poolOf(ep))) return false;
        if (ep.kind === "host") return true;
        const bridged = ep.nics.filter((n) => n.bridge);
        return !bridged.length || bridged.some((n) => bridgeVisible(n.bridge));
    }

    function flowVisible(f, eps) {
        const o = state.opts;
        if (!o.flows || state.hidden.services.has(f.service)) return false;
        if (f.verdict === "unknown" && !o.unknown) return false;
        if (f.from === "host" && !o.hostFlows) return false;
        const a = eps.get(f.from), b = eps.get(f.to);
        if (!a || !b || !endpointVisible(a) || !endpointVisible(b)) return false;
        const ba = nicBridge(a, f.src_nic), bb = nicBridge(b, f.dst_nic);
        if ((ba && !bridgeVisible(ba)) || (bb && !bridgeVisible(bb))) return false;
        const groups = f.groups.length ? f.groups : [NO_GROUP];
        if (groups.some((g) => state.hidden.groups.has(g))) return false;
        if (o.focus && state.selected && state.selected.type === "ep") {
            if (f.from !== state.selected.id && f.to !== state.selected.id) return false;
        }
        return true;
    }

    // ------------------------------------------------------------- layout

    function graph(onlyVisible) {
        const d = state.data;
        const nodes = [];
        const edges = [];
        for (const b of d.bridges) {
            if (onlyVisible && !bridgeVisible(b.id)) continue;
            nodes.push({ id: "br:" + b.id, r: 70 });
        }
        const present = new Set(nodes.map((n) => n.id));
        for (const ep of d.endpoints) {
            if (onlyVisible && !endpointVisible(ep)) continue;
            nodes.push({ id: ep.id, r: ep.kind === "host" ? 110 : 90 });
            for (const nic of ep.nics) {
                if (nic.bridge && present.has("br:" + nic.bridge)) edges.push([ep.id, "br:" + nic.bridge]);
            }
        }
        return { nodes, edges };
    }

    function initialPosition(id) {
        const d = state.data;
        const bridges = d.bridges.map((b) => b.id);
        const bx = (bid) => (bridges.indexOf(bid) - (bridges.length - 1) / 2) * 260;
        if (id.startsWith("br:")) return { x: bx(id.slice(3)), y: 0 };
        if (id === "host") return { x: 0, y: -320 };
        const ep = d.endpoints.find((e) => e.id === id);
        const own = ep.nics.map((n) => n.bridge).filter(Boolean);
        const x = own.length ? own.reduce((s, b) => s + bx(b), 0) / own.length : 0;
        return { x: x + (hash(id) - 0.5) * 300, y: 260 + hash(id + "y") * 420 };
    }

    // Deterministic Fruchterman–Reingold with collision; positions persist.
    function layout(onlyVisible, reset) {
        const { nodes, edges } = graph(onlyVisible);
        for (const n of nodes) {
            if (reset || !state.pos.has(n.id)) state.pos.set(n.id, initialPosition(n.id));
            const p = state.pos.get(n.id);
            n.x = p.x; n.y = p.y; n.dx = 0; n.dy = 0;
        }
        const index = new Map(nodes.map((n) => [n.id, n]));
        const k = 210;
        let temp = 120;
        for (let it = 0; it < 320; it++) {
            for (const n of nodes) { n.dx = 0; n.dy = -n.y * 0.004; n.dx -= n.x * 0.002; }
            for (let i = 0; i < nodes.length; i++) {
                for (let j = i + 1; j < nodes.length; j++) {
                    const a = nodes[i], b = nodes[j];
                    let dx = a.x - b.x, dy = a.y - b.y;
                    let dist = Math.hypot(dx, dy) || 0.01;
                    let force = (k * k) / dist;
                    const min = a.r + b.r;
                    if (dist < min) force += (min - dist) * 4;
                    dx /= dist; dy /= dist;
                    a.dx += dx * force * 0.02; a.dy += dy * force * 0.02;
                    b.dx -= dx * force * 0.02; b.dy -= dy * force * 0.02;
                }
            }
            for (const [s, t] of edges) {
                const a = index.get(s), b = index.get(t);
                const dx = a.x - b.x, dy = a.y - b.y;
                const dist = Math.hypot(dx, dy) || 0.01;
                const force = (dist * dist) / k * 0.02;
                a.dx -= (dx / dist) * force; a.dy -= (dy / dist) * force;
                b.dx += (dx / dist) * force; b.dy += (dy / dist) * force;
            }
            for (const n of nodes) {
                const len = Math.hypot(n.dx, n.dy) || 0.01;
                const step = Math.min(len, temp);
                n.x += (n.dx / len) * step;
                n.y += (n.dy / len) * step;
            }
            temp = Math.max(2, temp * 0.985);
        }
        for (const n of nodes) state.pos.set(n.id, { x: n.x, y: n.y });
    }

    // ----------------------------------------------------------- geometry

    function boxOf(id) {
        if (id.startsWith("br:")) return BRIDGE;
        return id === "host" ? HOST_CARD : CARD;
    }

    // Point where the segment from the box centre towards (tx, ty) leaves the box.
    function clip(p, box, tx, ty, pad = 4) {
        const dx = tx - p.x, dy = ty - p.y;
        if (!dx && !dy) return { x: p.x, y: p.y };
        const sx = dx ? (box.w / 2 + pad) / Math.abs(dx) : Infinity;
        const sy = dy ? (box.h / 2 + pad) / Math.abs(dy) : Infinity;
        const s = Math.min(sx, sy);
        return { x: p.x + dx * s, y: p.y + dy * s };
    }

    // -------------------------------------------------------------- render

    let layers = null;

    function ensureLayers() {
        if (layers) return layers;
        svg.textContent = "";
        const defs = sv("defs", {}, svg);
        const viewport = sv("g", {}, svg);
        layers = {
            defs,
            viewport,
            nics: sv("g", {}, viewport),
            flows: sv("g", {}, viewport),
            nodes: sv("g", {}, viewport),
            labels: sv("g", {}, viewport),
        };
        return layers;
    }

    function marker(color) {
        const id = "arrow-" + color.replace("#", "");
        if (!layers.defs.querySelector("#" + id)) {
            const m = sv("marker", { id, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" }, layers.defs);
            sv("path", { d: "M0,0 L10,5 L0,10 z", fill: color }, m);
        }
        return `url(#${id})`;
    }

    function applyView() {
        const v = state.view;
        layers.viewport.setAttribute("transform", `translate(${v.x},${v.y}) scale(${v.k})`);
    }

    function highlightSet() {
        const sel = state.selected;
        if (!sel) return null;
        const keep = new Set();
        const eps = byId();
        if (sel.type === "ep") {
            keep.add(sel.id);
            const ep = eps.get(sel.id);
            ep.nics.forEach((n) => n.bridge && keep.add("br:" + n.bridge));
            for (const f of state.data.flows) {
                if (f.from === sel.id || f.to === sel.id) { keep.add(f.id); keep.add(f.from); keep.add(f.to); }
            }
        } else if (sel.type === "br") {
            keep.add(sel.id);
            for (const ep of state.data.endpoints) {
                if (ep.nics.some((n) => "br:" + n.bridge === sel.id)) keep.add(ep.id);
            }
        } else if (sel.type === "flow") {
            const f = state.data.flows.find((x) => x.id === sel.id);
            if (f) { keep.add(f.id); keep.add(f.from); keep.add(f.to); }
        }
        return keep;
    }

    function matches(ep) {
        const q = state.search.trim().toLowerCase();
        if (!q) return false;
        return [ep.name, String(ep.vmid || ""), ...ep.nics.map((n) => n.ip)].some((s) => s && s.toLowerCase().includes(q));
    }

    function render() {
        ensureLayers();
        const d = state.data;
        const eps = byId();
        const keep = highlightSet();
        const dim = (id) => (keep && !keep.has(id) ? " dim" : "");
        layers.nics.textContent = "";
        layers.flows.textContent = "";
        layers.nodes.textContent = "";
        layers.labels.textContent = "";

        const visibleEps = d.endpoints.filter(endpointVisible);
        const visibleBridges = d.bridges.filter((b) => bridgeVisible(b.id));
        const pos = (id) => state.pos.get(id) || (state.pos.set(id, initialPosition(id)), state.pos.get(id));

        // NIC edges (guest/host <-> bridge)
        for (const ep of visibleEps) {
            const a = pos(ep.id);
            for (const nic of ep.nics) {
                if (!nic.bridge || !bridgeVisible(nic.bridge)) continue;
                const b = pos("br:" + nic.bridge);
                const unfiltered = !nicEnforced(ep, nic);
                const line = sv("line", {
                    x1: a.x, y1: a.y, x2: b.x, y2: b.y,
                    class: "nic" + (unfiltered ? " unfiltered" : "") + dim(ep.id),
                }, layers.nics);
                sv("title", { text: `${ep.name} ${nic.name} → ${nic.bridge}${nic.ip ? " · " + nic.cidr : ""}${unfiltered ? " · NOT FILTERED" : ""}` }, line);
                if (state.opts.nicLabels && nic.ip) {
                    const s = clip(a, boxOf(ep.id), b.x, b.y, 6);
                    const t = 0.28;
                    sv("text", {
                        x: s.x + (b.x - s.x) * t, y: s.y + (b.y - s.y) * t,
                        class: "mono muted halo" + dim(ep.id), "font-size": "11", "text-anchor": "middle",
                        text: nic.ip,
                    }, layers.labels);
                }
            }
        }

        // Flows (allowed connection setups)
        const flows = d.flows.filter((f) => flowVisible(f, eps));
        const pairs = new Map();
        for (const f of flows) {
            const key = [f.from, f.to].sort().join("|");
            if (!pairs.has(key)) pairs.set(key, []);
            pairs.get(key).push(f);
        }
        for (const list of pairs.values()) {
            list.forEach((f, i) => {
                const a = pos(f.from), b = pos(f.to);
                const forward = f.from < f.to ? 1 : -1;
                const offset = (i - (list.length - 1) / 2) * 34 + forward * 10;
                const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
                const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
                // Perpendicular offset, in a direction independent of flow direction.
                const sign = f.from < f.to ? 1 : -1;
                const nx = (-(b.y - a.y) / len) * sign, ny = ((b.x - a.x) / len) * sign;
                const cx = mx + nx * offset, cy = my + ny * offset;
                const s = clip(a, boxOf(f.from), cx, cy);
                const e = clip(b, boxOf(f.to), cx, cy, 6);
                const color = state.colors.get(f.service) || "#888";
                const dpath = `M${s.x},${s.y} Q${cx},${cy} ${e.x},${e.y}`;
                const selected = state.selected && state.selected.type === "flow" && state.selected.id === f.id;
                const path = sv("path", {
                    d: dpath, stroke: color, "marker-end": marker(color),
                    class: "flow" + (f.verdict === "unknown" ? " unknown" : "") + (selected ? " selected" : "") + dim(f.id),
                }, layers.flows);
                const hit = sv("path", { d: dpath, class: "flow-hit" }, layers.flows);
                const title = `${epLabel(eps.get(f.from))} → ${epLabel(eps.get(f.to))} ${f.service}${f.verdict === "unknown" ? " (depends on rules not visible via the API)" : ""}`;
                sv("title", { text: title }, hit);
                attachClick(hit, { type: "flow", id: f.id });
                attachClick(path, { type: "flow", id: f.id });
                if (state.opts.portLabels) {
                    // Point on the quadratic curve at t = 0.5.
                    const lx = 0.25 * s.x + 0.5 * cx + 0.25 * e.x;
                    const ly = 0.25 * s.y + 0.5 * cy + 0.25 * e.y;
                    sv("text", {
                        x: lx, y: ly + 4, "text-anchor": "middle", "font-size": "11",
                        class: "mono halo" + dim(f.id), style: `fill:${color}`, text: f.service,
                    }, layers.labels);
                }
            });
        }

        // Bridges
        for (const b of visibleBridges) {
            const id = "br:" + b.id;
            const p = pos(id);
            const g = sv("g", {
                transform: `translate(${p.x},${p.y})`,
                class: "node br" + (b.missing ? " missing" : "") + (state.selected && state.selected.id === id ? " selected" : "") + dim(id),
                "data-id": id,
            }, layers.nodes);
            sv("rect", { x: -BRIDGE.w / 2, y: -BRIDGE.h / 2, width: BRIDGE.w, height: BRIDGE.h, rx: BRIDGE.h / 2 }, g);
            sv("text", { y: b.cidr ? -2 : 4, "text-anchor": "middle", "font-size": "13", "font-weight": "700", class: "mono", text: b.id }, g);
            if (b.cidr) sv("text", { y: 12, "text-anchor": "middle", "font-size": "10", class: "mono muted", text: b.cidr }, g);
            sv("title", { text: `${b.id}${b.cidr ? " · " + b.cidr : ""}${b.ports ? " · uplink " + b.ports : " · no uplink"}${b.comment ? "\n" + b.comment : ""}` }, g);
            attachNode(g, id, { type: "br", id });
        }

        // Endpoints
        for (const ep of visibleEps) {
            const p = pos(ep.id);
            const box = boxOf(ep.id);
            const sel = state.selected && state.selected.id === ep.id;
            const g = sv("g", {
                transform: `translate(${p.x},${p.y})`,
                class: `node ep ${ep.kind === "host" ? "host" : ""} ${ep.status === "stopped" ? "stopped" : ""}${sel ? " selected" : ""}${matches(ep) ? " match" : ""}${dim(ep.id)}`,
                "data-id": ep.id,
            }, layers.nodes);
            sv("rect", { class: "card", x: -box.w / 2, y: -box.h / 2, width: box.w, height: box.h, rx: 6 }, g);
            if (ep.kind !== "host") {
                sv("circle", { cx: -box.w / 2 + 11, cy: -box.h / 2 + 13, r: 4, class: ep.status === "running" ? "status-running" : "status-stopped" }, g);
            }
            sv("text", { x: ep.kind === "host" ? 0 : -box.w / 2 + 20, y: -4, "text-anchor": ep.kind === "host" ? "middle" : "start", "font-size": "13", "font-weight": "700", text: ep.kind === "host" ? "PROXMOX HOST" : ep.name.length > 17 ? ep.name.slice(0, 16) + "…" : ep.name }, g);
            sv("text", {
                x: ep.kind === "host" ? 0 : -box.w / 2 + 10, y: 13, "text-anchor": ep.kind === "host" ? "middle" : "start", "font-size": "10", class: "mono " + (ep.kind === "host" ? "" : "muted"),
                text: ep.kind === "host" ? `${ep.name} · API :${state.data.api_port}` : `#${ep.vmid} · ${poolOf(ep)}`,
            }, g);
            const badge = sv("g", { transform: `translate(${box.w / 2 - 22},${-box.h / 2 + 4})` }, g);
            sv("rect", { width: 18, height: 12, rx: 3, class: fwClass(ep) }, badge);
            sv("text", { x: 9, y: 9.5, "text-anchor": "middle", "font-size": "8", "font-weight": "700", style: "fill:#fff", text: "FW" }, badge);
            sv("title", { text: `${epLabel(ep)}\nfirewall ${ep.firewall_enabled ? "on" : "OFF"} · in ${ep.policy_in} · out ${ep.policy_out}\n${ep.nics.map((n) => `${n.name} ${n.bridge || "-"} ${n.cidr || ""}`).join("\n")}` }, g);
            attachNode(g, ep.id, { type: "ep", id: ep.id });
        }

        const empty = document.getElementById("topo-empty");
        empty.hidden = visibleEps.length > 0;
        empty.textContent = "Everything is filtered out. Re-enable bridges or pools on the left.";
        applyView();
    }

    // ---------------------------------------------------------- interaction

    let drag = null;

    function svgPoint(ev) {
        const r = svg.getBoundingClientRect();
        return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    }

    function attachNode(g, id, selection) {
        g.addEventListener("pointerdown", (ev) => {
            ev.stopPropagation();
            const p = svgPoint(ev);
            const start = state.pos.get(id);
            drag = { kind: "node", id, sx: p.x, sy: p.y, ox: start.x, oy: start.y, moved: false, selection };
            svg.setPointerCapture(ev.pointerId);
        });
    }

    // Selectable but not draggable (flows).
    function attachClick(target, selection) {
        target.addEventListener("pointerdown", (ev) => {
            ev.stopPropagation();
            const p = svgPoint(ev);
            drag = { kind: "click", sx: p.x, sy: p.y, moved: false, selection };
            svg.setPointerCapture(ev.pointerId);
        });
    }

    svg.addEventListener("pointerdown", (ev) => {
        const p = svgPoint(ev);
        drag = { kind: "pan", sx: p.x, sy: p.y, ox: state.view.x, oy: state.view.y, moved: false };
        svg.classList.add("panning");
        svg.setPointerCapture(ev.pointerId);
    });

    svg.addEventListener("pointermove", (ev) => {
        if (!drag) return;
        const p = svgPoint(ev);
        const dx = p.x - drag.sx, dy = p.y - drag.sy;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        if (!drag.moved) return;
        if (drag.kind === "click") return;
        if (drag.kind === "pan") {
            state.view.x = drag.ox + dx;
            state.view.y = drag.oy + dy;
            applyView();
        } else {
            state.pos.set(drag.id, { x: drag.ox + dx / state.view.k, y: drag.oy + dy / state.view.k });
            render();
        }
    });

    svg.addEventListener("pointerup", () => {
        if (drag && !drag.moved) {
            select(drag.kind === "pan" ? null : drag.selection);
        }
        drag = null;
        svg.classList.remove("panning");
    });

    svg.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const p = svgPoint(ev);
        const v = state.view;
        const k = Math.min(3, Math.max(0.15, v.k * Math.exp(-ev.deltaY * 0.0015)));
        v.x = p.x - ((p.x - v.x) * k) / v.k;
        v.y = p.y - ((p.y - v.y) * k) / v.k;
        v.k = k;
        applyView();
    }, { passive: false });

    function fit() {
        const ids = [
            ...state.data.bridges.filter((b) => bridgeVisible(b.id)).map((b) => "br:" + b.id),
            ...state.data.endpoints.filter(endpointVisible).map((e) => e.id),
        ];
        if (!ids.length) return;
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        for (const id of ids) {
            const p = state.pos.get(id), b = boxOf(id);
            if (!p) continue;
            x0 = Math.min(x0, p.x - b.w / 2); x1 = Math.max(x1, p.x + b.w / 2);
            y0 = Math.min(y0, p.y - b.h / 2); y1 = Math.max(y1, p.y + b.h / 2);
        }
        const r = svg.getBoundingClientRect();
        const pad = 40;
        const k = Math.min(2, (r.width - pad * 2) / (x1 - x0 || 1), (r.height - pad * 2) / (y1 - y0 || 1));
        state.view = { k, x: r.width / 2 - ((x0 + x1) / 2) * k, y: r.height / 2 - ((y0 + y1) / 2) * k };
        applyView();
    }

    function select(selection) {
        state.selected = selection;
        render();
        renderDetail();
    }

    // -------------------------------------------------------------- panels

    function section(title, action) {
        return el("h4", {}, [title, action || null]);
    }

    function toggle(label, checked, onchange, extra = []) {
        const input = el("input", { type: "checkbox" });
        input.checked = checked;
        input.addEventListener("change", () => onchange(input.checked));
        return el("label", {}, [input, ...[].concat(label), ...extra]);
    }

    function setGroup(set, keys, visible) {
        keys.forEach((k) => (visible ? set.delete(k) : set.add(k)));
        renderFilters();
        render();
    }

    function allNone(set, keys) {
        const allHidden = keys.every((k) => set.has(k));
        return el("button", { type: "button", text: allHidden ? "all" : "none", onclick: () => setGroup(set, keys, allHidden) });
    }

    function renderFilters() {
        const d = state.data;
        const box = document.getElementById("topo-filters");
        box.textContent = "";

        box.append(section("Show"));
        const opts = [
            ["flows", "Allowed connections"],
            ["unknown", "…incl. uncertain (dashed)"],
            ["hostFlows", "…incl. from Proxmox host"],
            ["portLabels", "Port labels"],
            ["nicLabels", "NIC IP labels"],
            ["focus", "Only flows of selected VM"],
        ];
        for (const [key, label] of opts) {
            box.append(toggle(label, state.opts[key], (v) => { state.opts[key] = v; render(); }));
        }

        const bridgeIds = d.bridges.map((b) => b.id);
        box.append(section("Bridges", allNone(state.hidden.bridges, bridgeIds)));
        for (const b of d.bridges) {
            const count = d.endpoints.filter((e) => e.kind !== "host" && e.nics.some((n) => n.bridge === b.id)).length;
            box.append(toggle(
                [el("span", { class: "topo-mono", text: b.id }), el("span", { class: "sub", text: b.cidr })],
                bridgeVisible(b.id),
                (v) => setGroup(state.hidden.bridges, [b.id], v),
                [el("span", { class: "count", text: count })],
            ));
        }

        const pools = [...new Set(d.endpoints.map(poolOf))].sort();
        box.append(section("Pools", allNone(state.hidden.pools, pools)));
        for (const p of pools) {
            box.append(toggle(p, !state.hidden.pools.has(p), (v) => setGroup(state.hidden.pools, [p], v),
                [el("span", { class: "count", text: d.endpoints.filter((e) => poolOf(e) === p).length })]));
        }

        const services = [...state.colors.keys()];
        box.append(section("Services / ports", allNone(state.hidden.services, services)));
        for (const s of services) {
            box.append(toggle(
                [el("span", { class: "topo-swatch", style: `background:${state.colors.get(s)}` }), el("span", { class: "topo-mono", text: s })],
                !state.hidden.services.has(s),
                (v) => setGroup(state.hidden.services, [s], v),
                [el("span", { class: "count", text: d.flows.filter((f) => f.service === s).length })],
            ));
        }

        const groups = [...d.groups.map((g) => g.name), NO_GROUP];
        box.append(section("Firewall security groups", allNone(state.hidden.groups, groups)));
        for (const g of groups) {
            const info = d.groups.find((x) => x.name === g);
            box.append(toggle(
                el("span", { class: g === NO_GROUP ? "sub" : "topo-mono", text: g }),
                !state.hidden.groups.has(g),
                (v) => setGroup(state.hidden.groups, [g], v),
                [el("span", { class: "count", text: info ? `${info.members.length} VM` : "" })],
            ));
        }
    }

    function kvTable(rows) {
        return el("table", { class: "kv" }, rows.filter(Boolean).map(([k, v]) => el("tr", {}, [el("td", { text: k }), el("td", {}, v instanceof Node ? v : String(v ?? "—"))])));
    }

    function table(head, rows) {
        return el("table", {}, [
            el("tr", {}, head.map((h) => el("th", { text: h }))),
            ...rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, c instanceof Node ? c : String(c ?? ""))))),
        ]);
    }

    function link(text, selection) {
        return el("a", { text, onclick: () => select(selection) });
    }

    function flowList(flows, eps, other) {
        if (!flows.length) return el("p", { class: "subtitle", text: "none" });
        return table(["peer", "service", ""], flows.map((f) => [
            link(epLabel(eps.get(f[other])), { type: "ep", id: f[other] }),
            link(f.service, { type: "flow", id: f.id }),
            verdictPill(f.verdict),
        ]));
    }

    function renderDetail() {
        const box = document.getElementById("topo-detail");
        box.textContent = "";
        const d = state.data;
        const sel = state.selected;
        const eps = byId();

        if (!sel) {
            box.append(
                el("p", { class: "title", text: "Details" }),
                el("p", { class: "subtitle", text: "Click a VM, bridge or connection. Drag to move, scroll to zoom." }),
                section("Reading the graph"),
                el("ul", {}, [
                    el("li", { text: "Arrows are connections the Proxmox firewall lets the source open (egress of the source AND ingress of the target accept)." }),
                    el("li", { text: "Dashed arrows depend on rules the API cannot show (e.g. Proxmox's implicit management access from local_network)." }),
                    el("li", { text: "Red dashed NIC lines are unfiltered (NIC firewall=0, guest or datacenter firewall off)." }),
                    el("li", { text: "FW badge: green = enforced with DROP policies, amber = an ACCEPT policy, red = some NIC unfiltered." }),
                ]),
            );
            return;
        }

        if (sel.type === "br") {
            const b = d.bridges.find((x) => "br:" + x.id === sel.id);
            const nics = [];
            for (const ep of d.endpoints) for (const n of ep.nics) if (n.bridge === b.id) nics.push([ep, n]);
            box.append(
                el("p", { class: "title topo-mono", text: b.id }),
                el("p", { class: "subtitle", text: b.missing ? "Referenced by a NIC but not defined on the host!" : "Linux bridge" }),
                kvTable([["Host address", b.cidr || "none"], ["Uplink ports", b.ports || "none (isolated)"], ["Active", b.active ? "yes" : "no"], ["VLAN aware", b.vlan_aware ? "yes" : "no"], b.comment && ["Comment", b.comment]]),
                section(`Attached NICs (${nics.length})`),
                table(["guest", "nic", "address", "fw"], nics.map(([ep, n]) => [link(epLabel(ep), { type: "ep", id: ep.id }), n.name, n.cidr || "—", nicEnforced(ep, n) ? "on" : el("span", { class: "v-deny", text: "OFF" })])),
            );
            return;
        }

        if (sel.type === "flow") {
            const f = d.flows.find((x) => x.id === sel.id);
            if (!f) return;
            const a = eps.get(f.from), b = eps.get(f.to);
            box.append(
                el("p", { class: "title" }, [`${epLabel(a)} → ${epLabel(b)}`]),
                el("p", { class: "subtitle" }, [el("span", { class: "topo-mono", text: f.service }), " ", verdictPill(f.verdict)]),
                kvTable([["Path", f.path], ["Source NIC", `${f.src_nic} (${nicBridge(a, f.src_nic) || "-"})`], ["Target", `${f.dst_ip} on ${f.dst_nic}`], ["Granted by groups", f.groups.join(", ") || "—"]]),
                section("Egress (source)"),
                el("p", {}, [verdictPill(f.egress.verdict), " ", decisionText(f.egress)]),
                section("Ingress (target)"),
                el("p", {}, [verdictPill(f.ingress.verdict), " ", decisionText(f.ingress)]),
            );
            return;
        }

        const ep = eps.get(sel.id);
        if (!ep) return;
        const out = d.flows.filter((f) => f.from === ep.id);
        const inc = d.flows.filter((f) => f.to === ep.id);
        box.append(
            el("p", { class: "title", text: epLabel(ep) }),
            el("p", { class: "subtitle", text: ep.kind === "host" ? `Proxmox VE ${ep.version || ""}` : `VMID ${ep.vmid} · ${ep.kind} · ${ep.status}` }),
            kvTable([
                ep.kind !== "host" && ["Pool", poolOf(ep)],
                ep.kind !== "host" && ["Tags", ep.tags.join(", ") || "—"],
                ["Firewall", ep.firewall_enabled ? "enabled" : el("span", { class: "v-deny", text: "DISABLED" })],
                ["Policy in / out", `${ep.policy_in} / ${ep.policy_out}`],
                ep.firewall_options && ["IP / MAC filter", `${ep.firewall_options.ipfilter == 1 ? "on" : "off"} / ${ep.firewall_options.macfilter == 0 ? "off" : "on"}`],
                ep.security_groups && ["Security groups", ep.security_groups.join(", ") || "—"],
                ep.kind === "host" && ["Firewall daemons", (ep.services || []).map((s) => `${s.name}: ${s.state || s.active_state}`).join(", ") || "unknown"],
            ]),
            section(`NICs (${ep.nics.length})`),
            table(["nic", "bridge", "address", "fw"], ep.nics.map((n) => [
                n.name,
                n.bridge ? link(n.bridge, { type: "br", id: "br:" + n.bridge }) : "—",
                (n.cidr || "—") + (n.gateway ? ` gw ${n.gateway}` : ""),
                nicEnforced(ep, n) ? "on" : el("span", { class: "v-deny", text: "OFF" }),
            ])),
            section(`Effective rules (${ep.rules.length}, top-down)`),
            ep.rules.length
                ? table(["", "rule", "from"], ep.rules.map((r) => [
                    el("span", { class: r.action === "ACCEPT" ? "v-allow" : "v-deny", text: r.direction.toUpperCase() }),
                    ruleText(r),
                    r.group || r.origin,
                ]))
                : el("p", { class: "subtitle", text: `No rules: everything falls through to policy in ${ep.policy_in} / out ${ep.policy_out}.` }),
            section(`Outgoing connections (${out.length})`),
            flowList(out, eps, "to"),
            section(`Incoming connections (${inc.length})`),
            flowList(inc, eps, "from"),
        );
    }

    function renderChecks() {
        const d = state.data;
        const box = document.getElementById("topo-checks");
        box.textContent = "";
        const eps = byId();

        const reach = d.api_access.filter((a) => a.verdict !== "deny");
        const blocked = d.api_access.filter((a) => a.verdict === "deny");
        const dashboard = d.api_access.find((a) => a.name === "dashboard");
        const dashboardOk = !dashboard || dashboard.verdict === "deny";
        box.append(el("div", { class: "topo-card " + (dashboardOk ? "ok" : "bad") }, [
            el("h3", { text: `Proxmox API (tcp/${d.api_port})` }),
            dashboard
                ? el("div", {}, [el("strong", { text: "dashboard → Proxmox API: " }), verdictPill(dashboard.verdict), " ", dashboard.egress ? decisionText(dashboard.egress) : "no network path"])
                : el("div", { text: "No guest named \"dashboard\" found." }),
            el("div", { text: "Guests whose firewall lets them reach the API:" }),
            el("ul", {}, reach.length
                ? reach.map((a) => el("li", {}, [link(a.name, { type: "ep", id: a.endpoint }), " ", verdictPill(a.verdict), ` egress ${a.egress.verdict}, host ingress ${a.ingress.verdict}`]))
                : [el("li", { text: "none" })]),
            el("details", {}, [
                el("summary", { text: `${blocked.length} guests blocked` }),
                el("ul", {}, blocked.map((a) => el("li", {}, [link(a.name, { type: "ep", id: a.endpoint }), ": ", a.egress && a.egress.verdict === "deny" ? "own egress blocks it" : a.ingress ? "host ingress blocks it" : "no network path"]))),
            ]),
        ]));

        const host = eps.get("host");
        const daemons = (host && host.services) || [];
        const running = daemons.filter((s) => (s.state || s.active_state) === "running" || s.active_state === "active");
        const fwState = d.cluster_firewall.options.enable == 1 && running.length;
        box.append(el("div", { class: "topo-card " + (fwState ? "ok" : "bad") }, [
            el("h3", { text: "Firewall enforcement" }),
            el("div", { text: `Datacenter firewall: ${d.cluster_firewall.options.enable == 1 ? "enabled" : "DISABLED"} · policy in ${d.cluster_firewall.options.policy_in || "DROP"} / out ${d.cluster_firewall.options.policy_out || "ACCEPT"}` }),
            el("div", { text: `Daemons: ${daemons.map((s) => `${s.name} ${s.state || s.active_state}`).join(", ") || "unknown (no Sys.Audit?)"}` }),
            d.warnings.length ? el("ul", {}, d.warnings.map((w) => el("li", { class: "v-deny", text: w }))) : el("div", { class: "v-allow", text: "Every guest NIC is filtered." }),
        ]));

        if (d.errors.length || d.collection_error) {
            box.append(el("div", { class: "topo-card warn" }, [
                el("h3", { text: "Collection problems" }),
                d.collection_error ? el("div", { text: `Last collection failed: ${d.collection_error}` }) : null,
                el("details", {}, [
                    el("summary", { text: `${d.errors.length} resources could not be read (token privileges?)` }),
                    el("ul", {}, d.errors.map((e) => el("li", { class: "topo-mono", text: `${e.path}: ${e.error}` }))),
                ]),
            ]));
        }
    }

    function renderMeta() {
        const d = state.data;
        const meta = document.getElementById("topo-meta");
        meta.textContent = "";
        const when = new Date(d.collected_at);
        meta.append(
            `${d.endpoints.length - 1} guests · ${d.bridges.length} bridges · ${d.flows.length} allowed connections · node ${d.node} · collected ${when.toLocaleString()}`,
        );
        if (d.stale) meta.append(" ", el("span", { class: "stale", text: "STALE — is the orchestrator's collect-topology timer running?" }));
    }

    function renderLegend() {
        const lg = document.getElementById("topo-legend");
        lg.textContent = "";
        const line = (style) => el("span", { class: "topo-swatch", style });
        lg.append(
            el("span", {}, [line("background:var(--ink)"), "allowed"]),
            el("span", {}, [line("background:repeating-linear-gradient(90deg,var(--warn) 0 4px,transparent 4px 7px)"), "depends"]),
            el("span", {}, [line("background:repeating-linear-gradient(90deg,var(--bad) 0 4px,transparent 4px 7px)"), "unfiltered NIC"]),
            el("span", {}, [line("background:var(--ok)"), "FW enforced"]),
            el("span", {}, [line("background:var(--warn)"), "FW ACCEPT policy"]),
            el("span", {}, [line("background:var(--bad)"), "FW off"]),
        );
    }

    // ---------------------------------------------------------------- data

    async function load(initial) {
        const meta = document.getElementById("topo-meta");
        let response, body;
        try {
            response = await fetch(root.dataset.src, { credentials: "same-origin", headers: { Accept: "application/json" } });
            body = await response.json();
        } catch (e) {
            meta.textContent = `Could not load topology: ${e}`;
            return;
        }
        if (!response.ok) {
            meta.textContent = body.error || `HTTP ${response.status}`;
            if (body.collection_error) meta.append(" — last attempt: ", body.collection_error);
            return;
        }
        if (!initial && state.data && state.data.collected_at === body.collected_at) {
            state.data.stale = body.stale;
            renderMeta();
            return;
        }

        state.data = body;
        const services = [...new Set(body.flows.map((f) => f.service))];
        services.forEach((s) => { if (!state.colors.has(s)) state.colors.set(s, PALETTE[state.colors.size % PALETTE.length]); });
        if (state.selected) {
            const exists = state.selected.type === "flow"
                ? body.flows.some((f) => f.id === state.selected.id)
                : state.selected.type === "br"
                    ? body.bridges.some((b) => "br:" + b.id === state.selected.id)
                    : body.endpoints.some((e) => e.id === state.selected.id);
            if (!exists) state.selected = null;
        }

        const fresh = state.pos.size === 0;
        layout(false, false);
        renderMeta();
        renderChecks();
        renderFilters();
        renderLegend();
        render();
        renderDetail();
        if (fresh) fit();
    }

    document.getElementById("topo-fit").addEventListener("click", fit);
    document.getElementById("topo-reload").addEventListener("click", () => load(true));
    document.getElementById("topo-relayout").addEventListener("click", () => {
        layout(true, true);
        render();
        fit();
    });
    document.getElementById("topo-search").addEventListener("input", (ev) => {
        state.search = ev.target.value;
        render();
    });
    document.getElementById("topo-search").addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" || !state.data) return;
        const hit = state.data.endpoints.find((ep) => endpointVisible(ep) && matches(ep));
        if (!hit) return;
        const p = state.pos.get(hit.id), r = svg.getBoundingClientRect();
        state.view.x = r.width / 2 - p.x * state.view.k;
        state.view.y = r.height / 2 - p.y * state.view.k;
        select({ type: "ep", id: hit.id });
    });

    load(true);
    setInterval(() => load(false), 60000);
})();
{% endverbatim %}
