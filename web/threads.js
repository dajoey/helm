/* THREADS: every thread this site has ever held, done ones included, as the second
   section of the left pane. Newest first, paged (100 rows a page, "more" appends the next),
   with the filters the route carries (#/threads?state=&from=&to=&who=&q=&gate=1&page=):
   state chips (needs you, in flight, on hold, backlog, done, muted) with the facet
   counts, a date range on the last change, an agent or runner, and full-text search. A row
   opens its conversation in the middle pane (#/threads/<id>?…), the list staying beside it.
   Data: GET /api/triage/threads/index (helm.store.index_threads), light rows, no events. */
"use strict";
(() => {
  const host = document.getElementById("list-threads");
  const PAGE = 100;
  const STATES = [["needs-you", "needs you"], ["working", "in flight"], ["waiting-external", "on hold"], ["backlog", "backlog"], ["done", "done"], ["muted", "muted"]];
  const S = { params: new URLSearchParams(), pages: [], total: null, matched: 0, by_state: {}, agents: [], loading: false, err: "", seq: 0 };
  let timer = null;
  // Every thread the list has shown, for app.js's draft and fold hygiene. It is replaced when a
  // page 1 lands and added to by later pages, never emptied while a read is out; null until the
  // first read lands.
  let listed = null;

  // The filters as the address holds them now. Not Helm.route: that is the last navigation to
  // finish, and a filter set right after "clear filters" would put the cleared ones back.
  const params = () => { const r = Helm.parseHash(); return (r.view === "threads" && r.params) ? new URLSearchParams(r.params) : new URLSearchParams(); };
  const epoch = (ymd, end) => { if (!/^\d{4}-\d{2}-\d{2}$/.test(ymd || "")) return null; const d = new Date(ymd + "T00:00:00"); if (end) d.setDate(d.getDate() + 1); return d.getTime() / 1000; };
  function query(p, page) {
    const q = new URLSearchParams();
    if (p.get("state")) q.set("state", p.get("state"));
    const from = epoch(p.get("from")), to = epoch(p.get("to"), true);
    if (from != null) q.set("since", String(from));
    if (to != null) q.set("until", String(to));
    if (p.get("who")) q.set("who", p.get("who"));
    if (p.get("q")) q.set("q", p.get("q"));
    if (p.get("gate") === "1") q.set("ids", [...state.gateIds].join(",") || "-");
    q.set("page", String(page)); q.set("limit", String(PAGE));
    return "/api/triage/threads/index?" + q.toString();
  }
  async function loadPage(page) {
    const my = ++S.seq;
    S.loading = true; S.err = "";
    draw();
    try {
      const r = await api(query(S.params, page));
      if (my !== S.seq) return;
      S.pages[page - 1] = r.threads || [];
      const ids = S.pages[page - 1].map(t => t.thread_id);
      listed = page === 1 || !listed ? new Set(ids) : new Set([...listed, ...ids]);
      S.pages.length = page;
      S.total = r.total; S.matched = r.matched; S.pagesN = r.pages; S.by_state = r.by_state || {}; S.agents = r.agents || [];
      Helm.setCount("threads", r.total);
    } catch (e) { if (my === S.seq) S.err = e.message; }
    finally { if (my === S.seq) { S.loading = false; draw(); } }
    return my === S.seq;   // false: a newer read (a filter, a refresh) took the list over
  }
  function reload() { S.pages = []; return loadPage(1); }
  function go(p) { const tid = Helm.parseHash().tid; Helm.replace(tid ? "#/threads/" + encodeURIComponent(tid) + (p.toString() ? "?" + p.toString() : "") : Helm.threadsHref(p)); }
  function setParam(k, v) { const p = params(); if (v) p.set(k, v); else p.delete(k); go(p); }

  const stateLabel = (s) => ({ "needs-you": "needs you", working: "in flight", "waiting-external": "on hold", backlog: "backlog", done: "done", muted: "muted" })[s] || s;
  function row(t, on) {
    const a = el("a", "lrow " + t.state + (t.state === "needs-you" && t.urgency === "now" ? " hot" : "") + (on ? " on" : ""));
    a.href = Helm.convHref(t.thread_id, "threads");
    a.dataset.id = t.thread_id;
    a.append(el("span", "bar"));
    const main = el("div");
    main.append(el("div", "ltitle", t.title));
    const meta = el("div", "lmeta");
    meta.append(el("span", "badge " + (t.state === "needs-you" ? "urg-" + (t.urgency || "whenever") : t.state === "done" ? "done-badge" : t.state === "working" ? "agent-badge" : "wait-badge"), stateLabel(t.state)));
    if (t.category) meta.append(el("span", "lcat", t.category));   // quiet text, not a second box (2026-10-01)
    const who = (t.agent || t.responder || "").split(":")[0];
    if (who && who !== "owner") meta.append(el("span", null, who));
    if (t.parked) meta.append(el("span", null, "parked"));
    if (t.wake_at && t.wake_at * 1000 > Date.now()) meta.append(el("span", null, "snoozed"));
    main.append(meta);
    const age = el("span", "lage", fmtAge(askedAt(t)));
    const at = askedAt(t);
    if (at) age.dataset.clock = new Date(at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    a.append(main, age);
    return a;
  }

  // The header's inputs are built once and repainted in place. A read that lands while the owner is
  // typing (a slow host) used to rebuild them, which took the focus and, on Enter, the text.
  const box = {};
  function buildHeader() {
    const h = el("div", "lhead");
    const q = el("input"); q.type = "search"; q.placeholder = "search every thread (full text)"; q.setAttribute("aria-label", "Search threads");
    q.addEventListener("change", () => setParam("q", q.value.trim()));
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); setParam("q", q.value.trim()); } });
    const from = el("input"); from.type = "date"; from.title = "Changed on or after"; from.setAttribute("aria-label", "From date");
    from.addEventListener("change", () => setParam("from", from.value));
    const to = el("input"); to.type = "date"; to.title = "Changed on or before"; to.setAttribute("aria-label", "To date");
    to.addEventListener("change", () => setParam("to", to.value));
    const who = el("input", "lwho"); who.type = "text"; who.placeholder = "agent or runner (e.g. worker, agent)"; who.setAttribute("list", "threads-agents"); who.setAttribute("aria-label", "Agent or runner");
    who.addEventListener("change", () => setParam("who", who.value.trim()));
    who.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); setParam("who", who.value.trim()); } });
    const dl = el("datalist"); dl.id = "threads-agents";
    // The date range and the agent box sit in one wrap, built once so typed text survives; on a phone they open from "More filters".
    for (const i of [q, from, to, who]) i.classList.add("hm-input");
    const fw = el("div", "lfilters");
    const dates = el("div", "lfilters-dates"); dates.append(from, to);
    fw.append(dates, who);
    const tog = el("button", "lfilters-toggle", "More filters"); tog.type = "button"; tog.setAttribute("aria-expanded", "false");
    tog.onclick = () => { S.moreOpen = !h.classList.contains("more-open"); paintMore(); };
    h.append(q, tog, fw, dl);
    Object.assign(box, { head: h, q, from, to, who, dl, tog });
    return h;
  }
  // "More filters" is open when asked for, or while any of the three is in use (a filter in force is never hidden)
  function paintMore() {
    const p = S.params, used = !!(p.get("from") || p.get("to") || p.get("who"));
    const open = !!S.moreOpen || used;
    box.head.classList.toggle("more-open", open);
    box.tog.setAttribute("aria-expanded", String(open));
    box.tog.textContent = open && !used ? "Fewer filters" : used ? "Filters in use" : "More filters";
  }
  function paintHeader() {
    const p = S.params;
    paintMore();
    // the box being typed in keeps its text; the others show the filters this render was read with
    for (const k of ["q", "from", "to", "who"]) if (document.activeElement !== box[k]) box[k].value = p.get(k) || "";
    box.head.querySelectorAll(".chip").forEach(b => b.remove());
    const chips = [];
    const on = new Set((p.get("state") || "").split(",").filter(Boolean));
    for (const [key, label] of STATES) {
      if (key === "backlog" && !isWork()) continue;
      const n = S.by_state[key];
      const b = el("button", "chip" + (on.has(key) ? " on" : ""), label + (n != null ? " " + n : "")); b.type = "button";
      b.title = "Show " + label + " threads" + (on.size ? " (toggle)" : "");
      b.onclick = () => { on.has(key) ? on.delete(key) : on.add(key); setParam("state", [...on].join(",")); };
      chips.push(b);
    }
    if (!isWork() && (state.gateCount || p.get("gate") === "1")) {
      const g = el("button", "chip" + (p.get("gate") === "1" ? " on" : ""), "held by gate " + state.gateCount); g.type = "button";
      g.title = "What the gate kept from you in the last 14 days";
      g.onclick = () => setParam("gate", p.get("gate") === "1" ? "" : "1");
      chips.push(g);
    }
    box.q.after(...chips);   // the chips sit under the search box, above the toggle
    box.dl.replaceChildren(...S.agents.map(a => { const o = el("option"); o.value = a.agent; return o; }));
    const active = [...p.keys()].filter(k => p.get(k)).length;
    if (active) {
      const clear = el("button", "chip", "clear filters"); clear.type = "button";
      clear.onclick = () => go(new URLSearchParams());
      box.dl.after(clear);
    }
  }

  function titleBar() {
    const h = el("header", "list-title");
    h.append(el("h2", null, "Threads"), el("p", "hm-meta", "Newest first"));
    return h;
  }
  function draw() {
    const r = Helm.route;
    const on = r.view === "threads" ? r.tid : null;
    const head = box.head || buildHeader();
    if (host.firstChild !== head && !(host.firstChild && host.firstChild.classList.contains("list-title") && host.firstChild.nextSibling === head)) { host.replaceChildren(titleBar(), head); }
    while (head.nextSibling) head.nextSibling.remove();
    paintHeader();
    // What this render shows: the filters it was read with, and whether a read is still out
    // (the drives wait on these rather than on "some rows are on screen").
    host.dataset.q = S.params.toString(); host.dataset.loading = S.loading ? "1" : "";
    const rows = S.pages.flat();
    const stat = el("div", "lmore");
    stat.append(el("span", null, S.total == null ? (S.loading ? "reading…" : "") : `${S.matched} of ${S.total} thread${S.total === 1 ? "" : "s"}${S.loading ? " · reading…" : ""}`));
    host.append(stat);
    if (S.err) host.append(el("div", "lempty", "Threads unreachable: " + S.err));
    else if (!rows.length && !S.loading) host.append(el("div", "lempty", "No thread matches these filters"));
    for (const t of rows) host.append(row(t, t.thread_id === on));
    const loaded = S.pages.length;
    if (S.pagesN > loaded) {
      const more = el("div", "lmore");
      more.append(el("span", null, `page ${loaded} of ${S.pagesN}`));
      const b = el("button", "btn quiet", `more (${Math.min(PAGE, S.matched - rows.length)})`); b.type = "button";
      b.onclick = () => loadPage(loaded + 1);
      more.append(b);
      host.append(more);
    }
    if (r.view === "threads" && !r.tid) Helm.emptyState("Pick a thread", `${S.total == null ? "Every" : S.total} thread${S.total === 1 ? "" : "s"} this site has held, newest first. Choose one on the left to read its conversation.`, { glyph: "thread" });
  }

  function onRoute(r) {
    clearInterval(timer); timer = null;
    if (r.view !== "threads") return;
    const p = params();
    const changed = p.toString() !== S.params.toString();
    S.params = p;
    if (changed || !S.pages.length) reload(); else draw();
    timer = setInterval(() => { if (!document.hidden && Helm.route.view === "threads") reloadKeep(); }, 60_000);
  }
  // A refresh re-reads every page already open, so a long scroll is not lost. It stops when a
  // newer read takes over: its next page would land on that read's filters without their page 1.
  async function reloadKeep() {
    const n = Math.max(1, S.pages.length);
    S.pages = [];
    for (let i = 1; i <= n; i++) if (!(await loadPage(i))) return;
  }
  // null: THREADS is on screen and its first read has not landed, so nobody can say what it holds.
  window.threadsListed = () => (listed === null && Helm.parseHash().view === "threads") ? null : (listed || new Set());
  Helm.on("route", onRoute);
  Helm.on("refresh", () => { if (Helm.route.view === "threads") reloadKeep(); });
  Helm.on("acted", () => { if (Helm.route.view === "threads") reloadKeep(); });
  Helm.on("threads", () => { if (Helm.route.view === "threads" && S.pages.length) draw(); });
  // The count is wanted on every screen (the nav row), so it is read once on load.
  Helm.ready.then(async () => {
    try { const r = await api("/api/triage/threads/index?limit=1"); Helm.setCount("threads", r.total); } catch { /* the row shows no count until the list is opened */ }
  });
})();
